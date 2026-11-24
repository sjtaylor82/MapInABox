// Postcodes, offline, as the desktop's postal_codes.py: the nearest GeoNames
// postcode point (CC-BY 4.0) to a position. Each country's file is loaded
// only when the cursor is in it.
//
// This is a nearest-point match, not a boundary lookup, so close to a
// boundary it can give the neighbouring postcode. Canada, the Netherlands
// and the United Kingdom only have the first part of the postcode.

const MAX_MATCH_KM = 60;
const BBOX_PAD = 0.5;
const M = 111000;

let manifest = null;
const countries = new Map(); // code -> Promise<{ admins, rows }>

async function loadManifest() {
  if (!manifest) {
    manifest = fetch('data/postcodes/manifest.json').then((r) => (r.ok ? r.json() : {})).catch(() => ({}));
  }
  return manifest;
}

function loadCountry(cc) {
  if (!countries.has(cc)) {
    countries.set(cc, fetch(`data/postcodes/${cc}.json`)
      .then((r) => (r.ok ? r.json() : null))
      .catch(() => null));
  }
  return countries.get(cc);
}

/** { postcode, place, admin } for a position, or null when none is near enough. */
// Mail-only postcodes, not places: "Coorparoo DC" (delivery centre), "Brisbane GPO".
const MAIL_ONLY = /\b(DC|BC|MC|GPO|LPO|PO Box(es)?|Post Office|Mail Centre)$/i;

// Australian postcodes kept for PO boxes and large mail users (Burwood 1805,
// not Burwood 2134): NSW 1000-1999, ACT 0200-0299, NT 0900-0999, VIC 8000-8999,
// QLD 9000-9999, SA 5800-5999, WA 6800-6999, TAS 7800-7999.
function auPoBox(pc) {
  const n = Number(pc);
  return (n >= 1000 && n <= 1999) || (n >= 200 && n <= 299) || (n >= 900 && n <= 999) || (n >= 8000 && n <= 9999)
    || (n >= 5800 && n <= 5999) || (n >= 6800 && n <= 6999) || (n >= 7800 && n <= 7999);
}

export async function postcodeAt(lat, lon) {
  const m = await loadManifest();
  const candidates = Object.entries(m).filter(([, [s, n, w, e]]) =>
    lat >= s - BBOX_PAD && lat <= n + BBOX_PAD && lon >= w - BBOX_PAD && lon <= e + BBOX_PAD);
  let best = null;
  let bestD = Infinity;
  const k = Math.cos((lat * Math.PI) / 180);
  for (const [cc] of candidates) {
    const data = await loadCountry(cc);
    if (!data) continue;
    for (const r of data.rows) {
      if (MAIL_ONLY.test(r[1]) || (cc === 'AU' && auPoBox(r[0]))) continue;
      const dy = (r[3] - lat) * M;
      const dx = (r[4] - lon) * M * k;
      const d = dx * dx + dy * dy;
      if (d < bestD) {
        bestD = d;
        best = { postcode: r[0], place: r[1], admin: data.admins[r[2]] || '' };
      }
    }
  }
  if (!best || Math.sqrt(bestD) > MAX_MATCH_KM * 1000) return null;
  return best;
}
