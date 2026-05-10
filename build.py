"""
build.py — Map in a Box release build script

Usage:
    python build.py          — compress resources + PyInstaller only
    python build.py install  — also run Inno Setup to produce the installer
    python build.py mac      — build a macOS .app bundle (run on macOS)

Workflow:
    1. python build.py
    2. Test dist\MapInABox\MapInABox.exe
    3. python build.py install
    4. Ship installer\MapInABox-<version>-setup.exe
"""

import gzip
import os
import re
import shutil
import subprocess
import sys

HERE      = os.path.dirname(os.path.abspath(__file__))
ARGS = {arg.lower() for arg in sys.argv[1:]}
DO_INSTALL = "install" in ARGS
DO_MAC_APP = sys.platform == "darwin" or "mac" in ARGS or "app" in ARGS


def step(n, msg):
    print(f"\n[{n}] {msg}")


def fail(msg):
    print(f"\nBUILD FAILED: {msg}")
    sys.exit(1)


# ── Read version from core.py (single source of truth) ───────────────────────
core_src = open(os.path.join(HERE, "core.py"), encoding="utf-8").read()
m = re.search(r"APP_VERSION\s*=\s*['\"]([^'\"]+)['\"]", core_src)
if not m:
    fail("Could not find APP_VERSION in core.py")
VERSION = m.group(1)
print(f"Version: {VERSION}")

# ── Sync version into MapInABox.iss ──────────────────────────────────────────
iss_path = os.path.join(HERE, "MapInABox.iss")
if not DO_MAC_APP:
    iss = open(iss_path, encoding="utf-8").read()
    iss = re.sub(r'(AppVersion=).*',                       rf'\g<1>{VERSION}', iss)
    iss = re.sub(r'(OutputBaseFilename=MapInABox-)[\d.]+', rf'\g<1>{VERSION}', iss)
    open(iss_path, "w", encoding="utf-8").write(iss)
    print(f"Updated MapInABox.iss → version {VERSION}")


# ── Step 1: Compress bundled resources ───────────────────────────────────────
step(1, "Compressing resources")

RESOURCES = [
    "worldcities.csv",
    "airports.csv",
    "countries.geojson",
]

for name in RESOURCES:
    src = os.path.join(HERE, name)
    dst = os.path.join(HERE, name + ".gz")

    if not os.path.exists(src):
        if os.path.exists(dst):
            print(f"  OK    {name}.gz  (source removed, gz kept)")
        else:
            print(f"  WARN  {name}  — neither source nor .gz found, skipping")
        continue

    if os.path.exists(dst) and os.path.getmtime(dst) >= os.path.getmtime(src):
        print(f"  OK    {name}.gz  (up to date)")
        continue

    with open(src, "rb") as f_in, gzip.open(dst, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)

    size_mb = os.path.getsize(dst) / 1_048_576
    print(f"  WROTE {name}.gz  ({size_mb:.1f} MB)")


# ── Step 2: PyInstaller ───────────────────────────────────────────────────────
step(2, "Running PyInstaller")

result = subprocess.run(
    [sys.executable, "-m", "PyInstaller",
     os.path.join(HERE, "MapInABox.spec"), "--noconfirm"],
    cwd=HERE,
)
if result.returncode != 0:
    fail("PyInstaller exited with errors (see above)")

if DO_MAC_APP:
    app_path = os.path.join(HERE, "dist", "MapInABox.app")
    print(f"\nMac app ready — test it before packaging:")
    print(f"  {app_path}")
else:
    dist_exe = os.path.join(HERE, "dist", "MapInABox", "MapInABox.exe")
    print(f"\nExe ready — test it before building the installer:")
    print(f"  {dist_exe}")

    if not DO_INSTALL:
        print("\nRun  python build.py install  once you're happy with the exe.")
        sys.exit(0)

    # ── Step 3: Inno Setup ────────────────────────────────────────────────────
    step(3, "Building installer")

    ISCC_CANDIDATES = [
        r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        r"C:\Program Files\Inno Setup 6\ISCC.exe",
    ]
    iscc = next((p for p in ISCC_CANDIDATES if os.path.exists(p)), None)

    if iscc is None:
        fail("Inno Setup not found. Install from https://jrsoftware.org/isinfo.php")

    result = subprocess.run([iscc, iss_path], cwd=HERE)
    if result.returncode != 0:
        fail("Inno Setup exited with errors (see above)")

    installer = next(
        (os.path.join(HERE, "installer", f)
         for f in os.listdir(os.path.join(HERE, "installer"))
         if f.endswith(".exe")),
        None,
    )
    print("\nBuild complete.")
    if installer:
        print(f"  Installer: {installer}")
