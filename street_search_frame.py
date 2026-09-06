"""Accessible street-search window for Map in a Box."""

import re
import time

import wx

from logging_utils import miab_log

_STREET_GENERIC = frozenset({
    "road", "highway", "street", "residential street", "shared street",
    "service road", "motorway", "footpath", "cycle path", "path", "steps",
    "pedestrian area", "dirt track", "bridleway", "road under construction",
})

class _StreetSearchFrame(wx.Frame):

    def __init__(self, navigator):
        self._nav = navigator
        super().__init__(
            navigator,
            title="Street Search",
            size=(420, 200),
            style=(wx.DEFAULT_FRAME_STYLE | wx.FRAME_FLOAT_ON_PARENT)
                  & ~wx.MAXIMIZE_BOX & ~wx.RESIZE_BORDER,
        )
        self.SetBackgroundColour(wx.Colour(10, 20, 40))
        self.SetForegroundColour(wx.Colour(220, 220, 220))

        panel = wx.Panel(self)
        panel.SetBackgroundColour(wx.Colour(10, 20, 40))
        panel.SetForegroundColour(wx.Colour(220, 220, 220))
        vsz = wx.BoxSizer(wx.VERTICAL)

        lbl_street = wx.StaticText(panel, label="Street:")
        lbl_street.SetForegroundColour(wx.Colour(220, 220, 220))
        vsz.Add(lbl_street, 0, wx.LEFT | wx.TOP, 10)

        self._search = wx.TextCtrl(panel, style=wx.TE_PROCESS_ENTER)
        self._search.SetBackgroundColour(wx.Colour(20, 40, 70))
        self._search.SetForegroundColour(wx.Colour(220, 220, 220))
        vsz.Add(self._search, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)

        self._list = wx.ListBox(panel, style=wx.LB_SINGLE)
        self._list.SetBackgroundColour(wx.Colour(20, 40, 70))
        self._list.SetForegroundColour(wx.Colour(220, 220, 220))
        vsz.Add(self._list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)

        lbl_num = wx.StaticText(panel, label="Number (optional):")
        lbl_num.SetForegroundColour(wx.Colour(220, 220, 220))
        vsz.Add(lbl_num, 0, wx.LEFT | wx.TOP, 10)

        self._num = wx.TextCtrl(panel, style=wx.TE_PROCESS_ENTER)
        self._num.SetBackgroundColour(wx.Colour(20, 40, 70))
        self._num.SetForegroundColour(wx.Colour(220, 220, 220))
        vsz.Add(self._num, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)

        hsz = wx.BoxSizer(wx.HORIZONTAL)
        self._ok_btn     = wx.Button(panel, wx.ID_OK,     "OK")
        self._cancel_btn = wx.Button(panel, wx.ID_CANCEL, "Cancel")
        hsz.Add(self._ok_btn,     0, wx.RIGHT, 8)
        hsz.Add(self._cancel_btn, 0)
        vsz.Add(hsz, 0, wx.ALL, 10)

        panel.SetSizer(vsz)
        panel.Layout()
        self.Fit()

        self._all_names: list[str] = []
        self._filtered_names: list[str] = []
        self._last_filter_query = None
        self._selected_street_name = ""

        self._timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_timer, self._timer)
        self._timer.Start(800)

        self._search.Bind(wx.EVT_TEXT,       self._on_search_text)
        self._search.Bind(wx.EVT_TEXT_ENTER, self._on_jump)
        self._search.Bind(wx.EVT_KEY_DOWN,   self._on_search_key)
        self._list.Bind(wx.EVT_LISTBOX,       self._on_list_select)
        self._list.Bind(wx.EVT_LISTBOX_DCLICK, self._on_jump)
        self._list.Bind(wx.EVT_KEY_DOWN,     self._on_list_key)
        self._num.Bind(wx.EVT_TEXT_ENTER,    self._on_jump)
        self._ok_btn.Bind(wx.EVT_BUTTON,     self._on_jump)
        self._cancel_btn.Bind(wx.EVT_BUTTON, self._on_close)
        self.Bind(wx.EVT_CHAR_HOOK,          self._on_char_hook)
        self.Bind(wx.EVT_CLOSE,              self._on_close)

        self._refresh_combo(force=True)
        self.Layout()
        wx.CallAfter(self._search.SetFocus)
        self.CentreOnParent()

    def _street_names_from_segments(self) -> list[str]:
        segs = getattr(self._nav, '_road_segments', [])
        seen: set = set()
        names: list[str] = []
        for seg in segs:
            raw  = seg.get('name', '')
            name = re.sub(r'\s*\(.*?\)', '', raw).strip()
            if not name:
                continue
            low = name.lower()
            if low in seen:
                continue
            has_real_name = bool(seg.get("raw_name", "").strip())
            if not has_real_name and low in _STREET_GENERIC:
                continue
            seen.add(low)
            names.append(name)
        names.sort()
        return names

    def _refresh_combo(self, force: bool = False) -> None:
        new_names = self._street_names_from_segments()
        if not force and new_names == self._all_names:
            return
        self._all_names = new_names
        self._refresh_filtered_names()
        loading = getattr(self._nav, '_loading', False)
        n = len(new_names)
        if loading:
            self.SetTitle(f"Street Search — {n} streets, loading…")
        else:
            self.SetTitle(f"Street Search — {n} streets")
            self._timer.Stop()

    def _matching_street_names(self, query: str) -> list[str]:
        needle = query.strip().lower()
        if not needle:
            return list(self._all_names)
        matches = [
            name for name in self._all_names
            if needle in name.lower()
        ]
        def match_rank(name: str) -> tuple[int, str]:
            haystack = name.lower()
            words = re.findall(r"[a-z0-9]+", haystack)
            if haystack == needle:
                rank = 0
            elif haystack.startswith(needle):
                rank = 1
            elif any(word.startswith(needle) for word in words):
                rank = 2
            else:
                rank = 3
            return rank, haystack
        matches.sort(key=match_rank)
        return matches

    def _refresh_filtered_names(self) -> None:
        query = self._search.GetValue().strip().lower()
        old_selection = ""
        idx = self._list.GetSelection()
        if 0 <= idx < len(self._filtered_names):
            old_selection = self._filtered_names[idx]
        self._filtered_names = self._matching_street_names(query)
        self._list.Set(self._filtered_names)
        if self._filtered_names:
            if old_selection in self._filtered_names:
                self._list.SetSelection(self._filtered_names.index(old_selection))
            else:
                self._list.SetSelection(0)
        self._last_filter_query = query

    def _on_search_text(self, event) -> None:
        self._selected_street_name = ""
        self._refresh_filtered_names()
        event.Skip()

    def _on_search_key(self, event) -> None:
        code = event.GetKeyCode()
        if code in (wx.WXK_DOWN, wx.WXK_UP) and self._filtered_names:
            self._list.SetFocus()
            idx = 0 if code == wx.WXK_DOWN else len(self._filtered_names) - 1
            self._list.SetSelection(idx)
            self._selected_street_name = self._filtered_names[idx]
            return
        event.Skip()

    def _sync_selected_from_list(self) -> None:
        idx = self._list.GetSelection()
        if 0 <= idx < len(self._filtered_names):
            self._selected_street_name = self._filtered_names[idx]

    def _on_list_select(self, event) -> None:
        self._sync_selected_from_list()
        event.Skip()

    def _on_list_key(self, event) -> None:
        code = event.GetKeyCode()
        if code in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            self._sync_selected_from_list()
            self._on_jump(event)
            return
        if code in (wx.WXK_BACK, wx.WXK_DELETE):
            self._search.SetFocus()
            return
        event.Skip()
        wx.CallAfter(self._sync_selected_from_list)

    def _on_timer(self, event) -> None:
        self._refresh_combo()

    def _on_jump(self, event) -> None:
        query = self._search.GetValue().strip()
        house_number = self._num.GetValue().strip()
        matches = self._matching_street_names(query)
        if query and not matches:
            self._nav._status_update(f"No street matching {query}.", force=True)
            self._search.SetFocus()
            return
        sel = ""
        if self._selected_street_name in matches:
            sel = self._selected_street_name.strip()
        if query and not sel:
            sel = matches[0].strip()
        elif not sel and matches:
            sel = matches[0].strip()
        if not sel:
            sel = query
        if house_number and not query and self.FindFocus() != self._list:
            self._nav._status_update("Type or select a street before entering a number.", force=True)
            self._search.SetFocus()
            return
        if not sel:
            return
        nav = self._nav
        preview = matches[:5]
        miab_log(
            "snap",
            f"street search jump: query={query!r} selected={sel!r} house_number={house_number!r} matches={preview!r}",
            getattr(nav, "settings", None),
        )
        self._timer.Stop()
        nav._street_search_dlg = None
        nav._suppress_status_until = time.time() + 4.0
        nav._jump_to_street(sel, house_number=house_number)
        self.Hide()
        self.Destroy()
        nav._repeat_current_location_after_return(350)

    def _on_close(self, event) -> None:
        self._timer.Stop()
        self._nav._street_search_dlg = None
        self.Hide()
        self.Destroy()
        self._nav._focus_map_window_silently()

    def _on_char_hook(self, event) -> None:
        code    = event.GetKeyCode()
        focused = self.FindFocus()
        if code == wx.WXK_ESCAPE:
            self._nav._repeat_current_location_after_return()
            self._on_close(None)
            event.StopPropagation()
            return
        if code in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            if focused == self._cancel_btn:
                self._on_close(None)
            else:
                self._on_jump(None)
            event.StopPropagation()
            return
        event.Skip()
        event.StopPropagation()
