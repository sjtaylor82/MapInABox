// Menus for food places, through the Worker's /api/menu (a port of the
// desktop's menu search, with link finding from the menu browser prototype):
// the venue's own menu pages and PDFs, and its own store page on Uber Eats,
// DoorDash or Menulog when one names both the venue and its suburb. Menus on
// the venue's own site can be read right here, as sections of dishes with
// prices: web pages are read by the Worker, PDFs in the browser. With none
// of those, a customer's photo of the menu on RestaurantGuru can be opened or
// shared to a reading app such as Seeing AI.

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

/** When a menu was last changed ('2026-05-02' or '2026-05'): 'updated 7 months ago', or ''. */
export function updatedText(date, now = new Date()) {
  const m = /^(\d{4})-(\d{2})/.exec(date || '');
  if (!m) return '';
  const months = (now.getFullYear() - Number(m[1])) * 12 + (now.getMonth() + 1 - Number(m[2]));
  if (months <= 0) return 'updated this month';
  if (months === 1) return 'updated last month';
  if (months < 24) return `updated ${months} months ago`;
  return `updated ${Math.floor(months / 12)} years ago`;
}

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
  // v: bumped when the answer's shape changes, so browsers don't keep an old copy for a week.
  const params = new URLSearchParams({ name: place.name, suburb: (pc && pc.place) || '', v: '5' });
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
    // Menu photos: fetched and listed here, not RestaurantGuru's own page (with its adverts and other restaurants).
    if (r.kind === 'photo') {
      const b = document.createElement('button');
      b.type = 'button';
      b.textContent = `Photos of the menu, newest ${r.age || 'of unknown date'}`;
      b.addEventListener('click', () => getPhoto(r.url, text, place.name));
      li.append(b);
      ul.append(li);
      continue;
    }
    const a = document.createElement('a');
    a.href = r.url;
    a.target = '_blank';
    a.rel = 'noopener';
    const site = where(r.url);
    a.textContent = r.kind === 'delivery'
      ? `${place.name} on ${site}`
      : r.kind === 'facebook'
        ? `${place.name} on Facebook (menu photos may be in its posts)`
        : `${r.pdf ? `${r.title && !/\.pdf$/i.test(r.title) ? r.title : 'Menu'} (PDF)` : r.title || 'Menu'} on ${site}`
          + (r.updated ? `, ${updatedText(r.updated)}` : '');
    li.append(a);
    // A menu page whose menu is pictures: list them here, to share to a reading app.
    if (r.kind === 'own' && !r.readable && !r.pdf && r.pictures) {
      const b = document.createElement('button');
      b.type = 'button';
      b.textContent = r.pictures === 1 ? 'Menu picture' : `Menu pictures (${r.pictures})`;
      b.addEventListener('click', () => getPhoto(r.url, text, place.name));
      li.append(' ', b);
    }
    // A menu on the venue's own site that can be read here: a page with prices, or a PDF.
    if (r.kind === 'own' && (r.readable || r.pdf)) {
      const b = document.createElement('button');
      b.type = 'button';
      b.textContent = 'Read it here';
      b.addEventListener('click', () => (r.pdf ? readPdf(r.url, text, place.name) : readMenu(r.url, text, place.name)));
      li.append(' ', b);
    }
    ul.append(li);
  }
  box.append(ul, text);
  const readable = results.filter((r) => r.kind === 'own' && (r.readable || r.pdf)).length;
  const own = results.filter((r) => r.kind === 'own').length;
  const photos = results.filter((r) => r.kind === 'photo').length;
  const facebook = results.filter((r) => r.kind === 'facebook').length;
  const delivery = results.length - own - photos - facebook;
  const parts = [
    readable && `${plural(readable, 'menu')} you can read here`,
    own - readable && `${plural(own - readable, 'other menu page')}`,
    photos && 'photos of the menu',
    delivery && `${plural(delivery, 'delivery page')}`,
    facebook && 'its Facebook page',
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

/** "Muzzas Pies menu 2.jpg" */
function photoFile(blob, name, n) {
  const safe = name.replace(/[^\w' -]+/g, '').replace(/\s+/g, ' ').trim() || 'Menu';
  const ext = /png/.test(blob.type) ? 'png' : /webp/.test(blob.type) ? 'webp' : 'jpg';
  return new File([blob], `${safe} menu${n ? ` ${n}` : ''}.${ext}`, { type: blob.type || 'image/jpeg' });
}

/** A list item for one photo: "Page 1 of 3", Share the photo (on a phone), Open the photo. */
function photoItem(file, label) {
  const li = document.createElement('li');
  li.append(`${label} `);
  if (navigator.canShare && navigator.canShare({ files: [file] })) {
    const share = document.createElement('button');
    share.type = 'button';
    share.textContent = 'Share';
    share.setAttribute('aria-label', `Share ${label.toLowerCase()}`);
    share.addEventListener('click', () => navigator.share({ files: [file] }).catch(() => {}));
    li.append(share, ' ');
  }
  const open = document.createElement('a');
  open.href = URL.createObjectURL(file);
  open.target = '_blank';
  open.rel = 'noopener';
  open.download = file.name;
  open.textContent = 'Open';
  open.setAttribute('aria-label', `Open ${label.toLowerCase()}`);
  li.append(open);
  return li;
}

/**
 * Photos of the menu (RestaurantGuru): the newest menu first, then older
 * ones with how old they are. Each can be shared straight to a reading app
 * such as Seeing AI (the share sheet, on a phone), or opened. The photos are
 * fetched first, so Share works the moment it's pressed.
 */
async function getPhoto(pageUrl, box, name) {
  const done = working(null, `Getting the photos of the menu for ${name}`);
  const versions = [];
  try {
    const list = await fetch(`api/menuphotos?url=${encodeURIComponent(pageUrl)}`, { signal: AbortSignal.timeout(25000) })
      .then((r) => (r.ok ? r.json() : {})).catch(() => ({}));
    let n = 0;
    for (const v of (list.versions || []).slice(0, 4)) {
      const files = [];
      for (const u of v.images.slice(0, 4)) {
        const r = await fetch(`api/menuimage?u=${encodeURIComponent(u)}`, { signal: AbortSignal.timeout(20000) }).catch(() => null);
        if (r && r.ok) files.push(photoFile(await r.blob(), name, ++n));
      }
      if (files.length) versions.push({ ...v, files });
    }
  } finally {
    done();
  }
  if (!versions.length) return showProblem(box, 'The photos of the menu could not be found. Open the link instead.');
  box.replaceChildren();
  const h = document.createElement('h4');
  h.tabIndex = -1;
  h.textContent = versions[0].who === 'site' ? `Pictures of the ${name} menu` : `Photos of the ${name} menu`;
  const note = document.createElement('p');
  note.textContent = 'Share a photo to a reading app such as Seeing AI to hear it.';
  box.append(h, note);
  const describe = (v) => `${v.who === 'site' ? 'On its website' : v.who === 'owner' ? 'From the owner' : 'From a customer'}${v.age ? `, ${v.age}` : ''}`;
  versions.forEach((v, i) => {
    const sub = document.createElement('h5');
    sub.textContent = versions.length === 1 ? describe(v) : i === 0 ? `Newest menu: ${describe(v)}` : `Older menu: ${describe(v)}`;
    const ul = document.createElement('ul');
    ul.className = 'result-list';
    v.files.forEach((f, j) => ul.append(photoItem(f, v.files.length > 1 ? `Page ${j + 1} of ${v.files.length}` : 'Photo')));
    box.append(sub, ul);
  });
  const photos = versions.reduce((n, v) => n + v.files.length, 0);
  const what = versions[0].who === 'site' ? 'picture' : 'photo';
  say(`${photos === 1 ? `One ${what}` : `${photos} ${what}s`} of the ${name} menu. ${versions.length > 1 ? 'Newest: ' : ''}${describe(versions[0]).toLowerCase()}.`);
  h.focus();
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
