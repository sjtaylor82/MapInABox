"""cache_utils.py - shared JSON cache helpers for Map in a Box."""

import json
import os
import time


def _load_cache(cache_path: str) -> dict:
    """Load cache data from a JSON file."""
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[Cache] Cache load failed for {cache_path}: {e}")
    return {}


def _save_cache(cache_path: str, cache: dict) -> None:
    """Save cache data to a JSON file."""
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[Cache] Cache save failed for {cache_path}: {e}")


def _get_cached(cache: dict, key: str, ttl_days: int) -> str | None:
    """Return cached text if the entry is still fresh."""
    entry = cache.get(key)
    if not isinstance(entry, dict):
        return None
    if (time.time() - entry.get("ts", 0)) / 86400 > ttl_days:
        return None
    return entry.get("text")


def _set_cached(cache: dict, key: str, value: str) -> None:
    """Store cached text with a timestamp."""
    cache[key] = {"text": value, "ts": time.time()}
