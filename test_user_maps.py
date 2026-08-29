import user_maps


def test_imported_source_suggests_map_beside_it(tmp_path):
    source = tmp_path / "campus.plan.pdf"
    assert user_maps.suggested_save_path(str(source)) == str(
        tmp_path / "campus.plan.miabmap")


def test_import_without_source_has_no_suggested_save_path():
    assert user_maps.suggested_save_path("") == ""


def _place(identifier, name, x, y):
    return {"id": identifier, "name": name, "description": "", "x": x, "y": y}


def test_route_follows_drawn_paths_instead_of_crow_flight():
    data = user_maps.new_map("Campus", 100, 100)
    data["paths"] = [
        {"points": [[0, 10], [90, 10]]},
        {"points": [[90, 10], [90, 90]]},
    ]
    data["places"] = [
        _place("library", "Library", 5, 10),
        _place("block", "A Block", 90, 85),
    ]
    route = user_maps.find_route(data, "library", "block")
    assert round(route.distance) == 160
    assert route.points == [(5.0, 10.0), (90.0, 10.0), (90.0, 85.0)]


def test_parallel_aisles_do_not_teleport():
    data = user_maps.new_map("Shopping centre", 100, 100)
    data["paths"] = [
        {"points": [[0, 10], [90, 10]]},
        {"points": [[0, 20], [90, 20]]},
    ]
    data["places"] = [
        _place("shop-a", "Shop A", 5, 10),
        _place("shop-b", "Shop B", 5, 20),
    ]
    try:
        user_maps.find_route(data, "shop-a", "shop-b")
    except ValueError as exc:
        assert "not connected" in str(exc)
    else:
        raise AssertionError("The router connected separate parallel aisles")


def test_drawn_crossing_becomes_a_route_junction():
    data = user_maps.new_map("Crossing", 100, 100)
    data["paths"] = [
        {"points": [[0, 50], [100, 50]]},
        {"points": [[50, 0], [50, 100]]},
    ]
    data["places"] = [
        _place("west", "West", 5, 50),
        _place("north", "North", 50, 95),
    ]
    route = user_maps.find_route(data, "west", "north")
    assert route.points == [(5.0, 50.0), (50.0, 50.0), (50.0, 95.0)]


def test_internal_osm_label_is_not_pedestrian_information():
    from geo import INTERNAL_ROAD_LABELS
    assert "trunk_link" in INTERNAL_ROAD_LABELS


def test_freehand_wobble_is_simplified_without_losing_a_turn():
    straight = user_maps.simplify_points([(0, 0), (10, 1), (20, -1), (30, 0)])
    corner = user_maps.simplify_points([(0, 0), (20, 0), (20, 20)])
    assert straight == [(0, 0), (30, 0)]
    assert corner == [(0, 0), (20, 0), (20, 20)]


def test_browsing_can_only_take_connected_paths():
    data = user_maps.new_map("Shopping centre", 100, 100)
    data["paths"] = [
        {"points": [[0, 10], [90, 10]]},
        {"points": [[0, 20], [90, 20]]},
    ]
    data["places"] = [
        _place("shop-a", "Shop A", 5, 10),
        _place("shop-b", "Shop B", 5, 20),
    ]
    coords, edges, places = user_maps.build_graph(data)
    node = places["shop-a"]
    assert user_maps.connected_step(coords, edges, node, 0, 1) is None
    east = user_maps.connected_step(coords, edges, node, 1, 0)
    assert east is not None
    assert coords[east[2]][1] == 10


def test_close_stroke_endpoints_do_not_create_an_invisible_connection():
    data = user_maps.new_map("Walls matter", 100, 100)
    data["paths"] = [
        {"points": [[0, 10], [40, 10]]},
        {"points": [[43, 10], [90, 10]]},
    ]
    data["places"] = [
        _place("left", "Left aisle", 5, 10),
        _place("right", "Right aisle", 85, 10),
    ]
    try:
        user_maps.find_route(data, "left", "right")
    except ValueError as exc:
        assert "not connected" in str(exc)
    else:
        raise AssertionError("Nearby endpoints were treated as a doorway")


def test_deliberately_drawn_endpoint_snaps_visibly_to_existing_path():
    paths = [{"points": [[0, 10], [100, 10]]}]
    snapped = user_maps.snap_drawn_endpoints(paths, [(50, 14), (50, 80)])
    assert snapped == [(50.0, 10.0), (50, 80)]


def test_multi_floor_map_validates_as_floor_native_data():
    data = user_maps.new_map("Library")
    first = user_maps.new_floor("Floor 1", 500, 386, 0)
    second = user_maps.new_floor("Floor 2", 500, 386, 1)
    for index, floor in enumerate((first, second), 1):
        floor["paths"] = [{"points": [[0, 10], [100, 10]]}]
        floor["places"] = [_place(f"start-{index}", f"Start {index}", 10, 10)]
        floor["start"] = f"start-{index}"
    data["floors"] = [first, second]
    validated = user_maps.validate_map(data)
    assert [floor["name"] for floor in user_maps.floors_for(validated)] == ["Floor 1", "Floor 2"]


def test_grid_labels_and_directional_jump_support_free_exploration():
    data = user_maps.new_map("Grid", 100, 100)
    data["places"] = [
        _place("west", "West room", 10, 50),
        _place("east", "East room", 80, 50),
        _place("north", "North room", 50, 90),
    ]
    assert user_maps.next_place_in_direction(data, 50, 50, 1, 0)["id"] == "east"
    assert user_maps.next_place_in_direction(data, 50, 50, 0, 1)["id"] == "north"
    assert user_maps.places_in_grid_cell(data, 82, 52, 10)[0]["id"] == "east"


def test_grid_row_counts_labels_outside_the_current_cell():
    data = user_maps.new_map("Grid", 100, 100)
    data["places"] = [
        _place("west", "West room", 10, 55),
        _place("east", "East room", 80, 52),
        _place("north", "North room", 50, 75),
    ]
    assert [place["id"] for place in
            user_maps.places_in_grid_row(data, 51, 10)] == ["west", "east"]
    assert user_maps.places_in_grid_row(data, 61, 10) == []


def test_exploration_bounds_trim_empty_outer_map_space():
    data = user_maps.new_map("Sparse map", 500, 350)
    data["places"] = [
        _place("west", "West", 200, 140),
        _place("east", "East", 300, 210),
    ]
    left, right, bottom, top = user_maps.exploration_bounds(data)
    assert (left, right, bottom, top) == (175.0, 325.0, 122.5, 227.5)


def test_exploration_bounds_include_drawn_paths_and_barriers():
    data = user_maps.new_map("Geometry", 500, 350)
    data["places"] = [_place("centre", "Centre", 250, 175)]
    data["paths"] = [{"points": [[100, 100], [400, 250]]}]
    data["barriers"] = [{"points": [[90, 90], [90, 260]]}]
    left, right, bottom, top = user_maps.exploration_bounds(data)
    assert left <= 90 and right >= 400 and bottom <= 90 and top >= 260


def test_directional_jump_stays_strictly_in_its_grid_row_or_column():
    data = user_maps.new_map("Directional grid", 200, 200)
    data["places"] = [
        _place("east-row", "East in the same row", 100, 52),
        _place("east-diagonal", "Far north-east", 80, 120),
        _place("north-column", "North in the same column", 52, 110),
    ]
    assert user_maps.next_place_in_direction(data, 50, 50, 1, 0, 10)["id"] == "east-row"
    assert user_maps.next_place_in_direction(data, 50, 50, 0, 1, 10)["id"] == "north-column"

    data["places"] = [_place("diagonal", "Diagonal only", 80, 120)]
    assert user_maps.next_place_in_direction(data, 50, 50, 1, 0, 10) is None


def test_barrier_crossing_is_audible_but_does_not_define_movement():
    data = user_maps.new_map("Barrier", 100, 100)
    data["barriers"] = [{"points": [[50, 0], [50, 100]]}]
    assert user_maps.crosses_barrier(data, (40, 50), (60, 50))
    assert not user_maps.crosses_barrier(data, (20, 50), (40, 50))
