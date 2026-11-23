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

const PHOTON = 'https://photon.komoot.io/api/';
const SEARCH_TTL = 7 * 24 * 3600;

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (!url.pathname.startsWith('/api/')) return env.ASSETS.fetch(request);
    const planned = journeyRoute(request, url, env, ctx);
    if (planned) return planned;
    if (request.method !== 'GET') return json({ error: 'Method not allowed' }, 405);

    if (url.pathname === '/api/health') return json({ ok: true });
    if (url.pathname === '/api/search') return search(url, ctx);
    const tile = url.pathname.match(/^\/api\/tile\/(\d+)\/(\d+)\/(\d+)$/);
    if (tile) return mapTile(Number(tile[1]), Number(tile[2]), Number(tile[3]), ctx);
    if (url.pathname === '/api/place') return placeDetails(url, ctx);
    const area = url.pathname.match(/^\/api\/area\/(\d+)\/(\d+)\/(\d+)$/);
    if (area) return streetArea(Number(area[1]), Number(area[2]), Number(area[3]), ctx);
    return json({ error: 'Not found' }, 404);
  },
};

async function search(url, ctx) {
  const q = (url.searchParams.get('q') || '').trim().slice(0, 200);
  if (!q) return json({ features: [] });
  const params = new URLSearchParams({ q, limit: clampInt(url.searchParams.get('limit'), 1, 20, 10) });
  const lang = url.searchParams.get('lang');
  if (['en', 'de', 'fr', 'default'].includes(lang)) params.set('lang', lang);
  const lat = Number(url.searchParams.get('lat'));
  const lon = Number(url.searchParams.get('lon'));
  if (Number.isFinite(lat) && Number.isFinite(lon)) {
    // Rounded, so nearby searches share the cache and the exact spot is not sent.
    params.set('lat', lat.toFixed(1));
    params.set('lon', lon.toFixed(1));
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
async function placeDetails(url, ctx) {
  const name = (url.searchParams.get('name') || '').trim().slice(0, 200);
  const lat = Number(url.searchParams.get('lat'));
  const lon = Number(url.searchParams.get('lon'));
  if (!name || !Number.isFinite(lat) || !Number.isFinite(lon)) return json({ error: 'Bad request' }, 400);
  const cache = caches.default;
  const key = new Request(`https://cache.mapinabox/place/v1/${encodeURIComponent(name)}/${lat.toFixed(4)}/${lon.toFixed(4)}`);
  const hit = await cache.match(key);
  if (hit) return hit;
  const body = new URLSearchParams({ data: detailsQuery(name, lat, lon) });
  for (const mirror of OVERPASS) {
    try {
      const res = await fetch(mirror, {
        method: 'POST',
        body,
        headers: { 'User-Agent': USER_AGENT, 'Content-Type': 'application/x-www-form-urlencoded' },
        signal: AbortSignal.timeout(12000),
      });
      if (!res.ok) continue;
      const out = new Response(JSON.stringify({ details: pickDetails(await res.json()) }), {
        headers: { 'Content-Type': 'application/json', 'Cache-Control': `public, max-age=${DETAILS_TTL}` },
      });
      ctx.waitUntil(cache.put(key, out.clone()));
      return out;
    } catch { /* next */ }
  }
  return json({ error: 'The place details service is busy.' }, 502);
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
