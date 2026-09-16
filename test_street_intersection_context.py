from street_survey import StreetSurveyMixin


class _Fetcher:
    def __init__(self, ranked):
        self.ranked = ranked

    def nearest_roads_with_distances(self, lat, lon, segments):
        return self.ranked


class _Survey(StreetSurveyMixin):
    def __init__(self, postal_distance, ranked):
        self._poi = {"lat": -33.871, "lon": 151.197}
        self._postal_distance = postal_distance
        self._street_fetcher = _Fetcher(ranked)
        self._road_segments = [{
            "name": "Bunn Street",
            "coords": [(-33.871, 151.196), (-33.871, 151.198)],
        }]
        self.settings = {"logging": {"verbose": False}}

    def _current_street_survey_poi(self):
        return self._poi

    def _street_survey_project(self, street_name, lat, lon):
        return (self._postal_distance, 0.0, lat, lon)

    def _street_survey_address_axis(self, street_name):
        # North-south address street.
        return (0.0, 0.0, 0.0, 1.0, 111000.0)


def test_x_uses_nearby_physical_road_when_postal_road_is_distant():
    survey = _Survey(120.0, [("Bunn Street", 74.0), ("Murray Street", 120.0)])
    assert survey._street_survey_intersection_context_street(
        "Murray Street") == "Bunn Street"


def test_x_keeps_postal_road_when_its_geometry_is_nearby():
    survey = _Survey(12.0, [("Bunn Street", 8.0), ("Murray Street", 12.0)])
    assert survey._street_survey_intersection_context_street(
        "Murray Street") == "Murray Street"
