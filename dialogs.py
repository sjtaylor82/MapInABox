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
import re
import wx
import wx.adv

from i18n import _
from wx_utils import _log_key_event, _primary_down
from logging_utils import miab_log


class _ExplicitNameAccessible(getattr(wx, "Accessible", object)):
    """Expose a reliable MSAA name when wx control names are ignored by NVDA."""

    def __init__(self, name):
        super().__init__()
        self._name = name

    def GetName(self, childId):
        return wx.ACC_OK, self._name


def _set_explicit_accessible_name(owner, control, name):
    control.SetName(name)
    # wx.Accessible is implemented on Windows.  SetName remains the portable
    # fallback on macOS and on wx builds without MSAA support.
    try:
        accessible = _ExplicitNameAccessible(name)
        control.SetAccessible(accessible)
        owner._named_accessibles = getattr(owner, "_named_accessibles", [])
        owner._named_accessibles.append(accessible)
    except Exception:
        pass


def _return_parent_focus(dialog) -> None:
    parent = dialog.GetParent()
    suppress = getattr(parent, "_suppress_map_focus_repeat", None)
    if callable(suppress):
        suppress(800)
    focus_map = getattr(parent, "_focus_map_window_silently", None)
    if callable(focus_map):
        wx.CallAfter(focus_map)
    elif parent is not None:
        wx.CallAfter(parent.SetFocus)


def _hook_escape_enter(dialog, event, on_enter=None, escape_id=wx.ID_CANCEL) -> bool:
    """Handle Escape / Enter for small modal dialogs."""
    code = event.GetKeyCode()
    if code == wx.WXK_ESCAPE:
        _return_parent_focus(dialog)
        dialog.EndModal(escape_id)
        return True
    if code in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
        if on_enter is None:
            dialog.EndModal(wx.ID_OK)
        else:
            on_enter()
        return True
    return False


def _hook_detail_list(dialog, event, showing_detail: bool, show_list, show_detail,
                      escape_id=wx.ID_CLOSE, on_primary=None) -> bool:
    """Handle list/detail dialogs that switch between a summary list and details."""
    code = event.GetKeyCode()
    if on_primary is not None and on_primary(event):
        return True
    if showing_detail:
        if code in (wx.WXK_ESCAPE, wx.WXK_BACK):
            show_list()
            return True
    else:
        if code in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            show_detail()
            return True
        if code == wx.WXK_ESCAPE:
            _return_parent_focus(dialog)
            dialog.EndModal(escape_id)
            return True
    return False


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

    btn = wx.Button(dlg, wx.ID_OK, _("OK"))
    btn.SetDefault()
    vs.Add(btn, 0, wx.ALIGN_RIGHT | wx.RIGHT | wx.BOTTOM, 10)

    dlg.SetSizerAndFit(vs)
    dlg.CentreOnParent()
    dlg.ShowModal()
    dlg.Destroy()


def show_optional_key_warning(parent, title: str, message: str) -> bool:
    """Modal dialog explaining a missing optional key and its limitations.

    Returns True if the user checked "Don't show this again".
    """
    dlg = wx.Dialog(parent, title=title,
                    style=wx.DEFAULT_DIALOG_STYLE | wx.STAY_ON_TOP)
    vs = wx.BoxSizer(wx.VERTICAL)

    txt = wx.StaticText(dlg, label=message)
    txt.Wrap(440)
    vs.Add(txt, 0, wx.ALL, 14)

    # Translators: Checkbox label in optional API-key warning dialogs.
    cb = wx.CheckBox(dlg, label=_("Don't show this warning again"))
    vs.Add(cb, 0, wx.LEFT | wx.BOTTOM, 14)

    btn = wx.Button(dlg, wx.ID_OK, _("OK"))
    btn.SetDefault()
    vs.Add(btn, 0, wx.ALIGN_RIGHT | wx.RIGHT | wx.BOTTOM, 10)

    dlg.SetSizerAndFit(vs)
    dlg.CentreOnParent()
    dlg.ShowModal()
    suppress = cb.GetValue()
    dlg.Destroy()
    return suppress


def show_open_source_notice(parent) -> None:
    """Tell users the app prefers free/open services and accepts optional keys."""
    dlg = wx.Dialog(
        parent,
        title=_("Open Sources and Optional Keys"),
        style=wx.DEFAULT_DIALOG_STYLE | wx.STAY_ON_TOP,
    )
    vs = wx.BoxSizer(wx.VERTICAL)

    message = (
        _("Every effort has been made to keep Map in a Box usable with open "
          "data and free endpoints. The app will fall back to those services "
          "where it can.\n\n"
          "If you want richer coverage or higher limits, you can still add "
          "your own API keys in Settings.")
    )
    txt = wx.StaticText(dlg, label=message)
    txt.Wrap(430)
    vs.Add(txt, 0, wx.ALL, 14)

    btn = wx.Button(dlg, wx.ID_OK, _("OK"))
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
            parent, title=_("Settings"), size=(640, 680),
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

        self.notebook.AddPage(self.general_page, _("General"))
        self.notebook.AddPage(self.api_page, _("API Keys"))
        self.notebook.AddPage(self.logging_page, _("Logging"))
        vs.Add(self.notebook, 1, wx.EXPAND | wx.ALL, 8)

        general_vs = wx.BoxSizer(wx.VERTICAL)
        api_vs = wx.BoxSizer(wx.VERTICAL)
        log_vs = wx.BoxSizer(wx.VERTICAL)

        general_vs.Add(wx.StaticText(self.general_page, label=_("Walking mode POI announcements:")), 0, wx.ALL, 8)
        self.cb_walk = wx.CheckBox(self.general_page, label=_("Announce nearby POIs while walking"))
        general_vs.Add(self.cb_walk, 0, wx.LEFT | wx.BOTTOM, 12)

        general_vs.Add(wx.StaticText(self.general_page, label=_("POIs to announce while walking:")), 0, wx.LEFT, 8)
        self.combo_cat = wx.Choice(
            self.general_page,
            choices=[_(label) for _key, label in POI_CATEGORY_CHOICES],
        )
        general_vs.Add(self.combo_cat, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)

        general_vs.Add(wx.StaticText(self.general_page, label=_("Announce POIs within:")), 0, wx.LEFT, 8)
        self.combo_radius = wx.Choice(
            self.general_page,
            choices=[_("50 metres"), _("80 metres"), _("120 metres")],
        )
        general_vs.Add(self.combo_radius, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)

        self.cb_walk_cat = wx.CheckBox(self.general_page, label=_("Include category label in announcement"))
        general_vs.Add(self.cb_walk_cat, 0, wx.LEFT | wx.BOTTOM, 8)

        general_vs.Add(wx.StaticLine(self.general_page), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        general_vs.Add(wx.StaticText(self.general_page, label=_("Map announcements:")), 0, wx.LEFT, 8)
        self.cb_climate_zones = wx.CheckBox(self.general_page, label=_("Announce climate zones during navigation"))
        general_vs.Add(self.cb_climate_zones, 0, wx.LEFT | wx.BOTTOM, 8)
        self.cb_suburb_size = wx.CheckBox(self.general_page, label=_("Announce suburb size when streets load"))
        general_vs.Add(self.cb_suburb_size, 0, wx.LEFT | wx.BOTTOM, 8)

        general_vs.Add(wx.StaticLine(self.general_page), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        general_vs.Add(wx.StaticText(self.general_page, label=_("Spatial tones:")), 0, wx.LEFT, 8)
        self.combo_spatial_tones = wx.Choice(
            self.general_page,
            choices=[_("World"), _("Country"), _("Region")],
        )
        general_vs.Add(self.combo_spatial_tones, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)

        general_vs.Add(wx.StaticLine(self.general_page), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        general_vs.Add(wx.StaticText(self.general_page, label=_("Challenge direction:")), 0, wx.LEFT, 8)
        self.combo_challenge_direction = wx.Choice(
            self.general_page,
            choices=[_("Map learning"), _("Shortest globe")],
        )
        general_vs.Add(self.combo_challenge_direction, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)

        general_vs.Add(wx.StaticLine(self.general_page), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        general_vs.Add(wx.StaticText(self.general_page, label=_("Weather temperature units:")), 0, wx.LEFT, 8)
        self.combo_weather_units = wx.Choice(
            self.general_page,
            choices=[_("Automatic (country-based)"), _("Celsius"), _("Fahrenheit")],
        )
        general_vs.Add(self.combo_weather_units, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)

        general_vs.Add(wx.StaticLine(self.general_page), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        general_vs.Add(wx.StaticText(self.general_page, label=_("POI database (for street/free mode):")), 0, wx.LEFT, 8)
        self.combo_poi_source = wx.Choice(
            self.general_page,
            choices=[_("OpenStreetMap"), _("HERE")],
        )
        general_vs.Add(self.combo_poi_source, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)

        general_vs.Add(wx.StaticLine(self.general_page), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        general_vs.Add(wx.StaticText(self.general_page, label=_("Navigation provider (walking routes):")), 0, wx.LEFT, 8)
        self.combo_nav = wx.Choice(
            self.general_page,
            choices=[_("OpenStreetMap"), _("Google Maps"), _("HERE")],
        )
        general_vs.Add(self.combo_nav, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)

        general_vs.Add(wx.StaticLine(self.general_page), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        general_vs.Add(wx.StaticText(self.general_page, label=_("Departure board source:")), 0, wx.LEFT, 8)
        self.combo_departure_board = wx.Choice(
            self.general_page,
            choices=[_("GTFS data"), _("Google Places")],
        )
        general_vs.Add(self.combo_departure_board, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)

        self.btn_gtfs = wx.Button(self.general_page, label=_("Refresh Transit Feed Catalog"))
        general_vs.Add(self.btn_gtfs, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        self.gtfs_refreshed = False
        self.btn_gtfs.Bind(wx.EVT_BUTTON, self._on_gtfs_refresh)

        general_vs.Add(wx.StaticLine(self.general_page), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        self.cb_auto_updates = wx.CheckBox(
            self.general_page, label=_("Automatically check for updates at startup"))
        general_vs.Add(self.cb_auto_updates, 0, wx.LEFT | wx.BOTTOM, 8)

        self.general_page.SetSizer(general_vs)

        api_vs.Add(wx.StaticText(self.api_page, label=_("Google API key - enhanced geocoding/routing, satellite/street view, Google navigation:")), 0, wx.ALL, 8)
        self.txt_google_key = wx.TextCtrl(self.api_page, style=wx.TE_PASSWORD)
        api_vs.Add(self.txt_google_key, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)
        api_vs.Add(wx.adv.HyperlinkCtrl(self.api_page, label=_("Get a Google API key"),
            url="https://developers.google.com/maps/get-started"), 0, wx.LEFT | wx.BOTTOM, 8)

        api_vs.Add(wx.StaticLine(self.api_page), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        api_vs.Add(wx.StaticText(self.api_page, label=_("Mistral API key - optional descriptions for satellite/street view and transit:")), 0, wx.LEFT, 8)
        self.txt_mistral_key = wx.TextCtrl(self.api_page, style=wx.TE_PASSWORD)
        api_vs.Add(self.txt_mistral_key, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)
        api_vs.Add(wx.adv.HyperlinkCtrl(self.api_page, label=_("Get a Mistral API key (free mode, no credit card)"),
            url="https://console.mistral.ai/"), 0, wx.LEFT | wx.BOTTOM, 8)

        api_vs.Add(wx.StaticLine(self.api_page), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        api_vs.Add(wx.StaticText(self.api_page, label=_("HERE API key - optional POI details, HERE navigation, and departure board:")), 0, wx.LEFT, 8)
        self.txt_here_key = wx.TextCtrl(self.api_page, style=wx.TE_PASSWORD)
        api_vs.Add(self.txt_here_key, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)
        api_vs.Add(wx.adv.HyperlinkCtrl(self.api_page, label=_("Get a HERE API key"),
            url="https://developer.here.com/sign-up"), 0, wx.LEFT | wx.BOTTOM, 8)

        api_vs.Add(wx.StaticLine(self.api_page), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        api_vs.Add(wx.StaticText(self.api_page, label=_("OpenRouteService API key - optional walking/driving distance between marks:")), 0, wx.LEFT, 8)
        self.txt_ors_key = wx.TextCtrl(self.api_page, style=wx.TE_PASSWORD)
        api_vs.Add(self.txt_ors_key, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)
        api_vs.Add(wx.adv.HyperlinkCtrl(self.api_page, label=_("Get an OpenRouteService API key"),
            url="https://openrouteservice.org/sign-up/"), 0, wx.LEFT | wx.BOTTOM, 8)

        api_vs.Add(wx.StaticLine(self.api_page), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        api_vs.Add(wx.StaticText(self.api_page, label=_("AviationStack API key - optional airport departure/arrival boards:")), 0, wx.LEFT, 8)
        self.txt_aviationstack_key = wx.TextCtrl(self.api_page, style=wx.TE_PASSWORD)
        api_vs.Add(self.txt_aviationstack_key, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)
        api_vs.Add(wx.adv.HyperlinkCtrl(self.api_page, label=_("Get an AviationStack API key"),
            url="https://aviationstack.com/signup/free"), 0, wx.LEFT | wx.BOTTOM, 8)

        api_vs.Add(wx.StaticLine(self.api_page), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        api_vs.Add(wx.StaticText(self.api_page, label=_("OpenSky client ID - optional overhead flight destination lookup (free):")), 0, wx.LEFT, 8)
        self.txt_opensky_id = wx.TextCtrl(self.api_page)
        api_vs.Add(self.txt_opensky_id, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)
        api_vs.Add(wx.StaticText(self.api_page, label=_("OpenSky client secret:")), 0, wx.LEFT, 8)
        self.txt_opensky_secret = wx.TextCtrl(self.api_page, style=wx.TE_PASSWORD)
        api_vs.Add(self.txt_opensky_secret, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)
        api_vs.Add(wx.adv.HyperlinkCtrl(self.api_page, label=_("Register a free OpenSky account"),
            url="https://opensky-network.org/index.php?option=com_users&view=registration"),
            0, wx.LEFT | wx.BOTTOM, 8)

        api_vs.Add(wx.StaticLine(self.api_page), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        api_vs.Add(wx.StaticText(self.api_page, label=_("RapidAPI key - optional flight search and hotel search (F12 tools):")), 0, wx.LEFT, 8)
        self.txt_rapidapi_key = wx.TextCtrl(self.api_page, style=wx.TE_PASSWORD)
        api_vs.Add(self.txt_rapidapi_key, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)
        hs_rapid = wx.BoxSizer(wx.HORIZONTAL)
        hs_rapid.Add(wx.adv.HyperlinkCtrl(self.api_page, label=_("Sign up for RapidAPI"),
            url="https://rapidapi.com/auth/sign-up"), 0, wx.RIGHT, 16)
        hs_rapid.Add(wx.adv.HyperlinkCtrl(self.api_page, label=_("Subscribe: Priceline API"),
            url="https://rapidapi.com/tipsters/api/priceline-com-provider"), 0, wx.RIGHT, 16)
        hs_rapid.Add(wx.adv.HyperlinkCtrl(self.api_page, label=_("Subscribe: Timetable Lookup API"),
            url="https://rapidapi.com/obryan.sw/api/timetable-lookup"), 0)
        api_vs.Add(hs_rapid, 0, wx.LEFT | wx.BOTTOM, 8)

        self.api_page.SetSizer(api_vs)

        log = settings.get("logging", {})
        self.cb_log_errors    = wx.CheckBox(self.logging_page, label=_("Errors - exceptions, API failures, missing data"))
        self.cb_log_street    = wx.CheckBox(self.logging_page, label=_("Street/POI data - Overpass queries, cache hits/misses"))
        self.cb_log_snap      = wx.CheckBox(self.logging_page, label=_("Street snap - jump/search snap decisions and arrow key movement"))
        self.cb_log_api       = wx.CheckBox(self.logging_page, label=_("HERE/Mistral API calls - requests and responses"))
        self.cb_log_challenge = wx.CheckBox(self.logging_page, label=_("Challenge sessions - player, country, time, score"))
        self.cb_log_features  = wx.CheckBox(self.logging_page, label=_("Feature usage - keys pressed, lookups made"))
        self.cb_log_nav       = wx.CheckBox(self.logging_page, label=_("Navigation events - country entries, crossings, jumps"))
        self.cb_log_verbose   = wx.CheckBox(self.logging_page, label=_("Verbose diagnostics - key sequences and extra traces written to miab.log"))
        log_vs.Add(wx.StaticText(self.logging_page, label=_("Logging (miab.log):")), 0, wx.ALL, 8)
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
        ok_btn     = wx.Button(panel, wx.ID_OK,     _("Save"))
        cancel_btn = wx.Button(panel, wx.ID_CANCEL, _("Cancel"))
        btn_home   = wx.Button(panel, label=_("Set Home Location"))
        btn_folder = wx.Button(panel, label=_("Open Settings Folder"))
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
        self.cb_suburb_size.SetValue(settings.get("announce_suburb_size", False))
        self.cb_auto_updates.SetValue(settings.get("check_updates_at_startup", True))
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
        self.txt_google_key.SetValue(settings.get("google_api_key", ""))
        self.txt_mistral_key.SetValue(settings.get("mistral_api_key", ""))
        self.txt_here_key.SetValue(settings.get("here_api_key", ""))
        self.txt_ors_key.SetValue(settings.get("ors_api_key", ""))
        self.txt_aviationstack_key.SetValue(settings.get("aviationstack_api_key", ""))
        self.txt_rapidapi_key.SetValue(settings.get("rapidapi_key", ""))
        self.txt_opensky_id.SetValue(settings.get("opensky_client_id", ""))
        self.txt_opensky_secret.SetValue(settings.get("opensky_client_secret", ""))

        ok_btn.Bind(wx.EVT_BUTTON, self._on_ok)
        self.Bind(
            wx.EVT_CHAR_HOOK,
            lambda e: (_return_parent_focus(self), self.EndModal(wx.ID_CANCEL))
                       if e.GetKeyCode() == wx.WXK_ESCAPE else e.Skip()
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
            "announce_suburb_size":   self.cb_suburb_size.GetValue(),
            "check_updates_at_startup": self.cb_auto_updates.GetValue(),
            "spatial_tones_mode":     spatial_mode,
            "challenge_direction_mode": challenge_direction,
            "weather_temperature_unit": weather_units,
            "nav_provider":           nav_provider,
            "departure_board_source": departure_board_source,
            "poi_source":             "here" if self.combo_poi_source.GetSelection() == 1 else "osm",
            "google_api_key":         self.txt_google_key.GetValue().strip(),
            "mistral_api_key":         self.txt_mistral_key.GetValue().strip(),
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
        initial_street: str = "",
        initial_radius: int = 1000,
        notice: str = "",
    ) -> None:
        super().__init__(
            parent, title="POI Search", size=(430, 300),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self.selected_key    = None
        self.selected_name   = ""
        self.selected_street = ""
        self.selected_source = "osm"
        self.selected_radius = 1000
        sources = available_sources or ["osm"]
        panel = wx.Panel(self)
        vs = wx.BoxSizer(wx.VERTICAL)

        if notice:
            msg = wx.StaticText(panel, label=notice)
            msg.Wrap(390)
            vs.Add(msg, 0, wx.ALL | wx.EXPAND, 10)

        info = wx.StaticText(panel, label="Search by name or street (optional), then choose category and source.")
        info.Wrap(390)
        vs.Add(info, 0, wx.ALL | wx.EXPAND, 10)

        vs.Add(wx.StaticText(panel, label="Name:"), 0, wx.LEFT, 10)
        self.txt_name = wx.TextCtrl(panel, style=wx.TE_PROCESS_ENTER)
        if initial_name:
            self.txt_name.SetValue(initial_name)
            self.txt_name.SetSelection(-1, -1)
        vs.Add(self.txt_name, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        vs.Add(wx.StaticText(panel, label="Street:"), 0, wx.LEFT, 10)
        self.txt_street = wx.TextCtrl(panel, style=wx.TE_PROCESS_ENTER)
        if initial_street:
            self.txt_street.SetValue(initial_street)
            self.txt_street.SetSelection(-1, -1)
        vs.Add(self.txt_street, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        vs.Add(wx.StaticText(panel, label="Category:"), 0, wx.LEFT, 10)
        self.combo = wx.Choice(
            panel,
            choices=[label for _, label in POI_CATEGORY_CHOICES],
        )
        keys = [key for key, _ in POI_CATEGORY_CHOICES]
        initial_idx = keys.index(initial_key) if initial_key in keys else 0
        self.combo.SetSelection(initial_idx)
        vs.Add(self.combo, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        vs.Add(wx.StaticText(panel, label="Radius:"), 0, wx.LEFT, 10)
        self._radius_values = [1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000, 9000, 10000]
        self.combo_radius = wx.Choice(
            panel,
            choices=["1 km", "2 km", "3 km", "4 km", "5 km", "6 km", "7 km", "8 km", "9 km", "10 km"],
        )
        try:
            radius_idx = self._radius_values.index(int(initial_radius))
        except Exception:
            radius_idx = 0
        self.combo_radius.SetSelection(radius_idx)
        vs.Add(self.combo_radius, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        vs.Add(wx.StaticText(panel, label="Source:"), 0, wx.LEFT, 10)
        source_labels = {"osm": "OpenStreetMap", "here": "HERE", "google": "Google Maps"}
        self._source_keys = sources
        self.combo_source = wx.Choice(
            panel,
            choices=[source_labels.get(s, s) for s in sources],
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
        self.combo.Bind(wx.EVT_CHOICE, self._on_choice)
        self.txt_name.Bind(wx.EVT_TEXT_ENTER, self._on_go)
        self.txt_street.Bind(wx.EVT_TEXT_ENTER, self._on_go)
        self._tab_order = [
            self.txt_name,
            self.txt_street,
            self.combo,
            self.combo_radius,
            self.combo_source,
            go_btn,
            cancel_btn,
        ]
        for prev, cur in zip(self._tab_order, self._tab_order[1:]):
            try:
                cur.MoveAfterInTabOrder(prev)
            except Exception:
                pass
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)
        self.txt_name.SetFocus()
        self.CentreOnParent()

    def _on_char_hook(self, event) -> None:
        if _hook_escape_enter(self, event, on_enter=lambda: self._on_go(event)):
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
        self.selected_street = self.txt_street.GetValue().strip()
        src_idx = self.combo_source.GetSelection()
        self.selected_source = (self._source_keys[src_idx]
                                if 0 <= src_idx < len(self._source_keys)
                                else "osm")
        rad_idx = self.combo_radius.GetSelection()
        self.selected_radius = (self._radius_values[rad_idx]
                                if 0 <= rad_idx < len(self._radius_values)
                                else 1000)
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
        def _on_enter():
            obj = self.FindFocus()
            if obj in (self.search, self.listbox):
                self._choose_current()
        if _hook_escape_enter(self, event, on_enter=_on_enter):
            return
        event.Skip()


# ---------------------------------------------------------------------------
# Route Tools dialogs
# ---------------------------------------------------------------------------

class ToolsMenuDialog(wx.Dialog):
    """F12 Tools menu — pick an action from a short list."""

    TOOLS = [
        ("Detour Calculator",  "detour_calculator"),
        ("Suburb Lister",      "route_explorer"),
        ("Rendezvous Point",   "rendezvous_point"),
        ("Toll Compare",       "toll_compare"),
        ("Journey Planner",    "journey_planner"),
        ("Airport Amenity Guide", "airport_amenity_guide"),
        ("Departure Board",    "departure_board"),
        ("Flight Search",      "flight_search"),
        ("Virgin Australia Booking", "virgin_booking"),
        ("Hotel Search",       "hotel_search"),
        ("Find Food",          "find_food"),
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
        self.SetSize(360, 320)

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
        if _hook_escape_enter(self, event, on_enter=self._on_choose):
            return
        event.Skip()


class StopEntryDialog(wx.Dialog):
    """Prompt the user for an address/suburb name.  Returns the text."""

    def __init__(self, parent, prompt, default="", title="Start"):
        super().__init__(parent, title=title,
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
        if _hook_escape_enter(self, event):
            return
        event.Skip()


class MeetPointDialog(wx.Dialog):
    """Prompt for rendezvous inputs and match mode."""

    def __init__(self, parent) -> None:
        super().__init__(parent, title="Rendezvous Point",
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        panel = wx.Panel(self)
        vs = wx.BoxSizer(wx.VERTICAL)

        self.mode = wx.RadioBox(
            panel,
            choices=[
                "Find a pick-up point",
                "Get dropped off on the way",
                "Meet in the middle",
            ],
            majorDimension=1,
            style=wx.RA_SPECIFY_ROWS,
        )
        self.mode.SetSelection(0)
        vs.Add(self.mode, 0, wx.ALL | wx.EXPAND, 8)

        self.origin_label = wx.StaticText(panel, label="Your address:")
        vs.Add(self.origin_label, 0, wx.LEFT | wx.TOP, 8)
        self.origin = wx.TextCtrl(panel)
        vs.Add(self.origin, 0, wx.ALL | wx.EXPAND, 8)

        self.dest_a_label = wx.StaticText(panel, label="Friend's address:")
        vs.Add(self.dest_a_label, 0, wx.LEFT | wx.TOP, 8)
        self.dest_a = wx.TextCtrl(panel)
        vs.Add(self.dest_a, 0, wx.ALL | wx.EXPAND, 8)

        self.dest_b_label = wx.StaticText(panel, label="Shared destination:")
        vs.Add(self.dest_b_label, 0, wx.LEFT | wx.TOP, 8)
        self.dest_b = wx.TextCtrl(panel)
        vs.Add(self.dest_b, 0, wx.ALL | wx.EXPAND, 8)

        hs = wx.BoxSizer(wx.HORIZONTAL)
        ok_btn = wx.Button(panel, wx.ID_OK, "OK")
        cancel_btn = wx.Button(panel, wx.ID_CANCEL, "Cancel")
        hs.Add(ok_btn, 0, wx.RIGHT, 8)
        hs.Add(cancel_btn, 0)
        vs.Add(hs, 0, wx.LEFT | wx.BOTTOM, 8)

        panel.SetSizer(vs)
        self.SetSize(420, 400)

        ok_btn.Bind(wx.EVT_BUTTON, self._on_submit)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)
        self.mode.Bind(wx.EVT_RADIOBOX, self._on_mode_changed)
        self._apply_mode_labels()
        wx.CallAfter(self.mode.SetFocus)

    def GetValues(self):
        return (
            self.origin.GetValue().strip(),
            self.dest_a.GetValue().strip(),
            self.dest_b.GetValue().strip(),
            ["pickup", "dropoff", "meeting"][self.mode.GetSelection()],
        )

    def _apply_mode_labels(self):
        sel = self.mode.GetSelection()
        if sel == 0:  # pickup
            self.origin_label.SetLabel("Your address:")
            self.dest_a_label.SetLabel("Friend's address:")
            self.dest_b_label.SetLabel("Shared destination:")
            self.dest_b_label.Show(True)
            self.dest_b.Show(True)
        elif sel == 1:  # dropoff
            self.origin_label.SetLabel("Shared starting point:")
            self.dest_a_label.SetLabel("Your destination:")
            self.dest_b_label.SetLabel("Friend's destination:")
            self.dest_b_label.Show(True)
            self.dest_b.Show(True)
        else:  # meeting
            self.origin_label.SetLabel("Friend's suburb/address:")
            self.dest_a_label.SetLabel("Your suburb/address:")
            self.dest_b_label.Show(False)
            self.dest_b.Show(False)
        self.Layout()
        self.Fit()

    def _on_mode_changed(self, event):
        self._apply_mode_labels()
        event.Skip()

    def _on_submit(self, event=None):
        del event
        origin, dest_a, dest_b, mode = self.GetValues()
        if not origin:
            wx.MessageBox("Please enter the first address.", "Rendezvous Point", parent=self)
            self.origin.SetFocus()
            return
        if not dest_a:
            wx.MessageBox("Please enter the second address.", "Rendezvous Point", parent=self)
            self.dest_a.SetFocus()
            return
        if mode in ("pickup", "dropoff") and not dest_b:
            label = "shared destination" if mode == "pickup" else "friend's destination"
            wx.MessageBox(f"Please enter the {label}.", "Rendezvous Point", parent=self)
            self.dest_b.SetFocus()
            return
        self.EndModal(wx.ID_OK)

    def _on_char_hook(self, event):
        code = event.GetKeyCode()
        focus = wx.Window.FindFocus()
        focus_in_mode = focus is self.mode
        if focus is not None and not focus_in_mode:
            try:
                focus_in_mode = self.mode.IsDescendant(focus)
            except Exception:
                focus_in_mode = False
        if focus_in_mode and code in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            self._apply_mode_labels()
            self.origin.SetFocus()
            return
        if _hook_escape_enter(self, event, on_enter=self._on_submit):
            return
        event.Skip()


class ChoiceDialog(wx.Dialog):
    """Pick from a small set of options using radio buttons."""

    def __init__(self, parent, prompt: str, title: str, choices: list[str],
                 default_selection: int = 0) -> None:
        super().__init__(
            parent,
            title=title,
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        panel = wx.Panel(self)
        vs = wx.BoxSizer(wx.VERTICAL)

        label = wx.StaticText(panel, label=prompt)
        label.Wrap(520)
        vs.Add(label, 0, wx.ALL | wx.EXPAND, 10)

        self.choice_box = wx.RadioBox(
            panel,
            choices=choices,
            majorDimension=1,
            style=wx.RA_SPECIFY_ROWS,
        )
        if 0 <= default_selection < len(choices):
            self.choice_box.SetSelection(default_selection)
        vs.Add(self.choice_box, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        hs = wx.BoxSizer(wx.HORIZONTAL)
        ok_btn = wx.Button(panel, wx.ID_OK, "OK")
        cancel_btn = wx.Button(panel, wx.ID_CANCEL, "Cancel")
        hs.Add(ok_btn, 0, wx.RIGHT, 8)
        hs.Add(cancel_btn, 0)
        vs.Add(hs, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        panel.SetSizer(vs)

        ok_btn.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_OK))
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)
        wx.CallAfter(self.choice_box.SetFocus)
        self.Fit()
        width, height = self.GetSize()
        if width < 560:
            self.SetSize((560, height))
        self.SetMinSize(self.GetSize())

    def GetSelection(self) -> int:
        return self.choice_box.GetSelection()

    def _on_char_hook(self, event):
        if _hook_escape_enter(self, event):
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
        if _hook_escape_enter(self, event, escape_id=wx.ID_CLOSE):
            return
        event.Skip()


class RendezvousResultsDialog(wx.Dialog):
    """Browsable list of rendezvous candidates ranked from best to worst."""

    def __init__(self, parent, title, intro, candidates):
        super().__init__(parent, title=title,
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self._candidates = candidates

        panel = wx.Panel(self)
        vs = wx.BoxSizer(wx.VERTICAL)

        vs.Add(wx.StaticText(panel, label=intro), 0, wx.ALL | wx.EXPAND, 10)

        self.listbox = wx.ListBox(
            panel,
            choices=[c["summary"] for c in candidates],
            style=wx.LB_SINGLE,
        )
        if candidates:
            self.listbox.SetSelection(0)
        vs.Add(self.listbox, 1, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        self.detail = wx.TextCtrl(
            panel,
            value="",
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP,
        )
        vs.Add(self.detail, 1, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        close_btn = wx.Button(panel, wx.ID_CLOSE, "Close")
        vs.Add(close_btn, 0, wx.LEFT | wx.BOTTOM, 10)

        panel.SetSizer(vs)
        self.SetSize(700, 480)

        close_btn.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CLOSE))
        self.listbox.Bind(wx.EVT_LISTBOX, self._on_select)
        self.listbox.Bind(wx.EVT_LISTBOX_DCLICK, self._on_choose)
        self.listbox.Bind(wx.EVT_KEY_DOWN, self._on_key)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)
        wx.CallAfter(self.listbox.SetFocus)
        if candidates:
            self._set_detail(0)

    def _set_detail(self, sel: int):
        if sel == wx.NOT_FOUND or sel >= len(self._candidates):
            return
        self.detail.SetValue(self._candidates[sel]["detail_text"])
        self.detail.SetInsertionPoint(0)

    def _on_select(self, event):
        self._set_detail(self.listbox.GetSelection())
        event.Skip()

    def _on_choose(self, event=None):
        self.EndModal(wx.ID_OK)

    def _on_key(self, event):
        event.Skip()

    def _on_char_hook(self, event):
        if _hook_escape_enter(self, event, on_enter=self._on_choose, escape_id=wx.ID_CLOSE):
            return
        event.Skip()


# ---------------------------------------------------------------------------
# Journey Planner dialogs
# ---------------------------------------------------------------------------

class VirginAustraliaBookingDialog(wx.Dialog):
    """Accessible collection of Virgin Australia's flight-search fields."""

    def __init__(self, parent, airline_name="Virgin Australia", airport_choices=None):
        self.airline_name = airline_name
        super().__init__(parent, title=f"{airline_name} Booking",
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        # Prevent MSAA/NVDA from deriving the dialog name by concatenating all
        # of its static field labels.
        _set_explicit_accessible_name(self, self, f"{airline_name} Booking")
        import datetime as _dt

        panel = wx.Panel(self)
        outer = wx.BoxSizer(wx.VERTICAL)

        grid = wx.FlexGridSizer(cols=2, vgap=8, hgap=10)
        grid.AddGrowableCol(1, 1)

        self.trip_type = wx.Choice(panel, choices=["One way", "Return"])
        _set_explicit_accessible_name(self, self.trip_type, "Trip type")
        self.trip_type.SetSelection(0)
        self._airport_codes = dict(airport_choices or [])
        airport_labels = list(self._airport_codes)
        self.origin = wx.ComboBox(panel, choices=airport_labels, style=wx.CB_DROPDOWN)
        _set_explicit_accessible_name(self, self.origin, "Origin")
        self.destination = wx.ComboBox(panel, choices=airport_labels, style=wx.CB_DROPDOWN)
        _set_explicit_accessible_name(self, self.destination, "Destination")

        today = _dt.date.today()
        depart = today + _dt.timedelta(days=7)
        returning = depart + _dt.timedelta(days=7)
        self.depart_date = self._make_date_picker(panel, "Departure", depart)
        self.return_date = self._make_date_picker(panel, "Return", returning)

        # Choices are used instead of SpinCtrl because the Windows SpinCtrl's
        # embedded edit field is exposed to screen readers without its name.
        self.adults = wx.Choice(panel, choices=[str(i) for i in range(1, 10)])
        self.adults.SetSelection(0)
        _set_explicit_accessible_name(self, self.adults, "Adults age 12 and over")
        self.children = wx.Choice(panel, choices=[str(i) for i in range(0, 9)])
        self.children.SetSelection(0)
        _set_explicit_accessible_name(self, self.children, "Children age 2 to 11")
        self.infants = wx.Choice(panel, choices=[str(i) for i in range(0, 9)])
        self.infants.SetSelection(0)
        _set_explicit_accessible_name(self, self.infants, "Infants under 2")

        fields = [
            ("Trip type:", self.trip_type),
            ("Origin:", self.origin),
            ("Destination:", self.destination),
            ("Departure date:", self.depart_date["sizer"]),
            ("Return date:", self.return_date["sizer"]),
            ("Adults, age 12 and over:", self.adults),
            ("Children, age 2 to 11:", self.children),
            ("Infants, under 2:", self.infants),
        ]
        for label, control in fields:
            grid.Add(wx.StaticText(panel, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            grid.Add(control, 1, wx.EXPAND)
        outer.Add(grid, 1, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        search_btn = wx.Button(panel, wx.ID_OK, f"Open {airline_name}")
        cancel_btn = wx.Button(panel, wx.ID_CANCEL, "Cancel")
        buttons.Add(search_btn, 0, wx.RIGHT, 8)
        buttons.Add(cancel_btn, 0)
        outer.Add(buttons, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        panel.SetSizer(outer)
        self.SetSize(560, 430)
        self.trip_type.Bind(wx.EVT_CHOICE, self._on_trip_type)
        search_btn.Bind(wx.EVT_BUTTON, self._on_submit)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)
        self._on_trip_type()
        # Establish focus before ShowModal so screen readers announce the first
        # field rather than treating every static label as dialog description.
        self.trip_type.SetFocus()
        wx.CallAfter(self.trip_type.SetFocus)

    def _on_trip_type(self, event=None):
        enabled = self.trip_type.GetSelection() == 1
        for control in (self.return_date["day"], self.return_date["month"],
                        self.return_date["year"]):
            control.Enable(enabled)

    @staticmethod
    def _make_date_picker(parent, prefix, value):
        """Three named selectors: portable and predictable for screen readers."""
        row = wx.BoxSizer(wx.HORIZONTAL)
        day = wx.Choice(parent, choices=[str(i) for i in range(1, 32)])
        _set_explicit_accessible_name(parent.GetParent(), day, f"{prefix} day")
        month = wx.Choice(parent, choices=[
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        ])
        _set_explicit_accessible_name(parent.GetParent(), month, f"{prefix} month")
        current_year = __import__("datetime").date.today().year
        years = [str(y) for y in range(current_year, current_year + 3)]
        year = wx.Choice(parent, choices=years)
        _set_explicit_accessible_name(parent.GetParent(), year, f"{prefix} year")
        day.SetSelection(value.day - 1)
        month.SetSelection(value.month - 1)
        year.SetStringSelection(str(value.year))
        row.Add(day, 0, wx.RIGHT, 6)
        row.Add(month, 1, wx.RIGHT | wx.EXPAND, 6)
        row.Add(year, 0)
        return {"sizer": row, "day": day, "month": month, "year": year}

    @staticmethod
    def _date_value(control):
        return __import__("datetime").date(
            int(control["year"].GetStringSelection()),
            control["month"].GetSelection() + 1,
            control["day"].GetSelection() + 1,
        )

    def values(self):
        def _airport_code(control):
            text = control.GetValue().strip()
            return self._airport_codes.get(text, text.upper())
        return {
            "return": self.trip_type.GetSelection() == 1,
            "origin": _airport_code(self.origin),
            "destination": _airport_code(self.destination),
            "depart_date": self._date_value(self.depart_date),
            "return_date": self._date_value(self.return_date),
            "adults": int(self.adults.GetStringSelection()),
            "children": int(self.children.GetStringSelection()),
            "infants": int(self.infants.GetStringSelection()),
        }

    def _on_submit(self, event=None):
        try:
            values = self.values()
        except ValueError:
            wx.MessageBox("That day does not exist in the selected month and year.",
                          "Invalid date", wx.OK | wx.ICON_WARNING)
            self.depart_date["day"].SetFocus()
            return
        if not re.fullmatch(r"[A-Z]{3}", values["origin"]):
            wx.MessageBox("Select an origin airport.",
                          "Invalid origin", wx.OK | wx.ICON_WARNING)
            self.origin.SetFocus()
            return
        if not re.fullmatch(r"[A-Z]{3}", values["destination"]):
            wx.MessageBox("Select a destination airport.",
                          "Invalid destination", wx.OK | wx.ICON_WARNING)
            self.destination.SetFocus()
            return
        if values["origin"] == values["destination"]:
            wx.MessageBox("Origin and destination must be different.",
                          "Invalid journey", wx.OK | wx.ICON_WARNING)
            return
        import datetime as _dt
        if values["depart_date"] < _dt.date.today():
            wx.MessageBox("Departure date cannot be in the past.",
                          "Invalid date", wx.OK | wx.ICON_WARNING)
            return
        if values["return"] and values["return_date"] < values["depart_date"]:
            wx.MessageBox("Return date cannot be before the departure date.",
                          "Invalid date", wx.OK | wx.ICON_WARNING)
            return
        total = values["adults"] + values["children"] + values["infants"]
        if total > 9:
            wx.MessageBox("This search supports up to 9 guests.",
                          "Too many guests", wx.OK | wx.ICON_WARNING)
            return
        if values["infants"] > values["adults"]:
            wx.MessageBox("Each infant must travel with an adult.",
                          "Invalid guests", wx.OK | wx.ICON_WARNING)
            return
        self.EndModal(wx.ID_OK)

    def _on_char_hook(self, event):
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            self.EndModal(wx.ID_CANCEL)
            return
        event.Skip()


class DateTimePickerDialog(wx.Dialog):
    """Choice-based date/time picker. Returns a datetime object."""

    def __init__(self, parent, title="Choose date and time"):
        super().__init__(parent, title=title, style=wx.DEFAULT_DIALOG_STYLE)
        import datetime as _dt
        now = _dt.datetime.now()
        # Round up to next 5 minutes
        remainder = now.minute % 5
        if remainder:
            now = (now + _dt.timedelta(minutes=5 - remainder)).replace(
                second=0, microsecond=0)
        else:
            now = now.replace(second=0, microsecond=0)

        panel = wx.Panel(self)
        vs = wx.BoxSizer(wx.VERTICAL)

        vs.Add(wx.StaticText(panel, label="Day:"), 0, wx.LEFT | wx.TOP, 8)
        self.combo_day = wx.Choice(
            panel, choices=[str(d) for d in range(1, 32)])
        self.combo_day.SetSelection(now.day - 1)
        vs.Add(self.combo_day, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 8)

        vs.Add(wx.StaticText(panel, label="Month:"), 0, wx.LEFT | wx.TOP, 8)
        months = ["January", "February", "March", "April", "May", "June",
                  "July", "August", "September", "October", "November", "December"]
        self.combo_month = wx.Choice(panel, choices=months)
        self.combo_month.SetSelection(now.month - 1)
        vs.Add(self.combo_month, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 8)

        vs.Add(wx.StaticText(panel, label="Year:"), 0, wx.LEFT | wx.TOP, 8)
        years = [str(now.year), str(now.year + 1)]
        self.combo_year = wx.Choice(panel, choices=years)
        self.combo_year.SetSelection(0)
        vs.Add(self.combo_year, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 8)

        vs.Add(wx.StaticText(panel, label="Hour:"), 0, wx.LEFT | wx.TOP, 8)
        self.combo_hour = wx.Choice(
            panel, choices=[f"{h:02d}" for h in range(24)])
        self.combo_hour.SetSelection(now.hour)
        vs.Add(self.combo_hour, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 8)

        vs.Add(wx.StaticText(panel, label="Minute:"), 0, wx.LEFT | wx.TOP, 8)
        self.combo_min = wx.Choice(
            panel, choices=[f"{m:02d}" for m in range(0, 60, 5)])
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
        if _hook_escape_enter(self, event):
            return
        event.Skip()


class JourneyResultsDialog(wx.Dialog):
    """Two-level journey results: listbox of route summaries,
    Enter expands to detail, Escape/Backspace goes back.

    Accessible buttons offer a screen-reader-friendly summary from Google
    directions, plus an OSM walking alternative when available."""

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

        # Level 2a: route detail (hidden initially)
        self.detail = wx.TextCtrl(
            panel, value="",
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP,
        )
        self._sizer.Add(self.detail, 1, wx.ALL | wx.EXPAND, 10)
        self.detail.Hide()

        # Action buttons — platform and stop data only apply to transit journeys.
        self._has_transit = any(
            leg.get("type") == "transit"
            for r in routes for leg in (r.get("legs") or [])
        )
        action_row = wx.BoxSizer(wx.HORIZONTAL)
        self._platforms_btn = None
        self._stops_btn = None
        if self._has_transit:
            self._platforms_btn = wx.Button(panel, label="Show Platforms")
            action_row.Add(self._platforms_btn, 0, wx.RIGHT, 10)
            self._stops_btn = wx.Button(panel, label="Show Stops")
            action_row.Add(self._stops_btn, 0, wx.RIGHT, 10)
        self._sizer.Add(action_row, 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)

        accessible_row = wx.BoxSizer(wx.HORIZONTAL)
        self._accessible_osm_btn = wx.Button(panel, label="Accessible directions")
        accessible_row.Add(self._accessible_osm_btn, 0)
        self._sizer.Add(accessible_row, 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)

        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        close_btn = wx.Button(panel, wx.ID_CLOSE, "Close")
        btn_row.Add(close_btn, 0)
        self._sizer.Add(btn_row, 0, wx.LEFT | wx.BOTTOM, 10)

        panel.SetSizer(self._sizer)
        self.SetSize(620, 440)

        if self._platforms_btn:
            self._platforms_btn.Bind(wx.EVT_BUTTON, self._on_show_platforms)
        if self._stops_btn:
            self._stops_btn.Bind(wx.EVT_BUTTON, self._on_show_stops)
        self._accessible_osm_btn.Bind(wx.EVT_BUTTON, self._on_accessible_osm)
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

    def _selected_route_index(self):
        sel = self.listbox.GetSelection()
        if sel == wx.NOT_FOUND or sel >= len(self._routes):
            return 0 if self._routes else wx.NOT_FOUND
        return sel

    def _show_accessible_result(self, title: str, text: str):
        dlg = wx.Dialog(self, title=title, style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        sizer = wx.BoxSizer(wx.VERTICAL)
        ctrl = wx.TextCtrl(dlg, value=text,
                           style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP)
        ctrl.SetMinSize((480, 260))
        sizer.Add(ctrl, 1, wx.EXPAND | wx.ALL, 8)
        btn = wx.Button(dlg, wx.ID_CLOSE, "Close (Escape)")
        sizer.Add(btn, 0, wx.ALIGN_RIGHT | wx.RIGHT | wx.BOTTOM, 8)
        dlg.SetSizer(sizer)
        dlg.Fit()

        def _close(evt=None):
            dlg.EndModal(wx.ID_CLOSE)
            self.SetFocus()
            self.listbox.SetFocus()

        btn.Bind(wx.EVT_BUTTON, _close)
        dlg.Bind(
            wx.EVT_CHAR_HOOK,
            lambda e: _close() if e.GetKeyCode() == wx.WXK_ESCAPE else e.Skip(),
        )
        ctrl.SetFocus()
        ctrl.SelectAll()
        dlg.ShowModal()
        dlg.Destroy()
        parent = self.GetParent()
        if parent and hasattr(parent, "_finish_thinking"):
            parent._finish_thinking()

    def _on_show_platforms(self, event=None):
        if not self._platforms_btn:
            return
        self._platforms_btn.Disable()
        self._platforms_btn.SetLabel("Loading platforms…")
        parent = self.GetParent()
        if hasattr(parent, "_fetch_journey_platforms"):
            parent._fetch_journey_platforms(self._routes, self._on_platforms_ready)
        else:
            self._platforms_btn.SetLabel("Not available")

    def _on_show_stops(self, event=None):
        if not self._stops_btn:
            return
        idx = self._selected_route_index()
        if idx == wx.NOT_FOUND:
            return
        self._stops_btn.Disable()
        self._stops_btn.SetLabel("Loading stops…")
        parent = self.GetParent()
        if hasattr(parent, "_fetch_journey_stops"):
            parent._fetch_journey_stops(self._routes, idx, self._on_stops_ready)
        else:
            self._stops_btn.SetLabel("Not available")

    def _on_stops_ready(self, title, items):
        if self._stops_btn:
            self._stops_btn.Enable()
            self._stops_btn.SetLabel("Show Stops")
        dlg = wx.Dialog(self, title=title,
                        style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        sizer = wx.BoxSizer(wx.VERTICAL)
        stops = wx.ListBox(dlg, choices=items or ["No stop data available."],
                           style=wx.LB_SINGLE)
        if stops.GetCount():
            stops.SetSelection(0)
        stops.SetMinSize((560, 320))
        sizer.Add(stops, 1, wx.EXPAND | wx.ALL, 10)
        close_btn = wx.Button(dlg, wx.ID_CLOSE, "Close")
        sizer.Add(close_btn, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        dlg.SetSizer(sizer)
        dlg.SetSize(620, 440)
        close_btn.Bind(wx.EVT_BUTTON, lambda e: dlg.EndModal(wx.ID_CLOSE))
        dlg.Bind(wx.EVT_CHAR_HOOK,
                 lambda e: dlg.EndModal(wx.ID_CLOSE)
                 if e.GetKeyCode() in (wx.WXK_ESCAPE, wx.WXK_BACK)
                 else e.Skip())
        wx.CallAfter(stops.SetFocus)
        dlg.ShowModal()
        dlg.Destroy()
        if self._showing_detail:
            self.detail.SetFocus()
        else:
            self.listbox.SetFocus()

    def _on_accessible_osm(self, event=None):
        idx = self._selected_route_index()
        if idx == wx.NOT_FOUND:
            return
        parent = self.GetParent()
        if hasattr(parent, "_fetch_journey_accessible_osm"):
            parent._fetch_journey_accessible_osm(
                self._routes, idx, self._show_accessible_segments)
        else:
            self._show_accessible_result(
                "Journey Planner - Accessible directions",
                "Accessible directions are not available here.")

    def _show_accessible_segments(self, title, items):
        """Open the navigable segment list for an accessible route."""
        if isinstance(items, str):
            # Defensive: tolerate a plain-text result.
            items = [{"text": items, "lat": None, "lon": None, "heading": None}]
        dlg = AccessibleRouteDialog(self, title, items or [])
        dlg.ShowModal()
        dlg.Destroy()
        self.listbox.SetFocus()
        parent = self.GetParent()
        if parent and hasattr(parent, "_finish_thinking"):
            parent._finish_thinking()

    def _on_platforms_ready(self, routes):
        self._routes = routes
        if self._platforms_btn:
            self._platforms_btn.SetLabel("Platforms loaded")
        if self._showing_detail:
            sel = self.listbox.GetSelection()
            if sel != wx.NOT_FOUND and sel < len(self._routes):
                self.detail.SetValue(self._routes[sel]["detail_text"])
                self.detail.SetInsertionPoint(0)

    def _on_list_key(self, event):
        code = event.GetKeyCode()
        if code in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            self._show_detail()
            return
        event.Skip()

    def _on_char_hook(self, event):
        if _hook_detail_list(self, event, self._showing_detail, self._show_list, self._show_detail):
            return
        event.Skip()


class AccessibleRouteDialog(wx.Dialog):
    """Flat, navigable list of accessible-route segments.

    Each list item is fully self-describing (street, intersections, places by
    side, the turn) so a screen reader reads the whole stretch on arrow.  V (or
    the button) fetches a Street View description for the selected item's
    waypoint on demand.  Escape closes.
    """

    def __init__(self, parent, title, items):
        super().__init__(parent, title=title,
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self._items = items or []
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        intro = wx.StaticText(
            panel,
            label=("Route segments. Up and Down to browse, V for Street View "
                   "of the selected segment, Escape to close."))
        sizer.Add(intro, 0, wx.ALL, 8)

        self.listbox = wx.ListBox(
            panel, choices=[it.get("text", "") for it in self._items],
            style=wx.LB_SINGLE)
        sizer.Add(self.listbox, 1, wx.EXPAND | wx.ALL, 8)

        row = wx.BoxSizer(wx.HORIZONTAL)
        self._sv_btn = wx.Button(panel, label="Street View here (V)")
        row.Add(self._sv_btn, 0, wx.RIGHT, 8)
        close_btn = wx.Button(panel, wx.ID_CLOSE, "Close")
        row.Add(close_btn, 0)
        sizer.Add(row, 0, wx.ALL, 8)

        panel.SetSizer(sizer)
        self.SetSize(680, 480)

        self._sv_btn.Bind(wx.EVT_BUTTON, self._on_streetview)
        close_btn.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CLOSE))
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)
        if self._items:
            self.listbox.SetSelection(0)
        wx.CallAfter(self.listbox.SetFocus)

    def _selected(self):
        i = self.listbox.GetSelection()
        if i == wx.NOT_FOUND or i >= len(self._items):
            return None
        return self._items[i]

    def _on_streetview(self, event=None):
        it = self._selected()
        if not it:
            return
        lat, lon = it.get("lat"), it.get("lon")
        parent = self.GetParent()
        target = parent.GetParent() if parent else None
        if lat is None or lon is None:
            self._popup_text("Street View", "This item has no map point to look at.")
            return
        if not (target and hasattr(target, "_fetch_segment_streetview")):
            self._popup_text("Street View", "Street View is not available here.")
            return
        self._sv_btn.Disable()
        self._sv_btn.SetLabel("Loading Street View...")
        target._fetch_segment_streetview(lat, lon, it.get("heading"), self._on_sv_result)

    def _on_sv_result(self, text):
        self._sv_btn.Enable()
        self._sv_btn.SetLabel("Street View here (V)")
        self._popup_text("Street View", text or "No Street View description available.")

    def _popup_text(self, title, text):
        dlg = wx.Dialog(self, title=title,
                        style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        s = wx.BoxSizer(wx.VERTICAL)
        ctrl = wx.TextCtrl(dlg, value=text,
                           style=wx.TE_MULTILINE | wx.TE_READONLY)
        ctrl.SetMinSize((460, 220))
        s.Add(ctrl, 1, wx.EXPAND | wx.ALL, 8)
        btn = wx.Button(dlg, wx.ID_CLOSE, "Close (Escape)")
        s.Add(btn, 0, wx.ALIGN_RIGHT | wx.RIGHT | wx.BOTTOM, 8)
        dlg.SetSizer(s)
        dlg.Fit()

        def _close(evt=None):
            dlg.EndModal(wx.ID_CLOSE)

        btn.Bind(wx.EVT_BUTTON, _close)
        dlg.Bind(wx.EVT_CHAR_HOOK,
                 lambda e: _close() if e.GetKeyCode() == wx.WXK_ESCAPE else e.Skip())
        ctrl.SetFocus()
        dlg.ShowModal()
        dlg.Destroy()
        self.listbox.SetFocus()

    def _on_char_hook(self, event):
        code = event.GetKeyCode()
        if code == wx.WXK_ESCAPE:
            self.EndModal(wx.ID_CLOSE)
            return
        if code in (ord('v'), ord('V')):
            self._on_streetview()
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
        for list_ctrl in (
                self.listbox, self.dep_listbox,
                self.cand_listbox, self.stops_listbox):
            list_ctrl.Bind(wx.EVT_KEY_DOWN, self._on_key_down)
        wx.CallAfter(self.listbox.SetFocus)

    def _on_key_down(self, event):
        """Listbox fallback for browsers/controls that swallow Backspace."""
        _log_key_event(self, event, "dialog-key-down", type(event.GetEventObject()).__name__ if event.GetEventObject() else "")
        code = event.GetKeyCode()
        if code in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER, wx.WXK_ESCAPE, wx.WXK_BACK):
            self._on_char_hook(event)
            return
        event.Skip()

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
        wx.CallAfter(self.dep_listbox.SetFocus)

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
        wx.CallAfter(self.stops_listbox.SetFocus)

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
            wx.CallAfter(self.cand_listbox.SetFocus)
            return

        # ── Normal stop list ──────────────────────────────────────────
        self.stops_listbox.Clear()
        stops = result if isinstance(result, list) else []
        coords_stops = []
        if stops:
            for s in stops:
                if isinstance(s, dict):
                    name = s.get("name", s.get("stop_name", "?"))
                    if s.get("lat") and s.get("lon"):
                        coords_stops.append(s)
                else:
                    name = str(s)
                self.stops_listbox.Append(name)
            self.stops_listbox.SetSelection(0)
        else:
            self.stops_listbox.Append("No timetable data available for this service.")
        if coords_stops:
            self._current_stop_list = coords_stops
        self._level = 3
        self.cand_listbox.Hide()
        self.stops_listbox.Show()
        self.Layout()
        wx.CallAfter(self.stops_listbox.SetFocus)

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
            for name in stop_names:
                self.stops_listbox.Append(name)
            self.stops_listbox.SetSelection(0)
        else:
            self.stops_listbox.Append("No stop data for this route variant.")
        self.cand_listbox.Hide()
        self.stops_listbox.Show()
        self._level = 3
        self.Layout()
        wx.CallAfter(self.stops_listbox.SetFocus)

    def _on_char_hook(self, event):
        _log_key_event(self, event, "dialog-char-hook", f"level={self._level}")
        def _on_primary(evt):
            code = evt.GetKeyCode()
            # Ctrl+Alt+F — find food along the stop sequence shown at level 3
            if (_primary_down(evt) and evt.AltDown()
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
                return True
            return False
        def _show_list():
            if self._level == 3:
                self.stops_listbox.Hide()
                if self._candidates:
                    self.cand_listbox.Show()
                    self._level = 2
                    self.Layout()
                    wx.CallAfter(self.cand_listbox.SetFocus)
                else:
                    self.dep_listbox.Show()
                    self._level = 1
                    self.Layout()
                    wx.CallAfter(self.dep_listbox.SetFocus)
            elif self._level == 2:
                self._candidates = []
                self.cand_listbox.Hide()
                self.dep_listbox.Show()
                self._level = 1
                self.Layout()
                wx.CallAfter(self.dep_listbox.SetFocus)
            elif self._level == 1:
                self._title_label.SetLabel("Nearby stops and stations:")
                self.dep_listbox.Hide()
                self.listbox.Show()
                self._level = 0
                self.Layout()
                wx.CallAfter(self.listbox.SetFocus)
            else:
                self.EndModal(wx.ID_CLOSE)
        def _show_detail():
            if self._level == 0:
                self._show_departures()
            elif self._level == 1 and self._fetch_stops:
                self._show_stops()
            elif self._level == 2:
                self._show_stops_for_candidate()
        # Every level above the station list is a detail level.  Passing only
        # level 3 here made Backspace a no-op on departures and candidates.
        if _hook_detail_list(self, event, self._level > 0, _show_list, _show_detail,
                             escape_id=wx.ID_CLOSE, on_primary=_on_primary):
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

        prompt = "From — type city or airport name:"
        self._prompt_lbl = wx.TextCtrl(
            self,
            value=prompt,
            style=wx.TE_READONLY | wx.BORDER_NONE,
        )
        self._prompt_lbl.SetName(prompt)
        self._prompt_lbl.SetBackgroundColour(self.GetBackgroundColour())
        self._prompt_lbl.SetForegroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOWTEXT))
        vs.Add(self._prompt_lbl, 0, wx.LEFT | wx.TOP | wx.EXPAND, 8)
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
            miab_log("errors", f"[FlightSearch] Airport load failed: {exc}", getattr(self, "settings", None))

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
            _return_parent_focus(self)
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
            self._prompt_lbl.SetValue(f"{prompt}:")
            self._prompt_lbl.SetName(f"{prompt}:")
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

    def __init__(self, parent, places: list, detail_cb, title="Find Food", route_destination=None):
        super().__init__(parent, title=title,
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self._places    = places
        self._detail_cb = detail_cb
        self._showing_detail = False
        self._fetching  = False
        self._route_destination = route_destination or {}

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

    def _selected_place_name(self, place: dict) -> str:
        return (place.get("name") or "food place").split(",")[0].strip()

    def _selected_place_coords(self):
        place = self._selected_place()
        if not place or self._fetching:
            return None, None, None
        try:
            coords = (float(place.get("lat", 0.0)), float(place.get("lon", 0.0)))
        except Exception:
            return place, None, None
        return place, coords, self._selected_place_name(place)

    def _current_route_destination(self):
        if self._route_destination:
            return self._route_destination
        parent = self.GetParent()
        if parent and hasattr(parent, "_find_food_destination"):
            return getattr(parent, "_find_food_destination", None)
        if parent and hasattr(parent, "_map_destination"):
            return getattr(parent, "_map_destination", None)
        return None

    def _detail_announce(self, msg: str) -> None:
        parent = self.GetParent()
        if parent and hasattr(parent, "_poi_detail_announce"):
            try:
                parent._poi_detail_announce(msg)
                return
            except Exception:
                pass
        self._announce(msg)

    def _place_detail_text(self, place: dict, key_num: int) -> str:
        tags = place.get("tags") or {}
        name = self._selected_place_name(place)
        if key_num == 1:
            text = (place.get("address") or "").strip()
            if not text:
                text = name
            return text or "No address available."
        if key_num == 2:
            return (place.get("opening_hours") or tags.get("opening_hours") or "").strip() or "Opening hours not available."
        if key_num == 3:
            return (place.get("phone") or tags.get("phone") or tags.get("contact:phone") or "").strip() or "No phone number available."
        if key_num == 4:
            return (place.get("website") or tags.get("website") or tags.get("contact:website") or "").strip() or "No website available."
        return ""

    def _place_detail_raw(self, place: dict, key_num: int) -> str:
        tags = place.get("tags") or {}
        if key_num == 1:
            return (place.get("address") or "").strip()
        if key_num == 2:
            return (place.get("opening_hours") or tags.get("opening_hours") or "").strip()
        if key_num == 3:
            return (place.get("phone") or tags.get("phone") or tags.get("contact:phone") or "").strip()
        if key_num == 4:
            return (place.get("website") or tags.get("website") or tags.get("contact:website") or "").strip()
        return ""

    def _fetch_place_detail(self, place: dict, key_num: int, name: str) -> None:
        self._fetching = True
        self._detail_announce(f"Looking up {name}...")

        def _fetch():
            try:
                info = self._detail_cb(name, place.get("lat", 0.0), place.get("lon", 0.0))
            except Exception as exc:
                info = {
                    "address": f"Error: {exc}",
                    "phone": "",
                    "website": "",
                    "opening_hours": "",
                }
            wx.CallAfter(self._finish_place_detail_action, place, key_num, info or {})

        import threading
        threading.Thread(target=_fetch, daemon=True).start()

    def _finish_place_detail_action(self, place: dict, key_num: int, info: dict) -> None:
        self._fetching = False
        place.update(info or {})
        self._detail_announce(self._place_detail_text(place, key_num))

    def _delete_selected_place(self):
        place = self._selected_place()
        if not place or self._fetching:
            return
        old_sel = self.listbox.GetSelection()
        name = self._selected_place_name(place)
        dlg = wx.MessageDialog(
            self,
            f"Are you sure '{name}' no longer exists?",
            "Report Missing POI",
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION,
        )
        if dlg.ShowModal() != wx.ID_YES:
            dlg.Destroy()
            self.listbox.SetFocus()
            return
        dlg.Destroy()
        parent = self.GetParent()
        if parent and hasattr(parent, "_suppress_poi_entry"):
            try:
                parent._suppress_poi_entry(place, name)
            except Exception:
                pass
        self._places = [p for p in self._places if p is not place]
        summaries = [self._fmt_summary(p) for p in self._places]
        self.listbox.Set(summaries)
        if self._places:
            new_sel = min(max(old_sel, 0), len(self._places) - 1)
            self.listbox.SetSelection(new_sel)
            self.listbox.EnsureVisible(new_sel)
            self.listbox.SetFocus()
            if self._showing_detail:
                self._show_list()
            self._announce(f"Deleted {name}.")
        else:
            self._announce(f"Deleted {name}. No more results.")
            self.EndModal(wx.ID_CLOSE)

    def _rename_selected_place(self):
        place = self._selected_place()
        if not place or self._fetching:
            return
        old_name = (place.get("name") or "food place").split(",")[0].strip()
        dlg = wx.TextEntryDialog(
            self,
            f"New name for '{old_name}':",
            "Rename POI",
            old_name,
        )
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            self.listbox.SetFocus()
            return
        new_name = dlg.GetValue().strip()
        dlg.Destroy()
        if not new_name or new_name.lower() == old_name.lower():
            self.listbox.SetFocus()
            return

        parent = self.GetParent()
        if parent and hasattr(parent, "_rename_poi_entry"):
            try:
                updated, _ = parent._rename_poi_entry(place, new_name, old_name)
                place.update(updated)
            except Exception:
                place["name"] = new_name
        else:
            place["name"] = new_name

        self.listbox.Set(self._rebuild_summaries())
        if self._showing_detail:
            self._show_list()
        self._announce(f"Renamed to {new_name}.")

    def _mark_selected_place(self):
        place, coords, name = self._selected_place_coords()
        if not place or not coords:
            return
        parent = self.GetParent()
        if not parent or not hasattr(parent, "_prompt_mark_slot"):
            return
        parent._prompt_mark_slot(remove=False, coords=coords, name=name)

    def _set_selected_place_destination(self):
        place, coords, name = self._selected_place_coords()
        if not place or not coords:
            return
        parent = self.GetParent()
        if not parent or not hasattr(parent, "_set_route_destination_from_coords"):
            return
        self._route_destination = {"coords": coords, "name": name}
        parent._set_route_destination_from_coords(coords, name)

    def _handle_selected_place_key(self, event) -> bool:
        code = event.GetKeyCode()
        if _primary_down(event) and code in (ord("W"), ord("w")):
            self._open_selected_website()
            return True
        if _primary_down(event) and not event.AltDown() and code in (ord("1"), ord("2"), ord("3")):
            parent = self.GetParent()
            if parent and hasattr(parent, "_announce_mark"):
                parent._announce_mark(int(chr(code)), return_focus=False)
                return True
        if event.ShiftDown() and event.AltDown() and not _primary_down(event) and code in (ord("M"), ord("m")):
            parent = self.GetParent()
            if parent and hasattr(parent, "_report_all_mark_distances"):
                parent._report_all_mark_distances(return_focus=False)
                return True
        if _primary_down(event) and not event.ShiftDown() and not event.AltDown() and code in (ord("M"), ord("m")):
            self._mark_selected_place()
            return True
        if code == wx.WXK_F2:
            self._rename_selected_place()
            return True
        if code == wx.WXK_DELETE:
            self._delete_selected_place()
            return True
        if self._handle_ctrl_alt_place_shortcut(event):
            return True
        return False

    def _handle_ctrl_alt_place_shortcut(self, event) -> bool:
        if not (_primary_down(event) and event.AltDown()):
            return False
        code = event.GetKeyCode()
        if code not in (ord("1"), ord("2"), ord("3"), ord("4"), ord("5"), ord("6")):
            return False

        place = self._selected_place()
        if not place or self._fetching:
            return True

        name = self._selected_place_name(place)
        key_num = int(chr(code))
        parent = self.GetParent()

        if key_num == 4:
            if self._announce_parent_verified_website(place, name):
                return True
            text = self._place_detail_raw(place, key_num)
            self._detail_announce(text or "No website available.")
            return True

        if key_num in (1, 2, 3):
            text = self._place_detail_raw(place, key_num)
            if text:
                self._detail_announce(text)
                return True
            if self._detail_cb:
                self._fetch_place_detail(place, key_num, name)
                return True
            self._detail_announce(text or "No information available.")
            return True

        if key_num == 5:
            suburb = getattr(parent, "_current_suburb", "") or place.get("address", "")
            if parent and hasattr(parent, "_open_place_reviews"):
                parent._open_place_reviews(name, suburb)
            else:
                import webbrowser, urllib.parse
                query = " ".join(p for p in (name, suburb, "reviews") if p)
                webbrowser.open("https://www.google.com/search?q=" + urllib.parse.quote_plus(query))
                self._detail_announce(f"Opening Google reviews for {name} in your browser...")
            return True

        if key_num == 6:
            if parent and hasattr(parent, "_lookup_menu_links_for_poi"):
                parent._lookup_menu_links_for_poi(place, name)
            else:
                self._detail_announce("Menu lookup is not available here.")
            return True

        return True

    def _rebuild_summaries(self):
        return [self._fmt_summary(p) for p in self._places]

    def _open_selected_website(self):
        place = self._selected_place()
        if not place or self._fetching:
            return
        name = place.get("name", "food place")
        url = (place.get("website") or "").strip()
        if self._open_parent_verified_website(place, name):
            return
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
            name = place.get("name", "food place")
            self._open_food_url(url)
            return

        import urllib.parse
        name = place.get("name", "food place")
        address = place.get("address", "")
        query = " ".join(p for p in (name, address) if p).strip()
        search_url = f"https://www.google.com/search?q={urllib.parse.quote(query)}"
        self._open_food_url(search_url, found=False, label=query)

    def _open_parent_verified_website(self, place: dict, name: str) -> bool:
        parent = self.GetParent()
        opener = getattr(parent, "_open_verified_website_for", None) if parent else None
        if not callable(opener):
            return False
        return bool(opener(place, name=name))

    def _announce_parent_verified_website(self, place: dict, name: str) -> bool:
        parent = self.GetParent()
        announcer = getattr(parent, "_announce_verified_website_for", None) if parent else None
        if not callable(announcer):
            return False
        return bool(announcer(place, name=name, announce_cb=self._detail_announce))

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
        if self._handle_selected_place_key(event):
            return
        parent = self.GetParent()
        if parent and hasattr(parent, "on_key") and (_primary_down(event) or event.AltDown()):
            parent.on_key(event)
            return
        if code in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            self._show_detail()
            return
        event.Skip()

    def _on_char_hook(self, event):
        def _on_primary(evt):
            if self._handle_selected_place_key(evt):
                return True
            parent = self.GetParent()
            if parent and hasattr(parent, "on_key") and (_primary_down(evt) or evt.AltDown()):
                parent.on_key(evt)
                return True
            return False
        if _hook_detail_list(self, event, self._showing_detail,
                             self._show_list, self._show_detail,
                             escape_id=wx.ID_CLOSE, on_primary=_on_primary):
            return
        event.Skip()


class HotelResultsDialog(wx.Dialog):
    def __init__(self, parent, hotels, show_reviews=False,
                 show_google_reviews=False, show_tripadvisor_reviews=False):
        super().__init__(parent, title="Hotels",
                         size=(500, 500),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)

        self.hotels = hotels
        self.selected_index = None
        # Which action the caller should take for the selected hotel.
        self.action = "open"
        if show_reviews and not (show_google_reviews or show_tripadvisor_reviews):
            show_tripadvisor_reviews = True

        panel = wx.Panel(self)
        vs = wx.BoxSizer(wx.VERTICAL)

        self.listbox = wx.ListBox(panel, style=wx.LB_SINGLE)
        vs.Add(self.listbox, 1, wx.ALL | wx.EXPAND, 10)

        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        self._google_reviews_available = bool(show_google_reviews)
        self._tripadvisor_reviews_available = bool(show_tripadvisor_reviews)
        if self._google_reviews_available:
            google_btn = wx.Button(panel, wx.ID_ANY, "Get Google &Reviews")
            google_btn.Bind(wx.EVT_BUTTON, self._on_google_reviews)
            btn_row.Add(google_btn, 0, wx.RIGHT, 8)
        if self._tripadvisor_reviews_available:
            tripadvisor_btn = wx.Button(panel, wx.ID_ANY, "Get Trip&Advisor Reviews")
            tripadvisor_btn.Bind(wx.EVT_BUTTON, self._on_tripadvisor_reviews)
            btn_row.Add(tripadvisor_btn, 0, wx.RIGHT, 8)
        close_btn = wx.Button(panel, wx.ID_CANCEL, "Close")
        btn_row.Add(close_btn, 0)
        vs.Add(btn_row, 0, wx.ALL | wx.ALIGN_RIGHT, 10)

        panel.SetSizer(vs)

        # Populate list
        items = []
        for h in hotels:
            name = h.get("name", "")
            address = h.get("address", "")
            items.append(f"{name} - {address}" if address else name)

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
            self.action = "open"
            self.EndModal(wx.ID_OK)

    def _on_google_reviews(self, event=None):
        sel = self.listbox.GetSelection()
        if sel != wx.NOT_FOUND:
            self.selected_index = sel
            self.action = "google_reviews"
            self.EndModal(wx.ID_OK)

    def _on_tripadvisor_reviews(self, event=None):
        sel = self.listbox.GetSelection()
        if sel != wx.NOT_FOUND:
            self.selected_index = sel
            self.action = "tripadvisor_reviews"
            self.EndModal(wx.ID_OK)

    def _on_key(self, event):
        if _hook_escape_enter(self, event, on_enter=self._on_enter):
            return
        event.Skip()

    def _on_char(self, event):
        if _hook_escape_enter(self, event, on_enter=self._on_enter):
            return
        key = event.GetKeyCode()
        if key == ord('5') and event.AltDown() and self._google_reviews_available:
            self._on_google_reviews()
            return
        if key in (ord('A'), ord('a')) and event.AltDown() and self._tripadvisor_reviews_available:
            self._on_tripadvisor_reviews()
            return
        event.Skip()
