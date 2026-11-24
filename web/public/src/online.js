// Network lookups. Searches go through the Map in a Box Worker (/api/...)
// when it is there, so schools only need to allow one domain; when the page
// is served without the Worker (local testing) they go to the public service.

import { ZOOM, TILEJSON, streetData } from './vectortile.js';
import { OVERPASS, detailsQuery, pickDetails } from './placedetails.js';

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

let tileTemplate = null;

/**
 * Streets and places for one map tile: { ways, pois }. The tile comes from
 * OpenFreeMap, through the Worker when it is there (so schools only need to
 * allow one address), or straight from OpenFreeMap otherwise.
 */
export async function streetTile(ty, tx) {
  let bytes = null;
  if (await hasApi()) {
    // First choice: full OpenStreetMap data through the Worker (Overpass),
    // with addresses, phone numbers and opening hours. A part of the map
    // asked for the first time can take 10 to 20 seconds, so after 5 seconds
    // use the OpenFreeMap tile (street names, fewer addresses) instead. The
    // OpenStreetMap request carries on, so the Worker stores it and it is
    // quick next time.
    const full = fetch(`api/area/${ZOOM}/${tx}/${ty}`, { signal: AbortSignal.timeout(60000) })
      .then((r) => (r.ok ? r.json() : null)).catch(() => null);
    const data = await Promise.race([full, new Promise((r) => setTimeout(() => r(undefined), 5000))]);
    if (data) return data;
    try {
      const r = await fetch(`api/tile/${ZOOM}/${tx}/${ty}`);
      if (r.ok) bytes = new Uint8Array(await r.arrayBuffer());
    } catch { /* fall through */ }
  }
  if (!bytes) {
    if (!tileTemplate) tileTemplate = (await getJson(TILEJSON)).tiles[0];
    const url = tileTemplate.replace('{z}', ZOOM).replace('{x}', tx).replace('{y}', ty);
    const r = await fetch(url);
    if (!r.ok) throw new Error(`The street map service answered ${r.status}.`);
    bytes = new Uint8Array(await r.arrayBuffer());
  }
  return streetData(bytes, ty, tx);
}

/**
 * Phone, hours, website and address for one place, or null if OpenStreetMap
 * has none. Through the Worker, or straight from Overpass if that fails.
 */
export async function placeDetails(name, lat, lon) {
  if (await hasApi()) {
    try {
      const p = new URLSearchParams({ name, lat: String(lat), lon: String(lon) });
      const r = await fetch(`api/place?${p}`, { signal: AbortSignal.timeout(28000) });
      if (r.ok) return (await r.json()).details;
    } catch { /* fall through */ }
  }
  for (const mirror of OVERPASS) {
    try {
      const r = await fetch(mirror, {
        method: 'POST',
        body: new URLSearchParams({ data: detailsQuery(name, lat, lon) }),
        signal: AbortSignal.timeout(12000),
      });
      if (r.ok) return pickDetails(await r.json());
    } catch { /* next */ }
  }
  throw new Error('The place details service is busy.');
}

/** The address parts at a point (Photon reverse): { street, number, place, state, postcode } or null. */
export async function addressAt(lat, lon) {
  const params = new URLSearchParams({ lat: lat.toFixed(6), lon: lon.toFixed(6), lang: 'en' });
  let json = null;
  if (await hasApi()) {
    try { json = await getJson(`api/reverse?${params}`); } catch { /* fall through */ }
  }
  if (!json) json = await getJson(`https://photon.komoot.io/reverse?${params}`);
  const p = ((json.features || [])[0] || {}).properties;
  if (!p) return null;
  return {
    street: p.street || '',
    number: p.housenumber || '',
    name: p.name || '',
    place: townOf(p),
    state: p.state || '',
    postcode: p.postcode || '',
  };
}

/** Place search (Photon). Returns [{ label, lat, lon }]. */
/**
 * The town part of a Photon address. In Australia addresses use the suburb
 * (373 Old Cleveland Road, Coorparoo), not the city it's part of (Brisbane).
 */
function townOf(p) {
  if (p.countrycode === 'AU') return p.district || p.locality || p.city || '';
  return p.city || p.locality || p.district || '';
}

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
  // Nearer matches first (Vision Australia in your city, not another state's),
  // while famous places far away still come up. Rounded to about a kilometre.
  if (near) {
    params.set('lat', near.lat.toFixed(2));
    params.set('lon', near.lon.toFixed(2));
    params.set('location_bias_scale', '0.9'); // famous places still win: Paris is in France
  }
  const json = await ask();
  const features = json.features || [];
  const seen = new Set();
  return features.map((f) => {
    const p = f.properties || {};
    const [lon, lat] = f.geometry.coordinates;
    const street = p.housenumber && p.street ? `${p.housenumber} ${p.street}` : p.street;
    const parts = [p.name, street, townOf(p), p.state, p.country];
    const label = parts.filter((x, i) => x && parts.indexOf(x) === i).join(', ');
    // Food places can show their menu in Jump's Details.
    const food = p.osm_key === 'amenity' && /^(restaurant|cafe|fast_food|food_court|ice_cream|pub|bar|biergarten)$/.test(p.osm_value || '');
    return { label, lat, lon, food };
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
