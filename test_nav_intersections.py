from nav import NavigationEngine


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
