"""POI provider orchestration, caching, filtering, and ranking."""

import math
import re
import threading
import time

import wx

from distance_units import format_distance
from geo import dist_metres
from logging_utils import miab_log
from poi_fetch import (
    POI_BACKGROUND_RADIUS_METRES,
    POI_CATEGORY_CHOICES,
    filter_pois_by_category,
)

POI_LIVE_COOLDOWN_SECS = 3.0
POI_BACKGROUND_WAIT_SECS = 2.0


def _core_helper(name):
    from core import __dict__ as core_names
    return core_names[name]


def _load_suppressed():
    return _core_helper("_load_suppressed")()


def _is_suppressed(poi, suppressed):
    return _core_helper("_is_suppressed")(poi, suppressed)


def _load_renamed():
    return _core_helper("_load_renamed")()


def _apply_renames(pois, renamed):
    return _core_helper("_apply_renames")(pois, renamed)


class PoiSearchMixin:
    def _fetch_google_pois(self, category_key="all", radius=1000, name_filter=""):
        """Fetch Google POIs through PoiFetcher."""
        return self._poi_fetcher.fetch_google_pois(
            self.lat, self.lon,
            self.settings.get("google_api_key", ""),
            category_key=category_key,
            radius=radius,
            name_filter=name_filter,
        )

    def _queue_poi_live_search(self, still_in_progress=False, **params):
        """Queue the newest live POI search until the live-fetch cooldown expires.

        still_in_progress distinguishes two different situations that both
        land here: an actual live fetch is still running right now, versus
        the previous fetch already finished and this is just the brief
        pacing cooldown before another request is allowed. Saying "still
        working on the last search" in the second case is simply wrong —
        the last search is done — so it gets its own accurate wording.
        """
        params["lat"] = self.lat
        params["lon"] = self.lon
        self._pending_poi_live_search = params
        self._pending_poi_live_generation += 1
        generation = self._pending_poi_live_generation
        miab_log(
            "verbose",
            "p queued live POI search because another live fetch is active or cooling down.",
            self.settings,
        )
        # Previously this was silent — logged to file only, with nothing
        # spoken to the user. Since the caller sets self._loading = False
        # right before returning here, the loading-tick heartbeat doesn't
        # fire during this wait either, so a queued search gave zero
        # audible feedback until the eventual completion chime. Announce
        # it immediately instead.
        msg = ("Still working on the last search — this one will run next."
               if still_in_progress else
               "One moment before the next search starts...")
        wx.CallAfter(self._status_update, msg, True)
        self._start_pending_poi_feedback(generation)
        self._schedule_pending_poi_live_search(generation)

    def _start_pending_poi_feedback(self, generation):
        self._loading = True
        self._last_street_loading_beep_at = 0.0

        def _feedback_tick():
            params = getattr(self, "_pending_poi_live_search", None)
            if not params or generation != getattr(self, "_pending_poi_live_generation", 0):
                return
            if not (getattr(self, "street_mode", False) or getattr(self, "_free_mode", False)):
                return
            self._loading = True
            if getattr(self, "_poi_live_fetch_in_progress", False):
                wx.CallAfter(self._status_update, "Still working on the last search...", True)
            else:
                remaining = POI_LIVE_COOLDOWN_SECS - (
                    time.time() - getattr(self, "_poi_live_last_completed_at", 0.0)
                )
                if remaining <= 0:
                    return
                wx.CallAfter(
                    self._status_update,
                    f"Next POI search starts in {int(math.ceil(remaining))} seconds...",
                    True,
                )
            wx.CallAfter(wx.CallLater, 5000, _feedback_tick)

        wx.CallAfter(wx.CallLater, 4000, _feedback_tick)

    def _poi_live_fetch_started(self):
        self._poi_live_fetch_in_progress = True

    def _poi_live_fetch_finished(self):
        self._poi_live_fetch_in_progress = False
        self._poi_live_last_completed_at = time.time()
        if getattr(self, "_pending_poi_live_search", None):
            self._pending_poi_live_generation += 1
            self._schedule_pending_poi_live_search(self._pending_poi_live_generation)

    def _schedule_pending_poi_live_search(self, generation=None):
        if generation is None:
            generation = self._pending_poi_live_generation
        cooldown = POI_LIVE_COOLDOWN_SECS
        remaining = max(0.0, cooldown - (time.time() - getattr(self, "_poi_live_last_completed_at", 0.0)))
        delay_ms = int(max(remaining, 0.25) * 1000)
        # wx.CallLater (unlike wx.CallAfter) creates and starts a wx.Timer
        # immediately, which wx requires to happen on the main thread. This
        # method is called both from the main thread (when a search is first
        # queued) and from background fetch-completion threads (via
        # _poi_live_fetch_finished) — calling wx.CallLater directly from the
        # latter crashed with "timer can only be started from the main
        # thread", silently dropping the queued search. Routing the
        # CallLater creation itself through CallAfter makes this safe from
        # either thread.
        wx.CallAfter(wx.CallLater, delay_ms, self._run_pending_poi_live_search, generation)

    def _run_pending_poi_live_search(self, generation):
        params = getattr(self, "_pending_poi_live_search", None)
        if not params or generation != getattr(self, "_pending_poi_live_generation", 0):
            return
        if getattr(self, "_poi_live_fetch_in_progress", False):
            self._schedule_pending_poi_live_search(generation)
            return
        remaining = POI_LIVE_COOLDOWN_SECS - (time.time() - getattr(self, "_poi_live_last_completed_at", 0.0))
        if remaining > 0:
            self._schedule_pending_poi_live_search(generation)
            return
        queued_lat = params.pop("lat", None)
        queued_lon = params.pop("lon", None)
        if queued_lat is not None and queued_lon is not None:
            if dist_metres(self.lat, self.lon, queued_lat, queued_lon) > POI_BACKGROUND_RADIUS_METRES:
                miab_log("verbose", "p dropped queued live POI search because the map moved away.", self.settings)
                self._pending_poi_live_search = None
                self._loading = False
                return
        if not (getattr(self, "street_mode", False) or getattr(self, "_free_mode", False)):
            miab_log("verbose", "p dropped queued live POI search because map browsing is inactive.", self.settings)
            self._pending_poi_live_search = None
            self._loading = False
            return
        self._pending_poi_live_search = None
        threading.Thread(target=self._fetch_pois, kwargs=params, daemon=True).start()

    def _fetch_pois(self, category_key="all", radius=1000, timeout=30,
                    next_radius=0, name_filter="", street_filter="", source="",
                    bypass_live_guard=False):
        """Fetch POIs for *category_key* from the requested *source*.

        source: "osm" | "here" | "google" | "" (auto — here if key set, else osm)
        """
        import threading
        self._loading             = True
        self._poi_fetch_in_progress = True
        poi_context_generation = getattr(self, "_poi_context_generation", 0)
        category_key = (category_key or "all").lower()
        name_filter  = (name_filter or "").strip().lower()
        street_filter = (street_filter or "").strip().lower()
        here_key   = self.settings.get("here_api_key",   "").strip()
        google_key = self.settings.get("google_api_key", "").strip()
        if not source:
            poi_source = self.settings.get("poi_source", "osm")
            source = poi_source if (poi_source == "here" and here_key) else "osm"
        category_labels = dict(POI_CATEGORY_CHOICES)
        category_label  = category_labels.get(category_key, "All nearby")

        search_beep_started = False
        search_radii = [radius]
        if next_radius and next_radius > radius:
            search_radii.append(next_radius)
        if name_filter:
            # Name searches should use the radius the user chose directly.
            # Incremental radius probing caused extra Overpass requests and
            # made the search feel like it was hammering the servers.
            search_radii = [radius]
        else:
            # Keep plain category browsing local.
            search_radii = sorted(set(search_radii))

        live_started = False

        def _name_match(poi):
            if not name_filter:
                return True
            label = (poi.get("name") or poi.get("label") or "").lower()
            kind  = (poi.get("kind") or "").lower()
            return name_filter in label or name_filter in kind

        def _street_bare(text):
            parts = re.sub(r"[^a-z0-9 ]+", " ", str(text or "").lower()).split()
            suffixes = {
                "street", "st", "road", "rd", "avenue", "ave", "drive", "dr",
                "court", "ct", "place", "pl", "crescent", "cres", "lane", "ln",
                "grove", "gr", "parade", "pde", "terrace", "tce", "way",
            }
            if parts and parts[-1] in suffixes:
                parts = parts[:-1]
            return " ".join(parts)

        street_filter_bare = _street_bare(street_filter)

        def _street_match(poi):
            if not street_filter_bare:
                return True
            tags = poi.get("tags", {}) if isinstance(poi, dict) else {}
            values = [
                poi.get("street", ""),
                poi.get("address", ""),
                poi.get("addr", ""),
                poi.get("label", ""),
                tags.get("addr:street", ""),
            ]
            return any(street_filter_bare in _street_bare(v) for v in values)

        try:
            cached_presented = False
            cached_pois = []

            def _poi_context_is_current():
                return poi_context_generation == getattr(self, "_poi_context_generation", 0)

            def _discard_if_poi_context_changed(stage):
                if _poi_context_is_current():
                    return False
                miab_log(
                    "verbose",
                    f"p discarded {stage} POI result because the browsing context changed.",
                    self.settings,
                )
                self._loading = False
                self._poi_fetch_in_progress = False
                return True

            def _prepare_pois(raw_pois, radius_m=None):
                _suppressed = _load_suppressed()
                _renamed    = _load_renamed()
                prepared = _apply_renames(
                    [p for p in raw_pois if not _is_suppressed(p, _suppressed)],
                    _renamed)
                for poi in prepared:
                    if "distance_m" not in poi and poi.get("dist") is not None:
                        poi["distance_m"] = poi["dist"]
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
                        item["distance_m"] = d
                        current.append(item)
                    prepared = current
                prepared.sort(key=lambda x: x.get("dist", float("inf")))
                return prepared

            if ((name_filter or street_filter)
                    and source in ("osm", "here")
                    and getattr(self, "_background_poi_fetch_in_progress", False)):
                miab_log(
                    "verbose",
                    "p keyword search waiting for in-progress background POI list.",
                    self.settings,
                )
                wx.CallAfter(self._status_update, "Checking loaded POIs...", True)
                wait_started = time.time()
                while (getattr(self, "_background_poi_fetch_in_progress", False)
                       and time.time() - wait_started < POI_BACKGROUND_WAIT_SECS):
                    if not _poi_context_is_current():
                        self._loading = False
                        self._poi_fetch_in_progress = False
                        return
                    time.sleep(0.25)

            background = list(getattr(self, "_all_pois", []) or [])
            if ((name_filter or street_filter)
                    and source in ("osm", "here")
                    and not background):
                disk_bg = self._poi_fetcher.load_cached_pois(self.lat, self.lon)
                if disk_bg:
                    self._all_pois = self._merge_personal_pois(disk_bg)
                    self._poi_grid = self._build_poi_grid(self._all_pois)
                    self._poi_fetch_lat = self.lat
                    self._poi_fetch_lon = self.lon
                    background = list(self._all_pois)
                    miab_log(
                        "verbose",
                        f"p loaded {len(background)} background POIs from disk for keyword search.",
                        self.settings,
                    )
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
                radius_covered = max(search_radii) <= POI_BACKGROUND_RADIUS_METRES
                if name_filter or street_filter:
                    miab_log(
                        "verbose",
                        f"p keyword cache check: background={len(background)} "
                        f"sources={sorted(sources)} source={source} "
                        f"distance={background_distance:.0f}m "
                        f"radius={max(search_radii)}m "
                        f"source_matches={source_matches} "
                        f"location_matches={location_matches} "
                        f"radius_covered={radius_covered}.",
                        self.settings,
                    )
                if source_matches and location_matches and radius_covered:
                    cached_pois = filter_pois_by_category(background, category_key)
                    cached_pois = [p for p in cached_pois if _name_match(p) and _street_match(p)]
                    cached_pois = _prepare_pois(cached_pois, radius_m=max(search_radii))
                    if name_filter or street_filter:
                        if cached_pois:
                            if not _poi_context_is_current():
                                miab_log(
                                    "verbose",
                                    "p discarded cached POI result because the browsing context changed.",
                                    self.settings,
                                )
                                self._loading = False
                                self._poi_fetch_in_progress = False
                                return
                            self._poi_list = cached_pois
                            self._poi_index = 0
                            filters = []
                            if name_filter:
                                filters.append(f"name '{name_filter}'")
                            if street_filter:
                                filters.append(f"street '{street_filter}'")
                            miab_log(
                                "verbose",
                                f"p served keyword search from loaded background POIs: "
                                f"{len(cached_pois)} {category_key} matching "
                                f"{', '.join(filters)} via {source}; live search skipped.",
                                self.settings,
                            )
                            wx.CallAfter(self._present_poi_list)
                        else:
                            miab_log(
                                "verbose",
                                f"p loaded background POIs cover this area; no cached "
                                f"{category_key} result matching name='{name_filter}' "
                                f"street='{street_filter}'. Live search skipped.",
                                self.settings,
                            )
                            filters = []
                            if name_filter:
                                filters.append(f"'{name_filter}'")
                            if street_filter:
                                filters.append(f"on {street_filter}")
                            what = " ".join(filters) if filters else category_label.lower()
                            wx.CallAfter(
                                self._retry_poi_name_search,
                                category_key,
                                name_filter,
                                street_filter,
                                source,
                                max(search_radii),
                            )
                        self._loading = False
                        self._poi_fetch_in_progress = False
                        return
                    if cached_pois:
                        if not _poi_context_is_current():
                            miab_log(
                                "verbose",
                                "p discarded cached POI result because the browsing context changed.",
                                self.settings,
                            )
                            self._loading = False
                            self._poi_fetch_in_progress = False
                            return
                        self._poi_list     = cached_pois
                        self._poi_index    = 0
                        cached_presented = True
                        filters = []
                        if name_filter:
                            filters.append(f"name '{name_filter}'")
                        if street_filter:
                            filters.append(f"street '{street_filter}'")
                        name_desc = " matching " + ", ".join(filters) if filters else ""
                        miab_log(
                            "verbose",
                            f"p served from in-memory background POIs: "
                            f"{len(cached_pois)} {category_key}{name_desc} results via {source}.",
                            self.settings,
                        )
                        wx.CallAfter(self._present_poi_list)
                        if not name_filter and max(search_radii) <= POI_BACKGROUND_RADIUS_METRES:
                            self._loading = False
                            self._poi_fetch_in_progress = False
                            return
                        if max(search_radii) <= POI_BACKGROUND_RADIUS_METRES:
                            live_reason = (
                                "typed search may have matches missing from "
                                "the background cache"
                            )
                        else:
                            live_reason = (
                                f"configured radius {max(search_radii)}m exceeds "
                                f"background radius {POI_BACKGROUND_RADIUS_METRES}m"
                            )
                        miab_log(
                            "verbose",
                            f"p doing live {source.upper()} fetch: {live_reason}.",
                            self.settings,
                        )
                    elif name_filter or street_filter:
                        miab_log(
                            "verbose",
                            f"p in-memory background POIs cover this area; "
                            f"no cached {category_key} result matching "
                            f"name='{name_filter}' street='{street_filter}'; "
                            f"continuing with live {source.upper()} search.",
                            self.settings,
                        )
                        wx.CallAfter(
                            self._status_update,
                            "No cached match yet. Searching live nearby...",
                            True,
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
            if name_filter or street_filter:
                miab_log(
                    "verbose",
                    f"p doing live {source.upper()} fetch for name='{name_filter}' street='{street_filter}'.",
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

            if not bypass_live_guard:
                fetch_actually_running = getattr(self, "_poi_live_fetch_in_progress", False)
                cooldown_remaining = POI_LIVE_COOLDOWN_SECS - (
                    time.time() - getattr(self, "_poi_live_last_completed_at", 0.0)
                )
                if fetch_actually_running or cooldown_remaining > 0:
                    self._queue_poi_live_search(
                        still_in_progress=fetch_actually_running,
                        category_key=category_key,
                        radius=radius,
                        timeout=timeout,
                        next_radius=next_radius,
                        name_filter=name_filter,
                        street_filter=street_filter,
                        source=source,
                    )
                    self._loading = False
                    self._poi_fetch_in_progress = False
                    return
            self._poi_live_fetch_started()
            live_started = True

            # A live fetch can take anywhere from a couple of seconds to
            # 20+ seconds (Overpass mirror retries, slow HERE/Google calls).
            # Without any feedback in that gap the app reads as hung. This
            # just speaks a periodic reassurance — it doesn't touch the
            # fetch, the sound, or anything else, and stays quiet if a
            # cached result was already shown (that case is a silent
            # "just in case" supplementary check the user isn't waiting on,
            # same reasoning as the alarm-sound guard just below).
            poi_wait_stop = threading.Event()
            if not cached_presented:
                def _poi_wait_ticker():
                    if poi_wait_stop.wait(4.0):
                        return
                    while not poi_wait_stop.wait(6.0):
                        wx.CallAfter(self._status_update, "Still working, please wait...", True)
                threading.Thread(target=_poi_wait_ticker, daemon=True).start()

            pois = []
            collected_name_pois = []
            collected_name_only_pois = []
            collected_seen: set[str] = set()
            collected_name_only_seen: set[str] = set()
            attempted_radius = radius

            def _collect(poi_list: list) -> None:
                for poi in poi_list:
                    name_matches = _name_match(poi)
                    if name_filter and street_filter and name_matches:
                        dedup_name = (poi.get("label", "").split(",")[0] or "").lower()
                        dedup = f"{dedup_name}|{poi.get('kind','')}|{round(poi['lat'],5)}|{round(poi['lon'],5)}"
                        if dedup not in collected_name_only_seen:
                            collected_name_only_seen.add(dedup)
                            collected_name_only_pois.append(poi)
                    if not name_matches or not _street_match(poi):
                        continue
                    dedup_name = (poi.get("label", "").split(",")[0] or "").lower()
                    dedup = f"{dedup_name}|{poi.get('kind','')}|{round(poi['lat'],5)}|{round(poi['lon'],5)}"
                    if dedup in collected_seen:
                        continue
                    collected_seen.add(dedup)
                    collected_name_pois.append(poi)

            for attempt_radius in search_radii:
                attempted_radius = attempt_radius
                if not name_filter and not street_filter:
                    miab_log(
                        "verbose",
                        f"p live fetch {category_key} radius={attempt_radius}m source={source}.",
                        self.settings,
                    )
                if source == "google" and google_key:
                    raw = self._fetch_google_pois(
                        category_key, radius=attempt_radius,
                        name_filter=name_filter)
                    if _discard_if_poi_context_changed("Google live"):
                        return
                    raw = filter_pois_by_category(raw, category_key)
                    pois = [p for p in raw if _name_match(p) and _street_match(p)]
                    pois.sort(key=lambda x: x.get("dist", float("inf")))
                    if name_filter or street_filter:
                        _collect(pois)
                elif source == "here" and here_key:
                    self._poi_fetcher.set_here_key(here_key)
                    cache_kind = "name" if name_filter else "category"
                    cache_extra = name_filter if name_filter else ""
                    cached_raw_pois = _live_cache_get(
                        cache_kind, source, category_key, attempt_radius,
                        cache_extra)
                    raw_pois = cached_raw_pois
                    if raw_pois is None:
                        if name_filter:
                            live_raw_pois = self._poi_fetcher.fetch_here_name_search(
                                self.lat, self.lon, name_filter,
                                radius=attempt_radius,
                                address_points=getattr(self, "_address_points", []))
                        else:
                            live_raw_pois, _ = self._poi_fetcher.fetch_pois(
                                self.lat, self.lon,
                                category=category_key, radius=attempt_radius,
                                timeout=timeout,
                                address_points=getattr(self, "_address_points", []),
                            )
                        if live_raw_pois is not None:
                            raw_pois = live_raw_pois
                            _live_cache_set(
                                cache_kind, source, category_key,
                                attempt_radius, raw_pois, cache_extra)
                    if _discard_if_poi_context_changed("HERE live"):
                        return
                    pois = [p for p in raw_pois if _name_match(p) and _street_match(p)]
                    if name_filter or street_filter:
                        _collect(pois)
                else:
                    # OSM — temporarily clear HERE key so fetch_pois uses Overpass
                    self._poi_fetcher.set_here_key("")
                    cached_raw_pois = _live_cache_get("category", source, category_key, attempt_radius)
                    raw_pois = cached_raw_pois
                    if raw_pois is None:
                        # Skip the searching sound entirely when a result was
                        # already presented instantly from the in-memory
                        # background cache (cached_presented). In that case
                        # this live fetch is just a silent "just in case"
                        # supplementary check the user isn't actively waiting
                        # on — there's no real searching happening from their
                        # perspective, so playing the alarm here is exactly
                        # the "alarm with nothing to search for" bug reported.
                        if not search_beep_started and not cached_presented:
                            try:
                                self.sound.play_file(r"c:\windows\media\alarm09.wav", loops=-1)
                                search_beep_started = True
                            except Exception:
                                pass
                        # Before hitting Overpass at the exact radius, check
                        # whether the background disk cache already covers
                        # this request. This prevents repeated Overpass calls
                        # for requests within the loaded POI radius.
                        if not name_filter and attempt_radius <= POI_BACKGROUND_RADIUS_METRES:
                            disk_bg = self._poi_fetcher.load_cached_pois(self.lat, self.lon)
                            if disk_bg:
                                self._all_pois      = self._merge_personal_pois(disk_bg)
                                self._poi_fetch_lat = self.lat
                                self._poi_fetch_lon = self.lon
                                category_pois = filter_pois_by_category(disk_bg, category_key)
                                if category_pois:
                                    raw_pois = category_pois
                                    _live_cache_set("category", source, category_key, attempt_radius, raw_pois)
                                    miab_log("verbose",
                                             f"p reused background disk cache for {category_key} radius={attempt_radius}m.",
                                             self.settings)
                                else:
                                    # Disk cache has no entries for this category — do not
                                    # cache the empty result; fall through to live Overpass fetch.
                                    miab_log("verbose",
                                             f"p background disk cache has no {category_key} entries; will try live fetch.",
                                             self.settings)
                        if not raw_pois:
                            live_raw_pois, _ = self._poi_fetcher.fetch_pois(
                                self.lat, self.lon,
                                category=category_key, radius=attempt_radius,
                                timeout=timeout,
                                address_points=getattr(self, "_address_points", []),
                            )
                            if live_raw_pois is not None:
                                raw_pois = live_raw_pois
                                _live_cache_set("category", source, category_key, attempt_radius, raw_pois)
                    if _discard_if_poi_context_changed("OSM live"):
                        self._poi_fetcher.set_here_key(here_key)
                        return
                    self._poi_fetcher.set_here_key(here_key)
                    pois = [p for p in raw_pois if _name_match(p) and _street_match(p)]
                    if name_filter or street_filter:
                        _collect(pois)
                        # Category-specific searches get a category-aware
                        # live name query. Generic all-category searches only
                        # use the live name query when the radius is small.
                        if name_filter and (category_key != "all" or attempt_radius <= 1000):
                            raw_name_pois = _live_cache_get(
                                "name", source, category_key, attempt_radius, name_filter)
                            if raw_name_pois is None:
                                raw_name_pois = self._poi_fetcher.fetch_osm_name_search(
                                    self.lat, self.lon,
                                    name_filter=name_filter,
                                    category_key=category_key,
                                    radius=attempt_radius,
                                    timeout=timeout,
                                    address_points=getattr(self, "_address_points", []),
                                )
                                if raw_name_pois is not None:
                                    # Only cache real results; server failures (None)
                                    # are left uncached so the next P press retries.
                                    _live_cache_set(
                                        "name", source, category_key, attempt_radius,
                                        raw_name_pois, name_filter)
                                else:
                                    raw_name_pois = []
                            pois = [p for p in raw_name_pois if _name_match(p)]
                            _collect(pois)
                if not name_filter and pois:
                    break

            if name_filter or street_filter:
                pois = collected_name_pois
                if not pois and name_filter and street_filter and collected_name_only_pois:
                    pois = collected_name_only_pois
                    miab_log(
                        "verbose",
                        f"p found no live POIs matching name='{name_filter}' "
                        f"on street='{street_filter}'; showing name-only nearby matches.",
                        self.settings,
                    )

            if getattr(self, "_poi_explore_stack", []):
                self._loading = False
                return

            if not _poi_context_is_current():
                miab_log(
                    "verbose",
                    "p discarded live POI result because the browsing context changed.",
                    self.settings,
                )
                self._loading = False
                self._poi_fetch_in_progress = False
                return

            pois = _prepare_pois(pois)
            if not pois and cached_presented:
                miab_log(
                    "verbose",
                    "p live fetch returned no extra results; keeping in-memory background POI list.",
                    self.settings,
                )
                self._loading = False
                self._poi_fetch_in_progress = False
                return
            self._poi_list     = pois
            self._poi_index    = 0
            self._loading      = False
            self._poi_fetch_in_progress = False

            if self._poi_list:
                wx.CallAfter(self._present_poi_list)
            else:
                filters = []
                if name_filter:
                    filters.append(f"'{name_filter}'")
                if street_filter:
                    filters.append(f"on {street_filter}")
                what = " ".join(filters) if filters else category_label.lower()
                if name_filter or street_filter:
                    wx.CallAfter(
                        self._retry_poi_name_search,
                        category_key,
                        name_filter,
                        street_filter,
                        source,
                        attempted_radius,
                    )
                else:
                    wx.CallAfter(
                        self._announce_transient_then_return,
                        f"No {what} found within {format_distance(attempted_radius)}.")

        except Exception as e:
            miab_log("errors", f"POI fetch error: {e}", self.settings)
            if not getattr(self, "_street_data_fetch_in_progress", False):
                self._loading = False
            self._poi_fetch_in_progress = False
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
        finally:
            if live_started:
                if '_poi_context_is_current' in locals() and _poi_context_is_current():
                    self._poi_live_fetch_finished()
                else:
                    self._poi_live_fetch_in_progress = False
                    miab_log(
                        "verbose",
                        "p stale live POI fetch finished after context changed; no cooldown applied.",
                        self.settings,
                    )
                    if getattr(self, "_pending_poi_live_search", None):
                        self._schedule_pending_poi_live_search(
                            getattr(self, "_pending_poi_live_generation", 0)
                        )
            if 'poi_wait_stop' in locals():
                poi_wait_stop.set()
            if search_beep_started:
                try:
                    self.sound.stop()
                except Exception:
                    pass
