import unittest
from unittest import mock

from nav import NavMixin


class NavSummaryTests(unittest.TestCase):
    def test_opening_summary_does_not_repeat_destination(self):
        navigator = NavMixin.__new__(NavMixin)
        navigator._nav_instructions = [
            (1, 52, "Slight right onto Old Cleveland Road.", -27.49, 153.06),
            (2, 660, "Arriving at Factory 51.", -27.50, 153.07),
        ]
        navigator._nav_route = []
        navigator._walk_graph = None
        navigator._nav_route_mode = "walking"
        navigator._nav_total_min = 8

        summary = navigator._nav_route_summary("Factory 51")

        self.assertNotIn("Route to", summary)
        self.assertNotIn("Factory 51", summary)
        self.assertIn("Walking, 660m, about 8 min", summary)

    def test_new_route_message_cancels_delayed_older_speech(self):
        navigator = NavMixin.__new__(NavMixin)
        navigator._transient_return_generation = 4
        navigator._transient_message_active_until = 99.0
        messages = []
        navigator.update_ui = lambda message, force=False: messages.append(message)

        navigator._nav_update_ui("Step 2")

        self.assertEqual(navigator._transient_return_generation, 5)
        self.assertEqual(navigator._transient_message_active_until, 0.0)
        self.assertEqual(messages, ["Step 2"])

    def test_repeat_instruction_does_not_follow_with_status_override(self):
        navigator = NavMixin.__new__(NavMixin)
        navigator._nav_active = True
        navigator._nav_step = 1
        navigator._nav_instructions = [
            (1, 52, "Slight right onto Old Cleveland Road.", -27.49, 153.06)
        ]
        messages = []
        navigator._nav_update_ui = messages.append
        navigator._status_update = lambda *args, **kwargs: self.fail(
            "route instruction was overridden by a status message")

        navigator._nav_announce_step()

        self.assertEqual(len(messages), 1)

    def test_route_adds_cached_corridor_places_to_navigation_grid(self):
        navigator = NavMixin.__new__(NavMixin)
        navigator.lat, navigator.lon = -27.4935705, 153.0618904
        navigator.settings = {}
        navigator._poi_fetcher = mock.Mock()
        navigator._poi_fetcher.load_cached_nearby_pois.return_value = [
            {"name": "Little Viet Cafe", "lat": -27.494, "lon": 153.059}
        ]
        remembered = []
        navigator._remember_poi_search_results = remembered.extend

        count = navigator._nav_enrich_poi_grid(-27.49483, 153.05615)

        self.assertEqual(count, 1)
        self.assertEqual(remembered[0]["name"], "Little Viet Cafe")
        navigator._poi_fetcher.load_cached_nearby_pois.assert_called_once()


if __name__ == "__main__":
    unittest.main()
