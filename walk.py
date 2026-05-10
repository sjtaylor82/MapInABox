"""walk.py — WalkMixin for Map in a Box.

All walking-mode methods as a mixin class.
MapNavigator inherits from this alongside wx.Frame.
"""

import math
import re
import threading

import wx

from geo import GENERIC_STREET_TYPES, bearing_between_nodes, bearing_deg, compass_name


class WalkMixin:

    def _build_walk_graph(self):
        """Build an intersection graph from loaded road segments.

        Returns dict:
          nodes = {node_id: (lat, lon)}
          edges = {node_id: [(neighbour_id, street_name), ...]}
          node_streets = {node_id: set of street names meeting here}

        Exact shared coordinates are always treated as the same node.
        In addition, nearby *endpoints* may be snapped together within a
        small metre threshold. This helps with near-miss topology such as
        streets that visually meet but whose OSM endpoint coordinates are a
        few metres apart. Interior bend points are not merged this way.
        """

        GENERIC = GENERIC_STREET_TYPES
        ENDPOINT_SNAP_M = 18.0

        # Step 1: collect all coordinate points with their street names
        raw_chains = []
        for seg in self._road_segments:
            raw_name = seg.get("name", "")
            clean = re.sub(r'\s*\(.*?\)', '', raw_name).strip()
            if not clean or clean.lower() in GENERIC:
                continue
            coords = seg["coords"]
            if len(coords) < 2:
                continue
            raw_chains.append((clean, coords))

        # Step 2: create nodes.
        # Exact coordinate matches always share a node.
        # Endpoints can also merge with nearby endpoints.
        exact_node_map = {}  # (lat, lon) rounded tightly -> node_id
        endpoint_nodes = []  # [(nid, lat, lon)]
        nodes = {}
        next_id = [0]

        def dist_m(lat1, lon1, lat2, lon2):
            mean_lat = math.radians((lat1 + lat2) / 2.0)
            dy = (lat1 - lat2) * 111000.0
            dx = (lon1 - lon2) * 111000.0 * math.cos(mean_lat)
            return math.hypot(dx, dy)

        def new_node(lat, lon):
            nid = next_id[0]
            next_id[0] += 1
            nodes[nid] = (lat, lon)
            return nid

        def get_node(lat, lon, is_endpoint=False):
            key = (round(lat, 7), round(lon, 7))
            if key in exact_node_map:
                return exact_node_map[key]

            if is_endpoint:
                best_nid = None
                best_dist = ENDPOINT_SNAP_M + 1.0
                for nid, elat, elon in endpoint_nodes:
                    d = dist_m(lat, lon, elat, elon)
                    if d < best_dist:
                        best_dist = d
                        best_nid = nid
                if best_nid is not None:
                    exact_node_map[key] = best_nid
                    return best_nid

            nid = new_node(lat, lon)
            exact_node_map[key] = nid
            if is_endpoint:
                endpoint_nodes.append((nid, lat, lon))
            return nid

        # Step 3: build adjacency from chains
        edges = {}
        node_streets = {}

        def add_edge(a, b, street_name):
            if a == b:
                return
            if a not in edges:
                edges[a] = []
            if b not in edges:
                edges[b] = []
            if (b, street_name) not in edges[a]:
                edges[a].append((b, street_name))
            if (a, street_name) not in edges[b]:
                edges[b].append((a, street_name))

        for street_name, coords in raw_chains:
            prev_nid = None
            last_index = len(coords) - 1
            for i, (lat, lon) in enumerate(coords):
                nid = get_node(lat, lon, is_endpoint=(i == 0 or i == last_index))
                if nid not in node_streets:
                    node_streets[nid] = set()
                node_streets[nid].add(street_name)
                if nid not in edges:
                    edges[nid] = []
                if prev_nid is not None:
                    add_edge(prev_nid, nid, street_name)
                prev_nid = nid

        # Step 4: identify intersection nodes
        intersections = set()
        for nid, streets in node_streets.items():
            if len(streets) >= 2:
                intersections.add(nid)

        for nid, adj in edges.items():
            if len(adj) == 1 or len(adj) >= 3:
                intersections.add(nid)

        return {
            "nodes": nodes,
            "edges": edges,
            "node_streets": node_streets,
            "intersections": intersections,
        }

    def _walk_find_nearest_node(self, lat, lon, street_filter=None):
        """Find nearest graph node to a lat/lon, optionally filtered to a street."""
        if not self._walk_graph:
            return None
        best_nid = None
        best_dist = float("inf")
        nodes = self._walk_graph["nodes"]
        node_streets = self._walk_graph["node_streets"]
        for nid, (nlat, nlon) in nodes.items():
            if street_filter and street_filter not in node_streets.get(nid, set()):
                continue
            d = math.sqrt(((lat - nlat) * 111000)**2 +
                          ((lon - nlon) * 111000 * math.cos(math.radians(lat)))**2)
            if d < best_dist:
                best_dist = d
                best_nid = nid
        return best_nid

    def _walk_compass_name(self, angle: float) -> str:
        return compass_name(angle)

    def _walk_bearing_coords(self, lat1, lon1, lat2, lon2) -> float:
        return bearing_deg(lat1, lon1, lat2, lon2)

    def _walk_bearing(self, from_nid: int, to_nid: int) -> float:
        return bearing_between_nodes(self._walk_graph["nodes"], from_nid, to_nid)

    def _walk_next_intersection(self, from_nid, street_name, heading,
                                prev_nid=None, preferred_first_nid=None):
        """Walk along street_name from from_nid in the given heading direction.
        Follows edges matching street_name, preferring the neighbour closest to
        the heading direction. On the first step, can exclude the exact edge we
        arrived from and can force a preferred outgoing branch when the user has
        explicitly chosen a turn. Stops at the next intersection node.
        Returns (intersection_nid, bearing_to_it, incoming_nid) or
        (None, None, None)."""
        graph = self._walk_graph
        edges = graph["edges"]
        intersections = graph["intersections"]
        visited = {from_nid}
        current = from_nid
        first_step = True
        prev_before_current = prev_nid

        while True:
            candidates = []
            for neighbour, sname in edges.get(current, []):
                if sname != street_name:
                    continue
                if neighbour in visited:
                    continue
                if first_step and prev_nid is not None and neighbour == prev_nid:
                    continue
                bearing = self._walk_bearing(current, neighbour)
                diff = abs((bearing - heading + 180) % 360 - 180)
                preferred = 0 if (first_step and preferred_first_nid is not None and neighbour == preferred_first_nid) else 1
                candidates.append((preferred, diff, bearing, neighbour))

            if not candidates:
                return None, None, None

            candidates.sort()
            _, _, seg_bearing, next_nid = candidates[0]
            visited.add(next_nid)

            if next_nid in intersections and next_nid != from_nid:
                return next_nid, seg_bearing, current

            heading = seg_bearing
            prev_before_current = current
            current = next_nid
            first_step = False

    def _walk_get_cross_streets(self, node_id, current_street):
        """Thin delegator — see StreetFetcher.cross_streets_at_node."""
        return self._street_fetcher.cross_streets_at_node(
            node_id, current_street, self._walk_graph or {}
        )

    def _walk_describe_intersection(self, node_id, street_name, heading):
        """Build a description of the current intersection."""
        graph = self._walk_graph
        edges = graph["edges"]
        nodes = graph["nodes"]

        branches = len(set(n for n, s in edges.get(node_id, [])))
        cross = self._walk_get_cross_streets(node_id, street_name)
        heading_name = self._walk_compass_name(heading)

        nlat, nlon = nodes[node_id]

        # Nearest house number from preloaded local data — no API call
        num = self._nearest_address_number(nlat, nlon, street_name, radius=60)
        addr_part = f"  Near {num} {street_name}." if num else ""

        shape = self._walk_describe_intersection_shape(node_id, street_name, heading)

        if cross:
            n_ways = max(2, min(branches, 6))
            cross_list = ", ".join(cross)
            way_label = f"{n_ways}-way intersection" if n_ways >= 3 else "intersection"
            desc = (f"{street_name} at {cross_list}.  "
                    f"{way_label}, heading {heading_name}.{addr_part}")
        else:
            desc = f"{street_name}, heading {heading_name}.{addr_part}"
        if shape:
            desc += f"  {shape}"

        # Walking POI announcements — always re-announce on every intersection visit
        if self.settings.get("walk_announce_pois") and getattr(self, '_poi_grid', {}):
            walk_radius = self.settings.get("walk_poi_radius_m", 80)
            show_kind   = self.settings.get("walk_announce_category", True)
            nearby_pois = self._poi_grid_nearby(nlat, nlon, walk_radius)
            nearby = []
            for poi in nearby_pois:
                name = poi["label"].split(",")[0].strip()
                kind = poi.get("kind", "")
                if show_kind and kind:
                    nearby.append(f"{name}, {kind}")
                else:
                    nearby.append(name)
            if nearby:
                desc += "  Nearby: " + "; ".join(nearby) + "."
        return desc

    def _walk_describe_intersection_shape(self, node_id, current_street, heading):
        """Describe which side each intersecting branch is on.

        This is deliberately compact for screen-reader use. It is reused by
        walking mode and the F11 street survey commands.
        """
        graph = self._walk_graph or {}
        edges = graph.get("edges", {})
        node_edges = edges.get(node_id, [])
        if not node_edges:
            return ""

        def bare(name):
            if hasattr(self, "_street_survey_bare"):
                return self._street_survey_bare(name)
            return re.sub(r"\s*\(.*?\)", "", name or "").strip().lower()

        current_key = bare(current_street)
        by_street = {}
        current_dirs = set()

        for neighbour, street_name in node_edges:
            label = (street_name or "").strip() or "unnamed road"
            bearing = self._walk_bearing(node_id, neighbour)
            rel = (bearing - heading + 180) % 360 - 180
            abs_rel = abs(rel)
            if abs_rel <= 25:
                side = "ahead"
            elif abs_rel >= 155:
                side = "behind"
            elif rel < 0:
                side = "left"
            else:
                side = "right"

            if bare(label) == current_key:
                current_dirs.add(side)
                continue
            by_street.setdefault(label, set()).add(side)

        parts = []
        for street in sorted(by_street, key=str.lower):
            sides = by_street[street]
            if "left" in sides and "right" in sides:
                parts.append(f"{street} crosses left-right")
            elif "left" in sides:
                parts.append(f"{street} on the left")
            elif "right" in sides:
                parts.append(f"{street} on the right")
            elif "ahead" in sides:
                parts.append(f"{street} straight ahead")

        if "ahead" in current_dirs:
            parts.append(f"{current_street} continues ahead")

        return ".  ".join(parts) + ("." if parts else "")

    def _walk_toggle(self):
        """Toggle walking mode on or off."""
        if self._walking_mode:
            self._walking_mode = False
            self._walk_announced_pois = set()
            self._poi_list  = []  # restore free nav arrow keys
            self._poi_index = 0
            self.update_ui("Walking mode off.  Free movement restored.")
            wx.CallAfter(self.listbox.SetFocus)
            return

        if not self._road_fetched or not self._road_segments:
            self._status_update("No street data loaded yet. Wait for streets to load first.", force=True)
            return

        # Build the intersection graph
        self._status_update("Building walking graph...")
        try:
            self._walk_graph = self._build_walk_graph()
        except Exception as exc:
            print(f"[Walk] Graph build failed: {exc}")
            import traceback; traceback.print_exc()
            self._status_update(f"Walking graph failed: {exc}", force=True)
            return
        self._nav.set_graph(self._walk_graph)

        if not self._walk_graph["intersections"]:
            self._status_update("Could not build walking graph. Not enough intersections found.", force=True)
            return

        # Find nearest intersection to current position
        best_nid = None
        best_dist = float("inf")
        nodes = self._walk_graph["nodes"]
        intersections = self._walk_graph["intersections"]
        for nid in intersections:
            nlat, nlon = nodes[nid]
            d = math.sqrt(((self.lat - nlat) * 111000)**2 +
                          ((self.lon - nlon) * 111000 *
                           math.cos(math.radians(self.lat)))**2)
            if d < best_dist:
                best_dist = d
                best_nid = nid

        if best_nid is None:
            self._status_update("No intersections found nearby.", force=True)
            return

        # Pick a street at this intersection
        node_streets = self._walk_graph["node_streets"]
        streets_here = sorted(node_streets.get(best_nid, set()))
        # Filter out generic names
        GENERIC = GENERIC_STREET_TYPES
        named = [s for s in streets_here if s.lower() not in GENERIC]
        if not named:
            self._status_update("No named streets at nearest intersection.", force=True)
            return

        self._walk_street = named[0]
        self._walk_node = best_nid

        # Pick initial heading: direction with more intersections ahead
        # Try both directions along the street
        heading_fwd = None
        heading_rev = None
        for neighbour, sname in self._walk_graph["edges"].get(best_nid, []):
            if sname == self._walk_street:
                b = self._walk_bearing(best_nid, neighbour)
                if heading_fwd is None:
                    heading_fwd = b
                elif heading_rev is None:
                    heading_rev = b

        if heading_fwd is not None:
            self._walk_heading = heading_fwd
        else:
            self._walk_heading = 0.0

        # Move to the intersection position
        self.lat, self.lon = nodes[best_nid]
        self._walk_cross_options = self._walk_get_cross_streets(best_nid, self._walk_street)
        self._walk_cross_idx = 0
        self._walk_turn_options = []
        self._walk_option_idx = None
        self._walk_prev_node = None
        self._walk_preferred_next = None
        self._walk_history = []

        self._walking_mode = True
        n_intersections = len(self._walk_graph["intersections"])
        desc = self._walk_describe_intersection(best_nid, self._walk_street, self._walk_heading)
        self.street_label = self._walk_street
        wx.CallAfter(self.map_panel.set_position, self.lat, self.lon, True, self.street_label)
        self.update_ui(f"Walking mode on.  {n_intersections} intersections found.  {desc}")
        # Fetch POIs in background for walk-announce if not already loaded
        # Delayed 3s so walk graph build and first announcement complete first
        if not getattr(self, '_all_pois', []):
            def _delayed_fetch():
                import time as _time
                _time.sleep(3)
                if getattr(self, '_walking_mode', False):
                    self._fetch_all_pois_background(getattr(self, '_address_points', []))
            threading.Thread(target=_delayed_fetch, daemon=True).start()
        wx.CallAfter(self.listbox.SetFocus)

    def _walk_forward(self):
        """Up arrow in walking mode — walk to next intersection ahead."""
        current_node = self._walk_node
        result = self._walk_next_intersection(
            self._walk_node, self._walk_street, self._walk_heading,
            prev_nid=getattr(self, '_walk_prev_node', None),
            preferred_first_nid=getattr(self, '_walk_preferred_next', None))
        next_nid, bearing, incoming_nid = result
        self._walk_preferred_next = None
        if next_nid is None:
            self.update_ui(f"End of {self._walk_street}.")
            return

        nodes = self._walk_graph["nodes"]
        history = getattr(self, '_walk_history', None)
        if history is None:
            self._walk_history = []
            history = self._walk_history
        history.append(current_node)
        self._walk_node = next_nid
        # Use overall bearing from start to destination, not last segment bearing
        # This prevents heading flip on curved streets
        prev_lat, prev_lon = nodes[current_node]
        new_lat, new_lon = nodes[next_nid]
        overall_bearing = self._walk_bearing_coords(prev_lat, prev_lon, new_lat, new_lon)
        # Only update heading if it doesn't flip more than 90 degrees
        diff = abs((overall_bearing - self._walk_heading + 180) % 360 - 180)
        self._walk_heading = overall_bearing if diff < 90 else self._walk_heading
        self._walk_prev_node = incoming_nid
        self.lat, self.lon = nodes[next_nid]
        self._walk_cross_options = self._walk_get_cross_streets(next_nid, self._walk_street)
        self._walk_cross_idx = 0
        self._walk_browsing = False
        self._walk_turn_options = []
        self._walk_option_idx = None

        self.street_label = self._walk_street
        wx.CallAfter(self.map_panel.set_position, self.lat, self.lon, True, self.street_label)
        bounds = self._spatial_tone_bounds() if hasattr(self, "_spatial_tone_bounds") else None
        self.sound.play_spatial_tone(self.lat, self.lon, bounds)
        desc = self._walk_describe_intersection(next_nid, self._walk_street, self._walk_heading)

        # Nav: check progress and get upcoming instruction to append
        nav_str = self._nav_check_progress(next_nid)
        if nav_str:
            self.update_ui(f"{desc}  {nav_str}")
            self._nav_last_announced = nav_str
        else:
            # If nav active, append distance/direction to destination
            if getattr(self, '_nav_active', False):
                upcoming = self._nav_next_instruction_str()
                if upcoming:
                    self.update_ui(f"{desc}  {upcoming}")
                    self._nav_last_announced = upcoming
                    return
            self.update_ui(desc)

    def _walk_backward(self):
        """Down arrow in walking mode — walk to previous intersection behind."""
        history = getattr(self, '_walk_history', None)
        if not history:
            self._walk_browsing = False
            self._walk_turn_options = []
            self._walk_option_idx = None
            self.update_ui(f"Start of tracked path on {self._walk_street}.")
            return

        nodes = self._walk_graph["nodes"]
        target_nid = history.pop()
        self._walk_node = target_nid
        # Keep original heading; user moved back but is still facing the same way.
        self._walk_prev_node = history[-1] if history else None
        self._walk_preferred_next = None
        self._walk_cross_options = self._walk_get_cross_streets(target_nid, self._walk_street)
        self._walk_cross_idx = 0
        self._walk_browsing = False
        self._walk_turn_options = []
        self._walk_option_idx = None

        self.lat, self.lon = nodes[target_nid]
        self.street_label = self._walk_street
        wx.CallAfter(self.map_panel.set_position, self.lat, self.lon, True, self.street_label)
        bounds = self._spatial_tone_bounds() if hasattr(self, "_spatial_tone_bounds") else None
        self.sound.play_spatial_tone(self.lat, self.lon, bounds)
        desc = self._walk_describe_intersection(target_nid, self._walk_street, self._walk_heading)
        self.update_ui(desc)


    def _walk_get_turn_options(self, node_id, current_street, heading):
        """Build ordered turn options from actual outgoing branches, not street names.
        Returns a list of dicts sorted from left to right, excluding only the exact
        incoming edge as the default U-turn."""
        edges = self._walk_graph["edges"]
        options = []
        seen_neighbours = set()
        incoming_nid = getattr(self, '_walk_prev_node', None)

        for neighbour, street_name in edges.get(node_id, []):
            # Exclude only the exact edge we just arrived on.
            if incoming_nid is not None and neighbour == incoming_nid:
                continue
            if neighbour in seen_neighbours:
                continue
            seen_neighbours.add(neighbour)

            bearing = self._walk_bearing(node_id, neighbour)
            relative = (bearing - heading + 180) % 360 - 180

            label = (street_name or "").strip() or "unnamed road"
            options.append({
                "street": label,
                "bearing": bearing,
                "relative": relative,
                "neighbour": neighbour,
                "is_current_street": (street_name == current_street),
            })

        # If there is no remembered incoming edge, still suppress a near-pure U-turn.
        if incoming_nid is None:
            options = [o for o in options if abs(o["relative"]) <= 150]

        options.sort(key=lambda o: (o["relative"], o["street"].lower()))

        # Collapse only near-identical branches with the same street name and angle.
        collapsed = []
        for opt in options:
            if collapsed:
                prev = collapsed[-1]
                if (prev["street"].lower() == opt["street"].lower() and
                        abs(prev["relative"] - opt["relative"]) < 8):
                    continue
            collapsed.append(opt)

        return collapsed

    def _walk_option_text(self, option):
        """Speak a turn option using relative degrees from current travel direction."""
        rel = option["relative"]
        street = option["street"]

        if abs(rel) <= 15:
            if option["is_current_street"]:
                return f"Straight ahead on {street}."
            return f"Straight ahead onto {street}."

        degrees = int(round(abs(rel)))
        side = "left" if rel < 0 else "right"
        if option["is_current_street"]:
            return f"{side} {degrees} degrees onto {street}."
        return f"{side} {degrees} degrees onto {street}."

    def _walk_browse_turn(self, direction):
        """Browse turn options from left to right. direction is -1 for left, +1 for right."""
        options = self._walk_get_turn_options(
            self._walk_node, self._walk_street, self._walk_heading)
        if not options:
            self._status_update("No turn options here.", force=True)
            return

        if not getattr(self, '_walk_browsing', False):
            self._walk_browsing = True
            start_idx = min(range(len(options)), key=lambda i: abs(options[i]["relative"]))
            if direction < 0:
                idx = max(0, start_idx - 1)
            else:
                idx = min(len(options) - 1, start_idx + 1)
            self._walk_option_idx = idx
        else:
            current_idx = getattr(self, '_walk_option_idx', None)
            if current_idx is None or current_idx >= len(options):
                current_idx = min(range(len(options)), key=lambda i: abs(options[i]["relative"]))
            self._walk_option_idx = (current_idx + direction) % len(options)

        self._walk_turn_options = options
        chosen = options[self._walk_option_idx]
        n = len(options)
        msg = (f"{self._walk_option_text(chosen)}  "
               f"Option {self._walk_option_idx + 1} of {n}.")

        # Announce nearby POIs in this direction if available
        if self.settings.get("walk_announce_pois") and getattr(self, '_poi_grid', {}):
            neighbour_nid = chosen.get("neighbour")
            if neighbour_nid is not None and self._walk_graph:
                nodes = self._walk_graph["nodes"]
                if neighbour_nid in nodes:
                    nlat, nlon = nodes[neighbour_nid]
                    walk_radius = self.settings.get("walk_poi_radius_m", 80)
                    show_kind = self.settings.get("walk_announce_category", True)
                    nearby_pois = self._poi_grid_nearby(nlat, nlon, walk_radius)
                    nearby = []
                    for poi in nearby_pois:
                        name = poi["label"].split(",")[0].strip()
                        kind = poi.get("kind", "")
                        if show_kind and kind:
                            nearby.append(f"{name}, {kind}")
                        else:
                            nearby.append(name)
                    if nearby:
                        msg += "  Ahead: " + "; ".join(nearby) + "."

        self.update_ui(msg)

    def _walk_turn_right(self):
        """Right arrow — browse turn options toward the right."""
        self._walk_browse_turn(1)

    def _walk_turn_left(self):
        """Left arrow — browse turn options toward the left."""
        self._walk_browse_turn(-1)

    def _walk_commit_turn(self, announce=True):
        """Commit the currently selected turn option."""
        options = getattr(self, '_walk_turn_options', [])
        idx = getattr(self, '_walk_option_idx', None)
        if not options or idx is None or idx >= len(options):
            return False

        chosen = options[idx]
        self._walk_street = chosen["street"]
        self._walk_heading = chosen["bearing"]
        self.street_label = chosen["street"]
        self._walk_preferred_next = chosen["neighbour"]
        self._walk_prev_node = self._walk_node
        self._walk_cross_options = self._walk_get_cross_streets(self._walk_node, chosen["street"])
        self._walk_cross_idx = 0

        if announce:
            heading_name = self._walk_compass_name(self._walk_heading)
            self.update_ui(f"{self._walk_option_text(chosen)}  Heading {heading_name}.")
        wx.CallAfter(self.map_panel.set_position, self.lat, self.lon, True, self.street_label)
        return True

    def _walk_turnaround(self):
        """X key — turn 180 degrees."""
        self._walk_heading = (self._walk_heading + 180) % 360
        heading_name = self._walk_compass_name(self._walk_heading)
        self.update_ui(f"Turning around.  Now heading {heading_name} along {self._walk_street}.")

    def _download_new_area(self):
        """User pressed Space outside the barrier — fetch street data for current position."""
        self._pending_street_download = False
        self._inside_barrier = True
        self._road_fetched = False
        self._road_segments = []
        self._poi_list = []
        self._poi_index = 0
        self._poi_explore_stack = []
        self._status_update("Downloading street data for this area...")
        threading.Thread(target=self._try_enter_new_area, daemon=True).start()

    def _try_enter_new_area(self):
        """Re-geocode the new position for suburb name and radius, then fetch roads."""
        import urllib.request, urllib.parse, math
        # Clear stale fetch coords before re-geocoding.
        self._street_fetch_lat = None
        self._street_fetch_lon = None
        try:
            params = urllib.parse.urlencode({
                "lat": self.lat, "lon": self.lon,
                "format": "json", "zoom": 14, "addressdetails": 1,
            })
            req = urllib.request.Request(
                f"https://nominatim.openstreetmap.org/reverse?{params}",
                headers={"User-Agent": "MapInABox/1.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode())
            bb = data.get("boundingbox")
            if bb:
                minlat, maxlat, minlon, maxlon = map(float, bb)
                half_lat = abs(maxlat - minlat) / 2 * 111000
                half_lon = abs(maxlon - minlon) / 2 * 111000 * math.cos(math.radians(self.lat))
                radius = int(max(half_lat, half_lon))
                radius = max(500, min(radius, 3000))
                self._street_radius  = radius
                self._street_barrier = int(radius * 0.9)
                bbox_clat = (minlat + maxlat) / 2
                bbox_clon = (minlon + maxlon) / 2
                offset_m = math.sqrt(
                    ((bbox_clat - self.lat) * 111000) ** 2 +
                    ((bbox_clon - self.lon) * 111000 * math.cos(math.radians(self.lat))) ** 2)
                if offset_m > 50:
                    self._street_fetch_lat = bbox_clat
                    self._street_fetch_lon = bbox_clon
                    print(f"[Street] bbox centre {offset_m:.0f}m from position, using it")
                else:
                    self._street_fetch_lat = None
                    self._street_fetch_lon = None
                # Store suburb bbox for HERE snap filtering
                self._street_bbox = (minlat, maxlat, minlon, maxlon)
                place = (data.get("address", {}).get("suburb")
                         or data.get("address", {}).get("town")
                         or data.get("address", {}).get("city", "this area"))
                self._current_suburb = place
                self._current_country_code = data.get("address", {}).get("country_code", "")
                wx.CallAfter(self._status_update, f"Entering {place}. Loading streets...")
        except Exception as e:
            print(f"[Street] Suburb geocode failed, using defaults: {e}")
            wx.CallAfter(self._status_update, "Loading streets...")
        self._fetch_road_data()
