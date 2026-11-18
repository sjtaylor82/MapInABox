from unittest import mock

from core import MapNavigator, _ambient_sound_place


def test_southern_philippine_water_is_celebes_sea():
    assert MapNavigator._ocean_name(None, 4.49350, 120.43384) == "Celebes Sea"


def test_open_water_uses_ocean_sound_despite_nearby_country_context():
    assert _ambient_sound_place("Philippines", on_land=False) == "ocean"
    assert _ambient_sound_place("Philippines", on_land=True) == "Philippines"


def test_ambient_sound_changes_at_coast_even_when_country_context_does_not():
    class Host:
        _update_ambient_sound = MapNavigator._update_ambient_sound

        def __init__(self):
            self._last_ambient_sound_place = "Philippines"
            self.sounds_enabled = True
            self._game = mock.Mock(active=False)
            self.settings = {}
            self.played = []

        def _play_location_sound_if_allowed(self, place, continent=""):
            self.played.append((place, continent))

    host = Host()
    assert host._update_ambient_sound("Philippines", False, "Asia")
    assert host.played == [("ocean", "Asia")]
    assert not host._update_ambient_sound("Philippines", False, "Asia")
    assert host._update_ambient_sound("Philippines", True, "Asia")
    assert host.played[-1] == ("Philippines", "Asia")


def test_water_east_of_sydney_is_tasman_sea():
    assert MapNavigator._ocean_name(None, -33.84, 155.0) == "Tasman Sea"
    assert MapNavigator._ocean_name(None, -36.5, 165.0) == "Tasman Sea"
    # East of New Zealand's North Island is still the Pacific.
    assert MapNavigator._ocean_name(None, -36.5, 179.0) != "Tasman Sea"
