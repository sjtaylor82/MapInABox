import unittest
from unittest import mock

import jump_search
from jump_search import JumpSearchMixin


class _Host(JumpSearchMixin):
    def __init__(self):
        self.settings = {}
        self._current_suburb = "North Sydney"


class AddressHistoryTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(jump_search, "save_settings", lambda settings: True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.host = _Host()

    def test_labels_name_address_and_suburb(self):
        self.host._record_address("7", "Napier Street", -33.84, 151.21)
        self.host._record_address("171", "Pacific Highway", -33.839, 151.207,
                                  name="Vibe Hotel North Sydney")
        labels = [h["label"] for h in self.host.settings["address_history"]]
        self.assertEqual(labels, [
            "Vibe Hotel North Sydney, 171 Pacific Highway, North Sydney",
            "7 Napier Street, North Sydney",
        ])

    def test_a_destination_named_by_its_address_is_not_repeated(self):
        self.host._record_address("19", "George Street", -26.656, 153.094,
                                  name="19 George Street")
        self.assertEqual(self.host.settings["address_history"][0]["label"],
                         "19 George Street, North Sydney")

    def test_revisiting_moves_to_the_top_and_the_list_is_capped(self):
        for number in range(12):
            self.host._record_address(str(number), "Napier Street", -33.84 + number * 0.001, 151.21)
        self.host._record_address("3", "Napier Street", -33.837, 151.21)
        history = self.host.settings["address_history"]
        self.assertEqual(len(history), 10)
        self.assertEqual(history[0]["number"], "3")
        self.assertEqual([h["number"] for h in history].count("3"), 1)


if __name__ == "__main__":
    unittest.main()
