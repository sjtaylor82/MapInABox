// Tilt control for phones and tablets.
//
// The way the device is held when tilt is turned on (or when the map is
// tapped) counts as level. Tilting past a dead zone moves the cursor in that
// direction; the further the tilt, the faster and bigger the steps. Only the
// stronger of the two tilts counts, so a slightly diagonal hold still moves
// cleanly north, south, east or west.

const DEAD_ZONE = 12;   // degrees of tilt that do nothing
const HOLD_MS = 250;    // a tilt must be held this long before the first step
const TICK_MS = 50;

// [minimum tilt in degrees, milliseconds between steps, step size]
// No faster than 0.8 seconds, so each place has time to be spoken.
const TIERS = [
  [40, 900, 'large'],
  [25, 800, 'normal'],
  [DEAD_ZONE, 1200, 'normal'],
];

let enabled = false;
let reading = null;       // latest { beta, gamma }
let level = null;         // { beta, gamma } counted as level
let tiltSince = 0;        // when the current direction was first held
let tiltDir = null;
let lastStep = 0;
let timer = null;
let wakeLock = null;
let onStep = () => {};

export function tiltSupported() {
  return typeof window.DeviceOrientationEvent !== 'undefined';
}

export function isTouchDevice() {
  return matchMedia('(pointer: coarse)').matches || navigator.maxTouchPoints > 0;
}

/**
 * Ask for motion access. iPhone and iPad need this to come from a tap, so
 * call it straight from a click handler. Resolves true when tilt can be used.
 */
export async function requestTiltPermission() {
  if (!tiltSupported()) return false;
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

/** Read beta (forward/back) and gamma (left/right) as the user sees them, allowing for screen rotation. */
function screenRelative(e) {
  const angle = (screen.orientation && screen.orientation.angle) || window.orientation || 0;
  const { beta, gamma } = e;
  switch ((angle + 360) % 360) {
    case 90: return { beta: -gamma, gamma: beta };
    case 270: return { beta: gamma, gamma: -beta };
    case 180: return { beta: -beta, gamma: -gamma };
    default: return { beta, gamma };
  }
}

function onOrientation(e) {
  if (e.beta == null || e.gamma == null) return;
  reading = screenRelative(e);
  if (!level) level = { ...reading };
}

/** Count the current hold as level. */
export function setLevel() {
  level = reading ? { ...reading } : null;
  tiltDir = null;
  return !!reading;
}

function tick() {
  if (!enabled || !reading || !level) return;
  // Forward (top edge away from you) lowers beta; right side down raises gamma.
  const forward = level.beta - reading.beta;
  const right = reading.gamma - level.gamma;
  const big = Math.abs(forward) >= Math.abs(right);
  const amount = big ? Math.abs(forward) : Math.abs(right);
  const now = performance.now();

  if (amount < DEAD_ZONE) {
    tiltDir = null;
    return;
  }
  const dir = big ? (forward > 0 ? 'north' : 'south') : (right > 0 ? 'east' : 'west');
  if (dir !== tiltDir) {
    tiltDir = dir;
    tiltSince = now;
    return;
  }
  if (now - tiltSince < HOLD_MS) return;
  const [, interval, size] = TIERS.find(([min]) => amount >= min);
  if (now - lastStep < interval) return;
  lastStep = now;
  onStep(dir, size, amount);
}

async function keepAwake() {
  try {
    if ('wakeLock' in navigator && document.visibilityState === 'visible') {
      wakeLock = await navigator.wakeLock.request('screen');
    }
  } catch { /* not allowed; the screen may dim */ }
}

function onVisibility() {
  if (enabled && document.visibilityState === 'visible') keepAwake();
}

export function startTilt(stepHandler) {
  if (enabled) return;
  onStep = stepHandler;
  enabled = true;
  level = null;
  tiltDir = null;
  window.addEventListener('deviceorientation', onOrientation);
  document.addEventListener('visibilitychange', onVisibility);
  timer = setInterval(tick, TICK_MS);
  keepAwake();
}

export function stopTilt() {
  enabled = false;
  window.removeEventListener('deviceorientation', onOrientation);
  document.removeEventListener('visibilitychange', onVisibility);
  clearInterval(timer);
  timer = null;
  reading = null;
  level = null;
  if (wakeLock) wakeLock.release().catch(() => {});
  wakeLock = null;
}

export function tiltActive() {
  return enabled;
}

/** True once the device has actually reported a tilt (desktops never do). */
export function tiltReporting() {
  return !!reading;
}

export function buzz(ms = 15) {
  try { navigator.vibrate?.(ms); } catch { /* not available on iPhone */ }
}
