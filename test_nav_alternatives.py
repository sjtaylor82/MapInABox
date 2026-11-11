"""Walking routes: the start stays on the walker's street, and clearly different
routes to the same place are offered as alternatives."""
from nav import NavigationEngine


def _graph(edges_by_street, coords):
    edges, node_streets = {}, {}
    for street, chain in edges_by_street.items():
        for a, b in zip(chain, chain[1:]):
            edges.setdefault(a, []).append((b, street))
            edges.setdefault(b, []).append((a, street))
            node_streets.setdefault(a, set()).add(street)
            node_streets.setdefault(b, set()).add(street)
    return {"nodes": coords, "edges": edges, "node_streets": node_streets,
            "intersections": {n for n, s in node_streets.items() if len(s) > 1}}


# A block about 100 m square. The walker is on Front Street, on the south
# side; Back Lane runs along the north. Two ways east to the station at
# node 3: along Front Street, or up Side Street and along Back Lane.
#
#   4 ---- Back Lane ---- 5
#   |                     |
#   Side St           Far St
#   |                     |
#   0 ---- Front St ------ 3 (station)
D = 0.0009   # about 100 m
BLOCK = _graph(
    {"Front Street": [0, 1, 2, 3], "Back Lane": [4, 6, 5],
     "Side Street": [0, 4], "Far Street": [5, 3]},
    {0: (0.0, 0.0), 1: (0.0, D / 3), 2: (0.0, 2 * D / 3), 3: (0.0, D),
     4: (D * 0.9, 0.0), 6: (D * 0.9, D / 2), 5: (D * 0.9, D)})


def test_start_stays_on_the_walkers_street_when_a_lane_is_nearer():
    engine = NavigationEngine(BLOCK)
    # Standing nearer Back Lane's middle point (node 6) than any Front Street
    # point, but on Front Street.
    here = (D * 0.5, D / 2)
    nearest, _ = engine._find_nearest_node_with_distance(*here)
    assert nearest == 6
    msg, ok = engine.find_route_osm(*here, 0.0, D, "Station", from_street="Front Street")
    assert ok, msg
    assert engine.route[0] in (1, 2)          # a Front Street point
    assert "Back Lane" not in engine.instructions[0][2]


def test_far_from_own_street_falls_back_to_nearest_point():
    engine = NavigationEngine(BLOCK)
    # Front Street is more than START_ON_STREET_M away.
    msg, ok = engine.find_route_osm(D * 0.9, D / 2, 0.0, D, "Station",
                                    from_street="Front Street")
    assert ok, msg
    assert engine.route[0] == 6


def test_much_longer_route_is_not_offered():
    engine = NavigationEngine(BLOCK)
    msg, ok = engine.find_route_osm(0.0, 0.0, 0.0, D, "Station", alternatives=True)
    assert ok, msg
    # Front Street is ~100 m; Side Street, Back Lane, Far Street is ~300 m,
    # far more than 30% longer, so it is not offered.
    assert engine.alternatives == []


def test_two_similar_routes_are_both_offered_shortest_first():
    # Two parallel streets of nearly equal length between A (0) and B (3).
    g = _graph(
        {"North Road": [0, 1, 3], "South Road": [0, 2, 3]},
        {0: (0.0, 0.0), 1: (0.0003, D / 2), 2: (-0.00033, D / 2), 3: (0.0, D)})
    engine = NavigationEngine(g)
    msg, ok = engine.find_route_osm(0.0, 0.0, 0.0, D, "B", alternatives=True)
    assert ok, msg
    assert [o["via"] for o in engine.alternatives] == ["North Road", "South Road"]
    lines = engine.describe_alternatives()
    assert lines[0].startswith("Via North Road") and "shortest" in lines[0]
    assert lines[1].startswith("Via South Road") and "longer" in lines[1]
    assert engine.route == [0, 1, 3]
    engine.use_alternative(1)
    assert engine.route == [0, 2, 3]
    assert engine.active and engine.step == 1


def test_without_alternatives_behaviour_is_unchanged():
    g = _graph(
        {"North Road": [0, 1, 3], "South Road": [0, 2, 3]},
        {0: (0.0, 0.0), 1: (0.0003, D / 2), 2: (-0.00033, D / 2), 3: (0.0, D)})
    engine = NavigationEngine(g)
    msg, ok = engine.find_route_osm(0.0, 0.0, 0.0, D, "B")
    assert ok and msg.startswith("Route to B.")
    assert engine.alternatives == []
    assert engine.route == [0, 1, 3]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)


def test_choosing_a_route_switches_to_it_and_escape_takes_the_shortest():
    from unittest import mock
    import nav

    class Dialog:
        answer, pick = None, 0
        def __init__(self, parent, message, title, choices):
            Dialog.choices = choices
        def SetSelection(self, i): pass
        def ShowModal(self): return Dialog.answer
        def GetSelection(self): return Dialog.pick
        def Destroy(self): pass

    g = _graph(
        {"North Road": [0, 1, 3], "South Road": [0, 2, 3]},
        {0: (0.0, 0.0), 1: (0.0003, D / 2), 2: (-0.00033, D / 2), 3: (0.0, D)})

    class Host(nav.NavMixin):
        settings = {}
        def __init__(self):
            self._nav = NavigationEngine(g)
            self.begun = None
        def _sync_nav_state_from_engine(self):
            self._nav_route = self._nav.route
        def _nav_begin_route(self, dest_name, provider="Route"):
            self.begun = dest_name

    for answer, pick, expected in ((nav.wx.ID_OK, 1, [0, 2, 3]),
                                   (nav.wx.ID_CANCEL, 1, [0, 1, 3])):
        host = Host()
        host._nav.find_route_osm(0.0, 0.0, 0.0, D, "B", alternatives=True)
        Dialog.answer, Dialog.pick = answer, pick
        with mock.patch.object(nav.wx, "SingleChoiceDialog", Dialog), \
                mock.patch.object(nav, "miab_log", lambda *a, **k: None):
            host._nav_choose_route("B")
        assert len(Dialog.choices) == 2
        assert host._nav_route == expected
        assert host.begun == "B"
