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
        # No saved state: these tests must not read or change the user's.
        self.client = OverpassClient(state_path=None)
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



class RememberedServerTests(unittest.TestCase):
    """The server that last worked is tried first after a restart."""

    def setUp(self):
        import tempfile
        self.dir = tempfile.TemporaryDirectory()
        self.path = overpass_client.os.path.join(self.dir.name, "overpass_state.json")
        self.hosts = []

    def tearDown(self):
        self.dir.cleanup()

    def _client(self):
        client = OverpassClient(state_path=self.path)
        client._wait = lambda *a, **k: None
        return client

    def _urlopen(self, failing):
        def fake(req, timeout=None, context=None):
            self.hosts.append(req.full_url)
            if any(host in req.full_url for host in failing):
                raise socket.timeout("The read operation timed out")
            return _Response(json.dumps({"elements": [{"id": 1}]}).encode())
        return mock.patch.object(overpass_client.urllib.request, "urlopen", fake)

    def test_next_run_starts_on_the_server_that_last_worked(self):
        with self._urlopen(["overpass-api.de"]):
            self._client().large_request(b"data=street")
        self.hosts.clear()
        # A new client, as after restarting the app; Germany is healthy again.
        with self._urlopen([]):
            self._client().large_request(b"data=street")
        self.assertIn("openstreetmap.fr", self.hosts[0])

    def test_a_missing_or_broken_state_file_starts_on_germany(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("not json")
        with self._urlopen([]):
            self._client().large_request(b"data=street")
        self.assertIn("overpass-api.de", self.hosts[0])

    def test_an_unknown_saved_server_is_ignored(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"last_good": "https://example.invalid/api/interpreter"}, f)
        with self._urlopen([]):
            self._client().large_request(b"data=street")
        self.assertIn("overpass-api.de", self.hosts[0])


if __name__ == "__main__":
    unittest.main()
