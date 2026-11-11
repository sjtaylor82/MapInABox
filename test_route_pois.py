import math
import unittest

from route_pois import describe_pois_along, pois_along, street_key

LAT0, LON0 = -26.6585, 153.0952


def at(east_m, north_m):
    """Point east_m east and north_m north of the origin."""
    return (LAT0 + north_m / 110540.0,
            LON0 + east_m / (111320.0 * math.cos(math.radians(LAT0))))


def poi(name, east_m, north_m, **extra):
    lat, lon = at(east_m, north_m)
    return dict(name=name, label=f"{name}, somewhere", lat=lat, lon=lon, **extra)


# Walking north along First Avenue for 200 m: west (negative east) is left.
STRAIGHT = [at(0, 0), at(0, 100), at(0, 200)]
STREETS = ["First Avenue", "First Avenue"]


class PassByTests(unittest.TestCase):
    def test_order_and_sides_on_a_straight_street(self):
        pois = [
            poi("Battery World", 15, 120),
            poi("KFC", -20, 30),
            poi("Busy Health", 12, 80),
            poi("Moon and Pearl", -18, 170),
        ]
        self.assertEqual(
            describe_pois_along(STRAIGHT, STREETS, pois),
            "On your left, you'll pass KFC, and Moon and Pearl.  On your right, "
            "Busy Health and Battery World.")

    def test_places_beyond_either_end_are_left_out(self):
        pois = [poi("Behind", 10, -30), poi("Past the turn", 10, 240),
                poi("At the turn", 10, 198)]
        self.assertEqual(pois_along(STRAIGHT, STREETS, pois), [])

    def test_place_recorded_on_another_street_is_left_out(self):
        pois = [poi("Side street cafe", 20, 100, street="George Street"),
                poi("Fronting shop", 20, 60, street="First Ave")]
        self.assertEqual([name for _a, _s, name in pois_along(STRAIGHT, STREETS, pois)],
                         ["Fronting shop"])

    def test_unknown_street_needs_to_be_close(self):
        pois = [poi("Close", 25, 60), poi("Further", 35, 60, street="")]
        self.assertEqual([name for _a, _s, name in pois_along(STRAIGHT, STREETS, pois)],
                         ["Close"])

    def test_place_on_the_centre_line_is_left_out(self):
        self.assertEqual(pois_along(STRAIGHT, STREETS, [poi("Middle", 2, 60)]), [])

    def test_place_too_far_from_the_street_is_left_out(self):
        self.assertEqual(
            pois_along(STRAIGHT, STREETS, [poi("Far", 55, 60, street="First Avenue")]), [])

    def test_bend_exclusion(self):
        # North 100 m, then east 100 m: a place near the corner is ambiguous.
        bend = [at(0, 0), at(0, 100), at(100, 100)]
        pois = [poi("Corner shop", -10, 95), poi("Before corner", -10, 50),
                poi("After corner", 50, 110)]
        result = pois_along(bend, STREETS, pois)
        self.assertEqual([(side, name) for _a, side, name in result],
                         [("left", "Before corner"), ("left", "After corner")])

    def test_house_number_parity_overrules_doubtful_side(self):
        # Odd numbers are on the right (east) along this step.
        addresses = [
            {"number": str(n), "street": "First Avenue",
             "lat": at(12, 20 * k)[0], "lon": at(12, 20 * k)[1]}
            for k, n in enumerate((1, 3, 5, 7, 9), start=1)
        ] + [
            {"number": str(n), "street": "First Avenue",
             "lat": at(-12, 20 * k)[0], "lon": at(-12, 20 * k)[1]}
            for k, n in enumerate((2, 4, 6, 8, 10), start=1)
        ]
        pois = [poi("Odd but west", -10, 60, number="11", street="First Avenue"),
                poi("Odd and east", 10, 70, number="13", street="First Avenue")]
        result = pois_along(STRAIGHT, STREETS, pois, addresses)
        self.assertEqual([(side, name) for _a, side, name in result],
                         [("right", "Odd and east")])

    def test_at_most_five_and_no_repeats(self):
        pois = [poi(f"Shop {k}", 10, 10 + 20 * k) for k in range(8)]
        pois.append(poi("Shop 1", -10, 185))   # same name again, later
        text = describe_pois_along(STRAIGHT, STREETS, pois)
        self.assertEqual(text, "On your right, you'll pass Shop 0, Shop 1, Shop 2, "
                               "Shop 3, Shop 4 and Shop 5.")

    def test_eateries_are_always_named_with_cuisine(self):
        pois = [poi(f"Office {k}", 10, 10 + 15 * k) for k in range(6)]
        thai = poi("Samila Gaeng", 10, 150)
        thai.update(kind="restaurant", cuisine="Thai")
        cafe = poi("Nourish Espresso & Catering", 10, 165)
        cafe["kind"] = "generic"
        text = describe_pois_along(STRAIGHT, STREETS, pois + [thai, cafe])
        self.assertEqual(
            text, "On your right, you'll pass Office 0, Office 1, Office 2, Office 3, "
                  "the Thai restaurant Samila Gaeng and Nourish Espresso & Catering.")

    def test_building_group_still_names_its_eateries(self):
        tenants = []
        for name, kind in (("Samila Gaeng", "restaurant"), ("Utopia Hair", "hairdresser"),
                           ("Your Property Co", "services"), ("Keeping in Mind", "clinic")):
            tenant = poi(name, 10, 100)
            tenant.update(kind=kind, number="23")
            tenants.append(tenant)
        tenants[0]["tags"] = {"cuisine": "thai;asian"}
        text = describe_pois_along(STRAIGHT, STREETS, tenants)
        self.assertEqual(
            text, "On your right, you'll pass 23 First Avenue, with the Thai "
                  "restaurant Samila Gaeng, Keeping in Mind, Utopia Hair and Your "
                  "Property Co.")

    def test_fast_food_brand_is_not_padded(self):
        kfc = poi("KFC", -10, 100)
        kfc["kind"] = "fast food"
        self.assertEqual(describe_pois_along(STRAIGHT, STREETS, [kfc]),
                         "On your left, you'll pass KFC.")

    def test_eateries_come_first_and_the_rest_are_counted(self):
        pois = [poi(f"Office {k}", 10, 10 + 10 * k) for k in range(4)]
        pois += [poi(f"Spot {k}", 10, 60 + 10 * k, kind="restaurant") for k in range(8)]
        text = describe_pois_along(STRAIGHT, STREETS, pois)
        self.assertEqual(
            text, "On your right, you'll pass Spot 0, Spot 1, "
                  "Spot 2, Spot 3, Spot 4 and Spot 5.  2 more places to eat "
                  "along here.")

    def test_same_place_from_two_sources_is_said_once(self):
        pois = [poi("The Kent", -12, 100, kind="pub"),
                poi("The Kent Hotel", -12, 104),
                poi("Crust", -12, 140, kind="fast food"),
                poi("Crust Pizza", -12, 142)]
        self.assertEqual(describe_pois_along(STRAIGHT, STREETS, pois),
                         "On your left, you'll pass The Kent Hotel and Crust Pizza.")

    def test_corner_stop_is_not_a_place(self):
        stop = poi("Donald St at Beaumont St", 12, 100)
        self.assertEqual(describe_pois_along(STRAIGHT, STREETS, [stop]), "")
        tram = poi("Burwood Rd/Glenferrie Rd-Stop 73", 12, 100)
        self.assertEqual(describe_pois_along(STRAIGHT, STREETS, [tram]), "")

    def test_neighbouring_cafes_share_their_type(self):
        cafes = [poi("Roladoor", 12, 80, kind="cafe"), poi("Suspension", 12, 120, kind="cafe")]
        self.assertEqual(describe_pois_along(STRAIGHT, STREETS, cafes),
                         "On your right, you'll pass the cafés Roladoor and Suspension.")

    def test_house_numbers_on_this_street_are_given(self):
        dosa = poi("Dosa Cafe", 12, 60, kind="cafe", number="35", street="First Ave")
        corner = poi("Corner Store", 12, 120, number="2", street="George Street")
        plain = poi("Nevermind Bar", 12, 150, kind="bar")
        self.assertEqual(
            describe_pois_along(STRAIGHT, STREETS, [dosa, corner, plain]),
            "On your right, you'll pass Dosa Cafe (35) and Nevermind Bar.")

    def test_building_tenant_leads_its_group(self):
        # 112 Beaumont Street, Hamilton: OSM has the hotel, HERE its tenants.
        hotel = poi("Northern Star Hotel", -12, 100, kind="pub")
        bar = poi("Finn McCools Eatery", -12, 101, kind="bar", number="112")
        cellar = poi("Super Cellars", -12, 102, kind="bottle shop", number="112")
        self.assertEqual(
            describe_pois_along(STRAIGHT, STREETS, [hotel, bar, cellar]),
            "On your left, you'll pass the Northern Star Hotel, with Finn McCools "
            "Eatery and Super Cellars.")

    def test_plain_restaurants_need_no_prefix(self):
        depot = poi("Depot", 12, 80, kind="restaurant")
        broaster = poi("Broaster Chicken", 12, 120, kind="restaurant")
        sushi = poi("Sushi Revolution Hamilton", 12, 150, kind="restaurant")
        self.assertEqual(
            describe_pois_along(STRAIGHT, STREETS, [depot, broaster, sushi]),
            "On your right, you'll pass Depot, Broaster Chicken and Sushi "
            "Revolution Hamilton.")

    def test_nothing_confident_says_nothing(self):
        self.assertEqual(describe_pois_along(STRAIGHT, STREETS, [poi("Middle", 1, 60)]), "")

    def test_street_names_compare_loosely(self):
        self.assertEqual(street_key("First Ave"), street_key("first avenue"))
        self.assertNotEqual(street_key("First Avenue"), street_key("George Street"))


class NavStepTests(unittest.TestCase):
    """The nav mixin uses the step's node path, else falls back to Nearby."""

    def _nav(self, route, pois):
        import nav

        mixin = next(cls for cls in vars(nav).values()
                     if isinstance(cls, type) and hasattr(cls, "_nav_step_poi_text"))
        obj = mixin.__new__(mixin)
        nodes = {1: at(0, 0), 2: at(0, 100), 3: at(0, 200), 4: at(60, 200)}
        edges = {1: [(2, "First Avenue")], 2: [(3, "First Avenue")],
                 3: [(4, "George Street")]}
        obj._walk_graph = {"nodes": nodes, "edges": edges}
        obj._nav_route = route
        # entry[0] is the turn point's index in the route's node path.
        obj._nav_instructions = [(1, 100.0, "Continue across X.") + at(0, 100),
                                 (2, 100.0, "Turn right onto George Street.") + at(0, 200)]
        obj.settings = {"walk_announce_pois": True, "walk_poi_category": "all"}
        obj._address_points = []
        obj._poi_grid_nearby = lambda lat, lon, radius: pois
        obj._nearby_walk_poi_text = lambda lat, lon, prefix="Nearby": "Nearby: fallback."
        obj._nav_valid_coord = lambda lat, lon: True
        return obj

    def test_osm_step_uses_its_own_stretch_of_path(self):
        pois = [poi("First stretch", -10, 50), poi("Second stretch", 10, 150)]
        obj = self._nav([1, 2, 3, 4], pois)
        self.assertEqual(obj._nav_step_poi_text(1, obj._nav_instructions[1]),
                         "On your right, you'll pass Second stretch.")
        self.assertEqual(obj._nav_step_poi_text(0, obj._nav_instructions[0]),
                         "On your left, you'll pass First stretch.")

    def test_routes_without_a_path_keep_nearby(self):
        obj = self._nav([], [])
        self.assertEqual(obj._nav_step_poi_text(1, obj._nav_instructions[1]),
                         "Nearby: fallback.")


if __name__ == "__main__":
    unittest.main()
