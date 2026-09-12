from street_mode import _street_area_identity


def test_drops_reverse_geocode_boundary_when_preferred_city_differs():
    geo = {
        "suburb": "The Hill",
        "osm_type": "relation",
        "osm_id": 5989984,
    }

    assert _street_area_identity(geo, "Newcastle") == (
        "Newcastle", None, None)


def test_keeps_boundary_when_preferred_name_matches_reverse_geocode():
    geo = {
        "suburb": "Newcastle",
        "osm_type": "relation",
        "osm_id": 1234,
    }

    assert _street_area_identity(geo, "newcastle") == (
        "newcastle", "relation", 1234)


def test_reverse_geocode_name_and_boundary_are_used_without_preference():
    geo = {
        "suburb": "The Hill",
        "osm_type": "relation",
        "osm_id": 5989984,
    }

    assert _street_area_identity(geo) == (
        "The Hill", "relation", 5989984)
