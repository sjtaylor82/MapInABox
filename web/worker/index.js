// Map in a Box Web — Cloudflare Worker.
//
// Serves the static site from ./public (the ASSETS binding) and a small API
// on the same domain, so a school only has to allow one address.
//
//   GET /api/health              { ok: true }
//   GET /api/search?q=&lat=&lon= Photon place search, cached for a week
//   GET /api/tile/14/x/y         One OpenFreeMap vector tile (OpenStreetMap
//                                streets and places), cached for a week

import { ZOOM, TILEJSON, validTile } from '../public/src/vectortile.js';

const PHOTON = 'https://photon.komoot.io/api/';
const SEARCH_TTL = 7 * 24 * 3600;

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (!url.pathname.startsWith('/api/')) return env.ASSETS.fetch(request);
    if (request.method !== 'GET') return json({ error: 'Method not allowed' }, 405);

    if (url.pathname === '/api/health') return json({ ok: true });
    if (url.pathname === '/api/search') return search(url, ctx);
    const tile = url.pathname.match(/^\/api\/tile\/(\d+)\/(\d+)\/(\d+)$/);
    if (tile) return mapTile(Number(tile[1]), Number(tile[2]), Number(tile[3]), ctx);
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
