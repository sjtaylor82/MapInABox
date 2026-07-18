"""tripadvisor.py — TripAdvisor hotel reviews via RapidAPI.

Optional add-on for Hotel Search. Reuses the SAME ``rapidapi_key`` the Priceline
hotel search already uses — the user just also subscribes (free tier available)
to the "Tripadvisor COM" API (ntd119) at:
    https://rapidapi.com/ntd119/api/tripadvisor-com1

Parsing is deliberately schema-tolerant: it walks the JSON to find the location
id and the review objects, so small differences between the provider's response
and what we expect don't break it. Raw responses are printed when nothing is
found, to make any field-mapping fix trivial. Reviews are cached 30 days.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from logging_utils import miab_log

API_BASE = "https://tripadvisor-com1.p.rapidapi.com"
API_HOST = "tripadvisor-com1.p.rapidapi.com"

# Endpoint paths for the ntd119 "Tripadvisor COM" API (verified against live
# usage). /auto-complete resolves a name to a geoId; /hotels/reviews returns the
# reviews for that id (passed as contentId).
SEARCH_PATH  = "/auto-complete"
REVIEWS_PATH = "/hotels/reviews"

_CACHE_TTL_DAYS = 30


def _read_response(resp):
    raw = resp.read()
    if "gzip" in resp.headers.get("Content-Encoding", "").lower():
        try:
            raw = gzip.decompress(raw)
        except Exception:
            pass
    return json.loads(raw.decode("utf-8", errors="ignore"))


def _error_body(exc) -> str:
    """Best-effort read of an HTTPError body (RapidAPI puts a message there)."""
    try:
        raw = exc.read()
        if "gzip" in exc.headers.get("Content-Encoding", "").lower():
            raw = gzip.decompress(raw)
        return raw.decode("utf-8", "ignore")[:400]
    except Exception:
        return ""


class TripAdvisorClient:

    def __init__(self, api_key: str, cache_file: str = "tripadvisor_cache.json"):
        self._key = (api_key or "").strip()
        self._ctx = ssl.create_default_context()
        self._cache_file = cache_file
        self._cache = self._load_cache()

    @property
    def configured(self) -> bool:
        return bool(self._key)

    # ------------------------------------------------------------------ cache
    def _load_cache(self) -> dict:
        try:
            if os.path.exists(self._cache_file):
                with open(self._cache_file, encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def _save_cache(self) -> None:
        try:
            with open(self._cache_file, "w", encoding="utf-8") as f:
                json.dump(self._cache, f)
        except Exception as exc:
            miab_log("errors", f"[TripAdvisor] cache save failed: {exc}", getattr(self, "settings", None))

    # ---------------------------------------------------------------- request
    def _request(self, path: str, params: dict):
        url = f"{API_BASE}{path}?{urllib.parse.urlencode(params)}"
        miab_log("verbose", f"[TripAdvisor] GET {url}", getattr(self, "settings", None))
        req = urllib.request.Request(url, headers={
            "x-rapidapi-key": self._key,
            "x-rapidapi-host": API_HOST,
            "User-Agent": "MapInABox/1.0",
        })
        with urllib.request.urlopen(req, timeout=30, context=self._ctx) as r:
            return _read_response(r)

    # ----------------------------------------------------------------- public
    def get_hotel_reviews(self, name: str, lat=None, lon=None, limit: int = 6) -> list:
        """Return up to ``limit`` reviews as
        {title, text, rating, date, user} for the named hotel, or [].

        Raises PermissionError if the RapidAPI key is not subscribed to the API,
        so the caller can show a helpful 'subscribe' message.
        """
        name = (name or "").strip()
        if not name or not self.configured:
            return []

        cache_key = "rev_" + name.lower()
        entry = self._cache.get(cache_key)
        if entry and (time.time() - entry.get("ts", 0)) / 86400 < _CACHE_TTL_DAYS:
            reviews = self._filter_real_reviews(entry.get("reviews", []))
            if reviews:
                miab_log("verbose", f"[TripAdvisor] review cache hit for {name!r}", getattr(self, "settings", None))
                return reviews[:limit]
            miab_log("verbose", f"[TripAdvisor] ignoring stale non-review cache for {name!r}", getattr(self, "settings", None))
            self._cache.pop(cache_key, None)
            self._save_cache()

        # 1. Resolve the hotel to a location/content id via auto-complete.
        try:
            loc = self._request(SEARCH_PATH, {"query": name})
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise PermissionError("RapidAPI key not subscribed to Tripadvisor COM")
            miab_log("errors", f"[TripAdvisor] search HTTP {exc.code}", getattr(self, "settings", None))
            return []
        except Exception as exc:
            miab_log("errors", f"[TripAdvisor] search failed: {exc}", getattr(self, "settings", None))
            return []

        loc_id = self._extract_location_id(loc, name)
        if not loc_id:
            miab_log("verbose", f"[TripAdvisor] no location id in search response: "
                  f"{json.dumps(loc)[:600]}", getattr(self, "settings", None))
            return []

        # Surface the candidates so a wrong pick is easy to diagnose.
        try:
            cands = [f"{it.get('title')!r}:{it.get('geoId') or it.get('documentId')}"
                     for it in (loc.get('data') or [])[:6] if isinstance(it, dict)]
            miab_log("verbose", f"[TripAdvisor] candidates {cands} -> chose contentId={loc_id}", getattr(self, "settings", None))
        except Exception:
            pass

        # 2. Fetch reviews for that id (the API takes it as contentId).
        try:
            data = self._request(REVIEWS_PATH, {"contentId": loc_id})
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise PermissionError("RapidAPI key not subscribed to Tripadvisor COM")
            miab_log("errors", f"[TripAdvisor] reviews HTTP {exc.code}: {_error_body(exc)}", getattr(self, "settings", None))
            return []
        except Exception as exc:
            miab_log("errors", f"[TripAdvisor] reviews failed: {exc}", getattr(self, "settings", None))
            return []

        # Diagnostic: show where reviews actually live and a real sample, so the
        # field mapping can be pinned down exactly.
        try:
            cont = data.get("data") if isinstance(data, dict) else None
            if isinstance(cont, dict):
                miab_log("verbose", f"[TripAdvisor] data keys: {list(cont.keys())[:15]}", getattr(self, "settings", None))
                arr = cont.get("reviews")
                if isinstance(arr, list) and arr and isinstance(arr[0], dict):
                    miab_log("verbose", f"[TripAdvisor] sample review keys: {list(arr[0].keys())[:25]}", getattr(self, "settings", None))
                    miab_log("verbose", f"[TripAdvisor] sample review: {json.dumps(arr[0])[:600]}", getattr(self, "settings", None))
        except Exception:
            pass

        reviews = self._filter_real_reviews(self._extract_reviews(data))[:limit]
        if not reviews:
            miab_log("verbose", f"[TripAdvisor] no reviews parsed from: {json.dumps(data)[:600]}", getattr(self, "settings", None))
        else:
            self._cache[cache_key] = {"reviews": reviews, "ts": time.time()}
            self._save_cache()
        return reviews

    # --------------------------------------------- schema-tolerant extraction
    @staticmethod
    def _extract_location_id(data, name: str):
        """Pick the id of the auto-complete result that best matches the hotel
        name.

        Hotels and geos both expose their id as ``geoId`` here, so the important
        part is choosing the *hotel* result ('Adina ... Bondi Beach') over the
        bare locality ('Bondi Beach') — the reviews endpoint rejects a geo id
        (HTTP 405)."""
        want = [t for t in re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).split()
                if len(t) >= 3]

        items = data.get("data") if isinstance(data, dict) else None
        if isinstance(items, list):
            best_id, best_score = None, -1
            for it in items:
                if not isinstance(it, dict):
                    continue
                gid = (it.get("geoId") or it.get("documentId")
                       or it.get("locationId") or it.get("id"))
                if not gid:
                    continue
                title = str(it.get("title") or it.get("name")
                            or it.get("secondaryText") or "").lower()
                score = sum(1 for t in want if t in title)
                if score > best_score:
                    best_score, best_id = score, str(gid)
            if best_id:
                return best_id

        # Fallback: first id-bearing node anywhere in the structure.
        found = []

        def walk(node):
            if found:
                return
            if isinstance(node, dict):
                gid = (node.get("geoId") or node.get("documentId")
                       or node.get("id"))
                if gid:
                    found.append(str(gid))
                    return
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(data)
        return found[0] if found else None

    @staticmethod
    def _extract_reviews(data) -> list:
        """Map the reviews from the known path ``data["data"]["reviews"]``.

        Targeting that array avoids scooping up the filter labels ("All reviews",
        "Last 3 months", "January"…) that a whole-tree walk would wrongly grab.
        Each entry is required to have real body text so non-review items in the
        array are dropped."""
        def text_of(d: dict) -> str:
            return TripAdvisorClient._review_text_from_dict(d)

        def title_of(d: dict) -> str:
            for key in ("title", "reviewTitle", "heading", "header"):
                value = TripAdvisorClient._coerce_string(d.get(key))
                if value:
                    return value
            return ""

        def user_of(d: dict) -> str:
            for key in ("userProfile", "user", "author", "member", "owner"):
                u = d.get(key)
                if isinstance(u, dict):
                    value = TripAdvisorClient._coerce_string(
                        u.get("displayName") or u.get("username")
                        or u.get("name") or u.get("localizedName"))
                    if value:
                        return value
            return TripAdvisorClient._coerce_string(
                d.get("username") or d.get("userName") or d.get("displayName"))

        def rating_of(d):
            r = (d.get("rating") or d.get("bubbleRating")
                 or d.get("bubbleRatingValue") or d.get("score")
                 or d.get("ratingValue") or "")
            return TripAdvisorClient._normalise_rating(r)

        def date_of(d: dict) -> str:
            for key in (
                "publishedDate", "publishedDateTime", "date",
                "createdDate", "publicationDate", "travelDate",
            ):
                value = TripAdvisorClient._coerce_string(d.get(key))
                if value:
                    return value
            return ""

        def walk_dicts(node, out: list) -> None:
            if isinstance(node, dict):
                out.append(node)
                for v in node.values():
                    walk_dicts(v, out)
            elif isinstance(node, list):
                for v in node:
                    walk_dicts(v, out)

        # Locate the reviews array: known path first, else any list whose items
        # mostly carry review-like body text.
        container = data.get("data") if isinstance(data, dict) else None
        raw = container.get("reviews") if isinstance(container, dict) else None
        if raw is None:
            raw = TripAdvisorClient._find_reviews_list(data)

        candidates = []
        walk_dicts(raw, candidates)
        if not candidates:
            candidates = TripAdvisorClient._find_reviews_list(data)
        def candidate_score(d: dict):
            typename = str(d.get("__typename") or "")
            return (
                bool(rating_of(d)),
                bool(date_of(d)),
                bool(user_of(d)),
                bool(title_of(d)),
                any(k in d for k in (
                    "text", "htmlText", "body", "reviewText", "reviewBody",
                    "expandedText", "publishedReviewText", "reviewBodyText",
                )),
                "Review" in typename,
                not any(skip in typename for skip in ("Container", "Response", "Page")),
            )

        candidates.sort(key=candidate_score, reverse=True)

        out = []
        seen_text = set()
        for item in candidates:
            if not isinstance(item, dict):
                continue
            typename = str(item.get("__typename") or "")
            if any(skip in typename for skip in (
                "Summary", "GAI", "Filter", "ResponseContainer",
            )):
                continue
            text = text_of(item)
            if not text:
                continue            # filter labels / non-reviews have no body
            text_key = re.sub(r"\s+", " ", text.lower()).strip()
            if text_key in seen_text:
                continue
            seen_text.add(text_key)
            out.append({
                "title":  title_of(item).strip(),
                "text":   text,
                "rating": rating_of(item),
                "date":   date_of(item).strip(),
                "user":   user_of(item).strip(),
            })
        return out

    @staticmethod
    def _coerce_string(value) -> str:
        if value in (None, ""):
            return ""
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, str):
            return re.sub(r"\s+", " ", value).strip()
        if isinstance(value, dict):
            for key in (
                "string", "text", "value", "displayString", "localizedString",
                "debugValue", "htmlString", "plainText", "title", "label",
            ):
                text = TripAdvisorClient._coerce_string(value.get(key))
                if text:
                    return text
        return ""

    @staticmethod
    def _clean_review_text(value) -> str:
        text = TripAdvisorClient._coerce_string(value)
        text = re.sub(r"<[^>]+>", "", text)
        text = (text.replace("&amp;", "&").replace("&quot;", '"')
                    .replace("&#39;", "'").replace("&nbsp;", " "))
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _review_text_from_dict(d: dict) -> str:
        direct_keys = (
            "text", "htmlText", "body", "reviewText", "reviewBody",
            "expandedText", "publishedReviewText", "reviewBodyText",
            "content", "description",
        )
        for key in direct_keys:
            text = TripAdvisorClient._clean_review_text(d.get(key))
            if text:
                return text

        found = []

        def walk(node, key_hint: str = ""):
            if isinstance(node, dict):
                for k, v in node.items():
                    key = str(k).lower()
                    hint = key_hint or any(
                        marker in key for marker in
                        ("text", "body", "html", "content", "description")
                    )
                    walk(v, key if hint else "")
            elif isinstance(node, list):
                for v in node:
                    walk(v, key_hint)
            elif key_hint:
                text = TripAdvisorClient._clean_review_text(node)
                if len(text.split()) >= 4 and not TripAdvisorClient._is_filter_label(text):
                    found.append(text)

        walk(d)
        return max(found, key=len) if found else ""

    @staticmethod
    def _normalise_rating(value):
        if isinstance(value, dict):
            for key in ("rating", "value", "bubbleRating", "ratingValue", "score"):
                r = TripAdvisorClient._normalise_rating(value.get(key))
                if r:
                    return r
            value = TripAdvisorClient._coerce_string(value)
        if value in (None, ""):
            return ""
        try:
            rating = float(str(value).replace(",", "").strip())
        except Exception:
            m = re.search(r"\d+(?:\.\d+)?", str(value))
            if not m:
                return ""
            rating = float(m.group(0))
        if rating > 5 and rating <= 50:
            rating = rating / 10.0
        return f"{rating:g}"

    @staticmethod
    def _filter_real_reviews(reviews: list) -> list:
        """Drop TripAdvisor filter controls that can look like review records."""
        out = []
        for review in reviews or []:
            if not isinstance(review, dict):
                continue
            text = str(review.get("text") or "").strip()
            title = str(review.get("title") or "").strip()
            rating = review.get("rating")
            date = str(review.get("date") or "").strip()
            user = str(review.get("user") or "").strip()
            if not text or TripAdvisorClient._is_filter_label(text):
                continue
            has_review_metadata = bool(title or rating or date or user)
            if not has_review_metadata and len(text.split()) < 4:
                continue
            out.append(review)
        return out

    @staticmethod
    def _is_filter_label(text: str) -> bool:
        cleaned = re.sub(r"\s+", " ", (text or "").strip().lower())
        cleaned = cleaned.replace("–", "-").replace("—", "-")
        if not cleaned:
            return True
        filter_labels = {
            "all reviews", "last 3 months", "last 6 months", "last 12 months",
            "january", "february", "march", "april", "may", "june", "july",
            "august", "september", "october", "november", "december",
            "selected", "unselected", "review selected", "review unselected",
            "excellent", "very good", "average", "poor", "terrible",
            "families", "couples", "solo", "business", "friends",
            "time of year", "language", "traveler rating", "traveller rating",
        }
        if cleaned in filter_labels:
            return True
        if cleaned.startswith("review "):
            return TripAdvisorClient._is_filter_label(cleaned[7:])
        return False

    @staticmethod
    def _find_reviews_list(data) -> list:
        """Fallback: the longest list whose dicts carry review-like body text."""
        best = []

        def has_text(d):
            return (
                isinstance(d, dict)
                and bool(TripAdvisorClient._review_text_from_dict(d))
            )

        def walk(node):
            nonlocal best
            if isinstance(node, list):
                hits = [x for x in node if has_text(x)]
                if len(hits) > len(best):
                    best = hits
                for v in node:
                    walk(v)
            elif isinstance(node, dict):
                for v in node.values():
                    walk(v)

        walk(data)
        return best
