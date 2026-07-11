"""Deteksi & bantu instalasi rEFInd itu sendiri (bukan tema) -- dengan hati-hati.

Modul ini SENGAJA tidak menulis apa pun ke partisi EFI atau NVRAM secara
langsung. Semua langkah yang menyentuh boot loader didelegasikan ke skrip
resmi upstream 'refind-install' (bagian dari paket rEFInd), yang sudah diuji
secara luas oleh proyek rEFInd sendiri. Modul ini hanya membantu: deteksi
apakah rEFInd sudah terpasang, deteksi package manager yang tersedia, dan
menjalankan langkah instalasi -- selalu dengan konfirmasi eksplisit dari
pengguna (lihat cli.cmd_setup).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from typing import Callable, List, NamedTuple, Optional


class PackageManagerInfo(NamedTuple):
    name: str
    install_command: List[str]


# Peta package manager -> command untuk memasang paket 'refind'.
# Diperiksa dalam urutan ini (yang pertama cocok dipakai).
_PACKAGE_MANAGERS: List[PackageManagerInfo] = [
    PackageManagerInfo("apt", ["apt-get", "install", "-y", "refind"]),
    PackageManagerInfo("dnf", ["dnf", "install", "-y", "refind"]),
    PackageManagerInfo("pacman", ["pacman", "-S", "--noconfirm", "refind"]),
    PackageManagerInfo("zypper", ["zypper", "install", "-y", "refind"]),
]

_BINARY_FOR_MANAGER = {
    "apt": "apt-get",
    "dnf": "dnf",
    "pacman": "pacman",
    "zypper": "zypper",
}


class BootstrapError(Exception):
    """Kesalahan yang dapat dipahami pengguna terkait instalasi rEFInd itu sendiri."""


def is_root() -> bool:
    return os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0


def is_refind_install_available(
    which_fn: Callable[[str], Optional[str]] = shutil.which,
) -> bool:
    """True jika skrip 'refind-install' resmi (dari paket rEFInd) ada di PATH."""
    return which_fn("refind-install") is not None


def detect_package_manager(
    which_fn: Callable[[str], Optional[str]] = shutil.which,
) -> Optional[PackageManagerInfo]:
    """Deteksi package manager yang tersedia di sistem ini untuk memasang rEFInd."""
    for manager in _PACKAGE_MANAGERS:
        binary = _BINARY_FOR_MANAGER[manager.name]
        if which_fn(binary) is not None:
            return manager
    return None


def install_package(
    manager: PackageManagerInfo,
    run_fn: Callable[..., "subprocess.CompletedProcess"] = subprocess.run,
) -> None:
    """Jalankan command install package rEFInd lewat package manager sistem."""
    result = run_fn(manager.install_command, capture_output=True, text=True)
    if result.returncode != 0:
        raise BootstrapError(
            f"Gagal memasang paket rEFInd lewat {manager.name}: "
            f"{(result.stderr or '').strip() or (result.stdout or '').strip()}"
        )


def run_refind_install(
    run_fn: Callable[..., "subprocess.CompletedProcess"] = subprocess.run,
) -> str:
    """Jalankan skrip resmi 'refind-install' untuk memasang rEFInd ke partisi EFI.

    Ini SATU-SATUNYA fungsi di modul ini yang menyentuh partisi EFI/NVRAM, dan
    ia melakukannya dengan mendelegasikan seluruhnya ke skrip resmi upstream,
    bukan logika buatan sendiri.
    """
    result = run_fn(["refind-install"], capture_output=True, text=True)
    if result.returncode != 0:
        raise BootstrapError(
            f"refind-install gagal: {(result.stderr or '').strip() or (result.stdout or '').strip()}"
        )
    return result.stdout or ""
