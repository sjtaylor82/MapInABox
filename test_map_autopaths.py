import os
import tempfile
import unittest

import map_autopaths
import user_maps


def _plan(path):
    """An L-shaped light corridor with four rooms off it and a legend below."""
    from PIL import Image, ImageDraw
    image = Image.new("RGB", (800, 600), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((40, 40, 760, 460), fill=(150, 150, 150))        # building shell
    # Corridors 24 pixels wide (3% of the picture), like real floor plans.
    draw.rectangle((60, 218, 740, 242), fill=(242, 242, 242))       # main corridor
    draw.rectangle((398, 242, 422, 440), fill=(242, 242, 242))      # side corridor
    for box in ((80, 60, 300, 210), (420, 60, 720, 210),
                (80, 250, 390, 440), (430, 250, 720, 440)):
        draw.rectangle(box, fill=(225, 230, 235), outline=(150, 150, 150))
    image.save(path)


class AutoPathTests(unittest.TestCase):
    def test_places_off_a_corridor_are_all_reachable(self):
        with tempfile.TemporaryDirectory() as folder:
            picture = os.path.join(folder, "plan.png")
            _plan(picture)
            floor = user_maps.new_floor("Floor 1", 80, 60)

            def at(name, px, py):
                return user_maps.add_place(floor, name, px / 10, 60 - py / 10)

            at("Library", 190, 125)
            at("Hall", 570, 125)
            at("Office", 220, 355)
            at("Gym", 590, 355)
            at("Lift", 410, 400)
            for index in range(5):             # a packed legend block
                at(f"Legend {index}", 120 + index * 30, 540)
            summary = map_autopaths.add_paths_automatically(floor, picture)
        self.assertIsNotNone(summary)
        self.assertEqual(sorted(summary["left_out"]), [f"Legend {i}" for i in range(5)])
        problems = user_maps.check_floor(floor)
        self.assertFalse(any("Library" in p or "Gym" in p or "Lift" in p for p in problems),
                         problems)
        route = user_maps.find_route(floor, floor["places"][0]["id"], floor["places"][3]["id"])
        # Library to Gym goes along the corridor, not straight through rooms.
        self.assertGreater(route.distance, 40)

    def test_a_plain_picture_gives_no_paths(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as folder:
            picture = os.path.join(folder, "blank.png")
            Image.new("RGB", (400, 300), "white").save(picture)
            floor = user_maps.new_floor("Floor 1", 40, 30)
            user_maps.add_place(floor, "A", 10, 10)
            user_maps.add_place(floor, "B", 30, 20)
            self.assertIsNone(map_autopaths.add_paths_automatically(floor, picture))


if __name__ == "__main__":
    unittest.main()
