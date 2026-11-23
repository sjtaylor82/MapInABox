// Street data for one map tile straight from OpenStreetMap, through Overpass:
// named streets with their real junctions, places with their phone numbers,
// opening hours and websites, and house numbers with their street names.
// Shared by the Worker (which asks the servers; the French one answers only
// clients that identify themselves) and the page. Tiles are the same zoom-14
// squares as the OpenFreeMap fallback (vectortile.js).

import { tileBounds } from './vectortile.js';
import { detailsFromTags } from './placedetails.js';

export const AREA_SERVERS = [
  'https://overpass.openstreetmap.fr/api/interpreter',
  'https://overpass-api.de/api/interpreter',
];

export function areaQuery(ty, tx) {
  const [s, w, n, e] = tileBounds(ty, tx).map((v) => v.toFixed(6));
  return `[out:json][timeout:40][bbox:${s},${w},${n},${e}];
way["highway"]["name"]["highway"!~"^(proposed|construction|abandoned|razed|platform|elevator|corridor|busway|bus_guideway|raceway|escape)$"];
out geom qt;
(
  nwr["name"]["amenity"];
  nwr["name"]["shop"];
  nwr["name"]["tourism"];
  nwr["name"]["leisure"];
  nwr["name"]["healthcare"];
  node["name"]["highway"="bus_stop"];
  node["name"]["railway"~"^(station|halt|tram_stop)$"];
  way["name"]["railway"="station"];
);
out tags center qt;
nwr["addr:housenumber"][!"name"];
out tags center qt;`;
}

function words(value) {
  return String(value || '').replace(/[_;]/g, ' ').replace(/\s+/g, ' ').trim();
}

const KINDS = { fast_food: 'fast food', ice_cream: 'ice cream', fuel: 'fuel station' };

function placeKind(t) {
  if (t.highway === 'bus_stop') return ['bus stop', 'bus'];
  if (t.railway === 'tram_stop') return ['tram stop', 'railway'];
  if (t.railway) return ['railway station', 'railway'];
  for (const key of ['amenity', 'shop', 'tourism', 'leisure', 'healthcare']) {
    const v = t[key];
    if (!v) continue;
    const cls = key === 'shop' ? 'shop' : v;
    if (v === 'yes') return [key === 'shop' ? 'shop' : words(key), cls];
    return [KINDS[v] || words(v), cls];
  }
  return ['place', ''];
}

const r6 = (v) => Math.round(v * 1e6) / 1e6;

function isTunnel(t) {
  const layer = Number.parseInt(t.layer || '0', 10);
  return (t.tunnel && t.tunnel !== 'no' && t.tunnel !== 'building_passage') || layer < 0;
}

/** Overpass JSON to the street.js tile shape: { ways, pois, numbers }. */
export function areaData(json) {
  const ways = [];
  const pois = [];
  const numbers = [];
  for (const el of json.elements || []) {
    const t = el.tags || {};
    if (el.type === 'way' && t.highway && Array.isArray(el.geometry)) {
      if (!t.name || isTunnel(t)) continue;
      const c = [];
      for (const p of el.geometry) if (p && Number.isFinite(p.lat)) c.push(r6(p.lat), r6(p.lon));
      if (c.length >= 4) ways.push({ id: `w${el.id}`, n: t.name, k: t.highway, c });
      continue;
    }
    const la = el.lat ?? el.center?.lat;
    const lo = el.lon ?? el.center?.lon;
    if (!Number.isFinite(la) || !Number.isFinite(lo)) continue;
    if (t.name) {
      const [k, c] = placeKind(t);
      pois.push({ id: `${el.type}${el.id}`, n: t.name, k, c, la: r6(la), lo: r6(lo), d: detailsFromTags(t) });
    } else if (t['addr:housenumber']) {
      numbers.push({ id: `${el.type}${el.id}`, n: t['addr:housenumber'], s: t['addr:street'] || '', la: r6(la), lo: r6(lo) });
    }
  }
  return { ways, pois, numbers, source: 'overpass' };
}
