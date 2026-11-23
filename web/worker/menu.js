// Map in a Box Web — menu lookup, used by worker/index.js. A port of the
// desktop's menu search (core.py _lookup_menu_links_for_poi and helpers).
//
//   GET /api/menu?name=&suburb=&website=   Menu links for one food venue,
//       cached for a week: { results: [{ kind: 'delivery' | 'own', title, url, pdf }] }
//   GET /api/menutext?url=                  The text of a venue's own menu page.
//
// A Google search (Serper, through the miab-menu-search Worker, which holds
// the key) for "Name" suburb menu Australia. Only two kinds of result are
// kept, so the wrong business is not picked: a delivery store page that
// names both the venue and its suburb, and a menu page on the venue's own
// website. The venue's site is also probed for common menu paths.

const SEARCH = 'https://miab-menu-search.miab.workers.dev';
const BROWSER_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36';

const DELIVERY_HOSTS = ['ubereats.com', 'doordash.com', 'menulog.com.au', 'menulog.com', 'deliveroo.com.au', 'grubhub.com'];
const MENU_SIGNALS = ['menu', 'menus', 'order', 'order-online', 'ordering', 'takeaway', 'take-away', 'click-and-collect'];
const PROBE_PATHS = ['/menu', '/menus', '/our-menu', '/food-menu', '/drinks-menu', '/order', '/order-online', '/menu.pdf'];
const NAME_STOPWORDS = new Set(['the', 'a', 'an', 'of', 'and', 'on', 'at', 'by', 'in', 'co', 'cafe', 'café', 'bar', 'grill', 'kitchen',
  'eatery', 'house', 'restaurant', 'bistro', 'diner', 'pizzeria', 'takeaway']);
const MULTI_SUFFIX = ['com.au', 'net.au', 'org.au', 'co.uk', 'org.uk', 'co.nz', 'com.sg', 'co.za', 'com.my'];
const FOREIGN_CC = new Set(['us', 'gb', 'uk', 'ca', 'ie', 'nz', 'fr', 'de', 'es', 'it', 'nl', 'jp', 'in', 'sg', 'za', 'mx', 'br']);

export function menuRoute(request, url, env, ctx) {
  if (request.method !== 'GET') return null;
  if (url.pathname === '/api/menu') return menu(url, env, ctx);
  if (url.pathname === '/api/menutext') return menuText(url, ctx);
  return null;
}

function json(body, status = 200, maxAge = 0) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', 'Cache-Control': maxAge ? `public, max-age=${maxAge}` : 'no-store' },
  });
}

const compactOf = (s) => String(s || '').toLowerCase().replace(/[^a-z0-9]+/g, '');

function hostOf(u) {
  try {
    const h = new URL(u).hostname.toLowerCase();
    return h.startsWith('www.') ? h.slice(4) : h;
  } catch {
    return '';
  }
}

/** 'In a Pickle' -> distinctive ['pickle'], compact 'inapickle' (as _venue_name_tokens). */
function nameTokens(name) {
  const toks = String(name || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').split(' ').filter(Boolean);
  return { distinctive: toks.filter((t) => !NAME_STOPWORDS.has(t) && t.length >= 4), compact: toks.join('') };
}

/** The registrable domain label: 'inapickle.com.au' -> 'inapickle' (as _main_label). */
function mainLabel(host) {
  let h = String(host || '').toLowerCase().replace(/^\.+|\.+$/g, '');
  if (h.startsWith('www.')) h = h.slice(4);
  for (const suf of MULTI_SUFFIX) {
    if (h.endsWith(`.${suf}`)) {
      const base = h.slice(0, -(suf.length + 1));
      return base ? base.split('.').pop() : '';
    }
  }
  const parts = h.split('.');
  return parts.length >= 2 ? parts[parts.length - 2] : h;
}

function labelMatchesVenue(label, t) {
  label = String(label || '').toLowerCase();
  if (!label) return false;
  if (t.compact && t.compact.length >= 4 && label.includes(t.compact)) return true;
  return t.distinctive.some((w) => label.includes(w));
}

const isDelivery = (host) => DELIVERY_HOSTS.some((d) => host === d || host.endsWith(`.${d}`));

/** Delivery store pages clearly in another country (as _delivery_wrong_country). */
function wrongCountry(host, path) {
  if (host.endsWith('menulog.com.au')) return false;
  const first = (path.split('/').filter(Boolean)[0] || '').toLowerCase();
  if (!first) return false;
  const cc = first.includes('-') ? first.split('-').pop() : first;
  return FOREIGN_CC.has(cc);
}

/** 'delivery', 'own' or null (drop), as _classify_menu_result. */
function classify(item, t, suburb) {
  const host = hostOf(item.url);
  if (!host) return null;
  let path = '';
  try { path = new URL(item.url).pathname.toLowerCase(); } catch { return null; }
  if (isDelivery(host)) {
    if (wrongCountry(host, path)) return null;
    // Delivery sites hold thousands of venues and branches: keep a page only
    // when it names this venue and this suburb.
    const text = compactOf(`${item.title} ${item.url} ${item.snippet}`);
    const venue = (t.compact && text.includes(t.compact)) || t.distinctive.some((w) => text.includes(w));
    const place = !suburb || text.includes(compactOf(suburb));
    return venue && place ? 'delivery' : null;
  }
  if (labelMatchesVenue(mainLabel(host), t)) {
    const hay = `${path} ${String(item.title || '').toLowerCase()}`;
    if (MENU_SIGNALS.some((s) => hay.includes(s))) return 'own';
  }
  return null;
}

async function search(env, q) {
  const params = new URLSearchParams({ q, num: '10', gl: 'au' });
  const fetcher = env.MENU_SEARCH ? (u, o) => env.MENU_SEARCH.fetch(u, o) : fetch;
  const res = await fetcher(`${SEARCH}?${params}`, {
    headers: { 'User-Agent': BROWSER_UA, Accept: 'application/json' },
    signal: AbortSignal.timeout(12000),
  });
  if (!res.ok) throw new Error(`search ${res.status}`);
  const data = await res.json();
  return (data.results || []).filter((r) => r && r.url).map((r) => ({ title: (r.title || r.url).trim(), url: r.url.trim(), snippet: (r.snippet || '').trim() }));
}

/** GET a candidate menu page: { ok, url, title, pdf } (as _probe_url). */
async function probe(u, home = '', needMenuPath = true) {
  try {
    const res = await fetch(u, { headers: { 'User-Agent': BROWSER_UA }, redirect: 'follow', signal: AbortSignal.timeout(7000) });
    if (!res.ok) return { ok: false };
    const final = res.url || u;
    if (home && final.toLowerCase().replace(/\/$/, '') === home.toLowerCase().replace(/\/$/, '')) return { ok: false };
    const path = new URL(final).pathname.toLowerCase();
    const pdf = (res.headers.get('Content-Type') || '').toLowerCase().includes('pdf') || path.endsWith('.pdf');
    if (needMenuPath && !pdf && !['menu', 'order', 'takeaway'].some((s) => path.includes(s))) return { ok: false };
    let title = '';
    if (!pdf) {
      const html = (await res.text()).slice(0, 200000);
      const m = html.match(/<title[^>]*>([\s\S]*?)<\/title>/i);
      if (m) title = m[1].replace(/\s+/g, ' ').trim().slice(0, 120);
    }
    return { ok: true, url: final, title, pdf };
  } catch {
    return { ok: false };
  }
}

/** The venue's own site: common menu paths, then its homepage's own menu links. */
async function probeSite(host, name) {
  const base = `https://${host}`;
  const tries = await Promise.all(PROBE_PATHS.map((p) => probe(base + p)));
  const found = [];
  for (const r of tries) {
    if (r.ok && !found.some((f) => f.url.toLowerCase() === r.url.toLowerCase())) found.push(r);
    if (found.length >= 3) break;
  }
  if (found.length) return found.map((r) => ({ title: r.title || `${name} menu`, url: r.url, snippet: '', pdf: r.pdf }));
  // A menu on a non-standard path: follow the homepage's own Menu links.
  let html = '';
  let home = base;
  try {
    const res = await fetch(base, { headers: { 'User-Agent': BROWSER_UA }, signal: AbortSignal.timeout(8000) });
    home = res.url || base;
    html = (await res.text()).slice(0, 400000);
  } catch {
    return [];
  }
  const homeLabel = mainLabel(hostOf(home));
  const cands = [];
  for (const m of html.matchAll(/<a\b[^>]*href\s*=\s*["']([^"']+)["'][^>]*>([\s\S]*?)<\/a>/gi)) {
    const text = m[2].replace(/<[^>]+>/g, ' ');
    if (!['menu', 'order online', 'order now', 'takeaway'].some((k) => `${m[1]} ${text}`.toLowerCase().includes(k))) continue;
    let u;
    try { u = new URL(m[1], home).href; } catch { continue; }
    if (!/^https?:/i.test(u) || mainLabel(hostOf(u)) !== homeLabel) continue;
    if (!cands.includes(u)) cands.push(u);
  }
  const out = [];
  for (const u of cands.slice(0, 6)) {
    const r = await probe(u, home, false);
    if (r.ok && !out.some((f) => f.url === r.url)) out.push({ title: r.title || `${name} menu`, url: r.url, snippet: '', pdf: r.pdf });
    if (out.length >= 3) break;
  }
  return out;
}

async function menu(url, env, ctx) {
  const name = (url.searchParams.get('name') || '').trim().slice(0, 120);
  const suburb = (url.searchParams.get('suburb') || '').trim().slice(0, 80);
  const website = (url.searchParams.get('website') || '').trim().slice(0, 300);
  if (!name) return json({ error: 'Bad request' }, 400);
  const cache = caches.default;
  const key = new Request(`https://cache.mapinabox/menu/v3/${encodeURIComponent(name.toLowerCase())}/${encodeURIComponent(suburb.toLowerCase())}`);
  const hit = await cache.match(key);
  if (hit) return hit;

  const t = nameTokens(name);
  let raw = [];
  try {
    raw = await search(env, `"${name}"${suburb ? ` ${suburb}` : ''} menu Australia`);
  } catch { /* still probe the website below */ }
  const seen = new Set();
  let results = [];
  for (const item of raw) {
    // One page in two languages (doordash.com/store/... and /en-AU/store/...) is one result.
    const k = item.url.toLowerCase().split('?')[0].replace(/\/$/, '').replace(/\/[a-z]{2}-[a-z]{2}\//, '/');
    if (seen.has(k)) continue;
    seen.add(k);
    const kind = classify(item, t, suburb);
    if (kind) results.push({ kind, title: item.title, url: item.url, pdf: /\.pdf($|\?)/i.test(item.url) });
  }
  // No menu page on the venue's own site in the search: look on the site itself.
  if (!results.some((r) => r.kind === 'own')) {
    const hosts = [];
    if (website) hosts.push(hostOf(website));
    for (const item of raw) {
      const h = hostOf(item.url);
      if (h && !isDelivery(h) && labelMatchesVenue(mainLabel(h), t) && !hosts.includes(h)) hosts.push(h);
    }
    for (const h of hosts.filter(Boolean).slice(0, 2)) {
      const hits = await probeSite(h, name);
      if (hits.length) {
        results = results.concat(hits.map((r) => ({ kind: 'own', title: r.title, url: r.url, pdf: !!r.pdf })));
        break;
      }
    }
  }
  results.sort((a, b) => (a.kind === b.kind ? 0 : a.kind === 'own' ? -1 : 1));
  const out = json({ results: results.slice(0, 6), searched: raw.length }, 200, results.length ? 7 * 24 * 3600 : 0);
  if (results.length) ctx.waitUntil(cache.put(key, out.clone())); // an empty answer may be a passing failure
  return out;
}

/** The readable text of a menu page on a venue's own site (not delivery sites, which block it). */
async function menuText(url, ctx) {
  const target = url.searchParams.get('url') || '';
  let u;
  try { u = new URL(target); } catch { return json({ error: 'Bad request' }, 400); }
  if (!/^https?:$/.test(u.protocol) || isDelivery(hostOf(u.href))) return json({ error: 'Not readable' }, 400);
  const cache = caches.default;
  const key = new Request(`https://cache.mapinabox/menutext/v1/${encodeURIComponent(u.href)}`);
  const hit = await cache.match(key);
  if (hit) return hit;
  let html;
  try {
    const res = await fetch(u.href, { headers: { 'User-Agent': BROWSER_UA }, signal: AbortSignal.timeout(10000) });
    if (!res.ok || (res.headers.get('Content-Type') || '').includes('pdf')) return json({ error: 'Not readable' }, 502);
    html = (await res.text()).slice(0, 600000);
  } catch {
    return json({ error: 'Not readable' }, 502);
  }
  const text = html
    .replace(/<(script|style|noscript|svg|nav|footer|header)\b[\s\S]*?<\/\1>/gi, ' ')
    .replace(/<(br|\/p|\/div|\/li|\/h[1-6]|\/tr)\b[^>]*>/gi, '\n')
    .replace(/<[^>]+>/g, ' ')
    .replace(/&nbsp;/g, ' ').replace(/&amp;/g, '&').replace(/&#39;|&rsquo;/g, "'").replace(/&quot;/g, '"').replace(/&[a-z]+;/g, ' ')
    .split('\n').map((l) => l.replace(/\s+/g, ' ').trim()).filter((l) => l.length > 1)
    .join('\n').slice(0, 8000);
  const out = json({ text }, 200, 7 * 24 * 3600);
  ctx.waitUntil(cache.put(key, out.clone()));
  return out;
}
