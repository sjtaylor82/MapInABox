"""monarch.py — HumanWare/APH Monarch tactile display output for Map in a Box.

In Braille Terminal mode the Monarch connects to the computer as a standard
HID braille display (usage page 0x41), over USB or Bluetooth.  Besides the
usual braille cell reports it has one output report that sets every one of
its 96 x 40 pins, which is what MIAB draws with:

    report 0x21, usage 0x301, 3840 one-bit fields: 480 bytes of pins.
    The panel is packed in 2 x 4 blocks, 48 blocks across and 10 down,
    one byte per block in row-major order, bits least significant first:
        bit 0 (0,0)  bit 3 (1,0)
        bit 1 (0,1)  bit 4 (1,1)
        bit 2 (0,2)  bit 5 (1,2)
        bit 6 (0,3)  bit 7 (1,3)      (x, y) within the block

That layout comes from a published NVDA driver (BrlMultiline), not from a
HumanWare specification, so MIAB only writes to a device whose report
descriptor declares exactly that report.  Other braille displays on the HID
braille page are opened, examined and closed untouched.

Like the Dot Pad, the Monarch belongs to MIAB while it is in use: the screen
reader must not be using it as its braille display.  MIAB uses the top 35 pin
rows for the map and the bottom line of 32 braille cells for its own text.
"""

from dataclasses import dataclass

import numpy as np

try:
    import hid
except ImportError:  # pragma: no cover - hidapi is a runtime dependency
    hid = None


BRAILLE_USAGE_PAGE = 0x41
PIN_REPORT_ID = 0x21
PIN_WIDTH, PIN_HEIGHT = 96, 40
PIN_BYTES = PIN_WIDTH * PIN_HEIGHT // 8       # 480
BLOCK_W, BLOCK_H = 2, 4

# Text line: 32 cells, three pins apart, on the bottom four pin rows, with a
# blank row between it and the map.
TEXT_COLS = 32
CELL_PITCH = 3
TEXT_TOP = PIN_HEIGHT - 4                     # 36
GRAPHIC_HEIGHT = TEXT_TOP - 1                 # 35

# Bit for each (x, y) within a 2 x 4 block; also braille dot n at bit n-1.
_BLOCK_BIT = np.zeros((BLOCK_H, BLOCK_W), dtype=np.uint8)
for _bit, (_x, _y) in enumerate([(0, 0), (0, 1), (0, 2), (1, 0),
                                 (1, 1), (1, 2), (0, 3), (1, 3)]):
    _BLOCK_BIT[_y, _x] = _bit


@dataclass
class MonarchInfo:
    width: int = PIN_WIDTH
    height: int = GRAPHIC_HEIGHT
    text_rows: int = 1
    text_cols: int = TEXT_COLS
    has_graphic: bool = True


def output_report_bits(descriptor):
    """Total output bits per report ID on the braille usage page.

    A small HID report-descriptor walk: enough to see which output reports
    exist and how large they are, without interpreting their usages.
    """
    data = bytes(descriptor)
    bits = {}
    usage_page = report_size = report_count = report_id = 0
    stack = []
    i = 0
    while i < len(data):
        prefix = data[i]
        if prefix == 0xFE:                     # long item: skip it
            if i + 1 >= len(data):
                break
            i += 3 + data[i + 1]
            continue
        size = (0, 1, 2, 4)[prefix & 3]
        kind, tag = (prefix >> 2) & 3, prefix >> 4
        value = int.from_bytes(data[i + 1:i + 1 + size], "little")
        i += 1 + size
        if kind == 1:                          # global items
            if tag == 0x0:
                usage_page = value
            elif tag == 0x7:
                report_size = value
            elif tag == 0x8:
                report_id = value
            elif tag == 0x9:
                report_count = value
            elif tag == 0xA:
                stack.append((usage_page, report_size, report_count, report_id))
            elif tag == 0xB and stack:
                usage_page, report_size, report_count, report_id = stack.pop()
        elif kind == 0 and tag == 0x9 and usage_page == BRAILLE_USAGE_PAGE:
            bits[report_id] = bits.get(report_id, 0) + report_size * report_count
    return bits


def has_pin_report(descriptor):
    return output_report_bits(descriptor).get(PIN_REPORT_ID) == PIN_WIDTH * PIN_HEIGHT


def pack_pins(pins):
    """Pack a (40, 96) bool array into the 480-byte payload of report 0x21."""
    grid = np.zeros((PIN_HEIGHT, PIN_WIDTH), dtype=bool)
    h = min(PIN_HEIGHT, pins.shape[0])
    w = min(PIN_WIDTH, pins.shape[1])
    grid[:h, :w] = pins[:h, :w]
    blocks = grid.reshape(PIN_HEIGHT // BLOCK_H, BLOCK_H, PIN_WIDTH // BLOCK_W, BLOCK_W)
    weights = (1 << _BLOCK_BIT.astype(np.uint16))            # (4, 2)
    packed = (blocks * weights[None, :, None, :]).sum(axis=(1, 3))
    return packed.astype(np.uint8).tobytes()


def draw_text(pins, cells):
    """Raise braille ``cells`` (bit n-1 = dot n) along the bottom line."""
    for index, cell in enumerate(list(cells)[:TEXT_COLS]):
        x0 = index * CELL_PITCH
        for y in range(BLOCK_H):
            for x in range(BLOCK_W):
                if cell & (1 << int(_BLOCK_BIT[y, x])):
                    pins[TEXT_TOP + y, x0 + x] = True
    return pins


def candidates():
    """HID braille collections that might be a Monarch."""
    if hid is None:
        return []
    found = []
    try:
        devices = hid.enumerate()
    except Exception:
        return []
    for info in devices:
        if info.get("usage_page") == BRAILLE_USAGE_PAGE:
            found.append(info)
    return found


def looks_like_monarch(info):
    text = f"{info.get('product_string') or ''} {info.get('manufacturer_string') or ''}"
    return "monarch" in text.lower()


class MonarchBusy(RuntimeError):
    """The Monarch is open in another program, usually the screen reader."""


class NotMonarch(RuntimeError):
    """An HID braille display without the Monarch's pin report."""


class MonarchDevice:
    """One open Monarch, written to as a single 96 x 40 frame."""

    label = "Monarch"

    def __init__(self, hid_info, on_keys=None, log_settings=None):
        self.hid_info = hid_info
        self.port = hid_info.get("path", b"")
        self.on_keys = on_keys
        self._dev = None
        self._closed = False
        self._graphic = np.zeros((GRAPHIC_HEIGHT, PIN_WIDTH), dtype=bool)
        self._text = []
        self._last = None
        self.name = hid_info.get("product_string") or "Monarch"
        self.info = MonarchInfo()

    def open(self):
        if hid is None:
            raise RuntimeError("hidapi is not installed")
        dev = hid.device()
        try:
            dev.open_path(self.hid_info["path"])
        except Exception as exc:
            if looks_like_monarch(self.hid_info):
                raise MonarchBusy(f"could not open the Monarch: {exc}") from exc
            raise
        try:
            descriptor = dev.get_report_descriptor()
        except Exception as exc:
            dev.close()
            raise NotMonarch(f"no report descriptor: {exc}") from exc
        if not has_pin_report(descriptor):
            dev.close()
            raise NotMonarch("no 3840-pin output report")
        self._dev = dev
        return self

    def close(self):
        self._closed = True
        try:
            if self._dev is not None:
                self._dev.close()
        except Exception:
            pass

    @property
    def is_open(self):
        return not self._closed and self._dev is not None

    def show(self, cells, pixels):
        self._text = list(cells)
        self._graphic[:] = False
        h = min(GRAPHIC_HEIGHT, pixels.shape[0])
        w = min(PIN_WIDTH, pixels.shape[1])
        self._graphic[:h, :w] = pixels[:h, :w]
        self._send()

    def show_text(self, cells):
        self._text = list(cells)
        self._send()

    def show_graphic(self, pixels):
        self.show(self._text, pixels)

    def _send(self):
        pins = np.zeros((PIN_HEIGHT, PIN_WIDTH), dtype=bool)
        pins[:GRAPHIC_HEIGHT] = self._graphic
        payload = pack_pins(draw_text(pins, self._text))
        if payload == self._last:
            return
        try:
            written = self._dev.write(bytes([PIN_REPORT_ID]) + payload)
        except Exception:
            self._closed = True
            raise
        if written is not None and written < 0:
            self._closed = True
            raise OSError("Monarch write failed")
        self._last = payload
