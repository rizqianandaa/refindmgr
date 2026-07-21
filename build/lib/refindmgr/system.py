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
import re
import shutil
import subprocess
from typing import Callable, List, NamedTuple, Optional, Tuple


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


# rEFInd 0.14.2 dan yang lebih baru punya bug upstream yang dilaporkan luas oleh
# komunitas (SourceForge, ArchLinux BBS #294716, dll.): opsi 'showtools' berhenti
# berfungsi dengan benar -- baris tools di layar boot tetap menampilkan semua ikon
# (kadang malah dobel) walau 'showtools' sudah diatur ke daftar terbatas. Ini
# membuat 'refindmgr declutter' tidak terlihat efeknya di boot meski refind.conf
# sudah benar. Versi 0.14.1 belum terkena bug ini, jadi refindmgr menjadikannya
# versi target sampai ada perbaikan resmi dari proyek rEFInd.
TARGET_REFIND_VERSION = "0.14.1"

# Command untuk membaca versi paket 'refind' yang sudah terpasang, per package
# manager. Semua bersifat baca-saja (aman dijalankan kapan pun, termasuk saat
# 'setup' dipanggil tanpa --yes).
_VERSION_QUERY_COMMANDS = {
    "apt": ["dpkg-query", "-W", "-f=${Version}\n", "refind"],
    "dnf": ["rpm", "-q", "--qf", "%{VERSION}\n", "refind"],
    "zypper": ["rpm", "-q", "--qf", "%{VERSION}\n", "refind"],
    "pacman": ["pacman", "-Q", "refind"],
}

# Command untuk melihat daftar versi paket 'refind' yang TERSEDIA di repo (bukan
# yang terpasang), per package manager. pacman sengaja tidak disertakan --
# repo Arch Linux resmi tidak menyimpan versi lama, jadi pinning otomatis lewat
# pacman tidak bisa dijamin aman (lihat pin_refind_version).
_AVAILABLE_VERSIONS_COMMANDS = {
    "apt": ["apt-cache", "madison", "refind"],
    "dnf": ["dnf", "--showduplicates", "list", "refind"],
    "zypper": ["zypper", "--no-refresh", "search", "-s", "--match-exact", "refind"],
}

# Command untuk memasang persis satu versi (string versi lengkap termasuk
# revisi distro, hasil dari find_available_version) lewat masing-masing package
# manager. Dipakai baik untuk downgrade maupun upgrade -- kedua arah memakai
# command yang sama untuk apt/dnf/zypper (bukan verb 'downgrade' terpisah),
# karena permintaan versi eksplisit sudah cukup untuk memicu keduanya.
_PIN_INSTALL_COMMANDS = {
    "apt": lambda version: ["apt-get", "install", "-y", "--allow-downgrades", f"refind={version}"],
    "dnf": lambda version: ["dnf", "install", "-y", f"refind-{version}"],
    "zypper": lambda version: ["zypper", "install", "-y", "--oldpackage", f"refind={version}"],
}


def _normalize_version(raw: str) -> str:
    """Ambil pola X.Y[.Z...] di awal string, buang revisi distro (mis. '-2.1',
    '.fc40', atau nama paket seperti 'refind.x86_64' di depannya)."""
    match = re.search(r"(\d+(?:\.\d+)+)", raw.strip())
    return match.group(1) if match else raw.strip()


def version_tuple(version: str) -> Tuple[int, ...]:
    """Ubah string versi ternormalisasi ('0.14.1') jadi tuple untuk dibandingkan
    ('>' , '<', '==') secara numerik, bukan leksikografis (supaya '0.14.2' > '0.14.10'
    dibandingkan dengan benar, bukan seperti membandingkan string biasa)."""
    return tuple(int(part) for part in _normalize_version(version).split("."))


def get_installed_refind_version(
    manager: PackageManagerInfo,
    run_fn: Callable[..., "subprocess.CompletedProcess"] = subprocess.run,
) -> Optional[str]:
    """Versi rEFInd (ternormalisasi, tanpa revisi distro) yang terpasang lewat
    package manager ini saat ini, atau None kalau paket 'refind' belum
    terpasang lewat package manager tersebut. Operasi ini baca-saja (aman
    dipanggil kapan pun, tanpa perlu root atau --yes).
    """
    command = _VERSION_QUERY_COMMANDS.get(manager.name)
    if command is None:
        return None
    result = run_fn(command, capture_output=True, text=True)
    if result.returncode != 0:
        return None
    output = (result.stdout or "").strip()
    if not output:
        return None
    if manager.name == "pacman":
        # Format: "refind 0.14.2-1"
        parts = output.split()
        output = parts[1] if len(parts) > 1 else output
    return _normalize_version(output)


def find_available_version(
    manager: PackageManagerInfo,
    target: str,
    run_fn: Callable[..., "subprocess.CompletedProcess"] = subprocess.run,
) -> Optional[str]:
    """Cari string versi paket LENGKAP (termasuk revisi distro, mis. '0.14.1-1')
    yang tersedia di repo package manager ini dan versi upstream-nya persis sama
    dengan `target` (mis. '0.14.1'). Mengembalikan None kalau repo tidak
    menyediakannya sama sekali -- pin_refind_version SENGAJA tidak pernah
    menebak/memasang versi lain sebagai gantinya, supaya refindmgr tidak diam-diam
    memasang versi yang salah.
    """
    command = _AVAILABLE_VERSIONS_COMMANDS.get(manager.name)
    if command is None:
        return None
    result = run_fn(command, capture_output=True, text=True)
    if result.returncode != 0:
        return None
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        # apt (madison) dan zypper (search -s) memakai format kolom dipisah '|';
        # dnf (list --showduplicates) memakai kolom dipisah spasi.
        tokens = [p.strip() for p in line.split("|")] if manager.name in ("apt", "zypper") else line.split()
        for token in tokens:
            if token[:1].isdigit() and _normalize_version(token) == target:
                return token
    return None


def pin_refind_version(
    manager: PackageManagerInfo,
    target: str = TARGET_REFIND_VERSION,
    run_fn: Callable[..., "subprocess.CompletedProcess"] = subprocess.run,
) -> str:
    """Pasang PERSIS versi `target` dari paket rEFInd lewat package manager ini,
    baik itu berarti upgrade maupun downgrade dari versi yang terpasang sekarang.

    Ini HANYA memasang versi yang benar-benar tersedia di repo resmi package
    manager -- tidak pernah mengunduh/menempatkan binari dari luar mekanisme
    resmi distro (mis. zip rilis upstream langsung ke ESP), supaya status
    dpkg/rpm/pacman tetap konsisten dan sistem tetap aman di-upgrade/downgrade
    lagi nanti lewat package manager yang sama.

    Mengangkat BootstrapError (bukan diam-diam melewatkan) kalau repo tidak
    menyediakan versi target, atau kalau package manager ini tidak didukung
    untuk pinning otomatis (lihat _PIN_INSTALL_COMMANDS).
    """
    if manager.name not in _PIN_INSTALL_COMMANDS:
        raise BootstrapError(
            f"refindmgr belum bisa memasang versi rEFInd tertentu secara otomatis lewat {manager.name}.\n"
            f"Pasang manual versi {target} sesuai distro kamu, atau lihat rilis resmi di:\n"
            "  https://sourceforge.net/projects/refind/files/"
        )
    exact_version = find_available_version(manager, target, run_fn=run_fn)
    if exact_version is None:
        raise BootstrapError(
            f"Repo paket {manager.name} di sistem ini tidak menyediakan rEFInd versi {target}.\n"
            "refindmgr sengaja tidak memasang versi lain sebagai gantinya (bisa merusak status paket).\n"
            f"Pasang manual versi {target} dari rilis resmi rEFInd:\n"
            "  https://sourceforge.net/projects/refind/files/"
        )
    command = _PIN_INSTALL_COMMANDS[manager.name](exact_version)
    result = run_fn(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise BootstrapError(
            f"Gagal memasang rEFInd versi {exact_version} lewat {manager.name}: "
            f"{(result.stderr or '').strip() or (result.stdout or '').strip()}"
        )
    return exact_version


REFIND_DEB_URL_TEMPLATE = 'https://sourceforge.net/projects/refind/files/{version}/refind_{version}-1_amd64.deb/download'


def download_refind_deb(
    version: str,
    dest_path: str,
    run_fn: Callable[..., "subprocess.CompletedProcess"] = subprocess.run,
) -> str:
    """Unduh paket .deb resmi rEFInd untuk `version` langsung dari SourceForge,
    melewati repo apt distro.

    Ini penting karena repo apt banyak distro (termasuk Ubuntu) sering HANYA
    menyediakan rilis rEFInd terbaru, bukan versi lama seperti 0.14.1 yang
    dibutuhkan untuk menghindari bug upstream 'showtools' di 0.14.2+. Kalau
    pin_refind_version() gagal karena repo tidak punya versi target, ini jalur
    cadangan yang dipakai _ensure_refind_version_pinned di cli.py, memakai
    paket .deb resmi yang dirilis proyek rEFInd sendiri di SourceForge (bukan
    binari pihak ketiga).
    """
    url = REFIND_DEB_URL_TEMPLATE.format(version=version)
    result = run_fn(["curl", "-fsSL", "-o", dest_path, url], capture_output=True, text=True)
    if result.returncode != 0 or not os.path.exists(dest_path) or os.path.getsize(dest_path) == 0:
        raise BootstrapError(
            f"Gagal mengunduh paket rEFInd {version} dari SourceForge ({url}): "
            f"{(result.stderr or '').strip() or (result.stdout or '').strip() or 'file kosong/tidak ditemukan'}"
        )
    return dest_path


def install_deb_file(
    path: str,
    run_fn: Callable[..., "subprocess.CompletedProcess"] = subprocess.run,
) -> str:
    """Pasang file .deb rEFInd yang sudah diunduh langsung lewat dpkg.

    Kalau dpkg gagal karena dependency yang belum terpasang, otomatis coba
    'apt-get install -f' sekali lalu ulangi dpkg -- pola standar Debian/Ubuntu
    untuk memasang .deb lokal.
    """
    result = run_fn(["dpkg", "-i", path], capture_output=True, text=True)
    if result.returncode == 0:
        return result.stdout or ""
    run_fn(["apt-get", "install", "-f", "-y"], capture_output=True, text=True)
    retry = run_fn(["dpkg", "-i", path], capture_output=True, text=True)
    if retry.returncode != 0:
        raise BootstrapError(
            "Gagal memasang paket .deb rEFInd: "
            f"{(retry.stderr or '').strip() or (result.stderr or '').strip() or (result.stdout or '').strip()}"
        )
    return retry.stdout or ""


def is_root() -> bool:
    return os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0


def esp_root_from_refind_dir(refind_dir) -> Optional["__import__('pathlib').Path"]:
    """Tebak root ESP dari lokasi folder rEFInd, misal '.../EFI/refind' -> '...'.

    Dipakai oleh audit loader ('doctor') untuk tahu dari mana harus mulai
    menjelajah file *.efi lain di ESP yang sama. Mengembalikan None kalau
    struktur foldernya tidak seperti yang diharapkan (bukan '.../EFI/<nama>'),
    supaya pemanggilnya bisa melewati audit ini dengan aman daripada menebak
    lokasi yang salah.
    """
    from pathlib import Path

    refind_dir = Path(refind_dir)
    efi_dir = refind_dir.parent
    if efi_dir.name.upper() != "EFI":
        return None
    return efi_dir.parent


def list_esp_loader_files(refind_dir) -> List[str]:
    """Jelajahi seluruh ESP (dari folder EFI di atas refind_dir) untuk semua
    file '*.efi', kecuali yang ada di dalam folder rEFInd sendiri dan
    'EFI/tools' -- dua folder yang menurut dokumentasi resmi rEFInd memang
    TIDAK PERNAH dipindai untuk entri OS (jadi tidak relevan untuk audit ini).

    Mengembalikan path relatif terhadap root ESP (memakai '/' di semua OS),
    diurutkan, supaya mudah dibaca dan dibandingkan dengan 'dont_scan_files'.
    Mengembalikan list kosong (bukan error) kalau root ESP tidak bisa ditebak
    atau tidak bisa dibaca -- audit ini bersifat best-effort/informational.
    """
    from pathlib import Path

    esp_root = esp_root_from_refind_dir(refind_dir)
    if esp_root is None or not esp_root.is_dir():
        return []
    refind_dir = Path(refind_dir).resolve()
    tools_dir = (esp_root / "EFI" / "tools").resolve()
    results = []
    try:
        for path in esp_root.rglob("*"):
            if not path.is_file() or path.suffix.lower() != ".efi":
                continue
            resolved = path.resolve()
            if resolved == refind_dir or refind_dir in resolved.parents:
                continue
            if resolved == tools_dir or tools_dir in resolved.parents:
                continue
            results.append(path.relative_to(esp_root).as_posix())
    except OSError:
        return []
    return sorted(results)


def find_boot_kernel_files(boot_dir: str = "/boot") -> List[str]:
    """Cari file kernel Linux mentah (vmlinuz*/bzImage*/kernel*) dan
    'refind_linux.conf' di direktori /boot pada sistem yang sedang berjalan.

    Kenapa ini perlu, terpisah dari audit ESP: dokumentasi resmi rEFInd
    menyebutkan bahwa proses scan juga menjelajahi direktori 'boot' di setiap
    filesystem yang bisa diakses -- bukan cuma ESP. Di banyak instalasi
    Debian/Ubuntu, /boot ada di partisi/filesystem Linux (ext4) yang BERBEDA
    dari ESP (FAT), jadi tidak ikut ter-audit oleh list_esp_loader_files().
    Selain itu, dokumentasi juga menyebutkan bahwa 'scan_all_linux_kernels
    false' TETAP menampilkan sebuah kernel jika ada file 'refind_linux.conf'
    yang menyertainya -- karena itu dianggap sebagai tanda niat eksplisit agar
    rEFInd menangani kernel itu secara langsung. Jadi ikon penguin/Tux yang
    masih muncul walau 'scan_all_linux_kernels false' sudah diset kemungkinan
    besar berasal dari sini, bukan dari bug pada opsi itu sendiri.

    Mengembalikan list kosong (bukan error) kalau /boot tidak ada atau tidak
    bisa dibaca -- fungsi ini bersifat best-effort/informational.
    """
    from pathlib import Path

    root = Path(boot_dir)
    if not root.is_dir():
        return []
    prefixes = ("vmlinuz", "bzimage", "kernel")
    results = []
    try:
        for path in root.iterdir():
            if not path.is_file():
                continue
            lname = path.name.lower()
            if lname == "refind_linux.conf" or lname.startswith(prefixes):
                results.append(path.name)
    except OSError:
        return []
    return sorted(results)


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
