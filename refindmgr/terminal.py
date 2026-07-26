"""Terminal capability probing that survives ``sudo``.

Every previous detection path in refindmgr read ``os.environ``: ``TERM_PROGRAM``,
``VTE_VERSION``, ``WT_SESSION``, ``TMUX``, ``STY``.  The documented way to run
this tool is ``sudo refindmgr``, and the default sudoers policy is ``env_reset``
-- it keeps only a short allowlist (PATH, DISPLAY, LS_COLORS, ...) plus a few
checked variables (TERM, LANG, LC_*, COLORTERM).  Every variable the detection
relied on is therefore gone by the time the code runs, which is why previews
never appeared and why chafa's own auto-detection silently degraded to 16-colour
ANSI blocks.

The fix is to ask the terminal instead of the environment.  Escape-sequence
queries go through /dev/tty and work perfectly under sudo.

One combined probe is written, terminated by DA1.  DA1 is answered by every
terminal and, because responses come back in order, it always arrives last --
so it acts as a sentinel and removes the timeout guesswork that used to leak
stray escape bytes into the interactive menu prompt.
"""
from __future__ import annotations

import os
import re
import select
import sys
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

# Kitty graphics support query.  A supporting terminal answers ESC_Gi=31;OK ESC\
_KITTY_QUERY = b"\x1b_Gi=31,s=1,v=1,a=q,t=d,f=24;AAAA\x1b\\"
# XTVERSION: DCS > | <name and version> ST -- identifies terminals that have no
# capability query of their own (the iTerm2 image protocol has none).
_XTVERSION_QUERY = b"\x1b[>0q"
# XTWINOPS 16: report character cell size.  Answered as CSI 6 ; height ; width t
# Needed because thumbnails are sized in cells but the encoders work in pixels;
# guessing the cell geometry is what made previews come out squashed and tiny.
_CELLSIZE_QUERY = b"\x1b[16t"
# DA1 sentinel.  Parameter 4 in the reply advertises Sixel.
_DA1_QUERY = b"\x1b[c"

PROBE = _KITTY_QUERY + _XTVERSION_QUERY + _CELLSIZE_QUERY + _DA1_QUERY

_DA1_RE = re.compile(rb"\x1b\[\?([0-9;]+)c")
_KITTY_OK_RE = re.compile(rb"\x1b_Gi=31;OK\x1b\\")
_XTVERSION_RE = re.compile(rb"\x1bP>\|([^\x1b\x07]*)(?:\x1b\\|\x07)")
_CELLSIZE_RE = re.compile(rb"\x1b\[6;(\d+);(\d+)t")

# Fallback cell geometry when neither TIOCGWINSZ nor XTWINOPS answers.  A
# typical 15px monospace cell is about 8x19; assuming a square cell (what chafa
# does when its output is a pipe) squashes every image.
DEFAULT_CELL_PIXELS = (8, 19)

# Terminals whose font is a fixed 256/512-glyph set: no sextants, no braille,
# no block elements beyond a handful.  The Linux virtual console is the case
# that matters -- it is what a headless Ubuntu Server actually shows.
_TEXT_ONLY_TERMS = ("linux", "vt100", "vt102", "vt220", "ansi", "dumb", "cons25")

# Terminals known to implement the iTerm2 inline-image protocol.  Matched
# against the XTVERSION name, which is available under sudo.
_ITERM_TERMINALS = (
    "wezterm", "konsole", "mintty", "iterm2", "rio", "ghostty", "tabby",
    "vscode", "hyper", "warpterminal",
)

# Terminals that answer XTVERSION but whose Sixel support is reliable even when
# DA1 is relayed through a multiplexer that strips parameters.
_SIXEL_TERMINALS = (
    "foot", "contour", "mlterm", "wezterm", "konsole", "ghostty", "xterm",
)

_TRUE_VALUES = {"1", "true", "yes", "on", "force"}
_FALSE_VALUES = {"0", "false", "no", "off", "disable", "disabled"}


@dataclass
class TerminalCapabilities:
    """What the attached terminal actually told us about itself."""

    kitty_graphics: bool = False
    sixel: bool = False
    iterm_images: bool = False
    name: str = ""
    responded: bool = False
    is_tty: bool = True
    multiplexer: str = ""          # "tmux", "screen" or ""
    passthrough_ok: bool = True    # False when tmux allow-passthrough is off
    cell_pixels: Optional[Tuple[int, int]] = None   # (width, height) per cell
    colors: int = 256              # 16, 256 or 16777216
    rich_glyphs: bool = True       # False on fixed-font consoles
    notes: List[str] = field(default_factory=list)

    @property
    def any_graphics(self) -> bool:
        return self.kitty_graphics or self.sixel or self.iterm_images

    @property
    def cell(self) -> Tuple[int, int]:
        """Cell geometry in pixels, always usable."""
        return self.cell_pixels or DEFAULT_CELL_PIXELS


def detect_color_depth() -> int:
    """How many colours the terminal can actually show.

    Character art at 16 colours looks like coarse blocks; at truecolor it looks
    like a photograph.  Getting this wrong in either direction is what made the
    symbol fallback unusable.
    """
    if os.environ.get("COLORTERM", "").strip().lower() in {"truecolor", "24bit"}:
        return 16777216
    term = os.environ.get("TERM", "").lower()
    if any(name in term for name in _TEXT_ONLY_TERMS):
        return 16
    if "256color" in term or "direct" in term:
        return 256
    if not term or term == "dumb":
        return 16
    return 256


def detect_rich_glyphs() -> bool:
    """Whether the terminal can draw sextants/blocks rather than plain ASCII.

    The Linux virtual console renders from a fixed font of 256 (or 512) glyphs.
    Sending it sextants (U+1FB00 block, Unicode 13) produces substitution
    characters -- the diamonds and hashes that make a preview unreadable on a
    headless server.
    """
    term = os.environ.get("TERM", "").lower()
    if any(term == name or term.startswith(name + "-") for name in _TEXT_ONLY_TERMS):
        return False
    encoding = (getattr(sys.stdout, "encoding", "") or "").lower()
    return "utf" in encoding


def detect_multiplexer() -> str:
    """Identify tmux/screen without relying on $TMUX or $STY.

    sudo strips both variables, which is why the old passthrough branches never
    ran and raw Sixel bytes were dumped into the pane.  ``TERM`` survives sudo
    (it is on the env_check list), and multiplexers set it to screen*/tmux*.
    """
    if os.environ.get("TMUX"):
        return "tmux"
    if os.environ.get("STY"):
        return "screen"
    term = os.environ.get("TERM", "").lower()
    if term.startswith("tmux"):
        return "tmux"
    if term.startswith("screen"):
        # GNU screen and tmux both use screen-*; tmux additionally exports TMUX,
        # already handled above, so treat a bare screen-* as GNU screen.
        return "screen"
    return ""


def _tmux_passthrough_enabled() -> Optional[bool]:
    """tmux >= 3.3 drops DCS passthrough unless allow-passthrough is on."""
    import shutil
    import subprocess

    binary = shutil.which("tmux")
    if not binary:
        return None
    try:
        result = subprocess.run(
            [binary, "show", "-gv", "allow-passthrough"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode:
        return None
    value = (result.stdout or "").strip().lower()
    if value in {"on", "all"}:
        return True
    if value in {"off", ""}:
        return False
    return None


def cell_pixel_size(fd: Optional[int] = None) -> Optional[Tuple[int, int]]:
    """Return the pixel size of one character cell, or None.

    The catalog used to assume a fixed 8px cell, which is wrong on HiDPI and
    with most non-default fonts, so image widths never lined up with the
    reserved columns.
    """
    try:
        import fcntl
        import struct
        import termios
    except ImportError:
        return None
    for candidate in ([fd] if fd is not None else []) + [
        getattr(sys.stdout, "fileno", lambda: None)(), 0, 1, 2
    ]:
        if candidate is None:
            continue
        try:
            packed = fcntl.ioctl(candidate, termios.TIOCGWINSZ, b"\x00" * 8)
        except (OSError, ValueError):
            continue
        rows, cols, xpixel, ypixel = struct.unpack("HHHH", packed)
        if rows and cols and xpixel and ypixel:
            width = xpixel // cols
            height = ypixel // rows
            if width > 0 and height > 0:
                return (width, height)
    return None


def _read_probe_response(fd: int, timeout: float) -> bytes:
    """Read until the DA1 sentinel arrives or the deadline passes."""
    data = bytearray()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            readable, _, _ = select.select([fd], [], [], remaining)
        except (OSError, ValueError):
            break
        if not readable:
            break
        try:
            chunk = os.read(fd, 4096)
        except BlockingIOError:
            continue
        except OSError:
            break
        if not chunk:
            break
        data.extend(chunk)
        if _DA1_RE.search(bytes(data)):
            break
    return bytes(data)


def probe(timeout: float = 2.0) -> TerminalCapabilities:
    """Ask the terminal what it supports.  Never raises."""
    caps = TerminalCapabilities()
    caps.multiplexer = detect_multiplexer()

    override = os.environ.get("REFINDMGR_PREVIEW", "").strip().lower()
    if override in _FALSE_VALUES or override == "none":
        caps.notes.append("preview dimatikan lewat REFINDMGR_PREVIEW")
        return caps

    caps.colors = detect_color_depth()
    caps.rich_glyphs = detect_rich_glyphs()
    if not sys.stdout.isatty() or not sys.stdin.isatty():
        caps.is_tty = False
        caps.notes.append("bukan terminal interaktif")
        return caps

    caps.cell_pixels = cell_pixel_size()
    caps.colors = detect_color_depth()
    caps.rich_glyphs = detect_rich_glyphs()

    if caps.multiplexer == "tmux":
        enabled = _tmux_passthrough_enabled()
        if enabled is False:
            caps.passthrough_ok = False
            caps.notes.append(
                "tmux allow-passthrough mati; jalankan "
                "'tmux set -g allow-passthrough on' agar gambar bisa tampil"
            )

    if os.name != "posix":
        return caps

    try:
        import termios
    except ImportError:
        return caps

    try:
        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError:
        caps.notes.append("tidak dapat membuka /dev/tty")
        return caps

    old = None
    try:
        old = termios.tcgetattr(fd)
        raw = termios.tcgetattr(fd)
        raw[3] &= ~(termios.ICANON | termios.ECHO)
        raw[6][termios.VMIN] = 0
        raw[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, raw)

        payload = PROBE
        if caps.multiplexer == "tmux" and caps.passthrough_ok:
            # The Kitty query is a DCS-class sequence and needs wrapping, but
            # DA1/XTVERSION must stay unwrapped so tmux answers them itself.
            payload = wrap_passthrough(_KITTY_QUERY, "tmux") + _XTVERSION_QUERY + _DA1_QUERY

        _write_all(fd, payload)
        response = _read_probe_response(fd, timeout)
    except (OSError, termios.error):
        response = b""
    finally:
        if old is not None:
            try:
                termios.tcsetattr(fd, termios.TCSANOW, old)
            except (OSError, termios.error):
                pass
        # Drain anything that arrived late so a straggling DA1 reply can never
        # be consumed as the user's menu choice.
        try:
            termios.tcflush(fd, termios.TCIFLUSH)
        except (OSError, termios.error):
            pass
        os.close(fd)

    return _interpret(response, caps)


def _interpret(response: bytes, caps: TerminalCapabilities) -> TerminalCapabilities:
    if not response:
        caps.notes.append("terminal tidak menjawab probe kapabilitas")
        return caps
    caps.responded = True

    if _KITTY_OK_RE.search(response):
        caps.kitty_graphics = True

    version = _XTVERSION_RE.search(response)
    if version:
        caps.name = version.group(1).decode("ascii", errors="replace").strip()

    cell = _CELLSIZE_RE.search(response)
    if cell:
        height, width = int(cell.group(1)), int(cell.group(2))
        if 0 < width < 100 and 0 < height < 200:
            caps.cell_pixels = (width, height)

    da1 = _DA1_RE.search(response)
    conformance = None
    if da1:
        fields = da1.group(1).split(b";")
        params = set(fields)
        if b"4" in params:
            caps.sixel = True
        if fields and fields[0].isdigit():
            conformance = int(fields[0])

    lowered = caps.name.lower()
    if lowered:
        if any(item in lowered for item in _ITERM_TERMINALS):
            caps.iterm_images = True
        if not caps.sixel and any(item in lowered for item in _SIXEL_TERMINALS):
            # A multiplexer can rewrite DA1 and drop parameter 4 even though the
            # outer terminal renders Sixel fine.
            if caps.multiplexer:
                caps.sixel = True

    # kitty itself deliberately does not implement Sixel; never claim otherwise.
    if "kitty" in lowered:
        caps.kitty_graphics = True

    # Second, TERM-independent signal for a fixed-font console. The Linux
    # virtual console identifies itself as a VT102 (CSI ?6c) and does not
    # answer XTVERSION at all, whereas every terminal with a real font reports
    # conformance level 62+ or gives its name. Requiring BOTH conditions keeps
    # tmux (which answers CSI ?1;2c) out of this branch.
    if (
        conformance is not None
        and conformance <= 6
        and not caps.name
        and not caps.multiplexer
        and not caps.any_graphics
    ):
        caps.rich_glyphs = False
        caps.colors = min(caps.colors, 16)

    return caps


def wrap_passthrough(payload: bytes, multiplexer: str) -> bytes:
    """Wrap a DCS/APC graphics payload for tmux or GNU screen.

    Both multiplexers require every ESC inside the payload to be doubled.  The
    old screen branch omitted the doubling, so screen's DCS parser terminated on
    the payload's own string terminator and the image was truncated while the
    trailing ESC \\ leaked to the outer terminal as garbage.
    """
    if multiplexer == "tmux":
        return b"\x1bPtmux;" + payload.replace(b"\x1b", b"\x1b\x1b") + b"\x1b\\"
    if multiplexer == "screen":
        return b"\x1bP" + payload.replace(b"\x1b", b"\x1b\x1b") + b"\x1b\\"
    return payload


def _write_all(fd: int, payload: bytes) -> None:
    """os.write is not guaranteed to write everything in one call.

    A partial write truncated the graphics stream mid-image and left the
    terminal parsing an unterminated DCS, swallowing the menu text that
    followed.
    """
    view = memoryview(payload)
    while view:
        try:
            written = os.write(fd, view)
        except BlockingIOError:
            time.sleep(0.005)
            continue
        if written <= 0:
            break
        view = view[written:]


def write_stdout(payload: bytes) -> None:
    """Write raw bytes to stdout, handling partial writes."""
    try:
        fd = sys.stdout.fileno()
    except (AttributeError, OSError, ValueError):
        fd = None
    if fd is None:
        buffer = getattr(sys.stdout, "buffer", None)
        if buffer is None:
            sys.stdout.write(payload.decode("latin-1"))
            sys.stdout.flush()
            return
        buffer.write(payload)
        buffer.flush()
        return
    sys.stdout.flush()
    _write_all(fd, payload)
