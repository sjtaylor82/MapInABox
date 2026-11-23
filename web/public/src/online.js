// Network lookups. Searches go through the Map in a Box Worker (/api/...)
// when it is there, so schools only need to allow one domain; when the page
// is served without the Worker (local testing) they go to the public service.

import { buildQuery, compact, OVERPASS_MIRRORS } from './streetquery.js';

let apiAvailable = null;

async function hasApi() {
  if (apiAvailable !== null) return apiAvailable;
  try {
    const r = await fetch('api/health', { cache: 'no-store' });
    apiAvailable = r.ok && (await r.json()).ok === true;
  } catch {
    apiAvailable = false;
  }
  return apiAvailable;
}

async function getJson(url, timeoutMs = 12000) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const r = await fetch(url, { signal: ctrl.signal, headers: { Accept: 'application/json' } });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return await r.json();
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Streets and places for one street tile: { ways, pois }. Through the Worker,
 * which caches each tile for everyone; straight from Overpass without it.
 */
export async function streetTile(ty, tx) {
  if (await hasApi()) {
    // The Worker shares each tile with everyone once it has it. Overpass
    // often turns the Worker away, so when it can't get a tile, ask Overpass
    // straight from this browser instead.
    try {
      const r = await fetch(`api/streets?ty=${ty}&tx=${tx}`, { headers: { Accept: 'application/json' } });
      if (r.ok) return await r.json();
    } catch { /* fall through */ }
  }
  // Straight to the public Overpass servers, in turn.
  let lastError = 'could not be reached';
  for (const mirror of OVERPASS_MIRRORS) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 45000);
    try {
      const r = await fetch(mirror, {
        method: 'POST',
        body: new URLSearchParams({ data: buildQuery(ty, tx) }),
        signal: ctrl.signal,
      });
      if (r.ok) return compact(await r.json());
      lastError = r.status === 429 || r.status >= 502 ? 'is busy' : `answered ${r.status}`;
    } catch (err) {
      lastError = err.name === 'AbortError' ? 'took too long' : 'could not be reached';
    } finally {
      clearTimeout(timer);
    }
  }
  throw new Error(`The street data service ${lastError}. Try again in a minute.`);
}

/** Place search (Photon). Returns [{ label, lat, lon }]. */
export async function searchOnline(query, near) {
  const params = new URLSearchParams({ q: query.slice(0, 200), limit: '10', lang: 'en' });
  const direct = 'https://photon.komoot.io/api/';
  // Photon sometimes refuses Cloudflare's servers, so fall back to it directly.
  const ask = async () => {
    if (await hasApi()) {
      try { return await getJson(`api/search?${params}`); } catch { /* fall through */ }
    }
    return getJson(`${direct}?${params}`);
  };
  let json = await ask();
  let features = json.features || [];
  if (!features.length && near) {
    params.set('lat', near.lat.toFixed(2));
    params.set('lon', near.lon.toFixed(2));
    json = await ask();
    features = json.features || [];
  }
  const seen = new Set();
  return features.map((f) => {
    const p = f.properties || {};
    const [lon, lat] = f.geometry.coordinates;
    const street = p.housenumber && p.street ? `${p.housenumber} ${p.street}` : p.street;
    const parts = [p.name, street, p.city || p.locality || p.district, p.state, p.country];
    const label = parts.filter((x, i) => x && parts.indexOf(x) === i).join(', ');
    return { label, lat, lon };
  }).filter((r) => r.label && !seen.has(r.label) && seen.add(r.label));
}

const WEATHER_CODES = {
  0: 'clear sky', 1: 'mainly clear', 2: 'partly cloudy', 3: 'overcast', 45: 'fog', 48: 'freezing fog',
  51: 'light drizzle', 53: 'drizzle', 55: 'heavy drizzle', 56: 'freezing drizzle', 57: 'freezing drizzle',
  61: 'light rain', 63: 'rain', 65: 'heavy rain', 66: 'freezing rain', 67: 'freezing rain',
  71: 'light snow', 73: 'snow', 75: 'heavy snow', 77: 'snow grains', 80: 'light showers',
  81: 'showers', 82: 'heavy showers', 85: 'snow showers', 86: 'heavy snow showers',
  95: 'thunderstorm', 96: 'thunderstorm with hail', 99: 'thunderstorm with heavy hail',
};

function meteoUrl(lat, lon, extra) {
  const p = new URLSearchParams({ latitude: lat.toFixed(3), longitude: lon.toFixed(3), timezone: 'auto', ...extra });
  return `https://api.open-meteo.com/v1/forecast?${p}`;
}

export async function weather(lat, lon, imperial) {
  const j = await getJson(meteoUrl(lat, lon, {
    current: 'temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m',
    ...(imperial ? { temperature_unit: 'fahrenheit', wind_speed_unit: 'mph' } : {}),
  }));
  const c = j.current;
  const t = imperial ? '°F' : '°C';
  const w = imperial ? 'miles per hour' : 'kilometres per hour';
  return `${WEATHER_CODES[c.weather_code] || 'Weather'}, ${Math.round(c.temperature_2m)}${t}, `
    + `feels like ${Math.round(c.apparent_temperature)}${t}. Humidity ${c.relative_humidity_2m}%. `
    + `Wind ${Math.round(c.wind_speed_10m)} ${w}.`;
}

export async function timezoneInfo(lat, lon) {
  const j = await getJson(meteoUrl(lat, lon, { current: 'temperature_2m', forecast_days: '1' }));
  return { zone: j.timezone, abbreviation: j.timezone_abbreviation, offset: j.utc_offset_seconds };
}

export function localTimeText(tz) {
  try {
    return new Intl.DateTimeFormat('en-AU', {
      timeZone: tz.zone, hour: 'numeric', minute: '2-digit', weekday: 'long', day: 'numeric', month: 'long',
    }).format(new Date());
  } catch {
    const d = new Date(Date.now() + tz.offset * 1000);
    return d.toUTCString().slice(0, 22);
  }
}

export async function sunTimes(lat, lon) {
  const j = await getJson(meteoUrl(lat, lon, { daily: 'sunrise,sunset', forecast_days: '1' }));
  const time = (iso) => (iso ? iso.slice(11, 16) : 'none');
  return `Sunrise ${time(j.daily.sunrise[0])}, sunset ${time(j.daily.sunset[0])}, local time.`;
}

export async function airQuality(lat, lon) {
  const p = new URLSearchParams({ latitude: lat.toFixed(3), longitude: lon.toFixed(3), current: 'european_aqi,us_aqi,pm2_5' });
  const j = await getJson(`https://air-quality-api.open-meteo.com/v1/air-quality?${p}`);
  const aqi = j.current.us_aqi;
  const band = aqi == null ? 'unknown' : aqi <= 50 ? 'good' : aqi <= 100 ? 'moderate'
    : aqi <= 150 ? 'unhealthy for sensitive groups' : aqi <= 200 ? 'unhealthy' : 'very unhealthy';
  return `Air quality ${band}, US index ${aqi ?? 'unknown'}. Fine particles ${j.current.pm2_5 ?? 'unknown'} micrograms per cubic metre.`;
}

export async function wikipediaSummary(title) {
  const j = await getJson(`https://en.wikipedia.org/api/rest_v1/page/summary/${encodeURIComponent(title.replaceAll(' ', '_'))}`);
  return j.extract || `No Wikipedia summary for ${title}.`;
}
