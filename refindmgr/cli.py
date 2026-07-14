"""Antarmuka CLI untuk refindmgr."""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
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
    covered_names = {
        item.strip().lower()
        for item in MINIMAL_DONT_SCAN_FILES.lstrip("+").strip().split(",")
        if item.strip()
    }
    for rel_path in loader_files:
        basename = rel_path.rsplit("/", 1)[-1].lower()
        is_covered = basename in covered_names or rel_path.lower() in covered_names
        tag = "[disembunyikan oleh declutter]" if is_covered else "[TIDAK disembunyikan]"
        print(f"  {tag}  {rel_path}")
    uncovered = [p for p in loader_files if p.rsplit("/", 1)[-1].lower() not in covered_names and p.lower() not in covered_names]
    if uncovered:
        print(
            "[PERINGATAN]    Ada file .efi yang belum tercakup 'dont_scan_files' di atas. "
            "Kalau salah satu di antaranya ternyata membuat entri OS duplikat yang tidak "
            "diinginkan (ikon generik/kubus/ketupat), tambahkan nama filenya (atau path "
            "relatifnya, misal 'EFI/BOOT/bootx64.efi') ke MINIMAL_DONT_SCAN_FILES di cli.py "
            "lalu jalankan ulang 'refindmgr declutter'."
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
    print(f"  {_BOLD}{_MAGENTA}refindmgr{_RESET}{_DIM} -- rEFInd Theme Manager (v{__version__}){_RESET}")
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
