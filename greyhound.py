"""Greyhound Australia stop directory helpers."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import urllib.request
import datetime as _dt
from http.cookiejar import CookieJar

from app_paths import CACHE_DIR
from logging_utils import miab_log

STOPS_URL = "https://www.greyhound.com.au/umbraco/api/travel/getstops"
CACHE_PATH = os.path.join(CACHE_DIR, "greyhound_stops.json")
CACHE_TTL_SECONDS = 86400
TIMETABLE_PAGE = "https://www.greyhound.com.au/travel-information/timetables"
TIMETABLE_URL = (
    "https://www.greyhound.com.au/umbraco/surface/travel/"
    "TimetablesSearchResultsV5")
AVAILABILITY_URL = (
    "https://www.greyhound.com.au/umbraco/surface/travel/"
    "postexpressavailability")


def _normalise_stops(data):
    if isinstance(data, dict):
        data = data.get("Stops", data.get("stops", []))
    if not isinstance(data, list):
        raise ValueError("Greyhound returned an unexpected stop-list format")
    stops, seen = [], set()
    for item in data:
        if not isinstance(item, dict):
            continue
        code = str(item.get("Code") or item.get("code") or "").strip().upper()
        description = str(item.get("Description") or item.get("description") or "").strip()
        address = str(item.get("Address") or item.get("address") or "").strip()
        if code and description and code not in seen:
            seen.add(code)
            stops.append({"code": code, "description": description, "address": address})
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


def stop_label(stop):
    label = f"{stop['description']} ({stop['code']})"
    address = str(stop.get("address") or "").strip()
    if address.casefold().startswith("multiple addresses for this stop"):
        address = ""
    return label + (f" — {address}" if address else "")


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
        match = re.search(
            r'id=["\']ttw-csrf["\'][^>]*value=["\']([^"\']+)', page,
            flags=re.IGNORECASE)
        if not match:
            raise ValueError("the timetable verification token was not found")
        payload = json.dumps({"origin": origin_code,
                              "destination": destination_code,
                              "travelDate": travel_date.isoformat()}).encode("utf-8")
        result_request = urllib.request.Request(
            TIMETABLE_URL, data=payload, method="POST",
            headers={"User-Agent": "Map in a Box/1.0 (accessible timetable)",
                     "Content-Type": "application/json",
                     "__RequestVerificationToken": match.group(1),
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
        match = re.search(
            r'id=["\']csrf["\'][^>]*value=["\']([^"\']+)', page,
            flags=re.IGNORECASE)
        if not match:
            raise ValueError("the booking verification token was not found")
        request = urllib.request.Request(
            AVAILABILITY_URL, data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"User-Agent": "Map in a Box/1.0 (accessible booking)",
                     "Content-Type": "application/json",
                     "__RequestVerificationToken": match.group(1),
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
