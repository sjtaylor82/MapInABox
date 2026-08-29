"""Simple sighted/keyboard authoring dialogs for user-created maps."""

from __future__ import annotations

import math
import os

import wx

import user_maps


class MapDrawCanvas(wx.Panel):
    def __init__(self, parent, data, background=""):
        super().__init__(parent, style=wx.BORDER_SIMPLE | wx.WANTS_CHARS)
        self.data = data
        self.background_path = background
        self.background = wx.Bitmap(background) if background and os.path.exists(background) else None
        # Opening or importing a map must not make an accidental mouse action
        # draw on it.  The creator explicitly selects a drawing tool first.
        self.tool = "none"
        self.stroke = []
        self.history = []
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.SetMinSize((640, 420))
        self.Bind(wx.EVT_PAINT, self._paint)
        self.Bind(wx.EVT_LEFT_DOWN, self._down)
        self.Bind(wx.EVT_MOTION, self._motion)
        self.Bind(wx.EVT_LEFT_UP, self._up)
        self.Bind(wx.EVT_CHAR_HOOK, self._key)

    def _to_map(self, px, py):
        width, height = self.GetClientSize()
        x = max(0.0, min(self.data["width"], px / max(1, width) * self.data["width"]))
        y = max(0.0, min(self.data["height"], (height - py) / max(1, height) * self.data["height"]))
        return x, y

    def _to_px(self, x, y):
        width, height = self.GetClientSize()
        return x / self.data["width"] * width, height - y / self.data["height"] * height

    def set_tool(self, tool):
        self.tool = tool
        self.SetFocus()

    def set_floor(self, data, background=""):
        self.data = data
        self.background_path = background
        self.background = wx.Bitmap(background) if background and os.path.exists(background) else None
        self.stroke = []
        self.history = []
        self.Refresh()

    def _down(self, event):
        if self.tool == "none":
            return
        if self.tool == "place":
            self._add_place(*self._to_map(event.GetX(), event.GetY()))
            return
        self.CaptureMouse()
        self.stroke = [self._to_map(event.GetX(), event.GetY())]

    def _motion(self, event):
        if not event.Dragging() or not event.LeftIsDown() or not self.stroke:
            return
        point = self._to_map(event.GetX(), event.GetY())
        if math.hypot(point[0] - self.stroke[-1][0], point[1] - self.stroke[-1][1]) >= 2.0:
            self.stroke.append(point)
            self.Refresh()

    def _up(self, event):
        if self.HasCapture():
            self.ReleaseMouse()
        if len(self.stroke) >= 2:
            clean = user_maps.simplify_points(self.stroke)
            if self.tool == "barrier":
                self.data.setdefault("barriers", []).append(
                    {"points": [[x, y] for x, y in clean]})
                self.history.append("barrier")
            else:
                clean = user_maps.snap_drawn_endpoints(self.data["paths"], clean)
                self.data["paths"].append({"points": [[x, y] for x, y in clean]})
                self.history.append("path")
        self.stroke = []
        self.Refresh()

    def _add_place(self, x, y):
        dialog = wx.TextEntryDialog(self, "What is this place called?", "Add a place")
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            name = dialog.GetValue().strip()
        finally:
            dialog.Destroy()
        if not name:
            return
        place_id = f"place-{len(self.data['places']) + 1}"
        self.data["places"].append({
            "id": place_id, "name": name, "description": "", "x": x, "y": y,
        })
        self.history.append("place")
        if not self.data.get("start"):
            self.data["start"] = place_id
        self.Refresh()

    def _key(self, event):
        key = event.GetKeyCode()
        if key in (ord("P"), ord("p")):
            self.tool = "path"
            wx.GetTopLevelParent(self).SetStatusText("Draw path selected. Drag across the map.")
            return
        if key in (ord("L"), ord("l")):
            self.tool = "place"
            wx.GetTopLevelParent(self).SetStatusText("Add place selected. Click its position.")
            return
        if key in (ord("B"), ord("b")):
            self.tool = "barrier"
            wx.GetTopLevelParent(self).SetStatusText(
                "Draw barrier selected. Drag along a wall or boundary.")
            return
        if event.ControlDown() and key in (ord("Z"), ord("z")):
            self.undo()
            return
        event.Skip()

    def undo(self):
        if not self.history:
            wx.GetTopLevelParent(self).SetStatusText("Nothing to undo.")
            return
        kind = self.history.pop()
        if kind == "path":
            collection = self.data["paths"]
        elif kind == "barrier":
            collection = self.data.setdefault("barriers", [])
        else:
            collection = self.data["places"]
        if collection:
            removed = collection.pop()
            if kind == "place" and removed.get("id") == self.data.get("start"):
                self.data["start"] = self.data["places"][0]["id"] if self.data["places"] else ""
        self.Refresh()
        wx.GetTopLevelParent(self).SetStatusText("Last action undone.")

    def _paint(self, event):
        dc = wx.AutoBufferedPaintDC(self)
        width, height = self.GetClientSize()
        dc.SetBackground(wx.Brush(wx.Colour(248, 248, 244)))
        dc.Clear()
        if self.background and self.background.IsOk():
            image = self.background.ConvertToImage().Scale(max(1, width), max(1, height), wx.IMAGE_QUALITY_HIGH)
            dc.DrawBitmap(wx.Bitmap(image), 0, 0)
        dc.SetPen(wx.Pen(wx.Colour(20, 85, 155), 5, wx.PENSTYLE_SOLID))
        for path in self.data["paths"]:
            points = [self._to_px(*point) for point in path["points"]]
            for first, second in zip(points, points[1:]):
                dc.DrawLine(round(first[0]), round(first[1]), round(second[0]), round(second[1]))
        dc.SetPen(wx.Pen(wx.Colour(190, 45, 35), 4, wx.PENSTYLE_SHORT_DASH))
        for barrier in self.data.get("barriers", []):
            points = [self._to_px(*point) for point in barrier["points"]]
            for first, second in zip(points, points[1:]):
                dc.DrawLine(round(first[0]), round(first[1]), round(second[0]), round(second[1]))
        if len(self.stroke) >= 2:
            points = [self._to_px(*point) for point in self.stroke]
            for first, second in zip(points, points[1:]):
                dc.DrawLine(round(first[0]), round(first[1]), round(second[0]), round(second[1]))
        dc.SetFont(wx.Font(10, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        for place in self.data["places"]:
            px, py = self._to_px(place["x"], place["y"])
            dc.SetBrush(wx.Brush(wx.Colour(235, 75, 45)))
            dc.SetPen(wx.Pen(wx.Colour(80, 20, 10), 1))
            dc.DrawCircle(round(px), round(py), 6)
            dc.SetTextForeground(wx.Colour(15, 15, 15))
            dc.DrawText(place["name"], round(px + 9), round(py - 8))


class MapDrawerDialog(wx.Dialog):
    def __init__(self, parent, data, background="", save_path="",
                 suggested_save_path=""):
        super().__init__(parent, title="Create Map File",
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.data = data
        self.floors = user_maps.floors_for(data)
        if isinstance(background, (list, tuple)):
            self.backgrounds = list(background)
        else:
            self.backgrounds = [background]
        self.backgrounds.extend([""] * (len(self.floors) - len(self.backgrounds)))
        self.floor_index = 0
        self.background = self.backgrounds[0]
        self.save_path = save_path
        self.suggested_save_path = suggested_save_path
        panel = wx.Panel(self)
        outer = wx.BoxSizer(wx.VERTICAL)
        help_text = wx.StaticText(panel, label=(
            "Add the places students need to find. Paths are optional for directions. "
            "Draw barriers when crossing a wall should make a sound. "
            "Shortcuts: P paths; L places; B barriers."))
        help_text.Wrap(760)
        outer.Add(help_text, 0, wx.ALL | wx.EXPAND, 10)

        if len(self.floors) > 1:
            floor_row = wx.BoxSizer(wx.HORIZONTAL)
            floor_row.Add(wx.StaticText(panel, label="Floor:"), 0,
                          wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
            self.floor_choice = wx.Choice(
                panel, choices=[floor.get("name", f"Floor {i + 1}")
                                for i, floor in enumerate(self.floors)])
            self.floor_choice.SetSelection(0)
            floor_row.Add(self.floor_choice, 1, wx.EXPAND)
            outer.Add(floor_row, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)
        else:
            self.floor_choice = None

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        path_button = wx.Button(panel, label="Draw a path")
        place_button = wx.Button(panel, label="Add a place")
        barrier_button = wx.Button(panel, label="Draw a barrier")
        test_button = wx.Button(panel, label="Test a route")
        undo_button = wx.Button(panel, label="Undo")
        save_button = wx.Button(panel, wx.ID_SAVE, "Save map")
        close_button = wx.Button(panel, wx.ID_CANCEL, "Close")
        for button in (path_button, place_button, barrier_button, undo_button, test_button, save_button, close_button):
            buttons.Add(button, 0, wx.RIGHT, 8)
        outer.Add(buttons, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        self.canvas = MapDrawCanvas(panel, self.floors[0], self.backgrounds[0])
        outer.Add(self.canvas, 1, wx.LEFT | wx.RIGHT | wx.EXPAND, 10)
        label_count = sum(len(floor.get("places", [])) for floor in self.floors)
        self.status = wx.StaticText(
            panel, label=f"Map ready. {label_count} labels available. Select a tool only if editing is needed.")
        outer.Add(self.status, 0, wx.ALL | wx.EXPAND, 10)
        panel.SetSizer(outer)
        self.SetSize((860, 650))
        path_button.Bind(wx.EVT_BUTTON, lambda e: self._select("path"))
        place_button.Bind(wx.EVT_BUTTON, lambda e: self._select("place"))
        barrier_button.Bind(wx.EVT_BUTTON, lambda e: self._select("barrier"))
        test_button.Bind(wx.EVT_BUTTON, self._test)
        undo_button.Bind(wx.EVT_BUTTON, lambda e: self.canvas.undo())
        save_button.Bind(wx.EVT_BUTTON, self._save)
        close_button.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CANCEL))
        if self.floor_choice:
            self.floor_choice.Bind(wx.EVT_CHOICE, self._change_floor)
        self.CentreOnParent()

    def SetStatusText(self, text):
        self.status.SetLabel(text)

    def _change_floor(self, event=None):
        self.floor_index = self.floor_choice.GetSelection()
        self.background = self.backgrounds[self.floor_index]
        self.canvas.set_floor(self.floors[self.floor_index], self.background)
        self.SetStatusText(
            f"{self.floors[self.floor_index]['name']}. "
            f"{len(self.floors[self.floor_index].get('places', []))} labels.")

    def _select(self, tool):
        self.canvas.set_tool(tool)
        messages = {
            "path": "Draw path selected. Drag across the map.",
            "place": "Add place selected. Click its position.",
            "barrier": "Draw barrier selected. Drag along a wall or boundary.",
        }
        self.SetStatusText(messages[tool])

    def _test(self, event=None):
        floor = self.floors[self.floor_index]
        if len(floor["places"]) < 2:
            wx.MessageBox("Add at least two places before testing a route.",
                          "Test route", wx.OK | wx.ICON_INFORMATION, self)
            return
        dialog = LocalRouteDialog(self, floor, allow_explore=False)
        dialog.ShowModal()
        dialog.Destroy()

    def _save(self, event=None):
        path = self.save_path
        if not path:
            default_dir = os.path.dirname(self.suggested_save_path)
            default_file = os.path.basename(self.suggested_save_path)
            dialog = wx.FileDialog(self, "Save map file", wildcard="Map in a Box maps (*.miabmap)|*.miabmap",
                                   defaultDir=default_dir, defaultFile=default_file,
                                   style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT)
            try:
                if dialog.ShowModal() != wx.ID_OK:
                    return
                path = dialog.GetPath()
            finally:
                dialog.Destroy()
        try:
            backgrounds = self.backgrounds if "floors" in self.data else (self.background or None)
            self.save_path = user_maps.save_map(path, self.data, backgrounds)
        except Exception as exc:
            wx.MessageBox(f"The map could not be saved.\n\n{exc}", "Save map",
                          wx.OK | wx.ICON_ERROR, self)
            return
        self.SetStatusText(f"Saved {os.path.basename(self.save_path)}.")
        self.EndModal(wx.ID_OK)


class LocalRouteDialog(wx.Dialog):
    def __init__(self, parent, data, allow_explore=True):
        super().__init__(parent, title="Directions between places",
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.data = data
        self.route = None
        self.allow_explore = allow_explore
        places = data.get("places") or []
        names = [p["name"] for p in places]
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)
        grid = wx.FlexGridSizer(2, 2, 8, 8)
        grid.Add(wx.StaticText(panel, label="From:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.origin = wx.ComboBox(panel, choices=names, style=wx.CB_READONLY)
        grid.Add(self.origin, 1, wx.EXPAND)
        grid.Add(wx.StaticText(panel, label="To:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.destination = wx.ComboBox(panel, choices=names, style=wx.CB_READONLY)
        grid.Add(self.destination, 1, wx.EXPAND)
        grid.AddGrowableCol(1, 1)
        sizer.Add(grid, 0, wx.ALL | wx.EXPAND, 10)
        self.result = wx.TextCtrl(panel, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP)
        self.result.SetMinSize((560, 190))
        sizer.Add(self.result, 1, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)
        row = wx.BoxSizer(wx.HORIZONTAL)
        find_button = wx.Button(panel, label="Find route")
        self.explore_button = wx.Button(panel, label="Explore route")
        self.explore_button.Enable(False)
        close_button = wx.Button(panel, wx.ID_CLOSE, "Close")
        for button in (find_button, self.explore_button, close_button):
            row.Add(button, 0, wx.RIGHT, 8)
        sizer.Add(row, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        panel.SetSizer(sizer)
        self.SetSize((640, 390))
        if names:
            self.origin.SetSelection(0)
        if len(names) > 1:
            self.destination.SetSelection(1)
        find_button.Bind(wx.EVT_BUTTON, self._find)
        self.explore_button.Bind(wx.EVT_BUTTON, self._explore)
        close_button.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CLOSE))
        self.CentreOnParent()
        wx.CallAfter(self.origin.SetFocus)

    def _find(self, event=None):
        oi, di = self.origin.GetSelection(), self.destination.GetSelection()
        places = self.data.get("places") or []
        if oi < 0 or di < 0 or oi == di:
            self.result.SetValue("Choose two different places.")
            return
        try:
            self.route = user_maps.find_route(self.data, places[oi]["id"], places[di]["id"])
            direct = math.hypot(places[di]["x"] - places[oi]["x"], places[di]["y"] - places[oi]["y"])
            direction = user_maps.compass_name(places[di]["x"] - places[oi]["x"], places[di]["y"] - places[oi]["y"])
            lines = [f"{places[di]['name']} is about {direct:.0f} metres {direction} as the crow flies.",
                     f"The drawn route is about {self.route.distance:.0f} metres.", ""]
            lines.extend(user_maps.route_directions(self.route))
            self.result.SetValue("\n".join(lines))
            self.result.SetInsertionPoint(0)
            self.explore_button.Enable(self.allow_explore)
        except ValueError as exc:
            self.route = None
            self.explore_button.Enable(False)
            self.result.SetValue(str(exc))

    def _explore(self, event=None):
        if not self.route:
            return
        from dialogs import ExplorePathDialog
        points = [
            {"lat": y / 111195.0, "lon": x / 111195.0,
             "instruction": "", "maneuver": ""}
            for x, y in self.route.points
        ]
        route_data = {
            "travel_mode": "walking",
            "legs": [{"type": "walking", "_walk_path_points": points}],
            "_journey_origin": {"name": self.route.origin["name"]},
            "_journey_destination": {"name": self.route.destination["name"]},
        }
        dialog = ExplorePathDialog(self, route_data)
        dialog.ShowModal()
        dialog.Destroy()
