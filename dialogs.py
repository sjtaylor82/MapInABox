"""dialogs.py — reusable wx.Dialog subclasses for Map in a Box.

All standalone dialog classes live here.  ``MapNavigator`` imports what
it needs rather than embedding UI logic alongside network and map code.

Classes
-------
SettingsDialog        — walk-mode POI settings
POICategoryDialog     — choose a POI category before searching
StreetSearchDialog    — filterable street/name picker
                        (replaces the old _pick_street_dialog method AND
                         the inline _street_search_show flow — one dialog,
                         one code path)
"""

import os
import wx
import wx.adv


def _primary_down(event) -> bool:
    """Treat Control as the main modifier on Windows/Linux and Command on macOS."""
    if wx.Platform == "__WXMAC__" and hasattr(event, "CmdDown"):
        return event.CmdDown()
    return event.ControlDown()


def show_api_key_required(parent, title: str, message: str,
                          link_label: str, link_url: str) -> None:
    """Modal dialog telling the user a key is missing, with a clickable signup link."""
    dlg = wx.Dialog(parent, title=title,
                    style=wx.DEFAULT_DIALOG_STYLE | wx.STAY_ON_TOP)
    vs = wx.BoxSizer(wx.VERTICAL)

    txt = wx.StaticText(dlg, label=message)
    txt.Wrap(420)
    vs.Add(txt, 0, wx.ALL, 14)

    link = wx.adv.HyperlinkCtrl(dlg, label=link_label, url=link_url)
    def _on_link_key(evt):
        if evt.GetKeyCode() in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER, wx.WXK_SPACE):
            import webbrowser
            webbrowser.open(link_url)
        else:
            evt.Skip()
    link.Bind(wx.EVT_KEY_DOWN, _on_link_key)
    vs.Add(link, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 14)

    btn = wx.Button(dlg, wx.ID_OK, "OK")
    btn.SetDefault()
    vs.Add(btn, 0, wx.ALIGN_RIGHT | wx.RIGHT | wx.BOTTOM, 10)

    dlg.SetSizerAndFit(vs)
    dlg.CentreOnParent()
    dlg.ShowModal()
    dlg.Destroy()


def show_optional_key_warning(parent, title: str, message: str) -> None:
    """Modal dialog explaining a missing optional key and its limitations."""
    dlg = wx.Dialog(parent, title=title,
                    style=wx.DEFAULT_DIALOG_STYLE | wx.STAY_ON_TOP)
    vs = wx.BoxSizer(wx.VERTICAL)

    txt = wx.StaticText(dlg, label=message)
    txt.Wrap(440)
    vs.Add(txt, 0, wx.ALL, 14)

    btn = wx.Button(dlg, wx.ID_OK, "OK")
    btn.SetDefault()
    vs.Add(btn, 0, wx.ALIGN_RIGHT | wx.RIGHT | wx.BOTTOM, 10)

    dlg.SetSizerAndFit(vs)
    dlg.CentreOnParent()
    dlg.ShowModal()
    dlg.Destroy()


def show_open_source_notice(parent) -> None:
    """Tell users the app prefers free/open services and accepts optional keys."""
    dlg = wx.Dialog(
        parent,
        title="Open Sources and Optional Keys",
        style=wx.DEFAULT_DIALOG_STYLE | wx.STAY_ON_TOP,
    )
    vs = wx.BoxSizer(wx.VERTICAL)

    message = (
        "Every effort has been made to keep Map in a Box usable with open "
        "data and free endpoints. The app will fall back to those services "
        "where it can.\n\n"
        "If you want richer coverage or higher limits, you can still add "
        "your own API keys in Settings."
    )
    txt = wx.StaticText(dlg, label=message)
    txt.Wrap(430)
    vs.Add(txt, 0, wx.ALL, 14)

    btn = wx.Button(dlg, wx.ID_OK, "OK")
    btn.SetDefault()
    vs.Add(btn, 0, wx.ALIGN_RIGHT | wx.RIGHT | wx.BOTTOM, 10)

    dlg.SetSizerAndFit(vs)
    dlg.CentreOnParent()
    dlg.ShowModal()
    dlg.Destroy()

# ---------------------------------------------------------------------------
# Constants mirrored here to avoid circular imports with core.
# These must be kept in sync with core.POI_CATEGORY_CHOICES.
# ---------------------------------------------------------------------------

POI_CATEGORY_CHOICES: list[tuple[str, str]] = [
    ("all",       "All nearby"),
    ("food",      "Food & drink"),
    ("shopping",  "Shopping"),
    ("transport", "Public transport"),
    ("trains",    "Trains & stations"),
    ("health",    "Health & medical"),
    ("community", "Community & services"),
    ("arts",      "Arts, venues & landmarks"),
    ("parks",     "Parks & outdoors"),
    ("accommodation", "Accommodation"),
]

# ---------------------------------------------------------------------------
# SettingsDialog
# ---------------------------------------------------------------------------

class SettingsDialog(wx.Dialog):
    """Walk-mode POI announcement settings."""

    def __init__(self, parent, settings: dict, user_dir: str = "") -> None:
        super().__init__(
            parent, title="Settings", size=(640, 680),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self.settings = dict(settings)
        self._user_dir = user_dir
        panel = wx.Panel(self)
        vs = wx.BoxSizer(wx.VERTICAL)
        self.notebook = wx.Notebook(panel)

        self.general_page = wx.ScrolledWindow(self.notebook, style=wx.VSCROLL)
        self.api_page = wx.ScrolledWindow(self.notebook, style=wx.VSCROLL)
        self.logging_page = wx.ScrolledWindow(self.notebook, style=wx.VSCROLL)
        for page in (self.general_page, self.api_page, self.logging_page):
            page.SetScrollRate(0, 20)

        self.notebook.AddPage(self.general_page, "General")
        self.notebook.AddPage(self.api_page, "API Keys")
        self.notebook.AddPage(self.logging_page, "Logging")
        vs.Add(self.notebook, 1, wx.EXPAND | wx.ALL, 8)

        general_vs = wx.BoxSizer(wx.VERTICAL)
        api_vs = wx.BoxSizer(wx.VERTICAL)
        log_vs = wx.BoxSizer(wx.VERTICAL)

        general_vs.Add(wx.StaticText(self.general_page, label="Walking mode POI announcements:"), 0, wx.ALL, 8)
        self.cb_walk = wx.CheckBox(self.general_page, label="Announce nearby POIs while walking")
        general_vs.Add(self.cb_walk, 0, wx.LEFT | wx.BOTTOM, 12)

        general_vs.Add(wx.StaticText(self.general_page, label="POIs to announce while walking:"), 0, wx.LEFT, 8)
        self.combo_cat = wx.ComboBox(
            self.general_page,
            choices=[label for _, label in POI_CATEGORY_CHOICES],
            style=wx.CB_READONLY,
        )
        general_vs.Add(self.combo_cat, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)

        general_vs.Add(wx.StaticText(self.general_page, label="Announce POIs within:"), 0, wx.LEFT, 8)
        self.combo_radius = wx.ComboBox(
            self.general_page,
            choices=["50 metres", "80 metres", "120 metres"],
            style=wx.CB_READONLY,
        )
        general_vs.Add(self.combo_radius, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)

        self.cb_walk_cat = wx.CheckBox(self.general_page, label="Include category label in announcement")
        general_vs.Add(self.cb_walk_cat, 0, wx.LEFT | wx.BOTTOM, 8)

        general_vs.Add(wx.StaticLine(self.general_page), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        general_vs.Add(wx.StaticText(self.general_page, label="Map announcements:"), 0, wx.LEFT, 8)
        self.cb_climate_zones = wx.CheckBox(self.general_page, label="Announce climate zones during navigation")
        general_vs.Add(self.cb_climate_zones, 0, wx.LEFT | wx.BOTTOM, 8)

        general_vs.Add(wx.StaticLine(self.general_page), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        general_vs.Add(wx.StaticText(self.general_page, label="Spatial tones:"), 0, wx.LEFT, 8)
        self.combo_spatial_tones = wx.ComboBox(
            self.general_page,
            choices=["World", "Country", "Region"],
            style=wx.CB_READONLY,
        )
        general_vs.Add(self.combo_spatial_tones, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)

        general_vs.Add(wx.StaticLine(self.general_page), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        general_vs.Add(wx.StaticText(self.general_page, label="Challenge direction:"), 0, wx.LEFT, 8)
        self.combo_challenge_direction = wx.ComboBox(
            self.general_page,
            choices=["Map learning", "Shortest globe"],
            style=wx.CB_READONLY,
        )
        general_vs.Add(self.combo_challenge_direction, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)

        general_vs.Add(wx.StaticLine(self.general_page), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        general_vs.Add(wx.StaticText(self.general_page, label="Weather temperature units:"), 0, wx.LEFT, 8)
        self.combo_weather_units = wx.ComboBox(
            self.general_page,
            choices=["Automatic (country-based)", "Celsius", "Fahrenheit"],
            style=wx.CB_READONLY,
        )
        general_vs.Add(self.combo_weather_units, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)

        general_vs.Add(wx.StaticLine(self.general_page), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        general_vs.Add(wx.StaticText(self.general_page, label="POI database (for street/free mode):"), 0, wx.LEFT, 8)
        self.combo_poi_source = wx.ComboBox(
            self.general_page,
            choices=["OpenStreetMap", "HERE"],
            style=wx.CB_READONLY,
        )
        general_vs.Add(self.combo_poi_source, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)

        general_vs.Add(wx.StaticText(self.general_page, label="Named POI search radius:"), 0, wx.LEFT, 8)
        self.combo_poi_name_radius = wx.ComboBox(
            self.general_page,
            choices=[f"{km} km" for km in range(1, 11)],
            style=wx.CB_READONLY,
        )
        general_vs.Add(self.combo_poi_name_radius, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)

        general_vs.Add(wx.StaticLine(self.general_page), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        general_vs.Add(wx.StaticText(self.general_page, label="Navigation provider (walking routes):"), 0, wx.LEFT, 8)
        self.combo_nav = wx.ComboBox(
            self.general_page,
            choices=["OpenStreetMap", "Google Maps", "HERE"],
            style=wx.CB_READONLY,
        )
        general_vs.Add(self.combo_nav, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)

        general_vs.Add(wx.StaticLine(self.general_page), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        general_vs.Add(wx.StaticText(self.general_page, label="Departure board source:"), 0, wx.LEFT, 8)
        self.combo_departure_board = wx.ComboBox(
            self.general_page,
            choices=["GTFS data", "Google Places"],
            style=wx.CB_READONLY,
        )
        general_vs.Add(self.combo_departure_board, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)

        self.btn_gtfs = wx.Button(self.general_page, label="Refresh Transit Feed Catalog")
        general_vs.Add(self.btn_gtfs, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        self.gtfs_refreshed = False
        self.btn_gtfs.Bind(wx.EVT_BUTTON, self._on_gtfs_refresh)

        self.general_page.SetSizer(general_vs)

        api_vs.Add(wx.StaticText(self.api_page, label="Google API key — enhanced geocoding/routing, satellite/street view, Google navigation:"), 0, wx.ALL, 8)
        self.txt_google_key = wx.TextCtrl(self.api_page, style=wx.TE_PASSWORD)
        api_vs.Add(self.txt_google_key, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)
        api_vs.Add(wx.adv.HyperlinkCtrl(self.api_page, label="Get a Google API key",
            url="https://developers.google.com/maps/get-started"), 0, wx.LEFT | wx.BOTTOM, 8)

        api_vs.Add(wx.StaticLine(self.api_page), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        api_vs.Add(wx.StaticText(self.api_page, label="Gemini API key — optional descriptions for satellite/street view, transit, and menus:"), 0, wx.LEFT, 8)
        self.txt_gemini_key = wx.TextCtrl(self.api_page, style=wx.TE_PASSWORD)
        api_vs.Add(self.txt_gemini_key, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)
        api_vs.Add(wx.adv.HyperlinkCtrl(self.api_page, label="Get a Gemini API key (free — takes 30 seconds at Google AI Studio)",
            url="https://aistudio.google.com/app/apikey"), 0, wx.LEFT | wx.BOTTOM, 8)

        api_vs.Add(wx.StaticLine(self.api_page), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        api_vs.Add(wx.StaticText(self.api_page, label="HERE API key — optional POI details, HERE navigation, and departure board:"), 0, wx.LEFT, 8)
        self.txt_here_key = wx.TextCtrl(self.api_page, style=wx.TE_PASSWORD)
        api_vs.Add(self.txt_here_key, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)
        api_vs.Add(wx.adv.HyperlinkCtrl(self.api_page, label="Get a HERE API key",
            url="https://developer.here.com/sign-up"), 0, wx.LEFT | wx.BOTTOM, 8)

        api_vs.Add(wx.StaticLine(self.api_page), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        api_vs.Add(wx.StaticText(self.api_page, label="OpenRouteService API key — optional walking/driving distance between marks:"), 0, wx.LEFT, 8)
        self.txt_ors_key = wx.TextCtrl(self.api_page, style=wx.TE_PASSWORD)
        api_vs.Add(self.txt_ors_key, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)
        api_vs.Add(wx.adv.HyperlinkCtrl(self.api_page, label="Get an OpenRouteService API key",
            url="https://openrouteservice.org/sign-up/"), 0, wx.LEFT | wx.BOTTOM, 8)

        api_vs.Add(wx.StaticLine(self.api_page), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        api_vs.Add(wx.StaticText(self.api_page, label="AviationStack API key — optional airport departure/arrival boards:"), 0, wx.LEFT, 8)
        self.txt_aviationstack_key = wx.TextCtrl(self.api_page, style=wx.TE_PASSWORD)
        api_vs.Add(self.txt_aviationstack_key, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)
        api_vs.Add(wx.adv.HyperlinkCtrl(self.api_page, label="Get an AviationStack API key",
            url="https://aviationstack.com/signup/free"), 0, wx.LEFT | wx.BOTTOM, 8)

        api_vs.Add(wx.StaticLine(self.api_page), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        api_vs.Add(wx.StaticText(self.api_page, label="OpenSky client ID — optional overhead flight destination lookup (free):"), 0, wx.LEFT, 8)
        self.txt_opensky_id = wx.TextCtrl(self.api_page)
        api_vs.Add(self.txt_opensky_id, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)
        api_vs.Add(wx.StaticText(self.api_page, label="OpenSky client secret:"), 0, wx.LEFT, 8)
        self.txt_opensky_secret = wx.TextCtrl(self.api_page, style=wx.TE_PASSWORD)
        api_vs.Add(self.txt_opensky_secret, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)
        api_vs.Add(wx.adv.HyperlinkCtrl(self.api_page, label="Register a free OpenSky account",
            url="https://opensky-network.org/index.php?option=com_users&view=registration"),
            0, wx.LEFT | wx.BOTTOM, 8)

        api_vs.Add(wx.StaticLine(self.api_page), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        api_vs.Add(wx.StaticText(self.api_page, label="RapidAPI key — optional flight search and hotel search (F12 tools):"), 0, wx.LEFT, 8)
        self.txt_rapidapi_key = wx.TextCtrl(self.api_page, style=wx.TE_PASSWORD)
        api_vs.Add(self.txt_rapidapi_key, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)
        hs_rapid = wx.BoxSizer(wx.HORIZONTAL)
        hs_rapid.Add(wx.adv.HyperlinkCtrl(self.api_page, label="Sign up for RapidAPI",
            url="https://rapidapi.com/auth/sign-up"), 0, wx.RIGHT, 16)
        hs_rapid.Add(wx.adv.HyperlinkCtrl(self.api_page, label="Subscribe: Priceline API",
            url="https://rapidapi.com/tipsters/api/priceline-com-provider"), 0, wx.RIGHT, 16)
        hs_rapid.Add(wx.adv.HyperlinkCtrl(self.api_page, label="Subscribe: Timetable Lookup API",
            url="https://rapidapi.com/obryan.sw/api/timetable-lookup"), 0)
        api_vs.Add(hs_rapid, 0, wx.LEFT | wx.BOTTOM, 8)

        self.api_page.SetSizer(api_vs)

        log = settings.get("logging", {})
        self.cb_log_errors    = wx.CheckBox(self.logging_page, label="Errors — exceptions, API failures, missing data")
        self.cb_log_street    = wx.CheckBox(self.logging_page, label="Street/POI data — Overpass queries, cache hits/misses")
        self.cb_log_snap      = wx.CheckBox(self.logging_page, label="Street snap — jump/search snap decisions and arrow key movement")
        self.cb_log_api       = wx.CheckBox(self.logging_page, label="HERE/Gemini API calls — requests and responses")
        self.cb_log_challenge = wx.CheckBox(self.logging_page, label="Challenge sessions — player, country, time, score")
        self.cb_log_features  = wx.CheckBox(self.logging_page, label="Feature usage — keys pressed, lookups made")
        self.cb_log_nav       = wx.CheckBox(self.logging_page, label="Navigation events — country entries, crossings, jumps")
        self.cb_log_verbose   = wx.CheckBox(self.logging_page, label="Verbose diagnostics — extra traces written to miab.log")
        log_vs.Add(wx.StaticText(self.logging_page, label="Logging (miab.log):"), 0, wx.ALL, 8)
        self.cb_log_errors.SetValue(log.get("errors",    True))
        self.cb_log_street.SetValue(log.get("street",    False))
        self.cb_log_snap.SetValue(log.get("snap",        False))
        self.cb_log_api.SetValue(log.get("api_calls",    False))
        self.cb_log_challenge.SetValue(log.get("challenges",     False))
        self.cb_log_features.SetValue(log.get("feature_usage",   False))
        self.cb_log_nav.SetValue(log.get("navigation",           False))
        self.cb_log_verbose.SetValue(log.get("verbose", False))
        for cb in (self.cb_log_errors, self.cb_log_street, self.cb_log_snap, self.cb_log_api,
                   self.cb_log_challenge, self.cb_log_features, self.cb_log_nav, self.cb_log_verbose):
            log_vs.Add(cb, 0, wx.LEFT | wx.BOTTOM, 8)
        self.logging_page.SetSizer(log_vs)

        hs = wx.BoxSizer(wx.HORIZONTAL)
        ok_btn     = wx.Button(panel, wx.ID_OK,     "Save")
        cancel_btn = wx.Button(panel, wx.ID_CANCEL, "Cancel")
        btn_home   = wx.Button(panel, label="Set Home Location")
        btn_folder = wx.Button(panel, label="Open Settings Folder")
        hs.Add(ok_btn, 0, wx.RIGHT, 8)
        hs.Add(cancel_btn, 0, wx.RIGHT, 8)
        hs.Add(btn_home, 0, wx.RIGHT, 8)
        hs.Add(btn_folder, 0)
        vs.Add(hs, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        panel.SetSizer(vs)

        self.set_home_requested = False
        btn_home.Bind(wx.EVT_BUTTON, self._on_set_home)
        btn_folder.Bind(wx.EVT_BUTTON, self._on_open_folder)

        # Populate from existing settings
        self.cb_walk.SetValue(settings.get("walk_announce_pois", True))
        cat_keys = [k for k, _ in POI_CATEGORY_CHOICES]
        cur = settings.get("walk_poi_category", "all")
        self.combo_cat.SetSelection(cat_keys.index(cur) if cur in cat_keys else 0)
        self.combo_radius.SetSelection(
            {50: 0, 80: 1, 120: 2}.get(settings.get("walk_poi_radius_m", 80), 1)
        )
        self.cb_walk_cat.SetValue(settings.get("walk_announce_category", True))
        self.cb_climate_zones.SetValue(settings.get("announce_climate_zones", True))
        spatial_mode = settings.get("spatial_tones_mode", "world")
        self.combo_spatial_tones.SetSelection(
            {"world": 0, "country": 1, "region": 2, "city": 2}.get(spatial_mode, 0))
        challenge_direction = settings.get("challenge_direction_mode", "map")
        self.combo_challenge_direction.SetSelection(
            {"map": 0, "globe": 1}.get(challenge_direction, 0))
        weather_units = settings.get("weather_temperature_unit", "auto")
        self.combo_weather_units.SetSelection(
            {"auto": 0, "celsius": 1, "fahrenheit": 2}.get(weather_units, 0))
        nav_provider = settings.get("nav_provider", "osm")
        nav_idx = {"osm": 0, "google": 1, "here": 2}.get(nav_provider, 0)
        self.combo_nav.SetSelection(nav_idx)
        departure_source = settings.get("departure_board_source", "gtfs")
        self.combo_departure_board.SetSelection(1 if departure_source == "google" else 0)
        poi_source = settings.get("poi_source", "osm")
        self.combo_poi_source.SetSelection(1 if poi_source == "here" else 0)
        try:
            name_radius_km = int(settings.get("poi_name_search_radius_km", 10))
        except (TypeError, ValueError):
            name_radius_km = 10
        name_radius_km = max(1, min(10, name_radius_km))
        self.combo_poi_name_radius.SetSelection(name_radius_km - 1)
        self.txt_google_key.SetValue(settings.get("google_api_key", ""))
        self.txt_gemini_key.SetValue(settings.get("gemini_api_key", ""))
        self.txt_here_key.SetValue(settings.get("here_api_key", ""))
        self.txt_ors_key.SetValue(settings.get("ors_api_key", ""))
        self.txt_aviationstack_key.SetValue(settings.get("aviationstack_api_key", ""))
        self.txt_rapidapi_key.SetValue(settings.get("rapidapi_key", ""))
        self.txt_opensky_id.SetValue(settings.get("opensky_client_id", ""))
        self.txt_opensky_secret.SetValue(settings.get("opensky_client_secret", ""))

        ok_btn.Bind(wx.EVT_BUTTON, self._on_ok)
        self.Bind(
            wx.EVT_CHAR_HOOK,
            lambda e: (self.EndModal(wx.ID_CANCEL)
                       if e.GetKeyCode() == wx.WXK_ESCAPE else e.Skip()),
        )
        self.CentreOnParent()

    def _on_set_home(self, event):
        self.set_home_requested = True
        self._on_ok(event)

    def _on_open_folder(self, event) -> None:
        if not (self._user_dir and os.path.isdir(self._user_dir)):
            return
        import sys, subprocess
        if sys.platform == "darwin":
            subprocess.Popen(["open", self._user_dir])
        else:
            os.startfile(self._user_dir)


    def _on_ok(self, event) -> None:
        cat_keys = [k for k, _ in POI_CATEGORY_CHOICES]
        nav_provider = {0: "osm", 1: "google", 2: "here"}.get(
            self.combo_nav.GetSelection(), "osm")
        departure_board_source = "google" if self.combo_departure_board.GetSelection() == 1 else "gtfs"
        spatial_mode = {0: "world", 1: "country", 2: "region"}.get(
            self.combo_spatial_tones.GetSelection(), "world")
        challenge_direction = {0: "map", 1: "globe"}.get(
            self.combo_challenge_direction.GetSelection(), "map")
        weather_units = {0: "auto", 1: "celsius", 2: "fahrenheit"}.get(
            self.combo_weather_units.GetSelection(), "auto")
        self.settings.update({
            "walk_announce_pois":     self.cb_walk.GetValue(),
            "walk_poi_category":      cat_keys[max(0, self.combo_cat.GetSelection())],
            "walk_poi_radius_m":      [50, 80, 120][max(0, self.combo_radius.GetSelection())],
            "walk_announce_category": self.cb_walk_cat.GetValue(),
            "announce_climate_zones": self.cb_climate_zones.GetValue(),
            "spatial_tones_mode":     spatial_mode,
            "challenge_direction_mode": challenge_direction,
            "weather_temperature_unit": weather_units,
            "nav_provider":           nav_provider,
            "departure_board_source": departure_board_source,
            "poi_source":             "here" if self.combo_poi_source.GetSelection() == 1 else "osm",
            "poi_name_search_radius_km": max(1, self.combo_poi_name_radius.GetSelection() + 1),
            "google_api_key":         self.txt_google_key.GetValue().strip(),
            "gemini_api_key":         self.txt_gemini_key.GetValue().strip(),
            "here_api_key":             self.txt_here_key.GetValue().strip(),
            "ors_api_key":              self.txt_ors_key.GetValue().strip(),
            "aviationstack_api_key":    self.txt_aviationstack_key.GetValue().strip(),
            "rapidapi_key":             self.txt_rapidapi_key.GetValue().strip(),
            "opensky_client_id":        self.txt_opensky_id.GetValue().strip(),
            "opensky_client_secret":    self.txt_opensky_secret.GetValue().strip(),
            "logging": {
                "errors":        self.cb_log_errors.GetValue(),
                "street":        self.cb_log_street.GetValue(),
                "snap":          self.cb_log_snap.GetValue(),
                "api_calls":     self.cb_log_api.GetValue(),
                "challenges":    self.cb_log_challenge.GetValue(),
                "feature_usage": self.cb_log_features.GetValue(),
                "navigation":    self.cb_log_nav.GetValue(),
                "verbose":       self.cb_log_verbose.GetValue(),
            },
        })
        self.EndModal(wx.ID_OK)

    def _on_gtfs_refresh(self, event) -> None:
        """Mark that the caller should trigger a GTFS refresh after dialog closes."""
        self.gtfs_refreshed = True
        self.btn_gtfs.SetLabel("Catalog refresh will run on save")
        self.btn_gtfs.Disable()


# ---------------------------------------------------------------------------
# POICategoryDialog
# ---------------------------------------------------------------------------

class POICategoryDialog(wx.Dialog):
    """Choose a POI category, optional name, and data source before searching."""

    def __init__(
        self,
        parent,
        available_sources: list[str] | None = None,
        preferred_source: str = "osm",
        initial_key: str = "all",
        initial_name: str = "",
        notice: str = "",
    ) -> None:
        super().__init__(
            parent, title="POI Search", size=(430, 300),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self.selected_key    = None
        self.selected_name   = ""
        self.selected_source = "osm"
        sources = available_sources or ["osm"]
        panel = wx.Panel(self)
        vs = wx.BoxSizer(wx.VERTICAL)

        if notice:
            msg = wx.StaticText(panel, label=notice)
            msg.Wrap(390)
            vs.Add(msg, 0, wx.ALL | wx.EXPAND, 10)

        info = wx.StaticText(panel, label="Search by name (optional), then choose category and source.")
        info.Wrap(390)
        vs.Add(info, 0, wx.ALL | wx.EXPAND, 10)

        vs.Add(wx.StaticText(panel, label="Name:"), 0, wx.LEFT, 10)
        self.txt_name = wx.TextCtrl(panel, style=wx.TE_PROCESS_ENTER)
        if initial_name:
            self.txt_name.SetValue(initial_name)
            self.txt_name.SetSelection(-1, -1)
        vs.Add(self.txt_name, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        vs.Add(wx.StaticText(panel, label="Category:"), 0, wx.LEFT, 10)
        self.combo = wx.ComboBox(
            panel,
            choices=[label for _, label in POI_CATEGORY_CHOICES],
            style=wx.CB_READONLY | wx.TE_PROCESS_ENTER,
        )
        keys = [key for key, _ in POI_CATEGORY_CHOICES]
        initial_idx = keys.index(initial_key) if initial_key in keys else 0
        self.combo.SetSelection(initial_idx)
        vs.Add(self.combo, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        vs.Add(wx.StaticText(panel, label="Source:"), 0, wx.LEFT, 10)
        source_labels = {"osm": "OpenStreetMap", "here": "HERE", "google": "Google Maps"}
        self._source_keys = sources
        self.combo_source = wx.ComboBox(
            panel,
            choices=[source_labels.get(s, s) for s in sources],
            style=wx.CB_READONLY,
        )
        default_idx = sources.index(preferred_source) if preferred_source in sources else 0
        self.combo_source.SetSelection(default_idx)
        vs.Add(self.combo_source, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        hs = wx.BoxSizer(wx.HORIZONTAL)
        go_btn     = wx.Button(panel, wx.ID_OK,     "Go")
        cancel_btn = wx.Button(panel, wx.ID_CANCEL, "Cancel")
        hs.Add(go_btn, 0, wx.RIGHT, 8)
        hs.Add(cancel_btn, 0)
        vs.Add(hs, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        panel.SetSizer(vs)

        go_btn.Bind(wx.EVT_BUTTON, self._on_go)
        self.combo.Bind(wx.EVT_COMBOBOX, self._on_choice)
        self.combo.Bind(wx.EVT_TEXT_ENTER, self._on_go)
        self.txt_name.Bind(wx.EVT_TEXT_ENTER, self._on_go)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)
        self.txt_name.SetFocus()
        self.CentreOnParent()

    def _on_char_hook(self, event) -> None:
        code = event.GetKeyCode()
        if code == wx.WXK_ESCAPE:
            self.EndModal(wx.ID_CANCEL)
            return
        if code in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            self._on_go(event)
            return
        event.Skip()

    def _on_choice(self, event) -> None:
        idx = self.combo.GetSelection()
        if idx != wx.NOT_FOUND:
            self.selected_key = POI_CATEGORY_CHOICES[idx][0]
        event.Skip()

    def _on_go(self, event) -> None:
        idx = self.combo.GetSelection()
        if idx == wx.NOT_FOUND:
            return
        self.selected_key    = POI_CATEGORY_CHOICES[idx][0]
        self.selected_name   = self.txt_name.GetValue().strip()
        src_idx = self.combo_source.GetSelection()
        self.selected_source = (self._source_keys[src_idx]
                                if 0 <= src_idx < len(self._source_keys)
                                else "osm")
        self.EndModal(wx.ID_OK)


# ---------------------------------------------------------------------------
# StreetSearchDialog  (merged replacement for _pick_street_dialog +
#                      _street_search_show)
# ---------------------------------------------------------------------------

#: Sentinel returned in ``selected_name`` when the user clicks "Load More".
LOAD_MORE_SENTINEL = "__LOAD_MORE__"


class StreetSearchDialog(wx.Dialog):
    """Filterable street/name picker dialog.

    This single class replaces three former implementations:

    * older street-search dialog flows
    * navigation address picking

    Parameters
    ----------
    parent:
        Parent window.
    street_names:
        Iterable of street name strings to show.
    title:
        Dialog window title.
    prompt:
        Instructional label shown above the search box.
    show_load_more:
        If ``True`` a "Load More Streets" button is shown.  When the user
        clicks it ``selected_name`` is set to ``LOAD_MORE_SENTINEL`` and
        the dialog closes with ``wx.ID_OK``.
    extended:
        If ``True`` an extra banner label is shown indicating these are
        wider-area streets.  The "Load More" button is hidden.
    """

    def __init__(
        self,
        parent,
        street_names: list[str],
        title: str = "Street Search",
        prompt: str = "Type to filter streets. Use Up/Down to browse. Press Enter to jump.",
        show_load_more: bool = False,
        extended: bool = False,
    ) -> None:
        super().__init__(
            parent, title=title, size=(560, 460),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self.street_names   = list(street_names)
        self.filtered       = list(street_names)
        self.selected_name  = None   # set to chosen name or LOAD_MORE_SENTINEL
        self._show_load_more = show_load_more and not extended

        panel = wx.Panel(self)
        vs    = wx.BoxSizer(wx.VERTICAL)

        if extended:
            banner = wx.StaticText(panel, label="Wider area streets. Select one to jump there.")
            vs.Add(banner, 0, wx.ALL, 6)

        info = wx.StaticText(panel, label=prompt)
        info.Wrap(520)
        vs.Add(info, 0, wx.ALL | wx.EXPAND, 10)

        self.search = wx.SearchCtrl(panel, style=wx.TE_PROCESS_ENTER)
        self.search.ShowSearchButton(True)
        self.search.ShowCancelButton(True)
        vs.Add(self.search, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        self.listbox = wx.ListBox(panel, style=wx.LB_SINGLE)
        vs.Add(self.listbox, 1, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        btn_sizer  = wx.BoxSizer(wx.HORIZONTAL)
        ok_btn     = wx.Button(panel, wx.ID_OK,     "Jump")
        cancel_btn = wx.Button(panel, wx.ID_CANCEL, "Cancel")
        btn_sizer.Add(ok_btn, 0, wx.RIGHT, 8)
        if self._show_load_more:
            self._wider_btn = wx.Button(panel, label="Load More Streets")
            btn_sizer.Add(self._wider_btn, 0, wx.RIGHT, 8)
            self._wider_btn.Bind(wx.EVT_BUTTON, self._on_load_more)
        btn_sizer.Add(cancel_btn, 0)
        vs.Add(btn_sizer, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        panel.SetSizer(vs)
        self._refresh_list()

        self.search.Bind(wx.EVT_TEXT,         self._on_text)
        self.search.Bind(wx.EVT_TEXT_ENTER,   self._on_enter)
        self.listbox.Bind(wx.EVT_LISTBOX_DCLICK, self._on_enter)
        self.listbox.Bind(wx.EVT_KEY_DOWN,    self._on_list_key)
        ok_btn.Bind(wx.EVT_BUTTON,            self._on_enter)
        self.Bind(wx.EVT_CHAR_HOOK,           self._on_char_hook)

        self.search.SetFocus()
        self.CentreOnParent()

    # ------------------------------------------------------------------
    # Filtering
    # ------------------------------------------------------------------

    def _refresh_list(self) -> None:
        import re
        q = self.search.GetValue().strip().lower() if hasattr(self, "search") else ""
        if q:
            pattern = re.compile(r"\b" + re.escape(q))
            self.filtered = [n for n in self.street_names if pattern.search(n.lower())]
        else:
            self.filtered = list(self.street_names)
        self.listbox.Set(self.filtered)
        if self.filtered:
            self.listbox.SetSelection(0)

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_text(self, event) -> None:
        self._refresh_list()
        event.Skip()

    def _choose_current(self) -> None:
        sel = self.listbox.GetSelection()
        if sel == wx.NOT_FOUND and self.filtered:
            sel = 0
        if sel != wx.NOT_FOUND and 0 <= sel < len(self.filtered):
            self.selected_name = self.filtered[sel]
            self.EndModal(wx.ID_OK)
        else:
            wx.Bell()

    def _on_enter(self, event) -> None:
        self._choose_current()

    def _on_load_more(self, event) -> None:
        self.selected_name = LOAD_MORE_SENTINEL
        self.EndModal(wx.ID_OK)

    def _on_list_key(self, event) -> None:
        key = event.GetKeyCode()
        if key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            self._choose_current()
            return
        event.Skip()

    def _on_char_hook(self, event) -> None:
        code = event.GetKeyCode()
        if code == wx.WXK_ESCAPE:
            self.EndModal(wx.ID_CANCEL)
            return
        if code in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            obj = self.FindFocus()
            if obj in (self.search, self.listbox):
                self._choose_current()
                return
        event.Skip()


# ---------------------------------------------------------------------------
# Route Tools dialogs
# ---------------------------------------------------------------------------

class ToolsMenuDialog(wx.Dialog):
    """F12 Tools menu — pick an action from a short list."""

    TOOLS = [
        ("Detour Calculator",  "detour_calculator"),
        ("Route Explorer",     "route_explorer"),
        ("Toll Compare",       "toll_compare"),
        ("Journey Planner",    "journey_planner"),
        ("Departure Board",    "departure_board"),
        ("Flight Search",      "flight_search"),
        ("Hotel Search",       "hotel_search"),
    ]

    def __init__(self, parent) -> None:
        super().__init__(parent, title="Tools", style=wx.DEFAULT_DIALOG_STYLE)
        panel = wx.Panel(self)
        vs = wx.BoxSizer(wx.VERTICAL)

        vs.Add(wx.StaticText(panel, label="Choose a tool:"), 0,
               wx.LEFT | wx.TOP, 10)

        self.listbox = wx.ListBox(
            panel, choices=[t[0] for t in self.TOOLS],
            style=wx.LB_SINGLE,
        )
        self.listbox.SetSelection(0)
        vs.Add(self.listbox, 1, wx.ALL | wx.EXPAND, 10)

        panel.SetSizer(vs)
        self.SetSize(300, 250)

        self.selected_tool = ""
        self.listbox.Bind(wx.EVT_LISTBOX_DCLICK, self._on_choose)
        self.listbox.Bind(wx.EVT_KEY_DOWN, self._on_key)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)
        wx.CallAfter(self.listbox.SetFocus)

    def _on_choose(self, event=None):
        sel = self.listbox.GetSelection()
        if sel != wx.NOT_FOUND:
            self.selected_tool = self.TOOLS[sel][1]
            self.EndModal(wx.ID_OK)

    def _on_key(self, event):
        event.Skip()

    def _on_char_hook(self, event):
        code = event.GetKeyCode()
        if code == wx.WXK_ESCAPE:
            self.EndModal(wx.ID_CANCEL)
            return
        if code in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            self._on_choose()
            return
        event.Skip()


class StopEntryDialog(wx.Dialog):
    """Prompt the user for an address/suburb name.  Returns the text."""

    def __init__(self, parent, prompt, default=""):
        super().__init__(parent, title="Enter Stop",
                         style=wx.DEFAULT_DIALOG_STYLE)
        panel = wx.Panel(self)
        vs = wx.BoxSizer(wx.VERTICAL)

        vs.Add(wx.StaticText(panel, label=prompt), 0, wx.LEFT | wx.TOP, 10)
        self.text = wx.TextCtrl(panel)
        self.text.SetValue(default)
        vs.Add(self.text, 0, wx.ALL | wx.EXPAND, 10)

        hs = wx.BoxSizer(wx.HORIZONTAL)
        ok_btn = wx.Button(panel, wx.ID_OK, "OK")
        cancel_btn = wx.Button(panel, wx.ID_CANCEL, "Cancel")
        hs.Add(ok_btn, 0, wx.RIGHT, 8)
        hs.Add(cancel_btn, 0)
        vs.Add(hs, 0, wx.LEFT | wx.BOTTOM, 10)

        panel.SetSizer(vs)
        self.SetSize(400, 150)

        ok_btn.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_OK))
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)
        wx.CallAfter(self.text.SetFocus)

    def GetValue(self):
        return self.text.GetValue().strip()

    def _on_char_hook(self, event):
        code = event.GetKeyCode()
        if code == wx.WXK_ESCAPE:
            self.EndModal(wx.ID_CANCEL)
            return
        if code in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            self.EndModal(wx.ID_OK)
            return
        event.Skip()


class RouteResultsDialog(wx.Dialog):
    """Read-only dialog displaying route comparison results."""

    def __init__(self, parent, title, text):
        super().__init__(parent, title=title,
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        panel = wx.Panel(self)
        vs = wx.BoxSizer(wx.VERTICAL)

        self.results = wx.TextCtrl(
            panel, value=text,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP,
        )
        vs.Add(self.results, 1, wx.ALL | wx.EXPAND, 10)

        close_btn = wx.Button(panel, wx.ID_CLOSE, "Close")
        vs.Add(close_btn, 0, wx.LEFT | wx.BOTTOM, 10)

        panel.SetSizer(vs)
        self.SetSize(500, 350)

        close_btn.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CLOSE))
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)
        wx.CallAfter(self.results.SetFocus)

    def _on_char_hook(self, event):
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            self.EndModal(wx.ID_CLOSE)
            return
        event.Skip()


# ---------------------------------------------------------------------------
# Journey Planner dialogs
# ---------------------------------------------------------------------------

class DateTimePickerDialog(wx.Dialog):
    """Combo-based date/time picker. Returns a datetime object."""

    def __init__(self, parent, title="Choose date and time"):
        super().__init__(parent, title=title, style=wx.DEFAULT_DIALOG_STYLE)
        import datetime as _dt
        now = _dt.datetime.now()
        # Round up to next 5 minutes
        mins = now.minute
        remainder = mins % 5
        if remainder:
            now = now.replace(minute=mins + (5 - remainder), second=0, microsecond=0)
        else:
            now = now.replace(second=0, microsecond=0)

        panel = wx.Panel(self)
        vs = wx.BoxSizer(wx.VERTICAL)

        vs.Add(wx.StaticText(panel, label="Day:"), 0, wx.LEFT | wx.TOP, 8)
        self.combo_day = wx.ComboBox(
            panel, choices=[str(d) for d in range(1, 32)],
            style=wx.CB_READONLY)
        self.combo_day.SetSelection(now.day - 1)
        vs.Add(self.combo_day, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 8)

        vs.Add(wx.StaticText(panel, label="Month:"), 0, wx.LEFT | wx.TOP, 8)
        months = ["January", "February", "March", "April", "May", "June",
                  "July", "August", "September", "October", "November", "December"]
        self.combo_month = wx.ComboBox(
            panel, choices=months, style=wx.CB_READONLY)
        self.combo_month.SetSelection(now.month - 1)
        vs.Add(self.combo_month, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 8)

        vs.Add(wx.StaticText(panel, label="Year:"), 0, wx.LEFT | wx.TOP, 8)
        years = [str(now.year), str(now.year + 1)]
        self.combo_year = wx.ComboBox(
            panel, choices=years, style=wx.CB_READONLY)
        self.combo_year.SetSelection(0)
        vs.Add(self.combo_year, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 8)

        vs.Add(wx.StaticText(panel, label="Hour:"), 0, wx.LEFT | wx.TOP, 8)
        self.combo_hour = wx.ComboBox(
            panel, choices=[f"{h:02d}" for h in range(24)],
            style=wx.CB_READONLY)
        self.combo_hour.SetSelection(now.hour)
        vs.Add(self.combo_hour, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 8)

        vs.Add(wx.StaticText(panel, label="Minute:"), 0, wx.LEFT | wx.TOP, 8)
        self.combo_min = wx.ComboBox(
            panel, choices=[f"{m:02d}" for m in range(0, 60, 5)],
            style=wx.CB_READONLY)
        self.combo_min.SetSelection(min(now.minute // 5, 11))
        vs.Add(self.combo_min, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 8)

        hs = wx.BoxSizer(wx.HORIZONTAL)
        ok_btn = wx.Button(panel, wx.ID_OK, "OK")
        cancel_btn = wx.Button(panel, wx.ID_CANCEL, "Cancel")
        hs.Add(ok_btn, 0, wx.RIGHT, 8)
        hs.Add(cancel_btn, 0)
        vs.Add(hs, 0, wx.ALL, 10)

        panel.SetSizer(vs)
        self.SetSize(300, 400)

        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)
        wx.CallAfter(self.combo_day.SetFocus)

    def get_datetime(self):
        """Return a datetime object from the selected values, or None."""
        import datetime as _dt
        try:
            day = int(self.combo_day.GetStringSelection())
            month = self.combo_month.GetSelection() + 1
            year = int(self.combo_year.GetStringSelection())
            hour = int(self.combo_hour.GetStringSelection())
            minute = int(self.combo_min.GetStringSelection())
            return _dt.datetime(year, month, day, hour, minute)
        except (ValueError, TypeError):
            return None

    def _on_char_hook(self, event):
        code = event.GetKeyCode()
        if code == wx.WXK_ESCAPE:
            self.EndModal(wx.ID_CANCEL)
            return
        if code in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            self.EndModal(wx.ID_OK)
            return
        event.Skip()


class JourneyResultsDialog(wx.Dialog):
    """Two-level journey results: listbox of route summaries,
    Enter expands to detail, Escape/Backspace goes back."""

    def __init__(self, parent, routes):
        super().__init__(parent, title="Journey Planner",
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self._routes = routes
        self._showing_detail = False

        panel = wx.Panel(self)
        self._sizer = wx.BoxSizer(wx.VERTICAL)

        # Level 1: route summary list
        self.listbox = wx.ListBox(
            panel,
            choices=[r["summary"] for r in routes],
            style=wx.LB_SINGLE,
        )
        if routes:
            self.listbox.SetSelection(0)
        self._sizer.Add(self.listbox, 1, wx.ALL | wx.EXPAND, 10)

        # Level 2: detail text (hidden initially)
        self.detail = wx.TextCtrl(
            panel, value="",
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP,
        )
        self._sizer.Add(self.detail, 1, wx.ALL | wx.EXPAND, 10)
        self.detail.Hide()

        close_btn = wx.Button(panel, wx.ID_CLOSE, "Close")
        self._sizer.Add(close_btn, 0, wx.LEFT | wx.BOTTOM, 10)

        panel.SetSizer(self._sizer)
        self.SetSize(600, 400)

        close_btn.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CLOSE))
        self.listbox.Bind(wx.EVT_LISTBOX_DCLICK, lambda e: self._show_detail())
        self.listbox.Bind(wx.EVT_KEY_DOWN, self._on_list_key)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)
        wx.CallAfter(self.listbox.SetFocus)

    def _show_detail(self):
        sel = self.listbox.GetSelection()
        if sel == wx.NOT_FOUND or sel >= len(self._routes):
            return
        self.detail.SetValue(self._routes[sel]["detail_text"])
        self.listbox.Hide()
        self.detail.Show()
        self._showing_detail = True
        self.Layout()
        self.detail.SetFocus()
        self.detail.SetInsertionPoint(0)

    def _show_list(self):
        self.detail.Hide()
        self.listbox.Show()
        self._showing_detail = False
        self.Layout()
        self.listbox.SetFocus()

    def _on_char_hook(self, event):
        code = event.GetKeyCode()
        if self._showing_detail:
            if code in (wx.WXK_ESCAPE, wx.WXK_BACK):
                self._show_list()
                return
        else:
            if code in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
                self._show_detail()
                return
            if code == wx.WXK_ESCAPE:
                self.EndModal(wx.ID_CLOSE)
                return
        event.Skip()


class TransitLookupDialog(wx.Dialog):
    """Three-level departure board: stations -> departures -> GTFS stops.
    Enter drills down, Escape/Backspace goes back.

    Level 0 — nearby stations
    Level 1 — departures from selected station
    Level 2 — GTFS candidate routes (when multiple matches found)
    Level 3 — stop sequence for selected route/direction
    """

    def __init__(self, parent, stations, fetch_departures_cb, fetch_stops_cb=None):
        super().__init__(parent, title="Departure Board",
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self._stations = stations
        self._fetch_departures = fetch_departures_cb
        self._fetch_stops = fetch_stops_cb
        self._departures = []
        self._candidates = []   # list of candidate dicts when level==2
        self._level = 0  # 0=stations, 1=departures, 2=candidates, 3=stops
        self._current_stop_list = None   # full stop dicts (with lat/lon) for Ctrl+Alt+F
        self._current_route_name = ""

        panel = wx.Panel(self)
        self._sizer = wx.BoxSizer(wx.VERTICAL)

        self._title_label = wx.StaticText(panel, label="Nearby stops and stations:")
        self._sizer.Add(self._title_label, 0, wx.LEFT | wx.TOP, 10)

        self.listbox = wx.ListBox(
            panel, choices=[s["label"] for s in stations], style=wx.LB_SINGLE)
        if stations:
            self.listbox.SetSelection(0)
        self._sizer.Add(self.listbox, 1, wx.ALL | wx.EXPAND, 10)

        self.dep_listbox = wx.ListBox(panel, style=wx.LB_SINGLE)
        self._sizer.Add(self.dep_listbox, 1, wx.ALL | wx.EXPAND, 10)
        self.dep_listbox.Hide()

        self.cand_listbox = wx.ListBox(panel, style=wx.LB_SINGLE)
        self._sizer.Add(self.cand_listbox, 1, wx.ALL | wx.EXPAND, 10)
        self.cand_listbox.Hide()

        self.stops_listbox = wx.ListBox(panel, style=wx.LB_SINGLE)
        self._sizer.Add(self.stops_listbox, 1, wx.ALL | wx.EXPAND, 10)
        self.stops_listbox.Hide()

        close_btn = wx.Button(panel, wx.ID_CLOSE, "Close")
        self._sizer.Add(close_btn, 0, wx.LEFT | wx.BOTTOM, 10)

        panel.SetSizer(self._sizer)
        self.SetSize(550, 400)

        close_btn.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CLOSE))
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)
        wx.CallAfter(self.listbox.SetFocus)

    def _show_departures(self):
        sel = self.listbox.GetSelection()
        if sel == wx.NOT_FOUND or sel >= len(self._stations):
            return
        station = self._stations[sel]
        self._title_label.SetLabel(f"Departures from {station['name']}:")
        try:
            self._departures = self._fetch_departures(station)
        except Exception as e:
            self._departures = []
            self._title_label.SetLabel(f"Error: {e}")
        self.dep_listbox.Clear()
        if self._departures:
            for d in self._departures:
                self.dep_listbox.Append(d["label"])
            self.dep_listbox.SetSelection(0)
        else:
            self.dep_listbox.Append("No departures found.")
        self.listbox.Hide()
        self.stops_listbox.Hide()
        self.cand_listbox.Hide()
        self.dep_listbox.Show()
        self._level = 1
        self.Layout()
        self.dep_listbox.SetFocus()

    def _show_stops(self):
        if not self._fetch_stops:
            return
        sel = self.dep_listbox.GetSelection()
        if sel == wx.NOT_FOUND or sel >= len(self._departures):
            return
        dep = self._departures[sel]
        self._current_route_name = f"{dep['line']} to {dep['direction']}"
        self._current_stop_list = None
        self._title_label.SetLabel(f"Stops: {dep['line']} to {dep['direction']}:")
        self.stops_listbox.Clear()
        self.stops_listbox.Append("Loading timetable data...")
        self.dep_listbox.Hide()
        self.cand_listbox.Hide()
        self.stops_listbox.Show()
        self._level = 3
        self.Layout()
        self.stops_listbox.SetFocus()

        import threading
        def _fetch():
            try:
                result = self._fetch_stops(dep)
            except Exception as e:
                result = [f"Error: {e}"]
            wx.CallAfter(self._populate_stops, result)
        threading.Thread(target=_fetch, daemon=True).start()

    def _populate_stops(self, result):
        """Handle the return from _fetch_stops.

        result may be:
          - list of stop name strings  → display stop sequence (level 3)
          - {"__candidates__": [...]}  → show candidate picker (level 2)
        """
        # ── Candidate choice list ─────────────────────────────────────
        if isinstance(result, dict) and "__candidates__" in result:
            self._candidates = result["__candidates__"]
            self.cand_listbox.Clear()
            for c in self._candidates:
                self.cand_listbox.Append(c["label"])
            if self._candidates:
                self.cand_listbox.SetSelection(0)
            self._title_label.SetLabel(
                "Multiple routes found — choose one (Enter to view stops):")
            self.stops_listbox.Hide()
            self.dep_listbox.Hide()
            self.cand_listbox.Show()
            self._level = 2
            self.Layout()
            self.cand_listbox.SetFocus()
            return

        # ── Normal stop list ──────────────────────────────────────────
        self.stops_listbox.Clear()
        stops = result if isinstance(result, list) else []
        coords_stops = []
        if stops:
            for i, s in enumerate(stops, 1):
                if isinstance(s, dict):
                    name = s.get("name", s.get("stop_name", "?"))
                    if s.get("lat") and s.get("lon"):
                        coords_stops.append(s)
                else:
                    name = str(s)
                self.stops_listbox.Append(f"{i}. {name}")
            self.stops_listbox.SetSelection(0)
        else:
            self.stops_listbox.Append("No timetable data available for this service.")
        if coords_stops:
            self._current_stop_list = coords_stops
        self._level = 3
        self.cand_listbox.Hide()
        self.stops_listbox.Show()
        self.Layout()
        self.stops_listbox.SetFocus()

    def _show_stops_for_candidate(self):
        """Load and display the stop sequence for the selected candidate."""
        sel = self.cand_listbox.GetSelection()
        if sel == wx.NOT_FOUND or sel >= len(self._candidates):
            return
        candidate = self._candidates[sel]
        stop_list  = candidate.get("stop_list", [])
        stop_names = [s.get("name", s.get("stop_name", "Unknown"))
                      for s in stop_list]
        self._current_stop_list = [s for s in stop_list
                                    if s.get("lat") and s.get("lon")]
        self._current_route_name = candidate.get("label", "")
        self._title_label.SetLabel(f"Stops: {candidate['label']}:")
        self.stops_listbox.Clear()
        if stop_names:
            for i, name in enumerate(stop_names, 1):
                self.stops_listbox.Append(f"{i}. {name}")
            self.stops_listbox.SetSelection(0)
        else:
            self.stops_listbox.Append("No stop data for this route variant.")
        self.cand_listbox.Hide()
        self.stops_listbox.Show()
        self._level = 3
        self.Layout()
        self.stops_listbox.SetFocus()

    def _on_char_hook(self, event):
        code = event.GetKeyCode()
        # Ctrl+Alt+F — find food along the stop sequence shown at level 3
        if (_primary_down(event) and event.AltDown()
                and code in (ord('F'), ord('f'))):
            parent = self.GetParent()
            if self._level == 3 and self._current_stop_list:
                import threading
                threading.Thread(
                    target=parent._tool_find_food_transit_line,
                    args=({"name": self._current_route_name,
                           "stops": self._current_stop_list},),
                    daemon=True,
                ).start()
            else:
                parent._status_update(
                    "No stop data available. View a stop sequence first.",
                    force=True)
            return
        if code in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            if self._level == 0:
                self._show_departures()
                return
            elif self._level == 1 and self._fetch_stops:
                self._show_stops()
                return
            elif self._level == 2:
                self._show_stops_for_candidate()
                return
        if code in (wx.WXK_ESCAPE, wx.WXK_BACK):
            if self._level == 3:
                self.stops_listbox.Hide()
                # Go back to candidates if we came from there, else departures
                if self._candidates:
                    self.cand_listbox.Show()
                    self._level = 2
                    self.Layout()
                    self.cand_listbox.SetFocus()
                else:
                    self.dep_listbox.Show()
                    self._level = 1
                    self.Layout()
                    self.dep_listbox.SetFocus()
                return
            elif self._level == 2:
                self._candidates = []
                self.cand_listbox.Hide()
                self.dep_listbox.Show()
                self._level = 1
                self.Layout()
                self.dep_listbox.SetFocus()
                return
            elif self._level == 1:
                self._title_label.SetLabel("Nearby stops and stations:")
                self.dep_listbox.Hide()
                self.listbox.Show()
                self._level = 0
                self.Layout()
                self.listbox.SetFocus()
                return
            else:
                self.EndModal(wx.ID_CLOSE)
                return
        event.Skip()


class FlightSearchDialog(wx.Dialog):
    """Flight search — two-step airport picker (origin then destination)."""

    def __init__(self, parent, airports_csv_path: str):
        super().__init__(parent, title="Flight search — origin",
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.airports_csv_path = airports_csv_path
        self.origin_iata = ""
        self.dest_iata   = ""
        self._airports   = []
        self._load_airports()
        self._matches    = []  # current suggestion list

        vs = wx.BoxSizer(wx.VERTICAL)

        self._prompt_lbl = wx.StaticText(self, label="From — type city or airport name:")
        vs.Add(self._prompt_lbl, 0, wx.LEFT | wx.TOP, 8)
        self.txt = wx.TextCtrl(self)
        vs.Add(self.txt, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)

        self.lb = wx.ListBox(self, style=wx.LB_SINGLE)
        self.lb.SetMinSize((420, 180))
        vs.Add(self.lb, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 4)

        hs = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_next = wx.Button(self, wx.ID_OK, "Next")
        btn_cancel    = wx.Button(self, wx.ID_CANCEL, "Cancel")
        hs.Add(self.btn_next, 0, wx.RIGHT, 8)
        hs.Add(btn_cancel)
        vs.Add(hs, 0, wx.ALIGN_RIGHT | wx.ALL, 8)

        self.SetSizer(vs)
        self.Fit()
        self.CentreOnScreen()

        self.txt.Bind(wx.EVT_TEXT, self._on_text)
        self.lb.Bind(wx.EVT_LISTBOX_DCLICK, self._on_dclick)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_key)
        self.btn_next.Bind(wx.EVT_BUTTON, self._on_next)
        self.txt.SetFocus()

    def _load_airports(self):
        import csv
        try:
            with open(self.airports_csv_path, encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    apt_type = row.get("type", "")
                    if apt_type not in ("large_airport", "medium_airport"):
                        continue
                    iata = row.get("iata_code", "").strip()
                    if not iata:
                        continue
                    name    = row.get("name", "")
                    city    = row.get("municipality", "") or ""
                    country = row.get("iso_country", "")
                    is_large = 1 if apt_type == "large_airport" else 0
                    self._airports.append((name, iata, city, country, is_large))
        except Exception as exc:
            print(f"[FlightSearch] Airport load failed: {exc}")

    def _suggest(self, q):
        q = q.lower().strip()
        if not q or len(q) < 2:
            return []

        scored = []
        for name, iata, city, country, is_large in self._airports:
            city_l = city.lower()
            name_l = name.lower()
            iata_l = iata.lower()

            if iata_l == q:
                score = 0  # exact IATA match
            elif city_l == q:
                score = 1  # exact city match
            elif city_l.startswith(q):
                score = 2  # city starts with
            elif q in city_l:
                score = 3  # city contains
            elif q in name_l:
                score = 4  # airport name contains
            else:
                continue

            # Within same score, large airports first
            label = f"{city or name}  {iata}  ({country})"
            scored.append((score, 1 - is_large, label, iata))

        scored.sort(key=lambda x: (x[0], x[1]))
        return [(label, iata) for _, _, label, iata in scored[:12]]

    def _on_text(self, evt):
        self._matches = self._suggest(self.txt.GetValue())
        self.lb.Clear()
        for label, iata in self._matches:
            self.lb.Append(label, iata)
        if self._matches:
            self.lb.SetSelection(0)
        evt.Skip()

    def _on_key(self, evt):
        kc = evt.GetKeyCode()
        if kc == wx.WXK_DOWN and self.FindFocus() == self.txt:
            if self.lb.GetCount():
                self.lb.SetFocus()
                self.lb.SetSelection(0)
        elif kc in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            self._on_next(evt)
        elif kc == wx.WXK_ESCAPE:
            self.EndModal(wx.ID_CANCEL)
        else:
            evt.Skip()

    def _on_dclick(self, evt):
        self._on_next(evt)

    def _selected_iata(self):
        idx = self.lb.GetSelection()
        if idx != wx.NOT_FOUND:
            return self.lb.GetClientData(idx)
        if self._matches:
            return self._matches[0][1]
        return ""

    def _on_next(self, evt):
        iata = self._selected_iata()
        if not iata:
            q = self.txt.GetValue().strip().upper()
            if len(q) == 3:
                iata = q
        if not iata:
            wx.MessageBox("Please select an airport from the list.",
                          "No airport selected", wx.OK | wx.ICON_WARNING)
            return

        if not self.origin_iata:
            # First step — got origin, now ask for destination
            self.origin_iata = iata
            prompt = "To — type destination city or airport name"
            self.SetTitle(f"Flight search — {prompt}")
            self._prompt_lbl.SetLabel(f"{prompt}:")
            self.txt.Clear()
            self.lb.Clear()
            self._matches = []
            self.btn_next.SetLabel("Search")
            # Focus label first so NVDA reads it, then move to textctrl
            self._prompt_lbl.SetFocus()
            wx.CallLater(100, self.txt.SetFocus)
        else:
            # Second step — got destination
            self.dest_iata = iata
            self.EndModal(wx.ID_OK)


class FindFoodDialog(wx.Dialog):
    """Two-level Find Food results dialog.

    Level 1 — listbox of food places sorted by distance along route.
             Each item shows: name, address, distance along route, cross-street.
    Level 2 — HERE detail for the selected place: open/closed, phone,
             website, address.  Fetched on demand when Enter is pressed.
             Escape returns to the list.

    The dialog is created with a list of place dicts:
        {
            "name":          str,
            "lat":           float,
            "lon":           float,
            "kind":          str,          # e.g. "restaurant", "cafe"
            "address":       str,
            "along_m":       float,        # metres along route
            "cross_street":  str,          # nearest cross-street or ""
        }

    The ``detail_cb`` callable is called on a background thread when the
    user presses Enter on a list item.  It receives (name, lat, lon) and
    must return a dict with keys: address, phone, website, opening_hours.
    """

    def __init__(self, parent, places: list, detail_cb, title="Find Food"):
        super().__init__(parent, title=title,
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self._places    = places
        self._detail_cb = detail_cb
        self._showing_detail = False
        self._fetching  = False

        panel = wx.Panel(self)
        self._sizer = wx.BoxSizer(wx.VERTICAL)

        # Level 1 — summary list
        summaries = [self._fmt_summary(p) for p in places]
        self.listbox = wx.ListBox(panel, choices=summaries, style=wx.LB_SINGLE)
        if places:
            self.listbox.SetSelection(0)
        self._sizer.Add(self.listbox, 1, wx.ALL | wx.EXPAND, 10)

        # Level 2 — detail text (hidden initially)
        self.detail = wx.TextCtrl(
            panel, value="",
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP,
        )
        self._sizer.Add(self.detail, 1, wx.ALL | wx.EXPAND, 10)
        self.detail.Hide()

        close_btn = wx.Button(panel, wx.ID_CLOSE, "Close")
        self._sizer.Add(close_btn, 0, wx.LEFT | wx.BOTTOM, 10)

        panel.SetSizer(self._sizer)
        self.SetSize(640, 420)

        close_btn.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CLOSE))
        self.listbox.Bind(wx.EVT_LISTBOX_DCLICK, lambda e: self._show_detail())
        self.listbox.Bind(wx.EVT_KEY_DOWN, self._on_list_key)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)
        wx.CallAfter(self.listbox.SetFocus)

    # ------------------------------------------------------------------
    # Formatting helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _fmt_distance(metres: float) -> str:
        if metres < 950:
            return f"{int(round(metres / 50) * 50)} m"
        return f"{metres / 1000:.1f} km"

    @staticmethod
    def _fmt_summary(p: dict) -> str:
        name  = p.get("name", "Unknown")
        kind  = p.get("kind", "")
        addr  = p.get("address", "")
        along = p.get("along_m", 0.0)
        cross = p.get("cross_street", "")
        dist_str = FindFoodDialog._fmt_distance(along)
        parts = [f"{name}"]
        if kind:
            parts.append(f"({kind})")
        if addr:
            parts.append(addr)
        distance_label = p.get("distance_label", "along route")
        parts.append(f"— {dist_str} {distance_label}")
        if cross:
            parts.append(f"near {cross}")
        return "  ".join(parts)

    # ------------------------------------------------------------------
    # Level switching
    # ------------------------------------------------------------------

    def _show_detail(self):
        sel = self.listbox.GetSelection()
        if sel == wx.NOT_FOUND or sel >= len(self._places):
            return
        if self._fetching:
            return

        place = self._places[sel]
        name  = place.get("name", "")
        lat   = place.get("lat", 0.0)
        lon   = place.get("lon", 0.0)

        # Switch to detail panel immediately with a loading message
        self.detail.SetValue(f"Looking up {name}…")
        self.listbox.Hide()
        self.detail.Show()
        self._showing_detail = True
        self._fetching = True
        self.Layout()
        self.detail.SetFocus()
        self.detail.SetInsertionPoint(0)

        def _fetch():
            try:
                info = self._detail_cb(name, lat, lon)
            except Exception as exc:
                info = {"address": f"Error: {exc}",
                        "phone": "", "website": "", "opening_hours": ""}
            wx.CallAfter(self._populate_detail, name, info)

        import threading
        threading.Thread(target=_fetch, daemon=True).start()

    def _populate_detail(self, name: str, info: dict):
        self._fetching = False
        sel = self.listbox.GetSelection()
        if sel != wx.NOT_FOUND and sel < len(self._places):
            self._places[sel].update(info or {})
        address  = info.get("address", "")  or "Not available"
        phone    = info.get("phone", "")    or "Not available"
        website  = info.get("website", "")  or ""
        oh       = info.get("opening_hours", "") or "Hours not available"

        lines = [
            name,
            "",
            f"Status:   {oh}",
            f"Address:  {address}",
            f"Phone:    {phone}",
        ]
        if website:
            lines.append(f"Website:  {website}")
        lines += ["", "Press Escape to go back to the list."]

        self.detail.SetValue("\n".join(lines))
        self.detail.SetInsertionPoint(0)

    def _show_list(self):
        self.detail.Hide()
        self.listbox.Show()
        self._showing_detail = False
        self.Layout()
        self.listbox.SetFocus()

    # ------------------------------------------------------------------
    # Input
    # ------------------------------------------------------------------

    def _announce(self, msg: str):
        parent = self.GetParent()
        if parent and hasattr(parent, "_status_update"):
            parent._status_update(msg, force=True)

    def _selected_place(self):
        sel = self.listbox.GetSelection()
        if sel == wx.NOT_FOUND or sel >= len(self._places):
            return None
        return self._places[sel]

    def _open_selected_website(self):
        place = self._selected_place()
        if not place or self._fetching:
            return
        name = place.get("name", "food place")
        url = (place.get("website") or "").strip()
        if url:
            self._open_food_url(url)
            return

        self._fetching = True
        self._announce(f"Looking up website for {name}...")

        def _fetch():
            try:
                info = self._detail_cb(
                    name,
                    place.get("lat", 0.0),
                    place.get("lon", 0.0),
                )
            except Exception as exc:
                info = {"website": "", "_error": str(exc)}
            wx.CallAfter(self._open_website_from_detail, place, info)

        import threading
        threading.Thread(target=_fetch, daemon=True).start()

    def _open_website_from_detail(self, place: dict, info: dict):
        self._fetching = False
        place.update(info or {})
        url = (place.get("website") or "").strip()
        if url:
            self._open_food_url(url)
            return

        import urllib.parse
        name = place.get("name", "food place")
        address = place.get("address", "")
        query = " ".join(p for p in (name, address) if p).strip()
        search_url = f"https://www.google.com/search?q={urllib.parse.quote(query)}"
        self._open_food_url(search_url, found=False, label=query)

    def _open_food_url(self, url: str, found: bool = True, label: str = ""):
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        try:
            import webbrowser
            webbrowser.open(url)
            if found:
                self._announce(f"Opening {url}")
            else:
                self._announce(f"No website found — opening Google search for {label}")
        except Exception as exc:
            self._announce(f"Could not open website: {exc}")

    def _on_list_key(self, event):
        code = event.GetKeyCode()
        if _primary_down(event) and code in (ord("W"), ord("w")):
            self._open_selected_website()
            return
        if code in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            self._show_detail()
            return
        event.Skip()

    def _on_char_hook(self, event):
        code = event.GetKeyCode()
        if _primary_down(event) and code in (ord("W"), ord("w")):
            self._open_selected_website()
            return
        if self._showing_detail:
            if code in (wx.WXK_ESCAPE, wx.WXK_BACK):
                self._show_list()
                return
        else:
            if code in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
                self._show_detail()
                return
            if code == wx.WXK_ESCAPE:
                self.EndModal(wx.ID_CLOSE)
                return
        event.Skip()


class HotelResultsDialog(wx.Dialog):
    def __init__(self, parent, hotels):
        super().__init__(parent, title="Hotels",
                         size=(500, 500),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)

        self.hotels = hotels
        self.selected_index = None

        panel = wx.Panel(self)
        vs = wx.BoxSizer(wx.VERTICAL)

        self.listbox = wx.ListBox(panel, style=wx.LB_SINGLE)
        vs.Add(self.listbox, 1, wx.ALL | wx.EXPAND, 10)

        close_btn = wx.Button(panel, wx.ID_CANCEL, "Close")
        vs.Add(close_btn, 0, wx.ALL | wx.ALIGN_RIGHT, 10)

        panel.SetSizer(vs)

        # Populate list
        items = []
        for h in hotels:
            name = h.get("name", "")
            address = h.get("address", "")
            items.append(f"{name} - {address}")

        self.listbox.Set(items)
        if items:
            self.listbox.SetSelection(0)

        self.listbox.Bind(wx.EVT_LISTBOX_DCLICK, self._on_enter)

        self.Bind(wx.EVT_CHAR_HOOK, self._on_char)

        wx.CallAfter(self.listbox.SetFocus)

    def _on_enter(self, event=None):
        sel = self.listbox.GetSelection()
        if sel != wx.NOT_FOUND:
            self.selected_index = sel
            self.EndModal(wx.ID_OK)

    def _on_key(self, event):
        if event.GetKeyCode() in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            self._on_enter()
            return
        event.Skip()

    def _on_char(self, event):
        code = event.GetKeyCode()
        if code == wx.WXK_ESCAPE:
            self.EndModal(wx.ID_CANCEL)
            return
        if code in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            self._on_enter()
            return
        event.Skip()
