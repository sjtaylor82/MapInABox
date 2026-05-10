"""nav.py — Turn-by-turn navigation engine for Map in a Box.

All routing logic lives here: Dijkstra pathfinding over the walk graph,
instruction building, HERE/Google/OSM route fetching, polyline decoding,
and address geocoding.

No wx, no pygame, no threading — all methods return plain data and strings.
MapNavigator imports NavigationEngine and is responsible for threading,
UI updates, and sound.

Classes
-------
NavigationEngine
    Route fetching and state management for active navigation.
    Instantiate once per session; call reset() when street mode exits.
"""

from __future__ import annotations

import heapq
import json
import math
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from geo import dist_metres, bearing_between_nodes, compass_name, GENERIC_STREET_TYPES


# ---------------------------------------------------------------------------
# NavigationEngine
# ---------------------------------------------------------------------------

class NavigationEngine:
    """Owns all navigation state and routing logic.

    Parameters
    ----------
    walk_graph:
        The graph dict produced by MapNavigator._build_walk_graph().
        May be None; set via ``set_graph()`` before routing.
    settings:
        The app settings dict (for API keys and provider preference).
    """

    def __init__(
        self,
        walk_graph: Optional[dict] = None,
        settings: Optional[dict] = None,
    ) -> None:
        self._graph    = walk_graph
        self._settings = settings or {}

        # Active navigation state
        self.active         : bool        = False
        self.route          : list[int]   = []   # OSM node path
        self.instructions   : list        = []   # [(idx, dist, text, lat, lon)]
        self.step           : int         = 0
        self.dest_name      : str         = ""
        self.dest_lat       : float       = 0.0
        self.dest_lon       : float       = 0.0
        self.last_announced : str         = ""
        self.google_mode    : bool        = False  # True for Google/HERE (no OSM path)
        self.route_mode     : str         = "walking"
        self.total_min      : int         = 0

    # ------------------------------------------------------------------
    # Graph management
    # ------------------------------------------------------------------

    def set_graph(self, walk_graph: Optional[dict]) -> None:
        """Update the walk graph (called after _build_walk_graph)."""
        self._graph = walk_graph

    def update_settings(self, settings: dict) -> None:
        self._settings = settings

    @staticmethod
    def _clean_provider_instruction(text: str) -> str:
        """Remove provider boilerplate like 'Go for 200 m.'."""
        text = (text or "").strip()
        text = re.sub(
            r'\bgo\s+for\s+[\d.,]+\s*(?:m|metres?|meters?|km|kilometres?|kilometers?)\.?\s*',
            '',
            text,
            flags=re.IGNORECASE,
        ).strip()
        text = re.sub(r'\s{2,}', ' ', text).strip()
        return text or "Continue."

    def reset(self) -> None:
        """Clear all navigation state (call on street mode exit)."""
        self.active         = False
        self.route          = []
        self.instructions   = []
        self.step           = 0
        self.dest_name      = ""
        self.dest_lat       = 0.0
        self.dest_lon       = 0.0
        self.last_announced = ""
        self.google_mode    = False
        self.route_mode     = "walking"
        self.total_min      = 0

    # ------------------------------------------------------------------
    # OSM / Dijkstra routing
    # ------------------------------------------------------------------

    def find_route_osm(
        self,
        from_lat: float,
        from_lon: float,
        to_lat: float,
        to_lon: float,
        dest_name: str,
        travel_mode: str = "walking",
    ) -> tuple[str, bool]:
        """Calculate an OSM pedestrian route and set navigation state.

        Returns
        -------
        (announcement_str, success)
        """
        if not self._graph:
            return "Walk graph not available.", False

        start_nid, start_snap_m = self._find_nearest_node_with_distance(from_lat, from_lon)
        end_nid, end_snap_m     = self._find_nearest_node_with_distance(to_lat, to_lon)
        if start_nid is None or end_nid is None:
            return "Could not place start or destination on the street graph.", False
        if start_snap_m > 250:
            return (
                f"Current position is {int(start_snap_m)} metres from the loaded street graph. "
                "Move into the loaded street area or reload streets.",
                False,
            )
        if end_snap_m > 250:
            return (
                f"{dest_name} is {int(end_snap_m)} metres from the loaded street graph. "
                "OSM local routing cannot safely route there from the current loaded area.",
                False,
            )
        if start_nid == end_nid:
            return "You are already at the destination.", False

        path = self._dijkstra(start_nid, end_nid)
        if not path:
            return f"No walkable route found to {dest_name}.", False

        instructions = self._build_instructions(path, dest_name)
        nodes = self._graph["nodes"]
        total_m = sum(
            dist_metres(
                nodes[path[i-1]][0], nodes[path[i-1]][1],
                nodes[path[i]][0],   nodes[path[i]][1],
            )
            for i in range(1, len(path))
        )
        n_turns  = sum(1 for inst in instructions if "arriving" not in inst[2].lower())
        n_steps  = len(instructions)
        first    = instructions[0][2] if instructions else ""
        total_min = max(1, int(round(total_m / 80.0))) if total_m > 0 else 0

        self.active         = True
        self.route          = path
        self.instructions   = instructions
        self.step           = 1
        self.dest_name      = dest_name
        self.dest_lat       = to_lat
        self.dest_lon       = to_lon
        self.last_announced = first
        self.google_mode    = False
        self.route_mode     = "walking"
        self.total_min      = total_min

        msg = (
            f"Route to {dest_name}.  "
            f"{int(total_m)}m, {n_turns} turn{'s' if n_turns != 1 else ''}, "
            f"{n_steps} step{'s' if n_steps != 1 else ''}.  "
            f"Step 1 of {n_steps}: {first}  "
            f"Up for next, Down for previous, I to repeat."
        )
        return msg, True

    # ------------------------------------------------------------------
    # Google Maps routing
    # ------------------------------------------------------------------

    def find_route_google(
        self,
        from_lat: float,
        from_lon: float,
        to_lat: float,
        to_lon: float,
        dest_name: str,
        travel_mode: str = "walking",
    ) -> tuple[str, bool]:
        """Fetch a Google Maps route and set navigation state.

        Returns
        -------
        (announcement_str, success)
        Raises RuntimeError on network failure so the caller can fall back.
        """
        api_key = self._settings.get("google_api_key", "").strip()
        if not api_key:
            raise RuntimeError("No Google API key configured.")

        params = urllib.parse.urlencode({
            "origin":      f"{from_lat},{from_lon}",
            "destination": f"{to_lat},{to_lon}",
            "mode":        travel_mode,
            "key":         api_key,
        })
        url = f"https://maps.googleapis.com/maps/api/directions/json?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": "MapInABox/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())

        if data.get("status") != "OK":
            raise RuntimeError(f"Google routing failed: {data.get('status', 'unknown')}")

        leg = data["routes"][0]["legs"][0]

        def _strip(s):
            text = re.sub(r'<[^>]+>', ' ', s).replace('&nbsp;', ' ').strip()
            return self._clean_provider_instruction(text)

        instructions = []
        cum_dist = 0.0
        for i, step in enumerate(leg["steps"]):
            elat = step["end_location"]["lat"]
            elon = step["end_location"]["lng"]
            cum_dist += step["distance"]["value"]
            instructions.append((i + 1, cum_dist, _strip(step["html_instructions"]),
                                  elat, elon))

        total_m   = leg["distance"]["value"]
        duration_s = leg["duration"]["value"]
        total_min = max(1, duration_s // 60) if duration_s > 0 else 0
        n_turns   = len(instructions) - 1
        n_steps   = len(instructions)
        first     = instructions[0][2] if instructions else ""

        self.active         = True
        self.route          = []
        self.instructions   = instructions
        self.step           = 1
        self.dest_name      = dest_name
        self.dest_lat       = to_lat
        self.dest_lon       = to_lon
        self.last_announced = first
        self.google_mode    = True
        self.route_mode     = travel_mode
        self.total_min      = int(total_min)

        msg = (
            f"Google route to {dest_name}.  "
            f"About {total_m}m, {total_min} min, "
            f"{n_turns} turn{'s' if n_turns != 1 else ''}.  "
            f"Step 1 of {n_steps}: {first}  "
            f"Up for next, Down for previous, I to repeat."
        )
        return msg, True

    # ------------------------------------------------------------------
    # HERE routing
    # ------------------------------------------------------------------

    def find_route_here(
        self,
        from_lat: float,
        from_lon: float,
        to_lat: float,
        to_lon: float,
        dest_name: str,
        travel_mode: str = "pedestrian",
    ) -> tuple[str, bool]:
        """Fetch a HERE pedestrian route and set navigation state.

        Raises RuntimeError on failure so the caller can fall back to OSM.
        """
        api_key = self._settings.get("here_api_key", "").strip()
        if not api_key:
            raise RuntimeError("No HERE API key configured.")

        params = urllib.parse.urlencode({
            "transportMode": travel_mode,
            "origin":        f"{from_lat},{from_lon}",
            "destination":   f"{to_lat},{to_lon}",
            "return":        "polyline,actions,summary,instructions",
            "apiKey":        api_key,
            "lang":          "en-us",
        })
        req = urllib.request.Request(
            f"https://router.hereapi.com/v8/routes?{params}",
            headers={"User-Agent": "MapInABox/1.0"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())

        routes = data.get("routes", [])
        if not routes:
            raise RuntimeError(f"HERE found no route to {dest_name}.")

        section   = routes[0]["sections"][0]
        summary   = section.get("summary", {})
        total_m   = summary.get("length", 0)
        duration_s = summary.get("duration", 0)
        total_min = max(1, duration_s // 60) if duration_s > 0 else 0

        polyline = section.get("polyline", "")
        coords   = self._decode_here_polyline(polyline) if polyline else []
        if any(not (-90 <= lat <= 90 and -180 <= lon <= 180) for lat, lon in coords):
            raise RuntimeError("HERE returned invalid route geometry.")

        instructions = []
        cum_dist = 0.0
        for i, action in enumerate(section.get("actions", [])):
            text     = self._clean_provider_instruction(action.get("instruction", ""))
            length   = action.get("length", 0)
            offset   = action.get("offset", 0)
            cum_dist += length
            if coords and offset < len(coords):
                alat, alon = coords[offset]
            else:
                alat, alon = to_lat, to_lon
            instructions.append((i + 1, cum_dist, text, alat, alon))

        if not instructions:
            raise RuntimeError(f"HERE returned no instructions for {dest_name}.")

        n_turns = len(instructions) - 1
        n_steps = len(instructions)
        first   = instructions[0][2]

        self.active         = True
        self.route          = []
        self.instructions   = instructions
        self.step           = 1
        self.dest_name      = dest_name
        self.dest_lat       = to_lat
        self.dest_lon       = to_lon
        self.last_announced = first
        self.google_mode    = True
        self.route_mode     = travel_mode
        self.total_min      = int(total_min)

        msg = (
            f"HERE route to {dest_name}.  "
            f"About {total_m}m, {total_min} min, "
            f"{n_turns} turn{'s' if n_turns != 1 else ''}.  "
            f"Step 1 of {n_steps}: {first}  "
            f"Up for next, Down for previous, I to repeat."
        )
        return msg, True

    # ------------------------------------------------------------------
    # Progress tracking
    # ------------------------------------------------------------------

    def check_progress(self, current_nid: int) -> tuple[str, bool]:
        """Call after each walk step.

        Returns
        -------
        (instruction_str, arrived)
            instruction_str is non-empty when a waypoint is reached.
            arrived is True when the destination is reached.
        """
        if not self.active or not self.instructions:
            return "", False
        if not self.route:
            return "", False

        try:
            cur_pos = self.route.index(current_nid)
        except ValueError:
            nodes = self._graph["nodes"]
            cur_lat, cur_lon = nodes.get(current_nid, (0.0, 0.0))
            best_i, best_d = 0, float("inf")
            for i, nid in enumerate(self.route):
                la, lo = nodes[nid]
                d = math.hypot((la - cur_lat) * 111000,
                               (lo - cur_lon) * 111000)
                if d < best_d:
                    best_d, best_i = d, i
            cur_pos = best_i

        announced = ""
        arrived   = False
        while self.step < len(self.instructions):
            waypoint_idx, _leg_dist, text = self.instructions[self.step][:3]
            if cur_pos >= waypoint_idx:
                announced = text
                self.step += 1
                self.last_announced = text
                if "arriving" in text.lower():
                    self.active = False
                    arrived = True
                break
            else:
                break
        return announced, arrived

    def next_instruction_str(self, walk_node: Optional[int]) -> str:
        """Return a distance-prefixed string for the next upcoming instruction."""
        if not self.active:
            return ""
        if self.step >= len(self.instructions):
            return ""
        waypoint_idx, _leg_dist, text = self.instructions[self.step][:3]
        if not self.route or walk_node is None:
            return text
        nodes = self._graph["nodes"]
        try:
            cur_pos = self.route.index(walk_node)
        except (ValueError, AttributeError):
            cur_pos = 0
        dist = sum(
            math.hypot(
                (nodes[self.route[i+1]][0] - nodes[self.route[i]][0]) * 111000,
                (nodes[self.route[i+1]][1] - nodes[self.route[i]][1]) * 111000 *
                math.cos(math.radians(nodes[self.route[i]][0])))
            for i in range(cur_pos, min(waypoint_idx, len(self.route) - 1))
        )
        dist_str = f"In {int(dist)}m: " if dist > 10 else ""
        return f"{dist_str}{text}"

    def step_forward(self) -> str:
        """Up key during navigation — announce next instruction."""
        if not self.active:
            return ""
        if self.step >= len(self.instructions):
            return f"Arriving at {self.dest_name}."
        text = self.instructions[self.step][2]
        self.last_announced = text
        self.step = min(self.step + 1, len(self.instructions))
        n = len(self.instructions)
        return f"Step {min(self.step, n)} of {n}: {text}"

    def step_back(self) -> str:
        """Down key during navigation — go back one instruction."""
        if not self.active:
            return ""
        self.step = max(0, self.step - 1)
        if self.step < len(self.instructions):
            text = self.instructions[self.step][2]
            self.last_announced = text
            n = len(self.instructions)
            return f"Step {self.step + 1} of {n}: {text}"
        return ""

    # ------------------------------------------------------------------
    # Address geocoding (HERE)
    # ------------------------------------------------------------------

    def geocode_here(
        self,
        query: str,
        near_lat: float,
        near_lon: float,
        suburb: str = "",
    ) -> Optional[tuple[float, float]]:
        """Geocode *query* near (near_lat, near_lon) using HERE Geocoding API.

        Returns (lat, lon) or None on failure.
        """
        api_key = self._settings.get("here_api_key", "").strip()
        if not api_key:
            return None
        try:
            q = f"{query}, {suburb}" if suburb else query
            params = urllib.parse.urlencode({
                "q":      q,
                "at":     f"{near_lat},{near_lon}",
                "limit":  1,
                "apiKey": api_key,
            })
            req = urllib.request.Request(
                f"https://geocode.search.hereapi.com/v1/geocode?{params}",
                headers={"User-Agent": "MapInABox/1.0"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            items = data.get("items", [])
            if not items:
                return None
            pos = items[0]["position"]
            return pos["lat"], pos["lng"]
        except Exception as exc:
            print(f"[Nav] HERE geocode failed: {exc}")
            return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _find_nearest_node(
        self,
        lat: float,
        lon: float,
        street_filter: Optional[str] = None,
    ) -> Optional[int]:
        best_nid, _best_dist = self._find_nearest_node_with_distance(
            lat, lon, street_filter)
        return best_nid

    def _find_nearest_node_with_distance(
        self,
        lat: float,
        lon: float,
        street_filter: Optional[str] = None,
    ) -> tuple[Optional[int], float]:
        if not self._graph:
            return None, float("inf")
        nodes        = self._graph["nodes"]
        node_streets = self._graph["node_streets"]
        best_nid  = None
        best_dist = float("inf")
        for nid, (nlat, nlon) in nodes.items():
            if street_filter and street_filter not in node_streets.get(nid, set()):
                continue
            d = math.sqrt(
                ((lat  - nlat) * 111000) ** 2 +
                ((lon  - nlon) * 111000 * math.cos(math.radians(lat))) ** 2
            )
            if d < best_dist:
                best_dist = d
                best_nid  = nid
        return best_nid, best_dist

    def _dijkstra(self, start_nid: int, end_nid: int) -> Optional[list[int]]:
        """Dijkstra shortest path. Returns ordered node ID list or None."""
        graph  = self._graph
        nodes  = graph["nodes"]
        edges  = graph["edges"]

        def _dist(a, b):
            la, loa = nodes[a]; lb, lob = nodes[b]
            return dist_metres(la, loa, lb, lob)

        heap    = [(0.0, start_nid, [start_nid])]
        visited : set = set()
        while heap:
            cost, nid, path = heapq.heappop(heap)
            if nid in visited:
                continue
            visited.add(nid)
            if nid == end_nid:
                return path
            for nb, _ in edges.get(nid, []):
                if nb not in visited:
                    heapq.heappush(heap,
                        (cost + _dist(nid, nb), nb, path + [nb]))
        return None

    def _turn_word(self, angle_diff: float) -> str:
        """Convert signed angle (-180..180, positive=right) to a turn word."""
        a    = abs(angle_diff)
        side = "right" if angle_diff >= 0 else "left"
        if a < 20:   return "straight"
        if a < 55:   return f"slight {side}"
        if a < 125:  return side
        if a < 165:  return f"sharp {side}"
        return "U-turn"

    def _build_instructions(
        self,
        node_path: list[int],
        dest_name: str,
    ) -> list:
        """Convert a node path to a list of (idx, cum_dist, text, lat, lon)."""
        graph        = self._graph
        nodes        = graph["nodes"]
        edges        = graph["edges"]
        node_streets = graph["node_streets"]

        def _dist(a, b):
            la, loa = nodes[a]; lb, lob = nodes[b]
            return dist_metres(la, loa, lb, lob)

        def _bearing(a, b):
            return bearing_between_nodes(nodes, a, b)

        def _street_between(a, b):
            for nb, sname in edges.get(a, []):
                if nb == b:
                    return sname
            return ""

        instructions = []
        n = len(node_path)
        if n < 2:
            return instructions

        leg_dist     = 0.0
        prev_bearing = _bearing(node_path[0], node_path[1])

        for i in range(1, n - 1):
            leg_dist    += _dist(node_path[i - 1], node_path[i])
            curr_bearing = _bearing(node_path[i], node_path[i + 1])
            diff         = (curr_bearing - prev_bearing + 180) % 360 - 180
            turn         = self._turn_word(diff)
            next_street  = _street_between(node_path[i], node_path[i + 1])
            card         = compass_name(curr_bearing)

            if turn != "straight":
                onto = f" onto {next_street}" if next_street else ""
                wlat, wlon = nodes[node_path[i]]
                instructions.append((
                    i, leg_dist,
                    f"{turn}{onto}, heading {card}.",
                    wlat, wlon,
                ))
                leg_dist = 0.0
            prev_bearing = curr_bearing

        last       = node_path[-1]
        last_name  = next(iter(node_streets.get(last, set())), "destination")
        alat, alon = nodes[last]
        instructions.append((
            n - 1, leg_dist,
            f"Arriving at {dest_name} on {last_name}.",
            alat, alon,
        ))
        return instructions

    @staticmethod
    def _decode_here_polyline(encoded: str) -> list[tuple[float, float]]:
        """Decode HERE flexible polyline to list of (lat, lon)."""
        TABLE = {c: i for i, c in enumerate(
            'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_')}

        def _uint(s, p):
            result, shift = 0, 0
            while p < len(s):
                if s[p] not in TABLE:
                    raise ValueError(f"Invalid HERE polyline character {s[p]!r}.")
                c = TABLE[s[p]]; p += 1
                result |= (c & 0x1F) << shift; shift += 5
                if (c & 0x20) == 0:
                    break
            return result, p

        def _int(s, p):
            val, p = _uint(s, p)
            return (~(val >> 1) if val & 1 else val >> 1), p

        pos = 0
        version, pos = _uint(encoded, pos)
        if version != 1:
            raise ValueError(f"Unsupported HERE polyline version {version}.")
        header, pos = _uint(encoded, pos)
        precision   = header & 0xF
        third_dim   = (header >> 4) & 0x7
        factor      = 10 ** precision
        has_third   = third_dim > 0

        coords, lat, lng = [], 0, 0
        while pos < len(encoded):
            dlat, pos = _int(encoded, pos)
            dlng, pos = _int(encoded, pos)
            if has_third:
                _, pos = _int(encoded, pos)
            lat += dlat; lng += dlng
            coords.append((lat / factor, lng / factor))
        return coords


# =============================================================================
# NavMixin — UI-level navigation methods for MapNavigator
# Provides all _nav_* methods that call into NavigationEngine.
# Usage: class MapNavigator(NavMixin, LookupsMixin, wx.Frame): ...
# =============================================================================

import threading
import math
import json
import urllib.parse
import urllib.request

import wx
from dialogs import POICategoryDialog, StreetSearchDialog
from logging_utils import miab_log
from poi_fetch import filter_pois_by_category


class NavMixin:

    def _nav_valid_coord(self, lat, lon):
        try:
            lat = float(lat)
            lon = float(lon)
        except (TypeError, ValueError):
            return False
        return -90 <= lat <= 90 and -180 <= lon <= 180

    def _nav_update_ui(self, msg):
        """Navigation messages own the focused listbox item."""
        self._suppress_update_ui_until = 0
        try:
            self.update_ui(msg, force=True)
        except TypeError:
            self.update_ui(msg)
        wx.CallAfter(self.listbox.SetFocus)

    def _nav_status(self, msg):
        """Navigation status that should speak without changing focus."""
        if hasattr(self, "_status_update"):
            self._status_update(msg)
        else:
            self.update_ui(msg)

    def _nav_instruction_distance(self, idx, entry):
        """Distance for this instruction, not remaining route distance."""
        try:
            dist = float(entry[1])
        except (TypeError, ValueError, IndexError):
            return 0
        instructions = getattr(self, '_nav_instructions', [])
        if getattr(self, '_nav_google_mode', False) and idx > 0 and idx < len(instructions):
            try:
                dist = max(0, dist - float(instructions[idx - 1][1]))
            except (TypeError, ValueError, IndexError):
                pass
        return int(round(dist))

    def _nav_format_instruction(self, idx, entry, include_step=True):
        text = entry[2] if len(entry) > 2 else ""
        text = NavigationEngine._clean_provider_instruction(text)
        dist = self._nav_instruction_distance(idx, entry)
        if text and "arriving" not in text.lower():
            unit = "metre" if dist == 1 else "metres"
            out = f"In {dist} {unit}, {text[0].lower() + text[1:]}"
        else:
            out = text
        if include_step:
            total = len(getattr(self, '_nav_instructions', []))
            out = f"{out}  Step {idx + 1} of {total}."
        return out

    def _nearby_walk_poi_text(self, lat, lon, prefix="Nearby"):
        if not self.settings.get("walk_announce_pois"):
            return ""
        radius = self.settings.get("walk_poi_radius_m", 80)
        category = (self.settings.get("walk_poi_category", "all") or "all").lower()
        show_kind = self.settings.get("walk_announce_category", True)
        pois = self._poi_grid_nearby(lat, lon, radius)
        if category != "all":
            pois = filter_pois_by_category(pois, category)
        labels = []
        seen = set()
        for poi in pois:
            name = (poi.get("label") or poi.get("name") or "").split(",")[0].strip()
            if not name or name.lower() in seen:
                continue
            seen.add(name.lower())
            kind = poi.get("kind", "")
            labels.append(f"{name}, {kind}" if show_kind and kind else name)
            if len(labels) >= 5:
                break
        if not labels:
            return ""
        if category == "all":
            heading = prefix
        else:
            heading = f"{prefix} {category}"
        return f"{heading}: " + "; ".join(labels) + "."

    def _nav_to_address(self):
        """G key in street mode — choose destination type then start navigation."""
        if not self._road_fetched or not self._road_segments:
            self._status_update("No street data loaded yet. Wait for streets to load first.", force=True)
            return

        # Offer Address or POI
        has_pois = bool(getattr(self, '_poi_list', []))
        choices = ["Street address"]
        if has_pois:
            choices.append(f"Point of interest ({len(self._poi_list)} loaded)")
        else:
            choices.append("Point of interest (choose category)")

        dlg = wx.SingleChoiceDialog(self, "Navigate to:", "Navigation", choices)
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy(); self.listbox.SetFocus(); return
        sel = dlg.GetSelection(); dlg.Destroy()

        if sel == 0:
            self._nav_to_address_pick()
        else:
            self._nav_to_poi_pick()

    def _nav_to_address_pick(self):
        """Pick a street and number using the same dialog as S key."""
        SUFFIXES = GENERIC_STREET_TYPES
        streets = sorted({
            re.sub(r'\s*\(.*?\)', '', s.get('name', '')).strip()
            for s in self._road_segments
            if re.sub(r'\s*\(.*?\)', '', s.get('name', '')).strip()
            and re.sub(r'\s*\(.*?\)', '', s.get('name', '')).strip().lower() not in SUFFIXES
        })
        if not streets:
            self._status_update("No named streets loaded.", force=True)
            return

        dlg = StreetSearchDialog(self, streets,
                                 title="Navigate — Street",
                                 prompt="Type street name, then press Enter.")
        result = dlg.ShowModal()
        street = dlg.selected_name
        dlg.Destroy()
        if result != wx.ID_OK or not street:
            self.listbox.SetFocus(); return

        ndlg = wx.TextEntryDialog(self, f"House number on {street}:", "Navigate — Number")
        if ndlg.ShowModal() != wx.ID_OK:
            ndlg.Destroy(); self.listbox.SetFocus(); return
        number = ndlg.GetValue().strip(); ndlg.Destroy()
        if not number:
            self.listbox.SetFocus(); return

        def bare(s):
            parts = s.lower().strip().split()
            if parts and parts[-1] in {x.lower() for x in SUFFIXES}:
                parts = parts[:-1]
            return " ".join(parts)

        best = None; best_d = float("inf")
        for addr in getattr(self, "_address_points", []):
            if bare(addr["street"]) == bare(street) and addr["number"] == number:
                d = math.sqrt(
                    ((self.lat - addr["lat"]) * 111000) ** 2 +
                    ((self.lon - addr["lon"]) * 111000 *
                     math.cos(math.radians(self.lat))) ** 2)
                if d < best_d:
                    best_d = d; best = addr

        if best is None:
            # Try HERE geocoding as fallback if key is configured
            here_key = self.settings.get("here_api_key", "").strip()
            if here_key:
                self._status_update(f"Address not in local data, searching HERE for {number} {street}...", force=True)
                def _here_geocode():
                    try:
                        params = urllib.parse.urlencode({
                            "q":     f"{number} {street}, {getattr(self, '_current_suburb', '')}",
                            "at":    f"{self.lat},{self.lon}",
                            "limit": 1,
                            "apiKey": here_key,
                        })
                        url = f"https://geocode.search.hereapi.com/v1/geocode?{params}"
                        req = urllib.request.Request(url, headers={"User-Agent": "MapInABox/1.0"})
                        with urllib.request.urlopen(req, timeout=10) as resp:
                            data = json.loads(resp.read().decode())
                        items = data.get("items", [])
                        if not items:
                            wx.CallAfter(self._status_update,
                                f"Could not find {number} {street}.", True)
                            return
                        pos = items[0]["position"]
                        dest_name = f"{number} {street}"
                        wx.CallAfter(self._nav_launch,
                                     pos["lat"], pos["lng"], dest_name)
                    except Exception as e:
                        print(f"[Nav] HERE geocode failed: {e}")
                        wx.CallAfter(self._status_update,
                            f"Could not find {number} {street}.", True)
                threading.Thread(target=_here_geocode, daemon=True).start()
            else:
                self._status_update(f"Could not find {number} {street} in address data.", force=True)
            return

        dest_name = f"{number} {street}"
        self._nav_launch(best["lat"], best["lon"], dest_name)

    def _nav_to_poi_pick(self):
        """Pick a destination from the loaded POI list, fetching first if needed."""
        pois = getattr(self, '_poi_list', [])
        if not pois:
            sources = ["osm"]
            if self.settings.get("here_api_key", "").strip():
                sources.append("here")
            if self.settings.get("google_api_key", "").strip():
                sources.append("google")
            dlg = POICategoryDialog(self, available_sources=sources)
            if dlg.ShowModal() != wx.ID_OK or not dlg.selected_key:
                dlg.Destroy()
                self.listbox.SetFocus()
                return
            category = dlg.selected_key
            name     = dlg.selected_name
            source   = dlg.selected_source
            dlg.Destroy()
            self._status_update("Loading points of interest...")
            def _fetch_then_pick():
                self._fetch_pois(category, name_filter=name, source=source)
                wx.CallAfter(self._nav_to_poi_pick)
            threading.Thread(target=_fetch_then_pick, daemon=True).start()
            return

        names = [p["label"].split(",")[0].strip() for p in pois]
        dlg = wx.SingleChoiceDialog(self, "Navigate to which POI?", "Navigation — POI", names)
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy(); self.listbox.SetFocus(); return
        idx = dlg.GetSelection(); dlg.Destroy()
        poi = pois[idx]
        dest_name = names[idx]
        self._poi_list  = []
        self._poi_index = 0
        self._poi_explore_stack = []
        self.listbox.SetFocus()
        self._nav_launch(
            poi["lat"], poi["lon"], dest_name,
            target_source="poi",
            target_meta=poi,
        )

    def _nav_launch(self, dest_lat, dest_lon, dest_name,
                    target_source="manual", target_meta=None):
        """Common entry point — check provider and start navigation."""
        requested_provider = self.settings.get("nav_provider", "osm")
        provider = requested_provider
        try:
            dest_lat = float(dest_lat)
            dest_lon = float(dest_lon)
        except (TypeError, ValueError):
            self._nav_update_ui(f"No GPS coordinate for {dest_name}.")
            return
        meta = target_meta or {}
        distance_m = dist_metres(self.lat, self.lon, dest_lat, dest_lon)
        source = meta.get("source") or meta.get("_source") or target_source
        allow_osm_fallback = provider == "osm"
        route_mode = "walking"
        google_mode = "walking"
        here_mode = "pedestrian"
        if target_source == "poi" and distance_m > 2000 and provider in ("google", "here"):
            route_mode = "driving"
            google_mode = "driving"
            here_mode = "car"
        miab_log(
            "navigation",
            (f"Route target selected: source={source} provider={provider} "
             f"requested_provider={requested_provider} "
             f"mode={route_mode} "
             f"name={dest_name!r} lat={dest_lat:.6f} lon={dest_lon:.6f} "
             f"distance_m={distance_m:.0f}"),
            self.settings,
        )
        self._pending_nav_after_street_load = None
        self._nav_active = False
        self._nav_route = []
        self._nav_instructions = []
        self._nav_step = 0
        self._nav_last_announced = ""
        self._nav_google_mode = False
        self._nav_dest_name = dest_name
        self._nav_dest_lat = dest_lat
        self._nav_dest_lon = dest_lon
        if provider == "google":
            label = "driving" if google_mode == "driving" else "walking"
            self._nav_status(f"Getting Google {label} directions to {dest_name}...")
            threading.Thread(
                target=self._nav_start_google,
                args=(dest_lat, dest_lon, dest_name, allow_osm_fallback, google_mode, target_source),
                daemon=True).start()
        elif provider == "here":
            here_key = self.settings.get("here_api_key", "").strip()
            if not here_key:
                self._nav_update_ui("No HERE API key configured. Set one in settings, or change GPS provider.")
            else:
                label = "driving" if here_mode == "car" else "walking"
                self._nav_status(f"Getting HERE {label} directions to {dest_name}...")
                threading.Thread(
                    target=self._nav_start_here,
                    args=(dest_lat, dest_lon, dest_name, allow_osm_fallback, here_mode, target_source),
                    daemon=True).start()
        else:
            self._nav_status(f"Calculating route to {dest_name}...")
            threading.Thread(
                target=self._nav_start,
                args=(dest_lat, dest_lon, dest_name, target_source),
                daemon=True).start()

    def _nav_start(self, dest_lat, dest_lon, dest_name, target_source="manual"):
        """OSM routing — delegate to NavigationEngine."""
        def fail(msg, detail=None):
            log_detail = f" detail={detail}" if detail else ""
            miab_log(
                "navigation",
                f"OSM route failed: target={dest_name!r} source={target_source}.{log_detail}",
                self.settings,
            )
            suggestion = "Try HERE or Google in Navigation provider settings."
            if target_source == "poi":
                msg = (
                    f"OpenStreetMap could not calculate a route to {dest_name}. "
                    f"{msg} {suggestion}"
                )
            elif suggestion not in str(msg):
                msg = f"{msg} {suggestion}"
            wx.CallAfter(self._nav_status, msg)

        walk_graph = getattr(self, "_walk_graph", None)
        if not walk_graph:
            if not self._road_fetched or not self._road_segments:
                if not getattr(self, "street_mode", False):
                    self._pending_nav_after_street_load = (
                        dest_lat, dest_lon, dest_name, target_source
                    )
                    wx.CallAfter(
                        self._nav_status,
                        f"Loading street data before routing to {dest_name}...",
                    )
                    self._suppress_next_street_loading_status = True
                    wx.CallAfter(self.toggle_street_mode)
                    return
                fail("Street data is not loaded yet. Wait for streets to finish loading.")
                return
            wx.CallAfter(self._nav_status, "Building walk graph...")
            self._walk_graph = self._build_walk_graph()
            self._nav.set_graph(self._walk_graph)
            if not self._walk_graph or not self._walk_graph.get("intersections"):
                fail("Could not build walk graph; not enough intersections found.")
                return
        self._nav.set_graph(self._walk_graph)
        self._nav.update_settings(self.settings)
        wx.CallAfter(self._nav_status, "Calculating route...")
        msg, ok = self._nav.find_route_osm(
            self.lat, self.lon, dest_lat, dest_lon, dest_name)
        if ok:
            self._sync_nav_state_from_engine()
            msg = self._nav_route_summary(dest_name, provider="Route")
            miab_log(
                "navigation",
                f"OSM route started: target={dest_name!r} steps={len(self._nav_instructions)}",
                self.settings,
            )
        else:
            fail(str(msg or "No route was returned."), detail=msg)
            return
        wx.CallAfter(self._nav_update_ui, msg)

    def _nav_start_google(self, dest_lat, dest_lon, dest_name,
                          allow_osm_fallback=True, travel_mode="walking",
                          target_source="manual"):
        """Google Maps routing — delegate to NavigationEngine."""
        self._nav.update_settings(self.settings)
        try:
            msg, ok = self._nav.find_route_google(
                self.lat, self.lon, dest_lat, dest_lon, dest_name,
                travel_mode=travel_mode)
            if ok:
                self._sync_nav_state_from_engine()
                msg = self._nav_route_summary(dest_name, provider="Google route")
            wx.CallAfter(self._nav_update_ui, msg)
        except Exception as exc:
            print(f"[Nav] Google routing failed: {exc}")
            err_str = str(exc)
            if "No Google API key" in err_str:
                wx.CallAfter(self._nav_update_ui,
                    "Google API key not configured. Add one in Settings, or change navigation provider to OSM.")
                return
            if not allow_osm_fallback:
                wx.CallAfter(self._nav_update_ui,
                    f"Google could not route to {dest_name}.")
                return
            wx.CallAfter(self._nav_update_ui,
                f"Google routing error. Falling back to OSM.")
            self._nav_start(dest_lat, dest_lon, dest_name, target_source)

    def _nav_start_here(self, dest_lat, dest_lon, dest_name,
                        allow_osm_fallback=True, travel_mode="pedestrian",
                        target_source="manual"):
        """HERE routing — delegate to NavigationEngine, fall back to OSM."""
        self._nav.update_settings(self.settings)
        try:
            msg, ok = self._nav.find_route_here(
                self.lat, self.lon, dest_lat, dest_lon, dest_name,
                travel_mode=travel_mode)
            if ok:
                self._sync_nav_state_from_engine()
                msg = self._nav_route_summary(dest_name, provider="HERE route")
            wx.CallAfter(self._nav_update_ui, msg)
        except urllib.error.HTTPError as exc:
            print(f"[Nav] HERE routing HTTP {exc.code}")
            if not allow_osm_fallback:
                wx.CallAfter(self._nav_update_ui,
                    f"HERE could not route to {dest_name}.")
                return
            wx.CallAfter(self._nav_update_ui,
                f"HERE routing error ({exc.code}). Falling back to OSM.")
            self._nav_start(dest_lat, dest_lon, dest_name, target_source)
        except Exception as exc:
            print(f"[Nav] HERE routing failed: {exc}")
            if not allow_osm_fallback:
                wx.CallAfter(self._nav_update_ui,
                    f"HERE could not route to {dest_name}.")
                return
            wx.CallAfter(self._nav_update_ui,
                "HERE routing error. Falling back to OSM.")
            self._nav_start(dest_lat, dest_lon, dest_name, target_source)

    def _sync_nav_state_from_engine(self):
        """Mirror NavigationEngine state into legacy _nav_* attributes.
        Called after any successful route fetch so existing code keeps working."""
        self._nav_active         = self._nav.active
        self._nav_route          = self._nav.route
        self._nav_instructions   = self._nav.instructions
        self._nav_step           = self._nav.step
        self._nav_dest_name      = self._nav.dest_name
        self._nav_dest_lat       = self._nav.dest_lat
        self._nav_dest_lon       = self._nav.dest_lon
        self._nav_last_announced = self._nav.last_announced
        self._nav_google_mode    = self._nav.google_mode
        self._nav_route_mode     = self._nav.route_mode
        self._nav_total_min      = self._nav.total_min

    def _nav_route_summary(self, dest_name, provider="Route"):
        instructions = getattr(self, '_nav_instructions', [])
        if not instructions:
            return f"{provider} to {dest_name} ready."
        total_m = int(round(instructions[-1][1])) if len(instructions[-1]) > 1 else 0
        route = getattr(self, '_nav_route', [])
        graph = getattr(self, '_walk_graph', None)
        if route and graph and graph.get("nodes"):
            nodes = graph["nodes"]
            total_m = int(round(sum(
                dist_metres(
                    nodes[route[i - 1]][0], nodes[route[i - 1]][1],
                    nodes[route[i]][0], nodes[route[i]][1],
                )
                for i in range(1, len(route))
            )))
        n_steps = len(instructions)
        n_turns = sum(1 for inst in instructions if "arriving" not in inst[2].lower())
        first = self._nav_format_instruction(0, instructions[0])
        dist_part = f"{total_m}m, " if total_m > 0 else ""
        mode = getattr(self, '_nav_route_mode', 'walking')
        mode_label = "Driving" if mode in ("driving", "car") else "Walking"
        total_min = int(getattr(self, '_nav_total_min', 0) or 0)
        time_part = f"about {total_min} min, " if total_min > 0 else ""
        return (
            f"{first}  {provider} to {dest_name}.  "
            f"{mode_label}, {dist_part}{time_part}"
            f"{n_turns} turn{'s' if n_turns != 1 else ''}, "
            f"{n_steps} step{'s' if n_steps != 1 else ''}.  "
            f"Up for next, Down for previous, I to repeat."
        )

    def _nav_next_instruction_str(self) -> str:
        """Delegate to NavigationEngine."""
        self._nav.active       = getattr(self, '_nav_active', False)
        self._nav.instructions = getattr(self, '_nav_instructions', [])
        self._nav.step         = getattr(self, '_nav_step', 0)
        self._nav.route        = getattr(self, '_nav_route', [])
        return self._nav.next_instruction_str(getattr(self, '_walk_node', None))

    def _nav_announce_step(self):
        """I key — repeat last nav instruction, or announce next if none yet."""
        if not getattr(self, '_nav_active', False):
            self._nav_update_ui("No navigation active. Press G to navigate to an address.")
            return
        last_idx = max(0, min(
            getattr(self, '_nav_step', 1) - 1,
            len(getattr(self, '_nav_instructions', [])) - 1))
        if getattr(self, '_nav_instructions', []):
            self._nav_update_ui(
                self._nav_format_instruction(last_idx, self._nav_instructions[last_idx]))
        else:
            nxt = self._nav_next_instruction_str()
            if nxt:
                self._nav_update_ui(nxt)
            else:
                self._nav_update_ui(f"Heading to {self._nav_dest_name}.")

    def _play_arrival_sound(self):
        """Play a distinct sound on arriving at navigation destination."""
        import numpy as np
        import pygame
        def _gen():
            sr = 44100
            # Three rising tones — celebratory but brief
            segments = []
            for freq, dur in [(440, 0.12), (554, 0.12), (659, 0.25)]:
                t = np.linspace(0, dur, int(sr * dur), False)
                wave = np.sin(2 * np.pi * freq * t) * np.linspace(1, 0.2, len(t))
                segments.append(wave)
            full = np.concatenate(segments)
            audio = (full * 14000).astype(np.int16)
            stereo = np.ascontiguousarray(np.stack([audio, audio], axis=-1))
            snd = pygame.sndarray.make_sound(stereo)
            pygame.mixer.Channel(1).play(snd)
        threading.Thread(target=_gen, daemon=True).start()

    def _nav_arrival_context(self):
        """Focus where navigation left the user after the arrival sound."""
        if not getattr(self, "street_mode", False):
            return
        if getattr(self, "_nav_arrival_provider_mode", False):
            instructions = getattr(self, "_nav_instructions", [])
            final = ""
            if instructions:
                try:
                    final = NavigationEngine._clean_provider_instruction(instructions[-1][2])
                except (TypeError, IndexError):
                    final = ""
            dest_name = getattr(self, "_nav_dest_name", "")
            if final and "arriv" in final.lower():
                msg = final
            elif dest_name:
                msg = f"Arrived at {dest_name}."
            else:
                msg = "Arrived."
            self._nav_arrival_provider_mode = False
            self._nav_update_ui(msg)
            return
        label, cross = self._nearest_road(self.lat, self.lon)
        no_data = ("No street data nearby", "No street data", "Unknown", "")
        if label in no_data:
            msg = "Arrived. Street position unknown."
        elif cross:
            msg = f"Arrived. Near {label} and {cross}."
        else:
            msg = f"Arrived. On {label}."
        self._nav_update_ui(msg)

    def _nav_finish_arrival(self):
        """End navigation and announce final street context after the sound."""
        self._nav_arrival_provider_mode = getattr(self, "_nav_google_mode", False)
        dest_name = getattr(self, "_nav_dest_name", "") or "destination"
        dest_lat = getattr(self, "_nav_dest_lat", None)
        dest_lon = getattr(self, "_nav_dest_lon", None)
        if self._nav_arrival_provider_mode:
            if self._nav_valid_coord(dest_lat, dest_lon):
                self.lat, self.lon = float(dest_lat), float(dest_lon)
            self.last_location_str = dest_name
            self.last_city_found = ""
            if not getattr(self, "street_mode", False):
                self.street_label = ""
            self._suppress_next_location = True
            wx.CallAfter(
                self.map_panel.set_position,
                self.lat,
                self.lon,
                getattr(self, "street_mode", False),
                getattr(self, "street_label", ""),
            )
            threading.Thread(target=self._lookup, daemon=True).start()
        self._nav_active = False
        self._nav_google_mode = False
        wx.CallAfter(self._play_arrival_sound)
        wx.CallLater(200, self._nav_arrival_context)

    def _nav_check_progress(self, current_nid) -> str:
        """Delegate to NavigationEngine. Syncs state and fires arrival sound."""
        self._nav.active       = getattr(self, '_nav_active', False)
        self._nav.instructions = getattr(self, '_nav_instructions', [])
        self._nav.route        = getattr(self, '_nav_route', [])
        self._nav.step         = getattr(self, '_nav_step', 0)
        announced, arrived = self._nav.check_progress(current_nid)
        # Sync state back
        self._nav_step           = self._nav.step
        self._nav_active         = self._nav.active
        self._nav_last_announced = self._nav.last_announced
        if arrived:
            wx.CallAfter(self._play_arrival_sound)
        if announced:
            idx = max(0, min(self._nav_step - 1, len(self._nav_instructions) - 1))
            announced = self._nav_format_instruction(idx, self._nav_instructions[idx])
        return announced


    def _announce_nearest_intersection(self):
        """X key — announce nearest cross street from any mode."""
        # Walking mode — use graph node
        if getattr(self, '_walking_mode', False) and self._walk_graph:
            node = getattr(self, '_walk_node', None)
            street = getattr(self, '_walk_street', None)
            if node and street:
                nlat, nlon = self._walk_graph["nodes"].get(node, (self.lat, self.lon))
                cross = self._walk_get_cross_streets(node, street)
                if cross:
                    msg = f"{street} at {', '.join(cross[:2])}."
                else:
                    msg = f"On {street}, no cross streets nearby."
                poi_text = self._nearby_walk_poi_text(nlat, nlon)
                if poi_text:
                    msg = f"{msg}  {poi_text}"
                self._status_update(msg, force=True)
                return

        # Street mode — announce intersection + nearby POIs from already-loaded grid
        if self.street_mode:
            _NO_DATA = ("No street data nearby", "No street data", "")
            _SUFFIXES = {
                "st": "street", "rd": "road", "ave": "avenue", "dr": "drive",
                "ct": "court", "pl": "place", "cres": "crescent", "cl": "close",
                "blvd": "boulevard", "hwy": "highway", "tce": "terrace",
                "pde": "parade", "esp": "esplanade", "ln": "lane", "gr": "grove",
            }

            def _street_key(name):
                words = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower()).split()
                if words and words[-1] in _SUFFIXES:
                    words[-1] = _SUFFIXES[words[-1]]
                return " ".join(words)

            def _add_cross(candidates, name, distance):
                key = _street_key(name)
                if not key or key == _street_key(label):
                    return
                if key not in candidates or distance < candidates[key][1]:
                    candidates[key] = (name, distance)

            def _street_pair_other(name):
                match = re.match(r"(.+?)\s+near\s+(.+)$", name or "", re.I)
                if not match:
                    return None
                first, second = match.group(1).strip(), match.group(2).strip()
                current_key = _street_key(label)
                if _street_key(first) == current_key:
                    return second
                if _street_key(second) == current_key:
                    return first
                return None

            nearby_roads = self._nearest_roads_with_distances(self.lat, self.lon)
            label, cross = self._nearest_road(self.lat, self.lon)
            pinned = getattr(self, '_jump_street_label', None)
            pin_lat = getattr(self, '_jump_street_pin_lat', None)
            pin_lon = getattr(self, '_jump_street_pin_lon', None)
            if pinned and (pin_lat is None or pin_lon is None or dist_metres(self.lat, self.lon, pin_lat, pin_lon) <= 2.0):
                if label == pinned:
                    pass
                elif cross == pinned:
                    label, cross = pinned, label
                else:
                    label, cross = pinned, None if label in _NO_DATA else label
                road_map = {name: distance for name, distance in nearby_roads}
                if pinned in road_map:
                    nearby_roads = [(pinned, road_map[pinned])] + [
                        item for item in nearby_roads if item[0] != pinned
                    ]
                else:
                    nearby_roads = [(pinned, 0.0)] + nearby_roads
            no_street = not label or label in _NO_DATA
            if no_street:
                announcement = "No street data."
                radius = 300  # widen search in open areas / parks / bridges
            else:
                cross_candidates = {}
                road_map = {name: distance for name, distance in nearby_roads}
                if cross:
                    _add_cross(cross_candidates, cross, road_map.get(cross, float("inf")))
                elif nearby_roads:
                    for name, distance in nearby_roads:
                        _add_cross(cross_candidates, name, distance)
                        if len(cross_candidates) >= 2:
                            break
                radius = self.settings.get("walk_poi_radius_m", 80)
                cross_parts = [
                    f"{name}, {int(round(distance))} metres"
                    for _key, (name, distance) in sorted(
                        cross_candidates.items(), key=lambda item: item[1][1])
                    if math.isfinite(distance)
                ]
                if cross_parts:
                    announcement = "; ".join(cross_parts) + "."
                else:
                    announcement = f"No cross streets nearby. On {label}."

            poi_grid = getattr(self, '_poi_grid', {})
            if poi_grid:
                category  = (self.settings.get("walk_poi_category", "all") or "all").lower()
                show_kind = self.settings.get("walk_announce_category", True)
                pois = self._poi_grid_nearby(self.lat, self.lon, radius)
                if category != "all":
                    pois = filter_pois_by_category(pois, category)
                labels = []
                seen = set()
                for poi in pois:
                    name = (poi.get("label") or poi.get("name") or "").split(",")[0].strip()
                    if not name or name.lower() in seen:
                        continue
                    kind = poi.get("kind", "")
                    plat = poi.get("lat"); plon = poi.get("lon")
                    live_dist = int(math.sqrt(
                        ((self.lat - plat) * 111000) ** 2 +
                        ((self.lon - plon) * 111000 * math.cos(math.radians(self.lat))) ** 2
                    )) if plat is not None else None
                    other_street = None if no_street else _street_pair_other(name)
                    if other_street and live_dist is not None:
                        _add_cross(cross_candidates, other_street, live_dist)
                        seen.add(name.lower())
                        continue
                    seen.add(name.lower())
                    dist_str = f", {live_dist}m" if live_dist is not None else ""
                    if show_kind and kind:
                        labels.append(f"{name}, {kind}{dist_str}")
                    else:
                        labels.append(f"{name}{dist_str}")
                    if len(labels) >= 5:
                        break
                if not no_street:
                    cross_parts = [
                        f"{name}, {int(round(distance))} metres"
                        for _key, (name, distance) in sorted(
                            cross_candidates.items(), key=lambda item: item[1][1])
                        if math.isfinite(distance)
                    ]
                    if cross_parts:
                        announcement = "; ".join(cross_parts) + "."
                    else:
                        announcement = f"No cross streets nearby. On {label}."
                if labels:
                    announcement = f"{announcement}  Nearby: {'; '.join(labels)}."
            elif not getattr(self, '_poi_fetch_in_progress', False):
                announcement = f"{announcement}  No POIs loaded — press P to search."

            self._status_update(announcement, force=True)
            return

        # World map mode
        lat_str = f"{abs(self.lat):.4f} {'North' if self.lat >= 0 else 'South'}"
        lon_str = f"{abs(self.lon):.4f} {'East' if self.lon >= 0 else 'West'}"
        self.update_ui(f"{lat_str}, {lon_str}.")

    def _nav_announce_cross_street(self):
        """X key during navigation — announce nearest cross street."""
        if getattr(self, "_nav_google_mode", False):
            self._status_update(
                "Cross street information is not available for this GPS route.",
                force=True,
            )
            return
        node = getattr(self, '_walk_node', None)
        street = getattr(self, '_walk_street', None)
        if node and street and self._walk_graph:
            cross = self._walk_get_cross_streets(node, street)
            if cross:
                self._status_update(f"Near {street} and {', '.join(cross[:2])}.", force=True)
            else:
                self._status_update(f"On {street}, no cross streets identified nearby.", force=True)
        else:
            # Not in walking mode — use current position and nearest road
            label, cross = self._nearest_road(self.lat, self.lon)
            if cross:
                self._status_update(f"Near {label} and {cross}.", force=True)
            elif label:
                self._status_update(f"On {label}.", force=True)
            else:
                self._status_update("Cross street information not available.", force=True)

    def _nav_step_forward(self):
        """Up arrow during navigation — announce next instruction, silently move position."""
        instructions = getattr(self, '_nav_instructions', [])
        step = getattr(self, '_nav_step', 0)
        if step >= len(instructions):
            self._nav_update_ui(f"You have arrived at {self._nav_dest_name}.")
            self._nav_finish_arrival()
            return
        entry = instructions[step]
        _, _, text = entry[0], entry[1], entry[2]
        # Silently update position if coords present — enables X and A to work
        if len(entry) >= 5:
            if self._nav_valid_coord(entry[3], entry[4]):
                self.lat, self.lon = entry[3], entry[4]
            else:
                miab_log(
                    "navigation",
                    f"Ignored invalid route coordinate: lat={entry[3]!r} lon={entry[4]!r}",
                    self.settings,
                )
        self._nav_step += 1
        self._nav_last_announced = text
        msg = self._nav_format_instruction(step, entry)
        
        if "arriving" in text.lower() or self._nav_step >= len(instructions):
            self._nav_finish_arrival()
        self._nav_update_ui(msg)

    def _nav_step_back(self):
        """Down arrow during navigation — go back to previous instruction."""
        step = getattr(self, '_nav_step', 0)
        if step <= 1:
            instructions = getattr(self, '_nav_instructions', [])
            if instructions:
                self._nav_update_ui(self._nav_format_instruction(0, instructions[0]))
            else:
                self._nav_update_ui("No previous instruction.")
            return
        self._nav_step -= 1
        entry = self._nav_instructions[self._nav_step - 1]
        text = entry[2]
        if len(entry) >= 5:
            if self._nav_valid_coord(entry[3], entry[4]):
                self.lat, self.lon = entry[3], entry[4]
            else:
                miab_log(
                    "navigation",
                    f"Ignored invalid route coordinate: lat={entry[3]!r} lon={entry[4]!r}",
                    self.settings,
                )
        self._nav_last_announced = text
        self._nav_update_ui(
            self._nav_format_instruction(self._nav_step - 1, entry))
