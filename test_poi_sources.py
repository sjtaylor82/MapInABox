import os
import tempfile
import unittest
from unittest import mock

import poi_fetch
from poi_fetch import PoiFetcher, merge_background_sources


def _poi(name, lat, lon, source=None):
    poi = {"name": name, "kind": "hotel", "lat": lat, "lon": lon}
    if source:
        poi["source"] = source
    return poi


class MergeTests(unittest.TestCase):
    def test_same_place_from_both_sources_is_kept_once(self):
        here = [_poi("Holiday Inn Express", -26.6541, 153.0905, "here")]
        osm = [_poi("Holiday Inn Express", -26.6542, 153.0906),
               _poi("KFC", -26.6550, 153.0900)]
        merged = merge_background_sources([here, osm])
        self.assertEqual([(p["name"], p.get("source")) for p in merged],
                         [("Holiday Inn Express", "here"), ("KFC", None)])

    def test_same_name_far_apart_is_two_places(self):
        merged = merge_background_sources([
            [_poi("KFC", -26.65, 153.09, "here")],
            [_poi("KFC", -26.70, 153.09)],
        ])
        self.assertEqual(len(merged), 2)

    def test_name_matching_ignores_case_and_punctuation(self):
        merged = merge_background_sources([
            [_poi("Busy Health", -26.6541, 153.0905, "here")],
            [_poi("busy-health", -26.6541, 153.0905)],
        ])
        self.assertEqual(len(merged), 1)

    def test_one_source_keeps_its_own_entries(self):
        same = [_poi("Unit", 0, 0), _poi("Unit", 0, 0)]
        self.assertEqual(len(merge_background_sources([same])), 2)


class BackgroundSourcesTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.cache_path = os.path.join(tmp, "poi_cache.json")
        self.fetcher = PoiFetcher(None, self.cache_path, here_api_key="test-key")
        self.calls = []

        def fake_source(source, lat, lon, address_points=None, force_refresh=False):
            self.calls.append(source)
            pois = {"here": [_poi("Holiday Inn Express", 1.0, 1.0, "here")],
                    "osm": [_poi("Holiday Inn Express", 1.0, 1.0),
                            _poi("Corner Store", 1.001, 1.0)]}[source]
            cache = poi_fetch._load_poi_cache(self.cache_path)
            poi_fetch._set_cached(cache, self.fetcher._background_cache_key(lat, lon, source), pois)
            poi_fetch._save_poi_cache(self.cache_path, cache)
            return pois

        patcher = mock.patch.object(self.fetcher, "_fetch_background_source", fake_source)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_both_sources_are_fetched_and_merged(self):
        pois = self.fetcher.fetch_all_background(1.0, 1.0, sources=["here", "osm"])
        self.assertEqual(self.calls, ["here", "osm"])
        self.assertEqual(sorted(p["name"] for p in pois),
                         ["Corner Store", "Holiday Inn Express"])

    def test_cache_needs_every_source(self):
        self.fetcher.fetch_all_background(1.0, 1.0, sources=["osm"])
        self.assertIsNone(self.fetcher.load_cached_pois(1.0, 1.0, sources=["here", "osm"]))
        self.assertIsNone(self.fetcher.cached_background_age_hours(
            1.0, 1.0, sources=["here", "osm"]))
        self.fetcher.fetch_all_background(1.0, 1.0, sources=["here"])
        cached = self.fetcher.load_cached_pois(1.0, 1.0, sources=["here", "osm"])
        self.assertEqual(len(cached), 2)
        self.assertLess(self.fetcher.cached_background_age_hours(
            1.0, 1.0, sources=["here", "osm"]), 1)

    def test_here_without_key_falls_back_to_osm(self):
        self.fetcher.set_here_key("")
        self.fetcher.fetch_all_background(1.0, 1.0, sources=["here"])
        self.assertEqual(self.calls, ["osm"])


class SettingsMigrationTests(unittest.TestCase):
    def test_old_single_choice_becomes_checkbox_list(self):
        import json
        import core
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "settings.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"poi_source": "here"}, f)
            with mock.patch.object(core, "SETTINGS_PATH", path), \
                    mock.patch.object(core, "load_secure_credentials"):
                settings = core.load_settings()
        self.assertEqual(settings["poi_sources"], ["here"])


if __name__ == "__main__":
    unittest.main()


class SearchSourceTests(unittest.TestCase):
    """POI sources ticked in Settings drive both street mode and P searches."""

    def _host(self, **settings):
        import core

        class Host:
            _poi_background_sources = core.MapNavigator._poi_background_sources
            _poi_search_sources = core.MapNavigator._poi_search_sources

        host = Host()
        host.settings = settings
        return host, core.MapNavigator._poi_sources_label

    def test_google_is_only_used_when_ticked_and_keyed(self):
        host, _label = self._host(poi_sources=["osm", "here"], here_api_key="h",
                                  google_api_key="g")
        self.assertEqual(host._poi_search_sources(), ["here", "osm"])
        host.settings["poi_sources"].append("google")
        self.assertEqual(host._poi_search_sources(), ["here", "osm", "google"])
        host.settings["google_api_key"] = ""
        self.assertEqual(host._poi_search_sources(), ["here", "osm"])

    def test_google_never_feeds_the_street_mode_list(self):
        host, _label = self._host(poi_sources=["google"], google_api_key="g")
        self.assertEqual(host._poi_background_sources(), ["osm"])
        self.assertEqual(host._poi_search_sources(), ["osm", "google"])

    def test_default_has_google_off(self):
        import core
        self.assertNotIn("google", core.DEFAULT_SETTINGS["poi_sources"])

    def test_sources_are_named_for_speech(self):
        _host, label = self._host()
        self.assertEqual(label(["here", "osm", "google"]), "HERE, OpenStreetMap and Google")
        self.assertEqual(label(["osm"]), "OpenStreetMap")
