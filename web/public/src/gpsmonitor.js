// GPS monitor (Expert): follows where the device really is, walking or
// driving, and says when you turn onto a new street, when you pass a cross
// street (and on which side), and when you enter a new suburb.
//
// Positions come from watchPosition. Streets and corners come from the same
// street data as street mode (street.js), loaded around you as you go.

import * as street from './street.js';
import { say } from './speech.js';
import { postcodeAt } from './postcode.js';
import { bearingDeg } from './geo.js';

const MAX_ACCURACY = 40;      // metres: worse fixes are ignored
const DRIVE_SPEED = 6;        // metres a second (about 22 km/h): faster than this is driving
const SUBURB_EVERY_M = 150;   // check the suburb after moving this far

const mon = {
  watch: null, hooks: {}, level: 'suburbs', wakeLock: null,
  last: null, heading: null, speed: 0,
  street: '', candidate: '', candidateCount: 0,
  passed: new Map(), // corner key -> time announced
  suburb: '', suburbAt: null, waitingSaid: false, loading: false,
  lastSaid: '', accuracy: null,
};

/** Say something and keep it as the last thing said, for the status panel. */
function tell(text) {
  mon.lastSaid = text;
  say(text);
  report();
}

/** What the status panel shows: { street, suburb, lastSaid, moving, accuracy }. */
export function monitorStatus() {
  const kmh = Math.round(mon.speed * 3.6);
  const moving = !mon.last ? 'Finding your position'
    : mon.speed > DRIVE_SPEED ? `Driving, about ${kmh} kilometres an hour`
      : mon.speed > 0.5 ? 'Walking' : 'Standing still';
  return {
    street: mon.street, suburb: mon.suburb, lastSaid: mon.lastSaid, moving,
    accuracy: mon.accuracy === null ? '' : `Accurate to about ${Math.round(mon.accuracy)} metres`,
  };
}

function report() {
  if (mon.hooks.status) mon.hooks.status(monitorStatus());
}

const angleDiff = (a, b) => Math.abs(((a - b + 540) % 360) - 180);
const cornerKey = (c) => `${c.lat.toFixed(6)},${c.lon.toFixed(6)}`;

export const monitorActive = () => mon.watch !== null;

/**
 * Start the monitor. hooks: { follow(p), stopped() }. level: 'streets'
 * (new streets only), 'cross' (and cross streets) or 'suburbs' (and suburbs).
 */
export async function startMonitor(hooks = {}, level = 'suburbs') {
  if (mon.watch !== null) return;
  if (!('geolocation' in navigator) || !window.isSecureContext) {
    say('Location is not available in this browser.');
    return;
  }
  Object.assign(mon, {
    hooks, level, last: null, heading: null, speed: 0, street: '', candidate: '', candidateCount: 0,
    passed: new Map(), suburb: '', suburbAt: null, waitingSaid: false, lastSaid: '', accuracy: null,
    announced: false,
  });
  report();
  say('GPS monitor on. Finding your position…');
  mon.watch = navigator.geolocation.watchPosition(onFix, onError, { enableHighAccuracy: true, maximumAge: 2000, timeout: 30000 });
  keepAwake();
  document.addEventListener('visibilitychange', keepAwake);
}

export function stopMonitor({ quiet = false } = {}) {
  if (mon.watch === null) return;
  navigator.geolocation.clearWatch(mon.watch);
  mon.watch = null;
  document.removeEventListener('visibilitychange', keepAwake);
  if (mon.wakeLock) mon.wakeLock.release().catch(() => {});
  mon.wakeLock = null;
  if (!quiet) say('GPS monitor off.');
  if (mon.hooks.stopped) mon.hooks.stopped();
}

export function setMonitorLevel(level) {
  mon.level = level;
}

/** Keep the screen on while monitoring, so the phone keeps reporting where you are. */
async function keepAwake() {
  if (mon.watch === null || mon.wakeLock || document.visibilityState !== 'visible' || !('wakeLock' in navigator)) return;
  try {
    mon.wakeLock = await navigator.wakeLock.request('screen');
    mon.wakeLock.addEventListener('release', () => { mon.wakeLock = null; });
  } catch { /* not allowed: the monitor still works while the screen is on */ }
}

function onError(err) {
  if (err.code === err.PERMISSION_DENIED) {
    say('Location access was not allowed, so the GPS monitor has stopped.');
    stopMonitor({ quiet: true });
  }
}

/** One position from the device. Exported for testing. */
export function onFix(pos) {
  if (mon.watch === null) return;
  const { latitude: lat, longitude: lon, accuracy, speed, heading } = pos.coords;
  if (!(accuracy <= MAX_ACCURACY)) {
    mon.accuracy = accuracy;
    report();
    if (!mon.waitingSaid) tell(`Waiting for a better GPS signal (now about ${Math.round(accuracy)} metres).`);
    mon.waitingSaid = true;
    return;
  }
  mon.accuracy = accuracy;
  const now = pos.timestamp || Date.now();
  // Speed and heading: from the device when it gives them, else from the last fix.
  if (mon.last) {
    const moved = street.metres(mon.last.lat, mon.last.lon, lat, lon);
    const secs = Math.max(0.5, (now - mon.last.t) / 1000);
    const own = Number.isFinite(speed) && speed !== null ? speed : moved / secs;
    mon.speed = 0.6 * mon.speed + 0.4 * own;
    if (Number.isFinite(heading) && heading !== null && own > 1) mon.heading = heading;
    else if (moved >= 6) mon.heading = bearingDeg(mon.last.lat, mon.last.lon, lat, lon);
    if (moved < 3 && mon.heading !== null) {
      mon.last.t = now; // standing still: nothing new to say
      return;
    }
  }
  mon.last = { lat, lon, t: now };
  if (mon.hooks.follow) mon.hooks.follow({ lat, lon });

  if (!street.areaReady(lat, lon)) {
    if (!mon.loading) {
      mon.loading = true;
      street.ensureRadius(lat, lon, 400).catch(() => {}).finally(() => {
        mon.loading = false;
        // Standing still, the phone may not report again: say where you are now.
        if (mon.watch !== null && !mon.announced && mon.last && street.areaReady(mon.last.lat, mon.last.lon)) {
          announceStart(mon.last.lat, mon.last.lon);
        }
      });
    }
    return;
  }
  if (!mon.announced) return announceStart(lat, lon);
  const driving = mon.speed > DRIVE_SPEED;
  checkStreet(lat, lon, driving);
  if (mon.level !== 'streets' && mon.heading !== null && mon.street) checkCorners(lat, lon, driving);
  if (mon.level === 'suburbs') checkSuburb(lat, lon);
  report();
}

/**
 * Where you are, as soon as the monitor has a good position and the streets
 * around it: "On Shore Street, Cleveland." Nobody needs to move first.
 */
async function announceStart(lat, lon) {
  mon.announced = true;
  const near = street.nearestStreet(lat, lon, 40);
  const pc = await postcodeAt(lat, lon).catch(() => null);
  if (mon.watch === null) return;
  if (pc && pc.place) {
    mon.suburb = pc.place;
    mon.suburbAt = { lat, lon };
  }
  if (near && near.name) mon.street = near.name;
  const where = [near && near.name ? `${near.dist <= 12 ? 'On' : 'Near'} ${near.name}` : '', pc && pc.place].filter(Boolean).join(', ');
  tell(where ? `${where}.` : 'Position found. No street close by.');
}

/** A new street: said once you are clearly on it (two fixes in a row, or very close to it). */
function checkStreet(lat, lon, driving) {
  // Until you move, there's no heading: only a street you're right on counts.
  const near = mon.heading === null
    ? street.nearestStreet(lat, lon, 10, mon.street)
    : street.streetAlong(lat, lon, mon.heading, driving ? 30 : 20, mon.street);
  if (!near || !near.name || near.name === mon.street) {
    mon.candidate = '';
    mon.candidateCount = 0;
    return;
  }
  if (near.name === mon.candidate) mon.candidateCount++;
  else {
    mon.candidate = near.name;
    mon.candidateCount = 1;
  }
  if (mon.candidateCount < 2 && near.dist > 8) return;
  const first = !mon.street;
  mon.street = near.name;
  mon.candidate = '';
  mon.candidateCount = 0;
  tell(first ? `On ${near.name}.` : `Now on ${near.name}.`);
}

/** A cross street ahead: say it once, with its side, as you reach it. */
function checkCorners(lat, lon, driving) {
  const reach = driving ? 45 : 18;
  for (const c of street.intersectionsNear(lat, lon, reach)) {
    const key = cornerKey(c);
    if (mon.passed.has(key)) continue;
    const names = [...c.names].filter((n) => n && n !== mon.street);
    if (!names.length || (mon.street && !c.names.has(mon.street))) continue;
    if (c.dist > 6 && angleDiff(bearingDeg(lat, lon, c.lat, c.lon), mon.heading) > 50) continue; // behind or beside
    mon.passed.set(key, Date.now());
    const foot = footSide(lat, lon);
    const parts = names.slice(0, 2).map((n) => {
      const side = street.streetSide(c, n, mon.heading);
      if (side === 'both') return `crossing ${n}, which runs left and right`;
      if (!side) return `${n} ahead`;
      // On that side's footpath you cross it; on the other side it's across the road.
      if (foot && !driving) return foot === side ? `crossing ${n}, which joins from your ${side}` : `${n} joins from across the road on your ${side}`;
      return `${n} on your ${side}`;
    });
    const text = parts.join(', and ');
    tell(`${text[0].toUpperCase()}${text.slice(1)}.`);
    break;
  }
  // Forget corners passed long ago, so a second trip past one is said again.
  const old = Date.now() - 10 * 60e3;
  for (const [k, t] of mon.passed) if (t < old) mon.passed.delete(k);
}

/** Which footpath of the current street you're on, as you walk: 'left', 'right' or ''. */
function footSide(lat, lon) {
  if (!mon.street || mon.heading === null) return '';
  const n = street.nearestStreet(lat, lon, 25, mon.street);
  if (!n || n.name !== mon.street || n.dist < 3) return '';
  const rel = ((bearingDeg(lat, lon, n.lat, n.lon) - mon.heading + 540) % 360) - 180;
  return rel < 0 ? 'right' : 'left';
}

async function checkSuburb(lat, lon) {
  if (mon.suburbAt && street.metres(mon.suburbAt.lat, mon.suburbAt.lon, lat, lon) < SUBURB_EVERY_M) return;
  mon.suburbAt = { lat, lon };
  const pc = await postcodeAt(lat, lon).catch(() => null);
  if (!pc || !pc.place || pc.place === mon.suburb) return;
  const first = !mon.suburb;
  mon.suburb = pc.place;
  if (!first) tell(`Entering ${pc.place}.`);
  else report();
}
