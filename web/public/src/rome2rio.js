// Rome2Rio journey options, made short and speakable.
//
// Shared by the Worker (which fetches Rome2Rio's public Search and
// flightSchedules data and sends the browser only what it needs) and the
// page (which reads the result out). As in the desktop app (rome2rio.py),
// these are broad multimodal estimates, not a live booking feed.

/** Vehicle kinds whose route follows roads or paths, so food along it can be listed. */
export const ROAD_KINDS = new Set(['car', 'bus', 'taxi', 'rideshare', 'towncar', 'shuttle', 'walk', 'bicycle']);

const clean = (v) => String(v ?? '').replace(/\s+/g, ' ').trim();
const at = (list, i) => (Number.isInteger(i) && list && i >= 0 && i < list.length ? list[i] : null);

function place(places, i) {
  const p = at(places, i);
  if (!p) return null;
  const lat = Number(p.lat ?? p.latitude);
  const lon = Number(p.lng ?? p.lon ?? p.longitude);
  return {
    name: clean(p.shortName || p.canonicalName || p.name),
    kind: clean(p.kind),
    code: clean(p.code),
    lat: Number.isFinite(lat) ? lat : null,
    lon: Number.isFinite(lon) ? lon : null,
    // Rome2Rio gives cities and regions a radius in kilometres; stations and exact points have none.
    area: Number(p.radius) > 0.5,
  };
}

function price(prices) {
  const p = (prices || []).find((x) => x && x.isDisplay !== false) || (prices || [])[0];
  if (!p) return null;
  const low = Number(p.priceLow);
  const high = Number(p.priceHigh);
  const mid = Number(p.price);
  return {
    currency: clean(p.currencyCode),
    low: Number.isFinite(low) ? low : Number.isFinite(mid) ? mid : null,
    high: Number.isFinite(high) ? high : null,
  };
}

/**
 * Rome2Rio Search response -> { from, to, options: [...] }.
 * Each option: { name, duration (seconds), price, legs }.
 * Each leg: { kind, mode, operator, services, from, to, stops, duration,
 *             distance (metres), perWeek, path (encoded polyline, road legs only) }.
 * Transfers between hops are left out; they are walks of a few metres.
 */
export function parseSearch(payload) {
  const places = payload.places || [];
  const { segments = [], options = [], hops = [], lines = [], carriers = [], vehicles = [], paths = [] } = payload;
  const out = [];
  for (const route of payload.routes || []) {
    const legs = [];
    for (const si of route.segments || []) {
      const segment = at(segments, si) || {};
      const option = at(options, (segment.options || [])[0]) || {};
      for (const hi of option.hops || []) {
        const hop = at(hops, hi) || {};
        const vehicle = at(vehicles, hop.vehicle) || {};
        const kind = clean(vehicle.kind || vehicle.name).toLowerCase() || 'travel';
        if (kind === 'transfer') continue;
        const line = at(lines, hop.line) || {};
        const stops = (line.places || []).map((i) => place(places, i)).filter(Boolean);
        const carrier = at(carriers, hop.marketingCarrier);
        const freq = Number(hop.frequency);
        const path = ROAD_KINDS.has(kind) ? at(paths, line.path) : null;
        legs.push({
          kind,
          mode: clean(vehicle.name) || kind,
          operator: carrier ? clean(carrier.name) : '',
          services: (line.names || []).map(clean).filter(Boolean).slice(0, 3),
          from: stops[0] || null,
          to: stops[stops.length - 1] || null,
          stops: Math.max(0, stops.length - 2),
          duration: Number(hop.duration) || 0,
          distance: Number(line.distance) || 0,
          perWeek: freq > 0 ? freq : 0,
          path: typeof path === 'string' ? path : '',
        });
      }
    }
    if (!legs.length) continue;
    out.push({
      name: clean(route.name) || 'Route',
      duration: Number(route.duration) || 0,
      price: price(route.indicativePrices),
      legs,
    });
  }
  const req = payload.request || {};
  return { from: clean(req.oName), to: clean(req.dName), options: out };
}

/**
 * Rome2Rio flightSchedules response -> [{ changes, depart, arrive, minutes, hops }],
 * non-stop flights first, then by departure time, each itinerary once.
 * Each hop: { carrier, operatedBy, flight, from, to, depart, arrive, layover }.
 */
export function parseFlights(payload, max = 40) {
  const { outboundItineraries = [], legs = [], hops = [], lines = [], places = [], carriers = [], layovers = [] } = payload;
  const seen = new Set();
  const out = [];
  for (const it of outboundItineraries) {
    const leg = at(legs, (it.legs || [])[0]);
    if (!leg) continue;
    const hopList = (leg.hops || []).map((i) => at(hops, i));
    if (!hopList.length || hopList.some((h) => !h)) continue;
    const key = hopList.map((h) => [h.marketingCarrier, h.name, h.departureDate, h.departureTime].join('/')).join('|');
    if (seen.has(key)) continue;
    seen.add(key);
    const described = hopList.map((h, n) => {
      const line = at(lines, h.line) || {};
      const [o, d] = (line.places || []).map((i) => at(places, i) || {});
      const m = at(carriers, h.marketingCarrier) || {};
      const op = at(carriers, h.operatingCarrier) || m;
      const lay = at(layovers, (leg.layovers || [])[n]);
      return {
        carrier: clean(m.name),
        operatedBy: op.name && op.name !== m.name ? clean(op.name) : '',
        flight: `${clean(m.code)}${clean(h.name)}`,
        from: clean(o?.code || o?.name),
        to: clean(d?.code || d?.name),
        depart: clean(h.departureTime),
        arrive: clean(h.arrivalTime),
        nextDay: !!(h.arrivalDate && h.departureDate && h.arrivalDate !== h.departureDate),
        layover: lay ? Number(lay.duration) || 0 : 0,
      };
    });
    out.push({
      changes: hopList.length - 1,
      depart: described[0].depart,
      arrive: described[described.length - 1].arrive,
      minutes: Number(leg.duration) || 0,
      hops: described,
    });
  }
  out.sort((a, b) => (a.changes > 0) - (b.changes > 0) || a.depart.localeCompare(b.depart));
  return out.slice(0, max);
}

// ── Route lines ───────────────────────────────────────────────────────────

/** Decode a Google-style encoded polyline (5 decimal places) to [[lat, lon], ...]. */
export function decodePolyline(str) {
  const pts = [];
  let i = 0;
  let lat = 0;
  let lon = 0;
  while (i < str.length) {
    for (const which of [0, 1]) {
      let shift = 0;
      let result = 0;
      let b;
      do {
        b = str.charCodeAt(i++) - 63;
        result |= (b & 0x1f) << shift;
        shift += 5;
      } while (b >= 0x20 && i < str.length);
      const d = result & 1 ? ~(result >> 1) : result >> 1;
      if (which === 0) lat += d; else lon += d;
    }
    pts.push([lat / 1e5, lon / 1e5]);
  }
  return pts;
}

/** Encode [[lat, lon], ...] as a Google-style polyline. */
export function encodePolyline(pts) {
  let out = '';
  let pLat = 0;
  let pLon = 0;
  const enc = (v) => {
    let n = v < 0 ? ~(v << 1) : v << 1;
    let s = '';
    while (n >= 0x20) {
      s += String.fromCharCode((0x20 | (n & 0x1f)) + 63);
      n >>= 5;
    }
    return s + String.fromCharCode(n + 63);
  };
  for (const [lat, lon] of pts) {
    const a = Math.round(lat * 1e5);
    const b = Math.round(lon * 1e5);
    out += enc(a - pLat) + enc(b - pLon);
    pLat = a;
    pLon = b;
  }
  return out;
}
