import unittest
from unittest import mock

import nav
from nav import NavMixin


class _Navigator(NavMixin):
    def __init__(self):
        self.settings = {}
        self._nav_poi_pick_generation = 2
        self._nav_destination_committed = False
        self._poi_list = [{"label": "Factory 51", "lat": -27.49, "lon": 153.06}]


class NavPoiPickerTests(unittest.TestCase):
    def test_delayed_picker_is_ignored_after_destination_is_committed(self):
        navigator = _Navigator()
        with mock.patch.object(nav.wx, "SingleChoiceDialog") as dialog:
            navigator._nav_to_poi_pick(1)
        dialog.assert_not_called()

    def test_untokened_picker_is_ignored_after_destination_is_committed(self):
        navigator = _Navigator()
        navigator._nav_destination_committed = True
        with mock.patch.object(nav.wx, "SingleChoiceDialog") as dialog:
            navigator._nav_to_poi_pick()
        dialog.assert_not_called()


if __name__ == "__main__":
    unittest.main()
