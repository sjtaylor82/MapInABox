import io
import json
import socket
import unittest
from unittest import mock

import overpass_client
from overpass_client import OverpassClient


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FailoverTests(unittest.TestCase):
    def setUp(self):
        self.client = OverpassClient()
        self.client._wait = lambda *a, **k: None
        self.hosts = []

    def _urlopen(self, timing_out):
        def fake(req, timeout=None, context=None):
            self.hosts.append(req.full_url)
            if any(host in req.full_url for host in timing_out):
                raise socket.timeout("The read operation timed out")
            return _Response(json.dumps({"elements": [{"id": 1}]}).encode())
        return mock.patch.object(overpass_client.urllib.request, "urlopen", fake)

    def test_timed_out_server_is_avoided_for_minutes(self):
        with self._urlopen(["overpass-api.de"]):
            self.client.large_request(b"data=street")
            self.client.poi_request(b"data=search")
        # The street request tried Germany then France; the search went
        # straight to France instead of re-trying the German cluster.
        self.assertIn("overpass-api.de", self.hosts[0])
        self.assertIn("openstreetmap.fr", self.hosts[1])
        self.assertEqual(len(self.hosts), 3)
        self.assertIn("openstreetmap.fr", self.hosts[2])
        cooled = self.client._rate_limited_until[0] - overpass_client.time.time()
        self.assertGreater(cooled, 200)

    def test_search_starts_on_the_last_server_that_worked(self):
        self.client._last_successful_mirror = 2
        with self._urlopen([]):
            self.client.poi_request(b"data=search")
        self.assertIn("openstreetmap.fr", self.hosts[0])


if __name__ == "__main__":
    unittest.main()
