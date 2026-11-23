// Map in a Box Web — Cloudflare Worker.
//
// Serves the static site from ./public (the ASSETS binding) and a small API
// on the same domain, so a school only has to allow one address.
//
//   GET /api/health              { ok: true }
//   GET /api/search?q=&lat=&lon= Photon place search, cached for a week
//   GET /api/streets?ty=&tx=     Streets and places for one street tile
//                                (OpenStreetMap via Overpass), cached for two weeks

import { buildQuery, compact, validTile, OVERPASS_MIRRORS } from '../public/src/streetquery.js';

const PHOTON = 'https://photon.komoot.io/api/';
const SEARCH_TTL = 7 * 24 * 3600;

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (!url.pathname.startsWith('/api/')) return env.ASSETS.fetch(request);
    if (request.method !== 'GET') return json({ error: 'Method not allowed' }, 405);

    if (url.pathname === '/api/health') return json({ ok: true });
    if (url.pathname === '/api/search') return search(url, ctx);
    if (url.pathname === '/api/streets') return streets(url, ctx);
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

const STREETS_TTL = 14 * 24 * 3600;
const USER_AGENT = 'MapInABoxWeb/0.1 (+https://github.com/sjtaylor82/MapInABox)';

async function streets(url, ctx) {
  const ty = Number(url.searchParams.get('ty'));
  const tx = Number(url.searchParams.get('tx'));
  if (!validTile(ty, tx)) return json({ error: 'Bad tile' }, 400);

  const cache = caches.default;
  const key = new Request(`https://cache.mapinabox/streets/v1/${ty}/${tx}`);
  const hit = await cache.match(key);
  if (hit) return hit;

  const body = new URLSearchParams({ data: buildQuery(ty, tx) });
  // The public Overpass servers are often busy, and a dense city tile can
  // take 30 to 50 seconds. Try each in turn, twice, within about 85 seconds
  // (Cloudflare gives up on a response after 100).
  const deadline = Date.now() + 85000;
  let lastError = 'no answer';
  for (let round = 0; round < 2; round++) {
    for (const mirror of OVERPASS_MIRRORS) {
      const left = deadline - Date.now();
      if (left < 5000) break;
      try {
        const res = await fetch(mirror, {
          method: 'POST',
          body,
          headers: { 'User-Agent': USER_AGENT, 'Content-Type': 'application/x-www-form-urlencoded' },
          signal: AbortSignal.timeout(Math.min(70000, left)),
        });
        if (!res.ok) {
          lastError = res.status === 429 || res.status >= 502 ? 'is busy' : `answered ${res.status}`;
          continue;
        }
        const data = compact(await res.json());
        const out = new Response(JSON.stringify(data), {
          headers: { 'Content-Type': 'application/json', 'Cache-Control': `public, max-age=${STREETS_TTL}` },
        });
        ctx.waitUntil(cache.put(key, out.clone()));
        return out;
      } catch (err) {
        lastError = err.name === 'TimeoutError' ? 'took too long' : 'could not be reached';
      }
    }
    if (round === 0) await new Promise((r) => setTimeout(r, 2000));
  }
  return json({ error: `The street data service ${lastError}. Try again in a minute.` }, 502);
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
