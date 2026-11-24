// Where exactly a place is, in the words you'd use to find it:
//   "corner of George Street and Bathurst Street"
//   "Old Cleveland Road, south side, between Cavendish Road and Amelia Street"
// From the street map (street.js), loaded around the place first.

import * as street from './street.js';
import { bearingDeg, offsetKm, compassName } from './geo.js';

// A corner place: within CORNER_M of where the streets' centre lines cross (a
// big corner building's pin can be 30-40 m from that point on wide roads) AND
// close to both streets. Within ON_M of both: "corner of". Within NEAR_M of
// both: "near the corner of" (a big building whose pin is in its middle, or a
// shop a few doors along: we can't tell which, so don't claim the corner).
const CORNER_M = 55;
const ON_M = 22;
const NEAR_M = 38;
const SEARCH_M = 400;   // how far along its street to look for the cross streets either side
const STEP_M = 10;

const listJoin = (xs) => (xs.length <= 1 ? xs.join('') : `${xs.slice(0, -1).join(', ')} and ${xs[xs.length - 1]}`);

/** The side of the street a point is on, as a compass word: "south side". */
function sideOf(lat, lon, near) {
  if (!near || near.dist < 4) return '';
  const dir = compassName(bearingDeg(near.lat, near.lon, lat, lon)).replace(/-.*$/, '');
  return `${dir} side`;
}

/** The first other street meeting this one, walking from (lat, lon) along bearing, within SEARCH_M. */
function nextCrossStreet(lat, lon, bearing, name) {
  for (let d = STEP_M; d <= SEARCH_M; d += STEP_M) {
    const p = offsetKm(lat, lon, d / 1000, bearing);
    // Stay on the street as it bends.
    const on = street.nearestNamedStreet(p.lat, p.lon, 30, name);
    if (!on || on.name !== name) return '';
    const ix = street.nearestIntersection(on.lat, on.lon, 12);
    if (ix && ix.names.has(name)) {
      const other = [...street.roadNamesAt(ix)].find((n) => n !== name);
      if (other) return other;
    }
  }
  return '';
}

/**
 * A short description of where a place is, or '' if the street map can't
 * say. addressStreet: the street in its address, when known (a hotel backing
 * onto a lane is on its front street). Loads the street map first (up to 8 s).
 */
export async function describeLocation(lat, lon, addressStreet = '') {
  await Promise.race([street.ensureArea(lat, lon), new Promise((r) => setTimeout(r, 8000))]);
  if (!street.areaReady(lat, lon)) return '';
  // On a corner.
  const streetDist = (name) => {
    const s = street.nearestNamedStreet(lat, lon, NEAR_M, name);
    return s?.name === name ? s.dist : Infinity;
  };
  // The nearest such corner, but one on the street in its address first (on a
  // block between two corners, the address says which).
  const corners = street.intersectionsNear(lat, lon, CORNER_M)
    .map((ix) => [...street.roadNamesAt(ix)].map((n) => [n, streetDist(n)]).filter(([, d]) => d <= NEAR_M)
      .sort((x, y) => (y[0] === addressStreet) - (x[0] === addressStreet) || x[1] - y[1]).slice(0, 2))
    .filter((names) => names.length === 2);
  const best = corners.find((names) => names.some(([n]) => n === addressStreet)) || corners[0];
  if (best) {
    const pair = listJoin(best.map(([n]) => n));
    return Math.max(best[0][1], best[1][1]) <= ON_M ? `corner of ${pair}` : `near the corner of ${pair}`;
  }
  // Otherwise: its street (the one in its address when known), which side, and the cross streets either side.
  const near = street.nearestNamedStreet(lat, lon, 60, addressStreet);
  if (!near || !near.name) return '';
  const a = nextCrossStreet(near.lat, near.lon, near.bearing, near.name);
  const b = nextCrossStreet(near.lat, near.lon, (near.bearing + 180) % 360, near.name);
  const side = sideOf(lat, lon, near);
  const between = a && b && a !== b ? `between ${b} and ${a}` : a || b ? `near ${a || b}` : '';
  return [near.name, side, between].filter(Boolean).join(', ');
}
