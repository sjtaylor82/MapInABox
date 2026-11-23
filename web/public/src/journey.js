// Journey planner: Rome2Rio journey options, the legs of each, flight times
// for a chosen date, and food along the way.
//
// As in the desktop app: Rome2Rio (through the Worker) gives broad
// multimodal options; food comes from OpenStreetMap, within 100 metres of
// the road for drive, bus and walk legs (an exact road line from OSRM, then
// Overpass), and around each station or stop (the street map tiles).
// Everything is read out through adjustable pickers, so VoiceOver users
// swipe up or down to move through options, legs, flights and food.

import { say } from './speech.js';
import { makePicker } from './picker.js';
import { formatDistance, compassName, haversineM } from './geo.js';
import { settings } from './settings.js';
import * as street from './street.js';
import { ROAD_KINDS, decodePolyline, encodePolyline } from './rome2rio.js';

const $ = (id) => document.getElementById(id);

const CORRIDOR_M = 100;   // food this close to the road counts as on the way (desktop Find Food)
const STOP_RADIUS_M = 400;
const STOP_FOOD = 12;     // nearest food places listed for each stop
const MAX_FOOD = 300;

let hooks = null;
const st = {
  date: '',
  options: [], optIdx: 0,
  option: null, legIdx: 0,
  flights: [], flightIdx: 0, flightsFor: -1,
  flightCache: new Map(),
  food: [], foodIdx: 0,
  walk: null, stepIdx: 0,
  seq: 0,
};
let stepPicker;
let datePicker; let optionPicker; let legPicker; let flightPicker; let foodPicker;

// ── Wording ───────────────────────────────────────────────────────────────

function spokenDuration(seconds) {
  const mins = Math.max(1, Math.round(seconds / 60));
  const d = Math.floor(mins / 1440);
  const h = Math.floor((mins % 1440) / 60);
  const m = mins % 60;
  const part = (n, w) => (n ? `${n} ${w}${n === 1 ? '' : 's'}` : '');
  return [part(d, 'day'), part(h, 'hour'), part(m, 'minute')].filter(Boolean).join(' ');
}

function currency() {
  const region = (navigator.language || '').split('-')[1] || '';
  const map = { AU: 'AUD', NZ: 'NZD', US: 'USD', CA: 'CAD', GB: 'GBP', IE: 'EUR', JP: 'JPY', SG: 'SGD', IN: 'INR', ZA: 'ZAR', HK: 'HKD' };
  return map[region.toUpperCase()] || (/^(de|fr|es|it|nl|pt|fi|el)/.test(navigator.language || '') ? 'EUR' : 'AUD');
}

function money(n, code) {
  try {
    return new Intl.NumberFormat(navigator.language || 'en-AU', { style: 'currency', currency: code, maximumFractionDigits: 0 }).format(n);
  } catch {
    return `${code} ${n}`;
  }
}

function priceText(p) {
  if (!p || p.low == null) return '';
  return p.high != null && p.high !== p.low
    ? `${money(p.low, p.currency)} to ${money(p.high, p.currency)}`
    : `About ${money(p.low, p.currency)}`;
}

function optionText(o, i, n) {
  const legs = o.legs.length === 1 ? '' : ` ${o.legs.length} legs.`;
  const price = priceText(o.price);
  // The position goes last: iPhone VoiceOver does not read a picker's text when it starts with a number.
  return `${o.name}. About ${spokenDuration(o.duration)}.${price ? ` ${price}.` : ''}${legs} ${i + 1} of ${n}.`;
}

const KIND_WORD = { subway: 'metro', towncar: 'car', rideshare: 'rideshare' };

function legText(l) {
  const route = l.from && l.to ? ` from ${l.from.name} to ${l.to.name}` : '';
  const dur = l.duration ? `, about ${spokenDuration(l.duration)}` : '';
  const dist = l.distance ? ` ${formatDistance(l.distance)}` : '';
  let often = '';
  if (l.perWeek >= 10) often = `, about ${Math.round(l.perWeek / 7)} a day`;
  else if (l.perWeek > 0) often = `, ${l.perWeek} a week`;
  switch (l.kind) {
    case 'plane': return `Fly${l.operator ? ` with ${l.operator}` : ''}${route}${dur}.`;
    case 'car': case 'towncar': return `Drive${dist}${route}${dur}.`;
    case 'taxi': case 'rideshare': return `${l.kind === 'taxi' ? 'Taxi' : 'Rideshare'}${dist}${route}${dur}.`;
    case 'walk': return `Walk${dist}${route}${dur}.`;
    case 'bicycle': return `Cycle${dist}${route}${dur}.`;
    default: {
      const word = KIND_WORD[l.kind] || l.kind;
      const who = l.operator ? `${l.operator} ${word}` : word[0].toUpperCase() + word.slice(1);
      const svc = l.services.length ? `, service ${l.services.join(' or ')}` : '';
      const stops = l.stops ? `, ${l.stops} stop${l.stops === 1 ? '' : 's'} on the way` : '';
      return `${who}${svc}${route}${stops}${dur}${often}.`;
    }
  }
}

function clock(hhmm) {
  const m = /^(\d{1,2}):(\d{2})/.exec(hhmm || '');
  if (!m) return hhmm || '';
  const h = Number(m[1]);
  return `${h % 12 || 12}:${m[2]} ${h < 12 ? 'am' : 'pm'}`;
}

function flightText(f, i, n) {
  const hops = f.hops.map((h) => {
    const by = h.operatedBy ? `, operated by ${h.operatedBy}` : '';
    const wait = h.layover ? `, then ${spokenDuration(h.layover * 60)} at ${h.to}` : '';
    return `${h.carrier} ${h.flight}${by}, ${h.from} ${clock(h.depart)} to ${h.to} ${clock(h.arrive)}${h.nextDay ? ' the next day' : ''}${wait}`;
  });
  const changes = f.changes ? `${f.changes} change${f.changes === 1 ? '' : 's'}` : 'Non-stop';
  return `${hops.join('; ')}. ${changes}, ${spokenDuration(f.minutes * 60)}. ${i + 1} of ${n}.`;
}

const airport = (name) => String(name || '').replace(/\s*\([A-Z]{3}\)\s*$/, '');

function dayLabel(d, i) {
  const s = d.toLocaleDateString('en-AU', { weekday: 'long', day: 'numeric', month: 'long' });
  return i === 0 ? `Today, ${s}` : i === 1 ? `Tomorrow, ${s}` : s;
}

const isoDay = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;

function travelDays() {
  const out = [];
  const now = new Date();
  for (let i = 0; i < 30; i += 1) {
    const d = new Date(now.getFullYear(), now.getMonth(), now.getDate() + i);
    out.push({ value: isoDay(d), label: dayLabel(d, i) });
  }
  return out;
}

// ── Network ───────────────────────────────────────────────────────────────

async function api(path, opts = {}, timeoutMs = 45000) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const r = await fetch(path, { ...opts, signal: ctrl.signal, headers: { Accept: 'application/json', ...(opts.headers || {}) } });
    let body = null;
    try { body = await r.json(); } catch { /* not JSON */ }
    if (!r.ok || !body) {
      if (r.status === 404 || r.status === 501 || !body) throw new Error('The journey planner needs the Map in a Box server, which is not answering.');
      throw new Error(body.error || `The server answered ${r.status}.`);
    }
    return body;
  } catch (err) {
    if (err.name === 'AbortError') throw new Error('That took too long. Try again.');
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

// ── Planning ──────────────────────────────────────────────────────────────

function setPrompt(text) {
  $('journey-prompt').textContent = text;
}

function showOnly(...ids) {
  for (const id of ['journey-options', 'journey-legs', 'journey-food', 'journey-walk']) $(id).hidden = !ids.includes(id);
}

// ── Walking route ─────────────────────────────────────────────────────────

/** 'walk' for a walking route, 'travel' for Rome2Rio travel options. */
function routeType() {
  return settings.get('routeType') === 'travel' ? 'travel' : 'walk';
}

/**
 * A typed place as a point: a place already in the street data with that
 * name, nearest first, or else a place search near where you are.
 */
async function locate(text, here) {
  const want = text.toLowerCase();
  // Places nearby by name first, once the map around here has loaded.
  await Promise.race([street.ensureRadius(here.lat, here.lon, 2000), new Promise((r) => setTimeout(r, 8000))]);
  const local = street.placesNear(here.lat, here.lon, 5000, 2000).find((p) => p.name.toLowerCase().includes(want));
  if (local) return { lat: local.lat, lon: local.lon, label: local.name };
  const params = new URLSearchParams({ q: text.slice(0, 200), limit: '10', lang: 'en', lat: here.lat.toFixed(4), lon: here.lon.toFixed(4) });
  let json;
  try {
    json = await api(`api/search?${params}`, {}, 15000);
  } catch {
    json = await (await fetch(`https://photon.komoot.io/api/?${params}`)).json();
  }
  const found = (json.features || []).map((f) => {
    const p = f.properties || {};
    const [lon, lat] = f.geometry.coordinates;
    const addr = p.housenumber && p.street ? `${p.housenumber} ${p.street}` : p.street;
    const label = [p.name, addr, p.city || p.locality || p.district].filter(Boolean).join(', ');
    return { lat, lon, label, d: haversineM(here.lat, here.lon, lat, lon) };
  }).filter((r) => r.d <= WALK_MAX_M).sort((a, b) => a.d - b.d);
  return found[0] || null;
}

const WALK_MAX_M = 50000; // further than this is not a walk: use Travel options

const TURN = {
  left: 'turn left', right: 'turn right', 'slight left': 'bear left', 'slight right': 'bear right',
  'sharp left': 'turn sharp left', 'sharp right': 'turn sharp right', straight: 'go straight on', uturn: 'turn round',
};

/** What to do at a step, without the distance: "Turn left onto Middle Street". */
function stepAction(s) {
  // Footpaths often have no name: say which street the turn is by instead.
  const by = !s.name && s.near ? `, by ${s.near}` : '';
  const onto = s.name ? ` onto ${s.name}` : by;
  const along = s.name ? ` along ${s.name}` : by;
  const turn = TURN[s.modifier] || 'continue';
  switch (s.type) {
    case 'depart': return `Start${along}, heading ${compassName(s.bearing || 0).replace('-', ' ')}`;
    case 'arrive': return 'Arrive at your destination';
    case 'roundabout': case 'rotary': return `At the roundabout, take exit ${s.exit || 1}${onto}`;
    case 'end of road': return `At the end of the road, ${turn}${onto}`;
    case 'fork': return `At the fork, ${turn}${onto}`;
    case 'new name': case 'continue': return `Continue${onto || along}`;
    default: return `${turn[0].toUpperCase()}${turn.slice(1)}${onto}`;
  }
}

function stepText(s, i, n) {
  const walkOn = s.type !== 'arrive' && s.distance ? `, then walk ${formatDistance(s.distance)}` : '';
  return `${stepAction(s)}${walkOn}. Step ${i + 1} of ${n}.`;
}

async function planWalk(from, to) {
  stopGuiding(false);
  const here = hooks.getHere();
  const seq = ++st.seq;
  showOnly();
  say(`Finding a walking route to ${to}…`);
  setPrompt('');
  let a = null;
  let b = null;
  try {
    a = from ? await locate(from, here) : { lat: here.lat, lon: here.lon, label: here.label || 'where you are' };
    if (a) b = await locate(to, a);
  } catch { /* reported below */ }
  if (seq !== st.seq) return;
  if (!a || !b) {
    const msg = `${!a ? from : to} was not found within ${formatDistance(WALK_MAX_M)}. Try a street address, or Travel options for longer trips.`;
    setPrompt(msg);
    return say(msg);
  }
  let res;
  try {
    res = await api(`api/walk?from=${a.lat.toFixed(5)},${a.lon.toFixed(5)}&to=${b.lat.toFixed(5)},${b.lon.toFixed(5)}`, {}, 30000);
  } catch (err) {
    if (seq !== st.seq) return;
    setPrompt(err.message);
    return say(err.message);
  }
  if (seq !== st.seq) return;
  for (const step of res.steps) {
    if (!step.name && step.lat != null) {
      const n = street.nearestStreet(step.lat, step.lon, 30);
      if (n) step.near = n.name;
    }
  }
  st.walk = res;
  st.walk.toLabel = b.label;
  st.stepIdx = 0;
  stepPicker.refresh();
  showOnly('journey-walk');
  const mins = Math.max(1, Math.round(res.duration / 60));
  const summary = `Walking route to ${b.label}: ${formatDistance(res.distance)}, about ${mins} minute${mins === 1 ? '' : 's'}, ${res.steps.length} steps.`;
  setPrompt(summary);
  $('journey-step-list').focus();
  say(`${summary} ${stepText(res.steps[0], 0, res.steps.length)}`);
}

function readSteps() {
  if (!st.walk) return;
  const n = st.walk.steps.length;
  say(st.walk.steps.map((s, i) => stepText(s, i, n)).join(' '));
}

function goToStep() {
  const s = st.walk && st.walk.steps[st.stepIdx];
  if (!s || s.lat == null) return;
  hooks.goTo({ lat: s.lat, lon: s.lon, name: `Step ${st.stepIdx + 1}`, kind: stepAction(s) });
}

// ── Live guidance ─────────────────────────────────────────────────────────
//
// Follows the device's location along the walking route: the next turn is
// announced as you approach it ("In 30 metres, turn left onto ...") and
// again when you reach it, going off the route is announced, and arriving
// ends it. Works while the page is open and the screen is on.

const AHEAD_M = 40;      // announce the next turn this far before it
const AT_M = 15;         // within this of a turn: you are at it
const OFF_ROUTE_M = 45;  // further than this from the route line: off route
const ARRIVE_M = 20;

const guide = { watch: null, next: 1, warned: -1, off: false, wakeLock: null, line: null };

function lineOf(walkRes) {
  if (!guide.line) guide.line = decodePolyline(walkRes.line);
  return guide.line;
}

function distToLine(lat, lon, line) {
  let best = Infinity;
  const k = Math.cos((lat * Math.PI) / 180);
  for (let i = 0; i + 1 < line.length; i++) {
    const [la1, lo1] = line[i];
    const [la2, lo2] = line[i + 1];
    const ax = (lo1 - lon) * 111000 * k;
    const ay = (la1 - lat) * 111000;
    const dx = (lo2 - lo1) * 111000 * k;
    const dy = (la2 - la1) * 111000;
    const len2 = dx * dx + dy * dy;
    const t = len2 ? Math.max(0, Math.min(1, -(ax * dx + ay * dy) / len2)) : 0;
    best = Math.min(best, Math.hypot(ax + t * dx, ay + t * dy));
  }
  return best;
}

function onGuidePosition(pos) {
  const w = st.walk;
  if (!w) return;
  const { latitude: lat, longitude: lon, accuracy } = pos.coords;
  hooks.follow({ lat, lon });
  const steps = w.steps;
  const last = steps[steps.length - 1];
  if (haversineM(lat, lon, last.lat, last.lon) <= ARRIVE_M + Math.min(accuracy, 30)) {
    say(`You have arrived at ${w.toLabel}.`);
    stopGuiding(false);
    return;
  }
  // Off the route?
  const off = distToLine(lat, lon, lineOf(w)) > OFF_ROUTE_M + Math.min(accuracy, 40);
  if (off && !guide.off) {
    guide.off = true;
    say('You are off the route. Choose Get route again for a new one from here.');
  } else if (!off && guide.off) {
    guide.off = false;
    say('Back on the route.');
  }
  // Move on past any turns already reached.
  while (guide.next < steps.length - 1 && haversineM(lat, lon, steps[guide.next].lat, steps[guide.next].lon) <= AT_M) {
    const s = steps[guide.next];
    say(`${stepAction(s)}${s.distance ? `, then walk ${formatDistance(s.distance)}` : ''}.`);
    st.stepIdx = guide.next;
    stepPicker.refresh();
    guide.next += 1;
  }
  const s = steps[guide.next];
  const d = haversineM(lat, lon, s.lat, s.lon);
  if (d <= AHEAD_M && guide.warned !== guide.next && s.type !== 'arrive') {
    guide.warned = guide.next;
    say(`In ${formatDistance(Math.round(d / 5) * 5)}, ${stepAction(s).replace(/^./, (c) => c.toLowerCase())}.`);
  }
}

async function startGuiding() {
  if (!st.walk) return;
  if (!('geolocation' in navigator)) return say('Location is not available in this browser.');
  stopGuiding(false);
  guide.next = 1;
  guide.warned = -1;
  guide.off = false;
  guide.line = null;
  try {
    if ('wakeLock' in navigator) guide.wakeLock = await navigator.wakeLock.request('screen');
  } catch { /* the screen may dim */ }
  guide.watch = navigator.geolocation.watchPosition(onGuidePosition, (err) => {
    if (err.code === err.PERMISSION_DENIED) {
      say('Location access was not allowed, so the route cannot be followed.');
      stopGuiding(false);
    }
  }, { enableHighAccuracy: true, maximumAge: 2000, timeout: 30000 });
  $('journey-guide').textContent = 'Stop walking guidance';
  say(`Following the route. ${stepText(st.walk.steps[0], 0, st.walk.steps.length)} Keep the page open and the screen on.`);
}

function stopGuiding(announce = true) {
  if (guide.watch != null) navigator.geolocation.clearWatch(guide.watch);
  guide.watch = null;
  if (guide.wakeLock) guide.wakeLock.release().catch(() => {});
  guide.wakeLock = null;
  if ($('journey-guide')) $('journey-guide').textContent = 'Start walking guidance';
  if (announce) say('Walking guidance stopped.');
}

function toggleGuiding() {
  if (guide.watch != null) stopGuiding(true);
  else startGuiding();
}

async function plan() {
  if (routeType() === 'walk') {
    const to = $('journey-to').value.trim();
    if (!to) {
      $('journey-to').focus();
      return say('Type where you are going.');
    }
    return planWalk($('journey-from').value.trim(), to);
  }
  const from = $('journey-from').value.trim();
  const to = $('journey-to').value.trim();
  if (!to) {
    $('journey-to').focus();
    return say('Type where you are going.');
  }
  const here = hooks.getHere();
  const params = new URLSearchParams({ to, currency: currency() });
  let fromName = from;
  if (from) params.set('from', from);
  else {
    fromName = here.label || 'where you are';
    params.set('from', here.label || 'where you are');
    params.set('fromPos', `${here.lat.toFixed(5)},${here.lon.toFixed(5)}`);
  }
  const seq = ++st.seq;
  showOnly();
  setPrompt(`Looking up journeys from ${fromName} to ${to}…`);
  say(`Looking up journeys from ${fromName} to ${to}.`);
  let res;
  try {
    res = await api(`api/journey?${params}`);
  } catch (err) {
    if (seq !== st.seq) return;
    setPrompt(err.message);
    return say(err.message);
  }
  if (seq !== st.seq) return;
  st.options = res.options || [];
  st.optIdx = 0;
  if (!st.options.length) {
    const msg = `Rome2Rio found no way from ${fromName} to ${to}. Try a town or city name.`;
    setPrompt(msg);
    return say(msg);
  }
  optionPicker.refresh();
  showOnly('journey-options');
  const n = st.options.length;
  setPrompt(`${n} way${n === 1 ? '' : 's'} from ${(from && res.from) || fromName} to ${res.to || to}, from Rome2Rio. These are estimates, not timetables. Swipe up or down on Option to hear them, then choose Show steps.`);
  $('journey-option-list').focus();
  say(`${n} way${n === 1 ? '' : 's'} to ${res.to || to}. ${optionText(st.options[0], 0, n)}`);
}

function chooseOption() {
  const o = st.options[st.optIdx];
  if (!o) return;
  st.option = o;
  st.legIdx = 0;
  st.food = [];
  st.flightsFor = -1;
  legPicker.refresh();
  showOnly('journey-options', 'journey-legs');
  $('journey-flights').hidden = true;
  setPrompt(`${o.name}. Swipe up or down on Leg to move through the journey. Read all speaks the whole plan.`);
  $('journey-leg-list').focus();
  say(`${o.name}. ${legItem(0).label}`);
  loadFlights(o);
}

function legItem(i) {
  const o = st.option;
  return { value: i, label: `${legText(o.legs[i])} Leg ${i + 1} of ${o.legs.length}.` };
}

function readAll() {
  const o = st.option;
  if (!o) return;
  const price = priceText(o.price);
  const legs = o.legs.map((l, i) => `${i + 1}. ${legText(l)}`).join(' ');
  say(`${o.name}. About ${spokenDuration(o.duration)}.${price ? ` ${price}.` : ''} ${legs}`);
}

// ── Flights ───────────────────────────────────────────────────────────────

/** Fetch that day's flights for each flight leg of the chosen option, then show the first. */
async function loadFlights(o) {
  const planes = o.legs.map((l, i) => [l, i]).filter(([l]) => l.kind === 'plane' && l.from && l.to);
  if (!planes.length) return;
  const seq = st.seq;
  await Promise.all(planes.map(async ([l]) => {
    const key = `${airport(l.from.name)}|${airport(l.to.name)}|${st.date}`;
    if (st.flightCache.has(key)) return;
    const params = new URLSearchParams({ from: airport(l.from.name), to: airport(l.to.name), date: st.date, currency: currency() });
    try {
      st.flightCache.set(key, (await api(`api/flights?${params}`)).flights || []);
    } catch {
      st.flightCache.set(key, null);
    }
  }));
  if (seq !== st.seq || st.option !== o) return;
  showFlights(o.legs[st.legIdx]?.kind === 'plane' ? st.legIdx : planes[0][1], false);
}

function flightsFor(l) {
  return st.flightCache.get(`${airport(l.from.name)}|${airport(l.to.name)}|${st.date}`);
}

function showFlights(legIndex, announce) {
  const l = st.option?.legs[legIndex];
  if (!l || l.kind !== 'plane' || st.flightsFor === legIndex) return;
  const list = flightsFor(l);
  if (list === undefined) return;
  st.flightsFor = legIndex;
  st.flights = list || [];
  st.flightIdx = 0;
  flightPicker.refresh();
  const day = travelDays().find((d) => d.value === st.date)?.label || st.date;
  const box = $('journey-flights');
  box.hidden = false;
  $('journey-flight-list').hidden = !st.flights.length;
  $('journey-flights-note').textContent = list === null
    ? 'Flight times could not be loaded.'
    : st.flights.length
      ? `${st.flights.length} flights from ${airport(l.from.name)} to ${airport(l.to.name)} on ${day}, non-stop first. Swipe up or down on Flight to hear them.`
      : `Rome2Rio has no flight times from ${airport(l.from.name)} to ${airport(l.to.name)} on ${day}.`;
  if (announce) say($('journey-flights-note').textContent);
}

// ── Food along the way ────────────────────────────────────────────────────

const near = (a, b) => a && b && a.lat != null && b.lat != null
  && Math.abs(a.lat - b.lat) < 0.0015 && Math.abs(a.lon - b.lon) < 0.0015 / Math.max(0.2, Math.cos(a.lat * Math.PI / 180));

/** Distance from a point to a line, and how far along the line its nearest point is (metres). */
function alongLine(line, cum, lat, lon) {
  const k = Math.cos(lat * Math.PI / 180) * 111320;
  let best = Infinity;
  let along = 0;
  for (let i = 0; i < line.length - 1; i += 1) {
    const ax = (line[i][1] - lon) * k; const ay = (line[i][0] - lat) * 111320;
    const bx = (line[i + 1][1] - lon) * k; const by = (line[i + 1][0] - lat) * 111320;
    const dx = bx - ax; const dy = by - ay;
    const len2 = dx * dx + dy * dy;
    const t = len2 ? Math.max(0, Math.min(1, -(ax * dx + ay * dy) / len2)) : 0;
    const px = ax + t * dx; const py = ay + t * dy;
    const d = Math.hypot(px, py);
    if (d < best) { best = d; along = cum[i] + t * Math.sqrt(len2); }
  }
  return { cross: best, along };
}

function cumulative(line) {
  const cum = [0];
  for (let i = 1; i < line.length; i += 1) {
    const k = Math.cos(line[i][0] * Math.PI / 180) * 111320;
    cum.push(cum[i - 1] + Math.hypot((line[i][1] - line[i - 1][1]) * k, (line[i][0] - line[i - 1][0]) * 111320));
  }
  return cum;
}

/** Douglas-Peucker simplification, tolerance in metres. */
function simplify(line, tol) {
  if (line.length < 3) return line.slice();
  const keep = new Uint8Array(line.length);
  keep[0] = 1; keep[line.length - 1] = 1;
  const stack = [[0, line.length - 1]];
  while (stack.length) {
    const [a, b] = stack.pop();
    const k = Math.cos(line[a][0] * Math.PI / 180) * 111320;
    const ax = line[a][1] * k; const ay = line[a][0] * 111320;
    const dx = line[b][1] * k - ax; const dy = line[b][0] * 111320 - ay;
    const len = Math.hypot(dx, dy) || 1;
    let far = -1; let farD = 0;
    for (let i = a + 1; i < b; i += 1) {
      const d = Math.abs((line[i][1] * k - ax) * dy - (line[i][0] * 111320 - ay) * dx) / len;
      if (d > farD) { farD = d; far = i; }
    }
    if (far > 0 && farD > tol) {
      keep[far] = 1;
      stack.push([a, far], [far, b]);
    }
  }
  return line.filter((_, i) => keep[i]);
}

function kindText(p) {
  return p.cuisine && !p.kind.includes(p.cuisine) ? `${p.kind}, ${p.cuisine}` : p.kind;
}

/** Food within CORRIDOR_M of one road leg, in order along it. */
async function roadFood(leg) {
  let line = null;
  let coarse = false;
  if (leg.from?.lat != null && leg.to?.lat != null) {
    const params = new URLSearchParams({
      from: `${leg.from.lat},${leg.from.lon}`, to: `${leg.to.lat},${leg.to.lon}`,
      profile: leg.kind === 'walk' ? 'foot' : 'car',
    });
    try {
      line = decodePolyline((await api(`api/road?${params}`)).line);
    } catch { /* use Rome2Rio's own line */ }
  }
  if (!line || line.length < 2) {
    line = leg.path ? decodePolyline(leg.path) : null;
    coarse = true;
  }
  if (!line || line.length < 2) return [];
  const limit = coarse ? 400 : CORRIDOR_M;
  let tol = 25;
  let simple = simplify(line, tol);
  while (simple.length > 2500) { tol *= 2; simple = simplify(line, tol); }
  const cum = cumulative(line);
  let found = [];
  try {
    found = (await api('api/food', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ line: encodePolyline(simple), radius: limit + tol }),
    }, 90000)).places || [];
  } catch {
    // Overpass is busy: for shorter legs, use the street map tiles along the line instead.
    if (cum[cum.length - 1] > 25000) throw new Error('road');
    found = await tileFoodAlong(line, cum);
  }
  const from = leg.from?.name || 'the start';
  const word = leg.kind === 'walk' ? 'walk' : leg.kind === 'bus' ? 'bus route' : 'drive';
  return found
    .map((p) => ({ p, ...alongLine(line, cum, p.lat, p.lon) }))
    .filter((x) => x.cross <= limit)
    .sort((a, b) => a.along - b.along)
    .map(({ p, along }) => ({
      ...p,
      text: `${p.name}, ${kindText(p)}, on the ${word}, ${formatDistance(along)} after ${from}`,
    }));
}

async function tileFoodAlong(line, cum) {
  const pts = [];
  let next = 0;
  for (let i = 0; i < line.length; i += 1) {
    if (cum[i] >= next) { pts.push(line[i]); next = cum[i] + 400; }
  }
  pts.push(line[line.length - 1]);
  await Promise.all(pts.map(([la, lo]) => street.ensureArea(la, lo)));
  const out = new Map();
  for (const [la, lo] of pts) {
    for (const p of street.placesNear(la, lo, 250, 100, new Set(['food']))) {
      out.set(`${p.name}|${p.lat.toFixed(5)}|${p.lon.toFixed(5)}`, { name: p.name, kind: p.kind, cuisine: '', lat: p.lat, lon: p.lon });
    }
  }
  return [...out.values()];
}

/** The nearest food around one station or stop, from the street map tiles. */
async function stopFood(stop) {
  const r = await street.ensureArea(stop.lat, stop.lon);
  if (!r || r.ok === false) throw new Error('stop');
  return street.placesNear(stop.lat, stop.lon, STOP_RADIUS_M, STOP_FOOD, new Set(['food'])).map((p) => ({
    name: p.name, kind: p.kind, cuisine: '', lat: p.lat, lon: p.lon,
    text: `${p.name}, ${p.kind}, ${formatDistance(p.dist)} from ${stop.name}`,
  }));
}

async function findFood() {
  const o = st.option;
  if (!o) return;
  const seq = st.seq;
  // Stations, airports and stops where the journey starts, changes and ends; not whole cities.
  const stops = [];
  for (const l of o.legs) {
    for (const s of [l.from, l.to]) {
      if (s && s.lat != null && !s.area && !stops.some((x) => near(x, s))) stops.push(s);
    }
  }
  const roads = o.legs.filter((l) => ROAD_KINDS.has(l.kind));
  const km = Math.round(roads.reduce((t, l) => t + l.distance, 0) / 1000);
  const msg = roads.length && km > 100
    ? `Looking for food near ${stops.length} stops and along ${km} kilometres of road. This can take a minute.`
    : 'Looking for food along the way.';
  setPrompt(msg);
  say(msg);
  $('journey-food-find').disabled = true;
  const failed = [];
  const stopJobs = stops.map((s) => stopFood(s).catch(() => { failed.push(s.name); return []; }));
  const roadJobs = new Map(roads.map((l) => [l, roadFood(l).catch(() => { failed.push(`the ${l.kind === 'walk' ? 'walk' : 'road'} from ${l.from?.name || 'the start'}`); return []; })]));
  // In journey order: each stop, then the road leg that leaves it.
  const order = [];
  for (const l of o.legs) {
    const si = stops.findIndex((s) => near(s, l.from));
    if (si >= 0 && !order.includes(stopJobs[si])) order.push(stopJobs[si]);
    if (roadJobs.has(l)) order.push(roadJobs.get(l));
  }
  stopJobs.forEach((j) => { if (!order.includes(j)) order.push(j); });
  const lists = await Promise.all(order);
  $('journey-food-find').disabled = false;
  if (seq !== st.seq || st.option !== o) return;
  const seen = new Set();
  const all = [];
  for (const list of lists) {
    for (const p of list) {
      const key = `${p.name.toLowerCase()}|${p.lat.toFixed(4)}|${p.lon.toFixed(4)}`;
      if (seen.has(key)) continue;
      seen.add(key);
      all.push(p);
    }
  }
  st.food = all.slice(0, MAX_FOOD);
  st.foodIdx = 0;
  foodPicker.refresh();
  showFoodLinks();
  const missed = failed.length ? ` Food could not be loaded for ${failed.join(', ')}.` : '';
  $('journey-food').hidden = !st.food.length;
  if (!st.food.length) {
    const none = `No food places found along this journey.${missed}`;
    setPrompt(none);
    return say(none);
  }
  const more = all.length > MAX_FOOD ? ` The first ${MAX_FOOD} of ${all.length} are listed.` : '';
  setPrompt(`${all.length} food places along the way, in journey order.${more}${missed} Swipe up or down on Food to hear them.`);
  $('journey-food-list').focus();
  say(`${all.length} food places along the way.${more}${missed} ${foodLabel(0)}`);
}

function foodLabel(i) {
  return `${st.food[i].text}. ${i + 1} of ${st.food.length}.`;
}

/** Apple Maps, Uber Eats and DoorDash links for the chosen food place, as in Places nearby. */
function showFoodLinks() {
  const p = st.food[st.foodIdx];
  $('journey-food-links').hidden = !p;
  if (!p) return;
  const q = encodeURIComponent(p.name);
  $('journey-food-maps').href = `https://maps.apple.com/?q=${q}&ll=${p.lat},${p.lon}`;
  $('journey-food-maps').textContent = `Open ${p.name} in Apple Maps`;
  $('journey-food-ubereats').href = `https://www.ubereats.com/search?q=${q}`;
  $('journey-food-ubereats').textContent = `Search Uber Eats for ${p.name}`;
  $('journey-food-doordash').href = `https://www.doordash.com/search/store/${q}/`;
  $('journey-food-doordash').textContent = `Search DoorDash for ${p.name}`;
}

function goToFood() {
  const p = st.food[st.foodIdx];
  if (p) hooks.goTo({ lat: p.lat, lon: p.lon, name: p.name, kind: kindText(p) });
}

// ── Set-up ────────────────────────────────────────────────────────────────

/**
 * hooks.getHere(): { lat, lon, label } — the map position, used when From is empty.
 * hooks.goTo({ lat, lon, name, kind }) — move the map to a food place.
 */
export function initJourney(h) {
  hooks = h;
  st.date = travelDays()[1].value;
  makePicker($('journey-type'), {
    shown: $('journey-type-value'),
    items: () => [{ value: 'walk', label: 'Walking route' }, { value: 'travel', label: 'Travel options: fly, train, bus or drive' }],
    get: routeType,
    set: (v) => {
      settings.set('routeType', v);
      $('journey-date-row').hidden = v === 'walk';
    },
  });
  $('journey-date-row').hidden = routeType() === 'walk';
  stepPicker = makePicker($('journey-step-list'), {
    shown: $('journey-step-value'),
    items: () => (st.walk ? st.walk.steps.map((s, i) => ({ value: i, label: stepText(s, i, st.walk.steps.length) })) : []),
    get: () => st.stepIdx,
    set: (v) => { st.stepIdx = v; },
  });
  datePicker = makePicker($('journey-date'), {
    shown: $('journey-date-value'),
    items: travelDays,
    get: () => st.date,
    set: (v) => {
      st.date = v;
      if (st.option) {
        st.flightsFor = -1;
        loadFlights(st.option);
      }
    },
  });
  optionPicker = makePicker($('journey-option-list'), {
    shown: $('journey-option-value'),
    items: () => st.options.map((o, i) => ({ value: i, label: optionText(o, i, st.options.length) })),
    get: () => st.optIdx,
    set: (v) => { st.optIdx = v; },
  });
  legPicker = makePicker($('journey-leg-list'), {
    shown: $('journey-leg-value'),
    items: () => (st.option ? st.option.legs.map((_, i) => legItem(i)) : []),
    get: () => st.legIdx,
    set: (v) => {
      st.legIdx = v;
      showFlights(v, false);
    },
  });
  flightPicker = makePicker($('journey-flight-list'), {
    shown: $('journey-flight-value'),
    items: () => st.flights.map((f, i) => ({ value: i, label: flightText(f, i, st.flights.length) })),
    get: () => st.flightIdx,
    set: (v) => { st.flightIdx = v; },
  });
  foodPicker = makePicker($('journey-food-list'), {
    shown: $('journey-food-value'),
    items: () => st.food.map((_, i) => ({ value: i, label: foodLabel(i) })),
    get: () => st.foodIdx,
    set: (v) => { st.foodIdx = v; showFoodLinks(); },
  });

  const enter = (id, fn) => $(id).addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); fn(); }
  });
  enter('journey-from', plan);
  enter('journey-to', plan);
  enter('journey-option-list', chooseOption);
  enter('journey-food-list', goToFood);
  enter('journey-step-list', goToStep);
  $('journey-plan').addEventListener('click', plan);
  $('journey-step-go').addEventListener('click', goToStep);
  $('journey-step-read').addEventListener('click', readSteps);
  $('journey-guide').addEventListener('click', toggleGuiding);
  $('journey-choose').addEventListener('click', chooseOption);
  $('journey-read').addEventListener('click', readAll);
  $('journey-food-find').addEventListener('click', findFood);
  $('journey-food-go').addEventListener('click', goToFood);
  datePicker.refresh();
}

/** The Journey toolbar button: go to the journey planner. */
export function openJourney() {
  $('journey-section').open = true;
  const h = $('journey-title');
  h.scrollIntoView({ block: 'start' });
  h.focus();
}
