import time
import unittest
from unittest import mock

import numpy as np

import dotpad
import monarch
from dotpad import DotPadController, text_to_braille

# A braille-page collection with eight 32-cell rows and the 3840-pin report.
MONARCH_DESCRIPTOR = bytes([
    0x05, 0x41, 0x09, 0x01, 0xA1, 0x01,
    0x85, 0x02, 0x75, 0x08, 0x95, 0x20, 0x91, 0x02,          # a 32-cell row
    0x85, 0x21, 0x75, 0x01, 0x96, 0x00, 0x0F, 0x91, 0x02,    # 3840 pins
    0xC0,
])
OTHER_DESCRIPTOR = bytes([
    0x05, 0x41, 0x09, 0x01, 0xA1, 0x01,
    0x85, 0x02, 0x75, 0x08, 0x95, 0x28, 0x91, 0x02,          # 40 cells only
    0xC0,
])


class FakeHid:
    """Stands in for hid.device()."""

    descriptor = MONARCH_DESCRIPTOR
    busy = False
    instances = []

    def __init__(self):
        self.writes = []
        self.closed = False
        FakeHid.instances.append(self)

    def open_path(self, path):
        if FakeHid.busy:
            raise OSError("open failed")

    def get_report_descriptor(self):
        return list(self.descriptor)

    def write(self, data):
        self.writes.append(bytes(data))
        return len(data)

    def close(self):
        self.closed = True


def pins_of(report):
    """Unpack a written report 0x21 back to a (40, 96) bool array."""
    payload = np.frombuffer(report[1:], dtype=np.uint8).reshape(10, 48)
    pins = np.zeros((40, 96), dtype=bool)
    for by in range(10):
        for bx in range(48):
            for y in range(4):
                for x in range(2):
                    bit = int(monarch._BLOCK_BIT[y, x])
                    pins[by * 4 + y, bx * 2 + x] = bool(payload[by, bx] >> bit & 1)
    return pins


class PackingTests(unittest.TestCase):
    def test_descriptor_reports(self):
        self.assertEqual(monarch.output_report_bits(MONARCH_DESCRIPTOR),
                         {0x02: 256, 0x21: 3840})
        self.assertTrue(monarch.has_pin_report(MONARCH_DESCRIPTOR))
        self.assertFalse(monarch.has_pin_report(OTHER_DESCRIPTOR))

    def test_pin_bit_order(self):
        pins = np.zeros((40, 96), bool)
        pins[0, 0] = pins[3, 1] = True           # block 0: bits 0 and 7
        pins[0, 2] = True                        # block 1: bit 0
        pins[4, 0] = True                        # block 48 (second block row): bit 0
        pins[3, 0] = True                        # block 0: dot 7, bit 6
        payload = monarch.pack_pins(pins)
        self.assertEqual(len(payload), 480)
        self.assertEqual(payload[0], 0b11000001)
        self.assertEqual(payload[1], 0b00000001)
        self.assertEqual(payload[48], 0b00000001)

    def test_text_sits_on_the_bottom_line(self):
        pins = monarch.draw_text(np.zeros((40, 96), bool), text_to_braille("Peru"))
        self.assertFalse(pins[:36].any())
        self.assertTrue(pins[36:40, 0:2].any())  # "p", no capital sign first
        self.assertTrue(pins[36:40, 3:5].any())
        self.assertFalse(pins[36:40, 2].any())   # gap column between cells


class MonarchControllerTests(unittest.TestCase):
    def setUp(self):
        self.spoken = []
        FakeHid.instances = []
        FakeHid.descriptor = MONARCH_DESCRIPTOR
        FakeHid.busy = False
        info = {"path": b"\\\\?\\hid#monarch", "usage_page": 0x41,
                "product_string": "Monarch", "manufacturer_string": "HumanWare"}
        for patcher in (mock.patch.object(monarch, "candidates", lambda: [info]),
                        mock.patch.object(monarch, "hid", mock.Mock(device=FakeHid)),
                        mock.patch.object(dotpad, "candidate_ports", lambda p="": [])):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.settings = {"dotpad_enabled": True}
        self.controller = DotPadController(lambda: self.settings, self.spoken.append)
        self.addCleanup(self.controller.shutdown)

    def _wait(self, condition, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return True
            time.sleep(0.02)
        return False

    def _writes(self):
        return [w for dev in FakeHid.instances for w in dev.writes]

    def test_shape_and_name_drawn_in_one_frame(self):
        square = [[(0, 0), (10, 0), (10, 10), (0, 10)]]
        self.controller.show_shape("Peru", square)
        self.assertTrue(self._wait(lambda: self._writes()))
        self.assertIn("Monarch connected.", self.spoken)
        report = self._writes()[-1]
        self.assertEqual(report[0], 0x21)
        self.assertEqual(len(report), 481)
        pins = pins_of(report)
        self.assertTrue(pins[17, 48])             # middle of the square
        self.assertFalse(pins[35].any())          # blank row above the text
        self.assertTrue(pins[36:40, :12].any())   # "Peru" in braille

    def test_other_braille_displays_are_left_alone(self):
        FakeHid.descriptor = OTHER_DESCRIPTOR
        self.controller.show_shape("Peru", [[(0, 0), (1, 0), (1, 1)]])
        self.assertTrue(self._wait(lambda: FakeHid.instances
                                   and FakeHid.instances[0].closed))
        time.sleep(0.1)
        self.assertEqual(self._writes(), [])
        self.assertNotIn("Monarch connected.", self.spoken)

    def test_monarch_held_by_screen_reader_is_reported(self):
        FakeHid.busy = True
        self.controller.show_shape("Peru", [[(0, 0), (1, 0), (1, 1)]])
        self.assertTrue(self._wait(lambda: any(
            s.startswith("Monarch is in use") for s in self.spoken)))


if __name__ == "__main__":
    unittest.main()
