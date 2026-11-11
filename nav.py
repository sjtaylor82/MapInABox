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

from geo import (dist_metres, bearing_deg, bearing_between_nodes, compass_name,
                 GENERIC_STREET_TYPES, INTERNAL_ROAD_LABELS)
from distance_units import format_distance
from route_pois import join_names


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
        self.google_mode    : bool        = False  # True for Google/HERE (no OSM path)
        self.route_mode     : str         = "walking"
        self.total_min      : int         = 0
        # Distinct ways to the same destination, shortest first. Each entry
        # is a dict from _route_option(); use_alternative() switches route.
        self.alternatives   : list[dict]  = []

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
        self.google_mode    = False
        self.route_mode     = "walking"
        self.total_min      = 0
        self.alternatives   = []

    # ------------------------------------------------------------------
    # OSM / Dijkstra routing
    # ------------------------------------------------------------------

    def route_via_waypoints(self, waypoints: list) -> Optional[list]:
        """Build a node path that passes through each (lat, lon) in *waypoints*.

        Snaps each waypoint to the nearest graph node and runs Dijkstra between
        consecutive ones, concatenating the segments.  Used to make an OSM
        description FOLLOW a route the user already chose (e.g. Google's) by
        threading the graph through that route's turn points, rather than taking
        a different shortest path.  Returns the ordered node list, or None.
        """
        if not self._graph or not waypoints:
            return None
        wp_nodes = []
        for lat, lon in waypoints:
            nid, _dist = self._find_nearest_node_with_distance(lat, lon)
            if nid is not None and (not wp_nodes or wp_nodes[-1] != nid):
                wp_nodes.append(nid)
        if len(wp_nodes) < 2:
            return None
        full = [wp_nodes[0]]
        for a, b in zip(wp_nodes, wp_nodes[1:]):
            if a == b:
                continue
            seg = self._dijkstra(a, b)
            if not seg:
                return None
            full.extend(seg[1:])
        return full if len(full) >= 2 else None

    def find_route_osm(
        self,
        from_lat: float,
        from_lon: float,
        to_lat: float,
        to_lon: float,
        dest_name: str,
        travel_mode: str = "walking",
        from_street: Optional[str] = None,
        alternatives: bool = False,
    ) -> tuple[str, bool]:
        """Calculate an OSM pedestrian route and set navigation state.

        ``from_street`` is the street the walker is on. The start is placed on
        that street when it is close, rather than at whichever graph point is
        nearest: beside a building, a laneway behind it can be nearer than the
        street out the front, and the route then began by walking out the back.

        With ``alternatives``, up to two clearly different routes that are not
        much longer are kept in ``self.alternatives`` after the shortest.

        Returns
        -------
        (announcement_str, success)
        """
        if not self._graph:
            return "Walk graph not available.", False

        start_nid, start_snap_m = self._find_nearest_node_with_distance(from_lat, from_lon)
        if from_street:
            on_street, on_street_m = self._find_nearest_node_with_distance(
                from_lat, from_lon, street_filter=from_street)
            if on_street is not None and on_street_m <= self.START_ON_STREET_M:
                start_nid, start_snap_m = on_street, on_street_m
        end_nid, end_snap_m     = self._find_nearest_node_with_distance(to_lat, to_lon)
        if start_nid is None or end_nid is None:
            return "Could not place start or destination on the street graph.", False
        if start_snap_m > 250:
            return (
                f"Current position is {format_distance(start_snap_m)} from the loaded street graph. "
                "Move into the loaded street area or reload streets.",
                False,
            )
        if end_snap_m > 750:
            return (
                f"{dest_name} is {format_distance(end_snap_m)} from the loaded street graph. "
                "OSM local routing cannot safely route there from the current loaded area.",
                False,
            )
        if start_nid == end_nid:
            return "You are already at the destination.", False

        path = self._dijkstra(start_nid, end_nid)
        if not path:
            return f"No walkable route found to {dest_name}.", False

        approximate_dest = end_snap_m > 250
        options = [self._route_option(path, dest_name, approximate_dest)]
        if alternatives:
            options += self._alternative_options(
                start_nid, end_nid, options[0], dest_name, approximate_dest)
        self.alternatives = options if len(options) > 1 else []

        self.dest_name      = dest_name
        self.dest_lat       = to_lat
        self.dest_lon       = to_lon
        self._approx_snap_m = end_snap_m if approximate_dest else 0.0
        return self.use_alternative(0) if self.alternatives else self._apply_option(options[0]), True

    # How far the walker can be from their own street for the start to stay
    # on it; further than this the nearest graph point is used instead.
    START_ON_STREET_M = 60.0
    # An alternative may be at most this much longer than the shortest route,
    # and at least this share of it must be on streets no route already
    # offered uses. Routes to one place often share the last stretch, so
    # this is deliberately not higher.
    ALT_MAX_DETOUR = 1.3
    ALT_MIN_DIFFERENT = 0.30
    ALT_MAX_OPTIONS = 3

    def _path_length(self, path: list[int]) -> float:
        nodes = self._graph["nodes"]
        return sum(
            dist_metres(nodes[a][0], nodes[a][1], nodes[b][0], nodes[b][1])
            for a, b in zip(path, path[1:]))

    def _edge_streets(self) -> dict:
        lookup = {}
        for a, neighbours in self._graph["edges"].items():
            for b, street in neighbours:
                lookup[(a, b)] = street
        return lookup

    def _route_via(self, path: list[int]) -> str:
        """The streets a route mostly follows, in walking order: 'Charles
        Street and Pacific Highway'. Short connecting pieces are left out."""
        nodes = self._graph["nodes"]
        streets = self._edge_streets()
        lengths, first_seen = {}, {}
        for i, (a, b) in enumerate(zip(path, path[1:])):
            name = streets.get((a, b)) or streets.get((b, a)) or ""
            if not name:
                continue
            lengths[name] = lengths.get(name, 0.0) + dist_metres(
                nodes[a][0], nodes[a][1], nodes[b][0], nodes[b][1])
            first_seen.setdefault(name, i)
        top = sorted(lengths, key=lengths.get, reverse=True)[:2]
        top.sort(key=first_seen.get)
        return " and ".join(top)

    def _route_option(self, path: list[int], dest_name: str, approximate: bool) -> dict:
        instructions = self._build_instructions(path, dest_name, approximate=approximate)
        total_m = self._path_length(path)
        n_turns = sum(
            1 for inst in instructions
            if "arriving" not in inst[2].lower()
            and not inst[2].lower().startswith(("continue across ", "continue past "))
        )
        return {
            "path": path,
            "instructions": instructions,
            "total_m": total_m,
            "n_turns": n_turns,
            "total_min": max(1, int(round(total_m / 80.0))) if total_m > 0 else 0,
            "via": self._route_via(path),
        }

    def _shortest_tree(self, source: int, limit_m: float):
        """Distances and predecessors from *source* to every point within
        *limit_m* along the walk graph."""
        nodes, edges = self._graph["nodes"], self._graph["edges"]
        dist, prev, done = {source: 0.0}, {}, set()
        heap = [(0.0, source)]
        while heap:
            d, nid = heapq.heappop(heap)
            if nid in done:
                continue
            done.add(nid)
            if d > limit_m:
                break
            for nb, _street in edges.get(nid, []):
                nd = d + dist_metres(nodes[nid][0], nodes[nid][1],
                                     nodes[nb][0], nodes[nb][1])
                if nd < dist.get(nb, float("inf")):
                    dist[nb], prev[nb] = nd, nid
                    heapq.heappush(heap, (nd, nb))
        return dist, prev

    @staticmethod
    def _tree_path(prev: dict, source: int, node: int) -> list[int]:
        path = [node]
        while path[-1] != source:
            path.append(prev[path[-1]])
        return path[::-1]

    def _alternative_options(self, start_nid, end_nid, best, dest_name, approximate):
        """Clearly different routes that are not much longer.

        Via-point method: for every point, the shortest route that passes
        through it is the shortest way there plus the shortest way on. The
        shortest of those that are mostly on different streets from every
        route already offered, and do not double back, become alternatives.
        """
        limit = best["total_m"] * self.ALT_MAX_DETOUR
        from_start, prev_start = self._shortest_tree(start_nid, limit)
        from_end, prev_end = self._shortest_tree(end_nid, limit)
        candidates = sorted(
            (from_start[v] + from_end[v], v) for v in from_start
            if v in from_end and from_start[v] + from_end[v] <= limit)
        offered, found = [best], []
        for _length, via_node in candidates:
            if len(offered) >= self.ALT_MAX_OPTIONS:
                break
            there = self._tree_path(prev_start, start_nid, via_node)
            onward = self._tree_path(prev_end, end_nid, via_node)[::-1]
            if set(there[:-1]) & set(onward[1:]):
                continue                      # goes out and comes back
            path = there + onward[1:]
            if any(self._shared_share(path, o["path"]) > 1 - self.ALT_MIN_DIFFERENT
                   for o in offered):
                continue
            option = self._route_option(path, dest_name, approximate)
            if not option["via"] or any(option["via"] == o["via"] for o in offered):
                continue
            offered.append(option)
            found.append(option)
        return found

    def _shared_share(self, path: list[int], other: list[int]) -> float:
        """Share of *path*'s length that also lies on *other*."""
        other_edges = set(zip(other, other[1:])) | set(zip(other[1:], other))
        nodes = self._graph["nodes"]
        shared = total = 0.0
        for a, b in zip(path, path[1:]):
            d = dist_metres(nodes[a][0], nodes[a][1], nodes[b][0], nodes[b][1])
            total += d
            if (a, b) in other_edges:
                shared += d
        return shared / total if total else 1.0

    def _apply_option(self, option: dict) -> str:
        instructions = option["instructions"]
        n_steps = len(instructions)
        self.active         = True
        self.route          = option["path"]
        self.instructions   = instructions
        self.step           = 1
        self.google_mode    = False
        self.route_mode     = "walking"
        self.total_min      = option["total_min"]
        n_turns = option["n_turns"]
        first = instructions[0][2] if instructions else ""
        msg = (
            f"Route to {self.dest_name}.  "
            f"{int(option['total_m'])}m, {n_turns} turn{'s' if n_turns != 1 else ''}, "
            f"{n_steps} step{'s' if n_steps != 1 else ''}.  "
            f"Step 1 of {n_steps}: {first}"
        )
        snap = getattr(self, "_approx_snap_m", 0.0)
        if snap:
            msg += (
                f"  The destination is about {int(snap)}m from the nearest "
                "loaded street, so the final approach is approximate."
            )
        return msg

    def use_alternative(self, index: int) -> str:
        """Switch to one of ``self.alternatives`` and return its announcement."""
        return self._apply_option(self.alternatives[index])

    def describe_alternatives(self) -> list[str]:
        """One line per alternative, for a list the walker chooses from."""
        lines = []
        for i, o in enumerate(self.alternatives):
            turns = f"{o['n_turns']} turn{'s' if o['n_turns'] != 1 else ''}"
            extra = o["total_m"] - self.alternatives[0]["total_m"]
            lead = "shortest" if i == 0 else f"{format_distance(extra)} longer"
            lines.append(
                f"Via {o['via']}: {format_distance(o['total_m'])}, about "
                f"{o['total_min']} min, {turns} ({lead})")
        return lines

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
        self.alternatives = []
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
        self.google_mode    = True
        self.route_mode     = travel_mode
        self.total_min      = int(total_min)

        msg = (
            f"Google route to {dest_name}.  "
            f"About {total_m}m, {total_min} min, "
            f"{n_turns} turn{'s' if n_turns != 1 else ''}.  "
            f"Step 1 of {n_steps}: {first}"
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
        self.alternatives = []
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
        self.google_mode    = True
        self.route_mode     = travel_mode
        self.total_min      = int(total_min)

        msg = (
            f"HERE route to {dest_name}.  "
            f"About {total_m}m, {total_min} min, "
            f"{n_turns} turn{'s' if n_turns != 1 else ''}.  "
            f"Step 1 of {n_steps}: {first}"
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
                if "arriving" in text.lower():
                    # Don't deactivate — UI keeps nav alive so the user can
                    # browse the route after arrival; Escape exits.
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
        self.step = min(self.step + 1, len(self.instructions))
        n = len(self.instructions)
        return f"Step {min(self.step, n)} of {n}: {text}"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

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

    # Road classes worth naming when the walker crosses one (OSM highway=*).
    _ROAD_CLASS_NOUNS = {
        "motorway": "motorway",
        "trunk": "highway",
        "primary": "major road",
        "secondary": "main road",
    }

    def _road_description(self, node: int, street: str) -> str:
        """', a 6-lane major road' for a big road at ``node``, else ''.

        Lanes are only given for two-way ways: a one-way way is one
        carriageway of a divided road, and its lanes are not the road's.
        """
        graph = self._graph or {}
        edge_info = graph.get("edge_info", {})
        infos = [edge_info.get(frozenset((node, neighbour)))
                 for neighbour, name in graph.get("edges", {}).get(node, [])
                 if name == street]
        infos = [info for info in infos if info]
        noun = next((self._ROAD_CLASS_NOUNS[kind] for kind in self._ROAD_CLASS_NOUNS
                     if any(info.get("kind") == kind for info in infos)), "")
        lanes = {info["lanes"] for info in infos
                 if info.get("lanes") and not info.get("oneway")}
        lane_text = ""
        if len(lanes) == 1 and next(iter(lanes)) >= 4:
            lane_text = f"{next(iter(lanes))}-lane "
            noun = noun or "road"
        return f", a {lane_text}{noun}" if noun else ""

    def _side_streets_at(self, prev: int, node: int, route_streets: set) -> tuple:
        """Streets meeting the route at ``node``, other than the route's own.

        Returns (through, passed): ``through`` lists streets that carry on
        across the route, so the walker crosses them, each with its road
        description; ``passed`` lists (street, side) for streets that end
        here on one side.  A street ending nearly straight ahead or behind
        has no clear side and is left out.
        """
        graph = self._graph
        nodes, edges = graph["nodes"], graph["edges"]
        route_names = {s.casefold() for s in route_streets if s}
        heading = bearing_between_nodes(nodes, prev, node)
        through, passed = [], []
        for street in sorted(graph["node_streets"].get(node, set()), key=str.casefold):
            folded = street.casefold()
            if (not street or folded in route_names
                    or folded in GENERIC_STREET_TYPES | INTERNAL_ROAD_LABELS):
                continue
            ways = [nb for nb, name in edges.get(node, []) if name == street]
            if len(ways) >= 2:
                through.append(street + self._road_description(node, street))
            elif len(ways) == 1:
                relative = (bearing_between_nodes(nodes, node, ways[0]) - heading
                            + 180) % 360 - 180
                if 30 <= abs(relative) <= 150:
                    passed.append((street, "right" if relative > 0 else "left"))
        return through, passed

    def _build_instructions(
        self,
        node_path: list[int],
        dest_name: str,
        approximate: bool = False,
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
        intersections = graph.get("intersections", set())

        for i in range(1, n - 1):
            leg_dist    += _dist(node_path[i - 1], node_path[i])
            curr_bearing = _bearing(node_path[i], node_path[i + 1])
            diff         = (curr_bearing - prev_bearing + 180) % 360 - 180
            turn         = self._turn_word(diff)
            next_street  = _street_between(node_path[i], node_path[i + 1])

            incoming_street = _street_between(node_path[i - 1], node_path[i])
            if turn != "straight":
                onto = f" onto {next_street}" if next_street else ""
                text = f"{turn}{onto}."
                through = []
                if (node_path[i] in intersections and next_street
                        and incoming_street.casefold() == next_street.casefold()):
                    # A jog in the street where it crosses another one, as
                    # First Avenue does at Aerodrome Road: the walker crosses
                    # that road rather than turning onto anything new.
                    through, _passed = self._side_streets_at(
                        node_path[i - 1], node_path[i], {incoming_street})
                if through:
                    text = (f"cross {join_names(through)}, then {turn} "
                            f"to stay on {next_street}.")
                wlat, wlon = nodes[node_path[i]]
                instructions.append((
                    i, leg_dist,
                    text,
                    wlat, wlon,
                ))
                leg_dist = 0.0
            elif node_path[i] in intersections:
                through, passed = self._side_streets_at(
                    node_path[i - 1], node_path[i], {incoming_street, next_street})
                sentences = []
                if through:
                    sentences.append(f"Continue across {join_names(through)}.")
                for street, side in passed:
                    if sentences:
                        sentences.append(f"{street} is on your {side}.")
                    else:
                        sentences.append(f"Continue past {street} on your {side}.")
                if sentences:
                    wlat, wlon = nodes[node_path[i]]
                    instructions.append((
                        i, leg_dist,
                        "  ".join(sentences),
                        wlat, wlon,
                    ))
                    leg_dist = 0.0
            prev_bearing = curr_bearing

        leg_dist += _dist(node_path[-2], node_path[-1])
        last       = node_path[-1]
        last_name  = next(iter(node_streets.get(last, set())), "destination")
        alat, alon = nodes[last]
        instructions.append((
            n - 1, leg_dist,
            (f"Arriving near {dest_name} on {last_name}."
             if approximate else f"Arriving at {dest_name} on {last_name}."),
            alat, alon,
        ))
        return instructions

    # ------------------------------------------------------------------
    # Route digest — structured data for narrative-AI directions
    # ------------------------------------------------------------------

    # Countries where pedestrians/vehicles drive on the left. ISO 3166-1
    # alpha-2 codes. Used to phrase "traffic on your left moves in the same
    # direction" correctly in narrative briefings.
    _DRIVE_LEFT_COUNTRIES = frozenset({
        "ag", "au", "bb", "bd", "bn", "bs", "bt", "bw", "cy", "dm", "fj",
        "gb", "gd", "gy", "hk", "id", "ie", "in", "jm", "jp", "ke", "kn",
        "lc", "lk", "ls", "mo", "mt", "mu", "mv", "mw", "my", "mz", "na",
        "np", "nz", "pg", "pk", "sc", "sg", "sr", "sz", "tt", "tz", "ug",
        "vc", "za", "zm", "zw",
    })

    @staticmethod
    def _classify_side(point_lat: float, point_lon: float,
                       node_lat: float, node_lon: float,
                       bearing: float) -> Optional[str]:
        """Which side of a leg the point sits on, relative to a walker at
        (node_lat, node_lon) heading along ``bearing``.

        Returns 'left' | 'right' | 'ahead' | 'behind' | None.
        """
        try:
            bd = bearing_deg(node_lat, node_lon, point_lat, point_lon)
        except (TypeError, ValueError):
            return None
        rel = (bd - bearing + 180) % 360 - 180
        if abs(rel) < 25:   return "ahead"
        if abs(rel) > 155:  return "behind"
        return "left" if rel < 0 else "right"

    def build_route_digest(
        self,
        origin_label: str = "",
        origin_lat: Optional[float] = None,
        origin_lon: Optional[float] = None,
        country_code: str = "",
    ) -> Optional[dict]:
        """Produce a structured digest of the current OSM route.

        Only works for OSM routes (where ``self.route`` is a node path through
        the walk graph). Returns ``None`` for Google/HERE — those providers
        give us per-step text but no traversable node list, so we cannot list
        the cross streets passed within a leg.

        Output shape (all street names are taken verbatim from the OSM graph;
        callers must never permit a downstream AI to invent new names):

            {
              "origin":      {"lat", "lon", "label"},
              "destination": {"lat", "lon", "label", "side"},
              "total_distance_m": int,
              "total_minutes":    int,
              "legs": [
                {
                  "street":               "Carrington Road",
                  "compass":              "north",
                  "bearing_deg":          5.2,
                  "distance_m":           312,
                  "cross_streets_passed": ["Pine Street", "Church Street"],
                  "end_action": {
                    "turn":          "right" | "left" | "straight" | "arrive" | ...,
                    "onto":          "Bronte Road" | None,
                    "junction_type": "T-junction" | "4-way intersection" | ...,
                  },
                },
                ...
              ],
            }
        """
        if not self.active or self.google_mode or not self.route or not self._graph:
            return None

        graph         = self._graph
        nodes         = graph["nodes"]
        edges         = graph["edges"]
        node_streets  = graph["node_streets"]
        intersections = graph.get("intersections", set())
        path = self.route
        n = len(path)
        if n < 2:
            return None

        def _street_between(a, b):
            for nb, sname in edges.get(a, []):
                if nb == b:
                    return sname
            return ""

        def _bearing(a, b):
            return bearing_between_nodes(nodes, a, b)

        def _classify_junction(node_id, leg_street):
            branches = len({nb for nb, _ in edges.get(node_id, [])})
            # The leg's street continues past this node iff it appears on
            # at least 2 edges here (one is the one we arrived on).
            leg_edge_count = sum(
                1 for _, s in edges.get(node_id, []) if s == leg_street
            ) if leg_street else 0
            leg_continues = leg_edge_count >= 2
            if branches >= 5:
                return "complex intersection"
            if branches == 4:
                return "4-way intersection"
            if branches == 3:
                if leg_street and not leg_continues:
                    return "T-junction"
                return "3-way intersection"
            return "intersection"

        legs = []
        leg_start_idx     = 0
        leg_street        = _street_between(path[0], path[1])
        leg_start_bearing = _bearing(path[0], path[1])
        leg_dist          = 0.0
        leg_cross         = []
        prev_bearing      = leg_start_bearing

        def _flush(turn, onto, junction_node, end_idx):
            jtype = (_classify_junction(junction_node, leg_street)
                     if junction_node is not None else None)
            s_lat, s_lon = nodes[path[leg_start_idx]]
            legs.append({
                "street":               leg_street or "unnamed road",
                "compass":              compass_name(leg_start_bearing),
                "bearing_deg":          round(leg_start_bearing, 1),
                "distance_m":           int(round(leg_dist)),
                "distance_display":     format_distance(leg_dist),
                "cross_streets_passed": leg_cross[:],
                "node_start":           leg_start_idx,
                "node_end":             end_idx,
                "start_lat":            s_lat,
                "start_lon":            s_lon,
                "end_action": {
                    "turn":          turn,
                    "onto":          onto,
                    "junction_type": jtype,
                },
            })

        def _record_cross(mid, mid_idx, arriving_bearing, cur_street, dest_list):
            """Append the named cross streets meeting the route at *mid* to
            *dest_list*, with the side they sit relative to travel."""
            if mid not in intersections:
                return
            sides_by_street: dict[str, set[str]] = {}
            nlat, nlon = nodes[mid]
            for nb, sname in edges.get(mid, []):
                if not sname or sname == cur_street:
                    continue
                if sname.lower() in GENERIC_STREET_TYPES | INTERNAL_ROAD_LABELS:
                    continue
                blat, blon = nodes[nb]
                br = bearing_deg(nlat, nlon, blat, blon)
                rel = (br - arriving_bearing + 180) % 360 - 180
                if abs(rel) < 25:
                    s_side = "ahead"
                elif abs(rel) > 155:
                    s_side = "behind"
                elif rel < 0:
                    s_side = "left"
                else:
                    s_side = "right"
                sides_by_street.setdefault(sname, set()).add(s_side)
            existing = {c["name"] for c in dest_list}
            for name in sorted(sides_by_street):
                if name in existing:
                    continue
                s = sides_by_street[name]
                if "left" in s and "right" in s:
                    side = "left-right"
                elif "left" in s:
                    side = "left"
                elif "right" in s:
                    side = "right"
                elif "ahead" in s:
                    side = "ahead"
                elif "behind" in s:
                    side = "behind"
                else:
                    side = None
                dest_list.append({"name": name, "side": side, "node_index": mid_idx})
                existing.add(name)

        for i in range(1, n):
            from_nid = path[i - 1]
            to_nid   = path[i]
            seg_dist    = dist_metres(*nodes[from_nid], *nodes[to_nid])
            seg_bearing = _bearing(from_nid, to_nid)
            seg_street  = _street_between(from_nid, to_nid)

            if i == 1:
                leg_dist     = seg_dist
                prev_bearing = seg_bearing
                continue

            # Decide whether node path[i-1] is a turn point.
            diff = (seg_bearing - prev_bearing + 180) % 360 - 180
            street_changed = bool(seg_street and leg_street and seg_street != leg_street)
            is_turn = abs(diff) >= 20 or street_changed

            mid_node = path[i - 1]

            if is_turn:
                turn_word = self._turn_word(diff)
                if street_changed and turn_word == "straight":
                    turn_word = "continue"
                onto = seg_street if street_changed else None
                _flush(turn_word, onto, mid_node, i - 1)
                # Start new leg from this turn node
                leg_start_idx     = i - 1
                leg_street        = seg_street
                leg_start_bearing = seg_bearing
                leg_dist          = seg_dist
                leg_cross         = []
                # A bend that stays on the same road still passes an
                # intersection (e.g. Burwood Rd curving at Church St); record
                # its cross streets so it remains a real boundary, not lost.
                if not street_changed:
                    _record_cross(mid_node, i - 1, prev_bearing, leg_street, leg_cross)
            else:
                # Continuing through a (possibly cross-) intersection — record
                # the cross streets with the side they sit, relative to travel.
                _record_cross(mid_node, i - 1, prev_bearing, leg_street, leg_cross)
                leg_dist += seg_dist

            prev_bearing = seg_bearing

        # Final leg — arrival at path[-1]
        _flush("arrive", None, None, n - 1)

        # Destination side, relative to the final leg's direction of travel.
        dest_side = None
        if legs:
            end_node_lat, end_node_lon = nodes[path[-1]]
            dest_side = self._classify_side(
                self.dest_lat, self.dest_lon,
                end_node_lat, end_node_lon,
                legs[-1]["bearing_deg"],
            )

        # Origin side, relative to leg 0's direction of travel. Only computed
        # when the caller passed real address coordinates (the actual
        # property), not the graph-snapped node.
        origin_side = None
        if legs and origin_lat is not None and origin_lon is not None:
            start_node_lat, start_node_lon = nodes[path[0]]
            origin_side = self._classify_side(
                float(origin_lat), float(origin_lon),
                start_node_lat, start_node_lon,
                legs[0]["bearing_deg"],
            )

        drives_on = None
        cc = (country_code or "").strip().lower()
        if cc:
            drives_on = "left" if cc in self._DRIVE_LEFT_COUNTRIES else "right"

        # The road is always on the opposite side of the walker from where
        # the property sits. This is the single most important orientation
        # cue for a blind walker.
        origin_road_side = None
        if origin_side == "right":
            origin_road_side = "left"
        elif origin_side == "left":
            origin_road_side = "right"

        # Which way traffic in the nearest lane flows relative to the
        # walker. Derived from drive-side + road-side:
        #
        #   drives_on=left:  road on right → nearest lane = with you;
        #                    road on left  → nearest lane = toward you.
        #   drives_on=right: mirror image.
        origin_traffic_nearest = None
        if drives_on and origin_road_side:
            if drives_on == "left":
                origin_traffic_nearest = (
                    "with_you" if origin_road_side == "right" else "toward_you"
                )
            else:  # drives_on == "right"
                origin_traffic_nearest = (
                    "with_you" if origin_road_side == "left" else "toward_you"
                )

        # Does the walker have to cross the carriageway to reach the
        # destination?  The road sits on the opposite side of the walker from
        # their footpath, so the destination is reachable WITHOUT crossing only
        # when it is on the footpath side — i.e. NOT on the road side.  Hence
        # crossing is needed exactly when the destination sits on the road side.
        #
        # CAVEAT: origin_road_side is measured on leg 0, while dest_side is
        # measured on the final leg.  Those frames only agree when the route has
        # not made a net turn between them — after a turn the relative road side
        # can flip, so the comparison would be unsafe.  Only assert a crossing
        # when the final leg still runs roughly parallel to the first; otherwise
        # leave it unknown and let the narrator fall back to a plain
        # "on your left/right as you approach", which never invents a crossing.
        crossing_needed = None
        if origin_road_side and dest_side in ("left", "right") and legs:
            net_turn = abs(((legs[-1]["bearing_deg"] - legs[0]["bearing_deg"]
                             + 180) % 360) - 180)
            if net_turn <= 35:
                crossing_needed = (dest_side == origin_road_side)

        # Mark whether the walker physically crosses each passed side street.
        # A side street is crossed only when it opens onto the walker's own
        # footpath — the side OPPOSITE the road.  This needs the road side in the
        # cross street's leg frame, which only matches the leg-0 road side while
        # that leg has not turned away from leg 0; otherwise leave it unknown so
        # the narrator makes no crossing claim.
        base_bearing = legs[0]["bearing_deg"] if legs else 0.0
        footpath_side = None
        if origin_road_side:
            footpath_side = "left" if origin_road_side == "right" else "right"
        for leg in legs:
            net = abs(((leg["bearing_deg"] - base_bearing + 180) % 360) - 180)
            aligned = net <= 35
            for cs in leg.get("cross_streets_passed", []):
                side = cs.get("side")
                if not footpath_side or not aligned or side not in ("left", "right", "left-right"):
                    cs["crossed"] = None
                elif side == "left-right":
                    cs["crossed"] = True
                else:
                    cs["crossed"] = (side == footpath_side)

        total_m = sum(leg["distance_m"] for leg in legs)
        return {
            "origin": {
                "lat":                  nodes[path[0]][0],
                "lon":                  nodes[path[0]][1],
                "label":                origin_label or "current position",
                "side":                 origin_side,
                "road_side":            origin_road_side,
                "traffic_nearest_lane": origin_traffic_nearest,
            },
            "destination": {
                "lat":              self.dest_lat,
                "lon":              self.dest_lon,
                "label":            self.dest_name,
                "side":             dest_side,
                "crossing_needed":  crossing_needed,
            },
            "country": {
                "code":      cc.upper() if cc else None,
                "drives_on": drives_on,
            },
            "total_distance_m": total_m,
            "total_distance_display": format_distance(total_m),
            "total_minutes":    self.total_min,
            "legs":             legs,
        }

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
from route_pois import describe_pois_along, describe_sides

_NAV_ARRIVAL_CONTEXT_DELAY_MS = 2500


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
        try:
            self.update_ui(msg, force=True)
        except TypeError:
            self.update_ui(msg)

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
            out = f"In {format_distance(dist)}, {text[0].lower() + text[1:]}"
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

    def _nav_step_leg(self, idx):
        """The stretch walked for route step ``idx``: from the previous step's
        point to this one.  Returns (path, polyline, streets, graph), or None
        when the route keeps no node path (Google and HERE routes)."""
        route = getattr(self, "_nav_route", []) or []
        graph = getattr(getattr(self, "_nav", None), "_graph", None) \
            or getattr(self, "_walk_graph", None) or {}
        nodes = graph.get("nodes", {})
        instructions = getattr(self, "_nav_instructions", [])
        if not route or not nodes:
            return None
        try:
            start = int(instructions[idx - 1][0]) if idx > 0 else 0
            path = route[start:int(instructions[idx][0]) + 1]
        except (TypeError, ValueError, IndexError):
            return None
        if len(path) < 2 or any(node not in nodes for node in path):
            return None
        polyline = [tuple(nodes[node]) for node in path]
        edges = graph.get("edges", {})
        streets = [next((name for neighbour, name in edges.get(a, []) if neighbour == b), "")
                   for a, b in zip(path, path[1:])]
        return path, polyline, streets, graph

    def _nav_leg_pois(self, polyline):
        """Loaded POIs near a stretch.  Samples every 50 m; a 60 m search
        radius then covers the 40 m either side that route_pois considers."""
        pois, seen = [], set()
        for (alat, alon), (blat, blon) in zip(polyline, polyline[1:]):
            samples = max(1, int(dist_metres(alat, alon, blat, blon) // 50) + 1)
            for k in range(samples + 1):
                t = k / samples
                for poi in self._poi_grid_nearby(alat + (blat - alat) * t,
                                                 alon + (blon - alon) * t, 60):
                    if id(poi) not in seen:
                        seen.add(id(poi))
                        pois.append(poi)
        return pois

    def _nav_step_poi_text(self, idx, entry):
        """Places passed during a route step, in order and by side.

        MIAB's own walking routes keep the node path between turns, so the
        step says "You'll pass KFC on your left, then ..." and leaves out any
        place whose order or side is doubtful.  Google and HERE routes keep
        no step geometry, so they keep the nearby list at the step's point.
        """
        leg = self._nav_step_leg(idx)
        if leg is None:
            if len(entry) >= 5 and self._nav_valid_coord(entry[3], entry[4]):
                return self._nearby_walk_poi_text(entry[3], entry[4], prefix="Nearby")
            return ""
        if not self.settings.get("walk_announce_pois"):
            return ""
        _path, polyline, streets, _graph = leg
        pois = self._nav_leg_pois(polyline)
        category = (self.settings.get("walk_poi_category", "all") or "all").lower()
        if category != "all":
            pois = filter_pois_by_category(pois, category)
        return describe_pois_along(polyline, streets, pois,
                                   getattr(self, "_address_points", []))

    def _nav_stretch_summary(self, idx):
        """Shift+I — a description of one stretch from MIAB's own data.

        "First Avenue, a main road, 108 metres from Aerodrome Road to George
        Street.  On your left: KFC.  On your right: ...  At the end, right
        onto George Street."  Empty for routes without a node path.
        """
        leg = self._nav_step_leg(idx)
        if leg is None:
            return ""
        path, polyline, streets, graph = leg
        entry = self._nav_instructions[idx]
        names = []
        for street in streets:
            if street and (not names or names[-1].casefold() != street.casefold()):
                names.append(street)
        excluded = {n.casefold() for n in names} | GENERIC_STREET_TYPES | INTERNAL_ROAD_LABELS

        def others(node):
            return sorted((s for s in graph.get("node_streets", {}).get(node, set())
                           if s and s.casefold() not in excluded), key=str.casefold)

        length = sum(dist_metres(a[0], a[1], b[0], b[1])
                     for a, b in zip(polyline, polyline[1:]))
        road = self._nav._road_description(path[0], names[0]) if names else ""
        opening = f"{' then '.join(names) or 'This stretch'}{road}, {format_distance(int(round(length)))}"
        arrival = "arriv" in str(entry[2]).lower()
        if idx == 0:
            opening += " from your start"
        elif others(path[0]):
            opening += " from " + join_names(others(path[0]))
        if arrival:
            opening += f" to {getattr(self, '_nav_dest_name', '') or 'your destination'}"
        elif others(path[-1]):
            opening += " to " + join_names(others(path[-1]))
        sides = describe_sides(polyline, streets, self._nav_leg_pois(polyline),
                               getattr(self, "_address_points", []))
        if not sides:
            sides = "No places are recorded along this stretch."
        text = NavigationEngine._clean_provider_instruction(str(entry[2]))
        if arrival:
            ending = f"Then you arrive at {getattr(self, '_nav_dest_name', '') or 'your destination'}."
        else:
            ending = f"At the end, {text[:1].lower() + text[1:]}"
        return f"{opening}.  {sides}  {ending}"

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

        ndlg = wx.TextEntryDialog(self, f"Number on {street}:", "Navigate — Number")
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
                        miab_log("errors", f"[Nav] HERE geocode failed: {e}", None)
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
            dlg = POICategoryDialog(self)
            if dlg.ShowModal() != wx.ID_OK or not dlg.selected_key:
                dlg.Destroy()
                self.listbox.SetFocus()
                return
            category = dlg.selected_key
            name     = dlg.selected_name
            dlg.Destroy()
            self._status_update("Loading points of interest...")
            def _fetch_then_pick():
                self._fetch_pois(category, name_filter=name)
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
            already_there = "already at the destination" in str(msg).lower()
            if target_source == "poi":
                full_msg = (
                    msg if already_there else
                    f"OpenStreetMap could not calculate a route to {dest_name}. "
                    f"{msg} {suggestion}"
                )
                wx.CallAfter(self._nav_update_ui, full_msg)
                return
            if not already_there and suggestion not in str(msg):
                msg = f"{msg} {suggestion}"
            wx.CallAfter(self._nav_update_ui, msg)

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
        from_street = (getattr(self, "street_label", "") or "").strip() or None
        msg, ok = self._nav.find_route_osm(
            self.lat, self.lon, dest_lat, dest_lon, dest_name,
            from_street=from_street, alternatives=True)
        if ok:
            self._sync_nav_state_from_engine()
            miab_log(
                "navigation",
                f"OSM route started: target={dest_name!r} from_street={from_street!r} "
                f"steps={len(self._nav_instructions)} "
                f"options={len(self._nav.alternatives) or 1}",
                self.settings,
            )
        else:
            fail(str(msg or "No route was returned."), detail=msg)
            return
        if len(self._nav.alternatives) > 1:
            wx.CallAfter(self._nav_choose_route, dest_name)
            return
        wx.CallAfter(self._nav_begin_route, dest_name, "Route")

    def _nav_choose_route(self, dest_name):
        """More than one sensible way: let the walker pick before step one.

        Enter takes the highlighted route; Escape takes the shortest, so
        dismissing the list never leaves the walker without directions.
        """
        lines = self._nav.describe_alternatives()
        dlg = wx.SingleChoiceDialog(
            self,
            f"There are {len(lines)} ways to {dest_name}. Choose one, or press "
            "Escape for the shortest.",
            "Choose a route", lines)
        dlg.SetSelection(0)
        try:
            index = dlg.GetSelection() if dlg.ShowModal() == wx.ID_OK else 0
        finally:
            dlg.Destroy()
        self._nav.use_alternative(max(0, index))
        self._sync_nav_state_from_engine()
        miab_log("navigation",
                 f"OSM route chosen: option {index + 1} of {len(lines)}: {lines[index]}",
                 self.settings)
        self._nav_begin_route(dest_name, "Route")

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
                wx.CallAfter(self._nav_begin_route, dest_name, "Google route")
                return
            wx.CallAfter(self._nav_update_ui, msg)
        except Exception as exc:
            miab_log("errors", f"[Nav] Google routing failed: {exc}", getattr(self, "settings", None))
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
                wx.CallAfter(self._nav_begin_route, dest_name, "HERE route")
                return
            wx.CallAfter(self._nav_update_ui, msg)
        except urllib.error.HTTPError as exc:
            miab_log("errors", f"[Nav] HERE routing HTTP {exc.code}", getattr(self, "settings", None))
            if not allow_osm_fallback:
                wx.CallAfter(self._nav_update_ui,
                    f"HERE could not route to {dest_name}.")
                return
            wx.CallAfter(self._nav_update_ui,
                f"HERE routing error ({exc.code}). Falling back to OSM.")
            self._nav_start(dest_lat, dest_lon, dest_name, target_source)
        except Exception as exc:
            miab_log("errors", f"[Nav] HERE routing failed: {exc}", getattr(self, "settings", None))
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
        self._nav_arrived        = False
        self._nav_route          = self._nav.route
        self._nav_instructions   = self._nav.instructions
        self._nav_step           = self._nav.step
        self._nav_dest_name      = self._nav.dest_name
        self._nav_dest_lat       = self._nav.dest_lat
        self._nav_dest_lon       = self._nav.dest_lon
        self._nav_google_mode    = self._nav.google_mode
        self._nav_route_mode     = self._nav.route_mode
        self._nav_total_min      = self._nav.total_min
        wx.CallAfter(self._set_nav_button_visible, True)

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
        n_turns = sum(
            1 for inst in instructions
            if "arriving" not in inst[2].lower()
            and not inst[2].lower().startswith(("continue across ", "continue past "))
        )
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
            f"{n_steps} step{'s' if n_steps != 1 else ''}."
        )

    def _nav_begin_route(self, dest_name, provider="Route"):
        """Enter step one immediately and announce it without stale UI text."""
        instructions = getattr(self, '_nav_instructions', [])
        if not instructions:
            self._nav_update_ui(f"{provider} to {dest_name} ready.")
            return

        entry = instructions[0]
        if len(entry) >= 5 and self._nav_valid_coord(entry[3], entry[4]):
            self.lat, self.lon = entry[3], entry[4]
            if hasattr(self, "map_panel"):
                self.map_panel.set_position(
                    self.lat, self.lon, True, getattr(self, "street_label", ""))
        elif len(entry) >= 5:
            miab_log(
                "navigation",
                f"Ignored invalid initial route coordinate: lat={entry[3]!r} lon={entry[4]!r}",
                self.settings,
            )

        # Step one is now current. Up advances to step two; Down and I can
        # still repeat step one.
        self._nav_step = 1
        self._nav.step = 1
        message = self._nav_route_summary(dest_name, provider=provider)
        # Any background/live POI request that began before navigation is now
        # stale; its completion must not re-announce the destination POI.
        self._poi_context_generation = getattr(
            self, "_poi_context_generation", 0) + 1
        self._nav_say_first_instruction(message)

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
            self._nav_update_ui("No navigation active. Press Ctrl+G to navigate to an address.")
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

    def _nav_request_route_summary(self):
        """Run the stretch summary after the current UI event returns."""
        wx.CallAfter(self._nav_shift_i_summary)

    def _nav_shift_i_summary(self):
        """Shift+I — describe the stretch you are on, from MIAB's own data.

        Pressing again on the same stretch steps through the whole route,
        stretch by stretch: Up/Down move, I repeats, Escape closes and the
        route stays active.  Only MIAB walking routes keep the street shape
        this needs.
        """
        instructions = getattr(self, "_nav_instructions", []) or []
        if not getattr(self, "_nav_active", False) or not instructions:
            self._announce_transient("No route is active.")
            return
        if not getattr(self, "_nav_route", None):
            self._announce_transient(
                "Stretch summaries need a Map in a Box walking route. "
                "Google and HERE routes do not include the street shape.")
            return
        idx = max(0, min(getattr(self, "_nav_step", 1) - 1, len(instructions) - 1))
        shown = (idx, id(instructions))
        if getattr(self, "_nav_summary_shown_for", None) == shown:
            self._nav_summary_shown_for = None
            steps = [self._nav_stretch_summary(i) for i in range(len(instructions))]
            self._nav_briefing_enter([step for step in steps if step], title="Route summary")
            return
        text = self._nav_stretch_summary(idx)
        if not text:
            self._announce_transient("No summary is available for this stretch.")
            return
        self._nav_summary_shown_for = shown
        self._nav_update_ui(text)

    def _nav_briefing_enter(self, steps: list[str], title: str = "Briefing"):
        """Begin step-through of a freshly loaded briefing."""
        self._nav_briefing_title = title
        self._nav_briefing_mode  = True
        self._nav_briefing_steps = steps
        self._nav_briefing_step  = 0
        self._nav_briefing_announce_current(intro=True)

    def _nav_briefing_exit(self, announce: bool = True):
        """Leave briefing mode; route stays active for normal step-through."""
        was_on = getattr(self, '_nav_briefing_mode', False)
        self._nav_briefing_mode  = False
        self._nav_briefing_steps = []
        self._nav_briefing_step  = 0
        if announce and was_on:
            self._announce_transient(
                "Briefing closed. Up and Down step through the route.")

    def _nav_briefing_announce_current(self, intro: bool = False):
        steps = getattr(self, '_nav_briefing_steps', [])
        idx   = getattr(self, '_nav_briefing_step', 0)
        if not steps:
            self._announce_transient("No briefing loaded.")
            return
        idx = max(0, min(idx, len(steps) - 1))
        self._nav_briefing_step = idx
        prefix = f"{getattr(self, '_nav_briefing_title', 'Briefing')}.  " if intro else ""
        msg = f"{prefix}Step {idx + 1} of {len(steps)}.  {steps[idx]}"
        self._nav_update_ui(msg)

    def _nav_briefing_next(self):
        steps = getattr(self, '_nav_briefing_steps', [])
        if not steps:
            return
        if self._nav_briefing_step >= len(steps) - 1:
            self._announce_transient(
                "End of briefing.")
            return
        self._nav_briefing_step += 1
        self._nav_briefing_announce_current()

    def _nav_briefing_prev(self):
        steps = getattr(self, '_nav_briefing_steps', [])
        if not steps:
            return
        if self._nav_briefing_step <= 0:
            self._announce_transient(
                "Start of briefing.")
            return
        self._nav_briefing_step -= 1
        self._nav_briefing_announce_current()

    def _play_arrival_sound(self):
        """Play a distinct sound on arriving at navigation destination."""
        import numpy as np
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
            self.sound.play_array_on_channel(stereo, 1)
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
        self._nav_update_ui(self._nav_arrival_street_text())

    def _nav_arrival_street_text(self):
        """'Arrived. Near George Street and Maud Street.' for a MIAB route."""
        label, cross = self._nearest_road(self.lat, self.lon)
        no_data = ("No street data nearby", "No street data", "Unknown", "")
        if label in no_data:
            return "Arrived. Street position unknown."
        if cross:
            return f"Arrived. Near {label} and {cross}."
        return f"Arrived. On {label}."

    def _nav_record_destination(self):
        """Add the route's destination to the address history (Ctrl+A)."""
        record = getattr(self, "_record_address", None)
        if record is None:
            return
        name = getattr(self, "_nav_dest_name", "") or ""
        lat = getattr(self, "_nav_dest_lat", None)
        lon = getattr(self, "_nav_dest_lon", None)
        if not self._nav_valid_coord(lat, lon):
            lat, lon = self.lat, self.lon
        pin = getattr(self, "_nav_street_pin", None)
        street = pin[2] if pin and (pin[0], pin[1]) == (self.lat, self.lon) else ""
        if not street and getattr(self, "street_mode", False):
            label, _cross = self._nearest_road(float(lat), float(lon))
            if label not in ("", "Unknown", "No street data", "No street data nearby"):
                street = label
        number = ""
        if street and hasattr(self, "_nearest_address_number"):
            number = self._nearest_address_number(float(lat), float(lon), street, radius=60) or ""
        record(number, street, lat, lon, name=name)

    def _nav_finish_arrival(self, context_later=True):
        """Fire arrival sound + final context the first time the user reaches
        the destination. Navigation stays active so the user can keep stepping
        back and forward through the route; Escape exits."""
        if getattr(self, '_nav_arrived', False):
            # Already arrived once — don't replay the sound or re-run lookup;
            # the user is just hitting Up at the end again.
            return
        self._nav_arrived = True
        self._nav_arrival_provider_mode = getattr(self, "_nav_google_mode", False)
        self._nav_record_destination()
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
            wx.CallAfter(
                self.map_panel.set_position,
                self.lat,
                self.lon,
                getattr(self, "street_mode", False),
                getattr(self, "street_label", ""),
            )
            threading.Thread(target=self._lookup, daemon=True).start()
        wx.CallAfter(self._play_arrival_sound)
        if context_later:
            # Let the final turn instruction finish speaking before the
            # secondary GPS/street context uses the same speech channel.
            wx.CallLater(_NAV_ARRIVAL_CONTEXT_DELAY_MS, self._nav_arrival_context)

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
        if arrived:
            wx.CallAfter(self._play_arrival_sound)
            self._nav_record_destination()
        if announced:
            idx = max(0, min(self._nav_step - 1, len(self._nav_instructions) - 1))
            announced = self._nav_format_instruction(idx, self._nav_instructions[idx])
            if not arrived:
                entry = self._nav_instructions[idx]
                if len(entry) >= 5 and self._nav_valid_coord(entry[3], entry[4]):
                    poi_text = self._nav_step_poi_text(idx, entry)
                    if poi_text:
                        announced = f"{announced}  {poi_text}"
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

            def _nearest_side_or_cross_street_text():
                if not hasattr(self, "_street_survey_current_street"):
                    return ""
                street = self._street_survey_current_street()
                if not street:
                    return ""
                context_resolver = getattr(
                    self, "_street_survey_intersection_context_street", None)
                if callable(context_resolver):
                    physical_street = context_resolver(street)
                    if not physical_street:
                        return "Cross street information is not available here."
                    if _street_key(physical_street) != _street_key(street):
                        physical_here = self._street_survey_project(
                            physical_street, self.lat, self.lon)
                        if physical_here:
                            metres = int(round(physical_here[0]))
                            direction = compass_name(bearing_deg(
                                self.lat, self.lon,
                                physical_here[2], physical_here[3]
                            )).lower()
                            return (
                                f"{physical_street} {format_distance(metres)} "
                                f"{direction}."
                            )
                        return f"{physical_street}."
                intersections = self._street_survey_intersections(street)
                here = self._street_survey_project(street, self.lat, self.lon)
                if not intersections or not here:
                    return ""

                axis = self._street_survey_address_axis(street)
                if axis:
                    _lat0, _lon0, ux, uy, _scale_x = axis
                    heading = (math.degrees(math.atan2(ux, uy)) + 360) % 360
                    here_along = self._street_survey_axis_value(
                        axis, here[2], here[3])
                else:
                    heading = getattr(self, "_road_heading", None)
                    if heading is None:
                        heading = bearing_deg(self.lat, self.lon, here[2], here[3])
                    here_along = here[1]
                my_side = self._nav._classify_side(
                    self.lat, self.lon, here[2], here[3], heading)

                graph = self._walk_graph or {}
                nodes = graph.get("nodes", {})
                edges = graph.get("edges", {})
                current_key = _street_key(street)
                candidates = []

                def _cross_names(nid):
                    names = []
                    seen = set()
                    for _neighbour, street_name in edges.get(nid, []):
                        name = (street_name or "").strip()
                        key = _street_key(name)
                        if not key or key == current_key or key in seen:
                            continue
                        seen.add(key)
                        names.append(name)
                    return sorted(names, key=str.lower)

                def _junction_type(nid):
                    node_edges = edges.get(nid, [])
                    neighbours = list({
                        neighbour for neighbour, _name in node_edges
                        if neighbour in nodes
                    })
                    branches = len(neighbours)
                    bearings = [
                        bearing_deg(
                            nodes[nid][0], nodes[nid][1],
                            nodes[neighbour][0], nodes[neighbour][1])
                        for neighbour in neighbours
                    ] if nid in nodes else []

                    def opposite(a, b):
                        separation = abs((a - b + 180) % 360 - 180)
                        return separation >= 145.0

                    if branches >= 5:
                        return f"{branches}-way intersection"
                    if branches == 4:
                        paired = all(any(
                            index != other and opposite(bearing, bearings[other])
                            for other in range(len(bearings))
                        ) for index, bearing in enumerate(bearings))
                        return ("4-way intersection" if paired
                                else "complex intersection")
                    if branches == 3:
                        has_opposite_pair = any(
                            opposite(bearings[a], bearings[b])
                            for a in range(len(bearings))
                            for b in range(a + 1, len(bearings))
                        )
                        return ("T-junction" if has_opposite_pair
                                else "3-way intersection")
                    return "intersection"

                nearest_here = None
                for along, nid, nlat, nlon in intersections:
                    real_distance = dist_metres(self.lat, self.lon, nlat, nlon)
                    if real_distance > 250:
                        continue
                    cross_names = _cross_names(nid)
                    if real_distance <= 35 and cross_names:
                        if nearest_here is None or real_distance < nearest_here[0]:
                            nearest_here = (real_distance, nid, cross_names)
                    intersection_bearing = bearing_deg(self.lat, self.lon, nlat, nlon)
                    front_rel = (intersection_bearing - heading + 180) % 360 - 180
                    front_word = "ahead" if abs(front_rel) <= 90 else "behind"
                    along_delta = along - here_along
                    if abs(along_delta) < 4:
                        continue
                    if not cross_names:
                        continue
                    for neighbour, street_name in edges.get(nid, []):
                        name = (street_name or "").strip()
                        key = _street_key(name)
                        if not key or key == current_key:
                            continue
                        nb_lat, nb_lon = nodes.get(neighbour, (None, None))
                        if nb_lat is None:
                            continue
                        branch_bearing = bearing_deg(nlat, nlon, nb_lat, nb_lon)
                        rel = (branch_bearing - heading + 180) % 360 - 180
                        abs_rel = abs(rel)
                        branch_side = None
                        if abs_rel > 25 and abs_rel < 155:
                            branch_side = "left" if rel < 0 else "right"
                        candidates.append({
                            "name": name,
                            "delta": along_delta,
                            "front": front_word,
                            "bearing": intersection_bearing,
                            "side": branch_side,
                            "perpendicular": abs(abs_rel - 90.0) <= 35.0,
                            "junction_type": _junction_type(nid),
                            "same_side": my_side in ("left", "right") and branch_side == my_side,
                            "distance": real_distance,
                        })
                if nearest_here:
                    metres, nid, cross_names = nearest_here
                    miab_log(
                        "verbose",
                        f"X street result: current='{street}' here='{', '.join(cross_names[:2])}' "
                        f"distance={metres:.1f}m node={nid}",
                        self.settings,
                    )
                    return f"At {cross_names[0]}, {_junction_type(nid)}."
                if not candidates:
                    return ""
                candidates.sort(key=lambda item: item["distance"])
                perpendicular = [item for item in candidates if item["perpendicular"]]
                nearest = perpendicular[0] if perpendicular else candidates[0]
                metres = int(round(nearest["distance"]))
                direction = compass_name(nearest["bearing"]).lower()
                miab_log(
                    "verbose",
                    f"X street result: current='{street}' cross='{nearest['name']}' "
                    f"direction='{direction}' distance={nearest['distance']:.1f}m "
                    f"side='{nearest['side']}' my_side='{my_side}'",
                    self.settings,
                )
                return (
                    f"{nearest['name']} {format_distance(metres)} "
                    f"{direction}, {nearest['junction_type']}."
                )

            announcement = (
                _nearest_side_or_cross_street_text()
                or "Cross street information is not available here."
            )

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

    def _nav_route_street_at(self, idx):
        """The street the route follows from step ``idx``'s point: the edge
        leaving it, or for the last step the edge arriving.  "" for routes
        without a node path."""
        route = getattr(self, "_nav_route", []) or []
        graph = getattr(getattr(self, "_nav", None), "_graph", None) \
            or getattr(self, "_walk_graph", None) or {}
        edges = graph.get("edges", {})
        try:
            i = int(self._nav_instructions[idx][0])
        except (TypeError, ValueError, IndexError, AttributeError):
            return ""
        if not route or not 0 <= i < len(route):
            return ""
        a, b = (route[i], route[i + 1]) if i + 1 < len(route) else (route[i - 1], route[i])
        return next((name for neighbour, name in edges.get(a, []) if neighbour == b), "")

    def _nav_sync_street_position(self, instruction_text="", idx=None):
        """Keep street mode on the route position as steps move it.

        Page Up/Down, X, A and the street display read ``street_label``.
        Left alone it stays on the street where navigation started.  A step's
        point is a turn or crossing, where the nearest road is as likely the
        cross street ("continue across Lindsay Street" sits on Lindsay
        Street), so use the street the route takes from there; failing that
        the street the instruction names ("onto First Avenue"), then the
        nearest road.  ``_nav_street_pin`` records it for this position.
        """
        if not getattr(self, "street_mode", False):
            return
        label = ""
        if idx is not None:
            route_street = self._nav_route_street_at(idx)
            if route_street and self._street_survey_segments_for(route_street):
                label = route_street
        match = re.search(r"\b(?:onto|on)\s+([^.]+?)\s*\.?\s*$", instruction_text or "")
        if not label and match:
            hint = match.group(1).strip()
            if self._street_survey_segments_for(hint):
                label = hint
        if not label:
            label, _cross = self._nearest_road(self.lat, self.lon)
        self.street_label = label
        self._nav_street_pin = (self.lat, self.lon, label)
        # The step moved the position, so an address chosen earlier with S
        # or Page Up/Down no longer describes it.  A trusts that pin within
        # 150 m: at Vibe Hotel North Sydney it still said "8-20 Napier
        # Street", where the route began.
        self._jump_street_label = None
        self._jump_street_pin_lat = None
        self._jump_street_pin_lon = None
        self._jump_address_number = None
        self._jump_address_street = None
        self._clear_street_survey_cache()
        wx.CallAfter(self.map_panel.set_position, self.lat, self.lon, True, label)

    def _nav_step_forward(self):
        """Up arrow during navigation — announce next instruction, silently move position."""
        instructions = getattr(self, '_nav_instructions', [])
        step = getattr(self, '_nav_step', 0)
        if step >= len(instructions):
            # Already past the final instruction. Stay put and remind the
            # user the route is finished; Down still steps back, Escape exits.
            if getattr(self, '_nav_arrived', False):
                self._nav_update_ui(
                    f"End of route at {self._nav_dest_name}.")
            else:
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
        self._nav_sync_street_position(text, step)
        # The arrival step lists its stretch too (23 George Street sits
        # between the final turn and 19 George Street).
        msg = self._nav_step_message(step, entry)

        arrived = "arriving" in text.lower() or self._nav_step >= len(instructions)

        if arrived:
            # A timed "Arrived. Near ..." cut off a longer arrival step (one
            # listing the places on the last stretch), so say it as part of
            # this message instead.  Provider routes' context only repeated
            # the final instruction.
            first_arrival = not getattr(self, "_nav_arrived", False)
            self._nav_finish_arrival(context_later=False)
            if first_arrival and getattr(self, "street_mode", False):
                if getattr(self, "_nav_arrival_provider_mode", False):
                    self._nav_arrival_provider_mode = False
                else:
                    msg = f"{msg}  {self._nav_arrival_street_text()}"
        self._nav_update_ui(msg)

    def _nav_step_back(self):
        """Down arrow during navigation — go back to previous instruction."""
        step = getattr(self, '_nav_step', 0)
        if step <= 1:
            instructions = getattr(self, '_nav_instructions', [])
            if instructions:
                self._nav_say_first_instruction(self._nav_step_message(0, instructions[0]))
            else:
                self._nav_update_ui("No previous instruction.")
            return
        self._nav_step -= 1
        entry = self._nav_instructions[self._nav_step - 1]
        if len(entry) >= 5:
            if self._nav_valid_coord(entry[3], entry[4]):
                self.lat, self.lon = entry[3], entry[4]
            else:
                miab_log(
                    "navigation",
                    f"Ignored invalid route coordinate: lat={entry[3]!r} lon={entry[4]!r}",
                    self.settings,
                )
        self._nav_sync_street_position(entry[2] if len(entry) > 2 else "",
                                       self._nav_step - 1)
        message = self._nav_step_message(self._nav_step - 1, entry)
        if self._nav_step == 1:
            self._nav_say_first_instruction(message)
        else:
            self._nav_update_ui(message)

    def _nav_say_first_instruction(self, message):
        """Speak step one without leaving it anywhere.

        Written to the status line it stuck there, and written into the
        focused row it replaced what the user had (Insert+Tab still read the
        route summary long after).  Spoken after focus settles, it cannot be
        cut off by the list's own selection announcement either.
        """
        if hasattr(self, "_announce_transient_then_return"):
            self._announce_transient_then_return(message)
        elif hasattr(self, "_announce_transient"):
            self._announce_transient(message)
        else:
            self._nav_update_ui(message)

    def _nav_step_message(self, idx, entry):
        """A step as spoken when moving through the route: the instruction
        and, the same both ways, the places passed on its stretch."""
        msg = self._nav_format_instruction(idx, entry)
        if len(entry) >= 5 and self._nav_valid_coord(entry[3], entry[4]):
            poi_text = self._nav_step_poi_text(idx, entry)
            if poi_text:
                msg = f"{msg}  {poi_text}"
        return msg
