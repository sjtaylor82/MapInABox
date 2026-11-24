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
};

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
    passed: new Map(), suburb: '', suburbAt: null, waitingSaid: false,
  });
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
    if (!mon.waitingSaid) say(`Waiting for a better GPS signal (now about ${Math.round(accuracy)} metres).`);
    mon.waitingSaid = true;
    return;
  }
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
      street.ensureRadius(lat, lon, 400).catch(() => {}).finally(() => { mon.loading = false; });
    }
    return;
  }
  const driving = mon.speed > DRIVE_SPEED;
  checkStreet(lat, lon, driving);
  if (mon.level !== 'streets' && mon.heading !== null && mon.street) checkCorners(lat, lon, driving);
  if (mon.level === 'suburbs') checkSuburb(lat, lon);
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
  say(first ? `On ${near.name}.` : `Now on ${near.name}.`);
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
    const parts = names.slice(0, 2).map((n) => {
      const side = street.streetSide(c, n, mon.heading);
      return side === 'both' ? `crossing ${n}` : side ? `${n} on your ${side}` : `${n} ahead`;
    });
    const text = parts.join(', and ');
    say(`${text[0].toUpperCase()}${text.slice(1)}.`);
    break;
  }
  // Forget corners passed long ago, so a second trip past one is said again.
  const old = Date.now() - 10 * 60e3;
  for (const [k, t] of mon.passed) if (t < old) mon.passed.delete(k);
}

async function checkSuburb(lat, lon) {
  if (mon.suburbAt && street.metres(mon.suburbAt.lat, mon.suburbAt.lon, lat, lon) < SUBURB_EVERY_M) return;
  mon.suburbAt = { lat, lon };
  const pc = await postcodeAt(lat, lon).catch(() => null);
  if (!pc || !pc.place || pc.place === mon.suburb) return;
  const first = !mon.suburb;
  mon.suburb = pc.place;
  if (!first) say(`Entering ${pc.place}.`);
}
