from unittest import mock

import lookups


class _Host(lookups.LookupsMixin):
    def __init__(self, lat, lon):
        self.lat, self.lon, self.settings, self.result = lat, lon, {}, None

    def _status_update(self, msg, force=False):
        self.result = msg

    def _do_nearest_land_jump(self, lat, lon, msg, found_country=""):
        self.result = msg


class _SyncThread:
    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)


def test_country_polygons_load_for_point_lookup():
    assert _Host(-33.87, 151.21)._country_at_point(-33.87, 151.21) == "Australia"


def test_ctrl_alt_up_from_sydney_finds_papua_new_guinea():
    host = _Host(-33.87, 151.21)
    with mock.patch.object(lookups.threading, "Thread", _SyncThread), \
            mock.patch.object(lookups.wx, "CallAfter", lambda f, *a, **k: f(*a, **k)):
        host._jump_nearest_land("north")
    assert host.result.startswith("Next country to the north: Papua New Guinea")
