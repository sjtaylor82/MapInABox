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
import { formatDistance, compassName, haversineM, bearingDeg } from './geo.js';
import { settings } from './settings.js';
import * as street from './street.js';
import { ROAD_KINDS, decodePolyline, encodePolyline } from './rome2rio.js';
import { postcodeAt } from './postcode.js';
import { showMenu } from './menu.js';
import { onSlowClick } from './busy.js';
import { addressAt } from './online.js';
import { describeLocation } from './placeinfo.js';

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
  for (const id of ['journey-options', 'journey-legs', 'journey-food', 'journey-walk', 'journey-drive', 'journey-choices', 'journey-stations']) $(id).hidden = !ids.includes(id);
}

// ── Nearest stations ──────────────────────────────────────────────────────
//
// The train, metro and light rail stations nearest to you, nearest first.
// Choose one to walk there, book an Uber to it, or open it in Apple Maps.

// Built-in station lists (data/stations/<country>.json, from OpenStreetMap):
// answers at once. Places outside those countries ask the server instead.
const STATION_KIND_NAMES = ['train station', 'metro station', 'light rail station'];
let stationManifest = null;
const stationFiles = new Map();

async function stationsBuiltIn(lat, lon) {
  if (!stationManifest) {
    stationManifest = await fetch('data/stations/manifest.json').then((r) => (r.ok ? r.json() : {})).catch(() => ({}));
  }
  const cc = Object.keys(stationManifest).find((k) => {
    const [la0, la1, lo0, lo1] = stationManifest[k];
    return lat >= la0 && lat <= la1 && lon >= lo0 && lon <= lo1;
  });
  if (!cc) return null;
  if (!stationFiles.has(cc)) stationFiles.set(cc, fetch(`data/stations/${cc}.json`).then((r) => r.json()));
  const rows = await stationFiles.get(cc);
  const byName = new Map();
  for (const [name, kind, sLat, sLon] of rows) {
    const dist = haversineM(lat, lon, sLat, sLon);
    if (dist > 15000) continue;
    const k = name.toLowerCase();
    if (!byName.has(k) || dist < byName.get(k).dist) {
      byName.set(k, { name, kind: STATION_KIND_NAMES[kind] || 'train station', lat: sLat, lon: sLon, dist: Math.round(dist) });
    }
  }
  return [...byName.values()].sort((a, b) => a.dist - b.dist).slice(0, 20);
}

async function nearestStations() {
  showOnly();
  say('Finding stations near you…');
  // From where you really are; the map position if the device can't say.
  let from;
  try {
    from = await hooks.myLocation();
  } catch {
    const here = hooks.getHere();
    from = { lat: here.lat, lon: here.lon, label: here.label || 'the map position' };
  }
  let list = [];
  try {
    list = await stationsBuiltIn(from.lat, from.lon).catch(() => null)
      || (await api(`api/stations?lat=${from.lat.toFixed(5)}&lon=${from.lon.toFixed(5)}`, {}, 50000)).stations || [];
  } catch (err) {
    setPrompt(err.message);
    return say(err.message);
  }
  st.stationFrom = from;
  st.stations = list;
  $('journey-station-actions').hidden = true;
  if (!list.length) {
    setPrompt('No stations found within 15 kilometres.');
    return say('No stations found within 15 kilometres.');
  }
  setPrompt('');
  $('journey-stations-list').replaceChildren(...list.map((s) => {
    const li = document.createElement('li');
    const b = document.createElement('button');
    b.type = 'button';
    const dir = compassName(bearingDeg(from.lat, from.lon, s.lat, s.lon));
    b.textContent = `${s.name}, ${s.kind}, ${formatDistance(s.dist)} ${dir}`;
    b.addEventListener('click', () => chooseStation(s));
    li.append(b);
    return li;
  }));
  showOnly('journey-stations');
  $('journey-stations-title').textContent = `Nearest stations: ${list.length}`;
  $('journey-stations-title').focus();
}

function chooseStation(s) {
  const from = st.stationFrom;
  st.station = s;
  $('journey-station-chosen').textContent = `${s.name}, ${formatDistance(s.dist)} away.`;
  // Uber's universal link: the Uber app if it's installed, else Uber's website.
  const q = new URLSearchParams({
    action: 'setPickup',
    'pickup[latitude]': from.lat.toFixed(6),
    'pickup[longitude]': from.lon.toFixed(6),
    'pickup[nickname]': from.label || 'My location',
    'dropoff[latitude]': s.lat.toFixed(6),
    'dropoff[longitude]': s.lon.toFixed(6),
    'dropoff[nickname]': s.name,
  });
  $('journey-station-uber').href = `https://m.uber.com/ul/?${q}`;
  $('journey-station-maps').href = `https://maps.apple.com/?saddr=${from.lat.toFixed(6)},${from.lon.toFixed(6)}&daddr=${s.lat.toFixed(6)},${s.lon.toFixed(6)}&dirflg=w`;
  $('journey-station-actions').hidden = false;
  $('journey-station-walk').focus();
}

/** A walk from where you are (or the map position) to a place, as from Jump's Walk there. */
export async function walkTo(place) {
  const here = hooks.getHere();
  const me = await nearYou(here);
  $('journey-to').value = place.label;
  $('journey-title').scrollIntoView({ block: 'start' });
  // Somewhere to be while the route is found (it then moves to the route's heading).
  $('journey-title').focus();
  return planWalk('', place.label, {
    a: { lat: me.lat, lon: me.lon, label: me === here ? (here.label || 'the map position') : 'your location' },
    b: { lat: place.lat, lon: place.lon, label: place.label },
  });
}

function walkToStation() {
  const s = st.station;
  const from = st.stationFrom;
  if (!s || !from) return;
  return planWalk(from.label || '', s.name, {
    a: { lat: from.lat, lon: from.lon, label: from.label || 'where you are' },
    b: { lat: s.lat, lon: s.lon, label: s.name },
  });
}

// ── Walking route ─────────────────────────────────────────────────────────

/**
 * A typed place as a point: a place already in the street data with that
 * name, nearest first, or else a place search near where you are.
 */
/**
 * Every place matching typed text: places nearby in the street data first,
 * then the address search. nearOnly: favour and sort by distance from here,
 * within maxM metres; otherwise the search's own order, anywhere.
 */
async function candidates(text, here, nearOnly = true, maxM = WALK_MAX_M) {
  const want = text.toLowerCase();
  const out = [];
  if (nearOnly) {
    // Places nearby by name, once the map around here has loaded.
    await Promise.race([street.ensureRadius(here.lat, here.lon, 2000), new Promise((r) => setTimeout(r, 8000))]);
    for (const p of street.placesNear(here.lat, here.lon, 5000, 2000)) {
      if (p.name.toLowerCase().includes(want)) out.push({ lat: p.lat, lon: p.lon, label: p.name });
      if (out.length >= 5) break;
    }
  }
  const near = nearOnly ? `&lat=${here.lat.toFixed(4)}&lon=${here.lon.toFixed(4)}` : '';
  let found = [];
  try {
    found = (await api(`api/geocode?q=${encodeURIComponent(text.slice(0, 200))}${near}`, {}, 15000)).results || [];
  } catch { /* the place search below */ }
  if (!found.length) {
    const params = new URLSearchParams({ q: text.slice(0, 200), limit: '10', lang: 'en' });
    if (nearOnly) {
      params.set('lat', here.lat.toFixed(4));
      params.set('lon', here.lon.toFixed(4));
    }
    try {
      const json = await (await fetch(`https://photon.komoot.io/api/?${params}`)).json();
      found = (json.features || []).map((f) => {
        const p = f.properties || {};
        const [lon, lat] = f.geometry.coordinates;
        const addr = p.housenumber && p.street ? `${p.housenumber} ${p.street}` : p.street;
        return { lat, lon, label: [p.name, addr, p.city || p.locality || p.district].filter(Boolean).join(', ') };
      });
    } catch { /* nothing found */ }
  }
  // Short labels: "Burwood, Burwood Road, Burwood" at most, not the whole address down to the country.
  out.push(...found.map((r) => ({ ...r, label: r.label.split(', ').slice(0, 3).join(', ') })));
  // The same place from both sources, or listed twice: keep one.
  const list = [];
  for (const r of out) {
    if (!list.some((x) => haversineM(x.lat, x.lon, r.lat, r.lon) < 60)) list.push(r);
  }
  if (!nearOnly) return list;
  return list.map((r) => ({ ...r, d: haversineM(here.lat, here.lon, r.lat, r.lon) }))
    .filter((r) => r.d <= maxM)
    .sort((x, y) => x.d - y.d);
}

async function locate(text, here, nearOnly = true, maxM = WALK_MAX_M) {
  return (await candidates(text, here, nearOnly, maxM))[0] || null;
}

/**
 * The destination: straight away when only one place matches; otherwise a
 * list to choose from ("Vision Australia, Coorparoo, 6.2 kilometres"),
 * resolving with the one chosen. Resolves null if nothing matches.
 */
/**
 * A place typed in From or To. One match: that one. Several: they're listed
 * under a heading that gets focus ("Choose start" or "Choose destination"),
 * nearest first, and the chosen one is
 * returned. role: 'start' or 'destination'.
 */
async function choosePlace(text, near, maxM, role = 'destination') {
  const list = (await candidates(text, near, true, maxM)).slice(0, 10);
  if (list.length <= 1) return list[0] || null;
  const question = role === 'start' ? 'Choose start' : 'Choose destination';
  const box = $('journey-choices');
  const ul = $('journey-choice-list');
  return new Promise((resolve) => {
    ul.replaceChildren(...list.map((r) => {
      const li = document.createElement('li');
      const b = document.createElement('button');
      b.type = 'button';
      b.textContent = `${r.label}, ${formatDistance(r.d)}`;
      b.addEventListener('click', () => {
        box.hidden = true;
        resolve(r);
      });
      li.append(b);
      return li;
    }));
    box.hidden = false;
    setPrompt('');
    $('journey-choices-title').textContent = question;
    $('journey-choices-title').focus();
  });
}

const chooseDestination = (text, from, maxM) => choosePlace(text, from, maxM, 'destination');

/** Where you are, for ranking matches nearest you: the device if it can say within 4 seconds, else the map. */
async function nearYou(here) {
  const p = hooks.nearMe ? await hooks.nearMe() : null;
  return p ? { lat: p.lat, lon: p.lon, label: 'your location' } : here;
}

/**
 * Drive: find From and To, then hand the trip to a navigation app. On a
 * phone, these links open Apple Maps or Google Maps straight into driving
 * directions when the app is installed, or the website when it is not.
 */
async function planDrive() {
  const from = $('journey-from').value.trim();
  const to = $('journey-to').value.trim();
  if (!to) {
    $('journey-to').focus();
    return say('Type where you are going.');
  }
  const here = hooks.getHere();
  const seq = ++st.seq;
  showOnly();
  $('journey-drive').hidden = true;
  say(`Finding ${to}…`);
  let a = null;
  let b = null;
  try {
    if (st.fromHere && from === st.fromHere.label) a = st.fromHere;
    else a = from ? await choosePlace(from, await nearYou(here), Infinity, 'start') : { lat: here.lat, lon: here.lon, label: here.label || 'where you are' };
    if (a) b = await chooseDestination(to, a, Infinity);
  } catch { /* reported below */ }
  if (seq !== st.seq) return;
  if (!a || !b) {
    const msg = `${!a ? from : to} was not found. Try a street address or the name of a place.`;
    setPrompt(msg);
    return say(msg);
  }
  showDrive(a, b);
}

/** Drive from where you are to a place already found (Jump's Drive there). */
export async function driveTo(place) {
  const here = hooks.getHere();
  const me = await nearYou(here);
  $('journey-to').value = place.label;
  $('journey-title').scrollIntoView({ block: 'start' });
  st.seq++;
  stopGuiding(false);
  showOnly();
  showDrive(
    { lat: me.lat, lon: me.lon, label: me === here ? (here.label || 'the map position') : 'your location' },
    { lat: place.lat, lon: place.lon, label: place.label },
  );
}

/** The Apple Maps and Google Maps driving links from a to b. */
function showDrive(a, b) {
  const s = `${a.lat.toFixed(6)},${a.lon.toFixed(6)}`;
  const d = `${b.lat.toFixed(6)},${b.lon.toFixed(6)}`;
  $('journey-drive-apple').href = `https://maps.apple.com/?saddr=${s}&daddr=${d}&dirflg=d`;
  $('journey-drive-google').href = `https://www.google.com/maps/dir/?api=1&origin=${s}&destination=${d}&travelmode=driving`;
  $('journey-drive').hidden = false;
  st.drive = { from: { lat: a.lat, lon: a.lon, name: a.label }, to: { lat: b.lat, lon: b.lon, name: b.label } };
  const km = formatDistance(haversineM(a.lat, a.lon, b.lat, b.lon));
  const msg = `Driving to ${b.label}, ${km} in a straight line. Open it in Apple Maps or Google Maps for directions.`;
  setPrompt(msg);
  say(msg);
  $('journey-drive-apple').focus();
}

const WALK_MAX_M = 50000; // further than this is not a walk: use Travel options

const TURN = {
  left: 'turn left', right: 'turn right', 'slight left': 'bear left', 'slight right': 'bear right',
  'sharp left': 'turn sharp left', 'sharp right': 'turn sharp right', straight: 'go straight on', uturn: 'turn round',
};

/** What to do at a step, without the distance: "Turn left onto Middle Street". */
/** The street a step walks along: its own name, or the street its footpath runs beside. */
function streetOf(s) {
  return s.name || (s.nearDist <= 15 && !s.crossing ? s.near : '');
}

/**
 * "Arrive at Factory 51, on your right", or "…, on the other side of the
 * road, on your right" when it's across the street from your footpath.
 */
/** The destination's street address when known: "51 Holdsworth Street", or ''. */
function destinationAddress(b) {
  const parts = String(b.label || '').split(', ');
  const numbered = parts.slice(1).find((x) => /^\d/.test(x));
  if (numbered) return numbered;
  const a = street.areaReady(b.lat, b.lon) ? street.nearestAddress(b.lat, b.lon, 40) : null;
  return a && a.street ? `${a.num} ${a.street}` : '';
}

function arriveText(s) {
  const base = st.walk && st.walk.toLabel ? st.walk.toLabel.split(', ')[0] : 'your destination';
  const addr = st.walk && st.walk.toAddress && st.walk.toAddress !== base ? st.walk.toAddress : '';
  const name = addr ? `${base}, ${addr}` : base;
  const side = s.modifier === 'left' || s.modifier === 'right' ? s.modifier : s.modifier === 'straight' ? 'ahead' : '';
  if (!side) return `Arrive at ${name}`;
  if (side === 'ahead') return `Arrive at ${name}, straight ahead`;
  const across = s.foot && s.foot !== side ? ', on the other side of the road' : '';
  return `Arrive at ${name}${across}, on your ${side}`;
}

function stepAction(s) {
  // Footpaths often have no name: a footpath beside a street is that street;
  // otherwise say which street the turn is by.
  const name = streetOf(s);
  const by = !name && s.near ? `, by ${s.near}` : '';
  const onto = name ? ` onto ${name}` : by;
  const along = name ? ` along ${name}` : by;
  const turn = TURN[s.modifier] || 'continue';
  // Already on this street: keep going along it, whatever the footpath does at the corner.
  if (name && s.prevStreet === name && s.type !== 'arrive' && s.type !== 'depart') {
    return `Continue along ${name}`;
  }
  if (s.crossing) {
    const way = s.modifier && TURN[s.modifier] && s.modifier !== 'straight' ? `${TURN[s.modifier][0].toUpperCase()}${TURN[s.modifier].slice(1)} and cross` : 'Cross';
    return `${way} ${s.near}${s.control ? ` ${s.control}` : ''}`;
  }
  switch (s.type) {
    case 'depart': return `Start${along}, heading ${compassName(s.bearing || 0).replace('-', ' ')}`;
    case 'arrive': return arriveText(s);
    case 'roundabout': case 'rotary': return `At the roundabout, take exit ${s.exit || 1}${onto}`;
    case 'end of road': return `At the end of the road, ${turn}${onto}`;
    case 'fork': return `At the fork, ${turn}${onto}`;
    case 'new name': case 'continue': return `Continue${onto || along}`;
    default: return `${turn[0].toUpperCase()}${turn.slice(1)}${onto}`;
  }
}

function listJoin(items) {
  return items.length <= 1 ? items.join('') : `${items.slice(0, -1).join(', ')} and ${items[items.length - 1]}`;
}

/** "at the corner of Railway Parade": the other streets meeting where a step happens. */
function cornerText(s) {
  if (!s.corner || !s.corner.length) return '';
  // Going straight on through a corner: which streets you cross, and which are to one side.
  const name = streetOf(s);
  if (name && s.prevStreet === name && s.cornerSides) {
    return `, ${s.cornerSides.slice(0, 3).map(crossingWords).join(', ')}`;
  }
  return `, at ${s.corner.length > 1 ? 'the corner of ' : ''}${listJoin(s.corner)}`;
}

/**
 * One cross street: "cross Wilga Street, which runs left and right", or
 * "Burwood Road on your right" when it only leaves one side.
 */
function crossingWords(c) {
  const at = c.control ? `, ${c.control}` : '';
  if (c.side === 'both') return `cross ${c.name}, which runs left and right${at}`;
  if (!c.side) return `cross ${c.name}${at}`;
  // On the footpath on that side, you cross the street; on the other side, you pass it across the road.
  if (c.foot && c.foot === c.side) return `cross ${c.name}, which joins from your ${c.side}${at}`;
  if (c.foot) return `${c.name} joins from across the road on your ${c.side}`;
  return `${c.name} on your ${c.side}`;
}

/** The cross streets on the way to the next step, in order. */
function crossingText(s) {
  return s.crossings && s.crossings.length ? `: ${s.crossings.map(crossingWords).join(', then ')}` : '';
}

function stepText(s) {
  const walkOn = s.type !== 'arrive' && s.distance ? `, then walk ${formatDistance(s.distance)}` : '';
  return `${stepAction(s)}${cornerText(s)}${walkOn}${crossingText(s)}.`;
}

/** The point a given distance along a line. */
function pointAlong(line, cum, along) {
  let i = 0;
  while (i < cum.length - 2 && cum[i + 1] < along) i += 1;
  const seg = cum[i + 1] - cum[i] || 1;
  const t = Math.max(0, Math.min(1, (along - cum[i]) / seg));
  return [line[i][0] + t * (line[i + 1][0] - line[i][0]), line[i][1] + t * (line[i + 1][1] - line[i][1])];
}

/**
 * Corners and cross streets from the street map: the streets meeting where
 * each step happens, and the cross streets passed on the way to the next.
 */
/**
 * The street map along the whole walk, so its unnamed footpaths can be named
 * by the streets beside them and cross streets found. A part of the map seen
 * for the first time can take 10 to 20 seconds; wait up to 30. Returns
 * whether it all arrived.
 */
async function loadStreetsAlong(res) {
  const line = decodePolyline(res.line);
  const cum = cumulative(line);
  const loads = [];
  for (let a = 0; a <= cum[cum.length - 1]; a += 400) {
    const [la, lo] = pointAlong(line, cum, a);
    loads.push(street.ensureArea(la, lo).then(() => true, () => false));
  }
  const [endLat, endLon] = line[line.length - 1];
  loads.push(street.ensureArea(endLat, endLon).then(() => true, () => false));
  const all = await Promise.race([Promise.all(loads), new Promise((r) => setTimeout(() => r(null), 30000))]);
  return !!all && all.every(Boolean);
}

/**
 * Which footpath you're on along a street: 'left' or 'right' of its centre
 * line as you walk, or '' when the route follows the centre line (no
 * footpaths mapped) or the street can't be found.
 */
function footSide(lat, lon, heading, streetName) {
  if (!streetName) return '';
  const n = street.nearestStreet(lat, lon, 25, streetName);
  if (!n || n.name !== streetName || n.dist < 3) return '';
  const toCentre = bearingDeg(lat, lon, n.lat, n.lon);
  const rel = ((toCentre - heading + 540) % 360) - 180; // negative: centre to the left
  return rel < 0 ? 'right' : 'left';
}

/**
 * Traffic lights and pedestrian crossings where the walk crosses a street:
 * "cross Old Cleveland Road at the traffic lights". Optional: if the
 * lookup is slow or fails, the steps simply don't mention them.
 */
async function addTrafficControls(res) {
  let controls = [];
  try {
    controls = (await api('api/crossings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ line: res.line }),
    }, 12000)).controls || [];
  } catch { return; }
  if (!controls.length) return;
  const nearest = (lat, lon) => {
    let best = null;
    for (const c of controls) {
      const d = haversineM(lat, lon, c.lat, c.lon);
      if (d <= 25 && (!best || d < best.d)) best = { d, kind: c.kind };
    }
    return best ? (best.kind === 'lights' ? 'at the traffic lights' : 'at a pedestrian crossing') : '';
  };
  for (const s of res.steps) {
    if (s.crossing && s.lat != null) s.control = nearest(s.lat, s.lon);
    for (const c of s.crossings || []) if (c.side === 'both' || !c.foot || c.foot === c.side) c.control = nearest(c.lat, c.lon);
    for (const c of s.cornerSides || []) if (c.lat != null && (c.side === 'both' || !c.foot || c.foot === c.side)) c.control = nearest(c.lat, c.lon);
  }
}

/**
 * Places you pass close by on your side (within 15 metres of the route: a
 * landmark to confirm you're on track), and bus stops (within 20 metres).
 * Stored as res.passes: [{ name, along, side, bus }], in walking order.
 */
const LANDMARK_CATEGORIES = new Set(['food', 'shopping', 'community', 'health', 'arts', 'accommodation', 'parks', 'trains']);
function addLandmarks(res) {
  const line = decodePolyline(res.line);
  const cum = cumulative(line);
  const seen = new Set();
  const passes = [];
  for (let a = 0; a <= cum[cum.length - 1]; a += 10) {
    const [la, lo] = pointAlong(line, cum, a);
    for (const p of street.placesNear(la, lo, 20, 40)) {
      const bus = /bus stop/i.test(p.kind || '');
      if (!bus && !LANDMARK_CATEGORIES.has(p.category)) continue;
      const key = bus ? `bus|${p.lat.toFixed(4)},${p.lon.toFixed(4)}` : p.name.toLowerCase();
      if (seen.has(key)) continue;
      const at = alongLine(line, cum, p.lat, p.lon);
      if (at.cross > (bus ? 20 : 15)) continue;
      // Not the destination itself, nor anything across the road from your footpath.
      const dest = res.toName ? res.toName.toLowerCase() : '';
      if ((dest && p.name.toLowerCase() === dest) || haversineM(p.lat, p.lon, line[line.length - 1][0], line[line.length - 1][1]) < 40) continue;
      const [fl, fo] = pointAlong(line, cum, at.along);
      const [h1, h2] = [pointAlong(line, cum, Math.max(0, at.along - 6)), pointAlong(line, cum, at.along + 6)];
      const heading = bearingDeg(h1[0], h1[1], h2[0], h2[1]);
      const road = street.streetAlong(fl, fo, heading, 25); // the named street you walk beside, not the footpath
      const foot = road ? footSide(fl, fo, heading, road.name) : '';
      if (foot && foot !== at.side) continue;
      seen.add(key);
      passes.push({ name: p.name, along: at.along, side: at.side, bus });
    }
  }
  res.passes = passes.sort((x, y) => x.along - y.along);
}

/** ", passing Burwood Hotel on your right and a bus stop on your left": at most two, between from and to metres. */
function passingText(from, to) {
  const here = ((st.walk && st.walk.passes) || []).filter((p) => p.along > from + 5 && p.along < to - 5);
  if (!here.length) return '';
  const marks = here.filter((p) => !p.bus);
  const bus = here.find((p) => p.bus);
  const pick = [...marks.slice(0, bus ? 1 : 2), ...(bus ? [bus] : [])].sort((x, y) => x.along - y.along);
  return `, passing ${pick.map((p) => `${p.bus ? 'a bus stop' : p.name} on your ${p.side}`).join(' and ')}`;
}

async function addCrossStreets(res) {
  const line = decodePolyline(res.line);
  const cum = cumulative(line);
  await loadStreetsAlong(res); // already loaded by planWalk: returns at once
  const at = res.steps.map((s) => alongLine(line, cum, s.lat, s.lon).along);
  res.steps.forEach((s, i) => { s.along = at[i]; s.nextAlong = at[i + 1]; });
  // Unnamed footpaths: name them by the street they run beside a little way
  // past the turn (at the corner itself the nearest street is often the one
  // being crossed). A short step straight across a street is a crossing.
  res.steps.forEach((s, i) => {
    if (s.name || s.type === 'arrive' || i + 1 >= res.steps.length) return;
    s.crossing = false;
    const len = Math.max(0, at[i + 1] - at[i]);
    const probeAt = at[i] + Math.min(25, len / 2);
    const [pl, po] = pointAlong(line, cum, probeAt);
    const [q1, q2] = [pointAlong(line, cum, probeAt - 5), pointAlong(line, cum, probeAt + 5)];
    const walkBearing = bearingDeg(q1[0], q1[1], q2[0], q2[1]);
    const n = street.nearestStreet(pl, po, 20);
    if (!n) return;
    const diff = Math.abs(((walkBearing - n.bearing + 540) % 360) - 180);
    const across = Math.min(diff, 180 - diff); // 0: along the street, 90: straight across
    s.near = n.name;
    s.nearDist = n.dist;
    if (across >= 55 && len <= 40 && s.type !== 'depart') s.crossing = true;
    else if (across > 35) s.nearDist = 99; // neither along nor across: just "by" it
  });
  res.steps.forEach((s, i) => {
    if (s.lat == null) return;
    const mine = streetOf(s);
    const prevStreet = i > 0 ? streetOf(res.steps[i - 1]) : '';
    s.prevStreet = prevStreet;
    // Every street meeting within 25 metres of the turn: two corners can be that close.
    s.corner = [];
    s.cornerSides = [];
    let footHere = '';
    if (s.bearing != null && at[i] > 15) {
      const [fl, fo] = pointAlong(line, cum, at[i] - 15);
      footHere = footSide(fl, fo, s.bearing, mine || prevStreet);
    }
    for (const ix of street.intersectionsNear(s.lat, s.lon, 25)) {
      for (const n of ix.names) {
        if (n === mine || n === s.near || n === prevStreet || s.corner.includes(n)) continue;
        s.corner.push(n);
        s.cornerSides.push({ name: n, side: s.bearing != null ? street.streetSide(ix, n, s.bearing) : '', foot: footHere, lat: ix.lat, lon: ix.lon });
      }
    }
    if (s.corner.length > 3) s.corner = s.corner.slice(0, 3);
    if (s.type === 'arrive' && at[i] > 15) {
      const [fl, fo] = pointAlong(line, cum, at[i] - 12);
      const [g1, g2] = [pointAlong(line, cum, at[i] - 18), pointAlong(line, cum, at[i] - 6)];
      s.foot = footSide(fl, fo, bearingDeg(g1[0], g1[1], g2[0], g2[1]), prevStreet || mine);
    }
    // Cross streets between this step and the next, in order, not the corners at either end.
    s.crossings = [];
    if (s.type === 'arrive' || i + 1 >= res.steps.length) return;
    const seen = new Set([mine, s.near, prevStreet, ...s.corner]);
    const nextIx = street.nearestIntersection(res.steps[i + 1].lat, res.steps[i + 1].lon, 25);
    for (const n of nextIx ? nextIx.names : []) seen.add(n);
    for (let a = at[i] + 20; a < at[i + 1] - 20; a += 8) {
      const [la, lo] = pointAlong(line, cum, a);
      const c = street.nearestIntersection(la, lo, 12);
      if (!c) continue;
      const [b1, b2] = [pointAlong(line, cum, a - 6), pointAlong(line, cum, a + 6)];
      const heading = bearingDeg(b1[0], b1[1], b2[0], b2[1]);
      for (const name of c.names) {
        if (seen.has(name)) continue;
        seen.add(name);
        const [fl, fo] = pointAlong(line, cum, Math.max(0, a - 15));
        const foot = footSide(fl, fo, heading, mine || prevStreet);
        s.crossings.push({ name, lat: c.lat, lon: c.lon, side: street.streetSide(c, name, heading), foot, along: a });
      }
    }
  });
}

/** The steps as a plain numbered list, read by swiping like any other text. */
/**
 * The steps as a plain list. A step's cross streets are lines of their own,
 * each with the walk to the next: "Church Street on your left, then walk
 * 110 metres."
 */
function showSteps() {
  const ol = $('journey-steps');
  const items = [];
  const metresTo = (from, to) => formatDistance(Math.max(5, Math.round((to - from) / 5) * 5));
  st.walk.steps.forEach((s, i) => {
    const li = document.createElement('li');
    li.dataset.step = String(i);
    const crossings = s.crossings || [];
    if (!crossings.length || s.along == null) {
      const passing = s.along != null && s.nextAlong != null ? passingText(s.along, s.nextAlong) : '';
      li.textContent = passing ? stepText(s).replace(/\.$/, `${passing}.`) : stepText(s);
      items.push(li);
      return;
    }
    li.textContent = `${stepAction(s)}${cornerText(s)}, then walk ${metresTo(s.along, crossings[0].along)}${passingText(s.along, crossings[0].along)}.`;
    items.push(li);
    crossings.forEach((c, k) => {
      const next = k + 1 < crossings.length ? crossings[k + 1].along : s.nextAlong;
      const words = crossingWords(c);
      const cli = document.createElement('li');
      cli.textContent = `${words[0].toUpperCase()}${words.slice(1)}${next != null ? `, then walk ${metresTo(c.along, next)}${passingText(c.along, next)}` : ''}.`;
      items.push(cli);
    });
  });
  ol.replaceChildren(...items);
}

/** Mark the step guidance has reached. */
function markStep(i) {
  for (const li of $('journey-steps').children) {
    if (li.dataset.step === String(i)) li.setAttribute('aria-current', 'step');
    else li.removeAttribute('aria-current');
  }
}

async function planWalk(from, to, known = null) {
  stopGuiding(false);
  const here = hooks.getHere();
  const seq = ++st.seq;
  showOnly();
  say(`Finding a walking route to ${to}…`);
  setPrompt('');
  let a = known ? known.a : null;
  let b = known ? known.b : null;
  if (!known) try {
    // From can be anywhere, so a walk can be looked up for a place you are not.
    if (st.fromHere && from === st.fromHere.label) a = st.fromHere; // Use my location: the exact spot
    else a = from ? await choosePlace(from, await nearYou(here), Infinity, 'start') : { lat: here.lat, lon: here.lon, label: here.label || 'where you are' };
    if (a) b = await chooseDestination(to, a, WALK_MAX_M);
  } catch { /* reported below */ }
  if (seq !== st.seq) return;
  if (!a || !b) {
    const msg = !a
      ? `${from} was not found. Try a street address or the name of a place.`
      : `${to} was not found within ${formatDistance(WALK_MAX_M)} of ${from || 'the map position'}. Type a From near it, jump the map there first, or choose Travel options.`;
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
  // Platform numbers ("2;3") are not names; fold tiny steps (inside a
  // station, a few metres apart) into the next one.
  const steps = [];
  for (const step of res.steps) {
    if (/^[\d;,\s]*$/.test(step.name || '')) step.name = '';
    const prev = steps[steps.length - 1];
    if (prev && prev.type !== 'depart' && prev.distance < 10 && step.type !== 'arrive') {
      steps[steps.length - 1] = { ...step, distance: step.distance + prev.distance };
      continue;
    }
    steps.push(step);
  }
  res.steps = steps;
  // The streets along the walk first: unnamed footpaths are named by them.
  setPrompt('Loading the streets along the way…');
  const streetsOk = await loadStreetsAlong(res);
  if (seq !== st.seq) return;
  if (!streetsOk) say('Some street names could not be loaded, so a few steps may only say turn left or right.');
  for (const step of res.steps) {
    if (!step.name && step.lat != null) {
      const n = street.nearestStreet(step.lat, step.lon, 30);
      if (n) {
        step.near = n.name;
        step.nearDist = n.dist;
        // An unnamed path across the nearest street is a crossing of it, not a
        // turn onto it: compare the way you walk with the way the street runs.
        if (step.bearing != null && n.dist <= 15) {
          const diff = Math.abs(((step.bearing - n.bearing + 540) % 360) - 180);
          const across = Math.min(diff, 180 - diff); // 0: along the street, 90: straight across
          if (across >= 50 && step.type !== 'depart' && step.type !== 'arrive') step.crossing = true;
        }
      }
    }
  }
  setPrompt('Finding the cross streets…');
  await addCrossStreets(res);
  await addTrafficControls(res);
  res.toName = String(b.label || '').split(', ')[0];
  addLandmarks(res);
  if (seq !== st.seq) return;
  st.walk = res;
  st.walk.toLabel = b.label;
  st.walk.toAddress = destinationAddress(b);
  st.stepIdx = 0;
  showSteps();
  showOnly('journey-walk');
  const mins = Math.max(1, Math.round(res.duration / 60));
  // A heading to jump to ("Walking route to Ethel Street, Burwood"), then the summary.
  const summary = `${formatDistance(res.distance)}, about ${mins} minute${mins === 1 ? '' : 's'}, ${res.steps.length} steps.`;
  setPrompt('');
  $('journey-walk-title').textContent = `Walking route to ${b.label}`;
  $('journey-walk-summary').textContent = summary;
  $('journey-walk-title').focus();
  say(summary);
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

const guide = { watch: null, next: 1, warned: -1, off: false, wakeLock: null, line: null, last: null, heading: null, called: new Set() };

const CALLOUT_M = 25;    // places this close, beside or just ahead, are called out
const BUSY = 3;          // more places than this on a stretch: list them, don't call each out

/**
 * The places to call out along the whole walk, worked out when guidance
 * starts: each with how far along the route it is, so a stretch between two
 * turns can be counted.
 */
async function prepareStretches() {
  const cat = calloutCategory();
  guide.places = [];
  guide.stepAlong = [];
  if (cat === 'none') return;
  const line = lineOf(st.walk);
  const cum = cumulative(line);
  guide.stepAlong = st.walk.steps.map((s) => alongLine(line, cum, s.lat, s.lon).along);
  const found = await tileFoodAlong(line, cum, cat === 'all' ? null : new Set([cat]));
  for (const p of found) {
    const at = alongLine(line, cum, p.lat, p.lon);
    if (at.cross <= CALLOUT_M) guide.places.push({ ...p, along: at.along });
  }
  guide.places.sort((a, b) => a.along - b.along);
}

/**
 * Starting the stretch after step i: if it is busy, say how many places it
 * has and put them in the Food along the way picker, instead of calling out
 * each one. Returns the words to add to the turn instruction.
 */
function enterStretch(i) {
  if (!guide.places || !guide.stepAlong.length) return '';
  const from = guide.stepAlong[i] ?? 0;
  const to = guide.stepAlong[i + 1] ?? Infinity;
  const here = guide.places.filter((p) => p.along >= from - 5 && p.along < to);
  if (here.length <= BUSY) return '';
  for (const p of here) guide.called.add(`${p.name}|${p.lat.toFixed(3)}|${p.lon.toFixed(3)}`);
  st.food = here.map((p) => ({ ...p, base: `${p.name}, ${p.kind}`, distance: Math.max(0, p.along - from), text: `${p.name}, ${p.kind}` }));
  st.foodIdx = 0;
  foodPicker.refresh();
  addSuburbs(st.food).then(() => foodPicker.refresh()); // suburbs follow a moment later
  showFoodLinks();
  $('journey-food').hidden = false;
  const cat = calloutCategory();
  const what = cat === 'all' ? 'places' : cat === 'food' ? 'food places' : 'places';
  return ` ${here.length} ${what} on this stretch: they are in Places along the way.`;
}

/** The category of places called out on a walk (Settings): 'none', 'all' or a place category. */
export function calloutCategory() {
  return settings.get('walkCallouts') || 'food';
}

/** Call out one place in the chosen category beside or just ahead: "Cafe Julz, cafe, on your left." */
function calloutPlace(lat, lon) {
  const cat = calloutCategory();
  if (cat === 'none' || guide.heading == null) return false;
  const cats = cat === 'all' ? null : new Set([cat]);
  for (const p of street.placesNear(lat, lon, CALLOUT_M, 20, cats)) {
    // The same name within about 100 metres is the same place listed twice.
    const key = `${p.name}|${p.lat.toFixed(3)}|${p.lon.toFixed(3)}`;
    if (guide.called.has(key)) continue;
    const rel = ((p.bearing - guide.heading + 540) % 360) - 180;
    if (Math.abs(rel) > 110) continue; // behind you: passed already
    guide.called.add(key);
    const side = Math.abs(rel) < 20 ? 'ahead' : rel < 0 ? 'on your left' : 'on your right';
    say(`${p.name}, ${p.kind}, ${side}.`);
    return true;
  }
  return false;
}

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
  if (!w || guide.watch == null) return; // guidance has stopped
  const { latitude: lat, longitude: lon, accuracy } = pos.coords;
  hooks.follow({ lat, lon });
  street.ensureArea(lat, lon); // places along the way need the map data here
  // Which way you are walking, from where you were a few metres ago.
  if (!guide.last || haversineM(guide.last.lat, guide.last.lon, lat, lon) >= 5) {
    if (guide.last) guide.heading = bearingDeg(guide.last.lat, guide.last.lon, lat, lon);
    guide.last = { lat, lon };
  }
  let spoke = false;
  const steps = w.steps;
  const last = steps[steps.length - 1];
  if (haversineM(lat, lon, last.lat, last.lon) <= ARRIVE_M + Math.min(accuracy, 30)) {
    say(`${arriveText(last).replace(/^Arrive at/, 'You have arrived at')}.`);
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
    say(`${stepAction(s)}${s.distance ? `, then walk ${formatDistance(s.distance)}` : ''}.${enterStretch(guide.next)}`);
    spoke = true;
    st.stepIdx = guide.next;
    markStep(guide.next);
    guide.next += 1;
  }
  const s = steps[guide.next];
  const d = haversineM(lat, lon, s.lat, s.lon);
  if (d <= AHEAD_M && guide.warned !== guide.next && s.type !== 'arrive') {
    guide.warned = guide.next;
    say(`In ${formatDistance(Math.round(d / 5) * 5)}, ${stepAction(s).replace(/^./, (c) => c.toLowerCase())}.`);
    spoke = true;
  }
  // Cross streets on this stretch, as you reach them.
  if (!spoke) {
    const cur = steps[guide.next - 1];
    for (const c of (cur && cur.crossings) || []) {
      if (guide.crossed.has(c.name) || haversineM(lat, lon, c.lat, c.lon) > 15) continue;
      guide.crossed.add(c.name);
      const words = crossingWords(c);
      say(`${words[0].toUpperCase()}${words.slice(1)}.`.replace(/^Cross /, 'Crossing '));
      spoke = true;
      break;
    }
  }
  // Places along the way, when nothing else is being spoken.
  if (!spoke && !guide.off) calloutPlace(lat, lon);
}

async function startGuiding() {
  if (!st.walk) return;
  if (!('geolocation' in navigator)) return say('Location is not available in this browser.');
  stopGuiding(false);
  guide.next = 1;
  guide.warned = -1;
  guide.off = false;
  guide.line = null;
  guide.last = null;
  guide.heading = null;
  guide.called = new Set();
  guide.crossed = new Set();
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
  await prepareStretches();
  say(`Following the route. ${stepText(st.walk.steps[0])}${enterStretch(0)} Keep the page open and the screen on.`);
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

function walkClick() {
  const to = $('journey-to').value.trim();
  if (!to) {
    $('journey-to').focus();
    return say('Type where you are going.');
  }
  return planWalk($('journey-from').value.trim(), to);
}

async function plan() {
  const from = $('journey-from').value.trim();
  const to = $('journey-to').value.trim();
  if (!to) {
    $('journey-to').focus();
    return say('Type where you are going.');
  }
  const here = hooks.getHere();
  const params = new URLSearchParams({ to, currency: currency() });
  let fromName = from;
  if (st.fromHere && from === st.fromHere.label) {
    params.set('from', from);
    params.set('fromPos', `${st.fromHere.lat.toFixed(5)},${st.fromHere.lon.toFixed(5)}`);
  } else if (from) params.set('from', from);
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
  let side = '';
  for (let i = 0; i < line.length - 1; i += 1) {
    const ax = (line[i][1] - lon) * k; const ay = (line[i][0] - lat) * 111320;
    const bx = (line[i + 1][1] - lon) * k; const by = (line[i + 1][0] - lat) * 111320;
    const dx = bx - ax; const dy = by - ay;
    const len2 = dx * dx + dy * dy;
    const t = len2 ? Math.max(0, Math.min(1, -(ax * dx + ay * dy) / len2)) : 0;
    const px = ax + t * dx; const py = ay + t * dy;
    const d = Math.hypot(px, py);
    if (d < best) {
      best = d;
      along = cum[i] + t * Math.sqrt(len2);
      // Which side of the way you travel the point is on (it sits at the origin).
      side = dx * (-ay) - dy * (-ax) > 0 ? 'left' : 'right';
    }
  }
  return { cross: best, along, side };
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
  return found
    .map((p) => ({ p, ...alongLine(line, cum, p.lat, p.lon) }))
    .filter((x) => x.cross <= limit)
    .sort((a, b) => a.along - b.along)
    .map(({ p, along, side, cross }) => ({
      ...p,
      // Every place listed is beside the route: say how far along, and which side.
      base: `${p.name}, ${kindText(p)}`,
      near: along < 50 ? from.split(', ')[0] : '',
      distance: along,
      side: cross > 8 ? side : '',
      text: `${p.name}, ${kindText(p)}`,
    }));
}

async function tileFoodAlong(line, cum, cats = new Set(['food'])) {
  const pts = [];
  let next = 0;
  for (let i = 0; i < line.length; i += 1) {
    if (cum[i] >= next) { pts.push(line[i]); next = cum[i] + 400; }
  }
  pts.push(line[line.length - 1]);
  await Promise.all(pts.map(([la, lo]) => street.ensureArea(la, lo)));
  const out = new Map();
  for (const [la, lo] of pts) {
    for (const p of street.placesNear(la, lo, 250, 200, cats)) {
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
  await addSuburbs(st.food);
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
  $('journey-food-list').querySelector('button')?.focus();
  say(`${all.length} food places along the way.${more}${missed} ${foodLabel(0)}`);
}

const WALK_FOOD_M = 50; // food this close to a walking route counts as on the way

/** Food within 100 metres of the road for the drive, in driving order. */
async function driveFood() {
  const d = st.drive;
  if (!d) return;
  const seq = st.seq;
  const km = haversineM(d.from.lat, d.from.lon, d.to.lat, d.to.lon) / 1000;
  say(km > 100 ? 'Looking for food along the drive. A long drive can take a minute.' : 'Looking for food along the drive.');
  $('journey-drive-food').disabled = true;
  let found = [];
  try {
    found = await roadFood({ kind: 'car', from: d.from, to: d.to });
  } catch {
    found = null;
  }
  $('journey-drive-food').disabled = false;
  if (seq !== st.seq || st.drive !== d) return;
  if (found === null) return say('Food along the drive could not be loaded. Try again in a minute.');
  st.food = found.slice(0, MAX_FOOD);
  await addSuburbs(st.food);
  st.foodIdx = 0;
  foodPicker.refresh();
  showFoodLinks();
  $('journey-food').hidden = !st.food.length;
  if (!st.food.length) return say('No food places found along the drive.');
  $('journey-food-list').querySelector('button')?.focus();
  const more = found.length > MAX_FOOD ? ` The first ${MAX_FOOD} are listed.` : '';
  say(`${found.length} food places along the drive.${more} ${foodLabel(0)}`);
}

/** Food within 50 metres of the walking route, in walking order. */
async function walkFood() {
  const w = st.walk;
  if (!w) return;
  const seq = st.seq;
  say('Looking for food along this walk.');
  const line = decodePolyline(w.line);
  const cum = cumulative(line);
  const found = await tileFoodAlong(line, cum);
  if (seq !== st.seq || st.walk !== w) return;
  const along = [];
  for (const p of found) {
    const at = alongLine(line, cum, p.lat, p.lon);
    if (at.cross > WALK_FOOD_M) continue;
    along.push({
      ...p, along: at.along, base: `${p.name}, ${p.kind}`, distance: at.along,
      side: at.cross > 8 ? at.side : '', text: `${p.name}, ${p.kind}`,
    });
  }
  along.sort((a, b) => a.along - b.along);
  st.food = along.slice(0, MAX_FOOD);
  await addSuburbs(st.food);
  st.foodIdx = 0;
  foodPicker.refresh();
  showFoodLinks();
  $('journey-food').hidden = !st.food.length;
  if (!st.food.length) return say('No food places found along this walk.');
  $('journey-food-list').querySelector('button')?.focus();
  say(`${st.food.length} food places along this walk. ${foodLabel(0)}`);
}

/** Each food place's suburb, from the postcode data, for the list: "…, in Granville". */
async function addSuburbs(list) {
  for (const p of list) {
    if (p.suburb !== undefined) continue;
    const pc = await postcodeAt(p.lat, p.lon).catch(() => null);
    p.suburb = pc && pc.place ? pc.place : '';
  }
}

/**
 * A food place's address when it's known without a lookup: its own from
 * OpenStreetMap, or the nearest house number in the street map (loaded along
 * a walk). "4 Parramatta Square, Parramatta", or '' if neither.
 */
function knownAddress(p) {
  if (p.addr) return p.addr.includes(',') || !p.suburb ? p.addr : `${p.addr}, ${p.suburb}`;
  if (street.areaReady(p.lat, p.lon)) {
    const a = street.nearestAddress(p.lat, p.lon, 30);
    if (a && a.street) return [`${a.num} ${a.street}`, p.suburb].filter(Boolean).join(', ');
  }
  return '';
}

/** A food place's text with its address (or suburb), unless it already says where ("near Burwood"). */
function withSuburb(p) {
  // "Ruse Bar and Brasserie, restaurant, 4 Parramatta Square, Parramatta, in 13 kilometres, right"
  const where = knownAddress(p) || p.suburb;
  if (p.base) {
    if (p.near) return [p.base, knownAddress(p), `near ${p.near}`].filter(Boolean).join(', ');
    const d = p.distance < 100 ? Math.round(p.distance / 5) * 5 : p.distance < 1000 ? Math.round(p.distance / 10) * 10 : p.distance;
    return [p.base, where, p.distance != null ? `in ${formatDistance(d)}` : '', p.side].filter(Boolean).join(', ');
  }
  const addr = knownAddress(p);
  if (addr) return `${p.text}, ${addr}`;
  if (!p.suburb || p.text.includes(p.suburb)) return p.text;
  return `${p.text}, in ${p.suburb}`;
}

/** The food places as buttons; choosing one picks it for Go there and the links below. */
function showFoodList() {
  const ul = $('journey-food-list');
  ul.replaceChildren(...st.food.map((p, i) => {
    const li = document.createElement('li');
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = withSuburb(p);
    b.addEventListener('click', () => {
      st.foodIdx = i;
      showFoodLinks();
      $('journey-food-chosen').textContent = `${p.name}. Finding the address…`;
      $('journey-food-go').focus();
      foodAddress(p).then(async (addr) => {
        if (st.foodIdx !== i) return; // another place chosen meanwhile
        const streetName = (addr.match(/^\s*[\dA-Za-z/-]*\d[\dA-Za-z/-]*\s+([^,]+)/) || [])[1] || '';
        const where = await describeLocation(p.lat, p.lon, streetName).catch(() => '');
        if (st.foodIdx !== i) return;
        const whereText = where ? ` ${where[0].toUpperCase()}${where.slice(1)}.` : '';
        $('journey-food-chosen').textContent = addr ? `${p.name}. Address: ${addr}.${whereText}` : `${p.name}.${whereText || ' No address found.'}`;
      });
    });
    li.append(b);
    return li;
  }));
  $('journey-food-chosen').textContent = '';
}

/**
 * A food place's street address: its own from OpenStreetMap, else the nearest
 * house number in the street map, else an address lookup at its position.
 */
async function foodAddress(p) {
  if (p.addr) return p.addr;
  if (street.areaReady(p.lat, p.lon)) {
    const a = street.nearestAddress(p.lat, p.lon, 30);
    if (a && a.street) return [`${a.num} ${a.street}`, p.suburb].filter(Boolean).join(', ');
  }
  const a = await addressAt(p.lat, p.lon).catch(() => null);
  if (!a || !a.street) return '';
  // The suburb the list already gives (the lookup's place can be the city, "Sydney").
  return [a.number ? `${a.number} ${a.street}` : a.street, p.suburb || a.place].filter(Boolean).join(', ');
}

function foodLabel(i) {
  const p = st.food[i];
  return `${withSuburb(p)}. ${i + 1} of ${st.food.length}.`;
}

/** Apple Maps, Uber Eats and DoorDash links for the chosen food place, as in Places nearby. */
function showFoodLinks() {
  $('journey-food-menu-box').hidden = true;
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
/** Use my location: From becomes where the device really is, by its street address. */
async function fromMyLocation() {
  say('Finding your location…');
  try {
    const p = await hooks.myLocation();
    st.fromHere = p;
    $('journey-from').value = p.label;
    say(`From ${p.label}.`);
    $('journey-to').focus();
  } catch (err) {
    say(err.message || 'Your location could not be found.');
  }
}

export function initJourney(h) {
  hooks = h;
  $('journey-from-here').addEventListener('click', fromMyLocation);
  st.date = travelDays()[1].value;
  datePicker = makePicker($('journey-date'), {
    browse: true,
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
    browse: true,
    shown: $('journey-option-value'),
    items: () => st.options.map((o, i) => ({ value: i, label: optionText(o, i, st.options.length) })),
    get: () => st.optIdx,
    set: (v) => { st.optIdx = v; },
  });
  legPicker = makePicker($('journey-leg-list'), {
    browse: true,
    shown: $('journey-leg-value'),
    items: () => (st.option ? st.option.legs.map((_, i) => legItem(i)) : []),
    get: () => st.legIdx,
    set: (v) => {
      st.legIdx = v;
      showFlights(v, false);
    },
  });
  flightPicker = makePicker($('journey-flight-list'), {
    browse: true,
    shown: $('journey-flight-value'),
    items: () => st.flights.map((f, i) => ({ value: i, label: flightText(f, i, st.flights.length) })),
    get: () => st.flightIdx,
    set: (v) => { st.flightIdx = v; },
  });
  // Food is a plain list of buttons: VoiceOver reads each as you swipe, which
  // the adjustable picker did not do reliably here.
  foodPicker = { refresh: showFoodList };

  const enter = (id, fn) => $(id).addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); fn(); }
  });
  enter('journey-from', walkClick);
  enter('journey-to', walkClick);
  enter('journey-option-list', chooseOption);
  onSlowClick($('journey-plan'), 'Finding ways to make the trip', plan, { slow: true });
  onSlowClick($('journey-stations-find'), 'Finding stations near you', nearestStations);
  onSlowClick($('journey-station-walk'), 'Finding a walking route', walkToStation);
  onSlowClick($('journey-walk-plan'), 'Finding a walking route', walkClick);
  onSlowClick($('journey-drive-plan'), 'Finding the place', planDrive);
  onSlowClick($('journey-drive-food'), 'Finding food along the drive', driveFood, { slow: true });
  onSlowClick($('journey-walk-food'), 'Finding food along the walk', walkFood);
  $('journey-guide').addEventListener('click', toggleGuiding);
  $('journey-choose').addEventListener('click', chooseOption);
  $('journey-read').addEventListener('click', readAll);
  onSlowClick($('journey-food-find'), 'Finding food along the way', findFood, { slow: true });
  $('journey-food-go').addEventListener('click', goToFood);
  $('journey-food-menu').addEventListener('click', () => {
    const p = st.food[st.foodIdx];
    if (p) showMenu($('journey-food-menu-box'), { name: p.name, lat: p.lat, lon: p.lon, website: p.website || (p.details && p.details.website) });
  });
  datePicker.refresh();
}

/** The Journey toolbar button: go to the journey planner. */
export function openJourney() {
  $('journey-section').open = true;
  const h = $('journey-title');
  h.scrollIntoView({ block: 'start' });
  h.focus();
}
