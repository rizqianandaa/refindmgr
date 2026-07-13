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
        print(
            "Tidak menemukan folder rEFInd (refind.conf) secara otomatis.\n"
            "Tentukan lokasinya manual dengan --refind-dir, contoh:\n"
            "  refindmgr --refind-dir /boot/efi/EFI/refind list\n"
            "Belum pernah install rEFInd sama sekali? Coba 'refindmgr setup' dulu.\n"
            "Jalankan 'refindmgr doctor' untuk diagnostik lebih lanjut.",
            file=sys.stderr,
        )
        sys.exit(1)
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


def cmd_list(args: argparse.Namespace) -> None:
    refind_dir = _resolve_refind_dir(args)
    installed = themes_mod.list_installed(refind_dir)
    conf_path = refind_conf_path(refind_dir)
    active_list = []
    if conf_path.is_file():
        active_list = conf_mod.get_active_themes(conf_mod.read_lines(conf_path))
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
        print(f"refind.conf tidak ditemukan di {refind_dir}", file=sys.stderr)
        sys.exit(1)
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
        print(f"Gagal memasang tema: {exc}", file=sys.stderr)
        sys.exit(1)
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
        print(f"Nama tema tidak valid: {exc}", file=sys.stderr)
        sys.exit(1)
    installed = themes_mod.list_installed(refind_dir)
    if args.name not in installed:
        print(
            f"Tema '{args.name}' belum terpasang. Tema yang tersedia: {', '.join(installed) or '(tidak ada)'}",
            file=sys.stderr,
        )
        sys.exit(1)
    _activate(refind_dir, args.name)


def cmd_deactivate(args: argparse.Namespace) -> None:
    refind_dir = _resolve_refind_dir(args)
    _warn_if_not_root()
    conf_path = refind_conf_path(refind_dir)
    if not conf_path.is_file():
        print(f"refind.conf tidak ditemukan di {refind_dir}", file=sys.stderr)
        sys.exit(1)
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
        print(f"Gagal menghapus tema: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"Tema '{args.name}' telah dihapus.")


def cmd_backup(args: argparse.Namespace) -> None:
    refind_dir = _resolve_refind_dir(args)
    _warn_if_not_root()
    conf_path = refind_conf_path(refind_dir)
    if not conf_path.is_file():
        print(f"refind.conf tidak ditemukan di {refind_dir}", file=sys.stderr)
        sys.exit(1)
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
        print("Tidak ada file backup ditemukan.", file=sys.stderr)
        sys.exit(1)
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
            print(
                "Tidak bisa mendeteksi package manager yang didukung (apt/dnf/pacman/zypper) di sistem ini.\n"
                "Install rEFInd secara manual sesuai distro kamu, lihat panduan resmi:\n"
                "  https://www.rodsbooks.com/refind/installing.html",
                file=sys.stderr,
            )
            sys.exit(1)
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
            print("Perintah ini butuh akses root. Jalankan ulang dengan: sudo refindmgr setup --yes", file=sys.stderr)
            sys.exit(1)
        try:
            system_mod.install_package(manager)
        except system_mod.BootstrapError as exc:
            print(f"Gagal memasang paket rEFInd: {exc}", file=sys.stderr)
            sys.exit(1)
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
        print("Perintah ini butuh akses root. Jalankan ulang dengan: sudo refindmgr setup --yes", file=sys.stderr)
        sys.exit(1)
    try:
        output = system_mod.run_refind_install()
    except system_mod.BootstrapError as exc:
        print(f"Gagal memasang rEFInd: {exc}", file=sys.stderr)
        sys.exit(1)
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
        print(f"    {_DIM}Pakai menu '10) Pasang rEFInd' di bawah, atau set --refind-dir.{_RESET}")
    else:
        conf_path = refind_conf_path(refind_dir)
        active = None
        if conf_path.is_file():
            active_list = conf_mod.get_active_themes(conf_mod.read_lines(conf_path))
            active = active_list[0] if active_list else None
        installed_count = len(themes_mod.list_installed(refind_dir))
        theme_info = f"aktif: {active}" if active else "tidak ada tema aktif"
        print(f"  {_GREEN}v{_RESET} rEFInd terdeteksi: {_DIM}{refind_dir}{_RESET}")
        print(f"  {_GREEN}v{_RESET} {installed_count} tema terpasang ({theme_info})")
    if system_mod.is_root():
        print(f"  {_GREEN}v{_RESET} Berjalan sebagai root")
    else:
        print(f"  {_YELLOW}o{_RESET} Bukan root {_DIM}(sudo dibutuhkan untuk aksi yang menulis){_RESET}")
    print(rule)


def _menu_list(top_args: argparse.Namespace) -> None:
    cmd_list(top_args)


def _menu_catalog(top_args: argparse.Namespace) -> None:
    cmd_catalog(top_args)


def _menu_install(top_args: argparse.Namespace) -> None:
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
    refind_dir = detect_refind_dir(_refind_dir_arg(top_args))
    if refind_dir:
        installed = themes_mod.list_installed(refind_dir)
        print("Tema terpasang: " + (", ".join(installed) if installed else "(tidak ada)"))
    name = _prompt("Nama tema yang diaktifkan")
    if not name:
        print("Dibatalkan.")
        return
    cmd_activate(argparse.Namespace(name=name, **_carry(top_args)))


def _menu_deactivate(top_args: argparse.Namespace) -> None:
    if not _confirm("Nonaktifkan semua tema (kembali ke tampilan default rEFInd)?"):
        print("Dibatalkan.")
        return
    cmd_deactivate(argparse.Namespace(**_carry(top_args)))


def _menu_remove(top_args: argparse.Namespace) -> None:
    refind_dir = detect_refind_dir(_refind_dir_arg(top_args))
    if refind_dir:
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


def _menu_backup(top_args: argparse.Namespace) -> None:
    cmd_backup(argparse.Namespace(**_carry(top_args)))


def _menu_restore(top_args: argparse.Namespace) -> None:
    refind_dir = detect_refind_dir(_refind_dir_arg(top_args))
    if refind_dir:
        backups = conf_mod.list_backups(refind_conf_path(refind_dir))
        if backups:
            print("Backup tersedia (terbaru di bawah):")
            for backup_path in backups:
                print(f"  - {backup_path}")
    backup = _prompt("Path backup (kosongkan untuk pakai yang terbaru)") or None
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
        "Sistem",
        [
            ("9", "Diagnostik (doctor)", _menu_doctor),
            ("10", "Pasang rEFInd itu sendiri (setup)", _menu_setup),
        ],
    ),
]

_MENU_HANDLERS = {key: handler for _, items in _MENU_SECTIONS for key, _, handler in items}


def run_interactive_menu(top_args: argparse.Namespace) -> None:
    """Menu CLI interaktif -- dipanggil otomatis saat 'refindmgr' dijalankan tanpa subcommand."""
    while True:
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
        except SystemExit:
            # cmd_* memanggil sys.exit() untuk kegagalan validasi -- tangkap di sini
            # supaya menu tetap berjalan, bukan menutup seluruh program.
            pass
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
