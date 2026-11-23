// Street data from OpenStreetMap, shared by the Worker and the page.
//
// The world is cut into square tiles of TILE degrees. Each tile is fetched
// from Overpass once, made compact, and cached (by the Worker for everyone,
// by the page for the visit). No browser or Worker APIs are used here, so
// both can import it.

export const TILE = 0.02; // degrees: about 2.2 km north to south

export const OVERPASS_MIRRORS = [
  'https://overpass-api.de/api/interpreter',
  'https://z.overpass-api.de/api/interpreter',
  'https://overpass.kumi.systems/api/interpreter',
];

export function tileOf(lat, lon) {
  return { ty: Math.floor(lat / TILE), tx: Math.floor(lon / TILE) };
}

/** [south, west, north, east] */
export function tileBounds(ty, tx) {
  const s = ty * TILE;
  const w = tx * TILE;
  return [round(s, 5), round(w, 5), round(s + TILE, 5), round(w + TILE, 5)];
}

export function validTile(ty, tx) {
  return Number.isInteger(ty) && Number.isInteger(tx)
    && ty >= Math.floor(-85 / TILE) && ty < Math.ceil(85 / TILE)
    && tx >= Math.floor(-180 / TILE) && tx < Math.ceil(180 / TILE);
}

/** Named streets and paths, then named places, for one tile. */
export function buildQuery(ty, tx) {
  const [s, w, n, e] = tileBounds(ty, tx);
  return `[out:json][timeout:40][bbox:${s},${w},${n},${e}];
way["highway"]["name"]["highway"!~"^(proposed|construction|abandoned|razed|platform|elevator|corridor|busway|bus_guideway|raceway|escape)$"];
out geom qt;
(
  node["name"]["amenity"];
  node["name"]["shop"];
  node["name"]["tourism"];
  node["name"]["leisure"];
  node["name"]["healthcare"];
  node["name"]["highway"="bus_stop"];
  node["name"]["railway"~"^(station|halt|tram_stop)$"];
  way["name"]["amenity"];
  way["name"]["shop"];
  way["name"]["tourism"];
  way["name"]["leisure"];
  way["name"]["railway"="station"];
);
out center qt;`;
}

const KIND_KEYS = ['amenity', 'shop', 'tourism', 'leisure', 'healthcare'];

function words(value) {
  return String(value).replace(/[_;]/g, ' ').replace(/\s+/g, ' ').trim();
}

/** A short spoken kind, such as "cafe", "supermarket" or "bus stop". */
export function placeKind(tags) {
  if (tags.highway === 'bus_stop') return 'bus stop';
  if (tags.railway === 'station' || tags.railway === 'halt') return 'railway station';
  if (tags.railway === 'tram_stop') return 'tram stop';
  for (const key of KIND_KEYS) {
    const v = tags[key];
    if (!v) continue;
    if (v === 'yes') return key === 'shop' ? 'shop' : words(key);
    return words(v);
  }
  return 'place';
}

/**
 * Overpass JSON to { ways: [{ id, n, k, c: [lat, lon, lat, lon, ...] }],
 *                    pois: [{ id, n, k, la, lo }] }.
 */
export function compact(json) {
  const ways = [];
  const pois = [];
  const seenPoi = new Set();
  for (const el of json.elements || []) {
    const tags = el.tags || {};
    const name = String(tags.name || '').trim();
    if (!name) continue;
    if (el.type === 'way' && tags.highway && Array.isArray(el.geometry)) {
      const c = [];
      for (const p of el.geometry) {
        if (p && Number.isFinite(p.lat) && Number.isFinite(p.lon)) c.push(round(p.lat, 6), round(p.lon, 6));
      }
      if (c.length >= 4) ways.push({ id: el.id, n: name, k: tags.highway, c });
      continue;
    }
    const la = el.type === 'node' ? el.lat : el.center?.lat;
    const lo = el.type === 'node' ? el.lon : el.center?.lon;
    if (!Number.isFinite(la) || !Number.isFinite(lo)) continue;
    const key = `${el.type}${el.id}`;
    if (seenPoi.has(key)) continue;
    seenPoi.add(key);
    pois.push({ id: key, n: name, k: placeKind(tags), la: round(la, 6), lo: round(lo, 6) });
  }
  return { ways, pois };
}

function round(v, places) {
  const f = 10 ** places;
  return Math.round(v * f) / f;
}
