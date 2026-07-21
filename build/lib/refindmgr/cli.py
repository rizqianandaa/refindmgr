"""Antarmuka CLI untuk refindmgr."""
from __future__ import annotations

import argparse
import hashlib
import shutil
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

from . import catalog as catalog_mod
from . import conf as conf_mod
from . import system as system_mod
from . import themes as themes_mod
from . import __version__
from .paths import detect_refind_dir, refind_conf_path


class CLIError(Exception):
    """Kegagalan yang harus ditampilkan ke pengguna sebagai pesan biasa.

    Dipakai oleh semua fungsi cmd_* alih-alih memanggil sys.exit() langsung, supaya
    kegagalan validasi/operasi ditangani secara konsisten baik saat dipanggil dari
    CLI langsung (lihat main()) maupun dari menu interaktif (lihat
    run_interactive_menu()), tanpa mengandalkan SystemExit -- yang aslinya
    dimaksudkan untuk menghentikan seluruh proses Python, bukan untuk alur
    kendali di dalam satu sesi menu yang tetap berjalan.
    """


def _refind_dir_arg(args: argparse.Namespace) -> Optional[str]:
    # NOTE: --refind-dir is defined on both the top-level parser and every
    # subparser (via the shared `common` parent) so it can be placed either
    # before or after the subcommand name. To make that combination work with
    # argparse's namespace-merging behavior (which otherwise lets the
    # subparser's default silently overwrite a value already set by the
    # top-level parser), the shared definition uses default=argparse.SUPPRESS,
    # so the attribute is only ever set by whichever parser actually saw the
    # flag on the command line. That means it may be entirely absent here.
    return getattr(args, "refind_dir", None)


def _resolve_refind_dir(args: argparse.Namespace) -> Path:
    refind_dir = detect_refind_dir(_refind_dir_arg(args))
    if refind_dir is None:
        raise CLIError(
            "Tidak menemukan folder rEFInd (refind.conf) secara otomatis.\n"
            "Tentukan lokasinya manual dengan --refind-dir, contoh:\n"
            "  refindmgr --refind-dir /boot/efi/EFI/refind list\n"
            "Belum pernah install rEFInd sama sekali? Coba 'refindmgr setup' dulu.\n"
            "Jalankan 'refindmgr doctor' untuk diagnostik lebih lanjut."
        )
    return refind_dir


def _warn_if_not_root() -> None:
    """Ingatkan pengguna sebelum operasi yang menulis ke partisi EFI, tanpa
    menghentikan proses -- di sandbox/test, izin lokal biasanya cukup."""
    if not system_mod.is_root():
        print(
            "Peringatan: partisi EFI biasanya hanya bisa ditulis oleh root.\n"
            "Jika perintah ini gagal dengan 'Permission denied', ulangi dengan sudo.",
            file=sys.stderr,
        )


def _theme_status(refind_dir: Path) -> tuple:
    """Baca status tema (terpasang & aktif) sekali dari disk.

    Dipakai bersama oleh cmd_list dan _print_status_banner supaya logika
    pembacaan refind.conf tidak terduplikasi di dua tempat berbeda.
    """
    installed = themes_mod.list_installed(refind_dir)
    conf_path = refind_conf_path(refind_dir)
    active_list = conf_mod.get_active_themes(conf_mod.read_lines(conf_path)) if conf_path.is_file() else []
    return installed, active_list


def cmd_list(args: argparse.Namespace) -> None:
    refind_dir = _resolve_refind_dir(args)
    installed, active_list = _theme_status(refind_dir)
    active = active_list[0] if active_list else None

    if not installed:
        print("Belum ada tema terpasang. Coba 'refindmgr catalog' untuk melihat pilihan.")
        return

    print(f"Tema terpasang di {refind_dir / 'themes'}:\n")
    for name in installed:
        marker = "* " if name == active else "  "
        print(f"{marker}{name}")
    if active is None:
        print("\n(Tidak ada tema aktif -- rEFInd memakai tampilan default.)")
    else:
        print(f"\n(* = tema aktif saat ini: {active})")
    if len(active_list) > 1:
        print(
            f"\nPERINGATAN: ditemukan {len(active_list)} baris include tema aktif sekaligus "
            f"di refind.conf ({', '.join(active_list)}). Sebaiknya hanya satu yang aktif -- "
            "jalankan 'refindmgr activate <nama>' untuk merapikannya."
        )


def cmd_catalog(args: argparse.Namespace) -> None:
    print("Katalog tema rEFInd (buka tautan Preview untuk melihat screenshot):\n")
    for index, entry in enumerate(catalog_mod.CATALOG, start=1):
        print(f"  {index}. {entry.name}  [{entry.key}]")
        print(f"     Preview: {entry.git_url}#readme")
    print("\nPasang: refindmgr install <key> --activate")
    print("Tema lain: https://refind-themes-collection.netlify.app/")


def _activate(refind_dir: Path, theme_name: str, include_path: Optional[str] = None) -> None:
    conf_path = refind_conf_path(refind_dir)
    if not conf_path.is_file():
        raise CLIError(f"refind.conf tidak ditemukan di {refind_dir}")
    lines = conf_mod.read_lines(conf_path)
    conf_mod.backup(conf_path)
    # Rosé Pine is documented by its upstream author as a direct child of
    # rEFInd (include rose-pine/theme.conf), not as a themes/ child.
    if include_path:
        new_lines = conf_mod.deactivate_all(lines)
        for i, line in enumerate(new_lines):
            if line.strip().lower() in {"include rose-pine/theme.conf", "include refind-sublime/theme.conf"}:
                new_lines[i] = "# " + line.strip()
        if new_lines and new_lines[-1].strip():
            new_lines.append("")
        new_lines.append(f"include {include_path}")
    else:
        new_lines = conf_mod.activate_theme(lines, theme_name)
        for i, line in enumerate(new_lines):
            if line.strip().lower() in {"include rose-pine/theme.conf", "include refind-sublime/theme.conf"}:
                new_lines[i] = "# " + line.strip()
    # Hide the small volume/disk overlay shown beside OS icons by rEFInd.
    if "hideui badges" not in [line.strip() for line in new_lines]:
        new_lines.append("hideui badges")
    conf_mod.write_lines(conf_path, new_lines)
    print(f"Tema '{theme_name}' sekarang aktif. Backup refind.conf sebelumnya sudah disimpan otomatis.")


def cmd_install(args: argparse.Namespace) -> None:
    refind_dir = _resolve_refind_dir(args)
    _warn_if_not_root()
    source = args.source
    catalog_entry = catalog_mod.find(source)
    if catalog_entry:
        source = catalog_entry.git_url
    try:
        installed_name = themes_mod.install_theme(refind_dir, source, name=args.name, subdir=getattr(args, "subdir", None))
    except themes_mod.ThemeError as exc:
        raise CLIError(f"Gagal memasang tema: {exc}") from exc
    if catalog_entry and catalog_entry.key == "digital-void":
        themes_mod.patch_digital_void_theme(refind_dir / "themes" / installed_name)
    is_rose_pine = bool(catalog_entry and catalog_entry.key == "soho")
    if is_rose_pine:
        old_theme_dir = refind_dir / "themes" / installed_name
        rose_dir = refind_dir / "rose-pine"
        if rose_dir.exists():
            raise CLIError("Folder rose-pine sudah ada di rEFInd. Hapus folder lama itu sebelum memasang ulang.")
        shutil.move(str(old_theme_dir), str(rose_dir))
        installed_name = "rose-pine"
        themes_mod.patch_rose_pine_theme(rose_dir, getattr(args, "color_variant", "main"))
    is_sublime = bool(catalog_entry and catalog_entry.key == "sublime")
    if is_sublime:
        old_theme_dir = refind_dir / "themes" / installed_name
        sublime_dir = refind_dir / "refind-sublime"
        if sublime_dir.exists():
            raise CLIError("Folder refind-sublime sudah ada di rEFInd. Hapus folder lama itu sebelum memasang ulang.")
        shutil.move(str(old_theme_dir), str(sublime_dir))
        installed_name = "refind-sublime"
        themes_mod.patch_sublime_theme(sublime_dir)
    theme_location = (refind_dir / installed_name) if (is_rose_pine or is_sublime) else (refind_dir / "themes" / installed_name)
    print(f"Tema '{installed_name}' berhasil dipasang di {theme_location}")
    if args.activate:
        special_include = "rose-pine/theme.conf" if is_rose_pine else ("refind-sublime/theme.conf" if is_sublime else None)
        _activate(refind_dir, installed_name, include_path=special_include)
    else:
        print(f"Jalankan 'refindmgr activate {installed_name}' untuk mengaktifkannya.")


def cmd_activate(args: argparse.Namespace) -> None:
    refind_dir = _resolve_refind_dir(args)
    _warn_if_not_root()
    try:
        themes_mod.validate_theme_name(args.name)
    except themes_mod.ThemeError as exc:
        raise CLIError(f"Nama tema tidak valid: {exc}") from exc
    installed = themes_mod.list_installed(refind_dir)
    if args.name not in installed:
        raise CLIError(
            f"Tema '{args.name}' belum terpasang. Tema yang tersedia: {', '.join(installed) or '(tidak ada)'}"
        )
    _activate(refind_dir, args.name)


def cmd_deactivate(args: argparse.Namespace) -> None:
    refind_dir = _resolve_refind_dir(args)
    _warn_if_not_root()
    conf_path = refind_conf_path(refind_dir)
    if not conf_path.is_file():
        raise CLIError(f"refind.conf tidak ditemukan di {refind_dir}")
    lines = conf_mod.read_lines(conf_path)
    conf_mod.backup(conf_path)
    new_lines = conf_mod.deactivate_all(lines)
    conf_mod.write_lines(conf_path, new_lines)
    print("Semua tema dinonaktifkan. rEFInd akan memakai tampilan default saat boot berikutnya.")


def cmd_remove(args: argparse.Namespace) -> None:
    refind_dir = _resolve_refind_dir(args)
    _warn_if_not_root()
    try:
        themes_mod.remove_theme(refind_dir, args.name)
    except themes_mod.ThemeError as exc:
        raise CLIError(f"Gagal menghapus tema: {exc}") from exc
    print(f"Tema '{args.name}' telah dihapus.")


# Preset 'declutter': hanya menyisakan Shutdown & Reboot di baris tools (baris
# bawah), dan menjaga baris OS (baris atas) pada metode scan yang paling umum
# (internal/external/optical/manual), tanpa opsi 'firmware' yang menambahkan
# tag boot dari daftar boot firmware -- salah satu sumber tag 'aneh' yang
# sering muncul di layar boot rEFInd, seperti dikeluhkan banyak pengguna.
# Lihat README.md bagian 'Rapikan tampilan boot (declutter)' untuk rincian.
MINIMAL_SHOWTOOLS = "shutdown,reboot"
MINIMAL_SCANFOR = "internal,external,optical,manual"

# Sumber "entri aneh" kedua yang sangat sering dikeluhkan (terpisah dari baris
# tools di atas): rEFInd secara default membuat entri boot TERSENDIRI untuk
# setiap kernel Linux mentah yang ditemukan di /boot (ikon penguin), DAN untuk
# file loader mentah seperti grubx64.efi/fbx64.efi yang sebenarnya cuma
# dipanggil secara internal oleh shim (ikon generik/ketupat) -- di samping
# entri OS yang sudah benar (misalnya "ubuntu", lewat shimx64.efi). Hasilnya:
# satu OS yang sama bisa muncul sampai 3x di baris atas dengan ikon berbeda-
# beda, dan ini akan selalu terjadi lagi di laptop siapa pun yang memakai
# shim+GRUB (Ubuntu, Debian, Fedora, dll.), bukan cuma kasus spesifik.
# - scan_all_linux_kernels false: hentikan pembuatan entri terpisah per kernel
#   mentah (ikon penguin generik).
# - dont_scan_files: sembunyikan file loader mentah yang cuma dipakai secara
#   internal oleh shim, di semua volume/lokasi manapun ditemukan (ikon
#   generik/ketupat/kubus).
MINIMAL_SCAN_ALL_LINUX_KERNELS = "false"
# PENTING: dimulai dengan '+' supaya ini MENAMBAH ke daftar bawaan rEFInd
# sendiri (yang sudah cukup panjang -- termasuk nama-nama file shim/MokManager
# tertentu, lihat refind.log dengan log_level 3+), bukan MENIMPA/mengganti
# daftar bawaan itu. Tanpa '+' di depan, nilai ini justru bisa membuka
# kembali file yang sebelumnya disembunyikan rEFInd sendiri secara default.
# Selain grub*/fb*/mm* (loader internal shim+GRUB), turut disembunyikan
# bootx64.efi dkk. di folder EFI/BOOT -- loader fallback wajib-UEFI yang oleh
# banyak distro (termasuk Ubuntu) sengaja dibuat identik/duplikat dengan
# loader OS yang sudah punya entri sendiri, dan karena folder 'BOOT' tidak
# cocok dengan nama ikon OS manapun, entri ini tampil dengan ikon generik
# (kubus/ketupat) -- kandidat paling umum untuk "entri OS ke-3" yang dilihat
# pengguna di layar boot.
MINIMAL_DONT_SCAN_FILES = (
    "+ "
    "grubx64.efi,grubia32.efi,grubaa64.efi,grubarm.efi,"
    "fbx64.efi,fbia32.efi,fbaa64.efi,"
    "mmx64.efi,mmia32.efi,mmaa64.efi,"
    "EFI/BOOT/bootx64.efi,EFI/BOOT/bootia32.efi,EFI/BOOT/bootaa64.efi"
)


def _normalise_esp_relative_path(value: str) -> str:
    """Validasi path loader relatif terhadap root ESP, tanpa path traversal."""
    candidate = value.replace("\\", "/").strip().lstrip("/")
    if not candidate or candidate.startswith("../") or "/../" in candidate or candidate == "..":
        raise CLIError("Path loader harus relatif terhadap root ESP, misalnya EFI/ubuntu/grubx64.efi.")
    return candidate


def _esp_loader_path(refind_dir: Path, relative_path: str) -> Path:
    esp_root = system_mod.esp_root_from_refind_dir(refind_dir)
    if esp_root is None:
        raise CLIError("Root ESP tidak dapat ditentukan dari lokasi folder rEFInd.")
    relative_path = _normalise_esp_relative_path(relative_path)
    candidate = (esp_root / relative_path).resolve()
    try:
        candidate.relative_to(esp_root.resolve())
    except ValueError as exc:
        raise CLIError("Path loader berada di luar ESP dan ditolak.") from exc
    if not candidate.is_file():
        raise CLIError(f"Loader yang dipertahankan tidak ditemukan di ESP: {relative_path}")
    return candidate


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _dont_scan_items(lines: list) -> set[str]:
    """Ambil aturan eksplisit dont_scan_files; daftar bawaan rEFInd tidak ditebak."""
    value = conf_mod.get_global_option(lines, "dont_scan_files")
    if not value:
        return set()
    value = value.lstrip("+").strip()
    return {item.strip().replace("\\", "/").lower().lstrip("/") for item in value.split(",") if item.strip()}


def _assert_keep_loader_is_not_excluded(lines: list, relative_path: str) -> None:
    rules = _dont_scan_items(lines)
    canonical = _normalise_esp_relative_path(relative_path).lower()
    basename = canonical.rsplit("/", 1)[-1]
    if canonical in rules or basename in rules:
        raise CLIError(
            f"Loader yang kamu pilih untuk dipertahankan ({relative_path}) masih tercakup "
            "oleh dont_scan_files aktif. Jalankan 'refindmgr declutter --undo' untuk "
            "membersihkan aturan peninggalan versi lama, lalu jalankan dedupe lagi."
        )


def _append_dont_scan_path(lines: list, relative_path: str) -> list:
    """Tambahkan SATU path ESP spesifik; tidak pernah menambahkan nama file global."""
    canonical = _normalise_esp_relative_path(relative_path)
    value = conf_mod.get_global_option(lines, "dont_scan_files")
    if value:
        prefix = "+ " if value.lstrip().startswith("+") else ""
        values = [item.strip() for item in value.lstrip("+").split(",") if item.strip()]
        if canonical.lower() not in {item.replace("\\", "/").lstrip("/").lower() for item in values}:
            values.append(canonical)
        return conf_mod.set_global_option(lines, "dont_scan_files", prefix + ",".join(values))
    # '+' mempertahankan blacklist bawaan rEFInd; path penuh mencegah grubx64.efi
    # di folder distro lain ikut tersembunyi.
    return conf_mod.set_global_option(lines, "dont_scan_files", f"+ {canonical}")


def _fallback_duplicates(refind_dir: Path, keep_path: Path) -> list[str]:
    """Cari fallback EFI/BOOT yang byte-identik dengan loader yang dipertahankan."""
    esp_root = system_mod.esp_root_from_refind_dir(refind_dir)
    if esp_root is None:
        return []
    keep_hash = _sha256(keep_path)
    found = []
    for rel_path in system_mod.list_esp_loader_files(refind_dir):
        normalised = rel_path.lower()
        if not normalised.startswith("efi/boot/") or not normalised.endswith(".efi"):
            continue
        candidate = esp_root / rel_path
        if candidate.resolve() != keep_path.resolve() and _sha256(candidate) == keep_hash:
            found.append(rel_path)
    return found


def cmd_dedupe(args: argparse.Namespace) -> None:
    """Pratinjau atau terapkan pengurangan entri boot secara path-aware dan aman."""
    refind_dir = _resolve_refind_dir(args)
    conf_path = refind_conf_path(refind_dir)
    if not conf_path.is_file():
        raise CLIError(f"refind.conf tidak ditemukan di {refind_dir}")
    lines = conf_mod.read_lines(conf_path)
    loaders = system_mod.list_esp_loader_files(refind_dir)
    kernels = [item for item in system_mod.find_boot_kernel_files() if item != "refind_linux.conf"]

    if not args.apply:
        print("=== Pratinjau dedupe boot (TIDAK ADA FILE DIUBAH) ===")
        print("\nLoader EFI yang terdeteksi:")
        for loader in loaders or ["(tidak ditemukan / ESP tidak dapat dibaca)"]:
            print(f"  - {loader}")
        print("\nKandidat ikon penguin (kernel mentah /boot):")
        for kernel in kernels or ["(tidak ditemukan)"]:
            print(f"  - {kernel}")
        print("\nPrinsip keamanan: jangan pernah menyembunyikan grubx64.efi atau shimx64.efi "
              "secara global. Pilih tepat SATU loader OS yang terbukti harus dipertahankan.")
        print("\nContoh tahap 1 (hanya menyembunyikan ikon penguin):")
        print("  sudo refindmgr dedupe --apply --disable-kernels --keep-loader EFI/ubuntu/grubx64.efi")
        print("\nTahap 2 untuk ikon kotak hanya boleh dilakukan bila fallback byte-identik:")
        print("  sudo refindmgr dedupe --apply --hide-fallback EFI/BOOT/BOOTX64.EFI "
              "--keep-loader EFI/ubuntu/grubx64.efi")
        print("Perintah apply akan menolak jika loader pilihan tidak ada, sedang dikecualikan, "
              "atau fallback bukan salinan byte-identik. Backup dibuat otomatis.")
        return

    if not args.keep_loader:
        raise CLIError("Mode --apply wajib memakai --keep-loader EFI/<distro>/<loader>.efi.")
    if not args.disable_kernels and not args.hide_fallback:
        raise CLIError("Pilih minimal satu tindakan: --disable-kernels dan/atau --hide-fallback PATH.")

    keep_rel = _normalise_esp_relative_path(args.keep_loader)
    keep_path = _esp_loader_path(refind_dir, keep_rel)
    _assert_keep_loader_is_not_excluded(lines, keep_rel)
    new_lines = list(lines)
    actions = []

    if args.disable_kernels:
        new_lines = conf_mod.set_global_option(new_lines, "scan_all_linux_kernels", "false")
        actions.append("ikon kernel mentah (scan_all_linux_kernels false)")

    if args.hide_fallback:
        fallback_rel = _normalise_esp_relative_path(args.hide_fallback)
        if not fallback_rel.lower().startswith("efi/boot/") or not fallback_rel.lower().endswith(".efi"):
            raise CLIError("--hide-fallback hanya menerima file .efi tepat di bawah EFI/BOOT/.")
        fallback_path = _esp_loader_path(refind_dir, fallback_rel)
        if fallback_path.resolve() == keep_path.resolve():
            raise CLIError("Fallback yang disembunyikan tidak boleh sama dengan loader yang dipertahankan.")
        if _sha256(fallback_path) != _sha256(keep_path):
            raise CLIError(
                "Fallback ditolak: isinya TIDAK byte-identik dengan loader yang dipertahankan. "
                "Tidak aman menyembunyikannya otomatis."
            )
        new_lines = _append_dont_scan_path(new_lines, fallback_rel)
        actions.append(f"fallback duplikat path-spesifik ({fallback_rel})")

    _warn_if_not_root()
    backup_path = conf_mod.backup(conf_path)
    conf_mod.write_lines(conf_path, new_lines)
    print("Dedupe diterapkan dengan aman:\n- " + "\n- ".join(actions))
    print(f"Loader OS yang dipertahankan: {keep_rel}")
    print(f"Backup dibuat: {backup_path}")
    print("Reboot dan pastikan loader OS tetap muncul. Jika tidak, jalankan 'sudo refindmgr restore'.")


def _declutter_theme_override_note(refind_dir: Path, lines: list, undo: bool) -> str:
    """Netralkan baris 'showtools'/'scanfor' milik tema aktif (jika ada) di
    theme.conf-nya sendiri, dan kembalikan catatan penjelasan untuk dicetak.

    Root cause nyata dari "declutter sudah jalan tapi ikon tools masih penuh":
    rEFInd memproses arahan 'include themes/<nama>/theme.conf' secara inline --
    jadi kalau tema aktif punya baris 'showtools' sendiri (sangat umum untuk
    tema dekoratif yang mau memamerkan ikon custom mereka untuk shell/memtest/
    dll.), baris itu bisa menimpa baris 'showtools' yang refindmgr tulis di
    refind.conf utama, terlepas dari urutan baris di file dan terlepas dari
    versi rEFInd yang dipakai. refindmgr sebelumnya hanya mengedit refind.conf
    utama dan tidak memeriksa hal ini -- inilah yang diperbaiki di sini.
    """
    active_theme = conf_mod.get_active_theme(lines)
    if active_theme is None:
        return ""
    theme_conf = themes_mod.theme_conf_path(refind_dir, active_theme)
    if theme_conf is None:
        return ""

    if undo:
        # Tidak mengecek isi baris theme.conf saat ini di sini: kalau declutter
        # sebelumnya sudah mengomentari baris 'showtools'/'scanfor' milik tema,
        # baris itu memang TIDAK lagi aktif -- jadi satu-satunya sinyal yang
        # benar untuk 'apakah kita pernah mengubah file ini' adalah adanya
        # backup otomatis yang kita buat sendiri saat itu, bukan status aktif
        # baris saat ini.
        backups = conf_mod.list_backups(theme_conf)
        if not backups:
            return ""
        conf_mod.restore(theme_conf, backups[-1])
        return (
            f"\nCatatan: baris showtools/scanfor milik tema aktif '{active_theme}' "
            "(di theme.conf-nya sendiri) juga dikembalikan ke isi aslinya."
        )

    theme_lines = conf_mod.read_lines(theme_conf)
    # Diperluas dari cek showtools/scanfor semula ke keempat token yang ditulis
    # oleh declutter, supaya kalau ada tema yang (jarang, tapi mungkin) juga
    # menyetel scan_all_linux_kernels/dont_scan_files sendiri, itu ikut
    # dinetralkan dengan cara yang sama -- bukan cuma showtools/scanfor.
    overriding = [
        token
        for token in ("showtools",)
        if conf_mod.get_global_option(theme_lines, token) is not None
    ]
    if not overriding:
        return ""

    conf_mod.backup(theme_conf)
    new_theme_lines = theme_lines
    for token in overriding:
        new_theme_lines = conf_mod.unset_global_option(new_theme_lines, token)
    conf_mod.write_lines(theme_conf, new_theme_lines)
    return (
        f"\nCatatan penting: tema aktif '{active_theme}' punya baris "
        f"{', '.join(overriding)} sendiri di theme.conf, yang bisa menimpa pengaturan "
        "di atas (rEFInd memproses 'include' secara inline, baris terakhir yang menang). "
        "Baris itu ikut dikomentari otomatis di theme.conf tema tersebut. Backup theme.conf "
        "juga sudah dibuat otomatis, dan akan dikembalikan jika kamu jalankan "
        "'refindmgr declutter --undo'."
    )


def cmd_declutter(args: argparse.Namespace) -> None:
    """Rapikan tampilan boot rEFInd: sembunyikan ikon tools yang jarang dipakai
    (shell, memtest, gdisk, mok_tool, about, hidden_tags, firmware, fwupdate,
    dll.) dan hanya sisakan Shutdown & Reboot, tanpa mengubah daftar OS yang
    terdeteksi. Semua perubahan ditulis ke refind.conf lewat conf_mod, dengan
    backup otomatis, jadi bisa dibalik lewat 'declutter --undo' atau 'restore'.

    Juga memeriksa apakah tema aktif (jika ada) punya baris 'showtools'/
    'scanfor' sendiri di theme.conf-nya yang bisa menimpa pengaturan di atas --
    lihat _declutter_theme_override_note.
    """
    refind_dir = _resolve_refind_dir(args)
    _warn_if_not_root()
    conf_path = refind_conf_path(refind_dir)
    if not conf_path.is_file():
        raise CLIError(f"refind.conf tidak ditemukan di {refind_dir}")
    lines = conf_mod.read_lines(conf_path)
    conf_mod.backup(conf_path)
    if args.undo:
        new_lines = conf_mod.unset_global_option(lines, "showtools")
        # Turut membersihkan token lama ini kalau masih aktif dari versi
        # refindmgr sebelumnya (versi ini sendiri tidak lagi menulisnya).
        new_lines = conf_mod.unset_global_option(new_lines, "scanfor")
        new_lines = conf_mod.unset_global_option(new_lines, "scan_all_linux_kernels")
        new_lines = conf_mod.unset_global_option(new_lines, "dont_scan_files")
        new_lines = conf_mod.unset_global_option(new_lines, "dont_scan_dirs")
        conf_mod.write_lines(conf_path, new_lines)
        theme_note = _declutter_theme_override_note(refind_dir, lines, undo=True)
        print(
            "Tampilan tools rEFInd dikembalikan ke pengaturan bawaan rEFInd sendiri "
            "(baris 'showtools' yang ditulis refindmgr dikomentari lagi; baris "
            "'scanfor'/'scan_all_linux_kernels'/'dont_scan_files'/'dont_scan_dirs' peninggalan "
            "versi refindmgr sebelumnya, jika ada, juga ikut dikomentari).\n"
            "Backup refind.conf sebelum ini juga sudah disimpan otomatis."
            f"{theme_note}"
        )
        return
    new_lines = conf_mod.set_global_option(lines, "showtools", MINIMAL_SHOWTOOLS)
    conf_mod.write_lines(conf_path, new_lines)
    theme_note = _declutter_theme_override_note(refind_dir, new_lines, undo=False)
    print(
        "Tampilan boot dirapikan:\n"
        "- Baris bawah rEFInd sekarang cuma menampilkan 'Shutdown' dan 'Reboot' -- ikon "
        "shell/memtest/mok_tool/about/hidden tags/firmware setup/dll. disembunyikan.\n"
        f"(Ditulis ke refind.conf: 'showtools {MINIMAL_SHOWTOOLS}'.)\n"
        "CATATAN: declutter versi ini SENGAJA tidak lagi menyentuh 'scanfor', "
        "'scan_all_linux_kernels', atau 'dont_scan_files' sama sekali -- dua kali "
        "perubahan otomatis di opsi-opsi itu terbukti membuat entri OS asli (Ubuntu) "
        "ikut hilang total di layar boot pada pengujian nyata, jadi sekarang declutter "
        "hanya menjamin aman: baris tools saja. Kalau kamu masih ingin menyembunyikan "
        "entri kernel mentah/loader duplikat, lakukan itu MANUAL dan bertahap (satu opsi, "
        "reboot, cek, baru lanjut opsi berikutnya) -- lihat README.md bagian "
        "'Menyembunyikan entri OS duplikat (manual, opsional)'.\n"
        "Backup refind.conf sebelum ini sudah disimpan otomatis -- jalankan "
        "'refindmgr declutter --undo' atau 'refindmgr restore' kapan saja untuk mengembalikannya."
        f"{theme_note}"
    )



_CLEAN_MENU_BEGIN = "# refindmgr-clean-menu: begin"
_CLEAN_MENU_END = "# refindmgr-clean-menu: end"
_CLEAN_MENU_PREVIOUS = "# refindmgr-clean-menu: previous-scanfor="


def _remove_managed_clean_menu(lines: list) -> tuple[list, Optional[str]]:
    """Hapus blok menu manual yang sebelumnya dibuat refindmgr, bila ada."""
    result, previous, inside = [], None, False
    for line in lines:
        stripped = line.strip()
        if stripped == _CLEAN_MENU_BEGIN:
            inside = True
            continue
        if stripped == _CLEAN_MENU_END:
            inside = False
            continue
        if inside:
            if stripped.startswith(_CLEAN_MENU_PREVIOUS):
                previous = stripped[len(_CLEAN_MENU_PREVIOUS):]
            continue
        result.append(line)
    if inside:
        raise CLIError("Blok clean-menu lama tidak lengkap; pulihkan refind.conf dari backup sebelum melanjutkan.")
    return result, previous


def _parse_os_specs(specs: list[str], refind_dir: Path, lines: list) -> list[tuple[str, str]]:
    if not specs:
        raise CLIError("Tambahkan minimal satu --os 'Nama OS=EFI/path/loader.efi'.")
    parsed, names, paths = [], set(), set()
    for spec in specs:
        if "=" not in spec:
            raise CLIError("Format --os harus 'Nama OS=EFI/path/loader.efi', misalnya 'Ubuntu=EFI/ubuntu/grubx64.efi'.")
        name, relative_path = (part.strip() for part in spec.split("=", 1))
        if not name or any(char in name for char in '"{}\n\r'):
            raise CLIError("Nama OS tidak boleh kosong atau mengandung tanda kutip, kurung kurawal, atau baris baru.")
        relative_path = _normalise_esp_relative_path(relative_path)
        if not relative_path.lower().endswith(".efi"):
            raise CLIError(f"Loader OS harus file .efi: {relative_path}")
        _esp_loader_path(refind_dir, relative_path)
        _assert_keep_loader_is_not_excluded(lines, relative_path)
        if name.lower() in names or relative_path.lower() in paths:
            raise CLIError("Nama OS dan path loader dalam --os harus unik.")
        names.add(name.lower())
        paths.add(relative_path.lower())
        parsed.append((name, relative_path))
    return parsed



def _detect_standard_os_loaders(refind_dir: Path, lines: list) -> list[tuple[str, str]]:
    """Deteksi loader OS standar yang aman dijadikan menu manual.

    Sengaja hanya menerima path distro yang terkenal dan loader utama; shim,
    MokManager, fbx/mmx, dan EFI/BOOT fallback tidak pernah dipilih otomatis.
    Sistem dengan tata letak non-standar tetap bisa memakai --os manual.
    """
    known_linux = {
        "ubuntu": "Ubuntu", "debian": "Debian", "fedora": "Fedora",
        "arch": "Arch Linux", "manjaro": "Manjaro", "opensuse": "openSUSE",
        "linuxmint": "Linux Mint", "pop_os": "Pop!_OS", "zorin": "Zorin OS",
        "elementary": "elementary OS", "kali": "Kali Linux",
    }
    candidates = []
    available = {path.lower(): path for path in system_mod.list_esp_loader_files(refind_dir)}
    windows = "efi/microsoft/boot/bootmgfw.efi"
    if windows in available:
        candidates.append(("Windows", available[windows]))
    for folder, label in known_linux.items():
        path = f"efi/{folder}/grubx64.efi"
        if path in available:
            candidates.append((label, available[path]))
    # Terapkan validasi yang sama seperti input manual. Aturan global lama bisa
    # masih mengecualikan grubx64.efi; kandidat seperti itu tidak boleh dipilih.
    safe = []
    for name, relative_path in candidates:
        try:
            _esp_loader_path(refind_dir, relative_path)
            _assert_keep_loader_is_not_excluded(lines, relative_path)
        except CLIError:
            continue
        safe.append((name, relative_path))
    return safe

def cmd_clean_menu(args: argparse.Namespace) -> None:
    """Buat menu OS-only dari stanza manual yang dipilih eksplisit pengguna.

    Tidak menebak mapping ikon->loader dan tidak mem-blacklist loader. Sebagai
    gantinya, scan otomatis dimatikan (scanfor manual) HANYA sesudah pengguna
    memilih loader yang ada di ESP. Ini satu-satunya cara universal untuk
    menyisakan daftar OS tanpa bergantung pada nama grub/shim/fallback distro.
    """
    refind_dir = _resolve_refind_dir(args)
    conf_path = refind_conf_path(refind_dir)
    if not conf_path.is_file():
        raise CLIError(f"refind.conf tidak ditemukan di {refind_dir}")
    lines = conf_mod.read_lines(conf_path)
    base_lines, saved_previous = _remove_managed_clean_menu(lines)

    if args.undo:
        if saved_previous is None:
            raise CLIError("Tidak menemukan konfigurasi clean-menu buatan refindmgr untuk dikembalikan.")
        _warn_if_not_root()
        backup_path = conf_mod.backup(conf_path)
        if saved_previous == "__DEFAULT__":
            restored = conf_mod.unset_global_option(base_lines, "scanfor")
        else:
            restored = conf_mod.set_global_option(base_lines, "scanfor", saved_previous)
        conf_mod.write_lines(conf_path, restored)
        print("Berhasil membatalkan mode OS saja.")
        print(f"Backup: {backup_path}")
        return

    if args.auto:
        os_entries = _detect_standard_os_loaders(refind_dir, base_lines)
        if not os_entries:
            raise CLIError(
                "Tidak menemukan loader OS standar yang aman untuk dipilih otomatis. "
                "Gunakan --os 'Nama=EFI/path/loader.efi' setelah menjalankan 'refindmgr dedupe'."
            )
    else:
        os_entries = _parse_os_specs(args.os, refind_dir, base_lines)
    other_active_manual = [stanza for stanza in conf_mod.find_manual_stanzas(base_lines)
                           if not stanza["commented"] and not stanza["disabled"]]
    if other_active_manual:
        names = ", ".join(stanza["name"] for stanza in other_active_manual)
        raise CLIError(
            "Ditemukan menuentry manual aktif lain (" + names + "). Tool menolak agar "
            "tidak ada entri tambahan tersembunyi. Nonaktifkan/tinjau stanza tersebut dulu."
        )

    if not args.apply:
        print("=== Pratinjau menu OS-only (TIDAK ADA FILE DIUBAH) ===")
        if args.auto:
            print("Loader berikut dipilih otomatis dari path OS standar di ESP:")
        else:
            print("OS yang akan ditampilkan:")
        for name, path in os_entries:
            print(f"  - {name}: /{path}")
        print("\nYang akan dilakukan saat memakai --apply:")
        print("  - menulis menuentry manual untuk OS di atas;")
        print("  - mengatur 'scanfor manual', sehingga kernel/penguin, fallback/kotak, dan loader auto-scan lain tidak tampil;")
        print("  - TIDAK menambah dont_scan_files/dont_scan_dirs dan TIDAK menghapus file EFI;")
        print("  - membuat backup otomatis. Batalkan nanti: sudo refindmgr clean-menu --undo")
        return

    old_scanfor = conf_mod.get_global_option(base_lines, "scanfor")
    previous = old_scanfor if old_scanfor is not None else "__DEFAULT__"
    new_lines = conf_mod.set_global_option(base_lines, "scanfor", "manual")
    if new_lines and new_lines[-1].strip():
        new_lines.append("")
    new_lines.extend([_CLEAN_MENU_BEGIN, _CLEAN_MENU_PREVIOUS + previous])
    for name, relative_path in os_entries:
        new_lines.extend([f'menuentry "{name}" {{', f"    loader /{relative_path}", "}"])
    new_lines.append(_CLEAN_MENU_END)

    _warn_if_not_root()
    backup_path = conf_mod.backup(conf_path)
    conf_mod.write_lines(conf_path, new_lines)
    print("Berhasil menerapkan mode OS saja.")
    print("OS yang tampil:")
    for name, path in os_entries:
        print(f"- {name}: /{path}")
    print("Ikon penguin, kotak/fallback, dan loader duplikat disembunyikan.")
    print(f"Backup: {backup_path}")
    print("Reboot untuk melihat hasilnya. Batalkan: refindmgr clean-menu --undo")

def cmd_backup(args: argparse.Namespace) -> None:
    refind_dir = _resolve_refind_dir(args)
    _warn_if_not_root()
    conf_path = refind_conf_path(refind_dir)
    if not conf_path.is_file():
        raise CLIError(f"refind.conf tidak ditemukan di {refind_dir}")
    backup_path = conf_mod.backup(conf_path)
    print(f"Backup dibuat: {backup_path}")


def cmd_restore(args: argparse.Namespace) -> None:
    refind_dir = _resolve_refind_dir(args)
    _warn_if_not_root()
    conf_path = refind_conf_path(refind_dir)
    backups = conf_mod.list_backups(conf_path)
    if args.backup:
        backup_path = Path(args.backup)
    elif backups:
        backup_path = backups[-1]
    else:
        raise CLIError("Tidak ada file backup ditemukan.")
    conf_mod.restore(conf_path, backup_path)
    print(f"refind.conf dipulihkan dari: {backup_path}")


def cmd_doctor(args: argparse.Namespace) -> None:
    refind_dir = detect_refind_dir(_refind_dir_arg(args))
    print("=== Diagnostik refindmgr ===")
    # Selalu cetak versi refindmgr yang SEDANG BERJALAN di baris pertama.
    # Ini penting untuk audit itu sendiri: cara paling gampang membedakan
    # "perbaikan belum berhasil" dari "perbaikan belum ter-deploy sama sekali"
    # (misalnya lupa jalankan ulang 'sudo ./install.sh' setelah menarik/
    # extract kode baru, sehingga /usr/local/bin/refindmgr masih menjalankan
    # kode lama) adalah membandingkan angka versi ini dengan yang tercantum
    # di README/rilis terbaru.
    print(f"[INFO]    Versi refindmgr yang berjalan sekarang: {__version__}")
    if refind_dir is None:
        print("[GAGAL] Folder rEFInd tidak ditemukan otomatis.")
        print("        Coba jalankan dengan --refind-dir /path/ke/EFI/refind")
        print("        Belum pernah install rEFInd sama sekali? Coba 'refindmgr setup'.")
    else:
        print(f"[OK]    Folder rEFInd ditemukan: {refind_dir}")
        conf_path = refind_conf_path(refind_dir)
        status = "OK" if conf_path.is_file() else "GAGAL"
        print(f"[{status}]    refind.conf: {conf_path}")
        if conf_path.is_file():
            conf_lines = conf_mod.read_lines(conf_path)
            active_list = conf_mod.get_active_themes(conf_lines)
            if len(active_list) > 1:
                print(
                    f"[PERINGATAN]    Ada {len(active_list)} tema aktif sekaligus di refind.conf "
                    f"({', '.join(active_list)}). Jalankan 'refindmgr activate <nama>' untuk merapikannya."
                )
            _print_manual_stanza_audit(conf_lines)
    git_ok = themes_mod.is_git_available()
    print(f"[{'OK' if git_ok else 'PERINGATAN'}]    git terpasang di PATH" + ("" if git_ok else " (diperlukan untuk install dari URL)"))
    root_ok = system_mod.is_root()
    print(f"[{'OK' if root_ok else 'INFO'}]    dijalankan sebagai root" + ("" if root_ok else " (perlu sudo untuk operasi yang menulis ke EFI)"))
    if refind_dir is not None:
        _print_esp_loader_audit(refind_dir)
    _print_boot_kernel_audit()


def _print_boot_kernel_audit() -> None:
    """Cetak file kernel Linux mentah (vmlinuz*/bzImage*/kernel*) dan
    'refind_linux.conf' yang ditemukan di /boot pada sistem yang berjalan.

    Ini di luar cakupan _print_esp_loader_audit (yang hanya melihat ESP),
    karena /boot di Debian/Ubuntu biasanya berada di filesystem Linux yang
    berbeda dari ESP. Kalau ada 'refind_linux.conf' di sini, rEFInd akan
    tetap menampilkan entri kernel itu (ikon Tux/penguin) WALAUPUN
    'scan_all_linux_kernels' sudah diset ke false -- ini bukan bug, tapi
    perilaku resmi rEFInd (kehadiran refind_linux.conf = niat eksplisit).
    """
    found = system_mod.find_boot_kernel_files()
    print("\n=== Kernel mentah & refind_linux.conf di /boot ===")
    if not found:
        print("  (tidak ada file kernel mentah atau refind_linux.conf ditemukan di /boot)")
        return
    for name in found:
        print(f"  - /boot/{name}")
    if any(name == "refind_linux.conf" for name in found):
        print(
            "[INFO]    Ada 'refind_linux.conf' di /boot -- ini membuat rEFInd tetap "
            "menampilkan entri kernel mentah (ikon penguin) meski 'scan_all_linux_kernels' "
            "sudah false, karena rEFInd menganggap file ini sebagai tanda niat eksplisit. "
            "Kalau entri penguin ini yang tidak diinginkan (karena OS sudah punya entri "
            "GRUB/shim sendiri), satu-satunya cara menyembunyikannya adalah menghapus/"
            "memindahkan refind_linux.conf, atau menambahkan path kernelnya (mis. "
            "'/boot/vmlinuz-5.15.0-91-generic') ke dont_scan_files -- perlu diperbarui "
            "tiap kali versi kernel berganti, jadi hati-hati."
        )


def _print_manual_stanza_audit(conf_lines: list) -> None:
    """Cetak semua blok 'menuentry' (stanza boot manual) yang ditemukan di
    refind.conf, dan tandai mana yang AKTIF (tidak ada baris 'disabled').

    Ini penting karena declutter (showtools/scanfor/scan_all_linux_kernels/
    dont_scan_files) HANYA mengatur proses auto-scan rEFInd -- tidak satu pun
    dari opsi itu menyaring stanza 'menuentry' manual. refind.conf-sample yang
    sering ikut dipasang otomatis oleh 'refind-install' di Debian/Ubuntu
    menyertakan contoh blok seperti ini untuk Ubuntu, langsung menunjuk ke
    /EFI/ubuntu/grubx64.efi, dinonaktifkan lewat baris 'disabled' di dalamnya.
    Kalau baris 'disabled' itu hilang/pernah terhapus, stanza itu AKTIF dan
    akan selalu tampil sebagai entri boot terpisah tanpa ikon OS (karena tidak
    ada baris 'icon' di dalamnya) -- ikon generik/kubus/ketupat -- dan TIDAK
    akan pernah hilang lewat opsi declutter manapun, karena bukan hasil scan.
    """
    stanzas = conf_mod.find_manual_stanzas(conf_lines)
    if not stanzas:
        return
    print("\n=== Stanza boot manual ('menuentry') di refind.conf ===")
    active_unnamed_icon = []
    for stanza in stanzas:
        if stanza["commented"]:
            tag = "[dikomentari, tidak aktif]"
        elif stanza["disabled"]:
            tag = "[nonaktif via 'disabled']"
        else:
            tag = "[AKTIF]"
            active_unnamed_icon.append(stanza["name"])
        print(f"  {tag}  menuentry \"{stanza['name']}\" (baris {stanza['start_line'] + 1})")
    if active_unnamed_icon:
        names = ", ".join(active_unnamed_icon)
        print(
            f"[PERINGATAN]    Stanza manual berikut AKTIF dan tidak difilter oleh declutter sama "
            f"sekali: {names}. Kalau salah satu ini yang membuat entri OS duplikat berikon generik/"
            "kubus/ketupat, tambahkan baris 'disabled' di dalam blok 'menuentry' tersebut di "
            "refind.conf (atau hapus blok itu), lalu simpan (refindmgr akan tetap membuat backup "
            "otomatis kalau kamu edit lewat 'refindmgr backup' sebelumnya)."
        )


def _print_esp_loader_audit(refind_dir: Path) -> None:
    """Cetak semua file '*.efi' lain yang ditemukan di ESP yang sama dengan
    rEFInd (di luar folder rEFInd sendiri & EFI/tools), dan tandai mana yang
    sudah tercakup oleh 'dont_scan_files' bawaan declutter (MINIMAL_DONT_SCAN_FILES)
    dan mana yang belum.

    Ini adalah bagian "audit" nyata yang diminta pengguna: daripada menebak
    nama file loader duplikat lewat asumsi/dokumentasi saja, tool ini melihat
    langsung isi ESP yang sebenarnya, supaya kalau declutter masih belum
    menghilangkan sebuah entri, kita punya daftar file konkret untuk dicek --
    bukan tebakan lagi.
    """
    loader_files = system_mod.list_esp_loader_files(refind_dir)
    print("\n=== Audit loader di ESP (di luar folder rEFInd & EFI/tools) ===")
    if not loader_files:
        print("[INFO]    Tidak ada file .efi lain ditemukan (atau root ESP tidak bisa ditebak).")
        return
    # Jangan pernah menyimpulkan status dari daftar hard-code: daftar bawaan
    # rEFInd tidak terlihat dari refind.conf dan sebelumnya membuat audit ini
    # salah menyatakan shimx64.efi aman. Tampilkan hanya aturan eksplisit yang
    # benar-benar tertulis saat ini.
    explicit_rules = _dont_scan_items(conf_mod.read_lines(refind_conf_path(refind_dir)))
    for rel_path in loader_files:
        canonical = rel_path.lower()
        basename = canonical.rsplit("/", 1)[-1]
        is_explicitly_excluded = canonical in explicit_rules or basename in explicit_rules
        tag = "[dikecualikan eksplisit]" if is_explicitly_excluded else "[tidak dikecualikan eksplisit]"
        print(f"  {tag}  {rel_path}")
    print(
        "[INFO]    Status daftar bawaan internal rEFInd (misalnya shim/MokManager) "
        "tidak dapat dipastikan hanya dari refind.conf, jadi audit ini sengaja tidak "
        "menebak. Gunakan 'refindmgr dedupe' untuk pratinjau path-aware sebelum "
        "menyembunyikan ikon generik."
    )


def _version_tuple(version: str) -> tuple:
    return tuple(int(part) for part in version.split("."))


def _ensure_refind_version_pinned(args: argparse.Namespace, manager: Optional["system_mod.PackageManagerInfo"]) -> None:
    """Pastikan versi paket rEFInd yang terpasang persis system_mod.TARGET_REFIND_VERSION.

    rEFInd 0.14.2+ punya bug upstream yang dilaporkan luas: opsi 'showtools'
    (dipakai oleh 'refindmgr declutter') berhenti berfungsi dengan benar, jadi
    tampilan boot tetap menunjukkan semua ikon tools meski refind.conf sudah
    benar. refindmgr menjaga versi paket rEFInd tetap di 0.14.1 (belum terkena
    bug ini) sampai ada perbaikan resmi dari proyek rEFInd -- baik itu berarti
    memasang, menaikkan, atau menurunkan versi paket yang sudah ada.

    Dipanggil otomatis dari cmd_setup (jadi juga otomatis lewat install.sh),
    tetap menghormati flag --yes yang sama seperti langkah setup lain: tanpa
    --yes ini hanya pratinjau, tidak pernah mengubah apa pun.
    """
    target = system_mod.TARGET_REFIND_VERSION
    if manager is None:
        print(
            "Tidak bisa mendeteksi package manager yang didukung, jadi refindmgr melewati "
            f"pengecekan versi rEFInd otomatis (target: {target}, untuk menghindari bug "
            "'showtools' di rEFInd 0.14.2+)."
        )
        return

    installed_version = system_mod.get_installed_refind_version(manager)
    if installed_version == target:
        print(f"Versi paket rEFInd sudah {installed_version} (target: {target}). Tidak ada yang perlu diubah.")
        return

    if installed_version is None:
        action_desc = f"memasang paket rEFInd versi {target}"
    elif _version_tuple(installed_version) > _version_tuple(target):
        action_desc = f"menurunkan (downgrade) paket rEFInd dari versi {installed_version} ke {target}"
    else:
        action_desc = f"menaikkan (upgrade) paket rEFInd dari versi {installed_version} ke {target}"

    print(
        f"refindmgr akan {action_desc}.\n"
        "Alasan: rEFInd 0.14.2 dan yang lebih baru punya bug upstream yang dilaporkan luas -- opsi \n"
        "'showtools' (dipakai oleh 'refindmgr declutter') tidak lagi berfungsi dengan benar, jadi \n"
        "tampilan boot tetap menunjukkan semua ikon tools meski sudah diatur. Versi 0.14.1 belum \n"
        "terkena bug ini."
    )
    if not args.yes:
        print(
            "Ini baru pratinjau -- belum ada perubahan apa pun yang dibuat.\n"
            "Jalankan ulang dengan 'sudo refindmgr setup --yes' untuk benar-benar menerapkannya."
        )
        return
    if not system_mod.is_root():
        raise CLIError(f"Perintah ini butuh akses root untuk {action_desc}. Jalankan ulang dengan: sudo refindmgr setup --yes")
    try:
        exact_version = system_mod.pin_refind_version(manager, target=target)
    except system_mod.BootstrapError as exc:
        if manager.name == "apt":
            # Banyak repo apt distro (termasuk Ubuntu) hanya menyediakan rilis
            # rEFInd TERBARU, bukan versi lama seperti target di sini -- itu
            # sebabnya pin_refind_version gagal. Jalur cadangan: unduh paket
            # .deb resmi versi target langsung dari SourceForge (bukan repo
            # distro) dan pasang lewat dpkg, supaya versi target tetap benar
            # -benar tercapai alih-alih hanya menampilkan peringatan.
            print(
                f"PERINGATAN: gagal {action_desc} lewat repo apt: {exc}\n"
                "Mencoba jalur cadangan: mengunduh paket .deb resmi rEFInd langsung dari "
                "SourceForge (bukan repo distro) dan memasangnya lewat dpkg..."
            )
            try:
                with tempfile.TemporaryDirectory() as tmp_dir:
                    deb_path = os.path.join(tmp_dir, f"refind_{target}-1_amd64.deb")
                    system_mod.download_refind_deb(target, deb_path)
                    system_mod.install_deb_file(deb_path)
            except system_mod.BootstrapError as fallback_exc:
                print(
                    f"PERINGATAN: jalur cadangan juga gagal: {fallback_exc}\n"
                    f"Pasang manual versi {target} dari rilis resmi rEFInd:\n"
                    "  https://sourceforge.net/projects/refind/files/"
                )
                return
            exact_version = target
            print(f"Berhasil {action_desc} lewat paket .deb resmi dari SourceForge (bukan repo apt).")
        else:
            # Bukan fatal: kegagalan pinning versi tidak seharusnya membatalkan proses
            # setup rEFInd itu sendiri (yang sudah berhasil sampai titik ini).
            print(f"PERINGATAN: gagal {action_desc}: {exc}")
            return
    print(f"Berhasil {action_desc} (paket terpasang: refind={exact_version}).")


def _sync_refind_esp_binary(args: argparse.Namespace) -> None:
    """Jalankan ulang skrip resmi 'refind-install' supaya binari rEFInd yang ada
    di partisi EFI (refind_x64.efi) benar-benar cocok dengan versi paket yang
    baru saja dipastikan oleh _ensure_refind_version_pinned.

    Bug yang diperbaiki lewat fungsi ini: mengganti versi PAKET rEFInd lewat
    apt/dnf/zypper (pin_refind_version) hanya mengubah catatan dpkg/rpm/pacman
    -- itu TIDAK otomatis menyalin ulang binari baru ke partisi EFI. Tanpa
    langkah ini, refind_x64.efi di ESP bisa tetap versi LAMA selamanya
    (termasuk versi yang masih kena bug upstream 'showtools' di 0.14.2+) walau
    dpkg sudah melaporkan versi target terpasang, sehingga 'refindmgr declutter'
    maupun pin-versi terlihat "berhasil" tapi tidak berefek apa pun di boot.
    """
    print(
        "Menjalankan skrip resmi 'refind-install' supaya binari rEFInd di partisi EFI\n"
        "benar-benar cocok dengan versi paket yang terpasang (mengganti versi paket saja\n"
        "TIDAK otomatis memperbarui file di partisi EFI)."
    )
    if not args.yes:
        print(
            "Ini baru pratinjau -- belum ada perubahan apa pun yang dibuat.\n"
            "Jalankan ulang dengan 'sudo refindmgr setup --yes' untuk benar-benar menerapkannya."
        )
        return
    if not system_mod.is_root():
        raise CLIError("Perintah ini butuh akses root. Jalankan ulang dengan: sudo refindmgr setup --yes")
    try:
        output = system_mod.run_refind_install()
    except system_mod.BootstrapError as exc:
        print(f"PERINGATAN: gagal menjalankan refind-install: {exc}")
        return
    if output.strip():
        print(output.strip())
    print("Binari rEFInd di partisi EFI sudah disegarkan supaya cocok dengan versi paket saat ini.")


def cmd_setup(args: argparse.Namespace) -> None:
    """Bantu memasang rEFInd itu sendiri jika belum terpasang di sistem ini.

    Semua langkah yang menyentuh partisi EFI/NVRAM didelegasikan ke skrip resmi
    upstream 'refind-install' (bagian dari paket rEFInd), bukan ditulis ulang
    sendiri -- lihat refindmgr/system.py. Tidak ada apa pun yang dijalankan tanpa
    konfirmasi eksplisit lewat flag --yes.

    Selain memasang rEFInd, langkah ini juga selalu mengecek/menyesuaikan versi
    paket rEFInd ke system_mod.TARGET_REFIND_VERSION lewat
    _ensure_refind_version_pinned, untuk menghindari bug upstream 'showtools' di
    rEFInd 0.14.2+ (lihat README bagian Troubleshooting).
    """
    refind_dir = detect_refind_dir(_refind_dir_arg(args))
    manager = system_mod.detect_package_manager()

    if refind_dir is not None:
        print(f"rEFInd sudah terpasang di {refind_dir}. Tidak perlu instalasi ulang.")
        _ensure_refind_version_pinned(args, manager)
        _sync_refind_esp_binary(args)
        return

    print("rEFInd belum terdeteksi terpasang di sistem ini.\n")

    if not system_mod.is_refind_install_available():
        if manager is None:
            raise CLIError(
                "Tidak bisa mendeteksi package manager yang didukung (apt/dnf/pacman/zypper) di sistem ini.\n"
                "Install rEFInd secara manual sesuai distro kamu, lihat panduan resmi:\n"
                "  https://www.rodsbooks.com/refind/installing.html"
            )
        command_str = " ".join(manager.install_command)
        print(f"Paket rEFInd belum terpasang. Perintah yang akan dijalankan:\n  sudo {command_str}\n")
        if not args.yes:
            print(
                "Ini baru pratinjau -- belum ada perubahan apa pun yang dibuat.\n"
                "Jalankan ulang dengan 'sudo refindmgr setup --yes' untuk benar-benar memasangnya,\n"
                "atau jalankan perintah di atas secara manual."
            )
            return
        if not system_mod.is_root():
            raise CLIError("Perintah ini butuh akses root. Jalankan ulang dengan: sudo refindmgr setup --yes")
        try:
            system_mod.install_package(manager)
        except system_mod.BootstrapError as exc:
            raise CLIError(f"Gagal memasang paket rEFInd: {exc}") from exc
        print("Paket rEFInd berhasil dipasang.\n")

    # Sesuaikan versi paket SEBELUM menjalankan refind-install, supaya binari
    # yang disalin ke partisi EFI sudah dari versi paket yang benar.
    _ensure_refind_version_pinned(args, manager)

    print(
        "Menjalankan skrip resmi 'refind-install' untuk memasang rEFInd ke partisi EFI.\n"
        "PERINGATAN: ini akan mengubah konfigurasi boot loader sistem kamu."
    )
    if not args.yes:
        print(
            "Ini baru pratinjau -- belum ada perubahan apa pun yang dibuat.\n"
            "Jalankan ulang dengan 'sudo refindmgr setup --yes' untuk melanjutkan."
        )
        return
    if not system_mod.is_root():
        raise CLIError("Perintah ini butuh akses root. Jalankan ulang dengan: sudo refindmgr setup --yes")
    try:
        output = system_mod.run_refind_install()
    except system_mod.BootstrapError as exc:
        raise CLIError(f"Gagal memasang rEFInd: {exc}") from exc
    if output.strip():
        print(output.strip())

    refind_dir = detect_refind_dir(_refind_dir_arg(args))
    if refind_dir is not None:
        print(f"\nrEFInd berhasil terpasang di {refind_dir}. Coba 'refindmgr doctor' untuk verifikasi.")
    else:
        print(
            "\nrefind-install selesai, tapi refindmgr belum bisa mendeteksi lokasi rEFInd secara otomatis.\n"
            "Cek manual lokasi partisi EFI kamu, lalu jalankan: refindmgr --refind-dir <path> doctor"
        )


# ---------------------------------------------------------------------------
# Menu interaktif -- dipanggil otomatis saat 'refindmgr' dijalankan tanpa subcommand.
# ---------------------------------------------------------------------------

_USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _style(code: str) -> str:
    return code if _USE_COLOR else ""


_RESET = _style("\033[0m")
_BOLD = _style("\033[1m")
_DIM = _style("\033[2m")
_RED = _style("\033[31m")
_GREEN = _style("\033[32m")
_YELLOW = _style("\033[33m")
_CYAN = _style("\033[36m")
_MAGENTA = _style("\033[35m")


def _prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"{label}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    return value or default


def _confirm(label: str, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    try:
        value = input(f"{label} ({hint}): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    if not value:
        return default
    return value in ("y", "yes", "ya")


def _carry(top_args: argparse.Namespace) -> dict:
    """Bawa --refind-dir dari args tingkat atas ke Namespace baru yang dibuat menu."""
    extra: dict = {}
    if hasattr(top_args, "refind_dir"):
        extra["refind_dir"] = top_args.refind_dir
    return extra


def _print_status_banner(top_args: argparse.Namespace) -> None:
    refind_dir = detect_refind_dir(_refind_dir_arg(top_args))
    rule = f"{_CYAN}{'=' * 56}{_RESET}"
    print(rule)
    print(f"  {_BOLD}╭─ refindmgr ─╮{_RESET}")
    print(f"  {_DIM}rEFInd Theme Manager · v{__version__}{_RESET}")
    print(rule)
    if refind_dir is None:
        print(f"  {_RED}x{_RESET} rEFInd belum terdeteksi di lokasi umum.")
        print(f"    {_DIM}Pakai menu '11) Pasang rEFInd itu sendiri (setup)' di bawah, atau set --refind-dir.{_RESET}")
    else:
        installed, active_list = _theme_status(refind_dir)
        active = active_list[0] if active_list else None
        theme_info = f"aktif: {active}" if active else "tidak ada tema aktif"
        print(f"  {_GREEN}v{_RESET} rEFInd terdeteksi: {_DIM}{refind_dir}{_RESET}")
        print(f"  {_GREEN}v{_RESET} {len(installed)} tema terpasang ({theme_info})")
    if system_mod.is_root():
        print(f"  {_GREEN}v{_RESET} Berjalan sebagai root")
    else:
        print(f"  {_YELLOW}o{_RESET} Bukan root {_DIM}(sudo dibutuhkan untuk aksi yang menulis){_RESET}")
    print(rule)


def _require_refind_dir(top_args: argparse.Namespace) -> Optional[Path]:
    """Pastikan folder rEFInd terdeteksi sebelum menu meminta input apa pun.

    Dicek paling awal di setiap handler menu yang butuh rEFInd sudah terpasang,
    supaya pengguna tidak diminta mengisi prompt yang toh akan gagal juga kalau
    foldernya memang belum ada.
    """
    refind_dir = detect_refind_dir(_refind_dir_arg(top_args))
    if refind_dir is None:
        print(
            f"{_RED}Folder rEFInd tidak ditemukan.{_RESET} "
            "Coba menu '11) Pasang rEFInd itu sendiri (setup)' atau jalankan ulang dengan --refind-dir."
        )
    return refind_dir


def _menu_list(top_args: argparse.Namespace) -> None:
    cmd_list(top_args)


def _menu_install(top_args: argparse.Namespace) -> None:
    if _require_refind_dir(top_args) is None:
        return
    print("Pilih tema dari katalog (buka URL di sampingnya untuk melihat preview):\n")
    for index, entry in enumerate(catalog_mod.CATALOG, start=1):
        print(f"  {index}) {entry.name}")
        print(f"     {entry.git_url}")
    choice = _prompt(f"Pilih nomor tema (1-{len(catalog_mod.CATALOG)})")
    if not choice.isdigit() or not 1 <= int(choice) <= len(catalog_mod.CATALOG):
        print("Dibatalkan: nomor tema tidak valid.")
        return
    entry = catalog_mod.CATALOG[int(choice) - 1]
    subdir = None
    if entry.variants:
        print("Pilih varian:")
        for variant_index, (variant_name, _) in enumerate(entry.variants, start=1):
            print(f"  {variant_index}) {variant_name}")
        variant_choice = _prompt(f"Pilih nomor varian (1-{len(entry.variants)})")
        if not variant_choice.isdigit() or not 1 <= int(variant_choice) <= len(entry.variants):
            print("Dibatalkan: nomor varian tidak valid.")
            return
        variant_name, subdir = entry.variants[int(variant_choice) - 1]
        entry_label = f"{entry.name} — {variant_name}"
    else:
        entry_label = entry.name
    if not _confirm(f"Pasang tema '{entry_label}'?", default=True):
        print("Dibatalkan.")
        return
    color_variant = "main"
    if entry.key == "soho":
        print("Pilih warna Rosé Pine: 1) Main  2) Moon  3) Dawn")
        color_choice = _prompt("Pilih nomor warna", "1")
        if color_choice not in {"1", "2", "3"}:
            print("Dibatalkan: nomor warna tidak valid.")
            return
        color_variant = {"1": "main", "2": "moon", "3": "dawn"}[color_choice]
    ns = argparse.Namespace(source=entry.key, name=(None if subdir else entry.install_name), subdir=subdir, color_variant=color_variant, activate=True, **_carry(top_args))
    _menu_loading("Memasang")
    cmd_install(ns)



def _menu_install_source(top_args: argparse.Namespace) -> None:
    if _require_refind_dir(top_args) is None:
        return
    source = _prompt("URL GitHub atau path file ZIP/folder tema")
    if not source:
        print("Dibatalkan.")
        return
    name = _prompt("Nama folder tema (kosong = otomatis)") or None
    if not _confirm("Pasang dan aktifkan tema dari sumber ini?", default=True):
        print("Dibatalkan.")
        return
    _menu_loading("Memasang")
    cmd_install(argparse.Namespace(source=source, name=name, subdir=None, color_variant="main", activate=True, **_carry(top_args)))

def _menu_activate(top_args: argparse.Namespace) -> None:
    refind_dir = _require_refind_dir(top_args)
    if refind_dir is None:
        return
    installed = themes_mod.list_installed(refind_dir)
    print("Tema terpasang: " + (", ".join(installed) if installed else "(tidak ada)"))
    name = _prompt("Nama tema yang diaktifkan")
    if not name:
        print("Dibatalkan.")
        return
    _menu_loading("Mengaktifkan")
    cmd_activate(argparse.Namespace(name=name, **_carry(top_args)))


def _menu_deactivate(top_args: argparse.Namespace) -> None:
    if _require_refind_dir(top_args) is None:
        return
    if not _confirm("Nonaktifkan semua tema (kembali ke tampilan default rEFInd)?"):
        print("Dibatalkan.")
        return
    _menu_loading("Menonaktifkan")
    cmd_deactivate(argparse.Namespace(**_carry(top_args)))


def _menu_remove(top_args: argparse.Namespace) -> None:
    refind_dir = _require_refind_dir(top_args)
    if refind_dir is None:
        return
    installed = themes_mod.list_installed(refind_dir)
    if not installed:
        print("Tidak ada tema terpasang untuk dihapus.")
        return
    print("Pilih tema yang dihapus:")
    for index, name in enumerate(installed, start=1):
        print(f"  {index}) {name}")
    choice = _prompt(f"Pilih nomor tema (1-{len(installed)})")
    if not choice.isdigit() or not 1 <= int(choice) <= len(installed):
        print("Dibatalkan: nomor tema tidak valid.")
        return
    name = installed[int(choice) - 1]
    if not _confirm(f"Hapus tema '{name}'?", default=False):
        print("Dibatalkan.")
        return
    _menu_loading("Menghapus")
    cmd_remove(argparse.Namespace(name=name, **_carry(top_args)))


def _menu_declutter(top_args: argparse.Namespace) -> None:
    if _require_refind_dir(top_args) is None:
        return
    print(
        "Ini akan menyembunyikan ikon-ikon tools yang jarang dipakai di baris bawah\n"
        "rEFInd (shell, memtest, gdisk, mok_tool, about, hidden tags, firmware setup,\n"
        "fwupdate, dll.) dan hanya menyisakan Shutdown & Reboot. Daftar OS di baris\n"
        "atas TIDAK ikut diubah/disembunyikan."
    )
    if not _confirm("Rapikan tampilan boot sekarang?", default=True):
        print("Dibatalkan.")
        return
    cmd_declutter(argparse.Namespace(undo=False, **_carry(top_args)))


def _menu_declutter_undo(top_args: argparse.Namespace) -> None:
    if _require_refind_dir(top_args) is None:
        return
    if not _confirm("Kembalikan tampilan tools rEFInd ke pengaturan bawaan (batalkan declutter)?"):
        print("Dibatalkan.")
        return
    cmd_declutter(argparse.Namespace(undo=True, **_carry(top_args)))



def _menu_loading(action: str = "Menerapkan") -> None:
    """Jeda singkat setelah konfirmasi agar aksi menu terasa jelas, tanpa berlebihan."""
    print(f"\n{action}", end="", flush=True)
    for _ in range(3):
        time.sleep(0.25)
        print(".", end="", flush=True)
    print()


def _menu_clean_menu_auto(top_args: argparse.Namespace) -> None:
    refind_dir = _require_refind_dir(top_args)
    if refind_dir is None:
        return
    conf_path = refind_conf_path(refind_dir)
    lines = conf_mod.read_lines(conf_path)
    detected = _detect_standard_os_loaders(refind_dir, lines)
    if not detected:
        print("Tidak menemukan loader OS standar yang aman untuk dipilih otomatis.")
        print("Gunakan perintah advanced 'refindmgr clean-menu --os Nama=EFI/path/loader.efi' bila layout ESP-nya tidak standar.")
        return
    print("OS ditemukan:")
    for name, path in detected:
        print(f"  - {name}: /{path}")
    if not _confirm("Tampilkan hanya OS ini?", default=False):
        print("Dibatalkan.")
        return
    _menu_loading()
    cmd_clean_menu(argparse.Namespace(os=[], auto=True, apply=True, undo=False, **_carry(top_args)))


def _menu_clean_menu_undo(top_args: argparse.Namespace) -> None:
    if _require_refind_dir(top_args) is None:
        return
    if not _confirm("Batalkan mode OS saja dan pulihkan menu sebelumnya?", default=False):
        print("Dibatalkan.")
        return
    _menu_loading()
    cmd_clean_menu(argparse.Namespace(os=[], auto=False, apply=False, undo=True, **_carry(top_args)))

def _menu_backup(top_args: argparse.Namespace) -> None:
    if _require_refind_dir(top_args) is None:
        return
    _menu_loading("Membuat backup")
    cmd_backup(argparse.Namespace(**_carry(top_args)))


def _menu_restore(top_args: argparse.Namespace) -> None:
    refind_dir = _require_refind_dir(top_args)
    if refind_dir is None:
        return
    backups = conf_mod.list_backups(refind_conf_path(refind_dir))
    if not backups:
        print("Tidak ada file backup ditemukan.")
        return
    print("Backup tersedia (terbaru di paling bawah):")
    for idx, backup_path in enumerate(backups, start=1):
        print(f"  {idx}) {backup_path}")
    choice = _prompt(f"Pilih nomor backup (1-{len(backups)}, kosongkan = paling baru)")
    if not choice:
        backup = None
    elif choice.isdigit() and 1 <= int(choice) <= len(backups):
        backup = str(backups[int(choice) - 1])
    else:
        print(f"{_RED}Input tidak valid: '{choice}'. Masukkan angka 1-{len(backups)} atau kosongkan.{_RESET}")
        return
    _menu_loading("Memulihkan")
    cmd_restore(argparse.Namespace(backup=backup, **_carry(top_args)))


def _menu_doctor(top_args: argparse.Namespace) -> None:
    cmd_doctor(top_args)


def _menu_setup(top_args: argparse.Namespace) -> None:
    yes = _confirm("Jalankan instalasi rEFInd sekarang (bukan hanya pratinjau)?")
    if yes:
        _menu_loading("Menyiapkan")
    cmd_setup(argparse.Namespace(yes=yes, **_carry(top_args)))


_MENU_SECTIONS = [
    ("Tema", [
        ("1", "Lihat tema terpasang & aktif", _menu_list),
        ("2", "Pasang tema dari katalog", _menu_install),
        ("3", "Pasang dari URL GitHub / ZIP / folder", _menu_install_source),
        ("4", "Aktifkan tema", _menu_activate),
        ("5", "Nonaktifkan semua tema", _menu_deactivate),
        ("6", "Hapus tema", _menu_remove),
    ]),
    ("Backup refind.conf", [("7", "Buat backup sekarang", _menu_backup), ("8", "Restore dari backup", _menu_restore)]),
    ("Tampilan boot", [("9", "Hanya tampilkan OS saja", _menu_clean_menu_auto), ("10", "Batalkan mode OS saja", _menu_clean_menu_undo)]),
    ("Sistem", [("11", "Diagnostik (doctor)", _menu_doctor), ("12", "Pasang rEFInd itu sendiri (setup)", _menu_setup)]),
]

_MENU_HANDLERS = {key: handler for _, items in _MENU_SECTIONS for key, _, handler in items}


def _clear_screen() -> None:
    """Bersihkan layar terminal antar-siklus menu agar tidak menumpuk ke bawah.

    Dilewati saat stdout bukan TTY (misal saat dites lewat pipe/CI) supaya output
    yang ditangkap tetap bersih dan tidak berisi kode escape terminal yang tidak
    berguna di luar terminal interaktif sungguhan.
    """
    if sys.stdout.isatty():
        os.system("cls" if os.name == "nt" else "clear")


def run_interactive_menu(top_args: argparse.Namespace) -> None:
    """Menu CLI interaktif -- dipanggil otomatis saat 'refindmgr' dijalankan tanpa subcommand."""
    while True:
        _clear_screen()
        print()
        _print_status_banner(top_args)
        print()
        for section, items in _MENU_SECTIONS:
            print(f"{_BOLD}{section}{_RESET}")
            for key, label, _handler in items:
                print(f"  {_CYAN}{key}){_RESET} {label}")
            print()
        print(f"  {_CYAN}0){_RESET} Keluar\n")
        try:
            choice = input(f"{_BOLD}Pilih menu >{_RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            print("Sampai jumpa!")
            return
        print()
        if choice == "":
            continue
        if choice in ("0", "q", "quit", "exit"):
            print("Sampai jumpa!")
            return
        handler = _MENU_HANDLERS.get(choice)
        if handler is None:
            print(f"{_RED}Pilihan tidak dikenal: '{choice}'{_RESET}")
            continue
        try:
            handler(top_args)
        except CLIError as exc:
            print(f"{_RED}{exc}{_RESET}", file=sys.stderr)
        except PermissionError as exc:
            print(
                f"{_RED}Akses ditolak: {exc}{_RESET}\n"
                "Perintah ini butuh akses root karena menyentuh partisi EFI. Coba ulangi dengan sudo.",
                file=sys.stderr,
            )
        except OSError as exc:
            print(f"{_RED}Terjadi kesalahan sistem: {exc}{_RESET}", file=sys.stderr)
        print()
        try:
            input(f"{_DIM}Tekan Enter untuk kembali ke menu...{_RESET}")
        except (EOFError, KeyboardInterrupt):
            print()
            return


def build_parser() -> argparse.ArgumentParser:
    # Parser bersama untuk --refind-dir, supaya flag ini bisa dipakai baik
    # sebelum maupun setelah nama subcommand, misal:
    #   refindmgr --refind-dir /x list
    #   refindmgr list --refind-dir /x
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--refind-dir",
        default=argparse.SUPPRESS,
        help="Lokasi folder rEFInd (berisi refind.conf). Default: deteksi otomatis.",
    )

    parser = argparse.ArgumentParser(
        prog="refindmgr",
        description="rEFInd Theme Manager -- kelola tema rEFInd tanpa perlu edit manual refind.conf.",
        epilog="Jalankan 'refindmgr' tanpa argumen untuk membuka menu interaktif.",
        parents=[common],
    )
    sub = parser.add_subparsers(dest="command", required=False)

    p_list = sub.add_parser("list", help="Tampilkan tema yang terpasang dan yang aktif.", parents=[common])
    p_list.set_defaults(func=cmd_list)

    p_catalog = sub.add_parser("catalog", help="Tampilkan katalog tema pilihan.", parents=[common])
    p_catalog.set_defaults(func=cmd_catalog)

    p_install = sub.add_parser(
        "install",
        help="Pasang tema dari katalog, URL git, folder lokal, atau file .zip.",
        parents=[common],
    )
    p_install.add_argument("source", help="Key katalog (misal 'minimal') / URL git / path folder / path .zip")
    p_install.add_argument("--name", help="Nama folder tujuan (default: ditebak otomatis dari sumbernya).")
    p_install.add_argument("--activate", action="store_true", help="Langsung aktifkan tema setelah dipasang.")
    p_install.set_defaults(func=cmd_install)

    p_activate = sub.add_parser(
        "activate",
        help="Jadikan tema tertentu aktif (nonaktifkan tema lain otomatis).",
        parents=[common],
    )
    p_activate.add_argument("name")
    p_activate.set_defaults(func=cmd_activate)

    p_deactivate = sub.add_parser(
        "deactivate",
        help="Nonaktifkan semua tema, kembali ke tampilan default rEFInd.",
        parents=[common],
    )
    p_deactivate.set_defaults(func=cmd_deactivate)

    p_remove = sub.add_parser("remove", help="Hapus tema yang terpasang.", parents=[common])
    p_remove.add_argument("name")
    p_remove.set_defaults(func=cmd_remove)

    p_declutter = sub.add_parser(
        "declutter",
        help="Rapikan tampilan boot: sisakan cuma daftar OS + Shutdown + Reboot (sembunyikan ikon tools lain).",
        parents=[common],
    )
    p_declutter.add_argument(
        "--undo",
        action="store_true",
        help="Kembalikan showtools/scanfor ke pengaturan bawaan rEFInd (batalkan declutter sebelumnya).",
    )
    p_declutter.set_defaults(func=cmd_declutter)

    p_dedupe = sub.add_parser(
        "dedupe",
        help="Pratinjau/terapkan penyembunyian kernel atau fallback duplikat dengan validasi aman.",
        parents=[common],
    )
    p_dedupe.add_argument("--apply", action="store_true", help="Terapkan tindakan yang dipilih (tanpa ini hanya pratinjau).")
    p_dedupe.add_argument("--keep-loader", help="Path loader OS yang WAJIB dipertahankan, relatif ke ESP.")
    p_dedupe.add_argument("--disable-kernels", action="store_true", help="Sembunyikan entri kernel mentah/penguin saja.")
    p_dedupe.add_argument("--hide-fallback", help="Sembunyikan satu fallback byte-identik di EFI/BOOT, dengan path spesifik.")
    p_dedupe.set_defaults(func=cmd_dedupe)

    p_clean_menu = sub.add_parser(
        "clean-menu",
        help="Buat menu OS-only aman dari loader yang kamu pilih; preview dulu secara default.",
        parents=[common],
    )
    p_clean_menu.add_argument("--os", action="append", default=[], metavar="NAMA=EFI/PATH/LOADER.EFI",
                              help="OS yang ditampilkan. Ulangi --os untuk setiap OS.")
    p_clean_menu.add_argument("--auto", action="store_true", help="Pilih otomatis loader OS standar (Ubuntu/Windows/Fedora/dll.) dari ESP.")
    p_clean_menu.add_argument("--apply", action="store_true", help="Terapkan menu OS-only (tanpa ini hanya pratinjau).")
    p_clean_menu.add_argument("--undo", action="store_true", help="Pulihkan mode scanfor sebelum clean-menu diterapkan.")
    p_clean_menu.set_defaults(func=cmd_clean_menu)

    p_backup = sub.add_parser("backup", help="Buat backup refind.conf saat ini.", parents=[common])
    p_backup.set_defaults(func=cmd_backup)

    p_restore = sub.add_parser("restore", help="Kembalikan refind.conf dari backup.", parents=[common])
    p_restore.add_argument("--backup", help="Path file backup spesifik (default: backup terbaru).")
    p_restore.set_defaults(func=cmd_restore)

    p_doctor = sub.add_parser(
        "doctor",
        help="Diagnostik: cek folder rEFInd, refind.conf, git, dan akses root.",
        parents=[common],
    )
    p_doctor.set_defaults(func=cmd_doctor)

    p_setup = sub.add_parser(
        "setup",
        help="Bantu memasang rEFInd itu sendiri jika belum terpasang (butuh --yes untuk eksekusi nyata).",
        parents=[common],
    )
    p_setup.add_argument(
        "--yes",
        action="store_true",
        help="Benar-benar jalankan langkah instalasi. Tanpa flag ini hanya pratinjau, tidak ada perubahan.",
    )
    p_setup.set_defaults(func=cmd_setup)

    return parser


def main(argv=None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if getattr(args, "command", None) is None:
            run_interactive_menu(args)
        else:
            args.func(args)
    except CLIError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    except PermissionError as exc:
        print(
            f"Akses ditolak: {exc}\n"
            "Perintah ini butuh akses root karena menyentuh partisi EFI. Coba jalankan lagi dengan sudo.",
            file=sys.stderr,
        )
        sys.exit(1)
    except OSError as exc:
        print(f"Terjadi kesalahan sistem: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
