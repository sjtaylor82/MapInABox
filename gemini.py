"""gemini.py — All Gemini AI queries for Map in a Box.

One module, one client, one cache file.  Core.py imports GeminiClient and
calls clean methods — no prompts, no JSON parsing, no google-genai imports
live anywhere else.

Classes
-------
GeminiClient
    init(api_key)
    is_configured  → bool
    ask_transit(lat, lon, place_name)  → list[dict]
    ask_times(operator, service, route_name)  → str
    ask_shopping(centre_name, lat, lon)  → list[dict]
    ask_menu_links(name, kind, address_or_coords, website)  → list[str]
"""

import json
import os
import time
import urllib.parse
import urllib.request
from typing import Optional

GEMINI_MODEL       = "gemini-2.5-flash-lite"
GEMINI_MENU_MODEL  = "gemini-2.5-flash-lite"
GEMINI_THINKING_BUDGET = 0
_CACHE_TTL_DAYS       = 90
_MENU_CACHE_TTL_DAYS  = 30
class GeminiClient:
    """Single Gemini client shared across all Map in a Box AI features.

    Parameters
    ----------
    script_dir:
        Directory where ``gemini_cache.json`` is stored.
        Defaults to the directory containing this file.
    """

    def __init__(self, script_dir: Optional[str] = None) -> None:
        import sys
        self._base   = script_dir or getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
        self._client = None
        self._cache: dict = {}
        self._load_cache()

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def init(self, api_key: str) -> None:
        """Initialise (or re-initialise) the Gemini client.  Safe with empty string."""
        if not api_key or not api_key.strip():
            self._client = None
            print("[Gemini] No API key provided — Gemini disabled.")
            return
        try:
            from google import genai
            self._client = genai.Client(api_key=api_key.strip())
            print("[Gemini] Client initialised.")
        except Exception as exc:
            self._client = None
            print(f"[Gemini] Init failed: {exc}")

    @property
    def is_configured(self) -> bool:
        return self._client is not None

    # ------------------------------------------------------------------
    # Transit — regional routes + stops
    # ------------------------------------------------------------------

    def ask_transit(
        self, lat: float, lon: float, place_name: str = "this location"
    ) -> list[dict]:
        """Return regional routes serving *place_name*.

        Each dict: operator, service, route_name, stops (list[str]).
        Cached 90 days.
        """
        if not self.is_configured:
            print("[Gemini] Not configured — skipping transit query.")
            return []

        cache_key = f"transit_{round(lat, 2)}_{round(lon, 2)}"
        cached = self._get_cache(cache_key)
        if cached is not None:
            print(f"[Gemini] Transit cache hit for {place_name}.")
            return cached

        prompt = (
            f"List every REGIONAL and LONG-DISTANCE public transport route "
            f"(coach, regional bus, regional train, ferry) "
            f"that serves or stops at '{place_name}' at coordinates {lat:.4f}, {lon:.4f}. "
            f"Do NOT include local urban or suburban routes. "
            f"Include ALL regional operators serving this stop. "
            f"For each route list every stop in order from first to last. "
            f"Return ONLY a JSON array, no explanation, no markdown:\n"
            f"[\n"
            f"  {{\n"
            f"    \"operator\": \"Operator name\",\n"
            f"    \"service\": \"Route number or code\",\n"
            f"    \"route_name\": \"Origin to Destination\",\n"
            f"    \"stops\": [\"Stop A\", \"Stop B\", \"Stop C\"]\n"
            f"  }}\n"
            f"]\n"
            f"route_name MUST be 'Origin to Destination' — never '?' or 'Unknown'. "
            f"Only include routes that genuinely serve this stop. "
            f"Do not invent routes or stops."
        )

        try:
            print(f"[Gemini] place_name='{place_name}' lat={lat} lon={lon}")
            print(f"[Gemini] Querying regional transit at '{place_name}'…")
            text   = self._grounded_query(prompt, label="transit")
            routes = self._parse_json_list(text)
            print(f"[Gemini] _parse_json_list returned {len(routes)} item(s)")
            if routes:
                print(f"[Gemini] First item keys: {list(routes[0].keys()) if isinstance(routes[0], dict) else type(routes[0])}")
            clean  = [
                {
                    "operator":   str(r.get("operator",   "")).strip(),
                    "service":    str(r.get("service",    "")).strip(),
                    "route_name": str(r.get("route_name", "")).strip(),
                    "stops":      [str(s) for s in r.get("stops", []) if s],
                }
                for r in routes if isinstance(r, dict)
                if r.get("operator") and r.get("service")
            ]
            print(f"[Gemini] {len(clean)} regional route(s) returned.")
            if clean:
                self._set_cache(cache_key, clean)
            return clean
        except Exception as exc:
            print(f"[Gemini] Transit query failed: {exc}")
            return []

    # ------------------------------------------------------------------
    # Transit — plain-English timetable summary
    # ------------------------------------------------------------------

    def ask_times(
        self, operator: str, service: str, route_name: str = ""
    ) -> str:
        """Return a plain-English timetable summary for one service.

        Cached 90 days.  Returns a human-readable string.
        """
        if not self.is_configured:
            return "Gemini not configured."

        # Key on operator+service only — direction (route_name) is irrelevant
        # for timetable data, and this prevents cache misses on reverse direction
        cache_key = (f"times_{operator}_{service}"
                     .lower()
                     .replace(" ", "_")
                     .replace("(", "")
                     .replace(")", "")
                     .replace(".", ""))
        cached = self._get_cache(cache_key, text=True)
        if cached is not None:
            print(f"[Gemini] Times cache hit for {operator} {service}.")
            return cached

        desc = f"{service} ({route_name})" if route_name else service
        prompt = (
            f"Describe the timetable for the {operator} service {desc} "
            f"in plain English. "
            f"Include: frequency, approximate first and last service, "
            f"and any differences on weekends or public holidays. "
            f"Be concise — two or three sentences. "
            f"If you are not certain about specific times, say so rather than guessing."
        )

        try:
            print(f"[Gemini] Fetching times for {operator} {service}…")
            text = self._grounded_query(prompt, label="times")
            if text:
                self._set_cache(cache_key, text, text=True)
                return text
        except Exception as exc:
            print(f"[Gemini] Times query failed: {exc}")

        return "Could not retrieve timetable information."

    # ------------------------------------------------------------------
    # Shopping — store directory
    # ------------------------------------------------------------------

    def ask_shopping(
        self, centre_name: str, lat: float, lon: float
    ) -> list[str]:
        """Return store names for *centre_name* — fast, names only.

        Cached 90 days.  Returns a sorted list of store name strings.
        """
        if not self.is_configured:
            print("[Gemini] Not configured — skipping shopping query.")
            return []

        cache_key = f"shop_names_{centre_name.lower().strip()}"
        cached = self._get_cache(cache_key)
        if cached is not None:
            print(f"[Gemini] Shopping cache hit for {centre_name}.")
            return cached

        prompt = (
            f"Search the web for the current tenant list of '{centre_name}' "
            f"shopping centre in Australia (near {lat:.4f}, {lon:.4f}). "
            f"Include ALL stores — specialty, food, services, department stores. "
            f"Return the names in strict alphabetical order (A to Z). "
            f"Return ONLY a JSON array of store name strings — "
            f"no floors, no categories, no explanation, no markdown:\n"
            f'["Store A", "Store B", "Store C"]'
        )

        try:
            print(f"[Gemini] Querying store names (grounded) for '{centre_name}'\u2026")
            text = self._grounded_query(prompt, label="shopping")
            names = self._parse_json_list(text)
            clean = sorted({str(n).strip() for n in names if n and str(n).strip()},
                           key=str.lower)
            print(f"[Gemini] {len(clean)} store names returned.")
            if clean:
                self._set_cache(cache_key, clean)
            return clean
        except Exception as exc:
            print(f"[Gemini] Shopping query failed: {exc}")
            return []

    def ask_store_detail(
        self, store_name: str, centre_name: str
    ) -> str:
        """Return plain-English location detail for one store.

        e.g. "Ground floor, near the food court entrance."
        Cached 90 days.  Returns a string.
        """
        if not self.is_configured:
            return "Gemini not configured."

        cache_key = (f"store_{centre_name}_{store_name}"
                     .lower().replace(" ", "_"))
        cached = self._get_cache(cache_key, text=True)
        if cached is not None:
            print(f"[Gemini] Store detail cache hit for {store_name}.")
            return cached

        prompt = (
            f"In '{centre_name}' shopping centre, where is {store_name} located? "
            f"Give the floor level and what it is near (e.g. near the food court, "
            f"near the main entrance). One or two sentences maximum. "
            f"If you are not certain, say so."
        )

        try:
            print(f"[Gemini] Fetching store detail for '{store_name}'…")
            text = self._grounded_query(prompt, label="store_detail")
            if text:
                self._set_cache(cache_key, text, text=True)
                return text
        except Exception as exc:
            print(f"[Gemini] Store detail query failed: {exc}")

        return "Location details not available."

    # ------------------------------------------------------------------
    # Menu links — URL discovery only, no menu extraction
    # ------------------------------------------------------------------

    def search_menu_links_places(
        self,
        name: str,
        suburb: str = "",
        region: str = "",
        country: str = "",
        api_key: str = "",
        lat: float = 0.0,
        lon: float = 0.0,
    ) -> tuple[list[str], str]:
        """Find menu links via Google Places + path probing.
        Returns (urls, places_website) — urls may be empty if probing found nothing."""
        if not api_key or not name:
            return [], ""

        location_str = ", ".join(p for p in [suburb, region, country] if p)
        cache_key = (
            f"menu_places_v4_{name}_{suburb}_{region}_{country}"
            .lower().replace(" ", "_").replace(",", "").replace(".", "")
        )
        entry = self._cache.get(cache_key)
        if isinstance(entry, dict):
            if (time.time() - entry.get("ts", 0)) / 86400 <= _MENU_CACHE_TTL_DAYS:
                cached = entry.get("data")
                if cached:
                    print(f"[Places] Menu cache hit for {name}.")
                    return cached, ""

        # Step 1a — find place_id via findplacefromtext
        query = f"{name} {location_str}".strip()
        location_restrict = (f"&locationrestrict=circle:50000@{lat},{lon}"
                              if lat and lon else "")
        find_url = (
            "https://maps.googleapis.com/maps/api/place/findplacefromtext/json"
            f"?input={urllib.parse.quote(query)}"
            "&inputtype=textquery"
            "&fields=place_id,name"
            f"&key={urllib.parse.quote(api_key)}"
            f"{location_restrict}"
        )
        try:
            print(f"[Places] Finding place for '{query}'…")
            req = urllib.request.Request(find_url, headers={"User-Agent": "MapInABox/1.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode())
            print(f"[Places] findplacefromtext response: {data}")
            candidates_list = data.get("candidates") or []
            place_id = candidates_list[0].get("place_id", "") if candidates_list else ""
            print(f"[Places] place_id: {place_id!r}")
        except Exception as exc:
            print(f"[Places] findplacefromtext failed: {exc}")
            return [], ""

        if not place_id:
            return [], ""

        # Step 1b — fetch website from Place Details
        details_url = (
            "https://maps.googleapis.com/maps/api/place/details/json"
            f"?place_id={urllib.parse.quote(place_id)}"
            "&fields=website"
            f"&key={urllib.parse.quote(api_key)}"
        )
        try:
            req = urllib.request.Request(details_url, headers={"User-Agent": "MapInABox/1.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                detail_data = json.loads(resp.read().decode())
            website = detail_data.get("result", {}).get("website", "")
            print(f"[Places] Website: {website!r}")
        except Exception as exc:
            print(f"[Places] Place Details failed: {exc}")
            return [], ""

        if not website:
            print(f"[Places] No website found for {name}.")
            return [], ""

        # Step 2 — probe common menu paths on the domain
        parsed = urllib.parse.urlparse(website)
        base = f"{parsed.scheme}://{parsed.netloc}"
        location_path = parsed.path.rstrip("/")
        suburb_slug = suburb.lower().replace(" ", "-") if suburb else ""

        browser_headers = {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/124.0.0.0 Safari/537.36"),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-AU,en;q=0.9",
        }

        candidates = []
        if location_path and location_path not in ("", "/"):
            candidates += [
                f"{base}{location_path}/menu/",
                f"{base}{location_path}/menu",
            ]
        if suburb_slug:
            candidates += [
                f"{base}/location/{suburb_slug}/menu/",
                f"{base}/locations/{suburb_slug}/menu/",
            ]
        candidates += [
            f"{base}/menu/",
            f"{base}/menu",
            f"{base}/our-menu",
        ]

        found = []
        seen = set()
        for url in candidates:
            if url in seen:
                continue
            seen.add(url)
            try:
                req = urllib.request.Request(url, headers=browser_headers)
                with urllib.request.urlopen(req, timeout=5) as resp:
                    final = resp.url
                    if final not in found:
                        found.append(final)
                        print(f"[Places] ✓ {url} → {final}")
                    if len(found) >= 3:
                        break
            except Exception as e:
                print(f"[Places] ✗ {url} → {type(e).__name__}: {e}")

        print(f"[Places] Verified menu URLs: {found}")
        if found:
            self._set_cache(cache_key, found)
        return found, website

    # ------------------------------------------------------------------

    def ask_menu_links(
        self,
        name: str,
        kind: str = "food outlet",
        address_or_coords: str = "",
        website: str = "",
        country: str = "",
        region: str = "",
    ) -> list[str]:
        """Return likely menu-related URLs for a food business.

        This deliberately returns links only. It does not extract or structure menu content.
        """
        if not self.is_configured:
            print("[Gemini] Not configured — skipping menu link query.")
            return []

        safe_name = str(name or "").strip()
        safe_kind = str(kind or "food outlet").strip() or "food outlet"
        safe_location = str(address_or_coords or "").strip()
        safe_website = str(website or "").strip()
        safe_country = str(country or "").strip()
        safe_region = str(region or "").strip()
        if not safe_name:
            return []

        location_str = ", ".join(p for p in [safe_location, safe_region, safe_country] if p)

        cache_key = (
            f"menu_links_v7_{safe_name}_{safe_kind}_{safe_location}_{safe_region}_{safe_country}"
            .lower()
            .replace(" ", "_")
            .replace("/", "_")
            .replace("\\", "_")
            .replace("(", "")
            .replace(")", "")
            .replace(".", "")
            .replace(",", "")
        )
        entry = self._cache.get(cache_key)
        if isinstance(entry, dict):
            age_days = (time.time() - entry.get("ts", 0)) / 86400
            if age_days <= _MENU_CACHE_TTL_DAYS:
                cached = entry.get("data")
                if cached:
                    print(f"[Gemini] Menu link cache hit for {safe_name}.")
                    return cached

        kind_str = f" ({safe_kind})" if safe_kind and safe_kind != "food outlet" else ""
        prompt = (
            f"Link me to the menu or products page for {safe_name}{kind_str} in {location_str}.\n"
            f"Return the direct URL to the menu or products page — not the homepage.\n"
            f"Prefer the location-specific menu page for {location_str} over a generic menu page.\n"
            f"Also include delivery aggregators (Uber Eats, DoorDash, OpenTable) for this location if available.\n"
            f"One URL per line. No explanation."
        )

        try:
            print(f"[Gemini] Finding menu links for '{safe_name}'…")
            from google.genai import types
            resp = self._client.models.generate_content(
                model=GEMINI_MENU_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                ),
            )
            text = self._extract_text(resp)
            raw_grounding = self._extract_grounding_urls(resp)

            # Follow grounding-api-redirect wrappers to get the real destination URL.
            # Also drop any entry containing a newline — those are whole-text false positives.
            import urllib.request as _ureq
            resolved = []
            for u in raw_grounding:
                if "\n" in u or " " in u:
                    continue  # whole-text blob, not a real URL
                if "grounding-api-redirect" in u or "vertexaisearch" in u:
                    try:
                        req = _ureq.Request(u, headers={"User-Agent": "Mozilla/5.0"})
                        with _ureq.urlopen(req, timeout=5) as r:
                            resolved.append(r.url)
                            print(f"[Gemini] Redirect {u[:60]}… → {r.url}")
                    except Exception as e:
                        print(f"[Gemini] Redirect failed: {e}")
                else:
                    resolved.append(u)

            text_urls = [u for u in self._parse_url_list(text) if "\n" not in u and " " not in u]

            print(f"[Gemini] Menu text response: {repr(text[:300]) if text else '(empty)'}")
            print(f"[Gemini] Resolved grounding URLs: {resolved}")

            if not text and not resolved:
                print(f"[Gemini] No response from menu search.")
                return []

            all_urls = resolved + text_urls
            print(f"[Gemini] All URLs before filter: {all_urls}")
            clean = self._clean_url_list(all_urls)

            print(f"[Gemini] Found {len(clean)} menu link(s) after filter: {clean}")
            if clean:
                self._set_cache(cache_key, clean)
            return clean
        except Exception as exc:
            print(f"[Gemini] Menu link query failed: {exc}")
            return []

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Satellite vision — accessibility description of aerial imagery
    # ------------------------------------------------------------------

    def describe_satellite_image(self, image_bytes: bytes, cache_key: str = "") -> str:
        """Return a rich plain-English description of a satellite image.

        Uses Gemini vision (no grounding needed — image is the source).
        Cached 90 days.  Returns a string, or "" on failure.
        """
        if not self.is_configured:
            return ""

        if cache_key:
            cached = self._get_cache(cache_key, text=True)
            if cached:
                print(f"[Gemini] Satellite cache hit for {cache_key}.")
                return cached

        prompt = (
            "You are describing a satellite or aerial image for a blind person "
            "who cannot see it. Describe the landscape in rich, practical detail: "
            "terrain type, land use, vegetation, water bodies, roads, settlements, "
            "and any notable features. Be specific and vivid. "
            "Two to four sentences."
        )

        from google.genai import types
        print(f"[Gemini] Describing satellite image ({len(image_bytes)} bytes)…")
        last_exc = None
        for attempt in range(4):
            if attempt:
                wait = 2 ** attempt   # 2, 4, 8 seconds
                print(f"[Gemini] Satellite retry {attempt} in {wait}s…")
                time.sleep(wait)
            try:
                resp = self._client.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=[
                        types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                        types.Part.from_text(text=prompt),
                    ],
                    config=types.GenerateContentConfig(
                        thinking_config=types.ThinkingConfig(
                            thinking_budget=GEMINI_THINKING_BUDGET
                        )
                    ),
                )
                text = self._extract_text(resp)
                if text:
                    return text
            except Exception as exc:
                last_exc = exc
                msg = str(exc)
                if "503" in msg or "UNAVAILABLE" in msg or "429" in msg or "quota" in msg.lower():
                    print(f"[Gemini] Satellite description failed (attempt {attempt+1}): {exc}")
                    continue   # retryable
                break          # non-retryable — don't wait
        print(f"[Gemini] Satellite description gave up after retries: {last_exc}")
        return ""

    def describe_streetview_images(
        self,
        image_bytes_list: list,
        headings: list,
    ) -> str:
        """Return a plain-English description of one or two Street View images.

        image_bytes_list: 1 or 2 JPEG byte strings.
        headings: matching list of compass bearings (degrees) for each image.
        Caching handled by caller (streetview.py). Returns a string, or "" on failure.
        """
        if not self.is_configured:
            return ""


        from google.genai import types

        def _cardinal(h: float) -> str:
            dirs = [
                "north", "north-east", "east", "south-east",
                "south", "south-west", "west", "north-west",
            ]
            return dirs[round(h / 45) % 8]

        if len(image_bytes_list) == 2:
            dir_a = _cardinal(headings[0])
            dir_b = _cardinal(headings[1])
            prompt = (
                f"You are helping a blind person understand what is around them using two Google Street View images. "
                f"The first image faces {dir_a}; the second faces {dir_b}. "
                f"Your job is to describe the scene. list named businesses, services, or landmarks visible in each image, "
                f"in left-to-right order as seen in the image. "
                f"For each one state: its name (read from signage), what type of place it is, and which side of the street it is on. "
                f"Note any visible entrance features such as steps, ramps, or automatic doors. "
                f"If a residential front, describe parked vehicles. "
                f"10 sentences maximum. Be specific and factual. Do not exclude information."
            )
            contents = [
                types.Part.from_bytes(data=image_bytes_list[0], mime_type="image/jpeg"),
                types.Part.from_bytes(data=image_bytes_list[1], mime_type="image/jpeg"),
                types.Part.from_text(text=prompt),
            ]
        else:
            dir_a = _cardinal(headings[0]) if headings else "north"
            prompt = (
                f"You are describing a Google Street View image for a blind person. "
                f"The image faces {dir_a}. "
                f"Describe what businesses, buildings, or landmarks are visible, "
                f"reading from left to right as seen in the image. "
                f"Note signage, shop types, and any access features such as steps or ramps. "
                f"Do not comment on how busy the street looks. "
                f"Two to four sentences."
            )
            contents = [
                types.Part.from_bytes(data=image_bytes_list[0], mime_type="image/jpeg"),
                types.Part.from_text(text=prompt),
            ]

        print(f"[Gemini] Describing Street View ({len(image_bytes_list)} image(s))...")
        last_exc = None
        for attempt in range(4):
            if attempt:
                wait = 2 ** attempt
                print(f"[Gemini] Street View retry {attempt} in {wait}s...")
                time.sleep(wait)
            try:
                resp = self._client.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        thinking_config=types.ThinkingConfig(
                            thinking_budget=GEMINI_THINKING_BUDGET
                        )
                    ),
                )
                text = self._extract_text(resp)
                if text:
                    return text
            except Exception as exc:
                last_exc = exc
                msg = str(exc)
                if "503" in msg or "UNAVAILABLE" in msg or "429" in msg or "quota" in msg.lower():
                    print(f"[Gemini] Street View description failed (attempt {attempt+1}): {exc}")
                    continue
                break
        print(f"[Gemini] Street View description gave up after retries: {last_exc}")
        return ""

    def _extract_text(self, resp) -> str:
        """Extract text from a Gemini response, checking candidates fallback."""
        direct = getattr(resp, "text", "") or ""
        if direct:
            return direct.strip()
        candidates = getattr(resp, "candidates", None) or []
        parts = []
        for cand in candidates:
            content = getattr(cand, "content", None)
            for part in (getattr(content, "parts", None) or []):
                text = getattr(part, "text", "") or ""
                if text:
                    parts.append(text)
        return "".join(parts).strip()

    def _grounded_query(self, prompt: str, label: str = "") -> str:
        """Run a grounded Gemini query and return the raw text."""
        from google.genai import types
        resp = self._client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                thinking_config=types.ThinkingConfig(
                    thinking_budget=GEMINI_THINKING_BUDGET
                ),
                tools=[types.Tool(google_search=types.GoogleSearch())],
            ),
        )
        return self._extract_text(resp)

    @staticmethod
    def _parse_url_list(text: str) -> list[str]:
        """Parse a JSON URL array, or fall back to URL regex extraction."""
        text = str(text or "").strip()
        if not text:
            return []

        # Strip markdown fences if Gemini ignores the instruction.
        if "```" in text:
            parts = text.split("```")
            for part in parts:
                p = part.strip()
                if p.startswith("json"):
                    p = p[4:].strip()
                if p.startswith("["):
                    text = p
                    break

        values = []
        start = text.find("[")
        if start != -1:
            depth = 0
            end = -1
            for i in range(start, len(text)):
                if text[i] == "[":
                    depth += 1
                elif text[i] == "]":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            if end != -1:
                try:
                    parsed = json.loads(text[start:end])
                    if isinstance(parsed, list):
                        for item in parsed:
                            if isinstance(item, str):
                                values.append(item)
                            elif isinstance(item, dict):
                                u = item.get("url") or item.get("uri") or item.get("link")
                                if u:
                                    values.append(str(u))
                except Exception:
                    pass

        if not values:
            import re
            values = re.findall(r"https?://[^\s\]>)\"']+", text)
        return values

    @staticmethod
    def _is_bad_menu_url(url: str) -> bool:
        """Reject opaque redirect/search URLs that are not real menu destinations."""
        try:
            import urllib.parse
            parsed = urllib.parse.urlparse(str(url or ""))
            host = (parsed.netloc or "").lower()
            path = (parsed.path or "").lower()
            query = (parsed.query or "").lower()
            full = str(url or "").lower()
        except Exception:
            return True

        if not host:
            return True

        blocked_hosts = (
            "vertexaisearch.cloud.google.com",
            "googleapis.com",
            "googleusercontent.com",
            "gstatic.com",
        )
        if any(host == h or host.endswith("." + h) for h in blocked_hosts):
            return True

        if "grounding-api-redirect" in full:
            return True

        # Google/Bing/etc result pages are not menu destinations.
        if host in {"google.com", "www.google.com", "bing.com", "www.bing.com", "duckduckgo.com", "www.duckduckgo.com"}:
            if path.startswith(("/url", "/search", "/aclk")) or "q=" in query or "url=" in query:
                return True

        # Generic redirect/tracking wrappers with an encoded destination are not safe to show.
        if any(part in path for part in ("/redirect", "/redir", "/url")) and any(k in query for k in ("url=", "u=", "target=")):
            return True

        # Bare homepages (no meaningful path) are not menu destinations.
        if path in ("", "/") and not query:
            return True

        return False

    @staticmethod
    def _clean_url_list(urls: list) -> list[str]:
        """Normalise, dedupe, and filter returned URLs. Exclude pre-2024 dates."""
        import re
        clean = []
        seen = set()
        for url in urls or []:
            url = str(url or "").strip().strip(".,;:)}]\"'")
            if not url.lower().startswith(("http://", "https://")):
                continue

            # Reject URLs with obvious old dates (pre-2024)
            old_date_pattern = r'/(19\d{2}|20(?:0[0-9]|1[0-9]|2[0-3]))[/-]'
            if re.search(old_date_pattern, url):
                print(f"[Gemini] Rejected pre-2024 menu URL: {url}")
                continue

            # Drop common tracking fragments/UTMs while preserving useful query args.
            try:
                import urllib.parse
                parsed = urllib.parse.urlparse(url)
                query_items = [
                    (k, v) for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
                    if not k.lower().startswith("utm_")
                ]
                url = urllib.parse.urlunparse(parsed._replace(
                    query=urllib.parse.urlencode(query_items, doseq=True),
                    fragment="",
                ))
            except Exception:
                pass
            if GeminiClient._is_bad_menu_url(url):
                print(f"[Gemini] Rejected non-destination menu URL: {url}")
                continue
            key = url.lower().rstrip("/")
            if key in seen:
                continue
            seen.add(key)
            clean.append(url)
            if len(clean) >= 8:
                break
        return clean

    @staticmethod
    def _extract_grounding_urls(resp) -> list[str]:
        """Best-effort extraction of URLs from Gemini grounding metadata."""
        urls = []
        seen_objs = set()

        def visit(obj, depth=0):
            if obj is None or depth > 8:
                return
            oid = id(obj)
            if oid in seen_objs:
                return
            seen_objs.add(oid)

            if isinstance(obj, str):
                if obj.startswith(("http://", "https://")):
                    urls.append(obj)
                return
            if isinstance(obj, dict):
                for key, value in obj.items():
                    if key in ("url", "uri", "link") and isinstance(value, str):
                        urls.append(value)
                    else:
                        visit(value, depth + 1)
                return
            if isinstance(obj, (list, tuple, set)):
                for item in obj:
                    visit(item, depth + 1)
                return

            if hasattr(obj, "model_dump"):
                try:
                    visit(obj.model_dump(), depth + 1)
                    return
                except Exception:
                    pass
            for attr in ("candidates", "grounding_metadata", "grounding_chunks", "web", "uri", "url"):
                try:
                    visit(getattr(obj, attr), depth + 1)
                except Exception:
                    pass

        visit(resp)
        return urls

    @staticmethod
    def _parse_json_list(text: str) -> list:
        """Extract and parse the first balanced JSON array from *text*.

        Uses bracket counting so trailing citation text like [1] or [2]
        doesn't cause rfind to grab the wrong closing bracket.
        """
        # Strip markdown fences
        if "```" in text:
            parts = text.split("```")
            for part in parts:
                p = part.strip()
                if p.startswith("json"):
                    p = p[4:]
                if p.startswith("["):
                    text = p
                    break

        start = text.find("[")
        if start == -1:
            print("[Gemini] No JSON array found in response.")
            return []

        # Walk forward counting brackets to find the matching close
        depth = 0
        end   = -1
        for i in range(start, len(text)):
            if text[i] == "[":
                depth += 1
            elif text[i] == "]":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break

        if end == -1:
            print("[Gemini] Unmatched '[' in response — JSON truncated?")
            return []

        try:
            result = json.loads(text[start:end])
            return result if isinstance(result, list) else []
        except Exception as exc:
            print(f"[Gemini] JSON parse failed: {exc}")
            print(f"[Gemini] Attempted to parse: {text[start:end][:200]}")
            return []

    # ------------------------------------------------------------------
    # Cache
    # ------------------------------------------------------------------

    def _cache_path(self) -> str:
        return os.path.join(self._base, "search_cache.json")

    def _load_cache(self) -> None:
        try:
            p = self._cache_path()
            if os.path.exists(p):
                with open(p, encoding="utf-8") as f:
                    self._cache = json.load(f)
                print(f"[Gemini] Loaded cache: {len(self._cache)} entries.")
        except Exception as exc:
            print(f"[Gemini] Cache load failed: {exc}")
            self._cache = {}

    def _save_cache(self) -> None:
        try:
            with open(self._cache_path(), "w", encoding="utf-8") as f:
                json.dump(self._cache, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            print(f"[Gemini] Cache save failed: {exc}")

    def query_text(self, prompt: str, cache_key: str) -> str:
        if not self.is_configured:
            return ""
        cached = self._get_cache(cache_key, text=True)
        if cached:
            return cached
        try:
            from google.genai import types
            resp = self._client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    thinking_config=types.ThinkingConfig(
                        thinking_budget=GEMINI_THINKING_BUDGET
                    )
                ),
            )
            result = (resp.text or "").strip()
            if result:
                self._set_cache(cache_key, result, text=True)
            return result
        except Exception as exc:
            print(f"[Gemini] query_text failed: {exc}")
            return ""

    def _get_cache(self, key: str, text: bool = False):
        """Return cached value if fresh, else None."""
        entry = self._cache.get(key)
        if not isinstance(entry, dict):
            return None
        if (time.time() - entry.get("ts", 0)) / 86400 > _CACHE_TTL_DAYS:
            return None
        return entry.get("text") if text else entry.get("data")

    def _set_cache(self, key: str, value, text: bool = False) -> None:
        field = "text" if text else "data"
        self._cache[key] = {field: value, "ts": time.time()}
        self._save_cache()
