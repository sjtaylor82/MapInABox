// Map in a Box Web — places of a kind near a point, for Jump's "what in where"
// ("pizza in Cleveland", "chinese in Coorparoo", "pharmacy near Carina").
// Used by worker/index.js.
//
//   GET /api/near?q=&lat=&lon=   Places within 3 km (6 km if under 3), nearest
//       first: { results: [{ name, lat, lon, kind, street, number, website, food, dist }] }
//
// From OpenStreetMap (Overpass), so it costs nothing and is in Starter too.
// "chinese" finds restaurants whose cuisine is Chinese, though few have the word
// in their names; kinds of place ("cafe", "pharmacy", "park") are matched by what
// they are; anything else by cuisine, by what the place is, or by name.

import { OVERPASS } from '../public/src/placedetails.js';

const USER_AGENT = 'MapInABoxWeb/0.1 (+https://github.com/sjtaylor82/MapInABox)';
const FOOD_AMENITY = 'restaurant|cafe|fast_food|food_court|pub|bar|ice_cream|biergarten';

// Kinds of place by what they are (OpenStreetMap tags), not by name: a park's
// name may not say park, and "Park" in a name ("Parkside Pizza") isn't one.
const KINDS = [
  [/^(cafes?|coffee( shops?)?)$/, ['amenity=cafe']],
  [/^restaurants?$/, ['amenity=restaurant']],
  [/^(pubs?|hotels? bars?)$/, ['amenity=pub']],
  [/^bars?$/, ['amenity=bar', 'amenity=pub']],
  [/^(takeaways?|take ?aways?|fast ?food)$/, ['amenity=fast_food']],
  [/^(food|places to eat|eat|dinner|lunch|breakfast|brunch)$/, [`amenity~"^(${FOOD_AMENITY})$"`]],
  [/^(bakery|bakeries|bakers?)$/, ['shop=bakery']],
  [/^(pharmacy|pharmacies|chemists?)$/, ['amenity=pharmacy']],
  [/^(supermarkets?|groceries|grocery|grocers?)$/, ['shop=supermarket', 'shop=greengrocer']],
  [/^(doctors?|gps?|medical( centres?)?)$/, ['amenity=doctors', 'amenity=clinic']],
  [/^dentists?$/, ['amenity=dentist']],
  [/^(petrol( stations?)?|fuel|servos?|service stations?)$/, ['amenity=fuel']],
  [/^atms?$/, ['amenity=atm']],
  [/^banks?$/, ['amenity=bank']],
  [/^post( offices?)?$/, ['amenity=post_office']],
  [/^librar(y|ies)$/, ['amenity=library']],
  [/^parks?$/, ['leisure=park']],
  [/^playgrounds?$/, ['leisure=playground']],
  [/^(toilets?|loos?|bathrooms?)$/, ['amenity=toilets']],
  [/^(hotels?|motels?|accommodation)$/, ['tourism~"^(hotel|motel|guest_house|apartment)$"']],
  [/^(gyms?|fitness)$/, ['leisure=fitness_centre']],
  [/^(hairdressers?|barbers?|hair)$/, ['shop=hairdresser']],
  [/^butchers?$/, ['shop=butcher']],
  [/^(florists?|flowers)$/, ['shop=florist']],
  [/^hardware( stores?)?$/, ['shop~"^(hardware|doityourself)$"']],
  [/^(bottle ?shops?|bottle-?os?|liquor( stores?)?)$/, ['shop=alcohol']],
  [/^newsagents?$/, ['shop=newsagent']],
  [/^schools?$/, ['amenity=school']],
  [/^hospitals?$/, ['amenity=hospital']],
  [/^(ice ?creams?|gelato|gelaterias?)$/, ['amenity=ice_cream', 'cuisine~"ice_cream|gelato",i']],
  [/^vegan$/, ['"diet:vegan"~"^(yes|only)$"', 'cuisine~"vegan",i']],
  [/^vegetarian$/, ['"diet:vegetarian"~"^(yes|only)$"', 'cuisine~"vegetarian",i']],
];

/** "Fish and chips" -> "fish[ _]and[ _]chips"; only plain words reach Overpass. */
const pattern = (w) => w.replace(/[^a-z0-9 &'-]/g, '').trim().replace(/\s+/g, '[ _]');

function query(kind, lat, lon, radius) {
  const k = kind.toLowerCase().replace(/\s+/g, ' ').trim();
  const known = KINDS.find(([re]) => re.test(k));
  const parts = [];
  if (known) {
    for (const f of known[1]) parts.push(`nwr.p[${f}];`);
  } else {
    // Its cuisine (also "pizzas" -> pizza), what it is, or its name.
    const words = [...new Set([k, k.replace(/(ies)$/, 'y').replace(/(es|s)$/, '')])].filter((w) => w.length >= 3).map(pattern);
    for (const w of words) {
      parts.push(`nwr.p[amenity~"^(${FOOD_AMENITY})$"][cuisine~"${w}",i];`);
      parts.push(`nwr.p[shop~"^${w}$",i];`, `nwr.p[amenity~"^${w}$",i];`, `nwr.p[name~"${w}",i];`);
    }
  }
  // Only shops, amenities and the like within the radius are searched, which keeps the
  // name search quick (searching every named thing timed out).
  return `[out:json][timeout:20];nwr(around:${radius},${lat.toFixed(5)},${lon.toFixed(5)})[~"^(amenity|shop|tourism|leisure|healthcare)$"~"."]->.p;(${parts.join('')});out center tags 300;`;
}

function distM(lat1, lon1, lat2, lon2) {
  const r = Math.PI / 180;
  const a = Math.sin(((lat2 - lat1) * r) / 2) ** 2
    + Math.cos(lat1 * r) * Math.cos(lat2 * r) * Math.sin(((lon2 - lon1) * r) / 2) ** 2;
  return 12742000 * Math.asin(Math.sqrt(a));
}

/** What a place is, to say: its cuisine for food ("pizza restaurant"), else its kind. */
function kindOf(t) {
  const what = (t.amenity || t.shop || t.tourism || t.leisure || '').replace(/_/g, ' ');
  const cuisine = (t.cuisine || '').split(';')[0].replace(/_/g, ' ').trim();
  if (what === 'cafe' && /coffee|cafe/.test(cuisine)) return 'cafe';
  if (cuisine && /restaurant|fast food|cafe/.test(what)) return `${cuisine} ${what === 'fast food' ? 'takeaway' : what}`;
  return what;
}

async function overpass(q) {
  for (const mirror of OVERPASS) {
    try {
      const res = await fetch(mirror, {
        method: 'POST',
        body: new URLSearchParams({ data: q }),
        headers: { 'User-Agent': USER_AGENT, 'Content-Type': 'application/x-www-form-urlencoded' },
        signal: AbortSignal.timeout(15000),
      });
      if (res.ok) return (await res.json()).elements || [];
    } catch { /* next */ }
  }
  return null;
}

function json(body, status = 200, maxAge = 0) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', 'Cache-Control': maxAge ? `public, max-age=${maxAge}` : 'no-store' },
  });
}

export async function placesNear(url, ctx) {
  const kind = (url.searchParams.get('q') || '').trim().slice(0, 40);
  const lat = Number(url.searchParams.get('lat'));
  const lon = Number(url.searchParams.get('lon'));
  if (kind.length < 2 || !Number.isFinite(lat) || !Number.isFinite(lon) || Math.abs(lat) > 90 || Math.abs(lon) > 180) {
    return json({ error: 'Bad request' }, 400);
  }
  const cache = caches.default;
  const key = new Request(`https://cache.mapinabox/near/v1/${encodeURIComponent(kind.toLowerCase())}/${lat.toFixed(3)}/${lon.toFixed(3)}`);
  const hit = await cache.match(key);
  if (hit) return hit;

  // 3 km (about a suburb and those around it); 6 km when that finds fewer than 3.
  let results = [];
  for (const radius of [3000, 6000]) {
    const els = await overpass(query(kind, lat, lon, radius));
    if (els === null) return json({ error: 'The map data service is busy. Try again in a moment.' }, 502);
    const seen = new Set();
    results = els.filter((e) => e.tags && e.tags.name).map((e) => {
      const t = e.tags;
      const la = e.lat ?? (e.center && e.center.lat);
      const lo = e.lon ?? (e.center && e.center.lon);
      return {
        name: t.name, lat: la, lon: lo, kind: kindOf(t),
        street: t['addr:street'] || '', number: t['addr:housenumber'] || '',
        website: t.website || t['contact:website'] || '',
        food: new RegExp(`^(${FOOD_AMENITY})$`).test(t.amenity || '') || /bakery|deli|confectionery/.test(t.shop || ''),
        dist: Math.round(distM(lat, lon, la, lo)),
      };
    }).filter((p) => Number.isFinite(p.lat) && Number.isFinite(p.lon))
      // A place drawn as both a point and a building is one place.
      .filter((p) => { const k = `${p.name.toLowerCase()}|${Math.round(p.lat * 2000)}|${Math.round(p.lon * 2000)}`; return !seen.has(k) && seen.add(k); })
      .sort((a, b) => a.dist - b.dist)
      .slice(0, 40);
    if (results.length >= 3) break;
  }
  const out = json({ results }, 200, results.length ? 24 * 3600 : 0);
  if (results.length) ctx.waitUntil(cache.put(key, out.clone()));
  return out;
}
