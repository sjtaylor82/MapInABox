import threading
import unittest

import wx

import core


class _Host(wx.Frame):
    """Hidden frame carrying MapNavigator's thread-sensitive helpers."""

    _defer_to_ui_thread = core.MapNavigator._defer_to_ui_thread
    _set_current_location_title = core.MapNavigator._set_current_location_title
    _keep_app_title = core.MapNavigator._keep_app_title

    def __init__(self):
        super().__init__(None, title="Somewhere else")
        self.settings = {}
        self.title_reads_off_ui_thread = 0
        self.announced = []

    def GetTitle(self):
        if not wx.IsMainThread():
            self.title_reads_off_ui_thread += 1
        return super().GetTitle()

    def _announce(self, msg):
        if self._defer_to_ui_thread(self._announce, msg):
            return
        self.announced.append((msg, wx.IsMainThread()))


class UiThreadSafetyTests(unittest.TestCase):
    def test_background_lookup_never_touches_the_window_from_its_thread(self):
        app = wx.App(False)
        host = _Host()
        results = {}

        def background_lookup():
            results["label"] = host._set_current_location_title("Ireland")
            host._announce("Entered Ireland")
            wx.CallAfter(wx.CallLater, 50, app.ExitMainLoop)

        wx.CallAfter(lambda: threading.Thread(target=background_lookup).start())
        wx.CallLater(5000, app.ExitMainLoop)  # safety net
        app.MainLoop()

        self.assertEqual(results["label"], "Ireland")
        self.assertEqual(host._current_focus_location_label, "Ireland")
        self.assertEqual(host.title_reads_off_ui_thread, 0)
        self.assertEqual(host.GetTitle(), core.APP_NAME)
        self.assertEqual(host.announced, [("Entered Ireland", True)])
        host.Destroy()


if __name__ == "__main__":
    unittest.main()
