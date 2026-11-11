"""Screen-reader speech and braille dispatch for Map in a Box."""

from __future__ import annotations

import collections
import os
import time
import threading
from typing import Callable

import wx

from logging_utils import miab_log

# Diagnostics/A-B switches for screen-reader timing.  Set in the environment
# before launching; all default to off.
_TRACE_TIMING = bool(os.environ.get("MIAB_SPEECH_TIMING"))
# JAWS braille goes through RunFunction('BrailleString(...)'), which executes
# in the JAWS script engine.  Set to skip it entirely and hear whether speech
# alone is prompt.
_JAWS_NO_BRAILLE = bool(os.environ.get("MIAB_JAWS_NO_BRAILLE"))


def _timing(msg: str) -> None:
    if _TRACE_TIMING:
        try:
            miab_log("verbose", "[speech] " + msg)
        except Exception:
            pass

try:
    import accessible_output2.outputs.auto as _ao2_auto
    _ao2 = _ao2_auto.Auto()
except Exception:
    _ao2 = None

_active_output = None
_active_output_lock = threading.Lock()


def _get_output():
    """Resolve AccessibleOutput2's backend once and cache it.

    ``Auto.speak``/``Auto.braille`` re-run ``get_first_available_output()`` on
    every call, and the JAWS backend's ``is_active()`` probe is a blocking COM
    round-trip into the JAWS process (``SayString("", 0)``).  NVDA's probe is a
    local DLL call, which is why only JAWS users saw movement turn sluggish.
    Resolving the backend a single time removes that probe from every
    announcement.
    """
    global _active_output
    if _active_output is not None:
        return _active_output or None
    with _active_output_lock:
        if _active_output is None and _ao2 is not None:
            try:
                _active_output = _ao2.get_first_available_output()
            except Exception:
                _active_output = False
    return _active_output or None


def _jaws_output():
    """Return the JAWS backend if it is the active screen reader, else None."""
    output = _get_output()
    if (output is not None
            and getattr(output, "name", "").lower() == "jaws"
            and getattr(output, "object", None) is not None):
        return output
    return None


def active_output_name() -> str:
    """Return the active AccessibleOutput2 backend name, if available."""
    try:
        output = _get_output()
        return str(getattr(output, "name", "") or "").strip().casefold()
    except Exception:
        return ""


class _Utterance:
    """One queued JAWS request: speech, braille, or both."""

    __slots__ = ("speech", "interrupt", "braille", "posted_at")

    def __init__(self, speech=None, interrupt=True, braille=None):
        self.speech = speech
        self.interrupt = interrupt
        self.braille = braille
        self.posted_at = time.time()


class _JawsChannel:
    """Serialises JAWS COM calls onto one dedicated worker thread.

    Every JAWS call is a cross-process COM round-trip costing tens of
    milliseconds.  Making them from wx's event thread parks the main loop and
    throttles Windows key-repeat, so holding an arrow key crawls.  The COM
    proxy is apartment-bound to the thread that created it, so this worker
    builds its own object after ``CoInitialize`` rather than borrowing the one
    made during import.
    """

    def __init__(self, output) -> None:
        self._fallback = output.object
        self._queue = collections.deque()
        self._wake = threading.Condition()
        self._thread = None
        self._thread_lock = threading.Lock()

    def post(self, item: _Utterance) -> None:
        """Queue an utterance, collapsing announcements the user outran.

        An interrupting utterance would cancel everything still queued ahead of
        it anyway, so those entries are dropped instead of being spoken into
        the void.  That is what keeps a held arrow key announcing the position
        it arrives at rather than working through every cell it passed over.
        """
        with self._wake:
            if item.interrupt and item.speech is not None:
                self._queue.clear()
            elif item.speech is None and self._queue:
                # Consecutive braille-only writes: only the last one lands on
                # the display, so there is no point queueing the earlier ones.
                while self._queue and self._queue[-1].speech is None:
                    self._queue.pop()
            self._queue.append(item)
            self._wake.notify()
        self._ensure_thread()

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._run, name="jaws-speech", daemon=True)
            self._thread.start()

    def _connect(self):
        """Build a COM object owned by this thread, falling back to the shared one."""
        try:
            import pythoncom
            pythoncom.CoInitialize()
        except Exception:
            return self._fallback
        try:
            from libloader.com import load_com
            return load_com("FreedomSci.JawsApi", "jfwapi")
        except Exception:
            return self._fallback

    def _run(self) -> None:
        api = self._connect()
        while True:
            with self._wake:
                while not self._queue:
                    self._wake.wait()
                item = self._queue.popleft()
            if item.speech is not None:
                t0 = time.time()
                try:
                    api.SayString(item.speech, item.interrupt)
                except Exception:
                    pass
                _timing("SayString %.1fms queued=%.1fms %r" % (
                    (time.time() - t0) * 1000.0,
                    (t0 - item.posted_at) * 1000.0, item.speech))
            if item.braille is not None and not _JAWS_NO_BRAILLE:
                t0 = time.time()
                try:
                    api.RunFunction(
                        'BrailleString("%s")' % item.braille.replace('"', "'"))
                except Exception:
                    pass
                _timing("BrailleString %.1fms" % ((time.time() - t0) * 1000.0))


_jaws_channel = None
_jaws_channel_lock = threading.Lock()


def _channel():
    """Return the JAWS channel, or None when JAWS is not the active reader."""
    global _jaws_channel
    if _jaws_channel is not None:
        return _jaws_channel or None
    output = _jaws_output()
    if output is None:
        return None
    with _jaws_channel_lock:
        if _jaws_channel is None:
            _jaws_channel = _JawsChannel(output)
    return _jaws_channel or None


def speak(msg: str, interrupt: bool = True) -> None:
    """Output directly to the active screen reader via AccessibleOutput2."""
    if not _ao2:
        return
    text = str(msg)
    try:
        channel = _channel()
    except Exception:
        channel = None
    if channel is not None:
        # AO2's JAWS adapter also prepends six spaces to every utterance, which
        # JAWS renders as an audible pause before the text.  Calling SayString
        # ourselves drops it.
        channel.post(_Utterance(speech=text, interrupt=interrupt, braille=text))
        return
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
    text = str(msg)
    try:
        channel = _channel()
    except Exception:
        channel = None
    if channel is not None:
        channel.post(_Utterance(braille=text))
        return
    try:
        _ao2.braille(text)
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
            if braille_text is not None:
                # speak() already brailled the spoken text; only a distinct
                # display string needs its own write.
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
