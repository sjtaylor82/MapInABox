"""satellite.py — Google Maps satellite imagery lookup with vision analysis.

Fetches satellite/aerial imagery at a given coordinate and uses Gemini 2.5 Flash Lite
to provide a rich, detailed description of the landscape suitable for accessibility.
"""

import json
import os
import time
import urllib.parse
import urllib.request
from typing import Optional, Tuple


def lat_lon_to_tile_url(lat: float, lon: float, zoom: int, api_key: str) -> str:
    """Build Google Maps Static API URL for satellite tile at given coordinate.

    Args:
        lat: Latitude
        lon: Longitude
        zoom: Zoom level (13-15 for city/region scale)
        api_key: Google Maps API key

    Returns:
        Full URL to fetch satellite image
    """
    params = {
        "center": f"{lat},{lon}",
        "zoom": str(zoom),
        "size": "640x640",
        "maptype": "satellite",
        "key": api_key,
        "style": "feature:all|element:labels|visibility:off",
    }
    base_url = "https://maps.googleapis.com/maps/api/staticmap"
    return f"{base_url}?{urllib.parse.urlencode(params)}"


def fetch_satellite_image(lat: float, lon: float, zoom: int, api_key: str) -> Optional[bytes]:
    """Fetch satellite image bytes from Google Maps Static API.

    Args:
        lat: Latitude
        lon: Longitude
        zoom: Zoom level
        api_key: Google Maps API key

    Returns:
        Image bytes (JPEG) or None if unavailable/error
    """
    if not api_key or not api_key.strip():
        return None

    url = lat_lon_to_tile_url(lat, lon, zoom, api_key)

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MapInABox/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read()
            # Check if response is an error placeholder (Google returns specific error images)
            if len(data) < 1000:  # Error images are typically very small
                return None
            return data
    except Exception as e:
        print(f"[Satellite] Fetch failed at ({lat}, {lon}): {e}")
        return None


def _load_cache(cache_path: str) -> dict:
    """Load cache from JSON file."""
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[Satellite] Cache load failed: {e}")
    return {}


def _save_cache(cache_path: str, cache: dict) -> None:
    """Save cache to JSON file."""
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[Satellite] Cache save failed: {e}")


def _get_cached(cache: dict, key: str, ttl_days: int = 90) -> Optional[str]:
    """Get cached value if fresh."""
    entry = cache.get(key)
    if not isinstance(entry, dict):
        return None
    if (time.time() - entry.get("ts", 0)) / 86400 > ttl_days:
        return None
    return entry.get("text")


def _set_cached(cache: dict, key: str, value: str) -> None:
    """Set cache value with timestamp."""
    cache[key] = {"text": value, "ts": time.time()}


def lookup_satellite_description(
    lat: float,
    lon: float,
    zoom: int = 15,
    google_api_key: str = "",
    gemini_client = None,
    cache_path: str = "satellite_cache.json"
) -> Optional[Tuple[bytes, str]]:
    """Fetch satellite image and return image bytes + description.

    Args:
        lat: Latitude
        lon: Longitude
        zoom: Zoom level (default 15 for city/neighborhood scale)
        google_api_key: Google Maps API key
        gemini_client: GeminiClient instance for vision analysis
        cache_path: Path to cache file

    Returns:
        Tuple of (image_bytes, description) or None if unavailable
    """
    if not google_api_key:
        return None

    # Build cache key (4 decimal places)
    cache_key = f"sat_{lat:.4f}_{lon:.4f}"

    # Load/check cache
    cache = _load_cache(cache_path)
    cached_desc = _get_cached(cache, cache_key)

    # Fetch image
    image_bytes = fetch_satellite_image(lat, lon, zoom, google_api_key)
    if not image_bytes:
        return None

    # If description is cached, return cached + fresh image
    if cached_desc:
        return (image_bytes, cached_desc)

    if not gemini_client:
        return (
            image_bytes,
            "Satellite imagery loaded. A Gemini API key is required to fetch a visual description.",
        )

    # Get description from Gemini
    description = gemini_client.describe_satellite_image(image_bytes, cache_key)
    if not description:
        return None

    # Save description to cache
    _set_cached(cache, cache_key, description)
    _save_cache(cache_path, cache)

    return (image_bytes, description)
