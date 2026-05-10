"""street_data.py — Road segment fetching and street-name queries for Map in a Box.

All Overpass queries related to road/street data live here.
No wx imports, no MapNavigator state mutation — every method takes its
inputs as arguments and returns plain data.

MapNavigator holds a StreetFetcher instance and is responsible for:
  - calling these methods on background threads
  - storing results in self._road_segments / self._address_points
  - calling wx.CallAfter to update the UI with results

Classes
-------
StreetFetcher
    fetch_road_data(lat, lon, radius) → (segments, addresses) | raises
    nearest_road(lat, lon, segments) → (primary_name, cross_name | None)
    nearest_roads_with_distances(lat, lon, segments) → list[(name, distance_m)]
    street_names_from_segments(segments) → list[str]
"""

import json
import os
import re
import time
import urllib.parse
import urllib.request

from geo import (
    dist_to_segment_metres,
    GENERIC_STREET_TYPES, LOW_PRIORITY_HIGHWAY,
)

# ---------------------------------------------------------------------------
# Geocoding cache (365 day expiry)
# ---------------------------------------------------------------------------

_GEOCODE_CACHE_FILE = None  # Set by init_geocode_cache()
_GEOCODE_REMOTE_URL = None  # Disabled - don't rely on external server
_GEOCODE_CACHE_DAYS = 365

_ADMIN_REGION_FIELDS = (
    "state",
    "province",
    "state_district",
    "region",
    "county",
    "prefecture",
    "department",
    "district",
)

def init_geocode_cache(cache_dir: str):
    """Initialize geocoding cache file path."""
    global _GEOCODE_CACHE_FILE
    _GEOCODE_CACHE_FILE = os.path.join(cache_dir, "geocode_cache.json")

def _load_geocode_cache() -> dict:
    """Load geocoding cache from disk."""
    if not _GEOCODE_CACHE_FILE or not os.path.exists(_GEOCODE_CACHE_FILE):
        return {}
    try:
        with open(_GEOCODE_CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_geocode_cache(cache: dict):
    """Save geocoding cache to disk."""
    if not _GEOCODE_CACHE_FILE:
        return
    try:
        os.makedirs(os.path.dirname(_GEOCODE_CACHE_FILE), exist_ok=True)
        with open(_GEOCODE_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2)
    except Exception as e:
        print(f"[Geocode] Failed to save cache: {e}")

def geocode_location(lat: float, lon: float) -> dict | None:
    """Geocode lat/lon to suburb, bbox, and radius.
    
    Returns dict with keys: suburb, bbox (tuple), radius, country_code
    Checks: 1) local cache, 2) samtaylor9, 3) Nominatim
    Cache expires after 365 days.
    """
    cache_key = f"{round(lat, 3):.3f}_{round(lon, 3):.3f}"

    # Check local cache first (365 day expiry)
    cache = _load_geocode_cache()
    if cache_key in cache:
        entry = cache[cache_key]
        age_days = (time.time() - entry.get("timestamp", 0)) / 86400
        if age_days < _GEOCODE_CACHE_DAYS:
            print(f"[Geocode] Cache hit (age: {age_days:.0f} days): {entry.get('suburb')}")
            return {
                "suburb": entry.get("suburb"),
                "bbox": tuple(entry.get("bbox", [])) if entry.get("bbox") else None,
                "radius": entry.get("radius"),
                "country_code": entry.get("country_code"),
            }
    
    # Try remote cache (if configured)
    if _GEOCODE_REMOTE_URL:
        try:
            print(f"[Geocode] Checking remote cache: {_GEOCODE_REMOTE_URL}")
            req = urllib.request.Request(_GEOCODE_REMOTE_URL, 
                                         headers={"User-Agent": "MapInABox/1.0"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                remote_cache = json.loads(resp.read().decode())
            if cache_key in remote_cache:
                entry = remote_cache[cache_key]
                print(f"[Geocode] Remote cache hit: {entry.get('suburb')}")
                # Save to local cache
                cache[cache_key] = {
                    **entry,
                    "timestamp": time.time()
                }
                _save_geocode_cache(cache)
                return {
                    "suburb": entry.get("suburb"),
                    "bbox": tuple(entry.get("bbox", [])) if entry.get("bbox") else None,
                    "radius": entry.get("radius"),
                    "country_code": entry.get("country_code"),
                }
        except Exception as e:
            print(f"[Geocode] Remote cache failed: {e}")
    
    # Fall back to Nominatim
    try:
        print(f"[Geocode] Querying Nominatim for {lat:.4f},{lon:.4f}")
        params = urllib.parse.urlencode({
            "lat": lat, "lon": lon,
            "format": "json", "zoom": 14, "addressdetails": 1,
        })
        req = urllib.request.Request(
            f"https://nominatim.openstreetmap.org/reverse?{params}",
            headers={"User-Agent": "MapInABox/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
        
        # Always use fixed 3000m radius for consistent coverage
        import math
        radius = 3000
        
        bb = data.get("boundingbox")
        if bb:
            minlat, maxlat, minlon, maxlon = map(float, bb)
        else:
            # No bbox - create one from radius
            minlat = lat - (radius / 111000)
            maxlat = lat + (radius / 111000)
            minlon = lon - (radius / (111000 * math.cos(math.radians(lat))))
            maxlon = lon + (radius / (111000 * math.cos(math.radians(lat))))
        
        addr = data.get("address", {})
        suburb = (addr.get("city_district") or addr.get("suburb") or
                  addr.get("town") or addr.get("village") or
                  addr.get("municipality") or addr.get("city", "this area"))
        country_code = addr.get("country_code", "")
        
        # Save to cache
        cache[cache_key] = {
            "suburb": suburb,
            "bbox": [minlat, maxlat, minlon, maxlon] if bb else None,
            "radius": radius,
            "country_code": country_code,
            "timestamp": time.time()
        }
        _save_geocode_cache(cache)
        print(f"[Geocode] Nominatim success, cached: {suburb}")
        
        return {
            "suburb": suburb,
            "bbox": tuple([minlat, maxlat, minlon, maxlon]) if bb else None,
            "radius": radius,
            "country_code": country_code,
        }
    except Exception as e:
        print(f"[Geocode] Nominatim failed: {e}")
        return None


def reverse_geocode_region(lat: float, lon: float) -> dict | None:
    """Reverse geocode to the nearest named admin region.

    Returns a dict with region_name, region_type, country, country_code, and
    display_name. Unlike geocode_location(), this only reports administrative
    regions and does not fall back to localities.
    """
    cache_key = f"{round(lat, 3):.3f}_{round(lon, 3):.3f}"
    cache = _load_geocode_cache()
    entry = cache.get(cache_key, {})

    region_name = (entry.get("admin_region") or "").strip()
    region_type = (entry.get("admin_region_type") or "").strip()
    country = (entry.get("admin_country") or "").strip()
    country_code = (entry.get("admin_country_code") or "").strip()
    if region_name:
        display_name = region_name if not country else f"{region_name}, {country}"
        return {
            "region_name": region_name,
            "region_type": region_type,
            "country": country,
            "country_code": country_code,
            "display_name": display_name,
        }

    try:
        params = urllib.parse.urlencode({
            "lat": lat,
            "lon": lon,
            "format": "json",
            "zoom": 10,
            "addressdetails": 1,
        })
        req = urllib.request.Request(
            f"https://nominatim.openstreetmap.org/reverse?{params}",
            headers={"User-Agent": "MapInABox/1.0"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode())
        addr = data.get("address", {})

        region_name = ""
        region_type = ""
        for field in _ADMIN_REGION_FIELDS:
            val = str(addr.get(field, "")).strip()
            if val:
                region_name = val
                region_type = field
                break

        country = str(addr.get("country", "")).strip()
        country_code = str(addr.get("country_code", "")).strip()
        if not region_name:
            return None

        display_name = region_name if not country else f"{region_name}, {country}"
        cache[cache_key] = {
            **entry,
            "admin_region": region_name,
            "admin_region_type": region_type,
            "admin_country": country,
            "admin_country_code": country_code,
            "timestamp": time.time(),
        }
        _save_geocode_cache(cache)
        return {
            "region_name": region_name,
            "region_type": region_type,
            "country": country,
            "country_code": country_code,
            "display_name": display_name,
        }
    except Exception:
        return None

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ROAD_LABELS: dict[str, str] = {
    "footway":       "footpath",
    "cycleway":      "cycle path",
    "path":          "path",
    "steps":         "steps",
    "pedestrian":    "pedestrian area",
    "track":         "dirt track",
    "service":       "service road",
    "motorway":      "motorway",
    "trunk":         "highway",
    "primary":       "main road",
    "secondary":     "road",
    "tertiary":      "street",
    "residential":   "residential street",
    "unclassified":  "road",
    "living_street": "shared street",
    "bridleway":     "bridleway",
    "construction":  "road under construction",
}

_LOW_DETAIL = frozenset({
    "footway", "cycleway", "path", "steps", "track", "bridleway",
})

_CACHE_VERSION = 3
_CACHE_MAX_AGE_DAYS = 90


def _make_display(name: str, kind: str) -> str:
    human = ROAD_LABELS.get(kind, kind or "road")
    return f"{name} ({human})" if (name and kind in _LOW_DETAIL) else (name or human)


# ---------------------------------------------------------------------------
# Cache helpers — one JSON file per city in a road_cache/ folder
# Files are named by suburb/locality, looked up via an index.json
# ---------------------------------------------------------------------------

_INDEX_FILE = "index.json"


def _safe_name(s: str) -> str:
    """Convert a place name to a safe filename stem."""
    return re.sub(r'[^a-z0-9]+', '_', s.lower()).strip('_')[:40]


def _load_index(cache_dir: str) -> dict:
    """Load lat_lon → filename index. Returns {} on any failure."""
    path = os.path.join(cache_dir, _INDEX_FILE)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_index(cache_dir: str, index: dict) -> None:
    try:
        with open(os.path.join(cache_dir, _INDEX_FILE), "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _index_key(lat: float, lon: float, suburb_name: str = None, used_boundary: bool = False) -> str:
    """Generate cache key based on query type.
    
    Suburban boundary queries: Cache by suburb name (entire suburb cached once)
    Rural radius queries: Cache by ~10km grid (larger cells for better coverage)
    """
    if used_boundary and suburb_name:
        # Suburb-based cache: whole suburb cached together
        safe_name = suburb_name.lower().replace(" ", "_").replace("'", "")
        return f"suburb_{safe_name}"
    else:
        # Radius-based cache: larger grid cells for rural areas (1 decimal = ~10km)
        return f"{round(lat, 1):.1f}_{round(lon, 1):.1f}"


def _resolve_friendly_name(lat: float, lon: float) -> str:
    """Best-effort Nominatim reverse geocode → suburb_state string.
    Falls back to lat_lon on any failure."""
    try:
        import urllib.request as _ur, urllib.parse as _up
        params = _up.urlencode({
            "lat": round(lat, 4), "lon": round(lon, 4),
            "format": "json", "zoom": 14, "addressdetails": 1,
        })
        req = _ur.Request(
            f"https://nominatim.openstreetmap.org/reverse?{params}",
            headers={"User-Agent": "MapInABox/1.0"})
        with _ur.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode())
        addr = data.get("address", {})
        parts = []
        for field in ("suburb", "town", "city_district", "city", "state"):
            val = addr.get(field, "").strip()
            if val and val not in parts:
                parts.append(val)
                if len(parts) == 2:
                    break
        if parts:
            return "_".join(_safe_name(p) for p in parts)
    except Exception:
        pass
    return _index_key(lat, lon)


def _load_road_cache(cache_dir: str, lat: float, lon: float, 
                     suburb_name: str = None) -> dict:
    """Load cached road data. Tries suburb-based key first if available."""
    index = _load_index(cache_dir)
    
    # Try suburb-based cache first if we have a suburb name
    if suburb_name:
        suburb_key = _index_key(lat, lon, suburb_name, used_boundary=True)
        fname = index.get(suburb_key)
        if fname:
            path = os.path.join(cache_dir, fname)
            if os.path.exists(path):
                try:
                    with open(path, encoding="utf-8") as f:
                        data = json.load(f)
                    if data.get("_version") == _CACHE_VERSION:
                        return data
                except Exception:
                    pass
    
    # Fall back to coordinate-based cache
    key = _index_key(lat, lon)
    fname = index.get(key)
    
    # If exact cell misses, check adjacent grid cells (±0.1 degrees = ~11km)
    # Only use if cache center is within 7km (max radius coverage)
    if not fname:
        import math
        for dlat in [-0.1, 0, 0.1]:
            for dlon in [-0.1, 0, 0.1]:
                if dlat == 0 and dlon == 0:
                    continue  # Already checked exact cell
                adj_key = _index_key(lat + dlat, lon + dlon)
                fname = index.get(adj_key)
                if fname:
                    # Load and check if cache center is within range
                    path = os.path.join(cache_dir, fname)
                    if os.path.exists(path):
                        try:
                            with open(path, encoding="utf-8") as f:
                                data = json.load(f)
                            # Check cache metadata for center coordinates
                            cache_lat = data.get("cache_center_lat")
                            cache_lon = data.get("cache_center_lon")
                            if cache_lat is not None and cache_lon is not None:
                                # Calculate distance from target to cache center
                                dlat_m = (lat - cache_lat) * 111000
                                dlon_m = (lon - cache_lon) * 111000 * math.cos(math.radians(lat))
                                dist = math.sqrt(dlat_m**2 + dlon_m**2)
                                # Only use if within 7km (typical cache radius)
                                if dist < 7000:
                                    print(f"[Street] Found cache in adjacent cell {adj_key}, {dist:.0f}m from center")
                                    if data.get("_version") == _CACHE_VERSION:
                                        return data
                        except Exception:
                            pass
                    fname = None  # Reset if validation failed
    
    if not fname:
        return {}
    path = os.path.join(cache_dir, fname)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("_version") != _CACHE_VERSION:
            return {}
        return data
    except Exception:
        return {}


def _save_road_cache(cache_dir: str, lat: float, lon: float, entry: dict,
                     suburb_name: str = None, used_boundary: bool = False) -> None:
    """Save road data to cache. Uses suburb name for boundary queries."""
    try:
        os.makedirs(cache_dir, exist_ok=True)
        key = _index_key(lat, lon, suburb_name, used_boundary)
        print(f"[Street] Saving cache to {cache_dir} key={key}")
        index = _load_index(cache_dir)
        # Reuse existing filename if already indexed, else resolve a friendly name
        fname = index.get(key)
        if not fname:
            friendly = _resolve_friendly_name(lat, lon)
            fname = f"road_{friendly}.json"
            # Avoid collisions if two nearby keys resolve to the same name
            base = fname
            n = 1
            existing = set(index.values())
            while fname in existing:
                fname = base.replace(".json", f"_{n}.json")
                n += 1
            index[key] = fname
            _save_index(cache_dir, index)
        entry["_version"] = _CACHE_VERSION
        with open(os.path.join(cache_dir, fname), "w", encoding="utf-8") as f:
            json.dump(entry, f, ensure_ascii=False)
    except Exception as _e:
        print(f"[Street] Cache save FAILED: {_e}")


def _cache_is_stale(entry: dict) -> bool:
    age_days = (time.time() - entry.get("ts", 0)) / 86400
    return age_days > _CACHE_MAX_AGE_DAYS


# ---------------------------------------------------------------------------
# StreetFetcher
# ---------------------------------------------------------------------------



class StreetFetcher:
    """Pure data fetcher — no wx, no MapNavigator state.

    Parameters
    ----------
    overpass:
        The shared OverpassClient instance.
    cache_path:
        Full path to the road JSON cache file (e.g. BASE_DIR/road_cache.json).
    """

    def __init__(self, overpass, cache_path: str) -> None:
        self._overpass   = overpass
        # Accept either a .json path (legacy) or a directory path
        if cache_path.endswith(".json"):
            self._cache_dir = os.path.splitext(cache_path)[0] + "_dir"
        else:
            self._cache_dir = cache_path
        # Always create the cache directory on startup so saves never fail
        # due to a missing parent.
        os.makedirs(self._cache_dir, exist_ok=True)
        # Initialize geocoding cache
        init_geocode_cache(self._cache_dir)

    # ------------------------------------------------------------------
    # Road segment fetch  (called when entering street mode)
    # ------------------------------------------------------------------

    def fetch_road_data(
        self,
        lat: float,
        lon: float,
        radius: int = 800,
        fetch_lat: float | None = None,
        fetch_lon: float | None = None,
        status_cb=None,
        suburb_name: str | None = None,
        country_code: str | None = None,
    ) -> tuple[list, list, bool, float | None, float | None]:
        """Fetch road segments and address points for the area around (lat, lon).

        Parameters
        ----------
        lat, lon:
            Current player position.
        radius:
            Overpass search radius in metres.
        fetch_lat, fetch_lon:
            If provided, fetch is centred here instead of lat/lon.
        status_cb:
            Optional callable(str) for progress messages.

        Returns
        -------
        (segments, addresses, from_cache, snap_lat, snap_lon)
        """
        def status(msg):
            if status_cb:
                status_cb(msg)
            print(f"[Street] {msg}")

        centre_lat = fetch_lat or lat
        centre_lon = fetch_lon or lon
        snap       = (fetch_lat, fetch_lon) if (fetch_lat and fetch_lon) else (None, None)

        entry = _load_road_cache(self._cache_dir, centre_lat, centre_lon, suburb_name)

        if entry:
            segs  = entry.get("segments", [])
            addrs = entry.get("addresses", [])
            natural_features = entry.get("natural_features", [])
            interpolations = entry.get("interpolations", [])
            stale = _cache_is_stale(entry)
            if not stale:
                print(f"[Street] cache hit — {len(segs)} segments, {len(natural_features)} natural features, {len(interpolations)} interpolations")
                return segs, addrs, True, snap[0], snap[1], False, natural_features, interpolations
            else:
                # Stale but usable — return immediately, refresh in background
                print(f"[Street] stale cache — {len(segs)} segments, {len(natural_features)} natural features, {len(interpolations)} interpolations, serving now, refreshing background")
                # Kick off background refresh without blocking
                import threading as _threading
                def _bg_refresh():
                    try:
                        self._live_fetch(centre_lat, centre_lon, radius)
                    except Exception:
                        pass
                _threading.Thread(target=_bg_refresh, daemon=True).start()
                return segs, addrs, True, snap[0], snap[1], False, natural_features, interpolations

        # No fresh cache — try live fetch, fall back to stale if all mirrors fail.
        stale_entry = entry
        try:
            return self._live_fetch(centre_lat, centre_lon, radius,
                                    snap=snap, status_cb=status_cb,
                                    suburb_name=suburb_name,
                                    country_code=country_code)
        except RuntimeError:
            if stale_entry:
                segs  = stale_entry.get("segments", [])
                addrs = stale_entry.get("addresses", [])
                natural_features = stale_entry.get("natural_features", [])
                interpolations = stale_entry.get("interpolations", [])
                status("All servers timed out — using cached streets (may be outdated).")
                return segs, addrs, True, snap[0], snap[1], False, natural_features, interpolations
            raise

    def _live_fetch(
        self,
        centre_lat: float,
        centre_lon: float,
        radius: int,
        snap: tuple = (None, None),
        status_cb=None,
        suburb_name: str | None = None,
        country_code: str | None = None,
    ) -> tuple:
        """Fetch streets using OSM admin boundary if available, else radius.

        Primary: query streets within the named suburb admin boundary — this
        respects the actual suburb shape so peninsula/coastal suburbs only
        get their own streets, not neighbours.
        Fallback: radius query if no boundary found in OSM.

        Returns (segs, addrs, False, snap_lat, snap_lon).
        """
        def status(msg):
            if status_cb:
                status_cb(msg)
            print(f"[Street] {msg}")

        print(f"[Street] Fetching at centre: {centre_lat:.5f}, {centre_lon:.5f} radius {radius}m suburb={suburb_name!r}")
        # status_cb set on overpass only for radius fallback — boundary loop
        # announces once via status() and suppresses per-server messages to avoid double-speak.
        self._overpass.status_cb = None

        result = None
        used_boundary = False

        # Boundary-only mode with radius fallback
        skip_boundary = False

        # ── Name-based boundary query ─────────────────────────────────
        if suburb_name and not skip_boundary:
            status(f"Loading streets for {suburb_name}...")
            import math as _math
            _deg_lat = radius / 111000.0
            _deg_lon = radius / (111000.0 * _math.cos(_math.radians(centre_lat)))
            bbox_str = (f"{centre_lat - _deg_lat:.5f},{centre_lon - _deg_lon:.5f},"
                        f"{centre_lat + _deg_lat:.5f},{centre_lon + _deg_lon:.5f}")
            for area_filter in (
                f'area["name"="{suburb_name}"]["boundary"="administrative"]->.a;',
                f'area["name"="{suburb_name}"]["place"~"suburb|town|village|municipality|locality|quarter|neighbourhood"]->.a;',
            ):
                boundary_query = (
                    f"[out:json][timeout:60][bbox:{bbox_str}];\n"
                    f"{area_filter}\n"
                    "(\n"
                    '  way["highway"~"primary|secondary|tertiary|residential|unclassified|living_street|trunk|motorway"](area.a);\n'
                    '  way["highway"~"footway|cycleway|path|service"]["name"](area.a);\n'
                    '  way["natural"~"water|wetland|wood|beach|scrub|grassland|heath"](area.a);\n'
                    '  way["waterway"~"river|stream|canal|drain"](area.a);\n'
                    '  way["leisure"~"park|nature_reserve|recreation_ground"](area.a);\n'
                    '  way["landuse"~"farmland|orchard|vineyard|meadow|forest|grass|quarry"](area.a);\n'
                    '  way["barrier"~"fence|hedge|gate"](area.a);\n'
                    '  way["addr:interpolation"](area.a);\n'  # Address ranges
                    ");\n"
                    "out geom;\n"
                    "(\n"
                    "  way._[\"addr:interpolation\"];\n"
                    "  node(w)[\"addr:housenumber\"];\n"  # Endpoints with house numbers
                    ");\n"
                    "out;\n"
                )
                data   = urllib.parse.urlencode({"data": boundary_query}).encode()
                result = self._overpass.large_request(data, timeout=60)
                if result and result.get("elements"):
                    used_boundary = True
                    print(f"[Street] Name-based query succeeded for {suburb_name!r}: {len(result['elements'])} ways")
                    break
                else:
                    print(f"[Street] Name-based query returned nothing for {suburb_name!r}, trying next...")
                    result = None

        # ── Radius fallback if boundary failed ────────────────────────
        if not used_boundary:
            self._overpass.status_cb = status_cb  # announce server only for fallback
            if suburb_name:
                print(f"[Street] No boundary found for {suburb_name}, trying radius fallback...")
            else:
                print(f"[Street] No suburb name, using radius query...")
            
            # Simple radius query like old version
            radius_query = (
                f"[out:json][timeout:30];\n(\n"
                f'  way["highway"~"primary|secondary|tertiary|residential|unclassified|living_street|trunk|motorway"](around:{radius},{centre_lat},{centre_lon});\n'
                f'  way["highway"~"footway|cycleway|path|service"]["name"](around:{radius},{centre_lat},{centre_lon});\n'
                f'  way["natural"~"water|wetland|wood|beach|scrub|grassland|heath"](around:{radius},{centre_lat},{centre_lon});\n'
                f'  way["waterway"~"river|stream|canal|drain"](around:{radius},{centre_lat},{centre_lon});\n'
                f'  way["leisure"~"park|nature_reserve|recreation_ground"](around:{radius},{centre_lat},{centre_lon});\n'
                f'  way["landuse"~"farmland|orchard|vineyard|meadow|forest|grass|quarry"](around:{radius},{centre_lat},{centre_lon});\n'
                f'  way["barrier"~"fence|hedge|gate"](around:{radius},{centre_lat},{centre_lon});\n'
                f'  way["addr:interpolation"](around:{radius},{centre_lat},{centre_lon});\n'  # Address ranges
                f");\n"
                f"out geom;\n"
                f"(\n"
                f"  way._[\"addr:interpolation\"];\n"
                f"  node(w)[\"addr:housenumber\"];\n"  # Endpoints
                f");\n"
                f"out;\n"
            )
            data = urllib.parse.urlencode({"data": radius_query}).encode()
            result = self._overpass.large_request(data, timeout=35)
            if result and result.get("elements"):
                print(f"[Street] Radius fallback succeeded: {len(result['elements'])} ways")
            else:
                print(f"[Street] Radius fallback also failed")

        self._overpass.status_cb = None
        if not result:
            raise RuntimeError("No street data available (both boundary and radius failed).")

        segments: list = []
        natural_features: list = []  # Store natural/landuse/leisure features
        interpolations: list = []  # Store address interpolation data
        nodes_dict: dict = {}  # Store nodes by ID for interpolation endpoints
        
        # First pass: collect all nodes (needed for interpolation endpoints)
        for el in result.get("elements", []):
            if el.get("type") == "node":
                node_id = el.get("id")
                if node_id:
                    nodes_dict[node_id] = {
                        "lat": el.get("lat"),
                        "lon": el.get("lon"),
                        "tags": el.get("tags", {})
                    }
        
        # Second pass: process ways
        for el in result.get("elements", []):
            if el.get("type") == "way":
                tags  = el.get("tags", {})
                geom  = el.get("geometry", [])
                if len(geom) < 2:
                    continue
                coords = [(pt["lat"], pt["lon"]) for pt in geom]
                
                # Highway = street segment
                if "highway" in tags:
                    kind  = tags.get("highway", "")
                    name  = tags.get("name") or tags.get("ref") or ""
                    label = _make_display(name, kind)
                    segments.append({"name": label, "kind": kind, "coords": coords,
                                      "way_id": el.get("id", 0), "raw_name": name})
                
                # Natural/landuse/leisure/waterway/barrier = natural feature
                elif "addr:interpolation" in tags:
                    # Address interpolation way
                    interp_type = tags.get("addr:interpolation", "all")
                    street_name = tags.get("addr:street", "")
                    nodes = el.get("nodes", [])
                    
                    if street_name and len(nodes) >= 2:
                        # Get start and end nodes with house numbers
                        start_node = nodes_dict.get(nodes[0], {})
                        end_node = nodes_dict.get(nodes[-1], {})
                        
                        start_num = start_node.get("tags", {}).get("addr:housenumber")
                        end_num = end_node.get("tags", {}).get("addr:housenumber")
                        
                        # Only use if both endpoints have numbers
                        if start_num and end_num:
                            try:
                                start_num_int = int(''.join(filter(str.isdigit, start_num)))
                                end_num_int = int(''.join(filter(str.isdigit, end_num)))
                                
                                interpolations.append({
                                    "street": street_name,
                                    "type": interp_type,
                                    "start": {
                                        "lat": start_node.get("lat"),
                                        "lon": start_node.get("lon"),
                                        "num": start_num_int
                                    },
                                    "end": {
                                        "lat": end_node.get("lat"),
                                        "lon": end_node.get("lon"),
                                        "num": end_num_int
                                    },
                                    "coords": coords
                                })
                            except (ValueError, TypeError):
                                # Skip if numbers aren't parseable
                                pass
                else:
                    feature_type = None
                    feature_name = tags.get("name", "")
                    
                    if "natural" in tags:
                        feature_type = tags["natural"]
                    elif "waterway" in tags:
                        feature_type = tags["waterway"]
                    elif "leisure" in tags:
                        feature_type = tags["leisure"]
                    elif "landuse" in tags:
                        feature_type = tags["landuse"]
                    elif "barrier" in tags:
                        feature_type = tags["barrier"]
                    
                    if feature_type:
                        natural_features.append({
                            "type": feature_type,
                            "name": feature_name,
                            "coords": coords,
                            "way_id": el.get("id", 0)
                        })


        source = "boundary" if used_boundary else "radius"
        print(f"[Street] Stage 1 complete ({source}): {len(segments)} segments, {len(natural_features)} natural features, {len(interpolations)} interpolations")
        
        # Use GNAF for Australia, Overpass elsewhere
        country_code = country_code or ""
        if country_code.lower() == 'au':
            addresses = self._fetch_gnaf_addresses(centre_lat, centre_lon, suburb_name or "", radius)
        else:
            addresses = self._fetch_addresses(centre_lat, centre_lon, radius)

        # Boundary query returns all road types in one shot — cache immediately
        # so Shift+F11 pre-downloads are persisted and F11 entry is instant.
        if used_boundary and len(segments) >= 10:
            _save_road_cache(self._cache_dir, centre_lat, centre_lon, {
                "segments":  segments,
                "addresses": addresses,
                "interpolations": interpolations,
                "natural_features": natural_features,
                "ts":        time.time(),
            }, suburb_name=suburb_name, used_boundary=True)
            print(f"[Street] Cached {len(segments)} segments for future use")
            
            # Disabled: 7km prefetch causes rate limiting (429) and timeouts (504)
            # Large radius already provides good coverage without hammering server
            # threading.Thread(
            #     target=self._prefetch_neighbors,
            #     args=(centre_lat, centre_lon, suburb_name, status_cb),
            #     daemon=True
            # ).start()
            # print(f"[Street] Starting 7km neighbor pre-fetch in background...")

        # 6th element: whether query is complete (skip Stage 2 if True)
        # Since we now fetch full radius in Stage 1, mark as complete
        # 7th element: natural features list
        # 8th element: interpolations list
        return segments, addresses, False, snap[0], snap[1], True, natural_features, interpolations
    
    def _prefetch_neighbors(
        self,
        centre_lat: float,
        centre_lon: float,
        origin_suburb: str,
        status_cb=None,
    ):
        """Background fetch: 7km radius around suburb center to pre-cache neighbors.
        
        Fetches all streets within 7km and caches as a radius entry.
        When loading cache for nearby suburbs, both suburb cache and radius 
        cache are checked, providing instant coverage for all neighbors.
        
        7km radius = ~150 km² coverage = 5-6 neighboring suburbs in urban areas.
        No geocoding needed - just cache the entire radius result.
        """
        import time as _time
        _time.sleep(12)  # Let main boundary query + address fetch + cooldown finish
        
        print(f"[Street] Pre-fetching 7km radius for neighbor caching...")
        radius_query = (
            "[out:json][timeout:30];\n(\n"
            f'  way["highway"~"primary|secondary|tertiary|residential|unclassified|living_street|trunk|motorway"](around:7000,{centre_lat},{centre_lon});\n'
            f'  way["highway"~"footway|cycleway|path|service"]["name"](around:7000,{centre_lat},{centre_lon});\n'
            f'  way["natural"~"water|wetland|wood|beach|scrub|grassland|heath"](around:7000,{centre_lat},{centre_lon});\n'
            f'  way["waterway"~"river|stream|canal|drain"](around:7000,{centre_lat},{centre_lon});\n'
            f'  way["leisure"~"park|nature_reserve|recreation_ground"](around:7000,{centre_lat},{centre_lon});\n'
            f'  way["landuse"~"farmland|orchard|vineyard|meadow|forest|grass|quarry"](around:7000,{centre_lat},{centre_lon});\n'
            f'  way["barrier"~"fence|hedge|gate"](around:7000,{centre_lat},{centre_lon});\n'
            ");\nout geom;\n"
        )
        data = urllib.parse.urlencode({"data": radius_query}).encode()
        result = self._overpass.large_request(data, timeout=35)
        
        if not result or not result.get("elements"):
            print("[Street] 7km neighbor pre-fetch failed or empty")
            return
        
        # Parse all segments and natural features in radius
        segments = []
        natural_features = []
        
        for el in result.get("elements", []):
            if el.get("type") != "way":
                continue
            
            tags = el.get("tags", {})
            geom = el.get("geometry", [])
            
            if len(geom) < 2:
                continue
            
            coords = [(pt["lat"], pt["lon"]) for pt in geom]
            way_id = el.get("id", 0)
            
            # Highway = street segment
            if "highway" in tags:
                kind = tags.get("highway", "")
                name = tags.get("name") or tags.get("ref") or ""
                label = _make_display(name, kind)
                
                segments.append({
                    "name": label,
                    "kind": kind,
                    "coords": coords,
                    "way_id": way_id,
                    "raw_name": name
                })
            
            # Natural/landuse/leisure/waterway/barrier = natural feature
            else:
                feature_type = None
                feature_name = tags.get("name", "")
                
                if "natural" in tags:
                    feature_type = tags["natural"]
                elif "waterway" in tags:
                    feature_type = tags["waterway"]
                elif "leisure" in tags:
                    feature_type = tags["leisure"]
                elif "landuse" in tags:
                    feature_type = tags["landuse"]
                elif "barrier" in tags:
                    feature_type = tags["barrier"]
                
                if feature_type:
                    natural_features.append({
                        "type": feature_type,
                        "name": feature_name,
                        "coords": coords,
                        "way_id": way_id
                    })
        
        if len(segments) < 10:
            print(f"[Street] 7km radius returned only {len(segments)} segments, not caching")
            return
        
        # Cache entire radius as a coordinate-based entry (not suburb-based)
        # This gets checked as a fallback when suburb-specific cache misses
        print(f"[Street] Caching {len(segments)} streets, {len(natural_features)} natural features from 7km radius")
        _save_road_cache(self._cache_dir, centre_lat, centre_lon, {
            "segments": segments,
            "addresses": [],
            "natural_features": natural_features,
            "ts": time.time(),
            "cache_center_lat": centre_lat,
            "cache_center_lon": centre_lon,
        }, suburb_name=None, used_boundary=False)
        print(f"[Street] 7km radius pre-fetch complete: {len(segments)} streets, {len(natural_features)} features cached")
        
        # Notify completion
        if status_cb:
            status_cb(f"Neighbor cache ready: {len(segments)} streets within 7km.")

    def live_fetch_outer(
        self,
        centre_lat: float,
        centre_lon: float,
        radius: int,
        existing_segments: list,
        status_cb=None,
    ) -> tuple[list, list]:
        """Stage 2 — fetch full radius all road types, merge with existing segments.

        Returns (merged_segments, addresses).
        Called on a background thread after Stage 1 has already been
        announced to the user.
        """
        def status(msg):
            if status_cb:
                status_cb(msg)
            print(f"[Street] {msg}")

        outer_query = (
            "[out:json][timeout:20];\n(\n"
            f'  way["highway"~"primary|secondary|tertiary|residential|unclassified|living_street|trunk|motorway"](around:{radius},{centre_lat},{centre_lon});\n'
            f'  way["highway"~"footway|cycleway|path|service"]["name"](around:{radius},{centre_lat},{centre_lon});\n'
            ");\nout geom;\n"
        )
        data   = urllib.parse.urlencode({"data": outer_query}).encode()
        result = self._overpass.large_request(data, timeout=20)
        if not result:
            print("[Street] Stage 2 outer fetch failed — keeping inner segments")
            return existing_segments, []

        # Deduplicate by OSM way ID — coords[0] is insufficient because the
        # same road can have multiple ways starting at different points.
        # Stage 2 re-fetches the full radius including inner streets already
        # loaded by Stage 1, so without ID deduplication the same OSM way
        # gets added twice and _merge_chunks creates spurious loops.
        existing_ids: set = {seg.get("way_id", 0) for seg in existing_segments
                             if seg.get("way_id")}

        new_segments = list(existing_segments)
        for el in result.get("elements", []):
            if el.get("type") == "way":
                way_id = el.get("id", 0)
                if way_id and way_id in existing_ids:
                    continue
                tags  = el.get("tags", {})
                kind  = tags.get("highway", "")
                name  = tags.get("name") or tags.get("ref") or ""
                label = _make_display(name, kind)
                geom  = el.get("geometry", [])
                if len(geom) < 2:
                    continue
                coords = [(pt["lat"], pt["lon"]) for pt in geom]
                new_segments.append({"name": label, "kind": kind, "coords": coords,
                                     "way_id": way_id, "raw_name": name})
                if way_id:
                    existing_ids.add(way_id)

        addresses = self._fetch_addresses(centre_lat, centre_lon, radius)

        print(f"[Street] Stage 2 complete: {len(new_segments)} total segments "
              f"({len(new_segments) - len(existing_segments)} added)")

        # Cache the full merged result (radius-based, use coordinate grid)
        if len(new_segments) >= 10:
            _save_road_cache(self._cache_dir, centre_lat, centre_lon, {
                "segments":  new_segments,
                "addresses": addresses,
                "ts":        time.time(),
            }, suburb_name=None, used_boundary=False)

        return new_segments, addresses

    def _fetch_gnaf_addresses(self, lat, lon, suburb, radius=2000):
        """Fetch addresses from GNAF server (Australia only)."""
        GNAF_SERVER = "https://samtaylor9.nfshost.com/cgi-bin/gnaf_server.py"
        
        import urllib.request
        import urllib.parse
        import json
        
        params = urllib.parse.urlencode({
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "suburb": suburb,
            "radius": radius,
        })
        
        try:
            url = f"{GNAF_SERVER}?{params}"
            req = urllib.request.Request(url, headers={"User-Agent": "MapInABox/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
            
            if "error" in data:
                print(f"[GNAF] Server error: {data['error']}")
                return []
            
            addresses = data.get("addresses", [])
            print(f"[GNAF] Fetched {len(addresses)} addresses for {suburb}")
            return addresses
            
        except Exception as e:
            print(f"[GNAF] Fetch failed: {e}")
            return []

    def _fetch_addresses(self, lat: float, lon: float, radius: int) -> list:
        """Fetch address nodes as a separate query — silent failure is fine."""
        query = (
            "[out:json][timeout:15];\n"
            f'node["addr:housenumber"]["addr:street"](around:{radius},{lat},{lon});\n'
            "out;\n"
        )
        data = urllib.parse.urlencode({"data": query}).encode()
        try:
            result = self._overpass.large_request(data, timeout=20)
        except Exception as e:
            print(f"[Street] Address fetch error at ({lat:.5f},{lon:.5f}) r={radius}: {e}")
            return []
        if not result:
            print(f"[Street] Address fetch empty result at ({lat:.5f},{lon:.5f}) r={radius}")
            return []
        addresses = []
        for el in result.get("elements", []):
            tags   = el.get("tags", {})
            number = tags.get("addr:housenumber", "")
            street = tags.get("addr:street", "")
            if number and street:
                addresses.append({
                    "number": number,
                    "street": street,
                    "lat":    el.get("lat", 0),
                    "lon":    el.get("lon", 0),
                })
        print(f"[Street] Fetched {len(addresses)} addresses at ({lat:.5f},{lon:.5f}) r={radius}")
        return addresses

    # ------------------------------------------------------------------
    # Nearest road  (called on every player move)
    # ------------------------------------------------------------------

    @staticmethod
    def nearest_roads_with_distances(
        lat: float,
        lon: float,
        segments: list,
    ) -> list[tuple[str, float]]:
        """Return nearby named roads with their true distance in metres."""
        if not segments:
            return []

        MAX_DIST_M = 150.0
        name_dists: dict[str, tuple[float, float]] = {}

        for seg in segments:
            coords   = seg["coords"]
            kind     = seg.get("kind", "")
            raw_name = seg.get("name", "")
            clean    = re.sub(r'\s*\(.*?\)', '', raw_name).strip()
            has_real_name = bool(seg.get("raw_name", "").strip())
            if not clean:
                continue
            if not has_real_name and clean.lower() in GENERIC_STREET_TYPES:
                continue
            if kind in _LOW_DETAIL:
                penalty = 100.0
            elif kind in LOW_PRIORITY_HIGHWAY:
                penalty = 30.0
            else:
                penalty = 0.0

            for i in range(len(coords) - 1):
                alat, alon = coords[i]
                blat, blon = coords[i + 1]
                true_d = dist_to_segment_metres(lat, lon, alat, alon, blat, blon)
                ranked_d = true_d + penalty
                if ranked_d < MAX_DIST_M:
                    if clean not in name_dists or ranked_d < name_dists[clean][0]:
                        name_dists[clean] = (ranked_d, true_d)

        ranked = sorted(name_dists.items(), key=lambda item: item[1][0])
        return [(name, true_d) for name, (_ranked_d, true_d) in ranked]

    @staticmethod
    def nearest_road(
        lat: float,
        lon: float,
        segments: list,
    ) -> tuple[str, str | None]:
        """Find the nearest named road and nearest cross-street.

        Returns
        -------
        (primary_name, cross_name_or_None)
        """
        ranked = StreetFetcher.nearest_roads_with_distances(lat, lon, segments)
        if not ranked and not segments:
            return "No street data", None
        if not ranked:
            return "No street data nearby", None
        primary = ranked[0][0]
        cross   = ranked[1][0] if len(ranked) > 1 else None
        return primary, cross

    # ------------------------------------------------------------------
    # Street name list from loaded segments  (S-key picker)
    # ------------------------------------------------------------------

    @staticmethod
    def street_names_from_segments(segments: list) -> list[str]:
        """Return sorted list of unique, non-generic named streets
        from the currently loaded road segments."""
        seen:  set        = set()
        names: list[str]  = []
        for seg in segments:
            raw  = seg.get("name", "")
            name = re.sub(r'\s*\(.*?\)', '', raw).strip()
            if not name:
                continue
            low = name.lower()
            if low in seen:
                continue
            # Only suppress if this is a generic fallback label (no real
            # name) — streets genuinely called "Main Road" or "Station Street"
            # must still appear.
            has_real_name = bool(seg.get("raw_name", "").strip())
            if not has_real_name and low in GENERIC_STREET_TYPES:
                continue
            seen.add(low)
            names.append(name)
        names.sort()
        return names

    # ------------------------------------------------------------------
    # Cross-street lookup for walk-mode intersections
    # ------------------------------------------------------------------

    @staticmethod
    def cross_streets_at_node(
        node_id: int,
        current_street: str,
        walk_graph: dict,
    ) -> list[str]:
        """Return the names of other streets that meet at *node_id*,
        excluding *current_street* itself."""
        node_streets = walk_graph.get("node_streets", {})
        all_streets  = node_streets.get(node_id, set())
        return [s for s in all_streets
                if s.lower() != current_street.lower()]
