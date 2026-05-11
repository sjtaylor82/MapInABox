"""timetable.py — TimeTable Lookup API client for Map in a Box.

Returns OTA XML which we parse into plain dicts.
"""

import urllib.parse
import urllib.request
import ssl
import xml.etree.ElementTree as ET
import datetime

API_BASE = "https://timetable-lookup.p.rapidapi.com"
API_HOST = "timetable-lookup.p.rapidapi.com"


class TimetableClient:

    def __init__(self, api_key: str):
        self._key = api_key.strip() if api_key else ""
        self._ctx = ssl.create_default_context()

    @property
    def configured(self) -> bool:
        return bool(self._key)

    def search(self, origin: str, dest: str,
               compression: str = "AUTO",
               results: int = 10,
               sort: str = "Duration") -> list:
        """Search for flights. Returns list of itinerary dicts."""
        today  = datetime.datetime.utcnow().strftime("%Y%m%d")
        params = urllib.parse.urlencode({
            "Results": str(results),
            "Sort":    sort,
        })
        url = f"{API_BASE}/TimeTable/{origin}/{dest}/{today}/?{params}"
        req = urllib.request.Request(url, headers={
            "X-RapidAPI-Key":  self._key,
            "X-RapidAPI-Host": API_HOST,
            "User-Agent":      "MapInABox/1.0",
        })
        with urllib.request.urlopen(req, timeout=15, context=self._ctx) as r:
            body = r.read().decode().strip()

        return _parse_ota(body)


def _parse_ota(xml_str: str) -> list:
    """Parse OTA_AirDetailsRS XML into list of itinerary dicts."""
    try:
        xml_str = xml_str.replace(' xmlns="http://www.opentravel.org/OTA/2003/05"', '')
        root = ET.fromstring(xml_str)
    except ET.ParseError as exc:
        print(f"[Timetable] XML parse error: {exc}")
        return []

    itineraries = []

    for flight_details in root.iter("FlightDetails"):
        dur  = flight_details.get("TotalTripTime", "").replace("PT","").replace("H","h ").replace("M","m").strip()
        legs = []

        for leg in flight_details.iter("FlightLegDetails"):
            dep_time = leg.get("DepartureDateTime", "")[11:16]
            arr_time = leg.get("ArrivalDateTime",   "")[11:16]
            # Carrier resolution — work through sources best-to-worst:
            #  1. MarketingCarrierCode  (OTA standard, the airline you book with)
            #  2. OperatingCarrierCode  (physical operator — may be a codeshare partner)
            #  3. Leading letters of FlightNumber  (e.g. "QF824" → "QF")
            #  4. UUID slice [14:16]    (last resort; may hit operator not marketer)
            raw_fnum = leg.get("MarketingFlightNumber") or leg.get("FlightNumber", "")
            import re as _re
            _prefix = _re.match(r"^([A-Z]{2,3})", raw_fnum.upper())
            uuid     = leg.get("FLSUUID", "")
            carrier  = (leg.get("MarketingCarrierCode")
                        or leg.get("OperatingCarrierCode")
                        or (_prefix.group(1) if _prefix else "")
                        or (uuid[14:16] if len(uuid) >= 16 else ""))
            # Strip carrier prefix from flight number if already present
            if carrier and raw_fnum.upper().startswith(carrier.upper()):
                fnum = raw_fnum[len(carrier):]
            else:
                fnum = raw_fnum

            # Prefer explicit child airport elements; fall back to UUID slice.
            dep_el  = leg.find("DepartureAirport")
            arr_el  = leg.find("ArrivalAirport")
            uuid    = leg.get("FLSUUID", "")
            dep_apt = (dep_el.get("LocationCode", "") if dep_el is not None
                       else uuid[0:3] if len(uuid) >= 6 else "")
            arr_apt = (arr_el.get("LocationCode", "") if arr_el is not None
                       else uuid[3:6] if len(uuid) >= 6 else "")

            legs.append({
                "Carrier": carrier, "Flight": fnum,
                "Origin":  dep_apt, "Dest":   arr_apt,
                "DepTime": dep_time,"ArrTime": arr_time,
            })

        if legs:
            itineraries.append({"Flights": legs, "ElapsedTime": dur})

    print(f"[Timetable] Parsed {len(itineraries)} itineraries")
    return itineraries


def fmt_itinerary(itin: dict) -> str:
    """Format a single itinerary for display."""
    try:
        from airlines import AIRLINES
        # Build reverse map: IATA code -> airline name
        _iata_to_name = {v[1]: v[0] for v in AIRLINES.values() if v[1]}
    except ImportError:
        _iata_to_name = {}

    legs  = itin.get("Flights", [])
    dur   = itin.get("ElapsedTime", "")
    stops = len(legs) - 1

    if not legs:
        return str(itin)

    lines = []
    for leg in legs:
        carrier  = leg.get("Carrier", "")
        name     = _iata_to_name.get(carrier, carrier)
        fnum     = leg.get("Flight",  "")
        dep      = leg.get("Origin",  "")
        arr      = leg.get("Dest",    "")
        dt       = leg.get("DepTime", "")
        at       = leg.get("ArrTime", "")
        lines.append(f"  {name} {carrier}{fnum}  {dep} {dt} → {arr} {at}")

    stop_str = "Non-stop" if stops == 0 else f"{stops} stop{'s' if stops>1 else ''}"
    dur_str  = f"  Total: {dur}" if dur else ""
    return f"{stop_str}{dur_str}\n" + "\n".join(lines)
