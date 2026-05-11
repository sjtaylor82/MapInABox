"""geo.py — shared geodesic helpers for Map in a Box.

All distance, bearing, compass-direction, and point-on-segment maths
live here so they are never duplicated across modules.

All coordinates are (lat, lon) in decimal degrees, WGS-84.
All distances returned are in metres unless stated otherwise.
"""

import math

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Street names that carry no navigational meaning and should be filtered out.
GENERIC_STREET_TYPES: frozenset = frozenset({
    "road", "highway", "street", "residential street", "shared street",
    "service road", "motorway", "footpath", "cycle path", "path", "steps",
    "pedestrian area", "dirt track", "bridleway", "road under construction",
})

# Low-priority OSM highway values (footways etc.) used when ranking roads.
LOW_PRIORITY_HIGHWAY: frozenset = frozenset({
    "footway", "cycleway", "path", "steps", "pedestrian",
    "track", "service", "bridleway",
})

# ---------------------------------------------------------------------------
# Distance
# ---------------------------------------------------------------------------

def dist_metres(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Flat-earth (equirectangular) distance in metres between two points.

    Accurate to within ~0.1 % for separations under 50 km, which is more
    than sufficient for neighbourhood-scale navigation.
    """
    mean_lat = math.radians((lat1 + lat2) / 2.0)
    dy = (lat1 - lat2) * 111_000.0
    dx = (lon1 - lon2) * 111_000.0 * math.cos(mean_lat)
    return math.hypot(dx, dy)


def dist_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres using the Haversine formula.

    Accurate globally — use this for world-map scale distances where the
    flat-earth approximation in dist_metres would accumulate error.

    Examples:
        dist_km(-27.47, 153.02, 51.51, -0.13)  # Brisbane → London ≈ 16330 km
    """
    R = 6_371.0  # mean Earth radius in km
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2.0) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2.0) ** 2)
    return R * 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


# ---------------------------------------------------------------------------
# Bearing
# ---------------------------------------------------------------------------

def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Forward bearing in degrees (0 = north, clockwise) from point 1 to point 2.

    Uses the equirectangular approximation — good enough for sub-50 km hops.
    """
    mean_lat = math.radians((lat1 + lat2) / 2.0)
    dlat = lat2 - lat1
    dlon = (lon2 - lon1) * math.cos(mean_lat)
    return math.degrees(math.atan2(dlon, dlat)) % 360


def compass_name(bearing: float) -> str:
    """Convert a bearing in degrees to an 8-point compass name.

    Examples:
        compass_name(0)   → 'north'
        compass_name(90)  → 'east'
        compass_name(225) → 'south-west'
    """
    _DIRS = [
        "north", "north-east", "east", "south-east",
        "south", "south-west", "west", "north-west",
    ]
    return _DIRS[int((bearing % 360 + 22.5) / 45) % 8]


# ---------------------------------------------------------------------------
# Segment geometry
# ---------------------------------------------------------------------------

def nearest_point_on_segment(
    plat: float, plon: float,
    alat: float, alon: float,
    blat: float, blon: float,
) -> tuple[float, float]:
    """Return the point on segment AB closest to P, in (lat, lon).

    All values are in decimal degrees.  The projection is done in degree-space
    (not metres) which introduces negligible error for short segments.
    """
    dlat = blat - alat
    dlon = blon - alon
    seg_len_sq = dlat * dlat + dlon * dlon
    if seg_len_sq == 0.0:
        return alat, alon
    t = max(0.0, min(1.0,
        ((plat - alat) * dlat + (plon - alon) * dlon) / seg_len_sq))
    return alat + t * dlat, alon + t * dlon


def dist_to_segment_metres(
    plat: float, plon: float,
    alat: float, alon: float,
    blat: float, blon: float,
) -> float:
    """Distance in metres from point P to the nearest point on segment AB."""
    proj_lat, proj_lon = nearest_point_on_segment(plat, plon, alat, alon, blat, blon)
    return dist_metres(plat, plon, proj_lat, proj_lon)


# ---------------------------------------------------------------------------
# Convenience: bearing between two graph nodes
# ---------------------------------------------------------------------------

def bearing_between_nodes(
    nodes: dict,
    from_nid: int,
    to_nid: int,
) -> float:
    """Bearing in degrees from one walk-graph node to another.

    ``nodes`` is the ``{"nodes": {nid: (lat, lon), …}}`` dict produced by
    ``_build_walk_graph``.
    """
    lat1, lon1 = nodes[from_nid]
    lat2, lon2 = nodes[to_nid]
    # NB: this uses a simpler formula (no mean-lat correction for dlon)
    # because the original code did the same — keep it identical so bearing
    # values don't change between versions.
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    return math.degrees(math.atan2(dlon, dlat)) % 360
