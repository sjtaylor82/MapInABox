import json
import os
import urllib.parse
import urllib.error
import urllib.request
import ssl
import gzip


API_BASE = "https://priceline-com-provider.p.rapidapi.com"
API_HOST = "priceline-com-provider.p.rapidapi.com"


def _safe_read_response(resp):
    raw = resp.read()
    encoding = resp.headers.get("Content-Encoding", "").lower()
    if "gzip" in encoding:
        try:
            raw = gzip.decompress(raw)
        except Exception:
            pass
    return json.loads(raw.decode("utf-8", errors="ignore"))


class PricelineClient:

    _HOTEL_CACHE_DAYS = 365

    def __init__(self, api_key: str, cache_file: str = "location_cache.json"):
        self._key = api_key.strip() if api_key else ""
        self._ctx = ssl.create_default_context()
        self.cache_file = cache_file
        self._location_cache = self._load_cache()
        self._hotel_cache_file = cache_file.replace("location_cache.json", "hotel_cache.json")
        self._hotel_cache = self._load_hotel_cache()

    def _load_cache(self) -> dict:
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, 'r') as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def _save_cache(self):
        try:
            with open(self.cache_file, 'w') as f:
                json.dump(self._location_cache, f, indent=2)
        except Exception as e:
            print(f"[Priceline] Cache save failed: {e}")

    def _load_hotel_cache(self) -> dict:
        if os.path.exists(self._hotel_cache_file):
            try:
                with open(self._hotel_cache_file, 'r') as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def _save_hotel_cache(self):
        try:
            with open(self._hotel_cache_file, 'w') as f:
                json.dump(self._hotel_cache, f, indent=2)
        except Exception as e:
            print(f"[Priceline] Hotel cache save failed: {e}")

    @property
    def configured(self) -> bool:
        return bool(self._key)

    def _request(self, path, params):
        url = f"{API_BASE}{path}?{urllib.parse.urlencode(params)}"
        print(f"[Priceline] GET {url}")
        req = urllib.request.Request(url, headers={
            "x-rapidapi-key": self._key,
            "x-rapidapi-host": API_HOST,
            "User-Agent": "MapInABox/1.0",
        })
        try:
            with urllib.request.urlopen(req, timeout=30, context=self._ctx) as r:
                return _safe_read_response(r)
        except urllib.error.HTTPError as e:
            body = ""
            try:
                raw = e.read()
                enc = e.headers.get("Content-Encoding", "").lower()
                if "gzip" in enc:
                    import gzip as _gz
                    raw = _gz.decompress(raw)
                body = raw.decode("utf-8", errors="ignore")[:500]
            except Exception:
                pass
            print(f"[Priceline] HTTP {e.code} from {path}: {body}")
            raise


    def get_location_id(self, name: str):
        name_key = name.lower().strip()

        # cache still supported (optional)
        if name_key in self._location_cache:
            return [{"label": name, "id": self._location_cache[name_key]}]

        try:
            data = self._request("/v1/hotels/locations", {
                "name": name,
                "search_type": "CITY"
            })

            results = data if isinstance(data, list) else data.get("results", []) or data.get("data", [])

            if not results:
                return None

            choices = []

            for r in results[:5]:
                cid = r.get("cityID") or r.get("id")
                if not cid:
                    continue

                label = f"{r.get('displayLine1','')} {r.get('displayLine2','')}".strip()

                if not label:
                    label = r.get("itemName", "Unknown location")

                choices.append({
                    "label": label,
                    "id": cid
                })

            return choices if choices else None

        except Exception as e:
            print(f"[Priceline] Location lookup error: {e}")

        return None





    def search_hotels(self, location_id: str,
                      date_checkin: str,
                      date_checkout: str,
                      sort_order: str = "PRICE",
                      rooms: int = 1,
                      max_pages: int = 2,
                      min_price: float = None,
                      max_price: float = None,
                      min_rating: float = 3.5) -> list:

        # --- cache check ---
        import time as _time
        cache_key = f"{location_id}_{date_checkin}_{date_checkout}"
        cache_entry = self._hotel_cache.get(cache_key)
        if cache_entry:
            age_days = (_time.time() - cache_entry.get("ts", 0)) / 86400
            if age_days < self._HOTEL_CACHE_DAYS:
                print(f"[Priceline] Hotel cache hit for {cache_key}")
                return cache_entry.get("hotels", [])

        try:
            all_hotels = []

            for page in range(1, max_pages + 1):
                # API requires YYYY-MM-DD; convert from YYYYMMDD if needed
                def _fmt(d):
                    d = str(d).replace("-", "")
                    return f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 else d
                data = self._request("/v1/hotels/search", {
                    "location_id": location_id,
                    "date_checkin": _fmt(date_checkin),
                    "date_checkout": _fmt(date_checkout),
                    "sort_order": sort_order,
                    "rooms_number": rooms,
                    "page_number": page - 1,  # API is 0-indexed
                    "pageSize": 25,
                })

                raw = data.get("hotels") or []

                page_hotels = _parse_hotels(data)

                if not page_hotels:
                    break

                all_hotels.extend(page_hotels)

                if len(page_hotels) < 10:
                    break

            # --- dedupe ---
            unique = {}
            for h in all_hotels:
                key = (h.get("name"), h.get("address"))
                if key not in unique:
                    unique[key] = h

            all_hotels = list(unique.values())

            # --- filtering ---
            filtered = []
            for h in all_hotels:
                rating = h.get("rating", 0)

                if min_rating is not None and rating < min_rating:
                    continue

                filtered.append(h)

            # --- sort alphabetically ---
            filtered.sort(key=lambda x: (x.get("name") or "").lower())

            # --- save to cache (only if results found) ---
            import time as _time
            if filtered:
                self._hotel_cache[cache_key] = {"hotels": filtered, "ts": _time.time()}
                self._save_hotel_cache()

            return filtered

        except Exception as e:
            print(f"[Priceline] Hotel search error: {e}")
            return []




def _parse_hotels(data):
    hotels = []
    results = data.get("hotels") or []

    for item in results:
        try:
            name = item.get("name") or item.get("hotelName")
            if not name:
                continue

            # --- price ---
            price_data = item.get("ratesSummary", {}) or item.get("price", {})
            raw_price = price_data.get("minPrice") or price_data.get("total") or 0
            try:
                price = float(raw_price)
            except (TypeError, ValueError):
                price = 0.0
            currency = price_data.get("currency", "")

            # --- rating ---
            rating = item.get("starRating") or item.get("rating") or 0

            # --- safe address handling ---
            loc = item.get("location", {}) or item.get("address", {})

            def _safe_str(v):
                if isinstance(v, dict):
                    return ""
                return str(v) if v else ""

            address_parts = [
                _safe_str(loc.get("address")),
                _safe_str(loc.get("cityName")),
                _safe_str(loc.get("province")),
            ]

            address = ", ".join([p for p in address_parts if p])

            # --- lat/lon ---
            lat = loc.get("latitude")
            lon = loc.get("longitude")

            hotels.append({
                "name": name,
                "price": price,
                "currency": currency,
                "rating": rating,
                "address": address,
                "lat": lat,
                "lon": lon,
            })

        except Exception as e:
            print(f"[Priceline] Hotel parse error: {e}")

    return hotels
