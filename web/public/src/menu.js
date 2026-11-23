// Menus for food places, through the Worker's /api/menu (a port of the
// desktop's menu search): the venue's own menu page, and its own store page
// on Uber Eats, DoorDash or Menulog when one names both the venue and its
// suburb. A menu page on the venue's own site can often be read right here.

import { say } from './speech.js';
import { postcodeAt } from './postcode.js';

const DELIVERY = { 'ubereats.com': 'Uber Eats', 'doordash.com': 'DoorDash', 'menulog.com.au': 'Menulog', 'deliveroo.com.au': 'Deliveroo' };

function where(url) {
  let host = '';
  try { host = new URL(url).hostname.replace(/^www\./, ''); } catch { return 'link'; }
  const d = Object.keys(DELIVERY).find((k) => host === k || host.endsWith(`.${k}`));
  return d ? DELIVERY[d] : host;
}

/**
 * Find and show the menu for a place in `box` (an empty element).
 * place: { name, lat, lon, website }
 */
export async function showMenu(box, place) {
  box.hidden = false;
  box.replaceChildren();
  say(`Looking for the menu for ${place.name}…`);
  const pc = await postcodeAt(place.lat, place.lon).catch(() => null);
  const params = new URLSearchParams({ name: place.name, suburb: (pc && pc.place) || '' });
  if (place.website) params.set('website', place.website);
  let results = [];
  try {
    const r = await fetch(`api/menu?${params}`, { signal: AbortSignal.timeout(45000) });
    if (r.ok) results = (await r.json()).results || [];
  } catch { /* none */ }
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
      : `${r.pdf ? 'Menu (PDF)' : r.title || 'Menu'} on ${site}`;
    li.append(a);
    // The venue's own (not PDF) menu page: offer to read it here.
    if (r.kind === 'own' && !r.pdf) {
      const b = document.createElement('button');
      b.type = 'button';
      b.textContent = 'Read it here';
      b.addEventListener('click', () => readMenu(r.url, text, place.name));
      li.append(' ', b);
    }
    ul.append(li);
  }
  box.append(ul, text);
  const own = results.filter((r) => r.kind === 'own').length;
  const delivery = results.length - own;
  say(`${place.name}: ${[own && `${own} menu page${own > 1 ? 's' : ''} on its own website`, delivery && `${delivery} delivery page${delivery > 1 ? 's' : ''}`].filter(Boolean).join(' and ')}.`);
  h.focus();
}

async function readMenu(url, box, name) {
  say('Reading the menu…');
  let text = '';
  try {
    const r = await fetch(`api/menutext?url=${encodeURIComponent(url)}`, { signal: AbortSignal.timeout(20000) });
    if (r.ok) text = (await r.json()).text || '';
  } catch { /* below */ }
  box.replaceChildren();
  if (text.length < 200) {
    const p = document.createElement('p');
    p.textContent = `This menu page can't be read here (the site builds it as you view it). Open the link instead.`;
    box.append(p);
    return say(p.textContent);
  }
  const h = document.createElement('h4');
  h.tabIndex = -1;
  h.textContent = `${name} menu`;
  box.append(h, ...text.split('\n').map((line) => {
    const p = document.createElement('p');
    p.textContent = line;
    return p;
  }));
  h.focus();
  say(`${name} menu. ${text.split('\n').length} lines.`);
}
