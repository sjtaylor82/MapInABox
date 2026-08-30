"""Accessible Rome2Rio journey summaries parsed from public route pages.

Rome2Rio is used for broad, long-distance multimodal discovery.  The overview
cards describe useful combinations of planes, trains, coaches and driving, but
they are not a live booking feed and may omit individual flight connections.
"""

from __future__ import annotations

import json
import datetime
import os
import re
import time
import urllib.parse
from dataclasses import dataclass

from bs4 import BeautifulSoup


_CACHE_TTL_SECONDS = 7 * 86400
_PUBLIC_API_KEY = "jGq3Luw3"
_DURATION_RE = re.compile(
    r"^(?:\d+\s*(?:d|days?)\s*)?(?:\d+\s*(?:h|hours?)\s*)?"
    r"(?:\d+\s*(?:m|mins?|minutes?))?$",
    re.IGNORECASE,
)
_PRICE_RE = re.compile(r"^(?:A?\$|£|€|¥)\s*[\d,.]+(?:\s*[–-]\s*(?:A?\$|£|€|¥)?\s*[\d,.]+)?$")
_ROUTE_CODE_RE = re.compile(r"^[A-Z]{3}\s*[-–]\s*[A-Z]{3}$")
_SERVICE_ID_RE = re.compile(r"^[A-Za-z0-9]{1,8}$")
_MODE_WORDS = {
    "plane", "bus", "train", "car", "ferry", "tram", "subway",
    "walk", "taxi", "rideshare",
}
_STEP_PREFIXES = (
    "fly ", "take ", "drive ", "walk ", "travel ", "catch ",
)


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _slug(value: str) -> str:
    value = _clean(value).replace(" ", "-")
    return urllib.parse.quote(value, safe="-(),")


def _match_text(value: str) -> str:
    """Normalise route prose for matching steps to expandable detail blocks."""
    value = re.sub(r"\([A-Z]{3}\)", "", value or "")
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def route_url(origin: str, destination: str) -> str:
    return f"https://www.rome2rio.com/s/{_slug(origin)}/{_slug(destination)}"


def add_rome2rio_upcoming_flight(
        routes: list[dict], api_key: str, request_get,
        departure_date: datetime.date | None = None) -> bool:
    """Add Rome2Rio's next-day flight detail using its public schedule data."""
    target = None
    origin_name = destination_name = ""
    for route in routes:
        match = re.search(
            r"Fly from (.+?)\s*\([A-Z]{3}\) to (.+?)\s*\([A-Z]{3}\)",
            route.get("detail_text", ""), re.IGNORECASE)
        if match:
            target = route
            origin_name, destination_name = map(_clean, match.groups())
            break
    if target is None or not api_key:
        return False

    day = departure_date or (datetime.date.today() + datetime.timedelta(days=1))
    response = request_get(
        "https://www.rome2rio.com/api/1.5/json/flightSchedules",
        params={
            "key": api_key,
            "oName": origin_name,
            "dName": destination_name,
            "languageCode": "en",
            "currencyCode": "AUD",
            "oDateTime": day.isoformat() + "T00:00:00",
            "paxAges": "18",
        },
        impersonate="edge101", timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    itineraries = data.get("outboundItineraries") or []
    legs = data.get("legs") or []
    hops = data.get("hops") or []
    lines = data.get("lines") or []
    places = data.get("places") or []
    carriers = data.get("carriers") or []
    layovers = data.get("layovers") or []

    choices = []
    seen_itineraries = set()
    for itinerary in itineraries:
        leg_indexes = itinerary.get("legs") or []
        if not leg_indexes or leg_indexes[0] >= len(legs):
            continue
        leg = legs[leg_indexes[0]]
        hop_indexes = leg.get("hops") or []
        if not hop_indexes or any(index >= len(hops) for index in hop_indexes):
            continue
        signature = tuple(
            (hops[index].get("marketingCarrier"), hops[index].get("name"),
             hops[index].get("departureDate"), hops[index].get("departureTime"),
             hops[index].get("arrivalDate"), hops[index].get("arrivalTime"))
            for index in hop_indexes)
        if signature in seen_itineraries:
            continue
        seen_itineraries.add(signature)
        first = hops[hop_indexes[0]]
        choices.append((0 if len(hop_indexes) == 1 else 1,
                        first.get("departureTime", "99:99"), leg))
    if not choices:
        return False
    choices.sort(key=lambda item: (item[0], item[1]))
    flight_lines = [f"Flight schedules for {day.strftime('%A, %d %B %Y')}:"]
    for option_number, (_, _, leg) in enumerate(choices, 1):
        hop_indexes = leg.get("hops") or []
        layover_indexes = leg.get("layovers") or []
        flight_lines.extend([
            "",
            f"Option {option_number}.",
            "Non-stop." if len(hop_indexes) == 1
            else f"{len(hop_indexes) - 1} change{'s' if len(hop_indexes) > 2 else ''}.",
        ])
        for position, hop_index in enumerate(hop_indexes):
            hop = hops[hop_index]
            line = lines[hop.get("line", -1)]
            place_indexes = line.get("places") or []
            origin = places[place_indexes[0]] if len(place_indexes) > 0 else {}
            destination = places[place_indexes[1]] if len(place_indexes) > 1 else {}
            marketing_index = hop.get("marketingCarrier", -1)
            operating_index = hop.get("operatingCarrier", marketing_index)
            marketing = carriers[marketing_index] if 0 <= marketing_index < len(carriers) else {}
            operating = carriers[operating_index] if 0 <= operating_index < len(carriers) else marketing
            carrier_text = marketing.get("name", "")
            code = marketing.get("code", "")
            number = hop.get("name", "")
            if operating.get("name") and operating.get("name") != carrier_text:
                carrier_text += f", operated by {operating['name']}"
            flight_lines.append(
                f"{carrier_text} {code}{number}: "
                f"{origin.get('code', origin.get('name', ''))} {hop.get('departureTime', '')} "
                f"to {destination.get('code', destination.get('name', ''))} "
                f"{hop.get('arrivalTime', '')}."
            )
            if position < len(layover_indexes):
                layover_index = layover_indexes[position]
                if 0 <= layover_index < len(layovers):
                    minutes = int(layovers[layover_index].get("duration", 0))
                    flight_lines.append(
                        f"Transfer at {destination.get('code', destination.get('name', ''))}: "
                        f"{minutes // 60}h {minutes % 60}m."
                    )
        duration = int(leg.get("duration", 0))
        flight_lines.append(
            f"Total duration: {duration // 60}h {duration % 60}m.")

    marker = "\n\nThis is a broad Rome2Rio estimate"
    addition = "\n".join(flight_lines)
    detail = target.get("detail_text", "")
    target["detail_text"] = detail.replace(
        marker, f"\n\n{addition}{marker}", 1) if marker in detail else detail + "\n\n" + addition
    return True


def add_rome2rio_flights_for_all_routes(
        routes: list[dict], api_key: str, request_get,
        departure_date: datetime.date | None = None) -> int:
    """Add dated schedules to every flight route that Rome2Rio returned."""
    added = 0
    for route in routes:
        try:
            if add_rome2rio_upcoming_flight(
                    [route], api_key, request_get, departure_date):
                added += 1
        except Exception:
            continue
    return added


def parse_routes(html: bytes | str, page_url: str) -> list[dict]:
    """Parse Rome2Rio overview cards into JourneyResultsDialog dictionaries."""
    soup = BeautifulSoup(html, "html.parser")
    parsed: list[dict] = []
    seen = set()
    operator_paragraphs: list[str] = []
    flight_details: list[dict] = []
    transit_details: list[dict] = []
    operator_section = soup.find(id="route-operators")
    if operator_section is not None:
        for paragraph in operator_section.find_all("p"):
            text = _clean(paragraph.get_text(" ", strip=True))
            # Operator summaries are prose. Photo credits and captions lower
            # in the same section do not contain a transport relationship.
            if text and re.search(r"\b(?:operates?|fly|flies|runs?)\b", text,
                                  re.IGNORECASE):
                operator_paragraphs.append(text)

    # Flight timings, operating days and prices are commonly hidden in an
    # expandable operator panel rather than repeated in route-operator prose.
    for button in soup.find_all(attrs={"data-action": "Expand:Operator"}):
        operator = _clean(button.get("data-label", ""))
        controls = button.get("aria-controls", "")
        panel = soup.find(id=controls) if controls else None
        if not operator or panel is None:
            continue
        for heading_node in panel.find_all("h5"):
            heading = _clean(heading_node.get_text(" ", strip=True))
            transit_match = re.match(
                r"(Bus|Train|Ferry|Tram) from (.+?) to (.+?)$", heading,
                re.IGNORECASE)
            match = re.match(
                r"Flights? from (.+?) to (.+?)(?: via .+)?$", heading,
                re.IGNORECASE)
            if not match and not transit_match:
                continue
            values = {}
            details = heading_node.find_next("dl")
            if details is not None:
                terms = details.find_all(["dt", "dd"])
                for index in range(0, len(terms) - 1, 2):
                    key = _clean(terms[index].get_text(" ", strip=True))
                    value = _clean(terms[index + 1].get_text(" ", strip=True))
                    if key and value:
                        values[key.lower()] = value
            record = {
                "operator": operator,
                "heading": heading,
                "values": values,
            }
            if match:
                record.update({
                    "origin": _match_text(match.group(1)),
                    "destination": _match_text(match.group(2)),
                })
                flight_details.append(record)
            else:
                record.update({
                    "mode": transit_match.group(1).lower(),
                    "origin": _match_text(transit_match.group(2)),
                    "destination": _match_text(transit_match.group(3)),
                })
                transit_details.append(record)

    for heading_node in soup.find_all("h3"):
        heading = _clean(heading_node.get_text(" ", strip=True))
        if not heading or heading in seen:
            continue
        card = heading_node.find_parent("a")
        if card is None:
            continue
        action = card.get("data-action", "")
        label = card.get("data-label", "")
        if action != "Click:RouteLink" and not label:
            continue

        strings = [_clean(text) for text in card.stripped_strings]
        strings = [text for text in strings if text]
        badge = ""
        duration = ""
        price = ""
        steps: list[str] = []
        identifiers: list[str] = []
        for text in strings[1:]:
            lower = text.lower()
            if lower in ("best", "cheapest", "recommended") and not badge:
                badge = lower
            elif _DURATION_RE.fullmatch(text) and any(ch.isdigit() for ch in text):
                duration = duration or text
            elif _PRICE_RE.fullmatch(text):
                price = price or text
            elif lower in _MODE_WORDS:
                continue
            elif (_ROUTE_CODE_RE.fullmatch(text) or _SERVICE_ID_RE.fullmatch(text)):
                if text not in identifiers:
                    identifiers.append(text)
            elif lower.startswith(_STEP_PREFIXES) and text not in steps:
                steps.append(text)

        badge_text = f", {badge}" if badge else ""
        summary_parts = [f"Option {len(parsed) + 1}{badge_text}: {heading}."]
        if duration:
            summary_parts.append(duration + ".")
        if price:
            summary_parts.append(f"Estimated {price}.")

        detail = [heading + "."]
        if badge:
            detail.append(f"Rome2Rio marks this option as {badge}.")
        if duration:
            detail.append(f"Estimated journey time: {duration}.")
        if price:
            detail.append(f"Estimated price: {price}.")
        if steps:
            detail.extend(["", "Overview:"])
            detail.extend(f"{index}. {step}." for index, step in enumerate(steps, 1))
        if identifiers:
            detail.extend(["", "Route and service identifiers: "
                           + ", ".join(identifiers) + "."])
        service_info = ""
        for step in steps:
            signature = re.sub(r"^(?:Take|Catch)\s+the\s+", "", step,
                               flags=re.IGNORECASE).lower()
            if len(signature) > 8:
                service_info = next(
                    (paragraph for paragraph in operator_paragraphs
                     if signature in paragraph.lower()), "")
            if service_info:
                break
        matched_flights = []
        for step in steps:
            if not step.lower().startswith("fly "):
                continue
            normal_step = _match_text(step)
            for flight in flight_details:
                if (flight["origin"] in normal_step
                        and flight["destination"] in normal_step):
                    values = flight["values"]
                    parts = [f'{flight["operator"]}: {flight["heading"]}.']
                    if values.get("ave. duration"):
                        parts.append(
                            f'Average flight duration {values["ave. duration"]}.')
                    if values.get("when"):
                        parts.append(f'Operates {values["when"]}.')
                    if values.get("estimated price"):
                        parts.append(
                            f'Estimated flight price {values["estimated price"]}.')
                    text = " ".join(parts)
                    if text not in matched_flights:
                        matched_flights.append(text)
        if matched_flights:
            detail.extend(["", "Flights mentioned:"])
            detail.extend(matched_flights)
        matched_services = []
        for step in steps:
            normal_step = _match_text(step)
            step_mode = next(
                (mode for mode in ("bus", "train", "ferry", "tram")
                 if mode in normal_step.split()), "")
            if not step_mode:
                continue
            for service in transit_details:
                if (service["mode"] == step_mode
                        and service["origin"] in normal_step
                        and service["destination"] in normal_step):
                    values = service["values"]
                    parts = [f'{service["operator"]}: {service["heading"]}.']
                    if values.get("ave. duration"):
                        parts.append(
                            f'Average duration {values["ave. duration"]}.')
                    if values.get("frequency"):
                        parts.append(f'Frequency {values["frequency"]}.')
                    if values.get("estimated price"):
                        parts.append(
                            f'Estimated price {values["estimated price"]}.')
                    text = " ".join(parts)
                    if text not in matched_services:
                        matched_services.append(text)
        if matched_services:
            detail.extend(["", "Services mentioned:"])
            detail.extend(matched_services)
        if service_info:
            # General Rome2Rio prose often combines direct and connecting
            # airlines under one origin/destination sentence.  Prefer the
            # structured flight records above whenever available; retain this
            # prose fallback for routes (especially buses) with no such data.
            detail.extend(["", "Service information: " + service_info])
        detail.extend([
            "",
            "This is a broad Rome2Rio estimate, not a confirmed live timetable. "
            "Open the route in Rome2Rio to check current details.",
        ])

        href = card.get("href") or page_url
        parsed.append({
            "summary": " ".join(summary_parts),
            "detail_text": "\n".join(detail),
            "travel_mode": "multimodal",
            "source": "rome2rio",
            "source_url": urllib.parse.urljoin(page_url, href),
            "legs": [],
        })
        seen.add(heading)

    return parsed


@dataclass
class Rome2RioClient:
    cache_dir: str

    def __post_init__(self):
        self.cache_path = os.path.join(self.cache_dir, "rome2rio_cache.json")
        self._cache = self._load_cache()

    def search(self, origin: str, destination: str,
               departure_date: datetime.date | None = None) -> list[dict]:
        # Version the key when presentation/parsing changes so cached route
        # dictionaries do not retain obsolete spoken wording.
        key = f"v9|{_clean(origin).lower()}|{_clean(destination).lower()}"
        entry = self._cache.get(key, {})
        if (isinstance(entry, dict)
                and time.time() - float(entry.get("timestamp", 0)) < _CACHE_TTL_SECONDS
                and isinstance(entry.get("routes"), list)):
            from curl_cffi import requests as browser_requests
            routes = json.loads(json.dumps(entry["routes"]))
            try:
                add_rome2rio_flights_for_all_routes(
                    routes, entry.get("api_key") or _PUBLIC_API_KEY,
                    browser_requests.get,
                    departure_date)
            except Exception:
                pass
            return routes

        url = route_url(origin, destination)
        # Rome2Rio protects its public pages with a Cloudflare browser check.
        # Use the known-working Edge TLS/browser fingerprint directly: trying
        # a plain HTTP request first only creates a rejected request and delay.
        from curl_cffi import requests as browser_requests
        response = browser_requests.get(
            url, impersonate="edge101", timeout=30, allow_redirects=True)
        response.raise_for_status()
        final_url = str(response.url)
        page = response.content
        routes = parse_routes(page, final_url)
        key_match = re.search(rb'"API_KEY":"([^"]+)"', page)
        public_api_key = (
            key_match.group(1).decode() if key_match else _PUBLIC_API_KEY)
        if routes:
            self._cache[key] = {
                "timestamp": time.time(), "routes": routes,
                "api_key": public_api_key,
            }
            self._save_cache()
        routes = json.loads(json.dumps(routes))
        try:
            add_rome2rio_flights_for_all_routes(
                routes, public_api_key, browser_requests.get, departure_date)
        except Exception:
            pass
        return routes

    def _load_cache(self) -> dict:
        try:
            with open(self.cache_path, encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_cache(self) -> None:
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
            with open(self.cache_path, "w", encoding="utf-8") as handle:
                json.dump(self._cache, handle, ensure_ascii=False, indent=2)
        except Exception:
            pass
