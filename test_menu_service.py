"""Tests for menu_service: how menu results are listed and menus laid out.
Offline: the server's answers are stubbed."""

import unittest
from unittest import mock

import menu_service as m


class LabelTests(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(
            m.result_label({"kind": "delivery", "url": "https://www.ubereats.com/au/store/x"}, "Sitar"),
            "Sitar on Uber Eats")
        self.assertEqual(
            m.result_label({"kind": "own", "pdf": True, "readable": True, "title": "Food Menu",
                            "url": "https://ahmets.com/menu.pdf"}, "Ahmet's"),
            "Food Menu (PDF) (ahmets.com)")
        self.assertEqual(
            m.result_label({"kind": "own", "readable": True, "title": "Menu",
                            "url": "https://www.sitar.com.au/menu"}, "Sitar"),
            "Menu, can be read here (sitar.com.au)")
        with mock.patch.object(m, "ocr_available", return_value=True):
            self.assertEqual(
                m.result_label({"kind": "photo", "age": "4 months ago", "url": "https://restaurantguru.com/x"}, "A"),
                "Customers' photos of the menu, newest 4 months ago, can be read here (RestaurantGuru)")
            self.assertEqual(
                m.result_label({"kind": "own", "title": "Our Menu", "pictures": 5,
                                "url": "https://kelseycafe.com/menu/coorparoo"}, "The Kelsey Cafe"),
                "Our Menu (5 pictures of the menu), can be read here (kelseycafe.com)")
        # Without Windows' text recognition (a Mac): listed, to open, not to read here.
        with mock.patch.object(m, "ocr_available", return_value=False):
            self.assertEqual(
                m.result_label({"kind": "own", "title": "Our Menu", "pictures": 5,
                                "url": "https://kelseycafe.com/menu/coorparoo"}, "The Kelsey Cafe"),
                "Our Menu (5 pictures of the menu) (kelseycafe.com)")

    def test_updated(self):
        self.assertEqual(m.updated_text("2026-11-12"), "updated 12 Nov 2026")
        self.assertEqual(m.updated_text("2026-10"), "updated Oct 2026")
        self.assertEqual(m.updated_text("2026-13"), "")
        self.assertEqual(m.updated_text(""), "")
        self.assertEqual(
            m.result_label({"kind": "own", "pdf": True, "title": "Menu", "updated": "2026-11-12",
                            "url": "https://ahmets.com/menu.pdf"}, "Ahmet's"),
            "Menu (PDF), updated 12 Nov 2026 (ahmets.com)")

    def test_pdfs_are_opened_not_read(self):
        self.assertFalse(m.can_read_here({"kind": "own", "readable": True, "pdf": True}))
        self.assertFalse(m.can_read_here({"kind": "delivery", "readable": True}))
        self.assertTrue(m.can_read_here({"kind": "own", "readable": True}))

    def test_summary(self):
        results = [
            {"kind": "own", "readable": True, "url": "a"},
            {"kind": "own", "pdf": True, "readable": True, "url": "b"},
            {"kind": "delivery", "url": "c"},
            {"kind": "photo", "url": "d"},
        ]
        self.assertEqual(m.summary(results, "Ahmet's"),
                         "Ahmet's: 1 menu you can read here, 1 other menu page, "
                         "photos of the menu, 1 delivery page.")


class PictureTests(unittest.TestCase):
    def test_rows_join_prices_and_read_columns(self):
        # (left, top, bottom, text) on an 1000-pixel-wide board with two columns.
        lines = [
            (40, 100, 120, "The Kelsey Breakfast"), (400, 101, 119, "32.0"),
            (40, 125, 140, "Bacon, eggs, hash brown"),
            (600, 100, 120, "Flat white"), (900, 100, 120, "5.5"),
            (40, 160, 180, "Eggs your way"), (400, 160, 180, "16.0"),
            (600, 140, 160, "Latte"), (900, 140, 160, "5.5"),
        ]
        self.assertEqual(m._rows(lines, 1000), [
            "The Kelsey Breakfast  32.0", "Bacon, eggs, hash brown", "Eggs your way  16.0",
            "Flat white  5.5", "Latte  5.5",
        ])

    def test_read_result_chooses_pictures_or_page(self):
        with mock.patch.object(m, "read_pictures", return_value=("p", [])) as pics, \
                mock.patch.object(m, "read_menu", return_value=("t", [])) as page:
            m.read_result({"kind": "own", "pictures": 5, "url": "a"})
            m.read_result({"kind": "photo", "url": "b"})
            m.read_result({"kind": "own", "readable": True, "url": "c"})
        self.assertEqual([c.args[0] for c in pics.call_args_list], ["a", "b"])
        self.assertEqual([c.args[0] for c in page.call_args_list], ["c"])


class ReadMenuTests(unittest.TestCase):
    def test_sections_with_prices(self):
        answer = {"sections": [{"title": "Mains", "items": [
            {"name": "Beef burger", "price": "22", "desc": "With chips"},
            {"name": "Fish", "price": "$24.50", "desc": ""},
            {"name": "Salad", "price": "18 ea", "desc": ""},
        ]}], "text": ""}
        with mock.patch.object(m, "_get_json", return_value=answer):
            heading, lines = m.read_menu("https://example.com/menu")
        self.assertEqual(heading, "1 section, 3 dishes")
        self.assertEqual(lines[:4], ["Mains", "    Beef burger, $22. With chips",
                                     "    Fish, $24.50", "    Salad, $18 each"])

    def test_plain_text_when_few_prices(self):
        answer = {"sections": [], "text": "Our menu\n" + "Dish of the day\n" * 20}
        with mock.patch.object(m, "_get_json", return_value=answer):
            heading, lines = m.read_menu("https://example.com/menu")
        self.assertEqual(heading, "")
        self.assertEqual(lines[0], "Our menu")

    def test_unreadable(self):
        with mock.patch.object(m, "_get_json", return_value={"error": "Not readable", "_status": 502}):
            self.assertIsNone(m.read_menu("https://example.com/menu"))
        with mock.patch.object(m, "_get_json", side_effect=m.MenuServiceUnavailable("offline")):
            self.assertIsNone(m.read_menu("https://example.com/menu"))

    def test_find_menus_refused_is_unavailable(self):
        with mock.patch.object(m, "_get_json", return_value={"error": "Expert only", "_status": 403}):
            with self.assertRaises(m.MenuServiceUnavailable):
                m.find_menus("Sitar", "Coorparoo")


if __name__ == "__main__":
    unittest.main()
