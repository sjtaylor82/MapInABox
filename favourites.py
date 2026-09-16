"""Local favourites for Map in a Box.

Stores user-saved POIs and places in a small JSON file and presents them in
an accessible favourites manager.
"""

from __future__ import annotations

import json
import math
from distance_units import format_distance
import os
import time
import uuid

import wx
from app_paths import USER_DIR

from geo import bearing_deg, compass_name


os.makedirs(USER_DIR, exist_ok=True)
FAVOURITES_PATH = os.path.join(USER_DIR, "favourites.json")


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
    if str(entry.get("kind") or "").casefold() == "address":
        # An address remains the same favourite when better source data moves
        # its coordinate from a road centreline to the actual address point.
        return entry.get("type", "place"), name, "address"
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
    return f"{format_distance(metres)} {direction}"


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


def unified_favourite_items(entries: list[dict],
                            personal_pois: list[dict]) -> list[tuple[dict, bool]]:
    """Combine every saved-place type for the single favourites list."""
    combined = [(entry, False) for entry in entries]
    combined.extend((entry, True) for entry in personal_pois or [])
    return sorted(
        combined,
        key=lambda item: str(item[0].get("name", "")).casefold(),
    )


class FavouritesDialog(wx.Dialog):
    """Accessible unified browser for every kind of saved place."""

    def __init__(self, parent, entries: list[dict], personal_pois: list[dict] | None = None):
        super().__init__(
            parent,
            title="Favourites",
            size=(560, 420),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self._parent = parent
        self.entries = entries
        self.personal_pois = personal_pois or []
        self._current_items: list[tuple[dict, bool]] = []

        panel = wx.Panel(self)
        vs = wx.BoxSizer(wx.VERTICAL)
        self.saved_list = wx.ListBox(panel, style=wx.LB_SINGLE)
        vs.Add(self.saved_list, 1, wx.EXPAND | wx.ALL, 8)
        close_btn = wx.Button(panel, wx.ID_CLOSE, "Close")
        vs.Add(close_btn, 0, wx.ALIGN_RIGHT | wx.RIGHT | wx.BOTTOM, 8)
        panel.SetSizer(vs)

        close_btn.Bind(wx.EVT_BUTTON, lambda e: self.Destroy())
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)
        self.saved_list.Bind(
            wx.EVT_LISTBOX_DCLICK, lambda e: self._jump_selected())
        self.saved_list.Bind(wx.EVT_CONTEXT_MENU, self._show_context_menu)

        self.refresh()
        self.CentreOnParent()
        self.saved_list.SetFocus()

    def refresh(self) -> None:
        combined = unified_favourite_items(self.entries, self.personal_pois)
        self._current_items = combined
        labels = [
            favourite_label(item[0], self._parent.lat, self._parent.lon)
            for item in combined
        ]
        self.saved_list.Set(labels or ["No favourites saved."])
        self.saved_list.SetSelection(0)

    def _active_item_name(self) -> str:
        return "favourite"

    def _selected_entry(self) -> dict | None:
        idx = self.saved_list.GetSelection()
        if idx == wx.NOT_FOUND or idx < 0 or idx >= len(self._current_items):
            return None
        return self._current_items[idx][0]

    def _selected_is_personal(self) -> bool:
        idx = self.saved_list.GetSelection()
        return bool(
            idx != wx.NOT_FOUND and 0 <= idx < len(self._current_items)
            and self._current_items[idx][1])

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
            self._announce(f"No {self._active_item_name()} selected.")
            return
        is_personal = self._selected_is_personal()
        self.Destroy()
        wx.CallAfter(self._parent._jump_to_saved_entry, entry, is_personal)

    def _navigate_selected(self) -> None:
        entry = self._selected_entry()
        if not entry:
            self._announce(f"No {self._active_item_name()} selected.")
            return
        is_personal = self._selected_is_personal()
        self.Destroy()
        wx.CallAfter(self._parent._navigate_to_saved_entry, entry, is_personal)

    def _rename_selected(self) -> None:
        entry = self._selected_entry()
        if not entry:
            self._announce(f"No {self._active_item_name()} selected.")
            return
        is_personal = self._selected_is_personal()
        old_name = str(entry.get("name") or "Unnamed favourite")
        title = "Rename Personal POI" if is_personal else "Rename Favourite"
        dlg = wx.TextEntryDialog(self, f"New name for '{old_name}':", title, old_name)
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            return
        new_name = dlg.GetValue().strip()
        dlg.Destroy()
        if not new_name:
            return
        if is_personal:
            if hasattr(self._parent, "_rename_personal_poi_entry"):
                self._parent._rename_personal_poi_entry(entry, new_name)
        else:
            entry["name"] = new_name
            save_favourites(self.entries)
        self.refresh()
        item_name = "personal POI" if is_personal else "favourite"
        self._announce(f"Renamed {item_name} to {new_name}.")

    def _delete_selected(self) -> None:
        entry = self._selected_entry()
        if not entry:
            self._announce(f"No {self._active_item_name()} selected.")
            return
        is_personal = self._selected_is_personal()
        item_name = "personal POI" if is_personal else "favourite"
        name = str(entry.get("name") or f"this {item_name}")
        dlg = wx.MessageDialog(
            self,
            f"Delete '{name}' from {item_name}s?",
            "Delete Personal POI" if is_personal else "Delete Favourite",
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION,
        )
        if dlg.ShowModal() != wx.ID_YES:
            dlg.Destroy()
            return
        dlg.Destroy()
        if is_personal:
            if hasattr(self._parent, "_delete_personal_poi_entry"):
                self.personal_pois = self._parent._delete_personal_poi_entry(entry)
        else:
            entry_id = entry.get("id")
            self.entries = [
                e for e in self.entries
                if e.get("id") != entry_id and e is not entry
            ]
            save_favourites(self.entries)
        self.refresh()
        self._announce(f"Deleted {name} from {item_name}s.")

    def _show_context_menu(self, event) -> None:
        if not self._selected_entry():
            return
        is_personal = self._selected_is_personal()
        item_name = "personal POI" if is_personal else "favourite"
        menu = wx.Menu()
        jump = menu.Append(wx.ID_ANY, f"Jump to {item_name}")
        nav = menu.Append(wx.ID_ANY, f"Navigate to {item_name}")
        rename = menu.Append(wx.ID_ANY, f"Rename {item_name}")
        delete = menu.Append(wx.ID_ANY, f"Delete {item_name}")
        self.Bind(wx.EVT_MENU, lambda e: self._jump_selected(), jump)
        self.Bind(wx.EVT_MENU, lambda e: self._navigate_selected(), nav)
        self.Bind(wx.EVT_MENU, lambda e: self._rename_selected(), rename)
        self.Bind(wx.EVT_MENU, lambda e: self._delete_selected(), delete)
        self.PopupMenu(menu)
        menu.Destroy()
