import datetime as dt
import urllib.parse

from tools import _build_booking_com_url


def _values(search_type):
    return {
        "search_type": search_type,
        "origin": "Sydney (SYD)",
        "destination": "Melbourne (MEL)",
        "check_in": dt.date(2026, 10, 23),
        "check_out": dt.date(2026, 10, 25),
        "rooms": 1,
        "adults": 2,
    }


def test_booking_com_hotel_url():
    parsed = urllib.parse.urlparse(_build_booking_com_url(_values("hotels")))
    params = urllib.parse.parse_qs(parsed.query)
    assert parsed.path == "/searchresults.html"
    assert params["ss"] == ["Melbourne (MEL)"]
    assert params["checkin"] == ["2026-10-23"]
    assert params["checkout"] == ["2026-10-25"]
    assert params["group_adults"] == ["2"]
    assert params["no_rooms"] == ["1"]


def test_booking_com_flight_url():
    parsed = urllib.parse.urlparse(_build_booking_com_url(_values("flights")))
    params = urllib.parse.parse_qs(parsed.query)
    assert parsed.path == "/flights/index.en-gb.html"
    assert params["type"] == ["ROUNDTRIP"]
    assert params["depart"] == ["Sydney (SYD)"]
    assert params["arrive"] == ["Melbourne (MEL)"]
    assert params["departDate"] == ["2026-10-23"]
    assert params["returnDate"] == ["2026-10-25"]


def test_booking_com_package_url():
    parsed = urllib.parse.urlparse(_build_booking_com_url(_values("package")))
    params = urllib.parse.parse_qs(parsed.query)
    assert parsed.path == "/packages.html"
    assert params["package_type"] == ["flight_hotel"]
    assert params["origin"] == ["Sydney (SYD)"]
    assert params["destination"] == ["Melbourne (MEL)"]
    assert params["nr_rooms"] == ["1"]


def test_booking_com_rejects_invalid_dates():
    values = _values("hotels")
    values["check_out"] = values["check_in"]
    try:
        _build_booking_com_url(values)
    except ValueError:
        return
    raise AssertionError("same-day end date should be rejected")
