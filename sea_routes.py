"""sea_routes.py — Offline sea routing for Map in a Box.

Hardcoded ports, waypoints and routes for educational maritime routing.
No API required — all distances calculated via haversine.
"""

import math

# ---------------------------------------------------------------------------
# Major ports: name → (lat, lon)
# ---------------------------------------------------------------------------
PORTS = {
    # Australia / Pacific
    "Port of Sydney":          (-33.8688,  151.2093),
    "Port of Melbourne":       (-37.8136,  144.9631),
    "Port of Brisbane":        (-27.3818,  153.1175),
    "Port of Fremantle":       (-32.0569,  115.7440),
    "Port of Adelaide":        (-34.9285,  138.6007),
    "Port of Auckland":        (-36.8485,  174.7633),
    "Port of Lyttelton":       (-43.6036,  172.7194),
    # Asia
    "Port of Singapore":       (  1.2644,  103.8223),
    "Port of Hong Kong":       ( 22.2855,  114.1577),
    "Port of Shanghai":        ( 31.2304,  121.4737),
    "Port of Tokyo":           ( 35.6762,  139.6503),
    "Port of Dubai":           ( 25.0657,   55.1713),
    "Port of Colombo":         (  6.9271,   79.8612),
    # Europe
    "Port of Southampton":     ( 50.9097,   -1.4044),
    "Port of Rotterdam":       ( 51.9244,    4.4777),
    "Port of Hamburg":         ( 53.5753,    9.9882),
    "Port of Piraeus":         ( 37.9475,   23.6432),
    # Africa
    "Port of Cape Town":       (-33.9249,   18.4241),
    "Port of Durban":          (-29.8587,   31.0218),
    # Americas
    "Port of Los Angeles":     ( 33.7294, -118.2617),
    "Port of San Francisco":   ( 37.8044, -122.2712),
    "Port of New York":        ( 40.6892,  -74.0445),
    "Port of Miami":           ( 25.7617,  -80.1918),
    "Port of New Orleans":     ( 29.9511,  -90.0715),
    "Port of Vancouver":       ( 49.2827, -123.1207),
    "Port of Valparaiso":      (-33.0472,  -71.6127),
    "Port of Buenos Aires":    (-34.6037,  -58.3816),
    "Port of Santos":          (-23.9608,  -46.3336),
    # Middle East / Indian Ocean
    "Port of Mombasa":         ( -4.0435,   39.6682),
    "Port of Mauritius":       (-20.1625,   57.4989),
}

# ---------------------------------------------------------------------------
# Named waypoints: name → (lat, lon)
# ---------------------------------------------------------------------------
WAYPOINTS = {
    "Panama Canal (Pacific)":  (  8.9936,  -79.5673),
    "Panama Canal (Atlantic)": (  9.3547,  -79.9150),
    "Suez Canal (South)":      ( 29.9668,   32.5498),
    "Suez Canal (North)":      ( 31.2653,   32.3028),
    "Cape of Good Hope":       (-34.3568,   18.4734),
    "Cape Horn":               (-55.9833,  -67.2667),
    "Strait of Malacca":       (  1.6641,  102.7052),
    "Strait of Gibraltar":     ( 35.9581,   -5.4609),
    "Strait of Hormuz":        ( 26.5667,   56.2500),
    "Bass Strait":             (-39.2000,  146.5000),
    "Torres Strait":           (-10.5833,  142.1667),
}

# ---------------------------------------------------------------------------
# Route definitions: list of (lat, lon, label) tuples
# Each entry is a waypoint the ship passes through in order.
# ---------------------------------------------------------------------------

def _p(name):
    return PORTS[name] + (name,)

def _w(name):
    return WAYPOINTS[name] + (name,)

def _c(lat, lon, label):
    return (lat, lon, label)


ROUTES = {
    # ── Australia → US West Coast ─────────────────────────────────────────
    ("AU_EAST", "US_WEST"): {
        "waypoints": [
            _c(-33.87, 151.21, "Sydney"),
            _c( 21.31,-157.86, "North Pacific"),
            _c( 33.73,-118.26, "Los Angeles"),
        ],
        "description": "Direct Pacific Ocean crossing",
        "oceans": ["Pacific Ocean"],
    },
    # ── Australia → US East Coast ─────────────────────────────────────────
    ("AU_EAST", "US_EAST"): {
        "waypoints": [
            _c(-33.87, 151.21, "Sydney"),
            _c(-20.00, 175.00, "South Pacific"),
            _c(  8.99, -79.57, "Panama Canal (Pacific entrance)"),
            _c(  9.35, -79.92, "Panama Canal (Atlantic exit)"),
            _c( 15.00, -75.00, "Caribbean Sea"),
            _c( 40.69, -74.04, "New York"),
        ],
        "description": "Pacific Ocean → Panama Canal → Caribbean Sea → Atlantic Ocean",
        "oceans": ["Pacific Ocean", "Panama Canal", "Caribbean Sea", "Atlantic Ocean"],
        "canal_note": "Panama Canal saves approximately 8,000km vs Cape Horn route",
    },
    # ── Australia → UK / Northern Europe ──────────────────────────────────
    ("AU_EAST", "EU_NORTH"): {
        "waypoints": [
            _c(-33.87, 151.21, "Sydney"),
            _c(  1.26, 103.82, "Strait of Malacca / Singapore"),
            _c(  6.93,  79.86, "Indian Ocean / Sri Lanka"),
            _c( 12.00,  44.00, "Gulf of Aden"),
            _c( 29.97,  32.55, "Suez Canal (south entrance)"),
            _c( 31.27,  32.30, "Suez Canal (north exit)"),
            _c( 35.96,  -5.46, "Strait of Gibraltar"),
            _c( 50.91,  -1.40, "Southampton"),
        ],
        "description": "Indian Ocean → Suez Canal → Mediterranean → Atlantic",
        "oceans": ["Tasman Sea", "Indian Ocean", "Suez Canal", "Mediterranean Sea", "Atlantic Ocean"],
        "canal_note": "Suez Canal saves approximately 7,000km vs Cape of Good Hope route",
    },
    # ── Australia (West) → UK / Northern Europe ───────────────────────────
    ("AU_WEST", "EU_NORTH"): {
        "waypoints": [
            _c(-32.06, 115.74, "Fremantle"),
            _c(  6.93,  79.86, "Indian Ocean / Sri Lanka"),
            _c( 12.00,  44.00, "Gulf of Aden"),
            _c( 29.97,  32.55, "Suez Canal (south entrance)"),
            _c( 31.27,  32.30, "Suez Canal (north exit)"),
            _c( 35.96,  -5.46, "Strait of Gibraltar"),
            _c( 50.91,  -1.40, "Southampton"),
        ],
        "description": "Indian Ocean → Suez Canal → Mediterranean → Atlantic",
        "oceans": ["Indian Ocean", "Suez Canal", "Mediterranean Sea", "Atlantic Ocean"],
        "canal_note": "Suez Canal route",
    },
    # ── Australia → South America ─────────────────────────────────────────
    ("AU_EAST", "SA"): {
        "waypoints": [
            _c(-33.87, 151.21, "Sydney"),
            _c(-45.00,-120.00, "South Pacific"),
            _c(-33.05, -71.61, "Valparaiso / Chile"),
        ],
        "description": "Direct South Pacific crossing",
        "oceans": ["Tasman Sea", "Pacific Ocean"],
    },
    # ── Australia → South Africa ──────────────────────────────────────────
    ("AU_EAST", "AF_SOUTH"): {
        "waypoints": [
            _c(-33.87, 151.21, "Sydney"),
            _c(-40.00,  80.00, "Southern Indian Ocean"),
            _c(-34.36,  18.47, "Cape of Good Hope"),
            _c(-33.92,  18.42, "Cape Town"),
        ],
        "description": "Southern Indian Ocean → Cape of Good Hope",
        "oceans": ["Tasman Sea", "Southern Ocean", "Indian Ocean"],
    },
    # ── New Zealand → UK ──────────────────────────────────────────────────
    ("NZ", "EU_NORTH"): {
        "waypoints": [
            _c(-36.85, 174.76, "Auckland"),
            _c(  1.26, 103.82, "Singapore / Strait of Malacca"),
            _c( 29.97,  32.55, "Suez Canal (south entrance)"),
            _c( 31.27,  32.30, "Suez Canal (north exit)"),
            _c( 35.96,  -5.46, "Strait of Gibraltar"),
            _c( 50.91,  -1.40, "Southampton"),
        ],
        "description": "Pacific → Indian Ocean → Suez Canal → Mediterranean → Atlantic",
        "oceans": ["Pacific Ocean", "Indian Ocean", "Suez Canal", "Mediterranean Sea", "Atlantic Ocean"],
        "canal_note": "Suez Canal route",
    },
}

# ---------------------------------------------------------------------------
# Region detection — map country to region code
# ---------------------------------------------------------------------------
COUNTRY_REGION = {
    "Australia":      "AU_EAST",
    "New Zealand":    "NZ",
    "United States of America": "US_EAST",
    "United States":  "US_EAST",
    "Canada":         "US_WEST",
    "United Kingdom": "EU_NORTH",
    "France":         "EU_NORTH",
    "Germany":        "EU_NORTH",
    "Netherlands":    "EU_NORTH",
    "Spain":          "EU_NORTH",
    "Portugal":       "EU_NORTH",
    "Italy":          "EU_NORTH",
    "Greece":         "EU_NORTH",
    "Norway":         "EU_NORTH",
    "Sweden":         "EU_NORTH",
    "Denmark":        "EU_NORTH",
    "South Africa":   "AF_SOUTH",
    "Brazil":         "SA",
    "Argentina":      "SA",
    "Chile":          "SA",
    "Peru":           "SA",
    "Japan":          "ASIA",
    "China":          "ASIA",
    "South Korea":    "ASIA",
    "Singapore":      "ASIA",
    "India":          "ASIA",
}

# US West Coast cities
US_WEST_CITIES = {"Los Angeles", "San Francisco", "Seattle", "Portland",
                  "San Diego", "Vancouver", "Victoria"}


def _haversine(lat1, lon1, lat2, lon2):
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlam/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))


def nearest_port(lat, lon):
    """Return (port_name, port_lat, port_lon, dist_km) for the nearest port."""
    best_dist = float('inf')
    best_name = None
    best_lat = best_lon = 0.0
    for name, (plat, plon) in PORTS.items():
        d = _haversine(lat, lon, plat, plon)
        if d < best_dist:
            best_dist, best_name = d, name
            best_lat, best_lon = plat, plon
    return best_name, best_lat, best_lon, best_dist


def get_sea_route(o_country, o_city, o_lat, o_lon, d_country, d_city, d_lat, d_lon):
    """Return sea route description string, or None if no route defined."""

    # Determine regions
    o_region = COUNTRY_REGION.get(o_country)
    d_region = COUNTRY_REGION.get(d_country)

    # Refine US region by city
    if o_country in ("United States of America", "United States"):
        o_region = "US_WEST" if o_city in US_WEST_CITIES else "US_EAST"
    if d_country in ("United States of America", "United States"):
        d_region = "US_WEST" if d_city in US_WEST_CITIES else "US_EAST"

    if not o_region or not d_region:
        return None

    # Look up route (try both directions)
    route = ROUTES.get((o_region, d_region)) or ROUTES.get((d_region, o_region))
    if not route:
        return None

    # Reverse waypoints if we found the reverse route
    waypoints = route["waypoints"]
    if not ROUTES.get((o_region, d_region)):
        waypoints = list(reversed(waypoints))

    # Calculate total distance
    total_km = 0.0
    for i in range(len(waypoints) - 1):
        total_km += _haversine(
            waypoints[i][0], waypoints[i][1],
            waypoints[i+1][0], waypoints[i+1][1])

    # Find nearest ports
    o_port, _, _, o_port_dist = nearest_port(o_lat, o_lon)
    d_port, _, _, d_port_dist = nearest_port(d_lat, d_lon)

    # Time at 22 knots (cargo) = 40.7 km/h
    CARGO_KMH   = 40.7
    CRUISE_KMH  = 51.9  # 28 knots
    cargo_days  = total_km / CARGO_KMH / 24
    cruise_days = total_km / CRUISE_KMH / 24

    # Build waypoint list string
    wp_labels = [w[2] for w in waypoints]

    lines = [
        "── By Sea ──────────────────────────────",
        f"Nearest departure port: {o_port} ({o_port_dist:.0f}km away)",
        f"Nearest arrival port:   {d_port} ({d_port_dist:.0f}km away)",
        f"Route: {route['description']}",
        f"Waypoints: {' → '.join(wp_labels)}",
        f"Total sea distance: {total_km:,.0f}km",
        f"Estimated voyage: {cargo_days:.0f} days by cargo ship (22 knots)",
        f"               or {cruise_days:.0f} days by cruise liner (28 knots)",
    ]

    if "canal_note" in route:
        lines.append(f"Note: {route['canal_note']}")

    return "\n".join(lines)

# ---------------------------------------------------------------------------
# Additional region mappings
# ---------------------------------------------------------------------------
COUNTRY_REGION.update({
    # More European countries
    "Belgium":        "EU_NORTH",
    "Ireland":        "EU_NORTH",
    "Poland":         "EU_NORTH",
    "Russia":         "EU_NORTH",
    "Russian Federation": "EU_NORTH",
    "Finland":        "EU_NORTH",
    "Switzerland":    "EU_NORTH",
    "Austria":        "EU_NORTH",
    # Mediterranean
    "Turkey":         "MED",
    "Egypt":          "MED",
    "Morocco":        "MED",
    "Algeria":        "MED",
    "Tunisia":        "MED",
    "Libya":          "MED",
    "Israel":         "MED",
    "Lebanon":        "MED",
    # Middle East
    "Saudi Arabia":   "GULF",
    "UAE":            "GULF",
    "United Arab Emirates": "GULF",
    "Kuwait":         "GULF",
    "Qatar":          "GULF",
    "Oman":           "GULF",
    "Iran":           "GULF",
    # East Africa
    "Kenya":          "AF_EAST",
    "Tanzania":       "AF_EAST",
    "Mozambique":     "AF_EAST",
    "Madagascar":     "AF_EAST",
    # West Africa
    "Nigeria":        "AF_WEST",
    "Ghana":          "AF_WEST",
    "Senegal":        "AF_WEST",
    "Ivory Coast":    "AF_WEST",
    "Cote d'Ivoire":  "AF_WEST",
    # More Asia
    "Taiwan":         "ASIA",
    "Philippines":    "ASIA",
    "Indonesia":      "ASIA",
    "Malaysia":       "ASIA",
    "Vietnam":        "ASIA",
    "Thailand":       "ASIA",
    "Myanmar":        "ASIA",
    "Bangladesh":     "ASIA",
    "Pakistan":       "ASIA",
    "Sri Lanka":      "ASIA",
    # Caribbean / Central America
    "Cuba":           "CARIB",
    "Jamaica":        "CARIB",
    "Haiti":          "CARIB",
    "Dominican Republic": "CARIB",
    "Puerto Rico":    "CARIB",
    "Trinidad and Tobago": "CARIB",
    "Mexico":         "US_WEST",
})

# Additional routes
ROUTES.update({
    # ── US East → UK ──────────────────────────────────────────────────────
    ("US_EAST", "EU_NORTH"): {
        "waypoints": [
            _c( 40.69, -74.04, "New York"),
            _c( 45.00, -40.00, "North Atlantic"),
            _c( 50.91,  -1.40, "Southampton"),
        ],
        "description": "North Atlantic crossing",
        "oceans": ["Atlantic Ocean"],
    },
    # ── US West → Asia ────────────────────────────────────────────────────
    ("US_WEST", "ASIA"): {
        "waypoints": [
            _c( 33.73,-118.26, "Los Angeles"),
            _c( 21.31,-157.86, "Hawaii (mid-Pacific)"),
            _c(  1.26, 103.82, "Singapore"),
        ],
        "description": "Pacific Ocean crossing",
        "oceans": ["Pacific Ocean"],
    },
    # ── Australia → Asia ──────────────────────────────────────────────────
    ("AU_EAST", "ASIA"): {
        "waypoints": [
            _c(-33.87, 151.21, "Sydney"),
            _c( -5.00, 115.00, "Java Sea"),
            _c(  1.26, 103.82, "Singapore"),
        ],
        "description": "Coral Sea → Java Sea → Singapore",
        "oceans": ["Coral Sea", "Java Sea"],
    },
    # ── Australia → Middle East ───────────────────────────────────────────
    ("AU_EAST", "GULF"): {
        "waypoints": [
            _c(-33.87, 151.21, "Sydney"),
            _c(  1.26, 103.82, "Strait of Malacca / Singapore"),
            _c(  6.93,  79.86, "Indian Ocean"),
            _c( 12.00,  44.00, "Gulf of Aden"),
            _c( 26.57,  56.25, "Strait of Hormuz"),
            _c( 25.07,  55.17, "Dubai"),
        ],
        "description": "Indian Ocean → Gulf of Aden → Strait of Hormuz → Persian Gulf",
        "oceans": ["Indian Ocean", "Arabian Sea", "Gulf of Aden", "Persian Gulf"],
    },
    # ── UK → South Africa ─────────────────────────────────────────────────
    ("EU_NORTH", "AF_SOUTH"): {
        "waypoints": [
            _c( 50.91,  -1.40, "Southampton"),
            _c( 35.96,  -5.46, "Strait of Gibraltar"),
            _c(  0.00,  -5.00, "Gulf of Guinea"),
            _c(-15.00,  -5.00, "South Atlantic"),
            _c(-33.92,  18.42, "Cape Town"),
        ],
        "description": "Atlantic Ocean → Cape of Good Hope",
        "oceans": ["Atlantic Ocean"],
    },
    # ── UK → East Africa ──────────────────────────────────────────────────
    ("EU_NORTH", "AF_EAST"): {
        "waypoints": [
            _c( 50.91,  -1.40, "Southampton"),
            _c( 35.96,  -5.46, "Strait of Gibraltar"),
            _c( 31.27,  32.30, "Suez Canal (north)"),
            _c( 29.97,  32.55, "Suez Canal (south)"),
            _c( 12.00,  44.00, "Gulf of Aden"),
            _c( -4.04,  39.67, "Mombasa"),
        ],
        "description": "Mediterranean → Suez Canal → Red Sea → Indian Ocean",
        "oceans": ["Atlantic Ocean", "Mediterranean Sea", "Suez Canal", "Red Sea", "Indian Ocean"],
        "canal_note": "Suez Canal route",
    },
    # ── South America → UK ────────────────────────────────────────────────
    ("SA", "EU_NORTH"): {
        "waypoints": [
            _c(-23.96, -46.33, "Santos / Brazil"),
            _c(  0.00, -25.00, "Equatorial Atlantic"),
            _c( 35.96,  -5.46, "Strait of Gibraltar"),
            _c( 50.91,  -1.40, "Southampton"),
        ],
        "description": "South Atlantic → North Atlantic",
        "oceans": ["South Atlantic Ocean", "North Atlantic Ocean"],
    },
    # ── Caribbean → UK ────────────────────────────────────────────────────
    ("CARIB", "EU_NORTH"): {
        "waypoints": [
            _c( 25.76, -80.19, "Miami / Caribbean"),
            _c( 35.00, -45.00, "North Atlantic"),
            _c( 50.91,  -1.40, "Southampton"),
        ],
        "description": "North Atlantic crossing",
        "oceans": ["Caribbean Sea", "Atlantic Ocean"],
    },
    # ── NZ → US West Coast ────────────────────────────────────────────────
    ("NZ", "US_WEST"): {
        "waypoints": [
            _c(-36.85, 174.76, "Auckland"),
            _c( -5.00,-150.00, "South Pacific"),
            _c( 21.31,-157.86, "Hawaii"),
            _c( 33.73,-118.26, "Los Angeles"),
        ],
        "description": "South Pacific → Hawaii → California",
        "oceans": ["Pacific Ocean"],
    },
    # ── Asia → UK ─────────────────────────────────────────────────────────
    ("ASIA", "EU_NORTH"): {
        "waypoints": [
            _c(  1.26, 103.82, "Singapore"),
            _c(  6.93,  79.86, "Indian Ocean"),
            _c( 12.00,  44.00, "Gulf of Aden"),
            _c( 29.97,  32.55, "Suez Canal (south)"),
            _c( 31.27,  32.30, "Suez Canal (north)"),
            _c( 35.96,  -5.46, "Strait of Gibraltar"),
            _c( 50.91,  -1.40, "Southampton"),
        ],
        "description": "Indian Ocean → Suez Canal → Mediterranean → Atlantic",
        "oceans": ["Indian Ocean", "Arabian Sea", "Red Sea", "Suez Canal", "Mediterranean Sea", "Atlantic Ocean"],
        "canal_note": "Suez Canal route",
    },
})
