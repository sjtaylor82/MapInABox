"""braille.py — contracted (UEB grade 2) braille through liblouis.

Map in a Box bundles liblouis (vendor/liblouis: the library and the nine UEB
tables grade 2 needs) and calls it through ctypes.  Without it, for example
on a Mac with no liblouis installed, ``contracted`` returns None and callers
fall back to MIAB's own uncontracted translation.

Cells come back as ints with bit n-1 = dot n, the same as
dotpad.text_to_braille.  Capital signs are never written: the text is
lowercased first, since every cell counts on a 20- or 32-cell line.
"""

import ctypes
import ctypes.util
import os
import sys
import threading
import unicodedata

from app_paths import RESOURCE_DIR

TABLE = "en-ueb-g2.ctb"
VENDOR_DIR = os.path.join(RESOURCE_DIR, "vendor", "liblouis")

# liblouis translation modes (liblouis.h)
_DOTS_IO = 4
_UC_BRL = 64

_lock = threading.Lock()
_lib = None
_table = None
_tried = False


def _load():
    """Load liblouis once.  Returns (library, table list) or (None, None)."""
    global _lib, _table, _tried
    with _lock:
        if _tried:
            return _lib, _table
        _tried = True
        candidates = []
        bundled = os.path.join(VENDOR_DIR, "liblouis.dll")
        if sys.platform == "win32" and os.path.isfile(bundled):
            candidates.append((bundled, os.path.join(VENDOR_DIR, "tables", TABLE)))
        system = ctypes.util.find_library("louis") or ctypes.util.find_library("liblouis")
        if system:
            candidates.append((system, TABLE))   # an installed liblouis knows its tables
        for path, table in candidates:
            try:
                lib = ctypes.CDLL(path)
                lib.lou_charSize.restype = ctypes.c_int
                lib.lou_getTable.restype = ctypes.c_void_p
                lib.lou_getTable.argtypes = [ctypes.c_char_p]
                if not lib.lou_getTable(table.encode("utf-8")):
                    continue
            except Exception:
                continue
            _lib, _table = lib, table
            break
        return _lib, _table


def available():
    return _load()[0] is not None


def contracted(text):
    """UEB grade 2 cells for ``text``, or None when liblouis is unavailable."""
    lib, table = _load()
    if lib is None:
        return None
    # Accents cost a cell each in UEB; place names read fine without them.
    text = "".join(c for c in unicodedata.normalize("NFKD", str(text).lower())
                   if not unicodedata.combining(c))
    if not text:
        return []
    char_size = lib.lou_charSize()
    char_type = ctypes.c_uint32 if char_size == 4 else ctypes.c_uint16
    if char_size == 2 and any(ord(c) > 0xFFFF for c in text):
        text = "".join(c for c in text if ord(c) <= 0xFFFF)
    in_len = ctypes.c_int(len(text))
    inbuf = (char_type * len(text))(*[ord(c) for c in text])
    capacity = len(text) * 4 + 16
    outbuf = (char_type * capacity)()
    out_len = ctypes.c_int(capacity)
    with _lock:   # liblouis keeps translation state in globals
        ok = lib.lou_translateString(
            table.encode("utf-8"), inbuf, ctypes.byref(in_len),
            outbuf, ctypes.byref(out_len), None, None, _DOTS_IO | _UC_BRL)
    if not ok:
        return None
    cells = []
    for code in outbuf[:out_len.value]:
        if 0x2800 <= code <= 0x28FF:
            cells.append(code - 0x2800)
        elif code == 0x20:
            cells.append(0)
        else:
            cells.append(code & 0xFF)   # dotsIO without ucBrl: 0x8000 | dots
    return cells
