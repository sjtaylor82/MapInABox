"""city_packs.py - Bulk pre-fetch of chosen suburbs for Map in a Box.

Ctrl+Shift+F11 opens a short wizard: pick a country, pick a state/region
(or a postcode range), then pick individual suburbs from a real,
OSM-sourced list for that area - checkbox by checkbox, full control.
Once confirmed the wizard closes immediately and the actual fetch runs
fully in the background - reported through the same status line
Shift+F11 already uses - so the app is usable again right away rather
than being stuck behind a modal dialog.

No separate server, no pre-built downloadable files: this is a client
that talks to the same live Overpass mirrors F11/Shift+F11 already use,
through the same cooldown-respecting OverpassClient, and writes results
through the same _save_road_cache() every organic fetch uses. Re-running
the wizard over an already-covered area is cheap - fetch_road_data()
skips any suburb that already has a fresh cache entry.

How the suburb list is built (design history)
------------------------------------------------
Five earlier attempts at "which suburbs make up a city" all fell short:

1. A hand-guessed bounding box for Brisbane pulled in Yarrabilba and Peak
   Crossing, both well outside the city.
2. A Nominatim administrative-boundary lookup for "Brisbane" matched
   something the size of regional Queensland.
3. A curated per-country suburb list worked but only covered a handful
   of countries and needed hand maintenance.
4. A population-sized bounding box, grid-sampled and reverse-geocoded,
   still pulled in neighbouring council areas and bushland while missing
   well-known suburbs - a uniform grid picks up whatever sits at each
   grid intersection, not what anyone would call part of the city.
5. Auto-discovering suburbs via an Overpass place-node query and fetching
   all of them automatically was closer, but still left the user with no
   say over which specific suburbs got downloaded.

This version keeps the Overpass place-node discovery (it's the part that
actually works - it only returns places OSM's own community has tagged
place=suburb/neighbourhood/quarter/town/city) but stops there: the
discovered list is shown to the user as real checkboxes, nothing gets
fetched until they pick specific suburbs and confirm. That gives complete
control and still works worldwide, since nothing is hardcoded - the area
searched comes from a live Nominatim lookup of whatever state/region (or
postcode range) name the user picked, and the suburb list comes from a
live Overpass query over that area.

Classes
-------
CityPackWizardDialog
    The Ctrl+Shift+F11 wizard: intro -> country -> state/region (or
    postcode range) -> suburb checklist (discovered live) -> confirm.

Functions
---------
discover_suburbs_in_bbox(overpass_client, south, north, west, east) -> list
    The real OSM place nodes in a bounding box - the core of this file.
run_batch_fetch(street_fetcher, packs, country_code, ...) -> stats
    Fetch every selected suburb, synchronously.
start_batch_fetch_background(street_fetcher, packs, country_code, ...)
    Convenience wrapper - runs run_batch_fetch() on a daemon thread.
is_batch_fetch_active() -> bool
    Whether a background batch fetch started by this module is still running.
"""

import threading
import time
import urllib.parse
import urllib.request
import json

import wx
from shapely.geometry import shape, Point
from logging_utils import miab_log

# ── Screen-reader speech (AccessibleOutput2) ──────────────────────────────
# Same direct-to-screen-reader pattern core.py uses (_speak there) - a
# wx.StaticText label change is *not* announced automatically by most
# screen readers unless something explicitly speaks it, so "please wait"
# progress messages in this wizard need this rather than just SetLabel().
try:
    import accessible_output2.outputs.auto as _ao2_auto
    _ao2 = _ao2_auto.Auto()
except Exception:
    _ao2 = None


def _speak(msg: str, interrupt: bool = True) -> None:
    if _ao2:
        try:
            _ao2.speak(str(msg), interrupt=interrupt)
        except Exception:
            pass


def _bind_typeahead(listbox, timeout_ms=1200):
    """Attach a forgiving multi-character incremental search to a
    wx.ListBox.

    wx's built-in type-ahead resets its match buffer very quickly (a
    couple hundred ms) - fine for a sighted, fast typist, but too tight
    once a screen reader is echoing every keystroke back: by the time the
    second letter of e.g. "au" for Australia arrives, the native search
    has already reset and jumped to the first single-letter match
    instead. This keeps its own buffer alive for timeout_ms and matches
    by prefix, replacing the native single-char jump entirely.
    """
    state = {"buffer": "", "last": 0.0}

    def on_char(event):
        keycode = event.GetUnicodeKey()
        if keycode == wx.WXK_NONE:
            event.Skip()
            return
        ch = chr(keycode)
        if not ch.isalnum():
            event.Skip()
            return
        ch = ch.lower()
        now = time.monotonic()
        if now - state["last"] > (timeout_ms / 1000):
            state["buffer"] = ""
        state["buffer"] += ch
        state["last"] = now
        for i in range(listbox.GetCount()):
            if listbox.GetString(i).lower().startswith(state["buffer"]):
                listbox.SetSelection(i)
                return
        # No match on the full buffer - fall back to just the latest
        # character so a mistyped prefix doesn't strand the user.
        state["buffer"] = ch
        for i in range(listbox.GetCount()):
            if listbox.GetString(i).lower().startswith(ch):
                listbox.SetSelection(i)
                return

    listbox.Bind(wx.EVT_CHAR, on_char)

# ---------------------------------------------------------------------------
# Country / state catalog, built from the already-bundled worldcities data
# ---------------------------------------------------------------------------

# Radius (metres) for each selected suburb's own fetch - matches the fixed
# radius Shift+F11 already uses for a single-suburb prefetch.
SUBURB_RADIUS_M = 3000

# Rough per-area time budget for the estimate shown to the user. Each
# uncached area costs at least one Overpass query plus one address query,
# each behind the shared 8-second cooldown - cached areas return instantly.
EST_SECONDS_PER_AREA = 20

# Place tags Overpass is asked for when discovering suburbs in an area.
# Deliberately excludes village/hamlet/isolated_dwelling/locality so a big
# state doesn't flood the list with hundreds of tiny rural spots - both
# for relevance (the point of this design is real, recognisable places)
# and because a very long checklist is slow to build and hard to scan
# with a screen reader. A postcode range still gives finer-grained control
# within a smaller area if a specific village is wanted.
_PLACE_TYPES = "suburb|neighbourhood|quarter|town|city"


def list_countries(df):
    """Sorted list of distinct country names present in the worldcities data."""
    return sorted(c for c in df['country'].dropna().unique() if c)


def list_states_for_country(df, country):
    """Sorted list of distinct state/region (admin_name) values for one
    country - e.g. Queensland, New South Wales, ... Empty if the dataset
    has no subdivisions for that country (small countries, city-states)."""
    sub = df[df['country'] == country]
    names = set()
    for val in sub['admin_name'].dropna():
        val = str(val).strip()
        if val and val != country:
            names.add(val)
    return sorted(names)


def geocode_admin_boundary(name, country=None, timeout=15):
    """Bounding box, and where available a precise polygon boundary, for a
    named administrative area (state/region) via Nominatim.

    Returns (bbox, geometry): bbox is (south, north, west, east); geometry
    is a shapely Polygon/MultiPolygon, or None if Nominatim didn't return
    a proper boundary for the match.

    The polygon matters: a bounding box is a rectangle, and a rectangle
    drawn around an irregular state/region border inevitably overlaps a
    strip of whatever's next door - that's how a Queensland search
    surfaced Alstonville, which is in New South Wales. The bbox is still
    used to scope the initial Overpass discovery query (cheap, and errs
    generous), but the polygon is then used to filter out anything that
    fell inside the box but outside the real border.
    """
    query = f"{name}, {country}" if country else name
    params = urllib.parse.urlencode({
        "q": query, "format": "jsonv2", "limit": 1, "polygon_geojson": 1,
    })
    req = urllib.request.Request(
        f"https://nominatim.openstreetmap.org/search?{params}",
        headers={"User-Agent": "MapInABox/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if not data or "boundingbox" not in data[0]:
            return None, None
        south, north, west, east = (float(v) for v in data[0]["boundingbox"])
        geometry = None
        geojson = data[0].get("geojson")
        if geojson and geojson.get("type") in ("Polygon", "MultiPolygon"):
            try:
                geometry = shape(geojson)
            except Exception as e:
                miab_log("errors", f"[CityPacks] Boundary polygon parse failed for {name!r}: {e}", None)
        return (south, north, west, east), geometry
    except Exception as e:
        miab_log("errors", f"[CityPacks] Area geocode failed for {name!r}: {e}", None)
        return None, None


def discover_suburbs_in_bbox(overpass_client, south, north, west, east, timeout=60):
    """Ask OSM directly what it considers a named suburb/town/etc within a
    bounding box, instead of grid-sampling geography and reverse-geocoding
    whatever's nearest to each sample point.

    Only returns places OSM's own community has tagged place=suburb/
    neighbourhood/quarter/town/city/village, deduplicated by name, sorted
    alphabetically (so the checkbox list the user sees is easy to scan
    with a screen reader). Returns [] on failure or if OSM has nothing
    tagged in that area.
    """
    query = (
        "[out:json][timeout:50];\n"
        f'node["place"~"{_PLACE_TYPES}"]({south},{west},{north},{east});\n'
        "out body;\n"
    )
    data = urllib.parse.urlencode({"data": query}).encode()
    result = overpass_client.large_request(data, timeout=timeout)
    if not result:
        return []
    seen = set()
    points = []
    for el in result.get("elements", []):
        tags = el.get("tags", {})
        name = (tags.get("name") or "").strip()
        el_lat, el_lon = el.get("lat"), el.get("lon")
        if not name or el_lat is None or el_lon is None:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        points.append({"name": name, "lat": el_lat, "lon": el_lon})
    points.sort(key=lambda p: p["name"].lower())
    return points


_batch_active_lock = threading.Lock()
_batch_active = False


def is_batch_fetch_active():
    with _batch_active_lock:
        return _batch_active


def _set_batch_active(value):
    global _batch_active
    with _batch_active_lock:
        _batch_active = value


def run_batch_fetch(
    street_fetcher,
    packs,
    country_code,
    use_gnaf=True,
    status_cb=None,
    is_cancelled=lambda: False,
):
    """Fetch every selected pack's chosen suburbs, synchronously. Call off
    the UI thread - see start_batch_fetch_background for the usual entry
    point. Each pack must already have a "suburbs" list - the user's
    checkbox selection from the wizard's suburb page.

    status_cb(str) - called with a human progress line before each pack and
    once with a final summary. Kept deliberately sparse (not per-suburb) so
    it doesn't spam a screen reader for a fetch that can run a long time.

    Returns a stats dict: {"areas": n, "fetched": n, "from_cache": n, "failed": n}.
    """
    def status(msg):
        if status_cb:
            status_cb(msg)
        miab_log("verbose", f"[CityPacks] {msg}", None)

    _set_batch_active(True)
    overall = {"areas": 0, "fetched": 0, "from_cache": 0, "failed": 0}
    try:
        for pack in packs:
            if is_cancelled():
                break
            suburbs = pack.get("suburbs") or []
            status(f"Fetching {pack['label']}... ({len(suburbs)} area(s))")
            for suburb in suburbs:
                if is_cancelled():
                    break
                try:
                    result = street_fetcher.fetch_road_data(
                        suburb["lat"], suburb["lon"], radius=SUBURB_RADIUS_M,
                        suburb_name=suburb.get("name"),
                        country_code=country_code, use_gnaf=use_gnaf,
                    )
                    from_cache = result[2]
                    overall["areas"] += 1
                    if from_cache:
                        overall["from_cache"] += 1
                    else:
                        overall["fetched"] += 1
                except Exception as e:
                    overall["failed"] += 1
                    miab_log("errors", f"[CityPacks] Area {suburb.get('name')} failed: {e}", None)
        status(
            f"City data download finished: {overall['fetched']} area(s) fetched, "
            f"{overall['from_cache']} already cached, {overall['failed']} failed."
        )
    finally:
        _set_batch_active(False)
    return overall


def start_batch_fetch_background(
    street_fetcher, packs, country_code, use_gnaf=True, status_cb=None,
):
    """Run run_batch_fetch() on a daemon thread and return immediately."""
    t = threading.Thread(
        target=run_batch_fetch,
        args=(street_fetcher, packs, country_code),
        kwargs={"use_gnaf": use_gnaf, "status_cb": status_cb},
        daemon=True,
    )
    t.start()
    return t


# ---------------------------------------------------------------------------
# Postcode range -> bounding box (via Nominatim, two lookups: start + end)
# ---------------------------------------------------------------------------

def _geocode_postcode(postcode, country_code):
    """Best-effort centre point for a postcode via Nominatim. None on failure."""
    params = urllib.parse.urlencode({
        "postalcode": postcode,
        "countrycodes": country_code.lower(),
        "format": "jsonv2",
        "limit": 1,
    })
    req = urllib.request.Request(
        f"https://nominatim.openstreetmap.org/search?{params}",
        headers={"User-Agent": "MapInABox/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception as e:
        miab_log("errors", f"[CityPacks] Postcode geocode failed for {postcode}: {e}", None)
    return None


def bbox_for_postcode_range(country_code, start, end):
    """Bounding box (south, north, west, east) spanning two postcodes'
    geocoded centres, with padding - the area suburb discovery then
    searches within. This is an approximation - postcodes aren't
    contiguous geographic areas - but discovery still only returns real
    named places within it, and the user picks from those individually.
    """
    p1 = _geocode_postcode(start, country_code)
    p2 = _geocode_postcode(end, country_code)
    if not p1 or not p2:
        return None
    lats = [p1[0], p2[0]]
    lons = [p1[1], p2[1]]
    pad = 0.15  # ~15km padding so the range's own edges get proper coverage
    return (min(lats) - pad, max(lats) + pad, min(lons) - pad, max(lons) + pad)


# ---------------------------------------------------------------------------
# Wizard dialog
# ---------------------------------------------------------------------------

INTRO_MESSAGE = (
    "Commonly explored suburbs can be downloaded to "
    "your computer to save significant time when navigating "
    "to them for the first time.\n\n"
    "Choose a country, then a state or region (or a postcode range), "
    "then pick the individual suburbs you require.\n\n"
    "Once confirmed, this wizard closes and the download continues in "
    "the background."
    "You can keep using the app, but avoid exploring brand-new "
    "areas at the same time - that uses the same street servers and will "
    "be slower until the download finishes. You can run this wizard "
    "again any time with Control+Shift+F11."
)


class CityPackWizardDialog(wx.Dialog):
    """Ctrl+Shift+F11 wizard - intro, country, state/postcode area, suburb
    checklist, confirm.

    ShowModal() returns wx.ID_OK with .result_packs and .result_country_code
    set once the user confirms a selection; the caller starts the actual
    fetch (start_batch_fetch_background) after the dialog is destroyed, so
    nothing about the download itself is tied to this dialog's lifetime.
    """

    PAGE_INTRO, PAGE_COUNTRY, PAGE_AREA, PAGE_SUBURBS, PAGE_CONFIRM = range(5)

    def __init__(
        self,
        parent,
        street_fetcher,
        df,
        initial_country_name=None,
        transport_download_cb=None,
    ):
        super().__init__(
            parent, title="Download Area Data",
            size=(600, 480),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self._street_fetcher = street_fetcher
        self._df = df
        self._initial_country_name = initial_country_name or ""
        self._transport_download_cb = transport_download_cb
        self._country_name = None
        self._area_label = None
        self._all_suburbs = []        # full discovered suburb_dict list for the current area
        self._checked_names = set()   # names checked so far - source of truth, survives filtering
        self._visible_suburbs = []    # suburb_dict list currently shown in the list control (post-filter)
        self.result_packs = None
        self.result_country_code = None

        panel = wx.Panel(self)
        outer = wx.BoxSizer(wx.VERTICAL)

        self.book = wx.Simplebook(panel)
        outer.Add(self.book, 1, wx.EXPAND | wx.ALL, 10)

        self._build_intro_page()
        self._build_country_page()
        self._build_area_page()
        self._build_suburbs_page()
        self._build_confirm_page()

        panel.SetSizer(outer)
        self.book.ChangeSelection(self.PAGE_INTRO)

        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)
        wx.CallAfter(self.continue_btn.SetFocus)

    # ------------------------------------------------------------------
    # Page: intro
    # ------------------------------------------------------------------

    def _build_intro_page(self):
        page = wx.Panel(self.book)
        vs = wx.BoxSizer(wx.VERTICAL)

        txt = wx.StaticText(page, label=INTRO_MESSAGE)
        txt.Wrap(540)
        vs.Add(txt, 1, wx.ALL | wx.EXPAND, 10)

        hs = wx.BoxSizer(wx.HORIZONTAL)
        self.continue_btn = wx.Button(page, label="&Continue")
        self.continue_btn.SetDefault()
        cancel_btn = wx.Button(page, wx.ID_CANCEL, "Cancel")
        hs.Add(self.continue_btn, 0, wx.RIGHT, 8)
        hs.Add(cancel_btn, 0)
        vs.Add(hs, 0, wx.ALL, 10)

        page.SetSizer(vs)
        self.book.AddPage(page, "Intro")

        self.continue_btn.Bind(
            wx.EVT_BUTTON, lambda e: self._goto(self.PAGE_COUNTRY, self.country_list))
        cancel_btn.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CANCEL))

    # ------------------------------------------------------------------
    # Page: country
    # ------------------------------------------------------------------

    def _build_country_page(self):
        page = wx.Panel(self.book)
        vs = wx.BoxSizer(wx.VERTICAL)

        vs.Add(wx.StaticText(page, label="Choose a country:"), 0, wx.ALL, 10)

        self._countries = list_countries(self._df)
        self.country_list = wx.ListBox(page, choices=self._countries)
        _bind_typeahead(self.country_list)
        default_index = 0
        if self._initial_country_name in self._countries:
            default_index = self._countries.index(self._initial_country_name)
        if self._countries:
            self.country_list.SetSelection(default_index)
        vs.Add(self.country_list, 1, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        hs = wx.BoxSizer(wx.HORIZONTAL)
        back_btn = wx.Button(page, label="&Back")
        next_btn = wx.Button(page, label="&Next")
        next_btn.SetDefault()
        hs.Add(back_btn, 0, wx.RIGHT, 8)
        hs.Add(next_btn, 0)
        vs.Add(hs, 0, wx.ALL, 10)

        page.SetSizer(vs)
        self.book.AddPage(page, "Country")

        back_btn.Bind(
            wx.EVT_BUTTON, lambda e: self._goto(self.PAGE_INTRO, self.continue_btn))
        next_btn.Bind(wx.EVT_BUTTON, self._on_country_next)
        self.country_list.Bind(wx.EVT_LISTBOX_DCLICK, self._on_country_next)

    def _on_country_next(self, event):
        sel = self.country_list.GetSelection()
        if sel == wx.NOT_FOUND:
            return
        self._country_name = self._countries[sel]
        self._populate_area_page()
        focus_target = self.state_list if self.state_list.GetCount() else self.postcode_from
        self._goto(self.PAGE_AREA, focus_target)

    # ------------------------------------------------------------------
    # Page: state/region or postcode range
    # ------------------------------------------------------------------

    def _build_area_page(self):
        page = wx.Panel(self.book)
        vs = wx.BoxSizer(wx.VERTICAL)

        self.area_label_ctrl = wx.StaticText(page, label="Choose a state or region:")
        vs.Add(self.area_label_ctrl, 0, wx.ALL, 10)

        self.state_list = wx.ListBox(page, choices=[])
        _bind_typeahead(self.state_list)
        vs.Add(self.state_list, 1, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        transport_btn = wx.Button(
            page, label="Download &Transport Data for This Area")
        transport_btn.Enable(self._transport_download_cb is not None)
        vs.Add(transport_btn, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        find_state_btn = wx.Button(page, label="&Find Suburbs In This State")
        vs.Add(find_state_btn, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        vs.Add(wx.StaticLine(page), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        vs.Add(wx.StaticText(page, label="Or use a postcode range instead:"),
               0, wx.LEFT | wx.RIGHT, 10)

        range_hs = wx.BoxSizer(wx.HORIZONTAL)
        range_hs.Add(wx.StaticText(page, label="From:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self.postcode_from = wx.TextCtrl(page, size=(70, -1))
        range_hs.Add(self.postcode_from, 0, wx.RIGHT, 10)
        range_hs.Add(wx.StaticText(page, label="To:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self.postcode_to = wx.TextCtrl(page, size=(70, -1))
        range_hs.Add(self.postcode_to, 0, wx.RIGHT, 10)
        find_range_btn = wx.Button(page, label="Find Suburbs In This &Range")
        range_hs.Add(find_range_btn, 0)
        vs.Add(range_hs, 0, wx.ALL, 10)

        self.area_status = wx.StaticText(page, label="")
        vs.Add(self.area_status, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        back_btn = wx.Button(page, label="&Back")
        vs.Add(back_btn, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        page.SetSizer(vs)
        self.book.AddPage(page, "Area")

        back_btn.Bind(
            wx.EVT_BUTTON, lambda e: self._goto(self.PAGE_COUNTRY, self.country_list))
        transport_btn.Bind(wx.EVT_BUTTON, self._on_download_transport)
        find_state_btn.Bind(wx.EVT_BUTTON, self._on_find_state)
        self.state_list.Bind(wx.EVT_LISTBOX_DCLICK, self._on_find_state)
        find_range_btn.Bind(wx.EVT_BUTTON, self._on_find_postcode_range)

    def _populate_area_page(self):
        states = list_states_for_country(self._df, self._country_name)
        self.state_list.Set(states)
        if states:
            self.state_list.SetSelection(0)
        self.area_label_ctrl.SetLabel(
            f"Choose a state or region in {self._country_name}:"
            if states else
            f"{self._country_name} has no listed states/regions - use a postcode range below."
        )

    def _on_find_state(self, event):
        sel = self.state_list.GetSelection()
        if sel == wx.NOT_FOUND:
            return
        state = self.state_list.GetString(sel)
        self._start_area_discovery(state, area_name=state, country=self._country_name)

    def _on_download_transport(self, event):
        """Start GTFS prefetch for the selected region's largest listed city."""
        sel = self.state_list.GetSelection()
        if sel == wx.NOT_FOUND or not self._transport_download_cb:
            return
        state = self.state_list.GetString(sel)
        rows = self._df[
            (self._df["country"] == self._country_name)
            & (self._df["admin_name"] == state)
        ].copy()
        if rows.empty:
            wx.MessageBox(
                f"No representative location was found for {state}.",
                "Transport Download", wx.OK | wx.ICON_INFORMATION, self,
            )
            return
        try:
            import pandas as pd
            rows["_population_sort"] = pd.to_numeric(
                rows.get("population", 0), errors="coerce").fillna(0)
            row = rows.sort_values(
                "_population_sort", ascending=False).iloc[0]
            lat, lon = float(row["lat"]), float(row["lng"])
        except Exception as exc:
            miab_log(
                "errors",
                f"[CityPacks] Could not choose transport point for {state}: {exc}",
                None,
            )
            wx.MessageBox(
                f"Could not determine where to download transport data for {state}.",
                "Transport Download", wx.OK | wx.ICON_WARNING, self,
            )
            return

        started = self._transport_download_cb(
            state, self._country_name, lat, lon)
        if started:
            msg = (
                f"Transport data for {state} is downloading in the background. "
                "You can continue using this wizard."
            )
            self.area_status.SetLabel(msg)
            _speak(msg)

    def _on_find_postcode_range(self, event):
        country_code = "au" if self._country_name == "Australia" else ""
        start = self.postcode_from.GetValue().strip()
        end = self.postcode_to.GetValue().strip()
        if not (start.isdigit() and end.isdigit()):
            wx.MessageBox(
                "Enter numeric postcodes in both fields, e.g. 2000 to 2300.",
                "Invalid postcode range", wx.OK | wx.ICON_WARNING, self,
            )
            return
        if int(start) > int(end):
            start, end = end, start
        label = f"Postcodes {start} to {end}"
        self._start_area_discovery(
            label, postcode_range=(country_code, start, end))

    def _start_area_discovery(self, area_label, area_name=None, country=None, postcode_range=None):
        """Resolve an area to a bounding box (state/region via Nominatim,
        or a postcode range via two Nominatim lookups) then run the live
        Overpass suburb discovery within it - all off the UI thread, since
        both are network calls. Populates the suburb checklist page when done.
        """
        self._area_label = area_label
        status_msg = (
            f"Searching for suburbs in {area_label}. This can take up "
            "to a minute for a large state or region."
        )
        # A wx.StaticText label change is not announced automatically by
        # a screen reader - nothing was reading it out even though the
        # text was correctly on the visible page. _speak() pushes it
        # straight to the screen reader; the label is kept too for
        # sighted users and so the wait state is visible if re-focused.
        self.area_status.SetLabel(status_msg)
        _speak(status_msg)
        self.Disable()
        wx.BeginBusyCursor()

        def worker():
            geometry = None
            if postcode_range:
                country_code, start, end = postcode_range
                bbox = bbox_for_postcode_range(country_code, start, end)
            else:
                bbox, geometry = geocode_admin_boundary(area_name, country)
            if not bbox:
                wx.CallAfter(self._area_discovery_failed, area_label)
                return
            south, north, west, east = bbox
            suburbs = discover_suburbs_in_bbox(
                self._street_fetcher._overpass, south, north, west, east)
            if geometry is not None:
                before = len(suburbs)
                suburbs = [s for s in suburbs if geometry.contains(Point(s["lon"], s["lat"]))]
                dropped = before - len(suburbs)
                if dropped:
                    miab_log("verbose", f"[CityPacks] Dropped {dropped} suburb(s) outside {area_label}'s real border", None)
            wx.CallAfter(self._area_discovery_done, area_label, suburbs)

        threading.Thread(target=worker, daemon=True).start()

    def _area_discovery_failed(self, area_label):
        wx.EndBusyCursor()
        self.Enable()
        self.area_status.SetLabel("")
        _speak(f"Couldn't find a location for {area_label}.")
        wx.MessageBox(
            f"Couldn't find a location for {area_label}. Check the name/postcodes and try again.",
            "Lookup failed", wx.OK | wx.ICON_WARNING, self,
        )

    def _area_discovery_done(self, area_label, suburbs):
        wx.EndBusyCursor()
        self.Enable()
        self.area_status.SetLabel("")
        _speak(f"Found {len(suburbs)} suburb(s) in {area_label}.")
        self._populate_suburbs_page(area_label, suburbs)
        if not suburbs:
            wx.MessageBox(
                f"OpenStreetMap has no suburbs/towns tagged in {area_label}. "
                "Try a different state/region or postcode range.",
                "Nothing found", wx.OK | wx.ICON_INFORMATION, self,
            )
            return
        focus_target = self.suburb_filter
        self._goto(self.PAGE_SUBURBS, focus_target)

    # ------------------------------------------------------------------
    # Page: suburb checklist
    # ------------------------------------------------------------------

    def _build_suburbs_page(self):
        page = wx.Panel(self.book)
        vs = wx.BoxSizer(wx.VERTICAL)

        self.suburbs_label = wx.StaticText(page, label="Select suburbs to fetch:")
        vs.Add(self.suburbs_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)

        filter_hs = wx.BoxSizer(wx.HORIZONTAL)
        filter_hs.Add(wx.StaticText(page, label="Filter:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.suburb_filter = wx.TextCtrl(page)
        filter_hs.Add(self.suburb_filter, 1)
        vs.Add(filter_hs, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        self.suburbs_hint = wx.StaticText(page, label="")
        vs.Add(self.suburbs_hint, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        # A real native list control, not individual wx.CheckBox widgets
        # in a panel. A checkbox panel has no built-in keyboard navigation
        # of its own - no arrow-key movement between rows, no type-ahead -
        # and hundreds of separate native controls for a state like NSW
        # was slow to build. wx.ListCtrl in report mode with
        # EnableCheckBoxes(True) uses the native Windows ListView checkbox
        # column - the same control Windows Explorer uses for checkbox
        # selection - so this gets proper list semantics for free: arrow
        # keys move between rows, typing a letter jumps to the first
        # matching row (built into the control), and each row's checked
        # state is a real native checkbox a screen reader announces
        # correctly, unlike wx.CheckListBox's owner-drawn approximation.
        # It's also a single control rather than N separate ones, so the
        # full suburb list can be shown without lazy-loading tricks.
        self.suburb_list = wx.ListCtrl(page, style=wx.LC_REPORT | wx.BORDER_SUNKEN)
        self.suburb_list.InsertColumn(0, "Suburb")
        self._suburb_checkboxes_supported = hasattr(self.suburb_list, "EnableCheckBoxes")
        if self._suburb_checkboxes_supported:
            self.suburb_list.EnableCheckBoxes(True)
        else:
            # Fall back to multi-selection standing in for "checked" on a
            # wxPython build too old for native checkbox columns (EnableCheckBoxes
            # needs wx 4.1+). Still a real list control with native
            # navigation either way.
            self.suburb_list.SetSingleStyle(wx.LC_SINGLE_SEL, False)
        vs.Add(self.suburb_list, 1, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        select_hs = wx.BoxSizer(wx.HORIZONTAL)
        select_all_btn = wx.Button(page, label="Select &All Shown")
        clear_all_btn = wx.Button(page, label="&Clear All")
        # A real checkbox, not a toggle button: a wx.ToggleButton's
        # pressed/not-pressed state is exactly the kind of thing that
        # doesn't reliably announce - the same problem wx.CheckListBox
        # had earlier. Only one of these exists on this page, so none of
        # the "hundreds of individual checkboxes is slow" concerns apply.
        self.review_btn = wx.CheckBox(page, label="&Review selected suburbs only")
        select_hs.Add(select_all_btn, 0, wx.RIGHT, 8)
        select_hs.Add(clear_all_btn, 0, wx.RIGHT, 8)
        select_hs.Add(self.review_btn, 0)
        vs.Add(select_hs, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        hs = wx.BoxSizer(wx.HORIZONTAL)
        back_btn = wx.Button(page, label="&Back")
        next_btn = wx.Button(page, label="&Next")
        next_btn.SetDefault()
        hs.Add(back_btn, 0, wx.RIGHT, 8)
        hs.Add(next_btn, 0)
        vs.Add(hs, 0, wx.ALL, 10)

        page.SetSizer(vs)
        self.book.AddPage(page, "Suburbs")

        back_btn.Bind(
            wx.EVT_BUTTON, lambda e: self._goto(self.PAGE_AREA, self.state_list))
        next_btn.Bind(wx.EVT_BUTTON, self._on_suburbs_next)
        self.suburb_filter.Bind(wx.EVT_TEXT, self._on_suburb_filter_changed)
        select_all_btn.Bind(wx.EVT_BUTTON, self._on_select_all_shown)
        clear_all_btn.Bind(wx.EVT_BUTTON, self._on_clear_all)
        self.review_btn.Bind(wx.EVT_CHECKBOX, self._on_review_toggled)
        if self._suburb_checkboxes_supported:
            self.suburb_list.Bind(wx.EVT_LIST_ITEM_CHECKED, self._on_suburb_item_checked)
            self.suburb_list.Bind(wx.EVT_LIST_ITEM_UNCHECKED, self._on_suburb_item_unchecked)
        else:
            self.suburb_list.Bind(wx.EVT_LIST_ITEM_SELECTED, self._on_suburb_item_checked)
            self.suburb_list.Bind(wx.EVT_LIST_ITEM_DESELECTED, self._on_suburb_item_unchecked)

    def _populate_suburbs_page(self, area_label, suburbs):
        self._all_suburbs = suburbs
        self._checked_names = set()
        self.suburbs_label.SetLabel(f"Select suburbs in {area_label} to fetch ({len(suburbs)} found):")
        self.suburb_filter.SetValue("")
        self.review_btn.SetValue(False)
        self._rebuild_suburb_list()

    def _rebuild_suburb_list(self):
        """Rebuild the list from the current filter text, or - if Review
        Selected is toggled on - from every checked suburb regardless of
        filter. Unlike the earlier per-checkbox design, the full list can
        be shown at once here (no lazy-loading needed): a single
        wx.ListCtrl handles a thousand-plus rows fine, since it's one
        native control, not one HWND per suburb."""
        show_selected_only = self.review_btn.GetValue()
        needle = self.suburb_filter.GetValue().strip().lower()
        if show_selected_only:
            matches = [s for s in self._all_suburbs if s["name"] in self._checked_names]
            hint = f"{len(matches)} selected"
        elif needle:
            matches = [s for s in self._all_suburbs if needle in s["name"].lower()]
            hint = f"{len(matches)} match(es)"
        else:
            matches = self._all_suburbs
            hint = f"{len(matches)} suburb(s)"
        hint += f" · {len(self._checked_names)} selected overall"
        self.suburbs_hint.SetLabel(hint)

        self._visible_suburbs = matches
        self.suburb_list.Freeze()
        try:
            self.suburb_list.DeleteAllItems()
            for i, suburb in enumerate(matches):
                idx = self.suburb_list.InsertItem(i, suburb["name"])
                checked = suburb["name"] in self._checked_names
                if self._suburb_checkboxes_supported:
                    self.suburb_list.CheckItem(idx, checked)
                elif checked:
                    self.suburb_list.SetItemState(idx, wx.LIST_STATE_SELECTED, wx.LIST_STATE_SELECTED)
            self.suburb_list.SetColumnWidth(0, wx.LIST_AUTOSIZE_USEHEADER)
        finally:
            self.suburb_list.Thaw()

    def _on_suburb_filter_changed(self, event):
        if self.review_btn.GetValue():
            self.review_btn.SetValue(False)
        self._rebuild_suburb_list()

    def _on_review_toggled(self, event):
        self._rebuild_suburb_list()

    def _on_suburb_item_checked(self, event):
        idx = event.GetIndex()
        if 0 <= idx < len(self._visible_suburbs):
            self._checked_names.add(self._visible_suburbs[idx]["name"])
        self._update_selected_count()

    def _on_suburb_item_unchecked(self, event):
        idx = event.GetIndex()
        if 0 <= idx < len(self._visible_suburbs):
            self._checked_names.discard(self._visible_suburbs[idx]["name"])
        self._update_selected_count()

    def _update_selected_count(self):
        label = self.suburbs_hint.GetLabel()
        base = label.split(" · ")[0]
        self.suburbs_hint.SetLabel(f"{base} · {len(self._checked_names)} selected overall")

    def _on_select_all_shown(self, event):
        for suburb in self._visible_suburbs:
            self._checked_names.add(suburb["name"])
        self._rebuild_suburb_list()

    def _on_clear_all(self, event):
        self._checked_names = set()
        self._rebuild_suburb_list()

    def _on_suburbs_next(self, event):
        selected = [s for s in self._all_suburbs if s["name"] in self._checked_names]
        # Defensive dedupe by name: a Queensland run showed one suburb
        # (Rockhampton) counted 3 times in the final "N area(s)" total -
        # each duplicate just cost an instant cache-hit re-check rather
        # than a real extra fetch, but it made the confirm-page count and
        # time estimate wrong. Collapsing to one entry per name here fixes
        # the symptom regardless of exactly how a duplicate got in.
        seen_names = set()
        deduped = []
        for s in selected:
            key = s["name"].lower()
            if key in seen_names:
                continue
            seen_names.add(key)
            deduped.append(s)
        selected = deduped
        if not selected:
            wx.MessageBox(
                "Select at least one suburb.",
                "Nothing selected", wx.OK | wx.ICON_WARNING, self,
            )
            return
        self._selected_packs = [{
            "label": self._area_label,
            "suburbs": selected,
        }]
        self._populate_confirm_page()
        self._goto(self.PAGE_CONFIRM, self.confirm_start_btn)

    # ------------------------------------------------------------------
    # Page: confirm
    # ------------------------------------------------------------------

    def _build_confirm_page(self):
        page = wx.Panel(self.book)
        vs = wx.BoxSizer(wx.VERTICAL)

        vs.Add(wx.StaticText(page, label="Ready to fetch:"), 0, wx.ALL, 10)

        self.confirm_list = wx.TextCtrl(
            page, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP,
        )
        vs.Add(self.confirm_list, 1, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        hs = wx.BoxSizer(wx.HORIZONTAL)
        back_btn = wx.Button(page, label="&Back")
        self.confirm_start_btn = wx.Button(page, label="&Start")
        self.confirm_start_btn.SetDefault()
        hs.Add(back_btn, 0, wx.RIGHT, 8)
        hs.Add(self.confirm_start_btn, 0)
        vs.Add(hs, 0, wx.ALL, 10)

        page.SetSizer(vs)
        self.book.AddPage(page, "Confirm")

        back_btn.Bind(
            wx.EVT_BUTTON, lambda e: self._goto(self.PAGE_SUBURBS, self.suburb_filter))
        self.confirm_start_btn.Bind(wx.EVT_BUTTON, self._on_confirm_start)

    def _populate_confirm_page(self):
        pack = self._selected_packs[0]
        suburbs = pack["suburbs"]
        names = ", ".join(s["name"] for s in suburbs)
        est_minutes = (len(suburbs) * EST_SECONDS_PER_AREA) / 60
        self.confirm_list.SetValue(
            f"{pack['label']}: {len(suburbs)} suburb(s) selected.\n"
            f"Estimated time if nothing is cached yet: about {est_minutes:.0f} minute(s) "
            "(much faster for anywhere you've already visited).\n\n"
            + names
        )

    def _on_confirm_start(self, event):
        self.result_packs = self._selected_packs
        self.result_country_code = "au" if self._country_name == "Australia" else ""
        self.EndModal(wx.ID_OK)

    # ------------------------------------------------------------------
    # Navigation helpers
    # ------------------------------------------------------------------

    def _goto(self, page_index, focus_ctrl=None):
        self.book.ChangeSelection(page_index)
        if focus_ctrl is not None:
            wx.CallAfter(focus_ctrl.SetFocus)

    def _on_char_hook(self, event):
        code = event.GetKeyCode()
        if code == wx.WXK_ESCAPE:
            self.EndModal(wx.ID_CANCEL)
            return
        event.Skip()
