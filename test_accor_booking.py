import datetime as dt
import urllib.parse

from tools import _build_accor_booking_url, _parse_ddmmyyyy


def test_parses_ddmmyyyy_dates():
    assert _parse_ddmmyyyy("19092026") == dt.date(2026, 9, 19)


def test_rejects_nonexistent_or_non_compact_dates():
    for value in ("2026-09-19", "31022027", "190926"):
        try:
            _parse_ddmmyyyy(value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted invalid compact date {value!r}")


def test_builds_accor_search_handoff():
    url = _build_accor_booking_url({
        "destination": "Newcastle NSW, Australia",
        "check_in": dt.date(2026, 9, 19),
        "check_out": dt.date(2026, 9, 22),
        "rooms": 1,
        "adults": 2,
        "children": 0,
        "accessible": True,
    })
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert query["destination"] == ["Newcastle NSW, Australia"]
    assert query["dayIn"] == ["19"]
    assert query["monthIn"] == ["09"]
    assert query["yearIn"] == ["2026"]
    assert query["nightNb"] == ["3"]
    assert query["adultNumber"] == ["2"]
    assert query["accessibleRooms"] == ["true"]
