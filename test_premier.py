import datetime as dt
import unittest

import premier


class PremierTests(unittest.TestCase):
    def test_timetable_rows_parse_time_stop_and_address(self):
        page = """
        <table><tr><td>13:00</td><td>Brisbane</td>
        <td>Coach Terminal on Parkland Crescent</td></tr></table>
        """
        self.assertEqual(premier._timetable_rows(page), [{
            "time": "13:00", "name": "Brisbane",
            "address": "Coach Terminal on Parkland Crescent",
        }])

    def test_destinations_follow_service_direction(self):
        routes = [{"service": "PM11", "stops": [
            {"code": "BNE", "description": "Brisbane"},
            {"code": "CFS", "description": "Coffs Harbour"},
            {"code": "SYD", "description": "Sydney Central"},
        ]}]
        self.assertEqual(
            premier.destinations(routes, "Coffs Harbour"), ["Sydney Central"])
        self.assertEqual(premier.destinations(routes, "Sydney Central"), [])

    def test_live_service_markup_parses_time_fare_and_next_day(self):
        page = """
        <table><tr><td><input name="ForwardService" value="PM11-DS"></td>
        <td class="Pickup">1:00 PM</td><td>PM11-DS</td>
        <td>Brisbane</td><td class="DropOff">7:45 AM</td>
        <td>Sydney Central</td><td class="Fare"><span>$133.00</span></td>
        </tr></table>
        """
        origin = {"code": "BNE", "description": "Brisbane"}
        destination = {"code": "SYD", "description": "Sydney Central"}
        routes = [{"service": "PM11", "stops": [origin, destination]}]
        rows = premier._service_rows(
            page, origin, destination, dt.date(2026, 11, 12), routes)
        self.assertEqual(rows[0]["depart"], "13:00")
        self.assertEqual(rows[0]["arrive"], "07:45")
        self.assertEqual(rows[0]["price"], 133)
        self.assertIn("next day", rows[0]["label"])
        self.assertIn("from $133", rows[0]["label"])

    def test_route_stop_items_slice_selected_journey_and_roll_over_midnight(self):
        row = {
            "service": "PM11-DS", "origin_code": "CFS", "destination_code": "SYD",
            "route": {"stops": [
                {"code": "BNE", "description": "Brisbane", "time": "13:00",
                 "address": "Parkland Crescent"},
                {"code": "CFS", "description": "Coffs Harbour", "time": "22:50",
                 "address": "Coach Terminal"},
                {"code": "SYD", "description": "Sydney Central", "time": "07:45",
                 "address": "Bay 17"},
            ]},
        }
        items = premier.route_stop_items(row)
        self.assertEqual(items[0], "Service PM11-DS")
        self.assertIn("Coffs Harbour: 10:50pm", items[1])
        self.assertIn("Sydney Central: 7:45am next day", items[2])


if __name__ == "__main__":
    unittest.main()
