// Turn a menu (a web page, the text of a PDF, or schema.org data) into
// sections of dishes with prices, and tell whether a page is a menu at all.
// Used by the Worker for web pages and by the browser for PDFs. A port of
// the Accessible Menu Browser prototype's menustructure.py and the menu
// check in its menus.py.
//
// structure() returns [{ title, items: [{ name, price, desc }] }].

const PRICE_SRC = String.raw`(?:(?:from|large|small|regular|x ?large|medium|glass|bottle|half|full|each)\s*[-:]?\s*)?\$\s?\d{1,3}(?:[.,]\d{2})?(?!\d)` +
  String.raw`|(?<![\d(/:.\-])\b[1-9]\d{0,2}(?:\.\d{1,2})?\s*(?:ea|each|pp)?\s*$` +
  String.raw`|\b[1-9]\d{0,2}\.\d{2}\b`;
const PRICE = new RegExp(PRICE_SRC, 'i');
// One or more prices and nothing else: "$6.00", "14 67", "LARGE - $19.95, X LARGE - $24.95".
// (Only size words may label a price, so "Ice cream 9" stays a dish.)
const SIZE_WORDS = String.raw`(?:from|x ?large|large|small|regular|medium|sml?|lge?|reg|glass|bottle|carafe|jug|pot|cup|mug|pint|schooner|middy|half|full|each|single|double|kids?|adult|serve|pp)`;
const PRICE_ONLY = new RegExp(String.raw`^(?:\s*(?:${SIZE_WORDS}\s*[-:]?\s*)?\$?\s?\d{1,3}(?:[.,]\d{2})?\s*[,/|]?)+\s*$`, 'i');
// Dot leaders between a dish and its price ("CREAM....19.5").
const BARE_PRICE = /^\$\s?\d{1,3}(?:[.,]\d{2})?$/;
const LEADER =/\.{2,}(?=\s*\$?\d)|\.{3,}|…+|_{3,}/;
const DROP_TAGS = 'script|style|noscript|svg|nav|header|footer|form|iframe|button|select|template';
const HEADING = '\u0001H';

const PHONE = /\b0\d{3}\s?\d{3}\s?\d{3}\b|\(0\d\)\s?\d{4}|\b1[38]00\s?\d{3}/i;
const MONTHS = /\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\s*$/i;
const PRICE_LINE = /\$\s?[1-9]\d{0,2}(?:[.,]\d{2})?(?!\d)|(?<![\d(/:.\-])\b[1-9]\d{0,2}(?:\.\d{1,2})?\s*$|\b[1-9]\d{0,2}\.\d{2}\b/;

const clean = (s) => String(s || '').replace(/\s+/g, ' ').trim();

function strip(s, chars) {
  let a = 0;
  let b = s.length;
  while (a < b && chars.includes(s[a])) a++;
  while (b > a && chars.includes(s[b - 1])) b--;
  return s.slice(a, b);
}

const priceOnly = (line) => line.length <= 80 && PRICE_ONLY.test(line);

const ENTITIES = { amp: '&', nbsp: ' ', quot: '"', apos: "'", lt: '<', gt: '>', rsquo: '’', lsquo: '‘',
  rdquo: '”', ldquo: '“', ndash: '–', mdash: '—', hellip: '…', eacute: 'é', egrave: 'è',
  agrave: 'à', ccedil: 'ç', ntilde: 'ñ', middot: '·', bull: '•', reg: '®', trade: '™', frac12: '½' };

export function decodeEntities(s) {
  return String(s || '').replace(/&(#x[0-9a-f]+|#\d+|[a-z0-9]+);/gi, (m, e) => {
    if (e[0] === '#') {
      const n = e[1] === 'x' || e[1] === 'X' ? parseInt(e.slice(2), 16) : parseInt(e.slice(1), 10);
      try { return String.fromCodePoint(n); } catch { return ' '; }
    }
    const v = ENTITIES[e.toLowerCase()];
    return v === undefined ? ' ' : v;
  });
}

/** How many lines carry something that looks like a price (not a date or a phone number). */
export function priceLines(text) {
  return String(text || '').split('\n').filter((l) => PRICE_LINE.test(l) && !MONTHS.test(l) && !PHONE.test(l)).length;
}

/** A menu has at least 4 lines with prices. */
export const looksLikeMenu = (text) => priceLines(text) >= 4;

// ---------- schema.org (many restaurant sites) ----------

function priceText(offer) {
  if (Array.isArray(offer)) offer = offer[0] || {};
  if (!offer || typeof offer !== 'object') return '';
  const p = offer.price;
  if (p === undefined || p === null || p === '') return '';
  const n = Number(p);
  return Number.isFinite(n) ? `$${n.toFixed(2)}` : String(p);
}

export function fromJsonLd(html) {
  const sections = [];
  for (const m of String(html || '').matchAll(/<script[^>]+application\/ld\+json[^>]*>([\s\S]*?)<\/script>/gi)) {
    let data;
    try { data = JSON.parse(m[1]); } catch { continue; }
    const stack = Array.isArray(data) ? [...data] : [data];
    let guard = 0;
    while (stack.length && guard++ < 5000) {
      const node = stack.shift();
      if (Array.isArray(node)) { stack.push(...node); continue; }
      if (!node || typeof node !== 'object') continue;
      if (node['@graph']) stack.push(...[].concat(node['@graph']));
      let menu = 'hasMenu' in node ? node.hasMenu : node['@type'] === 'Menu' ? node : null;
      if (Array.isArray(menu)) menu = menu[0];
      if (!menu || typeof menu !== 'object') continue;
      for (const sec of [].concat(menu.hasMenuSection || [])) {
        if (!sec || typeof sec !== 'object') continue;
        const items = [].concat(sec.hasMenuItem || []).filter((i) => i && typeof i === 'object' && i.name)
          .map((i) => ({ name: clean(i.name), price: priceText(i.offers), desc: clean(i.description) }));
        if (items.length) sections.push({ title: clean(sec.name) || 'Menu', items });
      }
    }
  }
  return sections;
}

// ---------- grouping lines into sections and dishes ----------

/**
 * lines: [kind, text] where kind is 'section', 'dish' (a heading used as a
 * dish name) or 'text'. Handles 'name ... price', 'name / price / desc' and
 * 'name / desc / price'.
 */
function group(lines) {
  const sections = [{ title: '', items: [] }];
  const caps = (s) => /[A-Z]/.test(s) && s === s.toUpperCase();
  // Some pages show each price twice (one copy for phones, one for computers):
  // "$12", "GARLIC BREAD", "$12". A price arriving when the dish before already
  // has one waits for the next dish.
  let pending = '';
  for (const [kind, line] of lines) {
    if (kind === 'text' && PHONE.test(line)) continue; // "Call 0412 345 678" is not a dish
    const items = sections[sections.length - 1].items;
    const last = items.length ? items[items.length - 1] : null;
    const lead = LEADER.exec(line);
    if (kind === 'section') {
      sections.push({ title: line, items: [] });
      pending = '';
    } else if (kind === 'dish') {
      // A heading that is only a price: the dish's words come after it.
      if (priceOnly(line)) items.push({ name: '', price: strip(line, ' ,|'), desc: '', heading: true });
      else items.push({ name: line, price: pending, desc: '', heading: true });
      pending = '';
    } else if (last && last.twice && BARE_PRICE.test(line) && line.replace(/\s/g, '') === last.price) {
      delete last.twice; // the second copy of a price
    } else if (BARE_PRICE.test(line) && last && last.price && !last.heading) {
      pending = line.replace(/\s/g, '');
    } else if (pending && !PRICE.test(line) && !(last && caps(last.name) && !caps(line) && !last.desc)) {
      items.push({ name: line, price: pending, desc: '', twice: true });
      pending = '';
    } else if (last && last.price && !last.desc && caps(last.name) && !caps(line) && !PRICE.test(line)) {
      last.desc = line; // "SALT AND PEPPER CALAMARI", "Lemon and smoked aioli"
    } else if (lead && /^\$?\s?\d/.test(line.slice(lead.index + lead[0].length).trim())) {
      // "description.........34.5": the dots lead to the price.
      const left = strip(line.slice(0, lead.index).trim(), ' .');
      const right = strip(line.slice(lead.index + lead[0].length).trim(), ' .');
      if (last && !last.price) {
        last.desc = last.desc || left;
        last.price = right;
      } else {
        items.push({ name: left || right, price: right, desc: '' });
      }
    } else if (priceOnly(line)) {
      if (last) last.price = last.price ? strip(`${last.price} ${line}`, ' ,') : strip(line, ' ,|');
    } else if (PRICE.test(line)) {
      const first = line.search(PRICE);
      const name = strip(line.slice(0, first), ' .-–:|,');
      if (!name && last && !last.price) last.price = line;
      else if (last && !last.price && name.length > 40 && last.name && !last.desc) {
        last.desc = name; // name, then "description 12"
        last.price = line.slice(first).trim();
      } else {
        items.push({ name: name || line, price: strip(line.slice(first), ' ,|'), desc: '' });
      }
    } else if (last && !last.desc && (line.length > 30 || last.heading)
      && (last.price || last.name.length < 50 || last.heading)) {
      last.desc = line;
    } else {
      items.push({ name: line, price: '', desc: '' });
    }
  }
  // Keep sections with at least one priced dish; drop unpriced clutter before the first one.
  for (const s of sections) {
    for (const i of s.items) if (/^\$?\s?0(?:[.,]00?)?$/.test(i.price)) i.price = ''; // "Cart 0"
  }
  const kept = sections.filter((s) => s.items.some((i) => i.price));
  for (const s of kept) {
    while (s.items.length && !s.items[0].price) s.items.shift();
    for (const i of s.items) {
      delete i.heading;
      delete i.twice;
      if (!i.name) { i.name = i.desc; i.desc = ''; } // a price heading, then the dish's words
    }
    s.items = s.items.filter((i) => i.name);
    s.title = s.title || 'Menu';
  }
  return kept;
}

const priced = (sections) => sections.reduce((n, s) => n + s.items.filter((i) => i.price).length, 0);

/** A page's lines: [heading level (0 for ordinary text), text]. */
function htmlRaw(html) {
  let s = String(html || '').slice(0, 800000)
    .replace(/<!--[\s\S]*?-->/g, ' ')
    .replace(new RegExp(`<(${DROP_TAGS})\\b[\\s\\S]*?<\\/\\1\\s*>`, 'gi'), ' ')
    .replace(/<h([1-6])\b[^>]*>([\s\S]*?)<\/h\1\s*>/gi, (m, lv, inner) => {
      const t = clean(decodeEntities(inner.replace(/<[^>]+>/g, ' ')));
      return t ? `\n${HEADING}${lv}${t}\n` : '\n';
    })
    .replace(/<[^>]+>/g, '\n');
  s = decodeEntities(s);
  const raw = [];
  for (const piece of s.split('\n')) {
    const line = clean(piece);
    if (!line) continue;
    if (line.startsWith(HEADING)) {
      const level = Number(line[HEADING.length]);
      const title = line.slice(HEADING.length + 1).trim();
      if (title && title.length < 80) raw.push([level, title]);
    } else if (raw.length && raw[raw.length - 1][0] === 0 && raw[raw.length - 1][1] === '$' && /^\d/.test(line)) {
      raw[raw.length - 1][1] = `$${line}`; // "$" and "12" in separate tags
    } else if (!raw.length || raw[raw.length - 1][1] !== line) {
      raw.push([0, line]);
    }
  }
  return raw;
}

/** The readable text of a web page, one line per piece of text. */
export function htmlToText(html) {
  return htmlRaw(html).map(([, t]) => t).join('\n');
}

export function fromHtml(html) {
  const raw = htmlRaw(html);
  // A heading level used for dish names: most of its headings have a price within the next 2 lines.
  const dishLevels = new Set();
  for (const level of new Set(raw.map(([lv]) => lv).filter(Boolean))) {
    const idx = raw.map(([lv], i) => (lv === level ? i : -1)).filter((i) => i >= 0);
    const withPrice = idx.filter((i) => raw.slice(i + 1, i + 3).some(([lv, t]) => lv === 0 && PRICE.test(t))).length;
    if (idx.length >= 3 && withPrice >= 0.5 * idx.length) dishLevels.add(level);
  }
  return group(raw.map(([lv, t]) => [dishLevels.has(lv) ? 'dish' : lv ? 'section' : 'text', t]));
}

/** Plain text (from a PDF): a short line with no price, followed by a priced line, is a section heading. */
export function fromText(text) {
  const raw = [];
  let letters = false; // the last line was built from single letters
  for (const l of String(text || '').split('\n').map(clean)) {
    if (!l || l.startsWith('--- Page')) continue;
    // A word printed one letter per line ("S", "I", "D", "E", "S"): put it back together.
    if (/^[A-Za-z]$/.test(l)) {
      if (letters) raw[raw.length - 1] += l;
      else raw.push(l);
      letters = true;
    } else {
      raw.push(l);
      letters = false;
    }
  }
  const marked = raw.some((l) => l.startsWith('## ')); // the PDF reader marked headings by text size
  const lines = raw.map((line, i) => {
    if (line.startsWith('## ')) return ['section', line.slice(3).trim()];
    const next = raw[i + 1] || '';
    const heading = !marked && line.length <= 28 && !PRICE.test(line) && !/\d/.test(line)
      && PRICE.test(next) && !priceOnly(next) && next.length > line.length;
    return [heading ? 'section' : 'text', line];
  });
  return group(lines);
}

/** Best available structure: schema.org data, then the page's headings, then plain text. */
export function structure(text, html = '') {
  if (html) {
    const ld = fromJsonLd(html);
    if (ld.reduce((n, s) => n + s.items.length, 0) >= 3) return ld;
  }
  const byHtml = html ? fromHtml(html) : [];
  const byText = text ? fromText(text) : [];
  // Prefer the page's own headings unless plain text finds clearly more priced dishes.
  if (priced(byHtml) >= 3 && priced(byHtml) >= 0.8 * priced(byText)) return byHtml;
  return byText;
}

export const pricedCount = priced;
