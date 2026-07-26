"""Deteksi lokasi folder rEFInd (tempat refind.conf berada)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

# Lokasi umum partisi EFI tempat rEFInd biasa terpasang di berbagai distro.
COMMON_REFIND_DIRS = [
    "/boot/efi/EFI/refind",
    "/boot/EFI/refind",
    "/efi/EFI/refind",
    "/boot/efi/EFI/REFIND",
    "/boot/efi/EFI/Refind",
]

ENV_OVERRIDE = "REFIND_DIR"


def detect_refind_dir(explicit: Optional[str] = None) -> Optional[Path]:
    """Cari folder rEFInd (yang berisi refind.conf).

    Urutan pencarian:
    1. `explicit` (misal dari flag --refind-dir)
    2. Variabel environment REFIND_DIR
    3. Lokasi umum partisi EFI

    Mengembalikan None jika tidak ditemukan sama sekali.
    """
    candidates = []
    if explicit:
        candidates.append(explicit)
    env_val = os.environ.get(ENV_OVERRIDE)
    if env_val:
        candidates.append(env_val)
    # An explicit target that turns out to be wrong must FAIL, not silently
    # retarget the live boot directory. Mounting a spare ESP at /mnt/esp and
    # mistyping the path used to send 'remove' at /boot/efi/EFI/refind instead.
    explicit_requested = bool(candidates)
    # Explicit choices always win.  A managed firmware-compatibility install
    # comes next because it is the instance the firmware actually launches;
    # the conventional EFI/refind directory remains a recovery/source copy.
    for candidate in candidates:
        path = Path(candidate)
        if (path / "refind.conf").is_file():
            return path
    if explicit_requested:
        return None

    try:
        from .firmware_compat import detect_compat_dir

        compat = detect_compat_dir()
        if compat is not None:
            return compat
    except (ImportError, OSError):
        pass

    for candidate in COMMON_REFIND_DIRS:
        path = Path(candidate)
        # Path.is_file() already swallows OSError, so the old try/except here
        # was dead code.
        if (path / "refind.conf").is_file():
            return path

    return None

def refind_conf_path(refind_dir: Path) -> Path:
    return Path(refind_dir) / "refind.conf"


def themes_dir(refind_dir: Path) -> Path:
    return Path(refind_dir) / "themes"
