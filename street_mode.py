"""Street Mode loading, caching, and address-data behaviour."""

import math
import re
import threading
import time
import urllib.parse
import urllib.request

import wx

from distance_units import format_distance
from free import FreeExploreEngine
from geo import haversine_m, dist_metres, nearest_point_on_segment
from logging_utils import miab_log
from world_map_panel import _IS_LAND

def _load_personal_pois():
    from core import _load_personal_pois as load_personal_pois
    return load_personal_pois()


def save_settings(settings):
    from core import save_settings as save
    return save(settings)


class StreetModeMixin:
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
        self._walking_mode      = False
        self._personal_pois     = _load_personal_pois()
        self._all_pois          = self._merge_personal_pois([])
        self._poi_grid          = self._build_poi_grid(self._all_pois)
        self._walk_graph        = None
        self._walk_node         = None
        self._walk_street       = None
        self._walk_heading      = 0.0
        self._walk_road_side    = None
        self._walk_side_name    = "unknown side"
        self._walk_side_anchor  = ""
        self._walk_on_anchor_side = None
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

    def _refresh_cached_timetables(self):
        """Force-refresh every timetable feed already cached on this device."""
        def status(message):
            wx.CallAfter(self._status_update, message, True)
        status("Preparing to update all cached timetables…")
        try:
            updated, failed = self._transit.refresh_cached_feeds(
                status_cb=status)
        except Exception as exc:
            miab_log(
                "errors", f"[Transit] Cached timetable update failed: {exc}",
                self.settings)
            status("Cached timetable update failed. Existing data was retained.")
            return
        if failed:
            status(
                f"Timetable update finished: {len(updated)} updated; "
                f"{len(failed)} failed and retained their previous data.")
        else:
            status(
                f"Timetable update finished: {len(updated)} feed"
                f"{'s' if len(updated) != 1 else ''} updated.")

    def _confirm_stale_gtfs_update(
        self,
        feed_id: str,
        age_days: float,
    ) -> bool:
        """Ask safely on the UI thread whether stale timetable data may update."""
        answer = {"update": False}

        def ask():
            days = max(8, math.ceil(age_days))
            dlg = wx.MessageDialog(
                self,
                f"Timetable data is {days} days old.\n\n"
                "Would you like to update it now?\n\n"
                "Choose No to use the existing timetable.",
                "Update Timetable Data?",
                wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION,
            )
            answer["update"] = dlg.ShowModal() == wx.ID_YES
            dlg.Destroy()

        if wx.IsMainThread():
            ask()
            return answer["update"]

        finished = threading.Event()

        def ask_and_signal():
            try:
                ask()
            finally:
                finished.set()

        wx.CallAfter(ask_and_signal)
        finished.wait()
        return answer["update"]

    def _confirm_or_toggle_gnaf_addresses(self):
        if str(getattr(self, "_current_country_code", "") or "").upper() != "AU":
            self._gnaf_toggle_pending_until = 0.0
            self._status_update(
                "GNAF addresses are available only in Australia.", force=True)
            return
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
        if str(getattr(self, "_current_country_code", "") or "").upper() != "AU":
            self._gnaf_toggle_pending_until = 0.0
            self._status_update(
                "GNAF addresses are available only in Australia.", force=True)
            return
        self._gnaf_toggle_pending_until = 0.0
        enabled = not self.settings.get("gnaf_enabled", True)
        self.settings["gnaf_enabled"] = enabled
        save_settings(self.settings)
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
