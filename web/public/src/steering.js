// Tilt steering for phones and tablets, held sideways (landscape).
//
// Hold the device sideways in both hands. The angle you hold it at when it
// says "Ready" is "still". From there, tilt it gently towards the way you
// want to go, like rolling a marble on a tray:
//   top edge down and away: north        towards you: south
//   left edge down: west                 right edge down: east
//   and in between for the diagonals.
// A new direction is spoken first, then movement starts a moment later.
// Come back to the starting angle and it stops. Nothing needs tapping.
//
// Hold it upright like a phone, or put it down, and steering pauses. Pick it
// up sideways again and the new angle counts as still: that is also how to
// reset "still" after shifting your grip.

const TICK_MS = 50;
const GO_TILT = 0.17;        // about 10 degrees from still: start moving
const STOP_TILT = 0.10;      // back within about 6 degrees: stop
const FAST_TILT = 0.34;      // about 20 degrees: quicker steps
const SECTOR = 45;
const HYSTERESIS = 8;        // degrees past a sector edge before the direction changes
const SETTLE_MS = 250;       // a direction must hold this long before it is spoken
const AFTER_HEADING_MS = 700; // time to hear the direction before the next step
const STEP_MS = 1100;
const FAST_STEP_MS = 800;
const CALIBRATE_MS = 500;
const PAUSE_MS = 700;

const rad = (d) => (d * Math.PI) / 180;
const deg = (r) => (r * 180) / Math.PI;
const norm = (a) => ((a % 360) + 360) % 360;
const gap = (a, b) => Math.abs(((a - b + 540) % 360) - 180);

let enabled = false;
let up = null;              // smoothed "up" in the device's frame { x, y, z }
let sideways = false;       // the latest reading is a landscape (or flat) hold
let hasReported = false;
let mode = 'paused';        // 'paused' | 'calibrating' | 'ready'
let lostSince = 0;
let calibrateSince = 0;
let still = null;           // "up" when ready: the still position
let side = 1;               // 1: top edge to the left (screen up is device +x); -1: to the right
let dir = null;             // spoken direction, a compass bearing, or null when still
let pendingDir = null;
let pendingSince = 0;
let moving = false;
let fast = false;
let nextStepAt = 0;
let timer = null;
let wakeLock = null;
let handlers = {};

export function steeringSupported() {
  return typeof window.DeviceOrientationEvent !== 'undefined';
}

export function isTouchDevice() {
  return matchMedia('(pointer: coarse)').matches || navigator.maxTouchPoints > 0;
}

/**
 * Ask for motion access. iPhone and iPad need this to come from a tap, so
 * call it straight from a click handler. Resolves true when allowed.
 */
export async function requestSteeringPermission() {
  if (!steeringSupported()) return false;
  const DOE = window.DeviceOrientationEvent;
  if (typeof DOE.requestPermission === 'function') {
    try {
      return (await DOE.requestPermission()) === 'granted';
    } catch {
      return false;
    }
  }
  return true;
}

/** "Up" in the device's frame (x to its right edge, y to its top, z out of the screen). */
export function upFrom(beta, gamma) {
  const b = rad(beta);
  const g = rad(gamma);
  return { x: -Math.sin(g) * Math.cos(b), y: Math.sin(b), z: Math.cos(b) * Math.cos(g) };
}

/**
 * How far the device is tilted from still, as { east, north } in radians,
 * for a landscape hold with the top edge on `side` (1: to the left).
 * A marble rolls downhill: the edge that goes down is the way you go.
 */
export function tiltFrom(u, s, sideNow) {
  // Top edge to the left: screen up is device +x, screen right is device -y.
  // Top edge away turns "up" from the screen's top edge towards its face.
  const lean = (v) => Math.atan2(v.z, sideNow * v.x);
  // Right edge down turns "up" towards the device's right-hand side.
  const roll = (v) => Math.asin(Math.max(-1, Math.min(1, -sideNow * v.y)));
  let north = lean(u) - lean(s);
  if (north > Math.PI) north -= 2 * Math.PI;
  if (north < -Math.PI) north += 2 * Math.PI;
  return { north, east: -(roll(u) - roll(s)) };
}

function onOrientation(e) {
  if (e.beta == null || e.gamma == null) return;
  hasReported = true;
  const u = upFrom(e.beta, e.gamma);
  up = up ? { x: up.x + (u.x - up.x) * 0.4, y: up.y + (u.y - up.y) * 0.4, z: up.z + (u.z - up.z) * 0.4 } : u;
  const plane = Math.hypot(up.x, up.y);
  // Landscape when the device's long side is level, or when it is nearly flat.
  sideways = plane < 0.35 || Math.abs(up.x) >= Math.abs(up.y) * 0.8;
}

function pointFor(bearing, current) {
  if (current != null && gap(bearing, current) <= SECTOR / 2 + HYSTERESIS) return current;
  return norm(Math.round(bearing / SECTOR) * SECTOR);
}

function emit(name, ...args) {
  try { handlers[name]?.(...args); } catch (err) { console.error(err); }
}

function chooseSide() {
  if (Math.abs(still.x) > 0.3) return still.x > 0 ? 1 : -1;
  const angle = (screen.orientation && screen.orientation.angle) ?? window.orientation ?? 90;
  return norm(angle) === 270 ? -1 : 1;
}

function tick() {
  if (!enabled) return;
  const now = performance.now();

  if (!up || !sideways) {
    if (mode === 'paused') return;
    if (!lostSince) lostSince = now;
    if (now - lostSince >= PAUSE_MS) {
      const wasMoving = moving;
      mode = 'paused';
      moving = false;
      dir = null;
      emit('paused', wasMoving);
    }
    return;
  }
  lostSince = 0;

  if (mode === 'paused') {
    mode = 'calibrating';
    calibrateSince = now;
    return;
  }
  if (mode === 'calibrating') {
    if (now - calibrateSince < CALIBRATE_MS) return;
    still = { ...up };
    side = chooseSide();
    mode = 'ready';
    moving = false;
    dir = null;
    pendingDir = null;
    emit('ready');
    return;
  }

  const t = tiltFrom(up, still, side);
  const amount = Math.hypot(t.east, t.north);
  fast = amount >= FAST_TILT;

  if (amount <= STOP_TILT || (!moving && dir == null && amount < GO_TILT)) {
    pendingDir = null;
    if (dir != null) {
      dir = null;
      const wasMoving = moving;
      moving = false;
      emit('stop', wasMoving);
    }
    return;
  }

  const point = pointFor(norm(deg(Math.atan2(t.east, t.north))), dir);
  if (point !== dir) {
    if (point !== pendingDir) {
      pendingDir = point;
      pendingSince = now;
    } else if (now - pendingSince >= SETTLE_MS) {
      dir = point;
      pendingDir = null;
      nextStepAt = now + AFTER_HEADING_MS;
      emit('heading', dir, moving);
    }
  }
  if (dir != null && now >= nextStepAt) {
    if (!moving) {
      moving = true;
      emit('go', dir);
    }
    nextStepAt = now + (fast ? FAST_STEP_MS : STEP_MS);
    emit('step', dir, fast);
  }
}

async function keepAwake() {
  try {
    if ('wakeLock' in navigator && document.visibilityState === 'visible') {
      wakeLock = await navigator.wakeLock.request('screen');
    }
  } catch { /* not allowed; the screen may dim */ }
}

function onVisibility() {
  if (!enabled) return;
  if (document.visibilityState === 'visible') keepAwake();
  else {
    // Coming back to the page starts again from still.
    up = null;
    mode = 'paused';
    moving = false;
    dir = null;
  }
}

/**
 * Start steering. handlers: { ready(), heading(bearing, moving), go(bearing),
 * step(bearing, fast), stop(wasMoving), paused(wasMoving) }.
 */
export function startSteering(h) {
  if (enabled) return;
  handlers = h || {};
  enabled = true;
  up = null;
  mode = 'paused';
  moving = false;
  dir = null;
  lostSince = 0;
  window.addEventListener('deviceorientation', onOrientation);
  document.addEventListener('visibilitychange', onVisibility);
  timer = setInterval(tick, TICK_MS);
  keepAwake();
}

export function stopSteering() {
  enabled = false;
  window.removeEventListener('deviceorientation', onOrientation);
  document.removeEventListener('visibilitychange', onVisibility);
  clearInterval(timer);
  timer = null;
  up = null;
  mode = 'paused';
  moving = false;
  dir = null;
  if (wakeLock) wakeLock.release().catch(() => {});
  wakeLock = null;
}

export function steeringActive() {
  return enabled;
}

/** True once the device has reported its orientation (desktops never do). */
export function steeringReporting() {
  return hasReported;
}

/** { mode: 'off' | 'paused' | 'calibrating' | 'ready', moving, heading, fast } */
export function steeringStatus() {
  return { mode: enabled ? mode : 'off', moving, heading: dir, fast };
}

export function buzz(ms = 15) {
  try { navigator.vibrate?.(ms); } catch { /* not available on iPhone */ }
}
