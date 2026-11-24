// Map in a Box Web — menu lookup, used by worker/index.js. A port of the
// desktop's menu search (core.py _lookup_menu_links_for_poi and helpers).
//
//   GET /api/menu?name=&suburb=&website=   Menu links for one food venue,
//       cached for a week: { results: [{ kind: 'delivery' | 'own', title, url, pdf }] }
//   GET /api/menutext?url=                  A venue's own menu page as sections of
//       dishes with prices: { text, sections, menu }
//   GET /api/menupdf?url=                   A venue's PDF menu, passed through so the
//       browser can read it (pdf.js); PDF hosts don't allow that directly.
//
// A Google search (Serper, through the miab-menu-search Worker, which holds
// the key) for "Name" suburb menu Australia. Only two kinds of result are
// kept, so the wrong business is not picked: a delivery store page that
// names both the venue and its suburb, and a menu page on the venue's own
// website. The venue's site is also probed for common menu paths.

import { htmlToText, looksLikeMenu, structure, pricedCount } from '../public/src/menustructure.js';

const SEARCH = 'https://miab-menu-search.miab.workers.dev';
const BROWSER_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36';

const DELIVERY_HOSTS = ['ubereats.com', 'doordash.com', 'menulog.com.au', 'menulog.com', 'deliveroo.com.au', 'grubhub.com'];
const MENU_SIGNALS = ['menu', 'menus', 'order', 'order-online', 'ordering', 'takeaway', 'take-away', 'click-and-collect'];
const PROBE_PATHS = ['/menu', '/menus', '/our-menu', '/food-menu', '/food', '/drinks-menu', '/order-online', '/menu.pdf'];
// From the menu browser prototype (menus.py): how a link to a menu reads.
const MENU_WORD = /menu|carte/i;
const MEAL_WORDS = /\b(food|drinks?|wine list|eat|lunch|dinner|breakfast|brunch|dessert|cocktails?|takeaway)\b/i;
const NOT_MENU = /press|news|blog|marketing|career|job|gift|voucher|book|reserv|event|function|wedding|contact|about|privacy|terms|login|cart|shop|instagram|facebook|birthday|party|parties|christmas|venue|near me|best /i;
const DINING = /restaurant|dining|dine|eat|bistro|cafe|kitchen|pizzeria|trattoria|our food/i;
// Words too common to pick out a chain's branch.
const BRANCH_STOP = new Set(['restaurant', 'restaurants', 'cafe', 'coffee', 'kitchen', 'hotel', 'bar', 'pizza', 'pizzeria',
  'indian', 'thai', 'chinese', 'italian', 'japanese', 'sushi', 'burger', 'burgers', 'grill', 'the', 'and', 'house', 'express',
  'shop', 'store', 'eatery', 'bistro', 'takeaway', 'food', 'north', 'south', 'east', 'west', 'city', 'central', 'street', 'road']);
// Parked or for-sale domains (from the prototype's sitecheck.py).
const PARKED_TEXT = /domain (name )?(is|may be) for sale|buy this domain|this domain (has )?expired|domain (is )?parked|parked (free|domain)|account (has been )?suspended/i;
// Sites that refuse automatic visitors but work for a person.
const ALIVE_ANYWAY = new Set([401, 403, 405, 429, 503]);
const SOFT_404 = /\b404\b|not found|page (can.?t|cannot|could not) be found|no longer exists|page doesn.?t exist/i;
// Parts of a business's site that never hold the menu.
const OTHER_SITES = /^(careers|jobs|shop|store|blog|news|press|investors?|franchis\w*|gift\w*|corporate)\./i;
const NAME_STOPWORDS = new Set(['the', 'a', 'an', 'of', 'and', 'on', 'at', 'by', 'in', 'co', 'cafe', 'café', 'bar', 'grill', 'kitchen',
  'eatery', 'house', 'restaurant', 'bistro', 'diner', 'pizzeria', 'takeaway']);
const MULTI_SUFFIX = ['com.au', 'net.au', 'org.au', 'co.uk', 'org.uk', 'co.nz', 'com.sg', 'co.za', 'com.my'];
const FOREIGN_CC = new Set(['us', 'gb', 'uk', 'ca', 'ie', 'nz', 'fr', 'de', 'es', 'it', 'nl', 'jp', 'in', 'sg', 'za', 'mx', 'br']);

export function menuRoute(request, url, env, ctx) {
  if (request.method !== 'GET') return null;
  if (url.pathname === '/api/menu') return menu(url, env, ctx);
  if (url.pathname === '/api/menutext') return menuText(url, ctx);
  if (url.pathname === '/api/menupdf') return menuPdf(url, ctx);
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

/**
 * GET a page: { ok, status, url, pdf, title, html, text, menu, readable }.
 * `menu` is the menu check: at least 4 lines with prices, or 4 priced dishes.
 */
async function fetchPage(u) {
  try {
    const res = await fetch(u, { headers: { 'User-Agent': BROWSER_UA, Accept: 'text/html,application/pdf,*/*' }, redirect: 'follow', signal: AbortSignal.timeout(7000) });
    const final = res.url || u;
    if (!res.ok) {
      res.body?.cancel();
      return { ok: false, status: res.status, url: final };
    }
    const path = new URL(final).pathname.toLowerCase();
    const pdf = (res.headers.get('Content-Type') || '').toLowerCase().includes('pdf') || path.endsWith('.pdf');
    if (pdf) {
      res.body?.cancel();
      return { ok: true, url: final, pdf: true, title: '', html: '', text: '', menu: true, readable: true };
    }
    const html = (await res.text()).slice(0, 600000);
    const m = html.match(/<title[^>]*>([\s\S]*?)<\/title>/i);
    const title = m ? htmlToText(m[1]).replace(/\s+/g, ' ').trim().slice(0, 120) : '';
    const text = htmlToText(html);
    const menu = looksLikeMenu(text) || pricedCount(structure(text, html)) >= 4;
    // A "page not found" page sent as if it were fine.
    if (!menu && SOFT_404.test(title || text.slice(0, 300))) return { ok: false, status: 404, url: final };
    return { ok: true, url: final, pdf: false, title, html, text, menu, readable: menu };
  } catch {
    return { ok: false, status: 0, url: u };
  }
}

const urlKey = (u) => {
  try {
    const p = new URL(u);
    return p.hostname.replace(/^www\./, '') + p.pathname.replace(/\/$/, '') + p.search;
  } catch {
    return u;
  }
};

/** { url, text } for each distinct link on a page, keeping the longest text per url. */
function links(base, html) {
  const best = new Map();
  for (const m of html.matchAll(/<a\b[^>]*href\s*=\s*["']([^"']+)["'][^>]*>([\s\S]*?)<\/a>/gi)) {
    let href;
    try { href = new URL(m[1].replace(/&amp;/g, '&'), base).href.split('#')[0]; } catch { continue; }
    if (!/^https?:/i.test(href)) continue;
    const text = htmlToText(m[2]).replace(/\s+/g, ' ').trim().slice(0, 80);
    if (!best.has(href) || text.length > best.get(href).length) best.set(href, text);
  }
  return [...best].map(([url, text]) => ({ url, text }));
}

const sameSite = (a, b) => mainLabel(hostOf(a)) === mainLabel(hostOf(b));

/** Links on a venue's page that lead to its menus, best first (as the prototype's _menu_links). */
function menuLinks(base, html) {
  const found = [];
  for (const { url: href, text } of links(base, html)) {
    let path = '';
    try { path = new URL(href).pathname; } catch { continue; }
    const isPdf = path.toLowerCase().endsWith('.pdf');
    if (!(sameSite(href, base) || isPdf)) continue; // menus are often PDFs on a file host
    if (text.length > 60 || '←→«»<>'.includes(text[0] || ' ')) continue; // blog posts, next and previous
    const saysMenu = MENU_WORD.test(text) || MENU_WORD.test(path);
    const meal = MEAL_WORDS.test(text);
    if (!(saysMenu || meal) || (NOT_MENU.test(`${text} ${path}`) && !saysMenu)) continue;
    found.push({ score: 3 * saysMenu + 2 * isPdf + meal, url: href, text });
  }
  return found.sort((a, b) => b.score - a.score);
}

/** Same-site pages likely to hold the menus, e.g. 'The Restaurant', 'Eat & Drink'. */
function diningPages(base, html) {
  return links(base, html).filter(({ url: href, text }) => {
    let path = '';
    try { path = new URL(href).pathname; } catch { return false; }
    return sameSite(href, base) && path.replace(/\//g, '') && DINING.test(text) && !NOT_MENU.test(`${text} ${path}`) && text.length < 40;
  }).map((l) => l.url).slice(0, 2);
}

/** On a chain's main site, the link to this venue's branch, e.g. burwood.brand.com.au or /locations/burwood. */
function branchLink(home, html, words) {
  const base = hostOf(home);
  const brand = base.split('.')[0].replace(/[^a-z]/g, '');
  words = words.filter((w) => !brand.includes(w));
  if (!words.length) return '';
  for (const { url: href } of links(home, html)) {
    let p;
    try { p = new URL(href); } catch { continue; }
    const host = p.hostname.toLowerCase().replace(/^www\./, '');
    if (!(host === base || host.endsWith(`.${base}`)) || urlKey(href) === urlKey(home)) continue;
    const sub = host !== base ? host.slice(0, -base.length).replace(/[^a-z]/g, '') : '';
    const where = `${sub} ${p.pathname.toLowerCase().replace(/[^a-z/]/g, '')}`;
    if (words.some((w) => where.includes(w))) return href;
  }
  return '';
}

function asResult(page, name, text = '') {
  return { kind: 'own', title: page.title || text || `${name} menu`, url: page.url, pdf: !!page.pdf, readable: !!page.readable };
}

/**
 * The venue's own site: common menu paths, then the menu links on its home
 * page (or its branch's page, for a chain), then one step into pages like
 * "The Restaurant". Keeps PDFs, pages that pass the menu check, and pages
 * whose address or link says menu.
 */
async function probeSite(host, name, suburb) {
  const base = `https://${host}`;
  const found = [];
  const add = (page, text) => {
    if (page.ok && !found.some((f) => urlKey(f.url) === urlKey(page.url)) && found.length < 4) found.push(asResult(page, name, text));
  };
  const [homePage, ...guesses] = await Promise.all([fetchPage(base), ...PROBE_PATHS.map((p) => fetchPage(base + p))]);
  if (homePage.ok && !homePage.menu && PARKED_TEXT.test(homePage.text || '')) return []; // a parked domain
  for (const g of guesses) {
    if (!g.ok || (homePage.ok && urlKey(g.url) === urlKey(homePage.url))) continue;
    if (g.pdf || g.menu || /menu/.test(new URL(g.url).pathname.toLowerCase())) add(g);
  }
  // A guessed /menu page that lists the menus (often PDFs): bring those.
  const hub = guesses.find((g) => g.ok && !g.pdf && !g.menu && found.some((f) => urlKey(f.url) === urlKey(g.url)));
  if (hub) {
    for (const r of await hubMenus(hub, name)) {
      if (!found.some((f) => urlKey(f.url) === urlKey(r.url)) && found.length < 7) found.push(r);
    }
  }
  if (found.some((f) => f.readable) || !homePage.ok) return found;

  // A chain: go to this branch's page first.
  let home = homePage;
  const words = String(suburb || '').toLowerCase().split(/[^a-z]+/).filter((w) => w.length >= 4 && !BRANCH_STOP.has(w));
  const branch = branchLink(home.url, home.html, words);
  if (branch) {
    const b = await fetchPage(branch);
    if (b.ok) {
      if (b.menu) add(b);
      if (!b.pdf) home = b;
    }
  }
  const tryLinks = async (page, n) => {
    const cands = menuLinks(page.url, page.html).filter((c) => !found.some((f) => urlKey(f.url) === urlKey(c.url))).slice(0, n);
    const pages = await Promise.all(cands.map((c) => fetchPage(c.url).then((p) => [p, c])));
    for (const [p, c] of pages) {
      if (!p.ok) continue;
      const strong = MENU_WORD.test(c.text) || MENU_WORD.test(new URL(p.url).pathname);
      if (p.pdf || p.menu || strong) add(p, p.pdf ? c.text : '');
    }
  };
  await tryLinks(home, 6);
  if (!found.length) {
    // "The Restaurant", "Eat & Drink": the menus are often one page further in.
    for (const d of diningPages(home.url, home.html)) {
      const page = await fetchPage(d);
      if (!page.ok || page.pdf) continue;
      if (page.menu) add(page);
      else await tryLinks(page, 3);
      if (found.length) break;
    }
  }
  return found;
}

/**
 * A page that lists the menus rather than being one ("Lunch & dinner menu",
 * "Dessert menu", "Wine list"): its PDF menus, and up to two menu pages that
 * pass the menu check.
 */
async function hubMenus(page, name) {
  if (!page.ok || page.pdf || page.menu || !page.html) return [];
  const out = [];
  const pages = [];
  for (const c of menuLinks(page.url, page.html)) {
    if (urlKey(c.url) === urlKey(page.url)) continue;
    if (/\.pdf$/i.test(new URL(c.url).pathname)) {
      if (out.length < 5) out.push({ kind: 'own', title: c.text || `${name} menu`, url: c.url, pdf: true, readable: true });
    } else if (pages.length < 2 && (MENU_WORD.test(c.text) || MEAL_WORDS.test(c.text))) {
      pages.push(c);
    }
  }
  for (const [p, c] of await Promise.all(pages.map((c) => fetchPage(c.url).then((p) => [p, c])))) {
    if (p.ok && p.menu) out.push({ ...asResult(p, name), title: c.text || p.title });
  }
  return out;
}

/**
 * Search hits on the venue's own site: check each is still there and whether
 * it's a menu that can be read; a page that lists the menus brings them too.
 */
async function checkOwn(results, name) {
  return (await Promise.all(results.map(async (r) => {
    if (r.kind !== 'own') return [r];
    if (OTHER_SITES.test(hostOf(r.url))) return [];
    const page = await fetchPage(r.url);
    if (!page.ok) return ALIVE_ANYWAY.has(page.status) ? [{ ...r, readable: false }] : []; // a dead link goes
    let path = '';
    try { path = new URL(page.url).pathname; } catch { /* below */ }
    // A booking or "about" page that mentions the menu: not a menu.
    if (!page.pdf && !page.menu && NOT_MENU.test(path) && !MENU_WORD.test(path)) return [];
    // A home page is not a menu, but may link to the menus.
    if (!page.pdf && !page.menu && path.replace(/\//g, '') === '') return hubMenus(page, name);
    return [{ ...asResult(page, name), title: r.title || page.title }, ...await hubMenus(page, name)];
  }))).flat();
}

async function menu(url, env, ctx) {
  const name = (url.searchParams.get('name') || '').trim().slice(0, 120);
  const suburb = (url.searchParams.get('suburb') || '').trim().slice(0, 80);
  const website = (url.searchParams.get('website') || '').trim().slice(0, 300);
  if (!name) return json({ error: 'Bad request' }, 400);
  const cache = caches.default;
  const key = new Request(`https://cache.mapinabox/menu/v5/${encodeURIComponent(name.toLowerCase())}/${encodeURIComponent(suburb.toLowerCase())}`);
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
    if (kind) results.push({ kind, title: item.title, url: item.url, pdf: /\.pdf($|\?)/i.test(item.url), readable: false });
  }
  const seenOwn = new Set();
  results = (await checkOwn(results.slice(0, 8), name)).filter((r) => {
    const k = urlKey(r.url);
    if (seenOwn.has(k)) return false;
    seenOwn.add(k);
    return true;
  });
  // No menu page on the venue's own site in the search: look on the site itself.
  if (!results.some((r) => r.kind === 'own' && r.readable)) {
    const hosts = [];
    if (website) hosts.push(hostOf(website));
    for (const item of raw) {
      const h = hostOf(item.url);
      if (h && !isDelivery(h) && !OTHER_SITES.test(h) && labelMatchesVenue(mainLabel(h), t) && !hosts.includes(h)) hosts.push(h);
    }
    for (const h of hosts.filter(Boolean).slice(0, 2)) {
      const hits = (await probeSite(h, name, suburb)).filter((r) => !results.some((o) => urlKey(o.url) === urlKey(r.url)));
      if (hits.length) {
        results = results.concat(hits);
        break;
      }
    }
  }
  // Menus that can be read here first (web pages, then PDFs), then other menu pages, then delivery.
  const rank = (r) => (r.kind !== 'own' ? 3 : r.readable && !r.pdf ? 0 : r.pdf ? 1 : 2);
  results.sort((a, b) => rank(a) - rank(b));
  const out = json({ results: results.slice(0, 8), searched: raw.length }, 200, results.length ? 7 * 24 * 3600 : 0);
  if (results.length) ctx.waitUntil(cache.put(key, out.clone())); // an empty answer may be a passing failure
  return out;
}

/** A menu page on a venue's own site as sections of dishes with prices (not delivery sites, which block it). */
async function menuText(url, ctx) {
  const target = url.searchParams.get('url') || '';
  let u;
  try { u = new URL(target); } catch { return json({ error: 'Bad request' }, 400); }
  if (!/^https?:$/.test(u.protocol) || isDelivery(hostOf(u.href))) return json({ error: 'Not readable' }, 400);
  const cache = caches.default;
  const key = new Request(`https://cache.mapinabox/menutext/v2/${encodeURIComponent(u.href)}`);
  const hit = await cache.match(key);
  if (hit) return hit;
  const page = await fetchPage(u.href);
  if (!page.ok || page.pdf) return json({ error: 'Not readable' }, 502);
  const sections = structure(page.text, page.html);
  const out = json({ text: page.text.slice(0, 8000), sections, menu: page.menu }, 200, 7 * 24 * 3600);
  ctx.waitUntil(cache.put(key, out.clone()));
  return out;
}

/** A PDF menu, passed through for the browser to read (up to 10 MB). */
async function menuPdf(url, ctx) {
  const target = url.searchParams.get('url') || '';
  let u;
  try { u = new URL(target); } catch { return json({ error: 'Bad request' }, 400); }
  if (!/^https?:$/.test(u.protocol) || isDelivery(hostOf(u.href))) return json({ error: 'Not readable' }, 400);
  const cache = caches.default;
  const key = new Request(`https://cache.mapinabox/menupdf/v1/${encodeURIComponent(u.href)}`);
  const hit = await cache.match(key);
  if (hit) return hit;
  let bytes;
  try {
    const res = await fetch(u.href, { headers: { 'User-Agent': BROWSER_UA, Accept: 'application/pdf,*/*' }, redirect: 'follow', signal: AbortSignal.timeout(15000) });
    if (!res.ok || Number(res.headers.get('Content-Length') || 0) > 10e6) return json({ error: 'Not readable' }, 502);
    bytes = await res.arrayBuffer();
  } catch {
    return json({ error: 'Not readable' }, 502);
  }
  if (bytes.byteLength > 10e6 || String.fromCharCode(...new Uint8Array(bytes.slice(0, 5))) !== '%PDF-') return json({ error: 'Not a PDF' }, 502);
  const out = new Response(bytes, { headers: { 'Content-Type': 'application/pdf', 'Cache-Control': 'public, max-age=604800' } });
  ctx.waitUntil(cache.put(key, out.clone()));
  return out;
}
