"""free.py — free-flow spatial exploration mode for Map in a Box.

Purpose
-------
This module provides a lighter alternative to the current intersection-led
walking mode.  Instead of hopping from junction to junction and forcing the
user to browse turn options, it lets the caller move forward or backward in
small distance steps along a street and hear only nearby points of interest.

Design goals
------------
- Up/Down style movement in fixed metre steps along one street
- Minimal intersection chatter
- Relative POI descriptions such as ahead-left / right / nearby
- Pure logic only: no wx, no pygame, no network calls
- Reuses the existing Map in a Box segment and POI structures

Expected input structures
-------------------------
segments:
    list[{
        "name": str,
        "kind": str,
        "coords": list[(lat, lon), ...],
    }]

pois:
    list[{
        "name": str,
        "kind": str,
        "lat": float,
        "lon": float,
        ...
    }]

Typical integration
-------------------
    engine = FreeExploreEngine(step_m=20, poi_radius_m=70)
    engine.set_segments(self._road_segments)
    engine.set_pois(self._all_pois)
    msg = engine.start(self.lat, self.lon)
    ui(msg)

    msg = engine.step_forward()
    self.lat, self.lon = engine.position
    ui(msg)

This module does not change core.py by itself.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
import threading
from typing import Iterable, Optional
import wx

from geo import dist_metres, nearest_point_on_segment, bearing_deg, compass_name
from logging_utils import miab_log


@dataclass(frozen=True)
class PathPoint:
    lat: float
    lon: float


_FREE_KIND_PRIORITY = {
    "restaurant": 0, "cafe": 0, "bakery": 0, "fast food": 0, "bar": 0,
    "pub": 0, "supermarket": 0, "convenience": 0, "butcher": 0,
    "greengrocer": 0, "bottle shop": 0,
    "station": 1, "bus stop": 1, "bus station": 1, "tram stop": 1,
    "ferry terminal": 1, "transport": 1,
}

# Types excluded from free mode POI announcements.  Previously this list
# included "generic", which suppressed any POI whose category was not
# mapped to a known type.  That inadvertently hid useful shops such as
# takeout food and bakery venues which HERE labels as generic.  Remove
# "generic" from this set to allow unmapped categories to surface.
_FREE_KIND_EXCLUDE = frozenset({
    # Excluded categories.  'hairdresser' was removed from this list so
    # that barbershops and salons can be announced when they appear in
    # the allowed_kinds set.
    "services",
})


@dataclass
class FreeState:
    street_name: str = ""
    path_id: int = 0
    path_index: int = 0
    lat: float = 0.0
    lon: float = 0.0
    heading_deg: float = 0.0


class FreeExploreEngine:
    """Free-flow POI exploration along a single street.

    The engine picks the nearest suitable street path, snaps the current
    position to it, then advances in fixed metre increments along the polyline.
    Nearby POIs are classified relative to the direction of travel.
    """

    def __init__(
        self,
        # Use a slightly larger step than the previous default to reduce
        # address number duplication without causing large jumps.  A 15 metre
        # stride still respects fine-grained navigation while skipping fewer
        # duplicate house numbers.
        step_m: float = 15.0,
        # POI detection radius.  45 metres balances capturing nearby
        # establishments like Muzzas Pies (≈39 m away) with excluding
        # most cross‑street venues.  Combined with the angular filter
        # defined below, this avoids spurious detections at corners.
        poi_radius_m: float = 60.0,
        # Optional list of POI kinds to include.  Only POIs whose kind is in
        # this set will be considered.  If None, a sensible default set
        # including food, transport, education and healthcare categories is used.
        allowed_kinds: Optional[Iterable[str]] = None,
        cone_forward_m: float = 90.0,
        intersection_radius_m: float = 18.0,
    ) -> None:
        self.step_m = float(step_m)
        self.poi_radius_m = float(poi_radius_m)
        self.cone_forward_m = float(cone_forward_m)
        self.intersection_radius_m = float(intersection_radius_m)

        self._segments: list[dict] = []
        self._pois: list[dict] = []
        self._paths: list[dict] = []
        self.state = FreeState()
        self._seen_poi_keys: set[str] = set()
        self._seen_poi_pos: set[str] = set()
        self._seen_side_pos: set[str] = set()  # cleared each step
        self._seen_crossings: set[str] = set()
        self._travel_dir: int = 1

        # Enable verbose debug logging.  When True, the engine will emit
        # diagnostic information on each step and POI classification.  This
        # is intended for troubleshooting inconsistent stepping or missing
        # announcements.  Set to False to disable logging.
        self.debug: bool = False
        self.log_settings = None

        # Global step counter used to determine how long ago a POI was last
        # encountered.  This helps suppress repeated announcements when the
        # same POI falls within range on adjacent segments.  Incremented
        # on every call to :meth:`_step`.
        self._global_step_count: int = 0
        # Mapping of POI key to the most recent step count when it was seen.
        self._poi_last_seen: dict[str, int] = {}
        # Number of steps that must elapse before re-announcing the same POI.
        # For example, a value of 2 means a POI won't be considered 'new'
        # again until at least two steps have been taken since it was last
        # within range.
        self.reannounce_steps: int = 2

        # Track the last step index when each POI was spoken via an arrow press.
        self._poi_last_uttered: dict[str, int] = {}
        # The global step count at which the last left/right arrow call occurred.
        # Used to determine if the current side query is a repeat within the
        # same step (allowing duplicates) or on a new step (suppressing
        # recently uttered POIs).
        self._last_side_call_step: int = -1

        # Maximum allowable angular deviation, in degrees, for a POI to be
        # considered “on the current street”.  POIs located at roughly
        # right-angles (e.g. on cross streets) are filtered out.  Lowering
        # this value restricts POIs to be more closely aligned with the
        # current road direction.  A threshold of 60° excludes most
        # perpendicular venues while keeping those slightly off-axis.
        self.poi_angle_threshold_deg: float = 60.0

        # Maximum angular difference from the heading to include a POI.  Any
        # place located more than this many degrees behind the traveller is
        # ignored.  This prevents the engine from announcing POIs that are
        # directly behind you while still allowing side and forward venues.
        # A value of 150° excludes only those far behind (30° from straight
        # back), retaining cross‑street POIs at 90°.
        self.poi_back_threshold_deg: float = 150.0

        # Allowed POI categories.  A POI whose kind is not in this set will
        # be ignored by the engine.  Defaults to categories most useful for
        # blind travellers: food venues, transport stops, educational
        # institutions and healthcare providers.
        if allowed_kinds is None:
            # Default set of POI categories to announce in free mode.  In
            # addition to food, transport, education and healthcare, this
            # includes a wider range of lodging-related terms to ensure
            # hotels such as the Comfort Hotel Pacific Cleveland (128 Middle
            # Street) are caught even if their source category differs.
            self.allowed_kinds: set[str] = {
                # Food and drink
                'restaurant', 'cafe', 'bakery', 'fast food', 'bar', 'pub',
                'supermarket', 'convenience', 'butcher', 'greengrocer', 'bottle shop',
                # Lodging and accommodation
                'hotel', 'motel', 'hostel', 'inn', 'guest house', 'resort',
                'accommodation', 'lodging', 'bed and breakfast',
                # Transport
                'station', 'bus stop', 'bus station', 'tram stop', 'ferry terminal', 'transport',
                # Education
                'school', 'college', 'university', 'kindergarten', 'childcare',
                # Healthcare
                'doctor', 'hospital',
                # Services and community
                'hairdresser', 'barber', 'barbershop', 'library',
                # Shopping centres (space and underscore variants from OSM/HERE)
                'shopping centre', 'shopping_centre', 'shopping center', 'shopping_center',
                'shopping mall', 'shopping_mall', 'mall',
                # Extra lodging variants from OSM/HERE tagging
                'hotel;motel', 'motel;hotel', 'guest_house', 'bed_and_breakfast',
                # Generic — HERE uses this for many useful unmapped categories
                # (takeaway food, markets, plazas, etc.).  The _FREE_KIND_EXCLUDE
                # set intentionally omits 'generic' so these can surface here.
                'generic',
                # Office, civic and social facility — catches Vision Australia,
                # Centrelink, charities, NFPs and similar organisations
                'office', 'social_facility', 'social facility', 'charity', 'ngo',
                'government', 'civic', 'community_centre', 'community centre',
                'social_centre', 'social centre', 'association',
                # Additional health
                'pharmacy', 'optician', 'medical', 'clinic',
            }
        else:
            self.allowed_kinds = {k.strip().lower() for k in allowed_kinds}

    @property
    def position(self) -> tuple[float, float]:
        return self.state.lat, self.state.lon

    @property
    def street_name(self) -> str:
        return self.state.street_name

    @property
    def step_pois_seen(self) -> set:
        """Read-only view of seen POI keys."""
        return frozenset(self._seen_poi_keys)

    def _verbose(self, msg: str) -> None:
        if self.debug:
            miab_log("verbose", msg, self.log_settings)

    def reset(self) -> None:
        self.state = FreeState()
        self._seen_poi_keys.clear()
        self._seen_poi_pos.clear()
        self._seen_crossings.clear()
        self._travel_dir = 1
        self._last_path_id = -1   # prevents oscillating back to path we just left

    def set_segments(self, segments: Iterable[dict]) -> None:
        # Store raw segments and build initial paths
        self._segments = [s for s in segments if s.get("coords")]
        raw_paths = self._build_paths(self._segments)
        # Densify each path to ensure long OSM segments are subdivided into
        # smaller hops.  Without densification, a single long polyline edge
        # could cause a single step to jump many metres and skip POIs.  We
        # insert evenly spaced intermediate points so that the distance
        # between consecutive nodes does not exceed the configured step size.
        def _densify(pts: list[PathPoint], max_spacing_m: float) -> list[PathPoint]:
            if len(pts) < 2:
                return pts
            densified: list[PathPoint] = [pts[0]]
            for i in range(len(pts) - 1):
                a = pts[i]
                b = pts[i + 1]
                seg_len = dist_metres(a.lat, a.lon, b.lat, b.lon)
                if seg_len <= 0:
                    continue
                n_extra = int(seg_len // max_spacing_m)
                for k in range(1, n_extra + 1):
                    frac = (k * max_spacing_m) / seg_len
                    if frac >= 1.0:
                        break
                    lat = a.lat + frac * (b.lat - a.lat)
                    lon = a.lon + frac * (b.lon - a.lon)
                    densified.append(PathPoint(lat, lon))
                densified.append(b)
            return densified
        self._paths = []
        for p in raw_paths:
            pts = p["points"]
            # Use half the step length as max spacing to ensure at least one
            # intermediate point per step.  Fallback to 5m if step_m is zero.
            max_spacing = max(1.0, self.step_m * 0.5)
            densified_pts = _densify(pts, max_spacing)
            self._paths.append({
                "street_name": p["street_name"],
                "points": densified_pts,
                "length_m": self._path_length(densified_pts),
            })
        self.reset()

    def set_pois(self, pois: Iterable[dict]) -> None:
        clean: list[dict] = []
        for poi in pois:
            lat = poi.get("lat")
            lon = poi.get("lon")
            if lat is None or lon is None:
                continue
            item = dict(poi)
            if not item.get("name"):
                label = str(item.get("label") or "").strip()
                if label:
                    item["name"] = label.split(",")[0].strip()
                elif item.get("kind"):
                    item["name"] = str(item.get("kind")).strip().title()
            clean.append(item)
        self._pois = clean
        self._seen_poi_keys.clear()
        self._seen_poi_pos.clear()
        self._seen_crossings.clear()

    def start(self, lat: float, lon: float, preferred_street: Optional[str] = None,
              heading_deg: Optional[float] = None) -> str:
        if not self._paths:
            return "Free mode unavailable. No street geometry loaded."

        best = self._pick_best_path(lat, lon, preferred_street)
        if best is None:
            return "Could not find a suitable street for free mode."

        path = best["points"]
        idx = best["nearest_index"]
        snapped = best["snapped"]
        # Compute the natural (index-increasing) heading of this path segment.
        fwd_heading = self._heading_for_index(path, idx, 1)

        # If a real-world heading hint was supplied, orient travel direction so
        # that it best matches the hint.  This prevents left/right being
        # swapped when OSM stores a street in the opposite direction to travel.
        travel_dir = 1
        if heading_deg is not None:
            diff_fwd = abs((fwd_heading - heading_deg + 180) % 360 - 180)
            diff_rev = abs(((fwd_heading + 180) % 360 - heading_deg + 180) % 360 - 180)
            if diff_rev < diff_fwd:
                travel_dir = -1
        heading = fwd_heading if travel_dir == 1 else (fwd_heading + 180.0) % 360.0

        self.state = FreeState(
            street_name=best["street_name"],
            path_index=idx,
            path_id=best["path_id"],
            lat=snapped[0],
            lon=snapped[1],
            heading_deg=heading,
        )
        self._seen_poi_keys.clear()
        self._seen_poi_pos.clear()
        self._seen_crossings.clear()
        self._travel_dir = travel_dir
        # Reset direction-change tracking so the first step is always treated
        # as a fresh start regardless of the previous session's direction.
        self._last_step_direction = travel_dir

        intro = f"Free mode on {self.state.street_name}."
        poi_text = self._describe_current_pois(include_seen=False)
        return intro if not poi_text else f"{intro} {poi_text}"

    def step_forward(self) -> str:
        return self._step(self._travel_dir)

    def step_backward(self) -> str:
        return self._step(-self._travel_dir)

    def describe_current(self) -> str:
        if not self.state.street_name:
            return "Free mode is not active."
        poi_text     = self._describe_current_pois(include_seen=True)
        intersection = self._intersection_hint()
        parts = []
        if poi_text:
            parts.append(poi_text)
        if intersection:
            parts.append(intersection)
        return " ".join(parts) if parts else ""

    def describe_nearest_intersection(self) -> str:
        if not self.state.street_name:
            return "Free mode is not active."
        hint = self._intersection_hint(force=True)
        return hint or "No nearby cross street."

    def describe_left(self) -> str:
        return self._describe_side("left")[0]

    def describe_right(self) -> str:
        return self._describe_side("right")[0]

    def describe_left_with_pois(self) -> tuple[str, list]:
        return self._describe_side("left")

    def describe_right_with_pois(self) -> tuple[str, list]:
        return self._describe_side("right")

    def snap_to_nearest_cross(self) -> str:
        """Switch to the nearest cross street and snap position to it."""
        if not self.state.street_name:
            return "Free mode is not active."
        current = self._base_street_name(self.state.street_name)

        # Find the nearest point on any other named path to our current position
        best_d = float("inf")
        best_pid = -1
        best_idx = 0

        for i, p in enumerate(self._paths):
            other = self._base_street_name(p["street_name"])
            if not other or other == current:
                continue
            nearest = self._nearest_point_on_path(
                self.state.lat, self.state.lon, p["points"])
            if nearest is None:
                continue
            if nearest["distance_m"] < best_d:
                best_d = nearest["distance_m"]
                best_pid = i
                best_idx = nearest["index"]

        if best_pid < 0:
            return "No cross street found nearby."

        best_path = self._paths[best_pid]
        # Switch to the cross street
        self.state.street_name = best_path["street_name"]
        self.state.path_id = best_pid
        self.state.path_index = best_idx
        pts = best_path["points"]
        self.state.lat = pts[best_idx].lat
        self.state.lon = pts[best_idx].lon
        # Keep travel direction that best matches current heading
        h_fwd = self._heading_for_index(pts, best_idx, 1)
        diff_fwd = abs((h_fwd - self.state.heading_deg + 180) % 360 - 180)
        diff_rev = abs(((h_fwd + 180) % 360 - self.state.heading_deg + 180) % 360 - 180)
        self._travel_dir = 1 if diff_fwd <= diff_rev else -1
        self.state.heading_deg = h_fwd if self._travel_dir == 1 else (h_fwd + 180.0) % 360.0
        self._seen_poi_keys.clear()
        self._seen_poi_pos.clear()
        self._seen_crossings.clear()
        poi_text = self._describe_current_pois(include_seen=False)
        dist_m = int(best_d)
        msg = (f"Now on {self.state.street_name}"
               + (f", {dist_m}m away" if dist_m > 10 else "")
               + f". Heading {compass_name(self.state.heading_deg)}.")
        return f"{msg} {poi_text}" if poi_text else msg

    def reverse(self) -> str:
        if not self.state.street_name:
            return "Free mode is not active."
        self._travel_dir *= -1
        self.state.heading_deg = (self.state.heading_deg + 180.0) % 360.0
        self._seen_poi_keys.clear()
        self._seen_poi_pos.clear()
        self._seen_crossings.clear()
        return "Turned around."

    def _step(self, direction: int) -> str:
        """Move along the current path by the configured step distance.

        This method updates the internal position by stepping a fixed distance
        along the polyline rather than jumping directly to the next vertex.  It
        first determines which segment the step ends within using
        :meth:`_advance_index` and then computes an interpolated latitude and
        longitude using :meth:`_interpolate_position`.  This avoids long
        jumps across coarse OSM segments which could otherwise skip nearby
        POIs.  If the path ends before the step distance is exhausted, the
        transition logic is invoked.
        """
        if not self.state.street_name:
            return "Free mode is not active."

        path = self._active_path()
        if not path:
            return "Street path lost."

        # Determine which vertex the step will end on.  If the returned index
        # equals the current index, we're at the end of this chunk and need to
        # transition to the next street or report a dead end.
        new_index = self._advance_index(path, self.state.path_index, direction, self.step_m)
        if new_index == self.state.path_index:
            return self._transition(direction)

        # Compute the exact position step_m metres along the path from the
        # current index.  This interpolated position reduces the chance of
        # overshooting POIs when segments are spaced far apart.
        prev_lat = self.state.lat
        prev_lon = self.state.lon
        new_lat, new_lon = self._interpolate_position(path, self.state.path_index, direction, self.step_m)
        self.state.lat = new_lat
        self.state.lon = new_lon

        # Update path index for subsequent steps.
        self.state.path_index = new_index
        self._seen_side_pos.clear()

        # Heading = actual displacement direction from prev to new position.
        # Guard against loop-induced reversals: if the new displacement points
        # more than 120° away from the current heading, the road has looped
        # back on itself (e.g. Old Cleveland Road near French Street).
        # Keep the last good heading in that case rather than flipping.
        moved_m = dist_metres(prev_lat, prev_lon, new_lat, new_lon)
        if moved_m > 0.5:
            new_bearing = bearing_deg(prev_lat, prev_lon, new_lat, new_lon)
            dev = abs((new_bearing - self.state.heading_deg + 180) % 360 - 180)
            if dev <= 120:
                self.state.heading_deg = new_bearing

        # Increment global step counter so that POI announcements can be
        # suppressed across adjacent segments.
        self._global_step_count += 1

        # When the step direction changes from the previous step (e.g. the
        # user reverses and comes back), clear positional and timing memory.
        # This ensures that returning to a previously-visited location
        # re-announces nearby POIs rather than silently suppressing them.
        last_dir = getattr(self, '_last_step_direction', direction)
        if direction != last_dir:
            self._seen_poi_pos.clear()
            self._poi_last_seen.clear()
        self._last_step_direction = direction

        # Announce any newly entered POIs and intersection hints.
        poi_text = self._describe_current_pois(include_seen=False)
        hint     = self._intersection_hint()
        parts = []
        if poi_text:
            parts.append(poi_text)
        if hint:
            parts.append(hint)

        if self.debug:
            # Log step diagnostics including location, path index and any nearby POIs.
            nearby = self._nearby_pois(self.state.lat, self.state.lon, self.poi_radius_m)
            poi_names = [p.get("name") or p.get("kind") for p in nearby]
            self._verbose(
                f"[FreeDebug] step dir={direction} street='{self.state.street_name}' "
                f"idx={self.state.path_index} lat={self.state.lat:.6f} lon={self.state.lon:.6f} "
                f"heading={self.state.heading_deg:.1f} POIs={poi_names}"
            )
        return " ".join(parts) if parts else ""

    def _transition(self, direction: int) -> str:
        """Handle moving past the end of the current path chunk.

        direction is the raw step direction (+1 or -1).  We use current
        heading to find the best-matching continuation — same street first,
        cross-street second.  This ensures backward steps don't accidentally
        flip to a forward chunk and create loops.
        """
        path = self._active_path()
        endpoint = path[self.state.path_index]
        current_street = self._base_street_name(self.state.street_name)

        # ── Same-street continuation ──────────────────────────────────────
        same_best: "tuple | None" = None
        same_best_d = 50.0
        prev_pid = getattr(self, '_last_path_id', -1)

        for pid, p in enumerate(self._paths):
            if pid == self.state.path_id:
                continue
            if pid == prev_pid:
                continue   # don't immediately return to path we just came from
            if self._base_street_name(p["street_name"]) != current_street:
                continue
            pts = p["points"]
            nearest = self._nearest_point_on_path(endpoint.lat, endpoint.lon, pts)
            if nearest is None or nearest["distance_m"] > same_best_d:
                continue
            enter_idx = nearest["index"]
            # Pick the direction on the new chunk that best matches our heading
            h_fwd = self._heading_for_index(pts, enter_idx, 1)
            h_rev = (h_fwd + 180.0) % 360.0
            diff_fwd = abs((h_fwd - self.state.heading_deg + 180) % 360 - 180)
            diff_rev = abs((h_rev - self.state.heading_deg + 180) % 360 - 180)
            enter_dir = 1 if diff_fwd <= diff_rev else -1
            same_best_d = nearest["distance_m"]
            same_best = (pid, p, enter_idx, enter_dir)

        if same_best is not None:
            pid, bp, enter_idx, enter_dir = same_best
            self._last_path_id = self.state.path_id   # remember where we came from
            self._travel_dir = enter_dir
            self.state.street_name = bp["street_name"]
            self.state.path_id = pid
            pts = bp["points"]
            next_idx = self._advance_index(pts, enter_idx, enter_dir, self.step_m)
            self.state.path_index = next_idx
            self.state.lat = pts[next_idx].lat
            self.state.lon = pts[next_idx].lon
            base = self._heading_for_index(pts, next_idx, 1)
            self.state.heading_deg = (base + (180.0 if enter_dir < 0 else 0.0)) % 360.0
            self._seen_poi_keys.clear()
            self._seen_poi_pos.clear()
            self._seen_crossings.clear()
            poi_text = self._describe_current_pois(include_seen=False)
            return poi_text if poi_text else ""

        # ── Cross-street endpoint ─────────────────────────────────────────
        best: "tuple | None" = None
        best_diff = float("inf")
        for pid, p in enumerate(self._paths):
            pts = p["points"]
            if pid == self.state.path_id:
                continue
            if self._base_street_name(p["street_name"]) == current_street:
                continue
            for check_idx in (0, len(pts) - 1):
                d = dist_metres(endpoint.lat, endpoint.lon,
                                pts[check_idx].lat, pts[check_idx].lon)
                if d > 50:
                    continue
                h_fwd = self._heading_for_index(pts, check_idx, 1)
                h_rev = (h_fwd + 180.0) % 360.0
                diff_fwd = abs((h_fwd - self.state.heading_deg + 180) % 360 - 180)
                diff_rev = abs((h_rev - self.state.heading_deg + 180) % 360 - 180)
                enter_dir = 1 if diff_fwd <= diff_rev else -1
                diff = min(diff_fwd, diff_rev)
                if diff < best_diff:
                    best_diff = diff
                    best = (pid, p, check_idx, enter_dir)

        if best is None:
            return f"Dead end on {self.state.street_name}."

        pid, bp, check_idx, enter_dir = best
        self._last_path_id = self.state.path_id   # remember where we came from
        self._travel_dir = enter_dir
        self.state.street_name = bp["street_name"]
        self.state.path_id = pid
        pts = bp["points"]
        next_idx = self._advance_index(pts, check_idx, enter_dir, self.step_m)
        self.state.path_index = next_idx
        self.state.lat = pts[next_idx].lat
        self.state.lon = pts[next_idx].lon
        base = self._heading_for_index(pts, next_idx, 1)
        self.state.heading_deg = (base + (180.0 if enter_dir < 0 else 0.0)) % 360.0
        self._seen_poi_keys.clear()
        self._seen_poi_pos.clear()
        self._seen_crossings.clear()
        poi_text = self._describe_current_pois(include_seen=False)
        parts    = [f"Onto {self.state.street_name}."]
        if poi_text:
            parts.append(poi_text)
        return " ".join(parts)

    def _active_path(self) -> list[PathPoint]:
        pid = self.state.path_id
        if 0 <= pid < len(self._paths):
            pts = self._paths[pid]["points"]
            # Clamp path_index if somehow out of range
            if self.state.path_index >= len(pts):
                self.state.path_index = len(pts) - 1
            return pts
        # Fallback: search by name
        for p in self._paths:
            if p["street_name"] == self.state.street_name:
                if self.state.path_index < len(p["points"]):
                    return p["points"]
        return []

    def _build_paths(self, segments: list[dict]) -> list[dict]:
        grouped: dict[str, list[list[PathPoint]]] = {}
        for seg in segments:
            street_name = self._base_street_name(seg.get("name", ""))
            if not street_name:
                continue
            coords = seg.get("coords") or []
            if len(coords) < 2:
                continue
            pts = [PathPoint(float(lat), float(lon)) for lat, lon in coords]
            grouped.setdefault(street_name, []).append(pts)

        paths: list[dict] = []
        for street_name, chunks in grouped.items():
            merged = self._merge_chunks(chunks)
            for pts in merged:
                if len(pts) < 2:
                    continue
                paths.append({
                    "street_name": street_name,
                    "points": pts,
                    "length_m": self._path_length(pts),
                })
        return paths

    def _merge_chunks(self, chunks: list[list[PathPoint]], join_m: float = 25.0) -> list[list[PathPoint]]:
        remaining = [list(c) for c in chunks if c]
        merged: list[list[PathPoint]] = []

        while remaining:
            current = remaining.pop(0)
            changed = True
            while changed:
                changed = False
                for i, other in enumerate(list(remaining)):
                    d1 = dist_metres(current[-1].lat, current[-1].lon, other[0].lat, other[0].lon)
                    d2 = dist_metres(current[-1].lat, current[-1].lon, other[-1].lat, other[-1].lon)
                    d3 = dist_metres(current[0].lat, current[0].lon, other[-1].lat, other[-1].lon)
                    d4 = dist_metres(current[0].lat, current[0].lon, other[0].lat, other[0].lon)
                    best = min(d1, d2, d3, d4)
                    if best > join_m:
                        continue
                    if best == d1:
                        current.extend(other[1:])
                    elif best == d2:
                        current.extend(list(reversed(other[:-1])))
                    elif best == d3:
                        current = other[:-1] + current
                    else:
                        current = list(reversed(other[1:])) + current
                    remaining.pop(i)
                    changed = True
                    break
            merged.append(self._dedupe_consecutive(current))
        return merged

    def _pick_best_path(self, lat: float, lon: float, preferred_street: Optional[str]) -> Optional[dict]:
        preferred = self._base_street_name(preferred_street or "") if preferred_street else ""
        ranked: list[tuple[float, dict]] = []
        for i, path in enumerate(self._paths):
            if preferred and path["street_name"] != preferred:
                continue
            nearest = self._nearest_point_on_path(lat, lon, path["points"])
            if nearest is None:
                continue
            score = nearest["distance_m"]
            ranked.append((score, {
                "street_name": path["street_name"],
                "path_id": i,
                "points": path["points"],
                "nearest_index": nearest["index"],
                "snapped": nearest["point"],
                "distance_m": nearest["distance_m"],
            }))
        if not ranked and preferred:
            return self._pick_best_path(lat, lon, None)
        if not ranked:
            return None
        ranked.sort(key=lambda x: x[0])
        return ranked[0][1]

    def _nearest_point_on_path(self, lat: float, lon: float, points: list[PathPoint]) -> Optional[dict]:
        best = None
        for i in range(len(points) - 1):
            a = points[i]
            b = points[i + 1]
            plat, plon = nearest_point_on_segment(lat, lon, a.lat, a.lon, b.lat, b.lon)
            d = dist_metres(lat, lon, plat, plon)
            if best is None or d < best["distance_m"]:
                best = {
                    "distance_m": d,
                    "point": (plat, plon),
                    "index": i if dist_metres(plat, plon, a.lat, a.lon) <= dist_metres(plat, plon, b.lat, b.lon) else i + 1,
                }
        return best

    def _advance_index(self, path: list[PathPoint], start_idx: int, direction: int, distance_m: float) -> int:
        idx = start_idx
        remaining = distance_m
        step = 1 if direction > 0 else -1
        while 0 <= idx + step < len(path):
            a = path[idx]
            b = path[idx + step]
            seg_m = dist_metres(a.lat, a.lon, b.lat, b.lon)
            if seg_m <= 0:
                idx += step
                continue
            if seg_m >= remaining:
                return idx + step
            remaining -= seg_m
            idx += step
        return idx

    def _interpolate_position(self, path: list[PathPoint], start_idx: int, direction: int, distance_m: float) -> tuple[float, float]:
        """Return interpolated (lat, lon) exactly distance_m along path from start_idx."""
        idx = start_idx
        remaining = distance_m
        step = 1 if direction > 0 else -1
        while 0 <= idx + step < len(path):
            a = path[idx]
            b = path[idx + step]
            seg_m = dist_metres(a.lat, a.lon, b.lat, b.lon)
            if seg_m <= 0:
                idx += step
                continue
            if seg_m >= remaining:
                frac = remaining / seg_m
                lat = a.lat + frac * (b.lat - a.lat)
                lon = a.lon + frac * (b.lon - a.lon)
                return lat, lon
            remaining -= seg_m
            idx += step
        return path[idx].lat, path[idx].lon

    def _describe_current_pois(self, include_seen: bool) -> str:
        nearby = self._nearby_pois(self.state.lat, self.state.lon, self.poi_radius_m)
        # Collect POI keys within range, excluding any kinds in the exclude set
        current_keys = {self._poi_key(p) for p in nearby
                        if p.get("kind", "") not in _FREE_KIND_EXCLUDE}

        if self.debug:
            names = [
                f"{(p.get('name') or p.get('kind') or 'POI').strip()}[{p.get('kind', '')}]"
                for p in nearby
            ]
            self._verbose(
                f"[FreeDebug] describe_current include_seen={include_seen} "
                f"nearby={len(nearby)} current_keys={len(current_keys)} "
                f"items={names}"
            )

        # No POIs in range — clear positional memory and return nothing
        if not current_keys:
            self._seen_poi_pos = set()
            if self.debug:
                self._verbose("[FreeDebug] describe_current: no current_keys after filtering")
            return ""

        left  = False
        right = False

        if include_seen:
            # include_seen=True: report everything currently in range regardless of history
            for poi in nearby:
                if poi.get("kind", "") in _FREE_KIND_EXCLUDE:
                    continue
                phrase = self._classify_poi(poi)
                if phrase.startswith("left:"):
                    left = True
                elif phrase.startswith("right:"):
                    right = True
        else:
            # Only announce POIs that have not been recently announced or have just entered range
            new_keys: set[str] = set()
            for key in current_keys:
                # A key is considered new if it wasn't present on the previous step or it
                # hasn't been reannounced within the configured number of steps.
                last_seen_step = self._poi_last_seen.get(key, -1)
                if key not in self._seen_poi_pos or (self._global_step_count - last_seen_step) > self.reannounce_steps:
                    new_keys.add(key)
            if self.debug:
                self._verbose(
                    f"[FreeDebug] describe_current new_keys={len(new_keys)} "
                    f"seen_prev={len(self._seen_poi_pos)} step={self._global_step_count}"
                )
            for poi in nearby:
                if poi.get("kind", "") in _FREE_KIND_EXCLUDE:
                    continue
                k = self._poi_key(poi)
                if k not in new_keys:
                    if self.debug:
                        name = (poi.get("name") or poi.get("kind") or "POI").strip()
                        self._verbose(f"[FreeDebug] describe_current skip {name}: not new")
                    continue
                phrase = self._classify_poi(poi)
                if phrase.startswith("left:"):
                    left = True
                elif phrase.startswith("right:"):
                    right = True

        # Update seen and last-seen information.  Only refresh the timer for
        # POIs that were newly announced; others retain their original timestamp
        # so the reannounce window expires naturally rather than being reset
        # every step the POI stays in range.
        announced_keys = new_keys if not include_seen else current_keys
        for k in announced_keys:
            self._poi_last_seen[k] = self._global_step_count
        self._seen_poi_pos = current_keys

        if left or right:
            # Mark POIs that triggered this alert as recently uttered and
            # record the side-call step.  Treat the automatic tone as
            # equivalent to the user pressing left/right: subsequent side
            # queries within this step should repeat the names, but queries
            # after moving a step or two should suppress them.  We update
            # both ``_poi_last_uttered`` and ``_last_side_call_step``.
            for k in (new_keys if not include_seen else current_keys):
                # Only track keys that are actually in range and not excluded
                self._poi_last_uttered[k] = self._global_step_count
            self._last_side_call_step = self._global_step_count
            if left and right:
                return "POIs left and right."
            if left:
                return "POIs left."
            if right:
                return "POIs right."
        if self.debug:
            # Emit debug information when no POIs were reported.
            if not current_keys:
                self._verbose(
                    f"[FreeDebug] no POIs within radius {self.poi_radius_m}m at "
                    f"lat={self.state.lat:.6f}, lon={self.state.lon:.6f}"
                )
            else:
                self._verbose(
                    f"[FreeDebug] POIs found but none new to announce at "
                    f"lat={self.state.lat:.6f}, lon={self.state.lon:.6f}"
                )
        return ""

    def _describe_side(self, side: str) -> tuple[str, list]:
        """Return a comma-separated description of POIs on the given side.

        The behaviour differs depending on whether this is a repeat query within
        the same step or a new one after moving:

          * If the caller presses left/right again before stepping, the same list
            of POIs should be repeated.  To support this, we treat a call
            occurring at the same global step count as a repeat and do not
            filter out recently uttered POIs.
          * If the caller has moved one or more steps since the last side
            query, POIs recently spoken (within ``reannounce_steps`` steps)
            should be suppressed.  This prevents the same venues from being
            announced every time the user moves a short distance and asks
            again for left/right POIs.

        The method returns a tuple of the spoken text and the list of POI
        dictionaries corresponding to that text.
        """
        # Determine whether this call is a repeat within the same step.  We use
        # ``_last_side_call_step`` to store the step count at which the most
        # recent left/right query occurred.  If it matches the current
        # ``_global_step_count``, we consider this a repeat call and allow
        # announcing the same POIs again.
        is_repeat = (self._last_side_call_step == self._global_step_count)
        # Update the last side call step for next time
        self._last_side_call_step = self._global_step_count

        # Always clear side-specific memory so that repeated calls within the
        # same step can still enumerate all POIs.  The separate suppression
        # logic based on ``_poi_last_uttered`` will handle deduplication across
        # steps.
        self._seen_side_pos.clear()
        nearby = self._nearby_pois(self.state.lat, self.state.lon, self.poi_radius_m)
        if self.debug:
            self._verbose(f"[FreeDebug] describe_side side={side} nearby={len(nearby)}")
        entries: list[tuple[int, str, dict]] = []
        for poi in nearby:
            kind = poi.get("kind", "")
            # Skip excluded kinds entirely
            if kind in _FREE_KIND_EXCLUDE:
                if self.debug:
                    name = (poi.get("name") or poi.get("kind") or "POI").strip()
                    self._verbose(f"[FreeDebug] describe_side skip {name}: excluded kind='{kind}'")
                continue
            phrase = self._classify_poi(poi)
            # Only interested in POIs on the requested side
            if not phrase.startswith(side + ":"):
                if self.debug:
                    name = (poi.get("name") or poi.get("kind") or "POI").strip()
                    self._verbose(f"[FreeDebug] describe_side skip {name}: phrase='{phrase}'")
                continue
            # Determine POI key for tracking
            key = self._poi_key(poi)
            # If this is not a repeat call within the same step, filter out
            # POIs that were uttered recently (within ``reannounce_steps`` steps).
            if not is_repeat:
                last_uttered_step = self._poi_last_uttered.get(key, -1)
                if last_uttered_step >= 0 and (self._global_step_count - last_uttered_step) <= self.reannounce_steps:
                    # Skip this POI as it was announced too recently
                    if self.debug:
                        name = (poi.get("name") or poi.get("kind") or "POI").strip()
                        self._verbose(f"[FreeDebug] describe_side skip {name}: uttered at step {last_uttered_step}")
                    continue
            # Extract name and priority for sorting
            name = phrase[len(side) + 1:].strip()
            priority = _FREE_KIND_PRIORITY.get(kind, 2)
            entries.append((priority, name, poi))
            if self.debug:
                self._verbose(f"[FreeDebug] describe_side keep {name}: priority={priority}")
        # Sort by priority (0: food/essential, 1: transport, 2: others)
        entries.sort(key=lambda x: x[0])
        # Construct spoken text
        text = ", ".join(e[1] for e in entries) if entries else ""
        # Update tracking: mark each returned POI as uttered at this step
        for _, _, poi in entries:
            key = self._poi_key(poi)
            self._poi_last_uttered[key] = self._global_step_count
            self._seen_side_pos.add(key)
        return text, [e[2] for e in entries]

    def _nearby_pois(self, lat: float, lon: float, radius_m: float) -> list[dict]:
        """Return all POIs within ``radius_m`` of the given point, filtering
        out those located far behind the current heading.

        In addition to the radial distance, this method computes the
        difference between the traveller's heading and the bearing to each
        candidate POI.  POIs whose angle exceeds the configured
        ``poi_back_threshold_deg`` are ignored.  This prevents the engine
        from announcing places that are effectively behind you while still
        including those on both sides and slightly ahead.
        """
        found: list[tuple[float, dict]] = []
        if self.debug:
            self._verbose(
                f"[FreeDebug] nearby scan lat={lat:.6f} lon={lon:.6f} "
                f"heading={getattr(self.state, 'heading_deg', 0.0):.1f} "
                f"radius={radius_m:.1f} candidates={len(self._pois)}"
            )
        # Precompute forward vector components for the current heading
        heading = getattr(self.state, 'heading_deg', 0.0)
        ang_rad = math.radians(heading)
        fwd_x = math.sin(ang_rad)
        fwd_y = math.cos(ang_rad)
        # Precompute cos(lat) for longitude scaling
        lat_rad = math.radians(lat)
        cos_lat = math.cos(lat_rad)
        for poi in self._pois:
            # Filter out POIs whose kind is not in the allowed set
            kind = poi.get('kind')
            name = (poi.get("name") or poi.get("label") or poi.get("kind") or "POI").strip()
            if kind:
                k = str(kind).strip().lower()
                if k not in self.allowed_kinds:
                    if self.debug:
                        self._verbose(f"[FreeDebug] skip {name}: kind='{k}' not allowed")
                    continue
            p_lat = float(poi.get("lat", 0.0))
            p_lon = float(poi.get("lon", 0.0))
            d = dist_metres(lat, lon, p_lat, p_lon)
            if d > radius_m:
                if self.debug:
                    self._verbose(f"[FreeDebug] skip {name}: distance {d:.1f}m > radius {radius_m:.1f}m")
                continue
            # Compute vector to POI in metres
            dx = (p_lon - lon) * 111_000.0 * cos_lat
            dy = (p_lat - lat) * 111_000.0
            # Dot product with forward vector; negative means behind
            dot = dx * fwd_x + dy * fwd_y
            # If the projection along the heading is significantly negative,
            # the POI lies behind us.  Allow a small tolerance (≈5 m) to
            # account for rounding errors and cross‑street alignment.
            if dot < -5.0:
                if self.debug:
                    self._verbose(f"[FreeDebug] skip {name}: behind heading dot={dot:.1f}")
                continue
            if self.debug:
                self._verbose(f"[FreeDebug] keep {name}: kind='{kind}' dist={d:.1f}m dot={dot:.1f}")
            found.append((d, poi))
        found.sort(key=lambda x: x[0])
        if self.debug:
            self._verbose(f"[FreeDebug] nearby result count={len(found)}")
        return [poi for _, poi in found]

    def _classify_poi(self, poi: dict) -> str:
        lat = self.state.lat
        lon = self.state.lon
        p_lat = float(poi["lat"])
        p_lon = float(poi["lon"])
        ang = math.radians(self.state.heading_deg)
        fwd_x = math.sin(ang)
        fwd_y = math.cos(ang)
        dx = (p_lon - lon) * 111_000.0 * math.cos(math.radians(lat))
        dy = (p_lat - lat) * 111_000.0
        side_m = dx * fwd_y - dy * fwd_x
        rel = "right" if side_m > 0 else "left"
        name = (poi.get("name") or poi.get("kind") or "POI").strip()
        return f"{rel}:{name}"

    def _intersection_hint(self, force: bool = False) -> str:
        if not self._paths or not self.state.street_name:
            return ""
        current = self._base_street_name(self.state.street_name)
        RADIUS  = max(self.intersection_radius_m, 50.0)
        found: list[tuple[float, str]] = []
        active_keys: set[str] = set()

        for p in self._paths:
            other = self._base_street_name(p["street_name"])
            if not other or other == current:
                continue
            nearest = self._nearest_point_on_path(
                self.state.lat, self.state.lon, p["points"])
            if nearest is None:
                continue
            if nearest["distance_m"] <= RADIUS:
                found.append((nearest["distance_m"], other))
                active_keys.add(other.lower())

        # Clear crossings that have fallen out of range
        self._seen_crossings.intersection_update(active_keys)

        if not found:
            return ""

        uniq: list[str] = []
        seen: set[str] = set()
        for _, name in sorted(found):
            if name.lower() not in seen:
                uniq.append(name)
                seen.add(name.lower())

        if not uniq:
            return ""

        if force:
            return (f"Nearest cross street: {uniq[0]}."
                    if len(uniq) == 1
                    else "Cross streets: " + ", ".join(uniq[:3]) + ".")

        # Only announce crossings not yet seen
        new = [n for n in uniq if n.lower() not in self._seen_crossings]
        if not new:
            return ""
        for n in new:
            self._seen_crossings.add(n.lower())
        if len(new) == 1:
            return f"{new[0]} crossing."
        return ", ".join(new[:3]) + " crossing."

    def _heading_for_index(self, path: list[PathPoint], idx: int, direction: int) -> float:
        """Compute heading by taking the bearing from the current position to
        a point ~60m ahead along the path.

        Using start-to-end displacement rather than averaging segment bearings
        correctly handles looping roads (roundabouts, U-bends) where averaging
        produces a nonsensical result.
        """
        if len(path) < 2:
            return 0.0

        LOOK_M = 60.0
        step = 1 if direction >= 0 else -1

        # Walk ahead up to LOOK_M metres and record the endpoint
        dist_so_far = 0.0
        i = idx
        end_lat = path[idx].lat
        end_lon = path[idx].lon
        while 0 <= i + step < len(path) and dist_so_far < LOOK_M:
            a = path[i]
            b = path[i + step]
            seg_m = dist_metres(a.lat, a.lon, b.lat, b.lon)
            if seg_m > 0.5:
                dist_so_far += seg_m
                end_lat = b.lat
                end_lon = b.lon
            i += step

        if dist_so_far < 0.5:
            # Fallback — less than one step ahead, use adjacent segment
            if direction >= 0:
                a = path[idx]
                b = path[min(idx + 1, len(path) - 1)]
                if a == b and idx > 0:
                    a = path[idx - 1]
            else:
                a = path[max(idx - 1, 0)]
                b = path[idx]
                if a == b and idx + 1 < len(path):
                    b = path[idx + 1]
            return bearing_deg(a.lat, a.lon, b.lat, b.lon)

        return bearing_deg(path[idx].lat, path[idx].lon, end_lat, end_lon)

    def _path_length(self, points: list[PathPoint]) -> float:
        total = 0.0
        for i in range(len(points) - 1):
            total += dist_metres(points[i].lat, points[i].lon, points[i + 1].lat, points[i + 1].lon)
        return total

    def _dedupe_consecutive(self, pts: list[PathPoint]) -> list[PathPoint]:
        if not pts:
            return []
        out = [pts[0]]
        for pt in pts[1:]:
            if dist_metres(out[-1].lat, out[-1].lon, pt.lat, pt.lon) > 0.5:
                out.append(pt)
        return out

    def _base_street_name(self, name: str) -> str:
        return re.sub(r"\s*\(.*?\)", "", (name or "")).strip()

    def _poi_key(self, poi: dict) -> str:
        name = (poi.get("name") or poi.get("kind") or "").strip().lower()
        lat = round(float(poi.get("lat", 0.0)), 6)
        lon = round(float(poi.get("lon", 0.0)), 6)
        return f"{name}|{lat}|{lon}"


class FreeMixin:

    def _free_announce_poi_update(self):
        """Called when POI background fetch completes while in free mode."""
        if not getattr(self, '_free_mode', False):
            return
        street = self._free_engine.street_name or getattr(self, 'street_label', '')
        addr   = self._free_address_numbers()
        poi_presence = self._free_engine._describe_current_pois(include_seen=True)
        if self._free_engine.debug:
            self._free_engine._verbose(
                f"[FreeDebug] refresh street='{street}' addr='{addr}' "
                f"all_pois={len(getattr(self, '_all_pois', []))} "
                f"presence='{poi_presence}'"
            )
        parts = []
        if street:
            parts.append(street)
        if addr:
            parts.append(addr)
        if poi_presence:
            parts.append(poi_presence)
        msg = ", ".join(parts) + "." if parts else street or "POIs loaded."
        self._status_update(msg)

    def _toggle_free_mode(self):
        if not self.street_mode:
            self._status_update("Free mode works only in street mode.", force=True)
            return
        if getattr(self, '_walking_mode', False):
            self._status_update("Turn off walking mode first with W.", force=True)
            return
        if getattr(self, '_free_mode', False):
            self._free_mode = False
            self._free_engine.debug = False
            self.update_ui("Free mode off.")
            return
        if not getattr(self, '_road_segments', []):
            self._status_update("Street data is still loading.", force=True)
            return
        # Only trigger a background POI refresh if we don't already have POIs
        # for this location — avoids hammering Overpass when toggling free mode
        # or re-entering street mode within the same area.
        _plat = getattr(self, '_poi_fetch_lat', None)
        _plon = getattr(self, '_poi_fetch_lon', None)
        _has_pois = bool(getattr(self, '_all_pois', []))
        _moved_far = (
            _plat is None or _plon is None or
            math.sqrt(((self.lat - _plat) * 111000) ** 2 +
                      ((self.lon - _plon) * 111000 *
                       math.cos(math.radians(self.lat))) ** 2) > 500
        )
        if not _has_pois or _moved_far:
            threading.Thread(target=self._fetch_all_pois_background,
                             args=(getattr(self, '_address_points', []),),
                             daemon=True).start()
        try:
            self._free_engine.debug = bool(self.settings.get("logging", {}).get("verbose", False))
            self._free_engine._verbose(
                f"[FreeDebug] toggle on street='{getattr(self, 'street_label', '')}' "
                f"road_segments={len(getattr(self, '_road_segments', []))} "
                f"all_pois={len(getattr(self, '_all_pois', []))} "
                f"poi_fetch_at=({_plat},{_plon}) moved_far={_moved_far}"
            )
            self._free_engine.set_segments(getattr(self, '_road_segments', []))
            self._free_engine.set_pois(getattr(self, '_all_pois', []))
            # Derive a heading hint from walking mode or a previous free-mode
            # state so that OSM segment direction doesn't flip left/right.
            init_heading = None
            if getattr(self, '_walking_mode', False) and hasattr(self, '_walk_heading'):
                init_heading = self._walk_heading
            elif (hasattr(self._free_engine, 'state')
                  and self._free_engine.state.street_name):
                init_heading = self._free_engine.state.heading_deg
            msg = self._free_engine.start(
                self.lat, self.lon,
                preferred_street=getattr(self, 'street_label', '') or None,
                heading_deg=init_heading,
            )
            self.lat, self.lon = self._free_engine.position
            self.street_label = self._free_engine.street_name or getattr(self, 'street_label', '')
            if not getattr(self, '_all_pois', []):
                msg += " POIs are still loading."
            else:
                addr = self._free_address_numbers()
                if addr:
                    msg = f"{msg} {addr}"
            self._free_mode = True
            self._free_last_side_pois = []
            self._free_last_side      = ""
            self._free_last_poi_sig   = None
            wx.CallAfter(self.map_panel.set_position, self.lat, self.lon, True, self.street_label)
            self._status_update(msg, force=True)
        except Exception as e:
            miab_log("errors", f"Free mode start failed: {e}", getattr(self, "settings", None))
            self._free_engine.debug = False
            self._free_mode = False
            self._status_update("Could not start free mode.", force=True)

    def _free_step(self, direction):
        if not getattr(self, '_free_mode', False):
            return
        try:
            # Store the previously announced address
            last_addr = getattr(self, '_free_last_addr', None)
            steps_taken = 0
            max_steps = int(300 // max(1, self._free_engine.step_m))
            msg = ""
            addr = ""
            # Loop until a new POI/intersection message or a different house number appears.
            while True:
                # Perform one walking step in the chosen direction
                msg = (self._free_engine.step_forward() if direction > 0
                       else self._free_engine.step_backward())
                self.lat, self.lon = self._free_engine.position
                # Reset per-step state for side-list
                self._free_last_side_pois = []
                self._free_last_side      = ""
                # Update street label
                if self._free_engine.street_name:
                    self.street_label = self._free_engine.street_name
                # Compute nearest address
                addr = self._free_address_numbers()
                # Break if there is a POI/intersection message or the address changed
                if msg or (addr and addr != last_addr):
                    break
                steps_taken += 1
                if steps_taken >= max_steps:
                    break
            # Announce directional POI tone only when there is a POI message
            if not self._game.active and msg:
                if "left and right" in msg:
                    self.sound.play_poi_tone("both")
                elif "left" in msg:
                    self.sound.play_poi_tone("left")
                elif "right" in msg:
                    self.sound.play_poi_tone("right")
            # Update map position
            wx.CallAfter(self.map_panel.set_position, self.lat, self.lon, True, self.street_label)
            # Compose announcement text if appropriate
            parts = []
            if addr:
                parts.append(addr)
            if msg:
                parts.append(msg)
            if parts:
                out = ", ".join(parts)
                if not out.endswith("."):
                    out += "."
                self.update_ui(out)
            # Remember last announced address for next iteration
            self._free_last_addr = addr
            # Debug: emit diagnostic information about the step
            if getattr(self._free_engine, 'debug', False):
                self._free_engine._verbose(
                    f"[FreeDebug] UI message: '{msg}', address: '{addr}', "
                    f"position=({self.lat:.6f},{self.lon:.6f}), street_label='{self.street_label}'"
                )
        except Exception as e:
            miab_log("errors", f"Free mode step failed: {e}", getattr(self, "settings", None))
            self._status_update("Free mode movement failed.", force=True)

    def _free_address_numbers(self) -> str:
        current = (self._free_engine.street_name or
                   getattr(self, 'street_label', '')).lower().strip()
        if not current:
            return ""

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
            "rise",
            "row",
            "mews",
            "track",
        }
        def bare(s):
            parts = re.sub(r'\s*\(.*?\)', '', s.lower()).strip().split()
            if parts and parts[-1] in SUFFIXES:
                parts = parts[:-1]
            return " ".join(parts)

        current_bare = bare(current)
        if not current_bare:
            return ""
        best_d   = float("inf")
        best_num = None
        for ap in getattr(self, '_address_points', []):
            if bare(ap.get("street", "")) != current_bare:
                continue
            d = math.sqrt(
                ((self.lat - ap["lat"]) * 111000) ** 2 +
                ((self.lon - ap["lon"]) * 111000 *
                 math.cos(math.radians(self.lat))) ** 2)
            if d < best_d:
                best_d = d
                best_num = ap.get("number")
        if not best_num:
            return ""
        try:
            num = int(re.sub(r'\D.*', '', best_num))
            return f"{num}"
        except (ValueError, TypeError):
            return ""

    def _free_heading(self):
        if getattr(self, '_free_mode', False):
            heading = compass_name(getattr(self._free_engine.state, 'heading_deg', 0.0))
            self.update_ui(f"Heading {heading}.")

    def _free_describe_intersection(self):
        if getattr(self, '_free_mode', False):
            self.update_ui(self._free_engine.describe_nearest_intersection())

    def _free_turnaround(self):
        if getattr(self, '_free_mode', False):
            self._free_last_poi_sig = None
            self.update_ui(self._free_engine.reverse())

    def _free_poi_action(self, key):
        """Delete or F2 in free mode — pick from last side's POI list then act."""
        if not getattr(self, '_free_mode', False):
            return
        pois = getattr(self, '_free_last_side_pois', [])
        side = getattr(self, '_free_last_side', '')
        if not pois:
            self._status_update(
                "Press left or right arrow first to see nearby POIs.",
                force=True,
            )
            return

        if len(pois) == 1:
            chosen = pois[0]
        else:
            names = [(p.get("name") or p.get("label") or "POI").split(",")[0].strip()
                     for p in pois]
            dlg = wx.SingleChoiceDialog(
                self,
                f"Which POI on the {side}?",
                "Select POI",
                names,
            )
            if dlg.ShowModal() != wx.ID_OK:
                dlg.Destroy()
                self.listbox.SetFocus()
                return
            chosen = pois[dlg.GetSelection()]
            dlg.Destroy()

        if key == wx.WXK_DELETE:
            # Reuse existing delete flow via a temporary single-item list
            self._poi_list  = [chosen]
            self._poi_index = 0
            self._report_poi_nonexistent()
        else:
            # F2 rename
            self._poi_list  = [chosen]
            self._poi_index = 0
            self._rename_poi()

    def _free_snap_cross(self):
        if getattr(self, '_free_mode', False):
            msg = self._free_engine.snap_to_nearest_cross()
            self.lat, self.lon = self._free_engine.position
            if self._free_engine.street_name:
                self.street_label = self._free_engine.street_name
            self._free_last_poi_sig   = None
            self._free_last_side_pois = []
            self._free_last_side      = ""
            wx.CallAfter(self.map_panel.set_position,
                         self.lat, self.lon, True, self.street_label)
            self.update_ui(msg)
