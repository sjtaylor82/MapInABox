"""wx_utils.py - shared wx keyboard and logging helpers for Map in a Box."""

import wx

from logging_utils import miab_log

IS_MAC = wx.Platform == "__WXMAC__"


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
