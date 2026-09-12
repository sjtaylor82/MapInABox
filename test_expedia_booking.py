import datetime as dt
import urllib.parse

from tools import _build_expedia_booking_url


def test_expedia_booking_url_contains_search_details():
    url = _build_expedia_booking_url({
        "search_type": "hotels",
        "origin": "",
        "destination": "Newcastle NSW, Australia",
        "check_in": dt.date(2026, 10, 23),
        "check_out": dt.date(2026, 10, 25),
        "rooms": 1,
        "adults": 2,
    })
    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    assert parsed.netloc == "www.expedia.com.au"
    assert parsed.path == "/Hotel-Search"
    assert params["destination"] == ["Newcastle NSW, Australia"]
    assert params["startDate"] == ["2026-10-23"]
    assert params["endDate"] == ["2026-10-25"]
    assert params["d1"] == ["2026-10-23"]
    assert params["d2"] == ["2026-10-25"]
    assert params["rooms"] == ["1"]
    assert params["adults"] == ["2"]


def test_expedia_booking_url_rejects_invalid_stay():
    values = {
        "destination": "Sydney",
        "check_in": dt.date(2026, 10, 23),
        "check_out": dt.date(2026, 10, 23),
        "rooms": 1,
        "adults": 2,
    }
    try:
        _build_expedia_booking_url(values)
    except ValueError:
        return
    raise AssertionError("same-day check-out should be rejected")


def test_expedia_flight_url_contains_return_legs():
    url = _build_expedia_booking_url({
        "search_type": "flights",
        "origin": "Sydney (SYD)",
        "destination": "Melbourne (MEL)",
        "check_in": dt.date(2026, 10, 23),
        "check_out": dt.date(2026, 10, 25),
        "rooms": 1,
        "adults": 2,
    })
    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query)
    assert parsed.path == "/Flights-Search"
    assert params["trip"] == ["roundtrip"]
    assert params["leg1"] == [
        "from:Sydney (SYD),to:Melbourne (MEL),departure:10/23/2026TANYT"]
    assert params["leg2"] == [
        "from:Melbourne (MEL),to:Sydney (SYD),departure:10/25/2026TANYT"]
    assert params["passengers"] == ["adults:2"]


def test_expedia_package_url_contains_origin_and_stay():
    url = _build_expedia_booking_url({
        "search_type": "package",
        "origin": "Brisbane",
        "destination": "Fiji",
        "check_in": dt.date(2026, 11, 10),
        "check_out": dt.date(2026, 11, 17),
        "rooms": 2,
        "adults": 2,
    })
    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query)
    assert parsed.path == "/Holidays"
    assert params["origin"] == ["Brisbane"]
    assert params["destination"] == ["Fiji"]
    assert params["startDate"] == ["2026-11-10"]
    assert params["endDate"] == ["2026-11-17"]
    assert params["packageType"] == ["fh"]
