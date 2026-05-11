"""route_tools.py — Driving route tools for Map in a Box.

Provides geocoding, detour comparison, and route exploration.

When a Google API key is present we use Google Maps Platform for the
highest-coverage path. When it is absent, we fall back to open services:
Nominatim/Photon for geocoding and OSRM for routing. That keeps the core
tools usable without forcing users to collect a pile of credentials.

No wx, no pygame — returns plain data structures.

Classes
-------
RouteTools
    geocode(address, country_code) → (lat, lon, formatted_address)
    compare_routes(stops) → dict          # Detour Calculator
    explore_routes(origin, destination, status_cb) → dict  # Route Explorer
"""

from __future__ import annotations

import datetime
import json
import math
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} seconds"
    mins = seconds // 60
    if mins < 60:
        return f"{mins} minute{'s' if mins != 1 else ''}"
    hours = mins // 60
    remainder = mins % 60
    if remainder == 0:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    return f"{hours} hour{'s' if hours != 1 else ''} {remainder} min"


def _fmt_distance(metres: int) -> str:
    km = metres / 1000.0
    if km < 1.0:
        return f"{metres} metres"
    if km < 10.0:
        return f"{km:.1f} km"
    return f"{int(round(km))} km"


# ---------------------------------------------------------------------------
# Polyline decoder (Google Encoded Polyline Algorithm)
# ---------------------------------------------------------------------------

def _decode_polyline(encoded: str) -> list[tuple[float, float]]:
    """Decode a Google encoded polyline string into (lat, lon) pairs."""
    points: list[tuple[float, float]] = []
    idx = 0
    lat = 0
    lng = 0
    while idx < len(encoded):
        for coord in range(2):
            shift = 0
            result = 0
            while True:
                b = ord(encoded[idx]) - 63
                idx += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if (result & 1) else (result >> 1)
            if coord == 0:
                lat += delta
            else:
                lng += delta
        points.append((lat / 1e5, lng / 1e5))
    return points


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two points."""
    R = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _sample_polyline(points: list[tuple[float, float]],
                     interval_m: float = 7000.0,
                     ) -> list[tuple[float, float]]:
    """Sample points along a polyline at fixed distance intervals.

    Returns one (lat, lon) every ``interval_m`` metres along the path,
    skipping the very start and end (which are already known as origin
    and destination).
    """
    if len(points) < 2:
        return []

    samples: list[tuple[float, float]] = []
    cum_dist = 0.0
    next_sample = interval_m  # first sample after interval_m

    for i in range(len(points) - 1):
        seg_m = _haversine_m(points[i][0], points[i][1],
                             points[i + 1][0], points[i + 1][1])
        seg_start = cum_dist
        cum_dist += seg_m

        while next_sample <= cum_dist:
            frac = (next_sample - seg_start) / seg_m if seg_m > 0 else 0
            lat = points[i][0] + frac * (points[i + 1][0] - points[i][0])
            lon = points[i][1] + frac * (points[i + 1][1] - points[i][1])
            samples.append((lat, lon))
            next_sample += interval_m

    return samples


# ---------------------------------------------------------------------------
# RouteTools
# ---------------------------------------------------------------------------

class RouteTools:
    """Geocoding, detour comparison, and route exploration."""

    _GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
    _ROUTES_URL  = "https://routes.googleapis.com/directions/v2:computeRoutes"
    _NOMINATIM_SEARCH_URL  = "https://nominatim.openstreetmap.org/search"
    _NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
    _PHOTON_URL            = "https://photon.komoot.io/api/"
    _OSRM_URL              = "https://router.project-osrm.org/route/v1/driving"

    def __init__(self, api_key: str) -> None:
        self._key = (api_key or "").strip()

    @property
    def is_configured(self) -> bool:
        return bool(self._key)

    @staticmethod
    def _request_json(
        url: str,
        timeout: int = 10,
        data: bytes | None = None,
        headers: Optional[dict] = None,
    ) -> dict:
        req = urllib.request.Request(
            url,
            data=data,
            headers=headers or {"User-Agent": "MapInABox/1.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode()
            except Exception:
                pass
            raise RuntimeError(f"HTTP {exc.code} from {url}: {detail[:250]}")

    @staticmethod
    def _nominatim_country_code(country_code: str) -> str:
        code = (country_code or "").strip().lower()
        if code == "uk":
            return "gb"
        return code

    def _google_geocode(
        self, address: str, country_code: str = ""
    ) -> tuple[float, float, str]:
        if not self._key:
            raise RuntimeError("No Google API key configured.")

        params: dict = {"address": address, "key": self._key}
        if country_code:
            params["components"] = f"country:{country_code}"

        url = f"{self._GEOCODE_URL}?{urllib.parse.urlencode(params)}"
        data = self._request_json(url, timeout=10)
        if data.get("status") != "OK" or not data.get("results"):
            raise RuntimeError(
                f"Could not find '{address}': {data.get('status', 'no results')}"
            )

        result = data["results"][0]
        loc = result["geometry"]["location"]
        return float(loc["lat"]), float(loc["lng"]), result.get("formatted_address", address)

    def _nominatim_geocode(
        self, address: str, country_code: str = ""
    ) -> tuple[float, float, str]:
        params: dict = {
            "q": address,
            "format": "jsonv2",
            "limit": 1,
            "addressdetails": 1,
        }
        cc = self._nominatim_country_code(country_code)
        if cc:
            params["countrycodes"] = cc
        url = f"{self._NOMINATIM_SEARCH_URL}?{urllib.parse.urlencode(params)}"
        data = self._request_json(
            url,
            timeout=10,
            headers={
                "User-Agent": "MapInABox/1.0",
                "Accept-Language": "en",
            },
        )
        if not isinstance(data, list) or not data:
            raise RuntimeError(f"Could not find '{address}' with open geocoder.")
        item = data[0]
        return float(item["lat"]), float(item["lon"]), item.get("display_name", address)

    def _photon_geocode(
        self, address: str, country_code: str = ""
    ) -> tuple[float, float, str]:
        params: dict = {
            "q": address,
            "limit": 1,
            "lang": "en",
        }
        if country_code:
            params["country"] = country_code
        url = f"{self._PHOTON_URL}?{urllib.parse.urlencode(params)}"
        data = self._request_json(
            url,
            timeout=10,
            headers={"User-Agent": "MapInABox/1.0"},
        )
        features = data.get("features", [])
        if not features:
            raise RuntimeError(f"Could not find '{address}' with open geocoder.")
        feat = features[0]
        props = feat.get("properties", {})
        geom = feat.get("geometry", {})
        coords = geom.get("coordinates", [None, None])
        if coords[0] is None or coords[1] is None:
            raise RuntimeError(f"Could not find '{address}' with open geocoder.")
        label = props.get("name") or props.get("street") or address
        if props.get("city") and props.get("city") not in label:
            label = f"{label}, {props.get('city')}"
        return float(coords[1]), float(coords[0]), label

    def _open_geocode(
        self, address: str, country_code: str = ""
    ) -> tuple[float, float, str]:
        last_exc: Exception | None = None
        for fn in (self._nominatim_geocode, self._photon_geocode):
            try:
                return fn(address, country_code)
            except Exception as exc:
                last_exc = exc
        raise RuntimeError(
            f"Could not find '{address}' with open geocoders."
            + (f" ({last_exc})" if last_exc else "")
        )

    # ------------------------------------------------------------------
    # Geocoding
    # ------------------------------------------------------------------

    def geocode(
        self, address: str, country_code: str = ""
    ) -> tuple[float, float, str]:
        """Resolve an address string to (lat, lon, formatted_address)."""
        try:
            if self._key:
                return self._google_geocode(address, country_code)
        except Exception as exc:
            print(f"[RouteTools] Google geocode failed, falling back to open data: {exc}")
        return self._open_geocode(address, country_code)

    def _reverse_geocode_suburb(self, lat: float, lon: float) -> str:
        """Return the suburb/locality name for a point, or empty string."""
        if self._key:
            params = {
                "latlng": f"{lat},{lon}",
                "result_type": "locality|sublocality|administrative_area_level_2",
                "key": self._key,
            }
            url = f"{self._GEOCODE_URL}?{urllib.parse.urlencode(params)}"
            try:
                data = self._request_json(url, timeout=8)
            except Exception:
                data = {}

            for result in data.get("results", []):
                for comp in result.get("address_components", []):
                    types = comp.get("types", [])
                    if "sublocality" in types or "locality" in types:
                        return comp.get("long_name", "")
            for result in data.get("results", []):
                for comp in result.get("address_components", []):
                    if "administrative_area_level_2" in comp.get("types", []):
                        return comp.get("long_name", "")

        params = {
            "lat": lat,
            "lon": lon,
            "format": "jsonv2",
            "addressdetails": 1,
            "zoom": 18,
        }
        url = f"{self._NOMINATIM_REVERSE_URL}?{urllib.parse.urlencode(params)}"
        try:
            data = self._request_json(
                url,
                timeout=8,
                headers={
                    "User-Agent": "MapInABox/1.0",
                    "Accept-Language": "en",
                },
            )
        except Exception:
            return ""
        addr = data.get("address", {})
        for key in ("suburb", "city_district", "quarter", "neighbourhood", "town", "city", "county"):
            value = addr.get(key, "")
            if value:
                return value
        return ""

    def _suburb_chain(
        self, points: list[tuple[float, float]],
        sample_interval_m: float = 7000.0,
        status_cb: Optional[Callable[[str], None]] = None,
    ) -> list[str]:
        """Return deduplicated suburb names sampled along a polyline."""
        samples = _sample_polyline(points, interval_m=sample_interval_m)
        suburbs: list[str] = []
        for i, (lat, lon) in enumerate(samples):
            if status_cb and i % 3 == 0:
                status_cb(f"Identifying suburbs... ({i + 1}/{len(samples)})")
            name = self._reverse_geocode_suburb(lat, lon)
            if name and (not suburbs or name != suburbs[-1]):
                suburbs.append(name)
        return suburbs

    # ------------------------------------------------------------------
    # Route computation (Google or open fallback) — internal
    # ------------------------------------------------------------------

    def _routes_request(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        alternatives: bool = False,
        request_tolls: bool = False,
        request_polyline: bool = False,
    ) -> list[dict]:
        """Raw route request. Returns a list of provider route dicts."""
        if self._key:
            try:
                return self._google_routes_request(
                    origin,
                    destination,
                    alternatives=alternatives,
                    request_tolls=request_tolls,
                    request_polyline=request_polyline,
                )
            except Exception as exc:
                print(f"[RouteTools] Google routes failed, falling back to OSRM: {exc}")
        return self._osrm_routes_request(
            origin,
            destination,
            alternatives=alternatives,
            request_tolls=request_tolls,
            request_polyline=request_polyline,
        )

    def _google_routes_request(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        alternatives: bool = False,
        request_tolls: bool = False,
        request_polyline: bool = False,
    ) -> list[dict]:
        if not self._key:
            raise RuntimeError("No Google API key configured.")

        def _wp(lat, lon):
            return {"location": {"latLng": {"latitude": lat, "longitude": lon}}}

        now_utc = ((datetime.datetime.now(datetime.timezone.utc)
                    + datetime.timedelta(seconds=60))
                   .strftime("%Y-%m-%dT%H:%M:%SZ"))

        body: dict = {
            "origin": _wp(*origin),
            "destination": _wp(*destination),
            "travelMode": "DRIVE",
            "routingPreference": "TRAFFIC_AWARE",
            "departureTime": now_utc,
            "computeAlternativeRoutes": alternatives,
        }

        if request_tolls:
            body["extraComputations"] = ["TOLLS"]

        fields = [
            "routes.duration",
            "routes.distanceMeters",
            "routes.description",
        ]
        if request_polyline:
            fields.append("routes.polyline.encodedPolyline")
        if request_tolls:
            fields.append("routes.travelAdvisory.tollInfo")

        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": self._key,
            "X-Goog-FieldMask": ",".join(fields),
        }

        req = urllib.request.Request(
            self._ROUTES_URL,
            data=json.dumps(body).encode(),
            headers=headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode()
            except Exception:
                pass
            print(f"[RouteTools] HTTP {exc.code}: {detail}")
            raise RuntimeError(
                f"Google Routes API error {exc.code}. {detail[:300]}"
            )
        except Exception as exc:
            raise RuntimeError(f"Routes request failed: {exc}")

        return data.get("routes", [])

    def _osrm_routes_request(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        alternatives: bool = False,
        request_tolls: bool = False,
        request_polyline: bool = False,
    ) -> list[dict]:
        coord_text = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in (origin, destination))
        params = {
            "alternatives": "true" if alternatives else "false",
            "overview": "full" if request_polyline else "simplified",
            "geometries": "polyline",
            "steps": "true",
        }
        url = f"{self._OSRM_URL}/{coord_text}?{urllib.parse.urlencode(params)}"
        data = self._request_json(
            url,
            timeout=15,
            headers={"User-Agent": "MapInABox/1.0"},
        )
        if data.get("code") != "Ok":
            raise RuntimeError(f"OSRM route error: {data.get('code', 'unknown')}")
        return data.get("routes", [])

    def _parse_route(self, route: dict) -> dict:
        """Parse a single route dict from the API into a clean summary."""
        if "distanceMeters" in route or "travelAdvisory" in route:
            dur_str = route.get("duration", "0s")
            duration_s = int(dur_str.rstrip("s")) if isinstance(dur_str, str) else int(dur_str)
            distance_m = int(route.get("distanceMeters", 0))

            toll_price = None
            toll_currency = ""
            toll_info = (route.get("travelAdvisory") or {}).get("tollInfo")
            if toll_info:
                estimated = toll_info.get("estimatedPrice")
                if estimated and len(estimated) > 0:
                    price = estimated[0]
                    toll_currency = price.get("currencyCode", "")
                    units = int(price.get("units", 0))
                    nanos = int(price.get("nanos", 0))
                    toll_price = units + nanos / 1_000_000_000.0

            description = route.get("description", "")
            polyline = (route.get("polyline") or {}).get("encodedPolyline", "")

            legs = []
            for leg in route.get("legs", []):
                leg_dur = leg.get("duration", "0s")
                leg_s = int(leg_dur.rstrip("s")) if isinstance(leg_dur, str) else int(leg_dur)
                leg_m = int(leg.get("distanceMeters", 0))
                legs.append({
                    "duration_s": leg_s,
                    "distance_m": leg_m,
                    "duration_text": _fmt_duration(leg_s),
                    "distance_text": _fmt_distance(leg_m),
                })

            return {
                "duration_s": duration_s,
                "distance_m": distance_m,
                "duration_text": _fmt_duration(duration_s),
                "distance_text": _fmt_distance(distance_m),
                "toll_price": toll_price,
                "toll_currency": toll_currency,
                "description": description,
                "polyline": polyline,
                "legs": legs,
                "provider": "google",
                "supports_tolls": True,
            }

        duration_s = int(round(float(route.get("duration", 0))))
        distance_m = int(round(float(route.get("distance", 0))))
        geometry = route.get("geometry", "")
        legs = []
        summaries = []
        for leg in route.get("legs", []):
            leg_s = int(round(float(leg.get("duration", 0))))
            leg_m = int(round(float(leg.get("distance", 0))))
            summary = (leg.get("summary") or "").strip()
            if summary:
                summaries.append(summary)
            legs.append({
                "duration_s": leg_s,
                "distance_m": leg_m,
                "duration_text": _fmt_duration(leg_s),
                "distance_text": _fmt_distance(leg_m),
            })
        description = " / ".join(dict.fromkeys(summaries)) if summaries else "OSRM route"
        return {
            "duration_s": duration_s,
            "distance_m": distance_m,
            "duration_text": _fmt_duration(duration_s),
            "distance_text": _fmt_distance(distance_m),
            "toll_price": None,
            "toll_currency": "",
            "description": description,
            "polyline": geometry,
            "legs": legs,
            "provider": "osrm",
            "supports_tolls": False,
        }

    # ------------------------------------------------------------------
    # Legacy _compute_route (used by Detour Calculator)
    # ------------------------------------------------------------------

    def _compute_route(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        intermediates: Optional[list[tuple[float, float]]] = None,
        avoid_tolls: bool = False,
        request_tolls: bool = False,
    ) -> dict:
        """Single-route computation for detour calculator."""
        if not self._key:
            return self._osrm_compute_route(
                origin,
                destination,
                intermediates=intermediates,
                avoid_tolls=avoid_tolls,
                request_tolls=request_tolls,
            )

        try:
            def _wp(lat, lon):
                return {"location": {"latLng": {"latitude": lat, "longitude": lon}}}

            now_utc = ((datetime.datetime.now(datetime.timezone.utc)
                        + datetime.timedelta(seconds=60))
                       .strftime("%Y-%m-%dT%H:%M:%SZ"))

            body: dict = {
                "origin": _wp(*origin),
                "destination": _wp(*destination),
                "travelMode": "DRIVE",
                "routingPreference": "TRAFFIC_AWARE",
                "departureTime": now_utc,
            }

            if intermediates:
                body["intermediates"] = [_wp(*pt) for pt in intermediates]
            if avoid_tolls:
                body.setdefault("routeModifiers", {})["avoidTolls"] = True
            if request_tolls:
                body["extraComputations"] = ["TOLLS"]

            fields = [
                "routes.duration",
                "routes.distanceMeters",
                "routes.legs.duration",
                "routes.legs.distanceMeters",
            ]
            if request_tolls:
                fields.append("routes.travelAdvisory.tollInfo")

            headers = {
                "Content-Type": "application/json",
                "X-Goog-Api-Key": self._key,
                "X-Goog-FieldMask": ",".join(fields),
            }

            req = urllib.request.Request(
                self._ROUTES_URL,
                data=json.dumps(body).encode(),
                headers=headers,
                method="POST",
            )

            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
            if not data.get("routes"):
                raise RuntimeError("Google Routes API returned no routes.")
            return self._parse_route(data["routes"][0])
        except Exception as exc:
            print(f"[RouteTools] Google route failed, falling back to OSRM: {exc}")
            return self._osrm_compute_route(
                origin,
                destination,
                intermediates=intermediates,
                avoid_tolls=avoid_tolls,
                request_tolls=False,
            )

    def _osrm_compute_route(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        intermediates: Optional[list[tuple[float, float]]] = None,
        avoid_tolls: bool = False,
        request_tolls: bool = False,
    ) -> dict:
        coords = [origin] + list(intermediates or []) + [destination]
        coord_text = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in coords)
        params = {
            "overview": "full",
            "geometries": "polyline",
            "steps": "true",
        }
        url = f"{self._OSRM_URL}/{coord_text}?{urllib.parse.urlencode(params)}"
        data = self._request_json(
            url,
            timeout=15,
            headers={"User-Agent": "MapInABox/1.0"},
        )
        if data.get("code") != "Ok" or not data.get("routes"):
            raise RuntimeError(f"OSRM route error: {data.get('code', 'unknown')}")
        return self._parse_route(data["routes"][0])

    # ------------------------------------------------------------------
    # Detour Calculator — multi-stop vs direct
    # ------------------------------------------------------------------

    def compare_routes(
        self,
        stops: list[tuple[float, float, str]],
    ) -> dict:
        """Compare a multi-stop route with a direct origin-to-destination route."""
        if len(stops) < 3:
            raise ValueError("Need at least 3 stops for route comparison.")

        origin = (stops[0][0], stops[0][1])
        destination = (stops[-1][0], stops[-1][1])
        intermediates = [(s[0], s[1]) for s in stops[1:-1]]
        stop_names = [s[2] for s in stops]

        via = self._compute_route(origin, destination, intermediates=intermediates)
        direct = self._compute_route(origin, destination)

        time_diff = via["duration_s"] - direct["duration_s"]
        dist_diff = via["distance_m"] - direct["distance_m"]

        via_names = " to ".join(stop_names)
        direct_names = f"{stop_names[0]} to {stop_names[-1]}"

        lines = [
            f"With detour: {via_names}",
            f"  {via['duration_text']}, {via['distance_text']}.",
        ]

        if len(via["legs"]) > 1:
            lines.append("")
            lines.append("Leg breakdown:")
            for i, leg in enumerate(via["legs"]):
                lines.append(
                    f"  {stop_names[i]} to {stop_names[i + 1]}: "
                    f"{leg['duration_text']}, {leg['distance_text']}."
                )

        lines.append("")
        lines.append(f"Direct: {direct_names}")
        lines.append(f"  {direct['duration_text']}, {direct['distance_text']}.")
        lines.append("")

        if time_diff > 0:
            lines.append(
                f"The detour adds {_fmt_duration(abs(time_diff))} "
                f"and {_fmt_distance(abs(dist_diff))}."
            )
        elif time_diff < 0:
            lines.append(
                f"The detour is actually {_fmt_duration(abs(time_diff))} faster "
                f"and {_fmt_distance(abs(dist_diff))} shorter."
            )
        else:
            lines.append("Both routes take about the same time.")

        return {
            "via_route": via,
            "direct_route": direct,
            "time_diff_s": time_diff,
            "dist_diff_m": dist_diff,
            "stop_names": stop_names,
            "summary_text": "\n".join(lines),
        }

    # ------------------------------------------------------------------
    # Route Explorer — alternatives with suburb chain and tolls
    # ------------------------------------------------------------------

    def explore_routes(
        self,
        origin: tuple[float, float, str],
        destination: tuple[float, float, str],
        status_cb: Optional[Callable[[str], None]] = None,
    ) -> dict:
        """Fetch up to 3 alternative routes with suburb chains and toll info.

        Returns dict with keys: routes (list), summary_text.
        Each route entry has: duration_text, distance_text, description,
        toll_price, toll_currency, suburbs (list[str]).
        """
        o = (origin[0], origin[1])
        d = (destination[0], destination[1])

        if status_cb:
            status_cb("Fetching routes...")

        raw_routes = self._routes_request(
            o, d,
            alternatives=True,
            request_tolls=True,
            request_polyline=True,
        )

        if not raw_routes:
            raise RuntimeError("No routes found.")

        parsed: list[dict] = []
        for i, raw in enumerate(raw_routes):
            if status_cb:
                status_cb(f"Analysing route {i + 1} of {len(raw_routes)}...")

            r = self._parse_route(raw)

            # Decode polyline and sample suburbs
            suburbs: list[str] = []
            if r["polyline"]:
                points = _decode_polyline(r["polyline"])
                # Adjust sample interval based on route length
                dist_km = r["distance_m"] / 1000.0
                if dist_km < 20:
                    interval = 5000.0
                elif dist_km < 80:
                    interval = 7000.0
                else:
                    interval = 15000.0
                suburbs = self._suburb_chain(points, interval, status_cb)

            r["suburbs"] = suburbs
            parsed.append(r)

        # Sort by duration (fastest first)
        parsed.sort(key=lambda r: r["duration_s"])

        # Build summary text
        lines = [f"From {origin[2]} to {destination[2]}:", ""]

        for i, r in enumerate(parsed):
            label = f"Route {i + 1}"
            if r["description"]:
                label += f" via {r['description']}"

            line = f"{label}: {r['duration_text']}, {r['distance_text']}."

            # Toll info
            if r["toll_price"] is not None and r["toll_price"] > 0:
                line = line.rstrip(".") + f", toll ${r['toll_price']:.2f} {r['toll_currency']}."
            elif r["toll_price"] is not None:
                line = line.rstrip(".") + ", no toll."

            lines.append(line)

            # Suburb chain
            if r["suburbs"]:
                lines.append(f"  Through: {', '.join(r['suburbs'])}.")
            lines.append("")

        # Summary comparison
        if len(parsed) > 1:
            fastest = parsed[0]
            for r in parsed[1:]:
                diff = r["duration_s"] - fastest["duration_s"]
                if diff > 0:
                    desc = r["description"] or f"Route"
                    lines.append(
                        f"{desc} is {_fmt_duration(diff)} slower than "
                        f"{fastest['description'] or 'the fastest route'}."
                    )

        return {
            "routes": parsed,
            "summary_text": "\n".join(lines),
        }

    # ------------------------------------------------------------------
    # Toll Compare — toll vs toll-free for the same corridor
    # ------------------------------------------------------------------

    def compare_tolls(
        self,
        origin: tuple[float, float, str],
        destination: tuple[float, float, str],
    ) -> dict:
        """Compare toll vs toll-free routes between two points.

        Makes two separate API calls: one allowing tolls (with pricing),
        one forcing toll avoidance.  This guarantees you see both sides
        of the same corridor rather than Google's arbitrary alternatives.

        Returns dict with keys: toll_route, free_route, toll_price,
        time_saved_s, summary_text.
        """
        o = (origin[0], origin[1])
        d = (destination[0], destination[1])

        if not self._key:
            route = self._compute_route(o, d)
            return {
                "toll_route": route,
                "free_route": route,
                "toll_price": None,
                "toll_currency": "",
                "time_saved_s": 0,
                "summary_text": "\n".join([
                    f"From {origin[2]} to {destination[2]}:",
                    "",
                    f"Route: {route['duration_text']}, {route['distance_text']}.",
                    "",
                    "Open routing is available here, but toll pricing and toll-free comparison need a Google API key.",
                ]),
            }

        toll_route = self._compute_route(o, d, request_tolls=True)
        free_route = self._compute_route(o, d, avoid_tolls=True)

        time_saved = free_route["duration_s"] - toll_route["duration_s"]

        toll_price = toll_route.get("toll_price")
        toll_curr = toll_route.get("toll_currency", "")

        lines = [
            f"From {origin[2]} to {destination[2]}:",
            "",
            f"Toll route: {toll_route['duration_text']}, {toll_route['distance_text']}.",
        ]

        if toll_price is not None and toll_price > 0:
            lines[-1] = lines[-1].rstrip(".") + f", toll ${toll_price:.2f} {toll_curr}."
        else:
            lines.append("  No toll cost data available for this route.")

        lines.append("")
        lines.append(
            f"Toll-free route: {free_route['duration_text']}, {free_route['distance_text']}."
        )
        lines.append("")

        if time_saved > 0 and toll_price is not None and toll_price > 0:
            lines.append(
                f"Toll saves {_fmt_duration(time_saved)} for ${toll_price:.2f}."
            )
        elif time_saved > 0:
            lines.append(f"Toll route is {_fmt_duration(time_saved)} faster.")
        elif time_saved < 0:
            lines.append(
                f"Toll-free route is actually {_fmt_duration(abs(time_saved))} faster."
            )
        else:
            lines.append("Both routes take about the same time.")

        if (toll_route["duration_s"] == free_route["duration_s"] and
                toll_route["distance_m"] == free_route["distance_m"]):
            lines = [
                f"From {origin[2]} to {destination[2]}:",
                "",
                f"Route: {toll_route['duration_text']}, {toll_route['distance_text']}.",
                "",
                "No toll roads found on this route.",
            ]

        return {
            "toll_route": toll_route,
            "free_route": free_route,
            "toll_price": toll_price,
            "toll_currency": toll_curr,
            "time_saved_s": time_saved,
            "summary_text": "\n".join(lines),
        }

    # ------------------------------------------------------------------
    # Journey Planner — public transit with alternatives
    # ------------------------------------------------------------------

    _DIRECTIONS_URL = "https://maps.googleapis.com/maps/api/directions/json"

    # Google transit_mode filter values
    TRANSIT_FILTERS = {
        "all":     None,
        "bus":     "bus",
        "train":   "rail|train|tram|subway",
        "ferry":   "ferry",
    }

    def _transit_directions(
        self,
        origin_text: str,
        dest_text: str,
        country_code: str = "",
        departure_time: int | None = None,
        arrival_time: int | None = None,
        transit_mode: str | None = None,
    ) -> list[dict]:
        """Single Google Directions API call for transit.

        Returns list of raw route dicts from the API response.
        """
        params: dict = {
            "origin": origin_text,
            "destination": dest_text,
            "mode": "transit",
            "alternatives": "true",
            "key": self._key,
        }
        if country_code:
            # Bias results to country by appending to addresses
            if country_code not in origin_text.upper():
                params["origin"] = f"{origin_text}, {country_code}"
            if country_code not in dest_text.upper():
                params["destination"] = f"{dest_text}, {country_code}"
        if departure_time:
            params["departure_time"] = str(departure_time)
        if arrival_time:
            params["arrival_time"] = str(arrival_time)
        if transit_mode:
            params["transit_mode"] = transit_mode

        url = f"{self._DIRECTIONS_URL}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": "MapInABox/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
        except Exception as exc:
            raise RuntimeError(f"Transit directions request failed: {exc}")

        if data.get("status") != "OK":
            if data.get("status") == "ZERO_RESULTS":
                return []
            raise RuntimeError(
                f"Transit directions failed: {data.get('status', 'unknown')}"
            )

        return data.get("routes", [])

    @staticmethod
    def _parse_transit_route(route: dict, route_num: int) -> dict:
        """Parse a raw Directions API transit route into a clean structure.

        Returns dict with: summary, duration_text, departure_time,
        arrival_time, legs (list of leg dicts), transfers, dedup_key.
        """
        import re

        def _strip_html(s):
            return re.sub(r'<[^>]+>', ' ', s).replace('&nbsp;', ' ').strip()

        leg = route["legs"][0]  # transit routes always have one leg

        duration_text = leg["duration"]["text"]
        dep_time = leg.get("departure_time", {}).get("text", "")
        arr_time = leg.get("arrival_time", {}).get("text", "")
        dep_value = leg.get("departure_time", {}).get("value", 0)

        steps = leg.get("steps", [])
        parsed_legs: list[dict] = []
        service_names: list[str] = []
        transfers = 0

        for step in steps:
            mode = step.get("travel_mode", "")

            if mode == "TRANSIT":
                td = step.get("transit_details", {})
                line = td.get("line", {})
                line_name = line.get("short_name") or line.get("name", "")
                vehicle_type = line.get("vehicle", {}).get("type", "")
                agency = line.get("agencies", [{}])[0].get("name", "")
                dep_stop = td.get("departure_stop", {}).get("name", "")
                arr_stop = td.get("arrival_stop", {}).get("name", "")
                dep_t = td.get("departure_time", {}).get("text", "")
                arr_t = td.get("arrival_time", {}).get("text", "")
                num_stops = td.get("num_stops", 0)
                headsign = td.get("headsign", "")

                service_names.append(line_name or vehicle_type)
                if len(service_names) > 1:
                    transfers += 1

                parsed_legs.append({
                    "type": "transit",
                    "line_name": line_name,
                    "vehicle_type": vehicle_type,
                    "agency": agency,
                    "departure_stop": dep_stop,
                    "arrival_stop": arr_stop,
                    "departure_time": dep_t,
                    "arrival_time": arr_t,
                    "num_stops": num_stops,
                    "headsign": headsign,
                    "duration": step.get("duration", {}).get("text", ""),
                })

            elif mode == "WALKING":
                walk_steps: list[str] = []
                for sub in step.get("steps", []):
                    instruction = _strip_html(sub.get("html_instructions", ""))
                    dist = sub.get("distance", {}).get("text", "")
                    if instruction:
                        walk_steps.append(f"{instruction} ({dist})" if dist else instruction)

                parsed_legs.append({
                    "type": "walking",
                    "duration": step.get("duration", {}).get("text", ""),
                    "distance": step.get("distance", {}).get("text", ""),
                    "instructions": walk_steps,
                })

        # Build summary line for the listbox
        services = ", ".join(service_names) if service_names else "Walk"
        transfer_text = (f", {transfers} transfer{'s' if transfers != 1 else ''}"
                         if transfers > 0 else ", direct")
        summary = (f"Option {route_num}: {duration_text}"
                   f", depart {dep_time}, arrive {arr_time}"
                   f". {services}{transfer_text}.")

        # Dedup key: departure time + services used (to detect same route from two calls)
        dedup_key = f"{dep_value}|{'|'.join(service_names)}"

        return {
            "summary": summary,
            "duration_text": duration_text,
            "departure_time": dep_time,
            "arrival_time": arr_time,
            "departure_value": dep_value,
            "legs": parsed_legs,
            "transfers": transfers,
            "services": service_names,
            "dedup_key": dedup_key,
        }

    def _build_detail_text(self, parsed_route: dict) -> str:
        """Build the full detail text for a single parsed transit route."""
        lines: list[str] = []
        lines.append(f"Depart {parsed_route['departure_time']}"
                     f", arrive {parsed_route['arrival_time']}"
                     f", {parsed_route['duration_text']}.")
        lines.append("")

        for i, leg in enumerate(parsed_route["legs"]):
            if leg["type"] == "transit":
                line_desc = leg["line_name"]
                if leg["headsign"]:
                    line_desc += f" toward {leg['headsign']}"
                if leg["agency"]:
                    line_desc += f" ({leg['agency']})"
                lines.append(f"Board {line_desc}.")
                lines.append(f"  From {leg['departure_stop']}"
                            f" at {leg['departure_time']}.")
                lines.append(f"  To {leg['arrival_stop']}"
                            f" at {leg['arrival_time']}.")
                if leg["num_stops"]:
                    lines.append(f"  {leg['num_stops']} stops"
                                f", {leg['duration']}.")
                lines.append("")

            elif leg["type"] == "walking":
                lines.append(f"Walk {leg['duration']}, {leg['distance']}.")
                for instruction in leg["instructions"]:
                    lines.append(f"  {instruction}")
                lines.append("")

        return "\n".join(lines).strip()

    def journey_plan(
        self,
        origin_text: str,
        dest_text: str,
        country_code: str = "",
        timing_mode: str = "now",
        timestamp: int | None = None,
        transit_filter: str = "all",
        status_cb: Callable[[str], None] | None = None,
    ) -> list[dict]:
        """Plan a transit journey and return parsed route options.

        Parameters
        ----------
        origin_text, dest_text:
            Raw address strings (geocoded by Directions API).
        country_code:
            Two-letter ISO code to bias address resolution.
        timing_mode:
            "now", "depart", or "arrive".
        timestamp:
            Unix timestamp for depart/arrive modes.
        transit_filter:
            "all", "bus", "train", or "ferry".
        status_cb:
            Optional callback for progress updates.

        Returns list of parsed route dicts sorted by departure time.
        Each dict has: summary, duration_text, departure_time,
        arrival_time, legs, transfers, detail_text.
        """
        if not self._key:
            raise RuntimeError(
                "Journey planner needs a Google API key for transit data. "
                "For open departures and stop sequences, use the Departure Board."
            )

        mode_filter = self.TRANSIT_FILTERS.get(transit_filter)

        # Build timing params
        dep_time = None
        arr_time = None
        if timing_mode == "now":
            dep_time = int(datetime.datetime.now().timestamp())
        elif timing_mode == "depart" and timestamp:
            dep_time = timestamp
        elif timing_mode == "arrive" and timestamp:
            arr_time = timestamp

        if status_cb:
            status_cb("Searching for transit options...")

        # First call with the user's chosen timing
        routes_raw = self._transit_directions(
            origin_text, dest_text, country_code,
            departure_time=dep_time, arrival_time=arr_time,
            transit_mode=mode_filter,
        )

        # Second call for "all" filter: arrive by end of day to catch coaches
        if transit_filter == "all" and timing_mode != "arrive":
            if status_cb:
                status_cb("Checking for additional services...")
            # Arrive by 11pm same day
            if timestamp:
                base_dt = datetime.datetime.fromtimestamp(timestamp)
            else:
                base_dt = datetime.datetime.now()
            eod = base_dt.replace(hour=23, minute=0, second=0)
            eod_ts = int(eod.timestamp())
            if eod_ts > int(datetime.datetime.now().timestamp()):
                extra = self._transit_directions(
                    origin_text, dest_text, country_code,
                    arrival_time=eod_ts,
                    transit_mode=mode_filter,
                )
                routes_raw.extend(extra)

        if not routes_raw:
            raise RuntimeError("No transit routes found for this journey.")

        if status_cb:
            status_cb("Processing results...")

        # Parse and deduplicate
        seen_keys: set[str] = set()
        parsed: list[dict] = []
        for raw in routes_raw:
            r = self._parse_transit_route(raw, 0)  # number assigned after sort
            if r["dedup_key"] in seen_keys:
                continue
            seen_keys.add(r["dedup_key"])
            r["detail_text"] = self._build_detail_text(r)
            parsed.append(r)

        # Sort by departure time
        parsed.sort(key=lambda r: r["departure_value"])

        # Re-number
        for i, r in enumerate(parsed):
            r["summary"] = r["summary"].replace("Option 0:", f"Option {i + 1}:")

        return parsed

    # ------------------------------------------------------------------
    # Departure Board — HERE station search + departure boards
    # ------------------------------------------------------------------

    _HERE_STATIONS_URL   = "https://transit.hereapi.com/v8/stations"
    _HERE_DEPARTURES_URL = "https://transit.hereapi.com/v8/departures"

    def here_station_search(
        self,
        lat: float,
        lon: float,
        here_api_key: str,
        radius_m: int = 2000,
        max_stations: int = 20,
    ) -> list[dict]:
        """Find transit stations near a point using HERE Transit API.

        Returns list of dicts: name, id, lat, lon, distance_m, transport_types.
        """
        params = {
            "in": f"{lat},{lon};r={radius_m}",
            "return": "transport",
            "maxPlaces": max_stations,
            "apiKey": here_api_key,
        }
        url = f"{self._HERE_STATIONS_URL}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": "MapInABox/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
        except Exception as exc:
            raise RuntimeError(f"HERE station search failed: {exc}")

        # Debug: log first station entry to verify ID format
        raw_stations = data.get("stations", [])
        if raw_stations:
            print(f"[HERE Transit] First station raw: {json.dumps(raw_stations[0], indent=2)[:500]}")

        stations: list[dict] = []
        for stn in raw_stations:
            place = stn.get("place", {})
            name = place.get("name", "Unknown stop")
            stn_id = place.get("id", "")
            loc = place.get("location", {})
            s_lat = loc.get("lat", lat)
            s_lon = loc.get("lng", lon)

            # Distance
            d = _haversine_m(lat, lon, s_lat, s_lon)

            # Transport types available at this station
            transports = stn.get("transports", [])
            types: list[str] = []
            for t in transports:
                mode = t.get("mode", "")
                tname = t.get("name", "")
                if tname and tname not in types:
                    types.append(tname)
                elif mode and mode not in types:
                    types.append(mode)

            transport_str = ", ".join(types) if types else ""
            dist_str = _fmt_distance(int(d))

            label = f"{name}, {dist_str}"
            if transport_str:
                label += f" ({transport_str})"

            stations.append({
                "name": name,
                "id": stn_id,
                "lat": s_lat,
                "lon": s_lon,
                "distance_m": int(d),
                "transport_types": types,
                "label": label,
            })

        stations.sort(key=lambda s: s["distance_m"])
        return stations

    def here_departures(
        self,
        station_id: str,
        here_api_key: str,
        station_lat: float = 0.0,
        station_lon: float = 0.0,
        max_per_board: int = 30,
    ) -> list[dict]:
        """Get departures over ~36 hours from a station using HERE Transit API.

        Makes 2 calls (now and +18h) to cover a wider window.
        Deduplicates by line+headsign, keeping the next departure for each.
        """
        all_raw: list[dict] = []

        # Two time windows: now and +18 hours
        now = datetime.datetime.now(datetime.timezone.utc)
        offsets = [
            now,
            now + datetime.timedelta(hours=18),
        ]

        for dt in offsets:
            dt_str = dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
            data = self._here_departures_call(
                station_id, here_api_key, station_lat, station_lon,
                max_per_board, date_time=dt_str)
            if data:
                all_raw.extend(data.get("boards", []))

        departures: list[dict] = []
        for board in all_raw:
            place = board.get("place", {})

            for dep in board.get("departures", []):
                transport = dep.get("transport", {})
                line_name = transport.get("name", "")
                short_name = transport.get("shortName", "")
                headsign = transport.get("headsign", "")
                mode = transport.get("mode", "")
                timetable_url = transport.get("url", "")
                long_name = transport.get("longName", "")
                description = transport.get("description", "")
                operator_name = ""
                agency = dep.get("agency") or {}
                if agency:
                    operator_name = agency.get("name", "")

                time_str = dep.get("time", "")
                # Show date+time for departures not today
                display_time = time_str
                try:
                    if "T" in time_str:
                        date_part = time_str.split("T")[0]
                        time_part = time_str.split("T")[1][:5]
                        today = datetime.date.today().isoformat()
                        if date_part != today:
                            # Show as "Mon 14:17" for other days
                            dt_obj = datetime.datetime.fromisoformat(time_str)
                            display_time = dt_obj.strftime("%a %H:%M")
                        else:
                            display_time = time_part
                except Exception:
                    pass

                platform = dep.get("platform", "")

                line_label = short_name or line_name or mode
                parts_list = [display_time]
                if line_label:
                    parts_list.append(line_label)
                if headsign:
                    parts_list.append(f"to {headsign}")
                if operator_name:
                    parts_list.append(f"({operator_name})")
                if platform:
                    parts_list.append(f"platform {platform}")

                label = "  ".join(parts_list)

                # Sort key: full ISO time for ordering
                sort_key = time_str

                departures.append({
                    "line": line_label,
                    "direction": headsign,
                    "departure_time": display_time,
                    "operator": operator_name,
                    "platform": platform,
                    "mode": mode,
                    "label": label,
                    "url": timetable_url,
                    "long_name": long_name,
                    "description": description,
                    "dedup_key": f"{line_label}|{headsign}",
                    "sort_key": sort_key,
                    "station_lat": station_lat,
                    "station_lon": station_lon,
                })

        # Deduplicate: keep only the next departure per line+headsign
        seen: dict[str, dict] = {}
        for dep in sorted(departures, key=lambda d: d["sort_key"]):
            key = dep["dedup_key"]
            if key not in seen:
                seen[key] = dep

        # Sort by departure time
        result = sorted(seen.values(), key=lambda d: d["sort_key"])
        return result

    def _here_departures_call(
        self,
        station_id: str,
        here_api_key: str,
        station_lat: float,
        station_lon: float,
        max_per_board: int,
        date_time: str = "",
    ) -> dict | None:
        """Single HERE departures API call. Returns raw JSON dict or None."""
        params: dict = {
            "ids": station_id,
            "return": "transport",
            "maxPerBoard": max_per_board,
            "apiKey": here_api_key,
        }
        if date_time:
            params["dateTime"] = date_time

        url = f"{self._HERE_DEPARTURES_URL}?{urllib.parse.urlencode(params)}"
        print(f"[HERE Transit] Departures URL: {url.replace(here_api_key, 'KEY')}")
        req = urllib.request.Request(url, headers={"User-Agent": "MapInABox/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode()
                return json.loads(raw)
        except urllib.error.HTTPError:
            # ID lookup failed — try by coordinates
            if station_lat:
                params.pop("ids", None)
                params["in"] = f"{station_lat},{station_lon};r=100"
                url = f"{self._HERE_DEPARTURES_URL}?{urllib.parse.urlencode(params)}"
                req = urllib.request.Request(url, headers={"User-Agent": "MapInABox/1.0"})
                try:
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        raw = resp.read().decode()
                        return json.loads(raw)
                except Exception:
                    pass
        except Exception:
            pass
        return None
