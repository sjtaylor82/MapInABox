import datetime
import unittest

from rome2rio import (
    add_rome2rio_flights_for_all_routes,
    add_rome2rio_upcoming_flight,
    add_rome2rio_transit_schedules,
    parse_routes,
    route_url,
)

BUS_SCHEDULE_HTML = r'''<section id="schedules"><p>Buses run twice daily.</p></section>
<script>window.data={"scheduleGroups":[{"date":"2026-08-31","title":"Departing Monday, August 31, 2026","scheduleItems":[{"departureTime":"09:30:00","arrivalTime":"11:10:00","durationInMinutes":100,"changesMessage":"Direct","operators":[{"name":"Greyhound Australia"}]},{"departureTime":"14:00:00","arrivalTime":"15:30:00","durationInMinutes":90,"changesMessage":"Direct","operators":[{"name":"Premier Motor Service"}]}]}]};</script>'''


HTML = """
<html><body><article><ol>
  <li><a href="/map/Brisbane/Batemans-Bay" data-action="Click:RouteLink"
         data-label="Plane|Bus:0:test">
    <h3>Fly Brisbane Airport to Canberra International Airport, bus</h3>
    <span>best</span>
    <p>Fly from Brisbane Airport (BNE) to Canberra International Airport (CBR)</p>
    <span>plane</span><span>plane</span><span>BNE - CBR</span>
    <p>Take the bus from Queanbeyan to Batemans Bay</p>
    <span>bus</span><span>Mcs</span><span>7h 53m</span><span>$202–462</span>
  </a></li>
  <li><a href="/map/Brisbane/Moruya" data-action="Click:RouteLink"
         data-label="Plane:1:test">
    <h3>Fly Brisbane Airport to Moruya Airport</h3>
    <p>Fly from Brisbane Airport (BNE) to Moruya Airport (MYA)</p>
    <span>7h 17m</span><span>$284–932</span>
  </a></li>
</ol></article>
<section id="route-operators">
  <p>Murrays Coaches operates a bus from Queanbeyan to Batemans Bay once daily.</p>
  <p>Jetstar, Japan Airlines, and five other airlines fly from Brisbane Airport
     (BNE) to Moruya Airport (MYA) twice daily.</p>
</section>
<button data-action="Expand:Operator" data-label="Regional Express"
        aria-controls="rex-details">Regional Express</button>
<div id="rex-details">
  <h5>Flights from Toowoomba Wellcamp Airport to St. George Airport</h5>
  <dl><dt>Ave. Duration</dt><dd>1h</dd>
      <dt>When</dt><dd>Wednesday and Sunday</dd>
      <dt>Estimated price</dt><dd>$260–550</dd></dl>
</div>
<button data-action="Expand:Operator" data-label="Greyhound Australia"
        aria-controls="greyhound-details">Greyhound Australia</button>
<div id="greyhound-details">
  <h5>Bus from Queanbeyan to Batemans Bay</h5>
  <dl><dt>Ave. Duration</dt><dd>2h</dd>
      <dt>Frequency</dt><dd>Once daily</dd>
      <dt>Estimated price</dt><dd>$30–50</dd></dl>
</div>
</body></html>
"""


class Rome2RioTests(unittest.TestCase):
    def test_dated_bus_departures_are_added_with_full_details(self):
        class Response:
            content = BUS_SCHEDULE_HTML.encode()
            def raise_for_status(self):
                pass

        calls = []
        routes = [{"detail_text": (
            "Overview:\n1. Take the bus from Brisbane to Maroochydore.\n\n"
            "This is a broad Rome2Rio estimate") }]
        added = add_rome2rio_transit_schedules(
            routes, lambda url, **kwargs: calls.append(url) or Response(),
            datetime.date(2026, 8, 31))
        self.assertEqual(added, 1)
        self.assertEqual(calls, [
            "https://www.rome2rio.com/Bus/Brisbane/Maroochydore"])
        detail = routes[0]["detail_text"]
        self.assertIn("Bus schedules for Monday, August 31, 2026", detail)
        self.assertIn("Buses run twice daily", detail)
        self.assertIn("Greyhound Australia", detail)
        self.assertIn("Depart Brisbane at 9:30 AM", detail)
        self.assertIn("arrive Maroochydore at 11:10 AM", detail)
        self.assertIn("Duration 1h 40m. Direct.", detail)
        self.assertIn("Premier Motor Service", detail)

    def test_transit_schedule_fetch_is_shared_by_duplicate_routes(self):
        class Response:
            content = BUS_SCHEDULE_HTML.encode()
            def raise_for_status(self):
                pass
        calls = []
        routes = [{"detail_text": "1. Take the bus from Brisbane to Maroochydore."}
                  for _ in range(2)]
        added = add_rome2rio_transit_schedules(
            routes, lambda *args, **kwargs: calls.append(1) or Response(),
            datetime.date(2026, 9, 1))
        self.assertEqual(len(calls), 1)
        self.assertEqual(added, 2)
        self.assertIn("No bus departures were listed for Tuesday, 01 September 2026",
                      routes[0]["detail_text"])
    def test_route_url_uses_place_slugs(self):
        self.assertEqual(
            route_url("Brisbane", "Batemans Bay"),
            "https://www.rome2rio.com/s/Brisbane/Batemans-Bay",
        )

    def test_route_cards_become_accessible_journey_results(self):
        routes = parse_routes(
            HTML, "https://www.rome2rio.com/s/Brisbane/Batemans-Bay")
        self.assertEqual(len(routes), 2)
        self.assertIn("Option 1, best", routes[0]["summary"])
        self.assertIn("7h 53m", routes[0]["summary"])
        self.assertIn("Estimated $202–462", routes[0]["summary"])
        self.assertIn("Take the bus from Queanbeyan", routes[0]["detail_text"])
        self.assertIn("BNE - CBR", routes[0]["detail_text"])
        self.assertIn("Mcs", routes[0]["detail_text"])
        self.assertIn("Murrays Coaches operates", routes[0]["detail_text"])
        self.assertIn("Services mentioned:", routes[0]["detail_text"])
        self.assertIn("Greyhound Australia", routes[0]["detail_text"])
        self.assertIn("Frequency Once daily", routes[0]["detail_text"])
        self.assertEqual(routes[0]["source"], "rome2rio")

    def test_flight_summary_uses_the_route_card_heading(self):
        routes = parse_routes(
            HTML, "https://www.rome2rio.com/s/Brisbane/Batemans-Bay")
        self.assertIn("Fly Brisbane Airport to Moruya Airport",
                      routes[1]["summary"])
        self.assertNotIn("connections may be required", routes[1]["summary"])
        self.assertIn("Jetstar, Japan Airlines", routes[1]["detail_text"])

    def test_compact_hours_are_a_duration_not_an_identifier(self):
        compact = HTML.replace("7h 53m", "10h")
        routes = parse_routes(
            compact, "https://www.rome2rio.com/s/Brisbane/Batemans-Bay")
        self.assertIn("Estimated journey time: 10h", routes[0]["detail_text"])
        self.assertNotIn("identifiers: BNE - CBR, Mcs, 10h",
                         routes[0]["detail_text"])

    def test_expandable_flight_operator_details_are_included(self):
        html = HTML.replace(
            "Fly from Brisbane Airport (BNE) to Moruya Airport (MYA)",
            "Fly from Toowoomba Wellcamp Airport (WTB) to St. George Airport (SGO)",
        )
        routes = parse_routes(
            html, "https://www.rome2rio.com/s/Cleveland/St-George")
        detail = routes[1]["detail_text"]
        self.assertIn("Flights mentioned:", detail)
        self.assertIn("Regional Express", detail)
        self.assertIn("Average flight duration 1h", detail)
        self.assertIn("Operates Wednesday and Sunday", detail)
        self.assertIn("Estimated flight price $260–550", detail)

    def test_rome2rio_schedule_adds_flight_number_and_times(self):
        data = {
            "carriers": [
                {"name": "Virgin Australia", "code": "VA"},
                {"name": "Link Airways", "code": "FC"},
            ],
            "places": [
                {"name": "Brisbane Airport", "code": "BNE"},
                {"name": "Armidale", "code": "ARM"},
            ],
            "lines": [{"places": [0, 1]}, {"places": [0, 1]}],
            "hops": [{
                "name": "4053", "line": 0, "departureTime": "12:00",
                "arrivalTime": "13:10", "marketingCarrier": 0,
                "operatingCarrier": 1,
            }, {
                "name": "999", "line": 1, "departureTime": "15:00",
                "arrivalTime": "16:20", "marketingCarrier": 1,
            }],
            "legs": [
                {"hops": [0], "layovers": [], "duration": 70},
                {"hops": [1], "layovers": [], "duration": 80},
            ],
            "layovers": [],
            "outboundItineraries": [{"legs": [0]}, {"legs": [1]}],
        }

        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return data

        calls = []

        def request_get(url, **kwargs):
            calls.append((url, kwargs))
            return Response()

        routes = parse_routes(
            HTML.replace("CBR", "ARM"),
            "https://www.rome2rio.com/s/Brisbane/Armidale")
        added = add_rome2rio_upcoming_flight(
            routes, "public-key", request_get, datetime.date(2026, 8, 30))
        self.assertTrue(added)
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0][1]["params"]["oDateTime"],
            "2026-08-30T00:00:00")
        detail = routes[0]["detail_text"]
        self.assertIn("Flight schedules for Sunday, 30 August 2026", detail)
        self.assertIn("Option 1", detail)
        self.assertIn("Option 2", detail)
        self.assertIn("Virgin Australia, operated by Link Airways VA4053", detail)
        self.assertIn("Link Airways FC999", detail)
        self.assertIn("BNE 12:00 to ARM 13:10", detail)

    def test_schedules_are_added_to_every_flight_route(self):
        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "carriers": [{"name": "Link Airways", "code": "FC"}],
                    "places": [{"code": "BNE"}, {"code": "ARM"}],
                    "lines": [{"places": [0, 1]}],
                    "hops": [{"name": "1", "line": 0,
                              "departureTime": "10:00", "arrivalTime": "11:00",
                              "marketingCarrier": 0}],
                    "legs": [{"hops": [0], "layovers": [], "duration": 60}],
                    "layovers": [],
                    "outboundItineraries": [{"legs": [0]}],
                }

        routes = parse_routes(
            HTML.replace("CBR", "ARM"),
            "https://www.rome2rio.com/s/Brisbane/Armidale")
        added = add_rome2rio_flights_for_all_routes(
            routes, "public-key", lambda *args, **kwargs: Response(),
            datetime.date(2026, 8, 30))
        self.assertEqual(added, 2)
        self.assertTrue(all("Flight schedules for" in route["detail_text"]
                            for route in routes))

if __name__ == "__main__":
    unittest.main()
