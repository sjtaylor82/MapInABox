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
import time
import urllib.parse
import urllib.request

from logging_utils import miab_log
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

IS_MAC = wx.Platform == "__WXMAC__"


def _primary_down(event) -> bool:
    """Treat Control as the main modifier on Windows/Linux and Command on macOS."""
    if IS_MAC and hasattr(event, "CmdDown"):
        return event.CmdDown()
    return event.ControlDown()


def _shortcut_label(primary: str) -> str:
    """Format a shortcut label for the current platform."""
    return primary if not IS_MAC else primary.replace("Ctrl", "Cmd")

# ── Screen-reader speech and Braille (AccessibleOutput2) ─────────────────
try:
    import accessible_output2.outputs.auto as _ao2_auto
    _ao2 = _ao2_auto.Auto()
except Exception:
    _ao2 = None

def _speak(msg: str, interrupt: bool = True) -> None:
    """Output directly to the active screen reader via AO2."""
    if _ao2:
        text = str(msg)
        try:
            _ao2.speak(text, interrupt=interrupt)
        except Exception:
            pass
        try:
            _ao2.braille(text)
        except Exception:
            pass

def _braille(msg: str) -> None:
    """Send text to the active braille display without adding extra speech."""
    if _ao2:
        try:
            _ao2.braille(str(msg))
        except Exception:
            pass


def _key_name(keycode) -> str:
    """Best-effort human-readable name for a wx keycode."""
    try:
        keycode = int(keycode)
    except Exception:
        return str(keycode)
    named = {
        wx.WXK_BACK: "BACK",
        wx.WXK_RETURN: "RETURN",
        wx.WXK_NUMPAD_ENTER: "NUMPAD_ENTER",
        wx.WXK_ESCAPE: "ESCAPE",
        wx.WXK_UP: "UP",
        wx.WXK_DOWN: "DOWN",
        wx.WXK_LEFT: "LEFT",
        wx.WXK_RIGHT: "RIGHT",
        wx.WXK_SPACE: "SPACE",
        wx.WXK_TAB: "TAB",
    }
    return named.get(keycode, chr(keycode) if 32 <= keycode < 127 else str(keycode))


def _log_key_event(owner, event, source: str, note: str = "") -> None:
    """Write a verbose trace for a keyboard event."""
    settings = getattr(owner, "settings", None)
    if not settings or not settings.get("logging", {}).get("verbose", False):
        return
    try:
        focus = wx.Window.FindFocus()
        focus_name = focus.GetName() if focus and focus.GetName() else type(focus).__name__ if focus else "None"
        target = event.GetEventObject()
        target_name = target.GetName() if target and target.GetName() else type(target).__name__ if target else "None"
        miab_log(
            "verbose",
            (
                f"Key {source}: key={_key_name(event.GetKeyCode())} "
                f"code={event.GetKeyCode()} primary={_primary_down(event)} "
                f"alt={event.AltDown()} shift={event.ShiftDown()} "
                f"focus={focus_name} target={target_name}"
                + (f" note={note}" if note else "")
            ),
            settings,
        )
    except Exception as exc:
        miab_log("verbose", f"Key {source} logging failed: {exc}", settings)


# ── Sub-modules ──────────────────────────────────────────────────
from geo import (
    bearing_deg,
    compass_name,
    dist_km,
    dist_metres,
    nearest_point_on_segment,
)
from overpass_client import OverpassClient
from transit_lookup import TransitLookup
from free import FreeExploreEngine
from nav import NavigationEngine
from here_poi import HereClient as HerePoi

import sys as _sys
APP_NAME      = 'Map in a Box'
APP_VERSION   = '1.0.0.0'

# Bundled read-only resources — inside the exe (_MEIPASS) or next to the script.
BASE_DIR      = getattr(_sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))

# User data — platform-appropriate location.
if _sys.platform == 'darwin':
    USER_DIR  = os.path.join(os.path.expanduser('~'), 'Library', 'Application Support', 'MapInABox')
else:
    USER_DIR  = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'MapInABox')

# Machine-local caches — platform-appropriate location.
if _sys.platform == 'darwin':
    CACHE_DIR = os.path.join(os.path.expanduser('~'), 'Library', 'Caches', 'MapInABox')
else:
    CACHE_DIR = os.path.join(os.environ.get('LOCALAPPDATA', os.path.expanduser('~')), 'MapInABox', 'Cache')

for _d in (USER_DIR, CACHE_DIR):
    os.makedirs(_d, exist_ok=True)

# ── Bundled resources (read-only) ────────────────────────────────────────────
CSV_PATH               = os.path.join(BASE_DIR,  "worldcities.csv.gz")
FACTS_PATH             = os.path.join(BASE_DIR,  "facts.json")
SOUNDS_DIR             = os.path.join(BASE_DIR,  "sounds")
COUNTRY_DIR            = os.path.join(SOUNDS_DIR, "countries")
REGION_DIR             = os.path.join(SOUNDS_DIR, "regions")
GEO_FEATURES_PATH      = os.path.join(BASE_DIR,  "geo_features.csv")
GEO_FEATURES_DIR       = os.path.join(BASE_DIR,  "GeoFeatures")
GEO_FEATURES_MANIFEST_PATH = os.path.join(GEO_FEATURES_DIR, "manifest.json")

# ── User data (%APPDATA%\MapInABox) ──────────────────────────────────────────
SETTINGS_PATH          = os.path.join(USER_DIR,  "settings.json")
SUPPRESSED_POIS_PATH   = os.path.join(USER_DIR,  "suppressed_pois.json")
RENAMED_POIS_PATH      = os.path.join(USER_DIR,  "renamed_pois.json")

# ── Caches (%LOCALAPPDATA%\MapInABox\Cache) ───────────────────────────────────
CACHE_PATH             = os.path.join(CACHE_DIR, "worldcities.pkl")
SHOP_CACHE_PATH        = os.path.join(CACHE_DIR, "shop_cache.json")
WIKI_CACHE_PATH        = os.path.join(CACHE_DIR, "wiki_cache.json")
PLACE_CACHE_PATH       = os.path.join(CACHE_DIR, "place_cache.json")
ORS_ROUTE_CACHE_PATH   = os.path.join(CACHE_DIR, "ors_route_cache.json")
AIRPORTS_CSV_PATH      = os.path.join(CACHE_DIR, "airports.csv")
AIRPORTS_CSV_SEED      = os.path.join(BASE_DIR,  "airports.csv.gz")
AIRPORTS_CSV_URL       = "https://davidmegginson.github.io/ourairports-data/airports.csv"
PLACE_NAME_CLOSE_KM = 5.0
NEAREST_PLACE_FALLBACK_KM = 250.0

# ── Geographic features (deserts, mountain ranges, oceans etc.) ──────────────

class GeoFeatures:
    """Loads country feature files on demand and checks nearby features.

    Uses a simple radius check — features are stored as centroids, so
    a generous radius is used to approximate "being inside" large features.
    """

    # Approximate radii in degrees for each feature type
    _RADII = {
        "H.OCN":  0.0,   # excluded — hardcoded KNOWN_OCEANS
        "H.SEA":  0.0,   # handled by KNOWN_OCEANS
        "H.GULF": 0.3,   # reduced from 1.0 (~11km instead of ~111km)
        "H.BAY":  0.15,  # reduced from 1.0 (~17km instead of ~111km)
        "H.RF":   0.25,  # reefs — useful named coastal features
        "H.RFS":  0.20,
        "H.STRT": 0.15,  # reduced from 0.3
        "H.CHAN": 0.15,  # reduced from 0.3
        "H.CHN":  0.15,
        "H.CHNL": 0.15,
        "H.LGN":  0.12,
        "H.RFC":  0.20,
        "H.SD":   0.18,
        "H.SHOL": 0.06,
        "H.SPIT": 0.04,
        "T.DES":  1.0,   # reduced from 3.0
        "T.DSRT": 1.0,   # reduced from 3.0
        "T.DUNE": 0.05,
        "T.ERG":  0.30,
        "T.GAP":  0.03,
        "T.GRGE": 0.04,
        "T.HDLD": 0.06,
        "T.MTS":  0.25,  # reduced from 0.5
        "T.HMDA": 0.20,
        "T.ISTH": 0.04,
        "T.KRST": 0.15,
        "T.PLN":  0.0,   # removed — too many minor plains
        "T.PLAT": 0.0,   # removed — too many minor plateaus
        "T.REG":  0.5,   # reduced from 1.0
        "T.RGN":  0.5,   # reduced from 1.0
        "T.PEN":  0.25,  # reduced from 0.5
        "T.CAPE": 0.1,   # reduced from 0.2
        "T.ISL":  0.04,
        "T.ISLET": 0.03,
        "T.ISLF": 0.04,
        "T.ISLM": 0.04,
        "T.ISLS": 0.05,  # reduced from 1.0 to 0.05 (~5.5km) — prevents distant island reaches
        "T.ISLT": 0.03,
        "T.SAND": 0.05,
        "T.CONT": 0.0,
        "L.LCTY": 0.04,
        "S.FRM":  0.04,
        "S.FRMS": 0.05,
        "S.HMSD": 0.04,
        "S.RNCH": 0.05,
        "S.RNCHS": 0.06,
    }

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

    def _stream_country(self, country_code):
        meta = self._manifest.get(country_code)
        if not meta:
            return
        filename = meta.get("file", f"{country_code}.csv")
        plain_path = os.path.join(self._base, filename)
        gz_path = plain_path + ".gz"
        path = plain_path if os.path.exists(plain_path) else gz_path
        if not os.path.exists(path):
            return
        open_func = gzip.open if path.endswith(".gz") else open
        try:
            with open_func(path, "rt", encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    try:
                        yield {
                            "name": row["name"],
                            "lat": float(row["lat"]),
                            "lon": float(row["lon"]),
                            "code": row["feature_code"],
                            "country_code": row.get("country_code", country_code),
                        }
                    except (KeyError, ValueError):
                        continue
        except Exception:
            return

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

    def features_in_box(self, lat_min, lat_max, lon_min, lon_max, country_code=None):
        """Yield features inside a small lat/lon box."""
        for feat in self._query_box(lat_min, lat_max, lon_min, lon_max, country_code=country_code):
            yield feat

    # Only announce these codes — big educational features only
    _ANNOUNCE_CODES = {"T.DES", "T.DSRT"}
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

    def lookup(self, lat: float, lon: float, country_code: str = None) -> str:
        """Return desert name for live per-keypress announcement, or ''."""
        best      = None
        best_dist = float("inf")
        for feat in self._nearby_features(lat, lon, max(self._RADII.values() or [0.0]), country_code):
            if feat["code"] not in self._ANNOUNCE_CODES:
                continue
            r = self._RADII.get(feat["code"], 0.0)
            if r == 0.0:
                continue
            dlat = abs(feat["lat"] - lat)
            dlon = abs(feat["lon"] - lon)
            if dlat > r or dlon > r * 1.5:
                continue
            dist = math.sqrt(dlat*dlat + dlon*dlon)
            if dist < r and dist < best_dist:
                best_dist = dist
                best      = feat["name"]
        return best or ""

    def lookup_any(self, lat: float, lon: float, country_code: str = None) -> str:
        """Return the name of ANY nearby feature using broad radii, or ''."""
        best      = None
        best_dist = float("inf")
        for feat in self._nearby_features(lat, lon, max(self._RADII_BROAD.values() or [0.0]), country_code):
            r = self._RADII_BROAD.get(feat["code"], 0.0)
            if r == 0.0:
                continue
            dlat = abs(feat["lat"] - lat)
            dlon = abs(feat["lon"] - lon)
            if dlat > r or dlon > r * 1.5:
                continue
            dist = math.sqrt(dlat*dlat + dlon*dlon)
            if dist < r and dist < best_dist:
                best_dist = dist
                best      = feat["name"]
        return best or ""

    def lookup_precise_label(self, lat: float, lon: float, country_code: str = None) -> str:
        """Return a feature only when the cursor is very close to its point."""
        best      = None
        best_dist = float("inf")
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
        for feat in self._nearby_features(lat, lon, max(limits.values() or [0.0]), country_code):
            code = feat.get("code", "")
            r = limits.get(code, 0.0)
            if r == 0.0:
                continue
            dlat = abs(feat["lat"] - lat)
            dlon = abs(feat["lon"] - lon)
            if dlat > r or dlon > r * 1.5:
                continue
            dist = math.sqrt(dlat*dlat + dlon*dlon)
            if dist < r and dist < best_dist:
                best_dist = dist
                best      = feat["name"]
        return best or ""

    def lookup_context_label(self, lat: float, lon: float, country_code: str = None) -> str:
        """Offline contextual label for broad natural features."""
        items = self.context_items(lat, lon, limit=1, country_code=country_code)
        return items[0] if items else ""

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
                candidates.append((dist * 111.0, code, feat["name"]))
        candidates.sort(key=lambda item: item[0])
        result = []
        for km, _code, name in candidates[:limit]:
            if km < 1:
                dist_text = f"{round(km * 1000)} metres"
            else:
                dist_text = f"{round(km)} km"
            result.append(f"{name} {dist_text}")
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
        """Return list of (name, feature_code) using broad radii for X key panel."""
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
                results.append((feat["name"], feat["code"]))
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
    "Coral Sea":      [(-25, -10, 147, 165)],
    "Great Australian Bight": [(-50, -32, 115, 145)],
    "Tasman Sea":     [(-50, -25, 145, 175)],
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
    "spatial_tones_mode":     "world",  # "world", "country", or "region"
    "challenge_direction_mode": "map",  # "map" or "globe"
    "poi_name_search_radius_km": 10,
    "gemini_api_key":         "",
    "google_cse_id":          "",
    "nav_provider":           "osm",   # "osm" or "google" or "here"
    "departure_board_source": "gtfs",  # "gtfs" or "google"
    "here_api_key":           "",
    "ors_api_key":            "",
    "weather_temperature_unit": "auto",  # "auto", "celsius", or "fahrenheit"
    "poi_source":             "osm",   # "osm" or "here"
    "logging": {
        "errors":        True,
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
            s.update(saved)
        except Exception:
            pass
    return s

def save_settings(s):
    data = {k: v for k, v in dict(s).items() if not str(k).startswith("_")}
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

# ── Dialog classes are in dialogs.py ─────────────────────────────────────
from dialogs import (
    SettingsDialog,
    POICategoryDialog,
    show_open_source_notice,
)
from timetable import TimetableClient
from poi_fetch import (
    PoiFetcher,
    POI_CATEGORY_CHOICES,
    POI_BACKGROUND_RADIUS_METRES,
    filter_pois_by_category,
)
from favourites import (
    FavouritesDialog,
    add_or_replace_favourite,
    load_favourites,
    make_favourite,
)
from street_data import StreetFetcher
from gemini import GeminiClient
from opensky import OpenSkyClient
from aviationstack import AviationStackClient, fmt_dep, fmt_arr
from priceline import PricelineClient
from airlines import decode_callsign
try:
    from game import ChallengeGame, ChallengeSession
except Exception as _game_import_err:
    print(f"[Game] Import failed: {_game_import_err}")
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
        "Armenia":                  "europe",
        "Azerbaijan":               "europe",
        "Belarus":                  "europe",
        "Bosnia and Herzegovina":   "europe",
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
        "Romania":                  "europe",
        "Rwanda":                   "africa",
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
        "Russian Federation":       "russian_federation",
        "Syrian Arab Republic":     "syrian_arab_republic",
        "United States of America": "united_states_of_america",
    }

    def play_location_sound(self, country_name, continent=""):
        canonical = COUNTRY_ALIASES.get(country_name, country_name)

        if canonical == self._current:
            return

        # Build candidate paths — try original country name first,
        # then canonical (parent country), then region fallbacks
        candidates = []
        orig_stem = _safe_stem(country_name)
        can_stem  = _safe_stem(canonical)

        # Original country name takes priority (e.g. new_caledonia.ogg over france.ogg)
        for ext in ("ogg", "mp3"):
            candidates.append(os.path.join(COUNTRY_DIR, f"{orig_stem}.{ext}"))
        for ext in ("ogg", "mp3"):
            candidates.append(os.path.join(REGION_DIR, f"{orig_stem}.{ext}"))

        # Then canonical/parent country
        if can_stem != orig_stem:
            for ext in ("ogg", "mp3"):
                candidates.append(os.path.join(COUNTRY_DIR, f"{can_stem}.{ext}"))
            for ext in ("ogg", "mp3"):
                candidates.append(os.path.join(REGION_DIR, f"{can_stem}.{ext}"))

        fallback = self._SOUND_FALLBACKS.get(canonical)
        if fallback:
            fb_stem = _safe_stem(fallback)
            for ext in ("ogg", "mp3"):
                for d in (COUNTRY_DIR, REGION_DIR):
                    candidates.append(os.path.join(d, f"{fb_stem}.{ext}"))

        if continent:
            cont_stem = _safe_stem(continent)
            for ext in ("ogg", "mp3"):
                candidates.append(os.path.join(REGION_DIR, f"{cont_stem}.{ext}"))

        for path in candidates:
            if os.path.exists(path):
                self._current = canonical
                self._play(path)
                return

        # No sound found — stop current sound
        self._current = canonical
        self._ch.stop()

    def _play(self, path):
        try:
            sound = pygame.mixer.Sound(path)
            self._ch.play(sound, loops=-1)
        except Exception as e:
            print(f"[SoundEngine] Cannot play {path}: {e}")

    def play_file(self, path, loops=0):
        """Play a WAV file once (or looped if loops=-1)."""
        try:
            sound = pygame.mixer.Sound(path)
            self._ch.play(sound, loops=loops)
        except Exception as e:
            print(f"[SoundEngine] Cannot play {path}: {e}")

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
        print(f"[Map] Land checker failed: {e}")
        return lambda lat, lon: False

_IS_LAND   = _build_land_checker(_GEO_LAND_POLYGONS)






class WorldMapPanel(wx.Panel):
    """Accurate world map from GeoJSON. Shows ISO-2 country codes always;
    F8 flashes full name and highlights country polygon in gold."""

    _COL_LABEL      = wx.Colour(255, 220,  50)
    _COL_LABEL_OUT  = wx.Colour(0,   0,    0)
    _COL_FLASH_FILL = wx.Colour(255, 200,  0, 180)
    _LABEL_SIZE     = 11
    _FLASH_SIZE     = 28

    def __init__(self, parent):
        super().__init__(parent, style=wx.NO_BORDER)
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.SetDoubleBuffered(True)
        self.SetBackgroundColour(COL_BG)
        self.lat          = 0.0
        self.lon          = 0.0
        self.street_mode  = False
        self.street_label = ""
        self._flash_name  = ""
        self._flash_rings = []
        self._flash_cx    = 0.0
        self._flash_cy    = 0.0
        self._label_cache_size = (-1, -1)
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_SIZE,  self._on_size)

    def _on_size(self, event):
        self._label_cache_size = (-1, -1)
        self._bg_bitmap = None   # invalidate background cache
        self.Refresh()
        event.Skip()

    def set_position(self, lat, lon, street_mode=False, street_label=""):
        self.lat          = lat
        self.lon          = lon
        self.street_mode  = street_mode
        self.street_label = street_label
        self.Refresh()

    def set_flash(self, name, rings_idx, centroid_lon, centroid_lat):
        self._flash_name  = name
        self._flash_rings = rings_idx
        self._flash_cx    = centroid_lon
        self._flash_cy    = centroid_lat
        self._bg_bitmap   = None   # invalidate so highlight redraws
        self.Refresh()
        wx.CallLater(2500, self._clear_flash)

    def _clear_flash(self):
        self._flash_name  = ""
        self._flash_rings = []
        self._bg_bitmap   = None
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

        # Build background bitmap once (expensive: polygons + labels)
        if not getattr(self, '_bg_bitmap', None) or \
                getattr(self, '_bg_bitmap_size', None) != (w, h):
            bmp = wx.Bitmap(w, h)
            mdc = wx.MemoryDC(bmp)
            gc2 = wx.GraphicsContext.Create(mdc)
            if gc2:
                self._paint_world_bg(gc2, w, h)
            mdc.SelectObject(wx.NullBitmap)
            self._bg_bitmap      = bmp
            self._bg_bitmap_size = (w, h)

        # Blit cached background
        dc.DrawBitmap(self._bg_bitmap, 0, 0)

        # Draw only the position dot on top (fast)
        gc = wx.GraphicsContext.Create(dc)
        if gc:
            px, py = self._geo_to_px(self.lon, self.lat, w, h)
            gc.SetBrush(gc.CreateBrush(wx.Brush(COL_RING)))
            gc.SetPen(wx.NullPen)
            gc.DrawEllipse(px - 8, py - 8, 16, 16)
            gc.SetBrush(gc.CreateBrush(wx.Brush(COL_DOT)))
            gc.DrawEllipse(px - 5, py - 5, 10, 10)

    def _draw_label(self, gc, text, cx, cy, size):
        font = wx.Font(size, wx.FONTFAMILY_SWISS,
                       wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)
        gc.SetFont(gc.CreateFont(font, self._COL_LABEL_OUT))
        for dx, dy in ((-1,-1),(1,-1),(-1,1),(1,1),(0,-1),(0,1),(-1,0),(1,0)):
            gc.DrawText(text, cx + dx, cy + dy)
        gc.SetFont(gc.CreateFont(font, self._COL_LABEL))
        gc.DrawText(text, cx, cy)

    def _paint_world_bg(self, gc, w, h):
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
        flash_set = set(self._flash_rings)
        gc.SetPen(gc.CreatePen(wx.GraphicsPenInfo(COL_BORDER).Width(1)))
        for i, ring in enumerate(_GEO_RINGS):
            colour = self._COL_FLASH_FILL if i in flash_set else COL_LAND
            gc.SetBrush(gc.CreateBrush(wx.Brush(colour)))
            pts = [self._geo_to_px(lon, lat, w, h) for lon, lat in ring]
            path = gc.CreatePath()
            path.MoveToPoint(*pts[0])
            for pt in pts[1:]:
                path.AddLineToPoint(*pt)
            path.CloseSubpath()
            gc.DrawPath(path)
        # ISO-2 labels — use cached positions, recalculate only on resize
        if not hasattr(self, '_label_cache') or self._label_cache_size != (w, h):
            # Estimate label size once using fixed char width (no gc calls needed)
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
        # Flash: large full name
        if self._flash_name:
            fcx, fcy = self._geo_to_px(self._flash_cx, self._flash_cy, w, h)
            est_w = len(self._flash_name) * self._FLASH_SIZE * 0.6
            est_h = self._FLASH_SIZE + 4
            fx = max(4, min(int(fcx - est_w / 2), w - int(est_w) - 4))
            fy = max(4, min(int(fcy - est_h / 2), h - int(est_h) - 4))
            self._draw_label(gc, self._flash_name, fx, fy, self._FLASH_SIZE)

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
        px, py = self._geo_to_px(self.lon, self.lat, w, h, **kw)
        gc.SetBrush(gc.CreateBrush(wx.Brush(COL_RING)))
        gc.SetPen(wx.NullPen)
        gc.DrawEllipse(px - 12, py - 12, 24, 24)
        gc.SetBrush(gc.CreateBrush(wx.Brush(COL_DOT)))
        gc.DrawEllipse(px - 8, py - 8, 16, 16)
        if self.street_label:
            gc.SetFont(gc.CreateFont(
                wx.Font(10, wx.FONTFAMILY_DEFAULT,
                        wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD),
                wx.Colour(220, 220, 220)))
            gc.DrawText("STREET  " + self.street_label, 8, 8)


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

        self._combo = wx.ComboBox(panel, style=wx.CB_DROPDOWN | wx.TE_PROCESS_ENTER)
        self._combo.SetBackgroundColour(wx.Colour(20, 40, 70))
        self._combo.SetForegroundColour(wx.Colour(220, 220, 220))
        vsz.Add(self._combo, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)

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

        self._timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_timer, self._timer)
        self._timer.Start(800)

        self._combo.Bind(wx.EVT_TEXT_ENTER,  self._on_jump)
        self._num.Bind(wx.EVT_TEXT_ENTER,    self._on_jump)
        self._ok_btn.Bind(wx.EVT_BUTTON,     self._on_jump)
        self._cancel_btn.Bind(wx.EVT_BUTTON, self._on_close)
        self.Bind(wx.EVT_CHAR_HOOK,          self._on_char_hook)
        self.Bind(wx.EVT_CLOSE,              self._on_close)

        self._refresh_combo(force=True)
        self.Layout()
        wx.CallAfter(self._combo.SetFocus)
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
        prev = self._combo.GetValue()
        self._combo.Set(new_names)
        if prev:
            self._combo.SetValue(prev)
        loading = getattr(self._nav, '_loading', False)
        n = len(new_names)
        if loading:
            self.SetTitle(f"Street Search — {n} streets, loading…")
        else:
            self.SetTitle(f"Street Search — {n} streets")
            self._timer.Stop()

    def _on_timer(self, event) -> None:
        self._refresh_combo()

    def _on_jump(self, event) -> None:
        sel = self._combo.GetValue().strip()
        if not sel:
            return
        house_number = self._num.GetValue().strip()
        nav = self._nav
        self._timer.Stop()
        nav._street_search_dlg = None
        nav._suppress_status_until = time.time() + 4.0
        nav._skip_next_activate_street_display = True
        nav._jump_to_street(sel, house_number=house_number)
        self.Hide()
        self.Destroy()

    def _on_close(self, event) -> None:
        self._timer.Stop()
        self._nav._street_search_dlg = None
        self.Hide()
        self.Destroy()
        wx.CallAfter(self._nav.listbox.SetFocus)

    def _on_char_hook(self, event) -> None:
        code    = event.GetKeyCode()
        focused = self.FindFocus()
        if code == wx.WXK_ESCAPE:
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
        print(f"[CoordGuard] {msg}")
        try:
            miab_log("navigation", msg, getattr(self, "settings", {}))
        except Exception:
            pass

    def __init__(self, atlas_data, facts_data):
        self.heartbeat_generation = 0
        self._street_radius     = 1500  # Increased from 800 for better coverage
        self._street_barrier    = 1300  # Increased from 700 (barrier at ~87%)
        self._poi_explore_stack = []
        self._street_matches    = []
        self._street_match_idx  = 0
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
        self._city_labels = []
        self._city_regions = []
        self._city_grid = {}
        self._region_probe_cache = {}
        self._region_points = {}
        self._region_indices = {}
        self._region_stats = {}
        city_values = self.df["city"].fillna("").astype(str).tolist()
        admin_values = self.df["admin_name"].fillna("").astype(str).tolist()
        country_values = self.df["country"].fillna("").astype(str).tolist()
        for i, (city, admin, country, lat, lon) in enumerate(zip(
                city_values, admin_values, country_values,
                self._city_lats, self._city_lons)):
            admin = "" if admin.lower() == "nan" else admin.strip()
            country = "" if country.lower() == "nan" else country.strip()
            parts, seen = [], set()
            for value in (city, admin, country):
                if value and value.lower() != "nan" and value not in seen:
                    parts.append(value)
                    seen.add(value)
            self._city_labels.append(", ".join(parts))
            self._city_regions.append((admin, country))
            self._region_indices.setdefault((admin, country), []).append(i)
            self._region_points.setdefault((admin, country), []).append(
                (float(lat), float(lon))
            )
            lat_f = float(lat)
            lon_f = float(lon)
            self._city_grid.setdefault(
                (int(math.floor(lat_f * 10)),
                 int(math.floor(lon_f * 10))),
                [],
            ).append(i)
        for region_key, points in self._region_points.items():
            if not points:
                continue
            lats = sorted(p[0] for p in points)
            lons = sorted(p[1] for p in points)
            n = len(points)
            self._region_stats[region_key] = {
                "min_lat": lats[0],
                "max_lat": lats[-1],
                "min_lon": lons[0],
                "max_lon": lons[-1],
                "center_lat": lats[n // 2],
                "center_lon": lons[n // 2],
                "count": n,
            }
        self.facts  = facts_data
        self.sound  = SoundEngine()
        self._geo_features = GeoFeatures(GEO_FEATURES_DIR)
        self._geo_features_loading = False
        self._geo_features_prefetch_lock = threading.Lock()
        self._geo_features_prefetched = set()
        self._geo_features_prefetching = set()
        self.settings = load_settings()
        self.settings["_log_path"] = os.path.join(USER_DIR, "miab.log")

        root = wx.Panel(self)
        root.SetBackgroundColour(COL_BG)
        self._h_sizer = wx.BoxSizer(wx.HORIZONTAL)

        self.map_panel = WorldMapPanel(root)
        self._h_sizer.Add(self.map_panel, 3, wx.EXPAND | wx.ALL, 4)
        self.map_panel.Bind(wx.EVT_MOTION, self._on_map_mouse_motion)
        self.map_panel.Bind(wx.EVT_LEFT_DOWN, self._on_map_mouse_click)
        self.map_panel.Bind(wx.EVT_LEFT_DCLICK, self._on_map_mouse_click)

        self.listbox = wx.ListBox(root, style=wx.LB_SINGLE)
        self.listbox.Set([""])
        self.listbox.SetBackgroundColour(wx.Colour(10, 20, 40))
        self.listbox.SetForegroundColour(wx.Colour(220, 220, 220))
        self._h_sizer.Add(self.listbox, 1, wx.EXPAND | wx.ALL, 4)

        self.info_panel = self._build_info_panel(root)
        self._h_sizer.Add(self.info_panel, 1, wx.EXPAND | wx.ALL, 4)

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
        self._poi_fetch_lat         = None   # location where POIs were last fetched
        self._poi_fetch_lon         = None
        self._poi_fetch_in_progress = False  # guard against duplicate background fetches
        self._last_spoken           = ""     # last AO2 utterance for focus-return
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
        self._poi_category      = "all"
        self._poi_populating    = False
        self._poi_index         = 0
        self._all_pois          = []
        self._poi_live_cache    = {}
        self._last_street_query = 0.0
        self.sounds_enabled     = True
        self._transit           = TransitLookup(script_dir=CACHE_DIR, resource_dir=BASE_DIR)
        self._game              = ChallengeGame(
            announce_cb = self._accessible_status,
            direction_mode_cb = lambda: self.settings.get("challenge_direction_mode", "map"),
            position_tone_cb = self._play_challenge_position_tone,
            log_cb      = lambda msg: miab_log("challenges", msg, self.settings),
        )
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
        self._poi_detail_pending_restore = False
        self._poi_detail_last_time = 0.0
        self._poi_detail_last_text = ""
        self._map_marks             = {}     # slot -> {"coords": (lat, lon), "name": str}
        self._map_destination       = None   # {"coords": (lat, lon), "name": str}
        self._prev_lat              = None   # for latitude-line crossing detection
        self._prev_lon              = None   # for Date Line crossing detection
        # Fetch throttling state
        self._last_fetch_lat        = self.lat
        self._last_fetch_lon        = self.lon
        self._distance_since_fetch  = 0.0
        self._fetch_in_progress     = False
        self._current_subregion     = ""     # for challenge milestone scoring
        self._current_country_code  = ""
        self._prefetch_in_progress  = False  # Shift+F11 background download
        # Gemini client — owns all AI queries
        self._gemini   = GeminiClient(script_dir=CACHE_DIR)
        self._gemini.init(self.settings.get("gemini_api_key", ""))
        self._opensky       = OpenSkyClient(
            base_dir=USER_DIR,
            client_id=self.settings.get("opensky_client_id", ""),
            client_secret=self.settings.get("opensky_client_secret", ""))
        self._aviationstack = AviationStackClient(
            self.settings.get("aviationstack_api_key", ""))
        self._priceline = PricelineClient(self.settings.get("rapidapi_key", ""))
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
        self.Bind(wx.EVT_CHAR_HOOK, self._on_keyboard)
        self.Bind(wx.EVT_ACTIVATE, self._on_activate)
        self.Bind(wx.EVT_CLOSE, self.on_close)
        self._refresh_info_panel()
        # Loading ticker — 1s pulse while streets or POIs are fetching
        self._loading_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_loading_tick, self._loading_timer)
        self._loading_timer.Start(1000)
        self.Show()
        self.Raise()
        self.listbox.SetFocus()
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

    def _next_known_map_target(self, direction: str):
        """Return the next named city or natural feature in a map direction."""
        lat0, lon0 = self.lat, self.lon
        eps = 0.00001
        corridors = (0.02, 0.05, 0.1, 0.25, 0.5, 1.0)

        def axis_distance(lat, lon):
            if direction == "north":
                ahead = lat - lat0
                cross = abs(lon - lon0)
            elif direction == "south":
                ahead = lat0 - lat
                cross = abs(lon - lon0)
            else:
                delta = ((lon - lon0 + 540.0) % 360.0) - 180.0
                ahead = delta if direction == "east" else -delta
                cross = abs(lat - lat0)
            return ahead, cross

        def box_for(corridor):
            if direction == "north":
                return lat0 + eps, min(90.0, lat0 + 8.0), lon0 - corridor, lon0 + corridor
            if direction == "south":
                return max(-90.0, lat0 - 8.0), lat0 - eps, lon0 - corridor, lon0 + corridor
            if direction == "east":
                return max(-90.0, lat0 - corridor), min(90.0, lat0 + corridor), lon0 + eps, min(180.0, lon0 + 8.0)
            return max(-90.0, lat0 - corridor), min(90.0, lat0 + corridor), max(-180.0, lon0 - 8.0), lon0 - eps

        def city_indices_in_box(lat_min, lat_max, lon_min, lon_max):
            gy_min = int(math.floor(lat_min * 10))
            gy_max = int(math.floor(lat_max * 10))
            gx_min = int(math.floor(lon_min * 10))
            gx_max = int(math.floor(lon_max * 10))
            for gy in range(gy_min, gy_max + 1):
                for gx in range(gx_min, gx_max + 1):
                    for i in self._city_grid.get((gy, gx), []):
                        lat = self._city_lats[i]
                        lon = self._city_lons[i]
                        if lat_min <= lat <= lat_max and lon_min <= lon <= lon_max:
                            yield i

        feature_type_rank = {
            "T.ISL": 0, "T.ISLET": 0, "T.ISLF": 0, "T.ISLM": 0,
            "T.ISLS": 0, "T.ISLT": 0,
            "H.BAY": 1, "H.BAYS": 1, "H.GULF": 1, "H.LGN": 1,
            "H.CHAN": 1, "H.CHN": 1, "H.CHNL": 1, "H.STRT": 1,
            "H.RF": 1, "H.RFC": 1, "H.RFS": 1, "H.SD": 1,
            "T.CAPE": 2, "T.PEN": 2, "T.MTS": 3, "T.DES": 3, "T.DSRT": 3,
        }

        for corridor in corridors:
            best = None
            lat_min, lat_max, lon_min, lon_max = box_for(corridor)

            for i in city_indices_in_box(lat_min, lat_max, lon_min, lon_max):
                lat = self._city_lats[i]
                lon = self._city_lons[i]
                ahead, cross = axis_distance(lat, lon)
                if ahead <= eps or cross > corridor:
                    continue
                label = self._city_labels[i]
                if not label:
                    continue
                score = (ahead, cross, 4)
                if best is None or score < best[0]:
                    best = (score, lat, lon, label)

            country_code = getattr(self, "_current_country_code", None)
            for feat in self._geo_features.features_in_box(
                    lat_min, lat_max, lon_min, lon_max, country_code=country_code):
                code = feat.get("code", "")
                if code not in feature_type_rank:
                    continue
                lat = feat["lat"]
                lon = feat["lon"]
                ahead, cross = axis_distance(lat, lon)
                if ahead <= eps or cross > corridor:
                    continue
                label = feat.get("name", "")
                if not label:
                    continue
                score = (ahead, cross, feature_type_rank.get(code, 5))
                if best is None or score < best[0]:
                    best = (score, lat, lon, label)

            if best:
                return best[1], best[2], best[3]
        return None

    def _next_region_map_target(self, direction: str):
        """Return the next named admin region in the given direction.

        This uses the atlas's city coordinates as a fast local estimate of
        regional extents, rather than probing a live geocoder on every press.
        """
        lat0, lon0 = self.lat, self.lon
        _dist, current_idx = _nearest_city(self._city_lats, self._city_lons, lat0, lon0)
        current_region = self._city_regions[current_idx]
        current_country = current_region[1]

        def _label(region: tuple[str, str]) -> str:
            return ", ".join(value for value in region if value and value.lower() != "nan")

        candidates = []
        for region, bounds in self._region_stats.items():
            if region == current_region:
                continue
            if current_country and region[1] != current_country:
                continue

            label = _label(region)
            if not label:
                continue

            if direction == "north":
                ahead = bounds["min_lat"] - lat0
                if bounds["min_lon"] <= lon0 <= bounds["max_lon"]:
                    band_gap = 0.0
                else:
                    band_gap = min(abs(lon0 - bounds["min_lon"]),
                                   abs(lon0 - bounds["max_lon"]))
                target_lat = bounds["min_lat"] + 0.02
                target_lon = min(max(lon0, bounds["min_lon"]), bounds["max_lon"])
            elif direction == "south":
                ahead = lat0 - bounds["max_lat"]
                if bounds["min_lon"] <= lon0 <= bounds["max_lon"]:
                    band_gap = 0.0
                else:
                    band_gap = min(abs(lon0 - bounds["min_lon"]),
                                   abs(lon0 - bounds["max_lon"]))
                target_lat = bounds["max_lat"] - 0.02
                target_lon = min(max(lon0, bounds["min_lon"]), bounds["max_lon"])
            elif direction == "east":
                ahead = bounds["min_lon"] - lon0
                if bounds["min_lat"] <= lat0 <= bounds["max_lat"]:
                    band_gap = 0.0
                else:
                    band_gap = min(abs(lat0 - bounds["min_lat"]),
                                   abs(lat0 - bounds["max_lat"]))
                target_lat = min(max(lat0, bounds["min_lat"]), bounds["max_lat"])
                target_lon = bounds["min_lon"] + 0.02
            else:
                ahead = lon0 - bounds["max_lon"]
                if bounds["min_lat"] <= lat0 <= bounds["max_lat"]:
                    band_gap = 0.0
                else:
                    band_gap = min(abs(lat0 - bounds["min_lat"]),
                                   abs(lat0 - bounds["max_lat"]))
                target_lat = min(max(lat0, bounds["min_lat"]), bounds["max_lat"])
                target_lon = bounds["max_lon"] - 0.02

            if ahead <= 0:
                continue

            score = ahead + (band_gap * 4.0)
            distance_score = math.hypot(ahead, band_gap)
            region_indices = self._region_indices.get(region, [])
            if not region_indices:
                continue
            anchor_idx = max(
                region_indices,
                key=lambda idx: (
                    self._city_pops[idx],
                    -(
                        (self._city_lats[idx] - bounds["center_lat"]) ** 2 +
                        (self._city_lons[idx] - bounds["center_lon"]) ** 2
                    ),
                    -(
                        (self._city_lats[idx] - target_lat) ** 2 +
                        (self._city_lons[idx] - target_lon) ** 2
                    ),
                    self._city_labels[idx].lower(),
                ),
            )
            anchor_label = self._city_labels[anchor_idx] or label
            anchor_lat = self._city_lats[anchor_idx]
            anchor_lon = self._city_lons[anchor_idx]
            candidates.append((score, ahead, distance_score, region, anchor_label, anchor_lat, anchor_lon))

        if candidates:
            candidates.sort(key=lambda item: (item[0], item[1], item[2], item[4].lower()))
            best = candidates[0]
            return best[5], best[6], best[4]

        return None

    def _on_activate(self, event):
        """Window regained focus — restore POI list or refresh street position.
        Only forces listbox focus if a POI list is being restored, to avoid
        NVDA re-announcing the current item when returning from a dialog."""
        if event.GetActive():
            if getattr(self, '_poi_explore_stack', []) or getattr(self, '_poi_list', []):
                self._show_poi_in_listbox()
                wx.CallAfter(self.listbox.SetFocus)
            elif getattr(self, 'street_mode', False) and getattr(self, '_road_fetched', False):
                if getattr(self, '_skip_next_activate_street_display', False):
                    self._skip_next_activate_street_display = False
                else:
                    self._update_street_display()
        event.Skip()

    def _ready(self):
        self.listbox.SetFocus()
        self._start_geo_features_background()
        # First run — no home location set yet
        if "home_lat" not in self.settings:
            wx.CallAfter(self._setup_home_location)
        else:
            threading.Thread(target=self._lookup, daemon=True).start()
        threading.Thread(target=self._ensure_airports_csv, daemon=True).start()
        # Update check — silent background thread
        self._updater = None
        if UpdateChecker:
            self._updater = UpdateChecker(
                current_version = APP_VERSION,
                repo            = "sjtaylor82/MapInABox",
                on_update_found = self._on_update_found,
            )
            self._updater.start()

    def _on_update_found(self, latest_version: str) -> None:
        dlg = wx.MessageDialog(
            self,
            f"Version {latest_version} of Map in a Box is available.\n\nWould you like to update now?",
            "Update Available",
            wx.YES_NO | wx.YES_DEFAULT | wx.ICON_INFORMATION,
        )
        if dlg.ShowModal() == wx.ID_YES:
            self._status_update("Downloading update...", force=True)
            if self._updater.download_and_install():
                # On Windows the installer is launching — close the app cleanly
                import sys as _sys
                if _sys.platform != "darwin":
                    self.Close()
            else:
                wx.MessageBox(
                    "Update download failed. Please visit the website to download manually.",
                    "Update Failed",
                    wx.OK | wx.ICON_ERROR,
                )
        dlg.Destroy()

    def _build_info_panel(self, parent):
        """Create the sighted-user information panel. It never takes focus."""
        panel = wx.Panel(parent)
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
            "jump": new_id(), "street": new_id(), "prefetch": new_id(),
            "favourites": new_id(),
            "nearby": new_id(), "nearby_features": new_id(),
            "latitude": new_id(), "longitude": new_id(), "capital": new_id(),
            "airport": new_id(), "overhead": new_id(), "facts": new_id(),
            "wiki": new_id(), "weather": new_id(), "time": new_id(),
            "sun": new_id(), "languages": new_id(), "currency": new_id(),
            "fullscreen": new_id(),
            "poi_address": new_id(), "poi_hours": new_id(),
            "poi_phone": new_id(), "poi_website": new_id(),
            "poi_gemini": new_id(), "poi_menu": new_id(),
            "poi_launch_website": new_id(),
            "poi_search": new_id(), "address": new_id(), "street_search": new_id(),
            "nav_address": new_id(), "intersection": new_id(), "walking": new_id(),
            "add_fav": new_id(),
            "tools": new_id(), "sounds": new_id(), "challenge": new_id(),
            "challenge_multi": new_id(),
            "help": new_id(), "about": new_id(), "manual": new_id(), "donate": new_id(),
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
        file_menu.AppendSeparator()
        add_item(file_menu, "exit", "E&xit\tAlt+F4",
                 lambda e: self.Close())
        menubar.Append(file_menu, "&File")

        go_menu = wx.Menu()
        add_item(go_menu, "jump", "&Jump",
                 lambda e: self.show_jump_dialog())
        add_item(go_menu, "street", "&Street Mode\tF11",
                 lambda e: self._menu_toggle_street_mode())
        add_item(go_menu, "prefetch", "Pre-download &Streets\tShift+F11",
                 lambda e: self._prefetch_streets())
        add_item(go_menu, "favourites", "&Favourites\tCtrl+F",
                 lambda e: self._show_favourites())
        menubar.Append(go_menu, "&Go")

        map_menu = wx.Menu()
        add_item(map_menu, "nearby", "&Nearby",
                 lambda e: self._announce_poi_count())
        add_item(map_menu, "nearby_features", "Nearby &Features",
                 lambda e: self._announce_nearby_features())
        map_menu.AppendSeparator()
        add_item(map_menu, "latitude", "&Latitude\tF3",
                 lambda e: self._status_update(
                     f"{abs(self.lat):.4f} {'North' if self.lat >= 0 else 'South'}",
                     force=True))
        add_item(map_menu, "longitude", "L&ongitude\tF4",
                 lambda e: self._status_update(
                     f"{abs(self.lon):.4f} {'East' if self.lon >= 0 else 'West'}",
                     force=True))
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
        add_item(map_menu, "fullscreen", "Full Screen &Map\tF9",
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
        add_item(poi_menu, "poi_gemini", "Ask &Gemini About Selected POI\tCtrl+Alt+5",
                 lambda e: self._run_after_menu(lambda: self._poi_detail(5)))
        add_item(poi_menu, "poi_menu", "Find POI &Menu\tCtrl+Alt+6",
                 lambda e: self._run_after_menu(lambda: self._poi_detail(6)))
        poi_menu.AppendSeparator()
        add_item(poi_menu, "poi_launch_website", "Open POI &Website\tCtrl+W",
                 lambda e: self._run_after_menu(self._open_poi_website))
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
                 lambda e: __import__("webbrowser").open(
                     "file:///" + os.path.join(BASE_DIR, "manual.html").replace("\\", "/")))
        add_item(help_menu, "about", "&About",
                 lambda e: self._show_about())
        help_menu.AppendSeparator()
        add_item(help_menu, "donate", "Donate to Project",
                 lambda e: __import__("webbrowser").open("https://www.paypal.com/donate?business=samtaylor9%40me.com&currency_code=AUD&item_name=Map+in+a+Box"))
        menubar.Append(help_menu, "&Help")

        self.SetMenuBar(menubar)
        self.Bind(wx.EVT_MENU_OPEN, lambda e: self._update_main_menu_state())

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

        street_label = "Exit &Street Mode\tF11" if street else "&Street Mode\tF11"
        self._menu_items["street"].SetItemLabel(street_label)

        toolbar = self.GetToolBar()
        if toolbar:
            toolbar.EnableTool(self._menu_ids["poi_search"], street)
            toolbar.EnableTool(self._menu_ids["nav_address"], street)

    def _menu_toggle_street_mode(self):
        if getattr(self, "_prefetch_in_progress", False) and not self.street_mode:
            self._status_update("Street download in progress. Please wait.")
            return
        self.toggle_street_mode()
        self._update_main_menu_state()

    def _run_after_menu(self, callback):
        self._speech_from_menu_until = time.time() + 1.0
        wx.CallLater(150, callback)

    def _menu_toggle_challenge(self):
        if self._session and self._session.active:
            self._session.stop()
            self._session = None
            self._game._timeout_cb = None
            self._status_update("Challenge session ended.", force=True)
            wx.CallAfter(self._resume_location_sound)
            return
        if self._game.active:
            self._game.stop()
            wx.CallAfter(self._resume_location_sound)
            return
        if self.df is not None and not self.df.empty:
            self.sound.stop()
            self._game.start(self.df, self.lat, self.lon)
        else:
            self._status_update("No city data available for the challenge.", force=True)

    def _menu_toggle_challenge_session(self):
        if self._session and self._session.active:
            self._session.stop()
            self._session = None
            self._game._timeout_cb = None
            self._status_update("Challenge session ended.", force=True)
            wx.CallAfter(self._resume_location_sound)
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
        self._status_update(self._describe_map_mouse_position(lat, lon), force=True)
        event.Skip()

    def _on_map_mouse_click(self, event):
        lat, lon = self._map_mouse_position(event)
        if getattr(self, "street_mode", False):
            self.lat = lat
            self.lon = lon
            self._query_street()
            wx.CallLater(120, self.listbox.SetFocus)
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
        self._suppress_update_ui_until = 0
        label = self._describe_map_mouse_position(lat, lon)
        self._last_jump_display_label = label
        self._last_jump_display_until = time.time() + 1.5
        wx.CallAfter(self.map_panel.set_position, self.lat, self.lon, False, "")
        wx.CallAfter(self.update_ui, label)
        threading.Thread(target=self._lookup, daemon=True).start()
        wx.CallLater(120, self.listbox.SetFocus)
        event.Skip()

    def _check_internet(self):
        try:
            urllib.request.urlopen("https://www.google.com", timeout=5)
            return True
        except Exception as e:
            print(f"[Street] Internet check failed: {e}")
            return False

    def _calc_distance_meters(self, lat1, lon1, lat2, lon2):
        """Simple distance calculation in meters using haversine."""
        R = 6371000  # Earth radius in meters
        lat1_rad = math.radians(lat1)
        lat2_rad = math.radians(lat2)
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        
        a = (math.sin(dlat / 2) ** 2 +
             math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon / 2) ** 2)
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        return R * c

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
                print(f"[Street] Cache invalid - {dist:.0f}m from center, clearing")
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
                if not self._fetch_in_progress:
                    self._distance_since_fetch = 0
                    self._last_fetch_lat = self.lat
                    self._last_fetch_lon = self.lon
                    self._fetch_in_progress = True
                    threading.Thread(target=self._query_street, daemon=True).start()
                return
        
        # Check if we have no data at all (first entry to street mode)
        if not self._road_fetched or not self._data_ready:
            if not self._fetch_in_progress and not getattr(self, '_loading', False):
                print(f"[Street] No data ready, triggering initial fetch")
                self._distance_since_fetch = 0
                self._last_fetch_lat = self.lat
                self._last_fetch_lon = self.lon
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
                    location_info = (self._geo_features.lookup_precise_label(self.lat, self.lon, cc)
                                     or self._geo_features.lookup_any(self.lat, self.lon, cc))
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
        
        for i, st in enumerate(streets_to_annotate):
            num = self._nearest_address_number(self.lat, self.lon, st, radius=200)
            if i == 0:
                parts.append(f"{num + ' ' + st if num else st}")
            else:
                parts.append(f"near {st}")
        
        label = ".  ".join(parts)
        
        wx.CallAfter(self._update_location_focus, label)

    def _prefetch_streets(self):
        """Shift+F11 — silently download and cache street data for current position."""
        if getattr(self, '_prefetch_in_progress', False):
            self._status_update("Street download already in progress.", force=True)
            return
        if self.street_mode:
            self._status_update("Already in street mode.", force=True)
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
            
            print(f"[Prefetch] Resolved place={place!r}, radius={radius}m")
            
            if bb:
                minlat, maxlat, minlon, maxlon = bb
            else:
                # No bbox - use default radius
                radius = 3000
                place  = "this area"
        except Exception as e:
            print(f"[Prefetch] Nominatim failed: {e}")
            self._prefetch_in_progress = False
            wx.CallAfter(self._status_update, "Could not resolve suburb. Check connection.", True)
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
        try:
            if not streets_cached:
                _segs, addrs, _from_cache, _snap_lat, _snap_lon, _used_boundary, _natural, _interps = \
                    self._street_fetcher.fetch_road_data(
                        self.lat, self.lon,
                        radius=radius,
                        fetch_lat=fetch_lat,
                        fetch_lon=fetch_lon,
                        status_cb=lambda msg: wx.CallAfter(self._status_update, msg),
                        suburb_name=place,
                        country_code=country_code,
                    )

            cached_pois = self._poi_fetcher.load_cached_pois(clat, clon)
            if cached_pois is None:
                wx.CallAfter(self._status_update, f"Downloading POIs for {place}...")
                pois = self._poi_fetcher.fetch_all_background(clat, clon, addrs)
                poi_note = f"{len(pois)} POIs"
            else:
                poi_note = f"{len(cached_pois)} cached POIs"

            street_note = "cached streets" if streets_cached else "streets"
            wx.CallAfter(self._status_update,
                         f"Prepared {place}: {street_note} and {poi_note}.", True)
        except Exception as e:
            print(f"[Prefetch] fetch failed: {e}")
            wx.CallAfter(self._status_update,
                f"Could not prepare {place}. Server may be busy.", True)
        finally:
            self._prefetch_in_progress = False

    def toggle_street_mode(self):
        self._loading = False          # silence loading beep immediately on F11
        if self.street_mode:
            self._exit_street_mode()
        else:
            self._loading = True
            self._street_loading_announced = True
            if getattr(self, "_suppress_next_street_loading_status", False):
                self._suppress_next_street_loading_status = False
            else:
                self._status_update("Loading streets...", force=True)
            threading.Thread(target=self._try_enter_street_mode, daemon=True).start()

    def _try_enter_street_mode(self):
        import urllib.request, urllib.parse, json, math

        fetch_seed_lat = self.lat
        fetch_seed_lon = self.lon

        # If the current position appears to be open water, load a nearby street
        # grid without moving the real cursor. The land/water polygon is coarse
        # around bays, ports, and reclaimed land, so hidden cursor snaps make the
        # global latitude/longitude untrustworthy.
        if not _IS_LAND(self.lat, self.lon):
            dist, idx = _nearest_city(self._city_lats, self._city_lons, self.lat, self.lon)
            if dist > 0.01:   # more than ~1km from any city point — genuinely at sea
                row = self.df.iloc[idx]
                fetch_seed_lat = float(row['lat'])
                fetch_seed_lon = float(row['lng'])
                city = str(row['city'])
                print(f"[Street] Position appears in water ({self.lat:.4f},{self.lon:.4f}) — loading streets near {city} ({fetch_seed_lat:.4f},{fetch_seed_lon:.4f}) without moving cursor")
                wx.CallAfter(self.update_ui,
                    f"Position appears to be in open water. Loading streets near {city} without moving.")

        # Clear stale fetch coords before re-geocoding so a previous city's
        # bbox centre never leaks into the new fetch.
        self._street_fetch_lat = None
        self._street_fetch_lon = None
        if fetch_seed_lat != self.lat or fetch_seed_lon != self.lon:
            self._street_fetch_lat = fetch_seed_lat
            self._street_fetch_lon = fetch_seed_lon

        # Use cached geocoding (checks disk cache -> samtaylor9 -> Nominatim)
        from street_data import geocode_location
        geo = geocode_location(fetch_seed_lat, fetch_seed_lon)

        if geo:
            radius = geo.get("radius", 3000)
            self._street_radius  = radius
            self._street_barrier = int(radius * 0.9)
            self._street_bbox = geo.get("bbox")
            # Prefer last_city_found (worldcities CSV, now suburb-level) over
            # Nominatim reverse-geocode — it reflects what the user sees on the
            # map and is more reliable at suburb boundaries.
            force_geocode_suburb = getattr(self, "_force_geocode_suburb_once", False)
            self._force_geocode_suburb_once = False
            map_city = "" if force_geocode_suburb else (getattr(self, 'last_city_found', '') or '')
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
            self._prefetch_geo_features_for_point(self.lat, self.lon)
        else:
            # Geocoding failed - use fallback
            print("[Street] Geocoding failed, using 3000m radius fallback")
            self._street_radius  = 3000
            self._street_barrier = 2700
            self._street_bbox = None
            self._current_suburb = None
            self._current_country_code = ""
            
        wx.CallAfter(self._enter_street_mode)

    def _enter_street_mode(self):
        self.street_mode    = True
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
        self._poi_list          = []
        self._poi_index         = 0
        self._poi_explore_stack = []
        self.street_label       = ""
        self._jump_street_label = None
        self._jump_street_pin_lat = None
        self._jump_street_pin_lon = None
        self._jump_address_number = None
        self._jump_address_street = None
        self._inside_barrier    = True
        self._pending_street_download = False
        self._barrier_dialog_pending = False
        self._gnaf_preloaded    = False
        self._gnaf_num_cache    = {}
        self._gnaf_num_fetching = None
        self._walking_mode      = False
        self._walk_announced_pois = set()
        self._all_pois          = []
        self._poi_grid          = {}   # (gx, gy) → [poi, ...] spatial index
        self._walk_graph        = None
        self._walk_node         = None
        self._walk_street       = None
        self._walk_heading      = 0.0
        self._walk_cross_options = []
        self._walk_cross_idx    = 0
        self._walk_browsing     = False
        self._walk_prev_node    = None
        self._walk_preferred_next = None
        self._walk_history      = []
        self._free_mode         = False
        self._free_engine       = FreeExploreEngine()
        self._free_engine.log_settings = self.settings
        self._nav_active        = False
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

    def _exit_street_mode(self):
        self.street_mode  = False
        self.street_label = ""
        self._jump_street_label = None
        self._jump_street_pin_lat = None
        self._jump_street_pin_lon = None
        self._jump_address_number = None
        self._jump_address_street = None
        self.last_country_found = ""
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
        self._close_poi_list()
        wx.CallAfter(self.map_panel.set_position, self.lat, self.lon, False, "")
        self._status_update("Street mode off. Returning to map.", force=True)
        threading.Thread(target=self._lookup, daemon=True).start()

    def _confirm_barrier_crossing(self, new_lat, new_lon, suburb_name):
        """Show Yes/No dialog when crossing barrier into uncached suburb."""
        self._barrier_dialog_pending = False  # Allow future prompts once dialog is shown
        suburb_name = suburb_name or "this area"
        
        dlg = wx.MessageDialog(
            self,
            f"Fetch {suburb_name}?",
            "Download Streets",
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION
        )
        result = dlg.ShowModal()
        dlg.Destroy()
        
        if result == wx.ID_YES:
            # Move to new position and download
            self.lat = new_lat
            self.lon = new_lon
            self._arrow_download = True  # Flag to prevent auto-recentering
            self._status_update(f"Entering {suburb_name}, downloading streets...")
            self._download_new_area()
        else:
            self._status_update("Download cancelled. Staying in current area.", force=True)
    
    def _confirm_poi_suburb_download(self, lat, lon, poi_name, known_street, suburb_name):
        """Show Yes/No dialog to confirm downloading suburb after POI jump."""
        dlg = wx.MessageDialog(
            self,
            f"Fetch {suburb_name}?",
            "Download Streets",
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION
        )
        result = dlg.ShowModal()
        dlg.Destroy()
        
        if result == wx.ID_YES:
            self._status_update(f"Downloading streets for {suburb_name}...")
            self._road_fetched = False
            self._road_segments = []
            self._loading = True
            self._fetch_road_data()
            threading.Thread(target=self._fetch_poi_intersection,
                           args=(lat, lon, poi_name, known_street), daemon=True).start()
        else:
            self._status_update(f"At {poi_name}. Download cancelled. No street data.", force=True)
    
    def _download_new_area(self):
        """Download street data for the current position when outside loaded area.
        Called when user presses space after leaving the cached street boundary."""
        # Check if in water first
        if not _IS_LAND(self.lat, self.lon):
            from street_data import geocode_location
            geo = geocode_location(self.lat, self.lon)
            location_name = geo.get("suburb", "water") if geo else "water"
            self._status_update(f"Can't download. You're in {location_name}.", force=True)
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
            wx.CallAfter(self._status_update, "No internet connection.", True)
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
            self._prefetch_geo_features_for_point(lat, lon)
        else:
            # Geocoding failed - use fallback
            print("[Street] Geocoding failed, using 3000m radius fallback")
            self._street_radius  = 3000
            self._street_barrier = 2700
            self._street_bbox = None
            self._current_suburb = None
            self._current_country_code = ""
        
        # Fetch road data at current position
        wx.CallAfter(self._fetch_road_data)

    # ── Walking mode ────────────────────────────────────────────────

    def _fetch_road_data(self, _attempt=1):
        # Capture current fetch ID to detect if we become stale
        my_fetch_id = getattr(self, '_street_fetch_id', 0)
        
        if not self.street_mode or self._street_fetch_id != my_fetch_id:
            print("[Street] Fetch aborted — street mode cancelled or superseded.")
            self._loading = False
            return


        self._loading = True
        fetch_lat = getattr(self, "_street_fetch_lat", None)
        fetch_lon = getattr(self, "_street_fetch_lon", None)
        if _attempt == 1:
            suburb = getattr(self, "_current_suburb", None) or "this area"
            if not getattr(self, "_street_loading_announced", False):
                wx.CallAfter(self._status_update, f"Loading streets for {suburb}...")

        def _street_fetch_status(msg):
            if (getattr(self, "_street_loading_announced", False)
                    and str(msg).lower().startswith("loading streets")):
                self._street_loading_announced = False
                print(f"[Street] Suppressed duplicate status: {msg}")
                return
            wx.CallAfter(self._status_update, msg)

        try:
            segs, addrs, from_cache, snap_lat, snap_lon, used_boundary, natural_features, interpolations = \
                self._street_fetcher.fetch_road_data(
                    self.lat, self.lon,
                    radius=self._street_radius,
                    fetch_lat=fetch_lat,
                    fetch_lon=fetch_lon,
                    status_cb=_street_fetch_status,
                    suburb_name=getattr(self, "_current_suburb", None),
                    country_code=getattr(self, "_current_country_code", None),
                )

            if not self.street_mode or self._street_fetch_id != my_fetch_id:
                print("[Street] Fetch complete but street mode was cancelled or superseded — discarding.")
                self._loading = False
                self._street_loading_announced = False
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
                print(f"[Street] Only {named_segs} named streets — widening to {wider}m and retrying from player position")
                wx.CallAfter(self._status_update,
                             f"Only {named_segs} streets found, expanding search area...")
                self._street_radius  = wider
                self._street_barrier = int(wider * 0.9)
                self._street_fetch_lat = None
                self._street_fetch_lon = None
                self._loading = False
                self._fetch_road_data(_attempt=_attempt + 1)
                return

            # ── Early recentre — don't wait for GNAF ─────────────────
            # If Stage 1 returned no named streets and no addresses,
            # we're likely positioned in water or far from the street grid.
            # Snap to the suburb centre immediately via HERE or Nominatim
            # rather than waiting 5-6s for GNAF to notice the same thing.
            if named_segs == 0 and not segs and not from_cache:
                def _fast_recentre():
                    clat, clon = None, None
                    if True:  # Nominatim recentre
                      if clat is None:
                        try:
                            import urllib.request as _ur, urllib.parse as _up, json as _j
                            params = _up.urlencode({"lat": self.lat, "lon": self.lon,
                                                    "format": "json", "zoom": 12,
                                                    "addressdetails": 1})
                            req = _ur.Request(
                                f"https://nominatim.openstreetmap.org/reverse?{params}",
                                headers={"User-Agent": "MapInABox/1.0"})
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
                            print(f"[Street] Fast fetch-centre shift: {dist:.0f}m to suburb centre")
                            self._recentring = True
                            self._street_fetch_lat = clat
                            self._street_fetch_lon = clon
                            self._road_segments  = []
                            self._natural_features = []
                            self._interpolations = []
                            self._road_fetched   = False
                            self._loading        = False
                            wx.CallAfter(self._status_update, "Loading street grid from suburb centre...")
                            threading.Thread(target=self._fetch_road_data, daemon=True).start()
                threading.Thread(target=_fast_recentre, daemon=True).start()
                return

            # ── Stage 1 complete — announce immediately ───────────────
            self._road_segments  = segs
            self._natural_features = natural_features  # Store natural features from fetch
            self._interpolations = interpolations  # Store address interpolation data
            self._address_points = addrs
            fetch_origin_lat = fetch_lat if fetch_lat is not None else self.lat
            fetch_origin_lon = fetch_lon if fetch_lon is not None else self.lon
            self._cache_center_lat = fetch_origin_lat  # Track cache center for validity
            self._cache_center_lon = fetch_origin_lon
            self._data_ready = True  # Data is now ready for display
            miab_log(
                "verbose",
                f"Stored {len(addrs)} address points, {len(interpolations)} interpolations in cache",
                self.settings,
            )
            try:
                self._free_engine.set_segments(segs)
            except Exception:
                pass
            self._road_fetched   = True
            self._recentring     = False
            self._street_loading_announced = False

            _jlabel = getattr(self, "_jump_street_label", None)
            miab_log("snap",
                     f"fetch done: {len(segs)} segs, cursor=({self.lat:.4f},{self.lon:.4f}), "
                     f"snap_pt=({snap_lat},{snap_lon}), jump_label='{_jlabel}'",
                     self.settings)
            if snap_lat and snap_lon and not getattr(self, "_jump_street_label", None):
                miab_log("snap",
                         f"fetch snap point ({snap_lat:.4f},{snap_lon:.4f}) ignored; keeping cursor at ({self.lat:.4f},{self.lon:.4f})",
                         self.settings)

            # If player is still not on a street (e.g. on a peninsula/water),
            # snap to centroid of loaded segments. When boundary query was used
            # all segments are already within the correct suburb, so the centroid
            # is guaranteed to be in the right place.
            # SKIP if download was triggered by arrow movement (user chose that location)
            if not getattr(self, "_jump_street_label", None) and not getattr(self, "_arrow_download", False):
                _primary, _ = self._nearest_road(self.lat, self.lon)
                miab_log("snap",
                         f"fetch post-load nearest_road='{_primary}' at ({self.lat:.4f},{self.lon:.4f})",
                         self.settings)
                if _primary in ("No street data", "No street data nearby"):
                    _named = [
                        (clat, clon)
                        for seg in segs
                        if seg.get("raw_name", "").strip()
                        for clat, clon in seg.get("coords", [])
                    ]
                    if _named:
                        _clat = sum(c[0] for c in _named) / len(_named)
                        _clon = sum(c[1] for c in _named) / len(_named)
                        _d = dist_metres(self.lat, self.lon, _clat, _clon)
                        miab_log("snap",
                                 f"not on street; centroid {_d:.0f}m away at ({_clat:.4f},{_clon:.4f})",
                                 self.settings)
                        if _d > 500:
                            _loaded = getattr(self, '_current_suburb', 'this area') or 'this area'
                            self._pending_snap_lat = _clat
                            self._pending_snap_lon = _clon
                            wx.CallAfter(_speak,
                                f"Nearest streets in {_loaded} are {int(_d)} metres away. "
                                f"Press Space to snap there.")
            
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

            pending_nav = getattr(self, "_pending_nav_after_street_load", None)
            if pending_nav:
                self._pending_nav_after_street_load = None
                wx.CallAfter(self._nav_launch, *pending_nav)

            _glat = fetch_lat or self.lat
            _glon = fetch_lon or self.lon
            threading.Thread(target=self._gnaf_preload_addresses,
                             args=(_glat, _glon), daemon=True).start()

            # Pre-populate _all_pois from disk cache; fetch live if cache is empty
            def _try_load_poi_cache():
                try:
                    cached = self._poi_fetcher.load_cached_pois(_glat, _glon)
                    if cached:
                        _suppressed = _load_suppressed()
                        _renamed    = _load_renamed()
                        pois = _apply_renames(
                            [p for p in cached if not _is_suppressed(p, _suppressed)],
                            _renamed)
                        self._all_pois = pois
                        self._poi_grid = self._build_poi_grid(pois)
                        self._poi_fetch_lat = _glat
                        self._poi_fetch_lon = _glon
                        try:
                            self._free_engine.set_pois(pois)
                        except Exception:
                            pass
                        miab_log("verbose", f"Pre-loaded {len(pois)} POIs from cache.", self.settings)
                        # Cache is only a quick first pass.  Always refresh
                        # live so removed or newly added OSM POIs are
                        # reconciled against current data.
                        threading.Thread(
                            target=self._fetch_all_pois_background,
                            args=(self._address_points,),
                            daemon=True,
                        ).start()
                    else:
                        miab_log("verbose", "No disk cache — fetching live.", self.settings)
                        self._fetch_all_pois_background(self._address_points)
                except Exception as exc:
                    miab_log("errors", f"POI cache pre-load error: {exc}", self.settings)
            threading.Thread(target=_try_load_poi_cache, daemon=True).start()

            # ── Stage 2 — full radius background fetch ────────────────
            # _loading stays True so progress beeps continue until done.
            if not from_cache and not used_boundary:
                def _outer_fetch(clat=_glat, clon=_glon,
                                 rad=self._street_radius, segs_so_far=segs):
                    try:
                        merged, full_addrs = \
                            self._street_fetcher.live_fetch_outer(
                                clat, clon, rad, segs_so_far,
                                status_cb=lambda msg: wx.CallAfter(
                                    self._status_update, msg),
                            )
                        if not self.street_mode or self._street_fetch_id != my_fetch_id:
                            return
                        self._road_segments  = merged
                        self._address_points = full_addrs or self._address_points
                        try:
                            self._free_engine.set_segments(merged)
                        except Exception:
                            pass
                        # Silently rebuild walk graph if walking mode active
                        if getattr(self, '_walking_mode', False):
                            self._walk_graph = self._build_walk_graph()
                            self._nav.set_graph(self._walk_graph)
                        self._loading = False
                        wx.CallAfter(self.map_panel.set_position,
                                     self.lat, self.lon, True, self.street_label)
                        # Only update status if user is idle
                        if not (self._poi_list or
                                getattr(self, '_walking_mode', False) or
                                getattr(self, '_free_mode', False)):
                            wx.CallAfter(self._status_update,
                                         f"Streets fully loaded.  "
                                         f"{len(merged)} streets in area.")
                    except Exception as exc:
                        print(f"[Street] Stage 2 error: {exc}")
                        self._loading = False
                threading.Thread(target=_outer_fetch, daemon=True).start()
            else:
                # Cache hit — no Stage 2 needed, stop beeps immediately
                self._loading = False

        except Exception as e:
            print(f"[Street] fetch error: {e}")
            self._loading = False
            self._street_loading_announced = False
            wx.CallAfter(self._status_update,
                         "Street servers unavailable and no cached data for this area.  "
                         "Try again later with F11, or move to a previously visited area.",
                         True)

    def _nearest_road(self, lat, lon):
        """Thin delegator — see StreetFetcher.nearest_road."""
        return self._street_fetcher.nearest_road(lat, lon, self._road_segments)

    def _nearest_roads_with_distances(self, lat, lon):
        """Thin delegator — see StreetFetcher.nearest_roads_with_distances."""
        return self._street_fetcher.nearest_roads_with_distances(
            lat, lon, self._road_segments)



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

    # ── GTFS query delegators ────────────────────────────────────────────────
    # All heavy lifting is in self._transit (TransitLookup).

    @staticmethod
    def _gtfs_is_transit_poi(poi: dict) -> bool:
        return TransitLookup.is_transit_poi(poi)

    def _gtfs_nearby_stops(self, lat, lon, radius=200, status_cb=None):
        return self._transit.nearby_stops(lat, lon, radius=radius, status_cb=status_cb)

    def _gtfs_routes_for_stop(self, stop_id, feed_id):
        return self._transit.routes_for_stop(stop_id, feed_id)

    def _gtfs_stops_for_route(self, route_id, feed_id, headsign=""):
        return self._transit.stops_for_route(route_id, feed_id, headsign=headsign)

    def _gtfs_next_departures(self, stop_id, route_id, feed_id, n=3):
        return self._transit.next_departures(stop_id, route_id, feed_id, n=n)



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

    def _gnaf_preload_addresses(self, lat, lon, radius=2000):
        """Fetch all GNAF addresses for current area and merge into _address_points."""
        if not GNAF_URL:
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
                    })
                    added += 1
            if self.settings.get("here_api_key", "").strip():
                return
            if added > 50 and self._road_fetched:
                label, _ = self._nearest_road(self.lat, self.lon)
                if "No street data" in label:
                    lats = [a["lat"] for a in addresses]
                    lons = [a["lon"] for a in addresses]
                    clat = sum(lats) / len(lats)
                    clon = sum(lons) / len(lons)
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
        for ap in getattr(self, "_address_points", []):
            if bare(ap["street"]) != clean:
                continue
            if not ap.get("number"):
                continue
            d = math.sqrt(((lat - ap["lat"]) * 111000)**2 +
                          ((lon - ap["lon"]) * 111000 *
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
                            print(f"[Query] No streets, {location_info}: {feature_name}, {dist}m from centre")
                        else:
                            msg = f"{self.street_label}.  {location_info}."
                            print(f"[Query] No streets, {location_info}, {dist}m from centre")
                    else:
                        msg = f"{self.street_label}."
                        print(f"[Query] No street data, {dist}m from centre")
                    
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

            for i, st in enumerate(streets_to_annotate):
                num = self._nearest_address_number(self.lat, self.lon, st, radius=200)
                if i == 0:
                    # Primary street: "number Street" format
                    parts.append(f"{num + ' ' + st if num else st}")
                else:
                    # Cross street: drop number, just "near Street"
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

    def _fetch_pois(self, category_key="all", radius=1000, timeout=30,
                    next_radius=0, name_filter="", source=""):
        """Fetch POIs for *category_key* from the requested *source*.

        source: "osm" | "here" | "google" | "" (auto — here if key set, else osm)
        """
        import threading
        self._loading    = True
        category_key = (category_key or "all").lower()
        name_filter  = (name_filter or "").strip().lower()
        here_key   = self.settings.get("here_api_key",   "").strip()
        google_key = self.settings.get("google_api_key", "").strip()
        if not source:
            poi_source = self.settings.get("poi_source", "osm")
            source = poi_source if (poi_source == "here" and here_key) else "osm"
        category_labels = dict(POI_CATEGORY_CHOICES)
        category_label  = category_labels.get(category_key, "All nearby")
        def _name_search_max_radius():
            try:
                km = int(self.settings.get("poi_name_search_radius_km", 10))
            except (TypeError, ValueError):
                km = 10
            return max(1, min(10, km)) * 1000

        search_radii = [radius]
        if next_radius and next_radius > radius:
            search_radii.append(next_radius)
        if name_filter:
            max_radius = _name_search_max_radius()
            search_radii = sorted({
                r for r in (radius, 3000, max_radius)
                if r <= max_radius
            })
        else:
            # Keep plain category browsing local. Named searches may widen,
            # but category-only lookups should start from the requested radius
            # instead of jumping straight to the user's name-search cap.
            search_radii = sorted(set(search_radii))

        def _name_match(poi):
            if not name_filter:
                return True
            label = (poi.get("name") or poi.get("label") or "").lower()
            kind  = (poi.get("kind") or "").lower()
            return name_filter in label or name_filter in kind

        try:
            cached_presented = False
            cached_pois = []

            def _prepare_pois(raw_pois, radius_m=None):
                _suppressed = _load_suppressed()
                _renamed    = _load_renamed()
                prepared = _apply_renames(
                    [p for p in raw_pois if not _is_suppressed(p, _suppressed)],
                    _renamed)
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
                        current.append(item)
                    prepared = current
                prepared.sort(key=lambda x: x.get("dist", float("inf")))
                return prepared

            background = list(getattr(self, "_all_pois", []) or [])
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
                if source_matches and location_matches:
                    cached_pois = filter_pois_by_category(background, category_key)
                    cached_pois = [p for p in cached_pois if _name_match(p)]
                    cached_pois = _prepare_pois(cached_pois, radius_m=max(search_radii))
                    if cached_pois:
                        self._poi_list     = cached_pois
                        self._poi_index    = 0
                        self._poi_category = category_key
                        cached_presented = True
                        name_desc = f" matching '{name_filter}'" if name_filter else ""
                        miab_log(
                            "verbose",
                            f"p served from in-memory background POIs: "
                            f"{len(cached_pois)} {category_key}{name_desc} results via {source}.",
                            self.settings,
                        )
                        wx.CallAfter(self._present_poi_list)
                        if max(search_radii) <= POI_BACKGROUND_RADIUS_METRES:
                            self._loading = False
                            return
                        miab_log(
                            "verbose",
                            f"p doing live {source.upper()} fetch: "
                            f"configured radius {max(search_radii)}m exceeds "
                            f"background radius {POI_BACKGROUND_RADIUS_METRES}m.",
                            self.settings,
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
            if name_filter:
                miab_log(
                    "verbose",
                    f"p doing live {source.upper()} fetch for name search '{name_filter}'.",
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

            pois = []
            attempted_radius = radius
            for attempt_radius in search_radii:
                attempted_radius = attempt_radius
                if not name_filter:
                    miab_log(
                        "verbose",
                        f"p live fetch {category_key} radius={attempt_radius}m source={source}.",
                        self.settings,
                    )
                if source == "google" and google_key:
                    raw = self._fetch_google_pois(
                        category_key, radius=attempt_radius,
                        name_filter=name_filter)
                    raw = filter_pois_by_category(raw, category_key)
                    pois = [p for p in raw if _name_match(p)]
                    pois.sort(key=lambda x: x.get("dist", float("inf")))
                elif source == "here" and here_key:
                    self._poi_fetcher.set_here_key(here_key)
                    raw_pois = _live_cache_get("category", source, category_key, attempt_radius)
                    if raw_pois is None:
                        raw_pois, _ = self._poi_fetcher.fetch_pois(
                            self.lat, self.lon,
                            category=category_key, radius=attempt_radius,
                            timeout=timeout,
                            address_points=getattr(self, "_address_points", []),
                        )
                        _live_cache_set("category", source, category_key, attempt_radius, raw_pois)
                    pois = [p for p in raw_pois if _name_match(p)]
                else:
                    # OSM — temporarily clear HERE key so fetch_pois uses Overpass
                    self._poi_fetcher.set_here_key("")
                    raw_pois = _live_cache_get("category", source, category_key, attempt_radius)
                    if raw_pois is None:
                        raw_pois, _ = self._poi_fetcher.fetch_pois(
                            self.lat, self.lon,
                            category=category_key, radius=attempt_radius,
                            timeout=timeout,
                            address_points=getattr(self, "_address_points", []),
                        )
                        _live_cache_set("category", source, category_key, attempt_radius, raw_pois)
                    self._poi_fetcher.set_here_key(here_key)
                    pois = [p for p in raw_pois if _name_match(p)]
                    if not pois and name_filter:
                        raw_name_pois = _live_cache_get(
                            "name", source, category_key, attempt_radius, name_filter)
                        if raw_name_pois is None:
                            raw_name_pois = self._poi_fetcher.fetch_osm_name_search(
                                self.lat, self.lon,
                                name_filter=name_filter,
                                radius=attempt_radius,
                                timeout=timeout,
                                address_points=getattr(self, "_address_points", []),
                            )
                            _live_cache_set(
                                "name", source, category_key, attempt_radius,
                                raw_name_pois, name_filter)
                        pois = [p for p in raw_name_pois if _name_match(p)]
                if pois or not name_filter:
                    break

            if getattr(self, "_poi_explore_stack", []):
                self._loading = False
                return

            pois = _prepare_pois(pois)
            if not pois and cached_presented:
                miab_log(
                    "verbose",
                    "p live fetch returned no extra results; keeping in-memory background POI list.",
                    self.settings,
                )
                self._loading = False
                return
            self._poi_list     = pois
            self._poi_index    = 0
            self._poi_category = category_key
            self._loading      = False

            if self._poi_list:
                wx.CallAfter(self._present_poi_list)
            else:
                what = f"'{name_filter}'" if name_filter else category_label.lower()
                if name_filter:
                    wx.CallAfter(
                        self._retry_poi_name_search,
                        category_key,
                        name_filter,
                        source,
                        attempted_radius,
                    )
                else:
                    wx.CallAfter(_speak,
                                 f"No {what} found within {attempted_radius} metres.")

        except Exception as e:
            miab_log("errors", f"POI fetch error: {e}", self.settings)
            self._loading = False
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

    def _announce_postcode(self):
        """Fetch and announce postcode for current position via Nominatim."""
        self._status_update("Looking up postcode...")
        def _fetch():
            try:
                postcode = None
                for zoom in (18, 14, 10):
                    url = (f"https://nominatim.openstreetmap.org/reverse"
                           f"?lat={self.lat}&lon={self.lon}&format=json&zoom={zoom}&addressdetails=1")
                    req = urllib.request.Request(url, headers={"User-Agent": "MapInABox/1.0"})
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        data = json.loads(resp.read().decode())
                    postcode = data.get("address", {}).get("postcode")
                    if postcode:
                        break
                if postcode:
                    wx.CallAfter(self._status_update, f"Postcode: {postcode}.", True)
                else:
                    wx.CallAfter(self._status_update, "No postcode found for this location.", True)
            except Exception:
                wx.CallAfter(self._status_update,
                             "Could not fetch postcode. Check internet connection.",
                             True)
        threading.Thread(target=_fetch, daemon=True).start()

    def _poi_detail(self, key_num: int):
        poi = None
        if getattr(self, '_poi_list', []) and 0 <= getattr(self, '_poi_index', -1) < len(self._poi_list):
            poi = self._poi_list[self._poi_index]
        elif getattr(self, '_poi_explore_stack', []):
            stack = self._poi_explore_stack[-1]
            items = stack.get('items', [])
            idx   = stack.get('index', 0)
            if items and 0 <= idx < len(items):
                poi = items[idx]

        if poi is None:
            self._poi_detail_announce("No POI selected."); return

        name = (poi.get('name') or poi.get('label', '')).split(',')[0].strip()

        needs_detail = (
            key_num in (1, 2, 3, 4)
            and self.settings.get("here_api_key", "").strip()
            and not any([
                poi.get("address"), poi.get("phone"),
                poi.get("website"), poi.get("opening_hours"), poi.get("here_id"),
            ])
        )

        if needs_detail and self.settings.get("here_api_key", "").strip():
            self._poi_detail_announce(f"Looking up {name}...")
            def _fetch_and_dispatch():
                detail = self._here.fetch_poi_detail(
                    name, poi.get('lat', self.lat), poi.get('lon', self.lon))
                poi.update(detail)
                wx.CallAfter(self._poi_detail_dispatch, key_num, poi, name)
            threading.Thread(target=_fetch_and_dispatch, daemon=True).start()
            return

        self._poi_detail_dispatch(key_num, poi, name)

    def _poi_detail_announce(self, text: str) -> None:
        """Announce POI detail via ao2 speech and braille."""
        _speak(text, interrupt=True)
        _braille(text)

    class _AnnounceAccessible(wx.Accessible):
        def __init__(self, win):
            super().__init__(win)
            self._text = ""
        def GetName(self, childId):
            return (wx.ACC_OK, self._text)
        def GetRole(self, childId):
            return (wx.ACC_OK, wx.ROLE_SYSTEM_ALERT)
        def GetState(self, childId):
            return (wx.ACC_OK, 0)
        def GetDescription(self, childId):
            return (wx.ACC_OK, self._text)
        def GetValue(self, childId):
            return (wx.ACC_OK, self._text)

    def _sr_announce(self, text: str) -> None:
        """Speak text via IAccessible EVENT_SYSTEM_ALERT — no focus change."""
        import sys
        if sys.platform != "win32":
            return
        import ctypes
        if not hasattr(self, "_sr_acc"):
            root = self.GetChildren()[0] if self.GetChildren() else self
            self._sr_widget = wx.StaticText(root, label="", size=(1, 1), pos=(0, 0))
            self._sr_widget.SetForegroundColour(root.GetBackgroundColour())
            self._sr_widget.SetBackgroundColour(root.GetBackgroundColour())
            self._sr_acc = self._AnnounceAccessible(self._sr_widget)
            self._sr_widget.SetAccessible(self._sr_acc)
        hwnd         = self._sr_widget.GetHandle()
        OBJID_CLIENT = 0xFFFFFFFC
        CHILDID_SELF = 0
        self._sr_acc._text = ""
        self._sr_widget.SetLabel("")
        self._sr_acc._text = text
        self._sr_widget.SetLabel(text)
        ctypes.windll.user32.NotifyWinEvent(0x0002, hwnd, OBJID_CLIENT, CHILDID_SELF)
        ctypes.windll.user32.NotifyWinEvent(0x800C, hwnd, OBJID_CLIENT, CHILDID_SELF)

    def _poi_detail_dispatch(self, key_num: int, poi: dict, name: str):
        import time as _time

        if key_num == 1:
            text = poi.get('address', '').strip()
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
            tags = poi.get('tags') or {}
            text = (poi.get('website') or tags.get('website') or tags.get('contact:website') or '').strip() or "No website available."
        elif key_num == 5:
            kind   = poi.get('kind', '')
            suburb = getattr(self, '_current_suburb', '')
            cache_key = f"review_{name.lower().replace(' ','_')}_{round(poi.get('lat',0),2)}_{round(poi.get('lon',0),2)}"
            if not self._gemini.is_configured:
                self._poi_detail_announce("Gemini not configured — add API key in settings.")
                return
            self._poi_detail_announce(f"Asking Gemini about {name}...")
            def _gemini_review():
                loc    = f" in {suburb}" if suburb else ""
                kind_s = f" ({kind})" if kind else ""
                prompt = (f"Give a brief 2-3 sentence summary of what people say about "
                          f"{name}{kind_s}{loc}. Focus on what it's like to visit. "
                          f"Be concise and factual.")
                result = self._gemini.query_text(prompt, cache_key)
                result = result if result else f"No information found for {name}."
                self._poi_detail_last_text = result
                self._poi_detail_last_key  = key_num
                wx.CallAfter(self._poi_detail_announce, result)
            threading.Thread(target=_gemini_review, daemon=True).start()
            return
        elif key_num == 6:
            if not self.settings.get("google_api_key", "").strip() and not self._gemini.is_configured:
                self._poi_detail_announce(
                    "Menu lookup needs a Google API key or Gemini API key. Add one in Settings."
                )
                return
            self._poi_detail_announce(f"Looking up menu for {name}...")
            def _gemini_menu():
                kind = poi.get('kind', '')
                address = (poi.get('address') or getattr(self, '_current_suburb', ''))
                website = poi.get('website') or ""
                country = getattr(self, 'last_country_found', '')
                region = getattr(self, '_current_subregion', '')
                google_key = self.settings.get("google_api_key", "").strip()
                suburb = (poi.get('address') or
                          getattr(self, '_current_suburb', '') or
                          address)
                urls = []
                places_website = ""
                if google_key:
                    urls, places_website = self._gemini.search_menu_links_places(
                        name, suburb, region, country, google_key,
                        poi.get('lat') or self.lat, poi.get('lon') or self.lon)
                if not urls and self._gemini.is_configured:
                    # Fall back to Gemini, passing the Places website as extra context
                    effective_website = places_website or website
                    urls = self._gemini.ask_menu_links(
                        name, kind, address, effective_website, country, region)
                if urls:
                    wx.CallAfter(self._show_menu_links_dialog, name, urls)
                else:
                    if google_key and not self._gemini.is_configured:
                        msg = (
                            f"No menu found for {name}. "
                            "Menu lookup can still use Gemini for broader web search if you add a Gemini API key."
                        )
                    elif not google_key and self._gemini.is_configured:
                        msg = (
                            f"No menu found for {name}. "
                            "Add a Google API key for Places-based menu discovery."
                        )
                    else:
                        msg = f"No menu found for {name}."
                    wx.CallAfter(self._poi_detail_announce, msg)
            threading.Thread(target=_gemini_menu, daemon=True).start()
            return
        else:
            return

        now = _time.monotonic()
        double = (key_num == self._poi_detail_last_key
                  and (now - self._poi_detail_last_time) < 0.6)
        self._poi_detail_last_key  = key_num
        self._poi_detail_last_time = now
        self._poi_detail_last_text = text

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
            dlg.EndModal(wx.ID_CLOSE)
            self.listbox.SetFocus()

        btn.Bind(wx.EVT_BUTTON, _close)
        dlg.Bind(wx.EVT_CHAR_HOOK, lambda e: _close() if e.GetKeyCode() == wx.WXK_ESCAPE else e.Skip())
        ctrl.SetFocus()
        ctrl.SelectAll()
        dlg.ShowModal()
        dlg.Destroy()

    def _show_menu_links_dialog(self, restaurant_name: str, urls: list):
        """Display clickable menu links in a dialog."""
        import webbrowser

        dlg = wx.Dialog(self, title=f"Menu Links: {restaurant_name}",
                        style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
                        size=(700, 500))
        sizer = wx.BoxSizer(wx.VERTICAL)

        # Title
        title = wx.StaticText(dlg, label=f"Menu links for {restaurant_name}: {len(urls)} found")
        title_font = title.GetFont()
        title_font.MakeBold()
        title.SetFont(title_font)
        sizer.Add(title, 0, wx.ALL | wx.EXPAND, 10)

        # Create scroll window first
        scroll = wx.ScrolledWindow(dlg)
        link_sizer = wx.BoxSizer(wx.VERTICAL)

        # Button for each URL
        for i, url in enumerate(urls[:8], 1):  # Max 8 links
            btn = wx.Button(scroll, label=f"{i}. {url[:70]}..." if len(url) > 70 else f"{i}. {url}")
            btn.SetToolTip(url)  # Show full URL in tooltip
            btn.Bind(wx.EVT_BUTTON, lambda e, u=url: webbrowser.open(u))
            link_sizer.Add(btn, 0, wx.ALL | wx.EXPAND, 5)

        scroll.SetSizer(link_sizer)
        scroll.SetScrollRate(5, 5)
        sizer.Add(scroll, 1, wx.EXPAND | wx.ALL, 8)

        # Close button
        btn_close = wx.Button(dlg, wx.ID_CLOSE, "Close (Escape)")
        sizer.Add(btn_close, 0, wx.ALIGN_RIGHT | wx.RIGHT | wx.BOTTOM, 8)

        dlg.SetSizer(sizer)
        btn_close.Bind(wx.EVT_BUTTON, lambda e: dlg.EndModal(wx.ID_CLOSE))
        dlg.Bind(wx.EVT_CHAR_HOOK,
                 lambda e: dlg.EndModal(wx.ID_CLOSE) if e.GetKeyCode() == wx.WXK_ESCAPE else e.Skip())

        dlg.ShowModal()
        dlg.Destroy()

        # Announce to screen reader
        self._poi_detail_announce(f"Menu links for {restaurant_name}: {len(urls)} link(s) found. Press Tab to navigate buttons.")

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
                print(f"[Address Lookup] Error: {e}")
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

    def _show_poi_category_dialog(self, initial_key="all", initial_name="", initial_source=None, notice=""):
        sources = ["osm"]
        if self.settings.get("here_api_key", "").strip():
            sources.append("here")
        if self.settings.get("google_api_key", "").strip():
            sources.append("google")
        preferred = initial_source or self.settings.get("poi_source", "osm")
        dlg = POICategoryDialog(
            self,
            available_sources=sources,
            preferred_source=preferred,
            initial_key=initial_key,
            initial_name=initial_name,
            notice=notice,
        )
        try:
            if dlg.ShowModal() == wx.ID_OK and dlg.selected_key:
                category_map = dict(POI_CATEGORY_CHOICES)
                label  = category_map.get(dlg.selected_key, "All nearby")
                name   = dlg.selected_name
                source = dlg.selected_source
                if name:
                    self._status_update(f"Searching {label.lower()} for '{name}' via {source.upper()}...")
                else:
                    self._status_update(f"Searching {label.lower()} via {source.upper()}...")
                threading.Thread(
                    target=self._fetch_pois,
                    args=(dlg.selected_key,),
                    kwargs={"name_filter": name, "source": source},
                    daemon=True,
                ).start()
        finally:
            dlg.Destroy()

    def _announce_poi_count(self):
        wx.CallAfter(self._show_poi_category_dialog)

    def _retry_poi_name_search(self, category_key, name_filter, source, radius):
        what = f"'{name_filter}'" if name_filter else "that search"
        self._status_update(f"No {what} found within {radius} metres.", force=True)
        self._show_poi_category_dialog(
            initial_key=category_key,
            initial_name=name_filter,
            initial_source=source,
            notice=f"No {what} found within {radius} metres. Edit the search and try again.",
        )

    def _poi_travel_time_label(self, distance_m):
        """Approximate POI travel time for list labels."""
        if distance_m < 1000:
            mins = max(1, int(round(distance_m / 80.0)))
            return f"about {mins} min walk"
        mins = max(1, int(round(distance_m / 500.0)))
        return f"about {mins} min drive"

    def _show_poi_in_listbox(self):
        """Populate listbox with all POIs and select the current one.
        Uses _poi_populating flag to suppress EVT_LISTBOX during fill."""
        self._poi_populating = True
        labels = []
        for poi in self._poi_list:
            label = poi["label"]
            plat = poi.get("lat"); plon = poi.get("lon")
            suppress_travel = poi.get("kind") in {
                "_shopping_store",
                "_gemini_stop_seq",
                "_transit_route",
                "_transit_stop_seq",
            }
            if plat is not None and plon is not None and not suppress_travel:
                live_m = int(math.sqrt(
                    ((self.lat - plat) * 111000) ** 2 +
                    ((self.lon - plon) * 111000 * math.cos(math.radians(self.lat))) ** 2
                ))
                live_bearing = compass_name(bearing_deg(self.lat, self.lon, plat, plon))
                label = re.sub(
                    r'\d+ metres [\w-]+',
                    f"{live_m} metres {live_bearing}",
                    label,
                    count=1,
                )
                travel = self._poi_travel_time_label(live_m)
                if not re.search(r'\bmin (?:walk|drive)\b', label):
                    label = f"{label}, {travel}"
                shortcut = _shortcut_label("Ctrl+Enter")
                if (self._transit and
                    poi.get("kind") not in
                    ("_transit_stop","_transit_route","_transit_stop_seq") and
                    self._gtfs_is_transit_poi(poi) and
                    shortcut not in label):
                    label = label + f" — {shortcut} for transit info"
            labels.append(label)
        self.listbox.Set(labels)
        if self._poi_list:
            self.listbox.SetSelection(self._poi_index)
        self._poi_populating = False
        self.listbox.SetFocus()

    def _on_poi_listbox_select(self, event):
        if self._poi_populating or not self._poi_list:
            event.Skip()
            return
        sel = self.listbox.GetSelection()
        if sel != wx.NOT_FOUND and 0 <= sel < len(self._poi_list):
            self._poi_index = sel
        event.Skip()

    def _present_poi_list(self):
        if not self._poi_list:
            return
        # Don't overwrite the listbox if the user has drilled into a submenu
        if getattr(self, '_poi_explore_stack', []):
            return
        self._show_poi_in_listbox()
        wx.CallAfter(self.listbox.SetFocus)

    def _close_poi_list(self):
        self._poi_list = []
        self._poi_index = 0
        self._poi_explore_stack = []
        self._active_transit_route = None   # clear so Ctrl+Alt+F reverts to route mode
        self._loading = False
        label = getattr(self, 'street_label', '') or getattr(self, 'last_location_str', '')
        if label:
            self._poi_populating = True
            self.listbox.Set([label])
            self.listbox.SetSelection(0)
            self._poi_populating = False
        self.listbox.SetFocus()

    def _replace_poi_action_item(self, msg, clear_model=False):
        """Replace the selected POI row after an action is chosen."""
        if clear_model:
            self._poi_list = []
            self._poi_index = 0
            self._poi_explore_stack = []
        self._poi_populating = True
        self.listbox.Set([msg])
        self.listbox.SetSelection(0)
        self._poi_populating = False
        self.listbox.SetFocus()

    def _announce_and_restore_poi_list(self, msg, delay_ms=1200):
        """Speak a transient message via AO2, then restore the current POI list."""
        _speak(msg)
        if self._poi_list:
            def restore():
                if self._poi_list:
                    self._show_poi_in_listbox()
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
        if not entries:
            self._status_update(
                "No favourites saved. Press Ctrl+Shift+F on Windows/Linux or Command+Shift+F on Mac to add one.",
                force=True,
            )
            return
        dlg = FavouritesDialog(self, entries)
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

    def _jump_to_favourite(self, entry):
        try:
            poi = self._favourite_as_poi(entry)
        except Exception:
            self._status_update("Favourite has no valid position.", force=True)
            return
        self._poi_list = [poi]
        self._poi_index = 0
        self._poi_explore_stack = []
        self._jump_to_poi()
        wx.CallAfter(self.listbox.SetFocus)

    def _navigate_to_favourite(self, entry):
        try:
            lat = float(entry.get("lat"))
            lon = float(entry.get("lon"))
        except (TypeError, ValueError):
            self._status_update("Favourite has no valid position.", force=True)
            return
        name = entry.get("name", "Favourite")
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
                        if kind in ("_leaf", "_transit_stop_seq", "_gemini_stop_seq"):
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

        # TAB: allow focus traversal while a POI list is open; swallow otherwise.
        if key == wx.WXK_TAB:
            if poi_list_open:
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

        self.on_key(event)

    def _transit_drill_or_jump(self):
        """Enter on a POI — drill into transit children, load Google Places, or jump."""
        if not (self._poi_list and self._poi_index < len(self._poi_list)):
            return
        self._sync_poi_selection_from_listbox()
        poi = self._poi_list[self._poi_index]
        kind = poi.get("kind", "")
        
        # Handle "Ask Gemini for store directory" sentinel
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
                names = self._gemini.ask_shopping(n, la, lo)
                done_event.set()
                try:
                    self.sound.stop()
                except Exception:
                    pass
                if not names:
                    wx.CallAfter(_speak, f"No store directory found for {n}.")
                    return
                child_pois = [
                    {
                        "label":          store,
                        "lat":            la,
                        "lon":            lo,
                        "kind":           "_shopping_store",
                        "_store_name":    store,
                        "_centre_name":   n,
                    }
                    for store in names
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

        # Handle "Get times" sentinel
        if kind == "sentinel" and poi.get("sentinel_type") == "get_times":
            operator   = poi.get("operator", "")
            service    = poi.get("service", "")
            route_name = poi.get("route_name", "")
            self._transit_nav_announce(f"Fetching timetable for {operator} {service}...")
            def _fetch_times():
                text = self._gemini.ask_times(operator, service, route_name)
                # Push as a single-item explore leaf so screenreader can read
                # the full text uninterrupted. Backspace returns to the stop list.
                leaf = [{
                    "label": text,
                    "lat":   poi.get("lat", 0),
                    "lon":   poi.get("lon", 0),
                    "kind":  "_gemini_stop_seq",
                }]
                def _show():
                    self._poi_explore_stack.append((list(self._poi_list), self._poi_index))
                    self._poi_list  = leaf
                    self._poi_index = 0
                    self.listbox.Set([text])
                    self.listbox.SetSelection(0)
                    self.listbox.SetFocus()
                wx.CallAfter(_show)
            threading.Thread(target=_fetch_times, daemon=True).start()
            return
        
        if kind == "_shopping_store":
            store_name  = poi.get("_store_name", poi.get("label", ""))
            centre_name = poi.get("_centre_name", "")
            _speak(f"Looking up {store_name}…")
            def _fetch_detail(s=store_name, c=centre_name, p=poi):
                text = self._gemini.ask_store_detail(s, c)
                leaf = [{
                    "label": text,
                    "lat":   p.get("lat", 0),
                    "lon":   p.get("lon", 0),
                    "kind":  "_gemini_stop_seq",
                }]
                def _push():
                    self._poi_explore_stack.append((list(self._poi_list), self._poi_index))
                    self._poi_list  = leaf
                    self._poi_index = 0
                    self.listbox.Set([text])
                    self.listbox.SetSelection(0)
                    self.listbox.SetFocus()
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
        elif kind == "_ask_gemini":
            if not self._gemini.is_configured:
                print("[Gemini] Not configured — no API key.")
                self._transit_nav_announce(
                    "No Gemini API key configured. "
                    "Add your key in Settings (Ctrl+comma) under Gemini API key.")
                return
            self._status_update("Asking Gemini for long-distance services…")
            threading.Thread(
                target=self._explore_gemini_transit,
                args=(poi,), daemon=True).start()
        elif kind == "_gemini_service":
            self._explore_gemini_service(poi)
        elif kind == "_gemini_stop_seq":
            pass   # leaf node
        else:
            self._street_confirm_jump()

    def _poi_entry_uses_action_dialog(self, poi):
        kind = (poi or {}).get("kind", "")
        if kind in {
            "_transit_stop",
            "_transit_route",
            "_transit_stop_seq",
            "_ask_gemini",
            "_gemini_service",
            "_gemini_stop_seq",
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
        if self._poi_entry_uses_action_dialog(poi):
            self._poi_enter_action_dialog()
        else:
            self._transit_drill_or_jump()

    def _poi_enter_action_dialog(self):
        """Enter on a POI — choose between current POI action and GPS route."""
        if not self._poi_list:
            self._status_update("No points of interest loaded.", force=True)
            return
        self._sync_poi_selection_from_listbox()
        if not (0 <= self._poi_index < len(self._poi_list)):
            self._status_update("No point of interest selected.", force=True)
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
            self._replace_poi_action_item(f"Exploring {name}...")
            self._transit_drill_or_jump()
            return

        if sel == 2:
            self._add_current_favourite()
            self.listbox.SetFocus()
            return

        lat = poi.get("lat")
        lon = poi.get("lon")
        if lat is None or lon is None:
            self._status_update(f"No GPS coordinate for {name}.", force=True)
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
            self._status_update("No points of interest loaded.", force=True)
            return
        poi = self._poi_list[self._poi_index]
        plat = poi["lat"]; plon = poi["lon"]
        name = poi["label"].split(",")[0]
        
        # Check if POI is in water
        if not _IS_LAND(plat, plon):
            self._status_update(f"Can't jump to {name}. Location is in water.", force=True)
            return
        
        # Check if POI is within already-loaded area by testing if streets exist there
        within_loaded = False
        if self._road_fetched and self._road_segments:
            test_road, _ = self._street_fetcher.nearest_road(plat, plon, self._road_segments)
            within_loaded = (test_road != "No street data nearby")

        self.lat = plat
        self.lon = plon

        # ── Transit hub: check for eateries within walking distance ──────────
        if self._gtfs_is_transit_poi(poi):
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
            self.last_city_found = ""
            self._force_geocode_suburb_once = True
            self._last_jump_display_label = name
            self._last_jump_display_until = time.time() + 1.5
            wx.CallAfter(self.map_panel.set_position, self.lat, self.lon, False, "")
            if getattr(self, "_prefetch_in_progress", False):
                self._status_update("Street download in progress. Please wait.")
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
            
            # Force fetch to ensure data is current for this location
            # Fetch fresh data — guard against concurrent fetches
            if not self._fetch_in_progress:
                self._fetch_in_progress = True
                self._distance_since_fetch = 0
                self._last_fetch_lat = self.lat
                self._last_fetch_lon = self.lon
                threading.Thread(target=self._query_street, daemon=True).start()
            
            threading.Thread(target=self._fetch_poi_intersection,
                             args=(plat, plon, name,
                                   poi.get("street", "")), daemon=True).start()
        else:
            self._status_update(f"Jumping to {name}.  Loading streets...")
            self._loading = True
            threading.Thread(
                target=self._load_streets_after_poi_jump,
                args=(plat, plon, name, poi.get("street", "")),
                daemon=True
            ).start()
    
    def _load_streets_after_poi_jump(self, lat, lon, poi_name, known_street=""):
        """Load streets after POI jump - tries cache first."""
        try:
            from street_data import geocode_location, _load_road_cache
            geo = geocode_location(lat, lon)
            if geo:
                self._current_suburb = geo.get("suburb")
                self._current_country_code = geo.get("country_code", "")
                radius = geo.get("radius", 3000)
                self._street_radius  = radius
                self._street_barrier = int(radius * 0.9)
                self._street_bbox = geo.get("bbox")
                self._prefetch_geo_features_for_point(lat, lon)
            else:
                self._street_radius  = 3000
                self._street_barrier = 2700
                self._current_suburb = None
            cache_entry = _load_road_cache(
                self._street_fetcher._cache_dir,
                lat, lon,
                suburb_name=self._current_suburb
            )
            
            if cache_entry and cache_entry.get("segments"):
                # Cache hit - verify it covers this location
                cached_segments = cache_entry.get("segments", [])
                test_label, cross = self._street_fetcher.nearest_road(lat, lon, cached_segments)
                
                if test_label in ("No street data nearby", "Unknown", "", "No street data"):
                    self._loading = False
                    suburb_name = self._current_suburb or "this area"
                    wx.CallAfter(self._status_update, f"Jumped to {poi_name}. No cached streets.")
                    wx.CallAfter(self._confirm_poi_suburb_download, lat, lon, poi_name, known_street, suburb_name)
                else:
                    self._road_segments  = cached_segments
                    self._address_points = cache_entry.get("addresses", [])
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
                    
                    # Fetch POI intersection info
                    threading.Thread(target=self._fetch_poi_intersection,
                                   args=(lat, lon, poi_name, known_street), daemon=True).start()
            else:
                self._loading = False
                suburb_name = self._current_suburb or "this area"
                wx.CallAfter(self._status_update, f"Jumped to {poi_name}. No cached streets.")
                wx.CallAfter(self._confirm_poi_suburb_download, lat, lon, poi_name, known_street, suburb_name)
        except Exception as e:
            miab_log("poi_jump", f"Cache load error: {e}", self.settings)
            self._loading = False
            suburb_name = getattr(self, '_current_suburb', None) or "this area"
            wx.CallAfter(self._status_update, f"Jumped to {poi_name}. Error loading cache.")
            wx.CallAfter(self._confirm_poi_suburb_download, lat, lon, poi_name, known_street, suburb_name)

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
            self._status_update("No street data loaded. Press F11 first.", force=True)
            return

        self._street_search_dlg = _StreetSearchFrame(self)
        self._street_search_dlg.Show()

    def _jump_to_street(self, street_name, fetch_geometry=False, house_number=""):
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
            self._status_update(
                f"Could not locate {street_name} yet. The suburb may still be loading in background.",
                force=True,
            )
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
            print(f"[StreetJump] Seeking #{house_number} on '{street_name}' "
                  f"(bare='{bare_target}'). {len(on_street)} address points on street. "
                  f"Numbers: {sorted(set(ap['number'] for ap in on_street))[:20]}")

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
                    print(f"[StreetJump] Snapped #{best_pt['number']} onto {street_name} "
                          f"({snap_d:.1f}m from address point) at ({self.lat:.5f},{self.lon:.5f})")
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
                        print(f"[StreetJump] No projection found for #{best_pt['number']}; "
                              f"using address point ({self.lat:.5f},{self.lon:.5f})")

            # Exact match first
            exact = [ap for ap in on_street
                     if ap.get('number', '').strip().lower() == num_want
                     and ap.get('lat') and ap.get('lon')]
            if exact:
                snap_d, _from_here, best_pt, projected = _pick_address_candidate(exact)
                print(f"[StreetJump] Exact match #{best_pt['number']} at ({best_pt['lat']:.5f},{best_pt['lon']:.5f})")
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
                    print(f"[StreetJump] Fuzzy match #{best_pt['number']} at ({best_pt['lat']:.5f},{best_pt['lon']:.5f})")
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
                        print(f"[StreetJump] No exact match for #{house_number}; nearest known "
                              f"number is #{best_pt['number']} at ({best_pt['lat']:.5f},{best_pt['lon']:.5f})")
                        _apply_address_candidate(best_pt, projected, snap_d)
                        best_lat, best_lon = self.lat, self.lon
                        number_found = True
                        resolved_house_number = str(best_pt.get('number') or "").strip()
                        _speak(f"Number {house_number} not found. Jumping to nearest known number, "
                               f"{resolved_house_number} {street_name}.")
                    else:
                        print(f"[StreetJump] No match for #{house_number} on '{street_name}'")
                        _speak(f"Number {house_number} not found. Jumping to nearest part of {street_name}.")
            else:
                print(f"[StreetJump] No match for #{house_number} on '{street_name}'")
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
                self._walk_cross_options = self._walk_get_cross_streets(nid, street_name)
                self._walk_cross_idx = 0
                wx.CallAfter(self.map_panel.set_position, self.lat, self.lon, True, street_name)
                desc = self._walk_describe_intersection(nid, street_name, self._walk_heading)
                addr_prefix = f"{self._jump_address_number} " if house_number and number_found else ""
                self.update_ui(f"Jumped to {addr_prefix}{street_name}.  {desc}")
                wx.CallAfter(self.listbox.SetFocus)
                return

        addr_prefix = f"{self._jump_address_number} " if house_number and number_found else ""
        _nr, _nc = self._nearest_road(self.lat, self.lon)
        miab_log("snap",
                 f"_jump_to_street: landed ({self.lat:.5f},{self.lon:.5f}); "
                 f"nearest_road='{_nr}' cross='{_nc}'; pin=({self._jump_street_pin_lat},{self._jump_street_pin_lon})",
                 self.settings)
        self.update_ui(f"Jumped to {addr_prefix}{street_name}.")
        wx.CallAfter(self.map_panel.set_position, self.lat, self.lon, True, street_name)
        wx.CallAfter(self._update_street_display)
        wx.CallAfter(self.listbox.SetFocus)

    def _explore_poi(self):
        """Enter on a top-level explorable POI — drill into its child elements."""
        if not self._poi_list:
            self._status_update("No points of interest loaded.", force=True)
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
            self._status_update("Already at top level POI list.", force=True)
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
            print(f"[Explore] error: {e}")
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
            self._status_update("No point of interest selected.", force=True)
            return True
        self._sync_poi_selection_from_listbox()
        self._jump_to_poi()
        return True

    def _street_confirm_explore(self):
        """Ctrl+Enter — explore selected POI. Transit POIs get GTFS lookup; others show OSM tags."""
        if not (self._poi_list and self._poi_index < len(self._poi_list)):
            self._status_update("No point of interest selected.", force=True)
            return True
        self._sync_poi_selection_from_listbox()
        poi = self._poi_list[self._poi_index]
        # Transit POI handling
        is_transit = self._gtfs_is_transit_poi(poi)
        if is_transit:
            name = poi["label"].split(",")[0]
            self._status_update(f"Loading transit routes near {name}...")
            threading.Thread(target=self._explore_transit_poi,
                             args=(poi,), daemon=True).start()
            return True
        if self._poi_explore_stack:
            return True
        # Shopping centres — intercept regardless of explorable flag
        # (OSM shopping centres are often nodes which don't get explorable=True)
        if poi.get("kind", "").lower() in ("mall", "shopping centre", "department store"):
            name     = poi["label"].split(",")[0].strip()
            lat      = poi["lat"]
            lon      = poi["lon"]
            ask_item = [{
                "label":         f"Ask Gemini for store directory — {name}",
                "lat":           lat,
                "lon":           lon,
                "kind":          "sentinel",
                "sentinel_type": "ask_shopping",
                "_centre_name":  name,
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
        if not self._poi_list or self._poi_index >= len(self._poi_list):
            self._status_update("No point of interest selected.", force=True)
            return
        self._sync_poi_selection_from_listbox()
        poi = self._poi_list[self._poi_index]
        self._open_poi_website_for(poi)

    def _open_poi_website_for(self, poi):
        """Open website for a specific POI dict, falling back to HERE once if needed."""
        tags = poi.get("tags") or {}
        url  = (poi.get("website") or
                tags.get("website") or
                tags.get("contact:website") or "")
        if not url and self.settings.get("here_api_key", "").strip() and not poi.get("_here_checked"):
            name = (poi.get("name") or poi.get("label", "")).split(",")[0].strip()
            self._status_update(f"Looking up website for {name}...")
            def _fetch():
                detail = self._here.fetch_poi_detail(
                    name, poi.get("lat", self.lat), poi.get("lon", self.lon))
                poi.update(detail)
                poi["_here_checked"] = True
                fetched_url = (detail.get("website") or "").strip()
                if fetched_url:
                    if not fetched_url.startswith(("http://", "https://")):
                        fetched_url = "https://" + fetched_url
                    import webbrowser
                    wx.CallAfter(lambda: webbrowser.open(fetched_url))
                    wx.CallAfter(lambda: self._status_update(f"Opening {fetched_url}"))
                else:
                    import webbrowser, urllib.parse
                    suburb = getattr(self, "_current_suburb", "")
                    query = f"{name} {suburb}".strip()
                    search_url = f"https://www.google.com/search?q={urllib.parse.quote(query)}"
                    wx.CallAfter(lambda: webbrowser.open(search_url))
                    wx.CallAfter(lambda: self._status_update(f"No website found — opening Google search for {query}"))
            threading.Thread(target=_fetch, daemon=True).start()
            return
        if not url:
            import webbrowser, urllib.parse
            name = (poi.get("name") or poi.get("label", "")).split(",")[0].strip()
            suburb = getattr(self, "_current_suburb", "")
            query = f"{name} {suburb}".strip()
            search_url = f"https://www.google.com/search?q={urllib.parse.quote(query)}"
            webbrowser.open(search_url)
            self._status_update(f"No website found — opening Google search for {query}")
            return
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        # Test URL before opening
        try:
            req = urllib.request.Request(url, method='HEAD', headers={'User-Agent': 'MapInABox/1.0'})
            with urllib.request.urlopen(req, timeout=5) as resp:
                if 200 <= resp.status < 400:
                    import webbrowser
                    webbrowser.open(url)
                    self._status_update(f"Opening {url}")
                else:
                    # URL returned error, fall back to Google search
                    self._status_update(f"Website not found (HTTP {resp.status}) — searching instead")
                    import webbrowser, urllib.parse
                    name = (poi.get("name") or poi.get("label", "")).split(",")[0].strip()
                    suburb = getattr(self, "_current_suburb", "")
                    query = f"{name} {suburb}".strip()
                    search_url = f"https://www.google.com/search?q={urllib.parse.quote(query)}"
                    webbrowser.open(search_url)
        except Exception as exc:
            # URL failed to connect, fall back to Google search
            self._status_update(f"Website unavailable — searching instead")
            import webbrowser, urllib.parse
            name = (poi.get("name") or poi.get("label", "")).split(",")[0].strip()
            suburb = getattr(self, "_current_suburb", "")
            query = f"{name} {suburb}".strip()
            search_url = f"https://www.google.com/search?q={urllib.parse.quote(query)}"
            webbrowser.open(search_url)

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

        _primary, stops = self._gtfs_nearby_stops(
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
            routes_here = self._gtfs_routes_for_stop(stop_id, feed_id)
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
                headsign, times = self._gtfs_next_departures(stop_id, r["route_id"], feed_id)
                # If no headsign from departures, get one from route_stops so
                # Enter still works even when no more services run today
                if not headsign:
                    fallback_stops = self._gtfs_stops_for_route(r["route_id"], feed_id)
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
        """Push route list onto explore stack, with Gemini option for major stations."""
        if orig_poi is not None and self._transit.is_major_station(orig_poi):
            child_pois.append({
                "label":      "Ask Gemini for long-distance services…",
                "lat":        orig_poi["lat"],
                "lon":        orig_poi["lon"],
                "kind":       "_ask_gemini",
                "_poi_name":  parent_name,
            })
        self._poi_explore_stack.append((list(self._poi_list), self._poi_index))
        self._poi_list  = child_pois
        self._poi_index = 0
        self._poi_index = 0
        # Count excludes the Gemini sentinel if one was added
        gemini_added = (orig_poi is not None and 
                        self._transit.is_major_station(orig_poi))
        n = len(child_pois) - (1 if gemini_added else 0)
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
                    self._status_update(
                        "No active transit route — open a route first.", force=True)
                return
            if kc == wx.WXK_BACK:
                idx = lb.GetSelection()
                if 0 <= idx < len(child_pois):
                    kind = child_pois[idx].get("kind", "")
                    if kind in ("_leaf", "_transit_stop_seq", "_gemini_stop_seq"):
                        self._transit_drill_back_one_level = True
                dlg.EndModal(wx.ID_CANCEL)
                return
            if kc in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
                dlg.EndModal(wx.ID_OK)
                return
            if kc == wx.WXK_ESCAPE:
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
                if kind in ("_leaf", "_transit_stop_seq", "_gemini_stop_seq"):
                    continue

                # Get times sentinel
                if kind == "sentinel" and poi.get("sentinel_type") == "get_times":
                    op = poi.get("operator", "")
                    svc = poi.get("service", "")
                    rn  = poi.get("route_name", "")
                    self._status_update(f"Fetching timetable for {op} {svc}...")
                    def _fetch_t(op=op, svc=svc, rn=rn):
                        text = self._gemini.ask_times(op, svc, rn)
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
                    stops = self._gtfs_stops_for_route(
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

                # Gemini service -> stops + sentinels
                if kind == "_gemini_service":
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
                            "kind": "_gemini_service",
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

                # Ask Gemini for long-distance services
                if kind == "_ask_gemini":
                    self._hub_transit_mode = True
                    self._explore_gemini_transit(poi)
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
        stops = self._gtfs_stops_for_route(route_id, feed_id, headsign=headsign)
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
                print(f"[Transit] YOU ARE HERE matched '{sname}' for origin '{origin}'")
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
            print(f"[Transit] No YOU ARE HERE match for '{origin_bare}'. First 5: {all_names}")
        # Stash raw GTFS stops (with real coords) so Ctrl+Alt+F can query food nearby
        self._active_transit_route = {"name": route_name, "stops": stops}

        self._poi_explore_stack.append((list(self._poi_list), self._poi_index))
        self._poi_list  = child_pois
        self._poi_index = focus_index
        self._show_poi_in_listbox()
        self._transit_nav_announce(
            f"{len(child_pois)} stops on {route_name}.  "
            f"Arrow to browse.  Backspace to go back.")

    def _explore_gemini_transit(self, poi: dict) -> None:
        """Background: call Gemini and push a flat route list.

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

        routes = self._gemini.ask_transit(lat, lon, display_name)
        done_event.set()  # stop progress thread before touching the listbox
        try:
            self.sound.stop()
        except Exception:
            pass

        if not routes:
            wx.CallAfter(self._status_update,
                         f"Gemini found no regional services at {name}.")
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
                "kind":        "_gemini_service",
                "_operator":   operator,
                "_service":    service,
                "_route_name": route_name,
                "_stops":      stops,
            })

        # Small delay so any in-flight progress CallAfters drain before we push results
        import time as _time
        _time.sleep(0.05)
        wx.CallAfter(self._push_gemini_flat, child_pois, name)

    def _push_gemini_flat(self, child_pois: list, parent_name: str) -> None:
        """Push flat Gemini route list — dialog if hub mode, listbox otherwise."""
        if getattr(self, "_hub_transit_mode", False):
            self._hub_transit_mode = False
            self._show_transit_drill_dialog(
                child_pois,
                title=f"Gemini: {parent_name} — {len(child_pois)} route(s)",
                hint="Enter for stops  |  Escape to close",
                focus_index=0,
            )
            return
        self._poi_explore_stack.append((list(self._poi_list), self._poi_index))
        self._poi_list  = child_pois
        self._poi_index = 0
        self._show_poi_in_listbox()
        self._transit_nav_announce(
            f"Gemini found {len(child_pois)} regional route(s) at {parent_name}.  "
            f"Arrow to browse, Enter for stops, Backspace to go back.")

    def _explore_gemini_service(self, poi: dict) -> None:
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
                "kind":  "_gemini_stop_seq",
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
                "kind":        "_gemini_service",
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
        suppressed = _load_suppressed()
        entry = {
            "name":     name.lower(),
            "lat":      round(float(poi.get("lat", 0)), 4),
            "lon":      round(float(poi.get("lon", 0)), 4),
            "kind":     poi.get("kind", ""),
            "source":   poi.get("source", "osm"),
            "reported": json.dumps({"t": time.time()}),
        }
        suppressed.append(entry)
        _save_suppressed(suppressed)

        # Remove from current list immediately
        self._poi_list.pop(self._poi_index)
        self._poi_index = max(0, self._poi_index - 1)
        if self._poi_list:
            self._show_poi_in_listbox()
            wx.CallAfter(self.listbox.SetFocus)
        else:
            self.listbox.Clear()
            wx.CallAfter(_speak, "No more points of interest.")
            wx.CallAfter(self.listbox.SetFocus)

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
                    print(f"[OSM Note] Posted for '{name}': HTTP {resp.status}")
                wx.CallAfter(self._status_update,
                    f"'{name}' reported to OpenStreetMap.")
            except Exception as e:
                print(f"[OSM Note] Failed: {e}")
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
        renamed = _load_renamed()
        # Remove any existing entry for this POI first
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

        # Update in current list immediately
        poi = dict(poi)
        poi["name"] = new_name
        old_label = poi.get("label", "")
        poi["label"] = old_label.replace(old_label.split(",")[0], new_name, 1)
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
                    print(f"[OSM Note] Rename posted for '{old_name}': HTTP {resp.status}")
                wx.CallAfter(self._status_update,
                    f"Renamed to '{new_name}' and reported to OpenStreetMap.")
            except Exception as e:
                print(f"[OSM Note] Rename report failed: {e}")
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
        """F9 — toggle the map panel between full screen and normal split view."""
        self._map_fullscreen = not self._map_fullscreen
        if self._map_fullscreen:
            self._h_sizer.GetItem(self.map_panel).SetProportion(999)
            self._h_sizer.GetItem(self.listbox).SetProportion(1)
            self._h_sizer.GetItem(self.listbox).SetMinSize((1, -1))
            self._h_sizer.GetItem(self.info_panel).SetProportion(0)
            self._h_sizer.GetItem(self.info_panel).SetMinSize((1, -1))
            self.info_panel.Hide()
            self._status_update("Map maximised.", force=True)
        else:
            self._h_sizer.GetItem(self.map_panel).SetProportion(3)
            self._h_sizer.GetItem(self.listbox).SetProportion(1)
            self._h_sizer.GetItem(self.listbox).SetMinSize((-1, -1))
            self.info_panel.Show()
            self._h_sizer.GetItem(self.info_panel).SetProportion(1)
            self._h_sizer.GetItem(self.info_panel).SetMinSize((250, -1))
            self._status_update("Map restored.", force=True)
        self._h_sizer.Layout()
        self.map_panel.Refresh()
        self.listbox.SetFocus()

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
        self.sound.play_spatial_tone(lat, lon, self._spatial_tone_bounds())

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
                num = self._nearest_address_number(self.lat, self.lon, label, radius=200)
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

    def _prompt_mark_slot(self, remove=False):
        """Ask for mark slot 1-3 and apply immediately on number press."""
        title = "Remove Mark" if remove else "Store Mark"
        prompt = "Remove mark 1, 2, or 3." if remove else "Store mark 1, 2, or 3."
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
            if remove:
                marks = getattr(self, "_map_marks", {})
                if slot in marks:
                    del marks[slot]
                    self._status_update(f"mark {slot} removed", force=True)
                else:
                    self._status_update(f"mark {slot} not set", force=True)
            else:
                coords, name = self._current_map_place()
                self._map_marks[slot] = {"coords": coords, "name": name}
                self._status_update(f"mark {slot} set to {name}", force=True)
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
        wx.CallAfter(self.listbox.SetFocus)

    def _set_map_destination(self):
        lat, lon = self._poi_lat_lon_if_focused()
        poi_focused = (lat, lon) != (self.lat, self.lon)
        if poi_focused:
            self._sync_poi_selection_from_listbox()
            idx = getattr(self, '_poi_index', -1)
            pois = getattr(self, '_poi_list', [])
            poi = pois[idx] if 0 <= idx < len(pois) else None
            name = poi.get('label', '').split(',')[0] if poi else f"{lat:.4f}, {lon:.4f}"
            coords = (float(lat), float(lon))
        else:
            coords, name = self._current_map_place()
        self._map_destination = {"coords": coords, "name": name}
        self._status_update(f"Destination set to {name}.", force=True)

    def _prompt_destination_mark_slot(self):
        marks = getattr(self, "_map_marks", {})
        if not marks:
            self._status_update("No marks set. Press M then 1, 2, or 3.", force=True)
            return

        choices = []
        slots = []
        for slot in (1, 2, 3):
            mark = marks.get(slot)
            if not mark:
                continue
            choices.append(f"Mark {slot}: {mark.get('name', 'current position')}")
            slots.append(slot)
        if not choices:
            self._status_update("No marks set. Press M then 1, 2, or 3.", force=True)
            return

        dlg = wx.SingleChoiceDialog(
            self, "Choose destination mark:", "Set Destination", choices)
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            self._status_update("Destination unchanged.", force=True)
            wx.CallAfter(self.listbox.SetFocus)
            return
        slot = slots[dlg.GetSelection()]
        dlg.Destroy()
        mark = marks[slot]
        self._map_destination = {
            "coords": mark["coords"],
            "name": f"mark {slot}, {mark.get('name', f'mark {slot}')}",
        }
        self._status_update(
            f"Destination set to {self._map_destination['name']}.",
            force=True)
        wx.CallAfter(self.listbox.SetFocus)

    def _report_mark_to_destination(self, slot):
        mark = getattr(self, "_map_marks", {}).get(slot)
        if not mark:
            self._status_update(
                f"mark {slot} not set. Press Ctrl M on Windows/Linux or Command M on Mac, then {slot}.",
                force=True,
            )
            return

        dest = getattr(self, "_map_destination", None)

        if self.street_mode and not dest:
            # Street mode with no destination: report distance from HERE to the mark.
            origin = (float(self.lat), float(self.lon))
            target = mark["coords"]
            mark_name = mark.get("name", f"mark {slot}")
            km = dist_km(origin[0], origin[1], target[0], target[1])
            dist_str = (f"{int(km * 1000)} metres" if km < 1.0
                        else f"{km:.2f} kilometres" if km < 10.0
                        else f"{km:.1f} kilometres")
            direction = compass_name(bearing_deg(origin[0], origin[1], target[0], target[1])).lower()
            fallback = f"Mark {slot}, {mark_name}, is {dist_str} {direction} from here."
            ors_key = self.settings.get("ors_api_key", "").strip()
            if not ors_key:
                self._status_update(fallback, force=True)
                return
            self._status_update("Calculating walking distance...", force=True)
            def _fetch_walk(origin=origin, target=target, fallback=fallback,
                            mark_name=mark_name):
                try:
                    route_dist, route_time = self._ors_route_summary(
                        origin, target, ors_key, profile="foot-walking")
                    msg = (f"Mark {slot}, {mark_name}, is {route_dist} walking from here; "
                           f"about {route_time}. Straight line: {fallback.split('is ')[1]}")
                    wx.CallAfter(self._status_update, msg, True)
                except Exception as exc:
                    print(f"[ORS] Mark walk route failed: {exc}")
                    wx.CallAfter(self._status_update, fallback, True)
            threading.Thread(target=_fetch_walk, daemon=True).start()
            return
        if not dest:
            self._status_update("Destination not set. Press D at the destination.", force=True)
            return
        origin = mark["coords"]
        target = dest["coords"]
        km = dist_km(origin[0], origin[1], target[0], target[1])
        if km < 1.0:
            dist_str = f"{int(km * 1000)} metres"
        elif km < 10.0:
            dist_str = f"{km:.2f} kilometres"
        else:
            dist_str = f"{km:.1f} kilometres"
        direction = compass_name(bearing_deg(origin[0], origin[1], target[0], target[1])).lower()
        dest_name = dest.get("name", "destination")
        mark_name = mark.get("name", f"mark {slot}")
        fallback = f"{dest_name} is {dist_str} {direction} from mark {slot}, {mark_name}."
        ors_key = self.settings.get("ors_api_key", "").strip()
        if not ors_key:
            self._status_update(fallback, force=True)
            return

        self._status_update("Calculating route...", force=True)

        def _fetch_route():
            try:
                route_dist, route_time = self._ors_route_summary(origin, target, ors_key)
                msg = (
                    f"{dest_name} is {route_dist} by road from mark {slot}, {mark_name}; "
                    f"estimated driving time {route_time}. "
                    f"Straight line: {dist_str} {direction}."
                )
                wx.CallAfter(self._status_update, msg, True)
            except Exception as exc:
                print(f"[ORS] Mark route failed: {exc}")
                wx.CallAfter(self._status_update, fallback, True)

        threading.Thread(target=_fetch_route, daemon=True).start()

    def _ors_route_summary(self, origin, target, api_key, profile="driving-car"):
        cache_key = self._ors_route_cache_key(origin, target) + f"_{profile}"
        cached = self._ors_route_cache_get(cache_key)
        if cached:
            return cached["distance_text"], cached["duration_text"]

        body = json.dumps({
            "coordinates": [
                [float(origin[1]), float(origin[0])],
                [float(target[1]), float(target[0])],
            ],
            "instructions": False,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.openrouteservice.org/v2/directions/{profile}/geojson",
            data=body,
            headers={
                "Authorization": api_key,
                "Content-Type": "application/json",
                "Accept": "application/json, application/geo+json",
                "User-Agent": "MapInABox/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        error_msg = (data.get("error") or {}).get("message", "")
        if "too short" in error_msg.lower() or "point was not found" in error_msg.lower():
            raise RuntimeError(f"ORS: {error_msg}")
        features = data.get("features") or []
        if not features:
            raise RuntimeError("ORS returned no route.")
        summary = ((features[0].get("properties") or {}).get("summary") or {})
        distance_m = float(summary.get("distance", 0))
        duration_s = float(summary.get("duration", 0))
        if distance_m <= 0 or duration_s <= 0:
            raise RuntimeError("ORS route summary missing distance or duration.")
        distance_text = self._format_route_distance(distance_m)
        duration_text = self._format_route_duration(duration_s)
        self._ors_route_cache_set(cache_key, distance_text, duration_text)
        return distance_text, duration_text

    @staticmethod
    def _ors_route_cache_key(origin, target):
        return "|".join([
            "driving-car",
            f"{float(origin[0]):.5f},{float(origin[1]):.5f}",
            f"{float(target[0]):.5f},{float(target[1]):.5f}",
        ])

    def _load_ors_route_cache(self):
        try:
            with open(ORS_ROUTE_CACHE_PATH, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {}

    def _save_ors_route_cache(self, cache):
        try:
            with open(ORS_ROUTE_CACHE_PATH, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            print(f"[ORS] Cache save failed: {exc}")

    def _ors_route_cache_get(self, key, max_age_days=30):
        cache = self._load_ors_route_cache()
        entry = cache.get(key)
        if not isinstance(entry, dict):
            return None
        saved_at = float(entry.get("saved_at", 0) or 0)
        if time.time() - saved_at > max_age_days * 86400:
            return None
        if not entry.get("distance_text") or not entry.get("duration_text"):
            return None
        return entry

    def _ors_route_cache_set(self, key, distance_text, duration_text):
        cache = self._load_ors_route_cache()
        cache[key] = {
            "distance_text": distance_text,
            "duration_text": duration_text,
            "saved_at": time.time(),
        }
        if len(cache) > 500:
            items = sorted(
                cache.items(),
                key=lambda item: float((item[1] or {}).get("saved_at", 0) or 0),
                reverse=True)
            cache = dict(items[:500])
        self._save_ors_route_cache(cache)

    @staticmethod
    def _format_route_distance(distance_m):
        if distance_m < 1000:
            return f"{int(round(distance_m))} metres"
        km = distance_m / 1000.0
        if km < 10:
            return f"{km:.1f} kilometres"
        return f"{km:.0f} kilometres"

    @staticmethod
    def _format_route_duration(duration_s):
        minutes = max(1, int(round(duration_s / 60.0)))
        if minutes < 60:
            return f"about {minutes} minute{'s' if minutes != 1 else ''}"
        hours = minutes // 60
        mins = minutes % 60
        if mins == 0:
            return f"about {hours} hour{'s' if hours != 1 else ''}"
        return f"about {hours} hour{'s' if hours != 1 else ''} {mins} minute{'s' if mins != 1 else ''}"

    def _format_mark_distance(self, origin, target):
        km = dist_km(origin[0], origin[1], target[0], target[1])
        if km < 1.0:
            dist_str = f"{int(km * 1000)} metres"
        elif km < 10.0:
            dist_str = f"{km:.2f} kilometres"
        else:
            dist_str = f"{km:.1f} kilometres"
        direction = compass_name(
            bearing_deg(origin[0], origin[1], target[0], target[1])
        ).lower()
        return dist_str, direction

    def _flash_current_country(self):
        """F8 — flash the current country name and highlight its polygon on the map."""
        country = getattr(self, 'last_country_found', '')
        if not country or country == 'Open Water':
            return
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
        if hasattr(self, "_geo_features"):
            self._geo_features.cleanup_temp()
        pygame.quit()
        self.Destroy()
        os._exit(0)

    def _status_update(self, msg, force=False):
        """Transient background status (loading, connecting) — AO2 only."""
        if (not force
                and time.time() < getattr(self, '_suppress_status_until', 0)
                and not str(msg).startswith("Looking up address")
                and not getattr(self, '_address_lookup_in_progress', False)):
            print(f"[Status] Suppressed: {msg}")
            return
        _speak(msg)

    def _accessible_status(self, msg):
        """wx-safe AO2/Braille announcement that does not move list focus."""
        if time.time() < getattr(self, "_speech_from_menu_until", 0):
            wx.CallLater(150, self._status_update, msg, True)
        else:
            wx.CallAfter(self._status_update, msg, True)

    def _on_listbox_focus(self, event):
        """Suppress update_ui briefly when listbox regains focus so that any
        pending wx.CallAfter(update_ui) doesn't double-speak the street label."""
        self._suppress_update_ui_until = time.time() + 0.05
        event.Skip()

    def update_ui(self, msg, force=False):
        """Update listbox — fires selection-changed so screen reader announces once.
        Listbox always populated so focus-return never says unknown.
        Suppressed while user is browsing a POI list."""
        if getattr(self, '_poi_explore_stack', []):
            return
        if not force and time.time() < getattr(self, '_suppress_update_ui_until', 0):
            return
        _braille(msg)
        self._refresh_info_panel()
        self._poi_populating = True
        self.listbox.Set([msg])
        self.listbox.SetSelection(0)
        self._poi_populating = False

    def _update_location_focus(self, msg):
        """Update the focused location row, for real position changes."""
        self._suppress_update_ui_until = 0
        self.update_ui(msg, force=True)
        wx.CallAfter(self.listbox.SetFocus)

    def _get_street_orientation(self, street_name):
        """Calculate street orientation (N/S or E/W) from segment geometry.
        
        Returns 'north-south', 'east-west', or None if can't determine.
        """
        if not street_name:
            return None
        
        # Clean street name (remove type suffixes like "(road)")
        import re
        clean_name = re.sub(r'\s*\(.*?\)', '', street_name).strip()
        if not clean_name:
            return None
        
        # Find segment(s) for this street
        segments = getattr(self, '_road_segments', [])
        if not segments:
            return None
        
        # Look for matching segment
        import math
        matching_segments = 0
        bearings = []
        
        for seg in segments:
            seg_name = seg.get('name', '')
            seg_clean = re.sub(r'\s*\(.*?\)', '', seg_name).strip()
            
            if seg_clean.lower() == clean_name.lower():
                matching_segments += 1
                coords = seg.get('coords', [])
                if len(coords) >= 2:
                    # Calculate bearing from first to last point
                    lat1, lon1 = coords[0]
                    lat2, lon2 = coords[-1]
                    
                    # Calculate bearing
                    dlon = lon2 - lon1
                    dlat = lat2 - lat1
                    bearing = math.atan2(dlon, dlat) * 180 / math.pi
                    
                    # Normalize to 0-360
                    if bearing < 0:
                        bearing += 360
                    
                    bearings.append(bearing)
        
        if not bearings:
            return None
        
        # Use average bearing if multiple segments
        avg_bearing = sum(bearings) / len(bearings)
        
        # Determine orientation
        # N/S: 315-45° or 135-225°
        # E/W: 45-135° or 225-315°
        if (avg_bearing >= 315 or avg_bearing < 45) or (135 <= avg_bearing < 225):
            orientation = "north-south"
        else:
            orientation = "east-west"
        
        print(f"[Orientation] {clean_name}: {matching_segments} segments, avg_bearing={avg_bearing:.1f}° → {orientation}")
        
        return orientation
    
    def _announce_current_place_or_street(self):
        if getattr(self, '_walking_mode', False) and getattr(self, '_walk_street', None):
            # Add orientation for walking mode
            orientation = self._get_street_orientation(self._walk_street)
            suburb = getattr(self, "_current_suburb", "")
            if orientation and suburb:
                self.update_ui(f"{self._walk_street}, {orientation}, {suburb}")
            elif orientation:
                self.update_ui(f"{self._walk_street}, {orientation}")
            elif suburb:
                self.update_ui(f"{self._walk_street}, {suburb}")
            else:
                self.update_ui(self._walk_street)
            return
        if getattr(self, 'street_label', ''):
            # Add orientation for street mode
            orientation = self._get_street_orientation(self.street_label)
            suburb = getattr(self, "_current_suburb", "")
            if orientation and suburb:
                self.update_ui(f"{self.street_label}, {orientation}, {suburb}")
            elif orientation:
                self.update_ui(f"{self.street_label}, {orientation}")
            elif suburb:
                self.update_ui(f"{self.street_label}, {suburb}")
            else:
                self.update_ui(self.street_label)
            return
        self.update_ui(self.last_location_str or "Location unknown.")

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


    def _fetch_all_pois_background(self, address_points=None):
        """Background POI fetch for walk-announce. Delegates to PoiFetcher."""
        if getattr(self, "_recentring", False):
            return
        if not self.street_mode:
            return
        if getattr(self, "_poi_fetch_in_progress", False):
            miab_log("verbose", "Background fetch already in progress — skipping duplicate.", self.settings)
            return
        if address_points is None:
            address_points = getattr(self, "_address_points", [])

        # Respect poi_source setting — only use HERE if explicitly chosen
        poi_source = self.settings.get("poi_source", "osm")
        here_key   = self.settings.get("here_api_key", "").strip()
        if poi_source == "here" and here_key:
            self._poi_fetcher.set_here_key(here_key)
        else:
            self._poi_fetcher.set_here_key("")

        try:
            self._loading = True
            self._poi_fetch_in_progress = True
            pois = self._poi_fetcher.fetch_all_background(
                self.lat, self.lon, address_points
            )
            self._loading = False
            self._poi_fetch_in_progress = False
            # Discard if street mode was cancelled while fetching
            if not self.street_mode:
                miab_log(
                    "verbose",
                    "Background fetch complete but street mode cancelled — discarding.",
                    self.settings,
                )
                return
            _suppressed = _load_suppressed()
            _renamed    = _load_renamed()
            self._all_pois = _apply_renames(
                [p for p in pois if not _is_suppressed(p, _suppressed)],
                _renamed)
            self._poi_grid = self._build_poi_grid(pois)
            self._poi_fetch_lat = self.lat
            self._poi_fetch_lon = self.lon
            try:
                self._free_engine.set_pois(self._all_pois)
            except Exception:
                pass
            miab_log(
                "verbose",
                f"Grid index: {len(self._poi_grid)} occupied cells across {len(pois)} POIs.",
                self.settings,
            )
            wx.CallAfter(self._play_pois_ready_sound)
            if getattr(self, '_free_mode', False):
                wx.CallAfter(self._free_announce_poi_update)
        except Exception as e:
            self._loading = False
            self._poi_fetch_in_progress = False
            miab_log("errors", f"Background POI fetch error: {e}", self.settings)

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

    @staticmethod
    def _play_system_sound(kind: str = "default") -> None:
        """Play a brief system notification sound, cross-platform.

        Parameters
        ----------
        kind:
            One of ``"default"``, ``"balloon"``, ``"asterisk"``.
            Falls back to a pygame beep if the platform-specific call
            fails (e.g. on macOS, or Windows without the WAV files).
        """
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
        """1s timer — tick while streets or POIs are loading."""
        if getattr(self, '_loading', False):
            self.sound.play_poi_tone("both")

    def _play_pois_ready_sound(self):
        """Notification sound when background POI fetch completes."""
        self._play_system_sound("balloon")

    def _play_roads_ready_sound(self):
        """Notification sound when road data is ready."""
        self._play_system_sound("default")

    def _open_settings(self):
        prev_focus = self.FindFocus()
        dlg = SettingsDialog(self, self.settings, user_dir=USER_DIR)
        saved = dlg.ShowModal() == wx.ID_OK
        gtfs_refresh = dlg.gtfs_refreshed
        if saved:
            self.settings = dlg.settings
            self.settings["_log_path"] = os.path.join(USER_DIR, "miab.log")
            self._free_engine.log_settings = self.settings
            save_settings(self.settings)
            self._gemini.init(self.settings.get("gemini_api_key", ""))
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
            self._opensky = OpenSkyClient(
                base_dir=USER_DIR,
                client_id=self.settings.get("opensky_client_id", ""),
                client_secret=self.settings.get("opensky_client_secret", ""))
            # Offer to update home location if requested
            if dlg.set_home_requested:
                self._home_setup_mode = True
                self.update_ui("Type your location to set as home.")
                self.show_jump_dialog()
                return
            _speak("Settings saved.")
        dlg.Destroy()
        if saved and gtfs_refresh:
            self._status_update("Refreshing transit feed catalog...")
            threading.Thread(target=self._refresh_transit_catalog, daemon=True).start()
        def _restore():
            target = prev_focus if (prev_focus and prev_focus.IsShown()) else self.listbox
            target.SetFocus()
        wx.CallLater(1000 if saved else 0, _restore)

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
            self.update_ui(f"{street}.  {lat_str}, {lon_str}.")
        else:
            self.update_ui(f"{lat_str}, {lon_str}.")

    def _announce_lat_lon(self):
        lat_str = f"{abs(self.lat):.5f} {'North' if self.lat >= 0 else 'South'}"
        lon_str = f"{abs(self.lon):.5f} {'East' if self.lon >= 0 else 'West'}"
        self._status_update(f"{lat_str}, {lon_str}.", force=True)

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

    def _street_survey_current_street(self):
        street = getattr(self, "street_label", "") or ""
        invalid = ("No street data nearby", "No street data", "Unknown", "")
        if street not in invalid:
            target = self._street_survey_bare(street)
            has_loaded_street = any(
                self._street_survey_bare(re.sub(r"\s*\(.*?\)", "", seg.get("name", "")).strip()) == target
                for seg in getattr(self, "_road_segments", [])
            )
            if not has_loaded_street:
                street = ""
        if not street or street in invalid:
            street, _ = self._nearest_road(self.lat, self.lon)
            if street not in invalid:
                self.street_label = street
        if street in invalid:
            return ""
        return re.sub(r"\s*\(.*?\)", "", street).strip()

    def _street_survey_project(self, street_name, lat, lon):
        target = self._street_survey_bare(street_name)
        best = None
        for seg in getattr(self, "_road_segments", []):
            raw = re.sub(r"\s*\(.*?\)", "", seg.get("name", "")).strip()
            if self._street_survey_bare(raw) != target:
                continue
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

    def _street_survey_addresses(self, street_name):
        target = self._street_survey_bare(street_name)
        out = []
        seen = set()
        for ap in getattr(self, "_address_points", []):
            if self._street_survey_bare(ap.get("street", "")) != target:
                continue
            if not ap.get("number") or ap.get("lat") is None or ap.get("lon") is None:
                continue
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
            })
        return sorted(out, key=lambda item: self._street_survey_number_key(item["number"]))

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
        target = self._street_survey_bare(street_name)
        points = []
        for ap in getattr(self, "_address_points", []):
            if self._street_survey_bare(ap.get("street", "")) != target:
                continue
            if not ap.get("number") or ap.get("lat") is None or ap.get("lon") is None:
                continue
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
            self.update_ui("No current street.")
            return True
        addresses = self._street_survey_addresses(street)
        if not addresses:
            self.update_ui(f"No known house numbers loaded for {street}.")
            return True
        current_num = self._street_survey_current_number(street)
        if not current_num:
            self.update_ui(f"No current house number found on {street}.")
            return True
        current_key = self._street_survey_number_key(current_num)
        unique = {}
        for addr in addresses:
            unique.setdefault(addr["number"].lower(), addr)
        addresses = sorted(unique.values(), key=lambda item: self._street_survey_number_key(item["number"]))
        if direction > 0:
            choices = [a for a in addresses if self._street_survey_number_key(a["number"]) > current_key]
            target = choices[0] if choices else None
            edge_msg = f"No higher known house number on {street}."
        else:
            choices = [a for a in addresses if self._street_survey_number_key(a["number"]) < current_key]
            target = choices[-1] if choices else None
            edge_msg = f"No lower known house number on {street}."
        if not target:
            self.update_ui(edge_msg)
            return True
        self.lat = target["lat"]
        self.lon = target["lon"]
        self.street_label = street
        self._jump_street_label = street
        self._jump_street_pin_lat = self.lat
        self._jump_street_pin_lon = self.lon
        self._jump_address_number = target["number"]
        self._jump_address_street = street
        wx.CallAfter(self.map_panel.set_position, self.lat, self.lon, True, street)
        self.update_ui(f"{target['number']} {street}.")
        wx.CallAfter(self.listbox.SetFocus)
        return True

    def _street_survey_intersections(self, street_name):
        if not getattr(self, "_walk_graph", None):
            try:
                self._walk_graph = self._build_walk_graph()
                self._nav.set_graph(self._walk_graph)
            except Exception as exc:
                print(f"[StreetSurvey] Walk graph build failed: {exc}")
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
                self._address_points = cache_entry.get("addresses", [])
                self._natural_features = cache_entry.get("natural_features", [])
                self._road_fetch_lat = self.lat
                self._road_fetch_lon = self.lon
                self.street_label = test_label
                self.update_ui(f"Entered {current_suburb}. {test_label}")
                self.sound.play_spatial_tone(
                    self.lat, self.lon, self._spatial_tone_bounds())
                wx.CallAfter(self.map_panel.set_position, self.lat, self.lon, True, test_label)
                return True

        nf = self._check_natural_feature(new_lat, new_lon)
        if nf:
            name = nf.get("name")
            desc = nf.get("description", "edge of loaded area")
            self.update_ui(f"Edge of loaded area. {name if name else desc}")
            return True
        if not _IS_LAND(new_lat, new_lon):
            # Only block if the player is currently on land — if already in
            # water (bad jump, tidal flat, coarse polygon) let them move out.
            if _IS_LAND(self.lat, self.lon):
                label = None
                if hasattr(self, '_geo_features'):
                    try:
                        cc = getattr(self, '_current_country_code', None)
                        label = (self._geo_features.lookup_precise_label(new_lat, new_lon, cc)
                                 or self._geo_features.lookup_any(new_lat, new_lon, cc))
                    except Exception:
                        pass
                self.update_ui(f"{label}." if label else "Can't move into water.")
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
                wx.CallAfter(self.update_ui,
                             f"No further {current_street} intersections found in this loaded area.")
        threading.Thread(target=_geocode_and_confirm, daemon=True).start()
        return True

    def _street_survey_try_boundary_continue(self, street, direction, edge_msg):
        axis = self._street_survey_address_axis(street)
        if not axis or self._road_fetch_lat is None:
            self.update_ui(edge_msg)
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
        self.update_ui(edge_msg)
        return True

    def _street_survey_go_block(self, direction):
        if not self.street_mode:
            return False
        street = self._street_survey_current_street()
        if not street:
            self.update_ui("No current street.")
            return True
        intersections = self._street_survey_intersections(street)
        if not intersections:
            self.update_ui(f"No intersections loaded for {street}.")
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
        self._street_survey_last_direction = direction
        cross = self._walk_get_cross_streets(nid, street) if getattr(self, "_walk_graph", None) else []
        wx.CallAfter(self.map_panel.set_position, self.lat, self.lon, True, street)
        suffix = f"  {shape}" if shape else ""
        if cross:
            cross_text = " and ".join(cross[:2])
            self.update_ui(f"{street} at {cross_text}.{suffix}")
        else:
            self.update_ui(f"Intersection on {street}.{suffix}")
        wx.CallAfter(self.listbox.SetFocus)
        return True

    def _street_survey_turn_cross_street(self, turn_back=False):
        """Ctrl+Shift+Page Down turns onto a cross street; Ctrl+Shift+Page Up turns back."""
        if not self.street_mode:
            return False
        street = self._street_survey_current_street()
        if not street:
            self.update_ui("No current street.")
            return True

        if turn_back:
            prev = getattr(self, "_street_turn_previous", None)
            turn_lat = getattr(self, "_street_turn_lat", None)
            turn_lon = getattr(self, "_street_turn_lon", None)
            if not prev or turn_lat is None or turn_lon is None:
                self.update_ui("No previous street to turn back onto.")
                return True
            if dist_metres(self.lat, self.lon, turn_lat, turn_lon) > 35:
                self.update_ui("Return to the last turn intersection first.")
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
            self.update_ui(f"Turned back onto {prev}.")
            wx.CallAfter(self.listbox.SetFocus)
            return True

        intersections = self._street_survey_intersections(street)
        if not intersections:
            self.update_ui(f"No intersections loaded for {street}.")
            return True

        best = None
        for _along, nid, nlat, nlon in intersections:
            d = dist_metres(self.lat, self.lon, nlat, nlon)
            if best is None or d < best[0]:
                best = (d, nid, nlat, nlon)

        if best is None:
            self.update_ui("No intersection found here.")
            return True

        dist_m, nid, nlat, nlon = best
        if dist_m > 35:
            self.update_ui("Move to an intersection first with Ctrl+Page Up or Ctrl+Page Down.")
            return True

        cross = self._walk_get_cross_streets(nid, street)
        cross = [s for s in cross if self._street_survey_bare(s) != self._street_survey_bare(street)]
        if not cross:
            self.update_ui(f"No other street to turn onto from {street}.")
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
        self.update_ui(f"Turned onto {target}.")
        wx.CallAfter(self.listbox.SetFocus)
        return True

    def _street_survey_summary(self):
        street = self._street_survey_current_street()
        if not street:
            self.update_ui("No current street.")
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
        self.update_ui(".  ".join(parts) + ".")


    def on_key(self, event):
        key   = event.GetKeyCode()
        shift = event.ShiftDown()
        primary = _primary_down(event)
        alt = event.AltDown()
        # True when no modifier is held — used to prevent bare letter/F-key
        # handlers from firing on modifier shortcuts.
        no_mod = not shift and not primary and not alt
        _log_key_event(self, event, "frame", f"street_mode={self.street_mode} walking={getattr(self, '_walking_mode', False)} nav={getattr(self, '_nav_active', False)}")


        dist, _ = _nearest_city(self._city_lats, self._city_lons, self.lat, self.lon)
        on_land = _IS_LAND(self.lat, self.lon) or dist < 0.3

        # Primary+Alt+F — find food along active transit line.
        # Must come BEFORE the primary+F favourites check below, which otherwise
        # matches first because its condition does not exclude alt being held.
        if primary and alt and not shift and (key == ord('F') or key == ord('f')):
            self._tool_find_food()
            return

        # Favourites — works in any mode when a coordinate/POI is available.
        if primary and not alt and key in (ord('F'), ord('f')):
            if shift:
                self._add_current_favourite()
            else:
                self._show_favourites()
            return

        # Escape exits walking mode.
        if key == wx.WXK_ESCAPE and getattr(self, '_walking_mode', False):
            self._nav_active = False
            self._walk_toggle()
            return

        if no_mod and (key == ord('F') or key == ord('f')):
            if self.street_mode and not bool(self._poi_list):
                self._toggle_free_mode()
            return
        if getattr(self, '_free_mode', False):
            if key == wx.WXK_UP:
                self._free_step(1); return
            if key == wx.WXK_DOWN:
                self._free_step(-1); return
            if primary and key == wx.WXK_LEFT:
                self._free_snap_cross(); return
            if primary and key == wx.WXK_RIGHT:
                self._free_snap_cross(); return
            if key == wx.WXK_LEFT:
                text, pois = self._free_engine.describe_left_with_pois()
                self._free_last_side_pois = pois
                self._free_last_side      = "left"
                self.update_ui(text if text else "Nothing on the left."); return
            if key == wx.WXK_RIGHT:
                text, pois = self._free_engine.describe_right_with_pois()
                self._free_last_side_pois = pois
                self._free_last_side      = "right"
                self.update_ui(text if text else "Nothing on the right."); return
            if no_mod and (key == ord('A') or key == ord('a')):
                self._announce_address(); return
            if no_mod and (key == ord('H') or key == ord('h')):
                self._free_heading(); return
            if no_mod and (key == ord('X') or key == ord('x')):
                self._free_describe_intersection(); return
            if no_mod and (key == ord('R') or key == ord('r')):
                self._free_turnaround(); return
            if key in (wx.WXK_DELETE, wx.WXK_F2):
                self._free_poi_action(key); return
            # Let system key combos (Alt+F4, etc.) fall through
            if alt or key in (wx.WXK_F1, wx.WXK_F7, wx.WXK_F11,
                                           wx.WXK_F2, wx.WXK_F3, wx.WXK_F4,
                                           wx.WXK_F5, wx.WXK_F6):                pass  # fall through to normal handlers below
            else:
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

        if no_mod and key == wx.WXK_F1:    self.show_help();              return
        if shift and not primary and key == wx.WXK_F2:
            self._announce_climate_zone(); return
        if no_mod and key == wx.WXK_F2:    self._announce_current_place_or_street();   return
        if shift and not primary and key == wx.WXK_F3:
            self._status_update(self.sound.volume_down(), force=True); return
        if shift and not primary and key == wx.WXK_F4:
            self._status_update(self.sound.volume_up(), force=True); return
        if no_mod and key == wx.WXK_F3:
            self._status_update(f"{abs(self.lat):.4f} {'North' if self.lat >= 0 else 'South'}", force=True); return
        if primary and key == ord(','):
            self._open_settings();  return
        if no_mod and key == wx.WXK_F4:
            self._status_update(f"{abs(self.lon):.4f} {'East' if self.lon >= 0 else 'West'}", force=True); return
        if no_mod and key == wx.WXK_F5:
            miab_log("feature_usage", "Key: F5 (continent)", self.settings)
            self.announce_continent();    return
        if no_mod and key == wx.WXK_F6:
            miab_log("feature_usage", "Key: F6 (facts)", self.settings)
            self.announce_facts();        return
        if shift and not primary and key == wx.WXK_F6:
            miab_log("feature_usage", f"Key: Shift+F6 (Wikipedia) at {self.last_country_found}", self.settings)
            self.announce_wikipedia_summary(); return
        if no_mod and key == wx.WXK_F7:    self.toggle_sounds();    return
        if no_mod and key == wx.WXK_F8:    self._flash_current_country(); return
        if no_mod and key == wx.WXK_F9:    self._toggle_map_fullscreen(); return
        if shift and not primary and key == wx.WXK_F10:
            self._game.repeat_target()
            return
        if primary and key == wx.WXK_F10:
            if self._session and self._session.active:
                self._session.stop()
                self._session = None
                self._game._timeout_cb = None
                self._status_update("Challenge session ended.", force=True)
                wx.CallAfter(self._resume_location_sound)
            else:
                self._start_challenge_session()
            return
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
                    self.sound.stop()
                    self._game.start(self.df, self.lat, self.lon)
                else:
                    self._status_update("No city data available for the challenge.", force=True)
            return
        if key == wx.WXK_F11:
            if shift and not primary:
                if not self.street_mode:
                    self._prefetch_streets()
                else:
                    self._status_update("Shift+F11: pre-download works from world map only.", force=True)
            elif no_mod:
                if getattr(self, '_prefetch_in_progress', False):
                    self._status_update("Street download in progress. Please wait.")
                else:
                    self.toggle_street_mode()
                    self._update_main_menu_state()
                    miab_log("navigation",
                             f"Street mode {'entered' if self.street_mode else 'exited'}.",
                             self.settings)
            return
        if no_mod and key == wx.WXK_F12:
            self._open_tools_menu(); return

        if (not self.street_mode and not getattr(self, "_walking_mode", False)
                and not getattr(self, "_free_mode", False)
                and not getattr(self, "_game", None).active):
            if no_mod and (key == ord('R') or key == ord('r')):
                self._announce_current_region(); return
            if no_mod and (key == ord('C') or key == ord('c')):
                self._announce_current_country(); return

        # ── GPS navigation intercept — Up/Down step through instructions ──
        # Fires regardless of walking mode, street mode or world map mode.
        if getattr(self, '_nav_active', False):
            if key == wx.WXK_UP:
                self._nav_step_forward(); return
            if key == wx.WXK_DOWN:
                self._nav_step_back(); return
            if no_mod and (key == ord('I') or key == ord('i')):
                self._nav_announce_step(); return
            if no_mod and (key == ord('X') or key == ord('x')):
                self._nav_announce_cross_street(); return

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
            return
        if no_mod and key in (page_up, page_down) and not self.street_mode and not getattr(self, "_walking_mode", False):
            self._cycle_spatial_tones_mode(1 if key == page_down else -1)
            return

        # X key — intersection in street/walk mode.
        if no_mod and (key == ord('X') or key == ord('x')):
            if self.street_mode or getattr(self, '_walking_mode', False):
                miab_log("feature_usage", "Key: X (nearest intersection)", self.settings)
                self._announce_nearest_intersection()
            return

        # N key — nearby features.
        if no_mod and (key == ord('N') or key == ord('n')):
            miab_log("feature_usage", "Key: N (nearby features)", self.settings)
            self._announce_nearby_features(); return

        if no_mod and (key == ord('P') or key == ord('p')):
            miab_log("feature_usage", "Key: p (nearby menu)", self.settings)
            self._show_poi_category_dialog(); return

        # ── Satellite / Street View — available in all modes ─────────
        if primary and shift and not alt and (key == ord('S') or key == ord('s')):
            # Ctrl+Shift+S: satellite view everywhere
            miab_log("feature_usage", "Key: Ctrl+Shift+S (satellite view)", self.settings)
            lat, lon = self._poi_lat_lon_if_focused()
            self._satellite_view_at_location(lat, lon); return
        if primary and shift and alt and (key == ord('S') or key == ord('s')):
            # Ctrl+Shift+Alt+S: street view in street/walking mode, or from a
            # focused POI in any mode (POI has real coords worth fetching).
            # Falls back to satellite only when on the bare world map.
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
                self._status_update(
                    "Street View works in street mode or from a POI list. "
                    "Showing satellite instead.", force=True)
                self._satellite_view_at_location(lat, lon)
            return

        # ── World map only keys ───────────────────────────────────────
        if not self.street_mode and not getattr(self, '_walking_mode', False):
            if no_mod and (key == ord('T') or key == ord('t')):
                miab_log("feature_usage", "Key: T (local time)", self.settings)
                self.announce_time();  return
            if shift and not primary and (key == ord('T') or key == ord('t')):
                miab_log("feature_usage", "Key: Shift+T (timezone)", self.settings)
                self._announce_timezone(); return
            if no_mod and (key == ord('S') or key == ord('s')):
                miab_log("feature_usage", "Key: S (sunrise/sunset)", self.settings)
                self._announce_sunrise_sunset(); return
            if shift and (key == ord('4') or key == ord('$')):
                miab_log("feature_usage", "Key: $ (currency)", self.settings)
                self._announce_currency(); return
            if primary and alt and key == wx.WXK_UP:
                self._jump_nearest_land("north"); return
            if primary and alt and key == wx.WXK_DOWN:
                self._jump_nearest_land("south"); return
            if primary and alt and key == wx.WXK_LEFT:
                self._jump_nearest_land("west"); return
            if primary and alt and key == wx.WXK_RIGHT:
                self._jump_nearest_land("east"); return
            if no_mod and (key == ord('A') or key == ord('a')):
                miab_log("feature_usage", "Key: A (nearest airport)", self.settings)
                self._announce_nearest_airport(); return
            if shift and not primary and (key == ord('A') or key == ord('a')):
                miab_log("feature_usage", "Key: Shift+A (overhead flights)", self.settings)
                self._announce_overhead_flights(); return
            if shift and primary and (key == ord('A') or key == ord('a')):
                miab_log("feature_usage", "Key: Ctrl+Shift+A (airport flights)", self.settings)
                self._announce_airport_flights(); return
            if shift and not primary and key == wx.WXK_F1:
                miab_log("feature_usage", "Key: Shift+F1 (capital city)", self.settings)
                self._announce_capital(); return
            if no_mod and (key == ord('W') or key == ord('w')):
                miab_log("feature_usage", "Key: W (weather)", self.settings)
                self._announce_weather(); return
            if no_mod and (key == ord('Q') or key == ord('q')):
                miab_log("feature_usage", "Key: Q (air quality)", self.settings)
                self._announce_air_quality(); return
        if no_mod and (key == ord('L') or key == ord('l')):
            miab_log("feature_usage", "Key: L (latitude/longitude)", self.settings)
            self._announce_lat_lon(); return
        if shift and not primary and (key == ord('L') or key == ord('l')):
            miab_log("feature_usage", "Key: Shift+L (languages)", self.settings)
            self._announce_languages(); return
        if no_mod and key == wx.WXK_SPACE:
            if self._session and self._session.active:
                if self._session.on_space(self.df, self.lat, self.lon):
                    return
        if no_mod and (key == ord('J') or key == ord('j')):
            if self._game.active:
                self._status_update("Jump is disabled during the challenge. Use your ears!", force=True)
                return
            if self.street_mode:
                dlg = wx.MessageDialog(self,
                    "Exit street view and jump to a new location?",
                    "Exit Street View", wx.YES_NO | wx.NO_DEFAULT)
                if dlg.ShowModal() != wx.ID_YES:
                    dlg.Destroy()
                    self.listbox.SetFocus()
                    return
                dlg.Destroy()
                self._exit_street_mode()
            self.show_jump_dialog();  return

        if alt and not primary and not shift and key in (ord('1'), ord('2'), ord('3')):
            self._report_mark_to_destination(int(chr(key)))
            return
        if primary and not shift and not alt and (key == ord('M') or key == ord('m')):
            self._prompt_mark_slot(remove=False)
            return
        if primary and shift and not alt and (key == ord('M') or key == ord('m')):
            self._prompt_mark_slot(remove=True)
            return

        if primary and not shift and not alt and (key == ord('D') or key == ord('d')):
            self._set_map_destination()
            return

        if primary and alt and not shift and (key == ord('D') or key == ord('d')):
            self._prompt_destination_mark_slot()
            return

        if primary and alt and not shift:
            alt_map = {ord('1'): 1, ord('2'): 2, ord('3'): 3,
                       ord('4'): 4, ord('5'): 5, ord('6'): 6}
            if key in alt_map:
                self._poi_detail(alt_map[key]); return

        if not self.street_mode:
            if shift and not primary and not alt and (key == ord('P') or key == ord('p')):
                self._announce_postcode();  return


        if self.street_mode:
            if primary and (key == ord('W') or key == ord('w')):
                self._open_poi_website(); return
            if no_mod and (key == ord('W') or key == ord('w')):
                self._walk_toggle();  return
            if no_mod and (key == ord('P') or key == ord('p')):
                self._announce_poi_count();  return
            if no_mod and (key == ord('A') or key == ord('a')):
                self._announce_address();    return
            if no_mod and (key == ord('S') or key == ord('s')):
                self._street_search()
                return
            if primary and (key == ord('G') or key == ord('g')):
                self._nav_to_address()
                return
            if no_mod and (key == ord('I') or key == ord('i')):
                self._announce_position_info()
                return
            if no_mod and (key == ord('H') or key == ord('h')):
                if getattr(self, '_walking_mode', False):
                    heading = self._walk_compass_name(getattr(self, '_walk_heading', 0))
                    self.update_ui(f"Heading {heading}.")
                return
            if no_mod and (key == ord('R') or key == ord('r')):
                if getattr(self, '_walking_mode', False):
                    self._walk_turnaround()
                elif self._game.active:
                    self._game.repeat_target()
                return
            if key == wx.WXK_RETURN or key == wx.WXK_NUMPAD_ENTER:
                if primary:
                    if self._street_confirm_explore(): return
                else:
                    if self._street_confirm_jump(): return
            if key == wx.WXK_SPACE:
                if getattr(self, '_pending_snap_lat', None) is not None:
                    self.lat = self._pending_snap_lat
                    self.lon = self._pending_snap_lon
                    self._pending_snap_lat = None
                    self._pending_snap_lon = None
                    wx.CallAfter(self.map_panel.set_position, self.lat, self.lon, True, "")
                    wx.CallAfter(self._update_street_display)
                    return
                if getattr(self, '_pending_street_download', False):
                    self._download_new_area()
                    return
                self._announce_poi_crossing();  return

            # Walking mode intercepts arrow keys
            if getattr(self, '_walking_mode', False):
                if key == wx.WXK_UP:
                    if getattr(self, '_walk_browsing', False):
                        self._walk_browsing = False
                        if self._walk_commit_turn(announce=False):
                            self._walk_forward()
                            return
                    self._walk_forward();  return
                if key == wx.WXK_DOWN:
                    if getattr(self, '_walk_browsing', False):
                        self._walk_browsing = False
                        self._walk_turn_options = []
                        self._walk_option_idx = None
                    self._walk_backward();  return
                if key == wx.WXK_LEFT:
                    self._walk_turn_left();  return
                if key == wx.WXK_RIGHT:
                    self._walk_turn_right();  return

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

        map_arrow = key in (wx.WXK_UP, wx.WXK_DOWN, wx.WXK_LEFT, wx.WXK_RIGHT)
        if (not self.street_mode and not getattr(self, '_walking_mode', False)
                and map_arrow and primary and not shift and not alt):
            direction = {
                wx.WXK_UP: "north",
                wx.WXK_DOWN: "south",
                wx.WXK_LEFT: "west",
                wx.WXK_RIGHT: "east",
            }[key]
            target = self._next_region_map_target(direction)
            if target:
                new_lat, new_lon, target_label = target
                self._status_update(target_label, force=True)
                self._pinned_jump_label = ""
                self._pinned_jump_label_until = 0
            else:
                self._last_region_jump = None
                self._status_update("No region in that direction.", force=True)
                return
        elif key == wx.WXK_UP:
            self._last_region_jump = None
            new_lat = min(90, self.lat + step)
        elif key == wx.WXK_DOWN:
            self._last_region_jump = None
            new_lat = max(-90, self.lat - step)
        elif key == wx.WXK_LEFT:
            self._last_region_jump = None
            new_lon = ((self.lon - step + 180) % 360) - 180
        elif key == wx.WXK_RIGHT:
            self._last_region_jump = None
            new_lon = ((self.lon + step + 180) % 360) - 180

        if new_lat != self.lat or new_lon != self.lon:
            # In street mode, check if new location has streets before moving
            if self.street_mode and self._road_segments:
                # Check if streets exist at new location
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
                        label = None
                        if hasattr(self, '_geo_features'):
                            try:
                                cc = getattr(self, '_current_country_code', None)
                                label = (self._geo_features.lookup_precise_label(new_lat, new_lon, cc)
                                         or self._geo_features.lookup_any(new_lat, new_lon, cc))
                            except Exception:
                                pass
                        self._status_update(f"{label}." if label else "Can't move into water.", force=True)
                        return
            
            # Hard barrier in street mode - block ALL arrow movement beyond loaded area
            if self.street_mode and self._road_fetch_lat is not None:
                if self._street_boundary_move(new_lat, new_lon):
                    return
            
            if key == wx.WXK_LEFT and new_lon > self.lon:
                self._status_update("Wrapping around. Now on east side of map.", force=True)
            elif key == wx.WXK_RIGHT and new_lon < self.lon:
                self._status_update("Wrapping around. Now on west side of map.", force=True)
            self.lat = new_lat
            self.lon = new_lon
            moved = True
        if moved:
            # Keep the visual map and coordinate panel responsive while the
            # slower place/country lookup runs in the background.
            self.map_panel.set_position(
                self.lat, self.lon, self.street_mode, self.street_label)
            self._refresh_info_panel()

            # Spatial tone only for world map, not street/walking mode
            if not self._game.active and not self.street_mode and not getattr(self, '_walking_mode', False):
                self.sound.play_spatial_tone(
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
                self._last_fetch_lat = self.lat
                self._last_fetch_lon = self.lon
                self._distance_since_fetch = 0.0
                
                if self.street_mode:
                    self._last_move_was_shift = shift
                    threading.Thread(target=self._query_street, daemon=True).start()
                else:
                    threading.Thread(target=self._lookup, daemon=True).start()
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
        km_text = f"{km:.1f}" if km < 10 else str(round(km))
        return f"{km_text} km {direction} of centre"

    def _lookup(self):
        try:
            # ── Latitude-line and Date Line crossing announcements ─────────
            prev_lat = self._prev_lat
            prev_lon = self._prev_lon
            cur_lat  = self.lat
            cur_lon  = self.lon

            if not getattr(self, 'street_mode', False) and \
               not getattr(self, '_walking_mode', False) and \
               not getattr(self, '_nav_active', False):

                # Latitude lines
                if (self.settings.get("announce_climate_zones", True)
                        and prev_lat is not None and prev_lat != cur_lat):
                    _LINES = (0, 23.5, 66.5, -23.5, -66.5)
                    for lat_line in _LINES:
                        if (prev_lat < lat_line <= cur_lat) or (cur_lat <= lat_line < prev_lat):
                            if lat_line == 0:
                                msg = "Northern Hemisphere" if cur_lat >= 0 else "Southern Hemisphere"
                            elif abs(lat_line) == 23.5:
                                msg = "Tropical zone" if abs(cur_lat) <= 23.5 else "Temperate zone"
                            else:
                                msg = "Polar zone"
                            self._suppress_next_location = True
                            wx.CallAfter(self._status_update, msg, True)
                            miab_log("navigation", f"Crossed latitude line {lat_line}°: {msg}", self.settings)
                            break

                # International Date Line — large longitude jump signals crossing
                if prev_lon is not None and abs(cur_lon - prev_lon) > 300:
                    wx.CallAfter(self._sr_announce, "Crossed the International Date Line.")
                    miab_log("navigation", "Crossed the International Date Line.", self.settings)

            self._prev_lat = cur_lat
            self._prev_lon = cur_lon

            dist, idx = _nearest_city(self._city_lats, self._city_lons, self.lat, self.lon)
            country = "Open Water"

            # Antarctica has no cities — detect purely by latitude
            if self.lat < -60.0:
                country = "Antarctica"
                if country != self.last_country_found:
                    self.last_city_found    = ""
                    self.last_state_found   = ""
                    self.last_country_found = country
                    self.current_continent  = "Antarctica"
                    self.last_location_str  = "Antarctica"
                    wx.CallAfter(self._refresh_info_panel)
                    if getattr(self, '_suppress_next_location', False):
                        self._suppress_next_location = False
                    else:
                        wx.CallAfter(self.update_ui, "Antarctica")
                    if getattr(self, 'sounds_enabled', True) and not self._game.active:
                        self.sound.play_location_sound("Antarctica", "Antarctica")
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
                            wx.CallAfter(lambda c=country: wx.CallLater(2000, lambda: self.sound.play_location_sound(c)))
                        else:
                            miab_log("challenges",
                                     f"Solo win: country={country} time={elapsed:.1f}s "
                                     f"score={max(0, 180 - int(elapsed))}",
                                     self.settings)
                            wx.CallAfter(self._game.on_win)
                            wx.CallAfter(lambda c=country: wx.CallLater(2000, lambda: self.sound.play_location_sound(c)))
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
                close_place = city_matches_country and dist_km <= PLACE_NAME_CLOSE_KM
                prev_state   = getattr(self, 'last_state_found', '')
                prev_country = self.last_country_found
                self.last_city_found = (
                    city if close_place and city and city != 'nan' else ""
                )
                self.last_state_found = (
                    state if city_matches_country and state and state != 'nan' else ""
                )

                if close_place:
                    parts = []
                    if city and city.lower() != 'nan':
                        parts.append(city)
                    if state and state.lower() != 'nan' and state != prev_state:
                        parts.append(state)
                    if country and country.lower() != 'nan' and country != prev_country:
                        parts.append(country)
                    label = ", ".join(parts) if parts else city
                elif city_matches_country:
                    country_code = getattr(self, "_current_country_code", None)
                    context = self._geo_features.context_items(
                        self.lat, self.lon, limit=1, country_code=country_code)
                    feature = self._geo_features.lookup_precise_label(
                        self.lat, self.lon, country_code=country_code)
                    feature_any = ""
                    if not feature:
                        feature_any = self._geo_features.lookup_any(
                            self.lat, self.lon, country_code=country_code)
                    if feature:
                        label = feature
                    elif context:
                        label = ". ".join(context)
                    elif feature_any:
                        label = feature_any
                    elif city and city.lower() != "nan" and dist_km <= NEAREST_PLACE_FALLBACK_KM:
                        label = f"{city} {round(dist_km)} km"
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
                    context = self._geo_features.context_items(
                        self.lat, self.lon, limit=1, country_code=country_code)
                    feature = self._geo_features.lookup_precise_label(
                        self.lat, self.lon, country_code=country_code)
                    feature_any = ""
                    if not feature:
                        feature_any = self._geo_features.lookup_any(
                            self.lat, self.lon, country_code=country_code)
                    if feature:
                        label = feature
                    elif context:
                        label = ". ".join(context)
                    elif feature_any:
                        label = feature_any
                    else:
                        label = country if country and country.lower() != "nan" else "Location unknown"
                        _only_region = True
            else:
                # Named bays, islands and coastal features are often part of the
                # user's local country context, so keep the nearby country sound.
                country_code = getattr(self, "_current_country_code", None)
                context = self._geo_features.context_items(
                    self.lat, self.lon, limit=1, country_code=country_code)
                coastal_feature = (
                    (context[0] if context else "")
                    or self._geo_features.lookup_precise_label(
                        self.lat, self.lon, country_code=country_code)
                    or self._geo_features.lookup_any(
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
            previous_display = getattr(self, "last_location_str", "")
            self._last_location_base = display_base
            self.last_location_str = display
            wx.CallAfter(self._refresh_info_panel)
            if getattr(self, '_suppress_next_location', False):
                self._suppress_next_location = False
            elif (display == getattr(self, '_last_jump_display_label', None)
                  and time.time() < getattr(self, '_last_jump_display_until', 0)):
                self._last_jump_display_label = None
                self._last_jump_display_until = 0
            elif _only_region and display == getattr(self, '_last_region_only_display', ''):
                pass  # region/country unchanged, skip repeat announcement
            else:
                if _only_region:
                    self._last_region_only_display = display
                else:
                    self._last_region_only_display = ""  # geo/city found; reset so re-entry to bare region speaks
                wx.CallAfter(self.update_ui, display)

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
                        wx.CallAfter(lambda c=country: wx.CallLater(2000, lambda: self.sound.play_location_sound(c)))
                    else:
                        miab_log("challenges",
                                 f"Solo win: country={country} time={elapsed:.1f}s "
                                 f"score={max(0, 180 - int(elapsed))}",
                                 self.settings)
                        wx.CallAfter(self._game.on_win)
                        wx.CallAfter(lambda c=country: wx.CallLater(2000, lambda: self.sound.play_location_sound(c)))
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
                        canonical_for_sound = COUNTRY_ALIASES.get(country, country)
                        self.sound.play_location_sound(country if country != "Open Water" else "ocean", continent)
                    miab_log("navigation",
                             f"Entered country: {country}"
                             + (f" (continent: {continent})" if continent else ""),
                             self.settings)
                    cached = getattr(self, '_rest_countries_cache', {}).get(country)
                    if cached:
                        self._current_subregion = cached.get('subregion', '')

            wx.CallAfter(self.map_panel.set_position, self.lat, self.lon,
                         self.street_mode, self.street_label)
        finally:
            # Always clear fetch flag, even on error or early return
            self._fetch_in_progress = False

    def _ocean_name(self, lat, lon):
        """Return the name of the ocean corresponding to a lat/lon point."""
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
            self._status_update("No city data available for the challenge.", force=True)
            return
        if self._game.active or (self._session and self._session.active):
            self._status_update("A challenge is already active. Press F10 to stop it first.", force=True)
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
                 lambda e: dlg.EndModal(wx.ID_CANCEL)
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
            announce_cb   = self._accessible_status,
            players       = players,
            rounds        = rounds,
            on_complete   = lambda: wx.CallAfter(self._on_session_complete),
            wait_cb       = self._accessible_status,
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
            print(f"[GTFS] Saved operator map: '{operator_key}' → feed {feed_id}")
        except Exception as exc:
            print(f"[GTFS] Failed to save operator map: {exc}")

    def _resume_location_sound(self):
        """Re-start the country/region ambient sound and refresh the UI label."""
        country = getattr(self, 'last_country_found', '')
        continent = getattr(self, 'current_continent', '')
        if country and country != "Open Water":
            self.sound.play_location_sound(country, continent)
        # Restore the location label in the listbox
        label = getattr(self, 'last_location_str', '')
        if label:
            self.update_ui(label)

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
                "N: nearby features.",
                "A: address lookup.",
                "R: reverse direction.",
                "F: leave free mode.",
                "Ctrl+F: favourites.",
                "Ctrl+Shift+F: add selected POI or current place to favourites.",
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
                "N: nearby features.",
                "A: address lookup.",
                "P: POI search.",
                "Ctrl+F: favourites.",
                "Ctrl+Shift+F: add current place to favourites.",
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
                "Ctrl+Page Up: previous intersection.",
                "Ctrl+Page Down: next intersection.",
                "Ctrl+Shift+Page Down: turn onto the cross street.",
                "Ctrl+Shift+Page Up: turn back onto the abandoned street.",
                "S: street search.",
                "A: address lookup.",
                "P: POI search.",
                "X: nearest cross street.",
                "N: nearby features.",
                "I: street summary.",
                "W: walking mode.",
                "F: free mode.",
                "Ctrl+G: navigate to address.",
                "Ctrl+F: favourites.",
                "Ctrl+Shift+F: add selected POI or current place to favourites.",
                "Enter: jump to selected POI.",
                "Ctrl+Enter: transit info or explore selected POI.",
                "Space: nearest intersection for selected POI.",
                "Ctrl+Alt+1: selected POI address.",
                "Ctrl+Alt+2: selected POI hours.",
                "Ctrl+Alt+3: selected POI phone.",
                "Ctrl+Alt+4: selected POI website.",
                "Ctrl+Alt+5: ask Gemini about selected POI.",
                "Ctrl+Alt+6: find menu (Gemini/web search).",
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
            lines = [
                "Arrow keys: move around the map.",
                "Shift+arrows: fine movement.",
                "Ctrl+arrows: jump to the next administrative region in that direction.",
                "Ctrl+Alt+arrows: jump to the nearest foreign country in that direction.",
                "F2: repeat location.",
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
                "F9: toggle full-screen map.",
                "F10: country discovery challenge.",
                "Ctrl+F10: scored challenge session.",
                "Shift+F10: repeat challenge target.",
                "F11: street mode.",
                "Shift+F11: pre-download streets.",
                "Page Up/Page Down: cycle spatial tones between world, country, and region.",
                "F12: tools menu.",
                "Ctrl+F: favourites.",
                "Ctrl+Shift+F: add current place to favourites.",
                "J: jump to city, country, or coordinates.",
                "M: store current location as mark 1, 2, or 3.",
                "Shift+M: remove mark 1, 2, or 3.",
                "D: set destination.",
                "Alt+D: set destination from mark 1, 2, or 3.",
                "Alt+1, Alt+2, Alt+3: distance and direction from mark to destination.",
                "N: nearby geographic features.",
                "P: POI search.",
                "POI menu: selected POI address, hours, phone, website, Gemini, menu, and website launch.",
                "Shift+F: find food from a saved mark to the destination.",
                "T: local time.",
                "Shift+T: timezone.",
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
        if IS_MAC:
            lines = [
                line.replace("Ctrl+", "Command+").replace("Alt+", "Option+")
                for line in lines
            ]
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
                 lambda e: dlg.EndModal(wx.ID_CLOSE)
                 if e.GetKeyCode() == wx.WXK_ESCAPE else e.Skip())
        dlg.ShowModal()
        dlg.Destroy()

    def _show_about(self):
        """About dialog with the open-source / optional key notice."""
        dlg = wx.Dialog(self, title=f"About {APP_NAME}",
                        style=wx.DEFAULT_DIALOG_STYLE | wx.STAY_ON_TOP)
        vs = wx.BoxSizer(wx.VERTICAL)

        header = wx.StaticText(dlg, label=f"{APP_NAME}\nVersion {APP_VERSION}\nCopyright © 2026 Sam Taylor. All rights reserved.")
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
            self._status_update("Street View module not available.", force=True)
            return

        google_key = self.settings.get("google_api_key", "").strip()
        if not google_key:
            self._status_update(
                "Street View is using an open fallback instead of Google.",
                force=True)
            self._open_mapillary_view(lat, lon)
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
                    gemini_client=self._gemini,
                    street_heading=heading,
                    cache_path=os.path.join(CACHE_DIR, "streetview_cache.json"),
                )

                if not result:
                    wx.CallAfter(
                        self._status_update,
                        "No Street View coverage here. Showing satellite instead.",
                        force=True)
                    wx.CallAfter(self._satellite_view_at_location, lat, lon)
                    return

                image_bytes_list, description = result
                wx.CallAfter(
                    self._show_streetview_dialog,
                    image_bytes_list, description, lat, lon)

            except Exception as e:
                miab_log("error", f"Street View lookup failed: {e}", self.settings)
                wx.CallAfter(
                    self._status_update, f"Error: {str(e)[:50]}", force=True)

        threading.Thread(target=fetch_and_display, daemon=True).start()

    def _open_mapillary_view(self, lat: float, lon: float) -> None:
        """Open an open street-level viewer as a fallback."""
        try:
            import webbrowser
            url = f"https://www.mapillary.com/app/?lat={lat:.6f}&lng={lon:.6f}&z=17"
            webbrowser.open(url)
        except Exception as exc:
            self._status_update(
                f"Could not open open street-level viewer: {exc}",
                force=True,
            )

    def _show_streetview_dialog(
        self,
        image_bytes_list: list,
        description: str,
        lat: float,
        lon: float,
    ):
        """Display one or two Street View images and a description in a dialog."""
        dlg = wx.Dialog(
            self,
            title=f"Street View ({lat:.4f}, {lon:.4f})",
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
            size=(920, 700),
        )
        vs = wx.BoxSizer(wx.VERTICAL)

        # Images side by side (or centred if only one)
        img_sizer = wx.BoxSizer(wx.HORIZONTAL)
        for img_bytes in image_bytes_list:
            try:
                from PIL import Image as PilImage
                pil = PilImage.open(io.BytesIO(img_bytes))
                max_w = 420 if len(image_bytes_list) > 1 else 640
                pil.thumbnail((max_w, 480), PilImage.Resampling.LANCZOS)
                wx_img = wx.Image(pil.width, pil.height)
                wx_img.SetData(pil.convert("RGB").tobytes())
                bmp = wx.StaticBitmap(dlg, bitmap=wx.Bitmap(wx_img))
                img_sizer.Add(bmp, 0, wx.ALL, 6)
            except Exception as e:
                print(f"[UI] Street View image display failed: {e}")
        vs.Add(img_sizer, 0, wx.ALL | wx.CENTER, 4)

        txt = wx.TextCtrl(
            dlg, value=description,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_WORDWRAP)
        txt.SetMinSize((880, 130))
        vs.Add(txt, 1, wx.ALL | wx.EXPAND, 10)

        btn = wx.Button(dlg, wx.ID_CLOSE, "Close")
        dlg.Bind(wx.EVT_BUTTON, lambda e: dlg.EndModal(wx.ID_CLOSE))
        vs.Add(btn, 0, wx.ALL | wx.CENTER, 10)

        dlg.SetSizer(vs)
        dlg.ShowModal()
        dlg.Destroy()

    def _satellite_view_at_location(self, lat: float, lon: float):
        """Fetch and display satellite image + description at location."""
        google_key = self.settings.get("google_api_key", "").strip()
        if not google_key:
            self._status_update(
                "Satellite view uses Google imagery and needs a Google API key.",
                force=True)
            return
        self._status_update("Fetching satellite image...", force=True)

        def fetch_and_display():
            try:
                if not lookup_satellite_description:
                    wx.CallAfter(self._status_update, "Satellite module not available.", force=True)
                    return
                result = lookup_satellite_description(
                    lat, lon, zoom=15,
                    google_api_key=self.settings.get("google_api_key", ""),
                    gemini_client=self._gemini,
                    cache_path=os.path.join(CACHE_DIR, "satellite_cache.json")
                )

                if not result:
                    wx.CallAfter(self._status_update, "Satellite image unavailable at this location.", force=True)
                    return

                image_bytes, description = result
                wx.CallAfter(self._show_satellite_dialog, image_bytes, description, lat, lon)

            except Exception as e:
                miab_log("error", f"Satellite lookup failed: {e}", self.settings)
                wx.CallAfter(self._status_update, f"Error: {str(e)[:50]}", force=True)

        threading.Thread(target=fetch_and_display, daemon=True).start()

    def _show_satellite_dialog(self, image_bytes: bytes, description: str, lat: float, lon: float):
        """Display satellite image and description in a dialog."""
        dlg = wx.Dialog(self, title=f"Satellite View ({lat:.4f}, {lon:.4f})",
                        style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
                        size=(900, 700))

        vs = wx.BoxSizer(wx.VERTICAL)

        try:
            pil_image = Image.open(io.BytesIO(image_bytes))
            pil_image.thumbnail((600, 600), Image.Resampling.LANCZOS)
            wx_image = wx.Image(pil_image.width, pil_image.height)
            wx_image.SetData(pil_image.convert("RGB").tobytes())
            bitmap = wx.Bitmap(wx_image)
            img_ctrl = wx.StaticBitmap(dlg, bitmap=bitmap)
            vs.Add(img_ctrl, 0, wx.ALL | wx.CENTER, 10)
        except Exception as e:
            print(f"[UI] Image display failed: {e}")

        txt = wx.TextCtrl(dlg, value=description,
                          style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_WORDWRAP)
        txt.SetMinSize((850, 150))
        vs.Add(txt, 1, wx.ALL | wx.EXPAND, 10)

        btn = wx.Button(dlg, wx.ID_CLOSE, "Close")
        dlg.Bind(wx.EVT_BUTTON, lambda e: dlg.EndModal(wx.ID_CLOSE))
        vs.Add(btn, 0, wx.ALL | wx.CENTER, 10)

        dlg.SetSizer(vs)
        dlg.ShowModal()
        dlg.Destroy()

        self._status_update(f"Satellite view: {description}", force=True)
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
            print(f"[PlaceCache] Save failed: {exc}")

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
            print(f"[Jump] Online search failed for {query!r}: {exc}")
            return []

        candidates = []
        seen = set()
        for item in data if isinstance(data, list) else []:
            try:
                lat = float(item["lat"])
                lon = float(item["lon"])
            except Exception:
                continue
            label = str(item.get("display_name", "")).strip()
            if not label:
                continue
            key = label.lower()
            if key in seen:
                continue
            seen.add(key)
            candidates.append((label, lat, lon, 2, "online"))
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

    def show_jump_dialog(self, initial_value=""):
        dlg = wx.TextEntryDialog(self, "Search City or Country (or paste lat,lon):", "Jump")
        if initial_value:
            dlg.SetValue(initial_value)
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            self.listbox.SetFocus()
            return
        q = dlg.GetValue().strip()
        dlg.Destroy()

        if not q:
            self.listbox.SetFocus()
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
                    if getattr(self, '_home_setup_mode', False):
                        self._home_setup_mode = False
                        self.settings["home_lat"] = lat
                        self.settings["home_lon"] = lon
                        save_settings(self.settings)
                        _speak(f"{lat}, {lon} set as your home location.")
                    self._suppress_update_ui_until = 0
                    self._last_jump_display_label = f"{lat}, {lon}"
                    self._last_jump_display_until = time.time() + 1.5
                    if not _IS_LAND(lat, lon):
                        self._last_jump_display_label += " (appears to be in water — use arrow keys to move to land)"
                    wx.CallAfter(self.map_panel.set_position, self.lat, self.lon,
                                 self.street_mode, self.street_label)
                    wx.CallAfter(self.update_ui, self._last_jump_display_label)
                    threading.Thread(target=self._lookup, daemon=True).start()
                    self.listbox.SetFocus()
                    return
                else:
                    _speak("Coordinates out of range. Latitude -90 to 90, longitude -180 to 180.")
                    self.listbox.SetFocus()
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
                        self._status_update("No online result found.", force=True)
                        wx.CallAfter(self.listbox.SetFocus)
                        return
                else:
                    self._status_update("Not found.", force=True)
                    wx.CallAfter(self.listbox.SetFocus)
                    return
            else:
                self._status_update(
                    "Not found. Type at least 4 characters to search online.",
                    force=True,
                )
                wx.CallAfter(self.listbox.SetFocus)
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

        def _jump_candidate_sort_key(c):
            source = c[4]
            if source == "feature":
                return (_candidate_type_rank(c), _dist_from_current(c), _dist_from_home(c))
            return (_candidate_type_rank(c), _dist_from_home(c), _dist_from_current(c))

        candidates.sort(key=_jump_candidate_sort_key)
        candidates = candidates[:50]

        labels = [c[0] for c in candidates]
        online_choice_index = None
        if len(original_q) >= 4:
            online_choice_index = len(labels)
            labels.append(f'Search online for "{original_q}"')
        pick_dlg = wx.SingleChoiceDialog(self, "", "Jump", labels)
        if pick_dlg.ShowModal() == wx.ID_OK:
            selection = pick_dlg.GetSelection()
            if selection == online_choice_index:
                pick_dlg.Destroy()
                pick_dlg = None
                self._status_update("Searching online...", force=True)
                online_candidates = self._online_place_candidates(original_q)
                if not online_candidates:
                    self._status_update("No online result found.", force=True)
                    wx.CallAfter(self.listbox.SetFocus)
                    return
                online_labels = [c[0] for c in online_candidates]
                online_dlg = wx.SingleChoiceDialog(
                    self, "", "Online Jump Results", online_labels)
                if online_dlg.ShowModal() != wx.ID_OK:
                    online_dlg.Destroy()
                    self.listbox.SetFocus()
                    return
                label, lat, lon, _, source = online_candidates[online_dlg.GetSelection()]
                online_dlg.Destroy()
            else:
                label, lat, lon, _, source = candidates[selection]
            self.lat = lat
            self.lon = lon
            self.street_label = "" if self.street_mode else self.street_label
            self._jump_street_label = None
            self._jump_street_pin_lat = None
            self._jump_street_pin_lon = None
            self._jump_address_number = None
            self._jump_address_street = None
            miab_log("navigation", f"Jump to: {label} ({lat:.3f}, {lon:.3f})", self.settings)
            self._suppress_update_ui_until = 0
            self.last_location_str = label
            self._last_jump_display_label = label
            self._last_jump_display_until = time.time() + (8.0 if source == "online" else 1.5)
            if source == "online":
                self._pinned_jump_label = label
                self._pinned_jump_label_until = time.time() + 8.0
                self._cache_place_result(label, lat, lon)
            wx.CallAfter(self.map_panel.set_position, self.lat, self.lon,
                         self.street_mode, self.street_label)
            wx.CallAfter(self.update_ui, label)
            # Save as home location if this is first-run setup
            if getattr(self, '_home_setup_mode', False):
                self._home_setup_mode = False
                self.settings["home_lat"] = lat
                self.settings["home_lon"] = lon
                save_settings(self.settings)
                self.update_ui(
                    f"{label} set as your home location. "
                    f"You can change this any time in Settings.")
            threading.Thread(target=self._lookup, daemon=True).start()
        elif getattr(self, '_home_setup_mode', False):
            # User cancelled — default to Sydney and save
            self._home_setup_mode = False
            self.settings["home_lat"] = -33.8688
            self.settings["home_lon"] =  151.2093
            save_settings(self.settings)
        if pick_dlg:
            pick_dlg.Destroy()
        self.listbox.SetFocus()

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
                    wx.CallAfter(self._status_update, "No aircraft detected overhead.", True)
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
                    wx.CallAfter(self._status_update, "No airborne aircraft detected overhead.", True)
                    return

                flights.sort(key=lambda x: x["dist"])
                if not flights:
                    wx.CallAfter(self._status_update, "No identified airline flights overhead.", True)
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
                    lb.Delete(i)
                    lb.Insert(lbl, i)
                    lb.SetSelection(i)
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
                print(f"[FlightCache] Save failed: {exc}")
            wx.CallAfter(status_lbl.SetLabel, msg)
            if lb is not None and idx is not None:
                num   = flight["flight_num"] if flight["airline"] else ""
                parts = [p for p in [flight["airline"] or num, num,
                                     flight["alt_ft"], flight["heading"],
                                     f"→ {route_str}"] if p]
                new_label = "  ".join(parts)
                def _update_lb(i=idx, lbl=new_label):
                    lb.Delete(i)
                    lb.Insert(lbl, i)
                    lb.SetSelection(i)
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
                    print(f"[FlightDest] OpenSky route lookup failed: {exc}")

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
            self._status_update("AviationStack API key not set. Add it in Settings.", force=True)
            return

        self._status_update("Looking up nearest airport flights...")
        lat, lon = self.lat, self.lon

        def _fetch():
            try:
                import csv, math
                path = self._ensure_airports_csv()
                if not path:
                    wx.CallAfter(self._status_update, "Airport data not available.", True)
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
                    wx.CallAfter(self._status_update, "No airport found nearby.", True)
                    return

                icao     = best.get('ident', '')
                name     = best.get('name', icao)
                iata     = best.get('iata_code', '').strip()
                name_str = f"{name} ({iata})" if iata else name

                if not iata:
                    wx.CallAfter(self._status_update,
                                 f"No IATA code for {name} — cannot look up flights.",
                                 True)
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
                wx.CallAfter(self._status_update, f"Airport flights failed: {exc}", True)

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
                 lambda e: dlg.Destroy() if e.GetKeyCode() == wx.WXK_ESCAPE else e.Skip())
        vs.Add(btn, 0, wx.ALIGN_RIGHT | wx.RIGHT | wx.BOTTOM, 8)
        dlg.SetSizer(vs)
        dlg.CentreOnScreen()
        dlg.Show()
        txt.SetFocus()


if __name__ == "__main__":
    import atexit, sys

    _LOG_PATH = os.path.join(USER_DIR, "miab.log")
    os.environ["MIAB_LOG_PATH"] = _LOG_PATH

    class _Tee:
        """Write to log file, and also to the original stream if one exists."""
        def __init__(self, original):
            self._orig = original  # None when console=False in frozen exe
            self._file = open(_LOG_PATH, "w", encoding="utf-8", buffering=1)
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
        def close(self):
            try: self._file.close()
            except Exception: pass

    _tee_out = _Tee(sys.stdout)
    _tee_err = _Tee(sys.stderr)
    sys.stdout = _tee_out
    sys.stderr = _tee_err

    def _cleanup_log():
        sys.stdout = _tee_out._orig or sys.__stdout__
        sys.stderr = _tee_err._orig or sys.__stderr__
        _tee_out.close()
        _tee_err.close()

    atexit.register(_cleanup_log)

    import datetime as _dt2
    miab_log("navigation", "Map in a Box started.", None)

    import atexit as _atexit2
    _atexit2.register(lambda: miab_log("navigation", "Map in a Box closed.", None))

    app   = wx.App(False)
    data  = load_offline_data()
    if not data:
        wx.MessageBox(
            "worldcities.csv.gz not found.\n\n"
            "This file should be bundled with Map in a Box.\n"
            "Please reinstall the application.",
            "Missing Data File", wx.ICON_ERROR)
        os._exit(1)
    facts = load_facts()
    MapNavigator(data, facts)
    app.MainLoop()
