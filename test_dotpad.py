import threading
import time
import unittest
from unittest import mock

import numpy as np

import dotpad
from dotpad import (
    BoardInfo, DotPadController, PacketParser, ShapeFit, add_marker,
    build_packet, pixels_to_rows, rasterise, render_rings, text_to_braille,
)


def _dots(*numbers):
    return sum(1 << (n - 1) for n in numbers)


class ProtocolTests(unittest.TestCase):
    def test_packet_layout_and_checksum(self):
        packet = build_packet(0, dotpad.REQ_DEVICE_NAME)
        self.assertEqual(packet[:4], b"\xAA\x55\x00\x05")
        body = packet[4:]
        self.assertEqual(body[:4], b"\x00\x01\x00\x00")
        self.assertEqual(body[4], 0xA5 ^ 0x00 ^ 0x01 ^ 0x00 ^ 0x00)

    def test_parser_resyncs_and_splits_packets(self):
        first = build_packet(2, dotpad.RSP_DISPLAY_LINE, b"\x00")
        second = build_packet(0, dotpad.RSP_DEVICE_NAME, b"DotPad320A")
        parser = PacketParser()
        packets = parser.feed(b"\x13\x37" + first + second[:5])
        self.assertEqual(packets, [(2, dotpad.RSP_DISPLAY_LINE, 0, b"\x00")])
        packets = parser.feed(second[5:])
        self.assertEqual(packets, [(0, dotpad.RSP_DEVICE_NAME, 0, b"DotPad320A")])

    def test_parser_drops_bad_checksum(self):
        packet = bytearray(build_packet(0, dotpad.RSP_DEVICE_NAME, b"X"))
        packet[-1] ^= 0xFF
        self.assertEqual(PacketParser().feed(bytes(packet)), [])

    def test_board_information(self):
        info = BoardInfo.parse(bytes([0xC0, 8, 25, 4, 1, 20, 0, 0, 10, 30, 0, 0]))
        self.assertEqual((info.text_rows, info.text_cols), (1, 20))
        self.assertEqual((info.width, info.height), (60, 40))

    def test_pixel_bit_order(self):
        pixels = np.zeros((40, 60), bool)
        pixels[0, 0] = True    # left column, top    -> bit 0
        pixels[3, 0] = True    # left column, bottom -> bit 3
        pixels[0, 1] = True    # right column, top   -> bit 4
        pixels[39, 59] = True  # last cell, right bottom -> bit 7
        rows = pixels_to_rows(pixels, 30, 10)
        self.assertEqual(len(rows), 10)
        self.assertEqual(rows[0][0], 0b00011001)
        self.assertEqual(rows[9][29], 0b10000000)


class RenderTests(unittest.TestCase):
    SQUARE = [[(0, 0), (10, 0), (10, 10), (0, 10)]]

    def test_square_fills_display_height_and_keeps_aspect(self):
        grid = render_rings(self.SQUARE, 60, 40)
        rows = np.where(grid.any(axis=1))[0]
        cols = np.where(grid.any(axis=0))[0]
        self.assertEqual((rows.min(), rows.max()), (1, 38))
        self.assertAlmostEqual(cols.max() - cols.min() + 1, 38, delta=1)
        self.assertTrue(grid[20, 30])

    def test_outline_hollows_interior(self):
        grid = render_rings(self.SQUARE, 60, 40, style="outline")
        self.assertFalse(grid[20, 30])
        self.assertTrue(grid[1, 30])

    def test_tiny_island_gets_a_dot(self):
        island = [(30, 30), (30.01, 30), (30.01, 30.01)]
        grid = render_rings(self.SQUARE + [island], 60, 40)
        self.assertGreater(grid.sum(), 0)
        far = render_rings([[(0, 0), (1, 0), (1, 1), (0, 1)], island], 60, 40)
        self.assertTrue(far[1:5, :].any())

    def test_dateline_country_is_unwrapped(self):
        east = [(178, -16), (180, -16), (180, -18), (178, -18)]
        west = [(-180, -16), (-178, -16), (-178, -18), (-180, -18)]
        grid = render_rings([east, west], 60, 40, centre_lon=179)
        cols = np.where(grid.any(axis=0))[0]
        self.assertLess(cols.max() - cols.min(), 60)
        self.assertGreater(grid.sum(), 1000)   # one solid block, not two slivers

    def test_every_country_renders(self):
        from world_map_panel import _GEO_COUNTRIES, _GEO_RINGS
        started = time.perf_counter()
        for country in _GEO_COUNTRIES:
            rings = [_GEO_RINGS[i] for i in country["rings_idx"]]
            grid = render_rings(rings, 60, 40, centre_lon=country["centroid_lon"])
            self.assertGreater(grid.sum(), 0, country["name"])
        self.assertLess(time.perf_counter() - started, 30)


class MarkerTests(unittest.TestCase):
    SQUARE = [[(0, 0), (10, 0), (10, 10), (0, 10)]]

    def test_marker_is_a_gap_in_a_filled_shape(self):
        fit = ShapeFit(self.SQUARE, 60, 40)
        grid = rasterise(fit)
        marked = add_marker(grid, fit, 5, 5)
        self.assertEqual(int((grid != marked).sum()), 4)
        self.assertFalse(marked[20, 30])
        self.assertTrue(grid[20, 30])

    def test_marker_is_raised_inside_an_outline(self):
        fit = ShapeFit(self.SQUARE, 60, 40)
        marked = add_marker(rasterise(fit, "outline"), fit, 5, 5)
        self.assertTrue(marked[20, 30])

    def test_marker_off_the_display_is_ignored(self):
        fit = ShapeFit(self.SQUARE, 60, 40)
        grid = rasterise(fit)
        self.assertIs(add_marker(grid, fit, 5, 80), grid)


class BrailleTests(unittest.TestCase):
    def test_capital_and_letters(self):
        self.assertEqual(text_to_braille("Peru"),
                         [_dots(6), _dots(1, 2, 3, 4), _dots(1, 5),
                          _dots(1, 2, 3, 5), _dots(1, 3, 6)])

    def test_capital_word_indicator(self):
        self.assertEqual(text_to_braille("USA")[:2], [_dots(6), _dots(6)])
        self.assertEqual(len(text_to_braille("USA")), 5)

    def test_numbers_and_accents(self):
        self.assertEqual(text_to_braille("10"),
                         [_dots(3, 4, 5, 6), _dots(1), _dots(2, 4, 5)])
        self.assertEqual(text_to_braille("Côte"), text_to_braille("Cote"))

    def test_grade1_indicator_after_number(self):
        self.assertEqual(text_to_braille("2a"),
                         [_dots(3, 4, 5, 6), _dots(1, 2), _dots(5, 6), _dots(1)])


class FakeDotPad:
    """Stands in for serial.Serial and answers like a DotPad 320A."""

    BOARD = bytes([0xC0 | 0x02, 8, 25, 4, 1, 20, 0, 0, 10, 30, 0, 0])

    def __init__(self, port, *args, **kwargs):
        self.port = port
        self.is_open = True
        self.lines = {}
        self._parser = PacketParser()
        self._out = bytearray()
        self._lock = threading.Condition()
        FakeDotPad.instance = self

    @property
    def in_waiting(self):
        return len(self._out)

    def write(self, data):
        for dest, cmd, seq, payload in self._parser.feed(data):
            if cmd == dotpad.REQ_DEVICE_NAME:
                self._reply(dest, dotpad.RSP_DEVICE_NAME, b"DotPad320A")
            elif cmd == dotpad.REQ_BOARD_INFORMATION:
                self._reply(dest, dotpad.RSP_BOARD_INFORMATION, self.BOARD)
            elif cmd == dotpad.REQ_DISPLAY_LINE:
                self.lines[dest] = (seq, payload)
                self._reply(dest, dotpad.RSP_DISPLAY_LINE, b"\x00")

    def press(self, group_cmd, payload):
        self._send(build_packet(0, group_cmd, payload))
        self._send(build_packet(0, group_cmd, bytes(len(payload))))

    def _reply(self, dest, cmd, data):
        self._send(build_packet(dest, cmd, data))

    def _send(self, packet):
        with self._lock:
            self._out += packet
            self._lock.notify_all()

    def read(self, size=1):
        with self._lock:
            if not self._out:
                self._lock.wait(0.05)
            data = bytes(self._out[:max(size, len(self._out))])
            del self._out[:len(data)]
            return data

    def close(self):
        self.is_open = False


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.spoken = []
        FakeDotPad.instance = None
        self.settings = {"dotpad_enabled": True, "dotpad_port": "COM9"}
        patcher = mock.patch.object(dotpad.serial, "Serial", FakeDotPad)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.controller = DotPadController(lambda: self.settings, self.spoken.append)
        self.addCleanup(self.controller.shutdown)

    def _wait(self, condition, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return True
            time.sleep(0.02)
        return False

    def test_shape_and_name_reach_the_device(self):
        square = [[(0, 0), (10, 0), (10, 10), (0, 10)]]
        self.controller.show_shape("Peru", square)
        self.assertTrue(self._wait(lambda: FakeDotPad.instance is not None
                                   and len(FakeDotPad.instance.lines) == 11))
        pad = FakeDotPad.instance
        self.assertIn("Dot Pad connected.", self.spoken)
        seq, text = pad.lines[0]
        self.assertEqual(seq, dotpad.SEQ_TEXT)
        self.assertEqual(list(text[1:6]), text_to_braille("Peru"))
        self.assertEqual(len(text), 21)
        seq, middle = pad.lines[1 + 5]
        self.assertEqual(middle[0], 0)
        self.assertEqual(middle[15], 0xFF)   # solid fill in the centre

    def test_function_keys(self):
        square = [[(0, 0), (10, 0), (10, 10), (0, 10)]]
        self.controller.show_shape("Peru", square)
        self.assertTrue(self._wait(lambda: FakeDotPad.instance is not None
                                   and len(FakeDotPad.instance.lines) == 11))
        pad = FakeDotPad.instance
        pad.press(dotpad.NTF_KEYS_FUNCTION, b"\x80")          # F1
        self.assertTrue(self._wait(lambda: "Outline." in self.spoken))
        self.assertTrue(self._wait(lambda: pad.lines[6][1][15] == 0))
        pad.press(dotpad.NTF_KEYS_FUNCTION, b"\x10")          # F4
        self.assertTrue(self._wait(lambda: "Dot Pad cleared." in self.spoken))
        self.assertTrue(all(not any(data[1:]) for _, data in pad.lines.values()))

    def test_pan_scrolls_long_names(self):
        name = "Saint Vincent and the Grenadines"
        self.controller.show_shape(name, [[(0, 0), (1, 0), (1, 1)]])
        self.assertTrue(self._wait(
            lambda: FakeDotPad.instance is not None and 0 in FakeDotPad.instance.lines))
        pad = FakeDotPad.instance
        pad.press(dotpad.NTF_KEYS_PERKINS, b"\x00\x02")       # pan right (key 14)
        expected = text_to_braille(name)[20:40]
        self.assertTrue(self._wait(
            lambda: list(pad.lines[0][1][1:1 + len(expected)]) == expected))

    def test_follow_draws_only_the_country_movement_settles_in(self):
        self.controller.FOLLOW_SETTLE = 0.3
        square = [[(0, 0), (10, 0), (10, 10), (0, 10)]]
        self.controller.follow("Peru", square, None, (5, 5))
        self.controller.follow("Chile", square, None, (5, 5))
        self.assertTrue(self._wait(lambda: FakeDotPad.instance is not None
                                   and len(FakeDotPad.instance.lines) == 11))
        pad = FakeDotPad.instance
        expected = text_to_braille("Chile")
        self.assertEqual(list(pad.lines[0][1][1:1 + len(expected)]), expected)
        # The marker is a gap in the middle of the filled square (cell 15).
        self.assertNotEqual(pad.lines[6][1][16], 0xFF)
        before = dict(pad.lines)
        self.controller.follow("Chile", square, None, (1, 9))
        self.assertTrue(self._wait(lambda: pad.lines[6][1][16] == 0xFF))
        self.assertEqual(pad.lines[0], before[0])   # braille line untouched

    def test_f2_toggles_follow_and_reports_it(self):
        changes = []
        self.controller._on_follow_changed = changes.append
        square = [[(0, 0), (10, 0), (10, 10), (0, 10)]]
        self.controller.show_shape("Peru", square, None, (5, 5))
        self.assertTrue(self._wait(lambda: FakeDotPad.instance is not None
                                   and len(FakeDotPad.instance.lines) == 11))
        pad = FakeDotPad.instance
        self.assertNotEqual(pad.lines[6][1][16], 0xFF)
        pad.press(dotpad.NTF_KEYS_FUNCTION, b"@")          # F2
        self.assertTrue(self._wait(lambda: "Follow off." in self.spoken))
        self.assertEqual(changes, [False])
        self.assertTrue(self._wait(lambda: pad.lines[6][1][16] == 0xFF))
        self.assertFalse(self.controller.following)

    def test_follow_setting_off_ignores_movement(self):
        self.settings["dotpad_follow"] = False
        controller = DotPadController(lambda: self.settings, self.spoken.append)
        self.addCleanup(controller.shutdown)
        controller.follow("Peru", [[(0, 0), (1, 0), (1, 1)]], None, (0, 0))
        time.sleep(0.8)
        self.assertIsNone(FakeDotPad.instance)

    def test_disabled_does_nothing(self):
        self.settings["dotpad_enabled"] = False
        self.controller.show_shape("Peru", [[(0, 0), (1, 0), (1, 1)]])
        time.sleep(0.2)
        self.assertIsNone(FakeDotPad.instance)


if __name__ == "__main__":
    unittest.main()
