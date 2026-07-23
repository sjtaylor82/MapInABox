import json
import tempfile
import unittest
from pathlib import Path

from package_manifest import build_manifest, write_manifest
from updater import _is_newer, _pick_asset


class PackageManifestTests(unittest.TestCase):
    def test_manifest_is_deterministic_and_excludes_mutable_files(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "MapInABox"
            (root / "_internal").mkdir(parents=True)
            (root / "Data").mkdir()
            (root / "MapInABox.exe").write_bytes(b"application")
            (root / "_internal" / "library.dll").write_bytes(b"library")
            (root / "_internal" / "_portable").write_bytes(b"")
            (root / "_internal" / "_education").write_bytes(b"")
            (root / "Data" / "settings.json").write_text(
                "user data", encoding="utf-8")

            first = build_manifest(root, "1.2.3", "education")
            second = build_manifest(root, "1.2.3", "education")

            self.assertEqual(first, second)
            self.assertEqual(
                list(first["files"]),
                ["_internal/library.dll", "MapInABox.exe"],
            )
            self.assertEqual(first["edition"], "education")

    def test_written_manifest_does_not_include_itself(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "MapInABox"
            root.mkdir()
            (root / "MapInABox.exe").write_bytes(b"application")

            destination = write_manifest(root, "1.2.3", "pro")
            manifest = json.loads(destination.read_text(encoding="utf-8"))

            self.assertEqual(
                destination, root / "_internal" / "update-manifest.json")
            self.assertNotIn(
                "_internal/update-manifest.json", manifest["files"])

    def test_portable_asset_selection_respects_edition(self):
        assets = [
            {
                "name": "MapInABox-Windows-portable.zip",
                "browser_download_url": "https://example.invalid/pro",
            },
            {
                "name": "MapInABox-Windows-Education-portable.zip",
                "browser_download_url": "https://example.invalid/education",
            },
        ]

        pro = _pick_asset(
            assets, portable=True, platform="win32", education=False)
        education = _pick_asset(
            assets, portable=True, platform="win32", education=True)
        self.assertTrue(pro["browser_download_url"].endswith("/pro"))
        self.assertTrue(
            education["browser_download_url"].endswith("/education"))

    def test_calendar_version_migrates_from_historical_version(self):
        self.assertTrue(_is_newer("v2026.7.0", "1.0.0.34"))
        self.assertTrue(_is_newer("2026.7.1", "2026.7.0"))
        self.assertFalse(_is_newer("2026.7.0", "2026.7.0"))


if __name__ == "__main__":
    unittest.main()
