"""mall_directory.py - Shopping-centre directory helpers for Map in a Box.

The main app fetches official centre website text via fetch_official_source_text(),
asks Mistral to extract store names, and uses describe_tenant() for detail views.

Public API
----------
fetch_official_source_text(directory_url, centre_name) -> tuple[str, list[str]]
describe_tenant(rec, centre_name) -> str
"""

from __future__ import annotations

import html
import re
import urllib.parse
import urllib.request

_CACHE_VERSION = "v4"


def fetch_official_source_text(directory_url: str, centre_name: str) -> tuple[str, list[str]]:
    """Fetch official directory page text plus same-domain candidate links."""
    url = (directory_url or "").strip()
    if not url:
        return "", []
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MapInABox/1.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            html_text = resp.read().decode("utf-8", errors="replace")
            page_url = resp.geturl()
    except Exception as exc:
        print(f"[MallDir] Official source fetch failed for {url}: {exc}")
        return "", []

    links = []
    base = urllib.parse.urlsplit(page_url)
    for href in re.findall(r'href\s*=\s*["\']([^"\']+)["\']', html_text, flags=re.I):
        abs_url = urllib.parse.urljoin(page_url, html.unescape(href))
        parsed = urllib.parse.urlsplit(abs_url)
        if parsed.netloc != base.netloc:
            continue
        href_low = f"{parsed.path}?{parsed.query}".lower()
        if any(token in href_low for token in ("store", "stores", "tenant", "tenants", "directory", "shop", "shops")):
            links.append(abs_url)

    texts = [f"SOURCE: {page_url}\n{_html_to_text(html_text)}"]
    seen_urls = {page_url}
    for link in dict.fromkeys(links[:12]):
        if link in seen_urls:
            continue
        seen_urls.add(link)
        try:
            req = urllib.request.Request(link, headers={"User-Agent": "MapInABox/1.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                link_html = resp.read().decode("utf-8", errors="replace")
        except Exception as exc:
            print(f"[MallDir] Official candidate fetch failed for {link}: {exc}")
            continue
        texts.append(f"SOURCE: {link}\n{_html_to_text(link_html)}")

    text = "\n\n".join(texts)
    if links:
        text += "\n\nCANDIDATE_LINKS:\n" + "\n".join(dict.fromkeys(links[:20]))
    return text, links[:20]


def _html_to_text(html_text: str) -> str:
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html_text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _normalise(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _decap_first(s: str) -> str:
    """Lowercase the first letter when joining mid-sentence, unless the word
    is an acronym or proper noun (heuristic: keep as-is if second char is also
    uppercase, e.g. 'IGA')."""
    s = (s or "").strip()
    if len(s) < 2:
        return s
    if s[0].isupper() and not s[1].isupper():
        return s[0].lower() + s[1:]
    return s


_LANDMARK_JOIN_RE = re.compile(r"(\s+(?:and|or)\s+|\s*;\s*)([A-Z][a-z])")


def _clean_landmark(s: str) -> str:
    """Strip trailing period, then lowercase the first letter of each clause
    that follows 'and' / 'or' / ';' — so multi-instance joins read naturally
    mid-sentence."""
    s = (s or "").strip().rstrip(".")
    return _LANDMARK_JOIN_RE.sub(lambda m: m.group(1) + m.group(2).lower(), s)


def describe_tenant(rec: dict, centre_name: str) -> str:
    """Format a screen-reader friendly description from a tenant record."""
    if not isinstance(rec, dict):
        return str(rec or "")
    name = rec.get("name") or ""
    parts: list[str] = []

    cat = rec.get("category") or ""
    cat_phrase = f" is a {cat.lower()}" if cat else ""
    floor = (rec.get("floor") or "").strip()
    landmark = _decap_first(_clean_landmark(rec.get("landmark") or ""))
    dist = rec.get("distance_m")
    bearing = rec.get("bearing") or ""

    if floor and landmark:
        parts.append(f"{name}{cat_phrase} on {floor}, {landmark}.")
    elif floor:
        parts.append(f"{name}{cat_phrase} on {floor}.")
    elif landmark:
        parts.append(f"{name}{cat_phrase}, {landmark}.")
    elif cat and dist is not None and bearing:
        parts.append(f"{name}{cat_phrase}, about {dist} metres {bearing} of the centre point.")
    elif dist is not None and bearing:
        parts.append(f"{name} is about {dist} metres {bearing} of the centre point.")
    elif cat:
        parts.append(f"{name}{cat_phrase}.")
    else:
        parts.append(name)

    nearby = [n for n in (rec.get("nearby") or []) if n]
    if nearby:
        parts.append("Nearby: " + ", ".join(nearby) + ".")

    addr = rec.get("address") or ""
    if addr and addr.lower() != f"{name.lower()}, {centre_name.lower()}":
        parts.append(f"Address: {addr}.")

    hours = rec.get("opening_hours") or []
    if hours:
        parts.append("Opening hours: " + "; ".join(h for h in hours if h) + ".")

    phone = rec.get("phone") or ""
    if phone:
        parts.append(f"Phone: {phone}.")

    www = rec.get("www") or ""
    if www:
        parts.append(f"Website: {www}.")

    source = (rec.get("source") or "").strip().lower()
    if source:
        source_label = {
            "here": "HERE",
            "osm": "OSM",
            "westfield": "Westfield",
            "mistral": "Mistral",
            "official": "Official directory",
        }.get(source, source.title())
        parts.append(f"Source: {source_label}.")

    return " ".join(parts).strip()
