import os
import unittest

from postal_codes import PostalCodeLookup


POSTAL_CODES_DIR = os.path.join(os.path.dirname(__file__), "PostalCodes")


class PostalCodeSearchTests(unittest.TestCase):
    def test_finds_carina_for_offline_jump_fallback(self):
        lookup = PostalCodeLookup(POSTAL_CODES_DIR)

        results = lookup.search_places("Carina", "AU")

        self.assertIn(
            ("Carina", "Queensland", "4152", -27.4873, 153.0989),
            results,
        )
        self.assertEqual(results[0][0], "Carina")

    def test_exact_matches_precede_prefix_matches(self):
        lookup = PostalCodeLookup(POSTAL_CODES_DIR)

        names = [row[0] for row in lookup.search_places("Carina", "AU")]

        self.assertEqual(names[:3], ["Carina", "Carina", "Carina Heights"])

    def test_infers_australian_catalogue_from_brisbane_coordinates(self):
        lookup = PostalCodeLookup(POSTAL_CODES_DIR)

        country_code = lookup.country_code_near(-27.49, 153.06)

        self.assertEqual(country_code, "AU")

    def test_lookup_details_identifies_carina_at_its_postal_centre(self):
        lookup = PostalCodeLookup(POSTAL_CODES_DIR)

        result = lookup.lookup_details(-27.4873, 153.0989)

        self.assertEqual(result[:5], (
            "4152", "Carina", "Queensland", -27.4873, 153.0989))
        self.assertAlmostEqual(result[5], 0.0)


if __name__ == "__main__":
    unittest.main()


def test_military_mail_codes_are_not_localities():
    match = PostalCodeLookup(POSTAL_CODES_DIR).lookup_details(-6.215, 106.845)   # central Jakarta
    assert match is None or not match[1].upper().startswith(("APO", "FPO", "DPO"))
