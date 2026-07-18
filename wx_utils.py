"""wx_utils.py - shared wx keyboard and logging helpers for Map in a Box."""

import wx

from logging_utils import miab_log

IS_MAC = wx.Platform == "__WXMAC__"


class MSAAListBox(wx.ListBox):
    """A wx.ListBox that updates without double-reads on Windows screen readers.

    Background
    ----------
    Two common wx.ListBox update patterns each fire more than one MSAA event
    per logical change, which Windows screen readers (notably NVDA) read as
    either a stale value, a duplicate, or both:

      * ``SetString(i, text)`` mutates an existing item — fires two events.
      * ``Set([text])`` + ``SetSelection(0)`` replaces the contents and the
        selection separately — fires two events.

    The reliable workaround (see project memory: project_listbox_msaa.md) is
    the Append+Select+Delete cycle: append the new value, select it, then
    delete the prior items. The selection-change event for the newly added
    item is the only MSAA write event the screen reader sees.

    This subclass exposes that cycle as three intent-named methods so callers
    do not have to remember the dance. Mixing them with the bare wx API is
    safe — they are only safer alternatives for the patterns above, not
    replacements for the whole API.
    """

    def set_single(self, text: str) -> None:
        """Replace contents with one selected item, read exactly once."""
        n = self.GetCount()
        self.Insert(str(text), 0)
        self.SetSelection(0)
        for i in range(n, 0, -1):
            self.Delete(i)

    def set_many(self, items, sel: int = 0) -> None:
        """Replace contents with a list, focus a specific index, read once.

        Use this instead of ``Set(labels)`` + ``SetSelection(idx)``. ``sel`` is
        clamped to a valid index for the new list.

        New items are inserted at the front (indices 0..len(items)-1)
        rather than appended after the leftover old ones. That matters:
        the old implementation appended new items *after* the old ones,
        selected the new item, and only then deleted the old items sitting
        *before* it — which silently shifts the selected index down as
        each earlier item is removed, with no fresh selection event to go
        with it. NVDA would end up querying a now-stale index and read the
        newly selected item as blank/"unknown". Inserting the new items at
        the front means every old item being deleted sits *above* the
        selected index, so the selection is never touched by the cleanup.
        """
        items = list(items)
        if not items:
            self.set_single("")
            return
        n = self.GetCount()
        for offset, it in enumerate(items):
            self.Insert(str(it), offset)
        new_idx = max(0, min(int(sel), len(items) - 1))
        self.SetSelection(new_idx)
        # Old items now sit at [len(items), len(items)+n-1]. Delete from the
        # highest index down so removing one never renumbers another that
        # still needs deleting.
        for i in range(len(items) + n - 1, len(items) - 1, -1):
            self.Delete(i)

    def update_focused(self, text: str) -> None:
        """Change the text of the current selection, read exactly once.

        Equivalent to ``SetString(GetSelection(), text)`` but without the
        double-read. Falls back to ``set_single`` when there is no selection.
        The new value occupies the same position as the old one, so
        surrounding items keep their order.
        """
        sel = self.GetSelection()
        if sel == wx.NOT_FOUND:
            self.set_single(text)
            return
        # Insert the replacement directly above the old item so the order is
        # preserved, select it, then drop the now-stale old item below.
        self.Insert(str(text), sel)
        self.SetSelection(sel)
        self.Delete(sel + 1)


def _primary_down(event) -> bool:
    """Treat Command as the primary modifier on macOS and Control elsewhere."""
    if IS_MAC and hasattr(event, "CmdDown"):
        return event.CmdDown()
    return event.ControlDown()


def _key_name(keycode) -> str:
    """Best-effort human-readable name for a wx keycode."""
    try:
        keycode = int(keycode)
    except Exception:
        return str(keycode)

    named = {
        wx.WXK_BACK: "BACK",
        wx.WXK_RETURN: "RETURN",
        wx.WXK_NUMPAD_ENTER: "NUMPAD_ENTER",
        wx.WXK_ESCAPE: "ESCAPE",
        wx.WXK_UP: "UP",
        wx.WXK_DOWN: "DOWN",
        wx.WXK_LEFT: "LEFT",
        wx.WXK_RIGHT: "RIGHT",
        wx.WXK_SPACE: "SPACE",
        wx.WXK_TAB: "TAB",
    }
    return named.get(keycode, chr(keycode) if 32 <= keycode < 127 else str(keycode))


def _log_key_event(owner, event, source: str, note: str = "") -> None:
    """Write a verbose trace for a keyboard event."""
    settings = getattr(owner, "settings", None)
    if not settings or not settings.get("logging", {}).get("verbose", False):
        return

    try:
        focus = wx.Window.FindFocus()
        focus_name = focus.GetName() if focus and focus.GetName() else type(focus).__name__ if focus else "None"
        target = event.GetEventObject()
        target_name = target.GetName() if target and target.GetName() else type(target).__name__ if target else "None"
        miab_log(
            "verbose",
            (
                f"Key {source}: key={_key_name(event.GetKeyCode())} "
                f"code={event.GetKeyCode()} primary={_primary_down(event)} "
                f"alt={event.AltDown()} shift={event.ShiftDown()} "
                f"focus={focus_name} target={target_name}"
                + (f" note={note}" if note else "")
            ),
            settings,
        )
    except Exception as exc:
        miab_log("verbose", f"Key {source} logging failed: {exc}", settings)
