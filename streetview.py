"""streetview.py — Google Street View imagery lookup with vision analysis.

Fetches two Street View frames at a given coordinate (one in each direction
along the street) and uses Gemini to describe what is visible from street
level — shops, signage, building types, access features.

Parallel to satellite.py in structure and calling convention.
"""

import json
import os
import time
import urllib.parse
import urllib.request
from typing import Optional, Tuple


# ── Heading helpers ────────────────────────────────────────────────────────────

def _cardinal(heading: float) -> str:
    """Return a compass name for a heading in degrees."""
    dirs = [
        "north", "north-east", "east", "south-east",
        "south", "south-west", "west", "north-west",
    ]
    return dirs[round(heading / 45) % 8]


def _opposite(heading: float) -> float:
    return (heading + 180) % 360


# ── Coverage check ─────────────────────────────────────────────────────────────

def _streetview_available(lat: float, lon: float, api_key: str) -> bool:
    """Return True if Google Street View has coverage at this location."""
    params = urllib.parse.urlencode({"location": f"{lat},{lon}", "key": api_key})
    url = f"https://maps.googleapis.com/maps/api/streetview/metadata?{params}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MapInABox/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
            return data.get("status") == "OK"
    except Exception as e:
        print(f"[StreetView] Metadata check failed: {e}")
        return False


# ── Image fetch ────────────────────────────────────────────────────────────────

def _fetch_streetview_image(
    lat: float, lon: float, heading: float, api_key: str
) -> Optional[bytes]:
    """Fetch one Street View JPEG for the given heading. Returns raw bytes or None."""
    params = urllib.parse.urlencode({
        "size":     "640x480",
        "location": f"{lat},{lon}",
        "heading":  f"{heading:.1f}",
        "fov":      "90",
        "pitch":    "0",
        "key":      api_key,
    })
    url = f"https://maps.googleapis.com/maps/api/streetview?{params}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MapInABox/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read()
            # Google returns a small grey placeholder (~5 KB) when there is no
            # imagery at the requested heading.  Real photos are always larger.
            if len(data) < 10_000:
                return None
            return data
    except Exception as e:
        print(f"[StreetView] Image fetch failed (heading {heading:.0f}deg): {e}")
        return None


# ── Cache helpers (same format as satellite.py) ────────────────────────────────

def _load_cache(cache_path: str) -> dict:
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[StreetView] Cache load failed: {e}")
    return {}


def _save_cache(cache_path: str, cache: dict) -> None:
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[StreetView] Cache save failed: {e}")


def _get_cached(cache: dict, key: str, ttl_days: int = 30) -> Optional[str]:
    """Get cached description if still fresh.

    30-day TTL — street scenes change more often than satellite imagery.
    """
    entry = cache.get(key)
    if not isinstance(entry, dict):
        return None
    if (time.time() - entry.get("ts", 0)) / 86400 > ttl_days:
        return None
    return entry.get("text")


def _set_cached(cache: dict, key: str, value: str) -> None:
    cache[key] = {"text": value, "ts": time.time()}


# ── Public entry point ─────────────────────────────────────────────────────────

def lookup_streetview_description(
    lat: float,
    lon: float,
    google_api_key: str = "",
    gemini_client=None,
    street_heading: Optional[float] = None,
    cache_path: str = "streetview_cache.json",
) -> Optional[Tuple[list, str]]:
    """Fetch Street View imagery and return image bytes list + description.

    street_heading, if supplied, is the compass bearing the user is currently
    travelling along.  We fetch that direction and its opposite so both sides
    of the street are covered with meaningful direction labels in the
    description.  When None we default to north (0 deg) and south (180 deg).

    Returns (image_bytes_list, description) or None if no coverage or error.
    image_bytes_list contains 1 or 2 JPEG byte strings for display.
    """
    if not google_api_key:
        return None

    cache_key = f"sv_{lat:.4f}_{lon:.4f}"
    cache = _load_cache(cache_path)
    cached_desc = _get_cached(cache, cache_key)

    # ── Coverage check ─────────────────────────────────────────────────────
    print(f"[StreetView] Checking coverage at ({lat:.4f}, {lon:.4f})...")
    if not _streetview_available(lat, lon, google_api_key):
        print("[StreetView] No Street View coverage at this location.")
        return None

    # ── Determine headings ─────────────────────────────────────────────────
    h1 = street_heading if street_heading is not None else 0.0
    h2 = _opposite(h1)

    # ── Fetch images ───────────────────────────────────────────────────────
    print(f"[StreetView] Fetching images (headings {h1:.0f}deg and {h2:.0f}deg)...")
    img_a = _fetch_streetview_image(lat, lon, h1, google_api_key)
    img_b = _fetch_streetview_image(lat, lon, h2, google_api_key)

    images = []   # list of (bytes, heading) for Gemini
    if img_a:
        images.append((img_a, h1))
    if img_b:
        images.append((img_b, h2))

    if not images:
        print("[StreetView] Image fetch returned no usable frames.")
        return None

    image_bytes_list = [img for img, _ in images]

    # ── Description (cached text reused with fresh images) ─────────────────
    if cached_desc:
        print(f"[StreetView] Cache hit for {cache_key}.")
        return (image_bytes_list, cached_desc)

    if not gemini_client:
        return (
            image_bytes_list,
            "Street View imagery loaded. A Gemini API key is required to fetch a visual description.",
        )

    headings = [h for _, h in images]
    description = gemini_client.describe_streetview_images(
        image_bytes_list, headings
    )
    if not description:
        return None

    _set_cached(cache, cache_key, description)
    _save_cache(cache_path, cache)

    return (image_bytes_list, description)
