import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

import greyhound
from greyhound import (
    _normalise_stops, availability_options, filter_stops, format_option_stops,
    live_departure_rows, live_route_stop_items, load_availability, load_stops,
    option_stop_items, stop_coordinates,
    plain_site_message, stop_label, stops_named_after, stops_near, validate_route,
    valid_destinations,
)
from tools import _build_greyhound_booking_url


STOPS = [
    {"Code": "NEW", "Description": "Newcastle", "Address": "NSW"},
    {"Code": "NEWC", "Description": "Newcastle West", "Address": "King Street"},
    {"Code": "BNE", "Description": "Brisbane", "Address": "Roma Street"},
]


class _Response:
    def __init__(self, body):
        self.body = body
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self):
        return self.body


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
    def test_html_route_error_is_plain_readable_text(self):
        message = plain_site_message(
            '<h1>No availability</h1><p>See the <a href="map">network map</a>.</p>')
        self.assertEqual(message, "No availability\nSee the network map.")

    def test_only_the_selected_route_pair_is_validated_and_cached(self):
        calls = []

        def opener(request, timeout=0):
            calls.append(request.full_url)
            return _Response(json.dumps({
                "IsValid": False, "Message": "<h1>No availability</h1>"
            }).encode())

        greyhound._route_validation_cache.clear()
        origin = {"code": "ADL"}
        destination = {"code": "SYD"}
        self.assertEqual(validate_route(origin, destination, opener=opener, now=0),
                         (False, "No availability"))
        self.assertEqual(validate_route(origin, destination, opener=opener, now=1),
                         (False, "No availability"))
        self.assertEqual(len(calls), 1)

    def test_destination_population_keeps_only_valid_pairs(self):
        raw_stops = [dict(stop, IsTerminal=stop["Code"] == "NEW") for stop in STOPS]
        stops = _normalise_stops(raw_stops)
        origin = next(stop for stop in stops if stop["code"] == "BNE")

        def loader(_origin, destination, _day):
            if destination["code"] != "NEW":
                return {"Routes": []}
            return {"Routes": [{"Services": [{"Stops": [
                {"StopCode": "BNE"}, {"StopCode": "NEW"}
            ]}]}]}

        greyhound._destination_validation_cache.clear()
        found = valid_destinations(
            origin, stops, dt.date(2026, 11, 11), loader=loader,
            now=0, max_workers=1)
        self.assertEqual([stop["code"] for stop in found], ["NEW"])

    def test_current_timetable_rows_include_live_time_service_and_fare(self):
        data = {"Routes": [{
            "DepartureDate": "2026-11-11 11:00",
            "ArrivalDate": "2026-11-11 12:50",
            "RouteServiceSummary": "GX714", "CheapestFare": 42,
            "Services": [],
        }]}
        rows = live_departure_rows(data, "BRISBANE")
        self.assertEqual(rows[0]["depart"], "11:00")
        self.assertEqual(rows[0]["arrive"], "12:50")
        self.assertIn("Greyhound GX714", rows[0]["label"])
        self.assertIn("from $42", rows[0]["label"])

    def test_live_timetable_decodes_transfers_and_marks_next_day(self):
        data = {"Routes": [{
            "DepartureDate": "2026-11-11 16:45",
            "ArrivalDate": "2026-11-12 08:30",
            "RouteServiceSummary": "GX496 &gt; GX712", "CheapestFare": 37.7,
            "Services": [],
        }]}
        row = live_departure_rows(data, "BRISBANE DOMESTIC AIRPORT")[0]
        self.assertIn("GX496 > GX712", row["label"])
        self.assertIn("next day", row["label"])

    def test_zero_fare_is_not_spoken_as_free_and_temp_stop_name_is_cleaned(self):
        data = {"Routes": [{
            "DepartureDate": "2026-11-11 04:00",
            "ArrivalDate": "2026-11-11 10:00",
            "RouteServiceSummary": "GX243", "CheapestFare": 0,
            "Services": [],
        }]}
        row = live_departure_rows(data, "(SFR - TEMP LOCAT) SOUTHPORT")[0]
        self.assertIn("arrives at Southport at 10:00am", row["label"])
        self.assertNotIn("from $0", row["label"])

    def test_live_route_stops_include_times_request_stops_and_meal_breaks(self):
        row = {
            "origin_code": "WGG", "destination_code": "BNE",
            "route": {"Services": [{"Service": "GX243", "Stops": [
                {"StopCode": "WGG", "StopName": "Woolgoolga",
                 "DepartureTime": "2026-11-11 04:00"},
                {"StopCode": "ARR", "StopName": "Arrawarra (Spot X)",
                 "DepartureTime": "2026-11-11 04:10", "OnRequest": True},
                {"StopCode": "BNM", "StopName": "Ballina Meal Break Only",
                 "ArrivalTime": "2026-11-11 07:20",
                 "DepartureTime": "2026-11-11 07:55",
                 "StopOver": True, "StopOverTime": "35m"},
                {"StopCode": "SPT", "StopName": "(SFR - TEMP LOCAT) Southport",
                 "DepartureTime": "2026-11-11 10:00", "OnRequest": True},
                {"StopCode": "BNE", "StopName": "Brisbane",
                 "ArrivalTime": "2026-11-11 11:00"},
            ]}]},
        }
        items = live_route_stop_items(row)
        self.assertEqual(items[0], "Service GX243")
        self.assertIn("Arrawarra (Spot X): 4:10am, request stop", items)
        self.assertIn(
            "Ballina Meal Break Only: arrive 7:20am, depart 7:55am, stopover 35m",
            items)
        self.assertIn("Southport: 10:00am, request stop", items)
        self.assertEqual(items[-1], "Brisbane: 11:00am")

    def test_booking_url_populates_official_widget_parameters(self):
        url = _build_greyhound_booking_url(values())
        for expected in ("b-origin=BNE", "b-destination=NEW",
                         "b-travel-date=2026-10-10",
                         "b-return-date=2026-10-12", "b-travel-type=return",
                         "b-promo-code=SAVE"):
            self.assertIn(expected, url)

    def test_one_way_url_does_not_send_return_date(self):
        url = _build_greyhound_booking_url(values(is_return=False))
        self.assertIn("b-travel-type=one-way", url)
        self.assertNotIn("b-return-date", url)

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

    def test_nearby_places_match_stop_names_not_partial_words(self):
        stops = _normalise_stops(STOPS + [
            {"Code": "SYD", "Description": "SYDNEY", "Address": "Central Station"},
            {"Code": "QFT", "Description": "SYDNEY DOMESTIC AIRPORT",
             "Address": "Multiple addresses for this stop"},
        ])
        codes = {s["code"] for s in stops_named_after(stops, {"Sydney", "Newcastle"})}
        self.assertEqual(codes, {"SYD", "QFT", "NEW", "NEWC"})
        # Part of a word is not a place name.
        self.assertEqual(stops_named_after(stops, {"Newcast"}), [])

    def test_stop_location_falls_back_and_is_cached(self):
        queries = []

        def photon(request, timeout=0):
            query = request.full_url.split("q=")[1].split("&")[0]
            queries.append(query)
            features = [] if len(queries) < 3 else [
                {"geometry": {"coordinates": [153.11, -30.30]}}]
            return _Response(json.dumps({"features": features}).encode())

        stop = {"code": "CFS", "description": "COFFS HARBOUR",
                "address": "COACH TERMINAL, PACIFIC HWY"}
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "coords.json")
            self.assertEqual(stop_coordinates(stop, path, now=0, opener=photon),
                             (-30.30, 153.11))
            self.assertEqual(len(queries), 3)  # address, then name + town, then town

            def no_network(*args, **kwargs):
                raise AssertionError("network should not be used")

            self.assertEqual(stop_coordinates(stop, path, now=100, opener=no_network),
                             (-30.30, 153.11))

    def test_network_failure_is_not_cached_as_unknown_location(self):
        def offline(*args, **kwargs):
            raise OSError("offline")

        stop = {"code": "BNE", "description": "BRISBANE", "address": ""}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "coords.json"
            self.assertIsNone(stop_coordinates(stop, str(path), opener=offline))
            self.assertFalse(path.exists())

    def test_stops_near_rejects_same_named_town_elsewhere(self):
        stops = _normalise_stops([
            {"Code": "RCM", "Description": "RICHMOND", "Address": "GOLDRING ST, RICHMOND QLD"},
            {"Code": "BYR", "Description": "BYRON BAY", "Address": "BUTLER ST, BYRON BAY NSW"},
        ])
        where = {"RICHMOND": (-20.73, 143.14), "BYRON BAY": (-28.645, 153.611)}

        def photon(request, timeout=0):
            for name, (lat, lon) in where.items():
                if name.replace(" ", "+") in request.full_url.upper():
                    return _Response(json.dumps({"features": [
                        {"geometry": {"coordinates": [lon, lat]}}]}).encode())
            return _Response(b'{"features": []}')

        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "coords.json")
            # Standing in Byron Bay, near a (Victorian) suburb also called Richmond.
            found = stops_near(stops, -28.64, 153.60, {"Byron Bay", "Richmond"},
                               cache_path=path, opener=photon)
        self.assertEqual([s["code"] for s in found], ["BYR"])

    def test_departures_from_a_stop_combine_searches_towards_each_terminus(self):
        stops = _normalise_stops([
            {"Code": "CFS", "Description": "COFFS HARBOUR", "Address": "PACIFIC HWY"},
            {"Code": "BNE", "Description": "BRISBANE", "Address": "ROMA ST"},
            {"Code": "SYD", "Description": "SYDNEY", "Address": "CENTRAL"},
        ])
        coffs = next(stop for stop in stops if stop["code"] == "CFS")
        day = dt.date(2026, 9, 17)

        def service(number, stop_times):
            return {"Service": number, "Stops": [
                {"StopName": name, "StopCode": code, "DepartureTime": f"2026-09-{when}"}
                for name, code, when in stop_times]}

        north = service("GX244", [("Coffs Harbour", "CFS", "17 05:10"),
                                  ("Brisbane", "BNE", "17 13:30")])
        south = service("GX422", [("Brisbane", "BNE", "17 16:00"),
                                  ("Coffs Harbour", "CFS", "17 22:10"),
                                  ("Sydney", "SYD", "18 07:00")])
        ends_here = service("GX999", [("Sydney", "SYD", "17 09:00"),
                                      ("Coffs Harbour", "CFS", "17 18:00")])
        calls = []

        def loader(origin, destination, when):
            calls.append(destination)
            if destination == "BNE":
                # The same northbound service can come back in several routes.
                return {"Routes": [{"Services": [north]}, {"Services": [north, ends_here]}]}
            if destination == "SYD":
                return {"Routes": [{"Services": [south]}]}
            raise OSError("offline")

        greyhound._departures_cache.clear()
        departures = greyhound.departures_from(coffs, stops, day, loader=loader, now=0)
        self.assertEqual(sorted(calls), ["BNE", "SYD"])
        self.assertEqual([(d["service"], d["depart"].strftime("%H:%M"), d["towards"])
                          for d in departures],
                         [("GX244", "05:10", "Brisbane"), ("GX422", "22:10", "Sydney")])
        self.assertEqual(greyhound.format_departure_stops(departures[1]),
                         "Greyhound GX422 towards Sydney\nCoffs Harbour: 22:10\nSydney: 07:00")

        def no_network(*args):
            raise AssertionError("cached result should be used")

        self.assertEqual(greyhound.departures_from(coffs, stops, day, loader=no_network, now=60),
                         departures)

    def test_failed_departure_searches_are_not_cached(self):
        stops = _normalise_stops([
            {"Code": "CFS", "Description": "COFFS HARBOUR", "Address": ""},
            {"Code": "BNE", "Description": "BRISBANE", "Address": ""},
        ])

        def offline(*args):
            raise OSError("offline")

        greyhound._departures_cache.clear()
        day = dt.date(2026, 9, 17)
        self.assertEqual(greyhound.departures_from(stops[0], stops, day, loader=offline, now=0), [])
        self.assertEqual(greyhound._departures_cache, {})

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

    def test_availability_token_attributes_can_be_reordered(self):
        captured = []

        def opener(request, timeout=0):
            captured.append(request)
            if request.data is None:
                return _Response(
                    b'<input value="token" name="__RequestVerificationToken" id="csrf">')
            return _Response(json.dumps({
                "status": "success", "data": [{"RouteOptions": []}]
            }).encode())

        load_availability(values(is_return=False), opener=opener)
        headers = {name.casefold(): value for name, value in captured[1].header_items()}
        self.assertEqual(headers["__requestverificationtoken"], "token")

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
