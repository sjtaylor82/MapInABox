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

const $ = (id) => document.getElementById(id);

const state = {
  lat: -27.47,
  lon: 153.03,
  info: { country: '', admin: '', city: '', iso2: '', continent: '', onLand: false, label: '' },
  seq: 0,
  started: false,
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
  applyVisualAssist();
  // Phones and tablets: offer steering, and the browser's voice for people without a screen reader.
  const touch = steering.isTouchDevice() && steering.steeringSupported();
  $('intro-steer-row').hidden = !touch;
  $('intro-steer').checked = touch && settings.get('steering') !== false;
  const hasSaved = !!(settings.get('position') && Number.isFinite(settings.get('position').lat));
  state.startWhere = hasSaved ? 'last' : touch ? 'here' : 'choose';
  makePicker($('intro-speech'), {
    shown: $('intro-speech-value'),
    items: () => [
      { value: 'screenreader', label: 'My screen reader is talking' },
      { value: 'voice', label: "Use the browser's voice (no screen reader)" },
      { value: 'both', label: 'Both' },
    ],
    get: () => settings.get('speech'),
    set: (v) => settings.set('speech', v),
  });
  makePicker($('intro-where'), {
    shown: $('intro-where-value'),
    items: () => [
      ...(hasSaved ? [{ value: 'last', label: 'Where I left off' }] : []),
      { value: 'here', label: 'My current location' },
      { value: 'choose', label: 'A place I choose' },
    ],
    get: () => state.startWhere,
    set: (v) => { state.startWhere = v; },
  });
  $('btn-steer').hidden = !touch;
  $('start').addEventListener('click', () => start());
  $('start-last').hidden = !hasSaved;
  $('start-last').addEventListener('click', () => start('last'));
  $('start-here').addEventListener('click', () => start('here'));
  $('start-choose').addEventListener('click', () => start('choose'));
  $('start-options').addEventListener('click', () => {
    $('start-dialog').close();
    $('intro-speech').focus();
  });
  // Closed with Escape: the start page has the same choices and a Start button.
  $('start-dialog').addEventListener('cancel', () => setTimeout(() => $('start').focus(), 0));
  try {
    await loadWorld((msg) => { $('load-status').textContent = `${msg}…`; });
    audio.configureAudio(data.sounds, data.tables);
    $('load-status').textContent = 'Ready.';
    $('start').disabled = false;
    // Ask straight away where to start; the answer is also the tap that
    // lets iPhone play sound, speak and use motion.
    $('start-dialog').showModal();
    $('start-dialog').querySelector('button:not([hidden])').focus();
  } catch (err) {
    $('load-status').textContent = `Map in a Box could not load its map data. ${err.message}`;
  }
}

async function start(choice) {
  if (state.started) return;
  if (choice) state.startWhere = choice;
  if ($('start-dialog').open) $('start-dialog').close();
  // Both must run straight from the tap: iPhone refuses them otherwise.
  const wantSteer = !$('intro-steer-row').hidden && $('intro-steer').checked;
  const steerAllowed = wantSteer ? steering.requestSteeringPermission() : Promise.resolve(false);
  audio.unlock();
  unlockVoice();
  state.started = true;
  $('intro').hidden = true;
  $('app').hidden = false;
  $('map').focus();
  drawMap();
  if (wantSteer) {
    // Don't hold up the start waiting for the permission answer.
    steerAllowed.then((ok) => {
      if (ok) setSteering(true);
      else say('Steering is not available. Motion access was not allowed.');
    });
  } else {
    settings.set('steering', false);
  }
  const where = state.startWhere;
  const saved = settings.get('position');
  if (where === 'here') {
    // If the location can't be found, ask where to start instead of leaving the map nowhere.
    whereAmI(() => setTimeout(() => openJump('Where would you like to start? Type a city, country or coordinates, then press Enter.'), 2500));
  } else if (where === 'last' && saved && Number.isFinite(saved.lat)) {
    state.lat = saved.lat;
    state.lon = saved.lon;
    await moved({ tone: false });
  } else {
    openJump('Where would you like to start? Type a city, country or coordinates, then press Enter.');
  }
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
  say(prefix ? `${prefix} ${text}` : text);
  updateMapView(state.lat, state.lon, info.label, info.country);
  if (countryChanged) {
    const place = info.onLand && info.country !== 'Open Water' ? info.country : 'ocean';
    audio.playPlace(place, info.continent);
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
    say(bearing === 0 ? 'North Pole.' : 'South Pole.');
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

// ── Where am I (device location) ──────────────────────────────────────────

let locating = false;

function whereAmI(onFail = () => {}) {
  if (typeof onFail !== 'function') onFail = () => {}; // called from a click: the event
  if (locating) return;
  if (!('geolocation' in navigator) || !window.isSecureContext) {
    say('Location is not available in this browser.');
    onFail();
    return;
  }
  locating = true;
  say('Finding your location…');
  navigator.geolocation.getCurrentPosition(
    (pos) => {
      locating = false;
      const { latitude, longitude, accuracy } = pos.coords;
      const near = accuracy <= 1000
        ? `accurate to about ${formatDistance(accuracy)}`
        : 'only roughly, perhaps from your internet connection';
      if (document.querySelector('dialog[open]')) document.querySelector('dialog[open]').close();
      jumpTo(latitude, longitude, `You are here, ${near}.`);
    },
    (err) => {
      locating = false;
      if (err.code === err.PERMISSION_DENIED) {
        say('Location access was not allowed. To use Where am I, allow location for this site in your browser settings.');
      } else if (err.code === err.TIMEOUT) {
        say('Finding your location took too long. Try again, ideally outdoors or near a window.');
      } else {
        say('Your location could not be found. Check that location services are on for this device.');
      }
      onFail();
    },
    { enableHighAccuracy: true, timeout: 20000, maximumAge: 30000 },
  );
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
  movedStreet({ prefix: 'Street mode.' });
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
  $('map').setAttribute('aria-roledescription', inStreet ? 'street map' : 'world map');
  $('map').setAttribute('aria-label', inStreet
    ? 'Street map. Use the arrow keys to move. Press P for places nearby, F11 for the world map.'
    : 'World map. Use the arrow keys to move. Press F11 for street mode, F1 for help.');
  for (const el of document.querySelectorAll('[data-street-only]')) el.hidden = !inStreet;
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
    const result = await waitForArea(lat, lon, seq);
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
  say([prefix, note, label.endsWith('.') ? label : `${label}.`, suffix].filter(Boolean).join(' '));
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
let placesPicker = null;

/** P, or Find places: list the places around you in the Places nearby picker. */
function openPlaces() {
  if (state.mode !== 'street') return say('Places nearby are in street mode. Press F11 first.');
  if (!street.areaReady(state.lat, state.lon)) return say('The streets are still loading.');
  let radius = 300;
  let found = street.placesNear(state.lat, state.lon, radius);
  if (found.length < 5) {
    radius = 1000;
    found = street.placesNear(state.lat, state.lon, radius);
  }
  if (!found.length) {
    $('places-pick').hidden = true;
    $('places-prompt').textContent = `No places found within ${formatDistance(radius)}.`;
    return say(`No places found within ${formatDistance(radius)}.`);
  }
  placeResults = found;
  state.placeIdx = 0;
  const how = found.length >= 60 ? `The ${found.length} nearest places` : `${found.length} places within ${formatDistance(radius)}`;
  $('places-prompt').textContent = `${how}, nearest first. Swipe up or down on Place to hear them, then choose Go there.`;
  $('places-pick').hidden = false;
  placesPicker.refresh();
  $('places-list').focus();
  say(`${how}. ${street.placeText(found[0])}.`);
}

function choosePlace() {
  const p = placeResults[state.placeIdx];
  if (!p) return;
  $('places-pick').hidden = true;
  $('places-prompt').textContent = 'Find places lists the places around you, nearest first.';
  $('map').focus();
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
      : 'Ready. Tip the phone well over, more than 60 degrees, towards the way you want to go, and lay it flat to stop.');
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

function showSteerStatus() {
  const el = $('steer-status');
  const st = steering.steeringStatus();
  const off = st.mode === 'off';
  el.hidden = off;
  for (const e of document.querySelectorAll('[data-steer-only]')) e.hidden = off;
  if (off) return;
  if (st.mode !== 'ready') el.textContent = 'Steering paused: lay the phone flat, screen up.';
  else if (st.direction != null) el.textContent = `Steering: tilted ${directionName(st.direction)}.`;
  else el.textContent = 'Steering: flat and still. Tilt to move.';
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

function showStep() {
  const slider = $('step-size');
  const inStreet = state.mode === 'street';
  const text = inStreet ? streetStepText(streetStepM()) : stepText(stepKm());
  slider.value = String(inStreet ? STREET_STEPS.indexOf(streetStepM()) : STEP_SIZES.indexOf(stepKm()));
  slider.setAttribute('aria-valuetext', text);
  $('step-value').textContent = text;
  showTiltMove(); // its "One step" label shows the size
  $('step-hint').textContent = inStreet
    ? 'How far each arrow key or steering step moves in street mode.'
    : 'How far each arrow key or steering step moves on land. Smaller steps find more suburbs.';
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
  $('btn-steer').textContent = on ? 'Steering: on' : 'Steering: off';
  $('btn-steer').setAttribute('aria-pressed', String(on));
  showSteerStatus();
  if (!on) return say(byShake ? 'Steering off. Shake again to turn it back on.' : 'Steering off.');
  steerIntroduced = false;
  say(byShake ? 'Steering on. Lay the phone flat, screen up.' : 'Steering on. Lay the phone flat, screen up. Shake the phone to turn steering off.');
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
  if (!state.started || document.querySelector('dialog[open]')) return;
  const { key, shiftKey: shift, ctrlKey, metaKey, altKey: alt } = e;
  const ctrl = ctrlKey || metaKey;
  const handled = () => { e.preventDefault(); e.stopPropagation(); };

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
  'Ctrl+Home': () => jumpTo(90, state.lon, 'North Pole.'),
  'Ctrl+End': () => jumpTo(-90, state.lon, 'South Pole.'),
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
      results = await online.searchOnline(text, state);
    } catch {
      return say('Online search failed. Check the internet connection and try again.');
    }
  }
  if (!results.length) return say(`${text} not found.`);
  if (results.length === 1) return chooseJump(results[0]);
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
      const more = await online.searchOnline($('jump-input').value.trim(), state);
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
  $('map').focus();
  jumpTo(r.lat, r.lon, r.country ? `${r.country}.` : '');
}

// ── Help and settings ─────────────────────────────────────────────────────

function openHelp() {
  $('help-dialog').showModal();
  $('help-dialog').querySelector('h2').focus();
}

/** Ctrl+Comma or the Settings button: go to the Settings section. */
function openSettings() {
  const h = $('settings-title');
  h.scrollIntoView({ block: 'start' });
  h.focus();
}

const pickers = [];

function setUpPickers() {
  tiltMovePicker = makePicker($('tilt-move'), {
    shown: $('tilt-move-value'),
    items: () => TILT_MOVES[state.mode].map(([value, label]) => ({ value, label: label() })),
    get: tiltMove,
    set: (v) => settings.set(state.mode === 'street' ? 'tiltMoveStreet' : 'tiltMove', v),
  });
  placesPicker = makePicker($('places-list'), {
    shown: $('places-value'),
    items: () => placeResults.map((p, i) => ({ value: i, label: `${street.placeText(p)}. ${i + 1} of ${placeResults.length}` })),
    get: () => state.placeIdx || 0,
    set: (i) => { state.placeIdx = i; },
  });
  jumpPicker = makePicker($('jump-results'), {
    shown: $('jump-results-value'),
    items: () => jumpResults.map((r, i) => ({ value: i, label: `${r.label}. ${i + 1} of ${jumpResults.length}` })),
    get: () => state.jumpIdx || 0,
    set: (i) => { state.jumpIdx = i; },
  });
  pickers.push(makePicker($('set-units'), {
    shown: $('set-units-value'),
    items: () => [{ value: 'metric', label: 'Metres and kilometres' }, { value: 'imperial', label: 'Feet and miles' }],
    get: () => settings.get('units'),
    set: (v) => { settings.set('units', v); showStep(); },
  }));
  pickers.push(makePicker($('set-speech'), {
    shown: $('set-speech-value'),
    items: () => [
      { value: 'screenreader', label: 'My screen reader' },
      { value: 'voice', label: "The browser's voice" },
      { value: 'both', label: 'Both' },
    ],
    get: () => settings.get('speech'),
    set: (v) => settings.set('speech', v),
  }));
  pickers.push(makePicker($('set-street-steer'), {
    shown: $('set-street-steer-value'),
    items: () => [
      { value: 'pick', label: 'Pick turns: follow the street and stop at each corner' },
      { value: 'free', label: 'Free: go straight the way I tilt, across blocks too' },
    ],
    get: streetSteerMode,
    set: (v) => { settings.set('streetSteer', v); state.corner = null; showStreetSteer(); },
  }));
  $('set-sounds').checked = settings.get('sounds');
  $('set-geo').checked = settings.get('geoFeatures');
  $('set-visual').checked = settings.get('visualAssist');
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
$('places-list').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { e.preventDefault(); choosePlace(); }
});
$('places-go').addEventListener('click', choosePlace);
$('places-find').addEventListener('click', () => { if (state.started) openPlaces(); });
for (const id of ['jump-dialog', 'help-dialog']) {
  $(id).addEventListener('close', () => { if (state.started) $('map').focus(); });
}
for (const btn of document.querySelectorAll('[data-close]')) {
  btn.addEventListener('click', () => btn.closest('dialog').close());
}
$('btn-jump').addEventListener('click', () => openJump());
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
$('step-size').addEventListener('input', (e) => setStepIndex(Number(e.target.value), false));
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
$('jump-here').addEventListener('click', whereAmI);
// Keys pressed while focus is on the toolbar still drive the map.
document.addEventListener('keydown', (e) => {
  if (e.target === $('map') || document.querySelector('dialog[open]')) return;
  if (e.target.closest && e.target.closest('#toolbar') && (ARROWS[e.key] || /^F\d+$/.test(e.key))) onMapKey(e);
});

// Exported for the browser console while testing.
window.miab = { state, describe, nearestCity, city, bearingDeg, compassName, lastSpoken, setSteering, street, steering, toggleStreetMode, streetFollow };

boot();
