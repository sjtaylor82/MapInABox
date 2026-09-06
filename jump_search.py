"""Place search, jump history, and Jump dialog behaviour."""

import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request

import wx

from app_paths import CACHE_DIR
from geo import dist_km
from geo_features import GeoFeatures
from logging_utils import miab_log
from speech_dispatch import speak as _speak
from world_map_panel import _IS_LAND

PLACE_CACHE_PATH = os.path.join(CACHE_DIR, "place_cache.json")


def _nearest_city(*args, **kwargs):
    from core import _nearest_city as nearest_city
    return nearest_city(*args, **kwargs)


def save_settings(settings):
    from core import save_settings as save
    return save(settings)


class JumpSearchMixin:
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
            source_rank = 0 if source == "country" else 1
            affinity  = _geo_affinity(c)
            penalty   = _label_importance_penalty(c)
            if source == "feature":
                return (type_rank, source_rank, penalty, affinity,
                        _dist_from_current(c), _dist_from_home(c))
            return (type_rank, source_rank, penalty, affinity,
                    _dist_from_home(c), _dist_from_current(c))

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
