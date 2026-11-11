import os
import tempfile
import unittest

import map_symbols
import user_maps


def _symbol(page, x, y, kind):
    """A white symbol box with a black drawing, as airport maps print them."""
    import fitz
    box = fitz.Rect(x, y, x + 9, y + 9)
    page.draw_rect(box, color=(0, 0, 0), fill=(1, 1, 1), width=0.5)
    if kind == "Lift":
        page.draw_rect(fitz.Rect(x + 3, y + 2, x + 6, y + 7), color=(0, 0, 0), fill=(0, 0, 0))
    elif kind == "Public Toilet":
        page.draw_circle((x + 4.5, y + 4.5), 2.5, color=(0, 0, 0), fill=(0, 0, 0))
    else:                                       # Stairs
        page.draw_polyline([(x + 1.5, y + 7.5), (x + 4.5, y + 7.5), (x + 4.5, y + 4.5),
                            (x + 7.5, y + 4.5), (x + 7.5, y + 1.5)], color=(0, 0, 0), width=1.2)


def _airport_pdf(path):
    import fitz
    document = fitz.open()
    page = document.new_page(width=600, height=400)
    page.draw_rect(fitz.Rect(0, 0, 600, 300), color=None, fill=(0.94, 0.94, 0.95))
    for kind, x, y in (("Lift", 100, 100), ("Lift", 300, 120), ("Public Toilet", 200, 80),
                       ("Stairs", 400, 150), ("Public Toilet", 450, 60)):
        _symbol(page, x, y, kind)
    page.insert_text((50, 200), "Mi Casa", fontsize=8)
    for row, kind in enumerate(("Lift", "Public Toilet", "Stairs")):    # the key
        _symbol(page, 400, 320 + row * 14, kind)
        page.insert_text((411, 327 + row * 14), kind, fontsize=8)
    document.save(path)
    document.close()


class SymbolTests(unittest.TestCase):
    def test_symbols_are_named_from_the_key(self):
        with tempfile.TemporaryDirectory() as folder:
            pdf = os.path.join(folder, "airport.pdf")
            _airport_pdf(pdf)
            symbols, key = map_symbols.find_symbols(pdf)
            places, _key = map_symbols.symbol_places(pdf, 0, 600, 400)
        self.assertEqual(key, ["Lift", "Public Toilet", "Stairs"])
        self.assertEqual(sorted(name for name, _x, _y in symbols),
                         ["Lift", "Lift", "Public Toilet", "Public Toilet", "Stairs"])
        lift = min((p for p in places if p["name"] == "Lift"), key=lambda p: p["x"])
        # Centre of the box at (104.5, 104.5) on a 600 by 400 page, north up.
        self.assertAlmostEqual(lift["x"], 104.5, delta=1)
        self.assertAlmostEqual(lift["y"], 400 - 104.5, delta=1)

    def test_a_page_without_a_key_has_no_symbols(self):
        import fitz
        with tempfile.TemporaryDirectory() as folder:
            pdf = os.path.join(folder, "plain.pdf")
            document = fitz.open()
            page = document.new_page(width=600, height=400)
            _symbol(page, 100, 100, "Lift")
            document.save(pdf)
            document.close()
            self.assertEqual(map_symbols.find_symbols(pdf), ([], []))

    def test_counts_read_naturally(self):
        self.assertEqual(
            map_symbols.describe_counts(["Lift"] * 4 + ["Stairs"] * 2 + ["ATM", "ATM"]
                                        + ["Oversize Baggage"] * 3 + ["Prayer Room"]),
            "4 lifts, 3 oversize baggage, 2 ATMs, 2 sets of stairs and 1 prayer room")

    def test_repeated_symbols_are_named_by_the_nearest_shop(self):
        floor = user_maps.new_floor("Floor 1", 100, 100)
        user_maps.add_place(floor, "LEGO", 10, 10)
        user_maps.add_place(floor, "ooaoo", 12, 12)            # a misread label
        user_maps.add_place(floor, "Mi Casa", 80, 80)
        for x, y in ((11, 14), (11, 6), (78, 80)):
            floor["places"].append({"id": f"s{x}{y}", "name": "Public Toilet", "description": "",
                                    "x": x, "y": y, "symbol": "Public Toilet"})
        floor["places"].append({"id": "s-lift", "name": "Lift", "description": "",
                                "x": 50, "y": 50, "symbol": "Lift"})
        user_maps.tell_symbols_apart(floor)
        names = [p["name"] for p in floor["places"] if p.get("symbol")]
        self.assertEqual(names, ["Public Toilet by LEGO", "Public Toilet south of LEGO",
                                 "Public Toilet by Mi Casa", "Lift"])


if __name__ == "__main__":
    unittest.main()
