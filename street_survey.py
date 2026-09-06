"""Street Survey behaviour mixed into MapNavigator."""

import math
import re
import threading

import wx

from geo import bearing_deg, dist_metres, nearest_point_on_segment
from logging_utils import miab_log
from world_map_panel import _IS_LAND


class StreetSurveyMixin:
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
            "all": "all numbers",
            "odd": "odd numbers only",
            "even": "even numbers only",
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
            else:
                mode = "poi_names"
        # The former "plain" mode visited POI coordinates while suppressing
        # their names, which could produce the meaningless announcement
        # "on <street>". Treat any live legacy value as the all-addresses mode.
        if mode == "plain":
            mode = "poi_names"
        return mode if mode in ("poi_names", "poi_only") else "poi_names"

    def _toggle_street_survey_address_announce_mode(self):
        if not self.street_mode:
            self._announce_transient("Address announcement mode is available in street mode.")
            return True
        current = self._street_survey_address_announce_mode()
        next_mode = {
            "poi_names": "poi_only",
            "poi_only": "poi_names",
        }[current]
        self._street_survey_address_announce_mode_value = next_mode
        label = {
            "poi_names": "stop at all addresses",
            "poi_only": "skip addresses without POIs",
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
        self._street_survey_same_address_pois = []
        self._street_survey_same_address_poi_index = 0
        self._street_survey_address_cursor = None

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
        seen_pois = set()

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

        # Background and live-search POIs are separate collections.  HERE's
        # structured houseNumber/street fields are retained on both, so merge
        # both globally: a business found with P should immediately become an
        # address-navigation candidate even when it was outside the original
        # background radius.  OSM remains the base and GNAF remains an optional
        # Australian supplement below.
        for collection in (getattr(self, "_all_pois", []) or [],
                           getattr(self, "_poi_list", []) or []):
            for poi in collection:
                if not isinstance(poi, dict):
                    continue
                identity = (
                    poi.get("source", ""), poi.get("here_id", ""),
                    poi.get("osm_type", ""), poi.get("osm_id", ""),
                    round(float(poi.get("lat") or 0), 7),
                    round(float(poi.get("lon") or 0), 7),
                    str(poi.get("name") or "").casefold(),
                )
                if identity in seen_pois:
                    continue
                seen_pois.add(identity)
                tags = poi.get("tags", {}) if isinstance(poi.get("tags"), dict) else {}
                number = poi.get("number") or tags.get("addr:housenumber")
                street = poi.get("street") or tags.get("addr:street")
                name = poi.get("name") or poi.get("label") or tags.get("name") or ""
                add_candidate(number, street, poi.get("lat"), poi.get("lon"),
                              poi.get("source") or "poi", name, poi=poi)

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
        collections = [
            getattr(self, "_all_pois", []) or [],
            getattr(self, "_poi_list", []) or [],
        ]
        # HERE Browse is result-capped.  Also include fresh nearby POIs learned
        # by previous broad/name searches, so a business does not disappear
        # from street navigation merely because this background fetch omitted
        # it.  Projection below still limits candidates to the selected road.
        try:
            collections.append(self._poi_fetcher.load_cached_nearby_pois(
                self.lat, self.lon, radius=2000.0))
        except Exception:
            pass
        for collection in collections:
          for poi in collection:
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
            # Different businesses commonly share an address and therefore
            # have identical coordinates.  Include the POI name in their
            # identity so one tenant does not erase the others; unnamed
            # address records still collapse normally.
            key = (
                str(ap["number"]).lower(),
                round(ap["lat"], 7),
                round(ap["lon"], 7),
                str(ap.get("name") or "").casefold(),
            )
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

    def _street_survey_physical_side(self, street_name, lat, lon, axis):
        """Return -1/1 for a point's side of the nearest oriented segment."""
        if not axis:
            return 0
        _lat0, _lon0, axis_ux, axis_uy, _axis_scale = axis
        scale_x = 111000 * math.cos(math.radians(lat))
        best = None
        for seg in self._street_survey_segments_for(street_name):
            coords = seg.get("coords", [])
            for index in range(len(coords) - 1):
                a_lat, a_lon = coords[index]
                b_lat, b_lon = coords[index + 1]
                p_lat, p_lon = nearest_point_on_segment(
                    lat, lon, a_lat, a_lon, b_lat, b_lon)
                distance = dist_metres(lat, lon, p_lat, p_lon)
                if best is not None and distance >= best[0]:
                    continue
                vx = (b_lon - a_lon) * scale_x
                vy = (b_lat - a_lat) * 111000
                length = math.hypot(vx, vy)
                if length < 0.1:
                    continue
                vx, vy = vx / length, vy / length
                # Orient every local segment toward increasing house numbers.
                if vx * axis_ux + vy * axis_uy < 0:
                    vx, vy = -vx, -vy
                px = (lon - p_lon) * scale_x
                py = (lat - p_lat) * 111000
                cross = vx * py - vy * px
                best = (distance, -1 if cross < 0 else 1 if cross > 0 else 0)
        return best[1] if best else 0

    def _street_survey_infer_unnumbered_parity(
            self, street_name, poi, numbered_addresses, axis):
        """Infer odd/even from the locally consistent physical road side."""
        poi_side = self._street_survey_physical_side(
            street_name, poi["lat"], poi["lon"], axis)
        if not poi_side:
            return None
        # Identical OSM/HERE/GNAF copies must not inflate confidence.
        observations = {}
        for address in numbered_addresses:
            parity = self._street_survey_number_parity(address.get("number"))
            if not parity:
                continue
            side = self._street_survey_physical_side(
                street_name, address["lat"], address["lon"], axis)
            if not side:
                continue
            identity = (
                str(address.get("number")), round(address["lat"], 5),
                round(address["lon"], 5))
            observations[identity] = (side, parity)
        votes = {1: {"odd": 0, "even": 0}, -1: {"odd": 0, "even": 0}}
        for side, parity in observations.values():
            votes[side][parity] += 1
        direct = votes[poi_side]
        total = direct["odd"] + direct["even"]
        if total >= 2 and max(direct.values()) / total >= 0.75:
            return max(direct, key=direct.get)
        # If one side is strongly established, conventional alternating road
        # numbering makes the opposite side a useful but still cautious clue.
        opposite = votes[-poi_side]
        opposite_total = opposite["odd"] + opposite["even"]
        if opposite_total >= 3 and max(opposite.values()) / opposite_total >= 0.8:
            known = max(opposite, key=opposite.get)
            return "even" if known == "odd" else "odd"
        return None

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
        # A business may be accurately positioned beside the road without a
        # road house number (HumanWare at Bullmatt Business Centre is one real
        # example).  Keep it in the spatial sequence without pretending that
        # its unit number is a Northampton Road house number.
        # Unnumbered POIs are useful destinations only in the POI-only mode.
        # The all-addresses mode must remain a sequence of actual addresses.
        if address_mode == "poi_only":
            numbered_poi_ids = {
                id(addr.get("poi")) for addr in addresses if addr.get("poi")
            }
            for poi_item in self._street_survey_poi_candidates(street):
                if id(poi_item.get("poi")) in numbered_poi_ids:
                    continue
                poi_item = dict(poi_item)
                poi = poi_item.get("poi") or {}
                tags = (poi.get("tags", {})
                        if isinstance(poi.get("tags"), dict) else {})
                number = poi.get("number") or tags.get("addr:housenumber")
                poi_street = poi.get("street") or tags.get("addr:street")
                has_matching_address = bool(
                    number and poi_street
                    and self._street_survey_bare(poi_street)
                    == self._street_survey_bare(street)
                )
                poi_item["number"] = str(number).strip() if has_matching_address else ""
                poi_item["unnumbered"] = not has_matching_address
                addresses.append(poi_item)
        if not addresses:
            self._announce_transient_then_return(
                f"No known addresses or businesses loaded for {street}.")
            return True
        address_groups = {}
        unique = {}
        numbered_locations = [a for a in addresses if a.get("number")]
        for addr in addresses:
            attached_number = None
            if addr.get("unnumbered") and numbered_locations:
                nearest = min(
                    numbered_locations,
                    key=lambda item: dist_metres(
                        addr["lat"], addr["lon"], item["lat"], item["lon"]))
                if dist_metres(addr["lat"], addr["lon"],
                               nearest["lat"], nearest["lon"]) <= 20:
                    attached_number = nearest["number"].lower()
            key = (addr["number"].lower() if addr.get("number") else
                   attached_number or
                   f"location:{round(addr['lat'], 5)}:{round(addr['lon'], 5)}")
            addr["address_group_key"] = key
            address_groups.setdefault(key, []).append(addr)
            # A co-located unnumbered business belongs in Shift+Page Up/Down
            # at that stop, not as an unreachable second Page Up/Down target
            # with identical coordinates.
            if attached_number:
                continue
            unique_key = (key if addr.get("number") else
                          f"{key}:{str(addr.get('name') or '').casefold()}")
            addr["navigation_key"] = unique_key
            existing = unique.get(unique_key)
            if existing is None or (addr.get("name") and not existing.get("name")):
                unique[unique_key] = addr
        addresses = list(unique.values())
        number_filter = self._street_survey_number_filter()
        if number_filter != "all":
            axis = self._street_survey_address_axis(street)
            numbered_addresses = [a for a in addresses if not a.get("unnumbered")]
            addresses = [
                a for a in addresses
                if ((not a.get("unnumbered") and
                     self._street_survey_number_parity(a["number"]) == number_filter)
                    or (a.get("unnumbered") and
                        self._street_survey_infer_unnumbered_parity(
                            street, a, numbered_addresses, axis) == number_filter))
            ]
            if not addresses:
                self._announce_transient_then_return(f"No {number_filter} numbers loaded for {street}.")
                return True
        if address_mode == "poi_only":
            addresses = [a for a in addresses if a.get("name")]
            if not addresses:
                self._announce_transient_then_return(f"No POI numbers loaded for {street}.")
                return True
        axis = self._street_survey_address_axis(street)
        if axis:
            for addr in addresses:
                addr["spatial_order"] = self._street_survey_axis_value(
                    axis, addr["lat"], addr["lon"])
            here_order = self._street_survey_axis_value(axis, self.lat, self.lon)
        else:
            for addr in addresses:
                projection = self._street_survey_project(
                    street, addr["lat"], addr["lon"])
                addr["spatial_order"] = projection[1] if projection else 0.0
            here_projection = self._street_survey_project(street, self.lat, self.lon)
            here_order = here_projection[1] if here_projection else 0.0
        addresses.sort(key=lambda item: item["spatial_order"])
        cursor = getattr(self, "_street_survey_address_cursor", None)
        cursor_key = (self._street_survey_bare(street),
                      address_mode, number_filter)
        cursor_index = None
        if isinstance(cursor, tuple) and len(cursor) == 2 and cursor[0] == cursor_key:
            for index, item in enumerate(addresses):
                if item.get("navigation_key") == cursor[1]:
                    cursor_index = index
                    break
        if direction > 0:
            if cursor_index is not None:
                target = (addresses[cursor_index + 1]
                          if cursor_index + 1 < len(addresses) else None)
            else:
                choices = [a for a in addresses if a["spatial_order"] > here_order + 2.0]
                target = choices[0] if choices else None
            edge_msg = (
                f"No higher known {number_filter} number on {street}."
                if number_filter != "all"
                else f"No higher POI number on {street}."
                if address_mode == "poi_only"
                else f"No higher known number on {street}."
            )
        else:
            if cursor_index is not None:
                target = addresses[cursor_index - 1] if cursor_index > 0 else None
            else:
                choices = [a for a in addresses if a["spatial_order"] < here_order - 2.0]
                target = choices[-1] if choices else None
            edge_msg = (
                f"No lower known {number_filter} number on {street}."
                if number_filter != "all"
                else f"No lower POI number on {street}."
                if address_mode == "poi_only"
                else f"No lower known number on {street}."
            )
        if not target:
            self._announce_transient(edge_msg)
            return True
        self._street_survey_address_cursor = (
            cursor_key, target.get("navigation_key"))
        self.lat = target["lat"]
        self.lon = target["lon"]
        self.street_label = street
        self._jump_street_label = street
        self._jump_street_pin_lat = self.lat
        self._jump_street_pin_lon = self.lon
        self._jump_address_number = target["number"] or None
        self._jump_address_street = street
        self._street_survey_current_poi = (
            target.get("poi")
            if address_mode in ("poi_names", "poi_only") and target.get("name")
            else None
        )
        same_address_pois = []
        seen_names = set()
        if address_mode in ("poi_names", "poi_only"):
            for addr in address_groups.get(target["address_group_key"], []):
                name = str(addr.get("name") or "").strip()
                if not name or name.casefold() in seen_names:
                    continue
                seen_names.add(name.casefold())
                same_address_pois.append(addr)
        self._street_survey_same_address_pois = same_address_pois
        self._street_survey_same_address_poi_index = 0
        if target.get("name"):
            for index, addr in enumerate(same_address_pois):
                if addr.get("name") == target.get("name"):
                    self._street_survey_same_address_poi_index = index
                    break
        self._street_survey_last_direction = direction
        wx.CallAfter(self.map_panel.set_position, self.lat, self.lon, True, street)
        name = target.get("name", "") if address_mode in ("poi_names", "poi_only") else ""
        address_text = (f"{target['number']} {street}"
                        if target.get("number") else f"on {street}")
        if name:
            if len(same_address_pois) > 1:
                self.sound.play_shared_address_tone()
                wx.CallLater(
                    70,
                    self._announce_transient,
                    f"Multiple POIs. {name}, {address_text}.",
                )
            else:
                self._announce_transient(f"{name}, {address_text}.")
        else:
            self._announce_transient(address_text + ".")
        return True

    def _street_survey_cycle_same_address_poi(self, direction):
        """Shift+Page Up/Down — browse POIs sharing the current address."""
        pois = getattr(self, "_street_survey_same_address_pois", []) or []
        if len(pois) < 2:
            self._announce_transient("No other POIs at this address.")
            return True
        index = getattr(self, "_street_survey_same_address_poi_index", 0)
        index = (index + direction) % len(pois)
        self._street_survey_same_address_poi_index = index
        target = pois[index]
        self._street_survey_current_poi = target.get("poi")
        street = self._street_survey_current_street()
        location = (f"{target['number']} {street}"
                    if target.get("number") else f"on {street}")
        self._announce_transient(f"{target['name']}, {location}.")
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
            parts.append("no known numbers loaded")
        self._announce_transient(".  ".join(parts) + ".")
