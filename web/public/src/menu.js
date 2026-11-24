// Menus for food places, through the Worker's /api/menu (a port of the
// desktop's menu search, with link finding from the menu browser prototype):
// the venue's own menu pages and PDFs, and its own store page on Uber Eats,
// DoorDash or Menulog when one names both the venue and its suburb. Menus on
// the venue's own site can be read right here, as sections of dishes with
// prices: web pages are read by the Worker, PDFs in the browser. With none
// of those, a customer's photo of the menu on RestaurantGuru can be read by AI.

import { say } from './speech.js';
import { postcodeAt } from './postcode.js';
import { structure, pricedCount } from './menustructure.js';
import { working } from './busy.js';

const DELIVERY = { 'ubereats.com': 'Uber Eats', 'doordash.com': 'DoorDash', 'menulog.com.au': 'Menulog', 'deliveroo.com.au': 'Deliveroo' };

function where(url) {
  let host = '';
  try { host = new URL(url).hostname.replace(/^www\./, ''); } catch { return 'link'; }
  const d = Object.keys(DELIVERY).find((k) => host === k || host.endsWith(`.${k}`));
  return d ? DELIVERY[d] : host;
}

const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

/**
 * Find and show the menu for a place in `box` (an empty element).
 * place: { name, lat, lon, website }
 */
export async function showMenu(box, place) {
  box.hidden = false;
  // Shown in the box while the search runs, with a reminder every 10 seconds.
  const wait = document.createElement('p');
  wait.className = 'working-status';
  wait.textContent = `Looking for the menu for ${place.name}… This can take up to half a minute.`;
  box.replaceChildren(wait);
  const done = working(null, `Looking for the menu for ${place.name}`, { slow: true });
  const pc = await postcodeAt(place.lat, place.lon).catch(() => null);
  const params = new URLSearchParams({ name: place.name, suburb: (pc && pc.place) || '' });
  if (place.website) params.set('website', place.website);
  let results = [];
  try {
    const r = await fetch(`api/menu?${params}`, { signal: AbortSignal.timeout(45000) });
    if (r.ok) results = (await r.json()).results || [];
  } catch { /* none */ }
  done();
  box.replaceChildren();
  const h = document.createElement('h3');
  h.tabIndex = -1;
  h.textContent = `Menu for ${place.name}`;
  box.append(h);
  if (!results.length) {
    const p = document.createElement('p');
    p.textContent = `No menu found for ${place.name}. Small places often have none online.`;
    box.append(p);
    say(p.textContent);
    return;
  }
  const ul = document.createElement('ul');
  ul.className = 'result-list';
  const text = document.createElement('div');
  text.className = 'menu-text';
  for (const r of results) {
    const li = document.createElement('li');
    const a = document.createElement('a');
    a.href = r.url;
    a.target = '_blank';
    a.rel = 'noopener';
    const site = where(r.url);
    a.textContent = r.kind === 'delivery'
      ? `${place.name} on ${site}`
      : r.kind === 'photo'
        ? `Customer photo of the menu on ${site}`
        : `${r.pdf ? `${r.title && !/\.pdf$/i.test(r.title) ? r.title : 'Menu'} (PDF)` : r.title || 'Menu'} on ${site}`;
    li.append(a);
    // A menu on the venue's own site that can be read here: a page with prices, or a PDF.
    if ((r.kind === 'own' && (r.readable || r.pdf)) || r.kind === 'photo') {
      const b = document.createElement('button');
      b.type = 'button';
      b.textContent = 'Read it here';
      b.addEventListener('click', () => (r.kind === 'photo' ? readPhoto(r.url, text, place.name)
        : r.pdf ? readPdf(r.url, text, place.name) : readMenu(r.url, text, place.name)));
      li.append(' ', b);
    }
    ul.append(li);
  }
  box.append(ul, text);
  const readable = results.filter((r) => r.kind === 'own' && (r.readable || r.pdf)).length;
  const own = results.filter((r) => r.kind === 'own').length;
  const photos = results.filter((r) => r.kind === 'photo').length;
  const delivery = results.length - own - photos;
  const parts = [
    readable && `${plural(readable, 'menu')} you can read here`,
    own - readable && `${plural(own - readable, 'other menu page')}`,
    photos && 'a customer photo of the menu, which can be read here',
    delivery && `${plural(delivery, 'delivery page')}`,
  ].filter(Boolean);
  say(`${place.name}: ${parts.join(', ')}.`);
  h.focus();
}

async function readMenu(url, box, name) {
  say('Reading the menu…');
  let data = {};
  try {
    const r = await fetch(`api/menutext?url=${encodeURIComponent(url)}`, { signal: AbortSignal.timeout(20000) });
    if (r.ok) data = await r.json();
  } catch { /* below */ }
  const sections = data.sections || [];
  if (pricedCount(sections) >= 3) return showSections(box, name, sections);
  const text = data.text || '';
  if (text.length < 200) {
    return showProblem(box, `This menu page can't be read here (the site builds it as you view it). Open the link instead.`);
  }
  showLines(box, name, text);
}

async function readPdf(url, box, name) {
  say('Reading the PDF menu. This can take a few seconds…');
  let text = '';
  try {
    const r = await fetch(`api/menupdf?url=${encodeURIComponent(url)}`, { signal: AbortSignal.timeout(30000) });
    if (!r.ok) throw new Error(String(r.status));
    const { pdfText } = await import('./menupdf.js');
    text = await pdfText(await r.arrayBuffer());
  } catch {
    return showProblem(box, `This PDF menu couldn't be opened here. Open the link instead.`);
  }
  if (text.replace(/--- Page \d+ ---|\s/g, '').length < 80) {
    return showProblem(box, `This PDF is a picture of the menu, so it can't be read here. Open the link instead.`);
  }
  const sections = structure(text);
  if (pricedCount(sections) >= 3) return showSections(box, name, sections);
  showLines(box, name, text.replace(/^## /gm, '').replace(/^--- Page \d+ ---$/gm, ''));
}

/**
 * A customer's photo of the menu (RestaurantGuru), read by AI. The photo is
 * cut into two enlarged halves first: the AI reads small writing on menu
 * boards far better that way.
 */
async function readPhoto(pageUrl, box, name) {
  const done = working(null, `Reading the photo of the menu for ${name}`);
  try {
    const list = await fetch(`api/menuphotos?url=${encodeURIComponent(pageUrl)}`, { signal: AbortSignal.timeout(20000) })
      .then((r) => (r.ok ? r.json() : {})).catch(() => ({}));
    const photo = (list.images || [])[0];
    if (!photo) return showProblem(box, 'The photo of the menu could not be found. Open the link instead.');
    let parts;
    try {
      const img = await fetch(`api/menuimage?u=${encodeURIComponent(photo)}`, { signal: AbortSignal.timeout(20000) });
      if (!img.ok) throw new Error(String(img.status));
      parts = await photoParts(await createImageBitmap(await img.blob()));
    } catch {
      return showProblem(box, 'The photo of the menu could not be opened. Open the link instead.');
    }
    let data = {};
    let status = 0;
    try {
      const r = await fetch('api/menuread', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ photo, parts }),
        signal: AbortSignal.timeout(70000),
      });
      status = r.status;
      data = await r.json();
    } catch { /* below */ }
    const sections = data.sections || [];
    if (!sections.length) {
      return showProblem(box, status === 429 ? 'The menu reader is busy. Try again in a minute.'
        : data.error || 'No menu could be read from the photo. Open the link instead.');
    }
    showSections(box, name, sections);
    const note = document.createElement('p');
    note.textContent = 'Read by AI from a photo a customer took, which may be old. Some names or prices may be wrong.';
    box.querySelector('h4').after(note);
  } finally {
    done();
  }
}

/** The photo as JPEG data URLs: two overlapping halves of a wide or tall photo, each about 1600 pixels across. */
async function photoParts(bmp) {
  const { width: w, height: h } = bmp;
  const pieces = w >= h * 1.3 ? [[0, 0, w * 0.58, h], [w * 0.42, 0, w * 0.58, h]]
    : h >= w * 1.3 ? [[0, 0, w, h * 0.58], [0, h * 0.42, w, h * 0.58]]
      : [[0, 0, w, h]];
  return pieces.map(([x, y, pw, ph]) => {
    const scale = Math.min(3, 1600 / Math.max(pw, ph));
    const c = document.createElement('canvas');
    c.width = Math.round(pw * scale);
    c.height = Math.round(ph * scale);
    const g = c.getContext('2d');
    g.imageSmoothingQuality = 'high';
    g.drawImage(bmp, x, y, pw, ph, 0, 0, c.width, c.height);
    return c.toDataURL('image/jpeg', 0.85);
  });
}

function showProblem(box, message) {
  box.replaceChildren();
  const p = document.createElement('p');
  p.textContent = message;
  box.append(p);
  say(message);
}

/** "24" -> "$24", "$9.50" stays, "Large $19.95, X Large $24.95" stays. */
function speakablePrice(p) {
  return (/^\d/.test(p) ? `$${p}` : p).replace(/(\d)\s*ea$/i, '$1 each');
}

/** Sections as headings, each with a list of dishes: "Beef burger, $22. With chips." */
function showSections(box, name, sections) {
  box.replaceChildren();
  const h = document.createElement('h4');
  h.tabIndex = -1;
  h.textContent = `${name} menu`;
  box.append(h);
  let dishes = 0;
  for (const s of sections) {
    const sh = document.createElement('h5');
    sh.textContent = s.title;
    const ul = document.createElement('ul');
    ul.className = 'menu-dishes';
    for (const i of s.items) {
      const li = document.createElement('li');
      li.textContent = [i.name, i.price && speakablePrice(i.price)].filter(Boolean).join(', ') + (i.desc ? `. ${i.desc}` : '');
      ul.append(li);
      dishes++;
    }
    box.append(sh, ul);
  }
  h.textContent = `${name} menu: ${plural(sections.length, 'section')}, ${dishes} dish${dishes === 1 ? '' : 'es'}`;
  h.focus();
}

function showLines(box, name, text) {
  box.replaceChildren();
  const lines = text.split('\n').map((l) => l.trim()).filter(Boolean);
  const h = document.createElement('h4');
  h.tabIndex = -1;
  h.textContent = `${name} menu`;
  box.append(h, ...lines.map((line) => {
    const p = document.createElement('p');
    p.textContent = line;
    return p;
  }));
  h.focus();

}
