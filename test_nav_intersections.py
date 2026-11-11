from nav import NavMixin, NavigationEngine, _NAV_ARRIVAL_CONTEXT_DELAY_MS


def test_osm_instructions_include_cross_streets_but_not_internal_labels():
    graph = {
        "nodes": {
            0: (0.0, 0.0),
            1: (0.0, 0.0009),
            2: (0.0, 0.0018),
            3: (0.0009, 0.0009),
            4: (-0.0009, 0.0009),
        },
        "edges": {
            0: [(1, "Main Road")],
            1: [(0, "Main Road"), (2, "Main Road"),
                (3, "Side Street"), (4, "trunk_link")],
            2: [(1, "Main Road")],
            3: [(1, "Side Street")],
            4: [(1, "trunk_link")],
        },
        "node_streets": {
            0: {"Main Road"},
            1: {"Main Road", "Side Street", "trunk_link"},
            2: {"Main Road"},
            3: {"Side Street"},
            4: {"trunk_link"},
        },
        "intersections": {1},
    }
    engine = NavigationEngine(graph)
    instructions = engine._build_instructions([0, 1, 2], "Destination")
    # Side Street ends here on the north side; heading east, that is the left.
    assert instructions[0][2] == "Continue past Side Street on your left."
    assert all("trunk_link" not in instruction[2] for instruction in instructions)
    assert instructions[-1][1] > 90


def _crossroads(main_jog=0.0, edge_info=None):
    """Main Road heading east, crossed at node 1 by Big Road running north-south."""
    return {
        "nodes": {
            0: (0.0, 0.0),
            1: (0.0, 0.0009),
            2: (main_jog, 0.0018),
            3: (0.0009, 0.0009),
            4: (-0.0009, 0.0009),
        },
        "edges": {
            0: [(1, "Main Road")],
            1: [(0, "Main Road"), (2, "Main Road"), (3, "Big Road"), (4, "Big Road")],
            2: [(1, "Main Road")],
            3: [(1, "Big Road")],
            4: [(1, "Big Road")],
        },
        "node_streets": {
            0: {"Main Road"}, 1: {"Main Road", "Big Road"}, 2: {"Main Road"},
            3: {"Big Road"}, 4: {"Big Road"},
        },
        "intersections": {1},
        "edge_info": edge_info or {},
    }


def test_crossing_a_through_road_names_its_size():
    info = {"kind": "primary", "lanes": 6, "oneway": False}
    graph = _crossroads(edge_info={frozenset((1, 3)): info, frozenset((1, 4)): info})
    instructions = NavigationEngine(graph)._build_instructions([0, 1, 2], "Destination")
    assert instructions[0][2] == "Continue across Big Road, a 6-lane major road."


def test_one_way_lanes_are_not_claimed_for_the_whole_road():
    info = {"kind": "primary", "lanes": 3, "oneway": True}
    graph = _crossroads(edge_info={frozenset((1, 3)): info, frozenset((1, 4)): info})
    instructions = NavigationEngine(graph)._build_instructions([0, 1, 2], "Destination")
    assert instructions[0][2] == "Continue across Big Road, a major road."


def test_jog_across_a_road_is_a_crossing_not_a_turn():
    # Main Road carries on slightly to the left after crossing Big Road, as
    # First Avenue does at Aerodrome Road.
    info = {"kind": "primary"}
    graph = _crossroads(main_jog=0.0004,
                        edge_info={frozenset((1, 3)): info, frozenset((1, 4)): info})
    instructions = NavigationEngine(graph)._build_instructions([0, 1, 2], "Destination")
    assert instructions[0][2] == (
        "cross Big Road, a major road, then slight left to stay on Main Road.")


class _MapPanel:
    def __init__(self):
        self.position = None

    def set_position(self, lat, lon, street_mode, street_label):
        self.position = (lat, lon, street_mode, street_label)


class _NavState:
    step = 0


class _NavHost(NavMixin):
    def __init__(self):
        self.lat = 0.0
        self.lon = 0.0
        self.street_label = "Start Street"
        self._nav = _NavState()
        self._nav_step = 0
        self._nav_instructions = [
            (1, 37, "Continue across Title Street.", -27.5201, 153.2209),
            (2, 97, "Left onto Princeton Avenue.", -27.5198, 153.2210),
        ]
        self._nav_route = []
        self._walk_graph = None
        self._nav_route_mode = "walking"
        self._nav_total_min = 2
        self.map_panel = _MapPanel()
        self.transient = []
        self.persistent = []

    def _announce_transient(self, message):
        self.transient.append(message)

    def update_ui(self, message, force=False):
        self.persistent.append(message)


def test_route_start_moves_to_and_transiently_announces_first_instruction():
    host = _NavHost()

    host._nav_begin_route("Princeton Ave at Garter St")

    assert (host.lat, host.lon) == (-27.5201, 153.2209)
    assert host.map_panel.position == (
        -27.5201, 153.2209, True, "Start Street")
    assert host._nav_step == 1
    assert host._nav.step == 1
    assert host.transient
    assert "Step 1 of 2" in host.transient[0]
    assert host.persistent == []


class _NativeNavHost(_NavHost):
    def __init__(self):
        super().__init__()
        self.native_rows = []

    def _replace_poi_action_item(self, message, clear_model=False):
        self.native_rows.append((message, clear_model))


def test_route_start_leaves_the_focused_row_alone():
    # Writing instruction one into the focused row replaced what the user
    # had there; Insert+Tab still read the route summary long afterwards.
    host = _NativeNavHost()

    host._nav_begin_route("Ibis Hotel")

    assert host.native_rows == []
    assert host.persistent == []
    assert host.transient[0].startswith("In 37 metres, continue across Title Street.")
    assert "Step 1 of 2" in host.transient[0]
    assert host._poi_context_generation == 1


def test_arrival_context_waits_for_final_instruction():
    assert _NAV_ARRIVAL_CONTEXT_DELAY_MS == 2500
