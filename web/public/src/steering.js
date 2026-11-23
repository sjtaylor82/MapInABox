// Tilt steering for phones and tablets.
//
// It works like the arrow keys. Hold the device at a comfortable angle,
// upright or sideways, screen towards you; the way you hold it when it says
// "Ready" is "still". Then tilt it firmly one way to move once that way:
//   top edge away from you: north        towards you: south
//   lower the right side: east           lower the left side: west
// Come back to still (a soft click) before the next move. Hold the tilt and
// it repeats, like holding down an arrow key. Only the four directions
// count: a tilt halfway between two does nothing.
//
// Turn it right over, or put it down at a very different angle, and
// steering pauses; once it is held steady again, that becomes the new
// still. This is also how to reset "still" after shifting your grip.

const TICK_MS = 50;
const GO_TILT = 0.21;        // about 12 degrees from still: move
const STILL_TILT = 0.10;     // back within about 6 degrees: still again
const DOMINANCE = 1.5;       // the main tilt must be this much bigger than the other
const SETTLE_MS = 120;       // a direction must hold this long before it counts
const CALIBRATE_MS = 500;
const PAUSE_MS = 700;

const rad = (d) => (d * Math.PI) / 180;
const deg = (r) => (r * 180) / Math.PI;
const norm = (a) => ((a % 360) + 360) % 360;

let enabled = false;
let up = null;              // smoothed "up" in the device's frame { x, y, z }
let hasReported = false;
let mode = 'paused';        // 'paused' | 'calibrating' | 'ready'
let lostSince = 0;
let calibrateSince = 0;
let still = null;
let top = { x: 0, y: 1 };   // the screen's top edge, in the device's x-y plane
let anchor = null;          // while calibrating: the hold being checked for steadiness
let latched = null;         // direction moved in since last still, or null
let pendingDir = null;
let pendingSince = 0;
let nextRepeatAt = 0;
let lastTilt = { north: 0, east: 0 };
let timer = null;
let wakeLock = null;
let handlers = {};
let repeatMs = () => 1200;

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
 * How far the device is tilted from still, as { east, north } in radians.
 * `top` is the direction of the screen's top edge as the user sees it, in
 * the device's own x-y plane, so this works held upright or sideways and
 * whichever way the screen has rotated.
 * The end that goes down is the way you go, like a marble on a tray.
 */
export function tiltFrom(u, s, top) {
  const right = { x: top.y, y: -top.x };
  const along = (v, d) => v.x * d.x + v.y * d.y;
  // Top edge away from you turns "up" from the screen's top edge towards its face.
  const lean = (v) => Math.atan2(v.z, along(v, top));
  // Right end down turns "up" towards the screen's left.
  const roll = (v) => Math.asin(Math.max(-1, Math.min(1, along(v, right))));
  let north = lean(u) - lean(s);
  if (north > Math.PI) north -= 2 * Math.PI;
  if (north < -Math.PI) north += 2 * Math.PI;
  return { north, east: -(roll(u) - roll(s)) };
}

/** The one direction a tilt points (0, 90, 180 or 270), or null if it is too small or between two. */
export function directionOf(t) {
  const n = Math.abs(t.north);
  const e = Math.abs(t.east);
  const big = Math.max(n, e);
  if (big < GO_TILT || big < DOMINANCE * Math.min(n, e)) return null;
  if (n >= e) return t.north > 0 ? 0 : 180;
  return t.east > 0 ? 90 : 270;
}

function onOrientation(e) {
  if (e.beta == null || e.gamma == null) return;
  hasReported = true;
  const u = upFrom(e.beta, e.gamma);
  up = up ? { x: up.x + (u.x - up.x) * 0.4, y: up.y + (u.y - up.y) * 0.4, z: up.z + (u.z - up.z) * 0.4 } : u;
}

function emit(name, ...args) {
  try { handlers[name]?.(...args); } catch (err) { console.error(err); }
}

/**
 * Which way is the top of the screen, in the device's x-y plane. Held at an
 * angle, gravity shows it directly; lying nearly flat, fall back on the
 * screen's rotation.
 */
function chooseTop() {
  const plane = Math.hypot(still.x, still.y);
  if (plane > 0.25) return { x: still.x / plane, y: still.y / plane };
  const angle = norm((screen.orientation && screen.orientation.angle) ?? window.orientation ?? 0);
  return { 0: { x: 0, y: 1 }, 90: { x: 1, y: 0 }, 180: { x: 0, y: -1 }, 270: { x: -1, y: 0 } }[angle] || { x: 0, y: 1 };
}

const dot = (a, b) => a.x * b.x + a.y * b.y + a.z * b.z;

function tick() {
  if (!enabled) return;
  const now = performance.now();

  if (!up) return;

  // Turned right over or put down at a very different angle: pause. Steering
  // starts again, from a new still, once the device is held steady.
  const far = mode === 'ready' && dot(up, still) < Math.cos(rad(55));
  if (far) {
    if (!lostSince) lostSince = now;
    if (now - lostSince >= PAUSE_MS) {
      mode = 'paused';
      latched = null;
      emit('paused');
    }
    return;
  }
  lostSince = 0;

  if (mode === 'paused') {
    mode = 'calibrating';
    calibrateSince = now;
    anchor = { ...up };
    return;
  }
  if (mode === 'calibrating') {
    // Wait until it is held steady, not still being picked up.
    if (dot(up, anchor) < Math.cos(rad(4))) {
      anchor = { ...up };
      calibrateSince = now;
      return;
    }
    if (now - calibrateSince < CALIBRATE_MS) return;
    still = { ...up };
    top = chooseTop();
    mode = 'ready';
    latched = null;
    pendingDir = null;
    emit('ready');
    return;
  }

  const t = tiltFrom(up, still, top);
  lastTilt = t;
  const amount = Math.hypot(t.north, t.east);

  if (amount <= STILL_TILT) {
    pendingDir = null;
    if (latched != null) {
      latched = null;
      emit('still');
    }
    return;
  }

  const dir = directionOf(t);
  if (latched != null) {
    // Holding the same tilt repeats; any other tilt waits for still first.
    if (dir === latched && now >= nextRepeatAt) {
      nextRepeatAt = now + repeatMs(latched);
      emit('move', latched, true, t);
    }
    return;
  }
  if (dir == null) {
    pendingDir = null;
    return;
  }
  if (dir !== pendingDir) {
    pendingDir = dir;
    pendingSince = now;
    return;
  }
  if (now - pendingSince < SETTLE_MS) return;
  latched = dir;
  pendingDir = null;
  nextRepeatAt = now + repeatMs(dir);
  emit('move', dir, false, t);
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
    latched = null;
  }
}

/**
 * Start steering. handlers: { ready(), move(bearing, repeat, tilt), still(), paused() }.
 * repeat(bearing) gives the milliseconds between repeats while a tilt is held.
 */
export function startSteering(h, repeat) {
  if (enabled) return;
  handlers = h || {};
  if (repeat) repeatMs = repeat;
  enabled = true;
  up = null;
  mode = 'paused';
  latched = null;
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
  latched = null;
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

/** { mode: 'off' | 'paused' | 'calibrating' | 'ready', direction, tilt: { north, east } in degrees } */
export function steeringStatus() {
  return {
    mode: enabled ? mode : 'off',
    direction: latched,
    tilt: { north: Math.round(deg(lastTilt.north)), east: Math.round(deg(lastTilt.east)) },
  };
}

export function buzz(ms = 15) {
  try { navigator.vibrate?.(ms); } catch { /* not available on iPhone */ }
}
