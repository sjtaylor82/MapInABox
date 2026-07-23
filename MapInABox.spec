# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for Map in a Box
#
# Build command (run from C:\miab):
#   pyinstaller MapInABox.spec
#
# Output: dist\MapInABox\MapInABox.exe  (plus supporting files)
# Feed that folder to Inno Setup to produce the installer.

import os
import sys
from PyInstaller.utils.hooks import collect_all, collect_submodules

if sys.platform == "darwin":
    from PyInstaller.building.osx import BUNDLE

# ── Collect packages whose internals PyInstaller can't fully auto-detect ─────

shapely_d,   shapely_b,   shapely_h   = collect_all('shapely')
ao2_d,       ao2_b,       ao2_h       = collect_all('accessible_output2')
pygame_d,    pygame_b,    pygame_h    = collect_all('pygame')
certifi_d,   certifi_b,   certifi_h   = collect_all('certifi')

all_datas    = shapely_d  + ao2_d + pygame_d + certifi_d
all_binaries = shapely_b  + ao2_b + pygame_b + certifi_b
all_hidden   = shapely_h  + ao2_h + pygame_h + certifi_h

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
        ('currency_data.json',   '.'),
        ('languages_data.json',  '.'),
        ('gtfs_overrides.json',  '.'),
        ('manual.html',          '.'),
        ('LICENSE',              '.'),
        ('THIRD_PARTY_NOTICES.txt', '.'),
        ('TRADEMARKS.md',        '.'),
        ('portable_updater.ps1',    '.'),
        ('locale',               'locale'),
        ('sounds',               'sounds'),
        ('GeoFeatures',          'GeoFeatures'),
        ('PostalCodes',          'PostalCodes'),
    ],
    hiddenimports=all_hidden + [
        # pandas internals that aren't always picked up
        'pandas._libs.tslibs.np_datetime',
        'pandas._libs.tslibs.nattype',
        'pandas._libs.tslibs.timedeltas',
        'pandas._libs.tslibs.timestamps',
        # wx
        'wx._xml',
        'wx.lib.agw',
        # tzfpy (Rust extension) — make sure the compiled module is found
        'tzfpy',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # ── Not used directly; nothing in the app imports it, only pulled
        # in as an optional pandas backend that we don't need ───────────
        'pyarrow',
        # ── Replaced by tzfpy (much smaller Rust-backed data) ────────────
        'timezonefinder',
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
        'pytest',
        # ── Network / proxy tools ─────────────────────────────────────────
        'mitmproxy', 'aioquic', 'h2', 'hpack', 'hyperframe',
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
