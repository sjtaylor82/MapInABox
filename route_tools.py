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
    explore_routes(origin, destination, status_cb) → dict  # Suburb Lister
"""

from __future__ import annotations

import datetime
import json
import urllib.error
import urllib.parse
import urllib.request
from distance_units import format_distance
from typing import Callable, Optional
from logging_utils import miab_log

from geo import haversine_m as _haversine_m


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
    return format_distance(metres)


def _gd(value) -> dict:
    """Coerce an API-returned value to a dict.

    Some transit/route API fields are documented as objects but occasionally
    come back missing, null, or (rarely) as a bare string in malformed
    responses. Calling .get() on those directly raises AttributeError and
    kills the whole journey plan. Route every such field through this first.
    """
    return value if isinstance(value, dict) else {}


class GeocodeResult:
    """Tuple-like geocode result with optional Google place metadata."""

    __slots__ = ("lat", "lon", "formatted", "place_id")

    def __init__(
        self,
        lat: float,
        lon: float,
        formatted: str,
        place_id: str = "",
    ) -> None:
        self.lat = float(lat)
        self.lon = float(lon)
        self.formatted = formatted
        self.place_id = place_id or ""

    def __iter__(self):
        yield self.lat
        yield self.lon
        yield self.formatted

    def __len__(self) -> int:
        return 3

    def __getitem__(self, idx):
        return (self.lat, self.lon, self.formatted)[idx]

    def __repr__(self) -> str:
        return (
            f"GeocodeResult(lat={self.lat!r}, lon={self.lon!r}, "
            f"formatted={self.formatted!r}, place_id={'set' if self.place_id else 'empty'})"
        )


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
    _PHOTON_REVERSE_URL    = "https://photon.komoot.io/reverse"
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
    ) -> GeocodeResult:
        candidates = self._google_geocode_candidates(address, country_code)
        if not candidates:
            raise RuntimeError(f"Could not find '{address}': no results")
        result = candidates[0]
        loc = result["geometry"]["location"]
        return GeocodeResult(
            loc["lat"],
            loc["lng"],
            result.get("formatted_address", address),
            result.get("place_id", ""),
        )

    def _google_geocode_candidates(
        self, address: str, country_code: str = "", limit: int = 5
    ) -> list[dict]:
        if not self._key:
            raise RuntimeError("No Google API key configured.")

        params: dict = {"address": address, "key": self._key}
        if country_code:
            params["components"] = f"country:{country_code}"

        url = f"{self._GEOCODE_URL}?{urllib.parse.urlencode(params)}"
        data = self._request_json(url, timeout=10)
        if data.get("status") != "OK" or not data.get("results"):
            return []
        return data.get("results", [])[:limit]

    def _nominatim_geocode(
        self, address: str, country_code: str = ""
    ) -> GeocodeResult:
        candidates = self._nominatim_geocode_candidates(address, country_code)
        if not candidates:
            raise RuntimeError(f"Could not find '{address}' with open geocoder.")
        item = candidates[0]
        return GeocodeResult(item["lat"], item["lon"], item.get("display_name", address))

    def _nominatim_geocode_candidates(
        self, address: str, country_code: str = "", limit: int = 5
    ) -> list[dict]:
        params: dict = {
            "q": address,
            "format": "jsonv2",
            "limit": limit,
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
        if not isinstance(data, list):
            return []
        return data[:limit]

    def _photon_geocode(
        self, address: str, country_code: str = ""
    ) -> GeocodeResult:
        candidates = self._photon_geocode_candidates(address, country_code)
        if not candidates:
            raise RuntimeError(f"Could not find '{address}' with open geocoder.")
        feat = candidates[0]
        props = feat.get("properties", {})
        geom = feat.get("geometry", {})
        coords = geom.get("coordinates", [None, None])
        if coords[0] is None or coords[1] is None:
            raise RuntimeError(f"Could not find '{address}' with open geocoder.")
        label = props.get("name") or props.get("street") or address
        if props.get("city") and props.get("city") not in label:
            label = f"{label}, {props.get('city')}"
        return GeocodeResult(coords[1], coords[0], label)

    def _photon_geocode_candidates(
        self, address: str, country_code: str = "", limit: int = 5
    ) -> list[dict]:
        params: dict = {
            "q": address,
            "limit": limit,
            "lang": "en",
        }
        if country_code:
            params["countrycode"] = country_code
        url = f"{self._PHOTON_URL}?{urllib.parse.urlencode(params)}"
        data = self._request_json(
            url,
            timeout=10,
            headers={"User-Agent": "MapInABox/1.0"},
        )
        return data.get("features", [])[:limit] if isinstance(data, dict) else []

    def _open_geocode(
        self, address: str, country_code: str = ""
    ) -> GeocodeResult:
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

    def geocode_candidates(
        self, address: str, country_code: str = "", limit: int = 5
    ) -> list[GeocodeResult]:
        """Return up to `limit` candidate geocodes for disambiguation."""
        candidates: list[GeocodeResult] = []
        try:
            if self._key:
                for item in self._google_geocode_candidates(address, country_code, limit=limit):
                    loc = item.get("geometry", {}).get("location", {})
                    formatted = item.get("formatted_address", address)
                    lat = loc.get("lat")
                    lng = loc.get("lng")
                    if lat is None or lng is None:
                        continue
                    candidates.append(
                        GeocodeResult(lat, lng, formatted, item.get("place_id", ""))
                    )
        except Exception:
            candidates = []

        if candidates:
            return candidates[:limit]

        try:
            for item in self._nominatim_geocode_candidates(address, country_code, limit=limit):
                lat = item.get("lat")
                lon = item.get("lon")
                if lat is None or lon is None:
                    continue
                candidates.append(GeocodeResult(lat, lon, item.get("display_name", address)))
        except Exception:
            pass

        if candidates:
            return candidates[:limit]

        try:
            for feat in self._photon_geocode_candidates(address, country_code, limit=limit):
                props = feat.get("properties", {})
                geom = feat.get("geometry", {})
                coords = geom.get("coordinates", [None, None])
                if coords[0] is None or coords[1] is None:
                    continue
                label = props.get("name") or props.get("street") or address
                if props.get("city") and props.get("city") not in label:
                    label = f"{label}, {props.get('city')}"
                candidates.append(GeocodeResult(coords[1], coords[0], label))
        except Exception:
            pass

        return candidates[:limit]

    # ------------------------------------------------------------------
    # Geocoding
    # ------------------------------------------------------------------

    def geocode(
        self, address: str, country_code: str = ""
    ) -> GeocodeResult:
        """Resolve an address string to (lat, lon, formatted_address)."""
        try:
            if self._key:
                return self._google_geocode(address, country_code)
        except Exception as exc:
            miab_log("errors", f"[RouteTools] Google geocode failed, falling back to open data: {exc}", getattr(self, "settings", None))
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
            data = {}
        addr = data.get("address", {})
        for key in ("suburb", "city_district", "quarter", "neighbourhood", "town", "city", "county"):
            value = addr.get(key, "")
            if value:
                return value

        photon_params = {"lat": lat, "lon": lon, "lang": "en"}
        photon_url = (
            f"{self._PHOTON_REVERSE_URL}?"
            f"{urllib.parse.urlencode(photon_params)}")
        try:
            photon_data = self._request_json(
                photon_url,
                timeout=8,
                headers={"User-Agent": "MapInABox/1.0"},
            )
        except Exception:
            return ""
        features = photon_data.get("features", [])
        if not features:
            return ""
        props = features[0].get("properties", {})
        for key in ("district", "locality", "city", "county", "state"):
            value = props.get(key, "")
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

    def _suburb_anchors(
        self, points: list[tuple[float, float]], sample_interval_m: float,
        status_cb: Optional[Callable[[str], None]] = None,
    ) -> list[dict]:
        """Return named route samples with their approximate distance along."""
        samples = _sample_polyline(points, interval_m=sample_interval_m)
        anchors: list[dict] = []
        for i, (lat, lon) in enumerate(samples):
            if status_cb and i % 3 == 0:
                status_cb(f"Identifying suburbs... ({i + 1}/{len(samples)})")
            name = self._reverse_geocode_suburb(lat, lon)
            if not name:
                continue
            distance_m = float((i + 1) * sample_interval_m)
            if anchors and anchors[-1]["name"] == name:
                continue
            anchors.append({
                "name": name,
                "lat": lat,
                "lon": lon,
                "distance_m": distance_m,
            })
        return anchors

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
                miab_log("errors", f"[RouteTools] Google routes failed, falling back to OSRM: {exc}", getattr(self, "settings", None))
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
            "routes.legs.steps.navigationInstruction.instructions",
            "routes.legs.steps.distanceMeters",
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
            miab_log("errors", f"[RouteTools] HTTP {exc.code}: {detail}", getattr(self, "settings", None))
            raise RuntimeError(
                f"Google Routes API error {exc.code}. {detail[:300]}"
            )
        except Exception as exc:
            raise RuntimeError(f"Routes request failed: {exc}")

        return data.get("routes", [])

    def _google_walking_routes_request(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        alternatives: bool = False,
        *,
        origin_place_id: str = "",
        dest_place_id: str = "",
        variant_label: str = "",
    ) -> list[dict]:
        """Google Routes API walking directions with rich step details."""
        if not self._key:
            raise RuntimeError("No Google API key configured.")

        def _wp(lat, lon):
            return {"location": {"latLng": {"latitude": lat, "longitude": lon}}}

        def _place_wp(place_id: str):
            return {"placeId": place_id}

        body: dict = {
            "origin": _place_wp(origin_place_id) if origin_place_id else _wp(*origin),
            "destination": _place_wp(dest_place_id) if dest_place_id else _wp(*destination),
            "travelMode": "WALK",
            "computeAlternativeRoutes": alternatives,
            "polylineEncoding": "ENCODED_POLYLINE",
        }

        fields = [
            "routes.duration",
            "routes.distanceMeters",
            "routes.description",
            "routes.warnings",
            "routes.legs.duration",
            "routes.legs.distanceMeters",
            "routes.legs.startLocation",
            "routes.legs.endLocation",
            "routes.legs.steps.distanceMeters",
            "routes.legs.steps.staticDuration",
            "routes.legs.steps.startLocation",
            "routes.legs.steps.endLocation",
            "routes.legs.steps.navigationInstruction",
            "routes.legs.steps.polyline.encodedPolyline",
            "routes.polyline.encodedPolyline",
            "routes.localizedValues",
            "routes.legs.localizedValues",
            "routes.legs.steps.localizedValues",
        ]

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
            miab_log("errors", f"[RouteTools] HTTP {exc.code}: {detail}", getattr(self, "settings", None))
            raise RuntimeError(
                f"Google Routes API error {exc.code}. {detail[:300]}"
            )
        except Exception as exc:
            raise RuntimeError(f"Walking routes request failed: {exc}")

        routes = data.get("routes", [])
        if variant_label:
            for route in routes:
                route["_waypoint_variant"] = variant_label
        return routes

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
            instructions = []
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
                for step in leg.get("steps", []) or []:
                    instruction = ((step.get("navigationInstruction") or {})
                                   .get("instructions", "")).strip()
                    if not instruction:
                        continue
                    step_m = int(step.get("distanceMeters", 0) or 0)
                    if step_m:
                        instruction = f"{instruction} for {_fmt_distance(step_m)}"
                    instructions.append(instruction.rstrip(".") + ".")

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
                "instructions": instructions,
                "provider": "google",
                "supports_tolls": True,
            }

        duration_s = int(round(float(route.get("duration", 0))))
        distance_m = int(round(float(route.get("distance", 0))))
        geometry = route.get("geometry", "")
        legs = []
        instructions = []
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
            for step in leg.get("steps", []) or []:
                maneuver = step.get("maneuver") or {}
                kind = str(maneuver.get("type", "continue")).replace("_", " ")
                modifier = str(maneuver.get("modifier", "")).replace("_", " ")
                road = (step.get("name") or step.get("ref") or "the road").strip()
                if kind == "depart":
                    instruction = f"Start on {road}"
                elif kind == "arrive":
                    instruction = "Arrive at the destination"
                else:
                    action = " ".join(part for part in (kind, modifier) if part)
                    instruction = f"{action.capitalize()} onto {road}"
                step_m = int(round(float(step.get("distance", 0) or 0)))
                if step_m and kind != "arrive":
                    instruction += f" for {_fmt_distance(step_m)}"
                instructions.append(instruction.rstrip(".") + ".")
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
            "instructions": instructions,
            "provider": "osrm",
            "supports_tolls": False,
        }

    @staticmethod
    def _parse_google_walking_route(route: dict, route_num: int) -> dict:
        """Parse a Google Routes API WALK route into the shared route format."""
        def _loc_to_tuple(loc: dict) -> tuple[float | None, float | None]:
            ll = (loc or {}).get("latLng") or {}
            lat = ll.get("latitude")
            lon = ll.get("longitude")
            if lat is None or lon is None:
                return None, None
            return float(lat), float(lon)

        def _duration_s(value) -> int:
            if isinstance(value, str) and value.endswith("s"):
                try:
                    return int(round(float(value[:-1])))
                except Exception:
                    return 0
            try:
                return int(round(float(value)))
            except Exception:
                return 0

        duration_s = _duration_s(route.get("duration", 0))
        distance_m = int(round(float(route.get("distanceMeters", 0))))
        description = str(route.get("description", "")).strip()
        route_basis = str(route.get("_waypoint_variant", "")).strip()
        warnings = [str(w).strip() for w in (route.get("warnings") or []) if str(w).strip()]

        legs = []
        leg_steps = []
        leg = (route.get("legs") or [{}])[0]
        for step in leg.get("steps", []):
            nav = step.get("navigationInstruction") or {}
            text = str(nav.get("instructions", "")).strip()
            maneuver = str(nav.get("maneuver", "")).strip()
            dist_m = int(round(float(step.get("distanceMeters", 0) or 0)))
            dur_s = _duration_s(step.get("staticDuration", 0))
            start_lat, start_lon = _loc_to_tuple(step.get("startLocation") or {})
            end_lat, end_lon = _loc_to_tuple(step.get("endLocation") or {})

            if not text and maneuver:
                text = maneuver.replace("_", " ").title()
            if not text:
                continue

            dist_text = format_distance(dist_m)

            leg_steps.append({
                "lat": start_lat,
                "lon": start_lon,
                "end_lat": end_lat,
                "end_lon": end_lon,
                "instruction": text,
                "maneuver": maneuver,
                "distance_m": dist_m,
                "duration_s": dur_s,
                "distance": dist_text,
            })

        leg_distance_m = int(round(float(leg.get("distanceMeters", distance_m) or distance_m)))
        leg_duration_s = _duration_s(leg.get("duration", duration_s))
        start_lat, start_lon = _loc_to_tuple(leg.get("startLocation") or {})
        end_lat, end_lon = _loc_to_tuple(leg.get("endLocation") or {})

        leg_steps_poly = []
        for step in leg.get("steps", []):
            step_poly = (step.get("polyline") or {}).get("encodedPolyline", "")
            if step_poly:
                leg_steps_poly.extend(_decode_polyline(step_poly))

        if not leg_steps_poly:
            route_poly = (route.get("polyline") or {}).get("encodedPolyline", "")
            if route_poly:
                leg_steps_poly = _decode_polyline(route_poly)
        walk_path_points = [
            {"lat": lat, "lon": lon, "instruction": "", "maneuver": ""}
            for lat, lon in leg_steps_poly
        ]
        if not walk_path_points and start_lat is not None and start_lon is not None:
            walk_path_points = [{"lat": start_lat, "lon": start_lon, "instruction": "", "maneuver": ""}]

        walking_leg = {
            "type": "walking",
            "duration": _fmt_duration(leg_duration_s),
            "distance": _fmt_distance(leg_distance_m),
            "instructions": [
                (f"{s['instruction']} ({_fmt_distance(s['distance_m'])})"
                 if s.get("distance_m") else s["instruction"])
                for s in leg_steps
            ],
            "steps": leg_steps,
            "_walk_points": [
                {
                    "lat": s["lat"],
                    "lon": s["lon"],
                    "instruction": s["instruction"],
                    "maneuver": s["maneuver"],
                }
                for s in leg_steps
                if s.get("lat") is not None and s.get("lon") is not None
            ],
            "_walk_path_points": walk_path_points,
        }

        basis_text = f", {route_basis}" if route_basis else ""
        return {
            "summary": (f"Option {route_num}: Walk {_fmt_duration(duration_s)}, "
                        f"{_fmt_distance(distance_m)}{basis_text}"
                        + (f" via {description}" if description else "") + "."),
            "duration_text": _fmt_duration(duration_s),
            "distance_text": _fmt_distance(distance_m),
            "duration_seconds": duration_s,
            "distance_m": distance_m,
            "departure_time": "",
            "arrival_time": "",
            "departure_value": 0,
            "legs": [walking_leg],
            "transfers": 0,
            "services": ["walk"],
            "dedup_key": f"walk|{duration_s}|{distance_m}|{route.get('description','')}",
            "warnings": warnings,
            "route_basis": route_basis,
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
        request_polyline: bool = False,
    ) -> dict:
        """Single-route computation for detour calculator."""
        if not self._key:
            return self._osrm_compute_route(
                origin,
                destination,
                intermediates=intermediates,
                avoid_tolls=avoid_tolls,
                request_tolls=request_tolls,
                request_polyline=request_polyline,
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

            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
            if not data.get("routes"):
                raise RuntimeError("Google Routes API returned no routes.")
            return self._parse_route(data["routes"][0])
        except Exception as exc:
            miab_log("errors", f"[RouteTools] Google route failed, falling back to OSRM: {exc}", getattr(self, "settings", None))
            return self._osrm_compute_route(
                origin,
                destination,
                intermediates=intermediates,
                avoid_tolls=avoid_tolls,
                request_tolls=False,
                request_polyline=request_polyline,
            )

    def _osrm_compute_route(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        intermediates: Optional[list[tuple[float, float]]] = None,
        avoid_tolls: bool = False,
        request_tolls: bool = False,
        request_polyline: bool = False,
    ) -> dict:
        coords = [origin] + list(intermediates or []) + [destination]
        coord_text = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in coords)
        params = {
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
    # Suburb Lister — alternatives with suburb chain and tolls
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
            suburb_anchors: list[dict] = []
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
                suburb_anchors = self._suburb_anchors(
                    points, interval, status_cb)
                suburbs = [anchor["name"] for anchor in suburb_anchors]
                if points:
                    r["origin_offset_m"] = _haversine_m(
                        o[0], o[1], points[0][0], points[0][1])
                    r["destination_offset_m"] = _haversine_m(
                        d[0], d[1], points[-1][0], points[-1][1])

            r["suburbs"] = suburbs
            r["suburb_anchors"] = suburb_anchors
            parsed.append(r)

        # Sort by duration (fastest first)
        parsed.sort(key=lambda r: r["duration_s"])

        # Build summary text
        lines = [f"From {origin[2]} to {destination[2]}:", ""]

        total_routes = len(parsed)
        for i, r in enumerate(parsed):
            label = f"Route {i + 1} of {total_routes}"
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
            else:
                lines.append("  Through: no suburb chain was identified on this route.")
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
        *,
        origin_coords: tuple[float, float] | None = None,
        dest_coords: tuple[float, float] | None = None,
    ) -> list[dict]:
        """Single Google Directions API call for transit.

        Returns list of raw route dicts from the API response.
        """
        origin_value = (
            f"{origin_coords[0]},{origin_coords[1]}"
            if origin_coords is not None else origin_text
        )
        dest_value = (
            f"{dest_coords[0]},{dest_coords[1]}"
            if dest_coords is not None else dest_text
        )

        params: dict = {
            "origin": origin_value,
            "destination": dest_value,
            "mode": "transit",
            "alternatives": "true",
            "key": self._key,
        }
        if country_code and origin_coords is None:
            # Bias results to country by appending to addresses
            if country_code not in origin_text.upper():
                params["origin"] = f"{origin_text}, {country_code}"
        if country_code and dest_coords is None:
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

    def _walking_directions(
        self,
        origin_text: str,
        dest_text: str,
        country_code: str = "",
        *,
        origin_coords: tuple[float, float] | None = None,
        dest_coords: tuple[float, float] | None = None,
        alternatives: bool = False,
    ) -> list[dict]:
        """Google Directions API call for walking route(s)."""
        origin_value = (
            f"{origin_coords[0]},{origin_coords[1]}"
            if origin_coords is not None else origin_text
        )
        dest_value = (
            f"{dest_coords[0]},{dest_coords[1]}"
            if dest_coords is not None else dest_text
        )
        params: dict = {
            "origin": origin_value,
            "destination": dest_value,
            "mode": "walking",
            "alternatives": "true" if alternatives else "false",
            "key": self._key,
        }
        if country_code and origin_coords is None:
            if country_code not in origin_text.upper():
                params["origin"] = f"{origin_text}, {country_code}"
        if country_code and dest_coords is None:
            if country_code not in dest_text.upper():
                params["destination"] = f"{dest_text}, {country_code}"

        url = f"{self._DIRECTIONS_URL}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": "MapInABox/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
        except Exception as exc:
            raise RuntimeError(f"Walking directions request failed: {exc}")

        if data.get("status") != "OK":
            if data.get("status") == "ZERO_RESULTS":
                return []
            raise RuntimeError(
                f"Walking directions failed: {data.get('status', 'unknown')}"
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

        leg = _gd((route.get("legs") or [{}])[0])  # transit routes always have one leg

        duration_text = _gd(leg.get("duration")).get("text", "")
        dep_time = _gd(leg.get("departure_time")).get("text", "")
        arr_time = _gd(leg.get("arrival_time")).get("text", "")
        dep_value = _gd(leg.get("departure_time")).get("value", 0)

        steps = leg.get("steps", [])
        parsed_legs: list[dict] = []
        service_names: list[str] = []
        transfers = 0

        for step in steps:
            mode = step.get("travel_mode", "")

            if mode == "TRANSIT":
                td = _gd(step.get("transit_details"))
                line = _gd(td.get("line"))
                line_name = line.get("short_name") or line.get("name", "")
                vehicle_type = _gd(line.get("vehicle")).get("type", "")
                agencies = line.get("agencies") or []
                first_agency = agencies[0] if agencies else {}
                agency = _gd(first_agency).get("name", "")
                dep_stop_obj = _gd(td.get("departure_stop"))
                arr_stop_obj = _gd(td.get("arrival_stop"))
                dep_stop = dep_stop_obj.get("name", "")
                arr_stop = arr_stop_obj.get("name", "")
                dep_stop_loc = _gd(dep_stop_obj.get("location"))
                arr_stop_loc = _gd(arr_stop_obj.get("location"))
                dep_t = _gd(td.get("departure_time")).get("text", "")
                arr_t = _gd(td.get("arrival_time")).get("text", "")
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
                    "departure_stop_lat": dep_stop_loc.get("lat"),
                    "departure_stop_lon": dep_stop_loc.get("lng"),
                    "arrival_stop": arr_stop,
                    "arrival_stop_lat": arr_stop_loc.get("lat"),
                    "arrival_stop_lon": arr_stop_loc.get("lng"),
                    "departure_time": dep_t,
                    "arrival_time": arr_t,
                    "num_stops": num_stops,
                    "headsign": headsign,
                    "duration": _gd(step.get("duration")).get("text", ""),
                })

            elif mode == "WALKING":
                walk_steps: list[str] = []
                walk_points: list[dict] = []
                walk_path_points: list[dict] = []
                _TURN_MANEUVERS = frozenset({
                    "turn-left", "turn-right",
                    "turn-slight-left", "turn-slight-right",
                    "turn-sharp-left", "turn-sharp-right",
                    "uturn-left", "uturn-right",
                })
                last_path_coord = None
                for i, sub in enumerate(step.get("steps", [])):
                    instruction = _strip_html(sub.get("html_instructions", ""))
                    dist_data = _gd(sub.get("distance"))
                    dist_value = dist_data.get("value")
                    try:
                        dist = format_distance(float(dist_value))
                    except (TypeError, ValueError):
                        dist = dist_data.get("text", "")
                    if not instruction:
                        continue
                    _wm = re.match(r'^walk for (\d+(?:\.\d+)?)\s*(km?|metres?|meters?)\b',
                                   instruction, re.IGNORECASE)
                    if _wm:
                        value = float(_wm.group(1))
                        metres = value * 1000 if _wm.group(2).lower() == "km" else value
                        walk_steps.append(f"Walk for {format_distance(metres)}")
                    else:
                        walk_steps.append(f"{instruction} ({dist})" if dist else instruction)
                    loc = _gd(sub.get("start_location"))
                    slat = loc.get("lat")
                    slng = loc.get("lng")
                    maneuver = sub.get("maneuver", "")
                    if slat is not None and slng is not None:
                        if i == 0 or maneuver in _TURN_MANEUVERS:
                            walk_points.append({
                                "lat": slat,
                                "lon": slng,
                                "instruction": instruction,
                                "maneuver": maneuver,
                            })

                    step_poly = (sub.get("polyline") or {}).get("points", "")
                    path_points = _decode_polyline(step_poly) if step_poly else []
                    if not path_points and slat is not None and slng is not None:
                        path_points = [(slat, slng)]
                    for plat, plon in path_points:
                        coord_key = (round(plat, 6), round(plon, 6))
                        if coord_key == last_path_coord:
                            continue
                        walk_path_points.append({
                            "lat": plat,
                            "lon": plon,
                            "instruction": instruction,
                            "maneuver": maneuver,
                        })
                        last_path_coord = coord_key

                parsed_legs.append({
                    "type": "walking",
                    "duration": _gd(step.get("duration")).get("text", ""),
                    "distance": _gd(step.get("distance")).get("text", ""),
                    "instructions": walk_steps,
                    "_walk_points": walk_points,
                    "_walk_path_points": walk_path_points,
                })

        # Build summary line — include "walk" in order where walking legs occur
        services_parts: list[str] = []
        prev_walk = False
        for leg in parsed_legs:
            if leg["type"] == "transit":
                name = leg.get("line_name") or leg.get("vehicle_type") or ""
                if name:
                    services_parts.append(name)
                prev_walk = False
            elif leg["type"] == "walking":
                if not prev_walk:
                    services_parts.append("walk")
                prev_walk = True
        services = ", ".join(services_parts) if services_parts else "Walk"
        if services.lower() == "walk":
            services = "Walk"
        transfer_text = (f", {transfers} transfer{'s' if transfers != 1 else ''}"
                         if transfers > 0 else ", direct")
        if dep_time or arr_time:
            summary = (f"Option {route_num}: {duration_text}"
                       f", depart {dep_time}, arrive {arr_time}"
                       f". {services}{transfer_text}.")
        else:
            summary = (f"Option {route_num}: {duration_text}. "
                       f"{services}{transfer_text}.")

        # Dedup key: departure time + services used (to detect same route from two calls)
        dedup_key = f"{dep_value}|{'|'.join(service_names)}"

        return {
            "summary": summary,
            "duration_text": duration_text,
            "distance_text": _gd(leg.get("distance")).get("text", ""),
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
        has_times = bool(parsed_route.get("departure_time") or parsed_route.get("arrival_time"))
        if has_times:
            lines.append(f"Depart {parsed_route['departure_time']}"
                         f", arrive {parsed_route['arrival_time']}"
                         f", {parsed_route['duration_text']}.")
            lines.append("")
        else:
            dist = parsed_route.get("distance_text", "")
            dur = parsed_route.get("duration_text", "")
            if dist and dur:
                lines.append(f"Walk {dur}, {dist}.")
            elif dur:
                lines.append(f"Walk {dur}.")
            elif dist:
                lines.append(f"Walk {dist}.")
            if parsed_route.get("route_basis"):
                lines.append(f"Route basis: {parsed_route['route_basis']}.")
            lines.append("")

        for warning in parsed_route.get("warnings", []) or []:
            lines.append(f"Warning: {warning}.")
        if parsed_route.get("warnings"):
            lines.append("")

        for i, leg in enumerate(parsed_route["legs"]):
            if leg["type"] == "transit":
                line_desc = leg["line_name"]
                if leg["headsign"]:
                    line_desc += f" toward {leg['headsign']}"
                if leg["agency"]:
                    line_desc += f" ({leg['agency']})"
                lines.append(f"Board {line_desc}.")
                plat = leg.get("departure_platform", "")
                plat_str = f", platform {plat}" if plat else ""
                lines.append(f"  From {leg['departure_stop']}{plat_str}"
                            f" at {leg['departure_time']}.")
                lines.append(f"  To {leg['arrival_stop']}"
                            f" at {leg['arrival_time']}.")
                if leg["num_stops"]:
                    lines.append(f"  {leg['num_stops']} stops"
                                f", {leg['duration']}.")
                lines.append("")

            elif leg["type"] == "walking":
                if has_times:
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
        travel_mode: str = "transit",
        *,
        origin_coords: tuple[float, float] | None = None,
        dest_coords: tuple[float, float] | None = None,
        origin_place_id: str = "",
        dest_place_id: str = "",
    ) -> list[dict]:
        """Plan a transit journey and return parsed route options.

        Parameters
        ----------
        origin_text, dest_text:
            Raw address strings (geocoded by Directions API).
        origin_coords, dest_coords:
            Optional exact coordinates for the selected points. When provided,
            these are used for routing so Google does not re-geocode the text.
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
        travel_mode:
            "transit" for public transport itineraries, "walking" for a
            direct walking route.

        Returns list of parsed route dicts sorted by departure time.
        Each dict has: summary, duration_text, departure_time,
        arrival_time, legs, transfers, detail_text.
        """
        if not self._key:
            raise RuntimeError(
                "Journey planner needs a Google API key for transit data. "
                "For open departures and stop sequences, use the Departure Board."
            )

        travel_mode = (travel_mode or "transit").strip().lower()

        if travel_mode == "walking":
            if status_cb:
                status_cb("Searching for walking route...")
            if origin_coords is not None and dest_coords is not None:
                variants: list[tuple[str, str, str]] = [
                    ("map point route", "", ""),
                ]
                if dest_place_id:
                    variants.append(("destination access point route", "", dest_place_id))
                if origin_place_id:
                    variants.append(("origin access point route", origin_place_id, ""))
                if origin_place_id and dest_place_id:
                    variants.append(
                        ("place access point route", origin_place_id, dest_place_id)
                    )

                routes_raw = []
                errors = []
                seen_variants: set[tuple[str, str]] = set()
                for i, (label, opid, dpid) in enumerate(variants):
                    key = (opid, dpid)
                    if key in seen_variants:
                        continue
                    seen_variants.add(key)
                    if i == 1 and status_cb:
                        status_cb("Checking place access points...")
                    try:
                        routes_raw.extend(
                            self._google_walking_routes_request(
                                origin_coords,
                                dest_coords,
                                alternatives=True,
                                origin_place_id=opid,
                                dest_place_id=dpid,
                                variant_label=label,
                            )
                        )
                    except Exception as exc:
                        errors.append(exc)
                if not routes_raw and errors:
                    raise errors[0]
            else:
                routes_raw = self._walking_directions(
                    origin_text, dest_text, country_code,
                    origin_coords=origin_coords,
                    dest_coords=dest_coords,
                    alternatives=True,
                )
        else:
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
                origin_coords=origin_coords,
                dest_coords=dest_coords,
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
                        origin_coords=origin_coords,
                        dest_coords=dest_coords,
                        arrival_time=eod_ts,
                        transit_mode=mode_filter,
                    )
                    routes_raw.extend(extra)

        if not routes_raw:
            raise RuntimeError("No routes found for this journey.")

        if status_cb:
            status_cb("Processing results...")

        # Parse and deduplicate
        seen_keys: set[str] = set()
        parsed: list[dict] = []
        allowed_vehicle_types = {
            "train": {
                "RAIL", "TRAIN", "HEAVY_RAIL", "COMMUTER_TRAIN",
                "HIGH_SPEED_TRAIN", "LONG_DISTANCE_TRAIN", "METRO_RAIL",
                "SUBWAY", "TRAM", "LIGHT_RAIL", "MONORAIL",
            },
            "bus": {"BUS", "INTERCITY_BUS", "TROLLEYBUS", "SHARE_TAXI"},
            "ferry": {"FERRY"},
        }
        for raw in routes_raw:
            raw = _gd(raw)
            first_leg = _gd((raw.get("legs") or [{}])[0])
            first_step = _gd((first_leg.get("steps") or [{}])[0])
            if travel_mode == "walking" and first_step.get("navigationInstruction"):
                r = self._parse_google_walking_route(raw, 0)
            else:
                r = self._parse_transit_route(raw, 0)  # number assigned after sort
            if travel_mode != "walking" and transit_filter in allowed_vehicle_types:
                transit_legs = [leg for leg in r.get("legs", [])
                                if leg.get("type") == "transit"]
                vehicle_types = {
                    (leg.get("vehicle_type") or "").strip().upper()
                    for leg in transit_legs
                }
                allowed = allowed_vehicle_types[transit_filter]
                # The selected filter is a hard constraint, not a preference:
                # every transit leg must be of the requested kind.
                if (not transit_legs or not vehicle_types
                        or not vehicle_types.issubset(allowed)):
                    miab_log(
                        "navigation",
                        f"Journey {transit_filter}-only filter rejected route with "
                        f"transit vehicle types {sorted(vehicle_types)}.",
                        getattr(self, "settings", None),
                    )
                    continue
            if travel_mode == "walking" and origin_coords and dest_coords:
                straight_m = int(round(_haversine_m(
                    origin_coords[0], origin_coords[1], dest_coords[0], dest_coords[1]
                )))
                route_m = int(r.get("distance_m", 0) or 0)
                if straight_m > 0:
                    r["straight_line_distance_m"] = straight_m
                if route_m and straight_m and route_m > max(250, straight_m * 3):
                    note = (
                        "Route is much longer than the direct map distance "
                        f"({_fmt_distance(straight_m)}). It may be using a mapped "
                        "entrance or crossing."
                    )
                    warnings = r.setdefault("warnings", [])
                    if note not in warnings:
                        warnings.append(note)
            if r["dedup_key"] in seen_keys:
                continue
            seen_keys.add(r["dedup_key"])
            r["detail_text"] = self._build_detail_text(r)
            if travel_mode == "walking":
                r["travel_mode"] = "walking"
            parsed.append(r)

        # Sort by departure time for transit; by shortest plausible option for walks.
        if travel_mode == "walking":
            parsed.sort(key=lambda r: (
                int(r.get("duration_seconds", 0) or 0),
                int(r.get("distance_m", 0) or 0),
            ))
        else:
            parsed.sort(key=lambda r: r["departure_value"])

        # Re-number
        for i, r in enumerate(parsed):
            r["summary"] = r["summary"].replace("Option 0:", f"Option {i + 1}:")
            if travel_mode == "walking" and r.get("warnings"):
                warning = " ".join(r["warnings"])
                r["summary"] = f"{r['summary']} Warning: {warning}"

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
            miab_log("verbose", f"[HERE Transit] First station raw: {json.dumps(raw_stations[0], indent=2)[:500]}", getattr(self, "settings", None))

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
        miab_log("verbose", f"[HERE Transit] Departures URL: {url.replace(here_api_key, 'KEY')}", getattr(self, "settings", None))
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
