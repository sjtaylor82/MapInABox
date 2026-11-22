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
  if (!ctx) {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return;
    ctx = new AC();
    master = ctx.createGain();
    master.gain.value = settings.get('volume');
    master.connect(ctx.destination);
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
    for (const ext of ['ogg', 'mp3']) {
      if (available.countries.has(`${s}.${ext}`)) return `sounds/countries/${s}.${ext}`;
      if (available.regions.has(`${s}.${ext}`)) return `sounds/regions/${s}.${ext}`;
    }
  }
  if (continent) {
    const c = stem(continent);
    for (const ext of ['ogg', 'mp3']) if (available.regions.has(`${c}.${ext}`)) return `sounds/regions/${c}.${ext}`;
  }
  return null;
}

/** Crossfade to the ambience for a country ("ocean" for open water). */
export function playPlace(place, continent) {
  if (!ctx || !settings.get('sounds')) return;
  const path = soundPathFor(place, continent);
  if (ambient && ambient.path === path) return; // neighbours sharing a regional loop
  const old = ambient;
  ambient = null;
  if (old) fadeOutAndStop(old);
  if (!path) return;
  const el = new Audio(path);
  el.loop = true;
  el.crossOrigin = 'anonymous';
  const src = ctx.createMediaElementSource(el);
  const gain = ctx.createGain();
  gain.gain.value = 0;
  src.connect(gain).connect(master);
  ambient = { path, el, src, gain };
  el.play().then(() => {
    gain.gain.setTargetAtTime(0.5, ctx.currentTime, 0.4);
  }).catch(() => {});
}

function fadeOutAndStop(a) {
  try {
    a.gain.gain.setTargetAtTime(0, ctx.currentTime, 0.3);
    setTimeout(() => { a.el.pause(); a.src.disconnect(); a.gain.disconnect(); }, 1500);
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
