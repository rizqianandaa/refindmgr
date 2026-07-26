"""Baca/ubah refind.conf dengan aman: backup otomatis, edit baris 'include themes/...'."""
from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path
from typing import List, Optional, Tuple


DEFAULT_BACKUP_LIMIT = 5

# Mencocokkan konfigurasi kanonis dan konfigurasi varian, misalnya:
#   include themes/rEFInd-minimal/theme.conf
#   include themes/catppuccin/macchiato.conf
#   # include themes/catppuccin/mocha.conf
#   include themes/foo/theme.conf   # dengan komentar di belakang
#
# ``name`` sengaja menolak '.' murni ('.' / '..') supaya
# 'include themes/../etc/passwd.conf' tidak pernah terbaca sebagai nama tema.
# ``trailer`` menangkap komentar di belakang baris; tanpa grup ini baris yang
# diberi komentar oleh pengguna tidak akan pernah cocok, sehingga tema tidak
# bisa dinonaktifkan dan include kedua ikut ditambahkan.
INCLUDE_RE = re.compile(
    r"^(?P<comment>#\s*)?include\s+themes[\\/](?P<name>(?!\.{1,2}(?:[\\/]|$))[^\\/\s]+)[\\/]"
    r"(?P<config>[^#\r\n]*?\.conf)"
    r"(?P<trailer>\s*(?:#[^\r\n]*)?)$",
    re.IGNORECASE,
)


def _include_target(match: re.Match) -> str:
    config = match.group("config").replace("\\", "/")
    return f"themes/{match.group('name')}/{config}"


def _include_line(match: re.Match, *, active: bool) -> str:
    """Rebuild an include line, preserving any trailing comment the user wrote."""
    trailer = (match.groupdict().get("trailer") or "").strip()
    suffix = f"  {trailer}" if trailer else ""
    prefix = "" if active else "# "
    return f"{prefix}include {_include_target(match)}{suffix}"


def read_lines(conf_path: Path) -> List[str]:
    """Read refind.conf without ever destroying bytes we cannot decode.

    ``errors="replace"`` used to turn a latin-1 menu title into U+FFFD and the
    next write committed that replacement character permanently.  With
    ``surrogateescape`` the undecodable bytes survive a read/write round trip.
    ``split("\\n")`` replaces ``splitlines()`` because the latter also breaks on
    form feed, NEL and other control characters, silently restructuring stanzas.
    """
    data = conf_path.read_bytes()
    text = data.decode("utf-8", errors="surrogateescape")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def write_lines(conf_path: Path, lines: List[str]) -> None:
    """Atomically replace a config file on the same filesystem.

    A power loss can no longer leave ``refind.conf`` half-written: data is
    flushed to a temporary sibling and then committed with ``os.replace``.
    """
    content = "\n".join(lines)
    if not content.endswith("\n"):
        content += "\n"
    temp = conf_path.with_name(f".{conf_path.name}.refindmgr-{os.getpid()}.tmp")
    try:
        with temp.open("w", encoding="utf-8", errors="surrogateescape", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if conf_path.exists():
            # The ESP is vfat, which derives modes from the fmask/dmask mount
            # options and has no per-file mode bits.  chmod is a no-op there and
            # can fail outright on some mounts, so it must never abort the write.
            try:
                os.chmod(temp, conf_path.stat().st_mode)
            except OSError:
                pass
        os.replace(temp, conf_path)
        try:
            directory_fd = os.open(conf_path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        temp.unlink(missing_ok=True)


def backup(conf_path: Path) -> Path:
    """Simpan salinan refind.conf dengan nama berstempel waktu, kembalikan path-nya.

    Nama file dijamin unik (menambah sufiks angka jika perlu) supaya dua backup
    yang dibuat dalam detik yang sama tidak saling menimpa satu sama lain.
    """
    # Do not create another snapshot when the newest backup already contains
    # exactly the current config. This prevents repeated activate/no-op actions
    # from producing identical files.
    backups = list_backups(conf_path)
    if backups and _files_equal(conf_path, backups[-1]):
        return backups[-1]

    timestamp = time.strftime("%Y%m%d-%H%M%S")
    candidate = conf_path.with_name(f"{conf_path.name}.{timestamp}.bak")
    suffix = 1
    while candidate.exists():
        candidate = conf_path.with_name(f"{conf_path.name}.{timestamp}-{suffix}.bak")
        suffix += 1
    shutil.copy2(conf_path, candidate)
    _prune_backups(conf_path)
    return candidate


def restore(conf_path: Path, backup_path: Path) -> None:
    shutil.copy2(backup_path, conf_path)


def list_backups(conf_path: Path) -> List[Path]:
    """Return retained backups, oldest first.

    This used to call ``_prune_backups`` and therefore *deleted* files merely to
    display them.  Pruning now happens only in :func:`backup`, where a new
    snapshot is actually created.
    """
    return _all_backups(conf_path)[-_backup_limit():]


def _backup_limit() -> int:
    try:
        return max(1, int(os.environ.get("REFINDMGR_BACKUP_LIMIT", str(DEFAULT_BACKUP_LIMIT))))
    except ValueError:
        return DEFAULT_BACKUP_LIMIT


# refind.conf.20260101-120000.bak / refind.conf.20260101-120000-2.bak
_BACKUP_STAMP_RE = re.compile(r"^(?P<stamp>\d{8}-\d{6})(?:-(?P<seq>\d+))?$")


def _backup_sort_key(path: Path, prefix_len: int) -> Tuple[int, str, int, str]:
    """Order backups by the timestamp encoded in their own filename.

    Sorting by name is wrong because '-' (0x2D) sorts before '.' (0x2E), so
    ``...-120000-1.bak`` lands before ``...-120000.bak`` and the *oldest*
    snapshot ends up last.  Sorting by mtime is also wrong: ``shutil.copy2``
    copies the source mtime, so every backup carries the identical timestamp of
    refind.conf itself.  The filename is the only trustworthy ordering.
    """
    middle = path.name[prefix_len:-len(".bak")]
    match = _BACKUP_STAMP_RE.match(middle)
    if not match:
        # Unrecognized names sort oldest so they are pruned first, never crash.
        return (0, "", 0, path.name)
    return (1, match.group("stamp"), int(match.group("seq") or 0), path.name)


def _all_backups(conf_path: Path) -> List[Path]:
    pattern = f"{conf_path.name}.*.bak"
    prefix_len = len(conf_path.name) + 1
    return sorted(
        conf_path.parent.glob(pattern),
        key=lambda item: _backup_sort_key(item, prefix_len),
    )


def _prune_backups(conf_path: Path) -> List[Path]:
    backups = _all_backups(conf_path)
    keep = _backup_limit()
    for old in backups[:-keep]:
        try:
            old.unlink()
        except OSError:
            # Keep unreadable/undeletable entries visible instead of pretending
            # they were removed successfully.
            pass
    return _all_backups(conf_path)[-keep:]


def _files_equal(first: Path, second: Path) -> bool:
    try:
        if first.stat().st_size != second.stat().st_size:
            return False
        with first.open("rb") as left, second.open("rb") as right:
            while True:
                a = left.read(1024 * 1024)
                b = right.read(1024 * 1024)
                if a != b:
                    return False
                if not a:
                    return True
    except OSError:
        return False


def find_theme_includes(lines: List[str]) -> List[Tuple[int, str, bool]]:
    """Kembalikan list (index_baris, nama_tema, aktif_atau_tidak) untuk setiap baris
    'include themes/<nama>/<config>.conf', aktif maupun yang dikomentari."""
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
            disabled = False
            depth = 0
            opened = False
            j = i
            # rEFInd's own sample config puts '{' on the line after the
            # menuentry.  Counting braces only on the header line therefore
            # produced depth == 0 and the body was never scanned, so a stanza
            # holding 'disabled' was reported as active.  Scan forward until the
            # opening brace is found, then track depth until it balances.
            while j < n:
                line = lines[j]
                body_raw = line.strip()
                body_commented = body_raw.startswith("#")
                body_stripped = body_raw.lstrip("#").strip()
                # A '#' inside an otherwise active stanza is a real comment and
                # must not be read as an effective 'disabled' directive.
                if body_stripped == "disabled" and (commented or not body_commented):
                    disabled = True
                scan = body_stripped if (commented or body_commented) else line
                depth += scan.count("{") - scan.count("}")
                if scan.count("{"):
                    opened = True
                j += 1
                if opened and depth <= 0:
                    break
                if not opened and j > i + 8:
                    # No opening brace nearby: not a stanza header after all.
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
    wanted = theme_name.casefold()

    # Collect every include belonging to this theme first.  Activating them all
    # in one pass used to leave two variants of the same theme included at once
    # (INCLUDE_RE matches commented lines too), which makes rEFInd merge two
    # conflicting configs.  Exactly one line may end up active.
    own: List[Tuple[int, re.Match]] = []
    for idx, line in enumerate(new_lines):
        match = INCLUDE_RE.match(line.strip())
        if match and match.group("name").casefold() == wanted:
            own.append((idx, match))

    chosen_idx = None
    if own:
        # Prefer the canonical theme.conf that install/switch_variant generates;
        # otherwise keep whichever variant config the user already had, so that
        # re-activating a commented-out variant preserves its config path.
        for idx, match in own:
            if Path(match.group("config").replace("\\", "/")).name.casefold() == "theme.conf":
                chosen_idx = idx
                break
        if chosen_idx is None:
            chosen_idx = own[0][0]

    for idx, line in enumerate(new_lines):
        match = INCLUDE_RE.match(line.strip())
        if not match:
            continue
        is_own = match.group("name").casefold() == wanted
        if is_own:
            new_lines[idx] = _include_line(match, active=(idx == chosen_idx))
        elif not match.group("comment"):
            new_lines[idx] = _include_line(match, active=False)

    if chosen_idx is None:
        if new_lines and new_lines[-1].strip() != "":
            new_lines.append("")
        new_lines.append(f"include themes/{theme_name}/theme.conf")
    return new_lines


def deactivate_all(lines: List[str]) -> List[str]:
    """Komentari semua baris include tema yang aktif (kembali ke tampilan default)."""
    new_lines = list(lines)
    for idx, line in enumerate(new_lines):
        match = INCLUDE_RE.match(line.strip())
        if match and not match.group("comment"):
            new_lines[idx] = _include_line(match, active=False)
    return new_lines


def remove_theme_includes(lines: List[str], theme_name: str) -> List[str]:
    """Hapus seluruh baris include (aktif maupun dikomentari) untuk `theme_name`."""
    return [
        line
        for line in lines
        if not (
            (match := INCLUDE_RE.match(line.strip())) is not None
            and match.group("name").casefold() == theme_name.casefold()
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
    # Overwrite the first ACTIVE line.  Overwriting matches[0] unconditionally
    # destroyed the commented-out default that ships in refind.conf-sample, so
    # 'declutter --undo' could never put it back.
    active_matches = [item for item in matches if item[1]]
    if active_matches:
        new_lines[active_matches[0][0]] = target_line
        for idx, _is_active, _ in active_matches[1:]:
            new_lines[idx] = f"# {new_lines[idx].strip()}"
    elif matches:
        # Only commented lines exist: keep them, add the active line after the
        # last one so the documented default stays visible above it.
        new_lines.insert(matches[-1][0] + 1, target_line)
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
