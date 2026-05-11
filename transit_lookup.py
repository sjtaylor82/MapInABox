"""transit_lookup.py — GTFS / transit timetable lookup for Map in a Box.

Replaces the old transit.py stub.  All GTFS parsing, MobilityData catalog
management, feed discovery, and timetable queries live here.

MapNavigator imports ``TransitLookup`` and holds one instance as
``self._transit``.  The split means MapNavigator no longer needs to know
anything about ZIP files, CSV catalogs, or departure-time arithmetic.

Key design decisions
--------------------
* The MobilityData catalog CSV is the only feed-discovery mechanism.
  The old ``TRANSIT_REGIONS`` hardcoded QLD list is gone — those feeds
  are all in the catalog.
* For rural / regional areas (e.g. Parkes NSW, Dubbo, Ballarat) the
  catalog lookup now accepts *all* bounding-box matches sorted by area
  and scans each parsed feed for stops within the walk radius.  This
  costs one extra ZIP download per region visited but works globally.
* The ``TransitLookup`` object caches parsed feeds in memory and on disk
  so repeated lookups in the same session are instant.
"""

import csv
import pickle
import io
import json
import os
import threading
import time
import urllib.request
import urllib.parse
import zipfile
import datetime
import math
from typing import Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CATALOG_CSV_URL   = "https://bit.ly/catalogs-csv"   # MobilityData ~3 MB
CATALOG_STALE_DAYS = 7
GTFS_STALE_DAYS   = 7

# Increment this whenever _parse_zip output structure changes (new fields etc).
# Any pickle saved with a different version will be discarded and re-parsed.
GTFS_PARSER_VERSION = 5

# Overrides file — supplementary feeds not in MobilityData catalog.
# Set OVERRIDES_SERVER_URL to your hosted copy so URL fixes propagate
# to users without an app update.  Leave blank to use local file only.
OVERRIDES_LOCAL_FILE = "gtfs_overrides.json"   # relative to script dir
OVERRIDES_SERVER_URL = ""                       # e.g. "https://yourserver.com/gtfs_overrides.json"
OVERRIDES_STALE_DAYS = 7

# POI kinds that trigger "Ask Gemini" option in the stop list
MAJOR_STATION_KINDS: frozenset = frozenset({
    "station", "halt", "bus station", "ferry terminal",
})

ROUTE_TYPE_LABELS: dict[str, str] = {
    "0": "tram", "1": "metro", "2": "train", "3": "bus",
    "4": "ferry", "5": "cable tram", "6": "aerial",
    "7": "funicular", "11": "trolleybus", "12": "monorail",
}

# OSM kind values that indicate a transit stop/station
TRANSIT_POI_KINDS: frozenset = frozenset({
    "station", "halt", "tram stop", "bus stop", "bus station",
    "ferry terminal", "stop position", "platform", "stop area",
})


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _t2s(t: str) -> int:
    """Convert HH:MM:SS (GTFS time, may exceed 24h) to seconds since midnight."""
    try:
        h, m, s = t.strip().split(":")
        return int(h) * 3600 + int(m) * 60 + int(s)
    except Exception:
        return -1


# ---------------------------------------------------------------------------
# TransitLookup
# ---------------------------------------------------------------------------

class TransitLookup:
    """Download, parse, cache, and query GTFS feeds for any location worldwide.

    Parameters
    ----------
    script_dir:
        Directory where ``gtfs_cache/`` and the catalog CSV are stored.
        Defaults to the directory containing this file.
    """

    def __init__(self, script_dir: Optional[str] = None, resource_dir: Optional[str] = None) -> None:
        import sys
        self._base         = script_dir or getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
        self._resource_dir = resource_dir or self._base
        self._feeds: dict[str, dict]    = {}
        self._location_feeds: dict[str, list[str]] = {}
        self._catalog_df      = None
        self._catalog_df_full = None
        self._geocode_cache: dict[str, tuple[str, str]] = {}
        self._catalog_lock = threading.Lock()
        self._overrides: dict | None  = None

    # ------------------------------------------------------------------
    # Public helpers used by MapNavigator
    # ------------------------------------------------------------------

    @staticmethod
    def is_transit_poi(poi: dict) -> bool:
        """Return True if ``poi["kind"]`` indicates a transit stop/station."""
        return poi.get("kind", "").lower() in TRANSIT_POI_KINDS

    def is_major_station(self, poi: dict) -> bool:
        """Return True if poi is a major station warranting an Ask Gemini option."""
        return poi.get("kind", "").lower() in MAJOR_STATION_KINDS

    def nearby_stops(
        self,
        lat: float,
        lon: float,
        radius: int = 200,
        status_cb=None,
    ) -> tuple[Optional[str], list[dict]]:
        """Return ``(primary_feed_id, stops)`` for all transit stops within *radius* metres.

        Automatically widens to 500m then 1000m if nothing is found at the
        requested radius — train stations often have GTFS stops placed on
        platforms that are 100–400m from the OSM building node.

        Each stop dict includes ``_feed_id`` so callers can query routes
        against the correct feed even when multiple feeds are merged.
        """
        if status_cb:
            status_cb("Loading transit data…")

        feed_ids = self._ensure_feeds_for_location(lat, lon)
        if not feed_ids:
            return None, []

        all_stops = []
        chosen_radius = None
        # Try the requested radius first, then auto-widen if we only found
        # anonymous interchange nodes or platform centroids with no departures.
        for search_radius in sorted({radius, 500, 1000}):
            candidate_stops = self._stops_within(feed_ids, lat, lon, search_radius)
            if not candidate_stops:
                continue
            if any(self._feeds.get(s.get("_feed_id"), {}).get("stop_departures", {}).get(s["stop_id"], []) for s in candidate_stops):
                all_stops = candidate_stops
                chosen_radius = search_radius
                break
            if not all_stops:
                all_stops = candidate_stops
                chosen_radius = search_radius
        if all_stops and chosen_radius is None:
            chosen_radius = max({radius, 500, 1000})

        all_stops.sort(
            key=lambda x: (
                0 if self._feeds.get(x.get("_feed_id"), {}).get("stop_departures", {}).get(x["stop_id"], []) else 1,
                x["distance"],
            )
        )

        # Save the winning feed to the verified index only if it has a substantial
        # number of stops — avoids small regional operators (e.g. Byron Easybus with
        # 27 stops) displacing the correct state-wide feed (e.g. TransLink 3048).
        if all_stops:
            winning_feed = all_stops[0].get("_feed_id") or (feed_ids[0] if feed_ids else None)
            if winning_feed:
                winning_data = self._feeds.get(winning_feed, {})
                n_stops = len(winning_data.get("stops", {}))
                # Only save if this feed has at least 50 stops — filters out tiny
                # regional operators that happened to have a stop nearby
                if n_stops >= 50:
                    region_key, _, _ = self._region_key_for(lat, lon)
                    if region_key:
                        index = self._load_verified_index()
                        existing = index.get(region_key, {}).get("feed_id")
                        if existing != winning_feed:
                            df = self._catalog_df_full if self._catalog_df_full is not None else self._catalog_df
                            self._save_to_verified_index(region_key, [winning_feed], df)
                else:
                    print(f"[Transit] Not saving feed {winning_feed} to verified index "
                          f"— only {n_stops} stops (too small to be authoritative)")

        primary_feed = feed_ids[0] if feed_ids else None
        return primary_feed, all_stops

    def _stops_within(
        self, feed_ids: list[str], lat: float, lon: float, radius: int
    ) -> list[dict]:
        """Return all stops across *feed_ids* within *radius* metres of (lat, lon)."""
        all_stops: list[dict] = []
        seen_ids: set[str] = set()
        for feed_id in feed_ids:
            data = self._feeds.get(feed_id)
            if not data:
                print(f"[Transit] _stops_within: feed {feed_id} not in _feeds!")
                continue
            for sid, s in data["stops"].items():
                d = math.sqrt(
                    ((lat - s["lat"]) * 111_000) ** 2
                    + ((lon - s["lon"]) * 111_000 * math.cos(math.radians(lat))) ** 2
                )
                if d <= radius:
                    uid = f"{feed_id}:{sid}"
                    if uid not in seen_ids:
                        seen_ids.add(uid)
                        all_stops.append({
                            **s,
                            "stop_id":  sid,
                            "distance": round(d),
                            "_feed_id": feed_id,
                        })
        return all_stops

    def find_stops_by_name(
        self,
        name: str,
        lat: float,
        lon: float,
        max_results: int = 8,
    ) -> tuple[Optional[str], list[dict]]:
        """Find stops whose name contains *name* (case-insensitive) in the feeds
        for this location.  Returns ``(primary_feed_id, stops)`` sorted by
        distance from (lat, lon).  Useful when coordinate-based search misses
        a station because the OSM centroid is far from the GTFS platform node.
        """
        feed_ids = self._ensure_feeds_for_location(lat, lon)
        if not feed_ids:
            return None, []

        query = name.lower().strip()
        results: list[dict] = []
        seen_ids: set[str] = set()

        for feed_id in feed_ids:
            data = self._feeds.get(feed_id)
            if not data:
                continue
            for sid, s in data["stops"].items():
                if query not in s["name"].lower():
                    continue
                uid = f"{feed_id}:{sid}"
                if uid in seen_ids:
                    continue
                seen_ids.add(uid)
                d = math.sqrt(
                    ((lat - s["lat"]) * 111_000) ** 2
                    + ((lon - s["lon"]) * 111_000 * math.cos(math.radians(lat))) ** 2
                )
                results.append({
                    **s,
                    "stop_id":  sid,
                    "distance": round(d),
                    "_feed_id": feed_id,
                })

        results.sort(key=lambda x: x["distance"])
        primary_feed = feed_ids[0] if feed_ids else None
        return primary_feed, results[:max_results]

    def routes_for_stop(self, stop_id: str, feed_id: str) -> list[dict]:
        """Return list of route dicts that serve *stop_id* in *feed_id*."""
        data = self._feeds.get(feed_id, {})
        route_ids = data.get("stop_routes", {}).get(stop_id, set())
        routes = []
        seen: set = set()
        for rid in route_ids:
            r = data.get("routes", {}).get(rid)
            if r:
                label = r["short"] or r["long"]
                key = (r["type"], label)
                if key not in seen:
                    seen.add(key)
                    routes.append(r)
        routes.sort(key=lambda x: (x["type"], x["short"] or x["long"]))
        return routes

    def routes_for_agency_name(
        self, operator_words: list[str], feed_id: str
    ) -> list[dict]:
        """Return routes in *feed_id* whose agency name contains ALL *operator_words*.

        *operator_words* should be lowercase significant words extracted from
        the HERE operator string (e.g. ["greyhound"] from "Greyhound Australia").
        Returns a list of route dicts (same shape as routes_for_stop results),
        sorted by type then short/long name.
        """
        data   = self._feeds.get(feed_id, {})
        routes = data.get("routes", {})
        result = []
        seen: set = set()
        for rid, r in routes.items():
            agency_lower = r.get("agency", "").lower()
            if not agency_lower:
                continue
            if all(w in agency_lower for w in operator_words):
                label = r["short"] or r["long"]
                key   = (r["type"], label)
                if key not in seen:
                    seen.add(key)
                    result.append(r)
        result.sort(key=lambda x: (x["type"], x["short"] or x["long"]))
        return result

    def stops_for_route(self, route_id: str, feed_id: str, headsign: str = "") -> list[dict]:
        """Return ordered stop list for *route_id* matching *headsign*.

        route_stops is now keyed by (route_id, headsign) so each direction
        is stored separately.  Matching order:
          1. Exact match
          2. Case-insensitive exact match
          3. Fuzzy word-overlap: score = |query_words ∩ candidate_words| / |query_words|
             Returns the best candidate if score >= 0.5 (i.e. at least half the
             query words appear in the GTFS headsign, or vice versa).
          4. Fallback: first entry for this route_id
        """
        import re as _re

        data     = self._feeds.get(feed_id, {})
        rs       = data.get("route_stops", {})
        hs_clean = headsign.strip().lower()

        # ── 1. Exact match ───────────────────────────────────────────
        exact = rs.get((route_id, headsign)) or rs.get((route_id, headsign.strip()))
        if exact:
            return exact

        # ── 2. Case-insensitive exact ────────────────────────────────
        for (rid, hs), stops in rs.items():
            if rid == route_id and hs.strip().lower() == hs_clean:
                return stops

        # ── 3. Fuzzy word-overlap ────────────────────────────────────
        _STOP_WORDS = frozenset({
            "to", "via", "the", "and", "from", "at", "of", "in",
            "on", "a", "an", "central", "station", "stop",
        })

        def _words(s: str) -> set:
            return {
                w for w in _re.sub(r"[^a-z0-9\s]", "", s.lower()).split()
                if w and w not in _STOP_WORDS and len(w) > 1
            }

        q_words = _words(headsign)
        best_score = 0.0
        best_stops: list[dict] = []

        if q_words:
            for (rid, hs), stops in rs.items():
                if rid != route_id:
                    continue
                c_words = _words(hs)
                if not c_words:
                    continue
                # Symmetric overlap: max of forward and reverse coverage
                fwd = len(q_words & c_words) / len(q_words)
                rev = len(q_words & c_words) / len(c_words)
                score = max(fwd, rev)
                if score > 0:
                    print(f"[Transit] Headsign fuzzy: query='{headsign}' "
                          f"candidate='{hs}' score={score:.2f}")
                if score > best_score:
                    best_score = score
                    best_stops = stops

        if best_score >= 0.5 and best_stops:
            print(f"[Transit] Headsign fuzzy match accepted (score={best_score:.2f})")
            return best_stops

        # ── 4. Fallback: first entry for this route_id ───────────────
        # Only fall back when NO headsign was given — if the caller supplied
        # a headsign and nothing matched, return [] so the caller can try a
        # different strategy (e.g. keyword search) rather than returning the
        # wrong direction's stops.
        if not headsign.strip():
            for (rid, hs), stops in rs.items():
                if rid == route_id:
                    return stops

        print(f"[Transit] stops_for_route: no headsign match for '{headsign}' "
              f"on route '{route_id}' — returning empty (best score={best_score:.2f})")
        return []

    def next_departures(
        self,
        stop_id: str,
        route_id: str,
        feed_id: str,
        n: int = 3,
    ) -> tuple[Optional[str], list[str]]:
        """Return ``(headsign, ["HH:MM", …])`` for the next *n* departures.

        Falls back to the earliest departures of the day if none remain today.
        Returns ``(None, [])`` if no data.
        """
        data = self._feeds.get(feed_id, {})
        deps = data.get("stop_departures", {}).get(stop_id, [])
        if not deps:
            deps = self._fallback_departures_for_route(stop_id, route_id, feed_id)
            if not deps:
                return None, []

        now = datetime.datetime.now()
        now_secs = now.hour * 3600 + now.minute * 60 + now.second

        upcoming = [(s, hs) for s, _tid, rid, hs in deps
                    if rid == route_id and s >= now_secs]
        if not upcoming:
            upcoming = [(s, hs) for s, _tid, rid, hs in deps if rid == route_id]
        if not upcoming:
            deps = self._fallback_departures_for_route(stop_id, route_id, feed_id)
            if not deps:
                return None, []
            upcoming = [(s, hs) for s, _tid, rid, hs in deps if rid == route_id and s >= now_secs]
            if not upcoming:
                upcoming = [(s, hs) for s, _tid, rid, hs in deps if rid == route_id]
            if not upcoming:
                return None, []

        headsign = upcoming[0][1]
        times = []
        for secs, _ in upcoming[:n]:
            h, m = divmod(secs // 60, 60)
            times.append(f"{h % 24:02d}:{m:02d}")
        return headsign, times

    def _fallback_departures_for_route(
        self,
        stop_id: str,
        route_id: str,
        feed_id: str,
        radius_m: int = 600,
    ) -> list[tuple[int, str, str, str]]:
        """Return departures from the nearest nearby stop that serves route_id.

        Some GTFS feeds expose an interchange node or platform centroid with no
        departure rows, while a nearby platform stop carries the actual times.
        This searches nearby stops in the same feed and borrows the closest
        usable departures for the same route.
        """
        data = self._feeds.get(feed_id, {})
        stops = data.get("stops", {})
        base = stops.get(stop_id)
        if not base:
            return []
        base_lat = base.get("lat")
        base_lon = base.get("lon")
        if base_lat is None or base_lon is None:
            return []

        best = None
        best_dist = float("inf")
        for sid, stop in stops.items():
            if sid == stop_id:
                continue
            if route_id not in data.get("stop_routes", {}).get(sid, set()):
                continue
            deps = data.get("stop_departures", {}).get(sid, [])
            if not deps:
                continue
            try:
                d = math.sqrt(
                    ((base_lat - stop["lat"]) * 111_000) ** 2
                    + ((base_lon - stop["lon"]) * 111_000 * math.cos(math.radians(base_lat))) ** 2
                )
            except Exception:
                continue
            if d <= radius_m and d < best_dist:
                best = deps
                best_dist = d
        return best or []

    def refresh_catalog(self) -> Optional[object]:
        """Force-download a fresh MobilityData catalog CSV and clear all caches.

        Called by F12.  Clears the verified feed index so every region is
        re-discovered against the fresh catalog on next visit.

        Returns the loaded DataFrame or None on failure.
        """
        # Clear catalog CSV
        p = self._catalog_csv_path()
        try:
            os.remove(p)
        except Exception:
            pass
        # Clear verified index — forces re-discovery on next visit
        vi = self._verified_index_path()
        try:
            os.remove(vi)
        except Exception:
            pass
        # Reset in-memory state
        self._catalog_df      = None
        self._catalog_df_full = None
        self._location_feeds.clear()
        self._geocode_cache.clear()
        print("[Transit] Cleared verified feed index and all caches")
        return self._ensure_catalog()

    # ------------------------------------------------------------------
    # Feed discovery
    # ------------------------------------------------------------------

    def _ensure_feeds_for_location(
        self, lat: float, lon: float
    ) -> list[str]:
        """Return feed_ids that cover (lat, lon), downloading as needed.

        Lookup order:
          1. In-memory session cache (_location_feeds) — instant
          2. Verified index on disk (gtfs_verified.json) — persists across sessions,
             built up as new regions are visited for the first time
          3. Discovery: catalog bbox → country fallback → KDTree
             On success, result is saved to the verified index for next time.
        """
        loc_key = f"{round(lat, 2)}_{round(lon, 2)}"

        # ── 1. In-memory session cache ────────────────────────────────
        cached = self._location_feeds.get(loc_key)
        if cached is not None:
            return cached

        # ── 2. Verified index (persistent, built by prior discovery) ──
        region_key, country_code, subdivision = self._region_key_for(lat, lon)
        if region_key:
            index = self._load_verified_index()
            entry = index.get(region_key)
            if entry:
                fid = entry["feed_id"]
                url = entry["url"]
                _fid, _data = self._gtfs_ensure(fid, url)
                if _data:
                    # Sanity-check: verify the feed still has stops near here.
                    # Catches cases where a bad feed was previously saved to the index.
                    closest_d = self._nearest_stop_distance(lat, lon, _data)
                    if closest_d <= 100_000:
                        print(f"[Transit] Verified index: feed {fid} for {region_key}")
                        # Always append supplementary override feeds so e.g. Queensland
                        # Rail appears alongside Sunbus even on cached repeat visits.
                        feed_ids = [fid]
                        for ov_fid, ov_url in self._overrides_for_region(country_code, subdivision):
                            if ov_fid in feed_ids:
                                continue
                            _of, _od = self._gtfs_ensure(ov_fid, ov_url)
                            if _od:
                                od = self._nearest_stop_distance(lat, lon, _od)
                                if od <= 100_000:
                                    feed_ids.append(ov_fid)
                                else:
                                    self._feeds.pop(ov_fid, None)
                        self._location_feeds[loc_key] = feed_ids
                        return feed_ids
                    # Bad entry — evict from index and memory, re-discover
                    print(f"[Transit] Verified index: feed {fid} invalid "
                          f"(nearest stop {closest_d/1000:.0f}km) — evicting")
                    self._feeds.pop(fid, None)
                    index.pop(region_key, None)
                    self._save_verified_index(index)
                else:
                    # ZIP gone or corrupt — fall through to re-discover
                    print(f"[Transit] Verified index: feed {fid} unavailable, re-discovering")

        # ── 3. Discovery ──────────────────────────────────────────────
        df = self._catalog_df
        if df is None:
            df = self._ensure_catalog()
            self._catalog_df = df

        feed_ids: list[str] = []

        if df is not None and len(df):
            # 3a. Bbox spatial search
            col_sub = "location.subdivision_name"
            mask = (
                (df["location.bounding_box.minimum_latitude"]  <= lat)
                & (df["location.bounding_box.maximum_latitude"]  >= lat)
                & (df["location.bounding_box.minimum_longitude"] <= lon)
                & (df["location.bounding_box.maximum_longitude"] >= lon)
            )
            matches = df[mask].copy()
            if not matches.empty:
                matches["bbox_area"] = (
                    (matches["location.bounding_box.maximum_latitude"]
                     - matches["location.bounding_box.minimum_latitude"])
                    * (matches["location.bounding_box.maximum_longitude"]
                       - matches["location.bounding_box.minimum_longitude"])
                )
                matches = matches.sort_values("bbox_area")
                for _, row in matches.iterrows():
                    fid = str(row["mdb_source_id"])
                    url = str(row["urls.direct_download"])
                    if not fid or not url or url == "nan":
                        continue
                    # Reject subdivision mismatch BEFORE downloading — e.g. NSW
                    # feed whose bbox happens to cover part of QLD.
                    if subdivision and col_sub in matches.columns:
                        feed_subdiv = str(row.get(col_sub, "")).strip().lower()
                        if feed_subdiv and subdivision.lower()[:6] not in feed_subdiv:
                            print(f"[Transit] Bbox skipping feed {fid} before download — "
                                  f"subdivision '{feed_subdiv}' doesn't match '{subdivision}'")
                            continue
                    _fid, _data = self._gtfs_ensure(fid, url)
                    if not _data:
                        continue
                    closest_d = self._nearest_stop_distance(lat, lon, _data)
                    n_stops   = len(_data.get("stops", {}))
                    print(f"[Transit] Feed {fid}: nearest stop {closest_d/1000:.1f}km, {n_stops} stops")
                    if closest_d > 100_000:
                        print(f"[Transit] Skipping feed {fid} — nearest stop "
                              f"{closest_d/1000:.1f}km away (wrong region)")
                        self._feeds.pop(fid, None)
                        continue
                    feed_ids.append(fid)
                    if (closest_d <= 5_000 and n_stops >= 50) or len(feed_ids) >= 4:
                        break

            # 3b. Country/subdivision fallback
            # Run if bbox found nothing, OR if bbox only found small feeds
            # (< 50 stops) that are unlikely to be the primary network
            bbox_adequate = any(
                len(self._feeds.get(fid, {}).get("stops", {})) >= 50
                for fid in feed_ids
            )
            if not feed_ids or not bbox_adequate:
                country_ids = self._country_fallback(
                    df, lat, lon, country_code, subdivision, region_key)
                # Merge — country fallback may find the real primary feed
                for fid in country_ids:
                    if fid not in feed_ids:
                        feed_ids.append(fid)

            # 3c. KDTree centroid fallback
            if not feed_ids:
                feed_ids = self._kdtree_fallback(df, lat, lon)

        # ── 4. Supplementary overrides (always appended) ──────────────
        # Load any entries from gtfs_overrides.json for this region.
        # These run regardless of whether catalog discovery found anything,
        # so Queensland Rail appears alongside Sunbus even when Sunbus was
        # already found via the verified index on the previous pass.
        override_entries = self._overrides_for_region(country_code, subdivision)
        for ov_fid, ov_url in override_entries:
            if ov_fid in feed_ids:
                continue   # already loaded this session
            _fid, _data = self._gtfs_ensure(ov_fid, ov_url)
            if not _data:
                continue
            closest_d = self._nearest_stop_distance(lat, lon, _data)
            if closest_d > 100_000:
                print(f"[Transit] Override feed {ov_fid} skipped — "
                      f"nearest stop {closest_d/1000:.0f}km away")
                self._feeds.pop(ov_fid, None)
                continue
            feed_ids.append(ov_fid)

        self._location_feeds[loc_key] = feed_ids
        return feed_ids

    def _nearest_stop_distance(self, lat: float, lon: float, data: dict) -> float:
        """Return metres to the nearest stop in *data*, or inf if no stops."""
        return min(
            (math.sqrt(
                ((lat - s["lat"]) * 111_000) ** 2
                + ((lon - s["lon"]) * 111_000 * math.cos(math.radians(lat))) ** 2
            ) for s in data["stops"].values() if s["lat"] or s["lon"]),
            default=float("inf"),
        )

    def _region_key_for(
        self, lat: float, lon: float
    ) -> tuple[str, str, str]:
        """Return (region_key, country_code, subdivision) for (lat, lon).

        Uses a cached geocode result so Nominatim is only called once per
        unique rounded coordinate.  Returns ("", "", "") on failure.
        """
        geo_key = f"{round(lat, 1)}_{round(lon, 1)}"
        cached = self._geocode_cache.get(geo_key)
        if cached is not None:
            country_code, subdivision = cached
        else:
            country_code, subdivision = self._reverse_geocode_country(lat, lon)
            self._geocode_cache[geo_key] = (country_code, subdivision)

        if not country_code:
            return "", "", ""
        region_key = f"{country_code}_{subdivision}" if subdivision else country_code
        return region_key, country_code, subdivision

    def _save_to_verified_index(
        self, region_key: str, feed_ids: list[str], df
    ) -> None:
        """Persist the first verified feed for *region_key* to gtfs_verified.json."""
        if not feed_ids:
            return
        fid = feed_ids[0]
        # Look up the download URL from the catalog or existing zip
        url = ""
        if df is not None:
            try:
                row = df[df["mdb_source_id"].astype(str) == fid]
                if not row.empty:
                    url = str(row.iloc[0]["urls.direct_download"])
                    if url == "nan":
                        url = ""
            except Exception:
                pass
        if not url:
            # Try full catalog
            df_full = self._catalog_df_full
            if df_full is not None:
                try:
                    row = df_full[df_full["mdb_source_id"].astype(str) == fid]
                    if not row.empty:
                        url = str(row.iloc[0]["urls.direct_download"])
                        if url == "nan":
                            url = ""
                except Exception:
                    pass
        index = self._load_verified_index()
        index[region_key] = {"feed_id": fid, "url": url}
        self._save_verified_index(index)
        print(f"[Transit] Saved feed {fid} for region '{region_key}' to verified index")

    def _reverse_geocode_country(self, lat: float, lon: float) -> tuple[str, str]:
        """Return (country_code, subdivision) for (lat, lon) via Nominatim.

        Returns ("", "") on failure.  Results are not cached — only called
        once per location when bbox search fails.
        """
        try:
            import urllib.request, urllib.parse, json
            params = urllib.parse.urlencode({
                "lat": round(lat, 4), "lon": round(lon, 4),
                "format": "json", "zoom": 5, "addressdetails": 1,
            })
            req = urllib.request.Request(
                f"https://nominatim.openstreetmap.org/reverse?{params}",
                headers={"User-Agent": "MapInABox/1.0"},
            )
            with urllib.request.urlopen(req, timeout=8) as r:
                data = json.loads(r.read().decode())
            addr = data.get("address", {})
            country_code = addr.get("country_code", "").upper()
            subdivision  = (addr.get("state") or addr.get("province")
                            or addr.get("region") or "")
            print(f"[Transit] Geocoded ({lat:.2f},{lon:.2f}) → "
                  f"country={country_code} subdivision={subdivision}")
            return country_code, subdivision
        except Exception as exc:
            print(f"[Transit] Reverse geocode failed: {exc}")
            return "", ""

    def _country_fallback(
        self, df, lat: float, lon: float,
        country_code: str = "", subdivision: str = "",
        region_key: str = ""
    ) -> list[str]:
        """Find feeds by country/subdivision when bbox search yields nothing valid.

        Uses pre-geocoded country_code/subdivision from _ensure_feeds_for_location
        so Nominatim is not called twice.  Falls back to geocoding if not supplied.
        """
        try:
            import pandas as pd
        except ImportError:
            return []
        # Use the full catalog (no bbox requirement) so we catch feeds that
        # are valid but lack bounding box data in the catalog
        df = self._catalog_df_full if self._catalog_df_full is not None else df
        if not country_code:
            country_code, subdivision = self._reverse_geocode_country(lat, lon)
        if not country_code:
            return []

        # Filter by country code
        col_cc = "location.country_code"
        col_sub = "location.subdivision_name"
        if col_cc not in df.columns:
            return []

        country_mask = df[col_cc].astype(str).str.upper() == country_code
        country_df = df[country_mask].copy()

        if country_df.empty:
            print(f"[Transit] No catalog feeds for country {country_code}")
            return []

        # Prefer feeds whose subdivision matches (e.g. "Queensland")
        if subdivision and col_sub in country_df.columns:
            sub_mask = country_df[col_sub].astype(str).str.lower().str.contains(
                subdivision.lower()[:6], na=False   # first 6 chars avoids encoding issues
            )
            preferred = country_df[sub_mask]
            rest      = country_df[~sub_mask]
            country_df = pd.concat([preferred, rest])

        # Sort candidates to try the most-likely feed first, minimising downloads.
        # Sort priority (all ascending, so 0 beats 1):
        #   1. Subdivision match — QLD feeds always before NSW/WA feeds when searching QLD
        #   2. Major operator keyword — state agencies before tiny regional ones
        #   3. Larger bbox area — broader coverage first (most have 0.0, so mainly a tiebreak)
        #   4. Higher feed id — more recent feed wins ties
        _major = ("transport for", "translink", "transperth", "ptv",
                  "public transport", "metro", "transit")
        _sub_key = subdivision.lower()[:6] if subdivision else ""
        def _sort_key(row):
            provider = str(row.get("provider", "")).lower()
            in_subdiv = bool(_sub_key and _sub_key in
                             str(row.get("location.subdivision_name", "")).lower())
            is_major = any(k in provider for k in _major)
            try:
                minlat = float(row["location.bounding_box.minimum_latitude"])
                maxlat = float(row["location.bounding_box.maximum_latitude"])
                minlon = float(row["location.bounding_box.minimum_longitude"])
                maxlon = float(row["location.bounding_box.maximum_longitude"])
                area = (maxlat - minlat) * (maxlon - minlon)
            except Exception:
                area = 0.0
            try:
                fid_int = -int(row["mdb_source_id"])  # higher id = more recent = better
            except Exception:
                fid_int = 0
            # No-bbox major operators (area==0.0) are likely state-wide networks —
            # sort them FIRST among majors, not last. Small-bbox feeds sort by -area.
            if is_major and area == 0.0:
                area_key = -999999  # sorts before any real area
            else:
                area_key = -area
            return (0 if in_subdiv else 1, 0 if is_major else 1, area_key, fid_int)

        sort_keys = [_sort_key(row) for _, row in country_df.iterrows()]
        country_df = country_df.iloc[
            sorted(range(len(country_df)), key=lambda i: sort_keys[i])
        ].reset_index(drop=True)

        print(f"[Transit] Country fallback: {len(country_df)} feeds in {country_code}"
              + (f"/{subdivision}" if subdivision else ""))

        feed_ids: list[str] = []
        primary_found = False
        for _, row in country_df.iterrows():
            fid = str(row["mdb_source_id"])
            url = str(row.get("urls.direct_download", ""))
            if not fid or not url or url == "nan":
                continue

            # Pre-filter: skip feeds whose bbox centroid is more than 150km away.
            # This avoids downloading Mackay/Cairns/Innisfail zips when searching
            # Brisbane — saves significant bandwidth and time.
            try:
                clat = (float(row["location.bounding_box.minimum_latitude"]) +
                        float(row["location.bounding_box.maximum_latitude"])) / 2
                clon = (float(row["location.bounding_box.minimum_longitude"]) +
                        float(row["location.bounding_box.maximum_longitude"])) / 2
                centroid_d = math.sqrt(
                    ((lat - clat) * 111_000) ** 2
                    + ((lon - clon) * 111_000 * math.cos(math.radians(lat))) ** 2
                )
                if centroid_d > 150_000:
                    print(f"[Transit] Country fallback pre-skipping {fid} — "
                          f"centroid {centroid_d/1000:.0f}km away")
                    continue
            except Exception:
                pass  # no bbox — can't pre-filter, proceed to download

            _fid, _data = self._gtfs_ensure(fid, url)
            if not _data:
                continue
            closest_d = min(
                (math.sqrt(
                    ((lat - s["lat"]) * 111_000) ** 2
                    + ((lon - s["lon"]) * 111_000
                       * math.cos(math.radians(lat))) ** 2
                ) for s in _data["stops"].values() if s["lat"] or s["lon"]),
                default=float("inf"),
            )
            if closest_d > 100_000:
                print(f"[Transit] Country fallback skipping {fid} — "
                      f"nearest stop {closest_d/1000:.0f}km away")
                self._feeds.pop(fid, None)
                continue
            # Once a primary feed (stop within 5km) is found, only accept
            # additional feeds within 10km — avoids distant regional operators.
            if primary_found and closest_d > 10_000:
                print(f"[Transit] Country fallback skipping {fid} — "
                      f"primary already found, {closest_d/1000:.0f}km too far")
                self._feeds.pop(fid, None)
                continue
            # If a subdivision-matching primary exists, skip feeds from other
            # subdivisions (e.g. skip NSW feeds when QLD primary is found).
            if primary_found and subdivision and col_sub in country_df.columns:
                feed_subdiv = str(row.get(col_sub, "")).strip().lower()
                if feed_subdiv and subdivision.lower()[:6] not in feed_subdiv:
                    print(f"[Transit] Country fallback skipping {fid} — "
                          f"subdivision '{feed_subdiv}' doesn't match '{subdivision}'")
                    self._feeds.pop(fid, None)
                    continue
            print(f"[Transit] Country fallback accepted feed {fid} "
                  f"(nearest stop {closest_d:.0f}m)")
            feed_ids.append(fid)
            if closest_d <= 5_000:
                primary_found = True
                break  # found a nearby primary — stop immediately

        return feed_ids

    def _kdtree_fallback(self, df, lat: float, lon: float) -> list[str]:
        """Use KDTree to find the nearest feed centroid, with proximity check."""
        try:
            from scipy.spatial import KDTree as _KDTree
            coords = df[["center_lat", "center_lon"]].values
            tree = _KDTree(coords)
            # Query several candidates in case the nearest centroid is a bad feed
            k = min(10, len(df))
            _, idxs = tree.query([lat, lon], k=k)
            if k == 1:
                idxs = [idxs]
            for idx in idxs:
                row = df.iloc[idx]
                fid = str(row["mdb_source_id"])
                url = str(row.get("urls.direct_download", ""))
                if not fid or not url or url == "nan":
                    continue
                _fid, _data = self._gtfs_ensure(fid, url)
                if not _data:
                    continue
                closest_d = self._nearest_stop_distance(lat, lon, _data)
                if closest_d > 100_000:
                    print(f"[Transit] KDTree skipping feed {fid} — "
                          f"nearest stop {closest_d/1000:.0f}km away")
                    self._feeds.pop(fid, None)
                    continue
                return [fid]
        except Exception as exc:
            print(f"[Transit] KDTree fallback failed: {exc}")
        return []

    # ------------------------------------------------------------------
    # Feed download + parse
    # ------------------------------------------------------------------

    def _verified_index_path(self) -> str:
        return os.path.join(self._cache_dir(), "gtfs_verified.json")

    def _load_verified_index(self) -> dict:
        """Load {region_key: {"feed_id": ..., "url": ...}} from disk."""
        p = self._verified_index_path()
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_verified_index(self, index: dict) -> None:
        p = self._verified_index_path()
        try:
            with open(p, "w", encoding="utf-8") as f:
                json.dump(index, f, indent=2)
        except Exception as exc:
            print(f"[Transit] Could not save verified index: {exc}")

    # ------------------------------------------------------------------
    # GTFS overrides — supplementary feeds not in MobilityData catalog
    # ------------------------------------------------------------------

    def _overrides_path(self) -> str:
        return os.path.join(self._cache_dir(), "gtfs_overrides.json")

    def _load_overrides(self) -> dict:
        """Load gtfs_overrides.json, optionally refreshing from server.

        Resolution order:
          1. In-memory cache (self._overrides) — instant after first call
          2. Server URL (if OVERRIDES_SERVER_URL set and cached copy is stale)
          3. Local file shipped with app (OVERRIDES_LOCAL_FILE, script dir)
          4. Cached copy in gtfs_cache/ from previous server download
        Returns an empty dict on failure so callers can proceed gracefully.
        """
        if self._overrides is not None:
            return self._overrides

        cache_path = self._overrides_path()

        # Try to refresh from server if URL configured and cached copy is stale
        if OVERRIDES_SERVER_URL:
            stale = True
            if os.path.exists(cache_path):
                age_days = (time.time() - os.path.getmtime(cache_path)) / 86400
                stale = age_days > OVERRIDES_STALE_DAYS
            if stale:
                try:
                    import ssl
                    ctx = ssl.create_default_context()
                    req = urllib.request.Request(
                        OVERRIDES_SERVER_URL,
                        headers={"User-Agent": "MapInABox/1.0"},
                    )
                    with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
                        data = r.read()
                    with open(cache_path, "wb") as f:
                        f.write(data)
                except Exception:
                    pass

        # Load from cache dir (server download) or fall back to local shipped file
        for path in [cache_path,
                     os.path.join(self._resource_dir, OVERRIDES_LOCAL_FILE)]:
            if os.path.exists(path):
                try:
                    with open(path, encoding="utf-8") as f:
                        data = json.load(f)
                    self._overrides = data
                    return data
                except Exception:
                    pass

        self._overrides = {}
        return {}

    def _overrides_for_region(
        self, country_code: str, subdivision: str
    ) -> list[tuple[str, str]]:
        """Return [(feed_id, url), …] from the overrides file for this region.

        Matches the most-specific key first (e.g. AU_Queensland before AU),
        then the country-only key.  Skips entries with status 'dead' or
        requires_auth=True (we can't supply auth headers yet).
        """
        overrides = self._load_overrides()
        if not overrides:
            return []

        # Build candidate keys from most- to least-specific
        cc = country_code.upper()
        keys = []
        if subdivision:
            # Normalise subdivision to match JSON keys (spaces → underscores)
            sub_norm = subdivision.replace(" ", "_")
            keys.append(f"{cc}_{sub_norm}")
            # Also try just the first word (e.g. "New" from "New South Wales")
            first_word = subdivision.split()[0] if subdivision else ""
            if first_word and first_word != sub_norm:
                keys.append(f"{cc}_{first_word}")
        keys.append(cc)

        results: list[tuple[str, str]] = []
        seen_ids: set[str] = set()
        for key in keys:
            entries = overrides.get(key)
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                if entry.get("status") == "dead":
                    continue
                if entry.get("requires_auth"):
                    continue
                fid = entry.get("feed_id", "").strip()
                url = entry.get("url", "").strip()
                if fid and url and fid not in seen_ids:
                    seen_ids.add(fid)
                    results.append((fid, url))

        if results:
            print(f"[Transit] Overrides: {len(results)} supplementary feed(s) "
                  f"for {cc}/{subdivision or 'country'}: "
                  + ", ".join(f[0] for f in results))
        return results

    def _cache_dir(self) -> str:
        d = os.path.join(self._base, "gtfs_cache")
        os.makedirs(d, exist_ok=True)
        return d

    def _gtfs_is_stale(self, feed_id: str) -> bool:
        meta = os.path.join(self._cache_dir(), f"{feed_id}.meta.json")
        zp   = os.path.join(self._cache_dir(), f"{feed_id}.zip")
        if not os.path.exists(zp) or not os.path.exists(meta):
            return True
        try:
            with open(meta, encoding="utf-8") as f:
                m = json.load(f)
            return (time.time() - m["downloaded_at"]) / 86400 > GTFS_STALE_DAYS
        except Exception:
            return True

    def _gtfs_ensure(
        self, feed_id: str, download_url: str
    ) -> tuple[Optional[str], Optional[dict]]:
        """Return ``(feed_id, data)`` for *feed_id*, downloading at most once.

        Parsed data is pickled alongside the zip so re-parsing only happens
        when the zip is stale or the pickle is missing/corrupt.
        """
        if feed_id in self._feeds:
            return feed_id, self._feeds[feed_id]

        zp      = os.path.join(self._cache_dir(), f"{feed_id}.zip")
        pickle_p = os.path.join(self._cache_dir(), f"{feed_id}.parsed.pkl")
        stale   = self._gtfs_is_stale(feed_id)

        # ── Try loading from pickle cache first ───────────────────────
        # Always try pickle if it exists — even if the zip is stale/deleted.
        # The zip is intentionally removed after pickling to save disk, so
        # staleness (which checks zip existence) should not block pickle use.
        # If the pickle was saved by an older parser version, discard it and
        # re-download so new fields (e.g. agency) are available.
        if os.path.exists(pickle_p):
            try:
                with open(pickle_p, "rb") as f:
                    data = pickle.load(f)
                if data.get("_parser_version") != GTFS_PARSER_VERSION:
                    print(f"[Transit] Pickle for {feed_id} is parser version "
                          f"{data.get('_parser_version')} (current={GTFS_PARSER_VERSION})"
                          f" — discarding and re-downloading")
                    os.remove(pickle_p)
                    # fall through to download
                else:
                    print(f"[Transit] Loaded parsed cache for {feed_id}")
                    self._feeds[feed_id] = data
                    return feed_id, data
            except Exception as e:
                print(f"[Transit] Pickle load failed for {feed_id}: {e} — will re-parse")

        # ── Load or download the zip ──────────────────────────────────
        if not stale and os.path.exists(zp):
            print(f"[Transit] Using cached GTFS zip for {feed_id}")
            with open(zp, "rb") as f:
                zip_bytes = f.read()
        else:
            print(f"[Transit] Downloading GTFS for {feed_id} …")
            try:
                req = urllib.request.Request(
                    download_url,
                    headers={"User-Agent": "MapInABox/1.0"},
                )
                with urllib.request.urlopen(req, timeout=90) as r:
                    zip_bytes = r.read()
            except Exception as exc:
                print(f"[Transit] Download failed for {feed_id}: {exc}")
                return feed_id, None

            meta_p = os.path.join(self._cache_dir(), f"{feed_id}.meta.json")
            with open(zp, "wb") as f:
                f.write(zip_bytes)
            with open(meta_p, "w", encoding="utf-8") as f:
                json.dump({"downloaded_at": time.time(), "feed_id": feed_id}, f)
            # Invalidate any existing pickle when zip is refreshed
            if os.path.exists(pickle_p):
                os.remove(pickle_p)

        # ── Parse and save pickle ─────────────────────────────────────
        data = self._parse_zip(zip_bytes, feed_id)
        data["_parser_version"] = GTFS_PARSER_VERSION
        self._feeds[feed_id] = data
        try:
            with open(pickle_p, "wb") as f:
                pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)
            print(f"[Transit] Saved parsed cache for {feed_id}")
            try:
                os.remove(zp)
            except Exception:
                pass
        except Exception as e:
            print(f"[Transit] Pickle save failed for {feed_id}: {e}")
        return feed_id, data

    def _parse_zip(self, zip_bytes: bytes, feed_id: str) -> dict:
        """Parse a GTFS ZIP in one pass and return a structured dict."""

        def read_csv(zf: zipfile.ZipFile, fname: str) -> list[dict]:
            names = {n.lower().split("/")[-1]: n for n in zf.namelist()}
            if fname not in names:
                return []
            with zf.open(names[fname]) as f:
                return list(csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")))

        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))

        # ── stops ──────────────────────────────────────────────────────
        stops: dict[str, dict] = {}
        for row in read_csv(zf, "stops.txt"):
            sid = row.get("stop_id", "").strip()
            try:
                slat = float(row.get("stop_lat", 0))
                slon = float(row.get("stop_lon", 0))
            except ValueError:
                continue
            if sid:
                stops[sid] = {
                    "name":     row.get("stop_name", "").strip(),
                    "lat":      slat,
                    "lon":      slon,
                    "platform": (row.get("platform_code", "").strip()
                                 or row.get("stop_code", "").strip()),
                }

        # ── agencies ───────────────────────────────────────────────────
        agencies: dict[str, str] = {}  # agency_id → agency_name
        for row in read_csv(zf, "agency.txt"):
            aid  = row.get("agency_id",   "").strip()
            name = row.get("agency_name", "").strip()
            if name:
                # Some feeds omit agency_id when there is only one agency —
                # store under both the real id and "" so route lookup works.
                agencies[aid]  = name
                agencies[""]   = name   # fallback for routes with no agency_id

        # ── routes ─────────────────────────────────────────────────────
        routes: dict[str, dict] = {}
        for row in read_csv(zf, "routes.txt"):
            rid = row.get("route_id", "").strip()
            if rid:
                aid = row.get("agency_id", "").strip()
                routes[rid] = {
                    "route_id":  rid,
                    "short":     row.get("route_short_name", "").strip(),
                    "long":      row.get("route_long_name",  "").strip(),
                    "type":      ROUTE_TYPE_LABELS.get(
                                     row.get("route_type", "3").strip(), "bus"),
                    "agency_id": aid,
                    "agency":    agencies.get(aid, agencies.get("", "")),
                }

        # ── trips ──────────────────────────────────────────────────────
        trip_info: dict[str, tuple[str, str]] = {}  # tid → (rid, headsign)
        for row in read_csv(zf, "trips.txt"):
            tid = row.get("trip_id", "").strip()
            rid = row.get("route_id", "").strip()
            hs  = row.get("trip_headsign", "").strip()
            if tid:
                trip_info[tid] = (rid, hs)

        # ── stop_times ─────────────────────────────────────────────────
        stop_routes:     dict[str, set]   = {}
        # Accumulate ALL departures first — cap applied after sort+dedup
        stop_departures: dict[str, list]  = {}
        # One representative trip per (route_id, headsign): stores ordered stops
        # keyed as (rid, headsign) → {trip_id, stops: [(seq, sid), ...]}
        rep_trip:   dict[tuple, dict]  = {}   # (rid, hs) → {"tid": ..., "seqs": [(seq,sid)]}

        # First pass: find minimum stop_sequence per trip (= the origin stop)
        trip_min_seq:  dict[str, int] = {}   # tid → min seq int
        trip_orig_sid: dict[str, str] = {}   # tid → stop_id at min seq
        for row in read_csv(zf, "stop_times.txt"):
            tid = row.get("trip_id", "").strip()
            sid = row.get("stop_id", "").strip()
            seq = row.get("stop_sequence", "0").strip()
            if not (tid and sid):
                continue
            try:
                s_int = int(seq)
            except ValueError:
                s_int = 0
            if tid not in trip_min_seq or s_int < trip_min_seq[tid]:
                trip_min_seq[tid]  = s_int
                trip_orig_sid[tid] = sid

        # Second pass: build stop_routes, rep_trip, stop_departures
        # Record departures at every stop so the departure board can answer
        # "what leaves from this stop?" instead of only the trip origin.
        for row in read_csv(zf, "stop_times.txt"):
            tid = row.get("trip_id", "").strip()
            sid = row.get("stop_id", "").strip()
            dep = (row.get("departure_time", "") or row.get("arrival_time", "")).strip()
            seq = row.get("stop_sequence", "0").strip()
            if not (tid and sid):
                continue
            rid, hs = trip_info.get(tid, ("", ""))
            if not rid:
                continue
            stop_routes.setdefault(sid, set()).add(rid)
            # Representative trip: first trip seen per (rid, headsign)
            rkey = (rid, hs)
            if rkey not in rep_trip:
                rep_trip[rkey] = {"tid": tid, "seqs": []}
            if rep_trip[rkey]["tid"] == tid:
                try:
                    s_int = int(seq)
                except ValueError:
                    s_int = 0
                rep_trip[rkey]["seqs"].append((s_int, sid))
            # Record the stop's own departure time (or arrival fallback if
            # departure_time is blank in this feed).
            if dep:
                secs = _t2s(dep)
                if secs >= 0:
                    bucket = stop_departures.setdefault(sid, [])
                    if len(bucket) < 5000:
                        bucket.append((secs, tid, rid, hs))

        # Sort first, then deduplicate (same route + same minute = same service day),
        # then cap at 300 per stop.
        for sid in stop_departures:
            stop_departures[sid].sort()
            seen_rt: set = set()
            deduped: list = []
            for entry in stop_departures[sid]:
                secs, _tid, rid, _hs = entry
                key2 = (rid, secs // 60)
                if key2 not in seen_rt:
                    seen_rt.add(key2)
                    deduped.append(entry)
            stop_departures[sid] = deduped[:300]

        # ── ordered stop sequences per (route_id, headsign) ────────────
        # Key: (route_id, headsign)  Value: ordered list of stop dicts
        route_stops: dict[tuple, list] = {}
        for (rid, hs), rep in rep_trip.items():
            rep["seqs"].sort(key=lambda x: x[0])
            stop_list = []
            for _seq, sid in rep["seqs"]:
                s = stops.get(sid)
                if s:
                    stop_list.append({
                        "stop_id":  sid,
                        "name":     s["name"],
                        "platform": s["platform"],
                        "lat":      s["lat"],
                        "lon":      s["lon"],
                    })
            if stop_list:
                route_stops[(rid, hs)] = stop_list

        n_unique_routes = len({(r["short"] or r["long"]).strip() or rid for rid, r in routes.items() if r["short"] or r["long"]})
        unique_agencies = sorted({r.get("agency", "") for r in routes.values() if r.get("agency")})
        print(f"[Transit] Parsed {feed_id}: {len(stops)} stops, "
              f"{len(routes)} route variants ({n_unique_routes} unique), "
              f"{len(stop_departures)} stops with departures, "
              f"{len(unique_agencies)} agencies.")

        return {
            "feed_id":         feed_id,
            "stops":           stops,
            "routes":          routes,
            "agencies":        agencies,
            "stop_routes":     stop_routes,
            "route_stops":     route_stops,
            "stop_departures": stop_departures,
            "trip_headsign":   {tid: v[1] for tid, v in trip_info.items()},
        }

    # ------------------------------------------------------------------
    # MobilityData catalog
    # ------------------------------------------------------------------

    def _catalog_csv_path(self) -> str:
        return os.path.join(self._cache_dir(), "mobility_catalog.csv")

    def _catalog_is_stale(self) -> bool:
        p = self._catalog_csv_path()
        if not os.path.exists(p):
            return True
        return (time.time() - os.path.getmtime(p)) / 86400 > CATALOG_STALE_DAYS

    def _ensure_catalog(self):
        """Download the MobilityData catalog CSV if missing or stale.

        Returns a pandas DataFrame or None on failure.
        """
        import ssl
        try:
            import pandas as pd
        except ImportError:
            print("[Transit] pandas not available — catalog lookup disabled.")
            return None

        with self._catalog_lock:
            p = self._catalog_csv_path()
            if self._catalog_is_stale():
                print("[Transit] Downloading MobilityData catalog CSV…")
                try:
                    ctx = ssl.create_default_context()
                    req = urllib.request.Request(
                        CATALOG_CSV_URL,
                        headers={"User-Agent": "MapInABox/1.0"},
                    )
                    with urllib.request.urlopen(req, timeout=60, context=ctx) as r:
                        data = r.read()
                    with open(p, "wb") as f:
                        f.write(data)
                    print(f"[Transit] Catalog downloaded: {len(data) // 1024} KB")
                except Exception as exc:
                    print(f"[Transit] Catalog download failed: {exc}")
                    if not os.path.exists(p):
                        return None

        try:
            _want = {
                "mdb_source_id",
                "data_type",
                "status",
                "location.country_code",
                "location.subdivision_name",
                "provider",
                "location.bounding_box.minimum_latitude",
                "location.bounding_box.maximum_latitude",
                "location.bounding_box.minimum_longitude",
                "location.bounding_box.maximum_longitude",
                "urls.direct_download",
            }
            df = pd.read_csv(
                p,
                usecols=lambda c: c in _want,
                low_memory=False,
            )
            # Full usable GTFS rows (may lack bbox — used for country fallback).
            # Treat blank/missing status as active — only exclude "deprecated".
            status_col = df.get("status", pd.Series(dtype=str)).fillna("").astype(str)
            df_full = df[
                (df.get("data_type", pd.Series(dtype=str)) == "gtfs")
                & (status_col != "deprecated")
                & df["urls.direct_download"].notna()
            ].copy()
            self._catalog_df_full = df_full

            # Bbox-filtered subset used for the primary spatial search.
            # Exclude feeds with planetary bboxes (catalog data errors like feed 784
            # whose bbox spans the entire globe and matches every location on Earth).
            df_has_bbox = df_full[df_full["location.bounding_box.minimum_latitude"].notna()].copy()
            lat_span = (df_has_bbox["location.bounding_box.maximum_latitude"]
                        - df_has_bbox["location.bounding_box.minimum_latitude"])
            lon_span = (df_has_bbox["location.bounding_box.maximum_longitude"]
                        - df_has_bbox["location.bounding_box.minimum_longitude"])
            df_bbox = df_has_bbox[(lat_span <= 90) & (lon_span <= 180)].copy()
            df_bbox["center_lat"] = (
                df_bbox["location.bounding_box.minimum_latitude"]
                + df_bbox["location.bounding_box.maximum_latitude"]
            ) / 2
            df_bbox["center_lon"] = (
                df_bbox["location.bounding_box.minimum_longitude"]
                + df_bbox["location.bounding_box.maximum_longitude"]
            ) / 2
            return df_bbox
        except Exception as exc:
            print(f"[Transit] Catalog parse failed: {exc}")
            return None

    def validate_catalog_columns(self) -> tuple[bool, set]:
        """Check the catalog has all required columns.

        Returns ``(ok, missing_set)``.
        """
        df = self._catalog_df if self._catalog_df is not None else self._ensure_catalog()
        required = {
            "mdb_source_id",
            "urls.direct_download",
            "location.bounding_box.minimum_latitude",
            "location.bounding_box.maximum_latitude",
            "location.bounding_box.minimum_longitude",
            "location.bounding_box.maximum_longitude",
        }
        if df is None:
            return False, required
        missing = required - set(df.columns)
        return len(missing) == 0, missing
