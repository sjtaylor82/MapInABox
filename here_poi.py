"""here_poi.py — HERE POI detail and ratings lookup for Map in a Box.

Provides address, phone, opening hours and ratings for points of interest
via the HERE Discover and Lookup APIs.  Street data comes from OSM/Overpass.

Classes
-------
HereClient
    fetch_poi_detail(name, lat, lon) → dict
    fetch_poi_rating(here_id, name)  → (rating, count)
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
import urllib.parse
import urllib.request
from typing import Optional

_POI_CACHE_DAYS  = 30
_RATE_CACHE_DAYS = 90


class HereClient:
    def __init__(self, api_key: str, cache_dir: str) -> None:
        self._key             = api_key
        self._cache_dir       = cache_dir
        self._poi_cache_path  = os.path.join(cache_dir, "here_poi_cache.json")
        self._rate_cache_path = os.path.join(cache_dir, "here_rating_cache.json")
        self._poi_cache:  dict = self._load_json(self._poi_cache_path)
        self._rate_cache: dict = self._load_json(self._rate_cache_path)
        self._lock = threading.Lock()

    def fetch_poi_rating(
        self, here_id: str, name: str
    ) -> tuple[Optional[float], Optional[int]]:
        """Return (average_rating, review_count) for a HERE POI id."""
        with self._lock:
            cached = self._rate_cache.get(here_id)
        if cached and (time.time() - cached.get("ts", 0)) < _RATE_CACHE_DAYS * 86400:
            return cached.get("rating"), cached.get("count")
        try:
            params = urllib.parse.urlencode({"id": here_id, "apiKey": self._key})
            req = urllib.request.Request(
                f"https://lookup.search.hereapi.com/v1/lookup?{params}",
                headers={"User-Agent": "MapInABox/1.0"},
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode())
            rating = data.get("averageRating")
            count  = data.get("ratingCount")
            if rating is not None:
                rating = round(float(rating), 1)
            with self._lock:
                self._rate_cache[here_id] = {
                    "rating": rating, "count": count, "ts": time.time()}
                self._save_json(self._rate_cache_path, self._rate_cache)
            return rating, count
        except Exception as exc:
            print(f"[HERE] fetch_poi_rating failed for {name}: {exc}")
            return None, None

    @staticmethod
    def _name_similarity(query: str, result: str) -> float:
        """Return 0.0–1.0 word-overlap score between query and result names.

        Only strips true grammar filler (the, a, of, etc.) — business type
        words like cafe, hotel, pharmacy are kept because they distinguish
        businesses (e.g. 'Chino Cafe' vs 'Hotel Chino').
        """
        import re as _re
        _FILLER = frozenset({
            "the", "a", "an", "and", "of", "at", "in", "on", "by", "for",
        })
        def _words(s):
            return set(
                w for w in _re.sub(r'[^a-z0-9\s]', '', s.lower()).split()
                if w and w not in _FILLER and len(w) > 1
            )
        q = _words(query)
        r = _words(result)
        if not q:
            return 1.0  # nothing meaningful to compare
        return len(q & r) / len(q)

    def fetch_poi_detail(self, name: str, lat: float, lon: float) -> dict:
        """Return address, phone, website and opening hours for a POI."""
        print(f"[HERE] fetch_poi_detail called for '{name}' at ({lat}, {lon})")
        cache_key = f"detail_{name.lower().replace(' ','_')}_{round(lat,4)}_{round(lon,4)}"
        with self._lock:
            cached = self._poi_cache.get(cache_key)
        if cached and (time.time() - cached.get("ts", 0)) < _POI_CACHE_DAYS * 86400:
            print(f"[HERE] Cache hit for '{name}'")
            return cached.get("detail", {})
        try:
            params = urllib.parse.urlencode({
                "at":     f"{lat},{lon}",
                "q":      name,
                "limit":  3,
                "lang":   "en-US",
                "apiKey": self._key,
            })
            req = urllib.request.Request(
                f"https://discover.search.hereapi.com/v1/discover?{params}",
                headers={"User-Agent": "MapInABox/1.0"},
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode())
            items = data.get("items", [])
            if not items:
                print(f"[HERE] No results returned for '{name}'")
                return {}

            # Pick best matching result — reject fuzzy mismatches and distant results
            item = None
            for candidate in items:
                cand_name = (candidate.get("title") or "").strip()
                score = self._name_similarity(name, cand_name)
                # Distance check
                cand_pos = candidate.get("position", {})
                clat = cand_pos.get("lat", lat)
                clon = cand_pos.get("lng", lon)
                dlat = (clat - lat) * 111_000
                dlon = (clon - lon) * 111_000 * math.cos(math.radians(lat))
                dist_m = math.sqrt(dlat * dlat + dlon * dlon)
                print(f"[HERE] Candidate '{cand_name}' score={score:.2f} dist={dist_m:.0f}m for query '{name}'")
                if score >= 0.4 and dist_m <= 150:
                    item = candidate
                    print(f"[HERE] Accepted '{cand_name}'")
                    break
                elif score >= 0.4:
                    print(f"[HERE] Rejected '{cand_name}' — too far ({dist_m:.0f}m)")
            if item is None:
                print(f"[HERE] No close match for '{name}' — skipping")
                return {}

            addr_obj   = item.get("address", {})
            contacts   = item.get("contacts", [{}])
            oh_list    = item.get("openingHours", [])
            categories = item.get("categories", [])

            phone = website = ""
            for ct in contacts:
                for ph in ct.get("phone", []):
                    phone = ph.get("value", ""); break
                for wb in ct.get("www", []):
                    website = wb.get("value", ""); break

            oh_text = ""
            if oh_list:
                oh      = oh_list[0]
                is_open = oh.get("isOpen")
                texts   = oh.get("text", [])
                status  = "Open now." if is_open else ("Closed" if is_open is False else "")
                oh_text = (status + (". " if status and texts else "") + "; ".join(texts)).strip()

            detail = {
                "address":       addr_obj.get("label", ""),
                "phone":         phone,
                "website":       website,
                "opening_hours": oh_text,
                "here_id":       item.get("id", ""),
                "kind":          categories[0].get("name", "").lower() if categories else "",
            }
            with self._lock:
                self._poi_cache[cache_key] = {"detail": detail, "ts": time.time()}
                self._save_json(self._poi_cache_path, self._poi_cache)
            return detail
        except Exception as exc:
            print(f"[HERE] fetch_poi_detail failed for {name}: {exc}")
            return {}

    @staticmethod
    def _load_json(path: str) -> dict:
        try:
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    @staticmethod
    def _save_json(path: str, data: dict) -> None:
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception as exc:
            print(f"[HERE] save_json {path} failed: {exc}")
