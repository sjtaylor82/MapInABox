import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from greyhound import (
    _normalise_stops, availability_options, filter_stops, format_option_stops,
    load_availability, load_stops, option_stop_items, stop_label,
)
from tools import _build_greyhound_booking_url


STOPS = [
    {"Code": "NEW", "Description": "Newcastle", "Address": "NSW"},
    {"Code": "NEWC", "Description": "Newcastle West", "Address": "King Street"},
    {"Code": "BNE", "Description": "Brisbane", "Address": "Roma Street"},
]


def values(**changes):
    result = {
        "origin": {"code": "BNE", "description": "Brisbane"},
        "destination": {"code": "NEW", "description": "Newcastle"},
        "depart": dt.date(2026, 10, 10),
        "return": dt.date(2026, 10, 12),
        "is_return": True,
        "promo_code": "SAVE",
    }
    result.update(changes)
    return result


class GreyhoundTests(unittest.TestCase):
    def test_booking_url_populates_official_widget_parameters(self):
        url = _build_greyhound_booking_url(values())
        for expected in ("ORIGIN=BNE", "DESTINATION=NEW", "TRAVELDATE=2026-10-10",
                         "RETURNDATE=2026-10-12", "ISRETURN=TRUE", "PROMOCODE=SAVE"):
            self.assertIn(expected, url)

    def test_one_way_url_does_not_send_return_date(self):
        url = _build_greyhound_booking_url(values(is_return=False))
        self.assertIn("ISRETURN=FALSE", url)
        self.assertNotIn("RETURNDATE", url)

    def test_return_must_follow_departure(self):
        with self.assertRaisesRegex(ValueError, "return date"):
            _build_greyhound_booking_url(
                values(**{"return": dt.date(2026, 10, 9)}))

    def test_stop_filter_prefers_exact_code_and_searches_address(self):
        stops = _normalise_stops(STOPS)
        self.assertEqual(filter_stops(stops, "NEW")[0]["code"], "NEW")
        self.assertEqual(filter_stops(stops, "King Street")[0]["code"], "NEWC")

    def test_generic_multiple_address_notice_is_not_in_label(self):
        label = stop_label({
            "code": "ADR", "description": "Adelaide River",
            "address": "MULTIPLE ADDRESSES FOR THIS STOP. PLEASE CHECK TIMETABLE."})
        self.assertEqual(label, "Adelaide River (ADR)")

    def test_fresh_stop_cache_avoids_network(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "greyhound.json"
            path.write_text(
                json.dumps({"saved": 1000, "stops": _normalise_stops(STOPS)}),
                encoding="utf-8")

            def no_network(*args, **kwargs):
                raise AssertionError("network should not be used")

            self.assertEqual(len(load_stops(str(path), now=1001,
                                            opener=no_network)), 3)

    def test_availability_submission_is_for_one_adult(self):
        captured = []

        class Response:
            def __init__(self, body):
                self.body = body
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return self.body

        def opener(request, timeout=0):
            captured.append(request)
            if request.data is None:
                return Response(b'<input id="csrf" value="token">')
            return Response(json.dumps({
                "status": "success", "data": [{"RouteOptions": []}]
            }).encode())

        result = load_availability(values(is_return=False), opener=opener)
        payload = json.loads(captured[1].data)
        self.assertEqual(payload["passengers"]["adults"], 1)
        self.assertEqual(payload["options"]["fareBasis1Code"], "000")
        self.assertEqual(payload["options"]["fareBasis1Count"], 1)
        self.assertEqual(len(payload["options"]["TravelLegs"]), 1)
        self.assertEqual(result, [{"RouteOptions": []}])

    def test_availability_rows_include_one_adult_price(self):
        rows = availability_options([{"RouteOptions": [{
            "CheapestFare": 94, "BusesNumber": "GX421",
            "RouteTotalDurationText": "17h",
            "Sectors": [{"PassengerBoardingDate": "19/09/2026 7:00 AM",
                         "PassengerArrivalDate": "20/09/2026 12:00 AM"}]
        }]}])
        self.assertIn("GX421", rows[0]["label"])
        self.assertIn("from $94", rows[0]["label"])
        self.assertNotIn("Outbound:", rows[0]["label"])

    def test_return_results_identify_outbound_and_return_legs(self):
        route = {"CheapestFare": 50, "BusesNumber": "GX1",
                 "RouteTotalDurationText": "1h", "Sectors": []}
        rows = availability_options([
            {"RouteOptions": [route]}, {"RouteOptions": [route]}])
        self.assertTrue(rows[0]["label"].startswith("Outbound:"))
        self.assertTrue(rows[1]["label"].startswith("Return:"))

    def test_availability_label_is_concise_and_marks_next_day(self):
        rows = availability_options([{"RouteOptions": [{
            "CheapestFare": 139, "BusesNumber": "GX243",
            "RouteTotalDurationText": "17h 0m",
            "Sectors": [{
                "PassengerBoardingDate": "19/09/2026 6:00:00 PM",
                "PassengerArrivalDate": "20/09/2026 11:00:00 AM",
            }]
        }]}])
        self.assertEqual(
            rows[0]["label"],
            "GX243 departing 6:00pm, arriving 11:00am next day, from $139")

    def test_stop_view_shows_the_whole_service_route(self):
        row = {"route": {"Sectors": [{"BusNumber": "GX421"}]}}
        timetable = {"Routes": [{"Services": [{
            "Service": "GX421", "Stops": [
                {"StopCode": "PRE", "StopName": "Before boarding",
                 "ArrivalTime": "2026-10-10 06:00",
                 "DepartureTime": "2026-10-10 06:00"},
                {"StopCode": "BNE", "StopName": "Brisbane",
                 "ArrivalTime": "2026-10-10 07:00",
                 "DepartureTime": "2026-10-10 07:00"},
                {"StopCode": "AFT", "StopName": "After destination",
                 "ArrivalTime": "2026-10-11 01:00",
                 "DepartureTime": "2026-10-11 01:00"},
            ]}]}]}
        text = format_option_stops(row, timetable)
        self.assertIn("Before boarding", text)
        self.assertIn("Brisbane", text)
        self.assertIn("After destination", text)
        self.assertIn("Saturday 10 October 2026", text)
        self.assertNotIn("2026-10-10 06:00", text)
        items = option_stop_items(row, timetable)
        self.assertEqual(len(items), 4)
        self.assertTrue(items[0].startswith("Stops for service GX421"))


if __name__ == "__main__":
    unittest.main()
