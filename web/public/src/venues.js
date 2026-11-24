// Menus where you are: "Looks like you're at Ruse Bar and Brasserie. Menu?"
// when the app opens at a food venue, and Menus nearby (under
// More) for the places to eat around the map position.
//
// Only speaks up when the location is good (25 metres or better) and one
// place to eat is clearly nearest. When two to four are about as close,
// it asks which one. Inside a shopping centre or on a busy strip, it
// usually stays quiet.

import * as street from './street.js';
import { settings } from './settings.js';
import { say } from './speech.js';
import { showMenu } from './menu.js';

// Places that have a menu (not supermarkets or butchers).
const EATING = new Set(['restaurant', 'cafe', 'café', 'fast food', 'pub', 'bar', 'biergarten', 'ice cream', 'bakery',
  'food court', 'juice bar', 'coffee', 'bistro', 'takeaway']);
const GOOD_FIX_M = 25;   // worse than this, and the phone can't tell next door from here
const AT_M = 20;         // a place this close may be where you are
const CLOSE_M = 30;      // places this close are listed when there's no clear winner
const NEARBY_M = 300;    // Menus nearby
const NEARBY_MAX = 25;
const QUIET_HOURS = 3;   // after "Not here", don't ask about the same place again for a while

const $ = (id) => document.getElementById(id);
const isEating = (p) => EATING.has(String(p.kind || '').toLowerCase().replace(/_/g, ' '));

function eatingNear(lat, lon, radius, max) {
  return street.placesNear(lat, lon, radius, 200, new Set(['food'])).filter(isEating).slice(0, max);
}

/**
 * Where you might be eating: { at: place } when one is clearly nearest,
 * { choices: [places] } when a few are about as close, or null.
 */
export function venueGuess(lat, lon, accuracy) {
  if (!(accuracy <= GOOD_FIX_M)) return null;
  const near = eatingNear(lat, lon, 60, 8);
  if (!near.length || near[0].dist > AT_M) return null;
  const [first, second] = near;
  if (!second || second.dist >= Math.max(2 * first.dist, first.dist + 15)) return { at: first };
  const close = near.filter((p) => p.dist <= CLOSE_M).slice(0, 4);
  return close.length >= 2 ? { choices: close } : { at: first };
}

const placeKey = (p) => `${p.name}|${p.lat.toFixed(4)},${p.lon.toFixed(4)}`;

function recentlyDismissed(places) {
  const d = settings.get('venueDismissed');
  if (!d || Date.now() - d.t > QUIET_HOURS * 3600e3) return false;
  return places.some((p) => d.keys.includes(placeKey(p)));
}

/**
 * A place's street address, short: "164 Cavendish Road", or '' when unknown.
 * Its own address on the map, else one looked up (lookUpAddresses), else
 * a house number right beside it (any further and it's often next door's).
 */
function shortAddress(p) {
  const own = ((p.details && p.details.address) || '').split(', ')[0];
  if (/\d/.test(own) && /[a-z]{3}/i.test(own)) return own;
  if (p.lookedUp) return p.lookedUp;
  const a = street.nearestAddress(p.lat, p.lon, 12);
  return a && a.street ? `${a.num} ${a.street}` : '';
}

/** Look up the addresses the map doesn't have (HERE, through the Worker), six at a time, for up to 8 seconds. */
async function lookUpAddresses(places) {
  const todo = places.filter((p) => !/\d/.test(((p.details && p.details.address) || '').split(', ')[0]));
  const one = async (p) => {
    const q = new URLSearchParams({ name: p.name, lat: p.lat.toFixed(6), lon: p.lon.toFixed(6) });
    const r = await fetch(`api/venueaddr?${q}`, { signal: AbortSignal.timeout(8000) }).catch(() => null);
    const j = r && r.ok ? await r.json().catch(() => ({})) : {};
    if (j.addr) p.lookedUp = j.addr;
  };
  const all = (async () => {
    for (let i = 0; i < todo.length; i += 6) await Promise.all(todo.slice(i, i + 6).map(one));
  })();
  await Promise.race([all, new Promise((r) => setTimeout(r, 8000))]);
}

/** "Yami Sushi, restaurant, 164 Cavendish Road, 120 metres north" */
function venueText(p) {
  const addr = shortAddress(p);
  if (!addr) return street.placeText(p);
  const [name, kind, ...rest] = street.placeText(p).split(', ');
  return [name, kind, addr, ...rest].join(', ');
}

function button(text, onClick) {
  const b = document.createElement('button');
  b.type = 'button';
  b.textContent = text;
  b.addEventListener('click', onClick);
  return b;
}

function openDialog(title) {
  const dlg = $('venue-dialog');
  $('venue-title').textContent = title;
  $('venue-choices').replaceChildren();
  $('venue-menu').replaceChildren();
  $('venue-menu').hidden = true;
  if (!dlg.open) dlg.showModal();
  return dlg;
}

function menuFor(p) {
  // Leave only the chosen place above its menu.
  $('venue-choices').replaceChildren();
  showMenu($('venue-menu'), { name: p.name, lat: p.lat, lon: p.lon, website: p.details && p.details.website });
}

/**
 * On opening the app at your real location: ask about the menu when you seem
 * to be at a place to eat. Waits for the street data (up to 8 seconds).
 */
export async function checkVenue({ lat, lon, accuracy }) {
  // Expert only, like Routes and Menus nearby: Starter is just the map.
  if (settings.get('level') !== 'expert' || settings.get('venueAsk') === false || !(accuracy <= GOOD_FIX_M)) return;
  await Promise.race([street.ensureArea(lat, lon), new Promise((r) => setTimeout(r, 8000))]);
  if (!street.areaReady(lat, lon) || document.querySelector('dialog[open]')) return;
  const guess = venueGuess(lat, lon, accuracy);
  if (!guess) return;
  const places = guess.at ? [guess.at] : guess.choices;
  if (recentlyDismissed(places)) return;
  await lookUpAddresses(places);
  if (document.querySelector('dialog[open]')) return;
  const notHere = button(guess.at ? 'Not here' : 'None of these', () => {
    settings.set('venueDismissed', { t: Date.now(), keys: places.map(placeKey) });
    $('venue-dialog').close();
  });
  if (guess.at) {
    const p = guess.at;
    const addr = shortAddress(p);
    openDialog(`Looks like you're at ${p.name}${addr ? `, ${addr}` : ''}.`);
    $('venue-choices').append(button(`Menu for ${p.name}`, () => menuFor(p)), notHere);
  } else {
    openDialog('Looks like you\'re at one of these.');
    for (const p of places) $('venue-choices').append(button([p.name, p.kind, shortAddress(p)].filter(Boolean).join(', '), () => menuFor(p)));
    $('venue-choices').append(notHere);
  }
  $('venue-choices').querySelector('button').focus();
}

/** Menus nearby: the places to eat around a point, nearest first; choose one for its menu. */
export async function openMenusNearby(lat, lon) {
  openDialog('Menus nearby');
  const wait = document.createElement('p');
  wait.textContent = 'Finding places to eat…';
  $('venue-choices').append(wait);
  $('venue-title').focus();
  await Promise.race([street.ensureRadius(lat, lon, NEARBY_M), new Promise((r) => setTimeout(r, 15000))]);
  const near = eatingNear(lat, lon, NEARBY_M, NEARBY_MAX);
  await lookUpAddresses(near);
  $('venue-choices').replaceChildren();
  if (!near.length) {
    const p = document.createElement('p');
    p.textContent = `No food venues found within ${NEARBY_M} metres.`;
    $('venue-choices').append(p);
    say(p.textContent);
    return;
  }
  const ul = document.createElement('ul');
  ul.className = 'result-list';
  for (const p of near) {
    const li = document.createElement('li');
    li.append(button(venueText(p), () => menuFor(p)));
    ul.append(li);
  }
  $('venue-choices').append(ul);
  $('venue-title').textContent = `Menus nearby: ${near.length} places to eat`;
  ul.querySelector('button').focus();
}
