"""airport_directory.py - Airport amenity directory helpers.

Design (v4)
-----------
The amenity data comes primarily from OpenStreetMap indoor airport mapping,
fetched by ``PoiFetcher.fetch_airport_amenities`` in poi_fetch.py.  OSM gives a
consistent, global, structured picture of named shops, food venues and
facilities together with the terminal and level they sit on.

An *optional* enrichment layer fetches an official airport page and asks
Mistral to extract before/after-security and assistance details that OSM does
not carry.  Every field the model returns is discarded unless it is literally
evidenced in the fetched page text, because for a blind traveller a wrong
"after security, near Gate 24" is worse than no location at all.

This module is pure: it does no geocoding and no Overpass querying.  It takes
already-fetched records (from OSM) or already-fetched page text (for the
optional model enrichment) and turns them into screen-reader friendly output.

Public API
----------
focus_label(focus_key) -> str
clean_osm_records(records, focus_key="all") -> list[dict]
merge_records(records) -> list[dict]
summarise_records(records, query, source_links=None, airline_hints=None, terminal_gates=None, priority_query="") -> str
describe_item(rec, airport_name="") -> str
extract_airline_gate_hints(records) -> list[dict]
combine_airline_hints(*groups) -> list[dict]

# Optional official-source enrichment (model output is evidence-gated)
discover_source_urls(query, search_client=None, source_hint="") -> list[str]
fetch_official_source_text(source_urls) -> tuple[str, list[str]]
build_extraction_prompt(query, focus_key, source_text) -> str
clean_directory_records(records, source_text, focus_key="all") -> list[dict]
build_airline_gate_prompt(query, source_text) -> str
clean_airline_gate_records(records, source_text) -> list[dict]
cache_key(query, focus_key, source_text="") -> str
"""

from __future__ import annotations

import hashlib
import html
import re
import time
import urllib.parse
import urllib.request
from logging_utils import miab_log

_CACHE_VERSION = "v4"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36 MapInABox/1.0"
)
_REQUEST_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-AU,en;q=0.9",
    "Accept-Encoding": "identity",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}
_MAX_PAGE_TEXT = 35000
_MAX_SOURCE_TEXT = 120000

_SOURCE_LINK_TOKENS = (
    "shop", "shops", "shopping", "dine", "dining", "eat", "food",
    "restaurant", "restaurants", "cafe", "cafes", "bar", "bars",
    "terminal", "terminals", "map", "maps", "directory", "store",
    "stores", "retail", "services", "facilities", "amenities",
    "accessibility", "accessible", "assistance", "sensory", "quiet",
    "lounge", "lounges", "toilet", "toilets", "security",
)

_REJECT_SEARCH_HOSTS = (
    "google.", "bing.", "yahoo.", "wikipedia.org", "wikivoyage.org",
    "tripadvisor.", "yelp.", "facebook.", "instagram.", "x.com",
    "twitter.", "youtube.", "maps.apple.", "mapcarta.", "rome2rio.",
)

_FOCUS_LABELS = {
    "all": "All amenities",
    "food": "Food and coffee",
    "shopping": "Shops",
    "facilities": "Toilets, water, charging and services",
    "accessibility": "Accessibility and calmer spaces",
}

_CATEGORY_GROUPS = {
    "food": "Food and coffee",
    "coffee": "Food and coffee",
    "cafe": "Food and coffee",
    "restaurant": "Food and coffee",
    "bar": "Food and coffee",
    "fast food": "Food and coffee",
    "takeaway": "Food and coffee",
    "shop": "Shops",
    "shopping": "Shops",
    "retail": "Shops",
    "store": "Shops",
    "duty free": "Shops",
    "pharmacy": "Shops",
    "toilet": "Facilities",
    "toilets": "Facilities",
    "bathroom": "Facilities",
    "charging": "Facilities",
    "water": "Facilities",
    "wifi": "Facilities",
    "baggage": "Facilities",
    "atm": "Facilities",
    "bank": "Facilities",
    "currency": "Facilities",
    "lounge": "Facilities",
    "accessibility": "Accessibility and calmer spaces",
    "accessible": "Accessibility and calmer spaces",
    "assistance": "Accessibility and calmer spaces",
    "sensory": "Accessibility and calmer spaces",
    "quiet": "Accessibility and calmer spaces",
    "parents": "Facilities",
    "parents room": "Facilities",
    "family": "Facilities",
}

_FOCUS_TERMS = {
    "food": {"food", "coffee", "cafe", "restaurant", "bar", "fast food", "takeaway", "dining"},
    "shopping": {"shop", "shopping", "retail", "store", "duty free", "pharmacy"},
    "facilities": {
        "toilet", "toilets", "bathroom", "charging", "water", "wifi",
        "baggage", "atm", "bank", "currency", "lounge", "parents",
        "parents room", "family", "service",
    },
    "accessibility": {
        "accessibility", "accessible", "assistance", "sensory", "quiet",
        "changing places", "mobility", "hearing loop",
    },
}


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def normalise(text: str) -> str:
    text = html.unescape(str(text or "")).lower()
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", normalise(text))


def focus_label(focus_key: str) -> str:
    return _FOCUS_LABELS.get((focus_key or "all").lower(), _FOCUS_LABELS["all"])


def cache_key(query: str, focus_key: str, source_text: str = "") -> str:
    digest = hashlib.sha1((source_text or "")[:50000].encode("utf-8", "ignore")).hexdigest()[:12]
    return f"airport_amenity_{_CACHE_VERSION}_{normalise(query).replace(' ', '_')}_{focus_key}_{digest}"


# ---------------------------------------------------------------------------
# OSM record cleaning
# ---------------------------------------------------------------------------

def clean_osm_records(records, focus_key: str = "all") -> list[dict]:
    """Normalise the structured records produced from OpenStreetMap.

    OSM records are trusted structurally (they come from tagged data, not from
    a language model), so unlike :func:`clean_directory_records` there is no
    evidence gate — only normalisation, focus filtering and de-duplication.
    """
    if not isinstance(records, list):
        return []
    today = time.strftime("%Y-%m-%d")
    clean: list[dict] = []
    for raw in records:
        if not isinstance(raw, dict):
            continue
        name = _clean_value(raw.get("name"))
        if not name:
            continue
        category = _clean_category(raw.get("category"), name)
        if not _matches_focus(category, name, focus_key):
            continue
        out = {
            "name": name,
            "category": category,
            "terminal": _clean_value(raw.get("terminal"), max_len=80),
            "zone": _clean_value(raw.get("zone"), max_len=80),
            "level": _clean_value(raw.get("level"), max_len=80),
            "area": _clean_value(raw.get("area"), max_len=120),
            "gate": _clean_value(raw.get("gate"), max_len=80),
            "exit": _clean_value(raw.get("exit"), max_len=80),
            "airlines": _clean_value(raw.get("airlines"), max_len=120),
            "opening_hours": _clean_value(raw.get("opening_hours"), max_len=160),
            "notes": _clean_value(raw.get("notes"), max_len=180),
            "source_url": "",
            "evidence": "",
            "source": "osm",
            "last_checked": today,
        }
        out["_location_quality"] = _location_quality(out)
        clean.append(out)
    return _sort_records(merge_records(clean))


# ---------------------------------------------------------------------------
# Optional official-source enrichment
# ---------------------------------------------------------------------------

def discover_source_urls(query: str, search_client=None, source_hint: str = "") -> list[str]:
    """Return official-looking source URLs for an airport query.

    URLs the user typed are always trusted.  Otherwise we ask the search
    client (Serper) for the airport's own site and keep only results that look
    like they belong to the airport the user asked about.
    """
    urls: list[str] = []
    urls.extend(_urls_from_text(source_hint))
    urls.extend(_urls_from_text(query))

    q_norm = normalise(query)
    if search_client is not None and q_norm:
        searches = [
            f"{query} official airport shops dining facilities",
            f"{query} official airport accessibility security map",
        ]
        for search in searches:
            try:
                results = search_client.search(search, num=8)
            except Exception as exc:
                miab_log("errors", f"[AirportDir] Search failed for {search!r}: {exc}", None)
                results = []
            for item in results or []:
                url = (item.get("url") or item.get("link") or "").strip()
                title = item.get("title") or ""
                snippet = item.get("snippet") or ""
                if _search_result_looks_official(url, title, snippet, q_norm):
                    urls.append(url)

    return _dedupe_urls(urls)[:6]


def fetch_official_source_text(source_urls) -> tuple[str, list[str]]:
    """Fetch the official airport pages named in *source_urls*.

    Deliberately simple: it fetches the given pages and converts them to text.
    It does not crawl, follow links, or reverse-engineer site-specific JSON
    APIs — that complexity was the most fragile part of the old design.
    """
    urls = _dedupe_urls(_coerce_url_list(source_urls))[:6]
    if not urls:
        return "", []

    texts: list[str] = []
    fetched_urls: list[str] = []
    for url in urls:
        fetched = _fetch_url(url)
        if not fetched:
            continue
        page_url, page_html = fetched
        text = _html_to_text(page_html)
        if not text:
            continue
        texts.append(f"SOURCE: {page_url}\n{text[:_MAX_PAGE_TEXT]}")
        fetched_urls.append(page_url)
        if sum(len(t) for t in texts) >= _MAX_SOURCE_TEXT:
            break

    if not texts:
        return "", []
    body = "\n\n".join(texts)
    if fetched_urls:
        body += "\n\nOFFICIAL_SOURCE_LINKS:\n" + "\n".join(fetched_urls)
    return body[:_MAX_SOURCE_TEXT], _dedupe_urls(fetched_urls)


def build_extraction_prompt(query: str, focus_key: str, source_text: str) -> str:
    focus = focus_label(focus_key)
    return (
        f"Extract an airport amenity guide for '{query}'. Focus: {focus}.\n\n"
        "Use ONLY the official source text below. Do not use general knowledge, "
        "Google Maps assumptions, common airport layouts, or old memory. Do not "
        "invent terminal, level, gate, before-security, after-security, hours, or "
        "nearby-area details. If a field is not stated, use an empty string. Do "
        "not include emergency procedures unless the user specifically asked for "
        "emergency information.\n\n"
        "Return ONLY a JSON array of objects. Each object must have these string "
        "keys: name, category, terminal, zone, level, area, gate, exit, airlines, "
        "opening_hours, notes, source_url, evidence. Category should be one of: food, coffee, cafe, "
        "restaurant, bar, fast food, shop, duty free, pharmacy, toilets, water, "
        "charging, baggage, atm, currency, lounge, accessibility, sensory, quiet "
        "space, parents room, service, amenity.\n\n"
        "Rules:\n"
        "- Include only named public passenger amenities, shops, food venues, or useful services.\n"
        "- The name must appear in the source text.\n"
        "- terminal must be exactly what the source states, such as T2, Domestic Terminal, or International Terminal.\n"
        "- zone must only say before security, after security, landside, airside, arrivals, departures, or empty.\n"
        "- Prefer adding before security or after security wording, as that is the detail travellers most need.\n"
        "- area may include a precinct, concourse, level, or nearby landmark only when explicitly stated.\n"
        "- gate is a high-priority location cue: include gate numbers, gate ranges, or phrases like near Gate 24 only when stated.\n"
        "- exit is for exit, door, entry, arrivals exit, or departures entrance wording only when stated.\n"
        "- airlines may list airlines only when the source says those flights check in, depart, or board from that area.\n"
        "- evidence must be a short source phrase that supports the item.\n"
        "- Return fewer items rather than guessing.\n\n"
        f"SOURCE TEXT:\n{source_text}"
    )


def clean_directory_records(
    records,
    source_text: str,
    focus_key: str = "all",
) -> list[dict]:
    """Validate and normalise model-extracted records against *source_text*.

    Any field not literally evidenced in the source text is dropped.  Terminal
    scoping is not applied here — every terminal is kept, and a terminal the
    user asked about is brought to the front of the output by
    :func:`summarise_records`, so a wording mismatch never zeroes out results.
    """
    if not isinstance(records, list):
        return []
    source_norm = normalise(source_text)
    source_compact = _compact(source_text)
    today = time.strftime("%Y-%m-%d")

    clean: list[dict] = []
    for raw in records:
        rec = _coerce_record(raw)
        name = _clean_value(rec.get("name"))
        if not name or not _evidenced(name, source_norm, source_compact):
            continue

        category = _clean_category(rec.get("category"), name)
        if not _matches_focus(category, name, focus_key):
            continue

        out = {
            "name": name,
            "category": category,
            "terminal": _supported_field(rec.get("terminal"), source_norm, source_compact),
            "zone": _clean_zone(rec.get("zone"), source_norm, source_compact),
            "level": _supported_field(rec.get("level"), source_norm, source_compact),
            "area": _supported_field(rec.get("area"), source_norm, source_compact),
            "gate": _supported_field(rec.get("gate"), source_norm, source_compact),
            "exit": _supported_field(rec.get("exit"), source_norm, source_compact),
            "airlines": _supported_field(rec.get("airlines"), source_norm, source_compact),
            "opening_hours": _supported_field(rec.get("opening_hours"), source_norm, source_compact),
            "notes": _supported_field(rec.get("notes"), source_norm, source_compact, max_len=180),
            "source_url": _clean_source_url(rec.get("source_url"), source_text),
            "evidence": _supported_field(rec.get("evidence"), source_norm, source_compact, max_len=220),
            "source": "official",
            "last_checked": today,
        }
        out["_location_quality"] = _location_quality(out)
        clean.append(out)

    return _sort_records(merge_records(clean))


# ---------------------------------------------------------------------------
# Merge / sort
# ---------------------------------------------------------------------------

def merge_records(records: list[dict]) -> list[dict]:
    """Deduplicate records while preserving the richest locator.

    When the same venue appears in both OSM and an official page, the record
    with the stronger location (gate, zone, etc.) wins, but a known
    before/after-security ``zone`` from the official page is always kept.
    """
    by_key: dict[str, dict] = {}
    for rec in records or []:
        if not isinstance(rec, dict):
            continue
        # Key on name + terminal only, so an OSM record and the official-page
        # record for the same venue merge (and the official zone/gate is kept),
        # while the same chain in a different terminal stays separate.
        key = _compact(rec.get("name", "") + "|" + rec.get("terminal", ""))
        if not key:
            continue
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = dict(rec)
            continue
        # Keep the copy with the stronger locator, but back-fill the
        # high-value fields (zone, hours, source URL) from the other copy.
        if _locator_score(rec) > _locator_score(existing):
            winner, other = dict(rec), existing
        else:
            winner, other = dict(existing), rec
        for field in ("zone", "opening_hours", "source_url"):
            if not winner.get(field) and other.get(field):
                winner[field] = other[field]
        by_key[key] = winner
    return list(by_key.values())


def _sort_records(records: list[dict]) -> list[dict]:
    records.sort(key=lambda r: (
        _group_for_record(r),
        normalise(r.get("terminal", "")),
        -_locator_score(r),
        normalise(r.get("name", "")),
    ))
    return records


# ---------------------------------------------------------------------------
# Summary / description
# ---------------------------------------------------------------------------

def summarise_records(
    records: list[dict],
    query: str,
    source_links: list[str] | None = None,
    airline_hints: list[dict] | None = None,
    terminal_gates: list[dict] | None = None,
    priority_query: str = "",
) -> str:
    checked = time.strftime("%Y-%m-%d")
    links = _dedupe_urls(source_links or [])
    used_official = any((r.get("source") == "official") for r in (records or []))
    if used_official:
        source_line = (
            "Source: OpenStreetMap, with before/after-security details added "
            "from official airport pages; gate, exit and airline fields are "
            "left out unless a source states them."
        )
    else:
        source_line = (
            "Source: OpenStreetMap indoor airport data. Terminal and level come "
            "from the map; before/after-security is not available from this source."
        )
    lines = [
        f"Airport Amenity Guide: {query}",
        f"Last checked: {checked}",
        source_line,
        "",
    ]

    merged = _sort_records(merge_records(records))
    if airline_hints is None:
        airline_hints = extract_airline_gate_hints(merged)
    lines.extend(_airline_gate_block(airline_hints, terminal_gates))

    if not records:
        lines.append("No amenities were found for this query.")
        lines.append("Try the full airport name, or paste the official airport website URL.")
        if links:
            lines.append("")
            lines.append("Pages checked:")
            lines.extend(f"- {url}" for url in links[:8])
        return "\n".join(lines).strip()

    terminals: dict[str, list[dict]] = {}
    for rec in merged:
        terminal = _terminal_group_label(rec.get("terminal", ""))
        terminals.setdefault(terminal, []).append(rec)

    # If the user named a terminal (e.g. "SYD T2"), float its section to the
    # top while still showing the rest — a wording mismatch never hides it.
    requested_terms = _requested_terminal_terms(priority_query)

    def _order_key(label: str):
        prioritised = 0 if (requested_terms
                            and _record_matches_terminal({"terminal": label}, requested_terms)) else 1
        return (prioritised, _terminal_sort_key(label))

    for terminal in sorted(terminals, key=_order_key):
        lines.append(terminal)
        groups: dict[str, list[dict]] = {}
        for rec in terminals[terminal]:
            groups.setdefault(_group_for_record(rec), []).append(rec)

        for group in ("Food and coffee", "Shops", "Facilities",
                      "Accessibility and calmer spaces", "Other amenities"):
            items = groups.get(group) or []
            if not items:
                continue
            lines.append(group)
            for rec in items:
                bits = [rec["name"]]
                cat = rec.get("category")
                if cat:
                    bits.append(cat)
                loc = _location_phrase(rec, include_terminal=False)
                if loc:
                    bits.append(loc)
                hours = rec.get("opening_hours")
                if hours:
                    bits.append(f"hours: {hours}")
                lines.append("- " + "; ".join(bits) + ".")
        lines.append("")

    if links:
        lines.append("Official pages checked:")
        lines.extend(f"- {url}" for url in links[:10])
    return "\n".join(lines).strip()


def _airline_gate_block(airline_hints: list[dict] | None, terminal_gates: list[dict] | None) -> list[str]:
    """Render the 'Airline and gate information' section, or return []."""
    hints = airline_hints or []
    ranges = terminal_gates or []
    if not hints and not ranges:
        return []
    lines = [
        "Airline and gate information",
        "Gate ranges come from OpenStreetMap; per-airline detail, where shown, "
        "comes from official airport pages. None are live gate assignments.",
    ]
    for hint in hints:
        bits = [hint["airline"]]
        loc = _location_phrase(hint)
        if loc:
            bits.append(loc)
        lines.append("- " + "; ".join(bits) + ".")
    for tg in ranges:
        term = _clean_value(tg.get("terminal"), max_len=80)
        if not term:
            continue
        low, high = tg.get("low"), tg.get("high")
        if low and high:
            rng = f"gate {low}" if low == high else f"gates {low} to {high}"
        elif tg.get("gates"):
            rng = "gates " + ", ".join(tg["gates"])
        else:
            continue
        count = tg.get("count")
        suffix = f" ({count} gates)" if isinstance(count, int) and count > 1 else ""
        lines.append(f"- {term}: {rng}{suffix}.")
    lines.append("")
    return lines


def build_airline_gate_prompt(query: str, source_text: str) -> str:
    """Prompt to extract airline -> gate/terminal facts from official text."""
    return (
        f"From the official airport source text below for '{query}', extract which "
        "airlines depart from which gates or terminals.\n\n"
        "Use ONLY the source text. Do not use general knowledge, common airport "
        "layouts, or old memory. Do not invent gate numbers. If the text does not "
        "state an airline's gates or terminal, do not include that airline.\n\n"
        "Return ONLY a JSON array of objects with these string keys: airline, "
        "terminal, zone, gate, area, evidence.\n"
        "- airline: the airline name exactly as written in the source.\n"
        "- gate: a gate number or range exactly as stated, such as 25-38 or "
        "16 to 42. Empty if not stated.\n"
        "- terminal: such as T2, Domestic Terminal, International Terminal. Empty if not stated.\n"
        "- zone: before security, after security, departures, arrivals, or empty.\n"
        "- area: a pier, concourse or precinct if stated, else empty.\n"
        "- evidence: a short phrase from the source that supports the mapping.\n"
        "- Include an item only if the source states a gate, gate range, or terminal for that airline.\n"
        "- Return fewer items rather than guessing.\n\n"
        f"SOURCE TEXT:\n{source_text}"
    )


def clean_airline_gate_records(records, source_text: str) -> list[dict]:
    """Validate model-extracted airline gate facts against *source_text*."""
    if not isinstance(records, list):
        return []
    source_norm = normalise(source_text)
    source_compact = _compact(source_text)
    hints: list[dict] = []
    for raw in records:
        rec = _coerce_record(raw)
        airline = _clean_value(rec.get("airline"), max_len=80)
        if not airline or not _evidenced(airline, source_norm, source_compact):
            continue
        gate = _supported_field(rec.get("gate"), source_norm, source_compact, max_len=80)
        terminal = _supported_field(rec.get("terminal"), source_norm, source_compact, max_len=80)
        area = _supported_field(rec.get("area"), source_norm, source_compact, max_len=120)
        zone = _clean_zone(rec.get("zone"), source_norm, source_compact)
        if not (gate or terminal or area):
            continue
        hints.append({
            "airline": airline,
            "terminal": terminal,
            "zone": zone,
            "level": "",
            "gate": gate,
            "exit": "",
            "area": area,
            "evidence": _supported_field(rec.get("evidence"), source_norm, source_compact, max_len=160),
        })
    return _dedupe_airline_hints(hints)


def combine_airline_hints(*groups: list[dict]) -> list[dict]:
    """Concatenate and de-duplicate several airline-hint lists."""
    out: list[dict] = []
    for group in groups:
        out.extend(group or [])
    return _dedupe_airline_hints(out)


def describe_item(rec: dict, airport_name: str = "") -> str:
    """Format a screen-reader friendly airport amenity record."""
    if not isinstance(rec, dict):
        return str(rec or "")
    name = _clean_value(rec.get("name"))
    if not name:
        return ""
    parts = [name]
    cat = _clean_value(rec.get("category"))
    if cat:
        parts.append(f"Category: {cat}.")
    term = _clean_value(rec.get("terminal"))
    if term:
        parts.append(f"Terminal: {term}.")
    zone = _clean_value(rec.get("zone"))
    if zone:
        parts.append(f"Zone: {zone}.")
    level = _display_location_value("level", rec.get("level"))
    if level:
        parts.append(f"Level: {level}.")
    area = _clean_value(rec.get("area"))
    if area:
        parts.append(f"Area: {area}.")
    gate = _display_location_value("gate", rec.get("gate"))
    if gate:
        parts.append(f"Gate relationship: {gate}.")
    exit_hint = _display_location_value("exit", rec.get("exit"))
    if exit_hint:
        parts.append(f"Exit or door: {exit_hint}.")
    airlines = _clean_value(rec.get("airlines"))
    if airlines:
        parts.append(f"Airlines: {airlines}.")
    hours = _clean_value(rec.get("opening_hours"))
    if hours:
        parts.append(f"Opening hours: {hours}.")
    notes = _clean_value(rec.get("notes"))
    if notes:
        parts.append(f"Notes: {notes}.")
    checked = _clean_value(rec.get("last_checked"))
    if checked:
        src = "official airport page" if rec.get("source") == "official" else "OpenStreetMap"
        parts.append(f"Source: {src}, checked {checked}.")
    url = _clean_value(rec.get("source_url"))
    if url:
        parts.append(f"Source URL: {url}")
    return " ".join(parts).strip()


# ---------------------------------------------------------------------------
# Airline gate hints (from records only)
# ---------------------------------------------------------------------------

def extract_airline_gate_hints(records: list[dict] | None = None) -> list[dict]:
    """Extract official-source airline area hints without live-gate claims."""
    hints: list[dict] = []
    for rec in records or []:
        if not _looks_like_airline_hint_context(_record_context_text(rec)):
            continue
        airlines = _split_airlines(rec.get("airlines", ""))
        if not airlines:
            continue
        for airline in airlines:
            hint = {
                "airline": airline,
                "terminal": rec.get("terminal", ""),
                "zone": rec.get("zone", ""),
                "level": rec.get("level", ""),
                "gate": rec.get("gate", ""),
                "exit": rec.get("exit", ""),
                "area": rec.get("area", ""),
                "evidence": rec.get("evidence", "")[:160],
            }
            if _has_any_locator(hint):
                hints.append(hint)
    return _dedupe_airline_hints(hints)


def _split_airlines(value: str) -> list[str]:
    names = []
    for part in re.split(r",|/|;|\band\b", str(value or ""), flags=re.I):
        name = _clean_value(part, max_len=80)
        if not name:
            continue
        if normalise(name) == "virgin":
            name = "Virgin Australia"
        key = normalise(name)
        if key and key not in {normalise(n) for n in names}:
            names.append(name)
    return names


def _dedupe_airline_hints(hints: list[dict]) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for hint in hints or []:
        airline = _clean_value(hint.get("airline"), max_len=80)
        if not airline:
            continue
        clean = {
            "airline": airline,
            "terminal": _clean_value(hint.get("terminal"), max_len=80),
            "zone": _clean_value(hint.get("zone"), max_len=80),
            "level": _clean_value(hint.get("level"), max_len=80),
            "gate": _clean_value(hint.get("gate"), max_len=80),
            "exit": _clean_value(hint.get("exit"), max_len=80),
            "area": _clean_value(hint.get("area"), max_len=120),
            "evidence": _clean_value(hint.get("evidence"), max_len=160),
        }
        key = _compact("|".join([
            clean["airline"], clean["terminal"], clean["zone"],
            clean["level"], clean["gate"], clean["exit"], clean["area"],
        ]))
        if key in seen:
            continue
        seen.add(key)
        out.append(clean)
    out.sort(key=lambda h: (normalise(h["airline"]), -_locator_score(h), normalise(h.get("terminal", ""))))
    return out[:20]


def _record_context_text(rec: dict) -> str:
    return " ".join(str(rec.get(k, "")) for k in (
        "name", "category", "area", "gate", "exit", "notes", "evidence"
    ))


def _looks_like_airline_hint_context(raw: str) -> bool:
    text = str(raw or "")
    low = normalise(text)
    if not low:
        return False
    if any(term in low for term in (
        "check in", "checkin", "boarding", "board from", "boards from",
        "depart from", "departs from", "departure gate", "departure gates",
        "airline", "airlines", "flight", "flights", "uses gates",
        "use gates", "gates for",
    )):
        return True
    return bool(re.search(
        r"\b(?:Qantas|Virgin(?:\s+Australia)?)\b.{0,80}\b(?:check[- ]?in|boarding|boards?|departs?|flights?|airlines?)\b",
        text, re.I,
    ) or re.search(
        r"\b(?:check[- ]?in|boarding|boards?|departs?|flights?|airlines?)\b.{0,80}\b(?:Qantas|Virgin(?:\s+Australia)?)\b",
        text, re.I,
    ))


# ---------------------------------------------------------------------------
# Scoring / grouping
# ---------------------------------------------------------------------------

def _locator_score(rec: dict) -> int:
    score = 0
    if rec.get("gate"):
        score += 12
    if rec.get("zone"):
        score += 10
    if rec.get("exit"):
        score += 8
    if rec.get("area"):
        score += 6
    if rec.get("airlines"):
        score += 5
    if rec.get("level"):
        score += 3
    if rec.get("terminal"):
        score += 1
    if rec.get("opening_hours"):
        score += 1
    return score


def _location_quality(rec: dict) -> str:
    if rec.get("gate") or rec.get("exit") or rec.get("zone") or rec.get("airlines"):
        return "specific"
    area = normalise(rec.get("area", ""))
    if area and area not in {"multiple locations"}:
        return "specific"
    if rec.get("level") and (rec.get("terminal") or rec.get("zone")):
        return "medium"
    if rec.get("terminal") or rec.get("zone") or rec.get("area"):
        return "broad"
    return ""


def _has_any_locator(rec: dict) -> bool:
    return any(rec.get(k) for k in ("terminal", "zone", "level", "gate", "exit", "area", "airlines"))


def _group_for_record(rec: dict) -> str:
    hay = normalise(f"{rec.get('category', '')} {rec.get('name', '')} {rec.get('notes', '')}")
    for term, group in _CATEGORY_GROUPS.items():
        if term in hay:
            return group
    return "Other amenities"


def _terminal_group_label(value: str) -> str:
    terminal = _clean_value(value, max_len=80)
    return f"Terminal: {terminal}" if terminal else "Terminal not stated"


def _terminal_sort_key(label: str) -> tuple:
    low = normalise(label)
    if "domestic" in low:
        return (0, low)
    m = re.search(r"\bt\s*([0-9])\b|\bterminal\s*([0-9])\b", low)
    if m:
        num = int(m.group(1) or m.group(2))
        return (1, f"{num:02d}", low)
    if "international" in low:
        return (3, low)
    if "not stated" in low:
        return (99, low)
    return (10, low)


def _location_phrase(rec: dict, include_terminal: bool = True) -> str:
    parts = []
    fields = [
        ("terminal", "terminal"),
        ("zone", "zone"),
        ("level", "level"),
        ("gate", "gate"),
        ("exit", "exit"),
        ("airlines", "airlines"),
        ("area", "area"),
    ]
    for key, label in fields:
        if key == "terminal" and not include_terminal:
            continue
        value = rec.get(key)
        if value:
            parts.append(f"{label}: {_display_location_value(key, value)}")
    return ", ".join(parts)


def _display_location_value(key: str, value) -> str:
    """Avoid spoken duplication like 'level: Level 2' or 'gate: Gate 28'."""
    text = _clean_value(value, max_len=160)
    if not text:
        return ""
    key = (key or "").lower()
    if key == "level":
        text = re.sub(r"(?i)^level\s+", "", text).strip()
    elif key == "gate":
        text = re.sub(r"(?i)^gate\s+", "", text).strip()
    elif key == "exit":
        text = re.sub(r"(?i)^exit\s+", "", text).strip()
    return text


# ---------------------------------------------------------------------------
# Field cleaning / evidence gate
# ---------------------------------------------------------------------------

def _coerce_record(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        return {"name": raw}
    return {}


def _clean_value(value, max_len: int = 240) -> str:
    text = html.unescape(str(value or "")).strip()
    text = re.sub(r"\s+", " ", text)
    if text.lower() in {"unknown", "not stated", "n/a", "none", "null", "-"}:
        return ""
    return text[:max_len].strip()


def _evidenced(value: str, source_norm: str, source_compact: str) -> bool:
    value_norm = normalise(value)
    if not value_norm:
        return False
    if value_norm in source_norm:
        return True
    return _compact(value) in source_compact


def _supported_field(value, source_norm: str, source_compact: str, max_len: int = 160) -> str:
    text = _clean_value(value, max_len=max_len)
    if not text:
        return ""
    return text if _evidenced(text, source_norm, source_compact) else ""


def _clean_category(category, name: str) -> str:
    cat = normalise(category)
    if not cat:
        cat = _category_from_name(name)
    if not cat:
        return "amenity"
    if cat in {"restroom", "bathrooms", "bathroom"}:
        return "toilets"
    if cat in {"store", "retail"}:
        return "shop"
    return cat[:40]


def _category_from_name(name: str) -> str:
    low = normalise(name)
    if any(t in low for t in ("coffee", "cafe", "espresso")):
        return "coffee"
    if any(t in low for t in ("bar", "pub")):
        return "bar"
    if any(t in low for t in ("grill", "kitchen", "sushi", "burger", "pizza", "restaurant")):
        return "restaurant"
    if any(t in low for t in ("pharmacy", "chemist")):
        return "pharmacy"
    if any(t in low for t in ("duty free", "news", "book", "market", "store", "shop")):
        return "shop"
    return ""


def _clean_zone(value, source_norm: str, source_compact: str) -> str:
    text = _supported_field(value, source_norm, source_compact, max_len=80)
    low = normalise(text)
    if not low:
        return ""
    if "after security" in low:
        return "after security"
    if "before security" in low:
        return "before security"
    if low in {"airside", "landside", "arrivals", "departures"}:
        return low
    return text


def _clean_source_url(value, source_text: str) -> str:
    url = _clean_value(value, max_len=400)
    if not url.startswith(("http://", "https://")):
        return ""
    return url if url in source_text else ""


def _matches_focus(category: str, name: str, focus_key: str) -> bool:
    focus_key = (focus_key or "all").lower()
    if focus_key == "all":
        return True
    terms = _FOCUS_TERMS.get(focus_key)
    if not terms:
        return True
    hay = normalise(f"{category} {name}")
    return any(term in hay for term in terms)


def _requested_terminal_terms(query: str) -> set[str]:
    q = normalise(query)
    out: set[str] = set()
    for n in range(1, 10):
        if re.search(rf"\bt\s*{n}\b", q) or f"terminal {n}" in q:
            out.add(f"t{n}")
            out.add(f"terminal {n}")
    if "domestic" in q:
        out.add("domestic")
    if "international" in q:
        out.add("international")
    return out


def _record_matches_terminal(rec: dict, requested_terms: set[str]) -> bool:
    hay = normalise(" ".join(str(rec.get(k, "")) for k in (
        "terminal", "area", "gate", "exit", "airlines", "notes", "evidence"
    )))
    if not hay:
        return False
    for term in requested_terms:
        if term in hay:
            return True
        m = re.fullmatch(r"t([0-9]+)", term)
        if m and f"terminal {m.group(1)}" in hay:
            return True
    return False


# ---------------------------------------------------------------------------
# URL / HTML helpers (for the optional enrichment layer)
# ---------------------------------------------------------------------------

def _urls_from_text(text: str) -> list[str]:
    return re.findall(r"https?://[^\s\]\)\"']+", str(text or ""))


def _coerce_url_list(source_urls) -> list[str]:
    if isinstance(source_urls, str):
        return _urls_from_text(source_urls) or [source_urls]
    if isinstance(source_urls, (list, tuple)):
        out: list[str] = []
        for item in source_urls:
            out.extend(_coerce_url_list(item))
        return out
    return []


def _dedupe_urls(urls: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for url in urls:
        url = html.unescape(str(url or "")).strip().strip(".,;:)}]\"'")
        if not url:
            continue
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        parsed = urllib.parse.urlsplit(url)
        if not parsed.netloc:
            continue
        clean = urllib.parse.urlunsplit((
            parsed.scheme or "https",
            parsed.netloc.lower(),
            parsed.path or "/",
            parsed.query,
            "",
        ))
        key = clean.lower().rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        out.append(clean)
    return out


def _fetch_url(url: str) -> tuple[str, str] | None:
    try:
        headers = dict(_REQUEST_HEADERS)
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme and parsed.netloc:
            headers["Referer"] = urllib.parse.urlunsplit((
                parsed.scheme, parsed.netloc, "/", "", ""
            ))
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read(2_000_000)
            enc = resp.headers.get_content_charset() or "utf-8"
            return resp.geturl(), raw.decode(enc, errors="replace")
    except Exception as exc:
        miab_log("errors", f"[AirportDir] Fetch failed for {url}: {exc}", None)
        return None


def _html_to_text(html_text: str) -> str:
    text = re.sub(r"<script.*?</script>|<style.*?</style>|<noscript.*?</noscript>", " ", html_text, flags=re.S | re.I)
    text = re.sub(r"</(?:p|div|li|tr|td|th|h[1-6]|section|article|br)>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    return "\n".join(lines)


def _host_key(host: str) -> str:
    host = (host or "").lower()
    return host[4:] if host.startswith("www.") else host


def _search_result_looks_official(url: str, title: str, snippet: str, query_norm: str) -> bool:
    if not url:
        return False
    parsed = urllib.parse.urlsplit(url)
    host = _host_key(parsed.netloc)
    if not host:
        return False
    if any(bad in host for bad in _REJECT_SEARCH_HOSTS):
        return False
    hay = normalise(f"{host} {parsed.path} {title} {snippet}")
    if not any(token in hay for token in _SOURCE_LINK_TOKENS):
        return False
    if not _search_result_matches_requested_airport(hay, host, query_norm):
        return False
    airport_words = ("airport", "aeroport", "aeropuerto", "aeroporto", "flughafen", "terminal")
    return any(word in hay or word in host for word in airport_words)


def _search_result_matches_requested_airport(hay: str, host: str, query_norm: str) -> bool:
    """Require search results to belong to the airport the user asked about.

    Search can return plausible but wrong "official airport" pages.  For a
    blind traveller, wrong-airport amenities are worse than no amenities.
    """
    identity_stop = {
        "airport", "airports", "terminal", "terminals", "official", "www",
        "com", "co", "nz", "au", "information", "shopping", "shop", "shops",
        "dining", "dine", "food", "facilities", "services", "amenities",
        "maps", "map", "international", "domestic", "security", "accessibility",
    }
    query_words = [
        w for w in query_norm.split()
        if len(w) > 3 and w not in identity_stop
    ]
    if query_words:
        return any(w in hay for w in query_words)

    code_match = re.fullmatch(r"[a-z]{3}", query_norm or "")
    if code_match:
        return bool(re.search(rf"\b{re.escape(query_norm)}\b", hay))

    return False
