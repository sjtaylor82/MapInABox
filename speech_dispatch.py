"""Screen-reader speech and braille dispatch for Map in a Box."""

from __future__ import annotations

import time
from typing import Callable

import wx

try:
    import accessible_output2.outputs.auto as _ao2_auto
    _ao2 = _ao2_auto.Auto()
except Exception:
    _ao2 = None


def speak(msg: str, interrupt: bool = True) -> None:
    """Output directly to the active screen reader via AccessibleOutput2."""
    if not _ao2:
        return
    text = str(msg)
    try:
        _ao2.speak(text, interrupt=interrupt)
    except Exception:
        pass
    try:
        _ao2.braille(text)
    except Exception:
        pass


def braille(msg: str) -> None:
    """Send text to the active braille display without adding extra speech."""
    if not _ao2:
        return
    try:
        _ao2.braille(str(msg))
    except Exception:
        pass


class SpeechDispatch:
    """Owns direct speech/braille dispatch rules that are independent of UI state."""

    def __init__(self, trace_cb: Callable[[str], None] | None = None) -> None:
        self._trace = trace_cb or (lambda msg: None)
        self._last_mode_text = ""
        self._last_mode_at = 0.0

    def speak(self, msg: str, interrupt: bool = True) -> None:
        speak(msg, interrupt=interrupt)

    def braille(self, msg: str) -> None:
        braille(msg)

    def emit(
        self,
        text: str,
        braille_text: str | None = None,
        interrupt: bool = True,
        second_braille: bool = True,
    ) -> None:
        """Speak and braille on the wx main loop, with an optional second braille pass."""
        speech_text = str(text)
        display_text = speech_text if braille_text is None else str(braille_text)

        def _emit() -> None:
            speak(speech_text, interrupt=interrupt)
            braille(display_text)
            if second_braille:
                try:
                    wx.CallLater(80, lambda value=display_text: braille(value))
                except Exception:
                    pass

        wx.CallAfter(_emit)

    def transient(self, msg: str, braille_msg: str | None = None) -> None:
        """Speak and braille a transient announcement without touching focus."""
        text = str(msg)
        self._trace(f"transient announcement applied: {text!r}")
        self.emit(text, braille_msg)

    def mode_change(self, msg: str, duplicate_window_s: float = 1.25) -> None:
        """Announce a mode change once, suppressing immediate duplicates."""
        text = str(msg)
        now = time.time()
        if text == self._last_mode_text and now - self._last_mode_at < duplicate_window_s:
            self._trace(f"mode announcement suppressed duplicate: {text!r}")
            return
        self._last_mode_text = text
        self._last_mode_at = now
        self.transient(text)
