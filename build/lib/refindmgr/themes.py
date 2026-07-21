"""Pasang, hapus, dan daftar tema rEFInd dari git URL, folder lokal, atau file .zip."""
from __future__ import annotations

import shutil
import stat
import subprocess
import zipfile
from pathlib import Path
from typing import List, Optional

from . import conf as conf_mod
from .paths import refind_conf_path, themes_dir


class ThemeError(Exception):
    """Kesalahan yang dipahami pengguna terkait pemasangan/penghapusan tema."""


def is_git_available() -> bool:
    return shutil.which("git") is not None


def _is_url(source: str) -> bool:
    return (
        source.startswith("http://")
        or source.startswith("https://")
        or source.startswith("git@")
        or source.startswith("ssh://")
        or source.startswith("file://")
    )


def validate_theme_name(name: str) -> None:
    """Pastikan nama tema aman dipakai sebagai satu komponen path di dalam themes/.

    Ini mencegah path traversal (misal --name '../../etc') yang bisa membuat
    refindmgr menulis/menghapus file di luar folder themes/ -- penting karena
    tool ini biasa dijalankan sebagai root di atas partisi EFI.
    """
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        raise ThemeError(
            f"Nama tema tidak valid: '{name}'. Nama tidak boleh kosong dan tidak boleh "
            "berisi '/', '\\', atau berupa '.' / '..'."
        )



def _assert_safe_theme_tree(root: Path) -> None:
    """Tolak symbolic link agar sumber lokal/ZIP tidak menyeret file di luar tema."""
    try:
        for path in root.rglob("*"):
            if path.is_symlink():
                raise ThemeError(f"Tema tidak aman: symbolic link tidak didukung ({path.name}).")
    except OSError as exc:
        raise ThemeError(f"Tidak dapat memeriksa isi tema: {exc}") from exc


def _safe_extract_zip(archive: zipfile.ZipFile, destination: Path) -> None:
    """Ekstrak ZIP tanpa Zip Slip atau symlink, lalu salin stream satu per satu."""
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    for info in archive.infolist():
        member = Path(info.filename)
        if member.is_absolute() or ".." in member.parts:
            raise ThemeError("File zip ditolak: berisi path yang mencoba keluar dari folder tema.")
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise ThemeError("File zip ditolak: symbolic link tidak didukung.")
        target = (destination / member).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ThemeError("File zip ditolak: path tidak aman.") from exc
        if info.is_dir() or info.filename.endswith("/"):
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(info, "r") as src, target.open("wb") as dst:
            shutil.copyfileobj(src, dst)


def _find_theme_conf(root: Path) -> Optional[Path]:
    """Cari theme.conf di root folder, atau satu level di dalamnya."""
    direct = root / "theme.conf"
    if direct.is_file():
        return direct
    if root.is_dir():
        for child in sorted(root.iterdir()):
            if child.is_dir():
                candidate = child / "theme.conf"
                if candidate.is_file():
                    return candidate
    return None


def theme_conf_path(refind_dir: Path, theme_name: str) -> Optional[Path]:
    """Kembalikan path theme.conf milik `theme_name` jika tema itu terpasang,
    atau None jika tidak. Dipakai oleh 'declutter' untuk memeriksa apakah tema
    aktif punya baris 'showtools'/'scanfor' sendiri yang bisa menimpa
    pengaturan refind.conf utama (lihat cli.py: rEFInd memproses 'include'
    secara inline, jadi baris terakhir yang menang, apa pun urutan file-nya).
    """
    candidate = themes_dir(refind_dir) / theme_name / "theme.conf"
    return candidate if candidate.is_file() else None


def list_installed(refind_dir: Path) -> List[str]:
    t_dir = themes_dir(refind_dir)
    if not t_dir.is_dir():
        return []
    names = []
    for child in sorted(t_dir.iterdir()):
        if child.is_dir() and (child / "theme.conf").is_file():
            names.append(child.name)
    return names


def _guess_name_from_url(source: str) -> str:
    tail = source.rstrip("/").rsplit("/", 1)[-1]
    if tail.endswith(".git"):
        tail = tail[: -len(".git")]
    return tail


def install_theme(refind_dir: Path, source: str, name: Optional[str] = None, subdir: Optional[str] = None) -> str:
    """Pasang tema dari git URL, folder lokal, atau file .zip lokal.

    Mengembalikan nama folder tema yang terpasang di dalam themes/.
    Melempar ThemeError untuk semua kegagalan yang dapat dipahami pengguna
    (git tidak ada, theme.conf tidak ditemukan, nama sudah dipakai, nama tidak
    valid/path traversal, dll).
    """
    if name is not None:
        validate_theme_name(name)

    t_dir = themes_dir(refind_dir)
    t_dir.mkdir(parents=True, exist_ok=True)

    if _is_url(source):
        if not is_git_available():
            raise ThemeError(
                "git tidak ditemukan di PATH. Install git terlebih dahulu untuk memasang tema dari URL "
                "(atau download manual sebagai .zip lalu pasang dari file lokal)."
            )
        theme_name = name or _guess_name_from_url(source)
        validate_theme_name(theme_name)
        dest = t_dir / theme_name
        if dest.exists():
            raise ThemeError(f"Tema '{theme_name}' sudah terpasang. Hapus dulu (remove) jika ingin memasang ulang.")
        try:
            result = subprocess.run(
                ["git", "clone", "--depth", "1", source, str(dest)],
                capture_output=True,
                text=True,
            )
        except OSError as exc:
            shutil.rmtree(dest, ignore_errors=True)
            raise ThemeError(f"Gagal menjalankan git clone: {exc}") from exc
        if result.returncode != 0:
            shutil.rmtree(dest, ignore_errors=True)
            raise ThemeError(f"Gagal clone repository: {result.stderr.strip() or result.stdout.strip()}")
        if subdir:
            candidate = (dest / subdir / "theme.conf").resolve()
            try:
                candidate.relative_to(dest.resolve())
            except ValueError:
                shutil.rmtree(dest, ignore_errors=True)
                raise ThemeError("Varian tema tidak aman.")
            theme_conf = candidate if candidate.is_file() else None
        else:
            theme_conf = _find_theme_conf(dest)
        if theme_conf is None:
            shutil.rmtree(dest, ignore_errors=True)
            raise ThemeError("theme.conf tidak ditemukan pada varian/repository ini.")
        _assert_safe_theme_tree(theme_conf.parent)
        if theme_conf.parent != dest:
            _flatten_into(dest, theme_conf.parent)
        git_dir = dest / ".git"
        if git_dir.exists():
            shutil.rmtree(git_dir, ignore_errors=True)
        return dest.name

    src_path = Path(source).expanduser()

    if src_path.is_file() and src_path.suffix.lower() == ".zip":
        theme_name = name or src_path.stem
        validate_theme_name(theme_name)
        dest = t_dir / theme_name
        if dest.exists():
            raise ThemeError(f"Tema '{theme_name}' sudah terpasang. Hapus dulu (remove) jika ingin memasang ulang.")
        extract_tmp = t_dir / f".__extract_{theme_name}"
        if extract_tmp.exists():
            shutil.rmtree(extract_tmp)
        try:
            with zipfile.ZipFile(src_path) as zf:
                _safe_extract_zip(zf, extract_tmp)
            theme_conf = _find_theme_conf(extract_tmp)
            if theme_conf is None:
                raise ThemeError("theme.conf tidak ditemukan di dalam file zip ini.")
            _assert_safe_theme_tree(theme_conf.parent)
            try:
                shutil.copytree(theme_conf.parent, dest)
            except OSError as exc:
                shutil.rmtree(dest, ignore_errors=True)
                raise ThemeError(f"Gagal menyalin tema dari zip: {exc}") from exc
        finally:
            shutil.rmtree(extract_tmp, ignore_errors=True)
        return dest.name

    if src_path.is_dir():
        theme_name = name or src_path.name
        validate_theme_name(theme_name)
        dest = t_dir / theme_name
        if dest.exists():
            raise ThemeError(f"Tema '{theme_name}' sudah terpasang. Hapus dulu (remove) jika ingin memasang ulang.")
        theme_conf = _find_theme_conf(src_path)
        if theme_conf is None:
            raise ThemeError("theme.conf tidak ditemukan di folder ini. Folder lokal harus berisi theme.conf beserta aset tema, bukan file gambar tunggal.")
        _assert_safe_theme_tree(theme_conf.parent)
        try:
            shutil.copytree(theme_conf.parent, dest)
        except OSError as exc:
            shutil.rmtree(dest, ignore_errors=True)
            raise ThemeError(f"Gagal menyalin folder tema: {exc}") from exc
        return dest.name

    if src_path.is_file() and src_path.suffix.lower() in {".png", ".jpg", ".jpeg", ".svg", ".bmp", ".gif", ".webp"}:
        raise ThemeError("File gambar tunggal bukan tema rEFInd. Gunakan folder/ZIP/repository tema yang berisi theme.conf dan asetnya.")

    raise ThemeError(f"Sumber tema tidak dikenali atau tidak ditemukan: {source}")



def patch_sublime_theme(theme_dir: Path) -> None:
    """Normalize Sublime's asset paths to its themes/refind-sublime location."""
    conf_path = theme_dir / "theme.conf"
    if not conf_path.is_file():
        raise ThemeError("theme.conf Sublime tidak ditemukan.")
    text = conf_path.read_text(encoding="utf-8", errors="replace")
    import re
    # Upstream versions use either bare filenames or refind-sublime/... paths.
    text = re.sub(r"(?m)^(banner)\s+background\.png\s*$", r"\1 refind-sublime/background.png", text)
    text = re.sub(r"(?m)^(icons_dir)\s+icons\s*$", r"\1 refind-sublime/icons", text)
    text = re.sub(r"(?m)^(selection_(?:big|small))\s+(selection_(?:big|small)\.png)\s*$", r"\1 refind-sublime/\2", text)
    conf_path.write_text(text, encoding="utf-8")

def patch_rose_pine_theme(theme_dir: Path, variant: str) -> None:
    """Set one documented Rosé Pine main/moon/dawn asset variant."""
    if variant not in {"main", "moon", "dawn"}:
        raise ThemeError("Varian Rosé Pine tidak valid.")
    conf_path = theme_dir / "theme.conf"
    if not conf_path.is_file():
        raise ThemeError("theme.conf Rosé Pine tidak ditemukan.")
    text = conf_path.read_text(encoding="utf-8", errors="replace")
    import re
    text = re.sub(r"banner rose-pine/background/[^\s]+", f"banner rose-pine/background/solid-{variant}.png", text)
    text = re.sub(r"selection_big rose-pine/selection/[^\s]+", f"selection_big rose-pine/selection/{variant}-big.png", text)
    text = re.sub(r"selection_small rose-pine/selection/[^\s]+", f"selection_small rose-pine/selection/{variant}-small.png", text)
    conf_path.write_text(text, encoding="utf-8")

def patch_digital_void_theme(theme_dir: Path) -> None:
    """Perbaiki ketidaksesuaian paket Digital Void upstream.

    ZIP/repo tidak memiliki background.png, tetapi theme.conf bawaan merujuk
    file itu. Akibatnya rEFInd menampilkan banner default. Gunakan background
    green yang memang ada sebagai default dan hilangkan resolution 0 yang
    menimpa pengaturan grafis rEFInd.
    """
    conf_path = theme_dir / "theme.conf"
    default_background = theme_dir / "background.green.png"
    if not conf_path.is_file() or not default_background.is_file():
        raise ThemeError("Paket Digital Void tidak lengkap: theme.conf atau background.green.png tidak ditemukan.")
    text = conf_path.read_text(encoding="utf-8", errors="replace")
    text = text.replace("resolution 0", "resolution max")
    text = text.replace(
        "banner themes/rEFInd-digital-void/background.png",
        "banner themes/rEFInd-digital-void/background.green.png",
    )
    conf_path.write_text(text, encoding="utf-8")

def _flatten_into(dest: Path, inner_dir: Path) -> None:
    """Pindahkan isi inner_dir naik ke dest, lalu hapus inner_dir yang kosong."""
    for item in inner_dir.iterdir():
        target = dest / item.name
        if target.exists():
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        shutil.move(str(item), str(target))
    shutil.rmtree(inner_dir, ignore_errors=True)


def remove_theme(refind_dir: Path, theme_name: str) -> None:
    """Hapus folder tema dan baris include-nya di refind.conf (backup otomatis dibuat)."""
    validate_theme_name(theme_name)
    t_dir = themes_dir(refind_dir)
    theme_path = t_dir / theme_name
    if not theme_path.is_dir():
        raise ThemeError(f"Tema '{theme_name}' tidak ditemukan di {t_dir}.")
    shutil.rmtree(theme_path)

    conf_path = refind_conf_path(refind_dir)
    if conf_path.is_file():
        lines = conf_mod.read_lines(conf_path)
        new_lines = conf_mod.remove_theme_includes(lines, theme_name)
        if new_lines != lines:
            conf_mod.backup(conf_path)
            conf_mod.write_lines(conf_path, new_lines)
