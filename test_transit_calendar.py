import datetime as dt
import io
import tempfile
import unittest
import zipfile

from transit_lookup import TransitLookup


def gtfs_zip(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, rows in files.items():
            zf.writestr(name, "\n".join(rows) + "\n")
    return buffer.getvalue()


# 2026-09-17 is a Thursday.
FEED = {
    "agency.txt": ["agency_id,agency_name", "TL,NSW TrainLink"],
    "stops.txt": ["stop_id,stop_name,stop_lat,stop_lon",
                  "CFS,Coffs Harbour Station,-30.3024,153.1403",
                  "GRF,Grafton Station,-29.6873,152.9386"],
    "routes.txt": ["route_id,agency_id,route_short_name,route_long_name,route_type",
                   "COACH,TL,,Coffs Harbour to Grafton,204",
                   "XPT,TL,,North Coast,2"],
    "trips.txt": ["route_id,service_id,trip_id,trip_headsign",
                  "COACH,MON_WED_FRI,C1,Grafton",
                  "COACH,DAILY,C2,Grafton",
                  "XPT,DAILY,X1,Brisbane"],
    "stop_times.txt": ["trip_id,arrival_time,departure_time,stop_id,stop_sequence",
                       "C1,08:00:00,08:00:00,CFS,1", "C1,09:00:00,09:00:00,GRF,2",
                       "C2,25:10:00,25:10:00,CFS,1", "C2,26:10:00,26:10:00,GRF,2",
                       "X1,06:30:00,06:30:00,CFS,1", "X1,08:30:00,08:30:00,GRF,2"],
    "calendar.txt": ["service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date",
                     "MON_WED_FRI,1,0,1,0,1,0,0,20260101,20261231",
                     "DAILY,1,1,1,1,1,1,1,20260101,20261231"],
    # The daily XPT does not run on Friday 18 September.
    "calendar_dates.txt": ["service_id,date,exception_type", "DAILY,20260918,2"],
}


class TransitCalendarTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.transit = TransitLookup(script_dir=self.folder.name)
        self.transit._feeds["f"] = self.transit._parse_zip(gtfs_zip(FEED), "f")

    def tearDown(self):
        self.folder.cleanup()

    def test_extended_route_types_are_named(self):
        routes = self.transit._feeds["f"]["routes"]
        self.assertEqual(routes["COACH"]["type"], "coach")
        self.assertEqual(routes["XPT"]["type"], "train")

    def test_service_that_does_not_run_today_is_skipped(self):
        # Thursday 07:00: the Mon/Wed/Fri 08:00 coach is not running today,
        # so the next ones are after midnight (daily 25:10) and Friday 08:00.
        # Friday's daily service is cancelled, so its 25:10 (early Saturday)
        # is skipped too, leaving Saturday's 25:10 in the early hours of Sunday.
        headsign, times = self.transit.next_departures(
            "CFS", "COACH", "f", now=dt.datetime(2026, 9, 17, 7, 0))
        self.assertEqual(headsign, "Grafton")
        self.assertEqual(times, ["tomorrow 01:10", "tomorrow 08:00", "Sunday 01:10"])

    def test_after_midnight_trip_belongs_to_previous_service_day(self):
        # 00:30 on Friday: Thursday's 25:10 trip leaves at 01:10 today.
        _headsign, times = self.transit.next_departures(
            "CFS", "COACH", "f", n=1, now=dt.datetime(2026, 9, 18, 0, 30))
        self.assertEqual(times, ["01:10"])

    def test_removed_date_is_skipped(self):
        # Thursday evening: Friday's XPT is cancelled, so the next is Saturday.
        _headsign, times = self.transit.next_departures(
            "CFS", "XPT", "f", n=1, now=dt.datetime(2026, 9, 17, 20, 0))
        self.assertEqual(times, ["Saturday 06:30"])

    def test_feed_without_calendars_treats_every_trip_as_daily(self):
        feed = {name: rows for name, rows in FEED.items()
                if name not in ("calendar.txt", "calendar_dates.txt")}
        self.transit._feeds["plain"] = self.transit._parse_zip(gtfs_zip(feed), "plain")
        _headsign, times = self.transit.next_departures(
            "CFS", "COACH", "plain", n=1, now=dt.datetime(2026, 9, 17, 7, 0))
        self.assertEqual(times, ["08:00"])

    def test_line_published_per_timetable_period_finds_todays_variant(self):
        # TransLink style: the same Cleveland line under a new route_id and
        # service_id for each timetable period.
        feed = {
            "agency.txt": ["agency_id,agency_name", "QR,Queensland Rail"],
            "stops.txt": ["stop_id,stop_name,stop_lat,stop_lon",
                          "CLV,Cleveland station platform 1,-27.52,153.27"],
            "routes.txt": ["route_id,agency_id,route_short_name,route_long_name,route_type",
                           "BDCL-5111,QR,BDCL,Cleveland line,2",
                           "BDCL-5115,QR,BDCL,Cleveland line,2"],
            "trips.txt": ["route_id,service_id,trip_id,trip_headsign",
                          "BDCL-5111,OCTOBER,T1,Brisbane City",
                          "BDCL-5115,SEPTEMBER,T2,Brisbane City",
                          "BDCL-5115,SEPTEMBER,T3,Brisbane City"],
            "stop_times.txt": ["trip_id,arrival_time,departure_time,stop_id,stop_sequence",
                               "T1,07:35:00,07:35:00,CLV,1",
                               "T2,07:35:00,07:35:00,CLV,1",
                               "T3,07:50:00,07:50:00,CLV,1"],
            "calendar.txt": ["service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date",
                             "OCTOBER,1,1,1,1,1,0,0,20261001,20261031",
                             "SEPTEMBER,1,1,1,1,1,0,0,20260901,20260930"],
        }
        self.transit._feeds["seq"] = data = self.transit._parse_zip(gtfs_zip(feed), "seq")
        # Both periods' 07:35 departures merge into one entry.
        self.assertEqual(len(data["stop_departures"]["CLV"]), 2)
        route_listed = self.transit.routes_for_stop("CLV", "seq")[0]["route_id"]
        for route_id in {route_listed, "BDCL-5111", "BDCL-5115"}:
            _headsign, times = self.transit.next_departures(
                "CLV", route_id, "seq", now=dt.datetime(2026, 9, 17, 7, 21))
            self.assertEqual(times[:2], ["07:35", "07:50"], route_id)

    def test_departure_board_orders_services_from_all_nearby_stops(self):
        feed = {
            "agency.txt": ["agency_id,agency_name", "QR,Queensland Rail"],
            "stops.txt": ["stop_id,stop_name,stop_lat,stop_lon,platform_code",
                          "P1,Cleveland station platform 1,-27.52,153.27,1",
                          "P2,Cleveland station platform 2,-27.52,153.27,2",
                          "BNE,Brisbane City,-27.47,153.02,"],
            "routes.txt": ["route_id,agency_id,route_short_name,route_long_name,route_type",
                           "CL,QR,CLSH,Cleveland line,2",
                           "IN,QR,SHCL,Cleveland line inbound,2"],
            "trips.txt": ["route_id,service_id,trip_id,trip_headsign",
                          "CL,WEEKDAY,OUT1,Shorncliffe",
                          "CL,WEEKDAY,OUT2,Shorncliffe",
                          "CL,WEEKEND,OUT3,Shorncliffe",
                          "IN,WEEKDAY,ARR1,Cleveland station",
                          "CL,WEEKDAY,LATE,Shorncliffe"],
            "stop_times.txt": ["trip_id,arrival_time,departure_time,stop_id,stop_sequence",
                               "OUT1,08:09:00,08:09:00,P1,1", "OUT1,09:00:00,09:00:00,BNE,2",
                               "OUT2,07:54:00,07:54:00,P2,1", "OUT2,08:45:00,08:45:00,BNE,2",
                               "OUT3,07:40:00,07:40:00,P1,1", "OUT3,08:30:00,08:30:00,BNE,2",
                               # Arrives and ends at Cleveland: not a departure.
                               "ARR1,07:00:00,07:00:00,BNE,1", "ARR1,07:50:00,07:50:00,P1,2",
                               # Timed past midnight on the weekday service.
                               "LATE,24:20:00,24:20:00,P1,1", "LATE,25:10:00,25:10:00,BNE,2"],
            "calendar.txt": ["service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date",
                             "WEEKDAY,1,1,1,1,1,0,0,20260101,20261231",
                             "WEEKEND,0,0,0,0,0,1,1,20260101,20261231"],
        }
        self.transit._feeds["q"] = data = self.transit._parse_zip(gtfs_zip(feed), "q")
        stops = [{**data["stops"][sid], "stop_id": sid, "_feed_id": "q"} for sid in ("P1", "P2")]

        # Thursday 07:30: weekday trips from both platforms, in time order,
        # the after-midnight trip last; the weekend trip and arrival skipped.
        departures, incomplete = self.transit.upcoming_departures(
            stops, now=dt.datetime(2026, 9, 17, 7, 30))
        self.assertFalse(incomplete)
        self.assertEqual([(d["when"].strftime("%a %H:%M"), d["stop"]["stop_id"]) for d in departures],
                         [("Thu 07:54", "P2"), ("Thu 08:09", "P1"), ("Fri 00:20", "P1")])

        # Just after midnight on Saturday: Friday's 24:20 trip is still to come.
        departures, _ = self.transit.upcoming_departures(
            stops, now=dt.datetime(2026, 9, 19, 0, 5))
        self.assertEqual(departures[0]["when"], dt.datetime(2026, 9, 19, 0, 20))

        # Saturday evening: nothing left today, so the next day with services.
        departures, _ = self.transit.upcoming_departures(
            stops, now=dt.datetime(2026, 9, 19, 20, 0))
        self.assertEqual(departures[0]["when"], dt.datetime(2026, 9, 20, 7, 40))

    def test_cache_from_older_parser_still_gives_times(self):
        # Kept in use while a feed re-downloads after a parser upgrade.
        self.transit._feeds["old"] = {"stop_departures": {
            "CFS": [(8 * 3600, "C1", "COACH", "Grafton")]}}
        headsign, times = self.transit.next_departures(
            "CFS", "COACH", "old", n=1, now=dt.datetime(2026, 9, 17, 7, 0))
        self.assertEqual((headsign, times), ("Grafton", ["08:00"]))

    def test_busy_stop_does_not_guess_other_days(self):
        data = self.transit._feeds["f"]
        data["departures_truncated"].add("CFS")
        _headsign, times = self.transit.next_departures(
            "CFS", "XPT", "f", now=dt.datetime(2026, 9, 17, 20, 0))
        self.assertEqual(times, [])


if __name__ == "__main__":
    unittest.main()
