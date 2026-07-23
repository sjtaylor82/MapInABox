"""postal_codes.py — Offline postcode lookup for Map in a Box.

Bundled data comes from GeoNames' postal-code export (CC-BY 4.0; see
THIRD_PARTY_NOTICES.txt), reshaped into the same per-country, manifest-driven
layout as GeoFeatures: one gzip-compressed CSV per country plus a
manifest.json with each country's bounding box, decompressed straight into
memory on demand — no extraction to disk. A small in-memory LRU keeps the
most recently used countries parsed so repeat lookups in the same area are
free.

This is a nearest-point match against postcode centroids, not a boundary
lookup — accurate enough for "what postcode is this" in normal use, but it
can occasionally return a neighbouring postcode very close to a boundary,
and is coarser than live geocoding in rural areas with sparse data. For
Canada, the Netherlands, and the United Kingdom the bundled data only has
the first part of the postcode (e.g. a UK outward code like "SW1A" rather
than the full "SW1A 1AA") — GeoNames' free export doesn't include the
remainder for these three countries.

Public API
----------
PostalCodeLookup(path).lookup(lat, lon) -> (postcode, place_name, admin_name1) | None
"""

from __future__ import annotations

import csv
import gzip
import json
import os

from geo import dist_metres


class PostalCodeLookup:
    """Lazy, manifest-driven offline postcode lookup."""

    _COUNTRY_CACHE_LIMIT = 8
    # A lookup this far from the nearest known point isn't a postcode match,
    # it's open water or a genuine data gap - don't report a false result.
    _MAX_MATCH_KM = 60.0
    # Countries are selected by bounding box; this padding catches points
    # just outside a country's listed extent (coastal points, sparse data
    # near a border) without pulling in every country on the continent.
    _BBOX_PAD_DEG = 0.5

    def __init__(self, path: str):
        self._base = path
        self._manifest: dict = {}
        self._country_cache: dict[str, list] = {}
        self._country_cache_order: list[str] = []
        if not path or not os.path.isdir(path):
            return
        try:
            with open(os.path.join(path, "manifest.json"), encoding="utf-8") as f:
                self._manifest = json.load(f)
        except Exception:
            self._manifest = {}

    @property
    def available(self) -> bool:
        """True if the bundled dataset loaded successfully."""
        return bool(self._manifest)

    def _countries_near(self, lat: float, lon: float) -> list[str]:
        pad = self._BBOX_PAD_DEG
        result = []
        for country_code, meta in self._manifest.items():
            try:
                if (meta["lat_max"] + pad < lat or meta["lat_min"] - pad > lat or
                        meta["lon_max"] + pad < lon or meta["lon_min"] - pad > lon):
                    continue
                result.append(country_code)
            except Exception:
                continue
        return result

    def _load_country(self, country_code: str) -> list[tuple]:
        if country_code in self._country_cache:
            return self._country_cache[country_code]
        meta = self._manifest.get(country_code)
        if not meta:
            return []
        filename = meta.get("file", f"{country_code}.csv")
        gz_path = os.path.join(self._base, filename + ".gz")
        plain_path = os.path.join(self._base, filename)
        path = gz_path if os.path.exists(gz_path) else (
            plain_path if os.path.exists(plain_path) else None)
        if not path:
            return []

        rows = []
        try:
            open_func = gzip.open if path.endswith(".gz") else open
            with open_func(path, "rt", encoding="utf-8", newline="") as f:
                for row in csv.DictReader(f):
                    try:
                        rows.append((
                            row["postcode"],
                            row["place_name"],
                            row["admin_name1"],
                            float(row["lat"]),
                            float(row["lon"]),
                        ))
                    except (KeyError, ValueError):
                        continue
        except Exception:
            return []

        self._country_cache[country_code] = rows
        self._country_cache_order.append(country_code)
        while len(self._country_cache_order) > self._COUNTRY_CACHE_LIMIT:
            evicted = self._country_cache_order.pop(0)
            self._country_cache.pop(evicted, None)
        return rows

    def lookup(self, lat: float, lon: float) -> tuple[str, str, str] | None:
        """Return (postcode, place_name, admin_name1) for the nearest known
        point within range, or None if nothing usable is bundled nearby."""
        if not self._manifest:
            return None
        candidates = self._countries_near(lat, lon)
        if not candidates:
            return None

        best = None
        best_dist = None
        for country_code in candidates:
            for postcode, place, admin1, r_lat, r_lon in self._load_country(country_code):
                d = dist_metres(lat, lon, r_lat, r_lon)
                if best_dist is None or d < best_dist:
                    best_dist = d
                    best = (postcode, place, admin1)

        if best is None or best_dist is None or best_dist > self._MAX_MATCH_KM * 1000:
            return None
        return best
