"""Local favourites for Map in a Box.

Stores user-saved POIs and places in a small JSON file and presents them in
an accessible two-tab dialog.
"""

from __future__ import annotations

import json
import math
import os
import time
import uuid

import wx

from geo import bearing_deg, compass_name


_user_dir = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'MapInABox')
os.makedirs(_user_dir, exist_ok=True)
FAVOURITES_PATH = os.path.join(_user_dir, "favourites.json")


def load_favourites(path: str = FAVOURITES_PATH) -> list[dict]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_favourites(entries: list[dict], path: str = FAVOURITES_PATH) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)


def favourite_key(entry: dict) -> tuple:
    name = str(entry.get("name", "")).strip().lower()
    try:
        lat = round(float(entry.get("lat", 0)), 5)
        lon = round(float(entry.get("lon", 0)), 5)
    except (TypeError, ValueError):
        lat = lon = 0
    return entry.get("type", "place"), name, lat, lon


def make_favourite(name: str, lat: float, lon: float, fav_type: str,
                   kind: str = "", source: str = "", meta: dict | None = None) -> dict:
    fav_type = "poi" if fav_type == "poi" else "place"
    return {
        "id": uuid.uuid4().hex,
        "type": fav_type,
        "name": name.strip() or "Unnamed favourite",
        "lat": float(lat),
        "lon": float(lon),
        "kind": kind or ("POI" if fav_type == "poi" else "place"),
        "source": source or "favourite",
        "created_at": time.time(),
        "meta": meta or {},
    }


def add_or_replace_favourite(entry: dict) -> tuple[list[dict], bool]:
    entries = load_favourites()
    key = favourite_key(entry)
    replaced = False
    for idx, existing in enumerate(entries):
        if favourite_key(existing) == key:
            entry = dict(entry)
            entry["id"] = existing.get("id") or entry.get("id") or uuid.uuid4().hex
            entry["created_at"] = existing.get("created_at", entry.get("created_at", time.time()))
            entries[idx] = entry
            replaced = True
            break
    if not replaced:
        entries.append(entry)
    save_favourites(entries)
    return entries, replaced


def _distance_label(from_lat: float, from_lon: float, to_lat: float, to_lon: float) -> str:
    dlat = (to_lat - from_lat) * 111000
    dlon = (to_lon - from_lon) * 111000 * math.cos(math.radians(from_lat))
    metres = math.sqrt(dlat * dlat + dlon * dlon)
    direction = compass_name(bearing_deg(from_lat, from_lon, to_lat, to_lon))
    if metres < 1000:
        return f"{int(round(metres))} metres {direction}"
    return f"{metres / 1000:.1f} kilometres {direction}"


def favourite_label(entry: dict, current_lat: float, current_lon: float) -> str:
    name = str(entry.get("name") or "Unnamed favourite").strip()
    kind = str(entry.get("kind") or "").strip()
    try:
        lat = float(entry["lat"])
        lon = float(entry["lon"])
        distance = _distance_label(current_lat, current_lon, lat, lon)
    except Exception:
        distance = ""
    parts = [name]
    if kind and kind.lower() not in ("poi", "place"):
        parts.append(kind)
    if distance:
        parts.append(distance)
    return ", ".join(parts)


class FavouritesDialog(wx.Dialog):
    """Two-tab favourites browser."""

    def __init__(self, parent, entries: list[dict]):
        super().__init__(
            parent,
            title="Favourites",
            size=(560, 420),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self._parent = parent
        self.entries = entries
        self._current_items: dict[str, list[dict]] = {"poi": [], "place": []}

        panel = wx.Panel(self)
        vs = wx.BoxSizer(wx.VERTICAL)
        self.notebook = wx.Notebook(panel)
        self.poi_page = wx.Panel(self.notebook)
        self.place_page = wx.Panel(self.notebook)
        self.poi_list = wx.ListBox(self.poi_page, style=wx.LB_SINGLE)
        self.place_list = wx.ListBox(self.place_page, style=wx.LB_SINGLE)
        self._setup_page(self.poi_page, self.poi_list)
        self._setup_page(self.place_page, self.place_list)
        self.notebook.AddPage(self.poi_page, "POIs")
        self.notebook.AddPage(self.place_page, "Places")
        vs.Add(self.notebook, 1, wx.EXPAND | wx.ALL, 8)
        close_btn = wx.Button(panel, wx.ID_CLOSE, "Close")
        vs.Add(close_btn, 0, wx.ALIGN_RIGHT | wx.RIGHT | wx.BOTTOM, 8)
        panel.SetSizer(vs)

        close_btn.Bind(wx.EVT_BUTTON, lambda e: self.Destroy())
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)
        for lb in (self.poi_list, self.place_list):
            lb.Bind(wx.EVT_LISTBOX_DCLICK, lambda e: self._jump_selected())
            lb.Bind(wx.EVT_CONTEXT_MENU, self._show_context_menu)

        self.refresh()
        self.CentreOnParent()
        self.notebook.SetFocus()

    def _setup_page(self, page: wx.Panel, listbox: wx.ListBox) -> None:
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(listbox, 1, wx.EXPAND | wx.ALL, 8)
        page.SetSizer(sizer)

    def refresh(self) -> None:
        pois = [e for e in self.entries if e.get("type") == "poi"]
        places = [e for e in self.entries if e.get("type") != "poi"]
        pois.sort(key=lambda e: str(e.get("name", "")).lower())
        places.sort(key=lambda e: str(e.get("name", "")).lower())
        self._current_items = {"poi": pois, "place": places}
        self._set_list(self.poi_list, pois)
        self._set_list(self.place_list, places)

    def _set_list(self, listbox: wx.ListBox, items: list[dict]) -> None:
        labels = [
            favourite_label(item, self._parent.lat, self._parent.lon)
            for item in items
        ]
        listbox.Set(labels or ["No favourites saved."])
        listbox.SetSelection(0)

    def _active_kind_and_list(self) -> tuple[str, wx.ListBox]:
        page = self.notebook.GetSelection()
        if page == 0:
            return "poi", self.poi_list
        return "place", self.place_list

    def _selected_entry(self) -> dict | None:
        kind, listbox = self._active_kind_and_list()
        items = self._current_items[kind]
        idx = listbox.GetSelection()
        if idx == wx.NOT_FOUND or idx < 0 or idx >= len(items):
            return None
        return items[idx]

    def _announce(self, msg: str) -> None:
        if hasattr(self._parent, "_status_update"):
            wx.CallAfter(self._parent._status_update, msg, True)
        else:
            wx.CallAfter(self._parent.update_ui, msg)

    def _on_char_hook(self, event) -> None:
        key = event.GetKeyCode()
        if key == wx.WXK_ESCAPE:
            self.Destroy()
            return
        if key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            self._jump_selected()
            return
        if key == wx.WXK_DELETE:
            self._delete_selected()
            return
        if key == wx.WXK_F10 and event.ShiftDown():
            self._show_context_menu(event)
            return
        if key == getattr(wx, "WXK_MENU", -1):
            self._show_context_menu(event)
            return
        event.Skip()

    def _jump_selected(self) -> None:
        entry = self._selected_entry()
        if not entry:
            self._announce("No favourite selected.")
            return
        self.Destroy()
        wx.CallAfter(self._parent._jump_to_favourite, entry)

    def _navigate_selected(self) -> None:
        entry = self._selected_entry()
        if not entry:
            self._announce("No favourite selected.")
            return
        self.Destroy()
        wx.CallAfter(self._parent._navigate_to_favourite, entry)

    def _rename_selected(self) -> None:
        entry = self._selected_entry()
        if not entry:
            self._announce("No favourite selected.")
            return
        old_name = str(entry.get("name") or "Unnamed favourite")
        dlg = wx.TextEntryDialog(self, f"New name for '{old_name}':", "Rename Favourite", old_name)
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            return
        new_name = dlg.GetValue().strip()
        dlg.Destroy()
        if not new_name:
            return
        entry["name"] = new_name
        save_favourites(self.entries)
        self.refresh()
        self._announce(f"Renamed favourite to {new_name}.")

    def _delete_selected(self) -> None:
        entry = self._selected_entry()
        if not entry:
            self._announce("No favourite selected.")
            return
        name = str(entry.get("name") or "this favourite")
        dlg = wx.MessageDialog(
            self,
            f"Delete '{name}' from favourites?",
            "Delete Favourite",
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION,
        )
        if dlg.ShowModal() != wx.ID_YES:
            dlg.Destroy()
            return
        dlg.Destroy()
        entry_id = entry.get("id")
        self.entries = [
            e for e in self.entries
            if e.get("id") != entry_id and e is not entry
        ]
        save_favourites(self.entries)
        self.refresh()
        self._announce(f"Deleted {name} from favourites.")

    def _show_context_menu(self, event) -> None:
        if not self._selected_entry():
            return
        menu = wx.Menu()
        jump = menu.Append(wx.ID_ANY, "Jump to favourite")
        nav = menu.Append(wx.ID_ANY, "Navigate to favourite")
        rename = menu.Append(wx.ID_ANY, "Rename favourite")
        delete = menu.Append(wx.ID_ANY, "Delete favourite")
        self.Bind(wx.EVT_MENU, lambda e: self._jump_selected(), jump)
        self.Bind(wx.EVT_MENU, lambda e: self._navigate_selected(), nav)
        self.Bind(wx.EVT_MENU, lambda e: self._rename_selected(), rename)
        self.Bind(wx.EVT_MENU, lambda e: self._delete_selected(), delete)
        self.PopupMenu(menu)
        menu.Destroy()
