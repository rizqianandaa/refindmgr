"""Theme discovery, normalization, installation, and removal for rEFInd."""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import stat
import subprocess
import tempfile
import zipfile
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable, List, Optional
from urllib.parse import urlparse

from . import conf as conf_mod
from .paths import refind_conf_path, themes_dir

_LOGGER = logging.getLogger("refindmgr.themes")

MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_EXTRACTED_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 10_000
GIT_TIMEOUT_SECONDS = 120

_THEME_DIRECTIVES = {
    "banner", "icons_dir", "selection_big", "selection_small", "font",
    "hideui", "showtools", "resolution", "small_icon_size", "big_icon_size",
    "banner_scale",
}
_ASSET_DIRECTIVES = {"banner", "icons_dir", "selection_big", "selection_small", "font"}
_BOOT_SENSITIVE_DIRECTIVES = {
    "menuentry", "submenuentry", "loader", "scanfor", "dont_scan_files",
    "dont_scan_dirs", "default_selection", "also_scan_dirs",
    "scan_all_linux_kernels",
}
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".svg"}


class ThemeError(Exception):
    """A user-facing theme error."""


@dataclass(frozen=True)
class ThemeVariant:
    key: str
    label: str
    config_path: str
    banner_path: Optional[str] = None
    selection_big_path: Optional[str] = None
    selection_small_path: Optional[str] = None


@dataclass(frozen=True)
class InstalledTheme:
    name: str
    path: str
    include_path: str
    source: str = ""
    variant: str = "default"
    warnings: tuple[str, ...] = ()
    # Catalog entries are cloned from third-party repositories at HEAD. Record
    # exactly which commit landed on the ESP so an install is auditable and can
    # be reproduced with 'install <source>@<commit>'.
    commit: str = ""


@dataclass
class PreparedTheme(AbstractContextManager):
    root: Path
    source: str
    variants: List[ThemeVariant]
    _tmp: Optional[tempfile.TemporaryDirectory] = None
    commit: str = ""

    def __exit__(self, exc_type, exc, tb):
        if self._tmp is not None:
            self._tmp.cleanup()
        return False


def is_git_available() -> bool:
    return shutil.which("git") is not None


def _is_url(source: str) -> bool:
    """Recognize remote sources.

    ``git@``/``ssh://`` stay accepted only for github.com, where
    :func:`_public_git_source` rewrites them to anonymous HTTPS.  ``file://``
    and SSH to any other host are not recognized as URLs, so they can never
    slip past the HTTPS gate in :func:`prepare_theme_source`.
    """
    value = source.strip()
    if value.startswith(("http://", "https://")):
        return True
    if value.startswith(("github.com/", "www.github.com/")):
        return True
    if value.startswith(("git@github.com:", "ssh://git@github.com/")):
        return True
    return False


def _public_git_source(source: str) -> str:
    """Normalize public GitHub inputs to anonymous HTTPS.

    This deliberately converts GitHub SSH forms to HTTPS and disables the
    situation where Git asks for a GitHub username/password for a public repo.
    """
    value = source.strip()
    if value.startswith(("github.com/", "www.github.com/")):
        value = "https://" + value
    if value.startswith("git@github.com:"):
        value = "https://github.com/" + value[len("git@github.com:"):]
    elif value.startswith("ssh://git@github.com/"):
        value = "https://github.com/" + value[len("ssh://git@github.com/"):]
    if value.startswith("http://github.com/") or value.startswith("http://www.github.com/"):
        value = "https://github.com/" + value.split("github.com/", 1)[1]
    if value.startswith("https://www.github.com/"):
        value = "https://github.com/" + value[len("https://www.github.com/"):]
    if value.startswith("https://github.com/"):
        parsed = urlparse(value)
        parts = [part for part in parsed.path.strip("/").split("/") if part]
        if len(parts) < 2:
            raise ThemeError("URL GitHub harus menunjuk ke repository public: github.com/owner/repo")
        # Clone only the repository root, never a /tree or /blob web URL.
        repo = parts[1]
        value = "https://github.com/" + parts[0] + "/" + repo
        if not value.endswith(".git"):
            value += ".git"
    return value


def sanitize_theme_name(name: str) -> str:
    """Turn an inferred name into one refind.conf can actually reference."""
    collapsed = re.sub(r"\s+", "-", (name or "").strip())
    return collapsed.strip("-") or "theme"


def validate_theme_name(name: str) -> None:
    if (
        not name
        or name in (".", "..")
        or "/" in name
        or "\\" in name
        or any(ord(ch) < 32 for ch in name)
    ):
        raise ThemeError(
            f"Nama tema tidak valid: '{name}'. Gunakan satu nama folder tanpa separator atau karakter kontrol."
        )
    # rEFInd's own parser splits 'include themes/<name>/theme.conf' on
    # whitespace, so a name with a space produces an include line that neither
    # rEFInd nor refindmgr can resolve: the theme installs but can never be
    # deactivated or removed again.
    if any(ch.isspace() for ch in name):
        raise ThemeError(
            f"Nama tema tidak boleh mengandung spasi: '{name}'. "
            f"rEFInd tidak dapat membaca baris include-nya. Coba '{sanitize_theme_name(name)}'."
        )


def _assert_safe_theme_tree(root: Path) -> None:
    try:
        for path in root.rglob("*"):
            if path.is_symlink():
                raise ThemeError(f"Tema tidak aman: symbolic link tidak didukung ({path.name}).")
    except OSError as exc:
        raise ThemeError(f"Tidak dapat memeriksa isi tema: {exc}") from exc


def _safe_extract_zip(archive: zipfile.ZipFile, destination: Path) -> None:
    infos = archive.infolist()
    if len(infos) > MAX_ARCHIVE_MEMBERS:
        raise ThemeError(f"ZIP berisi terlalu banyak entry ({len(infos)} > {MAX_ARCHIVE_MEMBERS}).")
    total = sum(max(0, info.file_size) for info in infos)
    if total > MAX_EXTRACTED_BYTES:
        raise ThemeError("ZIP ditolak: ukuran hasil ekstraksi melebihi batas 512 MiB.")
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    written = 0
    for info in infos:
        member = Path(info.filename)
        if member.is_absolute() or ".." in member.parts:
            raise ThemeError("ZIP ditolak: path mencoba keluar dari folder tema.")
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise ThemeError("ZIP ditolak: symbolic link tidak didukung.")
        target = (destination / member).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ThemeError("ZIP ditolak: path tidak aman.") from exc
        if info.is_dir() or info.filename.endswith("/"):
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(info, "r") as src, target.open("wb") as dst:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_EXTRACTED_BYTES:
                    raise ThemeError("ZIP ditolak: hasil ekstraksi melewati batas 512 MiB.")
                dst.write(chunk)


def _guess_name_from_url(source: str) -> str:
    normalized = _public_git_source(source) if "github.com" in source else source
    parts = [part for part in urlparse(normalized).path.strip("/").split("/") if part]
    tail = parts[-1] if parts else source.rstrip("/").rsplit("/", 1)[-1]
    tail = tail[:-4] if tail.endswith(".git") else tail
    if tail.lower() in {"refind", "theme", "themes"} and len(parts) >= 2:
        return parts[-2]
    return tail


def _theme_score(path: Path) -> int:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    score = 0
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        token = stripped.split(None, 1)[0].lower()
        if token in _THEME_DIRECTIVES:
            score += 1
    return score


def _label_from_path(path: Path) -> str:
    if path.name.lower() == "theme.conf" and path.parent.name:
        return path.parent.name
    return path.stem.replace("_", " ").replace("-", " ").title()


def _variant_key(label: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
    return key or "default"


def _candidate_configs(root: Path) -> List[Path]:
    candidates = []
    for path in root.rglob("*.conf"):
        if ".git" in path.parts:
            continue
        try:
            depth = len(path.relative_to(root).parts)
        except ValueError:
            continue
        # Some valid minimal themes contain only one asset directive. The
        # filename plus one known directive is enough; arbitrary .conf files
        # still need at least two theme directives.
        score = _theme_score(path)
        if depth <= 5 and (score >= 2 or (path.name.lower() == "theme.conf" and score >= 1)):
            candidates.append(path)
    return sorted(candidates, key=lambda p: (len(p.relative_to(root).parts), p.as_posix().lower()))


def _extract_variant_token(path: Path) -> str:
    stem = path.stem.lower()
    for prefix in ("background.", "background-", "background_", "solid-", "solid_", "banner-", "banner_"):
        if stem.startswith(prefix):
            return stem[len(prefix):]
    return stem


def _background_variants(root: Path, conf: Path) -> List[ThemeVariant]:
    # Walk the freshly cloned tree once; this used to rglob("*") twice.
    all_images = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in _IMAGE_SUFFIXES]
    images = [
        p for p in all_images
        if any(word in p.stem.lower() for word in ("background", "banner", "solid-", "solid_"))
        and "preview" not in p.stem.lower() and "screenshot" not in p.stem.lower()
    ]
    if len(images) <= 1:
        return []
    variants: List[ThemeVariant] = []
    seen_keys: set = set()
    for banner in sorted(images):
        token = _extract_variant_token(banner)
        big = next((p for p in all_images if token in p.stem.lower() and ("big" in p.stem.lower() or "selection_big" in p.stem.lower())), None)
        small = next((p for p in all_images if token in p.stem.lower() and ("small" in p.stem.lower() or "selection_small" in p.stem.lower())), None)
        label = token.replace("-", " ").replace("_", " ").title()
        key = _variant_key(label)
        # 'background-dark.png' and 'background_dark.jpg' both yield 'dark'.
        # Without this guard two variants shared a key and _select_variant could
        # only ever reach the first, making the second unselectable.
        if key in seen_keys:
            continue
        seen_keys.add(key)
        variants.append(ThemeVariant(
            key=key, label=label,
            config_path=conf.relative_to(root).as_posix(),
            banner_path=banner.relative_to(root).as_posix(),
            selection_big_path=big.relative_to(root).as_posix() if big else None,
            selection_small_path=small.relative_to(root).as_posix() if small else None,
        ))
    return variants


def discover_variants(root: Path) -> List[ThemeVariant]:
    """Discover config-file and background variants without repository-specific rules."""
    root = Path(root)
    configs = _candidate_configs(root)
    if not configs:
        raise ThemeError("Tidak menemukan file konfigurasi tema rEFInd yang valid (*.conf).")
    if len(configs) == 1:
        asset_variants = _background_variants(root, configs[0])
        if asset_variants:
            return asset_variants
        return [ThemeVariant("default", "Default", configs[0].relative_to(root).as_posix())]
    variants: List[ThemeVariant] = []
    used: set[str] = set()
    for conf in configs:
        label = _label_from_path(conf)
        key = _variant_key(label)
        base = key
        count = 2
        while key in used:
            key = f"{base}-{count}"
            count += 1
        used.add(key)
        variants.append(ThemeVariant(key, label, conf.relative_to(root).as_posix()))
    return variants


def _read_cloned_commit(stage: Path, run_fn) -> str:
    """Resolve the commit a clone actually landed on."""
    try:
        result = run_fn(
            ["git", "-C", str(stage), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if getattr(result, "returncode", 1) != 0:
        return ""
    return (result.stdout or "").strip()


def prepare_theme_source(
    source: str,
    *,
    allow_insecure_http: bool = False,
    run_fn: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> PreparedTheme:
    """Stage a source in a temporary directory and discover its variants."""
    tmp = tempfile.TemporaryDirectory(prefix="refindmgr-theme-")
    stage = Path(tmp.name) / "source"
    commit = ""
    # A file:// URI names a path on this machine. Handle it as a local source
    # instead of handing it to git: there is nothing remote to authenticate or
    # verify, and the local-directory path already strips .git and audits
    # symlinks before anything is copied.
    if source.strip().startswith("file://"):
        from urllib.request import url2pathname
        source = url2pathname(urlparse(source.strip()).path)
    try:
        if _is_url(source):
            _LOGGER.info("Menyiapkan sumber tema kind=public-repository")
            clone_source = _public_git_source(source)
            if clone_source.startswith("http://") and not allow_insecure_http:
                raise ThemeError("URL HTTP ditolak. Gunakan HTTPS, atau --allow-insecure-http jika benar-benar diperlukan.")
            if not clone_source.startswith(("https://", "http://")):
                raise ThemeError(
                    "Sumber remote harus HTTPS. Skema seperti ssh:// atau file:// ditolak "
                    f"karena tidak dapat diverifikasi: {clone_source}"
                )
            if not is_git_available():
                raise ThemeError("git tidak ditemukan di PATH.")
            try:
                git_env = os.environ.copy()
                git_env.update({
                    "GIT_TERMINAL_PROMPT": "0",
                    "GCM_INTERACTIVE": "Never",
                    "GIT_ASKPASS": "/bin/false",
                    "SSH_ASKPASS": "/bin/false",
                })
                result = run_fn(
                    ["git", "-c", "credential.helper=", "clone", "--depth", "1", "--no-recurse-submodules", clone_source, str(stage)],
                    capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS, env=git_env,
                )
            except subprocess.TimeoutExpired as exc:
                raise ThemeError(f"Git clone melewati timeout {GIT_TIMEOUT_SECONDS} detik.") from exc
            except OSError as exc:
                raise ThemeError(f"Gagal menjalankan git clone: {exc}") from exc
            if result.returncode != 0:
                _LOGGER.error("Clone sumber tema gagal returncode=%s", result.returncode)
                detail = (result.stderr or result.stdout).strip()
                raise ThemeError(
                    "Gagal clone repository public tanpa autentikasi. Pastikan URL benar dan repo public. "
                    f"Detail: {detail}"
                )
            commit = _read_cloned_commit(stage, run_fn)
            shutil.rmtree(stage / ".git", ignore_errors=True)
            _LOGGER.info("Clone sumber tema selesai commit=%s", commit or "unknown")
        else:
            src = Path(source).expanduser()
            if src.is_file() and src.suffix.lower() == ".zip":
                _LOGGER.info("Menyiapkan sumber tema kind=local-zip")
                try:
                    if src.stat().st_size > MAX_ARCHIVE_BYTES:
                        raise ThemeError("ZIP ditolak: file arsip melebihi batas 128 MiB.")
                    with zipfile.ZipFile(src) as archive:
                        _safe_extract_zip(archive, stage)
                except zipfile.BadZipFile as exc:
                    raise ThemeError(f"ZIP ditolak: berkas bukan arsip ZIP yang valid ({exc}).") from exc
                except RuntimeError as exc:
                    # zipfile raises a bare RuntimeError for encrypted members.
                    if "encrypted" in str(exc).lower():
                        raise ThemeError("ZIP ditolak: arsip terenkripsi/berpassword tidak didukung.") from exc
                    raise ThemeError(f"ZIP ditolak: {exc}") from exc
                except OSError as exc:
                    raise ThemeError(f"ZIP tidak dapat dibaca: {exc}") from exc
            elif src.is_dir():
                _LOGGER.info("Menyiapkan sumber tema kind=local-directory")
                _assert_safe_theme_tree(src)
                # Never copy the repository history onto a 100-512 MiB FAT ESP;
                # the git-clone path already strips it.
                shutil.copytree(
                    src, stage, symlinks=True,
                    ignore=shutil.ignore_patterns(".git", "__pycache__", ".github"),
                )
                _assert_safe_theme_tree(stage)
            elif src.is_file() and src.suffix.lower() in _IMAGE_SUFFIXES:
                raise ThemeError("File gambar tunggal bukan tema rEFInd.")
            else:
                raise ThemeError(f"Sumber tema tidak dikenali atau tidak ditemukan: {source}")
        _assert_safe_theme_tree(stage)
        # Strip a single archive wrapper directory when no config exists at the wrapper level.
        children = [p for p in stage.iterdir() if p.name != "__MACOSX"]
        if len(children) == 1 and children[0].is_dir():
            stage = children[0]
        variants = discover_variants(stage)
        return PreparedTheme(stage, source, variants, tmp, commit=commit)
    except Exception:
        tmp.cleanup()
        raise


def _directives(text: str) -> Iterable[tuple[int, str, str, str]]:
    for index, line in enumerate(text.splitlines()):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split(None, 1)
        yield index, parts[0].lower(), parts[1].strip() if len(parts) > 1 else "", line


def _quote_path(value: str) -> str:
    """Quote a rewritten asset path when it contains whitespace.

    _resolve_asset strips the quotes off the source value and nothing put them
    back, so 'banner "my banner.png"' was rewritten to an unquoted path and
    rEFInd silently read only the first word.  Theme names may contain spaces
    (validate_theme_name allows them), so this affects ordinary installs.
    """
    return f'"{value}"' if any(ch.isspace() for ch in value) else value


def _sanitized_block(lines: List[str], index: int) -> int:
    """Return the exclusive end index of the stanza opened at ``index``.

    Commenting only the 'menuentry' line left the stanza body and its closing
    brace at top level inside a file that gets included into refind.conf, so
    rEFInd saw a stray '}' and an un-rewritten relative 'icon' path -- exactly
    what sanitizing is supposed to prevent.
    """
    depth = lines[index].count("{") - lines[index].count("}")
    if depth <= 0:
        # The brace may sit on one of the next lines, as in refind.conf-sample.
        probe = index + 1
        while probe < len(lines) and probe <= index + 3:
            stripped = lines[probe].strip()
            if stripped.startswith("{"):
                depth = 1
                index = probe
                break
            if stripped and not stripped.startswith("#"):
                return index + 1
            probe += 1
        else:
            return index + 1
        if depth <= 0:
            return index + 1
    cursor = index + 1
    while cursor < len(lines) and depth > 0:
        depth += lines[cursor].count("{") - lines[cursor].count("}")
        cursor += 1
    return cursor


def _resolve_asset(root: Path, conf: Path, raw_value: str, expect_dir: bool = False) -> Optional[Path]:
    value = raw_value.strip().strip('"').replace("\\", "/").lstrip("/")
    if not value:
        return None
    rel = Path(value)
    candidates = [conf.parent / rel, root / rel]
    parts = list(rel.parts)
    if parts and parts[0].lower() == "themes":
        parts = parts[1:]
        if parts:
            candidates.append(root.joinpath(*parts))
            if len(parts) > 1:
                candidates.append(root.joinpath(*parts[1:]))
    elif len(parts) > 1:
        candidates.append(root.joinpath(*parts[1:]))
    for candidate in candidates:
        if (candidate.is_dir() if expect_dir else candidate.is_file()):
            try:
                return candidate.resolve().relative_to(root.resolve())
            except ValueError:
                continue
    # Last resort: unique basename match. Ambiguous matches are rejected.
    # rglob() interprets its argument as a pattern, so an asset literally named
    # 'background[1].png' is read as a character class and never matches itself.
    wanted = rel.name.casefold()
    matches = [
        p for p in root.rglob("*")
        if p.name.casefold() == wanted and (p.is_dir() if expect_dir else p.is_file())
    ]
    if len(matches) == 1:
        return matches[0].resolve().relative_to(root.resolve())
    return None


def _audit_config(text: str) -> List[str]:
    found = sorted({token for _, token, _, _ in _directives(text) if token in _BOOT_SENSITIVE_DIRECTIVES})
    return [f"Konfigurasi berisi directive sensitif: {', '.join(found)}"] if found else []


def _rewrite_config(root: Path, variant: ThemeVariant, install_name: str, allow_unsafe: bool = False) -> tuple[str, List[str]]:
    conf = root / variant.config_path
    text = conf.read_text(encoding="utf-8", errors="replace")
    warnings = _audit_config(text)
    overrides = {
        "banner": variant.banner_path,
        "selection_big": variant.selection_big_path,
        "selection_small": variant.selection_small_path,
    }
    lines = text.splitlines()
    if not allow_unsafe:
        sanitized: set = set()
        for index, token, _value, original in list(_directives(text)):
            if token not in _BOOT_SENSITIVE_DIRECTIVES or index in sanitized:
                continue
            if token in {"menuentry", "submenuentry"}:
                end = _sanitized_block(lines, index)
            else:
                end = index + 1
            for cursor in range(index, min(end, len(lines))):
                if cursor in sanitized:
                    continue
                sanitized.add(cursor)
                lines[cursor] = f"# refindmgr-sanitized: {lines[cursor].strip()}"
    seen_override: set[str] = set()
    for index, token, value, original in list(_directives(text)):
        if token not in _ASSET_DIRECTIVES:
            continue
        override = overrides.get(token)
        if override:
            rel = Path(override)
            seen_override.add(token)
        else:
            rel = _resolve_asset(root, conf, value, expect_dir=(token == "icons_dir"))
        if rel is None:
            warnings.append(f"Aset tidak ditemukan untuk '{token} {value}'.")
            continue
        indent = original[: len(original) - len(original.lstrip())]
        lines[index] = f"{indent}{token} {_quote_path(f'themes/{install_name}/{rel.as_posix()}')}"
    for token, override in overrides.items():
        if override and token not in seen_override:
            lines.append(f"{token} {_quote_path(f'themes/{install_name}/{override}')}")
    return "\n".join(lines).rstrip() + "\n", warnings


def _select_variant(variants: List[ThemeVariant], requested: Optional[str]) -> ThemeVariant:
    if requested:
        wanted = requested.lower()
        for variant in variants:
            if wanted in {variant.key.lower(), variant.label.lower(), variant.config_path.lower()}:
                return variant
        choices = ", ".join(v.key for v in variants)
        raise ThemeError(f"Varian '{requested}' tidak ditemukan. Pilihan: {choices}")
    if len(variants) > 1:
        choices = ", ".join(f"{v.key} ({v.label})" for v in variants)
        raise ThemeError(f"Sumber memiliki beberapa varian. Pilih dengan --variant: {choices}")
    return variants[0]


def install_prepared_theme(
    refind_dir: Path,
    prepared: PreparedTheme,
    *,
    name: Optional[str] = None,
    variant: Optional[str] = None,
    allow_unsafe_theme: bool = False,
) -> InstalledTheme:
    source_path = Path(prepared.source).expanduser()
    inferred_name = source_path.stem if source_path.suffix.lower() == ".zip" else _guess_name_from_url(prepared.source)
    # An explicit --name is validated as typed; an inferred one is normalized,
    # because zip stems and repository names routinely contain spaces.
    install_name = name or sanitize_theme_name(inferred_name or prepared.root.name)
    validate_theme_name(install_name)
    selected = _select_variant(prepared.variants, variant)
    rewritten, warnings = _rewrite_config(prepared.root, selected, install_name, allow_unsafe_theme)
    if warnings and not allow_unsafe_theme:
        missing = [w for w in warnings if "Aset tidak ditemukan" in w]
        if missing:
            raise ThemeError("Tema tidak diterapkan karena referensi aset tidak valid: " + "; ".join(missing))
    destination = themes_dir(refind_dir) / install_name
    if destination.exists():
        raise ThemeError(f"Tema '{install_name}' sudah terpasang.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.parent / f".__install_{install_name}_{os.getpid()}"
    shutil.rmtree(staging, ignore_errors=True)
    try:
        _LOGGER.info("Memasang tema name=%s variant=%s", install_name, selected.key)
        shutil.copytree(prepared.root, staging)
        _write_durable(staging / "theme.conf", rewritten)
        # Flush the tree before publishing it: refind.conf is about to point at
        # these files, and an unflushed ESP write survives as zero-length data.
        _fsync_tree(staging)
        os.replace(staging, destination)
    except OSError as exc:
        _LOGGER.exception("Instalasi tema gagal name=%s", install_name)
        shutil.rmtree(staging, ignore_errors=True)
        raise ThemeError(f"Gagal memasang tema secara atomik: {exc}") from exc
    installed = InstalledTheme(
        name=install_name,
        path=str(destination),
        include_path=f"themes/{install_name}/theme.conf",
        source=prepared.source,
        variant=selected.key,
        warnings=tuple(warnings),
        commit=getattr(prepared, "commit", "") or "",
    )
    _write_theme_metadata(refind_dir, installed)
    _LOGGER.info("Instalasi tema selesai name=%s", install_name)
    return installed


def install_theme(
    refind_dir: Path,
    source: str,
    name: Optional[str] = None,
    subdir: Optional[str] = None,
    variant: Optional[str] = None,
    allow_insecure_http: bool = False,
    allow_unsafe_theme: bool = False,
) -> str:
    """Compatibility wrapper returning only the installed theme name."""
    with prepare_theme_source(source, allow_insecure_http=allow_insecure_http) as prepared:
        if subdir and not variant:
            variant = next((v.key for v in prepared.variants if subdir.lower() in v.config_path.lower()), subdir)
        installed = install_prepared_theme(
            refind_dir, prepared, name=name, variant=variant, allow_unsafe_theme=allow_unsafe_theme
        )
    return installed.name


def installed_variants(refind_dir: Path, theme_name: str) -> List[ThemeVariant]:
    """Discover variants already stored inside an installed theme.

    Installation keeps the original repository files, so switching variants
    only regenerates the canonical ``theme.conf``; no clone or reinstall is
    required.
    """
    validate_theme_name(theme_name)
    root = themes_dir(refind_dir) / theme_name
    if not root.is_dir():
        raise ThemeError(f"Tema '{theme_name}' tidak ditemukan.")
    variants = discover_variants(root)
    originals = [item for item in variants if item.config_path != "theme.conf"]
    return originals if originals else variants


def switch_variant(
    refind_dir: Path,
    theme_name: str,
    requested: str,
    *,
    allow_unsafe_theme: bool = False,
) -> InstalledTheme:
    """Atomically switch an installed theme to another bundled variant."""
    root = themes_dir(refind_dir) / theme_name
    variants = installed_variants(refind_dir, theme_name)
    selected = _select_variant(variants, requested)
    metadata = _read_metadata(refind_dir).get(theme_name, {})
    current = str(metadata.get("variant", ""))
    canonical = root / "theme.conf"
    if current == selected.key and canonical.is_file():
        return InstalledTheme(
            name=theme_name,
            path=str(root),
            include_path=f"themes/{theme_name}/theme.conf",
            source=str(metadata.get("source", "")),
            variant=current,
            warnings=tuple(metadata.get("warnings", ())),
        )
    rewritten, warnings = _rewrite_config(root, selected, theme_name, allow_unsafe_theme)
    missing = [warning for warning in warnings if "Aset tidak ditemukan" in warning]
    if missing:
        raise ThemeError("Varian tidak dapat diterapkan karena aset tidak valid: " + "; ".join(missing))
    temporary = canonical.with_name(f".theme.conf.variant-{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(rewritten)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, canonical)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise ThemeError(f"Gagal mengganti varian secara atomik: {exc}") from exc
    installed = InstalledTheme(
        name=theme_name,
        path=str(root),
        include_path=f"themes/{theme_name}/theme.conf",
        source=str(metadata.get("source", "")),
        variant=selected.key,
        warnings=tuple(warnings),
        commit=str(metadata.get("commit", "")),
    )
    _write_theme_metadata(refind_dir, installed)
    return installed


def preview_image(root: Path, variant: ThemeVariant) -> Optional[Path]:
    """Return the original banner/background for a variant when possible."""
    root = Path(root)
    if variant.banner_path:
        candidate = root / variant.banner_path
        if candidate.is_file():
            return candidate
    config = root / variant.config_path
    try:
        text = config.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for _index, token, value, _original in _directives(text):
        if token == "banner":
            resolved = _resolve_asset(root, config, value)
            if resolved is not None:
                candidate = root / resolved
                # A .jpg banner is just as usable a preview as a .png; rejecting
                # it sent this function to the glob fallback, which then handed
                # back an arbitrary unrelated image.
                if candidate.suffix.lower() in _IMAGE_SUFFIXES and candidate.is_file():
                    return candidate
    token = variant.key.lower()
    # sorted(): rglob order is filesystem-dependent, so the same theme could
    # yield a different "preview" on every machine.
    images = sorted(
        path for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in _IMAGE_SUFFIXES
        and any(word in path.name.lower() for word in ("preview", "screenshot", "background", "banner"))
    )
    matching = [path for path in images if token in path.as_posix().lower()]
    return (matching or images or [None])[0]


def _metadata_path(refind_dir: Path) -> Path:
    return Path(refind_dir) / ".refindmgr" / "themes.json"


def _write_durable(path: Path, text: str) -> None:
    """Write text and flush it all the way to the device."""
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_tree(root: Path) -> None:
    """Flush a freshly installed theme to the ESP.

    conf.write_lines and switch_variant already do this; installation did not,
    so a crash right after os.replace could leave refind.conf pointing at a
    theme whose files were never written.
    """
    try:
        for current, _dirs, files in os.walk(root):
            for name in files:
                try:
                    fd = os.open(os.path.join(current, name), os.O_RDONLY)
                except OSError:
                    continue
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            try:
                fd = os.open(current, os.O_RDONLY)
            except OSError:
                continue
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    except OSError:
        # Best effort: vfat and some kernels refuse directory fsync.
        pass


def _read_metadata(refind_dir: Path) -> dict:
    path = _metadata_path(refind_dir)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    # A truncated or hand-edited themes.json can hold a list; indexing it later
    # raised TypeError *after* the theme had already been copied to the ESP.
    return data if isinstance(data, dict) else {}


def _write_theme_metadata(refind_dir: Path, installed: InstalledTheme) -> None:
    path = _metadata_path(refind_dir)
    data = _read_metadata(refind_dir)
    data[installed.name] = asdict(installed)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    _write_durable(tmp, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def _remove_theme_metadata(refind_dir: Path, name: str) -> None:
    path = _metadata_path(refind_dir)
    data = _read_metadata(refind_dir)
    if name not in data:
        return
    data.pop(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    _write_durable(tmp, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def theme_conf_path(refind_dir: Path, theme_name: str) -> Optional[Path]:
    conf_path = refind_conf_path(refind_dir)
    if conf_path.is_file():
        for line in conf_mod.read_lines(conf_path):
            match = conf_mod.INCLUDE_RE.match(line.strip())
            if not match or match.group("name").casefold() != theme_name.casefold():
                continue
            if match.group("comment"):
                # A commented include is a DEACTIVATED variant; returning its
                # config made callers act on a theme that is not in effect.
                continue
            relative = Path(match.group("config").replace("\\", "/"))
            candidate = themes_dir(refind_dir) / theme_name / relative
            try:
                candidate.resolve().relative_to((themes_dir(refind_dir) / theme_name).resolve())
            except ValueError:
                continue
            if candidate.is_file():
                return candidate
    candidate = themes_dir(refind_dir) / theme_name / "theme.conf"
    if candidate.is_file():
        return candidate
    variants = sorted((themes_dir(refind_dir) / theme_name).glob("*.conf"))
    return variants[0] if len(variants) == 1 else None


def list_installed(refind_dir: Path) -> List[str]:
    directory = themes_dir(refind_dir)
    names = set()
    if directory.is_dir():
        for child in directory.iterdir():
            if child.is_dir() and any(path.is_file() for path in child.glob("*.conf")):
                names.add(child.name)
    names.update(
        name for name in _read_metadata(refind_dir)
        if (directory / name).is_dir()
    )
    conf_path = refind_conf_path(refind_dir)
    if conf_path.is_file():
        for _idx, name, _active in conf_mod.find_theme_includes(conf_mod.read_lines(conf_path)):
            if (directory / name).is_dir():
                names.add(name)
    # Recognize legacy special-theme layouts created by refindmgr <=1.0.2.
    for legacy in ("rose-pine", "refind-sublime"):
        if (Path(refind_dir) / legacy / "theme.conf").is_file():
            names.add(legacy)
    return sorted(names)


def remove_theme(refind_dir: Path, theme_name: str) -> None:
    validate_theme_name(theme_name)
    conf_path = refind_conf_path(refind_dir)
    lines = conf_mod.read_lines(conf_path) if conf_path.is_file() else []
    new_lines = conf_mod.remove_theme_includes(lines, theme_name)
    # Also remove legacy direct includes.
    legacy_re = re.compile(rf"^\s*#?\s*include\s+{re.escape(theme_name)}/theme\.conf\s*$", re.I)
    new_lines = [line for line in new_lines if not legacy_re.match(line)]
    theme_path = themes_dir(refind_dir) / theme_name
    legacy_path = Path(refind_dir) / theme_name if theme_name in {"rose-pine", "refind-sublime"} else None
    target = theme_path if theme_path.is_dir() else legacy_path
    if target is None or not target.is_dir():
        raise ThemeError(f"Tema '{theme_name}' tidak ditemukan.")
    trash = target.with_name(f".__remove_{target.name}_{os.getpid()}")
    if conf_path.is_file() and new_lines != lines:
        conf_mod.backup(conf_path)
    conf_updated = False
    try:
        _LOGGER.info("Menghapus tema name=%s", theme_name)
        # Order matters. Removing the directory first meant a crash between the
        # two steps left refind.conf including a theme that no longer existed,
        # which drops rEFInd to an error screen at the next boot. Dropping the
        # include first is always safe: at worst an unused directory remains.
        if conf_path.is_file() and new_lines != lines:
            conf_mod.write_lines(conf_path, new_lines)
            conf_updated = True
        os.replace(target, trash)
    except OSError as exc:
        _LOGGER.exception("Penghapusan tema gagal name=%s", theme_name)
        if conf_updated:
            try:
                conf_mod.write_lines(conf_path, lines)
                conf_updated = False
            except OSError:
                raise ThemeError(
                    "Gagal menghapus tema DAN gagal mengembalikan refind.conf. "
                    f"Pulihkan dari backup di {conf_path.parent}: {exc}"
                ) from exc
        raise ThemeError(f"Gagal menghapus tema; perubahan dibatalkan: {exc}") from exc
    try:
        shutil.rmtree(trash)
    except OSError as exc:
        # refind.conf no longer references the theme, so boot is already safe.
        # Reporting a rollback here would be a lie: the config edit is committed.
        _LOGGER.warning("Sisa folder tema tidak terhapus path=%s error=%s", trash, exc)
    _remove_theme_metadata(refind_dir, theme_name)
    _LOGGER.info("Penghapusan tema selesai name=%s", theme_name)


# Backward-compatible patch helpers; generic normalization now handles these sources.
def patch_sublime_theme(theme_dir: Path) -> None:
    if not (theme_dir / "theme.conf").is_file():
        raise ThemeError("theme.conf Sublime tidak ditemukan.")


def patch_rose_pine_theme(theme_dir: Path, variant: str) -> None:
    if variant not in {"main", "moon", "dawn"}:
        raise ThemeError("Varian Rosé Pine tidak valid.")


def patch_digital_void_theme(theme_dir: Path) -> None:
    if not (theme_dir / "theme.conf").is_file():
        raise ThemeError("theme.conf Digital Void tidak ditemukan.")
