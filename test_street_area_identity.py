from street_mode import StreetModeMixin, _street_area_identity, _street_area_key


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


def test_postal_dc_suffix_matches_reverse_geocoded_suburb():
    geo = {
        "suburb": "Coorparoo",
        "osm_type": "relation",
        "osm_id": 1234,
    }

    assert _street_area_key("Coorparoo DC") == _street_area_key("Coorparoo")
    assert _street_area_identity(geo, "Coorparoo DC") == (
        "Coorparoo DC", "relation", 1234)


class _NavStub:
    def __init__(self):
        self.graph = "old graph"

    def set_graph(self, graph):
        self.graph = graph


def test_replacing_road_segments_invalidates_derived_graphs():
    host = StreetModeMixin()
    host._road_segments = ["old segments"]
    host._walk_graph = "old graph"
    host._nav = _NavStub()

    host._replace_road_segments(["new segments"])

    assert host._road_segments == ["new segments"]
    assert host._walk_graph is None
    assert host._nav.graph is None
