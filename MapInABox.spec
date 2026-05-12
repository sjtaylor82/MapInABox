# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for Map in a Box
#
# Build command (run from C:\miab):
#   pyinstaller MapInABox.spec
#
# Output: dist\MapInABox\MapInABox.exe  (plus supporting files)
# Feed that folder to Inno Setup to produce the installer.

import glob as _glob
import os
import sys
from PyInstaller.utils.hooks import collect_all, collect_submodules

if sys.platform == "darwin":
    from PyInstaller.building.osx import BUNDLE

# ── Collect packages whose internals PyInstaller can't fully auto-detect ─────

shapely_d,   shapely_b,   shapely_h   = collect_all('shapely')

# h3 Cython extensions — collect_all misses some native modules on Windows
h3_d,        h3_b,        h3_h        = collect_all('h3')
if sys.platform == "win32":
    import h3 as _h3
    _h3_site = os.path.dirname(os.path.abspath(_h3.__file__))
    h3_b = h3_b + [(p, 'h3/_cy') for p in _glob.glob(os.path.join(_h3_site, "_cy", "*.pyd"))]
genai_d,     genai_b,     genai_h     = collect_all('google.genai')
apicore_d,   apicore_b,   apicore_h   = collect_all('google.api_core')
proto_d,     proto_b,     proto_h     = collect_all('proto')
ao2_d,       ao2_b,       ao2_h       = collect_all('accessible_output2')
pygame_d,    pygame_b,    pygame_h    = collect_all('pygame')

all_datas    = shapely_d   + genai_d   + apicore_d + proto_d + ao2_d + pygame_d + h3_d
all_binaries = shapely_b   + genai_b   + apicore_b + proto_b + ao2_b + pygame_b + h3_b
all_hidden   = shapely_h   + genai_h   + apicore_h + proto_h + ao2_h + pygame_h + h3_h

a = Analysis(
    ['core.py'],
    pathex=[os.getcwd()],
    binaries=all_binaries,
    datas=all_datas + [
        # ── Bundled read-only resources ───────────────────────────────────
        ('worldcities.csv.gz',   '.'),
        ('airports.csv.gz',      '.'),
        ('countries.geojson.gz', '.'),
        ('facts.json',           '.'),
        ('gtfs_overrides.json',  '.'),
        ('manual.html',          '.'),
        ('sounds',               'sounds'),
        ('GeoFeatures',          'GeoFeatures'),
    ],
    hiddenimports=all_hidden + [
        # pyarrow — needed for .feather read/write
        'pyarrow',
        'pyarrow.vendored',
        'pyarrow.vendored.version',
        # pandas internals that aren't always picked up
        'pandas._libs.tslibs.np_datetime',
        'pandas._libs.tslibs.nattype',
        'pandas._libs.tslibs.timedeltas',
        'pandas._libs.tslibs.timestamps',
        # h3 Cython extensions — collect_all misses these
        'h3._cy',
        'h3._cy.cells',
        'h3._cy.edges',
        'h3._cy.vertexes',
        'h3._cy.latlng',
        'h3._cy.inspection',
        'h3._cy.regions',
        # wx
        'wx._xml',
        'wx.lib.agw',
        # timezonefinder uses data files; make sure the module is found
        'timezonefinder',
        # pycountry uses data files bundled with the package
        'pycountry',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # ── Machine-learning stack (not used) ────────────────────────────
        'torch', 'torchvision', 'torchaudio',
        'transformers', 'huggingface_hub', 'tokenizers', 'safetensors',
        'sklearn', 'scikit_learn', 'scipy',
        'cv2', 'opencv', 'easyocr', 'pytesseract',
        'PIL.ImageFilter', 'PIL.ImageDraw', 'imageio',
        'tifffile', 'pywavelets',
        # ── Browser automation ────────────────────────────────────────────
        'selenium', 'playwright', 'undetected_chromedriver',
        'browser_cookie3',
        # ── Web frameworks ────────────────────────────────────────────────
        'flask', 'fastapi', 'starlette', 'uvicorn', 'django',
        'werkzeug', 'jinja2', 'itsdangerous', 'click',
        # ── gRPC / test deps (suppresses warnings about missing optional modules) ─
        'grpc', 'grpcio',
        'google.api_core.operations_v1',
        'google.api_core.operations_v1.lro_schedules',
        'google.genai.tests',
        'pytest',
        # ── Network / proxy tools ─────────────────────────────────────────
        'mitmproxy', 'aioquic', 'h2', 'h3', 'hpack', 'hyperframe',
        # ── Media tools ───────────────────────────────────────────────────
        'yt_dlp', 'streamlink', 'mutagen', 'spotipy',
        # ── Other unused ──────────────────────────────────────────────────
        'redis', 'IPython', 'jupyter', 'notebook', 'matplotlib',
        'fpdf2', 'pdf2docx', 'pdfminer', 'pdfplumber', 'pymupdf',
        'docx', 'python_docx',
        'psutil',
        'lz4',
        'torch',
    ],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='MapInABox',
    debug=False,
    bootloader_ignore_signals=False,
    strip=sys.platform == "darwin",   # Strip debug symbols on macOS to reduce size
    upx=False,           # UPX disabled — triggers AV false positives, bad for an accessibility app
    console=False,       # No console window; output goes to %APPDATA%\MapInABox\miab.log
    disable_windowed_traceback=False,
    icon='icon.ico' if os.path.exists('icon.ico') else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=sys.platform == "darwin",   # Strip debug symbols on macOS to reduce size
    upx=False,
    name='MapInABox',
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name='MapInABox.app',
        icon='icon.icns' if os.path.exists('icon.icns') else None,
        bundle_identifier='com.samtaylor.MapInABox',
    )
