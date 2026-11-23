// Tilt steering for phones and tablets.
//
// Lay the device flat, screen up: flat is still. Tip it well over, more than
// 60 degrees, to move, like rolling a marble on a tray:
//   top edge down, away from you: north     top edge up, towards you: south
//   right edge down: east                   left edge down: west
// Each tilt moves once; hold the tilt and it keeps moving, like holding an
// arrow key. Bring it back below 30 degrees to stop (a soft click). Only those four
// count: a tilt halfway between two does nothing. Hold it upright and
// steering pauses until it is lying down again.
//
// Flat means truly level, measured from the device's own front-to-back
// (beta) and side-to-side (gamma) angles, not from however it was held
// when steering started.

const TICK_MS = 50;
const GO_DEG = 60;           // tilt this far from flat to move: tipped right over, nothing subtle
const FLAT_DEG = 30;         // back below this: counts as flat, still
const DOMINANCE = 1.5;       // the main tilt must be this much bigger than the other
const SETTLE_MS = 150;       // a direction must hold this long before it counts
const UPRIGHT_DEG = 88;      // further than this (past upright): pause
const PAUSE_MS = 900;
const READY_MS = 400;        // lying flat this long before it says Ready

let enabled = false;
let tilt = null;             // smoothed { north, east } in degrees from flat
let hasReported = false;
let mode = 'paused';         // 'paused' | 'ready'
let lostSince = 0;
let flatSince = 0;
let latched = null;          // direction moved in since last flat, or null
let pendingDir = null;
let pendingSince = 0;
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

/**
 * A device lying screen up has beta and gamma near 0. Tipping the top edge
 * down (away from you) makes beta negative; the right edge down makes gamma
 * positive.
 */
export function tiltFrom(beta, gamma) {
  return { north: -beta, east: gamma };
}

/** The one direction a tilt points (0, 90, 180 or 270), or null if it is too small or between two. */
export function directionOf(t) {
  const n = Math.abs(t.north);
  const e = Math.abs(t.east);
  const big = Math.max(n, e);
  if (big < GO_DEG || big < DOMINANCE * Math.min(n, e)) return null;
  if (n >= e) return t.north > 0 ? 0 : 180;
  return t.east > 0 ? 90 : 270;
}

function onOrientation(e) {
  if (e.beta == null || e.gamma == null) return;
  hasReported = true;
  const t = tiltFrom(e.beta, e.gamma);
  tilt = tilt
    ? { north: tilt.north + (t.north - tilt.north) * 0.4, east: tilt.east + (t.east - tilt.east) * 0.4 }
    : t;
}

function emit(name, ...args) {
  try { handlers[name]?.(...args); } catch (err) { console.error(err); }
}

function tick() {
  if (!enabled || !tilt) return;
  const now = performance.now();
  const amount = Math.max(Math.abs(tilt.north), Math.abs(tilt.east));

  // Held upright (or upside down): pause.
  if (amount > UPRIGHT_DEG) {
    flatSince = 0;
    if (mode === 'paused') return;
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
    // Ready once it has been lying flat for a moment.
    if (amount > FLAT_DEG) {
      flatSince = 0;
      return;
    }
    if (!flatSince) flatSince = now;
    if (now - flatSince < READY_MS) return;
    mode = 'ready';
    latched = null;
    pendingDir = null;
    emit('ready');
    return;
  }

  if (amount <= FLAT_DEG) {
    pendingDir = null;
    if (latched != null) {
      latched = null;
      emit('still');
    }
    return;
  }

  const dir = directionOf(tilt);
  if (latched != null) {
    // Holding the same tilt repeats; any other tilt waits for flat first.
    if (dir === latched && now >= nextRepeatAt) {
      nextRepeatAt = now + repeatMs(latched);
      emit('move', latched, true);
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
  emit('move', dir, false);
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
    tilt = null;
    mode = 'paused';
    latched = null;
  }
}

/**
 * Start steering. handlers: { ready(), move(bearing, repeat), still(), paused() }.
 * repeat(bearing) gives the milliseconds between moves while a tilt is held.
 */
export function startSteering(h, repeat) {
  if (enabled) return;
  handlers = h || {};
  if (repeat) repeatMs = repeat;
  enabled = true;
  tilt = null;
  mode = 'paused';
  latched = null;
  lostSince = 0;
  flatSince = 0;
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
  tilt = null;
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

/** { mode: 'off' | 'paused' | 'ready', direction, tilt: { north, east } in whole degrees } */
export function steeringStatus() {
  return {
    mode: enabled ? mode : 'off',
    direction: latched,
    tilt: tilt ? { north: Math.round(tilt.north), east: Math.round(tilt.east) } : { north: 0, east: 0 },
  };
}

export function buzz(ms = 15) {
  try { navigator.vibrate?.(ms); } catch { /* not available on iPhone */ }
}
