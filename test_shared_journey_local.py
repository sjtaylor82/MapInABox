import unittest

import pandas as pd

from tools import ToolsMixin, _taxi_order_extras


class LocalSharedJourneyLookupTests(unittest.TestCase):
    def test_australian_suburb_with_state_abbreviation_is_local(self):
        owner = ToolsMixin()
        owner.last_country_found = "Australia"
        owner.df = pd.DataFrame([{
            "city": "Cleveland",
            "admin_name": "Queensland",
            "country": "Australia",
            "lat": -27.526,
            "lng": 153.266,
        }])
        matches = owner._local_geocode_candidates("Cleveland QLD")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0][2], "Cleveland, Queensland, Australia")

    def test_full_street_address_is_not_mistaken_for_a_suburb(self):
        owner = ToolsMixin()
        owner.last_country_found = "Australia"
        owner.df = pd.DataFrame([{
            "city": "Brisbane",
            "admin_name": "Queensland",
            "country": "Australia",
            "lat": -27.47,
            "lng": 153.03,
        }])
        self.assertEqual(
            owner._local_geocode_candidates("10 Queen Street Brisbane"), [])


class TaxiOrderComparisonTests(unittest.TestCase):
    def test_distance_winner_is_baseline_for_both_distance_and_time(self):
        orders = [
            {"route": {"distance_m": 10_000, "duration_s": 1_200}},
            {"route": {"distance_m": 12_000, "duration_s": 900}},
        ]

        self.assertEqual(
            _taxi_order_extras(orders),
            [(0, 0), (2_000, 0)],
        )


if __name__ == "__main__":
    unittest.main()
