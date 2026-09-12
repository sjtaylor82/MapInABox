from poi_search import _sort_pois_from_position


def test_pois_are_resorted_from_current_position_not_stored_distance():
    pois = [
        {"label": "Old nearest", "lat": 0.0, "lon": 0.009, "dist": 100},
        {"label": "Current nearest", "lat": 0.0, "lon": 0.006, "dist": 900},
    ]

    result = _sort_pois_from_position(pois, 0.0, 0.0, radius_m=1000)

    assert [poi["label"] for poi in result] == [
        "Current nearest", "Old nearest"]
    assert result[0]["dist"] < result[1]["dist"]
    assert result[0]["distance_m"] == result[0]["dist"]


def test_pois_outside_current_search_radius_are_removed():
    pois = [
        {"label": "Inside", "lat": 0.0, "lon": 0.004, "dist": 9999},
        {"label": "Outside", "lat": 0.0, "lon": 0.012, "dist": 1},
    ]

    result = _sort_pois_from_position(pois, 0.0, 0.0, radius_m=1000)

    assert [poi["label"] for poi in result] == ["Inside"]
