import time

_PROCESS_START_T0 = time.perf_counter()

import csv
import json
import math
import os
import re
import threading
import urllib.parse
import urllib.request

from logging_utils import miab_log
from i18n import set_language
from speech_dispatch import SpeechDispatch, braille as _braille, speak as _speak
from wx_utils import IS_MAC, MSAAListBox, _log_key_event, _primary_down
from keystrokes import action_for_event, disabled_default_for_event
from lookups import LookupsMixin
from nav import NavMixin
from walk import WalkMixin
from tools import ToolsMixin
from free import FreeMixin

try:
    from updater import UpdateChecker
except ImportError:
    UpdateChecker = None

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
    nearest_point_on_segment,
)
from overpass_client import OverpassClient
from transit_lookup import TransitLookup
from free import FreeExploreEngine
from nav import NavigationEngine
from here_poi import HereClient as HerePoi
import mall_directory
import user_maps
from user_map_dialogs import LocalRouteDialog, MapDrawerDialog
from postal_codes import PostalCodeLookup
from network_utils import NETWORK_UNAVAILABLE_MESSAGE
from app_paths import (
    APP_DIR, CACHE_DIR, EDUCATION_EDITION, PORTABLE_MODE, RESOURCE_DIR,
    USER_DIR,
)
from secret_store import (
    CREDENTIAL_KEYS, PORTABLE_PLAINTEXT, SECURE, SecretStoreError,
    clear_secure_credentials, load_secure_credentials,
    remove_credentials_from_dict,
    save_secure_credentials,
)
from distance_units import (
    format_distance, format_distance_label, set_unit_system,
)
from geo_features import GeoFeatures
from sound_engine import COUNTRY_ALIASES, SoundEngine
from world_map_panel import (
    COL_BG,
    WorldMapPanel,
    _GEO_COUNTRIES,
    _GEO_LAND_POLYGONS,
    _IS_LAND,
)
from street_search_frame import _StreetSearchFrame
from street_survey import StreetSurveyMixin
from jump_search import JumpSearchMixin
from imagery import ImageryMixin
from street_mode import StreetModeMixin
from poi_search import (
    POI_LIVE_COOLDOWN_SECS,
    PoiSearchMixin,
)

import sys as _sys
APP_NAME      = 'Map in a Box'
APP_VERSION   = '2026.9.2'

# Bundled read-only resources — inside the executable bundle or source tree.
BASE_DIR = RESOURCE_DIR

for _d in (USER_DIR, CACHE_DIR):
    os.makedirs(_d, exist_ok=True)

# ── Bundled resources (read-only) ────────────────────────────────────────────
CSV_PATH               = os.path.join(BASE_DIR,  "worldcities.csv.gz")
FACTS_PATH             = os.path.join(BASE_DIR,  "facts.json")
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
AIRPORTS_CSV_PATH      = os.path.join(CACHE_DIR, "airports.csv")
AIRPORTS_CSV_SEED      = os.path.join(BASE_DIR,  "airports.csv.gz")
AIRPORTS_CSV_URL       = "https://davidmegginson.github.io/ourairports-data/airports.csv"
PLACE_NAME_CLOSE_KM = 5.0
# Keep remote-area place labels conservative so we do not announce
# faraway towns when there is no local feature match.
NEAREST_PLACE_FALLBACK_KM = 20.0

AIRPORTS_STALE_DAYS = 90


# One shared Overpass client used by all callers in this module.
_overpass = OverpassClient()


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
    "poi_browse_radius_km":    2,
    "suppress_warn_google":    False,
    "mistral_api_key":         "",
    "google_service_enabled":  True,
    "mistral_service_enabled": True,
    "here_service_enabled":    True,
    "ors_service_enabled":     True,
    "aviationstack_service_enabled": True,
    "opensky_service_enabled": True,
    "rapidapi_service_enabled": True,
    "credential_storage":      SECURE,
    "nav_provider":           "osm",   # "osm" or "google" or "here"
    "departure_board_source": "gtfs",  # "gtfs" or "google"
    "here_api_key":           "",
    "ors_api_key":            "",
    "weather_temperature_unit": "auto",  # "auto", "celsius", or "fahrenheit"
    "distance_unit":          "metric",  # "metric" or "imperial"
    "poi_source":             "osm",   # "osm" or "here"
    "visual_mapping_source":  "auto",  # "google", "mapillary", or legacy-aware "auto"
    "journey_transport_source": "rome2rio",  # free multimodal discovery by default
    "driving_route_source":   "osrm",  # paid Google driving is explicit opt-in
    "key_bindings":           {},
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

_SERVICE_FLAGS_BY_CREDENTIAL = {
    "google_api_key": "google_service_enabled",
    "mistral_api_key": "mistral_service_enabled",
    "here_api_key": "here_service_enabled",
    "ors_api_key": "ors_service_enabled",
    "aviationstack_api_key": "aviationstack_service_enabled",
    "opensky_client_id": "opensky_service_enabled",
    "opensky_client_secret": "opensky_service_enabled",
    "rapidapi_key": "rapidapi_service_enabled",
}


class ServiceSettings(dict):
    """Keep credentials stored while hiding disabled services at runtime."""

    def get(self, key, default=None):
        flag = _SERVICE_FLAGS_BY_CREDENTIAL.get(key)
        if flag and not bool(dict.get(self, flag, True)):
            return default
        return dict.get(self, key, default)

def load_settings():
    s = dict(DEFAULT_SETTINGS)
    if os.path.exists(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH, encoding="utf-8") as f:
                saved = json.load(f)
            # Credentials from retired integrations are migrated by the
            # secure store.  This public but unused Custom Search identifier
            # is obsolete and should not linger in the settings file.
            saved.pop("serper_api_key", None)
            saved.pop("google_cse_id", None)
            s.update(saved)
        except Exception:
            saved = {}
    else:
        saved = {}
    plaintext_credentials = any(
        str(saved.get(name, "") or "").strip() for name in CREDENTIAL_KEYS)
    explicitly_chosen = "credential_storage" in saved
    if not PORTABLE_MODE:
        s["credential_storage"] = SECURE
    elif (EDUCATION_EDITION
          and s.get("credential_storage") == PORTABLE_PLAINTEXT):
        from education_policy import load_education_policy
        if not load_education_policy()[
                "allow_portable_plaintext_credentials"]:
            # Revoking the machine-wide permission also moves any existing
            # portable plaintext credentials back into secure storage.
            s["credential_storage"] = SECURE
    if plaintext_credentials and (
            not explicitly_chosen or s.get("credential_storage") == SECURE):
        # Keep legacy values in memory until the user has chosen and a secure
        # write has been verified.  The original JSON remains recoverable.
        s["_credential_migration_pending"] = True
    elif s.get("credential_storage") == SECURE:
        try:
            load_secure_credentials(s)
        except SecretStoreError as exc:
            s["_credential_store_error"] = str(exc)
    return ServiceSettings(s)

def save_settings(s):
    data = {
        k: v for k, v in dict(s).items()
        if (not str(k).startswith("_")
            and k not in {"serper_api_key", "google_cse_id"})
    }
    storage = (
        s.get("credential_storage", SECURE)
        if PORTABLE_MODE else SECURE
    )
    data["credential_storage"] = storage
    try:
        if storage == SECURE:
            save_secure_credentials(data)
            remove_credentials_from_dict(data)
        elif storage == PORTABLE_PLAINTEXT:
            # Prevent an older secure value reappearing if the user later
            # changes storage mode after editing or clearing the portable key.
            try:
                clear_secure_credentials()
            except SecretStoreError:
                pass
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as exc:
        miab_log(
            "errors", f"Settings could not be saved: {exc}",
            getattr(s, "settings", s))
        return False


def migrate_legacy_credentials(parent, settings) -> bool:
    """Move existing JSON credentials after an explicit portable choice."""
    if not settings.get("_credential_migration_pending", False):
        return True
    storage = SECURE
    if PORTABLE_MODE:
        allow_plaintext = True
        if EDUCATION_EDITION:
            from education_policy import load_education_policy
            allow_plaintext = load_education_policy()[
                "allow_portable_plaintext_credentials"]
        if allow_plaintext:
            answer = wx.MessageBox(
                "Existing API keys are stored as plain text in portable "
                "MIAB.\n\nChoose Yes to move them to secure storage on "
                "this computer. Choose No to keep them with portable MIAB "
                "as plain text (not recommended).",
                "Protect API Keys",
                wx.YES_NO | wx.YES_DEFAULT | wx.ICON_WARNING,
                parent=parent,
            )
            storage = SECURE if answer == wx.YES else PORTABLE_PLAINTEXT
        else:
            wx.MessageBox(
                "Existing API keys will be moved to secure storage. "
                "Plain-text storage has not been allowed by the computer "
                "administrator.",
                "Protect API Keys", wx.OK | wx.ICON_INFORMATION,
                parent=parent,
            )
    settings["credential_storage"] = storage
    if not save_settings(settings):
        wx.MessageBox(
            "The API keys could not be moved. The existing settings file "
            "has been left unchanged.",
            "API Key Storage", wx.OK | wx.ICON_ERROR, parent=parent)
        return False
    settings.pop("_credential_migration_pending", None)
    return True



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




# Antarctica hardcoded polygon




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




# ---------------------------------------------------------------------------
# Non-modal street search — live-updating as Stage 2 loads
# ---------------------------------------------------------------------------





class MapNavigator(
        NavMixin, WalkMixin, ToolsMixin, FreeMixin, LookupsMixin,
        StreetSurveyMixin, JumpSearchMixin, ImageryMixin, StreetModeMixin,
        PoiSearchMixin, wx.Frame):
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
        migrate_legacy_credentials(self, self.settings)
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
        self.map_zoom_factor = 1
        self.map_panel = WorldMapPanel(root, owner=self)
        self._map_sizer_item = self._h_sizer.Add(self.map_panel, 3, wx.EXPAND | wx.ALL, 4)
        self.map_panel.Bind(wx.EVT_MOTION, self._on_map_mouse_motion)
        self.map_panel.Bind(wx.EVT_LEFT_DOWN, self._on_map_mouse_click)
        self.map_panel.Bind(wx.EVT_LEFT_DCLICK, self._on_map_mouse_click)
        self.Bind(wx.EVT_CHILD_FOCUS, self._on_country_visual_focus_change)
        self.Bind(wx.EVT_MENU, self._on_country_visual_menu)

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
        self._transit.stale_confirm_cb = self._confirm_stale_gtfs_update
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
        self._user_map_data         = None   # active instructor-created local map
        self._user_map_path         = ""
        self._user_map_background   = ""
        self._user_map_backgrounds  = []
        self._user_map_floor        = None
        self._user_map_floor_index  = 0
        self._user_map_saved_state  = None
        self._user_map_graph        = None
        self._user_map_node         = None
        self._prev_lat              = None   # for latitude-line crossing detection
        self._prev_lon              = None   # for Date Line crossing detection
        self._distance_since_fetch  = 0.0
        self._fetch_in_progress     = False
        self._lookup_pending       = False
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
            "user_map_new": new_id(), "user_map_open": new_id(), "user_map_edit": new_id(),
            "user_map_close": new_id(), "user_map_route": new_id(),
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
            "fullscreen": new_id(), "zoom_in": new_id(),
            "zoom_out": new_id(), "zoom_reset": new_id(),
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
        add_item(file_menu, "user_map_new", "&Create Map File...",
                 lambda e: self._create_user_map())
        add_item(file_menu, "user_map_open", "&Open Map File...",
                 lambda e: self._open_user_map())
        add_item(file_menu, "user_map_edit", "&Edit Map File...",
                 lambda e: self._edit_user_map())
        add_item(file_menu, "user_map_close", "&Close Map File",
                 lambda e: self._close_user_map())
        file_menu.AppendSeparator()
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
        add_item(go_menu, "city_packs", "Download Area &Data...\tCtrl+Shift+F11",
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
        add_item(marks_menu, "mark_distances", "&Compare Mark Distances and Directions\tShift+Alt+M",
                 lambda e: self._report_all_mark_distances())
        add_item(marks_menu, "clear_mark", "&Clear Mark\tCtrl+Shift+M",
                 lambda e: self._prompt_mark_slot(remove=True))
        menubar.Append(marks_menu, "Mar&ks")

        map_menu = wx.Menu()
        add_item(map_menu, "user_map_route", "Directions Between Map Places\tCtrl+R",
                 lambda e: self._user_map_route())
        map_menu.AppendSeparator()
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
        map_menu.AppendSeparator()
        add_item(map_menu, "zoom_in", "Zoom &In\tCtrl++",
                 lambda e: self._change_visual_zoom(1))
        add_item(map_menu, "zoom_out", "Zoom &Out\tCtrl+-",
                 lambda e: self._change_visual_zoom(-1))
        add_item(map_menu, "zoom_reset", "&Reset Zoom\tCtrl+0",
                 lambda e: self._set_visual_zoom(1))
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
        reviews_label = (
            "Google &Rating for Selected POI\tCtrl+Alt+5"
            if EDUCATION_EDITION
            else "Open Google &Reviews for Selected POI\tCtrl+Alt+5"
        )
        add_item(poi_menu, "poi_mistral", reviews_label,
                 lambda e: self._run_after_menu(lambda: self._poi_detail(5)))
        if not EDUCATION_EDITION:
            add_item(poi_menu, "poi_menu", "Find &Food Menu\tCtrl+Alt+6",
                     lambda e: self._run_after_menu(lambda: self._poi_detail(6)))
        if not EDUCATION_EDITION:
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
            (primary_accel, ord('R'), int(ids["user_map_route"])),
            (primary_accel, ord(','), int(ids["settings"])),
        ])
        self.SetAcceleratorTable(self._accelerator_table)
        self.Bind(wx.EVT_MENU_OPEN, self._on_main_menu_open)
        self.Bind(wx.EVT_MENU_CLOSE, self._on_main_menu_close)

        toolbar = self.CreateToolBar(wx.TB_HORIZONTAL | wx.TB_TEXT)
        tool_specs = [
            ("jump", "Jump", "Jump to a city, country, or coordinates (J)"),
            ("street", "Street", "Toggle street mode (F11)"),
            ("nearby", "Nearby", "Nearby POI search (P)"),
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

    def _create_user_map(self):
        """Open the intentionally small path-and-label map drawer."""
        choice = wx.SingleChoiceDialog(
            self, "How would you like to begin?", "Create Map File",
            ["Draw a new map", "Import a JPG, PNG, or PDF"],
        )
        choice.SetSelection(1)
        try:
            if choice.ShowModal() != wx.ID_OK:
                return
            use_image = choice.GetSelection() == 1
        finally:
            choice.Destroy()
        background = ""
        source_path = ""
        if use_image:
            picker = wx.FileDialog(
                self, "Choose a map image or PDF",
                wildcard=("Map sources (*.jpg;*.jpeg;*.png;*.pdf)|*.jpg;*.jpeg;*.png;*.pdf|"
                          "PDF files (*.pdf)|*.pdf|Image files (*.jpg;*.jpeg;*.png)|*.jpg;*.jpeg;*.png"),
                style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
            )
            try:
                if picker.ShowModal() != wx.ID_OK:
                    return
                background = picker.GetPath()
                source_path = background
            finally:
                picker.Destroy()
        source_text = ""
        source_page = 0
        source_size = None
        backgrounds = []
        imported_floors = []
        if background.lower().endswith(".pdf"):
            try:
                page_count = user_maps.pdf_page_count(background)
                import_all = False
                if page_count > 1:
                    mode_dialog = wx.SingleChoiceDialog(
                        self, "How should the PDF pages be imported?", "Import PDF",
                        ["Import all pages as floors", "Import one page"])
                    try:
                        if mode_dialog.ShowModal() != wx.ID_OK:
                            return
                        import_all = mode_dialog.GetSelection() == 0
                    finally:
                        mode_dialog.Destroy()
                    if not import_all:
                        page_dialog = wx.SingleChoiceDialog(
                            self, "Which PDF page should this map use?", "Select PDF Page",
                            [f"Page {number}" for number in range(1, page_count + 1)])
                        try:
                            if page_dialog.ShowModal() != wx.ID_OK:
                                return
                            source_page = page_dialog.GetSelection()
                        finally:
                            page_dialog.Destroy()
                pages = range(page_count) if import_all else [source_page]
                for page_index in pages:
                    rendered = os.path.join(CACHE_DIR, f"user_map_pdf_page_{page_index + 1}.png")
                    text, pixel_width, pixel_height = user_maps.render_pdf_page(
                        background, page_index, rendered)
                    width = 500.0
                    height = max(10.0, width * pixel_height / pixel_width)
                    floor = user_maps.new_floor(
                        f"Floor {page_index + 1}", width, height, page_index)
                    floor["source_text"] = text
                    try:
                        floor["places"] = user_maps.detect_pdf_labels(
                            background, page_index, width, height,
                            f"floor-{page_index + 1}")
                        if not floor["places"]:
                            floor["places"] = user_maps.detect_image_labels(
                                rendered, width, height, f"floor-{page_index + 1}")
                    except Exception as exc:
                        # Rendering the source is still useful when optional OCR
                        # is unavailable; the instructor can label it later.
                        miab_log("errors", f"PDF map label detection failed: {exc}", self.settings)
                    if floor["places"]:
                        floor["start"] = floor["places"][0]["id"]
                    imported_floors.append(floor)
                    backgrounds.append(rendered)
                if not import_all:
                    background = backgrounds[0]
                    source_text = imported_floors[0]["source_text"]
                    source_size = (imported_floors[0]["width"], imported_floors[0]["height"])
            except Exception as exc:
                wx.MessageBox(f"The PDF could not be imported.\n\n{exc}",
                              "Create Map File", wx.OK | wx.ICON_ERROR, self)
                return
        name_dialog = wx.TextEntryDialog(self, "What is the map called?", "Create Map File")
        try:
            if name_dialog.ShowModal() != wx.ID_OK:
                return
            name = name_dialog.GetValue().strip() or "Untitled map"
        finally:
            name_dialog.Destroy()
        data = user_maps.new_map(name, *(source_size or (500.0, 350.0)))
        if len(imported_floors) > 1:
            data["floors"] = imported_floors
            background = backgrounds
        elif imported_floors:
            imported = imported_floors[0]
            data["source_text"] = imported["source_text"]
            data["source_page"] = imported["source_page"]
            data["places"] = imported["places"]
            data["start"] = imported["start"]
        elif background:
            try:
                data["places"] = user_maps.detect_image_labels(
                    background, data["width"], data["height"], "image")
                if data["places"]:
                    data["start"] = data["places"][0]["id"]
            except Exception as exc:
                miab_log("errors", f"Map image label detection failed: {exc}", self.settings)
        else:
            data["source_text"] = source_text
            data["source_page"] = source_page
        dialog = MapDrawerDialog(
            self, data, background,
            suggested_save_path=user_maps.suggested_save_path(source_path))
        dialog.ShowModal()
        dialog.Destroy()
        self._focus_map_window_silently()

    def _open_user_map(self):
        picker = wx.FileDialog(
            self, "Open Map File",
            wildcard="Map in a Box maps (*.miabmap)|*.miabmap",
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        )
        try:
            if picker.ShowModal() != wx.ID_OK:
                return
            path = picker.GetPath()
        finally:
            picker.Destroy()
        sounds_were_enabled = getattr(self, "sounds_enabled", True)
        self.sounds_enabled = False
        try:
            # Opening a local document is an audio boundary: stop looping
            # world sounds and suppress incidental tones until it is ready.
            try:
                self.sound.stop()
                pygame.mixer.stop()
            except Exception:
                pass
            extract_dir = os.path.join(CACHE_DIR, "open_user_map")
            data, background = user_maps.load_map(path, extract_dir)
        except Exception as exc:
            wx.MessageBox(f"The map file could not be opened.\n\n{exc}",
                          "Open Map File", wx.OK | wx.ICON_ERROR, self)
            return
        finally:
            self.sounds_enabled = sounds_were_enabled
        if self._user_map_data:
            self._close_user_map(announce=False)
        self._user_map_saved_state = (
            self.lat, self.lon, bool(self.street_mode), self.street_label,
        )
        self.street_mode = False
        self._walking_mode = False
        self._free_mode = False
        self._user_map_data = data
        self._user_map_path = path
        self._user_map_backgrounds = (background if isinstance(background, list)
                                      else [background])
        self._user_map_floor_index = 0
        if not self._activate_user_map_floor(0, announce=False):
            self._close_user_map(announce=False)
            wx.MessageBox("This map has no usable travel paths.", "Open Map File",
                          wx.OK | wx.ICON_ERROR, self)
            return
        self.street_label = ""
        self._update_main_menu_state()
        floor_count = len(user_maps.floors_for(data))
        floor_text = (f" {floor_count} floors." if floor_count > 1 else "")
        self._status_update(f"Opened map {data['name']}.{floor_text} "
                            f"{self._user_map_floor['name']}.", force=True)

    def _edit_user_map(self):
        picker = wx.FileDialog(
            self, "Edit Map File",
            wildcard="Map in a Box maps (*.miabmap)|*.miabmap",
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        try:
            if picker.ShowModal() != wx.ID_OK:
                return
            path = picker.GetPath()
        finally:
            picker.Destroy()
        try:
            extract_dir = os.path.join(CACHE_DIR, "edit_user_map")
            data, backgrounds = user_maps.load_map(path, extract_dir)
        except Exception as exc:
            wx.MessageBox(f"The map file could not be edited.\n\n{exc}",
                          "Edit Map File", wx.OK | wx.ICON_ERROR, self)
            return
        dialog = MapDrawerDialog(self, data, backgrounds, save_path=path)
        dialog.ShowModal()
        dialog.Destroy()
        self._focus_map_window_silently()

    def _activate_user_map_floor(self, index, announce=True):
        floors = user_maps.floors_for(self._user_map_data)
        if not 0 <= index < len(floors):
            return False
        floor = floors[index]
        coords, edges, place_nodes = user_maps.build_graph(floor)
        self._user_map_floor_index = index
        self._user_map_floor = floor
        self._user_map_background = (
            self._user_map_backgrounds[index]
            if index < len(self._user_map_backgrounds) else "")
        self._user_map_graph = (coords, edges, place_nodes)
        places = floor.get("places") or []
        start_id = floor.get("start")
        start = next((p for p in places if p["id"] == start_id), places[0] if places else None)
        self._user_map_node = place_nodes.get(start["id"]) if start else None
        target_x = start["x"] if start else floor["width"] / 2.0
        target_y = start["y"] if start else floor["height"] / 2.0
        if self._user_map_node is None and coords:
            self._user_map_node = min(coords, key=lambda node: math.hypot(
                coords[node][0] - target_x, coords[node][1] - target_y))
        if self._user_map_node is not None:
            target_x, target_y = coords[self._user_map_node]
        x, y = target_x, target_y
        self.lat, self.lon = y / 111195.0, x / 111195.0
        self.map_panel._bg_bitmap = None
        self.map_panel.set_position(self.lat, self.lon, False, "")
        self.map_panel.Refresh()
        if announce:
            location = f" Starting at {start['name']}." if start else ""
            self._status_update(f"{floor['name']}.{location}", force=True)
        return True

    def _close_user_map(self, announce=True):
        if not self._user_map_data:
            if announce:
                self._status_update("No map file is open.", force=True)
            return
        saved = self._user_map_saved_state
        name = self._user_map_data.get("name", "map")
        self._user_map_data = None
        self._user_map_path = ""
        self._user_map_background = ""
        self._user_map_backgrounds = []
        self._user_map_floor = None
        self._user_map_floor_index = 0
        self._user_map_saved_state = None
        self._user_map_graph = None
        self._user_map_node = None
        if saved:
            self.lat, self.lon, self.street_mode, self.street_label = saved
        self.map_panel._bg_bitmap = None
        self.map_panel.set_position(self.lat, self.lon, self.street_mode, self.street_label)
        self.map_panel.Refresh()
        self._update_main_menu_state()
        if announce:
            self._status_update(f"Closed map {name}.", force=True)

    def _user_map_route(self):
        data = getattr(self, "_user_map_floor", None)
        if not data:
            self._status_update("Open a map file before requesting a local route.", force=True)
            return
        if len(data.get("places") or []) < 2:
            self._status_update("This map needs at least two labelled places for directions.", force=True)
            return
        dialog = LocalRouteDialog(self, data)
        dialog.ShowModal()
        dialog.Destroy()
        self._focus_map_window_silently()

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
        local_map = bool(getattr(self, "_user_map_data", None))
        world = not street and not getattr(self, "_walking_mode", False) and not local_map
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
        self._menu_items["user_map_close"].Enable(local_map)
        self._menu_items["user_map_route"].Enable(local_map)

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
        self.map_panel.dismiss_country_visual()
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

    def _on_country_visual_focus_change(self, event):
        # wx can deliver the focus event caused by the command that opened the
        # card after F8 itself has returned.  Do not let that queued event erase
        # the result before the first paint.
        shown_at = getattr(self.map_panel, "_country_visual_shown_at", 0.0)
        if time.time() - shown_at >= 0.35:
            self.map_panel.dismiss_country_visual()
        event.Skip()

    def _on_country_visual_menu(self, event):
        self.map_panel.dismiss_country_visual()
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
        if EDUCATION_EDITION and key_num == 6:
            return
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
        then, in Pro, offer to open the full reviews. Education announces only
        the rating and review count and never hands off to a browser."""
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

        if EDUCATION_EDITION:
            self._poi_detail_announce(
                summary or f"No Google review information found for {name}."
            )
            if getattr(self, "listbox", None) is not None:
                self.listbox.SetFocus()
            return

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
                    results = self._rank_menu_results(
                        raw, distinctive, compact, suburb)

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
                                    distinctive, compact, suburb)
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
        import re as _re

        tags = poi.get("tags") or {}
        for key in ("addr:suburb", "addr:city", "addr:town", "addr:village"):
            value = (tags.get(key) or "").strip()
            if value:
                return value

        for key in ("suburb", "city", "town", "village"):
            value = (poi.get(key) or "").strip()
            if value:
                return value

        # Some POI providers supply only a display address. Recover an
        # Australian suburb from "... Cleveland QLD 4163, Australia" rather
        # than falling back to the suburb currently under the map cursor.
        address = (
            poi.get("address") or poi.get("addr") or tags.get("addr:full") or ""
        ).strip()
        for part in reversed(address.split(",")):
            match = _re.match(
                r"^\s*(.+?)\s+(?:ACT|NSW|NT|QLD|SA|TAS|VIC|WA)\s+\d{4}\s*$",
                part,
                flags=_re.I,
            )
            if match:
                return match.group(1).strip()

        return (getattr(self, "_current_suburb", "") or "").strip()

    def _menu_search_queries(self, name: str, suburb: str = "") -> list[str]:
        """Build a single, locality-anchored query: "Name" suburb menu country.

        Suburb and country are soft (unquoted) terms — they bias results to
        the right branch without hard-excluding the venue's own pages."""
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

    def _classify_menu_result(
        self,
        item,
        distinctive: list,
        compact: str,
        locality: str = "",
    ) -> "int | None":
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
            if self._delivery_wrong_country(host, parsed.path):
                return None
            # Delivery domains host thousands of unrelated venues and branches.
            # Keep a result only when its own title/URL/snippet identifies both
            # this venue and the selected locality.
            delivery_text = " ".join((
                item.get("title") or "",
                url,
                item.get("snippet") or "",
            )).lower()
            delivery_compact = re.sub(r"[^a-z0-9]+", "", delivery_text)
            venue_matches = (
                (bool(compact) and compact in delivery_compact)
                or any(token in delivery_compact for token in distinctive)
            )
            locality_compact = re.sub(
                r"[^a-z0-9]+", "", (locality or "").lower())
            locality_matches = (
                not locality_compact or locality_compact in delivery_compact
            )
            return 0 if venue_matches and locality_matches else None
        if self._label_matches_venue(self._main_label(host), distinctive, compact):
            hay = parsed.path + " " + (item.get("title") or "").lower()
            if any(sig in hay for sig in self._MENU_SIGNALS):
                return 1
        return None

    def _is_own_menu(self, item, distinctive: list, compact: str) -> bool:
        return self._classify_menu_result(item, distinctive, compact) == 1

    def _rank_menu_results(
        self,
        results: list,
        distinctive: list,
        compact: str,
        locality: str = "",
    ) -> list:
        """Keep only delivery pages and the venue's own menu pages; delivery
        first, original order within each group. Everything else is dropped."""
        scored = []
        for idx, item in enumerate(results):
            pr = self._classify_menu_result(
                item, distinctive, compact, locality)
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
                selected_street_poi = (
                    getattr(self, "_street_survey_current_poi", None)
                    if (getattr(self, "street_mode", False)
                        and self._street_survey_address_announce_mode() == "poi_only")
                    else None
                )
                if (not getattr(self, '_walking_mode', False)
                        and pinned_street and (pinned_num or selected_street_poi)
                        and pin_lat is not None and pin_lon is not None
                        and dist_metres(self.lat, self.lon, pin_lat, pin_lon) <= 150.0):
                    suburb = getattr(self, "_current_suburb", "") or ""
                    address = (f"{pinned_num} {pinned_street}"
                               if pinned_num else pinned_street)
                    wx.CallAfter(
                        self._status_update,
                        address + (f", {suburb}" if suburb else "")
                    )
                    return

                if getattr(self, '_walking_mode', False):
                    street = getattr(self, '_walk_street', '') or ''
                    if street:
                        num = self._walk_nearest_address_number(
                            self.lat,
                            self.lon,
                            street,
                            getattr(self, '_walk_heading', 0.0),
                            radius=200,
                        )
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
        initial_radius = int(self.settings.get("poi_browse_radius_km", 2) or 2) * 1000
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
        self._poi_list = []
        self._poi_index = 0
        self._poi_explore_stack = []
        self.listbox.SetFocus()
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

        # The city-wide street geometry may place a selected road beyond the
        # one-kilometre background POI area.  Re-centre that fetch so HERE/OSM
        # businesses and their structured addresses become available to Page
        # Up/Down and shared-address browsing on the selected street.
        poi_lat = getattr(self, "_poi_fetch_lat", None)
        poi_lon = getattr(self, "_poi_fetch_lon", None)
        if (poi_lat is None or poi_lon is None or
                dist_metres(self.lat, self.lon, poi_lat, poi_lon) >
                POI_BACKGROUND_RADIUS_METRES):
            self._clear_street_survey_cache()
            self._status_update(
                f"Loading nearby businesses for {street_name}...", force=True)
            threading.Thread(
                target=self._fetch_all_pois_background,
                args=(getattr(self, "_address_points", []), True,
                      self.lat, self.lon, self._street_fetch_id),
                daemon=True,
            ).start()

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
        if EDUCATION_EDITION:
            return
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
                dist_str, direction = self._format_mark_distance(
                    coords_a, coords_b)
                direction = direction.replace("-", " ")
                parts.append(
                    f"{name_b} is {dist_str} {direction} of {name_a}.")
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
        dist_str, direction = self._format_mark_distance(origin, (lat, lon))
        direction = direction.replace("-", " ")
        if current_name:
            msg = f"{place_name} is {dist_str} {direction} from {current_name}."
        else:
            msg = f"{place_name} is {dist_str} {direction} from here."
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
            self.map_panel._bg_bitmap = None
            self.map_panel._bg_bitmap_view_key = None
            self.map_panel.Refresh()
        self._announce_transient(self._map_display_mode_name(new_mode))
        return new_mode

    _VISUAL_ZOOM_LEVELS = (1, 2, 4, 8, 16, 32, 64, 128)

    def _set_visual_zoom(self, factor):
        factor = min(self._VISUAL_ZOOM_LEVELS,
                     key=lambda value: abs(value - int(factor)))
        self.map_zoom_factor = factor
        if hasattr(self, "map_panel"):
            self.map_panel._bg_bitmap = None
            self.map_panel._bg_bitmap_view_key = None
            self.map_panel.Refresh()
            self.map_panel.Update()
        self._announce_transient(f"Zoom {factor} X.")

    def _change_visual_zoom(self, direction):
        current = int(getattr(self, "map_zoom_factor", 1))
        try:
            index = self._VISUAL_ZOOM_LEVELS.index(current)
        except ValueError:
            index = 0
        index = max(0, min(len(self._VISUAL_ZOOM_LEVELS) - 1,
                           index + (1 if direction > 0 else -1)))
        self._set_visual_zoom(self._VISUAL_ZOOM_LEVELS[index])

    def _flash_current_country(self):
        """F8 — highlight the country and display its enlarged silhouette."""
        country = getattr(self, 'last_country_found', '')
        if not country or country == 'Open Water':
            return False
        # F8 is also used by the external visual-description workflow.  Once
        # magnified, preserve the current viewport for that capture instead of
        # replacing it with the standard 1x country silhouette.
        if getattr(self, "map_zoom_factor", 1) > 1:
            self.map_panel.dismiss_country_visual()
            return "zoom"
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
        if hasattr(self, "map_panel"):
            self.map_panel.show_visual_assist_caption(msg_text)
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

    def _announce_transient(self, msg, braille_msg=None,
                            visual_caption=True) -> None:
        """Speak and braille a transient announcement without touching the listbox."""
        if getattr(self, "_update_dialog_active", False):
            self._verbose_trace(f"transient suppressed while update dialog is active: {msg!r}")
            return
        if visual_caption and hasattr(self, "map_panel"):
            self.map_panel.show_visual_assist_caption(msg)
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
        if hasattr(self, "map_panel"):
            self.map_panel.show_visual_assist_caption(msg_text)
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
        self._announce_transient(msg_text, visual_caption=False)

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
        current_country = getattr(self, "last_country_found", "")
        if current_country in ("Antarctica", "Open Water"):
            self._status_update(current_country, force=True)
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
        country = getattr(self, "last_country_found", "")
        if not country:
            _dist, idx = _nearest_city(self._city_lats, self._city_lons, self.lat, self.lon)
            country = self._city_regions[idx][1]
        self._status_update(country if country else "Country unknown.", force=True)

    def _announce_nearest_city_only(self):
        """N in map mode — speak the nearest city/locality only."""
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
                        # The cache was already preloaded above.  Bypass it
                        # here so the intended six-hour refresh genuinely
                        # queries OSM/HERE and replaces changed or removed
                        # POIs instead of returning the same 30-day entry.
                        args=(address_points, True, lat, lon, fetch_id),
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
            self._clear_street_survey_cache()
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
            initial_country_name=getattr(self, "last_country_found", ""),
            transport_download_cb=self._download_transport_data_for_area)
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

    def _download_transport_data_for_area(
        self,
        area_name: str,
        country_name: str,
        lat: float,
        lon: float,
    ) -> bool:
        """Start an explicit regional GTFS download from the area wizard."""
        if getattr(self, "_transport_prefetch_active", False):
            self._announce_transient_then_return(
                "A transport data download is already in progress.")
            return False

        self._transport_prefetch_active = True
        self._status_update(
            f"Preparing transport data for {area_name}…", force=True)

        def status(msg):
            wx.CallAfter(
                self._status_update, f"{area_name}: {msg}", True)

        def worker():
            try:
                feed_ids = self._transit.prefetch_for_region(
                    lat, lon, area_name, status_cb=status)
                if feed_ids:
                    message = (
                        f"Transport data download for {area_name} finished: "
                        f"{len(feed_ids)} feed"
                        f"{'s' if len(feed_ids) != 1 else ''} ready."
                    )
                else:
                    message = (
                        f"No public transport timetable data was found for "
                        f"{area_name}."
                    )
                wx.CallAfter(self._status_update, message, True)
            except Exception as exc:
                miab_log(
                    "errors",
                    f"[Transit] Area prefetch failed for "
                    f"{area_name}, {country_name}: {exc}",
                    self.settings,
                )
                wx.CallAfter(
                    self._status_update,
                    f"Could not download transport data for {area_name}.",
                    True,
                )
            finally:
                self._transport_prefetch_active = False

        threading.Thread(target=worker, daemon=True).start()
        return True

    def _open_settings(self):
        try:
            dlg = SettingsDialog(
                self, self.settings, user_dir=USER_DIR,
                transit_feeds=self._transit.cached_feed_summaries())
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
        timetable_refresh = dlg.timetables_refreshed
        set_home_requested = dlg.set_home_requested
        saved_settings = dlg.settings if saved else None
        # Destroy the ended modal before doing save work or speaking.  Leaving
        # it alive allows Windows/MSAA to restore map focus once at EndModal
        # and again when the dialog is eventually destroyed.
        dlg.Destroy()
        if saved:
            candidate_settings = ServiceSettings(saved_settings)
            if not save_settings(candidate_settings):
                wx.MessageBox(
                    "Settings could not be saved. Existing settings and API "
                    "keys were left unchanged.",
                    "Settings", wx.OK | wx.ICON_ERROR, parent=self)
                self._focus_map_window_silently()
                return
            self.settings = candidate_settings
            set_unit_system(self.settings.get("distance_unit", "metric"))
            self.settings["_log_path"] = os.path.join(USER_DIR, "miab.log")
            self._free_engine.log_settings = self.settings
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
        if saved and timetable_refresh:
            self._status_update("Updating all cached timetables...")
            threading.Thread(
                target=self._refresh_cached_timetables, daemon=True).start()
        elif saved and gtfs_refresh:
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



    def _handle_preface_shortcuts(self, event, key, shift, primary, alt, no_mod):
        if primary and not alt:
            plus_keys = {ord('+'), ord('='), getattr(wx, 'WXK_NUMPAD_ADD', -1)}
            minus_keys = {ord('-'), getattr(wx, 'WXK_NUMPAD_SUBTRACT', -1)}
            if key in plus_keys:
                self._change_visual_zoom(1)
                return True
            if key in minus_keys:
                self._change_visual_zoom(-1)
                return True
            if key in (ord('0'), getattr(wx, 'WXK_NUMPAD0', -1)):
                self._set_visual_zoom(1)
                return True

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
            miab_log("feature_usage", "Key: Ctrl+Shift+S (street view)", self.settings)
            lat, lon = self._poi_lat_lon_if_focused()
            self._streetview_at_location(lat, lon)
            return True
        if no_mod and (key == ord('C') or key == ord('c')):
            if getattr(self, '_walking_mode', False):
                self._walk_virtual_crossing()
                return True
        if primary and shift and alt and (key == ord('S') or key == ord('s')):
            miab_log("feature_usage", "Key: Ctrl+Shift+Alt+S (satellite view)", self.settings)
            lat, lon = self._poi_lat_lon_if_focused()
            self._satellite_view_at_location(lat, lon); return True
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
                if flashed == "zoom":
                    wx.CallAfter(self._status_update, "Displaying zoomed map.", True)
                else:
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
        if primary and not shift and not alt and key in (wx.WXK_HOME, wx.WXK_END):
            if (not self.street_mode
                    and not getattr(self, "_walking_mode", False)
                    and not getattr(self, "_free_mode", False)
                    and not getattr(self, "_poi_list", [])):
                if self._game.active:
                    self._announce_transient_then_return(
                        "Pole jumps are disabled during the challenge. Use your ears!")
                    return True
                self._jump_to_map_pole(north=(key == wx.WXK_HOME))
                return True
            return False
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
                key_num = alt_map[key]
                if not (EDUCATION_EDITION and key_num == 6):
                    self._poi_detail(key_num)
                return True
            if key == ord('P') or key == ord('p'):
                self._refresh_background_pois(); return True
        if not self.street_mode:
            if shift and not primary and not alt and (key == ord('P') or key == ord('p')):
                self._announce_postcode();  return True
        return False

    def _jump_to_map_pole(self, north: bool) -> None:
        """Move directly to a geographic pole while preserving longitude."""
        self.lat = 90.0 if north else -90.0
        label = "North Pole" if north else "South Pole"
        self.street_label = ""
        self._jump_street_label = None
        self._jump_street_pin_lat = None
        self._jump_street_pin_lon = None
        self._jump_address_number = None
        self._jump_address_street = None
        self._record_jump(label, self.lat, self.lon)
        miab_log(
            "navigation",
            f"Key: Ctrl+{'Home' if north else 'End'} ({label}) at longitude {self.lon:.5f}",
            self.settings,
        )
        self.map_panel.set_position(self.lat, self.lon, False, "")
        self._refresh_info_panel()
        self._status_update(f"Moving to {label}.", force=True)
        if getattr(self, "_fetch_in_progress", False):
            self._lookup_pending = True
        else:
            self._fetch_in_progress = True
            self._distance_since_fetch = 0.0
            threading.Thread(target=self._lookup, daemon=True).start()

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
            elif shift and not primary and not alt:
                self._street_survey_cycle_same_address_poi(direction)
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
            if not EDUCATION_EDITION:
                self._open_poi_website()
            return True
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
        # F8 is a temporary visual query.  Dismiss it for a subsequent command,
        # but not for standalone modifier/helper events emitted while an
        # external visual-description shortcut is being formed.  Those are not
        # MIAB functions and previously erased the silhouette before capture.
        non_command_keys = {
            0, wx.WXK_SHIFT, wx.WXK_CONTROL, wx.WXK_ALT,
            getattr(wx, "WXK_RAW_CONTROL", -1),
        }
        if hasattr(self, "map_panel") and key not in non_command_keys:
            self.map_panel.dismiss_country_visual()
        if getattr(self, "_suppress_location_restore", False):
            _log_key_event(self, event, "frame", "suppressed while restoring location")
            return
        # True when no modifier is held — used to prevent bare letter/F-key
        # handlers from firing on modifier shortcuts.
        no_mod = not shift and not primary and not alt
        _log_key_event(self, event, "frame", f"street_mode={self.street_mode} walking={getattr(self, '_walking_mode', False)} nav={getattr(self, '_nav_active', False)}")

        # An opened instructor map is an isolated local document.  Its arrows
        # move within the drawing and never enter world/street navigation.
        if self._handle_user_map_shortcuts(key, shift, primary, alt):
            return

        # Free mode owns a dense, purpose-built keyboard layer. Keep it ahead
        # of user mappings so route/side exploration cannot be disrupted.
        if not getattr(self, '_free_mode', False):
            key_contexts = (("Street", "Global") if self.street_mode
                            else ("Map", "Global"))
            custom_bindings = self.settings.get("key_bindings", {})
            custom_action = action_for_event(event, custom_bindings, key_contexts)
            if custom_action and self._run_custom_keystroke_action(custom_action):
                return
            if disabled_default_for_event(event, custom_bindings, key_contexts):
                return

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
        else:
            # World-map movement is specified in kilometres.  The horizontal
            # degree step is calculated at the current latitude below so an
            # east/west press covers a useful distance outside the tropics too.
            if primary:
                map_step_km = 100.0
            elif shift:
                map_step_km = 1.0
            else:
                map_step_km = 5.0
            step = map_step_km / 111.195

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
        lon_step = step
        if not self.street_mode:
            # Longitude lines converge toward the poles.  Cap the angular
            # change at half the globe, where east/west distance is inherently
            # ambiguous, rather than allowing one press to circle repeatedly.
            latitude_scale = abs(math.cos(math.radians(self.lat)))
            lon_step = min(180.0, step / max(latitude_scale, 1e-9))
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
            new_lon = ((self.lon - lon_step + 180) % 360) - 180
        elif key == wx.WXK_RIGHT:
            new_lon = ((self.lon + lon_step + 180) % 360) - 180

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
                        # Keep a selected house number anchored to the address
                        # where it was found.  Moving its pin with the cursor
                        # makes that number follow the user along the street
                        # and be announced at unrelated intersections.
                        if not getattr(self, "_jump_address_number", None):
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
            # Every world-map arrow movement needs spoken feedback.  A
            # distance threshold makes horizontal movement increasingly silent
            # near the poles, where a longitude step can represent less than a
            # metre.  Busy lookups are coalesced below, so forcing the lookup
            # here does not create overlapping worker threads.
            world_map_move = not self.street_mode
            if (not self.street_mode
                    and getattr(self, "_fetch_in_progress", False)):
                # Coalesce rapid map movements while the previous lookup is
                # running.  Its completion will look up and announce the most
                # recent position, rather than silently dropping this move.
                self._lookup_pending = True
            elif self._should_fetch(
                    self.lat, self.lon, force=world_map_move):
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

    def _handle_user_map_shortcuts(self, key, shift, primary, alt):
        if not getattr(self, "_user_map_data", None):
            return False
        data = self._user_map_floor
        if key == wx.WXK_ESCAPE:
            self._close_user_map()
            return True
        if primary and not shift and not alt and key in (ord("R"), ord("r")):
            self._user_map_route()
            return True
        if primary and not shift and not alt and key in (wx.WXK_PAGEUP, wx.WXK_PAGEDOWN):
            floors = user_maps.floors_for(self._user_map_data)
            delta = 1 if key == wx.WXK_PAGEUP else -1
            target = self._user_map_floor_index + delta
            if not 0 <= target < len(floors):
                edge = "highest" if delta > 0 else "lowest"
                self._status_update(f"Already on the {edge} floor.", force=True)
            else:
                self._activate_user_map_floor(target)
            return True
        if primary and not shift and not alt and key in (wx.WXK_HOME, wx.WXK_END):
            left, right, bottom, top = user_maps.exploration_bounds(data)
            x = min(max(self.lon * 111195.0, left), right)
            y = top if key == wx.WXK_HOME else bottom
            self._user_map_node = None
            self.lat, self.lon = y / 111195.0, x / 111195.0
            self.map_panel.set_position(self.lat, self.lon, False, "")
            self.map_panel.Refresh()
            bounds = (bottom / 111195.0, top / 111195.0,
                      left / 111195.0, right / 111195.0)
            self._play_spatial_tone_if_allowed(self.lat, self.lon, bounds)
            edge_name = "Top" if key == wx.WXK_HOME else "Bottom"
            self._status_update(f"{edge_name} of map.", force=True)
            return True
        line_edge = None
        if not shift and not alt:
            if not primary and key == wx.WXK_HOME:
                line_edge = "start"
            elif not primary and key == wx.WXK_END:
                line_edge = "end"
            elif IS_MAC and primary and key == wx.WXK_LEFT:
                line_edge = "start"
            elif IS_MAC and primary and key == wx.WXK_RIGHT:
                line_edge = "end"
        if line_edge:
            left, right, bottom, top = user_maps.exploration_bounds(data)
            x = left if line_edge == "start" else right
            y = min(max(self.lat * 111195.0, bottom), top)
            self._user_map_node = None
            self.lat, self.lon = y / 111195.0, x / 111195.0
            self.map_panel.set_position(self.lat, self.lon, False, "")
            self.map_panel.Refresh()
            bounds = (bottom / 111195.0, top / 111195.0,
                      left / 111195.0, right / 111195.0)
            self._play_spatial_tone_if_allowed(self.lat, self.lon, bounds)
            self._status_update(
                f"{'Start' if line_edge == 'start' else 'End'} of row.",
                force=True)
            return True
        if key not in (wx.WXK_UP, wx.WXK_DOWN, wx.WXK_LEFT, wx.WXK_RIGHT):
            if key == wx.WXK_F11:
                self._status_update("Street mode is unavailable while a map file is open.", force=True)
                return True
            return False
        if alt:
            return True
        dx, dy = {
            wx.WXK_UP: (0.0, 1.0), wx.WXK_DOWN: (0.0, -1.0),
            wx.WXK_LEFT: (-1.0, 0.0), wx.WXK_RIGHT: (1.0, 0.0),
        }[key]
        left, right, bottom, top = user_maps.exploration_bounds(data)
        old_x = min(max(self.lon * 111195.0, left), right)
        old_y = min(max(self.lat * 111195.0, bottom), top)
        cell_size = max(right - left, top - bottom) / 30.0
        if primary:
            place = user_maps.next_place_in_direction(
                data, old_x, old_y, dx, dy, cell_size)
            if not place:
                self._status_update(
                    f"No labelled place {user_maps.compass_name(dx, dy)}.", force=True)
                return True
            x, y = float(place["x"]), float(place["y"])
        else:
            x = min(max(old_x + dx * cell_size, left), right)
            y = min(max(old_y + dy * cell_size, bottom), top)
            place = None
        if abs(x - old_x) < 1e-9 and abs(y - old_y) < 1e-9:
            self._status_update(
                f"Edge of map {user_maps.compass_name(dx, dy)}.", force=True)
            return True
        if user_maps.crosses_barrier(data, (old_x, old_y), (x, y)):
            self.sound.play_barrier_tone()
        self._user_map_node = None
        self.lat, self.lon = y / 111195.0, x / 111195.0
        self.map_panel.set_position(self.lat, self.lon, False, "")
        self.map_panel.Refresh()
        bounds = (bottom / 111195.0, top / 111195.0,
                  left / 111195.0, right / 111195.0)
        self._play_spatial_tone_if_allowed(self.lat, self.lon, bounds)
        labels = ([place] if place else
                  user_maps.places_in_grid_cell(data, x, y, cell_size))
        if labels:
            messages = []
            for label in labels:
                message = label["name"]
                if label.get("description"):
                    message += ". " + label["description"]
                messages.append(message)
            self._status_update("; ".join(messages) + ".", force=True)
        elif key in (wx.WXK_UP, wx.WXK_DOWN) and not primary:
            row_item_count = len(user_maps.places_in_grid_row(
                data, y, cell_size))
            if row_item_count:
                noun = "item" if row_item_count == 1 else "items"
                self._status_update(
                    f"{row_item_count} {noun}", force=True)
        return True

    def _run_custom_keystroke_action(self, action):
        """Run a command selected by the user-configurable keystroke map."""
        if action == "street_imagery":
            lat, lon = self._poi_lat_lon_if_focused()
            self._streetview_at_location(lat, lon)
            return True
        if action == "satellite_imagery":
            lat, lon = self._poi_lat_lon_if_focused()
            self._satellite_view_at_location(lat, lon)
            return True
        actions = {
            "jump": self.show_jump_dialog,
            "weather": self._announce_weather,
            "poi_search": self._announce_poi_count,
            "street_search": self._street_search,
            "address": self._announce_address,
            "navigate_address": self._nav_to_address,
            "walking_mode": self._walk_toggle,
            "route_briefing": self._nav_request_narrative_briefing,
        }
        callback = actions.get(action)
        if callback:
            callback()
            return True
        return False

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

            # Do not let an older background lookup overwrite city/state
            # context after the cursor has already moved elsewhere.  The later
            # stale check protects the final label; this one protects the
            # mutable context fields used by Country view and its callout.
            if _lookup_is_stale():
                return

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
                            # A property-level feature is useful local detail,
                            # but it must not displace a recognised populated
                            # centre such as St George or Lightning Ridge.  The
                            # feature remains eligible once no town is within
                            # the normal named-place fallback radius.
                            if dist_km <= NEAREST_PLACE_FALLBACK_KM:
                                return self._nearest_city_distance_label()
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
                    elif not self._geo_features_enabled() and not polygon_country:
                        label = self._nearest_city_distance_label()
                    else:
                        label = country if country and country.lower() != "nan" else "Location unknown"
                        _only_region = True
            else:
                # Choose country context independently of the optional
                # GeoFeatures label.  Otherwise toggling GeoFeatures can change
                # the country, sound, languages, and facts at one coordinate.
                # Antarctic ocean positions must not borrow New Zealand from
                # the nearest-city dataset, which has no Antarctic cities.
                if self.lat < -60.0:
                    country = "Open Water"
                elif (dist_km <= 75.0 and nearest_country
                      and nearest_country.lower() != "nan"):
                    country = nearest_country
                else:
                    country = "Open Water"

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
                    label = coastal_feature
                else:
                    label = self._ocean_name(self.lat, self.lon)

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

            # Keep shared geographic context current during a challenge.  The
            # challenge still suppresses normal ambient country sounds, but
            # explicit lookups and continent milestone scoring must use the
            # country beneath the cursor rather than the pre-challenge country.
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
                if getattr(self, 'sounds_enabled', True) and not self._game.active:
                    self._play_location_sound_if_allowed(
                        country if country != "Open Water" else "ocean", continent)
                miab_log("navigation",
                         f"Entered country: {country}"
                         + (f" (continent: {continent})" if continent else ""),
                         self.settings)
                self._current_subregion = ""

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
            wx.CallAfter(self.map_panel.set_position, self.lat, self.lon,
                         self.street_mode, self.street_label)
        finally:
            # Always clear fetch flag, even on error or early return
            self._fetch_in_progress = False
            if getattr(self, "_lookup_pending", False) and not self.street_mode:
                self._lookup_pending = False
                self._fetch_in_progress = True
                self._distance_since_fetch = 0.0
                threading.Thread(target=self._lookup, daemon=True).start()

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
            "Arrow keys: move about 5 kilometres.",
            "Ctrl+Home: jump to the North Pole.",
            "Ctrl+End: jump to the South Pole.",
            "Shift+arrows: fine movement of about 1 kilometre.",
            "Ctrl+arrows: move in large steps of about 100 kilometres.",
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
            "Ctrl+Plus and Ctrl+Minus: change geographic visual zoom.",
            "Ctrl+0: reset visual zoom to 1 X.",
            "F9: toggle Visual Assist mode.",
            "F10: country discovery challenge.",
            "Ctrl+F10: scored challenge session.",
            "Shift+F10: repeat challenge target.",
            "F11: street mode.",
            "Shift+F11: pre-download streets.",
            "Ctrl+Shift+F11: area downloader for streets, addresses, and transport data.",
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
            "Ctrl+1, Ctrl+2, Ctrl+3: read a mark's distance and direction from here.",
            "Shift+Alt+M: compare distances and directions between all saved marks.",
            "G: nearby geographic features.",
            "P: POI search.",
            "POI menu: selected POI address, hours, phone, website, Mistral, menu lookup, and website launch.",
            "T: local time.",
            "Z: timezone.",
            "S: sunrise and sunset.",
            "Ctrl+Shift+S: street view of selected POI (falls back to satellite if no coverage).",
            "Ctrl+Shift+Alt+S: satellite view.",
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
                "Page Up: previous known number.",
                "Page Down: next known number.",
                "Shift+Page Up or Shift+Page Down: browse multiple POIs at the current address.",
                "Ctrl+F12: switch between all addresses and addresses with POIs only.",
                "Ctrl+Alt+F12: cycle number filter between all, odd only, and even only.",
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
                (
                    "Ctrl+Alt+5: announce the selected POI's Google rating and review count."
                    if EDUCATION_EDITION
                    else "Ctrl+Alt+5: open Google reviews for selected POI in your browser."
                ),
                *([] if EDUCATION_EDITION else [
                    "Ctrl+Alt+6: search for food venue menu links.",
                ]),
                *([] if EDUCATION_EDITION else [
                    "Ctrl+W: open selected POI website.",
                ]),
                "Escape: close POI list.",
                "Backspace: go back in POI exploration.",
                "F11: return to map mode.",
                "Ctrl+Shift+S: street view (falls back to satellite if no coverage).",
                "Ctrl+Shift+Alt+S: satellite view.",
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

        header = wx.StaticText(
            dlg,
            label=(
                f"{APP_NAME}\nVersion {APP_VERSION}\n"
                "Copyright © 2026 Sam Taylor. "
                "Licensed under GNU GPL version 3 or later."
            ),
        )
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
    _policy_prefix = "--write-education-policy="
    _policy_argument = next(
        (arg for arg in sys.argv[1:] if arg.startswith(_policy_prefix)), None)
    if _policy_argument is not None:
        if not EDUCATION_EDITION:
            wx.MessageBox(
                "Education tool choices can only be changed from the "
                "Education edition.",
                "Education Admin", wx.OK | wx.ICON_ERROR)
            sys.exit(1)
        try:
            from education_policy import write_education_tools
            _enabled_policy_tools = {
                value for value in
                _policy_argument[len(_policy_prefix):].split(",") if value
            }
            _written_policy_path = write_education_tools(
                _enabled_policy_tools,
                allow_portable_plaintext_credentials=(
                    "--allow-portable-plaintext-credentials" in sys.argv[1:])),
            wx.MessageBox(
                "Education tool choices were saved for everyone who uses "
                "this computer.",
                "Education Admin", wx.OK | wx.ICON_INFORMATION)
            sys.exit(0)
        except Exception as _policy_error:
            wx.MessageBox(
                "The Education tool choices could not be saved.\n\n" +
                str(_policy_error),
                "Education Admin", wx.OK | wx.ICON_ERROR)
            sys.exit(1)
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
