// Street data from OpenFreeMap vector tiles (OpenMapTiles schema, from
// OpenStreetMap). A small Mapbox Vector Tile decoder, with no dependencies,
// shared by the page and the Worker.
//
// Tiles are the standard web-map squares at zoom 14 (about 2.4 km across at
// the equator, less towards the poles). Streets come from the
// transportation_name layer and places from the poi layer.

export const ZOOM = 14;
export const TILEJSON = 'https://tiles.openfreemap.org/planet';
export const ATTRIBUTION = '© OpenMapTiles © OpenStreetMap contributors';

const N = 2 ** ZOOM;

export function tileOf(lat, lon) {
  const x = Math.floor(((lon + 180) / 360) * N);
  const r = (lat * Math.PI) / 180;
  const y = Math.floor(((1 - Math.asinh(Math.tan(r)) / Math.PI) / 2) * N);
  return { tx: Math.max(0, Math.min(N - 1, x)), ty: Math.max(0, Math.min(N - 1, y)) };
}

const lonOf = (x) => (x / N) * 360 - 180;
const latOf = (y) => (Math.atan(Math.sinh(Math.PI * (1 - (2 * y) / N))) * 180) / Math.PI;

/** [south, west, north, east] of a tile. */
export function tileBounds(ty, tx) {
  return [latOf(ty + 1), lonOf(tx), latOf(ty), lonOf(tx + 1)];
}

export function validTile(ty, tx) {
  return Number.isInteger(ty) && Number.isInteger(tx) && ty >= 0 && ty < N && tx >= 0 && tx < N;
}

// ── Protocol buffers ──────────────────────────────────────────────────────

class Reader {
  constructor(buf, pos = 0, end = buf.length) {
    this.buf = buf;
    this.pos = pos;
    this.end = end;
  }

  varint() {
    let result = 0;
    let shift = 0;
    let b;
    do {
      b = this.buf[this.pos++];
      result += (b & 0x7f) * 2 ** shift;
      shift += 7;
    } while (b & 0x80);
    return result;
  }

  /** Each field as [number, wireType, value or sub-Reader]. */
  * fields() {
    while (this.pos < this.end) {
      const key = this.varint();
      const field = Math.floor(key / 8);
      const wire = key & 7;
      if (wire === 0) yield [field, wire, this.varint()];
      else if (wire === 2) {
        const len = this.varint();
        const sub = new Reader(this.buf, this.pos, this.pos + len);
        this.pos += len;
        yield [field, wire, sub];
      } else if (wire === 1) {
        const v = new DataView(this.buf.buffer, this.buf.byteOffset + this.pos, 8).getFloat64(0, true);
        this.pos += 8;
        yield [field, wire, v];
      } else if (wire === 5) {
        const v = new DataView(this.buf.buffer, this.buf.byteOffset + this.pos, 4).getFloat32(0, true);
        this.pos += 4;
        yield [field, wire, v];
      } else {
        throw new Error(`Unexpected protobuf wire type ${wire}`);
      }
    }
  }

  string() {
    return new TextDecoder().decode(this.buf.subarray(this.pos, this.end));
  }

  packed() {
    const out = [];
    while (this.pos < this.end) out.push(this.varint());
    return out;
  }
}

const zigzag = (n) => (n % 2 ? -(n + 1) / 2 : n / 2);

function readValue(r) {
  for (const [f, , v] of r.fields()) {
    if (f === 1) return v.string();
    if (f === 2 || f === 3) return v;
    if (f === 4 || f === 5) return v;
    if (f === 6) return zigzag(v);
    if (f === 7) return !!v;
  }
  return null;
}

/** { layerName: [{ type, tags, geometry: [[[x, y], ...], ...], extent }] } for the layers asked for. */
export function decode(bytes, wanted) {
  const tile = new Reader(bytes);
  const layers = {};
  for (const [f, , layerReader] of tile.fields()) {
    if (f !== 3) continue;
    let name = '';
    let extent = 4096;
    const keys = [];
    const values = [];
    const features = [];
    for (const [lf, , v] of layerReader.fields()) {
      if (lf === 1) name = v.string();
      else if (lf === 2) features.push(v);
      else if (lf === 3) keys.push(v.string());
      else if (lf === 4) values.push(readValue(v));
      else if (lf === 5) extent = v;
    }
    if (!wanted.includes(name)) continue;
    layers[name] = features.map((fr) => {
      let type = 0;
      let tagIds = [];
      let geom = [];
      for (const [ff, , v] of fr.fields()) {
        if (ff === 2) tagIds = v.packed();
        else if (ff === 3) type = v;
        else if (ff === 4) geom = v.packed();
      }
      const tags = {};
      for (let i = 0; i + 1 < tagIds.length; i += 2) tags[keys[tagIds[i]]] = values[tagIds[i + 1]];
      return { type, tags, geometry: geometry(geom), extent };
    });
  }
  return layers;
}

function geometry(cmds) {
  const lines = [];
  let line = null;
  let x = 0;
  let y = 0;
  let i = 0;
  while (i < cmds.length) {
    const c = cmds[i++];
    const id = c & 7;
    const count = c >> 3;
    if (id === 7) continue; // ClosePath
    for (let k = 0; k < count; k++) {
      x += zigzag(cmds[i++]);
      y += zigzag(cmds[i++]);
      if (id === 1) {
        line = [[x, y]];
        lines.push(line);
      } else if (line) {
        line.push([x, y]);
      }
    }
  }
  return lines;
}

// ── To Map in a Box street data ───────────────────────────────────────────

function words(value) {
  return String(value || '').replace(/[_;]/g, ' ').replace(/\s+/g, ' ').trim();
}

const KIND = {
  bus: 'bus stop', railway: 'railway station', ferry_terminal: 'ferry terminal',
  fast_food: 'fast food', ice_cream: 'ice cream', fuel: 'fuel station',
};

function placeKind(tags) {
  const sub = tags.subclass && tags.subclass !== 'yes' ? tags.subclass : '';
  if (tags.class === 'bus') return 'bus stop';
  if (tags.class === 'railway') return sub === 'tram_stop' ? 'tram stop' : 'railway station';
  return KIND[sub] || words(sub) || KIND[tags.class] || words(tags.class) || 'place';
}

// ── Rebuilding corners ────────────────────────────────────────────────────
//
// Tiles are simplified, so two streets that meet do not always share a
// point. Find where streets on the same level cross or meet, and give both
// lines the same point there, so corners can be found and followed.

const SNAP_PX = 6; // about 3 metres at zoom 14 in Australia

function segCross(a, b, c, d) {
  const r = [b[0] - a[0], b[1] - a[1]];
  const s = [d[0] - c[0], d[1] - c[1]];
  const den = r[0] * s[1] - r[1] * s[0];
  if (den === 0) return null;
  const t = ((c[0] - a[0]) * s[1] - (c[1] - a[1]) * s[0]) / den;
  const u = ((c[0] - a[0]) * r[1] - (c[1] - a[1]) * r[0]) / den;
  if (t < 0 || t > 1 || u < 0 || u > 1) return null;
  return { t, u, p: [Math.round(a[0] + t * r[0]), Math.round(a[1] + t * r[1])] };
}

function nearOnSeg(p, a, b) {
  const dx = b[0] - a[0];
  const dy = b[1] - a[1];
  const len2 = dx * dx + dy * dy;
  const t = len2 ? Math.max(0, Math.min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / len2)) : 0;
  const q = [a[0] + t * dx, a[1] + t * dy];
  return { t, d: Math.hypot(p[0] - q[0], p[1] - q[1]), p: [Math.round(q[0]), Math.round(q[1])] };
}

/** lines: [{ name, layer, pts: [[x, y], ...] }], changed in place. */
export function joinCorners(lines) {
  const CELL = 128;
  const grid = new Map();
  lines.forEach((ln, li) => {
    for (let si = 0; si + 1 < ln.pts.length; si++) {
      const [a, b] = [ln.pts[si], ln.pts[si + 1]];
      for (let gx = Math.floor((Math.min(a[0], b[0]) - SNAP_PX) / CELL); gx <= Math.floor((Math.max(a[0], b[0]) + SNAP_PX) / CELL); gx++) {
        for (let gy = Math.floor((Math.min(a[1], b[1]) - SNAP_PX) / CELL); gy <= Math.floor((Math.max(a[1], b[1]) + SNAP_PX) / CELL); gy++) {
          const k = `${gx},${gy}`;
          if (!grid.has(k)) grid.set(k, []);
          grid.get(k).push([li, si]);
        }
      }
    }
  });
  const inserts = lines.map(() => []); // per line: [segIndex, t, point]
  const moves = []; // [line, vertexIndex, point]
  const seen = new Set();
  for (const bucket of grid.values()) {
    for (let i = 0; i < bucket.length; i++) {
      for (let j = i + 1; j < bucket.length; j++) {
        const [la, sa] = bucket[i];
        const [lb, sb] = bucket[j];
        const A = lines[la];
        const B = lines[lb];
        if (A.name === B.name || A.layer !== B.layer) continue;
        const key = la < lb ? `${la}:${sa}:${lb}:${sb}` : `${lb}:${sb}:${la}:${sa}`;
        if (seen.has(key)) continue;
        seen.add(key);
        const a1 = A.pts[sa];
        const a2 = A.pts[sa + 1];
        const b1 = B.pts[sb];
        const b2 = B.pts[sb + 1];
        const x = segCross(a1, a2, b1, b2);
        if (x) {
          inserts[la].push([sa, x.t, x.p]);
          inserts[lb].push([sb, x.u, x.p]);
          continue;
        }
        // A street that ends just short of (or just past) another: a T junction.
        for (const [L, s, M, ms, p1, p2] of [[la, sa, lb, sb, b1, b2], [lb, sb, la, sa, a1, a2]]) {
          const pts = lines[L].pts;
          for (const vi of [s, s + 1]) {
            if (vi !== 0 && vi !== pts.length - 1) continue;
            const n = nearOnSeg(pts[vi], p1, p2);
            if (n.d > 0 && n.d <= SNAP_PX) {
              inserts[M].push([ms, n.t, n.p]);
              moves.push([L, vi, n.p]);
            }
          }
        }
      }
    }
  }
  for (const [L, vi, p] of moves) lines[L].pts[vi] = p;
  lines.forEach((ln, li) => {
    if (!inserts[li].length) return;
    inserts[li].sort((p, q) => q[0] - p[0] || q[1] - p[1]); // back to front, so indexes stay valid
    for (const [si, , p] of inserts[li]) {
      const before = ln.pts[si];
      const after = ln.pts[si + 1];
      if ((before[0] === p[0] && before[1] === p[1]) || (after[0] === p[0] && after[1] === p[1])) continue;
      ln.pts.splice(si + 1, 0, p);
    }
  });
}

const NOT_STREETS = new Set(['rail', 'transit', 'ferry', 'aerialway', 'busway', 'bus_guideway', 'raceway']);

/**
 * One tile's streets and places as { ways: [{ id, n, k, c: [lat, lon, ...] }],
 * pois: [{ id, n, k, la, lo }] }, the same shape street.js has always used.
 */
export function streetData(bytes, ty, tx) {
  const layers = decode(bytes, ['transportation_name', 'poi', 'housenumber']);
  const toLatLon = (px, py, extent) => [
    Math.round(latOf(ty + py / extent) * 1e6) / 1e6,
    Math.round(lonOf(tx + px / extent) * 1e6) / 1e6,
  ];
  const lines = [];
  for (const f of layers.transportation_name || []) {
    const name = f.tags.name_en || f.tags.name || '';
    if (!name || f.type !== 2 || NOT_STREETS.has(f.tags.class)) continue;
    // Underground roads (tunnels) can't be walked and mustn't be named as the street you're on.
    if ((f.tags.layer || 0) < 0) continue;
    for (const line of f.geometry) {
      if (line.length >= 2) {
        lines.push({ name, kind: f.tags.subclass || f.tags.class || '', layer: f.tags.layer || 0, extent: f.extent, pts: line });
      }
    }
  }
  joinCorners(lines);
  const ways = lines.map((ln, i) => {
    const c = [];
    for (const [px, py] of ln.pts) c.push(...toLatLon(px, py, ln.extent));
    return { id: `${ty}/${tx}/${i}`, n: ln.name, k: ln.kind, c };
  });
  const pois = [];
  for (const [i, f] of (layers.poi || []).entries()) {
    const name = f.tags.name_en || f.tags.name || '';
    if (!name || f.type !== 1 || !f.geometry[0]) continue;
    const [px, py] = f.geometry[0][0];
    // Tiles repeat places from a wide margin around their edges; keep each
    // place only in the tile it is really in, or it is listed several times.
    if (px < 0 || py < 0 || px >= f.extent || py >= f.extent) continue;
    const [la, lo] = toLatLon(px, py, f.extent);
    pois.push({ id: `${ty}/${tx}/p${i}`, n: name, k: placeKind(f.tags), c: f.tags.class || '', la, lo });
  }
  // House numbers: the number only, no street; street.js matches each to its nearest street.
  const numbers = [];
  for (const f of layers.housenumber || []) {
    const num = String(f.tags.housenumber || '').trim();
    if (!num || f.type !== 1 || !f.geometry[0]) continue;
    const [px, py] = f.geometry[0][0];
    if (px < 0 || py < 0 || px >= f.extent || py >= f.extent) continue;
    const [la, lo] = toLatLon(px, py, f.extent);
    numbers.push({ n: num, la, lo });
  }
  return { ways, pois, numbers };
}
