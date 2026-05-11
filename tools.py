"""tools.py — ToolsMixin for Map in a Box.

F12 tools (detour calculator, route explorer, toll compare,
journey planner, departure board) as a mixin class.
"""

import json
import math
import threading
import urllib.parse
import urllib.request

import wx

try:
    from route_tools import RouteTools
except ImportError:
    RouteTools = None

# Dialogs imported lazily to avoid circular imports
def _get_tools_menu_dialog():
    from dialogs import ToolsMenuDialog
    return ToolsMenuDialog

def _get_dialogs():
    from dialogs import (
        ToolsMenuDialog, StopEntryDialog, DateTimePickerDialog,
        JourneyResultsDialog, TransitLookupDialog, RouteResultsDialog,
        FindFoodDialog,
    )
    return (ToolsMenuDialog, StopEntryDialog, DateTimePickerDialog,
            JourneyResultsDialog, TransitLookupDialog, RouteResultsDialog,
            FindFoodDialog)

def _key_required(parent, title, message, link_label, link_url):
    from dialogs import show_api_key_required
    show_api_key_required(parent, title, message, link_label, link_url)


class ToolsMixin:
    def _warn_optional_key(self, tool_name: str, key_name: str, limitation: str) -> None:
        """Announce that a tool can continue, but with reduced coverage."""
        from dialogs import show_optional_key_warning
        show_optional_key_warning(
            self,
            f"{tool_name} Warning",
            f"Warning: {key_name} API key not detected.\n\n"
            f"{tool_name} will still work, but {limitation}",
        )

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
        self.sound.stop()
        if self._dlgs is None:
            return
        try:
            ToolsMenuDialog = self._dlgs[0]
            dlg = ToolsMenuDialog(self)
            if dlg.ShowModal() == wx.ID_OK:
                tool = dlg.selected_tool
                dlg.Destroy()
                if tool == "detour_calculator":
                    self._tool_detour_calculator()
                elif tool == "route_explorer":
                    self._tool_route_explorer()
                elif tool == "toll_compare":
                    self._tool_toll_compare()
                elif tool == "journey_planner":
                    self._tool_journey_planner()
                elif tool == "departure_board":
                    self._tool_departure_board()
                elif tool == "flight_search":
                    self._tool_flight_search()
                elif tool == "hotel_search":
                    self._tool_hotel_search()
                else:
                    self._resume_location_sound()
            else:
                dlg.Destroy()
                self._resume_location_sound()
        except Exception as exc:
            import wx as _wx
            _wx.MessageBox(f"Tools menu error:\n\n{exc}", "Error", _wx.OK | _wx.ICON_ERROR)
            self._resume_location_sound()
        self.listbox.SetFocus()

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
            self._resume_location_sound()
            return

        def _geocode(prompt_text):
            """Show dialog, geocode, return (lat, lon, name) or None on cancel."""
            dlg = self._dlgs[1](self, prompt_text)
            if dlg.ShowModal() != wx.ID_OK or not dlg.GetValue():
                dlg.Destroy()
                return None
            value = dlg.GetValue()
            dlg.Destroy()
            self._status_update(f"Looking up {value}...")
            try:
                lat, lon, formatted = rt.geocode(value, country_code)
                self._status_update(f"Found: {formatted}", force=True)
                return (lat, lon, formatted)
            except Exception as e:
                self._status_update(f"Could not find '{value}': {e}", force=True)
                return "retry"

        # 1. Start — mandatory
        while True:
            result = _geocode("Where are you starting from?")
            if result is None:
                self._status_update("Detour calculator cancelled.", force=True)
                self._resume_location_sound()
                return
            if result != "retry":
                start = result
                break

        # 2. Stop-off — mandatory (at least one)
        while True:
            result = _geocode("Where do you need to stop?")
            if result is None:
                self._status_update("Detour calculator cancelled.", force=True)
                self._resume_location_sound()
                return
            if result != "retry":
                first_stop = result
                break

        # 3. Destination — mandatory
        while True:
            result = _geocode("What is your final destination?")
            if result is None:
                self._status_update("Detour calculator cancelled.", force=True)
                self._resume_location_sound()
                return
            if result != "retry":
                destination = result
                break

        # Build stops list: start, stop-offs..., destination
        stops = [start, first_stop]

        # 4. Optional additional stop-offs
        while True:
            result = _geocode(
                "Additional stop-off (or leave blank to finish):")
            if result is None:
                break  # blank or cancel — done adding stops
            if result == "retry":
                continue
            stops.append(result)

        stops.append(destination)

        # Run comparison in background
        self._status_update("Calculating detour vs direct route...")

        def _calc():
            try:
                result = rt.compare_routes(stops)
                wx.CallAfter(self._show_route_results,
                             "Detour Calculator", result["summary_text"])
            except Exception as e:
                wx.CallAfter(self._status_update, f"Detour calculation failed: {e}", True)
                wx.CallAfter(self._resume_location_sound)

        threading.Thread(target=_calc, daemon=True).start()

    def _tool_route_explorer(self):
        """Route Explorer — compare alternative routes with suburbs and tolls."""
        rt = self._get_route_tools()
        if not rt:
            self._resume_location_sound()
            return
        if not rt.is_configured:
            self._warn_optional_key(
                "Route Explorer",
                "Google",
                "it will use open geocoding and OSRM routing instead of Google Maps, "
                "so route coverage and suburb matching may be less complete.",
            )

        country_code = self._ask_country_code()
        if not country_code:
            self._status_update("Route explorer cancelled.", force=True)
            self._resume_location_sound()
            return

        # Get origin
        dlg = self._dlgs[1](
            self, "Where are you starting from? (full address, suburb or city):")
        if dlg.ShowModal() != wx.ID_OK or not dlg.GetValue():
            dlg.Destroy()
            self._status_update("Route explorer cancelled.", force=True)
            self._resume_location_sound()
            return
        origin_text = dlg.GetValue()
        dlg.Destroy()

        self._status_update(f"Looking up {origin_text}...")
        try:
            o_lat, o_lon, o_name = rt.geocode(origin_text, country_code)
            self._status_update(f"Origin: {o_name}", force=True)
        except Exception as e:
            self._status_update(f"Could not find '{origin_text}': {e}", force=True)
            self._resume_location_sound()
            return

        # Get destination
        dlg = self._dlgs[1](
            self, "Where are you going? (full address, suburb or city):")
        if dlg.ShowModal() != wx.ID_OK or not dlg.GetValue():
            dlg.Destroy()
            self._status_update("Route explorer cancelled.", force=True)
            self._resume_location_sound()
            return
        dest_text = dlg.GetValue()
        dlg.Destroy()

        self._status_update(f"Looking up {dest_text}...")
        try:
            d_lat, d_lon, d_name = rt.geocode(dest_text, country_code)
            self._status_update(f"Destination: {d_name}", force=True)
        except Exception as e:
            self._status_update(f"Could not find '{dest_text}': {e}", force=True)
            self._resume_location_sound()
            return

        # Run exploration in background
        self._status_update("Finding routes and identifying suburbs...")

        def _status(msg):
            wx.CallAfter(self._status_update, msg)

        def _calc():
            try:
                result = rt.explore_routes(
                    (o_lat, o_lon, o_name),
                    (d_lat, d_lon, d_name),
                    status_cb=_status,
                )
                wx.CallAfter(self._show_route_results,
                             "Route Explorer", result["summary_text"])
            except Exception as e:
                wx.CallAfter(self._status_update, f"Route exploration failed: {e}", True)
                wx.CallAfter(self._resume_location_sound)

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
            self._resume_location_sound()
            return

        # Get origin
        dlg = self._dlgs[1](
            self, "Where are you starting from? (full address, suburb or city):")
        if dlg.ShowModal() != wx.ID_OK or not dlg.GetValue():
            dlg.Destroy()
            self._status_update("Toll compare cancelled.", force=True)
            self._resume_location_sound()
            return
        origin_text = dlg.GetValue()
        dlg.Destroy()

        self._status_update(f"Looking up {origin_text}...")
        try:
            o_lat, o_lon, o_name = rt.geocode(origin_text, country_code)
            self._status_update(f"Origin: {o_name}", force=True)
        except Exception as e:
            self._status_update(f"Could not find '{origin_text}': {e}", force=True)
            self._resume_location_sound()
            return

        # Get destination
        dlg = self._dlgs[1](
            self, "Where are you going? (full address, suburb or city):")
        if dlg.ShowModal() != wx.ID_OK or not dlg.GetValue():
            dlg.Destroy()
            self._status_update("Toll compare cancelled.", force=True)
            self._resume_location_sound()
            return
        dest_text = dlg.GetValue()
        dlg.Destroy()

        self._status_update(f"Looking up {dest_text}...")
        try:
            d_lat, d_lon, d_name = rt.geocode(dest_text, country_code)
            self._status_update(f"Destination: {d_name}", force=True)
        except Exception as e:
            self._status_update(f"Could not find '{dest_text}': {e}", force=True)
            self._resume_location_sound()
            return

        self._status_update("Comparing toll vs toll-free routes...")

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
                wx.CallAfter(self._resume_location_sound)

        threading.Thread(target=_calc, daemon=True).start()

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
            self._resume_location_sound()
            return

        # Origin
        dlg = self._dlgs[1](
            self, "Where are you leaving from? (address, stop or suburb):")
        if dlg.ShowModal() != wx.ID_OK or not dlg.GetValue():
            dlg.Destroy()
            self._status_update("Journey planner cancelled.", force=True)
            self._resume_location_sound()
            return
        origin_text = dlg.GetValue()
        dlg.Destroy()

        # Destination
        dlg = self._dlgs[1](
            self, "Where are you going? (address, stop or suburb):")
        if dlg.ShowModal() != wx.ID_OK or not dlg.GetValue():
            dlg.Destroy()
            self._status_update("Journey planner cancelled.", force=True)
            self._resume_location_sound()
            return
        dest_text = dlg.GetValue()
        dlg.Destroy()

        # Timing mode
        timing_choices = ["Leave now", "Leave at a specific time",
                          "Arrive by a specific time"]
        dlg = wx.SingleChoiceDialog(
            self, "When?", "Timing", timing_choices)
        dlg.SetSelection(0)
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            self._status_update("Journey planner cancelled.", force=True)
            self._resume_location_sound()
            return
        timing_sel = dlg.GetSelection()
        dlg.Destroy()

        timing_mode = ["now", "depart", "arrive"][timing_sel]
        timestamp = None

        if timing_mode in ("depart", "arrive"):
            label = "Depart at:" if timing_mode == "depart" else "Arrive by:"
            dt_dlg = self._dlgs[2](self, title=label)
            if dt_dlg.ShowModal() != wx.ID_OK:
                dt_dlg.Destroy()
                self._status_update("Journey planner cancelled.", force=True)
                self._resume_location_sound()
                return
            chosen_dt = dt_dlg.get_datetime()
            dt_dlg.Destroy()
            if not chosen_dt:
                self._status_update("Invalid date/time. Journey planner cancelled.", force=True)
                self._resume_location_sound()
                return
            timestamp = int(chosen_dt.timestamp())

        # Transit filter
        filter_choices = ["All transport types", "Buses and coaches only",
                          "Trains only", "Ferries only"]
        dlg = wx.SingleChoiceDialog(
            self, "Show routes using:", "Transport Type", filter_choices)
        dlg.SetSelection(0)
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            self._status_update("Journey planner cancelled.", force=True)
            self._resume_location_sound()
            return
        filter_sel = dlg.GetSelection()
        dlg.Destroy()

        transit_filter = ["all", "bus", "train", "ferry"][filter_sel]

        self._status_update("Searching for transit options...")

        def _status(msg):
            wx.CallAfter(self._status_update, msg)

        def _calc():
            try:
                routes = rt.journey_plan(
                    origin_text, dest_text, country_code,
                    timing_mode=timing_mode,
                    timestamp=timestamp,
                    transit_filter=transit_filter,
                    status_cb=_status,
                )
                wx.CallAfter(self._show_journey_results, routes)
            except Exception as e:
                wx.CallAfter(self._status_update, f"Journey planner failed: {e}", True)
                wx.CallAfter(self._resume_location_sound)

        threading.Thread(target=_calc, daemon=True).start()

    def _show_journey_results(self, routes):
        """Display journey results in the two-level dialog."""
        if not routes:
            self._status_update("No transit options found.", force=True)
            self._resume_location_sound()
            return
        self._status_update(f"Found {len(routes)} option{'s' if len(routes) != 1 else ''}.", force=True)
        dlg = self._dlgs[3](self, routes)
        dlg.ShowModal()
        dlg.Destroy()
        self._resume_location_sound()
        self.listbox.SetFocus()

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
            self._resume_location_sound()
            return

        # Ask for location
        dlg = self._dlgs[1](
            self, "Where? (suburb, stop name, or address):")
        if dlg.ShowModal() != wx.ID_OK or not dlg.GetValue():
            dlg.Destroy()
            self._status_update("Departure board cancelled.", force=True)
            self._resume_location_sound()
            return
        location_text = dlg.GetValue()
        dlg.Destroy()

        # Geocode to get lat/lon
        self._status_update(f"Looking up {location_text}...")
        try:
            lat, lon, formatted = rt.geocode(location_text, country_code)
            self._status_update(f"Searching for stops near {formatted}...")
        except Exception as e:
            self._status_update(f"Could not find '{location_text}': {e}", force=True)
            self._resume_location_sound()
            return

        # Fetch stations in background
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
                    wx.CallAfter(self._resume_location_sound)
                    return
                wx.CallAfter(self._show_departure_board, stations, here_key, rt, source)
            except Exception as e:
                wx.CallAfter(self._status_update, f"Departure board failed: {e}", True)
                wx.CallAfter(self._resume_location_sound)

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
                "label": f"{s['name']} — {s['distance']}m",
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
            print(f"[GTFS] Google station discovery failed: {exc}")
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
        self._resume_location_sound()
        self.listbox.SetFocus()

    def _gtfs_stops_for_departure(self, departure):
        """Try to find GTFS stop sequence matching a HERE departure.

        Strategy:
        1. Search local feeds (by station coordinates) for matching route
        2. If no match, search the MobilityData catalog by operator name,
           download that feed, and search it

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
                        print(f"[GTFS] Rejected '{short}' — type '{rinfo.get('type')}' "
                              f"incompatible with HERE mode '{here_mode}'")
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
                                        print(f"[GTFS] Fuzzy headsign match: "
                                              f"'{headsign}' ~ '{hs_key}' "
                                              f"(score={max(fwd,rev):.2f})")
                                        found_hs = True
                                        break
                    if not found_hs:
                        continue

                stops = self._transit.stops_for_route(matched_rid, feed_id, headsign)
                if stops:
                    return stops

            if require_headsign and hs_lower:
                print(f"[GTFS] Route '{line}' found in feed {feed_id} "
                      f"but no variant has headsign matching '{headsign}'")
            return None

        def _keyword_search_feed(feed_id, keywords):
            """Search a feed's route long_names for routes containing ALL keywords.

            Returns list of (route_id, label) tuples for matching routes.
            """
            data = self._transit._feeds.get(feed_id, {})
            routes = data.get("routes", {})
            matches = []
            for rid, rinfo in routes.items():
                long = (rinfo.get("long") or "").strip().lower()
                short = (rinfo.get("short") or "").strip()
                if long and all(kw in long for kw in keywords):
                    label = short or rinfo.get("long", "")
                    rtype = rinfo.get("type", "")
                    matches.append((rid, f"{label} ({rtype}): {rinfo.get('long', '')}"))
            return matches

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
                    print(f"[GTFS] Found '{line}' in feed {feed_id} "
                          f"(mode check relaxed)")
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
                print(f"[GTFS] Operator map: '{operator}' → feed {cached_fid}")
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
            print(f"[GTFS] No local match for '{line}' ({here_mode}). "
                  f"Searching catalog for operator '{operator}'...")
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
                        print(f"[GTFS] Trying operator feed {fid} "
                              f"({row.get('provider', 'unknown')})")
                        try:
                            _fid, _data = self._transit._gtfs_ensure(fid, url)
                        except Exception as exc:
                            print(f"[GTFS] Feed {fid} load failed: {exc}")
                            continue
                        if not _data:
                            continue
                        result = _search_feed(fid, skip_mode_check=True, require_headsign=True)
                        if result:
                            print(f"[GTFS] Found route '{line}' in feed {fid}")
                            # Save mapping for next time
                            self._save_operator_map(op_lower, fid)
                            return result
            except Exception as exc:
                print(f"[GTFS] Operator catalog search failed: {exc}")

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
        if _dest_query and not candidates:
            try:
                rt_obj = self._get_route_tools()
            except Exception:
                rt_obj = None
            if rt_obj and rt_obj.is_configured:
                try:
                    d_lat, d_lon, d_fmt = rt_obj.geocode(_dest_query)
                    d_km = ((lat - d_lat)**2 * 111**2
                            + (lon - d_lon)**2 * (111 * math.cos(
                                math.radians(lat)))**2) ** 0.5
                    print(f"[GTFS] Destination geocode '{_dest_query}' → "
                          f"({d_lat:.3f},{d_lon:.3f}) {d_fmt}  dist={d_km:.0f}km")
                    if d_km <= 800:
                        _dest_feed_ids = self._transit._ensure_feeds_for_location(
                            d_lat, d_lon)
                        print(f"[GTFS] Destination feeds: {_dest_feed_ids}")
                        _collect_last_stop_candidates(_dest_feed_ids)
                        _collect_longname_candidates(_dest_feed_ids)
                    else:
                        print(f"[GTFS] Destination too far ({d_km:.0f}km) — "
                              f"skipping feed download")
                except Exception as exc:
                    print(f"[GTFS] Destination geocode failed: {exc}")

        candidates.sort(key=lambda c: -c["score"])
        print(f"[GTFS] Candidate scan: {len(candidates)} direction(s) "
              f"match headsign '{headsign}'")

        # ── Return ────────────────────────────────────────────────────
        if len(candidates) == 1:
            stop_names = [s.get("name", s.get("stop_name", "Unknown"))
                          for s in candidates[0]["stop_list"]]
            print(f"[GTFS] Single candidate — auto-picking: {candidates[0]['label']}")
            return stop_names

        if candidates:
            print(f"[GTFS] Returning {len(candidates)} candidates for user choice")
            return {"__candidates__": candidates}

        here_info = "No timetable data found for this service."
        if line:
            here_info += f"  Line: {line}."
        if headsign:
            here_info += f"  Direction: {headsign}."
        if operator:
            here_info += f"  Operator: {operator}."
        return [here_info]

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

    def _show_route_results(self, title: str, text: str):
        """Display route results in a read-only dialog."""
        dlg = self._dlgs[5](self, title, text)
        dlg.ShowModal()
        dlg.Destroy()
        self._resume_location_sound()
        self.listbox.SetFocus()


    def _route_from_mark(self, origin_coords, origin_name, dest_coords, dest_name):
        """Shift+D — driving directions from M-marked origin to current position.
        Detects cross-water routes and describes them using airports data."""
        o_lat, o_lon = origin_coords
        d_lat, d_lon = dest_coords

        self._status_update(f"Getting directions from {origin_name} to {dest_name}...")

        def _calc():
            try:
                # Check if same country — extract from names (last comma-separated part)
                o_country = origin_name.split(",")[-1].strip()
                d_country = dest_name.split(",")[-1].strip()

                if o_country != d_country:
                    text = self._cross_water_description(
                        o_lat, o_lon, origin_name,
                        d_lat, d_lon, dest_name)
                else:
                    text = self._driving_directions(
                        o_lat, o_lon, origin_name,
                        d_lat, d_lon, dest_name)

                wx.CallAfter(self._show_route_results,
                             f"{origin_name} → {dest_name}", text)
            except Exception as e:
                wx.CallAfter(self._status_update, f"Directions failed: {e}", True)

        threading.Thread(target=_calc, daemon=True).start()

    def _cross_water_description(self, o_lat, o_lon, o_name, d_lat, d_lon, d_name):
        """Describe a cross-country/water route — flight info and sea route."""
        import csv, math
        from geo import dist_metres
        from sea_routes import get_sea_route

        straight_km = dist_metres(o_lat, o_lon, d_lat, d_lon) / 1000.0

        def _nearest_airport(lat, lon):
            path = self._ensure_airports_csv()
            if not path:
                return None
            best_dist = float('inf')
            best = None
            with open(path, encoding='utf-8') as f:
                for row in csv.DictReader(f):
                    if row.get('type','') not in ('large_airport','medium_airport'):
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
                        best_dist = d
                        best = (row.get('name',''), row.get('iata_code','').strip(),
                                alat, alon, best_dist)
            return best

        o_airport = _nearest_airport(o_lat, o_lon)
        d_airport = _nearest_airport(d_lat, d_lon)

        mid_lat = (o_lat + d_lat) / 2
        mid_lon = (o_lon + d_lon) / 2
        water = self._ocean_name(mid_lat, mid_lon) or \
                self._ocean_name(o_lat, o_lon) or "open water"

        # Extract country and city from names (last / first comma-separated parts)
        o_parts  = [p.strip() for p in o_name.split(",")]
        d_parts  = [p.strip() for p in d_name.split(",")]
        o_country = o_parts[-1] if o_parts else ""
        d_country = d_parts[-1] if d_parts else ""
        o_city    = o_parts[0]  if o_parts else ""
        d_city    = d_parts[0]  if d_parts else ""

        lines = [
            f"Route: {o_name} → {d_name}",
            "",
            f"These locations are in different countries separated by {water}.",
            "No direct driving route exists.",
            "",
            f"Straight-line distance: {straight_km:,.0f}km",
            "",
            "── By Air ──────────────────────────────",
        ]

        if o_airport:
            lines.append(f"Nearest departure airport: {o_airport[0]}"
                         + (f" ({o_airport[1]})" if o_airport[1] else "")
                         + f" — {o_airport[4]:.0f}km from {o_city}")
        if d_airport:
            lines.append(f"Nearest arrival airport:   {d_airport[0]}"
                         + (f" ({d_airport[1]})" if d_airport[1] else "")
                         + f" — {d_airport[4]:.0f}km from {d_city}")

        if o_airport and d_airport:
            flight_km  = dist_metres(o_airport[2], o_airport[3],
                                     d_airport[2], d_airport[3]) / 1000.0
            flight_min = int(flight_km / 900 * 60)
            lines += [
                f"Flight distance: {flight_km:,.0f}km",
                f"Estimated flight time: {flight_min // 60}h {flight_min % 60}min"
                f" (at 900km/h cruising speed)",
            ]

        # Sea route
        sea = get_sea_route(o_country, o_city, o_lat, o_lon,
                            d_country, d_city, d_lat, d_lon)
        if sea:
            lines += ["", sea]
        else:
            lines += ["", "── By Sea ──────────────────────────────",
                      "No sea route data available for this corridor."]

        return "\n".join(lines)

    def _driving_directions(self, o_lat, o_lon, o_name, d_lat, d_lon, d_name):
        """Fetch driving directions. Google first, OSRM fallback."""
        api_key = self.settings.get("google_api_key", "").strip()

        if api_key:
            return self._google_driving(o_lat, o_lon, o_name, d_lat, d_lon, d_name, api_key)
        else:
            return self._osrm_driving(o_lat, o_lon, o_name, d_lat, d_lon, d_name)

    def _google_driving(self, o_lat, o_lon, o_name, d_lat, d_lon, d_name, api_key):
        """Google Maps driving directions."""
        import urllib.parse, urllib.request, json, re, ssl

        params = urllib.parse.urlencode({
            "origin":      f"{o_lat},{o_lon}",
            "destination": f"{d_lat},{d_lon}",
            "mode":        "driving",
            "key":         api_key,
        })
        url = f"https://maps.googleapis.com/maps/api/directions/json?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": "MapInABox/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())

        if data.get("status") != "OK":
            raise RuntimeError(f"Google: {data.get('status')}")

        leg = data["routes"][0]["legs"][0]

        def _strip(s):
            return re.sub(r'<[^>]+>', ' ', s).replace('&nbsp;', ' ').strip()

        total_m   = leg["distance"]["value"]
        total_min = leg["duration"]["value"] // 60
        steps     = leg["steps"]

        lines = [
            f"Driving directions: {o_name} → {d_name}",
            f"Distance: {total_m/1000:.1f}km  Estimated time: {total_min} min",
            "",
        ]
        for step in steps:
            dist = step["distance"]["text"]
            inst = re.sub(r'\s+', ' ', _strip(step["html_instructions"])).strip()
            lines.append(f"{inst}  ({dist})")

        return "\n".join(lines)

    def _osrm_driving(self, o_lat, o_lon, o_name, d_lat, d_lon, d_name):
        """OSRM free driving directions — no API key needed."""
        import urllib.request, json

        url = (f"http://router.project-osrm.org/route/v1/driving/"
               f"{o_lon},{o_lat};{d_lon},{d_lat}"
               f"?steps=true&annotations=false&geometries=geojson&overview=false")
        req = urllib.request.Request(url, headers={"User-Agent": "MapInABox/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())

        if data.get("code") != "Ok":
            raise RuntimeError(f"OSRM: {data.get('code','unknown error')}")

        route = data["routes"][0]
        total_m   = route["distance"]
        total_min = int(route["duration"] // 60)
        steps     = [s for leg in route["legs"] for s in leg["steps"]]

        lines = [
            f"Driving directions: {o_name} → {d_name}",
            f"Distance: {total_m/1000:.1f}km  Estimated time: {total_min} min",
            "",
        ]
        for step in steps:
            maneuver = step.get("maneuver", {})
            inst     = maneuver.get("type", "").replace("_", " ").title()
            modifier = maneuver.get("modifier", "")
            name     = step.get("name", "")
            dist_m   = step.get("distance", 0)
            dist_str = f"{dist_m/1000:.1f}km" if dist_m >= 1000 else f"{int(dist_m)}m"
            parts    = [p for p in [inst, modifier, name] if p]
            lines.append(f"{' '.join(parts)}  ({dist_str})")

        return "\n".join(lines)

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

        self._status_update(f"Searching flights {origin} → {dest}...")

        def _fetch():
            try:
                timetable_results = []

                try:
                    timetable_results = self._timetable.search(
                        origin, dest,
                        results=15,
                        sort="Duration")
                except Exception as e:
                    print(f"[FlightSearch] Timetable API error: {e}")

                if not timetable_results:
                    wx.CallAfter(self._status_update,
                                 f"No flights found from {origin} to {dest}.",
                                 True)
                    wx.CallAfter(self._resume_location_sound)
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
                wx.CallAfter(self._resume_location_sound)

        threading.Thread(target=_fetch, daemon=True).start()

    def _show_flight_results(self, text: str, origin: str, dest: str):
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
            self._resume_location_sound()

        btn.Bind(wx.EVT_BUTTON, _close)
        dlg.Bind(wx.EVT_CHAR_HOOK,
                 lambda e: _close() if e.GetKeyCode() == wx.WXK_ESCAPE else e.Skip())
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
            wx.CallAfter(lambda: wx.CallLater(2000, self._resume_location_sound))
            return

        labels = [c["label"] for c in choices]
        dlg2 = wx.SingleChoiceDialog(self, "Select location", "Location", labels)
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

        self._status_update(f"Searching hotels in {location_label}...")

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
                    wx.CallAfter(lambda: wx.CallLater(2000, self._resume_location_sound))
                    return

                def _show():
                    from dialogs import HotelResultsDialog

                    dlg = HotelResultsDialog(self, results)
                    while dlg.ShowModal() == wx.ID_OK:
                        idx = dlg.selected_index
                        if idx is None:
                            continue

                        hotel = results[idx]

                        import webbrowser, urllib.parse
                        website = None
                        try:
                            from here_poi import HereClient
                            here = HereClient(
                                self.settings.get("here_api_key", ""),
                                self.settings.get("cache_dir", ".")
                            )
                            detail = here.fetch_poi_detail(
                                hotel.get("name", ""),
                                hotel.get("lat", 0),
                                hotel.get("lon", 0)
                            )
                            website = detail.get("website")
                        except Exception:
                            pass

                        if website:
                            webbrowser.open(website)
                        else:
                            q = urllib.parse.quote(hotel.get("name", ""))
                            webbrowser.open(f"https://www.google.com/search?q={q}&btnI=1")

                    dlg.Destroy()
                    self._resume_location_sound()

                wx.CallAfter(_show)

            except Exception as exc:
                import traceback
                traceback.print_exc()
                wx.CallAfter(self._status_update, f"Hotel search failed: {exc}", True)
                wx.CallAfter(self._resume_location_sound)
 

        threading.Thread(target=_fetch, daemon=True).start()

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

        CORRIDOR_M   = 300   # metres either side of the route
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

        # ---- destination ---------------------------------------------------
        country = getattr(self, 'last_country_found', '') or ''
        dest_text = ""
        dest_fmt = dest_label or "current position"
        dest_lat = dest_lon = None
        if origin_coords is None and dest_coords is None:
            dest = getattr(self, "_map_destination", None)
            if not dest:
                self._status_update("Destination not set. Press D at the destination.", force=True)
                self._resume_location_sound()
                return
            marks = getattr(self, "_map_marks", {})
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
                self._resume_location_sound()
                return
            dlg = wx.SingleChoiceDialog(
                self, "Choose mark for food search:", "Find Food", choices)
            if dlg.ShowModal() != wx.ID_OK:
                dlg.Destroy()
                self._status_update("Find Food cancelled.", force=True)
                self._resume_location_sound()
                return
            slot = slots[dlg.GetSelection()]
            dlg.Destroy()
            mark = marks[slot]
            origin_coords = mark["coords"]
            dest_coords = dest["coords"]
            dest_label = dest.get("name", "destination")
            dest_fmt = dest_label
            dest_text = dest_label
            self._status_update(f"Finding food from mark {slot} to destination.")
        if dest_coords is not None:
            dest_lat, dest_lon = dest_coords
            dest_text = dest_fmt
        else:
            dlg = self._dlgs[1](self, "Find food on route to (suburb or address):",
                                default="")
            if dlg.ShowModal() != wx.ID_OK or not dlg.GetValue():
                dlg.Destroy()
                self._status_update("Find Food cancelled.", force=True)
                self._resume_location_sound()
                return
            dest_text = dlg.GetValue()
            dest_fmt = dest_text
            dlg.Destroy()

        if origin_coords is None:
            origin_lat = self.lat
            origin_lon = self.lon
        else:
            origin_lat, origin_lon = origin_coords
        self._find_food_populating = True
        self._status_update(f"Finding route to {dest_text}…")

        def _search():
            nonlocal dest_lat, dest_lon, dest_fmt
            try:
                # -- geocode destination ------------------------------------
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

                if dest_coords is None:
                    dest_lat, dest_lon, dest_fmt = rt.geocode(dest_text, country_code)

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
                    wx.CallAfter(self._resume_location_sound)
                    return

                parsed = rt._parse_route(raw_routes[0])
                encoded = parsed.get("polyline", "")
                if not encoded:
                    wx.CallAfter(self._status_update, "Route has no polyline.", True)
                    wx.CallAfter(self._resume_location_sound)
                    return

                points = _decode_polyline(encoded)  # list of (lat, lon)
                if len(points) < 2:
                    print(f"[FindFood] Polyline decoded to {len(points)} point(s) "
                          f"(dist={parsed.get('distance_m',0)}m) — using straight line fallback.")
                    points = [(origin_lat, origin_lon), (dest_lat, dest_lon)]

                dist_km = parsed.get("distance_m", 0) / 1000.0
                wx.CallAfter(self._status_update,
                    f"Route is {dist_km:.1f} km — searching for food…",
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
                    wx.CallAfter(self._resume_location_sound)
                    return

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
                        cumulative += _haversine_m(alat, alon, blat, blon)

                    if min_cross > CORRIDOR_M:
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
                    wx.CallAfter(self._resume_location_sound)
                    return

                places.sort(key=lambda p: p["along_m"])

                wx.CallAfter(self._status_update,
                    f"Found {len(places)} food place"
                    f"{'s' if len(places) != 1 else ''} along the route.",
                    True)
                wx.CallAfter(self._show_find_food_results, places)

            except Exception as exc:
                wx.CallAfter(self._status_update, f"Find Food failed: {exc}", True)
                wx.CallAfter(self._resume_location_sound)
            finally:
                wx.CallAfter(setattr, self, "_find_food_populating", False)

        threading.Thread(target=_search, daemon=True).start()

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

    def _tool_find_food_near_city(self, city_label: str, announce_start=True):
        """Find food near the currently focused city/map position."""
        from geo import dist_metres

        radius_m = 3500
        centre_lat = float(self.lat)
        centre_lon = float(self.lon)
        self._find_food_populating = True
        if announce_start:
            self._status_update(f"Finding food in {city_label}…")

        def _visible_and_accessible_status(msg, delay_ms=150):
            self.update_ui(msg)
            wx.CallLater(delay_ms, self._status_update, msg, True)

        def _search():
            try:
                query = (
                    f"[out:json][timeout:40];\n"
                    f"(\n"
                    f'  nwr["amenity"~"cafe|restaurant|bar|fast_food|pub|food_court|ice_cream"]'
                    f"(around:{radius_m},{centre_lat:.6f},{centre_lon:.6f});\n"
                    f'  nwr["shop"~"bakery|butcher"]'
                    f"(around:{radius_m},{centre_lat:.6f},{centre_lon:.6f});\n"
                    f");\n"
                    f"out center tags;"
                ).encode()

                from core import _overpass
                result = _overpass.poi_request(query, timeout=45)
                if not result or not result.get("elements"):
                    msg = f"No food places found in {city_label}."
                    wx.CallAfter(_visible_and_accessible_status, msg)
                    wx.CallAfter(self._resume_location_sound)
                    return

                places = []
                seen = set()
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
                    seen.add(dedup)

                    amenity = tags.get("amenity", tags.get("shop", ""))
                    kind_map = {
                        "cafe": "café", "restaurant": "restaurant",
                        "fast_food": "fast food", "bar": "bar",
                        "pub": "pub", "food_court": "food court",
                        "ice_cream": "ice cream", "bakery": "bakery",
                        "butcher": "butcher",
                    }
                    kind = kind_map.get(amenity, amenity)
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
                        suburb = (getattr(self, "last_city_found", "") or "").strip()
                    if suburb and suburb.lower() != "nan" and suburb not in address:
                        address = f"{address}, {suburb}" if address else suburb

                    distance = dist_metres(centre_lat, centre_lon, plat, plon)
                    places.append({
                        "name": name,
                        "lat": plat,
                        "lon": plon,
                        "kind": kind,
                        "address": address,
                        "phone": (
                            tags.get("phone", "").strip() or
                            tags.get("contact:phone", "").strip()
                        ),
                        "website": (
                            tags.get("website", "").strip() or
                            tags.get("contact:website", "").strip() or
                            tags.get("url", "").strip()
                        ),
                        "opening_hours": tags.get("opening_hours", "").strip(),
                        "along_m": distance,
                        "distance_label": "from centre",
                        "cross_street": "",
                    })

                if not places:
                    msg = f"No named food places found in {city_label}."
                    wx.CallAfter(_visible_and_accessible_status, msg)
                    wx.CallAfter(self._resume_location_sound)
                    return

                places.sort(key=lambda p: p["along_m"])
                msg = (
                    f"Found {len(places)} food place"
                    f"{'s' if len(places) != 1 else ''} in {city_label}."
                )
                wx.CallAfter(_visible_and_accessible_status, msg)
                wx.CallAfter(lambda: wx.CallLater(650, self._show_find_food_results, places))
            except Exception as exc:
                msg = f"Find Food failed: {exc}"
                wx.CallAfter(_visible_and_accessible_status, msg)
                wx.CallAfter(self._resume_location_sound)
            finally:
                wx.CallAfter(setattr, self, "_find_food_populating", False)

        threading.Thread(target=_search, daemon=True).start()

    def _tool_find_accommodation_near_city(self, city_label: str, announce_start=True):
        """Find accommodation near the currently focused city/map position."""
        from geo import dist_metres

        radius_m = 3500
        centre_lat = float(self.lat)
        centre_lon = float(self.lon)
        self._find_food_populating = True
        if announce_start:
            self._status_update(f"Finding accommodation in {city_label}...")

        def _visible_and_accessible_status(msg, delay_ms=150):
            self.update_ui(msg)
            wx.CallLater(delay_ms, self._status_update, msg, True)

        def _search():
            try:
                query = (
                    f"[out:json][timeout:40];\n"
                    f"(\n"
                    f'  nwr["tourism"~"hotel|motel|guest_house|hostel|apartment|chalet|camp_site|caravan_site"]'
                    f"(around:{radius_m},{centre_lat:.6f},{centre_lon:.6f});\n"
                    f");\n"
                    f"out center tags;"
                ).encode()

                from core import _overpass
                result = _overpass.poi_request(query, timeout=45)
                if not result or not result.get("elements"):
                    msg = f"No accommodation found in {city_label}."
                    wx.CallAfter(_visible_and_accessible_status, msg)
                    wx.CallAfter(self._resume_location_sound)
                    return

                places = []
                seen = set()
                kind_map = {
                    "hotel": "hotel",
                    "motel": "motel",
                    "guest_house": "guest house",
                    "hostel": "hostel",
                    "apartment": "apartment",
                    "chalet": "chalet",
                    "camp_site": "camp site",
                    "caravan_site": "caravan site",
                }

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
                    seen.add(dedup)

                    tourism = tags.get("tourism", "")
                    kind = kind_map.get(tourism, tourism or "accommodation")
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
                        suburb = (getattr(self, "last_city_found", "") or "").strip()
                    if suburb and suburb.lower() != "nan" and suburb not in address:
                        address = f"{address}, {suburb}" if address else suburb

                    distance = dist_metres(centre_lat, centre_lon, plat, plon)
                    places.append({
                        "name": name,
                        "lat": plat,
                        "lon": plon,
                        "kind": kind,
                        "address": address,
                        "phone": (
                            tags.get("phone", "").strip() or
                            tags.get("contact:phone", "").strip()
                        ),
                        "website": (
                            tags.get("website", "").strip() or
                            tags.get("contact:website", "").strip() or
                            tags.get("url", "").strip()
                        ),
                        "opening_hours": tags.get("opening_hours", "").strip(),
                        "along_m": distance,
                        "distance_label": "from centre",
                        "cross_street": "",
                    })

                if not places:
                    msg = f"No named accommodation found in {city_label}."
                    wx.CallAfter(_visible_and_accessible_status, msg)
                    wx.CallAfter(self._resume_location_sound)
                    return

                places.sort(key=lambda p: p["along_m"])
                msg = (
                    f"Found {len(places)} accommodation place"
                    f"{'s' if len(places) != 1 else ''} in {city_label}."
                )
                wx.CallAfter(_visible_and_accessible_status, msg)
                wx.CallAfter(lambda: wx.CallLater(
                    650, self._show_find_food_results, places, "Accommodation"))
            except Exception as exc:
                msg = f"Accommodation search failed: {exc}"
                wx.CallAfter(_visible_and_accessible_status, msg)
                wx.CallAfter(self._resume_location_sound)
            finally:
                wx.CallAfter(setattr, self, "_find_food_populating", False)

        threading.Thread(target=_search, daemon=True).start()

    def _show_find_food_results(self, places: list, title="Find Food"):
        """Show the FindFoodDialog with results."""
        from dialogs import FindFoodDialog

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

        dlg = FindFoodDialog(self, places, _detail_cb, title=title)
        dlg.ShowModal()
        dlg.Destroy()
        self._resume_location_sound()
        self.listbox.SetFocus()
