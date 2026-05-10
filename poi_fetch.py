"""poi_fetch.py — POI fetching and parsing for Map in a Box.

All Overpass queries related to Points of Interest live here.
No wx imports, no MapNavigator state — every method takes what it needs
as arguments and returns plain data (lists of dicts).

MapNavigator holds a PoiFetcher instance and is responsible for:
  - calling these methods on background threads
  - calling wx.CallAfter to update the UI with results
  - storing results in self._poi_list / self._all_pois

Classes
-------
PoiFetcher
    fetch_pois(lat, lon, category, radius, timeout) → list[dict]
    fetch_all_background(lat, lon, address_points)  → list[dict]
    fetch_poi_intersection(lat, lon, road_segments)  → str | None
    fetch_explore_children(lat, lon, osm_type, osm_id, centre_lat, centre_lon) → list[dict]
"""

import json
import math
import os
import re
import time
import urllib.parse
import urllib.request

from geo import (
    dist_metres, bearing_deg, compass_name,
    dist_to_segment_metres, GENERIC_STREET_TYPES, LOW_PRIORITY_HIGHWAY,
)

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

POI_BACKGROUND_RADIUS_METRES = 2000

POI_CATEGORY_CHOICES: list[tuple[str, str]] = [
    ("all",            "All nearby"),
    ("food",           "Food & drink"),
    ("shopping",       "Shopping"),
    ("transport",      "Public transport"),
    ("trains",         "Trains & stations"),
    ("health",         "Health & medical"),
    ("community",      "Community & services"),
    ("arts",           "Arts, venues & landmarks"),
    ("parks",          "Parks & outdoors"),
    ("accommodation",  "Accommodation"),
]

# OSM kind values to skip unconditionally
POI_KIND_EXCLUDE: frozenset = frozenset({
    "bench", "bicycle parking", "drinking water", "waste basket",
})

# Kinds that are useful even without a name
UNNAMED_OK_KINDS: frozenset = frozenset({
    "mall", "shopping centre", "department store", "supermarket",
    "marketplace", "hospital", "university", "school", "college",
    "airport", "station", "bus station", "ferry terminal",
    "park", "garden", "sports centre", "stadium", "museum",
    "theatre", "arts centre", "conference centre", "events venue", "gallery",
    "library", "cinema", "community centre", "place of worship",
    "zoo", "theme park", "cafe", "restaurant", "bar", "pub",
    "fast food", "pharmacy", "bank", "atm", "post office",
    "police", "fire station", "doctors", "dentist", "clinic",
})

EXPLORABLE_KINDS: frozenset = frozenset({
    # Shopping
    "mall", "department store", "shopping centre",
    # Transit — major nodes only (bus stops handled separately via GTFS)
    "station", "bus station", "ferry terminal",
    # Airport
    "airport",
    # Major venues
    "theatre", "arts centre", "conference centre", "events venue", "stadium",
})

SHOPPING_PRIORITY: dict[str, int] = {
    "mall": 0,
    "department store": 1,
    "shopping centre": 2,
    "supermarket": 3,
    "marketplace": 4,
}

# Overpass query fragments per category.
# {lat}, {lon}, {radius} are filled at call time.
_CATEGORY_QUERIES: dict[str, list[str]] = {
    "all": [
        'nwr["amenity"~"cafe|restaurant|bar|pharmacy|hospital|school|library|bus_station|toilets|atm|bank|supermarket|fast_food|community_centre|cinema|theatre|arts_centre|conference_centre|events_venue|place_of_worship|marketplace|ferry_terminal|post_office|dentist|doctors|veterinary|fuel"](around:{radius},{lat},{lon});',
        'nwr["railway"~"station|halt|tram_stop"](around:{radius},{lat},{lon});',
        'node["public_transport"="station"]["train"="yes"](around:{radius},{lat},{lon});',
        'nwr["leisure"~"park|playground|sports_centre|garden|fitness_centre|shopping_centre|stadium|theme_park"](around:{radius},{lat},{lon});',
        'nwr["tourism"~"attraction|museum|hotel|information|viewpoint|gallery|zoo|theme_park"](around:{radius},{lat},{lon});',
        'nwr["shop"~"convenience|supermarket|bakery|chemist|mall|department_store|clothes|books|electronics"](around:{radius},{lat},{lon});',
        'node["historic"](around:{radius},{lat},{lon});',
        'node["natural"~"peak|spring|beach|cliff"](around:{radius},{lat},{lon});',
        'node["man_made"~"lighthouse|tower|windmill"](around:{radius},{lat},{lon});',
    ],
    "shopping": [
        'nwr["shop"~"mall|department_store|supermarket|convenience|bakery|chemist|clothes|books|electronics|shoes|jewelry|mobile_phone"](around:{radius},{lat},{lon});',
        'nwr["amenity"~"marketplace"](around:{radius},{lat},{lon});',
        'nwr["leisure"~"shopping_centre"](around:{radius},{lat},{lon});',
    ],
    "food": [
        'nwr["amenity"~"cafe|restaurant|bar|fast_food|pub|food_court|ice_cream"](around:{radius},{lat},{lon});',
        'nwr["shop"~"bakery|supermarket|convenience|greengrocer|butcher"](around:{radius},{lat},{lon});',
    ],
    "transport": [
        'node["public_transport"~"stop_position|platform|station"](around:{radius},{lat},{lon});',
        'node["highway"~"bus_stop"](around:{radius},{lat},{lon});',
        'nwr["amenity"~"bus_station|ferry_terminal"](around:{radius},{lat},{lon});',
        'nwr["railway"~"station|halt|tram_stop"](around:{radius},{lat},{lon});',
    ],
    "trains": [
        'nwr["railway"~"station|halt"](around:{radius},{lat},{lon});',
        'node["public_transport"="station"]["train"="yes"](around:{radius},{lat},{lon});',
        'nwr["public_transport"="station"](around:{radius},{lat},{lon});',
    ],
    "health": [
        'nwr["amenity"~"hospital|pharmacy|dentist|doctors|clinic|veterinary"](around:{radius},{lat},{lon});',
    ],
    "community": [
        'nwr["amenity"~"school|library|community_centre|cinema|place_of_worship|post_office|bank|atm|toilets|college|university"](around:{radius},{lat},{lon});',
    ],
    "arts": [
        'nwr["amenity"~"theatre|arts_centre|conference_centre|events_venue|cinema"](around:{radius},{lat},{lon});',
        'nwr["tourism"~"attraction|museum|gallery"](around:{radius},{lat},{lon});',
        'nwr["leisure"~"stadium"](around:{radius},{lat},{lon});',
        'nwr["building"="theatre"](around:{radius},{lat},{lon});',
    ],
    "parks": [
        'nwr["leisure"~"park|playground|sports_centre|garden|fitness_centre|stadium|theme_park"](around:{radius},{lat},{lon});',
        'nwr["tourism"~"attraction|museum|information|viewpoint|gallery|zoo|theme_park"](around:{radius},{lat},{lon});',
        'node["historic"](around:{radius},{lat},{lon});',
        'node["natural"~"peak|spring|beach|cliff"](around:{radius},{lat},{lon});',
    ],
    "accommodation": [
        'nwr["tourism"~"hotel|motel|hostel|guest_house|apartment|camp_site|caravan_site"](around:{radius},{lat},{lon});',
    ],
}

# ---------------------------------------------------------------------------
# Kind → category mapping for filtering _all_pois without re-querying Overpass
# ---------------------------------------------------------------------------

# Transit kinds used by is_transit_poi and GTFS prefetch
TRANSIT_KINDS: frozenset = frozenset({
    "station", "halt", "tram stop",
    "bus station", "ferry terminal", "stop position", "platform",
    "bus stop",
})

# Explicit kind → category mapping.
# Derived from _CATEGORY_QUERIES tag values — maintained manually since the
# query format is too complex to parse reliably with a regex.
_KIND_TO_CATEGORY: dict[str, str] = {
    # food
    "food": "food", "cafe": "food", "restaurant": "food", "bar": "food", "fast food": "food",
    "pub": "food", "food court": "food", "ice cream": "food",
    "bakery": "food", "supermarket": "food", "convenience": "food",
    "greengrocer": "food", "butcher": "food",
    # shopping
    "mall": "shopping", "department store": "shopping",
    "shopping centre": "shopping", "marketplace": "shopping",
    "shop": "shopping",
    "clothes": "shopping", "books": "shopping", "electronics": "shopping",
    "shoes": "shopping", "jewelry": "shopping", "mobile phone": "shopping",
    "chemist": "shopping", "hardware": "shopping", "furniture": "shopping",
    "florist": "shopping", "gift shop": "shopping", "music": "shopping",
    "toys": "shopping", "sports shop": "shopping", "pet shop": "shopping",
    "optician": "shopping",
    # health
    "health": "health", "hospital": "health", "pharmacy": "health", "dentist": "health",
    "doctors": "health", "clinic": "health", "veterinary": "health",
    # community
    "school": "community", "library": "community",
    "community centre": "community",
    "place of worship": "community", "post office": "community",
    "bank": "community", "atm": "community", "toilets": "community",
    "college": "community", "university": "community",
    "police": "community", "fire station": "community",
    "laundry": "community", "hairdresser": "community",
    "fuel": "community", "parking": "community",
    # parks
    "park": "parks", "playground": "parks", "sports centre": "parks",
    "garden": "parks", "fitness centre": "parks",
    "theme park": "parks", "zoo": "parks",
    "information": "parks", "viewpoint": "parks",
    # arts, venues, landmarks
    "cinema": "arts", "theatre": "arts", "arts centre": "arts",
    "conference centre": "arts", "events venue": "arts",
    "stadium": "arts", "attraction": "arts", "museum": "arts",
    "gallery": "arts",
    # accommodation
    "hotel": "accommodation", "motel": "accommodation",
    "hostel": "accommodation", "guest house": "accommodation",
    "camp site": "accommodation", "caravan site": "accommodation",
    # transport
    "transit station": "transport", "bus stop": "transport", "bus station": "transport",
    "ferry terminal": "transport", "tram stop": "transport",
    "stop position": "transport", "platform": "transport",
    "transport": "transport", "airport": "transport",
    # trains (subset of transport — larger stations with GTFS data)
    "station": "trains", "halt": "trains",
}

# ---------------------------------------------------------------------------
# HERE Places integration
# ---------------------------------------------------------------------------

_HERE_BROWSE_URL = "https://browse.search.hereapi.com/v1/browse"

# HERE top-level category IDs mapped to our category keys.
_HERE_CATEGORIES: dict[str, str] = {
    "food":      "100",
    "shopping":  "600",
    "transport": "400",   # top-level transport — sub-category IDs vary by API version
    "trains":    "400",   # same top-level; _HERE_KIND_MAP + filter_pois_by_category narrows to trains
    "health":    "700",
    "community": "800",
    "arts":     "300",
    "parks":         "300",
    "accommodation": "500",
}

# HERE category name → our kind string.
# Only kinds useful to a pedestrian explorer are mapped.
# Anything not in this map AND not a passthrough kind is filtered out.
_HERE_KIND_MAP: dict[str, str] = {
    # ── Food & Drink ─────────────────────────────────────────────────
    "restaurant":               "restaurant",
    "casual dining":            "restaurant",
    "fine dining":              "restaurant",
    "coffee shop":              "cafe",
    "cafe":                     "cafe",
    "tea house":                "cafe",
    "donut shop":               "cafe",
    "juice bar":                "cafe",
    "bar or pub":               "bar",
    "bar":                      "bar",
    "pub":                      "pub",
    "nightclub":                "bar",
    "fast food":                "fast food",
    "food court":               "fast food",
    "chicken":                  "fast food",
    "hamburgers":               "fast food",
    "pizza":                    "restaurant",
    "seafood":                  "restaurant",
    "asian food":               "restaurant",
    "chinese food":             "restaurant",
    "thai food":                "restaurant",
    "indian food":              "restaurant",
    "mexican food":             "restaurant",
    "italian food":             "restaurant",
    "sushi":                    "restaurant",
    "vegetarian/vegan":         "restaurant",
    "bakery":                   "bakery",
    "bakery and deli":          "bakery",
    "ice cream/frozen yogurt":  "cafe",
    "ice cream":                "cafe",
    "smoothie or juice bar":    "cafe",
    "supermarket or hypermarket": "supermarket",
    "supermarket":              "supermarket",
    "grocery":                  "supermarket",
    "convenience store":        "convenience",
    "butcher":                  "butcher",
    "greengrocer":              "greengrocer",
    "fishmonger":               "butcher",
    "wine and spirits":         "bar",
    "liquor store":             "bar",
    # ── Shopping ─────────────────────────────────────────────────────
    "shopping mall":            "mall",
    "shopping center":          "mall",
    "shopping centre":          "mall",
    "department store":         "department store",
    "clothing":                 "clothes",
    "clothing store":           "clothes",
    "fashion":                  "clothes",
    "shoes":                    "shoes",
    "shoe store":               "shoes",
    "jewelry and watches":      "jewelry",
    "jewelry":                  "jewelry",
    "electronics":              "electronics",
    "consumer electronics":     "electronics",
    "computers and electronics": "electronics",
    "mobile phone":             "mobile phone",
    "mobile phone store":       "mobile phone",
    "books":                    "books",
    "bookstore":                "books",
    "newsagent":                "books",
    "newsagent or tobacconist": "books",
    "chemist or drugstore":     "chemist",
    "chemist":                  "chemist",
    "hardware store":           "hardware",
    "home improvement":         "hardware",
    "furniture":                "furniture",
    "florist":                  "florist",
    "gift shop":                "gift shop",
    "music store":              "music",
    "toy store":                "toys",
    "sporting goods":           "sports shop",
    "sports store":             "sports shop",
    "pet store":                "pet shop",
    "optician":                 "optician",
    "eyewear":                  "optician",
    # ── Health & Medical ─────────────────────────────────────────────
    "hospital":                 "hospital",
    "emergency room":           "hospital",
    "pharmacy":                 "pharmacy",
    "dentist":                  "dentist",
    "dental office":            "dentist",
    "physician":                "doctors",
    "doctor":                   "doctors",
    "medical center":           "clinic",
    "medical office":           "clinic",
    "clinic":                   "clinic",
    "urgent care":              "clinic",
    "veterinarian":             "veterinary",
    "animal hospital":          "veterinary",
    "physiotherapist":          "clinic",
    "physical therapy":         "clinic",
    "therapist":                "clinic",
    "psychologist":             "clinic",
    "mental health":            "clinic",
    "optometrist":              "clinic",
    "audiologist":              "clinic",
    "pathology":                "clinic",
    "radiology":                "clinic",
    "aged care":                "clinic",
    "nursing home":             "clinic",
    "disability services":      "community centre",
    # ── Education ────────────────────────────────────────────────────
    "school":                   "school",
    "primary school":           "school",
    "high school":              "school",
    "secondary school":         "school",
    "kindergarten":             "school",
    "childcare":                "school",
    "preschool or childcare":   "school",
    "university or college":    "university",
    "university":               "university",
    "college":                  "university",
    "tafe":                     "university",
    "library":                  "library",
    "tutoring":                 "school",
    # ── Community & Services ─────────────────────────────────────────
    "bank":                     "bank",
    "atm":                      "atm",
    "post office":              "post office",
    "police station":           "police",
    "fire station":             "fire station",
    "ambulance station":        "fire station",
    "government office":        "community centre",
    "community centre":         "community centre",
    "community center":         "community centre",
    "club":                     "community centre",
    "rsl club":                 "community centre",
    "leagues club":             "community centre",
    "sports club":              "community centre",
    "courthouse":               "community centre",
    "cinema":                   "cinema",
    "movie theater":            "cinema",
    "place of worship":         "place of worship",
    "church":                   "place of worship",
    "mosque":                   "place of worship",
    "temple":                   "place of worship",
    "synagogue":                "place of worship",
    "funeral home":             "community centre",
    "embassy":                  "community centre",
    "laundry":                  "laundry",
    "dry cleaning and laundry": "laundry",
    "hairdresser":              "hairdresser",
    "barber shop":              "hairdresser",
    "beauty salon":             "hairdresser",
    "nail salon":               "hairdresser",
    "tattoo":                   "hairdresser",
    "gym":                      "fitness centre",
    "fitness center":           "fitness centre",
    "fitness centre":           "fitness centre",
    "hotel":                    "hotel",
    "motel":                    "hotel",
    "hostel":                   "hotel",
    "guest house":              "hotel",
    "lodging":                  "hotel",
    "bed and breakfast":        "hotel",
    "fuel station":             "fuel",
    "gas station":              "fuel",
    "petrol station":           "fuel",
    "car wash":                 "fuel",
    # ── Parks & Outdoors ─────────────────────────────────────────────
    "park":                     "park",
    "national park":            "park",
    "nature reserve":           "park",
    "garden":                   "garden",
    "botanical garden":         "garden",
    "playground":               "playground",
    "beach":                    "park",
    "sports centre":            "sports centre",
    "stadium":                  "stadium",
    "sports complex":           "stadium",
    "swimming pool":            "sports centre",
    "golf course":              "sports centre",
    "sports facility":          "sports centre",
    "theater":                  "theatre",
    "theatre":                  "theatre",
    "performing arts theater":  "theatre",
    "performing arts theatre":  "theatre",
    "performing arts":          "theatre",
    "concert hall":             "theatre",
    "music venue":              "theatre",
    "event venue":              "events venue",
    "events venue":             "events venue",
    "convention center":        "conference centre",
    "convention centre":        "conference centre",
    "conference center":        "conference centre",
    "conference centre":        "conference centre",
    "arts center":              "arts centre",
    "arts centre":              "arts centre",
    "art center":               "arts centre",
    "art centre":               "arts centre",
    "museum":                   "museum",
    "art gallery":              "gallery",
    "gallery":                  "gallery",
    "zoo":                      "zoo",
    "aquarium":                 "zoo",
    "theme park":               "theme park",
    "tourist attraction":       "attraction",
    "attraction":               "attraction",
    "landmark":                 "attraction",
    "monument":                 "attraction",
    "casino":                   "attraction",
    "bowling":                  "attraction",
    "cinema complex":           "cinema",
    # ── Transport ────────────────────────────────────────────────────
    "bus stop":                 "bus stop",
    "bus station":              "bus station",
    "railway station":          "station",
    "train station":            "station",
    "metro station":            "station",
    "tram stop":                "tram stop",
    "ferry terminal":           "ferry terminal",
    "ferry":                    "ferry terminal",
    "taxi stand":               "transport",
    "transportation service":   "transport",
    "airport":                  "airport",
    "hospital helipad":         "airport",
    # ── Additional categories from live data ────────────────────────────
    # Food
    "food/beverage specialty store":    "shop",
    "cafeteria":                        "cafe",
    "coffee/tea":                       "cafe",
    "bakery & baked goods store":       "bakery",
    # Shopping
    "specialty store":                  "shop",
    "wine & liquor":                    "bottle shop",
    "gift, antique & art":              "gift shop",
    "jeweler":                          "jewelry",
    "shoes/footwear":                   "shoes",
    "furniture store":                  "furniture",
    "consumer electronics store":       "electronics",
    "home improvement/hardware store":  "hardware",
    "mobile/cell phone service center": "mobile phone",
    "computer & software":              "electronics",
    "record, cd & video":               "shop",
    "video & game rental":              "shop",
    "sporting goods store":             "sports shop",
    "cigar & tobacco shop":             "shop",
    "drugstore":                        "chemist",
    # Health & Beauty
    "hair & beauty":                    "hairdresser",
    "hair salon":                       "hairdresser",
    "barber":                           "hairdresser",
    "body piercing & tattoos":          "hairdresser",
    "medical services/clinics":         "clinic",
    "dentist/dental office":            "dentist",
    "family/general practice physicians": "doctors",
    "hospital or health care facility": "clinic",
    "healthcare and healthcare support services": "clinic",
    "wellness center & services":       "clinic",
    "chiropractor":                     "clinic",
    "fitness/health club":              "fitness centre",
    # Education & Community
    "education facility":               "school",
    "training & development":           "school",
    "fine arts":                        "school",
    "social services":                  "community centre",
    "organizations and societies":      "community centre",
    "religious place":                  "place of worship",
    "bowling center":                   "attraction",
    "leisure":                          "attraction",
    # Services
    "real estate services":             "services",
    "property management":              "services",
    "finance and insurance":            "services",
    "construction":                     "services",
    "apartment/flat rental":            "hotel",
    "public restroom":                  "toilets",
    # From live data — additional unmapped categories
    "travel agent/ticketing":           "services",
    "delivery entrance":                "services",
    "discount store":                   "shop",
    "ev charging station":              "services",
    "couriers":                         "services",
    "money transferring service":       "services",
    "drugstore or pharmacy":            "pharmacy",
    "parking garage/parking house":     "parking",
    "parking lot":                      "parking",
    "women's apparel":                  "clothes",
    "advertising/marketing, pr & market research": "services",
    "telephone service":                "services",
    "management and consulting services": "services",
    "take out & delivery only":         "fast food",
    "financial investment firm":        "services",
    "lottery booth":                    "services",
    "car wash - detailing":             "fuel",
    "clothing & accessories":           "clothes",
    "repair service":                   "services",
    "pet supply":                       "pet shop",
    "photography":                      "services",
    "specialty clothing store":         "clothes",
    "market":                           "supermarket",
    "loading dock":                     "services",
}

def _parse_here_item(
    item: dict,
    ref_lat: float,
    ref_lon: float,
    address_points: list,
) -> dict | None:
    """Normalise one HERE browse API result to our standard POI dict."""
    title = (item.get("title") or "").strip()
    if not title:
        return None

    pos  = item.get("position") or {}
    elat = pos.get("lat")
    elon = pos.get("lng")
    if elat is None or elon is None:
        return None

    cats     = item.get("categories") or []
    raw_kind = (cats[0].get("name") or "").lower() if cats else ""

    # Map to our kind string — strict whitelist.
    # Anything not explicitly mapped gets logged and passed through as generic.
    kind = _HERE_KIND_MAP.get(raw_kind)
    if kind is None:
        if raw_kind:
            print(f"[HERE] Unmapped category '{raw_kind}' for '{title}' — passing through as generic")
        kind = "generic"

    if kind in POI_KIND_EXCLUDE:
        return None

    addr_obj = item.get("address") or {}
    number   = addr_obj.get("houseNumber") or ""
    street   = addr_obj.get("street") or ""
    address  = f"{number} {street}".strip() if (number or street) else ""
    if not address:
        address = (addr_obj.get("label") or "").split(",")[0].strip()

    contacts = item.get("contacts") or [{}]
    phone    = ""
    website  = ""
    for ct in contacts:
        for ph in ct.get("phone", []):
            phone = ph.get("value", ""); break
        for wb in ct.get("www", []):
            website = wb.get("value", ""); break

    oh_list  = item.get("openingHours") or []
    oh_text  = ""
    if oh_list:
        oh      = oh_list[0]
        is_open = oh.get("isOpen")
        texts   = oh.get("text") or []
        status  = "Open now" if is_open else ("Closed" if is_open is False else "")
        oh_text = (status + (". " if status and texts else "") + "; ".join(texts)).strip()

    here_id = item.get("id") or ""

    dist_m  = int(item.get("distance") or dist_metres(ref_lat, ref_lon, elat, elon))
    bearing = compass_name(bearing_deg(ref_lat, ref_lon, elat, elon))

    explorable = kind in EXPLORABLE_KINDS
    label = (
        f"{title}"
        + (f", {kind}" if kind else "")
        + (f", {address}" if address else "")
        + f", {dist_m} metres {bearing}"
        + ("  Explorable." if explorable else "")
    )

    return {
        "name":          title,
        "label":         label,
        "lat":           elat,
        "lon":           elon,
        "dist":          dist_m,
        "bearing":       bearing,
        "addr":          address,
        "address":       addr_obj.get("label", ""),
        "street":        street,
        "number":        number,
        "phone":         phone,
        "website":       website,
        "opening_hours": oh_text,
        "here_id":       here_id,
        "kind":          kind,
        "osm_type":      "node",
        "osm_id":        0,
        "explorable":    explorable,
        "tags":          {},
        "source":        "here",
    }


def filter_pois_by_category(pois: list, category: str) -> list:
    """Filter a flat POI list to those matching *category*.

    Used by _fetch_pois to avoid re-querying Overpass when _all_pois
    already contains a full picture of the area.
    """
    if category == "all":
        return list(pois)
    result = []
    for p in pois:
        kind = p.get("kind", "").lower()
        mapped = _KIND_TO_CATEGORY.get(kind, "all")
        # "transport" category includes trains too
        if mapped == category:
            result.append(p)
        elif category == "transport" and mapped == "trains":
            result.append(p)
    return result


# Cache helpers
_POI_CACHE_VERSION = 3
_POI_CACHE_MAX_AGE_DAYS = 30


# ---------------------------------------------------------------------------
# Module-level cache helpers (stateless, path-based)
# ---------------------------------------------------------------------------

def _load_poi_cache(cache_path: str) -> dict:
    if not os.path.exists(cache_path):
        return {}
    try:
        with open(cache_path, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("_version") != _POI_CACHE_VERSION:
            return {}
        return data
    except Exception:
        return {}


def _save_poi_cache(cache_path: str, cache: dict) -> None:
    try:
        cache["_version"] = _POI_CACHE_VERSION
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except Exception:
        pass


def _cache_key(lat: float, lon: float, category: str, radius: int,
               source: str = "osm") -> str:
    return f"{round(lat, 2)}_{round(lon, 2)}_{category}_{radius}_{source}"


def _get_cached(cache: dict, key: str) -> list | None:
    entry = cache.get(key)
    if not isinstance(entry, dict):
        return None
    age_days = (time.time() - entry.get("ts", 0)) / 86400
    if age_days > _POI_CACHE_MAX_AGE_DAYS:
        return None
    return entry.get("pois")


def _set_cached(cache: dict, key: str, pois: list) -> None:
    cache[key] = {"ts": time.time(), "pois": pois}


# ---------------------------------------------------------------------------
# POI parsing helpers
# ---------------------------------------------------------------------------

def _parse_element(el: dict, lat: float, lon: float,
                   address_points: list) -> dict | None:
    """Parse one Overpass element into a POI dict, or return None to skip."""
    tags     = el.get("tags", {})
    osm_type = el.get("type", "node")
    osm_id   = el.get("id", 0)

    # Skip noisy transit sub-elements
    if tags.get("railway") == "platform":
        return None
    pt = tags.get("public_transport", "")
    if pt in ("stop_position", "stop_area"):
        return None

    kind = (tags.get("amenity") or tags.get("railway") or
            tags.get("leisure") or tags.get("tourism") or
            tags.get("shop") or tags.get("historic") or
            tags.get("public_transport") or tags.get("natural") or
            tags.get("man_made") or tags.get("highway") or "")
    kind = kind.replace("_", " ")

    if kind.lower() in POI_KIND_EXCLUDE:
        return None

    name = tags.get("name", "")
    if not name:
        name = (tags.get("brand") or tags.get("brand:name") or
                tags.get("operator") or tags.get("official_name") or
                tags.get("short_name") or "")
        # Only keep inferred name for venue-type kinds
        if name and kind.lower() not in UNNAMED_OK_KINDS:
            name = ""

    if not name and not kind:
        return None
    if not name and kind.lower() not in UNNAMED_OK_KINDS:
        return None

    elat = el.get("lat") or (el.get("center") or {}).get("lat", 0)
    elon = el.get("lon") or (el.get("center") or {}).get("lon", 0)
    if not elat:
        return None

    dist_m  = int(dist_metres(lat, lon, elat, elon))
    bearing = compass_name(bearing_deg(lat, lon, elat, elon))

    # Address: OSM tags first, then nearest address point
    addr_parts = []
    for field in ("addr:housenumber", "addr:street",
                  "addr:suburb", "addr:city", "addr:state"):
        val = tags.get(field)
        if val and val not in addr_parts:
            addr_parts.append(val)
    address = ", ".join(addr_parts)

    if not address and address_points:
        best_addr = None
        best_d    = float("inf")
        for ap in address_points:
            d = dist_metres(elat, elon, ap["lat"], ap["lon"])
            if d < best_d and d < 80:
                best_d = d
                best_addr = ap
        if best_addr:
            address = f"{best_addr['number']} {best_addr['street']}"

    explorable = osm_type in ("way", "relation") and kind.lower() in EXPLORABLE_KINDS
    lead  = name if name else kind.title()
    label = (
        f"{lead}, {kind}"
        + (f", {address}" if address else "")
        + f", {dist_m} metres {bearing}"
        + ("  Explorable." if explorable else "")
    )

    return {
        "label":    label,
        "lat":      elat,
        "lon":      elon,
        "dist":     dist_m,
        "addr":     address,
        "street":   tags.get("addr:street", ""),
        "number":   tags.get("addr:housenumber", ""),
        "osm_type": osm_type,
        "osm_id":   osm_id,
        "explorable": explorable,
        "kind":     kind.lower(),
        "tags":     tags,
    }


def _parse_background_element(el: dict, lat: float, lon: float,
                               address_points: list) -> dict | None:
    """Like _parse_element but returns the slimmer background-POI format."""
    tags     = el.get("tags", {})
    osm_type = el.get("type", "node")
    osm_id   = el.get("id", 0)

    if tags.get("railway") == "platform":
        return None
    pt = tags.get("public_transport", "")
    if pt in ("stop_position", "stop_area"):
        return None

    kind = (tags.get("amenity") or tags.get("shop") or
            tags.get("railway") or tags.get("leisure") or
            tags.get("tourism") or tags.get("public_transport") or "")
    kind = kind.replace("_", " ")

    if kind.lower() in POI_KIND_EXCLUDE:
        return None

    name = tags.get("name", "")
    if not name:
        name = (tags.get("brand") or tags.get("operator") or
                tags.get("official_name") or "")
    if not name and not kind:
        return None
    if not name and kind.lower() not in UNNAMED_OK_KINDS:
        return None

    elat = el.get("lat") or (el.get("center") or {}).get("lat", 0)
    elon = el.get("lon") or (el.get("center") or {}).get("lon", 0)
    if not elat:
        return None

    dist_m  = int(dist_metres(lat, lon, elat, elon))
    bearing = compass_name(bearing_deg(lat, lon, elat, elon))

    addr_parts = []
    for field in ("addr:housenumber", "addr:street"):
        val = tags.get(field)
        if val and val not in addr_parts:
            addr_parts.append(val)
    address = " ".join(addr_parts)

    if not address and address_points:
        best_addr = None
        best_d    = float("inf")
        for ap in address_points:
            d = dist_metres(elat, elon, ap["lat"], ap["lon"])
            if d < best_d and d < 80:
                best_d = d
                best_addr = ap
        if best_addr:
            address = f"{best_addr['number']} {best_addr['street']}"

    explorable   = osm_type in ("way", "relation") and kind.lower() in EXPLORABLE_KINDS
    display_name = name or kind.title()
    label = (
        f"{display_name}, {kind}"
        + (f", {address}" if address else "")
        + f", {dist_m} metres {bearing}"
        + ("  Explorable." if explorable else "")
    )

    return {
        "name":      display_name,
        "kind":      kind.lower(),
        "address":   address,
        "addr":      address,
        "street":    tags.get("addr:street", ""),
        "number":    tags.get("addr:housenumber", ""),
        "lat":       elat,
        "lon":       elon,
        "dist":      dist_m,
        "bearing":   bearing,
        "osm_type":  osm_type,
        "osm_id":    osm_id,
        "explorable": explorable,
        "tags":      tags,
        "label":     label,
    }


# ---------------------------------------------------------------------------
# PoiFetcher
# ---------------------------------------------------------------------------

class PoiFetcher:
    """Pure data fetcher — no wx, no MapNavigator state.

    Parameters
    ----------
    overpass:
        The shared OverpassClient instance.
    cache_path:
        Full path to the POI JSON cache file (e.g. BASE_DIR/poi_cache.json).
    """

    def __init__(self, overpass, cache_path: str,
                 here_api_key: str = "") -> None:
        self._overpass   = overpass
        self._cache_path = cache_path
        self._here_api_key = here_api_key.strip()

    def set_here_key(self, key: str) -> None:
        """Update the HERE API key at runtime (called after settings saved)."""
        self._here_api_key = (key or "").strip()

    def _fetch_here(
        self,
        lat: float,
        lon: float,
        radius: int,
        category: str = "all",
        limit: int = 100,
        address_points: list | None = None,
    ) -> list:
        """Call HERE browse API and return normalised POI list."""
        address_points = address_points or []
        params: dict = {
            "at":     f"{lat},{lon}",
            "in":     f"circle:{lat},{lon};r={radius}",
            "limit":  limit,
            "apiKey": self._here_api_key,
            "lang":   "en",
            "sortBy": "distance",
        }
        if category != "all" and category in _HERE_CATEGORIES:
            params["categories"] = _HERE_CATEGORIES[category]

        url = _HERE_BROWSE_URL + "?" + urllib.parse.urlencode(params, safe=",")
        req = urllib.request.Request(url, headers={"User-Agent": "MapInABox/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
        except Exception as e:
            print(f"[HERE] Request failed: {e}")
            return []

        seen: set = set()
        pois: list = []
        for item in data.get("items", []):
            poi = _parse_here_item(item, lat, lon, address_points)
            if poi is None:
                continue
            dedup = f"{poi['name'].lower()}|{poi['kind']}"
            if dedup in seen:
                continue
            seen.add(dedup)
            pois.append(poi)

        print(f"[HERE] Got {len(pois)} places "
              f"(category={category}, radius={radius}m)")
        return pois

    def fetch_google_pois(
        self,
        lat: float,
        lon: float,
        api_key: str,
        category_key: str = "all",
        radius: int = 1000,
        name_filter: str = "",
    ) -> list[dict]:
        """Fetch POIs from Google Places in the standard Map in a Box POI format."""
        api_key = (api_key or "").strip()
        if not api_key:
            return []
        category_key = (category_key or "all").lower()
        name_filter = (name_filter or "").strip()

        cache = _load_poi_cache(self._cache_path)
        cache_category = (
            category_key if not name_filter
            else f"{category_key}:{name_filter.lower()}"
        )
        cache_key = _cache_key(lat, lon, cache_category, radius, "google")
        cached = _get_cached(cache, cache_key)
        if cached is not None:
            print(f"[Google POI] Cache hit — {len(cached)} results for '{category_key}'")
            return cached

        category_type: dict[str, str | list[str] | None] = {
            "food": "food",
            "shopping": "store",
            "transport": "transit_station",
            "trains": "train_station",
            "health": "health",
            "community": "local_government_office",
            "arts": ["tourist_attraction", "museum", "art_gallery",
                     "movie_theater", "stadium"],
            "parks": "park",
            "accommodation": "lodging",
            "all": None,
        }
        google_type_to_kind: dict[str, str] = {
            "restaurant": "restaurant", "cafe": "cafe", "bakery": "bakery",
            "bar": "bar", "night_club": "bar", "meal_takeaway": "fast food",
            "meal_delivery": "fast food", "food": "food",
            "supermarket": "supermarket", "grocery_or_supermarket": "supermarket",
            "convenience_store": "convenience", "store": "shop",
            "shopping_mall": "mall", "department_store": "department store",
            "clothing_store": "clothes", "shoe_store": "shoes",
            "book_store": "books", "electronics_store": "electronics",
            "hardware_store": "hardware", "furniture_store": "furniture",
            "pet_store": "pet shop", "florist": "florist",
            "pharmacy": "pharmacy", "hospital": "hospital",
            "doctor": "doctors", "dentist": "dentist", "clinic": "clinic",
            "health": "health",
            "train_station": "station", "subway_station": "station",
            "transit_station": "transit station", "bus_station": "bus station",
            "light_rail_station": "tram stop",
            "park": "park", "natural_feature": "park",
            "library": "library", "school": "school", "university": "university",
            "place_of_worship": "place of worship",
            "local_government_office": "community centre",
            "post_office": "post office", "bank": "bank", "atm": "atm",
            "police": "police", "fire_station": "fire station",
            "museum": "museum", "art_gallery": "gallery",
            "tourist_attraction": "attraction", "movie_theater": "cinema",
            "gym": "fitness centre", "stadium": "stadium",
            "lodging": "hotel", "gas_station": "petrol station",
        }

        place_type = category_type.get(category_key)
        if place_type is None and category_key not in category_type:
            return []

        try:
            google_places: list[dict] = []
            seen_place_ids: set[str] = set()
            if name_filter:
                params: dict = {
                    "query": name_filter,
                    "location": f"{lat},{lon}",
                    "radius": radius,
                    "key": api_key,
                }
                if isinstance(place_type, str) and place_type:
                    params["type"] = place_type
                req = urllib.request.Request(
                    "https://maps.googleapis.com/maps/api/place/textsearch/json"
                    "?" + urllib.parse.urlencode(params),
                    headers={"User-Agent": "MapInABox/1.0"},
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode())
                status = data.get("status", "")
                if status not in ("OK", "ZERO_RESULTS"):
                    print(f"[Google POI] API error: {status} — {data.get('error_message','')}")
                    return []
                google_places = data.get("results", [])[:25]
            else:
                place_types = place_type if isinstance(place_type, list) else [place_type]
                for one_type in place_types:
                    params: dict = {
                        "location": f"{lat},{lon}",
                        "radius": radius,
                        "key": api_key,
                    }
                    if one_type:
                        params["type"] = one_type
                    req = urllib.request.Request(
                        "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
                        "?" + urllib.parse.urlencode(params),
                        headers={"User-Agent": "MapInABox/1.0"},
                    )
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        data = json.loads(resp.read().decode())
                    status = data.get("status", "")
                    if status not in ("OK", "ZERO_RESULTS"):
                        print(f"[Google POI] API error: {status} — {data.get('error_message','')}")
                        continue
                    for place in data.get("results", [])[:25]:
                        place_id = place.get("place_id") or ""
                        dedup = place_id or f"{place.get('name','')}|{place.get('vicinity','')}"
                        if dedup in seen_place_ids:
                            continue
                        seen_place_ids.add(dedup)
                        google_places.append(place)

            results = []
            for place in google_places[:50]:
                try:
                    name = (place.get("name") or "").strip()
                    if not name:
                        continue
                    loc = place.get("geometry", {}).get("location", {})
                    plat = loc.get("lat")
                    plon = loc.get("lng")
                    if plat is None or plon is None:
                        continue
                    dist_m = int(math.sqrt(
                        ((plat - lat) * 111000) ** 2 +
                        ((plon - lon) * 111000 *
                         math.cos(math.radians(lat))) ** 2
                    ))
                    bearing = compass_name(bearing_deg(lat, lon, plat, plon))
                    types = place.get("types") or []
                    kind = next(
                        (google_type_to_kind[t] for t in types
                         if t in google_type_to_kind),
                        types[0].replace("_", " ") if types else "place",
                    )
                    vicinity = (place.get("vicinity") or "").strip()
                    addr = vicinity.split(",")[0].strip() if vicinity else ""
                    explorable = kind in EXPLORABLE_KINDS
                    label = (
                        f"{name}"
                        + (f", {kind}" if kind else "")
                        + (f", {addr}" if addr else "")
                        + f", {dist_m} metres {bearing}"
                        + ("  Explorable." if explorable else "")
                    )
                    results.append({
                        "name": name,
                        "label": label,
                        "lat": round(plat, 6),
                        "lon": round(plon, 6),
                        "dist": dist_m,
                        "bearing": bearing,
                        "addr": addr,
                        "street": addr,
                        "number": "",
                        "kind": kind,
                        "osm_type": "node",
                        "osm_id": 0,
                        "explorable": explorable,
                        "tags": {},
                        "source": "google",
                    })
                except Exception as exc:
                    print(f"[Google POI] Parse error: {exc}")
                    continue

            print(f"[Google POI] {len(results)} results for '{category_key}' within {radius}m")
            _set_cached(cache, cache_key, results)
            _save_poi_cache(self._cache_path, cache)
            return results
        except Exception as exc:
            print(f"[Google POI] Fetch error: {exc}")
            return []

    # ------------------------------------------------------------------
    # Primary user-triggered POI fetch
    # ------------------------------------------------------------------

    def fetch_pois(
        self,
        lat: float,
        lon: float,
        category: str = "all",
        radius: int = 300,
        timeout: int = 20,
        address_points: list | None = None,
    ) -> tuple[list, bool]:
        """Fetch POIs for *category* within *radius* metres of (lat, lon).

        Returns
        -------
        (pois, from_cache)
            *pois* is a list of POI dicts sorted by distance (or shopping
            priority for the shopping category).
            *from_cache* is True if the result came from disk cache.
        """
        address_points = address_points or []
        category = (category or "all").lower()
        source   = "here" if self._here_api_key else "osm"

        cache      = _load_poi_cache(self._cache_path)
        key        = _cache_key(lat, lon, category, radius, source)
        cached     = _get_cached(cache, key)
        if cached is not None:
            return cached, True

        queries    = _CATEGORY_QUERIES.get(category, _CATEGORY_QUERIES["all"])
        query_body = "\n  ".join(
            q.format(lat=lat, lon=lon, radius=radius) for q in queries)
        query = (
            f"[out:json][timeout:{timeout}];\n"
            f"(\n  {query_body}\n);\n"
            "out body center 300;\n"
        )

        print(f"[POI] Fetching {category} radius={radius}m timeout={timeout}s")

        # ── HERE path ────────────────────────────────────────────────────
        # If HERE is explicitly chosen (key is set), always return its result —
        # even if empty.  Do NOT silently fall through to Overpass; that would
        # ignore the user's poi_source preference.
        if self._here_api_key:
            pois = self._fetch_here(lat, lon, radius, category,
                                    address_points=address_points)
            if category == "shopping":
                pois.sort(key=lambda x: (
                    SHOPPING_PRIORITY.get(x.get("kind", ""), 50),
                    x["dist"]))
            else:
                pois.sort(key=lambda x: x["dist"])
            _set_cached(cache, key, pois)
            _save_poi_cache(self._cache_path, cache)
            return pois, False

        # ── OSM / Overpass path ──────────────────────────────────────────
        data   = urllib.parse.urlencode({"data": query}).encode()
        result = self._overpass.poi_request(data, timeout=timeout + 5)
        if result is None:
            raise RuntimeError("All Overpass mirrors failed for POI query")

        seen_keys: set = set()
        pois: list = []
        for el in result.get("elements", []):
            poi = _parse_element(el, lat, lon, address_points)
            if poi is None:
                continue
            dedup = (f"{poi['label'].split(',')[0]}|{poi['kind']}"
                     if poi.get("label", "").split(",")[0]
                     else f"{round(poi['lat'],3)}|{round(poi['lon'],3)}")
            if dedup in seen_keys:
                continue
            seen_keys.add(dedup)
            pois.append(poi)

        if category == "shopping":
            pois.sort(key=lambda x: (SHOPPING_PRIORITY.get(x.get("kind", ""), 50),
                                      x["dist"]))
        else:
            pois.sort(key=lambda x: x["dist"])

        _set_cached(cache, key, pois)
        _save_poi_cache(self._cache_path, cache)

        print(f"[POI] Got {len(pois)} pois at radius={radius}m")
        return pois, False

    def fetch_osm_name_search(
        self,
        lat: float,
        lon: float,
        name_filter: str,
        radius: int = 3000,
        timeout: int = 25,
        address_points: list | None = None,
    ) -> list:
        """Broader OSM search used when category fetches miss a named place."""
        address_points = address_points or []
        query_text = (name_filter or "").strip()
        if not query_text:
            return []

        pattern = re.escape(query_text)
        ar = f"(around:{radius},{lat},{lon})"
        query = (
            f"[out:json][timeout:{timeout}];\n"
            "(\n"
            f'  nwr["name"~"{pattern}",i]{ar};\n'
            f'  nwr["brand"~"{pattern}",i]{ar};\n'
            f'  nwr["operator"~"{pattern}",i]{ar};\n'
            f'  nwr["official_name"~"{pattern}",i]{ar};\n'
            ");\n"
            "out body center 300;\n"
        )
        print(f"[POI] OSM broad name search '{query_text}' radius={radius}m")
        data = urllib.parse.urlencode({"data": query}).encode()
        result = self._overpass.poi_request(data, timeout=timeout + 5)
        if result is None:
            return []

        pois = []
        seen_keys: set = set()
        for el in result.get("elements", []):
            tags = el.get("tags", {})
            # Avoid returning named roads as POIs. Transport stops still come
            # through via highway=bus_stop in normal category searches.
            if tags.get("highway") and not (
                    tags.get("amenity") or tags.get("shop")
                    or tags.get("tourism") or tags.get("leisure")
                    or tags.get("railway") or tags.get("public_transport")):
                continue
            poi = _parse_element(el, lat, lon, address_points)
            if poi is None:
                continue
            dedup_name = (poi.get("label", "").split(",")[0] or "").lower()
            dedup = f"{dedup_name}|{poi.get('kind','')}|{round(poi['lat'],5)}|{round(poi['lon'],5)}"
            if dedup in seen_keys:
                continue
            seen_keys.add(dedup)
            pois.append(poi)

        pois.sort(key=lambda x: x["dist"])
        print(f"[POI] OSM broad name search got {len(pois)} pois")
        return pois

    # ------------------------------------------------------------------
    # Background ambient POI fetch  (fires after road data loads)
    # ------------------------------------------------------------------

    def fetch_all_background(
        self,
        lat: float,
        lon: float,
        address_points: list | None = None,
    ) -> list:
        """Broad POI fetch used to populate self._all_pois for walk-announce.

        Returns a list of POI dicts (background format — includes 'name',
        'kind', 'address', 'bearing' fields used by walk announce).
        Results are cached for 30 days so HERE and Overpass are not
        hammered on every street mode entry.
        """
        address_points = address_points or []
        radius = POI_BACKGROUND_RADIUS_METRES
        source = "here" if self._here_api_key else "osm"

        # Check cache first — avoids redundant HERE/Overpass calls for
        # recently visited areas.  Key rounded to 1 decimal place (~11 km)
        # so any position within the same suburb hits the same cache entry.
        cache     = _load_poi_cache(self._cache_path)
        cache_key = _cache_key(round(lat, 1), round(lon, 1), "all_background", radius, source)
        cached    = _get_cached(cache, cache_key)
        if cached is not None:
            print(f"[POI] Background cache hit — {len(cached)} places.")

        # ── HERE path ────────────────────────────────────────────────────
        if self._here_api_key:
            buckets = [
                ("100",     "food"),
                ("600",     "shopping"),
                ("700",     "health"),
                ("800",     "community"),
                ("400-4100-0010,400-4100-0011,400-4100-0035,400-4300-0000", "transport"),
                ("300",     "parks"),
                ("500",     "accommodation"),
            ]
            seen: set = set()
            all_pois: list = []
            for cat_id, label in buckets:
                batch = self._fetch_here(
                    lat, lon, radius,
                    category=label,
                    limit=100,
                    address_points=address_points,
                )
                for poi in batch:
                    dedup = f"{poi['name'].lower()}|{poi['kind']}"
                    if dedup not in seen:
                        seen.add(dedup)
                        all_pois.append(poi)
            print(f"[POI] HERE background fetch complete: "
                  f"{len(all_pois)} places across {len(buckets)} buckets.")
            _set_cached(cache, cache_key, all_pois)
            _save_poi_cache(self._cache_path, cache)
            return all_pois

        # ── OSM / Overpass path ──────────────────────────────────────────
        query = (
            f"[out:json][timeout:15];\n(\n"
            f'  node["amenity"](around:{radius},{lat},{lon});\n'
            f'  node["shop"](around:{radius},{lat},{lon});\n'
            f'  node["leisure"](around:{radius},{lat},{lon});\n'
            f'  node["tourism"](around:{radius},{lat},{lon});\n'
            f'  way["amenity"](around:{radius},{lat},{lon});\n'
            f'  way["shop"](around:{radius},{lat},{lon});\n'
            f'  way["leisure"](around:{radius},{lat},{lon});\n'
            f'  way["tourism"](around:{radius},{lat},{lon});\n'
            f'  relation["amenity"~"mall|hospital|university|school|college"](around:{radius},{lat},{lon});\n'
            f'  node["railway"~"station|halt|tram_stop"](around:{radius},{lat},{lon});\n'
            f'  way["railway"~"station|halt|tram_stop"](around:{radius},{lat},{lon});\n'
            f'  relation["railway"~"station|halt"](around:{radius},{lat},{lon});\n'
            f'  node["public_transport"](around:{radius},{lat},{lon});\n'
            f'  node["highway"~"bus_stop"](around:{radius},{lat},{lon});\n'
            f'  nwr["amenity"~"bus_station|ferry_terminal"](around:{radius},{lat},{lon});\n'
            f'  node["public_transport"="station"]["train"="yes"](around:{radius},{lat},{lon});\n'
            ");\nout body center 300;\n"
        )
        data   = urllib.parse.urlencode({"data": query}).encode()
        result = self._overpass.poi_request(data, timeout=15)
        if not result:
            return []

        seen_keys: set = set()
        pois: list = []
        for el in result.get("elements", []):
            poi = _parse_background_element(el, lat, lon, address_points)
            if poi is None:
                continue
            dedup = (f"{poi['name']}|{poi['kind']}"
                     if poi["name"]
                     else f"{round(poi['lat'],3)}|{round(poi['lon'],3)}")
            if dedup in seen_keys:
                continue
            seen_keys.add(dedup)
            pois.append(poi)

        print(f"[POI] Background fetch complete: {len(pois)} places.")
        _set_cached(cache, cache_key, pois)
        _save_poi_cache(self._cache_path, cache)
        return pois

    def load_cached_pois(self, lat: float, lon: float) -> list | None:
        """Return cached background POIs for (lat, lon) if fresh, else None.

        Called silently after streets load so _all_pois can be pre-populated
        from disk without any network call.
        """
        cache = _load_poi_cache(self._cache_path)
        source = "here" if self._here_api_key else "osm"
        key = _cache_key(round(lat, 1), round(lon, 1), "all_background",
                         POI_BACKGROUND_RADIUS_METRES, source)
        cached = _get_cached(cache, key)
        if cached is not None:
            print(f"[POI] Background cache preload hit — {len(cached)} places.")
        else:
            print("[POI] Background cache preload miss.")
        return cached

    # ------------------------------------------------------------------
    # Intersection lookup for POI announcements
    # ------------------------------------------------------------------

    def nearest_cross_streets(
        self,
        lat: float,
        lon: float,
        road_segments: list,
        n: int = 2,
    ) -> list[str]:
        """Return up to *n* closest named road names to (lat, lon).

        Uses cached road segments if available; falls back to a lightweight
        Overpass query returning only tags (no geometry downloaded).
        """
        if road_segments:
            return self._cross_from_segments(lat, lon, road_segments, n)
        return self._cross_from_overpass(lat, lon, n)

    def _cross_from_segments(
        self,
        lat: float,
        lon: float,
        segments: list,
        n: int,
    ) -> list[str]:
        LOW = LOW_PRIORITY_HIGHWAY
        scored: dict[str, float] = {}
        for seg in segments:
            name = seg.get("name", "")
            kind = seg.get("kind", "")
            if not name or kind in LOW:
                continue
            clean = re.sub(r'\s*\(.*?\)', '', name).strip()
            if not clean or clean.lower() in GENERIC_STREET_TYPES:
                continue
            coords = seg["coords"]
            best_d = float("inf")
            for i in range(len(coords) - 1):
                alat, alon = coords[i]
                blat, blon = coords[i + 1]
                d = dist_to_segment_metres(lat, lon, alat, alon, blat, blon)
                best_d = min(best_d, d)
            if clean not in scored or best_d < scored[clean]:
                scored[clean] = best_d
        return sorted(scored, key=lambda x: scored[x])[:n]

    def _cross_from_overpass(self, lat: float, lon: float, n: int) -> list[str]:
        query = (
            "[out:json][timeout:25];\n"
            f'way["highway"~"primary|secondary|tertiary|residential|unclassified"]["name"]'
            f"(around:150,{lat},{lon});\n"
            "out tags bb;\n"
        )
        data   = urllib.parse.urlencode({"data": query}).encode()
        result = self._overpass.request(data, timeout=8)
        if not result:
            return []

        def bbox_dist(el: dict) -> float:
            b = el.get("bounds", {})
            if not b:
                return float("inf")
            clat = (b["minlat"] + b["maxlat"]) / 2
            clon = (b["minlon"] + b["maxlon"]) / 2
            return dist_metres(lat, lon, clat, clon)

        ways  = [el for el in result.get("elements", []) if el.get("type") == "way"]
        seen: set = set()
        names: list[str] = []
        for el in sorted(ways, key=bbox_dist):
            name = el.get("tags", {}).get("name", "")
            if name and name not in seen:
                seen.add(name)
                names.append(name)
            if len(names) == n:
                break
        return names

    # ------------------------------------------------------------------
    # Explore: fetch child POIs inside a building/venue
    # ------------------------------------------------------------------

    def fetch_explore_children(
        self,
        osm_type: str,
        osm_id: int,
        centre_lat: float,
        centre_lon: float,
    ) -> list[dict]:
        """Fetch the POIs inside an explorable venue (mall, hospital, etc).

        Returns a list of child POI dicts with label, lat, lon, dist, kind.
        """
        if osm_type == "way":
            area_filter = f"way({osm_id}) -> .parent; nwr(area.parent)"
        else:
            area_filter = f"rel({osm_id}) -> .parent; nwr(area.parent)"

        bbox_half = 0.005
        bmin_lat  = centre_lat - bbox_half
        bmax_lat  = centre_lat + bbox_half
        bmin_lon  = centre_lon - bbox_half
        bmax_lon  = centre_lon + bbox_half

        # First: relation/way membership query
        query1 = (
            "[out:json][timeout:60];\n"
            f"({area_filter}"
            '["shop"]);out body center;'
        )
        data1   = urllib.parse.urlencode({"data": query1}).encode()
        result1 = self._overpass.request(data1, timeout=65)
        elements = result1.get("elements", []) if result1 else []

        # Second: bbox fallback for anything not in the relation
        bb_query = (
            "[out:json][timeout:60];\n(\n"
            f'  node["shop"]({bmin_lat},{bmin_lon},{bmax_lat},{bmax_lon});\n'
            f'  node["amenity"]({bmin_lat},{bmin_lon},{bmax_lat},{bmax_lon});\n'
            f'  node["leisure"]({bmin_lat},{bmin_lon},{bmax_lat},{bmax_lon});\n'
            f'  node["healthcare"]({bmin_lat},{bmin_lon},{bmax_lat},{bmax_lon});\n'
            f'  node["office"]({bmin_lat},{bmin_lon},{bmax_lat},{bmax_lon});\n'
            f'  node["highway"="elevator"]({bmin_lat},{bmin_lon},{bmax_lat},{bmax_lon});\n'
            f'  node["vending"]({bmin_lat},{bmin_lon},{bmax_lat},{bmax_lon});\n'
            f'  way["shop"]({bmin_lat},{bmin_lon},{bmax_lat},{bmax_lon});\n'
            f'  way["amenity"]({bmin_lat},{bmin_lon},{bmax_lat},{bmax_lon});\n'
            f'  way["leisure"]({bmin_lat},{bmin_lon},{bmax_lat},{bmax_lon});\n'
            f'  way["healthcare"]({bmin_lat},{bmin_lon},{bmax_lat},{bmax_lon});\n'
            f'  way["office"]({bmin_lat},{bmin_lon},{bmax_lat},{bmax_lon});\n'
            ");\nout body center;\n"
        )
        data2   = urllib.parse.urlencode({"data": bb_query}).encode()
        result2 = self._overpass.request(data2, timeout=60)
        if result2:
            elements = result2.get("elements", [])

        UNNAMED_OK = frozenset({
            "elevator", "toilets", "atm", "vending machine",
            "main entrance", "parking", "telephone",
            "post box", "defibrillator",
        })

        seen: set = set()
        children: list = []

        for el in elements:
            tags = el.get("tags", {})
            name = tags.get("name", "")
            kind = (tags.get("shop") or tags.get("amenity") or
                    tags.get("craft") or tags.get("office") or
                    tags.get("healthcare") or tags.get("leisure") or
                    tags.get("tourism") or tags.get("cuisine") or
                    tags.get("place_of_worship") or tags.get("emergency") or
                    tags.get("social_facility") or tags.get("club") or
                    tags.get("vending") or "")

            # Special-case unnamed venue types
            if not name and tags.get("shop") == "mall":
                name = (tags.get("brand") or tags.get("brand:name") or
                        tags.get("operator") or tags.get("official_name") or
                        tags.get("short_name") or "")
            if not kind:
                if tags.get("highway") == "elevator":
                    kind = "elevator"
                elif tags.get("entrance") == "main":
                    kind = "main entrance"

            kind = kind.replace("_", " ")

            if not name and not kind:
                continue
            if not name:
                if kind.lower() in {"mall", "shopping centre", "department store",
                                    "supermarket", "marketplace"}:
                    name = (tags.get("brand") or tags.get("brand:name") or
                            tags.get("operator") or tags.get("official_name") or
                            tags.get("short_name") or "")
                if not name:
                    if kind.lower() not in UNNAMED_OK:
                        continue
                    name = kind.title()

            elat = (el.get("lat") or
                    (el.get("center") or {}).get("lat") or
                    centre_lat)
            elon = (el.get("lon") or
                    (el.get("center") or {}).get("lon") or
                    centre_lon)

            dist_m  = int(dist_metres(centre_lat, centre_lon, elat, elon))
            bearing = compass_name(bearing_deg(centre_lat, centre_lon, elat, elon))

            label = f"{name}" + (f", {kind}" if kind else "") + f", {dist_m} metres {bearing}"
            dedup = f"{name.lower()}|{kind.lower()}"
            if dedup in seen:
                continue
            seen.add(dedup)
            children.append({
                "label":    label,
                "lat":      elat,
                "lon":      elon,
                "dist":     dist_m,
                "addr":     "",
                "street":   "",
                "number":   "",
                "osm_type": el.get("type", "node"),
                "osm_id":   el.get("id", 0),
                "kind":     kind.lower(),
            })

        children.sort(key=lambda x: x["dist"])
        return children
