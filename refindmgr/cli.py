"""Antarmuka CLI untuk refindmgr."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import logging
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
from . import preview as preview_mod
from . import firmware_compat as compat_mod
from . import boot_diagnostics as bootdiag_mod
from . import boot_recovery as recovery_mod
from . import os_inventory as osinv_mod
from . import app_logging as log_mod
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


_LOGGER = logging.getLogger("refindmgr.cli")


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
        marker = f"{_GREEN}{_dot()}{_RESET} " if name == active else "  "
        print(f"{marker}{name}")
    if active is None:
        print("\n(Tidak ada tema aktif -- rEFInd memakai tampilan default.)")
    else:
        print(f"\n({_GREEN}{_dot()}{_RESET} = tema aktif saat ini: {active})")
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
        if entry.description:
            print(f"     {entry.description}")
        print(f"     Preview: {entry.git_url}#readme")
    print("\nPasang: refindmgr install <key> --activate")
    print("Tema lain: https://refind-themes-collection.netlify.app/")


def _activate(refind_dir: Path, theme_name: str, include_path: Optional[str] = None) -> None:
    conf_path = refind_conf_path(refind_dir)
    if not conf_path.is_file():
        raise CLIError(f"refind.conf tidak ditemukan di {refind_dir}")
    lines = conf_mod.read_lines(conf_path)
    # Also clean up backup piles created by older refindmgr versions, even if
    # activating this theme turns out to be a no-op.
    conf_mod.list_backups(conf_path)
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
    # A theme may define its own showtools directive. Keep refindmgr's managed
    # OS-only block last so its shutdown/reboot setting always wins, including
    # when a different theme is activated after OS-only mode was enabled.
    new_lines = _move_managed_clean_menu_to_end(new_lines)
    new_lines = _move_managed_firmware_compat_to_end(new_lines)
    if new_lines == lines:
        print(f"Tema '{theme_name}' sudah aktif. Tidak ada perubahan dan tidak membuat backup baru.")
        return
    conf_mod.backup(conf_path)
    conf_mod.write_lines(conf_path, new_lines)
    print(f"Tema '{theme_name}' sekarang aktif. Backup refind.conf sebelumnya sudah disimpan otomatis.")


def cmd_install(args: argparse.Namespace) -> None:
    refind_dir = _resolve_refind_dir(args)
    _warn_if_not_root()
    source = args.source
    catalog_entry = catalog_mod.find(source)
    if catalog_entry:
        source = catalog_entry.git_url
    requested_variant = getattr(args, "variant", None) or getattr(args, "subdir", None)
    if catalog_entry and catalog_entry.key == "soho" and not requested_variant:
        requested_variant = getattr(args, "color_variant", None)
    install_name = getattr(args, "name", None)
    if install_name is None and catalog_entry and catalog_entry.install_name:
        install_name = catalog_entry.install_name
    try:
        print("Memeriksa struktur tema dan mencari varian...", flush=True)
        with themes_mod.prepare_theme_source(
            source,
            allow_insecure_http=getattr(args, "allow_insecure_http", False),
        ) as prepared:
            variants = prepared.variants
            if requested_variant:
                # Catalog subdir names and generic variant keys are both accepted.
                match = next(
                    (v for v in variants if requested_variant.lower() in {
                        v.key.lower(), v.label.lower(), v.config_path.lower()
                    } or requested_variant.lower() in v.config_path.lower()),
                    None,
                )
                if match:
                    requested_variant = match.key
            elif len(variants) > 1:
                print(f"Ditemukan {len(variants)} varian tema:")
                for index, item in enumerate(variants, start=1):
                    print(f"  {index}) {item.label} [{item.key}]")
                if not sys.stdin.isatty():
                    choices = ", ".join(item.key for item in variants)
                    raise CLIError(
                        "Sumber memiliki beberapa varian. Jalankan ulang dengan "
                        f"--variant <nama>. Pilihan: {choices}"
                    )
                try:
                    choice = input(f"Pilih varian (1-{len(variants)}): ").strip()
                except (EOFError, KeyboardInterrupt):
                    raise CLIError("Instalasi dibatalkan.") from None
                if not choice.isdigit() or not 1 <= int(choice) <= len(variants):
                    raise CLIError("Pilihan varian tidak valid; instalasi dibatalkan.")
                requested_variant = variants[int(choice) - 1].key
            installed = themes_mod.install_prepared_theme(
                refind_dir,
                prepared,
                name=install_name,
                variant=requested_variant,
                allow_unsafe_theme=getattr(args, "allow_unsafe_theme", False),
            )
    except themes_mod.ThemeError as exc:
        raise CLIError(f"Gagal memasang tema: {exc}") from exc
    installed_name = installed.name
    print(f"Tema '{installed_name}' varian '{installed.variant}' berhasil dipasang di {installed.path}")
    if getattr(installed, "commit", ""):
        # Catalog/GitHub sources are cloned at HEAD as root onto the ESP. Show
        # what actually landed so the install is auditable and reproducible.
        print(f"  Commit sumber: {installed.commit}")
    for warning in installed.warnings:
        print(f"PERINGATAN: {warning}")
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
    theme_dir = refind_dir / "themes" / args.name
    if theme_dir.is_dir():
        includes = [
            name for _idx, name, _active
            in conf_mod.find_theme_includes(conf_mod.read_lines(refind_conf_path(refind_dir)))
        ]
        if not (theme_dir / "theme.conf").is_file() and args.name not in includes:
            raise CLIError(
                f"Tema '{args.name}' memiliki beberapa konfigurasi varian tanpa theme.conf kanonis. "
                "Pilih varian terlebih dahulu dengan 'refindmgr variant <nama> --set <varian>'."
            )
        legacy_include = None
    else:
        legacy_include = f"{args.name}/theme.conf"
    _activate(refind_dir, args.name, include_path=legacy_include)


def cmd_deactivate(args: argparse.Namespace) -> None:
    refind_dir = _resolve_refind_dir(args)
    _warn_if_not_root()
    conf_path = refind_conf_path(refind_dir)
    if not conf_path.is_file():
        raise CLIError(f"refind.conf tidak ditemukan di {refind_dir}")
    lines = conf_mod.read_lines(conf_path)
    conf_mod.list_backups(conf_path)
    new_lines = conf_mod.deactivate_all(lines)
    if new_lines == lines:
        print("Tidak ada tema aktif. Tidak ada perubahan dan tidak membuat backup baru.")
        return
    conf_mod.backup(conf_path)
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


def cmd_variant(args: argparse.Namespace) -> None:
    refind_dir = _resolve_refind_dir(args)
    try:
        variants = themes_mod.installed_variants(refind_dir, args.name)
    except themes_mod.ThemeError as exc:
        raise CLIError(str(exc)) from exc
    if len(variants) < 2:
        raise CLIError(f"Tema '{args.name}' tidak memiliki lebih dari satu varian.")
    print(f"Varian tersedia untuk '{args.name}':")
    for index, item in enumerate(variants, 1):
        print(f"  {index}) {item.label} [{item.key}]")
    requested = getattr(args, "set_variant", None)
    if not requested:
        if not sys.stdin.isatty():
            return
        choice = input(f"Pilih varian (1-{len(variants)}): ").strip()
        if not choice.isdigit() or not 1 <= int(choice) <= len(variants):
            raise CLIError("Pilihan varian tidak valid.")
        requested = variants[int(choice) - 1].key
    selected = next((v for v in variants if requested.lower() in {v.key.lower(), v.label.lower()}), None)
    if selected is None:
        raise CLIError(f"Varian '{requested}' tidak ditemukan.")
    try:
        changed = themes_mod.switch_variant(refind_dir, args.name, selected.key)
    except themes_mod.ThemeError as exc:
        raise CLIError(str(exc)) from exc
    print(f"Varian tema '{args.name}' sekarang: {changed.variant}. Tidak perlu install ulang.")


# Preset 'declutter': hanya menyisakan Shutdown & Reboot di baris tools (baris
# bawah), dan menjaga baris OS (baris atas) pada metode scan yang paling umum
# (internal/external/optical/manual), tanpa opsi 'firmware' yang menambahkan
# tag boot dari daftar boot firmware -- salah satu sumber tag 'aneh' yang
# sering muncul di layar boot rEFInd, seperti dikeluhkan banyak pengguna.
# Lihat README.md bagian 'Rapikan tampilan boot (declutter)' untuk rincian.
MINIMAL_SHOWTOOLS = "shutdown,reboot"
# NOTE: refindmgr <=1.5.3 also defined MINIMAL_SCANFOR,
# MINIMAL_SCAN_ALL_LINUX_KERNELS and MINIMAL_DONT_SCAN_FILES here. cmd_declutter
# stopped writing all three (it only sets 'showtools' now), so they were dead
# constants carrying ~40 lines of comments describing behaviour that no longer
# exists. Removed rather than left to mislead the next reader.


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


from .hashing import sha256_file as _sha256


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


def cmd_dedupe(args: argparse.Namespace) -> None:
    """Pratinjau atau terapkan pengurangan entri boot secara path-aware dan aman."""
    refind_dir = _resolve_refind_dir(args)
    try:
        compat_status = compat_mod.load_status(refind_dir)
    except compat_mod.FirmwareCompatError as exc:
        raise CLIError(str(exc)) from exc
    if compat_status is not None:
        raise CLIError(
            "Dedupe umum dinonaktifkan saat mode kompatibilitas firmware aktif. "
            "Gunakan 'refindmgr firmware-compat status' agar loader vendor terkelola tidak dianggap duplikat."
        )
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
_CLEAN_MENU_PREVIOUS_SHOWTOOLS = "# refindmgr-clean-menu: previous-showtools="


def _move_managed_clean_menu_to_end(lines: list) -> list:
    """Keep the managed block after theme includes and repair old blocks."""
    start = next((i for i, line in enumerate(lines) if line.strip() == _CLEAN_MENU_BEGIN), None)
    if start is None:
        return list(lines)
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].strip() == _CLEAN_MENU_END),
        None,
    )
    if end is None:
        return list(lines)
    block = list(lines[start:end + 1])
    if not any(line.strip().lower().startswith("showtools ") for line in block):
        block.insert(-1, f"showtools {MINIMAL_SHOWTOOLS}")
    remaining = list(lines[:start]) + list(lines[end + 1:])
    while remaining and not remaining[-1].strip():
        remaining.pop()
    if remaining:
        remaining.append("")
    return remaining + block


def _move_managed_firmware_compat_to_end(lines: list) -> list:
    """Pastikan directive menu kompatibilitas menang atas include tema."""
    marker_pairs = (
        ("# refindmgr-firmware-compat: begin", "# refindmgr-firmware-compat: end"),
        ("# refindmgr-hp-compat: begin", "# refindmgr-hp-compat: end"),
    )
    result = list(lines)
    for begin, end in marker_pairs:
        start = next((i for i, line in enumerate(result) if line.strip() == begin), None)
        if start is None:
            continue
        finish = next((i for i in range(start + 1, len(result)) if result[i].strip() == end), None)
        if finish is None:
            return result
        block = result[start:finish + 1]
        result = result[:start] + result[finish + 1:]
        while result and not result[-1].strip():
            result.pop()
        if result:
            result.append("")
        result.extend(block)
    return result


def _remove_managed_clean_menu(lines: list) -> tuple[list, Optional[str], Optional[str]]:
    """Hapus blok menu manual yang sebelumnya dibuat refindmgr, bila ada."""
    result, previous, previous_showtools, inside = [], None, None, False
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
            elif stripped.startswith(_CLEAN_MENU_PREVIOUS_SHOWTOOLS):
                previous_showtools = stripped[len(_CLEAN_MENU_PREVIOUS_SHOWTOOLS):]
            continue
        result.append(line)
    if inside:
        raise CLIError("Blok clean-menu lama tidak lengkap; pulihkan refind.conf dari backup sebelum melanjutkan.")
    return result, previous, previous_showtools


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
    """Gunakan inventory profil v1.5 lalu terapkan guard konfigurasi rEFInd."""
    inventory = osinv_mod.build_inventory(refind_dir)
    candidates = inventory.menu_entries()
    # Terapkan validasi yang sama seperti input manual. Aturan global lama bisa
    # masih mengecualikan loader; kandidat seperti itu tidak boleh dipilih.
    safe = []
    try:
        own_hash = compat_mod.sha256(compat_mod.refind_binary(refind_dir))
    except (OSError, compat_mod.FirmwareCompatError):
        own_hash = None
    for name, relative_path in candidates:
        try:
            loader_path = _esp_loader_path(refind_dir, relative_path)
            _assert_keep_loader_is_not_excluded(lines, relative_path)
            if own_hash is not None and compat_mod.sha256(loader_path) == own_hash:
                continue
        except CLIError:
            continue
        except OSError:
            continue
        # _parse_os_specs rejects empty names for manual input; the automatic
        # path must apply the same rule, otherwise 'scanfor manual' can be
        # written together with an entry that has no label at all.
        clean = (name or "").strip()
        if not clean:
            continue
        safe.append((clean, relative_path))
    return safe


def cmd_os(args: argparse.Namespace) -> None:
    """Tampilkan inventory OS/loader satu ESP tanpa mengubah apa pun."""
    refind_dir = _resolve_refind_dir(args)
    inventory = osinv_mod.build_inventory(refind_dir)
    if args.action == "baseline":
        snapshot = osinv_mod.create_baseline(refind_dir, inventory)
        destination = Path(args.baseline_file) if getattr(args, "baseline_file", None) else None
        print("=== Baseline kesehatan OS dan loader ===")
        print(f"File yang akan dilacak: {len(snapshot['files'])}")
        compat = snapshot.get("compatibility")
        if compat:
            print(f"Mode kompatibilitas: {compat['state']}")
        if not args.apply:
            print("Tidak ada perubahan. Tambahkan --apply untuk menyimpan baseline.")
            return
        if not system_mod.is_root() and destination is None:
            raise CLIError("Menyimpan baseline sistem membutuhkan sudo.")
        try:
            saved = osinv_mod.save_baseline(snapshot, destination)
        except (OSError, ValueError) as exc:
            raise CLIError(f"Gagal menyimpan baseline: {exc}") from exc
        print(f"Baseline tersimpan: {saved}")
        return
    runtime = inventory.runtime.pretty_name or inventory.runtime.distro_id or "tidak diketahui"
    print("=== Inventory OS dan loader EFI ===")
    print(f"Arsitektur firmware: {inventory.architecture}")
    print(f"OS yang sedang berjalan: {runtime}")
    if not inventory.loaders:
        print("Tidak ada OS dengan profil yang didukung ditemukan pada ESP ini.")
    for item in inventory.loaders:
        marker = _dot() if item.current_os else "-"
        confidence = {
            "verified": "terverifikasi",
            "high": "keyakinan tinggi",
            "medium": "perlu tinjauan",
        }.get(item.confidence, item.confidence)
        print(f"{marker} {item.label}: /{item.path}")
        print(f"  Jenis: {item.kind}; arsitektur: {item.architecture}; {confidence}")
        if args.action == "doctor":
            for evidence in item.evidence:
                print(f"  Bukti: {evidence}")
            for issue in item.issues:
                print(f"  Masalah: {issue}")
    if inventory.warnings:
        print("Peringatan:")
        for warning in inventory.warnings:
            print(f"- {warning}")
    if args.action == "doctor":
        ok, problems = osinv_mod.health_summary(inventory)
        print("\n=== Health check ===")
        for item in ok:
            print(f"[OK] {item}")
        for item in problems:
            print(f"[PERINGATAN] {item}")
        baseline_file = Path(args.baseline_file) if getattr(args, "baseline_file", None) else None
        try:
            previous = osinv_mod.load_baseline(baseline_file)
        except ValueError as exc:
            print(f"[PERINGATAN] {exc}")
            previous = None
        print("\n=== Perubahan sejak baseline ===")
        if previous is None:
            print("Baseline belum tersedia. Simpan dengan: sudo refindmgr os baseline --apply")
        else:
            current = osinv_mod.create_baseline(refind_dir, inventory)
            unchanged, changes = osinv_mod.compare_baseline(previous, current)
            print(f"File tidak berubah: {len(unchanged)}")
            if changes:
                for change in changes:
                    print(f"[PERUBAHAN] {change}")
            else:
                print("Tidak ada perubahan loader yang terdeteksi.")
    print("\nPemeriksaan ini read-only; tidak ada file ESP atau NVRAM yang diubah.")

def cmd_clean_menu(args: argparse.Namespace) -> None:
    """Buat menu OS-only dari stanza manual yang dipilih eksplisit pengguna.

    Tidak menebak mapping ikon->loader dan tidak mem-blacklist loader. Sebagai
    gantinya, scan otomatis dimatikan (scanfor manual) HANYA sesudah pengguna
    memilih loader yang ada di ESP. Ini satu-satunya cara universal untuk
    menyisakan daftar OS tanpa bergantung pada nama grub/shim/fallback distro.
    """
    refind_dir = _resolve_refind_dir(args)
    try:
        compat_status = compat_mod.load_status(refind_dir)
    except compat_mod.FirmwareCompatError as exc:
        raise CLIError(str(exc)) from exc
    if compat_status is not None:
        if not compat_status.managed:
            raise CLIError("Mode kompatibilitas legacy harus di-adopt sebelum menu dapat diperbarui.")
        if args.undo:
            if args.apply and not system_mod.is_root():
                raise CLIError("Pemulihan menu kompatibilitas membutuhkan sudo.")
            try:
                restored = compat_mod.restore_menu(compat_status, apply=args.apply)
            except (OSError, compat_mod.FirmwareCompatError) as exc:
                raise CLIError(str(exc)) from exc
            print("=== Pemulihan menu kompatibilitas ===")
            print(f"Backup: {restored['backup']}")
            if not args.apply:
                print("Tidak ada perubahan. Tambahkan --apply untuk memulihkan.")
            else:
                print("Menu kompatibilitas sebelumnya berhasil dipulihkan.")
            return
        inventory = osinv_mod.build_inventory(refind_dir)
        if args.auto:
            os_entries = inventory.menu_entries()
        else:
            conf_path = refind_conf_path(refind_dir)
            lines = conf_mod.read_lines(conf_path)
            os_entries = _parse_os_specs(args.os, refind_dir, lines)
        if compat_status.data.get("linux_mode") == "direct":
            current_paths = {item.path.casefold() for item in inventory.loaders if item.current_os}
            os_entries = [(name, path) for name, path in os_entries if path.casefold() not in current_paths]
        direct_label = inventory.runtime.pretty_name or "Linux"
        try:
            result = compat_mod.refresh_menu(
                compat_status, os_entries, direct_label=direct_label, apply=args.apply
            )
        except (OSError, compat_mod.FirmwareCompatError) as exc:
            raise CLIError(str(exc)) from exc
        print("=== Menu OS mode kompatibilitas ===")
        if result.get("direct_label"):
            print(f"  - {result['direct_label']}: kernel EFI Stub langsung")
        for name, path in result["entries"]:
            print(f"  - {name}: /{path.lstrip('/')}")
        if not args.apply:
            print("Tidak ada perubahan. Tambahkan --apply untuk menerapkan.")
        else:
            snapshot = osinv_mod.create_baseline(refind_dir, inventory)
            osinv_mod.save_baseline(snapshot)
            print("Menu diperbarui dengan backup dan baseline kesehatan baru.")
        return
    conf_path = refind_conf_path(refind_dir)
    if not conf_path.is_file():
        raise CLIError(f"refind.conf tidak ditemukan di {refind_dir}")
    lines = conf_mod.read_lines(conf_path)
    base_lines, saved_previous, saved_previous_showtools = _remove_managed_clean_menu(lines)

    if args.undo:
        if saved_previous is None:
            raise CLIError("Tidak menemukan konfigurasi clean-menu buatan refindmgr untuk dikembalikan.")
        _warn_if_not_root()
        backup_path = conf_mod.backup(conf_path)
        if saved_previous == "__DEFAULT__":
            restored = conf_mod.unset_global_option(base_lines, "scanfor")
        else:
            restored = conf_mod.set_global_option(base_lines, "scanfor", saved_previous)
        if saved_previous_showtools == "__DEFAULT__":
            restored = conf_mod.unset_global_option(restored, "showtools")
        elif saved_previous_showtools is not None:
            restored = conf_mod.set_global_option(
                restored, "showtools", saved_previous_showtools
            )
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
    old_showtools = conf_mod.get_global_option(base_lines, "showtools")
    previous_showtools = old_showtools if old_showtools is not None else "__DEFAULT__"
    new_lines = conf_mod.set_global_option(base_lines, "scanfor", "manual")
    if new_lines and new_lines[-1].strip():
        new_lines.append("")
    new_lines.extend([
        _CLEAN_MENU_BEGIN,
        _CLEAN_MENU_PREVIOUS + previous,
        _CLEAN_MENU_PREVIOUS_SHOWTOOLS + previous_showtools,
    ])
    for name, relative_path in os_entries:
        new_lines.extend([f'menuentry "{name}" {{', f"    loader /{relative_path}", "}"])
    new_lines.extend([f"showtools {MINIMAL_SHOWTOOLS}", _CLEAN_MENU_END])

    _warn_if_not_root()
    conf_mod.backup(conf_path)
    conf_mod.write_lines(conf_path, new_lines)
    print("Berhasil menerapkan mode OS saja.")
    print("OS yang tampil:")
    for name, path in os_entries:
        print(f"- {name}: /{path}")

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
        backup_path = Path(args.backup).expanduser().resolve()
        known = {path.resolve() for path in backups}
        if backup_path not in known and not getattr(args, "allow_external_backup", False):
            raise CLIError(
                "Path tersebut bukan backup refindmgr yang dikenal. Gunakan backup dari "
                "daftar, atau tambahkan --allow-external-backup setelah memeriksa isinya."
            )
    elif backups:
        backup_path = backups[-1]
    else:
        raise CLIError("Tidak ada file backup ditemukan.")
    conf_mod.restore(conf_path, backup_path)
    print(f"refind.conf dipulihkan dari: {backup_path}")


def _print_result_warnings(result: dict) -> None:
    """Surface warnings that module APIs return in their result dict.

    Several of these describe safety-relevant limits (an NVRAM rollback that
    will not be byte-identical, files skipped during a restore) and used to be
    computed and then dropped on the floor.
    """
    for warning in (result or {}).get("warnings") or []:
        print(f"PERINGATAN: {warning}")


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
        try:
            compat_status = compat_mod.load_status(refind_dir)
        except compat_mod.FirmwareCompatError as exc:
            print(f"[GAGAL] Manifest mode kompatibilitas: {exc}")
            compat_status = None
        if compat_status is not None:
            label = "terkelola" if compat_status.managed else "legacy/belum diadopsi"
            print(f"[OK]    Mode kompatibilitas firmware aktif ({label})")
            print(f"[INFO]  State: {compat_status.state_path}")
            active_loader = compat_status.data.get("active_loader")
            expected_hash = compat_status.data.get("refind_sha256")
            if active_loader and expected_hash:
                try:
                    identity_ok = compat_mod.sha256(Path(active_loader)) == expected_hash
                except OSError:
                    identity_ok = False
                print(f"[{'OK' if identity_ok else 'GAGAL'}]    Identitas loader kompatibilitas")
        _print_esp_loader_audit(refind_dir)
    _print_boot_kernel_audit()
    if getattr(args, "forensic", False) or getattr(args, "export", None) is not None:
        try:
            report = bootdiag_mod.collect_report(
                scan_unmounted=getattr(args, "scan_unmounted", False),
                allow_secure_boot=getattr(args, "allow_secure_boot", False),
            )
        except bootdiag_mod.DiagnosticError as exc:
            raise CLIError(f"Diagnosis forensik gagal: {exc}") from exc
        print()
        print(bootdiag_mod.format_report(report))
        export_value = getattr(args, "export", None)
        if export_value is not None:
            destination = None if export_value == "AUTO" else Path(export_value)
            try:
                archive = bootdiag_mod.export_report(report, destination)
            except OSError as exc:
                raise CLIError(f"Gagal membuat export diagnosis: {exc}") from exc
            print(f"Laporan diagnosis tersensor dibuat: {archive}")


def cmd_preflight(args: argparse.Namespace) -> None:
    """Read-only gate before any automatic refind-install operation."""
    try:
        report = bootdiag_mod.collect_report(
            scan_unmounted=getattr(args, "scan_unmounted", False),
            allow_secure_boot=getattr(args, "allow_secure_boot", False),
        )
    except bootdiag_mod.DiagnosticError as exc:
        raise CLIError(f"Preflight tidak dapat diselesaikan: {exc}") from exc
    print(bootdiag_mod.format_report(report))
    if not report.setup_safe:
        message = (
            "Preflight menghentikan setup otomatis. Tidak ada perubahan pada ESP atau NVRAM.\n"
            "Jalankan 'sudo refindmgr doctor --forensic --scan-unmounted --export' untuk laporan lengkap."
        )
        if any("Secure Boot aktif" in item for item in report.ambiguities):
            message += (
                "\nSecure Boot aktif. Kalau kamu paham risikonya dan punya cara "
                "memulihkan boot, ulangi dengan --allow-secure-boot."
            )
        raise CLIError(message)
    print("Preflight lulus: setup otomatis boleh dilanjutkan.")


def _print_boot_test_state(state: Optional[dict]) -> None:
    print("=== Pengujian boot lintas-reboot ===")
    if state is None:
        print("Status: belum ada pengujian")
        return
    print(f"Fase: {state.get('phase', '-')}")
    print(f"Target: Boot{state.get('target', '-')} {state.get('target_label', '')}")
    print(f"BootOrder awal: {','.join(state.get('original_order') or []) or '-'}")
    if state.get("proposed_order"):
        print(f"BootOrder uji: {','.join(state['proposed_order'])}")
    if state.get("firmware_behavior"):
        print(f"Perilaku firmware: {state['firmware_behavior']}")
    if state.get("recommendation"):
        print(f"Rekomendasi: {state['recommendation']}")


def cmd_boot_test(args: argparse.Namespace) -> None:
    try:
        if args.action == "status":
            _print_boot_test_state(recovery_mod.load_boot_test())
            return
        if args.action == "start":
            if not args.entry:
                raise CLIError("Gunakan --entry XXXX untuk memilih entry uji.")
            report = bootdiag_mod.collect_report(scan_unmounted=True)
            target_file = recovery_mod.validate_boot_target(report, args.entry)
            print(f"Loader target terverifikasi: {target_file.esp_device}:{target_file.relative_path} ({target_file.identity})")
            if args.apply and not system_mod.is_root():
                raise CLIError("Menulis BootNext membutuhkan sudo.")
            state = recovery_mod.start_boot_test(args.entry, apply=args.apply)
            _print_boot_test_state(state)
            if not args.apply:
                print("Tidak ada perubahan. Tambahkan --apply untuk menulis BootNext satu kali.")
            else:
                print("BootNext disiapkan. Setelah reboot, buka 'sudo refindmgr' untuk observasi otomatis.")
            return
        if args.action == "observe":
            if not system_mod.is_root():
                raise CLIError("Menyimpan observasi lintas-reboot membutuhkan sudo.")
            state = recovery_mod.observe_boot_test()
            _print_boot_test_state(state)
            return
        if args.action == "promote":
            if args.apply and not system_mod.is_root():
                raise CLIError("Menulis BootOrder membutuhkan sudo.")
            state = recovery_mod.promote_boot_order(
                apply=args.apply,
                recovery_bundle=Path(args.bundle) if args.bundle else None,
            )
            _print_boot_test_state(state)
            if not args.apply:
                print("Tidak ada perubahan. Tambahkan --apply untuk menguji BootOrder permanen.")
            else:
                print("BootOrder uji ditulis. Setelah reboot, buka 'sudo refindmgr' untuk observasi otomatis.")
            return
        if args.action == "restore":
            if args.apply and not system_mod.is_root():
                raise CLIError("Memulihkan BootOrder membutuhkan sudo.")
            result = recovery_mod.restore_boot_order(apply=args.apply)
            print("BootOrder awal: " + ",".join(result.get("original_order") or []))
            _print_result_warnings(result)
            if result.get("restore_fallback_used"):
                dropped = ", ".join(result.get("restore_dropped_entries") or [])
                print(
                    "PERINGATAN: firmware menolak BootOrder lengkap, jadi dipakai daftar "
                    f"cadangan. Entry yang tidak dapat dipulihkan: {dropped or 'tidak diketahui'}"
                )
            print("Dipulihkan." if args.apply else "Tidak ada perubahan. Tambahkan --apply untuk memulihkan.")
            return
        if args.action == "verify-os":
            if not args.entry or not args.label:
                raise CLIError("verify-os membutuhkan --entry dan --label.")
            if not system_mod.is_root():
                raise CLIError("Menyimpan verifikasi boot membutuhkan sudo.")
            recovery_mod.confirm_manual_boot(args.entry, args.label, args.confirm_booted or "")
            print(f"Boot{args.entry.upper()} dicatat terverifikasi berdasarkan konfirmasi pengguna.")
            return
    except recovery_mod.BootRecoveryError as exc:
        raise CLIError(str(exc)) from exc


def cmd_recovery(args: argparse.Namespace) -> None:
    try:
        if args.action == "validate":
            if not args.bundle:
                raise CLIError("Gunakan --bundle /path/paket.zip.")
            manifest = recovery_mod.validate_recovery_bundle(Path(args.bundle))
            print(f"Paket recovery valid. File terverifikasi: {len(manifest.get('files', {}))}")
            return
        report = bootdiag_mod.collect_report(scan_unmounted=args.scan_unmounted)
        output = Path(args.output) if args.output else Path.cwd() / f"refindmgr-recovery-{time.strftime('%Y%m%d-%H%M%S')}.zip"
        bundle = recovery_mod.create_recovery_bundle(
            report, output, refind_dir=detect_refind_dir(_refind_dir_arg(args))
        )
        print(f"Paket recovery berhasil dibuat dan divalidasi: {bundle}")
    except (recovery_mod.BootRecoveryError, bootdiag_mod.DiagnosticError) as exc:
        raise CLIError(str(exc)) from exc


def cmd_nvram_cleanup(args: argparse.Namespace) -> None:
    try:
        if args.action == "restore":
            if args.apply and not system_mod.is_root():
                raise CLIError("Memulihkan entry NVRAM membutuhkan sudo.")
            result = recovery_mod.restore_deleted_nvram_entry(apply=args.apply)
            _print_result_warnings(result)
            for item in result.get("not_reproduced") or []:
                print(f"  TIDAK dapat direproduksi: {item}")
            if args.apply:
                print(f"Entry dipulihkan sebagai Boot{result['new_entry']} dan BootOrder dipulihkan.")
            else:
                print("Pratinjau restore NVRAM:")
                print("  " + " ".join(result["command"]))
                print("Tidak ada perubahan. Tambahkan --apply untuk memulihkan.")
            return
        report = bootdiag_mod.collect_report(scan_unmounted=args.scan_unmounted)
        classified = recovery_mod.classify_nvram_entries(report.boot, report.esps)
        print("=== Audit cleanup NVRAM ===")
        if not classified:
            print("Tidak ada entry Boot#### yang dapat diklasifikasikan.")
            return
        for item in classified:
            print(
                f"- Boot{item['entry']} {item['label']}: "
                f"[{item['classification']}] {item['reason']}"
            )
        if args.action == "list":
            print("Pratinjau saja; tidak ada entry yang dihapus.")
            return
        if not args.entry or not args.confirm or not args.bundle:
            raise CLIError("delete membutuhkan --entry XXXX --confirm XXXX --bundle recovery.zip.")
        if args.apply and not system_mod.is_root():
            raise CLIError("Menghapus entry NVRAM membutuhkan sudo.")
        result = recovery_mod.delete_nvram_entry(
            args.entry, args.confirm, Path(args.bundle), apply=args.apply, report=report
        )
        _print_result_warnings(result)
        complete = result.get("rollback_complete", True)
        if args.apply:
            print(f"Boot{result['deleted']} berhasil dihapus setelah validasi paket recovery.")
        else:
            rollback = result["rollback"]
            # Do not call this "terverifikasi" when efibootmgr -c cannot
            # reproduce the entry's optional data (Windows Boot Manager's BCD
            # argument blob, GRUB/systemd-boot arguments) or its active flag.
            label = "Rollback terverifikasi" if complete else "Rollback SEBAGIAN"
            print(
                f"{label}: "
                f"{rollback['disk']} partisi {rollback['partition']} -> {rollback['efi_path']}"
            )
            if not complete:
                print(
                    "  Entry ini punya optional data yang tidak dapat dibuat ulang oleh "
                    "'efibootmgr -c'. Entry Windows khususnya bisa gagal boot setelah dipulihkan."
                )
            print("Penghapusan lolos validasi tetapi belum diterapkan. Tambahkan --apply.")
    except (recovery_mod.BootRecoveryError, bootdiag_mod.DiagnosticError) as exc:
        raise CLIError(str(exc)) from exc


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
    rEFInd (di luar folder rEFInd sendiri & EFI/tools), lalu bandingkan dengan
    aturan 'dont_scan_files' yang BENAR-BENAR ada di refind.conf saat ini.

    Catatan: versi sebelumnya mengklaim membandingkan dengan daftar bawaan
    declutter, padahal declutter sudah lama tidak menulis 'dont_scan_files'
    sama sekali -- jadi dokumentasi itu menjanjikan perilaku yang tidak ada.

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
    # Namespace callers from the Python API created before v2 did not carry
    # this field; preserve their old behavior. The real v2 CLI parser always
    # supplies False unless the user explicitly passes --pin-version.
    if not getattr(args, "pin_version", True):
        print("Version pinning dilewati (aktifkan secara eksplisit dengan --pin-version).")
        return
    target = getattr(args, "target_version", None) or system_mod.TARGET_REFIND_VERSION
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
        if manager.name == "apt" and getattr(args, "allow_direct_download", False):
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
                    system_mod.download_refind_deb(
                        target, deb_path,
                        expected_sha256=getattr(args, "deb_sha256", None),
                    )
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
    if refind_dir is not None:
        try:
            compat_status = compat_mod.load_status(refind_dir)
        except compat_mod.FirmwareCompatError as exc:
            raise CLIError(str(exc)) from exc
        if compat_status is not None:
            print(
                f"Mode kompatibilitas firmware aktif di {refind_dir}.\n"
                "Setup/refind-install otomatis dilewati agar loader vendor, manifest, "
                "dan jalur pemulihan tidak tertimpa."
            )
            return
    # A real UEFI setup must pass the same read-only forensic gate used by
    # install.sh. Calling cmd_setup directly therefore cannot bypass it.
    #
    # A dry-run must remain usable without root/ESP access: it only describes
    # package/refind-install actions and returns before any write. Running the
    # forensic scan during preview made ``refindmgr setup`` fail on unprivileged
    # systems (including GitHub Actions) while trying to inspect /boot/efi.
    # Keep the mandatory gate immediately before a confirmed apply instead.
    if (
        refind_dir is None
        and getattr(args, "yes", False)
        and Path("/sys/firmware/efi").is_dir()
    ):
        try:
            preflight = bootdiag_mod.collect_report(scan_unmounted=False)
        except bootdiag_mod.DiagnosticError as exc:
            raise CLIError(f"Preflight setup gagal: {exc}") from exc
        if not preflight.setup_safe:
            print(bootdiag_mod.format_report(preflight))
            raise CLIError(
                "Setup otomatis dihentikan karena layout boot ambigu. CLI tetap aman digunakan.\n"
                "Jalankan 'sudo refindmgr doctor --forensic --scan-unmounted --export'."
            )
    manager = system_mod.detect_package_manager()

    if refind_dir is not None:
        print(f"rEFInd sudah terpasang di {refind_dir}. Tidak perlu instalasi ulang.")
        _ensure_refind_version_pinned(args, manager)
        if getattr(args, "refresh_esp", True):
            _sync_refind_esp_binary(args)
        else:
            print("Binari ESP tidak disentuh. Gunakan --refresh-esp jika memang ingin menjalankan refind-install ulang.")
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
# Firmware compatibility mode
# ---------------------------------------------------------------------------

def _compat_status_from_args(args: argparse.Namespace):
    target = getattr(args, "target_dir", None)
    if target:
        directory = Path(target)
    else:
        # Honour --refind-dir before falling back to the hardcoded /boot/efi,
        # /boot, /efi scan. Without this, pointing at a rescue mount still
        # reported and mutated the HOST's compatibility install.
        explicit = _refind_dir_arg(args)
        directory = None
        if explicit:
            candidate = Path(explicit)
            if compat_mod.state_path(candidate).is_file() or compat_mod.legacy_state_path(candidate).is_file():
                directory = candidate
        if directory is None and not explicit:
            directory = compat_mod.detect_compat_dir()
    if directory is None:
        return None
    try:
        return compat_mod.load_status(directory)
    except compat_mod.FirmwareCompatError as exc:
        raise CLIError(str(exc)) from exc


def _compat_source_dir(args: argparse.Namespace, active_dir: Optional[Path] = None) -> Path:
    source = getattr(args, "source_dir", None)
    if source:
        return Path(source)
    if active_dir is not None:
        candidate = active_dir.parent / "refind"
        if (candidate / "refind.conf").is_file():
            return candidate
    explicit = _refind_dir_arg(args)
    if explicit and Path(explicit).name.lower() == "refind":
        return Path(explicit)
    for candidate in (Path("/boot/efi/EFI/refind"), Path("/boot/EFI/refind"), Path("/efi/EFI/refind")):
        if (candidate / "refind.conf").is_file():
            return candidate
    raise CLIError("Folder rEFInd dedicated tidak ditemukan. Gunakan --source-dir /path/EFI/refind.")


def _print_compat_status(status) -> None:
    print("=== Mode kompatibilitas firmware ===")
    if status is None:
        print("Status: tidak aktif")
        return
    print(f"Status: {'aktif dan terkelola' if status.managed else 'aktif legacy (belum diadopsi)'}")
    print(f"Direktori aktif: {status.active_dir}")
    print(f"State: {status.state_path}")
    print(f"Mode Linux: {status.data.get('linux_mode', 'grub')}")
    if status.data.get("active_loader"):
        print(f"Loader firmware: {status.data['active_loader']}")
    if status.data.get("original_loader_backup") or status.data.get("shim_backup"):
        print(f"Backup loader asli: {status.data.get('original_loader_backup') or status.data.get('shim_backup')}")
    if status.managed:
        try:
            health = compat_mod.reapply_loader(status, apply=False)
            labels = {
                "healthy": "sehat",
                "original-restored": "loader vendor dipulihkan oleh pembaruan sistem",
                "changed": "loader berubah ke hash belum dikenal",
            }
            print(f"Kesehatan loader: {labels.get(health['state'], health['state'])}")
        except (OSError, compat_mod.FirmwareCompatError) as exc:
            print(f"Kesehatan loader: tidak dapat diverifikasi ({exc})")


def cmd_firmware_compat(args: argparse.Namespace) -> None:
    action = args.action
    status = _compat_status_from_args(args)

    if action == "status":
        _print_compat_status(status)
        return

    if action == "enable":
        if status is not None:
            raise CLIError("Mode kompatibilitas sudah aktif. Gunakan status, refresh-kernel, atau restore.")
        source = _compat_source_dir(args)
        target = Path(args.target_dir) if getattr(args, "target_dir", None) else source.parent / args.vendor
        linux_info = None
        linux_mode = "direct" if args.direct_linux else "grub"
        if linux_mode == "direct":
            try:
                linux_info = compat_mod.detect_linux_boot()
            except compat_mod.FirmwareCompatError as exc:
                raise CLIError(str(exc)) from exc
        try:
            plan = compat_mod.plan_install(source, target, vendor=args.vendor, linux_mode=linux_mode, linux_info=linux_info)
        except compat_mod.FirmwareCompatError as exc:
            raise CLIError(str(exc)) from exc
        print("=== Pratinjau mode kompatibilitas firmware ===")
        print(f"rEFInd sumber: {plan['source_binary']}")
        print(f"Path yang diprioritaskan firmware: {plan['active_loader']}")
        print(f"Ubuntu: {'kernel EFI Stub langsung' if linux_mode == 'direct' else plan['grub_loader']}")
        print(f"Windows: {plan['windows_loader']}")
        print("Shim dan refind.conf asli akan dibackup dengan hash di manifest JSON.")
        if not args.apply:
            print("Tidak ada perubahan. Tambahkan --apply untuk menerapkan.")
            return
        if not system_mod.is_root():
            raise CLIError("Mode kompatibilitas harus diterapkan sebagai root (sudo).")
        secure_boot = compat_mod.secure_boot_enabled()
        if secure_boot is True:
            raise CLIError("Secure Boot aktif. Mode kompatibilitas ditolak agar firmware tidak memblokir rEFInd.")
        if secure_boot is None and not args.allow_unknown_secure_boot:
            raise CLIError("Status Secure Boot tidak dapat diverifikasi. Periksa manual atau gunakan --allow-unknown-secure-boot setelah yakin nonaktif.")
        try:
            installed = compat_mod.apply_install(plan)
        except (OSError, compat_mod.FirmwareCompatError) as exc:
            raise CLIError(f"Gagal memasang mode kompatibilitas: {exc}") from exc
        print(f"Mode kompatibilitas berhasil dipasang di {installed.active_dir}.")
        try:
            inventory = osinv_mod.build_inventory(installed.active_dir)
            osinv_mod.save_baseline(osinv_mod.create_baseline(installed.active_dir, inventory))
            print("Baseline kesehatan loader awal berhasil disimpan.")
        except OSError as exc:
            print(f"Peringatan: baseline kesehatan belum tersimpan: {exc}")
        print("Reboot dan uji kedua OS sebelum membersihkan entry NVRAM apa pun.")
        return

    if action == "adopt":
        if status is None or status.managed:
            raise CLIError("Tidak menemukan mode kompatibilitas legacy yang perlu diadopsi.")
        if args.apply and not system_mod.is_root():
            raise CLIError("Adopsi manifest pada ESP membutuhkan sudo.")
        source = _compat_source_dir(args, status.active_dir)
        try:
            adopted = compat_mod.adopt_legacy(status.active_dir, source, apply=args.apply)
        except (OSError, compat_mod.FirmwareCompatError) as exc:
            raise CLIError(str(exc)) from exc
        _print_compat_status(adopted)
        if args.apply:
            print("Mode legacy berhasil diadopsi tanpa mengubah loader atau refind.conf.")
        else:
            print("Tidak ada perubahan. Tambahkan --apply untuk membuat manifest terkelola.")
        return

    if status is None:
        raise CLIError("Mode kompatibilitas tidak aktif.")
    if not status.managed:
        raise CLIError("Mode legacy harus di-adopt terlebih dahulu.")

    if action == "refresh-kernel":
        if not args.apply:
            print("Symlink kernel direct akan diarahkan ke pasangan kernel/initrd EFI Stub terbaru.")
            print("Tidak ada perubahan. Tambahkan --apply untuk menerapkan.")
            return
        if not system_mod.is_root():
            raise CLIError("Refresh kernel membutuhkan sudo.")
        try:
            version = compat_mod.refresh_kernel_links(status)
        except (OSError, compat_mod.FirmwareCompatError) as exc:
            raise CLIError(str(exc)) from exc
        print(f"Symlink direct boot sekarang menunjuk kernel {version}.")
        return

    if action == "reapply":
        if args.apply and not system_mod.is_root():
            raise CLIError("Reapply loader kompatibilitas membutuhkan sudo.")
        try:
            result = compat_mod.reapply_loader(
                status,
                apply=args.apply,
                confirm_current_hash=getattr(args, "confirm_current_hash", None),
            )
        except (OSError, compat_mod.FirmwareCompatError) as exc:
            raise CLIError(str(exc)) from exc
        print("=== Penerapan ulang loader kompatibilitas ===")
        print(f"Loader aktif: {result['active_loader']}")
        print(f"Hash saat ini: {result['current_sha256']}")
        print(f"Hash rEFInd yang diharapkan: {result['expected_sha256']}")
        if result["state"] == "healthy":
            print("Loader masih sehat; tidak ada perubahan yang diperlukan.")
            return
        if not args.apply:
            print("Loader saat ini akan dibackup sebelum rEFInd diterapkan ulang.")
            if result["state"] == "changed":
                print(f"Hash belum dikenal; apply juga membutuhkan --confirm-current-hash {result['confirmation']}")
            print("Tidak ada perubahan. Tambahkan --apply untuk menerapkan.")
            return
        inventory = osinv_mod.build_inventory(status.active_dir)
        osinv_mod.save_baseline(osinv_mod.create_baseline(status.active_dir, inventory))
        print(f"rEFInd berhasil diterapkan ulang. Backup loader sebelumnya: {result['backup']}")
        return

    if action == "restore":
        if args.apply and not system_mod.is_root():
            raise CLIError("Pemulihan loader dan refind.conf membutuhkan sudo.")
        try:
            result = compat_mod.restore(status, apply=args.apply)
        except (OSError, compat_mod.FirmwareCompatError) as exc:
            raise CLIError(str(exc)) from exc
        print("=== Pratinjau pemulihan boot standar ===")
        print(f"Pulihkan loader: {result['loader_backup']} -> {result['active_loader']}")
        if result.get("config_backup"):
            print(f"Pulihkan konfigurasi: {result['config_backup']}")
        # Always show exactly what will be unlinked. An adopted legacy manifest
        # can list arbitrary paths, so the user must see the list before, not
        # after, anything is deleted.
        for path in result.get("files_to_delete") or []:
            print(f"  Hapus: {path}")
        for path in result.get("skipped_files") or []:
            print(f"  DILEWATI (bukan berkas kelolaan refindmgr): {path}")
        _print_result_warnings(result)
        if not args.apply:
            print("Tidak ada perubahan. Tambahkan --apply untuk memulihkan.")
            return
        print("Boot standar berhasil dipulihkan. Rollback perubahan restore:")
        print(result["rollback_dir"])
        return

    raise CLIError(f"Aksi mode kompatibilitas tidak dikenal: {action}")


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


def _unicode_supported(stream=None) -> bool:
    stream = stream or sys.stdout
    encoding = getattr(stream, "encoding", None) or ""
    try:
        "─❯".encode(encoding or "ascii")
    except (LookupError, UnicodeEncodeError):
        return False
    return True


# Zero-argument accessors on purpose: Python 3.9-3.11 f-strings allow neither
# nested same-type quotes (PEP 701, 3.12+) nor backslashes in the expression
# part, so the glyph cannot be chosen inline at the call site.
def _dot() -> str:
    """Active-item marker, degraded to ASCII on a LANG=C terminal."""
    return "\u25cf" if _unicode_supported() else "*"


def _dash() -> str:
    """Em dash, degraded to ASCII on a LANG=C terminal."""
    return "\u2014" if _unicode_supported() else "-"


def _prompt_arrow() -> str:
    return "❯" if sys.stdout.isatty() and _unicode_supported(sys.stdout) else ">"


def _rule_character() -> str:
    return "─" if sys.stdout.isatty() and _unicode_supported(sys.stdout) else "-"

# Exact FIGlet/TAAG "Slant" output for: refindmgr
_REFINDMGR_ASCII = (
    "               ____           __",
    "   _____ ____  / __(_)___  ____/ /___ ___  ____ ______",
    r"  / ___/ / __ \/ /_/ / __ \/ __  / __ `__ \/ __ `/ ___/",
    " / /    / /_/ / __/ / / / / /_/ / / / / / / /_/ / /",
    r"/_/     \____/_/ /_/_/ /_/\__,_/_/ /_/ /_/\__, /_/",
    "                                         /____/",
)


def _prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        value = input(
            f"{_BOLD}{label}{_RESET}{suffix} "
            f"{_MAGENTA}{_prompt_arrow()}{_RESET} "
        ).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    return value or default


def _confirm(label: str, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    try:
        value = input(
            f"{_BOLD}{label}{_RESET} ({hint}) "
            f"{_MAGENTA}{_prompt_arrow()}{_RESET} "
        ).strip().lower()
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
    width = min(72, max(40, shutil.get_terminal_size((60, 24)).columns - 4))
    rule = f"{_CYAN}{_rule_character() * width}{_RESET}"
    for line in _REFINDMGR_ASCII:
        print(f"{_CYAN}{_BOLD}{line}{_RESET}")
    print(f"  {_DIM}v{__version__}{_RESET}")
    print(rule)
    if refind_dir is None:
        print(f"  {_RED}x{_RESET} rEFInd belum terdeteksi di lokasi umum.")
        print(f"    {_DIM}Pakai menu '13) Pasang rEFInd itu sendiri (setup)' di bawah, atau set --refind-dir.{_RESET}")
    else:
        installed, active_list = _theme_status(refind_dir)
        active = active_list[0] if active_list else None
        theme_info = f"aktif: {active}" if active else "tidak ada tema aktif"
        print(f"  {_GREEN}v{_RESET} rEFInd terdeteksi: {_DIM}{refind_dir}{_RESET}")
        print(f"  {_GREEN}v{_RESET} {len(installed)} tema terpasang ({theme_info})")
        try:
            compat_status = compat_mod.load_status(refind_dir)
        except compat_mod.FirmwareCompatError:
            compat_status = None
        if compat_status is not None:
            label = "terkelola" if compat_status.managed else "legacy"
            print(f"  {_GREEN}v{_RESET} mode kompatibilitas firmware: {label}")
    if not system_mod.is_root():
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
            "Coba menu '13) Pasang rEFInd itu sendiri (setup)' atau jalankan ulang dengan --refind-dir."
        )
    return refind_dir


def _menu_list(top_args: argparse.Namespace) -> None:
    cmd_list(top_args)


def _cached_preview_engine(top_args: argparse.Namespace) -> "preview_mod.PreviewEngine":
    """Probe the terminal once per interactive session, before any menu opens."""
    cached = getattr(top_args, "_preview_engine", None)
    if cached is None:
        cached = preview_mod.resolve(
            requested=getattr(top_args, "preview", None),
            symbols=getattr(top_args, "preview_symbols", None),
        )
        setattr(top_args, "_preview_engine", cached)
    return cached


def _cached_sixel_status(top_args: argparse.Namespace) -> tuple[str, str]:
    """Backward-compatible view of the cached preview capability."""
    engine = _cached_preview_engine(top_args)
    return ("ready", "") if engine.available else ("unavailable", engine.reason)


def _catalog_preview_path(entry) -> Optional[Path]:
    """Return the bundled, optimized preview without network access.

    PNG is preferred because the Kitty protocol's direct transfer accepts PNG
    only; the JPEG copy remains for the iTerm2 and character-art backends.
    """
    base = Path(__file__).resolve().parent / "assets" / "previews"
    for suffix in (".png", ".jpg"):
        image = base / f"{entry.key}{suffix}"
        if image.is_file():
            return image
    return None


def _catalog_titles() -> list:
    return [
        f"  {index}. {entry.name}"
        for index, entry in enumerate(catalog_mod.CATALOG, start=1)
    ]


_GRID_TOP_ROW = 3


def _print_catalog_list(engine) -> None:
    """Show the whole catalog at once, as large as the terminal allows.

    Entries are laid out in a grid rather than a single column: stacking eight
    thumbnails vertically means the height budget divided by eight caps how big
    each one can be, while most of a wide terminal sits empty. Two columns
    halve the grid rows and roughly double the thumbnail size for free.
    """
    _clear_screen()
    preview_mod.clear_graphics(engine)

    titles = _catalog_titles()
    columns, rows = preview_mod.terminal_size()
    layout = None
    if engine.available:
        layout = preview_mod.grid_layout(
            columns, rows, len(catalog_mod.CATALOG), max(len(item) for item in titles),
            chrome_rows=_GRID_TOP_ROW + 3,
            cell=engine.caps.cell if engine.caps else None,
        )

    if layout is None:
        print("Katalog tema:\n")
        if not engine.available:
            print(f"Preview gambar tidak tersedia: {engine.reason}.\n")
        else:
            print("Terminal terlalu sempit untuk preview berdampingan.\n")
        for title in titles:
            print(title)
        print()
        return

    print("Katalog tema:")
    failed = ""
    for index, (title, entry) in enumerate(zip(titles, catalog_mod.CATALOG)):
        row_offset, column_offset = layout.position(index)
        row = _GRID_TOP_ROW + row_offset
        column = 1 + column_offset
        # Absolute placement, so a cell never depends on where the previous
        # one left the cursor and the last row cannot scroll the grid away.
        sys.stdout.write(f"\x1b[{row};{column}H{title}")
        sys.stdout.flush()
        image = _catalog_preview_path(entry) if not failed else None
        if image is None:
            continue
        shown, note = preview_mod.render(
            engine, image,
            columns=layout.box_columns, rows=layout.box_rows,
            column=column + layout.image_offset, row=row, advance=False,
        )
        if not shown:
            # Report once and fall back to plain titles; repeating the same
            # failure eight times buries the reason.
            failed = note

    bottom = _GRID_TOP_ROW + layout.grid_rows * layout.pitch
    sys.stdout.write(f"\x1b[{bottom};1H")
    if failed:
        print(f"Preview gagal: {failed}")
    print(f"{_DIM}Preview: {preview_mod.describe(engine)}{_RESET}")
    if preview_mod.framebuffer_viewer(engine.caps) is not None:
        print(f"{_DIM}Ketik g<nomor> (mis. g2) untuk melihat gambar asli layar penuh.{_RESET}")
    sys.stdout.flush()


def _menu_install(top_args: argparse.Namespace) -> None:
    if _require_refind_dir(top_args) is None:
        return
    engine = _cached_preview_engine(top_args)
    total = len(catalog_mod.CATALOG)

    while True:
        _print_catalog_list(engine)
        choice = _prompt(f"Pilih nomor tema (1-{total})").strip()
        # 'g<n>' paints the real image on the console framebuffer. No terminal
        # protocol works on a Linux virtual console, so this is the only way to
        # see an actual picture there rather than character art.
        if choice[:1].lower() == "g" and choice[1:].isdigit():
            index = int(choice[1:])
            if not 1 <= index <= total:
                print("Nomor tema tidak valid.")
                continue
            image = _catalog_preview_path(catalog_mod.CATALOG[index - 1])
            if image is None:
                print("Preview tidak tersedia untuk tema itu.")
                continue
            shown, note = preview_mod.show_fullscreen(image, engine.caps)
            if not shown:
                print(f"Tidak dapat menampilkan gambar: {note}")
                _prompt("Tekan Enter untuk kembali", "")
            continue
        break
    preview_mod.clear_graphics(engine)

    if not choice.isdigit() or not 1 <= int(choice) <= total:
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
        entry_label = f"{entry.name} {_dash()} {variant_name}"
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
    ns = argparse.Namespace(source=entry.key, name=(None if subdir else entry.install_name), subdir=subdir, variant=None, color_variant=color_variant, activate=True, **_carry(top_args))
    with _menu_loading("Memasang"):
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
    with _menu_loading("Memasang"):
        cmd_install(argparse.Namespace(source=source, name=name, subdir=None, color_variant="main", activate=True, **_carry(top_args)))

def _menu_activate(top_args: argparse.Namespace) -> None:
    refind_dir = _require_refind_dir(top_args)
    if refind_dir is None:
        return
    installed = themes_mod.list_installed(refind_dir)
    if not installed:
        print("Tidak ada tema terpasang untuk diaktifkan.")
        return
    active = conf_mod.get_active_theme(conf_mod.read_lines(refind_conf_path(refind_dir)))
    print("Pilih tema yang diaktifkan:")
    for index, name in enumerate(installed, start=1):
        active_note = f" {_GREEN}{_dot()} aktif{_RESET}" if name == active else ""
        print(f"  {index}) {name}{active_note}")
    choice = _prompt(f"Pilih nomor tema (1-{len(installed)})")
    if not choice.isdigit() or not 1 <= int(choice) <= len(installed):
        print("Dibatalkan: nomor tema tidak valid.")
        return
    name = installed[int(choice) - 1]
    with _menu_loading("Mengaktifkan"):
        cmd_activate(argparse.Namespace(name=name, **_carry(top_args)))


def _menu_deactivate(top_args: argparse.Namespace) -> None:
    if _require_refind_dir(top_args) is None:
        return
    if not _confirm("Nonaktifkan semua tema (kembali ke tampilan default rEFInd)?"):
        print("Dibatalkan.")
        return
    with _menu_loading("Menonaktifkan"):
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
    with _menu_loading("Menghapus"):
        cmd_remove(argparse.Namespace(name=name, **_carry(top_args)))


def _menu_variant(top_args: argparse.Namespace) -> None:
    refind_dir = _require_refind_dir(top_args)
    if refind_dir is None:
        return
    candidates = []
    for name in themes_mod.list_installed(refind_dir):
        try:
            if len(themes_mod.installed_variants(refind_dir, name)) > 1:
                candidates.append(name)
        except themes_mod.ThemeError:
            pass
    if not candidates:
        print("Tidak ada tema terpasang yang memiliki beberapa varian.")
        return
    print("Pilih tema:")
    for index, name in enumerate(candidates, 1):
        print(f"  {index}) {name}")
    choice = _prompt(f"Pilih nomor tema (1-{len(candidates)})")
    if not choice.isdigit() or not 1 <= int(choice) <= len(candidates):
        print("Dibatalkan: nomor tidak valid.")
        return
    cmd_variant(argparse.Namespace(name=candidates[int(choice)-1], set_variant=None, **_carry(top_args)))


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



@contextmanager
def _menu_loading(action: str = "Menerapkan"):
    """Spinner ringan hanya selama operasi berjalan, tanpa mengubah layout lain."""
    if not sys.stdout.isatty():
        print(f"\n{action}...")
        yield
        return
    try:
        from rich.console import Console
    except ImportError:
        print(f"\n{action}...", flush=True)
        yield
        return
    console = Console(file=sys.stdout, no_color=bool(os.environ.get("NO_COLOR")))
    with console.status(f"[cyan]{action}...[/cyan]", spinner="dots"):
        yield


def _refind_version_at_least(version: str, minimum: str) -> bool:
    """Compare package versions without letting a junk string crash the menu.

    version_tuple raises on values such as 'unknown' or '(none)', which some
    package managers do emit.
    """
    try:
        return system_mod.version_tuple(version) >= system_mod.version_tuple(minimum)
    except system_mod.BootstrapError:
        return False


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
    manager = system_mod.detect_package_manager()
    installed_version = (
        system_mod.get_installed_refind_version(manager) if manager is not None else None
    )
    if installed_version is not None and _refind_version_at_least(installed_version, "0.14.2"):
        print(
            f"\nrEFInd {installed_version} terdeteksi. Versi 0.14.2+ memiliki bug upstream "
            "yang membuat 'showtools' diabaikan, sehingga semua tombol tetap tampil.\n"
            f"refindmgr bisa menurunkannya ke {system_mod.TARGET_REFIND_VERSION} dan "
            "menyegarkan binari rEFInd di partisi EFI."
        )
        # Spell out every consequence. The only confirmation given so far was
        # "Tampilkan hanya OS ini?", which says nothing about downgrading a
        # system package or letting refind-install rewrite an NVRAM entry.
        print(
            "  Yang akan dijalankan:\n"
            f"    1. Menurunkan paket sistem rEFInd ke {system_mod.TARGET_REFIND_VERSION} lewat package manager.\n"
            "    2. Menjalankan 'refind-install', yang MENULIS ke partisi EFI dan\n"
            "       membuat/mengurutkan ulang entri boot Boot#### di NVRAM."
        )
        if not _confirm(
            "Lanjutkan downgrade paket rEFInd dan tulis ulang ESP/NVRAM?", default=False
        ):
            print("Dilewati: versi rEFInd dan ESP/NVRAM dibiarkan apa adanya.")
            print("Menu OS-only tetap dilanjutkan, tapi 'showtools' mungkin diabaikan rEFInd.")
            _apply_os_only_menu(top_args)
            return
        cmd_setup(argparse.Namespace(
            yes=True, pin_version=True, refresh_esp=True,
            allow_direct_download=False, deb_sha256=None, target_version=None, **_carry(top_args)
        ))
        repaired_version = system_mod.get_installed_refind_version(manager)
        if repaired_version is None or _refind_version_at_least(repaired_version, "0.14.2"):
            print(
                "Mode OS saja dibatalkan karena versi rEFInd yang terkena bug 'showtools' "
                "belum berhasil diperbaiki. Tidak ada konfigurasi menu yang diterapkan."
            )
            return
    _apply_os_only_menu(top_args)


def _apply_os_only_menu(top_args: argparse.Namespace) -> None:
    with _menu_loading():
        cmd_clean_menu(argparse.Namespace(os=[], auto=True, apply=True, undo=False, **_carry(top_args)))


def _menu_clean_menu_undo(top_args: argparse.Namespace) -> None:
    if _require_refind_dir(top_args) is None:
        return
    if not _confirm("Batalkan mode OS saja dan pulihkan menu sebelumnya?", default=False):
        print("Dibatalkan.")
        return
    with _menu_loading():
        cmd_clean_menu(argparse.Namespace(os=[], auto=False, apply=False, undo=True, **_carry(top_args)))

def _menu_backup(top_args: argparse.Namespace) -> None:
    if _require_refind_dir(top_args) is None:
        return
    with _menu_loading("Membuat backup"):
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
    with _menu_loading("Memulihkan"):
        cmd_restore(argparse.Namespace(backup=backup, **_carry(top_args)))


def _menu_doctor(top_args: argparse.Namespace) -> None:
    cmd_doctor(top_args)


def _menu_setup(top_args: argparse.Namespace) -> None:
    yes = _confirm("Jalankan instalasi rEFInd sekarang (bukan hanya pratinjau)?")
    if yes:
        with _menu_loading("Menyiapkan"):
            cmd_setup(argparse.Namespace(
                yes=yes, pin_version=True, refresh_esp=True,
                allow_direct_download=True, target_version=None, **_carry(top_args)
            ))
    else:
        cmd_setup(argparse.Namespace(
            yes=False, pin_version=True, refresh_esp=True,
            allow_direct_download=True, target_version=None, **_carry(top_args)
        ))


def _menu_firmware_compat(top_args: argparse.Namespace) -> None:
    status = _compat_status_from_args(argparse.Namespace(**_carry(top_args)))
    _print_compat_status(status)
    print()
    if status is None:
        if not _confirm("Aktifkan mode kompatibilitas firmware?", default=False):
            print("Dibatalkan.")
            return
        direct = _confirm("Boot Ubuntu langsung tanpa menampilkan GRUB?", default=True)
        if not _confirm("Lanjut setelah backup otomatis dan pemeriksaan Secure Boot?", default=False):
            print("Dibatalkan.")
            return
        cmd_firmware_compat(argparse.Namespace(
            action="enable", source_dir=None, target_dir=None, vendor="ubuntu",
            direct_linux=direct, apply=True, allow_unknown_secure_boot=False,
            **_carry(top_args),
        ))
        return
    if not status.managed:
        if not _confirm("Adopsi mode legacy ini ke manifest aman refindmgr?", default=True):
            print("Dibatalkan.")
            return
        cmd_firmware_compat(argparse.Namespace(
            action="adopt", source_dir=None, target_dir=str(status.active_dir),
            apply=True, vendor="ubuntu", direct_linux=False,
            allow_unknown_secure_boot=False, **_carry(top_args),
        ))
        return
    print("  1) Perbarui daftar OS otomatis")
    print("  2) Refresh symlink kernel direct")
    print("  3) Terapkan ulang rEFInd setelah pembaruan sistem")
    print("  4) Pulihkan boot standar")
    print("  0) Kembali")
    choice = _prompt("Pilih aksi", "0")
    if choice == "1":
        cmd_clean_menu(argparse.Namespace(
            auto=True, os=[], undo=False, apply=True, **_carry(top_args),
        ))
    elif choice == "2":
        cmd_firmware_compat(argparse.Namespace(
            action="refresh-kernel", target_dir=str(status.active_dir), apply=True,
            source_dir=None, vendor="ubuntu", direct_linux=False,
            allow_unknown_secure_boot=False, confirm_current_hash=None, **_carry(top_args),
        ))
    elif choice == "3":
        preview_args = argparse.Namespace(
            action="reapply", target_dir=str(status.active_dir), apply=False,
            source_dir=None, vendor="ubuntu", direct_linux=False,
            allow_unknown_secure_boot=False, confirm_current_hash=None, **_carry(top_args),
        )
        cmd_firmware_compat(preview_args)
        # reapply_loader raises FirmwareCompatError (a RuntimeError), which the
        # cmd_* wrapper converts to CLIError but this direct call does not.
        try:
            health = compat_mod.reapply_loader(status, apply=False)
        except compat_mod.FirmwareCompatError as exc:
            raise CLIError(f"Mode kompatibilitas firmware: {exc}") from exc
        if health["state"] != "healthy" and _confirm("Terapkan ulang setelah backup otomatis?", default=False):
            cmd_firmware_compat(argparse.Namespace(
                **{**vars(preview_args), "apply": True,
                   "confirm_current_hash": health["confirmation"] if health["state"] == "changed" else None}
            ))
    elif choice == "4":
        if not _confirm("Pulihkan shim dan refind.conf asli dari backup manifest?", default=False):
            print("Dibatalkan.")
            return
        cmd_firmware_compat(argparse.Namespace(
            action="restore", target_dir=str(status.active_dir), apply=True,
            source_dir=None, vendor="ubuntu", direct_linux=False,
            allow_unknown_secure_boot=False, confirm_current_hash=None, **_carry(top_args),
        ))


def _menu_boot_diagnostics(top_args: argparse.Namespace) -> None:
    cmd_doctor(argparse.Namespace(
        forensic=True, scan_unmounted=True, export=None, **_carry(top_args)
    ))


def _menu_boot_recovery(top_args: argparse.Namespace) -> None:
    print("  1) Status pengujian BootOrder")
    print("  2) Mulai uji BootNext")
    print("  3) Observasi setelah reboot")
    print("  4) Promosikan ke uji BootOrder")
    print("  5) Pulihkan BootOrder awal")
    print("  6) Buat paket recovery")
    print("  7) Pratinjau cleanup NVRAM")
    print("  0) Kembali")
    choice = _prompt("Pilih aksi", "0")
    if choice == "1":
        cmd_boot_test(argparse.Namespace(action="status", entry=None, label=None, confirm_booted=None, bundle=None, apply=False))
    elif choice == "2":
        entry = _prompt("Entry target, mis. 000A")
        apply = _confirm("Tulis BootNext untuk satu kali reboot?", default=False)
        cmd_boot_test(argparse.Namespace(action="start", entry=entry, label=None, confirm_booted=None, bundle=None, apply=apply))
    elif choice == "3":
        cmd_boot_test(argparse.Namespace(action="observe", entry=None, label=None, confirm_booted=None, bundle=None, apply=False))
    elif choice == "4":
        bundle = _prompt("Path paket recovery tervalidasi")
        apply = _confirm("Uji BootOrder permanen setelah BootNext lulus?", default=False)
        cmd_boot_test(argparse.Namespace(action="promote", entry=None, label=None, confirm_booted=None, bundle=bundle or None, apply=apply))
    elif choice == "5":
        apply = _confirm("Pulihkan BootOrder awal?", default=False)
        cmd_boot_test(argparse.Namespace(action="restore", entry=None, label=None, confirm_booted=None, bundle=None, apply=apply))
    elif choice == "6":
        cmd_recovery(argparse.Namespace(action="create", bundle=None, output=None, scan_unmounted=True, **_carry(top_args)))
    elif choice == "7":
        cmd_nvram_cleanup(argparse.Namespace(action="list", entry=None, confirm=None, bundle=None, apply=False, scan_unmounted=True))


_MENU_SECTIONS = [
    ("Tema", [
        ("1", "Lihat tema terpasang & aktif", _menu_list),
        ("2", "Pasang tema dari katalog", _menu_install),
        ("3", "Pasang dari URL GitHub / ZIP / folder", _menu_install_source),
        ("4", "Aktifkan tema", _menu_activate),
        ("5", "Nonaktifkan semua tema", _menu_deactivate),
        ("6", "Hapus tema", _menu_remove),
        ("7", "Ganti varian tema", _menu_variant),
    ]),
    ("Tampilan boot", [("8", "Hanya tampilkan OS saja", _menu_clean_menu_auto), ("9", "Batalkan mode OS saja", _menu_clean_menu_undo)]),
    ("Backup refind.conf", [("10", "Buat backup sekarang", _menu_backup), ("11", "Restore dari backup", _menu_restore)]),
    ("Sistem", [
        ("12", "Diagnostik (doctor)", _menu_doctor),
        ("13", "Pasang rEFInd itu sendiri (setup)", _menu_setup),
        ("14", "Mode kompatibilitas firmware", _menu_firmware_compat),
        ("15", "Diagnosis forensik multi-ESP", _menu_boot_diagnostics),
        ("16", "Pengujian & pemulihan boot", _menu_boot_recovery),
    ]),
]

_MENU_HANDLERS = {key: handler for _, items in _MENU_SECTIONS for key, _, handler in items}


def _clear_screen() -> None:
    """Bersihkan layar terminal antar-siklus menu agar tidak menumpuk ke bawah.

    Dilewati saat stdout bukan TTY (misal saat dites lewat pipe/CI) supaya output
    yang ditangkap tetap bersih dan tidak berisi kode escape terminal yang tidak
    berguna di luar terminal interaktif sungguhan.
    """
    if sys.stdout.isatty():
        # os.system spawns a shell as root and resolves 'clear' through the
        # inherited PATH.  The escape sequence does the same job with no
        # subprocess: home cursor, erase screen, erase scrollback.
        sys.stdout.write("\x1b[H\x1b[2J\x1b[3J")
        sys.stdout.flush()


def run_interactive_menu(top_args: argparse.Namespace) -> None:
    """Menu CLI interaktif -- dipanggil otomatis saat 'refindmgr' dijalankan tanpa subcommand."""
    # Capability detection is intentionally performed once at startup. Menu 2
    # consumes this cached result and never interrupts the user with y/n.
    _cached_preview_engine(top_args)
    resumed_boot_state = None
    resume_error = None
    if system_mod.is_root():
        try:
            resumed_boot_state = recovery_mod.auto_observe_boot_test()
        except recovery_mod.BootRecoveryError as exc:
            resume_error = str(exc)
    while True:
        _clear_screen()
        print()
        _print_status_banner(top_args)
        if resumed_boot_state is not None:
            print()
            print(f"{_GREEN}Boot test dilanjutkan otomatis setelah reboot.{_RESET}")
            _print_boot_test_state(resumed_boot_state)
            resumed_boot_state = None
        elif resume_error is not None:
            print()
            print(f"{_YELLOW}Boot test belum dapat diamati otomatis: {resume_error}{_RESET}")
            resume_error = None
        print()
        for section, items in _MENU_SECTIONS:
            print(f"{_BOLD}{section}{_RESET}")
            for key, label, _handler in items:
                print(f"  {_CYAN}{key}){_RESET} {label}")
            print()
        print(f"  {_CYAN}0){_RESET} Keluar\n")
        try:
            choice = input(
                f"{_BOLD}Pilih menu{_RESET} "
                f"{_MAGENTA}{_BOLD}{_prompt_arrow()}{_RESET} "
            ).strip()
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
            _LOGGER.exception("Operasi menu gagal: %s", exc)
            print(f"{_RED}{exc}{_RESET}", file=sys.stderr)
        except PermissionError as exc:
            _LOGGER.exception("Akses menu ditolak: %s", exc)
            print(
                f"{_RED}Akses ditolak: {exc}{_RESET}\n"
                "Perintah ini butuh akses root karena menyentuh partisi EFI. Coba ulangi dengan sudo.",
                file=sys.stderr,
            )
        except OSError as exc:
            _LOGGER.exception("Kesalahan sistem pada menu: %s", exc)
            print(f"{_RED}Terjadi kesalahan sistem: {exc}{_RESET}", file=sys.stderr)
        except KeyboardInterrupt:
            print()
            print(f"{_DIM}Operasi dibatalkan.{_RESET}")
        except (
            bootdiag_mod.DiagnosticError,
            recovery_mod.BootRecoveryError,
            compat_mod.FirmwareCompatError,
            themes_mod.ThemeError,
            system_mod.BootstrapError,
        ) as exc:
            # These reach the loop when a menu handler calls a module API
            # directly instead of going through its cmd_* wrapper. They used to
            # escape as a traceback and take the whole interactive session down.
            _LOGGER.exception("Operasi menu gagal: %s", exc)
            print(f"{_RED}{exc}{_RESET}", file=sys.stderr)
        except ValueError as exc:
            _LOGGER.exception("Data sistem tidak terduga pada menu: %s", exc)
            print(
                f"{_RED}Data dari sistem tidak dapat dibaca: {exc}{_RESET}\n"
                "Jalankan 'sudo refindmgr doctor --forensic --export' dan sertakan hasilnya bila melapor.",
                file=sys.stderr,
            )
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
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--preview",
        choices=["auto", "kitty", "iterm", "sixel", "framebuffer", "chafa", "none"],
        default="auto",
        help=(
            "Backend preview katalog. 'auto' memprobe terminal dan memilih "
            "Kitty > iTerm2 > Sixel > framebuffer > chafa. 'framebuffer' menulis "
            "piksel langsung ke /dev/fb0 dan hanya dipakai di konsol Linux. "
            "Bisa juga lewat REFINDMGR_PREVIEW."
        ),
    )
    parser.add_argument(
        "--preview-symbols",
        choices=["auto", "unicode", "ascii"],
        default="auto",
        help=(
            "Karakter untuk preview mode chafa. 'auto' memakai ASCII di konsol "
            "berfont tetap (TERM=linux) dan blok Unicode di terminal lain. "
            "Pakai 'unicode' kalau font konsolmu sebenarnya punya glyph blok. "
            "Bisa juga lewat REFINDMGR_PREVIEW_SYMBOLS."
        ),
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
    p_install.add_argument("source", help="Key katalog (misal 'minimalistic') / URL git / path folder / path .zip")
    p_install.add_argument("--name", help="Nama folder tujuan (default: ditebak otomatis dari sumbernya).")
    p_install.add_argument("--variant", help="Key/nama varian. Jika di terminal dan ada beberapa varian, CLI akan menampilkan pilihan.")
    p_install.add_argument("--activate", action="store_true", help="Langsung aktifkan tema setelah dipasang.")
    p_install.add_argument("--allow-insecure-http", action="store_true", help="Izinkan clone lewat HTTP tanpa TLS (tidak disarankan).")
    p_install.add_argument("--allow-unsafe-theme", action="store_true", help="Izinkan theme.conf dengan directive boot-sensitive setelah ditinjau manual.")
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

    p_variant = sub.add_parser("variant", help="Ganti varian tema terpasang tanpa install ulang.", parents=[common])
    p_variant.add_argument("name", help="Nama tema terpasang.")
    p_variant.add_argument("--set", dest="set_variant", help="Key/nama varian tujuan; tanpa ini daftar varian ditampilkan.")
    p_variant.set_defaults(func=cmd_variant)

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

    p_os = sub.add_parser(
        "os",
        help="Lihat inventory OS/loader EFI atau jalankan health check read-only.",
        parents=[common],
    )
    p_os.add_argument("action", nargs="?", default="list", choices=("list", "doctor", "baseline"))
    p_os.add_argument("--apply", action="store_true", help="Simpan baseline kesehatan loader; default pratinjau.")
    p_os.add_argument("--baseline-file", help="Path baseline alternatif untuk pengujian atau audit.")
    p_os.set_defaults(func=cmd_os)

    p_backup = sub.add_parser("backup", help="Buat backup refind.conf saat ini.", parents=[common])
    p_backup.set_defaults(func=cmd_backup)

    p_restore = sub.add_parser("restore", help="Kembalikan refind.conf dari backup.", parents=[common])
    p_restore.add_argument("--backup", help="Path file backup spesifik (default: backup terbaru).")
    p_restore.add_argument("--allow-external-backup", action="store_true", help="Izinkan file di luar daftar backup refindmgr setelah diperiksa manual.")
    p_restore.set_defaults(func=cmd_restore)

    p_doctor = sub.add_parser(
        "doctor",
        help="Diagnostik: cek folder rEFInd, refind.conf, git, dan akses root.",
        parents=[common],
    )
    p_doctor.add_argument("--forensic", action="store_true", help="Analisis read-only seluruh ESP terdeteksi dan rantai BootCurrent.")
    p_doctor.add_argument("--scan-unmounted", action="store_true", help="Mount ESP tambahan sementara secara read-only (butuh root).")
    p_doctor.add_argument("--export", nargs="?", const="AUTO", metavar="ZIP", help="Buat ZIP laporan tersensor; path opsional.")
    p_doctor.set_defaults(func=cmd_doctor)

    p_preflight = sub.add_parser(
        "preflight",
        help="Periksa apakah layout boot cukup jelas untuk setup otomatis (read-only).",
        parents=[common],
    )
    p_preflight.add_argument(
        "--setup", action="store_true",
        help=argparse.SUPPRESS,  # Diterima untuk kompatibilitas installer lama; tidak berpengaruh.
    )
    p_preflight.add_argument(
        "--allow-secure-boot",
        action="store_true",
        help=(
            "Lanjutkan walau Secure Boot aktif. Tanpa flag ini setup otomatis "
            "sengaja dihentikan, karena rEFInd yang tidak ditandatangani bisa "
            "membuat mesin gagal boot."
        ),
    )
    p_preflight.add_argument("--scan-unmounted", action="store_true", help="Mount ESP tambahan sementara secara read-only (butuh root).")
    p_preflight.set_defaults(func=cmd_preflight)

    p_boot_test = sub.add_parser(
        "boot-test",
        help="Uji BootNext dan BootOrder bertahap dengan state lintas-reboot.",
        parents=[common],
    )
    p_boot_test.add_argument("action", choices=("status", "start", "observe", "promote", "restore", "verify-os"))
    p_boot_test.add_argument("--entry", help="Nomor Boot#### target, mis. 000A.")
    p_boot_test.add_argument("--label", help="Label OS untuk verify-os.")
    p_boot_test.add_argument("--confirm-booted", help="Konfirmasi manual persis BOOT-BERHASIL.")
    p_boot_test.add_argument("--bundle", help="Paket recovery tervalidasi, wajib untuk promote --apply.")
    p_boot_test.add_argument("--apply", action="store_true", help="Terapkan perubahan NVRAM; default pratinjau.")
    p_boot_test.set_defaults(func=cmd_boot_test)

    p_recovery = sub.add_parser(
        "recovery",
        help="Buat atau validasi paket pemulihan boot.",
        parents=[common],
    )
    p_recovery.add_argument("action", choices=("create", "validate"))
    p_recovery.add_argument("--output", help="Path ZIP tujuan untuk create.")
    p_recovery.add_argument("--bundle", help="Path ZIP untuk validate.")
    p_recovery.add_argument("--scan-unmounted", action="store_true", help="Periksa ESP tambahan read-only.")
    p_recovery.set_defaults(func=cmd_recovery)

    p_cleanup = sub.add_parser(
        "nvram-cleanup",
        help="Pratinjau atau hapus satu entry NVRAM terverifikasi.",
        parents=[common],
    )
    p_cleanup.add_argument("action", choices=("list", "delete", "restore"))
    p_cleanup.add_argument("--entry", help="Boot#### yang akan dihapus.")
    p_cleanup.add_argument("--confirm", help="Konfirmasi persis empat digit entry.")
    p_cleanup.add_argument("--bundle", help="Paket recovery tervalidasi yang wajib tersedia.")
    p_cleanup.add_argument("--apply", action="store_true", help="Benar-benar hapus satu entry; default pratinjau.")
    p_cleanup.add_argument("--scan-unmounted", action="store_true", help="Periksa ESP tambahan read-only.")
    p_cleanup.set_defaults(func=cmd_nvram_cleanup)

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
    p_setup.add_argument("--pin-version", action="store_true", help="Sesuaikan versi paket rEFInd secara eksplisit; default-nya tidak mengubah versi.")
    p_setup.add_argument("--target-version", help=f"Versi target bersama --pin-version (default: {system_mod.TARGET_REFIND_VERSION}).")
    p_setup.add_argument(
        "--allow-direct-download", action="store_true",
        help=(
            "Izinkan fallback unduh paket .deb resmi bila repo distro tidak punya versi target. "
            "Wajib disertai --deb-sha256."
        ),
    )
    p_setup.add_argument(
        "--deb-sha256", default=None,
        help=(
            "Checksum SHA-256 paket .deb yang diharapkan. Wajib untuk "
            "--allow-direct-download karena SourceForge mengarahkan ke mirror komunitas "
            "yang isinya tidak terverifikasi."
        ),
    )
    p_setup.add_argument("--refresh-esp", action="store_true", help="Jalankan refind-install ulang pada instalasi yang sudah ada.")
    p_setup.set_defaults(func=cmd_setup)

    p_compat = sub.add_parser(
        "firmware-compat",
        help="Deteksi, pasang, adopsi, kelola, atau pulihkan mode kompatibilitas firmware.",
        parents=[common],
    )
    p_compat.add_argument(
        "action",
        choices=("status", "enable", "adopt", "refresh-kernel", "reapply", "restore"),
    )
    p_compat.add_argument("--source-dir", help="Folder rEFInd dedicated, mis. /boot/efi/EFI/refind.")
    p_compat.add_argument("--target-dir", help="Folder vendor yang diprioritaskan firmware, mis. /boot/efi/EFI/ubuntu.")
    p_compat.add_argument("--vendor", default="ubuntu", help="Nama folder vendor target (default: ubuntu).")
    p_compat.add_argument("--direct-linux", action="store_true", help="Boot kernel Linux langsung via EFI Stub, tanpa GRUB terlihat.")
    p_compat.add_argument("--apply", action="store_true", help="Terapkan perubahan; default hanya pratinjau/status.")
    p_compat.add_argument("--confirm-current-hash", help="Konfirmasi 12 karakter awal hash loader berubah untuk reapply.")
    p_compat.add_argument(
        "--allow-unknown-secure-boot",
        action="store_true",
        help="Izinkan apply bila status Secure Boot tidak dapat dibaca, setelah diperiksa manual.",
    )
    p_compat.set_defaults(func=cmd_firmware_compat)

    return parser


def main(argv=None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    log_path = log_mod.configure()
    command = getattr(args, "command", None) or "interactive"
    _LOGGER.info("Mulai refindmgr version=%s command=%s", __version__, command)
    try:
        if getattr(args, "command", None) is None:
            run_interactive_menu(args)
        else:
            args.func(args)
        _LOGGER.info("Selesai command=%s", command)
    except CLIError as exc:
        _LOGGER.exception("Perintah gagal command=%s error=%s", command, exc)
        print(str(exc), file=sys.stderr)
        if log_path is not None:
            print(f"Detail teknis: {log_path}", file=sys.stderr)
        sys.exit(1)
    except PermissionError as exc:
        _LOGGER.exception("Akses ditolak command=%s error=%s", command, exc)
        print(
            f"Akses ditolak: {exc}\n"
            "Perintah ini butuh akses root karena menyentuh partisi EFI. Coba jalankan lagi dengan sudo.",
            file=sys.stderr,
        )
        if log_path is not None:
            print(f"Detail teknis: {log_path}", file=sys.stderr)
        sys.exit(1)
    except OSError as exc:
        _LOGGER.exception("Kesalahan sistem command=%s error=%s", command, exc)
        print(f"Terjadi kesalahan sistem: {exc}", file=sys.stderr)
        if log_path is not None:
            print(f"Detail teknis: {log_path}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
