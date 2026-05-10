"""lookups.py — LookupsMixin for Map in a Box.

All world-map data-fetch methods live here as a mixin class.
MapNavigator inherits from this alongside wx.Frame.

Methods use self freely (self.lat, self.lon, self.update_ui, self.settings etc.)
No extra coupling — just a cleaner file split.
"""

import datetime
import gzip
import json
import math
import os
import threading
import time
import urllib.parse
import urllib.request

import wx

from logging_utils import miab_log


class LookupsMixin:

    # ------------------------------------------------------------------
    # Facts / Wikipedia
    # ------------------------------------------------------------------

    def announce_facts(self):
        """F6 — display facts about the current country."""
        if not self.last_country_found or self.last_country_found == "Open Water":
            self._set_country_facts_panel({}, "Open Water")
            self._status_update("No facts for open water.", force=True)
            return
        canonical = self._country_aliases().get(
            self.last_country_found, self.last_country_found).lower()
        found = next(
            (info for info in self.facts.values()
             if info.get('name', '').lower() == canonical),
            None
        )
        if found:
            self._set_country_facts_panel(found, self.last_country_found)
            self._status_update(
                f"{found.get('name')}.  "
                f"Capital: {found.get('capital')}.  "
                f"Continent: {found.get('continent')}.  "
                f"Currency: {found.get('currency')}.  "
                f"Fact: {found.get('fact')}",
                force=True
            )
        else:
            self._set_country_facts_panel({}, self.last_country_found)
            self._status_update(f"No facts found for {self.last_country_found}.", force=True)

    def announce_wikipedia_summary(self):
        """Shift+F6 — Wikipedia summary near the current coordinate."""
        country = getattr(self, 'last_country_found', '')
        if not country:
            self._status_update("No location to look up.", force=True)
            return

        # Open water — look up the ocean/gulf name directly
        if country == "Open Water":
            ocean = self._ocean_name(self.lat, self.lon)
            if not ocean or ocean == "Open Water":
                self._status_update("No location to look up.", force=True)
                return
            self._wiki_ensure_cache()
            cache_key = f"ocean|{ocean}"
            if cache_key in self._wiki_cache:
                self._status_update(self._wiki_cache[cache_key], force=True)
                return
            self._status_update(f"Looking up {ocean}...")
            def _fetch_ocean(ocean=ocean, cache_key=cache_key):
                try:
                    params = urllib.parse.urlencode({
                        "action": "opensearch", "search": ocean,
                        "limit": 3, "namespace": 0, "format": "json",
                    })
                    req = urllib.request.Request(
                        f"https://en.wikipedia.org/w/api.php?{params}",
                        headers={"User-Agent": "MapInABox/1.0"})
                    with urllib.request.urlopen(req, timeout=10) as r:
                        data = json.loads(r.read().decode())
                    titles = data[1] if isinstance(data, list) and len(data) > 1 else []
                    if not titles:
                        wx.CallAfter(self._on_wiki_result, cache_key,
                                     f"No Wikipedia article found for {ocean}.")
                        return
                    title = urllib.parse.quote(titles[0].replace(" ", "_"))
                    req2  = urllib.request.Request(
                        f"https://en.wikipedia.org/api/rest_v1/page/summary/{title}",
                        headers={"User-Agent": "MapInABox/1.0"})
                    with urllib.request.urlopen(req2, timeout=10) as r2:
                        pg = json.loads(r2.read().decode())
                    extract  = (pg.get("extract") or "").strip()
                    if not extract:
                        wx.CallAfter(self._on_wiki_result, cache_key,
                                     f"No Wikipedia article found for {ocean}.")
                        return
                    summary = ". ".join(extract.split(". ")[:3]).strip()
                    if not summary.endswith("."):
                        summary += "."
                    wx.CallAfter(self._on_wiki_result, cache_key,
                                 f"{titles[0]}.  {summary}")
                except Exception as exc:
                    wx.CallAfter(self._on_wiki_result, cache_key,
                                 f"Lookup failed: {exc}")
            threading.Thread(target=_fetch_ocean, daemon=True).start()
            return

        self._wiki_ensure_cache()
        lat = float(getattr(self, "lat", 0.0))
        lon = float(getattr(self, "lon", 0.0))
        cache_key = f"geo|{lat:.3f}|{lon:.3f}"
        if cache_key in self._wiki_cache:
            self._status_update(self._wiki_cache[cache_key], force=True)
            return

        self._status_update("Looking up nearby Wikipedia articles...")

        def _http(url):
            req = urllib.request.Request(url, headers={"User-Agent": "MapInABox/1.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read().decode())

        def _summary(title):
            t = urllib.parse.quote(title.replace(" ", "_"))
            return _http(f"https://en.wikipedia.org/api/rest_v1/page/summary/{t}")

        def _geo_articles(radius):
            params = urllib.parse.urlencode({
                "action": "query",
                "list": "geosearch",
                "gscoord": f"{lat}|{lon}",
                "gsradius": radius,
                "gslimit": 10,
                "format": "json",
            })
            data = _http(f"https://en.wikipedia.org/w/api.php?{params}")
            return (data.get("query") or {}).get("geosearch") or []

        def _fetch_geo():
            try:
                for radius in (5000, 15000, 50000):
                    for item in _geo_articles(radius):
                        title = item.get("title", "")
                        if not title:
                            continue
                        try:
                            data = _summary(title)
                        except Exception:
                            continue
                        if data.get("type") != "standard":
                            continue
                        extract = (data.get("extract") or "").strip()
                        if not extract or len(extract) < 80:
                            continue
                        summary = ". ".join(extract.split(". ")[:3]).strip()
                        if not summary.endswith("."):
                            summary += "."
                        dist = item.get("dist")
                        dist_text = ""
                        if isinstance(dist, (int, float)):
                            if dist < 1000:
                                dist_text = f" {round(dist)} metres away."
                            else:
                                dist_text = f" {dist / 1000:.1f} km away."
                        wx.CallAfter(
                            self._on_wiki_result,
                            cache_key,
                            f"{title}.{dist_text}  {summary}",
                        )
                        return
                wx.CallAfter(
                    self._on_wiki_result,
                    cache_key,
                    "No Wikipedia article found near this coordinate.",
                )
            except Exception as exc:
                wx.CallAfter(self._on_wiki_result, cache_key, f"Lookup failed: {exc}")

        threading.Thread(target=_fetch_geo, daemon=True).start()
        return

        COUNTRY_ALIASES = self._country_aliases()
        country_q = COUNTRY_ALIASES.get(country, country)
        state_q   = getattr(self, 'last_state_found',  '') or ''
        city_q    = getattr(self, 'last_city_found',   '') or ''
        suburb_q  = getattr(self, '_current_suburb',   '') or ''
        cached_label = ""
        nearby_cached = getattr(self, "_nearby_cached_place_label", None)
        if callable(nearby_cached):
            cached_label = nearby_cached(self.lat, self.lon) or ""
        if cached_label:
            cached_main = cached_label.split(",", 1)[0].strip()
            if cached_main:
                suburb_q = cached_main
                city_q = cached_main

        self._wiki_ensure_cache()
        cache_key = f"{suburb_q}|{city_q}|{state_q}|{country_q}"
        if cache_key in self._wiki_cache:
            self._status_update(self._wiki_cache[cache_key], force=True)
            return

        hint = suburb_q or city_q or state_q or country_q
        self._status_update(f"Looking up {hint}...")

        def _http(url):
            req = urllib.request.Request(url, headers={"User-Agent": "MapInABox/1.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read().decode())

        def _opensearch(query):
            params = urllib.parse.urlencode({
                "action": "opensearch", "search": query,
                "limit": 5, "namespace": 0, "format": "json",
            })
            data   = _http(f"https://en.wikipedia.org/w/api.php?{params}")
            titles = data[1] if isinstance(data, list) and len(data) > 1 else []
            return titles[0] if titles else ""

        def _summary(title):
            t = urllib.parse.quote(title.replace(" ", "_"))
            return _http(f"https://en.wikipedia.org/api/rest_v1/page/summary/{t}")

        def _wikidata(qid):
            if not qid:
                return None, "", None
            data   = _http(f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json")
            entity = (data.get("entities") or {}).get(qid, {})
            claims = entity.get("claims", {})
            best_val, best_year, best_time = None, "", ""
            for stmt in claims.get("P1082", []):
                mv  = stmt.get("mainsnak", {}).get("datavalue", {}).get("value", {})
                amt = mv.get("amount") if isinstance(mv, dict) else None
                if amt is None:
                    continue
                try:
                    pop = int(float(str(amt)))
                except ValueError:
                    continue
                time_str = year = ""
                for qval in stmt.get("qualifiers", {}).get("P585", []):
                    tv = qval.get("datavalue", {}).get("value", {}).get("time", "")
                    if tv:
                        time_str, year = tv, tv[1:5]
                        break
                if time_str > best_time or best_val is None:
                    best_time, best_val, best_year = time_str, pop, year
            area = None
            for stmt in claims.get("P2046", []):
                mv  = stmt.get("mainsnak", {}).get("datavalue", {}).get("value", {})
                amt = mv.get("amount") if isinstance(mv, dict) else None
                if amt is not None:
                    try:
                        area = float(str(amt))
                        break
                    except ValueError:
                        pass
            return best_val, best_year, area

        def _fmt_pop(pop, year):
            if pop is None:
                return ""
            s = f"{pop:,}"
            if year:
                try:
                    if datetime.date.today().year - int(year) >= 5:
                        return f"{s} (last census {year})"
                except ValueError:
                    pass
            return s

        def _fmt_area(area):
            if area is None:
                return ""
            if area >= 1_000_000:
                return f"{area/1_000_000:,.1f} million km²"
            if area >= 1000:
                return f"{area:,.0f} km²"
            return f"{area:.1f} km²"

        def _try_article(query, context_words):
            title = _opensearch(query)
            if not title:
                return None
            try:
                data = _summary(title)
            except Exception:
                return None
            if data.get("type") != "standard":
                return None
            extract = (data.get("extract") or "").strip()
            if not extract or len(extract) < 80:
                return None
            el = extract.lower()
            if context_words and not any(w.lower() in el for w in context_words):
                print(f"[Wiki] '{title}' doesn't mention {context_words} — skipping")
                return None
            qid  = data.get("wikibase_item", "")
            pop, yr, area = _wikidata(qid)
            summary = ". ".join(extract.split(". ")[:3]).strip()
            if not summary.endswith("."):
                summary += "."
            stats = []
            if pop:
                stats.append(f"Population: {_fmt_pop(pop, yr)}")
            if area:
                stats.append(f"Area: {_fmt_area(area)}")
            stats_str = f"  {'.  '.join(stats)}." if stats else ""
            return title, f"{title}.{stats_str}  {summary}"

        def _fetch():
            try:
                context = [w for w in [state_q, country_q] if w]
                for search, ctx in [
                    (f"{suburb_q} {state_q or country_q}", context)
                        if suburb_q and suburb_q.lower() not in
                            (city_q.lower(), country_q.lower()) else (None, None),
                    (f"{city_q} {state_q or country_q}", context)
                        if city_q and city_q.lower() != country_q.lower() else (None, None),
                    (f"{state_q} {country_q}", [country_q] if country_q else [])
                        if state_q and state_q.lower() != country_q.lower() else (None, None),
                    (country_q, []),
                ]:
                    if not search:
                        continue
                    result = _try_article(search, ctx)
                    if result:
                        _, text = result
                        wx.CallAfter(self._on_wiki_result, cache_key, text)
                        return
                wx.CallAfter(self._on_wiki_result, cache_key,
                             f"No Wikipedia article found for {hint}.")
            except Exception as exc:
                wx.CallAfter(self._on_wiki_result, cache_key,
                             f"Lookup failed: {exc}")

        threading.Thread(target=_fetch, daemon=True).start()

    def _wiki_ensure_cache(self):
        """Load wiki cache from disk if not already loaded."""
        if not hasattr(self, '_wiki_cache'):
            try:
                from core import WIKI_CACHE_PATH
                if os.path.exists(WIKI_CACHE_PATH):
                    with open(WIKI_CACHE_PATH, encoding="utf-8") as f:
                        self._wiki_cache = json.load(f)
                else:
                    self._wiki_cache = {}
            except Exception:
                self._wiki_cache = {}

    def _on_wiki_result(self, cache_key: str, summary: str):
        try:
            from core import WIKI_CACHE_PATH
        except ImportError:
            WIKI_CACHE_PATH = None
        if not hasattr(self, '_wiki_cache'):
            self._wiki_cache = {}
        self._wiki_cache[cache_key] = summary
        if WIKI_CACHE_PATH:
            try:
                with open(WIKI_CACHE_PATH, "w", encoding="utf-8") as f:
                    json.dump(self._wiki_cache, f, ensure_ascii=False, indent=2)
            except Exception as exc:
                print(f"[Wiki] Cache save failed: {exc}")
        miab_log("feature_usage",
                 f"Wikipedia result for cache_key='{cache_key}': "
                 f"{summary[:80].replace(chr(10), ' ')}...",
                 self.settings)
        self._status_update(summary, force=True)

    # ------------------------------------------------------------------
    # Geography
    # ------------------------------------------------------------------

    def announce_continent(self):
        """F5 — announce the continent of the current country."""
        continent = getattr(self, 'current_continent', '')
        country   = getattr(self, 'last_country_found', '')
        if continent:
            self._status_update(f"{continent}.", force=True)
        elif country == "Antarctica":
            self.current_continent = "Antarctica"
            self._status_update("Antarctica.", force=True)
        elif country and country != "Open Water":
            COUNTRY_ALIASES = self._country_aliases()
            canonical = COUNTRY_ALIASES.get(country, country).lower()
            found = next(
                (info for info in self.facts.values()
                 if info.get('name', '').lower() == canonical),
                None
            )
            if found and found.get('continent'):
                self.current_continent = found['continent']
                self._status_update(f"{found['continent']}.", force=True)
            else:
                self._status_update(f"Continent unknown for {country}.", force=True)
        else:
            self._status_update("No location found yet.", force=True)

    def _announce_climate_zone(self):
        """Shift+F2 — announce the climate/latitude zone."""
        lat  = self.lat
        alat = abs(lat)
        hem  = "Northern" if lat >= 0 else "Southern"

        if alat <= 23.5:
            zone = "Tropical (Torrid) Zone"
            desc = "between the Tropics of Cancer and Capricorn, receiving direct overhead sunlight year-round"
        elif alat <= 66.5:
            zone = f"Temperate Zone ({hem} Hemisphere)"
            desc = "between a Tropic and the Arctic or Antarctic Circle, with distinct seasons"
        else:
            zone = f"Polar (Frigid) Zone ({hem} Hemisphere)"
            desc = "within the Arctic or Antarctic Circle, with midnight sun and polar night"

        markers = []
        if abs(alat - 0)    < 1.5: markers.append("near the Equator")
        if abs(alat - 23.5) < 1.5: markers.append(f"near the Tropic of {'Cancer' if lat >= 0 else 'Capricorn'}")
        if abs(alat - 66.5) < 1.5: markers.append(f"near the {'Arctic' if lat >= 0 else 'Antarctic'} Circle")
        if alat > 88:               markers.append("near the Pole")

        msg = f"Climate zone: {zone}. {desc}."
        if markers:
            msg += f"  You are {', '.join(markers)}."
        miab_log("feature_usage", f"Climate zone: {zone}", self.settings)
        self._status_update(msg, force=True)

    # ------------------------------------------------------------------
    # Time / Timezone
    # ------------------------------------------------------------------

    def announce_time(self):
        """T key — local time at current position."""
        try:
            from timezonefinder import TimezoneFinder
            import pytz
            tf      = TimezoneFinder()
            tz_name = tf.timezone_at(lat=self.lat, lng=self.lon)
            if tz_name:
                tz  = pytz.timezone(tz_name)
                now = datetime.datetime.now(tz)
                loc = tz_name.replace("_", " ")
            else:
                now = datetime.datetime.now()
                loc = "local"
        except Exception:
            now = datetime.datetime.now()
            loc = "local"
        hour = now.hour % 12 or 12
        self._status_update(
            f"{loc} time: {hour}:{now.strftime('%M')} {now.strftime('%p')}, "
            f"{now.strftime('%A')} {now.day} {now.strftime('%B %Y')}.",
            force=True
        )

    def _announce_timezone(self):
        """Shift+T — timezone name and UTC offset."""
        try:
            from timezonefinder import TimezoneFinder
            import pytz
            tf      = TimezoneFinder()
            tz_name = tf.timezone_at(lat=self.lat, lng=self.lon)
            if not tz_name:
                self._status_update("No timezone found for this location.", force=True)
                return
            tz            = pytz.timezone(tz_name)
            now           = datetime.datetime.now(tz)
            offset        = now.utcoffset()
            total_minutes = int(offset.total_seconds() / 60)
            hours, mins   = divmod(abs(total_minutes), 60)
            sign          = "+" if total_minutes >= 0 else "-"
            offset_str    = f"UTC{sign}{hours}" if mins == 0 else f"UTC{sign}{hours}:{mins:02d}"
            dst_offset    = tz.dst(datetime.datetime.now().replace(tzinfo=None))
            dst_str       = "  Daylight saving time currently active." \
                            if dst_offset and dst_offset.total_seconds() > 0 else ""
            friendly      = tz_name.replace("_", " ").replace("/", ", ")
            self._status_update(f"Timezone: {friendly}.  {offset_str}.{dst_str}", force=True)
        except Exception as exc:
            self._status_update(f"Could not determine timezone: {exc}", force=True)

    # ------------------------------------------------------------------
    # Weather / Environment
    # ------------------------------------------------------------------

    def _weather_uses_fahrenheit(self) -> bool:
        """Return True when the user prefers Fahrenheit for weather display."""
        unit_pref = (self.settings.get("weather_temperature_unit", "auto") or "auto").strip().lower()
        if unit_pref == "fahrenheit":
            return True
        if unit_pref == "celsius":
            return False
        country = getattr(self, "last_country_found", "") or ""
        aliases = self._country_aliases() if hasattr(self, "_country_aliases") else {}
        canonical = aliases.get(country, country).strip().lower()
        return canonical in {"united states", "united states of america", "usa"}

    def _announce_weather(self):
        """W — fetch and announce weather for the current map position."""
        country = getattr(self, "last_country_found", "") or ""
        if country == "Open Water":
            self._announce_sea_temperature()
            return

        imperial = self._weather_uses_fahrenheit()
        unit_sym = "°F" if imperial else "°C"
        cache_key = (round(self.lat, 2), round(self.lon, 2), imperial)
        cache = getattr(self, "_weather_cache", {})
        import time as _time
        cached = cache.get(cache_key)
        if cached and (_time.time() - cached["ts"]) < 600:
            self._status_update(cached["text"], force=True)
            return

        city = getattr(self, "last_city_found", "") or ""
        self._status_update(f"Fetching weather for {city or country or 'this location'}...")
        lat, lon = self.lat, self.lon

        wmo = {
            0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
            45: "fog", 48: "icy fog",
            51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
            61: "light rain", 63: "rain", 65: "heavy rain",
            71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
            80: "light showers", 81: "showers", 82: "heavy showers",
            85: "snow showers", 86: "heavy snow showers",
            95: "thunderstorm", 96: "thunderstorm with hail",
            99: "heavy thunderstorm with hail",
        }

        def _fetch():
            try:
                temp_unit = "fahrenheit" if imperial else "celsius"
                wind_unit = "mph" if imperial else "kmh"
                params = urllib.parse.urlencode({
                    "latitude": round(lat, 4),
                    "longitude": round(lon, 4),
                    "current": (
                        "temperature_2m,relative_humidity_2m,apparent_temperature,"
                        "weather_code,wind_speed_10m,wind_gusts_10m,cloud_cover,"
                        "precipitation,uv_index"
                    ),
                    "hourly": "temperature_2m,weather_code,precipitation_probability",
                    "temperature_unit": temp_unit,
                    "wind_speed_unit": wind_unit,
                    "forecast_days": 1,
                    "timezone": "auto",
                })
                url = f"https://api.open-meteo.com/v1/forecast?{params}"
                req = urllib.request.Request(url, headers={"User-Agent": "MapInABox/1.0"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    data = json.loads(r.read().decode())

                cur = data["current"]
                temp = round(cur["temperature_2m"])
                feels = round(cur["apparent_temperature"])
                humidity = cur["relative_humidity_2m"]
                wind = round(cur["wind_speed_10m"])
                wcode = cur["weather_code"]
                gust = cur.get("wind_gusts_10m")
                cloud = cur.get("cloud_cover")
                precip = cur.get("precipitation")
                uv = cur.get("uv_index")
                desc = wmo.get(wcode, f"code {wcode}").capitalize()
                wind_lbl = "mph" if imperial else "km/h"

                def _uv_label(v):
                    if v is None: return ""
                    if v < 3:     return "Low"
                    if v < 6:     return "Moderate"
                    if v < 8:     return "High"
                    if v < 11:    return "Very high"
                    return "Extreme"

                place_name = city or country or ""
                current_str = f"Weather: {desc}."
                if place_name:
                    current_str = f"Weather for {place_name}: {desc}."
                current_str += f" {temp}{unit_sym}, feels like {feels}{unit_sym}."
                if uv is not None:
                    current_str += f" UV {round(uv, 1)}, {_uv_label(uv).lower()}."
                current_str += f" Humidity {humidity}%."
                current_str += f" Wind {wind} {wind_lbl}."
                if gust is not None:
                    current_str += f" Gusts {round(gust)} {wind_lbl}."
                if cloud is not None:
                    current_str += f" Cloud cover {round(cloud)}%."
                if precip is not None and float(precip) > 0:
                    current_str += f" Precipitation {round(float(precip), 1)} mm."

                hourly_times = data["hourly"]["time"]
                hourly_temps = data["hourly"]["temperature_2m"]
                hourly_codes = data["hourly"]["weather_code"]
                hourly_pop = data["hourly"].get("precipitation_probability", [])
                now_hour = datetime.datetime.now().strftime("%Y-%m-%dT%H:00")
                forecast_parts = []
                for i, t in enumerate(hourly_times):
                    if t <= now_hour:
                        continue
                    hr = datetime.datetime.fromisoformat(t).strftime("%I %p").lstrip("0")
                    tmp = round(hourly_temps[i])
                    dsc = wmo.get(hourly_codes[i], "")
                    pop = ""
                    if i < len(hourly_pop) and hourly_pop[i] is not None:
                        pop = f", rain {round(hourly_pop[i])}%"
                    forecast_parts.append(f"{hr}: {tmp}{unit_sym} {dsc}{pop}")
                    if len(forecast_parts) >= 4:
                        break

                forecast_str = (
                    "  Next hours: " + ", ".join(forecast_parts) + "."
                    if forecast_parts else ""
                )
                full = current_str + forecast_str
                if not hasattr(self, "_weather_cache"):
                    self._weather_cache = {}
                self._weather_cache[cache_key] = {"text": full, "ts": _time.time()}
                wx.CallAfter(self._status_update, full, True)
            except Exception as exc:
                wx.CallAfter(self._status_update, f"Could not fetch weather: {exc}", True)

        threading.Thread(target=_fetch, daemon=True).start()

    def _announce_sunrise_sunset(self):
        """S key — sunrise and sunset times at current position."""
        self._status_update("Fetching sunrise and sunset...")
        lat, lon = self.lat, self.lon

        def _fetch():
            try:
                from timezonefinder import TimezoneFinder
                tf      = TimezoneFinder()
                tz_name = tf.timezone_at(lat=lat, lng=lon) or "UTC"
                params  = urllib.parse.urlencode({
                    "latitude":      round(lat, 4),
                    "longitude":     round(lon, 4),
                    "daily":         "sunrise,sunset",
                    "timezone":      tz_name,
                    "forecast_days": 1,
                })
                url = f"https://api.open-meteo.com/v1/forecast?{params}"
                req = urllib.request.Request(url, headers={"User-Agent": "MapInABox/1.0"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    data = json.loads(r.read().decode())
                daily    = data.get("daily", {})
                sunrises = daily.get("sunrise", [])
                sunsets  = daily.get("sunset",  [])
                if not sunrises or not sunsets:
                    wx.CallAfter(self._status_update, "Sunrise/sunset data not available.", True)
                    return
                def _fmt(iso):
                    t = datetime.datetime.fromisoformat(iso)
                    h = t.hour % 12 or 12
                    return f"{h}:{t.strftime('%M')} {t.strftime('%p')}"
                wx.CallAfter(self._status_update,
                             f"Sunrise: {_fmt(sunrises[0])}.  Sunset: {_fmt(sunsets[0])}.",
                             True)
            except Exception as exc:
                wx.CallAfter(self._status_update, f"Could not fetch sunrise/sunset: {exc}", True)

        threading.Thread(target=_fetch, daemon=True).start()

    def _announce_sea_temperature(self):
        """Called by map-mode weather over open water via Open-Meteo Marine."""
        self._status_update("Fetching sea surface temperature...")
        lat, lon = self.lat, self.lon
        imperial = self._weather_uses_fahrenheit()

        def _fetch():
            try:
                params = urllib.parse.urlencode({
                    "latitude":    round(lat, 4),
                    "longitude":   round(lon, 4),
                    "current":     "sea_surface_temperature,wave_height,wave_direction",
                    "forecast_days": 1,
                })
                url = f"https://marine-api.open-meteo.com/v1/marine?{params}"
                req = urllib.request.Request(url, headers={"User-Agent": "MapInABox/1.0"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    data = json.loads(r.read().decode())
                cur  = data.get("current", {})
                sst  = cur.get("sea_surface_temperature")
                wh   = cur.get("wave_height")
                wdir = cur.get("wave_direction")
                if sst is None:
                    wx.CallAfter(self._status_update, "Sea temperature data not available here.", True)
                    return
                from geo import compass_name
                if imperial:
                    sst = (float(sst) * 9 / 5) + 32
                parts = [f"Sea surface temperature: {round(sst, 1)}{'°F' if imperial else '°C'}."]
                if wh   is not None: parts.append(f"Wave height: {round(wh, 1)} metres.")
                if wdir is not None: parts.append(f"Waves from the {compass_name(wdir)}.")
                wx.CallAfter(self._status_update, "  ".join(parts), True)
            except Exception as exc:
                wx.CallAfter(self._status_update, f"Could not fetch sea temperature: {exc}", True)

        threading.Thread(target=_fetch, daemon=True).start()

    def _announce_air_quality(self):
        """Q key — air quality at current position."""
        self._status_update("Fetching air quality...")
        lat, lon = self.lat, self.lon

        def _fetch():
            try:
                params = urllib.parse.urlencode({
                    "latitude":      round(lat, 4),
                    "longitude":     round(lon, 4),
                    "current":       "pm2_5,pm10,us_aqi",
                    "forecast_days": 1,
                })
                url = f"https://air-quality-api.open-meteo.com/v1/air-quality?{params}"
                req = urllib.request.Request(url, headers={"User-Agent": "MapInABox/1.0"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    data = json.loads(r.read().decode())
                cur  = data.get("current", {})
                pm25 = cur.get("pm2_5")
                pm10 = cur.get("pm10")
                aqi  = cur.get("us_aqi")

                def _aqi_label(v):
                    if v is None: return ""
                    if v <= 50:   return "Good"
                    if v <= 100:  return "Moderate"
                    if v <= 150:  return "Unhealthy for sensitive groups"
                    if v <= 200:  return "Unhealthy"
                    if v <= 300:  return "Very unhealthy"
                    return "Hazardous"

                parts = []
                if aqi  is not None: parts.append(f"Air quality index: {round(aqi)} ({_aqi_label(aqi)}).")
                if pm25 is not None: parts.append(f"PM2.5: {round(pm25, 1)} µg/m³.")
                if pm10 is not None: parts.append(f"PM10: {round(pm10, 1)} µg/m³.")
                if not parts:
                    wx.CallAfter(self._status_update, "Air quality data not available here.", True)
                    return
                wx.CallAfter(self._status_update, "  ".join(parts), True)
            except Exception as exc:
                wx.CallAfter(self._status_update, f"Could not fetch air quality: {exc}", True)

        threading.Thread(target=_fetch, daemon=True).start()

    # ------------------------------------------------------------------
    # Airports / Flights
    # ------------------------------------------------------------------

    def _ensure_airports_csv(self):
        """Return path to airports.csv, seeding from bundle or downloading if needed."""
        import gzip
        import time as _t
        from core import AIRPORTS_CSV_PATH, AIRPORTS_CSV_SEED, AIRPORTS_CSV_URL, AIRPORTS_STALE_DAYS

        # Fresh cached copy — nothing to do.
        if os.path.exists(AIRPORTS_CSV_PATH):
            if (_t.time() - os.path.getmtime(AIRPORTS_CSV_PATH)) / 86400 < AIRPORTS_STALE_DAYS:
                return AIRPORTS_CSV_PATH

        # Seed from the bundled .gz on first run (or after cache cleared).
        if not os.path.exists(AIRPORTS_CSV_PATH) and os.path.exists(AIRPORTS_CSV_SEED):
            try:
                with gzip.open(AIRPORTS_CSV_SEED, 'rb') as src, \
                        open(AIRPORTS_CSV_PATH, 'wb') as dst:
                    dst.write(src.read())
                return AIRPORTS_CSV_PATH
            except Exception:
                pass

        # Stale or seed unavailable — try downloading.
        try:
            req = urllib.request.Request(
                AIRPORTS_CSV_URL, headers={"User-Agent": "MapInABox/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
            with open(AIRPORTS_CSV_PATH, "wb") as f:
                f.write(data)
            return AIRPORTS_CSV_PATH
        except Exception:
            return AIRPORTS_CSV_PATH if os.path.exists(AIRPORTS_CSV_PATH) else None

    def _announce_nearest_airport(self):
        """A key — nearest large/medium airport."""
        self._status_update("Finding nearest airport...")
        lat, lon = self.lat, self.lon

        def _fetch():
            try:
                import csv
                path = self._ensure_airports_csv()
                if not path:
                    wx.CallAfter(self._status_update,
                                 "Airport data not available. Check internet connection.",
                                 True)
                    return
                best      = None
                best_dist = float("inf")
                with open(path, encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        if row.get("type", "") not in ("large_airport", "medium_airport"):
                            continue
                        try:
                            alat = float(row["latitude_deg"])
                            alon = float(row["longitude_deg"])
                        except (ValueError, KeyError):
                            continue
                        dlat = (alat - lat) * 111.0
                        dlon = (alon - lon) * 111.0 * math.cos(math.radians(lat))
                        dist = math.sqrt(dlat*dlat + dlon*dlon)
                        if dist < best_dist:
                            best_dist, best = dist, row
                if not best:
                    wx.CallAfter(self._status_update, "No airport found nearby.", True)
                    return
                name  = best.get("name", "Unknown airport")
                iata  = best.get("iata_code", "").strip()
                atype = best.get("type", "")
                elev  = best.get("elevation_ft", "")
                from geo import compass_name, bearing_deg
                bearing   = bearing_deg(lat, lon,
                                        float(best["latitude_deg"]),
                                        float(best["longitude_deg"]))
                direction = compass_name(bearing)
                dist_km   = round(best_dist)
                iata_str  = f"  IATA code {iata}." if iata else ""
                elev_str  = f"  Elevation {round(float(elev) * 0.3048)} metres." if elev else ""
                wx.CallAfter(self._status_update,
                             f"Nearest airport: {name}, {dist_km} kilometres {direction}."
                             f"{iata_str}{elev_str}",
                             True)
            except Exception as exc:
                wx.CallAfter(self._status_update, f"Airport lookup failed: {exc}", True)

        threading.Thread(target=_fetch, daemon=True).start()

    def _announce_overhead_flights(self):
        """Shift+A — aircraft overhead via OpenSky Network."""
        self._status_update("Checking for overhead flights...")
        lat, lon   = self.lat, self.lon
        RADIUS_DEG = 0.45

        def _fetch():
            try:
                params = urllib.parse.urlencode({
                    "lamin": round(lat - RADIUS_DEG, 4),
                    "lomin": round(lon - RADIUS_DEG, 4),
                    "lamax": round(lat + RADIUS_DEG, 4),
                    "lomax": round(lon + RADIUS_DEG, 4),
                })
                url = f"https://opensky-network.org/api/states/all?{params}"
                req = urllib.request.Request(url, headers={"User-Agent": "MapInABox/1.0"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    data = json.loads(r.read().decode())
                states = data.get("states") or []
                if not states:
                    wx.CallAfter(self._status_update, "No aircraft detected overhead.", True)
                    return
                from geo import dist_km as _dist_km, compass_name, bearing_deg
                flights = []
                for s in states:
                    try:
                        callsign  = (s[1] or "").strip() or "Unknown"
                        flon, flat = s[5], s[6]
                        alt_m     = s[7]
                        velocity  = s[9]
                        heading   = s[10]
                        on_ground = s[8]
                        if on_ground or flat is None or flon is None:
                            continue
                        d   = _dist_km(lat, lon, flat, flon)
                        brg = bearing_deg(lat, lon, flat, flon)
                        flights.append((d, callsign, alt_m, velocity, heading, brg))
                    except Exception:
                        continue
                if not flights:
                    wx.CallAfter(self._status_update, "No airborne aircraft detected overhead.", True)
                    return
                flights.sort(key=lambda x: x[0])
                flights = flights[:5]
                parts = []
                for d, callsign, alt_m, velocity, heading, brg in flights:
                    alt_str = f"{round(alt_m):,} metres" if alt_m else "unknown altitude"
                    detail  = (f"{callsign}, {round(d)} km {compass_name(brg)}, "
                               f"altitude {alt_str}")
                    if heading is not None:
                        detail += f", heading {compass_name(heading)}"
                    if velocity:
                        detail += f", {round(velocity * 3.6)} km/h"
                    parts.append(detail)
                total = len([s for s in states if not s[8]])
                wx.CallAfter(self._status_update,
                             f"{total} aircraft overhead.  "
                             f"Nearest {len(flights)}: {'.  '.join(parts)}.",
                             True)
            except Exception as exc:
                wx.CallAfter(self._status_update, f"Could not fetch flight data: {exc}", True)

        threading.Thread(target=_fetch, daemon=True).start()

    # ------------------------------------------------------------------
    # Nearest foreign country
    # ------------------------------------------------------------------

    def _jump_nearest_land(self, direction):
        """Ctrl+Alt+Arrow — jump along a compass line to the next country."""
        if getattr(self, '_nearest_land_searching', False):
            return
        self._nearest_land_searching = True
        self._status_update(f"Searching for next country to the {direction}...")
        lat, lon = self.lat, self.lon

        def _fetch():
            try:
                current_country = self._country_at_point(lat, lon)
                if not current_country:
                    current_country = getattr(self, 'last_country_found', '') or ''
                    if current_country == "Open Water":
                        current_country = ""

                step_km = 5.0
                max_km = 20000.0
                clat, clon = lat, lon
                steps = int(max_km / step_km)
                for i in range(1, steps + 1):
                    if direction == "north":
                        clat = min(90.0, lat + (i * step_km / 111.0))
                        clon = lon
                    elif direction == "south":
                        clat = max(-90.0, lat - (i * step_km / 111.0))
                        clon = lon
                    else:
                        lat_factor = max(0.15, math.cos(math.radians(clat)))
                        delta_lon = (i * step_km) / (111.0 * lat_factor)
                        clon = lon + (delta_lon if direction == "east" else -delta_lon)
                        clon = ((clon + 180.0) % 360.0) - 180.0

                    if direction in ("north", "south") and abs(clat) >= 90.0:
                        pole = "North" if clat > 0 else "South"
                        wx.CallAfter(self._status_update,
                                     f"Reached the {pole} Pole. No other country to the {direction}.",
                                     True)
                        return

                    found_country = self._country_at_point(clat, clon)
                    if not found_country or found_country == current_country:
                        continue
                    dist_km = round(i * step_km)
                    msg = (f"Next country to the {direction}: "
                           f"{found_country}, {dist_km} kilometres.")
                    miab_log("navigation",
                             f"Next country {direction}: {found_country} "
                             f"at ({clat:.2f},{clon:.2f}) {dist_km}km",
                             self.settings)
                    wx.CallAfter(self._do_nearest_land_jump, clat, clon, msg, found_country)
                    return
                wx.CallAfter(self._status_update,
                             f"No other country found to the {direction} within {round(max_km)} kilometres.",
                             True)
            except Exception as exc:
                wx.CallAfter(self._status_update, f"Search failed: {exc}", True)
            finally:
                self._nearest_land_searching = False

        threading.Thread(target=_fetch, daemon=True).start()

    def _country_at_point(self, lat, lon):
        """Return the country polygon containing lat/lon, or empty string."""
        try:
            from shapely.geometry import Point, Polygon, shape
            if not hasattr(self, "_country_polygon_cache"):
                from core import GEOJSON_PATH
                countries = []
                if os.path.exists(GEOJSON_PATH):
                    with gzip.open(GEOJSON_PATH, 'rt', encoding="utf-8") as f:
                        data = json.load(f)
                    for feature in data.get("features", []):
                        props = feature.get("properties", {})
                        name = (props.get("NAME") or props.get("name") or
                                props.get("ADMIN") or "").strip()
                        if name:
                            countries.append((name, shape(feature["geometry"])))
                countries.append(("Antarctica", Polygon([
                    (-180, -90), (-180, -60), (-150, -65), (-120, -67),
                    (-90, -65), (-60, -70), (-30, -72), (0, -70),
                    (30, -68), (60, -70), (90, -65), (120, -67),
                    (150, -65), (180, -60), (180, -90), (-180, -90),
                ])))
                self._country_polygon_cache = countries

            point = Point(lon, lat)
            for name, geom in self._country_polygon_cache:
                minx, miny, maxx, maxy = geom.bounds
                if minx <= lon <= maxx and miny <= lat <= maxy and geom.covers(point):
                    return name
        except Exception:
            return ""
        return ""

    def _do_nearest_land_jump(self, lat, lon, msg, found_country=""):
        """Complete the nearest-country jump on the main thread."""
        self._nearest_land_searching = False
        self._suppress_next_location = True
        self._forced_country_name = found_country
        self._forced_country_lat = lat
        self._forced_country_lon = lon
        self._forced_country_until = time.time() + 5.0
        self.last_country_found = ""
        try:
            self.sound.stop()
            self.sound._current = None
        except Exception:
            pass
        self.lat = lat
        self.lon = lon
        self._status_update(msg, force=True)
        threading.Thread(target=self._lookup, daemon=True).start()

    # ------------------------------------------------------------------
    # REST Countries (languages, capital, currency)
    # ------------------------------------------------------------------

    def _fetch_rest_countries(self, country):
        """Shared REST Countries fetch — cached per country name."""
        if not hasattr(self, '_rest_countries_cache'):
            self._rest_countries_cache = {}
        if country in self._rest_countries_cache:
            return self._rest_countries_cache[country]
        try:
            COUNTRY_ALIASES = self._country_aliases()
            query = urllib.parse.quote(COUNTRY_ALIASES.get(country, country))
            url   = (f"https://restcountries.com/v3.1/name/{query}"
                     f"?fields=currencies,languages,capital,borders,area,population,region,subregion")
            req   = urllib.request.Request(url, headers={"User-Agent": "MapInABox/1.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode())
            if data and isinstance(data, list):
                self._rest_countries_cache[country] = data[0]
                return data[0]
        except Exception as exc:
            miab_log("errors",
                     f"REST Countries fetch failed for {country}: {exc}",
                     self.settings)
        return None

    def _announce_languages(self):
        """L key — official languages of current country."""
        country = getattr(self, 'last_country_found', '')
        if not country or country == 'Open Water':
            self._status_update("No country to look up.", force=True)
            return
        cached = getattr(self, '_rest_countries_cache', {}).get(country, {})
        if 'languages' in cached:
            self._status_update(f"Languages: {', '.join(cached['languages'].values())}.", force=True)
            return
        self._status_update(f"Looking up languages for {country}...")
        def _fetch():
            data  = self._fetch_rest_countries(country)
            langs = list((data or {}).get('languages', {}).values())
            wx.CallAfter(self._status_update,
                         f"Languages: {', '.join(langs)}." if langs
                         else f"No language data found for {country}.",
                         True)
        threading.Thread(target=_fetch, daemon=True).start()

    def _announce_capital(self):
        """Shift+F1 — capital city of current country."""
        country = getattr(self, 'last_country_found', '')
        if not country or country == 'Open Water':
            self._status_update("No country to look up.", force=True)
            return
        cached = getattr(self, '_rest_countries_cache', {}).get(country, {})
        if 'capital' in cached:
            caps = cached['capital']
            cap_str = ', '.join(caps) if isinstance(caps, list) else str(caps)
            self._status_update(f"You are in {country}.  Capital: {cap_str}.", force=True)
            return
        self._status_update(f"Looking up capital of {country}...", force=True)
        def _fetch():
            data = self._fetch_rest_countries(country)
            caps = (data or {}).get('capital', [])
            if caps:
                cap_str = ', '.join(caps) if isinstance(caps, list) else str(caps)
                wx.CallAfter(self._status_update,
                             f"You are in {country}.  Capital: {cap_str}.", True)
            else:
                wx.CallAfter(self._status_update,
                             f"No capital data found for {country}.", True)
        threading.Thread(target=_fetch, daemon=True).start()

    def _announce_currency(self):
        """$ key — currency of current country."""
        country = getattr(self, 'last_country_found', '')
        if not country or country == 'Open Water':
            self._status_update("No country to look up.", force=True)
            return
        if not hasattr(self, '_currency_cache'):
            self._currency_cache = {}
        if country in self._currency_cache:
            self._status_update(self._currency_cache[country], force=True)
            return
        self._status_update(f"Looking up currency for {country}...")
        def _fetch():
            try:
                COUNTRY_ALIASES = self._country_aliases()
                query = urllib.parse.quote(COUNTRY_ALIASES.get(country, country))
                url   = f"https://restcountries.com/v3.1/name/{query}?fields=currencies,name"
                req   = urllib.request.Request(url, headers={"User-Agent": "MapInABox/1.0"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    data = json.loads(r.read().decode())
                if not data or not isinstance(data, list):
                    wx.CallAfter(self._status_update, f"No currency data found for {country}.", True)
                    return
                currencies = data[0].get("currencies", {})
                if not currencies:
                    wx.CallAfter(self._status_update, f"No currency data found for {country}.", True)
                    return
                parts = []
                for code, info in currencies.items():
                    name   = info.get("name", code)
                    symbol = info.get("symbol", "")
                    parts.append(f"{name}{f' ({symbol})' if symbol else ''}, code {code}")
                msg = f"Currency: {'.  '.join(parts)}."
                self._currency_cache[country] = msg
                wx.CallAfter(self._status_update, msg, True)
            except Exception as exc:
                wx.CallAfter(self._status_update, f"Could not fetch currency: {exc}", True)
        threading.Thread(target=_fetch, daemon=True).start()

    # ------------------------------------------------------------------
    # Helper — avoids circular import by accessing COUNTRY_ALIASES lazily
    # ------------------------------------------------------------------

    def _country_aliases(self):
        """Return the COUNTRY_ALIASES dict from core module."""
        from core import COUNTRY_ALIASES
        return COUNTRY_ALIASES

    def _announce_nearby_features(self):
        """X key (world map) — show nearby geographic features from CSV."""
        location = getattr(self, 'last_location_str', '') or \
                   getattr(self, 'last_country_found', '') or 'this location'

        features = self._geo_features.nearby(
            self.lat, self.lon, country_code=getattr(self, "_current_country_code", None))

        if not features:
            self._status_update("No named geographic features found nearby.", force=True)
            return

        labels = {
            'T.MTS':  'Mountain range',
            'H.BAY':  'Bay',
            'H.GULF': 'Gulf',
            'H.STRT': 'Strait',
            'H.CHAN': 'Channel',
            'H.SEA':  'Sea',
            'H.OCN':  'Ocean',
            'T.PEN':  'Peninsula',
            'T.CAPE': 'Cape',
            'T.ISLS': 'Island group',
            'T.PLN':  'Plain',
            'T.PLAT': 'Plateau',
            'T.REG':  'Region',
            'T.RGN':  'Region',
            'T.DSRT': 'Desert',
        }

        lines = [f"Geographic features near {location}:", ""]
        for name, code in features:
            label = labels.get(code, code)
            lines.append(f"{label}: {name}")

        text = "\n".join(lines).strip()
        self._show_features_dialog(text, location)

    def _show_features_dialog(self, text: str, location: str):
        """Show nearby features in a read-only dialog."""
        dlg = wx.Dialog(self, title=f"Features near {location}",
                        style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        vs  = wx.BoxSizer(wx.VERTICAL)
        txt = wx.TextCtrl(dlg, value=text,
                          style=wx.TE_MULTILINE | wx.TE_READONLY,
                          size=(420, 320))
        vs.Add(txt, 1, wx.EXPAND | wx.ALL, 8)
        btn = wx.Button(dlg, wx.ID_CLOSE, "Close")
        btn.Bind(wx.EVT_BUTTON, lambda e: dlg.Destroy())
        dlg.Bind(wx.EVT_CHAR_HOOK,
                 lambda e: dlg.Destroy() if e.GetKeyCode() == wx.WXK_ESCAPE else e.Skip())
        vs.Add(btn, 0, wx.ALIGN_RIGHT | wx.RIGHT | wx.BOTTOM, 8)
        dlg.SetSizer(vs)
        dlg.CentreOnScreen()
        dlg.Show()
        txt.SetFocus()
