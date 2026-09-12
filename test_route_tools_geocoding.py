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

    def test_business_search_drops_leading_the_and_formats_full_photon_address(self):
        tool = RouteTools("")
        with mock.patch.object(
                tool, "_nominatim_geocode_candidates", return_value=[]
        ) as nominatim, mock.patch.object(
                tool, "_photon_geocode_candidates", return_value=[{
                    "geometry": {"coordinates": [151.759, -32.927]},
                    "properties": {
                        "name": "NEX",
                        "street": "King Street",
                        "district": "Newcastle West",
                        "state": "New South Wales",
                        "postcode": "2302",
                        "country": "Australia",
                    },
                }]
        ) as photon:
            results = tool.geocode_candidates("The NEX Newcastle", "AU", limit=8)

        nominatim.assert_called_once_with("NEX Newcastle", "AU", limit=8)
        photon.assert_called_once_with("NEX Newcastle", "AU", limit=8)
        self.assertEqual(
            results[0].formatted,
            "NEX, King Street, Newcastle West, New South Wales, 2302, Australia",
        )

    def test_strong_nominatim_business_match_avoids_photon_request(self):
        tool = RouteTools("")
        with mock.patch.object(tool, "_nominatim_geocode_candidates", return_value=[{
            "lat": "-33.889", "lon": "151.125",
            "display_name": "Vision Australia, 224 Liverpool Road, Ashfield, Australia",
        }]), mock.patch.object(tool, "_photon_geocode_candidates") as photon:
            results = tool.geocode_candidates("Vision Australia", "AU", limit=8)

        photon.assert_not_called()
        self.assertEqual(results[0].formatted.split(",", 1)[0], "Vision Australia")

    def test_missing_house_number_triggers_photon_and_exact_number_ranks_first(self):
        tool = RouteTools("")
        with mock.patch.object(tool, "_nominatim_geocode_candidates", return_value=[{
            "lat": "-32.926", "lon": "151.759",
            "display_name": "King Street, Newcastle West, New South Wales, 2302, Australia",
        }]), mock.patch.object(tool, "_photon_geocode_candidates", return_value=[{
            "geometry": {"coordinates": [151.760, -32.927]},
            "properties": {
                "name": "NEX", "housenumber": "309", "street": "King Street",
                "district": "Newcastle West", "state": "New South Wales",
                "postcode": "2302", "country": "Australia",
            },
        }]) as photon:
            results = tool.geocode_candidates(
                "309 King St Newcastle West NSW 2302", "AU", limit=8)

        photon.assert_called_once()
        self.assertTrue(results[0].formatted.startswith("NEX, 309 King Street"))


if __name__ == "__main__":
    unittest.main()
