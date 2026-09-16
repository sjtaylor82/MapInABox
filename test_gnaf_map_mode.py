from street_mode import StreetModeMixin


class _Host(StreetModeMixin):
    pass


def test_gnaf_is_available_from_map_mode_country_context():
    host = _Host()
    host._current_country_code = ""
    host.last_country_found = "Australia"

    assert host._gnaf_available_here() is True


def test_gnaf_is_unavailable_outside_australia():
    host = _Host()
    host._current_country_code = ""
    host.last_country_found = "New Zealand"

    assert host._gnaf_available_here() is False
