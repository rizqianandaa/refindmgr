"""Minimal PNG decoding and scaling, with no third-party dependency.

refindmgr must be able to turn a bundled preview into raw pixels on a machine
that has nothing installed -- a freshly imaged server has no Pillow, and
requiring it just to draw a thumbnail would be absurd.  Only what the bundled
assets actually need is implemented: 8-bit non-interlaced PNG in palette,
greyscale, RGB and RGBA forms.

This exists because character art can never resemble a photograph.  On a Linux
virtual console there is no image protocol at all, so the only way to show a
real picture is to decode it here and write the pixels to the framebuffer.
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path
from typing import List, Optional, Tuple

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# colour type -> channels per pixel
_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


class ImageError(Exception):
    """The image could not be decoded."""


class RGBImage:
    """A decoded, packed RGB image: three bytes per pixel, row-major."""

    __slots__ = ("width", "height", "data")

    def __init__(self, width: int, height: int, data: bytearray):
        self.width = width
        self.height = height
        self.data = data

    def pixel(self, x: int, y: int) -> Tuple[int, int, int]:
        index = (y * self.width + x) * 3
        return (self.data[index], self.data[index + 1], self.data[index + 2])

    def row(self, y: int) -> memoryview:
        start = y * self.width * 3
        return memoryview(self.data)[start:start + self.width * 3]


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def _unfilter(raw: bytes, width: int, height: int, bpp: int, stride: int) -> bytearray:
    """Reverse the per-scanline PNG filters."""
    out = bytearray(stride * height)
    previous = bytearray(stride)
    position = 0
    for y in range(height):
        if position >= len(raw):
            raise ImageError("data PNG terpotong")
        filter_type = raw[position]
        position += 1
        line = bytearray(raw[position:position + stride])
        if len(line) < stride:
            raise ImageError("baris PNG terpotong")
        position += stride

        if filter_type == 0:
            pass
        elif filter_type == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif filter_type == 2:
            for i in range(stride):
                line[i] = (line[i] + previous[i]) & 0xFF
        elif filter_type == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + previous[i]) >> 1)) & 0xFF
        elif filter_type == 4:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                upper_left = previous[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + _paeth(left, previous[i], upper_left)) & 0xFF
        else:
            raise ImageError(f"filter PNG tidak dikenal: {filter_type}")

        out[y * stride:(y + 1) * stride] = line
        previous = line
    return out


def _expand_bits(line: bytes, width: int, depth: int) -> List[int]:
    """Expand sub-byte palette indices into one value per pixel."""
    values: List[int] = []
    per_byte = 8 // depth
    mask = (1 << depth) - 1
    for byte in line:
        for slot in range(per_byte):
            shift = 8 - depth * (slot + 1)
            values.append((byte >> shift) & mask)
            if len(values) == width:
                return values
    return values[:width]


def decode_png(path: Path) -> RGBImage:
    """Decode a PNG file into packed RGB bytes."""
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise ImageError(f"tidak dapat membaca {path}: {exc}") from exc
    if not data.startswith(PNG_SIGNATURE):
        raise ImageError("berkas bukan PNG")

    width = height = depth = color_type = interlace = 0
    palette: bytes = b""
    idat = bytearray()
    position = len(PNG_SIGNATURE)
    while position + 8 <= len(data):
        length = struct.unpack(">I", data[position:position + 4])[0]
        kind = data[position + 4:position + 8]
        body = data[position + 8:position + 8 + length]
        position += 12 + length
        if kind == b"IHDR":
            (width, height, depth, color_type, _comp,
             _filt, interlace) = struct.unpack(">IIBBBBB", body[:13])
        elif kind == b"PLTE":
            palette = bytes(body)
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break

    if not width or not height:
        raise ImageError("PNG tanpa IHDR yang valid")
    if interlace:
        raise ImageError("PNG interlace (Adam7) tidak didukung")
    if color_type not in _CHANNELS:
        raise ImageError(f"tipe warna PNG tidak didukung: {color_type}")
    if depth not in (1, 2, 4, 8):
        raise ImageError(f"kedalaman bit PNG tidak didukung: {depth}")
    if depth != 8 and color_type != 3:
        raise ImageError("kedalaman <8 bit hanya didukung untuk PNG palet")

    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error as exc:
        raise ImageError(f"data PNG rusak: {exc}") from exc

    channels = _CHANNELS[color_type]
    bits_per_pixel = channels * depth
    stride = (width * bits_per_pixel + 7) // 8
    bpp = max(1, bits_per_pixel // 8)
    lines = _unfilter(raw, width, height, bpp, stride)

    out = bytearray(width * height * 3)
    for y in range(height):
        line = lines[y * stride:(y + 1) * stride]
        base = y * width * 3
        if color_type == 3:
            indices = _expand_bits(line, width, depth) if depth != 8 else line
            for x in range(width):
                source = indices[x] * 3
                if source + 3 > len(palette):
                    raise ImageError("indeks palet PNG di luar jangkauan")
                out[base + x * 3:base + x * 3 + 3] = palette[source:source + 3]
        elif color_type == 2:
            out[base:base + width * 3] = line[:width * 3]
        elif color_type == 0:
            for x in range(width):
                grey = line[x]
                out[base + x * 3] = grey
                out[base + x * 3 + 1] = grey
                out[base + x * 3 + 2] = grey
        elif color_type == 4:
            for x in range(width):
                grey = line[x * 2]
                out[base + x * 3] = grey
                out[base + x * 3 + 1] = grey
                out[base + x * 3 + 2] = grey
        else:  # RGBA
            for x in range(width):
                source = x * 4
                out[base + x * 3:base + x * 3 + 3] = line[source:source + 3]
    return RGBImage(width, height, out)


def scale(image: RGBImage, target_width: int, target_height: int) -> RGBImage:
    """Box-filter downscale (or nearest-neighbour upscale).

    Averaging every source pixel that lands in a destination cell keeps fine
    detail such as small icons legible; nearest-neighbour sampling at thumbnail
    sizes drops them entirely.
    """
    target_width = max(1, int(target_width))
    target_height = max(1, int(target_height))
    if target_width == image.width and target_height == image.height:
        return image

    out = bytearray(target_width * target_height * 3)
    source = image.data
    width, height = image.width, image.height
    for y in range(target_height):
        y0 = (y * height) // target_height
        y1 = max(y0 + 1, ((y + 1) * height) // target_height)
        for x in range(target_width):
            x0 = (x * width) // target_width
            x1 = max(x0 + 1, ((x + 1) * width) // target_width)
            red = green = blue = count = 0
            for sy in range(y0, y1):
                row = sy * width
                for sx in range(x0, x1):
                    index = (row + sx) * 3
                    red += source[index]
                    green += source[index + 1]
                    blue += source[index + 2]
                    count += 1
            index = (y * target_width + x) * 3
            out[index] = red // count
            out[index + 1] = green // count
            out[index + 2] = blue // count
    return RGBImage(target_width, target_height, out)


def fit(image: RGBImage, box_width: int, box_height: int) -> RGBImage:
    """Scale into the box, preserving aspect ratio."""
    factor = min(box_width / image.width, box_height / image.height)
    return scale(image, max(1, int(image.width * factor)),
                 max(1, int(image.height * factor)))


def load_scaled(path: Path, box_width: int, box_height: int) -> Optional[RGBImage]:
    """Decode and fit in one step; returns None when the file is unusable."""
    try:
        return fit(decode_png(path), box_width, box_height)
    except ImageError:
        return None
