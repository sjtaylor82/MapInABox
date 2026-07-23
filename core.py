import time

_PROCESS_START_T0 = time.perf_counter()

import csv
import gzip
import json
import math
import os
import re
import pickle
import shutil
import tempfile
import threading
import urllib.parse
import urllib.request

from logging_utils import miab_log
from i18n import _, set_language
from speech_dispatch import SpeechDispatch, braille as _braille, speak as _speak
from wx_utils import IS_MAC, MSAAListBox, _log_key_event, _primary_down
from lookups import LookupsMixin
from nav import NavMixin
from walk import WalkMixin
from tools import ToolsMixin
from free import FreeMixin
try:
    from satellite import lookup_satellite_description
except ImportError:
    lookup_satellite_description = None

try:
    from streetview import lookup_streetview_description
except ImportError:
    lookup_streetview_description = None

try:
    from updater import UpdateChecker
except ImportError:
    UpdateChecker = None

import io
from PIL import Image

os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "1"
import numpy as np
import pandas as pd
import pygame
import wx

def _shortcut_label(primary: str) -> str:
    """Format a shortcut label for the current platform."""
    return primary if not IS_MAC else primary.replace("Ctrl", "Cmd")

# ── Sub-modules ──────────────────────────────────────────────────
from geo import (
    bearing_deg,
    compass_name,
    dist_km,
    dist_metres,
    haversine_m,
    nearest_point_on_segment,
)
from overpass_client import OverpassClient
from transit_lookup import TransitLookup
from free import FreeExploreEngine
from nav import NavigationEngine
from here_poi import HereClient as HerePoi
import mall_directory
from postal_codes import PostalCodeLookup
from network_utils import NETWORK_UNAVAILABLE_MESSAGE
from app_paths import (
    APP_DIR, CACHE_DIR, EDUCATION_EDITION, PORTABLE_MODE, RESOURCE_DIR,
    USER_DIR,
)
from distance_units import (
    format_distance, format_distance_label, format_height, set_unit_system,
)

import sys as _sys
APP_NAME      = 'Map in a Box'
APP_VERSION   = '1.0.0.34'

POI_LIVE_COOLDOWN_SECS = 3.0
POI_BACKGROUND_WAIT_SECS = 2.0

# Bundled read-only resources — inside the executable bundle or source tree.
BASE_DIR = RESOURCE_DIR

for _d in (USER_DIR, CACHE_DIR):
    os.makedirs(_d, exist_ok=True)

# ── Bundled resources (read-only) ────────────────────────────────────────────
CSV_PATH               = os.path.join(BASE_DIR,  "worldcities.csv.gz")
FACTS_PATH             = os.path.join(BASE_DIR,  "facts.json")
SOUNDS_DIR             = os.path.join(BASE_DIR,  "sounds")
COUNTRY_DIR            = os.path.join(SOUNDS_DIR, "countries")
REGION_DIR             = os.path.join(SOUNDS_DIR, "regions")
GEO_FEATURES_DIR       = os.path.join(BASE_DIR,  "GeoFeatures")
POSTAL_CODES_DIR       = os.path.join(BASE_DIR,  "PostalCodes")

# ── User data (AppData, or Data in portable mode) ────────────────────────────
SETTINGS_PATH          = os.path.join(USER_DIR,  "settings.json")
SUPPRESSED_POIS_PATH   = os.path.join(USER_DIR,  "suppressed_pois.json")
RENAMED_POIS_PATH      = os.path.join(USER_DIR,  "renamed_pois.json")
PERSONAL_POIS_PATH     = os.path.join(USER_DIR,  "personal_pois.json")
USER_SOUNDS_DIR        = os.path.join(USER_DIR,  "sounds")
USER_COUNTRY_DIR       = os.path.join(USER_SOUNDS_DIR, "countries")
USER_REGION_DIR        = os.path.join(USER_SOUNDS_DIR, "regions")
for _d in (USER_COUNTRY_DIR, USER_REGION_DIR):
    os.makedirs(_d, exist_ok=True)

# ── Caches (local AppData, or Data\Cache in portable mode) ──────────────────
CACHE_PATH             = os.path.join(CACHE_DIR, "worldcities.pkl")
WIKI_CACHE_PATH        = os.path.join(CACHE_DIR, "wiki_cache.json")
PLACE_CACHE_PATH       = os.path.join(CACHE_DIR, "place_cache.json")
AIRPORTS_CSV_PATH      = os.path.join(CACHE_DIR, "airports.csv")
AIRPORTS_CSV_SEED      = os.path.join(BASE_DIR,  "airports.csv.gz")
AIRPORTS_CSV_URL       = "https://davidmegginson.github.io/ourairports-data/airports.csv"
PLACE_NAME_CLOSE_KM = 5.0
# Keep remote-area place labels conservative so we do not announce
# faraway towns when there is no local feature match.
NEAREST_PLACE_FALLBACK_KM = 20.0

# ── Geographic features (deserts, mountain ranges, oceans etc.) ──────────────

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
AIRPORTS_STALE_DAYS = 90


# One shared Overpass client used by all callers in this module.
_overpass = OverpassClient()

COUNTRY_ALIASES = {
    "United States":   "United States of America",
    "USA":             "United States of America",
    "United Kingdom":  "United Kingdom",
    "UK":              "United Kingdom",
    "UAE":             "United Arab Emirates",
    "United Arab Emirates": "United Arab Emirates",
    "Russia":          "Russian Federation",
    "South Korea":     "Republic of Korea",
    "North Korea":     "Democratic People's Republic of Korea",
    "Czech Republic":  "Czechia",
    "Central African Rep.": "Central African Republic",
    "Central African Rep":  "Central African Republic",
    "Ivory Coast":     "Cote d'Ivoire",
    "Syria":           "Syrian Arab Republic",
    "Iran":            "Iran",
    "Bolivia":         "Bolivia",
    "Venezuela":       "Venezuela",
    "Tanzania":        "Tanzania",
    "Moldova":         "Moldova",
    # Australian external territories
    "Norfolk Island":              "Australia",
    "Christmas Island":            "Australia",
    "Cocos (Keeling) Islands":     "Australia",
    "Cocos Islands":               "Australia",
    "Heard Island":                "Australia",
    "Heard Island and McDonald Islands": "Australia",
    "Ashmore and Cartier Islands": "Australia",
    "Coral Sea Islands":           "Australia",
    # NZ territories
    "Niue":            "New Zealand",
    "Tokelau":         "New Zealand",
    "Cook Islands":    "New Zealand",
    # UK territories
    "Falkland Islands":          "United Kingdom",
    "Gibraltar":                 "United Kingdom",
    "Bermuda":                   "United Kingdom",
    "Cayman Islands":            "United Kingdom",
    "British Virgin Islands":    "United Kingdom",
    "Turks and Caicos Islands":  "United Kingdom",
    "Saint Helena":              "United Kingdom",
    "Pitcairn":                  "United Kingdom",
    # French territories
    "French Polynesia":          "France",
    "New Caledonia":             "France",
    "Reunion":                   "France",
    "Martinique":                "France",
    "Guadeloupe":                "France",
    "Mayotte":                   "France",
    "French Guiana":             "France",
    "Saint Pierre and Miquelon": "France",
    "Wallis and Futuna":         "France",
    # US territories
    "Puerto Rico":               "United States of America",
    "Guam":                      "United States of America",
    "U.S. Virgin Islands":       "United States of America",
    "American Samoa":            "United States of America",
    "Northern Mariana Islands":  "United States of America",
}

# Territories whose continent differs from their parent country
CONTINENT_OVERRIDES = {
    # French Pacific/Indian Ocean territories
    "New Caledonia":             "Oceania",
    "French Polynesia":          "Oceania",
    "Wallis and Futuna":         "Oceania",
    "Reunion":                   "Africa",
    "Mayotte":                   "Africa",
    "French Guiana":             "South America",
    "Martinique":                "North America",
    "Guadeloupe":                "North America",
    "Saint Pierre and Miquelon": "North America",
    # Australian territories
    "Norfolk Island":            "Oceania",
    "Christmas Island":          "Asia",
    "Cocos (Keeling) Islands":   "Asia",
    # NZ territories
    "Niue":                      "Oceania",
    "Tokelau":                   "Oceania",
    "Cook Islands":              "Oceania",
    # UK territories
    "Falkland Islands":          "South America",
    "Bermuda":                   "North America",
    "Cayman Islands":            "North America",
    "British Virgin Islands":    "North America",
    "Turks and Caicos Islands":  "North America",
    "Saint Helena":              "Africa",
    "Pitcairn":                  "Oceania",
    "Gibraltar":                 "Europe",
    # US territories
    "Puerto Rico":               "North America",
    "Guam":                      "Oceania",
    "U.S. Virgin Islands":       "North America",
    "American Samoa":            "Oceania",
    "Northern Mariana Islands":  "Oceania",
}

KNOWN_OCEANS = {
    "Bass Strait": [(-43, -38, 143, 149)],
    "Timor Sea":      [(-13,  -8,  123,  133)],
    "Arafura Sea":    [(-13,  -8,  133,  141)],
    "Gulf of Carpentaria":[(-17, -10, 136, 142)],
    # Local southeast Queensland water body — keep this ahead of Tasman Sea.
    "Moreton Bay":    [(-28.6, -26.8, 152.5, 154.2)],
    # South West Rocks / Arakoon coastline.
    "Trial Bay":      [(-31.0, -30.8, 152.95, 153.15)],
    "Coral Sea":      [(-25, -10, 147, 165)],
    "Great Australian Bight": [(-50, -32, 115, 145)],
    # Broad offshore fallback only; local coastal bays/headlands should win first.
    # Include Tasmania and the NSW coast under the Tasman Sea rather than Pacific.
    "Tasman Sea":     [(-50, -38, 140, 175)],
    "Gulf of Mexico":     [( 18,  30,  -97,  -80)],
    "Caribbean Sea":  [( 10,  23,  -87,  -60)],
    "Mediterranean Sea":  [( 30,  46,   -6,   36)],
    "North Sea":      [( 51,  61,   -4,    9)],
    "Red Sea":        [(  12, 30,   32,   44)],
    "Arabian Sea":        [(  5,  25,   55,   78)],
    "East China Sea":     [( 23,  33,  118,  130)],
    "Sea of Japan":       [( 33,  52,  127,  142)],
    "Bering Sea":         [( 52,  66,  162, -157)],
    "Hudson Bay":         [( 51,  66,  -95,  -65)],
    "Gulf of Alaska":     [( 54,  62, -155, -135)],
    "Labrador Sea":       [( 53,  65,  -65,  -42)],
    "Norwegian Sea":      [( 62,  75,   -5,   30)],
    "Barents Sea":        [( 68,  81,   15,   60)],
    "Persian Gulf":   [(  22, 30,   48,   57)],
    "South China Sea":[(-5,   23,  105,  121)],
    "Black Sea":          [( 41,  47,   28,   42)],
    "Bay of Bengal":      [(  5,  23,   78,   99)],
    "Caspian Sea":        [( 37,  47,   49,   55)],
    "Baltic Sea":         [( 53,  66,    9,   30)],
    # Southern Ocean starts south of Tasmania under Australian conventions.
    "Southern Ocean (Australia)": [(-60, -43.6, 110, 180)],
    "Pacific Ocean":  [(-60,  60,  120, -80)],
    "Atlantic Ocean": [(-60,  70,  -80,  20)],
    "Indian Ocean":   [(-50,  30,   20, 120)],
    "Southern Ocean": [(-90, -45, -180, 180)],
    "Arctic Ocean":   [( 66,  90, -180, 180)],

}




def _safe_stem(name):
    return (name.lower()
               .replace(" ", "_")
               .replace("'", "")
               .replace("/", "_")
               .replace("&", "and")
               .replace(",", "")
               .replace(".", ""))
# ── GNAF endpoint ─────────────────────────────────────────────────────────────
GNAF_URL = "https://samtaylor9.nfshost.com/gnaf.cgi"

DEFAULT_SETTINGS = {
    "walk_announce_pois":     True,
    "walk_poi_category":      "all",
    "walk_poi_radius_m":      80,
    "walk_announce_category": True,
    "announce_climate_zones": True,
    "check_updates_at_startup": True,
    "skipped_update_version": "",
    "spatial_tones_mode":     "world",  # "world", "country", or "region"
    "challenge_direction_mode": "map",  # "map" or "globe"
    "poi_browse_radius_km":    1,
    "suppress_warn_google":    False,
    "mistral_api_key":         "",
    "nav_provider":           "osm",   # "osm" or "google" or "here"
    "departure_board_source": "gtfs",  # "gtfs" or "google"
    "here_api_key":           "",
    "ors_api_key":            "",
    "weather_temperature_unit": "auto",  # "auto", "celsius", or "fahrenheit"
    "distance_unit":          "metric",  # "metric" or "imperial"
    "poi_source":             "osm",   # "osm" or "here"
    "language":               "",      # empty means system/default language
    "gnaf_enabled":           True,    # Australian address point overlay
    "jump_history":           [],      # last 5 J-key destinations [{label,lat,lon}]
    # On in Education builds, off in Pro builds, unless the user overrides it.
    "clear_favourites_on_exit": EDUCATION_EDITION,
    "logging": {
        "errors":        False,
        "street":        False,
        "snap":          False,
        "api_calls":     False,
        "challenges":    False,
        "feature_usage": False,
        "navigation":    False,
        "verbose":       False,
    },
}

def load_settings():
    s = dict(DEFAULT_SETTINGS)
    if os.path.exists(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH, encoding="utf-8") as f:
                saved = json.load(f)
            saved.pop("serper_api_key", None)
            s.update(saved)
        except Exception:
            pass
    return s

def save_settings(s):
    data = {
        k: v for k, v in dict(s).items()
        if not str(k).startswith("_") and k != "serper_api_key"
    }
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass



def _load_suppressed() -> list:
    if not os.path.exists(SUPPRESSED_POIS_PATH):
        return []
    try:
        with open(SUPPRESSED_POIS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save_suppressed(entries: list) -> None:
    try:
        with open(SUPPRESSED_POIS_PATH, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)
        # Suppressed POIs are a local preference; keep the save quiet unless it fails.
    except Exception as e:
        miab_log("errors", f"Failed to save suppressed POIs: {e}", None)


def _is_suppressed(poi: dict, suppressed: list) -> bool:
    name = (poi.get("name") or poi.get("label") or "").split(",")[0].lower().strip()
    plat = round(float(poi.get("lat", 0)), 4)
    plon = round(float(poi.get("lon", 0)), 4)
    for entry in suppressed:
        if (entry.get("name", "").lower() == name
                and abs(entry.get("lat", 0) - plat) < 0.0002
                and abs(entry.get("lon", 0) - plon) < 0.0002):
            return True
    return False



def _load_renamed() -> list:
    if not os.path.exists(RENAMED_POIS_PATH):
        return []
    try:
        with open(RENAMED_POIS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save_renamed(entries: list) -> None:
    try:
        with open(RENAMED_POIS_PATH, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _apply_renames(pois: list, renamed: list) -> list:
    """Return a copy of pois with any local name overrides applied."""
    if not renamed:
        return pois
    result = []
    for poi in pois:
        old_name = (poi.get("name") or poi.get("label") or "").split(",")[0].lower().strip()
        plat = round(float(poi.get("lat", 0)), 4)
        plon = round(float(poi.get("lon", 0)), 4)
        match = next(
            (r for r in renamed
             if r.get("old_name", "").lower() == old_name
             and abs(r.get("lat", 0) - plat) < 0.0002
             and abs(r.get("lon", 0) - plon) < 0.0002),
            None,
        )
        if match:
            poi = dict(poi)
            new_name = match["new_name"]
            poi["name"] = new_name
            # Rebuild label — replace old name at start of label
            old_label = poi.get("label", "")
            poi["label"] = old_label.replace(
                old_label.split(",")[0], new_name, 1)
        result.append(poi)
    return result


def _load_personal_pois() -> list:
    if not os.path.exists(PERSONAL_POIS_PATH):
        return []
    try:
        with open(PERSONAL_POIS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_personal_pois(entries: list) -> None:
    try:
        with open(PERSONAL_POIS_PATH, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)
    except Exception as e:
        miab_log("errors", f"Failed to save personal POIs: {e}", None)

# ── Dialog classes are in dialogs.py ─────────────────────────────────────
from dialogs import (
    SettingsDialog,
    POICategoryDialog,
    show_open_source_notice,
)
from city_packs import CityPackWizardDialog
from timetable import TimetableClient
from poi_fetch import (
    PoiFetcher,
    POI_CATEGORY_CHOICES,
    POI_BACKGROUND_RADIUS_METRES,
    is_menu_eligible_poi,
    filter_pois_by_category,
)
from favourites import (
    FavouritesDialog,
    add_or_replace_favourite,
    load_favourites,
    make_favourite,
    save_favourites,
)
from street_data import StreetFetcher
from mistral import MistralClient
from serper import SerperClient
from opensky import OpenSkyClient
from aviationstack import AviationStackClient, fmt_dep, fmt_arr
from priceline import PricelineClient
from tripadvisor import TripAdvisorClient
from airlines import decode_callsign
try:
    from game import ChallengeGame, ChallengeSession
except Exception as _game_import_err:
    miab_log("errors", f"[Game] Import failed: {_game_import_err}", None)
    class ChallengeGame:
        """No-op fallback when game.py fails to import."""
        active = False
        target_country = ""
        def __init__(self, **kw): pass
        def start(self, *a, **kw): pass
        def stop(self, *a, **kw): pass
        def on_move(self, *a): pass
        def on_win(self): pass
        def repeat_target(self): pass

    class ChallengeSession:
        active = False
        def __init__(self, **kw): pass
        def start(self, *a, **kw): pass
        def stop(self): pass
        def on_win(self, *a, **kw): pass
        def on_timeout(self, *a, **kw): pass
        def on_space(self, *a, **kw): return False



class SoundEngine:
    # Volume step size and limits for Shift+F3/F4
    _VOL_STEP = 0.1
    _VOL_MIN  = 0.0
    _VOL_MAX  = 1.0

    def __init__(self):
        pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=512)
        pygame.mixer.set_num_channels(16)
        self._ch = pygame.mixer.Channel(0)
        self._master_volume = 0.7
        self._apply_volume()
        self._current = None

    def _apply_volume(self):
        """Set the master volume on every pygame mixer channel."""
        n = pygame.mixer.get_num_channels()
        for i in range(n):
            pygame.mixer.Channel(i).set_volume(self._master_volume)

    def volume_down(self) -> str:
        """Decrease master volume by 10%. Returns announcement string."""
        self._master_volume = max(self._VOL_MIN,
                                  round(self._master_volume - self._VOL_STEP, 2))
        self._apply_volume()
        pct = int(self._master_volume * 100)
        return f"Volume {pct}%." if pct > 0 else "Volume muted."

    def volume_up(self) -> str:
        """Increase master volume by 10%. Returns announcement string."""
        self._master_volume = min(self._VOL_MAX,
                                  round(self._master_volume + self._VOL_STEP, 2))
        self._apply_volume()
        return f"Volume {int(self._master_volume * 100)}%."

    # Maps canonical country name → existing sound stem when no direct file exists.
    # Specific country files take priority; region names are last resort.
    _SOUND_FALLBACKS = {
        # Europe
        "Albania":                  "europe",
        "Algeria":                  "africa",
        "Armenia":                  "europe",
        "Azerbaijan":               "europe",
        "Bahrain":                  "middle_east",
        "Belarus":                  "europe",
        "Bosnia and Herzegovina":   "europe",
        "Central African Republic": "africa",
        "Denmark":                  "europe",
        "Finland":                  "europe",
        "Gabon":                    "africa",
        "Georgia":                  "europe",
        "Guinea":                   "africa",
        "Guyana":                   "south_america",
        "Honduras":                 "north_america",
        "Iraq":                     "middle_east",
        "Ivory Coast":              "africa",
        "Kazakhstan":               "asia",
        "Liberia":                  "africa",
        "Libya":                    "africa",
        "Malawi":                   "africa",
        "Mauritania":               "africa",
        "Moldova":                  "europe",
        "Morocco":                  "africa",
        "Mozambique":               "africa",
        "Namibia":                  "africa",
        "North Macedonia":          "europe",
        "Paraguay":                 "south_america",
        "Ecuador":                  "south_america",
        "Poland":                   "europe",
        "Qatar":                    "middle_east",
        "Romania":                  "europe",
        "Rwanda":                   "africa",
        "Senegal":                  "gambia",
        "Slovakia":                 "europe",
        "Somalia":                  "africa",
        "South Sudan":              "africa",
        "Sudan":                    "africa",
        "Suriname":                 "south_america",
        "Venezuela":                "south_america",
        "Angola":                   "africa",
        "Eritrea":                  "africa",
        "Ethiopia":                 "africa",
        "Cote d'Ivoire":            "africa",
        # Aliases already handled by COUNTRY_ALIASES but add region safety net
        "Democratic People's Republic of Korea": "asia",
        "Republic of Korea":        "republic_of_korea",
        "Democratic Republic of the Congo": "congo_(kinshasa)",
        "Republic of the Congo":    "congo_(brazzaville)",
        "Russian Federation":       "russian_federation",
        "Syrian Arab Republic":     "syrian_arab_republic",
        "United States of America": "united_states_of_america",
    }

    def play_location_sound(self, country_name, continent=""):
        canonical = COUNTRY_ALIASES.get(country_name, country_name)

        if canonical == self._current:
            return

        orig_stem = _safe_stem(country_name)
        can_stem  = _safe_stem(canonical)

        def _candidates_for(country_dir, region_dir):
            paths = []
            # Original country name takes priority (for example,
            # new_caledonia.ogg over its canonical parent, france.ogg).
            for ext in ("ogg", "mp3"):
                paths.append(os.path.join(country_dir, f"{orig_stem}.{ext}"))
            for ext in ("ogg", "mp3"):
                paths.append(os.path.join(region_dir, f"{orig_stem}.{ext}"))

            if can_stem != orig_stem:
                for ext in ("ogg", "mp3"):
                    paths.append(os.path.join(country_dir, f"{can_stem}.{ext}"))
                for ext in ("ogg", "mp3"):
                    paths.append(os.path.join(region_dir, f"{can_stem}.{ext}"))

            fallback = self._SOUND_FALLBACKS.get(canonical)
            if fallback:
                fb_stem = _safe_stem(fallback)
                for ext in ("ogg", "mp3"):
                    for directory in (country_dir, region_dir):
                        paths.append(os.path.join(directory, f"{fb_stem}.{ext}"))

            if continent:
                cont_stem = _safe_stem(continent)
                for ext in ("ogg", "mp3"):
                    paths.append(os.path.join(region_dir, f"{cont_stem}.{ext}"))
            return paths

        # User-owned overrides always win. The bundled tree remains disposable
        # so installers can refresh it without deleting custom sounds.
        candidates = _candidates_for(USER_COUNTRY_DIR, USER_REGION_DIR)
        candidates.extend(_candidates_for(COUNTRY_DIR, REGION_DIR))

        for path in candidates:
            if os.path.exists(path):
                self._current = canonical
                self.play_file(path, loops=-1)
                return

        # No sound found — stop current sound
        self._current = canonical
        self._ch.stop()

    def play_file(self, path, loops=0):
        """Play a WAV file once (or looped if loops=-1)."""
        try:
            sound = pygame.mixer.Sound(path)
            self._ch.play(sound, loops=loops)
        except Exception as e:
            miab_log("errors", f"[SoundEngine] Cannot play {path}: {e}", getattr(self, "settings", None))

    def stop(self):
        """Stop current playback."""
        self._ch.stop()
        self._current = None

    def play_poi_tone(self, side: str):
        """Short directional beep: 'left', 'right', or 'both'."""
        def _gen():
            sr   = 44100
            t    = np.linspace(0, 0.08, int(sr * 0.08), False)
            wave = np.sin(2 * np.pi * 1760.0 * t)
            fade = int(sr * 0.02)
            wave[:fade]  *= np.linspace(0, 1, fade)
            wave[-fade:] *= np.linspace(1, 0, fade)
            wave = wave * 0.6 * 32767
            if side == "left":
                l, r = wave, np.zeros_like(wave)
            elif side == "right":
                l, r = np.zeros_like(wave), wave
            else:  # both
                l, r = wave * 0.7, wave * 0.7
            stereo = np.ascontiguousarray(
                np.stack([l, r], axis=-1), dtype=np.int16)
            snd = pygame.sndarray.make_sound(stereo)
            for idx in range(1, pygame.mixer.get_num_channels()):
                ch = pygame.mixer.Channel(idx)
                if not ch.get_busy():
                    ch.play(snd)
                    return
            pygame.mixer.Channel(1).play(snd)
        threading.Thread(target=_gen, daemon=True).start()

    def play_spatial_tone(self, lat, lon, bounds=None):
        """Pitch-panned navigation beep on channels 1+."""
        if bounds:
            try:
                min_lat, max_lat, min_lon, max_lon = bounds
                if (min_lon > 180.0 or max_lon > 180.0) and lon < 0.0:
                    lon += 360.0
                lat_span = max_lat - min_lat
                lon_span = max_lon - min_lon
                if lat_span > 0 and lon_span > 0:
                    lat = ((lat - min_lat) / lat_span) * 180.0 - 90.0
                    lon = ((lon - min_lon) / lon_span) * 360.0 - 180.0
            except Exception:
                pass
        def _gen():
            freq   = max(220.0, min(880.0, 440.0 + (lat / 90.0) * 440.0))
            pan    = max(-1.0,  min(1.0,   lon / 180.0))
            sr     = 44100
            t      = np.linspace(0, 0.15, int(sr * 0.15), False)
            wave   = np.sin(2 * np.pi * freq * t)
            fade   = int(sr * 0.04)
            wave[:fade]  *= np.linspace(0, 1, fade)
            wave[-fade:] *= np.linspace(1, 0, fade)
            left   = wave * (1.0 - pan) / 2.0
            right  = wave * (1.0 + pan) / 2.0
            stereo = np.ascontiguousarray(
                np.stack([left, right], axis=-1) * 0.5 * 32767,
                dtype=np.int16
            )
            snd = pygame.sndarray.make_sound(stereo)
            for idx in range(1, pygame.mixer.get_num_channels()):
                ch = pygame.mixer.Channel(idx)
                if not ch.get_busy():
                    ch.play(snd)
                    return
            pygame.mixer.Channel(1).play(snd)
        threading.Thread(target=_gen, daemon=True).start()

def load_offline_data():
    if os.path.exists(CACHE_PATH):
        try:
            if os.path.exists(CSV_PATH) and os.path.getmtime(CSV_PATH) > os.path.getmtime(CACHE_PATH):
                raise ValueError("stale cache")
            df = pd.read_pickle(CACHE_PATH)
            if 'city' not in df.columns or 'population' not in df.columns:
                raise ValueError("stale cache")
            return df, None
        except Exception:
            os.remove(CACHE_PATH)
            return load_offline_data()

    if os.path.exists(CSV_PATH):
        df = pd.read_csv(
            CSV_PATH,
            usecols=['city', 'admin_name', 'country', 'lat', 'lng', 'population'],
            compression='gzip',
        ).dropna(subset=['lat', 'lng'])
        df = df.reset_index(drop=True)
        try:
            df.to_pickle(CACHE_PATH)
        except Exception:
            pass
        return df, None

    return None

def _nearest_city(lats, lons, lat, lon):
    """Return (dist_degrees, idx) of nearest city — replaces scipy KDTree."""
    best_dist = float("inf")
    best_idx  = 0
    for i in range(len(lats)):
        dlat = lats[i] - lat
        dlon = lons[i] - lon
        d = dlat * dlat + dlon * dlon
        if d < best_dist:
            best_dist = d
            best_idx  = i
    return best_dist ** 0.5, best_idx


def load_facts():
    if os.path.exists(FACTS_PATH):
        try:
            with open(FACTS_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {}

GEOJSON_PATH = os.path.join(BASE_DIR, "countries.geojson.gz")
GEOJSON_PROCESSED_CACHE_PATH = os.path.join(CACHE_DIR, "countries_geojson_processed.pkl")

COL_BG      = wx.Colour(10,  20,  40)
COL_OCEAN   = wx.Colour(20,  50,  90)
COL_LAND    = wx.Colour(40,  80,  55)
COL_BORDER  = wx.Colour(30,  60,  40)
COL_GRID    = wx.Colour(30,  60,  80)
COL_DOT     = wx.Colour(255, 60,  60)
COL_RING    = wx.Colour(255, 180, 50)

def _load_geojson_polygons():
    """Load and simplify country polygons from countries.geojson.
    Returns:
        rings     — flat list of (lon,lat) coordinate rings for drawing
        countries — list of dicts {name, iso2, centroid_lon, centroid_lat, rings_idx}
                    where rings_idx is list of indices into rings[]
    """
    if not os.path.exists(GEOJSON_PATH):
        return [], [], []
    source_sig = None
    try:
        source_sig = (os.path.getmtime(GEOJSON_PATH), os.path.getsize(GEOJSON_PATH))
        if os.path.exists(GEOJSON_PROCESSED_CACHE_PATH):
            with open(GEOJSON_PROCESSED_CACHE_PATH, "rb") as f:
                cached = pickle.load(f)
            if cached.get("source_sig") == source_sig:
                return (
                    cached.get("rings") or [],
                    cached.get("countries") or [],
                    cached.get("land_polygons") or [],
                )
    except Exception:
        pass
    try:
        from shapely.geometry import shape
        with gzip.open(GEOJSON_PATH, 'rt', encoding="utf-8") as f:
            data = json.load(f)
        rings     = []
        countries = []
        land_polygons = []
        for feature in data["features"]:
            props    = feature.get("properties", {})
            name     = (props.get("NAME") or props.get("name") or
                        props.get("ADMIN") or "").strip()
            iso2     = (props.get("ISO_A2") or props.get("iso_a2") or "").strip()
            if iso2 in ("-99", "-1", "", None):
                iso2 = name[:2].upper() if name else "??"

            geom  = shape(feature["geometry"])
            land_polygons.append(geom)
            polys = list(geom.geoms) if geom.geom_type == "MultiPolygon" else [geom]

            country_ring_indices = []
            all_lons, all_lats   = [], []

            for poly in polys:
                simplified = poly.simplify(0.1, preserve_topology=True)
                sub_polys  = (list(simplified.geoms)
                              if simplified.geom_type == "MultiPolygon"
                              else [simplified])
                for sub in sub_polys:
                    if sub.is_empty:
                        continue
                    coords = list(sub.exterior.coords)
                    if len(coords) < 3:
                        continue
                    lons = [c[0] for c in coords]
                    if max(lons) - min(lons) > 180:
                        continue
                    country_ring_indices.append(len(rings))
                    rings.append(coords)
                    all_lons.extend(lons)
                    all_lats.extend(c[1] for c in coords)

            if country_ring_indices and all_lons:
                centroid_lon = sum(all_lons) / len(all_lons)
                centroid_lat = sum(all_lats) / len(all_lats)
                countries.append({
                    "name":         name,
                    "iso2":         iso2,
                    "centroid_lon": centroid_lon,
                    "centroid_lat": centroid_lat,
                    "rings_idx":    country_ring_indices,
                })

        if source_sig is not None:
            try:
                with open(GEOJSON_PROCESSED_CACHE_PATH, "wb") as f:
                    pickle.dump(
                        {
                            "source_sig": source_sig,
                            "rings": rings,
                            "countries": countries,
                            "land_polygons": land_polygons,
                        },
                        f,
                        protocol=pickle.HIGHEST_PROTOCOL,
                    )
            except Exception:
                pass
        return rings, countries, land_polygons
    except Exception:
        return [], [], []

_GEO_RINGS, _GEO_COUNTRIES, _GEO_LAND_POLYGONS = _load_geojson_polygons()

# Antarctica hardcoded polygon
_ANTARCTICA = [
    (-180, -90), (-180, -60), (-150, -65), (-120, -67), (-90, -65),
    (-60, -70),  (-30, -72),  (0,   -70),  (30,  -68),  (60,  -70),
    (90,  -65),  (120, -67),  (150, -65),  (180, -60),  (180, -90),
    (-180, -90),
]
_GEO_COUNTRIES.append({
    "name": "Antarctica", "iso2": "AQ",
    "centroid_lon": 0.0, "centroid_lat": -80.0,
    "rings_idx": [len(_GEO_RINGS)],
})
_GEO_RINGS.append(_ANTARCTICA)

def _build_land_checker(polygons=None):
    """Build a fast point-in-polygon land checker from the GeoJSON."""
    polygons = polygons or []
    if not polygons and not os.path.exists(GEOJSON_PATH):
        return lambda lat, lon: False
    try:
        from shapely.geometry import Point
        if not polygons:
            from shapely.geometry import shape
            with gzip.open(GEOJSON_PATH, 'rt', encoding='utf-8') as f:
                data = json.load(f)
            for feature in data['features']:
                try:
                    polygons.append(shape(feature['geometry']))
                except Exception:
                    pass
        def is_land(lat, lon):
            pt = Point(lon, lat)
            return any(p.contains(pt) for p in polygons)
        return is_land
    except Exception as e:
        miab_log("errors", f"[Map] Land checker failed: {e}", None)
        return lambda lat, lon: False

_IS_LAND   = _build_land_checker(_GEO_LAND_POLYGONS)


class _ModeStaticAccessible(wx.Accessible):
    """Expose the focused mode through one lightweight MSAA object."""

    def __init__(self, window):
        super().__init__()
        self._window = window

    def GetChildCount(self):
        return wx.ACC_OK, 0

    def GetName(self, child_id):
        if child_id == wx.ACC_SELF:
            return wx.ACC_OK, self._window.GetLabel()
        return wx.ACC_NOT_IMPLEMENTED, None

    def GetValue(self, child_id):
        if child_id == wx.ACC_SELF:
            return wx.ACC_OK, ""
        return wx.ACC_NOT_IMPLEMENTED, None

    def GetRole(self, child_id):
        if child_id == wx.ACC_SELF:
            return wx.ACC_OK, wx.ROLE_SYSTEM_PANE
        return wx.ACC_NOT_IMPLEMENTED, None

    def GetState(self, child_id):
        if child_id != wx.ACC_SELF:
            return wx.ACC_NOT_IMPLEMENTED, 0
        state = wx.ACC_STATE_SYSTEM_FOCUSABLE
        if self._window.HasFocus():
            state |= wx.ACC_STATE_SYSTEM_FOCUSED
        return wx.ACC_OK, state


class _ModeStaticText(wx.StaticText):
    """Native static text used as the idle keyboard target."""

    def AcceptsFocus(self):
        return True

    def AcceptsFocusFromKeyboard(self):
        return True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._mode_accessible = _ModeStaticAccessible(self)
        self.SetAccessible(self._mode_accessible)


class WorldMapPanel(wx.Panel):
    """Accurate world map from GeoJSON.

    World mode keeps the current ISO-2 label layer. Country mode reuses the
    same base map but swaps in a calmer, location-focused overlay.
    F8 still flashes the current country; Shift+F8 cycles the overlay mode.
    """

    _COL_LABEL      = wx.Colour(255, 220,  50)
    _COL_LABEL_OUT  = wx.Colour(0,   0,    0)
    _COL_FLASH_FILL = wx.Colour(255, 200,  0, 180)
    _LABEL_SIZE     = 11
    _FLASH_SIZE     = 28

    def AcceptsFocusFromKeyboard(self):
        return False

    def __init__(self, parent, owner=None):
        super().__init__(parent, style=wx.NO_BORDER)
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.SetDoubleBuffered(True)
        self.SetBackgroundColour(COL_BG)
        self._owner        = owner
        self.lat          = 0.0
        self.lon          = 0.0
        self.street_mode  = False
        self.street_label = ""
        self._flash_name  = ""
        self._flash_rings = []
        self._flash_cx    = 0.0
        self._flash_cy    = 0.0
        self._bg_bitmap   = None
        self._bg_bitmap_mode = None
        self._label_cache_size = (-1, -1)
        self._classroom_trail = []
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_SIZE,  self._on_size)

    def _on_size(self, event):
        self._label_cache_size = (-1, -1)
        self._bg_bitmap = None   # invalidate background cache
        self._bg_bitmap_mode = None
        self.Refresh()
        event.Skip()

    def set_position(self, lat, lon, street_mode=False, street_label=""):
        if self._classroom_mode_active():
            point = (float(lat), float(lon), bool(street_mode))
            if not self._classroom_trail or point[:2] != self._classroom_trail[-1][:2]:
                self._classroom_trail.append(point)
                self._classroom_trail = self._classroom_trail[-120:]
        self.lat          = lat
        self.lon          = lon
        self.street_mode  = street_mode
        self.street_label = street_label
        self.Refresh()

    def set_classroom_mode(self, enabled):
        """Start or stop a visual map session without adding focusable UI."""
        if enabled:
            self._classroom_trail = [(float(self.lat), float(self.lon), bool(self.street_mode))]
        self.Refresh()

    def _classroom_mode_active(self):
        return bool(self._owner and getattr(self._owner, "_map_fullscreen", False))

    @staticmethod
    def _coordinate_text(value, positive, negative):
        return f"{abs(float(value)):.3f}\N{DEGREE SIGN} {positive if value >= 0 else negative}"

    def _draw_classroom_trail(self, gc, w, h, geo_kwargs=None):
        if not self._classroom_mode_active() or len(self._classroom_trail) < 2:
            return
        geo_kwargs = geo_kwargs or {}
        expected_street = bool(geo_kwargs)
        points = []
        last_lon = None
        gc.SetPen(gc.CreatePen(wx.GraphicsPenInfo(wx.Colour(0, 235, 255, 230)).Width(4)))
        for lat, lon, street in self._classroom_trail:
            if street != expected_street:
                points = []
                last_lon = None
                continue
            if not expected_street and last_lon is not None and abs(lon - last_lon) > 180:
                points = []
            px, py = self._geo_to_px(lon, lat, w, h, **geo_kwargs)
            if 0 <= px <= w and 0 <= py <= h:
                if points:
                    gc.StrokeLine(points[-1][0], points[-1][1], px, py)
                points.append((px, py))
            last_lon = lon

    def _draw_classroom_destination(self, gc, w, h, geo_kwargs=None):
        owner = self._owner
        destination = getattr(owner, "_map_destination", None) if owner else None
        if not self._classroom_mode_active() or not destination:
            return
        try:
            lat, lon = destination["coords"]
            px, py = self._geo_to_px(lon, lat, w, h, **(geo_kwargs or {}))
        except (KeyError, TypeError, ValueError):
            return
        if not (0 <= px <= w and 0 <= py <= h):
            return
        size = max(8, min(15, int(min(w, h) / 50)))
        path = gc.CreatePath()
        path.MoveToPoint(px, py - size)
        path.AddLineToPoint(px + size, py)
        path.AddLineToPoint(px, py + size)
        path.AddLineToPoint(px - size, py)
        path.CloseSubpath()
        gc.SetBrush(gc.CreateBrush(wx.Brush(wx.Colour(255, 210, 0))))
        gc.SetPen(gc.CreatePen(wx.GraphicsPenInfo(wx.Colour(20, 20, 20)).Width(3)))
        gc.DrawPath(path)

    def _draw_classroom_hud(self, gc, w, h):
        if not self._classroom_mode_active() or w < 240 or h < 160:
            return
        owner = self._owner
        title = (getattr(owner, "last_location_str", "") or self.street_label or
                 getattr(owner, "last_country_found", "") or "Current position")
        title = str(title).strip()
        if len(title) > 72:
            title = title[:69].rstrip() + "..."
        context = []
        for value in (getattr(owner, "last_state_found", ""),
                      getattr(owner, "last_country_found", ""),
                      getattr(owner, "current_continent", "")):
            value = str(value or "").strip()
            if value and value not in context and value.lower() != title.lower():
                context.append(value)
        if getattr(owner, "_walking_mode", False):
            mode = "Walking map"
        elif self.street_mode:
            mode = "Street map"
        else:
            mode = "World map"
        coords = (self._coordinate_text(self.lat, "N", "S") + "   " +
                  self._coordinate_text(self.lon, "E", "W"))
        detail = "  \N{BULLET}  ".join(context)
        font_size = max(13, min(24, int(h / 32)))
        small_size = max(10, min(17, int(font_size * .7)))
        pad = max(10, int(font_size * .65))
        band_h = int(font_size * 3.5)
        gc.SetBrush(gc.CreateBrush(wx.Brush(wx.Colour(5, 16, 30, 225))))
        gc.SetPen(gc.CreatePen(wx.GraphicsPenInfo(wx.Colour(0, 235, 255)).Width(2)))
        gc.DrawRoundedRectangle(pad, pad, max(1, w - pad * 2), band_h, 8)
        gc.SetFont(gc.CreateFont(
            wx.Font(font_size, wx.FONTFAMILY_SWISS,
                    wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD),
            wx.Colour(255, 255, 255)))
        gc.DrawText(title, pad * 2, pad * 1.45)
        gc.SetFont(gc.CreateFont(
            wx.Font(small_size, wx.FONTFAMILY_SWISS,
                    wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL),
            wx.Colour(190, 245, 255)))
        second_line = detail or mode
        if detail:
            second_line += "  \N{BULLET}  " + mode
        gc.DrawText(second_line, pad * 2, pad * 1.55 + font_size * 1.25)
        gc.DrawText(coords, pad * 2, pad * 1.55 + font_size * 2.05)
        # Shape plus label means north is not communicated by colour alone.
        nx, ny = w - pad * 3, pad + band_h + pad * 2
        gc.SetPen(gc.CreatePen(wx.GraphicsPenInfo(wx.Colour(255, 255, 255)).Width(3)))
        gc.StrokeLine(nx, ny + 24, nx, ny)
        gc.StrokeLine(nx, ny, nx - 7, ny + 10)
        gc.StrokeLine(nx, ny, nx + 7, ny + 10)
        gc.SetFont(gc.CreateFont(
            wx.Font(small_size, wx.FONTFAMILY_SWISS,
                    wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD),
            wx.Colour(255, 255, 255)))
        gc.DrawText("N", nx - small_size / 2, ny - small_size * 1.4)

    def set_flash(self, name, rings_idx, centroid_lon, centroid_lat):
        self._flash_name  = name
        self._flash_rings = rings_idx
        self._flash_cx    = centroid_lon
        self._flash_cy    = centroid_lat
        self.Refresh()
        wx.CallLater(2500, self._clear_flash)

    def _clear_flash(self):
        self._flash_name  = ""
        self._flash_rings = []
        self.Refresh()

    def _geo_to_px(self, lon, lat, w, h, margin=6,
                   lon_min=-180, lon_max=180, lat_min=-90, lat_max=90):
        x = margin + (lon - lon_min) / (lon_max - lon_min) * (w - 2 * margin)
        y = margin + (lat_max - lat) / (lat_max - lat_min) * (h - 2 * margin)
        return int(x), int(y)

    def px_to_geo(self, x, y):
        w, h = self.GetSize()
        margin = 6
        if w <= margin * 2 or h <= margin * 2:
            return self.lat, self.lon
        if self.street_mode:
            span = 0.02
            lon_min = self.lon - span;  lon_max = self.lon + span
            lat_min = self.lat - span;  lat_max = self.lat + span
        else:
            lon_min = -180; lon_max = 180
            lat_min = -90;  lat_max = 90
        lon = lon_min + ((x - margin) / (w - 2 * margin)) * (lon_max - lon_min)
        lat = lat_max - ((y - margin) / (h - 2 * margin)) * (lat_max - lat_min)
        lat = max(-90.0, min(90.0, lat))
        lon = ((lon + 180.0) % 360.0) - 180.0
        return lat, lon

    def _on_paint(self, event):
        dc = wx.AutoBufferedPaintDC(self)
        w, h = self.GetSize()
        if self.street_mode:
            gc = wx.GraphicsContext.Create(dc)
            if gc:
                self._paint_street(gc, w, h)
            return

        mode = self._map_display_mode()

        # Build background bitmap once for the current map mode.
        if (not getattr(self, '_bg_bitmap', None) or
                getattr(self, '_bg_bitmap_size', None) != (w, h) or
                getattr(self, '_bg_bitmap_mode', None) != mode):
            bmp = wx.Bitmap(w, h)
            mdc = wx.MemoryDC(bmp)
            gc2 = wx.GraphicsContext.Create(mdc)
            if gc2:
                self._paint_world_bg(gc2, w, h, include_labels=(mode == "world"))
            mdc.SelectObject(wx.NullBitmap)
            self._bg_bitmap      = bmp
            self._bg_bitmap_size = (w, h)
            self._bg_bitmap_mode = mode

        # Blit cached background
        dc.DrawBitmap(self._bg_bitmap, 0, 0)

        gc = wx.GraphicsContext.Create(dc)
        if gc:
            self._draw_mode_overlay(gc, w, h)
            self._draw_classroom_trail(gc, w, h)
            self._draw_classroom_destination(gc, w, h)
            px, py = self._geo_to_px(self.lon, self.lat, w, h)
            marker_size = 14 if self._classroom_mode_active() else 8
            gc.SetBrush(gc.CreateBrush(wx.Brush(COL_RING)))
            gc.SetPen(wx.NullPen)
            gc.DrawEllipse(px - marker_size, py - marker_size,
                           marker_size * 2, marker_size * 2)
            gc.SetBrush(gc.CreateBrush(wx.Brush(COL_DOT)))
            dot_size = max(5, marker_size - 5)
            gc.DrawEllipse(px - dot_size, py - dot_size, dot_size * 2, dot_size * 2)
            self._draw_classroom_hud(gc, w, h)

    def _draw_label(self, gc, text, cx, cy, size):
        font = wx.Font(size, wx.FONTFAMILY_SWISS,
                       wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)
        gc.SetFont(gc.CreateFont(font, self._COL_LABEL_OUT))
        for dx, dy in ((-1,-1),(1,-1),(-1,1),(1,1),(0,-1),(0,1),(-1,0),(1,0)):
            gc.DrawText(text, cx + dx, cy + dy)
        gc.SetFont(gc.CreateFont(font, self._COL_LABEL))
        gc.DrawText(text, cx, cy)

    def _draw_label_at_geo(self, gc, text, lon, lat, w, h, size, dx=0, dy=0):
        if not text:
            return
        px, py = self._geo_to_px(lon, lat, w, h)
        px += dx
        py += dy
        est_w = max(12, len(text) * size * 0.6)
        est_h = size + 4
        fx = max(4, min(int(px - est_w / 2), max(4, w - int(est_w) - 4)))
        fy = max(4, min(int(py - est_h / 2), max(4, h - int(est_h) - 4)))
        self._draw_label(gc, text, fx, fy, size)

    def _owner_ref(self):
        return getattr(self, "_owner", None)

    def _map_display_mode(self):
        owner = self._owner_ref()
        return getattr(owner, "map_display_mode", "world") if owner else "world"

    def _canonical_country_name(self, name):
        text = (name or "").strip()
        if not text:
            return ""
        return COUNTRY_ALIASES.get(text, text).strip().lower()

    def _country_entry_for(self, name):
        raw = (name or "").strip().lower()
        if not raw:
            return None
        for entry in _GEO_COUNTRIES:
            if str(entry.get("name", "")).strip().lower() == raw:
                return entry
        target = self._canonical_country_name(name)
        if not target:
            return None
        for entry in _GEO_COUNTRIES:
            if self._canonical_country_name(entry.get("name", "")) == target:
                return entry
        return None

    def _country_city_index_key(self, name):
        owner = self._owner_ref()
        if not owner:
            return ""
        target = self._canonical_country_name(name)
        if not target:
            return ""
        for key in getattr(owner, "_city_country_index", {}):
            if self._canonical_country_name(key) == target:
                return key
        return ""

    def _draw_ring_polygons(self, gc, ring_indices, w, h, fill, pen_colour):
        """Draw filled ring polygons (by index into _GEO_RINGS) with the given pen/fill."""
        gc.SetPen(gc.CreatePen(wx.GraphicsPenInfo(pen_colour).Width(1)))
        for ring_idx in ring_indices:
            if ring_idx < 0 or ring_idx >= len(_GEO_RINGS):
                continue
            ring = _GEO_RINGS[ring_idx]
            if not ring:
                continue
            gc.SetBrush(gc.CreateBrush(wx.Brush(fill)))
            pts = [self._geo_to_px(lon, lat, w, h) for lon, lat in ring]
            if len(pts) < 3:
                continue
            path = gc.CreatePath()
            path.MoveToPoint(*pts[0])
            for pt in pts[1:]:
                path.AddLineToPoint(*pt)
            path.CloseSubpath()
            gc.DrawPath(path)

    def _draw_country_overlay(self, gc, entry, w, h, fill, outline=None):
        if not entry:
            return
        ring_indices = entry.get("rings_idx", []) or []
        if not ring_indices:
            return
        self._draw_ring_polygons(gc, ring_indices, w, h, fill,
                                  outline or wx.Colour(255, 220, 120, 220))

    def _draw_world_labels(self, gc, w, h):
        if not hasattr(self, '_label_cache') or self._label_cache_size != (w, h):
            char_w = self._LABEL_SIZE * 0.7
            char_h = self._LABEL_SIZE + 3
            self._label_cache = [
                (country["iso2"],
                 int(self._geo_to_px(country["centroid_lon"], country["centroid_lat"],
                                     w, h)[0] - len(country["iso2"]) * char_w / 2),
                 int(self._geo_to_px(country["centroid_lon"], country["centroid_lat"],
                                     w, h)[1] - char_h / 2))
                for country in _GEO_COUNTRIES
            ]
            self._label_cache_size = (w, h)
        for iso2, lx, ly in self._label_cache:
            self._draw_label(gc, iso2, lx, ly, self._LABEL_SIZE)

    def _draw_mode_overlay(self, gc, w, h):
        mode = self._map_display_mode()
        if mode == "country":
            self._draw_country_mode(gc, w, h)
        self._draw_flash_overlay(gc, w, h)

    def _draw_flash_overlay(self, gc, w, h):
        if self._flash_rings:
            self._draw_ring_polygons(gc, self._flash_rings, w, h,
                                      self._COL_FLASH_FILL,
                                      wx.Colour(255, 220, 120, 240))
        if self._flash_name:
            self._draw_label_at_geo(
                gc, self._flash_name, self._flash_cx, self._flash_cy, w, h,
                self._FLASH_SIZE)

    def _draw_country_mode(self, gc, w, h):
        owner = self._owner_ref()
        if not owner:
            self._draw_world_labels(gc, w, h)
            return
        country_name = str(getattr(owner, "last_country_found", "") or "").strip()
        if not country_name or country_name == "Open Water":
            self._draw_world_labels(gc, w, h)
            return

        entry = self._country_entry_for(country_name)
        if entry:
            self._draw_country_overlay(
                gc, entry, w, h,
                wx.Colour(255, 200, 0, 72),
                wx.Colour(255, 220, 120, 220))
            self._draw_label_at_geo(
                gc, entry["name"], entry["centroid_lon"], entry["centroid_lat"],
                w, h, 22)
        else:
            self._draw_label_at_geo(gc, country_name, owner.lon, owner.lat, w, h, 22)

        location_text = str(getattr(owner, "last_location_str", "") or "").strip()
        if location_text and location_text.lower() not in {
                country_name.lower(), (entry["name"].lower() if entry else "")}:
            self._draw_label_at_geo(gc, location_text, owner.lon, owner.lat, w, h, 18, dy=18)

        state_name = str(getattr(owner, "last_state_found", "") or "").strip()
        country_key = self._country_city_index_key(country_name)
        country_indices = list(getattr(owner, "_city_country_index", {}).get(country_key, [])) if country_key else []

        if state_name and country_indices:
            state_indices = [idx for idx in country_indices
                             if str(owner._city_admins[idx]).strip() == state_name]
            if state_indices:
                state_anchor = max(state_indices, key=lambda idx: owner._city_pops[idx])
                self._draw_label_at_geo(
                    gc, state_name, owner._city_lons[state_anchor],
                    owner._city_lats[state_anchor], w, h, 18, dy=-18)

        if country_indices:
            # One label per admin region keeps the country view readable.
            groups = {}
            for idx in country_indices:
                city = str(owner._city_names[idx]).strip()
                admin = str(owner._city_admins[idx]).strip()
                group_key = admin or city or f"row-{idx}"
                pop = owner._city_pops[idx]
                current = groups.get(group_key)
                if current is None or pop > current[0]:
                    groups[group_key] = (pop, idx)
            seen = set()
            draw_items = sorted(
                groups.values(),
                key=lambda item: (-item[0], str(owner._city_names[item[1]]).lower())
            )
            for pop, idx in draw_items[:8]:
                city = str(owner._city_names[idx]).strip()
                admin = str(owner._city_admins[idx]).strip()
                if not city:
                    continue
                text = city if not admin or admin == city else f"{city}, {admin}"
                key = text.lower()
                if key in seen:
                    continue
                seen.add(key)
                size = 13 if pop < 1_000_000 else 14
                if state_name and admin == state_name:
                    size = max(size, 15)
                self._draw_label_at_geo(gc, text, owner._city_lons[idx],
                                        owner._city_lats[idx], w, h, size)

    def _paint_world_bg(self, gc, w, h, include_labels=True):
        # Ocean
        gc.SetBrush(gc.CreateBrush(wx.Brush(COL_OCEAN)))
        gc.SetPen(wx.NullPen)
        gc.DrawRectangle(0, 0, w, h)
        # Grid
        gc.SetPen(gc.CreatePen(wx.GraphicsPenInfo(COL_GRID).Width(1)))
        for glon in range(-180, 181, 30):
            x1, y1 = self._geo_to_px(glon,  90, w, h)
            x2, y2 = self._geo_to_px(glon, -90, w, h)
            gc.StrokeLine(x1, y1, x2, y2)
        for glat in range(-90, 91, 30):
            x1, y1 = self._geo_to_px(-180, glat, w, h)
            x2, y2 = self._geo_to_px( 180, glat, w, h)
            gc.StrokeLine(x1, y1, x2, y2)
        # Land polygons
        gc.SetPen(gc.CreatePen(wx.GraphicsPenInfo(COL_BORDER).Width(1)))
        for ring in _GEO_RINGS:
            gc.SetBrush(gc.CreateBrush(wx.Brush(COL_LAND)))
            pts = [self._geo_to_px(lon, lat, w, h) for lon, lat in ring]
            path = gc.CreatePath()
            path.MoveToPoint(*pts[0])
            for pt in pts[1:]:
                path.AddLineToPoint(*pt)
            path.CloseSubpath()
            gc.DrawPath(path)
        if include_labels:
            self._draw_world_labels(gc, w, h)

    def _paint_street(self, gc, w, h):
        span = 0.02
        lon_min = self.lon - span;  lon_max = self.lon + span
        lat_min = self.lat - span;  lat_max = self.lat + span
        kw = dict(lon_min=lon_min, lon_max=lon_max,
                  lat_min=lat_min, lat_max=lat_max)
        gc.SetBrush(gc.CreateBrush(wx.Brush(COL_OCEAN)))
        gc.SetPen(wx.NullPen)
        gc.DrawRectangle(0, 0, w, h)
        gc.SetBrush(gc.CreateBrush(wx.Brush(COL_LAND)))
        gc.SetPen(gc.CreatePen(wx.GraphicsPenInfo(COL_BORDER).Width(1)))
        for ring in _GEO_RINGS:
            pts = [self._geo_to_px(lo, la, w, h, **kw) for lo, la in ring]
            if len(pts) < 3:
                continue
            path = gc.CreatePath()
            path.MoveToPoint(*pts[0])
            for pt in pts[1:]:
                path.AddLineToPoint(*pt)
            path.CloseSubpath()
            gc.DrawPath(path)
        gc.SetPen(gc.CreatePen(wx.GraphicsPenInfo(COL_GRID).Width(1)))
        step = 0.005
        glon = math.floor(lon_min / step) * step
        while glon <= lon_max:
            x1, y1 = self._geo_to_px(glon, lat_max, w, h, **kw)
            x2, y2 = self._geo_to_px(glon, lat_min, w, h, **kw)
            gc.StrokeLine(x1, y1, x2, y2)
            glon += step
        glat = math.floor(lat_min / step) * step
        while glat <= lat_max:
            x1, y1 = self._geo_to_px(lon_min, glat, w, h, **kw)
            x2, y2 = self._geo_to_px(lon_max, glat, w, h, **kw)
            gc.StrokeLine(x1, y1, x2, y2)
            glat += step
        self._draw_classroom_trail(gc, w, h, kw)
        self._draw_classroom_destination(gc, w, h, kw)
        px, py = self._geo_to_px(self.lon, self.lat, w, h, **kw)
        marker_size = 17 if self._classroom_mode_active() else 12
        gc.SetBrush(gc.CreateBrush(wx.Brush(COL_RING)))
        gc.SetPen(wx.NullPen)
        gc.DrawEllipse(px - marker_size, py - marker_size,
                       marker_size * 2, marker_size * 2)
        gc.SetBrush(gc.CreateBrush(wx.Brush(COL_DOT)))
        dot_size = max(7, marker_size - 5)
        gc.DrawEllipse(px - dot_size, py - dot_size, dot_size * 2, dot_size * 2)
        if self.street_label:
            gc.SetFont(gc.CreateFont(
                wx.Font(10, wx.FONTFAMILY_DEFAULT,
                        wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD),
                wx.Colour(220, 220, 220)))
            gc.DrawText("STREET  " + self.street_label, 8, 8)
        self._draw_classroom_hud(gc, w, h)


# ---------------------------------------------------------------------------
# Non-modal street search — live-updating as Stage 2 loads
# ---------------------------------------------------------------------------

_STREET_GENERIC = frozenset({
    "road", "highway", "street", "residential street", "shared street",
    "service road", "motorway", "footpath", "cycle path", "path", "steps",
    "pedestrian area", "dirt track", "bridleway", "road under construction",
})


class _StreetSearchFrame(wx.Frame):

    def __init__(self, navigator):
        self._nav = navigator
        super().__init__(
            navigator,
            title="Street Search",
            size=(420, 200),
            style=(wx.DEFAULT_FRAME_STYLE | wx.FRAME_FLOAT_ON_PARENT)
                  & ~wx.MAXIMIZE_BOX & ~wx.RESIZE_BORDER,
        )
        self.SetBackgroundColour(wx.Colour(10, 20, 40))
        self.SetForegroundColour(wx.Colour(220, 220, 220))

        panel = wx.Panel(self)
        panel.SetBackgroundColour(wx.Colour(10, 20, 40))
        panel.SetForegroundColour(wx.Colour(220, 220, 220))
        vsz = wx.BoxSizer(wx.VERTICAL)

        lbl_street = wx.StaticText(panel, label="Street:")
        lbl_street.SetForegroundColour(wx.Colour(220, 220, 220))
        vsz.Add(lbl_street, 0, wx.LEFT | wx.TOP, 10)

        self._search = wx.TextCtrl(panel, style=wx.TE_PROCESS_ENTER)
        self._search.SetBackgroundColour(wx.Colour(20, 40, 70))
        self._search.SetForegroundColour(wx.Colour(220, 220, 220))
        vsz.Add(self._search, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)

        self._list = wx.ListBox(panel, style=wx.LB_SINGLE)
        self._list.SetBackgroundColour(wx.Colour(20, 40, 70))
        self._list.SetForegroundColour(wx.Colour(220, 220, 220))
        vsz.Add(self._list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)

        lbl_num = wx.StaticText(panel, label="House number (optional):")
        lbl_num.SetForegroundColour(wx.Colour(220, 220, 220))
        vsz.Add(lbl_num, 0, wx.LEFT | wx.TOP, 10)

        self._num = wx.TextCtrl(panel, style=wx.TE_PROCESS_ENTER)
        self._num.SetBackgroundColour(wx.Colour(20, 40, 70))
        self._num.SetForegroundColour(wx.Colour(220, 220, 220))
        vsz.Add(self._num, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)

        hsz = wx.BoxSizer(wx.HORIZONTAL)
        self._ok_btn     = wx.Button(panel, wx.ID_OK,     "OK")
        self._cancel_btn = wx.Button(panel, wx.ID_CANCEL, "Cancel")
        hsz.Add(self._ok_btn,     0, wx.RIGHT, 8)
        hsz.Add(self._cancel_btn, 0)
        vsz.Add(hsz, 0, wx.ALL, 10)

        panel.SetSizer(vsz)
        panel.Layout()
        self.Fit()

        self._all_names: list[str] = []
        self._filtered_names: list[str] = []
        self._last_filter_query = None
        self._selected_street_name = ""

        self._timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_timer, self._timer)
        self._timer.Start(800)

        self._search.Bind(wx.EVT_TEXT,       self._on_search_text)
        self._search.Bind(wx.EVT_TEXT_ENTER, self._on_jump)
        self._search.Bind(wx.EVT_KEY_DOWN,   self._on_search_key)
        self._list.Bind(wx.EVT_LISTBOX,       self._on_list_select)
        self._list.Bind(wx.EVT_LISTBOX_DCLICK, self._on_jump)
        self._list.Bind(wx.EVT_KEY_DOWN,     self._on_list_key)
        self._num.Bind(wx.EVT_TEXT_ENTER,    self._on_jump)
        self._ok_btn.Bind(wx.EVT_BUTTON,     self._on_jump)
        self._cancel_btn.Bind(wx.EVT_BUTTON, self._on_close)
        self.Bind(wx.EVT_CHAR_HOOK,          self._on_char_hook)
        self.Bind(wx.EVT_CLOSE,              self._on_close)

        self._refresh_combo(force=True)
        self.Layout()
        wx.CallAfter(self._search.SetFocus)
        self.CentreOnParent()

    def _street_names_from_segments(self) -> list[str]:
        segs = getattr(self._nav, '_road_segments', [])
        seen: set = set()
        names: list[str] = []
        for seg in segs:
            raw  = seg.get('name', '')
            name = re.sub(r'\s*\(.*?\)', '', raw).strip()
            if not name:
                continue
            low = name.lower()
            if low in seen:
                continue
            has_real_name = bool(seg.get("raw_name", "").strip())
            if not has_real_name and low in _STREET_GENERIC:
                continue
            seen.add(low)
            names.append(name)
        names.sort()
        return names

    def _refresh_combo(self, force: bool = False) -> None:
        new_names = self._street_names_from_segments()
        if not force and new_names == self._all_names:
            return
        self._all_names = new_names
        self._refresh_filtered_names()
        loading = getattr(self._nav, '_loading', False)
        n = len(new_names)
        if loading:
            self.SetTitle(f"Street Search — {n} streets, loading…")
        else:
            self.SetTitle(f"Street Search — {n} streets")
            self._timer.Stop()

    def _matching_street_names(self, query: str) -> list[str]:
        needle = query.strip().lower()
        if not needle:
            return list(self._all_names)
        matches = [
            name for name in self._all_names
            if needle in name.lower()
        ]
        def match_rank(name: str) -> tuple[int, str]:
            haystack = name.lower()
            words = re.findall(r"[a-z0-9]+", haystack)
            if haystack == needle:
                rank = 0
            elif haystack.startswith(needle):
                rank = 1
            elif any(word.startswith(needle) for word in words):
                rank = 2
            else:
                rank = 3
            return rank, haystack
        matches.sort(key=match_rank)
        return matches

    def _refresh_filtered_names(self) -> None:
        query = self._search.GetValue().strip().lower()
        old_selection = ""
        idx = self._list.GetSelection()
        if 0 <= idx < len(self._filtered_names):
            old_selection = self._filtered_names[idx]
        self._filtered_names = self._matching_street_names(query)
        self._list.Set(self._filtered_names)
        if self._filtered_names:
            if old_selection in self._filtered_names:
                self._list.SetSelection(self._filtered_names.index(old_selection))
            else:
                self._list.SetSelection(0)
        self._last_filter_query = query

    def _on_search_text(self, event) -> None:
        self._selected_street_name = ""
        self._refresh_filtered_names()
        event.Skip()

    def _on_search_key(self, event) -> None:
        code = event.GetKeyCode()
        if code in (wx.WXK_DOWN, wx.WXK_UP) and self._filtered_names:
            self._list.SetFocus()
            idx = 0 if code == wx.WXK_DOWN else len(self._filtered_names) - 1
            self._list.SetSelection(idx)
            self._selected_street_name = self._filtered_names[idx]
            return
        event.Skip()

    def _sync_selected_from_list(self) -> None:
        idx = self._list.GetSelection()
        if 0 <= idx < len(self._filtered_names):
            self._selected_street_name = self._filtered_names[idx]

    def _on_list_select(self, event) -> None:
        self._sync_selected_from_list()
        event.Skip()

    def _on_list_key(self, event) -> None:
        code = event.GetKeyCode()
        if code in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            self._sync_selected_from_list()
            self._on_jump(event)
            return
        if code in (wx.WXK_BACK, wx.WXK_DELETE):
            self._search.SetFocus()
            return
        event.Skip()
        wx.CallAfter(self._sync_selected_from_list)

    def _on_timer(self, event) -> None:
        self._refresh_combo()

    def _on_jump(self, event) -> None:
        query = self._search.GetValue().strip()
        house_number = self._num.GetValue().strip()
        matches = self._matching_street_names(query)
        if query and not matches:
            self._nav._status_update(f"No street matching {query}.", force=True)
            self._search.SetFocus()
            return
        sel = ""
        if self._selected_street_name in matches:
            sel = self._selected_street_name.strip()
        if query and not sel:
            sel = matches[0].strip()
        elif not sel and matches:
            sel = matches[0].strip()
        if not sel:
            sel = query
        if house_number and not query and self.FindFocus() != self._list:
            self._nav._status_update("Type or select a street before entering a house number.", force=True)
            self._search.SetFocus()
            return
        if not sel:
            return
        nav = self._nav
        preview = matches[:5]
        miab_log(
            "snap",
            f"street search jump: query={query!r} selected={sel!r} house_number={house_number!r} matches={preview!r}",
            getattr(nav, "settings", None),
        )
        self._timer.Stop()
        nav._street_search_dlg = None
        nav._suppress_status_until = time.time() + 4.0
        nav._jump_to_street(sel, house_number=house_number)
        self.Hide()
        self.Destroy()
        nav._repeat_current_location_after_return(350)

    def _on_close(self, event) -> None:
        self._timer.Stop()
        self._nav._street_search_dlg = None
        self.Hide()
        self.Destroy()
        self._nav._focus_map_window_silently()

    def _on_char_hook(self, event) -> None:
        code    = event.GetKeyCode()
        focused = self.FindFocus()
        if code == wx.WXK_ESCAPE:
            self._nav._repeat_current_location_after_return()
            self._on_close(None)
            event.StopPropagation()
            return
        if code in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            if focused == self._cancel_btn:
                self._on_close(None)
            else:
                self._on_jump(None)
            event.StopPropagation()
            return
        event.Skip()
        event.StopPropagation()


class MapNavigator(NavMixin, WalkMixin, ToolsMixin, FreeMixin, LookupsMixin, wx.Frame):
    @property
    def lat(self):
        return getattr(self, "_lat", 0.0)

    @lat.setter
    def lat(self, value):
        self._set_coord_value("_lat", value, -90.0, 90.0, "lat")

    @property
    def lon(self):
        return getattr(self, "_lon", 0.0)

    @lon.setter
    def lon(self, value):
        self._set_coord_value("_lon", value, -180.0, 180.0, "lon")

    def _set_coord_value(self, attr, value, min_value, max_value, label):
        if time.time() < getattr(self, "_coord_reject_pair_until", 0):
            paired = getattr(self, "_coord_reject_pair_label", "")
            if paired and paired != label:
                self._log_bad_coord(label, value, f"paired {paired} assignment was rejected; keeping {getattr(self, attr, 0.0):.6f}")
                self._coord_reject_pair_until = 0
                self._coord_reject_pair_label = ""
                return
        try:
            val = float(value)
        except (TypeError, ValueError):
            self._coord_reject_pair_until = time.time() + 0.05
            self._coord_reject_pair_label = label
            self._log_bad_coord(label, value, f"not a number; keeping {getattr(self, attr, 0.0):.6f}")
            return
        if not math.isfinite(val) or val < min_value or val > max_value:
            self._coord_reject_pair_until = time.time() + 0.05
            self._coord_reject_pair_label = label
            self._log_bad_coord(label, value, f"out of range; keeping {getattr(self, attr, 0.0):.6f}")
            return
        self._coord_reject_pair_until = 0
        self._coord_reject_pair_label = ""
        setattr(self, attr, val)

    def _log_bad_coord(self, label, value, reason):
        try:
            import inspect
            frame = inspect.stack()[2]
            where = f"{os.path.basename(frame.filename)}:{frame.lineno} {frame.function}"
        except Exception:
            where = "unknown caller"
        msg = f"Rejected invalid {label} assignment from {where}: {value!r} ({reason})"
        miab_log("street", f"[CoordGuard] {msg}", getattr(self, "settings", None))
        try:
            miab_log("navigation", msg, getattr(self, "settings", {}))
        except Exception:
            pass

    def __init__(self, atlas_data, facts_data):
        self._street_radius     = 1500  # Increased from 800 for better coverage
        self._street_barrier    = 1300  # Increased from 700 (barrier at ~87%)
        self._poi_explore_stack = []
        super().__init__(None, title="Map in a Box",
                         size=(1100, 600),
                         style=wx.DEFAULT_FRAME_STYLE)

        self.df   = atlas_data[0]
        self._city_lats = self.df["lat"].tolist()
        self._city_lons = self.df["lng"].tolist()
        self._city_pops = (
            pd.to_numeric(self.df.get("population", 0), errors="coerce")
            .fillna(0)
            .astype(float)
            .tolist()
        )
        self._city_names = []
        self._city_admins = []
        self._city_labels = []
        self._city_regions = []
        self._city_grid = {}
        self._city_country_index = {}
        city_values = self.df["city"].fillna("").astype(str).tolist()
        admin_values = self.df["admin_name"].fillna("").astype(str).tolist()
        country_values = self.df["country"].fillna("").astype(str).tolist()
        for i, (city, admin, country, lat, lon) in enumerate(zip(
                city_values, admin_values, country_values,
                self._city_lats, self._city_lons)):
            city = "" if city.lower() == "nan" else city.strip()
            admin = "" if admin.lower() == "nan" else admin.strip()
            country = "" if country.lower() == "nan" else country.strip()
            parts, seen = [], set()
            for value in (city, admin, country):
                if value and value.lower() != "nan" and value not in seen:
                    parts.append(value)
                    seen.add(value)
            self._city_labels.append(", ".join(parts))
            self._city_regions.append((admin, country))
            self._city_names.append(city)
            self._city_admins.append(admin)
            if country:
                self._city_country_index.setdefault(country, []).append(i)
            lat_f = float(lat)
            lon_f = float(lon)
            self._city_grid.setdefault(
                (int(math.floor(lat_f * 10)),
                 int(math.floor(lon_f * 10))),
                [],
            ).append(i)
        self.facts  = facts_data
        self.sound  = SoundEngine()
        self._geo_features = GeoFeatures(GEO_FEATURES_DIR)
        self._geo_features_loading = False
        self._postal_codes = PostalCodeLookup(POSTAL_CODES_DIR)
        self._geo_features_prefetch_lock = threading.Lock()
        self._geo_features_prefetched = set()
        self._geo_features_prefetching = set()
        self.settings = load_settings()
        set_unit_system(self.settings.get("distance_unit", "metric"))
        # Source-test launcher support: allow diagnostics to be enabled before
        # Settings is usable.  This is intentionally opt-in and does not alter
        # the saved settings file.
        if os.environ.get("MIAB_FORCE_DIAGNOSTICS") == "1":
            log_cfg = dict(self.settings.get("logging", {}))
            log_cfg.update({
                "errors": True,
                "street": True,
                "feature_usage": True,
                "navigation": True,
                "verbose": True,
            })
            self.settings["logging"] = log_cfg
        set_language(self.settings.get("language") or None)
        self.settings["_log_path"] = os.path.join(USER_DIR, "miab.log")
        self.speech = SpeechDispatch(trace_cb=self._verbose_trace)

        root = wx.Panel(self)
        root.SetBackgroundColour(COL_BG)
        self._h_sizer = wx.BoxSizer(wx.HORIZONTAL)

        self.map_display_mode = "world"
        self.map_panel = WorldMapPanel(root, owner=self)
        self._map_sizer_item = self._h_sizer.Add(self.map_panel, 3, wx.EXPAND | wx.ALL, 4)
        self.map_panel.Bind(wx.EVT_MOTION, self._on_map_mouse_motion)
        self.map_panel.Bind(wx.EVT_LEFT_DOWN, self._on_map_mouse_click)
        self.map_panel.Bind(wx.EVT_LEFT_DCLICK, self._on_map_mouse_click)

        self.listbox = MSAAListBox(root, style=wx.LB_SINGLE)
        self.listbox.SetBackgroundColour(wx.Colour(10, 20, 40))
        self.listbox.SetForegroundColour(wx.Colour(220, 220, 220))

        self._mode_label = _ModeStaticText(
            root, label="Map mode",
            style=wx.ALIGN_CENTER_HORIZONTAL | wx.WANTS_CHARS)
        self._mode_label.SetBackgroundColour(wx.Colour(10, 20, 40))
        self._mode_label.SetForegroundColour(wx.Colour(220, 220, 220))

        self._btn_ai_summary = wx.Button(root, label="AI Summary (Shift+I)")
        self._btn_ai_summary.SetToolTip("Generate a spoken narrative briefing of the current GPS route")
        self._btn_ai_summary.Bind(wx.EVT_BUTTON, lambda e: self._nav_request_narrative_briefing())
        self._btn_ai_summary.Hide()

        self._list_vsizer = wx.BoxSizer(wx.VERTICAL)
        self._list_vsizer.Add(self._mode_label, 1, wx.EXPAND | wx.ALL, 4)
        self._list_vsizer.Add(self.listbox, 1, wx.EXPAND | wx.ALL, 4)
        self._list_vsizer.Add(self._btn_ai_summary, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 4)
        self._list_sizer_item = self._h_sizer.Add(self._list_vsizer, 1, wx.EXPAND)
        self.listbox.Hide()

        self.info_panel = self._build_info_panel(root)
        self._info_sizer_item = self._h_sizer.Add(self.info_panel, 1, wx.EXPAND | wx.ALL, 4)

        root.SetSizer(self._h_sizer)
        self._map_fullscreen = False
        panel = root

        self.lat  = float(self.settings.get("home_lat", -33.8688))
        self.lon  = float(self.settings.get("home_lon",  151.2093))
        self.last_country_found = ""
        self.current_continent  = ""
        self.last_location_str  = ""
        self.last_city_found    = ""
        self.last_state_found   = ""
        self._update_dialog_active = False
        self._suppress_focus_repeat_until = 0.0
        self._tools_workflow_active = False
        self._poi_fetch_lat         = None   # location where POIs were last fetched
        self._poi_fetch_lon         = None
        self._poi_fetch_in_progress = False
        self._background_poi_fetch_in_progress = False
        self._poi_live_fetch_in_progress = False
        self._poi_live_last_completed_at = 0.0
        self._pending_poi_live_search = None
        self._pending_poi_live_generation = 0
        self._poi_context_generation = 0
        self._pending_pois_ready_sound = False
        self.street_mode        = False
        self.street_label       = ""
        self._road_segments     = []
        self._natural_features  = []
        self._interpolations    = []  # OSM address interpolation data
        self._road_fetched      = False
        self._cache_center_lat  = None  # Track cache validity
        self._cache_center_lon  = None
        self._data_ready        = False  # Flag if data is loaded and valid
        self._loading           = False
        self._road_fetch_lat    = None
        self._road_fetch_lon    = None
        self._poi_list          = []
        self._poi_populating    = False
        self._poi_index         = 0
        self._personal_pois     = _load_personal_pois()
        self._all_pois          = []
        self._poi_live_cache    = {}
        self._street_survey_cache = {}
        self._street_survey_current_poi = None
        self.sounds_enabled     = True
        self._transit           = TransitLookup(script_dir=CACHE_DIR, resource_dir=BASE_DIR)
        self._game              = ChallengeGame(
            announce_cb = lambda msg: wx.CallAfter(self._status_update, msg, True),
            direction_mode_cb = lambda: self.settings.get("challenge_direction_mode", "map"),
            position_tone_cb = self._play_challenge_position_tone,
            country_info_cb = self._challenge_country_info,
            log_cb      = lambda msg: miab_log("challenges", msg, self.settings),
        )
        self._game._current_continent_cb = lambda: getattr(self, 'current_continent', '')
        self._game._current_subregion_cb = lambda: getattr(self, '_current_subregion', '')
        self._session           = None   # ChallengeSession when active
        self._free_mode         = False
        self._free_engine       = FreeExploreEngine()
        self._free_engine.log_settings = self.settings
        self._nav               = NavigationEngine(settings=self.settings)
        self._here              = HerePoi(
            api_key   = self.settings.get("here_api_key", ""),
            cache_dir = CACHE_DIR,
        )
        self._poi_detail_last_key  = -1
        self._poi_detail_last_time = 0.0
        self._last_shopping_store_poi = None
        self._map_marks             = {}     # slot -> {"coords": (lat, lon), "name": str}
        self._map_destination       = None   # {"coords": (lat, lon), "name": str}
        self._prev_lat              = None   # for latitude-line crossing detection
        self._prev_lon              = None   # for Date Line crossing detection
        self._distance_since_fetch  = 0.0
        self._fetch_in_progress     = False
        self._current_subregion     = ""     # for challenge milestone scoring
        self._current_country_code  = ""
        self._prefetch_in_progress  = False  # Shift+F11 background download
        # Mistral client — owns all AI queries
        self._mistral   = MistralClient(script_dir=CACHE_DIR)
        self._mistral.init(self.settings.get("mistral_api_key", ""))
        self._serper = SerperClient(script_dir=CACHE_DIR)
        self._opensky       = OpenSkyClient(
            base_dir=USER_DIR,
            client_id=self.settings.get("opensky_client_id", ""),
            client_secret=self.settings.get("opensky_client_secret", ""))
        self._aviationstack = AviationStackClient(
            self.settings.get("aviationstack_api_key", ""))
        self._priceline = PricelineClient(self.settings.get("rapidapi_key", ""))
        self._tripadvisor = TripAdvisorClient(
            self.settings.get("rapidapi_key", ""),
            os.path.join(CACHE_DIR, "tripadvisor_cache.json"))
        self._timetable     = TimetableClient(
            self.settings.get("rapidapi_key", ""))
        self._flight_dest_cache_path = os.path.join(CACHE_DIR, "flight_dest_cache.json")
        try:
            with open(self._flight_dest_cache_path, encoding="utf-8") as _f:
                self._flight_dest_cache: dict = json.load(_f)
        except Exception:
            self._flight_dest_cache: dict = {}
        self._poi_fetcher       = PoiFetcher(
            overpass=_overpass,
            cache_path=os.path.join(CACHE_DIR, "poi_cache.json"),
            here_api_key=self.settings.get("here_api_key", ""),
        )
        self._street_fetcher    = StreetFetcher(
            overpass=_overpass,
            cache_path=os.path.join(CACHE_DIR, "road_cache"),
        )
        self._init_main_menu_and_toolbar()
        self.listbox.Bind(wx.EVT_LISTBOX, self._on_poi_listbox_select)
        self.listbox.Bind(wx.EVT_SET_FOCUS, self._on_listbox_focus)
        self.listbox.Bind(wx.EVT_CHAR_HOOK, self._on_keyboard)
        self.listbox.Bind(wx.EVT_CHAR, self._on_listbox_char)
        self._mode_label.Bind(wx.EVT_CHAR_HOOK, self._on_keyboard)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_keyboard)
        self.Bind(wx.EVT_ACTIVATE, self._on_activate)
        self.Bind(wx.EVT_CLOSE, self.on_close)
        self._refresh_info_panel()
        # Loading ticker — used for street progress tones.
        self._loading_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_loading_tick, self._loading_timer)
        self._loading_timer.Start(1000)
        self.Show()
        self.Raise()
        self.Maximize(True)
        wx.CallAfter(self._focus_map_window_silently)
        wx.CallLater(200, self._ready)

    def _start_geo_features_background(self):
        threading.Thread(target=self._load_geo_features_background, daemon=True).start()

    def _load_geo_features_background(self):
        """Warm the geographic feature cache for the current location."""
        if getattr(self, "_geo_features_loading", False):
            return
        self._geo_features_loading = True
        try:
            self._prefetch_geo_features_for_point()
        finally:
            self._geo_features_loading = False

    def _prefetch_geo_features_for_point(self, lat: float = None, lon: float = None):
        """Warm the per-country feature cache around a point in the background."""
        if not getattr(self, "_geo_features", None):
            return
        if lat is None:
            lat = self.lat
        if lon is None:
            lon = self.lon

        def _worker():
            try:
                country_code = (getattr(self, "_current_country_code", "") or "").strip().upper()
                if country_code:
                    country_codes = [country_code]
                else:
                    box = 1.0
                    country_codes = self._geo_features._countries_for_box(
                        max(-90.0, lat - box),
                        min(90.0, lat + box),
                        max(-180.0, lon - box),
                        min(180.0, lon + box),
                    )
                for cc in country_codes:
                    with self._geo_features_prefetch_lock:
                        if cc in self._geo_features_prefetched or cc in self._geo_features_prefetching:
                            continue
                        self._geo_features_prefetching.add(cc)
                    try:
                        self._geo_features._load_country(cc)
                    finally:
                        with self._geo_features_prefetch_lock:
                            self._geo_features_prefetching.discard(cc)
                            self._geo_features_prefetched.add(cc)
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True).start()

    def _on_activate(self, event):
        """Window regained focus — let the OS/screen reader read the title."""
        event.Skip()

    def _ready(self):
        self._focus_map_window_silently()
        self._start_geo_features_background()
        # First run — no home location set yet
        if "home_lat" not in self.settings:
            wx.CallAfter(self._setup_home_location)
        else:
            threading.Thread(target=self._lookup, daemon=True).start()
        threading.Thread(target=self._ensure_airports_csv, daemon=True).start()
        # Update check — silent background thread
        self._updater = None
        if UpdateChecker and self.settings.get("check_updates_at_startup", True):
            self._updater = UpdateChecker(
                current_version = APP_VERSION,
                repo            = "sjtaylor82/MapInABox",
                on_update_found = self._on_update_found,
            )
            self._updater.start()

    def _on_update_found(self, latest_version: str, manual: bool = False) -> None:
        if (not manual
                and self.settings.get("skipped_update_version", "") == latest_version):
            return
        self._update_dialog_active = True
        dlg = wx.RichMessageDialog(
            self,
            f"Version {latest_version} of Map in a Box is available.\n\nWould you like to update now?",
            "Update Available",
            wx.YES_NO | wx.YES_DEFAULT | wx.ICON_INFORMATION,
        )
        dlg.ShowCheckBox("Skip this version")
        skip_version = False
        try:
            result = dlg.ShowModal()
            skip_version = dlg.IsCheckBoxChecked()
        finally:
            self._update_dialog_active = False
            dlg.Destroy()

        if skip_version and result != wx.ID_YES:
            self.settings["skipped_update_version"] = latest_version
            save_settings(self.settings)

        if result == wx.ID_YES:
            self._show_update_progress_dialog()
            self._update_last_announced_pct = -1
            threading.Thread(target=self._run_update_download, daemon=True).start()
        else:
            self._return_focus_to_map(repeat=False)
            wx.CallAfter(self._resume_location_sound)

    def _check_for_updates(self) -> None:
        """Run an explicit update check and always report its outcome."""
        if not UpdateChecker:
            wx.MessageBox("Update checking is unavailable in this build.",
                          "Check for Updates", wx.OK | wx.ICON_ERROR)
            return
        if getattr(self, "_manual_update_checking", False):
            self._status_update("An update check is already in progress.", force=True)
            return
        self._manual_update_checking = True
        self._status_update("Checking for updates...", force=True)

        def _found(version):
            self._manual_update_checking = False
            self._on_update_found(version, manual=True)

        def _current():
            self._manual_update_checking = False
            wx.MessageBox(
                f"You are using the latest version, {APP_VERSION}.",
                "Check for Updates", wx.OK | wx.ICON_INFORMATION)

        def _failed():
            self._manual_update_checking = False
            wx.MessageBox(
                "Could not check for updates. Please check your internet connection.",
                "Check for Updates", wx.OK | wx.ICON_ERROR)

        self._updater = UpdateChecker(
            current_version=APP_VERSION,
            repo="sjtaylor82/MapInABox",
            on_update_found=_found,
            on_no_update=_current,
            on_check_error=_failed,
        )
        self._updater.start()

    def _show_update_progress_dialog(self) -> None:
        """Show a native progress bar so screen readers can report progress."""
        self._update_dialog_active = True
        dlg = wx.Dialog(
            self,
            title="Downloading Update",
            style=wx.DEFAULT_DIALOG_STYLE,
        )
        panel = wx.Panel(dlg)
        sizer = wx.BoxSizer(wx.VERTICAL)
        label = wx.StaticText(panel, label="Downloading update...")
        gauge = wx.Gauge(panel, range=100, style=wx.GA_HORIZONTAL)
        try:
            gauge.SetName("Update download progress")
        except Exception:
            pass
        sizer.Add(label, 0, wx.ALL, 12)
        sizer.Add(gauge, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        panel.SetSizer(sizer)
        dlg_sizer = wx.BoxSizer(wx.VERTICAL)
        dlg_sizer.Add(panel, 1, wx.EXPAND)
        dlg.SetSizerAndFit(dlg_sizer)
        dlg.SetSize((360, dlg.GetSize().height))
        dlg.CentreOnParent()
        self._update_progress_dialog = dlg
        self._update_progress_gauge = gauge
        dlg.Show()
        wx.CallAfter(gauge.SetFocus)

    def _set_update_progress(self, pct: int) -> None:
        gauge = getattr(self, "_update_progress_gauge", None)
        if gauge is None:
            return
        try:
            gauge.SetValue(max(0, min(100, int(pct))))
        except Exception:
            pass

    def _close_update_progress_dialog(self) -> None:
        dlg = getattr(self, "_update_progress_dialog", None)
        self._update_progress_dialog = None
        self._update_progress_gauge = None
        self._update_dialog_active = False
        if dlg is not None:
            try:
                dlg.Destroy()
            except Exception:
                pass

    def _run_update_download(self) -> None:
        """Runs on a background thread — download_and_install() does blocking
        network I/O, so it must never run on the wx main thread (that's what
        made the app look frozen/"Not Responding" during the download)."""

        def _progress(pct: int) -> None:
            wx.CallAfter(self._set_update_progress, pct)

        success = self._updater.download_and_install(progress_cb=_progress)
        wx.CallAfter(self._close_update_progress_dialog)
        if success:
            if PORTABLE_MODE and not self._updater.portable_restart_scheduled:
                wx.CallAfter(self._portable_update_requires_manual_download)
                return
            # Installed Windows launches its installer. Portable Windows has
            # scheduled a helper which replaces the app after this process
            # exits and then restarts it. Both must close cleanly here.
            import sys as _sys
            if _sys.platform != "darwin":
                wx.CallAfter(self.Close)
        else:
            wx.CallAfter(
                wx.MessageBox,
                "Update download failed. Please visit the website to download manually.",
                "Update Failed",
                wx.OK | wx.ICON_ERROR,
            )

    def _portable_update_requires_manual_download(self) -> None:
        wx.MessageBox(
            "The release page has been opened because a portable update ZIP "
            "was not available. Map in a Box will remain open.",
            "Portable Update",
            wx.OK | wx.ICON_INFORMATION,
        )
        self._return_focus_to_map(repeat=False)
        wx.CallAfter(self._resume_location_sound)

    def _build_info_panel(self, parent):
        """Create the sighted-user information panel. It never takes focus."""
        panel = wx.Panel(parent)
        panel.SetWindowStyleFlag(panel.GetWindowStyleFlag() & ~wx.TAB_TRAVERSAL)
        panel.SetBackgroundColour(wx.Colour(15, 25, 45))
        panel.SetForegroundColour(wx.Colour(235, 235, 235))

        sizer = wx.BoxSizer(wx.VERTICAL)

        def heading(text):
            label = wx.StaticText(panel, label=text)
            font = label.GetFont()
            font.SetWeight(wx.FONTWEIGHT_BOLD)
            font.SetPointSize(max(10, font.GetPointSize() + 1))
            label.SetFont(font)
            label.SetForegroundColour(wx.Colour(255, 255, 255))
            sizer.Add(label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
            return label

        def value(name, initial="-"):
            title = wx.StaticText(panel, label=name)
            title.SetForegroundColour(wx.Colour(170, 190, 210))
            text = wx.StaticText(panel, label=initial)
            text.SetForegroundColour(wx.Colour(245, 245, 245))
            text.Wrap(230)
            sizer.Add(title, 0, wx.LEFT | wx.RIGHT | wx.TOP, 6)
            sizer.Add(text, 0, wx.LEFT | wx.RIGHT | wx.TOP, 2)
            return text

        self._info_place = value("Place")
        self._info_lat = value("Latitude")
        self._info_lon = value("Longitude")
        self._info_country = value("Country")
        self._info_continent = value("Continent")

        sizer.Add(wx.StaticLine(panel), 0, wx.EXPAND | wx.ALL, 8)
        heading("Facts F6")
        self._info_fact_capital = value("Capital")
        self._info_fact_currency = value("Currency")
        self._info_fact_text = value("Fact")

        sizer.Add(wx.StaticLine(panel), 0, wx.EXPAND | wx.ALL, 8)
        self._info_street = value("Street")
        self._info_status = value("Status")

        sizer.AddStretchSpacer(1)
        panel.SetSizer(sizer)
        panel.SetMinSize((250, -1))
        return panel

    def _set_info_label(self, ctrl, text):
        if not ctrl:
            return
        value = str(text or "-")
        if ctrl.GetLabel() != value:
            ctrl.SetLabel(value)
            ctrl.Wrap(max(180, ctrl.GetParent().GetSize().GetWidth() - 24))

    def _set_status_text(self, text):
        """Update the visual status panel without speech or focus changes."""
        if hasattr(self, "_info_status"):
            self._set_info_label(self._info_status, text)
            self.info_panel.Layout()
            self.info_panel.Refresh()

    def _format_info_coord(self, value, positive, negative):
        try:
            val = float(value)
        except (TypeError, ValueError):
            return "-"
        suffix = positive if val >= 0 else negative
        return f"{abs(val):.5f} {suffix}"

    def _refresh_info_panel(self):
        """Refresh the visual information panel without speech or focus changes."""
        if not hasattr(self, "_info_place"):
            return
        place = getattr(self, "last_location_str", "") or getattr(self, "street_label", "")
        street = getattr(self, "street_label", "") if getattr(self, "street_mode", False) else "Map mode"
        self._set_info_label(self._info_place, place)
        self._set_info_label(self._info_lat, self._format_info_coord(self.lat, "N", "S"))
        self._set_info_label(self._info_lon, self._format_info_coord(self.lon, "E", "W"))
        self._set_info_label(self._info_country, getattr(self, "last_country_found", ""))
        self._set_info_label(self._info_continent, getattr(self, "current_continent", ""))
        self._set_info_label(self._info_street, street)

    def _set_country_facts_panel(self, info=None, country_name=""):
        """Update the visible facts section; speech remains owned by F6."""
        if not hasattr(self, "_info_fact_capital"):
            return
        info = info or {}
        self._set_info_label(self._info_fact_capital, info.get("capital", ""))
        self._set_info_label(self._info_fact_currency, info.get("currency", ""))
        self._set_info_label(self._info_fact_text, info.get("fact", ""))
        self.info_panel.Layout()
        self.info_panel.Refresh()

    def _setup_home_location(self):
        """First-run dialog — ask where the user is and save as home."""
        wx.MessageBox(
            "Welcome to Map in a Box!\n\n"
            "First, where in the world are you?\n"
            "This will be your starting location every time you open the app.\n\n"
            "In the next dialog, type your country or city and press Enter.",
            "Welcome",
            wx.OK | wx.ICON_INFORMATION
        )
        show_open_source_notice(self)
        self._home_setup_mode = True
        self.show_jump_dialog()

    def _init_main_menu_and_toolbar(self):
        """Create visible menus/toolbar for sighted and menu-driven users."""
        self._menu_items = {}

        def new_id():
            return wx.NewIdRef()

        ids = {
            "settings": new_id(), "exit": new_id(),
            "jump": new_id(), "jump_history": new_id(),
            "street": new_id(), "prefetch": new_id(), "city_packs": new_id(),
            "favourites": new_id(),
            "store_mark": new_id(), "jump_mark": new_id(),
            "read_mark_1": new_id(), "read_mark_2": new_id(),
            "read_mark_3": new_id(), "clear_mark": new_id(),
            "mark_distances": new_id(),
            "nearby": new_id(), "nearby_features": new_id(),
            "latitude": new_id(), "longitude": new_id(), "capital": new_id(),
            "airport": new_id(), "overhead": new_id(), "facts": new_id(),
            "wiki": new_id(), "weather": new_id(), "time": new_id(),
            "sun": new_id(), "languages": new_id(), "currency": new_id(),
            "fullscreen": new_id(),
            "poi_address": new_id(), "poi_hours": new_id(),
            "poi_phone": new_id(), "poi_website": new_id(),
            "poi_mistral": new_id(), "poi_menu": new_id(),
            "poi_launch_website": new_id(), "personal_poi": new_id(),
            "poi_search": new_id(), "address": new_id(), "street_search": new_id(),
            "nav_address": new_id(), "nav_briefing": new_id(),
            "intersection": new_id(), "walking": new_id(),
            "add_fav": new_id(),
            "tools": new_id(), "sounds": new_id(), "challenge": new_id(),
            "challenge_multi": new_id(),
            "help": new_id(), "about": new_id(), "manual": new_id(),
            "check_updates": new_id(), "donate": new_id(),
        }
        self._menu_ids = ids

        menubar = wx.MenuBar()

        def add_item(menu, key, label, handler):
            item = menu.Append(ids[key], label)
            self._menu_items[key] = item
            self.Bind(wx.EVT_MENU, handler, id=ids[key])
            self.Bind(wx.EVT_TOOL, handler, id=ids[key])
            return item

        file_menu = wx.Menu()
        add_item(file_menu, "settings", "&Settings\tCtrl+,",
                 lambda e: self._open_settings())
        if IS_MAC:
            self.Bind(wx.EVT_MENU, lambda e: self._open_settings(), id=wx.ID_PREFERENCES)
        file_menu.AppendSeparator()
        add_item(file_menu, "exit", "E&xit\tAlt+F4",
                 lambda e: self.Close())
        menubar.Append(file_menu, "&File")

        go_menu = wx.Menu()
        add_item(go_menu, "jump", "&Jump",
                 lambda e: self.show_jump_dialog())
        add_item(go_menu, "jump_history", "Jump &History\tCtrl+H",
                 lambda e: self.show_jump_history())
        street_mode_label = "&Street Mode\tControl+F11" if IS_MAC else "&Street Mode\tF11"
        add_item(go_menu, "street", street_mode_label,
                 lambda e: self._menu_toggle_street_mode())
        add_item(go_menu, "prefetch", "Pre-download &Streets\tShift+F11",
                 lambda e: self._prefetch_streets())
        add_item(go_menu, "city_packs", "Download City &Data...\tCtrl+Shift+F11",
                 lambda e: self._open_city_pack_wizard())
        add_item(go_menu, "favourites", "&Favourites\tCtrl+F",
                 lambda e: self._show_favourites())
        menubar.Append(go_menu, "&Go")

        marks_menu = wx.Menu()
        add_item(marks_menu, "store_mark", "&Store Mark\tCtrl+M",
                 lambda e: self._prompt_mark_slot(remove=False))
        add_item(marks_menu, "jump_mark", "&Jump to Mark\tCtrl+J",
                 lambda e: self._jump_to_saved_mark())
        marks_menu.AppendSeparator()
        add_item(marks_menu, "read_mark_1", "Read Mark &1\tCtrl+1",
                 lambda e: self._announce_mark(1))
        add_item(marks_menu, "read_mark_2", "Read Mark &2\tCtrl+2",
                 lambda e: self._announce_mark(2))
        add_item(marks_menu, "read_mark_3", "Read Mark &3\tCtrl+3",
                 lambda e: self._announce_mark(3))
        marks_menu.AppendSeparator()
        add_item(marks_menu, "mark_distances", "&Compare Mark Distances\tShift+Alt+M",
                 lambda e: self._report_all_mark_distances())
        add_item(marks_menu, "clear_mark", "&Clear Mark\tCtrl+Shift+M",
                 lambda e: self._prompt_mark_slot(remove=True))
        menubar.Append(marks_menu, "Mar&ks")

        map_menu = wx.Menu()
        add_item(map_menu, "nearby", "&Nearby",
                 lambda e: self._announce_poi_count())
        add_item(map_menu, "nearby_features", "Nearby &Features",
                 lambda e: self._announce_nearby_features())
        map_menu.AppendSeparator()
        add_item(map_menu, "latitude", "&Latitude\tF3",
                 lambda e: self._announce_latitude())
        add_item(map_menu, "longitude", "L&ongitude\tF4",
                 lambda e: self._announce_longitude())
        add_item(map_menu, "capital", "&Capital City\tShift+F1",
                 lambda e: self._announce_capital())
        map_menu.AppendSeparator()
        add_item(map_menu, "airport", "Nearest &Airport",
                 lambda e: self._announce_nearest_airport())
        add_item(map_menu, "overhead", "&Overhead Flights",
                 lambda e: self._announce_overhead_flights())
        add_item(map_menu, "facts", "Country &Facts\tF6",
                 lambda e: self.announce_facts())
        add_item(map_menu, "wiki", "&Wikipedia Summary\tShift+F6",
                 lambda e: self.announce_wikipedia_summary())
        add_item(map_menu, "weather", "&Weather",
                 lambda e: self._announce_weather())
        add_item(map_menu, "time", "&Time",
                 lambda e: self.announce_time())
        add_item(map_menu, "sun", "&Sunrise and Sunset",
                 lambda e: self._announce_sunrise_sunset())
        add_item(map_menu, "languages", "&Languages",
                 lambda e: self._announce_languages())
        add_item(map_menu, "currency", "C&urrency",
                 lambda e: self._announce_currency())
        map_menu.AppendSeparator()
        add_item(map_menu, "fullscreen", "Visual Assist &Mode\tF9",
                 lambda e: self._toggle_map_fullscreen())
        menubar.Append(map_menu, "&Map")

        street_menu = wx.Menu()
        add_item(street_menu, "poi_search", "&POI Search",
                 lambda e: self._announce_poi_count())
        add_item(street_menu, "address", "&Address",
                 lambda e: self._announce_address())
        add_item(street_menu, "street_search", "&Street Search",
                 lambda e: self._street_search())
        add_item(street_menu, "nav_address", "&Navigate to Address",
                 lambda e: self._nav_to_address())
        add_item(street_menu, "nav_briefing", "Narrative &Briefing of Current Route\tShift+I",
                 lambda e: self._nav_request_narrative_briefing())
        add_item(street_menu, "intersection", "Nearest &Intersection",
                 lambda e: self._announce_nearest_intersection())
        add_item(street_menu, "walking", "&Walking Mode",
                 lambda e: self._walk_toggle())
        street_menu.AppendSeparator()
        add_item(street_menu, "add_fav", "Add Current Place to &Favourites\tCtrl+Shift+F",
                 lambda e: self._add_current_favourite())
        menubar.Append(street_menu, "&Street")

        poi_menu = wx.Menu()
        add_item(poi_menu, "poi_address", "Selected POI &Address\tCtrl+Alt+1",
                 lambda e: self._run_after_menu(lambda: self._poi_detail(1)))
        add_item(poi_menu, "poi_hours", "Selected POI &Hours\tCtrl+Alt+2",
                 lambda e: self._run_after_menu(lambda: self._poi_detail(2)))
        add_item(poi_menu, "poi_phone", "Selected POI &Phone\tCtrl+Alt+3",
                 lambda e: self._run_after_menu(lambda: self._poi_detail(3)))
        add_item(poi_menu, "poi_website", "Selected POI &Website\tCtrl+Alt+4",
                 lambda e: self._run_after_menu(lambda: self._poi_detail(4)))
        poi_menu.AppendSeparator()
        add_item(poi_menu, "poi_mistral", "Open Google &Reviews for Selected POI\tCtrl+Alt+5",
                 lambda e: self._run_after_menu(lambda: self._poi_detail(5)))
        add_item(poi_menu, "poi_menu", "Find &Food Menu\tCtrl+Alt+6",
                 lambda e: self._run_after_menu(lambda: self._poi_detail(6)))
        poi_menu.AppendSeparator()
        add_item(poi_menu, "poi_launch_website", "Open POI &Website\tCtrl+W",
                 lambda e: self._run_after_menu(self._open_poi_website))
        poi_menu.AppendSeparator()
        add_item(poi_menu, "personal_poi", "Add &Personal POI Here\tCtrl+Shift+P",
                 lambda e: self._run_after_menu(self._add_personal_poi_here))
        menubar.Append(poi_menu, "&POI")

        tools_menu = wx.Menu()
        add_item(tools_menu, "tools", "&Tools Menu\tF12",
                 lambda e: self._open_tools_menu())
        add_item(tools_menu, "sounds", "Toggle &Sounds\tF7",
                 lambda e: self.toggle_sounds())
        challenge_menu = wx.Menu()
        add_item(challenge_menu, "challenge", "&Challenge\tF10",
                 lambda e: self._run_after_menu(self._menu_toggle_challenge))
        add_item(challenge_menu, "challenge_multi", "&Multi-player Challenge\tCtrl+F10",
                 lambda e: self._run_after_menu(self._menu_toggle_challenge_session))
        tools_menu.AppendSubMenu(challenge_menu, "&Challenge")
        menubar.Append(tools_menu, "&Tools")

        help_menu = wx.Menu()
        add_item(help_menu, "help", "&Help\tF1",
                 lambda e: self.show_help())
        add_item(help_menu, "manual", "&Manual",
                 lambda e: os.startfile(os.path.join(BASE_DIR, "manual.html")))
        add_item(help_menu, "check_updates", "Check for &Updates",
                 lambda e: self._check_for_updates())
        add_item(help_menu, "about", "&About",
                 lambda e: self._show_about())
        help_menu.AppendSeparator()
        add_item(help_menu, "donate", "Donate to Project",
                 lambda e: __import__("webbrowser").open("https://www.paypal.com/donate?business=samtaylor9%40me.com&currency_code=AUD&item_name=Map+in+a+Box"))
        menubar.Append(help_menu, "&Help")

        self.SetMenuBar(menubar)
        # Register important application shortcuts with wx's native accelerator
        # machinery as well as handling them in EVT_CHAR_HOOK.  On macOS some
        # function keys are delivered as menu/accelerator events rather than
        # character-hook events, even when other function keys reach the hook.
        # Keeping both paths makes the behaviour consistent across controls and
        # platforms.  wx.ACCEL_CMD is the native Command modifier on macOS.
        primary_accel = getattr(wx, "ACCEL_CMD", wx.ACCEL_CTRL) if IS_MAC else wx.ACCEL_CTRL
        self._accelerator_table = wx.AcceleratorTable([
            (wx.ACCEL_SHIFT | wx.ACCEL_ALT, ord('M'), int(ids["mark_distances"])),
            (wx.ACCEL_SHIFT | wx.ACCEL_ALT, ord('m'), int(ids["mark_distances"])),
            (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, ord('P'), int(ids["personal_poi"])),
            (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, ord('p'), int(ids["personal_poi"])),
            (wx.ACCEL_NORMAL, wx.WXK_F3, int(ids["latitude"])),
            (wx.ACCEL_NORMAL, wx.WXK_F4, int(ids["longitude"])),
            (wx.ACCEL_NORMAL, wx.WXK_F11, int(ids["street"])),
            (primary_accel, ord(','), int(ids["settings"])),
        ])
        self.SetAcceleratorTable(self._accelerator_table)
        self.Bind(wx.EVT_MENU_OPEN, self._on_main_menu_open)
        self.Bind(wx.EVT_MENU_CLOSE, self._on_main_menu_close)

        toolbar = self.CreateToolBar(wx.TB_HORIZONTAL | wx.TB_TEXT)
        tool_specs = [
            ("jump", "Jump", "Jump to a city, country, or coordinates (J)"),
            ("street", "Street", "Toggle street mode (F11)"),
            ("nearby", "Nearby", "Nearby map menu (/)"),
            ("poi_search", "POIs", "Search points of interest in street mode (P)"),
            ("nav_address", "Navigate", "Navigate to an address in street mode (G)"),
            ("favourites", "Favourites", "Show favourites (Ctrl+F)"),
            ("settings", "Settings", "Open settings (Ctrl+,)"),
            ("help", "Help", "Open help (F1)"),
        ]
        self._toolbar_tools = {}
        for key, label, help_text in tool_specs:
            tool = toolbar.AddTool(
                ids[key], label,
                wx.ArtProvider.GetBitmap(wx.ART_NORMAL_FILE, wx.ART_TOOLBAR, (16, 16)),
                shortHelp=help_text)
            self._toolbar_tools[key] = tool
        toolbar.Realize()
        self._update_main_menu_state()

    def _on_main_menu_open(self, event):
        self._suppress_map_focus_repeat(1500)
        self._update_main_menu_state()
        event.Skip()

    def _on_main_menu_close(self, event):
        self._suppress_map_focus_repeat(1200)
        wx.CallAfter(self._quiet_focus_after_menu_close)
        event.Skip()

    def _quiet_focus_after_menu_close(self):
        if time.time() < getattr(self, "_transient_message_active_until", 0.0):
            return
        if getattr(self, "_poi_list", None):
            return
        self._focus_map_window_silently()

    def _update_main_menu_state(self):
        street = bool(getattr(self, "street_mode", False))
        world = not street and not getattr(self, "_walking_mode", False)
        has_streets = street and bool(getattr(self, "_road_fetched", False))

        for key in ("prefetch",):
            self._menu_items[key].Enable(world)
        for key in (
            "airport", "overhead", "facts", "wiki", "weather", "time",
            "sun", "languages", "currency", "capital",
        ):
            self._menu_items[key].Enable(world)
        for key in (
            "poi_search", "address", "street_search", "nav_address",
            "intersection", "walking", "add_fav",
        ):
            self._menu_items[key].Enable(street)
        self._menu_items["walking"].Enable(has_streets)

        street_shortcut = "Control+F11" if IS_MAC else "F11"
        street_label = (
            f"Exit &Street Mode\t{street_shortcut}"
            if street else f"&Street Mode\t{street_shortcut}"
        )
        self._menu_items["street"].SetItemLabel(street_label)

        toolbar = self.GetToolBar()
        if toolbar:
            toolbar.EnableTool(self._menu_ids["poi_search"], street)
            toolbar.EnableTool(self._menu_ids["nav_address"], street)

    def _menu_toggle_street_mode(self):
        if getattr(self, "_prefetch_in_progress", False) and not self.street_mode:
            self._announce_transient_then_return("Street download in progress. Please wait.")
            return
        self.toggle_street_mode()
        self._update_main_menu_state()

    def _run_after_menu(self, callback):
        wx.CallLater(150, callback)

    def _stop_challenge_session_if_active(self) -> bool:
        """Stop an active challenge session, if any. Returns True if one was stopped."""
        if self._session and self._session.active:
            self._session.stop()
            self._session = None
            self._game._timeout_cb = None
            self._status_update("Challenge session ended.", force=True)
            wx.CallAfter(self._resume_location_sound)
            return True
        return False

    def _menu_toggle_challenge(self):
        if self._stop_challenge_session_if_active():
            return
        if self._game.active:
            self._game.stop()
            wx.CallAfter(self._resume_location_sound)
            return
        if self.df is not None and not self.df.empty:
            self.sound.stop()
            self._game.start(self.df, self.lat, self.lon)
        else:
            self._announce_transient_then_return("No city data available for the challenge.")

    def _menu_toggle_challenge_session(self):
        if self._stop_challenge_session_if_active():
            return
        self._start_challenge_session()

    def _map_mouse_position(self, event):
        x, y = event.GetPosition()
        return self.map_panel.px_to_geo(x, y)

    def _describe_map_mouse_position(self, lat, lon):
        if getattr(self, "street_mode", False):
            try:
                primary, cross = self._nearest_road(lat, lon)
                if primary and primary not in ("No street data", "No street data nearby"):
                    return f"{primary} at {cross}" if cross else primary
            except Exception:
                pass
            return f"{abs(lat):.4f} {'North' if lat >= 0 else 'South'}, {abs(lon):.4f} {'East' if lon >= 0 else 'West'}"

        try:
            dist, idx = _nearest_city(self._city_lats, self._city_lons, lat, lon)
            row = self.df.iloc[idx]
            city = str(row.get("city", "")).strip()
            state = str(row.get("admin_name", "")).strip()
            country = str(row.get("country", "")).strip()
            parts = []
            for value in (city, state, country):
                if value and value.lower() != "nan" and value not in parts:
                    parts.append(value)
            nearest = ", ".join(parts)
            if not _IS_LAND(lat, lon) and dist > 0.01:
                ocean = self._ocean_name(lat, lon)
                return f"{ocean}, near {nearest}" if nearest else ocean
            return nearest or f"{abs(lat):.1f} {'North' if lat >= 0 else 'South'}, {abs(lon):.1f} {'East' if lon >= 0 else 'West'}"
        except Exception:
            return f"{abs(lat):.1f} {'North' if lat >= 0 else 'South'}, {abs(lon):.1f} {'East' if lon >= 0 else 'West'}"

    def _on_map_mouse_motion(self, event):
        if not event.Moving():
            event.Skip()
            return
        x, y = event.GetPosition()
        last_pos = getattr(self, "_last_map_mouse_pos", None)
        if last_pos is None:
            self._last_map_mouse_pos = (x, y)
            event.Skip()
            return
        if abs(x - last_pos[0]) < 8 and abs(y - last_pos[1]) < 8:
            event.Skip()
            return
        self._last_map_mouse_pos = (x, y)
        now = time.time()
        if now < getattr(self, "_map_mouse_speak_after", 0):
            event.Skip()
            return
        lat, lon = self._map_mouse_position(event)
        key = (round(lat, 2), round(lon, 2), bool(getattr(self, "street_mode", False)))
        if key == getattr(self, "_last_map_mouse_key", None):
            event.Skip()
            return
        self._last_map_mouse_key = key
        self._map_mouse_speak_after = now + 0.9
        # Hovering the map should announce the current place without touching
        # the selectable listbox.
        self._refresh_info_panel()
        self._announce_location(self._describe_map_mouse_position(lat, lon))
        event.Skip()

    def _on_map_mouse_click(self, event):
        lat, lon = self._map_mouse_position(event)
        if getattr(self, "street_mode", False):
            self.lat = lat
            self.lon = lon
            self._query_street()
            event.Skip()
            return
        self.lat = lat
        self.lon = lon
        self.street_label = ""
        self._jump_street_label = None
        self._jump_street_pin_lat = None
        self._jump_street_pin_lon = None
        self._jump_address_number = None
        self._jump_address_street = None
        label = self._describe_map_mouse_position(lat, lon)
        self._last_jump_display_label = label
        self._last_jump_display_until = time.time() + 1.5
        wx.CallAfter(self.map_panel.set_position, self.lat, self.lon, False, "")
        wx.CallAfter(self._refresh_info_panel)
        wx.CallAfter(self._announce_location, label)
        threading.Thread(target=self._lookup, daemon=True).start()
        event.Skip()

    def _check_internet(self):
        try:
            urllib.request.urlopen("https://www.google.com", timeout=5)
            return True
        except Exception as e:
            miab_log("errors", f"[Street] Internet check failed: {e}", getattr(self, "settings", None))
            return False

    def _calc_distance_meters(self, lat1, lon1, lat2, lon2):
        """Great-circle distance in metres between two points."""
        return haversine_m(lat1, lon1, lat2, lon2)

    def _should_fetch(self, new_lat, new_lon, force=False):
        """
        Decide if we should trigger a fetch based on accumulated movement.
        
        Args:
            new_lat, new_lon: Position we're moving to
            force: If True, bypass all checks and fetch
            
        Returns:
            bool: True if fetch should be triggered
        """
        if force:
            return True
            
        if self._fetch_in_progress:
            return False
            
        # Calculate distance from last position
        if hasattr(self, '_prev_lat') and self._prev_lat is not None:
            distance = self._calc_distance_meters(
                self._prev_lat, self._prev_lon,
                new_lat, new_lon
            )
            self._distance_since_fetch += distance
        
        # Threshold: 75 meters
        FETCH_THRESHOLD = 75.0
        
        if self._distance_since_fetch >= FETCH_THRESHOLD:
            return True
            
        return False

    def _check_cache_validity(self):
        """
        Check if cache is valid for current location and trigger fetch if needed.
        Called from movement handler BEFORE display.
        """
        if not self.street_mode:
            return
        
        # Check cache center validity
        if self._cache_center_lat is not None and self._cache_center_lon is not None:
            import math
            dlat = (self.lat - self._cache_center_lat) * 111000
            dlon = (self.lon - self._cache_center_lon) * 111000 * math.cos(math.radians(self.lat))
            dist = math.sqrt(dlat**2 + dlon**2)
            
            # Cache invalid if >7km from center
            if dist > 7000:
                miab_log("street", f"[Street] Cache invalid - {dist:.0f}m from center, clearing", getattr(self, "settings", None))
                self._road_segments = []
                self._natural_features = []
                self._interpolations = []
                self._address_points = []
                self._road_fetched = False
                self._data_ready = False
                self._cache_center_lat = None
                self._cache_center_lon = None
                self._empty_cache_announced = False
                
                # Trigger immediate fetch
                if (not self._fetch_in_progress
                        and not getattr(self, "_street_data_fetch_in_progress", False)):
                    self._distance_since_fetch = 0
                    self._fetch_in_progress = True
                    threading.Thread(target=self._query_street, daemon=True).start()
                return
        
        # Check if we have no data at all (first entry to street mode)
        if not self._road_fetched or not self._data_ready:
            if (not self._fetch_in_progress
                    and not getattr(self, "_street_data_fetch_in_progress", False)
                    and not getattr(self, '_loading', False)):
                miab_log("street", f"[Street] No data ready, triggering initial fetch", getattr(self, "settings", None))
                self._distance_since_fetch = 0
                self._fetch_in_progress = True
                threading.Thread(target=self._query_street, daemon=True).start()

    def _update_street_display(self):
        """
        Query cached street/address data and update display.
        Called on EVERY movement in street mode.
        Does not fetch - only reads from cache. DISPLAY ONLY.
        """
        if not self.street_mode:
            return
        
        # If no data ready, announce once and return
        if not self._road_fetched or not self._data_ready:
            if not getattr(self, '_empty_cache_announced', False):
                wx.CallAfter(self._status_update, "Fetching street data")
                self._empty_cache_announced = True
            return
            
        # Query cached road segments.  A numbered street jump can land closer
        # to a parallel/crossing road than to the named street's centreline, so
        # keep the selected street label until the user moves away.
        primary, cross = self._nearest_road(self.lat, self.lon)
        pinned = getattr(self, '_jump_street_label', None)
        if pinned:
            pin_lat = getattr(self, '_jump_street_pin_lat', None)
            pin_lon = getattr(self, '_jump_street_pin_lon', None)
            if pin_lat is None or pin_lon is None or dist_metres(self.lat, self.lon, pin_lat, pin_lon) <= 150.0:
                primary = pinned
                if cross == pinned:
                    cross = None
            else:
                self._jump_street_label = None
                self._jump_street_pin_lat = None
                self._jump_street_pin_lon = None
        
        if primary == "No street data nearby":
            snap = self._nearest_street_point(self.lat, self.lon)
            if not getattr(self, "_street_auto_land_done", False):
                if snap and snap[0] <= 3000.0:
                    snap_dist, snap_lat, snap_lon, snap_label = snap
                    miab_log("snap",
                             f"street display auto-landed to '{snap_label}' at ({snap_lat:.5f},{snap_lon:.5f}) "
                             f"({snap_dist:.1f}m away)",
                             self.settings)
                    self._land_on_street(snap_lat, snap_lon, snap_label)
                    return
            elif snap and snap[0] <= 300.0:
                # Cursor is inside a large feature polygon (e.g. sporting club
                # grounds) whose OSM centroid is >150m from any road. Report
                # the nearest road so the user gets a useful street reference
                # rather than a park/open-area label.
                _, _, _, snap_label = snap
                wx.CallAfter(self._update_location_focus, f"near {snap_label}.")
                return

            location_info = None
            feature_name = None

            if hasattr(self, '_natural_features'):
                cached_feature = self._check_natural_feature(self.lat, self.lon)
                if cached_feature:
                    location_info = cached_feature.get('description')
                    feature_name = cached_feature.get('name')

            if not location_info and hasattr(self, '_geo_features'):
                try:
                    cc = getattr(self, '_current_country_code', None)
                    location_info = (self._geo_lookup_precise(self.lat, self.lon, cc)
                                     or self._geo_lookup_any(self.lat, self.lon, cc))
                except Exception:
                    pass

            if location_info and feature_name:
                msg = f"{location_info}: {feature_name}."
                wx.CallAfter(self._update_location_focus, msg)
            elif location_info:
                wx.CallAfter(self._update_location_focus, location_info)
            else:
                wx.CallAfter(self._update_location_focus, "open area")
            return
        
        # Build label with addresses from cache
        self.street_label = primary
        parts = []
        streets_to_annotate = [primary]
        if cross:
            streets_to_annotate.append(cross)
        intersection_cross = self._street_display_intersection_cross(primary)

        pinned_num    = getattr(self, '_jump_address_number', None)
        pinned_street = getattr(self, '_jump_address_street', None)
        pin_lat       = getattr(self, '_jump_street_pin_lat', None)
        pin_lon       = getattr(self, '_jump_street_pin_lon', None)
        pin_active    = (pinned_num and pinned_street and pin_lat is not None
                         and dist_metres(self.lat, self.lon, pin_lat, pin_lon) <= 150.0)

        for i, st in enumerate(streets_to_annotate):
            if i == 0 and intersection_cross:
                num = ""
            elif i == 0 and pin_active and pinned_street.lower() == st.lower():
                num = pinned_num
            else:
                num = self._nearest_address_number(self.lat, self.lon, st, radius=200)
            if i == 0:
                if intersection_cross:
                    parts.append(f"{st} at {' and '.join(intersection_cross[:2])}")
                else:
                    parts.append(f"{num + ' ' + st if num else st}")
            else:
                parts.append(f"near {st}")

        label = ".  ".join(parts)

        wx.CallAfter(self._update_location_focus, label)

    def _street_display_intersection_cross(self, street_name, radius_m=35.0):
        """Return cross streets when the street display cursor is effectively
        at an intersection on *street_name*.

        This keeps intersection movement anchored to "Street at Cross Street"
        instead of borrowing a nearby house number beside the junction.
        """
        if not street_name or not hasattr(self, "_street_survey_intersections"):
            return []
        try:
            intersections = self._street_survey_intersections(street_name)
        except Exception:
            return []
        best = None
        for _along, nid, nlat, nlon in intersections:
            d = dist_metres(self.lat, self.lon, nlat, nlon)
            if d > radius_m:
                continue
            try:
                cross = self._walk_get_cross_streets(nid, street_name)
            except Exception:
                cross = []
            if cross:
                if best is None or d < best[0]:
                    best = (d, cross)
        return best[1] if best else []

    def _prefetch_streets(self):
        """Shift+F11 — silently download and cache street data for current position."""
        if getattr(self, '_prefetch_in_progress', False):
            self._announce_transient_then_return("Street download already in progress.")
            return
        if self.street_mode:
            self._announce_transient_then_return("Already in street mode.")
            return
        self._prefetch_in_progress = True
        threading.Thread(target=self._run_prefetch, daemon=True).start()

    def _run_prefetch(self):
        import math
        from street_data import geocode_location
        addrs = []
        
        try:
            # Use centralized geocode function instead of duplicate code
            geo = geocode_location(self.lat, self.lon)
            if not geo:
                raise Exception("Geocoding failed")
            
            place = geo.get("suburb", "this area")
            radius = geo.get("radius", 3000)
            bb = geo.get("bbox")
            country_code = geo.get("country_code", "")
            osm_type = geo.get("osm_type")
            osm_id = geo.get("osm_id")

            miab_log("street", f"[Prefetch] Resolved place={place!r}, radius={radius}m", getattr(self, "settings", None))
            
            if bb:
                minlat, maxlat, minlon, maxlon = bb
            else:
                # No bbox - use default radius
                radius = 3000
                place  = "this area"
        except Exception as e:
            miab_log("errors", f"[Prefetch] Nominatim failed: {e}", getattr(self, "settings", None))
            self._prefetch_in_progress = False
            wx.CallAfter(self._announce_transient_then_return, "Could not resolve suburb. Check connection.")
            return

        # Use bbox centre as fetch origin so cache key matches F11 entry.
        if bb:
            bbox_clat = (minlat + maxlat) / 2
            bbox_clon = (minlon + maxlon) / 2
            offset_m = math.sqrt(
                ((bbox_clat - self.lat) * 111000) ** 2 +
                ((bbox_clon - self.lon) * 111000 * math.cos(math.radians(self.lat))) ** 2)
            fetch_lat = bbox_clat if offset_m > 50 else None
            fetch_lon = bbox_clon if offset_m > 50 else None
        else:
            fetch_lat = fetch_lon = None

        # Check if already freshly cached
        from street_data import _load_road_cache, _cache_is_stale
        clat = fetch_lat or self.lat
        clon = fetch_lon or self.lon
        entry = _load_road_cache(self._street_fetcher._cache_dir, clat, clon)
        streets_cached = bool(entry and not _cache_is_stale(entry))
        if streets_cached:
            addrs = entry.get("addresses", []) if isinstance(entry, dict) else []
            wx.CallAfter(self._status_update, f"Streets already cached for {place}. Checking POIs...")
        else:
            wx.CallAfter(self._status_update, f"Downloading streets and POIs for {place}...")
        poi_future = None
        poi_executor = None
        cached_pois = self._poi_fetcher.load_cached_pois(clat, clon)
        if cached_pois is None and not streets_cached:
            import concurrent.futures
            wx.CallAfter(self._status_update, f"Downloading POIs for {place}...")
            poi_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            poi_future = poi_executor.submit(
                self._poi_fetcher.fetch_all_background, clat, clon, [])

        try:
            if not streets_cached:
                _segs, addrs, _from_cache, _snap_lat, _snap_lon, _skip_stage2, _natural, _interps = \
                    self._street_fetcher.fetch_road_data(
                        self.lat, self.lon,
                        radius=radius,
                        fetch_lat=fetch_lat,
                        fetch_lon=fetch_lon,
                        status_cb=lambda msg: wx.CallAfter(self._status_update, msg),
                        suburb_name=place,
                        country_code=country_code,
                        use_gnaf=self.settings.get("gnaf_enabled", True),
                        osm_type=osm_type,
                        osm_id=osm_id,
                    )

            if cached_pois is None:
                if poi_future is not None:
                    pois = poi_future.result()
                else:
                    wx.CallAfter(self._status_update, f"Downloading POIs for {place}...")
                    pois = self._poi_fetcher.fetch_all_background(clat, clon, addrs)
                poi_note = f"{len(pois)} POIs"
            else:
                poi_note = f"{len(cached_pois)} cached POIs"

            street_note = "cached streets" if streets_cached else "streets"
            wx.CallAfter(self._status_update,
                         f"Prepared {place}: {street_note} and {poi_note}.", True)
        except Exception as e:
            miab_log("errors", f"[Prefetch] fetch failed: {e}", getattr(self, "settings", None))
            wx.CallAfter(self._announce_transient_then_return,
                f"Could not prepare {place}. Server may be busy.")
        finally:
            if poi_executor is not None:
                poi_executor.shutdown(wait=False)
            self._prefetch_in_progress = False

    def toggle_street_mode(self):
        if getattr(self, "_loading", False) and not self.street_mode:
            return
        self._loading = False          # silence loading beep immediately on F11
        if self.street_mode:
            self._exit_street_mode()
        else:
            self._pending_street_entry_lat = self.lat
            self._pending_street_entry_lon = self.lon
            self._loading = True
            self._street_loading_announced = True
            self._street_auto_land_done = False
            self._gnaf_recentre_done = False
            if getattr(self, "_suppress_next_street_loading_status", False):
                self._suppress_next_street_loading_status = False
            else:
                self._set_status_text("Loading streets...")
            threading.Thread(target=self._try_enter_street_mode, daemon=True).start()

    def _try_enter_street_mode(self):
        import urllib.request, urllib.parse, json, math
        from street_data import geocode_location

        fetch_seed_lat = getattr(self, "_pending_street_entry_lat", self.lat)
        fetch_seed_lon = getattr(self, "_pending_street_entry_lon", self.lon)
        self._street_request_lat = fetch_seed_lat
        self._street_request_lon = fetch_seed_lon

        # If the current position appears to be open water, load a nearby street
        # grid without moving the real cursor. The land/water polygon is coarse
        # around bays, ports, and reclaimed land, so hidden cursor snaps make the
        # global latitude/longitude untrustworthy.
        if not _IS_LAND(fetch_seed_lat, fetch_seed_lon):
            geo_probe = geocode_location(fetch_seed_lat, fetch_seed_lon)
            suburb_probe = (geo_probe.get("suburb", "") if geo_probe else "").strip()
            country_probe = (geo_probe.get("country_code", "") if geo_probe else "").strip()
            water_key = (
                round(fetch_seed_lat, 4),
                round(fetch_seed_lon, 4),
                suburb_probe.lower(),
                country_probe.lower(),
            )
            if getattr(self, "_water_street_probe_key", None) == water_key:
                miab_log("street", f"[Street] Water probe already done for {water_key}; not retrying.", getattr(self, "settings", None))
                wx.CallAfter(
                    self._announce_transient_then_return,
                    "Water area already probed. Move to land or press Space to try again.",
                )
                return
            self._water_street_probe_key = water_key
            if suburb_probe:
                miab_log("street", f"[Street] Position appears in water ({fetch_seed_lat:.4f},{fetch_seed_lon:.4f}) — "
                    f"loading streets for {suburb_probe} from the current point", getattr(self, "settings", None))
                wx.CallAfter(
                    self.update_ui,
                    "Position appears to be in open water. Loading nearest streets."
                )
            else:
                miab_log("street", f"[Street] Position appears in water ({fetch_seed_lat:.4f},{fetch_seed_lon:.4f}) — "
                    "loading nearby streets from the current point", getattr(self, "settings", None))
                wx.CallAfter(
                    self.update_ui,
                    "Position appears to be in open water. Loading nearest streets."
                )
        else:
            self._water_street_probe_key = None

        # Prefer last_city_found (worldcities CSV, suburb-level) over Nominatim.
        # Read force_geocode flag early — it affects both the geocode seed and
        # the suburb-name preference below.
        force_geocode_suburb = getattr(self, "_force_geocode_suburb_once", False)
        self._force_geocode_suburb_once = False
        map_city = "" if force_geocode_suburb else (getattr(self, 'last_city_found', '') or '')

        # When a named suburb is selected, geocode from its worldcities coordinate
        # rather than the cursor so we get the correct bbox even when the cursor
        # sits across a suburb boundary (e.g. cursor in Ormiston, city = Wellington Point).
        city_seed_lat = getattr(self, '_last_city_found_lat', None)
        city_seed_lon = getattr(self, '_last_city_found_lon', None)
        if map_city and city_seed_lat and city_seed_lon:
            geo_seed_lat, geo_seed_lon = city_seed_lat, city_seed_lon
        else:
            geo_seed_lat, geo_seed_lon = fetch_seed_lat, fetch_seed_lon

        # Clear stale fetch coords before re-geocoding.
        self._street_fetch_lat = None
        self._street_fetch_lon = None

        # Use cached geocoding (checks disk cache -> samtaylor9 -> Nominatim)
        from street_data import geocode_location
        geo = geocode_location(geo_seed_lat, geo_seed_lon)

        if geo:
            radius = geo.get("radius", 3000)
            self._street_radius  = radius
            self._street_barrier = int(radius * 0.9)
            bbox = geo.get("bbox")
            self._street_bbox = bbox

            # Centre the Overpass fetch on the suburb bbox midpoint so the
            # downloaded area is properly centred on the suburb, not the cursor.
            if bbox:
                minlat, maxlat, minlon, maxlon = bbox
                self._street_fetch_lat = (minlat + maxlat) / 2
                self._street_fetch_lon = (minlon + maxlon) / 2
            elif fetch_seed_lat != self.lat or fetch_seed_lon != self.lon:
                self._street_fetch_lat = fetch_seed_lat
                self._street_fetch_lon = fetch_seed_lon

            nominatim_suburb = geo.get("suburb", "") or ""
            if map_city and map_city.lower() not in ("nan", ""):
                if map_city.lower() != nominatim_suburb.lower():
                    miab_log(
                        "verbose",
                        f"Preferring map city '{map_city}' over Nominatim '{nominatim_suburb}'",
                        self.settings,
                    )
                self._current_suburb = map_city
            else:
                if force_geocode_suburb and nominatim_suburb:
                    miab_log(
                        "verbose",
                        f"POI jump using Nominatim suburb '{nominatim_suburb}'",
                        self.settings,
                    )
                self._current_suburb = nominatim_suburb or "this area"
            self._current_country_code = geo.get("country_code", "")
            self._current_osm_type = geo.get("osm_type")
            self._current_osm_id = geo.get("osm_id")
            self._prefetch_geo_features_for_point(fetch_seed_lat, fetch_seed_lon)
        else:
            # Geocoding failed - use fallback
            miab_log("errors", "[Street] Geocoding failed, using 3000m radius fallback", getattr(self, "settings", None))
            self._street_radius  = 3000
            self._street_barrier = 2700
            self._street_bbox = None
            self._current_suburb = None
            self._current_country_code = ""
            self._current_osm_type = None
            self._current_osm_id = None

        wx.CallAfter(self._enter_street_mode)

    def _enter_street_mode(self):
        pending_addr_num = getattr(self, "_pending_jump_address_number", None)
        pending_addr_street = getattr(self, "_pending_jump_address_street", None)
        request_lat = getattr(self, "_street_request_lat", self.lat)
        request_lon = getattr(self, "_street_request_lon", self.lon)
        self.lat = request_lat
        self.lon = request_lon
        self.street_mode    = True
        self._suppress_map_focus_repeat(1200)
        self._last_focus_return_repeat_label = "Street mode"
        self._last_focus_return_repeat_at = time.time()
        # Set content before focusing — a focus event fires as soon as
        # SetFocus() is called, so the screen reader reads whatever the
        # listbox already contains at that moment. Setting the text first
        # means the focus event picks up "Street mode" immediately, instead
        # of whatever was left over from before (which can read as blank
        # or "unknown").
        self._show_mode_surface("Street mode", focus=True)
        self._road_segments  = []
        self._natural_features = []
        self._address_points = []
        self._road_fetched   = False
        self._road_fetch_lat = None
        self._road_fetch_lon = None
        self._pending_snap_lat = None
        self._pending_snap_lon = None
        # _street_fetch_lat/_street_fetch_lon and _street_bbox are prepared by
        # _try_enter_street_mode/_try_enter_new_area; keep them for the fetch.
        # Increment fetch ID to invalidate any stale background threads
        self._street_fetch_id = getattr(self, '_street_fetch_id', 0) + 1
        self._pending_poi_live_search = None
        self._pending_poi_live_generation += 1
        self._poi_context_generation += 1
        self._poi_fetch_in_progress = False
        self._poi_live_last_completed_at = 0.0
        self._pending_pois_ready_sound = False
        self._clear_street_survey_cache()
        self._poi_list          = []
        self._poi_index         = 0
        self._poi_explore_stack = []
        self.street_label       = ""
        self._jump_street_label = None
        self._jump_street_pin_lat = None
        self._jump_street_pin_lon = None
        self._jump_address_number = pending_addr_num
        self._jump_address_street = pending_addr_street
        self._inside_barrier    = True
        self._pending_street_download = False
        self._barrier_dialog_pending = False
        self._gnaf_preloaded    = False
        self._walking_mode      = False
        self._personal_pois     = _load_personal_pois()
        self._all_pois          = self._merge_personal_pois([])
        self._poi_grid          = self._build_poi_grid(self._all_pois)
        self._walk_graph        = None
        self._walk_node         = None
        self._walk_street       = None
        self._walk_heading      = 0.0
        self._walk_browsing     = False
        self._walk_prev_node    = None
        self._walk_preferred_next = None
        self._walk_history      = []
        self._free_mode         = False
        self._free_engine       = FreeExploreEngine()
        self._free_engine.log_settings = self.settings
        self._nav_active        = False
        self._nav_arrived       = False
        self._nav_briefing_mode = False
        self._nav_briefing_steps = []
        self._nav_briefing_step  = 0
        self._nav_route         = []
        self._nav_instructions  = []
        self._nav_step          = 0
        self._nav_dest_name     = ""
        self._nav_dest_lat      = None
        self._nav_dest_lon      = None
        self._nav.reset()
        self.sound._ch.fadeout(500)
        self.sound._current = None
        threading.Thread(target=self._fetch_road_data, daemon=True).start()
        initial_poi_lat = getattr(self, "_street_fetch_lat", None) or request_lat
        initial_poi_lon = getattr(self, "_street_fetch_lon", None) or request_lon
        # Start POI loading right now, in parallel with the street fetch,
        # instead of waiting for street data (and its address points) to
        # finish first — that wait was the entire reason POI results only
        # ever started appearing after streets had fully loaded.
        threading.Thread(
            target=self._prefetch_background_pois,
            args=(initial_poi_lat, initial_poi_lon, [], self._street_fetch_id),
            daemon=True,
        ).start()

    def _exit_street_mode(self, repeat_location=True):
        prev_country = getattr(self, "last_country_found", "")
        prev_continent = getattr(self, "current_continent", "")
        self.street_mode  = False
        self._suppress_map_focus_repeat(1200)
        self._last_focus_return_repeat_label = "Map mode"
        self._last_focus_return_repeat_at = time.time()
        # Content before focus — see matching comment in _enter_street_mode.
        self._show_mode_surface("Map mode", focus=True)
        self.street_label = ""
        self._street_auto_land_done = False
        self._jump_street_label = None
        self._jump_street_pin_lat = None
        self._jump_street_pin_lon = None
        self._jump_address_number = None
        self._jump_address_street = None
        self.last_country_found = prev_country
        self.current_continent = prev_continent
        self.sound._current = None
        self._inside_barrier = True
        self._pending_street_download = False
        self._walking_mode = False
        self._walk_graph   = None
        self._walk_prev_node = None
        self._walk_preferred_next = None
        self._walk_history = []
        self._free_mode = False
        self._free_engine = FreeExploreEngine()
        self._free_engine.log_settings = self.settings
        self._nav.reset()
        self._street_fetch_lat = None
        self._street_fetch_lon = None
        self._empty_cache_announced = False
        self._clear_poi_state()
        self._pending_poi_live_search = None
        self._pending_poi_live_generation += 1
        self._poi_context_generation += 1
        self._poi_fetch_in_progress = False
        self._pending_pois_ready_sound = False
        self._clear_street_survey_cache()
        wx.CallAfter(self.map_panel.set_position, self.lat, self.lon, False, "")
        if prev_country and prev_country != "Open Water":
            self._play_location_sound_if_allowed(prev_country, prev_continent)
        if repeat_location:
            # Follow the mode announcement with the same current-location
            # information spoken by a single F2 press.  Keep it within the
            # requested half-second window without altering F2's tap count.
            wx.CallLater(350, self._repeat_current_location, True)

    def _confirm_fetch_streets(self, suburb_name) -> bool:
        """Show the shared "Fetch <suburb>? / Download Streets" Yes/No dialog."""
        dlg = wx.MessageDialog(
            self,
            f"Fetch {suburb_name}?",
            "Download Streets",
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION
        )
        result = dlg.ShowModal()
        dlg.Destroy()
        return result == wx.ID_YES

    def _confirm_barrier_crossing(self, new_lat, new_lon, suburb_name):
        """Show Yes/No dialog when crossing barrier into uncached suburb."""
        self._barrier_dialog_pending = False  # Allow future prompts once dialog is shown
        suburb_name = suburb_name or "this area"

        if self._confirm_fetch_streets(suburb_name):
            # Move to new position and download
            self.lat = new_lat
            self.lon = new_lon
            self._arrow_download = True  # Flag to prevent auto-recentering
            self._status_update(f"Entering {suburb_name}, downloading streets...")
            self._download_new_area()
        else:
            self._status_update("Download cancelled. Staying in current area.", force=True)

    def _auto_download_poi_suburb(self, lat, lon, poi_name, known_street, suburb_name):
        """Silently download streets after a POI jump within the same suburb — no dialog."""
        self._status_update(f"Downloading streets for {suburb_name}...")
        self._road_fetched = False
        self._road_segments = []
        self._loading = True
        self._fetch_road_data()
        threading.Thread(target=self._fetch_poi_intersection,
                         args=(lat, lon, poi_name, known_street), daemon=True).start()

    def _confirm_poi_suburb_download(self, lat, lon, poi_name, known_street, suburb_name):
        """Show Yes/No dialog to confirm downloading suburb after POI jump."""
        if self._confirm_fetch_streets(suburb_name):
            self._status_update(f"Downloading streets for {suburb_name}...")
            self._road_fetched = False
            self._road_segments = []
            self._loading = True
            self._fetch_road_data()
            threading.Thread(target=self._fetch_poi_intersection,
                           args=(lat, lon, poi_name, known_street), daemon=True).start()
        else:
            self._announce_transient_then_return(f"At {poi_name}. Download cancelled. No street data.")
    
    def _download_new_area(self):
        """Download street data for the current position when outside loaded area.
        Called when user presses space after leaving the cached street boundary."""
        # Check if in water first
        if not _IS_LAND(self.lat, self.lon):
            from street_data import geocode_location
            geo = geocode_location(self.lat, self.lon)
            location_name = geo.get("suburb", "water") if geo else "water"
            self._announce_transient_then_return(f"Can't download. You're in {location_name}.")
            return
        
        self._pending_street_download = False
        self._street_fetch_lat = None
        self._street_fetch_lon = None
        self._street_fetch_id = getattr(self, '_street_fetch_id', 0) + 1
        self._road_segments  = []
        self._natural_features = []
        self._interpolations = []
        self._address_points = []
        self._road_fetched   = False
        self._data_ready     = False
        self._cache_center_lat = None
        self._cache_center_lon = None
        self._gnaf_preloaded = False
        threading.Thread(target=self._try_download_new_area, daemon=True).start()
    
    def _try_download_new_area(self):
        """Background geocoding and fetch for new area download."""
        if not self._check_internet():
            wx.CallAfter(self._announce_transient_then_return, "No internet connection.")
            return
        import math
        from street_data import geocode_location
        
        # Use cached geocoding
        geo = geocode_location(self.lat, self.lon)
        
        if geo:
            # Use radius from geocode_location - no duplicate calculations
            radius = geo.get("radius", 3000)
            self._street_radius  = radius
            self._street_barrier = int(radius * 0.9)
            self._street_bbox = geo.get("bbox")
            self._current_suburb = geo.get("suburb", "this area")
            self._current_country_code = geo.get("country_code", "")
            self._current_osm_type = geo.get("osm_type")
            self._current_osm_id = geo.get("osm_id")
            self._prefetch_geo_features_for_point(self.lat, self.lon)
        else:
            # Geocoding failed - use fallback
            miab_log("errors", "[Street] Geocoding failed, using 3000m radius fallback", getattr(self, "settings", None))
            self._street_radius  = 3000
            self._street_barrier = 2700
            self._street_bbox = None
            self._current_suburb = None
            self._current_country_code = ""
            self._current_osm_type = None
            self._current_osm_id = None
        
        # Fetch road data at current position
        wx.CallAfter(self._fetch_road_data)

    # ── Walking mode ────────────────────────────────────────────────

    def _fetch_road_data(self, _attempt=1):
        # Capture current fetch ID to detect if we become stale
        my_fetch_id = getattr(self, '_street_fetch_id', 0)
        
        if not self.street_mode or self._street_fetch_id != my_fetch_id:
            miab_log("street", "[Street] Fetch aborted — street mode cancelled or superseded.", getattr(self, "settings", None))
            self._loading = False
            self._street_data_fetch_in_progress = False
            self._quiet_gnaf_reload = False
            return


        self._loading = True
        self._street_data_fetch_in_progress = True
        fetch_lat = getattr(self, "_street_fetch_lat", None)
        fetch_lon = getattr(self, "_street_fetch_lon", None)
        request_lat = getattr(self, "_street_request_lat", self.lat)
        request_lon = getattr(self, "_street_request_lon", self.lon)
        request_suburb = getattr(self, "_current_suburb", None)
        request_country_code = getattr(self, "_current_country_code", None)
        request_osm_type = getattr(self, "_current_osm_type", None)
        request_osm_id = getattr(self, "_current_osm_id", None)
        if _attempt == 1:
            suburb = request_suburb or "this area"
            if getattr(self, "_quiet_gnaf_reload", False):
                self._street_loading_announced = True
            elif not getattr(self, "_street_loading_announced", False):
                self._street_loading_announced = True
                wx.CallAfter(self._status_update, f"Loading streets for {suburb}...")

        def _street_fetch_status(msg):
            if getattr(self, "_quiet_gnaf_reload", False):
                return
            if (getattr(self, "_street_loading_announced", False)
                    and str(msg).lower().startswith("loading streets")):
                miab_log("street", f"[Street] Suppressed duplicate status: {msg}", None)
                return
            if str(msg).lower().startswith("loading streets"):
                self._street_loading_announced = True
            wx.CallAfter(self._status_update, msg)

        def _street_stage1_done():
            if not self.street_mode or self._street_fetch_id != my_fetch_id:
                return
            if getattr(self, "_quiet_gnaf_reload", False):
                return
            bg_lat = fetch_lat if fetch_lat is not None else request_lat
            bg_lon = fetch_lon if fetch_lon is not None else request_lon
            if (not getattr(self, "_poi_fetch_in_progress", False)
                    and not getattr(self, "_background_poi_fetch_in_progress", False)):
                miab_log(
                    "verbose",
                    f"Starting background POI fetch alongside address fetch at "
                    f"({bg_lat:.5f},{bg_lon:.5f}).",
                    self.settings,
                )
                threading.Thread(
                    target=self._fetch_all_pois_background,
                    args=([], False, bg_lat, bg_lon, my_fetch_id),
                    daemon=True,
                ).start()
            wx.CallAfter(self._announce_transient, "Nearly Done.")

        def _street_fetch_cancelled():
            return (not self.street_mode
                    or getattr(self, "_street_fetch_id", 0) != my_fetch_id)

        try:
            (
                segs,
                addrs,
                from_cache,
                snap_lat,
                snap_lon,
                skip_stage2,
                natural_features,
                interpolations,
            ) = self._street_fetcher.fetch_road_data(
                request_lat,
                request_lon,
                radius=self._street_radius,
                fetch_lat=fetch_lat,
                fetch_lon=fetch_lon,
                status_cb=_street_fetch_status,
                stage1_done_cb=_street_stage1_done,
                suburb_name=request_suburb,
                country_code=request_country_code,
                use_gnaf=self.settings.get("gnaf_enabled", True),
                osm_type=request_osm_type,
                osm_id=request_osm_id,
                cancel_cb=_street_fetch_cancelled,
            )

            if not self.street_mode or self._street_fetch_id != my_fetch_id:
                miab_log("street", "[Street] Fetch complete but street mode was cancelled or superseded — discarding.", getattr(self, "settings", None))
                self._loading = False
                self._street_loading_announced = False
                self._street_data_fetch_in_progress = False
                self._quiet_gnaf_reload = False
                return
            moved_since_request = dist_metres(self.lat, self.lon, request_lat, request_lon)
            current_suburb = getattr(self, "_current_suburb", None)
            if ((request_suburb and current_suburb and request_suburb != current_suburb)
                    or moved_since_request > max(getattr(self, "_street_radius", 3000) * 2, 10000)):
                miab_log(
                    "snap",
                    f"Discarded stale street fetch for {request_suburb or 'this area'}; "
                    f"cursor moved {moved_since_request:.0f}m to {current_suburb or 'unknown area'}.",
                    self.settings,
                )
                self._loading = False
                self._street_loading_announced = False
                self._street_data_fetch_in_progress = False
                self._quiet_gnaf_reload = False
                return

            # Count only named, driveable streets — not bush tracks, footways
            # or unnamed service roads.
            _LOW = {"footway", "cycleway", "path", "steps", "track", "bridleway"}
            _GENERIC = {"road", "highway", "street", "residential street",
                        "shared street", "service road", "motorway", "footpath",
                        "cycle path", "path", "steps", "pedestrian area",
                        "dirt track", "bridleway", "road under construction"}
            named_segs = sum(
                1 for s in segs
                if s.get("kind", "") not in _LOW
                and s.get("raw_name", s.get("name", "")).strip()
                and s.get("raw_name", s.get("name", "")).strip().lower() not in _GENERIC
            )
            if named_segs < 20 and not from_cache and self._street_radius < 1800:
                wider = min(self._street_radius * 2, 2000)
                miab_log("street", f"[Street] Only {named_segs} named streets — widening to {wider}m and retrying from player position", getattr(self, "settings", None))
                if not getattr(self, "_quiet_gnaf_reload", False):
                    wx.CallAfter(self._status_update,
                                 f"Only {named_segs} streets found, expanding search area...")
                self._street_radius  = wider
                self._street_barrier = int(wider * 0.9)
                self._street_fetch_lat = None
                self._street_fetch_lon = None
                self._loading = False
                self._street_data_fetch_in_progress = False
                self._fetch_road_data(_attempt=_attempt + 1)
                return

            # ── Early recentre — don't wait for GNAF ─────────────────
            # If Stage 1 returned no named streets and no addresses,
            # we're likely positioned in water or far from the street grid.
            # Snap to the suburb centre immediately via reverse geocoding
            # rather than waiting 5-6s for GNAF to notice the same thing.
            if named_segs == 0 and not segs and not from_cache:
                def _fast_recentre():
                    clat, clon = None, None
                    try:
                        import urllib.request as _ur, urllib.parse as _up, json as _j
                        params = _up.urlencode({
                            "lat": self.lat,
                            "lon": self.lon,
                            "format": "json",
                            "zoom": 12,
                            "addressdetails": 1,
                        })
                        req = _ur.Request(
                            f"https://nominatim.openstreetmap.org/reverse?{params}",
                            headers={"User-Agent": "MapInABox/1.0"},
                        )
                        with _ur.urlopen(req, timeout=5) as r:
                            data = _j.loads(r.read().decode())
                        bb = data.get("boundingbox")
                        if bb and len(bb) == 4:
                            clat = (float(bb[0]) + float(bb[1])) / 2
                            clon = (float(bb[2]) + float(bb[3])) / 2
                    except Exception:
                        pass
                    if clat is not None:
                        dist = math.sqrt(((clat - self.lat)*111000)**2 +
                                         ((clon - self.lon)*111000)**2)
                        if dist > 200:
                            miab_log("street", f"[Street] Fast fetch-centre shift: {dist:.0f}m to suburb centre", None)
                            self._recentring = True
                            self._street_fetch_lat = clat
                            self._street_fetch_lon = clon
                            self._road_segments  = []
                            self._natural_features = []
                            self._interpolations = []
                            self._road_fetched   = False
                            self._loading        = False
                            if not getattr(self, "_quiet_gnaf_reload", False):
                                wx.CallAfter(self._status_update, "Loading street grid from suburb centre...")
                            threading.Thread(target=self._fetch_road_data, daemon=True).start()
                threading.Thread(target=_fast_recentre, daemon=True).start()
                return

            # ── Stage 1 complete — announce immediately ───────────────
            self._road_segments  = segs
            self._natural_features = natural_features  # Store natural features from fetch
            self._interpolations = interpolations  # Store address interpolation data
            self._address_points = addrs
            self._clear_street_survey_cache()
            fetch_origin_lat = fetch_lat if fetch_lat is not None else self.lat
            fetch_origin_lon = fetch_lon if fetch_lon is not None else self.lon
            self._cache_center_lat = fetch_origin_lat  # Track cache center for validity
            self._cache_center_lon = fetch_origin_lon
            self._data_ready = True  # Data is now ready for display
            miab_log(
                "verbose",
                f"Stored {len(addrs)} address points, {len(interpolations)} interpolations; "
                f"GNAF {'on' if self.settings.get('gnaf_enabled', True) else 'off'}; "
                f"sources={sorted({str(a.get('source', 'unknown')) for a in addrs if isinstance(a, dict)})}",
                self.settings,
            )
            try:
                self._free_engine.set_segments(segs)
            except Exception:
                pass
            self._road_fetched   = True
            self._recentring     = False
            self._street_loading_announced = False
            self._street_data_fetch_in_progress = False

            _jlabel = getattr(self, "_jump_street_label", None)
            miab_log("snap",
                     f"fetch done: {len(segs)} segs, cursor=({self.lat:.4f},{self.lon:.4f}), "
                     f"snap_pt=({snap_lat},{snap_lon}), jump_label='{_jlabel}'",
                     self.settings)
            # If player is still not on a street, snap to the nearest loaded
            # street. Applies whether data came from cache or a live fetch.
            # Only happens once per street-mode entry (_street_auto_land_done
            # guards re-entry). Use a generous 3000m cap so water/coastal
            # positions that are well outside the street grid still land.
            if (not getattr(self, "_jump_street_label", None)
                    and not getattr(self, "_arrow_download", False)
                    and not getattr(self, "_street_auto_land_done", False)):
                _primary, _ = self._nearest_road(self.lat, self.lon)
                miab_log("snap",
                         f"fetch post-load nearest_road='{_primary}' at ({self.lat:.4f},{self.lon:.4f})",
                         self.settings)
                if _primary in ("No street data", "No street data nearby"):
                    # Use the fetch centre (suburb bbox midpoint) as the snap
                    # origin when available — this lands the cursor in the
                    # selected suburb rather than at the nearest road to a
                    # water/off-road cursor position.
                    _land_lat = snap_lat or self.lat
                    _land_lon = snap_lon or self.lon
                    if _land_lat != self.lat or _land_lon != self.lon:
                        miab_log("snap",
                                 f"using fetch centre ({_land_lat:.4f},{_land_lon:.4f}) as snap origin",
                                 self.settings)
                    snap = self._nearest_street_point(_land_lat, _land_lon)
                    if snap:
                        _d, _clat, _clon, _label = snap
                        miab_log("snap",
                                 f"nearest street '{_label}' is {_d:.0f}m away at ({_clat:.4f},{_clon:.4f})",
                                 self.settings)
                        if _d <= 3000:
                            self._street_auto_land_done = True
                            self.lat = _clat
                            self.lon = _clon
                            self._prev_lat = _clat
                            self._prev_lon = _clon
                            self._distance_since_fetch = 0.0
                            self._pending_snap_lat = None
                            self._pending_snap_lon = None
                            # fall through — POI loading, display, and sound
                            # all run in the normal flow below
            
            # Clear arrow download flag
            if getattr(self, "_arrow_download", False):
                self._arrow_download = False

            self._road_fetch_lat = fetch_origin_lat
            self._road_fetch_lon = fetch_origin_lon

            _jump_label = getattr(self, "_jump_street_label", None)
            if _jump_label:
                label = _jump_label
                self._jump_street_label = None
                self._jump_street_pin_lat = None
                self._jump_street_pin_lon = None
            else:
                label, cross = self._nearest_road(self.lat, self.lon)
            self.street_label = label
            cross = None if _jump_label else (locals().get("cross") or None)
            wx.CallAfter(self.map_panel.set_position, self.lat, self.lon, True, label)
            wx.CallAfter(self._update_street_display)
            wx.CallAfter(self._play_roads_ready_sound)
            if getattr(self, "_pending_pois_ready_sound", False):
                self._pending_pois_ready_sound = False
                wx.CallAfter(self._play_pois_ready_sound)

            pending_nav = getattr(self, "_pending_nav_after_street_load", None)
            if pending_nav:
                self._pending_nav_after_street_load = None
                wx.CallAfter(self._nav_launch, *pending_nav)

            _glat = fetch_lat or self.lat
            _glon = fetch_lon or self.lon
            threading.Thread(target=self._gnaf_preload_addresses,
                             args=(_glat, _glon), daemon=True).start()

            # POI loading was already kicked off in parallel back in
            # _enter_street_mode (see _prefetch_background_pois). Calling it
            # again here — now with real address points for label enrichment
            # — is a safe no-op in the common case (the earlier call's
            # _poi_fetch_in_progress guard skips a duplicate live fetch) and
            # a useful fallback if that earlier attempt hasn't run yet or
            # found nothing cached.
            threading.Thread(
                target=self._prefetch_background_pois,
                args=(_glat, _glon, self._address_points, my_fetch_id),
                daemon=True,
            ).start()

            # ── Stage 2 — full radius background fetch ────────────────
            # _loading stays True so progress beeps continue until done.
            if not from_cache and not skip_stage2:
                def _outer_fetch(clat=_glat, clon=_glon,
                                 rad=self._street_radius, segs_so_far=segs):
                    try:
                        merged, full_addrs = \
                            self._street_fetcher.live_fetch_outer(
                                clat, clon, rad, segs_so_far,
                                status_cb=lambda msg: (
                                    None if getattr(self, "_quiet_gnaf_reload", False)
                                    else wx.CallAfter(self._status_update, msg)
                                ),
                            )
                        if not self.street_mode or self._street_fetch_id != my_fetch_id:
                            self._quiet_gnaf_reload = False
                            return
                        self._road_segments  = merged
                        self._address_points = full_addrs or self._address_points
                        self._clear_street_survey_cache()
                        try:
                            self._free_engine.set_segments(merged)
                        except Exception:
                            pass
                        # Silently rebuild walk graph if walking mode active
                        if getattr(self, '_walking_mode', False):
                            self._walk_graph = self._build_walk_graph()
                            self._nav.set_graph(self._walk_graph)
                        self._loading = False
                        quiet_reload = getattr(self, "_quiet_gnaf_reload", False)
                        self._quiet_gnaf_reload = False
                        wx.CallAfter(self.map_panel.set_position,
                                     self.lat, self.lon, True, self.street_label)
                        # Only update status if user is idle
                        if not (quiet_reload or
                                self._poi_list or
                                getattr(self, '_walking_mode', False) or
                                getattr(self, '_free_mode', False)):
                            size_msg = ""
                            if self.settings.get("announce_suburb_size") and self._street_bbox:
                                import math as _m
                                _sminlat, _smaxlat, _sminlon, _smaxlon = self._street_bbox
                                _clat = (_sminlat + _smaxlat) / 2
                                _wkm = (_smaxlon - _sminlon) * 111.0 * _m.cos(_m.radians(_clat))
                                _hkm = (_smaxlat - _sminlat) * 111.0
                                size_msg = (f"  {self._current_suburb or 'Suburb'} "
                                            f"is {format_distance(_wkm * 1000)} wide by {format_distance(_hkm * 1000)} tall.")
                            wx.CallAfter(self._status_update,
                                         f"Streets fully loaded.  "
                                         f"{len(merged)} streets in area.{size_msg}")
                    except Exception as exc:
                        miab_log("errors", f"[Street] Stage 2 error: {exc}", None)
                        self._loading = False
                        self._quiet_gnaf_reload = False
                threading.Thread(target=_outer_fetch, daemon=True).start()
            else:
                # Cache hit or boundary query — no Stage 2 needed, stop beeps immediately
                self._loading = False
                quiet_reload = getattr(self, "_quiet_gnaf_reload", False)
                self._quiet_gnaf_reload = False
                if (self.settings.get("announce_suburb_size") and self._street_bbox
                        and not (quiet_reload or
                                 self._poi_list or
                                 getattr(self, '_walking_mode', False) or
                                 getattr(self, '_free_mode', False))):
                    import math as _m
                    _sminlat, _smaxlat, _sminlon, _smaxlon = self._street_bbox
                    _clat = (_sminlat + _smaxlat) / 2
                    _wkm = (_smaxlon - _sminlon) * 111.0 * _m.cos(_m.radians(_clat))
                    _hkm = (_smaxlat - _sminlat) * 111.0
                    wx.CallAfter(self._status_update,
                                 f"{self._current_suburb or 'Suburb'} "
                                 f"is {format_distance(_wkm * 1000)} wide by {format_distance(_hkm * 1000)} tall.")

        except Exception as e:
            if _street_fetch_cancelled():
                miab_log(
                    "street",
                    "[Street] Cancelled fetch stopped before further network work.",
                    getattr(self, "settings", None),
                )
                return
            miab_log("errors", f"[Street] fetch error: {e}", getattr(self, "settings", None))
            self._loading = False
            self._street_data_fetch_in_progress = False
            self._street_loading_announced = False
            quiet_reload = getattr(self, "_quiet_gnaf_reload", False)
            self._quiet_gnaf_reload = False
            if quiet_reload:
                return
            wx.CallAfter(self._status_update,
                         "Street servers unavailable and no cached data for this area.  "
                         "Try again later with F11, or move to a previously visited area.",
                         True)

    def _nearest_road(self, lat, lon):
        """Thin delegator — see StreetFetcher.nearest_road."""
        return self._street_fetcher.nearest_road(lat, lon, self._road_segments)

    def _nearest_street_point(self, lat, lon, street_name=None):
        """Return the nearest point on a loaded street segment. If
        *street_name* is given, only that named street is considered;
        otherwise the nearest point on any loaded named street is returned."""
        target = street_name.strip().lower() if street_name else None
        if street_name and not target:
            return None
        best = None
        for seg in getattr(self, "_road_segments", []):
            label = re.sub(r"\s*\(.*?\)", "", seg.get("name", "")).strip()
            if target is None:
                if not label:
                    continue
            elif label.lower() != target:
                continue
            coords = seg.get("coords", [])
            if len(coords) < 2:
                continue
            for i in range(len(coords) - 1):
                a_lat, a_lon = coords[i]
                b_lat, b_lon = coords[i + 1]
                snap_lat, snap_lon = nearest_point_on_segment(
                    lat, lon, a_lat, a_lon, b_lat, b_lon
                )
                dist = dist_metres(lat, lon, snap_lat, snap_lon)
                if best is None or dist < best[0]:
                    best = (dist, snap_lat, snap_lon, label)
        return best

    def _land_on_street(self, lat, lon, label):
        """Move the cursor onto a nearby street after street data loads."""
        self._street_auto_land_done = True
        self.lat = lat
        self.lon = lon
        self.street_label = label
        self._prev_lat = lat
        self._prev_lon = lon
        self._distance_since_fetch = 0.0
        self._pending_snap_lat = None
        self._pending_snap_lon = None
        wx.CallAfter(self.map_panel.set_position, self.lat, self.lon, True, label)
        wx.CallAfter(self._update_street_display)

    # ── Transit / GTFS unified system ────────────────────────────────────────
    # All GTFS logic is in transit_lookup.TransitLookup (self._transit).
    # MapNavigator only calls public methods on self._transit.
    # The catalog DataFrame is cached inside the TransitLookup instance.

    def _refresh_transit_catalog(self):
        """F12 — force-refresh the MobilityData catalog CSV and validate columns."""
        wx.CallAfter(self._status_update, "Refreshing transit catalog...")
        df = self._transit.refresh_catalog()
        if df is None:
            wx.CallAfter(self._status_update,
                "Transit catalog update failed. Check your connection.")
            return
        ok, missing = self._transit.validate_catalog_columns()
        if not ok:
            wx.CallAfter(self._status_update,
                f"Catalog schema changed — missing columns: {', '.join(missing)}. "
                f"Transit lookup may not work correctly.")
        else:
            wx.CallAfter(self._status_update,
                f"Transit catalog updated: {len(df)} active feeds worldwide.")

    # ── GNAF bbox disk cache ──────────────────────────────────────────────────

    _GNAF_CACHE_PATH = os.path.join(CACHE_DIR, "gnaf_cache.json")
    _GNAF_CACHE_TTL  = 90 * 86400   # 90 days

    def _gnaf_cache_key(self, lat, lon, radius):
        return f"{round(lat, 2)}_{round(lon, 2)}_{radius}"

    def _gnaf_cache_load(self, key):
        try:
            with open(self._GNAF_CACHE_PATH, encoding="utf-8") as f:
                store = json.load(f)
            entry = store.get(key)
            if entry and (time.time() - entry["ts"]) < self._GNAF_CACHE_TTL:
                return entry["addresses"]
        except Exception:
            pass
        return None

    def _gnaf_cache_save(self, key, addresses):
        try:
            try:
                with open(self._GNAF_CACHE_PATH, encoding="utf-8") as f:
                    store = json.load(f)
            except Exception:
                store = {}
            store[key] = {"ts": time.time(), "addresses": addresses}
            # Evict entries older than TTL
            cutoff = time.time() - self._GNAF_CACHE_TTL
            store = {k: v for k, v in store.items() if v["ts"] > cutoff}
            with open(self._GNAF_CACHE_PATH, "w", encoding="utf-8") as f:
                json.dump(store, f, ensure_ascii=False)
        except Exception:
            pass

    # ── GNAF preload ──────────────────────────────────────────────────────────

    def _confirm_or_toggle_gnaf_addresses(self):
        now = time.time()
        pending_until = getattr(self, "_gnaf_toggle_pending_until", 0.0)
        if pending_until and now <= pending_until:
            self._gnaf_toggle_pending_until = 0.0
            self._toggle_gnaf_addresses()
            return
        state = "on" if self.settings.get("gnaf_enabled", True) else "off"
        self._gnaf_toggle_pending_until = now + 5.0
        self._status_update(
            f"GNAF {state}. Press again to toggle.",
            force=True,
        )
        wx.CallLater(5000, self._clear_gnaf_toggle_pending)

    def _clear_gnaf_toggle_pending(self):
        if time.time() >= getattr(self, "_gnaf_toggle_pending_until", 0.0):
            self._gnaf_toggle_pending_until = 0.0

    def _toggle_gnaf_addresses(self):
        self._gnaf_toggle_pending_until = 0.0
        enabled = not self.settings.get("gnaf_enabled", True)
        self.settings["gnaf_enabled"] = enabled
        save_settings(self.settings)
        self._gnaf_preloaded = False
        state = "on" if enabled else "off"
        miab_log("feature_usage", f"GNAF addresses turned {state}", self.settings)
        if self.street_mode:
            self._address_points = []
            self._road_fetched = False
            self._loading = True
            self._street_data_fetch_in_progress = True
            self._quiet_gnaf_reload = True
            wx.CallAfter(self._status_update, f"GNAF {state}.", True)
            wx.CallLater(
                1000,
                lambda: threading.Thread(target=self._fetch_road_data, daemon=True).start(),
            )
        else:
            self._status_update(f"GNAF {state}.", force=True)

    def _gnaf_preload_addresses(self, lat, lon, radius=2000):
        """Fetch all GNAF addresses for current area and merge into _address_points."""
        if not GNAF_URL:
            return
        if not self.settings.get("gnaf_enabled", True):
            miab_log("verbose", "GNAF preload skipped because GNAF is disabled.", self.settings)
            return
        if not self.street_mode:
            return
        # Guard against multiple calls per street mode entry — widening retries
        # and recentring each trigger _fetch_road_data which calls this again,
        # accumulating addresses from multiple suburbs into one pool and
        # producing a wildly incorrect centroid.
        if getattr(self, "_gnaf_preloaded", False):
            return
        self._gnaf_preloaded = True
        import urllib.request, urllib.parse
        try:
            cache_key = self._gnaf_cache_key(lat, lon, radius)
            addresses = self._gnaf_cache_load(cache_key)
            if addresses is None:
                params = urllib.parse.urlencode({
                    "mode": "bbox", "lat": round(lat, 6),
                    "lon": round(lon, 6), "radius": radius,
                })
                url = f"{GNAF_URL.rstrip('/')}?{params}"
                req = urllib.request.Request(url, headers={"User-Agent": "MapInABox/1.0"})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read().decode())
                addresses = data.get("addresses", [])
                if addresses:
                    self._gnaf_cache_save(cache_key, addresses)
            if not addresses:
                return
            # Merge into _address_points, avoiding duplicates
            existing = {(a["number"], a["street"].lower(), round(a["lat"],4), round(a["lon"],4))
                        for a in getattr(self, "_address_points", [])}
            added = 0
            for a in addresses:
                key = (a["number"], a["street"].lower(),
                       round(a["lat"],4), round(a["lon"],4))
                if key not in existing:
                    existing.add(key)
                    self._address_points.append({
                        "number": a["number"],
                        "street": a["street"],
                        "lat":    a["lat"],
                        "lon":    a["lon"],
                        "source": "gnaf",
                    })
                    added += 1
            if self.settings.get("here_api_key", "").strip():
                return
            if (added > 50 and self._road_fetched
                    and not getattr(self, "_water_street_probe_key", None)
                    and not getattr(self, "_gnaf_recentre_done", False)):
                label, _ = self._nearest_road(self.lat, self.lon)
                if "No street data" in label:
                    lats = [a["lat"] for a in addresses]
                    lons = [a["lon"] for a in addresses]
                    clat = sum(lats) / len(lats)
                    clon = sum(lons) / len(lons)
                    self._gnaf_recentre_done = True
                    self._recentring = True
                    self._street_fetch_lat = clat
                    self._street_fetch_lon = clon
                    self._road_segments = []
                    self._natural_features = []
                    self._road_fetched = False
                    self._gnaf_preloaded = False
                    wx.CallAfter(self._status_update, "Loading street grid from address data...")
                    threading.Thread(target=self._fetch_road_data, daemon=True).start()
        except Exception:
            pass

    def _interpolate_address_number(self, lat, lon, street_name):
        """Interpolate house number from OSM addr:interpolation ways.
        
        Projects position onto nearest interpolation segment, calculates position
        along the way between endpoints, and interpolates the number.
        
        Returns interpolated number or None if no suitable interpolation found.
        """
        import math
        
        SUFFIXES = {
            "street", "st", "road", "rd", "avenue", "ave", "drive", "dr",
            "court", "ct", "place", "pl", "crescent", "cres", "close", "cl",
            "boulevard", "blvd", "highway", "hwy", "terrace", "tce",
            "parade", "pde", "esplanade", "esp", "lane", "ln", "grove", "gr",
            "way", "circuit", "cct", "rise", "row", "mews", "track",
        }
        
        def bare(s):
            """Normalize street name for matching."""
            parts = s.lower().split(",")[0].strip().split()
            if parts and parts[-1] in SUFFIXES:
                parts = parts[:-1]
            return " ".join(parts)
        
        def distance_m(lat1, lon1, lat2, lon2):
            """Distance in meters between two points."""
            dlat = (lat2 - lat1) * 111000
            dlon = (lon2 - lon1) * 111000 * math.cos(math.radians((lat1 + lat2) / 2))
            return math.sqrt(dlat**2 + dlon**2)
        
        clean_street = bare(street_name)
        
        # Find interpolation ways for this street
        candidates = []
        for interp in getattr(self, "_interpolations", []):
            if bare(interp["street"]) == clean_street:
                candidates.append(interp)
        
        if not candidates:
            return None
        
        # Find nearest segment and calculate position
        best_interp = None
        best_distance = float("inf")
        best_fraction = 0.0
        
        for interp in candidates:
            coords = interp["coords"]
            if len(coords) < 2:
                continue
            
            # Calculate total way length first
            total_length = 0.0
            for i in range(len(coords) - 1):
                seg_len = distance_m(coords[i][0], coords[i][1], 
                                    coords[i+1][0], coords[i+1][1])
                total_length += seg_len
            
            if total_length < 1.0:  # Degenerate way
                continue
            
            # Find closest point on any segment
            distance_along = 0.0
            for i in range(len(coords) - 1):
                lat1, lon1 = coords[i]
                lat2, lon2 = coords[i + 1]
                
                # Convert to meters for easier math
                x1 = lon1 * 111000 * math.cos(math.radians(lat))
                y1 = lat1 * 111000
                x2 = lon2 * 111000 * math.cos(math.radians(lat))
                y2 = lat2 * 111000
                px = lon * 111000 * math.cos(math.radians(lat))
                py = lat * 111000
                
                # Segment vector
                dx = x2 - x1
                dy = y2 - y1
                seg_len_sq = dx*dx + dy*dy
                
                if seg_len_sq < 1e-10:
                    distance_along += 0
                    continue
                
                # Project point onto line segment (clamped to [0,1])
                t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / seg_len_sq))
                
                # Closest point on this segment
                closest_x = x1 + t * dx
                closest_y = y1 + t * dy
                
                # Distance from user to closest point
                dist = math.sqrt((px - closest_x)**2 + (py - closest_y)**2)
                
                if dist < best_distance:
                    best_distance = dist
                    best_interp = interp
                    
                    # Position along entire way = sum of previous segments + t * current segment
                    seg_len = math.sqrt(seg_len_sq)
                    best_fraction = (distance_along + t * seg_len) / total_length
                
                # Add this segment length for next iteration
                distance_along += math.sqrt(seg_len_sq)
        
        # Only use if within 50m of an interpolation way
        if best_interp is None or best_distance > 50:
            return None
        
        # Interpolate the house number
        start_num = best_interp["start"]["num"]
        end_num = best_interp["end"]["num"]
        interp_type = best_interp["type"]
        
        # Linear interpolation
        interpolated = start_num + (end_num - start_num) * best_fraction
        
        # Apply odd/even/all logic
        if interp_type == "odd":
            # Round to nearest odd number
            result = int(round(interpolated))
            if result % 2 == 0:
                # Choose the odd neighbor closest to interpolated value
                if interpolated > result:
                    result += 1
                else:
                    result -= 1
        elif interp_type == "even":
            # Round to nearest even number
            result = int(round(interpolated))
            if result % 2 == 1:
                # Choose the even neighbor closest to interpolated value
                if interpolated > result:
                    result += 1
                else:
                    result -= 1
        elif interp_type == "all":
            # Round to nearest integer
            result = int(round(interpolated))
        else:
            # Numeric step interpolation (e.g., type="2" means step by 2)
            try:
                step = int(interp_type)
                result = int(round(interpolated / step) * step)
            except (ValueError, TypeError):
                result = int(round(interpolated))
        
        # Clamp to valid range
        min_num = min(start_num, end_num)
        max_num = max(start_num, end_num)
        result = max(min_num, min(result, max_num))
        
        return str(result)

    def _address_point_source_enabled(self, ap):
        if not isinstance(ap, dict):
            return False
        source = str(ap.get("source", "") or "").lower()
        return source != "gnaf" or self.settings.get("gnaf_enabled", True)

    def _cache_addresses_for_current_gnaf_mode(self, cache_entry):
        if not isinstance(cache_entry, dict):
            return []
        addresses = cache_entry.get("addresses", [])
        if self.settings.get("gnaf_enabled", True):
            return addresses
        country_code = str(getattr(self, "_current_country_code", "") or "").lower()
        if country_code == "au" and cache_entry.get("address_source", "") != "osm":
            miab_log("verbose", "Cached GNAF/unknown addresses ignored because GNAF is disabled.", self.settings)
            return []
        return [a for a in addresses if self._address_point_source_enabled(a)]

    def _nearest_address_number(self, lat, lon, street_name, radius=500):
        """Return nearest house number on street_name.
        
        Tries discrete address points first, falls back to interpolation.
        Filters by street FIRST, then returns nearest by distance.
        No hard radius cutoff - returns nearest available address on that street.
        radius parameter is ignored - kept for API compatibility.
        """
        SUFFIXES = {
            "street", "st",
            "road", "rd",
            "avenue", "ave",
            "drive", "dr",
            "court", "ct",
            "place", "pl",
            "crescent", "cres",
            "close", "cl",
            "boulevard", "blvd",
            "highway", "hwy",
            "terrace", "tce",
            "parade", "pde",
            "esplanade", "esp",
            "lane", "ln",
            "grove", "gr",
            "way",
            "circuit", "cct",
            "rise", "row", "mews", "track",
        }
        def bare(s):
            parts = s.lower().split(",")[0].strip().split()
            if parts and parts[-1] in SUFFIXES:
                parts = parts[:-1]
            return " ".join(parts)

        clean = bare(street_name)
        
        # Try discrete address points first
        best = None; best_d = float("inf")
        address_candidates = []
        pending_num = getattr(self, "_pending_jump_address_number", None)
        pending_street = getattr(self, "_pending_jump_address_street", None)
        if pending_num and pending_street:
            address_candidates.append({
                "street": pending_street,
                "number": pending_num,
                "lat": getattr(self, "_pending_jump_address_lat", None),
                "lon": getattr(self, "_pending_jump_address_lon", None),
            })
        for ap in getattr(self, "_address_points", []):
            if not self._address_point_source_enabled(ap):
                continue
            address_candidates.append({
                "street": ap.get("street", ""),
                "number": ap.get("number", ""),
                "lat": ap.get("lat"),
                "lon": ap.get("lon"),
            })
        for poi in getattr(self, "_all_pois", []) or []:
            number = (poi.get("number") or poi.get("tags", {}).get("addr:housenumber") or "").strip()
            street = (poi.get("street") or poi.get("tags", {}).get("addr:street") or "").strip()
            if number and street:
                address_candidates.append({
                    "street": street,
                    "number": number,
                    "lat": poi.get("lat"),
                    "lon": poi.get("lon"),
                })

        for ap in address_candidates:
            if bare(ap.get("street", "")) != clean:
                continue
            if not ap.get("number") or ap.get("lat") is None or ap.get("lon") is None:
                continue
            d = math.sqrt(((lat - float(ap["lat"])) * 111000)**2 +
                          ((lon - float(ap["lon"])) * 111000 *
                           math.cos(math.radians(lat)))**2)
            if d < best_d:
                best_d = d; best = ap["number"]
        
        # If found discrete address, return it
        if best is not None:
            return best
        
        # Fall back to interpolation
        return self._interpolate_address_number(lat, lon, street_name)

    def _check_natural_feature(self, lat, lon):
        """Check if location is inside a cached natural feature.
        Returns dict with 'description' and 'name', or None if not found."""
        
        if not hasattr(self, '_natural_features') or not self._natural_features:
            return None
        
        from shapely.geometry import Point, Polygon
        
        point = Point(lon, lat)  # Shapely uses (lon, lat) order
        
        # Check each feature to see if point is inside
        for feature in self._natural_features:
            try:
                coords = feature.get("coords", [])
                if len(coords) < 3:  # Need at least 3 points for a polygon
                    continue
                
                # Convert to (lon, lat) tuples for Shapely
                poly_coords = [(c[1], c[0]) for c in coords]
                polygon = Polygon(poly_coords)
                
                if polygon.contains(point) or polygon.boundary.distance(point) < 0.0001:  # ~10m tolerance
                    feature_type = feature.get("type", "")
                    feature_name = feature.get("name", "")
                    
                    # Map feature types to descriptions
                    type_map = {
                        # Natural features
                        'water': 'over water',
                        'wetland': 'in wetlands',
                        'wood': 'in woodland',
                        'scrub': 'in scrubland',
                        'grassland': 'in grassland',
                        'beach': 'at beach',
                        'coastline': 'at coast',
                        'heath': 'on heath',
                        # Waterways
                        'river': 'at river',
                        'stream': 'at stream',
                        'canal': 'at canal',
                        'drain': 'at drain',
                        # Leisure
                        'park': 'in park',
                        'nature_reserve': 'in nature reserve',
                        'recreation_ground': 'at recreation area',
                        # Landuse
                        'farmland': 'in farmland',
                        'orchard': 'in orchard',
                        'vineyard': 'in vineyard',
                        'meadow': 'in meadow',
                        'forest': 'in forest',
                        'grass': 'on grassland',
                        'quarry': 'at quarry',
                        # Barriers
                        'fence': 'at fence',
                        'hedge': 'at hedge',
                        'gate': 'at gate',
                    }
                    
                    description = type_map.get(feature_type, f'in {feature_type}')
                    
                    return {
                        'description': description,
                        'name': feature_name if feature_name else None
                    }
            
            except Exception as e:
                # Skip invalid geometries
                continue
        
        return None
    
    def _query_street(self):
        """Called on each arrow keypress in street mode."""
        try:
            if not self.street_mode:
                return

            wx.CallAfter(self.map_panel.set_position,
                         self.lat, self.lon, True, self.street_label)

            # If outside barrier, don't bother querying — no data here
            if not getattr(self, '_inside_barrier', True):
                miab_log("snap", f"_query_street: outside barrier at ({self.lat:.5f},{self.lon:.5f})", self.settings)
                return

            if not self._road_fetched:
                miab_log("snap", f"_query_street: roads not fetched yet", self.settings)
                # Silent - _update_street_display owns speech in street mode
                return

            seg_count = len(self._road_segments) if hasattr(self, '_road_segments') else 0
            _qpin = getattr(self, "_jump_street_label", None)
            miab_log("snap",
                     f"_query_street: at ({self.lat:.5f},{self.lon:.5f}), {seg_count} segs loaded, "
                     f"pin='{_qpin}'",
                     self.settings)

            primary, cross = self._nearest_road(self.lat, self.lon)
            pinned = getattr(self, '_jump_street_label', None)
            if pinned:
                pin_lat = getattr(self, '_jump_street_pin_lat', None)
                pin_lon = getattr(self, '_jump_street_pin_lon', None)
                pinned_snap = self._nearest_street_point(self.lat, self.lon, pinned)
                if pinned_snap and pinned_snap[0] <= 80.0:
                    _snap_dist, pin_lat, pin_lon, _ = pinned_snap
                    self._jump_street_pin_lat = pin_lat
                    self._jump_street_pin_lon = pin_lon
                    pin_dist = _snap_dist
                else:
                    pin_dist = dist_metres(self.lat, self.lon, pin_lat, pin_lon) if (pin_lat and pin_lon) else None
                miab_log("snap",
                         f"_query_street: pin active='{pinned}', pin_dist={pin_dist:.1f}m" if pin_dist is not None
                         else f"_query_street: pin active='{pinned}', pin pos unknown",
                         self.settings)
                if pin_lat is None or pin_lon is None or dist_metres(self.lat, self.lon, pin_lat, pin_lon) <= 150.0:
                    primary = pinned
                    if cross == pinned:
                        cross = None
                else:
                    miab_log("snap",
                             f"_query_street: releasing pin '{pinned}' (moved {pin_dist:.1f}m > 150m from pin)",
                             self.settings)
                    self._jump_street_label = None
                    self._jump_street_pin_lat = None
                    self._jump_street_pin_lon = None
                    self._jump_address_number = None
                    self._jump_address_street = None
                    self._pending_jump_address_number = None
                    self._pending_jump_address_street = None
                    self._pending_jump_address_lat = None
                    self._pending_jump_address_lon = None
            miab_log("snap", f"_query_street: result primary='{primary}' cross='{cross}'", self.settings)
            
            if primary == "No street data nearby":
                # Check what feature we're actually in/near
                location_info = None
                feature_name = None
                
                # Try cached natural features first (most accurate)
                if not location_info:
                    cached_feature = self._check_natural_feature(self.lat, self.lon)
                    if cached_feature:
                        location_info = cached_feature.get('description')
                        feature_name = cached_feature.get('name')
                
                # Last resort: land checker
                if not location_info and hasattr(self, 'land_checker'):
                    if not self.land_checker.is_on_land(self.lat, self.lon):
                        location_info = "over water"
                
                # Build status message
                if self._road_fetch_lat is not None:
                    dlat = (self.lat - self._road_fetch_lat) * 111000
                    dlon = (self.lon - self._road_fetch_lon) * 111000 * math.cos(
                        math.radians(self.lat))
                    dist = int(math.sqrt(dlat**2 + dlon**2))
                    if location_info:
                        if feature_name:
                            msg = f"{self.street_label}.  {location_info}: {feature_name}."
                            miab_log("street", f"[Query] No streets, {location_info}: {feature_name}, {dist}m from centre", getattr(self, "settings", None))
                        else:
                            msg = f"{self.street_label}.  {location_info}."
                            miab_log("street", f"[Query] No streets, {location_info}, {dist}m from centre", getattr(self, "settings", None))
                    else:
                        msg = f"{self.street_label}."
                        miab_log("street", f"[Query] No street data, {dist}m from centre", getattr(self, "settings", None))
                    
                    # Silent - _update_street_display owns speech in street mode
                elif self.street_label:
                    # Silent - _update_street_display owns speech in street mode
                    pass
                return

            self.street_label = primary

            # Build label with single nearest house number per street.
            parts = []
            streets_to_annotate = [primary]
            if cross:
                streets_to_annotate.append(cross)

            pinned_num    = getattr(self, '_jump_address_number', None)
            pinned_street = getattr(self, '_jump_address_street', None)
            pin_lat       = getattr(self, '_jump_street_pin_lat', None)
            pin_lon       = getattr(self, '_jump_street_pin_lon', None)
            pin_active    = (pinned_num and pinned_street and pin_lat is not None
                             and dist_metres(self.lat, self.lon, pin_lat, pin_lon) <= 150.0)

            for i, st in enumerate(streets_to_annotate):
                if i == 0 and pin_active and pinned_street.lower() == st.lower():
                    num = pinned_num
                else:
                    num = self._nearest_address_number(self.lat, self.lon, st, radius=200)
                if i == 0:
                    parts.append(f"{num + ' ' + st if num else st}")
                else:
                    parts.append(f"near {st}")

            label = ".  ".join(parts)
            # Silent - _update_street_display owns speech in street mode
        finally:
            # Always clear fetch flag, even on error or early return
            self._fetch_in_progress = False

    def _fetch_google_pois(self, category_key="all", radius=1000, name_filter=""):
        """Fetch Google POIs through PoiFetcher."""
        return self._poi_fetcher.fetch_google_pois(
            self.lat, self.lon,
            self.settings.get("google_api_key", ""),
            category_key=category_key,
            radius=radius,
            name_filter=name_filter,
        )

    def _queue_poi_live_search(self, still_in_progress=False, **params):
        """Queue the newest live POI search until the live-fetch cooldown expires.

        still_in_progress distinguishes two different situations that both
        land here: an actual live fetch is still running right now, versus
        the previous fetch already finished and this is just the brief
        pacing cooldown before another request is allowed. Saying "still
        working on the last search" in the second case is simply wrong —
        the last search is done — so it gets its own accurate wording.
        """
        params["lat"] = self.lat
        params["lon"] = self.lon
        self._pending_poi_live_search = params
        self._pending_poi_live_generation += 1
        generation = self._pending_poi_live_generation
        miab_log(
            "verbose",
            "p queued live POI search because another live fetch is active or cooling down.",
            self.settings,
        )
        # Previously this was silent — logged to file only, with nothing
        # spoken to the user. Since the caller sets self._loading = False
        # right before returning here, the loading-tick heartbeat doesn't
        # fire during this wait either, so a queued search gave zero
        # audible feedback until the eventual completion chime. Announce
        # it immediately instead.
        msg = ("Still working on the last search — this one will run next."
               if still_in_progress else
               "One moment before the next search starts...")
        wx.CallAfter(self._status_update, msg, True)
        self._start_pending_poi_feedback(generation)
        self._schedule_pending_poi_live_search(generation)

    def _start_pending_poi_feedback(self, generation):
        self._loading = True
        self._last_street_loading_beep_at = 0.0

        def _feedback_tick():
            params = getattr(self, "_pending_poi_live_search", None)
            if not params or generation != getattr(self, "_pending_poi_live_generation", 0):
                return
            if not (getattr(self, "street_mode", False) or getattr(self, "_free_mode", False)):
                return
            self._loading = True
            if getattr(self, "_poi_live_fetch_in_progress", False):
                wx.CallAfter(self._status_update, "Still working on the last search...", True)
            else:
                remaining = POI_LIVE_COOLDOWN_SECS - (
                    time.time() - getattr(self, "_poi_live_last_completed_at", 0.0)
                )
                if remaining <= 0:
                    return
                wx.CallAfter(
                    self._status_update,
                    f"Next POI search starts in {int(math.ceil(remaining))} seconds...",
                    True,
                )
            wx.CallAfter(wx.CallLater, 5000, _feedback_tick)

        wx.CallAfter(wx.CallLater, 4000, _feedback_tick)

    def _poi_live_fetch_started(self):
        self._poi_live_fetch_in_progress = True

    def _poi_live_fetch_finished(self):
        self._poi_live_fetch_in_progress = False
        self._poi_live_last_completed_at = time.time()
        if getattr(self, "_pending_poi_live_search", None):
            self._pending_poi_live_generation += 1
            self._schedule_pending_poi_live_search(self._pending_poi_live_generation)

    def _schedule_pending_poi_live_search(self, generation=None):
        if generation is None:
            generation = self._pending_poi_live_generation
        cooldown = POI_LIVE_COOLDOWN_SECS
        remaining = max(0.0, cooldown - (time.time() - getattr(self, "_poi_live_last_completed_at", 0.0)))
        delay_ms = int(max(remaining, 0.25) * 1000)
        # wx.CallLater (unlike wx.CallAfter) creates and starts a wx.Timer
        # immediately, which wx requires to happen on the main thread. This
        # method is called both from the main thread (when a search is first
        # queued) and from background fetch-completion threads (via
        # _poi_live_fetch_finished) — calling wx.CallLater directly from the
        # latter crashed with "timer can only be started from the main
        # thread", silently dropping the queued search. Routing the
        # CallLater creation itself through CallAfter makes this safe from
        # either thread.
        wx.CallAfter(wx.CallLater, delay_ms, self._run_pending_poi_live_search, generation)

    def _run_pending_poi_live_search(self, generation):
        params = getattr(self, "_pending_poi_live_search", None)
        if not params or generation != getattr(self, "_pending_poi_live_generation", 0):
            return
        if getattr(self, "_poi_live_fetch_in_progress", False):
            self._schedule_pending_poi_live_search(generation)
            return
        remaining = POI_LIVE_COOLDOWN_SECS - (time.time() - getattr(self, "_poi_live_last_completed_at", 0.0))
        if remaining > 0:
            self._schedule_pending_poi_live_search(generation)
            return
        queued_lat = params.pop("lat", None)
        queued_lon = params.pop("lon", None)
        if queued_lat is not None and queued_lon is not None:
            if dist_metres(self.lat, self.lon, queued_lat, queued_lon) > POI_BACKGROUND_RADIUS_METRES:
                miab_log("verbose", "p dropped queued live POI search because the map moved away.", self.settings)
                self._pending_poi_live_search = None
                self._loading = False
                return
        if not (getattr(self, "street_mode", False) or getattr(self, "_free_mode", False)):
            miab_log("verbose", "p dropped queued live POI search because map browsing is inactive.", self.settings)
            self._pending_poi_live_search = None
            self._loading = False
            return
        self._pending_poi_live_search = None
        threading.Thread(target=self._fetch_pois, kwargs=params, daemon=True).start()

    def _fetch_pois(self, category_key="all", radius=1000, timeout=30,
                    next_radius=0, name_filter="", street_filter="", source="",
                    bypass_live_guard=False):
        """Fetch POIs for *category_key* from the requested *source*.

        source: "osm" | "here" | "google" | "" (auto — here if key set, else osm)
        """
        import threading
        self._loading             = True
        self._poi_fetch_in_progress = True
        poi_context_generation = getattr(self, "_poi_context_generation", 0)
        category_key = (category_key or "all").lower()
        name_filter  = (name_filter or "").strip().lower()
        street_filter = (street_filter or "").strip().lower()
        here_key   = self.settings.get("here_api_key",   "").strip()
        google_key = self.settings.get("google_api_key", "").strip()
        if not source:
            poi_source = self.settings.get("poi_source", "osm")
            source = poi_source if (poi_source == "here" and here_key) else "osm"
        category_labels = dict(POI_CATEGORY_CHOICES)
        category_label  = category_labels.get(category_key, "All nearby")

        search_beep_started = False
        search_radii = [radius]
        if next_radius and next_radius > radius:
            search_radii.append(next_radius)
        if name_filter:
            # Name searches should use the radius the user chose directly.
            # Incremental radius probing caused extra Overpass requests and
            # made the search feel like it was hammering the servers.
            search_radii = [radius]
        else:
            # Keep plain category browsing local.
            search_radii = sorted(set(search_radii))

        live_started = False

        def _name_match(poi):
            if not name_filter:
                return True
            label = (poi.get("name") or poi.get("label") or "").lower()
            kind  = (poi.get("kind") or "").lower()
            return name_filter in label or name_filter in kind

        def _street_bare(text):
            parts = re.sub(r"[^a-z0-9 ]+", " ", str(text or "").lower()).split()
            suffixes = {
                "street", "st", "road", "rd", "avenue", "ave", "drive", "dr",
                "court", "ct", "place", "pl", "crescent", "cres", "lane", "ln",
                "grove", "gr", "parade", "pde", "terrace", "tce", "way",
            }
            if parts and parts[-1] in suffixes:
                parts = parts[:-1]
            return " ".join(parts)

        street_filter_bare = _street_bare(street_filter)

        def _street_match(poi):
            if not street_filter_bare:
                return True
            tags = poi.get("tags", {}) if isinstance(poi, dict) else {}
            values = [
                poi.get("street", ""),
                poi.get("address", ""),
                poi.get("addr", ""),
                poi.get("label", ""),
                tags.get("addr:street", ""),
            ]
            return any(street_filter_bare in _street_bare(v) for v in values)

        try:
            cached_presented = False
            cached_pois = []

            def _poi_context_is_current():
                return poi_context_generation == getattr(self, "_poi_context_generation", 0)

            def _discard_if_poi_context_changed(stage):
                if _poi_context_is_current():
                    return False
                miab_log(
                    "verbose",
                    f"p discarded {stage} POI result because the browsing context changed.",
                    self.settings,
                )
                self._loading = False
                self._poi_fetch_in_progress = False
                return True

            def _prepare_pois(raw_pois, radius_m=None):
                _suppressed = _load_suppressed()
                _renamed    = _load_renamed()
                prepared = _apply_renames(
                    [p for p in raw_pois if not _is_suppressed(p, _suppressed)],
                    _renamed)
                for poi in prepared:
                    if "distance_m" not in poi and poi.get("dist") is not None:
                        poi["distance_m"] = poi["dist"]
                if radius_m is not None:
                    current = []
                    for poi in prepared:
                        plat = poi.get("lat")
                        plon = poi.get("lon")
                        if plat is None or plon is None:
                            continue
                        d = dist_metres(self.lat, self.lon, plat, plon)
                        if d > radius_m:
                            continue
                        item = dict(poi)
                        item["dist"] = d
                        item["distance_m"] = d
                        current.append(item)
                    prepared = current
                prepared.sort(key=lambda x: x.get("dist", float("inf")))
                return prepared

            if ((name_filter or street_filter)
                    and source in ("osm", "here")
                    and getattr(self, "_background_poi_fetch_in_progress", False)):
                miab_log(
                    "verbose",
                    "p keyword search waiting for in-progress background POI list.",
                    self.settings,
                )
                wx.CallAfter(self._status_update, "Checking loaded POIs...", True)
                wait_started = time.time()
                while (getattr(self, "_background_poi_fetch_in_progress", False)
                       and time.time() - wait_started < POI_BACKGROUND_WAIT_SECS):
                    if not _poi_context_is_current():
                        self._loading = False
                        self._poi_fetch_in_progress = False
                        return
                    time.sleep(0.25)

            background = list(getattr(self, "_all_pois", []) or [])
            if ((name_filter or street_filter)
                    and source in ("osm", "here")
                    and not background):
                disk_bg = self._poi_fetcher.load_cached_pois(self.lat, self.lon)
                if disk_bg:
                    self._all_pois = self._merge_personal_pois(disk_bg)
                    self._poi_grid = self._build_poi_grid(self._all_pois)
                    self._poi_fetch_lat = self.lat
                    self._poi_fetch_lon = self.lon
                    background = list(self._all_pois)
                    miab_log(
                        "verbose",
                        f"p loaded {len(background)} background POIs from disk for keyword search.",
                        self.settings,
                    )
            if background and source in ("osm", "here"):
                fetch_lat = getattr(self, "_poi_fetch_lat", None)
                fetch_lon = getattr(self, "_poi_fetch_lon", None)
                background_distance = (
                    dist_metres(self.lat, self.lon, fetch_lat, fetch_lon)
                    if fetch_lat is not None and fetch_lon is not None
                    else float("inf")
                )
                sources = set()
                for p in background:
                    if not isinstance(p, dict):
                        continue
                    poi_source = (p.get("source") or "osm").lower()
                    sources.add(poi_source)
                source_matches = not sources or source in sources
                location_matches = background_distance <= POI_BACKGROUND_RADIUS_METRES
                radius_covered = max(search_radii) <= POI_BACKGROUND_RADIUS_METRES
                if name_filter or street_filter:
                    miab_log(
                        "verbose",
                        f"p keyword cache check: background={len(background)} "
                        f"sources={sorted(sources)} source={source} "
                        f"distance={background_distance:.0f}m "
                        f"radius={max(search_radii)}m "
                        f"source_matches={source_matches} "
                        f"location_matches={location_matches} "
                        f"radius_covered={radius_covered}.",
                        self.settings,
                    )
                if source_matches and location_matches and radius_covered:
                    cached_pois = filter_pois_by_category(background, category_key)
                    cached_pois = [p for p in cached_pois if _name_match(p) and _street_match(p)]
                    cached_pois = _prepare_pois(cached_pois, radius_m=max(search_radii))
                    if name_filter or street_filter:
                        if cached_pois:
                            if not _poi_context_is_current():
                                miab_log(
                                    "verbose",
                                    "p discarded cached POI result because the browsing context changed.",
                                    self.settings,
                                )
                                self._loading = False
                                self._poi_fetch_in_progress = False
                                return
                            self._poi_list = cached_pois
                            self._poi_index = 0
                            filters = []
                            if name_filter:
                                filters.append(f"name '{name_filter}'")
                            if street_filter:
                                filters.append(f"street '{street_filter}'")
                            miab_log(
                                "verbose",
                                f"p served keyword search from loaded background POIs: "
                                f"{len(cached_pois)} {category_key} matching "
                                f"{', '.join(filters)} via {source}; live search skipped.",
                                self.settings,
                            )
                            wx.CallAfter(self._present_poi_list)
                        else:
                            miab_log(
                                "verbose",
                                f"p loaded background POIs cover this area; no cached "
                                f"{category_key} result matching name='{name_filter}' "
                                f"street='{street_filter}'. Live search skipped.",
                                self.settings,
                            )
                            filters = []
                            if name_filter:
                                filters.append(f"'{name_filter}'")
                            if street_filter:
                                filters.append(f"on {street_filter}")
                            what = " ".join(filters) if filters else category_label.lower()
                            wx.CallAfter(
                                self._retry_poi_name_search,
                                category_key,
                                name_filter,
                                street_filter,
                                source,
                                max(search_radii),
                            )
                        self._loading = False
                        self._poi_fetch_in_progress = False
                        return
                    if cached_pois:
                        if not _poi_context_is_current():
                            miab_log(
                                "verbose",
                                "p discarded cached POI result because the browsing context changed.",
                                self.settings,
                            )
                            self._loading = False
                            self._poi_fetch_in_progress = False
                            return
                        self._poi_list     = cached_pois
                        self._poi_index    = 0
                        cached_presented = True
                        filters = []
                        if name_filter:
                            filters.append(f"name '{name_filter}'")
                        if street_filter:
                            filters.append(f"street '{street_filter}'")
                        name_desc = " matching " + ", ".join(filters) if filters else ""
                        miab_log(
                            "verbose",
                            f"p served from in-memory background POIs: "
                            f"{len(cached_pois)} {category_key}{name_desc} results via {source}.",
                            self.settings,
                        )
                        wx.CallAfter(self._present_poi_list)
                        if not name_filter and max(search_radii) <= POI_BACKGROUND_RADIUS_METRES:
                            self._loading = False
                            self._poi_fetch_in_progress = False
                            return
                        if max(search_radii) <= POI_BACKGROUND_RADIUS_METRES:
                            live_reason = (
                                "typed search may have matches missing from "
                                "the background cache"
                            )
                        else:
                            live_reason = (
                                f"configured radius {max(search_radii)}m exceeds "
                                f"background radius {POI_BACKGROUND_RADIUS_METRES}m"
                            )
                        miab_log(
                            "verbose",
                            f"p doing live {source.upper()} fetch: {live_reason}.",
                            self.settings,
                        )
                    elif name_filter or street_filter:
                        miab_log(
                            "verbose",
                            f"p in-memory background POIs cover this area; "
                            f"no cached {category_key} result matching "
                            f"name='{name_filter}' street='{street_filter}'; "
                            f"continuing with live {source.upper()} search.",
                            self.settings,
                        )
                        wx.CallAfter(
                            self._status_update,
                            "No cached match yet. Searching live nearby...",
                            True,
                        )
                    else:
                        miab_log(
                            "verbose",
                            "p found no in-memory background POIs near the current position; doing live fetch.",
                            self.settings,
                        )
                else:
                    reasons = []
                    if not source_matches:
                        reasons.append(f"cached source {sorted(sources)} does not match {source}")
                    if not location_matches:
                        reasons.append(
                            f"current location is {background_distance:.0f}m from background centre")
                    miab_log(
                        "verbose",
                        "p skipped in-memory background POIs: " + "; ".join(reasons) + ".",
                        self.settings,
                    )
            elif background:
                miab_log(
                    "verbose",
                    f"p skipped in-memory background POIs for source {source}.",
                    self.settings,
                )
            else:
                miab_log(
                    "verbose",
                    "p has no in-memory background POIs; doing live fetch.",
                    self.settings,
                )
            if name_filter or street_filter:
                miab_log(
                    "verbose",
                    f"p doing live {source.upper()} fetch for name='{name_filter}' street='{street_filter}'.",
                    self.settings,
                )

            def _live_cache_key(kind, src, cat, rad, extra=""):
                return (
                    kind, src, cat, int(rad),
                    round(self.lat, 2), round(self.lon, 2),
                    (extra or "").lower(),
                )

            def _live_cache_get(kind, src, cat, rad, extra=""):
                cache = getattr(self, "_poi_live_cache", {})
                key = _live_cache_key(kind, src, cat, rad, extra)
                entry = cache.get(key)
                if not isinstance(entry, dict):
                    return None
                if time.time() - entry.get("ts", 0) > 15 * 60:
                    cache.pop(key, None)
                    return None
                miab_log(
                    "verbose",
                    f"p using in-memory {src.upper()} {kind} cache radius={rad}m.",
                    self.settings,
                )
                return [dict(p) for p in entry.get("pois", [])]

            def _live_cache_set(kind, src, cat, rad, pois_to_cache, extra=""):
                cache = getattr(self, "_poi_live_cache", None)
                if cache is None:
                    self._poi_live_cache = {}
                    cache = self._poi_live_cache
                key = _live_cache_key(kind, src, cat, rad, extra)
                cache[key] = {
                    "ts": time.time(),
                    "pois": [dict(p) for p in (pois_to_cache or [])],
                }
                # Keep this intentionally small; it is just a session helper.
                if len(cache) > 24:
                    oldest = sorted(cache, key=lambda k: cache[k].get("ts", 0))[:6]
                    for old_key in oldest:
                        cache.pop(old_key, None)

            if not bypass_live_guard:
                fetch_actually_running = getattr(self, "_poi_live_fetch_in_progress", False)
                cooldown_remaining = POI_LIVE_COOLDOWN_SECS - (
                    time.time() - getattr(self, "_poi_live_last_completed_at", 0.0)
                )
                if fetch_actually_running or cooldown_remaining > 0:
                    self._queue_poi_live_search(
                        still_in_progress=fetch_actually_running,
                        category_key=category_key,
                        radius=radius,
                        timeout=timeout,
                        next_radius=next_radius,
                        name_filter=name_filter,
                        street_filter=street_filter,
                        source=source,
                    )
                    self._loading = False
                    self._poi_fetch_in_progress = False
                    return
            self._poi_live_fetch_started()
            live_started = True

            # A live fetch can take anywhere from a couple of seconds to
            # 20+ seconds (Overpass mirror retries, slow HERE/Google calls).
            # Without any feedback in that gap the app reads as hung. This
            # just speaks a periodic reassurance — it doesn't touch the
            # fetch, the sound, or anything else, and stays quiet if a
            # cached result was already shown (that case is a silent
            # "just in case" supplementary check the user isn't waiting on,
            # same reasoning as the alarm-sound guard just below).
            poi_wait_stop = threading.Event()
            if not cached_presented:
                def _poi_wait_ticker():
                    if poi_wait_stop.wait(4.0):
                        return
                    while not poi_wait_stop.wait(6.0):
                        wx.CallAfter(self._status_update, "Still working, please wait...", True)
                threading.Thread(target=_poi_wait_ticker, daemon=True).start()

            pois = []
            collected_name_pois = []
            collected_name_only_pois = []
            collected_seen: set[str] = set()
            collected_name_only_seen: set[str] = set()
            attempted_radius = radius

            def _collect(poi_list: list) -> None:
                for poi in poi_list:
                    name_matches = _name_match(poi)
                    if name_filter and street_filter and name_matches:
                        dedup_name = (poi.get("label", "").split(",")[0] or "").lower()
                        dedup = f"{dedup_name}|{poi.get('kind','')}|{round(poi['lat'],5)}|{round(poi['lon'],5)}"
                        if dedup not in collected_name_only_seen:
                            collected_name_only_seen.add(dedup)
                            collected_name_only_pois.append(poi)
                    if not name_matches or not _street_match(poi):
                        continue
                    dedup_name = (poi.get("label", "").split(",")[0] or "").lower()
                    dedup = f"{dedup_name}|{poi.get('kind','')}|{round(poi['lat'],5)}|{round(poi['lon'],5)}"
                    if dedup in collected_seen:
                        continue
                    collected_seen.add(dedup)
                    collected_name_pois.append(poi)

            for attempt_radius in search_radii:
                attempted_radius = attempt_radius
                if not name_filter and not street_filter:
                    miab_log(
                        "verbose",
                        f"p live fetch {category_key} radius={attempt_radius}m source={source}.",
                        self.settings,
                    )
                if source == "google" and google_key:
                    raw = self._fetch_google_pois(
                        category_key, radius=attempt_radius,
                        name_filter=name_filter)
                    if _discard_if_poi_context_changed("Google live"):
                        return
                    raw = filter_pois_by_category(raw, category_key)
                    pois = [p for p in raw if _name_match(p) and _street_match(p)]
                    pois.sort(key=lambda x: x.get("dist", float("inf")))
                    if name_filter or street_filter:
                        _collect(pois)
                elif source == "here" and here_key:
                    self._poi_fetcher.set_here_key(here_key)
                    cached_raw_pois = _live_cache_get("category", source, category_key, attempt_radius)
                    raw_pois = cached_raw_pois
                    if raw_pois is None:
                        live_raw_pois, _ = self._poi_fetcher.fetch_pois(
                            self.lat, self.lon,
                            category=category_key, radius=attempt_radius,
                            timeout=timeout,
                            address_points=getattr(self, "_address_points", []),
                        )
                        if live_raw_pois is not None:
                            raw_pois = live_raw_pois
                            _live_cache_set("category", source, category_key, attempt_radius, raw_pois)
                    if _discard_if_poi_context_changed("HERE live"):
                        return
                    pois = [p for p in raw_pois if _name_match(p) and _street_match(p)]
                    if name_filter or street_filter:
                        _collect(pois)
                else:
                    # OSM — temporarily clear HERE key so fetch_pois uses Overpass
                    self._poi_fetcher.set_here_key("")
                    cached_raw_pois = _live_cache_get("category", source, category_key, attempt_radius)
                    raw_pois = cached_raw_pois
                    if raw_pois is None:
                        # Skip the searching sound entirely when a result was
                        # already presented instantly from the in-memory
                        # background cache (cached_presented). In that case
                        # this live fetch is just a silent "just in case"
                        # supplementary check the user isn't actively waiting
                        # on — there's no real searching happening from their
                        # perspective, so playing the alarm here is exactly
                        # the "alarm with nothing to search for" bug reported.
                        if not search_beep_started and not cached_presented:
                            try:
                                self.sound.play_file(r"c:\windows\media\alarm09.wav", loops=-1)
                                search_beep_started = True
                            except Exception:
                                pass
                        # Before hitting Overpass at the exact radius, check
                        # whether the background disk cache already covers
                        # this request. This prevents repeated Overpass calls
                        # for requests within the loaded POI radius.
                        if not name_filter and attempt_radius <= POI_BACKGROUND_RADIUS_METRES:
                            disk_bg = self._poi_fetcher.load_cached_pois(self.lat, self.lon)
                            if disk_bg:
                                self._all_pois      = self._merge_personal_pois(disk_bg)
                                self._poi_fetch_lat = self.lat
                                self._poi_fetch_lon = self.lon
                                category_pois = filter_pois_by_category(disk_bg, category_key)
                                if category_pois:
                                    raw_pois = category_pois
                                    _live_cache_set("category", source, category_key, attempt_radius, raw_pois)
                                    miab_log("verbose",
                                             f"p reused background disk cache for {category_key} radius={attempt_radius}m.",
                                             self.settings)
                                else:
                                    # Disk cache has no entries for this category — do not
                                    # cache the empty result; fall through to live Overpass fetch.
                                    miab_log("verbose",
                                             f"p background disk cache has no {category_key} entries; will try live fetch.",
                                             self.settings)
                        if not raw_pois:
                            live_raw_pois, _ = self._poi_fetcher.fetch_pois(
                                self.lat, self.lon,
                                category=category_key, radius=attempt_radius,
                                timeout=timeout,
                                address_points=getattr(self, "_address_points", []),
                            )
                            if live_raw_pois is not None:
                                raw_pois = live_raw_pois
                                _live_cache_set("category", source, category_key, attempt_radius, raw_pois)
                    if _discard_if_poi_context_changed("OSM live"):
                        self._poi_fetcher.set_here_key(here_key)
                        return
                    self._poi_fetcher.set_here_key(here_key)
                    pois = [p for p in raw_pois if _name_match(p) and _street_match(p)]
                    if name_filter or street_filter:
                        _collect(pois)
                        # Category-specific searches get a category-aware
                        # live name query. Generic all-category searches only
                        # use the live name query when the radius is small.
                        if name_filter and (category_key != "all" or attempt_radius <= 1000):
                            raw_name_pois = _live_cache_get(
                                "name", source, category_key, attempt_radius, name_filter)
                            if raw_name_pois is None:
                                raw_name_pois = self._poi_fetcher.fetch_osm_name_search(
                                    self.lat, self.lon,
                                    name_filter=name_filter,
                                    category_key=category_key,
                                    radius=attempt_radius,
                                    timeout=timeout,
                                    address_points=getattr(self, "_address_points", []),
                                )
                                if raw_name_pois is not None:
                                    # Only cache real results; server failures (None)
                                    # are left uncached so the next P press retries.
                                    _live_cache_set(
                                        "name", source, category_key, attempt_radius,
                                        raw_name_pois, name_filter)
                                else:
                                    raw_name_pois = []
                            pois = [p for p in raw_name_pois if _name_match(p)]
                            _collect(pois)
                if not name_filter and pois:
                    break

            if name_filter or street_filter:
                pois = collected_name_pois
                if not pois and name_filter and street_filter and collected_name_only_pois:
                    pois = collected_name_only_pois
                    miab_log(
                        "verbose",
                        f"p found no live POIs matching name='{name_filter}' "
                        f"on street='{street_filter}'; showing name-only nearby matches.",
                        self.settings,
                    )

            if getattr(self, "_poi_explore_stack", []):
                self._loading = False
                return

            if not _poi_context_is_current():
                miab_log(
                    "verbose",
                    "p discarded live POI result because the browsing context changed.",
                    self.settings,
                )
                self._loading = False
                self._poi_fetch_in_progress = False
                return

            pois = _prepare_pois(pois)
            if not pois and cached_presented:
                miab_log(
                    "verbose",
                    "p live fetch returned no extra results; keeping in-memory background POI list.",
                    self.settings,
                )
                self._loading = False
                self._poi_fetch_in_progress = False
                return
            self._poi_list     = pois
            self._poi_index    = 0
            self._loading      = False
            self._poi_fetch_in_progress = False

            if self._poi_list:
                wx.CallAfter(self._present_poi_list)
            else:
                filters = []
                if name_filter:
                    filters.append(f"'{name_filter}'")
                if street_filter:
                    filters.append(f"on {street_filter}")
                what = " ".join(filters) if filters else category_label.lower()
                if name_filter or street_filter:
                    wx.CallAfter(
                        self._retry_poi_name_search,
                        category_key,
                        name_filter,
                        street_filter,
                        source,
                        attempted_radius,
                    )
                else:
                    wx.CallAfter(
                        self._announce_transient_then_return,
                        f"No {what} found within {format_distance(attempted_radius)}.")

        except Exception as e:
            miab_log("errors", f"POI fetch error: {e}", self.settings)
            if not getattr(self, "_street_data_fetch_in_progress", False):
                self._loading = False
            self._poi_fetch_in_progress = False
            self._poi_fetcher.set_here_key(here_key)
            if getattr(self, "_poi_explore_stack", []):
                return
            if 'cached_presented' in locals() and cached_presented:
                miab_log(
                    "verbose",
                    "p live fetch failed after cached results were shown; keeping in-memory background POI list.",
                    self.settings,
                )
                return
            wx.CallAfter(self._status_update,
                         f"Could not fetch {category_label.lower()} — server may be busy.",
                         True)
        finally:
            if live_started:
                if '_poi_context_is_current' in locals() and _poi_context_is_current():
                    self._poi_live_fetch_finished()
                else:
                    self._poi_live_fetch_in_progress = False
                    miab_log(
                        "verbose",
                        "p stale live POI fetch finished after context changed; no cooldown applied.",
                        self.settings,
                    )
                    if getattr(self, "_pending_poi_live_search", None):
                        self._schedule_pending_poi_live_search(
                            getattr(self, "_pending_poi_live_generation", 0)
                        )
            if 'poi_wait_stop' in locals():
                poi_wait_stop.set()
            if search_beep_started:
                try:
                    self.sound.stop()
                except Exception:
                    pass

    def _announce_postcode(self):
        """Announce the postcode for the current position.

        Uses the bundled offline dataset (GeoNames) by default, or a live
        Nominatim reverse-geocode if the user has set "Postcode lookup" to
        "Search Online" in Settings. See postal_codes.py for the offline
        lookup's accuracy notes (nearest-point match, not a boundary
        lookup, and outward-code-only for CA/GB/NL)."""
        source = self.settings.get("postcode_lookup", "included")
        lat, lon = self.lat, self.lon

        if source != "online":
            result = self._postal_codes.lookup(lat, lon)
            if result:
                postcode, place, admin1 = result
                where = ", ".join(p for p in (place, admin1) if p)
                msg = f"Postcode: {postcode}" + (f" ({where})" if where else "") + "."
                self._status_update(msg, force=True)
            else:
                self._announce_transient_then_return(
                    "No postcode found in the included data for this location.")
            return

        self._status_update("Looking up postcode...")
        def _fetch():
            try:
                postcode = None
                for zoom in (18, 14, 10):
                    url = (f"https://nominatim.openstreetmap.org/reverse"
                           f"?lat={lat}&lon={lon}&format=json&zoom={zoom}&addressdetails=1")
                    req = urllib.request.Request(url, headers={"User-Agent": "MapInABox/1.0"})
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        data = json.loads(resp.read().decode())
                    postcode = data.get("address", {}).get("postcode")
                    if postcode:
                        break
                if postcode:
                    wx.CallAfter(self._status_update, f"Postcode: {postcode}.", True)
                else:
                    wx.CallAfter(
                        self._announce_transient_then_return,
                        "No postcode found for this location.")
            except Exception:
                wx.CallAfter(self._status_update,
                             f"Could not fetch postcode. {NETWORK_UNAVAILABLE_MESSAGE}",
                             True)
        threading.Thread(target=_fetch, daemon=True).start()

    def _poi_detail(self, key_num: int):
        poi = None
        focused = wx.Window.FindFocus()
        list_active = (
            getattr(self, '_poi_list', [])
            and (focused == self.listbox or focused == self)
        )
        if list_active and 0 <= getattr(self, '_poi_index', -1) < len(self._poi_list):
            poi = self._poi_list[self._poi_index]
        elif getattr(self, '_poi_explore_stack', []):
            stack = self._poi_explore_stack[-1]
            items = stack.get('items', [])
            idx   = stack.get('index', 0)
            if items and 0 <= idx < len(items):
                poi = items[idx]
        if poi is None:
            poi = self._current_street_survey_poi()

        if poi is None:
            self._poi_detail_announce("No POI selected."); return

        name = (poi.get('name') or poi.get('label', '')).split(',')[0].strip()

        # A POI's bundled data is often a locality-only stub (e.g. its "address"
        # is just "Greenslopes QLD", no street number), which would pass a
        # naive "is the field present?" check and wrongly skip the lookup. So on
        # the FIRST detail-key press for a POI we enrich it from HERE — the same
        # lookup Ctrl+Alt+2 does — then answer from the enriched data. The result
        # is cached, so repeat presses are instant.
        here_key = self.settings.get("here_api_key", "").strip()
        needs_detail = (
            key_num in (1, 2, 3, 4)
            and here_key
            and not poi.get("_here_detail_fetched")
        )
        miab_log("feature_usage", f"[POIDetail] key={key_num} name={name!r} needs_detail={needs_detail} "
              f"already_fetched={bool(poi.get('_here_detail_fetched'))} "
              f"have_here_key={bool(here_key)}", getattr(self, "settings", None))

        if needs_detail:
            self._poi_detail_announce(f"Looking up {name}...")
            def _fetch_and_dispatch():
                try:
                    detail = self._here.fetch_poi_detail(
                        name, poi.get('lat', self.lat), poi.get('lon', self.lon))
                    if detail:
                        poi.update(detail)
                except Exception as exc:
                    miab_log("errors", f"[POIDetail] HERE lookup failed: {exc}", None)
                poi["_here_detail_fetched"] = True
                wx.CallAfter(self._poi_detail_dispatch, key_num, poi, name)
            threading.Thread(target=_fetch_and_dispatch, daemon=True).start()
            return

        self._poi_detail_dispatch(key_num, poi, name)

    def _google_reviews_available(self) -> bool:
        """Whether the shared Google review lookup is available."""
        serper = getattr(self, "_serper", None)
        return bool(serper and getattr(serper, "is_configured", False))

    def _lookup_google_review_info(self, name: str, suburb: str = "") -> dict:
        """Shared Google place-rating lookup used by POIs and hotel reviews."""
        if not self._google_reviews_available():
            return {}
        try:
            info = self._serper.place_info(name, suburb)
        except Exception as exc:
            miab_log("errors", f"[Reviews] place lookup failed: {exc}", getattr(self, "settings", None))
            return {}
        return info if isinstance(info, dict) else {}

    def _google_review_summary(self, name: str, info: dict,
                               include_name: bool = True) -> str:
        rating = self._format_google_rating(self._google_info_value(
            info,
            "rating", "googleRating", "ratingValue", "averageRating",
            "aggregateRating", "stars",
        ))
        count = self._format_google_review_count(self._google_info_value(
            info,
            "ratingCount", "reviewCount", "reviewsCount",
            "userRatingCount", "totalReviewCount", "review_count", "reviews",
        ))
        if not rating:
            return ""
        count_str = f" from {count} reviews" if count else ""
        if include_name:
            return f"{name}: rated {rating} stars{count_str} on Google."
        return f"Google: rated {rating} stars{count_str}."

    @staticmethod
    def _google_info_value(info: dict, *keys):
        if not isinstance(info, dict):
            return None
        for key in keys:
            value = info.get(key)
            if value not in (None, ""):
                return value
        lower = {str(k).lower(): v for k, v in info.items()}
        for key in keys:
            value = lower.get(str(key).lower())
            if value not in (None, ""):
                return value
        return None

    @classmethod
    def _format_google_rating(cls, value) -> str:
        if isinstance(value, dict):
            value = cls._google_info_value(
                value, "rating", "value", "ratingValue", "averageRating")
        if value in (None, ""):
            return ""
        try:
            return f"{float(value):g}"
        except Exception:
            pass
        import re as _re
        m = _re.search(r"\d+(?:\.\d+)?", str(value))
        return f"{float(m.group(0)):g}" if m else ""

    @classmethod
    def _format_google_review_count(cls, value) -> str:
        if isinstance(value, dict):
            value = cls._google_info_value(
                value, "count", "total", "value", "ratingCount", "reviewCount")
        if isinstance(value, (list, tuple)):
            value = len(value)
        if value in (None, ""):
            return ""
        try:
            return f"{int(float(str(value).replace(',', '').strip())):,}"
        except Exception:
            pass
        import re as _re
        m = _re.search(r"\d[\d,]*", str(value))
        return f"{int(m.group(0).replace(',', '')):,}" if m else ""

    def _open_place_reviews(self, name: str, suburb: str = "") -> None:
        """Look up the venue's Google rating via the proxy (keyless), announce it,
        then offer to open the full reviews. Falls back to a keyless reviews
        search if the rating/place can't be resolved."""
        self._poi_detail_announce(f"Looking up reviews for {name}...")

        def _worker():
            info = self._lookup_google_review_info(name, suburb)
            wx.CallAfter(self._present_reviews, name, suburb, info)

        import threading
        threading.Thread(target=_worker, daemon=True).start()

    def _present_reviews(self, name: str, suburb: str, info: dict) -> None:
        import webbrowser
        import urllib.parse
        cid = (info.get("cid") or "").strip()

        summary = self._google_review_summary(name, info, include_name=True)

        # With a CID we can open the exact place's reviews in Maps — ask first,
        # putting the rating in the prompt so the screen reader reads it.
        if cid:
            msg = (summary + "\n\nShow Google reviews?") if summary else "Show Google reviews?"
            dlg = wx.MessageDialog(self, msg, "Reviews", wx.YES_NO | wx.ICON_QUESTION)
            answer = dlg.ShowModal()
            dlg.Destroy()
            if answer == wx.ID_YES:
                webbrowser.open("https://www.google.com/maps?cid=" + urllib.parse.quote(cid))
            else:
                self.listbox.SetFocus()
            return

        # No place/CID from the proxy — keyless reviews search fallback.
        if summary:
            self._poi_detail_announce(summary)
        query = " ".join(p for p in (name, suburb, "reviews") if p)
        try:
            webbrowser.open("https://www.google.com/search?q=" + urllib.parse.quote_plus(query))
        except Exception as exc:
            miab_log("errors", f"[Reviews] open failed for {name}: {exc}", getattr(self, "settings", None))
            self._poi_detail_announce(f"Could not open the browser for {name}.")

    def _poi_detail_announce(self, text: str) -> None:
        """Announce POI detail via ao2 speech and braille."""
        self._emit_speech(text)

    def _poi_announce_website(self, poi: dict, name: str) -> None:
        """Ctrl+Alt+4 — announce the POI's website, validated. If the listed
        site is dead, substitute the real homepage found via the search proxy.
        Result is cached on the POI so repeat presses are instant."""
        self._announce_verified_website_for(
            poi,
            name=name,
            announce_cb=self._poi_detail_announce,
        )

    def _poi_detail_dispatch(self, key_num: int, poi: dict, name: str):
        import time as _time

        if key_num == 1:
            text = (poi.get('address') or poi.get('addr') or '').strip()
            if not text:
                parts = [name]
                suburb = getattr(self, '_current_suburb', '')
                if suburb:
                    parts.append(suburb)
                text = ', '.join(p for p in parts if p)
            text = text or "No address available."
        elif key_num == 2:
            tags = poi.get('tags') or {}
            text = (poi.get('opening_hours') or tags.get('opening_hours') or '').strip() or "Opening hours not available."
        elif key_num == 3:
            tags = poi.get('tags') or {}
            text = (poi.get('phone') or tags.get('phone') or tags.get('contact:phone') or '').strip() or "No phone number available."
        elif key_num == 4:
            self._poi_announce_website(poi, name)
            return
        elif key_num == 5:
            suburb = getattr(self, '_current_suburb', '')
            self._open_place_reviews(name, suburb)
            return
        elif key_num == 6:
            self._lookup_menu_links_for_poi(poi, name)
            return
        else:
            return

        now = _time.monotonic()
        double = (key_num == self._poi_detail_last_key
                  and (now - self._poi_detail_last_time) < 0.6)
        self._poi_detail_last_key  = key_num
        self._poi_detail_last_time = now

        if double:
            self._show_detail_reader(text)
        else:
            self._poi_detail_announce(text)



    def _show_detail_reader(self, text: str):
        dlg = wx.Dialog(self, title="Detail", style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        sizer = wx.BoxSizer(wx.VERTICAL)
        ctrl = wx.TextCtrl(dlg, value=text,
                           style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_AUTO_URL)
        ctrl.SetMinSize((420, 120))
        sizer.Add(ctrl, 1, wx.EXPAND | wx.ALL, 8)
        btn = wx.Button(dlg, wx.ID_CLOSE, "Close (Escape)")
        sizer.Add(btn, 0, wx.ALIGN_RIGHT | wx.RIGHT | wx.BOTTOM, 8)
        dlg.SetSizer(sizer)
        dlg.Fit()

        def _close(evt=None):
            self._suppress_map_focus_repeat(800)
            dlg.EndModal(wx.ID_CLOSE)
            self.listbox.SetFocus()

        btn.Bind(wx.EVT_BUTTON, _close)
        dlg.Bind(
            wx.EVT_CHAR_HOOK,
            lambda e: _close()
            if e.GetKeyCode() == wx.WXK_ESCAPE else e.Skip(),
        )
        ctrl.SetFocus()
        ctrl.SelectAll()
        dlg.ShowModal()
        dlg.Destroy()

    def _lookup_menu_links_for_poi(self, poi: dict, name: str) -> None:
        if not is_menu_eligible_poi(poi):
            kind = (poi.get("kind") or "").strip()
            self._poi_detail_announce(
                f"Menu lookup is only available for food venues. {kind.title()} is not one."
                if kind else "Menu lookup is only available for food venues."
            )
            return

        suburb = self._menu_lookup_suburb(poi)

        # In-app menu results via the search proxy; fall back to the keyless
        # browser handoff when nothing usable comes back.
        if self._serper.is_configured:
            self._poi_detail_announce(f"Searching for {name} menu...")

            def _worker():
                results = []
                try:
                    distinctive, compact = self._venue_name_tokens(name)
                    raw = []
                    for query in self._menu_search_queries(name, suburb):
                        raw.extend(self._serper.search(query, num=10))
                    raw = self._merge_menu_results(raw)
                    results = self._rank_menu_results(raw, distinctive, compact)

                    # Recall: if the search surfaced no menu page on the venue's
                    # own site, probe that site's common menu paths directly.
                    if not any(self._is_own_menu(r, distinctive, compact) for r in results):
                        domains = self._venue_domains(poi, raw, distinctive, compact)
                        if domains:
                            wx.CallAfter(self._poi_detail_announce,
                                         f"Checking {name}'s website for a menu...")
                        for domain in domains[:2]:
                            hits = self._probe_menu_paths(domain, name)
                            if hits:
                                results = self._rank_menu_results(
                                    self._merge_menu_results(results + hits),
                                    distinctive, compact)
                                break
                except Exception as exc:
                    miab_log("errors", f"[Menu] search failed: {exc}", None)
                if results:
                    wx.CallAfter(self._show_menu_links_dialog, name, results)
                else:
                    # Nothing usable (or proxy down) — keyless browser fallback.
                    wx.CallAfter(self._open_place_menu_search, name, suburb)

            import threading
            threading.Thread(target=_worker, daemon=True).start()
            return

        self._open_place_menu_search(name, suburb)

    def _menu_lookup_suburb(self, poi: dict) -> str:
        """Pick the most specific suburb/city hint we already know for a POI."""
        tags = poi.get("tags") or {}
        for key in ("addr:suburb", "addr:city", "addr:town", "addr:village"):
            value = (tags.get(key) or "").strip()
            if value:
                return value

        for key in ("suburb", "city", "town", "village"):
            value = (poi.get(key) or "").strip()
            if value:
                return value

        return (getattr(self, "_current_suburb", "") or "").strip()

    def _menu_search_queries(self, name: str, suburb: str = "") -> list[str]:
        """Build a single, location-anchored query: "Name" suburb menu country.

        Suburb and country are soft (unquoted) terms — they bias results to the
        right place without hard-excluding the venue's own pages."""
        name = (name or "").strip()
        suburb = (suburb or "").strip()
        if not name:
            return []
        country = (getattr(self, "last_country_found", "") or "").strip()
        parts = [f'"{name}"']
        if suburb:
            parts.append(suburb)
        parts.append("menu")
        if country:
            parts.append(country)
        return [" ".join(parts)]

    # Food-delivery platforms — kept and surfaced first.
    _DELIVERY_HOSTS = (
        "ubereats.com", "doordash.com", "menulog.com.au", "menulog.com",
        "deliveroo.com.au", "grubhub.com",
    )
    # Strong menu/ordering signals in a URL path or title. Deliberately tight —
    # loose words like "food"/"eat"/"dining" matched guides and listicles.
    _MENU_SIGNALS = (
        "menu", "menus", "order", "order-online", "ordering",
        "takeaway", "take-away", "click-and-collect",
    )
    # Common menu paths to probe directly on a venue's own domain.
    _MENU_PROBE_PATHS = (
        "/menu", "/menus", "/our-menu", "/food-menu", "/drinks-menu",
        "/menu/", "/menus/", "/order", "/order-online", "/menu.pdf",
    )
    # Generic words ignored when matching a result's domain to the venue name.
    _NAME_STOPWORDS = frozenset({
        "the", "a", "an", "of", "and", "on", "at", "by", "in", "co",
        "cafe", "café", "bar", "grill", "kitchen", "eatery", "house",
        "restaurant", "bistro", "diner", "pizzeria", "takeaway",
    })
    # Multi-part public suffixes, to find the registrable domain label.
    _MULTI_SUFFIX = (
        "com.au", "net.au", "org.au", "co.uk", "org.uk", "co.nz",
        "com.sg", "co.za", "com.my",
    )
    # Locale codes treated as "not local" for AU-targeted delivery store pages.
    _FOREIGN_CC = frozenset({
        "us", "gb", "uk", "ca", "ie", "nz", "fr", "de", "es", "it", "nl",
        "jp", "in", "sg", "za", "mx", "br",
    })

    def _merge_menu_results(self, results: list) -> list:
        """Deduplicate menu hits while preserving first-seen order."""
        merged = []
        seen = set()
        for item in results:
            if not isinstance(item, dict):
                continue
            url = (item.get("url") or "").strip()
            if not url:
                continue
            parsed = urllib.parse.urlsplit(url)
            key = f"{parsed.netloc.lower()}|{parsed.path.lower().rstrip('/')}"
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(item)
        return merged

    @staticmethod
    def _result_host(url: str) -> str:
        host = urllib.parse.urlsplit((url or "").lower()).netloc
        return host[4:] if host.startswith("www.") else host

    def _delivery_wrong_country(self, host: str, path: str) -> bool:
        """Best-effort: drop delivery store pages clearly in another country
        (AU-targeted). menulog.com.au is always local."""
        if host.endswith("menulog.com.au"):
            return False
        segs = [s for s in (path or "").split("/") if s]
        if not segs:
            return False
        first = segs[0].lower()
        cc = first.rsplit("-", 1)[-1] if "-" in first else first
        return cc in self._FOREIGN_CC

    def _venue_name_tokens(self, name: str) -> "tuple[list, str]":
        """Return (distinctive tokens, compact name) for matching a domain to
        the venue. 'In a Pickle' -> (['pickle'], 'inapickle')."""
        import re as _re
        toks = [t for t in _re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).split() if t]
        distinctive = [t for t in toks if t not in self._NAME_STOPWORDS and len(t) >= 4]
        return distinctive, "".join(toks)

    def _main_label(self, host: str) -> str:
        """The registrable domain label, ignoring subdomains and public suffix.
        'in-a-pickle.wheree.com' -> 'wheree'; 'inapickle.com.au' -> 'inapickle'."""
        h = (host or "").lower().strip(".")
        if h.startswith("www."):
            h = h[4:]
        for suf in self._MULTI_SUFFIX:
            if h.endswith("." + suf):
                base = h[: -(len(suf) + 1)]
                return base.split(".")[-1] if base else ""
        parts = h.split(".")
        return parts[-2] if len(parts) >= 2 else h

    def _label_matches_venue(self, label: str, distinctive: list, compact: str) -> bool:
        """True if a domain's main label plausibly belongs to the venue."""
        label = (label or "").lower()
        if not label:
            return False
        if compact and len(compact) >= 4 and compact in label:
            return True
        return any(t in label for t in distinctive)

    def _classify_menu_result(self, item, distinctive: list, compact: str) -> "int | None":
        """0 = delivery page, 1 = the venue's own menu page, None = drop.

        Allow-list, not deny-list: a non-delivery result is kept only when it
        lives on the venue's own domain AND looks like a menu page. Guides,
        directories, review sites, social and wrong-venue results all fail the
        domain match and are dropped — no host blocklist to maintain."""
        if not isinstance(item, dict):
            return None
        url = (item.get("url") or "").strip()
        if not url:
            return None
        parsed = urllib.parse.urlsplit(url.lower())
        host = self._result_host(url)
        if any(host == h or host.endswith("." + h) for h in self._DELIVERY_HOSTS):
            return None if self._delivery_wrong_country(host, parsed.path) else 0
        if self._label_matches_venue(self._main_label(host), distinctive, compact):
            hay = parsed.path + " " + (item.get("title") or "").lower()
            if any(sig in hay for sig in self._MENU_SIGNALS):
                return 1
        return None

    def _is_own_menu(self, item, distinctive: list, compact: str) -> bool:
        return self._classify_menu_result(item, distinctive, compact) == 1

    def _rank_menu_results(self, results: list, distinctive: list, compact: str) -> list:
        """Keep only delivery pages and the venue's own menu pages; delivery
        first, original order within each group. Everything else is dropped."""
        scored = []
        for idx, item in enumerate(results):
            pr = self._classify_menu_result(item, distinctive, compact)
            if pr is None:
                continue
            scored.append((pr, idx, item))
        scored.sort(key=lambda t: (t[0], t[1]))
        out = [item for _, _, item in scored]
        delivery = sum(1 for pr, _, _ in scored if pr == 0)
        miab_log("feature_usage", f"[Menu] kept {len(out)} of {len(results)} result(s) ({delivery} delivery)", getattr(self, "settings", None))
        return out

    def _venue_domains(self, poi: dict, raw: list, distinctive: list, compact: str) -> list:
        """Candidate own-domain hosts to probe: the POI's website tag first,
        then any non-delivery result host whose domain matches the venue name."""
        out, seen = [], set()
        tags = poi.get("tags") or {}
        site = (poi.get("website") or tags.get("website")
                or tags.get("contact:website") or "").strip()
        if site:
            h = self._result_host(site)
            if h:
                seen.add(h)
                out.append(h)
        for item in raw:
            h = self._result_host((item.get("url") if isinstance(item, dict) else "") or "")
            if not h or h in seen:
                continue
            if any(h == d or h.endswith("." + d) for d in self._DELIVERY_HOSTS):
                continue
            if self._label_matches_venue(self._main_label(h), distinctive, compact):
                seen.add(h)
                out.append(h)
        return out

    def _probe_menu_paths(self, domain: str, name: str) -> list:
        """Find the venue's menu on its own domain. First tries common menu
        paths; if none resolve, harvests menu links from the homepage. Reliable
        (own sites aren't bot-protected) and free (no search call)."""
        host = self._result_host(domain) or (domain or "").strip()
        if not host:
            return []
        base = "https://" + host
        found = []
        for path in self._MENU_PROBE_PATHS:
            ok, final, title = self._probe_url(base + path)
            if ok and final.lower().rstrip("/") not in {f["url"].lower().rstrip("/") for f in found}:
                found.append({"title": title or f"{name} menu", "url": final, "snippet": ""})
                if len(found) >= 3:
                    break
        if not found:
            # The menu may sit on a non-standard path — follow the homepage's
            # own "Menu" links instead of guessing.
            found = self._harvest_menu_from_home(base, name)
        if found:
            miab_log("feature_usage", f"[Menu] probe found {len(found)} menu page(s) on {host}", getattr(self, "settings", None))
        return found

    def _harvest_menu_from_home(self, base: str, name: str) -> list:
        """Fetch the venue homepage and follow its own menu links, kept to the
        same site so no off-domain junk slips in."""
        import re as _re
        import urllib.request
        try:
            req = urllib.request.Request(base, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                home = resp.geturl()
                html = resp.read(400000).decode("utf-8", "ignore")
        except Exception:
            return []
        home_label = self._main_label(self._result_host(home))
        candidates, seen = [], set()
        for m in _re.finditer(
            r'<a\b[^>]*href\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</a>',
            html, _re.I | _re.S,
        ):
            href, text = m.group(1), _re.sub(r"<[^>]+>", " ", m.group(2))
            if not any(k in (href + " " + text).lower()
                       for k in ("menu", "order online", "order now", "takeaway")):
                continue
            url = urllib.parse.urljoin(home, href)
            if not url.lower().startswith(("http://", "https://")):
                continue
            if self._main_label(self._result_host(url)) != home_label:
                continue  # stay on the venue's own site
            key = url.lower().rstrip("/")
            if key in seen:
                continue
            seen.add(key)
            candidates.append(url)
        found = []
        for url in candidates[:8]:
            # The homepage vouched for it as a menu link, so don't require the
            # path itself to say "menu"; just reject a bounce back to home.
            ok, final, title = self._probe_url(url, home_url=home, require_menu_path=False)
            if ok and final.lower().rstrip("/") not in {f["url"].lower().rstrip("/") for f in found}:
                found.append({"title": title or f"{name} menu", "url": final, "snippet": ""})
                if len(found) >= 3:
                    break
        return found

    def _probe_url(self, url: str, home_url: str = "", require_menu_path: bool = True) -> tuple:
        """GET a candidate menu URL. Returns (ok, final_url, page_title).
        Rejects a bounce back to the homepage; when require_menu_path is set,
        also rejects pages whose final path doesn't look menu-like."""
        import re as _re
        import urllib.request
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                final = resp.geturl()
                ctype = resp.headers.get("Content-Type", "")
                raw = resp.read(200000)
        except Exception:
            return False, "", ""
        if home_url and final.lower().rstrip("/") == home_url.lower().rstrip("/"):
            return False, "", ""  # bounced to the homepage
        final_path = urllib.parse.urlsplit(final).path.lower()
        is_pdf = "pdf" in ctype.lower() or final_path.endswith(".pdf")
        if require_menu_path and not is_pdf and not any(
            sig in final_path for sig in ("menu", "order", "takeaway")
        ):
            return False, "", ""  # redirected away from a menu page
        title = ""
        if not is_pdf:
            try:
                m = _re.search(r"<title[^>]*>(.*?)</title>",
                               raw.decode("utf-8", "ignore"), _re.S | _re.I)
                if m:
                    title = _re.sub(r"\s+", " ", m.group(1)).strip()[:120]
            except Exception:
                pass
        return True, final, title

    def _show_menu_links_dialog(self, restaurant_name: str, results: list):
        import webbrowser
        dlg = wx.Dialog(self, title=f"Menu Results: {restaurant_name}",
                        style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
                        size=(700, 500))
        sizer = wx.BoxSizer(wx.VERTICAL)
        title = wx.StaticText(dlg, label=f"Menu results for {restaurant_name}: {len(results)} found")
        title_font = title.GetFont()
        title_font.MakeBold()
        title.SetFont(title_font)
        sizer.Add(title, 0, wx.ALL | wx.EXPAND, 10)

        scroll = wx.ScrolledWindow(dlg)
        link_sizer = wx.BoxSizer(wx.VERTICAL)
        for i, item in enumerate(results, 1):
            if isinstance(item, dict):
                label = (item.get("title") or item.get("url") or "").strip()
                url   = (item.get("url") or "").strip()
            else:
                label = url = str(item)
            btn = wx.Button(scroll, label=f"{i}. {label[:90]}{'...' if len(label) > 90 else ''}")
            btn.SetToolTip(url)
            btn.Bind(wx.EVT_BUTTON, lambda e, u=url: webbrowser.open(u))
            link_sizer.Add(btn, 0, wx.ALL | wx.EXPAND, 5)
        scroll.SetSizer(link_sizer)
        scroll.SetScrollRate(5, 5)
        sizer.Add(scroll, 1, wx.EXPAND | wx.ALL, 8)

        btn_close = wx.Button(dlg, wx.ID_CLOSE, "Close (Escape)")
        sizer.Add(btn_close, 0, wx.ALIGN_RIGHT | wx.RIGHT | wx.BOTTOM, 8)
        dlg.SetSizer(sizer)
        btn_close.Bind(wx.EVT_BUTTON, lambda e: dlg.EndModal(wx.ID_CLOSE))
        dlg.Bind(wx.EVT_CHAR_HOOK,
                 lambda e: (self._suppress_map_focus_repeat(800), dlg.EndModal(wx.ID_CLOSE))[1]
                 if e.GetKeyCode() == wx.WXK_ESCAPE else e.Skip())
        dlg.ShowModal()
        dlg.Destroy()

    def _open_place_menu_search(self, name: str, suburb: str = "") -> None:
        """Open a Google search for the venue's menu in the browser — keyless.

        No key, no API, no setup: Google renders the results and the user reads
        them with their screen reader, the same handoff as POI reviews.  Works
        for every user.
        """
        import webbrowser
        import urllib.parse
        country = (getattr(self, "last_country_found", "") or "").strip()
        query = " ".join(p for p in (name, suburb, "menu", country) if p)
        url = "https://www.google.com/search?q=" + urllib.parse.quote_plus(query)
        self._poi_detail_announce(f"Searching Google for {name} menu in your browser...")
        try:
            webbrowser.open(url)
        except Exception as exc:
            miab_log("errors", f"[Menu] open failed for {name}: {exc}", getattr(self, "settings", None))
            self._poi_detail_announce(f"Could not open the browser for {name}.")

    def _announce_address(self):
        """A key — non-blocking address lookup."""
        # Immediate feedback - don't block UI
        self._suppress_status_until = 0
        self._address_lookup_in_progress = True
        self._status_update("Looking up address...")
        
        # Do all lookups in background thread
        def _background_lookup():
            try:
                pinned_num = getattr(self, '_jump_address_number', None)
                pinned_street = getattr(self, '_jump_address_street', None)
                pin_lat = getattr(self, '_jump_street_pin_lat', None)
                pin_lon = getattr(self, '_jump_street_pin_lon', None)
                if (pinned_num and pinned_street and pin_lat is not None and pin_lon is not None
                        and dist_metres(self.lat, self.lon, pin_lat, pin_lon) <= 150.0):
                    suburb = getattr(self, "_current_suburb", "") or ""
                    wx.CallAfter(
                        self._status_update,
                        f"{pinned_num} {pinned_street}" + (f", {suburb}" if suburb else "")
                    )
                    return

                if getattr(self, '_walking_mode', False):
                    street = getattr(self, '_walk_street', '') or ''
                    if street:
                        num = self._nearest_address_number(self.lat, self.lon, street, radius=200)
                        if num:
                            suburb = getattr(self, "_current_suburb", "") or ""
                            wx.CallAfter(self._status_update, f"{num} {street}" + (f", {suburb}" if suburb else ""))
                            return
                    # Fall through to Nominatim
                    self._fetch_address()
                    return

                if getattr(self, '_free_mode', False):
                    street = self._free_engine.street_name or ""
                    if street:
                        num = self._nearest_address_number(self.lat, self.lon, street, radius=200)
                        suburb = getattr(self, "_current_suburb", "") or ""
                        if num:
                            wx.CallAfter(self._status_update, f"{num} {street}" + (f", {suburb}" if suburb else ""))
                        else:
                            wx.CallAfter(self._status_update, street + (f", {suburb}" if suburb else ""))
                        return

                if getattr(self, "street_mode", False):
                    street = ""
                    if hasattr(self, "_street_survey_current_street"):
                        street = self._street_survey_current_street()
                    street = street or getattr(self, "street_label", "") or ""
                    if street and street not in ("Unknown", "No street data", "No street data nearby"):
                        num = self._nearest_address_number(self.lat, self.lon, street, radius=500)
                        suburb = getattr(self, "_current_suburb", "") or ""
                        addr_str = f"{num} {street}" if num else street
                        if suburb:
                            addr_str += f", {suburb}"
                        wx.CallAfter(self._status_update, addr_str)
                        return

                label, cross = self._nearest_road(self.lat, self.lon)

                # No street data nearby — check natural features first (same
                # logic as _update_street_display) before falling back to the
                # stale street_label which may be from a different suburb.
                if not label or label in ("Unknown", "", "No street data", "No street data nearby"):
                    nf = self._check_natural_feature(self.lat, self.lon)
                    if nf:
                        name = nf.get("name")
                        desc = nf.get("description", "open area")
                        suburb = getattr(self, "_current_suburb", "") or ""
                        msg = (name if name else desc) + (f", {suburb}" if suburb else "")
                        wx.CallAfter(self._status_update, msg)
                        return
                    # No natural feature — only use street_label if still within
                    # 500m of the cache centre to avoid stale addresses from a
                    # different suburb being announced.
                    import math as _math
                    fetch_lat = getattr(self, '_road_fetch_lat', None)
                    fetch_lon = getattr(self, '_road_fetch_lon', None)
                    if fetch_lat is not None:
                        dlat = (self.lat - fetch_lat) * 111000
                        dlon = (self.lon - fetch_lon) * 111000 * _math.cos(_math.radians(self.lat))
                        dist = _math.sqrt(dlat**2 + dlon**2)
                    else:
                        dist = float('inf')
                    suburb = getattr(self, "_current_suburb", "") or ""
                    if dist < 500 and self.street_label and \
                            self.street_label not in ("", "Unknown", "No street data nearby"):
                        street = self.street_label
                        num = self._nearest_address_number(self.lat, self.lon, street, radius=500)
                        addr_str = f"{num} {street}" if num else street
                        if suburb:
                            addr_str += f", {suburb}"
                        wx.CallAfter(self._status_update, addr_str)
                    else:
                        wx.CallAfter(self._status_update, "Off network" + (f", {suburb}" if suburb else ""))
                    return
                    
                # Found nearby street - use ONLY cached data
                street = label.split("(")[0].strip()
                suburb = getattr(self, "_current_suburb", "") or ""
                
                # Cache-only lookup - no web fallbacks
                num = self._nearest_address_number(self.lat, self.lon, street, radius=500)
                
                boundary_dist, neighbor = None, None
                
                # Build address string
                if num:
                    addr_str = f"{num} {street}"
                else:
                    addr_str = f"{street}" + (f", near {cross}" if cross else "")
                
                if suburb:
                    addr_str += f", {suburb}"
                
                # Add boundary info only when no house number
                if not num and boundary_dist and neighbor:
                    addr_str += f", {boundary_dist}m from {neighbor}"
                
                wx.CallAfter(self._status_update, addr_str)
                
            except Exception as e:
                # Always announce SOMETHING, even on total failure
                miab_log("errors", f"[Address Lookup] Error: {e}", None)
                street = getattr(self, 'street_label', '') or 'Unknown location'
                suburb = getattr(self, "_current_suburb", "") or ""
                wx.CallAfter(self._status_update, f"{street}" + (f", {suburb}" if suburb else ""))
            finally:
                self._address_lookup_in_progress = False
        
        # Spawn background thread - don't block UI
        threading.Thread(target=_background_lookup, daemon=True).start()

    def _fetch_address(self):
        """Nominatim reverse geocode fallback."""
        try:
            url = (f"https://nominatim.openstreetmap.org/reverse"
                   f"?lat={self.lat}&lon={self.lon}&format=json&zoom=18&addressdetails=1")
            req = urllib.request.Request(url, headers={"User-Agent": "MapInABox/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            addr = data.get("address", {})
            parts = []
            for field in ("house_number", "road", "suburb", "city",
                          "state", "postcode"):
                val = addr.get(field)
                if val and val not in parts:
                    parts.append(val)
            label = ", ".join(parts) if parts else data.get("display_name", "No address found")
            wx.CallAfter(self._status_update, label)
        except Exception as e:
            wx.CallAfter(self._status_update, "Could not fetch address.  Server may be busy.")

    def _show_poi_category_dialog(
            self, initial_key="all", initial_name="", initial_street="",
            initial_source=None, notice=""):
        sources = ["osm"]
        if self.settings.get("here_api_key", "").strip():
            sources.append("here")
        if self.settings.get("google_api_key", "").strip():
            sources.append("google")
        preferred = initial_source or self.settings.get("poi_source", "osm")
        initial_radius = int(self.settings.get("poi_browse_radius_km", 1) or 1) * 1000
        dlg = POICategoryDialog(
            self,
            available_sources=sources,
            preferred_source=preferred,
            initial_key=initial_key,
            initial_name=initial_name,
            initial_street=initial_street,
            initial_radius=initial_radius,
            notice=notice,
        )
        # Suppress background location announcements while the dialog is open
        # and until the search announcement has been delivered.
        self._suppress_location_restore = True
        try:
            if dlg.ShowModal() == wx.ID_OK and dlg.selected_key:
                category_map = dict(POI_CATEGORY_CHOICES)
                label  = category_map.get(dlg.selected_key, "All nearby")
                name   = dlg.selected_name
                street = dlg.selected_street
                source = dlg.selected_source
                self.settings["poi_browse_radius_km"] = max(1, int(round(dlg.selected_radius / 1000.0)))
                filters = []
                if name:
                    filters.append(f"'{name}'")
                if street:
                    filters.append(f"on {street}")
                msg = (f"Searching {label.lower()} {' '.join(filters)} via {source.upper()}..."
                       if filters else f"Searching {label.lower()} via {source.upper()}...")
                def _announce_search(m=msg):
                    self._suppress_location_restore = False
                    self.update_ui(m, force=True)
                wx.CallAfter(_announce_search)
                threading.Thread(
                    target=self._fetch_pois,
                    args=(dlg.selected_key,),
                    kwargs={"name_filter": name, "source": source,
                            "street_filter": street,
                            "radius": dlg.selected_radius},
                    daemon=True,
                ).start()
            else:
                # Cancelled — release suppression immediately so location
                # updates aren't silenced after the dialog closes.
                self._suppress_location_restore = False
        finally:
            dlg.Destroy()

    def _announce_poi_count(self):
        wx.CallAfter(self._show_poi_category_dialog)

    def _retry_poi_name_search(self, category_key, name_filter, street_filter, source, radius):
        parts = []
        if name_filter:
            parts.append(f"'{name_filter}'")
        if street_filter:
            parts.append(f"on {street_filter}")
        what = " ".join(parts) if parts else "that search"
        self._announce_transient_then_return(f"No {what} found within {format_distance(radius)}.")
        wx.CallLater(
            2000,
            lambda: self._show_poi_category_dialog(
                initial_key=category_key,
                initial_name=name_filter,
                initial_street=street_filter,
                initial_source=source,
                notice=f"No {what} found within {format_distance(radius)}. Edit the search and try again.",
            ),
        )

    def _poi_travel_time_label(self, distance_m):
        """Approximate POI travel time for list labels."""
        if distance_m < 1000:
            mins = max(1, int(round(distance_m / 80.0)))
            return f"about {mins} min walk"
        mins = max(1, int(round(distance_m / 500.0)))
        return f"about {mins} min drive"

    def _show_poi_in_listbox(self, force_top: bool = False):
        """Populate listbox with all POIs and select the current one.
        Uses _poi_populating flag to suppress EVT_LISTBOX during fill."""
        self._show_list_surface()
        self._poi_populating = True
        labels = []
        for poi in self._poi_list:
            label = poi["label"]
            plat = poi.get("lat"); plon = poi.get("lon")
            suppress_travel = poi.get("kind") in {
                "_shopping_store",
                "_mistral_stop_seq",
                "_transit_route",
                "_transit_stop_seq",
                "_ask_mistral",
                "sentinel",
            }
            if plat is not None and plon is not None and not suppress_travel:
                live_m = int(math.sqrt(
                    ((self.lat - plat) * 111000) ** 2 +
                    ((self.lon - plon) * 111000 * math.cos(math.radians(self.lat))) ** 2
                ))
                live_bearing = compass_name(bearing_deg(self.lat, self.lon, plat, plon))
                label = format_distance_label(label, live_m, live_bearing)
                travel = self._poi_travel_time_label(live_m)
                if not re.search(r'\bmin (?:walk|drive)\b', label):
                    label = f"{label}, {travel}"
                shortcut = _shortcut_label("Ctrl+Enter")
                if (self._transit and
                    poi.get("kind") not in
                    ("_transit_stop","_transit_route","_transit_stop_seq") and
                    TransitLookup.is_transit_poi(poi) and
                    shortcut not in label):
                    label = label + f" — {shortcut} for transit info"
            labels.append(label)
        if force_top:
            self._poi_index = 0
        target_index = 0 if force_top else max(0, min(self._poi_index, len(labels) - 1))
        # Append the new items, select the target one, then delete the old
        # items — never leaves the listbox briefly empty (which is what a
        # Clear()-first sequence does, and what a screen reader can catch
        # as an empty MSAA object right as you land on a POI).
        self.listbox.set_many(labels, sel=target_index)
        self._poi_index = target_index
        self._poi_populating = False
        if not self.listbox.HasFocus():
            self.listbox.SetFocus()

    def _shopping_store_label(self, rec: dict) -> str:
        """Build a shopping-store label, optionally with known location hints."""
        name = (rec.get("name") or "").strip()
        floor = (rec.get("floor") or "").strip()
        landmark = (rec.get("landmark") or "").strip()
        bits = [name] if name else []
        if floor:
            bits.append(floor)
        if landmark:
            bits.append(landmark)
        if len(bits) == 1:
            return bits[0]
        return f"{bits[0]} ({', '.join(bits[1:])})"

    def _airport_poi_info(self, poi: dict) -> tuple[bool, str, str, str]:
        """Return whether a POI is an airport/terminal plus name, query and website."""
        if not isinstance(poi, dict):
            return False, "", "", ""
        tags = poi.get("tags") or {}
        kind = str(poi.get("kind", "") or "").strip().lower()
        aeroway = str(tags.get("aeroway", "") or "").strip().lower()
        is_airport = (
            kind in {"airport", "airport terminal"}
            or aeroway in {"aerodrome", "airport", "terminal"}
        )
        if not is_airport:
            return False, "", "", ""

        label = (poi.get("name") or poi.get("label") or "Airport").split(",")[0].strip()
        name = label or "Airport"
        website = (
            poi.get("website")
            or tags.get("website")
            or tags.get("contact:website")
            or tags.get("url")
            or ""
        ).strip()

        query_bits = [name]
        iata = (tags.get("iata") or tags.get("ref:iata") or tags.get("iata_code") or "").strip()
        if iata and iata.lower() not in name.lower():
            query_bits.append(f"{iata} airport")
        operator = (tags.get("operator") or "").strip()
        if operator and operator.lower() not in name.lower():
            query_bits.append(operator)
        if "airport" not in name.lower() and "terminal" not in name.lower():
            query_bits.append("airport")
        suburb = (getattr(self, "_current_suburb", "") or "").strip()
        if suburb and suburb.lower() not in " ".join(query_bits).lower():
            query_bits.append(suburb)
        country = (getattr(self, "last_country_found", "") or "").strip()
        if country and country.lower() != "open water":
            query_bits.append(country)
        query = " ".join(p for p in query_bits if p).strip()
        return True, name, query, website

    def _on_poi_listbox_select(self, event):
        self._sync_poi_selection_from_listbox()
        event.Skip()

    def _present_poi_list(self):
        if not self._poi_list:
            return
        # Don't overwrite the listbox if the user has drilled into a submenu
        if getattr(self, '_poi_explore_stack', []):
            return
        self._poi_index = 0
        self._show_poi_in_listbox(force_top=True)

    def _set_nav_button_visible(self, show: bool) -> None:
        """Show or hide the AI Summary button below the listbox."""
        btn = getattr(self, "_btn_ai_summary", None)
        if btn is None:
            return
        btn.Show(show)
        self._list_vsizer.Layout()

    def _set_nav_button_busy(self, busy: bool) -> None:
        """Update the GPS AI Summary button while work is running."""
        btn = getattr(self, "_btn_ai_summary", None)
        if btn is None:
            return
        if busy:
            btn.SetLabel("Thinking...")
            btn.Disable()
        else:
            btn.SetLabel("AI Summary (Shift+I)")
            btn.Enable()
        self._list_vsizer.Layout()

    def _clear_poi_state(self) -> None:
        self._poi_list = []
        self._poi_index = 0
        self._poi_explore_stack = []
        self._active_transit_route = None   # clear so Ctrl+Alt+F reverts to route mode
        self._loading = False

    def _close_poi_list(self, repeat_after_return: bool = True):
        self._clear_poi_state()
        self._show_mode_surface(focus=True)
        if repeat_after_return:
            self._repeat_current_location_after_return(250)

    def _show_mode_surface(self, label: str | None = None,
                           focus: bool = False) -> None:
        """Show the non-list map/street command surface."""
        label = str(label or self._map_focus_fallback_label())
        changed = self._mode_label.GetLabel() != label
        self.listbox.Hide()
        self._set_nav_button_visible(False)
        if changed:
            self._mode_label.SetLabel(label)
        self._mode_label.Show()
        self._mode_label.Refresh()
        self._list_vsizer.Layout()
        self._mode_label.GetParent().Layout()
        self._mode_label.Update()
        if focus and not self._mode_label.HasFocus():
            self._mode_label.SetFocus()

    def _show_list_surface(self) -> None:
        """Show the native list when there are browsable rows."""
        self._mode_label.Hide()
        self.listbox.Show()
        self._list_vsizer.Layout()

    def _listbox_set_single(self, text: str) -> None:
        """Replace the listbox with a single item using the
        Append+Select+Delete cycle so screen readers announce it once."""
        self._poi_populating = True
        try:
            self._show_list_surface()
            text = str(text)
            if (self.listbox.GetCount() == 1
                    and self.listbox.GetSelection() == 0
                    and self.listbox.GetString(0) == text):
                return
            self.listbox.set_single(text)
        finally:
            self._poi_populating = False

    def _replace_poi_action_item(self, msg, clear_model=False):
        """Replace the selected POI row after an action is chosen."""
        if clear_model:
            self._poi_list = []
            self._poi_index = 0
            self._poi_explore_stack = []
        self._listbox_set_single(msg)
        self.listbox.SetFocus()

    def _announce_and_restore_poi_list(self, msg, delay_ms=1200):
        """Speak a transient message via AO2, then restore the current POI list."""
        _speak(msg)
        def restore():
            if self._poi_list:
                self._show_poi_in_listbox()
            else:
                self._show_mode_surface(focus=True)
        wx.CallLater(delay_ms, restore)

    def _selected_poi_for_favourite(self):
        if not getattr(self, "_poi_list", []):
            return None
        if 0 <= self._poi_index < len(self._poi_list):
            poi = self._poi_list[self._poi_index]
            if poi.get("lat") is not None and poi.get("lon") is not None:
                return poi
        return None

    def _current_place_favourite_name(self):
        suburb = getattr(self, "_current_suburb", "") or ""
        pinned_num = getattr(self, "_jump_address_number", None)
        pinned_street = getattr(self, "_jump_address_street", None)
        if pinned_num and pinned_street:
            return f"{pinned_num} {pinned_street}" + (f", {suburb}" if suburb else ""), "address"
        if self.street_mode:
            street = getattr(self, "street_label", "") or ""
            if getattr(self, "_walking_mode", False):
                street = getattr(self, "_walk_street", "") or street
            elif getattr(self, "_free_mode", False):
                street = self._free_engine.street_name or street
            if street:
                num = self._nearest_address_number(self.lat, self.lon, street, radius=500)
                if num:
                    return f"{num} {street}" + (f", {suburb}" if suburb else ""), "address"
                return street + (f", {suburb}" if suburb else ""), "street"
        label = getattr(self, "last_location_str", "") or ""
        if label:
            return label, "place"
        return f"{self.lat:.5f}, {self.lon:.5f}", "coordinates"

    def _add_current_favourite(self):
        poi = self._selected_poi_for_favourite()
        if poi:
            name = (poi.get("name") or poi.get("label") or "POI").split(",")[0].strip()
            entry = make_favourite(
                name,
                float(poi["lat"]),
                float(poi["lon"]),
                "poi",
                kind=poi.get("kind", "POI"),
                source=poi.get("source", "poi"),
                meta={k: poi.get(k) for k in ("osm_id", "osm_type", "street") if k in poi},
            )
        else:
            name, kind = self._current_place_favourite_name()
            entry = make_favourite(
                name,
                float(self.lat),
                float(self.lon),
                "place",
                kind=kind,
                source="current_position",
            )
        _, replaced = add_or_replace_favourite(entry)
        action = "Updated" if replaced else "Added"
        self._status_update(f"{action} {entry['name']} in favourites.", force=True)

    def _personal_poi_from_entry(self, entry: dict) -> dict:
        name = str(entry.get("name") or "Personal POI").strip()
        lat = float(entry.get("lat"))
        lon = float(entry.get("lon"))
        number = str(entry.get("number") or "").strip()
        street = str(entry.get("street") or "").strip()
        kind = str(entry.get("kind") or "personal").strip() or "personal"
        label_parts = [name, kind]
        if number and street:
            label_parts.append(f"{number} {street}")
        elif street:
            label_parts.append(street)
        return {
            "label": ", ".join(label_parts),
            "name": name,
            "lat": lat,
            "lon": lon,
            "kind": kind,
            "source": "personal",
            "number": number,
            "street": street,
            "tags": {
                "name": name,
                "addr:housenumber": number,
                "addr:street": street,
                "source": "personal",
            },
            "personal": True,
        }

    def _personal_pois_near_current(self, radius_m: float = 2500.0) -> list:
        out = []
        for entry in getattr(self, "_personal_pois", []) or []:
            try:
                poi = self._personal_poi_from_entry(entry)
            except Exception:
                continue
            d = dist_metres(self.lat, self.lon, poi["lat"], poi["lon"])
            if d > radius_m:
                continue
            poi["dist"] = int(round(d))
            poi["bearing"] = compass_name(bearing_deg(self.lat, self.lon, poi["lat"], poi["lon"]))
            out.append(poi)
        out.sort(key=lambda p: p.get("dist", 0))
        return out

    def _merge_personal_pois(self, pois: list) -> list:
        personal = self._personal_pois_near_current()
        if not personal:
            return list(pois or [])
        seen = set()
        merged = []
        for poi in personal + list(pois or []):
            key = (
                str(poi.get("number") or "").lower(),
                self._street_survey_bare(str(poi.get("street") or "")),
                round(float(poi.get("lat", 0.0)), 5),
                round(float(poi.get("lon", 0.0)), 5),
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(poi)
        return merged

    def _sync_active_personal_pois(self):
        self._all_pois = self._merge_personal_pois(getattr(self, "_all_pois", []))
        self._poi_grid = self._build_poi_grid(self._all_pois)
        try:
            self._free_engine.set_pois(self._all_pois)
        except Exception:
            pass

    def _add_personal_poi_here(self):
        coords, current_name = self._current_map_place()
        number = getattr(self, "_jump_address_number", None)
        street = getattr(self, "_jump_address_street", None)
        if not number or not street:
            number, street = self._extract_street_address_from_label(current_name)
        if not street and self.street_mode:
            street = getattr(self, "street_label", "")
            number = number or self._nearest_address_number(self.lat, self.lon, street, radius=200)
        dlg = wx.TextEntryDialog(
            self,
            "Name for this personal POI:",
            "Add Personal POI",
            "",
        )
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            self._return_focus_to_map(repeat=True)
            return
        name = dlg.GetValue().strip()
        dlg.Destroy()
        if not name:
            self._announce_after_map_focus("Personal POI not saved.")
            return
        entry = {
            "name": name,
            "lat": float(coords[0]),
            "lon": float(coords[1]),
            "number": str(number or "").strip(),
            "street": str(street or "").strip(),
            "kind": "personal",
            "ts": time.time(),
        }
        existing = [
            p for p in (getattr(self, "_personal_pois", []) or [])
            if not (
                str(p.get("name", "")).strip().lower() == name.lower()
                and dist_metres(float(p.get("lat", 0.0)), float(p.get("lon", 0.0)),
                                entry["lat"], entry["lon"]) < 10
            )
        ]
        existing.insert(0, entry)
        self._personal_pois = existing
        _save_personal_pois(existing)
        self._sync_active_personal_pois()
        self._announce_after_map_focus(f"Saved personal POI {name}.")

    def _personal_poi_matches_entry(self, candidate: dict, entry: dict) -> bool:
        if candidate is entry:
            return True
        try:
            same_point = dist_metres(
                float(candidate.get("lat", 0.0)),
                float(candidate.get("lon", 0.0)),
                float(entry.get("lat", 0.0)),
                float(entry.get("lon", 0.0)),
            ) < 1.0
        except Exception:
            same_point = False
        return (
            same_point
            and str(candidate.get("name", "")).strip().lower()
            == str(entry.get("name", "")).strip().lower()
            and str(candidate.get("street", "")).strip().lower()
            == str(entry.get("street", "")).strip().lower()
            and str(candidate.get("number", "")).strip().lower()
            == str(entry.get("number", "")).strip().lower()
        )

    def _rename_personal_poi_entry(self, entry: dict, new_name: str) -> list:
        updated = []
        changed = False
        for candidate in getattr(self, "_personal_pois", []) or []:
            if not changed and self._personal_poi_matches_entry(candidate, entry):
                candidate = dict(candidate)
                candidate["name"] = new_name
                entry["name"] = new_name
                changed = True
            updated.append(candidate)
        self._personal_pois = updated
        _save_personal_pois(updated)
        self._sync_active_personal_pois()
        return updated

    def _delete_personal_poi_entry(self, entry: dict) -> list:
        updated = [
            candidate for candidate in (getattr(self, "_personal_pois", []) or [])
            if not self._personal_poi_matches_entry(candidate, entry)
        ]
        self._personal_pois = updated
        _save_personal_pois(updated)
        self._sync_active_personal_pois()
        return updated

    def _show_favourites(self):
        existing = getattr(self, "_favourites_dlg", None)
        if existing:
            try:
                if existing.IsShown():
                    existing.Raise()
                    existing.SetFocus()
                    return
            except Exception:
                pass
            self._favourites_dlg = None
        entries = load_favourites()
        self._personal_pois = _load_personal_pois()
        if not entries and not self._personal_pois:
            self._announce_transient_then_return("No favourites saved.")
            return
        dlg = FavouritesDialog(self, entries, personal_pois=self._personal_pois)
        self._favourites_dlg = dlg
        dlg.Bind(wx.EVT_WINDOW_DESTROY, lambda e: setattr(self, "_favourites_dlg", None) if e.GetEventObject() is dlg else e.Skip())
        dlg.Show()
        dlg.SetFocus()

    def _favourite_as_poi(self, entry):
        return {
            "label": entry.get("name", "Favourite"),
            "name": entry.get("name", "Favourite"),
            "lat": float(entry.get("lat")),
            "lon": float(entry.get("lon")),
            "kind": entry.get("kind", "favourite"),
            "source": entry.get("source", "favourite"),
        }

    def _jump_to_saved_entry(self, entry, is_personal=False):
        label = "Personal POI" if is_personal else "Favourite"
        try:
            poi = (self._personal_poi_from_entry(entry) if is_personal
                   else self._favourite_as_poi(entry))
        except Exception:
            self._announce_transient_then_return(f"{label} has no valid position.")
            return
        self._poi_list = [poi]
        self._poi_index = 0
        self._poi_explore_stack = []
        self._jump_to_poi()
        wx.CallAfter(self.listbox.SetFocus)

    def _navigate_to_saved_entry(self, entry, is_personal=False):
        label = "Personal POI" if is_personal else "Favourite"
        try:
            lat = float(entry.get("lat"))
            lon = float(entry.get("lon"))
        except (TypeError, ValueError):
            self._announce_transient_then_return(f"{label} has no valid position.")
            return
        name = entry.get("name", label)
        if is_personal:
            source = "personal"
        else:
            source = "poi" if entry.get("type") == "poi" else "favourite"
        self._nav_launch(lat, lon, name, target_source=source, target_meta=entry)

    def _on_listbox_char(self, event):
        """Handle printable chars in POI listbox for first-letter navigation.
        Consume event to prevent EVT_CHAR_HOOK from processing it."""
        key = event.GetKeyCode()
        no_mod = (not event.ShiftDown() and not _primary_down(event)
                  and not event.AltDown())
        # Unmodified printable key: let default listbox handler do first-letter nav
        if no_mod and 32 <= key < 256:
            event.Skip()  # Let listbox's default first-letter nav work
            return
        # For everything else, pass to keyboard handler
        event.Skip()

    def _on_keyboard(self, event):
        """Route keys: listbox navigation only, everything else forwarded to on_key."""
        _log_key_event(self, event, "frame-router")
        if getattr(self, "_transit_drill_modal_open", False):
            key = event.GetKeyCode()
            if key == wx.WXK_BACK:
                dlg = getattr(self, "_active_transit_drill_dlg", None)
                items = getattr(self, "_active_transit_drill_items", [])
                miab_log(
                    "verbose",
                    f"Transit modal backspace: dlg_alive={dlg is not None} items={len(items)}",
                    self.settings,
                )
                if dlg is not None:
                    idx = dlg._lb.GetSelection() if hasattr(dlg, "_lb") else wx.NOT_FOUND
                    if 0 <= idx < len(items):
                        kind = items[idx].get("kind", "")
                        if kind in ("_leaf", "_transit_stop_seq", "_mistral_stop_seq"):
                            self._transit_drill_back_one_level = True
                    dlg.EndModal(wx.ID_CANCEL)
                    return
            event.Skip()
            return
        # If focus is outside the main frame (e.g. a modal dialog is open),
        # let the event go to wherever focus actually is.
        # NB: self.FindFocus() returns None for controls in child dialogs;
        #     wx.Window.FindFocus() is the global version that always works.
        focused = wx.Window.FindFocus()
        if focused is not None and not self.IsDescendant(focused) and focused != self:
            event.Skip()
            return

        key = event.GetKeyCode()
        poi_list_open = bool(self._poi_list)
        is_listbox_focused = (focused == self.listbox or
                               (poi_list_open and focused == self))

        # TAB: allow focus traversal while a POI list is open or navigation is
        # active (so the user can reach the AI Summary button); swallow otherwise.
        if key == wx.WXK_TAB:
            if poi_list_open or getattr(self, '_nav_active', False):
                event.Skip()
            return

        if poi_list_open and is_listbox_focused:
            self._sync_poi_selection_from_listbox()

            if key in (wx.WXK_UP, wx.WXK_DOWN):
                n = self.listbox.GetCount()
                if n > 0:
                    idx = self.listbox.GetSelection()
                    idx = max(0, idx - 1) if key == wx.WXK_UP else min(n - 1, idx + 1)
                    self.listbox.SetSelection(idx)
                    self._poi_index = idx
                return

            if key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
                if _primary_down(event):
                    self._street_confirm_explore()
                else:
                    self._enter_selected_poi_or_drill()
                return

            if key == wx.WXK_F2:
                self._rename_poi()
                return

            if key == wx.WXK_DELETE:
                self._report_poi_nonexistent()
                return

            if _primary_down(event) and not event.ShiftDown() and not event.AltDown() and key in (ord('M'), ord('m')):
                poi = self._poi_list[self._poi_index]
                coords = (float(poi.get("lat", self.lat)), float(poi.get("lon", self.lon)))
                name = poi.get("label", "").split(",")[0].strip() or poi.get("name", "").split(",")[0].strip() or "selected POI"
                self._prompt_mark_slot(remove=False, coords=coords, name=name)
                return

            if key == wx.WXK_BACK:
                if getattr(self, "_poi_explore_stack", []):
                    self._explore_back()
                else:
                    self._close_poi_list()
                return

            if key == wx.WXK_ESCAPE:
                self._close_poi_list()
                return

            # Any modifier (Ctrl / Alt) held → always forward to on_key.
            # New modifier+key bindings work automatically without needing to be
            # added here as well. Unmodified keys skip to listbox for first-letter nav.
            if _primary_down(event) or event.AltDown():
                self.on_key(event)
                return

            event.Skip()
            return

        # Block on_key entirely for unmodified printable keys when POI list is open
        no_mod = (not _primary_down(event) and not event.AltDown()
                  and not event.ShiftDown())
        if poi_list_open and no_mod and 32 <= key < 256:
            if not is_listbox_focused:
                self.listbox.SetFocus()
            event.Skip()
            return

        # At idle the listbox contains only the mode label.  Bare M has no
        # command, so letting it reach the native listbox handler merely
        # selects "Map mode" and causes a misleading screen-reader
        # announcement.  Keep first-letter navigation available for actual
        # POI/result lists, but consume M for the single idle mode row.
        if not poi_list_open and no_mod and key in (ord('M'), ord('m')):
            return

        self.on_key(event)

    def _transit_drill_or_jump(self):
        """Enter on a POI — drill into transit children, load Google Places, or jump."""
        if not (self._poi_list and self._poi_index < len(self._poi_list)):
            return
        self._sync_poi_selection_from_listbox()
        poi = self._poi_list[self._poi_index]
        kind = poi.get("kind", "")
        
        # Handle "Ask Mistral for store directory" sentinel
        if kind == "sentinel" and poi.get("sentinel_type") == "ask_shopping":
            centre_name = poi.get("_centre_name", "")
            lat         = poi.get("lat", 0)
            lon         = poi.get("lon", 0)
            _speak(f"Fetching stores for {centre_name}…")
            try:
                self.sound.play_file(r"c:\windows\media\alarm09.wav", loops=-1)
            except Exception:
                pass
            done_event = threading.Event()
            def _progress():
                for msg in [
                    f"Searching {centre_name} store directory…",
                    "Checking official centre website…",
                    "Compiling store list…",
                    "Almost there…",
                ]:
                    if done_event.wait(timeout=5):
                        return
                    wx.CallAfter(self._status_update, msg)
            threading.Thread(target=_progress, daemon=True).start()
            def _fetch_stores(n=centre_name, la=lat, lo=lon):
                centre_address = (poi.get("_centre_address") or "").strip()
                directory_url = (poi.get("_centre_website") or "").strip()
                source_text, source_links = mall_directory.fetch_official_source_text(
                    directory_url, n
                )
                tenants = []
                if source_text:
                    prompt = (
                        f"Extract the tenant/store names from the official shopping-centre page for '{n}'. "
                        f"Use only the provided source text and links. Return ONLY a JSON array of store name strings. "
                        f"Do not include phone numbers, parking, trading hours, centre names, headings, or descriptions. "
                        f"Do not guess. If a store name is not clearly supported by the source text, omit it. "
                        f"Return the names in strict alphabetical order.\n\n"
                        f"SOURCE TEXT:\n{source_text}"
                    )
                    cache_key = f"shop_extract_{mall_directory._CACHE_VERSION}_{mall_directory._normalise(centre_address or n)}_{la:.4f}_{lo:.4f}"
                    text = self._mistral.query_text(prompt, cache_key)
                    names = self._mistral._parse_json_list(text)
                    clean = self._mistral._clean_store_names(names, n)
                    clean = self._mistral._retain_evidenced_store_names(
                        clean, source_text, existing_names=None
                    )
                    tenants = [{"name": s, "source": "official"} for s in clean]
                done_event.set()
                try:
                    self.sound.stop()
                except Exception:
                    pass
                if not tenants:
                    wx.CallAfter(
                        self._announce_transient_then_return,
                        f"No official store directory found for {n}.")
                    return
                child_pois = [
                    {
                        "label":          self._shopping_store_label(rec),
                        "lat":            rec.get("lat") if rec.get("lat") is not None else la,
                        "lon":            rec.get("lon") if rec.get("lon") is not None else lo,
                        "kind":           "_shopping_store",
                        "_store_name":    rec.get("name", ""),
                        "_centre_name":   n,
                        "_centre_address": centre_address,
                        "_directory_text": source_text,
                        "_directory_links": source_links,
                        "_tenant_record": rec,
                    }
                    for rec in tenants
                ]
                import time as _time
                _time.sleep(0.05)
                def _push():
                    self._poi_explore_stack.append((list(self._poi_list), self._poi_index))
                    self._poi_list  = child_pois
                    self._poi_index = 0
                    self._show_poi_in_listbox()
                    self.listbox.SetFocus()
                wx.CallAfter(_push)
            threading.Thread(target=_fetch_stores, daemon=True).start()
            return

        # Handle "Airport Amenity Guide" sentinel
        if kind == "sentinel" and poi.get("sentinel_type") == "ask_airport_amenities":
            airport_name = poi.get("_airport_name", "") or poi.get("label", "")
            query = poi.get("_airport_query", "") or airport_name
            source_hint = poi.get("_airport_website", "")
            self._show_airport_amenity_guide(
                query,
                source_hint=source_hint,
                airport_name=airport_name,
            )
            return

        # Handle "Get times" sentinel
        if kind == "sentinel" and poi.get("sentinel_type") == "get_times":
            operator   = poi.get("operator", "")
            service    = poi.get("service", "")
            route_name = poi.get("route_name", "")
            self._transit_nav_announce(f"Fetching timetable for {operator} {service}...")
            def _fetch_times():
                text = self._mistral.ask_times(operator, service, route_name)
                # Push as a single-item explore leaf so screenreader can read
                # the full text uninterrupted. Backspace returns to the stop list.
                leaf = [{
                    "label": text,
                    "lat":   poi.get("lat", 0),
                    "lon":   poi.get("lon", 0),
                    "kind":  "_mistral_stop_seq",
                }]
                def _show():
                    self._poi_explore_stack.append((list(self._poi_list), self._poi_index))
                    self._poi_list  = leaf
                    self._poi_index = 0
                    self._listbox_set_single(text)
                    self.listbox.SetFocus()
                wx.CallAfter(_show)
            threading.Thread(target=_fetch_times, daemon=True).start()
            return
        
        if kind == "_shopping_store":
            self._last_shopping_store_poi = dict(poi)
            store_name  = poi.get("_store_name", poi.get("label", ""))
            centre_name = poi.get("_centre_name", "")
            centre_address = poi.get("_centre_address", "")
            tenant_rec  = poi.get("_tenant_record") or {}
            directory_text = poi.get("_directory_text", "")
            directory_links = poi.get("_directory_links", [])
            # If we have a rich HERE/OSM record, format locally — no AI call.
            has_rich = bool(tenant_rec.get("category") or tenant_rec.get("address")
                            or tenant_rec.get("phone") or tenant_rec.get("opening_hours")
                            or tenant_rec.get("distance_m") is not None)
            if has_rich:
                text = mall_directory.describe_tenant(tenant_rec, centre_name)
                self._show_detail_reader(text)
                return
            _speak(f"Looking up {store_name}…")
            def _fetch_detail(s=store_name, c=centre_name):
                detail_text = directory_text
                if directory_text:
                    low_text = directory_text.lower()
                    low_name = (s or "").lower().strip()
                    if low_name and low_name in low_text:
                        spans = []
                        start = 0
                        while True:
                            idx = low_text.find(low_name, start)
                            if idx < 0:
                                break
                            lo = max(0, idx - 500)
                            hi = min(len(directory_text), idx + 1000)
                            spans.append(directory_text[lo:hi])
                            start = idx + len(low_name)
                        if spans:
                            detail_text = "\n\n".join(spans[:5])
                text = self._mistral.ask_store_detail(
                    s,
                    c,
                    centre_address,
                    source_text=detail_text,
                    source_links=directory_links,
                )
                def _push():
                    self._show_detail_reader(text)
                wx.CallAfter(_push)
            threading.Thread(target=_fetch_detail, daemon=True).start()
            return
        elif kind == "_transit_stop":
            stop_name = poi["label"].split("—")[0].strip()
            self._status_update(f"Loading routes for {stop_name}...")
            threading.Thread(target=self._explore_transit_poi,
                             args=(poi,), daemon=True).start()
        elif kind == "_transit_route":
            route_name = poi.get("_route_name", poi["label"].split("—")[0].strip())
            self._status_update(f"Loading stops for {route_name}...")
            self._explore_transit_route(poi)
        elif kind == "_transit_stop_seq":
            pass   # leaf node
        elif kind == "_ask_mistral":
            if not self._mistral.is_configured:
                miab_log("api_calls", "[Mistral] Not configured — no API key.", getattr(self, "settings", None))
                self._transit_nav_announce(
                    "No Mistral API key configured. "
                    "Add your key in Settings (Ctrl+comma) under Mistral API key.")
                return
            self._status_update("Asking Mistral for long-distance services…")
            threading.Thread(
                target=self._explore_mistral_transit,
                args=(poi,), daemon=True).start()
        elif kind == "_mistral_service":
            self._explore_mistral_service(poi)
        elif kind == "_mistral_stop_seq":
            pass   # leaf node
        else:
            self._street_confirm_jump()

    def _poi_entry_uses_action_dialog(self, poi):
        kind = (poi or {}).get("kind", "")
        if kind in {
            "_transit_stop",
            "_transit_route",
            "_transit_stop_seq",
            "_ask_mistral",
            "_mistral_service",
            "_mistral_stop_seq",
            "_shopping_store",
        }:
            return False
        if kind == "sentinel":
            return False
        return True

    def _enter_selected_poi_or_drill(self):
        if not (self._poi_list and self._poi_index < len(self._poi_list)):
            return
        self._sync_poi_selection_from_listbox()
        poi = self._poi_list[self._poi_index]
        if poi.get("kind") not in {"_shopping_store", "sentinel"} and self._last_shopping_store_poi:
            last = self._last_shopping_store_poi
            current_name = (poi.get("label") or poi.get("name") or "").strip().lower()
            last_name = (last.get("label") or last.get("_store_name") or last.get("name") or "").strip().lower()
            if current_name == last_name or not current_name:
                poi = last
        if self._poi_entry_uses_action_dialog(poi):
            self._poi_enter_action_dialog()
        else:
            self._transit_drill_or_jump()

    def _poi_enter_action_dialog(self):
        """Enter on a POI — choose between current POI action and GPS route."""
        if not self._poi_list:
            self._announce_transient_then_return("No points of interest loaded.")
            return
        self._sync_poi_selection_from_listbox()
        if not (0 <= self._poi_index < len(self._poi_list)):
            self._announce_transient_then_return("No point of interest selected.")
            return

        poi = self._poi_list[self._poi_index]
        name = (poi.get("label") or poi.get("name") or "POI").split(",")[0].strip()
        choices = ["Explore position", "Navigate to POI", "Add to favourites"]
        dlg = wx.SingleChoiceDialog(
            self,
            f"What do you want to do with {name}?",
            "POI Action",
            choices,
        )
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            self.listbox.SetFocus()
            return
        sel = dlg.GetSelection()
        dlg.Destroy()

        if sel == 0:
            saved_index = self._poi_index
            self._replace_poi_action_item(f"Exploring {name}...")
            self._poi_index = saved_index
            self._jump_to_poi()
            return

        if sel == 2:
            self._add_current_favourite()
            self.listbox.SetFocus()
            return

        lat = poi.get("lat")
        lon = poi.get("lon")
        if lat is None or lon is None:
            self._announce_transient_then_return(f"No GPS coordinate for {name}.")
            return
        self._replace_poi_action_item(f"Navigating to {name}...", clear_model=True)
        self._nav_launch(
            float(lat), float(lon), name,
            target_source="poi",
            target_meta=poi,
        )

    def _sync_poi_selection_from_listbox(self):
        if self._poi_populating or not self._poi_list:
            return
        sel = self.listbox.GetSelection()
        if sel != wx.NOT_FOUND and 0 <= sel < len(self._poi_list):
            self._poi_index = sel

    def _jump_to_poi(self):
        if not self._poi_list:
            self._announce_transient_then_return("No points of interest loaded.")
            return
        poi = self._poi_list[self._poi_index]
        plat = poi["lat"]; plon = poi["lon"]
        name = poi["label"].split(",")[0]
        
        # Check if POI is in water
        if not _IS_LAND(plat, plon):
            self._announce_transient_then_return(
                f"Can't jump to {name}. Location is in water.")
            wx.CallLater(2000, self._close_poi_list)
            return
        
        # Check if POI is within already-loaded area by testing if streets exist there
        within_loaded = False
        if self._road_fetched and self._road_segments:
            test_road, _ = self._street_fetcher.nearest_road(plat, plon, self._road_segments)
            within_loaded = (test_road != "No street data nearby")

        self.lat = plat
        self.lon = plon

        # ── Transit hub: check for eateries within walking distance ──────────
        # _check_transit_eateries was never implemented; guard so a transit
        # POI jump doesn't raise AttributeError and abort the rest of
        # _jump_to_poi (which would leave the POI list loaded and the
        # listbox's native arrow handler hijacking Up/Down).
        if TransitLookup.is_transit_poi(poi) and hasattr(self, "_check_transit_eateries"):
            threading.Thread(
                target=self._check_transit_eateries,
                args=(plat, plon, name),
                daemon=True,
            ).start()

        self._poi_list          = []
        self._poi_index         = 0
        self._jump_street_label    = None
        self._jump_street_pin_lat  = None
        self._jump_street_pin_lon  = None

        # In map mode, a POI jump should take the user into the local street
        # area for that POI rather than stopping at the world-map cursor.
        if not self.street_mode:
            self.last_location_str = name
            self._set_current_location_title(name)
            self.last_city_found = ""
            self._force_geocode_suburb_once = True
            self._last_jump_display_label = name
            self._last_jump_display_until = time.time() + 1.5
            wx.CallAfter(self.map_panel.set_position, self.lat, self.lon, False, "")
            if getattr(self, "_prefetch_in_progress", False):
                self._announce_transient_then_return("Street download in progress. Please wait.")
            else:
                self._suppress_next_street_loading_status = True
                self.toggle_street_mode()
            return

        # Invalidate cache center to force validation
        self._cache_center_lat = None
        self._cache_center_lon = None

        if within_loaded:
            # Stay on existing road data — just re-query nearest street
            miab_log(
                "verbose",
                f"POI within loaded area, using existing segments ({len(self._road_segments)} segments)",
                self.settings,
            )
            label, cross = self._nearest_road(self.lat, self.lon)
            self.street_label = label
            wx.CallAfter(self.map_panel.set_position, self.lat, self.lon, True, label)
            wx.CallAfter(self._update_street_display)
            wx.CallAfter(self._force_listbox_refocus)
            
            # Force fetch to ensure data is current for this location
            # Fetch fresh data — guard against concurrent fetches
            if not self._fetch_in_progress:
                self._fetch_in_progress = True
                self._distance_since_fetch = 0
                threading.Thread(target=self._query_street, daemon=True).start()
            
            threading.Thread(target=self._fetch_poi_intersection,
                             args=(plat, plon, name,
                                   poi.get("street", "")), daemon=True).start()
        else:
            self._status_update(f"Jumping to {name}.  Loading streets...")
            self._loading = True
            wx.CallAfter(self.map_panel.set_position, self.lat, self.lon, True, "")
            threading.Thread(
                target=self._load_streets_after_poi_jump,
                args=(plat, plon, name, poi.get("street", "")),
                daemon=True
            ).start()
    
    def _load_streets_after_poi_jump(self, lat, lon, poi_name, known_street=""):
        """Load streets after POI jump - tries cache first."""
        try:
            from street_data import geocode_location, _load_road_cache
            prev_suburb = getattr(self, '_current_suburb', None)
            geo = geocode_location(lat, lon)
            if geo:
                self._current_suburb = geo.get("suburb")
                self._current_country_code = geo.get("country_code", "")
                self._current_osm_type = geo.get("osm_type")
                self._current_osm_id = geo.get("osm_id")
                radius = geo.get("radius", 3000)
                self._street_radius  = radius
                self._street_barrier = int(radius * 0.9)
                self._street_bbox = geo.get("bbox")
                self._prefetch_geo_features_for_point(lat, lon)
            else:
                self._street_radius  = 3000
                self._street_barrier = 2700
                self._current_suburb = None
                self._current_osm_type = None
                self._current_osm_id = None
            cache_entry = _load_road_cache(
                self._street_fetcher._cache_dir,
                lat, lon,
                suburb_name=self._current_suburb
            )
            
            if cache_entry and cache_entry.get("segments"):
                # Cache hit — check if the landing point is within the data radius.
                cached_segments = cache_entry.get("segments", [])
                test_label, cross = self._street_fetcher.nearest_road(lat, lon, cached_segments)
                if test_label in ("No street data nearby", "Unknown", "", "No street data"):
                    # Distinguish park/open-area (within loaded radius) from a street
                    # that genuinely lies outside the loaded data area.
                    # Prefer the cache entry's own stored center over the old fetch
                    # origin — they can differ when the named cache (e.g. Wellington
                    # Point) was downloaded from a different location than the
                    # session's first street fetch (e.g. Ormiston).
                    prev_lat = cache_entry.get("cache_center_lat") or getattr(self, '_road_fetch_lat', None)
                    prev_lon = cache_entry.get("cache_center_lon") or getattr(self, '_road_fetch_lon', None)
                    radius   = getattr(self, '_street_radius', 3000)
                    if (prev_lat is None or prev_lon is None or
                            dist_metres(lat, lon, prev_lat, prev_lon) > radius):
                        # Outside the data — download or prompt depending on suburb.
                        self._loading = False
                        suburb_name = self._current_suburb or "this area"
                        fn = (self._auto_download_poi_suburb
                              if suburb_name and suburb_name == prev_suburb
                              else self._confirm_poi_suburb_download)
                        wx.CallAfter(self._status_update,
                                     f"Jumped to {poi_name}. No cached streets.")
                        wx.CallAfter(fn, lat, lon, poi_name, known_street, suburb_name)
                        return
                    # Within radius (park/open area) — load segments as-is
                    test_label = ""
                self._road_segments  = cached_segments
                self._address_points = self._cache_addresses_for_current_gnaf_mode(cache_entry)
                self._road_fetched   = True
                self._data_ready     = True
                self._cache_center_lat = lat
                self._cache_center_lon = lon
                self._road_fetch_lat = lat
                self._road_fetch_lon = lon
                self._loading        = False
                try:
                    self._free_engine.set_segments(cached_segments)
                except Exception:
                    pass
                self.street_label = test_label
                wx.CallAfter(self.map_panel.set_position, lat, lon, True, test_label)
                wx.CallAfter(self._update_street_display)
                wx.CallAfter(self._force_listbox_refocus)
                threading.Thread(target=self._fetch_poi_intersection,
                               args=(lat, lon, poi_name, known_street), daemon=True).start()
            else:
                # No named cache for this suburb — but check if currently-loaded
                # segments already cover this location before prompting a download.
                # (e.g. Ormiston cache loaded, user jumps to a Wellington Point POI
                # that geocodes as "Wellington Point" → no separate cache entry exists
                # yet the data is already in memory.)
                existing_segs = getattr(self, '_road_segments', [])
                if existing_segs:
                    test_label, _ = self._street_fetcher.nearest_road(lat, lon, existing_segs)
                    prev_lat = getattr(self, '_road_fetch_lat', None)
                    prev_lon = getattr(self, '_road_fetch_lon', None)
                    within_radius = (
                        prev_lat is not None and prev_lon is not None and
                        dist_metres(lat, lon, prev_lat, prev_lon) <= getattr(self, '_street_radius', 3000)
                    )
                    if test_label not in ("No street data nearby", "Unknown", "", "No street data") or within_radius:
                        self._road_fetched = True
                        self._data_ready   = True
                        self._loading      = False
                        try:
                            self._free_engine.set_segments(existing_segs)
                        except Exception:
                            pass
                        self.street_label = test_label
                        wx.CallAfter(self.map_panel.set_position, lat, lon, True, test_label)
                        wx.CallAfter(self._update_street_display)
                        wx.CallAfter(self._force_listbox_refocus)
                        threading.Thread(target=self._fetch_poi_intersection,
                                         args=(lat, lon, poi_name, known_street), daemon=True).start()
                        return
                self._loading = False
                suburb_name = self._current_suburb or "this area"
                fn = (self._auto_download_poi_suburb
                      if suburb_name and suburb_name == prev_suburb
                      else self._confirm_poi_suburb_download)
                wx.CallAfter(self._status_update, f"Jumped to {poi_name}. No cached streets.")
                wx.CallAfter(fn, lat, lon, poi_name, known_street, suburb_name)
        except Exception as e:
            miab_log("poi_jump", f"Cache load error: {e}", self.settings)
            self._loading = False
            suburb_name = getattr(self, '_current_suburb', None) or "this area"
            fn = (self._auto_download_poi_suburb
                  if suburb_name and suburb_name == prev_suburb
                  else self._confirm_poi_suburb_download)
            wx.CallAfter(self._status_update, f"Jumped to {poi_name}. Error loading cache.")
            wx.CallAfter(fn, lat, lon, poi_name, known_street, suburb_name)

    def _fetch_poi_intersection(self, lat, lon, poi_name, known_street=""):
        """Find the two closest named roads to the POI. Delegates to PoiFetcher."""
        names = self._poi_fetcher.nearest_cross_streets(
            lat, lon, getattr(self, "_road_segments", [])
        )
        if names:
            addr_part = f"  Address: {known_street}." if known_street else ""
            cross = " and ".join(names)
            wx.CallAfter(self._announce_and_restore_poi_list,
                f"{poi_name}.{addr_part}  Near the corner of {cross}.")
        else:
            wx.CallAfter(self._announce_and_restore_poi_list,
                f"{poi_name}.  No nearby street names found.")
    def _street_search(self):
        """S key — open the non-modal street search dialog.
        If already open, bring it to front and do nothing else."""
        existing = getattr(self, '_street_search_dlg', None)
        if existing:
            try:
                if existing.IsShown():
                    existing.Raise()
                    existing.SetFocus()
                    return
            except Exception:
                pass
            self._street_search_dlg = None

        if not self._road_segments and not getattr(self, '_road_fetch_lat', None):
            self._announce_transient_then_return("No street data loaded.")
            return

        self._street_search_dlg = _StreetSearchFrame(self)
        self._street_search_dlg.Show()

    def _jump_to_street(self, street_name, house_number=""):
        """Jump to the nearest point on the named street from current position.

        If house_number is given, locates that specific address in _address_points
        using the same suffix-stripping normalisation as _nearest_address_number.
        Falls back to nearest street point with a spoken announcement if not found."""
        best_dist = float("inf")
        best_lat  = None
        best_lon  = None

        _all_segs   = len(self._road_segments) if hasattr(self, '_road_segments') else 0
        _match_segs = sum(
            1 for seg in self._road_segments
            if re.sub(r"\s*\(.*?\)", "", seg.get("name", "")).strip().lower() == street_name.lower()
        ) if hasattr(self, '_road_segments') else 0
        miab_log("snap",
                 f"_jump_to_street: seeking '{street_name}' from ({self.lat:.5f},{self.lon:.5f}); "
                 f"{_match_segs}/{_all_segs} segments match",
                 self.settings)

        for seg in self._road_segments:
            raw = re.sub(r"\s*\(.*?\)", "", seg.get("name", "")).strip()
            if raw.lower() != street_name.lower():
                continue
            coords = seg["coords"]
            for i in range(len(coords) - 1):
                alat, alon = coords[i]
                blat, blon = coords[i + 1]
                dlat = blat - alat
                dlon = blon - alon
                sql  = dlat**2 + dlon**2
                if sql == 0:
                    t = 0.0
                else:
                    t = max(0.0, min(1.0,
                        ((self.lat - alat) * dlat +
                         (self.lon - alon) * dlon) / sql))
                plat = alat + t * dlat
                plon = alon + t * dlon
                d = math.sqrt(
                    ((plat - self.lat) * 111000)**2 +
                    ((plon - self.lon) * 111000 *
                     math.cos(math.radians(self.lat)))**2)
                if d < best_dist:
                    best_dist = d
                    best_lat  = plat
                    best_lon  = plon

        miab_log("snap",
                 f"_jump_to_street: projection pass done — best_dist={best_dist:.1f}m, "
                 f"best_pos=({best_lat},{best_lon})",
                 self.settings)

        if best_lat is None:
            # No matching geometry found — try matching the full display name
            # (in case the segment name has no parenthetical to strip)
            for seg in self._road_segments:
                full_name = seg.get("name", "").strip()
                if full_name.lower() != street_name.lower():
                    continue
                coords = seg["coords"]
                # Jump to the midpoint of the first matching segment
                mid = len(coords) // 2
                best_lat = coords[mid][0]
                best_lon = coords[mid][1]
                best_dist = 0
                break

        if best_lat is None:
            self._announce_transient_then_return(
                f"Could not locate {street_name} yet. The suburb may still be loading in background.")
            return

        self.lat = best_lat
        self.lon = best_lon
        self.street_label    = street_name
        self._jump_street_label = street_name

        def _nearest_on_selected_street(lat, lon):
            projected = None
            projected_dist = float("inf")
            for seg in self._road_segments:
                raw = re.sub(r"\s*\(.*?\)", "", seg.get("name", "")).strip()
                if raw.lower() != street_name.lower():
                    continue
                coords = seg.get("coords", [])
                for i in range(len(coords) - 1):
                    alat, alon = coords[i]
                    blat, blon = coords[i + 1]
                    plat, plon = nearest_point_on_segment(
                        lat, lon, alat, alon, blat, blon)
                    d = dist_metres(lat, lon, plat, plon)
                    if d < projected_dist:
                        projected_dist = d
                        projected = (plat, plon)
            if projected is None:
                return None
            return projected[0], projected[1], projected_dist

        # ── House number resolution ───────────────────────────────────
        # Uses the same suffix-stripping bare() as _nearest_address_number
        # so "Queen Street" matches "Queen St" in address data.
        number_found = False
        if house_number:
            _ADDR_SUFFIXES = {
                "street", "st", "road", "rd", "avenue", "ave", "drive", "dr",
                "court", "ct", "place", "pl", "crescent", "cres", "close", "cl",
                "boulevard", "blvd", "highway", "hwy", "terrace", "tce",
                "parade", "pde", "esplanade", "esp", "lane", "ln", "grove", "gr",
                "way", "circuit", "cct", "rise", "row", "mews", "track",
            }
            def _bare(s):
                parts = s.lower().split(",")[0].strip().split()
                if parts and parts[-1] in _ADDR_SUFFIXES:
                    parts = parts[:-1]
                return " ".join(parts)

            bare_target = _bare(street_name)
            num_want    = house_number.strip().lower()
            # Also prepare a digits-only fallback for "12A" → "12"
            num_digits  = re.sub(r'[^0-9]', '', num_want)
            resolved_house_number = None

            addr_pts = getattr(self, '_address_points', [])
            # Log all address points on this street for debugging
            on_street = [ap for ap in addr_pts if _bare(ap.get('street', '')) == bare_target]
            miab_log("street", f"[StreetJump] Seeking #{house_number} on '{street_name}' "
                  f"(bare='{bare_target}'). {len(on_street)} address points on street. "
                  f"Numbers: {sorted(set(ap['number'] for ap in on_street))[:20]}", getattr(self, "settings", None))

            def _pick_address_candidate(candidates):
                """Choose the address whose street projection is most plausible."""
                scored = []
                for candidate in candidates:
                    projected = _nearest_on_selected_street(
                        candidate['lat'], candidate['lon'])
                    snap_d = projected[2] if projected else float("inf")
                    from_here = dist_metres(
                        best_lat, best_lon, candidate['lat'], candidate['lon'])
                    scored.append((snap_d, from_here, candidate, projected))
                return min(scored, key=lambda item: (item[0], item[1]))

            def _apply_address_candidate(best_pt, projected, snap_d):
                """Snap to nearest point on the target street, unless the segment
                is too far away (address outside loaded data) — in that case use
                the raw address point and force a data reload at that location."""
                if projected and snap_d <= 100:
                    self.lat, self.lon, _ = projected
                    miab_log("street", f"[StreetJump] Snapped #{best_pt['number']} onto {street_name} "
                          f"({snap_d:.1f}m from address point) at ({self.lat:.5f},{self.lon:.5f})", None)
                else:
                    self.lat = best_pt['lat']
                    self.lon = best_pt['lon']
                    if snap_d > 100:
                        miab_log("snap",
                                 f"snap_d={snap_d:.0f}m > 100m — nearest segment is far; "
                                 f"using raw address point ({self.lat:.5f},{self.lon:.5f}), forcing reload",
                                 self.settings)
                        # Force a fresh download centred on the actual address location
                        self._road_fetch_lat = None
                        self._road_fetch_lon = None
                    else:
                        miab_log("street", f"[StreetJump] No projection found for #{best_pt['number']}; "
                              f"using address point ({self.lat:.5f},{self.lon:.5f})", None)

            # Exact match first
            exact = [ap for ap in on_street
                     if ap.get('number', '').strip().lower() == num_want
                     and ap.get('lat') and ap.get('lon')]
            if exact:
                snap_d, _from_here, best_pt, projected = _pick_address_candidate(exact)
                miab_log("street", f"[StreetJump] Exact match #{best_pt['number']} at ({best_pt['lat']:.5f},{best_pt['lon']:.5f})", getattr(self, "settings", None))
                _apply_address_candidate(best_pt, projected, snap_d)
                best_lat, best_lon = self.lat, self.lon
                number_found = True
                resolved_house_number = str(best_pt.get('number') or house_number).strip()
            elif num_digits:
                # Digits-only fallback: "12A" finds "12", "12B" etc.
                fuzzy = [ap for ap in on_street
                         if re.sub(r'[^0-9]', '', ap.get('number', '')) == num_digits
                         and ap.get('lat') and ap.get('lon')]
                if fuzzy:
                    snap_d, _from_here, best_pt, projected = _pick_address_candidate(fuzzy)
                    miab_log("street", f"[StreetJump] Fuzzy match #{best_pt['number']} at ({best_pt['lat']:.5f},{best_pt['lon']:.5f})", getattr(self, "settings", None))
                    _apply_address_candidate(best_pt, projected, snap_d)
                    best_lat, best_lon = self.lat, self.lon
                    number_found = True
                    resolved_house_number = str(best_pt.get('number') or house_number).strip()
                else:
                    wanted_int = int(num_digits)
                    numeric = []
                    for ap in on_street:
                        digits = re.sub(r'[^0-9]', '', ap.get('number', ''))
                        if not digits or not ap.get('lat') or not ap.get('lon'):
                            continue
                        numeric.append((abs(int(digits) - wanted_int), int(digits), ap))
                    if numeric:
                        _gap, _num, best_pt = min(numeric, key=lambda item: (item[0], item[1]))
                        projected = _nearest_on_selected_street(best_pt['lat'], best_pt['lon'])
                        snap_d = projected[2] if projected else float("inf")
                        miab_log("street", f"[StreetJump] No exact match for #{house_number}; nearest known "
                              f"number is #{best_pt['number']} at ({best_pt['lat']:.5f},{best_pt['lon']:.5f})", getattr(self, "settings", None))
                        _apply_address_candidate(best_pt, projected, snap_d)
                        best_lat, best_lon = self.lat, self.lon
                        number_found = True
                        resolved_house_number = str(best_pt.get('number') or "").strip()
                        _speak(f"Number {house_number} not found. Jumping to nearest known number, "
                               f"{resolved_house_number} {street_name}.")
                    else:
                        miab_log("street", f"[StreetJump] No match for #{house_number} on '{street_name}'", getattr(self, "settings", None))
                        _speak(f"Number {house_number} not found. Jumping to nearest part of {street_name}.")
            else:
                miab_log("street", f"[StreetJump] No match for #{house_number} on '{street_name}'", getattr(self, "settings", None))
                _speak(f"Number {house_number} not found. Jumping to nearest part of {street_name}.")

        # Centre the movement barrier on the jumped position so arrow keys
        # work immediately. Don't invalidate the cache — the street data is
        # already loaded for this suburb.
        self._road_fetch_lat = self.lat
        self._road_fetch_lon = self.lon
        self._jump_street_pin_lat = self.lat
        self._jump_street_pin_lon = self.lon
        if house_number and number_found:
            self._jump_address_number = resolved_house_number or house_number.strip()
            self._jump_address_street = street_name
        else:
            self._jump_address_number = None
            self._jump_address_street = None

        wx.CallAfter(self.map_panel.set_position, self.lat, self.lon, True, street_name)

        # If in walking mode, snap to nearest intersection on this street
        if getattr(self, '_walking_mode', False) and self._walk_graph:
            nid = self._walk_find_nearest_node(best_lat, best_lon, street_filter=street_name)
            if nid is None:
                nid = self._walk_find_nearest_node(best_lat, best_lon)
            if nid and nid in self._walk_graph["intersections"]:
                nodes = self._walk_graph["nodes"]
                self.lat, self.lon = nodes[nid]
                self._walk_node = nid
                self._walk_street = street_name
                self._walk_browsing = False
                for neighbour, sname in self._walk_graph["edges"].get(nid, []):
                    if sname == street_name:
                        self._walk_heading = self._walk_bearing(nid, neighbour)
                        break
                wx.CallAfter(self.map_panel.set_position, self.lat, self.lon, True, street_name)
                desc = self._walk_describe_intersection(nid, street_name, self._walk_heading)
                addr_prefix = f"{self._jump_address_number} " if house_number and number_found else ""
                self._announce_transient(f"Jumped to {addr_prefix}{street_name}.  {desc}")
                return

        addr_prefix = f"{self._jump_address_number} " if house_number and number_found else ""
        _nr, _nc = self._nearest_road(self.lat, self.lon)
        miab_log("snap",
                 f"_jump_to_street: landed ({self.lat:.5f},{self.lon:.5f}); "
                 f"nearest_road='{_nr}' cross='{_nc}'; pin=({self._jump_street_pin_lat},{self._jump_street_pin_lon})",
                 self.settings)
        self._announce_transient(f"Jumped to {addr_prefix}{street_name}.")
        wx.CallAfter(self.map_panel.set_position, self.lat, self.lon, True, street_name)
        wx.CallAfter(self._update_street_display)

    def _explore_poi(self):
        """Enter on a top-level explorable POI — drill into its child elements."""
        if not self._poi_list:
            self._announce_transient_then_return("No points of interest loaded.")
            return
        self._sync_poi_selection_from_listbox()
        poi = self._poi_list[self._poi_index]
        if not poi.get("explorable"):
            self._announce_and_restore_poi_list("No exploration available for this location.")
            return
        name = poi["label"].split(",")[0]
        self._announce_and_restore_poi_list(f"Exploring {name}...", delay_ms=1800)
        threading.Thread(
            target=self._run_explore,
            args=(poi["osm_type"], poi["osm_id"], poi["lat"], poi["lon"], name),
            daemon=True).start()

    def _explore_back(self):
        """Backspace — pop back to previous POI list."""
        if not self._poi_explore_stack:
            self._announce_transient_then_return("Already at top level POI list.")
            return
        self._poi_list, self._poi_index = self._poi_explore_stack.pop()
        depth = len(self._poi_explore_stack)
        self._announce_and_restore_poi_list(
            f"Back.  {len(self._poi_list)} items.  "
            + ("Press Backspace to go up again." if depth > 0 else "Top level POI list."),
            delay_ms=250)

    def _run_explore(self, osm_type, osm_id, centre_lat, centre_lon, parent_name):
        """Fetch child POIs inside an explorable venue. Delegates to PoiFetcher."""
        wx.CallAfter(self._status_update, f"Loading contents of {parent_name}...", True)
        try:
            children = self._poi_fetcher.fetch_explore_children(
                osm_type, osm_id, centre_lat, centre_lon
            )
            if not children:
                wx.CallAfter(self._announce_and_restore_poi_list,
                    f"No accessible POIs found inside {parent_name}.")
                return
            wx.CallAfter(self._push_explore, children, parent_name)
        except Exception as e:
            miab_log("errors", f"[Explore] error: {e}", getattr(self, "settings", None))
            wx.CallAfter(self._announce_and_restore_poi_list,
                f"Could not load {parent_name}. Server may be busy.")
    def _push_explore(self, child_pois, parent_name):
        """Switch to child POI list, saving current list on stack."""
        self._poi_explore_stack.append((list(self._poi_list), self._poi_index))
        self._poi_list  = child_pois
        self._poi_index = 0
        self._show_poi_in_listbox()
        n_osm = sum(1 for p in child_pois if p.get("osm_type") != "scraped")
        n_scraped = sum(1 for p in child_pois if p.get("osm_type") == "scraped")
        total = len(child_pois)
        if n_scraped > 0:
            source = f"{n_osm} from map data, {n_scraped} from store directory"
        else:
            source = f"{total} locations"
        # Keep the listbox visible here so arrow-key browsing speaks reliably.

    def _street_confirm_jump(self):
        """Enter key in street mode — always jump to the selected POI."""
        if not (self._poi_list and self._poi_index < len(self._poi_list)):
            self._announce_transient_then_return("No point of interest selected.")
            return True
        self._sync_poi_selection_from_listbox()
        self._jump_to_poi()
        return True

    def _street_confirm_explore(self):
        """Ctrl+Enter — explore selected POI. Transit POIs get GTFS lookup; others show OSM tags."""
        if not (self._poi_list and self._poi_index < len(self._poi_list)):
            self._announce_transient_then_return("No point of interest selected.")
            return True
        self._sync_poi_selection_from_listbox()
        poi = self._poi_list[self._poi_index]
        # Transit POI handling
        is_transit = TransitLookup.is_transit_poi(poi)
        if is_transit:
            name = poi["label"].split(",")[0]
            self._status_update(f"Loading transit routes near {name}...")
            threading.Thread(target=self._explore_transit_poi,
                             args=(poi,), daemon=True).start()
            return True
        if self._poi_explore_stack:
            return True
        # Airports and airport terminals — offer the official-source amenity guide.
        is_airport, name, query, website = self._airport_poi_info(poi)
        if is_airport:
            lat = poi.get("lat", self.lat)
            lon = poi.get("lon", self.lon)
            ask_item = [{
                "label":         f"Show airport amenity guide — {name}",
                "lat":           lat,
                "lon":           lon,
                "kind":          "sentinel",
                "sentinel_type": "ask_airport_amenities",
                "_airport_name": name,
                "_airport_query": query,
                "_airport_website": website,
            }]
            self._poi_explore_stack.append((list(self._poi_list), self._poi_index))
            self._poi_list = ask_item
            self._poi_index = 0
            self._show_poi_in_listbox()
            self.listbox.SetFocus()
            return True
        # Shopping centres — intercept regardless of explorable flag
        # (OSM shopping centres are often nodes which don't get explorable=True)
        if poi.get("kind", "").lower() in ("mall", "shopping centre", "department store"):
            name     = poi["label"].split(",")[0].strip()
            address  = (poi.get("address") or poi.get("addr") or "").strip()
            website  = (poi.get("website") or
                         (poi.get("tags") or {}).get("website") or
                         (poi.get("tags") or {}).get("contact:website") or "").strip()
            lat      = poi["lat"]
            lon      = poi["lon"]
            ask_item = [{
                "label":         f"Show store directory — {name}",
                "lat":           lat,
                "lon":           lon,
                "kind":          "sentinel",
                "sentinel_type": "ask_shopping",
                "_centre_name":  name,
                "_centre_address": address,
                "_centre_website": website,
            }]
            self._poi_explore_stack.append((list(self._poi_list), self._poi_index))
            self._poi_list  = ask_item
            self._poi_index = 0
            self._show_poi_in_listbox()
            self.listbox.SetFocus()
            return True

        if poi.get("explorable"):
            self._explore_poi()
            return True

        return True

    def _open_poi_website(self):
        """Ctrl+W — open the website of the currently selected POI in the browser."""
        focused = wx.Window.FindFocus()
        list_active = (
            getattr(self, '_poi_list', [])
            and (focused == self.listbox or focused == self)
        )
        if (not list_active or self._poi_index >= len(self._poi_list)):
            if self._open_current_street_poi_website():
                return
            self._announce_transient_then_return("No point of interest selected.")
            return
        self._sync_poi_selection_from_listbox()
        poi = self._poi_list[self._poi_index]
        self._open_poi_website_for(poi)

    def _open_current_street_poi_website(self):
        """Open the current street-survey POI website when POI names are enabled."""
        if not self.street_mode:
            return False
        if self._street_survey_address_announce_mode() not in ("poi_names", "poi_only"):
            return False
        poi = getattr(self, "_street_survey_current_poi", None)
        if not poi:
            poi = self._current_street_survey_poi()
        if not poi:
            return False
        name = (poi.get("name") or poi.get("label") or "point of interest").split(",")[0].strip()
        self._open_poi_website_for(poi)
        miab_log("feature_usage", f"Ctrl+W opened current street POI website for {name!r}", self.settings)
        return True

    # Browser-like UA — some sites reject the default urllib agent outright.
    _WEB_CHECK_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) "
                     "Chrome/126.0.0.0 Safari/537.36")

    def _open_poi_website_for(self, poi):
        """Open the POI's website, validating it first. If it 404s or the domain
        is dead, fall back to the venue's real homepage via the keyless search
        proxy, then to a Google search as a last resort. All network work runs
        off the UI thread."""
        self._open_verified_website_for(poi)

    def _open_verified_website_for(
        self,
        poi: dict,
        name: str = "",
        url: str = "",
        location_hint: str = "",
    ) -> bool:
        """Verify and open a POI-like item's website via the shared resolver."""
        poi, name, url, location_hint = self._website_request_parts(
            poi, name=name, url=url, location_hint=location_hint)
        self._status_update(f"Checking website for {name}...")
        threading.Thread(
            target=self._resolve_and_open_website,
            args=(poi, name, location_hint, url), daemon=True).start()
        return True

    def _announce_verified_website_for(
        self,
        poi: dict,
        name: str = "",
        url: str = "",
        location_hint: str = "",
        announce_cb=None,
    ) -> bool:
        """Verify and announce a POI-like item's website without opening it."""
        poi, name, url, location_hint = self._website_request_parts(
            poi, name=name, url=url, location_hint=location_hint)
        announce = announce_cb or self._poi_detail_announce
        if poi.get("_resolved_website_verified"):
            cached = poi.get("_resolved_website")
            announce(cached or "No website available.")
            return True

        self._status_update(f"Checking website for {name}...")

        def _work():
            resolved, had_listed_url = self._resolve_website_candidate(
                poi, name, location_hint, url)
            poi["_resolved_website"] = resolved
            poi["_resolved_website_verified"] = True
            if resolved:
                msg = resolved
            elif had_listed_url:
                msg = (
                    "The listed website appears to be offline, "
                    "and no working alternative was found."
                )
            else:
                msg = "No website available."
            wx.CallAfter(announce, msg)

        threading.Thread(target=_work, daemon=True).start()
        return True

    def _website_request_parts(
        self,
        poi: dict,
        name: str = "",
        url: str = "",
        location_hint: str = "",
    ) -> tuple[dict, str, str, str]:
        """Extract the standard website resolver inputs from any POI-like dict."""
        poi = poi if isinstance(poi, dict) else {}
        name = (
            name or poi.get("name") or poi.get("label", "") or "place"
        ).split(",")[0].strip() or "place"
        if not url:
            tags = poi.get("tags") or {}
            url = (
                poi.get("website") or tags.get("website")
                or tags.get("contact:website") or poi.get("url")
                or tags.get("url") or ""
            ).strip()
        location_hint = (location_hint or self._poi_location_hint(poi)).strip()
        return poi, name, url, location_hint

    def _website_status(self, url: str, timeout: float = 7.0) -> str:
        """Best-effort liveness check. Returns 'dead' only on a clear signal
        (404/410, or a domain that does not resolve); 'live'/'unknown' otherwise.
        Transient errors stay 'unknown' so a real site is never discarded."""
        import socket
        import urllib.error
        headers = {"User-Agent": self._WEB_CHECK_UA}

        def _probe(method):
            req = urllib.request.Request(url, method=method, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout):
                return "live"

        try:
            return _probe("HEAD")
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 410):
                return "dead"
            if exc.code in (401, 403, 405, 501):
                # HEAD blocked or auth-walled — retry GET to learn the truth.
                try:
                    return _probe("GET")
                except urllib.error.HTTPError as exc2:
                    return "dead" if exc2.code in (404, 410) else "unknown"
                except Exception:
                    return "unknown"
            return "unknown"
        except urllib.error.URLError as exc:
            if isinstance(getattr(exc, "reason", None), socket.gaierror):
                return "dead"   # domain does not resolve
            return "unknown"
        except Exception:
            return "unknown"

    def _poi_location_hint(self, poi: dict) -> str:
        """Return the best local search hint we have for a POI."""
        tags = poi.get("tags") or {}
        address = (
            poi.get("address") or poi.get("addr") or tags.get("addr:full") or ""
        ).strip()
        if address:
            return address
        for key in ("addr:suburb", "addr:city", "addr:town", "addr:village"):
            value = (tags.get(key) or "").strip()
            if value:
                return value
        for key in ("suburb", "city", "town", "village"):
            value = (poi.get(key) or "").strip()
            if value:
                return value
        return (getattr(self, "_current_suburb", "") or "").strip()

    def _find_homepage_via_search(self, poi: dict, name: str, location_hint: str) -> str:
        """Find the venue's own homepage via the keyless search proxy, reusing
        the same domain-matching the menu lookup uses. Returns a root URL or ''."""
        if not getattr(self, "_serper", None) or not self._serper.is_configured:
            return ""
        distinctive, compact = self._venue_name_tokens(name)
        query = " ".join(p for p in (name, location_hint) if p)
        for item in self._serper.search(query, num=10):
            link = (item.get("url") if isinstance(item, dict) else "") or ""
            host = self._result_host(link)
            if not host:
                continue
            if any(host == d or host.endswith("." + d) for d in self._DELIVERY_HOSTS):
                continue
            if self._label_matches_venue(self._main_label(host), distinctive, compact):
                parts = urllib.parse.urlsplit(link)
                homepage = f"{parts.scheme}://{parts.netloc}/"
                status = self._website_status(homepage, timeout=5.0)
                miab_log("api_calls", f"[Website] search candidate {homepage} -> {status}", getattr(self, "settings", None))
                if status != "dead":
                    return homepage
        return ""

    def _resolve_website_candidate(
        self,
        poi: dict,
        name: str,
        location_hint: str,
        url: str,
    ) -> tuple[str, bool]:
        """Resolve a working venue website. Returns (url, had_listed_url)."""
        miab_log("api_calls", f"[Website] resolving for {name!r}: tagged url={url!r}", getattr(self, "settings", None))
        had_listed_url = bool((url or "").strip())
        # No tagged website — ask HERE once for one.
        if (not url and self.settings.get("here_api_key", "").strip()
                and not poi.get("_here_checked")):
            try:
                detail = self._here.fetch_poi_detail(
                    name, poi.get("lat", self.lat), poi.get("lon", self.lon))
                poi.update(detail)
                poi["_here_checked"] = True
                url = (detail.get("website") or "").strip()
                had_listed_url = bool(url)
            except Exception:
                pass
        if url and not url.startswith(("http://", "https://")):
            url = "https://" + url

        # Validate; drop a dead website so the search fallbacks take over.
        if url:
            status = self._website_status(url)
            miab_log("api_calls", f"[Website] {name!r}: {url} -> {status}", getattr(self, "settings", None))
            if status == "dead":
                miab_log("api_calls", f"[Website] {url} is dead — searching for the homepage", getattr(self, "settings", None))
                url = ""

        # No (valid) website — find the real homepage via the search proxy.
        if not url:
            url = self._find_homepage_via_search(poi, name, location_hint)

        return url, had_listed_url

    def _resolve_and_open_website(self, poi: dict, name: str, location_hint: str, url: str) -> None:
        """Background worker: resolve a usable website then open it. Marshals all
        UI/browser actions back to the main thread via wx.CallAfter."""
        url, _had_listed_url = self._resolve_website_candidate(
            poi, name, location_hint, url)
        if url:
            wx.CallAfter(self._open_url_and_announce, url, f"Opening {url}")
            return
        query = " ".join(p for p in (name, location_hint) if p)
        search_url = "https://www.google.com/search?q=" + urllib.parse.quote(query)
        wx.CallAfter(self._open_url_and_announce, search_url,
                     f"No website found — opening Google search for {query}.")

    def _open_url_and_announce(self, url: str, msg: str = "") -> None:
        """Open a URL in the browser from the UI thread and announce the result."""
        import webbrowser
        try:
            if webbrowser.open(url) is False:
                raise RuntimeError("browser refused URL")
            self._status_update(msg or f"Opening {url}")
        except Exception:
            self._status_update("Could not open the website in a browser.")

    def _explore_transit_poi(self, poi):
        """Background: load transit data. Collects all routes across all nearby
        stops and presents them directly — skipping the intermediate stop level."""
        name = poi["label"].split(",")[0].strip()
        def status(msg):
            wx.CallAfter(self._status_update, msg)

        # Play looping alarm while GTFS feed may need downloading
        alarm_path = r"c:\windows\media\alarm09.wav"
        try:
            self.sound.play_file(alarm_path, loops=-1)
        except Exception:
            pass

        _primary, stops = self._transit.nearby_stops(
            poi["lat"], poi["lon"], radius=200, status_cb=status)

        if not stops:
            status(f"Coordinate search found nothing — trying name match for {name}…")
            _primary, stops = self._transit.find_stops_by_name(
                name, poi["lat"], poi["lon"])

        # Stop alarm regardless of outcome
        try:
            self.sound.stop()
        except Exception:
            pass

        if not stops:
            if self._transit.is_major_station(poi):
                wx.CallAfter(self._push_transit_routes, [], name, poi)
            else:
                wx.CallAfter(self._status_update, f"No transit stops found near {name}.")
            return

        # Collect all routes across all nearby stops, deduped by (route_id, feed_id)
        seen_routes: set = set()
        child_pois  = []
        for s in stops[:20]:
            stop_id   = s["stop_id"]
            feed_id   = s["_feed_id"]
            stop_name = s["name"]
            routes_here = self._transit.routes_for_stop(stop_id, feed_id)
            # If this stop is a named train platform, treat all its routes as trains
            is_train_platform = "platform" in stop_name.lower()

            for r in routes_here:
                key = (r["route_id"], feed_id)
                if key in seen_routes:
                    continue
                seen_routes.add(key)
                long  = r["long"].strip()  if r["long"]  else ""
                short = r["short"].strip() if r["short"] else ""
                if long and short and short.lower() not in long.lower():
                    rname = f"{long} ({short})"
                else:
                    rname = long or short
                headsign, times = self._transit.next_departures(stop_id, r["route_id"], feed_id)
                # If no headsign from departures, get one from route_stops so
                # Enter still works even when no more services run today
                if not headsign:
                    fallback_stops = self._transit.stops_for_route(r["route_id"], feed_id)
                    if fallback_stops:
                        # Pick headsign from route_stops keys for this route
                        data = self._transit._feeds.get(feed_id, {})
                        for (rid, hs) in data.get("route_stops", {}):
                            if rid == r["route_id"] and hs:
                                headsign = hs
                                break
                rtype = "train" if is_train_platform else r["type"]
                extra = ""
                if headsign:
                    extra += f" — towards {headsign}"
                if times:
                    extra += f" — next: {', '.join(times)}"
                child_pois.append({
                    "label":             f"{rtype}: {rname}{extra} — press Enter for stops",
                    "lat":               poi["lat"],
                    "lon":               poi["lon"],
                    "kind":              "_transit_route",
                    "_route_id":         r["route_id"],
                    "_feed_id":          feed_id,
                    "_route_name":       f"{rtype} {rname}",
                    "_origin_stop_name": stop_name,
                    "_headsign":         headsign,
                })

        wx.CallAfter(self._push_transit_routes, child_pois, name, poi)

    def _push_transit_routes(self, child_pois, parent_name, orig_poi):
        """Push route list onto explore stack, with Mistral option for major stations."""
        if orig_poi is not None and self._transit.is_major_station(orig_poi):
            child_pois.append({
                "label":      "Ask Mistral for long-distance services…",
                "lat":        orig_poi["lat"],
                "lon":        orig_poi["lon"],
                "kind":       "_ask_mistral",
                "_poi_name":  parent_name,
            })
        self._poi_explore_stack.append((list(self._poi_list), self._poi_index))
        self._poi_list  = child_pois
        self._poi_index = 0
        self._poi_index = 0
        # Count excludes the Mistral sentinel if one was added
        Mistral_added = (orig_poi is not None and 
                        self._transit.is_major_station(orig_poi))
        n = len(child_pois) - (1 if Mistral_added else 0)
        if getattr(self, "_hub_transit_mode", False):
            self._hub_transit_mode = False
            wx.CallAfter(self._show_transit_dialog, child_pois, parent_name, n)
        else:
            self._show_poi_in_listbox()
            self._transit_nav_announce(
                f"{n} routes near {parent_name}.  "
                f"Arrow to browse, Enter to see stop sequence, Backspace to go back.")

    def _show_transit_drill_dialog(self, child_pois, title, hint, focus_index=0):
        """Show a transit drill level as a modal dialog.

        ShowModal blocks until EndModal is called:
          ID_OK     = Enter  -> drill into item
          ID_CANCEL = Back   -> return to caller
          ID_ABORT  = Escape -> close everything
        """
        labels = [p["label"] for p in child_pois]
        dlg = wx.Dialog(self, title=title,
                        style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        vs = wx.BoxSizer(wx.VERTICAL)
        lb = wx.ListBox(dlg, choices=labels, style=wx.LB_SINGLE)
        lb.SetMinSize((500, 280))
        if labels:
            lb.SetSelection(min(focus_index, len(labels) - 1))
            lb.EnsureVisible(min(focus_index, len(labels) - 1))
        vs.Add(lb, 1, wx.EXPAND | wx.ALL, 8)
        vs.Add(wx.StaticText(dlg, label=hint), 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        dlg.SetSizer(vs)
        dlg.Fit()
        dlg.CentreOnScreen()
        dlg._lb = lb
        wx.CallAfter(lb.SetFocus)
        self._transit_drill_back_one_level = False

        lb.Bind(wx.EVT_LISTBOX_DCLICK, lambda e: dlg.EndModal(wx.ID_OK))

        def _hook(evt):
            kc   = evt.GetKeyCode()
            primary = _primary_down(evt)
            alt  = evt.AltDown()
            _log_key_event(self, evt, "transit-drill", f"title={title!r}")
            # Ctrl+Alt+F — find food along this transit line (works from any
            # level of the drill dialog, including the stop-sequence view)
            if primary and alt and kc in (ord('F'), ord('f')):
                active = getattr(self, "_active_transit_route", None)
                if active:
                    threading.Thread(
                        target=self._tool_find_food_transit_line,
                        args=(active,),
                        daemon=True,
                    ).start()
                else:
                    self._announce_transient_then_return(
                        "No active transit route — open a route first.")
                return
            if kc == wx.WXK_BACK:
                idx = lb.GetSelection()
                if 0 <= idx < len(child_pois):
                    kind = child_pois[idx].get("kind", "")
                    if kind in ("_leaf", "_transit_stop_seq", "_mistral_stop_seq"):
                        self._transit_drill_back_one_level = True
                dlg.EndModal(wx.ID_CANCEL)
                return
            if kc in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
                dlg.EndModal(wx.ID_OK)
                return
            if kc == wx.WXK_ESCAPE:
                self._suppress_map_focus_repeat(800)
                dlg.EndModal(wx.ID_ABORT)
                return
            evt.Skip()

        lb.Bind(wx.EVT_KEY_DOWN, _hook)
        dlg.Bind(wx.EVT_CHAR_HOOK, _hook)
        dlg.Bind(wx.EVT_CLOSE, lambda e: dlg.EndModal(wx.ID_ABORT))

        self._transit_drill_modal_open = True
        self._active_transit_drill_dlg = dlg
        self._active_transit_drill_items = child_pois
        miab_log("verbose", f"Transit modal open: title={title!r} items={len(child_pois)}", self.settings)
        try:
            while True:
                result = dlg.ShowModal()
                idx    = lb.GetSelection()

                if result == wx.ID_ABORT:
                    dlg.Destroy()
                    self._poi_list = []
                    self._poi_index = 0
                    self._poi_explore_stack = []
                    return

                if result == wx.ID_CANCEL or idx == wx.NOT_FOUND:
                    dlg.Destroy()
                    if self._transit_drill_back_one_level:
                        self._transit_drill_back_one_level = False
                        return "back"
                    return

                poi  = child_pois[idx]
                kind = poi.get("kind", "")

                # Leaf — nothing to drill into, just loop back
                if kind in ("_leaf", "_transit_stop_seq", "_mistral_stop_seq"):
                    continue

                # Get times sentinel
                if kind == "sentinel" and poi.get("sentinel_type") == "get_times":
                    op = poi.get("operator", "")
                    svc = poi.get("service", "")
                    rn  = poi.get("route_name", "")
                    self._status_update(f"Fetching timetable for {op} {svc}...")
                    def _fetch_t(op=op, svc=svc, rn=rn):
                        text = self._mistral.ask_times(op, svc, rn)
                        wx.CallAfter(self._show_transit_drill_dialog,
                                     [{"label": text, "kind": "_leaf"}],
                                     f"{op} {svc} timetable",
                                     "Backspace to go back  |  Escape to close")
                    threading.Thread(target=_fetch_t, daemon=True).start()
                    continue

                # GTFS route -> stop sequence
                if kind == "_transit_route":
                    route_id   = poi.get("_route_id")
                    feed_id    = poi.get("_feed_id")
                    route_name = poi.get("_route_name", "route")
                    if not route_id or not feed_id:
                        continue
                    stops = self._transit.stops_for_route(
                        route_id, feed_id, headsign=poi.get("_headsign", ""))
                    if not stops:
                        self._status_update(f"No stop sequence for {route_name}.")
                        continue
                    # Stash raw GTFS stops so Ctrl+Alt+F can find food along the line
                    self._active_transit_route = {"name": route_name, "stops": stops}
                    origin = poi.get("_origin_stop_name", "").lower().strip()
                    def _b(s):
                        s = re.sub(r",?\s*platform\s*\w+", "", s,
                                   flags=re.IGNORECASE).strip()
                        for sf in (" station"," stop"," halt",
                                   " busway"," ferry terminal"," wharf"):
                            if s.endswith(sf):
                                s = s[:-len(sf)].strip()
                        return s
                    ob = _b(origin)
                    sp = []; fi = 0; matched = False
                    for si, s in enumerate(stops):
                        sn = s["name"]
                        pl = s["platform"]
                        ps = (f"  platform {pl}"
                              if pl and f"platform {pl}".lower()
                              not in sn.lower() else "")
                        if ob and (_b(sn.lower().strip()) == ob or
                                   ob in _b(sn.lower().strip()) or
                                   _b(sn.lower().strip()) in ob):
                            sp.append({"label": f"YOU ARE HERE: {sn}{ps}",
                                       "kind": "_leaf",
                                       "lat": poi["lat"], "lon": poi["lon"]})
                            fi = si; matched = True
                        else:
                            sp.append({"label": f"{sn}{ps}", "kind": "_leaf",
                                       "lat": poi["lat"], "lon": poi["lon"]})
                    if ob and not matched:
                        sp.insert(0, {
                            "label": f"(Note: {ob.title()} not in this route)",
                            "kind": "_leaf",
                            "lat": poi["lat"], "lon": poi["lon"]})
                        fi = 0
                    back = self._show_transit_drill_dialog(
                        sp,
                        f"{route_name} — {len(sp)} stops",
                        "Backspace to go back  |  Escape to close",
                        focus_index=fi)
                    if back == "back":
                        continue
                    continue

                # Mistral service -> stops + sentinels
                if kind == "_mistral_service":
                    op  = poi.get("_operator", "")
                    svc = poi.get("_service", "")
                    rn  = poi.get("_route_name", "")
                    sts = poi.get("_stops", [])
                    lat = poi.get("lat", 0); lon = poi.get("lon", 0)
                    sp = [{"label": s, "kind": "_leaf", "lat": lat, "lon": lon}
                          for s in sts if isinstance(s, str) and s.strip()]
                    sp.append({
                        "label": f"Get times for {op} {svc}",
                        "kind": "sentinel", "sentinel_type": "get_times",
                        "operator": op, "service": svc,
                        "route_name": rn, "lat": lat, "lon": lon})
                    if len(sts) >= 2:
                        parts = rn.split(" to ", 1)
                        rev = (f"{parts[1]} to {parts[0]}"
                               if len(parts) == 2 else rn)
                        sp.append({
                            "label": f"Reverse: {rev}",
                            "kind": "_mistral_service",
                            "_operator": op, "_service": svc,
                            "_route_name": rev,
                            "_stops": list(reversed(sts)),
                            "lat": lat, "lon": lon})
                    desc = f"{svc} — {rn}" if rn else svc
                    back = self._show_transit_drill_dialog(
                        sp,
                        f"{op}: {desc}",
                        "Enter for timetable  |  Backspace to go back  |  Escape to close")
                    if back == "back":
                        continue
                    continue

                # Ask Mistral for long-distance services
                if kind == "_ask_mistral":
                    self._hub_transit_mode = True
                    self._explore_mistral_transit(poi)
                    continue
        finally:
            self._transit_drill_modal_open = False
            self._active_transit_drill_dlg = None
            self._active_transit_drill_items = []
            miab_log("verbose", f"Transit modal close: title={title!r}", self.settings)

    def _show_transit_dialog(self, child_pois, parent_name, n):
        """Wrapper — shows routes level via the drill dialog."""
        self._show_transit_drill_dialog(
            child_pois,
            title=f"{parent_name} — {n} route(s)",
            hint="Enter for stop sequence  |  Backspace to go back  |  Escape to close",
            focus_index=0,
        )

    def _explore_transit_route(self, poi):
        """Enter on a transit route — push ordered stop sequence as next child level."""
        route_id   = poi.get("_route_id")
        feed_id    = poi.get("_feed_id")
        route_name = poi.get("_route_name", "route")
        if not route_id or not feed_id:
            return
        headsign = poi.get("_headsign", "")
        stops = self._transit.stops_for_route(route_id, feed_id, headsign=headsign)
        if not stops:
            self._status_update(f"No stop sequence available for {route_name}.")
            return
        child_pois = []
        origin = poi.get("_origin_stop_name", "").lower().strip()
        focus_index = 0

        def _bare(s):
            """Strip platform numbers, common transit suffixes for fuzzy matching."""
            # Strip ", platform N" or " platform N" anywhere
            s = re.sub(r',?\s*platform\s*\w+', '', s, flags=re.IGNORECASE).strip()
            # Strip trailing transit words
            for suffix in (" station", " stop", " halt",
                           " busway", " ferry terminal", " wharf"):
                if s.endswith(suffix):
                    s = s[:-len(suffix)].strip()
            return s

        origin_bare = _bare(origin)
        for i, s in enumerate(stops):
            sname = s['name']
            plat = s['platform']
            # Only append platform if it's not already embedded in the stop name
            if plat and f"platform {plat}".lower() not in sname.lower():
                platform = f"  platform {plat}"
            else:
                platform = ""
            sname_bare = _bare(sname.lower().strip())
            if origin_bare and (sname_bare == origin_bare or
                                origin_bare in sname_bare or
                                sname_bare in origin_bare):
                label = f"YOU ARE HERE: {sname}{platform}"
                focus_index = i
                miab_log("navigation", f"[Transit] YOU ARE HERE matched '{sname}' for origin '{origin}'", getattr(self, "settings", None))
            else:
                label = f"{sname}{platform}"
            child_pois.append({
                "label": label,
                "lat":   poi["lat"],
                "lon":   poi["lon"],
                "kind":  "_transit_stop_seq",
            })
        if focus_index == 0 and origin_bare:
            all_names = [_bare(s['name'].lower().strip()) for s in stops[:5]]
            miab_log("navigation", f"[Transit] No YOU ARE HERE match for '{origin_bare}'. First 5: {all_names}", getattr(self, "settings", None))
        # Stash raw GTFS stops (with real coords) so Ctrl+Alt+F can query food nearby
        self._active_transit_route = {"name": route_name, "stops": stops}

        self._poi_explore_stack.append((list(self._poi_list), self._poi_index))
        self._poi_list  = child_pois
        self._poi_index = focus_index
        self._show_poi_in_listbox()
        self._transit_nav_announce(
            f"{len(child_pois)} stops on {route_name}.  "
            f"Arrow to browse.  Backspace to go back.")

    def _explore_mistral_transit(self, poi: dict) -> None:
        """Background: call Mistral and push a flat route list.

        Level 1 — flat list of routes: "Operator — Service — Route name"
        Level 2 — stops for that route + Get times sentinel at bottom
        """
        name         = poi.get("_poi_name", poi["label"].split(",")[0].strip())
        display_name = name  # coords in the prompt provide geographic context
        lat          = poi["lat"]
        lon          = poi["lon"]

        done_event = threading.Event()

        def _progress():
            msgs = [
                f"Searching for regional routes at {name}…",
                "Checking Greyhound, regional trains, ferries…",
                "Searching operator websites…",
                "Processing results…",
                "Almost there…",
            ]
            for msg in msgs:
                if done_event.wait(timeout=5):
                    return
                wx.CallAfter(_speak, msg)
        threading.Thread(target=_progress, daemon=True).start()

        try:
            self.sound.play_file(r"c:\windows\media\alarm09.wav", loops=-1)
        except Exception:
            pass

        routes = self._mistral.ask_transit(lat, lon, display_name)
        done_event.set()  # stop progress thread before touching the listbox
        try:
            self.sound.stop()
        except Exception:
            pass

        if not routes:
            wx.CallAfter(self._status_update,
                        f"Mistral found no regional services at {name}.")
            return

        child_pois = []
        for r in routes:
            operator   = r.get("operator",   "")
            service    = r.get("service",    "")
            route_name = r.get("route_name", "")
            stops      = r.get("stops",      [])
            label = " — ".join(p for p in [operator, service, route_name] if p)
            child_pois.append({
                "label":       label,
                "lat":         lat,
                "lon":         lon,
                "kind":        "_mistral_service",
                "_operator":   operator,
                "_service":    service,
                "_route_name": route_name,
                "_stops":      stops,
            })

        # Small delay so any in-flight progress CallAfters drain before we push results
        import time as _time
        _time.sleep(0.05)
        wx.CallAfter(self._push_mistral_flat, child_pois, name)

    def _push_mistral_flat(self, child_pois: list, parent_name: str) -> None:
        """Push flat Mistral route list — dialog if hub mode, listbox otherwise."""
        if getattr(self, "_hub_transit_mode", False):
            self._hub_transit_mode = False
            self._show_transit_drill_dialog(
                child_pois,
                title=f"Mistral: {parent_name} — {len(child_pois)} route(s)",
                hint="Enter for stops  |  Escape to close",
                focus_index=0,
            )
            return
        self._poi_explore_stack.append((list(self._poi_list), self._poi_index))
        self._poi_list  = child_pois
        self._poi_index = 0
        self._show_poi_in_listbox()
        self._transit_nav_announce(
            f"Mistral found {len(child_pois)} regional route(s) at {parent_name}.  "
            f"Arrow to browse, Enter for stops, Backspace to go back.")

    def _explore_mistral_service(self, poi: dict) -> None:
        """Enter on a route — show its stops, Get times, and reverse direction sentinel."""
        operator   = poi.get("_operator",   "")
        service    = poi.get("_service",    "")
        route_name = poi.get("_route_name", "")
        stops      = poi.get("_stops",      [])
        lat        = poi.get("lat", 0)
        lon        = poi.get("lon", 0)

        child_pois = []
        for stop in stops:
            if not isinstance(stop, str) or not stop.strip():
                continue
            child_pois.append({
                "label": stop,
                "lat":   lat,
                "lon":   lon,
                "kind":  "_mistral_stop_seq",
            })

        child_pois.append({
            "label":         f"Get times for {operator} {service}",
            "lat":           lat,
            "lon":           lon,
            "kind":          "sentinel",
            "sentinel_type": "get_times",
            "operator":      operator,
            "service":       service,
            "route_name":    route_name,
        })

        # Reverse direction — free, just reverse the stops list
        if len(stops) >= 2:
            parts = route_name.split(" to ", 1)
            rev_name = f"{parts[1]} to {parts[0]}" if len(parts) == 2 else route_name
            child_pois.append({
                "label":       f"Reverse: {rev_name}",
                "lat":         lat,
                "lon":         lon,
                "kind":        "_mistral_service",
                "_operator":   operator,
                "_service":    service,
                "_route_name": rev_name,
                "_stops":      list(reversed(stops)),
            })

        self._poi_explore_stack.append((list(self._poi_list), self._poi_index))
        self._poi_list  = child_pois
        self._poi_index = 0
        self._show_poi_in_listbox()
        desc = f"{service} — {route_name}" if route_name else service
        self._transit_nav_announce(
            f"{operator}: {desc}.  "
            f"{len(stops)} stop(s).  "
            f"Arrow to browse, Backspace to go back.")

    def _transit_nav_announce(self, msg):
        """Announce transit navigation context via AO2, then restore POI list focus."""
        _speak(msg)
        wx.CallLater(800, self._transit_nav_focus)

    def _transit_nav_focus(self):
        """Restore focus to current POI item after a transit nav announcement."""
        if not self._poi_list:
            return
        self._show_poi_in_listbox()
        self.listbox.SetFocus()

    def _restore_poi_listbox(self):
        """Restore POI listbox after a status message — called via CallLater."""
        if self._poi_list:
            self._show_poi_in_listbox()
            self.listbox.SetFocus()

    def _announce_poi_crossing(self):
        """Space in street mode with POIs loaded — fetch nearest intersection for current POI."""
        if not self._poi_list:
            return
        poi = self._poi_list[self._poi_index]
        name = poi["label"].split(",")[0]
        self._announce_and_restore_poi_list(f"Finding nearest intersection for {name}...")
        threading.Thread(target=self._fetch_poi_intersection,
                         args=(poi["lat"], poi["lon"], name,
                               poi.get("street", "")), daemon=True).start()

    def _suppress_poi_entry(self, poi: dict, name: str | None = None) -> None:
        """Persist a local POI suppression entry for the given POI dict."""
        name = (name or poi.get("name") or poi.get("label") or "POI").split(",")[0].strip()
        suppressed = _load_suppressed()
        suppressed.append({
            "name":     name.lower(),
            "lat":      round(float(poi.get("lat", 0)), 4),
            "lon":      round(float(poi.get("lon", 0)), 4),
            "kind":     poi.get("kind", ""),
            "source":   poi.get("source", "osm"),
            "reported": json.dumps({"t": time.time()}),
        })
        _save_suppressed(suppressed)

    def _rename_poi_entry(self, poi: dict, new_name: str, old_name: str | None = None) -> tuple[dict, list]:
        """Persist a local POI rename and return the updated POI plus rename table."""
        old_name = (old_name or poi.get("name") or poi.get("label") or "POI").split(",")[0].strip()
        new_name = (new_name or "").strip()
        if not new_name:
            return dict(poi), _load_renamed()

        renamed = _load_renamed()
        plat = round(float(poi.get("lat", 0)), 4)
        plon = round(float(poi.get("lon", 0)), 4)
        renamed = [r for r in renamed
                   if not (r.get("old_name", "").lower() == old_name.lower()
                           and abs(r.get("lat", 0) - plat) < 0.0002
                           and abs(r.get("lon", 0) - plon) < 0.0002)]
        renamed.append({
            "old_name": old_name.lower(),
            "new_name": new_name,
            "lat":      plat,
            "lon":      plon,
            "kind":     poi.get("kind", ""),
            "source":   poi.get("source", "osm"),
        })
        _save_renamed(renamed)

        updated = dict(poi)
        updated["name"] = new_name
        old_label = updated.get("label", "")
        if old_label:
            updated["label"] = old_label.replace(old_label.split(",")[0], new_name, 1)
        return updated, renamed

    def _report_poi_nonexistent(self):
        """Delete key — confirm, suppress locally, and optionally post OSM note."""
        if not self._poi_list or self._poi_index >= len(self._poi_list):
            return
        self._sync_poi_selection_from_listbox()
        poi  = self._poi_list[self._poi_index]
        name = poi["label"].split(",")[0].strip()

        dlg = wx.MessageDialog(
            self,
            f"Are you sure '{name}' no longer exists?",
            "Report Missing POI",
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION,
        )
        if dlg.ShowModal() != wx.ID_YES:
            dlg.Destroy()
            self.listbox.SetFocus()
            return
        dlg.Destroy()

        # ── Option 1: local suppress ──────────────────────────────────
        self._suppress_poi_entry(poi, name)

        # Remove from current list immediately
        self._poi_list.pop(self._poi_index)
        self._poi_index = max(0, self._poi_index - 1)
        if self._poi_list:
            self._show_poi_in_listbox()
            wx.CallAfter(self.listbox.SetFocus)
        else:
            self._show_mode_surface(focus=True)
            wx.CallAfter(_speak, "No more points of interest.")

        # ── Option 2: OSM note (only for OSM-sourced POIs with an ID) ─
        osm_id   = poi.get("osm_id", 0)
        osm_type = poi.get("osm_type", "node")
        source   = poi.get("source", "osm")

        def _post_note():
            try:
                note_text = (
                    f"This POI may no longer exist: {name}"
                    + (f" ({poi.get('kind', '')})" if poi.get("kind") else "")
                    + (f" [OSM {osm_type}/{osm_id}]" if osm_id else "")
                    + " — reported via Map in a Box accessibility app."
                )
                params = urllib.parse.urlencode({
                    "lat":  poi["lat"],
                    "lon":  poi["lon"],
                    "text": note_text,
                })
                req = urllib.request.Request(
                    "https://api.openstreetmap.org/api/0.6/notes",
                    data=params.encode(),
                    headers={"User-Agent": "MapInABox/1.0"},
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    miab_log("api_calls", f"[OSM Note] Posted for '{name}': HTTP {resp.status}", None)
                wx.CallAfter(self._status_update,
                    f"'{name}' reported to OpenStreetMap.")
            except Exception as e:
                miab_log("errors", f"[OSM Note] Failed: {e}", None)
                wx.CallAfter(self._status_update,
                    f"OSM report failed for '{name}'.")
            finally:
                wx.CallLater(2000, self._restore_poi_listbox)

        if source == "osm":
            threading.Thread(target=_post_note, daemon=True).start()
        else:
            self._status_update(f"'{name}' suppressed locally.")
            wx.CallLater(2000, self._restore_poi_listbox)

    def _rename_poi(self):
        """F2 with POI list open — rename the selected POI locally and notify OSM."""
        if not self._poi_list or self._poi_index >= len(self._poi_list):
            return
        self._sync_poi_selection_from_listbox()
        poi      = self._poi_list[self._poi_index]
        old_name = (poi.get("name") or poi.get("label") or "").split(",")[0].strip()

        dlg = wx.TextEntryDialog(
            self,
            f"New name for '{old_name}':",
            "Rename POI",
            old_name,
        )
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            self.listbox.SetFocus()
            return
        new_name = dlg.GetValue().strip()
        dlg.Destroy()

        if not new_name or new_name.lower() == old_name.lower():
            self.listbox.SetFocus()
            return

        # ── Save to renamed_pois.json ─────────────────────────────────
        poi, renamed = self._rename_poi_entry(poi, new_name, old_name)

        # Update in current list immediately
        self._poi_list[self._poi_index] = poi
        self._show_poi_in_listbox()
        wx.CallAfter(self.listbox.SetFocus)

        # Also update in _all_pois if present
        self._all_pois = _apply_renames(
            getattr(self, "_all_pois", []), renamed)
        try:
            self._free_engine.set_pois(self._all_pois)
        except Exception:
            pass

        # ── Post OSM note if OSM-sourced ──────────────────────────────
        source  = poi.get("source", "osm")
        osm_id  = poi.get("osm_id", 0)
        osm_type = poi.get("osm_type", "node")

        def _post_note():
            try:
                note_text = (
                    f"This POI may have been renamed: '{old_name}' is now '{new_name}'"
                    + (f" ({poi.get('kind', '')})" if poi.get("kind") else "")
                    + (f" [OSM {osm_type}/{osm_id}]" if osm_id else "")
                    + " — reported via Map in a Box accessibility app."
                )
                params = urllib.parse.urlencode({
                    "lat":  poi["lat"],
                    "lon":  poi["lon"],
                    "text": note_text,
                })
                req = urllib.request.Request(
                    "https://api.openstreetmap.org/api/0.6/notes",
                    data=params.encode(),
                    headers={"User-Agent": "MapInABox/1.0"},
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    miab_log("api_calls", f"[OSM Note] Rename posted for '{old_name}': HTTP {resp.status}", None)
                wx.CallAfter(self._status_update,
                    f"Renamed to '{new_name}' and reported to OpenStreetMap.")
            except Exception as e:
                miab_log("errors", f"[OSM Note] Rename report failed: {e}", None)
                wx.CallAfter(self._status_update,
                    f"Renamed to '{new_name}' locally. OSM report failed.")
            finally:
                wx.CallLater(2000, self._restore_poi_listbox)

        if source == "osm":
            threading.Thread(target=_post_note, daemon=True).start()
        else:
            self._status_update(f"Renamed to '{new_name}' locally.")
            wx.CallLater(2000, self._restore_poi_listbox)

    def _toggle_map_fullscreen(self):
        """F9 — toggle the shared Windows/macOS Visual Assist presentation."""
        self._map_fullscreen = not self._map_fullscreen
        status = ""
        if self._map_fullscreen:
            self._map_was_maximized = self.IsMaximized()
            self.Maximize(True)
            self._map_sizer_item.SetProportion(999)
            self._list_sizer_item.SetProportion(1)
            self._list_sizer_item.SetMinSize((1, -1))
            self._info_sizer_item.SetProportion(0)
            self._info_sizer_item.SetMinSize((1, -1))
            self.info_panel.Hide()
            status = "Visual Assist mode on."
        else:
            self._map_sizer_item.SetProportion(3)
            self._list_sizer_item.SetProportion(1)
            self._list_sizer_item.SetMinSize((-1, -1))
            self.info_panel.Show()
            self._info_sizer_item.SetProportion(1)
            self._info_sizer_item.SetMinSize((250, -1))
            if not getattr(self, "_map_was_maximized", False):
                self.Maximize(False)
            status = "Visual Assist mode off."
        self.map_panel.set_classroom_mode(self._map_fullscreen)
        self._h_sizer.Layout()
        self.map_panel.Refresh()
        self.listbox.SetFocus()
        if IS_MAC:
            wx.CallLater(180, self._status_update, status, True)
        else:
            self._status_update(status, force=True)

    def _spatial_tone_bounds(self):
        """Return tone-normalisation bounds for the selected spatial tone mode."""
        mode = self.settings.get("spatial_tones_mode", "world")
        if mode == "city":
            mode = "region"
        if mode == "world":
            return None
        try:
            _, idx = _nearest_city(self._city_lats, self._city_lons, self.lat, self.lon)
            row = self.df.iloc[idx]
        except Exception:
            return None

        def _clean(value):
            value = str(value or "").strip()
            return "" if value.lower() == "nan" else value

        country = _clean(row.get("country", ""))
        region = _clean(row.get("admin_name", ""))
        if not country:
            return None
        cache_key = (mode, country, region)
        cache = getattr(self, "_spatial_tone_bounds_cache", {})
        if cache_key in cache:
            return cache[cache_key]

        def _remember(bounds):
            self._spatial_tone_bounds_cache = cache
            cache[cache_key] = bounds
            return bounds

        def _expanded_bounds(rows, min_lat_span, min_lon_span):
            if rows is None or rows.empty:
                return None
            min_lat = float(rows["lat"].min())
            max_lat = float(rows["lat"].max())
            min_lon = float(rows["lng"].min())
            max_lon = float(rows["lng"].max())
            unwrapped_lon = False
            if max_lon - min_lon > 180.0:
                lons = rows["lng"].apply(lambda x: float(x) + 360.0 if float(x) < 0 else float(x))
                min_lon = float(lons.min())
                max_lon = float(lons.max())
                unwrapped_lon = True
            center_lat = max(min(self.lat, 90.0), -90.0)
            center_lon = max(min(self.lon, 180.0), -180.0)
            if unwrapped_lon and center_lon < 0.0:
                center_lon += 360.0
            if max_lat - min_lat < min_lat_span:
                half = min_lat_span / 2.0
                min_lat = center_lat - half
                max_lat = center_lat + half
            if max_lon - min_lon < min_lon_span:
                half = min_lon_span / 2.0
                min_lon = center_lon - half
                max_lon = center_lon + half
            return (
                max(-90.0, min_lat),
                min(90.0, max_lat),
                min_lon if unwrapped_lon else max(-180.0, min_lon),
                max_lon if unwrapped_lon else min(180.0, max_lon),
            )

        if mode == "country":
            rows = self.df[self.df["country"] == country]
            return _remember(_expanded_bounds(rows, 2.0, 2.0))

        if mode == "region":
            if not region:
                rows = self.df[self.df["country"] == country]
                return _remember(_expanded_bounds(rows, 2.0, 2.0))
            rows = self.df[
                (self.df["country"] == country)
                & (self.df["admin_name"] == region)
            ]
            return _remember(_expanded_bounds(rows, 0.5, 0.5))

        return None

    def _cycle_spatial_tones_mode(self, step: int) -> None:
        """Cycle map spatial tones between world, country, and region."""
        modes = ["world", "country", "region"]
        current = self.settings.get("spatial_tones_mode", "world")
        if current not in modes:
            current = "world"
        idx = modes.index(current)
        new_mode = modes[(idx + step) % len(modes)]
        self.settings["spatial_tones_mode"] = new_mode
        save_settings(self.settings)
        self._status_update(f"Spatial tones: {new_mode.title()}.", force=True)
        miab_log("feature_usage", f"Spatial tones mode set to {new_mode}", self.settings)

    def _play_challenge_position_tone(self, lat, lon):
        """Play the normal map-position tone while challenge mode is active."""
        if not getattr(self, "sounds_enabled", True):
            return
        self._play_spatial_tone_if_allowed(lat, lon, self._spatial_tone_bounds())

    def _current_map_place(self):
        """Return current coordinates and a readable nearest-place label.

        In street mode uses the current street label for a precise address;
        falls back to nearest city in map mode.
        """
        coords = (float(self.lat), float(self.lon))
        if self.street_mode:
            # Use the displayed street label (respects the jump pin) rather than
            # calling nearest_road directly, which ignores the pin.
            label = self.street_label
            if not label or label in ("", "Unknown", "No street data", "No street data nearby"):
                label, _ = self._nearest_road(self.lat, self.lon)
            suburb = getattr(self, "_current_suburb", "") or ""
            if label and label not in ("", "Unknown", "No street data", "No street data nearby"):
                pinned_num = getattr(self, "_jump_address_number", None)
                pinned_street = getattr(self, "_jump_address_street", None)
                pin_lat = getattr(self, "_jump_street_pin_lat", None)
                pin_lon = getattr(self, "_jump_street_pin_lon", None)
                pin_active = (
                    pinned_num and pinned_street
                    and pin_lat is not None and pin_lon is not None
                    and pinned_street.lower() == label.lower()
                    and dist_metres(self.lat, self.lon, pin_lat, pin_lon) <= 150.0
                )
                num = pinned_num if pin_active else self._nearest_address_number(
                    self.lat, self.lon, label, radius=200)
                addr = f"{num} {label}" if num else label
                name = f"{addr}, {suburb}" if suburb else addr
                return coords, name
        try:
            _, idx = _nearest_city(self._city_lats, self._city_lons, self.lat, self.lon)
            row = self.df.iloc[idx]
            parts = []
            for p in [str(row["city"]), str(row["admin_name"]), str(row["country"])]:
                if p and p.lower() != "nan" and p not in parts:
                    parts.append(p)
            name = ", ".join(parts) if parts else "current position"
        except Exception:
            name = "current position"
        return coords, name

    def _prompt_mark_slot(self, remove=False, coords=None, name=None):
        """Ask for mark slot 1-3 and apply immediately on number press."""
        title = "Remove Mark" if remove else "Store Mark"
        prompt = "Remove mark 1, 2, or 3." if remove else "Store mark 1, 2, or 3."
        result_msg = None
        dlg = wx.Dialog(self, title=title, style=wx.DEFAULT_DIALOG_STYLE)
        panel = wx.Panel(dlg)
        sizer = wx.BoxSizer(wx.VERTICAL)
        label = wx.StaticText(panel, label=prompt)
        sizer.Add(label, 0, wx.ALL, 12)
        cancel = wx.Button(panel, wx.ID_CANCEL, "Cancel")
        sizer.Add(cancel, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        panel.SetSizer(sizer)
        dlg.Fit()
        dlg.CentreOnParent()

        def _finish(slot):
            nonlocal result_msg
            if remove:
                marks = getattr(self, "_map_marks", {})
                if slot in marks:
                    del marks[slot]
                    result_msg = f"mark {slot} removed"
                else:
                    result_msg = f"mark {slot} not set"
            else:
                mark_coords = coords
                mark_name = name
                if mark_coords is None or mark_name is None:
                    mark_coords, mark_name = self._current_map_place()
                self._mark_coords(slot, mark_coords, mark_name, announce=False)
                result_msg = f"mark {slot} set to {mark_name}"
            dlg.EndModal(wx.ID_OK)

        def _hook(event):
            code = event.GetKeyCode()
            if code in (wx.WXK_ESCAPE,):
                dlg.EndModal(wx.ID_CANCEL)
                return
            numpad = {
                getattr(wx, "WXK_NUMPAD1", None): 1,
                getattr(wx, "WXK_NUMPAD2", None): 2,
                getattr(wx, "WXK_NUMPAD3", None): 3,
            }
            slot = numpad.get(code)
            if slot is None:
                char = chr(code) if 0 <= code < 256 else ""
                slot = int(char) if char in ("1", "2", "3") else None
            if slot:
                _finish(slot)
                return
            self._status_update("Press 1, 2, or 3.", force=True)

        cancel.Bind(wx.EVT_BUTTON, lambda e: dlg.EndModal(wx.ID_CANCEL))
        dlg.Bind(wx.EVT_CHAR_HOOK, _hook)
        wx.CallAfter(panel.SetFocus)
        dlg.ShowModal()
        dlg.Destroy()
        if result_msg:
            self._announce_after_map_focus(result_msg)
        else:
            self._return_focus_to_map(repeat=True)

    def _set_map_destination_from_coords(self, coords, name, announce=True):
        self._map_destination = {"coords": (float(coords[0]), float(coords[1])), "name": name}
        if announce:
            self._status_update(f"Destination set to {name}.", force=True)

    def _set_route_destination_from_coords(self, coords, name, announce=True):
        self._find_food_destination = {"coords": (float(coords[0]), float(coords[1])), "name": name}
        self._set_map_destination_from_coords(coords, name, announce=announce)

    def _confirm_exit_street_mode(self, prompt, repeat_location=False):
        """Ask to leave street mode before doing something that only makes
        sense on the world map (jumping, starting the challenge game).
        Returns True if it's now safe to proceed - either street mode
        wasn't active, or the user agreed to exit it. Returns False if the
        user declined, in which case the caller should just stop."""
        if not self.street_mode:
            return True
        dlg = wx.MessageDialog(
            self, prompt, "Exit Street Mode",
            wx.YES_NO | wx.NO_DEFAULT)
        if dlg.ShowModal() != wx.ID_YES:
            dlg.Destroy()
            self._return_focus_to_map(repeat=True)
            return False
        dlg.Destroy()
        self._exit_street_mode(repeat_location=repeat_location)
        return True

    def _confirm_exit_street_mode_for_jump(self, repeat_location=False):
        return self._confirm_exit_street_mode(
            "Exit street mode and jump to a new location?",
            repeat_location=repeat_location)

    def _mark_coords(self, slot, coords, name, announce=True):
        self._map_marks[slot] = {"coords": (float(coords[0]), float(coords[1])), "name": name}
        if announce:
            self._status_update(f"mark {slot} set to {name}", force=True)

    def _jump_to_saved_mark(self):
        marks = getattr(self, "_map_marks", {})
        slots = []
        choices = []
        for slot in (1, 2, 3):
            mark = marks.get(slot)
            if not mark:
                continue
            choices.append(f"Mark {slot}: {mark.get('name', 'current position')}")
            slots.append(slot)

        if not slots:
            self._announce_after_map_focus("No marks set. Press Ctrl+M then 1, 2, or 3.")
            return
        if not self._confirm_exit_street_mode_for_jump():
            return

        if len(slots) == 1:
            slot = slots[0]
        else:
            dlg = wx.SingleChoiceDialog(self, "Choose mark to jump to:", "Jump to Mark", choices)
            if dlg.ShowModal() != wx.ID_OK:
                dlg.Destroy()
                self._return_focus_to_map(repeat=True)
                return
            slot = slots[dlg.GetSelection()]
            dlg.Destroy()

        mark = marks.get(slot)
        if not mark:
            self._announce_after_map_focus(f"Mark {slot} not set.")
            return
        try:
            lat, lon = mark["coords"]
            lat = float(lat)
            lon = float(lon)
        except (TypeError, ValueError, KeyError):
            self._announce_after_map_focus(f"Mark {slot} has no valid position.")
            return

        name = mark.get("name", f"mark {slot}")
        label = f"mark {slot}, {name}"
        self.lat = lat
        self.lon = lon
        self.street_label = "" if self.street_mode else self.street_label
        self._jump_street_label = None
        self._jump_street_pin_lat = None
        self._jump_street_pin_lon = None
        self._jump_address_number = None
        self._jump_address_street = None
        self.last_location_str = name
        self._set_current_location_title(name)
        self._last_jump_display_label = label
        self._last_jump_display_until = time.time() + 1.5
        miab_log("navigation", f"Jump to mark {slot}: {name} ({lat:.3f}, {lon:.3f})", self.settings)
        self._record_jump(label, lat, lon)
        wx.CallAfter(self.map_panel.set_position, self.lat, self.lon,
                     self.street_mode, self.street_label)
        threading.Thread(target=self._lookup, daemon=True).start()
        self._return_focus_to_map(repeat=True, delay_ms=250)

    def _mark_pairwise_entries(self):
        entries = []
        marks = getattr(self, "_map_marks", {})
        for slot in (1, 2, 3):
            mark = marks.get(slot) or marks.get(str(slot))
            if not mark:
                continue
            coords = mark.get("coords") or ()
            if len(coords) != 2:
                continue
            try:
                coords = (float(coords[0]), float(coords[1]))
            except (TypeError, ValueError):
                continue
            name = re.split(
                r"\.\s{2,}|[,;]",
                str(mark.get("name") or f"mark {slot}"),
                1,
            )[0].strip()
            entries.append((slot, name or f"mark {slot}", coords))
        return entries

    def _report_all_mark_distances(self, return_focus=True):
        entries = self._mark_pairwise_entries()
        if len(entries) < 2:
            msg = "Set at least two marks to compare distances."
            if return_focus:
                self._announce_after_map_focus(msg)
            else:
                self._announce_transient(msg)
            return

        parts = []
        for i, (_slot_a, name_a, coords_a) in enumerate(entries):
            for _slot_b, name_b, coords_b in entries[i + 1:]:
                dist_str, _direction = self._format_mark_distance(coords_a, coords_b)
                parts.append(f"{name_a} to {name_b}: {dist_str}.")
        msg = " ".join(parts)
        if return_focus:
            self._announce_after_map_focus(msg)
        else:
            self._announce_transient(msg)

    def _announce_mark(self, slot, return_focus=True):
        def _say(msg):
            if return_focus:
                self._announce_after_map_focus(msg)
            else:
                self._announce_transient(msg)

        mark = getattr(self, "_map_marks", {}).get(slot)
        if not mark:
            _say(f"Mark {slot} not set.")
            return
        coords = mark.get("coords") or ()
        name = mark.get("name", f"mark {slot}")
        if len(coords) != 2:
            _say(str(name))
            return
        try:
            lat, lon = float(coords[0]), float(coords[1])
            origin = (float(self.lat), float(self.lon))
        except (TypeError, ValueError):
            _say(str(name))
            return

        place_name = re.split(r"\.\s{2,}|[,;]", str(name or f"mark {slot}"), 1)[0].strip()
        current_name = self._last_landed_object_label()
        dist_str, _direction = self._format_mark_distance(origin, (lat, lon))
        if current_name:
            msg = f"{place_name} is {dist_str} from {current_name}."
        else:
            msg = f"{place_name} is {dist_str} from here."
        _say(msg)

    def _format_mark_distance(self, origin, target):
        km = dist_km(origin[0], origin[1], target[0], target[1])
        dist_str = format_distance(km * 1000)
        direction = compass_name(
            bearing_deg(origin[0], origin[1], target[0], target[1])
        ).lower()
        return dist_str, direction

    def _map_display_mode_name(self, mode=None):
        mode = mode or getattr(self, "map_display_mode", "world")
        return {
            "world": "World view",
            "country": "Country view",
        }.get(mode, "World view")

    def _cycle_map_display_mode(self):
        modes = ("world", "country")
        current = getattr(self, "map_display_mode", "world")
        try:
            idx = modes.index(current)
        except ValueError:
            idx = -1
        new_mode = modes[(idx + 1) % len(modes)]
        self.map_display_mode = new_mode
        if hasattr(self, "map_panel"):
            self.map_panel.Refresh()
        self._announce_transient(self._map_display_mode_name(new_mode))
        return new_mode

    def _flash_current_country(self):
        """F8 — flash the current country name and highlight its polygon on the map."""
        country = getattr(self, 'last_country_found', '')
        if not country or country == 'Open Water':
            return False
        # Find matching entry in _GEO_COUNTRIES
        c_lower = country.lower()
        match = None
        for c in _GEO_COUNTRIES:
            if c['name'].lower() == c_lower:
                match = c
                break
        # Fuzzy fallback
        if not match:
            for c in _GEO_COUNTRIES:
                if c_lower in c['name'].lower() or c['name'].lower() in c_lower:
                    match = c
                    break
        if match:
            self.map_panel.set_flash(
                match['name'],
                match['rings_idx'],
                match['centroid_lon'],
                match['centroid_lat'],
            )
        else:
            # Country not in GeoJSON — just flash the name at current position
            self.map_panel.set_flash(country, [], self.lon, self.lat)
        return True

    def toggle_sounds(self):
        self.sounds_enabled = not getattr(self, 'sounds_enabled', True)
        if self.sounds_enabled:
            self._status_update("Sounds on.", force=True)
            self.sound._current = None
            self.last_country_found = ""
            threading.Thread(target=self._lookup, daemon=True).start()

        else:
            self.sound._ch.fadeout(500)
            self.sound._current = None
            self._status_update("Sounds off.", force=True)

    def on_close(self, event):
        if getattr(self, "_shutdown_pending", False):
            return
        self._shutdown_pending = True
        _speak("Exiting.", interrupt=True)
        wx.CallLater(150, self._finish_shutdown)
        if event is not None:
            try:
                event.Veto()
            except Exception:
                pass

    def _finish_shutdown(self):
        if self.settings.get("clear_favourites_on_exit", EDUCATION_EDITION):
            self._clear_favourites_and_personal_pois()
        if hasattr(self, "_geo_features"):
            self._geo_features.cleanup_temp()
        pygame.quit()
        self.Destroy()
        os._exit(0)

    def _clear_favourites_and_personal_pois(self):
        """Wipe favourites.json and personal_pois.json (settings-gated)."""
        try:
            save_favourites([])
        except Exception:
            pass
        try:
            _save_personal_pois([])
        except Exception:
            pass
        self._personal_pois = []

    def _status_update(self, msg, force=False):
        """Transient background status (loading, connecting) — AO2 only."""
        msg_text = str(msg)
        if getattr(self, "_update_dialog_active", False):
            self._verbose_trace(f"status suppressed while update dialog is active: {msg!r}")
            return
        if (not force
                and time.time() < getattr(self, '_suppress_status_until', 0)
                and not str(msg).startswith("Looking up address")
                and not getattr(self, '_address_lookup_in_progress', False)):
            self._verbose_trace(f"status suppressed: {msg!r}")
            return
        terminal = msg_text.strip().lower()
        if (terminal.rstrip(".! ").endswith("cancelled")
                and getattr(self, "_tool_cancel_already_announced", False)):
            self._tool_cancel_already_announced = False
            return
        if force and (
                terminal.startswith(("no ", "not found", "nothing ",
                                     "could not ", "can't ", "cannot "))
                or terminal.rstrip(".! ").endswith("cancelled")):
            self._announce_transient_then_return(msg_text)
            return
        _speak(msg)

    def _map_sound_allowed(self) -> bool:
        """True when map-driven ambient/spatial sounds may play."""
        return (getattr(self, "sounds_enabled", True)
                and not getattr(self, "_update_dialog_active", False))

    def _play_location_sound_if_allowed(self, country, continent="") -> None:
        if not self._map_sound_allowed():
            self._verbose_trace("location sound suppressed while update dialog is active.")
            return
        self.sound.play_location_sound(country, continent)

    def _play_spatial_tone_if_allowed(self, lat, lon, bounds=None) -> None:
        if not self._map_sound_allowed():
            self._verbose_trace("spatial tone suppressed while update dialog is active.")
            return
        self.sound.play_spatial_tone(lat, lon, bounds)

    def _emit_speech(self, text, braille_text=None, interrupt: bool = True,
                     second_braille: bool = True) -> None:
        """Speak + braille through the shared dispatcher."""
        self.speech.emit(text, braille_text, interrupt, second_braille)

    def _announce_transient(self, msg, braille_msg=None) -> None:
        """Speak and braille a transient announcement without touching the listbox."""
        if getattr(self, "_update_dialog_active", False):
            self._verbose_trace(f"transient suppressed while update dialog is active: {msg!r}")
            return
        self.speech.transient(msg, braille_msg)

    def _announce_transient_then_return(self, msg, delay_ms=3000, focus_target=None) -> None:
        """Speak through AO2, then restore focus without changing MSAA text."""
        if getattr(self, "_update_dialog_active", False):
            self._verbose_trace(f"transient-return suppressed while update dialog is active: {msg!r}")
            return
        self._transient_return_generation = getattr(
            self, "_transient_return_generation", 0) + 1
        generation = self._transient_return_generation
        speech_delay_ms = 300
        self._transient_message_active_until = (
            time.time()
            + (speech_delay_ms + max(1, int(delay_ms))) / 1000.0)
        return_to_poi_list = bool(getattr(self, "_poi_list", []))

        def _speak_after_map_focus():
            if generation == getattr(self, "_transient_return_generation", None):
                self._announce_transient(str(msg))

        wx.CallLater(speech_delay_ms, _speak_after_map_focus)

        def _return_after_message():
            if generation != getattr(self, "_transient_return_generation", None):
                return
            self._transient_message_active_until = 0.0
            if getattr(self, "_update_dialog_active", False):
                return
            try:
                focused = wx.Window.FindFocus()
                if (focused is not None
                        and focused.GetTopLevelParent() is not self):
                    return
            except Exception:
                pass
            target = focus_target
            if target is not None:
                try:
                    if target.IsShown() and not target.HasFocus():
                        target.SetFocus()
                    return
                except Exception:
                    pass
            if return_to_poi_list and getattr(self, "_poi_list", []):
                self._show_poi_in_listbox()
                self.listbox.SetFocus()
            else:
                self._show_mode_surface(
                    self._map_focus_fallback_label(), focus=True)

        try:
            wx.CallLater(
                speech_delay_ms + max(1, int(delay_ms)),
                _return_after_message)
        except Exception:
            try:
                self._show_mode_surface(
                    self._map_focus_fallback_label(), focus=True)
            except Exception:
                pass

    def _announce_after_map_focus(self, msg, delay_ms=350) -> None:
        """Return map focus first, then speak so the frame title cannot cut in."""
        if getattr(self, "_update_dialog_active", False):
            self._verbose_trace(f"map-focus announcement suppressed while update dialog is active: {msg!r}")
            return
        self._return_focus_to_map(repeat=False)
        wx.CallLater(delay_ms, lambda: self._announce_transient(msg))

    def _suppress_map_focus_repeat(self, duration_ms: int = 800) -> None:
        """Briefly suppress automatic F2-style repeats during internal UI flow."""
        until = time.time() + max(0, duration_ms) / 1000.0
        self._suppress_focus_repeat_until = max(
            until, getattr(self, "_suppress_focus_repeat_until", 0.0))

    def _map_focus_repeat_allowed(self) -> bool:
        """True only when focus has really settled back on the map surface."""
        if getattr(self, "_update_dialog_active", False):
            return False
        if time.time() < getattr(self, "_suppress_focus_repeat_until", 0.0):
            return False
        if getattr(self, "_suppress_location_restore", False):
            return False
        if getattr(self, "_thinking_active", False):
            return False
        if getattr(self, "_tools_workflow_active", False):
            return False
        if getattr(self, "_find_food_populating", False):
            return False
        if not self._last_landed_object_label():
            return False
        try:
            focused = wx.Window.FindFocus()
        except Exception:
            focused = None
        if focused is None:
            return True
        if focused in (self, self.listbox):
            return True
        try:
            return focused.GetTopLevelParent() is self
        except Exception:
            return False

    def _verbose_trace(self, msg: str) -> None:
        """Write a verbose trace when diagnostics are enabled."""
        try:
            settings = getattr(self, "settings", None) or {}
            if settings.get("logging", {}).get("verbose", False):
                miab_log("verbose", msg, settings)
        except Exception:
            pass

    def _on_listbox_focus(self, event):
        event.Skip()

    def update_ui(self, msg, force=False):
        """Update the visible status line and keep the info panel current.

        Repeated identical updates are still spoken; silence can look like a
        screen-reader freeze while navigating. Suppressed only during modal
        restore flows or while the user is browsing a POI list.
        """
        msg_text = str(msg)
        if getattr(self, "_update_dialog_active", False):
            self._verbose_trace(f"update_ui suppressed while update dialog is active: {msg!r}")
            return
        if not force and getattr(self, '_poi_explore_stack', []):
            self._verbose_trace(f"update_ui suppressed during POI browse: {msg!r}")
            return
        if not force and getattr(self, "_suppress_location_restore", False):
            self._verbose_trace(f"update_ui suppressed while location restore is active: {msg!r}")
            return
        self._verbose_trace(f"update_ui applied: {msg!r}")

        _braille(msg_text)
        self._refresh_info_panel()
        if hasattr(self, "_info_status"):
            self._set_info_label(self._info_status, msg_text)
            self.info_panel.Layout()
            self.info_panel.Refresh()
        if IS_MAC:
            self._listbox_set_single(msg_text)
        elif force:
            # On Windows the static info-panel label is not auto-announced by
            # NVDA, and the listbox is not retitled for force=True messages.
            # Forced updates (nav start, route summary, POI search status) are
            # the ones the user must hear — speak them directly.
            _speak(msg_text)

    def _announce_location(self, msg):
        """Announce the current map location via AO2.

        Uses _announce_transient on all platforms — AO2 speaks directly to
        JAWS, NVDA, or VoiceOver without touching the listbox, so there is
        no MSAA selection event and no double-speak.
        """
        msg_text = str(msg)
        if getattr(self, "_update_dialog_active", False):
            self._verbose_trace(f"_announce_location suppressed while update dialog is active: {msg!r}")
            return
        if getattr(self, "_suppress_location_restore", False):
            self._verbose_trace(f"_announce_location suppressed while location restore is active: {msg!r}")
            return
        self._announce_transient(msg_text)

    def _set_current_location_title(self, msg) -> str:
        """Track the current landed location for status/braille use.

        Deliberately does NOT touch the OS window title — Alt+Tab announces
        the title, and constantly changing it to the current place name
        (e.g. "Cleveland") meant every app-switch spoke a stray place name.
        The title stays fixed at APP_NAME; use _current_focus_location_label
        for anything that needs the last landed location.
        """
        msg_text = str(msg or "").strip()
        if not msg_text:
            return ""
        self._current_focus_location_label = msg_text
        try:
            if self.GetTitle() != APP_NAME:
                self.SetTitle(APP_NAME)
        except Exception:
            pass
        return msg_text

    def _update_location_focus(self, msg):
        """Update the focused location row, for real position changes."""
        msg_text = self._set_current_location_title(msg)
        if getattr(self, "_update_dialog_active", False):
            self._verbose_trace(f"_update_location_focus suppressed while update dialog is active: {msg!r}")
            return
        if getattr(self, "_suppress_location_restore", False):
            self._verbose_trace(f"_update_location_focus suppressed while location restore is active: {msg!r}")
            return
        if time.time() < getattr(self, "_last_jump_display_until", 0):
            # Jump result was just announced — don't overwrite it with a shorter
            # lookup string from the background _lookup thread.  The title still
            # gets updated above so Alt-Tab does not keep an old place name.
            self._verbose_trace(f"_update_location_focus suppressed within jump display window: {msg!r}")
            return
        self._verbose_trace(f"_update_location_focus applied: {msg!r}")
        self._refresh_info_panel()
        self._announce_location(msg_text)
        if getattr(self, "_poi_list", []):
            wx.CallAfter(self.listbox.SetFocus)

    def _handle_f2_tap(self):
        """F2: repeat current location. Double-tap within 0.6s: spell it
        out letter by letter. Triple-tap: copy it to the clipboard.

        Each press fires its action immediately (no waiting to see if
        another tap follows) — a rapid follow-up press just interrupts the
        prior speech with the escalated action, same as pressing F2 once
        always has done.
        """
        now = time.time()
        last_at = getattr(self, "_f2_last_tap_at", 0.0)
        tap_window = 0.6
        count = (getattr(self, "_f2_tap_count", 0) + 1) if (now - last_at) <= tap_window else 1
        self._f2_tap_count = count
        self._f2_last_tap_at = now

        if count == 1:
            self._repeat_current_location(force=True)
        elif count == 2:
            self._spell_current_location()
        else:
            self._copy_current_location_to_clipboard()
            self._f2_tap_count = 0  # next press after a triple starts fresh

    def _spell_current_location(self):
        """Double-tap F2 — spell the current location letter by letter."""
        label = self._last_landed_object_label()
        if not label:
            self._status_update("Nothing to spell.", force=True)
            return
        spelled = " ".join(ch if ch.strip() else "," for ch in label)
        self._status_update(spelled, force=True)

    def _copy_current_location_to_clipboard(self):
        """Triple-tap F2 — copy the current location to the clipboard."""
        label = self._last_landed_object_label()
        if not label:
            self._status_update("Nothing to copy.", force=True)
            return
        try:
            if wx.TheClipboard.Open():
                try:
                    wx.TheClipboard.SetData(wx.TextDataObject(label))
                finally:
                    wx.TheClipboard.Close()
                self._status_update(f"Copied: {label}", force=True)
            else:
                self._status_update("Could not access the clipboard.", force=True)
        except Exception as e:
            miab_log("errors", f"F2 clipboard copy failed: {e}", getattr(self, "settings", None))
            self._status_update("Could not copy to clipboard.", force=True)

    def _repeat_current_location(self, force=False, allow_unknown=True):
        """Repeat the last landed object through AO2 speech and braille."""
        if not force:
            focused = wx.Window.FindFocus()
            if focused != self.listbox and (IS_MAC or focused != self):
                return
        if not force and getattr(self, "_suppress_location_restore", False):
            return
        label = self._last_landed_object_label()
        if not label and not allow_unknown:
            return
        _speak(label or "Location unknown.")

    def _repeat_current_location_after_return(self, delay_ms: int = 25,
                                              require_focus: bool = True) -> None:
        """Repeat the current place after focus has settled back on the map."""
        self._location_repeat_generation = getattr(self, "_location_repeat_generation", 0) + 1
        generation = self._location_repeat_generation

        def _repeat_if_current():
            if generation == getattr(self, "_location_repeat_generation", None):
                if require_focus and not self._map_focus_repeat_allowed():
                    return
                label = self._last_landed_object_label()
                if not label:
                    return
                now = time.time()
                last_label = getattr(self, "_last_focus_return_repeat_label", "")
                last_at = getattr(self, "_last_focus_return_repeat_at", 0.0)
                if label == last_label and now - last_at < 1.5:
                    return
                self._last_focus_return_repeat_label = label
                self._last_focus_return_repeat_at = now
                _speak(label)

        # wxOSX asserts when a one-shot timer is started with 0 ms.  Several
        # dialog/menu return paths intentionally request an immediate repeat,
        # so clamp that request to the smallest valid timer interval.
        wx.CallLater(max(1, int(delay_ms)), _repeat_if_current)

    def _force_listbox_refocus(self) -> None:
        """Force a genuine blur+focus cycle on the listbox.

        A plain self.listbox.SetFocus() is a no-op at the OS level when the
        listbox already has focus (which it usually does), so no real
        focus-changed event fires. JAWS still re-reads the object's content
        on a redundant SetFocus() call, but NVDA relies on an actual
        transition to know to re-query it — hence "works in JAWS, not
        NVDA" for mode-change announcements. Briefly moving focus to the
        frame and back creates two real transitions instead of a no-op.
        """
        if getattr(self, "_update_dialog_active", False):
            self._verbose_trace("_force_listbox_refocus suppressed while update dialog is active.")
            return
        if not (getattr(self, "_poi_list", [])
                or getattr(self, "_poi_explore_stack", [])):
            self._show_mode_surface(focus=True)
            return
        try:
            self.SetFocus()
        except Exception:
            pass
        self.listbox.SetFocus()

    def _focus_map_window_silently(self) -> None:
        """Focus the map command target through one shared path."""
        if getattr(self, "_update_dialog_active", False):
            self._verbose_trace("_focus_map_window_silently suppressed while update dialog is active.")
            return
        if time.time() < getattr(self, "_transient_message_active_until", 0.0):
            self._verbose_trace("map focus suppressed while a transient message is active.")
            return
        if (getattr(self, "_poi_list", [])
                or getattr(self, "_poi_explore_stack", [])):
            self._show_list_surface()
            self.listbox.SetFocus()
            return
        self._show_mode_surface(focus=True)

    def _map_focus_fallback_label(self) -> str:
        if (getattr(self, "street_mode", False)
                or getattr(self, "_walking_mode", False)
                or getattr(self, "_free_mode", False)):
            return "Street mode"
        return "Map mode"

    def _return_focus_to_map(self, repeat=True, delay_ms: int = 25,
                             restore_focus=True) -> None:
        """Restore map focus through one path, then optionally repeat like F2."""
        if getattr(self, "_update_dialog_active", False):
            self._verbose_trace("_return_focus_to_map suppressed while update dialog is active.")
            return
        self._map_return_generation = getattr(self, "_map_return_generation", 0) + 1
        generation = self._map_return_generation

        def _restore():
            if getattr(self, "_update_dialog_active", False):
                self._verbose_trace("delayed map focus restore suppressed while update dialog is active.")
                return
            if generation != getattr(self, "_map_return_generation", None):
                return
            if not restore_focus:
                if repeat:
                    quiet_delay = delay_ms if IS_MAC else max(delay_ms, 140)
                    self._repeat_current_location_after_return(quiet_delay)
                return
            if (getattr(self, "_poi_explore_stack", [])
                    or getattr(self, "_poi_list", [])):
                try:
                    self.listbox.SetFocus()
                except Exception:
                    pass
                return
            self._focus_map_window_silently()
            if repeat:
                quiet_delay = delay_ms if IS_MAC else max(delay_ms, 140)
                self._repeat_current_location_after_return(quiet_delay)

        wx.CallAfter(_restore)

    def _last_landed_object_label(self) -> str:
        """Return the most recent landed object without coordinates."""
        if getattr(self, "_walking_mode", False) and getattr(self, "_walk_street", None):
            label = self._walk_street
        elif getattr(self, "_free_mode", False):
            label = getattr(self._free_engine, "street_name", "") or getattr(self, "street_label", "")
        elif getattr(self, "street_mode", False):
            label = getattr(self, "_current_focus_location_label", "")
            if not label:
                number = getattr(self, "_jump_address_number", "")
                street = getattr(self, "_jump_address_street", "") or getattr(self, "street_label", "")
                if number and street:
                    label = f"{number} {street}"
                else:
                    label = street or getattr(self, "last_location_str", "")
        else:
            label = (
                getattr(self, "last_location_str", "")
                or getattr(self, "street_label", "")
                or getattr(self, "last_country_found", "")
            )
        label = str(label or "").strip()
        if not label:
            return ""
        # Keep the repeat short and object-like, e.g. "Coorparoo" instead of
        # the longer descriptive sentence that may have been announced.
        label = re.split(r"\.\s{2,}|[,;]", label, 1)[0].strip()
        return label

    def _announce_current_region(self):
        """R in map mode — speak the current state/admin region."""
        if self.lat < -60.0:
            self._status_update("Antarctica", force=True)
            return
        _dist, idx = _nearest_city(self._city_lats, self._city_lons, self.lat, self.lon)
        region, country = self._city_regions[idx]
        parts = [
            value for value in (region, country)
            if value and value.lower() != "nan"
        ]
        self._status_update(", ".join(parts) if parts else "Region unknown.", force=True)

    def _announce_current_country(self):
        """C in map mode — speak the current country."""
        if self.lat < -60.0:
            self._status_update("Antarctica", force=True)
            return
        country = getattr(self, "last_country_found", "")
        if not country:
            _dist, idx = _nearest_city(self._city_lats, self._city_lons, self.lat, self.lon)
            country = self._city_regions[idx][1]
        self._status_update(country if country else "Country unknown.", force=True)

    def _announce_nearest_city_only(self):
        """N in map mode — speak the nearest city/locality only."""
        if self.lat < -60.0:
            self._status_update("Antarctica", force=True)
            return
        try:
            self._status_update(self._nearest_city_distance_label(), force=True)
        except Exception:
            self._status_update("Nearest city unknown.", force=True)

    def _geo_features_enabled(self) -> bool:
        return bool(getattr(self, "_geo_features", None)) and self.settings.get("geo_features_enabled", True)

    def _geo_lookup_precise(self, lat: float, lon: float, country_code: str = None) -> str:
        if not self._geo_features_enabled():
            return ""
        return self._geo_features.lookup_precise_label(lat, lon, country_code)

    def _geo_lookup_any(self, lat: float, lon: float, country_code: str = None) -> str:
        if not self._geo_features_enabled():
            return ""
        return self._geo_features.lookup_any(lat, lon, country_code)

    def _geo_context_items(self, lat: float, lon: float, limit: int = 3, country_code: str = None) -> list[str]:
        if not self._geo_features_enabled():
            return []
        return self._geo_features.context_items(lat, lon, limit=limit, country_code=country_code)

    def _nearest_city_distance_label(self, lat: float = None, lon: float = None) -> str:
        """Return the nearest city name, distance and direction from a point."""
        lat = self.lat if lat is None else lat
        lon = self.lon if lon is None else lon
        dist, idx = _nearest_city(self._city_lats, self._city_lons, lat, lon)
        row = self.df.iloc[idx]
        city = str(row.get("city", "")).strip()
        if not city or city.lower() == "nan":
            city = "City unknown"
        city_lat = float(row.get("lat", lat))
        city_lon = float(row.get("lng", lon))
        km = dist * 111.0
        dist_text = format_distance(km * 1000)
        direction = compass_name(bearing_deg(lat, lon, city_lat, city_lon)).lower().replace("-", " ")
        return f"{city} {dist_text} {direction}".strip()

    def _toggle_geo_features(self):
        enabled = not self.settings.get("geo_features_enabled", True)
        self.settings["geo_features_enabled"] = enabled
        save_settings(self.settings)
        self._status_update(
            "GeoFeatures on." if enabled else "GeoFeatures off.",
            force=True,
        )
        miab_log("feature_usage",
                 f"GeoFeatures {'enabled' if enabled else 'disabled'}",
                 self.settings)


    def _refresh_background_pois(self):
        if not self.street_mode:
            self._status_update("POI refresh works in street mode.", force=True)
            return True
        if getattr(self, "_poi_fetch_in_progress", False):
            self._status_update("POI refresh already in progress.", force=True)
            return True
        if getattr(self, "_background_poi_fetch_in_progress", False):
            self._status_update("POI refresh already in progress.", force=True)
            return True
        if getattr(self, "_poi_live_fetch_in_progress", False):
            self._status_update("POI refresh already in progress.", force=True)
            return True
        cooldown_remaining = POI_LIVE_COOLDOWN_SECS - (
            time.time() - getattr(self, "_poi_live_last_completed_at", 0.0)
        )
        if cooldown_remaining > 0:
            self._status_update(
                f"POI refresh available in {math.ceil(cooldown_remaining)} seconds.",
                force=True,
            )
            return True
        self._status_update("Refreshing POIs...", force=True)
        self._poi_live_cache = {}
        threading.Thread(
            target=self._fetch_all_pois_background,
            args=(getattr(self, "_address_points", []), True),
            daemon=True,
        ).start()
        return True

    def _prefetch_background_pois(
        self,
        lat: float,
        lon: float,
        address_points=None,
        fetch_id=None,
    ) -> None:
        """Load POIs for (lat, lon), live if the disk cache is missing/stale.

        address_points is only used for optional label enrichment inside
        the fetch, never a hard dependency — so this is safe to call
        immediately when entering a new area, in parallel with the street
        data fetch, rather than waiting for the street fetch's address
        points to be ready first. That wait was the actual reason POI
        loading always started only after streets fully finished loading:
        not semaphore contention, just that nothing kicked it off any
        earlier. _fetch_all_pois_background's own _poi_fetch_in_progress
        guard makes it safe to also call this again later with fresher
        address points (see _fetch_road_data) — it just no-ops if a fetch
        from this earlier call is already running or done.
        """
        if fetch_id is not None and self._street_fetch_id != fetch_id:
            miab_log("verbose", "POI prefetch skipped — street fetch superseded.", self.settings)
            return
        if address_points is None:
            address_points = getattr(self, "_address_points", [])
        try:
            cached = self._poi_fetcher.load_cached_pois(lat, lon)
            if cached is not None:
                _suppressed = _load_suppressed()
                _renamed    = _load_renamed()
                pois = _apply_renames(
                    [p for p in cached if not _is_suppressed(p, _suppressed)],
                    _renamed)
                self._all_pois = self._merge_personal_pois(pois)
                self._poi_grid = self._build_poi_grid(self._all_pois)
                self._poi_fetch_lat = lat
                self._poi_fetch_lon = lon
                try:
                    self._free_engine.set_pois(pois)
                except Exception:
                    pass
                miab_log("verbose", f"Pre-loaded {len(pois)} POIs from cache.", self.settings)
                # Only refresh live if the cache is stale (> 6 h). Fresh
                # caches are served as-is to avoid hammering Overpass on
                # every street-mode entry.
                _age_h = self._poi_fetcher.cached_background_age_hours(lat, lon)
                if _age_h is None or _age_h > 6:
                    miab_log("verbose",
                             f"Background POI cache age {_age_h:.1f}h — refreshing live." if _age_h else
                             "Background POI cache age unknown — refreshing live.",
                             self.settings)
                    threading.Thread(
                        target=self._fetch_all_pois_background,
                        args=(address_points, False, lat, lon, fetch_id),
                        daemon=True,
                    ).start()
                else:
                    miab_log("verbose",
                             f"Background POI cache age {_age_h:.1f}h — skipping live refresh.",
                             self.settings)
            else:
                miab_log("verbose", "No disk cache — fetching live.", self.settings)
                self._fetch_all_pois_background(address_points, False, lat, lon, fetch_id)
        except Exception as exc:
            miab_log("errors", f"POI cache pre-load error: {exc}", self.settings)

    def _fetch_all_pois_background(
        self,
        address_points=None,
        force_refresh=False,
        fetch_lat=None,
        fetch_lon=None,
        fetch_id=None,
    ):
        """Background POI fetch for walk-announce. Delegates to PoiFetcher."""
        if getattr(self, "_recentring", False):
            return
        if not self.street_mode:
            return
        if fetch_id is not None and self._street_fetch_id != fetch_id:
            miab_log("verbose", "Background POI fetch skipped — street fetch superseded.", self.settings)
            return
        if getattr(self, "_background_poi_fetch_in_progress", False):
            miab_log("verbose", "Background fetch already in progress — skipping duplicate.", self.settings)
            return
        if getattr(self, "_poi_fetch_in_progress", False):
            miab_log("verbose", "User POI search already in progress — skipping background fetch.", self.settings)
            return
        if address_points is None:
            address_points = getattr(self, "_address_points", [])
        poi_lat = self.lat if fetch_lat is None else fetch_lat
        poi_lon = self.lon if fetch_lon is None else fetch_lon

        # Respect poi_source setting — only use HERE if explicitly chosen
        poi_source = self.settings.get("poi_source", "osm")
        here_key   = self.settings.get("here_api_key", "").strip()
        if poi_source == "here" and here_key:
            self._poi_fetcher.set_here_key(here_key)
        else:
            self._poi_fetcher.set_here_key("")

        try:
            self._background_poi_fetch_in_progress = True
            if (getattr(self, "_street_data_fetch_in_progress", False)
                    and not getattr(self, "_road_fetched", False)):
                miab_log(
                    "verbose",
                    "Background POI fetch yielding first Overpass slot to street fetch.",
                    self.settings,
                )
                time.sleep(1.5)
                if (not self.street_mode
                        or (fetch_id is not None and self._street_fetch_id != fetch_id)):
                    miab_log("verbose", "Background POI fetch skipped after yield — street fetch superseded.", self.settings)
                    return
            pois = self._poi_fetcher.fetch_all_background(
                poi_lat, poi_lon, address_points,
                force_refresh=force_refresh,
            )
            # Discard if street mode was cancelled while fetching
            if (not self.street_mode
                    or (fetch_id is not None and self._street_fetch_id != fetch_id)):
                miab_log(
                    "verbose",
                    "Background fetch complete but street mode cancelled or superseded — discarding.",
                    self.settings,
                )
                return
            _suppressed = _load_suppressed()
            _renamed    = _load_renamed()
            self._all_pois = self._merge_personal_pois(_apply_renames(
                [p for p in pois if not _is_suppressed(p, _suppressed)],
                _renamed))
            self._poi_grid = self._build_poi_grid(self._all_pois)
            self._poi_fetch_lat = poi_lat
            self._poi_fetch_lon = poi_lon
            try:
                self._free_engine.set_pois(self._all_pois)
            except Exception:
                pass
            miab_log(
                "verbose",
                f"Grid index: {len(self._poi_grid)} occupied cells across {len(pois)} POIs.",
                self.settings,
            )
            if getattr(self, "_street_data_fetch_in_progress", False):
                self._pending_pois_ready_sound = True
                miab_log(
                    "verbose",
                    "Background POIs ready; deferring ready sound until streets finish.",
                    self.settings,
                )
            else:
                wx.CallAfter(self._play_pois_ready_sound)
            if getattr(self, '_free_mode', False):
                wx.CallAfter(self._free_announce_poi_update)
            elif force_refresh:
                wx.CallAfter(
                    self._status_update,
                    f"POIs refreshed. {len(self._all_pois)} places loaded.",
                    True,
                )
        except Exception as e:
            miab_log("errors", f"Background POI fetch error: {e}", self.settings)
        finally:
            self._background_poi_fetch_in_progress = False

    def _free_announce_poi_update(self):
        """Announce that free-mode POIs have been refreshed."""
        if not getattr(self, "_free_mode", False):
            return
        try:
            msg = self._free_engine.describe_current()
        except Exception as exc:
            miab_log("errors", f"Free POI refresh announcement failed: {exc}", self.settings)
            return
        if msg:
            self._status_update(msg, force=True)
        elif getattr(self, "_all_pois", []):
            self._status_update(
                f"Free mode POIs refreshed. {len(self._all_pois)} places loaded.",
                force=True,
            )

    def _build_poi_grid(self, pois: list, cell_m: float = 80.0) -> dict:
        """Build a spatial grid index from a POI list.

        Each POI is bucketed into a (gx, gy) cell of size cell_m × cell_m.
        Lookup expands to enough neighbouring cells to cover the requested radius.
        Returns dict mapping (gx, gy) → list of POIs.
        """
        grid: dict = {}
        for poi in pois:
            plat = poi.get("lat")
            plon = poi.get("lon")
            if plat is None or plon is None:
                continue
            gx = int(plat * 111000 / cell_m)
            gy = int(plon * 111000 * math.cos(math.radians(plat)) / cell_m)
            key = (gx, gy)
            if key not in grid:
                grid[key] = []
            grid[key].append(poi)
        return grid

    def _poi_grid_nearby(self, lat: float, lon: float,
                         radius_m: float, cell_m: float = 80.0) -> list:
        """Return POIs within radius_m of (lat, lon) using the grid index."""
        grid = getattr(self, '_poi_grid', {})
        if not grid:
            return []
        gx = int(lat * 111000 / cell_m)
        gy = int(lon * 111000 * math.cos(math.radians(lat)) / cell_m)
        candidates = []
        span = max(1, int(math.ceil(float(radius_m) / cell_m)))
        for dx in range(-span, span + 1):
            for dy in range(-span, span + 1):
                candidates.extend(grid.get((gx + dx, gy + dy), []))
        result = []
        for poi in candidates:
            plat = poi.get("lat"); plon = poi.get("lon")
            if plat is None:
                continue
            d = math.sqrt(((lat - plat) * 111000) ** 2 +
                          ((lon - plon) * 111000 * math.cos(math.radians(lat))) ** 2)
            if d <= radius_m:
                result.append((d, poi))
        result.sort(key=lambda x: x[0])
        return [p for _, p in result]

    # ── Cross-platform system sound helpers ──────────────────────────────────

    def _play_system_sound(self, kind: str = "default") -> None:
        """Play a brief system notification sound, cross-platform.

        Parameters
        ----------
        kind:
            One of ``"default"``, ``"balloon"``, ``"asterisk"``.
            Falls back to a pygame beep if the platform-specific call
            fails (e.g. on macOS, or Windows without the WAV files).
        """
        if getattr(self, "_update_dialog_active", False):
            self._verbose_trace(f"system sound suppressed while update dialog is active: {kind!r}")
            return
        import platform
        sys_name = platform.system()

        # ── Windows ──────────────────────────────────────────────────
        if sys_name == "Windows":
            try:
                import winsound
                _WIN_SOUNDS = {
                    "balloon":  r"C:\Windows\Media\Windows Balloon.wav",
                    "default":  r"C:\Windows\Media\Windows Default.wav",
                    "asterisk": None,  # use MessageBeep
                }
                wav = _WIN_SOUNDS.get(kind, r"C:\Windows\Media\Windows Default.wav")
                if wav is None:
                    winsound.MessageBeep(winsound.MB_ICONASTERISK)
                else:
                    winsound.PlaySound(wav, winsound.SND_FILENAME | winsound.SND_ASYNC)
                return
            except Exception:
                pass  # fall through to pygame fallback

        # ── macOS ─────────────────────────────────────────────────────
        if sys_name == "Darwin":
            try:
                import subprocess
                # afplay is available on all macOS versions; /System/Library sounds
                # are present by default.
                _MAC_SOUNDS = {
                    "balloon":  "/System/Library/Sounds/Pop.aiff",
                    "default":  "/System/Library/Sounds/Funk.aiff",
                    "asterisk": "/System/Library/Sounds/Hero.aiff",
                }
                wav = _MAC_SOUNDS.get(kind, "/System/Library/Sounds/Funk.aiff")
                subprocess.Popen(
                    ["afplay", wav],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return
            except Exception:
                pass  # fall through to pygame fallback

        # ── Linux / fallback: pygame 50ms tone ────────────────────────
        try:
            sr   = 44100
            freq = {"balloon": 880.0, "default": 440.0, "asterisk": 660.0}.get(kind, 440.0)
            t    = np.linspace(0, 0.08, int(sr * 0.08), False)
            wave = np.sin(2 * np.pi * freq * t)
            fade = int(sr * 0.02)
            wave[:fade]  *= np.linspace(0, 1, fade)
            wave[-fade:] *= np.linspace(1, 0, fade)
            stereo = np.ascontiguousarray(
                np.stack([wave, wave], axis=-1) * 0.4 * 32767, dtype=np.int16)
            snd = pygame.sndarray.make_sound(stereo)
            snd.play()
        except Exception:
            pass

    def _on_loading_tick(self, event):
        """Timer tick for loading feedback (street loading only).

        POI fetches have their own dedicated "searching" sound
        (alarm09.wav, looped) rather than this heartbeat — playing both
        at once was the actual bug, not the tick itself. This guard
        restores the original behaviour: the tick stops as soon as
        street loading (self._loading) finishes, and does not also run
        during POI fetches.
        """
        if not getattr(self, '_loading', False):
            return
        if getattr(self, '_poi_fetch_in_progress', False):
            return
        if not getattr(self, 'street_mode', False):
            return

        now = time.time()
        last = getattr(self, '_last_street_loading_beep_at', 0.0)
        if now - last < 2.0:
            return
        self._last_street_loading_beep_at = now
        self.sound.play_poi_tone("both")

    def _play_pois_ready_sound(self):
        """Notification sound when background POI fetch completes."""
        miab_log("verbose", "Playing POIs-ready balloon sound.", self.settings)
        self._play_system_sound("balloon")

    def _play_roads_ready_sound(self):
        """Notification sound when road data is ready."""
        now = time.time()
        last = getattr(self, "_last_roads_ready_sound_at", 0.0)
        if now - last < 2.5:
            return
        self._last_roads_ready_sound_at = now
        miab_log("street", "[Street] Playing roads-ready sound.", self.settings)
        self._play_system_sound("default")

    def _open_city_pack_wizard(self):
        """Ctrl+Shift+F11 — pick cities/regions to bulk-prefetch in the background."""
        from city_packs import start_batch_fetch_background, is_batch_fetch_active
        if is_batch_fetch_active():
            self._announce_transient_then_return("A city data download is already in progress.")
            return
        dlg = CityPackWizardDialog(
            self, street_fetcher=self._street_fetcher, df=self.df,
            initial_country_name=getattr(self, "last_country_found", ""))
        result = dlg.ShowModal()
        packs = dlg.result_packs
        country_code = dlg.result_country_code
        dlg.Destroy()
        if hasattr(self, "_focus_map_window_silently"):
            self._focus_map_window_silently()
        else:
            self.SetFocus()
        if result == wx.ID_OK and packs:
            # Match the live navigation address source (self.settings
            # "gnaf_enabled") rather than always defaulting to GNAF -
            # otherwise a downloaded suburb's cached address_source
            # ("gnaf") wouldn't match what live navigation actually
            # requests when GNAF is off ("osm"), forcing a live address
            # re-fetch on the very next visit even though the street
            # cache itself hit fine.
            start_batch_fetch_background(
                self._street_fetcher, packs, country_code,
                use_gnaf=self.settings.get("gnaf_enabled", True),
                status_cb=lambda msg: wx.CallAfter(self._status_update, msg, True))
            self._status_update(f"Downloading {len(packs)} area(s) in the background.", force=True)

    def _open_settings(self):
        try:
            dlg = SettingsDialog(self, self.settings, user_dir=USER_DIR)
        except Exception as exc:
            # Settings used to fail silently in windowed release builds when
            # constructing a platform-specific control raised.  The errors
            # category is enabled by default, so this remains diagnosable even
            # when the user cannot open Settings to turn verbose logging on.
            import traceback
            miab_log(
                "errors",
                f"Settings dialog failed to open: {exc!r}\n{traceback.format_exc()}",
                self.settings,
            )
            self._announce_transient_then_return(
                "Settings could not open. Details were written to miab.log."
            )
            return
        saved = dlg.ShowModal() == wx.ID_OK
        gtfs_refresh = dlg.gtfs_refreshed
        set_home_requested = dlg.set_home_requested
        saved_settings = dlg.settings if saved else None
        # Destroy the ended modal before doing save work or speaking.  Leaving
        # it alive allows Windows/MSAA to restore map focus once at EndModal
        # and again when the dialog is eventually destroyed.
        dlg.Destroy()
        if saved:
            self.settings = saved_settings
            set_unit_system(self.settings.get("distance_unit", "metric"))
            self.settings["_log_path"] = os.path.join(USER_DIR, "miab.log")
            self._free_engine.log_settings = self.settings
            save_settings(self.settings)
            self._mistral.init(self.settings.get("mistral_api_key", ""))
            self._poi_fetcher.set_here_key(self.settings.get("here_api_key", ""))
            self._nav.update_settings(self.settings)
            self._here = HerePoi(
                api_key   = self.settings.get("here_api_key", ""),
                cache_dir = CACHE_DIR,
            )
            self._aviationstack = AviationStackClient(
                self.settings.get("aviationstack_api_key", ""))
            self._timetable = TimetableClient(
                self.settings.get("rapidapi_key", ""))
            self._priceline = PricelineClient(
                self.settings.get("rapidapi_key", ""))
            self._tripadvisor = TripAdvisorClient(
                self.settings.get("rapidapi_key", ""),
                os.path.join(CACHE_DIR, "tripadvisor_cache.json"))
            self._opensky = OpenSkyClient(
                base_dir=USER_DIR,
                client_id=self.settings.get("opensky_client_id", ""),
                client_secret=self.settings.get("opensky_client_secret", ""))
            if self._poi_list:
                self._show_poi_in_listbox()
            # Offer to update home location if requested
            if set_home_requested:
                self._home_setup_mode = True
                self.update_ui("Type your location to set as home.")
                self.show_jump_dialog()
                return
        if saved and gtfs_refresh:
            self._status_update("Refreshing transit feed catalog...")
            threading.Thread(target=self._refresh_transit_catalog, daemon=True).start()
        self._focus_map_window_silently()

    # ─────────────────────────────────────────────────────────────────
    #  TURN-BY-TURN NAVIGATION  (routing logic lives in nav.py)
    # ─────────────────────────────────────────────────────────────────

    def _announce_position_info(self):
        """I key — repeat last nav instruction when navigating, otherwise street + coords."""
        if getattr(self, '_nav_active', False):
            self._nav_announce_step()
            return
        if self.street_mode:
            self._street_survey_summary()
            return

        # ── Fallback: street name + GPS coordinates ─────────────────────────
        street = getattr(self, 'street_label', '') or getattr(self, 'last_location_str', '')
        lat_str = f"{abs(self.lat):.5f} {'North' if self.lat >= 0 else 'South'}"
        lon_str = f"{abs(self.lon):.5f} {'East' if self.lon >= 0 else 'West'}"
        if street:
            self._announce_transient(f"{street}.  {lat_str}, {lon_str}.")
        else:
            self._announce_transient(f"{lat_str}, {lon_str}.")

    def _announce_lat_lon(self):
        lat_str = f"{abs(self.lat):.5f} {'North' if self.lat >= 0 else 'South'}"
        lon_str = f"{abs(self.lon):.5f} {'East' if self.lon >= 0 else 'West'}"
        self._status_update(f"{lat_str}, {lon_str}.", force=True)

    def _announce_coordinate(self, msg: str) -> None:
        """Announce a coordinate after any native Mac menu speech settles.

        On wxOSX, F3/F4 are native menu accelerators.  VoiceOver announces the
        menu item name ("Latitude"/"Longitude") just after EVT_MENU runs, which
        can interrupt synchronous AO2 speech containing the actual value.
        """
        if IS_MAC:
            wx.CallLater(180, self._status_update, msg, True)
        else:
            self._status_update(msg, force=True)

    def _announce_latitude(self) -> None:
        self._announce_coordinate(
            f"{abs(self.lat):.4f} {'North' if self.lat >= 0 else 'South'}"
        )

    def _announce_longitude(self) -> None:
        self._announce_coordinate(
            f"{abs(self.lon):.4f} {'East' if self.lon >= 0 else 'West'}"
        )

    def _street_survey_bare(self, name):
        suffixes = {
            "street", "st", "road", "rd", "avenue", "ave", "drive", "dr",
            "court", "ct", "place", "pl", "crescent", "cres", "close", "cl",
            "boulevard", "blvd", "highway", "hwy", "terrace", "tce",
            "parade", "pde", "esplanade", "esp", "lane", "ln", "grove", "gr",
            "way", "circuit", "cct", "rise", "row", "mews", "track",
        }
        parts = (name or "").lower().split(",")[0].strip().split()
        if parts and parts[-1] in suffixes:
            parts = parts[:-1]
        return " ".join(parts)

    def _street_survey_number_key(self, number):
        match = re.match(r"^\s*(\d+)(.*)$", str(number or ""))
        if not match:
            return (float("inf"), str(number or "").lower())
        return (int(match.group(1)), match.group(2).strip().lower())

    def _street_survey_number_parity(self, number):
        match = re.match(r"^\s*(\d+)", str(number or ""))
        if not match:
            return None
        return "odd" if int(match.group(1)) % 2 else "even"

    def _street_survey_number_filter(self):
        mode = getattr(self, "_street_survey_number_filter_mode", None)
        if mode is None:
            legacy = self.__dict__.get("_street_survey_number_filter")
            if isinstance(legacy, str):
                mode = legacy
                try:
                    del self.__dict__["_street_survey_number_filter"]
                except KeyError:
                    pass
            else:
                mode = "all"
        return mode if mode in ("all", "odd", "even") else "all"

    def _toggle_street_survey_number_filter(self):
        if not self.street_mode:
            self._announce_transient("Number filter is available in street mode.")
            return True
        current = self._street_survey_number_filter()
        next_mode = {"all": "odd", "odd": "even", "even": "all"}[current]
        self._street_survey_number_filter_mode = next_mode
        label = {
            "all": "all house numbers",
            "odd": "odd house numbers only",
            "even": "even house numbers only",
        }[next_mode]
        self._announce_transient(f"Street number filter: {label}.")
        return True

    def _street_survey_address_announce_mode(self):
        mode = getattr(self, "_street_survey_address_announce_mode_value", None)
        if mode is None:
            shadowed = self.__dict__.get("_street_survey_address_announce_mode")
            if isinstance(shadowed, str):
                mode = shadowed
                try:
                    del self.__dict__["_street_survey_address_announce_mode"]
                except KeyError:
                    pass
            elif getattr(self, "_street_survey_poi_address_names", None) is False:
                mode = "plain"
            else:
                mode = "poi_names"
        return mode if mode in ("poi_names", "plain", "poi_only") else "poi_names"

    def _toggle_street_survey_address_announce_mode(self):
        if not self.street_mode:
            self._announce_transient("Address announcement mode is available in street mode.")
            return True
        current = self._street_survey_address_announce_mode()
        next_mode = {
            "poi_names": "plain",
            "plain": "poi_only",
            "poi_only": "poi_names",
        }[current]
        self._street_survey_address_announce_mode_value = next_mode
        label = {
            "poi_names": "announce POI names before house numbers",
            "plain": "do not announce POI names before house numbers",
            "poi_only": "skip house numbers without POIs",
        }[next_mode]
        self._announce_transient(f"Address mode: {label}.")
        return True

    def _street_survey_current_street(self):
        street = getattr(self, "street_label", "") or ""
        invalid = ("No street data nearby", "No street data", "Unknown", "")
        if street not in invalid:
            target = self._street_survey_bare(street)
            has_loaded_street = bool(self._street_survey_segments_for(target, already_bare=True))
            if not has_loaded_street:
                street = ""
        if not street or street in invalid:
            street, _ = self._nearest_road(self.lat, self.lon)
            if street not in invalid:
                self.street_label = street
        if street in invalid:
            return ""
        return re.sub(r"\s*\(.*?\)", "", street).strip()

    def _clear_street_survey_cache(self):
        self._street_survey_cache = {}
        self._street_survey_current_poi = None

    def _street_survey_cache_key(self, prefix, street_name, *extra):
        return (
            prefix,
            self._street_survey_bare(street_name),
            len(getattr(self, "_road_segments", []) or []),
            len(getattr(self, "_address_points", []) or []),
            len(getattr(self, "_all_pois", []) or []),
            self._street_survey_address_announce_mode(),
            self._street_survey_number_filter(),
            *extra,
        )

    def _street_survey_segments_for(self, street_name, already_bare=False):
        target = street_name if already_bare else self._street_survey_bare(street_name)
        cache = getattr(self, "_street_survey_cache", None)
        if cache is None:
            cache = {}
            self._street_survey_cache = cache
        key = ("segments", target, len(getattr(self, "_road_segments", []) or []))
        cached = cache.get(key)
        if cached is not None:
            return cached
        segments = []
        for seg in getattr(self, "_road_segments", []):
            raw = re.sub(r"\s*\(.*?\)", "", seg.get("name", "")).strip()
            if self._street_survey_bare(raw) == target:
                segments.append(seg)
        cache[key] = segments
        return segments

    def _street_survey_project(self, street_name, lat, lon):
        best = None
        for seg in self._street_survey_segments_for(street_name):
            coords = seg.get("coords", [])
            if len(coords) < 2:
                continue
            along = 0.0
            for i in range(len(coords) - 1):
                a_lat, a_lon = coords[i]
                b_lat, b_lon = coords[i + 1]
                p_lat, p_lon = nearest_point_on_segment(lat, lon, a_lat, a_lon, b_lat, b_lon)
                dist = dist_metres(lat, lon, p_lat, p_lon)
                seg_len = dist_metres(a_lat, a_lon, b_lat, b_lon)
                pos_len = dist_metres(a_lat, a_lon, p_lat, p_lon)
                candidate = (dist, along + pos_len, p_lat, p_lon)
                if best is None or candidate[0] < best[0]:
                    best = candidate
                along += seg_len
        return best

    def _street_survey_address_candidates(self, street_name):
        target = self._street_survey_bare(street_name)
        out = []

        def add_candidate(number, street, lat, lon, source="", name="", poi=None):
            if not number or lat is None or lon is None:
                return
            if self._street_survey_bare(street or "") != target:
                return
            try:
                out.append({
                    "number": str(number).strip(),
                    "street": street,
                    "lat": float(lat),
                    "lon": float(lon),
                    "source": source,
                    "name": str(name or "").split(",")[0].strip(),
                    "poi": poi if isinstance(poi, dict) else None,
                })
            except (TypeError, ValueError):
                return

        for poi in getattr(self, "_all_pois", []) or []:
            if not isinstance(poi, dict):
                continue
            tags = poi.get("tags", {}) if isinstance(poi.get("tags"), dict) else {}
            number = poi.get("number") or tags.get("addr:housenumber")
            street = poi.get("street") or tags.get("addr:street")
            name = poi.get("name") or poi.get("label") or tags.get("name") or ""
            add_candidate(number, street, poi.get("lat"), poi.get("lon"), "poi", name, poi=poi)

        for ap in getattr(self, "_address_points", []):
            if not self._address_point_source_enabled(ap):
                continue
            add_candidate(ap.get("number"), ap.get("street"), ap.get("lat"), ap.get("lon"), "address")

        pinned_num = getattr(self, "_jump_address_number", None)
        pinned_street = getattr(self, "_jump_address_street", None)
        pinned_lat = getattr(self, "_jump_street_pin_lat", None)
        pinned_lon = getattr(self, "_jump_street_pin_lon", None)
        add_candidate(pinned_num, pinned_street, pinned_lat, pinned_lon, "jump")

        pending_num = getattr(self, "_pending_jump_address_number", None)
        pending_street = getattr(self, "_pending_jump_address_street", None)
        pending_lat = getattr(self, "_pending_jump_address_lat", None)
        pending_lon = getattr(self, "_pending_jump_address_lon", None)
        add_candidate(pending_num, pending_street, pending_lat, pending_lon, "jump")

        return out

    def _street_survey_poi_candidates(self, street_name, max_snap_m=45.0):
        out = []
        seen = set()
        for poi in getattr(self, "_all_pois", []) or []:
            if not isinstance(poi, dict):
                continue
            name = (poi.get("name") or poi.get("label") or "").split(",")[0].strip()
            plat = poi.get("lat")
            plon = poi.get("lon")
            if not name or plat is None or plon is None:
                continue
            key = name.lower()
            if key in seen:
                continue
            proj = self._street_survey_project(street_name, plat, plon)
            if not proj or proj[0] > max_snap_m:
                continue
            seen.add(key)
            out.append({
                "number": name,
                "lat": float(plat),
                "lon": float(plon),
                "along": proj[1],
                "snap_dist": proj[0],
                "name": name,
                "source": "poi_near_street",
                "poi": poi,
            })
        return out

    def _street_survey_addresses(self, street_name, include_pois_near_street=False):
        cache = getattr(self, "_street_survey_cache", None)
        if cache is None:
            cache = {}
            self._street_survey_cache = cache
        cache_key = self._street_survey_cache_key(
            "addresses",
            street_name,
            bool(include_pois_near_street),
        )
        cached = cache.get(cache_key)
        if cached is not None:
            return list(cached)

        out = []
        seen = set()
        for ap in self._street_survey_address_candidates(street_name):
            proj = self._street_survey_project(street_name, ap["lat"], ap["lon"])
            if not proj:
                continue
            key = (str(ap["number"]).lower(), round(ap["lat"], 7), round(ap["lon"], 7))
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "number": str(ap["number"]),
                "lat": ap["lat"],
                "lon": ap["lon"],
                "along": proj[1],
                "snap_dist": proj[0],
                "name": ap.get("name", ""),
                "source": ap.get("source", ""),
                "poi": ap.get("poi"),
            })
        if include_pois_near_street:
            for poi in self._street_survey_poi_candidates(street_name):
                key = ("poi", poi["name"].lower())
                if key in seen:
                    continue
                seen.add(key)
                out.append(poi)
        sorted_out = sorted(out, key=lambda item: self._street_survey_number_key(item["number"]))
        try:
            numbers = ", ".join(item["number"] for item in sorted_out[:60])
            if len(sorted_out) > 60:
                numbers += ", ..."
            miab_log(
                "verbose",
                f"Street survey addresses for {street_name}: {len(sorted_out)} numbers [{numbers}]",
                self.settings,
            )
        except Exception:
            pass
        cache[cache_key] = tuple(sorted_out)
        return sorted_out

    def _current_street_survey_poi(self):
        if not getattr(self, "street_mode", False):
            return None
        if self._street_survey_address_announce_mode() not in ("poi_names", "poi_only"):
            return None
        poi = getattr(self, "_street_survey_current_poi", None)
        if isinstance(poi, dict):
            return poi
        street = self._street_survey_current_street()
        if not street:
            return None
        number = self._street_survey_current_number(street)
        if not number:
            return None
        current_key = self._street_survey_number_key(number)
        best = None
        best_dist = float("inf")
        for addr in self._street_survey_addresses(street, include_pois_near_street=False):
            poi = addr.get("poi")
            if not isinstance(poi, dict):
                continue
            if self._street_survey_number_key(addr.get("number")) != current_key:
                continue
            d = dist_metres(self.lat, self.lon, addr.get("lat"), addr.get("lon"))
            if d < best_dist:
                best_dist = d
                best = poi
        self._street_survey_current_poi = best
        return best

    def _street_survey_current_number(self, street):
        pinned_num = getattr(self, "_jump_address_number", None)
        pinned_street = getattr(self, "_jump_address_street", None)
        pin_lat = getattr(self, "_jump_street_pin_lat", None)
        pin_lon = getattr(self, "_jump_street_pin_lon", None)
        if (pinned_num and pinned_street
                and self._street_survey_bare(pinned_street) == self._street_survey_bare(street)
                and pin_lat is not None and pin_lon is not None
                and dist_metres(self.lat, self.lon, pin_lat, pin_lon) <= 5.0):
            return str(pinned_num)
        return self._nearest_address_number(self.lat, self.lon, street, radius=80)

    def _street_survey_address_axis(self, street_name):
        points = []
        for ap in self._street_survey_address_candidates(street_name):
            key = self._street_survey_number_key(ap.get("number"))
            if key[0] == float("inf"):
                continue
            points.append((key, ap["lat"], ap["lon"]))
        if len(points) < 2:
            return None
        points.sort(key=lambda item: item[0])
        lo = points[max(0, len(points) // 10)]
        hi = points[min(len(points) - 1, len(points) - 1 - len(points) // 10)]
        lat0 = (lo[1] + hi[1]) / 2.0
        lon0 = (lo[2] + hi[2]) / 2.0
        scale_x = 111000 * math.cos(math.radians(lat0))
        vx = (hi[2] - lo[2]) * scale_x
        vy = (hi[1] - lo[1]) * 111000
        length = math.hypot(vx, vy)
        if length < 10:
            return None
        return lat0, lon0, vx / length, vy / length, scale_x

    def _street_survey_axis_value(self, axis, lat, lon):
        lat0, lon0, ux, uy, scale_x = axis
        x = (lon - lon0) * scale_x
        y = (lat - lat0) * 111000
        return x * ux + y * uy

    def _street_survey_go_address(self, direction):
        if not self.street_mode:
            return False
        street = self._street_survey_current_street()
        if not street:
            self._announce_transient_then_return("No current street.")
            return True
        address_mode = self._street_survey_address_announce_mode()
        addresses = self._street_survey_addresses(
            street,
            include_pois_near_street=False,
        )
        if not addresses:
            self._announce_transient_then_return(f"No known house numbers loaded for {street}.")
            return True
        current_num = self._street_survey_current_number(street)
        if not current_num:
            self._announce_transient_then_return(f"No current house number found on {street}.")
            return True
        current_key = self._street_survey_number_key(current_num)
        unique = {}
        for addr in addresses:
            key = addr["number"].lower()
            existing = unique.get(key)
            if existing is None or (addr.get("name") and not existing.get("name")):
                unique[key] = addr
        addresses = sorted(unique.values(), key=lambda item: self._street_survey_number_key(item["number"]))
        number_filter = self._street_survey_number_filter()
        if number_filter != "all" and address_mode != "poi_only":
            addresses = [
                a for a in addresses
                if self._street_survey_number_parity(a["number"]) == number_filter
            ]
            if not addresses:
                self._announce_transient_then_return(f"No {number_filter} house numbers loaded for {street}.")
                return True
        if address_mode == "poi_only":
            addresses = [a for a in addresses if a.get("name")]
            if not addresses:
                self._announce_transient_then_return(f"No POI house numbers loaded for {street}.")
                return True
        if direction > 0:
            choices = [a for a in addresses if self._street_survey_number_key(a["number"]) > current_key]
            target = choices[0] if choices else None
            edge_msg = (
                f"No higher known {number_filter} house number on {street}."
                if number_filter != "all"
                else f"No higher POI house number on {street}."
                if address_mode == "poi_only"
                else f"No higher known house number on {street}."
            )
        else:
            choices = [a for a in addresses if self._street_survey_number_key(a["number"]) < current_key]
            target = choices[-1] if choices else None
            edge_msg = (
                f"No lower known {number_filter} house number on {street}."
                if number_filter != "all"
                else f"No lower POI house number on {street}."
                if address_mode == "poi_only"
                else f"No lower known house number on {street}."
            )
        if not target:
            self._announce_transient(edge_msg)
            return True
        self.lat = target["lat"]
        self.lon = target["lon"]
        self.street_label = street
        self._jump_street_label = street
        self._jump_street_pin_lat = self.lat
        self._jump_street_pin_lon = self.lon
        self._jump_address_number = target["number"]
        self._jump_address_street = street
        self._street_survey_current_poi = (
            target.get("poi")
            if address_mode in ("poi_names", "poi_only") and target.get("name")
            else None
        )
        self._street_survey_last_direction = direction
        wx.CallAfter(self.map_panel.set_position, self.lat, self.lon, True, street)
        name = target.get("name", "") if address_mode in ("poi_names", "poi_only") else ""
        if name:
            self._announce_transient(f"{name}, {target['number']} {street}.")
        else:
            self._announce_transient(f"{target['number']} {street}.")
        return True

    def _street_survey_intersections(self, street_name):
        if not getattr(self, "_walk_graph", None):
            try:
                self._walk_graph = self._build_walk_graph()
                self._nav.set_graph(self._walk_graph)
            except Exception as exc:
                miab_log("errors", f"[StreetSurvey] Walk graph build failed: {exc}", getattr(self, "settings", None))
                return []
        graph = self._walk_graph or {}
        nodes = graph.get("nodes", {})
        node_streets = graph.get("node_streets", {})
        intersections = graph.get("intersections", set())
        out = []
        target = self._street_survey_bare(street_name)
        axis = self._street_survey_address_axis(street_name)
        for nid in intersections:
            if target not in {self._street_survey_bare(name) for name in node_streets.get(nid, set())}:
                continue
            nlat, nlon = nodes.get(nid, (None, None))
            if nlat is None:
                continue
            if axis:
                out.append((self._street_survey_axis_value(axis, nlat, nlon), nid, nlat, nlon))
            else:
                proj = self._street_survey_project(street_name, nlat, nlon)
                if proj:
                    out.append((proj[1], nid, nlat, nlon))
        return sorted(out)

    def _street_survey_heading_to_node(self, street, nid, nlat, nlon, direction=1):
        """Best-effort heading for describing an F11 survey intersection."""
        if dist_metres(self.lat, self.lon, nlat, nlon) > 2:
            return bearing_deg(self.lat, self.lon, nlat, nlon)

        intersections = self._street_survey_intersections(street)
        current = None
        for idx, item in enumerate(intersections):
            if item[1] == nid:
                current = idx
                break
        if current is not None:
            neighbour_idx = current + (1 if direction >= 0 else -1)
            if 0 <= neighbour_idx < len(intersections):
                _along, _nnid, lat2, lon2 = intersections[neighbour_idx]
                return bearing_deg(nlat, nlon, lat2, lon2)
            neighbour_idx = current - (1 if direction >= 0 else -1)
            if 0 <= neighbour_idx < len(intersections):
                _along, _nnid, lat2, lon2 = intersections[neighbour_idx]
                return (bearing_deg(nlat, nlon, lat2, lon2) + 180) % 360

        axis = self._street_survey_address_axis(street)
        if axis:
            _lat0, _lon0, ux, uy, _scale_x = axis
            if direction < 0:
                ux, uy = -ux, -uy
            return (math.degrees(math.atan2(ux, uy)) + 360) % 360
        return 0.0

    def _street_survey_intersection_shape_text(self, street, nid, nlat, nlon, direction=1):
        if not getattr(self, "_walk_graph", None):
            return ""
        heading = self._street_survey_heading_to_node(street, nid, nlat, nlon, direction)
        return self._walk_describe_intersection_shape(nid, street, heading)

    def _street_boundary_move(self, new_lat, new_lon):
        if not self.street_mode or self._road_fetch_lat is None:
            return False
        dlat = (new_lat - self._road_fetch_lat) * 111000
        dlon = (new_lon - self._road_fetch_lon) * 111000 * math.cos(math.radians(new_lat))
        dist_from_origin = math.sqrt(dlat**2 + dlon**2)
        if dist_from_origin <= self._street_barrier:
            return False

        from street_data import _load_road_cache
        current_suburb = getattr(self, '_current_suburb', None)
        cache_entry = _load_road_cache(
            self._street_fetcher._cache_dir,
            new_lat, new_lon,
            suburb_name=current_suburb
        )
        if cache_entry and cache_entry.get("segments"):
            cached_segments = cache_entry.get("segments", [])
            test_label, _ = self._street_fetcher.nearest_road(
                new_lat, new_lon, cached_segments)
            if test_label not in ("No street data nearby", "Unknown", "", "No street data"):
                self.lat = new_lat
                self.lon = new_lon
                self._road_segments = cached_segments
                self._address_points = self._cache_addresses_for_current_gnaf_mode(cache_entry)
                self._natural_features = cache_entry.get("natural_features", [])
                self._road_fetch_lat = self.lat
                self._road_fetch_lon = self.lon
                self.street_label = test_label
                self._announce_transient(f"Entered {current_suburb}. {test_label}")
                self._play_spatial_tone_if_allowed(
                    self.lat, self.lon, self._spatial_tone_bounds())
                wx.CallAfter(self.map_panel.set_position, self.lat, self.lon, True, test_label)
                return True

        nf = self._check_natural_feature(new_lat, new_lon)
        if nf:
            name = nf.get("name")
            desc = nf.get("description", "edge of loaded area")
            self._announce_transient(f"Edge of loaded area. {name if name else desc}")
            return True
        if not _IS_LAND(new_lat, new_lon):
            # Only block if the player is currently on land — if already in
            # water (bad jump, tidal flat, coarse polygon) let them move out.
            if _IS_LAND(self.lat, self.lon):
                label = None
                if hasattr(self, '_geo_features'):
                    try:
                        cc = getattr(self, '_current_country_code', None)
                        label = (self._geo_lookup_precise(new_lat, new_lon, cc)
                                 or self._geo_lookup_any(new_lat, new_lon, cc))
                    except Exception:
                        pass
                if label:
                    self._announce_transient(f"{label}.")
                else:
                    self._announce_transient_then_return("Can't move into water.")
                return True
        if getattr(self, '_barrier_dialog_pending', False):
            return True
        self._barrier_dialog_pending = True
        def _geocode_and_confirm():
            try:
                from street_data import geocode_location
                geo = geocode_location(new_lat, new_lon)
                suburb = geo.get("suburb", "") if geo else ""
            except Exception:
                suburb = ""
            wx.CallAfter(self._confirm_barrier_crossing, new_lat, new_lon, suburb)
        threading.Thread(target=_geocode_and_confirm, daemon=True).start()
        return True

    def _street_offer_suburb_probe(self, new_lat, new_lon, current_street):
        if getattr(self, '_barrier_dialog_pending', False):
            return True
        if not _IS_LAND(new_lat, new_lon):
            return False
        self._barrier_dialog_pending = True
        def _geocode_and_confirm():
            try:
                from street_data import geocode_location
                geo = geocode_location(new_lat, new_lon)
                suburb = geo.get("suburb", "") if geo else ""
            except Exception:
                suburb = ""
            current = (getattr(self, "_current_suburb", "") or "").strip().lower()
            if suburb and suburb.strip().lower() != current:
                wx.CallAfter(self._confirm_barrier_crossing, new_lat, new_lon, suburb)
            else:
                self._barrier_dialog_pending = False
                wx.CallAfter(
                    self._announce_transient,
                    f"No further {current_street} intersections found in this loaded area.")
        threading.Thread(target=_geocode_and_confirm, daemon=True).start()
        return True

    def _street_survey_try_boundary_continue(self, street, direction, edge_msg):
        axis = self._street_survey_address_axis(street)
        if not axis or self._road_fetch_lat is None:
            self._announce_transient(edge_msg)
            return True
        lat0, lon0, ux, uy, scale_x = axis
        for metres in (350, 700, 1200, 1800):
            new_lat = self.lat + (direction * uy * metres / 111000)
            new_lon = self.lon + (direction * ux * metres / scale_x)
            test_label, _ = self._street_fetcher.nearest_road(
                new_lat, new_lon, getattr(self, "_road_segments", []))
            if self._street_survey_bare(test_label) == self._street_survey_bare(street):
                continue
            if self._street_boundary_move(new_lat, new_lon):
                return True
            if self._street_offer_suburb_probe(new_lat, new_lon, street):
                return True
        self._announce_transient(edge_msg)
        return True

    def _street_survey_go_block(self, direction):
        if not self.street_mode:
            return False
        street = self._street_survey_current_street()
        if not street:
            self._announce_transient_then_return("No current street.")
            return True
        intersections = self._street_survey_intersections(street)
        if not intersections:
            self._announce_transient_then_return(f"No intersections loaded for {street}.")
            return True
        axis = self._street_survey_address_axis(street)
        if axis:
            here_along = self._street_survey_axis_value(axis, self.lat, self.lon)
        else:
            here = self._street_survey_project(street, self.lat, self.lon)
            here_along = here[1] if here else intersections[0][0]
        if direction > 0:
            choices = [item for item in intersections if item[0] > here_along + 2.0]
            target = choices[0] if choices else None
            edge_msg = f"No higher-number direction intersection on {street}."
        else:
            choices = [item for item in intersections if item[0] < here_along - 2.0]
            target = choices[-1] if choices else None
            edge_msg = f"No lower-number direction intersection on {street}."
        if not target:
            return self._street_survey_try_boundary_continue(street, direction, edge_msg)
        _along, nid, nlat, nlon = target
        shape = self._street_survey_intersection_shape_text(
            street, nid, nlat, nlon, direction)
        self.lat, self.lon = nlat, nlon
        self.street_label = street
        self._jump_street_label = street
        self._jump_street_pin_lat = self.lat
        self._jump_street_pin_lon = self.lon
        self._jump_address_number = None
        self._jump_address_street = None
        self._street_survey_last_direction = direction
        cross = self._walk_get_cross_streets(nid, street) if getattr(self, "_walk_graph", None) else []
        wx.CallAfter(self.map_panel.set_position, self.lat, self.lon, True, street)
        suffix = f"  {shape}" if shape else ""
        if cross:
            cross_text = " and ".join(cross[:2])
            self._announce_transient(f"{street} at {cross_text}.{suffix}")
        else:
            self._announce_transient(f"Intersection on {street}.{suffix}")
        return True

    def _street_survey_turn_cross_street(self, turn_back=False):
        """Ctrl+Shift+Page Down turns onto a cross street; Ctrl+Shift+Page Up turns back."""
        if not self.street_mode:
            return False
        street = self._street_survey_current_street()
        if not street:
            self._announce_transient_then_return("No current street.")
            return True

        if turn_back:
            prev = getattr(self, "_street_turn_previous", None)
            turn_lat = getattr(self, "_street_turn_lat", None)
            turn_lon = getattr(self, "_street_turn_lon", None)
            if not prev or turn_lat is None or turn_lon is None:
                self._announce_transient_then_return("No previous street to turn back onto.")
                return True
            self.lat, self.lon = turn_lat, turn_lon
            self.street_label = prev
            self._jump_street_label = prev
            self._jump_street_pin_lat = self.lat
            self._jump_street_pin_lon = self.lon
            self._jump_address_number = None
            self._jump_address_street = None
            self._street_turn_previous = street
            self._street_turn_lat = self.lat
            self._street_turn_lon = self.lon
            wx.CallAfter(self.map_panel.set_position, self.lat, self.lon, True, prev)
            self._announce_transient(f"Turned back onto {prev}.")
            return True

        intersections = self._street_survey_intersections(street)
        if not intersections:
            self._announce_transient_then_return(f"No intersections loaded for {street}.")
            return True

        best = None
        for _along, nid, nlat, nlon in intersections:
            d = dist_metres(self.lat, self.lon, nlat, nlon)
            if best is None or d < best[0]:
                best = (d, nid, nlat, nlon)

        if best is None:
            self._announce_transient_then_return("No intersection found here.")
            return True

        dist_m, nid, nlat, nlon = best
        if dist_m > 35:
            self._announce_transient("Move to an intersection first with Ctrl+Page Up or Ctrl+Page Down.")
            return True

        cross = self._walk_get_cross_streets(nid, street)
        cross = [s for s in cross if self._street_survey_bare(s) != self._street_survey_bare(street)]
        if not cross:
            self._announce_transient_then_return(f"No other street to turn onto from {street}.")
            return True

        target = sorted(cross, key=str.lower)[0]
        self.lat, self.lon = nlat, nlon
        self.street_label = target
        self._jump_street_label = target
        self._jump_street_pin_lat = self.lat
        self._jump_street_pin_lon = self.lon
        self._jump_address_number = None
        self._jump_address_street = None
        self._street_turn_previous = street
        self._street_turn_lat = self.lat
        self._street_turn_lon = self.lon
        wx.CallAfter(self.map_panel.set_position, self.lat, self.lon, True, target)
        self._announce_transient(f"Turned onto {target}.")
        return True

    def _street_survey_summary(self):
        street = self._street_survey_current_street()
        if not street:
            self._announce_transient_then_return("No current street.")
            return
        addresses = self._street_survey_addresses(street)
        intersections = self._street_survey_intersections(street)
        here = self._street_survey_project(street, self.lat, self.lon)
        here_along = here[1] if here else None
        parts = [street]
        if intersections and here_along is not None:
            before = [item for item in intersections if item[0] <= here_along + 2.0]
            after = [item for item in intersections if item[0] >= here_along - 2.0]
            prev_item = before[-1] if before else None
            next_item = after[0] if after else None
            prev_cross = self._walk_get_cross_streets(prev_item[1], street) if prev_item else []
            next_cross = self._walk_get_cross_streets(next_item[1], street) if next_item else []
            if prev_cross and next_cross and prev_item != next_item:
                parts.append(f"block between {', '.join(prev_cross[:1])} and {', '.join(next_cross[:1])}")
            elif prev_cross:
                parts.append(f"near {', '.join(prev_cross[:2])}")
            nearest = None
            for item in (prev_item, next_item):
                if not item:
                    continue
                _along, nid, nlat, nlon = item
                d = dist_metres(self.lat, self.lon, nlat, nlon)
                if nearest is None or d < nearest[0]:
                    nearest = (d, nid, nlat, nlon)
            if nearest and nearest[0] <= 35:
                _d, nid, nlat, nlon = nearest
                direction = getattr(self, "_street_survey_last_direction", 1)
                shape = self._street_survey_intersection_shape_text(
                    street, nid, nlat, nlon, direction=direction)
                if shape:
                    parts.append(shape)
        if addresses:
            nums = sorted({a["number"] for a in addresses}, key=self._street_survey_number_key)
            parts.append(f"{len(nums)} known numbers, {nums[0]} to {nums[-1]}")
        else:
            parts.append("no known house numbers loaded")
        self._announce_transient(".  ".join(parts) + ".")


    def _handle_preface_shortcuts(self, event, key, shift, primary, alt, no_mod):
        # Favourites — works in any mode when a coordinate/POI is available.
        if primary and not alt and key in (ord('F'), ord('f')):
            if shift:
                self._add_current_favourite()
            else:
                self._show_favourites()
            return True

        # Escape exits walking mode.
        if key == wx.WXK_ESCAPE and getattr(self, '_walking_mode', False):
            self._nav_active = False
            self._nav_arrived = False
            self._set_nav_button_visible(False)
            self._walk_toggle()
            return True

        # Escape during active navigation (street mode, non-walking) cancels
        # the route — including after arrival, when the user has been
        # browsing back/forth through the step list.
        if (key == wx.WXK_ESCAPE
                and getattr(self, '_nav_active', False)
                and self.street_mode
                and not getattr(self, '_walking_mode', False)):
            arrived = getattr(self, '_nav_arrived', False)
            self._nav_active = False
            self._nav_arrived = False
            self._nav_briefing_mode  = False
            self._nav_briefing_steps = []
            self._nav_briefing_step  = 0
            self._nav.reset()
            self._set_nav_button_visible(False)
            msg = ("Navigation ended." if arrived
                   else f"Navigation to {getattr(self, '_nav_dest_name', 'destination')} cancelled.")
            self._announce_transient(msg)
            return True

        # Bare F in street mode toggles free mode when there is no POI list.
        if no_mod and (key == ord('F') or key == ord('f')):
            if self.street_mode and not bool(self._poi_list):
                self._toggle_free_mode()
            return True

        return False

    def _handle_free_mode_shortcuts(self, key, shift, primary, alt, no_mod):
        if not getattr(self, '_free_mode', False):
            return False
        if key == wx.WXK_UP:
            self._free_step(1); return True
        if key == wx.WXK_DOWN:
            self._free_step(-1); return True
        if primary and key == wx.WXK_LEFT:
            self._free_snap_cross(); return True
        if primary and key == wx.WXK_RIGHT:
            self._free_snap_cross(); return True
        if key == wx.WXK_LEFT:
            text, pois = self._free_engine.describe_left_with_pois()
            self._free_last_side_pois = pois
            self._free_last_side      = "left"
            self._announce_transient_then_return(text if text else "Nothing on the left."); return True
        if key == wx.WXK_RIGHT:
            text, pois = self._free_engine.describe_right_with_pois()
            self._free_last_side_pois = pois
            self._free_last_side      = "right"
            self._announce_transient_then_return(text if text else "Nothing on the right."); return True
        if no_mod and (key == ord('A') or key == ord('a')):
            self._announce_address(); return True
        if no_mod and (key == ord('H') or key == ord('h')):
            self._free_heading(); return True
        if no_mod and (key == ord('X') or key == ord('x')):
            self._free_describe_intersection(); return True
        if no_mod and (key == ord('R') or key == ord('r')):
            self._free_turnaround(); return True
        if key in (wx.WXK_DELETE, wx.WXK_F2):
            self._free_poi_action(key); return True
        # Let system key combos (Alt+F4, etc.) and the shared function keys
        # fall through to the normal handlers below.
        if alt or key in (wx.WXK_F1, wx.WXK_F7, wx.WXK_F11,
                                           wx.WXK_F2, wx.WXK_F3, wx.WXK_F4,
                                           wx.WXK_F5, wx.WXK_F6):
            return False
        return True

    def _handle_global_function_keys(self, key, shift, primary, alt, no_mod):
        if primary and shift and not alt and (key == ord('S') or key == ord('s')):
            miab_log("feature_usage", "Key: Ctrl+Shift+S (satellite view)", self.settings)
            lat, lon = self._poi_lat_lon_if_focused()
            self._satellite_view_at_location(lat, lon); return True
        if primary and shift and alt and (key == ord('S') or key == ord('s')):
            miab_log("feature_usage", "Key: Ctrl+Shift+Alt+S (street view)", self.settings)
            lat, lon = self._poi_lat_lon_if_focused()
            poi_focused = (lat, lon) != (self.lat, self.lon) or (
                bool(getattr(self, '_poi_list', [])) and
                getattr(self, 'listbox', None) is not None and
                self.listbox.HasFocus()
            )
            if self.street_mode or getattr(self, '_walking_mode', False) or poi_focused:
                self._streetview_at_location(lat, lon)
            else:
                self._announce_transient_then_return(
                    "Street View works in street mode or from a POI list. "
                    "Showing satellite instead.")
                self._schedule_satellite_view(lat, lon)
            return True
        if no_mod and key == wx.WXK_F1:    self.show_help();              return True
        if shift and not primary and key == wx.WXK_F2:
            self._announce_climate_zone(); return True
        if no_mod and key == wx.WXK_F2:
            self._handle_f2_tap()
            return True
        if shift and not primary and key == wx.WXK_F3:
            self._status_update(self.sound.volume_down(), force=True); return True
        if shift and not primary and key == wx.WXK_F4:
            self._status_update(self.sound.volume_up(), force=True); return True
        if no_mod and key == wx.WXK_F3:
            self._announce_latitude(); return True
        if primary and key == ord(','):
            self._open_settings();  return True
        if no_mod and key == wx.WXK_F4:
            self._announce_longitude(); return True
        if no_mod and key == wx.WXK_F5:
            miab_log("feature_usage", "Key: F5 (continent)", self.settings)
            self.announce_continent();    return True
        if shift and not primary and key == wx.WXK_F5:
            miab_log("feature_usage", "Key: Shift+F5 (toggle GeoFeatures)", self.settings)
            self._toggle_geo_features();  return True
        if no_mod and key == wx.WXK_F6:
            # Country facts - like the challenge game, this is tied to
            # the world-map country under the cursor, not to street-level
            # position, so it doesn't reliably mean anything mid-street-mode.
            # Same treatment as F10/Ctrl+F10: offer to exit street mode first.
            if not self._confirm_exit_street_mode(
                    "Country facts are for the world map. Exit street mode?"):
                return True
            miab_log("feature_usage", "Key: F6 (facts)", self.settings)
            self.announce_facts();        return True
        if shift and not primary and key == wx.WXK_F6:
            if not self._confirm_exit_street_mode(
                    "The Wikipedia summary is for the world map. Exit street mode?"):
                return True
            miab_log("feature_usage", f"Key: Shift+F6 (Wikipedia) at {self.last_country_found}", self.settings)
            self.announce_wikipedia_summary(); return True
        if no_mod and key == wx.WXK_F7:    self.toggle_sounds();    return True
        if shift and not primary and key == wx.WXK_F8:
            miab_log("feature_usage", "Key: Shift+F8 (map display mode)", self.settings)
            self._cycle_map_display_mode(); return True
        if no_mod and key == wx.WXK_F8:
            flashed = self._flash_current_country()
            if flashed:
                country = getattr(self, 'last_country_found', 'country')
                wx.CallAfter(self._status_update, f"Displaying {country}.", True)
            else:
                wx.CallAfter(
                    self._announce_transient_then_return,
                    "No current country to display.")
            return True
        if no_mod and key == wx.WXK_F9:    self._toggle_map_fullscreen(); return True
        if shift and not primary and key == wx.WXK_F10:
            self._game.repeat_target()
            return True
        if primary and key == wx.WXK_F10:
            if self._session and self._session.active:
                self._session.stop()
                self._session = None
                self._game._timeout_cb = None
                self._status_update("Challenge session ended.", force=True)
                wx.CallAfter(self._resume_location_sound)
            else:
                # The challenge is played by moving the world-map cursor -
                # in street mode arrow keys move along the road network
                # instead, so a challenge started from there would be
                # unplayable (nothing would ever answer the target). Exit
                # street mode first, same pattern as jumping with J.
                if not self._confirm_exit_street_mode(
                        "Playing the challenge exits street mode. Continue?"):
                    return True
                self._start_challenge_session()
            return True
        if no_mod and key == wx.WXK_F10:
            if self._session and self._session.active:
                self._session.stop()
                self._session = None
                self._game._timeout_cb = None
                self._status_update("Challenge session ended.", force=True)
                wx.CallAfter(self._resume_location_sound)
            elif self._game.active:
                miab_log("challenges", "Challenge stopped manually.", self.settings)
                self._game.stop()
                wx.CallAfter(self._resume_location_sound)
            else:
                if self.df is not None and not self.df.empty:
                    if not self._confirm_exit_street_mode(
                            "Playing the challenge exits street mode. Continue?"):
                        return True
                    self.sound.stop()
                    self._game.start(self.df, self.lat, self.lon)
                else:
                    self._announce_transient_then_return("No city data available for the challenge.")
            return True
        if primary and shift and not alt and key == wx.WXK_F11:
            self._open_city_pack_wizard()
            return True
        if key == wx.WXK_F11:
            if shift and not primary:
                if not self.street_mode:
                    self._prefetch_streets()
                else:
                    self._announce_transient_then_return(
                        "Shift+F11: pre-download works from world map only.")
            elif no_mod:
                if not self.street_mode and (getattr(self, '_prefetch_in_progress', False) or getattr(self, '_loading', False)):
                    self._announce_transient_then_return("Street download in progress. Please wait.")
                else:
                    # Capture state before toggling: exiting sets street_mode
                    # synchronously, but entering is async (a background thread
                    # geocodes first), so reading self.street_mode right after
                    # the call would wrongly log "exited" for an entry in
                    # progress. Log the action that was actually initiated.
                    was_in_street_mode = self.street_mode
                    self.toggle_street_mode()
                    self._update_main_menu_state()
                    miab_log("navigation",
                             f"Street mode {'exit' if was_in_street_mode else 'entry'} requested.",
                             self.settings)
            return True
        if primary and shift and not alt and key == wx.WXK_F12:
            self._confirm_or_toggle_gnaf_addresses()
            return True
        if primary and not shift and not alt and key == wx.WXK_F12:
            self._toggle_street_survey_address_announce_mode()
            return True
        if primary and alt and not shift and key == wx.WXK_F12:
            self._toggle_street_survey_number_filter()
            return True
        if no_mod and key == wx.WXK_F12:
            self._open_tools_menu(); return True
        return False

    def _handle_global_map_shortcuts(self, key, shift, primary, alt, no_mod):
        if no_mod and (key == ord('L') or key == ord('l')):
            miab_log("feature_usage", "Key: L (latitude/longitude)", self.settings)
            self._announce_lat_lon(); return True
        if shift and not primary and (key == ord('L') or key == ord('l')):
            miab_log("feature_usage", "Key: Shift+L (languages)", self.settings)
            self._announce_languages(); return True
        if no_mod and key == wx.WXK_SPACE:
            if self._session and self._session.active:
                if self._session.on_space(self.df, self.lat, self.lon):
                    return True
        if no_mod and (key == ord('J') or key == ord('j')):
            if self._game.active:
                self._announce_transient_then_return("Jump is disabled during the challenge. Use your ears!")
                return True
            if not self._confirm_exit_street_mode_for_jump():
                return True
            self.show_jump_dialog()
            return True
        if primary and not shift and not alt and (key == ord('J') or key == ord('j')):
            if self._game.active:
                self._announce_transient_then_return("Jump is disabled during the challenge. Use your ears!")
                return True
            self._jump_to_saved_mark()
            return True
        if primary and not shift and not alt and (key == ord('H') or key == ord('h')):
            self.show_jump_history(); return True
        if primary and not shift and not alt and key in (ord('1'), ord('2'), ord('3')):
            self._announce_mark(int(chr(key)), return_focus=False)
            return True
        if primary and not shift and not alt and (key == ord('M') or key == ord('m')):
            self._prompt_mark_slot(remove=False)
            return True
        if primary and shift and not alt and (key == ord('M') or key == ord('m')):
            self._prompt_mark_slot(remove=True)
            return True
        if primary and shift and not alt and (key == ord('P') or key == ord('p')):
            self._add_personal_poi_here()
            return True
        if shift and alt and not primary and (key == ord('M') or key == ord('m')):
            self._report_all_mark_distances(return_focus=False)
            return True
        if primary and alt and not shift:
            alt_map = {ord('1'): 1, ord('2'): 2, ord('3'): 3,
                       ord('4'): 4, ord('5'): 5, ord('6'): 6}
            alt_map.update({
                getattr(wx, "WXK_NUMPAD1", None): 1,
                getattr(wx, "WXK_NUMPAD2", None): 2,
                getattr(wx, "WXK_NUMPAD3", None): 3,
                getattr(wx, "WXK_NUMPAD4", None): 4,
                getattr(wx, "WXK_NUMPAD5", None): 5,
                getattr(wx, "WXK_NUMPAD6", None): 6,
            })
            if key in alt_map:
                self._poi_detail(alt_map[key]); return True
            if key == ord('P') or key == ord('p'):
                self._refresh_background_pois(); return True
        if not self.street_mode:
            if shift and not primary and not alt and (key == ord('P') or key == ord('p')):
                self._announce_postcode();  return True
        return False

    def _handle_map_shortcuts(self, event, key, shift, primary, alt, no_mod):
        if (not self.street_mode and not getattr(self, "_walking_mode", False)
                and not getattr(self, "_free_mode", False)
                and not getattr(self, "_game", None).active):
            if no_mod and (key == ord('R') or key == ord('r')):
                self._announce_current_region(); return True
            if no_mod and (key == ord('C') or key == ord('c')):
                self._announce_current_country(); return True
            if no_mod and (key == ord('N') or key == ord('n')):
                miab_log("feature_usage", "Key: N (nearest city only)", self.settings)
                self._announce_nearest_city_only(); return True

        if getattr(self, '_nav_active', False):
            # Briefing step-through takes priority over normal nav stepping
            # while a Mistral briefing is loaded.
            if getattr(self, '_nav_briefing_mode', False):
                if key == wx.WXK_UP:
                    self._nav_briefing_next(); return True
                if key == wx.WXK_DOWN:
                    self._nav_briefing_prev(); return True
                if no_mod and (key == ord('I') or key == ord('i')):
                    self._nav_briefing_announce_current(); return True
                # Shift+I while briefing is open also just repeats.
                if shift and not primary and not alt and (key == ord('I') or key == ord('i')):
                    self._nav_briefing_announce_current(); return True
                if key == wx.WXK_ESCAPE:
                    self._nav_briefing_exit(); return True

            if key == wx.WXK_UP:
                self._nav_step_forward(); return True
            if key == wx.WXK_DOWN:
                self._nav_step_back(); return True
            if no_mod and (key == ord('I') or key == ord('i')):
                self._nav_announce_step(); return True
            if shift and not primary and not alt and (key == ord('I') or key == ord('i')):
                self._nav_request_narrative_briefing(); return True
            if no_mod and (key == ord('X') or key == ord('x')):
                self._nav_announce_cross_street(); return True

        page_up = getattr(wx, "WXK_PAGEUP", getattr(wx, "WXK_PRIOR", None))
        page_down = getattr(wx, "WXK_PAGEDOWN", getattr(wx, "WXK_NEXT", None))
        if self.street_mode and key in (page_up, page_down):
            direction = 1 if key == page_down else -1
            if primary and not shift and not alt:
                self._street_survey_go_block(direction)
            elif primary and shift and not alt:
                self._street_survey_turn_cross_street(turn_back=(key == page_up))
            elif not primary and not shift and not alt:
                self._street_survey_go_address(direction)
            else:
                event.Skip()
            return True
        if no_mod and key in (page_up, page_down) and not self.street_mode and not getattr(self, "_walking_mode", False):
            self._cycle_spatial_tones_mode(1 if key == page_down else -1)
            return True

        if no_mod and (key == ord('X') or key == ord('x')):
            if self.street_mode or getattr(self, '_walking_mode', False):
                miab_log("feature_usage", "Key: X (nearest intersection)", self.settings)
                self._announce_nearest_intersection()
            return True
        if no_mod and (key == ord('G') or key == ord('g')):
            miab_log("feature_usage", "Key: G (nearby features)", self.settings)
            self._announce_nearby_features(); return True
        if no_mod and (key == ord('P') or key == ord('p')):
            miab_log("feature_usage", "Key: p (nearby menu)", self.settings)
            self._show_poi_category_dialog(); return True

        if (not self.street_mode and not getattr(self, '_walking_mode', False)):
            if no_mod and (key == ord('T') or key == ord('t')):
                miab_log("feature_usage", "Key: T (local time)", self.settings)
                self.announce_time();  return True
            if no_mod and (key == ord('Z') or key == ord('z')):
                miab_log("feature_usage", "Key: Z (timezone)", self.settings)
                self._announce_timezone(); return True
            if no_mod and (key == ord('S') or key == ord('s')):
                miab_log("feature_usage", "Key: S (sunrise/sunset)", self.settings)
                self._announce_sunrise_sunset(); return True
            if shift and (key == ord('4') or key == ord('$')):
                miab_log("feature_usage", "Key: $ (currency)", self.settings)
                self._announce_currency(); return True
            if primary and alt and key == wx.WXK_UP:
                self._jump_nearest_land("north"); return True
            if primary and alt and key == wx.WXK_DOWN:
                self._jump_nearest_land("south"); return True
            if primary and alt and key == wx.WXK_LEFT:
                self._jump_nearest_land("west"); return True
            if primary and alt and key == wx.WXK_RIGHT:
                self._jump_nearest_land("east"); return True
            if no_mod and (key == ord('A') or key == ord('a')):
                miab_log("feature_usage", "Key: A (nearest airport)", self.settings)
                self._announce_nearest_airport(); return True
            if shift and not primary and (key == ord('A') or key == ord('a')):
                miab_log("feature_usage", "Key: Shift+A (overhead flights)", self.settings)
                self._announce_overhead_flights(); return True
            if shift and primary and (key == ord('A') or key == ord('a')):
                miab_log("feature_usage", "Key: Ctrl+Shift+A (airport flights)", self.settings)
                self._announce_airport_flights(); return True
            if shift and not primary and key == wx.WXK_F1:
                miab_log("feature_usage", "Key: Shift+F1 (capital city)", self.settings)
                self._announce_capital(); return True
            if no_mod and (key == ord('W') or key == ord('w')):
                miab_log("feature_usage", "Key: W (weather)", self.settings)
                self._announce_weather(); return True
            if no_mod and (key == ord('Q') or key == ord('q')):
                miab_log("feature_usage", "Key: Q (air quality)", self.settings)
                self._announce_air_quality(); return True
        return False

    def _handle_street_mode_shortcuts(self, key, shift, primary, alt, no_mod, event):
        if not self.street_mode:
            return False
        if primary and (key == ord('W') or key == ord('w')):
            self._open_poi_website(); return True
        if no_mod and (key == ord('W') or key == ord('w')):
            self._walk_toggle();  return True
        if no_mod and (key == ord('P') or key == ord('p')):
            self._announce_poi_count();  return True
        if no_mod and (key == ord('A') or key == ord('a')):
            self._announce_address();    return True
        if no_mod and (key == ord('S') or key == ord('s')):
            self._street_search()
            return True
        if primary and (key == ord('G') or key == ord('g')):
            self._nav_to_address()
            return True
        if no_mod and (key == ord('I') or key == ord('i')):
            self._announce_position_info()
            return True
        if no_mod and (key == ord('H') or key == ord('h')):
            if getattr(self, '_walking_mode', False):
                heading = self._walk_compass_name(getattr(self, '_walk_heading', 0))
                self._announce_transient(f"Heading {heading}.")
            return True
        if no_mod and (key == ord('R') or key == ord('r')):
            if getattr(self, '_walking_mode', False):
                self._walk_turnaround()
            elif self._game.active:
                self._game.repeat_target()
            return True
        if key == wx.WXK_RETURN or key == wx.WXK_NUMPAD_ENTER:
            if primary:
                if self._street_confirm_explore(): return True
            else:
                if self._street_confirm_jump(): return True
        if key == wx.WXK_SPACE:
            if getattr(self, '_pending_snap_lat', None) is not None:
                self.lat = self._pending_snap_lat
                self.lon = self._pending_snap_lon
                self._pending_snap_lat = None
                self._pending_snap_lon = None
                wx.CallAfter(self.map_panel.set_position, self.lat, self.lon, True, "")
                wx.CallAfter(self._update_street_display)
                return True
            if getattr(self, '_pending_street_download', False):
                self._download_new_area()
                return True
            self._announce_poi_crossing();  return True

        if getattr(self, '_walking_mode', False):
            if key == wx.WXK_UP:
                if getattr(self, '_walk_browsing', False):
                    self._walk_browsing = False
                    if self._walk_commit_turn(announce=False):
                        self._walk_forward()
                        return True
                self._walk_forward();  return True
            if key == wx.WXK_DOWN:
                if getattr(self, '_walk_browsing', False):
                    self._walk_browsing = False
                    self._walk_turn_options = []
                    self._walk_option_idx = None
                self._walk_backward();  return True
            if key == wx.WXK_LEFT:
                self._walk_turn_left();  return True
            if key == wx.WXK_RIGHT:
                self._walk_turn_right();  return True
        return False

    def on_key(self, event):
        key   = event.GetKeyCode()
        shift = event.ShiftDown()
        primary = _primary_down(event)
        alt = event.AltDown()
        if getattr(self, "_suppress_location_restore", False):
            _log_key_event(self, event, "frame", "suppressed while restoring location")
            return
        # True when no modifier is held — used to prevent bare letter/F-key
        # handlers from firing on modifier shortcuts.
        no_mod = not shift and not primary and not alt
        _log_key_event(self, event, "frame", f"street_mode={self.street_mode} walking={getattr(self, '_walking_mode', False)} nav={getattr(self, '_nav_active', False)}")

        if self._handle_preface_shortcuts(event, key, shift, primary, alt, no_mod):
            return
        if self._handle_free_mode_shortcuts(key, shift, primary, alt, no_mod):
            return

        if self.street_mode:
            if primary:
                step = 0.0027      # ~300m — jump to next block
            elif shift:
                step = 0.00018     # ~20m — fine positioning
            else:
                step = 0.00072     # ~80m — normal walking pace
        elif primary:
            step = 3.0
        elif shift:
            step = 0.009      # ~1km — fine map movement
        else:
            step = 0.02       # ~2km — suburb-scale map movement

        if self._handle_global_function_keys(key, shift, primary, alt, no_mod):
            return
        if self._handle_global_map_shortcuts(key, shift, primary, alt, no_mod):
            return
        if self._handle_map_shortcuts(event, key, shift, primary, alt, no_mod):
            return
        if self._handle_street_mode_shortcuts(key, shift, primary, alt, no_mod, event):
            return

        moved = False
        new_lat = self.lat
        new_lon = self.lon
        # Block map movement when hub list is open
        if (not self.street_mode and bool(self._poi_list)
                and key in (wx.WXK_UP, wx.WXK_DOWN,
                            wx.WXK_LEFT, wx.WXK_RIGHT)):
            self._sync_poi_selection_from_listbox()
            if key == wx.WXK_UP:
                self._poi_index = max(0, self._poi_index - 1)
            elif key == wx.WXK_DOWN:
                self._poi_index = min(len(self._poi_list) - 1,
                                      self._poi_index + 1)
            self.listbox.SetSelection(self._poi_index)
            return

        if key == wx.WXK_UP:
            new_lat = min(90, self.lat + step)
        elif key == wx.WXK_DOWN:
            new_lat = max(-90, self.lat - step)
        elif key == wx.WXK_LEFT:
            new_lon = ((self.lon - step + 180) % 360) - 180
        elif key == wx.WXK_RIGHT:
            new_lon = ((self.lon + step + 180) % 360) - 180

        if new_lat != self.lat or new_lon != self.lon:
            test_label = "No street data nearby"
            # In street mode, check if new location has streets before moving
            if self.street_mode and self._road_segments:
                pinned_street = getattr(self, "_jump_street_label", None)
                if pinned_street:
                    pinned_snap = self._nearest_street_point(
                        new_lat, new_lon, pinned_street)
                    if pinned_snap and pinned_snap[0] <= 120.0:
                        _snap_dist, new_lat, new_lon, test_label = pinned_snap
                        self._jump_street_pin_lat = new_lat
                        self._jump_street_pin_lon = new_lon
                        miab_log("snap",
                                 f"arrow move: following pinned street '{pinned_street}' "
                                 f"via snap {pinned_snap[0]:.1f}m to ({new_lat:.5f},{new_lon:.5f})",
                                 self.settings)
                # Check if streets exist at new location
                if test_label == "No street data nearby":
                    test_label, _ = self._street_fetcher.nearest_road(new_lat, new_lon, self._road_segments)
                miab_log("snap",
                         f"arrow move: ({self.lat:.5f},{self.lon:.5f})→({new_lat:.5f},{new_lon:.5f}); "
                         f"nearest='{test_label}'; pin='{getattr(self,'_jump_street_label',None)}'",
                         self.settings)
                
            
            # Check if movement lands in water — but trust OSM road data over
            # the coarse land polygon (peninsulas like Wellington Point are often
            # misclassified as water by the polygon).
            if self.street_mode:
                if not _IS_LAND(new_lat, new_lon):
                    already_in_water = not _IS_LAND(self.lat, self.lon)
                    has_roads = (self._road_segments and
                                 test_label not in ("No street data", "No street data nearby"))
                    if not already_in_water and not has_roads:
                        return
            
            # Hard barrier in street mode - block ALL arrow movement beyond loaded area
            if self.street_mode and self._road_fetch_lat is not None:
                if self._street_boundary_move(new_lat, new_lon):
                    return
            self.lat = new_lat
            self.lon = new_lon
            moved = True
        if moved:
            self._street_survey_current_poi = None
            # Keep the visual map and coordinate panel responsive while the
            # slower place/country lookup runs in the background.
            self.map_panel.set_position(
                self.lat, self.lon, self.street_mode, self.street_label)
            self._refresh_info_panel()

            # Spatial tone only for world map, not street/walking mode
            if not self._game.active and not self.street_mode and not getattr(self, '_walking_mode', False):
                self._play_spatial_tone_if_allowed(
                    self.lat, self.lon, self._spatial_tone_bounds())
            
            # Street mode: check cache validity and trigger fetch if needed
            if self.street_mode:
                self._check_cache_validity()

            # CRITICAL: Query cache on EVERY movement for immediate feedback
            if self.street_mode:
                self._update_street_display()
            
            # Background: Refresh cache only when threshold crossed
            if self._should_fetch(self.lat, self.lon, force=False):
                self._fetch_in_progress = True
                self._distance_since_fetch = 0.0

                if self.street_mode:
                    threading.Thread(target=self._query_street, daemon=True).start()
                else:
                    threading.Thread(target=self._lookup, daemon=True).start()
            if self.street_mode:
                self._prev_lat = self.lat
                self._prev_lon = self.lon
        else:
            event.Skip()

    def _place_between_context(self, current_idx, current_km):
        """Return 'between X and Y' context when a neighbour is similarly close."""
        try:
            current_row = self.df.iloc[current_idx]
            current_city = str(current_row["city"])
            current_country = str(current_row["country"])
        except Exception:
            return ""
        if (not current_city or current_city.lower() == "nan"
                or current_km < 1.0 or current_km > 18.0):
            return ""

        lat0, lon0 = self.lat, self.lon
        radius_km = max(8.0, min(25.0, current_km * 1.8))
        radius_deg = radius_km / 111.0
        gy_min = int(math.floor((lat0 - radius_deg) * 10))
        gy_max = int(math.floor((lat0 + radius_deg) * 10))
        gx_min = int(math.floor((lon0 - radius_deg) * 10))
        gx_max = int(math.floor((lon0 + radius_deg) * 10))
        best = None

        for gy in range(gy_min, gy_max + 1):
            for gx in range(gx_min, gx_max + 1):
                for i in self._city_grid.get((gy, gx), []):
                    if i == current_idx:
                        continue
                    row = self.df.iloc[i]
                    city = str(row["city"])
                    country = str(row["country"])
                    if (not city or city.lower() == "nan"
                            or city == current_city
                            or country != current_country):
                        continue
                    km = dist_km(lat0, lon0, float(row["lat"]), float(row["lng"]))
                    if km > radius_km:
                        continue
                    if km <= max(current_km + 2.0, current_km * 1.35):
                        score = (abs(km - current_km), km)
                        if best is None or score < best[0]:
                            best = (score, city)

        if best:
            return f"between {current_city} and {best[1]}"
        return ""

    def _close_place_position_context(self, centre_lat, centre_lon, lat, lon,
                                      current_idx=None):
        """Describe where the cursor sits when a place label would repeat."""
        km = dist_km(centre_lat, centre_lon, lat, lon)
        if current_idx is not None:
            between = self._place_between_context(current_idx, km)
            if between:
                return between
        if km < 0.4:
            return "near centre"
        direction = compass_name(bearing_deg(centre_lat, centre_lon, lat, lon))
        direction = direction.replace("-", " ")
        if km < 1.2:
            return f"{direction} side"
        return f"{format_distance(km * 1000)} {direction} of centre"

    def _lookup(self):
        try:
            # ── Latitude-line and Date Line crossing announcements ─────────
            prev_lat = self._prev_lat
            prev_lon = self._prev_lon
            cur_lat  = self.lat
            cur_lon  = self.lon
            lookup_lat = cur_lat
            lookup_lon = cur_lon

            def _lookup_is_stale() -> bool:
                return (abs(self.lat - lookup_lat) > 0.0002
                        or abs(self.lon - lookup_lon) > 0.0002)

            if not getattr(self, 'street_mode', False) and \
               not getattr(self, '_walking_mode', False) and \
               not getattr(self, '_nav_active', False):

                # Latitude lines
                if (self.settings.get("announce_climate_zones", True)
                        and prev_lat is not None and prev_lat != cur_lat):
                    _LINES = (0, 23.5, 66.5, -23.5, -66.5)
                    for lat_line in _LINES:
                        if (prev_lat < lat_line <= cur_lat) or (cur_lat <= lat_line < prev_lat):
                            miab_log("navigation", f"Crossed latitude line {lat_line}°.", self.settings)
                            break

                # International Date Line — large longitude jump signals crossing
                if prev_lon is not None and abs(cur_lon - prev_lon) > 300:
                    miab_log("navigation", "Crossed the International Date Line.", self.settings)

            self._prev_lat = cur_lat
            self._prev_lon = cur_lon

            dist, idx = _nearest_city(self._city_lats, self._city_lons, self.lat, self.lon)
            country = "Open Water"

            # Antarctica has no cities — detect purely by latitude
            if self.lat < -60.0:
                country = "Antarctica"
                if _lookup_is_stale():
                    return
                if country != self.last_country_found:
                    self.last_city_found    = ""
                    self.last_state_found   = ""
                    self.last_country_found = country
                    self.current_continent  = "Antarctica"
                    self.last_location_str  = "Antarctica"
                    wx.CallAfter(self._refresh_info_panel)
                    wx.CallAfter(self._update_location_focus, "Antarctica")
                    if getattr(self, 'sounds_enabled', True) and not self._game.active:
                        self._play_location_sound_if_allowed("Antarctica", "Antarctica")
                wx.CallAfter(self.map_panel.set_position, self.lat, self.lon,
                             self.street_mode, self.street_label)
                if self._game.active:
                    if country == self._game.target_country:
                        elapsed = time.time() - self._game._start_time
                        if self._session and self._session.active:
                            self._game.active = False
                            self._game._generation += 1
                            miab_log("challenges",
                                     f"Session win: country={country} time={elapsed:.1f}s",
                                     self.settings)
                            wx.CallAfter(self._session.on_win, elapsed, self.df, self.lat, self.lon)
                            wx.CallAfter(lambda c=country: wx.CallLater(2000, lambda: self._play_location_sound_if_allowed(c)))
                        else:
                            miab_log("challenges",
                                     f"Solo win: country={country} time={elapsed:.1f}s "
                                     f"score={max(0, 180 - int(elapsed))}",
                                     self.settings)
                            wx.CallAfter(self._game.on_win)
                            wx.CallAfter(lambda c=country: wx.CallLater(2000, lambda: self._play_location_sound_if_allowed(c)))
                    else:
                        self._game.on_move(self.lat, self.lon)
                return

            DENSE_COUNTRIES = {"Luxembourg", "Monaco", "Singapore", "Bahrain",
                               "Malta", "Maldives", "San Marino", "Liechtenstein"}

            polygon_country = ""
            country_lookup = getattr(self, "_country_at_point", None)
            if callable(country_lookup):
                country_key = (round(self.lat, 2), round(self.lon, 2))
                if country_key == getattr(self, "_last_country_lookup_key", None):
                    polygon_country = getattr(self, "_last_country_lookup_value", "")
                else:
                    polygon_country = country_lookup(self.lat, self.lon)
                    self._last_country_lookup_key = country_key
                    self._last_country_lookup_value = polygon_country
            forced_country = ""
            if time.time() < getattr(self, "_forced_country_until", 0):
                flat = getattr(self, "_forced_country_lat", None)
                flon = getattr(self, "_forced_country_lon", None)
                if flat is not None and flon is not None:
                    if abs(self.lat - flat) < 0.01 and abs(self.lon - flon) < 0.01:
                        forced_country = getattr(self, "_forced_country_name", "")
            if forced_country:
                polygon_country = forced_country

            on_polygon_land = _IS_LAND(self.lat, self.lon)
            nearest_country = str(self.df.iloc[idx]['country'])

            if not _GEO_LAND_POLYGONS:
                for threshold in (0.1, 0.3, 0.5, 1.0, 2.0):
                    if dist < threshold:
                        break
                else:
                    threshold = 0.0

                if nearest_country in DENSE_COUNTRIES:
                    threshold = min(threshold, 0.3)
                if nearest_country == "Australia" and self.lat > -11.0:
                    threshold = min(threshold, 1.0)
            else:
                threshold = 0.0

            on_land = bool(polygon_country) or on_polygon_land or (threshold > 0.0 and dist < threshold) or dist * 111.0 <= PLACE_NAME_CLOSE_KM
            dist_km = dist * 111.0
            close_place = False
            _only_region = False

            if on_land:
                row = self.df.iloc[idx]
                city, state, city_country = (
                    str(row['city']),
                    str(row['admin_name']),
                    str(row['country']),
                )
                if polygon_country:
                    country = polygon_country
                    city_matches_country = (
                        city_country.lower() == country.lower()
                        or COUNTRY_ALIASES.get(city_country, city_country).lower()
                           == COUNTRY_ALIASES.get(country, country).lower()
                    )
                else:
                    country = city_country
                    city_matches_country = True
                def _with_nearby_town(feature_label: str) -> str:
                    if not feature_label or not city_matches_country:
                        return feature_label
                    if city and city.lower() != 'nan':
                        if feature_label.endswith((" Homestead", " Farm", " Farms")):
                            if dist_km <= 1.0:
                                return f"{feature_label}, {city}"
                            if dist_km <= 3.0:
                                return f"{feature_label}, near {city}"
                    return feature_label
                close_place = city_matches_country and dist_km <= PLACE_NAME_CLOSE_KM
                prev_state   = getattr(self, 'last_state_found', '')
                prev_country = self.last_country_found
                self.last_city_found = (
                    city if close_place and city and city != 'nan' else ""
                )
                # Store the worldcities coordinate for the found city so that
                # _try_enter_street_mode can geocode from the suburb's own
                # location rather than the cursor position.
                if self.last_city_found:
                    self._last_city_found_lat = self._city_lats[idx]
                    self._last_city_found_lon = self._city_lons[idx]
                else:
                    self._last_city_found_lat = None
                    self._last_city_found_lon = None
                self.last_state_found = (
                    state if city_matches_country and state and state != 'nan' else ""
                )

                if city_matches_country:
                    country_code = getattr(self, "_current_country_code", None)
                    context = self._geo_context_items(
                        self.lat, self.lon, limit=1, country_code=country_code)
                    feature = self._geo_lookup_precise(
                        self.lat, self.lon, country_code=country_code)
                    if close_place:
                        parts = []
                        if city and city.lower() != 'nan':
                            parts.append(city)
                        if state and state.lower() != 'nan' and state != prev_state:
                            parts.append(state)
                        if country and country.lower() != 'nan' and country != prev_country:
                            parts.append(country)
                        label = ", ".join(parts) if parts else city
                    elif feature:
                        label = _with_nearby_town(feature)
                    elif context:
                        label = ". ".join(context)
                    elif not self._geo_features_enabled():
                        label = self._nearest_city_distance_label()
                    elif city and city.lower() != "nan" and dist_km <= NEAREST_PLACE_FALLBACK_KM:
                        label = f"{city} {format_distance(dist_km * 1000)}"
                    else:
                        parts = []
                        if state and state.lower() != "nan":
                            parts.append(state)
                        if country and country.lower() != "nan":
                            parts.append(country)
                        label = ", ".join(parts) if parts else "Location unknown"
                        _only_region = True
                else:
                    country_code = getattr(self, "_current_country_code", None)
                    context = self._geo_context_items(
                        self.lat, self.lon, limit=1, country_code=country_code)
                    feature = self._geo_lookup_precise(
                        self.lat, self.lon, country_code=country_code)
                    if feature:
                        label = _with_nearby_town(feature)
                    elif context:
                        label = ". ".join(context)
                    elif not self._geo_features_enabled():
                        label = self._nearest_city_distance_label()
                    else:
                        label = country if country and country.lower() != "nan" else "Location unknown"
                        _only_region = True
            else:
                # Named bays, islands and coastal features are often part of the
                # user's local country context, so keep the nearby country sound.
                country_code = getattr(self, "_current_country_code", None)
                context = self._geo_context_items(
                    self.lat, self.lon, limit=1, country_code=country_code)
                coastal_feature = (
                    (context[0] if context else "")
                    or self._geo_lookup_precise(
                        self.lat, self.lon, country_code=country_code)
                    or self._geo_lookup_any(
                        self.lat, self.lon, country_code=country_code)
                )
                if coastal_feature:
                    label   = coastal_feature
                    country = nearest_country if nearest_country and nearest_country.lower() != "nan" else "Open Water"
                else:
                    label   = self._ocean_name(self.lat, self.lon)
                    if dist_km <= 75.0 and nearest_country and nearest_country.lower() != "nan":
                        country = nearest_country
                    else:
                        country = "Open Water"

            pinned_label = getattr(self, "_pinned_jump_label", "")
            if pinned_label and time.time() < getattr(self, "_pinned_jump_label_until", 0):
                display = pinned_label
                display_base = display
            else:
                self._pinned_jump_label = ""
                self._pinned_jump_label_until = 0
                cached_label = self._nearby_cached_place_label(self.lat, self.lon)
                display_base = cached_label or label
                display = display_base
                if (not cached_label and close_place
                        and display_base == getattr(self, "_last_location_base", "")):
                    try:
                        context = self._close_place_position_context(
                            float(row["lat"]), float(row["lng"]),
                            self.lat, self.lon, idx)
                    except Exception:
                        context = ""
                    if context:
                        display = context if context.startswith("between ") else f"{display_base}, {context}"
            if _lookup_is_stale():
                return

            self._last_location_base = display_base
            self.last_location_str = display
            self._set_current_location_title(display)
            wx.CallAfter(self._refresh_info_panel)
            if (display == getattr(self, '_last_jump_display_label', None)
                  and time.time() < getattr(self, '_last_jump_display_until', 0)):
                self._last_jump_display_label = None
                self._last_jump_display_until = 0
            else:
                wx.CallAfter(self._update_location_focus, display)

            if self._game.active:
                if country == self._game.target_country:
                    elapsed = time.time() - self._game._start_time
                    if self._session and self._session.active:
                        self._game.active = False
                        self._game._generation += 1
                        miab_log("challenges",
                                 f"Session win: country={country} time={elapsed:.1f}s",
                                 self.settings)
                        wx.CallAfter(self._session.on_win, elapsed, self.df, self.lat, self.lon)
                        wx.CallAfter(lambda c=country: wx.CallLater(2000, lambda: self._play_location_sound_if_allowed(c)))
                    else:
                        miab_log("challenges",
                                 f"Solo win: country={country} time={elapsed:.1f}s "
                                 f"score={max(0, 180 - int(elapsed))}",
                                 self.settings)
                        wx.CallAfter(self._game.on_win)
                        wx.CallAfter(lambda c=country: wx.CallLater(2000, lambda: self._play_location_sound_if_allowed(c)))
                else:
                    self._game.on_move(self.lat, self.lon)
            else:
                canonical = COUNTRY_ALIASES.get(country, country)
                if country != self.last_country_found:
                    self.last_country_found = country
                    if country == "Antarctica":
                        continent = "Antarctica"
                    else:
                        # Check continent override first (for territories in different region to parent)
                        continent = CONTINENT_OVERRIDES.get(country, "")
                        if not continent:
                            for info in self.facts.values():
                                if info.get('name', '').lower() in (canonical.lower(), country.lower()):
                                    continent = info.get('continent', '')
                                    break
                    self.current_continent = continent
                    wx.CallAfter(self._refresh_info_panel)
                    self._prefetch_geo_features_for_point(self.lat, self.lon)
                    if getattr(self, 'sounds_enabled', True):
                        self._play_location_sound_if_allowed(
                            country if country != "Open Water" else "ocean", continent)
                    miab_log("navigation",
                             f"Entered country: {country}"
                             + (f" (continent: {continent})" if continent else ""),
                             self.settings)
                    self._current_subregion = ""

            wx.CallAfter(self.map_panel.set_position, self.lat, self.lon,
                         self.street_mode, self.street_label)
        finally:
            # Always clear fetch flag, even on error or early return
            self._fetch_in_progress = False

    def _ocean_name(self, lat, lon):
        """Return the name of the ocean corresponding to a lat/lon point."""
        # Tasmania needs a special split: east of South East Cape is Tasman Sea,
        # west/southwest uses the southern-ocean convention.
        if -43.6 <= lat <= -40.0:
            if lon >= 146.8:
                return "Tasman Sea"
            return "Southern Ocean (Australia)"
        for name, boxes in KNOWN_OCEANS.items():
            for lat_min, lat_max, lon_min, lon_max in boxes:
                if lat_min <= lat <= lat_max:
                    if (lon_min < lon_max and lon_min <= lon <= lon_max) or \
                       (lon_min > lon_max and (lon >= lon_min or lon <= lon_max)):
                        return name
        return "Open Water"

    def _start_challenge_session(self):
        """Ctrl+F10 — set up and start a scored multi-round challenge session."""
        if self.df is None or self.df.empty:
            self._announce_transient_then_return("No city data available for the challenge.")
            return
        if self._game.active or (self._session and self._session.active):
            self._announce_transient_then_return("A challenge is already active. Press F10 to stop it first.")
            return

        dlg = wx.Dialog(self, title="Challenge Setup",
                        style=wx.DEFAULT_DIALOG_STYLE)
        panel = wx.Panel(dlg)
        vs = wx.BoxSizer(wx.VERTICAL)

        vs.Add(wx.StaticText(panel, label="Player 1 name:"), 0, wx.LEFT | wx.TOP, 8)
        txt_p1 = wx.TextCtrl(panel, style=wx.TE_PROCESS_ENTER)
        vs.Add(txt_p1, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        vs.Add(wx.StaticText(panel, label="Player 2 name (leave blank for solo):"), 0, wx.LEFT, 8)
        txt_p2 = wx.TextCtrl(panel, style=wx.TE_PROCESS_ENTER)
        vs.Add(txt_p2, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        vs.Add(wx.StaticText(panel, label="Rounds each player:"), 0, wx.LEFT, 8)
        spin = wx.SpinCtrl(panel, value="3", min=1, max=10)
        vs.Add(spin, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        hs = wx.BoxSizer(wx.HORIZONTAL)
        btn_ok     = wx.Button(panel, wx.ID_OK,     "Start")
        btn_cancel = wx.Button(panel, wx.ID_CANCEL, "Cancel")
        hs.Add(btn_ok, 0, wx.RIGHT, 8)
        hs.Add(btn_cancel)
        vs.Add(hs, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        panel.SetSizer(vs)
        vs.Fit(dlg)
        dlg.CentreOnParent()

        txt_p1.Bind(wx.EVT_TEXT_ENTER, lambda e: txt_p2.SetFocus())
        txt_p2.Bind(wx.EVT_TEXT_ENTER, lambda e: spin.SetFocus())
        spin.Bind(wx.EVT_TEXT_ENTER,   lambda e: dlg.EndModal(wx.ID_OK))

        dlg.Bind(wx.EVT_CHAR_HOOK,
                 lambda e: (self._suppress_map_focus_repeat(800), dlg.EndModal(wx.ID_CANCEL))[1]
                 if e.GetKeyCode() == wx.WXK_ESCAPE else e.Skip())

        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            self.listbox.SetFocus()
            return

        p1     = txt_p1.GetValue().strip() or "Player 1"
        p2     = txt_p2.GetValue().strip()
        rounds = spin.GetValue()
        dlg.Destroy()

        players = [p1, p2] if p2 else [p1]
        self._session = ChallengeSession(
            game          = self._game,
            announce_cb   = lambda msg: wx.CallAfter(self._status_update, msg, True),
            players       = players,
            rounds        = rounds,
            on_complete   = lambda: wx.CallAfter(self._on_session_complete),
            wait_cb       = lambda msg: wx.CallAfter(self._status_update, msg, True),
            stop_sound_cb = self.sound.stop,
            log_cb        = lambda msg: miab_log("challenges", msg, self.settings),
        )
        self.sound.stop()
        # Route timeouts through the session
        self._game._timeout_cb = lambda: wx.CallAfter(
            self._session.on_timeout, self.df, self.lat, self.lon)
        self._game._current_continent_cb = lambda: getattr(self, 'current_continent', '')
        self._game._current_subregion_cb = lambda: getattr(self, '_current_subregion', '')
        self._session.start(self.df, self.lat, self.lon)

    def _challenge_country_info(self, country):
        """Return local (continent, subregion) data for challenge milestones."""
        canonical = COUNTRY_ALIASES.get(country, country).lower()
        for info in self.facts.values():
            if info.get('name', '').lower() in (canonical, country.lower()):
                return info.get('continent', ''), info.get('subregion', '')
        return "", ""

    def _on_session_complete(self):
        self._session = None
        self._game._timeout_cb = None
        self._resume_location_sound()
        self.listbox.SetFocus()

    # ------------------------------------------------------------------
    # F12 Tools menu — detour calculator, route explorer, toll compare, journey planner
    # ------------------------------------------------------------------

    def _operator_map_path(self):
        """Path to the operator → feed_id mapping file."""
        return os.path.join(self._transit._cache_dir(), "gtfs_operator_map.json")

    def _load_operator_map(self) -> dict:
        """Load the operator → feed_id mapping from JSON."""
        p = self._operator_map_path()
        if not os.path.exists(p):
            return {}
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_operator_map(self, operator_key: str, feed_id: str):
        """Save an operator → feed_id mapping to JSON."""
        op_map = self._load_operator_map()
        op_map[operator_key] = feed_id
        p = self._operator_map_path()
        try:
            with open(p, "w", encoding="utf-8") as f:
                json.dump(op_map, f, indent=2)
            miab_log("api_calls", f"[GTFS] Saved operator map: '{operator_key}' → feed {feed_id}", getattr(self, "settings", None))
        except Exception as exc:
            miab_log("errors", f"[GTFS] Failed to save operator map: {exc}", getattr(self, "settings", None))

    def _resume_location_sound(self):
        """Re-start the country/region ambient sound and refresh the UI label."""
        if getattr(self, "_suppress_location_restore", False):
            self._verbose_trace("_resume_location_sound suppressed while restoring location.")
            return
        # Only the map surface should make these sounds — not while a dialog
        # (tools menu, journey results, accessible route) is still in front.
        if getattr(self, "_update_dialog_active", False):
            self._verbose_trace("_resume_location_sound suppressed while update dialog is active.")
            return
        if isinstance(wx.GetActiveWindow(), wx.Dialog):
            self._verbose_trace("_resume_location_sound suppressed: a dialog is in front.")
            return
        country = getattr(self, 'last_country_found', '')
        continent = getattr(self, 'current_continent', '')
        restored_sound = False
        if country and country != "Open Water":
            self._play_location_sound_if_allowed(country, continent)
            restored_sound = True
        if restored_sound and getattr(self, "_tools_workflow_active", False):
            # A tool-specific exit restored the saved sound successfully;
            # prevent the dispatcher fallback from restoring it a second time.
            self._tools_sound_was_on = False

    def _map_help_lines(self) -> list[str]:
        """Return the map-mode shortcut lines used by both help and docs."""
        return [
            "Arrow keys: move around the map.",
            "Shift+arrows: fine movement.",
            "Ctrl+arrows: move in large steps (~333km) for fast long-distance navigation.",
            "Ctrl+Alt+arrows: jump to the nearest foreign country in that direction.",
            "F2: repeat last landed object. Double-tap: spell it out. Triple-tap: copy to clipboard.",
            "Shift+F2: climate zone.",
            "F3: latitude.",
            "F4: longitude.",
            "Shift+F3: volume down.",
            "Shift+F4: volume up.",
            "F5: continent.",
            "F6: country facts.",
            "Shift+F6: Wikipedia summary.",
            "F7: toggle sounds.",
            "F8: flash country on map.",
            "Shift+F8: cycle world view and country view.",
            "F9: toggle Visual Assist mode.",
            "F10: country discovery challenge.",
            "Ctrl+F10: scored challenge session.",
            "Shift+F10: repeat challenge target.",
            "F11: street mode.",
            "Shift+F11: pre-download streets.",
            "Ctrl+Shift+F11: download city data (commonly explored cities/regions).",
            "Page Up/Page Down: cycle spatial tones between world, country, and region.",
            "F12: tools menu.",
            "Ctrl+Shift+F12: report GNAF address state; press again within five seconds to toggle.",
            "Ctrl+F: favourites.",
            "Ctrl+Shift+F: add current place to favourites.",
            "J: jump to city, country, or coordinates.",
            "Ctrl+J: jump to a saved mark.",
            "Shift+F5: toggle GeoFeatures on/off.",
            "N: nearest city only.",
            "Ctrl+M: save current position as mark (then press 1, 2, or 3 to choose a slot).",
            "Ctrl+Shift+P: save current position as a personal POI.",
            "Ctrl+Shift+M: clear a mark (then press 1, 2, or 3).",
            "Ctrl+1, Ctrl+2, Ctrl+3: read a mark's distance from here.",
            "Shift+Alt+M: compare distances between all saved marks.",
            "G: nearby geographic features.",
            "P: POI search.",
            "POI menu: selected POI address, hours, phone, website, Mistral, menu lookup, and website launch.",
            "T: local time.",
            "Z: timezone.",
            "S: sunrise and sunset.",
            "Ctrl+Shift+S: satellite view.",
            "Ctrl+Shift+Alt+S: street view of selected POI (falls back to satellite if no coverage).",
            "Shift+A: overhead flights.",
            "Q: air quality.",
            "L: latitude and longitude.",
            "Shift+L: languages.",
            "Shift+F1: capital city.",
            "$: currency.",
            "W: weather or sea temperature.",
            "Ctrl+comma: settings.",
            "F1: help.",
        ]

    def show_help(self):
        """F1 — show keyboard help in a read-only scrollable dialog."""
        if getattr(self, '_free_mode', False):
            title = "FREE MODE HELP"
            lines = [
                "Up: move forward.",
                "Down: move backward.",
                "Left: describe POIs on the left.",
                "Right: describe POIs on the right.",
                "Ctrl+Left: snap to nearest cross street.",
                "Ctrl+Right: snap to nearest cross street.",
                "H: current heading.",
                "X: nearest intersection.",
                "G: nearby features.",
                "A: address lookup.",
                "R: reverse direction.",
                "F: leave free mode.",
                "Ctrl+Alt+P: refresh POIs.",
                "Ctrl+F: favourites.",
                "Ctrl+Shift+F: add selected POI or current place to favourites.",
                "Ctrl+J: jump to a saved mark.",
                "Ctrl+Shift+P: save current position as a personal POI.",
                "Delete: Delete POI.",
                "F2: Rename POI.",
                "F1: help.",
            ]
        elif getattr(self, '_walking_mode', False):
            title = "WALKING MODE HELP"
            lines = [
                "Up: walk forward.",
                "Down: walk back.",
                "Left: browse turn options.",
                "Right: browse turn options.",
                "Up after browsing: commit the turn and walk.",
                "R: turn around.",
                "H: current heading.",
                "X: nearest intersection.",
                "G: nearby features.",
                "A: address lookup.",
                "P: POI search.",
                "Ctrl+Alt+P: refresh POIs.",
                "Ctrl+F: favourites.",
                "Ctrl+Shift+F: add current place to favourites.",
                "Ctrl+J: jump to a saved mark.",
                "Ctrl+Shift+P: save current position as a personal POI.",
                "W: leave walking mode.",
                "F1: help.",
            ]
        elif self.street_mode:
            title = "STREET MODE HELP"
            lines = [
                "Arrow keys: move along the street map.",
                "Shift+arrows: fine movement.",
                "Ctrl+arrows: larger movement.",
                "Page Up: previous known house number.",
                "Page Down: next known house number.",
                "Ctrl+F12: cycle address mode: POI names, plain numbers, POI numbers only.",
                "Ctrl+Alt+F12: cycle house-number filter between all, odd only, and even only.",
                "Ctrl+Page Up: previous intersection.",
                "Ctrl+Page Down: next intersection.",
                "Ctrl+Shift+Page Down: turn onto the cross street.",
                "Ctrl+Shift+Page Up: turn back onto the abandoned street.",
                "S: street search.",
                "A: address lookup.",
                "P: POI search.",
                "Ctrl+Alt+P: refresh POIs.",
                "X: nearest cross street.",
                "G: nearby features.",
                "I: street summary.",
                "W: walking mode.",
                "F: free mode.",
                "Ctrl+G: navigate to address.",
                "Ctrl+F: favourites.",
                "Ctrl+Shift+F: add selected POI or current place to favourites.",
                "Ctrl+J: jump to a saved mark.",
                "Ctrl+Shift+P: save current position as a personal POI.",
                "Enter: jump to selected POI.",
                "Ctrl+Enter: transit info or explore selected POI.",
                "Space: nearest intersection for selected POI.",
                "Ctrl+Alt+1: selected POI address.",
                "Ctrl+Alt+2: selected POI hours.",
                "Ctrl+Alt+3: selected POI phone.",
                "Ctrl+Alt+4: selected POI website.",
                "Ctrl+Alt+5: open Google reviews for selected POI in your browser.",
                "Ctrl+Alt+6: search for food venue menu links.",
                "Ctrl+W: open selected POI website.",
                "Escape: close POI list.",
                "Backspace: go back in POI exploration.",
                "F11: return to map mode.",
                "Ctrl+Shift+S: satellite view.",
                "Ctrl+Shift+Alt+S: street view (falls back to satellite if no coverage).",
                "F1: help.",
            ]
        else:
            title = "MAP MODE HELP"
            lines = self._map_help_lines()
        if IS_MAC:
            lines = [
                line.replace("Ctrl+", "Command+").replace("Alt+", "Option+")
                for line in lines
            ]
            lines = [
                "MAC KEYBOARD: Command replaces Ctrl and Option replaces Alt in the shortcuts below.",
                "On Mac, Control+F11 is the verified alternative when bare F11 does not reach the app. Bare F12 opens Tools normally.",
                "Physical Control is not a general substitute for Ctrl. For example, address mode is Command+F12.",
                "",
            ] + lines
        help_text = "MAP IN A BOX - " + title + "\n\n" + "\n".join(lines)
        dlg = wx.Dialog(self, title="Keyboard Help",
                        style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        sizer = wx.BoxSizer(wx.VERTICAL)
        txt = wx.TextCtrl(dlg, value=help_text,
                          style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_AUTO_URL)
        txt.SetMinSize((500, 380))
        txt.SetBackgroundColour(wx.Colour(10, 20, 40))
        txt.SetForegroundColour(wx.Colour(220, 220, 220))
        sizer.Add(txt, 1, wx.EXPAND | wx.ALL, 8)
        btn = wx.Button(dlg, wx.ID_CLOSE, "Close (Escape)")
        sizer.Add(btn, 0, wx.ALIGN_RIGHT | wx.RIGHT | wx.BOTTOM, 8)
        dlg.SetSizer(sizer)
        dlg.Fit()
        dlg.CentreOnParent()
        btn.Bind(wx.EVT_BUTTON, lambda e: dlg.EndModal(wx.ID_CLOSE))
        dlg.Bind(wx.EVT_CHAR_HOOK,
                 lambda e: (self._suppress_map_focus_repeat(800), dlg.EndModal(wx.ID_CLOSE))[1]
                 if e.GetKeyCode() == wx.WXK_ESCAPE else e.Skip())
        dlg.ShowModal()
        dlg.Destroy()

    def _show_about(self):
        """About dialog with the open-source / optional key notice."""
        dlg = wx.Dialog(self, title=f"About {APP_NAME}",
                        style=wx.DEFAULT_DIALOG_STYLE | wx.STAY_ON_TOP)
        vs = wx.BoxSizer(wx.VERTICAL)

        header = wx.StaticText(dlg, label=f"{APP_NAME}\nVersion {APP_VERSION}\nCopyright © 2026 Sam Taylor. Licensed under the MIT License.")
        vs.Add(header, 0, wx.ALL, 14)

        message = (
            "Map in a Box works with free data sources by default and will fall "
            "back to them where it can.\n\n"
            "For richer coverage or higher limits, you can add your own API keys "
            "in Settings."
        )
        txt = wx.StaticText(dlg, label=message)
        txt.Wrap(430)
        vs.Add(txt, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 14)

        btn = wx.Button(dlg, wx.ID_OK, "OK")
        btn.SetDefault()
        vs.Add(btn, 0, wx.ALIGN_RIGHT | wx.RIGHT | wx.BOTTOM, 10)

        dlg.SetSizerAndFit(vs)
        dlg.CentreOnParent()
        dlg.ShowModal()
        dlg.Destroy()

    def _poi_lat_lon_if_focused(self) -> tuple[float, float]:
        """Return the selected POI's lat/lon when the POI list is open and focused,
        otherwise return the current cursor position."""
        poi_list_open = bool(getattr(self, '_poi_list', []))
        listbox = getattr(self, 'listbox', None)
        is_listbox_focused = listbox is not None and listbox.HasFocus()
        if poi_list_open and is_listbox_focused:
            self._sync_poi_selection_from_listbox()
            idx = getattr(self, '_poi_index', -1)
            pois = getattr(self, '_poi_list', [])
            if 0 <= idx < len(pois):
                poi = pois[idx]
                plat = poi.get('lat')
                plon = poi.get('lon')
                if plat is not None and plon is not None:
                    return float(plat), float(plon)
        return self.lat, self.lon

    def _streetview_at_location(self, lat: float, lon: float):
        """Fetch and display Street View imagery + description at (lat, lon).
        Falls back to satellite if no Street View coverage exists, or an
        open street-level viewer if Google isn't configured."""
        if not lookup_streetview_description:
            self._announce_transient_then_return("Street View module not available.")
            return

        google_key = self.settings.get("google_api_key", "").strip()
        if not google_key:
            self._announce_transient_then_return(
                "Street View is using an open fallback instead of Google.")
            wx.CallLater(2000, self._open_mapillary_view, lat, lon)
            return

        self._status_update("Fetching Street View...", force=True)

        def fetch_and_display():
            try:
                # Pass current travel heading so both images have meaningful
                # direction labels.  _walk_heading is set in walking mode;
                # street mode uses _road_heading if available, else None (→ N/S).
                heading = None
                if getattr(self, '_walking_mode', False):
                    heading = getattr(self, '_walk_heading', None)

                result = lookup_streetview_description(
                    lat, lon,
                    google_api_key=google_key,
                    mistral_client=self._mistral,
                    street_heading=heading,
                    cache_path=os.path.join(CACHE_DIR, "streetview_cache.json"),
                )

                if not result:
                    wx.CallAfter(
                        self._announce_transient_then_return,
                        "No Street View coverage here. Showing satellite instead.")
                    wx.CallAfter(self._schedule_satellite_view, lat, lon)
                    return

                image_bytes_list, description = result
                wx.CallAfter(
                    self._show_image_dialog,
                    "Street View", image_bytes_list, description, lat, lon,
                    dialog_size=(920, 700), single_img_size=(640, 480),
                    multi_img_size=(420, 480), text_min_size=(880, 130))

            except Exception as e:
                miab_log("error", f"Street View lookup failed: {e}", self.settings)
                wx.CallAfter(
                    self._announce_transient_then_return, f"Error: {str(e)[:50]}")

        threading.Thread(target=fetch_and_display, daemon=True).start()

    def _open_mapillary_view(self, lat: float, lon: float) -> None:
        """Open an open street-level viewer as a fallback."""
        try:
            import webbrowser
            url = f"https://www.mapillary.com/app/?lat={lat:.6f}&lng={lon:.6f}&z=17"
            webbrowser.open(url)
        except Exception as exc:
            self._announce_transient_then_return(
                f"Could not open open street-level viewer: {exc}")

    def _schedule_satellite_view(self, lat: float, lon: float, delay_ms: int = 2000) -> None:
        """Queue satellite view on the UI thread after a short delay."""
        wx.CallLater(delay_ms, self._satellite_view_at_location, lat, lon)

    def _satellite_view_at_location(self, lat: float, lon: float):
        """Fetch and display satellite image + description at location."""
        google_key = self.settings.get("google_api_key", "").strip()
        if not google_key:
            self._announce_transient_then_return(
                "Satellite view uses Google imagery and needs a Google API key.")
            return
        self._status_update("Fetching satellite image...", force=True)

        def fetch_and_display():
            try:
                if not lookup_satellite_description:
                    wx.CallAfter(self._announce_transient_then_return, "Satellite module not available.")
                    return
                result = lookup_satellite_description(
                    lat, lon, zoom=15,
                    google_api_key=self.settings.get("google_api_key", ""),
                    mistral_client=self._mistral,
                    cache_path=os.path.join(CACHE_DIR, "satellite_cache.json")
                )

                if not result:
                    wx.CallAfter(self._announce_transient_then_return, "Satellite image unavailable at this location.")
                    return

                image_bytes, description = result
                wx.CallAfter(
                    self._show_image_dialog,
                    "Satellite View", [image_bytes], description, lat, lon,
                    return_focus=True)

            except Exception as e:
                miab_log("error", f"Satellite lookup failed: {e}", self.settings)
                wx.CallAfter(self._announce_transient_then_return, f"Error: {str(e)[:50]}")

        threading.Thread(target=fetch_and_display, daemon=True).start()

    def _show_image_dialog(self, title, image_bytes_list, description, lat, lon,
                            dialog_size=(900, 700), single_img_size=(600, 600),
                            multi_img_size=(420, 480), text_min_size=(850, 150),
                            return_focus=False):
        """Display one or more images with a description in a modal dialog.
        Used for both Street View (may pass 1-2 images) and Satellite View
        (always 1 image)."""
        dlg = wx.Dialog(
            self, title=f"{title} ({lat:.4f}, {lon:.4f})",
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
            size=dialog_size,
        )
        vs = wx.BoxSizer(wx.VERTICAL)

        multi = len(image_bytes_list) > 1
        img_w, img_h = multi_img_size if multi else single_img_size

        img_sizer = wx.BoxSizer(wx.HORIZONTAL)
        for img_bytes in image_bytes_list:
            try:
                pil = Image.open(io.BytesIO(img_bytes))
                pil.thumbnail((img_w, img_h), Image.Resampling.LANCZOS)
                wx_img = wx.Image(pil.width, pil.height)
                wx_img.SetData(pil.convert("RGB").tobytes())
                bmp = wx.StaticBitmap(dlg, bitmap=wx.Bitmap(wx_img))
                img_sizer.Add(bmp, 0, wx.ALL, 6)
            except Exception as e:
                miab_log("errors", f"[UI] {title} image display failed: {e}", getattr(self, "settings", None))
        vs.Add(img_sizer, 0, wx.ALL | wx.CENTER, 4)

        txt = wx.TextCtrl(
            dlg, value=description,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2)
        txt.SetMinSize(text_min_size)
        vs.Add(txt, 1, wx.ALL | wx.EXPAND, 10)

        btn = wx.Button(dlg, wx.ID_CLOSE, "Close")
        dlg.Bind(wx.EVT_BUTTON, lambda e: dlg.EndModal(wx.ID_CLOSE))
        vs.Add(btn, 0, wx.ALL | wx.CENTER, 10)

        dlg.SetSizer(vs)
        dlg.ShowModal()
        dlg.Destroy()

        if return_focus:
            self.listbox.SetFocus()

    def _load_place_cache(self):
        try:
            with open(PLACE_CACHE_PATH, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
        except Exception:
            pass
        return []

    def _save_place_cache(self, places):
        try:
            with open(PLACE_CACHE_PATH, "w", encoding="utf-8") as f:
                json.dump(places, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            miab_log("errors", f"[PlaceCache] Save failed: {exc}", getattr(self, "settings", None))

    def _cache_place_result(self, label, lat, lon):
        places = self._load_place_cache()
        key = label.lower()
        for place in places:
            if str(place.get("label", "")).lower() == key:
                place.update({"label": label, "lat": lat, "lon": lon})
                self._save_place_cache(places)
                return
        places.append({"label": label, "lat": lat, "lon": lon})
        self._save_place_cache(places[-200:])

    def _cached_place_candidates(self, query, sort_key):
        q = query.lower()
        candidates = []
        for place in self._load_place_cache():
            label = str(place.get("label", ""))
            if not label or q not in label.lower():
                continue
            try:
                lat = float(place["lat"])
                lon = float(place["lon"])
            except Exception:
                continue
            candidates.append((label, lat, lon, sort_key(label), "cache"))
        return candidates

    def _nearby_cached_place_label(self, lat, lon, radius_km=5.0):
        best = None
        best_km = radius_km
        for place in self._load_place_cache():
            label = str(place.get("label", ""))
            if not label:
                continue
            try:
                plat = float(place["lat"])
                plon = float(place["lon"])
            except Exception:
                continue
            km = dist_km(lat, lon, plat, plon)
            if km <= best_km:
                best = label
                best_km = km
        return best

    # Classes that represent geographic areas/features — keep existing map-mode behaviour.
    # Everything else (offices, amenities, shops, buildings, …) lands in street mode.
    _GEOGRAPHIC_CLASSES = frozenset({
        "place", "boundary", "natural", "waterway", "landuse",
    })

    def _nominatim_short_label(self, item, addr):
        name = (item.get("name") or
                addr.get("amenity") or addr.get("tourism") or
                addr.get("shop") or addr.get("leisure") or
                addr.get("building") or addr.get("office") or
                addr.get("healthcare") or "").strip()
        road = (addr.get("road") or addr.get("pedestrian") or
                addr.get("path") or addr.get("footway") or "").strip()
        house = addr.get("house_number", "").strip()
        suburb = (addr.get("suburb") or addr.get("quarter") or
                  addr.get("city_district") or "").strip()
        city = (addr.get("city") or addr.get("town") or
                addr.get("village") or "").strip()
        parts = []
        if name:
            parts.append(name)
        if house and road:
            parts.append(f"{house} {road}")
        elif road:
            parts.append(road)
        if suburb and suburb.lower() != city.lower():
            parts.append(suburb)
        if city:
            parts.append(city)
        return ", ".join(parts) if parts else str(item.get("display_name", "")).split(",")[0].strip()

    def _online_place_candidates(self, query):
        params = urllib.parse.urlencode({
            "q": query,
            "format": "jsonv2",
            "addressdetails": 1,
            "limit": 10,
        })
        req = urllib.request.Request(
            f"https://nominatim.openstreetmap.org/search?{params}",
            headers={"User-Agent": "MapInABox/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            miab_log("errors", f"[Jump] Online search failed for {query!r}: {exc}", getattr(self, "settings", None))
            return []

        candidates = []
        seen = set()
        for item in data if isinstance(data, list) else []:
            try:
                lat = float(item["lat"])
                lon = float(item["lon"])
            except Exception:
                continue
            nom_class = item.get("class", "")
            is_poi = nom_class not in self._GEOGRAPHIC_CLASSES
            addr = item.get("address", {})
            if is_poi:
                label = self._nominatim_short_label(item, addr)
                source = "online_poi"
            else:
                label = str(item.get("display_name", "")).strip()
                source = "online"
            if not label:
                continue
            key = label.lower()
            if key in seen:
                continue
            seen.add(key)
            candidates.append((label, lat, lon, 2, source))
        return candidates

    def _parse_jump_coordinates(self, query):
        text = (query or "").strip()
        if not text:
            return None

        pairs = re.findall(
            r'([+-]?\d+(?:\.\d+)?)\s*(north|south|east|west|[nsew])\b',
            text,
            flags=re.IGNORECASE)
        if len(pairs) >= 2:
            lat = lon = None
            for value, hemi in pairs[:2]:
                val = float(value)
                h = hemi.lower()[0]
                if h in ("n", "s"):
                    lat = val if h == "n" else -val
                elif h in ("e", "w"):
                    lon = val if h == "e" else -val
            if lat is not None and lon is not None:
                return lat, lon

        coord_match = re.match(
            r'^([+-]?\d+\.?\d*)\s*[,\s]\s*([+-]?\d+\.?\d*)$', text)
        if not coord_match:
            return None
        first = float(coord_match.group(1))
        second = float(coord_match.group(2))
        if -90 <= first <= 90 and -180 <= second <= 180:
            return first, second
        if -180 <= first <= 180 and -90 <= second <= 90:
            return second, first
        return first, second

    def _normalise_jump_query(self, query: str) -> str:
        """Expand common place abbreviations before local jump matching."""
        text = (query or "").strip().lower()
        if not text:
            return text
        aliases = {
            "nsw": "new south wales",
            "qld": "queensland",
            "vic": "victoria",
            "tas": "tasmania",
            "sa": "south australia",
            "wa": "western australia",
            "nt": "northern territory",
            "act": "australian capital territory",
        }
        words = re.split(r"(\W+)", text)
        return "".join(aliases.get(part, part) for part in words)

    def _jump_search_text(self, text: str) -> str:
        return GeoFeatures._jump_search_text(text)

    JUMP_HISTORY_CAP = 5

    # ~30 metres — named-place search results can render the same real
    # place with slightly different label text between searches (extra
    # disambiguation, different phrasing), so location proximity is a more
    # reliable "is this the same place" check than exact label text.
    JUMP_HISTORY_LOC_TOL = 3e-4

    def _record_jump(self, label: str, lat: float, lon: float) -> None:
        """Add an entry to the jump-history list and persist.

        Entries are deduped by label (case-insensitive) OR by being at
        essentially the same location; the most recent match is moved to
        the top. The list is capped at JUMP_HISTORY_CAP. Called from both
        the named-place and coord-only completion paths of show_jump_dialog.
        """
        try:
            lat = float(lat); lon = float(lon)
        except (TypeError, ValueError):
            return
        label = (label or "").strip()
        if not label:
            label = f"{lat:.4f}, {lon:.4f}"

        def _same_place(h) -> bool:
            if not isinstance(h, dict):
                return False
            if (h.get("label") or "").strip().lower() == label.lower():
                return True
            try:
                return (abs(float(h.get("lat")) - lat) < self.JUMP_HISTORY_LOC_TOL
                        and abs(float(h.get("lon")) - lon) < self.JUMP_HISTORY_LOC_TOL)
            except (TypeError, ValueError):
                return False

        history = list(self.settings.get("jump_history") or [])
        # Drop any prior entry for the same place (by label or location) so
        # the new one moves to the front instead of leaving a stale
        # duplicate sitting in its old spot.
        history = [h for h in history if not _same_place(h)]
        history.insert(0, {"label": label, "lat": lat, "lon": lon})
        del history[self.JUMP_HISTORY_CAP:]
        self.settings["jump_history"] = history
        save_settings(self.settings)

    def _extract_street_address_from_label(self, label: str):
        """Return (number, street) from labels like 'Name, 324 Burwood Road'."""
        text = str(label or "")
        match = re.search(
            r"\b(\d+[A-Za-z]?)\s+([A-Za-z][A-Za-z '\-]+?\s+"
            r"(?:Street|St|Road|Rd|Avenue|Ave|Drive|Dr|Court|Ct|Place|Pl|"
            r"Crescent|Cres|Lane|Ln|Grove|Gr|Parade|Pde|Terrace|Tce|Way))\b",
            text,
            re.IGNORECASE,
        )
        if not match:
            return None, None
        return match.group(1).strip(), re.sub(r"\s+", " ", match.group(2)).strip()

    def show_jump_history(self) -> None:
        """Ctrl+H — open the recent-jumps dialog. Select an entry to re-jump."""
        history = list(self.settings.get("jump_history") or [])
        history = [h for h in history if isinstance(h, dict) and h.get("label")]
        # Drop any entry that matches the user's current position. Without
        # this the top entry is always "where you just jumped to", which
        # duplicates the parent listbox's focused item and reads twice.
        cur_lat = float(getattr(self, "lat", 0.0) or 0.0)
        cur_lon = float(getattr(self, "lon", 0.0) or 0.0)
        TOL = 1e-4   # ~11 metres
        def _is_here(h):
            try:
                return (abs(float(h["lat"]) - cur_lat) < TOL
                        and abs(float(h["lon"]) - cur_lon) < TOL)
            except (TypeError, ValueError, KeyError):
                return False
        history = [h for h in history if not _is_here(h)]
        if not history:
            self._announce_transient(
                "No jump history yet. Press J to jump somewhere first.")
            return
        labels = [str(h["label"]) for h in history]
        dlg = wx.SingleChoiceDialog(
            self, "", "Jump History", labels)
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            self._return_focus_to_map(repeat=True)
            return
        idx = dlg.GetSelection()
        dlg.Destroy()
        if not (0 <= idx < len(history)):
            self._return_focus_to_map(repeat=True)
            return
        entry = history[idx]
        try:
            lat = float(entry["lat"]); lon = float(entry["lon"])
        except (TypeError, ValueError, KeyError):
            self._announce_transient("Selected history entry is invalid.")
            return
        label = str(entry.get("label") or f"{lat:.4f}, {lon:.4f}")
        miab_log("navigation", f"Jump from history: {label} ({lat:.3f}, {lon:.3f})", self.settings)
        if self.street_mode:
            self._exit_street_mode(repeat_location=False)
        self.lat = lat
        self.lon = lon
        self.street_label = "" if self.street_mode else self.street_label
        self._jump_street_label = None
        self._jump_street_pin_lat = None
        self._jump_street_pin_lon = None
        addr_num, addr_street = self._extract_street_address_from_label(label)
        self._pending_jump_address_number = addr_num
        self._pending_jump_address_street = addr_street
        self._pending_jump_address_lat = lat if addr_num and addr_street else None
        self._pending_jump_address_lon = lon if addr_num and addr_street else None
        self._jump_address_number = addr_num
        self._jump_address_street = addr_street
        self.last_location_str = label
        self._set_current_location_title(label)
        self._last_jump_display_label = label
        self._last_jump_display_until = time.time() + 1.5
        # Move to top of history without re-saving twice — just touch.
        self._record_jump(label, lat, lon)
        # Land in map mode and stop there — even if cached streets exist for
        # this spot, entering street mode automatically takes the choice
        # away from the user, who may want to do something else first (use
        # a tool, browse POIs, etc.). Street mode stays an explicit choice
        # (F11), same as jumping via search or coordinates.
        wx.CallAfter(self.map_panel.set_position, self.lat, self.lon,
                     self.street_mode, self.street_label)
        threading.Thread(target=self._lookup, daemon=True).start()
        # Confirm the landing once the lookup has had a moment to resolve.
        self._return_focus_to_map(repeat=True, delay_ms=250)

    def show_jump_dialog(self, initial_value=""):
        # Suppress background location announcements for the entire jump session,
        # including any 2-second retry waits.  Cleared at every real exit point.
        self._suppress_location_restore = True
        dlg = wx.TextEntryDialog(self, "Search City or Country (or paste lat,lon):", "Jump")
        if initial_value:
            dlg.SetValue(initial_value)
        def _on_escape(evt):
            if evt.GetKeyCode() == wx.WXK_ESCAPE:
                dlg.EndModal(wx.ID_CANCEL)
            else:
                evt.Skip()
        dlg.Bind(wx.EVT_CHAR_HOOK, _on_escape)
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            self._suppress_location_restore = False
            self._return_focus_to_map(repeat=True)
            return
        q = dlg.GetValue().strip()
        dlg.Destroy()

        if not q:
            self._suppress_location_restore = False
            self._return_focus_to_map(repeat=True)
            return

        miab_log("navigation", f"Jump search: '{q}'", self.settings)

        # Check if input looks like coordinates — e.g. "-25.3, 131.5",
        # "143.2271 East 13.3558 South", or "143.2271, -13.3558".
        coords = self._parse_jump_coordinates(q)
        if coords:
            try:
                lat, lon = coords
                if -90 <= lat <= 90 and -180 <= lon <= 180:
                    self.lat = lat
                    self.lon = lon
                    self.street_label = "" if self.street_mode else self.street_label
                    self._jump_street_label = None
                    self._jump_street_pin_lat = None
                    self._jump_street_pin_lon = None
                    self._jump_address_number = None
                    self._jump_address_street = None
                    miab_log("navigation", f"Jump to coords: ({lat}, {lon})", self.settings)
                    self._record_jump(f"{lat:.4f}, {lon:.4f}", lat, lon)
                    if getattr(self, '_home_setup_mode', False):
                        self._home_setup_mode = False
                        self.settings["home_lat"] = lat
                        self.settings["home_lon"] = lon
                        save_settings(self.settings)
                        _speak(f"{lat}, {lon} set as your home location.")
                    self._last_jump_display_label = f"{lat}, {lon}"
                    self._last_jump_display_until = time.time() + 1.5
                    if not _IS_LAND(lat, lon):
                        self._last_jump_display_label += " (appears to be in water — use arrow keys to move to land)"
                    self.last_location_str = self._last_jump_display_label
                    self._set_current_location_title(self.last_location_str)
                    wx.CallAfter(self.map_panel.set_position, self.lat, self.lon,
                                 self.street_mode, self.street_label)
                    self._suppress_location_restore = False
                    threading.Thread(target=self._lookup, daemon=True).start()
                    self._return_focus_to_map(repeat=True, delay_ms=250)
                    return
                else:
                    self._suppress_location_restore = False
                    _speak("Coordinates out of range. Latitude -90 to 90, longitude -180 to 180.")
                    self._return_focus_to_map(repeat=True)
                    return
            except ValueError:
                pass  # fall through to name search

        original_q = q
        q = self._normalise_jump_query(q)
        q_norm = self._jump_search_text(q)

        # Build candidate list: countries first, then cities
        # Each entry: (display_label, lat, lon, sort_key, source)
        # sort_key 0 = exact, 1 = starts-with, 2 = contains
        candidates = []
        seen_labels = set()

        def _sort_key(name):
            n = name.lower()
            if n == q:          return 0
            if n.startswith(q): return 1
            return 2

        def _candidate_type_rank(candidate):
            label, _lat, _lon, match_rank, source = candidate
            if source == "country":
                return match_rank * 2
            if source == "local":
                return match_rank * 2  # exact=0, prefix=2, contains=4 — always above features
            if source == "feature":
                if match_rank <= 1:   # 0 = nearby exact, 1 = country-level exact (+1 penalty)
                    return 1          # above substring cities (4), below prefix cities (2)
                return 10 + match_rank
            if source == "cache":
                return 20 + match_rank
            if source == "online":
                return 30 + match_rank
            return 40 + match_rank

        # Countries
        country_mask = (
            self.df['country'].str.lower().str.startswith(q, na=False) |
            self.df['country'].str.lower().str.contains(q, na=False)
        )
        for country in self.df[country_mask]['country'].unique():
            rows = self.df[self.df['country'] == country]
            label = str(country)
            if label not in seen_labels:
                seen_labels.add(label)
                candidates.append((label, float(rows.iloc[0]['lat']),
                                   float(rows.iloc[0]['lng']), _sort_key(country), "country"))

        # Cities
        city_mask = (
            self.df['city'].str.lower().str.startswith(q, na=False) |
            self.df['city'].str.lower().str.contains(q, na=False)
        )
        for _, row in self.df[city_mask].iterrows():
            parts, seen_parts = [], set()
            for p in [str(row['city']), str(row['admin_name']), str(row['country'])]:
                if p and p.lower() != 'nan' and p not in seen_parts:
                    parts.append(p)
                    seen_parts.add(p)
            label = ", ".join(parts)
            if label not in seen_labels:
                seen_labels.add(label)
                candidates.append((label, float(row['lat']), float(row['lng']),
                                   _sort_key(str(row['city'])), "local"))

        # Composite city/state/country search: handles input like
        # "burwood nsw" -> "burwood new south wales" matching
        # "Burwood, New South Wales, Australia".
        if " " in q_norm:
            first_word = q_norm.split()[0]
            composite_mask = self.df['city'].str.lower().str.contains(
                first_word, na=False, regex=False)
            for _, row in self.df[composite_mask].iterrows():
                parts, seen_parts = [], set()
                for p in [str(row['city']), str(row['admin_name']), str(row['country'])]:
                    if p and p.lower() != 'nan' and p not in seen_parts:
                        parts.append(p)
                        seen_parts.add(p)
                label = ", ".join(parts)
                label_norm = self._jump_search_text(label)
                if q_norm not in label_norm:
                    continue
                if label not in seen_labels:
                    seen_labels.add(label)
                    candidates.append((label, float(row['lat']), float(row['lng']),
                                       _sort_key(str(row['city'])), "local"))

        # Geographic features — localities, natural features and property names
        for label, glat, glon, name, match_rank, type_rank in self._geo_features.jump_candidates(
                q, self.lat, self.lon, country_code=getattr(self, "_current_country_code", None)):
            # Enrich label with nearest admin region for disambiguation
            # e.g. "King Island, Island, AU" -> "King Island, Island, Tasmania, AU"
            try:
                _, near_idx = _nearest_city(self._city_lats, self._city_lons, glat, glon)
                near_row = self.df.iloc[near_idx]
                admin = str(near_row.get('admin_name', '')).strip()
                if admin and admin.lower() != 'nan':
                    parts = label.rsplit(', ', 1)
                    if len(parts) == 2 and len(parts[1]) == 2 and parts[1].isupper():
                        label = f"{parts[0]}, {admin}, {parts[1]}"
                    else:
                        label = f"{label}, {admin}"
            except Exception:
                pass
            if label not in seen_labels:
                seen_labels.add(label)
                candidates.append((
                    label, glat, glon, match_rank, "feature"
                ))

        for candidate in self._cached_place_candidates(q, _sort_key):
            if candidate[0] not in seen_labels:
                seen_labels.add(candidate[0])
                candidates.append(candidate)

        if not candidates:
            if len(original_q) >= 4:
                msg = f'No local match. Search online for "{original_q}"?'
                dlg = wx.MessageDialog(
                    self, msg, "Online Search", wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION)
                do_online = dlg.ShowModal() == wx.ID_YES
                dlg.Destroy()
                if do_online:
                    self._status_update("Searching online...", force=True)
                    candidates = self._online_place_candidates(original_q)
                    if not candidates:
                        self._announce_transient_then_return("No online result found.")
                        wx.CallLater(2000, self.show_jump_dialog, original_q)
                        return
                else:
                    self._announce_transient_then_return("Not found.")
                    wx.CallLater(2000, self.show_jump_dialog, original_q)
                    return
            else:
                self._announce_transient_then_return(
                    "Not found. Type at least 4 characters to search online.")
                self._suppress_location_restore = False
                return

        # Sort: exact first, then prefix, then contains; alphabetical within each group
        home_lat = float(self.settings.get("home_lat", self.lat))
        home_lon = float(self.settings.get("home_lon", self.lon))

        def _dist_from_home(c):
            dlat = c[1] - home_lat
            dlon = c[2] - home_lon
            return dlat*dlat + dlon*dlon

        def _dist_from_current(c):
            dlat = c[1] - self.lat
            dlon = c[2] - self.lon
            return dlat*dlat + dlon*dlon

        current_state = self._jump_search_text(getattr(self, "last_state_found", "") or "")
        current_country = self._jump_search_text(getattr(self, "last_country_found", "") or "")

        def _label_text(c):
            return self._jump_search_text(c[0])

        def _geo_affinity(c):
            text = _label_text(c)
            state_penalty = 0 if current_state and current_state in text else 1
            country_penalty = 0 if current_country and current_country in text else 1
            return (state_penalty, country_penalty)

        # Place types in labels that indicate low-importance administrative
        # features (homesteads, localities, etc.) — penalised so proper
        # cities/suburbs always sort above them.
        _LOW_IMPORTANCE_LABELS = frozenset({
            "homestead", "locality", "hamlet", "farm", "station", "outstation",
            "pastoral", "settlement", "reserve", "property",
        })

        def _label_importance_penalty(c):
            label = c[0].lower()
            for word in _LOW_IMPORTANCE_LABELS:
                if f", {word}," in label or label.endswith(f", {word}"):
                    return 1
            return 0

        def _jump_candidate_sort_key(c):
            source = c[4]
            type_rank = _candidate_type_rank(c)
            affinity  = _geo_affinity(c)
            penalty   = _label_importance_penalty(c)
            if source == "feature":
                return (type_rank, penalty, affinity, _dist_from_current(c), _dist_from_home(c))
            return (type_rank, penalty, affinity, _dist_from_home(c), _dist_from_current(c))

        candidates.sort(key=_jump_candidate_sort_key)
        candidates = candidates[:50]

        labels = [c[0] for c in candidates]
        online_choice_index = None
        if len(original_q) >= 4:
            online_choice_index = len(labels)
            labels.append(f'Search online for "{original_q}"')
        pick_dlg = wx.SingleChoiceDialog(self, "", "Jump", labels)
        did_land = False
        if pick_dlg.ShowModal() == wx.ID_OK:
            selection = pick_dlg.GetSelection()
            if selection == online_choice_index:
                pick_dlg.Destroy()
                pick_dlg = None
                self._status_update("Searching online...", force=True)
                online_candidates = self._online_place_candidates(original_q)
                if not online_candidates:
                    self._suppress_location_restore = False
                    wx.CallAfter(self._announce_transient_then_return, "No online result found.")
                    return
                online_labels = [c[0] for c in online_candidates]
                online_dlg = wx.SingleChoiceDialog(
                    self, "", "Online Jump Results", online_labels)
                if online_dlg.ShowModal() != wx.ID_OK:
                    online_dlg.Destroy()
                    self._suppress_location_restore = False
                    self._return_focus_to_map(repeat=True)
                    return
                label, lat, lon, _, source = online_candidates[online_dlg.GetSelection()]
                online_dlg.Destroy()
            else:
                label, lat, lon, _, source = candidates[selection]
            self.lat = lat
            self.lon = lon
            did_land = True
            self.street_label = "" if self.street_mode else self.street_label
            self._jump_street_label = None
            self._jump_street_pin_lat = None
            self._jump_street_pin_lon = None
            addr_num, addr_street = self._extract_street_address_from_label(label)
            self._pending_jump_address_number = addr_num
            self._pending_jump_address_street = addr_street
            self._pending_jump_address_lat = lat if addr_num and addr_street else None
            self._pending_jump_address_lon = lon if addr_num and addr_street else None
            self._jump_address_number = addr_num
            self._jump_address_street = addr_street
            miab_log("navigation", f"Jump to: {label} ({lat:.3f}, {lon:.3f})", self.settings)
            self._record_jump(label, lat, lon)
            self.last_location_str = label
            self._set_current_location_title(label)
            self._last_jump_display_label = label
            self._last_jump_display_until = time.time() + (8.0 if source in ("online", "online_poi") else 1.5)
            if source in ("online", "online_poi"):
                self._pinned_jump_label = label
                self._pinned_jump_label_until = time.time() + 8.0
                self._cache_place_result(label, lat, lon)
            # Release suppression before the F2-style repeat; the lookup thread
            # is still blocked by the jump display window set above.
            self._suppress_location_restore = False
            # Save as home location if this is first-run setup
            if getattr(self, '_home_setup_mode', False):
                self._home_setup_mode = False
                self.settings["home_lat"] = lat
                self.settings["home_lon"] = lon
                save_settings(self.settings)
                self.update_ui(
                    f"{label} set as your home location. "
                    f"You can change this any time in Settings.")
            if source == "online_poi" and not self.street_mode:
                # Specific establishment found online — drop into street mode
                self.last_city_found = ""
                self._force_geocode_suburb_once = True
                self._street_auto_land_done = False
                self._suppress_next_street_loading_status = True
                wx.CallAfter(self.toggle_street_mode)
            elif source == "online_poi" and self.street_mode:
                # Already in street mode — move and reload streets for new area
                self._cache_center_lat = None
                self._cache_center_lon = None
                threading.Thread(target=self._query_street, daemon=True).start()
            else:
                wx.CallAfter(self.map_panel.set_position, self.lat, self.lon,
                             self.street_mode, self.street_label)
                threading.Thread(target=self._lookup, daemon=True).start()
                self._return_focus_to_map(repeat=True, delay_ms=250)
        elif getattr(self, '_home_setup_mode', False):
            # User cancelled — default to Sydney and save
            self._home_setup_mode = False
            self.settings["home_lat"] = -33.8688
            self.settings["home_lon"] =  151.2093
            save_settings(self.settings)
        if pick_dlg:
            pick_dlg.Destroy()
        self._suppress_location_restore = False
        if not did_land:
            self._return_focus_to_map(repeat=True)

    def _announce_overhead_flights(self):
        """Shift+A — show aircraft overhead in a listbox. Enter fetches destination."""
        _speak("Checking for overhead flights...")
        lat, lon = self.lat, self.lon
        RADIUS_DEG = 0.45

        def _fetch():
            try:
                states = self._opensky.states_in_bbox(
                    lat - RADIUS_DEG, lon - RADIUS_DEG,
                    lat + RADIUS_DEG, lon + RADIUS_DEG)
                if not states:
                    wx.CallAfter(
                        self._announce_transient_then_return,
                        "No aircraft detected overhead.")
                    return

                from geo import dist_km, compass_name, bearing_deg

                flights = []
                for s in states:
                    try:
                        icao24    = (s[0] or "").strip().lower()
                        raw_cs    = (s[1] or "").strip()
                        flon, flat = s[5], s[6]
                        alt_m     = s[7]
                        heading   = s[10]
                        on_ground = s[8]
                        if on_ground or flat is None or flon is None or not raw_cs:
                            continue
                        d   = dist_km(lat, lon, flat, flon)
                        airline, flight_num = decode_callsign(raw_cs)
                        if not airline:
                            continue  # skip unknown/private/military
                        alt_ft = f"{round(alt_m * 3.28084):,}ft" if alt_m else "unknown alt"
                        hdg    = f"heading {compass_name(heading)}" if heading is not None else ""
                        flights.append({
                            "raw":        raw_cs,
                            "icao24":     icao24,
                            "flight_num": flight_num,
                            "airline":    airline,
                            "alt_ft":     alt_ft,
                            "heading":    hdg,
                            "dist":       d,
                        })
                    except Exception:
                        continue

                if not flights:
                    wx.CallAfter(
                        self._announce_transient_then_return,
                        "No airborne aircraft detected overhead.")
                    return

                flights.sort(key=lambda x: x["dist"])
                if not flights:
                    wx.CallAfter(
                        self._announce_transient_then_return,
                        "No identified airline flights overhead.")
                    return
                wx.CallAfter(self._show_overhead_listbox, flights, len(flights))

            except Exception as exc:
                wx.CallAfter(self._status_update, f"Could not fetch flight data: {exc}", True)

        threading.Thread(target=_fetch, daemon=True).start()

    def _show_overhead_listbox(self, flights: list, total: int):
        """Show overhead flights in a listbox. Enter on item fetches destination."""
        dlg = wx.Dialog(self, title=f"Overhead flights ({total} aircraft)",
                        style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        vs  = wx.BoxSizer(wx.VERTICAL)

        labels = []
        for f in flights:
            airline = f["airline"] or f["flight_num"] or f["raw"]
            num     = f["flight_num"] if f["airline"] else ""
            parts   = [p for p in [airline, num, f["alt_ft"], f["heading"]] if p]
            labels.append("  ".join(parts))

        lb = wx.ListBox(dlg, choices=labels, style=wx.LB_SINGLE)
        lb.SetMinSize((460, 220))
        if labels:
            lb.SetSelection(0)
        vs.Add(lb, 1, wx.EXPAND | wx.ALL, 8)

        av_note = " — add an AviationStack key in Settings to enable" if not self._aviationstack.configured else ""
        status_lbl = wx.StaticText(dlg, label=f"Select a flight and press Enter for destination{av_note}.")
        status_lbl.Wrap(440)
        vs.Add(status_lbl, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)

        btn_close = wx.Button(dlg, wx.ID_CLOSE, "Close")
        btn_close.Bind(wx.EVT_BUTTON, lambda e: dlg.Destroy())
        vs.Add(btn_close, 0, wx.ALIGN_RIGHT | wx.RIGHT | wx.BOTTOM, 8)

        def _on_enter(evt=None):
            idx = lb.GetSelection()
            if idx != wx.NOT_FOUND:
                f = flights[idx]
                status_lbl.SetLabel(f"Looking up {f['flight_num'] or f['raw']}...")
                self._fetch_flight_destination(f, status_lbl, lb, idx)

        lb.Bind(wx.EVT_LISTBOX_DCLICK, lambda e: _on_enter())

        def _on_char_hook(evt):
            kc = evt.GetKeyCode()
            if kc in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
                _on_enter()
            elif kc == wx.WXK_ESCAPE:
                self._suppress_map_focus_repeat(800)
                dlg.Destroy()
            else:
                evt.Skip()

        dlg.Bind(wx.EVT_CHAR_HOOK, _on_char_hook)
        dlg.SetSizer(vs)
        dlg.Fit()
        dlg.CentreOnScreen()
        dlg.Show()
        lb.SetFocus()

    def _fetch_flight_destination(self, flight: dict, status_lbl, lb=None, idx=None):
        """Look up origin/destination for a selected flight.

        Tries OpenSky /flights/aircraft first (free, uses icao24 already in hand),
        falls back to AviationStack if OpenSky returns nothing and a key is configured.
        """
        raw   = flight["raw"]
        query = flight["flight_num"] or raw

        # Persistent cache check
        if query in self._flight_dest_cache:
            route_str = self._flight_dest_cache[query]
            msg = f"{flight['airline'] or query} {query}: {route_str}"
            wx.CallAfter(status_lbl.SetLabel, f"{msg} (cached)")
            if lb is not None and idx is not None:
                num   = flight["flight_num"] if flight["airline"] else ""
                parts = [p for p in [flight["airline"] or num, num,
                                     flight["alt_ft"], flight["heading"],
                                     f"→ {route_str}"] if p]
                def _update_lb(i=idx, lbl="  ".join(parts)):
                    lb.Insert(lbl, i)
                    lb.SetSelection(i)
                    lb.Delete(i + 1)
                wx.CallAfter(_update_lb)
            return

        def _icao_to_name(icao_code: str) -> str:
            """Convert ICAO airport code to a short name using the airports CSV."""
            if not icao_code:
                return ""
            try:
                import csv
                path = self._ensure_airports_csv()
                if not path:
                    return icao_code
                with open(path, encoding="utf-8") as f:
                    for row in csv.DictReader(f):
                        if row.get("icao_code", "").upper() == icao_code.upper():
                            name = row.get("name", "") or row.get("municipality", "")
                            iata = row.get("iata_code", "").strip()
                            if name:
                                return f"{name} ({iata})" if iata else name
            except Exception:
                pass
            return icao_code

        def _save_and_update(route_str: str, airline: str):
            msg = f"{airline} {query}: {route_str}"
            self._flight_dest_cache[query] = route_str
            try:
                with open(self._flight_dest_cache_path, "w", encoding="utf-8") as _f:
                    json.dump(self._flight_dest_cache, _f, ensure_ascii=False, indent=1)
            except Exception as exc:
                miab_log("errors", f"[FlightCache] Save failed: {exc}", None)
            wx.CallAfter(status_lbl.SetLabel, msg)
            if lb is not None and idx is not None:
                num   = flight["flight_num"] if flight["airline"] else ""
                parts = [p for p in [flight["airline"] or num, num,
                                     flight["alt_ft"], flight["heading"],
                                     f"→ {route_str}"] if p]
                new_label = "  ".join(parts)
                def _update_lb(i=idx, lbl=new_label):
                    lb.Insert(lbl, i)
                    lb.SetSelection(i)
                    lb.Delete(i + 1)
                wx.CallAfter(_update_lb)

        def _lookup():
            # ── Try OpenSky first (free, no extra key needed) ──────────
            icao24 = flight.get("icao24", "")
            if icao24:
                try:
                    route = self._opensky.flight_route(icao24)
                    dep = _icao_to_name(route.get("departure", "")) or route.get("departure", "")
                    arr = _icao_to_name(route.get("arrival", ""))   or route.get("arrival", "")
                    if dep or arr:
                        origin    = dep or "Unknown origin"
                        dest      = arr or "Unknown destination"
                        route_str = f"{origin} → {dest}"
                        _save_and_update(route_str, flight["airline"] or query)
                        return
                except Exception as exc:
                    miab_log("errors", f"[FlightDest] OpenSky route lookup failed: {exc}", None)

            # ── Fall back to AviationStack if key is configured ────────
            if not self._aviationstack.configured:
                wx.CallAfter(status_lbl.SetLabel, "Route not found.")
                return
            try:
                results = self._aviationstack._get("flights", {
                    "flight_iata": query, "limit": 1})
                data = results.get("data", [])
                if data:
                    fl   = data[0]
                    from aviationstack import _short_airport
                    origin = _short_airport((fl.get("departure") or {}).get("airport", "")) or \
                             (fl.get("departure") or {}).get("iata", "") or "Unknown"
                    dest   = _short_airport((fl.get("arrival") or {}).get("airport", "")) or \
                             (fl.get("arrival") or {}).get("iata", "") or "Unknown"
                    airline = (fl.get("airline") or {}).get("name") or flight["airline"] or query
                    _save_and_update(f"{origin} → {dest}", airline)
                else:
                    wx.CallAfter(status_lbl.SetLabel, f"No route found for {query}.")
            except Exception as exc:
                wx.CallAfter(status_lbl.SetLabel, f"Lookup failed: {exc}")

        threading.Thread(target=_lookup, daemon=True).start()

    def _announce_airport_flights(self):
        """Ctrl+Shift+A — departures and arrivals at nearest airport via AviationStack."""
        if not self._aviationstack.configured:
            self._announce_transient_then_return("AviationStack API key not set. Add it in Settings.")
            return

        self._status_update("Looking up nearest airport flights...")
        lat, lon = self.lat, self.lon

        def _fetch():
            try:
                import csv, math
                path = self._ensure_airports_csv()
                if not path:
                    wx.CallAfter(self._announce_transient_then_return, "Airport data not available.")
                    return

                best_dist = float('inf')
                best = None
                with open(path, encoding='utf-8') as f:
                    for row in csv.DictReader(f):
                        if row.get('type', '') not in ('large_airport', 'medium_airport'):
                            continue
                        icao = row.get('ident', '').strip()
                        if not icao:
                            continue
                        try:
                            alat = float(row['latitude_deg'])
                            alon = float(row['longitude_deg'])
                        except (ValueError, KeyError):
                            continue
                        dlat = (alat - lat) * 111.0
                        dlon = (alon - lon) * 111.0 * math.cos(math.radians(lat))
                        d = math.sqrt(dlat*dlat + dlon*dlon)
                        if d < best_dist:
                            best_dist, best = d, row

                if not best:
                    wx.CallAfter(
                        self._announce_transient_then_return,
                        "No airport found nearby.")
                    return

                icao     = best.get('ident', '')
                name     = best.get('name', icao)
                iata     = best.get('iata_code', '').strip()
                name_str = f"{name} ({iata})" if iata else name

                if not iata:
                    wx.CallAfter(self._announce_transient_then_return,
                                 f"No IATA code for {name} — cannot look up flights.")
                    return

                wx.CallAfter(self._status_update, f"Fetching flights at {name_str}...", True)

                deps = self._aviationstack.departures(iata)
                arrs = self._aviationstack.arrivals(iata)

                lines = [f"Flights at {name_str}", ""]

                if deps:
                    lines.append(f"Departures ({len(deps)}):")
                    lines.append("  Time    Flight     Airline              Destination")
                    lines.append("  " + "-" * 55)
                    for fl in deps:
                        lines.append(fmt_dep(fl))
                else:
                    lines.append("Departures: none found.")

                lines.append("")

                if arrs:
                    lines.append(f"Arrivals ({len(arrs)}):")
                    lines.append("  Time    Flight     Airline              Origin")
                    lines.append("  " + "-" * 55)
                    for fl in arrs:
                        lines.append(fmt_arr(fl))
                else:
                    lines.append("Arrivals: none found.")

                wx.CallAfter(self._show_airport_flights_dialog,
                             "\n".join(lines), name_str)

            except Exception as exc:
                wx.CallAfter(self._announce_transient_then_return, f"Airport flights failed: {exc}")

        threading.Thread(target=_fetch, daemon=True).start()

    def _show_airport_flights_dialog(self, text: str, airport_name: str):
        dlg = wx.Dialog(self, title=f"Flights — {airport_name}",
                        style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        vs  = wx.BoxSizer(wx.VERTICAL)
        txt = wx.TextCtrl(dlg, value=text,
                          style=wx.TE_MULTILINE | wx.TE_READONLY,
                          size=(420, 320))
        vs.Add(txt, 1, wx.EXPAND | wx.ALL, 8)
        btn = wx.Button(dlg, wx.ID_CLOSE, "Close")
        btn.Bind(wx.EVT_BUTTON, lambda e: dlg.Destroy())
        dlg.Bind(wx.EVT_CHAR_HOOK,
                 lambda e: (self._suppress_map_focus_repeat(800), dlg.Destroy())[1]
                 if e.GetKeyCode() == wx.WXK_ESCAPE else e.Skip())
        vs.Add(btn, 0, wx.ALIGN_RIGHT | wx.RIGHT | wx.BOTTOM, 8)
        dlg.SetSizer(vs)
        dlg.CentreOnScreen()
        dlg.Show()
        txt.SetFocus()


_CORE_LOADED_AT = time.perf_counter()


if __name__ == "__main__":
    import atexit, sys
    _startup_t0 = _PROCESS_START_T0

    # Keep this object alive until MainLoop exits; releasing it also releases
    # the native per-user instance lock. Installed and portable editions use
    # the same name so they cannot run over one another.
    app = wx.App(False)
    _wx_ready_at = time.perf_counter()
    _portable_update_lock = os.path.join(APP_DIR, ".update-in-progress")
    if PORTABLE_MODE and os.path.isfile(_portable_update_lock):
        try:
            _update_lock_age = time.time() - os.path.getmtime(_portable_update_lock)
        except OSError:
            _update_lock_age = 0
        if _update_lock_age < 1800:
            wx.MessageBox(
                "Map in a Box is still being updated. Please check the "
                "portable update window; the new version will open automatically.",
                "Portable Update in Progress",
                wx.OK | wx.ICON_INFORMATION,
            )
            sys.exit(0)
        try:
            os.remove(_portable_update_lock)
        except OSError:
            pass
    _instance_checker = wx.SingleInstanceChecker(
        f"MapInABox-{wx.GetUserId()}")
    if _instance_checker.IsAnotherRunning():
        wx.MessageBox(
            "Map in a Box is already running. Close the existing copy before "
            "opening another one.",
            "Map in a Box Already Running",
            wx.OK | wx.ICON_INFORMATION,
        )
        sys.exit(0)

    _portable_update_failure_log = os.environ.pop(
        "MIAB_PORTABLE_UPDATE_FAILED", "")
    if _portable_update_failure_log:
        wx.MessageBox(
            "The portable update could not be completed. Your existing copy "
            "has been reopened. Details were written to:\n\n" +
            _portable_update_failure_log,
            "Portable Update Failed",
            wx.OK | wx.ICON_ERROR,
        )

    _startup_settings = load_settings()
    _startup_log_cfg = dict(_startup_settings.get("logging", {}))
    if os.environ.get("MIAB_FORCE_DIAGNOSTICS") == "1":
        for _category in (
                "errors", "street", "snap", "api_calls", "challenges",
                "feature_usage", "navigation", "verbose"):
            _startup_log_cfg[_category] = True
        _startup_settings["logging"] = _startup_log_cfg

    _enabled_log_categories = {
        name for name, enabled in _startup_log_cfg.items() if enabled
    }
    _LOG_PATH = os.path.join(USER_DIR, "miab.log")
    _shared_log_file = None
    if _enabled_log_categories:
        os.environ["MIAB_LOG_PATH"] = _LOG_PATH
        os.environ["MIAB_LOG_CATEGORIES"] = ",".join(
            sorted(_enabled_log_categories))
        # Truncate once at startup, then keep structured writers in append
        # mode. Raw stderr is captured only when error logging is enabled.
        open(_LOG_PATH, "w", encoding="utf-8").close()
        _shared_log_file = open(_LOG_PATH, "a", encoding="utf-8", buffering=1)
    else:
        os.environ.pop("MIAB_LOG_PATH", None)
        os.environ.pop("MIAB_LOG_CATEGORIES", None)

    class _Tee:
        """Write to log file, and also to the original stream if one exists."""
        def __init__(self, original, log_file):
            self._orig = original  # None when console=False in frozen exe
            self._file = log_file
        def write(self, msg):
            if self._orig is not None:
                try: self._orig.write(msg)
                except Exception: pass
            try: self._file.write(msg)
            except Exception: pass
        def flush(self):
            if self._orig is not None:
                try: self._orig.flush()
                except Exception: pass
            try: self._file.flush()
            except Exception: pass
    _tee_err = None
    if _shared_log_file is not None and "errors" in _enabled_log_categories:
        _tee_err = _Tee(sys.stderr, _shared_log_file)
        sys.stderr = _tee_err

    def _cleanup_log():
        if _tee_err is not None:
            sys.stderr = _tee_err._orig or sys.__stderr__
        if _shared_log_file is not None:
            try: _shared_log_file.close()
            except Exception: pass

    atexit.register(_cleanup_log)

    miab_log("navigation", "Map in a Box started.", _startup_settings)

    import atexit as _atexit2
    _atexit2.register(lambda: miab_log(
        "navigation", "Map in a Box closed.", _startup_settings))

    miab_log("verbose", f"Startup: core module loaded in {_CORE_LOADED_AT - _startup_t0:.2f}s", _startup_settings)
    miab_log("verbose", f"Startup: wx.App ready in {_wx_ready_at - _startup_t0:.2f}s", _startup_settings)
    data  = load_offline_data()
    miab_log("verbose", f"Startup: city data loaded in {time.perf_counter() - _startup_t0:.2f}s", _startup_settings)
    if not data:
        wx.MessageBox(
            "worldcities.csv.gz not found.\n\n"
            "This file should be bundled with Map in a Box.\n"
            "Please reinstall the application.",
            "Missing Data File", wx.ICON_ERROR)
        os._exit(1)
    facts = load_facts()
    miab_log("verbose", f"Startup: facts loaded in {time.perf_counter() - _startup_t0:.2f}s", _startup_settings)
    MapNavigator(data, facts)
    miab_log("verbose", f"Startup: main window constructed in {time.perf_counter() - _startup_t0:.2f}s", _startup_settings)
    app.MainLoop()
