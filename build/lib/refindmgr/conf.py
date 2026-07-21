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


def find_manual_stanzas(lines: List[str]) -> List[dict]:
    """Cari semua blok 'menuentry { ... }' (stanza boot manual) di refind.conf.

    Penting: dont_scan_files/dont_scan_dirs/scan_all_linux_kernels/scanfor HANYA
    mengatur proses AUTO-SCAN rEFInd -- semuanya TIDAK BERPENGARUH SAMA SEKALI
    ke stanza 'menuentry' manual yang ditulis langsung di refind.conf. Banyak
    refind.conf-sample (termasuk yang dipasang otomatis oleh refind-install di
    Debian/Ubuntu) menyertakan contoh blok seperti:
        menuentry "Ubuntu" {
            loader /EFI/ubuntu/grubx64.efi
            disabled
        }
    yang normalnya nonaktif lewat baris 'disabled' di dalamnya. Kalau baris
    'disabled' itu ikut terhapus/tidak ada (misalnya waktu refind.conf pernah
    diedit manual), stanza itu jadi AKTIF dan akan selalu muncul sebagai entri
    boot terpisah, TANPA ikon OS (karena tidak ada baris 'icon' di dalamnya) --
    yaitu ikon generik/kubus/ketupat. Ini satu-satunya jenis entri yang tidak
    akan pernah hilang lewat opsi declutter manapun, karena bukan hasil scan.

    Mengembalikan list dict: {"name": str, "start_line": int, "disabled": bool,
    "commented": bool}. Deteksi bertingkat sederhana berbasis kurung kurawal,
    cukup untuk refind.conf yang format standar (tidak menangani kurung kurawal
    di dalam string literal secara khusus, karena itu tidak umum dipakai rEFInd).
    """
    stanzas: List[dict] = []
    i = 0
    n = len(lines)
    while i < n:
        raw = lines[i]
        stripped = raw.strip()
        commented = stripped.startswith("#")
        check = stripped.lstrip("#").strip()
        if check.lower().startswith("menuentry"):
            name = check[len("menuentry"):].strip()
            name = name.split("{", 1)[0].strip().strip('"')
            depth = raw.count("{") - raw.count("}")
            disabled = False
            j = i
            while j < n and (j == i or depth > 0):
                line = lines[j]
                if j != i:
                    depth += line.count("{") - line.count("}")
                body_stripped = line.strip().lstrip("#").strip()
                if body_stripped == "disabled":
                    disabled = True
                j += 1
                if depth <= 0 and j > i:
                    break
            stanzas.append({
                "name": name or "(tanpa nama)",
                "start_line": i,
                "disabled": disabled,
                "commented": commented,
            })
            i = j if j > i else i + 1
        else:
            i += 1
    return stanzas


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


# ---------------------------------------------------------------------------
# Opsi global generik (satu baris 'token nilai'), dipakai misalnya oleh fitur
# 'declutter' untuk mengatur 'showtools' dan 'scanfor' -- lihat cli.py.
# ---------------------------------------------------------------------------


def _global_option_re(token: str) -> re.Pattern:
    return re.compile(rf"^(?P<comment>#\s*)?(?P<token>{re.escape(token)})\b(?P<rest>.*)$", re.IGNORECASE)


def find_global_option(lines: List[str], token: str) -> List[Tuple[int, bool, str]]:
    """Cari semua baris yang mengatur `token` (misal 'showtools' atau 'scanfor'),
    aktif maupun yang dikomentari. Kembalikan list (index_baris, aktif_atau_tidak,
    nilai_parameter_setelah_token)."""
    pattern = _global_option_re(token)
    results = []
    for idx, line in enumerate(lines):
        match = pattern.match(line.strip())
        if match:
            is_active = not match.group("comment")
            results.append((idx, is_active, match.group("rest").strip()))
    return results


def get_global_option(lines: List[str], token: str) -> Optional[str]:
    """Kembalikan nilai baris `token` yang sedang AKTIF, atau None jika tidak ada
    baris aktif untuk token tersebut (rEFInd lalu memakai nilai bawaannya)."""
    for _, is_active, rest in find_global_option(lines, token):
        if is_active:
            return rest
    return None


def set_global_option(lines: List[str], token: str, value: str) -> List[str]:
    """Kembalikan salinan `lines` baru dengan `token` diset ke `value` (baris
    'token value' aktif). Baris `token` aktif pertama yang sudah ada akan
    ditimpa; baris aktif duplikat lainnya (jika ada, biasanya dari edit manual
    yang tidak konsisten) ikut dikomentari supaya cuma satu yang aktif. Baris
    yang sudah dikomentari sebelumnya dibiarkan apa adanya. Jika `token` belum
    pernah muncul sama sekali, baris baru ditambahkan di akhir file."""
    new_lines = list(lines)
    matches = find_global_option(new_lines, token)
    target_line = f"{token} {value}" if value else token
    if matches:
        first_idx = matches[0][0]
        new_lines[first_idx] = target_line
        for idx, is_active, _ in matches[1:]:
            if is_active:
                new_lines[idx] = f"# {new_lines[idx].strip()}"
    else:
        if new_lines and new_lines[-1].strip() != "":
            new_lines.append("")
        new_lines.append(target_line)
    return new_lines


def unset_global_option(lines: List[str], token: str) -> List[str]:
    """Komentari semua baris `token` yang aktif, sehingga rEFInd kembali memakai
    nilai bawaannya sendiri untuk opsi tersebut (dipakai oleh 'declutter --undo')."""
    new_lines = list(lines)
    for idx, is_active, _ in find_global_option(new_lines, token):
        if is_active:
            new_lines[idx] = f"# {new_lines[idx].strip()}"
    return new_lines
