// Map in a Box Web: world map mode.

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
import { initMapView, updateMapView, draw as drawMap } from './mapview.js';
import * as tilt from './tilt.js';

const $ = (id) => document.getElementById(id);

const state = {
  lat: -27.47,
  lon: 153.03,
  info: { country: '', admin: '', city: '', iso2: '', continent: '', onLand: false, label: '' },
  seq: 0,
  started: false,
  busy: false,
};

// ── Start-up ──────────────────────────────────────────────────────────────

async function boot() {
  initSpeech();
  initMapView($('map-canvas'), $('map-caption'));
  applyVisualAssist();
  // Phones and tablets: offer tilt, and the browser's voice for people without a screen reader.
  const touch = tilt.isTouchDevice() && tilt.tiltSupported();
  $('intro-tilt-row').hidden = !touch;
  $('intro-tilt').checked = touch && settings.get('tilt') !== false;
  $('intro-speech').value = settings.get('speech');
  const hasSaved = !!(settings.get('position') && Number.isFinite(settings.get('position').lat));
  $('intro-where').querySelector('[value="last"]').hidden = !hasSaved;
  $('intro-where').value = hasSaved ? 'last' : touch ? 'here' : 'choose';
  $('btn-tilt').hidden = !touch;
  $('btn-level').hidden = true;
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
  const wantTilt = !$('intro-tilt-row').hidden && $('intro-tilt').checked;
  const tiltAllowed = wantTilt ? tilt.requestTiltPermission() : Promise.resolve(false);
  settings.set('speech', $('intro-speech').value);
  audio.unlock();
  unlockVoice();
  state.started = true;
  $('intro').hidden = true;
  $('app').hidden = false;
  $('map').focus();
  drawMap();
  if (wantTilt) {
    // Don't hold up the start waiting for the permission answer.
    tiltAllowed.then((ok) => {
      if (ok) setTilt(true, { quiet: true });
      else say('Tilt is not available. Motion access was not allowed.');
    });
  } else {
    settings.set('tilt', false);
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

// ── Tilt ──────────────────────────────────────────────────────────────────

const TILT_BEARING = { north: 0, east: 90, south: 180, west: 270 };

let tiltCrossing = false;

function onTiltStep(direction, size, angle) {
  if (!state.started || document.querySelector('dialog[open]') || tiltCrossing) return;
  const bearing = TILT_BEARING[direction];
  if (!state.info.onLand) {
    // Seas are huge: a sharp tilt crosses to the next country in that
    // direction, and gentler tilts take bigger steps than on land.
    if (size === 'large') {
      tilt.buzz(40);
      tiltCrossing = true;
      nextCountry(bearing, direction);
      // Give the landing time to be heard before the tilt carries on.
      setTimeout(() => { tiltCrossing = false; }, 2500);
      return;
    }
    tilt.buzz(12);
    step(bearing, angle >= 25 ? 50 : 25);
    return;
  }
  tilt.buzz(size === 'large' ? 30 : 12);
  step(bearing, size === 'large' ? 100 : stepKm());
}

// ── Step size ─────────────────────────────────────────────────────────────

const STEP_SIZES = [1, 2, 5, 10, 25];

function stepKm() {
  const km = settings.get('stepKm');
  return STEP_SIZES.includes(km) ? km : 5;
}

function stepText(km) {
  if (settings.get('units') === 'imperial') return formatDistance(km * 1000);
  return `${km} ${km === 1 ? 'kilometre' : 'kilometres'}`;
}

function showStep() {
  const km = stepKm();
  const slider = $('step-size');
  slider.value = String(STEP_SIZES.indexOf(km));
  slider.setAttribute('aria-valuetext', stepText(km));
  $('step-value').textContent = stepText(km);
}

function setStepIndex(i, announce = true) {
  const idx = Math.max(0, Math.min(STEP_SIZES.length - 1, i));
  settings.set('stepKm', STEP_SIZES[idx]);
  showStep();
  if (announce) say(`Step size ${stepText(STEP_SIZES[idx])}.`);
}

function setTilt(on, { quiet = false } = {}) {
  settings.set('tilt', on);
  if (on) tilt.startTilt(onTiltStep);
  else tilt.stopTilt();
  $('btn-tilt').textContent = on ? 'Tilt: on' : 'Tilt: off';
  $('btn-tilt').setAttribute('aria-pressed', String(on));
  $('btn-level').hidden = !on;
  if (!quiet) say(on ? 'Tilt on. Hold the device how you like, then tap Set level.' : 'Tilt off.');
  else if (on) say('Tilt on. The way you are holding the device now counts as level. Tilt it to move.');
}

async function toggleTilt() {
  if (tilt.tiltActive()) return setTilt(false);
  if (await tilt.requestTiltPermission()) setTilt(true);
  else say('Tilt is not available. Motion access was not allowed.');
}

function setLevelNow() {
  if (!tilt.tiltActive()) return;
  say(tilt.setLevel() ? 'Level set.' : 'No tilt reading yet. Move the device a little and try again.');
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
  const label = state.info.label || 'No location yet.';
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
    if (alt && !ctrl) return nextCountry(bearing, name);
    return step(bearing, ctrl ? 100 : shift ? 1 : stepKm());
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
  y: whereAmI,
  '-': () => setStepIndex(STEP_SIZES.indexOf(stepKm()) - 1),
  '=': () => setStepIndex(STEP_SIZES.indexOf(stepKm()) + 1),
  'Shift++': () => setStepIndex(STEP_SIZES.indexOf(stepKm()) + 1),
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
  if (results.length && tilt.isTouchDevice()) {
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
  $('settings-dialog').showModal();
  f.units.focus();
}

function saveSettings(e) {
  e.preventDefault();
  const f = $('settings-form');
  settings.set('units', f.units.value);
  settings.set('speech', f.speech.value);
  settings.set('geoFeatures', f.geoFeatures.checked);
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
$('jump-results').addEventListener('change', () => { if (tilt.isTouchDevice()) chooseSelected(); });
for (const id of ['jump-dialog', 'help-dialog', 'settings-dialog']) {
  $(id).addEventListener('close', () => { if (state.started) $('map').focus(); });
}
for (const btn of document.querySelectorAll('[data-close]')) {
  btn.addEventListener('click', () => btn.closest('dialog').close());
}
$('settings-form').addEventListener('submit', saveSettings);
$('btn-jump').addEventListener('click', () => openJump());
$('btn-help').addEventListener('click', openHelp);
$('btn-settings').addEventListener('click', openSettings);
$('btn-tilt').addEventListener('click', toggleTilt);
showStep();
// The slider reports its own new value (aria-valuetext), so it doesn't speak as well.
$('step-size').addEventListener('input', (e) => setStepIndex(Number(e.target.value), false));
for (const btn of document.querySelectorAll('#panel [data-key]')) {
  btn.addEventListener('click', () => { if (state.started) KEYS[btn.dataset.key]?.(); });
}
for (const btn of document.querySelectorAll('#panel [data-move]')) {
  btn.addEventListener('click', () => { if (state.started) step(Number(btn.dataset.move), stepKm()); });
}
$('btn-where').addEventListener('click', whereAmI);
$('jump-here').addEventListener('click', whereAmI);
$('btn-level').addEventListener('click', setLevelNow);
// Tapping the map (a double tap with VoiceOver or TalkBack) sets level while tilting.
$('map').addEventListener('click', () => { if (tilt.tiltActive()) setLevelNow(); });
// Keys pressed while focus is on the toolbar still drive the map.
document.addEventListener('keydown', (e) => {
  if (e.target === $('map') || document.querySelector('dialog[open]')) return;
  if (e.target.closest && e.target.closest('#toolbar') && (ARROWS[e.key] || /^F\d+$/.test(e.key))) onMapKey(e);
});

// Exported for the browser console while testing.
window.miab = { state, describe, nearestCity, city, bearingDeg, compassName, lastSpoken, setTilt };

boot();
