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
    fetch_road_data(lat, lon, radius)
        → (segments, addresses, from_cache, snap_lat, snap_lon,
           skip_stage2, natural_features, interpolations) | raises
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
from logging_utils import miab_log
from overpass_client import OverpassRequestCancelled

from geo import (
    dist_metres,
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
        miab_log("errors", f"[Geocode] Failed to save cache: {e}", None)

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
            miab_log("verbose", f"[Geocode] Cache hit (age: {age_days:.0f} days): {entry.get('suburb')}", None)
            return {
                "suburb": entry.get("suburb"),
                "bbox": tuple(entry.get("bbox", [])) if entry.get("bbox") else None,
                "radius": entry.get("radius"),
                "country_code": entry.get("country_code"),
                "osm_type": entry.get("osm_type"),
                "osm_id": entry.get("osm_id"),
            }
    
    # Try remote cache (if configured)
    if _GEOCODE_REMOTE_URL:
        try:
            miab_log("verbose", f"[Geocode] Checking remote cache: {_GEOCODE_REMOTE_URL}", None)
            req = urllib.request.Request(_GEOCODE_REMOTE_URL, 
                                         headers={"User-Agent": "MapInABox/1.0"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                remote_cache = json.loads(resp.read().decode())
            if cache_key in remote_cache:
                entry = remote_cache[cache_key]
                miab_log("verbose", f"[Geocode] Remote cache hit: {entry.get('suburb')}", None)
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
                    "osm_type": entry.get("osm_type"),
                    "osm_id": entry.get("osm_id"),
                }
        except Exception as e:
            miab_log("errors", f"[Geocode] Remote cache failed: {e}", None)
    
    # Fall back to Nominatim
    try:
        miab_log("verbose", f"[Geocode] Querying Nominatim for {lat:.4f},{lon:.4f}", None)
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

        # Nominatim's reverse response identifies the actual OSM
        # relation/way it matched (at zoom=14, normally the suburb/
        # neighbourhood boundary itself). Keeping this lets fetch_road_data
        # build the Overpass area directly from the known id instead of
        # scanning area["name"=...], which is one of the more expensive
        # query shapes on public Overpass servers.
        osm_type = data.get("osm_type")
        osm_id = data.get("osm_id")

        # Save to cache
        cache[cache_key] = {
            "suburb": suburb,
            "bbox": [minlat, maxlat, minlon, maxlon] if bb else None,
            "radius": radius,
            "country_code": country_code,
            "osm_type": osm_type,
            "osm_id": osm_id,
            "timestamp": time.time()
        }
        _save_geocode_cache(cache)
        miab_log("verbose", f"[Geocode] Nominatim success, cached: {suburb}", None)

        return {
            "suburb": suburb,
            "bbox": tuple([minlat, maxlat, minlon, maxlon]) if bb else None,
            "radius": radius,
            "country_code": country_code,
            "osm_type": osm_type,
            "osm_id": osm_id,
        }
    except Exception as e:
        miab_log("errors", f"[Geocode] Nominatim failed: {e}", None)
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


class StreetFetchCancelled(RuntimeError):
    """The owning street-mode request was cancelled or superseded."""


def _overpass_string(value: str) -> str:
    """Escape a value embedded in an Overpass QL double-quoted string."""
    return (str(value).replace("\\", "\\\\").replace('"', '\\"')
            .replace("\r", "\\r").replace("\n", "\\n"))


def _make_display(name: str, kind: str) -> str:
    human = ROAD_LABELS.get(kind, kind or "road")
    return f"{name} ({human})" if (name and kind in _LOW_DETAIL) else (name or human)


# ---------------------------------------------------------------------------
# Cache helpers — one JSON file per city in a road_cache/ folder
# Files are named by suburb/locality, looked up via an index.json
# ---------------------------------------------------------------------------

_INDEX_FILE = "index.json"

# Sidecar file: cache key -> [lat, lon] the entry was actually centred on,
# regardless of whether the key itself is a coordinate grid cell or a
# suburb name. index.json alone can't answer "what's cached near here?"
# for suburb-keyed entries, since a key like "suburb_annerley" doesn't
# encode a coordinate the way "-27.5_153.0" does - so a plain coordinate
# lookup (organic navigation, which usually doesn't know the suburb name
# in advance) would never find a suburb pre-fetched by name, e.g. via the
# city-pack wizard or Shift+F11, and would silently re-fetch and re-cache
# it under a different key instead. This sidecar makes that coordinate
# lookup able to check every cached area's real centre, not just ones
# whose key happens to be coordinate-shaped.
_CENTERS_FILE = "centers_index.json"


def _load_centers_index(cache_dir: str) -> dict:
    """Load cache key -> [lat, lon] sidecar. Returns {} on any failure."""
    path = os.path.join(cache_dir, _CENTERS_FILE)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_centers_index(cache_dir: str, centers: dict) -> None:
    try:
        with open(os.path.join(cache_dir, _CENTERS_FILE), "w", encoding="utf-8") as f:
            json.dump(centers, f, ensure_ascii=False)
    except Exception:
        pass


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


def _load_road_cache_by_coord(
    cache_dir: str,
    lat: float,
    lon: float,
    include_named_suburb_entries: bool = True,
) -> dict:
    """Load cached road data by coordinate grid cell (no suburb constraint).
    Checks the exact cell, then adjacent cells (±0.1 degrees = ~11km),
    validating any adjacent-cell hit is within 7km of the target so it
    doesn't silently return a distant/unrelated area's data."""
    index = _load_index(cache_dir)
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
                                    miab_log("street", f"[Street] Found cache in adjacent cell {adj_key}, {dist:.0f}m from center", None)
                                    if data.get("_version") == _CACHE_VERSION:
                                        return data
                        except Exception:
                            pass
                    fname = None  # Reset if validation failed

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

    # Last resort: this spot may only be cached under a suburb-name key
    # (e.g. bulk-downloaded via the city-pack wizard, or single-suburb
    # prefetched via Shift+F11 - both cache by suburb name, not by grid
    # coordinate). A key like "suburb_annerley" isn't coordinate-shaped,
    # so the grid lookups above never see it even though it covers this
    # exact spot - without this, organic navigation into a suburb that
    # was only ever bulk-downloaded by name would always miss cache and
    # silently re-fetch/re-cache it under a different key. The sidecar
    # centre index makes this a single small-file read rather than
    # opening every cached area's JSON file to check its centre.
    import math
    centers = _load_centers_index(cache_dir)
    # One-time backfill for entries saved before this sidecar existed
    # (e.g. suburbs already downloaded via an earlier version of the
    # city-pack wizard) - without this, only newly-saved areas would
    # benefit and something like Annerley would need to be re-fetched
    # once more before it started being found this way. Only opens files
    # for keys not already in the sidecar, and only on an actual miss, so
    # it's a bounded one-off cost, not a per-request one.
    backfilled = False
    for cand_key, cand_fname in index.items():
        if cand_key in centers or cand_key == key:
            continue
        cand_path = os.path.join(cache_dir, cand_fname)
        if not os.path.exists(cand_path):
            continue
        try:
            with open(cand_path, encoding="utf-8") as f:
                cand_data = json.load(f)
            clat, clon = cand_data.get("cache_center_lat"), cand_data.get("cache_center_lon")
            if clat is not None and clon is not None:
                centers[cand_key] = [clat, clon]
                backfilled = True
        except Exception:
            continue
    if backfilled:
        _save_centers_index(cache_dir, centers)

    best_key, best_dist = None, 7000.0
    for cand_key, (clat, clon) in centers.items():
        if cand_key == key:
            continue  # already checked above
        if not include_named_suburb_entries and cand_key.startswith("suburb_"):
            continue
        dlat_m = (lat - clat) * 111000
        dlon_m = (lon - clon) * 111000 * math.cos(math.radians(lat))
        dist = math.sqrt(dlat_m ** 2 + dlon_m ** 2)
        if dist < best_dist:
            best_key, best_dist = cand_key, dist
    if best_key:
        cand_fname = index.get(best_key)
        if cand_fname:
            path = os.path.join(cache_dir, cand_fname)
            if os.path.exists(path):
                try:
                    with open(path, encoding="utf-8") as f:
                        data = json.load(f)
                    if data.get("_version") == _CACHE_VERSION:
                        miab_log("street", f"[Street] Found cache via centre-index scan ({best_key}), {best_dist:.0f}m away", None)
                        return data
                except Exception:
                    pass

    return {}


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
                        cache_lat = data.get("cache_center_lat")
                        cache_lon = data.get("cache_center_lon")
                        indexed_center = _load_centers_index(cache_dir).get(suburb_key)
                        if (
                            indexed_center
                            and cache_lat is not None
                            and cache_lon is not None
                            and dist_metres(
                                indexed_center[0], indexed_center[1],
                                cache_lat, cache_lon,
                            ) > 250
                        ):
                            miab_log(
                                "street",
                                f"[Street] Ignoring suburb cache for {suburb_name!r}; "
                                "stored centre disagrees with cache index.",
                                None,
                            )
                            return {}
                        return data
                except Exception:
                    pass
        # Named (suburb-boundary) lookup missed — this suburb may only ever
        # have been cached via the radius fallback (e.g. no OSM admin
        # boundary matched its name). Fall back to the coordinate-grid
        # cache rather than giving up and re-hitting Overpass every time;
        # the distance check in _load_road_cache_by_coord still guards
        # against silently returning a distant/unrelated area's data.
        coord_hit = _load_road_cache_by_coord(
            cache_dir, lat, lon, include_named_suburb_entries=False)
        if coord_hit:
            return coord_hit
        return {}

    # No suburb name — coordinate-based cache only.
    return _load_road_cache_by_coord(cache_dir, lat, lon)


def _save_road_cache(cache_dir: str, lat: float, lon: float, entry: dict,
                     suburb_name: str = None, used_boundary: bool = False) -> None:
    """Save road data to cache. Uses suburb name for boundary queries."""
    try:
        os.makedirs(cache_dir, exist_ok=True)
        key = _index_key(lat, lon, suburb_name, used_boundary)
        miab_log("street", f"[Street] Saving cache to {cache_dir} key={key}", None)
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
        centers = _load_centers_index(cache_dir)
        centers[key] = [lat, lon]
        _save_centers_index(cache_dir, centers)
        entry["_version"] = _CACHE_VERSION
        with open(os.path.join(cache_dir, fname), "w", encoding="utf-8") as f:
            json.dump(entry, f, ensure_ascii=False)
    except Exception as _e:
        miab_log("errors", f"[Street] Cache save FAILED: {_e}", None)


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
        stage1_done_cb=None,
        suburb_name: str | None = None,
        country_code: str | None = None,
        use_gnaf: bool = True,
        osm_type: str | None = None,
        osm_id: int | None = None,
        cancel_cb=None,
    ) -> tuple:
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
        stage1_done_cb:
            Optional zero-argument callback invoked when stage 1 completes.
        osm_type, osm_id:
            Optional OSM relation/way identifying suburb_name's boundary
            (from geocode_location's Nominatim result). When given, the
            boundary area is built directly from this id instead of the
            slower area["name"=...] scan.

        Returns
        -------
        (segments, addresses, from_cache, snap_lat, snap_lon,
         skip_stage2, natural_features, interpolations)
        """
        def status(msg):
            if status_cb:
                status_cb(msg)
            miab_log("street", f"[Street] {msg}", None)

        def check_cancelled():
            if cancel_cb and cancel_cb():
                raise StreetFetchCancelled()

        centre_lat = fetch_lat or lat
        centre_lon = fetch_lon or lon
        snap       = (fetch_lat, fetch_lon) if (fetch_lat and fetch_lon) else (None, None)

        check_cancelled()
        entry = _load_road_cache(self._cache_dir, centre_lat, centre_lon, suburb_name)
        stale_entry = entry

        if entry:
            segs  = entry.get("segments", [])
            addrs = entry.get("addresses", [])
            if (suburb_name and len(segs) < 150
                    and entry.get("coverage") != "boundary"
                    and not entry.get("boundary_supplemented")):
                miab_log("street", f"[Street] small suburb cache — {len(segs)} segments, refreshing with radius supplement", getattr(self, "settings", None))
                entry = {}

        if entry:
            segs  = entry.get("segments", [])
            addrs = entry.get("addresses", [])
            address_source = entry.get("address_source", "")
            address_source_corrected = False
            if country_code and country_code.lower() == "au":
                if use_gnaf and address_source != "gnaf":
                    addrs = self._fetch_gnaf_addresses(centre_lat, centre_lon, suburb_name or "", radius)
                    address_source = "gnaf"
                    address_source_corrected = True
                elif not use_gnaf and address_source != "osm":
                    addrs = self._fetch_addresses(centre_lat, centre_lon, radius)
                    address_source = "osm"
                    address_source_corrected = True
            if address_source_corrected:
                # Persist the correction, not just this call's return value -
                # otherwise an entry cached with the "wrong" address source
                # (e.g. downloaded by the city-pack wizard with GNAF on,
                # while this install has GNAF off) would re-hit the address
                # server on every single visit forever, not just once.
                entry["addresses"] = addrs
                entry["address_source"] = address_source
                _save_road_cache(
                    self._cache_dir, centre_lat, centre_lon, entry,
                    suburb_name=suburb_name, used_boundary=bool(suburb_name),
                )
            natural_features = entry.get("natural_features", [])
            interpolations = entry.get("interpolations", [])
            stale = _cache_is_stale(entry)
            if not stale:
                miab_log("street", f"[Street] cache hit — {len(segs)} segments, {len(natural_features)} natural features, {len(interpolations)} interpolations", getattr(self, "settings", None))
                return segs, addrs, True, snap[0], snap[1], False, natural_features, interpolations
            else:
                # Stale but usable — return immediately, refresh in background
                miab_log("street", f"[Street] stale cache — {len(segs)} segments, {len(natural_features)} natural features, {len(interpolations)} interpolations, serving now, refreshing background", getattr(self, "settings", None))
                # Kick off background refresh without blocking
                import threading as _threading
                def _bg_refresh():
                    try:
                        self._live_fetch(
                            centre_lat, centre_lon, radius,
                            suburb_name=suburb_name,
                            country_code=country_code,
                            use_gnaf=use_gnaf,
                            osm_type=osm_type,
                            osm_id=osm_id,
                            cancel_cb=cancel_cb,
                        )
                    except Exception:
                        pass
                _threading.Thread(target=_bg_refresh, daemon=True).start()
                return segs, addrs, True, snap[0], snap[1], False, natural_features, interpolations

        # No fresh cache — try live fetch, fall back to stale if all mirrors fail.
        try:
            return self._live_fetch(centre_lat, centre_lon, radius,
                                    snap=snap, status_cb=status_cb,
                                    stage1_done_cb=stage1_done_cb,
                                    suburb_name=suburb_name,
                                    country_code=country_code,
                                    use_gnaf=use_gnaf,
                                    osm_type=osm_type,
                                    osm_id=osm_id,
                                    cancel_cb=cancel_cb)

        except StreetFetchCancelled:
            raise
        except RuntimeError:
            if stale_entry:
                segs  = stale_entry.get("segments", [])
                addrs = stale_entry.get("addresses", [])
                address_source = stale_entry.get("address_source", "")
                if country_code and country_code.lower() == "au":
                    if use_gnaf and address_source != "gnaf":
                        addrs = self._fetch_gnaf_addresses(centre_lat, centre_lon, suburb_name or "", radius)
                    elif not use_gnaf and address_source != "osm":
                        addrs = self._fetch_addresses(centre_lat, centre_lon, radius)
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
        stage1_done_cb=None,
        suburb_name: str | None = None,
        country_code: str | None = None,
        use_gnaf: bool = True,
        osm_type: str | None = None,
        osm_id: int | None = None,
        cancel_cb=None,
    ) -> tuple:
        """Fetch streets using OSM admin boundary if available, else radius.

        Primary: query streets within the named suburb admin boundary — this
        respects the actual suburb shape so peninsula/coastal suburbs only
        get their own streets, not neighbours.
        Fallback: radius query if no boundary found in OSM.

        Returns the same tuple shape as ``fetch_road_data``.
        """
        def status(msg):
            if status_cb:
                status_cb(msg)
            miab_log("street", f"[Street] {msg}", None)

        def check_cancelled():
            if cancel_cb and cancel_cb():
                raise StreetFetchCancelled()

        def overpass_request(data, **kwargs):
            check_cancelled()
            try:
                return self._overpass.large_request(
                    data, cancel_cb=cancel_cb, **kwargs)
            except OverpassRequestCancelled as exc:
                raise StreetFetchCancelled() from exc

        miab_log("street", f"[Street] Fetching at centre: {centre_lat:.5f}, {centre_lon:.5f} radius {radius}m suburb={suburb_name!r}", getattr(self, "settings", None))
        # status_cb set on overpass only for radius fallback — boundary loop
        # announces once via status() and suppresses per-server messages to avoid double-speak.
        self._overpass.status_cb = None

        result = None
        used_boundary = False
        escaped_suburb = _overpass_string(suburb_name or "")

        def _street_core_query(
            scope: str,
            timeout_secs: int,
            bbox: str = "",
            prefix: str = "",
        ) -> str:
            bbox_clause = f"[bbox:{bbox}]" if bbox else ""
            return (
                f"[out:json][timeout:{timeout_secs}]{bbox_clause};\n"
                f"{prefix}"
                "(\n"
                # All named highways are required for trustworthy street
                # search. The first clause also retains unnamed driveable ways
                # needed to build a connected navigation graph.
                f'  way["highway"~"primary|secondary|tertiary|residential|unclassified|living_street|trunk|motorway"]{scope};\n'
                f'  way["highway"]["name"]{scope};\n'
                f'  way["addr:interpolation"]{scope};\n'
                ");\n"
                "out geom;\n"
                "(\n"
                "  way._[\"addr:interpolation\"];\n"
                "  node(w)[\"addr:housenumber\"];\n"
                ");\n"
                "out;\n"
            )

        import math as _math
        _deg_lat = radius / 111000.0
        _deg_lon = radius / (111000.0 * _math.cos(_math.radians(centre_lat)))
        bbox_str = (f"{centre_lat - _deg_lat:.5f},{centre_lon - _deg_lon:.5f},"
                    f"{centre_lat + _deg_lat:.5f},{centre_lon + _deg_lon:.5f}")

        # Boundary-only mode with radius fallback
        skip_boundary = False
        id_boundary_failed = False

        # ── Fast path: build the area directly from a known OSM id ────
        # If geocode_location already told us which relation/way this
        # suburb is (from Nominatim's reverse lookup), skip the
        # area["name"=...] scan entirely — that name-scan is one of the
        # more expensive query shapes on public Overpass servers and a
        # likely cause of the 504 timeouts seen in practice. A direct
        # id-based area is essentially free by comparison.
        if suburb_name and not skip_boundary and osm_type in ("relation", "way") and osm_id:
            status(f"Loading streets for {suburb_name}...")
            type_prefix = "rel" if osm_type == "relation" else "way"
            id_query = _street_core_query(
                "(area.a)", 15,
                prefix=f"{type_prefix}({osm_id});\nmap_to_area->.a;\n")
            data = urllib.parse.urlencode({"data": id_query}).encode()
            result = overpass_request(data, timeout=18, max_mirrors=2)
            if result and result.get("elements"):
                used_boundary = True
                miab_log(
                    "street",
                    f"[Street] ID-based area query succeeded for {suburb_name!r} "
                    f"({osm_type} {osm_id}): {len(result['elements'])} ways",
                    getattr(self, "settings", None),
                )
            elif result is None:
                id_boundary_failed = True
                miab_log(
                    "street",
                    f"[Street] ID-based area query for {suburb_name!r} ({osm_type} {osm_id}) "
                    "timed out; skipping slower name-based boundary retry.",
                    getattr(self, "settings", None),
                )
            else:
                miab_log(
                    "street",
                    f"[Street] ID-based area query for {suburb_name!r} ({osm_type} {osm_id}) "
                    "returned nothing; falling back to a name-based boundary query.",
                    getattr(self, "settings", None),
                )

        # ── Name-based boundary query (fallback, or when no id known) ──
        if suburb_name and not skip_boundary and not used_boundary and not id_boundary_failed:
            status(f"Loading streets for {suburb_name}...")
            # Select both common boundary representations in the same request.
            # The old probe followed by one or two full queries delayed every
            # cold-cache load without making the final street set more complete.
            area_filter = (
                "(\n"
                f'  area["name"="{escaped_suburb}"]["boundary"="administrative"]({bbox_str});\n'
                f'  area["name"="{escaped_suburb}"]["place"~"suburb|town|village|municipality|locality|quarter|neighbourhood"]({bbox_str});\n'
                ")->.a;"
            )
            boundary_query = _street_core_query(
                "(area.a)", 15, prefix=f"{area_filter}\n")
            data = urllib.parse.urlencode({"data": boundary_query}).encode()
            result = overpass_request(data, timeout=18, max_mirrors=2)
            if result and result.get("elements"):
                used_boundary = True
                miab_log("street", f"[Street] Name-based query succeeded for {suburb_name!r}: {len(result['elements'])} elements", getattr(self, "settings", None))
            else:
                result = None
                miab_log(
                    "street",
                    f"[Street] Boundary query for {suburb_name!r} did not return streets.",
                    getattr(self, "settings", None),
                )

        # ── Radius fallback if boundary failed ────────────────────────
        if not used_boundary:
            self._overpass.status_cb = status_cb  # announce server only for fallback
            if suburb_name:
                miab_log("street", f"[Street] No boundary found for {suburb_name}, trying radius fallback...", getattr(self, "settings", None))
            else:
                miab_log("street", f"[Street] No suburb name, using radius query...", getattr(self, "settings", None))
            
            radius_scope = f"(around:{radius},{centre_lat},{centre_lon})"
            radius_query = _street_core_query(radius_scope, 15)
            data = urllib.parse.urlencode({"data": radius_query}).encode()
            result = overpass_request(data, timeout=18, max_mirrors=2)
            if result and result.get("elements"):
                miab_log("street", f"[Street] Radius fallback succeeded: {len(result['elements'])} ways", getattr(self, "settings", None))
            else:
                miab_log("errors", f"[Street] Radius fallback also failed", getattr(self, "settings", None))

        self._overpass.status_cb = None
        check_cancelled()
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
                
                # Address interpolation way
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
        miab_log("street", f"[Street] Stage 1 complete ({source}): {len(segments)} segments, {len(natural_features)} natural features, {len(interpolations)} interpolations", getattr(self, "settings", None))
        if stage1_done_cb:
            stage1_done_cb()

        check_cancelled()
        
        # Use GNAF for Australia when enabled, otherwise use OSM addresses.
        country_code = country_code or ""
        if country_code.lower() == 'au' and use_gnaf:
            addresses = self._fetch_gnaf_addresses(centre_lat, centre_lon, suburb_name or "", radius)
            address_source = "gnaf"
        else:
            addresses = self._fetch_addresses(centre_lat, centre_lon, radius)
            address_source = "osm"

        # Cache immediately so Shift+F11 pre-downloads are persisted and F11
        # entry is instant — for both the boundary query (suburb-keyed) and
        # the radius fallback (coordinate-grid-keyed). Previously only the
        # boundary path was cached, so any suburb where the OSM boundary
        # query didn't match (rural areas, name mismatches, etc.) silently
        # re-hit Overpass on every single visit.
        if len(segments) >= 10:
            _save_road_cache(self._cache_dir, centre_lat, centre_lon, {
                "segments":  segments,
                "addresses": addresses,
                "address_source": address_source,
                "interpolations": interpolations,
                "natural_features": natural_features,
                "coverage": source,
                "ts":        time.time(),
                "cache_center_lat": centre_lat,
                "cache_center_lon": centre_lon,
            }, suburb_name=suburb_name, used_boundary=used_boundary)
            miab_log("street", f"[Street] Cached {len(segments)} segments for future use "
                  f"({'boundary' if used_boundary else 'radius'})", getattr(self, "settings", None))
            # 7km neighbor prefetch stays disabled to avoid rate limiting and timeouts.

        return segments, addresses, False, snap[0], snap[1], True, natural_features, interpolations
    
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
            miab_log("street", f"[Street] {msg}", None)

        outer_query = (
            "[out:json][timeout:20];\n(\n"
            f'  way["highway"~"primary|secondary|tertiary|residential|unclassified|living_street|trunk|motorway"](around:{radius},{centre_lat},{centre_lon});\n'
            f'  way["highway"~"footway|cycleway|path|service"]["name"](around:{radius},{centre_lat},{centre_lon});\n'
            ");\nout geom;\n"
        )
        data   = urllib.parse.urlencode({"data": outer_query}).encode()
        result = self._overpass.large_request(data, timeout=20)
        if not result:
            miab_log("errors", "[Street] Stage 2 outer fetch failed — keeping inner segments", getattr(self, "settings", None))
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

        miab_log("street", f"[Street] Stage 2 complete: {len(new_segments)} total segments "
              f"({len(new_segments) - len(existing_segments)} added)", getattr(self, "settings", None))

        # Cache the full merged result (radius-based, use coordinate grid)
        if len(new_segments) >= 10:
            _save_road_cache(self._cache_dir, centre_lat, centre_lon, {
                "segments":  new_segments,
                "addresses": addresses,
                "address_source": "osm",
                "ts":        time.time(),
            }, suburb_name=None, used_boundary=False)

        return new_segments, addresses

    def _fetch_gnaf_addresses(self, lat, lon, suburb, radius=2000):
        """Fetch addresses from GNAF server (Australia only)."""
        GNAF_SERVER = "https://samtaylor9.nfshost.com/cgi-bin/gnaf_server.py"

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
                miab_log("errors", f"[GNAF] Server error: {data['error']}", getattr(self, "settings", None))
                return []

            addresses = data.get("addresses", [])
            for addr in addresses:
                if isinstance(addr, dict):
                    addr["source"] = "gnaf"
            miab_log("verbose", f"[GNAF] Fetched {len(addresses)} addresses for {suburb}", getattr(self, "settings", None))
            return addresses

        except Exception as e:
            miab_log("errors", f"[GNAF] Fetch failed: {e}", getattr(self, "settings", None))
            return []

    def _fetch_addresses(self, lat: float, lon: float, radius: int) -> list:
        """Fetch address nodes, building polygons and multipolygon buildings.

        Big buildings (e.g. apartment blocks, schools, shopping centres)
        almost always carry their addr:housenumber on the building polygon
        rather than a separate node. Querying ways and relations as well
        catches these; ``out center;`` makes Overpass return a centroid
        for each shape so we can use it as the address point. Silent
        failure is fine — the caller falls back to nodes-only data.
        """
        query = (
            "[out:json][timeout:15];\n"
            "(\n"
            f'  node["addr:housenumber"]["addr:street"](around:{radius},{lat},{lon});\n'
            f'  way["addr:housenumber"]["addr:street"](around:{radius},{lat},{lon});\n'
            f'  relation["addr:housenumber"]["addr:street"](around:{radius},{lat},{lon});\n'
            ");\n"
            "out center;\n"
        )
        data = urllib.parse.urlencode({"data": query}).encode()
        try:
            result = self._overpass.large_request(data, timeout=20)
        except Exception as e:
            miab_log("errors", f"[Street] Address fetch error at ({lat:.5f},{lon:.5f}) r={radius}: {e}", getattr(self, "settings", None))
            return []
        if not result:
            miab_log("street", f"[Street] Address fetch empty result at ({lat:.5f},{lon:.5f}) r={radius}", getattr(self, "settings", None))
            return []
        addresses = []
        n_node = n_way = n_rel = 0
        for el in result.get("elements", []):
            tags   = el.get("tags", {})
            number = tags.get("addr:housenumber", "")
            street = tags.get("addr:street", "")
            if not (number and street):
                continue
            kind = el.get("type", "node")
            if kind == "node":
                plat = el.get("lat", 0)
                plon = el.get("lon", 0)
                n_node += 1
            else:
                # way / relation: centroid from `out center;`
                centre = el.get("center") or {}
                plat = centre.get("lat", 0)
                plon = centre.get("lon", 0)
                if not plat or not plon:
                    continue
                if kind == "way":
                    n_way += 1
                else:
                    n_rel += 1
            addresses.append({
                "number": number,
                "street": street,
                "lat":    plat,
                "lon":    plon,
                "source": "osm",
            })
        miab_log("street", f"[Street] Fetched {len(addresses)} addresses at ({lat:.5f},{lon:.5f}) "
              f"r={radius}  (nodes={n_node} ways={n_way} relations={n_rel})", getattr(self, "settings", None))
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
