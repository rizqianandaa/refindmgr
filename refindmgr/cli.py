"""Antarmuka CLI untuk refindmgr."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Optional

from . import catalog as catalog_mod
from . import conf as conf_mod
from . import system as system_mod
from . import themes as themes_mod
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
    print("Katalog tema rEFInd yang sudah dikurasi:\n")
    for entry in catalog_mod.CATALOG:
        print(f"  {entry.key:<14} {entry.name}")
        print(f"  {'':<14} {entry.description}")
        print(f"  {'':<14} {entry.git_url}\n")
    print("Pasang salah satu dengan: refindmgr install <key> --activate")
    print("Cari lebih banyak pilihan (140+ tema) di: https://refind-themes-collection.netlify.app/")


def _activate(refind_dir: Path, theme_name: str) -> None:
    conf_path = refind_conf_path(refind_dir)
    if not conf_path.is_file():
        raise CLIError(f"refind.conf tidak ditemukan di {refind_dir}")
    lines = conf_mod.read_lines(conf_path)
    conf_mod.backup(conf_path)
    new_lines = conf_mod.activate_theme(lines, theme_name)
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
        installed_name = themes_mod.install_theme(refind_dir, source, name=args.name)
    except themes_mod.ThemeError as exc:
        raise CLIError(f"Gagal memasang tema: {exc}") from exc
    print(f"Tema '{installed_name}' berhasil dipasang di {refind_dir / 'themes' / installed_name}")
    if args.activate:
        _activate(refind_dir, installed_name)
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


def cmd_declutter(args: argparse.Namespace) -> None:
    """Rapikan tampilan boot rEFInd: sembunyikan ikon tools yang jarang dipakai
    (shell, memtest, gdisk, mok_tool, about, hidden_tags, firmware, fwupdate,
    dll.) dan hanya sisakan Shutdown & Reboot, tanpa mengubah daftar OS yang
    terdeteksi. Semua perubahan ditulis ke refind.conf lewat conf_mod, dengan
    backup otomatis, jadi bisa dibalik lewat 'declutter --undo' atau 'restore'.
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
        new_lines = conf_mod.unset_global_option(new_lines, "scanfor")
        conf_mod.write_lines(conf_path, new_lines)
        print(
            "Tampilan tools rEFInd dikembalikan ke pengaturan bawaan rEFInd sendiri "
            "(baris 'showtools'/'scanfor' yang ditulis refindmgr dikomentari lagi).\n"
            "Backup refind.conf sebelum ini juga sudah disimpan otomatis."
        )
        return
    new_lines = conf_mod.set_global_option(lines, "showtools", MINIMAL_SHOWTOOLS)
    new_lines = conf_mod.set_global_option(new_lines, "scanfor", MINIMAL_SCANFOR)
    conf_mod.write_lines(conf_path, new_lines)
    print(
        "Tampilan boot dirapikan: baris bawah rEFInd sekarang cuma menampilkan "
        "'Shutdown' dan 'Reboot' -- ikon shell/memtest/mok_tool/about/hidden tags/"
        "firmware setup/dll. disembunyikan. Daftar OS di baris atas tidak diubah.\n"
        f"(Ditulis ke refind.conf: 'showtools {MINIMAL_SHOWTOOLS}' dan "
        f"'scanfor {MINIMAL_SCANFOR}'.)\n"
        "Reboot untuk melihat hasilnya. Backup refind.conf sebelum ini sudah "
        "disimpan otomatis -- jalankan 'refindmgr declutter --undo' atau "
        "'refindmgr restore' kapan saja untuk mengembalikannya."
    )


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
            active_list = conf_mod.get_active_themes(conf_mod.read_lines(conf_path))
            if len(active_list) > 1:
                print(
                    f"[PERINGATAN]    Ada {len(active_list)} tema aktif sekaligus di refind.conf "
                    f"({', '.join(active_list)}). Jalankan 'refindmgr activate <nama>' untuk merapikannya."
                )
    git_ok = themes_mod.is_git_available()
    print(f"[{'OK' if git_ok else 'PERINGATAN'}]    git terpasang di PATH" + ("" if git_ok else " (diperlukan untuk install dari URL)"))
    root_ok = system_mod.is_root()
    print(f"[{'OK' if root_ok else 'INFO'}]    dijalankan sebagai root" + ("" if root_ok else " (perlu sudo untuk operasi yang menulis ke EFI)"))


def cmd_setup(args: argparse.Namespace) -> None:
    """Bantu memasang rEFInd itu sendiri jika belum terpasang di sistem ini.

    Semua langkah yang menyentuh partisi EFI/NVRAM didelegasikan ke skrip resmi
    upstream 'refind-install' (bagian dari paket rEFInd), bukan ditulis ulang
    sendiri -- lihat refindmgr/system.py. Tidak ada apa pun yang dijalankan tanpa
    konfirmasi eksplisit lewat flag --yes.
    """
    refind_dir = detect_refind_dir(_refind_dir_arg(args))
    if refind_dir is not None:
        print(f"rEFInd sudah terpasang di {refind_dir}. Tidak perlu instalasi ulang.")
        return

    print("rEFInd belum terdeteksi terpasang di sistem ini.\n")

    if not system_mod.is_refind_install_available():
        manager = system_mod.detect_package_manager()
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
    print(f"  {_BOLD}{_MAGENTA}refindmgr{_RESET}{_DIM} -- rEFInd Theme Manager{_RESET}")
    print(rule)
    if refind_dir is None:
        print(f"  {_RED}x{_RESET} rEFInd belum terdeteksi di lokasi umum.")
        print(f"    {_DIM}Pakai menu '12) Pasang rEFInd itu sendiri (setup)' di bawah, atau set --refind-dir.{_RESET}")
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
            "Coba menu '12) Pasang rEFInd itu sendiri (setup)' atau jalankan ulang dengan --refind-dir."
        )
    return refind_dir


def _menu_list(top_args: argparse.Namespace) -> None:
    cmd_list(top_args)


def _menu_catalog(top_args: argparse.Namespace) -> None:
    cmd_catalog(top_args)


def _menu_install(top_args: argparse.Namespace) -> None:
    if _require_refind_dir(top_args) is None:
        return
    print("Sumber tema: key katalog (misal 'minimal'), URL git, folder lokal, atau file .zip.")
    print("Lihat menu '2) Jelajahi katalog tema' dulu kalau belum punya sumber tema.")
    source = _prompt("Sumber tema")
    if not source:
        print("Dibatalkan (sumber tema tidak boleh kosong).")
        return
    name = _prompt("Nama folder tujuan (kosongkan untuk otomatis)") or None
    activate = _confirm("Langsung aktifkan setelah dipasang?", default=True)
    ns = argparse.Namespace(source=source, name=name, activate=activate, **_carry(top_args))
    cmd_install(ns)


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
    cmd_activate(argparse.Namespace(name=name, **_carry(top_args)))


def _menu_deactivate(top_args: argparse.Namespace) -> None:
    if _require_refind_dir(top_args) is None:
        return
    if not _confirm("Nonaktifkan semua tema (kembali ke tampilan default rEFInd)?"):
        print("Dibatalkan.")
        return
    cmd_deactivate(argparse.Namespace(**_carry(top_args)))


def _menu_remove(top_args: argparse.Namespace) -> None:
    refind_dir = _require_refind_dir(top_args)
    if refind_dir is None:
        return
    installed = themes_mod.list_installed(refind_dir)
    print("Tema terpasang: " + (", ".join(installed) if installed else "(tidak ada)"))
    name = _prompt("Nama tema yang dihapus")
    if not name:
        print("Dibatalkan.")
        return
    if not _confirm(f"Yakin hapus tema '{name}'? Ini tidak bisa dibatalkan"):
        print("Dibatalkan.")
        return
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


def _menu_backup(top_args: argparse.Namespace) -> None:
    if _require_refind_dir(top_args) is None:
        return
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
    cmd_restore(argparse.Namespace(backup=backup, **_carry(top_args)))


def _menu_doctor(top_args: argparse.Namespace) -> None:
    cmd_doctor(top_args)


def _menu_setup(top_args: argparse.Namespace) -> None:
    yes = _confirm("Jalankan instalasi rEFInd sekarang (bukan hanya pratinjau)?")
    cmd_setup(argparse.Namespace(yes=yes, **_carry(top_args)))


_MENU_SECTIONS = [
    (
        "Tema",
        [
            ("1", "Lihat tema terpasang & aktif", _menu_list),
            ("2", "Jelajahi katalog tema", _menu_catalog),
            ("3", "Pasang tema baru", _menu_install),
            ("4", "Aktifkan tema", _menu_activate),
            ("5", "Nonaktifkan semua tema", _menu_deactivate),
            ("6", "Hapus tema", _menu_remove),
        ],
    ),
    (
        "Backup refind.conf",
        [
            ("7", "Buat backup sekarang", _menu_backup),
            ("8", "Restore dari backup", _menu_restore),
        ],
    ),
    (
        "Tampilan boot",
        [
            ("9", "Rapikan tampilan boot (OS + Shutdown + Reboot saja)", _menu_declutter),
            ("10", "Kembalikan tampilan tools ke bawaan rEFInd", _menu_declutter_undo),
        ],
    ),
    (
        "Sistem",
        [
            ("11", "Diagnostik (doctor)", _menu_doctor),
            ("12", "Pasang rEFInd itu sendiri (setup)", _menu_setup),
        ],
    ),
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
