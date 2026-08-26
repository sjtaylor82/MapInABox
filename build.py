"""
build.py — Map in a Box release build script

Usage:
    python build.py                    — compress resources + PyInstaller only
    python build.py install            — also run Inno Setup to produce the installer
    python build.py mac                — build a macOS .app bundle (run on macOS)
    python build.py mac education      — build the macOS Education app
    python build.py install education  — build the Education edition installer
                                          (Tools menu withheld; favourites/personal
                                          POIs cleared on exit by default)

Workflow:
    1. python build.py
    2. Test dist\MapInABox\MapInABox.exe
    3. python build.py install [education]
    4. Ship installer\MapInABox-<version>-setup.exe
       or installer\MapInABox-Education-<version>-setup.exe
"""

import gzip
import os
import re
import shutil
import subprocess
import sys
import tempfile

from package_manifest import write_manifest

HERE      = os.path.dirname(os.path.abspath(__file__))
ARGS = {arg.lower() for arg in sys.argv[1:]}
DO_INSTALL = "install" in ARGS
DO_MAC_APP = sys.platform == "darwin" or "mac" in ARGS or "app" in ARGS
EDITION = "education" if "education" in ARGS else "pro"


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

# ── Sync version + edition into MapInABox.iss ────────────────────────────────
iss_path = os.path.join(HERE, "MapInABox.iss")
if not DO_MAC_APP:
    iss = open(iss_path, encoding="utf-8").read()
    if EDITION == "education":
        app_name = "Map in a Box Education"
        output_base = f"MapInABox-Education-{VERSION}-setup"
    else:
        app_name = "Map in a Box"
        output_base = f"MapInABox-{VERSION}-setup"
    iss = re.sub(r'(#define AppName\s+")[^"]+(")', rf'\g<1>{app_name}\2', iss)
    iss = re.sub(r'(#define AppVersion\s+")[^"]+(")', rf'\g<1>{VERSION}\2', iss)
    iss = re.sub(r'(AppVersion=).*',                       rf'\g<1>{VERSION}', iss)
    iss = re.sub(r'(OutputBaseFilename=).*',               rf'\g<1>{output_base}', iss)
    iss = iss.replace('Create a &desktop shortcut', 'Create a desktop shortcut')
    iss = iss.replace('Open the &Manual', 'Open the Manual')
    manual_run = (
        '; Offer to open the manual after install\n'
        'Filename: "{app}\\_internal\\manual.html"; \\\n'
        '    Description: "Open the Manual"; \\\n'
        '    Flags: postinstall shellexec skipifsilent\n'
    )
    if 'Open the Manual' not in iss and 'Open the &Manual' not in iss:
        launch_block = (
            '[Run]\n'
            '; Offer to launch after install\n'
            'Filename: "{app}\\{#AppExe}"; \\\n'
            '    Description: "Launch {#AppName}"; \\\n'
            '    Flags: nowait postinstall skipifsilent\n'
        )
        if launch_block in iss:
            iss = iss.replace(launch_block, launch_block + manual_run)
        elif '[Run]\n' in iss:
            iss = iss.replace('[Run]\n', '[Run]\n' + manual_run, 1)
    open(iss_path, "w", encoding="utf-8").write(iss)
    print(f"Updated MapInABox.iss -> version {VERSION}")

# ── Sync version into manual.html ────────────────────────────────────────────
manual_path = os.path.join(HERE, "manual.html")
manual = open(manual_path, encoding="utf-8").read()
manual = re.sub(
    r'(<p><strong>Version\s+)[^<]+(</strong>\s*&nbsp;\|&nbsp; Windows &amp; macOS</p>)',
    rf'\g<1>{VERSION}\2',
    manual,
)
open(manual_path, "w", encoding="utf-8").write(manual)
print(f"Updated manual.html -> version {VERSION}")


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

PYI_WORK = os.path.join(tempfile.gettempdir(), "MapInABox-pyinstaller")

build_env = os.environ.copy()
build_env["MIAB_EDITION"] = EDITION
build_env["MIAB_VERSION"] = VERSION

result = subprocess.run(
    [sys.executable, "-m", "PyInstaller",
     os.path.join(HERE, "MapInABox.spec"), "--noconfirm", "--clean",
     "--workpath", PYI_WORK],
    cwd=HERE,
    env=build_env,
)
if result.returncode != 0:
    fail("PyInstaller exited with errors (see above)")

# Keep legal notices visible at the top level of distributable packages.
if not DO_MAC_APP:
    dist_root = os.path.join(HERE, "dist", "MapInABox")
    for notice_name in ("LICENSE", "THIRD_PARTY_NOTICES.txt", "TRADEMARKS.md"):
        shutil.copy2(os.path.join(HERE, notice_name),
                     os.path.join(dist_root, notice_name))

    # A portable update treats the bundled sound tree as release-managed data.
    # Refuse to produce either distribution if PyInstaller silently omits it.
    source_sound_root = os.path.join(HERE, "sounds")
    bundled_sound_root = os.path.join(dist_root, "_internal", "sounds")
    source_sounds = {
        os.path.relpath(os.path.join(root, name), source_sound_root)
        for root, _dirs, names in os.walk(source_sound_root)
        for name in names
    }
    bundled_sounds = {
        os.path.relpath(os.path.join(root, name), bundled_sound_root)
        for root, _dirs, names in os.walk(bundled_sound_root)
        for name in names
    }
    if not source_sounds or "credits.txt" not in source_sounds:
        fail("Source sound library is empty or missing credits.txt")
    missing_sounds = source_sounds - bundled_sounds
    if missing_sounds:
        preview = ", ".join(sorted(missing_sounds)[:5])
        fail(f"PyInstaller omitted {len(missing_sounds)} sound files: {preview}")
    print(f"Verified bundled sound library: {len(bundled_sounds)} files")

    marker = os.path.join(dist_root, "_internal", "_education")
    if (EDITION == "education") != os.path.isfile(marker):
        fail(f"{EDITION.title()} build has an incorrect Education marker")
    print(f"Verified {EDITION.title()} edition marker state")

    manifest_path = write_manifest(dist_root, VERSION, EDITION)
    print(f"WROTE {manifest_path}  (portable update manifest)")

if DO_MAC_APP:
    app_name = ("MapInABox-Education.app"
                if EDITION == "education" else "MapInABox.app")
    app_path = os.path.join(HERE, "dist", app_name)
    markers = [
        os.path.join(root, name)
        for root, _dirs, names in os.walk(app_path)
        for name in names
        if name == "_education"
    ]
    if (EDITION == "education") != bool(markers):
        fail(f"{EDITION.title()} macOS bundle has an incorrect Education marker")
    print(f"Verified {EDITION.title()} edition marker state")
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
