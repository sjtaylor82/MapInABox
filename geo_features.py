"""Geographic-feature lookup for Map in a Box."""

import csv
import gzip
import json
import math
import os
import pickle
import re
import shutil
import tempfile

from app_paths import RESOURCE_DIR, USER_DIR
from distance_units import format_distance
from geo import bearing_deg, compass_name, haversine_m

GEO_FEATURES_DIR = os.path.join(RESOURCE_DIR, "GeoFeatures")


class GeoFeatures:
    """Loads country feature files on demand and checks nearby features.

    Uses a simple radius check — features are stored as centroids, so
    a generous radius is used to approximate "being inside" large features.
    """

    # Broader radii for X key panel and water/coastal context
    # Bays/gulfs are larger to cover full extent of bodies like Moreton Bay
    # Island groups tightened to avoid distant false matches
    _RADII_BROAD = {
        "H.OCN":  0.0,
        "H.SEA":  0.0,   # handled by KNOWN_OCEANS
        "H.GULF": 0.35,  # broad enough for local coastal context without distant matches
        "H.BAY":  0.2,
        "H.RF":   0.35,
        "H.RFS":  0.25,
        "H.STRT": 0.2,
        "H.CHAN": 0.2,
        "H.CHN":  0.2,
        "H.CHNL": 0.2,
        "H.LGN":  0.18,
        "H.RFC":  0.25,
        "H.SD":   0.22,
        "H.SHOL": 0.08,
        "H.SPIT": 0.06,
        "T.DES":  0.75,
        "T.DSRT": 0.75,
        "T.DUNE": 0.08,
        "T.ERG":  0.35,
        "T.GAP":  0.05,
        "T.GRGE": 0.06,
        "T.HDLD": 0.08,
        "T.MTS":  0.35,
        "T.HMDA": 0.25,
        "T.ISTH": 0.06,
        "T.KRST": 0.20,
        "T.PLN":  0.0,
        "T.PLAT": 0.0,
        "T.REG":  0.35,
        "T.RGN":  0.35,
        "T.PEN":  0.25,
        "T.CAPE": 0.12,
        "T.ISL":  0.06,
        "T.ISLET": 0.04,
        "T.ISLF": 0.06,
        "T.ISLM": 0.06,
        "T.ISLS": 0.05,
        "T.ISLT": 0.04,
        "T.SAND": 0.08,
        "T.CONT": 0.0,
        "L.LCTY": 0.45,
        "S.FRM":  0.25,
        "S.FRMS": 0.30,
        "S.HMSD": 0.25,
        "S.RNCH": 0.35,
        "S.RNCHS": 0.45,
    }
    _COUNTRY_CACHE_LIMIT = 16

    def __init__(self, path: str):
        self._base = path
        self._cache_dir = os.path.join(USER_DIR, "geo_features_cache")
        self._temp_dir = os.path.join(tempfile.gettempdir(), "miab_geo_features")
        self._manifest = {}
        self._country_cache = {}
        self._country_cache_order = []
        self._country_name_index = {}   # country_code -> {name_norm: [feat, ...]}
        self._country_name_sorted = {}  # country_code -> sorted list of name_norms
        if not path or not os.path.isdir(path):
            return
        try:
            with open(os.path.join(path, "manifest.json"), encoding="utf-8") as f:
                self._manifest = json.load(f)
        except Exception as exc:
            self._manifest = {}

    def _countries_for_box(self, lat_min, lat_max, lon_min, lon_max):
        result = []
        for country_code, meta in self._manifest.items():
            try:
                if (meta["lat_max"] < lat_min or meta["lat_min"] > lat_max or
                        meta["lon_max"] < lon_min or meta["lon_min"] > lon_max):
                    continue
                result.append(country_code)
            except Exception:
                continue
        return result

    def _country_source_path(self, country_code, meta):
        filename = meta.get("file", f"{country_code}.csv")
        plain_path = os.path.join(self._base, filename)
        gz_path = plain_path + ".gz"
        if os.path.exists(plain_path):
            return plain_path
        return gz_path if os.path.exists(gz_path) else None

    def _country_cache_path(self, country_code):
        return os.path.join(self._cache_dir, f"{country_code}.pkl")

    def _load_country_cache(self, country_code, source_path):
        cache_path = self._country_cache_path(country_code)
        if not os.path.exists(cache_path):
            return None
        try:
            with open(cache_path, "rb") as f:
                cached = pickle.load(f)
            if (cached.get("source_mtime") != os.path.getmtime(source_path)
                    or cached.get("source_size") != os.path.getsize(source_path)):
                return None
            features = cached.get("features") or []
            name_index = cached.get("name_index") or {}
            name_sorted = cached.get("name_sorted") or sorted(name_index.keys())
            return features, name_index, name_sorted
        except Exception:
            return None

    def _save_country_cache(self, country_code, source_path, features, name_index, name_sorted):
        try:
            os.makedirs(self._cache_dir, exist_ok=True)
            cache_path = self._country_cache_path(country_code)
            payload = {
                "source_mtime": os.path.getmtime(source_path),
                "source_size": os.path.getsize(source_path),
                "features": features,
                "name_index": name_index,
                "name_sorted": name_sorted,
            }
            with open(cache_path, "wb") as f:
                pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
        except Exception:
            pass

    def _load_country(self, country_code):
        if country_code in self._country_cache:
            return self._country_cache[country_code]
        meta = self._manifest.get(country_code)
        if not meta:
            return []
        path = self._country_source_path(country_code, meta)
        if not path:
            return []

        cached = self._load_country_cache(country_code, path)
        if cached:
            features, name_index, name_sorted = cached
            self._country_cache[country_code] = features
            self._country_name_index[country_code] = name_index
            self._country_name_sorted[country_code] = name_sorted
            self._country_cache_order.append(country_code)
            while len(self._country_cache_order) > self._COUNTRY_CACHE_LIMIT:
                evicted = self._country_cache_order.pop(0)
                self._country_cache.pop(evicted, None)
                self._country_name_index.pop(evicted, None)
                self._country_name_sorted.pop(evicted, None)
            return features

        features = []
        try:
            open_func = gzip.open if path.endswith(".gz") else open
            with open_func(path, "rt", encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    try:
                        name    = row["name"]
                        code    = row["feature_code"]
                        country = row.get("country_code", country_code)
                        type_label = GeoFeatures._JUMP_TYPE_LABELS.get(code, "")
                        features.append({
                            "name":         name,
                            "lat":          float(row["lat"]),
                            "lon":          float(row["lon"]),
                            "code":         code,
                            "country_code": country,
                            "name_norm":    GeoFeatures._jump_search_text(name),
                            "searchable":   GeoFeatures._jump_search_text(
                                                " ".join(p for p in (name, type_label, country) if p)),
                        })
                    except (KeyError, ValueError):
                        continue
        except Exception:
            return []
        # Build name index for fast jump-search lookups
        name_index = {}
        for feat in features:
            nn = feat["name_norm"]
            if nn:
                if nn not in name_index:
                    name_index[nn] = []
                name_index[nn].append(feat)
        self._country_cache[country_code] = features
        self._country_name_index[country_code] = name_index
        self._country_name_sorted[country_code] = sorted(name_index.keys())
        self._country_cache_order.append(country_code)
        self._save_country_cache(
            country_code,
            path,
            features,
            name_index,
            self._country_name_sorted[country_code],
        )
        while len(self._country_cache_order) > self._COUNTRY_CACHE_LIMIT:
            evicted = self._country_cache_order.pop(0)
            self._country_cache.pop(evicted, None)
            self._country_name_index.pop(evicted, None)
            self._country_name_sorted.pop(evicted, None)
        return features

    def cleanup_temp(self):
        try:
            shutil.rmtree(self._temp_dir, ignore_errors=True)
        except Exception:
            pass

    def _query_box(self, lat_min, lat_max, lon_min, lon_max, country_code=None):
        features = []
        countries = []
        if country_code and country_code in self._manifest:
            countries = [country_code]
        else:
            countries = self._countries_for_box(lat_min, lat_max, lon_min, lon_max)
        for cc in countries:
            for feat in self._load_country(cc):
                lat = feat["lat"]
                lon = feat["lon"]
                if lat_min <= lat <= lat_max and lon_min <= lon <= lon_max:
                    features.append(feat)
        return features

    def _nearby_features(self, lat, lon, radius_deg, country_code=None):
        """Yield features from grid cells around lat/lon."""
        for feat in self._query_box(
                max(-90.0, lat - radius_deg),
                min(90.0, lat + radius_deg),
                max(-180.0, lon - radius_deg * 1.5),
                min(180.0, lon + radius_deg * 1.5),
                country_code=country_code):
            yield feat

    _JUMP_CODES = {
        "T.DES", "T.DSRT", "T.MTS", "T.CAPE", "T.PEN",
        "T.ISL", "T.ISLET", "T.ISLF", "T.ISLM", "T.ISLS", "T.ISLT",
        "H.BAY", "H.BAYS", "H.CHAN", "H.CHN", "H.CHNL", "H.GULF",
        "H.LGN", "H.RF", "H.RFC", "H.RFS", "H.SD", "H.STRT",
        "L.LCTY", "S.FRM", "S.FRMS", "S.HMSD", "S.RNCH", "S.RNCHS",
    }
    _JUMP_TYPE_LABELS = {
        "H.BAY": "Bay", "H.BAYS": "Bays", "H.CHAN": "Channel",
        "H.CHN": "Channel", "H.CHNL": "Channel", "H.GULF": "Gulf",
        "H.LGN": "Lagoon", "H.RF": "Reef", "H.RFC": "Reef",
        "H.RFS": "Reefs", "H.SD": "Sound", "H.STRT": "Strait",
        "T.CAPE": "Cape", "T.DES": "Desert", "T.DSRT": "Desert",
        "T.ISL": "Island", "T.ISLET": "Islet", "T.ISLF": "Island",
        "T.ISLM": "Island", "T.ISLS": "Islands", "T.ISLT": "Islet",
        "T.MTS": "Mountain range", "T.PEN": "Peninsula",
        "L.LCTY": "Locality", "S.FRM": "Farm", "S.FRMS": "Farms",
        "S.HMSD": "Homestead", "S.RNCH": "Station or ranch",
        "S.RNCHS": "Stations or ranches",
    }
    _JUMP_TYPE_RANK = {
        "L.LCTY": 0,
        "T.ISL": 1, "T.ISLET": 1, "T.ISLF": 1, "T.ISLM": 1,
        "T.ISLS": 1, "T.ISLT": 1,
        "H.BAY": 2, "H.BAYS": 2, "H.CHAN": 2, "H.CHN": 2,
        "H.CHNL": 2, "H.GULF": 2, "H.LGN": 2, "H.RF": 2,
        "H.RFC": 2, "H.RFS": 2, "H.SD": 2, "H.STRT": 2,
        "T.CAPE": 3, "T.PEN": 3, "T.MTS": 3, "T.DES": 3,
        "T.DSRT": 3,
        "S.HMSD": 4, "S.FRM": 5, "S.FRMS": 5,
        "S.RNCH": 5, "S.RNCHS": 5,
    }

    @staticmethod
    def _jump_search_text(text: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (text or "").lower())).strip()

    # Common filler words to strip from queries before matching
    _FILLER_WORDS = frozenset({"the", "a", "an", "of", "at", "in", "on"})

    @classmethod
    def _strip_fillers(cls, text: str) -> str:
        """Remove common filler words from a normalised query."""
        return " ".join(w for w in text.split() if w not in cls._FILLER_WORDS)

    def _jump_feature_match(self, feat, query_norm: str):
        code = feat.get("code", "")
        name = feat.get("name", "")
        if code not in self._JUMP_CODES or not name:
            return None
        type_label = self._JUMP_TYPE_LABELS.get(code, "Feature")
        country_code = feat.get("country_code", "")
        # Use pre-computed values when available (populated by _load_country)
        name_norm  = feat.get("name_norm")  or self._jump_search_text(name)
        searchable = feat.get("searchable") or self._jump_search_text(
            " ".join(p for p in (name, type_label, country_code) if p)
        )
        q_stripped = self._strip_fillers(query_norm)

        def _check(q):
            if q == name_norm:                                    return 0
            if name_norm.startswith(q):                           return 1
            if searchable.startswith(q) or f" {q}" in searchable: return 2
            if len(q) >= 3 and q in searchable:                   return 3
            return None

        match_rank = _check(query_norm)
        if match_rank is None and q_stripped and q_stripped != query_norm:
            r2 = _check(q_stripped)
            if r2 is not None:
                match_rank = r2 + 1   # slightly lower priority than non-stripped match
        if match_rank is None:
            return None
        base_label = f"{name}, {type_label}"
        if country_code:
            base_label = f"{base_label}, {country_code}"
        dedupe_key = (
            name.lower(), code, country_code,
            round(feat["lat"], 4), round(feat["lon"], 4),
        )
        return (
            base_label, feat["lat"], feat["lon"], name,
            match_rank, self._JUMP_TYPE_RANK.get(code, 9), dedupe_key
        )

    def _feature_name_with_type(self, name: str, code: str) -> str:
        """Return a spoken feature name with a type suffix where helpful."""
        type_label = {
            "S.FRM":  "Farm",
            "S.FRMS": "Farms",
            "S.HMSD": "Homestead",
        }.get(code, "")
        return f"{name} {type_label}".strip() if type_label else name

    def _nearest_feature_by_radii(self, lat: float, lon: float, radii: dict,
                                   country_code: str = None):
        """Search nearby features against a code->radius(deg) map. Returns
        (name_with_type, feat) for the closest in-range match, or (None, None)."""
        best      = None
        best_dist = float("inf")
        best_feat = None
        for feat in self._nearby_features(lat, lon, max(radii.values() or [0.0]), country_code):
            r = radii.get(feat.get("code", ""), 0.0)
            if r == 0.0:
                continue
            dlat = abs(feat["lat"] - lat)
            dlon = abs(feat["lon"] - lon)
            if dlat > r or dlon > r * 1.5:
                continue
            dist = math.sqrt(dlat*dlat + dlon*dlon)
            if dist < r and dist < best_dist:
                best_dist = dist
                best      = self._feature_name_with_type(feat["name"], feat.get("code", ""))
                best_feat = feat
        return best, best_feat

    def lookup_any(self, lat: float, lon: float, country_code: str = None) -> str:
        """Return the name of ANY nearby feature using broad radii, or ''."""
        best, best_feat = self._nearest_feature_by_radii(
            lat, lon, self._RADII_BROAD, country_code)
        if not best:
            return ""
        from geo import bearing_deg, compass_name
        dist_km = haversine_m(lat, lon, best_feat["lat"], best_feat["lon"]) / 1000.0
        compass = compass_name(bearing_deg(lat, lon, best_feat["lat"], best_feat["lon"]))
        dist_str = format_distance(dist_km * 1000)
        return f"{best} {dist_str} {compass}"

    def lookup_precise_label(self, lat: float, lon: float, country_code: str = None) -> str:
        """Return a feature only when the cursor is very close to its point."""
        limits = {
            "T.MTS": 0.04,
            "T.DES": 0.15,
            "T.DSRT": 0.15,
            "H.RF": 0.06,
            "H.RFS": 0.06,
            "H.GULF": 0.06,
            "H.BAY": 0.03,
            "H.STRT": 0.04,
            "H.CHAN": 0.04,
            "H.CHN": 0.04,
            "H.CHNL": 0.04,
            "H.LGN": 0.04,
            "H.RFC": 0.06,
            "H.SD": 0.05,
            "H.SHOL": 0.03,
            "H.SPIT": 0.025,
            "T.DUNE": 0.03,
            "T.ERG": 0.08,
            "T.GAP": 0.02,
            "T.GRGE": 0.025,
            "T.HDLD": 0.03,
            "T.HMDA": 0.06,
            "T.ISTH": 0.025,
            "T.KRST": 0.05,
            "T.CAPE": 0.03,
            "T.PEN": 0.04,
            "T.ISL": 0.025,
            "T.ISLET": 0.02,
            "T.ISLF": 0.025,
            "T.ISLM": 0.025,
            "T.ISLS": 0.03,
            "T.ISLT": 0.02,
            "T.SAND": 0.03,
            "L.LCTY": 0.04,
            "S.FRM": 0.04,
            "S.FRMS": 0.05,
            "S.HMSD": 0.04,
            "S.RNCH": 0.05,
            "S.RNCHS": 0.06,
        }
        best, _ = self._nearest_feature_by_radii(lat, lon, limits, country_code)
        return best or ""

    def context_items(self, lat: float, lon: float, limit: int = 3, country_code: str = None) -> list[str]:
        """Return compact nearby context items with distances."""
        limits = {
            "H.BAY": 0.70, "H.BAYS": 0.70, "H.GULF": 0.80,
            "H.LGN": 0.25, "H.STRT": 0.35, "H.CHAN": 0.35,
            "H.CHN": 0.35, "H.CHNL": 0.35,
            "H.RF": 0.45, "H.RFS": 0.45, "H.RFC": 0.45,
            "T.ISL": 0.50, "T.ISLF": 0.50, "T.ISLM": 0.50,
            "T.ISLS": 0.50, "T.ISLET": 0.25, "T.ISLT": 0.25,
            "T.PEN": 0.35, "T.CAPE": 0.20,
            "L.LCTY": 0.45,
            "S.FRM": 0.25, "S.FRMS": 0.30, "S.HMSD": 0.25,
            "S.RNCH": 0.35, "S.RNCHS": 0.45,
        }
        candidates = []
        seen = set()
        for feat in self._nearby_features(lat, lon, max(limits.values()), country_code):
            code = feat.get("code", "")
            r = limits.get(code, 0.0)
            if r == 0.0:
                continue
            dlat = abs(feat["lat"] - lat)
            dlon = abs(feat["lon"] - lon)
            if dlat > r or dlon > r * 1.5:
                continue
            dist = math.sqrt(dlat*dlat + dlon*dlon)
            if dist < r and feat["name"] not in seen:
                seen.add(feat["name"])
                candidates.append((dist * 111.0, code,
                                   self._feature_name_with_type(feat["name"], code),
                                   feat["lat"], feat["lon"]))
        candidates.sort(key=lambda item: item[0])
        from geo import bearing_deg, compass_name
        result = []
        for km, _code, name, flat, flon in candidates[:limit]:
            dist_text = format_distance(km * 1000)
            compass = compass_name(bearing_deg(lat, lon, flat, flon))
            result.append(f"{name} {dist_text} {compass}")
        return result

    def jump_candidates(self, query: str, lat: float = None, lon: float = None, country_code: str = None) -> list[tuple[str, float, float, str, int, int]]:
        """Return jumpable localities, natural features and property names."""
        q = self._jump_search_text(query)
        if not q:
            return []
        raw_matches = []
        seen = set()
        searched_countries = set()

        def add_match(feat):
            match = self._jump_feature_match(feat, q)
            if not match:
                return
            label, flat, flon, name, match_rank, type_rank, dedupe_key = match
            if dedupe_key in seen:
                return
            seen.add(dedupe_key)
            dist_sort = ((flat - lat) ** 2 + (flon - lon) ** 2
                         if lat is not None and lon is not None else 0.0)
            raw_matches.append((
                label, flat, flon, name, match_rank, type_rank, dist_sort
            ))

        if lat is not None and lon is not None:
            for feat in self._nearby_features(lat, lon, 2.0, country_code=country_code):
                add_match(feat)

        if lat is not None and lon is not None and len(raw_matches) < 50:
            country_codes = (
                [country_code]
                if country_code and country_code in self._manifest
                else self._countries_for_box(
                    max(-90.0, lat - 4.0), min(90.0, lat + 4.0),
                    max(-180.0, lon - 6.0), min(180.0, lon + 6.0))
            )
            import bisect as _bisect
            q_stripped = self._strip_fillers(q)
            for cc in country_codes:
                searched_countries.add(cc)
                # Ensure country is loaded (populates name index as side-effect)
                self._load_country(cc)
                name_index  = self._country_name_index.get(cc, {})
                sorted_names = self._country_name_sorted.get(cc, [])

                def _add_indexed(feat, extra_rank=0):
                    match = self._jump_feature_match(feat, q)
                    if not match:
                        return
                    label, flat, flon, name, match_rank, type_rank, dedupe_key = match
                    if dedupe_key in seen:
                        return
                    seen.add(dedupe_key)
                    dist_sort = (flat - lat) ** 2 + (flon - lon) ** 2
                    raw_matches.append((
                        label, flat, flon, name, match_rank + 1 + extra_rank,
                        type_rank, dist_sort
                    ))

                # Exact name match — O(1)
                for feat in name_index.get(q, []):
                    _add_indexed(feat)
                # Also try filler-stripped query exact match
                if q_stripped and q_stripped != q:
                    for feat in name_index.get(q_stripped, []):
                        _add_indexed(feat, extra_rank=1)

                # Prefix matches — O(k) where k = matching prefix range
                lo = _bisect.bisect_left(sorted_names, q)
                for nn in sorted_names[lo:]:
                    if not nn.startswith(q):
                        break
                    if nn == q:
                        continue  # already handled above
                    for feat in name_index[nn]:
                        _add_indexed(feat)

                # No full-scan fallback — exact and prefix index coverage is sufficient.
                # Contains/searchable matching across an entire country is O(n) and
                # the cause of the search delay.  Users search by name, not by type label.


        raw_matches.sort(key=lambda item: (item[4], item[5], item[6], item[3].lower()))
        raw_matches = raw_matches[:200]
        label_counts = {}
        for label, *_rest in raw_matches:
            label_counts[label] = label_counts.get(label, 0) + 1
        matches = []
        for label, lat, lon, name, match_rank, type_rank, _dist_sort in raw_matches:
            if label_counts.get(label, 0) > 1:
                parts = label.rsplit(', ', 1)
                has_country = len(parts) == 2 and len(parts[1]) == 2 and parts[1].isupper()
                if not has_country:
                    label = f"{label}, {lat:.2f}, {lon:.2f}"
            matches.append((label, lat, lon, name, match_rank, type_rank))
        return matches

    def nearby(self, lat: float, lon: float, country_code: str = None) -> list:
        """Return list of (name, feature_code, dist_km, compass) using broad radii for X key panel."""
        from geo import bearing_deg, compass_name
        results = []
        seen    = set()
        for feat in self._nearby_features(lat, lon, max(self._RADII_BROAD.values() or [0.0]), country_code):
            r = self._RADII_BROAD.get(feat["code"], 0.0)
            if r == 0.0:
                continue
            dlat = abs(feat["lat"] - lat)
            dlon = abs(feat["lon"] - lon)
            if dlat > r or dlon > r * 1.5:
                continue
            dist = math.sqrt(dlat*dlat + dlon*dlon)
            if dist < r and feat["name"] not in seen:
                seen.add(feat["name"])
                dist_km = haversine_m(lat, lon, feat["lat"], feat["lon"]) / 1000.0
                compass = compass_name(bearing_deg(lat, lon, feat["lat"], feat["lon"]))
                results.append((self._feature_name_with_type(feat["name"], feat["code"]),
                                feat["code"], dist_km, compass))
        return results
