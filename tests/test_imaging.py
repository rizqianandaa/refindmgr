"""Tests for the dependency-free PNG decoder and scaler."""
import struct
import sys
import unittest
import zlib
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from refindmgr import imaging


def _chunk(kind: bytes, body: bytes) -> bytes:
    return (struct.pack(">I", len(body)) + kind + body
            + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))


def _png(width, height, color_type, depth, rows, palette=b"") -> bytes:
    """Build a PNG with filter 0 on every scanline."""
    ihdr = struct.pack(">IIBBBBB", width, height, depth, color_type, 0, 0, 0)
    data = b"".join(b"\x00" + bytes(row) for row in rows)
    out = imaging.PNG_SIGNATURE + _chunk(b"IHDR", ihdr)
    if palette:
        out += _chunk(b"PLTE", palette)
    return out + _chunk(b"IDAT", zlib.compress(data)) + _chunk(b"IEND", b"")


def _write(directory, name, payload) -> Path:
    path = Path(directory) / name
    path.write_bytes(payload)
    return path


class TestPngDecoding(unittest.TestCase):
    def test_truecolor(self):
        rows = [[255, 0, 0, 0, 255, 0], [0, 0, 255, 255, 255, 255]]
        with TemporaryDirectory() as tmp:
            image = imaging.decode_png(_write(tmp, "rgb.png", _png(2, 2, 2, 8, rows)))
        self.assertEqual((image.width, image.height), (2, 2))
        self.assertEqual(image.pixel(0, 0), (255, 0, 0))
        self.assertEqual(image.pixel(1, 1), (255, 255, 255))

    def test_palette(self):
        palette = bytes([255, 0, 0, 0, 255, 0, 0, 0, 255])
        rows = [[0, 1], [2, 0]]
        with TemporaryDirectory() as tmp:
            image = imaging.decode_png(_write(tmp, "p.png", _png(2, 2, 3, 8, rows, palette)))
        self.assertEqual(image.pixel(0, 0), (255, 0, 0))
        self.assertEqual(image.pixel(0, 1), (0, 0, 255))

    def test_sub_byte_palette_depth(self):
        # PIL writes 1/2/4-bit palettes when the image has few colours.
        palette = bytes([0, 0, 0, 255, 255, 255])
        with TemporaryDirectory() as tmp:
            image = imaging.decode_png(_write(tmp, "p1.png", _png(4, 1, 3, 1, [[0b01010000]], palette)))
        self.assertEqual([image.pixel(x, 0) for x in range(4)],
                         [(0, 0, 0), (255, 255, 255), (0, 0, 0), (255, 255, 255)])

    def test_greyscale_and_alpha_forms(self):
        with TemporaryDirectory() as tmp:
            grey = imaging.decode_png(_write(tmp, "g.png", _png(2, 1, 0, 8, [[0, 200]])))
            rgba = imaging.decode_png(
                _write(tmp, "a.png", _png(1, 1, 6, 8, [[10, 20, 30, 128]]))
            )
        self.assertEqual(grey.pixel(1, 0), (200, 200, 200))
        # Alpha is dropped rather than composited; the framebuffer is opaque.
        self.assertEqual(rgba.pixel(0, 0), (10, 20, 30))

    def test_every_filter_type_round_trips(self):
        # Filters 1-4 are what real encoders emit; getting Paeth wrong shows up
        # as diagonal smearing that is easy to miss by eye.
        width, height = 4, 4
        original = [[(x * 37 + y * 11) % 256 for x in range(width * 3)] for y in range(height)]
        for filter_type in range(5):
            with self.subTest(filter=filter_type):
                encoded = self._encode_with_filter(original, width, filter_type)
                ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
                blob = (imaging.PNG_SIGNATURE + _chunk(b"IHDR", ihdr)
                        + _chunk(b"IDAT", zlib.compress(encoded)) + _chunk(b"IEND", b""))
                with TemporaryDirectory() as tmp:
                    image = imaging.decode_png(_write(tmp, "f.png", blob))
                for y in range(height):
                    for x in range(width):
                        self.assertEqual(
                            list(image.pixel(x, y)), original[y][x * 3:x * 3 + 3]
                        )

    @staticmethod
    def _encode_with_filter(rows, width, filter_type):
        bpp, out, previous = 3, bytearray(), [0] * (width * 3)
        for row in rows:
            out.append(filter_type)
            line = []
            for i, value in enumerate(row):
                left = row[i - bpp] if i >= bpp else 0
                up = previous[i]
                upper_left = previous[i - bpp] if i >= bpp else 0
                if filter_type == 0:
                    line.append(value)
                elif filter_type == 1:
                    line.append((value - left) & 0xFF)
                elif filter_type == 2:
                    line.append((value - up) & 0xFF)
                elif filter_type == 3:
                    line.append((value - ((left + up) >> 1)) & 0xFF)
                else:
                    line.append((value - imaging._paeth(left, up, upper_left)) & 0xFF)
            out += bytes(line)
            previous = row
        return bytes(out)

    def test_rejects_non_png(self):
        with TemporaryDirectory() as tmp:
            path = _write(tmp, "x.png", b"not a png at all")
            with self.assertRaises(imaging.ImageError):
                imaging.decode_png(path)

    def test_rejects_interlaced(self):
        ihdr = struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 1)
        blob = (imaging.PNG_SIGNATURE + _chunk(b"IHDR", ihdr)
                + _chunk(b"IDAT", zlib.compress(b"\x00" * 20)) + _chunk(b"IEND", b""))
        with TemporaryDirectory() as tmp:
            with self.assertRaises(imaging.ImageError) as ctx:
                imaging.decode_png(_write(tmp, "i.png", blob))
        self.assertIn("interlace", str(ctx.exception))

    def test_truncated_data_is_reported_not_crashed(self):
        ihdr = struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0)
        blob = (imaging.PNG_SIGNATURE + _chunk(b"IHDR", ihdr)
                + _chunk(b"IDAT", zlib.compress(b"\x00" * 5)) + _chunk(b"IEND", b""))
        with TemporaryDirectory() as tmp:
            with self.assertRaises(imaging.ImageError):
                imaging.decode_png(_write(tmp, "t.png", blob))

    def test_missing_file_raises_image_error(self):
        with self.assertRaises(imaging.ImageError):
            imaging.decode_png(Path("/nonexistent/x.png"))


class TestBundledPreviews(unittest.TestCase):
    ASSETS = Path(__file__).resolve().parent.parent / "refindmgr" / "assets" / "previews"

    def test_every_shipped_preview_decodes(self):
        # These are what the framebuffer backend draws; a preview that cannot be
        # decoded means no image at all on a console.
        for path in sorted(self.ASSETS.glob("*.png")):
            with self.subTest(preview=path.name):
                image = imaging.decode_png(path)
                self.assertEqual((image.width, image.height), (512, 288))
                self.assertEqual(len(image.data), 512 * 288 * 3)


class TestScaling(unittest.TestCase):
    def _solid(self, width, height, color):
        return imaging.RGBImage(width, height, bytearray(bytes(color) * width * height))

    def test_downscale_preserves_a_solid_colour(self):
        small = imaging.scale(self._solid(8, 8, (10, 20, 30)), 3, 3)
        self.assertEqual((small.width, small.height), (3, 3))
        self.assertEqual(small.pixel(1, 1), (10, 20, 30))

    def test_box_filter_averages_rather_than_sampling(self):
        # Half black, half white: nearest-neighbour would return one or the
        # other, so an averaged mid-grey proves the box filter is running.
        data = bytearray()
        for _ in range(2):
            data += bytes([0, 0, 0]) + bytes([255, 255, 255])
        image = imaging.RGBImage(2, 2, data)
        self.assertEqual(imaging.scale(image, 1, 1).pixel(0, 0), (127, 127, 127))

    def test_fit_preserves_aspect_ratio(self):
        fitted = imaging.fit(self._solid(512, 288, (1, 2, 3)), 256, 256)
        self.assertEqual((fitted.width, fitted.height), (256, 144))

    def test_scale_is_a_no_op_at_the_same_size(self):
        image = self._solid(4, 4, (7, 7, 7))
        self.assertIs(imaging.scale(image, 4, 4), image)

    def test_load_scaled_returns_none_for_bad_input(self):
        self.assertIsNone(imaging.load_scaled(Path("/nonexistent.png"), 10, 10))


if __name__ == "__main__":
    unittest.main()
