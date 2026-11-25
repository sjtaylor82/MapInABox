// Map in a Box Web: world map and street mode.

import { settings } from './settings.js';
import { initSpeech, say, spell, lastSpoken } from './speech.js';
import * as audio from './audio.js';
import {
  data, loadWorld, describe, countryAt, countryByName, factsFor, continentFor, canonical,
  nearestCity, city, nearestCityLabel, nearbyFeatures, searchLocal, parseCoordinates,
} from './world.js';
import {
  formatDistance, formatLat, formatLon, offsetKm, bearingDeg, compassName, haversineM,
} from './geo.js';
import * as online from './online.js';
import { initMapView, updateMapView, draw as drawMap, setStreetDrawer } from './mapview.js';
import * as steering from './steering.js';
import * as street from './street.js';
import { makePicker } from './picker.js';
import { postcodeAt } from './postcode.js';
import { showMenu } from './menu.js';
import { checkVenue, openMenusNearby } from './venues.js';
import { onSlowClick, working } from './busy.js';
import { describeLocation } from './placeinfo.js';
import { startMonitor, stopMonitor, monitorActive, setMonitorLevel, monitorStatus } from './gpsmonitor.js';
import { speakableHours } from './placedetails.js';
import { initJourney, walkTo, driveTo } from './journey.js';
import { openScreen, closeScreen, closeAllScreens } from './screen.js';
import { lookUpAddresses, placeTextWithAddress } from './addresses.js';

const $ = (id) => document.getElementById(id);

const state = {
  lat: -27.47,
  lon: 153.03,
  info: { country: '', admin: '', city: '', iso2: '', continent: '', onLand: false, label: '' },
  seq: 0,
  started: false,
  soundWaiting: true, // the country's sound starts on the first move
  busy: false,
  mode: 'world',      // 'world' | 'street'
  streetName: '',     // the street being followed in street mode
  streetLabel: '',    // what was last said for the street position
  corner: null,       // picking turns: { options, idx, arrival, armed } while stopped at a corner
  travel: null,       // picking turns: the bearing of travel along the current street
  practice: null,     // tilt practice in progress: { i }
  skipTone: false,    // a tilt already played its direction tone for this move
};

// ── Start-up ──────────────────────────────────────────────────────────────

async function boot() {
  initSpeech();
  initMapView($('map-canvas'), $('map-caption'));
  applyLook();
  applyLevel();
  applyVisualAssist();
  showMode();
  // More: the toolbar buttons used less often.
  $('btn-more').addEventListener('click', () => {
    const open = $('toolbar-more').hidden;
    $('toolbar-more').hidden = !open;
    $('btn-more').setAttribute('aria-expanded', String(open));
  });
  // Get the Desktop App: only on Windows and Mac computers, where it runs.
  // (An iPad reports itself as a Mac, but has a touch screen.)
  const ua = navigator.userAgent;
  const iPad = /Macintosh/.test(ua) && navigator.maxTouchPoints > 1;
  $('link-desktop').hidden = !(/Windows NT|Macintosh/.test(ua) && !iPad && !/CrOS|Android|iPhone|iPad/.test(ua));
  const touch = steering.isTouchDevice() && steering.steeringSupported();
  $('btn-steer').hidden = !touch;
  if (steering.isTouchDevice()) $('map-hint').textContent = '';
  const saved = settings.get('position');
  const hasSaved = !!(saved && Number.isFinite(saved.lat));
  // iPhone lets a page play sound and speak only after a tap or key press.
  const unlockOnce = () => {
    audio.unlock();
    unlockVoice();
    // The map may already be somewhere (your location, found before any tap):
    // start that country's sound now that sound is allowed.
    if (steering.isTouchDevice() && state.mode === 'world' && state.info && state.info.country) {
      state.soundWaiting = !audio.playPlace(state.info.onLand && state.info.country !== 'Open Water' ? state.info.country : 'ocean', state.info.continent);
    }
    for (const t of UNLOCK_EVENTS) document.removeEventListener(t, unlockOnce, true);
  };
  // A VoiceOver double tap may arrive only as a click.
  const UNLOCK_EVENTS = ['pointerdown', 'keydown', 'touchend', 'click'];
  for (const t of UNLOCK_EVENTS) document.addEventListener(t, unlockOnce, true);
  try {
    await loadWorld((msg) => { $('load-status').textContent = `${msg}…`; });
    audio.configureAudio(data.sounds, data.tables);
  } catch (err) {
    $('load-status').textContent = `Map in a Box could not load its map data. ${err.message}`;
    return;
  }
  // Straight onto the map: where you left off for now, then where you
  // really are once the device says.
  state.started = true;
  $('intro').hidden = true;
  $('app').hidden = false;
  // Start at the top of the page, not on the map; the map is a Tab away.
  drawMap();
  if (hasSaved) {
    state.lat = saved.lat;
    state.lon = saved.lon;
  }
  // No location within 5 seconds (blocked by a school, or the Allow question
  // not answered): go to where you left off, or ask where to start. If the
  // location turns up later, and you haven't moved or typed meanwhile, go there.
  let fellBack = false;
  let seqAtFallback = -1;
  const fallBack = () => {
    if (fellBack) return;
    fellBack = true;
    if (hasSaved) moved({ tone: false });
    else openJump('Where would you like to start? Type a city, country or coordinates, then press Enter.');
    seqAtFallback = state.seq;
  };
  const fallbackTimer = setTimeout(fallBack, 5000);
  goToMyLocation({
    quiet: true,
    accept: () => !fellBack || (state.seq === seqAtFallback && !$('jump-input').value.trim()),
    // At a food venue? Offer its menu (Expert).
    onFound: (fix) => { clearTimeout(fallbackTimer); checkVenue(fix).catch(() => {}); },
    onFail: () => { clearTimeout(fallbackTimer); fallBack(); },
  });
}

// ── Moving and announcing ─────────────────────────────────────────────────

async function moved({ tone = true, prefix = '' } = {}) {
  if (state.mode === 'street') return movedStreet({ tone, prefix });
  const seq = ++state.seq;
  settings.set('position', { lat: state.lat, lon: state.lon });
  if (tone && !state.skipTone) audio.spatialTone(state.lat, state.lon, toneBounds());
  state.skipTone = false;
  updateMapView(state.lat, state.lon, null, null);
  const info = await describe(state.lat, state.lon, state.info);
  if (seq !== state.seq) return; // the cursor has moved on
  const countryChanged = info.country !== state.info.country;
  const sameSea = !info.onLand && info.label === state.info.label;
  state.info = info;
  let text = info.label;
  // A sea can stretch for thousands of kilometres. Repeating just its name
  // sounds like not moving, so say where the nearest land is as well.
  if (sameSea || (prefix && !info.onLand)) text = `${info.label}. ${landHint(state.lat, state.lon)}`;
  sayWhenSettled(seq, prefix ? `${prefix} ${text}` : text, { always: true });
  updateMapView(state.lat, state.lon, info.label, info.country);
  if (countryChanged || state.soundWaiting) {
    const place = info.onLand && info.country !== 'Open Water' ? info.country : 'ocean';
    state.soundWaiting = !audio.playPlace(place, info.continent);
  }
}

function landHint(lat, lon) {
  const { dist, idx } = nearestCity(lat, lon);
  const c = city(idx);
  const dir = compassName(bearingDeg(lat, lon, c.lat, c.lon)).replace('-', ' ');
  return `Nearest land ${c.name}, ${formatDistance(dist * 111 * 1000)} ${dir}.`;
}

function toneBounds() {
  if (state.mode === 'street') return street.areaBounds(state.lat, state.lon);
  if (settings.get('toneScope') !== 'country') return null;
  const c = countryByName(state.info.country);
  if (!c) return null;
  const [minx, miny, maxx, maxy] = c.bbox;
  return [miny, maxy, minx, maxx];
}

function step(bearing, km) {
  const before = state.lat;
  const p = offsetKm(state.lat, state.lon, km, bearing);
  if ((bearing === 0 && before >= 90) || (bearing === 180 && before <= -90)) {
    audio.edgeTone();
    say(bearing === 0 ? 'North Pole.' : 'South Pole.', { now: true });
    return;
  }
  state.lat = p.lat;
  state.lon = p.lon;
  moved();
}

function jumpTo(lat, lon, prefix = '') {
  state.lat = lat;
  state.lon = lon;
  moved({ tone: true, prefix });
}

/** Alt+Arrow: nearest foreign country in a direction (lookups.py). */
function nextCountry(bearing, direction) {
  const current = state.info.country;
  const stepKm = 10;
  const maxKm = 5000;
  for (let i = 1; i * stepKm <= maxKm; i++) {
    const p = offsetKm(state.lat, state.lon, i * stepKm, bearing);
    if ((bearing === 0 && p.lat >= 89.9) || (bearing === 180 && p.lat <= -89.9)) {
      say(`Reached the ${bearing === 0 ? 'North' : 'South'} Pole. No other country to the ${direction}.`);
      return;
    }
    const c = countryAt(p.lat, p.lon);
    if (c && c.name !== current) {
      jumpTo(p.lat, p.lon, `Next country to the ${direction}: ${c.name}, ${formatDistance(i * stepKm * 1000)}.`);
      return;
    }
  }
  audio.edgeTone();
  say(`No other country found to the ${direction} within ${formatDistance(maxKm * 1000)}.`);
}

/** The next town or suburb in a direction, from the map's own place list. */
function nextPlace(bearing, direction) {
  const start = nearestCity(state.lat, state.lon).idx;
  const seen = new Set([start]);
  for (let km = 2; km <= 1500; km += km < 300 ? 2 : 10) {
    const p = offsetKm(state.lat, state.lon, km, bearing);
    const { idx, dist } = nearestCity(p.lat, p.lon);
    if (seen.has(idx) || dist * 111 > 40) continue;
    seen.add(idx);
    const c = city(idx);
    const b = bearingDeg(state.lat, state.lon, c.lat, c.lon);
    if (street.bearingGap(b, bearing) > 35) continue;
    const away = haversineM(state.lat, state.lon, c.lat, c.lon);
    jumpTo(c.lat, c.lon, `Next place to the ${direction}, ${formatDistance(away)}:`);
    return;
  }
  audio.edgeTone();
  say(`No town found to the ${direction} within ${formatDistance(1500000)}.`);
}

// ── Postcode ──────────────────────────────────────────────────────────────

let postcodeTimer = null;

async function announcePostcode() {
  const { lat, lon } = state;
  const pc = await postcodeAt(lat, lon);
  if (!pc) return say('No postcode found for this location.');
  state.lastPostcode = pc.postcode;
  // "Cleveland 4163, Queensland."
  say(`${pc.place ? `${pc.place} ` : ''}${pc.postcode}${pc.admin ? `, ${pc.admin}` : ''}.`);
}

/**
 * Say where you are once you've stopped moving for 0.3 seconds (holding an
 * arrow or tilting doesn't speak every step), with the postcode: "Mortlake,
 * 2137." On the world map the postcode comes with every place; in street mode,
 * where each step names a street, only when it changes.
 */
function sayWhenSettled(seq, text, { always = false } = {}) {
  clearTimeout(postcodeTimer);
  postcodeTimer = setTimeout(async () => {
    if (seq !== state.seq) return; // moved on since
    let code = '';
    const onLand = state.mode === 'street' || state.info.onLand;
    if (settings.get('autoPostcode') !== false && onLand) {
      const pc = await postcodeAt(state.lat, state.lon).catch(() => null);
      if (seq !== state.seq) return;
      if (pc && (always || pc.postcode !== state.lastPostcode)) code = pc.postcode;
      if (pc) state.lastPostcode = pc.postcode;
    }
    say(code ? `${text.replace(/\.\s*$/, '')}, ${code}.` : text, { now: true });
  }, 300);
}

// ── Where am I (device location) ──────────────────────────────────────────

let locating = false;

/** The device's position: resolves { lat, lon, accuracy }, or rejects with a sentence to say. */
function devicePosition() {
  return new Promise((resolve, reject) => {
    if (!('geolocation' in navigator) || !window.isSecureContext) {
      reject(new Error('Location is not available in this browser.'));
      return;
    }
    navigator.geolocation.getCurrentPosition(
      (pos) => resolve({ lat: pos.coords.latitude, lon: pos.coords.longitude, accuracy: pos.coords.accuracy }),
      (err) => reject(new Error(err.code === err.PERMISSION_DENIED
        ? 'Location access was not allowed. Allow location for this site in your browser settings.'
        : err.code === err.TIMEOUT
          ? 'Finding your location took too long. Try again, ideally outdoors or near a window.'
          : 'Your location could not be found. Check that location services are on for this device.')),
      { enableHighAccuracy: true, timeout: 20000, maximumAge: 30000 },
    );
  });
}

/** Move the map to where the device is. quiet: no "Finding your location" first. */
async function goToMyLocation({ quiet = false, onFail = () => {}, onFound = () => {}, accept = () => true } = {}) {
  if (locating) return;
  locating = true;
  if (!quiet) say('Finding your location…');
  try {
    const { lat, lon, accuracy } = await devicePosition();
    if (!accept()) return; // arrived too late: you've already gone somewhere
    if (document.querySelector('dialog[open]')) document.querySelector('dialog[open]').close();
    const near = accuracy <= 1000 ? '' : ' Only roughly, perhaps from your internet connection.';
    jumpTo(lat, lon, `You are here.${near}`);
    onFound({ lat, lon, accuracy });
  } catch (err) {
    if (!quiet) say(err.message);
    onFail();
  } finally {
    locating = false;
  }
}

/** Where am I: say the street address where the device is, without moving the map. */
async function whereAmI() {
  if (locating) return;
  locating = true;
  say('Finding your address…');
  try {
    const { lat, lon, accuracy } = await devicePosition();
    // House numbers from the street map where it has one within 40 metres;
    // the suburb, state and postcode from the address search.
    const [ph] = await Promise.all([
      online.addressAt(lat, lon).catch(() => null),
      Promise.race([street.ensureArea(lat, lon), new Promise((r) => setTimeout(r, 6000))]),
    ]);
    const ad = street.areaReady(lat, lon) ? street.nearestAddress(lat, lon, 40) : null;
    let streetPart = '';
    if (ad && ad.street) streetPart = `${ad.num} ${ad.street}`;
    else if (ph && ph.street) streetPart = ph.number ? `${ph.number} ${ph.street}` : ph.street;
    else if (street.areaReady(lat, lon)) streetPart = street.streetLabel(lat, lon);
    let postcode = ph && ph.postcode;
    let place = ph && ph.place;
    if (!postcode || !place) {
      const pc = await postcodeAt(lat, lon).catch(() => null);
      postcode = postcode || (pc && pc.postcode);
      place = place || (pc && pc.place);
    }
    const text = [streetPart, place, [ph && ph.state, postcode].filter(Boolean).join(' ')].filter(Boolean).join(', ');
    const rough = accuracy > 100 ? ` Accurate to about ${formatDistance(accuracy)}.` : '';
    say(text ? `You are at ${text}.${rough}` : `Your address could not be found.${rough}`);
  } catch (err) {
    say(err.message);
  } finally {
    locating = false;
  }
}

// ── Street mode ───────────────────────────────────────────────────────────

async function toggleStreetMode() {
  if (state.mode === 'street') return exitStreetMode();
  state.mode = 'street';
  state.streetName = '';
  const seq = ++state.seq;
  showMode();
  audio.stopAmbient();
  say('Street mode. Loading streets…');
  const { lat, lon } = state;
  const result = await waitForArea(lat, lon, seq);
  if (state.mode !== 'street' || seq !== state.seq) return;
  if (!result.ok) {
    say(`Could not load the streets here. ${result.error} Press F11 to go back to the world map, or try again.`);
    return;
  }
  // Land on the nearest street, as the desktop does.
  const snap = street.nearestStreet(lat, lon, 3000);
  if (!snap) {
    say('Street mode. No streets found within 3 kilometres. Use Jump to go to a town.');
    return;
  }
  state.lat = snap.lat;
  state.lon = snap.lon;
  state.streetName = snap.name;
  // Say once when the backup map is in use, which has fewer details.
  const note = street.areaSource(lat, lon) === 'openfreemap' ? 'Using the backup map: fewer house numbers, no phone numbers.' : '';
  movedStreet({ prefix: 'Street mode.', note });
}

function exitStreetMode() {
  state.mode = 'world';
  state.streetName = '';
  showMode();
  state.info = { ...state.info, country: '', admin: '' }; // so the country and its sound come back
  moved({ prefix: 'World map.' });
}

function showMode() {
  const inStreet = state.mode === 'street';
  $('btn-street').setAttribute('aria-pressed', String(inStreet));
  // Short label; the keys go in a hint, which VoiceOver says after a pause
  // (and can be switched off). Phones and tablets have no keys, so no hint.
  if (steering.isTouchDevice()) {
    // Phones and tablets: no "World map, web application". The map just holds
    // where you are, which VoiceOver reads when focus moves to it.
    for (const a of ['role', 'aria-label', 'aria-describedby']) $('map').removeAttribute(a);
    $('map-caption').removeAttribute('aria-hidden');
  } else {
    // Computers: an application, so screen readers pass the arrow keys to the map.
    $('map').setAttribute('role', 'application');
    $('map').setAttribute('aria-label', inStreet ? 'Street map' : 'World map');
  }
  $('map-hint').textContent = steering.isTouchDevice() ? ''
    : inStreet ? 'Arrow keys move. P places nearby. F11 world map. Escape leaves the map.' : 'Arrow keys move. F11 street mode. F1 help. Escape leaves the map.';
  showStreetOnly();
  showTiltMove();
  showStreetSteer();
  setStreetDrawer(inStreet ? street.drawStreets : null);
  showStep();
}

async function movedStreet({ tone = true, prefix = '', note = '', suffix = '', keepCorner = false } = {}) {
  const seq = ++state.seq;
  if (!keepCorner) state.corner = null;
  const { lat, lon } = state;
  settings.set('position', { lat, lon });
  if (tone && !state.skipTone) audio.spatialTone(lat, lon, toneBounds());
  state.skipTone = false;
  updateMapView(lat, lon, null, null);
  if (!street.areaReady(lat, lon)) {
    say('Loading streets…');
    const doneLoading = working(null, 'Loading streets', { speak: false });
    const result = await waitForArea(lat, lon, seq).finally(doneLoading);
    if (seq !== state.seq) return;
    if (!result.ok) {
      say(`Could not load the streets here. ${result.error}`);
      return;
    }
  } else {
    street.ensureArea(lat, lon); // fetch the next tile ahead of time
  }
  const label = street.streetLabel(lat, lon, state.streetName);
  state.streetLabel = label;
  const near = street.nearestStreet(lat, lon, 25, state.streetName);
  state.streetName = near ? near.name : '';
  sayWhenSettled(seq, [prefix, note, label.endsWith('.') ? label : `${label}.`, suffix].filter(Boolean).join(' '));
  updateMapView(lat, lon, label, null);
  // Keep the country and region up to date for C, R and the other keys.
  describe(lat, lon, state.info).then((info) => {
    if (seq === state.seq) state.info = info;
  });
}

/**
 * Load the streets around a point. The street service can take most of a
 * minute for an area nobody has visited yet, so say it is still working.
 */
async function waitForArea(lat, lon, seq) {
  const timer = setInterval(() => {
    if (seq === state.seq) say('Still loading streets.');
  }, 15000);
  try {
    return await street.ensureArea(lat, lon);
  } finally {
    clearInterval(timer);
  }
}

/** One step in street mode, straight in any direction, in metres. */
function streetMove(bearing, m) {
  const p = offsetKm(state.lat, state.lon, m / 1000, bearing);
  state.lat = p.lat;
  state.lon = p.lon;
  movedStreet();
}

let noStreetSaidAt = 0;

/** Steering in street mode: 'pick' turns at corners, or go 'free'. */
function streetSteerMode() {
  return settings.get('streetSteer') === 'free' ? 'free' : 'pick';
}

/** Follow the streets the way nearest a heading, stopping at the next corner. */
function streetFollow(bearing, m, pick = false) {
  if (!street.areaReady(state.lat, state.lon)) return streetMove(bearing, m);
  const r = street.followStep(state.lat, state.lon, bearing, m, state.streetName);
  if (!r) return streetMove(bearing, m); // not on a street yet: head straight for one
  if (r.none) {
    audio.edgeTone();
    // Steering repeats this every step, so only say it now and then.
    const now = performance.now();
    if (now - noStreetSaidAt > 4000) {
      noStreetSaidAt = now;
      say(`No street goes ${compassName(bearing).replace('-', ' ')} from here.`);
    }
    return;
  }
  arriveAlong(r, pick, bearing);
}

/** After a step along the streets. When picking turns, stop at a corner with a choice of ways. */
function arriveAlong(r, pick, heading) {
  const from = [state.lat, state.lon];
  state.lat = r.lat;
  state.lon = r.lon;
  state.streetName = r.name;
  const note = r.end ? `End of ${r.name}.` : '';
  const arrival = bearingDeg(from[0], from[1], r.lat, r.lon);
  if (pick) state.travel = arrival;
  if (!pick || !r.corner) return movedStreet({ note });
  const options = street.cornerOptions(r.lat, r.lon);
  // Only stop where there is a real choice, not counting the way back.
  const ahead = options.filter((o) => street.bearingGap(o.bearing, (arrival + 180) % 360) > 35);
  if (ahead.length < 2) return movedStreet({ note });
  state.corner = { options, idx: nearestOption(options, heading), arrival, armed: false, chosen: null };
  movedStreet({
    note,
    keepCorner: true,
    suffix: `A corner with ${options.length} ways. Lay it flat, then tilt towards the way you want.`,
  });
}

function nearestOption(options, heading) {
  let best = 0;
  options.forEach((o, i) => {
    if (street.bearingGap(o.bearing, heading) < street.bearingGap(options[best].bearing, heading)) best = i;
  });
  return best;
}

function cornerChoice() {
  return state.corner.options[state.corner.idx];
}

/** "Turn left onto Ann Street, heading west." relative to the way you arrived at the corner. */
function optionText(o) {
  const rel = ((o.bearing - state.corner.arrival + 540) % 360) - 180;
  const same = o.name === state.streetName;
  const onto = same ? 'along' : 'onto';
  const side = rel < 0 ? 'left' : 'right';
  const a = Math.abs(rel);
  let action;
  if (a <= 20) action = 'Straight on';
  else if (a > 155) action = 'Back';
  else if (a < 55) action = `Bear ${side}`;
  else if (a < 125) action = `Turn ${side}`;
  else action = `Sharp ${side}`;
  return `${action} ${onto} ${o.name}, heading ${compassName(o.bearing).replace('-', ' ')}.`;
}

/** Set off from a corner along the chosen way. */
function takeCorner(m) {
  const o = cornerChoice();
  state.corner = null;
  state.streetName = o.name;
  arriveAlong(street.takeBranch(o, m), true, o.bearing);
}

// ── Places nearby ─────────────────────────────────────────────────────────

let placeResults = [];

let categoryTimer = null;

/** The category chosen in the Category picker: 'all' or one of street.PLACE_CATEGORIES. */
function placeCategory() {
  const v = settings.get('placeCategory');
  return v && street.PLACE_CATEGORIES.some(([k]) => k === v) ? v : 'all';
}

function categoryLabel(key) {
  return key === 'all' ? 'All places' : street.PLACE_CATEGORIES.find(([k]) => k === key)[1];
}

/** P, or Find places: list the places around you in the Places nearby picker. */
async function openPlaces() {
  // P on the map: Places nearby opens on its screen first, so Back from the list goes there.
  if ($('places-toggle').getAttribute('aria-expanded') !== 'true') $('places-toggle').click();
  // Places a kilometre away can be in the next map area: load those first.
  // (On the world map in Expert, the map data around the map position loads now.)
  const { lat, lon } = state;
  let slow = setTimeout(() => say('Finding places…'), 800);
  await Promise.race([street.ensureRadius(lat, lon, PLACES_RADIUS), new Promise((r) => setTimeout(r, 20000))]);
  clearTimeout(slow);
  if (!street.areaReady(lat, lon)) return say('The map data here could not be loaded. Try again in a moment.');
  $('places-pick').hidden = false;
  await listPlaces(true);
}

const PLACES_RADIUS = 1000; // metres
const PLACES_MAX = 100;

/** Fill the Place picker with the nearest places in the categories switched on. */
async function listPlaces(focusList) {
  const category = placeCategory();
  const cats = category === 'all' ? null : new Set([category]);
  let radius = PLACES_RADIUS;
  let found = street.placesNear(state.lat, state.lon, radius, PLACES_MAX, cats);
  if (found.length < 5) {
    radius = 3000; // the country: look further
    found = street.placesNear(state.lat, state.lon, radius, PLACES_MAX, cats);
  }
  // Street addresses the map doesn't have (Expert looks them up; a second or two).
  if (level() === 'expert') await lookUpAddresses(found);
  placeResults = found;
  state.placeIdx = 0;
  showPlaceButtons();
  if (!found.length) {
    const why = `No ${category === 'all' ? 'places' : categoryLabel(category).toLowerCase()} found within ${formatDistance(radius)}.`;
    $('places-prompt').textContent = why;
    $('places-prompt').hidden = false;
    if (focusList) $('places-category').focus();
    return say(why);
  }
  const what = category === 'all' ? 'places' : categoryLabel(category).toLowerCase();
  const how = `${found.length} ${what} within ${formatDistance(radius)}`;
  $('places-prompt').hidden = true;
  $('places-list-title').textContent = `${how[0].toUpperCase()}${how.slice(1)}`;
  if (focusList) {
    openScreen($('places-pick'), { from: $('places-find'), close: () => { $('places-pick').hidden = true; } });
    $('places-buttons').querySelector('button').focus();
  }
  say(`${how}. ${street.placeText(found[0])}.`);
}

/** The places as a list of buttons, nearest first. Pressing one opens that place on a screen of its own. */
function showPlaceButtons() {
  $('places-buttons').replaceChildren(...placeResults.map((p, i) => {
    const li = document.createElement('li');
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = placeTextWithAddress(p);
    b.addEventListener('click', () => openPlaceScreen(i, b));
    li.append(b);
    return li;
  }));
}

// The place screen: its name, Jump to here, Details, Menu and Apple Maps.
// Details and Menu each replace that on the screen; Back goes back a step,
// and from the place itself back to the list, onto the place you chose.
let placeOpener = null;   // the list button that opened the screen
let placeSubFrom = null;  // the button that opened Details or Menu

function openPlaceScreen(i, opener) {
  const p = placeResults[i];
  if (!p) return;
  state.placeIdx = i;
  placeOpener = opener;
  showPlaceLinks();
  $('place-screen-title').textContent = p.name;
  $('place-summary').textContent = placeTextWithAddress(p).slice(p.name.length + 2);
  placeView('home');
  $('place-screen').showModal();
  $('place-screen-title').focus();
}

function placeView(view) {
  $('place-home').hidden = view !== 'home';
  $('place-details').hidden = view !== 'details';
  $('place-menu').hidden = view !== 'menu';
}

function placeBack() {
  if (!$('place-home').hidden) return $('place-screen').close();
  placeView('home');
  (placeSubFrom || $('place-screen-title')).focus();
}

/** The Apple Maps link and Menu button for the chosen place. Apple Maps has hours, phone and directions. */
function showPlaceLinks() {
  const p = placeResults[state.placeIdx || 0];
  if (!p) return;
  const q = encodeURIComponent(p.name);
  $('place-maps').href = `https://maps.apple.com/?q=${q}&ll=${p.lat},${p.lon}`;
  $('place-maps').textContent = `Open ${p.name} in Apple Maps`;
  // The menu search finds its Uber Eats, DoorDash or own menu, when it has one.
  $('places-menu').hidden = p.category !== 'food';
}

/** Phone, hours and website for the chosen place, looked up in OpenStreetMap. */
async function placeDetails() {
  const p = placeResults[state.placeIdx || 0];
  if (!p) return;
  placeSubFrom = $('places-details');
  placeView('details');
  $('place-details-text').textContent = `Looking up ${p.name}…`;
  $('place-call').hidden = true;
  $('place-web').hidden = true;
  $('place-details-title').textContent = `Details: ${p.name}`;
  $('place-details-title').focus();
  let d = p.details; // already here when the street data came from Overpass
  if (!d || !d.phone || !d.hours) {
    // Ask the server: it checks OpenStreetMap and fills the gaps from HERE.
    say(`Looking up ${p.name}…`);
    try {
      const more = await online.placeDetails(p.name, p.lat, p.lon);
      if (more) {
        d = { ...(d || {}) };
        for (const [k, v] of Object.entries(more)) if (v && !d[k]) d[k] = v;
        p.details = d;
      }
    } catch { /* still give what we have, and the address below */ }
  }
  if (placeResults[state.placeIdx || 0] !== p) return; // chose another meanwhile
  const parts = [];
  if (d && d.phone) parts.push(`Phone ${d.phone}`);
  if (d && d.hours) parts.push(`Hours: ${speakableHours(d.hours)}`);
  else if (d && d.hoursText) parts.push(`Hours: ${d.hoursText}`);
  let addressStreet = '';
  if (d && d.address) {
    parts.push(`Address: ${d.address}`);
    addressStreet = (d.address.match(/^\s*[\dA-Za-z/-]*\d[\dA-Za-z/-]*\s+([^,]+)/) || [])[1] || '';
  } else {
    // No address in OpenStreetMap: give the nearest house number and street, and the postcode.
    const ad = street.nearestAddress(p.lat, p.lon, 60);
    const near = ad && ad.street ? `${ad.num} ${ad.street}` : street.streetLabel(p.lat, p.lon);
    if (ad && ad.street) addressStreet = ad.street;
    const pc = await postcodeAt(p.lat, p.lon).catch(() => null);
    parts.push(`Near ${near}${pc ? `, ${pc.place ? `${pc.place} ` : ''}${pc.postcode}` : ''}`);
  }
  // Where exactly: "Corner of George Street and Bathurst Street", or its street and the cross streets either side.
  const where = await describeLocation(p.lat, p.lon, addressStreet).catch(() => '');
  if (placeResults[state.placeIdx || 0] !== p) return;
  if (where) parts.push(`${where[0].toUpperCase()}${where.slice(1)}`);
  if (d && d.cuisine) parts.push(`Cuisine: ${d.cuisine}`);
  if (d && d.website) parts.push('Has a website');
  if (d && d.wheelchair) parts.push(`Wheelchair access: ${d.wheelchair}`);
  const missing = !(d && (d.phone || d.hours || d.hoursText || d.website));
  const text = `${p.name}. ${parts.join('. ')}.${missing ? ' No phone number or opening hours are known for it.' : ''}`;
  $('place-details-text').textContent = text;
  $('place-call').hidden = !(d && d.phone);
  if (d && d.phone) {
    $('place-call').href = `tel:${d.phone.split(';')[0].replace(/[^+\d]/g, '')}`;
    $('place-call').textContent = `Call ${p.name}`;
  }
  $('place-web').hidden = !(d && d.website);
  if (d && d.website) {
    $('place-web').href = d.website.split(';')[0];
    $('place-web').textContent = `${p.name} website`;
  }
  $('place-details-text').tabIndex = -1;
  $('place-details-text').focus();
}

function choosePlace() {
  const p = placeResults[state.placeIdx];
  if (!p) return;
  // The list stays open, so details for the place are still a button away.
  $('places-prompt').hidden = true;
  placeOpener = null; // going to the map, not back to the list
  if ($('place-screen').open) $('place-screen').close();
  focusMap();
  if (state.mode !== 'street') return jumpTo(p.lat, p.lon, `${p.name}, ${p.kind}.`);
  state.lat = p.lat;
  state.lon = p.lon;
  state.streetName = '';
  movedStreet({ prefix: `${p.name}, ${p.kind}.` });
}

// ── Tilt steering ─────────────────────────────────────────────────────────

let steerIntroduced = false;

const directionName = (h) => compassName(h).replace('-', ' ');

function headingWord(h) {
  const name = compassName(h).replace('-', ' ');
  return name[0].toUpperCase() + name.slice(1);
}

function steerBlocked() {
  return !state.started || !!document.querySelector('dialog[open]');
}

const atCorner = () => state.mode === 'street' && !!state.corner;

// What one tilt does, chosen with the "Each tilt moves" picker.
const TILT_MOVES = {
  world: [['step', () => `One step, ${stepText(stepKm())}`], ['place', () => 'To the next town or suburb'], ['country', () => 'To the next country']],
  street: [['step', () => `One step, ${streetStepText(streetStepM())}`], ['corner', () => 'To the next corner']],
};

function tiltMove() {
  const key = state.mode === 'street' ? 'tiltMoveStreet' : 'tiltMove';
  const value = settings.get(key);
  return TILT_MOVES[state.mode].some(([v]) => v === value) ? value : 'step';
}

let tiltMovePicker = null;

function showTiltMove() {
  tiltMovePicker?.refresh();
}

/** Holding a tilt repeats, slowly enough to hear each place; big jumps more slowly still. */
function tiltRepeatMs() {
  return tiltMove() === 'step' ? 1200 : 3000;
}

// First-use practice: tilt each way once, so the movements are learnt and
// we know the device really feels all four.
const PRACTICE = [90, 270, 0, 180];
const TILT_HOW = {
  0: 'tilt the top edge down, away from you',
  90: 'tilt the right edge down',
  180: 'tilt the top edge up, towards you',
  270: 'tilt the left edge down',
};

function startPractice() {
  state.practice = { i: 0 };
  say(`Practice. Tip it well over, more than 60 degrees, each time. With the phone flat, ${TILT_HOW[PRACTICE[0]]}.`);
}

function practiceMove(dir) {
  const want = PRACTICE[state.practice.i];
  const t = steering.steeringStatus().tilt;
  if (dir !== want) {
    say(`That was ${directionName(dir)}: the top edge ${Math.abs(t.north)} degrees ${t.north >= 0 ? 'down' : 'up'} and the right edge ${Math.abs(t.east)} degrees ${t.east >= 0 ? 'down' : 'up'}. Lay it flat, then ${TILT_HOW[want]}.`);
    return;
  }
  state.practice.i += 1;
  if (state.practice.i >= PRACTICE.length) {
    state.practice = null;
    settings.set('tiltPracticed', true);
    say(`That's ${directionName(dir)}. Practice done. Lay it flat, then tilt to explore.`);
    return;
  }
  say(`That's ${directionName(dir)}. Lay it flat, then ${TILT_HOW[PRACTICE[state.practice.i]]}.`);
}

const steerHandlers = {
  ready() {
    steering.buzz(30);
    audio.steerClick(true);
    showSteerStatus();
    if (steerBlocked()) return;
    if (!settings.get('tiltPracticed') && !state.practice) {
      say('Ready.');
      setTimeout(startPractice, 1500);
      return;
    }
    say(steerIntroduced
      ? 'Ready.'
      : 'Ready. Tip it well over to move, lay it flat to stop.');
    steerIntroduced = true;
  },
  move(dir, repeat) {
    showSteerStatus();
    if (steerBlocked()) return;
    if (state.practice) {
      if (!repeat) {
        audio.directionTone(dir);
        practiceMove(dir);
      }
      return;
    }
    steering.buzz(15);
    audio.directionTone(dir);
    state.skipTone = true;
    onTiltMove(dir, repeat);
  },
  still() {
    audio.steerClick();
    showSteerStatus();
    if (atCorner() && !state.corner.armed) {
      // Laid flat at a corner: now a tilt picks a way out.
      state.corner.armed = true;
      if (!steerBlocked()) say('Tilt towards the way you want to hear it, and hold the tilt to take it.');
    }
  },
  paused() {
    showSteerStatus();
    if (!steerBlocked()) say('Steering paused. Lay the phone flat, screen up, to steer.');
  },
};

function onTiltMove(dir, repeat = false) {
  const name = directionName(dir);
  const how = tiltMove();
  if (state.mode === 'street') {
    const m = how === 'corner' ? 3000 : streetStepM();
    if (streetSteerMode() === 'free') return how === 'corner' ? streetFollow(dir, m) : streetMove(dir, m);
    if (state.corner) {
      // Arrived still tilted: wait until it has been laid flat, so a corner is never passed by accident.
      if (!state.corner.armed) return;
      // The first tilt says which way that is; holding it (or tilting the same way again) takes it.
      const idx = nearestOption(state.corner.options, dir);
      if (state.corner.chosen !== idx) {
        state.corner.idx = idx;
        state.corner.chosen = idx;
        state.skipTone = false;
        return say(`${optionText(cornerChoice())} Hold the tilt to go.`);
      }
      return takeCorner(m);
    }
    // Between corners, keep along the street the way you are travelling,
    // unless you have turned the dial right round.
    const along = state.travel != null && street.bearingGap(dir, state.travel) <= 120 ? state.travel : dir;
    return streetFollow(along, m, true);
  }
  if (how === 'country') return nextCountry(dir, name);
  if (how === 'place') return nextPlace(dir, name);
  // Seas are huge, so steps over water are bigger.
  step(dir, state.info.onLand ? stepKm() : Math.max(50, stepKm()));
}

/** Steering's own settings show only while steering is on. Speech says the rest. */
function showSteerStatus() {
  const off = steering.steeringStatus().mode === 'off';
  for (const e of document.querySelectorAll('[data-steer-only]')) e.hidden = off;
}

// ── Step size ─────────────────────────────────────────────────────────────

const STEP_SIZES = [1, 2, 5, 10, 25];
const STREET_STEPS = [5, 10, 20, 50, 100]; // metres

function streetStepM() {
  const m = settings.get('streetStepM');
  return STREET_STEPS.includes(m) ? m : 20;
}

function stepKm() {
  const km = settings.get('stepKm');
  return STEP_SIZES.includes(km) ? km : 5;
}

function stepText(km) {
  if (settings.get('units') === 'imperial') return formatDistance(km * 1000);
  return `${km} ${km === 1 ? 'kilometre' : 'kilometres'}`;
}

function streetStepText(m) {
  return settings.get('units') === 'imperial' ? formatDistance(m) : `${m} metres`;
}

let stepPicker = null;
let stepPickerMode = '';

function showStep() {
  const inStreet = state.mode === 'street';
  const text = inStreet ? streetStepText(streetStepM()) : stepText(stepKm());
  // Step size: radio buttons, rebuilt only when the sizes change (world or street).
  if (!stepPicker) {
    stepPicker = makePicker($('step-size'), {
      shown: $('step-value'),
      items: () => (state.mode === 'street'
        ? STREET_STEPS.map((m, i) => ({ value: i, label: streetStepText(m) }))
        : STEP_SIZES.map((km, i) => ({ value: i, label: stepText(km) }))),
      get: () => stepIndex(),
      set: (i) => setStepIndex(i, false),
    });
    stepPickerMode = state.mode;
  } else if (stepPickerMode !== state.mode) {
    stepPickerMode = state.mode;
    stepPicker.refresh();
  } else {
    stepPicker.sync();
  }
  $('step-value').textContent = text;
  showTiltMove(); // its "One step" label shows the size
}

function stepIndex() {
  return state.mode === 'street' ? STREET_STEPS.indexOf(streetStepM()) : STEP_SIZES.indexOf(stepKm());
}

function setStepIndex(i, announce = true) {
  const inStreet = state.mode === 'street';
  const sizes = inStreet ? STREET_STEPS : STEP_SIZES;
  const idx = Math.max(0, Math.min(sizes.length - 1, i));
  settings.set(inStreet ? 'streetStepM' : 'stepKm', sizes[idx]);
  showStep();
  if (announce) say(`Step size ${inStreet ? streetStepText(sizes[idx]) : stepText(sizes[idx])}.`);
}

function showStreetSteer() {
  const btn = $('btn-street-steer');
  btn.hidden = state.mode !== 'street' || $('btn-steer').hidden;
  btn.textContent = streetSteerLabel();
  $('set-street-steer').checked = streetSteerMode() === 'free';
}

function streetSteerLabel() {
  return streetSteerMode() === 'free' ? 'Steering: free' : 'Steering: pick turns';
}

function toggleStreetSteer() {
  settings.set('streetSteer', streetSteerMode() === 'free' ? 'pick' : 'free');
  state.corner = null;
  showStreetSteer();
  refreshSettings();
  say(streetSteerMode() === 'free'
    ? 'Steering free: it goes straight the way you tilt, across blocks too.'
    : 'Steering picks turns: it follows the street and stops at each corner so you can choose the next one.');
}

function setSteering(on, { byShake = false } = {}) {
  settings.set('steering', on);
  if (on) {
    steering.startSteering(steerHandlers, tiltRepeatMs);
    // Motion access is granted by now, so a shake can turn steering off (and on again).
    steering.startShakeWatch(onShake);
  } else {
    steering.stopSteering();
  }
  $('btn-steer').setAttribute('aria-pressed', String(on));
  showSteerStatus();
  if (!on) return say(byShake ? 'Steering off. Shake again to turn it back on.' : 'Steering off.');
  steerIntroduced = false;
  say(settings.get('tiltPracticed') ? 'Steering on.' : 'Steering on. Lay the phone flat. Shake it to turn steering off.');
}

/** A firm shake turns steering off, or back on. */
function onShake() {
  if (!state.started || document.querySelector('dialog[open]')) return;
  steering.buzz(60);
  setSteering(!steering.steeringActive(), { byShake: true });
}

async function toggleSteering() {
  if (steering.steeringActive()) return setSteering(false);
  if (await steering.requestSteeringPermission()) setSteering(true);
  else say('Steering is not available. Motion access was not allowed.');
}

/** iPhone only lets the page talk after a tap, so say something quietly from the Start tap. */
function unlockVoice() {
  if (settings.get('speech') === 'screenreader' || !('speechSynthesis' in window)) return;
  try {
    const u = new SpeechSynthesisUtterance(' ');
    u.volume = 0;
    speechSynthesis.speak(u);
  } catch { /* no voice */ }
}

// ── Information keys ──────────────────────────────────────────────────────

function noCountry() {
  const c = state.info.country;
  return !c || c === 'Open Water';
}

function announceFacts() {
  if (noCountry()) return say('No facts for open water.');
  const f = factsFor(state.info.country);
  if (!f) return say(`No facts found for ${state.info.country}.`);
  say(`${f.name}. Capital: ${f.capital}. Continent: ${f.continent}. Currency: ${f.currency}. Fact: ${f.fact}`);
}

function announceCapital() {
  if (noCountry()) return say('No country to look up.');
  const f = factsFor(state.info.country);
  say(f && f.capital && f.capital !== 'None' ? `The capital of ${f.name} is ${f.capital}.` : `No capital found for ${state.info.country}.`);
}

function announceCurrency() {
  if (noCountry()) return say('No country to look up.');
  const country = state.info.country;
  const entry = data.currency[country] || data.currency[canonical(country)];
  if (entry) {
    const text = typeof entry === 'string' ? entry
      : [entry.name, entry.code, entry.symbol && `symbol ${entry.symbol}`].filter(Boolean).join(', ');
    return say(`${country} uses ${text}.`);
  }
  const f = factsFor(country);
  say(f && f.currency ? `${country} uses ${f.currency}.` : `No currency found for ${country}.`);
}

function announceLanguages() {
  if (noCountry()) return say('No country to look up.');
  const country = state.info.country;
  const langs = data.languages[country] || data.languages[canonical(country)] || (factsFor(country) && data.languages[factsFor(country).name]);
  if (!langs) return say(`No language information for ${country}.`);
  if (!langs.length) return say(`${country} has no official language.`);
  say(`Languages of ${country}: ${langs.join(', ')}.`);
}

async function withNetwork(what, fn) {
  if (state.busy) return;
  state.busy = true;
  say(`Getting ${what}…`);
  try {
    say(await fn());
  } catch {
    say(`Could not get ${what}. Check the internet connection and try again.`);
  } finally {
    state.busy = false;
  }
}

let f2Taps = [];
function repeatLocation() {
  const now = performance.now();
  f2Taps = f2Taps.filter((t) => now - t < 600);
  f2Taps.push(now);
  const label = (state.mode === 'street' ? state.streetLabel : state.info.label) || 'No location yet.';
  if (f2Taps.length >= 3) {
    navigator.clipboard?.writeText(label).then(() => say('Location copied.'), () => say('Could not copy.'));
    f2Taps = [];
  } else if (f2Taps.length === 2) {
    say(spell(label));
  } else {
    say(label);
  }
}

// ── Keyboard ──────────────────────────────────────────────────────────────

const ARROWS = { ArrowUp: [0, 'north'], ArrowDown: [180, 'south'], ArrowRight: [90, 'east'], ArrowLeft: [270, 'west'] };

function onMapKey(e) {
  if (!state.started || document.querySelector('dialog[open]') || !mapOn()) return;
  const { key, shiftKey: shift, ctrlKey, metaKey, altKey: alt } = e;
  const ctrl = ctrlKey || metaKey;
  const handled = () => { e.preventDefault(); e.stopPropagation(); };

  // Ctrl+Home and Ctrl+End go to the top and bottom of the page, as screen
  // reader users expect, instead of being kept by the map.
  if (ctrl && !alt && (key === 'Home' || key === 'End')) {
    handled();
    const all = [...document.querySelectorAll('#app button, #app a[href], #app input, #app h2[tabindex]')]
      .filter((el) => el.offsetParent !== null && el.id !== 'map');
    const target = key === 'Home' ? all[0] : all[all.length - 1];
    if (target) target.focus();
    return;
  }
  // Escape leaves the map: with a screen reader the map takes every key, so
  // reading on past it with the arrow keys would move the map instead.
  if (key === 'Escape' && !ctrl && !alt && !shift) {
    handled();
    const next = [...document.querySelectorAll('#panel h2, #panel button, #panel input')]
      .find((el) => el.offsetParent !== null);
    if (next) {
      if (next.matches('h2') && !next.hasAttribute('tabindex')) next.tabIndex = -1;
      next.focus();
    }
    audio.stopAmbient();
    state.soundWaiting = true;
    return say('Left the map. Shift+Tab goes back to it.');
  }
  if (ARROWS[key]) {
    handled();
    const [bearing, name] = ARROWS[key];
    if (state.mode === 'street') {
      if (alt && !ctrl) return streetFollow(bearing, 3000);
      return streetMove(bearing, ctrl ? 100 : shift ? 5 : streetStepM());
    }
    if (alt && !ctrl) return nextCountry(bearing, name);
    return step(bearing, ctrl ? 100 : shift ? 1 : stepKm());
  }
  if (key === 'F11' || (key === 'Enter' && shift && !ctrl && !alt)) {
    handled();
    return toggleStreetMode();
  }

  const k = key.length === 1 ? key.toLowerCase() : key;
  const combo = `${ctrl ? 'Ctrl+' : ''}${alt ? 'Alt+' : ''}${shift && key.length > 1 ? 'Shift+' : ''}${k}`;
  const shiftLetter = shift && key.length === 1;
  const action = KEYS[shiftLetter ? `Shift+${combo}` : combo];
  if (action) {
    handled();
    action();
  }
}

const KEYS = {
  F1: () => openHelp(),
  F2: repeatLocation,
  F3: () => say(formatLat(state.lat)),
  F4: () => say(formatLon(state.lon)),
  'Shift+F3': () => say(`Volume ${Math.round(audio.setVolume(settings.get('volume') - 0.1) * 100)}%.`),
  'Shift+F4': () => say(`Volume ${Math.round(audio.setVolume(settings.get('volume') + 0.1) * 100)}%.`),
  F5: () => {
    const cont = continentFor(state.info.country);
    say(cont ? `${cont}.` : noCountry() ? 'No continent for open water.' : `Continent unknown for ${state.info.country}.`);
  },
  'Shift+F5': () => {
    settings.set('geoFeatures', !settings.get('geoFeatures'));
    refreshSettings();
    say(`GeoFeatures ${settings.get('geoFeatures') ? 'on' : 'off'}.`);
  },
  F6: announceFacts,
  i: announceFacts,
  'Shift+F6': () => !noCountry() && withNetwork('the Wikipedia summary', () => online.wikipediaSummary(canonical(state.info.country))),
  'Shift+i': () => (noCountry() ? say('No country to look up.') : withNetwork('the Wikipedia summary', () => online.wikipediaSummary(canonical(state.info.country)))),
  'Shift+F1': announceCapital,
  F7: () => {
    const on = !settings.get('sounds');
    audio.setSoundsEnabled(on);
    refreshSettings();
    if (on) audio.playPlace(state.info.onLand ? state.info.country : 'ocean', state.info.continent);
    say(`Sounds ${on ? 'on' : 'off'}.`);
  },
  F9: () => {
    settings.set('visualAssist', !settings.get('visualAssist'));
    applyVisualAssist();
    refreshSettings();
    say(`Visual Assist ${settings.get('visualAssist') ? 'on' : 'off'}.`);
  },
  'Ctrl+=': () => zoom(2),
  'Ctrl++': () => zoom(2),
  'Ctrl+-': () => zoom(0.5),
  'Ctrl+0': () => { settings.set('zoom', 1); drawMap(); say('Map zoom 1 times.'); },
  'Ctrl+,': () => openSettings(),
  PageUp: cycleToneScope,
  PageDown: cycleToneScope,
  j: () => openJump(),
  p: openPlaces,
  y: whereAmI,
  '-': () => setStepIndex(stepIndex() - 1),
  '=': () => setStepIndex(stepIndex() + 1),
  'Shift++': () => setStepIndex(stepIndex() + 1),
  'Ctrl+h': () => openJumpHistory(),
  l: () => say(`${formatLat(state.lat)}, ${formatLon(state.lon)}.`),
  c: () => say(noCountry() ? 'Open water.' : `${state.info.country}.`),
  'Shift+p': () => announcePostcode(),
  a: () => {
    if (state.mode !== 'street') return say('Addresses are in street mode. Press F11 first.');
    const ad = street.nearestAddress(state.lat, state.lon);
    if (!ad) return say('No house numbers in the map near here.');
    const where = ad.dist < 10 ? 'here' : `${formatDistance(Math.round(ad.dist / 5) * 5)} ${compassName(ad.bearing).replace('-', ' ')}`;
    say(`${ad.num} ${ad.street || ''}, ${where}.`.replace(' ,', ','));
  },
  r: () => say(state.info.admin ? `${state.info.admin}.` : noCountry() ? 'Open water.' : `No region found in ${state.info.country}.`),
  n: () => say(nearestCityLabel(state.lat, state.lon)),
  g: async () => {
    const items = await nearbyFeatures(state.lat, state.lon, state.info.iso2);
    say(items.length ? items.join('. ') : 'No geographic features nearby.');
  },
  'Shift+l': announceLanguages,
  $: announceCurrency,
  'Shift+$': announceCurrency,
  'Shift+4': announceCurrency,
  t: () => withNetwork('the local time', async () => {
    const tz = await online.timezoneInfo(state.lat, state.lon);
    return `${online.localTimeText(tz)}.`;
  }),
  z: () => withNetwork('the time zone', async () => {
    const tz = await online.timezoneInfo(state.lat, state.lon);
    const h = tz.offset / 3600;
    const sign = h >= 0 ? 'plus' : 'minus';
    return `${tz.zone.replaceAll('_', ' ')}, UTC ${sign} ${Math.abs(h)} hours.`;
  }),
  s: () => withNetwork('sunrise and sunset', () => online.sunTimes(state.lat, state.lon)),
  w: () => withNetwork('the weather', () => online.weather(state.lat, state.lon, settings.get('units') === 'imperial')),
  q: () => withNetwork('the air quality', () => online.airQuality(state.lat, state.lon)),
};

function zoom(factor) {
  const z = Math.max(1, Math.min(128, (settings.get('zoom') || 1) * factor));
  settings.set('zoom', z);
  drawMap();
  say(`Map zoom ${z} times.`);
}

function cycleToneScope() {
  const next = settings.get('toneScope') === 'world' ? 'country' : 'world';
  settings.set('toneScope', next);
  say(`Spatial tones: ${next}.`);
}

/**
 * Put keyboard focus on the map, on computers only, where the arrow keys move
 * it. On phones and tablets the map isn't something you operate, and sending
 * VoiceOver there kept pulling it away from the button you'd just used.
 */
function focusMap() {
  // The map is hidden behind a full-screen workflow: going to the map closes it.
  closeAllScreens();
  if (!steering.isTouchDevice()) $('map').focus();
}

/** Starter (the default) or Expert. */
/**
 * Expert is only offered at expert.mapinabox.org (and on this computer while
 * developing). Everywhere else, mapinabox.org included, is Starter only, with
 * no switch to change it: schools block the expert address in their web
 * filters, so students can't reach Expert at all.
 */
const EXPERT_SITE = /^expert\./.test(location.hostname) || ['localhost', '127.0.0.1'].includes(location.hostname);
const level = () => (EXPERT_SITE && settings.get('level') === 'expert' ? 'expert' : 'starter');

/** Map on or off (Expert): off hides the map and Move, and silences its sounds. Starter always has it on. */
const mapOn = () => level() !== 'expert' || settings.get('mapOn') !== false;

function applyMapOn() {
  const on = mapOn();
  document.body.classList.toggle('map-off', !on);
  $('set-map').checked = on;
  audio.setPaused(!on);
  if (!on) state.soundWaiting = true; // the country's sound comes back with the next move
}

/** Show or hide what only Expert has. */
/**
 * Parts for street mode only: shown in street mode. Places nearby
 * (data-anywhere) is shown on the world map too, so after jumping to a
 * place its food and other places are right there.
 */
function showStreetOnly() {
  const inStreet = state.mode === 'street';
  for (const el of document.querySelectorAll('[data-street-only]')) {
    el.hidden = !(inStreet || el.hasAttribute('data-anywhere'));
  }
}

function applyLevel() {
  document.documentElement.dataset.level = level();
  showStreetOnly();
  if ($('set-map')) applyMapOn();
  if (level() !== 'expert' && monitorActive()) stopMonitor();
}

/** The GPS monitor's panel: plain text to read at any time (not spoken by itself). */
function showMonitorStatus(s) {
  const put = (id, text) => { if ($(id).textContent !== text) $(id).textContent = text; };
  put('gps-street', s.street || 'not known yet');
  put('gps-suburb', s.suburb || 'not known yet');
  put('gps-last', s.lastSaid || 'nothing yet');
  put('gps-moving', s.moving);
  $('gps-accuracy').hidden = !s.accuracy;
  put('gps-accuracy', s.accuracy);
}

/** GPS monitor on or off (Expert). */
function toggleMonitor() {
  const btn = $('btn-gps');
  const show = () => {
    openScreen($('gps-panel'), { from: btn, close: () => { $('gps-panel').hidden = true; } });
    $('gps-title').focus();
  };
  // Running: the button shows it again (Stop GPS monitor is on its screen).
  if (monitorActive()) return show();
  btn.setAttribute('aria-pressed', 'true');
  show();
  startMonitor({
    // Keep the map on where you are, quietly.
    follow: (p) => {
      state.lat = p.lat;
      state.lon = p.lon;
      updateMapView(p.lat, p.lon, null, null);
      settings.set('position', { lat: p.lat, lon: p.lon });
    },
    stopped: () => {
      btn.setAttribute('aria-pressed', 'false');
      closeScreen($('gps-panel'));
      $('gps-panel').hidden = true;
    },
    status: showMonitorStatus,
  }, settings.get('gpsLevel') || 'suburbs');
}

/** Tick the chosen colours and text size. */
function showLookChoices() {
  const theme = settings.get('theme') || 'auto';
  const size = settings.get('textSize') || 'normal';
  for (const r of document.querySelectorAll('input[name="theme"]')) r.checked = r.value === theme;
  for (const r of document.querySelectorAll('input[name="textsize"]')) r.checked = r.value === size;
}

/** Colours and text size from Settings. */
function applyLook() {
  const theme = settings.get('theme') || 'auto';
  if (theme === 'auto') delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = theme;
  const size = settings.get('textSize') || 'normal';
  if (size === 'normal') delete document.documentElement.dataset.text;
  else document.documentElement.dataset.text = size;
  requestAnimationFrame(drawMap);
}

function applyVisualAssist() {
  const on = settings.get('visualAssist');
  document.body.classList.toggle('visual-assist', on);
  $('map-canvas').hidden = !on;
  if (on) requestAnimationFrame(drawMap);
}

// ── Jump dialog ───────────────────────────────────────────────────────────

let jumpResults = [];

function openJump(prompt = 'Jump to a place. Type a city, country or coordinates, then press Enter.') {
  const dlg = $('jump-dialog');
  $('jump-input').value = '';
  $('jump-prompt').textContent = prompt;
  setJumpResults([]);
  dlg.showModal();
  $('jump-input').focus();
}

function openJumpHistory() {
  const history = settings.get('jumpHistory') || [];
  if (!history.length) return say('No jump history yet.');
  openJump('Recent jumps. Choose one and press Enter.');
  setJumpResults(history);
  $('jump-results').focus();
}

let jumpPicker = null;

function setJumpResults(results) {
  jumpResults = results;
  state.jumpIdx = 0;
  $('jump-pick').hidden = !results.length;
  $('jump-go').hidden = !results.length;
  jumpPicker.refresh();
  closeJumpDetails();
}

/** Back to the list: Details shows again for whichever result you move to. */
function closeJumpDetails() {
  $('jump-details').hidden = !jumpResults.length;
  $('jump-details-box').hidden = true;
  $('jump-menu-box').hidden = true;
  $('jump-menu-box').replaceChildren();
}

/**
 * Jump's Details: everything known about the result you're on, without
 * moving the map: its address, where exactly it is (corner, or the cross
 * streets either side), and its phone, hours and website when it's a place.
 */
async function jumpDetails() {
  const r = jumpResults[state.jumpIdx || 0];
  if (!r || r.online || !Number.isFinite(r.lat)) return say('Choose a result first.');
  const name = String(r.label).split(', ')[0];
  const parts = [];
  let d = null;
  // A named place (not just an address or a town): look up its phone, hours and website.
  if (!/^\d/.test(name)) d = await online.placeDetails(name, r.lat, r.lon).catch(() => null);
  if (jumpResults[state.jumpIdx || 0] !== r) return;
  let addressStreet = '';
  if (d && d.address) {
    parts.push(`Address: ${d.address}`);
    addressStreet = (d.address.match(/^\s*[\dA-Za-z/-]*\d[\dA-Za-z/-]*\s+([^,]+)/) || [])[1] || '';
  } else {
    const ad = await online.addressAt(r.lat, r.lon).catch(() => null);
    if (ad && ad.street) {
      parts.push(`Address: ${[ad.number ? `${ad.number} ${ad.street}` : ad.street, ad.place, ad.postcode].filter(Boolean).join(', ')}`);
      addressStreet = ad.street;
    }
  }
  const where = await describeLocation(r.lat, r.lon, addressStreet).catch(() => '');
  if (jumpResults[state.jumpIdx || 0] !== r) return;
  if (where) parts.push(`${where[0].toUpperCase()}${where.slice(1)}`);
  if (d && d.phone) parts.push(`Phone ${d.phone}`);
  if (d && d.hours) parts.push(`Hours: ${speakableHours(d.hours)}`);
  else if (d && d.hoursText) parts.push(`Hours: ${d.hoursText}`);
  if (d && d.cuisine) parts.push(`Cuisine: ${d.cuisine}`);
  const text = `${name}. ${parts.length ? `${parts.join('. ')}.` : 'Nothing more is known about it.'}`;
  $('jump-details-text').textContent = text;
  $('jump-call').hidden = !(d && d.phone);
  if (d && d.phone) {
    $('jump-call').href = `tel:${d.phone.split(';')[0].replace(/[^+\d]/g, '')}`;
    $('jump-call').textContent = `Call ${name}`;
  }
  $('jump-web').hidden = !(d && d.website);
  if (d && d.website) {
    $('jump-web').href = d.website.split(';')[0];
    $('jump-web').textContent = `${name} website`;
  }
  // Uber's universal link (the app if installed, else the website), picking you up where you are.
  const label = String(r.label).split(', ').slice(0, 2).join(', ');
  $('jump-uber').href = `https://m.uber.com/ul/?${new URLSearchParams({
    action: 'setPickup',
    pickup: 'my_location',
    'dropoff[latitude]': r.lat.toFixed(6),
    'dropoff[longitude]': r.lon.toFixed(6),
    'dropoff[nickname]': label,
  })}`;
  $('jump-uber').textContent = `Book an Uber to ${name}`;
  // Food places: their menu, as in Places nearby.
  $('jump-menu').hidden = !(r.food || (d && d.cuisine));
  $('jump-menu').textContent = `Menu for ${name}`;
  $('jump-details').hidden = true; // you're in Details now
  $('jump-details-box').hidden = false;
  $('jump-details-text').tabIndex = -1;
  $('jump-details-text').focus();
}

/** Where to rank online matches from: where you are in Expert, else the map. */
async function jumpNear() {
  if (level() === 'expert') {
    const p = await Promise.race([devicePosition().catch(() => null), new Promise((r) => setTimeout(() => r(null), 2500))]);
    if (p) return p;
  }
  return state;
}

async function runJumpSearch() {
  const text = $('jump-input').value.trim();
  if (!text) return;
  const coords = parseCoordinates(text);
  if (coords) return chooseJump({ label: `${coords.lat}, ${coords.lon}`, ...coords });
  let results = searchLocal(text);
  let local = true;
  if (!results.length && text.length >= 4) {
    say('No local match. Searching online…');
    local = false;
    try {
      results = await online.searchOnline(text, await jumpNear());
    } catch {
      return say('Online search failed. Check the internet connection and try again.');
    }
  }
  if (!results.length) return say(`${text} not found.`);
  // One match goes straight there; in Expert it's listed, so Details can be asked first.
  if (results.length === 1 && level() !== 'expert') return chooseJump(results[0]);
  const count = results.length;
  if (local && text.length >= 4) results.push({ label: 'Search online', online: true });
  setJumpResults(results);
  $('jump-results').focus();
  say(`${count} ${local ? '' : 'online '}results. ${results[0].label}.`);
}

async function chooseJump(r) {
  if (r.online) {
    say('Searching online…');
    try {
      const more = await online.searchOnline($('jump-input').value.trim(), await jumpNear());
      if (!more.length) return say('Nothing found online.');
      setJumpResults(more);
      $('jump-results').focus();
      return say(`${more.length} online results. ${more[0].label}.`);
    } catch {
      return say('Online search failed.');
    }
  }
  $('jump-dialog').close();
  const history = (settings.get('jumpHistory') || []).filter((h) => h.label !== r.label);
  history.unshift({ label: r.label, country: r.country, lat: r.lat, lon: r.lon });
  settings.set('jumpHistory', history.slice(0, 5));
  focusMap();
  jumpTo(r.lat, r.lon, r.country ? `${r.country}.` : '');
}

// ── Help and settings ─────────────────────────────────────────────────────

function openHelp() {
  $('help-dialog').showModal();
  $('help-dialog').querySelector('h2').focus();
}

/** Ctrl+Comma or the Settings button: go to the Settings section. */
function openSettings() {
  setExpanded($('settings-title'), true);
}

/** Information and Settings: each opens on a screen of its own, with Back. */
function setExpanded(btn, open) {
  const section = $(btn.getAttribute('aria-controls'));
  btn.setAttribute('aria-expanded', String(open));
  if (!open) {
    closeScreen(section);
    section.hidden = true;
    return;
  }
  section.hidden = false;
  openScreen(section, {
    from: btn,
    close: () => {
      btn.setAttribute('aria-expanded', 'false');
      section.hidden = true;
    },
  });
  section.querySelector('h2').focus();
}
for (const btn of document.querySelectorAll('.expander')) {
  btn.addEventListener('click', () => setExpanded(btn, btn.getAttribute('aria-expanded') !== 'true'));
}

const pickers = [];

function setUpPickers() {
  tiltMovePicker = makePicker($('tilt-move'), {
    shown: $('tilt-move-value'),
    items: () => TILT_MOVES[state.mode].map(([value, label]) => ({ value, label: label() })),
    get: tiltMove,
    set: (v) => settings.set(state.mode === 'street' ? 'tiltMoveStreet' : 'tiltMove', v),
  });
  makePicker($('places-category'), {
    shown: $('places-category-value'),
    items: () => [['all', 'All places'], ...street.PLACE_CATEGORIES].map(([value, label]) => ({ value, label })),
    get: placeCategory,
    set: (v) => {
      settings.set('placeCategory', v);
      // A list is showing: show this category instead, once the picker has settled,
      // so swiping through categories doesn't talk over itself.
      clearTimeout(categoryTimer);
      if (!$('places-pick').hidden) categoryTimer = setTimeout(() => listPlaces(false), 900);
    },
  });
  jumpPicker = makePicker($('jump-results'), {
    browse: true,
    shown: $('jump-results-value'),
    items: () => jumpResults.map((r, i) => ({ value: i, label: `${r.label}. ${i + 1} of ${jumpResults.length}` })),
    get: () => state.jumpIdx || 0,
    set: (i) => { state.jumpIdx = i; closeJumpDetails(); },
  });
  pickers.push(makePicker($('set-gps'), {
    shown: $('set-gps-value'),
    items: () => [
      { value: 'suburbs', label: 'New streets, cross streets and suburbs' },
      { value: 'cross', label: 'New streets and cross streets' },
      { value: 'streets', label: 'New streets only' },
    ],
    get: () => settings.get('gpsLevel') || 'suburbs',
    set: (v) => { settings.set('gpsLevel', v); setMonitorLevel(v); },
  }));
  // Colours and text size restyle the whole page, so they're radio buttons:
  // on a slider VoiceOver lost its place when the page changed under it.
  for (const r of document.querySelectorAll('input[name="theme"]')) {
    r.addEventListener('change', () => { settings.set('theme', r.value); applyLook(); });
  }
  for (const r of document.querySelectorAll('input[name="textsize"]')) {
    r.addEventListener('change', () => { settings.set('textSize', r.value); applyLook(); });
  }
  showLookChoices();
  pickers.push(makePicker($('set-speech'), {
    shown: $('set-speech-value'),
    items: () => [
      { value: 'screenreader', label: 'My screen reader (VoiceOver, TalkBack, NVDA, JAWS)' },
      { value: 'voice', label: 'The built-in voice, for use without a screen reader' },
      { value: 'both', label: 'Both' },
    ],
    get: () => settings.get('speech'),
    set: (v) => settings.set('speech', v),
  }));
  pickers.push(makePicker($('set-callouts'), {
    shown: $('set-callouts-value'),
    items: () => [
      ['food', 'Food and drink'], ['all', 'All places'], ['none', 'None'],
      ...street.PLACE_CATEGORIES.filter(([k]) => k !== 'food'),
    ].map(([value, label]) => ({ value, label })),
    get: () => settings.get('walkCallouts') || 'food',
    set: (v) => settings.set('walkCallouts', v),
  }));
  // Two-way choices are switches: VoiceOver adjusts a two-step slider unreliably.
  $('set-units').addEventListener('change', (e) => { settings.set('units', e.target.checked ? 'imperial' : 'metric'); showStep(); });
  $('set-street-steer').addEventListener('change', (e) => {
    settings.set('streetSteer', e.target.checked ? 'free' : 'pick');
    state.corner = null;
    showStreetSteer();
  });
  $('set-units').checked = settings.get('units') === 'imperial';
  $('set-sounds').checked = settings.get('sounds');
  $('set-geo').checked = settings.get('geoFeatures');
  $('set-visual').checked = settings.get('visualAssist');
  $('set-venue').checked = settings.get('venueAsk') !== false;
  $('set-venue').addEventListener('change', (e) => settings.set('venueAsk', e.target.checked));
  $('set-postcode').checked = settings.get('autoPostcode') !== false;
  $('set-postcode').addEventListener('change', (e) => settings.set('autoPostcode', e.target.checked));
  $('set-sounds').addEventListener('change', (e) => audio.setSoundsEnabled(e.target.checked));
  $('set-geo').addEventListener('change', (e) => settings.set('geoFeatures', e.target.checked));
  $('set-visual').addEventListener('change', (e) => {
    settings.set('visualAssist', e.target.checked);
    applyVisualAssist();
  });
}

/** Keep the Settings section in step when a key or button changes a setting. */
function refreshSettings() {
  for (const pk of pickers) pk.refresh();
  showLookChoices();
  $('set-units').checked = settings.get('units') === 'imperial';
  $('set-street-steer').checked = streetSteerMode() === 'free';
  $('set-sounds').checked = settings.get('sounds');
  $('set-geo').checked = settings.get('geoFeatures');
  $('set-visual').checked = settings.get('visualAssist');
}

// ── Wiring ────────────────────────────────────────────────────────────────

$('map').addEventListener('keydown', onMapKey);
$('jump-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { e.preventDefault(); runJumpSearch(); }
});
setUpPickers();

$('jump-results').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') {
    e.preventDefault();
    chooseSelected();
  }
});
function chooseSelected() {
  const r = jumpResults[state.jumpIdx || 0];
  if (r) chooseJump(r);
}
$('jump-go').addEventListener('click', chooseSelected);
onSlowClick($('jump-details'), 'Looking it up', jumpDetails);
$('jump-menu').addEventListener('click', () => {
  const r = jumpResults[state.jumpIdx || 0];
  if (!r) return;
  const website = $('jump-web').hidden ? '' : $('jump-web').href;
  showMenu($('jump-menu-box'), { name: String(r.label).split(', ')[0], lat: r.lat, lon: r.lon, website });
});
// Walk there / Drive there: the Navigation section takes over from Details.
for (const [id, go] of [['jump-walk', walkTo], ['jump-drive', driveTo]]) {
  $(id).addEventListener('click', () => {
    const r = jumpResults[state.jumpIdx || 0];
    if (!r || !Number.isFinite(r.lat)) return;
    state.handOff = true;
    $('jump-dialog').close();
    go({ lat: r.lat, lon: r.lon, label: String(r.label).split(', ').slice(0, 2).join(', ') });
  });
}
$('places-go').addEventListener('click', choosePlace);
$('places-details').addEventListener('click', placeDetails);
$('places-menu').addEventListener('click', () => {
  const p = placeResults[state.placeIdx || 0];
  if (!p) return;
  placeSubFrom = $('places-menu');
  placeView('menu');
  showMenu($('place-menu'), { name: p.name, lat: p.lat, lon: p.lon, website: p.details && p.details.website });
});
$('place-back').addEventListener('click', placeBack);
// Back to the list, onto the place you chose (Escape closes the screen too).
$('place-screen').addEventListener('close', () => {
  if (placeOpener && placeOpener.isConnected) placeOpener.focus();
});
onSlowClick($('places-find'), 'Finding places', () => (state.started ? openPlaces() : null));
for (const id of ['jump-dialog', 'help-dialog', 'venue-dialog']) {
  $(id).addEventListener('close', () => {
    // Walk there / Drive there move focus to the Navigation section themselves.
    if (state.handOff) { state.handOff = false; return; }
    if (state.started) focusMap();
  });
}
for (const btn of document.querySelectorAll('[data-close]')) {
  btn.addEventListener('click', () => btn.closest('dialog').close());
}
$('btn-jump').addEventListener('click', () => openJump());
$('btn-gps').addEventListener('click', toggleMonitor);
$('set-map').addEventListener('change', (e) => {
  settings.set('mapOn', e.target.checked);
  applyMapOn();
  say(e.target.checked ? 'Map on.' : 'Map off. Its sounds are off until you turn it back on.');
});
applyMapOn();
$('gps-stop').addEventListener('click', () => { stopMonitor(); $('btn-gps').focus(); });
$('gps-repeat').addEventListener('click', () => {
  const s = monitorStatus();
  say([s.street && `On ${s.street}`, s.suburb, s.moving].filter(Boolean).join(', ') + '.');
});
// Starter or Expert: a switch, which VoiceOver reads as "Expert mode, switch, off".
// Starter-only site: no Expert switch at all.
if (!EXPERT_SITE) {
  $('set-expert').disabled = true;
  $('set-expert').closest('label').hidden = true;
}
$('set-expert').checked = level() === 'expert';
$('set-expert').addEventListener('change', (e) => {
  settings.set('level', e.target.checked ? 'expert' : 'starter');
  applyLevel();
});
$('places-menus').addEventListener('click', () => { if (state.started) openMenusNearby(state.lat, state.lon); });
$('btn-help').addEventListener('click', openHelp);
$('btn-settings').addEventListener('click', openSettings);
$('btn-steer').addEventListener('click', toggleSteering);
$('btn-practice').addEventListener('click', () => {
  if (steering.steeringActive()) startPractice();
});
$('btn-street-steer').addEventListener('click', toggleStreetSteer);
$('btn-street').addEventListener('click', () => { if (state.started) toggleStreetMode(); });
showStep();
showTiltMove();
// The slider reports its own new value (aria-valuetext), so it doesn't speak as well.
for (const btn of document.querySelectorAll('#panel [data-key]')) {
  btn.addEventListener('click', () => { if (state.started) KEYS[btn.dataset.key]?.(); });
}
for (const btn of document.querySelectorAll('#panel [data-move]')) {
  btn.addEventListener('click', () => {
    if (!state.started) return;
    const bearing = Number(btn.dataset.move);
    if (state.mode === 'street') streetMove(bearing, streetStepM());
    else step(bearing, stepKm());
  });
}
$('btn-where').addEventListener('click', whereAmI);
$('btn-position').addEventListener('click', () => { if (state.started) repeatLocation(); });
initJourney({
  getHere: () => ({ lat: state.lat, lon: state.lon, label: state.streetLabel || state.info.label || state.info.city }),
  // Where the device is, for ranking place matches nearest you first; null if
  // location is off or slow (then the map position is used).
  nearMe: () => Promise.race([
    devicePosition().catch(() => null),
    new Promise((r) => setTimeout(() => r(null), 4000)),
  ]),
  // Where the device really is, with a short street address for the From box.
  myLocation: async () => {
    const { lat, lon } = await devicePosition();
    const ph = await online.addressAt(lat, lon).catch(() => null);
    const streetPart = ph && ph.street ? (ph.number ? `${ph.number} ${ph.street}` : ph.street) : '';
    const label = [streetPart, ph && ph.place].filter(Boolean).join(', ') || `${lat.toFixed(5)}, ${lon.toFixed(5)}`;
    return { lat, lon, label };
  },
  // Live walking guidance: keep the map on where you really are, quietly.
  follow: (p) => {
    state.lat = p.lat;
    state.lon = p.lon;
    updateMapView(p.lat, p.lon, null, null);
    settings.set('position', { lat: p.lat, lon: p.lon });
  },
  // A food place on the journey: go there, in street mode if it is on.
  goTo: (p) => {
    focusMap();
    const prefix = `${p.name}, ${p.kind}.`;
    if (state.mode === 'street') {
      state.lat = p.lat;
      state.lon = p.lon;
      state.streetName = '';
      movedStreet({ prefix });
    } else {
      jumpTo(p.lat, p.lon, prefix);
    }
  },
});
$('jump-here').addEventListener('click', () => goToMyLocation());
// Keys pressed while focus is on the toolbar still drive the map.
document.addEventListener('keydown', (e) => {
  if (e.target === $('map') || document.querySelector('dialog[open]')) return;
  if (e.target.closest && e.target.closest('#toolbar') && (ARROWS[e.key] || /^F\d+$/.test(e.key))) onMapKey(e);
});

// Exported for the browser console while testing.
window.miab = { state, describe, nearestCity, city, bearingDeg, compassName, lastSpoken, setSteering, street, steering, toggleStreetMode, streetFollow };

boot();
