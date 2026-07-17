"""Generate locale/mapinabox.pot from translatable Python strings."""

from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POT_PATH = os.path.join(ROOT, "locale", "mapinabox.pot")

SOURCES = [
    "accessible_route.py",
    "airlines.py",
    "airport_directory.py",
    "aviationstack.py",
    "cache_utils.py",
    "city_packs.py",
    "core.py",
    "dialogs.py",
    "favourites.py",
    "free.py",
    "game.py",
    "geo.py",
    "here_poi.py",
    "lookups.py",
    "mall_directory.py",
    "mistral.py",
    "nav.py",
    "opensky.py",
    "overpass_client.py",
    "poi_fetch.py",
    "priceline.py",
    "route_tools.py",
    "sea_routes.py",
    "street_data.py",
    "streetview.py",
    "timetable.py",
    "tools.py",
    "transit_lookup.py",
    "tripadvisor.py",
    "updater.py",
    "walk.py",
    "wx_utils.py",
]


def main() -> int:
    os.makedirs(os.path.dirname(POT_PATH), exist_ok=True)
    files = [os.path.join(ROOT, name) for name in SOURCES if os.path.exists(os.path.join(ROOT, name))]
    cmd = [
        "pybabel",
        "extract",
        "--add-comments=Translators:",
        "--keyword=_",
        "--keyword=pgettext:1c,2",
        "--keyword=ngettext:1,2",
        "--keyword=npgettext:1c,2,3",
        "--project=Map in a Box",
        "--version=1.0",
        "--msgid-bugs-address=https://github.com/sjtaylor82/MapInABox/issues",
        "--copyright-holder=Sam Taylor",
        "--output",
        POT_PATH,
        *files,
    ]
    try:
        subprocess.run(cmd, cwd=ROOT, check=True)
    except FileNotFoundError:
        print("pybabel is not installed. Install Babel with: python -m pip install Babel", file=sys.stderr)
        return 1
    print(f"Wrote {POT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
