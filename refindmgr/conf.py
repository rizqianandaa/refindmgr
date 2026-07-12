"""Baca/ubah refind.conf dengan aman: backup otomatis, edit baris 'include themes/...'."""
from __future__ import annotations

import re
import shutil
import time
from pathlib import Path
from typing import List, Optional, Tuple

# Mencocokkan baris seperti:
#   include themes/rEFInd-minimal/theme.conf
#   # include themes/rEFInd-minimal/theme.conf
INCLUDE_RE = re.compile(
    r"^(?P<comment>#\s*)?include\s+themes[\\/](?P<name>[^\\/]+)[\\/]theme\.conf\s*$",
    re.IGNORECASE,
)


def read_lines(conf_path: Path) -> List[str]:
    return conf_path.read_text(encoding="utf-8", errors="replace").splitlines()


def write_lines(conf_path: Path, lines: List[str]) -> None:
    content = "\n".join(lines)
    if not content.endswith("\n"):
        content += "\n"
    conf_path.write_text(content, encoding="utf-8")


def backup(conf_path: Path) -> Path:
    """Simpan salinan refind.conf dengan nama berstempel waktu, kembalikan path-nya.

    Nama file dijamin unik (menambah sufiks angka jika perlu) supaya dua backup
    yang dibuat dalam detik yang sama tidak saling menimpa satu sama lain.
    """
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    candidate = conf_path.with_name(f"{conf_path.name}.{timestamp}.bak")
    suffix = 1
    while candidate.exists():
        candidate = conf_path.with_name(f"{conf_path.name}.{timestamp}-{suffix}.bak")
        suffix += 1
    shutil.copy2(conf_path, candidate)
    return candidate


def restore(conf_path: Path, backup_path: Path) -> None:
    shutil.copy2(backup_path, conf_path)


def list_backups(conf_path: Path) -> List[Path]:
    pattern = f"{conf_path.name}.*.bak"
    return sorted(conf_path.parent.glob(pattern))


def find_theme_includes(lines: List[str]) -> List[Tuple[int, str, bool]]:
    """Kembalikan list (index_baris, nama_tema, aktif_atau_tidak) untuk setiap baris
    'include themes/<nama>/theme.conf', aktif maupun yang dikomentari."""
    results = []
    for idx, line in enumerate(lines):
        match = INCLUDE_RE.match(line.strip())
        if match:
            is_active = not match.group("comment")
            results.append((idx, match.group("name"), is_active))
    return results


def get_active_themes(lines: List[str]) -> List[str]:
    """Kembalikan semua nama tema yang aktif (tidak dikomentari). Normalnya cuma
    satu, tapi bisa lebih dari satu jika refind.conf diedit manual secara tidak
    konsisten -- ini pola yang berguna untuk mendeteksi misconfigurasi."""
    return [name for _, name, is_active in find_theme_includes(lines) if is_active]


def get_active_theme(lines: List[str]) -> Optional[str]:
    active = get_active_themes(lines)
    return active[0] if active else None


def activate_theme(lines: List[str], theme_name: str) -> List[str]:
    """Kembalikan salinan `lines` baru dengan hanya `theme_name` yang aktif;
    tema lain otomatis dikomentari. Jika baris include untuk `theme_name` belum
    ada, baris baru ditambahkan di akhir file."""
    new_lines = list(lines)
    found = False
    for idx, name, is_active in find_theme_includes(new_lines):
        target_line = f"include themes/{name}/theme.conf"
        if name == theme_name:
            new_lines[idx] = target_line
            found = True
        elif is_active:
            new_lines[idx] = f"# {target_line}"
    if not found:
        if new_lines and new_lines[-1].strip() != "":
            new_lines.append("")
        new_lines.append(f"include themes/{theme_name}/theme.conf")
    return new_lines


def deactivate_all(lines: List[str]) -> List[str]:
    """Komentari semua baris include tema yang aktif (kembali ke tampilan default)."""
    new_lines = list(lines)
    for idx, name, is_active in find_theme_includes(new_lines):
        if is_active:
            new_lines[idx] = f"# include themes/{name}/theme.conf"
    return new_lines


def remove_theme_includes(lines: List[str], theme_name: str) -> List[str]:
    """Hapus seluruh baris include (aktif maupun dikomentari) untuk `theme_name`."""
    return [
        line
        for line in lines
        if not (
            (match := INCLUDE_RE.match(line.strip())) is not None
            and match.group("name") == theme_name
        )
    ]
