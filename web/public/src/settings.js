// Per-browser settings. Storage can be missing (private windows, blocked
// site data), so every access is guarded and the app works without it.

const KEY = 'miab-web-settings';

const DEFAULTS = {
  units: 'metric',          // 'metric' | 'imperial'
  speech: 'screenreader',   // 'screenreader' | 'voice' | 'both'
  voiceRate: 1.1,
  sounds: true,
  volume: 0.6,
  toneScope: 'world',       // 'world' | 'country'
  visualAssist: true,
  geoFeatures: true,
  zoom: 1,
  position: null,           // { lat, lon } last position
  jumpHistory: [],
};

let values = { ...DEFAULTS };
try {
  const raw = localStorage.getItem(KEY);
  if (raw) values = { ...DEFAULTS, ...JSON.parse(raw) };
} catch { /* storage unavailable */ }

function save() {
  try { localStorage.setItem(KEY, JSON.stringify(values)); } catch { /* ignore */ }
}

export const settings = {
  get(name) { return values[name]; },
  set(name, value) { values[name] = value; save(); },
  all() { return { ...values }; },
};
