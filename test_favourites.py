from core import MapNavigator
from favourites import favourite_key, unified_favourite_items


def test_unified_favourites_mix_all_saved_place_types():
    external = {"name": "Vision Australia", "type": "poi"}
    address = {"name": "454 Glenferrie Road", "type": "place"}
    personal = {"name": "My entrance", "kind": "personal"}
    items = unified_favourite_items([external, address], [personal])

    assert [item[0]["name"] for item in items] == [
        "454 Glenferrie Road", "My entrance", "Vision Australia"]
    assert [item[1] for item in items] == [False, True, False]


def test_address_favourite_uses_saved_address_metadata():
    entry = {
        "kind": "address",
        "name": "Custom label",
        "lat": -37.0,
        "lon": 145.0,
        "meta": {
            "address_number": "454",
            "address_street": "Glenferrie Road",
            "address_lat": -37.83878,
            "address_lon": 145.03306,
        },
    }
    assert MapNavigator._favourite_address_context(None, entry) == (
        "454", "Glenferrie Road", -37.83878, 145.03306)


def test_legacy_address_favourite_recovers_number_and_street_from_name():
    entry = {
        "kind": "address",
        "name": "454 Glenferrie Road, Kooyong",
        "lat": -37.838675,
        "lon": 145.032457,
        "meta": {},
    }
    assert MapNavigator._favourite_address_context(None, entry) == (
        "454", "Glenferrie Road", -37.838675, 145.032457)


def test_address_favourite_identity_does_not_change_with_better_coordinate():
    road_centre = {
        "type": "place", "kind": "address",
        "name": "454 Glenferrie Road, Kooyong",
        "lat": -37.838675, "lon": 145.032457,
    }
    address_point = dict(
        road_centre, lat=-37.83878, lon=145.03306)
    assert favourite_key(road_centre) == favourite_key(address_point)
