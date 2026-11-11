import datetime as dt
import unittest

from murrays import (_parse_live_departures, canonical_stop_name, departures,
                     destinations, find_route, load_network, origins, routes)
from tools import _build_murrays_booking_url


class MurraysTests(unittest.TestCase):
    def test_provider_stop_variants_share_one_canonical_name(self):
        self.assertEqual(canonical_stop_name("BRISBANE DOMESTIC"),
                         "BRISBANE DOMESTIC AIRPORT")
        self.assertEqual(canonical_stop_name("Brisbane Domestic Airport"),
                         "BRISBANE DOMESTIC AIRPORT")
        self.assertEqual(canonical_stop_name("PLAINLAND FROM BNE"), "PLAINLAND")

    def test_route_choices_collapse_equivalent_provider_labels(self):
        network = [
            {"origin": "TOOWOOMBA", "destination": "BRISBANE DOMESTIC"},
            {"origin": "TOOWOOMBA", "destination": "BRISBANE DOMESTIC AIRPORT"},
        ]
        self.assertEqual(destinations(network, "TOOWOOMBA"),
                         ["BRISBANE DOMESTIC AIRPORT"])
    def test_toowoomba_departures_include_nearest_services_to_midday(self):
        rows = departures("Toowoomba", "Brisbane City", dt.date(2026, 11, 10))
        self.assertEqual([row["depart"] for row in rows],
                         ["06:00", "09:00", "11:00", "14:00", "17:00"])
        self.assertTrue(rows[2]["label"].startswith("11:00am departure"))
        self.assertIn("Brisbane City at 12:45pm", rows[2]["label"])
        self.assertNotIn("Murrays departing", rows[2]["label"])

    def test_route_list_is_bidirectional(self):
        self.assertIn(("Toowoomba", "Brisbane City"), routes())
        self.assertIn(("Brisbane City", "Toowoomba"), routes())

    def test_booking_link_selects_toowoomba_route(self):
        self.assertIn("RouteCode=TBE",
                      _build_murrays_booking_url("Toowoomba", "Brisbane City"))

    def test_parses_live_murrays_times_and_lowest_fare(self):
        page = '''<tr class="trChooseTime"><td><time>02:00pm</time></td>
        <td><time>03:45pm</time></td><td><sup>$</sup>37</td>
        <td><sup>$</sup>41</td></tr>'''
        rows = _parse_live_departures(page, "BRISBANE CITY")
        self.assertEqual(rows[0]["depart"], "14:00")
        self.assertEqual(rows[0]["arrive"], "15:45")
        self.assertEqual(rows[0]["price"], 37)
        self.assertIn("from $37", rows[0]["label"])


if __name__ == "__main__":
    unittest.main()
