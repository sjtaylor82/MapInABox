// Map in a Box Web — Cloudflare Worker.
//
// Serves the static site from ./public (the ASSETS binding) and a small API
// on the same domain, so a school only has to allow one address.
//
//   GET /api/health              { ok: true }
//   GET /api/search?q=&lat=&lon= Photon place search, cached for a week
//   GET /api/tile/14/x/y         One OpenFreeMap vector tile (OpenStreetMap
//                                streets and places), cached for a week
//   /api/journey, /api/flights, /api/road, /api/food  The journey planner
//                                (Rome2Rio, OSRM, Overpass): see journey.js

import { ZOOM, TILEJSON, validTile } from '../public/src/vectortile.js';
import { OVERPASS, detailsQuery, pickDetails } from '../public/src/placedetails.js';
import { AREA_SERVERS, areaQuery, areaData } from '../public/src/overpassarea.js';
import { journeyRoute } from './journey.js';
import { menuRoute } from './menu.js';

const PHOTON = 'https://photon.komoot.io/api/';
const SEARCH_TTL = 7 * 24 * 3600;

// Expert features answer only at expert.mapinabox.org (and on this computer
// while developing). mapinabox.org is Starter only, and schools block the
// expert address, so these can't be reached from a student's device.
const EXPERT_APIS = new Set(['/api/menu', '/api/menutext', '/api/menupdf', '/api/menuphotos', '/api/menuimage', '/api/venueaddr', '/api/stations', '/api/journey',
  '/api/flights', '/api/walk', '/api/road', '/api/routes', '/api/food', '/api/crossings']);
// wrangler dev presents requests as the first custom domain, so testing the
// Expert features on this computer needs `--var DEV_EXPERT:1`.
const isExpertHost = (url, env) => /^expert\./.test(url.hostname) || !!(env && env.DEV_EXPERT);

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (!url.pathname.startsWith('/api/')) return env.ASSETS.fetch(request);
    if (EXPERT_APIS.has(url.pathname) && !isExpertHost(url, env)) {
      return json({ error: 'This is part of Expert mode, at expert.mapinabox.org.' }, 403);
    }
    const menuAnswer = menuRoute(request, url, env, ctx);
    if (menuAnswer) return menuAnswer;
    const planned = journeyRoute(request, url, env, ctx);
    if (planned) return planned;
    if (request.method !== 'GET') return json({ error: 'Method not allowed' }, 405);

    if (url.pathname === '/api/health') return json({ ok: true });
    if (url.pathname === '/api/search') return search(url, ctx);
    if (url.pathname === '/api/reverse') return reverse(url, ctx);
    if (url.pathname === '/api/geocode') return geocode(url, env, ctx);
    const tile = url.pathname.match(/^\/api\/tile\/(\d+)\/(\d+)\/(\d+)$/);
    if (tile) return mapTile(Number(tile[1]), Number(tile[2]), Number(tile[3]), ctx);
    if (url.pathname === '/api/place') return placeDetails(url, env, ctx);
    if (url.pathname === '/api/venueaddr') return venueAddress(url, env, ctx);
    const area = url.pathname.match(/^\/api\/area\/(\d+)\/(\d+)\/(\d+)$/);
    if (area) return streetArea(Number(area[1]), Number(area[2]), Number(area[3]), ctx);
    return json({ error: 'Not found' }, 404);
  },
};

/**
 * GET /api/geocode?q=&lat=&lon=: places and street addresses for a typed
 * name, best first. OpenStreetMap's Nominatim first: in tests it found
 * street addresses (Ethel St, Burwood), stations and shopping centres where
 * Photon gave a bus stop or New Jersey, and HERE's geocoder turned
 * "Burwood Station" into Station St, Concord. HERE is the fallback.
 * lat and lon, when given, favour results near there. Cached a week (as
 * Nominatim's usage policy asks). Answers { results: [{ label, lat, lon }] }.
 */
async function geocode(url, env, ctx) {
  const q = (url.searchParams.get('q') || '').trim().slice(0, 200);
  if (!q) return json({ results: [] });
  const lat = Number(url.searchParams.get('lat'));
  const lon = Number(url.searchParams.get('lon'));
  const near = Number.isFinite(lat) && Number.isFinite(lon);
  const cache = caches.default;
  const key = new Request(`https://cache.mapinabox/geocode/v4?q=${encodeURIComponent(q.toLowerCase())}${near ? `&at=${lat.toFixed(2)},${lon.toFixed(2)}` : ''}`);
  const hit = await cache.match(key);
  if (hit) return hit;

  let results = [];
  try {
    const p = new URLSearchParams({ q, format: 'jsonv2', limit: '10', addressdetails: '1', 'accept-language': 'en' });
    if (near) p.set('viewbox', `${lon - 0.5},${lat + 0.5},${lon + 0.5},${lat - 0.5}`); // favoured, not a limit
    const res = await fetch(`https://nominatim.openstreetmap.org/search?${p}`, {
      headers: { 'User-Agent': USER_AGENT }, signal: AbortSignal.timeout(8000),
    });
    if (res.ok) {
      // "Vision Australia, 454 Glenferrie Road, Kooyong": name, street address, suburb.
      results = (await res.json()).map((r) => {
        const a = r.address || {};
        const road = [a.house_number, a.road || a.pedestrian || a.footway].filter(Boolean).join(' ');
        const area = a.suburb || a.town || a.village || a.city_district || a.city || '';
        const label = [r.name, road, area].filter((x, i, all) => x && all.indexOf(x) === i).join(', ')
          || r.display_name.split(', ').slice(0, 3).join(', ');
        return { label, lat: Number(r.lat), lon: Number(r.lon) };
      });
    }
  } catch { /* HERE below */ }

  // Few matches in OpenStreetMap (a business with several branches is
  // often only mapped once): add HERE's business listings near you.
  if (results.length && results.length < 5 && near && env.HERE_API_KEY) {
    try {
      const p = new URLSearchParams({ q, limit: '10', lang: 'en-AU', at: `${lat.toFixed(3)},${lon.toFixed(3)}`, apiKey: env.HERE_API_KEY });
      const res = await fetch(`https://discover.search.hereapi.com/v1/discover?${p}`, {
        headers: { 'User-Agent': USER_AGENT }, signal: AbortSignal.timeout(8000),
      });
      if (res.ok) {
        for (const it of (await res.json()).items || []) {
          if (!it.position) continue;
          const r = { label: [it.title, it.address && (it.address.street ? [it.address.houseNumber, it.address.street].filter(Boolean).join(' ') : ''), it.address && it.address.district || it.address && it.address.city].filter(Boolean).join(', '), lat: it.position.lat, lon: it.position.lng };
          const dup = results.some((x) => Math.abs(x.lat - r.lat) < 0.0006 && Math.abs(x.lon - r.lon) < 0.0006);
          if (!dup) results.push(r);
        }
      }
    } catch { /* OpenStreetMap's matches are enough */ }
  }

  if (!results.length && env.HERE_API_KEY) {
    const p = new URLSearchParams({ q, limit: '10', lang: 'en-AU', apiKey: env.HERE_API_KEY });
    if (near) p.set('at', `${lat.toFixed(3)},${lon.toFixed(3)}`);
    for (const endpoint of near ? ['geocode', 'discover'] : ['geocode']) {
      try {
        const res = await fetch(`https://${endpoint}.search.hereapi.com/v1/${endpoint}?${p}`, {
          headers: { 'User-Agent': USER_AGENT }, signal: AbortSignal.timeout(8000),
        });
        if (!res.ok) continue;
        results = ((await res.json()).items || []).filter((it) => it.position).map((it) => ({
          label: (it.address && it.address.label) || it.title, lat: it.position.lat, lon: it.position.lng,
        }));
        if (results.length) break;
      } catch { /* next */ }
    }
  }
  const out = new Response(JSON.stringify({ results }), {
    headers: { 'Content-Type': 'application/json', 'Cache-Control': 'public, max-age=604800' },
  });
  if (results.length) ctx.waitUntil(cache.put(key, out.clone()));
  return out;
}

/** GET /api/reverse?lat=&lon=: the address at a point (Photon reverse), cached a week. */
async function reverse(url, ctx) {
  const lat = Number(url.searchParams.get('lat'));
  const lon = Number(url.searchParams.get('lon'));
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) return json({ error: 'Bad request' }, 400);
  const upstream = `https://photon.komoot.io/reverse?lat=${lat.toFixed(5)}&lon=${lon.toFixed(5)}&lang=en`;
  const cache = caches.default;
  const key = new Request(upstream);
  const hit = await cache.match(key);
  if (hit) return hit;
  const res = await fetch(upstream, { headers: { 'User-Agent': USER_AGENT } });
  if (!res.ok) return json({ error: `Address service answered ${res.status}` }, 502);
  const out = new Response(res.body, {
    headers: { 'Content-Type': 'application/json', 'Cache-Control': `public, max-age=${SEARCH_TTL}` },
  });
  ctx.waitUntil(cache.put(key, out.clone()));
  return out;
}

async function search(url, ctx) {
  const q = (url.searchParams.get('q') || '').trim().slice(0, 200);
  if (!q) return json({ features: [] });
  const params = new URLSearchParams({ q, limit: clampInt(url.searchParams.get('limit'), 1, 20, 10) });
  const lang = url.searchParams.get('lang');
  if (['en', 'de', 'fr', 'default'].includes(lang)) params.set('lang', lang);
  const lat = Number(url.searchParams.get('lat'));
  const lon = Number(url.searchParams.get('lon'));
  if (url.searchParams.has('lat') && Number.isFinite(lat) && Number.isFinite(lon)) {
    // Rounded, so nearby searches share the cache and the exact spot is not sent.
    params.set('lat', lat.toFixed(1));
    params.set('lon', lon.toFixed(1));
    // Nearer first among similar matches, but famous places still win (Paris is in France).
    params.set('location_bias_scale', '0.9');
  }

  const upstream = `${PHOTON}?${params}`;
  const cache = caches.default;
  const key = new Request(upstream);
  const hit = await cache.match(key);
  if (hit) return hit;

  const res = await fetch(upstream, { headers: { 'User-Agent': USER_AGENT } });
  if (!res.ok) return json({ error: `Search service answered ${res.status}` }, 502);
  const out = new Response(res.body, {
    headers: { 'Content-Type': 'application/json', 'Cache-Control': `public, max-age=${SEARCH_TTL}` },
  });
  ctx.waitUntil(cache.put(key, out.clone()));
  return out;
}

const TILE_TTL = 7 * 24 * 3600;
const USER_AGENT = 'MapInABoxWeb/0.1 (+https://github.com/sjtaylor82/MapInABox)';
let tileTemplate = null;
let templateAt = 0;

/** The current OpenFreeMap tile address, from its TileJSON (it changes with each weekly build). */
async function tileUrl(z, x, y) {
  if (!tileTemplate || Date.now() - templateAt > 3600 * 1000) {
    const res = await fetch(TILEJSON, { headers: { 'User-Agent': USER_AGENT } });
    if (!res.ok) throw new Error(`TileJSON answered ${res.status}`);
    tileTemplate = (await res.json()).tiles[0];
    templateAt = Date.now();
  }
  return tileTemplate.replace('{z}', z).replace('{x}', x).replace('{y}', y);
}

async function mapTile(z, x, y, ctx) {
  if (z !== ZOOM || !validTile(y, x)) return json({ error: 'Bad tile' }, 400);
  const cache = caches.default;
  const key = new Request(`https://cache.mapinabox/tile/v1/${z}/${x}/${y}`);
  const hit = await cache.match(key);
  if (hit) return hit;
  try {
    const res = await fetch(await tileUrl(z, x, y), { headers: { 'User-Agent': USER_AGENT } });
    if (!res.ok) return json({ error: `The street map service answered ${res.status}.` }, 502);
    const out = new Response(res.body, {
      headers: {
        'Content-Type': 'application/vnd.mapbox-vector-tile',
        'Cache-Control': `public, max-age=${TILE_TTL}`,
      },
    });
    ctx.waitUntil(cache.put(key, out.clone()));
    return out;
  } catch {
    return json({ error: 'The street map service could not be reached.' }, 502);
  }
}

const AREA_TTL = 7 * 24 * 3600;

/**
 * GET /api/area/14/x/y: streets, places (with phone, hours and website) and
 * house numbers for one tile, from Overpass. The French server answers only
 * clients that identify themselves, which the Worker does and a browser
 * cannot. On failure the page falls back to OpenFreeMap tiles.
 */
async function streetArea(z, x, y, ctx) {
  if (z !== ZOOM || !validTile(y, x)) return json({ error: 'Bad tile' }, 400);
  const cache = caches.default;
  const key = new Request(`https://cache.mapinabox/area/v1/${z}/${x}/${y}`);
  const hit = await cache.match(key);
  if (hit) return hit;
  const body = new URLSearchParams({ data: areaQuery(y, x) });
  const deadline = Date.now() + 25000;
  for (const server of AREA_SERVERS) {
    const left = deadline - Date.now();
    if (left < 3000) break;
    try {
      const res = await fetch(server, {
        method: 'POST',
        body,
        headers: { 'User-Agent': USER_AGENT, 'Content-Type': 'application/x-www-form-urlencoded' },
        signal: AbortSignal.timeout(left),
      });
      if (!res.ok) continue;
      const data = await res.json();
      if (data.remark && /runtime error|timed out/i.test(data.remark)) continue;
      const out = new Response(JSON.stringify(areaData(data)), {
        headers: { 'Content-Type': 'application/json', 'Cache-Control': `public, max-age=${AREA_TTL}` },
      });
      ctx.waitUntil(cache.put(key, out.clone()));
      return out;
    } catch { /* next server */ }
  }
  return json({ error: 'The street data service is busy.' }, 502);
}

const DETAILS_TTL = 24 * 3600;

/** GET /api/place?name=&lat=&lon=: phone, hours, website and address for one place. */
async function placeDetails(url, env, ctx) {
  const name = (url.searchParams.get('name') || '').trim().slice(0, 200);
  const lat = Number(url.searchParams.get('lat'));
  const lon = Number(url.searchParams.get('lon'));
  if (!name || !Number.isFinite(lat) || !Number.isFinite(lon)) return json({ error: 'Bad request' }, 400);
  const cache = caches.default;
  const key = new Request(`https://cache.mapinabox/place/v3/${encodeURIComponent(name)}/${lat.toFixed(4)}/${lon.toFixed(4)}`);
  const hit = await cache.match(key);
  if (hit) return hit;

  // OpenStreetMap first.
  let osm = null;
  let osmAnswered = false;
  const body = new URLSearchParams({ data: detailsQuery(name, lat, lon) });
  for (const mirror of OVERPASS) {
    try {
      const res = await fetch(mirror, {
        method: 'POST',
        body,
        headers: { 'User-Agent': USER_AGENT, 'Content-Type': 'application/x-www-form-urlencoded' },
        signal: AbortSignal.timeout(10000),
      });
      if (!res.ok) continue;
      osm = pickDetails(await res.json());
      osmAnswered = true;
      break;
    } catch { /* next */ }
  }

  // HERE fills in the phone, hours, website and address OpenStreetMap lacks.
  let details = osm ? { ...osm } : null;
  if (env.HERE_API_KEY && !(osm && osm.phone && osm.hours)) {
    const here = await hereDetails(env.HERE_API_KEY, name, lat, lon).catch(() => null);
    if (here) {
      details = details || { phone: '', hours: '', website: '', address: '', cuisine: '', wheelchair: '', delivery: '', takeaway: '' };
      details.phone ||= here.phone;
      details.website ||= here.website;
      details.address ||= here.address;
      if (!details.hours && here.hoursText) details.hoursText = here.hoursText;
      details.source = osm ? 'openstreetmap and here' : 'here';
    }
  }
  if (!osmAnswered && !details) return json({ error: 'The place details service is busy.' }, 502);
  const out = new Response(JSON.stringify({ details }), {
    headers: { 'Content-Type': 'application/json', 'Cache-Control': `public, max-age=${DETAILS_TTL}` },
  });
  ctx.waitUntil(cache.put(key, out.clone()));
  return out;
}

/** Word overlap between two names, ignoring filler words (as here_poi._name_similarity). */
function nameSimilarity(query, result) {
  const filler = new Set(['the', 'a', 'an', 'and', 'of', 'at', 'in', 'on', 'by', 'for']);
  const words = (s) => new Set(String(s).toLowerCase().replace(/[^a-z0-9\s]/g, '').split(/\s+/)
    .filter((w) => w.length > 1 && !filler.has(w)));
  const q = words(query);
  const r = words(result);
  if (!q.size) return 1;
  let both = 0;
  for (const w of q) if (r.has(w)) both += 1;
  return both / q.size;
}

/** HERE Discover, as the desktop's here_poi.py: the best match within 150 metres. */
async function hereDetails(apiKey, name, lat, lon) {
  const params = new URLSearchParams({ at: `${lat},${lon}`, q: name, limit: '3', lang: 'en-US', apiKey });
  const res = await fetch(`https://discover.search.hereapi.com/v1/discover?${params}`, {
    headers: { 'User-Agent': USER_AGENT },
    signal: AbortSignal.timeout(8000),
  });
  if (!res.ok) return null;
  const data = await res.json();
  const item = (data.items || []).find((c) => {
    const p = c.position || {};
    const dy = ((p.lat ?? lat) - lat) * 111000;
    const dx = ((p.lng ?? lon) - lon) * 111000 * Math.cos((lat * Math.PI) / 180);
    return nameSimilarity(name, c.title || '') >= 0.4 && Math.hypot(dx, dy) <= 150;
  });
  if (!item) return null;
  let phone = '';
  let website = '';
  for (const ct of item.contacts || []) {
    if (!phone && ct.phone && ct.phone[0]) phone = ct.phone[0].value || '';
    if (!website && ct.www && ct.www[0]) website = ct.www[0].value || '';
  }
  const oh = (item.openingHours || [])[0];
  let hoursText = '';
  if (oh) {
    // Not HERE's "open now": answers are kept for a day, so it would go stale.
    hoursText = (oh.text || []).join('; ');
  }
  const a = item.address || {};
  // "164 Cavendish Road": just the street address, abbreviations spelt out.
  const streetName = String(a.street || '').replace(/\b([A-Za-z]+)\.?$/, (w, x) => STREET_TYPES[x.toLowerCase()] || w);
  const street = a.houseNumber && streetName ? `${a.houseNumber} ${streetName}` : '';
  return { phone, website, hoursText, address: a.label || '', street };
}

const STREET_TYPES = {
  rd: 'Road', st: 'Street', ave: 'Avenue', av: 'Avenue', pde: 'Parade', hwy: 'Highway', dr: 'Drive', tce: 'Terrace',
  ct: 'Court', pl: 'Place', cres: 'Crescent', cr: 'Crescent', blvd: 'Boulevard', bvd: 'Boulevard', ln: 'Lane',
  cl: 'Close', esp: 'Esplanade', cct: 'Circuit', gr: 'Grove', sq: 'Square', mall: 'Mall', way: 'Way', wy: 'Way',
};

/**
 * GET /api/venueaddr?name=&lat=&lon=  A shop's or cafe's street address from
 * HERE: { addr: "164 Cavendish Road" } ('' when unknown). For lists of places
 * (Menus nearby), where the nearest house number on the map is often the
 * shop next door.
 */
async function venueAddress(url, env, ctx) {
  const name = (url.searchParams.get('name') || '').trim().slice(0, 200);
  const lat = Number(url.searchParams.get('lat'));
  const lon = Number(url.searchParams.get('lon'));
  if (!name || !Number.isFinite(lat) || !Number.isFinite(lon)) return json({ error: 'Bad request' }, 400);
  const cache = caches.default;
  const key = new Request(`https://cache.mapinabox/venueaddr/v2/${encodeURIComponent(name)}/${lat.toFixed(4)}/${lon.toFixed(4)}`);
  const hit = await cache.match(key);
  if (hit) return hit;
  if (!env.HERE_API_KEY) return json({ addr: '' });
  let here;
  try {
    here = await hereDetails(env.HERE_API_KEY, name, lat, lon);
  } catch {
    return json({ error: 'The address service is busy.' }, 502);
  }
  const out = new Response(JSON.stringify({ addr: (here && here.street) || '' }), {
    headers: { 'Content-Type': 'application/json', 'Cache-Control': `public, max-age=${30 * 24 * 3600}` },
  });
  ctx.waitUntil(cache.put(key, out.clone()));
  return out;
}

function clampInt(v, min, max, dflt) {
  const n = Number.parseInt(v, 10);
  return String(Number.isFinite(n) ? Math.max(min, Math.min(max, n)) : dflt);
}

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' },
  });
}
