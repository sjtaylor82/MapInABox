import io
import json
import unittest
import urllib.error
import urllib.parse
from unittest import mock

import poi_fetch
from poi_fetch import PoiFetcher


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _item(title, lat, lng, here_id):
    return {
        "id": here_id,
        "title": title,
        "position": {"lat": lat, "lng": lng},
        "address": {"houseNumber": "42", "street": "First Avenue"},
        "categories": [{"name": "Hotel"}],
    }


HOLIDAY_INN = _item("Holiday Inn Express", -26.6560, 153.0960, "here:1")
HOLIDAY_PARK = _item("Cotton Tree Holiday Park", -26.6530, 153.1000, "here:2")
FAR_AWAY = _item("Holiday Resort", -26.80, 153.10, "here:3")


class HereNameSearchTests(unittest.TestCase):
    LAT, LON = -26.6585, 153.0952

    def setUp(self):
        self.fetcher = PoiFetcher(None, "unused-cache.json", here_api_key="test-key")
        self.requests = []

    def _urlopen(self, responses):
        def fake_urlopen(req, timeout=None):
            params = urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query)
            self.requests.append(params)
            reply = responses[len(self.requests) - 1]
            if isinstance(reply, Exception):
                raise reply
            return _Response(json.dumps({"items": reply}).encode())
        return mock.patch.object(poi_fetch.urllib.request, "urlopen", fake_urlopen)

    def test_asks_by_circle_and_by_point_and_merges(self):
        # HERE Discover rejects "at" with "in=circle", and each form can miss
        # places the other finds.
        with self._urlopen([[HOLIDAY_PARK], [HOLIDAY_INN, HOLIDAY_PARK, FAR_AWAY]]):
            pois = self.fetcher.fetch_here_name_search(
                self.LAT, self.LON, "holiday", radius=2000)
        circle, point = self.requests
        self.assertEqual(circle["in"], [f"circle:{self.LAT},{self.LON};r=2000"])
        self.assertNotIn("at", circle)
        self.assertEqual(point["at"], [f"{self.LAT},{self.LON}"])
        self.assertNotIn("in", point)
        self.assertEqual(sorted(p["name"] for p in pois),
                         ["Cotton Tree Holiday Park", "Holiday Inn Express"])

    def test_one_failed_request_still_returns_the_other(self):
        error = urllib.error.HTTPError("https://example", 400, "Bad Request", {}, None)
        with self._urlopen([error, [HOLIDAY_INN]]):
            pois = self.fetcher.fetch_here_name_search(self.LAT, self.LON, "holiday")
        self.assertEqual([p["name"] for p in pois], ["Holiday Inn Express"])

    def test_both_failed_returns_none(self):
        error = urllib.error.HTTPError("https://example", 400, "Bad Request", {}, None)
        with self._urlopen([error, error]):
            self.assertIsNone(self.fetcher.fetch_here_name_search(0, 0, "holiday"))


if __name__ == "__main__":
    unittest.main()
