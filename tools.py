"""tools.py — ToolsMixin for Map in a Box.

F12 tools (detour calculator, route explorer, toll compare,
journey planner, departure board) as a mixin class.
"""

import math
import os
import re
import sys
import threading
import urllib.parse
import urllib.request

import wx
import airport_directory
from logging_utils import miab_log
from geo import (
    GENERIC_STREET_TYPES, bearing_deg, dist_metres, nearest_point_on_segment,
)
from distance_units import format_distance

try:
    from route_tools import RouteTools
except ImportError:
    RouteTools = None

# Airports currently advertised in Virgin Australia's direct network.
_AIRLINE_DIRECT_AIRPORTS = {
    "Virgin Australia": frozenset("""
        ADL ASP BNK BNE BME CNS CBR DRW EMD GLT OOL HTI HBA KGI KTA KNX
        LST MKY MEL ISA NTL ZNE ONS PER PHE PPP ROK MCY SYD TSV AYQ DPS
        NAN ZQN APW VLI LHR CDG FCO ATH DOH
    """.split()),
}

# Dialogs imported lazily to avoid circular imports

def _get_dialogs():
    from dialogs import (
        ToolsMenuDialog, StopEntryDialog, DateTimePickerDialog,
        JourneyResultsDialog, TransitLookupDialog, RouteResultsDialog,
        RendezvousResultsDialog, FindFoodDialog, MeetPointDialog,
    )
    return (ToolsMenuDialog, StopEntryDialog, DateTimePickerDialog,
            JourneyResultsDialog, TransitLookupDialog, RouteResultsDialog,
            RendezvousResultsDialog, FindFoodDialog, MeetPointDialog)

def _key_required(parent, title, message, link_label, link_url):
    from dialogs import show_api_key_required
    show_api_key_required(parent, title, message, link_label, link_url)


def _osm_walk_features(
    pts: list,
    overpass_client,
    road_segments: list | None = None,
    walk_leg_index: int | None = None,
) -> list:
    """Query Overpass for real pedestrian features along a walking leg.

    pts: list of {"lat", "lon", "instruction"} walk-point dicts (in route order).
    Returns list of {"instruction": str, "sv_desc": str} context items sorted by
    position along the route.
    """

    def _hav(lat1, lon1, lat2, lon2):
        R = 6_371_000
        p1, p2 = math.radians(lat1), math.radians(lat2)
        dp, dl = p2 - p1, math.radians(lon2 - lon1)
        a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    lats = [p["lat"] for p in pts]
    lons = [p["lon"] for p in pts]
    pad = 0.0007  # ~80 m padding around route bbox
    S, N = min(lats) - pad, max(lats) + pad
    W, E = min(lons) - pad, max(lons) + pad

    query = (
        f"[out:json][timeout:20];\n"
        f"(\n"
        f'  node["highway"="crossing"]({S:.6f},{W:.6f},{N:.6f},{E:.6f});\n'
        f'  node["highway"="traffic_signals"]({S:.6f},{W:.6f},{N:.6f},{E:.6f});\n'
        f'  way["highway"="steps"]({S:.6f},{W:.6f},{N:.6f},{E:.6f});\n'
        f'  nwr["amenity"]({S:.6f},{W:.6f},{N:.6f},{E:.6f});\n'
        f'  nwr["shop"]({S:.6f},{W:.6f},{N:.6f},{E:.6f});\n'
        f'  nwr["tourism"]({S:.6f},{W:.6f},{N:.6f},{E:.6f});\n'
        f'  nwr["office"]({S:.6f},{W:.6f},{N:.6f},{E:.6f});\n'
        f'  nwr["public_transport"]({S:.6f},{W:.6f},{N:.6f},{E:.6f});\n'
        f'  nwr["railway"]({S:.6f},{W:.6f},{N:.6f},{E:.6f});\n'
        f'  nwr["leisure"]({S:.6f},{W:.6f},{N:.6f},{E:.6f});\n'
        f'  nwr["historic"]({S:.6f},{W:.6f},{N:.6f},{E:.6f});\n'
        f'  nwr["healthcare"]({S:.6f},{W:.6f},{N:.6f},{E:.6f});\n'
        f'  nwr["craft"]({S:.6f},{W:.6f},{N:.6f},{E:.6f});\n'
        f'  nwr["man_made"]({S:.6f},{W:.6f},{N:.6f},{E:.6f});\n'
        f");\n"
        f"out tags center geom;\n"
    ).encode()

    result = overpass_client.request(query, timeout=20)
    if not result:
        return []

    FEATURE_MAX_OFF_ROUTE_M = 12.0
    CROSSING_MAX_OFF_ROUTE_M = 20.0
    STEPS_MAX_OFF_ROUTE_M = 4.0
    items = []

    def _nearest_route_projection(lat: float, lon: float) -> tuple[float, int]:
        """Distance from a feature to the walked polyline and its route index."""
        if len(pts) < 2:
            return float("inf"), 0
        best_d = float("inf")
        best_idx = 0
        for i in range(len(pts) - 1):
            a = pts[i]
            b = pts[i + 1]
            snap_lat, snap_lon = nearest_point_on_segment(
                lat, lon, a["lat"], a["lon"], b["lat"], b["lon"])
            d = _hav(lat, lon, snap_lat, snap_lon)
            if d < best_d:
                best_d = d
                # Attach route features to the block being walked toward.
                best_idx = i + 1
        return best_d, best_idx

    def _feature_locations(el: dict) -> list[tuple[float, float]]:
        locs = []
        lat = el.get("lat") or (el.get("center") or {}).get("lat")
        lon = el.get("lon") or (el.get("center") or {}).get("lon")
        if lat is not None and lon is not None:
            locs.append((lat, lon))
        for pt in el.get("geometry") or []:
            glat = pt.get("lat")
            glon = pt.get("lon")
            if glat is not None and glon is not None:
                locs.append((glat, glon))
        return locs


    for el in result.get("elements", []):
        tags = el.get("tags", {})
        elat = el.get("lat") or (el.get("center") or {}).get("lat")
        elon = el.get("lon") or (el.get("center") or {}).get("lon")
        if elat is None or elon is None:
            continue

        projections = [
            _nearest_route_projection(flat, flon)
            for flat, flon in _feature_locations(el)
        ]
        if not projections:
            continue
        route_dist_m, nearest_idx = min(projections, key=lambda item: item[0])

        highway = tags.get("highway", "")
        name = tags.get("name", "")
        feature_kind = (
            tags.get("amenity", "")
            or tags.get("shop", "")
            or tags.get("tourism", "")
            or tags.get("office", "")
            or tags.get("public_transport", "")
            or tags.get("railway", "")
            or tags.get("leisure", "")
            or tags.get("historic", "")
            or tags.get("healthcare", "")
            or tags.get("craft", "")
            or tags.get("man_made", "")
        )
        desc = None
        is_crossing = False

        if highway in ("crossing", "traffic_signals"):
            is_crossing = True
            crossing_type = tags.get("crossing", "")
            tactile = tags.get("tactile_paving", "")
            sound = (tags.get("traffic_signals:sound")
                     or tags.get("crossing:bell")
                     or tags.get("acoustic_signals", ""))

            # The type only when it adds something beyond "pedestrian crossing";
            # the generic case is carried by the "Pedestrian crossing" prefix, so
            # we don't repeat it (no more "Pedestrian crossing: pedestrian crossing").
            parts = []
            if crossing_type == "traffic_signals" or highway == "traffic_signals":
                parts.append("traffic signals")
            elif crossing_type == "zebra":
                parts.append("zebra crossing, give way")
            elif crossing_type == "uncontrolled":
                parts.append("uncontrolled, no signals")

            if tactile == "yes":
                parts.append("tactile paving present")
            elif tactile == "no":
                parts.append("no tactile paving tagged in OSM")

            if sound == "yes":
                parts.append("audible signals tagged")
            elif sound == "no":
                parts.append("no audible signals tagged")

            street_a, street_b = _nearest_road_names(elat, elon, road_segments or [], limit_m=45.0)
            street_bits = [s for s in (street_a, street_b) if s]
            head = ("Pedestrian crossing at " + " and ".join(street_bits[:2])
                    if street_bits else "Pedestrian crossing")
            desc = head + (": " + ", ".join(parts) + "." if parts else ".")

        elif highway == "steps":
            desc = "Steps on the route here — no ramp mentioned in OSM."

        elif name and feature_kind:
            desc = f"Landmark: {name} ({feature_kind}) near the path."
        elif feature_kind in {"platform", "stop_position", "station", "tram_stop", "bus_stop"}:
            desc = f"Transit feature: {feature_kind} near the path."

        if desc:
            if is_crossing:
                # Only crossings essentially ON the walked path — in a dense
                # precinct, nearby side-street crossings can sit just one
                # route node away from the path the walker never uses.
                if route_dist_m > CROSSING_MAX_OFF_ROUTE_M:
                    continue
                items.append({
                    "nearest_idx": nearest_idx,
                    "instruction": pts[nearest_idx]["instruction"],
                    "desc": desc,
                    "lat": elat,
                    "lon": elon,
                    "walk_leg_index": walk_leg_index,
                    "is_crossing": True,
                })
            else:
                max_off_route_m = (STEPS_MAX_OFF_ROUTE_M if highway == "steps"
                                   else FEATURE_MAX_OFF_ROUTE_M)
                if route_dist_m > max_off_route_m:
                    continue
                items.append({
                    "nearest_idx": nearest_idx,
                    "instruction": pts[nearest_idx]["instruction"],
                    "desc": desc,
                    "lat": elat,
                    "lon": elon,
                    "walk_leg_index": walk_leg_index,
                    "is_crossing": False,
                })

    # Sort by position along route
    items.sort(key=lambda x: x["nearest_idx"])
    return [
        {"instruction": item["instruction"], "sv_desc": item["desc"],
         "route_index": item["nearest_idx"]}
        for item in items
    ]


def _road_name_bare(name: str) -> str:
    """Normalize a road label for comparison."""
    return re.sub(r"\s*\(.*?\)", "", name or "").strip().lower()


# POI kinds worth announcing for orientation on a walking route.  Excludes the
# clutter that floods commercial strips — hair/beauty/tattoo/cosmetic, individual
# doctors/dentists/clinics, lawyers/real-estate/offices ("services"), and minor
# specialty shops — while keeping food, major stores, schools, transport, civic
# buildings, pharmacies/hospitals, places of worship, parks and landmarks.
_NOTABLE_POI_KINDS = frozenset({
    "restaurant", "cafe", "bakery", "fast food", "pub", "bar", "food court",
    "ice cream", "supermarket", "convenience", "greengrocer", "butcher",
    "mall", "department store", "shopping centre", "marketplace", "chemist",
    "pharmacy", "hardware", "electronics", "books", "toys", "sports shop",
    "furniture", "music",
    "station", "bus station", "ferry terminal", "tram stop", "airport",
    "school", "university", "college", "library", "post office", "bank",
    "place of worship", "community centre", "police", "fire station",
    "hospital",
    "museum", "gallery", "theatre", "cinema", "arts centre", "events venue",
    "stadium", "park", "garden", "zoo", "theme park", "attraction", "hotel",
    "fuel",
})

_STREET_SUFFIXES = frozenset({
    "road", "rd", "street", "st", "avenue", "ave", "av", "drive", "dr",
    "lane", "ln", "crescent", "cres", "close", "cl", "boulevard", "blvd",
    "highway", "hwy", "terrace", "tce", "parade", "pde", "place", "pl",
    "court", "ct", "grove", "gr", "circuit", "cct", "way", "esplanade",
    "esp", "row", "mews", "track", "walk",
})


def _street_key(name: str) -> str:
    """Comparison key for a street name, tolerant of suffix abbreviations
    (so 'Old Cleveland Rd' matches 'Old Cleveland Road')."""
    s = re.sub(r"\s*\(.*?\)", "", (name or "").strip().lower())
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    words = s.split()
    if words and words[-1] in _STREET_SUFFIXES:
        words = words[:-1]
    return " ".join(words)


def _join_names(names: list) -> str:
    """Join names into a readable 'A, B and C' phrase."""
    names = [n for n in names if n]
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def _side_phrase(side: str) -> str:
    """Parenthetical for which way a cross street runs, relative to travel."""
    if side == "left-right":
        return " (on both sides)"
    if side == "left":
        return " (on the left)"
    if side == "right":
        return " (on the right)"
    return ""


# Compact crossing attributes (keyed by the post-comma-split tokens that
# _osm_walk_features emits).  Negatives ("no tactile paving tagged…") are
# omitted — absence of a tag is not useful and adds noise.
_CROSSING_ATTR = {
    "traffic signals": "signals",
    "tactile paving present": "tactile",
    "audible signals tagged": "audible",
    "zebra crossing": "zebra",
    "uncontrolled": "uncontrolled",
}


def _clean_crossings(descs: list, allowed_bares: set, seen: set = None) -> list:
    """Condense a block's crossings into a single line, plus any steps.

    A short block ends at one intersection, so its crossing nodes are merged
    into one summary ("Pedestrian crossing: signals, tactile") rather than a
    line per arm.  Only crossings involving a street the walker is on/passing
    are kept; *seen* suppresses a named crossing already reported in an earlier
    block; the road being walked is not repeated (the block label names the
    intersection).
    """
    attrs, steps, keys_here, any_crossing = [], [], [], False
    for d in descs:
        d = (d or "").strip()
        if d.startswith("Steps"):
            if d not in steps:
                steps.append(d)
            continue
        if not d.startswith("Pedestrian crossing"):
            continue
        m = re.match(r"Pedestrian crossing(?: at (.+?))?(?::\s*(.*?))?\.?$", d)
        if not m:
            continue
        streets = [s.strip() for s in (m.group(1) or "").split(" and ") if s.strip()]
        bares = {_road_name_bare(s) for s in streets if s}
        if allowed_bares and bares and not (bares & allowed_bares):
            continue
        key = frozenset(bares) if bares else None
        if key is not None and seen is not None and key in seen:
            continue
        any_crossing = True
        if key is not None:
            keys_here.append(key)
        for p in (m.group(2) or "").split(","):
            short = _CROSSING_ATTR.get(p.strip())
            if short and short not in attrs:
                attrs.append(short)
    if seen is not None:
        seen.update(keys_here)
    lines = []
    if any_crossing:
        lines.append("Pedestrian crossing" + (": " + ", ".join(attrs) + "." if attrs else "."))
    return lines + steps




def _nearest_road_names(lat: float, lon: float, road_segments: list, limit_m: float = 35.0) -> tuple[str, str]:
    """Return the two closest named roads near a point, if available.

    This is a best-effort helper for crossing names. It relies on the local
    loaded street cache, so it only produces names when that data is already
    available.
    """
    scores: dict[str, float] = {}
    for seg in road_segments or []:
        raw = seg.get("name", "")
        name = re.sub(r"\s*\(.*?\)", "", raw).strip()
        if not name:
            continue
        if name.lower() in GENERIC_STREET_TYPES:
            continue
        coords = seg.get("coords", [])
        if len(coords) < 2:
            continue
        best = None
        for i in range(len(coords) - 1):
            alat, alon = coords[i]
            blat, blon = coords[i + 1]
            snap_lat, snap_lon = nearest_point_on_segment(lat, lon, alat, alon, blat, blon)
            d = dist_metres(lat, lon, snap_lat, snap_lon)
            if best is None or d < best:
                best = d
        if best is None:
            continue
        prev = scores.get(name)
        if prev is None or best < prev:
            scores[name] = best

    if not scores:
        return "", ""

    ranked = sorted(scores.items(), key=lambda item: item[1])
    if ranked[0][1] > limit_m:
        return "", ""

    first = ranked[0][0]
    second = ""
    for name, d in ranked[1:]:
        if d <= limit_m and _road_name_bare(name) != _road_name_bare(first):
            second = name
            break
    return first, second


class ToolsMixin:
    def _tool_trace(self, msg: str) -> None:
        """Write a verbose trace when diagnostics are enabled."""
        try:
            settings = getattr(self, "settings", None) or {}
            if settings.get("logging", {}).get("verbose", False):
                miab_log("verbose", msg, settings)
        except Exception:
            pass

    def _announce_thinking(self) -> None:
        """Play a brief working sound while a tool runs, instead of speaking a
        'Thinking...' message that would interrupt the screen reader."""
        if not getattr(self, "_thinking_beep_active", False):
            return
        try:
            self._play_system_sound("balloon")
        except Exception:
            pass

    def _warn_optional_key(self, tool_name: str, key_name: str, limitation: str) -> None:
        """Announce that a tool can continue, but with reduced coverage."""
        suppress_key = f"suppress_warn_{key_name.lower()}"
        if self.settings.get(suppress_key, False):
            return
        from dialogs import show_optional_key_warning
        suppressed = show_optional_key_warning(
            self,
            f"{tool_name} Warning",
            f"Warning: {key_name} API key not detected.\n\n"
            f"{tool_name} will still work, but {limitation}",
        )
        if suppressed:
            from core import save_settings
            self.settings[suppress_key] = True
            save_settings(self.settings)

    def _location_picker_choices(self):
        """Build the shared location-choice list used by route tools."""
        marks = getattr(self, "_map_marks", {})
        mark_list = [(slot, m) for slot in (1, 2, 3)
                     if (m := marks.get(slot))]
        choices = ["Type an address..."]
        actions = ["address"]
        for slot, m in mark_list:
            choices.append(f"Mark {slot}: {m.get('name', 'current position')}")
            actions.append(("mark", slot))
        choices.append("Choose a favourite...")
        actions.append("favourite")
        choices.append(f"Current position ({self._current_map_place()[1]})")
        actions.append("current")
        return choices, actions, mark_list

    def _pick_location(self, prompt: str, purpose: str, rt, country_code: str):
        """Return (lat, lon, name) from a mark, favourite, current position, or typed address.

        The chooser always offers a small set of explicit options, then falls
        back to text input when needed.
        """
        def _pick_from_favourites():
            from favourites import load_favourites, favourite_label
            entries = load_favourites()
            if not entries:
                tool_name = getattr(self, "_active_tool_display_name", "Tool")
                self._tool_cancel_already_announced = True
                self._announce_transient_then_return(
                    f"No favourites saved. {tool_name} cancelled.")
                return None
            entries = sorted(entries, key=lambda e: str(e.get("name", "")).lower())
            labels = [favourite_label(entry, self.lat, self.lon) for entry in entries]
            from dialogs import ChoiceDialog
            dlg = ChoiceDialog(self, prompt, "Choose a favourite", labels)
            if dlg.ShowModal() != wx.ID_OK:
                dlg.Destroy()
                return None
            idx = dlg.GetSelection()
            dlg.Destroy()
            if idx == wx.NOT_FOUND or idx < 0 or idx >= len(entries):
                return None
            entry = entries[idx]
            try:
                return (float(entry["lat"]), float(entry["lon"]), entry.get("name", "Favourite"))
            except Exception:
                self._status_update("Favourite has no valid position.", force=True)
                return None

        choices, choice_actions, mark_list = self._location_picker_choices()

        from dialogs import ChoiceDialog
        dlg = ChoiceDialog(self, prompt, purpose.title(), choices)
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            return None
        idx = dlg.GetSelection()
        dlg.Destroy()
        if idx == wx.NOT_FOUND or idx < 0 or idx >= len(choice_actions):
            return None
        choice = choice_actions[idx]
        if choice == "current":
            coords, name = self._current_map_place()
            return coords[0], coords[1], name
        if isinstance(choice, tuple) and choice[0] == "mark":
            _, slot = choice
            _, mark = next(((s, m) for s, m in mark_list if s == slot), (None, None))
            if mark:
                lat, lon = mark["coords"]
                return (lat, lon, mark.get("name", f"mark {slot}"))
            return None
        if choice == "favourite":
            fav = _pick_from_favourites()
            if fav is not None:
                return fav
            return None
        # "Type an address..." selected — fall through to text input

        dlg = self._dlgs[1](self, prompt, title=purpose.title())
        if dlg.ShowModal() != wx.ID_OK or not dlg.GetValue():
            dlg.Destroy()
            return None
        text = dlg.GetValue()
        dlg.Destroy()
        try:
            return self._resolve_geocode(rt, text, country_code, purpose)
        except Exception as exc:
            self._status_update(f"Could not find '{text}': {exc}", force=True)
            return None

    def _thinking(self, msg: str = "Thinking...", play_busy_sound: bool = True) -> None:
        """Announce that a longer tool action is still running."""
        self._thinking_active = True
        self._thinking_beep_active = bool(play_busy_sound)
        self._suppress_location_restore = True
        suppress = getattr(self, "_suppress_map_focus_repeat", None)
        if callable(suppress):
            suppress(5000)
        if not play_busy_sound:
            return
        try:
            wx.CallLater(75, self._announce_thinking)
        except Exception:
            self._announce_thinking()

    def _begin_tools_workflow(self) -> None:
        """Mark the Tools UI as active so focus returns stay quiet."""
        self._tools_workflow_active = True
        suppress = getattr(self, "_suppress_map_focus_repeat", None)
        if callable(suppress):
            suppress(5000)

    def _end_tools_workflow(self) -> None:
        """Clear the Tools UI guard after controls have settled."""
        self._tools_workflow_active = False
        suppress = getattr(self, "_suppress_map_focus_repeat", None)
        if callable(suppress):
            suppress(800)

    def _finish_thinking(self) -> None:
        """Clear the thinking state and resume the normal location sound."""
        self._thinking_active = False
        self._thinking_beep_active = False
        self._suppress_location_restore = False
        if getattr(self, "_tools_workflow_active", False):
            self._end_tools_workflow()
        if getattr(self, "_tools_sound_was_on", True):
            self._resume_location_sound()
        self._tools_sound_was_on = False

    @property
    def _dlgs(self):
        """Lazy-load dialog classes to avoid circular imports."""
        if not hasattr(self, '_dialogs_cache'):
            try:
                self._dialogs_cache = _get_dialogs()
            except Exception as exc:
                import wx
                wx.MessageBox(
                    f"Failed to load tools dialogs:\n\n{exc}",
                    "Tools Error", wx.OK | wx.ICON_ERROR)
                self._dialogs_cache = None
        return self._dialogs_cache

    def _open_tools_menu(self):
        """F12 — open the tools menu dialog."""
        self._begin_tools_workflow()
        self._tools_sound_was_on = bool(getattr(self.sound, "_current", None))
        self.sound.stop()
        if self._dlgs is None:
            self._restore_tools_sound()
            return
        selected_tool = ""
        try:
            ToolsMenuDialog = self._dlgs[0]
            dlg = ToolsMenuDialog(self)
            if dlg.ShowModal() == wx.ID_OK:
                selected_tool = dlg.selected_tool
                self._active_tool_display_name = next(
                    (label for label, key in ToolsMenuDialog.TOOLS
                     if key == selected_tool),
                    "Tool",
                )
                dlg.Destroy()
                if selected_tool == "detour_calculator":
                    self._tool_detour_calculator()
                elif selected_tool == "route_explorer":
                    self._tool_route_explorer()
                elif selected_tool == "rendezvous_point":
                    self._tool_rendezvous_point()
                elif selected_tool == "toll_compare":
                    self._tool_toll_compare()
                elif selected_tool == "journey_planner":
                    self._tool_journey_planner()
                elif selected_tool == "airport_amenity_guide":
                    self._tool_airport_amenity_guide()
                elif selected_tool == "departure_board":
                    self._tool_departure_board()
                elif selected_tool == "flight_search":
                    self._tool_flight_search()
                elif selected_tool == "virgin_booking":
                    self._tool_virgin_australia_booking()
                elif selected_tool == "hotel_search":
                    self._tool_hotel_search()
                elif selected_tool == "find_food":
                    self._tool_find_food()
                else:
                    self._restore_tools_sound()
            else:
                dlg.Destroy()
                self._restore_tools_sound()
        except Exception as exc:
            import wx as _wx
            _wx.MessageBox(f"Tools menu error:\n\n{exc}", "Error", _wx.OK | _wx.ICON_ERROR)
            self._restore_tools_sound()
        finally:
            if not (getattr(self, "_thinking_active", False)
                    or getattr(self, "_find_food_populating", False)):
                # Individual tools historically had to restore the ambient
                # sound on every cancel/error/early-return branch.  Keep their
                # cleanup, but provide one reliable dispatcher-level fallback.
                if getattr(self, "_tools_sound_was_on", False):
                    self._restore_tools_sound()
                else:
                    self._end_tools_workflow()
                if not selected_tool:
                    # Cancelled with no tool chosen: re-announce the current
                    # location like F2. Clear the quiet-window _end_tools_workflow
                    # just set, otherwise the repeat would be suppressed.
                    self._suppress_focus_repeat_until = 0.0
                    self._return_focus_to_map(repeat=True, delay_ms=250)
                else:
                    focus_map = getattr(self, "_focus_map_window_silently", None)
                    if callable(focus_map):
                        wx.CallAfter(focus_map)
                    else:
                        wx.CallAfter(self.listbox.SetFocus)
            self._active_tool_display_name = ""

    def _restore_tools_sound(self):
        """Restore the pre-F12 sound state after leaving the tools menu."""
        self._finish_thinking()
        self._end_tools_workflow()

    def _get_route_tools(self) -> "RouteTools | None":
        """Return a configured RouteTools instance, or None."""
        api_key = self.settings.get("google_api_key", "").strip()
        return RouteTools(api_key)

    @staticmethod
    def _country_name_to_code(country_name: str) -> str:
        """Map common country names to ISO-style codes used by geocoders."""
        _CODES = {
            "australia": "AU", "united states": "US", "usa": "US",
            "united kingdom": "UK", "uk": "UK", "canada": "CA",
            "new zealand": "NZ", "germany": "DE", "france": "FR",
            "japan": "JP", "china": "CN", "india": "IN",
            "brazil": "BR", "south africa": "ZA", "ireland": "IE",
            "singapore": "SG", "malaysia": "MY", "indonesia": "ID",
            "philippines": "PH", "thailand": "TH", "vietnam": "VN",
        }
        code = _CODES.get(country_name.lower().strip(), "")
        if not code and len(country_name) == 2:
            code = country_name.upper()
        return code

    def _ask_country_code(self) -> str:
        """Use the current country when possible, otherwise ask the user."""
        current_country = (getattr(self, "last_country_found", "") or "").strip()
        current_code = (getattr(self, "_current_country_code", "") or "").strip()
        if current_country and current_country.lower() != "open water":
            if current_code:
                return current_code
            code = self._country_name_to_code(current_country)
            if code:
                return code

        dlg = self._dlgs[1](self, "Country (e.g. Australia):", default=current_country)
        if dlg.ShowModal() != wx.ID_OK or not dlg.GetValue():
            dlg.Destroy()
            return ""
        country_name = dlg.GetValue()
        dlg.Destroy()
        return self._country_name_to_code(country_name)

    def _resolve_geocode(self, rt, value: str, country_code: str, purpose: str = "location",
                          require_confirmation: bool = True):
        """Resolve an address and require the user to confirm the match —
        same pattern as the "Online Jump Results" dialog: geocoding can
        return a loose/best-guess match even for nonsense input, so the
        resolved place name is always shown back for explicit confirmation
        rather than silently accepted.

        require_confirmation=False skips the dialog and auto-accepts a
        single candidate — for internal/background lookups (e.g. picking a
        GTFS feed by destination proximity) where there's no interactive
        user prompt to confirm against and showing a dialog would be
        either a threading violation or an unwanted interruption."""
        try:
            candidates = rt.geocode_candidates(value, country_code, limit=8)
        except Exception as exc:
            self._tool_trace(f"Geocode candidate lookup failed for {value!r}: {exc}")
            candidates = []

        if not candidates:
            raise RuntimeError(f"Could not find '{value}'.")

        if not require_confirmation and len(candidates) == 1:
            return candidates[0]

        labels = []
        seen = set()
        for lat, lon, formatted in candidates:
            label = (formatted or value or "").strip() or value
            key = label.lower()
            if key in seen:
                label = f"{label} ({lat:.5f}, {lon:.5f})"
            seen.add(key)
            labels.append(label)

        self._tool_trace(
            f"Geocode for {value!r}: offering {len(labels)} choice(s) for confirmation."
        )
        from dialogs import ChoiceDialog
        prompt = ("Confirm this is the right place:" if len(labels) == 1
                  else "Multiple matches — choose the right one:")
        dlg = ChoiceDialog(self, prompt, f"Confirm {purpose}", labels)
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            return None
        sel = dlg.GetSelection()
        dlg.Destroy()
        if sel == wx.NOT_FOUND or sel >= len(candidates):
            return None
        return candidates[sel]

    def _tool_detour_calculator(self):
        """Detour Calculator — compare a trip with stop-offs vs going direct."""
        rt = self._get_route_tools()
        if not rt:
            self._resume_location_sound()
            return
        if not rt.is_configured:
            self._warn_optional_key(
                "Detour Calculator",
                "Google",
                "it will use open geocoding and OSRM routing instead of Google Maps, "
                "so coverage and turn-by-turn detail may be a little different.",
            )

        country_code = self._ask_country_code()
        if not country_code:
            self._status_update("Detour calculator cancelled.", force=True)
            return

        def _geocode_text(prompt_text):
            """Text-only geocode: show dialog, return (lat, lon, name), None, or 'retry'."""
            dlg = self._dlgs[1](self, prompt_text)
            if dlg.ShowModal() != wx.ID_OK or not dlg.GetValue():
                dlg.Destroy()
                return None
            value = dlg.GetValue()
            dlg.Destroy()
            self._status_update(f"Looking up {value}...")
            try:
                resolved = self._resolve_geocode(rt, value, country_code, "location")
                if resolved is None:
                    return None
                lat, lon, formatted = resolved
                self._status_update(f"Found: {formatted}", force=True)
                return (lat, lon, formatted)
            except Exception as e:
                self._status_update(f"Could not find '{value}': {e}", force=True)
                return "retry"

        # 1. Start — marks, favourite, current position, or typed address
        start = self._pick_location("Starting point (address, suburb or city):", "starting point", rt, country_code)
        if start is None:
            self._status_update("Detour calculator cancelled.", force=True)
            return

        # 2. Stop-off — mandatory (at least one), always typed
        while True:
            result = _geocode_text("Stop-off (address, suburb or city):")
            if result is None:
                self._status_update("Detour calculator cancelled.", force=True)
                return
            if result != "retry":
                first_stop = result
                break

        # 3. Destination — marks or typed address
        destination = self._pick_location("Destination (address, suburb or city):", "destination", rt, country_code)
        if destination is None:
            self._status_update("Detour calculator cancelled.", force=True)
            return

        # Build stops list: start, stop-offs..., destination
        stops = [start, first_stop]

        # 4. Optional additional stop-offs — always typed
        while True:
            result = _geocode_text("Additional stop-off (or leave blank to finish):")
            if result is None:
                break  # blank or cancel — done adding stops
            if result == "retry":
                continue
            stops.append(result)

        stops.append(destination)

        # Run comparison in background
        self._thinking()
        def _calc():
            try:
                result = rt.compare_routes(stops)
                wx.CallAfter(self._show_route_results,
                             "Detour Calculator", result["summary_text"])
            except Exception as e:
                wx.CallAfter(self._status_update, f"Detour calculation failed: {e}", True)
                wx.CallAfter(self._finish_thinking)

        threading.Thread(target=_calc, daemon=True).start()

    def _tool_route_explorer(self):
        """Suburb Lister — compare alternative routes with suburbs and tolls."""
        rt = self._get_route_tools()
        if not rt:
            self._resume_location_sound()
            return

        country_code = self._ask_country_code()
        if not country_code:
            self._status_update("Suburb lister cancelled.", force=True)
            self._finish_thinking()
            return

        origin = self._pick_location(
            "Starting point (address, suburb or city):", "starting point", rt, country_code)
        if origin is None:
            self._status_update("Suburb lister cancelled.", force=True)
            self._finish_thinking()
            return
        o_lat, o_lon, o_name = origin

        dest = self._pick_location(
            "Destination (address, suburb or city):", "destination", rt, country_code)
        if dest is None:
            self._status_update("Suburb lister cancelled.", force=True)
            self._finish_thinking()
            return
        d_lat, d_lon, d_name = dest

        self._thinking()
        self._tool_trace("Suburb Lister: route analysis started.")

        # Run exploration in background
        def _status(msg):
            wx.CallAfter(self._status_update, msg)

        def _calc():
            try:
                result = rt.explore_routes(
                    (o_lat, o_lon, o_name),
                    (d_lat, d_lon, d_name),
                    status_cb=_status,
                )
                self._tool_trace("Suburb Lister: route analysis complete.")
                self._thinking_beep_active = False
                wx.CallAfter(self._show_route_results,
                             "Suburb Lister", result["summary_text"])
            except Exception as e:
                self._tool_trace(f"Suburb Lister failed: {e}")
                wx.CallAfter(self._status_update, f"Suburb lister failed: {e}", True)
                wx.CallAfter(self._finish_thinking)

        threading.Thread(target=_calc, daemon=True).start()

    def _tool_rendezvous_point(self):
        """Rendezvous Point — suggest a dropoff point along the route to Destination A."""
        rt = self._get_route_tools()
        if not rt:
            self._resume_location_sound()
            return
        if self._dlgs is None:
            self._resume_location_sound()
            return

        dlg = self._dlgs[8](self)
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            self._status_update("Rendezvous point cancelled.", force=True)
            self._finish_thinking()
            return

        origin_text, dest_a_text, dest_b_text, mode = dlg.GetValues()
        dlg.Destroy()

        if not origin_text or not dest_a_text:
            self._status_update("Rendezvous point cancelled.", force=True)
            self._finish_thinking()
            return

        country_code = self._ask_country_code()
        if not country_code:
            self._status_update("Rendezvous point cancelled.", force=True)
            self._finish_thinking()
            return

        self._tool_trace("Rendezvous Point: route analysis started.")

        self._tool_trace(f"Rendezvous Point: geocoding origin {origin_text!r}.")
        try:
            resolved = self._resolve_geocode(rt, origin_text, country_code, "starting point")
            if resolved is None:
                self._status_update("Rendezvous point cancelled.", force=True)
                self._finish_thinking()
                return
            o_lat, o_lon, o_name = resolved
        except Exception as e:
            self._status_update(f"Could not find '{origin_text}': {e}", force=True)
            self._finish_thinking()
            return

        self._tool_trace(f"Rendezvous Point: geocoding destination A {dest_a_text!r}.")
        try:
            resolved = self._resolve_geocode(rt, dest_a_text, country_code, "destination")
            if resolved is None:
                self._status_update("Rendezvous point cancelled.", force=True)
                self._finish_thinking()
                return
            a_lat, a_lon, a_name = resolved
        except Exception as e:
            self._status_update(f"Could not find '{dest_a_text}': {e}", force=True)
            self._finish_thinking()
            return

        b_info = None
        if dest_b_text:
            self._tool_trace(f"Rendezvous Point: geocoding destination B {dest_b_text!r}.")
            try:
                purpose = "shared destination" if mode == "pickup" else "friend's destination" if mode == "dropoff" else "meeting spot"
                resolved = self._resolve_geocode(rt, dest_b_text, country_code, purpose)
                if resolved is None:
                    self._status_update("Rendezvous point cancelled.", force=True)
                    self._finish_thinking()
                    return
                b_lat, b_lon, b_name = resolved
                b_info = (b_lat, b_lon, b_name)
            except Exception as e:
                self._status_update(f"Could not find '{dest_b_text}': {e}", force=True)
                self._finish_thinking()
                return

        if mode in ("pickup", "dropoff") and not b_info:
            msg = "Please enter the shared destination." if mode == "pickup" else "Please enter your friend's destination."
            self._status_update(msg, force=True)
            self._finish_thinking()
            return

        self._thinking(play_busy_sound=False)

        def _route_candidates(points):
            if len(points) < 2:
                return []
            total_m = 0.0
            seg_lengths = []
            for i in range(len(points) - 1):
                seg_m = _haversine_m(
                    points[i][0], points[i][1],
                    points[i + 1][0], points[i + 1][1],
                )
                seg_lengths.append(seg_m)
                total_m += seg_m

            if total_m <= 0:
                return [(0.0, points[0][0], points[0][1])]

            if total_m < 10_000:
                interval_m = 250.0
            elif total_m < 50_000:
                interval_m = 500.0
            elif total_m < 150_000:
                interval_m = 1000.0
            else:
                interval_m = 1500.0

            candidates = [(0.0, points[0][0], points[0][1])]
            cum_dist = 0.0
            next_sample = interval_m
            for i, seg_m in enumerate(seg_lengths):
                seg_start = cum_dist
                cum_dist += seg_m
                while next_sample <= cum_dist:
                    frac = (next_sample - seg_start) / seg_m if seg_m > 0 else 0.0
                    lat = points[i][0] + frac * (points[i + 1][0] - points[i][0])
                    lon = points[i][1] + frac * (points[i + 1][1] - points[i][1])
                    candidates.append((next_sample, lat, lon))
                    next_sample += interval_m

            if candidates[-1][0] != total_m:
                candidates.append((total_m, points[-1][0], points[-1][1]))
            return candidates

        def _nearest_sample_distance(lat, lon, samples):
            if not samples:
                return float("inf")
            return min(
                _haversine_m(lat, lon, sample["lat"], sample["lon"])
                for sample in samples
            )

        def _label_point(lat, lon):
            suburb = ""
            try:
                suburb = rt._reverse_geocode_suburb(lat, lon) or ""
            except Exception:
                suburb = ""
            if suburb:
                return suburb
            return f"{lat:.5f}, {lon:.5f}"

        def _calc():
            try:
                if mode == "pickup":
                    # Walk the friend's route; score by (friend's drive to pickup) +
                    # (straight-line from user's location to pickup).  No user route needed.
                    friend_route = rt._compute_route(
                        (a_lat, a_lon),
                        (b_info[0], b_info[1]),
                        request_polyline=True,
                    )
                    friend_polyline = friend_route.get("polyline", "")
                    if not friend_polyline:
                        raise RuntimeError("No route polyline available.")
                    friend_points = _decode_polyline(friend_polyline)
                    friend_samples = [
                        {"source": "friend", "progress_m": p, "lat": lat, "lon": lon}
                        for p, lat, lon in _route_candidates(friend_points)
                    ]
                    if not friend_samples:
                        raise RuntimeError("No candidate points found on the route.")
                    user_samples = []
                    user_total_m = 0.0
                    route = friend_route
                    candidate_sets = friend_samples
                    total_m = max(friend_route.get("distance_m", 0.0), friend_samples[-1]["progress_m"])
                elif mode == "dropoff":
                    # shared starting point → your destination / friend's destination
                    user_route = rt._compute_route(
                        (o_lat, o_lon),
                        (a_lat, a_lon),
                        request_polyline=True,
                    )
                    friend_route = rt._compute_route(
                        (o_lat, o_lon),
                        (b_info[0], b_info[1]),
                        request_polyline=True,
                    )
                    user_polyline = user_route.get("polyline", "")
                    friend_polyline = friend_route.get("polyline", "")
                    if not user_polyline or not friend_polyline:
                        raise RuntimeError("No route polyline available.")
                    user_points = _decode_polyline(user_polyline)
                    friend_points = _decode_polyline(friend_polyline)
                    user_samples = [
                        {"source": "you", "progress_m": p, "lat": lat, "lon": lon}
                        for p, lat, lon in _route_candidates(user_points)
                    ]
                    friend_samples = [
                        {"source": "friend", "progress_m": p, "lat": lat, "lon": lon}
                        for p, lat, lon in _route_candidates(friend_points)
                    ]
                    if not user_samples or not friend_samples:
                        raise RuntimeError("No candidate points found on the routes.")
                    user_total_m = max(user_route.get("distance_m", 0.0), user_samples[-1]["progress_m"])
                    route = friend_route
                    candidate_sets = friend_samples + user_samples
                    total_m = max(friend_route.get("distance_m", 0.0), friend_samples[-1]["progress_m"])
                elif mode == "meeting":
                    # Route directly between the two people; midpoint by road = fairest meeting place
                    route = rt._compute_route(
                        (o_lat, o_lon),
                        (a_lat, a_lon),
                        request_polyline=True,
                    )
                    polyline = route.get("polyline", "")
                    if not polyline:
                        raise RuntimeError("No route polyline available.")
                    points = _decode_polyline(polyline)
                    candidate_sets = []
                    for progress_m, lat, lon in _route_candidates(points):
                        candidate_sets.append({
                            "source": "shared",
                            "progress_m": progress_m,
                            "lat": lat,
                            "lon": lon,
                        })
                    if not candidate_sets:
                        raise RuntimeError("No candidate points found on the route.")
                    total_m = candidate_sets[-1]["progress_m"]
                    midpoint = total_m / 2.0

                def _score(candidate):
                    progress_m = candidate["progress_m"]
                    lat = candidate["lat"]
                    lon = candidate["lon"]
                    if mode == "pickup":
                        # minimise: friend's drive to pickup + user's straight-line travel to pickup.
                        # early points near the user score best; late points far from the user score worst.
                        user_to_pickup_m = _haversine_m(lat, lon, o_lat, o_lon)
                        return (progress_m + user_to_pickup_m, progress_m, user_to_pickup_m)
                    if mode == "dropoff":
                        # minimise: user's remaining journey + heavily weighted friend detour.
                        # friend detour is weighted 4x — a 5 km detour for the friend costs as
                        # much as 20 km of remaining journey for the user.
                        nearest_user = min(
                            user_samples,
                            key=lambda s: _haversine_m(lat, lon, s["lat"], s["lon"]),
                        )
                        remaining_user_m = max(user_total_m - nearest_user["progress_m"], 0.0)
                        friend_detour_m = _nearest_sample_distance(lat, lon, friend_samples)
                        return (remaining_user_m + 4 * friend_detour_m, remaining_user_m, friend_detour_m, progress_m)
                    # meeting: score by road distance from the true halfway point
                    return (abs(progress_m - midpoint), progress_m)

                ranked = sorted(candidate_sets, key=_score)
                selected = []
                min_spacing_m = max(250.0, total_m * 0.03)
                for candidate in ranked:
                    progress_m = candidate["progress_m"]
                    lat = candidate["lat"]
                    lon = candidate["lon"]
                    # skip candidates within 5 km of the shared destination
                    if mode == "pickup" and _haversine_m(lat, lon, b_info[0], b_info[1]) < 5000:
                        continue
                    # skip dropoff candidates where the user is essentially already at their destination
                    if mode == "dropoff":
                        nu = min(user_samples, key=lambda s: _haversine_m(lat, lon, s["lat"], s["lon"]))
                        if max(user_total_m - nu["progress_m"], 0.0) < 2000:
                            continue
                    # dropoff pools two routes so deduplicate spatially; others use progress spacing
                    if mode == "dropoff":
                        if any(_haversine_m(lat, lon, item["lat"], item["lon"]) < min_spacing_m for item in selected):
                            continue
                    elif any(abs(progress_m - item["progress_m"]) < min_spacing_m for item in selected):
                        continue
                    selected.append({
                        "progress_m": progress_m,
                        "lat": lat,
                        "lon": lon,
                        "source": candidate.get("source", "shared"),
                    })
                    if len(selected) >= 6:
                        break
                if not selected:
                    for candidate in ranked[:5]:
                        selected.append({
                            "progress_m": candidate["progress_m"],
                            "lat": candidate["lat"],
                            "lon": candidate["lon"],
                            "source": candidate.get("source", "shared"),
                        })

                result_rows = []
                for idx, item in enumerate(selected, start=1):
                    progress_m = item["progress_m"]
                    lat = item["lat"]
                    lon = item["lon"]
                    label = _label_point(lat, lon)
                    if label == f"{lat:.5f}, {lon:.5f}":
                        label = f"{label} on the route"
                    summary = f"{idx}. {label}"
                    if mode == "meeting":
                        from_friend_m = progress_m
                        from_you_m = max(total_m - progress_m, 0.0)
                        offset_m = abs(progress_m - midpoint)
                        detail = "\n".join([
                            f"{_fmt_distance(int(round(from_friend_m)))} from your friend.",
                            f"{_fmt_distance(int(round(from_you_m)))} from you.",
                            f"{_fmt_distance(int(round(offset_m)))} from the exact midpoint.",
                        ])
                    elif mode == "pickup":
                        user_to_pickup_m = _haversine_m(lat, lon, o_lat, o_lon)
                        remaining_m = max(total_m - progress_m, 0.0)
                        detail = "\n".join([
                            f"~{_fmt_distance(int(round(user_to_pickup_m)))} from your location.",
                            f"{_fmt_distance(int(round(progress_m)))} from your friend's location.",
                            f"{_fmt_distance(int(round(remaining_m)))} shared journey to destination.",
                        ])
                    elif mode == "dropoff":
                        nearest_user = min(
                            user_samples,
                            key=lambda s: _haversine_m(lat, lon, s["lat"], s["lon"]),
                        )
                        remaining_user_m = max(user_total_m - nearest_user["progress_m"], 0.0)
                        carried_m = user_total_m - remaining_user_m
                        friend_detour_m = _nearest_sample_distance(lat, lon, friend_samples)
                        nearest_friend = min(
                            friend_samples,
                            key=lambda s: _haversine_m(lat, lon, s["lat"], s["lon"]),
                        )
                        friend_progress_m = nearest_friend["progress_m"]
                        friend_total_m = max(total_m, friend_samples[-1]["progress_m"])
                        friend_remaining_m = max(friend_total_m - friend_progress_m, 0.0)
                        on_friend_route = friend_detour_m < 500
                        detour_line = (
                            "No detour for your friend."
                            if on_friend_route
                            else f"Detour for your friend: ~{_fmt_distance(int(round(friend_detour_m)))}."
                        )
                        detail = "\n".join([
                            f"Friend carries you ~{_fmt_distance(int(round(carried_m)))} of your journey.",
                            f"~{_fmt_distance(int(round(remaining_user_m)))} still to your destination.",
                            f"Your friend has ~{_fmt_distance(int(round(friend_remaining_m)))} left after dropoff.",
                            detour_line,
                        ])
                    else:
                        offset_m = abs(progress_m - midpoint)
                        detail = f"{_fmt_distance(int(round(offset_m)))} from the midpoint."
                    result_rows.append({
                        "summary": summary,
                        "detail_text": detail,
                        "_label_key": label.lower().strip(),
                    })

                if selected:
                    best = selected[0]
                    if mode == "meeting":
                        best_from_friend = best["progress_m"]
                        best_from_you = max(total_m - best["progress_m"], 0.0)
                        trace_bits = [
                            f"{_fmt_distance(int(round(best_from_friend)))} from friend by road",
                            f"{_fmt_distance(int(round(best_from_you)))} from you by road",
                        ]
                    elif mode == "pickup":
                        best_user_to_pickup = _haversine_m(best["lat"], best["lon"], o_lat, o_lon)
                        best_remaining = max(total_m - best["progress_m"], 0.0)
                        trace_bits = [
                            f"~{_fmt_distance(int(round(best_user_to_pickup)))} from your location",
                            f"{_fmt_distance(int(round(best['progress_m'])))} from your friend's location",
                            f"{_fmt_distance(int(round(best_remaining)))} shared to destination",
                        ]
                    elif mode == "dropoff":
                        best_nearest_user = min(
                            user_samples,
                            key=lambda s: _haversine_m(best["lat"], best["lon"], s["lat"], s["lon"]),
                        )
                        best_remaining_user = max(user_total_m - best_nearest_user["progress_m"], 0.0)
                        best_friend_detour = _nearest_sample_distance(best["lat"], best["lon"], friend_samples)
                        best_carried = user_total_m - best_remaining_user
                        trace_bits = [
                            f"carries you ~{_fmt_distance(int(round(best_carried)))}",
                            f"~{_fmt_distance(int(round(best_remaining_user)))} still to your destination",
                            ("on friend's route" if best_friend_detour < 500
                             else f"~{_fmt_distance(int(round(best_friend_detour)))} friend detour"),
                        ]
                    else:
                        best_remaining = max(total_m - best["progress_m"], 0.0)
                        trace_bits = [f"{_fmt_distance(int(round(best_remaining)))} from the midpoint"]
                    self._tool_trace(
                        "Rendezvous Point: top candidate "
                        f"{best['lat']:.5f},{best['lon']:.5f} (" + ", ".join(trace_bits) + ")."
                    )

                deduped_rows = []
                seen_labels = set()
                for row in result_rows:
                    key = row.get("_label_key", "")
                    if key in seen_labels:
                        continue
                    seen_labels.add(key)
                    row.pop("_label_key", None)
                    deduped_rows.append(row)
                result_rows = deduped_rows

                for idx, row in enumerate(result_rows, start=1):
                    row["summary"] = re.sub(r"^\d+\.\s+", f"{idx}. ", row["summary"])
                    row["detail_text"] = re.sub(
                        r"^Option\s+\d+\s+of\s+\d+",
                        f"Option {idx} of {len(result_rows)}",
                        row["detail_text"],
                        count=1,
                    )

                if mode == "meeting":
                    intro = (
                        "The best meeting places are first. Browse down for less favorable choices."
                    )
                elif mode == "pickup":
                    intro = (
                        "The best pick-up points are first. Browse down for less favorable choices."
                    )
                elif mode == "dropoff":
                    intro = (
                        "The best dropoff points are first. Browse down for less favorable choices."
                    )
                else:
                    intro = (
                        "The best midpoint options are first. Browse down for less favorable choices."
                    )

                wx.CallAfter(
                    self._show_rendezvous_results,
                    o_name,
                    a_name,
                    b_info[2] if b_info else "",
                    route['duration_text'],
                    route['distance_text'],
                    mode,
                    result_rows,
                    intro,
                )
                self._tool_trace(
                    f"Rendezvous Point: route shortlist ready with {len(result_rows)} candidates."
                )
                self._thinking_beep_active = False
            except Exception as e:
                self._tool_trace(f"Rendezvous Point failed: {e}")
                wx.CallAfter(self._status_update, f"Rendezvous point failed: {e}", True)
                wx.CallAfter(self._finish_thinking)

        from route_tools import _decode_polyline, _fmt_distance, _haversine_m

        threading.Thread(target=_calc, daemon=True).start()

    def _tool_toll_compare(self):
        """Toll Compare — toll vs toll-free for the same corridor."""
        rt = self._get_route_tools()
        if not rt:
            self._resume_location_sound()
            return
        if not rt.is_configured:
            self._warn_optional_key(
                "Toll Compare",
                "Google",
                "it will use open geocoding and OSRM routing instead of Google Maps, "
                "so toll pricing may be unavailable and the comparison will be simpler.",
            )

        country_code = self._ask_country_code()
        if not country_code:
            self._status_update("Toll compare cancelled.", force=True)
            return

        origin = self._pick_location(
            "Starting point (address, suburb or city):",
            "starting point", rt, country_code)
        if origin is None:
            self._status_update("Toll compare cancelled.", force=True)
            return
        o_lat, o_lon, o_name = origin

        destination = self._pick_location(
            "Destination (address, suburb or city):",
            "destination", rt, country_code)
        if destination is None:
            self._status_update("Toll compare cancelled.", force=True)
            return
        d_lat, d_lon, d_name = destination

        self._thinking()
        def _calc():
            try:
                result = rt.compare_tolls(
                    (o_lat, o_lon, o_name),
                    (d_lat, d_lon, d_name),
                )
                wx.CallAfter(self._show_route_results,
                             "Toll Comparison", result["summary_text"])
            except Exception as e:
                wx.CallAfter(self._status_update, f"Toll comparison failed: {e}", True)
                wx.CallAfter(self._finish_thinking)

        threading.Thread(target=_calc, daemon=True).start()

    def _tool_virgin_australia_booking(self):
        """Collect an accessible flight search and hand it to Virgin Australia."""
        values = self._collect_airline_booking("Virgin Australia")
        if values is None:
            return

        def _fmt(value):
            return value.strftime("%m-%d-%Y")

        params = {
            "ADT": str(values["adults"]),
            "CHD": str(values["children"]),
            "INF": str(values["infants"]),
            "awardBooking": "false",
            "pos": "au-en",
            "journeyType": "round-trip" if values["return"] else "one-way",
            "date": _fmt(values["depart_date"]),
            "origin": values["origin"],
            "destination": values["destination"],
            "fareType": "REVENUE",
            "cabinType": "E",
        }
        if values["return"]:
            params.update({
                "date1": _fmt(values["return_date"]),
                "origin1": values["destination"],
                "destination1": values["origin"],
            })
        url = ("https://book.virginaustralia.com/dx/VADX/#/flight-selection?"
               + urllib.parse.urlencode(params))
        miab_log(
            "navigation",
            f"Opening Virgin Australia search: {values['origin']} to "
            f"{values['destination']}, {params['journeyType']}, "
            f"{values['adults']} adult(s), {values['children']} child(ren), "
            f"{values['infants']} infant(s).",
            self.settings,
        )
        try:
            import webbrowser
            opened = webbrowser.open(url)
            if opened is False:
                raise RuntimeError("The web browser did not accept the booking link.")
            self._status_update("Virgin Australia flight selection opened in your browser.",
                                force=True)
        except Exception as exc:
            wx.MessageBox(
                f"Could not open Virgin Australia:\n\n{exc}\n\nBooking link:\n{url}",
                "Virgin Australia Booking", wx.OK | wx.ICON_ERROR)

    def _collect_airline_booking(self, airline_name):
        """Collect all airline fields in one explicitly-labelled tab order."""
        airports_csv = self._ensure_airports_csv()
        if not airports_csv:
            self._status_update("Airport data not available.", force=True)
            return None
        import csv
        allowed = _AIRLINE_DIRECT_AIRPORTS.get(airline_name, frozenset())
        choices = []
        with open(airports_csv, encoding="utf-8") as airport_file:
            for row in csv.DictReader(airport_file):
                code = (row.get("iata_code") or "").strip()
                if code not in allowed:
                    continue
                city = (row.get("municipality") or row.get("name") or code).strip()
                country = (row.get("iso_country") or "").strip()
                choices.append((f"{city}, {country} ({code})", code))
        choices.sort(key=lambda item: item[0].lower())

        from dialogs import VirginAustraliaBookingDialog
        dlg = VirginAustraliaBookingDialog(
            self, airline_name=airline_name, airport_choices=choices)
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            return None
        values = dlg.values()
        dlg.Destroy()
        return values

    def _tool_journey_planner(self):
        """Journey Planner — public transit with alternatives."""
        rt = self._get_route_tools()
        if not rt:
            self._resume_location_sound()
            return
        if not rt.is_configured:
            _key_required(
                self,
                "Journey Planner Requires Google",
                "Google API key required.\n\n"
                "Journey Planner needs a Google API key in order to work.\n"
                "",
                "Get a Google API key",
                "https://developers.google.com/maps/get-started",
            )
            self._resume_location_sound()
            return

        country_code = self._ask_country_code()
        if not country_code:
            self._status_update("Journey planner cancelled.", force=True)
            return

        origin = self._pick_location(
            "Leaving from? (address, stop or suburb):", "starting point", rt, country_code)
        if origin is None:
            self._status_update("Journey planner cancelled.", force=True)
            return
        o_lat, o_lon, o_name = origin

        dest = self._pick_location(
            "Going to? (address, stop or suburb):", "destination", rt, country_code)
        if dest is None:
            self._status_update("Journey planner cancelled.", force=True)
            return
        d_lat, d_lon, d_name = dest

        # Do not offer implausible long-distance walking routes.  This uses
        # straight-line distance, so any real walking route would be longer.
        walk_limit_km = 25.0
        direct_km = math.sqrt(
            ((o_lat - d_lat) * 111.0) ** 2
            + ((o_lon - d_lon) * 111.0
               * math.cos(math.radians((o_lat + d_lat) / 2.0))) ** 2)

        from dialogs import ChoiceDialog
        if direct_km > walk_limit_km:
            travel_mode = "transit"
        else:
            # Transit is the normal Journey Planner mode; walking is secondary.
            trip_choices = ["Transit directions", "Walking directions"]
            dlg = ChoiceDialog(self, "What kind of journey?", "Journey Type", trip_choices)
            if dlg.ShowModal() != wx.ID_OK:
                dlg.Destroy()
                self._status_update("Journey planner cancelled.", force=True)
                return
            trip_sel = dlg.GetSelection()
            dlg.Destroy()
            travel_mode = "transit" if trip_sel == 0 else "walking"
        timing_mode = "now"
        timestamp = None

        transit_filter = "all"

        if travel_mode == "transit":
            # Timing mode
            timing_choices = ["Leave now", "Leave at a specific time",
                              "Arrive by a specific time"]
            dlg = ChoiceDialog(self, "When?", "Timing", timing_choices)
            if dlg.ShowModal() != wx.ID_OK:
                dlg.Destroy()
                self._status_update("Journey planner cancelled.", force=True)
                return
            timing_sel = dlg.GetSelection()
            dlg.Destroy()

            timing_mode = ["now", "depart", "arrive"][timing_sel]

            if timing_mode in ("depart", "arrive"):
                label = "Depart at:" if timing_mode == "depart" else "Arrive by:"
                dt_dlg = self._dlgs[2](self, title=label)
                if dt_dlg.ShowModal() != wx.ID_OK:
                    dt_dlg.Destroy()
                    self._status_update("Journey planner cancelled.", force=True)
                    return
                chosen_dt = dt_dlg.get_datetime()
                dt_dlg.Destroy()
                if not chosen_dt:
                    self._status_update("Invalid date/time. Journey planner cancelled.", force=True)
                    return
                timestamp = int(chosen_dt.timestamp())

            # Transit filter
            filter_choices = ["All transport types", "Buses and coaches only",
                              "Trains only", "Ferries only"]
            dlg = ChoiceDialog(self, "Show routes using:", "Transport Type", filter_choices)
            if dlg.ShowModal() != wx.ID_OK:
                dlg.Destroy()
                self._status_update("Journey planner cancelled.", force=True)
                return
            filter_sel = dlg.GetSelection()
            dlg.Destroy()

            transit_filter = ["all", "bus", "train", "ferry"][filter_sel]

        self._thinking()

        def _status(msg):
            wx.CallAfter(self._status_update, msg)

        def _calc():
            try:
                routes = rt.journey_plan(
                    o_name, d_name, country_code,
                    origin_coords=(o_lat, o_lon),
                    dest_coords=(d_lat, d_lon),
                    origin_place_id=getattr(origin, "place_id", ""),
                    dest_place_id=getattr(dest, "place_id", ""),
                    timing_mode=timing_mode,
                    timestamp=timestamp,
                    transit_filter=transit_filter,
                    status_cb=_status,
                    travel_mode=travel_mode,
                )
                for route in routes:
                    route["_journey_origin"] = {
                        "lat": o_lat,
                        "lon": o_lon,
                        "name": o_name,
                        "place_id": getattr(origin, "place_id", ""),
                    }
                    route["_journey_destination"] = {
                        "lat": d_lat,
                        "lon": d_lon,
                        "name": d_name,
                        "place_id": getattr(dest, "place_id", ""),
                    }
                wx.CallAfter(self._show_journey_results, routes)
            except Exception as e:
                import traceback
                miab_log("errors", f"Journey planner failed: {traceback.format_exc()}",
                         self.settings)
                wx.CallAfter(self._status_update, f"Journey planner failed: {e}", True)
                wx.CallAfter(self._finish_thinking)

        threading.Thread(target=_calc, daemon=True).start()

    def _tool_airport_amenity_guide(self):
        """Airport Amenity Guide — official-source shops, food and facilities."""
        if self._dlgs is None:
            self._restore_tools_sound()
            return
        dlg = self._dlgs[1](
            self,
            "Airport, terminal, or official airport URL:",
            title="Airport Amenity Guide",
        )
        if dlg.ShowModal() != wx.ID_OK or not dlg.GetValue():
            dlg.Destroy()
            self._status_update("Airport amenity guide cancelled.", force=True)
            self._restore_tools_sound()
            return
        query = dlg.GetValue()
        dlg.Destroy()
        self._show_airport_amenity_guide(query, restore_on_cancel=True)

    def _choose_airport_amenity_focus(self) -> str:
        from dialogs import ChoiceDialog
        choices = [
            "All amenities",
            "Food and coffee",
            "Shops",
            "Toilets, water, charging and services",
            "Accessibility and calmer spaces",
        ]
        keys = ["all", "food", "shopping", "facilities", "accessibility"]
        dlg = ChoiceDialog(self, "What do you want to focus on?", "Airport Amenity Guide", choices)
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            return ""
        sel = dlg.GetSelection()
        dlg.Destroy()
        if sel == wx.NOT_FOUND or sel < 0 or sel >= len(keys):
            return ""
        return keys[sel]

    def _show_airport_amenity_guide(
        self,
        query: str,
        focus_key: str = "",
        source_hint: str = "",
        airport_name: str = "",
        restore_on_cancel: bool = False,
    ) -> None:
        """Fetch and display an official-source airport amenity guide."""
        query = (query or airport_name or "").strip()
        source_hint = (source_hint or "").strip()
        if not query and not source_hint:
            self._status_update("Airport amenity guide needs an airport or terminal name.", force=True)
            if restore_on_cancel:
                self._restore_tools_sound()
            return

        if not focus_key:
            focus_key = self._choose_airport_amenity_focus()
            if not focus_key:
                self._status_update("Airport amenity guide cancelled.", force=True)
                if restore_on_cancel:
                    self._restore_tools_sound()
                return

        title_name = airport_name or query
        self._status_update(f"Fetching airport amenities for {title_name}...", force=True)
        self._thinking(play_busy_sound=False)
        alarm_on = False
        try:
            self.sound.play_file(r"c:\windows\media\alarm09.wav", loops=-1)
            alarm_on = True
        except Exception:
            pass

        def _calc():
            try:
                text = self._airport_amenity_guide_text(query, focus_key, source_hint)
                if alarm_on:
                    try:
                        self.sound.stop()
                    except Exception:
                        pass
                wx.CallAfter(self._show_route_results, "Airport Amenity Guide", text)
            except Exception as exc:
                if alarm_on:
                    try:
                        self.sound.stop()
                    except Exception:
                        pass
                wx.CallAfter(self._status_update, f"Airport amenity guide failed: {exc}", True)
                wx.CallAfter(self._finish_thinking)

        threading.Thread(target=_calc, daemon=True).start()

    def _airport_amenity_guide_text(self, query: str, focus_key: str, source_hint: str = "") -> str:
        """Build the amenity guide: OSM primary, optional official enrichment.

        1. Geocode the airport, then read its indoor shops/food/facilities from
           OpenStreetMap (consistent and global).
        2. Optionally enrich with before/after-security and assistance details
           extracted from an official airport page — every model-supplied field
           is discarded unless it is evidenced in the fetched page text.
        """
        osm_records: list[dict] = []
        airport_name = ""
        terminal_gates: list[dict] = []
        lat, lon = self._geocode_airport(query, source_hint)
        if lat is not None:
            try:
                raw_records, airport_name, terminal_gates = self._poi_fetcher.fetch_airport_amenities(
                    lat, lon, focus_key=focus_key,
                )
                osm_records = airport_directory.clean_osm_records(raw_records, focus_key=focus_key)
            except Exception as exc:
                self._tool_trace(f"Airport OSM amenity fetch failed: {exc}")

        # If the user typed a bare IATA code, the OSM aerodrome name is a
        # better search/identity anchor for optional official-page enrichment.
        enrichment_query = airport_name or query
        official_records, official_airline_hints, source_links = self._airport_official_enrichment(
            enrichment_query, focus_key, source_hint, osm_have=len(osm_records),
        )

        clean = airport_directory.merge_records(osm_records + official_records)
        airline_hints = airport_directory.combine_airline_hints(
            airport_directory.extract_airline_gate_hints(clean),
            official_airline_hints,
        )
        return airport_directory.summarise_records(
            clean,
            airport_name or query,
            source_links,
            airline_hints=airline_hints,
            terminal_gates=terminal_gates,
            priority_query=query,
        )

    def _geocode_airport(self, query: str, source_hint: str = ""):
        """Resolve an airport query to (lat, lon), or (None, None)."""
        rt = self._get_route_tools()
        if rt is None:
            return None, None
        text = (query or "").strip()
        if not text:
            return None, None
        # Help the geocoder when the user typed a bare name or IATA code.
        low = text.lower()
        if re.fullmatch(r"[a-z]{3}", low):
            geocode_query = f"{text} airport"
        elif not any(w in low for w in ("airport", "terminal", "aeroport",
                                        "aeropuerto", "aeroporto", "flughafen")):
            geocode_query = f"{text} airport"
        else:
            geocode_query = text
        try:
            result = rt.geocode(geocode_query)
            return result.lat, result.lon
        except Exception as exc:
            self._tool_trace(f"Airport geocode failed for {geocode_query!r}: {exc}")
            return None, None

    def _airport_official_enrichment(
        self, query: str, focus_key: str, source_hint: str, osm_have: int,
    ) -> tuple[list[dict], list[dict], list[str]]:
        """Optional, evidence-gated enrichment from official airport pages.

        Runs only when Mistral is configured and a source is reachable (the
        user pasted a URL, or Serper found the official site).  Returns
        (amenity_records, airline_hints, source_links).  Amenity records are
        only extracted when OSM was thin; the airline gate-range extraction
        runs for the comprehensive "all" view, since that data (per-airline
        gate ranges, before/after security) is exactly what OSM lacks.
        """
        mistral = getattr(self, "_mistral", None)
        if not (mistral and getattr(mistral, "is_configured", False)):
            return [], [], []

        want_amenities = osm_have < 12
        want_airlines = focus_key == "all"
        if not (want_amenities or want_airlines):
            return [], [], []

        search_client = getattr(self, "_serper", None)
        source_urls = airport_directory.discover_source_urls(
            query, search_client=search_client, source_hint=source_hint,
        )
        source_text, source_links = airport_directory.fetch_official_source_text(source_urls)
        if not source_text:
            return [], [], source_links

        official: list[dict] = []
        airline_hints: list[dict] = []
        if want_amenities:
            try:
                prompt = airport_directory.build_extraction_prompt(query, focus_key, source_text)
                ckey = airport_directory.cache_key(query, focus_key, source_text)
                raw_text = mistral.query_text(prompt, ckey)
                records = mistral._parse_json_list(raw_text)
                official = airport_directory.clean_directory_records(
                    records, source_text, focus_key=focus_key,
                )
            except Exception as exc:
                self._tool_trace(f"Airport amenity enrichment failed: {exc}")
        if want_airlines:
            try:
                prompt = airport_directory.build_airline_gate_prompt(query, source_text)
                ckey = airport_directory.cache_key("airline_gates " + query, "all", source_text)
                raw_text = mistral.query_text(prompt, ckey)
                records = mistral._parse_json_list(raw_text)
                airline_hints = airport_directory.clean_airline_gate_records(records, source_text)
            except Exception as exc:
                self._tool_trace(f"Airport airline-gate enrichment failed: {exc}")
        return official, airline_hints, source_links

    def _fetch_journey_platforms(self, routes, done_cb):
        """Enrich transit legs with GTFS platform numbers then call done_cb(routes)."""
        rt = self._get_route_tools()
        n_transit_legs = sum(
            1 for route in routes for leg in route.get("legs", [])
            if leg.get("type") == "transit"
        )
        miab_log("navigation",
                 f"Platform fetch started: {len(routes)} route(s), "
                 f"{n_transit_legs} transit leg(s).", self.settings)

        def _fetch():
            for route in routes:
                for leg in route.get("legs", []):
                    if leg.get("type") != "transit":
                        continue
                    lat = leg.get("departure_stop_lat")
                    lon = leg.get("departure_stop_lon")
                    name = leg.get("departure_stop", "")
                    if lat is None or lon is None:
                        continue
                    try:
                        _, stops = self._transit.find_stops_by_name(name, lat, lon)
                        if stops:
                            top = stops[0]
                            leg["departure_platform"] = top.get("platform", "")
                            # Always log what was matched and what platform value
                            # (if any) came back — this is the only way to tell
                            # apart "GTFS genuinely has no platform for this stop"
                            # from "matched the wrong stop" or "field is empty".
                            miab_log("navigation",
                                     f"Platform lookup for {name!r}: matched "
                                     f"{top.get('name', '')!r} (feed {top.get('_feed_id', '')}, "
                                     f"stop_id {top.get('stop_id', '')}, "
                                     f"{top.get('distance', '?')}m away) "
                                     f"platform={top.get('platform', '') or '(none)'}",
                                     self.settings)
                        else:
                            miab_log("errors",
                                     f"Platform lookup: no stops found for "
                                     f"{name!r} near ({lat}, {lon})", self.settings)
                    except Exception:
                        import traceback
                        miab_log("errors",
                                 f"Platform lookup failed for {name!r}: {traceback.format_exc()}",
                                 self.settings)
                route["detail_text"] = rt._build_detail_text(route)
            miab_log("navigation", "Platform fetch finished.", self.settings)
            wx.CallAfter(done_cb, routes)

        threading.Thread(target=_fetch, daemon=True).start()

    def _fetch_journey_stops(self, routes, selected_index, done_cb):
        """Load ordered GTFS stops for the selected journey's transit legs."""
        if not routes:
            return
        if selected_index < 0 or selected_index >= len(routes):
            selected_index = 0
        route = routes[selected_index]

        def _fetch():
            alarm_on = False
            items = []
            try:
                wx.CallAfter(
                    self._status_update,
                    "Loading route stops. Transit data may be downloaded…", True)
                try:
                    self.sound.play_file(r"c:\windows\media\alarm09.wav", loops=-1)
                    alarm_on = True
                except Exception:
                    pass

                transit_legs = [leg for leg in route.get("legs", [])
                                if leg.get("type") == "transit"]
                for leg_num, leg in enumerate(transit_legs, 1):
                    line = leg.get("line_name") or leg.get("vehicle_type") or "Service"
                    direction = leg.get("headsign") or leg.get("arrival_stop") or ""
                    heading = f"{line} to {direction}" if direction else line
                    result = self._gtfs_stops_for_journey_leg(leg)
                    if len(transit_legs) > 1:
                        items.append(f"Service {leg_num}: {heading}")
                    if isinstance(result, dict) and "__candidates__" in result:
                        candidates = result.get("__candidates__") or []
                        result = candidates[0].get("stop_list", []) if candidates else []
                        if candidates:
                            items.append(f"Possible GTFS match: {candidates[0].get('label', heading)}")
                    if not isinstance(result, list) or not result:
                        items.append("No stop sequence available for this service.")
                    else:
                        for stop in result:
                            name = (stop.get("name", stop.get("stop_name", "Unknown"))
                                    if isinstance(stop, dict) else str(stop))
                            items.append(name)

                if not items:
                    items = ["No transit legs were found in this journey."]
                miab_log("navigation",
                         f"Journey stop lookup finished: {len(transit_legs)} transit leg(s), "
                         f"{len(items)} display row(s).", self.settings)
            except Exception as exc:
                miab_log("errors", f"Journey stop lookup failed: {exc}", self.settings)
                items = [f"Could not load route stops: {exc}"]
            finally:
                if alarm_on:
                    try:
                        self.sound.stop()
                    except Exception:
                        pass
            wx.CallAfter(self._status_update, "Route stops ready.", True)
            wx.CallAfter(done_cb, "Journey Planner - Route Stops", items)

        threading.Thread(target=_fetch, daemon=True).start()

    def _gtfs_stops_for_journey_leg(self, leg):
        """Match a journey leg by its known boarding and alighting points.

        Google service codes and GTFS route names often differ (for example
        WST versus a feed's route identifier).  Endpoints are therefore the
        authoritative match for Journey Planner, just as coordinates are for
        platform lookup.  The returned list is only the travelled segment.
        """
        dep_lat = leg.get("departure_stop_lat")
        dep_lon = leg.get("departure_stop_lon")
        arr_lat = leg.get("arrival_stop_lat")
        arr_lon = leg.get("arrival_stop_lon")
        if None in (dep_lat, dep_lon, arr_lat, arr_lon):
            return ["No stop coordinates are available for this service."]

        feed_ids = self._transit._ensure_feeds_for_location(dep_lat, dep_lon)
        if not feed_ids:
            return ["No GTFS feed is available for this service."]

        def _distance_sq(stop, lat, lon):
            return (((float(stop.get("lat", lat)) - lat) * 111_000) ** 2
                    + ((float(stop.get("lon", lon)) - lon) * 111_000
                       * math.cos(math.radians(lat))) ** 2)

        line_key = (leg.get("line_name") or "").strip().lower()
        headsign_key = (leg.get("headsign") or "").strip().lower()
        best = None
        for feed_id in feed_ids:
            data = self._transit._feeds.get(feed_id, {})
            routes = data.get("routes", {})
            for (route_id, headsign), sequence in data.get("route_stops", {}).items():
                if len(sequence) < 2:
                    continue
                dep_idx, dep_stop = min(
                    enumerate(sequence), key=lambda pair: _distance_sq(pair[1], dep_lat, dep_lon))
                dep_d2 = _distance_sq(dep_stop, dep_lat, dep_lon)
                onward = sequence[dep_idx:]
                if not onward:
                    continue
                rel_arr_idx, arr_stop = min(
                    enumerate(onward), key=lambda pair: _distance_sq(pair[1], arr_lat, arr_lon))
                arr_d2 = _distance_sq(arr_stop, arr_lat, arr_lon)
                arr_idx = dep_idx + rel_arr_idx
                # Both endpoints must genuinely belong to this sequence.  Two
                # kilometres accommodates large station/platform complexes.
                if dep_d2 > 2_000 ** 2 or arr_d2 > 2_000 ** 2:
                    continue
                route_info = routes.get(route_id, {})
                names = " ".join((route_info.get("short", ""),
                                  route_info.get("long", ""))).lower()
                score = dep_d2 + arr_d2
                if line_key and line_key in names:
                    score *= 0.5
                if headsign_key and (headsign_key in (headsign or "").lower()
                                     or headsign_key in sequence[-1].get("name", "").lower()):
                    score *= 0.5
                candidate = (score, feed_id, route_id, headsign,
                             sequence[dep_idx:arr_idx + 1], dep_d2, arr_d2)
                if best is None or candidate[0] < best[0]:
                    best = candidate

        if best is None:
            miab_log("errors",
                     f"Journey GTFS endpoint match failed for "
                     f"{leg.get('departure_stop', '')!r} to {leg.get('arrival_stop', '')!r}.",
                     self.settings)
            return ["No GTFS stop sequence matched this journey's boarding and alighting stops."]

        _, feed_id, route_id, headsign, stops, dep_d2, arr_d2 = best
        miab_log("navigation",
                 f"Journey GTFS endpoint match: feed {feed_id}, route {route_id}, "
                 f"headsign={headsign!r}, {len(stops)} travelled stop(s), "
                 f"endpoint distances={math.sqrt(dep_d2):.0f}m/{math.sqrt(arr_d2):.0f}m.",
                 self.settings)
        return stops

    def _show_journey_results(self, routes):
        """Display journey results in the two-level dialog."""
        miab_log("navigation",
                 f"Journey planner returned {len(routes)} route(s).", self.settings)
        if not routes:
            self._status_update("No transit options found.", force=True)
            self._finish_thinking()
            return
        self._status_update(f"Found {len(routes)} option{'s' if len(routes) != 1 else ''}.", force=True)
        try:
            dlg = self._dlgs[3](self, routes)
            dlg.ShowModal()
            dlg.Destroy()
        except Exception as e:
            import traceback
            miab_log("errors", f"Journey planner results display failed: {traceback.format_exc()}",
                     self.settings)
            self._status_update(f"Journey planner: could not display results ({e}).", force=True)
        finally:
            self._finish_thinking()
            self.listbox.SetFocus()
        self.listbox.SetFocus()


    @staticmethod
    def _is_destination_poi(name: str, kind: str = "") -> bool:
        """Keep real destinations; drop lanes, road fragments and stop labels.

        On a shopping strip the POI fetch sweeps in bus-stop labels ("Burwood Rd
        at Livingstone St"), intersection names ("Clarence St Near Burwood Rd")
        and lanes ("Sym Ln") — noise for a traveller wanting places to go.
        """
        low = (name or "").strip().lower()
        if not low:
            return False
        k = (kind or "").strip().lower()
        if k in {"bus stop", "bus_stop", "stop position", "stop_position", "platform"}:
            return False
        if re.search(r"\b(at|near)\b", low) and re.search(
                r"\b(st|street|rd|road|ln|lane|ave|avenue|pde|parade|cres|crescent|hwy|highway)\b", low):
            return False
        if re.search(r"\b(ln|lane)$", low):
            return False
        return True

    @staticmethod
    def _project_pois_to_route(route_coords: list, pois: list,
                               max_off_m: float = 12.0) -> list:
        """Place POIs actually encountered on the route.

        Side is relative to the direction of travel (reliable geometry, unlike
        the footpath-absolute road side).  POIs further than *max_off_m* from the
        walked polyline, and non-destinations (lanes, stops), are dropped. Returns
        [{name, kind, node_index, side}], deduped by name, in route order.
        """
        n = len(route_coords)
        if n < 2:
            return []

        cumulative = [0.0] * n
        for i in range(1, n):
            cumulative[i] = cumulative[i - 1] + dist_metres(
                route_coords[i - 1][0], route_coords[i - 1][1],
                route_coords[i][0], route_coords[i][1])

        def _xy(lat: float, lon: float, ref_lat: float) -> tuple[float, float]:
            return (
                lon * 111000.0 * math.cos(math.radians(ref_lat)),
                lat * 111000.0,
            )

        def _nearest_segment(plat: float, plon: float) -> tuple[float, int, str, float]:
            best = (float("inf"), 0, "right", 0.0)
            for i in range(n - 1):
                alat, alon = route_coords[i]
                blat, blon = route_coords[i + 1]
                snap_lat, snap_lon = nearest_point_on_segment(
                    plat, plon, alat, alon, blat, blon)
                d = dist_metres(plat, plon, snap_lat, snap_lon)
                if d >= best[0]:
                    continue

                ref_lat = (alat + blat + plat) / 3.0
                ax, ay = _xy(alat, alon, ref_lat)
                bx, by = _xy(blat, blon, ref_lat)
                px, py = _xy(plat, plon, ref_lat)
                vx, vy = bx - ax, by - ay
                wx, wy = px - ax, py - ay
                seg_len_sq = vx * vx + vy * vy
                frac = 0.0
                if seg_len_sq:
                    frac = max(0.0, min(1.0, (wx * vx + wy * vy) / seg_len_sq))
                cross = vx * wy - vy * wx
                along = cumulative[i] + frac * math.sqrt(seg_len_sq)
                best = (d, i + 1, "left" if cross > 0 else "right", along)
            return best

        out, seen = [], set()
        for poi in pois or []:
            plat = poi.get("lat")
            plon = poi.get("lon")
            if plat is None or plon is None:
                continue
            name = (poi.get("label") or poi.get("name") or "").split(",")[0].strip()
            if not name:
                continue
            kind = (poi.get("kind") or "").strip().lower()
            if kind not in _NOTABLE_POI_KINDS:
                continue
            if not ToolsMixin._is_destination_poi(name, kind):
                continue
            key = name.lower()
            if key in seen:
                continue
            best_d, best_k, side, route_pos_m = _nearest_segment(plat, plon)
            if best_d > max_off_m:
                continue
            seen.add(key)
            out.append({
                "name": name,
                "kind": (poi.get("kind") or "").strip(),
                "number": (poi.get("number") or "").strip(),
                "street": (poi.get("street") or "").strip(),
                "node_index": best_k,
                "side": side,
                "off_route_m": round(best_d, 1),
                "route_pos_m": route_pos_m,
            })
        out.sort(key=lambda p: (p.get("route_pos_m", 0.0), p["node_index"]))
        return out



    @staticmethod
    def _build_route_blocks(digest: dict, pois: list | None = None,
                            route_coords: list | None = None,
                            features: list | None = None) -> list:
        """Block-by-block list items: one short stretch per item, position-anchored.

        Each item covers the route up to the next intersection, carrying its
        places (by side), the crossing/steps at that intersection, and a real
        Street View waypoint (the intersection ahead).  Keeps the list trackable
        and makes "Street View here" mean a specific point.
        """
        pois = pois or []
        features = features or []
        route_coords = route_coords or []
        legs = digest.get("legs") or []

        cum = []
        if len(route_coords) >= 2:
            cum = [0.0] * len(route_coords)
            for k in range(1, len(route_coords)):
                cum[k] = cum[k - 1] + dist_metres(
                    route_coords[k - 1][0], route_coords[k - 1][1],
                    route_coords[k][0], route_coords[k][1])

        def _coord(idx):
            if route_coords and 0 <= idx < len(route_coords):
                return route_coords[idx]
            return (None, None)

        def _heading(idx):
            if not route_coords or len(route_coords) < 2:
                return None
            if idx < len(route_coords) - 1:
                a, b = route_coords[idx], route_coords[idx + 1]
            else:
                a, b = route_coords[idx - 1], route_coords[idx]
            return bearing_deg(a[0], a[1], b[0], b[1])

        # Coalesce legs into per-street stretches.
        stretches = []
        for leg in legs:
            st = leg.get("street") or "an unnamed road"
            if stretches and stretches[-1]["street"] == st:
                s = stretches[-1]
            else:
                s = {"street": st, "node_start": leg.get("node_start"),
                     "node_end": leg.get("node_end"), "cross": [], "end_action": {}}
                stretches.append(s)
            s["cross"].extend(leg.get("cross_streets_passed") or [])
            s["end_action"] = leg.get("end_action") or {}
            if leg.get("node_end") is not None:
                s["node_end"] = leg.get("node_end")

        items = []
        seen_crossings = set()
        for s in stretches:
            ns, ne = s.get("node_start"), s.get("node_end")
            if ns is None or ne is None or ne <= ns:
                continue
            street = s["street"]
            # Cross-street name(s) at each interior node; when a node carries
            # several, prefer one not already used as a boundary so a bend
            # spanning two nodes (both tagged the same street) doesn't read
            # "Church Street to Church Street".
            by_idx = {}
            for c in s["cross"]:
                ci = c.get("node_index")
                if ci is not None and ns < ci < ne and c.get("name"):
                    by_idx.setdefault(ci, []).append((c["name"], c.get("side")))
            name_at, used = {}, set()   # node_index -> (name, side)
            for ci in sorted(by_idx):
                pick = next((t for t in by_idx[ci] if t[0] not in used), by_idx[ci][0])
                name_at[ci] = pick
                used.add(pick[0])
            # Drop a boundary that repeats the previous name (same street, two nodes).
            boundary_idxs, prev_name = [], None
            for ci in sorted(name_at):
                nm = name_at[ci][0]
                if nm == prev_name:
                    continue
                boundary_idxs.append(ci)
                prev_name = nm
            boundaries = [ns] + boundary_idxs + [ne]
            onto = (s["end_action"] or {}).get("onto")
            turn = (s["end_action"] or {}).get("turn")
            # Streets a crossing may legitimately involve on this stretch.
            allowed_bares = {_road_name_bare(street)}
            allowed_bares.update(_road_name_bare(t[0]) for t in name_at.values())
            if onto:
                allowed_bares.add(_road_name_bare(onto))
            allowed_bares.discard("")

            for bi in range(len(boundaries) - 1):
                a, b = boundaries[bi], boundaries[bi + 1]
                if b <= a:
                    continue
                # Features at a boundary node belong to the block that ENDS
                # there, so the next block starts strictly after it (no double
                # counting across blocks/stretches).  Only the route's very
                # first node is included in the opening block.
                lo = a - 1 if a == 0 else a
                is_last = (bi == len(boundaries) - 2)
                to_entry = name_at.get(b)

                dist = None
                if cum and a < len(cum) and b < len(cum):
                    dist = int(round(cum[b] - cum[a]))
                dist_phrase = f" for about {format_distance(dist)}" if dist else ""

                # Name each block only by where it leads — the previous block
                # already named the intersection it started from.
                if is_last and onto:
                    label = f"Along {street}{dist_phrase}"
                    if turn and turn not in ("arrive", "straight", "continue"):
                        label += f", then turn {turn} into {onto}."
                    else:
                        label += f", then continue onto {onto}."
                elif is_last:
                    label = f"Along {street}{dist_phrase}."
                else:
                    to = (to_entry[0] + _side_phrase(to_entry[1])
                          if to_entry else "the next intersection")
                    label = f"Along {street} to {to}"
                    label += f", about {format_distance(dist)}." if dist else "."

                blk = [p for p in pois if lo < p["node_index"] <= b]

                def _disp(p, _wkey=_street_key(street)):
                    # Announce the house number only when it is definitely on
                    # the street being walked (suffix-tolerant match).
                    num = (p.get("number") or "").strip()
                    if num and _wkey and _street_key(p.get("street") or "") == _wkey:
                        return f"{p['name']} at {num}"
                    return p["name"]

                # A few notable places per side, in walking order — not a list.
                MAX_PER_SIDE = 3
                left = [_disp(p) for p in blk if p["side"] == "left"][:MAX_PER_SIDE]
                right = [_disp(p) for p in blk if p["side"] == "right"][:MAX_PER_SIDE]

                blk_feat_descs = [
                    f.get("desc") for f in features
                    if f.get("route_index") is not None and lo < f["route_index"] <= b
                ]

                detail = []
                if left:
                    detail.append("On the left: " + _join_names(left) + ".")
                if right:
                    detail.append("On the right: " + _join_names(right) + ".")
                detail.extend(_clean_crossings(
                    blk_feat_descs, allowed_bares, seen_crossings))

                wlat, wlon = _coord(b)
                text = label + ("  " + "  ".join(detail) if detail else "")
                items.append({"text": text, "lat": wlat, "lon": wlon, "heading": _heading(b)})

        # Overview prepended, destination appended.
        origin = (digest.get("origin") or {}).get("label") or "the start"
        dest = digest.get("destination") or {}
        dname = dest.get("label") or "the destination"
        total = digest.get("total_distance_m", 0)
        drives = (digest.get("country") or {}).get("drives_on")
        streets = []
        for s in stretches:
            if s["street"] != "an unnamed road" and s["street"] not in streets:
                streets.append(s["street"])
        overview = f"Route from {origin} to {dname}"
        if total:
            overview += f", about {format_distance(total)}"
        overview += "."
        if drives:
            overview += f" Traffic drives on the {drives}."
        if streets:
            overview += " The route follows " + _join_names(streets) + "."
        items.insert(0, {"text": overview, "lat": None, "lon": None, "heading": None})

        side = dest.get("side")
        if side in ("left", "right"):
            dtext = f"{dname} is on your {side} as you arrive."
        elif side == "ahead":
            dtext = f"{dname} is a short distance ahead at the end of the route."
        elif side == "behind":
            dtext = f"{dname} is slightly behind the final point, so you may pass it."
        else:
            dtext = f"You arrive at {dname}."
        items.append({"text": dtext, "lat": dest.get("lat"), "lon": dest.get("lon"),
                      "heading": None})
        return items


    def _journey_pois_for_route(self, route_coords: list) -> list:
        """Fetch POIs near the route and project them onto it (side of travel)."""
        fetcher = getattr(self, "_poi_fetcher", None)
        if not fetcher or len(route_coords) < 2:
            return []
        lats = [c[0] for c in route_coords]
        lons = [c[1] for c in route_coords]
        mid_lat = (min(lats) + max(lats)) / 2.0
        mid_lon = (min(lons) + max(lons)) / 2.0
        span = max((dist_metres(mid_lat, mid_lon, la, lo) for la, lo in route_coords),
                   default=0.0)
        radius = int(min(3000, max(400, span + 150)))
        try:
            pois, _ = fetcher.fetch_pois(
                mid_lat, mid_lon, "all", radius,
                address_points=getattr(self, "_address_points", None),
            )
        except Exception as exc:
            self._tool_trace(f"Journey POI fetch failed: {exc}")
            return []
        return self._project_pois_to_route(route_coords, pois or [])


    def _journey_google_walk_items(self, route: dict, country_code: str = "",
                                   reason: str = "") -> tuple[str, list] | None:
        """Google walking-directions fallback, as accessible-route list items."""
        origin = route.get("_journey_origin") or {}
        dest = route.get("_journey_destination") or {}
        o_lat, o_lon = origin.get("lat"), origin.get("lon")
        d_lat, d_lon = dest.get("lat"), dest.get("lon")
        on = (origin.get("name") or "origin").strip()
        dn = (dest.get("name") or "destination").strip()
        if o_lat is None or o_lon is None or d_lat is None or d_lon is None:
            return None
        rt = self._get_route_tools()
        if not rt or not getattr(rt, "is_configured", False):
            return None
        try:
            groutes = rt.journey_plan(
                on, dn, country_code, travel_mode="walking",
                origin_coords=(float(o_lat), float(o_lon)),
                dest_coords=(float(d_lat), float(d_lon)),
                origin_place_id=str(origin.get("place_id") or ""),
                dest_place_id=str(dest.get("place_id") or ""),
            )
        except Exception as exc:
            self._tool_trace(f"Google walking fallback failed: {exc}")
            return None
        if not groutes:
            return None

        gr = groutes[0]
        leg = (gr.get("legs") or [{}])[0]
        title = "Journey Planner - Accessible directions"
        head = f"Walking directions from {on} to {dn}."
        dur, dist = gr.get("duration_text", ""), gr.get("distance_text", "")
        if dist and dur:
            head += f" About {dist}, {dur}."
        if reason:
            head += " " + reason
        items = [{"text": head, "lat": None, "lon": None, "heading": None}]

        steps = leg.get("steps") or []
        if steps:
            for s in steps:
                instr = (s.get("instruction") or "").strip()
                if not instr:
                    continue
                dtxt = s.get("distance") or ""
                heading = None
                if s.get("lat") is not None and s.get("end_lat") is not None:
                    heading = bearing_deg(s["lat"], s["lon"], s["end_lat"], s["end_lon"])
                items.append({
                    "text": instr + (f" ({dtxt})" if dtxt else ""),
                    "lat": s.get("lat"), "lon": s.get("lon"), "heading": heading,
                })
        else:
            for instr in (leg.get("instructions") or []):
                if instr and instr.strip():
                    items.append({"text": instr.strip(), "lat": None, "lon": None, "heading": None})

        path = [
            (p["lat"], p["lon"])
            for p in (leg.get("_walk_path_points") or [])
            if p.get("lat") is not None and p.get("lon") is not None
        ]
        pois = self._journey_pois_for_route(path) if path else []
        if pois:
            pl = [p["name"] for p in pois if p["side"] == "left"]
            pr = [p["name"] for p in pois if p["side"] == "right"]
            if pl:
                items.append({"text": "Places on the left: " + _join_names(pl) + ".",
                              "lat": None, "lon": None, "heading": None})
            if pr:
                items.append({"text": "Places on the right: " + _join_names(pr) + ".",
                              "lat": None, "lon": None, "heading": None})
        return title, items

    def _journey_transit_accessible_items(self, route: dict) -> tuple[str, list]:
        """Build accessible-directions items for a route that includes transit
        legs, using the leg data journey_plan() already computed instead of
        re-routing a fresh walk between the overall origin and destination.

        The whole-route OSM/Google walk in _journey_accessible_items is only
        correct when the entire journey is on foot — for a route that boards
        a train partway through, walking the straight-line origin-to-
        destination distance can be tens of kilometres of irrelevant road
        (e.g. a train journey ending up as a 50km rural walking route),
        because it ignores that most of the distance is meant to be covered
        by transit. Instead, describe each leg on its own terms: transit
        legs as board/ride/alight text (same fields as the detail text,
        including platform), walking legs using the short, already-correct
        turn-by-turn instructions Google returned for just that leg.
        """
        title = "Journey Planner - Accessible directions"
        origin = route.get("_journey_origin") or {}
        dest = route.get("_journey_destination") or {}
        on = (origin.get("name") or "origin").strip()
        dn = (dest.get("name") or "destination").strip()
        items = [{"text": f"Route from {on} to {dn}.", "lat": None, "lon": None, "heading": None}]

        for leg in route.get("legs", []):
            if leg.get("type") == "transit":
                line_desc = leg.get("line_name") or leg.get("vehicle_type") or "service"
                if leg.get("headsign"):
                    line_desc += f" toward {leg['headsign']}"
                if leg.get("agency"):
                    line_desc += f" ({leg['agency']})"
                plat = leg.get("departure_platform", "")
                plat_str = f", platform {plat}" if plat else ""
                items.append({
                    "text": f"Board {line_desc}. From {leg.get('departure_stop', '')}{plat_str} "
                            f"at {leg.get('departure_time', '')}.",
                    "lat": leg.get("departure_stop_lat"), "lon": leg.get("departure_stop_lon"),
                    "heading": None,
                })
                stops_text = (f"{leg['num_stops']} stops, {leg.get('duration', '')}."
                              if leg.get("num_stops") else "")
                items.append({
                    "text": f"To {leg.get('arrival_stop', '')} at {leg.get('arrival_time', '')}."
                            + (f" {stops_text}" if stops_text else ""),
                    "lat": leg.get("arrival_stop_lat"), "lon": leg.get("arrival_stop_lon"),
                    "heading": None,
                })
            elif leg.get("type") == "walking":
                dur, dist = leg.get("duration", ""), leg.get("distance", "")
                head = f"Walk {dur}, {dist}." if dur and dist else "Walk."
                items.append({"text": head, "lat": None, "lon": None, "heading": None})
                points = leg.get("_walk_points") or []
                for i, instr in enumerate(leg.get("instructions") or []):
                    wp = points[i] if i < len(points) else None
                    heading = None
                    if wp and i + 1 < len(points):
                        nxt = points[i + 1]
                        heading = bearing_deg(wp["lat"], wp["lon"], nxt["lat"], nxt["lon"])
                    items.append({
                        "text": instr,
                        "lat": wp["lat"] if wp else None,
                        "lon": wp["lon"] if wp else None,
                        "heading": heading,
                    })

        return title, items

    def _journey_accessible_items(self, route: dict, country_code: str = "") -> tuple[str, list]:
        """Produce the accessible route as list items: OSM first, Google fallback."""
        title = "Journey Planner - Accessible directions"
        if any(leg.get("type") == "transit" for leg in route.get("legs", [])):
            # Not a pure walking journey — describe each leg on its own terms
            # rather than routing a fresh walk across the whole origin-to-
            # destination distance (see _journey_transit_accessible_items).
            return self._journey_transit_accessible_items(route)
        origin = route.get("_journey_origin") or {}
        dest = route.get("_journey_destination") or {}
        on = (origin.get("name") or "origin").strip()
        dn = (dest.get("name") or "destination").strip()
        o_lat, o_lon = origin.get("lat"), origin.get("lon")
        d_lat, d_lon = dest.get("lat"), dest.get("lon")
        if o_lat is None or o_lon is None or d_lat is None or d_lon is None:
            return title, [{"text": "Accessible directions need origin and destination coordinates.",
                            "lat": None, "lon": None, "heading": None}]

        err = ""
        graph, gerr = self._journey_accessible_osm_graph(route, country_code)
        if graph:
            from nav import NavigationEngine
            engine = NavigationEngine(graph, self.settings)
            msg = ""
            # Prefer to FOLLOW the chosen Google walking route by threading the
            # OSM graph through its turn points, so the description matches the
            # path the user picked (e.g. one that avoids a particular street).
            gturns = []
            for leg in route.get("legs", []):
                if leg.get("type") != "walking":
                    continue
                for wp in leg.get("_walk_points") or []:
                    if wp.get("lat") is not None and wp.get("lon") is not None:
                        gturns.append((float(wp["lat"]), float(wp["lon"])))
            ok = False
            if gturns:
                waypoints = ([(float(o_lat), float(o_lon))] + gturns
                             + [(float(d_lat), float(d_lon))])
                path = engine.route_via_waypoints(waypoints)
                if path:
                    engine.active = True
                    engine.google_mode = False
                    engine.route = path
                    engine.dest_lat = float(d_lat)
                    engine.dest_lon = float(d_lon)
                    engine.dest_name = dn
                    ok = True
            if not ok:
                # No chosen-route turn points (or matching failed) — fall back
                # to a shortest-path OSM walking route.
                msg, ok = engine.find_route_osm(
                    float(o_lat), float(o_lon), float(d_lat), float(d_lon), dn, travel_mode="walking")
            if ok:
                digest = engine.build_route_digest(
                    origin_label=on, origin_lat=None, origin_lon=None, country_code=country_code)
                if digest:
                    dest_blk = digest.get("destination", {})
                    miab_log(
                        "navigation",
                        (f"Journey OSM digest: legs={len(digest.get('legs', []))} "
                         f"dest_side={dest_blk.get('side')} "
                         f"crossing_needed={dest_blk.get('crossing_needed')}"),
                        self.settings,
                    )
                    feature_items = []
                    try:
                        fetcher = getattr(self, "_poi_fetcher", None)
                        overpass = getattr(fetcher, "_overpass", None)
                        road_segs = getattr(self, "_road_segments", None)
                        nav_route = list(engine.route or [])
                        nav_nodes = (engine._graph or {}).get("nodes", {})
                        if overpass and nav_route and nav_nodes:
                            walk_pts = [
                                {"lat": nav_nodes[nid][0], "lon": nav_nodes[nid][1], "instruction": ""}
                                for nid in nav_route if nid in nav_nodes
                            ]
                            if walk_pts:
                                feats = _osm_walk_features(walk_pts, overpass, road_segments=road_segs)
                                # Only crossings and steps — places come from the
                                # projected POI list, so skip "Landmark:" entries.
                                feature_items = [
                                    {"desc": f.get("sv_desc"), "route_index": f.get("route_index")}
                                    for f in feats
                                    if str(f.get("sv_desc", "")).startswith(
                                        ("Pedestrian crossing", "Steps"))
                                ]
                    except Exception as exc:
                        self._tool_trace(f"Journey feature lookup failed: {exc}")
                    route_coords = [
                        engine._graph["nodes"][nid]
                        for nid in (engine.route or [])
                        if nid in (engine._graph or {}).get("nodes", {})
                    ]
                    pois = self._journey_pois_for_route(route_coords)
                    return title, self._build_route_blocks(
                        digest, pois, route_coords, feature_items)
            err = msg
        else:
            err = gerr

        fb = self._journey_google_walk_items(
            route, country_code,
            "OpenStreetMap could not produce a walking route here, so these are "
            "Google's walking directions.")
        if fb:
            return fb
        return title, [{"text": err or "Could not produce accessible directions.",
                        "lat": None, "lon": None, "heading": None}]

    def _fetch_segment_streetview(self, lat, lon, heading, show_cb):
        """Fetch and describe Street View at one route waypoint (on demand)."""
        google_key = self.settings.get("google_api_key", "").strip()
        mistral = getattr(self, "_mistral", None)
        if not google_key:
            show_cb("Street View needs a Google API key. Add one in settings.")
            return
        if not mistral or not getattr(mistral, "is_configured", False):
            show_cb("Street View descriptions need a Mistral API key. Add one in settings.")
            return
        if lat is None or lon is None:
            show_cb("There is no map point for this item to look at.")
            return

        def _calc():
            base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
            cache_path = os.path.join(base, "streetview_cache.json")
            from streetview import lookup_streetview_description
            desc = ""
            try:
                result = lookup_streetview_description(
                    float(lat), float(lon),
                    google_api_key=google_key, mistral_client=mistral,
                    street_heading=heading, cache_path=cache_path,
                    include_images=False, mode="navigation",
                )
                if result:
                    desc = result[1]
            except Exception as exc:
                self._tool_trace(f"Segment Street View failed: {exc}")
            wx.CallAfter(show_cb, desc or "No Street View coverage at this point.")

        threading.Thread(target=_calc, daemon=True).start()

    def _journey_accessible_osm_graph(self, route: dict, country_code: str = "") -> tuple[dict | None, str]:
        """Fetch a temporary walk graph centered on the journey itself."""
        origin = route.get("_journey_origin") or {}
        dest = route.get("_journey_destination") or {}
        o_lat = origin.get("lat")
        o_lon = origin.get("lon")
        d_lat = dest.get("lat")
        d_lon = dest.get("lon")
        if o_lat is None or o_lon is None or d_lat is None or d_lon is None:
            return None, "Accessible OSM directions need origin and destination coordinates."

        try:
            from street_data import geocode_location
        except Exception as exc:
            return None, f"Accessible OSM directions are unavailable: {exc}"

        straight_m = dist_metres(float(o_lat), float(o_lon), float(d_lat), float(d_lon))
        # Center the fetch on the route itself, not on the current GPS point.
        fetch_lat = (float(o_lat) + float(d_lat)) / 2.0
        fetch_lon = (float(o_lon) + float(d_lon)) / 2.0
        radius = int(max(2500, min(8000, (straight_m / 2.0) + 2500)))

        geo = None
        try:
            geo = geocode_location(fetch_lat, fetch_lon)
        except Exception:
            geo = None

        fetch_country = (country_code or (geo.get("country_code", "") if geo else "") or "").strip()

        if not getattr(self, "_street_fetcher", None):
            return None, "Street fetcher is not available."

        try:
            # Fetch by RADIUS around the route, not by suburb boundary.  A
            # point-to-point walk can span suburbs, and the reverse-geocoded
            # suburb name is unreliable (e.g. resolving Burwood to "Sydney"),
            # which sends Overpass a whole-city boundary query that returns
            # nothing and then retries — wasting calls and tripping 429s.  A
            # bounded radius around the route is both correct and far lighter.
            segs, addrs, from_cache, snap_lat, snap_lon, skip_stage2, natural_features, interpolations = (
                self._street_fetcher.fetch_road_data(
                    float(o_lat),
                    float(o_lon),
                    radius=radius,
                    fetch_lat=fetch_lat,
                    fetch_lon=fetch_lon,
                    suburb_name=None,
                    country_code=fetch_country or None,
                )
            )
        except Exception as exc:
            return None, f"Could not load streets for this route: {exc}"

        if not segs:
            return None, "Loaded street data was empty for this route."

        try:
            from types import SimpleNamespace
            scratch = SimpleNamespace(_road_segments=segs)
            graph = self._build_walk_graph.__func__(scratch)
        except Exception as exc:
            return None, f"Could not build a walk graph for this route: {exc}"

        if not graph:
            return None, "Could not build a walk graph for this route."

        return graph, ""

    def _fetch_journey_accessible_osm(self, routes, selected_index, show_cb):
        """Compute accessible walking directions (OSM, Google fallback) as a
        list of self-describing segment items for the route dialog."""
        if not routes:
            return
        if selected_index == wx.NOT_FOUND or selected_index < 0 or selected_index >= len(routes):
            selected_index = 0
        route = routes[selected_index]
        country_code = (getattr(self, "_current_country_code", "") or "").strip()
        title = "Journey Planner - Accessible directions"

        def _calc():
            # This can take ~30s (street fetch, routing, POIs, crossings).  Play
            # the looping working alarm so the wait isn't silent; stop it the
            # moment results are ready.
            alarm_on = False
            try:
                wx.CallAfter(self._status_update,
                             "Building accessible directions — this can take a moment...", True)
                try:
                    self.sound.play_file(r"c:\windows\media\alarm09.wav", loops=-1)
                    alarm_on = True
                except Exception:
                    pass
                used_title, items = self._journey_accessible_items(route, country_code)
                if alarm_on:
                    try:
                        self.sound.stop()
                    except Exception:
                        pass
                wx.CallAfter(show_cb, used_title or title, items)
            except Exception as exc:
                if alarm_on:
                    try:
                        self.sound.stop()
                    except Exception:
                        pass
                wx.CallAfter(show_cb, title, [{
                    "text": f"Accessible directions failed: {exc}",
                    "lat": None, "lon": None, "heading": None,
                }])

        threading.Thread(target=_calc, daemon=True).start()

    def _tool_departure_board(self):
        """Departure Board — find stops and departure boards via HERE, GTFS, or Google Places."""
        here_key = self.settings.get("here_api_key", "").strip()
        google_key = self.settings.get("google_api_key", "").strip()
        source_pref = (self.settings.get("departure_board_source", "gtfs") or "gtfs").strip().lower()
        rt = self._get_route_tools()
        if not rt:
            self._resume_location_sound()
            return
        if source_pref == "google" and not google_key:
            self._warn_optional_key(
                "Departure Board",
                "Google",
                "Google Places station discovery is unavailable, so the board will fall "
                "back to HERE or GTFS data when possible.",
            )
        elif source_pref != "google" and not here_key and not google_key:
            # No warning needed: GTFS-only mode is already the default fallback.
            pass

        country_code = self._ask_country_code()
        if not country_code:
            self._status_update("Departure board cancelled.", force=True)
            return

        location = self._pick_location(
            "Location (suburb, stop name, or address):", "location", rt, country_code)
        if location is None:
            self._status_update("Departure board cancelled.", force=True)
            return
        lat, lon, formatted = location
        self._status_update(f"Searching for stops near {formatted}...")

        # Fetch stations in background
        self._thinking()
        def _fetch():
            try:
                if source_pref == "google" and google_key:
                    stations = self._google_departure_board_stations(lat, lon, google_key)
                    source = "google"
                    if not stations:
                        wx.CallAfter(self._status_update,
                                     f"No Google transit stations found near {formatted}. Falling back to GTFS.",
                                     True)
                        _primary, nearby = self._transit.nearby_stops(
                            lat, lon, radius=250, status_cb=lambda msg: wx.CallAfter(self._status_update, msg))
                        stations = self._gtfs_station_rows(nearby)
                        source = "gtfs"
                elif here_key:
                    stations = rt.here_station_search(lat, lon, here_key)
                    source = "here"
                else:
                    _primary, nearby = self._transit.nearby_stops(
                        lat, lon, radius=250, status_cb=lambda msg: wx.CallAfter(self._status_update, msg))
                    stations = self._gtfs_station_rows(nearby)
                    source = "gtfs"
                if not stations:
                    wx.CallAfter(self._status_update,
                                 f"No transit stops found near {formatted}.",
                                 True)
                    wx.CallAfter(self._finish_thinking)
                    return
                wx.CallAfter(self._show_departure_board, stations, here_key, rt, source)
            except Exception as e:
                wx.CallAfter(self._status_update, f"Departure board failed: {e}", True)
                wx.CallAfter(self._finish_thinking)

        threading.Thread(target=_fetch, daemon=True).start()

    def _gtfs_station_rows(self, nearby: list[dict]) -> list[dict]:
        """Convert nearby GTFS stops into the station rows used by the board."""
        stations = []
        for s in nearby or []:
            feed_id = s.get("_feed_id", "")
            stop_id = s.get("stop_id", "")
            if not feed_id or not stop_id:
                continue
            feed_data = self._transit._feeds.get(feed_id, {})
            stop_departures = feed_data.get("stop_departures", {})
            departures = len(stop_departures.get(stop_id, []))
            stations.append({
                "label": f"{s['name']} — {format_distance(s['distance'])}",
                "name": s["name"],
                "lat": s["lat"],
                "lon": s["lon"],
                "_feed_id": feed_id,
                "_stop_id": stop_id,
                "_distance": s["distance"],
                "_departures": departures,
            })
        stations.sort(key=lambda s: (0 if s.get("_departures", 0) else 1, s["_distance"]))
        if stations and any(s.get("_departures", 0) for s in stations):
            stations = [s for s in stations if s.get("_departures", 0)]
        return stations

    def _google_departure_board_stations(self, lat, lon, google_key):
        """Use Google Places to locate nearby transit stations, then resolve to GTFS."""
        if not google_key:
            return []
        stations = []
        places = []
        try:
            places.extend(self._fetch_google_pois("transport", radius=1500))
            places.extend(self._fetch_google_pois("trains", radius=1500))
        except Exception as exc:
            miab_log("errors", f"[GTFS] Google station discovery failed: {exc}", getattr(self, "settings", None))
            return []

        seen = set()
        for place in places:
            kind = (place.get("kind") or "").lower()
            if kind not in {"transit station", "station", "bus station", "tram stop"}:
                continue
            name = (place.get("name") or "").strip()
            if not name:
                continue
            dedupe = name.lower()
            if dedupe in seen:
                continue
            seen.add(dedupe)
            try:
                plat = float(place["lat"])
                plon = float(place["lon"])
            except Exception:
                continue
            _, nearby = self._transit.nearby_stops(plat, plon, radius=500)
            resolved = self._gtfs_station_rows(nearby)
            if not resolved:
                continue
            for row in resolved[:1]:
                row = dict(row)
                row["label"] = f"{name} — {row['label']}"
                stations.append(row)

        stations.sort(key=lambda s: (0 if s.get("_departures", 0) else 1, s["_distance"]))
        if stations and any(s.get("_departures", 0) for s in stations):
            stations = [s for s in stations if s.get("_departures", 0)]
        return stations

    def _show_departure_board(self, stations, here_key, rt, source="here"):
        """Show the three-level departure board dialog."""
        self._status_update(f"Found {len(stations)} stop{'s' if len(stations) != 1 else ''}.", force=True)

        if source == "gtfs":
            def _fetch_departures(station):
                feed_id = station.get("_feed_id", "")
                stop_id = station.get("_stop_id", "")
                if not feed_id or not stop_id:
                    return []
                routes = self._transit.routes_for_stop(stop_id, feed_id)
                departures = []
                for route in routes:
                    route_id = route.get("route_id", "")
                    if not route_id:
                        continue
                    headsign, times = self._transit.next_departures(stop_id, route_id, feed_id, n=3)
                    label_bits = []
                    short = (route.get("short") or "").strip()
                    long = (route.get("long") or "").strip()
                    route_name = long or short or route_id
                    if short and long and short.lower() not in long.lower():
                        route_name = f"{long} ({short})"
                    if headsign:
                        label_bits.append(f"toward {headsign}")
                    if times:
                        label_bits.append(f"next: {', '.join(times)}")
                    extra = " — " + " — ".join(label_bits) if label_bits else ""
                    departures.append({
                        "label": f"{route_name}{extra}",
                        "line": route_name,
                        "direction": headsign or "",
                        "mode": route.get("type", ""),
                        "operator": route.get("agency", ""),
                        "route_id": route_id,
                        "feed_id": feed_id,
                        "stop_id": stop_id,
                        "station_lat": station["lat"],
                        "station_lon": station["lon"],
                        "source": "gtfs",
                    })
                return departures

            def _fetch_stops(departure):
                route_id = departure.get("route_id", "")
                feed_id = departure.get("feed_id", "")
                headsign = departure.get("direction", "")
                station_lat = departure.get("station_lat")
                station_lon = departure.get("station_lon")
                if not route_id or not feed_id:
                    return ["No timetable data available for this service."]
                stops = self._transit.stops_for_route(route_id, feed_id, headsign=headsign)
                if not stops and station_lat is not None and station_lon is not None:
                    data = self._transit._feeds.get(feed_id, {})
                    route_stops = data.get("route_stops", {})
                    best_stops = []
                    best_dist = float("inf")
                    for (rid, _hs), seq in route_stops.items():
                        if rid != route_id or not seq:
                            continue
                        try:
                            seq_dist = min(
                                ((float(st.get("lat", station_lat)) - station_lat) * 111_000) ** 2
                                + ((float(st.get("lon", station_lon)) - station_lon) * 111_000 * math.cos(math.radians(station_lat))) ** 2
                                for st in seq
                            )
                        except Exception:
                            continue
                        if seq_dist < best_dist:
                            best_dist = seq_dist
                            best_stops = seq
                    stops = best_stops
                if not stops:
                    return ["No timetable data available for this service."]
                return stops
        else:
            def _fetch_departures(station):
                return rt.here_departures(
                    station["id"], here_key,
                    station_lat=station["lat"], station_lon=station["lon"])

            def _fetch_stops(departure):
                return self._gtfs_stops_for_departure(departure)

        dlg = self._dlgs[4](self, stations, _fetch_departures, _fetch_stops)
        dlg.ShowModal()
        dlg.Destroy()
        self._finish_thinking()
        self.listbox.SetFocus()

    def _gtfs_stops_for_departure(self, departure, allow_destination_feed=True):
        """Try to find GTFS stop sequence matching a HERE departure.

        Strategy:
        1. Search local feeds (by station coordinates) for matching route
        2. If no match, search the MobilityData catalog by operator name,
           download that feed, and search it

        ``allow_destination_feed`` is disabled by the Journey Planner so its
        Show Stops action remains non-interactive, like Show Platforms.  The
        Departure Board keeps the destination fallback because users can drill
        into ambiguous service data there.

        Returns list of stop name strings, or a single-item error list.
        """
        # HERE mode → compatible GTFS route types
        _MODE_COMPAT = {
            "bus":              {"bus", "trolleybus"},
            "busRapid":         {"bus", "trolleybus"},
            "regionalTrain":    {"train"},
            "highSpeedTrain":   {"train"},
            "intercityTrain":   {"train"},
            "train":            {"train"},
            "lightRail":        {"tram", "train"},
            "tram":             {"tram"},
            "subway":           {"metro", "train"},
            "ferry":            {"ferry"},
            "monorail":         {"monorail"},
        }

        line = departure.get("line", "")
        headsign = departure.get("direction", "")
        here_mode = departure.get("mode", "")
        operator = departure.get("operator", "")
        lat = departure.get("station_lat", 0)
        lon = departure.get("station_lon", 0)

        if not line or not lat:
            return ["No line information available."]

        compatible_types = _MODE_COMPAT.get(here_mode, set())
        line_lower = line.strip().lower()

        def _mode_ok(rinfo, skip):
            if skip:
                return True
            if not compatible_types:
                return True
            return rinfo.get("type", "") in compatible_types

        def _search_feed(feed_id, skip_mode_check=False, require_headsign=False):
            """Search a loaded feed for a matching route by short_name, long_name, or keywords."""
            data = self._transit._feeds.get(feed_id, {})
            routes = data.get("routes", {})

            # 1. Exact match on short_name
            candidates = []
            for rid, rinfo in routes.items():
                short = (rinfo.get("short") or "").strip().lower()
                if short == line_lower:
                    if not _mode_ok(rinfo, skip_mode_check):
                        miab_log("api_calls", f"[GTFS] Rejected '{short}' — type '{rinfo.get('type')}' "
                              f"incompatible with HERE mode '{here_mode}'", None)
                        continue
                    candidates.append(rid)

            # 2. Exact match on long_name (e.g. "Sandringham" in route_long_name)
            if not candidates:
                for rid, rinfo in routes.items():
                    long = (rinfo.get("long") or "").strip().lower()
                    if long == line_lower:
                        if not _mode_ok(rinfo, skip_mode_check):
                            continue
                        candidates.append(rid)

            # 3. Substring match on short_name
            if not candidates:
                for rid, rinfo in routes.items():
                    short = (rinfo.get("short") or "").strip().lower()
                    if short and (short in line_lower or line_lower in short):
                        if not _mode_ok(rinfo, skip_mode_check):
                            continue
                        candidates.append(rid)

            # 4. Substring match on long_name
            if not candidates:
                for rid, rinfo in routes.items():
                    long = (rinfo.get("long") or "").strip().lower()
                    if long and (line_lower in long or long in line_lower):
                        if not _mode_ok(rinfo, skip_mode_check):
                            continue
                        candidates.append(rid)

            if not candidates:
                return None

            rs = data.get("route_stops", {})
            hs_lower = headsign.strip().lower() if headsign else ""

            # Build query word set for fuzzy headsign matching
            import re as _re_hs
            _HS_STOP_WORDS = frozenset({
                "to", "via", "the", "and", "from", "at", "of", "in",
                "on", "a", "an", "central", "station", "stop",
            })
            def _hs_words(s: str) -> set:
                return {
                    w for w in _re_hs.sub(r"[^a-z0-9\s]", "", s.lower()).split()
                    if w and w not in _HS_STOP_WORDS and len(w) > 1
                }
            q_hs_words = _hs_words(headsign) if headsign else set()

            for matched_rid in candidates:
                if require_headsign and hs_lower:
                    found_hs = False
                    for (rid_key, hs_key) in rs:
                        if rid_key == matched_rid:
                            hs_key_lower = hs_key.strip().lower()
                            # Exact / substring match
                            if (hs_key_lower == hs_lower
                                    or hs_lower in hs_key_lower
                                    or hs_key_lower in hs_lower):
                                found_hs = True
                                break
                            # Fuzzy word-overlap match (threshold 0.5)
                            if q_hs_words:
                                c_words = _hs_words(hs_key)
                                if c_words:
                                    fwd = len(q_hs_words & c_words) / len(q_hs_words)
                                    rev = len(q_hs_words & c_words) / len(c_words)
                                    if max(fwd, rev) >= 0.5:
                                        miab_log("api_calls", f"[GTFS] Fuzzy headsign match: "
                                              f"'{headsign}' ~ '{hs_key}' "
                                              f"(score={max(fwd,rev):.2f})", None)
                                        found_hs = True
                                        break
                    if not found_hs:
                        continue

                stops = self._transit.stops_for_route(matched_rid, feed_id, headsign)
                if stops:
                    return stops

            if require_headsign and hs_lower:
                miab_log("api_calls", f"[GTFS] Route '{line}' found in feed {feed_id} "
                      f"but no variant has headsign matching '{headsign}'", None)
            return None

        # ── Step 1: Search local feeds (by station coordinates) ──────
        try:
            feed_ids = self._transit._ensure_feeds_for_location(lat, lon)
        except Exception:
            feed_ids = []

        for feed_id in feed_ids:
            result = _search_feed(feed_id)
            if result:
                return result

        # ── Step 1.5: Re-search loaded feeds WITHOUT mode check ──────
        # The route may exist in an already-loaded feed but was rejected
        # by the mode filter (e.g. NSW TrainLink coach coded as "bus" in
        # GTFS but "regionalTrain" in HERE). Re-try all loaded feeds
        # with mode check disabled before hitting the catalog.
        if compatible_types:
            for feed_id in feed_ids:
                result = _search_feed(feed_id, skip_mode_check=True, require_headsign=True)
                if result:
                    miab_log("api_calls", f"[GTFS] Found '{line}' in feed {feed_id} "
                          f"(mode check relaxed)", getattr(self, "settings", None))
                    return result

        # ── Step 2: Search by operator name ──────────────────────────
        # First check the operator map (persisted JSON cache), then fall
        # back to catalog search and save the result for next time.
        if operator:
            op_lower = operator.strip().lower()

            # 2a. Check operator map
            op_map = self._load_operator_map()
            cached_fid = op_map.get(op_lower)
            if cached_fid:
                miab_log("api_calls", f"[GTFS] Operator map: '{operator}' → feed {cached_fid}", getattr(self, "settings", None))
                # Ensure the feed is loaded
                catalog = self._transit._catalog_df_full
                if catalog is not None:
                    row = catalog[catalog["mdb_source_id"].astype(str) == cached_fid]
                    if not row.empty:
                        url = str(row.iloc[0].get("urls.direct_download", ""))
                        if url and url != "nan":
                            try:
                                self._transit._gtfs_ensure(cached_fid, url)
                            except Exception:
                                pass
                result = _search_feed(cached_fid, skip_mode_check=True, require_headsign=True)
                if result:
                    return result
                # Cached mapping didn't work — fall through to catalog search

            # 2b. Catalog search
            miab_log("api_calls", f"[GTFS] No local match for '{line}' ({here_mode}). "
                  f"Searching catalog for operator '{operator}'...", getattr(self, "settings", None))
            try:
                catalog = self._transit._catalog_df_full
                if catalog is None:
                    catalog = self._transit._ensure_catalog()
                if catalog is not None and len(catalog):
                    # Full operator name match first
                    mask = catalog["provider"].fillna("").str.lower().str.contains(
                        op_lower, regex=False)
                    matches = catalog[mask]
                    if matches.empty:
                        # Require ALL significant words to appear in provider
                        words = [w for w in op_lower.split() if len(w) > 2]
                        if words:
                            providers_lower = catalog["provider"].fillna("").str.lower()
                            mask = providers_lower.apply(
                                lambda p: all(w in p for w in words))
                            matches = catalog[mask]

                    tried = 0
                    for _, row in matches.iterrows():
                        fid = str(row.get("mdb_source_id", ""))
                        url = str(row.get("urls.direct_download", ""))
                        if not fid or not url or url == "nan":
                            continue
                        if fid in feed_ids:
                            continue
                        if tried >= 3:
                            break
                        tried += 1
                        miab_log("api_calls", f"[GTFS] Trying operator feed {fid} "
                              f"({row.get('provider', 'unknown')})", getattr(self, "settings", None))
                        try:
                            _fid, _data = self._transit._gtfs_ensure(fid, url)
                        except Exception as exc:
                            miab_log("errors", f"[GTFS] Feed {fid} load failed: {exc}", getattr(self, "settings", None))
                            continue
                        if not _data:
                            continue
                        result = _search_feed(fid, skip_mode_check=True, require_headsign=True)
                        if result:
                            miab_log("api_calls", f"[GTFS] Found route '{line}' in feed {fid}", getattr(self, "settings", None))
                            # Save mapping for next time
                            self._save_operator_map(op_lower, fid)
                            return result
            except Exception as exc:
                miab_log("errors", f"[GTFS] Operator catalog search failed: {exc}", getattr(self, "settings", None))

        # ── Extract destination city words from headsign ──────────────
        _DEST_SKIP = frozenset({
            "coach", "terminal", "interchange", "depot", "station",
            "stop", "platform", "central", "the", "and", "of",
            "north", "south", "east", "west", "at", "to", "via",
        })
        _dest_words = [
            w for w in headsign.strip().split()
            if w.lower() not in _DEST_SKIP and len(w) >= 3
        ]
        _dest_query = " ".join(_dest_words)

        # ── Search local feeds for TrainLink/regional routes by destination ──
        # HERE uses its own line numbers (e.g. "38") which don't match GTFS
        # short_names (e.g. "175").  For regional/intercity operators already
        # in the local feed, search their route long_names for the destination
        # city words instead.
        import re as _re_cand

        _CAND_SKIP = frozenset({
            "to", "via", "the", "and", "from", "at", "of", "in",
            "on", "a", "an", "station", "stop", "terminal",
            "coach", "interchange", "central",
        })

        def _words(s: str) -> set:
            return {
                w for w in _re_cand.sub(r"[^a-z0-9\s]", "", s.lower()).split()
                if w and w not in _CAND_SKIP and len(w) > 1
            }

        hs_words = _words(headsign)
        candidates: list[dict] = []

        def _collect_last_stop_candidates(feed_ids_to_search: list) -> None:
            """Add route directions whose last stop matches headsign words."""
            for fid in feed_ids_to_search:
                data   = self._transit._feeds.get(fid, {})
                routes = data.get("routes", {})
                rs     = data.get("route_stops", {})
                for (rid, hs_key), stop_list in rs.items():
                    if not stop_list:
                        continue
                    last_stop_name = stop_list[-1].get("name", "")
                    last_words     = _words(last_stop_name)
                    if not last_words:
                        continue
                    score = len(hs_words & last_words) / len(hs_words)
                    if score < 0.5:
                        continue
                    r      = routes.get(rid, {})
                    short  = r.get("short", "")
                    long_  = r.get("long",  "")
                    agency = r.get("agency", "")
                    label  = short or long_
                    if long_ and long_.lower() not in label.lower():
                        label = f"{label} — {long_}" if label else long_
                    if agency and agency.lower() not in label.lower():
                        label = f"{label}  ({agency})"
                    label = f"{label}  →  {last_stop_name}"
                    # Avoid duplicates
                    if not any(c["feed_id"] == fid and c["route_id"] == rid
                               and c["hs_key"] == hs_key for c in candidates):
                        candidates.append({
                            "feed_id":   fid,
                            "route_id":  rid,
                            "hs_key":    hs_key,
                            "stop_list": stop_list,
                            "label":     label,
                            "score":     score,
                        })

        def _collect_longname_candidates(feed_ids_to_search: list) -> None:
            """Add routes whose long_name contains ALL destination words."""
            dest_lower = [w.lower() for w in _dest_words if len(w) >= 4]
            if not dest_lower:
                return
            for fid in feed_ids_to_search:
                data   = self._transit._feeds.get(fid, {})
                routes = data.get("routes", {})
                rs     = data.get("route_stops", {})
                for rid, r in routes.items():
                    long_ = (r.get("long") or "").lower()
                    if not long_:
                        continue
                    if not all(w in long_ for w in dest_lower):
                        continue
                    short  = r.get("short", "")
                    agency = r.get("agency", "")
                    label  = short or r.get("long", "")
                    if r.get("long","").lower() not in label.lower():
                        label = f"{label} — {r.get('long','')}" if label else r.get("long","")
                    if agency and agency.lower() not in label.lower():
                        label = f"{label}  ({agency})"
                    # Add each direction
                    for (r_id, hs_key), stop_list in rs.items():
                        if r_id != rid or not stop_list:
                            continue
                        last_stop_name = stop_list[-1].get("name", "")
                        full_label = f"{label}  →  {last_stop_name}"
                        if not any(c["feed_id"] == fid and c["route_id"] == rid
                                   and c["hs_key"] == hs_key for c in candidates):
                            candidates.append({
                                "feed_id":   fid,
                                "route_id":  rid,
                                "hs_key":    hs_key,
                                "stop_list": stop_list,
                                "label":     full_label,
                                "score":     0.5,
                            })

        if hs_words:
            # First search local feeds (already loaded) by last stop match
            _collect_last_stop_candidates(feed_ids)
            # Also search by long_name for regional operators in local feed
            # whose HERE line number doesn't match GTFS short_name
            _collect_longname_candidates(feed_ids)

        # ── Load destination feed only if reasonably close (<= 800km) ───
        # Perth/Adelaide are >2500km — Great Southern Rail isn't in any
        # GTFS feed anyway, so don't waste time downloading.
        _dest_feed_ids: list = []
        if allow_destination_feed and _dest_query and not candidates:
            try:
                rt_obj = self._get_route_tools()
            except Exception:
                rt_obj = None
            if rt_obj and rt_obj.is_configured:
                try:
                    resolved = self._resolve_geocode(
                        rt_obj, _dest_query, "", "destination",
                        require_confirmation=False)
                    if resolved is None:
                        miab_log(
                            "errors",
                            f"GTFS destination selection cancelled for {_dest_query!r}.",
                            getattr(self, "settings", None),
                        )
                        return [f"No timetable data found for this service."]
                    d_lat, d_lon, d_fmt = resolved
                    d_km = ((lat - d_lat)**2 * 111**2
                            + (lon - d_lon)**2 * (111 * math.cos(
                                math.radians(lat)))**2) ** 0.5
                    miab_log(
                        "navigation",
                        f"GTFS destination geocode {_dest_query!r} -> "
                        f"({d_lat:.3f},{d_lon:.3f}) {d_fmt} dist={d_km:.0f}km",
                        getattr(self, "settings", None),
                    )
                    if d_km <= 800:
                        _dest_feed_ids = self._transit._ensure_feeds_for_location(
                            d_lat, d_lon)
                        miab_log(
                            "navigation",
                            f"GTFS destination feeds for {_dest_query!r}: {_dest_feed_ids}",
                            getattr(self, "settings", None),
                        )
                        _collect_last_stop_candidates(_dest_feed_ids)
                        _collect_longname_candidates(_dest_feed_ids)
                    else:
                        miab_log(
                            "navigation",
                            f"GTFS destination {_dest_query!r} too far ({d_km:.0f}km); "
                            "skipping feed download.",
                            getattr(self, "settings", None),
                        )
                except Exception as exc:
                    miab_log(
                        "errors",
                        f"GTFS destination geocode failed for {_dest_query!r}: {exc}",
                        getattr(self, "settings", None),
                    )
                    return [f"No timetable data found for this service."]

        candidates.sort(key=lambda c: -c["score"])
        miab_log("api_calls", f"[GTFS] Candidate scan: {len(candidates)} direction(s) "
              f"match headsign '{headsign}'", getattr(self, "settings", None))

        # ── Return ────────────────────────────────────────────────────
        if len(candidates) == 1:
            stop_names = [s.get("name", s.get("stop_name", "Unknown"))
                          for s in candidates[0]["stop_list"]]
            miab_log("api_calls", f"[GTFS] Single candidate — auto-picking: {candidates[0]['label']}", getattr(self, "settings", None))
            return stop_names

        if candidates:
            miab_log("api_calls", f"[GTFS] Returning {len(candidates)} candidates for user choice", getattr(self, "settings", None))
            return {"__candidates__": candidates}

        here_info = "No timetable data found for this service."
        if line:
            here_info += f"  Line: {line}."
        if headsign:
            here_info += f"  Direction: {headsign}."
        if operator:
            here_info += f"  Operator: {operator}."
        return [here_info]

    def _resume_location_sound(self):
        """Re-start the country/region ambient sound and refresh the UI label."""
        if getattr(self, "_thinking_active", False) or getattr(self, "_suppress_location_restore", False):
            self._tool_trace("_resume_location_sound suppressed while thinking.")
            return
        country = getattr(self, 'last_country_found', '')
        continent = getattr(self, 'current_continent', '')
        if country and country != "Open Water":
            self.sound.play_location_sound(country, continent)
        # Restore the location label in the listbox
        label = getattr(self, 'last_location_str', '')
        if label and not getattr(self, "_suppress_location_restore", False):
            self._tool_trace(f"_resume_location_sound restoring label: {label!r}")
            self.update_ui(label)
        elif label:
            self._tool_trace(f"_resume_location_sound skipped label restore: {label!r}")

    def _show_route_results(self, title: str, text: str):
        """Display route results in a read-only dialog."""
        self._tool_trace(f"{title}: results dialog shown.")
        self._begin_tools_workflow()
        dlg = self._dlgs[5](self, title, text)
        try:
            dlg.ShowModal()
        finally:
            dlg.Destroy()
            self._finish_thinking()
            self._end_tools_workflow()
            focus_map = getattr(self, "_focus_map_window_silently", None)
            if callable(focus_map):
                wx.CallAfter(focus_map)
            else:
                wx.CallAfter(self.listbox.SetFocus)

    def _show_rendezvous_results(
        self,
        origin_name: str,
        dest_a_name: str,
        dest_b_name: str,
        route_duration_text: str,
        route_distance_text: str,
        mode: str,
        candidates: list[dict],
        intro: str,
    ):
        """Display ranked rendezvous candidates in an accessible list dialog."""
        if not candidates:
            self._status_update("No rendezvous candidates found.", force=True)
            self._finish_thinking()
            return

        if mode == "meeting":
            header = f"Friend's location: {origin_name}."
            header += f" Your location: {dest_a_name}."
            header += f" Route between the two locations: {route_duration_text}, {route_distance_text}."
            header += " Mode: meet in the middle."
        elif mode == "pickup":
            header = f"Your location: {origin_name}."
            header += f" Friend's location: {dest_a_name}."
            header += f" Shared destination: {dest_b_name}."
            header += f" Friend's route: {route_duration_text}, {route_distance_text}."
            header += " Mode: find a pick-up point."
        elif mode == "dropoff":
            header = f"Shared starting point: {origin_name}."
            header += f" Your destination: {dest_a_name}."
            header += f" Friend's destination: {dest_b_name}."
            header += f" Your route: {route_duration_text}, {route_distance_text}."
            header += " Mode: get dropped off on the way."
        else:
            header = f"Friend's location: {origin_name}."
            header += f" Your location: {dest_a_name}."
            header += f" Route: {route_duration_text}, {route_distance_text}."

        self._status_update("Rendezvous results ready. Use the list to browse options.", force=True)
        self._tool_trace(f"Rendezvous Point: results dialog shown with {len(candidates)} candidates.")
        self._begin_tools_workflow()
        dlg = self._dlgs[6](self, "Rendezvous Point", f"{header} {intro}", candidates)
        try:
            dlg.ShowModal()
        finally:
            dlg.Destroy()
            self._finish_thinking()
            self._end_tools_workflow()
            focus_map = getattr(self, "_focus_map_window_silently", None)
            if callable(focus_map):
                wx.CallAfter(focus_map)
            else:
                wx.CallAfter(self.listbox.SetFocus)


    def _tool_flight_search(self):
        """Flight Search — find flight itineraries between two airports."""
        if not self._timetable.configured:
            _key_required(
                self,
                "RapidAPI Key Required",
                "A RapidAPI key is required for Flight Search.\n\n"
                "Sign up at rapidapi.com, then subscribe to the\n"
                "Timetable Lookup API (free tier).",
                "Sign up for RapidAPI",
                "https://rapidapi.com/auth/sign-up",
            )
            self._resume_location_sound()
            return

        airports_csv = self._ensure_airports_csv()
        if not airports_csv:
            self._status_update("Airport data not available.", force=True)
            self._resume_location_sound()
            return

        from dialogs import FlightSearchDialog
        dlg = FlightSearchDialog(self, airports_csv)
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            self._resume_location_sound()
            return

        origin = dlg.origin_iata
        dest   = dlg.dest_iata
        dlg.Destroy()

        self._thinking()

        def _fetch():
            try:
                timetable_results = []

                try:
                    timetable_results = self._timetable.search(
                        origin, dest,
                        results=15,
                        sort="Duration")
                except Exception as e:
                    miab_log("errors", f"[FlightSearch] Timetable API error: {e}", None)

                if not timetable_results:
                    wx.CallAfter(self._status_update,
                                 f"No flights found from {origin} to {dest}.",
                                 True)
                    wx.CallAfter(self._finish_thinking)
                    return

                from timetable import fmt_itinerary
                lines = [f"Flights: {origin} → {dest}", ""]

                for i, itin in enumerate(timetable_results, 1):
                    lines.append(f"Option {i}:")
                    lines.append(fmt_itinerary(itin))
                    lines.append("")

                # Sound resumes when dialog closes, not now
                wx.CallAfter(self._show_flight_results,
                             "\n".join(lines), origin, dest)

            except Exception as exc:
                import traceback
                traceback.print_exc()
                wx.CallAfter(self._status_update, f"Flight search failed: {exc}", True)
                wx.CallAfter(self._finish_thinking)

        threading.Thread(target=_fetch, daemon=True).start()

    def _show_flight_results(self, text: str, origin: str, dest: str):
        self._begin_tools_workflow()
        dlg = wx.Dialog(self, title=f"Flights {origin} → {dest}",
                        style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        vs  = wx.BoxSizer(wx.VERTICAL)
        txt = wx.TextCtrl(dlg, value=text,
                          style=wx.TE_MULTILINE | wx.TE_READONLY,
                          size=(480, 360))
        vs.Add(txt, 1, wx.EXPAND | wx.ALL, 8)
        btn = wx.Button(dlg, wx.ID_CLOSE, "Close")

        def _close(evt=None):
            dlg.Destroy()
            self._finish_thinking()
            self._end_tools_workflow()

        btn.Bind(wx.EVT_BUTTON, _close)
        dlg.Bind(wx.EVT_CHAR_HOOK,
                 lambda e: _close()
                 if e.GetKeyCode() == wx.WXK_ESCAPE else e.Skip())
        dlg.Bind(wx.EVT_CLOSE, lambda e: _close())
        vs.Add(btn, 0, wx.ALIGN_RIGHT | wx.RIGHT | wx.BOTTOM, 8)
        dlg.SetSizer(vs)
        dlg.CentreOnScreen()
        dlg.Show()
        txt.SetFocus()

    def _ask_hotel_date(self, title: str, default_date=None) -> str:
        """Show a dialog with combo boxes for day/month/year; returns YYYYMMDD or ''."""
        import datetime as _dt

        today = default_date if default_date is not None else _dt.date.today()
        years  = [str(today.year + i) for i in range(3)]
        months = [
            "01 - January", "02 - February", "03 - March", "04 - April",
            "05 - May",     "06 - June",     "07 - July",  "08 - August",
            "09 - September","10 - October", "11 - November","12 - December",
        ]
        days = [f"{d:02d}" for d in range(1, 32)]

        dlg = wx.Dialog(self, title=title,
                        style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        vs = wx.BoxSizer(wx.VERTICAL)

        grid = wx.FlexGridSizer(cols=2, hgap=8, vgap=6)
        grid.AddGrowableCol(1)

        def _add_row(label, ctrl):
            grid.Add(wx.StaticText(dlg, label=label),
                     0, wx.ALIGN_CENTER_VERTICAL)
            grid.Add(ctrl, 1, wx.EXPAND)

        cb_day   = wx.Choice(dlg, choices=days)
        cb_month = wx.Choice(dlg, choices=months)
        cb_year  = wx.Choice(dlg, choices=years)

        cb_day.SetSelection(today.day - 1)
        cb_month.SetSelection(today.month - 1)
        cb_year.SetSelection(0)

        _add_row("Day:",   cb_day)
        _add_row("Month:", cb_month)
        _add_row("Year:",  cb_year)

        vs.Add(grid, 0, wx.EXPAND | wx.ALL, 10)

        btn_sizer = dlg.CreateButtonSizer(wx.OK | wx.CANCEL)
        vs.Add(btn_sizer, 0, wx.ALIGN_RIGHT | wx.ALL, 8)
        dlg.SetSizer(vs)
        dlg.Fit()
        dlg.CentreOnScreen()
        cb_day.SetFocus()

        result = ""
        if dlg.ShowModal() == wx.ID_OK:
            day   = int(days[cb_day.GetSelection()])
            month = int(months[cb_month.GetSelection()][:2])
            year  = int(years[cb_year.GetSelection()])
            try:
                d = _dt.date(year, month, day)
                result = d.strftime("%Y%m%d")
            except ValueError:
                pass          # invalid date (e.g. Feb 30) — caller will catch ""
        dlg.Destroy()
        return result

    def _tool_hotel_search(self):
        """Hotel Search — find hotels in a city."""
        if not self._priceline.configured:
            _key_required(
                self,
                "RapidAPI Key Required",
                "A RapidAPI key is required for Hotel Search.\n\n"
                "Sign up at rapidapi.com, then subscribe to the\n"
                "Priceline Com Provider API (free tier).",
                "Sign up for RapidAPI",
                "https://rapidapi.com/auth/sign-up",
            )
            self._resume_location_sound()
            return

        # --- ask location ---
        dlg = wx.TextEntryDialog(self, "City or destination:", "Hotel Search")
        if dlg.ShowModal() != wx.ID_OK or not dlg.GetValue().strip():
            dlg.Destroy()
            self._resume_location_sound()
            return
        location = dlg.GetValue().strip()
        dlg.Destroy()

        # --- resolve location first, before asking for dates ---
        choices = self._priceline.get_location_id(location)
        if not choices:
            self._status_update("No locations found.", force=True)
            wx.CallAfter(lambda: wx.CallLater(2000, self._finish_thinking))
            return

        labels = [c["label"] for c in choices]
        from dialogs import ChoiceDialog
        dlg2 = ChoiceDialog(self, "Select location", "Location", labels)
        if dlg2.ShowModal() != wx.ID_OK:
            dlg2.Destroy()
            self._resume_location_sound()
            return
        selection = dlg2.GetSelection()
        dlg2.Destroy()
        location_id = choices[selection]["id"]
        location_label = labels[selection]

        # --- now ask for dates ---
        import datetime as _dt
        _today = _dt.date.today()
        checkin = self._ask_hotel_date("Check-in date", default_date=_today)
        if not checkin:
            self._status_update("Hotel search cancelled.", force=True)
            self._resume_location_sound()
            return

        checkout = self._ask_hotel_date("Check-out date", default_date=_today + _dt.timedelta(days=1))
        if not checkout:
            self._status_update("Hotel search cancelled.", force=True)
            self._resume_location_sound()
            return

        if checkout <= checkin:
            self._status_update("Check-out must be after check-in. Hotel search cancelled.", force=True)
            self._resume_location_sound()
            return

        self._thinking()

        def _fetch():
            try:
                results = self._priceline.search_hotels(
                    location_id=location_id,
                    date_checkin=checkin,
                    date_checkout=checkout,
                    sort_order="STAR",
                    min_rating=3
                )
                if not results:
                    wx.CallAfter(self._status_update, f"No hotels found in {location_label}.", True)
                    wx.CallAfter(lambda: wx.CallLater(2000, self._finish_thinking))
                    return

                def _show():
                    from dialogs import HotelResultsDialog

                    dlg = HotelResultsDialog(
                        self, results,
                        show_google_reviews=self._google_reviews_available(),
                        show_tripadvisor_reviews=self._tripadvisor.configured)
                    while dlg.ShowModal() == wx.ID_OK:
                        idx = dlg.selected_index
                        if idx is None:
                            continue

                        hotel = results[idx]

                        action = getattr(dlg, "action", "open")
                        if action == "google_reviews":
                            self._show_hotel_google_reviews(hotel)
                            continue
                        if action == "tripadvisor_reviews":
                            self._show_hotel_tripadvisor_reviews(hotel)
                            continue

                        name = (hotel.get("name") or "").strip()
                        address = (hotel.get("address") or "").strip()
                        opener = getattr(self, "_open_verified_website_for", None)
                        if callable(opener):
                            opener(
                                hotel,
                                name=name,
                                url=(hotel.get("website") or "").strip(),
                                location_hint=address,
                            )
                        else:
                            import webbrowser, urllib.parse
                            query_parts = [name]
                            if address:
                                query_parts.append(address)
                            q = urllib.parse.quote(" ".join(p for p in query_parts if p).strip())
                            webbrowser.open(f"https://www.google.com/search?q={q}&btnI=1")

                    dlg.Destroy()
                    self._finish_thinking()

                wx.CallAfter(_show)

            except Exception as exc:
                import traceback
                traceback.print_exc()
                wx.CallAfter(self._status_update, f"Hotel search failed: {exc}", True)
                wx.CallAfter(self._finish_thinking)
 

        threading.Thread(target=_fetch, daemon=True).start()

    def _show_hotel_google_reviews(self, hotel: dict) -> None:
        """Open the Google reviews flow shared with POI Ctrl+Alt+5."""
        name = (hotel.get("name") or "").strip()
        address = (hotel.get("address") or "").strip()
        self._status_update(f"Looking up Google reviews for {name}...", force=True)
        info = self._lookup_google_review_info(name, address)
        self._present_reviews(name, address, info)

    def _show_hotel_tripadvisor_reviews(self, hotel: dict) -> None:
        """Fetch TripAdvisor review text for a hotel."""
        name = (hotel.get("name") or "").strip()
        lines = []

        ta_reviews = []
        if self._tripadvisor.configured:
            self._status_update(f"Loading TripAdvisor reviews for {name}...")
            try:
                ta_reviews = self._tripadvisor.get_hotel_reviews(
                    name, hotel.get("lat"), hotel.get("lon"))
            except PermissionError:
                lines.append("")
                lines.append("TripAdvisor reviews need a free subscription at "
                             "rapidapi.com/ntd119/api/tripadvisor-com1.")
            except Exception as exc:
                miab_log("errors", f"[HotelReviews] TripAdvisor failed: {exc}", getattr(self, "settings", None))

        if ta_reviews:
            lines.append(f"TripAdvisor reviews for {name}")
            lines.append("")
            for r in ta_reviews:
                bits = []
                if r.get("rating"):
                    bits.append(f"{r['rating']}/5")
                if r.get("date"):
                    bits.append(str(r["date"]))
                if r.get("user"):
                    bits.append(f"by {r['user']}")
                header = r.get("title") or "Review"
                if bits:
                    header += " — " + ", ".join(bits)
                lines.append(header)
                if r.get("text"):
                    lines.append(r["text"])
                lines.append("")

        if not lines:
            self._status_update(f"No TripAdvisor reviews found for {name}.", force=True)
            return

        lines.insert(0, f"Reviews for {name}")
        self._show_detail_reader("\n".join(lines).strip())

    # ------------------------------------------------------------------
    # Find Food  (F key in map mode)
    # ------------------------------------------------------------------

    def _tool_find_food(self, origin_coords=None, dest_coords=None, dest_label=""):
        # If a GTFS route is active (stop sequence or timetable view), find food along it.
        # _active_transit_route is set whenever a route is drilled into and cleared on
        # Backspace/Escape — so its presence is a reliable signal we are in transit context.
        active = getattr(self, "_active_transit_route", None)
        if active:
            self._tool_find_food_transit_line(active)
            return
        self._tool_find_food_route(origin_coords, dest_coords, dest_label)

    def _tool_find_food_route(self, origin_coords=None, dest_coords=None, dest_label=""):
        """F key in map mode — find food places on route to a destination.

        Flow:
          1. Use Google when available, otherwise fall back to open geocoding
             and routing.
          2. Prompt for destination suburb/address unless coordinates were supplied.
          3. Geocode destination when needed.
          4. Fetch a driving route polyline.
          5. Build a bounding box around the polyline + corridor padding.
          6. Single Overpass query for all food POIs in that bbox.
          7. Filter to corridor (cross-track distance ≤ CORRIDOR_M).
          8. Sort by along-route distance, show FindFoodDialog.
          9. On Enter, fetch HERE detail (open/closed, phone, website).
        """
        from geo import dist_to_segment_metres, dist_metres

        # Metres either side of the route a food place may sit and still count
        # as "on route". Kept tight so places you can't actually pull into
        # (e.g. a café a block from where a motorway cuts through) are excluded;
        # food you'd stop at sits on the road frontage. Tune here if needed.
        CORRIDOR_M   = 100
        BBOX_PAD_DEG = 0.005 # ~500 m padding on the bounding box

        # ---- guards -------------------------------------------------------
        rt = self._get_route_tools()
        if not rt:
            self._resume_location_sound()
            return
        if not rt.is_configured:
            self._warn_optional_key(
                "Find Food",
                "Google",
                "it will use open geocoding and OSRM routing instead of Google Maps, "
                "so the route and food search may be a bit less polished.",
            )

        # ---- resolve country code (pure lookup, no IO) --------------------
        country = getattr(self, 'last_country_found', '') or ''
        country_code = ""
        _CODES = {
            "australia": "AU", "united states": "US", "usa": "US",
            "united kingdom": "UK", "uk": "UK", "canada": "CA",
            "new zealand": "NZ", "germany": "DE", "france": "FR",
            "japan": "JP", "china": "CN", "india": "IN",
        }
        if country:
            country_code = _CODES.get(country.lower().strip(), "")
            if not country_code and len(country) == 2:
                country_code = country.upper()

        # ---- origin --------------------------------------------------------
        dest_text = ""
        dest_fmt  = dest_label or "current position"
        dest_lat  = dest_lon = None
        self._find_food_destination = None
        if origin_coords is None:
            origin = self._pick_location(
                "Starting point:", "origin", rt, country_code)
            if origin is None:
                self._status_update("Find Food cancelled.", force=True)
                self._resume_location_sound()
                return
            origin_coords = (origin[0], origin[1])

        # ---- destination ---------------------------------------------------
        if dest_coords is not None:
            dest_lat, dest_lon = dest_coords
            dest_text = dest_fmt
        else:
            dest_result = self._pick_location(
                "Destination:", "destination", rt, country_code)
            if dest_result is None:
                self._status_update("Find Food cancelled.", force=True)
                self._resume_location_sound()
                return
            dest_lat, dest_lon, dest_fmt = dest_result
            dest_coords = (dest_lat, dest_lon)
            dest_text   = dest_fmt
        self._find_food_destination = {"coords": (dest_lat, dest_lon), "name": dest_fmt}

        if origin_coords is None:
            origin_lat = self.lat
            origin_lon = self.lon
        else:
            origin_lat, origin_lon = origin_coords
        self._find_food_populating = True
        self._thinking()

        def _search():
            nonlocal dest_lat, dest_lon, dest_fmt
            try:
                wx.CallAfter(self._status_update,
                             f"Route to {dest_fmt} — fetching…",
                             True)

                # -- fetch route polyline ----------------------------------
                from route_tools import _decode_polyline, _haversine_m

                raw_routes = rt._routes_request(
                    (origin_lat, origin_lon),
                    (dest_lat,   dest_lon),
                    alternatives=False,
                    request_polyline=True,
                )
                if not raw_routes:
                    wx.CallAfter(self._status_update, "No route found.", True)
                    wx.CallAfter(self._finish_thinking)
                    return

                parsed = rt._parse_route(raw_routes[0])
                encoded = parsed.get("polyline", "")
                if not encoded:
                    wx.CallAfter(self._status_update, "Route has no polyline.", True)
                    wx.CallAfter(self._finish_thinking)
                    return

                points = _decode_polyline(encoded)  # list of (lat, lon)
                if len(points) < 2:
                    miab_log("verbose", f"[FindFood] Polyline decoded to {len(points)} point(s) "
                          f"(dist={parsed.get('distance_m',0)}m) — using straight line fallback.", None)
                    points = [(origin_lat, origin_lon), (dest_lat, dest_lon)]

                dist_km = parsed.get("distance_m", 0) / 1000.0
                wx.CallAfter(self._status_update,
                    f"Route is {format_distance(dist_km * 1000)} — searching for food…",
                    True)

                # -- bounding box ------------------------------------------
                lats = [p[0] for p in points]
                lons = [p[1] for p in points]
                s = min(lats) - BBOX_PAD_DEG
                n = max(lats) + BBOX_PAD_DEG
                w = min(lons) - BBOX_PAD_DEG
                e = max(lons) + BBOX_PAD_DEG

                # -- single Overpass query for all food in bbox ------------
                query = (
                    f"[out:json][timeout:40];\n"
                    f"(\n"
                    f'  nwr["amenity"~"cafe|restaurant|bar|fast_food|pub|food_court|ice_cream"]'
                    f"({s:.6f},{w:.6f},{n:.6f},{e:.6f});\n"
                    f'  nwr["shop"~"bakery|butcher"]'
                    f"({s:.6f},{w:.6f},{n:.6f},{e:.6f});\n"
                    f");\n"
                    f"out center tags;"
                ).encode()

                from core import _overpass
                result = _overpass.poi_request(query, timeout=45)

                if not result or not result.get("elements"):
                    wx.CallAfter(self._status_update,
                                 "No food places found along that route.",
                                 True)
                    wx.CallAfter(self._finish_thinking)
                    return

                # Flag the motorway/tunnel stretches so we can drop food you
                # can't pull off to reach (keyless Overpass; safe to fail).
                motorway_flags = self._route_motorway_segments(points, s, w, n, e)

                # -- corridor filter + along-route distance ----------------
                places = []
                seen   = set()

                for el in result["elements"]:
                    tags = el.get("tags", {})
                    name = tags.get("name", "").strip()
                    if not name:
                        continue

                    # lat/lon — ways use "center"
                    if "lat" in el and "lon" in el:
                        plat, plon = el["lat"], el["lon"]
                    elif "center" in el:
                        plat = el["center"]["lat"]
                        plon = el["center"]["lon"]
                    else:
                        continue

                    dedup = f"{name.lower()}|{round(plat,4)}|{round(plon,4)}"
                    if dedup in seen:
                        continue
                    seen.add(dedup)

                    # Cross-track distance to any segment of the route
                    min_cross = float("inf")
                    along_at_min = 0.0
                    cumulative = 0.0
                    best_i = -1

                    for i in range(len(points) - 1):
                        alat, alon = points[i]
                        blat, blon = points[i + 1]
                        cross = dist_to_segment_metres(plat, plon,
                                                       alat, alon,
                                                       blat, blon)
                        if cross < min_cross:
                            min_cross    = cross
                            along_at_min = cumulative + _haversine_m(
                                alat, alon, plat, plon)
                            best_i = i
                        cumulative += _haversine_m(alat, alon, blat, blon)

                    if min_cross > CORRIDOR_M:
                        continue
                    # Nearest the route on a motorway/tunnel here — unreachable
                    # without leaving the route, so drop it.
                    if 0 <= best_i < len(motorway_flags) and motorway_flags[best_i]:
                        continue

                    amenity = tags.get("amenity", tags.get("shop", ""))
                    _KIND_MAP = {
                        "cafe": "café", "restaurant": "restaurant",
                        "fast_food": "fast food", "bar": "bar",
                        "pub": "pub", "food_court": "food court",
                        "ice_cream": "ice cream", "bakery": "bakery",
                        "butcher": "butcher",
                    }
                    kind = _KIND_MAP.get(amenity, amenity)
                    address = tags.get("addr:full", "").strip()
                    if not address:
                        address_parts = []
                        house_number = tags.get("addr:housenumber", "").strip()
                        street = tags.get("addr:street", "").strip()
                        if house_number or street:
                            address_parts.append(" ".join(
                                p for p in [house_number, street] if p))
                        for key in ("addr:suburb", "addr:city", "addr:postcode"):
                            value = tags.get(key, "").strip()
                            if value and value not in address_parts:
                                address_parts.append(value)
                        address = ", ".join(address_parts)
                    if not address:
                        best_addr = None
                        best_addr_d = float("inf")
                        for ap in getattr(self, "_address_points", []):
                            try:
                                d = dist_metres(plat, plon, ap["lat"], ap["lon"])
                            except Exception:
                                continue
                            if d < best_addr_d and d < 80:
                                best_addr = ap
                                best_addr_d = d
                        if best_addr:
                            address = f"{best_addr['number']} {best_addr['street']}"
                    suburb = ""
                    for key in ("addr:suburb", "addr:city", "addr:town", "addr:village"):
                        value = tags.get(key, "").strip()
                        if value:
                            suburb = value
                            break
                    if not suburb:
                        try:
                            from core import _nearest_city
                            _, city_idx = _nearest_city(
                                self._city_lats, self._city_lons, plat, plon)
                            city_row = self.df.iloc[city_idx]
                            suburb = str(city_row.get("city", "")).strip()
                        except Exception:
                            suburb = ""
                    if suburb and suburb.lower() != "nan" and suburb not in address:
                        address = f"{address}, {suburb}" if address else suburb

                    places.append({
                        "name":         name,
                        "lat":          plat,
                        "lon":          plon,
                        "kind":         kind,
                        "address":      address,
                        "along_m":      along_at_min,
                        "cross_street": "",   # enriched below if HERE available
                    })

                if not places:
                    wx.CallAfter(self._status_update,
                                 "No food places within corridor of that route.",
                                 True)
                    wx.CallAfter(self._finish_thinking)
                    return

                places.sort(key=lambda p: p["along_m"])

                wx.CallAfter(self._status_update,
                    f"Found {len(places)} food place"
                    f"{'s' if len(places) != 1 else ''} along the route.",
                    True)
                wx.CallAfter(self._show_find_food_results, places)

            except Exception as exc:
                wx.CallAfter(self._status_update, f"Find Food failed: {exc}", True)
                wx.CallAfter(self._finish_thinking)
            finally:
                wx.CallAfter(setattr, self, "_find_food_populating", False)

        threading.Thread(target=_search, daemon=True).start()

    def _route_motorway_segments(self, points, s, w, n, e):
        """Return one bool per route segment: True where the route runs on a
        genuinely non-stoppable road — a motorway, a motorway ramp, a tunnel, or
        a road tagged motorroad=yes — i.e. a stretch you can't pull off to reach
        food.

        Deliberately does NOT include `trunk`/`primary`: in cities those are
        ordinary arterials (e.g. Old Cleveland Road) lined with shops you stop
        at, so excluding them would wrongly wipe out the whole route.

        Uses a single keyless Overpass query (works with or without a Google
        key). On any failure it returns all-False, so Find Food simply degrades
        to the plain corridor filter rather than breaking.
        """
        from geo import dist_to_segment_metres

        flags = [False] * max(0, len(points) - 1)
        if len(points) < 2:
            return flags

        try:
            query = (
                f"[out:json][timeout:40];\n"
                f"(\n"
                f'  way["highway"~"^(motorway|motorway_link)$"]'
                f"({s:.6f},{w:.6f},{n:.6f},{e:.6f});\n"
                f'  way["motorroad"="yes"]["highway"]'
                f"({s:.6f},{w:.6f},{n:.6f},{e:.6f});\n"
                f'  way["tunnel"="yes"]["highway"]'
                f"({s:.6f},{w:.6f},{n:.6f},{e:.6f});\n"
                f");\n"
                f"out geom;"
            ).encode()
            from core import _overpass
            result = _overpass.poi_request(query, timeout=45)
        except Exception as exc:
            miab_log("errors", f"[FindFood] motorway classification query failed: {exc}", getattr(self, "settings", None))
            return flags

        if not result or not result.get("elements"):
            return flags

        # Flatten the major-road ways into individual segments.
        road_segs = []
        for el in result["elements"]:
            geom = el.get("geometry") or []
            for i in range(len(geom) - 1):
                a, b = geom[i], geom[i + 1]
                road_segs.append((a["lat"], a["lon"], b["lat"], b["lon"]))
        if not road_segs:
            return flags

        THRESHOLD_M = 25.0   # how tightly a route point must hug a major road
        PAD_DEG     = 0.0006 # ~66 m coarse reject before the exact distance calc

        # A route vertex is "on a motorway" when it hugs any major-road segment.
        on_motorway = []
        for plat, plon in points:
            near = False
            for (alat, alon, blat, blon) in road_segs:
                if (min(alat, blat) - plat > PAD_DEG
                        or plat - max(alat, blat) > PAD_DEG
                        or min(alon, blon) - plon > PAD_DEG
                        or plon - max(alon, blon) > PAD_DEG):
                    continue
                if dist_to_segment_metres(plat, plon, alat, alon,
                                          blat, blon) <= THRESHOLD_M:
                    near = True
                    break
            on_motorway.append(near)

        # A segment counts as motorway only when both endpoints hug one, so a
        # single point near a crossing road doesn't wrongly exclude a stretch.
        for i in range(len(points) - 1):
            flags[i] = on_motorway[i] and on_motorway[i + 1]
        return flags

    def _tool_find_food_transit_line(self, active_route: dict) -> None:
        """Ctrl+Alt+F while browsing a GTFS stop sequence.

        Builds a single bounding-box Overpass query covering all stops on the
        active route, filters results to within WALK_M of any individual stop,
        then shows the standard FindFoodDialog with stop name, eatery name,
        address, and walking distance.
        """
        from geo import dist_metres

        WALK_M    = 250   # walking-distance threshold around each stop
        PAD_DEG   = 0.003 # ~330 m bbox padding

        route_name = active_route.get("name", "this route")
        stops      = active_route.get("stops", [])

        if not stops:
            self._status_update("No stop data for this route.", force=True)
            return

        self._status_update(
            f"Searching for food near {len(stops)} stops on {route_name}…")

        def _search():
            try:
                # ── bounding box across all stop coords ──────────────────
                lats = [s["lat"] for s in stops]
                lons = [s["lon"] for s in stops]
                s_bb = min(lats) - PAD_DEG
                n_bb = max(lats) + PAD_DEG
                w_bb = min(lons) - PAD_DEG
                e_bb = max(lons) + PAD_DEG

                query = (
                    f"[out:json][timeout:45];\n"
                    f"(\n"
                    f'  nwr["amenity"~"cafe|restaurant|bar|fast_food|pub|food_court|ice_cream"]'
                    f"({s_bb:.6f},{w_bb:.6f},{n_bb:.6f},{e_bb:.6f});\n"
                    f'  nwr["shop"~"bakery|butcher"]'
                    f"({s_bb:.6f},{w_bb:.6f},{n_bb:.6f},{e_bb:.6f});\n"
                    f");\n"
                    f"out center tags;"
                ).encode()

                from core import _overpass
                result = _overpass.poi_request(query, timeout=50)

                if not result or not result.get("elements"):
                    wx.CallAfter(self._status_update,
                                 "No food outlets found near any stop.", True)
                    wx.CallAfter(self._resume_location_sound)
                    return

                _KIND_MAP = {
                    "cafe": "café", "restaurant": "restaurant",
                    "fast_food": "fast food", "bar": "bar",
                    "pub": "pub", "food_court": "food court",
                    "ice_cream": "ice cream", "bakery": "bakery",
                    "butcher": "butcher",
                }

                places = []
                seen   = set()

                for el in result["elements"]:
                    tags = el.get("tags", {})
                    name = tags.get("name", "").strip()
                    if not name:
                        continue

                    if "lat" in el and "lon" in el:
                        plat, plon = el["lat"], el["lon"]
                    elif "center" in el:
                        plat = el["center"]["lat"]
                        plon = el["center"]["lon"]
                    else:
                        continue

                    dedup = f"{name.lower()}|{round(plat,4)}|{round(plon,4)}"
                    if dedup in seen:
                        continue

                    # ── nearest stop within walking distance ─────────────
                    best_stop  = None
                    best_dist  = float("inf")
                    for stop in stops:
                        d = dist_metres(plat, plon, stop["lat"], stop["lon"])
                        if d < best_dist:
                            best_dist = d
                            best_stop = stop

                    if best_dist > WALK_M:
                        continue   # outside walking distance of every stop

                    seen.add(dedup)

                    # ── address from OSM tags ─────────────────────────────
                    address = tags.get("addr:full", "").strip()
                    if not address:
                        parts = []
                        hn = tags.get("addr:housenumber", "").strip()
                        st = tags.get("addr:street", "").strip()
                        if hn or st:
                            parts.append(" ".join(p for p in [hn, st] if p))
                        for key in ("addr:suburb", "addr:city", "addr:postcode"):
                            v = tags.get(key, "").strip()
                            if v and v not in parts:
                                parts.append(v)
                        address = ", ".join(parts)

                    # Fall back to stop name as location context
                    if not address and best_stop:
                        address = f"near {best_stop['name']}"

                    amenity = tags.get("amenity", tags.get("shop", ""))
                    kind    = _KIND_MAP.get(amenity, amenity)

                    stop_name = best_stop["name"] if best_stop else ""
                    places.append({
                        "name":           name,
                        "lat":            plat,
                        "lon":            plon,
                        "kind":           kind,
                        "address":        address,
                        "along_m":        best_dist,
                        "distance_label": f"from {stop_name}" if stop_name else "from stop",
                        "_sort_key":      stops.index(best_stop) * 1000 + best_dist,
                    })

                if not places:
                    wx.CallAfter(self._status_update,
                                 f"No food outlets within {WALK_M} m of any stop "
                                 f"on {route_name}.", True)
                    wx.CallAfter(self._resume_location_sound)
                    return

                places.sort(key=lambda p: p["_sort_key"])

                n = len(places)
                wx.CallAfter(self._status_update,
                    f"Found {n} food outlet{'s' if n != 1 else ''} "
                    f"along {route_name}.", True)
                wx.CallAfter(self._show_find_food_results, places,
                             f"Food near {route_name}")

            except Exception as exc:
                wx.CallAfter(self._status_update,
                             f"Transit food search failed: {exc}", True)
                wx.CallAfter(self._resume_location_sound)

        threading.Thread(target=_search, daemon=True).start()

    def _show_find_food_results(self, places: list, title="Find Food"):
        """Show the FindFoodDialog with results."""
        self._finish_thinking()
        self._begin_tools_workflow()
        from dialogs import FindFoodDialog
        from core import _load_suppressed, _is_suppressed

        suppressed = _load_suppressed()
        places = [p for p in places if not _is_suppressed(p, suppressed)]

        by_coord = {
            (
                p.get("name", ""),
                round(float(p.get("lat", 0.0)), 6),
                round(float(p.get("lon", 0.0)), 6),
            ): p
            for p in places
        }

        def _detail_cb(name: str, lat: float, lon: float) -> dict:
            """Called on a background thread; returns HERE detail plus OSM fallback."""
            base = by_coord.get((name, round(float(lat), 6), round(float(lon), 6)), {})
            detail = {
                "address": base.get("address", ""),
                "phone": base.get("phone", ""),
                "website": base.get("website", ""),
                "opening_hours": base.get("opening_hours", ""),
            }
            here_key = self.settings.get("here_api_key", "").strip()
            if here_key:
                try:
                    here_detail = self._here.fetch_poi_detail(name, lat, lon)
                    for key in ("address", "phone", "website", "opening_hours"):
                        if here_detail.get(key):
                            detail[key] = here_detail[key]
                except Exception:
                    pass
            return detail

        route_destination = getattr(self, "_find_food_destination", None) or getattr(self, "_map_destination", None)
        dlg = FindFoodDialog(self, places, _detail_cb, title=title, route_destination=route_destination)
        try:
            dlg.ShowModal()
        finally:
            dlg.Destroy()
            self._end_tools_workflow()
            focus_map = getattr(self, "_focus_map_window_silently", None)
            if callable(focus_map):
                wx.CallAfter(focus_map)
            else:
                wx.CallAfter(self.listbox.SetFocus)
