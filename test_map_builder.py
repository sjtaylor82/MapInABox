import os
import tempfile
import unittest
from unittest import mock

import user_maps


class UnitTests(unittest.TestCase):
    def test_metric_and_imperial_speech(self):
        self.assertEqual(user_maps.format_map_distance(2.5), "2.5 metres")
        self.assertEqual(user_maps.format_map_distance(1.0), "1 metre")
        self.assertEqual(user_maps.format_map_distance(23.4), "23 metres")
        self.assertEqual(user_maps.format_map_distance(2.5, "imperial"), "8 feet 2 inches")
        self.assertEqual(user_maps.format_map_distance(0.3, "imperial"), "1 foot")
        self.assertEqual(user_maps.format_map_distance(20, "imperial"), "66 feet")

    def test_typed_distances(self):
        self.assertEqual(user_maps.parse_distance("12"), 12.0)
        self.assertEqual(user_maps.parse_distance("12,5 m"), 12.5)
        self.assertAlmostEqual(user_maps.parse_distance("10", "imperial"), 3.048, places=3)
        self.assertAlmostEqual(user_maps.parse_distance("6 feet 4 inches"), 1.9304, places=4)
        self.assertAlmostEqual(user_maps.parse_distance("6'4\""), 1.9304, places=4)
        self.assertIsNone(user_maps.parse_distance("near the door"))
        self.assertIsNone(user_maps.parse_distance(""))


class BuildingTests(unittest.TestCase):
    def _school(self):
        floor = user_maps.new_map("School")
        office = user_maps.add_place(floor, "Office", 0, 0)
        x, y = user_maps.position_from(floor, office["id"], 20, "east")
        hall = user_maps.add_place(floor, "Hall", x, y)
        user_maps.connect_places(floor, office["id"], hall["id"])
        x, y = user_maps.position_from(floor, hall["id"], 10, "north")
        library = user_maps.add_place(floor, "Library", x, y, "Quiet please")
        return floor, office, hall, library

    def test_places_by_distance_and_direction(self):
        floor, office, hall, library = self._school()
        self.assertEqual((hall["x"], hall["y"]), (20.0, 0.0))
        self.assertAlmostEqual(library["x"], 20.0)
        self.assertAlmostEqual(library["y"], 10.0)
        self.assertEqual(floor["start"], office["id"])
        self.assertEqual(user_maps.describe_place(floor, library),
                         "Library, 22 metres north-east of Office. Quiet please")

    def test_check_finds_unreachable_places_until_joined(self):
        floor, _office, hall, library = self._school()
        problems = user_maps.check_floor(floor)
        self.assertEqual(len(problems), 1)
        self.assertIn("Library", problems[0])
        user_maps.connect_places(floor, hall["id"], library["id"])
        self.assertEqual(user_maps.check_floor(floor), [])
        route = user_maps.find_route(floor, floor["start"], library["id"])
        self.assertAlmostEqual(route.distance, 30.0)

    def test_moving_a_place_carries_its_paths(self):
        floor, office, hall, _library = self._school()
        user_maps.move_place(floor, hall["id"], 25, 0)
        self.assertEqual(floor["paths"][0]["points"][-1], [25.0, 0.0])
        self.assertEqual(user_maps.describe_path(floor, floor["paths"][0]),
                         "Office to Hall, 25 metres")

    def test_fitting_keeps_a_margin_around_negative_positions(self):
        floor, office, _hall, _library = self._school()
        user_maps.add_place(floor, "Gate", -10, -4)
        user_maps.fit_to_content(floor)
        xs = [p["x"] for p in floor["places"]]
        ys = [p["y"] for p in floor["places"]]
        self.assertAlmostEqual(min(xs), user_maps.FIT_MARGIN_M)
        self.assertAlmostEqual(min(ys), user_maps.FIT_MARGIN_M)
        self.assertAlmostEqual(floor["width"], 30 + 2 * user_maps.FIT_MARGIN_M)

    def test_deleting_the_start_moves_it(self):
        floor, office, hall, _library = self._school()
        user_maps.delete_place(floor, office["id"])
        self.assertEqual(floor["start"], hall["id"])

    def test_adding_a_floor_keeps_the_first_one(self):
        floor, *_ = self._school()
        second = user_maps.add_floor(floor, "Upstairs")
        floors = user_maps.floors_for(floor)
        self.assertEqual([f["name"] for f in floors], ["Floor 1", "Upstairs"])
        self.assertEqual(len(floors[0]["places"]), 3)
        self.assertIs(floors[1], second)
        user_maps.validate_map(floor)

    def test_scaling_to_a_real_width(self):
        floor = user_maps.new_floor("Floor 1", 500, 250)
        user_maps.add_place(floor, "Door", 250, 125)
        user_maps.scale_floor(floor, 40 / 500)
        self.assertEqual((floor["width"], floor["height"]), (40.0, 20.0))
        self.assertEqual((floor["places"][0]["x"], floor["places"][0]["y"]), (20.0, 10.0))


class BuilderWindowTests(unittest.TestCase):
    """Drive the real builder window without showing it."""

    @classmethod
    def setUpClass(cls):
        import wx
        cls.app = wx.App(False)

    def _builder(self, data=None):
        import user_map_dialogs
        dialog = user_map_dialogs.MapDrawerDialog(None, data or user_maps.new_map("School"))
        self.addCleanup(dialog.Destroy)
        return dialog

    def _answer(self, result):
        import user_map_dialogs

        class Answer:
            def __init__(self, *args, **kwargs):
                self.result = result

            def ShowModal(self):
                import wx
                return wx.ID_OK

            def Destroy(self):
                pass
        return mock.patch.object(user_map_dialogs, "PlaceDialog", Answer)

    def test_teacher_builds_a_reachable_map_by_keyboard(self):
        builder = self._builder()
        with mock.patch("user_map_dialogs._say"):
            with self._answer({"name": "Office", "description": ""}):
                builder.add_place()
            office = builder.floor["places"][0]["id"]
            with self._answer({"name": "Hall", "description": "", "reference": office,
                               "distance": 20.0, "direction": "east", "connect": True}):
                builder.add_place()
        self.assertEqual(builder.place_list.GetCount(), 2)
        self.assertEqual(builder.place_list.GetString(1), "Hall, 20 metres east of Office")
        self.assertEqual(builder.path_list.GetString(0), "Office to Hall, 20 metres")
        self.assertEqual(builder._problems(), [])
        self.assertTrue(builder.dirty)
        builder.undo()
        self.assertEqual(builder.place_list.GetCount(), 1)

    def test_closing_with_changes_asks_to_save(self):
        builder = self._builder()
        builder.dirty = True
        import wx
        with mock.patch("user_map_dialogs.wx.MessageBox", return_value=wx.CANCEL) as box, \
                mock.patch.object(builder, "EndModal") as end:
            builder.close()
        box.assert_called_once()
        end.assert_not_called()

    def test_saving_writes_a_map_students_can_open(self):
        builder = self._builder()
        floor = builder.floor
        a = user_maps.add_place(floor, "Office", 5, 5)
        b = user_maps.add_place(floor, "Hall", 25, 5)
        user_maps.connect_places(floor, a["id"], b["id"])
        builder.dirty = True
        with tempfile.TemporaryDirectory() as folder:
            builder.save_path = os.path.join(folder, "school.miabmap")
            with mock.patch("user_map_dialogs._say"):
                self.assertTrue(builder.save())
            loaded, _background = user_maps.load_map(builder.save_path, folder)
        self.assertFalse(builder.dirty)
        self.assertEqual([p["name"] for p in loaded["places"]], ["Office", "Hall"])



def _two_floor_school():
    data = user_maps.new_map("School")
    ground = data
    office = user_maps.add_place(ground, "Office", 5, 5)
    stairs = user_maps.add_place(ground, "Stairwell", 25, 5)
    user_maps.connect_places(ground, office["id"], stairs["id"])
    upstairs = user_maps.add_floor(data, "Level 1")
    landing = user_maps.add_place(upstairs, "Landing", 25, 5)
    library = user_maps.add_place(upstairs, "Library", 25, 20)
    user_maps.connect_places(upstairs, landing["id"], library["id"])
    return data, office, stairs, landing, library


class FloorLinkTests(unittest.TestCase):
    def test_route_takes_the_stairs(self):
        data, office, stairs, landing, library = _two_floor_school()
        self.assertEqual(len(user_maps.check_links(data)), 1)
        user_maps.add_link(data, "stairs", 0, stairs["id"], 1, landing["id"])
        self.assertEqual(user_maps.check_links(data), [])
        route = user_maps.find_route_between_floors(data, (0, office["id"]), (1, library["id"]))
        self.assertEqual([floor for floor, _leg in route.legs], [0, 1])
        self.assertAlmostEqual(route.distance, 35.0)
        lines = user_maps.multi_floor_directions(data, route)
        self.assertIn("Take the stairs to Level 1.", lines)
        self.assertEqual(lines[0], "On Floor 1:")
        self.assertTrue(lines[-1].endswith("to Library."))

    def test_unlinked_floors_have_no_route(self):
        data, office, _stairs, _landing, library = _two_floor_school()
        with self.assertRaises(ValueError):
            user_maps.find_route_between_floors(data, (0, office["id"]), (1, library["id"]))

    def test_same_floor_route_is_unchanged(self):
        data, office, stairs, *_ = _two_floor_school()
        route = user_maps.find_route_between_floors(data, (0, office["id"]), (0, stairs["id"]))
        self.assertEqual(len(route.legs), 1)
        self.assertEqual(route.changes, [])

    def test_links_to_deleted_places_go_and_survive_saving(self):
        data, _office, stairs, landing, _library = _two_floor_school()
        user_maps.add_link(data, "lift", 0, stairs["id"], 1, landing["id"])
        with tempfile.TemporaryDirectory() as folder:
            path = user_maps.save_map(os.path.join(folder, "school.miabmap"), data, ["", ""])
            loaded, _background = user_maps.load_map(path, folder)
        self.assertEqual(user_maps.describe_link(loaded, loaded["links"][0]),
                         "Lift: Stairwell (Floor 1) to Landing (Level 1)")
        user_maps.remove_links_to_place(loaded, 1, landing["id"])
        self.assertEqual(loaded["links"], [])

    def test_bad_links_are_dropped_on_load(self):
        data, _office, stairs, _landing, _library = _two_floor_school()
        data["links"] = [{"id": "x", "kind": "stairs",
                          "a": {"floor": 0, "place": stairs["id"]},
                          "b": {"floor": 5, "place": "nowhere"}}]
        user_maps.validate_map(data)
        self.assertEqual(data["links"], [])


class StudentFloorChangeTests(unittest.TestCase):
    def test_stairs_at_the_student_lead_to_their_landing(self):
        import core
        data, _office, stairs, landing, _library = _two_floor_school()
        user_maps.add_link(data, "stairs", 0, stairs["id"], 1, landing["id"])

        class Student:
            _user_map_link_here = core.MapNavigator._user_map_link_here

        student = Student()
        student._user_map_data = data
        student._user_map_floor = user_maps.floors_for(data)[0]
        student._user_map_floor_index = 0
        student.lat, student.lon = 5 / 111195.0, 25 / 111195.0
        link, other = student._user_map_link_here(+1)
        self.assertEqual((link["kind"], other["place"]), ("stairs", landing["id"]))
        self.assertEqual(student._user_map_link_here(-1), (None, None))
        student.lat, student.lon = 5 / 111195.0, 5 / 111195.0
        self.assertEqual(student._user_map_link_here(+1), (None, None))


class BuilderLinkTests(BuilderWindowTests):
    def test_teacher_adds_a_lift_with_a_new_landing(self):
        import user_map_dialogs
        data, _office, stairs, _landing, _library = _two_floor_school()
        builder = self._builder(data)
        builder.place_list.SetSelection(1)

        class Answer:
            def __init__(self, *args, **kwargs):
                self.result = {"kind": "lift", "place": stairs["id"], "floor": 1, "arrival": None}

            def ShowModal(self):
                import wx
                return wx.ID_OK

            def Destroy(self):
                pass

        with mock.patch.object(user_map_dialogs, "LinkDialog", Answer),                 mock.patch("user_map_dialogs._say"):
            builder.add_link()
        upstairs = user_maps.floors_for(builder.data)[1]
        self.assertEqual([p["name"] for p in upstairs["places"]], ["Landing", "Library", "Lift"])
        self.assertEqual(builder.link_list.GetString(0), "Lift: Stairwell (Floor 1) to Lift (Level 1)")
        builder.undo()
        self.assertEqual(builder.link_list.GetCount(), 0)



class LabelReadingTests(unittest.TestCase):
    def test_misread_shop_codes_are_fixed(self):
        self.assertEqual([user_maps._fix_shop_code(t) for t in ("KIO", "KII", "F3", "Kmart")],
                         ["K10", "K11", "F3", "Kmart"])

    def test_garbled_readings_are_dropped(self):
        keep = [t for t in ("Target", "7", "Food Court", "Wodv.ortt6", "�,07E'C", "X")
                if user_maps._readable_label(t)]
        self.assertEqual(keep, ["Target", "7", "Food Court"])

    def test_enlargement_suits_the_picture(self):
        self.assertEqual([s for s, _c in user_maps._windows_ocr_passes(950)], [4, 3, 1])
        self.assertEqual([s for s, _c in user_maps._windows_ocr_passes(2105)], [2, 1])
        self.assertEqual([s for s, _c in user_maps._windows_ocr_passes(5000)], [1])

    def test_ocr_adds_only_what_the_pdf_text_lacks(self):
        pdf = [{"id": "p-1", "name": "GATE 1", "description": "", "x": 100, "y": 100}]
        ocr = [{"id": "i-1", "name": "Gate 1", "description": "", "x": 300, "y": 300},
               {"id": "i-2", "name": "O GATE 1", "description": "", "x": 101, "y": 100},
               {"id": "i-3", "name": "Train Station", "description": "", "x": 300, "y": 50}]
        merged = user_maps.merge_labels(pdf, ocr, 500, "p")
        self.assertEqual([l["name"] for l in merged], ["GATE 1", "Train Station"])

    def test_windows_reads_a_drawn_floor_plan(self):
        try:
            import winrt.windows.media.ocr  # noqa: F401
        except ImportError:
            self.skipTest("Windows OCR is not installed")
        from PIL import Image, ImageDraw, ImageFont
        image = Image.new("RGB", (800, 500), "white")
        draw = ImageDraw.Draw(image)
        font = ImageFont.truetype("arial.ttf", 22)
        draw.text((80, 80), "Library", fill="black", font=font)
        draw.text((520, 380), "Staff Room", fill="black", font=font)
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "plan.png")
            image.save(path)
            labels = user_maps.detect_image_labels(path, 40, 25)
        found = {l["name"]: l for l in labels}
        self.assertIn("Library", found)
        self.assertIn("Staff Room", found)
        # Positions are in map metres with north up: the library is top left.
        self.assertLess(found["Library"]["x"], 10)
        self.assertGreater(found["Library"]["y"], 18)
        self.assertGreater(found["Staff Room"]["x"], 25)
        self.assertLess(found["Staff Room"]["y"], 7)



class RealDialogTests(unittest.TestCase):
    """Build the actual small dialogs, fill them in and press OK.

    Earlier tests replaced these dialogs with stand-ins, which hid a layout
    error that stopped them opening at all.
    """

    @classmethod
    def setUpClass(cls):
        import wx
        cls.app = wx.App(False)

    def _press_ok(self, dialog):
        import wx
        self.addCleanup(dialog.Destroy)
        with mock.patch.object(dialog, "EndModal") as end,                 mock.patch("user_map_dialogs.wx.MessageBox") as box:
            dialog._ok(None)
        return end, box

    def test_first_place(self):
        import user_map_dialogs
        floor = user_maps.new_map("School")
        dialog = user_map_dialogs.PlaceDialog(None, floor, "metric", mode="add")
        dialog.name.SetValue("Office")
        end, box = self._press_ok(dialog)
        end.assert_called_once()
        box.assert_not_called()
        self.assertEqual(dialog.result, {"name": "Office", "description": ""})

    def test_next_place_by_distance(self):
        import user_map_dialogs
        floor = user_maps.new_map("School")
        office = user_maps.add_place(floor, "Office", 5, 5)
        dialog = user_map_dialogs.PlaceDialog(None, floor, "imperial", mode="add")
        dialog.name.SetValue("Hall")
        dialog.distance.SetValue("6 feet 4 inches")
        dialog.direction.SetSelection(2)   # east
        self._press_ok(dialog)
        self.assertEqual(dialog.result["reference"], office["id"])
        self.assertEqual(dialog.result["direction"], "east")
        self.assertAlmostEqual(dialog.result["distance"], 1.9304, places=4)
        self.assertTrue(dialog.result["connect"])

    def test_a_bad_distance_is_explained_not_accepted(self):
        import user_map_dialogs
        floor = user_maps.new_map("School")
        user_maps.add_place(floor, "Office", 5, 5)
        dialog = user_map_dialogs.PlaceDialog(None, floor, "metric", mode="add")
        dialog.name.SetValue("Hall")
        dialog.distance.SetValue("near the door")
        end, box = self._press_ok(dialog)
        end.assert_not_called()
        box.assert_called_once()

    def test_edit_and_move(self):
        import user_map_dialogs
        floor = user_maps.new_map("School")
        office = user_maps.add_place(floor, "Office", 5, 5)
        hall = user_maps.add_place(floor, "Hall", 25, 5)
        edit = user_map_dialogs.PlaceDialog(None, floor, "metric", mode="edit", place=hall)
        edit.description.SetValue("Assemblies")
        self._press_ok(edit)
        self.assertEqual(edit.result, {"name": "Hall", "description": "Assemblies"})
        move = user_map_dialogs.PlaceDialog(None, floor, "metric", mode="move", place=hall)
        move.distance.SetValue("30")
        self._press_ok(move)
        self.assertEqual((move.result["reference"], move.result["distance"]), (office["id"], 30.0))

    def test_join_two_places(self):
        import user_map_dialogs
        floor = user_maps.new_map("School")
        a = user_maps.add_place(floor, "Office", 5, 5)
        b = user_maps.add_place(floor, "Hall", 25, 5)
        dialog = user_map_dialogs.ConnectDialog(None, floor["places"])
        self._press_ok(dialog)
        self.assertEqual(dialog.result, (a["id"], b["id"]))

    def test_stairs_dialog(self):
        import user_map_dialogs
        data, _office, stairs, landing, _library = _two_floor_school()
        builder = user_map_dialogs.MapDrawerDialog(None, data)
        self.addCleanup(builder.Destroy)
        builder.place_list.SetSelection(1)
        dialog = user_map_dialogs.LinkDialog(builder, data, 0)
        dialog.arrival.SetSelection(1)     # Landing
        self._press_ok(dialog)
        self.assertEqual(dialog.result, {"kind": "stairs", "place": stairs["id"],
                                         "floor": 1, "arrival": landing["id"]})



class AutoPathButtonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import wx
        cls.app = wx.App(False)

    def test_button_joins_places_on_a_traced_floor_and_undo_removes_them(self):
        import user_map_dialogs
        from test_map_autopaths import _plan
        with tempfile.TemporaryDirectory() as folder:
            picture = os.path.join(folder, "plan.png")
            _plan(picture)
            data = user_maps.new_map("School", 80, 60)
            for name, px, py in (("Library", 190, 125), ("Hall", 570, 125),
                                 ("Office", 220, 355), ("Gym", 590, 355)):
                user_maps.add_place(data, name, px / 10, 60 - py / 10)
            builder = user_map_dialogs.MapDrawerDialog(None, data, picture)
            self.addCleanup(builder.Destroy)
            with mock.patch("user_map_dialogs._say") as say:
                builder.add_paths_automatically()
            message = say.call_args[0][0]
        self.assertIn("joined 4 of 4 places", message)
        self.assertIn("Every place can be reached", message)
        self.assertGreater(builder.path_list.GetCount(), 4)
        builder.undo()
        self.assertEqual(builder.path_list.GetCount(), 0)

    def test_button_explains_itself_on_a_floor_without_a_picture(self):
        import user_map_dialogs
        data = user_maps.new_map("School")
        user_maps.add_place(data, "Office", 5, 5)
        builder = user_map_dialogs.MapDrawerDialog(None, data)
        self.addCleanup(builder.Destroy)
        with mock.patch("user_map_dialogs._say") as say:
            builder.add_paths_automatically()
        self.assertIn("imported from a PDF or picture", say.call_args[0][0])
        self.assertEqual(builder.undo_stack, [])


class LegendTests(unittest.TestCase):
    def test_legend_names_numbers_and_drops_its_own_text(self):
        floor = user_maps.new_floor("Floor 1", 100, 100)
        for name, x, y in (("50", 10, 10), ("52", 30, 10), ("20", 12, 14), ("20", 80, 60),
                           ("53", 82, 64), ("Nippon Ramen", 90, 95)):   # the legend's own line
            user_maps.add_place(floor, name, x, y)
        text = "50.\tMi Casa\n52.\tNippon Ramen\n53.\tRoll'd\n20.\tnews@bne\n"
        keyed = user_maps.new_floor("Floor 1", 100, 100)
        keyed["places"] = [dict(p) for p in floor["places"]]
        self.assertTrue(user_maps.has_legend(floor, text))
        named, removed = user_maps.apply_legend(floor, text)
        self.assertEqual((named, removed), (5, 1))
        names = sorted(p["name"] for p in floor["places"])
        # Two outlets share number 20; each is named by its neighbour.
        self.assertEqual(names, ["Mi Casa", "Nippon Ramen", "Roll'd",
                                 "news@bne by Mi Casa", "news@bne by Roll'd"])
        self.assertEqual(user_maps.place_by_id(floor, floor["places"][0]["id"])["key"], "50")
        self.assertFalse(any("called" in p for p in user_maps.check_floor(floor)))
        # The creator can ask for the key numbers too.
        user_maps.apply_legend(keyed, text, show_keys=True)
        self.assertIn("Mi Casa (key 50)", [p["name"] for p in keyed["places"]])
        self.assertIn("news@bne (key 20) by Mi Casa (key 50)", [p["name"] for p in keyed["places"]])


class NameAndSizeGuessTests(unittest.TestCase):
    def _pdf(self, folder, filename, lines, title=""):
        import fitz
        path = os.path.join(folder, filename)
        document = fitz.open()
        page = document.new_page(width=842, height=595)
        for text, size, y in lines:
            page.insert_text((40, y), text, fontsize=size)
        if title:
            document.set_metadata({"title": title})
        document.save(path)
        document.close()
        return path

    def test_title_comes_from_the_biggest_text_with_the_place_from_the_file_name(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self._pdf(folder, "Brisbane-Airport-Terminal-Map.pdf",
                             [("Domestic Terminal", 24, 60), ("Level 2", 24, 90),
                              ("Gate 22", 8, 300), ("Gate 23", 8, 320), ("Toilets", 8, 340)])
            self.assertEqual(user_maps.suggest_map_name(path),
                             "Brisbane Airport Domestic Terminal Level 2")
            titled = self._pdf(folder, "x.pdf", [("Gate 1", 8, 300)], title="Riverside Campus")
            self.assertEqual(user_maps.suggest_map_name(titled), "Riverside Campus")

    def test_file_names_with_no_meaning_give_no_name(self):
        self.assertEqual(user_maps.title_from_file("C:/maps/scan0001.pdf"), "")
        self.assertEqual(user_maps.title_from_file("IMG_2041.jpg"), "")
        self.assertEqual(user_maps.title_from_file("capalaba-park-map.png"), "Capalaba Park Map")

    def test_a_printed_scale_gives_the_width(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self._pdf(folder, "plan.pdf", [("Ground floor  Scale 1:500", 10, 60)])
            # An A4 landscape page, 297 mm wide, at 1:500 is about 148.5 m.
            self.assertAlmostEqual(user_maps.pdf_scale_width(path), 148.5, delta=0.5)
            plain = self._pdf(folder, "plain.pdf", [("Ground floor", 10, 60)])
            self.assertIsNone(user_maps.pdf_scale_width(plain))

    def test_label_spacing_gives_a_rough_width(self):
        floor = user_maps.new_floor("Floor 1", 500, 300)
        for row in range(4):
            for column in range(5):     # shops 20 units apart on a 500-unit plan
                user_maps.add_place(floor, f"Shop {row}{column}", 100 + column * 20, 100 + row * 20)
        # 12 m apart, so the plan is 500 / 20 * 12 = 300 m wide.
        self.assertEqual(user_maps.estimate_width_from_labels(floor), 300)
        few = user_maps.new_floor("Floor 1", 500, 300)
        user_maps.add_place(few, "A", 10, 10)
        self.assertIsNone(user_maps.estimate_width_from_labels(few))


class StudentKeyTests(unittest.TestCase):
    """Keys on an opened map file describe the map, never the world at 0, 0."""

    def _student(self):
        import core
        data = user_maps.new_map("Airport", 300, 100)
        a = user_maps.add_place(data, "Mi Casa (50)", 100, 50)
        b = user_maps.add_place(data, "Gate 22", 200, 50)
        user_maps.connect_places(data, a["id"], b["id"])
        data["start"] = a["id"]

        class Student:
            _handle_user_map_shortcuts = core.MapNavigator._handle_user_map_shortcuts

        student = Student()
        student._user_map_data = data
        student._user_map_floor = user_maps.floors_for(data)[0]
        student._user_map_floor_index = 0
        student._user_map_node = None
        student.lat, student.lon = 50 / 111195.0, 100 / 111195.0
        student.said = []
        student._status_update = lambda text, force=False: student.said.append(text)
        student.map_panel = mock.Mock()
        student.sound = mock.Mock()
        student._play_spatial_tone_if_allowed = lambda *a: None
        return student

    def test_r_says_where_you_are_on_the_map(self):
        import wx
        student = self._student()
        self.assertTrue(student._handle_user_map_shortcuts(ord("R"), False, False, False))
        self.assertIn("Airport, Floor 1. At Mi Casa (50).", student.said[-1])
        self.assertIn("Gate 22", student.said[-1])
        self.assertNotIn("Ghana", student.said[-1])

    def test_world_keys_explain_instead_of_describing_the_sea(self):
        import wx
        student = self._student()
        self.assertTrue(student._handle_user_map_shortcuts(ord("W"), False, False, False))
        self.assertTrue(student._handle_user_map_shortcuts(wx.WXK_F8, False, False, False))
        self.assertIn("world map", student.said[-1])

    def test_an_empty_square_names_the_nearest_place(self):
        import wx
        student = self._student()
        student._handle_user_map_shortcuts(wx.WXK_RIGHT, False, False, False)
        self.assertRegex(student.said[-1], r"^Walkway\. Near Mi Casa \(50\), [\d.]+ metres west\.$")


class DrawingAreaFocusTests(unittest.TestCase):
    def test_tab_skips_the_drawing_area(self):
        import wx
        from user_map_dialogs import MapDrawCanvas
        _app = wx.GetApp() or wx.App(False)  # left running: closing wx breaks later OCR tests
        frame = wx.Frame(None)
        canvas = MapDrawCanvas(frame, None, user_maps.new_map("t"))
        self.assertFalse(canvas.AcceptsFocusFromKeyboard())
        frame.Destroy()


class DirectionsDialogTests(unittest.TestCase):
    def test_from_starts_where_the_student_is_and_focus_moves_to_the_directions(self):
        import wx
        from user_map_dialogs import LocalRouteDialog
        _app = wx.GetApp() or wx.App(False)  # left running: closing wx breaks later OCR tests
        data = user_maps.new_map("t", 300, 100)
        a = user_maps.add_place(data, "Mi Casa", 100, 50)
        b = user_maps.add_place(data, "Gate 22", 200, 50)
        user_maps.connect_places(data, a["id"], b["id"])
        dialog = LocalRouteDialog(None, data, here=b["id"])
        try:
            self.assertEqual(dialog.origin.GetStringSelection(), "Gate 22")
            self.assertEqual(dialog.destination.GetStringSelection(), "Mi Casa")
            dialog.Show()
            dialog._find()
            self.assertIn("100 metres west", dialog.result.GetValue())
            self.assertIs(wx.Window.FindFocus(), dialog.result)
        finally:
            dialog.Destroy()


class OneStepImportTests(unittest.TestCase):
    """Import PDF joins the places, drops label clutter and saves, with no
    questions about walkways or leftover text."""

    def _navigator(self):
        import wx
        import core
        # Kept for the whole run: closing wx breaks later OCR tests.
        OneStepImportTests.app = wx.GetApp() or wx.App(False)

        class Navigator(wx.Frame):
            MAP_SOURCE_WILDCARD = core.MapNavigator.MAP_SOURCE_WILDCARD
            settings = {}
            _map_from_source = core.MapNavigator._map_from_source
            _finish_imported_map = core.MapNavigator._finish_imported_map
            opened = None

            def _open_user_map(self, path=None):
                self.opened = path

            def _focus_map_window_silently(self):
                pass

        return Navigator(None)

    def test_places_are_joined_and_clutter_dropped(self):
        from test_map_autopaths import _plan
        navigator = self._navigator()
        with tempfile.TemporaryDirectory() as folder:
            picture = os.path.join(folder, "plan.png")
            _plan(picture)
            data = user_maps.new_map("School", 80, 60)
            for name, px, py in (("Library", 190, 125), ("Hall", 570, 125),
                                 ("Office", 220, 355), ("Gym", 590, 355)):
                user_maps.add_place(data, name, px / 10, 60 - py / 10)
            for index in range(5):             # title and legend text
                user_maps.add_place(data, f"Legend {index}", 12 + index * 3, 6)
            message = navigator._finish_imported_map(data, [picture])
        navigator.Destroy()
        self.assertEqual(message, "Made the map School: 4 places joined by walkways.")
        self.assertEqual(sorted(p["name"] for p in data["places"]),
                         ["Gym", "Hall", "Library", "Office"])
        self.assertEqual(user_maps.check_floor(data), [])

    def test_a_map_already_made_opens_instead_of_importing_again(self):
        navigator = self._navigator()
        with tempfile.TemporaryDirectory() as folder:
            pdf = os.path.join(folder, "Airport.pdf")
            made = os.path.join(folder, "Airport.miabmap")
            open(made, "w").close()
            navigator._map_from_source(pdf, ask_first=False)
        navigator.Destroy()
        self.assertEqual(navigator.opened, made)


class MouseDrawingTests(unittest.TestCase):
    """A sighted teacher draws a classroom from scratch with the mouse."""

    def test_clicks_and_drags_make_a_map_directions_can_use(self):
        import wx
        import user_map_dialogs as umd
        MouseDrawingTests.app = wx.GetApp() or wx.App(False)  # kept: see OneStepImportTests
        names = iter(["Door", "Desk", "Sink", "Reading corner"])

        class Size:
            def __init__(self, *a, **k): pass
            def ShowModal(self): return wx.ID_OK
            def GetValue(self): return "30"
            def Destroy(self): pass

        class Place:
            def __init__(self, *a, **k): self.result = {"name": next(names), "description": ""}
            def ShowModal(self): return wx.ID_OK
            def Destroy(self): pass

        dialog = umd.MapDrawerDialog(None, user_maps.new_map("Classroom"), "")
        dialog.SetSize((900, 820))
        dialog.Show()
        wx.Yield()
        canvas = dialog.canvas
        width, height = canvas.GetClientSize()

        def at(fx, fy):
            return canvas._to_map(width * fx, height * fy)

        try:
            with mock.patch.object(wx, "TextEntryDialog", Size), \
                    mock.patch.object(umd, "PlaceDialog", Place):
                dialog.tool_choice.SetSelection(1)       # Click to add a place
                dialog._change_tool()
                self.assertEqual(dialog.floor["width"], 30)
                dialog.add_place_at(*at(0.1, 0.3))
                dialog.add_place_at(*at(0.9, 0.3))
                # A corridor drawn from just beside the door to just beside the desk.
                dialog.add_drawn_line("path", [at(0.11, 0.32), at(0.5, 0.3), at(0.89, 0.28)])
                self.assertIn("joined to Door, Desk", dialog.status.GetLabel())
                # A side corridor starting just off the first one.
                dialog.add_drawn_line("path", [at(0.5, 0.33), at(0.5, 0.8)])
                self.assertIn("another path to open space", dialog.status.GetLabel())
                dialog.add_place_at(*at(0.51, 0.81))         # beside its end
                self.assertIn("joined to the path beside it", dialog.status.GetLabel())
                dialog.add_place_at(*at(0.1, 0.9))           # nowhere near a path
            self.assertEqual(dialog.floor["width"], 30)      # the drawing stayed still
            problems = user_maps.check_floor(dialog.floor)
            self.assertEqual(len(problems), 1)
            self.assertIn("Reading corner", problems[0])
            ids = {p["name"]: p["id"] for p in dialog.floor["places"]}
            self.assertIsNotNone(user_maps.find_route(dialog.floor, ids["Door"], ids["Sink"]))
        finally:
            dialog.Destroy()


class MouseToolTests(unittest.TestCase):
    """The Draw and Move tools, driven by mouse events."""

    class Event:
        def __init__(self, x, y):
            self.x, self.y = int(x), int(y)
        def GetX(self): return self.x
        def GetY(self): return self.y
        def Dragging(self): return True
        def LeftIsDown(self): return True

    def setUp(self):
        import wx
        import user_map_dialogs as umd
        MouseToolTests.app = wx.GetApp() or wx.App(False)  # kept: see OneStepImportTests
        self.umd, self.wx = umd, wx
        self.dialog = umd.MapDrawerDialog(None, user_maps.new_map("Centre"), "")
        self.dialog.SetSize((900, 820))
        self.dialog.Show()
        wx.Yield()
        self.canvas = self.dialog.canvas
        self.canvas.CaptureMouse = lambda: None
        self.canvas.HasCapture = lambda: False
        self.dialog.floor["fixed_size"] = True          # the size was given

    def tearDown(self):
        self.dialog.Destroy()

    def _px(self, place):
        return self.canvas._to_px(place["x"], place["y"])

    def _click(self, x, y):
        self.canvas._down(self.Event(x, y))
        self.canvas._up(self.Event(x, y))

    def _drag(self, *points):
        self.canvas._down(self.Event(*points[0]))
        for point in points[1:]:
            self.canvas._motion(self.Event(*point))
        self.canvas._up(self.Event(*points[-1]))

    def test_draw_tool_clicks_add_and_select_and_drags_draw(self):
        names = iter(["Entrance", "Kmart"])

        class Place:
            def __init__(self, *a, **k): self.result = {"name": next(names), "description": ""}
            def ShowModal(self): return self.wx.ID_OK
            def Destroy(self): pass
        Place.wx = self.wx
        self.dialog.tool_choice.SetSelection(1)
        self.dialog._change_tool()
        with mock.patch.object(self.umd, "PlaceDialog", Place):
            self._click(100, 100)
            self._click(400, 100)
        entrance, kmart = self.dialog.floor["places"]
        self._drag(self._px(entrance), (250, 102), self._px(kmart))
        self.assertIn("joined to Entrance, Kmart", self.dialog.status.GetLabel())
        self._click(*self._px(entrance))                      # a click on a place picks it
        self.assertEqual(self.dialog.selected_place_id(), entrance["id"])
        self.assertEqual(len(self.dialog.floor["places"]), 2)
        self.assertEqual(user_maps.check_floor(self.dialog.floor), [])

    def test_move_tool_drags_a_place_and_its_path_follows(self):
        floor = self.dialog.floor
        a = user_maps.add_place(floor, "A", 100, 100)
        b = user_maps.add_place(floor, "B", 300, 100)
        user_maps.connect_places(floor, a["id"], b["id"])
        self.dialog._refresh()
        self.dialog.tool_choice.SetSelection(2)
        self.dialog._change_tool()
        start = self._px(b)
        self._drag(start, (start[0], start[1] + 20), (start[0], start[1] + 40))
        self.assertLess(b["y"], 100)                          # moved down the screen
        self.assertEqual(user_maps.check_floor(floor), [])

    def test_right_click_finds_paths_and_walls_and_deletes_them(self):
        floor = self.dialog.floor
        floor["paths"].append({"points": [[100, 100], [300, 100]]})
        floor["barriers"].append({"points": [[100, 200], [300, 200]]})
        self.dialog._refresh()
        x, y = self.canvas._to_px(200, 100)
        self.assertEqual(self.canvas.line_at(x, y + 3), ("paths", 0))
        x, y = self.canvas._to_px(200, 200)
        self.assertEqual(self.canvas.line_at(x, y), ("barriers", 0))
        self.dialog.delete_line("barriers", 0)
        self.assertEqual(floor["barriers"], [])

    def test_the_grid_step_is_a_round_number(self):
        self.assertIn(self.canvas._grid_step(), (0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500))


if __name__ == "__main__":
    unittest.main()
