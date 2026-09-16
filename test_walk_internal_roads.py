from walk import WalkMixin
from street_data import StreetFetcher
from street_search_frame import _StreetSearchFrame


class _Walker(WalkMixin):
    pass


def test_internal_link_labels_do_not_enter_walking_graph():
    walker = _Walker()
    walker._road_segments = [
        {"name": "Harries Road", "coords": [(0.0, 0.0), (0.0, 0.001)]},
        {"name": "Old Cleveland Road", "coords": [(-0.001, 0.0), (0.0, 0.0), (0.001, 0.0)]},
        {"name": "trunk_link", "coords": [(0.0, 0.0), (0.001, 0.001)]},
    ]

    graph = walker._build_walk_graph()

    assert all(
        street != "trunk_link"
        for streets in graph["node_streets"].values()
        for street in streets
    )


def test_internal_link_labels_do_not_enter_street_lists():
    segments = [
        {"name": "Harries Road", "raw_name": "Harries Road"},
        {"name": "Main Road", "raw_name": "Main Road"},
        {"name": "Station Street", "raw_name": "Station Street"},
        # Some cached data incorrectly retains the fallback as raw_name too.
        {"name": "trunk_link", "raw_name": "trunk_link"},
    ]

    expected = ["Harries Road", "Main Road", "Station Street"]
    assert StreetFetcher.street_names_from_segments(segments) == expected

    frame = _StreetSearchFrame.__new__(_StreetSearchFrame)
    frame._nav = type("Nav", (), {"_road_segments": segments})()
    assert frame._street_names_from_segments() == expected
