import unittest
from unittest import mock

import nav
from nav import NavMixin
from street_survey import StreetSurveyMixin


class _Navigator(NavMixin, StreetSurveyMixin):
    """Street mode navigating Fairway Drive -> First Avenue -> George Street."""

    def __init__(self):
        self.settings = {"walk_announce_pois": False}
        self.street_mode = True
        self.street_label = "Fairway Drive"
        self.lat, self.lon = 0.0, 0.0
        self._address_points = []
        self._all_pois = []
        self._road_segments = [
            {"name": "Fairway Drive", "coords": [(0.0, 0.0), (0.0, 0.001)]},
            {"name": "First Avenue", "coords": [(0.0, 0.001), (0.001, 0.001)]},
            {"name": "George Street", "coords": [(0.001, 0.001), (0.001, 0.002)]},
        ]
        self._nav_active = True
        self._nav_arrived = False
        self._nav_dest_name = "19 George Street"
        self._nav_instructions = [
            (1, 50, "Head north on Fairway Drive.", 0.0, 0.0),
            (2, 103, "slight left onto First Avenue.", 0.0, 0.001),
            (3, 109, "right onto George Street.", 0.001, 0.001),
            (4, 40, "Arriving at 19 George Street on George Street.", 0.001, 0.0015),
        ]
        self._nav_step = 1
        self.map_panel = mock.Mock()
        self.messages = []

    def _nav_update_ui(self, msg):
        self.messages.append(msg)

    def _nav_finish_arrival(self, context_later=True):
        self._nav_arrived = True

    def _nearest_road(self, lat, lon):
        # At a turn point the nearest road is ambiguous; report the old street
        # so the test proves the instruction's named street wins.
        return "Fairway Drive", ""


class NavStreetSyncTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(nav.wx, "CallAfter", lambda fn, *a, **k: fn(*a, **k))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.navigator = _Navigator()

    def test_street_follows_steps_to_the_destination(self):
        navigator = self.navigator
        navigator._nav_step_forward()
        self.assertEqual(navigator.street_label, "First Avenue")
        navigator._nav_step_forward()
        self.assertEqual(navigator.street_label, "George Street")
        navigator._nav_step_forward()
        self.assertEqual(navigator.street_label, "George Street")
        self.assertEqual((navigator.lat, navigator.lon), (0.001, 0.0015))

    def test_stepping_back_follows_too(self):
        navigator = self.navigator
        navigator._nav_step_forward()
        navigator._nav_step_forward()
        navigator._nav_step_back()
        self.assertEqual(navigator.street_label, "First Avenue")

    def test_unloaded_street_name_falls_back_to_nearest_road(self):
        navigator = self.navigator
        navigator._nav_instructions[1] = (2, 103, "left onto Nowhere Lane.", 0.0, 0.001)
        navigator._nav_step_forward()
        self.assertEqual(navigator.street_label, "Fairway Drive")

    def test_arrival_context_is_part_of_the_arrival_message(self):
        # A timed "Arrived. Near ..." used to cut the arrival step off.
        navigator = self.navigator
        for _ in range(3):
            navigator._nav_step_forward()
        self.assertTrue(navigator.messages[-1].startswith("Arriving at 19 George Street"))
        self.assertTrue(navigator.messages[-1].endswith("Arrived. On Fairway Drive."))

    def test_step_clears_an_address_chosen_before_the_route(self):
        navigator = self.navigator
        navigator._jump_address_number = "8-20"
        navigator._jump_address_street = "Napier Street"
        navigator._jump_street_pin_lat, navigator._jump_street_pin_lon = 0.0, 0.0
        navigator._nav_step_forward()
        self.assertIsNone(navigator._jump_address_number)
        self.assertIsNone(navigator._jump_street_pin_lat)

    def test_stale_survey_cursor_is_cleared(self):
        navigator = self.navigator
        navigator._street_survey_address_cursor = ("Fairway Drive", "42")
        navigator._nav_step_forward()
        self.assertIsNone(navigator._street_survey_address_cursor)



class CrossingStepTests(unittest.TestCase):
    """At a crossing the nearest road is the cross street; keep the route's."""

    def setUp(self):
        patcher = mock.patch.object(nav.wx, "CallAfter", lambda fn, *a, **k: fn(*a, **k))
        patcher.start()
        self.addCleanup(patcher.stop)
        navigator = _Navigator()
        navigator.street_label = "Beaumont Street"
        navigator._road_segments = [
            {"name": "Beaumont Street", "coords": [(0.0, 0.0), (0.0, 0.002)]},
            {"name": "Lindsay Street", "coords": [(-0.001, 0.001), (0.001, 0.001)]},
        ]
        navigator._walk_graph = {
            "nodes": {0: (0.0, 0.0), 1: (0.0, 0.001), 2: (0.0, 0.002), 3: (0.001, 0.001)},
            "edges": {0: [(1, "Beaumont Street")],
                      1: [(0, "Beaumont Street"), (2, "Beaumont Street"), (3, "Lindsay Street")],
                      2: [(1, "Beaumont Street")], 3: [(1, "Lindsay Street")]},
        }
        navigator._nav_route = [0, 1, 2]
        navigator._nav_instructions = [
            (1, 111, "Continue across Lindsay Street.", 0.0, 0.001),
            (2, 111, "Arriving at 112 Beaumont Street on Beaumont Street.", 0.0, 0.002),
        ]
        navigator._nav_step = 0
        navigator._nearest_road = lambda lat, lon: ("Lindsay Street", "Beaumont Street")
        self.navigator = navigator

    def test_crossing_keeps_the_route_street(self):
        navigator = self.navigator
        navigator._nav_step_forward()
        self.assertEqual(navigator.street_label, "Beaumont Street")
        self.assertEqual(navigator._nav_street_pin, (0.0, 0.001, "Beaumont Street"))

    def test_stepping_back_repeats_the_same_message(self):
        navigator = self.navigator
        navigator._nav_step_poi_text = lambda idx, entry: f"Places for step {idx + 1}."
        navigator._nav_step_forward()
        first = navigator.messages[-1]
        navigator._nav_step_forward()
        navigator._nav_step_back()
        self.assertEqual(navigator.messages[-1], first)
        self.assertTrue(first.endswith("Places for step 1."))



class FirstStepSpeechTests(unittest.TestCase):
    """Step one is spoken, never left in the status line or the focused row."""

    def setUp(self):
        patcher = mock.patch.object(nav.wx, "CallAfter", lambda fn, *a, **k: fn(*a, **k))
        patcher.start()
        self.addCleanup(patcher.stop)
        navigator = _Navigator()
        navigator.spoken = []
        navigator.rows = []
        navigator._announce_transient_then_return = navigator.spoken.append
        navigator._replace_poi_action_item = lambda msg, clear_model=False: navigator.rows.append(msg)
        self.navigator = navigator

    def test_back_to_step_one_is_spoken(self):
        navigator = self.navigator
        navigator._nav_step_forward()           # step two
        navigator.messages.clear()
        navigator._nav_step_back()              # step one
        self.assertEqual((navigator.messages, navigator.rows), ([], []))
        self.assertTrue(navigator.spoken[-1].startswith("In 50 metres, head north"))

    def test_down_at_step_one_repeats_it(self):
        navigator = self.navigator
        navigator._nav_step_back()
        navigator._nav_step_back()
        self.assertEqual((navigator.messages, navigator.rows), ([], []))
        self.assertEqual(len(navigator.spoken), 2)


if __name__ == "__main__":
    unittest.main()
