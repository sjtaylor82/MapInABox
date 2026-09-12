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
    assert instructions[0][2] == "Continue across Side Street."
    assert all("trunk_link" not in instruction[2] for instruction in instructions)
    assert instructions[-1][1] > 90


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


def test_route_start_puts_first_instruction_in_focused_native_row():
    host = _NativeNavHost()

    host._nav_begin_route("Ibis Hotel")

    assert len(host.native_rows) == 1
    message, clear_model = host.native_rows[0]
    assert message.startswith("In 37 metres, continue across Title Street.")
    assert "Step 1 of 2" in message
    assert clear_model is True
    assert host._poi_context_generation == 1
    assert host.transient == []


def test_arrival_context_waits_for_final_instruction():
    assert _NAV_ARRIVAL_CONTEXT_DELAY_MS == 2500
