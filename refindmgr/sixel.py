"""Best-effort Sixel image previews using the standard img2sixel utility."""
from __future__ import annotations

import os
import re
import select
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

_TRUE_VALUES = {"1", "true", "yes", "on", "force"}
_FALSE_VALUES = {"0", "false", "no", "off", "disable", "disabled"}
_KNOWN_TERMINALS = (
    "sixel", "mlterm", "wezterm", "mintty", "contour", "yaft",
    "domterm", "xterm-sixel", "st-sixel", "windows_terminal",
)


def _parse_primary_da(response: bytes) -> Optional[bool]:
    """Parse a DEC Primary Device Attributes response.

    Sixel-capable terminals advertise parameter 4, for example
    ``ESC[?62;4;...c``.  ``None`` means no valid response was present.
    """
    matches = re.findall(rb"\x1b\[\?([0-9;]+)c", response)
    if not matches:
        return None
    params = {item for match in matches for item in match.split(b";")}
    return b"4" in params


def _query_terminal(timeout: float = 0.35) -> Optional[bool]:
    """Ask the attached terminal whether it advertises Sixel support."""
    if os.name != "posix" or not sys.stdin.isatty() or not sys.stdout.isatty():
        return None
    try:
        import termios

        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    except (ImportError, OSError):
        return None

    old = None
    data = bytearray()
    try:
        old = termios.tcgetattr(fd)
        raw = termios.tcgetattr(fd)
        raw[3] &= ~(termios.ICANON | termios.ECHO)
        raw[6][termios.VMIN] = 0
        raw[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, raw)
        termios.tcflush(fd, termios.TCIFLUSH)
        os.write(fd, b"\x1b[c")

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            readable, _, _ = select.select([fd], [], [], max(0.0, deadline - time.monotonic()))
            if not readable:
                break
            try:
                chunk = os.read(fd, 256)
            except BlockingIOError:
                continue
            if not chunk:
                break
            data.extend(chunk)
            if b"c" in chunk:
                parsed = _parse_primary_da(bytes(data))
                if parsed is not None:
                    return parsed
        return _parse_primary_da(bytes(data))
    except (OSError, termios.error):
        return None
    finally:
        if old is not None:
            try:
                termios.tcsetattr(fd, termios.TCSANOW, old)
            except (OSError, termios.error):
                pass
        os.close(fd)


def terminal_supports_sixel() -> Optional[bool]:
    """Return True, False, or None when support cannot be determined."""
    override = os.environ.get("REFINDMGR_SIXEL", "").strip().lower()
    if override in _TRUE_VALUES:
        return True
    if override in _FALSE_VALUES:
        return False

    if os.environ.get("WT_SESSION"):
        return True

    term = " ".join((
        os.environ.get("TERM", ""),
        os.environ.get("TERM_PROGRAM", ""),
    )).lower()
    if any(name in term for name in _KNOWN_TERMINALS):
        return True
    if os.environ.get("TERM", "").lower() in {"", "dumb", "linux"}:
        return False
    queried = _query_terminal()
    if queried is True:
        return True
    # Some terminals render Sixel correctly but omit parameter 4 from DA1.
    # Treat that response as inconclusive instead of rejecting a working
    # terminal; catalog mode will ask once and defaults to trying the preview.
    return None


def detection_status() -> Tuple[str, str]:
    """Return ``ready``, ``unknown``, or ``unavailable`` with a reason."""
    if not sys.stdout.isatty():
        return "unavailable", "bukan terminal interaktif"
    if not shutil.which("img2sixel"):
        return "unavailable", "img2sixel belum tersedia"

    supported = terminal_supports_sixel()
    if supported is True:
        return "ready", ""
    if supported is False:
        return "unavailable", "terminal melaporkan tidak mendukung Sixel"
    return "unknown", "dukungan Sixel tidak dapat dideteksi otomatis"


def availability() -> Tuple[bool, str]:
    """Backward-compatible boolean availability result."""
    status, reason = detection_status()
    return status == "ready", reason


def show(
    path: Path,
    width: int = 640,
    force: bool = False,
    column: Optional[int] = None,
) -> Tuple[bool, str]:
    """Render one image. ``force`` skips only the capability check."""
    if not sys.stdout.isatty():
        return False, "bukan terminal interaktif"
    binary = shutil.which("img2sixel")
    if not binary:
        return False, "img2sixel belum tersedia"
    if not force:
        status, reason = detection_status()
        if status != "ready":
            return False, reason + " (pakai REFINDMGR_SIXEL=1 untuk memaksa)."

    if column is not None:
        sys.stdout.write(f"\x1b[{max(1, int(column))}G")
        sys.stdout.flush()

    try:
        result = subprocess.run(
            [binary, "-w", str(width), str(path)],
            stdout=sys.stdout.buffer,
            stderr=subprocess.PIPE,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"img2sixel gagal: {exc}"
    if result.returncode:
        return False, result.stderr.decode(errors="replace").strip() or "img2sixel gagal."
    sys.stdout.write("\n")
    sys.stdout.flush()
    return True, ""
