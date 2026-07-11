"""Pasang, hapus, dan daftar tema rEFInd dari git URL, folder lokal, atau file .zip."""
from __future__ import annotations

import shutil
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
    )


def validate_theme_name(name: str) -> None:
    """Pastikan nama tema aman dipakai sebagai satu komponen path di dalam themes/.

    Ini mencegah path traversal (misal --name '../../etc') yang bisa membuat
    refindtm menulis/menghapus file di luar folder themes/ -- penting karena
    tool ini biasa dijalankan sebagai root di atas partisi EFI.
    """
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        raise ThemeError(
            f"Nama tema tidak valid: '{name}'. Nama tidak boleh kosong dan tidak boleh "
            "berisi '/', '\\', atau berupa '.' / '..'."
        )


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


def install_theme(refind_dir: Path, source: str, name: Optional[str] = None) -> str:
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
        result = subprocess.run(
            ["git", "clone", "--depth", "1", source, str(dest)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            shutil.rmtree(dest, ignore_errors=True)
            raise ThemeError(f"Gagal clone repository: {result.stderr.strip() or result.stdout.strip()}")
        theme_conf = _find_theme_conf(dest)
        if theme_conf is None:
            shutil.rmtree(dest, ignore_errors=True)
            raise ThemeError("theme.conf tidak ditemukan di repository ini. Pastikan ini repo tema rEFInd yang valid.")
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
                zf.extractall(extract_tmp)
            theme_conf = _find_theme_conf(extract_tmp)
            if theme_conf is None:
                raise ThemeError("theme.conf tidak ditemukan di dalam file zip ini.")
            shutil.copytree(theme_conf.parent, dest)
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
            raise ThemeError("theme.conf tidak ditemukan di folder ini.")
        shutil.copytree(theme_conf.parent, dest)
        return dest.name

    raise ThemeError(f"Sumber tema tidak dikenali atau tidak ditemukan: {source}")


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
