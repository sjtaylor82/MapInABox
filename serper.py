"""serper.py — Google search results via the app's Cloudflare Worker proxy.

A thin client that returns real Google results as JSON (title, url, snippet)
for a query.  Used for in-app menu search, and structured so store-directory
and transit discovery can adopt it later.  Results cached for 30 days.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import urllib.error
import urllib.request
from typing import Optional

_CACHE_TTL_DAYS = 30
_URL = "https://google.serper.dev/search"

# Production: the app calls this Cloudflare Worker proxy, which holds the Serper
# key server-side and shares one cache across all users — so end users need no
# key.  The real Serper key lives only as a Worker secret and never ships here.
PROXY_URL = "https://miab-menu-search.miab.workers.dev"
_PROXY_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-AU,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


class SerperClient:
    def __init__(self, script_dir: Optional[str] = None, country: str = "au") -> None:
        import sys
        self._base = script_dir or getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
        self._country = (country or "au").strip() or "au"
        self._cache: dict = {}
        self._load_cache()

    @property
    def is_configured(self) -> bool:
        return bool(PROXY_URL)

    def search(self, query: str, num: int = 8) -> list[dict]:
        """Return up to `num` Google results as {"title","url","snippet"}.

        Uses the worker proxy. Cached 30 days per query. Returns [] on error.
        """
        query = (query or "").strip()
        if not query or not self.is_configured:
            return []
        num = max(1, min(int(num), 20))
        cache_key = "serper_v1_" + re.sub(r"[^a-z0-9]+", "_", query.lower())
        cached = self._get_cache(cache_key)
        if cached is not None:
            print(f"[Serper] cache hit for {query!r}")
            return cached

        try:
            results = self._search_proxy(query, num)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "ignore")[:300]
            except Exception:
                pass
            print(f"[Serper] HTTP {exc.code}: {detail}")
            return []
        except Exception as exc:
            print(f"[Serper] failed: {exc}")
            return []

        print(f"[Serper] returned {len(results)} result(s)")
        if results:
            self._set_cache(cache_key, results)
        return results

    def place_info(self, name: str, suburb: str = "") -> dict:
        """Return {title, rating, ratingCount, cid} for the venue via the proxy's
        places lookup, or {} if not found. Cached 30 days."""
        query = " ".join(p for p in ((name or "").strip(), (suburb or "").strip()) if p)
        if not query or not self.is_configured:
            return {}
        cache_key = "place_v1_" + re.sub(r"[^a-z0-9]+", "_", query.lower())
        cached = self._get_cache(cache_key)
        if cached is not None:
            print(f"[Serper] place cache hit for {query!r}")
            return cached
        params = urllib.parse.urlencode({"q": query, "type": "places", "gl": self._country})
        req = urllib.request.Request(f"{PROXY_URL}?{params}", headers=_PROXY_HEADERS)
        print(f"[Serper] place lookup: {query!r}")
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            print(f"[Serper] place lookup failed: {exc}")
            return {}
        place = data.get("place") or {}
        if place:
            self._set_cache(cache_key, place)
        return place

    def _search_proxy(self, query: str, num: int) -> list[dict]:
        """Call the Cloudflare Worker proxy (production — no user key needed)."""
        params = urllib.parse.urlencode({"q": query, "num": num, "gl": self._country})
        req = urllib.request.Request(f"{PROXY_URL}?{params}", headers=_PROXY_HEADERS)
        print(f"[Serper] proxy search: {query!r}")
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        out = []
        for item in data.get("results", []):  # proxy already returns title/url/snippet
            link = (item.get("url") or "").strip()
            if link:
                out.append({
                    "title":   (item.get("title") or link).strip(),
                    "url":     link,
                    "snippet": (item.get("snippet") or "").strip(),
                })
        return out

    def _cache_path(self) -> str:
        return os.path.join(self._base, "serper_cache.json")

    def _load_cache(self) -> None:
        try:
            p = self._cache_path()
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    self._cache = json.load(f)
        except Exception:
            self._cache = {}

    def _save_cache(self) -> None:
        try:
            with open(self._cache_path(), "w", encoding="utf-8") as f:
                json.dump(self._cache, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _get_cache(self, key: str):
        entry = self._cache.get(key)
        if not isinstance(entry, dict):
            return None
        if (time.time() - entry.get("ts", 0)) / 86400 > _CACHE_TTL_DAYS:
            return None
        return entry.get("data")

    def _set_cache(self, key: str, value) -> None:
        self._cache[key] = {"data": value, "ts": time.time()}
        self._save_cache()
