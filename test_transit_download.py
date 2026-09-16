import io
import json
import os
import pickle
import tempfile
import time
import unittest
import urllib.error
import zipfile
from unittest import mock

import transit_lookup
from transit_lookup import GTFS_PARSER_VERSION, TransitLookup


def gtfs_zip(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, rows in files.items():
            zf.writestr(name, "\n".join(rows) + "\n")
    return buffer.getvalue()


FEED = {
    "stops.txt": ["stop_id,stop_name,stop_lat,stop_lon", "A,Alpha,-37.8,145.0", "B,Beta,-37.9,145.1"],
    "routes.txt": ["route_id,route_short_name,route_long_name,route_type", "R,1,One,3"],
    "trips.txt": ["route_id,service_id,trip_id,trip_headsign", "R,S,T1,Beta"],
    # No arrival_time column, and a short final row: both must still parse.
    "stop_times.txt": ["trip_id,departure_time,stop_id,stop_sequence",
                       "T1,08:00:00,A,1", "T1,08:10:00,B"],
}


class _Response(io.BytesIO):
    def __init__(self, body, headers):
        super().__init__(body)
        self.headers = headers

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
        return False


class TransitDownloadTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.transit = TransitLookup(script_dir=self.folder.name)
        self.cache = self.transit._cache_dir()
        self.reports = []
        self.transit._status.cb = self.reports.append

    def tearDown(self):
        self.folder.cleanup()

    def _save_stale_cache(self, meta):
        data = self.transit._parse_zip(gtfs_zip(FEED), "f")
        data["_parser_version"] = GTFS_PARSER_VERSION
        with open(os.path.join(self.cache, "f.parsed.pkl"), "wb") as f:
            pickle.dump(data, f)
        with open(os.path.join(self.cache, "f.meta.json"), "w", encoding="utf-8") as f:
            json.dump({"downloaded_at": time.time() - 30 * 86400, "feed_id": "f", **meta}, f)

    def test_unchanged_feed_skips_prompt_download_and_parse(self):
        self._save_stale_cache({"url": "https://x/gtfs.zip", "etag": '"v1"'})
        self.transit.stale_confirm_cb = mock.Mock(side_effect=AssertionError("no prompt expected"))

        def server(request, timeout=0, context=None):
            self.assertEqual(request.get_header("If-none-match"), '"v1"')
            raise urllib.error.HTTPError(request.full_url, 304, "Not Modified", {}, None)

        with mock.patch.object(transit_lookup.urllib.request, "urlopen", server), \
                mock.patch.object(self.transit, "_download_feed",
                                  side_effect=AssertionError("no download expected")):
            feed_id, data = self.transit._gtfs_ensure("f", "https://x/gtfs.zip")
        self.assertEqual(sorted(data["stops"]), ["A", "B"])
        self.assertLess(self.transit._gtfs_cache_age_days("f"), 1)  # current again

    def test_feed_without_saved_etag_is_not_checked(self):
        self._save_stale_cache({})
        with mock.patch.object(transit_lookup.urllib.request, "urlopen",
                               side_effect=AssertionError("no request expected")):
            self.assertFalse(self.transit._feed_unchanged("f", "https://x/gtfs.zip"))

    def test_streamed_download_saves_file_etag_and_reports_progress(self):
        body = gtfs_zip(FEED) + b"\0" * 24_000_000  # large enough to report progress
        headers = {"Content-Length": str(len(body)), "ETag": '"v2"',
                   "Last-Modified": "Fri, 04 Sep 2026 23:09:54 GMT"}
        zip_path = os.path.join(self.cache, "f.zip")
        with mock.patch.object(transit_lookup.urllib.request, "urlopen",
                               lambda request, timeout=0, context=None: _Response(body, headers)):
            self.transit._download_feed("f", "https://x/gtfs.zip", zip_path)
        self.assertEqual(os.path.getsize(zip_path), len(body))
        self.assertFalse(os.path.exists(zip_path + ".part"))
        meta = self.transit._read_meta("f")
        self.assertEqual((meta["etag"], meta["url"]), ('"v2"', "https://x/gtfs.zip"))
        self.assertIn("Timetable download 50 percent.", self.reports)
        self.assertTrue(self.reports[0].startswith("Downloading a 24 megabyte"))

    def test_interrupted_download_leaves_no_partial_file(self):
        body = b"x" * 1000
        headers = {"Content-Length": "5000"}
        zip_path = os.path.join(self.cache, "f.zip")
        with mock.patch.object(transit_lookup.urllib.request, "urlopen",
                               lambda request, timeout=0, context=None: _Response(body, headers)):
            with self.assertRaises(OSError):
                self.transit._download_feed("f", "https://x/gtfs.zip", zip_path)
        self.assertFalse(os.path.exists(zip_path))
        self.assertFalse(os.path.exists(zip_path + ".part"))

    def test_zip_of_zips_parses_from_disk_and_cleans_up(self):
        outer = io.BytesIO()
        with zipfile.ZipFile(outer, "w") as zf:
            zf.writestr("1/google_transit.zip", gtfs_zip(FEED))
            zf.writestr("2/google_transit.zip", gtfs_zip(FEED))
        zip_path = os.path.join(self.cache, "vic.zip")
        with open(zip_path, "wb") as f:
            f.write(outer.getvalue())
        before = set(os.listdir(self.cache))
        data = self.transit._parse_zip(zip_path, "vic")
        self.assertEqual(sorted(data["stops"]), ["z0_A", "z0_B", "z1_A", "z1_B"])
        self.assertEqual(len(data["stop_departures"]["z0_B"]), 1)
        self.assertEqual(set(os.listdir(self.cache)), before)  # temporary zips removed
        os.remove(zip_path)  # closed, so Windows allows deleting it


if __name__ == "__main__":
    unittest.main()
