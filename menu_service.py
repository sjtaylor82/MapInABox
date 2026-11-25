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
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request

SERVER = "https://expert.mapinabox.org"
# Matches the web page's v=, so both share the server's cached answers.
_API_VERSION = "5"
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


def is_picture_menu(result: dict) -> bool:
    """The menu as pictures: customers' photos of it, or a menu page whose menu
    is pictures (The Kelsey Cafe's menu boards)."""
    kind = result.get("kind")
    return kind == "photo" or (
        kind == "own" and bool(result.get("pictures"))
        and not result.get("readable") and not result.get("pdf"))


_ocr_ready = None


def ocr_available() -> bool:
    """Is Windows' built-in text recognition here to read menu pictures?"""
    global _ocr_ready
    if _ocr_ready is None:
        try:
            _ocr_engine()
            _ocr_ready = True
        except Exception:
            _ocr_ready = False
    return _ocr_ready


def can_read_here(result: dict) -> bool:
    """A menu that can be read in the app: a menu page on the venue's own site
    (the server reads it), or pictures of the menu (Windows reads their text).

    PDFs are read in the browser on the web edition (pdf.js), so here they
    are opened instead."""
    if is_picture_menu(result):
        return ocr_available()
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


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def updated_text(date: str) -> str:
    """When a menu was last changed: '2026-11-12' -> 'updated 12 Nov 2026',
    '2026-10' -> 'updated Oct 2026', or ''."""
    import re
    m = re.match(r"(\d{4})-(\d{2})(?:-(\d{2}))?", date or "")
    if not m or not 1 <= int(m.group(2)) <= 12:
        return ""
    day = f"{int(m.group(3))} " if m.group(3) else ""
    return f"updated {day}{_MONTHS[int(m.group(2)) - 1]} {m.group(1)}"


def result_label(result: dict, venue: str) -> str:
    """How a result is listed: "Ahmet's on Uber Eats", "Food Menu (PDF) on ahmets.com"."""
    kind = result.get("kind")
    url = result.get("url", "")
    title = (result.get("title") or "").strip()
    if kind == "delivery":
        return f"{venue} on {_site(url)}"
    if kind == "facebook":
        return f"{venue} on Facebook (menu photos may be in its posts)"
    readable = ", can be read here" if can_read_here(result) else ""
    if kind == "photo":
        age = result.get("age")
        return f"Customers' photos of the menu, newest {age or 'of unknown date'}{readable} (RestaurantGuru)"
    if is_picture_menu(result):
        n = int(result.get("pictures") or 0)
        what = f"{n} picture{'' if n == 1 else 's'} of the menu" if n else "pictures of the menu"
        return f"{title or 'Menu'} ({what}){readable} ({_site(url)})"
    # The menu, when it was updated, then the site:
    # "Food Menu (PDF), updated 12 Nov 2026 (ahmets.com)".
    if result.get("pdf"):
        name = title if title and not title.lower().endswith(".pdf") else "Menu"
        label = f"{name} (PDF)"
    else:
        label = title or "Menu"
    if result.get("updated"):
        label += f", {updated_text(result['updated'])}"
    return f"{label}{readable} ({_site(url)})"


def summary(results: list[dict], venue: str) -> str:
    """"Ahmet's: 1 menu you can read here, 2 other menu pages, 1 delivery page."""
    def plural(n, word):
        return f"{n} {word}{'' if n == 1 else 's'}"

    readable = sum(1 for r in results if can_read_here(r) and r.get("kind") == "own")
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


# ---- Menu pictures, read by Windows' text recognition -----------------------

def _ocr_engine():
    """Windows' built-in text recognition (Windows 10 and 11), as user_maps uses."""
    try:
        from winrt.windows.globalization import Language
        from winrt.windows.media.ocr import OcrEngine
    except ImportError:
        from winsdk.windows.globalization import Language
        from winsdk.windows.media.ocr import OcrEngine
    engine = (OcrEngine.try_create_from_user_profile_languages()
              or OcrEngine.try_create_from_language(Language("en")))
    if engine is None:
        raise ImportError("Windows has no text recognition language installed.")
    return engine


def menu_picture_urls(page_url: str) -> list[str]:
    """The pictures of a menu (newest version), as the menu server lists them."""
    data = _get_json("/api/menuphotos", {"url": page_url}, timeout=30)
    versions = [v for v in (data.get("versions") or []) if isinstance(v, dict)]
    return [u for u in (versions[0].get("images") or []) if isinstance(u, str)] if versions else []


def _fetch_picture(url: str) -> bytes:
    """One menu picture, through the menu server (sites refuse other callers)."""
    req = urllib.request.Request(
        f"{SERVER}/api/menuimage?{urllib.parse.urlencode({'u': url})}",
        headers={"User-Agent": _HEADERS["User-Agent"]})
    with urllib.request.urlopen(req, timeout=30, context=_ssl_context()) as resp:
        return resp.read()


_PRICE_ONLY = re.compile(r"^(?:[$]?\s?\d{1,3}(?:[.,]\d{1,2})?\s*(?:/|\+)?\s*)+$")


def _rows(lines, width: float = 0) -> list[str]:
    """Recognised lines in reading order: column by column, top to bottom, with a
    price beside a dish joined onto it ("The Kelsey Breakfast  32.0").

    lines: [(left, top, bottom, text)]; width: the picture's width. A line
    starting well to the right of the others (over a quarter of the width)
    begins another column, unless it is only a price."""
    width = width or max((l[0] for l in lines), default=0) * 1.3 or 1
    texts = [l for l in lines if not _PRICE_ONLY.match(l[3])]
    prices = [l for l in lines if _PRICE_ONLY.match(l[3])]
    # Columns: where the lines start, with a jump of over a quarter of the width between them.
    starts = sorted(l[0] for l in texts)
    edges = [starts[0]] if starts else []
    for a, b in zip(starts, starts[1:]):
        if b - a > width * 0.25:
            edges.append(b)
    column = lambda left: max([i for i, e in enumerate(edges) if left >= e - 1] or [0])
    rows = [{"col": column(l[0]), "left": l[0], "top": l[1], "bottom": l[2], "text": l[3]} for l in texts]
    # Each price onto the text line beside it, to its left, on the same height.
    for left, top, bottom, text in prices:
        mid = (top + bottom) / 2
        beside = [r for r in rows if r["left"] < left and r["top"] - 2 <= mid <= r["bottom"] + 2]
        if beside:
            max(beside, key=lambda r: r["left"])["text"] += f"  {text}"
        else:
            rows.append({"col": column(left), "left": left, "top": top, "bottom": bottom, "text": text})
    rows.sort(key=lambda r: (r["col"], r["top"], r["left"]))
    return [r["text"] for r in rows]


def _ocr_picture(data: bytes) -> list[str]:
    """The text of one menu picture, row by row."""
    import asyncio
    import io
    import os
    import tempfile
    from PIL import Image
    try:
        from winrt.windows.graphics.imaging import BitmapDecoder
        from winrt.windows.storage import FileAccessMode, StorageFile
        from winrt.windows.media.ocr import OcrEngine
    except ImportError:
        from winsdk.windows.graphics.imaging import BitmapDecoder
        from winsdk.windows.storage import FileAccessMode, StorageFile
        from winsdk.windows.media.ocr import OcrEngine
    engine = _ocr_engine()
    limit = int(OcrEngine.max_image_dimension or 4096)

    async def read(path):
        file = await StorageFile.get_file_from_path_async(path)
        stream = await file.open_async(FileAccessMode.READ)
        decoder = await BitmapDecoder.create_async(stream)
        bitmap = await decoder.get_software_bitmap_async()
        return await engine.recognize_async(bitmap)

    with Image.open(io.BytesIO(data)) as image:
        source = image.convert("RGB")
    # Small print reads better enlarged (as user_maps found), up to Windows' limit.
    scale = max(1, min(3, round(2400 / max(1, max(source.size)))))
    while scale > 1 and max(source.size) * scale > limit:
        scale -= 1
    if scale > 1:
        source = source.resize((source.width * scale, source.height * scale), Image.Resampling.BICUBIC)
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "menu.png")
        source.save(path)
        result = asyncio.run(read(os.path.abspath(path)))
    found = []
    for line in result.lines:
        rects = [w.bounding_rect for w in line.words]
        if rects and line.text.strip():
            found.append((min(r.x for r in rects), min(r.y for r in rects),
                          max(r.y + r.height for r in rects), line.text.strip()))
    return _rows(found, source.width)


def read_pictures(page_url: str) -> tuple[str, list[str]] | None:
    """Pictures of a menu as (heading, lines) read by Windows' text recognition,
    each picture under "Picture 1 of 5"; None if none could be read."""
    try:
        urls = menu_picture_urls(page_url)
    except MenuServiceUnavailable:
        return None
    lines, read = [], 0
    for i, url in enumerate(urls, 1):
        try:
            rows = _ocr_picture(_fetch_picture(url))
        except Exception:
            continue
        if not rows:
            continue
        read += 1
        lines.append(f"Picture {i} of {len(urls)}")
        lines.extend("    " + r for r in rows)
        lines.append("")
    if not read:
        return None
    heading = (f"{read} picture{'' if read == 1 else 's'} of the menu, "
               "read by Windows' text recognition (check prices with the venue)")
    return heading, lines


def read_result(result: dict) -> tuple[str, list[str]] | None:
    """Read a menu result in the app: its pictures, or its page."""
    if is_picture_menu(result):
        return read_pictures(result.get("url", ""))
    return read_menu(result.get("url", ""))
