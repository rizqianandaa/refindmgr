"""Backward-compatible Sixel entry points.

The preview stack now lives in :mod:`refindmgr.preview` and
:mod:`refindmgr.terminal`, which probe the terminal directly instead of reading
environment variables that ``sudo`` deletes, and which fall back through the
Kitty and iTerm2 protocols before reaching Sixel.  This module remains so that
``REFINDMGR_SIXEL`` and any external caller keep working.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Tuple

from . import preview as preview_mod
from . import terminal as terminal_mod

_TRUE_VALUES = {"1", "true", "yes", "on", "force"}
_FALSE_VALUES = {"0", "false", "no", "off", "disable", "disabled"}


def _parse_primary_da(response: bytes) -> Optional[bool]:
    """Parse a DEC Primary Device Attributes response for the Sixel flag."""
    match = terminal_mod._DA1_RE.findall(response)
    if not match:
        return None
    params = {item for group in match for item in group.split(b";")}
    return b"4" in params


def terminal_supports_sixel() -> Optional[bool]:
    """Return True, False, or None when support cannot be determined."""
    override = os.environ.get("REFINDMGR_SIXEL", "").strip().lower()
    if override in _TRUE_VALUES:
        return True
    if override in _FALSE_VALUES:
        return False
    caps = terminal_mod.probe()
    if caps.sixel:
        return True
    if caps.responded:
        return False
    return None


def detection_status() -> Tuple[str, str]:
    """Return ``ready`` or ``unavailable`` with a reason."""
    engine = preview_mod.resolve(requested="sixel")
    if engine.available:
        return "ready", ""
    return "unavailable", engine.reason


def availability() -> Tuple[bool, str]:
    status, reason = detection_status()
    return status == "ready", reason


def show(
    path: Path,
    width: int = 640,
    force: bool = False,
    column: Optional[int] = None,
    reserve_rows: int = 1,
) -> Tuple[bool, str]:
    """Render one image using Sixel.  Kept for backward compatibility."""
    import sys

    if not sys.stdout.isatty():
        return False, "bukan terminal interaktif"
    caps = terminal_mod.probe()
    engine = preview_mod.PreviewEngine("sixel", "", caps) if force else preview_mod.resolve(caps, "sixel")
    if not engine.available:
        return False, engine.reason
    if force and not engine.renderer:
        engine = preview_mod._build("sixel", caps, forced=True)
        if not engine.available:
            return False, engine.reason

    cell = caps.cell_pixels or (8, 17)
    columns = max(1, width // max(1, cell[0]))
    rows = max(1, int(reserve_rows))

    if column is not None:
        import sys as _sys
        _sys.stdout.write(f"\x1b[{max(1, int(column))}G")
        _sys.stdout.flush()

    shown, reason = preview_mod.render(engine, Path(path), columns=columns, rows=rows)
    if not shown:
        return False, reason
    import sys as _sys
    _sys.stdout.write("\n" * max(1, int(reserve_rows)))
    _sys.stdout.flush()
    return True, ""
