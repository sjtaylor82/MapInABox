// Details for one place, from OpenStreetMap through Overpass: phone, opening
// hours, website and address. The map tiles leave these out; a lookup for a
// single place is small and quick, unlike whole areas. Shared by the page
// and the Worker.

// The French server answers only clients that identify themselves (the
// Worker sends a Map in a Box User-Agent; a browser cannot), and is the most
// reliable. overpass-api.de refuses many clients and is often busy.
export const OVERPASS = [
  'https://overpass.openstreetmap.fr/api/interpreter',
  'https://overpass-api.de/api/interpreter',
  'https://overpass.kumi.systems/api/interpreter',
];

const quote = (s) => String(s).replace(/\\/g, '\\\\').replace(/"/g, '\\"');

export function detailsQuery(name, lat, lon) {
  const at = `around:60,${Number(lat).toFixed(6)},${Number(lon).toFixed(6)}`;
  return `[out:json][timeout:10];(node(${at})["name"="${quote(name)}"];way(${at})["name"="${quote(name)}"];);out tags center 1;`;
}

/** The useful tags of the first match, or null. */
export function pickDetails(json) {
  const el = (json.elements || [])[0];
  return el ? detailsFromTags(el.tags || {}) : null;
}

/** Phone, hours, website, address and so on from an OpenStreetMap element's tags. */
export function detailsFromTags(t) {
  const addr = [
    [t['addr:unit'] && `Unit ${t['addr:unit']}`, t['addr:housenumber'], t['addr:street']].filter(Boolean).join(' '),
    t['addr:suburb'] || t['addr:city'],
    t['addr:postcode'],
  ].filter(Boolean).join(', ');
  return {
    phone: t.phone || t['contact:phone'] || '',
    hours: t.opening_hours || '',
    website: t.website || t['contact:website'] || '',
    address: addr,
    cuisine: (t.cuisine || '').replace(/[_;]/g, ' ').replace(/\s+/g, ' ').trim(),
    wheelchair: t.wheelchair || '',
    delivery: t.delivery || '',
    takeaway: t.takeaway || '',
  };
}

const DAYS = { Mo: 'Monday', Tu: 'Tuesday', We: 'Wednesday', Th: 'Thursday', Fr: 'Friday', Sa: 'Saturday', Su: 'Sunday', PH: 'public holidays' };

/** "Mo-Fr 07:00-17:00; Sa 08:00-12:00" read as words. */
export function speakableHours(hours) {
  if (hours === '24/7') return 'open 24 hours, every day';
  return hours
    .replace(/\b(Mo|Tu|We|Th|Fr|Sa|Su|PH)\b/g, (d) => DAYS[d])
    .replace(/(\w+day|public holidays)-(\w+day)/g, '$1 to $2')
    .replace(/(\d\d:\d\d)-(\d\d:\d\d)/g, '$1 to $2')
    .replace(/\boff\b/g, 'closed')
    .replace(/;\s*/g, '. ')
    .replace(/,\s*/g, ', ');
}
