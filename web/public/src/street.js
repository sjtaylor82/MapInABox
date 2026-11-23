// Street mode data: streets and places around the cursor, from OpenStreetMap.
//
// Tiles (OpenFreeMap vector tiles, see vectortile.js) are loaded as the cursor reaches them. Streets
// are indexed on a grid of small cells so the nearest street, corner or
// place can be found quickly on every step.

import { tileOf, tileBounds } from './vectortile.js';
import { streetTile } from './online.js';
import { formatDistance, compassName, bearingDeg } from './geo.js';

const CELL = 0.001;        // index cell, degrees (about 110 m)
const M = 111000;          // metres per degree of latitude, as in walk.py
const RETRY_MS = 30000;    // wait before asking again for a tile that failed

const ways = [];           // { id, name, kind, pts: [[lat, lon], ...] }
const wayIds = new Set();
const pois = [];           // { name, kind, lat, lon }
const poiIds = new Set();
const segGrid = new Map(); // cell -> [[wayIndex, segmentIndex], ...]
const poiGrid = new Map(); // cell -> [poi, ...]
const nodes = new Map();   // "lat,lon" -> { lat, lon, names: Set, refs: [[wayIndex, vertexIndex], ...] }
const ixGrid = new Map();  // cell -> [node, ...] where two or more streets meet
const tiles = new Map();   // "ty,tx" -> { promise, ok, error, at }

const rad = (d) => (d * Math.PI) / 180;
const cellOf = (v) => Math.floor(v / CELL);
const ckey = (cy, cx) => `${cy},${cx}`;

function pushCell(grid, key, item) {
  const list = grid.get(key);
  if (list) list.push(item);
  else grid.set(key, [item]);
}

/** Every grid cell within radius metres of a point. */
function* cellsNear(lat, lon, radiusM) {
  const dLat = radiusM / M;
  const dLon = radiusM / (M * Math.max(0.01, Math.cos(rad(lat))));
  const y0 = cellOf(lat - dLat);
  const y1 = cellOf(lat + dLat);
  const x0 = cellOf(lon - dLon);
  const x1 = cellOf(lon + dLon);
  for (let y = y0; y <= y1; y++) for (let x = x0; x <= x1; x++) yield ckey(y, x);
}

/** Metres between two points, flat-earth, as walk.py. */
export function metres(lat1, lon1, lat2, lon2) {
  const dy = (lat2 - lat1) * M;
  const dx = (lon2 - lon1) * M * Math.cos(rad((lat1 + lat2) / 2));
  return Math.hypot(dx, dy);
}

function angleDiff(a, b) {
  return Math.abs(((a - b + 540) % 360) - 180);
}

// ── Loading ───────────────────────────────────────────────────────────────

function addWay(w) {
  if (wayIds.has(w.id)) return;
  wayIds.add(w.id);
  const pts = [];
  for (let i = 0; i + 1 < w.c.length; i += 2) pts.push([w.c[i], w.c[i + 1]]);
  if (pts.length < 2) return;
  const wi = ways.length;
  ways.push({ id: w.id, name: w.n, kind: w.k, pts });
  for (let i = 0; i + 1 < pts.length; i++) {
    const [la1, lo1] = pts[i];
    const [la2, lo2] = pts[i + 1];
    for (let y = cellOf(Math.min(la1, la2)); y <= cellOf(Math.max(la1, la2)); y++) {
      for (let x = cellOf(Math.min(lo1, lo2)); x <= cellOf(Math.max(lo1, lo2)); x++) {
        pushCell(segGrid, ckey(y, x), [wi, i]);
      }
    }
  }
  pts.forEach(([lat, lon], vi) => {
    const key = `${lat},${lon}`;
    let node = nodes.get(key);
    if (!node) {
      node = { lat, lon, names: new Set(), refs: [] };
      nodes.set(key, node);
    }
    node.refs.push([wi, vi]);
    if (!node.names.has(w.n)) {
      node.names.add(w.n);
      if (node.names.size === 2) pushCell(ixGrid, ckey(cellOf(lat), cellOf(lon)), node);
    }
  });
}

function addPlace(p) {
  // The same place can arrive from neighbouring tiles: one name at one spot is one place.
  const key = `${p.n}|${p.la.toFixed(5)}|${p.lo.toFixed(5)}`;
  if (poiIds.has(p.id) || poiIds.has(key)) return;
  poiIds.add(p.id);
  poiIds.add(key);
  const poi = { name: p.n, kind: p.k, lat: p.la, lon: p.lo };
  pois.push(poi);
  pushCell(poiGrid, ckey(cellOf(poi.lat), cellOf(poi.lon)), poi);
}

function loadTile(ty, tx) {
  const key = `${ty},${tx}`;
  const known = tiles.get(key);
  if (known && (known.ok !== false || performance.now() - known.at < RETRY_MS)) return known.promise;
  const entry = { ok: null, error: '', at: performance.now() };
  entry.promise = streetTile(ty, tx).then(
    (data) => {
      for (const w of data.ways || []) addWay(w);
      for (const p of data.pois || []) addPlace(p);
      entry.ok = true;
      return { ok: true };
    },
    (err) => {
      entry.ok = false;
      entry.at = performance.now();
      entry.error = err.message || 'The street data could not be loaded.';
      return { ok: false, error: entry.error };
    },
  );
  tiles.set(key, entry);
  return entry.promise;
}

/**
 * Make sure the streets around a point are loaded. Resolves { ok, error }
 * once the tile under the point is ready; tiles the point is close to the
 * edge of are fetched as well, without waiting for them.
 */
export function ensureArea(lat, lon) {
  const { ty, tx } = tileOf(lat, lon);
  const main = loadTile(ty, tx);
  const [s, w, n, e] = tileBounds(ty, tx);
  const mLat = 400 / M;
  const mLon = 400 / (M * Math.max(0.01, Math.cos(rad(lat))));
  const dy = lat - s < mLat ? -1 : n - lat < mLat ? 1 : 0;
  const dx = lon - w < mLon ? -1 : e - lon < mLon ? 1 : 0;
  if (dy) loadTile(ty + dy, tx);
  if (dx) loadTile(ty, tx + dx);
  if (dy && dx) loadTile(ty + dy, tx + dx);
  return main;
}

/** True when the tile under a point has loaded. */
export function areaReady(lat, lon) {
  const { ty, tx } = tileOf(lat, lon);
  return tiles.get(`${ty},${tx}`)?.ok === true;
}

/** The tile under a point as [minLat, maxLat, minLon, maxLon], for spatial tones. */
export function areaBounds(lat, lon) {
  const { ty, tx } = tileOf(lat, lon);
  const [s, w, n, e] = tileBounds(ty, tx);
  return [s, n, w, e];
}

export function counts() {
  return { ways: ways.length, places: pois.length, tiles: [...tiles.values()].filter((t) => t.ok).length };
}

// ── Finding streets ───────────────────────────────────────────────────────

/** Nearest point on segment si of way wi to a point, in local metres. */
function project(wi, si, lat, lon) {
  const [la1, lo1] = ways[wi].pts[si];
  const [la2, lo2] = ways[wi].pts[si + 1];
  const k = M * Math.cos(rad(lat));
  const ax = (lo1 - lon) * k;
  const ay = (la1 - lat) * M;
  const bx = (lo2 - lon) * k;
  const by = (la2 - lat) * M;
  const dx = bx - ax;
  const dy = by - ay;
  const len2 = dx * dx + dy * dy;
  let t = len2 > 0 ? -(ax * dx + ay * dy) / len2 : 0;
  t = Math.max(0, Math.min(1, t));
  const px = ax + t * dx;
  const py = ay + t * dy;
  return {
    dist: Math.hypot(px, py),
    t,
    len: Math.sqrt(len2),
    lat: la1 + t * (la2 - la1),
    lon: lo1 + t * (lo2 - lo1),
  };
}

function segmentsNear(lat, lon, radiusM) {
  const seen = new Set();
  const out = [];
  for (const key of cellsNear(lat, lon, radiusM)) {
    for (const [wi, si] of segGrid.get(key) || []) {
      const id = wi * 100000 + si;
      if (seen.has(id)) continue;
      seen.add(id);
      out.push([wi, si]);
    }
  }
  return out;
}

/**
 * The nearest street within maxM metres: { name, kind, dist, lat, lon, wi, si, t },
 * or null. A street called `prefer` wins over one up to 8 metres nearer.
 */
export function nearestStreet(lat, lon, maxM = 1500, prefer = '') {
  let best = null;
  let bestScore = Infinity;
  for (const [wi, si] of segmentsNear(lat, lon, maxM)) {
    const p = project(wi, si, lat, lon);
    if (p.dist > maxM) continue;
    const score = p.dist - (prefer && ways[wi].name === prefer ? 8 : 0);
    if (score < bestScore) {
      bestScore = score;
      best = { name: ways[wi].name, kind: ways[wi].kind, dist: p.dist, lat: p.lat, lon: p.lon, wi, si, t: p.t };
    }
  }
  return best;
}

/** The nearest corner where two or more named streets meet, within maxM metres. */
export function nearestIntersection(lat, lon, maxM = 25) {
  let best = null;
  for (const key of cellsNear(lat, lon, maxM)) {
    for (const node of ixGrid.get(key) || []) {
      const d = metres(lat, lon, node.lat, node.lon);
      if (d <= maxM && (!best || d < best.dist)) best = { ...node, dist: d };
    }
  }
  return best;
}

function listJoin(items) {
  if (items.length <= 1) return items.join('');
  return `${items.slice(0, -1).join(', ')} and ${items[items.length - 1]}`;
}

const roughly = (m) => formatDistance(m < 100 ? Math.round(m / 5) * 5 : Math.round(m / 10) * 10);

/**
 * What to say for a point: "Smith Street", "Smith Street at Jones Road",
 * or "Smith Street, 40 metres east" when off the street.
 */
export function streetLabel(lat, lon, prefer = '') {
  if (!ways.length) return 'No streets loaded here.';
  const near = nearestStreet(lat, lon, 1500, prefer);
  const ix = nearestIntersection(lat, lon, 20);
  if (ix) {
    const names = [...ix.names];
    const primary = (prefer && names.includes(prefer) && prefer)
      || (near && names.includes(near.name) && near.name) || names[0];
    return `${primary} at ${listJoin(names.filter((n) => n !== primary))}`;
  }
  if (!near) return `No streets within ${formatDistance(1500)}.`;
  if (near.dist <= 20) return near.name;
  const dir = compassName(bearingDeg(lat, lon, near.lat, near.lon));
  if (near.dist <= 250) return `${near.name}, ${roughly(near.dist)} ${dir}`;
  return `No street here. The nearest is ${near.name}, ${roughly(near.dist)} ${dir}`;
}

// ── Following streets ─────────────────────────────────────────────────────

function nodeAt(lat, lon) {
  return nodes.get(`${lat},${lon}`);
}

/**
 * Move up to distM metres along the streets, the way closest to heading.
 * Stops early at a corner, so corners are never skipped.
 * Returns null when not on a street (within 40 metres),
 * { none: true, name } when no street goes that way, or
 * { lat, lon, name, corner, end } after moving.
 */
export function followStep(lat, lon, heading, distM, prefer = '') {
  const snap = nearestStreet(lat, lon, 40, prefer);
  if (!snap) return null;
  const current = prefer || snap.name;
  let best = null;
  for (const [wi, si] of segmentsNear(snap.lat, snap.lon, 12)) {
    const p = project(wi, si, snap.lat, snap.lon);
    if (p.dist > 12) continue;
    const [a, b] = [ways[wi].pts[si], ways[wi].pts[si + 1]];
    for (const dir of [1, -1]) {
      const remaining = dir > 0 ? (1 - p.t) * p.len : p.t * p.len;
      if (remaining < 1) continue;
      const bearing = dir > 0 ? bearingDeg(a[0], a[1], b[0], b[1]) : bearingDeg(b[0], b[1], a[0], a[1]);
      const diff = angleDiff(bearing, heading);
      if (diff > 70) continue;
      const score = diff + (ways[wi].name === current ? 0 : 12) + p.dist;
      if (!best || score < best.score) best = { score, wi, si, t: p.t, dir, lat: p.lat, lon: p.lon };
    }
  }
  if (!best) return { none: true, name: current };
  return walkAlong(best, distM, heading);
}

function walkAlong(start, distM, heading) {
  let { wi, dir } = start;
  let pts = ways[wi].pts;
  let idx = dir > 0 ? start.si + 1 : start.si;
  let cur = [start.lat, start.lon];
  let travelled = 0;
  let lastBearing = heading;
  for (let guard = 0; guard < 5000; guard++) {
    const next = pts[idx];
    const d = metres(cur[0], cur[1], next[0], next[1]);
    if (d > 0) lastBearing = bearingDeg(cur[0], cur[1], next[0], next[1]);
    if (travelled + d >= distM) {
      const f = d > 0 ? (distM - travelled) / d : 0;
      return {
        lat: cur[0] + f * (next[0] - cur[0]),
        lon: cur[1] + f * (next[1] - cur[1]),
        name: ways[wi].name, corner: false, end: false,
      };
    }
    travelled += d;
    cur = next;
    const node = nodeAt(next[0], next[1]);
    if (node && node.names.size >= 2 && travelled > 3) {
      return { lat: next[0], lon: next[1], name: ways[wi].name, corner: true, end: false };
    }
    idx += dir;
    if (idx < 0 || idx >= pts.length) {
      // The end of this way. Carry on along another piece of the same street, if one joins here.
      const name = ways[wi].name;
      const prevWay = wi;
      let onward = null;
      for (const [wj, vj] of node ? node.refs : []) {
        if (wj === prevWay || ways[wj].name !== name) continue;
        const other = ways[wj].pts;
        for (const dj of [1, -1]) {
          const nj = vj + dj;
          if (nj < 0 || nj >= other.length) continue;
          const diff = angleDiff(bearingDeg(next[0], next[1], other[nj][0], other[nj][1]), lastBearing);
          if (diff <= 100 && (!onward || diff < onward.diff)) onward = { wj, nj, dj, diff };
        }
      }
      if (onward) {
        wi = onward.wj;
        pts = ways[wi].pts;
        dir = onward.dj;
        idx = onward.nj;
        continue;
      }
      // Map tiles cut streets at their edges, so the same street may carry
      // on a few metres away in the next tile. Pick it up there.
      let near = null;
      for (const [wj, sj] of segmentsNear(cur[0], cur[1], 10)) {
        if (wj === prevWay || ways[wj].name !== name) continue;
        const p = project(wj, sj, cur[0], cur[1]);
        if (p.dist > 10) continue;
        const [a, b] = [ways[wj].pts[sj], ways[wj].pts[sj + 1]];
        for (const dj of [1, -1]) {
          const remaining = dj > 0 ? (1 - p.t) * p.len : p.t * p.len;
          if (remaining < 1) continue;
          const bearing = dj > 0 ? bearingDeg(a[0], a[1], b[0], b[1]) : bearingDeg(b[0], b[1], a[0], a[1]);
          const diff = angleDiff(bearing, lastBearing);
          if (diff <= 60 && (!near || diff + p.dist < near.score)) near = { score: diff + p.dist, wj, sj, dj, lat: p.lat, lon: p.lon };
        }
      }
      if (!near) {
        return { lat: cur[0], lon: cur[1], name, corner: false, end: true };
      }
      wi = near.wj;
      pts = ways[wi].pts;
      dir = near.dj;
      idx = near.dj > 0 ? near.sj + 1 : near.sj;
      cur = [near.lat, near.lon];
    }
  }
  return { lat: cur[0], lon: cur[1], name: ways[wi].name, corner: false, end: false };
}

// ── Places ────────────────────────────────────────────────────────────────

/** Places within radiusM metres, nearest first: [{ name, kind, lat, lon, dist, bearing }]. */
export function placesNear(lat, lon, radiusM = 500, limit = 60) {
  const out = [];
  for (const key of cellsNear(lat, lon, radiusM)) {
    for (const p of poiGrid.get(key) || []) {
      const dist = metres(lat, lon, p.lat, p.lon);
      if (dist <= radiusM) out.push({ ...p, dist, bearing: bearingDeg(lat, lon, p.lat, p.lon) });
    }
  }
  out.sort((a, b) => a.dist - b.dist);
  return out.slice(0, limit);
}

export function placeText(p) {
  const here = p.dist < 10 ? 'here' : `${roughly(p.dist)} ${compassName(p.bearing)}`;
  return `${p.name}, ${p.kind}, ${here}`;
}

// ── Visual Assist ─────────────────────────────────────────────────────────

const MAJOR = new Set(['motorway', 'trunk', 'primary', 'secondary', 'motorway_link', 'trunk_link', 'primary_link']);
const PATHS = new Set(['footway', 'path', 'cycleway', 'pedestrian', 'steps', 'track', 'bridleway']);

/** Draw the streets around a point onto a canvas context. */
export function drawStreets(g, w, h, lat, lon, zoom, colours) {
  const spanM = 500 / Math.max(0.25, zoom);
  const scale = Math.min(w, h) / spanM; // pixels per metre
  const k = M * Math.cos(rad(lat));
  const X = (lo) => w / 2 + (lo - lon) * k * scale;
  const Y = (la) => h / 2 - (la - lat) * M * scale;
  const radius = Math.hypot(w, h) / 2 / scale;

  g.fillStyle = colours.ground;
  g.fillRect(0, 0, w, h);

  const visible = new Set();
  for (const key of cellsNear(lat, lon, radius)) {
    for (const [wi] of segGrid.get(key) || []) visible.add(wi);
  }
  g.lineCap = 'round';
  g.lineJoin = 'round';
  for (const pass of [0, 1]) {
    for (const wi of visible) {
      const way = ways[wi];
      const major = MAJOR.has(way.kind);
      const path = PATHS.has(way.kind);
      const width = major ? 12 : path ? 3 : 8;
      g.strokeStyle = pass === 0 ? colours.casing : (path ? colours.path : colours.road);
      g.lineWidth = pass === 0 ? width + 3 : width;
      if (path && pass === 0) continue;
      g.beginPath();
      way.pts.forEach(([la, lo], i) => (i ? g.lineTo(X(lo), Y(la)) : g.moveTo(X(lo), Y(la))));
      g.stroke();
    }
  }

  g.fillStyle = colours.place;
  for (const p of placesNear(lat, lon, radius, 200)) {
    g.fillRect(X(p.lon) - 4, Y(p.lat) - 4, 8, 8);
  }

  // One name per street, at the vertex nearest the middle of the view.
  g.font = '600 14px system-ui, sans-serif';
  g.textAlign = 'center';
  g.textBaseline = 'middle';
  g.lineWidth = 4;
  const named = new Map();
  for (const wi of visible) {
    const way = ways[wi];
    for (const [la, lo] of way.pts) {
      const x = X(lo);
      const y = Y(la);
      if (x < 40 || x > w - 40 || y < 16 || y > h - 16) continue;
      const d = Math.hypot(x - w / 2, y - h / 2);
      const prev = named.get(way.name);
      if (!prev || d < prev.d) named.set(way.name, { x, y, d });
    }
  }
  for (const [name, { x, y }] of named) {
    g.strokeStyle = colours.halo;
    g.strokeText(name, x, y - 12);
    g.fillStyle = colours.label;
    g.fillText(name, x, y - 12);
  }
}

// ── Picking turns at a corner ─────────────────────────────────────────────

/**
 * The ways out of the corner at exactly (lat, lon), as reached by followStep:
 * [{ name, bearing, wi, vi, dir }], sorted by compass bearing.
 */
export function cornerOptions(lat, lon) {
  const node = nodeAt(lat, lon);
  if (!node) return [];
  const out = [];
  for (const [wi, vi] of node.refs) {
    const pts = ways[wi].pts;
    for (const dir of [1, -1]) {
      const j = vi + dir;
      if (j < 0 || j >= pts.length) continue;
      const bearing = bearingDeg(lat, lon, pts[j][0], pts[j][1]);
      const name = ways[wi].name;
      if (out.some((o) => o.name === name && angleDiff(o.bearing, bearing) < 20)) continue;
      out.push({ name, bearing, wi, vi, dir });
    }
  }
  return out.sort((a, b) => a.bearing - b.bearing);
}

/** Set off along one of cornerOptions, for up to distM metres (stopping at the next corner). */
export function takeBranch(opt, distM) {
  const [lat, lon] = ways[opt.wi].pts[opt.vi];
  const start = opt.dir > 0
    ? { wi: opt.wi, si: opt.vi, t: 0, dir: 1, lat, lon }
    : { wi: opt.wi, si: opt.vi - 1, t: 1, dir: -1, lat, lon };
  return walkAlong(start, distM, opt.bearing);
}

/** angleDiff for main.js: degrees between two bearings, 0 to 180. */
export function bearingGap(a, b) {
  return angleDiff(a, b);
}
