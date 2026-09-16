from street_mode import StreetModeMixin
from street_survey import StreetSurveyMixin
from free import FreeExploreEngine


class _StreetMode(StreetModeMixin):
    pass


class _StreetSurvey(StreetSurveyMixin):
    pass


def _parallel_street_survey():
    mode = _StreetSurvey()
    mode.lat = 0.0
    mode.lon = 0.0
    mode.street_label = "Queen Street"
    mode._address_points = []
    mode._all_pois = []
    mode._road_segments = [
        {"name": "Queen Street",
         "coords": [(0.0, -0.002), (0.0, 0.002)]},
        {"name": "North Street",
         "coords": [(0.001, -0.002), (0.001, 0.002)]},
        {"name": "South Street",
         "coords": [(-0.001, -0.002), (-0.001, 0.002)]},
        {"name": "Cross Street",
         "coords": [(-0.002, 0.0005), (0.002, 0.0005)]},
    ]
    return mode


def test_shift_sideways_step_is_perpendicular_to_current_street():
    mode = _parallel_street_survey()
    left_lat, left_lon = mode._street_survey_sideways_target(-1, 20.0)
    right_lat, right_lon = mode._street_survey_sideways_target(1, 20.0)
    assert left_lat > mode.lat
    assert right_lat < mode.lat
    assert abs(left_lon - mode.lon) < 1e-9
    assert abs(right_lon - mode.lon) < 1e-9


def test_grid_movement_releases_street_follow_pin():
    mode = _StreetMode()
    mode._jump_street_label = "Queen Street"
    mode._jump_street_pin_lat = -27.5
    mode._jump_street_pin_lon = 153.2
    mode._street_arrow_engine = object()
    mode._street_arrow_axis_heading = 90.0
    mode._street_auto_land_done = False

    mode._release_street_pin_for_grid_move()

    assert mode._jump_street_label is None
    assert mode._jump_street_pin_lat is None
    assert mode._jump_street_pin_lon is None
    assert mode._street_arrow_engine is None
    assert mode._street_arrow_axis_heading is None
    assert mode._street_auto_land_done is True


def test_shift_follow_axis_matches_local_street_orientation():
    mode = _StreetMode()
    mode.lat = 0.0
    mode.lon = 0.0
    mode.street_label = "Queen Street"
    mode._jump_street_label = None
    mode._road_segments = [{
        "name": "Queen Street",
        "coords": [(0.0, -0.001), (0.0, 0.001)],
    }]
    assert mode._street_shift_follow_axis() == "horizontal"

    mode.street_label = "King Street"
    mode._road_segments = [{
        "name": "King Street",
        "coords": [(-0.001, 0.0), (0.001, 0.0)],
    }]
    assert mode._street_shift_follow_axis() == "vertical"


def test_horizontal_street_follows_up_and_retraces_down():
    mode = _StreetMode()
    mode.lat = 0.0
    mode.lon = 0.0
    mode.street_label = "Queen Street"
    mode._jump_street_label = "Queen Street"
    mode._road_segments = [{
        "name": "Queen Street",
        "coords": [(0.0, -0.001), (0.0, 0.0), (0.0, 0.001)],
    }]
    mode.settings = {"logging": {}}

    assert mode._step_along_pinned_street(
        1, 20.0, heading_hint=90.0)
    east = (mode.lat, mode.lon)
    assert east[1] > 0.0
    assert mode._step_along_pinned_street(
        -1, 20.0, heading_hint=90.0)
    assert abs(mode.lat) < 1e-9
    assert abs(mode.lon) < 1e-9


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
