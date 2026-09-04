import unittest
from unittest import mock
from urllib.parse import parse_qs, urlparse

from route_tools import RouteTools


class OpenGeocodingFallbackTests(unittest.TestCase):
    def test_photon_uses_supported_countrycode_parameter(self):
        tool = RouteTools("")
        with mock.patch.object(tool, "_request_json", return_value={
                "features": []}) as request:
            tool._photon_geocode_candidates(
                "cleveland qld", "AU", limit=8)
        query = parse_qs(urlparse(request.call_args.args[0]).query)
        self.assertEqual(query["countrycode"], ["AU"])
        self.assertNotIn("country", query)

    def test_photon_reverse_is_used_when_nominatim_fails(self):
        tool = RouteTools("")
        responses = [
            RuntimeError("HTTP 429"),
            {"features": [{"properties": {
                "district": "Coorparoo", "city": "Brisbane"}}]},
        ]

        def request(*_args, **_kwargs):
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

        with mock.patch.object(tool, "_request_json", side_effect=request):
            self.assertEqual(
                tool._reverse_geocode_suburb(-27.493, 153.061),
                "Coorparoo")


if __name__ == "__main__":
    unittest.main()
