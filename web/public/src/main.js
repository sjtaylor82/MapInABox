// Map in a Box Web: world map and street mode.

import { settings } from './settings.js';
import { initSpeech, say, spell, lastSpoken } from './speech.js';
import * as audio from './audio.js';
import {
  data, loadWorld, describe, countryAt, countryByName, factsFor, continentFor, canonical,
  nearestCity, city, nearestCityLabel, nearbyFeatures, searchLocal, parseCoordinates,
} from './world.js';
import {
  formatDistance, formatLat, formatLon, offsetKm, bearingDeg, compassName,
} from './geo.js';
import * as online from './online.js';
import { initMapView, updateMapView, draw as drawMap, setStreetDrawer } from './mapview.js';
import * as steering from './steering.js';
import * as street from './street.js';

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
  $('intro-speech').value = settings.get('speech');
  const hasSaved = !!(settings.get('position') && Number.isFinite(settings.get('position').lat));
  $('intro-where').querySelector('[value="last"]').hidden = !hasSaved;
  $('intro-where').value = hasSaved ? 'last' : touch ? 'here' : 'choose';
  $('btn-steer').hidden = !touch;
  $('start').addEventListener('click', start);
  try {
    await loadWorld((msg) => { $('load-status').textContent = `${msg}…`; });
    audio.configureAudio(data.sounds, data.tables);
    $('load-status').textContent = 'Ready.';
    $('start').disabled = false;
    $('start').focus();
  } catch (err) {
    $('load-status').textContent = `Map in a Box could not load its map data. ${err.message}`;
  }
}

async function start() {
  // Both must run straight from the tap: iPhone refuses them otherwise.
  const wantSteer = !$('intro-steer-row').hidden && $('intro-steer').checked;
  const steerAllowed = wantSteer ? steering.requestSteeringPermission() : Promise.resolve(false);
  settings.set('speech', $('intro-speech').value);
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
  const where = $('intro-where').value;
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
  if (tone) audio.spatialTone(state.lat, state.lon, toneBounds());
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
  showStreetSteer();
  setStreetDrawer(inStreet ? street.drawStreets : null);
  showStep();
}

async function movedStreet({ tone = true, prefix = '', note = '', suffix = '', keepCorner = false } = {}) {
  const seq = ++state.seq;
  if (!keepCorner) state.corner = null;
  const { lat, lon } = state;
  settings.set('position', { lat, lon });
  if (tone) audio.spatialTone(lat, lon, toneBounds());
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
    if (seq === state.seq) say('Still loading streets. The first visit to an area can take up to a minute.');
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
  state.corner = { options, idx: nearestOption(options, heading), arrival, armed: false };
  movedStreet({
    note,
    keepCorner: true,
    suffix: `A corner with ${options.length} ways. Come back to still, then tilt towards the way you want.`,
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

function openPlaces() {
  if (state.mode !== 'street') return say('Places nearby are in street mode. Press F11 first.');
  if (!street.areaReady(state.lat, state.lon)) return say('The streets are still loading.');
  let radius = 300;
  let found = street.placesNear(state.lat, state.lon, radius);
  if (found.length < 5) {
    radius = 1000;
    found = street.placesNear(state.lat, state.lon, radius);
  }
  if (!found.length) return say(`No places found within ${formatDistance(radius)}.`);
  placeResults = found;
  const list = $('places-list');
  list.replaceChildren(...found.map((p, i) => new Option(street.placeText(p), String(i))));
  const how = found.length >= 60 ? `The ${found.length} nearest places` : `${found.length} places within ${formatDistance(radius)}`;
  $('places-prompt').textContent = `${how}, nearest first. Choose one to go there.`;
  if (steering.isTouchDevice()) {
    // A phone shows the list as a picker with the first entry chosen, so a prompt goes first.
    const prompt = new Option(`${found.length} places. Choose one…`, '');
    prompt.disabled = true;
    list.prepend(prompt);
  }
  list.selectedIndex = 0;
  $('places-dialog').showModal();
  list.focus();
}

function choosePlace() {
  const value = $('places-list').value;
  const p = value === '' ? null : placeResults[Number(value)];
  if (!p) return;
  $('places-dialog').close();
  state.lat = p.lat;
  state.lon = p.lon;
  state.streetName = '';
  movedStreet({ prefix: `${p.name}, ${p.kind}.` });
}

// ── Tilt steering ─────────────────────────────────────────────────────────

let steerIntroduced = false;

function headingWord(h) {
  const name = compassName(h).replace('-', ' ');
  return name[0].toUpperCase() + name.slice(1);
}

function steerBlocked() {
  return !state.started || !!document.querySelector('dialog[open]');
}

const atCorner = () => state.mode === 'street' && !!state.corner;

const steerHandlers = {
  ready() {
    steering.buzz(30);
    audio.steerClick(true);
    showSteerStatus();
    if (steerBlocked()) return;
    say(steerIntroduced
      ? 'Ready. Hold it like this to stay still.'
      : 'Ready. Hold it like this to stay still. Tilt it gently towards the way you want to go, and come back to stop.');
    steerIntroduced = true;
  },
  heading(heading) {
    steering.buzz(12);
    audio.steerClick();
    showSteerStatus();
    if (steerBlocked()) return;
    if (atCorner() && state.corner.armed) {
      // At a corner, the tilt picks one of the ways out.
      state.corner.idx = nearestOption(state.corner.options, heading);
      say(optionText(cornerChoice()));
      return;
    }
    say(`${headingWord(heading)}.`);
  },
  go() {
    steering.buzz(25);
    showSteerStatus();
    if (!atCorner()) state.travel = null; // a fresh start goes the way you tilt
  },
  step(heading, fast) {
    showSteerStatus();
    onSteerStep(heading, fast);
  },
  stop(wasMoving) {
    steering.buzz(15);
    audio.steerClick(true);
    showSteerStatus();
    if (atCorner()) {
      // Arriving at a corner while tilted must not set off at once: come back to still first.
      state.corner.armed = true;
      if (!steerBlocked()) say('Still. Tilt towards the way you want.');
      return;
    }
    if (!steerBlocked() && wasMoving) say('Stopped.');
  },
  paused(wasMoving) {
    showSteerStatus();
    if (!steerBlocked()) say(`${wasMoving ? 'Stopped. ' : ''}Steering paused. Hold it sideways to steer.`);
  },
};

function onSteerStep(bearing, fast) {
  if (steerBlocked()) return;
  if (state.mode === 'street') {
    const m = streetStepM() * (fast ? 2 : 1);
    if (streetSteerMode() === 'free') return streetMove(bearing, m);
    if (state.corner) {
      if (state.corner.armed) takeCorner(m);
      return;
    }
    // Between corners, keep along the street the way you are travelling,
    // unless you have tilted right round.
    const dir = state.travel != null && street.bearingGap(bearing, state.travel) <= 120 ? state.travel : bearing;
    streetFollow(dir, m, true);
    return;
  }
  // Seas are huge, so steps over water are bigger.
  if (!state.info.onLand) return step(bearing, fast ? 100 : 50);
  step(bearing, fast ? Math.max(25, stepKm() * 4) : stepKm());
}

function showSteerStatus() {
  const el = $('steer-status');
  const st = steering.steeringStatus();
  el.hidden = st.mode === 'off';
  if (st.mode === 'off') return;
  if (st.mode !== 'ready') el.textContent = 'Steering paused: hold the device sideways.';
  else if (st.moving) el.textContent = `Steering: going ${headingWord(st.heading).toLowerCase()}${st.fast ? ', fast' : ''}.`;
  else el.textContent = 'Steering: still. Tilt gently to go.';
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
  say(streetSteerMode() === 'free'
    ? 'Steering free: it goes straight the way you tilt, across blocks too.'
    : 'Steering picks turns: it follows the street and stops at each corner so you can choose the next one.');
}

function setSteering(on) {
  settings.set('steering', on);
  if (on) steering.startSteering(steerHandlers);
  else steering.stopSteering();
  $('btn-steer').textContent = on ? 'Steering: on' : 'Steering: off';
  $('btn-steer').setAttribute('aria-pressed', String(on));
  showSteerStatus();
  if (!on) return say('Steering off.');
  steerIntroduced = false;
  say('Steering on. Hold the device sideways in both hands, at a comfortable angle.');
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
    if (on) audio.playPlace(state.info.onLand ? state.info.country : 'ocean', state.info.continent);
    say(`Sounds ${on ? 'on' : 'off'}.`);
  },
  F9: () => {
    settings.set('visualAssist', !settings.get('visualAssist'));
    applyVisualAssist();
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

function setJumpResults(results) {
  jumpResults = results;
  const list = $('jump-results');
  list.replaceChildren(...results.map((r, i) => new Option(r.label, String(i))));
  list.hidden = !results.length;
  $('jump-go').hidden = !results.length;
  // On a phone the picker's first entry is already selected, so a "choose"
  // prompt goes first; otherwise picking that first place would change nothing.
  if (results.length && steering.isTouchDevice()) {
    const prompt = new Option(`${results.length} results. Choose a place…`, '');
    prompt.disabled = true;
    list.prepend(prompt);
    list.selectedIndex = 0;
  } else if (results.length) {
    list.selectedIndex = 0;
  }
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

function openSettings() {
  const f = $('settings-form');
  f.units.value = settings.get('units');
  f.speech.value = settings.get('speech');
  f.sounds.checked = settings.get('sounds');
  f.visualAssist.checked = settings.get('visualAssist');
  f.geoFeatures.checked = settings.get('geoFeatures');
  f.streetSteer.value = streetSteerMode();
  $('settings-dialog').showModal();
  f.units.focus();
}

function saveSettings(e) {
  e.preventDefault();
  const f = $('settings-form');
  settings.set('units', f.units.value);
  settings.set('speech', f.speech.value);
  settings.set('geoFeatures', f.geoFeatures.checked);
  settings.set('streetSteer', f.streetSteer.value);
  showStreetSteer();
  audio.setSoundsEnabled(f.sounds.checked);
  settings.set('visualAssist', f.visualAssist.checked);
  applyVisualAssist();
  $('settings-dialog').close();
  $('map').focus();
  say('Settings saved.');
}

// ── Wiring ────────────────────────────────────────────────────────────────

$('map').addEventListener('keydown', onMapKey);
$('jump-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { e.preventDefault(); runJumpSearch(); }
});
$('jump-results').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') {
    e.preventDefault();
    chooseSelected();
  }
});
function chooseSelected() {
  const value = $('jump-results').value;
  const r = value === '' ? null : jumpResults[Number(value)];
  if (r) chooseJump(r);
}
$('jump-results').addEventListener('dblclick', chooseSelected);
$('jump-go').addEventListener('click', chooseSelected);
// Phones show the list as a picker: picking a place is the choice, with no Enter key to press.
$('jump-results').addEventListener('change', () => { if (steering.isTouchDevice()) chooseSelected(); });
$('places-list').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { e.preventDefault(); choosePlace(); }
});
$('places-list').addEventListener('dblclick', choosePlace);
$('places-list').addEventListener('change', () => { if (steering.isTouchDevice()) choosePlace(); });
$('places-go').addEventListener('click', choosePlace);
for (const id of ['jump-dialog', 'help-dialog', 'settings-dialog', 'places-dialog']) {
  $(id).addEventListener('close', () => { if (state.started) $('map').focus(); });
}
for (const btn of document.querySelectorAll('[data-close]')) {
  btn.addEventListener('click', () => btn.closest('dialog').close());
}
$('settings-form').addEventListener('submit', saveSettings);
$('btn-jump').addEventListener('click', () => openJump());
$('btn-help').addEventListener('click', openHelp);
$('btn-settings').addEventListener('click', openSettings);
$('btn-steer').addEventListener('click', toggleSteering);
$('btn-street-steer').addEventListener('click', toggleStreetSteer);
$('btn-street').addEventListener('click', () => { if (state.started) toggleStreetMode(); });
showStep();
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
