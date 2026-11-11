import unittest

from tools import _short_audiograph_name
from dialogs import JourneyAudioGraphPlaybackDialog
from sound_engine import SoundEngine


class JourneyAudioGraphNameTests(unittest.TestCase):
    def test_google_australian_address_uses_suburb(self):
        self.assertEqual(
            _short_audiograph_name(
                "309 King St, Newcastle West NSW 2302, Australia"),
            "Newcastle West",
        )

    def test_nominatim_address_uses_first_locality_after_street(self):
        self.assertEqual(
            _short_audiograph_name(
                "NEX, 309, King Street, Newcastle West, Newcastle, "
                "City of Newcastle, New South Wales, 2302, Australia"),
            "Newcastle West",
        )

    def test_city_result_stays_short(self):
        self.assertEqual(
            _short_audiograph_name("Brisbane, Queensland, Australia"),
            "Brisbane",
        )

    def test_audio_bounds_fit_regional_locations(self):
        bounds = JourneyAudioGraphPlaybackDialog._location_bounds([
            (-32.9283, 151.7817, "Newcastle"),
            (-33.8688, 151.2093, "Sydney"),
        ])
        min_lat, max_lat, min_lon, max_lon = bounds
        self.assertLess(min_lat, -33.8688)
        self.assertGreater(max_lat, -32.9283)
        self.assertLess(min_lon, 151.2093)
        self.assertGreater(max_lon, 151.7817)
        # It is a regional viewport, not the old full-world viewport.
        self.assertLess(max_lat - min_lat, 2.0)
        self.assertLess(max_lon - min_lon, 2.0)

    def test_audio_bounds_unwrap_date_line(self):
        bounds = JourneyAudioGraphPlaybackDialog._location_bounds([
            (-17.7, 178.4, "Fiji"),
            (-13.8, -171.8, "Samoa"),
        ])
        self.assertGreater(bounds[2], 170.0)
        self.assertGreater(bounds[3], 180.0)
        self.assertLess(bounds[3] - bounds[2], 20.0)

    def test_long_leg_starts_a_new_audio_region(self):
        locations = [
            (-33.87, 151.21, "Sydney"),
            (-33.80, 151.08, "Chatswood"),
            (-33.73, 151.08, "Hornsby"),
            (-32.93, 151.78, "Newcastle"),
            (-32.79, 151.84, "Newcastle Airport"),
        ]
        bounds = JourneyAudioGraphPlaybackDialog._cluster_location_bounds(
            locations)
        self.assertEqual(bounds[0], bounds[1])
        self.assertEqual(bounds[1], bounds[2])
        self.assertNotEqual(bounds[2], bounds[3])
        self.assertEqual(bounds[3], bounds[4])

    def test_ordinary_legs_keep_one_audio_region(self):
        locations = [
            (-33.87, 151.21, "Sydney"),
            (-33.80, 151.08, "Chatswood"),
            (-33.73, 151.08, "Hornsby"),
        ]
        bounds = JourneyAudioGraphPlaybackDialog._cluster_location_bounds(
            locations)
        self.assertTrue(all(item == bounds[0] for item in bounds))

    def test_bridge_location_is_merged_into_following_region(self):
        locations = [
            (-32.928271, 151.781680, "Newcastle"),
            (-32.731553, 151.552509, "Maitland"),
            (-32.399856, 151.757947, "Dungog"),
            (-31.434904, 152.900182, "Port Macquarie"),
            (-30.296201, 153.113892, "Coffs Harbour"),
            (-30.642374, 153.002789, "Nambucca Heads"),
        ]
        bounds = JourneyAudioGraphPlaybackDialog._cluster_location_bounds(
            locations)
        self.assertEqual(bounds[0], bounds[1])
        self.assertEqual(bounds[1], bounds[2])
        self.assertNotEqual(bounds[2], bounds[3])
        self.assertEqual(bounds[3], bounds[4])
        self.assertEqual(bounds[4], bounds[5])

    def test_first_spoken_location_has_no_distance(self):
        locations = [
            (-33.8688, 151.2093, "Sydney"),
            (-32.9283, 151.7817, "Newcastle"),
        ]
        self.assertEqual(
            JourneyAudioGraphPlaybackDialog._spoken_location(locations, 0),
            "Sydney",
        )

    def test_later_location_describes_previous_leg(self):
        locations = [
            (-33.8688, 151.2093, "Sydney"),
            (-32.9283, 151.7817, "Newcastle"),
        ]
        spoken = JourneyAudioGraphPlaybackDialog._spoken_location(locations, 1)
        self.assertTrue(spoken.startswith("Newcastle, "))
        self.assertIn("kilometres north east", spoken)

    def test_logged_parameters_match_spatial_tone_normalisation(self):
        bounds = (-34.0, -32.0, 150.0, 152.0)
        lat, lon, frequency, pan = SoundEngine.spatial_tone_parameters(
            -33.0, 151.0, bounds)
        self.assertAlmostEqual(lat, 0.0)
        self.assertAlmostEqual(lon, 0.0)
        self.assertAlmostEqual(frequency, 440.0)
        self.assertAlmostEqual(pan, 0.0)

    def test_southern_audio_latitudes_do_not_collapse_to_one_pitch(self):
        frequencies = [
            SoundEngine.spatial_tone_parameters(lat, 0.0)[2]
            for lat in (-90.0, -75.0, -60.0, -45.0, 0.0)
        ]
        self.assertEqual(len(set(frequencies)), len(frequencies))
        self.assertEqual(frequencies[0], 220.0)
        self.assertEqual(frequencies[-1], 440.0)


if __name__ == "__main__":
    unittest.main()
