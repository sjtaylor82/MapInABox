"""streetview.py — Google Street View imagery lookup with vision analysis.

Fetches two Street View frames at a given coordinate (one in each direction
along the street) and uses Mistral to describe what is visible from street
level or an indoor/place preview - crossings, entrances, stairs, signage, and
other access features.

Parallel to satellite.py in structure and calling convention.
"""

import urllib.parse
import urllib.request
import json
from typing import Optional, Tuple
from logging_utils import miab_log

from cache_utils import _get_cached, _load_cache, _save_cache, _set_cached


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
        miab_log("errors", f"[StreetView] Metadata check failed: {e}", None)
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
        miab_log("errors", f"[StreetView] Image fetch failed (heading {heading:.0f}deg): {e}", None)
        return None


# ── Public entry point ─────────────────────────────────────────────────────────

def lookup_streetview_description(
    lat: float,
    lon: float,
    google_api_key: str = "",
    mistral_client=None,
    street_heading: Optional[float] = None,
    cache_path: str = "streetview_cache.json",
    include_images: bool = True,
    mode: str = "explore",
) -> Optional[Tuple[list, str]]:
    """Fetch Street View imagery and return image bytes list + description.

    street_heading, if supplied, is the compass bearing the user is currently
    travelling along.  We fetch that direction and its opposite so both sides
    of the street are covered with meaningful direction labels in the
    description.  When None we default to north (0 deg) and south (180 deg).

    Returns (image_bytes_list, description) or None if no coverage or error.
    image_bytes_list contains 1 or 2 JPEG byte strings for display unless
    include_images is False, in which case an empty list is returned.
    """
    if not google_api_key:
        return None

    h1 = street_heading if street_heading is not None else 0.0
    h2 = _opposite(h1)
    mode = (mode or "explore").strip().lower()
    mode_tag = "nav" if mode == "navigation" else "exp"
    cache_key = f"sv_v3_{mode_tag}_{lat:.4f}_{lon:.4f}_{h1:.0f}_{h2:.0f}"
    cache = _load_cache(cache_path)
    cached_desc = _get_cached(cache, cache_key, ttl_days=30)

    if cached_desc and not include_images:
        miab_log("verbose", f"[StreetView] Cache hit for {cache_key} (text only).", None)
        return ([], cached_desc)

    # ── Coverage check ─────────────────────────────────────────────────────
    miab_log("verbose", f"[StreetView] Checking coverage at ({lat:.4f}, {lon:.4f})...", None)
    if not _streetview_available(lat, lon, google_api_key):
        miab_log("verbose", "[StreetView] No Street View coverage at this location.", None)
        return None

    mistral_ready = bool(mistral_client and getattr(mistral_client, "is_configured", False))

    # ── Determine headings ─────────────────────────────────────────────────
    # ── Fetch images ───────────────────────────────────────────────────────
    miab_log("verbose", f"[StreetView] Fetching images (headings {h1:.0f}deg and {h2:.0f}deg)...", None)
    img_a = _fetch_streetview_image(lat, lon, h1, google_api_key)
    img_b = _fetch_streetview_image(lat, lon, h2, google_api_key)

    images = []   # list of (bytes, heading) for Mistral
    if img_a:
        images.append((img_a, h1))
    if img_b:
        images.append((img_b, h2))

    if not images:
        miab_log("verbose", "[StreetView] Image fetch returned no usable frames.", None)
        return None

    image_bytes_list = [img for img, _ in images]

    # ── Description (cached text reused with fresh images) ─────────────────
    if cached_desc:
        miab_log("verbose", f"[StreetView] Cache hit for {cache_key}.", None)
        return (image_bytes_list if include_images else [], cached_desc)

    if not mistral_ready:
        return (
            image_bytes_list if include_images else [],
            "Street View imagery loaded. A Mistral API key is required to fetch a visual description.",
        )

    headings = [h for _, h in images]
    try:
        description = mistral_client.describe_streetview_images(image_bytes_list, headings, mode=mode)
    except Exception as exc:
        miab_log("errors", f"[StreetView] Mistral description failed: {exc}", None)
        description = ""
    if not description:
        description = (
            "Street View imagery loaded, but Mistral could not generate a description right now."
        )

    if description and "Mistral could not generate a description" not in description:
        _set_cached(cache, cache_key, description)
        _save_cache(cache_path, cache)

    return (image_bytes_list if include_images else [], description)
