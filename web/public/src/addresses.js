// Street addresses for lists of places (Places nearby, Menus nearby):
// "Yami Sushi, restaurant, 164 Cavendish Road, here".
//
// A place's own address on the map when it has one; else looked up from HERE
// through the Worker (Expert); else a house number right beside it (any
// further and it's often the shop next door's).

import * as street from './street.js';

/** "164 Cavendish Road", or '' when unknown. */
export function shortAddress(p) {
  const own = ((p.details && p.details.address) || '').split(', ')[0];
  if (/\d/.test(own) && /[a-z]{3}/i.test(own)) return own;
  if (p.lookedUp) return p.lookedUp;
  const a = street.nearestAddress(p.lat, p.lon, 12);
  return a && a.street ? `${a.num} ${a.street}` : '';
}

// Bus stops, stations, parks and the like have no street address to find.
const NO_ADDRESS = new Set(['transport', 'trains', 'parks']);

/** Look up the addresses the map doesn't have, six at a time, for up to 8 seconds. */
export async function lookUpAddresses(places) {
  const todo = places.filter((p) => !p.lookedUp && !NO_ADDRESS.has(p.category)
    && !/\d/.test(((p.details && p.details.address) || '').split(', ')[0]));
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

/** street.placeText with the address before the distance: "Yami Sushi, restaurant, 164 Cavendish Road, here". */
export function placeTextWithAddress(p) {
  const text = street.placeText(p);
  const addr = shortAddress(p);
  if (!addr) return text;
  const i = text.lastIndexOf(', ');
  return `${text.slice(0, i)}, ${addr}${text.slice(i)}`;
}
