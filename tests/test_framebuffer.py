"""Tests for writing real pixels to a Linux console framebuffer."""
import os
import struct
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from refindmgr import framebuffer as fb
from refindmgr import imaging


def var_screeninfo(width, height, bpp, red, green, blue) -> bytes:
    raw = bytearray(160)
    struct.pack_into("<II", raw, 0, width, height)
    struct.pack_into("<I", raw, 24, bpp)
    struct.pack_into("<II", raw, 32, *red)
    struct.pack_into("<II", raw, 44, *green)
    struct.pack_into("<II", raw, 56, *blue)
    return bytes(raw)


def fix_screeninfo(line_length) -> bytes:
    raw = bytearray(80)
    struct.pack_into("<I", raw, 48, line_length)
    return bytes(raw)


BGRX = ((16, 8), (8, 8), (0, 8))
RGB565 = ((11, 5), (5, 6), (0, 5))


class TestScreenInfoParsing(unittest.TestCase):
    def test_parses_geometry_and_bitfields(self):
        info = fb.parse_var_screeninfo(var_screeninfo(1920, 1080, 32, *BGRX))
        self.assertEqual((info.width, info.height, info.bits_per_pixel), (1920, 1080, 32))
        self.assertEqual(info.red, (16, 8))
        self.assertEqual(info.bytes_per_pixel, 4)

    def test_reads_line_length_not_the_naive_width(self):
        # Drivers pad rows. Assuming width * bpp skews every row of the image
        # diagonally across the screen.
        self.assertEqual(fb.parse_fix_screeninfo(fix_screeninfo(7680)), 7680)

    def test_short_structs_are_rejected(self):
        with self.assertRaises(fb.FramebufferError):
            fb.parse_var_screeninfo(b"\x00" * 10)
        with self.assertRaises(fb.FramebufferError):
            fb.parse_fix_screeninfo(b"\x00" * 10)


class TestPixelPacking(unittest.TestCase):
    def test_32bpp_bgrx(self):
        info = fb.ScreenInfo(8, 8, 32, 32, *BGRX)
        self.assertEqual(info.pack(255, 0, 0), bytes([0x00, 0x00, 0xFF, 0x00]))
        self.assertEqual(info.pack(0, 255, 0), bytes([0x00, 0xFF, 0x00, 0x00]))
        self.assertEqual(info.pack(0, 0, 255), bytes([0xFF, 0x00, 0x00, 0x00]))

    def test_16bpp_rgb565(self):
        info = fb.ScreenInfo(8, 8, 16, 16, *RGB565)
        self.assertEqual(info.pack(255, 0, 0), (0xF800).to_bytes(2, "little"))
        self.assertEqual(info.pack(0, 255, 0), (0x07E0).to_bytes(2, "little"))
        self.assertEqual(info.pack(0, 0, 255), (0x001F).to_bytes(2, "little"))
        self.assertEqual(info.pack(255, 255, 255), b"\xff\xff")


class TestGeometryOverride(unittest.TestCase):
    def test_parses_with_and_without_stride(self):
        info = fb.parse_geometry("1024x768x32")
        self.assertEqual((info.width, info.height, info.bits_per_pixel), (1024, 768, 32))
        self.assertEqual(info.line_length, 1024 * 4)
        self.assertEqual(fb.parse_geometry("800x600x16:2048").line_length, 2048)

    def test_rejects_nonsense(self):
        for value in ("", "abc", "1024x768", "1024x768x7", "0x0x32"):
            with self.subTest(value=value):
                self.assertIsNone(fb.parse_geometry(value))

    def test_override_short_circuits_the_ioctl(self):
        with patch.dict(os.environ, {fb.GEOMETRY_ENV: "640x480x32"}):
            info = fb.probe("/nonexistent/fb0")
        self.assertEqual((info.width, info.height), (640, 480))


class TestDeviceResolution(unittest.TestCase):
    def test_env_overrides_the_default(self):
        with patch.dict(os.environ, {fb.DEVICE_ENV: "/tmp/fake-fb"}):
            self.assertEqual(fb.resolve_device(), "/tmp/fake-fb")

    def test_explicit_argument_wins(self):
        with patch.dict(os.environ, {fb.DEVICE_ENV: "/tmp/fake-fb"}):
            self.assertEqual(fb.resolve_device("/dev/fb1"), "/dev/fb1")

    def test_default_when_nothing_is_set(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(fb.resolve_device(), fb.DEFAULT_DEVICE)

    def test_not_available_under_a_graphical_session(self):
        # The console framebuffer is not what an X/Wayland user is looking at.
        with patch.dict(os.environ, {"DISPLAY": ":0"}):
            self.assertFalse(fb.available())
        with patch.dict(os.environ, {"WAYLAND_DISPLAY": "wayland-0"}):
            self.assertFalse(fb.available())


class TestBlitting(unittest.TestCase):
    WIDTH, HEIGHT, STRIDE = 64, 32, 64 * 4 + 16   # deliberately padded

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.device = Path(self.tmp.name) / "fb0"
        self.device.write_bytes(b"\x00" * (self.STRIDE * self.HEIGHT))
        self.info = fb.ScreenInfo(self.WIDTH, self.HEIGHT, 32, self.STRIDE, *BGRX)

    def tearDown(self):
        self.tmp.cleanup()

    def _read_pixel(self, x, y):
        data = self.device.read_bytes()
        offset = y * self.STRIDE + x * 4
        blue, green, red, _ = data[offset:offset + 4]
        return (red, green, blue)

    def _image(self, width, height, color):
        return imaging.RGBImage(width, height, bytearray(bytes(color) * width * height))

    def test_writes_at_the_requested_position(self):
        ok = fb.blit(self._image(4, 3, (10, 20, 30)), 5, 7,
                     device=str(self.device), info=self.info)
        self.assertTrue(ok)
        self.assertEqual(self._read_pixel(5, 7), (10, 20, 30))
        self.assertEqual(self._read_pixel(8, 9), (10, 20, 30))
        self.assertEqual(self._read_pixel(4, 7), (0, 0, 0))   # just outside
        self.assertEqual(self._read_pixel(9, 7), (0, 0, 0))

    def test_row_padding_is_respected(self):
        # With the stride ignored, row 1 would start 16 bytes early and the
        # image would shear diagonally.
        fb.blit(self._image(2, 2, (255, 255, 255)), 0, 0,
                device=str(self.device), info=self.info)
        data = self.device.read_bytes()
        self.assertEqual(data[self.STRIDE:self.STRIDE + 4], bytes([255, 255, 255, 0]))
        self.assertEqual(data[self.WIDTH * 4:self.WIDTH * 4 + 4], b"\x00\x00\x00\x00")

    def test_clips_instead_of_wrapping_onto_the_next_row(self):
        fb.blit(self._image(10, 2, (1, 2, 3)), self.WIDTH - 3, 0,
                device=str(self.device), info=self.info)
        self.assertEqual(self._read_pixel(self.WIDTH - 1, 0), (1, 2, 3))
        # Nothing may appear at the left edge of the following row.
        self.assertEqual(self._read_pixel(0, 1), (0, 0, 0))

    def test_rows_below_the_screen_are_dropped(self):
        ok = fb.blit(self._image(2, 8, (9, 9, 9)), 0, self.HEIGHT - 2,
                     device=str(self.device), info=self.info)
        self.assertTrue(ok)
        self.assertEqual(self._read_pixel(0, self.HEIGHT - 1), (9, 9, 9))

    def test_fill_rects_erases_what_was_drawn(self):
        # A terminal screen clear repaints text only; framebuffer pixels survive
        # it and would linger over whatever is shown next.
        fb.blit(self._image(6, 4, (200, 100, 50)), 2, 2,
                device=str(self.device), info=self.info)
        self.assertNotEqual(self._read_pixel(3, 3), (0, 0, 0))
        fb.fill_rects([(2, 2, 6, 4)], device=str(self.device), info=self.info)
        self.assertEqual(self._read_pixel(3, 3), (0, 0, 0))

    def test_missing_device_fails_quietly(self):
        self.assertFalse(fb.blit(self._image(2, 2, (1, 1, 1)), 0, 0,
                                 device="/nonexistent/fb0", info=self.info))


class TestCellSize(unittest.TestCase):
    def test_derived_from_console_geometry(self):
        info = fb.ScreenInfo(1024, 768, 32, 4096, *BGRX)
        self.assertEqual(fb.cell_size(info, 128, 48), (8, 16))

    def test_guards_against_zero(self):
        info = fb.ScreenInfo(1024, 768, 32, 4096, *BGRX)
        self.assertIsNone(fb.cell_size(info, 0, 48))
        self.assertIsNone(fb.cell_size(info, 128, 0))


if __name__ == "__main__":
    unittest.main()
