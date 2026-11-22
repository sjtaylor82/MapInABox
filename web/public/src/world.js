// World data and the spoken description of a map position.
// describe() is a port of the map-mode branch of core.py _lookup().

import { settings } from './settings.js';
import { bearingDeg, compassName, formatDistance, inPolygons } from './geo.js';

export const data = {
  countries: [],
  cities: null,
  tables: null,
  facts: {},
  languages: {},
  currency: {},
  sounds: { countries: [], regions: [] },
  featureIndex: null,
};

const json = (path) => fetch(path).then((r) => {
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
  return r.json();
});

export async function loadWorld(progress = () => {}) {
  progress('Loading tables');
  const [tables, facts, languages, currency, sounds, featureIndex] = await Promise.all([
    json('data/tables.json'), json('data/facts.json'), json('data/languages.json'),
    json('data/currency.json').catch(() => ({})), json('data/sounds.json'),
    json('data/features/index.json'),
  ]);
  Object.assign(data, { tables, facts, languages, currency, sounds, featureIndex });
  progress('Loading countries');
  const [countries, cities] = await Promise.all([json('data/countries.json'), json('data/cities.json')]);
  data.countries = countries;
  progress('Indexing cities');
  data.cities = indexCities(cities);
}

// ── Cities: 1-degree grid for nearest-city search ─────────────────────────

function indexCities(c) {
  const n = c.n.length;
  const lat = new Float64Array(n);
  const lon = new Float64Array(n);
  const grid = new Map();
  for (let i = 0; i < n; i++) {
    lat[i] = c.lat[i] / 1e4;
    lon[i] = c.lon[i] / 1e4;
    const key = cellKey(Math.floor(lat[i]), Math.floor(lon[i]));
    let cell = grid.get(key);
    if (!cell) grid.set(key, (cell = []));
    cell.push(i);
  }
  return { ...c, lat, lon, grid, count: n };
}

const cellKey = (y, x) => (y + 90) * 400 + (((x % 360) + 540) % 360);

export function city(i) {
  const c = data.cities;
  return {
    name: c.n[i], admin: c.admins[c.a[i]], country: c.countries[c.c[i]],
    lat: c.lat[i], lon: c.lon[i], pop: c.pop[i],
  };
}

/** Nearest city by plain degree distance, as core._nearest_city. Returns { dist, idx }. */
export function nearestCity(lat, lon) {
  const c = data.cities;
  let best = Infinity;
  let bestIdx = 0;
  const cy = Math.floor(lat);
  const cx = Math.floor(lon);
  for (let ring = 0; ring <= 180; ring++) {
    // Once the ring is further than the best match, nothing closer remains.
    if (best < Infinity && (ring - 1) ** 2 > best) break;
    for (let dy = -ring; dy <= ring; dy++) {
      const y = cy + dy;
      if (y < -90 || y > 89) continue;
      for (let dx = -ring; dx <= ring; dx++) {
        if (Math.abs(dy) !== ring && Math.abs(dx) !== ring) continue;
        const cell = c.grid.get(cellKey(y, cx + dx));
        if (!cell) continue;
        for (const i of cell) {
          const dLat = c.lat[i] - lat;
          const dLon = c.lon[i] - lon; // desktop does not wrap the date line either
          const d = dLat * dLat + dLon * dLon;
          if (d < best) { best = d; bestIdx = i; }
        }
      }
    }
  }
  return { dist: Math.sqrt(best), idx: bestIdx };
}

// ── Countries ─────────────────────────────────────────────────────────────

export function countryAt(lat, lon) {
  for (const c of data.countries) {
    const [minx, miny, maxx, maxy] = c.bbox;
    if (lon < minx || lon > maxx || lat < miny || lat > maxy) continue;
    if (inPolygons(lon, lat, c.polys)) return c;
  }
  return null;
}

export function countryByName(name) {
  const alias = data.tables.countryAliases[name] || name;
  return data.countries.find((c) => c.name === name)
    || data.countries.find((c) => c.name === alias) || null;
}

export function canonical(name) {
  return data.tables.countryAliases[name] || name;
}

export function factsFor(country) {
  const want = canonical(country).toLowerCase();
  return Object.values(data.facts).find((f) => (f.name || '').toLowerCase() === want) || null;
}

export function continentFor(country) {
  if (!country || country === 'Open Water') return '';
  if (country === 'Antarctica') return 'Antarctica';
  const override = data.tables.continentOverrides[country];
  if (override) return override;
  const f = factsFor(country);
  if (f && f.continent) return f.continent;
  const poly = countryByName(country);
  return poly ? poly.continent : '';
}

export function oceanName(lat, lon) {
  if (lat >= -43.6 && lat <= -40.0) return lon >= 146.8 ? 'Tasman Sea' : 'Southern Ocean (Australia)';
  for (const [name, boxes] of data.tables.oceans) {
    for (const [latMin, latMax, lonMin, lonMax] of boxes) {
      if (lat < latMin || lat > latMax) continue;
      if ((lonMin < lonMax && lon >= lonMin && lon <= lonMax)
        || (lonMin > lonMax && (lon >= lonMin || lon <= lonMax))) return name;
    }
  }
  return 'Open Water';
}

// ── GeoFeatures (geo_features.py) ─────────────────────────────────────────

const tiles = new Map(); // key -> Promise<array>

function tileKeys(lat, lon, rLat, rLon) {
  const size = data.featureIndex.tile;
  const keys = [];
  const x0 = Math.floor((Math.max(-180, lon - rLon) + 180) / size);
  const x1 = Math.floor((Math.min(179.999, lon + rLon) + 180) / size);
  const y0 = Math.floor((Math.max(-90, lat - rLat) + 90) / size);
  const y1 = Math.floor((Math.min(89.999, lat + rLat) + 90) / size);
  for (let x = x0; x <= x1; x++) for (let y = y0; y <= y1; y++) keys.push(`${x}_${y}`);
  return keys;
}

const tileSet = () => (data._tileSet ||= new Set(data.featureIndex.tiles));

async function featuresNear(lat, lon, radius, iso2) {
  const keys = tileKeys(lat, lon, radius, radius * 1.5).filter((k) => tileSet().has(k));
  const lists = await Promise.all(keys.map((k) => {
    if (!tiles.has(k)) tiles.set(k, json(`data/features/${k}.json`).catch(() => []));
    return tiles.get(k);
  }));
  const out = [];
  for (const list of lists) {
    for (const [name, flat, flon, code, cc] of list) {
      if (iso2 && cc && cc !== iso2) continue;
      if (Math.abs(flat - lat) > radius || Math.abs(flon - lon) > radius * 1.5) continue;
      out.push({ name, lat: flat, lon: flon, code });
    }
  }
  return out;
}

const RADII_BROAD = {
  'H.GULF': 0.35, 'H.BAY': 0.2, 'H.RF': 0.35, 'H.RFS': 0.25, 'H.STRT': 0.2, 'H.CHAN': 0.2,
  'H.CHN': 0.2, 'H.CHNL': 0.2, 'H.LGN': 0.18, 'H.RFC': 0.25, 'H.SD': 0.22, 'H.SHOL': 0.08,
  'H.SPIT': 0.06, 'T.DES': 0.75, 'T.DSRT': 0.75, 'T.DUNE': 0.08, 'T.ERG': 0.35, 'T.GAP': 0.05,
  'T.GRGE': 0.06, 'T.HDLD': 0.08, 'T.MTS': 0.35, 'T.HMDA': 0.25, 'T.ISTH': 0.06, 'T.KRST': 0.20,
  'T.REG': 0.35, 'T.RGN': 0.35, 'T.PEN': 0.25, 'T.CAPE': 0.12, 'T.ISL': 0.06, 'T.ISLET': 0.04,
  'T.ISLF': 0.06, 'T.ISLM': 0.06, 'T.ISLS': 0.05, 'T.ISLT': 0.04, 'T.SAND': 0.08, 'L.LCTY': 0.45,
  'S.FRM': 0.25, 'S.FRMS': 0.30, 'S.HMSD': 0.25, 'S.RNCH': 0.35, 'S.RNCHS': 0.45,
};
const RADII_PRECISE = {
  'T.MTS': 0.04, 'T.DES': 0.15, 'T.DSRT': 0.15, 'H.RF': 0.06, 'H.RFS': 0.06, 'H.GULF': 0.06,
  'H.BAY': 0.03, 'H.STRT': 0.04, 'H.CHAN': 0.04, 'H.CHN': 0.04, 'H.CHNL': 0.04, 'H.LGN': 0.04,
  'H.RFC': 0.06, 'H.SD': 0.05, 'H.SHOL': 0.03, 'H.SPIT': 0.025, 'T.DUNE': 0.03, 'T.ERG': 0.08,
  'T.GAP': 0.02, 'T.GRGE': 0.025, 'T.HDLD': 0.03, 'T.HMDA': 0.06, 'T.ISTH': 0.025, 'T.KRST': 0.05,
  'T.CAPE': 0.03, 'T.PEN': 0.04, 'T.ISL': 0.025, 'T.ISLET': 0.02, 'T.ISLF': 0.025, 'T.ISLM': 0.025,
  'T.ISLS': 0.03, 'T.ISLT': 0.02, 'T.SAND': 0.03, 'L.LCTY': 0.04, 'S.FRM': 0.04, 'S.FRMS': 0.05,
  'S.HMSD': 0.04, 'S.RNCH': 0.05, 'S.RNCHS': 0.06,
};
const RADII_CONTEXT = {
  'H.BAY': 0.70, 'H.BAYS': 0.70, 'H.GULF': 0.80, 'H.LGN': 0.25, 'H.STRT': 0.35, 'H.CHAN': 0.35,
  'H.CHN': 0.35, 'H.CHNL': 0.35, 'H.RF': 0.45, 'H.RFS': 0.45, 'H.RFC': 0.45, 'T.ISL': 0.50,
  'T.ISLF': 0.50, 'T.ISLM': 0.50, 'T.ISLS': 0.50, 'T.ISLET': 0.25, 'T.ISLT': 0.25, 'T.PEN': 0.35,
  'T.CAPE': 0.20, 'L.LCTY': 0.45, 'S.FRM': 0.25, 'S.FRMS': 0.30, 'S.HMSD': 0.25, 'S.RNCH': 0.35,
  'S.RNCHS': 0.45,
};
const maxRadius = (radii) => Math.max(...Object.values(radii));

function nameWithType(name, code) {
  const t = { 'S.FRM': 'Farm', 'S.FRMS': 'Farms', 'S.HMSD': 'Homestead' }[code];
  return t ? `${name} ${t}` : name;
}

function withDistance(lat, lon, f) {
  const km = Math.hypot(f.lat - lat, f.lon - lon) * 111;
  return `${nameWithType(f.name, f.code)} ${formatDistance(km * 1000)} ${compassName(bearingDeg(lat, lon, f.lat, f.lon))}`;
}

async function nearestByRadii(lat, lon, radii, iso2) {
  let best = null;
  let bestDist = Infinity;
  for (const f of await featuresNear(lat, lon, maxRadius(radii), iso2)) {
    const r = radii[f.code] || 0;
    if (!r) continue;
    const dLat = Math.abs(f.lat - lat);
    const dLon = Math.abs(f.lon - lon);
    if (dLat > r || dLon > r * 1.5) continue;
    const d = Math.hypot(dLat, dLon);
    if (d < r && d < bestDist) { bestDist = d; best = f; }
  }
  return best;
}

export async function lookupPrecise(lat, lon, iso2) {
  if (!settings.get('geoFeatures')) return '';
  const f = await nearestByRadii(lat, lon, RADII_PRECISE, iso2);
  return f ? nameWithType(f.name, f.code) : '';
}

export async function lookupAny(lat, lon, iso2) {
  if (!settings.get('geoFeatures')) return '';
  const f = await nearestByRadii(lat, lon, RADII_BROAD, iso2);
  return f ? withDistance(lat, lon, f) : '';
}

export async function contextItems(lat, lon, limit = 3, iso2, radii = RADII_CONTEXT) {
  if (!settings.get('geoFeatures')) return [];
  const seen = new Set();
  const found = [];
  for (const f of await featuresNear(lat, lon, maxRadius(radii), iso2)) {
    const r = radii[f.code] || 0;
    if (!r) continue;
    const dLat = Math.abs(f.lat - lat);
    const dLon = Math.abs(f.lon - lon);
    if (dLat > r || dLon > r * 1.5) continue;
    const d = Math.hypot(dLat, dLon);
    if (d < r && !seen.has(f.name)) { seen.add(f.name); found.push([d, f]); }
  }
  found.sort((a, b) => a[0] - b[0]);
  return found.slice(0, limit).map(([, f]) => withDistance(lat, lon, f));
}

/** G key: nearby geographic features using the broad radii. */
export function nearbyFeatures(lat, lon, iso2) {
  return contextItems(lat, lon, 5, iso2, RADII_BROAD);
}

export function nearestCityLabel(lat, lon) {
  const { dist, idx } = nearestCity(lat, lon);
  const c = city(idx);
  const dir = compassName(bearingDeg(lat, lon, c.lat, c.lon)).replace('-', ' ');
  return `${c.name || 'City unknown'} ${formatDistance(dist * 111 * 1000)} ${dir}`;
}

// ── The spoken description ────────────────────────────────────────────────

/**
 * state carries what was last announced ({ country, admin }) so the state
 * and country are only repeated when they change, as on the desktop.
 * Returns { label, country, iso2, continent, city, admin, onLand }.
 */
export async function describe(lat, lon, state) {
  const { placeNameCloseKm, nearestPlaceFallbackKm } = data.tables;
  const { dist, idx } = nearestCity(lat, lon);
  const distKm = dist * 111;
  const poly = countryAt(lat, lon);
  const nearest = city(idx);
  const onLand = !!poly || distKm <= placeNameCloseKm;
  const iso2 = poly ? poly.iso2 : '';
  const result = { country: 'Open Water', iso2, city: '', admin: '', onLand, label: '' };

  if (onLand) {
    const country = poly ? poly.name : nearest.country;
    const matches = !poly || canonical(nearest.country).toLowerCase() === canonical(country).toLowerCase();
    const closePlace = matches && distKm <= placeNameCloseKm;
    result.country = country;
    result.city = closePlace ? nearest.name : '';
    result.admin = matches ? nearest.admin : '';

    if (matches) {
      if (closePlace) {
        const parts = [nearest.name];
        if (nearest.admin && nearest.admin !== state.admin) parts.push(nearest.admin);
        if (country !== state.country) parts.push(country);
        result.label = parts.join(', ');
      } else {
        const feature = await lookupPrecise(lat, lon, iso2);
        const context = feature ? [] : await contextItems(lat, lon, 1, iso2);
        if (feature) {
          // A homestead must not displace a recognised nearby town.
          result.label = /( Homestead| Farm| Farms)$/.test(feature) && distKm <= nearestPlaceFallbackKm
            ? nearestCityLabel(lat, lon) : feature;
        } else if (context.length) {
          result.label = context.join('. ');
        } else if (!settings.get('geoFeatures')) {
          result.label = nearestCityLabel(lat, lon);
        } else if (nearest.name && distKm <= nearestPlaceFallbackKm) {
          result.label = `${nearest.name} ${formatDistance(distKm * 1000)}`;
        } else {
          result.label = [nearest.admin, country].filter(Boolean).join(', ') || 'Location unknown';
        }
      }
    } else {
      const feature = await lookupPrecise(lat, lon, iso2);
      const context = feature ? [] : await contextItems(lat, lon, 1, iso2);
      result.label = feature || context.join('. ') || country;
    }
  } else {
    if (lat >= -60 && distKm <= 75 && nearest.country) result.country = nearest.country;
    const context = await contextItems(lat, lon, 1);
    result.label = context[0] || (await lookupPrecise(lat, lon)) || (await lookupAny(lat, lon)) || oceanName(lat, lon);
  }
  result.continent = continentFor(result.country);
  return result;
}

// ── Local search for the jump dialog ──────────────────────────────────────

const fold = (s) => String(s || '').toLowerCase().normalize('NFKD').replace(/[̀-ͯ]/g, '').trim();

export function parseCoordinates(text) {
  const m = /^\s*(-?\d+(?:\.\d+)?)\s*[, ]\s*(-?\d+(?:\.\d+)?)\s*$/.exec(text);
  if (!m) return null;
  const lat = Number(m[1]);
  const lon = Number(m[2]);
  if (Math.abs(lat) > 90 || Math.abs(lon) > 180) return null;
  return { lat, lon };
}

/** Countries, then cities (biggest first). "Cleveland, Queensland" narrows by state or country. */
export function searchLocal(query, limit = 15) {
  const parts = query.split(',').map(fold).filter(Boolean);
  if (!parts.length) return [];
  const [q, ...rest] = parts;
  const results = [];
  for (const c of data.countries) {
    const name = fold(c.name);
    if (rest.length === 0 && (name === q || name.startsWith(q))) {
      results.push({ label: `${c.name}, country`, country: c.name, lat: c.label[0], lon: c.label[1], rank: name === q ? 0 : 2, pop: Infinity });
    }
  }
  const c = data.cities;
  for (let i = 0; i < c.count; i++) {
    const name = fold(c.n[i]);
    if (name !== q && !(q.length >= 3 && name.startsWith(q))) continue;
    if (rest.length) {
      const admin = fold(c.admins[c.a[i]]);
      const country = fold(c.countries[c.c[i]]);
      if (!rest.every((r) => admin.startsWith(r) || country.startsWith(r) || abbreviation(admin) === r)) continue;
    }
    results.push({
      label: [c.n[i], c.admins[c.a[i]], c.countries[c.c[i]]].filter(Boolean).join(', '),
      lat: c.lat[i], lon: c.lon[i], rank: name === q ? 1 : 3, pop: c.pop[i],
    });
  }
  results.sort((a, b) => a.rank - b.rank || b.pop - a.pop);
  const seen = new Set();
  return results.filter((r) => (seen.has(r.label) ? false : seen.add(r.label))).slice(0, limit);
}

// "qld" for Queensland, "nsw" for New South Wales, "wa" for Western Australia...
const STATE_ABBR = {
  queensland: 'qld', 'new south wales': 'nsw', victoria: 'vic', tasmania: 'tas',
  'south australia': 'sa', 'western australia': 'wa', 'northern territory': 'nt',
  'australian capital territory': 'act',
};
function abbreviation(admin) {
  return STATE_ABBR[admin] || admin.split(/\s+/).map((w) => w[0]).join('');
}
