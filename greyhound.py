"""Greyhound Australia stop directory helpers."""

from __future__ import annotations

import json
import html
import os
import re
import time
import urllib.parse
import urllib.request
import datetime as _dt
from http.cookiejar import CookieJar

from app_paths import CACHE_DIR
from geo import dist_km
from logging_utils import miab_log

STOPS_URL = "https://book.greyhound.com.au/api/booking/options"
LIVE_TIMETABLE_URL = "https://book.greyhound.com.au/api/timetables/search"
STOP_VALIDATION_URL = "https://book.greyhound.com.au/api/stopvalidation/{origin}/{destination}"
CACHE_PATH = os.path.join(CACHE_DIR, "greyhound_stops_v2.json")
CACHE_TTL_SECONDS = 86400
# Greyhound's directory has no coordinates, so stops are geocoded with Photon.
PHOTON_URL = "https://photon.komoot.io/api/"
COORDS_CACHE_PATH = os.path.join(CACHE_DIR, "greyhound_stop_coords.json")
COORDS_CACHE_TTL_SECONDS = 30 * 86400
COORDS_MISS_TTL_SECONDS = 86400
TIMETABLE_PAGE = "https://www.greyhound.com.au/travel-information/timetables"
TIMETABLE_URL = (
    "https://www.greyhound.com.au/umbraco/surface/travel/"
    "TimetablesSearchResultsV5")
AVAILABILITY_URL = (
    "https://www.greyhound.com.au/umbraco/surface/travel/"
    "postexpressavailability")


class BookingSiteChanged(RuntimeError):
    """Greyhound's public site no longer exposes its former JSON form."""


def _verification_token(page, preferred_id=None):
    """Extract an antiforgery token without depending on attribute order."""
    for tag in re.findall(r"<input\b[^>]*>", page, flags=re.IGNORECASE):
        attrs = dict((name.casefold(), value) for name, _quote, value in re.findall(
            r"([\w:-]+)\s*=\s*([\"'])(.*?)\2", tag, flags=re.DOTALL))
        identity = (attrs.get("id", "") + " " + attrs.get("name", "")).casefold()
        if ((preferred_id and preferred_id.casefold() in identity)
                or "requestverificationtoken" in identity or "csrf" in identity):
            if attrs.get("value"):
                return attrs["value"]
    return None


def _normalise_stops(data):
    if isinstance(data, dict):
        data = data.get("Stops", data.get("stops", []))
    if not isinstance(data, list):
        raise ValueError("Greyhound returned an unexpected stop-list format")
    stops, seen = [], set()
    for item in data:
        if not isinstance(item, dict):
            continue
        if item.get("AvailableOnline", item.get("availableOnline", True)) is False:
            continue
        code = str(item.get("Code") or item.get("code") or "").strip().upper()
        description = str(item.get("Description") or item.get("description") or "").strip()
        address = str(item.get("Address") or item.get("address") or "").strip()
        if "MEAL BREAK" in description.upper() or "NO PICKUP" in address.upper():
            continue
        if code and description and code not in seen:
            seen.add(code)
            stops.append({"code": code, "description": description, "address": address,
                          "terminal": bool(item.get(
                              "IsTerminal", item.get("terminal", False)))})
    if not stops:
        raise ValueError("Greyhound returned no usable stops")
    return sorted(stops, key=lambda stop: (stop["description"].casefold(), stop["code"]))


def _read_cache(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            cached = json.load(handle)
        return _normalise_stops(cached["stops"]), float(cached.get("saved", 0))
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def _write_cache(path, stops):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump({"saved": time.time(), "stops": stops}, handle,
                  ensure_ascii=False, indent=2)
    os.replace(temporary, path)


def load_stops(cache_path=CACHE_PATH, now=None, opener=None):
    """Refresh once daily, retaining stale data when the site is unavailable."""
    now = time.time() if now is None else now
    cached = _read_cache(cache_path)
    if cached and now - cached[1] <= CACHE_TTL_SECONDS:
        return cached[0]
    request = urllib.request.Request(
        STOPS_URL, headers={"User-Agent": "Map in a Box/1.0 (accessible travel search)"})
    try:
        with (opener or urllib.request.urlopen)(request, timeout=15) as response:
            stops = _normalise_stops(json.loads(response.read().decode("utf-8-sig")))
        _write_cache(cache_path, stops)
        return stops
    except Exception as exc:
        if cached:
            miab_log("errors", f"[Greyhound] Stop refresh failed; using cache: {exc}", None)
            return cached[0]
        raise RuntimeError(f"Could not download Greyhound destinations: {exc}") from exc


def _live_time(value):
    text = str(value or "").strip()
    try:
        return _dt.datetime.strptime(text[:16], "%Y-%m-%d %H:%M")
    except ValueError:
        return None


def _spoken_live_time(value):
    return value.strftime("%I:%M%p").lstrip("0").lower()


def live_departure_rows(data, destination_name, origin_code=None,
                        destination_code=None):
    """Flatten Greyhound's current timetable response into accessible rows."""
    destination_name = re.sub(
        r"^\s*\(SFR\s*-\s*TEMP LOCAT\)\s*", "", destination_name,
        flags=re.IGNORECASE)
    rows = []
    seen = set()
    for route in (data or {}).get("Routes") or []:
        services = route.get("Services") or []
        depart = _live_time(route.get("DepartureDate"))
        arrive = _live_time(route.get("ArrivalDate"))
        if depart is None and services:
            depart = _live_time(services[0].get("DepartureDate"))
        if arrive is None and services:
            arrive = _live_time(services[-1].get("ArrivalTime"))
        if depart is None or arrive is None:
            continue
        service = html.unescape(str(route.get("RouteServiceSummary") or ", ".join(
            str(item.get("Service") or "") for item in services)).strip(", "))
        key = (depart, arrive, service)
        if key in seen:
            continue
        seen.add(key)
        fare = route.get("CheapestFare")
        if fare is None and services:
            fare = services[0].get("CheapestFare")
        label = (f"{_spoken_live_time(depart)} — Greyhound {service}, arrives at "
                 f"{destination_name.title()} at {_spoken_live_time(arrive)}")
        day_difference = (arrive.date() - depart.date()).days
        if day_difference == 1:
            label += " next day"
        elif day_difference > 1:
            label += f" {day_difference} days later"
        if isinstance(fare, (int, float)) and fare > 0:
            label += f", from ${fare:g}"
        rows.append({"operator": "Greyhound", "service": service,
                     "depart": depart.strftime("%H:%M"),
                     "arrive": arrive.strftime("%H:%M"),
                     "price": fare, "label": label, "route": route})
        rows[-1]["origin_code"] = origin_code
        rows[-1]["destination_code"] = destination_code
    return sorted(rows, key=lambda row: row["depart"])


def load_live_departures(origin, destination, day, opener=None):
    """Return live Greyhound services for one validated route and date."""
    open_url = opener or urllib.request.urlopen
    headers = {"Accept": "application/json",
               "User-Agent": "Map in a Box/1.0 (accessible coach search)"}
    try:
        valid, message = validate_route(origin, destination, opener=open_url)
        if not valid:
            message = message or "Greyhound does not operate this route"
            raise ValueError(message)
        data = load_live_timetable(origin, destination, day, opener=open_url)
        return live_departure_rows(
            data, destination["description"], origin["code"], destination["code"])
    except ValueError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Could not search Greyhound departure times: {exc}") from exc


def load_live_timetable(origin, destination, day, opener=None):
    """Return Greyhound's raw live timetable data without a pair precheck."""
    payload = json.dumps({"origin": origin["code"],
                          "destination": destination["code"],
                          "travelDate": day.isoformat()}).encode()
    request = urllib.request.Request(
        LIVE_TIMETABLE_URL, data=payload, method="POST",
        headers={"Accept": "application/json",
                 "Content-Type": "application/json",
                 "User-Agent": "Map in a Box/1.0 (accessible coach search)"})
    try:
        with (opener or urllib.request.urlopen)(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8-sig"))
    except Exception as exc:
        raise RuntimeError(f"Could not retrieve Greyhound timetable data: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("Routes"), list):
        raise ValueError("Greyhound returned an unexpected timetable format")
    return data


def stop_label(stop):
    label = f"{stop['description']} ({stop['code']})"
    address = str(stop.get("address") or "").strip()
    if address.casefold().startswith("multiple addresses for this stop"):
        address = ""
    return label + (f" — {address}" if address else "")


def stops_named_after(stops, place_names):
    """Return stops whose name is, or begins with, one of *place_names*."""
    names = {str(name).casefold().strip() for name in place_names
             if str(name or "").strip()}
    matched = []
    for stop in stops:
        description = stop["description"].casefold()
        if any(description == name or description.startswith(name + " ")
               for name in names):
            matched.append(stop)
    return matched


def _geocode_queries(stop):
    """Searches for a stop's location, most specific first."""
    description = stop["description"]
    address = str(stop.get("address") or "").strip()
    queries = []
    if address and not address.casefold().startswith("multiple addresses"):
        queries.append(address)
        first_part = address.split(",")[0].strip()
        if first_part:
            queries.append(f"{first_part}, {description}")
    queries.append(description)
    return list(dict.fromkeys(queries))


def stop_coordinates(stop, cache_path=COORDS_CACHE_PATH, now=None, opener=None):
    """Return approximate ``(lat, lon)`` for *stop*, or None when unknown.

    Photon often resolves Greyhound's addresses only to the right town, so
    treat the result as confirming the town rather than the kerbside stop.
    """
    now = time.time() if now is None else now
    try:
        with open(cache_path, "r", encoding="utf-8") as handle:
            cache = json.load(handle)
        if not isinstance(cache, dict):
            cache = {}
    except (OSError, ValueError):
        cache = {}
    entry = cache.get(stop["code"])
    if isinstance(entry, dict):
        found = entry.get("lat") is not None
        ttl = COORDS_CACHE_TTL_SECONDS if found else COORDS_MISS_TTL_SECONDS
        if now - float(entry.get("saved", 0)) <= ttl:
            return (entry["lat"], entry["lon"]) if found else None
    coords = None
    for query in _geocode_queries(stop):
        url = PHOTON_URL + "?" + urllib.parse.urlencode(
            {"q": query, "limit": 1, "lang": "en", "countrycode": "AU"})
        request = urllib.request.Request(
            url, headers={"User-Agent": "Map in a Box/1.0 (accessible travel search)"})
        try:
            with (opener or urllib.request.urlopen)(request, timeout=10) as response:
                features = json.loads(response.read().decode("utf-8")).get("features") or []
        except Exception as exc:
            # A network failure says nothing about the stop, so cache nothing.
            miab_log("errors", f"[Greyhound] Geocoding {query!r} failed: {exc}", None)
            return None
        if features:
            lon, lat = features[0]["geometry"]["coordinates"][:2]
            coords = (float(lat), float(lon))
            break
    cache[stop["code"]] = {"lat": coords[0] if coords else None,
                           "lon": coords[1] if coords else None, "saved": now}
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        temporary = cache_path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(cache, handle, indent=2)
        os.replace(temporary, cache_path)
    except OSError as exc:
        miab_log("errors", f"[Greyhound] Could not save stop locations: {exc}", None)
    return coords


def stops_near(stops, lat, lon, place_names, max_km=5.0, **coordinate_options):
    """Stops named after a nearby place that geocode within *max_km*, nearest first.

    Coach stops are often a few kilometres from a town's station, and Photon
    may place them a kilometre or more out, so this only confirms the town.
    """
    found = []
    for stop in stops_named_after(stops, place_names):
        coords = stop_coordinates(stop, **coordinate_options)
        if coords is None:
            continue
        distance = dist_km(lat, lon, coords[0], coords[1])
        if distance <= max_km:
            found.append((distance, stop))
    found.sort(key=lambda pair: pair[0])
    return [stop for _distance, stop in found]


# Greyhound's timetable search needs a destination, so services leaving a
# stop are found by searching towards each main terminus of the network.
HUB_NAMES = ("BRISBANE", "SYDNEY", "MELBOURNE", "ADELAIDE", "CANBERRA",
             "CAIRNS", "TOWNSVILLE", "ROCKHAMPTON", "MOUNT ISA",
             "ALICE SPRINGS", "DARWIN")
DEPARTURES_CACHE_SECONDS = 3600
_departures_cache = {}


def plain_site_message(value):
    """Convert a booking-site HTML response into readable dialog text."""
    text = re.sub(r"<\s*br\s*/?\s*>", "\n", str(value or ""),
                  flags=re.IGNORECASE)
    text = re.sub(r"</\s*(?:h[1-6]|p|div|li)\s*>", "\n", text,
                  flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return "\n".join(" ".join(line.split()) for line in html.unescape(text).splitlines()
                     if line.strip())


_route_validation_cache = {}
_destination_validation_cache = {}
ROUTE_VALIDATION_CACHE_SECONDS = 6 * 3600


def validate_route(origin, destination, opener=None, now=None):
    """Validate one selected Greyhound pair and briefly cache the result."""
    now = time.time() if now is None else now
    key = (origin["code"], destination["code"])
    cached = _route_validation_cache.get(key)
    if cached and now - cached[0] <= ROUTE_VALIDATION_CACHE_SECONDS:
        return cached[1], cached[2]
    url = STOP_VALIDATION_URL.format(
        origin=urllib.parse.quote(origin["code"]),
        destination=urllib.parse.quote(destination["code"]))
    request = urllib.request.Request(
        url, headers={"Accept": "application/json",
                      "User-Agent": "Map in a Box/1.0 (accessible coach search)"})
    try:
        with (opener or urllib.request.urlopen)(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8-sig"))
    except Exception as exc:
        raise RuntimeError(f"Could not validate the Greyhound route: {exc}") from exc
    if not isinstance(result, dict) or "IsValid" not in result:
        raise RuntimeError("Greyhound returned an unexpected route-validation response")
    valid = bool(result["IsValid"])
    message = plain_site_message(result.get("Message"))
    _route_validation_cache[key] = (now, valid, message)
    return valid, message


def valid_destinations(origin, stops, day, loader=None, now=None, max_workers=8,
                       cancel_event=None):
    """Return destinations present in actual timetable routes for *day*."""
    from concurrent.futures import ThreadPoolExecutor

    now = time.time() if now is None else now
    key = (origin["code"], day.isoformat())
    cached = _destination_validation_cache.get(key)
    if cached and now - cached[0] <= ROUTE_VALIDATION_CACHE_SECONDS:
        codes = cached[1]
        return [stop for stop in stops if stop["code"] in codes]
    loader = loader or load_live_timetable
    targets = [stop for stop in stops
               if stop["code"] != origin["code"]
               and (stop.get("terminal")
                    or stop["description"].upper() in HUB_NAMES)]

    def search(destination):
        if cancel_event is not None and cancel_event.is_set():
            return False, None
        try:
            return True, loader(origin, destination, day)
        except (RuntimeError, ValueError):
            return False, None

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        checked = list(pool.map(search, targets))
    if cancel_event is not None and cancel_event.is_set():
        raise RuntimeError("Greyhound destination check was cancelled")
    if checked and not any(completed for completed, _destination in checked):
        raise RuntimeError("Greyhound destinations could not be checked")
    codes = set()
    for _completed, timetable in checked:
        for route in (timetable or {}).get("Routes") or []:
            reachable = {origin["code"]}
            for service in route.get("Services") or []:
                service_stops = service.get("Stops") or []
                start = next((index for index, stop in enumerate(service_stops)
                              if str(stop.get("StopCode") or "").upper() in reachable),
                             None)
                if start is None:
                    continue
                for stop in service_stops[start + 1:]:
                    code = str(stop.get("StopCode") or "").upper()
                    if code:
                        codes.add(code)
                        reachable.add(code)
    valid = [stop for stop in stops if stop["code"] in codes]
    if all(completed for completed, _destination in checked):
        _destination_validation_cache[key] = (now, codes)
    return valid


def _stop_time(stop, field):
    value = str(stop.get(field) or "").replace("T", " ").strip()
    try:
        return _dt.datetime.strptime(value[:16], "%Y-%m-%d %H:%M")
    except ValueError:
        return None


def live_route_stop_items(row):
    """Return accessible stop items for one live Greyhound result row."""
    route = row.get("route") or {}
    origin_code = str(row.get("origin_code") or "").upper()
    destination_code = str(row.get("destination_code") or "").upper()
    items = []
    reachable = {origin_code} if origin_code else set()
    finished = False
    for service in route.get("Services") or []:
        stops = service.get("Stops") or []
        if not stops:
            continue
        if reachable:
            start = next((index for index, stop in enumerate(stops)
                          if str(stop.get("StopCode") or "").upper() in reachable),
                         None)
            if start is None:
                continue
        else:
            start = 0
        service_items = []
        for stop in stops[start:]:
            code = str(stop.get("StopCode") or "").upper()
            name = re.sub(
                r"^\s*\(SFR\s*-\s*TEMP LOCAT\)\s*", "",
                str(stop.get("StopName") or code), flags=re.IGNORECASE).title()
            arrival = _stop_time(stop, "ArrivalTime")
            departure = _stop_time(stop, "DepartureTime")
            if arrival and departure and arrival != departure:
                timing = (f"arrive {_spoken_live_time(arrival)}, "
                          f"depart {_spoken_live_time(departure)}")
            elif departure:
                timing = _spoken_live_time(departure)
            elif arrival:
                timing = _spoken_live_time(arrival)
            else:
                timing = "time unavailable"
            flags = []
            if stop.get("OnRequest"):
                flags.append("request stop")
            if stop.get("StopOver") and stop.get("StopOverTime"):
                flags.append(f"stopover {stop['StopOverTime']}")
            suffix = f", {', '.join(flags)}" if flags else ""
            service_items.append(f"{name}: {timing}{suffix}")
            if code:
                reachable.add(code)
            if destination_code and code == destination_code:
                finished = True
                break
        if service_items:
            number = str(service.get("Service") or "").strip()
            if number:
                items.append(f"Service {number}")
            if items and service_items and len(items) > 1:
                previous = items[-2] if len(items) >= 2 else ""
                if previous == service_items[0]:
                    service_items = service_items[1:]
            items.extend(service_items)
        if finished:
            break
    return items or ["No stop details were returned for this departure."]


def departures_from(stop, stops, day, loader=None, now=None):
    """Greyhound services leaving *stop* on *day*, earliest first.

    Each departure is ``{"service", "depart", "towards", "stops"}`` where
    *stops* is the service's own stop list.  Searches run in parallel and the
    combined result is cached for an hour.
    """
    from concurrent.futures import ThreadPoolExecutor

    now = time.time() if now is None else now
    key = (stop["code"], day.isoformat())
    cached = _departures_cache.get(key)
    if cached and now - cached[0] <= DEPARTURES_CACHE_SECONDS:
        return cached[1]
    loader = loader or load_timetable
    hubs = [s["code"] for s in stops
            if s["description"].upper() in HUB_NAMES and s["code"] != stop["code"]]

    def search(hub):
        try:
            return loader(stop["code"], hub, day)
        except Exception as exc:
            miab_log("errors", f"[Greyhound] Timetable {stop['code']} to {hub} failed: {exc}", None)
            return None

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(search, hubs))
    found = {}
    for result in results:
        for route in (result or {}).get("Routes") or []:
            for service in route.get("Services") or []:
                service_stops = service.get("Stops") or []
                here = next((i for i, s in enumerate(service_stops)
                             if str(s.get("StopCode") or "").upper() == stop["code"]), None)
                if here is None or here == len(service_stops) - 1:
                    continue
                depart = (_stop_time(service_stops[here], "DepartureTime")
                          or _stop_time(service_stops[here], "ArrivalTime"))
                if depart is None or depart.date() != day:
                    continue
                number = str(service.get("Service") or "").upper()
                found.setdefault((number, depart), {
                    "service": number,
                    "depart": depart,
                    "towards": str(service_stops[-1].get("StopName") or "").strip(),
                    "stops": service_stops[here:],
                })
    departures = sorted(found.values(), key=lambda d: d["depart"])
    # Don't cache a total failure: every search erroring says nothing.
    if any(result is not None for result in results):
        _departures_cache[key] = (now, departures)
    return departures


def format_departure_stops(departure):
    """Plain-text stop list for one Greyhound departure, one stop per line."""
    lines = [f"Greyhound {departure['service']} towards {departure['towards']}"]
    for stop in departure["stops"]:
        when = _stop_time(stop, "DepartureTime") or _stop_time(stop, "ArrivalTime")
        timing = when.strftime("%H:%M") if when else "time unavailable"
        flags = " (request stop)" if stop.get("OnRequest") else ""
        lines.append(f"{stop.get('StopName', '')}: {timing}{flags}")
    return "\n".join(lines)


def filter_stops(stops, query):
    words = query.casefold().strip().split()
    if not words:
        return list(stops)
    joined = " ".join(words)
    ranked = []
    for stop in stops:
        code = stop["code"].casefold()
        name = stop["description"].casefold()
        searchable = f"{name} {code} {stop.get('address', '').casefold()}"
        if not all(word in searchable for word in words):
            continue
        bucket = (0 if code == joined else 1 if name == joined else
                  2 if code.startswith(joined) else 3 if name.startswith(joined) else 4)
        ranked.append(((bucket, name, code), stop))
    ranked.sort(key=lambda pair: pair[0])
    return [stop for _rank, stop in ranked]


def load_timetable(origin_code, destination_code, travel_date, opener=None):
    """Retrieve the structured data used by Greyhound's timetable widget."""
    query = urllib.parse.urlencode({"origin": origin_code,
                                    "destination": destination_code,
                                    "date": travel_date.isoformat()})
    page_url = TIMETABLE_PAGE + "?" + query
    if opener is None:
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar())).open
    page_request = urllib.request.Request(
        page_url, headers={"User-Agent": "Map in a Box/1.0 (accessible timetable)"})
    try:
        with opener(page_request, timeout=20) as response:
            page = response.read().decode("utf-8", errors="replace")
        token = _verification_token(page, "ttw-csrf")
        if not token:
            raise ValueError("the timetable verification token was not found")
        payload = json.dumps({"origin": origin_code,
                              "destination": destination_code,
                              "travelDate": travel_date.isoformat()}).encode("utf-8")
        result_request = urllib.request.Request(
            TIMETABLE_URL, data=payload, method="POST",
            headers={"User-Agent": "Map in a Box/1.0 (accessible timetable)",
                     "Content-Type": "application/json",
                     "__RequestVerificationToken": token,
                     "Referer": page_url})
        with opener(result_request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8-sig"))
        if isinstance(result, str):
            result = json.loads(result)
        if not isinstance(result, dict) or not isinstance(result.get("Routes"), list):
            raise ValueError("Greyhound returned an unexpected timetable format")
        return result
    except Exception as exc:
        raise RuntimeError(f"Could not retrieve the Greyhound timetable: {exc}") from exc


def load_availability(values, opener=None):
    """Submit Greyhound's official availability request for one adult."""
    origin, destination = values["origin"], values["destination"]
    legs = [{"origin": origin["code"], "destination": destination["code"],
             "travelDate": values["depart"].isoformat(),
             "promotionCode": values.get("promo_code", "")}]
    if values.get("is_return"):
        legs.append({
            "origin": destination["code"], "destination": origin["code"],
            "travelDate": values["return"].isoformat(),
            "promotionCode": values.get("promo_code", "")})
    passengers = {
        "adults": 1, "concessions": 0, "children": 0, "seatedInfant": 0,
        "nursedInfant": 0, "seatedInfants": 0, "nursedInfants": 0,
        "accompaniedChildren": 0, "infants": 0, "concessionCard1": None,
        "concessionCard2": None, "concessionLater": None, "studentCard1": "",
        "studentCard2": "", "studentLater": None, "students": 0}
    payload = {
        "options": {
            "isReturn": len(legs) > 1, "nursedInfants": "0",
            "TravelLegs": legs, "currentSlide": 0, "sATypeId": None,
            "specialAssistance": False, "fareBasis1Code": "000",
            "fareBasis1Count": 1},
        "to": {"Code": destination["code"],
               "Description": destination["description"]},
        "from": {"Code": origin["code"], "Description": origin["description"]},
        "passengers": passengers}
    query = urllib.parse.urlencode({
        "ORIGIN": origin["code"], "DESTINATION": destination["code"],
        "TRAVELDATE": values["depart"].isoformat()})
    page_url = "https://www.greyhound.com.au/?" + query
    if opener is None:
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar())).open
    try:
        page_request = urllib.request.Request(
            page_url, headers={"User-Agent": "Map in a Box/1.0 (accessible booking)"})
        with opener(page_request, timeout=20) as response:
            page = response.read().decode("utf-8", errors="replace")
        token = _verification_token(page, "csrf")
        if not token:
            if "ga-booking-widget" in page:
                raise BookingSiteChanged(
                    "Greyhound now displays live times in its booking website")
            raise ValueError("the booking verification token was not found")
        request = urllib.request.Request(
            AVAILABILITY_URL, data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"User-Agent": "Map in a Box/1.0 (accessible booking)",
                     "Content-Type": "application/json",
                     "__RequestVerificationToken": token,
                     "Referer": page_url})
        with opener(request, timeout=45) as response:
            result = json.loads(response.read().decode("utf-8-sig"))
        if result.get("status") != "success":
            raise ValueError(result.get("responseMessage") or
                             "Greyhound returned no availability")
        data = result.get("data")
        if not isinstance(data, list) or not data:
            raise ValueError("Greyhound returned no available trips")
        return data
    except Exception as exc:
        raise RuntimeError(f"Could not search Greyhound availability: {exc}") from exc


def availability_options(data):
    """Flatten outbound and return availability into result rows."""
    rows = []
    show_direction = len(data) > 1
    for leg_index, leg in enumerate(data):
        direction = "Outbound" if leg_index == 0 else "Return"
        for route in leg.get("RouteOptions") or []:
            sectors = route.get("Sectors") or []
            depart = sectors[0].get("PassengerBoardingDate", "") if sectors else ""
            arrive = sectors[-1].get("PassengerArrivalDate", "") if sectors else ""
            services = route.get("BusesNumber") or ", ".join(
                f"GX{sector.get('ServiceNumber')}" for sector in sectors)
            price = route.get("CheapestFare", 0)
            direction_prefix = f"{direction}: " if show_direction else ""
            depart_text, depart_date = _spoken_booking_time(depart)
            arrive_text, arrive_date = _spoken_booking_time(arrive)
            if depart_date and arrive_date:
                days = (arrive_date - depart_date).days
                if days == 1:
                    arrive_text += " next day"
                elif days > 1:
                    arrive_text += f" {days} days later"
            rows.append({
                "direction": direction, "route": route,
                "label": (f"{direction_prefix}{services} departing {depart_text}, "
                          f"arriving {arrive_text}, from $" + f"{price:g}")})
    return rows


def _spoken_booking_time(value):
    text = str(value or "").strip()
    for fmt in ("%d/%m/%Y %I:%M:%S %p", "%d/%m/%Y %I:%M %p",
                "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            parsed = _dt.datetime.strptime(text, fmt)
            spoken = parsed.strftime("%I:%M%p").lstrip("0").lower()
            return spoken, parsed.date()
        except ValueError:
            pass
    return text or "time unavailable", None


def format_availability_option(row):
    route = row["route"]
    lines = [row["label"], "", "Fares for one adult:"]
    for fare in route.get("Fares") or []:
        status = ("sold out" if fare.get("IsSoldOut") else
                  f"{fare.get('RouteSeatsAvailable', 0)} seats")
        price = fare.get("Price", 0)
        lines.append(f"{fare.get('FareType', 'Fare')}: $" +
                     f"{price:g}; {status}.")
    lines.extend(["", "Services:"])
    for sector in route.get("Sectors") or []:
        lines.append(
            f"{sector.get('BusNumber', '')}: {sector.get('OriginName', '')}, "
            f"{sector.get('PassengerBoardingDate', '')}, to "
            f"{sector.get('DestinationName', '')}, "
            f"{sector.get('PassengerArrivalDate', '')}.")
        if sector.get("OriginAddress"):
            lines.append(f"  Boarding point: {sector['OriginAddress']}")
    return "\r\n".join(lines)


def _parse_service_time(value):
    text = str(value or "").strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return _dt.datetime.strptime(text, fmt)
        except ValueError:
            pass
    return None


def option_stop_items(row, timetable):
    """Return one accessible list item per heading and complete-route stop."""
    wanted = [
        str(sector.get("BusNumber") or
            ("GX" + str(sector.get("ServiceNumber", "")))).upper()
        for sector in row["route"].get("Sectors") or []
    ]
    wanted = list(dict.fromkeys(wanted))
    longest = {}
    for route in timetable.get("Routes") or []:
        for service in route.get("Services") or []:
            number = str(service.get("Service") or "").upper()
            if number in wanted and len(service.get("Stops") or []) > len(
                    (longest.get(number) or {}).get("Stops") or []):
                longest[number] = service
    items = []
    for number in wanted:
        service = longest.get(number)
        if not service:
            continue
        stops = service.get("Stops") or []
        first_time = next((
            parsed for stop in stops
            if (parsed := _parse_service_time(
                stop.get("DepartureTime") or stop.get("ArrivalTime")))
        ), None)
        heading = f"Stops for service {number}"
        if first_time:
            heading += f", {first_time:%A %d %B %Y}"
        items.append(heading)
        current_date = first_time.date() if first_time else None
        for stop in stops:
            arrival = _parse_service_time(stop.get("ArrivalTime"))
            departure = _parse_service_time(stop.get("DepartureTime"))
            timed = departure or arrival
            date_prefix = ""
            if timed and current_date and timed.date() != current_date:
                current_date = timed.date()
                date_prefix = f"{timed:%A %d %B}, "
            if arrival and departure and arrival.time() != departure.time():
                timing = f"arrive {arrival:%H:%M}, depart {departure:%H:%M}"
            elif departure:
                timing = f"{departure:%H:%M}"
            elif arrival:
                timing = f"{arrival:%H:%M}"
            else:
                timing = "time unavailable"
            flags = []
            if stop.get("OnRequest"):
                flags.append("request stop")
            if stop.get("StopOver"):
                flags.append(f"stopover {stop.get('StopOverTime', '')}".strip())
            suffix = f" ({'; '.join(flags)})" if flags else ""
            item = f"{stop.get('StopName', '')}: {date_prefix}{timing}{suffix}"
            if stop.get("Location"):
                item += f". {stop['Location']}"
            if stop.get("Memo"):
                item += f". Note: {' '.join(str(stop['Memo']).split())}"
            items.append(item)
    return items or ["No matching stop sequence was found."]


def format_option_stops(row, timetable):
    """Return the complete service routes as printable plain text."""
    return "\r\n".join(option_stop_items(row, timetable))


def route_label(route):
    services = route.get("RouteServiceSummary") or ", ".join(
        str(service.get("Service", "")) for service in route.get("Services", []))
    depart = str(route.get("DepartureDate") or "").replace("T", " ")
    arrive = str(route.get("ArrivalDate") or "").replace("T", " ")
    duration = route.get("TravelTimeSummary") or "duration unavailable"
    fare = route.get("CheapestFare")
    fare_text = f", from ${fare:g}" if isinstance(fare, (int, float)) else ""
    return f"{depart} to {arrive}; {services}; {duration}{fare_text}"


def format_route(route):
    """Produce a selectable, printable plain-text representation of one route."""
    lines = [str(route.get("RouteSummary") or "Greyhound timetable"),
             route_label(route), ""]
    for service in route.get("Services", []):
        lines.append(f"Service {service.get('Service', 'unknown')}")
        stops = service.get("Stops") or []
        for stop in stops:
            arrival = str(stop.get("ArrivalTime") or "").replace("T", " ")
            departure = str(stop.get("DepartureTime") or "").replace("T", " ")
            timing = (f"arrive {arrival}, depart {departure}"
                      if arrival != departure else f"{departure}")
            flags = []
            if stop.get("OnRequest"):
                flags.append("request stop")
            if stop.get("StopOver"):
                flags.append(f"stopover {stop.get('StopOverTime', '')}".strip())
            suffix = f" ({'; '.join(flags)})" if flags else ""
            lines.append(f"  {stop.get('StopName', '')}: {timing}{suffix}")
            if stop.get("Location"):
                lines.append(f"    {stop['Location']}")
            if stop.get("Memo"):
                lines.append(f"    Note: {' '.join(str(stop['Memo']).split())}")
        lines.append("")
    return "\r\n".join(lines).rstrip()
