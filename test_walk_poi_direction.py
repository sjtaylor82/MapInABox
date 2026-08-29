from walk import WalkMixin


class _Walker(WalkMixin):
    def __init__(self):
        self._walk_node = 0
        self._walk_heading = 0.0
        self._walk_graph = {"nodes": {0: (0.0, 0.0)}}
        self.settings = {
            "walk_poi_radius_m": 100,
            "walk_announce_category": False,
        }
        self._pois = [
            {"label": "North shop", "lat": 0.00045, "lon": 0.0},
            {"label": "East shop", "lat": 0.0, "lon": 0.00045},
            {"label": "South shop", "lat": -0.00045, "lon": 0.0},
        ]
        self.messages = []

    def update_ui(self, message, force=False):
        self.messages.append(message)

    def _walk_get_cross_streets(self, node_id, street):
        return ["North Street", "South Street"]

    def _poi_grid_nearby(self, lat, lon, radius):
        return self._pois


def test_walking_turn_pois_follow_selected_branch_bearing():
    walker = _Walker()
    north = walker._walk_pois_along_option({"bearing": 0.0})
    east = walker._walk_pois_along_option({"bearing": 90.0})
    south = walker._walk_pois_along_option({"bearing": 180.0})
    assert north == ["North shop"]
    assert east == ["East shop"]
    assert south == ["South shop"]


def test_walking_turn_options_use_words_and_compass_not_degrees():
    walker = _Walker()
    text = walker._walk_option_text({
        "relative": -84.0,
        "bearing": 270.0,
        "street": "Old Cleveland Road",
        "is_current_street": False,
    })
    assert text == "Turn left onto Old Cleveland Road, heading west."
    assert "degrees" not in text


def test_virtual_crossing_switches_available_side_streets():
    walker = _Walker()
    walker._walk_graph = {
        "nodes": {
            0: (0.0, 0.0), 1: (0.0, 0.001), 2: (0.0, -0.001),
            3: (0.001, 0.0), 4: (-0.001, 0.0),
        },
        "edges": {
            0: [(1, "Main Road"), (2, "Main Road"),
                (3, "North Street"), (4, "South Street")]
        },
    }
    walker._walk_street = "Main Road"
    walker._walk_heading = 90.0
    walker._walk_prev_node = None
    walker._walk_road_side = "left"
    walker._walk_side_name = "address side"
    before = walker._walk_get_turn_options(0, "Main Road", 90.0)
    assert "North Street" in [option["street"] for option in before]
    assert "South Street" not in [option["street"] for option in before]
    walker._walk_virtual_crossing()
    after = walker._walk_get_turn_options(0, "Main Road", 90.0)
    assert "South Street" in [option["street"] for option in after]
    assert "North Street" not in [option["street"] for option in after]
    assert walker.messages[-1].startswith("Virtual crossing of Main Road")
    assert "safety" not in walker.messages[-1].casefold()


def test_virtual_crossing_changes_nearest_address_number():
    walker = _Walker()
    walker._address_points = [
        {"street": "Main Road", "number": "11", "lat": 0.0, "lon": -0.0001},
        {"street": "Main Road", "number": "12", "lat": 0.0, "lon": 0.0001},
    ]
    walker._walk_road_side = "left"
    assert walker._walk_nearest_address_number(0.0, 0.0, "Main Road", 0.0) == "11"
    walker._walk_road_side = "right"
    assert walker._walk_nearest_address_number(0.0, 0.0, "Main Road", 0.0) == "12"
