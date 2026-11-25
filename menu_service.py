"""menu_service.py — food menus from the Map in a Box web server.

The web edition's menu search (web/worker/menu.js) runs on the app's
Cloudflare Worker at expert.mapinabox.org. The desktop asks it rather than
keeping its own copy, so both get the same results:

  /api/menu      Menu links for one venue: its own menu pages and PDFs,
                 its store page on Uber Eats, DoorDash or Menulog (only when
                 it names this venue and suburb), photos of the menu on
                 RestaurantGuru, and its Facebook page. Readable menus first.
  /api/menutext  A menu page as sections of dishes with prices.

Answers are cached on the server for a week. find_menus raises
MenuServiceUnavailable when the server can't be reached (offline, or a
school network that blocks expert.mapinabox.org), so the caller can fall
back to its own search.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request

SERVER = "https://expert.mapinabox.org"
# Matches the web page's v=, so both share the server's cached answers.
_API_VERSION = "4"
_HEADERS = {"User-Agent": "MapInABox-desktop", "Accept": "application/json"}

_DELIVERY = {
    "ubereats.com": "Uber Eats",
    "doordash.com": "DoorDash",
    "menulog.com.au": "Menulog",
    "deliveroo.com.au": "Deliveroo",
}


class MenuServiceUnavailable(Exception):
    """The menu server could not be reached or gave no proper answer."""


def _ssl_context():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _get_json(path: str, params: dict, timeout: float) -> dict:
    url = f"{SERVER}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8")) | {"_status": exc.code}
        except Exception:
            raise MenuServiceUnavailable(f"HTTP {exc.code}") from exc
    except Exception as exc:
        raise MenuServiceUnavailable(str(exc)) from exc


def find_menus(name: str, suburb: str = "", website: str = "") -> list[dict]:
    """Menu results for a venue: [{kind, title, url, pdf, readable, ...}].

    kind is 'own' (the venue's own site or ordering page), 'delivery',
    'photo' (RestaurantGuru menu photos) or 'facebook'. [] means the server
    searched and found nothing.
    """
    params = {"name": name.strip()[:120], "suburb": (suburb or "").strip()[:80], "v": _API_VERSION}
    if website:
        params["website"] = website.strip()[:300]
    data = _get_json("/api/menu", params, timeout=45)
    if "results" not in data:
        raise MenuServiceUnavailable(data.get("error") or "no results in answer")
    return [r for r in data["results"] if isinstance(r, dict) and r.get("url")]


def can_read_here(result: dict) -> bool:
    """A menu page on the venue's own site that the server can read.

    PDFs are read in the browser on the web edition (pdf.js), so here they
    are opened instead."""
    return result.get("kind") == "own" and bool(result.get("readable")) and not result.get("pdf")


def _site(url: str) -> str:
    try:
        host = urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        return "link"
    host = host.removeprefix("www.")
    for domain, label in _DELIVERY.items():
        if host == domain or host.endswith("." + domain):
            return label
    return host or "link"


def result_label(result: dict, venue: str) -> str:
    """How a result is listed: "Ahmet's on Uber Eats", "Food Menu (PDF) on ahmets.com"."""
    kind = result.get("kind")
    url = result.get("url", "")
    title = (result.get("title") or "").strip()
    if kind == "delivery":
        return f"{venue} on {_site(url)}"
    if kind == "facebook":
        return f"{venue} on Facebook (menu photos may be in its posts)"
    if kind == "photo":
        age = result.get("age")
        return f"Photos of the menu on RestaurantGuru, newest {age or 'of unknown date'}"
    if result.get("pdf"):
        name = title if title and not title.lower().endswith(".pdf") else "Menu"
        label = f"{name} (PDF) on {_site(url)}"
    else:
        label = f"{title or 'Menu'} on {_site(url)}"
    if can_read_here(result):
        label += ", can be read here"
    return label


def summary(results: list[dict], venue: str) -> str:
    """"Ahmet's: 1 menu you can read here, 2 other menu pages, 1 delivery page."""
    def plural(n, word):
        return f"{n} {word}{'' if n == 1 else 's'}"

    readable = sum(1 for r in results if can_read_here(r))
    own = sum(1 for r in results if r.get("kind") == "own")
    photos = sum(1 for r in results if r.get("kind") == "photo")
    facebook = sum(1 for r in results if r.get("kind") == "facebook")
    delivery = len(results) - own - photos - facebook
    parts = [
        readable and f"{plural(readable, 'menu')} you can read here",
        own - readable and plural(own - readable, "other menu page"),
        photos and "photos of the menu",
        delivery and plural(delivery, "delivery page"),
        facebook and "its Facebook page",
    ]
    return f"{venue}: {', '.join(p for p in parts if p)}."


def _speakable_price(price: str) -> str:
    """"24" -> "$24"; "$9.50" and "Large $19.95" stay as they are."""
    price = price.strip()
    if price[:1].isdigit():
        price = "$" + price
    if price.lower().endswith("ea"):
        price = price[:-2].rstrip() + " each"
    return price


def read_menu(url: str) -> tuple[str, list[str]] | None:
    """A readable menu page as (heading, lines), or None if it can't be read.

    With at least three priced dishes, it's laid out by section:
    "Mains", "Beef burger, $22. With chips.". Otherwise the page's text,
    line by line, if there's enough of it to be a menu.
    """
    try:
        data = _get_json("/api/menutext", {"url": url}, timeout=25)
    except MenuServiceUnavailable:
        return None
    sections = [s for s in (data.get("sections") or []) if isinstance(s, dict)]
    priced = sum(1 for s in sections for i in s.get("items", []) if i.get("price"))
    if priced >= 3:
        lines, dishes = [], 0
        for s in sections:
            lines.append(s.get("title") or "Menu")
            for i in s.get("items", []):
                line = ", ".join(p for p in (i.get("name"), i.get("price") and _speakable_price(i["price"])) if p)
                if i.get("desc"):
                    line += f". {i['desc']}"
                lines.append("    " + line)
                dishes += 1
            lines.append("")
        n = len(sections)
        heading = f"{n} section{'' if n == 1 else 's'}, {dishes} dish{'' if dishes == 1 else 'es'}"
        return heading, lines
    text = data.get("text") or ""
    if len(text) < 200:
        return None
    return "", [line.strip() for line in text.splitlines() if line.strip()]
