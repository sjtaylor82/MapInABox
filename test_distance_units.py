import unittest

from distance_units import (
    format_distance, format_distance_label, format_height, set_unit_system,
)


class DistanceUnitsTests(unittest.TestCase):
    def tearDown(self):
        set_unit_system("metric")

    def test_metric_short_and_long_distances(self):
        set_unit_system("metric")
        self.assertEqual(format_distance(1), "1 metre")
        self.assertEqual(format_distance(999), "999 metres")
        self.assertEqual(format_distance(1000), "1.0 kilometre")
        self.assertEqual(format_distance(12500), "12 kilometres")

    def test_imperial_feet_and_miles(self):
        set_unit_system("imperial")
        self.assertEqual(format_distance(1), "3 feet")
        self.assertEqual(format_distance(41), "130 feet")
        self.assertEqual(format_distance(160.9344), "0.1 miles")
        self.assertEqual(format_distance(1609.344), "1.0 mile")
        self.assertEqual(format_distance(16093.44), "10 miles")

    def test_short_labels(self):
        set_unit_system("metric")
        self.assertEqual(format_distance(80, short=True), "80 m")
        set_unit_system("imperial")
        self.assertEqual(format_distance(80, short=True), "260 ft")

    def test_height_uses_selected_system(self):
        set_unit_system("metric")
        self.assertEqual(format_height(100), "100 metres")
        set_unit_system("imperial")
        self.assertEqual(format_height(100), "328 feet")

    def test_cached_metric_label_is_reformatted_without_fetch(self):
        set_unit_system("imperial")
        label = "Cafe, restaurant, Main Street, 41 metres north  Explorable."
        self.assertEqual(
            format_distance_label(label, 41, "north"),
            "Cafe, restaurant, Main Street, 130 feet north  Explorable.",
        )


if __name__ == "__main__":
    unittest.main()
