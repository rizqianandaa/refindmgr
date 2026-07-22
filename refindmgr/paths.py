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
    candidates.extend(COMMON_REFIND_DIRS)

    for candidate in candidates:
        path = Path(candidate)
        try:
            if (path / "refind.conf").is_file():
                return path
        except OSError:
            continue
        
    return None

def refind_conf_path(refind_dir: Path) -> Path:
    return Path(refind_dir) / "refind.conf"


def themes_dir(refind_dir: Path) -> Path:
    return Path(refind_dir) / "themes"
