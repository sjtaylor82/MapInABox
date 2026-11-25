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
  if (url.pathname === '/api/menuphotos') return menuPhotos(url, ctx);
  if (url.pathname === '/api/menuimage') return menuImage(url, ctx);
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

// Review, listing and social sites: they copy menus, which may be old or guessed.
const LISTING_SITES = /(^|\.)(wheree|restaurantguru|yelp|tripadvisor|zomato|mapquest|facebook|instagram|tiktok|google|brisbane\.guide|menulist|sirved|menupix|allmenus)(\.|$)/;

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
    // One store's page, not a list of restaurants in an area.
    if (!/\/store\/|\/restaurants-|\/menu\//.test(path)) return null;
    // Delivery sites hold thousands of venues and branches: keep a page only
    // when it names this venue and this suburb.
    const text = compactOf(`${item.title} ${item.url} ${item.snippet}`);
    const words = t.distinctive.filter((w) => text.includes(w)).length;
    const venue = (t.compact && text.includes(t.compact)) || words >= Math.min(2, t.distinctive.length);
    const place = !suburb || text.includes(compactOf(suburb));
    return venue && place ? 'delivery' : null;
  }
  if (labelMatchesVenue(mainLabel(host), t)) {
    const hay = `${path} ${String(item.title || '').toLowerCase()}`;
    if (MENU_SIGNALS.some((s) => hay.includes(s))) return 'own';
  }
  // The venue's page on an online ordering service (hungryhungry, Mr Yum and
  // the like): its full name is in the address, and the page is a menu or
  // ordering page. Review and listing sites are left out. A chain's branch is
  // told apart by its suburb, as for delivery sites.
  if (t.compact.length >= 8 && !LISTING_SITES.test(host) && compactOf(host + path).includes(t.compact)
    && MENU_SIGNALS.some((s) => path.includes(s))) {
    const text = compactOf(`${item.title} ${item.url} ${item.snippet}`);
    if (!suburb || text.includes(compactOf(suburb))) return 'own';
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
  // A menu page with no menu text may show the menu as pictures.
  const pictures = !page.pdf && !page.readable && page.html ? menuPictures(page.html, page.url).length : 0;
  return { kind: 'own', title: page.title || text || `${name} menu`, url: page.url, pdf: !!page.pdf, readable: !!page.readable, pictures };
}

const NOT_PICTURE = /logo|icon|favicon|insta|facebook|avatar|badge|sprite|placeholder|payment|award|review|map[-_.]|banner-small/i;

/**
 * The big pictures on a venue's menu page, which on a page with no menu
 * text are usually the menu itself (designed menu cards, photos of the
 * board). The largest size up to 1600 pixels wide of each; logos, icons
 * and small pictures are left out.
 */
function menuPictures(html, base) {
  const out = [];
  const seen = new Set();
  for (const tag of String(html).match(/<img\b[^>]*>/gi) || []) {
    const attr = (n) => (tag.match(new RegExp(`\\s${n}="([^"]*)"`, 'i')) || [])[1] || '';
    let best = '';
    let bestW = 0;
    for (const set of [attr('data-srcset'), attr('srcset')]) {
      for (const part of set.split(',')) {
        const [u, w] = part.trim().split(/\s+/);
        const n = parseInt(w, 10);
        if (u && n && ((n <= 1600 && n > bestW) || !bestW)) { best = u; bestW = n; }
      }
    }
    if (!best) best = [attr('data-src'), attr('data-lazy-src'), attr('src')].find((u) => u && !u.startsWith('data:')) || '';
    if (!best) continue;
    let u;
    try { u = new URL(best.replace(/&amp;/g, '&'), base); } catch { continue; }
    if (!/^https?:$/.test(u.protocol) || !/\.(jpe?g|png|webp)$/i.test(u.pathname)) continue;
    if (NOT_PICTURE.test(u.pathname) || NOT_PICTURE.test(attr('alt')) || NOT_PICTURE.test(attr('class'))) continue;
    const w = parseInt(attr('width'), 10) || 0;
    const h = parseInt(attr('height'), 10) || 0;
    if ((w || h || bestW) && Math.max(w, h, bestW) < 500) continue;
    const key = u.pathname.replace(/-\d+x\d+(?=\.\w+$)/, '').toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(u.href);
  }
  return out.slice(0, 8);
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
    // Ordering pages built in the browser (Bopple, HungryHungry): their menus can be read here.
    if (isOrderingMenu(r.url)) return [{ ...r, readable: true }];
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
  const key = new Request(`https://cache.mapinabox/menu/v16/${encodeURIComponent(name.toLowerCase())}/${encodeURIComponent(suburb.toLowerCase())}`);
  const hit = await cache.match(key);
  if (hit) return hit;

  const t = nameTokens(name);
  // Photos of the menu on RestaurantGuru, looked for at the same time.
  const photoLookup = restaurantGuru(env, name, suburb, t).catch(() => null);
  let raw = [];
  try {
    raw = await search(env, `"${name}"${suburb ? ` ${suburb}` : ''} menu Australia`);
  } catch { /* still probe the website below */ }
  // No delivery page in the top results: search again for them.
  const isDel = (item) => isDelivery(hostOf(item.url));
  const found = new Set(raw.filter((item) => isDel(item) && classify(item, t, suburb)).map((item) => mainLabel(hostOf(item.url))));
  if (!(found.has('ubereats') && found.has('doordash'))) {
    try {
      // Loosely worded: delivery sites spell names their own way ("Pizza and
      // Pasta" for "Pizza & Pasta"), so an exact-name search misses them.
      const loose = name.replace(/&/g, ' and ').replace(/\s+/g, ' ').trim();
      const more = await search(env, `${loose}${suburb ? ` ${suburb}` : ''} ubereats doordash menulog`);
      raw = raw.concat(more);
    } catch { /* the first search is enough */ }
  }
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
  // Photos of the menu: always when nothing else was found; alongside other
  // menus when the newest is from the last year (a delivery menu may not
  // have everything).
  // Its Facebook page too, when there's no other menu (menus there are photos in its posts).
  const rg = (await photoLookup) || {};
  const nothingElse = !results.length;
  if (rg.photo && (nothingElse || ageDays(rg.photo.age) < 365)) results.push(rg.photo);
  if (rg.facebook && nothingElse) results.push(rg.facebook);
  // Menus that can be read here first (web pages, then PDFs), then other menu pages, then delivery, then photos.
  const rank = (r) => (r.kind === 'facebook' ? 5 : r.kind === 'photo' ? 4 : r.kind !== 'own' ? 3 : r.readable && !r.pdf ? 0 : r.pdf ? 1 : 2);
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
  const key = new Request(`https://cache.mapinabox/menutext/v6/${encodeURIComponent(u.href)}`);
  const hit = await cache.match(key);
  if (hit) return hit;
  // Ordering sites whose menus are built in the browser: read from their menu data.
  if (isOrderingMenu(u.href)) {
    const sections = await orderingMenu(u.href);
    if (!sections.length) return json({ error: 'Not readable' }, 502);
    const out = json({ text: '', sections, menu: true }, 200, 24 * 3600);
    ctx.waitUntil(cache.put(key, out.clone()));
    return out;
  }
  const page = await fetchPage(u.href);
  if (!page.ok || page.pdf) return json({ error: 'Not readable' }, 502);
  const sections = structure(page.text, page.html);
  const out = json({ text: page.text.slice(0, 8000), sections, menu: page.menu }, 200, 7 * 24 * 3600);
  ctx.waitUntil(cache.put(key, out.clone()));
  return out;
}

// ---- Ordering sites ---------------------------------------------------------
// Their pages are built in the browser, so there's no menu in the page itself,
// but the menu data behind them can be read directly.

/** Is this a menu page on an ordering site we can read? */
const isOrderingMenu = (u) => !!(boppleSlug(u) || hungryHungrySlug(u));

/** An ordering site's menu as sections of dishes, or []. */
async function orderingMenu(u) {
  if (boppleSlug(u)) return boppleMenu(boppleSlug(u));
  if (hungryHungrySlug(u)) return hungryHungryMenu(hungryHungrySlug(u));
  return [];
}

// HungryHungry: storefront.hungryhungry.com/<venue>/menu. Its settings give
// the menu's price level and UI numbers; the menu then comes as one file.

/** 'gustosgourmetpizzapasta' for https://storefront.hungryhungry.com/gustosgourmetpizzapasta/menu, else ''. */
function hungryHungrySlug(u) {
  try {
    const x = new URL(u);
    if (!/^(storefront|order\.platform)\.hungryhungry\.com$/.test(x.hostname)) return '';
    const slug = x.pathname.split('/').filter(Boolean)[0] || '';
    return /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$/.test(slug) ? slug : '';
  } catch {
    return '';
  }
}

async function hungryHungryMenu(slug) {
  const base = 'https://storefront.hungryhungry.com/api/v1';
  const get = (path, ms) => fetch(`${base}/${path}`, {
    headers: { 'User-Agent': BROWSER_UA, Accept: 'application/json' },
    signal: AbortSignal.timeout(ms),
  }).then((r) => (r.ok ? r.json() : null)).catch(() => null);
  const config = await get(`storefront/config?host=storefront.hungryhungry.com&path=${encodeURIComponent(slug)}`, 8000);
  const ui = config && config.themeObj && config.themeObj.UI_ID;
  const level = ((JSON.stringify(config || {}).match(/"price_level_id":"(\d+)"/) || [])[1]);
  if (!ui || !level) return [];
  const d = await get(`drupal/api/online/menu/${level}/${ui}/en/1`, 15000);
  return d ? hungryHungrySections(d) : [];
}

/** HungryHungry's menu file -> sections: its groups (Pasta, Meat Pizza...) with each dish's sizes and prices. */
function hungryHungrySections(d) {
  const money = (n) => `$${Number(n).toFixed(2)}`;
  const priceOf = (it) => Number(it && it.prices && it.prices.base);
  const groups = d.group || {};
  const cols = d.collection || {};
  const items = d.item || {};
  // Groups in menu order: the categories' content, first appearance wins.
  const order = [];
  for (const cat of Object.values(d.menu_map || {})) {
    if (cat.hidden && cat.hidden !== '0') continue;
    for (const c of cat.content || []) if (c.type === 'group' && !order.includes(String(c.id))) order.push(String(c.id));
  }
  for (const id of Object.keys(groups)) if (!order.includes(id)) order.push(id);
  const seen = new Set();
  const sections = [];
  for (const gid of order) {
    const g = groups[gid];
    if (!g || !g.title || gid === '0') continue;
    const dishes = [];
    const add = (name, desc, sizes, available) => {
      name = String(name || '').trim();
      if (!name || seen.has(`${gid}|${name}`)) return;
      seen.add(`${gid}|${name}`);
      const priced = sizes.filter((x) => Number.isFinite(x.price));
      let price = '';
      if (priced.length && priced.every((x) => x.price === priced[0].price)) price = money(priced[0].price);
      else if (priced.length) price = priced.sort((a, b) => a.price - b.price).map((x) => `${x.size || 'one size'} ${money(x.price)}`).join(', ');
      const note = [String(desc || '').replace(/\s+/g, ' ').trim(), available ? '' : 'Not available now'].filter(Boolean).join('. ');
      dishes.push({ name, price, desc: note });
    };
    for (const c of g.collections || []) {
      const col = cols[String(c.id)];
      if (!col) continue;
      const its = (col.items || []).map((x) => items[String(x.id)]).filter((it) => it && it.visible !== 'hidden');
      if (!its.length) continue;
      add(col.title, col.description, its.map((it) => ({ size: (it.attributes && it.attributes.Size) || '', price: priceOf(it) })),
        its.some((it) => it.availability !== false));
    }
    for (const x of Object.values(g.items || {})) {
      const it = items[String(x.id)];
      if (it && it.visible !== 'hidden') add(it.title, it.description, [{ size: '', price: priceOf(it) }], it.availability !== false);
    }
    if (dishes.length) sections.push({ title: String(g.title).trim(), items: dishes });
  }
  return sections;
}

// ---- Bopple ordering pages --------------------------------------------------
// bopple.app/<venue>/menu is built in the browser from orders.bopple.me's
// menu data, which can be read directly.

/** 'bavarian-sizzle' for https://bopple.app/bavarian-sizzle/menu, else ''. */
function boppleSlug(u) {
  try {
    const x = new URL(u);
    if (!/(^|\.)bopple\.app$/.test(x.hostname)) return '';
    const slug = x.pathname.split('/').filter(Boolean)[0] || '';
    return /^[a-z0-9-]+$/i.test(slug) ? slug : '';
  } catch {
    return '';
  }
}

/**
 * A Bopple venue's menu as sections of dishes: [{ title, items: [{ name, price, desc }] }].
 */
async function boppleMenu(slug) {
  const get = (path) => fetch(`https://orders.bopple.me/api/venues/${slug}/menu/${path}`, {
    headers: { 'User-Agent': BROWSER_UA, Accept: 'application/json' },
    signal: AbortSignal.timeout(10000),
  }).then((r) => (r.ok ? r.json() : [])).catch(() => []);
  const [cats, products] = await Promise.all([get('categories'), get('products')]);
  if (!Array.isArray(cats) || !Array.isArray(products)) return [];
  const money = (n) => `$${Number(n).toFixed(2)}`;
  const sections = cats.filter((c) => c && c.active !== false)
    .sort((a, b) => (a.sort_order || 0) - (b.sort_order || 0))
    .map((c) => ({
      title: String(c.category_desc || '').trim(),
      items: products.filter((p) => p && p.product_category_id === c.id)
        .sort((a, b) => (a.sort_order || 0) - (b.sort_order || 0))
        .map((p) => {
          const lo = Number(p.price_min ?? p.price);
          const hi = Number(p.price_max ?? p.price);
          const price = !Number.isFinite(lo) ? '' : hi > lo ? `from ${money(lo)}` : money(lo);
          const desc = [String(p.product_desc || '').replace(/\s+/g, ' ').trim(), p.availability_desc && String(p.availability_desc).trim()]
            .filter(Boolean).join('. ');
          return { name: String(p.product_name || '').trim(), price, desc };
        })
        .filter((i) => i.name),
    }))
    .filter((x) => x.title && x.items.length);
  return sections;
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

// ---- Menu photos on RestaurantGuru ----------------------------------------
// Customers upload photos of menus (boards, printed menus) to RestaurantGuru.
// For a venue with no menu online otherwise, the newest photo is passed
// through, so it can be shared to a reading app such as Seeing AI.

const RG_PAGE = /^([a-z]{2}\.)?restaurantguru\.com$/;
// Menu photos: RestaurantGuru's own, and Zomato's (older menus came from there).
const RG_IMAGE = /^((menu\d*|img\d*)\.restaurantguru\.com|b\.zmtcdn\.com)$/;

/**
 * The venue on RestaurantGuru: { photo, facebook } results (either may be
 * null), or null. photo: its menu photos; facebook: the Facebook page
 * RestaurantGuru links to for the menu (Meta removed Facebook's menu tab, so
 * it's the page, where menus are posted as photos).
 */
async function restaurantGuru(env, name, suburb, t) {
  let raw = [];
  try {
    raw = await search(env, `site:restaurantguru.com "${name}"${suburb ? ` ${suburb}` : ''}`);
  } catch {
    return null;
  }
  for (const item of raw) {
    const host = hostOf(item.url);
    if (!RG_PAGE.test(host)) continue;
    // The English page, not the AMP or another language's copy.
    const parts = new URL(item.url).pathname.replace(/^\/amp\//, '/').split('/').filter(Boolean);
    if (!parts.length || parts.length > 2 || (parts[1] && parts[1] !== 'menu')) continue;
    // Its title names the venue and its suburb ("Menu at Muzza's Gourmet Pies & Cafe, Coorparoo").
    const title = compactOf(item.title);
    const words = t.distinctive.filter((w) => title.includes(w)).length;
    if (!((t.compact && title.includes(t.compact)) || words >= Math.min(2, t.distinctive.length))) continue;
    if (suburb && !title.includes(compactOf(suburb))) continue;
    const page = `https://restaurantguru.com/${parts[0]}/menu`;
    const { versions, facebook } = await rgPage(page);
    const newest = versions.length ? versions.reduce((a, b) => (ageDays(b.age) < ageDays(a.age) ? b : a)) : null;
    return {
      photo: newest && { kind: 'photo', title: 'Photos of the menu', url: page, age: newest.age, pdf: false, readable: true },
      facebook: facebook && { kind: 'facebook', title: 'Facebook page', url: facebook, pdf: false, readable: false },
    };
  }
  return null;
}

/** Where a RestaurantGuru "View menu on www.facebook.com" link goes: the Facebook page, or ''. */
async function rgFacebook(html) {
  const m = String(html).match(/href="(https:\/\/restaurantguru\.com\/link\/[\w-]+)"[^>]*>\s*View menu on (?:www\.|m\.)?facebook\.com/i);
  if (!m) return '';
  try {
    const res = await fetch(m[1], { headers: { 'User-Agent': BROWSER_UA }, redirect: 'manual', signal: AbortSignal.timeout(6000) });
    const to = new URL(res.headers.get('Location') || '', m[1]);
    if (!/(^|\.)facebook\.com$/.test(to.hostname)) return '';
    return `https://www.facebook.com${to.pathname}`;
  } catch {
    return '';
  }
}

/**
 * Menu versions in a RestaurantGuru page's HTML, in page order (newest first):
 * [{ who: 'customer' | 'owner', age: '4 years ago', images: [url] }]
 */
function menuVersions(html) {
  const out = [];
  for (const chunk of String(html).split(/class="menu_section"/).slice(1)) {
    const title = (chunk.match(/class="h2_text">([^<]*)</) || [])[1] || '';
    const age = ((chunk.match(/class="menu_date">([^<]*)</) || [])[1] || '').trim();
    const images = [];
    for (const m of chunk.matchAll(/data-src="(https:\/\/[^"]+\.(?:jpe?g|png|webp))"/gi)) {
      let host = '';
      try { host = new URL(m[1]).hostname; } catch { continue; }
      if (RG_IMAGE.test(host) && !images.includes(m[1])) images.push(m[1]);
    }
    if (images.length) out.push({ who: /owner|restaurant/i.test(title) ? 'owner' : 'customer', age, images: images.slice(0, 6) });
  }
  return out;
}

/**
 * GET /api/menuphotos?url=  The menu photos on a RestaurantGuru menu page:
 * { versions: [{ who, age, images }] }, newest first. The current menu is on
 * the page; older ones come from its /legacy part.
 */
async function menuPhotos(url, ctx) {
  let u;
  try { u = new URL(url.searchParams.get('url') || ''); } catch { return json({ error: 'Bad request' }, 400); }
  if (!/^https?:$/.test(u.protocol)) return json({ error: 'Bad request' }, 400);
  const cache = caches.default;
  const key = new Request(`https://cache.mapinabox/menuphotos/v4/${encodeURIComponent(u.href)}`);
  const hit = await cache.match(key);
  if (hit) return hit;
  let versions;
  if (RG_PAGE.test(hostOf(u.href))) {
    versions = (await rgPage(u.href)).versions;
  } else {
    // The venue's own menu page: its menu pictures.
    const page = await fetchPage(u.href);
    const images = page.ok && page.html ? menuPictures(page.html, page.url) : [];
    versions = images.length ? [{ who: 'site', age: '', images }] : [];
  }
  if (!versions.length) return json({ error: 'Not readable' }, 502);
  // Newest first.
  versions.sort((a, b) => ageDays(a.age) - ageDays(b.age));
  const out = json({ versions: versions.slice(0, 5) }, 200, 7 * 24 * 3600);
  ctx.waitUntil(cache.put(key, out.clone()));
  return out;
}

/** "4 years ago" -> about 1460 days; "2 months ago" -> 60; unknown -> Infinity. */
function ageDays(age) {
  const a = String(age || '').toLowerCase();
  if (/today|just now|hour|minute|second/.test(a)) return 0;
  if (/yesterday/.test(a)) return 1;
  const m = a.match(/(\d+|an?|one)\s+(day|week|month|year)s?\s+ago/);
  if (!m) return Infinity;
  const n = /^\d+$/.test(m[1]) ? Number(m[1]) : 1;
  return n * { day: 1, week: 7, month: 30, year: 365 }[m[2]];
}

/**
 * A RestaurantGuru menu page: all its menu versions (the current one and
 * older ones) and the Facebook page it links to. { versions, facebook }
 */
async function rgPage(pageUrl) {
  const page = await fetchPage(pageUrl);
  if (!page.ok || !page.html) return { versions: [], facebook: '' };
  const facebookLookup = rgFacebook(page.html);
  const u = new URL(pageUrl);
  const versions = menuVersions(page.html);
  if (page.html.includes('show_old_menu__button')) {
    try {
      const res = await fetch(`${u.href.replace(/\/$/, '')}/legacy`, {
        headers: { 'User-Agent': BROWSER_UA, Accept: 'application/json', 'X-Requested-With': 'XMLHttpRequest', Referer: u.href },
        signal: AbortSignal.timeout(8000),
      });
      if (res.ok) versions.push(...menuVersions((await res.json()).html || ''));
    } catch { /* the current menu is enough */ }
  }
  return { versions, facebook: await facebookLookup };
}

/** GET /api/menuimage?u=  A menu photo, passed through so it can be shared from the browser. */
async function menuImage(url, ctx) {
  let u;
  try { u = new URL(url.searchParams.get('u') || ''); } catch { return json({ error: 'Bad request' }, 400); }
  if (!/^https?:$/.test(u.protocol)) return json({ error: 'Bad request' }, 400);
  const cache = caches.default;
  const key = new Request(`https://cache.mapinabox/menuimage/v1/${encodeURIComponent(u.href)}`);
  const hit = await cache.match(key);
  if (hit) return hit;
  let res;
  try {
    const referer = RG_IMAGE.test(u.hostname) ? 'https://restaurantguru.com/' : `${u.origin}/`;
    res = await fetch(u.href, { headers: { 'User-Agent': BROWSER_UA, Referer: referer }, signal: AbortSignal.timeout(15000) });
  } catch {
    return json({ error: 'Not readable' }, 502);
  }
  const type = res.headers.get('Content-Type') || '';
  // Pictures only (this is not a way to fetch other things), up to 8 MB.
  if (!res.ok || !/^image\/(jpeg|png|webp)/.test(type) || Number(res.headers.get('Content-Length') || 0) > 8e6) return json({ error: 'Not readable' }, 502);
  const bytes = await res.arrayBuffer();
  if (bytes.byteLength > 8e6) return json({ error: 'Too big' }, 502);
  const out = new Response(bytes, { headers: { 'Content-Type': type, 'Cache-Control': 'public, max-age=2592000' } });
  ctx.waitUntil(cache.put(key, out.clone()));
  return out;
}
