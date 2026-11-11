"""Premier Motor Service public timetable and live-search helpers."""

from __future__ import annotations

import datetime as _dt
import html
import json
import os
import re
import time
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

from app_paths import CACHE_DIR


OPTIONS_URL = "https://cloud.itmprojects.com.au/ws/Kiosk/KioskOptions"
TIMETABLE_URL = (
    "https://cloud.itmprojects.com.au/shuttles/Shuttles/"
    "TimetablePartial?cid=PMS&service={service}")
PRELOAD_URL = "https://cloud.itmprojects.com.au/shuttles/Shuttles/Preload"
SERVICES_URL = "https://cloud.itmprojects.com.au/shuttles/Shuttles/GetServices"
BOOKING_URL = "https://premierms.com.au/book-tickets/"
LONG_DISTANCE_SERVICES = ("PM1", "PM2", "PM11", "PM12", "PM16", "PM19")
CACHE_PATH = os.path.join(CACHE_DIR, "premier_network.json")
CACHE_SECONDS = 86400


def _text(fragment):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def _normal_name(value):
    value = " ".join(str(value or "").upper().replace("&", " AND ").split())
    value = re.sub(r"\s*\([^)]*\)\s*", " ", value)
    value = re.sub(r"\b(?:CENTRAL )?(?:RAILWAY )?STATION\b", "", value)
    value = re.sub(r"\b(?:TURN OFF|TURN-OFF)\b", "", value)
    value = re.sub(r"[^A-Z0-9]+", " ", value)
    aliases = {
        "SYDNEY": "SYDNEY CENTRAL",
        "MISSION WONGALING BEACH": "MISSION BEACH",
        "SOUTH GRAFTON": "GRAFTON",
    }
    value = " ".join(value.split())
    return aliases.get(value, value)


def _parse_options(data):
    if not isinstance(data, dict) or data.get("status") != 0:
        raise ValueError("Premier returned an unexpected stop-directory response")
    stops = []
    seen = set()
    for item in data.get("stops") or []:
        code = str(item.get("code") or "").strip().upper()
        name = " ".join(str(item.get("description") or "").split())
        if code and name and code not in seen:
            seen.add(code)
            stops.append({"code": code, "description": name,
                          "message1": str(item.get("message1") or "").strip(),
                          "message2": str(item.get("message2") or "").strip()})
    if not stops:
        raise ValueError("Premier returned no booking stops")
    return stops


def _timetable_rows(page):
    rows = []
    for match in re.finditer(r"<tr\b[^>]*>(.*?)</tr>", page,
                             flags=re.IGNORECASE | re.DOTALL):
        cells = re.findall(r"<td\b[^>]*>(.*?)</td>", match.group(1),
                           flags=re.IGNORECASE | re.DOTALL)
        values = [_text(cell) for cell in cells]
        if len(values) >= 2 and re.fullmatch(r"\d{1,2}:\d{2}", values[0]):
            rows.append({"time": values[0], "name": values[1],
                         "address": values[2] if len(values) > 2 else ""})
    if not rows:
        raise ValueError("Premier returned no timetable stops")
    return rows


def _match_stop(name, stops):
    wanted = _normal_name(name)
    exact = [stop for stop in stops if _normal_name(stop["description"]) == wanted]
    if exact:
        return exact[0]
    candidates = [stop for stop in stops
                  if wanted.startswith(_normal_name(stop["description"]))
                  or _normal_name(stop["description"]).startswith(wanted)]
    return candidates[0] if len(candidates) == 1 else None


def _download_network(opener=None):
    open_url = opener or urllib.request.urlopen
    request = urllib.request.Request(
        OPTIONS_URL, data=json.dumps({"client": "PMS"}).encode(), method="POST",
        headers={"Content-Type": "application/json; charset=utf-8",
                 "User-Agent": "Map in a Box/1.0 (accessible coach search)"})
    with open_url(request, timeout=30) as response:
        stops = _parse_options(json.loads(response.read().decode("utf-8-sig")))
    routes = []
    for service in LONG_DISTANCE_SERVICES:
        request = urllib.request.Request(
            TIMETABLE_URL.format(service=service),
            headers={"User-Agent": "Map in a Box/1.0 (accessible coach search)"})
        with open_url(request, timeout=30) as response:
            timetable = _timetable_rows(response.read().decode("utf-8", errors="replace"))
        mapped = []
        for item in timetable:
            stop = _match_stop(item["name"], stops)
            if stop is None:
                raise ValueError(
                    f"Premier stop code was not found for {item['name']} on {service}")
            mapped.append({**item, "code": stop["code"],
                           "description": stop["description"]})
        routes.append({"service": service, "stops": mapped})
    return routes


def load_network(cache_path=CACHE_PATH, now=None, opener=None):
    """Load Premier's six long-distance timetable sequences."""
    now = time.time() if now is None else now
    try:
        with open(cache_path, "r", encoding="utf-8") as handle:
            cached = json.load(handle)
        if now - float(cached.get("saved", 0)) <= CACHE_SECONDS:
            return cached["routes"]
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        cached = None
    try:
        routes = _download_network(opener=opener)
        if opener is None:
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            temporary = cache_path + ".tmp"
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump({"saved": now, "routes": routes}, handle, indent=2)
            os.replace(temporary, cache_path)
        return routes
    except Exception as exc:
        if cached and cached.get("routes"):
            return cached["routes"]
        raise RuntimeError(f"Could not load Premier routes: {exc}") from exc


def origins(routes):
    return sorted({stop["description"] for route in routes
                   for stop in route["stops"][:-1]})


def destinations(routes, origin):
    found = set()
    for route in routes:
        indexes = [i for i, stop in enumerate(route["stops"])
                   if stop["description"] == origin]
        for index in indexes:
            found.update(stop["description"] for stop in route["stops"][index + 1:])
    return sorted(found)


def _route_for(routes, origin_code, destination_code, service=None):
    base = str(service or "").split("-", 1)[0].upper()
    for route in routes:
        if base and route["service"] != base:
            continue
        codes = [stop["code"] for stop in route["stops"]]
        if origin_code in codes and destination_code in codes:
            if codes.index(origin_code) < codes.index(destination_code):
                return route
    return None


def _form_payload(origin, destination, day):
    payload = {
        "cID": "PMS", "type": "EXPRESS",
        "ForwardPickup": origin["code"], "ForwardDropOff": destination["code"],
        "ForwardDate": day.strftime("%d/%m/%Y"), "ReturnDate": "",
    }
    for index, code in enumerate(("000", "010", "011", "030", "020")):
        payload[f"Tickets[{index}].Key"] = code
        payload[f"Tickets[{index}].Value"] = "1" if index == 0 else "0"
    return payload


def _hidden_token(page):
    match = re.search(
        r'name=["\']__RequestVerificationToken["\'][^>]*value=["\']([^"\']+)',
        page, flags=re.IGNORECASE)
    if not match:
        raise ValueError("Premier booking verification token was not found")
    return html.unescape(match.group(1))


def _service_rows(page, origin, destination, day, routes):
    rows = []
    for match in re.finditer(r"<tr\b[^>]*>(.*?)</tr>", page,
                             flags=re.IGNORECASE | re.DOTALL):
        fragment = match.group(1)
        service_match = re.search(
            r'name=["\']ForwardService["\'][^>]*value=["\']([^"\']+)',
            fragment, flags=re.IGNORECASE)
        if not service_match:
            continue
        service = html.unescape(service_match.group(1)).strip()
        def cell(class_name):
            found = re.search(
                rf'<td\b[^>]*class=["\'][^"\']*\b{class_name}\b[^"\']*["\'][^>]*>'
                r'(.*?)</td>', fragment, flags=re.IGNORECASE | re.DOTALL)
            return _text(found.group(1)) if found else ""
        depart_text, arrive_text = cell("Pickup"), cell("DropOff")
        fare_match = re.search(r"\$\s*([\d,.]+)", cell("Fare"))
        try:
            depart_time = _dt.datetime.strptime(depart_text, "%I:%M %p").time()
            arrive_time = _dt.datetime.strptime(arrive_text, "%I:%M %p").time()
        except ValueError:
            continue
        depart = _dt.datetime.combine(day, depart_time)
        arrive = _dt.datetime.combine(day, arrive_time)
        if arrive <= depart:
            arrive += _dt.timedelta(days=1)
        fare = float(fare_match.group(1).replace(",", "")) if fare_match else None
        label = (f"{depart.strftime('%I:%M%p').lstrip('0').lower()} — Premier "
                 f"{service}, arrives at {destination['description'].title()} at "
                 f"{arrive.strftime('%I:%M%p').lstrip('0').lower()}")
        if arrive.date() > depart.date():
            label += " next day"
        if fare and fare > 0:
            label += f", from ${fare:g}"
        rows.append({"operator": "Premier", "service": service,
                     "depart": depart.strftime("%H:%M"),
                     "arrive": arrive.strftime("%H:%M"), "price": fare,
                     "label": label,
                     "route": _route_for(routes, origin["code"],
                                         destination["code"], service),
                     "origin_code": origin["code"],
                     "destination_code": destination["code"]})
    return rows


def load_live_departures(origin, destination, day, routes, opener=None):
    """Return live Premier services and one-adult fares."""
    if opener is None:
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar())).open
    headers = {"User-Agent": "Map in a Box/1.0 (accessible coach search)"}
    preload = urllib.request.Request(
        PRELOAD_URL, data=urllib.parse.urlencode(
            _form_payload(origin, destination, day)).encode(), method="POST",
        headers={**headers, "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with opener(preload, timeout=45) as response:
            page = response.read().decode("utf-8", errors="replace")
        token = _hidden_token(page)
        form = {
            "__RequestVerificationToken": token, "client": "PMS",
            "tickets[000]": "1", "tickets[010]": "0", "tickets[011]": "0",
            "tickets[030]": "0", "tickets[020]": "0",
            "forwardDate": day.strftime("%d/%m/%Y"),
            "forwardPickup": origin["code"],
            "forwardDropOff": destination["code"],
            "forwardPickupCache": origin["code"],
            "forwardDropOffCache": destination["code"],
            "returnDate": "", "returnPickup": destination["code"],
            "returnDropOff": origin["code"],
            "returnPickupCache": destination["code"],
            "returnDropOffCache": origin["code"],
            "isToAirport": "false", "isReturn": "false",
            "wantsValet": "false", "Wheelchair": "false",
        }
        request = urllib.request.Request(
            SERVICES_URL, data=urllib.parse.urlencode(form).encode(), method="POST",
            headers={**headers, "Content-Type": "application/x-www-form-urlencoded",
                     "X-Requested-With": "XMLHttpRequest",
                     "Referer": "https://cloud.itmprojects.com.au/shuttles/Shuttles"})
        with opener(request, timeout=45) as response:
            result = response.read().decode("utf-8", errors="replace")
        return _service_rows(result, origin, destination, day, routes)
    except (ValueError, RuntimeError):
        raise
    except Exception as exc:
        raise RuntimeError(f"Could not search Premier departure times: {exc}") from exc


def route_stop_items(row):
    route = row.get("route")
    if not route:
        return ["No Premier stop details were returned for this departure."]
    stops = route["stops"]
    codes = [stop["code"] for stop in stops]
    try:
        start = codes.index(row["origin_code"])
        end = codes.index(row["destination_code"], start + 1)
    except ValueError:
        return ["No Premier stop details were returned for this departure."]
    items = [f"Service {row['service']}"]
    day_offset = 0
    previous_minutes = None
    for stop in stops[start:end + 1]:
        hour, minute = (int(part) for part in stop["time"].split(":"))
        minutes = hour * 60 + minute
        if previous_minutes is not None and minutes < previous_minutes:
            day_offset += 1
        previous_minutes = minutes
        when = (_dt.datetime(2000, 1, 1, hour, minute)
                + _dt.timedelta(days=day_offset))
        label = f"{stop['description'].title()}: {when.strftime('%I:%M%p').lstrip('0').lower()}"
        if day_offset:
            label += " next day" if day_offset == 1 else f" {day_offset} days later"
        if stop.get("address"):
            label += f". {stop['address']}"
        items.append(label)
    return items
