import importlib
import os
import sys
import unittest
from unittest import mock

import app_paths


class EmbeddedEditionTests(unittest.TestCase):
    def tearDown(self):
        for name in ("_miab_embedded_edition", "frozen"):
            if hasattr(sys, name):
                delattr(sys, name)
        importlib.reload(app_paths)

    def test_packaging_variable_cannot_change_a_source_run(self):
        with mock.patch.dict(os.environ, {
                "MIAB_EDITION": "education", "MIAB_DEV_EDITION": ""}):
            sys._miab_embedded_edition = "education"
            self.assertEqual(importlib.reload(app_paths).APPLICATION_EDITION,
                             "pro")

    def test_source_can_explicitly_run_education_workflow(self):
        with mock.patch.dict(os.environ, {
                "MIAB_DEV_EDITION": "education"}):
            self.assertEqual(importlib.reload(app_paths).APPLICATION_EDITION,
                             "education")
            self.assertTrue(app_paths.EDUCATION_EDITION)

    def test_frozen_build_ignores_source_development_switch(self):
        with mock.patch.dict(os.environ, {
                "MIAB_DEV_EDITION": "education"}):
            sys.frozen = True
            sys._miab_embedded_edition = "pro"
            self.assertEqual(importlib.reload(app_paths).APPLICATION_EDITION,
                             "pro")

    def test_frozen_edition_comes_from_embedded_runtime_identity(self):
        sys.frozen = True
        sys._miab_embedded_edition = "education"
        self.assertEqual(importlib.reload(app_paths).APPLICATION_EDITION,
                         "education")
        self.assertTrue(app_paths.EDUCATION_EDITION)

    def test_frozen_build_without_identity_fails_closed(self):
        sys.frozen = True
        self.assertEqual(importlib.reload(app_paths).APPLICATION_EDITION,
                         "education")

    def test_invalid_frozen_identity_fails_closed(self):
        sys.frozen = True
        sys._miab_embedded_edition = "unexpected"
        self.assertEqual(importlib.reload(app_paths).APPLICATION_EDITION,
                         "education")


if __name__ == "__main__":
    unittest.main()
