// Country and region ambience plus spatial tones, ported from sound_engine.py.
//
// Browsers only allow audio after a user gesture, so unlock() is called from
// the Start button.

import { settings } from './settings.js';

let ctx = null;
let master = null;
let ambient = null; // { path, el, src, gain }
let available = { countries: new Set(), regions: new Set() };
let fallbacks = {};
let aliases = {};

export function configureAudio(soundList, tables) {
  available = {
    countries: new Set(soundList.countries || []),
    regions: new Set(soundList.regions || []),
  };
  fallbacks = tables.soundFallbacks || {};
  aliases = tables.countryAliases || {};
}

export function unlock() {
  // iPhone: play through the silent switch, like a media app, rather than
  // being muted along with ringtones (Safari 16.4 and later).
  try { if (navigator.audioSession) navigator.audioSession.type = 'playback'; } catch { /* older Safari */ }
  if (!ctx) {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return;
    ctx = new AC();
    master = ctx.createGain();
    master.gain.value = settings.get('volume');
    master.connect(ctx.destination);
    // Phones suspend audio when the page is hidden; pick it up again on return.
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible' && ctx.state !== 'running') ctx.resume().catch(() => {});
    });
  }
  if (ctx.state === 'suspended') ctx.resume();
}

export function setVolume(v) {
  v = Math.max(0, Math.min(1, v));
  settings.set('volume', v);
  if (master) master.gain.setTargetAtTime(v, ctx.currentTime, 0.05);
  return v;
}

export function setSoundsEnabled(on) {
  settings.set('sounds', on);
  if (!on) stopAmbient();
}

// Same as sound_engine._safe_stem.
const stem = (name) => String(name).toLowerCase().replaceAll(' ', '_').replaceAll("'", '')
  .replaceAll('/', '_').replaceAll('&', 'and').replaceAll(',', '').replaceAll('.', '');

/** Same candidate order as SoundEngine.play_location_sound. */
function soundPathFor(place, continent) {
  const canonical = aliases[place] || place;
  const stems = [stem(place)];
  if (stem(canonical) !== stems[0]) stems.push(stem(canonical));
  const fb = fallbacks[canonical];
  if (fb) stems.push(stem(fb));
  for (const s of stems) {
    for (const ext of ['m4a', 'ogg', 'mp3']) {
      if (available.countries.has(`${s}.${ext}`)) return `sounds/countries/${s}.${ext}`;
      if (available.regions.has(`${s}.${ext}`)) return `sounds/regions/${s}.${ext}`;
    }
  }
  if (continent) {
    const c = stem(continent);
    for (const ext of ['m4a', 'ogg', 'mp3']) if (available.regions.has(`${c}.${ext}`)) return `sounds/regions/${c}.${ext}`;
  }
  return null;
}

// Decoded sounds, so returning to a country does not download it again.
const buffers = new Map(); // path -> Promise<AudioBuffer>
const BUFFER_LIMIT = 12;

function loadBuffer(path) {
  if (!buffers.has(path)) {
    buffers.set(path, fetch(path)
      .then((r) => { if (!r.ok) throw new Error(r.status); return r.arrayBuffer(); })
      .then((data) => ctx.decodeAudioData(data))
      .catch((err) => { buffers.delete(path); throw err; }));
    if (buffers.size > BUFFER_LIMIT) buffers.delete(buffers.keys().next().value);
  }
  return buffers.get(path);
}

/**
 * Crossfade to the ambience for a country ("ocean" for open water).
 * Sounds are decoded and played through the already-unlocked AudioContext:
 * iPhone will not start a new <audio> element without a fresh tap.
 */
export function playPlace(place, continent) {
  if (!ctx || !settings.get('sounds')) return;
  const path = soundPathFor(place, continent);
  if (ambient && ambient.path === path) return; // neighbours sharing a regional loop
  const old = ambient;
  ambient = null;
  if (old) fadeOutAndStop(old);
  if (!path) return;
  const mine = { path, src: null, gain: ctx.createGain() };
  mine.gain.gain.value = 0;
  mine.gain.connect(master);
  ambient = mine;
  loadBuffer(path).then((buffer) => {
    if (ambient !== mine) return; // moved on while it downloaded
    const src = ctx.createBufferSource();
    src.buffer = buffer;
    src.loop = true;
    src.connect(mine.gain);
    src.start();
    mine.src = src;
    mine.gain.gain.setTargetAtTime(0.5, ctx.currentTime, 0.4);
  }).catch(() => { /* no sound for this place */ });
}

function fadeOutAndStop(a) {
  try {
    a.gain.gain.setTargetAtTime(0, ctx.currentTime, 0.3);
    setTimeout(() => {
      try { a.src?.stop(); } catch { /* not started */ }
      a.src?.disconnect();
      a.gain.disconnect();
    }, 1500);
  } catch { /* already gone */ }
}

export function stopAmbient() {
  if (ambient) fadeOutAndStop(ambient);
  ambient = null;
}

/** Normalised latitude/longitude to pitch and pan, as spatial_tone_parameters. */
export function toneParameters(lat, lon, bounds) {
  if (bounds) {
    let [minLat, maxLat, minLon, maxLon] = bounds;
    if ((minLon > 180 || maxLon > 180) && lon < 0) lon += 360;
    const latSpan = maxLat - minLat;
    const lonSpan = maxLon - minLon;
    if (latSpan > 0 && lonSpan > 0) {
      lat = ((lat - minLat) / latSpan) * 180 - 90;
      lon = ((lon - minLon) / lonSpan) * 360 - 180;
    }
  }
  let freq = lat < 0 ? 440 + (lat / 90) * 220 : 440 + (lat / 90) * 440;
  freq = Math.max(220, Math.min(880, freq));
  const pan = Math.max(-1, Math.min(1, lon / 180));
  return { freq, pan };
}

function beep(freq, pan, duration = 0.15, level = 0.35, type = 'sine') {
  if (!ctx || !settings.get('sounds')) return;
  const t = ctx.currentTime;
  const osc = ctx.createOscillator();
  const gain = ctx.createGain();
  const panner = ctx.createStereoPanner ? ctx.createStereoPanner() : null;
  osc.type = type;
  osc.frequency.value = freq;
  gain.gain.setValueAtTime(0, t);
  gain.gain.linearRampToValueAtTime(level, t + 0.04);
  gain.gain.setValueAtTime(level, t + duration - 0.04);
  gain.gain.linearRampToValueAtTime(0, t + duration);
  if (panner) { panner.pan.value = pan; osc.connect(gain).connect(panner).connect(master); }
  else osc.connect(gain).connect(master);
  osc.start(t);
  osc.stop(t + duration + 0.02);
}

export function spatialTone(lat, lon, bounds) {
  const { freq, pan } = toneParameters(lat, lon, bounds);
  beep(freq, pan);
}

/** Short low tone for "can't go further" (poles, no result). */
export function edgeTone() {
  beep(180, 0, 0.12, 0.3, 'triangle');
}

/** A short click as the steering wheel settles on a new heading; a double one when it is ready. */
export function steerClick(ready = false) {
  beep(1400, 0, 0.03, 0.25, 'square');
  if (ready && ctx) setTimeout(() => beep(1800, 0, 0.03, 0.25, 'square'), 90);
}

/** A short tone for a move: from the right for east, the left for west, higher for north, lower for south. */
export function directionTone(bearing) {
  const r = (bearing * Math.PI) / 180;
  beep(660 + 220 * Math.cos(r), Math.sin(r), 0.12, 0.3, 'triangle');
}
