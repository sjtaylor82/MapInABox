"""accessible_route.py - shared helpers for narrative route briefings."""

from __future__ import annotations

import math
import threading

import wx


def _clean_poi_name(poi: dict) -> str:
    name = (poi.get("label") or poi.get("name") or "").strip()
    if not name:
        return ""
    return name.split(",")[0].strip()


def _metres_between(lat1, lon1, lat2, lon2) -> float:
    return math.sqrt(
        ((lat1 - lat2) * 111000) ** 2
        + ((lon1 - lon2) * 111000 * math.cos(math.radians(lat1))) ** 2
    )


def _fetch_route_pois(host, sample_coords: list) -> list:
    """One route-wide POI fetch, used when the live POI grid is empty.

    Sampling from a single fetch keeps the briefing to one network call rather
    than one per turn node.
    """
    fetcher = getattr(host, "_poi_fetcher", None)
    if not fetcher or not sample_coords:
        return []
    lats = [c[0] for c in sample_coords]
    lons = [c[1] for c in sample_coords]
    mid_lat = (min(lats) + max(lats)) / 2.0
    mid_lon = (min(lons) + max(lons)) / 2.0
    span = max(
        (_metres_between(mid_lat, mid_lon, la, lo) for la, lo in sample_coords),
        default=0.0,
    )
    radius = int(min(3000, max(400, span + 200)))
    try:
        pois, _ = fetcher.fetch_pois(
            mid_lat, mid_lon, "all", radius,
            address_points=getattr(host, "_address_points", None),
        )
        return pois or []
    except Exception as exc:
        miab_log("errors", f"[Nav] Route POI fetch failed: {exc}", getattr(host, "settings", None))
        return []


def _route_highlights(host, digest: dict) -> list[dict]:
    """Sample nearby POIs at turn nodes along the loaded route for narration."""
    nav = getattr(host, "_nav", None)
    graph = getattr(nav, "_graph", None) or {}
    path = list(getattr(nav, "route", []) or [])
    nodes = graph.get("nodes", {})
    if not path or not nodes:
        return []

    # Prefer turn waypoints so landmarks land near actionable moments.
    instructions = list(getattr(nav, "instructions", []) or [])
    samples: set[int] = {0, len(path) - 1}
    for inst in instructions:
        if isinstance(inst, (list, tuple)) and inst:
            idx = inst[0]
            if isinstance(idx, int) and 0 <= idx < len(path):
                samples.add(idx)
    # Fall back to quartile sampling when there are no instructions yet.
    if len(samples) <= 2 and len(path) > 3:
        samples.add((len(path) - 1) // 4)
        samples.add((len(path) - 1) // 2)
        samples.add(((len(path) - 1) * 3) // 4)

    ordered = [i for i in sorted(samples) if nodes.get(path[i])]
    sample_coords = [nodes[path[i]] for i in ordered]

    # Use the loaded grid when present; otherwise do a single route-wide fetch
    # so the briefing always has landmarks even before street mode pre-loads them.
    use_grid = bool(getattr(host, "_poi_grid", None)) and hasattr(host, "_poi_grid_nearby")
    fetched: list = [] if use_grid else _fetch_route_pois(host, sample_coords)

    def _nearby(lat, lon):
        if use_grid:
            return host._poi_grid_nearby(lat, lon, 100)
        return [
            poi for poi in fetched
            if poi.get("lat") is not None and poi.get("lon") is not None
            and _metres_between(lat, lon, poi["lat"], poi["lon"]) <= 100
        ]

    highlights: list[dict] = []
    seen: set[str] = set()
    for idx in ordered:
        lat, lon = nodes[path[idx]]
        for poi in _nearby(lat, lon):
            name = _clean_poi_name(poi)
            if not name:
                continue
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            plat = poi.get("lat")
            plon = poi.get("lon")
            dist_m = None
            if plat is not None and plon is not None:
                dist_m = int(round(_metres_between(lat, lon, plat, plon)))
            highlights.append({
                "name": name,
                "kind": (poi.get("kind") or "").strip(),
                "route_index": idx,
                "distance_m": dist_m,
            })
            if len(highlights) >= 10:
                return highlights
    return highlights


def start_narrative_briefing(host) -> None:
    """Start the narrative briefing flow for a navigation host."""
    if not getattr(host, "_nav_active", False):
        host._nav_update_ui("No navigation active. Press Ctrl+G to navigate to an address.")
        return
    if getattr(host, "_nav_google_mode", False):
        host._announce_transient(
            "Narrative briefing is only available for OpenStreetMap routes. "
            "Switch navigation provider to OSM in settings.")
        return
    mistral = getattr(host, "_mistral", None)
    if not mistral or not getattr(mistral, "is_configured", False):
        host._announce_transient(
            "Narrative briefing needs a Mistral API key. Add one in settings.")
        return

    # If briefing mode is already active, treat the action as "repeat current".
    if getattr(host, "_nav_briefing_mode", False) and getattr(host, "_nav_briefing_steps", []):
        host._nav_briefing_announce_current()
        return

    host._announce_transient("Thinking...")
    try:
        host._play_system_sound("balloon")
    except Exception:
        pass
    if hasattr(host, "_set_nav_button_busy"):
        wx.CallAfter(host._set_nav_button_busy, True)

    def _work():
        try:
            origin_label = ""
            origin_lat = None
            origin_lon = None
            street = getattr(host, "street_label", "") or ""
            num = host._nearest_address_number(host.lat, host.lon, street, radius=60)
            if num and street:
                origin_label = f"{num} {street}"
                # Find the actual address point so the digest can compute which
                # side of the road the property sits on.
                def _bare(s: str) -> str:
                    s = (s or "").lower().split(",")[0].strip()
                    parts = s.split()
                    suffixes = {
                        "street", "st", "road", "rd", "avenue", "ave",
                        "drive", "dr", "court", "ct", "place", "pl",
                        "crescent", "cres", "close", "cl", "boulevard",
                        "blvd", "highway", "hwy", "terrace", "tce",
                        "parade", "pde", "esplanade", "esp", "lane", "ln",
                        "grove", "gr", "way", "circuit", "cct", "rise",
                        "row", "mews", "track",
                    }
                    if parts and parts[-1] in suffixes:
                        parts = parts[:-1]
                    return " ".join(parts)

                target_bare = _bare(street)
                for ap in getattr(host, "_address_points", []):
                    if ap.get("number") == num and _bare(ap.get("street", "")) == target_bare:
                        origin_lat = ap.get("lat")
                        origin_lon = ap.get("lon")
                        break

            country_code = getattr(host, "_current_country_code", "") or ""
            digest = host._nav.build_route_digest(
                origin_label=origin_label,
                origin_lat=origin_lat,
                origin_lon=origin_lon,
                country_code=country_code,
            )
            if not digest:
                wx.CallAfter(
                    host._announce_transient,
                    "Could not build a route digest. Falling back to step list.")
                wx.CallAfter(host._nav_announce_step)
                return
            try:
                from logging_utils import miab_log
                _d = digest.get("destination", {})
                miab_log(
                    "navigation",
                    (f"Briefing digest: legs={len(digest.get('legs', []))} "
                     f"dest_side={_d.get('side')} "
                     f"crossing_needed={_d.get('crossing_needed')}"),
                    getattr(host, "settings", None),
                )
            except Exception:
                pass

            highlights = _route_highlights(host, digest)
            if highlights:
                digest["route_highlights"] = highlights

            # Enrich with live pedestrian features: crossings, steps, tactile paving.
            try:
                from tools import _osm_walk_features
                _fetcher = getattr(host, "_poi_fetcher", None)
                _overpass = getattr(_fetcher, "_overpass", None)
                _road_segs = getattr(host, "_road_segments", None)
                _nav_route = list(getattr(host._nav, "route", []) or [])
                _nav_nodes = (getattr(host._nav, "_graph", None) or {}).get("nodes", {})
                if _overpass and _nav_route and _nav_nodes:
                    walk_pts = [
                        {"lat": _nav_nodes[nid][0], "lon": _nav_nodes[nid][1], "instruction": ""}
                        for nid in _nav_route
                        if nid in _nav_nodes
                    ]
                    if walk_pts:
                        features = _osm_walk_features(walk_pts, _overpass, road_segments=_road_segs)
                        if features:
                            digest["pedestrian_features"] = [f["sv_desc"] for f in features]
            except Exception as _exc:
                miab_log("errors", f"[Nav] Pedestrian feature lookup failed: {_exc}", getattr(host, "settings", None))

            text = mistral.narrative_directions(digest)
            if not text:
                wx.CallAfter(
                    host._announce_transient,
                    "Briefing was rejected or unavailable. Falling back to step list.")
                wx.CallAfter(host._nav_announce_step)
                return
            steps = host._parse_briefing_steps(text)
            if not steps:
                wx.CallAfter(
                    host._announce_transient,
                    "Briefing returned no steps. Falling back to step list.")
                wx.CallAfter(host._nav_announce_step)
                return
            wx.CallAfter(host._nav_briefing_enter, steps)
        except Exception as exc:
            miab_log("errors", f"[Nav] Narrative briefing failed: {exc}", getattr(host, "settings", None))
            wx.CallAfter(
                host._announce_transient,
                "Briefing failed. Falling back to step list.")
            wx.CallAfter(host._nav_announce_step)
        finally:
            if hasattr(host, "_set_nav_button_busy"):
                wx.CallAfter(host._set_nav_button_busy, False)

    threading.Thread(target=_work, daemon=True).start()
