"""Shared application paths for installed, portable, and source builds."""

from __future__ import annotations

import os
import sys


RESOURCE_DIR = getattr(
    sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
APP_DIR = (os.path.dirname(os.path.abspath(sys.executable))
           if getattr(sys, "frozen", False) else RESOURCE_DIR)
_INTERNAL_PORTABLE_MARKER = os.path.join(RESOURCE_DIR, "_portable")
_LEGACY_PORTABLE_MARKER = os.path.join(APP_DIR, "portable.flag")
# New portable packages keep their marker with the bundled runtime files so it
# is not presented to users as an apparently disposable empty file.  Continue
# recognising the original root marker while existing copies migrate.
PORTABLE_MODE = (
    os.path.isfile(_INTERNAL_PORTABLE_MARKER)
    or os.path.isfile(_LEGACY_PORTABLE_MARKER)
)

# PyInstaller runs an edition-specific hook from inside the executable before
# importing application modules. Source runs default to Pro unless the explicit
# development-only Education switch is set.
# Unlike the old external ``_education`` marker, this value cannot be changed
# by deleting or renaming a file in the installation directory.
APPLICATION_EDITION = (
    getattr(sys, "_miab_embedded_edition", "education")
    if getattr(sys, "frozen", False)
    else (
        "education"
        if os.environ.get("MIAB_DEV_EDITION", "").strip().lower()
        == "education"
        else "pro"
    )
)
if APPLICATION_EDITION not in {"pro", "education"}:
    # An invalid frozen identity must not accidentally enable Pro features.
    APPLICATION_EDITION = "education"
EDUCATION_EDITION = APPLICATION_EDITION == "education"


def writable_dirs(
    portable: bool = PORTABLE_MODE,
    app_dir: str = APP_DIR,
    platform: str = sys.platform,
) -> tuple[str, str]:
    """Return the user-data and cache directories for the current build."""
    if portable:
        data_dir = os.path.join(app_dir, "Data")
        return data_dir, os.path.join(data_dir, "Cache")
    if platform == "darwin":
        home = os.path.expanduser("~")
        return (
            os.path.join(home, "Library", "Application Support", "MapInABox"),
            os.path.join(home, "Library", "Caches", "MapInABox"),
        )
    home = os.path.expanduser("~")
    return (
        os.path.join(os.environ.get("APPDATA", home), "MapInABox"),
        os.path.join(os.environ.get("LOCALAPPDATA", home), "MapInABox", "Cache"),
    )


USER_DIR, CACHE_DIR = writable_dirs()
