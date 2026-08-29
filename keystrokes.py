"""Accessible, context-aware keyboard customisation for Map in a Box."""

from dataclasses import dataclass
import wx

from wx_utils import IS_MAC, _primary_down


@dataclass(frozen=True)
class KeyAction:
    id: str
    label: str
    context: str
    default: str


KEY_ACTIONS = (
    KeyAction("street_imagery", "Open Street View or Mapillary", "Global", "Primary+Shift+S"),
    KeyAction("satellite_imagery", "Open satellite view", "Global", "Primary+Shift+Alt+S"),
    KeyAction("jump", "Jump to a place or coordinates", "Map", "J"),
    KeyAction("weather", "Announce weather", "Map", "W"),
    KeyAction("poi_search", "Search nearby points of interest", "Street", "P"),
    KeyAction("street_search", "Search streets", "Street", "S"),
    KeyAction("address", "Announce current address", "Street", "A"),
    KeyAction("navigate_address", "Navigate to an address", "Street", "Primary+G"),
    KeyAction("walking_mode", "Toggle walking mode", "Street", "W"),
    KeyAction("route_briefing", "Narrative briefing of current route", "Street", "Shift+I"),
)
ACTIONS_BY_ID = {action.id: action for action in KEY_ACTIONS}
CONTEXTS = ("Global", "Map", "Street")


def normalize_bindings(value):
    if not isinstance(value, dict):
        return {}
    result = {}
    for action_id, chord in value.items():
        if action_id in ACTIONS_BY_ID and isinstance(chord, str):
            chord = chord.strip()
            if chord:
                result[action_id] = chord
            elif chord == "":
                result[action_id] = ""
    return result


def binding_for(action_id, custom):
    custom = normalize_bindings(custom)
    return custom.get(action_id, ACTIONS_BY_ID[action_id].default)


def display_chord(chord):
    if not chord:
        return "Not assigned"
    primary = "Command" if IS_MAC else "Control"
    return chord.replace("Primary", primary)


def chord_from_event(event):
    keycode = int(event.GetKeyCode())
    ignored = {
        0, wx.WXK_SHIFT, wx.WXK_CONTROL, wx.WXK_ALT,
        getattr(wx, "WXK_RAW_CONTROL", -1),
    }
    if keycode in ignored:
        return ""
    special = {
        wx.WXK_SPACE: "Space", wx.WXK_RETURN: "Enter",
        wx.WXK_NUMPAD_ENTER: "Enter", wx.WXK_TAB: "Tab",
        wx.WXK_DELETE: "Delete", wx.WXK_BACK: "Backspace",
        wx.WXK_LEFT: "Left", wx.WXK_RIGHT: "Right",
        wx.WXK_UP: "Up", wx.WXK_DOWN: "Down",
        wx.WXK_HOME: "Home", wx.WXK_END: "End",
        wx.WXK_PAGEUP: "Page Up", wx.WXK_PAGEDOWN: "Page Down",
    }
    if wx.WXK_F1 <= keycode <= wx.WXK_F24:
        key_name = f"F{keycode - wx.WXK_F1 + 1}"
    elif keycode in special:
        key_name = special[keycode]
    elif keycode in (ord('/'), getattr(wx, "WXK_NUMPAD_DIVIDE", -1)):
        key_name = "Slash"
    elif keycode in (ord('+'), ord('='), getattr(wx, "WXK_NUMPAD_ADD", -1)):
        key_name = "Plus"
    elif keycode in (ord('-'), getattr(wx, "WXK_NUMPAD_SUBTRACT", -1)):
        key_name = "Minus"
    elif 32 <= keycode < 127:
        key_name = chr(keycode).upper()
    else:
        key_name = f"Key {keycode}"
    parts = []
    if _primary_down(event):
        parts.append("Primary")
    if event.ShiftDown():
        parts.append("Shift")
    if event.AltDown():
        parts.append("Alt")
    parts.append(key_name)
    return "+".join(parts)


def action_for_event(event, custom, contexts):
    chord = chord_from_event(event)
    if not chord:
        return None
    for context in contexts:
        for action in KEY_ACTIONS:
            if action.context == context and binding_for(action.id, custom) == chord:
                return action.id
    return None


def disabled_default_for_event(event, custom, contexts):
    chord = chord_from_event(event)
    if not chord:
        return False
    normalized = normalize_bindings(custom)
    return any(
        action.context in contexts
        and action.default == chord
        and action.id in normalized
        and normalized[action.id] != action.default
        for action in KEY_ACTIONS
    )


class KeystrokeCaptureDialog(wx.Dialog):
    def __init__(self, parent, label):
        super().__init__(parent, title=f"Assign {label}")
        self.chord = ""
        sizer = wx.BoxSizer(wx.VERTICAL)
        text = wx.StaticText(
            self,
            label="Press the new keystroke now. Escape cancels.",
        )
        sizer.Add(text, 0, wx.ALL, 18)
        self.SetSizerAndFit(sizer)
        self.Bind(wx.EVT_CHAR_HOOK, self._capture)
        self.CentreOnParent()

    def _capture(self, event):
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            self.EndModal(wx.ID_CANCEL)
            return
        chord = chord_from_event(event)
        if chord:
            self.chord = chord
            self.EndModal(wx.ID_OK)


class KeystrokeManagerDialog(wx.Dialog):
    def __init__(self, parent, bindings):
        super().__init__(parent, title="Keystroke Manager",
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.bindings = normalize_bindings(bindings)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(wx.StaticText(self, label="Context"), 0, wx.LEFT | wx.RIGHT | wx.TOP, 12)
        self.context = wx.Choice(self, choices=["All commands", *CONTEXTS])
        self.context.SetSelection(0)
        outer.Add(self.context, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        outer.Add(wx.StaticText(self, label="Search commands"), 0, wx.LEFT | wx.RIGHT, 12)
        self.search = wx.TextCtrl(self)
        outer.Add(self.search, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        outer.Add(wx.StaticText(self, label="Commands and assigned keys"), 0, wx.LEFT | wx.RIGHT, 12)
        self.actions = wx.ListBox(self, choices=self._choices())
        if self.actions.GetCount():
            self.actions.SetSelection(0)
        outer.Add(self.actions, 1, wx.EXPAND | wx.ALL, 12)
        row = wx.BoxSizer(wx.HORIZONTAL)
        assign = wx.Button(self, label="&Assign...")
        clear = wx.Button(self, label="&Clear")
        defaults = wx.Button(self, label="Restore &defaults")
        row.Add(assign, 0, wx.RIGHT, 8)
        row.Add(clear, 0, wx.RIGHT, 8)
        row.Add(defaults, 0)
        outer.Add(row, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        buttons = self.CreateSeparatedButtonSizer(wx.OK | wx.CANCEL)
        if buttons:
            outer.Add(buttons, 0, wx.EXPAND | wx.ALL, 12)
        self.SetSizerAndFit(outer)
        self.SetMinSize((650, 480))
        self.context.Bind(wx.EVT_CHOICE, lambda e: self._refresh())
        self.search.Bind(wx.EVT_TEXT, lambda e: self._refresh())
        assign.Bind(wx.EVT_BUTTON, self._assign)
        clear.Bind(wx.EVT_BUTTON, self._clear)
        defaults.Bind(wx.EVT_BUTTON, self._restore_defaults)
        self.actions.Bind(wx.EVT_LISTBOX_DCLICK, self._assign)
        self.search.SetFocus()

    def _filtered_actions(self):
        selected = self.context.GetSelection()
        context = None if selected <= 0 else CONTEXTS[selected - 1]
        actions = [a for a in KEY_ACTIONS if context is None or a.context == context]
        query = self.search.GetValue().strip().casefold() if hasattr(self, "search") else ""
        if query:
            actions = [
                a for a in actions
                if query in f"{a.label} {a.context} {display_chord(binding_for(a.id, self.bindings))}".casefold()
            ]
        return actions

    def _choices(self):
        return [
            f"{a.context}: {a.label}: {display_chord(binding_for(a.id, self.bindings))}"
            for a in self._filtered_actions()
        ]

    def _selected_action(self):
        index = self.actions.GetSelection()
        actions = self._filtered_actions()
        return actions[index] if 0 <= index < len(actions) else None

    def _refresh(self, selection=0):
        self.actions.Set(self._choices())
        if self.actions.GetCount():
            self.actions.SetSelection(min(max(0, selection), self.actions.GetCount() - 1))

    def _assign(self, event=None):
        action = self._selected_action()
        if not action:
            return
        selected = self.actions.GetSelection()
        capture = KeystrokeCaptureDialog(self, action.label)
        accepted = capture.ShowModal() == wx.ID_OK
        chord = capture.chord
        capture.Destroy()
        if not accepted or not chord:
            return
        if chord in {
                "Up", "Down", "Left", "Right", "Home", "End",
                "Page Up", "Page Down", "Tab", "Enter", "Space"}:
            wx.MessageBox(
                "That unmodified key is reserved for navigation. Add a modifier or choose another key.",
                "Keystroke Manager", wx.OK | wx.ICON_WARNING, self)
            return
        conflict = next(
            (a for a in KEY_ACTIONS
             if a.id != action.id
             and (a.context == action.context
                  or a.context == "Global" or action.context == "Global")
             and binding_for(a.id, self.bindings) == chord),
            None,
        )
        if conflict:
            answer = wx.MessageBox(
                f"Replace {conflict.label}?", "Keystroke Manager",
                wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, self)
            if answer != wx.YES:
                return
            self.bindings[conflict.id] = ""
        self.bindings[action.id] = chord
        self._refresh(selected)

    def _clear(self, event=None):
        action = self._selected_action()
        if action:
            selected = self.actions.GetSelection()
            self.bindings[action.id] = ""
            self._refresh(selected)

    def _restore_defaults(self, event=None):
        selected_context = self.context.GetSelection()
        context = None if selected_context <= 0 else CONTEXTS[selected_context - 1]
        for action in KEY_ACTIONS:
            if context is None or action.context == context:
                self.bindings.pop(action.id, None)
        self._refresh()
