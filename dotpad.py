"""dotpad.py — Dot Pad tactile display output for Map in a Box.

MIAB drives the Dot Pad itself over USB.  The screen reader must not be using
it as a braille display: the 60 x 40 graphics area shows MIAB's tactile images
(the F8 country shape) and the 20-cell braille line shows MIAB's own text.

Wire protocol (Dot Inc. DotPad serial protocol, 115200 baud, 8N1):
    packet   = AA 55 | body length (2 bytes, big-endian) | body
    body     = destination | command (2 bytes, big-endian) | sequence | data | checksum
    checksum = XOR of every preceding body byte, starting from 0xA5
Display lines are addressed by destination: the braille line(s) first, then
one destination per row of graphics cells.  A line's data is a start-cell
offset followed by one byte per cell.
"""

import math
import queue
import struct
import threading
import time
import unicodedata
from dataclasses import dataclass
from functools import reduce
from operator import xor

import numpy as np

from logging_utils import miab_log

try:
    import serial
    from serial.tools import list_ports
except ImportError:  # pragma: no cover - pyserial is a runtime dependency
    serial = None
    list_ports = None


# ── Protocol constants ───────────────────────────────────────────────────────

SYNC = b"\xAA\x55"
CHECKSUM_BASE = 0xA5
MIN_PACKET = 4 + 5      # sync + length, then dest/cmd/seq/checksum
MAX_PACKET = 512
BAUD_RATE = 115200

REQ_DEVICE_NAME = 0x0100
RSP_DEVICE_NAME = 0x0101
REQ_BOARD_INFORMATION = 0x0110
RSP_BOARD_INFORMATION = 0x0111
REQ_DISPLAY_LINE = 0x0200
RSP_DISPLAY_LINE = 0x0201
NTF_DISPLAY_LINE = 0x0202
NTF_KEYS_PERKINS = 0x0312
NTF_KEYS_FUNCTION = 0x0332
NTF_ERROR = 0x9902

SEQ_TEXT = 0x80

FEATURE_GRAPHIC = 0x80
FEATURE_TEXT = 0x40

DISPLAY_ACK, DISPLAY_NACK, DISPLAY_WAIT, DISPLAY_CHECKSUM = 0, 1, 2, 3

KEY_GROUP_PERKINS = 0x12
KEY_GROUP_FUNCTION = 0x32

# USB serial bridges used by Dot Pads (FTDI dual RS232 in the DotPad 320A).
USB_IDS = {(0x0403, 0x6010)}

# Key numbers within the Perkins group, in the device's reversed bit order.
_PERKINS_KEY_NAMES = {
    0: "dot7", 1: "dot3", 2: "dot2", 3: "dot1", 4: "dot4", 5: "dot5",
    6: "dot6", 7: "dot8", 8: "space", 9: "shift_left", 10: "control_left",
    11: "shift_right", 12: "control_right", 13: "pan_left", 14: "pan_right",
    16: "nav_center", 17: "nav_up", 18: "nav_right", 19: "nav_down",
    20: "nav_left",
}

# Graphics cells are 2 dots wide and 4 high.  Dot (x, y) within a cell sets
# bit y + 4 * x, i.e. the left column is bits 0-3 top to bottom.
CELL_W, CELL_H = 2, 4
DEFAULT_GRAPHIC_COLS, DEFAULT_GRAPHIC_ROWS = 30, 10
DEFAULT_TEXT_COLS = 20


def build_packet(dest, cmd, data=b"", seq=0):
    body = bytearray([dest & 0xFF, (cmd >> 8) & 0xFF, cmd & 0xFF, seq & 0xFF])
    body += bytes(data)
    body.append(reduce(xor, body, CHECKSUM_BASE))
    return SYNC + struct.pack(">H", len(body)) + bytes(body)


class PacketParser:
    """Reassemble packets from a byte stream, resyncing on garbage."""

    def __init__(self):
        self._buf = bytearray()

    def feed(self, data):
        self._buf += data
        packets = []
        while len(self._buf) >= 4:
            if self._buf[:2] != SYNC:
                del self._buf[0]
                continue
            total = 4 + struct.unpack(">H", bytes(self._buf[2:4]))[0]
            if not MIN_PACKET <= total <= MAX_PACKET:
                del self._buf[0]
                continue
            if len(self._buf) < total:
                break
            body = bytes(self._buf[4:total])
            del self._buf[:total]
            if reduce(xor, body[:-1], CHECKSUM_BASE) != body[-1]:
                continue
            dest, cmd, seq = body[0], (body[1] << 8) | body[2], body[3]
            packets.append((dest, cmd, seq, body[4:-1]))
        return packets


@dataclass
class BoardInfo:
    features: int = FEATURE_GRAPHIC
    text_rows: int = 0
    text_cols: int = 0
    graphic_rows: int = DEFAULT_GRAPHIC_ROWS
    graphic_cols: int = DEFAULT_GRAPHIC_COLS

    @classmethod
    def parse(cls, data):
        data = bytes(data) + bytes(12)
        info = cls(features=data[0],
                   text_rows=data[4], text_cols=data[5],
                   graphic_rows=data[8] or DEFAULT_GRAPHIC_ROWS,
                   graphic_cols=data[9] or DEFAULT_GRAPHIC_COLS)
        if not info.features & FEATURE_TEXT:
            info.text_rows = info.text_cols = 0
        return info

    @property
    def has_graphic(self):
        return bool(self.features & FEATURE_GRAPHIC)

    @property
    def width(self):
        return self.graphic_cols * CELL_W

    @property
    def height(self):
        return self.graphic_rows * CELL_H


def pixels_to_rows(pixels, cell_cols, cell_rows):
    """Pack a (height, width) bool array into one bytes object per cell row."""
    grid = np.zeros((cell_rows * CELL_H, cell_cols * CELL_W), dtype=bool)
    h = min(grid.shape[0], pixels.shape[0])
    w = min(grid.shape[1], pixels.shape[1])
    grid[:h, :w] = pixels[:h, :w]
    rows = []
    for row in range(cell_rows):
        block = grid[row * CELL_H:(row + 1) * CELL_H]
        cells = np.zeros(cell_cols, dtype=np.uint8)
        for x in range(CELL_W):
            for y in range(CELL_H):
                cells |= block[y, x::CELL_W].astype(np.uint8) << (y + CELL_H * x)
        rows.append(bytes(cells))
    return rows


# ── Rendering ────────────────────────────────────────────────────────────────

class ShapeFit:
    """Maps lon/lat onto display dots so that a set of rings fills the display.

    The shape keeps its proportions and is centred, leaving ``margin`` dots
    clear at each edge.  Longitudes are unwrapped around ``centre_lon`` so
    countries crossing the 180th meridian stay in one piece.
    """

    def __init__(self, rings, width, height, centre_lon=None, margin=1):
        self.width, self.height, self.margin = width, height, margin
        self.centre_lon = centre_lon
        self.rings = [r for r in (self.unwrap(ring) for ring in rings) if len(r)]
        self.ok = bool(self.rings)
        if not self.ok:
            return
        all_pts = np.concatenate(self.rings)
        lat_mid = (all_pts[:, 1].min() + all_pts[:, 1].max()) / 2
        self.x_scale = max(0.2, math.cos(math.radians(lat_mid)))
        xy = self._xy(all_pts)
        self.min_x, self.min_y = xy.min(axis=0)
        geo_w = max(1e-9, xy[:, 0].max() - self.min_x)
        geo_h = max(1e-9, xy[:, 1].max() - self.min_y)
        avail_w, avail_h = width - 2 * margin, height - 2 * margin
        self.scale = min(avail_w / geo_w, avail_h / geo_h)
        self.off_x = margin + (avail_w - geo_w * self.scale) / 2
        self.off_y = margin + (avail_h - geo_h * self.scale) / 2

    def unwrap(self, points):
        pts = np.asarray(points, dtype=float).reshape(-1, 2).copy()
        if self.centre_lon is not None and len(pts):
            pts[:, 0] = (pts[:, 0] - self.centre_lon + 180) % 360 - 180 + self.centre_lon
        return pts

    def _xy(self, pts):
        return np.column_stack((pts[:, 0] * self.x_scale, -pts[:, 1]))

    def to_px(self, pts):
        xy = self._xy(pts)
        return np.column_stack(((xy[:, 0] - self.min_x) * self.scale + self.off_x,
                                (xy[:, 1] - self.min_y) * self.scale + self.off_y))


def render_rings(rings, width, height, style="filled", centre_lon=None, margin=1):
    """Rasterise geographic rings [(lon, lat), ...] to a (height, width) bool grid."""
    return rasterise(ShapeFit(rings, width, height, centre_lon, margin), style)


def rasterise(fit, style="filled"):
    """Draw ``fit``'s rings as a (height, width) bool grid.

    Rings too small to cover a dot still get one raised dot, so islands are
    not silently lost.  ``style`` is "filled" or "outline".
    """
    width, height, margin = fit.width, fit.height, fit.margin
    grid = np.zeros((height, width), dtype=bool)
    if not fit.ok:
        return grid
    px_rings = [fit.to_px(r) for r in fit.rings]

    # Even-odd scanline fill, sampling each dot at its centre.
    edges = [(r, np.roll(r, -1, axis=0)) for r in px_rings if len(r) >= 3]
    if edges:
        x0 = np.concatenate([a[:, 0] for a, _ in edges])
        y0 = np.concatenate([a[:, 1] for a, _ in edges])
        x1 = np.concatenate([b[:, 0] for _, b in edges])
        y1 = np.concatenate([b[:, 1] for _, b in edges])
        for row in range(height):
            yc = row + 0.5
            crossing = (y0 <= yc) != (y1 <= yc)
            if not crossing.any():
                continue
            cx0, cy0 = x0[crossing], y0[crossing]
            xs = np.sort(cx0 + (yc - cy0) * (x1[crossing] - cx0) / (y1[crossing] - cy0))
            for a, b in zip(xs[0::2], xs[1::2]):
                start = max(0, math.ceil(a - 0.5))
                end = min(width - 1, math.floor(b - 0.5))
                if end >= start:
                    grid[row, start:end + 1] = True

    # Trace every coastline too, so peninsulas and strips thinner than a dot
    # still appear, and give otherwise-invisible islands a single dot.
    for ring in px_rings:
        seg_start = ring
        seg_end = np.roll(ring, -1, axis=0) if len(ring) > 1 else ring
        delta = seg_end - seg_start
        counts = np.ceil(np.hypot(delta[:, 0], delta[:, 1]) / 0.5).astype(int) + 1
        idx = np.repeat(np.arange(len(ring)), counts)
        step = np.arange(counts.sum()) - np.repeat(np.cumsum(counts) - counts, counts)
        t = step / np.repeat(np.maximum(counts - 1, 1), counts)
        pts = seg_start[idx] + delta[idx] * t[:, None]
        # Points exactly on the far edge would otherwise floor into the margin.
        cols = np.clip(np.floor(pts[:, 0]).astype(int), margin, width - 1 - margin)
        rows = np.clip(np.floor(pts[:, 1]).astype(int), margin, height - 1 - margin)
        grid[rows, cols] = True

    if style == "outline":
        padded = np.pad(grid, 1, constant_values=False)
        interior = (padded[:-2, 1:-1] & padded[2:, 1:-1]
                    & padded[1:-1, :-2] & padded[1:-1, 2:])
        grid = grid & ~interior
    return grid


def add_marker(grid, fit, lon, lat):
    """Return a copy of ``grid`` with a 2 x 2 "you are here" marker at lon/lat.

    The marker inverts the pins beneath it: a gap in a filled shape, a raised
    block on open space or inside an outline.  Off-display positions are left
    unmarked.
    """
    if not fit.ok:
        return grid
    x, y = fit.to_px(fit.unwrap([(lon, lat)]))[0]
    col, row = math.floor(x), math.floor(y)
    if not (0 <= col < fit.width and 0 <= row < fit.height):
        return grid
    col, row = min(col, fit.width - 2), min(row, fit.height - 2)
    marked = grid.copy()
    marked[row:row + 2, col:col + 2] ^= True
    return marked


# ── Braille text (Unified English Braille, uncontracted) ─────────────────────

def _dots(*numbers):
    return sum(1 << (n - 1) for n in numbers)


_UEB_LETTERS = {
    "a": _dots(1), "b": _dots(1, 2), "c": _dots(1, 4), "d": _dots(1, 4, 5),
    "e": _dots(1, 5), "f": _dots(1, 2, 4), "g": _dots(1, 2, 4, 5),
    "h": _dots(1, 2, 5), "i": _dots(2, 4), "j": _dots(2, 4, 5),
    "k": _dots(1, 3), "l": _dots(1, 2, 3), "m": _dots(1, 3, 4),
    "n": _dots(1, 3, 4, 5), "o": _dots(1, 3, 5), "p": _dots(1, 2, 3, 4),
    "q": _dots(1, 2, 3, 4, 5), "r": _dots(1, 2, 3, 5), "s": _dots(2, 3, 4),
    "t": _dots(2, 3, 4, 5), "u": _dots(1, 3, 6), "v": _dots(1, 2, 3, 6),
    "w": _dots(2, 4, 5, 6), "x": _dots(1, 3, 4, 6), "y": _dots(1, 3, 4, 5, 6),
    "z": _dots(1, 3, 5, 6),
}
_UEB_DIGITS = {d: _UEB_LETTERS["jabcdefghi"[int(d)]] for d in "0123456789"}
_UEB_PUNCT = {
    " ": [0], ",": [_dots(2)], ".": [_dots(2, 5, 6)], "'": [_dots(3)],
    "-": [_dots(3, 6)], ":": [_dots(2, 5)], ";": [_dots(2, 3)],
    "!": [_dots(2, 3, 5)], "?": [_dots(2, 3, 6)],
    "(": [_dots(5), _dots(1, 2, 6)], ")": [_dots(5), _dots(3, 4, 5)],
    "&": [_dots(4), _dots(1, 2, 3, 4, 6)], "/": [_dots(4, 5, 6), _dots(3, 4)],
}
_CAPITAL = _dots(6)
_NUMBER = _dots(3, 4, 5, 6)
_GRADE1 = _dots(5, 6)


def text_to_braille(text):
    """Translate text to uncontracted UEB cells (bit n-1 = dot n)."""
    text = "".join(c for c in unicodedata.normalize("NFKD", str(text))
                   if not unicodedata.combining(c))
    cells = []
    in_number = False
    i = 0
    while i < len(text):
        ch = text[i]
        if ch.isdigit() and ch in _UEB_DIGITS:
            if not in_number:
                cells.append(_NUMBER)
                in_number = True
            cells.append(_UEB_DIGITS[ch])
            i += 1
            continue
        if (in_number and ch in ",." and i + 1 < len(text)
                and text[i + 1].isdigit()):
            cells.append(_UEB_PUNCT[ch][0])
            i += 1
            continue
        if ch.lower() in _UEB_LETTERS:
            end = i
            while end < len(text) and text[end].lower() in _UEB_LETTERS:
                end += 1
            word = text[i:end]
            if in_number and word[0].lower() in "abcdefghij":
                cells.append(_GRADE1)
            if len(word) > 1 and word.isupper():
                cells += [_CAPITAL, _CAPITAL]
                cells += [_UEB_LETTERS[c.lower()] for c in word]
            else:
                for c in word:
                    if c.isupper():
                        cells.append(_CAPITAL)
                    cells.append(_UEB_LETTERS[c.lower()])
            in_number = False
            i = end
            continue
        in_number = False
        cells += _UEB_PUNCT.get(ch, [])
        i += 1
    return cells


# ── Device ───────────────────────────────────────────────────────────────────

class DotPadDevice:
    """One open Dot Pad on a serial port."""

    RESPONSE_TIMEOUT = 2.0

    def __init__(self, port, on_keys=None, log_settings=None):
        self.port = port
        self.on_keys = on_keys
        self._log_settings = log_settings
        self._ser = None
        self._parser = PacketParser()
        self._responses = {}
        self._cond = threading.Condition()
        self._write_lock = threading.Lock()
        self._reader = None
        self._closed = False
        self._line_cache = {}
        self._keys_down = set()
        self._groups_held = set()
        self.name = ""
        self.info = BoardInfo()

    def open(self):
        if serial is None:
            raise RuntimeError("pyserial is not installed")
        self._ser = serial.Serial(self.port, BAUD_RATE, timeout=0.1, write_timeout=1)
        self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                        name="DotPadReader")
        self._reader.start()
        try:
            self.name = self.request(REQ_DEVICE_NAME, RSP_DEVICE_NAME).decode(
                "ascii", "replace").strip("\x00 ")
            self.info = BoardInfo.parse(
                self.request(REQ_BOARD_INFORMATION, RSP_BOARD_INFORMATION))
            if not self.info.has_graphic:
                raise RuntimeError("Device has no graphics area")
        except Exception:
            self.close()
            raise
        return self

    def close(self):
        self._closed = True
        try:
            if self._ser is not None:
                self._ser.close()
        except Exception:
            pass

    @property
    def is_open(self):
        return not self._closed and self._ser is not None and self._ser.is_open

    def request(self, cmd, rsp_cmd, data=b"", dest=0, seq=0, timeout=None):
        packet = build_packet(dest, cmd, data, seq)
        with self._cond:
            self._responses.pop((dest, rsp_cmd), None)
        with self._write_lock:
            self._ser.write(packet)
        deadline = time.monotonic() + (timeout or self.RESPONSE_TIMEOUT)
        with self._cond:
            while (dest, rsp_cmd) not in self._responses:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self._closed:
                    raise TimeoutError(f"No response to command 0x{cmd:04X}")
                self._cond.wait(remaining)
            return self._responses.pop((dest, rsp_cmd))

    def _read_loop(self):
        while not self._closed:
            try:
                data = self._ser.read(self._ser.in_waiting or 1)
            except Exception:
                self._closed = True
                with self._cond:
                    self._cond.notify_all()
                return
            if not data:
                continue
            for dest, cmd, seq, payload in self._parser.feed(data):
                self._handle_packet(dest, cmd, payload)

    def _handle_packet(self, dest, cmd, payload):
        if cmd in (NTF_KEYS_PERKINS, NTF_KEYS_FUNCTION):
            self._handle_keys(cmd & 0xFF, payload)
        elif cmd == NTF_ERROR:
            miab_log("errors", f"Dot Pad reported error {payload.hex()}",
                     self._log_settings)
        elif (cmd & 0x0F) == 0x01:     # RSP_* replies end in 0x01 / 0x11
            with self._cond:
                self._responses[(dest, cmd)] = payload
                self._cond.notify_all()

    def _handle_keys(self, group, payload):
        """Collect a key combination and report it once every key is released."""
        if any(payload):
            for byte_index, byte in enumerate(payload):
                for bit in range(8):
                    if byte & (1 << (7 - bit)):
                        self._keys_down.add((group, byte_index * 8 + bit))
            self._groups_held.add(group)
            return
        self._groups_held.discard(group)
        if self._keys_down and not self._groups_held:
            keys = frozenset(key_name(g, n) for g, n in self._keys_down)
            self._keys_down.clear()
            if self.on_keys:
                self.on_keys(keys)

    def display_line(self, dest, cells, seq=0):
        data = b"\x00" + bytes(cells)
        if self._line_cache.get(dest) == data:
            return True
        for _attempt in range(5):
            reply = self.request(REQ_DISPLAY_LINE, RSP_DISPLAY_LINE, data, dest, seq)
            code = reply[0] if reply else DISPLAY_ACK
            if code == DISPLAY_ACK:
                self._line_cache[dest] = data
                return True
            if code == DISPLAY_NACK:
                return False
            time.sleep(0.2)   # WAIT (still refreshing) or checksum: try again
        return False

    def show_graphic(self, pixels):
        first = self.info.text_rows
        rows = pixels_to_rows(pixels, self.info.graphic_cols, self.info.graphic_rows)
        for index, row in enumerate(rows):
            self.display_line(first + index, row)

    def show_text(self, cells):
        cols = self.info.text_cols
        if not cols:
            return
        cells = (list(cells) + [0] * cols)[:cols]
        self.display_line(0, cells, seq=SEQ_TEXT)


def key_name(group, number):
    if group == KEY_GROUP_FUNCTION:
        return f"f{number + 1}"
    if group == KEY_GROUP_PERKINS:
        return _PERKINS_KEY_NAMES.get(number, f"key{number}")
    return f"group{group:02x}_key{number}"


def candidate_ports(preferred=""):
    if preferred:
        return [preferred]
    if list_ports is None:
        return []
    return [p.device for p in sorted(list_ports.comports(), key=lambda p: p.device)
            if (p.vid, p.pid) in USB_IDS]


# ── Controller ───────────────────────────────────────────────────────────────

class DotPadController:
    """Owns the Dot Pad on a worker thread and maps MIAB content onto it.

    ``announce`` is called (from the worker thread) with text to speak, and
    ``on_follow_changed`` with the new follow state when F2 toggles it.
    Keys: F1 toggles filled/outline, F2 toggles follow mode, F4 clears, and
    the pan keys scroll the braille line.

    In follow mode the shape tracks the country under the cursor and a
    "you are here" marker tracks the cursor itself.  A new country is drawn
    only once movement has settled, so skimming across borders does not keep
    the pins churning; marker moves are drawn straight away.
    """

    STYLES = ("filled", "outline")
    FOLLOW_SETTLE = 0.5     # seconds in a country before its shape is drawn
    RESCAN_INTERVAL = 5.0   # seconds between USB scans when no Dot Pad is found

    def __init__(self, settings_getter, announce=None, on_follow_changed=None):
        self._settings = settings_getter
        self._announce = announce or (lambda text: None)
        self._on_follow_changed = on_follow_changed or (lambda value: None)
        self._queue = queue.Queue()
        self._device = None
        self._last_scan = 0.0
        self._style = "filled"
        self._follow = bool(self._settings().get("dotpad_follow", True))
        self._title = ""
        self._shape = None
        self._base_key = None
        self._base = None
        self._position = None
        self._text_cells = []
        self._text_offset = 0
        self._pending_lock = threading.Lock()
        self._pending_shape = None
        self._pending_at = 0.0
        self._pending_position = None
        self._follow_job_queued = False
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="DotPadController")
        self._thread.start()

    # Public API — safe to call from the UI thread.

    @property
    def enabled(self):
        return serial is not None and bool(self._settings().get("dotpad_enabled", True))

    @property
    def following(self):
        return self.enabled and self._follow

    def connect(self):
        if self.enabled:
            self._queue.put(lambda: self._connect(force=True))

    def show_shape(self, title, rings, centre_lon=None, position=None):
        """F8: show a shape now, with the marker when following."""
        if not self.enabled:
            return
        with self._pending_lock:
            self._pending_shape = None   # an explicit request wins
        self._queue.put(lambda: self._show_shape(title, rings, centre_lon,
                                                 position, force=True))

    def follow(self, title, rings, centre_lon, position):
        """Follow mode: report the country (title None for none) and the cursor."""
        if not self.following:
            return
        with self._pending_lock:
            if title and title != self._title:
                if not self._pending_shape or self._pending_shape[0] != title:
                    self._pending_at = time.monotonic()
                self._pending_shape = (title, rings, centre_lon)
            elif title == self._title:
                self._pending_shape = None   # wandered back before it settled
            self._pending_position = position
            if self._follow_job_queued:
                return
            self._follow_job_queued = True
        self._queue.put(self._follow_job)

    def apply_settings(self):
        follow = bool(self._settings().get("dotpad_follow", True))
        if self.enabled:
            self.connect()
            self._queue.put(lambda: self._set_follow(follow, announce=False))
        else:
            self._queue.put(self._disconnect)

    def shutdown(self, timeout=2.0):
        done = threading.Event()
        self._queue.put(lambda: (self._disconnect(clear=True), done.set()))
        self._queue.put(None)
        done.wait(timeout)

    # Worker-thread internals.

    def _run(self):
        while True:
            job = self._queue.get()
            if job is None:
                return
            try:
                job()
            except Exception as exc:
                miab_log("errors", f"Dot Pad: {exc!r}", self._settings())
                if self._device is not None and not self._device.is_open:
                    self._device = None
                    self._announce("Dot Pad disconnected.")

    def _connect(self, force=False):
        if self._device is not None and self._device.is_open:
            return self._device
        self._device = None
        now = time.monotonic()
        if not force and now - self._last_scan < self.RESCAN_INTERVAL:
            return None
        self._last_scan = now
        for port in candidate_ports(self._settings().get("dotpad_port", "")):
            device = DotPadDevice(port, self._on_keys, self._settings())
            try:
                device.open()
            except Exception as exc:
                miab_log("errors", f"Dot Pad probe on {port} failed: {exc!r}",
                         self._settings())
                continue
            self._device = device
            miab_log("feature_usage",
                     f"Dot Pad connected: {device.name} on {port}, {device.info}",
                     self._settings())
            self._announce("Dot Pad connected.")
            return device
        return None

    def _disconnect(self, clear=False):
        device, self._device = self._device, None
        if device is None:
            return
        if clear and device.is_open:
            try:
                self._clear(device)
            except Exception:
                pass
        device.close()

    def _show_shape(self, title, rings, centre_lon, position=None, force=False):
        self._title = title
        self._shape = (rings, centre_lon)
        self._base_key = None
        self._text_cells = text_to_braille(title)
        self._text_offset = 0
        if position is not None:
            self._position = position
        device = self._connect(force=force)
        if device is not None:
            self._draw(device)

    def _follow_job(self):
        while True:
            with self._pending_lock:
                shape = self._pending_shape
                wait = self._pending_at + self.FOLLOW_SETTLE - time.monotonic()
                if shape is None or wait <= 0:
                    self._pending_shape = None
                    position = self._pending_position
                    self._follow_job_queued = False
                    break
            time.sleep(min(wait, 0.1))   # a newer country may still arrive
        if not self._follow:
            return
        self._position = position
        if shape is not None:
            self._show_shape(*shape)
            return
        device = self._connect()
        if device is not None and self._shape is not None:
            self._draw(device)

    def _draw(self, device):
        if self._shape is None:
            return
        info = device.info
        key = (self._title, self._style, info.width, info.height)
        if self._base_key != key:
            rings, centre_lon = self._shape
            fit = ShapeFit(rings, info.width, info.height, centre_lon)
            self._base = (fit, rasterise(fit, self._style))
            self._base_key = key
        fit, pixels = self._base
        if self._follow and self._position is not None:
            pixels = add_marker(pixels, fit, *self._position)
        self._show_text_window(device)
        device.show_graphic(pixels)

    def _show_text_window(self, device):
        device.show_text(self._text_cells[self._text_offset:])

    def _clear(self, device):
        device.show_text([])
        device.show_graphic(np.zeros((device.info.height, device.info.width), bool))

    def _set_follow(self, value, announce=True):
        if value == self._follow:
            return
        self._follow = value
        if announce:
            self._announce("Follow on." if value else "Follow off.")
        device = self._device
        if device is not None and device.is_open:
            self._draw(device)   # add or remove the marker

    def _on_keys(self, keys):
        # Called on the reader thread; hand off to the worker.
        self._queue.put(lambda: self._handle_keys(keys))

    def _handle_keys(self, keys):
        device = self._device
        if device is None:
            return
        miab_log("feature_usage", f"Dot Pad keys: {'+'.join(sorted(keys))}",
                 self._settings())
        width = device.info.text_cols or DEFAULT_TEXT_COLS
        if keys == {"pan_right"}:
            if self._text_offset + width < len(self._text_cells):
                self._text_offset += width
                self._show_text_window(device)
        elif keys == {"pan_left"}:
            if self._text_offset:
                self._text_offset = max(0, self._text_offset - width)
                self._show_text_window(device)
        elif keys == {"f1"}:
            index = self.STYLES.index(self._style)
            self._style = self.STYLES[(index + 1) % len(self.STYLES)]
            self._announce(f"{self._style.capitalize()}.")
            self._draw(device)
        elif keys == {"f2"}:
            self._set_follow(not self._follow)
            self._on_follow_changed(self._follow)
        elif keys == {"f4"}:
            self._title = ""
            self._shape = None
            self._base_key = None
            self._text_cells = []
            self._text_offset = 0
            self._clear(device)
            self._announce("Dot Pad cleared.")
        else:
            self._announce(f"Dot Pad {' plus '.join(sorted(keys))} not assigned.")
