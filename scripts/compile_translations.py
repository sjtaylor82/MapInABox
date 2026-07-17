"""Compile locale/*/LC_MESSAGES/mapinabox.po files to .mo files."""

from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCALE_DIR = os.path.join(ROOT, "locale")


def main() -> int:
    cmd = [
        "pybabel",
        "compile",
        "--directory",
        LOCALE_DIR,
        "--domain",
        "mapinabox",
    ]
    try:
        subprocess.run(cmd, cwd=ROOT, check=True)
    except FileNotFoundError:
        print("pybabel is not installed. Install Babel with: python -m pip install Babel", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
