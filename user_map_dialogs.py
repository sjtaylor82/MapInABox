"""Map builder for teachers, usable by keyboard and screen reader or by mouse.

Places are the heart of a map.  A blind teacher adds each one by name, then
says where it is: "20 metres north-east of the Library".  The builder joins it
to that place with a path, so directions work without drawing anything.  A
sighted teacher can do the same, or trace over a picture of the area: click to
put a place, drag to draw a path or a wall.  Everything appears in two lists,
places and paths, that can be renamed, moved, described or deleted, and every
change can be undone.
"""

from __future__ import annotations

import copy
import math
import os

import wx

import user_maps
from distance_units import get_unit_system


def _say(text):
    """Speak a builder confirmation through the screen reader."""
    try:
        from speech_dispatch import speak
        speak(text)
    except Exception:
        pass


def _finish_dialog(dialog, panel, sizer):
    """Add OK and Cancel to ``sizer`` on ``panel`` and size ``dialog`` to fit."""
    buttons = wx.StdDialogButtonSizer()
    ok = wx.Button(panel, wx.ID_OK)
    ok.SetDefault()
    buttons.AddButton(ok)
    buttons.AddButton(wx.Button(panel, wx.ID_CANCEL))
    buttons.Realize()
    sizer.Add(buttons, 0, wx.ALL | wx.EXPAND, 10)
    panel.SetSizer(sizer)
    outer = wx.BoxSizer(wx.VERTICAL)
    outer.Add(panel, 1, wx.EXPAND)
    dialog.SetSizerAndFit(outer)


class MapDrawCanvas(wx.Panel):
    """The visual map: shows the picture, places and paths, and takes mouse input.

    The picture keeps its proportions; the map is drawn in a centred box.
    """

    def __init__(self, parent, owner, floor, background=""):
        super().__init__(parent, style=wx.BORDER_SIMPLE)
        self.owner = owner
        self.data = floor
        self.background = self._load(background)
        # Opening or importing a map must not make an accidental mouse action
        # draw on it.  The creator explicitly selects a drawing tool first.
        self.tool = "none"
        self.stroke = []
        self.press = None          # (x, y, place under it) of a left press
        self.dragging = None       # a place being dragged to a new spot
        self.drag_to = None
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.SetMinSize((520, 300))
        self.Bind(wx.EVT_PAINT, self._paint)
        self.Bind(wx.EVT_LEFT_DOWN, self._down)
        self.Bind(wx.EVT_MOTION, self._motion)
        self.Bind(wx.EVT_LEFT_UP, self._up)
        self.Bind(wx.EVT_RIGHT_DOWN, self._right_down)
        self.Bind(wx.EVT_SIZE, lambda e: (self.Refresh(), e.Skip()))

    def AcceptsFocusFromKeyboard(self):
        # The drawing area is for the mouse only.  Taken by Tab, it held the
        # keyboard with nothing to say and no way out; Tab now goes from the
        # Mouse box straight to Check map.
        return False

    @staticmethod
    def _load(background):
        if background and os.path.exists(background):
            bitmap = wx.Bitmap(background)
            return bitmap if bitmap.IsOk() else None
        return None

    def set_floor(self, floor, background=""):
        self.data = floor
        self.background = self._load(background)
        self.stroke = []
        self.Refresh()

    def _box(self):
        """(left, top, width, height) in pixels of the map's drawing box."""
        width, height = self.GetClientSize()
        scale = min(width / self.data["width"], height / self.data["height"])
        box_w, box_h = self.data["width"] * scale, self.data["height"] * scale
        return (width - box_w) / 2, (height - box_h) / 2, box_w, box_h

    def _to_map(self, px, py):
        left, top, box_w, box_h = self._box()
        x = (px - left) / max(1.0, box_w) * self.data["width"]
        y = (top + box_h - py) / max(1.0, box_h) * self.data["height"]
        return (max(0.0, min(self.data["width"], x)),
                max(0.0, min(self.data["height"], y)))

    def _to_px(self, x, y):
        left, top, box_w, box_h = self._box()
        return (left + x / self.data["width"] * box_w,
                top + box_h - y / self.data["height"] * box_h)

    def set_tool(self, tool):
        self.tool = tool

    # How near, on screen, a click or a line's end must be to join something.
    JOIN_PIXELS = 20
    # A press that moves less than this is a click, not a drag.
    CLICK_PIXELS = 5
    # How near, on screen, a click must be to pick a place or a path.
    PICK_PIXELS = 10

    def join_distance(self):
        """JOIN_PIXELS in metres at the current drawing size."""
        _left, _top, box_w, _box_h = self._box()
        return self.JOIN_PIXELS * self.data["width"] / max(1.0, box_w)

    def aspect(self):
        """Height over width of the drawing area on screen."""
        width, height = self.GetClientSize()
        return max(0.3, min(3.0, height / max(1, width)))

    def place_at(self, px, py):
        """The place drawn under a pixel, or None."""
        best = None
        for place in self.data.get("places", []):
            qx, qy = self._to_px(place["x"], place["y"])
            distance = math.hypot(qx - px, qy - py)
            if distance <= self.PICK_PIXELS and (best is None or distance < best[0]):
                best = (distance, place)
        return best[1] if best else None

    def line_at(self, px, py):
        """("paths" or "barriers", index) of the line under a pixel, or None."""
        best = None
        for kind in ("paths", "barriers"):
            for index, feature in enumerate(self.data.get(kind, [])):
                pixels = [self._to_px(*point) for point in feature["points"]]
                for a, b in zip(pixels, pixels[1:]):
                    (qx, qy), _t = user_maps._project((px, py), a, b)
                    distance = math.hypot(qx - px, qy - py)
                    if distance <= self.PICK_PIXELS and (best is None or distance < best[0]):
                        best = (distance, kind, index)
        return best[1:] if best else None

    def _down(self, event):
        px, py = event.GetX(), event.GetY()
        place = self.place_at(px, py)
        if self.tool == "none":
            if place:
                self.owner.select_place(place["id"])
            return
        if not self.owner.ready_to_draw():
            return
        self.press = (px, py, place)
        if self.tool == "move":
            if place is None:
                return
            self.owner.select_place(place["id"])
            self.dragging = place
            self.drag_to = (place["x"], place["y"])
        self.stroke = [self._to_map(px, py)]
        self.CaptureMouse()

    def _motion(self, event):
        if not event.Dragging() or not event.LeftIsDown() or not self.stroke:
            return
        point = self._to_map(event.GetX(), event.GetY())
        if self.dragging is not None:
            self.drag_to = point
            self.Refresh()
            return
        if math.hypot(point[0] - self.stroke[-1][0], point[1] - self.stroke[-1][1]) >= self.data["width"] / 400:
            self.stroke.append(point)
            self.Refresh()

    def _up(self, event):
        if self.HasCapture():
            self.ReleaseMouse()
        stroke, self.stroke = self.stroke, []
        dragging, self.dragging = self.dragging, None
        press, self.press = self.press, None
        if press is None:
            self.Refresh()
            return
        moved = math.hypot(event.GetX() - press[0], event.GetY() - press[1]) >= self.CLICK_PIXELS
        if dragging is not None:
            if moved:
                self.owner.move_place_to(dragging["id"], *self._to_map(event.GetX(), event.GetY()))
        elif self.tool == "draw" and not moved:
            # A click: on a place it picks the place, elsewhere it adds one.
            if press[2] is not None:
                self.owner.select_place(press[2]["id"])
            else:
                self.owner.add_place_at(*self._to_map(press[0], press[1]))
        elif len(stroke) >= 2 and moved:
            self.owner.add_drawn_line("barrier" if self.tool == "barrier" else "path", stroke)
        self.Refresh()

    def _right_down(self, event):
        px, py = event.GetX(), event.GetY()
        place = self.place_at(px, py)
        menu = wx.Menu()
        if place is not None:
            self.owner.select_place(place["id"])
            for label, action in (("&Rename or describe...", self.owner.edit_place),
                                  ("&Delete place", self.owner.delete_place),
                                  ("Set as &start", self.owner.set_start)):
                item = menu.Append(wx.ID_ANY, label)
                self.Bind(wx.EVT_MENU, lambda e, act=action: act(), item)
        else:
            line = self.line_at(px, py)
            if line is None:
                menu.Destroy()
                return
            kind, index = line
            label = "&Delete this path" if kind == "paths" else "&Delete this wall"
            item = menu.Append(wx.ID_ANY, label)
            self.Bind(wx.EVT_MENU, lambda e: self.owner.delete_line(kind, index), item)
        self.PopupMenu(menu)
        menu.Destroy()

    def _grid_step(self):
        """A round distance in metres whose grid squares are 40 or more pixels."""
        _left, _top, box_w, _box_h = self._box()
        per_metre = box_w / max(1e-6, self.data["width"])
        for step in (0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000):
            if step * per_metre >= 40:
                return step
        return 5000

    def _paint_scale(self, dc, left, top, box_w, box_h):
        """A light grid on a blank drawing, and a scale bar on every map."""
        step = self._grid_step()
        if self.background is None:
            dc.SetPen(wx.Pen(wx.Colour(225, 228, 232), 1))
            x = step
            while x < self.data["width"]:
                px, _ = self._to_px(x, 0)
                dc.DrawLine(round(px), round(top), round(px), round(top + box_h))
                x += step
            y = step
            while y < self.data["height"]:
                _, py = self._to_px(0, y)
                dc.DrawLine(round(left), round(py), round(left + box_w), round(py))
                y += step
        length = step * box_w / max(1e-6, self.data["width"])
        x0, y0 = left + 12, top + box_h - 14
        dc.SetPen(wx.Pen(wx.Colour(40, 40, 40), 2))
        dc.DrawLine(round(x0), round(y0), round(x0 + length), round(y0))
        for x in (x0, x0 + length):
            dc.DrawLine(round(x), round(y0 - 4), round(x), round(y0 + 4))
        dc.SetFont(wx.Font(9, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        dc.SetTextForeground(wx.Colour(40, 40, 40))
        dc.DrawText(user_maps.format_map_distance(step, self.owner.unit_system),
                    round(x0 + length + 6), round(y0 - 8))

    def _line(self, dc, points):
        pixels = [self._to_px(*point) for point in points]
        for first, second in zip(pixels, pixels[1:]):
            dc.DrawLine(round(first[0]), round(first[1]), round(second[0]), round(second[1]))

    def _paint(self, event):
        dc = wx.AutoBufferedPaintDC(self)
        dc.SetBackground(wx.Brush(wx.Colour(225, 225, 220)))
        dc.Clear()
        left, top, box_w, box_h = self._box()
        dc.SetBrush(wx.Brush(wx.Colour(250, 250, 246)))
        dc.SetPen(wx.Pen(wx.Colour(160, 160, 150), 1))
        dc.DrawRectangle(round(left), round(top), round(box_w), round(box_h))
        if self.background is not None:
            image = self.background.ConvertToImage().Scale(
                max(1, round(box_w)), max(1, round(box_h)), wx.IMAGE_QUALITY_HIGH)
            dc.DrawBitmap(wx.Bitmap(image), round(left), round(top))
        self._paint_scale(dc, left, top, box_w, box_h)
        dc.SetPen(wx.Pen(wx.Colour(20, 85, 155), 5, wx.PENSTYLE_SOLID))
        for path in self.data.get("paths", []):
            self._line(dc, path["points"])
        dc.SetPen(wx.Pen(wx.Colour(190, 45, 35), 4, wx.PENSTYLE_SHORT_DASH))
        for barrier in self.data.get("barriers", []):
            self._line(dc, barrier["points"])
        if len(self.stroke) >= 2 and self.dragging is None:
            dc.SetPen(wx.Pen(wx.Colour(60, 60, 60), 3, wx.PENSTYLE_SOLID))
            self._line(dc, self.stroke)
            # The length so far, beside the pointer.
            end_x, end_y = self._to_px(*self.stroke[-1])
            dc.SetFont(wx.Font(10, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
            dc.SetTextForeground(wx.Colour(20, 20, 20))
            dc.DrawText(user_maps.format_map_distance(
                user_maps.path_length({"points": self.stroke}), self.owner.unit_system),
                round(end_x + 12), round(end_y + 6))
        # Places a route cannot reach are drawn hollow, so gaps show at a glance.
        _coords, _edges, place_nodes = user_maps.build_graph(self.data)
        selected = self.owner.selected_place_id()
        dc.SetFont(wx.Font(10, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        for place in self.data.get("places", []):
            if self.dragging is not None and place["id"] == self.dragging["id"]:
                px, py = self._to_px(*self.drag_to)
            else:
                px, py = self._to_px(place["x"], place["y"])
            on_path = place["id"] in place_nodes
            dc.SetBrush(wx.Brush(wx.Colour(235, 75, 45)) if on_path else wx.TRANSPARENT_BRUSH)
            dc.SetPen(wx.Pen(wx.Colour(0, 0, 0) if place["id"] == selected
                             else wx.Colour(80, 20, 10), 3 if place["id"] == selected else 2))
            dc.DrawCircle(round(px), round(py), 7)
            dc.SetTextForeground(wx.Colour(15, 15, 15))
            label = place["name"] + (" (start)" if place["id"] == self.data.get("start") else "")
            # A pale backing keeps names readable where they cross a path.
            text_w, text_h = dc.GetTextExtent(label)
            dc.SetPen(wx.TRANSPARENT_PEN)
            dc.SetBrush(wx.Brush(wx.Colour(250, 250, 246, 215)))
            dc.DrawRectangle(round(px + 8), round(py - 9), text_w + 4, text_h + 2)
            dc.DrawText(label, round(px + 10), round(py - 8))


class PlaceDialog(wx.Dialog):
    """Name a place and, unless clicked, say where it is.

    mode "add": name, description and position; "edit": name and
    description; "move": position only.  Position is a distance and compass
    direction from another place on the floor.
    """

    def __init__(self, parent, floor, unit_system, mode="add", place=None,
                 position_known=False):
        title = {"add": "Add a place", "edit": "Edit place", "move": "Move place"}[mode]
        super().__init__(parent, title=title, style=wx.DEFAULT_DIALOG_STYLE)
        self.floor, self.unit_system, self.mode, self.place = floor, unit_system, mode, place
        self.result = None
        others = [p for p in floor.get("places", []) if place is None or p["id"] != place["id"]]
        self._others = others
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.name = self.description = None
        if mode in ("add", "edit"):
            sizer.Add(wx.StaticText(panel, label="Name:"), 0, wx.LEFT | wx.TOP, 10)
            self.name = wx.TextCtrl(panel, value=place["name"] if place else "")
            sizer.Add(self.name, 0, wx.ALL | wx.EXPAND, 10)
            sizer.Add(wx.StaticText(
                panel, label="Description, read to students when they arrive (optional):"),
                0, wx.LEFT, 10)
            self.description = wx.TextCtrl(panel, value=place.get("description", "") if place else "")
            sizer.Add(self.description, 0, wx.ALL | wx.EXPAND, 10)
        self.reference = self.distance = self.direction = self.connect = None
        if mode == "move" or (mode == "add" and not position_known):
            if others:
                unit = user_maps.unit_label(unit_system)
                sizer.Add(wx.StaticText(panel, label="Measure from:"), 0, wx.LEFT, 10)
                self.reference = wx.Choice(panel, choices=[p["name"] for p in others])
                self.reference.SetSelection(len(others) - 1)
                sizer.Add(self.reference, 0, wx.ALL | wx.EXPAND, 10)
                sizer.Add(wx.StaticText(
                    panel, label=f"Distance in {unit} (for example 12, or 6 feet 4 inches):"),
                    0, wx.LEFT, 10)
                self.distance = wx.TextCtrl(panel)
                sizer.Add(self.distance, 0, wx.ALL | wx.EXPAND, 10)
                sizer.Add(wx.StaticText(panel, label="Direction:"), 0, wx.LEFT, 10)
                self.direction = wx.Choice(panel, choices=list(user_maps.COMPASS_DIRECTIONS))
                self.direction.SetSelection(0)
                sizer.Add(self.direction, 0, wx.ALL | wx.EXPAND, 10)
                if mode == "add":
                    self.connect = wx.CheckBox(
                        panel, label="Join it to that place with a path, so directions can reach it")
                    self.connect.SetValue(True)
                    sizer.Add(self.connect, 0, wx.ALL, 10)
            elif mode == "add":
                sizer.Add(wx.StaticText(panel, label=(
                    "This is the first place. Each place you add next is placed by "
                    "distance and direction from one already on the map.")),
                    0, wx.ALL, 10)
        _finish_dialog(self, panel, sizer)
        self.Bind(wx.EVT_BUTTON, self._ok, id=wx.ID_OK)
        first = self.name or self.reference or self.distance
        if first is not None:
            wx.CallAfter(first.SetFocus)
        self.CentreOnParent()

    def _ok(self, event):
        result = {}
        if self.name is not None:
            name = self.name.GetValue().strip()
            if not name:
                wx.MessageBox("Give the place a name.", self.GetTitle(), wx.OK, self)
                self.name.SetFocus()
                return
            result["name"] = name
            result["description"] = self.description.GetValue().strip()
        if self.distance is not None:
            metres = user_maps.parse_distance(self.distance.GetValue(), self.unit_system)
            if metres is None:
                wx.MessageBox(
                    f"Type a distance in {user_maps.unit_label(self.unit_system)}, "
                    "for example 12, 12.5 or 6 feet 4 inches.",
                    self.GetTitle(), wx.OK, self)
                self.distance.SetFocus()
                return
            result["reference"] = self._others[self.reference.GetSelection()]["id"]
            result["distance"] = metres
            result["direction"] = user_maps.COMPASS_DIRECTIONS[self.direction.GetSelection()]
            result["connect"] = bool(self.connect and self.connect.GetValue())
        self.result = result
        self.EndModal(wx.ID_OK)


class ConnectDialog(wx.Dialog):
    """Choose two places to join with a straight path."""

    def __init__(self, parent, places, first_index=0):
        super().__init__(parent, title="Join two places with a path")
        self.result = None
        names = [p["name"] for p in places]
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(wx.StaticText(panel, label="From:"), 0, wx.LEFT | wx.TOP, 10)
        self.first = wx.Choice(panel, choices=names)
        self.first.SetSelection(max(0, first_index))
        sizer.Add(self.first, 0, wx.ALL | wx.EXPAND, 10)
        sizer.Add(wx.StaticText(panel, label="To:"), 0, wx.LEFT, 10)
        self.second = wx.Choice(panel, choices=names)
        self.second.SetSelection(0 if first_index else min(1, len(names) - 1))
        sizer.Add(self.second, 0, wx.ALL | wx.EXPAND, 10)
        _finish_dialog(self, panel, sizer)
        self.places = places
        self.Bind(wx.EVT_BUTTON, self._ok, id=wx.ID_OK)
        wx.CallAfter(self.first.SetFocus)
        self.CentreOnParent()

    def _ok(self, event):
        if self.first.GetSelection() == self.second.GetSelection():
            wx.MessageBox("Choose two different places.", self.GetTitle(), wx.OK, self)
            return
        self.result = (self.places[self.first.GetSelection()]["id"],
                       self.places[self.second.GetSelection()]["id"])
        self.EndModal(wx.ID_OK)


class LinkDialog(wx.Dialog):
    """Join a place on this floor to one on another floor by stairs, lift..."""

    NEW_PLACE = "A new place at the same spot"

    def __init__(self, parent, data, floor_index):
        super().__init__(parent, title="Add stairs or a lift")
        self.data, self.floor_index, self.result = data, floor_index, None
        floors = user_maps.floors_for(data)
        self.here = floors[floor_index].get("places", [])
        self.other_floors = [i for i in range(len(floors)) if i != floor_index]
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(wx.StaticText(panel, label="What joins the floors:"), 0, wx.LEFT | wx.TOP, 10)
        self.kind = wx.Choice(panel, choices=[k.capitalize() for k in user_maps.LINK_KINDS])
        self.kind.SetSelection(0)
        sizer.Add(self.kind, 0, wx.ALL | wx.EXPAND, 10)
        sizer.Add(wx.StaticText(
            panel, label=f"Where it is on {user_maps.floor_name(data, floor_index)}:"), 0, wx.LEFT, 10)
        self.place = wx.Choice(panel, choices=[p["name"] for p in self.here])
        self.place.SetSelection(max(0, parent.place_list.GetSelection()))
        sizer.Add(self.place, 0, wx.ALL | wx.EXPAND, 10)
        sizer.Add(wx.StaticText(panel, label="Floor it leads to:"), 0, wx.LEFT, 10)
        self.floor = wx.Choice(panel, choices=[user_maps.floor_name(data, i) for i in self.other_floors])
        self.floor.SetSelection(0)
        sizer.Add(self.floor, 0, wx.ALL | wx.EXPAND, 10)
        sizer.Add(wx.StaticText(panel, label="Where it arrives on that floor:"), 0, wx.LEFT, 10)
        self.arrival = wx.Choice(panel)
        sizer.Add(self.arrival, 0, wx.ALL | wx.EXPAND, 10)
        self._fill_arrivals()
        _finish_dialog(self, panel, sizer)
        self.floor.Bind(wx.EVT_CHOICE, lambda e: self._fill_arrivals())
        self.Bind(wx.EVT_BUTTON, self._ok, id=wx.ID_OK)
        wx.CallAfter(self.kind.SetFocus)
        self.CentreOnParent()

    def _there(self):
        floors = user_maps.floors_for(self.data)
        return floors[self.other_floors[self.floor.GetSelection()]].get("places", [])

    def _fill_arrivals(self):
        self.arrival.SetItems([self.NEW_PLACE] + [p["name"] for p in self._there()])
        self.arrival.SetSelection(0)

    def _ok(self, event):
        if not self.here:
            wx.MessageBox("Add a place on this floor first.", self.GetTitle(), wx.OK, self)
            return
        arrival = self.arrival.GetSelection()
        self.result = {
            "kind": user_maps.LINK_KINDS[self.kind.GetSelection()],
            "place": self.here[self.place.GetSelection()]["id"],
            "floor": self.other_floors[self.floor.GetSelection()],
            "arrival": None if arrival <= 0 else self._there()[arrival - 1]["id"],
        }
        self.EndModal(wx.ID_OK)


class MapDrawerDialog(wx.Dialog):
    def __init__(self, parent, data, background="", save_path="",
                 suggested_save_path="", traced=None, notice=""):
        super().__init__(parent, title=f"Map builder: {data.get('name', 'Untitled map')}",
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.MAXIMIZE_BOX)
        self.data = data
        self.unit_system = get_unit_system()
        self.floors = user_maps.floors_for(data)
        if isinstance(background, (list, tuple)):
            self.backgrounds = list(background)
        else:
            self.backgrounds = [background]
        self.backgrounds.extend([""] * (len(self.floors) - len(self.backgrounds)))
        # Floors traced over a picture keep the picture's coordinates; the
        # others grow and shrink to fit what is on them.
        self.traced = set(traced if traced is not None
                          else [i for i, b in enumerate(self.backgrounds) if b])
        self.floor_index = 0
        self.save_path = save_path
        self.suggested_save_path = suggested_save_path
        self.undo_stack = []
        self.dirty = False

        panel = wx.Panel(self)
        outer = wx.BoxSizer(wx.VERTICAL)
        intro = wx.StaticText(panel, label=(
            "Add the places students need to find. With the keyboard, choose Add place: "
            "each new place is set by distance and direction from one already on the map "
            "and joined to it with a path, so directions work straight away. With a mouse, "
            "choose Draw in the Mouse box, click to add places and drag to draw the paths "
            "between them; right-click a place or path to change it. Distances are in "
            + user_maps.unit_label(self.unit_system)
            + "; change this under Settings, Distance units."))
        intro.Wrap(820)
        outer.Add(intro, 0, wx.ALL | wx.EXPAND, 10)
        if notice:
            note = wx.StaticText(panel, label=notice)
            note.Wrap(820)
            outer.Add(note, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        floor_row = wx.BoxSizer(wx.HORIZONTAL)
        floor_row.Add(wx.StaticText(panel, label="Floor:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.floor_choice = wx.Choice(panel)
        floor_row.Add(self.floor_choice, 1, wx.RIGHT, 8)
        add_floor = wx.Button(panel, label="Add a floor")
        floor_row.Add(add_floor, 0)
        outer.Add(floor_row, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        lists = wx.BoxSizer(wx.HORIZONTAL)
        place_box = wx.StaticBoxSizer(wx.VERTICAL, panel, "Places")
        self.place_list = wx.ListBox(place_box.GetStaticBox(), size=(380, 150))
        place_box.Add(self.place_list, 1, wx.ALL | wx.EXPAND, 5)
        place_buttons = wx.WrapSizer(wx.HORIZONTAL)
        box = place_box.GetStaticBox()
        b_add = wx.Button(box, label="&Add place")
        b_edit = wx.Button(box, label="&Edit name or description")
        b_move = wx.Button(box, label="&Move")
        b_delete = wx.Button(box, label="&Delete place")
        b_start = wx.Button(box, label="Set as s&tart")
        for button in (b_add, b_edit, b_move, b_delete, b_start):
            place_buttons.Add(button, 0, wx.ALL, 3)
        place_box.Add(place_buttons, 0, wx.EXPAND)
        lists.Add(place_box, 1, wx.RIGHT | wx.EXPAND, 8)

        path_box = wx.StaticBoxSizer(wx.VERTICAL, panel, "Paths")
        self.path_list = wx.ListBox(path_box.GetStaticBox(), size=(300, 150))
        path_box.Add(self.path_list, 1, wx.ALL | wx.EXPAND, 5)
        path_buttons = wx.WrapSizer(wx.HORIZONTAL)
        pbox = path_box.GetStaticBox()
        b_connect = wx.Button(pbox, label="&Join two places")
        b_delete_path = wx.Button(pbox, label="Delete pat&h")
        b_auto = wx.Button(pbox, label="Add paths a&utomatically")
        for button in (b_connect, b_delete_path, b_auto):
            path_buttons.Add(button, 0, wx.ALL, 3)
        path_box.Add(path_buttons, 0, wx.EXPAND)
        lists.Add(path_box, 1, wx.EXPAND)
        outer.Add(lists, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 10)

        link_box = wx.StaticBoxSizer(wx.HORIZONTAL, panel, "Stairs and lifts between floors")
        lbox = link_box.GetStaticBox()
        self.link_list = wx.ListBox(lbox, size=(500, 60))
        link_box.Add(self.link_list, 1, wx.ALL | wx.EXPAND, 5)
        b_add_link = wx.Button(lbox, label="Add stairs or &lift")
        b_delete_link = wx.Button(lbox, label="Delete stairs or lift")
        link_box.Add(b_add_link, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 3)
        link_box.Add(b_delete_link, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 3)
        outer.Add(link_box, 0, wx.LEFT | wx.RIGHT | wx.TOP | wx.EXPAND, 10)

        tools = wx.BoxSizer(wx.HORIZONTAL)
        tools.Add(wx.StaticText(panel, label="Mouse:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.tool_choice = wx.Choice(panel, choices=[
            "Look only", "Draw: click to add a place, drag to draw a path",
            "Move: drag a place to a new spot", "Walls: drag to draw a wall or barrier"])
        self.tool_choice.SetSelection(0)
        tools.Add(self.tool_choice, 0)
        outer.Add(tools, 0, wx.ALL, 10)
        self.canvas = MapDrawCanvas(panel, self, self.floors[0], self.backgrounds[0])
        outer.Add(self.canvas, 1, wx.LEFT | wx.RIGHT | wx.EXPAND, 10)

        actions = wx.BoxSizer(wx.HORIZONTAL)
        b_check = wx.Button(panel, label="&Check map")
        b_test = wx.Button(panel, label="Test as a &student")
        b_undo = wx.Button(panel, label="&Undo")
        b_save = wx.Button(panel, wx.ID_SAVE, "&Save map")
        b_close = wx.Button(panel, wx.ID_CLOSE, "Cl&ose")
        for button in (b_check, b_test, b_undo, b_save, b_close):
            actions.Add(button, 0, wx.RIGHT, 8)
        outer.Add(actions, 0, wx.ALL, 10)
        self.status = wx.StaticText(panel, label="")
        outer.Add(self.status, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)
        panel.SetSizer(outer)
        self.SetSize((900, 820))

        b_add.Bind(wx.EVT_BUTTON, lambda e: self.add_place())
        b_edit.Bind(wx.EVT_BUTTON, lambda e: self.edit_place())
        b_move.Bind(wx.EVT_BUTTON, lambda e: self.move_place())
        b_delete.Bind(wx.EVT_BUTTON, lambda e: self.delete_place())
        b_start.Bind(wx.EVT_BUTTON, lambda e: self.set_start())
        b_connect.Bind(wx.EVT_BUTTON, lambda e: self.connect_places())
        b_delete_path.Bind(wx.EVT_BUTTON, lambda e: self.delete_path())
        b_auto.Bind(wx.EVT_BUTTON, lambda e: self.add_paths_automatically())
        b_add_link.Bind(wx.EVT_BUTTON, lambda e: self.add_link())
        b_delete_link.Bind(wx.EVT_BUTTON, lambda e: self.delete_link())
        add_floor.Bind(wx.EVT_BUTTON, lambda e: self.add_floor())
        b_check.Bind(wx.EVT_BUTTON, lambda e: self.check_map())
        b_test.Bind(wx.EVT_BUTTON, lambda e: self.test_route())
        b_undo.Bind(wx.EVT_BUTTON, lambda e: self.undo())
        b_save.Bind(wx.EVT_BUTTON, lambda e: self.save())
        b_close.Bind(wx.EVT_BUTTON, lambda e: self.close())
        self.Bind(wx.EVT_CLOSE, lambda e: self.close())
        self.Bind(wx.EVT_CHAR_HOOK, self._key)
        self.floor_choice.Bind(wx.EVT_CHOICE, self._change_floor)
        self.tool_choice.Bind(wx.EVT_CHOICE, self._change_tool)
        self.place_list.Bind(wx.EVT_LISTBOX, lambda e: self.canvas.Refresh())
        self.place_list.Bind(wx.EVT_LISTBOX_DCLICK, lambda e: self.edit_place())
        self._refresh(select_place=None)
        count = sum(len(f.get("places", [])) for f in self.floors)
        self._status(f"{count} {'place' if count == 1 else 'places'} on the map."
                     if count else "No places yet. Choose Add place to begin.", speak=False)
        wx.CallAfter(self.place_list.SetFocus)
        self.CentreOnParent()

    # -- state ---------------------------------------------------------------

    @property
    def floor(self):
        return self.floors[self.floor_index]

    def selected_place_id(self):
        index = self.place_list.GetSelection()
        places = self.floor.get("places", [])
        return places[index]["id"] if 0 <= index < len(places) else None

    def _selected_place(self):
        place_id = self.selected_place_id()
        if place_id is None:
            self._status("Select a place in the list first.")
        return user_maps.place_by_id(self.floor, place_id) if place_id else None

    def _status(self, text, speak=True):
        self.status.SetLabel(text)
        if speak:
            _say(text)

    def _before_change(self):
        self.undo_stack.append((copy.deepcopy(self.data), self.floor_index))
        del self.undo_stack[:-50]

    def _changed(self, select_place=None):
        self.dirty = True
        if self.floor_index not in self.traced:
            user_maps.fit_to_content(self.floor)
        self._refresh(select_place)

    def _refresh(self, select_place=None):
        """Rebuild the floor choice and both lists from the map."""
        self.floors = user_maps.floors_for(self.data)
        # A one-floor map keeps its fields at the top level, where "name" is
        # the map's name, not the floor's.
        self.floor_choice.SetItems(
            [f.get("name", f"Floor {i + 1}") for i, f in enumerate(self.floors)]
            if "floors" in self.data else ["Floor 1"])
        self.floor_choice.SetSelection(self.floor_index)
        places = self.floor.get("places", [])
        keep = select_place or self.selected_place_id()
        self.place_list.SetItems([user_maps.describe_place(self.floor, p, self.unit_system)
                                  for p in places])
        index = next((i for i, p in enumerate(places) if p["id"] == keep), None)
        if index is None and places:
            index = len(places) - 1 if select_place else 0
        if index is not None:
            self.place_list.SetSelection(index)
        self.path_list.SetItems([user_maps.describe_path(self.floor, p, self.unit_system)
                                 for p in self.floor.get("paths", [])])
        self.link_list.SetItems([user_maps.describe_link(self.data, link)
                                 for link in user_maps.links_for(self.data)])
        self.canvas.set_floor(self.floor, self.backgrounds[self.floor_index]
                              if self.floor_index < len(self.backgrounds) else "")

    # -- places --------------------------------------------------------------

    def add_place(self):
        dialog = PlaceDialog(self, self.floor, self.unit_system, mode="add")
        try:
            if dialog.ShowModal() != wx.ID_OK or not dialog.result:
                return
            result = dialog.result
        finally:
            dialog.Destroy()
        self._before_change()
        if "reference" in result:
            x, y = user_maps.position_from(self.floor, result["reference"],
                                           result["distance"], result["direction"])
        elif self.floor_index in self.traced:
            x, y = self.floor["width"] / 2, self.floor["height"] / 2
        else:
            x, y = user_maps.FIT_MARGIN_M, user_maps.FIT_MARGIN_M
        place = user_maps.add_place(self.floor, result["name"], x, y, result["description"])
        message = f"Added {place['name']}"
        if "reference" in result:
            reference = user_maps.place_by_id(self.floor, result["reference"])
            message += (f", {user_maps.format_map_distance(result['distance'], self.unit_system)} "
                        f"{result['direction']} of {reference['name']}")
            if result["connect"]:
                user_maps.connect_places(self.floor, reference["id"], place["id"])
                message += ", joined by a path"
        self._changed(select_place=place["id"])
        self._status(message + ".")
        self.place_list.SetFocus()

    def add_place_at(self, x, y):
        """Mouse: add a place where the creator clicked."""
        dialog = PlaceDialog(self, self.floor, self.unit_system, mode="add", position_known=True)
        try:
            if dialog.ShowModal() != wx.ID_OK or not dialog.result:
                return
            result = dialog.result
        finally:
            dialog.Destroy()
        self._before_change()
        place = user_maps.add_place(self.floor, result["name"], x, y, result["description"])
        joined = user_maps.join_place_to_paths(self.floor, place, self.canvas.join_distance())
        self._changed(select_place=place["id"])
        if joined:
            self._status(f"Added {place['name']}, joined to the path beside it.")
        else:
            self._status(f"Added {place['name']}. Draw a path to it, or click nearer a path, "
                         "so directions can reach it.")

    def edit_place(self):
        place = self._selected_place()
        if place is None:
            return
        dialog = PlaceDialog(self, self.floor, self.unit_system, mode="edit", place=place)
        try:
            if dialog.ShowModal() != wx.ID_OK or not dialog.result:
                return
            result = dialog.result
        finally:
            dialog.Destroy()
        self._before_change()
        place["name"], place["description"] = result["name"], result["description"]
        self._changed(select_place=place["id"])
        self._status(f"Updated {place['name']}.")

    def move_place(self):
        place = self._selected_place()
        if place is None:
            return
        if len(self.floor.get("places", [])) < 2:
            self._status("Add another place first, to measure from.")
            return
        dialog = PlaceDialog(self, self.floor, self.unit_system, mode="move", place=place)
        try:
            if dialog.ShowModal() != wx.ID_OK or not dialog.result:
                return
            result = dialog.result
        finally:
            dialog.Destroy()
        self._before_change()
        x, y = user_maps.position_from(self.floor, result["reference"],
                                       result["distance"], result["direction"])
        user_maps.move_place(self.floor, place["id"], x, y)
        reference = user_maps.place_by_id(self.floor, result["reference"])
        self._changed(select_place=place["id"])
        self._status(f"Moved {place['name']} to "
                     f"{user_maps.format_map_distance(result['distance'], self.unit_system)} "
                     f"{result['direction']} of {reference['name']}.")

    def select_place(self, place_id):
        """Mouse: pick a place on the map, as if chosen in the Places list."""
        places = self.floor.get("places", [])
        index = next((i for i, p in enumerate(places) if p["id"] == place_id), None)
        if index is None:
            return
        self.place_list.SetSelection(index)
        self.canvas.Refresh()
        self._status(user_maps.describe_place(self.floor, places[index], self.unit_system) + ".")

    def delete_line(self, kind, index):
        """Mouse: delete the path or wall that was right-clicked."""
        lines = self.floor.get(kind, [])
        if not 0 <= index < len(lines):
            return
        self._before_change()
        text = (user_maps.describe_path(self.floor, lines[index], self.unit_system)
                if kind == "paths" else "")
        del lines[index]
        self._changed()
        self._status(f"Deleted path {text}." if kind == "paths" else "Deleted the wall.")

    def move_place_to(self, place_id, x, y):
        """Mouse: a place dragged to a new spot."""
        place = user_maps.place_by_id(self.floor, place_id)
        if place is None:
            return
        self._before_change()
        user_maps.remove_spurs(self.floor, place["id"])
        user_maps.move_place(self.floor, place["id"], x, y)
        joined = user_maps.join_place_to_paths(self.floor, place, self.canvas.join_distance())
        self._changed(select_place=place["id"])
        self._status(f"Moved {place['name']}" + (", joined to the path beside it." if joined
                     else ". It is not beside a path, so draw one to it."))

    def delete_place(self):
        place = self._selected_place()
        if place is None:
            return
        self._before_change()
        user_maps.remove_spurs(self.floor, place["id"])
        user_maps.delete_place(self.floor, place["id"])
        user_maps.remove_links_to_place(self.data, self.floor_index, place["id"])
        self._changed()
        self._status(f"Deleted {place['name']}. Undo brings it back.")

    def set_start(self):
        place = self._selected_place()
        if place is None:
            return
        self._before_change()
        self.floor["start"] = place["id"]
        self._changed(select_place=place["id"])
        self._status(f"Students start at {place['name']}.")

    # -- paths ---------------------------------------------------------------

    def connect_places(self):
        places = self.floor.get("places", [])
        if len(places) < 2:
            self._status("Add at least two places first.")
            return
        dialog = ConnectDialog(self, places, max(0, self.place_list.GetSelection()))
        try:
            if dialog.ShowModal() != wx.ID_OK or not dialog.result:
                return
            first_id, second_id = dialog.result
        finally:
            dialog.Destroy()
        self._before_change()
        path = user_maps.connect_places(self.floor, first_id, second_id)
        self._changed()
        self.path_list.SetSelection(len(self.floor["paths"]) - 1)
        self._status("Added path " + user_maps.describe_path(self.floor, path, self.unit_system) + ".")

    def add_drawn_line(self, tool, stroke):
        """Mouse: keep a dragged path or barrier."""
        clean = user_maps.simplify_points(stroke, tolerance=max(0.5, self.floor["width"] / 200))
        self._before_change()
        if tool == "barrier":
            self.floor.setdefault("barriers", []).append({"points": [[x, y] for x, y in clean]})
            self._changed()
            self._status("Added a wall or barrier. Students hear a sound when they cross it.")
            return
        path = {"points": [[x, y] for x, y in clean]}
        self.floor["paths"].append(path)
        joined = user_maps.join_drawn_path(self.floor, path, self.canvas.join_distance())
        self._changed()
        self._status("Added path " + user_maps.describe_path(self.floor, path, self.unit_system)
                     + (f", joined to {user_maps.short_list(joined)}." if joined else "."))

    def add_paths_automatically(self):
        """Find the walkways on this floor's picture and join every place."""
        import map_autopaths
        picture = (self.backgrounds[self.floor_index]
                   if self.floor_index < len(self.backgrounds) else "")
        if not picture or not os.path.exists(picture):
            self._status("Paths can be added automatically only on a floor imported "
                         "from a PDF or picture. Join places yourself instead.")
            return
        if not self.floor.get("places"):
            self._status("Add the places first; paths are joined to them.")
            return
        self._before_change()
        try:
            with wx.BusyInfo("Finding the walkways on the map..."):
                summary = map_autopaths.add_paths_automatically(self.floor, picture)
        except Exception as exc:
            self.undo_stack.pop()
            self._status(f"Paths could not be added automatically: {exc}")
            return
        if summary is None:
            self.undo_stack.pop()
            self._status("No walkways could be found on this picture. "
                         "Join the places yourself with Join two places.")
            return
        self._changed()
        total = len(self.floor.get("places", []))
        message = (f"Added walkways and joined {summary['joined']} of {total} places.")
        if summary["named_left_out"]:
            message += (" Join these yourself, as they are away from the walkways found: "
                        f"{user_maps.short_list(summary['named_left_out'])}.")
        problems = user_maps.check_floor(self.floor)
        if problems:
            message += " Check map lists what directions cannot reach yet."
        else:
            message += " Every place can be reached."
        message += " Undo removes them all."
        self._status(message)
        if summary["left_out_ids"]:
            count = len(summary["left_out_ids"])
            answer = wx.MessageBox(
                f"{count} {'place was' if count == 1 else 'places were'} not joined to any "
                f"walkway: {user_maps.short_list(summary['left_out'], 10)}.\n\n"
                "These are usually legend or title text, or things outside the building. "
                "Delete them from the map? Undo brings them back.",
                "Places not joined", wx.YES_NO | wx.ICON_QUESTION, self)
            if answer == wx.YES:
                self._before_change()
                for place_id in summary["left_out_ids"]:
                    user_maps.delete_place(self.floor, place_id)
                    user_maps.remove_links_to_place(self.data, self.floor_index, place_id)
                self._changed()
                self._status(f"Deleted {count} {'place' if count == 1 else 'places'} "
                             "that were not joined.")

    def delete_path(self):
        index = self.path_list.GetSelection()
        paths = self.floor.get("paths", [])
        if not 0 <= index < len(paths):
            self._status("Select a path in the list first.")
            return
        self._before_change()
        text = user_maps.describe_path(self.floor, paths[index], self.unit_system)
        del paths[index]
        self._changed()
        self._status(f"Deleted path {text}.")

    # -- stairs and lifts ----------------------------------------------------

    def add_link(self):
        if len(self.floors) < 2:
            self._status("Add a floor first; stairs and lifts join two floors.")
            return
        if not self.floor.get("places"):
            self._status("Add a place on this floor first, where the stairs or lift is.")
            return
        dialog = LinkDialog(self, self.data, self.floor_index)
        try:
            if dialog.ShowModal() != wx.ID_OK or not dialog.result:
                return
            result = dialog.result
        finally:
            dialog.Destroy()
        self._before_change()
        target = self.floors[result["floor"]]
        arrival = result["arrival"]
        if arrival is None:
            # A matching place straight above or below, joined to nothing yet.
            here = user_maps.place_by_id(self.floor, result["place"])
            x = min(max(here["x"], 0.0), target["width"])
            y = min(max(here["y"], 0.0), target["height"])
            arrival = user_maps.add_place(target, result["kind"].capitalize(), x, y)["id"]
        link = user_maps.add_link(self.data, result["kind"], self.floor_index,
                                  result["place"], result["floor"], arrival)
        self._changed()
        message = "Added " + user_maps.describe_link(self.data, link) + "."
        if result["arrival"] is None:
            message += (f" A place called {result['kind'].capitalize()} was added on "
                        f"{user_maps.floor_name(self.data, result['floor'])}; join it to a path there.")
        self._status(message)

    def delete_link(self):
        index = self.link_list.GetSelection()
        links = user_maps.links_for(self.data)
        if not 0 <= index < len(links):
            self._status("Select stairs or a lift in the list first.")
            return
        self._before_change()
        text = user_maps.describe_link(self.data, links[index])
        del links[index]
        self._changed()
        self._status(f"Deleted {text}.")

    # -- floors, checks and testing -----------------------------------------

    def add_floor(self):
        dialog = wx.TextEntryDialog(self, "Name of the new floor:", "Add a floor",
                                    f"Floor {len(self.floors) + 1}")
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            name = dialog.GetValue().strip()
        finally:
            dialog.Destroy()
        self._before_change()
        user_maps.add_floor(self.data, name)
        self.floors = user_maps.floors_for(self.data)
        self.backgrounds.extend([""] * (len(self.floors) - len(self.backgrounds)))
        self.floor_index = len(self.floors) - 1
        self._changed()
        self._status(f"Added {self.floor['name']}. Add its places; students move between "
                     "floors with Ctrl+Page Up and Ctrl+Page Down.")

    def _change_floor(self, event=None):
        self.floor_index = self.floor_choice.GetSelection()
        self._refresh()
        count = len(self.floor.get("places", []))
        self._status(f"{self.floor_choice.GetStringSelection()}, "
                     f"{count} {'place' if count == 1 else 'places'}.")

    def _change_tool(self, event=None):
        tool = ("none", "draw", "move", "barrier")[self.tool_choice.GetSelection()]
        self.canvas.set_tool(tool)
        if tool != "none":
            self.ready_to_draw()

    def ready_to_draw(self):
        """True once this floor has a picture or a size to draw on."""
        if self.floor_index in self.traced or self.floor.get("fixed_size"):
            return True
        if self._ask_drawing_size():
            return True
        self.tool_choice.SetSelection(0)
        self.canvas.set_tool("none")
        return False

    def _ask_drawing_size(self):
        """Before drawing on a floor with no picture, learn how big the area
        is, so the drawing stays still and its distances are real."""
        unit = user_maps.unit_label(self.unit_system)
        imperial = self.unit_system == "imperial"
        current = self.floor["width"] if self.floor.get("places") else (100 if imperial else 30)
        if self.floor.get("places") and imperial:
            current *= user_maps.FEET_PER_METRE
        while True:
            dialog = wx.TextEntryDialog(
                self,
                f"How wide is the area you will draw, from left to right, in {unit}? "
                f"A classroom is about {'33' if imperial else '10'} {unit} across, and a school "
                f"or shopping centre a few hundred {unit}. The drawing area will be this wide, "
                "so the distances you draw are real.",
                "Size of the area", str(int(round(current))))
            try:
                if dialog.ShowModal() != wx.ID_OK:
                    return False
                text = dialog.GetValue()
            finally:
                dialog.Destroy()
            metres = user_maps.parse_distance(text, self.unit_system)
            if metres and metres > 0:
                break
            wx.MessageBox(f"Type a width in {unit}, for example 30.", "Size of the area",
                          wx.OK | wx.ICON_INFORMATION, self)
        self._before_change()
        user_maps.set_drawing_size(self.floor, metres, self.canvas.aspect())
        self._changed()
        self._status(f"The drawing area is {user_maps.format_map_distance(metres, self.unit_system)} "
                     "wide. Click to add places, and drag to draw paths between them.")
        return True

    def _problems(self):
        problems = list(user_maps.check_links(self.data))
        for floor in self.floors:
            for problem in user_maps.check_floor(floor):
                prefix = f"{floor['name']}: " if len(self.floors) > 1 else ""
                problems.append(prefix + problem)
        return problems

    def check_map(self):
        problems = self._problems()
        if not problems:
            self._status("The map is ready. Every place can be reached by directions.")
            return
        wx.MessageBox("\n\n".join(problems), "Check map", wx.OK | wx.ICON_INFORMATION, self)

    def test_route(self):
        if len(self.floor.get("places", [])) < 2:
            self._status("Add at least two places before testing directions.")
            return
        dialog = LocalRouteDialog(self, self.data, allow_explore=True,
                                  floor_index=self.floor_index)
        dialog.ShowModal()
        dialog.Destroy()

    def undo(self):
        if not self.undo_stack:
            self._status("Nothing to undo.")
            return
        self.data, self.floor_index = self.undo_stack.pop()
        self.floors = user_maps.floors_for(self.data)
        self.floor_index = min(self.floor_index, len(self.floors) - 1)
        self.dirty = True
        self._refresh()
        self._status("Undone.")

    # -- saving and closing --------------------------------------------------

    def save(self):
        problems = self._problems()
        if problems:
            answer = wx.MessageBox(
                "Students can explore this map, but directions will not work fully:\n\n"
                + "\n\n".join(problems) + "\n\nSave anyway?",
                "Save map", wx.YES_NO | wx.ICON_QUESTION, self)
            if answer != wx.YES:
                return False
        path = self.save_path
        if not path:
            dialog = wx.FileDialog(
                self, "Save map file", wildcard="Map in a Box maps (*.miabmap)|*.miabmap",
                defaultDir=os.path.dirname(self.suggested_save_path),
                defaultFile=(os.path.basename(self.suggested_save_path)
                             or f"{self.data.get('name', 'map')}.miabmap"),
                style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT)
            try:
                if dialog.ShowModal() != wx.ID_OK:
                    return False
                path = dialog.GetPath()
            finally:
                dialog.Destroy()
        try:
            backgrounds = (self.backgrounds if "floors" in self.data
                           else (self.backgrounds[0] or None))
            self.save_path = user_maps.save_map(path, self.data, backgrounds)
        except Exception as exc:
            wx.MessageBox(f"The map could not be saved.\n\n{exc}", "Save map",
                          wx.OK | wx.ICON_ERROR, self)
            return False
        self.dirty = False
        self._status(f"Saved {os.path.basename(self.save_path)}.")
        return True

    def close(self):
        if self.dirty:
            answer = wx.MessageBox(
                f"Save changes to {self.data.get('name', 'this map')} before closing?",
                "Close map builder", wx.YES_NO | wx.CANCEL | wx.ICON_QUESTION, self)
            if answer == wx.CANCEL:
                return
            if answer == wx.YES and not self.save():
                return
        self.EndModal(wx.ID_OK if self.save_path else wx.ID_CANCEL)

    def _key(self, event):
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            self.close()
            return
        if event.GetKeyCode() == wx.WXK_DELETE:
            focus = wx.Window.FindFocus()
            if focus is self.place_list:
                self.delete_place()
                return
            if focus is self.path_list:
                self.delete_path()
                return
        if event.ControlDown() and event.GetKeyCode() in (ord("Z"), ord("z")):
            self.undo()
            return
        if event.ControlDown() and event.GetKeyCode() in (ord("S"), ord("s")):
            self.save()
            return
        event.Skip()


class LocalRouteDialog(wx.Dialog):
    """Directions between two places, on one floor or across floors."""

    def __init__(self, parent, data, allow_explore=True, floor_index=0, here=None):
        """``here`` is the id of the place the student is at or nearest, so
        From starts there."""
        super().__init__(parent, title="Directions between places",
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.data = data
        self.route = None
        self.allow_explore = allow_explore
        floors = user_maps.floors_for(data)
        multi = len(floors) > 1
        # Places of the current floor first, so the usual trip is the default.
        order = [floor_index] + [i for i in range(len(floors)) if i != floor_index]
        self.places = [(i, place) for i in order for place in floors[i].get("places") or []]
        names = [f"{place['name']} ({user_maps.floor_name(data, i)})" if multi else place["name"]
                 for i, place in self.places]
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
        start = next((index for index, (floor, place) in enumerate(self.places)
                      if floor == floor_index and place["id"] == here), 0)
        if names:
            self.origin.SetSelection(start)
        if len(names) > 1:
            self.destination.SetSelection(1 if start == 0 else 0)
        # Enter in From or To finds the route.
        find_button.SetDefault()
        find_button.Bind(wx.EVT_BUTTON, self._find)
        self.explore_button.Bind(wx.EVT_BUTTON, self._explore)
        close_button.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CLOSE))
        self.CentreOnParent()
        wx.CallAfter(self.origin.SetFocus)

    def _find(self, event=None):
        oi, di = self.origin.GetSelection(), self.destination.GetSelection()
        if oi < 0 or di < 0 or oi == di:
            self.result.SetValue("Choose two different places.")
            self.result.SetFocus()
            return
        units = get_unit_system()
        (origin_floor, origin), (goal_floor, goal) = self.places[oi], self.places[di]
        try:
            self.route = user_maps.find_route_between_floors(
                self.data, (origin_floor, origin["id"]), (goal_floor, goal["id"]))
        except ValueError as exc:
            self.route = None
            self.explore_button.Enable(False)
            self.result.SetValue(str(exc))
            self.result.SetFocus()
            return
        lines = []
        if origin_floor == goal_floor:
            direct = math.hypot(goal["x"] - origin["x"], goal["y"] - origin["y"])
            direction = user_maps.compass_name(goal["x"] - origin["x"], goal["y"] - origin["y"])
            lines.append(f"{goal['name']} is about "
                         f"{user_maps.format_map_distance(direct, units)} {direction} as the crow flies.")
        else:
            lines.append(f"{goal['name']} is on {user_maps.floor_name(self.data, goal_floor)}.")
        lines += [f"The route is about {user_maps.format_map_distance(self.route.distance, units)}"
                  + (" of walking." if self.route.changes else "."), ""]
        lines.extend(user_maps.multi_floor_directions(self.data, self.route, units))
        self.result.SetValue("\n".join(lines))
        self.result.SetInsertionPoint(0)
        # Straight to the directions, so they can be read line by line.
        self.result.SetFocus()
        # Exploring with sound follows one floor's path.
        self.explore_button.Enable(self.allow_explore and len(self.route.legs) == 1)

    def _explore(self, event=None):
        if not self.route or len(self.route.legs) != 1:
            return
        from dialogs import ExplorePathDialog
        leg = self.route.legs[0][1]
        points = [
            {"lat": y / 111195.0, "lon": x / 111195.0,
             "instruction": "", "maneuver": ""}
            for x, y in leg.points
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
