import json
import tempfile
import unittest
from pathlib import Path

from education_policy import (
    DEFAULT_EDUCATION_TOOLS,
    EDUCATION_NEVER_AVAILABLE,
    admin_writer_arguments, load_education_tools,
    normalise_tools,
    policy_path,
    write_education_tools,
)


class EducationPolicyTests(unittest.TestCase):
    def test_missing_or_damaged_policy_uses_safe_defaults(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "policy.json"
            self.assertEqual(load_education_tools(path),
                             set(DEFAULT_EDUCATION_TOOLS))
            path.write_text("not json", encoding="utf-8")
            self.assertEqual(load_education_tools(path),
                             set(DEFAULT_EDUCATION_TOOLS))

    def test_policy_accepts_permitted_tools_only(self):
        values = normalise_tools([
            "journey_planner", "find_food", "hotel_search",
            "virgin_booking", "order_uber", "unknown",
        ])
        self.assertEqual(values, {"journey_planner", "find_food"})
        self.assertTrue(EDUCATION_NEVER_AVAILABLE.isdisjoint(values))

    def test_policy_round_trip_is_deterministic(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "policy.json"
            write_education_tools({"find_food", "departure_board"}, path)
            self.assertEqual(load_education_tools(path),
                             {"find_food", "departure_board"})
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["enabled_tools"],
                             ["departure_board", "find_food"])

    def test_windows_policy_is_machine_wide(self):
        path = policy_path("win32", {"PROGRAMDATA": r"D:\SchoolData"})
        self.assertEqual(
            path, Path(r"D:\SchoolData") / "MapInABox" /
            "education-policy.json")

    def test_admin_writer_only_receives_permitted_tools(self):
        program, arguments = admin_writer_arguments(
            {"find_food", "hotel_search"}, "python", "core.py", False)
        self.assertEqual(program, "python")
        self.assertEqual(arguments, [
            "core.py", "--write-education-policy=find_food",
        ])


if __name__ == "__main__":
    unittest.main()
