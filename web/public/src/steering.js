// Dial steering for phones and tablets.
//
// Lay the device flat, screen up, like a compass. Turn it round: the way its
// top edge points is the way you will go, using the real compass, and each
// new direction (one of eight) is spoken as you turn. Tip it a little off
// flat, any edge, to move one step that way; keep it tipped to keep moving.
// Lay it flat again to stop (a soft click). Which edge you tip does not
// matter, so a tip can never send you the wrong way.
//
// Hold it upright, or tip it right up, and steering pauses until it is
// lying flat again.
//
// The heading comes from the compass: webkitCompassHeading on iPhone and
// iPad, the absolute orientation event elsewhere.

const TICK_MS = 50;
const SECTOR = 45;
const HYSTERESIS = 8;        // degrees past a sector edge before the direction changes
const SETTLE_MS = 300;       // a new direction must hold this long before it is spoken
const TIP_GO = 0.26;         // about 15 degrees off flat: move
const TIP_FLAT = 0.14;       // back within about 8 degrees: flat again
const FLAT_ENOUGH = 0.75;    // "up" through the screen at least this much counts as lying flat
const CALIBRATE_MS = 500;
const PAUSE_MS = 900;

const rad = (d) => (d * Math.PI) / 180;
const norm = (a) => ((a % 360) + 360) % 360;
const gap = (a, b) => Math.abs(((a - b + 540) % 360) - 180);

let enabled = false;
let up = null;               // smoothed "up" in the device's frame
let compass = null;          // smoothed compass bearing of the top edge
let accuracy = null;
let absoluteSeen = false;
let hasReported = false;
let noCompass = false;
let startedAt = 0;
let mode = 'paused';         // 'paused' | 'calibrating' | 'ready'
let lostSince = 0;
let calibrateSince = 0;
let heading = null;          // spoken direction, one of the eight compass points
let pending = null;
let pendingSince = 0;
let tipped = false;
let nextRepeatAt = 0;
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
 * Ask for motion and compass access. iPhone and iPad need this to come from
 * a tap, so call it straight from a click handler. Resolves true when allowed.
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

function smoothAngle(prev, next, k) {
  if (prev == null) return next;
  const d = ((next - prev + 540) % 360) - 180;
  return norm(prev + d * k);
}

function onOrientation(e) {
  if (e.beta == null || e.gamma == null) return;
  const isAbsolute = e.type === 'deviceorientationabsolute' || e.absolute === true;
  const hasWebkit = typeof e.webkitCompassHeading === 'number' && e.webkitCompassHeading >= 0;
  if (isAbsolute) absoluteSeen = true;
  else if (absoluteSeen && !hasWebkit) return; // Android sends both: use the absolute one
  hasReported = true;
  const u = upFrom(e.beta, e.gamma);
  up = up ? { x: up.x + (u.x - up.x) * 0.4, y: up.y + (u.y - up.y) * 0.4, z: up.z + (u.z - up.z) * 0.4 } : u;
  let c = null;
  if (hasWebkit) {
    c = e.webkitCompassHeading; // iPhone and iPad: where the top edge points
    accuracy = typeof e.webkitCompassAccuracy === 'number' ? e.webkitCompassAccuracy : null;
  } else if (isAbsolute && e.alpha != null) {
    c = norm(360 - e.alpha); // alpha turns anticlockwise from north
  }
  if (c != null) compass = smoothAngle(compass, c, 0.35);
}

function pointFor(bearing, current) {
  if (current != null && gap(bearing, current) <= SECTOR / 2 + HYSTERESIS) return current;
  return norm(Math.round(bearing / SECTOR) * SECTOR);
}

function emit(name, ...args) {
  try { handlers[name]?.(...args); } catch (err) { console.error(err); }
}

/**
 * Sine of the angle the device is tipped from truly flat. Always measured
 * from level, never from the angle it happened to be at when it said Ready,
 * so lying flat on a table is always still.
 */
function tipAmount() {
  const z = Math.max(-1, Math.min(1, up.z));
  return Math.sqrt(1 - z * z);
}

function tick() {
  if (!enabled || !up) return;
  const now = performance.now();
  if (compass == null) {
    if (hasReported && !noCompass && now - startedAt > 3000) {
      noCompass = true;
      emit('nocompass');
    }
    return;
  }

  // Not lying flat (held upright, or tipped right up): pause.
  if (up.z < FLAT_ENOUGH) {
    if (mode === 'paused') return;
    if (!lostSince) lostSince = now;
    if (now - lostSince >= PAUSE_MS) {
      mode = 'paused';
      tipped = false;
      emit('paused');
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
    // Ready only once it has been lying flat for a moment.
    if (tipAmount() > TIP_FLAT) calibrateSince = now;
    if (now - calibrateSince < CALIBRATE_MS) return;
    heading = pointFor(compass, null);
    pending = heading;
    tipped = false;
    mode = 'ready';
    emit('ready', heading, accuracy);
    return;
  }

  // Turning the dial: a new direction once it has settled.
  const point = pointFor(compass, heading);
  if (point !== pending) {
    pending = point;
    pendingSince = now;
  } else if (point !== heading && now - pendingSince >= SETTLE_MS) {
    heading = point;
    emit('heading', heading);
    if (tipped) nextRepeatAt = Math.max(nextRepeatAt, now + 700); // time to hear it
  }

  // Tipping: move; flat again: stop.
  const tip = tipAmount();
  if (tipped && tip <= TIP_FLAT) {
    tipped = false;
    emit('still');
    return;
  }
  if (!tipped && tip >= TIP_GO) {
    tipped = true;
    nextRepeatAt = now + repeatMs(heading);
    emit('move', heading, false);
    return;
  }
  if (tipped && now >= nextRepeatAt) {
    nextRepeatAt = now + repeatMs(heading);
    emit('move', heading, true);
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
    up = null;
    mode = 'paused';
    tipped = false;
  }
}

/**
 * Start steering. handlers: { ready(heading, accuracy), heading(h), move(h, repeat),
 * still(), paused(), nocompass() }. repeat(h) gives the milliseconds between
 * moves while the device stays tipped.
 */
export function startSteering(h, repeat) {
  if (enabled) return;
  handlers = h || {};
  if (repeat) repeatMs = repeat;
  enabled = true;
  up = null;
  compass = null;
  noCompass = false;
  mode = 'paused';
  tipped = false;
  lostSince = 0;
  startedAt = performance.now();
  window.addEventListener('deviceorientationabsolute', onOrientation);
  window.addEventListener('deviceorientation', onOrientation);
  document.addEventListener('visibilitychange', onVisibility);
  timer = setInterval(tick, TICK_MS);
  keepAwake();
}

export function stopSteering() {
  enabled = false;
  window.removeEventListener('deviceorientationabsolute', onOrientation);
  window.removeEventListener('deviceorientation', onOrientation);
  document.removeEventListener('visibilitychange', onVisibility);
  clearInterval(timer);
  timer = null;
  up = null;
  compass = null;
  mode = 'paused';
  tipped = false;
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

/** { mode: 'off' | 'paused' | 'calibrating' | 'ready', heading, tipped, compass, accuracy } */
export function steeringStatus() {
  return {
    mode: enabled ? mode : 'off',
    heading,
    tipped,
    compass: compass == null ? null : Math.round(compass),
    accuracy,
  };
}

export function buzz(ms = 15) {
  try { navigator.vibrate?.(ms); } catch { /* not available on iPhone */ }
}
