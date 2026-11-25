// Menus where you are: "Looks like you're at Ruse Bar and Brasserie. Menu?"
// when the app opens at a food venue.
//
// Only speaks up when the location is good (25 metres or better) and one
// place to eat is clearly nearest. When two to four are about as close,
// it asks which one. Inside a shopping centre or on a busy strip, it
// usually stays quiet.

import * as street from './street.js';
import { settings } from './settings.js';
import { say } from './speech.js';
import { showMenu } from './menu.js';
import { shortAddress, lookUpAddresses } from './addresses.js';

// Places that have a menu (not supermarkets or butchers).
const EATING = new Set(['restaurant', 'cafe', 'café', 'fast food', 'pub', 'bar', 'biergarten', 'ice cream', 'bakery',
  'food court', 'juice bar', 'coffee', 'bistro', 'takeaway']);
const GOOD_FIX_M = 25;   // worse than this, and the phone can't tell next door from here
const AT_M = 20;         // a place this close may be where you are
const CLOSE_M = 30;      // places this close are listed when there's no clear winner
const QUIET_HOURS = 3;   // after "Not here", don't ask about the same place again for a while

const $ = (id) => document.getElementById(id);
const isEating = (p) => EATING.has(String(p.kind || '').toLowerCase().replace(/_/g, ' '));

function eatingNear(lat, lon, radius, max) {
  return street.placesNear(lat, lon, radius, 2000, new Set(['food'])).filter(isEating).slice(0, max);
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
  // Expert only, like Routes: Starter is just the map.
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
