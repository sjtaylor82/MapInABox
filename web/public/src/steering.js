// Steering wheel control for phones and tablets.
//
// Hold the device sideways, screen towards you, like a steering wheel.
//   Turn it slightly left: north-west. Further left: west.
//   Turn it slightly right: north-east. Further right: east.
//   Tilt the top edge away from you to go; tilt further to go faster.
//   Pull the top edge towards you to go south (south-west and south-east
//   when turned), and hold it upright again to stop.
//
// The wheel angle is read against gravity, so "straight" needs no setting
// up. How far forward or back you tilt is measured from the way you were
// holding the device when you picked it up as a wheel, so whatever angle is
// comfortable counts as stopped. Put the device down, or hold it upright
// like a phone, and steering pauses: nothing moves by accident.

const TICK_MS = 50;

// Wheel turn bands, in degrees from straight (left or right).
const SLIGHT = 12;      // past this: north-west or north-east
const FULL = 35;        // past this: west or east
const TOO_FAR = 72;     // past this the device is held as a phone, not a wheel
const HYSTERESIS = 3;   // a band boundary must be crossed by this much
const TURN_SETTLE_MS = 200;

// Forward or back tilt, in degrees from the calibrated hold.
const GO = 15;          // past this, start moving
const STOP = 9;         // back inside this, stop
const FAST = 32;        // past this, bigger or quicker steps
const GO_HOLD_MS = 300; // a tilt must be held this long before moving
const CALIBRATE_MS = 400;
const PAUSE_MS = 600;   // how long the device must be put down before steering pauses

const STEP_MS = 1100;
const FAST_STEP_MS = 800;
const AFTER_HEADING_MS = 700; // time to hear a new heading before the next step

// Turn band to heading: [forward or stopped, going back]. Positive = left.
const HEADING = {
  '2': [270, 270],
  '1': [315, 225],
  '0': [0, 180],
  '-1': [45, 135],
  '-2': [90, 90],
};

const deg = (r) => (r * 180) / Math.PI;
const rad = (d) => (d * Math.PI) / 180;

let enabled = false;
let reading = null;        // latest { roll, pitch } or null when not held as a wheel
let hasReported = false;
let mode = 'paused';       // 'paused' | 'calibrating' | 'ready'
let lostSince = 0;
let calibrateSince = 0;
let samples = [];
let neutral = 0;
let gear = 'stop';         // 'stop' | 'forward' | 'back'
let pendingGear = null;
let pendingGearSince = 0;
let fast = false;
let turn = 0;              // -2..2
let pendingTurn = 0;
let pendingTurnSince = 0;
let heading = 0;
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
 * call it straight from a click handler. Resolves true when steering can be used.
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
 * The wheel angle and forward tilt from the device's orientation.
 * Works whichever way round the device is held sideways, and whether or not
 * the screen has rotated, because it uses the device's own axes.
 * Returns { roll (left positive), pitch (top away positive) } or null.
 */
export function wheelFrom(beta, gamma) {
  const b = rad(beta);
  const g = rad(gamma);
  // "Up" in the device's frame: x to its right edge, y to its top, z out of the screen.
  const x = -Math.sin(g) * Math.cos(b);
  const y = Math.sin(b);
  const z = Math.cos(b) * Math.cos(g);
  if (Math.hypot(x, y) < 0.34) return null; // lying nearly flat
  // Sideways with the top to the left, +x points up; with the top to the right, -x does.
  const roll = x >= 0 ? deg(Math.atan2(-y, x)) : deg(Math.atan2(y, -x));
  const pitch = deg(Math.asin(Math.max(-1, Math.min(1, z))));
  return { roll, pitch };
}

function bandFor(roll, current) {
  const a = Math.abs(roll);
  const sign = roll >= 0 ? 1 : -1;
  const cur = Math.abs(current);
  const sameSide = current === 0 || Math.sign(current) === sign;
  // Moving out to a bigger band needs the boundary plus hysteresis; back in, minus it.
  const slight = sameSide && cur >= 1 ? SLIGHT - HYSTERESIS : SLIGHT + HYSTERESIS;
  const full = sameSide && cur >= 2 ? FULL - HYSTERESIS : FULL + HYSTERESIS;
  if (a >= full) return 2 * sign;
  if (a >= slight) return sign;
  return 0;
}

function headingFor(t, g) {
  return HEADING[String(t)][g === 'back' ? 1 : 0];
}

function emit(name, ...args) {
  try { handlers[name]?.(...args); } catch (err) { console.error(err); }
}

function onOrientation(e) {
  if (e.beta == null || e.gamma == null) return;
  hasReported = true;
  const w = wheelFrom(e.beta, e.gamma);
  reading = w && Math.abs(w.roll) <= TOO_FAR ? w : null;
}

function setHeading(now) {
  const h = headingFor(turn, gear);
  if (h === heading) return;
  heading = h;
  emit('heading', h, gear);
  nextStepAt = Math.max(nextStepAt, now + AFTER_HEADING_MS);
}

function tick() {
  if (!enabled) return;
  const now = performance.now();

  if (!reading) {
    if (mode === 'paused') return;
    if (!lostSince) lostSince = now;
    if (now - lostSince >= PAUSE_MS) {
      const wasMoving = gear !== 'stop';
      mode = 'paused';
      gear = 'stop';
      pendingGear = null;
      emit('paused', wasMoving);
    }
    return;
  }
  lostSince = 0;

  if (mode === 'paused') {
    mode = 'calibrating';
    calibrateSince = now;
    samples = [];
  }
  if (mode === 'calibrating') {
    samples.push(reading.pitch);
    if (now - calibrateSince < CALIBRATE_MS) return;
    neutral = samples.reduce((a, b) => a + b, 0) / samples.length;
    mode = 'ready';
    gear = 'stop';
    turn = bandFor(reading.roll, 0);
    pendingTurn = turn;
    heading = headingFor(turn, gear);
    emit('ready', heading);
    return;
  }

  // Steering: settle on a turn band before announcing it.
  const band = bandFor(reading.roll, turn);
  if (band !== pendingTurn) {
    pendingTurn = band;
    pendingTurnSince = now;
  } else if (band !== turn && now - pendingTurnSince >= TURN_SETTLE_MS) {
    turn = band;
    setHeading(now);
  }

  // Go and stop.
  const delta = reading.pitch - neutral;
  let target = gear;
  if (delta >= GO) target = 'forward';
  else if (delta <= -GO) target = 'back';
  else if (Math.abs(delta) <= STOP) target = 'stop';
  fast = Math.abs(delta) >= FAST;

  if (target !== gear) {
    if (target === 'stop') {
      gear = 'stop';
      pendingGear = null;
      // Back to the forward heading without saying so: "Stopped" is what matters here.
      heading = headingFor(turn, gear);
      emit('stop');
    } else if (pendingGear !== target) {
      pendingGear = target;
      pendingGearSince = now;
    } else if (now - pendingGearSince >= GO_HOLD_MS) {
      const from = gear;
      gear = target;
      pendingGear = null;
      nextStepAt = now;
      emit('go', gear, from);
      setHeading(now);
    }
  } else {
    pendingGear = null;
  }

  if (gear !== 'stop' && now >= nextStepAt) {
    nextStepAt = now + (fast ? FAST_STEP_MS : STEP_MS);
    emit('step', heading, fast);
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
    // Coming back to the page must not carry on moving from where it left off.
    reading = null;
    mode = 'paused';
    gear = 'stop';
  }
}

/**
 * Start steering. handlers: { ready(heading), heading(heading, gear),
 * go(gear, from), step(heading, fast), stop(), paused(wasMoving) }.
 */
export function startSteering(h) {
  if (enabled) return;
  handlers = h || {};
  enabled = true;
  reading = null;
  mode = 'paused';
  gear = 'stop';
  pendingGear = null;
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
  reading = null;
  mode = 'paused';
  gear = 'stop';
  if (wakeLock) wakeLock.release().catch(() => {});
  wakeLock = null;
}

/** Count the way the device is held now as stopped. Returns false if it isn't held as a wheel. */
export function centre() {
  if (!enabled || !reading) return false;
  neutral = reading.pitch;
  const wasMoving = gear !== 'stop';
  gear = 'stop';
  pendingGear = null;
  mode = 'ready';
  if (wasMoving) emit('stop');
  heading = headingFor(turn, gear);
  return true;
}

export function steeringActive() {
  return enabled;
}

/** True once the device has actually reported its orientation (desktops never do). */
export function steeringReporting() {
  return hasReported;
}

/** { mode, gear, heading, fast } for the visible status line. */
export function steeringStatus() {
  return { mode: enabled ? mode : 'off', gear, heading, fast };
}

export function buzz(ms = 15) {
  try { navigator.vibrate?.(ms); } catch { /* not available on iPhone */ }
}
