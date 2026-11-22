// Geometry and wording helpers, ported from geo.py and distance_units.py.

import { settings } from './settings.js';

const R = 6371000;
const rad = (d) => (d * Math.PI) / 180;

export function haversineM(lat1, lon1, lat2, lon2) {
  const dLat = rad(lat2 - lat1);
  const dLon = rad(lon2 - lon1);
  const a = Math.sin(dLat / 2) ** 2 + Math.cos(rad(lat1)) * Math.cos(rad(lat2)) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.min(1, Math.sqrt(a)));
}

/** Forward bearing, 0 = north, clockwise. Equirectangular, as in geo.py. */
export function bearingDeg(lat1, lon1, lat2, lon2) {
  const meanLat = rad((lat1 + lat2) / 2);
  const dLat = lat2 - lat1;
  const dLon = (lon2 - lon1) * Math.cos(meanLat);
  return ((Math.atan2(dLon, dLat) * 180) / Math.PI + 360) % 360;
}

const DIRS = ['north', 'north-east', 'east', 'south-east', 'south', 'south-west', 'west', 'north-west'];
export function compassName(bearing) {
  return DIRS[Math.floor((((bearing % 360) + 360) % 360 + 22.5) / 45) % 8];
}

export function formatDistance(metres) {
  metres = Math.max(0, Number(metres) || 0);
  if (settings.get('units') === 'imperial') {
    const miles = metres / 1609.344;
    if (miles < 0.1) {
      const raw = metres * 3.280839895;
      const feet = raw >= 100 ? Math.round(raw / 10) * 10 : Math.round(raw);
      return `${feet} ${feet === 1 ? 'foot' : 'feet'}`;
    }
    const text = miles < 10 ? miles.toFixed(1) : String(Math.round(miles));
    return `${text} ${Number(text) === 1 ? 'mile' : 'miles'}`;
  }
  if (metres < 1000) {
    const v = Math.round(metres);
    return `${v} ${v === 1 ? 'metre' : 'metres'}`;
  }
  const km = metres / 1000;
  const text = km < 10 ? km.toFixed(1) : String(Math.round(km));
  return `${text} ${Number(text) === 1 ? 'kilometre' : 'kilometres'}`;
}

export function wrapLon(lon) {
  return ((((lon + 180) % 360) + 360) % 360) - 180;
}

export function clampLat(lat) {
  return Math.max(-90, Math.min(90, lat));
}

/** Move a point by km along a compass direction (0 north, 90 east...). */
export function offsetKm(lat, lon, km, bearing) {
  const dLat = (km * Math.cos(rad(bearing))) / 111.0;
  const cos = Math.max(0.01, Math.cos(rad(lat)));
  const dLon = (km * Math.sin(rad(bearing))) / (111.0 * cos);
  return { lat: clampLat(lat + dLat), lon: wrapLon(lon + dLon) };
}

export function formatLat(lat) {
  return `${Math.abs(lat).toFixed(2)} degrees ${lat >= 0 ? 'north' : 'south'}`;
}

export function formatLon(lon) {
  return `${Math.abs(lon).toFixed(2)} degrees ${lon >= 0 ? 'east' : 'west'}`;
}

/** Ray-casting point-in-polygon for one ring of [lon, lat] pairs. */
function inRing(lon, lat, ring) {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [xi, yi] = ring[i];
    const [xj, yj] = ring[j];
    if ((yi > lat) !== (yj > lat) && lon < ((xj - xi) * (lat - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

/** polys: array of polygons, each [outer, ...holes]. */
export function inPolygons(lon, lat, polys) {
  for (const poly of polys) {
    if (!poly.length || !inRing(lon, lat, poly[0])) continue;
    let hole = false;
    for (let h = 1; h < poly.length; h++) if (inRing(lon, lat, poly[h])) { hole = true; break; }
    if (!hole) return true;
  }
  return false;
}
