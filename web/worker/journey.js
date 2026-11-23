// Map in a Box Web — journey planner API, used by worker/index.js.
//
//   GET /api/journey?from=&to=   Rome2Rio journey options, cached for a day.
//                                fromPos/toPos=lat,lon for exact points;
//                                currency=AUD (the default) and others.
//   GET /api/flights?from=&to=&date=YYYY-MM-DD
//                                Flights that day (Rome2Rio), cached for an hour
//   GET /api/road?from=lat,lon&to=lat,lon&profile=car|foot
//                                An exact road line (OSRM), cached for a week,
//                                for finding food along a journey leg
//   POST /api/food {line, radius} Named food places within radius metres of an
//                                encoded polyline (OpenStreetMap, via Overpass)

import { parseSearch, parseFlights, decodePolyline } from '../public/src/rome2rio.js';

const USER_AGENT = 'MapInABoxWeb/0.1 (+https://github.com/sjtaylor82/MapInABox)';

const ROME2RIO = 'https://www.rome2rio.com/api/1.5/json/';
// Rome2Rio's public key, the one its own pages use (as in the desktop's
// rome2rio.py). It stays here on the server; a key of your own can be set
// with `wrangler secret put ROME2RIO_KEY`.
const ROME2RIO_PUBLIC_KEY = 'jGq3Luw3';
const CURRENCIES = new Set(['AUD', 'NZD', 'USD', 'CAD', 'GBP', 'EUR', 'JPY', 'SGD', 'HKD', 'INR', 'ZAR', 'CHF', 'SEK', 'NOK', 'DKK']);

/** The journey routes this module answers, or null to let index.js carry on. */
export function journeyRoute(request, url, env, ctx) {
  switch (url.pathname) {
    case '/api/journey': return request.method === 'GET' ? journey(url, env, ctx) : null;
    case '/api/flights': return request.method === 'GET' ? flights(url, env, ctx) : null;
    case '/api/road': return request.method === 'GET' ? road(url, ctx) : null;
    case '/api/walk': return request.method === 'GET' ? walk(url, ctx) : null;
    case '/api/food': return request.method === 'POST' ? food(request) : null;
    default: return null;
  }
}

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' },
  });
}

function text(url, name) {
  return (url.searchParams.get(name) || '').replace(/\s+/g, ' ').trim().slice(0, 200);
}

/** "lat,lon" -> "lat,lon" rounded to about 10 m, or '' when it is not a position. */
function position(value) {
  const m = String(value || '').match(/^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$/);
  if (!m) return '';
  const lat = Number(m[1]);
  const lon = Number(m[2]);
  if (Math.abs(lat) > 90 || Math.abs(lon) > 180) return '';
  return `${lat.toFixed(4)},${lon.toFixed(4)}`;
}

function currencyOf(url) {
  const c = (url.searchParams.get('currency') || 'AUD').toUpperCase();
  return CURRENCIES.has(c) ? c : 'AUD';
}

async function cachedJson(key, ttl, ctx, produce) {
  const cache = caches.default;
  const req = new Request(key);
  const hit = await cache.match(req);
  if (hit) return hit;
  let body;
  try {
    body = await produce();
  } catch (err) {
    return json({ error: err.message || 'The service could not be reached.' }, 502);
  }
  const out = new Response(JSON.stringify(body), {
    headers: { 'Content-Type': 'application/json', 'Cache-Control': `public, max-age=${ttl}` },
  });
  ctx.waitUntil(cache.put(req, out.clone()));
  return out;
}

async function rome2rio(env, method, params) {
  const all = new URLSearchParams(params);
  all.set('key', env.ROME2RIO_KEY || ROME2RIO_PUBLIC_KEY);
  all.set('languageCode', 'en');
  const res = await fetch(`${ROME2RIO}${method}?${all}`, {
    headers: { 'User-Agent': USER_AGENT, Accept: 'application/json' },
    signal: AbortSignal.timeout(30000),
  });
  if (!res.ok) throw new Error(`Rome2Rio answered ${res.status}.`);
  return res.json();
}

async function journey(url, env, ctx) {
  const from = text(url, 'from');
  const to = text(url, 'to');
  const fromPos = position(url.searchParams.get('fromPos'));
  const toPos = position(url.searchParams.get('toPos'));
  if (!(from || fromPos) || !(to || toPos)) return json({ error: 'Say where from and where to.' }, 400);
  const params = new URLSearchParams({ oName: from || 'Start', dName: to || 'Destination', currencyCode: currencyOf(url) });
  if (fromPos) params.set('oPos', fromPos);
  if (toPos) params.set('dPos', toPos);
  return cachedJson(`https://cache.mapinabox/journey/v1?${params}`, 24 * 3600, ctx,
    async () => parseSearch(await rome2rio(env, 'Search', params)));
}

async function flights(url, env, ctx) {
  const from = text(url, 'from');
  const to = text(url, 'to');
  const date = url.searchParams.get('date') || '';
  if (!from || !to || !/^\d{4}-\d{2}-\d{2}$/.test(date)) return json({ error: 'Bad flight search.' }, 400);
  const params = new URLSearchParams({
    oName: from, dName: to, oDateTime: `${date}T00:00:00`, paxAges: '18', currencyCode: currencyOf(url),
  });
  return cachedJson(`https://cache.mapinabox/flights/v1?${params}`, 3600, ctx,
    async () => ({ date, flights: parseFlights(await rome2rio(env, 'flightSchedules', params)) }));
}

// ── Food along a journey ──────────────────────────────────────────────────

const OSRM = {
  car: ['https://routing.openstreetmap.de/routed-car/route/v1/driving/', 'https://router.project-osrm.org/route/v1/driving/'],
  foot: ['https://routing.openstreetmap.de/routed-foot/route/v1/foot/'],
};

/** An exact road line between two points. Rome2Rio's own lines are too coarse to find food a short walk from. */
async function road(url, ctx) {
  const from = position(url.searchParams.get('from'));
  const to = position(url.searchParams.get('to'));
  const profile = url.searchParams.get('profile') === 'foot' ? 'foot' : 'car';
  if (!from || !to) return json({ error: 'Bad road search.' }, 400);
  const lonLat = (p) => p.split(',').reverse().join(',');
  return cachedJson(`https://cache.mapinabox/road/v1/${profile}/${from}/${to}`, 7 * 24 * 3600, ctx, async () => {
    for (const base of OSRM[profile]) {
      try {
        const res = await fetch(`${base}${lonLat(from)};${lonLat(to)}?overview=full&geometries=polyline`, {
          headers: { 'User-Agent': USER_AGENT }, signal: AbortSignal.timeout(20000),
        });
        if (!res.ok) continue;
        const data = await res.json();
        const r = data.routes && data.routes[0];
        if (data.code === 'Ok' && r) return { line: r.geometry, distance: Math.round(r.distance), duration: Math.round(r.duration) };
      } catch { /* try the next server */ }
    }
    throw new Error('The road routing service could not be reached.');
  });
}

/**
 * GET /api/walk?from=lat,lon&to=lat,lon: a walking route with its turns
 * (OSRM foot profile on routing.openstreetmap.de), cached for a week.
 * Each step: { type, modifier, exit, name, distance, lat, lon, bearing }.
 */
async function walk(url, ctx) {
  const from = position(url.searchParams.get('from'));
  const to = position(url.searchParams.get('to'));
  if (!from || !to) return json({ error: 'Bad walking route search.' }, 400);
  const lonLat = (p) => p.split(',').reverse().join(',');
  return cachedJson(`https://cache.mapinabox/walk/v1/${from}/${to}`, 7 * 24 * 3600, ctx, async () => {
    for (const base of OSRM.foot) {
      try {
        const res = await fetch(`${base}${lonLat(from)};${lonLat(to)}?steps=true&overview=full&geometries=polyline`, {
          headers: { 'User-Agent': USER_AGENT }, signal: AbortSignal.timeout(20000),
        });
        if (!res.ok) continue;
        const data = await res.json();
        const r = data.routes && data.routes[0];
        if (data.code !== 'Ok' || !r) continue;
        const steps = [];
        for (const leg of r.legs || []) {
          for (const stp of leg.steps || []) {
            const m = stp.maneuver || {};
            steps.push({
              type: m.type || '',
              modifier: m.modifier || '',
              exit: m.exit || 0,
              name: stp.name || stp.ref || '',
              distance: Math.round(stp.distance || 0),
              lat: m.location ? m.location[1] : null,
              lon: m.location ? m.location[0] : null,
              bearing: m.bearing_after ?? null,
            });
          }
        }
        return { distance: Math.round(r.distance), duration: Math.round(r.duration), line: r.geometry, steps };
      } catch { /* try the next server */ }
    }
    throw new Error('The walking route service could not be reached.');
  });
}

// The main Overpass servers are often full, so several are tried in turn.
const FOOD_OVERPASS = [
  'https://overpass.openstreetmap.fr/api/interpreter',
  'https://overpass-api.de/api/interpreter',
  'https://overpass.kumi.systems/api/interpreter',
  'https://overpass.private.coffee/api/interpreter',
];
const FOOD_AMENITY = 'cafe|restaurant|fast_food|pub|bar|food_court|ice_cream|biergarten';
const FOOD_KIND = {
  cafe: 'café', restaurant: 'restaurant', fast_food: 'fast food', pub: 'pub', bar: 'bar',
  food_court: 'food court', ice_cream: 'ice cream', biergarten: 'beer garden', bakery: 'bakery',
};

/** Named food places within `radius` metres of an encoded polyline, as in the desktop's Find Food. */
async function food(request) {
  let body;
  try {
    body = await request.json();
  } catch {
    return json({ error: 'Bad food search.' }, 400);
  }
  const line = typeof body.line === 'string' ? body.line.slice(0, 60000) : '';
  const pts = line ? decodePolyline(line).slice(0, 3000) : [];
  if (pts.length < 2 || pts.some(([a, b]) => !(Math.abs(a) <= 90 && Math.abs(b) <= 180))) {
    return json({ error: 'Bad food search.' }, 400);
  }
  const radius = Math.round(Math.max(20, Math.min(800, Number(body.radius) || 120)));
  const around = `(around:${radius},${pts.map(([a, b]) => `${a.toFixed(5)},${b.toFixed(5)}`).join(',')})`;
  const query = `[out:json][timeout:60];(nwr["amenity"~"^(${FOOD_AMENITY})$"]["name"]${around};`
    + `nwr["shop"="bakery"]["name"]${around};);out center tags 1500;`;
  let last = 'The food search service could not be reached.';
  for (const endpoint of FOOD_OVERPASS) {
    try {
      const res = await fetch(endpoint, {
        method: 'POST',
        headers: { 'User-Agent': USER_AGENT, 'Content-Type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams({ data: query }),
        signal: AbortSignal.timeout(70000),
      });
      if (!res.ok) { last = `The food search service answered ${res.status}.`; continue; }
      const data = await res.json();
      const places = [];
      for (const el of data.elements || []) {
        const t = el.tags || {};
        const lat = el.lat ?? el.center?.lat;
        const lon = el.lon ?? el.center?.lon;
        if (!t.name || !Number.isFinite(lat) || !Number.isFinite(lon)) continue;
        const cuisine = (t.cuisine || '').split(';')[0].replace(/_/g, ' ').trim();
        places.push({ id: `${el.type[0]}${el.id}`, name: t.name, kind: FOOD_KIND[t.amenity || t.shop] || 'food', cuisine, lat, lon });
      }
      return json({ places });
    } catch (err) {
      if (err && err.name === 'TimeoutError') last = 'The food search took too long.';
    }
  }
  return json({ error: last }, 502);
}
