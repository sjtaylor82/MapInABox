"""mistral.py - Mistral-backed AI queries for Map in a Box."""

from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.parse
import urllib.request
from typing import Optional
from logging_utils import miab_log

MISTRAL_TEXT_MODEL = "mistral-small-latest"
MISTRAL_VISION_MODEL = "mistral-small-2506"
_CACHE_TTL_DAYS = 90
_MENU_CACHE_TTL_DAYS = 30


class MistralClient:
    def __init__(self, script_dir: Optional[str] = None) -> None:
        import sys
        self._base = script_dir or getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
        self._api_key = ""
        self._cache: dict = {}
        self._load_cache()

    def init(self, api_key: str) -> None:
        if not api_key or not api_key.strip():
            self._api_key = ""
            miab_log("api_calls", "[Mistral] No API key provided - Mistral disabled.", getattr(self, "settings", None))
            return
        self._api_key = api_key.strip()
        miab_log("api_calls", "[Mistral] Key loaded.", getattr(self, "settings", None))

    @property
    def is_configured(self) -> bool:
        return bool(self._api_key)

    def ask_transit(self, lat: float, lon: float, place_name: str = "this location") -> list[dict]:
        if not self.is_configured:
            return []
        miab_log("api_calls", f"[Mistral] Transit lookup start: {place_name!r} at ({lat:.4f}, {lon:.4f})", getattr(self, "settings", None))
        snippets, links = self._search_web_grounding(
            [
                f"{place_name} regional bus train ferry",
                f"{place_name} timetable coach regional bus",
                f"{place_name} public transport routes",
            ],
            label="Transit",
        )
        prompt = (
            f"List every REGIONAL and LONG-DISTANCE public transport route "
            f"(coach, regional bus, regional train, ferry) that serves or stops at "
            f"'{place_name}' at coordinates {lat:.4f}, {lon:.4f}. Do NOT include local "
            f"urban or suburban routes. Return ONLY a JSON array of objects with keys "
            f"operator, service, route_name, and stops.\n\n"
            f"WEB SNIPPETS:\n{snippets}\n\n"
            f"CANDIDATE LINKS:\n{chr(10).join(links)}"
        )
        try:
            text = self._chat(prompt)
            miab_log("api_calls", f"[Mistral] Transit raw response length: {len(text or '')}", getattr(self, "settings", None))
            routes = self._parse_json_list(text)
            miab_log("api_calls", f"[Mistral] Transit parsed entries: {len(routes)}", getattr(self, "settings", None))
            clean = []
            for r in routes:
                if isinstance(r, dict) and r.get("operator") and r.get("service"):
                    clean.append({
                        "operator": str(r.get("operator", "")).strip(),
                        "service": str(r.get("service", "")).strip(),
                        "route_name": str(r.get("route_name", "")).strip(),
                        "stops": [str(s) for s in (r.get("stops", []) or []) if s],
                    })
            miab_log("api_calls", f"[Mistral] Transit usable entries: {len(clean)}", getattr(self, "settings", None))
            return clean
        except Exception as exc:
            miab_log("errors", f"[Mistral] Transit query failed: {exc}", getattr(self, "settings", None))
            return []

    def ask_times(self, operator: str, service: str, route_name: str = "") -> str:
        if not self.is_configured:
            return "Mistral not configured."
        cache_key = (f"times_{operator}_{service}".lower().replace(" ", "_").replace("(", "").replace(")", "").replace(".", ""))
        cached = self._get_cache(cache_key, text=True)
        if cached is not None:
            miab_log("api_calls", f"[Mistral] Times cache hit for {operator} {service}", getattr(self, "settings", None))
            return cached
        miab_log("api_calls", f"[Mistral] Times lookup start: {operator} {service} {route_name!r}", getattr(self, "settings", None))
        snippets, links = self._search_web_grounding(
            [
                f"{operator} {service} timetable",
                f"{operator} {service} route timetable",
                f"{operator} {service} schedule",
            ],
            label="Times",
        )
        prompt = (
            f"Describe the timetable for the {operator} service {service} "
            f"{f'({route_name})' if route_name else ''} in plain English. "
            f"Include frequency, approximate first and last service, and weekend/public holiday differences. "
            f"Be concise.\n\n"
            f"WEB SNIPPETS:\n{snippets}\n\n"
            f"CANDIDATE LINKS:\n{chr(10).join(links)}"
        )
        try:
            text = self._chat(prompt)
            if text:
                miab_log("api_calls", f"[Mistral] Times raw response length: {len(text)}", getattr(self, "settings", None))
                self._set_cache(cache_key, text, text=True)
                return text
        except Exception as exc:
            miab_log("errors", f"[Mistral] Times query failed: {exc}", getattr(self, "settings", None))
        return "Could not retrieve timetable information."

    def ask_shopping(
        self,
        centre_name: str,
        lat: float,
        lon: float,
        centre_address: str = "",
        existing_names: list[str] | None = None,
    ) -> list[str]:
        if not self.is_configured:
            return []
        address_key = re.sub(r"[^a-z0-9]+", "", (centre_address or "").lower())
        cache_key = f"shop_names_{centre_name.lower().strip()}_{address_key}"
        cached = self._get_cache(cache_key)
        if cached is not None:
            miab_log("api_calls", f"[Mistral] Shopping cache hit for {centre_name}", getattr(self, "settings", None))
            return cached
        try:
            centre_hint = f"{centre_name} {centre_address}".strip()
            queries = [
                f"\"{centre_hint}\" store directory",
                f"\"{centre_hint}\" directory stores",
                f"\"{centre_hint}\" tenants",
                f"\"{centre_hint}\" official stores",
                f"\"{centre_hint}\" food restaurants cafes takeaway dining",
                f"\"{centre_hint}\" food court restaurants cafes outlets",
            ]
            miab_log("api_calls", f"[Mistral] Shopping lookup start: {centre_name!r} at ({lat:.4f}, {lon:.4f})", getattr(self, "settings", None))
            snippets, links = self._search_web_grounding(queries, label="Shopping")
            combined_text = snippets[:25000]
            miab_log("api_calls", f"[Mistral] Shopping combined text length: {len(combined_text)} "
                f"(links={len(links)})", getattr(self, "settings", None))
            if combined_text:
                miab_log("api_calls", f"[Mistral] Shopping text preview: {combined_text[:500]!r}", getattr(self, "settings", None))
            else:
                miab_log("api_calls", "[Mistral] Shopping grounding empty; skipping model call.", getattr(self, "settings", None))
                return []

            confirmed = ""
            if existing_names:
                confirmed = "Already confirmed tenants: " + ", ".join(existing_names[:80]) + "\n\n"
            prompt = (
                f"Search the web for the current tenant list of the shopping centre at '{centre_hint}' in Australia "
                f"(near {lat:.4f}, {lon:.4f}). Include ALL stores - specialty, food, services, department stores. "
                f"Only include tenants that are explicitly supported by the official centre directory or by clearly "
                f"matching source text. Do not infer, guess, embellish, or fill in gaps. If a store is not directly "
                f"evidenced, omit it. Return only actual tenant/store names. Do not return directory headings, section "
                f"titles, or the shopping centre name itself. Keep renamed food venues and current tenant names only "
                f"when the source explicitly shows they are the same store. Check for food outlets separately as well "
                f"as general tenants, because restaurant and takeaway tenants are often listed on different pages. "
                f"Treat search snippets as discovery hints only; trust the fetched page text and source URLs below. "
                f"If the sources are thin or noisy, return fewer names rather than guessing. "
                f"Return the names in strict alphabetical order (A to Z). Return ONLY a JSON array of store "
                f"name strings - no floors, no categories, no explanation, no markdown.\n\n"
                f"{confirmed}"
                f"Use the SOURCE blocks below as the evidence. Ignore anything not supported there:\n"
                f"{combined_text}"
            )
            text = self._chat(prompt)
            miab_log("api_calls", f"[Mistral] Shopping raw response length: {len(text or '')}", getattr(self, "settings", None))
            names = self._parse_json_list(text)
            miab_log("api_calls", f"[Mistral] Shopping parsed entries: {len(names)}", getattr(self, "settings", None))
            clean = self._clean_store_names(names, centre_name)
            clean = self._retain_evidenced_store_names(clean, combined_text, existing_names=existing_names)
            miab_log("api_calls", f"[Mistral] Shopping usable entries: {len(clean)}", getattr(self, "settings", None))
            if clean:
                self._set_cache(cache_key, clean)
            return clean
        except Exception as exc:
            miab_log("errors", f"[Mistral] Shopping query failed: {exc}", getattr(self, "settings", None))
            return []

    def ask_store_detail(
        self,
        store_name: str,
        centre_name: str,
        centre_address: str = "",
        source_text: str = "",
        source_links: list[str] | None = None,
    ) -> str:
        if not self.is_configured:
            return "Mistral not configured."
        cache_key = f"store_{centre_name}_{centre_address}_{store_name}".lower().replace(" ", "_")
        cached = self._get_cache(cache_key, text=True)
        if cached is not None:
            miab_log("api_calls", f"[Mistral] Store detail cache hit for {store_name}", getattr(self, "settings", None))
            return cached
        centre_hint = f"{centre_name} {centre_address}".strip()
        snippets = source_text or ""
        links = list(source_links or [])
        if not snippets:
            snippets, links = self._search_web_grounding(
                [
                    f"{store_name} {centre_hint} location",
                    f"{store_name} {centre_hint} where is it located",
                    f"{store_name} {centre_hint} store directory",
                ],
                label="StoreDetail",
            )
        prompt = (
            f"In the shopping centre at '{centre_hint}', extract the details for {store_name} from the official "
            f"source text and URLs below. Use only the official source text and URLs. "
            f"Return a short plain-text summary with any of these details that are explicitly supported: floor or "
            f"level, nearby landmark or section, opening hours, phone, and website. "
            f"Do not guess, infer, embellish, or use typical mall layouts. "
            f"If no location details are stated, say only that the store is listed in the official directory. "
            f"Use at most three short sentences.\n\n"
            f"PAGE TEXT:\n{snippets}\n\n"
            f"CANDIDATE LINKS:\n{chr(10).join(links)}"
        )
        try:
            miab_log("api_calls", f"[Mistral] Fetching store detail for '{store_name}'...", getattr(self, "settings", None))
            text = self._chat(prompt)
            if text:
                if text.strip().upper() == "NONE":
                    text = f"{store_name} is listed in the official store directory."
                self._set_cache(cache_key, text, text=True)
                return text
        except Exception as exc:
            miab_log("errors", f"[Mistral] Store detail query failed: {exc}", getattr(self, "settings", None))
        return f"{store_name} is listed in the official store directory."

    def ask_store_floor(self, store_name: str, centre_name: str) -> str:
        """Return floor/section info for a store inside a shopping centre.

        Consults the centre's own directory via web search. Returns an empty
        string when the page text does not clearly state the floor. Never
        guesses. Cached per (centre, store) for 30 days regardless of outcome.
        """
        if not self.is_configured:
            return ""
        cache_key = f"floor_{centre_name}_{store_name}".lower().replace(" ", "_")
        cached = self._get_cache(cache_key, text=True)
        if cached is not None:
            miab_log("api_calls", f"[Mistral] Floor cache hit for {store_name}: {cached!r}", getattr(self, "settings", None))
            return cached
        miab_log("api_calls", f"[Mistral] Floor lookup start: {store_name} at {centre_name}", getattr(self, "settings", None))
        snippets, links = self._search_web_grounding(
            [
                f"{centre_name} store directory {store_name} level",
                f"{centre_name} stores {store_name} floor",
                f"\"{store_name}\" \"{centre_name}\" level floor",
            ],
            label="Floor",
        )
        if not snippets:
            self._set_cache(cache_key, "", text=True)
            return ""
        prompt = (
            f"You are reading web page text that may describe where '{store_name}' is located "
            f"inside '{centre_name}' shopping centre. From the PAGE TEXT below, "
            f"state ONLY what the page actually says about the floor or level of this store, "
            f"and any zone or section if mentioned in the page text. "
            f"One short sentence, no more than 15 words.\n\n"
            f"STRICT RULES — read carefully:\n"
            f"- If the page text does NOT clearly state the floor or level of this specific store, "
            f"respond with EXACTLY the word: NONE\n"
            f"- Do NOT guess.\n"
            f"- Do NOT infer from category, anchor stores, or typical layouts.\n"
            f"- Do NOT say 'likely', 'probably', 'usually', 'typically', or similar.\n"
            f"- Do NOT mention any store other than '{store_name}'.\n\n"
            f"PAGE TEXT:\n{snippets[:18000]}"
        )
        try:
            text = (self._chat(prompt) or "").strip()
        except Exception as exc:
            miab_log("errors", f"[Mistral] Floor query failed: {exc}", getattr(self, "settings", None))
            return ""
        # Reject hedge words and the explicit NONE signal.
        cleaned = text.strip().rstrip(".")
        upper = cleaned.upper()
        if (not cleaned
                or upper == "NONE"
                or upper.startswith("NONE")
                or any(w in cleaned.lower() for w in
                       ("likely", "probably", "usually", "typically",
                        "not certain", "uncertain", "cannot determine",
                        "does not state", "does not mention", "unclear",
                        "i don't know", "i do not know", "could not find",
                        "couldn't find", "not enough information"))):
            miab_log("api_calls", f"[Mistral] Floor: not stated for {store_name} (raw={text!r})", getattr(self, "settings", None))
            self._set_cache(cache_key, "", text=True)
            return ""
        miab_log("api_calls", f"[Mistral] Floor for {store_name}: {text!r}", getattr(self, "settings", None))
        self._set_cache(cache_key, text, text=True)
        return text

    def describe_satellite_image(self, image_bytes: bytes, cache_key: str = "") -> str:
        if not self.is_configured:
            return ""
        if cache_key:
            cached = self._get_cache(cache_key, text=True)
            if cached:
                return cached
        prompt = (
            "You are describing a satellite or aerial image for a blind person who cannot see it. "
            "Describe terrain, land use, buildings, roads, water, and vegetation in 2 to 4 sentences."
        )
        try:
            text = self._chat(prompt, image_bytes=image_bytes, model=MISTRAL_VISION_MODEL)
            if cache_key and text:
                self._set_cache(cache_key, text, text=True)
            return text
        except Exception as exc:
            miab_log("errors", f"[Mistral] Satellite description failed: {exc}", getattr(self, "settings", None))
            return ""

    def describe_streetview_images(self, image_bytes_list: list, headings: list,
                                   mode: str = "explore") -> str:
        if not self.is_configured:
            return ""

        if mode == "navigation":
            # Images are fetched travel-direction first, then the reverse, so we
            # can label them body-relative instead of by compass bearing.
            labels = ["the direction you are walking", "the view behind you"]
            heading_bits = [
                f"image {idx + 1} shows {labels[idx]}"
                for idx in range(min(len(headings or []), 2))
            ]
            heading_note = (" " + "; ".join(heading_bits) + ".") if heading_bits else ""
            prompt = (
                "You are describing Google Street View images to orient a blind "
                "traveller during turn-by-turn walking navigation — a spoken "
                "equivalent of an augmented-reality walking view."
                f"{heading_note}"
                " First decide whether the images show an outdoor street scene, "
                "an indoor/place preview, or a mixed entrance area. If the images "
                "are indoor, say 'Indoor preview:' and describe the indoor access "
                "cues that would help after arrival; do not apologise for the lack "
                "of outdoor traffic details. Report ONLY what matters to cross "
                "safely, stay on route, enter, or orient indoors, and only what is "
                "clearly visible:\n"
                "- for outdoor images: the intersection or junction ahead, and "
                "its shape if clear "
                "(T-junction, four-way, roundabout);\n"
                "- for outdoor images: whether there is a pedestrian crossing and its type: traffic "
                "signals with a push button, a marked or zebra crossing, a refuge "
                "island, or none;\n"
                "- for outdoor images: kerb ramps, dropped kerbs, or steps at the kerb;\n"
                "- for outdoor images: which side traffic in the nearest lane comes from;\n"
                "- for outdoor images: whether the walker must cross a road to continue, and which side "
                "the footpath continues on.\n"
                "- for indoor/place previews: stairs, ramps, lifts, escalators, "
                "handrails, doors, corridors, reception desks, keypads, intercoms, "
                "light switches, tactile or Braille signs, obstacles, narrow spaces, "
                "floor surface or texture if visible (for example carpet, tile, "
                "concrete, timber, matting, polished floor, uneven surface, or a "
                "threshold), and whether the likely path continues left, right, "
                "ahead, upstairs, or downstairs.\n"
                "- for indoor/place previews: the useful spatial layout or place "
                "type, such as an open courtyard, atrium, arcade, covered passage, "
                "stairwell, lobby, reception area, shopping-centre concourse, or "
                "enclosed corridor. Prefer these wayfinding facts over generic "
                "decor such as ceiling, colours, or wall finishes. Do mention "
                "floor material or texture when visible, because it can help a "
                "blind traveller orient by sound, cane feel, slope, or threshold.\n"
                "- When repeated route landmarks are clearly visible, give an "
                "approximate count: houses or townhouses in a row, entrances, "
                "doors, gates, mailboxes, stairs, bollards, pillars, driveways, "
                "or other repeated features. Use cautious wording such as "
                "'about three' or 'at least two' when the full count is partly "
                "hidden.\n"
                "- Named landmarks the traveller could use as an audible waypoint: "
                "if a shop, cafe, restaurant, pub, bank, pharmacy, or other business "
                "has clearly readable signage, or a notable building (church, "
                "school, library, station entrance, and similar) is obviously "
                "identifiable, name it briefly and say which side it's on — for "
                "example 'a cafe on the left' or 'the entrance to Glenferrie "
                "Station ahead'. Only report ones you can actually read or clearly "
                "identify; do not guess at a business type from a generic "
                "storefront.\n"
                "Use ONLY body-relative directions: left, right, ahead, behind. "
                "NEVER use compass directions. Do NOT describe decorative building "
                "appearance, colours, general scenery, or people, and do not "
                "describe vehicles beyond which side traffic flows. If something is "
                "not clearly visible, say so briefly rather than guessing. Keep it "
                "to two or three short factual sentences, adding a fourth only if "
                "naming a landmark."
            )
        else:
            def _cardinal(heading: float) -> str:
                dirs = [
                    "north", "north-east", "east", "south-east",
                    "south", "south-west", "west", "north-west",
                ]
                return dirs[round(heading / 45) % 8]

            heading_bits = []
            for idx, heading in enumerate((headings or [])[:2], start=1):
                try:
                    heading_bits.append(f"image {idx} faces {heading:.0f} degrees ({_cardinal(float(heading))})")
                except Exception:
                    continue
            heading_note = ""
            if heading_bits:
                heading_note = " " + " ".join(heading_bits) + "."
            prompt = (
                "You are describing Google Street View images for a blind traveler as part of an accessible route planner."
                f"{heading_note}"
                " First decide whether the images show an outdoor street scene, an indoor/place preview, or a mixed entrance area."
                " Extract only facts that are clearly visible: the useful spatial layout or place type such as an open courtyard, atrium, arcade, covered passage, stairwell, lobby, reception area, shopping-centre concourse, or enclosed corridor; approximate counts of repeated route landmarks such as houses or townhouses in a row, entrances, doors, gates, mailboxes, stairs, bollards, pillars, or driveways; floor material or texture such as carpet, tile, concrete, timber, matting, polished floor, uneven surface, or thresholds; pedestrian crossings, kerb cuts, tactile paving, steps, ramps, lifts, escalators, handrails, barriers, sidewalk width, "
                "obstructions, covered walkways, readable stop or platform signs, entrance locations, doors, corridors, reception desks, keypads, intercoms, light switches, "
                "and whether the destination or route appears to be left, right, ahead, behind, upstairs, or downstairs. "
                "If the images are indoor, start with 'Indoor preview:' and describe access/orientation cues rather than saying outdoor traffic details are unavailable. "
                "Prefer spatial layout, floor surface, and wayfinding facts over generic decor such as ceiling, colours, or wall finishes. "
                "Don't mention cars or their models, but the side on which the traffic moves would be useful to note for outdoor images. "
                "Do NOT guess, infer, or fill gaps. If a sign is unreadable or a detail is uncertain, let the user know. "
                "Keep it concise and factual."
            )
        try:
            content = [{"type": "text", "text": prompt}]
            for b in image_bytes_list[:2]:
                b64 = base64.b64encode(b).decode("ascii")
                content.append({"type": "image_url", "image_url": f"data:image/jpeg;base64,{b64}"})
            return self._chat(contents=content, model=MISTRAL_VISION_MODEL)
        except Exception as exc:
            miab_log("errors", f"[Mistral] Street View description failed: {exc}", getattr(self, "settings", None))
            return ""

    def query_text(self, prompt: str, cache_key: str) -> str:
        if not self.is_configured:
            return ""
        cached = self._get_cache(cache_key, text=True)
        if cached:
            return cached
        try:
            result = self._chat(prompt, model=MISTRAL_TEXT_MODEL)
            if result:
                self._set_cache(cache_key, result, text=True)
            return result
        except Exception as exc:
            miab_log("errors", f"[Mistral] query_text failed: {exc}", getattr(self, "settings", None))
            return ""

    def narrative_directions(self, digest: dict) -> str:
        """Turn a route digest from NavigationEngine into pedestrian prose.

        Returns "" if Mistral is not configured, the digest is malformed, or
        the post-check finds an invented street name. Callers should fall back
        to the deterministic step list on empty return.
        """
        if not self.is_configured:
            return ""
        if not isinstance(digest, dict) or not digest.get("legs"):
            return ""

        # Build the allow-list of street/place names the model may use.
        allowed = set()
        for leg in digest["legs"]:
            if leg.get("street"):
                allowed.add(leg["street"])
            for cs in leg.get("cross_streets_passed", []) or []:
                # cross_streets_passed entries are {"name", "side"} dicts;
                # tolerate the legacy bare-string form too.
                if isinstance(cs, dict):
                    if cs.get("name"):
                        allowed.add(cs["name"])
                elif isinstance(cs, str):
                    allowed.add(cs)
            onto = (leg.get("end_action") or {}).get("onto")
            if onto:
                allowed.add(onto)
        for highlight in digest.get("route_highlights", []) or []:
            if isinstance(highlight, dict) and highlight.get("name"):
                allowed.add(highlight["name"])
        for feat in digest.get("pedestrian_features", []) or []:
            # Feature strings look like "Pedestrian crossing at X and Y: traffic signals."
            # Extract any road names so the post-check doesn't flag them as invented.
            m = re.search(r"crossing at ([^:]+):", feat or "")
            if m:
                for part in re.split(r"\s+and\s+", m.group(1)):
                    part = part.strip().rstrip(".")
                    if part:
                        allowed.add(part)
        origin_label = (digest.get("origin") or {}).get("label", "")
        dest_label   = (digest.get("destination") or {}).get("label", "")
        if origin_label: allowed.add(origin_label)
        if dest_label:   allowed.add(dest_label)

        cache_key = "narrative_" + hex(abs(hash(json.dumps(
            digest, sort_keys=True, default=str))))[2:]
        cached = self._get_cache(cache_key, text=True)
        if cached:
            return cached

        system = (
            "You write pedestrian walking directions for a blind user who relies "
            "on a screen reader.  You are given a JSON route digest produced from "
            "OpenStreetMap data.  STRICT RULES:\n"
            "- Use ONLY street, place and address names that appear in the digest. "
            "Never invent or guess a street name.  If a fact is not in the digest, "
            "do not state it.\n"
            "- NEVER use compass directions (north, south, east, west, "
            "north-east, etc.) or cardinal footpath names (\"the eastern "
            "footpath\").  A blind walker cannot sense compass bearings, so they "
            "are useless and must be omitted entirely, even if the digest "
            "contains a `compass` or `bearing_deg` field.  Orient the walker "
            "ONLY with body-relative cues: left, right, ahead, behind, and which "
            "side the road is on.\n"
            "- Every numbered step must end with an orientation tag describing "
            "where the road is and which way traffic moves, because a blind "
            "walker re-establishes their bearings at each step.  The walker "
            "stays on the same relative footpath through turns, so these "
            "values from `origin` apply to EVERY leg, not just leg 0.  Use "
            "the digest fields in this exact priority order, omitting any "
            "that are null:\n"
            "  * `origin.road_side` — say \"the road is on your left\" or \"the "
            "road is on your right\".  This is the single most important cue and "
            "must appear on every step when it is known.\n"
            "  * `origin.traffic_nearest_lane` — \"with_you\" → say "
            "\"traffic in the nearest lane moves in the same direction as "
            "you\"; \"toward_you\" → say \"traffic in the nearest lane comes "
            "toward you\".\n"
            "- Describe turns only as left, right, slight left, slight right, "
            "sharp left, sharp right, or continue straight — taken from the "
            "leg's `end_action.turn`.  Never attach a compass heading to a turn.\n"
            "- For the destination, read `destination.crossing_needed`:\n"
            "  * true  → say the destination is on the far side of the road and the walker must cross to reach it (you may name the final leg's street).\n"
            "  * false → say the destination is on the same side as the walker, so no crossing is needed.\n"
            "  * null  → the crossing relationship is UNKNOWN.  Say NOTHING about crossing — do not claim a crossing is needed and do not claim one is not.  Inventing or denying a crossing here is a serious error.  Instead state the arrival using `destination.side`: \"left\"/\"right\" → \"the destination is on your left/right as you approach\"; \"ahead\" → \"the destination is a short distance ahead\"; \"behind\" → \"the destination is slightly behind you\"; null → simply say you arrive at the destination on the named street.\n"
            "  Never say \"on the road side\" — it is ambiguous.  Never introduce a road crossing that `crossing_needed` does not explicitly set to true.\n"
            "- When `country.drives_on` is not null and `origin.road_side` is null, "
            "state the driving convention once in step 1: \"traffic drives on the "
            "{drives_on}\" (e.g. \"traffic drives on the left\").  This gives the "
            "walker essential context even when the exact road side is unknown.\n"
            "- Do not describe traffic flow when `country.drives_on` is null.\n"
            "- Use each `distance_display` value from the digest verbatim.  Never convert a distance to a compass direction.\n"
            "- A leg's `cross_streets_passed` lists side streets meeting the road as the walker continues along it, in order, each with a `side` and a `crossed` flag. "
            "Mention them passively, as features of the road, never as turns the walker makes, and only call something a crossing when the walker truly crosses it:\n"
            "  * `crossed` true  → the side street opens onto the walker's own footpath; say e.g. \"the mouth of {Name} opens on your {side}\" or \"you cross {Name} on your {side}\".\n"
            "  * `crossed` false → the side street meets the road on the far side; the walker does NOT cross it.  Say e.g. \"{Name} joins from the {side}, across the road\", and never imply the walker crosses it.\n"
            "  * `crossed` null  → make no claim either way: \"{Name} meets the road\" (add the side only if it is left or right).\n"
            "  * `side` \"ahead\" / \"behind\" → treat as the road continuing or forking, not a cross street.\n"
            "  Name every entry in the order given; never merge them into a single \"cross X and Y\".\n"
            "- `route_highlights` lists real points of interest along the route, each with a `name`, `kind` and `route_index`. "
            "Mention the landmark on the step whose position matches its `route_index`, so the walker hears it as they pass it, for example \"you'll pass the library on this stretch\". "
            "Name several of them across the route — they are valuable orientation anchors for a blind walker — but keep each mention to a few words and do not bunch them all into one step. "
            "Use only the exact names from the digest.  A landmark may be on either side, so do not use it to imply which side of the road the walker is on, and never let a landmark replace an explicit crossing instruction.\n"
            "- If `pedestrian_features` is present it lists real pedestrian features in route order — crossings, steps, and landmarks — "
            "fetched live from OpenStreetMap.  Weave each feature into the numbered step nearest to it.  Rules:\n"
            "  * 'traffic signals' → tell the user to press the crossing button and wait for the audible signal or beep.\n"
            "  * 'zebra crossing' → say it is a marked give-way crossing and to listen before stepping out.\n"
            "  * 'uncontrolled crossing' → warn explicitly that there is no signal and to listen and proceed when clear.\n"
            "  * 'pedestrian crossing' with no further qualifier → treat as uncontrolled.\n"
            "  * 'tactile paving present' → mention it as a locating cue ('the crossing has tactile paving').\n"
            "  * 'no tactile paving tagged in OSM' → mention this as a caution.\n"
            "  * 'audible signals tagged' → mention the crossing has an audible signal.\n"
            "  * 'Steps on the route' → warn: 'Warning — steps ahead. No ramp is recorded in OpenStreetMap.'\n"
            "  * 'Landmark: X' → mention briefly as an orientation cue.\n"
            "  Do not skip any feature.  A blind walker who does not know about steps or an uncontrolled crossing faces a safety risk.\n"
            "- Output numbered steps in plain prose.  No markdown, no bullet points, "
            "no preamble, no closing summary.  Each numbered step on its own line."
        )
        user = (
            "Route digest:\n"
            + json.dumps(digest, indent=2, default=str)
        )

        try:
            text = self._chat_with_system(system, user, model="mistral-large-latest")
        except Exception as exc:
            miab_log("errors", f"[Mistral] narrative_directions large failed: {exc} — retrying small", getattr(self, "settings", None))
            try:
                text = self._chat_with_system(system, user, model=MISTRAL_TEXT_MODEL)
            except Exception as exc2:
                miab_log("errors", f"[Mistral] narrative_directions small failed: {exc2}", getattr(self, "settings", None))
                return ""
        if not text:
            return ""

        # Post-check: blank out every allowed name, then look for any
        # Capitalised-word + street-suffix that remains. Anything that
        # survives is an invented street name.
        SUFFIXES = (
            "Road", "Street", "Avenue", "Drive", "Court", "Place", "Crescent",
            "Close", "Boulevard", "Highway", "Terrace", "Parade", "Esplanade",
            "Lane", "Grove", "Way", "Circuit", "Rise", "Row", "Mews", "Track",
            "St", "Rd", "Ave", "Dr", "Ct", "Pl", "Cres", "Cl", "Blvd", "Hwy",
            "Tce", "Pde", "Esp", "Ln", "Gr", "Cct",
        )
        sanitized = text
        for name in sorted(allowed, key=len, reverse=True):
            if name:
                sanitized = sanitized.replace(name, "<NAME>")
        leftover = re.search(
            r"\b[A-Z][A-Za-z'’-]+(?:\s+[A-Z][A-Za-z'’-]+){0,3}\s+(?:"
            + "|".join(SUFFIXES) + r")\b",
            sanitized,
        )
        if leftover:
            miab_log("api_calls", f"[Mistral] narrative_directions rejected — invented name: "
                  f"{leftover.group(0)!r}", getattr(self, "settings", None))
            return ""

        self._set_cache(cache_key, text, text=True)
        return text

    def _chat_with_system(self, system: str, user: str, model: str) -> str:
        """Mistral chat with a system message and a single user turn."""
        messages = [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ]
        req_body = json.dumps({"model": model, "messages": messages}).encode("utf-8")
        req = urllib.request.Request(
            "https://api.mistral.ai/v1/chat/completions",
            data=req_body,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type":  "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=90) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        choices = payload.get("choices") or []
        if not choices:
            return ""
        content = (choices[0].get("message") or {}).get("content", "")
        if isinstance(content, list):
            return "".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            ).strip()
        return str(content or "").strip()

    def _chat(self, prompt: str = "", contents=None, model: str = MISTRAL_TEXT_MODEL, image_bytes: bytes = None) -> str:
        messages = []
        if contents is not None:
            messages.append({"role": "user", "content": contents})
        elif image_bytes is not None:
            b64 = base64.b64encode(image_bytes).decode("ascii")
            messages.append({"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": f"data:image/jpeg;base64,{b64}"},
            ]})
        else:
            messages.append({"role": "user", "content": prompt})

        req_body = json.dumps({"model": model, "messages": messages}).encode("utf-8")
        req = urllib.request.Request(
            "https://api.mistral.ai/v1/chat/completions",
            data=req_body,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=90) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        choices = payload.get("choices") or []
        if not choices:
            return ""
        message = choices[0].get("message") or {}
        content = message.get("content", "")
        if isinstance(content, list):
            return "".join((part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text")).strip()
        return str(content or "").strip()

    def _search_web_grounding(self, queries: list[str], label: str = "") -> tuple[str, list[str]]:
        snippets = []
        candidate_links: list[tuple[int, str]] = []
        seen_links = set()
        search_urls = [
            lambda q: f"https://www.bing.com/search?q={q}",
            lambda q: f"https://html.duckduckgo.com/html/?q={q}",
        ]
        for phrase in queries:
            miab_log("api_calls", f"[Mistral] {label} search phrase: {phrase!r}", getattr(self, "settings", None))
            query = urllib.parse.quote(phrase)
            for build_search_url in search_urls:
                search_url = build_search_url(query)
                try:
                    req = urllib.request.Request(search_url, headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(req, timeout=20) as resp:
                        html = resp.read().decode("utf-8", "ignore")
                except Exception as exc:
                    miab_log("errors", f"[Mistral] {label} search fetch failed for {search_url}: {exc}", getattr(self, "settings", None))
                    continue

                found_snippets = re.findall(
                    r'(?:result__snippet|result__body|b_caption|b_snippet)[^>]*>(.*?)</',
                    html,
                    flags=re.S | re.I,
                )
                snippets.extend(
                    re.sub(r"<[^>]+>", " ", s).strip()
                    for s in found_snippets
                    if s
                )

                raw_links = self._extract_search_links(html, search_url)
                found_links = []
                for raw in raw_links:
                    link = self._normalize_search_link(raw)
                    if not link:
                        continue
                    lower = link.lower()
                    if any(block in lower for block in ("duckduckgo.com", "google.com", "bing.com", "microsoft.com")):
                        continue
                    if link in seen_links:
                        continue
                    score = self._score_candidate_link(link, phrase)
                    candidate_links.append((score, link))
                    found_links.append(link)
                    seen_links.add(link)

                miab_log("api_calls", f"[Mistral] {label} search via {urllib.parse.urlparse(search_url).netloc} "
                    f"found {len(found_snippets)} snippets and {len(found_links)} candidate URLs", getattr(self, "settings", None))
        candidate_links.sort(key=lambda item: (-item[0], item[1]))
        links = [link for _, link in candidate_links[:10]]
        page_text_parts = []
        for link in links[:6]:
            try:
                req = urllib.request.Request(link, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=20) as resp:
                    body = resp.read().decode("utf-8", "ignore")
                text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", body, flags=re.S | re.I)
                text = re.sub(r"<[^>]+>", " ", text)
                text = re.sub(r"\s+", " ", text).strip()
                if text:
                    page_text_parts.append(f"SOURCE: {link}\n{text[:12000]}")
                    miab_log("api_calls", f"[Mistral] {label} page text collected: {len(page_text_parts[-1])} chars from {link}", getattr(self, "settings", None))
            except Exception as exc:
                miab_log("errors", f"[Mistral] {label} page fetch failed for {link}: {exc}", getattr(self, "settings", None))
                continue
        combined = "\n\n".join(page_text_parts).strip()
        if not combined:
            if label in {"Shopping", "StoreDetail", "Floor"}:
                combined = ""
            else:
                combined = "\n".join(snippets).strip()
        miab_log("api_calls", f"[Mistral] {label} combined grounding length: {len(combined)} (links={len(links)}, pages={len(page_text_parts)})", getattr(self, "settings", None))
        return (combined, links[:10])

    @staticmethod
    def _normalize_search_link(raw: str) -> str:
        raw = str(raw or "").strip()
        if not raw:
            return ""
        parsed = urllib.parse.urlparse(raw)
        if parsed.scheme in ("http", "https"):
            host = parsed.netloc.lower()
            if "bing.com" in host or "microsoft.com" in host:
                query = urllib.parse.parse_qs(parsed.query)
                for key in ("u", "url", "ru", "r", "target", "dest"):
                    if key in query and query[key]:
                        candidate = urllib.parse.unquote(query[key][0]).strip()
                        decoded = MistralClient._decode_bing_destination(candidate)
                        if decoded:
                            return decoded
                        if candidate.startswith(("http://", "https://")):
                            return candidate
                if parsed.path.lower().startswith("/ck/a"):
                    decoded = MistralClient._decode_bing_destination(raw)
                    if decoded:
                        return decoded
            if "duckduckgo.com" not in host:
                return raw
            query = urllib.parse.parse_qs(parsed.query)
            for key in ("uddg", "u", "url"):
                if key in query and query[key]:
                    candidate = urllib.parse.unquote(query[key][0])
                    if candidate.startswith(("http://", "https://")):
                        return candidate
            return ""
        if raw.startswith("//"):
            raw = "https:" + raw
        if raw.startswith("/"):
            return ""
        return raw if raw.startswith(("http://", "https://")) else ""

    @staticmethod
    def _decode_bing_destination(raw: str) -> str:
        """Best-effort unwrap for Bing redirect URLs."""
        raw = str(raw or "").strip()
        if not raw:
            return ""
        parsed = urllib.parse.urlparse(raw)
        query = urllib.parse.parse_qs(parsed.query)
        candidates = []
        for key in ("u", "url", "ru", "r", "target", "dest"):
            candidates.extend(query.get(key) or [])
        candidates.append(raw)
        for candidate in candidates:
            candidate = urllib.parse.unquote(str(candidate or "").strip())
            if candidate.startswith(("http://", "https://")):
                return candidate
            if candidate.startswith(("a1", "a2", "a3", "a4")) and len(candidate) > 2:
                encoded = candidate[2:]
                pad = "=" * (-len(encoded) % 4)
                try:
                    import base64
                    decoded = base64.b64decode(encoded + pad).decode("utf-8", "ignore").strip()
                    if decoded.startswith(("http://", "https://")):
                        return decoded
                except Exception:
                    pass
            if candidate.startswith(("http%3A%2F%2F", "https%3A%2F%2F")):
                decoded = urllib.parse.unquote(candidate)
                if decoded.startswith(("http://", "https://")):
                    return decoded
        return ""

    def _extract_search_links(self, html: str, search_url: str) -> list[str]:
        host = urllib.parse.urlparse(search_url).netloc.lower()
        patterns: list[str]
        if "bing.com" in host:
            patterns = [
                r'<li[^>]+class="[^"]*\bb_algo\b[^"]*"[\s\S]*?<h2>\s*<a[^>]+href="([^"]+)"',
                r'<h2>\s*<a[^>]+href="([^"]+)"',
            ]
        elif "duckduckgo.com" in host:
            patterns = [
                r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"',
                r'<a[^>]+rel="nofollow"[^>]+href="([^"]+)"',
                r'<a[^>]+href="([^"]+)"',
            ]
        else:
            patterns = [r'href="(https?://[^"]+)"']

        for pattern in patterns:
            raw_links = re.findall(pattern, html, flags=re.S | re.I)
            if raw_links:
                return raw_links
        return []

    @staticmethod
    def _score_candidate_link(url: str, phrase: str = "") -> int:
        lower = (url or "").lower()
        score = 0
        good_terms = (
            "directory", "tenant", "tenants", "store", "stores", "shop", "shops",
            "mall", "centre", "center", "food", "dining", "restaurant", "cafe",
            "takeaway", "eat", "eatery", "map", "pdf",
        )
        bad_terms = (
            "facebook.com", "instagram.com", "linkedin.com", "youtube.com",
            "maps.google.com", "google.com", "bing.com", "duckduckgo.com",
            "login", "account", "signin", "search", "share",
        )
        for term in good_terms:
            if term in lower:
                score += 1
        for term in bad_terms:
            if term in lower:
                score -= 4
        bits = [b for b in re.findall(r"[a-z0-9]+", (phrase or "").lower()) if len(b) >= 4]
        hits = sum(1 for bit in bits if bit in lower)
        score += min(hits, 3)
        return score

    @staticmethod
    def _retain_evidenced_store_names(
        names: list,
        evidence_text: str,
        existing_names: list[str] | None = None,
    ) -> list[str]:
        evidence = re.sub(r"[^a-z0-9]+", " ", (evidence_text or "").lower())
        existing = {
            re.sub(r"[^a-z0-9]+", "", (name or "").lower())
            for name in (existing_names or [])
            if name
        }
        clean = []
        seen = set()
        for name in names or []:
            raw = str(name or "").strip()
            if not raw:
                continue
            key = re.sub(r"[^a-z0-9]+", "", raw.lower())
            if not key or key in seen:
                continue
            if key in existing:
                continue
            compact = re.sub(r"[^a-z0-9]+", " ", raw.lower()).strip()
            if compact and compact in evidence:
                clean.append(raw)
                seen.add(key)
        return clean

    @staticmethod
    def _clean_store_names(names: list, centre_name: str) -> list[str]:
        centre = str(centre_name or "").strip().lower()
        bad_exact = {
            "shopping centre",
            "shopping center",
            "store directory",
            "stores",
            "directory",
            "tenants",
            "home",
            "westfield",
        }
        clean = []
        seen = set()
        for name in names or []:
            raw = str(name or "").strip()
            if not raw:
                continue
            low = raw.lower()
            if low == centre:
                continue
            if low in bad_exact:
                continue
            if centre and MistralClient._looks_like_centre_heading(low, centre):
                continue
            if any(token in low for token in ("directory", "shopping centre", "shopping center", "tenant list")):
                continue
            if len(raw) < 2:
                continue
            key = low.rstrip(".")
            if key in seen:
                continue
            seen.add(key)
            clean.append(raw)
        return sorted(clean, key=str.lower)

    @staticmethod
    def _looks_like_centre_heading(text: str, centre: str) -> bool:
        """Return True for centre-level labels, not tenant names."""
        text = (text or "").strip()
        centre = (centre or "").strip()
        if not text or not centre:
            return False
        if text == centre:
            return True
        if text.startswith(centre):
            tail = text[len(centre):].strip(" -—:,.")
            if not tail:
                return True
            if any(token in tail for token in ("directory", "tenant", "tenants", "store", "stores", "official")):
                return True
        if text.endswith(centre):
            head = text[:-len(centre)].strip(" -—:,.")
            if not head:
                return True
            if any(token in head for token in ("directory", "tenant", "tenants", "store", "stores", "official")):
                return True
        return False

    @staticmethod
    def _parse_json_list(text: str) -> list:
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
            return []
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
        if end == -1:
            return []
        try:
            parsed = json.loads(text[start:end])
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []

    def _cache_path(self) -> str:
        return os.path.join(self._base, "search_cache.json")

    def _load_cache(self) -> None:
        try:
            p = self._cache_path()
            if os.path.exists(p):
                with open(p, encoding="utf-8") as f:
                    self._cache = json.load(f)
        except Exception:
            self._cache = {}

    def _save_cache(self) -> None:
        try:
            with open(self._cache_path(), "w", encoding="utf-8") as f:
                json.dump(self._cache, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _get_cache(self, key: str, text: bool = False):
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
