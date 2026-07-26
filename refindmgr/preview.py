"""Layered terminal image previews: Kitty -> iTerm2 -> Sixel -> chafa symbols.

Sixel alone was never enough on Linux.  The two terminals most desktop users
actually run (GNOME Terminal and anything else built on VTE, plus Alacritty)
do not implement it, while kitty deliberately implements its own protocol
instead.  Restricting previews to Sixel therefore excluded both ends of the
market at once.

Two of the four backends need no external program at all:

* Kitty graphics ``f=100`` takes a **raw PNG file** and lets the terminal decode
  it.
* The iTerm2 protocol takes the **raw file bytes** in any common format.

Both are just ``base64.b64encode(path.read_bytes())``, so the highest quality
tiers work on a machine with no image tooling installed.  chafa (or img2sixel)
is only needed for the two lower tiers.

The symbol fallback is deliberately driven with explicit flags.  chafa's own
auto-detection reads environment variables that ``sudo`` deletes, so it silently
fell back to a 16-colour ANSI palette -- the blocky output that made the symbol
mode look unusable.  Forcing ``--colors full`` with sextant symbols gives 24-bit
colour at 2x3 sub-cell resolution instead.
"""
from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from . import framebuffer as fb_mod
from . import imaging
from . import terminal as term_mod

# Order matters: best quality first. "framebuffer" sits above "chafa" because
# it draws real pixels; it only ever applies on a console, where none of the
# terminal protocols exist.
BACKENDS = ("kitty", "iterm", "sixel", "framebuffer", "chafa")
_ENV_OVERRIDE = "REFINDMGR_PREVIEW"
_RENDER_TIMEOUT = 30

_CHUNK = 4096  # kitty: max base64 payload per escape sequence


@dataclass
class PreviewEngine:
    """A resolved preview strategy for the current terminal."""

    backend: str = "none"
    reason: str = ""
    caps: Optional[term_mod.TerminalCapabilities] = None
    renderer: str = ""          # external binary used, when any
    symbols: str = ""           # "", "unicode" or "ascii" (user override)
    screen: object = None       # framebuffer ScreenInfo, when applicable

    @property
    def available(self) -> bool:
        return self.backend != "none"

    @property
    def is_graphical(self) -> bool:
        """True when real pixels are drawn rather than character art."""
        return self.backend in {"kitty", "iterm", "sixel", "framebuffer"}


def _forced_backend() -> Optional[str]:
    value = os.environ.get(_ENV_OVERRIDE, "").strip().lower()
    if value in {"kitty", "iterm", "iterm2", "sixel", "sixels", "framebuffer", "fb",
                 "chafa", "symbols", "none"}:
        return {"iterm2": "iterm", "sixels": "sixel", "symbols": "chafa",
                "fb": "framebuffer"}.get(value, value)
    # Backward compatibility with the old on/off switch.
    legacy = os.environ.get("REFINDMGR_SIXEL", "").strip().lower()
    if legacy in {"0", "false", "no", "off", "disable", "disabled"}:
        return "none"
    if legacy in {"1", "true", "yes", "on", "force"}:
        return "sixel"
    return None


def _chafa() -> Optional[str]:
    return shutil.which("chafa")


def _img2sixel() -> Optional[str]:
    return shutil.which("img2sixel")


def resolve(caps: Optional[term_mod.TerminalCapabilities] = None,
            requested: Optional[str] = None,
            symbols: Optional[str] = None) -> PreviewEngine:
    """Pick the best backend the terminal can actually display."""
    if caps is None:
        caps = term_mod.probe()

    choice = (requested or "").strip().lower()
    # 'auto' is the CLI default and means "probe and decide", not a backend
    # name. Passing it through made resolve() report an unknown backend and
    # disabled previews entirely.
    if choice in {"auto", ""}:
        choice = _forced_backend()
    if choice == "none":
        return PreviewEngine("none", "preview dinonaktifkan", caps)

    if not caps.is_tty:
        return PreviewEngine("none", "bukan terminal interaktif", caps)

    wanted = (symbols or "").strip().lower()
    wanted = "" if wanted in {"auto", ""} else wanted

    if choice:
        engine = _build(choice, caps, forced=True)
        if engine.available:
            engine.symbols = wanted
            return engine
        return PreviewEngine("none", engine.reason, caps)

    if not caps.passthrough_ok:
        # tmux would silently swallow every graphics payload; character art
        # still works because it is plain text.
        engine = _build("chafa", caps)
        if engine.available:
            engine.reason = "; ".join(caps.notes) or engine.reason
            engine.symbols = wanted
            return engine

    for backend in BACKENDS:
        engine = _build(backend, caps)
        if engine.available:
            engine.symbols = wanted
            return engine

    hint = "pasang chafa untuk preview gambar (mis. 'sudo apt install chafa')"
    if caps.notes:
        hint = "; ".join(caps.notes) + " -- " + hint
    return PreviewEngine("none", hint, caps)


def _build(backend: str, caps: term_mod.TerminalCapabilities, *, forced: bool = False) -> PreviewEngine:
    if backend == "kitty":
        if caps.kitty_graphics or forced:
            return PreviewEngine("kitty", "", caps)
        return PreviewEngine("none", "terminal tidak mendukung protokol grafis Kitty", caps)
    if backend == "iterm":
        if caps.iterm_images or forced:
            return PreviewEngine("iterm", "", caps)
        return PreviewEngine("none", "terminal tidak mendukung protokol gambar iTerm2", caps)
    if backend == "sixel":
        if not (caps.sixel or forced):
            return PreviewEngine("none", "terminal tidak mengiklankan dukungan Sixel", caps)
        binary = _chafa() or _img2sixel()
        if not binary:
            return PreviewEngine("none", "chafa/img2sixel belum tersedia untuk Sixel", caps)
        return PreviewEngine("sixel", "", caps, renderer=binary)
    if backend == "framebuffer":
        # Framebuffer writes bypass the terminal and can overwrite a graphical
        # desktop.  Never infer this from TERM, missing DISPLAY, or glyph
        # support: require an active kernel /dev/ttyN virtual console.
        if not caps.linux_console:
            return PreviewEngine("none", "framebuffer hanya dipakai di Linux VT aktif", caps)
        if caps.any_graphics:
            return PreviewEngine("none", "protokol grafis terminal lebih aman", caps)
        if not fb_mod.available():
            return PreviewEngine("none", "framebuffer tidak dapat diakses", caps)
        screen = fb_mod.probe()
        if screen is None:
            return PreviewEngine("none", "geometri framebuffer tidak terbaca", caps)
        return PreviewEngine("framebuffer", "", caps, screen=screen)
    if backend == "chafa":
        binary = _chafa()
        if not binary:
            return PreviewEngine("none", "chafa belum tersedia", caps)
        return PreviewEngine("chafa", "", caps, renderer=binary)
    return PreviewEngine("none", f"backend preview tidak dikenal: {backend}", caps)


_SYMBOL_ENV = "REFINDMGR_PREVIEW_SYMBOLS"


def chafa_profile(caps, requested: Optional[str] = None) -> Tuple[str, str, str]:
    """Pick a symbol set and colour depth the terminal can actually render.

    A fixed-font console (TERM=linux on a headless server) draws from a 256- or
    512-glyph set: no sextants, no braille, and block elements only if the
    console font happens to carry them.  Sending sextants there produces
    substitution glyphs -- the unreadable hash-and-diamond mess -- so ASCII is
    the default.  It is coarse, but it is a picture rather than noise.

    ``--colors 16/8`` matters just as much: plain ``16`` emits bright
    backgrounds (SGR 100-107) that the Linux console renders inconsistently,
    while ``16/8`` restricts backgrounds to the eight it always supports.
    """
    choice = (requested or os.environ.get(_SYMBOL_ENV, "")).strip().lower()
    rich = getattr(caps, "rich_glyphs", True) if caps is not None else True
    depth = getattr(caps, "colors", 256) if caps is not None else 256

    if choice in {"unicode", "block", "blocks"}:
        rich = True
    elif choice in {"ascii", "plain"}:
        rich = False

    if depth >= 16777216:
        colors = "full"
    elif depth >= 256:
        colors = "256"
    else:
        colors = "16/8"

    if not rich:
        return "ascii+space", colors, "ascii"
    return "sextant+block+space", colors, "block"


def describe(engine: PreviewEngine) -> str:
    if engine.backend == "chafa":
        symbols, colors, _fill = chafa_profile(engine.caps, engine.symbols)
        depth = {"full": "truecolor", "256": "256 warna", "16/8": "16 warna"}.get(colors, colors)
        style = "ASCII" if symbols.startswith("ascii") else "blok Unicode"
        return f"karakter chafa ({style}, {depth})"
    if engine.backend == "framebuffer" and engine.screen is not None:
        info = engine.screen
        return (f"framebuffer konsol {info.width}x{info.height} "
                f"{info.bits_per_pixel}bpp (gambar asli)")
    labels = {
        "kitty": "protokol grafis Kitty",
        "iterm": "protokol gambar iTerm2",
        "sixel": "Sixel",
        "framebuffer": "framebuffer konsol (gambar asli)",
    }
    return labels.get(engine.backend, "tidak ada")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render(
    engine: PreviewEngine,
    path: Path,
    *,
    columns: int,
    rows: int,
    column: Optional[int] = None,
    row: Optional[int] = None,
    advance: bool = True,
) -> Tuple[bool, str]:
    """Draw one image occupying at most ``columns`` x ``rows`` character cells.

    Sizing is expressed in cells rather than pixels so the terminal (or chafa)
    does the scaling.  The old code passed a fixed 160px width and assumed an
    8px cell, which produced a postage-stamp image that rarely matched the
    reserved rows.

    ``column`` anchors the image at a 1-based screen column so a list can show
    a title on the left and its preview on the right.  Placement is made
    deterministic by saving the cursor (DECSC), drawing, restoring it (DECRC)
    and then advancing exactly ``rows`` lines -- the protocols disagree about
    whether drawing moves the cursor at all, which is what the old fixed
    ``reserve_rows`` fudge factor was working around.
    """
    path = Path(path)
    if not path.is_file():
        return False, f"berkas preview tidak ditemukan: {path.name}"
    columns = max(1, int(columns))
    rows = max(1, int(rows))
    anchored = column is not None

    # The framebuffer draws beside the text rather than through it, so it needs
    # the cell coordinates instead of the cursor.
    if engine.backend == "framebuffer":
        return _render_framebuffer(engine, path, columns, rows, column, row)

    if anchored:
        term_mod.write_stdout(b"\x1b7" + b"\x1b[%dG" % max(1, int(column)))
    try:
        if engine.backend == "kitty":
            ok, note = _render_kitty(engine, path, columns, rows)
        elif engine.backend == "iterm":
            ok, note = _render_iterm(engine, path, columns, rows)
        elif engine.backend == "sixel":
            ok, note = _render_sixel(engine, path, columns, rows)
        elif engine.backend == "chafa":
            ok, note = _render_chafa(engine, path, columns, rows, column=column)
        else:
            ok, note = False, "backend preview tidak tersedia"
    except OSError as exc:
        ok, note = False, f"gagal menggambar preview: {exc}"
    if anchored:
        # Restore to the title line. A single-column list then steps down the
        # exact height used; a grid positions the next cell absolutely instead,
        # which also avoids scrolling the screen at the last row.
        term_mod.write_stdout(b"\x1b8")
        if ok and advance:
            term_mod.write_stdout(b"\r\n" * rows)
    return ok, note


# Decoding and scaling a preview costs about a tenth of a second in pure
# Python; the catalog redraws on every loop, so results are kept.
_SCALED_CACHE: dict = {}
_DRAWN_RECTS: List[Tuple[int, int, int, int]] = []


def _render_framebuffer(engine: PreviewEngine, path: Path, columns: int, rows: int,
                        column: Optional[int], row: Optional[int]) -> Tuple[bool, str]:
    """Write real pixels next to the text on a Linux console."""
    info = engine.screen or fb_mod.probe()
    if info is None:
        return False, "framebuffer tidak tersedia"
    if column is None or row is None:
        return False, "posisi sel tidak diketahui untuk framebuffer"

    source = _png_source(path)
    if source is None:
        return False, "framebuffer memerlukan berkas PNG"

    term_columns, term_rows = terminal_size()
    cell = fb_mod.cell_size(info, term_columns, term_rows)
    if cell is None:
        return False, "ukuran sel konsol tidak dapat dihitung"
    cell_w, cell_h = cell

    box = (columns * cell_w, rows * cell_h)
    key = (str(source), box)
    image = _SCALED_CACHE.get(key)
    if image is None:
        image = imaging.load_scaled(source, box[0], box[1])
        if image is None:
            return False, "gambar tidak dapat didekode"
        _SCALED_CACHE[key] = image

    x = (column - 1) * cell_w
    y = (row - 1) * cell_h
    if not fb_mod.blit(image, x, y, info=info):
        return False, "gagal menulis ke framebuffer"
    _DRAWN_RECTS.append((x, y, image.width, image.height))
    return True, ""


def _png_source(path: Path) -> Optional[Path]:
    """Kitty's f=100 direct transfer accepts PNG only."""
    if path.suffix.lower() == ".png":
        return path
    candidate = path.with_suffix(".png")
    return candidate if candidate.is_file() else None


def _render_kitty(engine: PreviewEngine, path: Path, columns: int, rows: int) -> Tuple[bool, str]:
    source = _png_source(path)
    if source is None:
        return False, "protokol Kitty memerlukan berkas PNG"
    payload = base64.b64encode(source.read_bytes())
    multiplexer = engine.caps.multiplexer if engine.caps else ""

    # a=T transmit+display, f=100 PNG, c/r scale into a cell box while the
    # terminal preserves the aspect ratio, C=1 leaves the cursor where it was.
    chunks = [payload[i:i + _CHUNK] for i in range(0, len(payload), _CHUNK)] or [b""]
    out = bytearray()
    for index, chunk in enumerate(chunks):
        more = b"1" if index < len(chunks) - 1 else b"0"
        if index == 0:
            head = b"a=T,f=100,t=d,C=1,c=%d,r=%d,m=%s" % (columns, rows, more)
        else:
            head = b"m=" + more
        sequence = b"\x1b_G" + head + b";" + chunk + b"\x1b\\"
        out += term_mod.wrap_passthrough(sequence, multiplexer) if multiplexer else sequence
    term_mod.write_stdout(bytes(out))
    return True, ""


def _render_iterm(engine: PreviewEngine, path: Path, columns: int, rows: int) -> Tuple[bool, str]:
    payload = base64.b64encode(path.read_bytes())
    multiplexer = engine.caps.multiplexer if engine.caps else ""
    header = (
        b"\x1b]1337;File=inline=1;preserveAspectRatio=1;doNotMoveCursor=1"
        b";size=%d;width=%d;height=%d:" % (len(payload), columns, rows)
    )
    sequence = header + payload + b"\x07"
    term_mod.write_stdout(term_mod.wrap_passthrough(sequence, multiplexer) if multiplexer else sequence)
    return True, ""


def _run_renderer(command: List[str]) -> Tuple[Optional[bytes], str]:
    try:
        result = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=_RENDER_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"{Path(command[0]).name} gagal: {exc}"
    if result.returncode:
        message = result.stderr.decode(errors="replace").strip()
        return None, message or f"{Path(command[0]).name} gagal."
    return result.stdout or b"", ""


def _image_pixels(path: Path) -> Optional[Tuple[int, int]]:
    """Read a PNG/JPEG header for its dimensions, without an image library."""
    try:
        with path.open("rb") as handle:
            data = handle.read(65536)
    except OSError:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return (int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big"))
    if data[:2] == b"\xff\xd8":
        index = 2
        while index + 9 < len(data):
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3):
                return (int.from_bytes(data[index + 7:index + 9], "big"),
                        int.from_bytes(data[index + 5:index + 7], "big"))
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                index += 2
                continue
            index += 2 + int.from_bytes(data[index + 2:index + 4], "big")
    return None


def fit_pixels(source: Optional[Tuple[int, int]], box: Tuple[int, int]) -> Tuple[int, int]:
    """Largest size inside ``box`` that keeps the source aspect ratio."""
    box_w, box_h = max(1, box[0]), max(1, box[1])
    if not source or source[0] <= 0 or source[1] <= 0:
        return box_w, box_h
    scale = min(box_w / source[0], box_h / source[1])
    return max(1, int(source[0] * scale)), max(1, int(source[1] * scale))


def _render_sixel(engine: PreviewEngine, path: Path, columns: int, rows: int) -> Tuple[bool, str]:
    multiplexer = engine.caps.multiplexer if engine.caps else ""
    # img2sixel is preferred because it takes an exact pixel size. chafa's
    # --size is in cells, and when its stdout is a pipe it cannot ask the
    # terminal how big a cell is, so it assumes a square 8x8 one: a 3-row
    # thumbnail came out 24px tall instead of ~57px, squashed to the wrong
    # aspect ratio as well.
    binary = _img2sixel() or _chafa() or engine.renderer or ""
    if not binary:
        return False, "renderer Sixel belum tersedia"

    cell_w, cell_h = engine.caps.cell if engine.caps else term_mod.DEFAULT_CELL_PIXELS
    target = fit_pixels(_image_pixels(path), (columns * cell_w, rows * cell_h))

    if Path(binary).name == "img2sixel":
        command = [binary, "-w", str(target[0]), "-h", str(target[1]), str(path)]
    else:
        # chafa fallback: convert the pixel target back into the 8x8 cells it
        # assumes, so the result is at least the right size and aspect.
        command = [
            binary, "-f", "sixels",
            "--size", f"{max(1, target[0] // 8)}x{max(1, target[1] // 8)}",
            "--colors", "256", "--dither", "ordered", "--animate", "off",
            "--polite", "on", "--passthrough", "none", str(path),
        ]

    payload, error = _run_renderer(command)
    if payload is None:
        return False, error
    # A zero exit with no DCS payload cannot possibly display anything.
    if not payload or b"\x1bP" not in payload:
        return False, "renderer tidak menghasilkan data Sixel yang valid"

    if multiplexer:
        payload = term_mod.wrap_passthrough(payload, multiplexer)
    term_mod.write_stdout(payload)
    return True, ""


def _render_chafa(engine: PreviewEngine, path: Path, columns: int, rows: int,
                  column: Optional[int] = None) -> Tuple[bool, str]:
    binary = engine.renderer or _chafa()
    if not binary:
        return False, "chafa belum tersedia"

    caps = engine.caps
    symbols, colors, fill = chafa_profile(caps, engine.symbols)
    # Explicit flags only.  chafa's auto-detection reads TERM_PROGRAM /
    # KITTY_WINDOW_ID / WEZTERM_*, all of which sudo removes, so left to itself
    # it degrades to a 16-colour palette and renders coarse blocks.
    command = [
        binary, "-f", "symbols",
        "--symbols", symbols,
        "--colors", colors,
        "--dither", "ordered",
        "--fill", fill,
        "--size", f"{columns}x{rows}",
        "--animate", "off",
        "--polite", "on",
        str(path),
    ]
    payload, error = _run_renderer(command)
    if payload is None:
        # Older chafa builds lack the sextant symbol class; retry with a set
        # every version understands rather than losing the preview entirely.
        fallback = list(command)
        fallback[fallback.index(symbols)] = "block+space"
        payload, error = _run_renderer(fallback)
        if payload is None:
            fallback[fallback.index("block+space")] = "ascii+space"
            payload, error = _run_renderer(fallback)
        if payload is None:
            return False, error
    if not payload:
        return False, "chafa tidak menghasilkan keluaran"
    if column is not None:
        # Character art is plain text, so every one of its lines has to be
        # re-anchored; otherwise line 2 onwards starts at column 1 and paints
        # over the titles in the left-hand list.
        anchor = b"\x1b[%dG" % max(1, int(column))
        lines = payload.split(b"\n")
        while lines and not lines[-1].strip():
            lines.pop()
        payload = b"".join(anchor + line + b"\r\n" for line in lines)
    term_mod.write_stdout(payload)
    return True, ""


def clear_graphics(engine: PreviewEngine) -> None:
    """Remove previously placed images so redraws do not stack them."""
    if engine.backend == "framebuffer":
        # A terminal screen clear only repaints text; pixels written straight
        # to the framebuffer survive it and would linger as garbage.
        info = engine.screen or fb_mod.probe()
        if info is not None and _DRAWN_RECTS:
            fb_mod.fill_rects(_DRAWN_RECTS, info=info)
        _DRAWN_RECTS.clear()
        return
    if engine.backend != "kitty":
        return
    multiplexer = engine.caps.multiplexer if engine.caps else ""
    sequence = b"\x1b_Ga=d,d=A\x1b\\"
    term_mod.write_stdout(
        term_mod.wrap_passthrough(sequence, multiplexer) if multiplexer else sequence
    )


def preview_box(columns_available: int, rows_available: int) -> Tuple[int, int]:
    """Choose a preview size that fits the terminal and keeps the 16:9 ratio."""
    columns = max(20, min(60, columns_available - 6))
    # 16:9 in character cells: a cell is roughly twice as tall as it is wide.
    rows = max(6, min(rows_available - 10, round(columns * 9 / 16 / 2)))
    return columns, rows


IMAGE_ASPECT = 16 / 9
MIN_LIST_ROWS = 3
MAX_LIST_ROWS = 10


def columns_per_row(cell: Optional[Tuple[int, int]] = None) -> float:
    """How many columns a 16:9 image needs per row of height.

    Derived from the real cell geometry rather than assuming a 1:2 cell -- a
    typical 8x19 cell needs 4.2 columns per row, not the 3.6 a square-ish
    assumption predicts, which is why thumbnails came out too narrow.
    """
    cell_w, cell_h = cell or term_mod.DEFAULT_CELL_PIXELS
    if cell_w <= 0 or cell_h <= 0:
        cell_w, cell_h = term_mod.DEFAULT_CELL_PIXELS
    return (cell_h / cell_w) * IMAGE_ASPECT


MAX_GRID_COLUMNS = 4


class GridLayout:
    """Where every catalog thumbnail goes, and how big it is."""

    __slots__ = ("grid_columns", "grid_rows", "cell_width", "image_offset",
                 "box_columns", "box_rows", "gap_rows")

    def __init__(self, grid_columns, grid_rows, cell_width, image_offset,
                 box_columns, box_rows, gap_rows):
        self.grid_columns = grid_columns
        self.grid_rows = grid_rows
        self.cell_width = cell_width
        self.image_offset = image_offset
        self.box_columns = box_columns
        self.box_rows = box_rows
        self.gap_rows = gap_rows

    @property
    def pitch(self) -> int:
        return self.box_rows + self.gap_rows

    @property
    def area(self) -> int:
        return self.box_columns * self.box_rows

    def position(self, index: int) -> Tuple[int, int]:
        """1-based (row, column) of the entry's title cell within the grid."""
        row = index // self.grid_columns
        column = index % self.grid_columns
        return row * self.pitch, column * self.cell_width

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"GridLayout(grid={self.grid_columns}x{self.grid_rows}, "
                f"box={self.box_columns}x{self.box_rows}, gap={self.gap_rows})")


def grid_layout(
    columns_available: int,
    rows_available: int,
    entries: int,
    label_width: int,
    *,
    chrome_rows: int = 4,
    cell: Optional[Tuple[int, int]] = None,
) -> Optional[GridLayout]:
    """Lay the catalog out to make each thumbnail as large as possible.

    A single column wastes most of a wide terminal: the entries stack down the
    left edge and the height budget -- divided by every entry -- is what caps
    the thumbnail size.  Splitting into two or more columns quarters the number
    of grid rows, so each thumbnail can be several times larger while the whole
    catalog still fits on one screen.
    """
    entries = max(1, entries)
    per_row = columns_per_row(cell)
    usable_rows = max(MIN_LIST_ROWS, rows_available - chrome_rows)
    best: Optional[GridLayout] = None

    for grid_columns in range(1, MAX_GRID_COLUMNS + 1):
        grid_rows = -(-entries // grid_columns)          # ceil
        # A separating row is kept whenever the thumbnail still clears the
        # minimum. Spending it on the image instead grows the preview by only
        # about a tenth, and without it two stacked previews in the same column
        # run together with no visible boundary.
        for gap_rows in (1, 0):
            box_rows = min(MAX_LIST_ROWS, usable_rows // grid_rows - gap_rows)
            if box_rows < MIN_LIST_ROWS:
                continue
            box_columns = round(box_rows * per_row)
            cell_width = label_width + 2 + box_columns + 3
            if grid_columns * cell_width > columns_available:
                # Shrink to the width that is actually free, then re-derive the
                # height so the thumbnail keeps its aspect ratio.
                free = columns_available // grid_columns - label_width - 5
                if free < 10:
                    continue
                box_columns = free
                box_rows = min(box_rows, max(MIN_LIST_ROWS, round(box_columns / per_row)))
                if box_rows < MIN_LIST_ROWS:
                    continue
                cell_width = label_width + 2 + box_columns + 3
            candidate = GridLayout(grid_columns, grid_rows, cell_width,
                                   label_width + 2, box_columns, box_rows, gap_rows)
            if best is None or candidate.area > best.area:
                best = candidate
            # gap=1 succeeded for this column count, so never consider gap=0.
            break
    return best


# Ordered by preference. fim is the maintained successor to fbi; mpv's DRM
# output works without root where the others usually need it.
_FRAMEBUFFER_VIEWERS = (
    ("fim", ["-a", "--quiet"]),
    ("fbi", ["-a", "--noverbose", "-1"]),
    ("mpv", ["--vo=drm", "--really-quiet", "--image-display-duration=inf"]),
)


def framebuffer_viewer(
    caps: Optional[term_mod.TerminalCapabilities] = None,
) -> Optional[Tuple[str, List[str]]]:
    """A way to show a REAL image on a console with no graphics protocol.

    No terminal protocol works on the Linux virtual console -- it has no Sixel,
    no Kitty protocol, and a fixed font -- so character art is the best that can
    be drawn inline.  The kernel framebuffer is the way out: fim/fbi/mpv paint
    actual pixels to /dev/fb0 or /dev/dri, including inside a VM console.

    Returns ``None`` under X/Wayland, where the terminal is a normal emulator
    and taking over the framebuffer would be wrong.
    """
    if caps is not None:
        if not caps.linux_console:
            return None
    elif not term_mod.linux_virtual_console_name():
        return None
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        return None
    if not (Path("/dev/fb0").exists() or Path("/dev/dri/card0").exists()):
        return None
    for name, args in _FRAMEBUFFER_VIEWERS:
        binary = shutil.which(name)
        if binary:
            return binary, args
    return None


def show_fullscreen(
    path: Path,
    caps: Optional[term_mod.TerminalCapabilities] = None,
) -> Tuple[bool, str]:
    """Display one image full-screen on the console framebuffer."""
    viewer = framebuffer_viewer(caps)
    if viewer is None:
        return False, "tidak ada penampil framebuffer (pasang 'fim' atau 'fbi')"
    binary, args = viewer
    try:
        result = subprocess.run([binary] + args + [str(path)], timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{Path(binary).name} gagal: {exc}"
    if result.returncode:
        return False, f"{Path(binary).name} keluar dengan kode {result.returncode}"
    return True, ""


def terminal_size() -> Tuple[int, int]:
    size = shutil.get_terminal_size((100, 30))
    return size.columns, size.lines


def supports_unicode() -> bool:
    encoding = (getattr(sys.stdout, "encoding", "") or "").lower()
    return "utf" in encoding
