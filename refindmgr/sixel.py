"""Best-effort Sixel image previews using the standard img2sixel utility."""
from __future__ import annotations
import os, shutil, subprocess, sys
from pathlib import Path


def terminal_supports_sixel() -> bool:
    if os.environ.get("REFINDMGR_SIXEL") == "1":
        return True
    term = (os.environ.get("TERM", "") + " " + os.environ.get("TERM_PROGRAM", "")).lower()
    return any(name in term for name in ("sixel", "mlterm", "wezterm", "foot", "mintty", "contour", "yaft", "windows_terminal"))


def availability() -> tuple[bool, str]:
    if not sys.stdout.isatty():
        return False, "bukan terminal interaktif"
    if not terminal_supports_sixel():
        return False, "terminal tidak terdeteksi mendukung Sixel"
    if not shutil.which("img2sixel"):
        return False, "img2sixel belum tersedia"
    return True, ""


def show(path: Path, width: int = 640) -> tuple[bool, str]:
    ready, reason = availability()
    if not ready:
        return False, reason + " (pakai REFINDMGR_SIXEL=1 untuk memaksa deteksi terminal)."
    binary = shutil.which("img2sixel")
    result = subprocess.run([binary, "-w", str(width), str(path)], stdout=sys.stdout.buffer, stderr=subprocess.PIPE, timeout=30)
    if result.returncode:
        return False, result.stderr.decode(errors="replace").strip() or "img2sixel gagal."
    sys.stdout.write("\n")
    return True, ""
