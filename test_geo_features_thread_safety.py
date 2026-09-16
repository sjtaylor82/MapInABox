import csv
import json
import os
import tempfile
import threading
import time
import unittest

from geo_features import GeoFeatures


class GeoFeaturesThreadSafetyTests(unittest.TestCase):
    def test_concurrent_country_load_is_serialized_and_cached_once(self):
        with tempfile.TemporaryDirectory() as source_dir, \
                tempfile.TemporaryDirectory() as cache_dir:
            source_path = os.path.join(source_dir, "AU.csv")
            with open(source_path, "w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(
                    f, fieldnames=("name", "feature_code", "country_code", "lat", "lon"))
                writer.writeheader()
                writer.writerow({
                    "name": "Test Bay", "feature_code": "H.BAY",
                    "country_code": "AU", "lat": "-27.5", "lon": "153.0",
                })
            with open(os.path.join(source_dir, "manifest.json"), "w", encoding="utf-8") as f:
                json.dump({"AU": {"file": "AU.csv"}}, f)

            features = GeoFeatures(source_dir)
            features._cache_dir = cache_dir
            original = features._load_country_cache
            active = 0
            max_active = 0
            calls = 0
            guard = threading.Lock()

            def observed_load(*args):
                nonlocal active, max_active, calls
                with guard:
                    active += 1
                    calls += 1
                    max_active = max(max_active, active)
                time.sleep(0.03)
                try:
                    return original(*args)
                finally:
                    with guard:
                        active -= 1

            features._load_country_cache = observed_load
            results = []
            threads = [
                threading.Thread(target=lambda: results.append(features._load_country("AU")))
                for _ in range(6)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(max_active, 1)
            self.assertEqual(calls, 1)
            self.assertEqual(len(results), 6)
            self.assertTrue(all(result is results[0] for result in results))
            self.assertEqual(results[0][0]["name"], "Test Bay")
            self.assertTrue(os.path.isfile(features._country_cache_path("AU")))


if __name__ == "__main__":
    unittest.main()
