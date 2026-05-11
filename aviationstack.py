"""aviationstack.py — AviationStack API client for Map in a Box."""

import json
import re
import urllib.parse
import urllib.request

API_BASE = "http://api.aviationstack.com/v1"

# Common suffixes to strip from airport names for brevity
_AIRPORT_SUFFIXES = re.compile(
    r'\s*[\-–]\s*(international|domestic|regional|kingsford smith|'
    r'tullamarine|airport|intl|int\'l)\s*$',
    re.IGNORECASE)

# Known non-commercial operator prefixes to filter out
_FILTER_OPERATORS = {"lifeflight", "royal flying doctor", "careflight",
                     "air ambulance", "rescue", "police", "military"}


def _short_airport(name: str) -> str:
    """Strip verbose suffixes from airport name."""
    if not name:
        return name
    name = _AIRPORT_SUFFIXES.sub('', name).strip()
    # Also strip trailing "Airport" word
    name = re.sub(r'\s+Airport$', '', name, flags=re.IGNORECASE).strip()
    return name


def _is_commercial(f: dict) -> bool:
    """Return False for empty, non-commercial or obviously bad entries."""
    flight  = f.get("flight") or {}
    airline = f.get("airline") or {}
    number  = (flight.get("iata") or flight.get("icao") or "").strip()
    name    = (airline.get("name") or "").strip().lower()
    if not number or not name:
        return False
    for op in _FILTER_OPERATORS:
        if op in name:
            return False
    return True


def _dep_key(f: dict) -> str:
    """Key for deduplication: time + destination."""
    sched = (f.get("departure") or {}).get("scheduled", "") or ""
    dest  = (f.get("arrival")   or {}).get("iata",      "") or ""
    return f"{sched[:16]}|{dest}"


def _arr_key(f: dict) -> str:
    """Key for deduplication: time + origin."""
    sched  = (f.get("arrival")   or {}).get("scheduled", "") or ""
    origin = (f.get("departure") or {}).get("iata",      "") or ""
    return f"{sched[:16]}|{origin}"


def _sort_time_dep(f: dict) -> str:
    return (f.get("departure") or {}).get("scheduled", "") or "9999"


def _sort_time_arr(f: dict) -> str:
    return (f.get("arrival") or {}).get("scheduled", "") or "9999"


def _preferred_flight(flights: list, mode: str) -> dict:
    """From a group of codeshares, pick the operating carrier flight."""
    # Prefer the flight whose airline IATA matches the flight number prefix
    for f in flights:
        flight_iata   = ((f.get("flight") or {}).get("iata") or "")
        airline_iata  = ((f.get("airline") or {}).get("iata") or "")
        if flight_iata and airline_iata and flight_iata.startswith(airline_iata):
            return f
    return flights[0]


def deduplicate(flights: list, mode: str) -> list:
    """Remove codeshare duplicates, keep operating carrier, sort by time."""
    keyfn = _dep_key if mode == "dep" else _arr_key
    groups: dict[str, list] = {}
    for f in flights:
        if not _is_commercial(f):
            continue
        k = keyfn(f)
        groups.setdefault(k, []).append(f)

    result = [_preferred_flight(g, mode) for g in groups.values()]
    sortfn = _sort_time_dep if mode == "dep" else _sort_time_arr
    result.sort(key=sortfn)
    return result


class AviationStackClient:

    def __init__(self, api_key: str):
        self._key = api_key.strip() if api_key else ""

    @property
    def configured(self) -> bool:
        return bool(self._key)

    def _get(self, endpoint: str, params: dict) -> dict:
        params["access_key"] = self._key
        url = f"{API_BASE}/{endpoint}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": "MapInABox/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())

    def departures(self, iata: str, limit: int = 100) -> list:
        try:
            data = self._get("flights", {
                "dep_iata":      iata,
                "limit":         limit,
                "flight_status": "scheduled",
            })
            raw = data.get("data", [])
            return deduplicate(raw, "dep")
        except Exception as exc:
            print(f"[AviationStack] Departures failed: {exc}")
            return []

    def arrivals(self, iata: str, limit: int = 100) -> list:
        try:
            data = self._get("flights", {
                "arr_iata":      iata,
                "limit":         limit,
                "flight_status": "scheduled",
            })
            raw = data.get("data", [])
            return deduplicate(raw, "arr")
        except Exception as exc:
            print(f"[AviationStack] Arrivals failed: {exc}")
            return []


def fmt_dep(f: dict) -> str:
    airline    = (f.get("airline") or {}).get("name", "")
    flight_num = (f.get("flight")  or {}).get("iata", "") or \
                 (f.get("flight")  or {}).get("icao", "")
    dest       = _short_airport((f.get("arrival") or {}).get("airport", "")) or \
                 (f.get("arrival") or {}).get("iata", "")
    sched      = (f.get("departure") or {}).get("scheduled", "")
    time_str   = sched[11:16] if sched and len(sched) >= 16 else ""
    parts = [p for p in [time_str, flight_num, airline, dest] if p]
    return "  " + "  ".join(parts)


def fmt_arr(f: dict) -> str:
    airline    = (f.get("airline")   or {}).get("name", "")
    flight_num = (f.get("flight")    or {}).get("iata", "") or \
                 (f.get("flight")    or {}).get("icao", "")
    origin     = _short_airport((f.get("departure") or {}).get("airport", "")) or \
                 (f.get("departure") or {}).get("iata", "")
    sched      = (f.get("arrival")   or {}).get("scheduled", "")
    time_str   = sched[11:16] if sched and len(sched) >= 16 else ""
    parts = [p for p in [time_str, flight_num, airline, origin] if p]
    return "  " + "  ".join(parts)
