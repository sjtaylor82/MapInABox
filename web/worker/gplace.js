// Map in a Box Web — a place on Google Maps, for Jump (Expert), used by
// worker/index.js.
//
//   GET /api/gplace?q=&lat=&lon=   The place called q nearest lat,lon on Google
//       Maps: { result: { label, lat, lon, food } } or { result: null }.
//
// For places OpenStreetMap doesn't have, so Jump's usual search can't find
// them (Sorai, Carina: on Uber Eats and Google, not on the map). Asked through
// the miab-menu-search Worker, which holds the Serper key and keeps answers
// for 30 days; each new question counts against its daily cap, so Jump only
// asks when its own search found nothing by that name nearby.

const SEARCH = 'https://miab-menu-search.miab.workers.dev';
const BROWSER_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36';
const FOOD = /restaurant|cafe|café|coffee|bar\b|pub\b|bakery|takeaway|take away|pizza|sushi|burger|bistro|diner|eatery|food|kitchen|grill|dessert|ice cream|brewery|tavern/i;
const STOP = new Set(['the', 'a', 'an', 'and', 'of', 'at', 'on', 'in']);

const words = (s) => String(s || '').toLowerCase().normalize('NFKD').replace(/[̀-ͯ]/g, '')
  .replace(/[^a-z0-9]+/g, ' ').split(' ').filter((w) => w && !STOP.has(w));

/**
 * Google gives its nearest guess, which may be another place ("Marinara" near
 * Carina gave La Regina). Keep it only when it is what was typed: every word
 * of its name was typed ("Sorai" for "sorai carina"), or the first word typed
 * is in its name ("sorai" for "Sorai Restaurant & Bar").
 */
function sameName(title, q) {
  const t = words(title);
  const typed = words(q);
  if (!t.length || !typed.length) return false;
  return t.every((w) => typed.includes(w)) || (typed[0].length >= 3 && t.includes(typed[0]));
}

function json(body, status = 200, maxAge = 0) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', 'Cache-Control': maxAge ? `public, max-age=${maxAge}` : 'no-store' },
  });
}

export async function googlePlace(url, env) {
  const q = (url.searchParams.get('q') || '').trim().slice(0, 120);
  const lat = Number(url.searchParams.get('lat'));
  const lon = Number(url.searchParams.get('lon'));
  if (!q || !Number.isFinite(lat) || !Number.isFinite(lon) || Math.abs(lat) > 90 || Math.abs(lon) > 180) {
    return json({ error: 'Bad request' }, 400);
  }
  const params = new URLSearchParams({ q, type: 'places', gl: 'au', ll: `${lat.toFixed(3)},${lon.toFixed(3)}` });
  const fetcher = env.MENU_SEARCH ? (u, o) => env.MENU_SEARCH.fetch(u, o) : fetch;
  let p = null;
  try {
    const res = await fetcher(`${SEARCH}?${params}`, {
      headers: { 'User-Agent': BROWSER_UA, Accept: 'application/json' },
      signal: AbortSignal.timeout(12000),
    });
    if (!res.ok) return json({ result: null, error: `search ${res.status}` }, 502);
    p = (await res.json()).place;
  } catch {
    return json({ result: null, error: 'search failed' }, 502);
  }
  if (!p || !Number.isFinite(p.lat) || !Number.isFinite(p.lon) || !sameName(p.title, q)) {
    return json({ result: null }, 200, 7 * 24 * 3600);
  }
  // "Sorai, 850 Old Cleveland Rd, Carina QLD 4152": the name and its street address.
  const address = String(p.address || '').replace(/,\s*Australia$/, '');
  const label = [p.title, address].filter(Boolean).join(', ');
  return json({ result: { label, lat: p.lat, lon: p.lon, food: FOOD.test(p.category || ''), website: p.website || '' } }, 200, 7 * 24 * 3600);
}
