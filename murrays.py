"""Accessible Murrays timetable data for the Toowoomba express route."""

from __future__ import annotations

import datetime as _dt
import html
import json
import re
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar


# Current timetable published at murrays.com.au/express-services/
# toowoomba-brisbane-timetable.  Times are kept as 24-hour (depart, arrive)
# pairs so they sort and format predictably for a screen reader.
TOOWOOMBA_EXPRESS = {
    ("Toowoomba", "Brisbane City"): [
        ("06:00", "08:20"), ("09:00", "10:45"), ("11:00", "12:45"),
        ("14:00", "15:45"), ("17:00", "18:45")],
    ("Toowoomba", "Brisbane Domestic Airport"): [
        ("06:00", "08:50"), ("09:00", "11:15"), ("11:00", "13:15"),
        ("14:00", "16:15"), ("17:00", "19:15")],
    ("Brisbane City", "Toowoomba"): [
        ("07:00", "09:00"), ("10:00", "11:45"), ("12:00", "13:45"),
        ("14:00", "16:00"), ("18:00", "19:45")],
    ("Brisbane Domestic Airport", "Toowoomba"): [
        ("06:20", "09:00"), ("09:25", "11:45"), ("11:15", "13:45"),
        ("13:25", "16:00"), ("17:15", "19:45")],
}

TIMETABLE_URL = (
    "https://www.murrays.com.au/express-services/"
    "toowoomba-brisbane-timetable")
NETWORK_URL = "https://online.murrays.com.au/murrayscoaches/api/retrievealllocations"
ROUTE_GROUPS = ("CSE", "CW", "CN", "TBE", "MBE", "STG")
_network_cache = None


def canonical_stop_name(name):
    """Return one user-facing name for equivalent provider stop labels."""
    value = " ".join(str(name or "").upper().replace("&", " AND ").split())
    value = re.sub(r"^\(SFR\s*-\s*TEMP LOCAT\)\s*", "", value)
    value = re.sub(r"\s+(?:FROM|TO)\s+BNE$", "", value)
    value = re.sub(r"\s+\((?:DEPART|ARRIVE)\)$", "", value)
    aliases = {
        "BRISBANE": "BRISBANE CITY",
        "BRISBANE DOMESTIC": "BRISBANE DOMESTIC AIRPORT",
        "SYDNEY": "SYDNEY CENTRAL",
        "SYDNEY AIRPORT INTERNATIONAL": "SYDNEY AIRPORT",
        "SYDNEY INTERNATIONAL AIRPORT": "SYDNEY AIRPORT",
        "CANBERRA JOLIMONT CENTRE": "CANBERRA",
    }
    return aliases.get(value, value)


def load_network(opener=None):
    """Load every currently bookable Murrays origin/destination pair."""
    global _network_cache
    if _network_cache is not None and opener is None:
        return list(_network_cache)
    open_url = opener or urllib.request.urlopen
    def retrieve(group):
        url = NETWORK_URL + "?" + urllib.parse.urlencode({"LineCode": group})
        request = urllib.request.Request(
            url, headers={"User-Agent": "Map in a Box/1.0 (accessible coach search)"})
        with open_url(request, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8-sig"))
        if not isinstance(data, list):
            raise ValueError("Murrays returned an unexpected route directory")
        return group, data

    if opener is None:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=len(ROUTE_GROUPS)) as pool:
            responses = list(pool.map(retrieve, ROUTE_GROUPS))
    else:
        responses = [retrieve(group) for group in ROUTE_GROUPS]
    pairs = {}
    for group, data in responses:
        for item in data:
            origin = str(item.get("FromLocationName") or "").strip()
            destination = str(item.get("ToLocationName") or "").strip()
            origin_code = str(item.get("FromLocationCode") or "").strip()
            destination_code = str(item.get("ToLocationCode") or "").strip()
            if origin and destination and origin_code and destination_code:
                pairs.setdefault((origin, destination), {
                    "group": group, "route_code": str(item.get("RouteCode") or group),
                    "origin": origin, "origin_code": origin_code,
                    "destination": destination, "destination_code": destination_code,
                })
    result = sorted(pairs.values(), key=lambda row: (row["origin"], row["destination"]))
    if opener is None:
        _network_cache = result
    return result


def origins(network):
    return sorted({canonical_stop_name(row["origin"]) for row in network})


def destinations(network, origin):
    wanted = canonical_stop_name(origin)
    return sorted({canonical_stop_name(row["destination"]) for row in network
                   if canonical_stop_name(row["origin"]) == wanted})


def find_route(network, origin, destination):
    wanted_origin = canonical_stop_name(origin)
    wanted_destination = canonical_stop_name(destination)
    return next((row for row in network
                 if canonical_stop_name(row["origin"]) == wanted_origin
                 and canonical_stop_name(row["destination"]) == wanted_destination), None)


def _parse_live_departures(page, destination):
    """Extract Murrays' live departure/arrival rows from trip-selection HTML."""
    page = html.unescape(page)
    found = {}
    for match in re.finditer(
            r"<tr\s+class=['\"]?trChooseTime['\"]?[^>]*>(.*?)</tr>",
            page, flags=re.IGNORECASE | re.DOTALL):
        times = re.findall(r"<time[^>]*>\s*([^<]+?)\s*</time>", match.group(1),
                           flags=re.IGNORECASE)
        if len(times) < 2:
            continue
        try:
            depart = _dt.datetime.strptime(times[0].strip().upper(), "%I:%M%p").strftime("%H:%M")
            arrive = _dt.datetime.strptime(times[1].strip().upper(), "%I:%M%p").strftime("%H:%M")
        except ValueError:
            continue
        prices = [int(value) for value in re.findall(
            r"<sup>\$</sup>\s*(\d+)", match.group(1), flags=re.IGNORECASE)]
        price = min(prices) if prices else None
        label = (f"{_spoken_time(depart)} — Murrays, arrives at "
                 f"{destination.title()} at {_spoken_time(arrive)}")
        if price is not None:
            label += f", from ${price}"
        found.setdefault((depart, arrive), {
            "operator": "Murrays", "depart": depart, "arrive": arrive,
            "price": price, "label": label})
    return sorted(found.values(), key=lambda row: row["depart"])


def load_live_departures(route, day, opener=None):
    """Submit Murrays' public one-adult search and return live trip times."""
    if route is None:
        return []
    if opener is None:
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar())).open
    base = "https://online.murrays.com.au/murrayscoaches/booking/dates"
    page_url = base + "?" + urllib.parse.urlencode({"RouteCode": route["group"]})
    headers = {"User-Agent": "Map in a Box/1.0 (accessible coach search)"}
    try:
        with opener(urllib.request.Request(page_url, headers=headers), timeout=20) as response:
            page = response.read().decode("utf-8", errors="replace")
        token_match = re.search(
            r'name=["\']__RequestVerificationToken["\'][^>]*value=["\']([^"\']+)',
            page, flags=re.IGNORECASE)
        if not token_match:
            raise ValueError("Murrays verification token was not found")
        values = {
            "__RequestVerificationToken": token_match.group(1),
            "RouteCode": route["group"], "Direction": "OneWay",
            "ddOrigin": route["origin_code"], "ddOriginText": route["origin"],
            "ddDestination": route["destination_code"],
            "ddDestinationText": route["destination"],
            "DepartureDate": day.strftime("%d/%m/%Y"), "ReturningDate": "",
            "txtAdult": "1", "txtStudent": "0", "txtConcession": "0",
            "txtPensioner": "0", "txtSenior": "0", "txtChild": "0",
            "txtInfant": "0", "seat-choice": "0",
        }
        request = urllib.request.Request(
            base, data=urllib.parse.urlencode(values).encode(), headers=headers)
        with opener(request, timeout=45) as response:
            result_page = response.read().decode("utf-8", errors="replace")
        return _parse_live_departures(result_page, route["destination"])
    except Exception as exc:
        raise RuntimeError(f"Could not search Murrays departure times: {exc}") from exc

def routes():
    return list(TOOWOOMBA_EXPRESS)


def _spoken_time(value):
    return _dt.datetime.strptime(value, "%H:%M").strftime("%I:%M%p").lstrip("0").lower()


def departures(origin, destination, day):
    """Return accessible labels for the daily Toowoomba express timetable."""
    return [
        {"depart": depart, "arrive": arrive,
         "label": (f"{_spoken_time(depart)} departure, arrives at "
                   f"{destination} at {_spoken_time(arrive)}")}
        for depart, arrive in TOOWOOMBA_EXPRESS.get((origin, destination), [])
    ]

