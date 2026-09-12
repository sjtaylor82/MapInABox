from street_mode import StreetModeMixin
from free import FreeExploreEngine


class _StreetMode(StreetModeMixin):
    pass


def test_arrow_movement_releases_number_but_keeps_street_pin():
    mode = _StreetMode()
    mode._jump_street_label = "Elizabeth Drive"
    mode._jump_street_pin_lat = -27.52017
    mode._jump_street_pin_lon = 153.22098
    mode._jump_address_number = "22"
    mode._jump_address_street = "Elizabeth Drive"
    mode._pending_jump_address_number = "22"
    mode._pending_jump_address_street = "Elizabeth Drive"
    mode._pending_jump_address_lat = -27.52012
    mode._pending_jump_address_lon = 153.22071

    mode._release_numbered_address_pin_after_move()

    assert mode._jump_street_label == "Elizabeth Drive"
    assert mode._jump_street_pin_lat == -27.52017
    assert mode._jump_street_pin_lon == 153.22098
    assert mode._jump_address_number is None
    assert mode._jump_address_street is None
    assert mode._pending_jump_address_number is None
    assert mode._pending_jump_address_street is None
    assert mode._pending_jump_address_lat is None
    assert mode._pending_jump_address_lon is None


def test_pinned_street_steps_continue_around_a_curve():
    mode = _StreetMode()
    mode.lat = 0.0
    mode.lon = 0.0
    mode.street_label = "Elizabeth Drive"
    mode._jump_street_label = "Elizabeth Drive"
    mode._road_segments = [{
        "name": "Elizabeth Drive",
        "coords": [(0.0, 0.0), (0.0003, 0.0), (0.0003, 0.0003)],
    }]
    mode.settings = {"logging": {}}

    positions = []
    for _ in range(3):
        assert mode._step_along_pinned_street(1, 20.0)
        positions.append((mode.lat, mode.lon))

    assert len(set(positions)) == 3
    assert positions[-1][1] > positions[0][1]


def test_road_step_can_join_the_middle_of_a_cross_street():
    engine = FreeExploreEngine(step_m=15.0)
    engine.set_segments([
        {"name": "Charter Street",
         "coords": [(0.0, 0.0), (0.0004, 0.0)]},
        {"name": "Crown Road",
         "coords": [(0.0004, -0.0004), (0.0004, 0.0004)]},
    ])
    engine.start(0.0, 0.0, preferred_street="Charter Street", heading_deg=0.0)

    for _ in range(8):
        engine.step_forward()
        if engine.street_name == "Crown Road":
            break

    assert engine.street_name == "Crown Road"


def test_backward_step_retraces_a_cross_street_transition():
    engine = FreeExploreEngine(step_m=15.0)
    engine.set_segments([
        {"name": "Queen Street",
         "coords": [(0.0, 0.0004), (0.0, 0.0)]},
        {"name": "Wellington Street",
         "coords": [(0.0, 0.0), (0.0004, 0.0)]},
    ])
    engine.start(
        0.0, 0.0004, preferred_street="Queen Street", heading_deg=270.0)

    position_before_turn = None
    for _ in range(8):
        position_before_turn = (
            engine.street_name, engine.state.path_id,
            engine.state.path_index, engine.position,
        )
        engine.step_forward()
        if engine.street_name == "Wellington Street":
            break

    assert engine.street_name == "Wellington Street"
    engine.step_backward()
    assert engine.street_name == position_before_turn[0] == "Queen Street"
    assert engine.state.path_id == position_before_turn[1]
    assert engine.state.path_index == position_before_turn[2]
    assert engine.position == position_before_turn[3]


def test_street_mode_follows_current_road_without_search_pin():
    mode = _StreetMode()
    mode.lat = 0.0
    mode.lon = 0.0
    mode.street_label = "Main Road"
    mode._jump_street_label = None
    mode._road_segments = [{
        "name": "Main Road",
        "coords": [(0.0, 0.0), (0.0003, 0.0002)],
    }]
    mode.settings = {"logging": {}}
    mode._nearest_road = lambda lat, lon: ("Main Road", None)

    assert mode._step_along_pinned_street(1, 20.0)
    first = (mode.lat, mode.lon)
    assert mode._jump_street_label is None
    assert mode._step_along_pinned_street(1, 20.0)
    assert (mode.lat, mode.lon) != first
