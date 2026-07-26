"""Draw real pixels on a Linux virtual console, inline with the text.

The Linux console implements no image protocol: no Sixel, no Kitty graphics,
no iTerm2 images, and its font carries none of the block or sextant glyphs that
character-art renderers rely on.  Every terminal-side approach therefore ends at
coloured ASCII, which cannot resemble a photograph no matter how it is tuned.

What the console *does* have is a framebuffer.  ``fim`` and ``fbi`` look right
precisely because they bypass the terminal and write pixels to ``/dev/fb0``.
This module does the same thing, but positioned at a character cell instead of
full-screen, so thumbnails can sit beside their titles in the catalog.

Everything here is stdlib: ioctl for the screen geometry, mmap for the pixels.
"""
from __future__ import annotations

import os
import struct
from pathlib import Path
from typing import Optional

from .imaging import RGBImage

FBIOGET_VSCREENINFO = 0x4600
FBIOGET_FSCREENINFO = 0x4602

DEFAULT_DEVICE = "/dev/fb0"
DEVICE_ENV = "REFINDMGR_FB_DEVICE"


def resolve_device(device: Optional[str] = None) -> str:
    """Pick the framebuffer node to use.

    Resolved per call rather than as a default argument: a default binds the
    value at import time, which makes the device impossible to override later
    (and silently ignores REFINDMGR_FB_DEVICE).
    """
    return device or os.environ.get(DEVICE_ENV) or DEFAULT_DEVICE


class FramebufferError(Exception):
    """The framebuffer could not be used."""


class ScreenInfo:
    """Geometry and pixel format of the framebuffer."""

    __slots__ = ("width", "height", "bits_per_pixel", "line_length",
                 "red", "green", "blue")

    def __init__(self, width, height, bits_per_pixel, line_length, red, green, blue):
        self.width = width
        self.height = height
        self.bits_per_pixel = bits_per_pixel
        self.line_length = line_length
        self.red = red        # (offset, length)
        self.green = green
        self.blue = blue

    @property
    def bytes_per_pixel(self) -> int:
        return max(1, self.bits_per_pixel // 8)

    def pack(self, red: int, green: int, blue: int) -> bytes:
        """Convert 8-bit RGB into this framebuffer's pixel format."""
        value = 0
        for component, (offset, length) in (
            (red, self.red), (green, self.green), (blue, self.blue)
        ):
            if length <= 0:
                continue
            value |= (component >> (8 - length)) << offset
        return value.to_bytes(self.bytes_per_pixel, "little")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"ScreenInfo({self.width}x{self.height}, {self.bits_per_pixel}bpp, "
                f"stride={self.line_length})")


def parse_var_screeninfo(raw: bytes) -> ScreenInfo:
    """Parse ``struct fb_var_screeninfo`` (geometry and colour bitfields)."""
    if len(raw) < 68:
        raise FramebufferError("fb_var_screeninfo terlalu pendek")
    width, height = struct.unpack_from("<II", raw, 0)
    bits_per_pixel = struct.unpack_from("<I", raw, 24)[0]
    red = struct.unpack_from("<II", raw, 32)
    green = struct.unpack_from("<II", raw, 44)
    blue = struct.unpack_from("<II", raw, 56)
    return ScreenInfo(width, height, bits_per_pixel, 0, red, green, blue)


def parse_fix_screeninfo(raw: bytes) -> int:
    """Return ``line_length`` from ``struct fb_fix_screeninfo``.

    The stride is not always width * bytes_per_pixel: drivers pad rows, and
    ignoring that skews every row of the image diagonally across the screen.
    """
    if len(raw) < 52:
        raise FramebufferError("fb_fix_screeninfo terlalu pendek")
    return struct.unpack_from("<I", raw, 48)[0]


GEOMETRY_ENV = "REFINDMGR_FB_GEOMETRY"


def parse_geometry(value: str) -> Optional[ScreenInfo]:
    """Parse ``WIDTHxHEIGHTxBPP[:STRIDE]`` into a ScreenInfo.

    An escape hatch for framebuffers whose ioctl reports nonsense, and the only
    way to exercise this code without a real console device.
    """
    text = (value or "").strip().lower()
    if not text:
        return None
    stride = 0
    if ":" in text:
        text, _, tail = text.partition(":")
        if tail.isdigit():
            stride = int(tail)
    parts = text.split("x")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return None
    width, height, bits = (int(part) for part in parts)
    if width <= 0 or height <= 0 or bits not in (16, 24, 32):
        return None
    if bits == 16:
        layout = ((11, 5), (5, 6), (0, 5))          # RGB565
    else:
        layout = ((16, 8), (8, 8), (0, 8))          # BGRX / BGR
    info = ScreenInfo(width, height, bits, stride, *layout)
    if not info.line_length:
        info.line_length = width * info.bytes_per_pixel
    return info


def probe(device: Optional[str] = None) -> Optional[ScreenInfo]:
    """Read the framebuffer geometry, or None when it is unavailable."""
    override = parse_geometry(os.environ.get(GEOMETRY_ENV, ""))
    if override is not None:
        return override
    try:
        import fcntl
    except ImportError:
        return None
    device = resolve_device(device)
    try:
        fd = os.open(device, os.O_RDWR)
    except OSError:
        return None
    try:
        var = fcntl.ioctl(fd, FBIOGET_VSCREENINFO, bytes(160))
        fix = fcntl.ioctl(fd, FBIOGET_FSCREENINFO, bytes(80))
        info = parse_var_screeninfo(var)
        info.line_length = parse_fix_screeninfo(fix)
    except (OSError, FramebufferError, ValueError):
        return None
    finally:
        os.close(fd)
    if not info.width or not info.height or info.bits_per_pixel < 8:
        return None
    if not info.line_length:
        info.line_length = info.width * info.bytes_per_pixel
    return info


def render_rows(image: RGBImage, info: ScreenInfo) -> list:
    """Pre-pack every image row into framebuffer pixel format."""
    rows = []
    for y in range(image.height):
        row = image.row(y)
        packed = bytearray()
        for x in range(image.width):
            index = x * 3
            packed += info.pack(row[index], row[index + 1], row[index + 2])
        rows.append(bytes(packed))
    return rows


def blit(image: RGBImage, x: int, y: int, *, device: Optional[str] = None,
         info: Optional[ScreenInfo] = None) -> bool:
    """Copy an image to the framebuffer with its top-left corner at (x, y)."""
    device = resolve_device(device)
    info = info or probe(device)
    if info is None:
        return False
    import mmap

    try:
        fd = os.open(device, os.O_RDWR)
    except OSError:
        return False
    try:
        size = info.line_length * info.height
        try:
            buffer = mmap.mmap(fd, size, mmap.MAP_SHARED,
                               mmap.PROT_READ | mmap.PROT_WRITE)
        except (OSError, ValueError):
            return False
        try:
            rows = render_rows(image, info)
            bpp = info.bytes_per_pixel
            for row_index, packed in enumerate(rows):
                target_y = y + row_index
                if target_y < 0 or target_y >= info.height:
                    continue
                # Clip horizontally rather than wrapping onto the next line.
                start_x = max(0, x)
                skip = (start_x - x) * bpp
                available = (info.width - start_x) * bpp
                chunk = packed[skip:skip + available]
                if not chunk:
                    continue
                offset = target_y * info.line_length + start_x * bpp
                buffer[offset:offset + len(chunk)] = chunk
            buffer.flush()
        finally:
            buffer.close()
    finally:
        os.close(fd)
    return True


def fill_rects(rects, *, device: Optional[str] = None,
               info: Optional[ScreenInfo] = None, rgb=(0, 0, 0)) -> bool:
    """Blank rectangles previously drawn.

    A terminal screen clear repaints the text layer only; pixels written to the
    framebuffer directly outlive it, so anything drawn has to be erased
    explicitly or it lingers over whatever comes next.
    """
    device = resolve_device(device)
    info = info or probe(device)
    if info is None or not rects:
        return False
    import mmap

    try:
        fd = os.open(device, os.O_RDWR)
    except OSError:
        return False
    try:
        try:
            buffer = mmap.mmap(fd, info.line_length * info.height, mmap.MAP_SHARED,
                               mmap.PROT_READ | mmap.PROT_WRITE)
        except (OSError, ValueError):
            return False
        try:
            pixel = info.pack(*rgb)
            bpp = info.bytes_per_pixel
            for x, y, width, height in rects:
                start_x = max(0, x)
                span = min(width - (start_x - x), info.width - start_x)
                if span <= 0:
                    continue
                blank = pixel * span
                for offset_y in range(height):
                    target_y = y + offset_y
                    if 0 <= target_y < info.height:
                        offset = target_y * info.line_length + start_x * bpp
                        buffer[offset:offset + len(blank)] = blank
            buffer.flush()
        finally:
            buffer.close()
    finally:
        os.close(fd)
    return True


def cell_size(info: ScreenInfo, columns: int, rows: int):
    """Pixel size of one character cell, derived from the console geometry."""
    if columns <= 0 or rows <= 0:
        return None
    width = info.width // columns
    height = info.height // rows
    if width <= 0 or height <= 0:
        return None
    return width, height


def available(device: Optional[str] = None) -> bool:
    """True when this process can actually write to the framebuffer.

    Under X or Wayland the console framebuffer is not what the user is looking
    at, so drawing to it would be invisible at best.
    """
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        return False
    device = resolve_device(device)
    if not Path(device).exists():
        return False
    return os.access(device, os.R_OK | os.W_OK)
