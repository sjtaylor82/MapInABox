from core import MapNavigator, _ambient_sound_place


def test_southern_philippine_water_is_celebes_sea():
    assert MapNavigator._ocean_name(None, 4.49350, 120.43384) == "Celebes Sea"


def test_open_water_uses_ocean_sound_despite_nearby_country_context():
    assert _ambient_sound_place("Philippines", on_land=False) == "ocean"
    assert _ambient_sound_place("Philippines", on_land=True) == "Philippines"
