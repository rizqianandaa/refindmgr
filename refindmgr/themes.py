"""Theme discovery, normalization, installation, and removal for rEFInd."""
from __future__ import annotations

import json
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

MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_EXTRACTED_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 10_000
GIT_TIMEOUT_SECONDS = 120

_THEME_DIRECTIVES = {
    "banner", "icons_dir", "selection_big", "selection_small", "font",
    "hideui", "showtools", "resolution", "small_icon_size", "big_icon_size",
    "banner_scale", "selection_big", "selection_small",
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


@dataclass
class PreparedTheme(AbstractContextManager):
    root: Path
    source: str
    variants: List[ThemeVariant]
    _tmp: Optional[tempfile.TemporaryDirectory] = None

    def __exit__(self, exc_type, exc, tb):
        if self._tmp is not None:
            self._tmp.cleanup()
        return False


def is_git_available() -> bool:
    return shutil.which("git") is not None


def _is_url(source: str) -> bool:
    return source.startswith(("http://", "https://", "git@", "ssh://", "file://")) or source.startswith(("github.com/", "www.github.com/"))


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
    images = [
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in _IMAGE_SUFFIXES
        and any(word in p.stem.lower() for word in ("background", "banner", "solid-", "solid_"))
        and "preview" not in p.stem.lower() and "screenshot" not in p.stem.lower()
    ]
    if len(images) <= 1:
        return []
    variants: List[ThemeVariant] = []
    all_images = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in _IMAGE_SUFFIXES]
    for banner in sorted(images):
        token = _extract_variant_token(banner)
        big = next((p for p in all_images if token in p.stem.lower() and ("big" in p.stem.lower() or "selection_big" in p.stem.lower())), None)
        small = next((p for p in all_images if token in p.stem.lower() and ("small" in p.stem.lower() or "selection_small" in p.stem.lower())), None)
        label = token.replace("-", " ").replace("_", " ").title()
        variants.append(ThemeVariant(
            key=_variant_key(label), label=label,
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


def prepare_theme_source(
    source: str,
    *,
    allow_insecure_http: bool = False,
    run_fn: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> PreparedTheme:
    """Stage a source in a temporary directory and discover its variants."""
    tmp = tempfile.TemporaryDirectory(prefix="refindmgr-theme-")
    stage = Path(tmp.name) / "source"
    try:
        if _is_url(source):
            clone_source = _public_git_source(source)
            if clone_source.startswith("http://") and not allow_insecure_http:
                raise ThemeError("URL HTTP ditolak. Gunakan HTTPS, atau --allow-insecure-http jika benar-benar diperlukan.")
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
                detail = (result.stderr or result.stdout).strip()
                raise ThemeError(
                    "Gagal clone repository public tanpa autentikasi. Pastikan URL benar dan repo public. "
                    f"Detail: {detail}"
                )
            shutil.rmtree(stage / ".git", ignore_errors=True)
        else:
            src = Path(source).expanduser()
            if src.is_file() and src.suffix.lower() == ".zip":
                if src.stat().st_size > MAX_ARCHIVE_BYTES:
                    raise ThemeError("ZIP ditolak: file arsip melebihi batas 128 MiB.")
                with zipfile.ZipFile(src) as archive:
                    _safe_extract_zip(archive, stage)
            elif src.is_dir():
                _assert_safe_theme_tree(src)
                shutil.copytree(src, stage)
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
        return PreparedTheme(stage, source, variants, tmp)
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
    matches = [p for p in root.rglob(rel.name) if (p.is_dir() if expect_dir else p.is_file())]
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
        for index, token, _value, original in list(_directives(text)):
            if token in _BOOT_SENSITIVE_DIRECTIVES:
                lines[index] = f"# refindmgr-sanitized: {original.strip()}"
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
        lines[index] = f"{indent}{token} themes/{install_name}/{rel.as_posix()}"
    for token, override in overrides.items():
        if override and token not in seen_override:
            lines.append(f"{token} themes/{install_name}/{override}")
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
    install_name = name or inferred_name or prepared.root.name
    validate_theme_name(install_name)
    selected = _select_variant(prepared.variants, variant)
    rewritten, warnings = _rewrite_config(prepared.root, selected, install_name, allow_unsafe_theme)
    if warnings and not allow_unsafe_theme:
        sensitive = [w for w in warnings if "directive sensitif" in w]
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
        shutil.copytree(prepared.root, staging)
        (staging / "theme.conf").write_text(rewritten, encoding="utf-8")
        os.replace(staging, destination)
    except OSError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise ThemeError(f"Gagal memasang tema secara atomik: {exc}") from exc
    installed = InstalledTheme(
        name=install_name,
        path=str(destination),
        include_path=f"themes/{install_name}/theme.conf",
        source=prepared.source,
        variant=selected.key,
        warnings=tuple(warnings),
    )
    _write_theme_metadata(refind_dir, installed)
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
                if candidate.suffix.lower() == ".png":
                    return candidate
    token = variant.key.lower()
    images = [
        path for path in root.rglob("*.png")
        if path.is_file() and any(word in path.name.lower() for word in ("preview", "screenshot", "background", "banner"))
    ]
    matching = [path for path in images if token in path.as_posix().lower()]
    return (matching or images or [None])[0]


def _metadata_path(refind_dir: Path) -> Path:
    return Path(refind_dir) / ".refindmgr" / "themes.json"


def _read_metadata(refind_dir: Path) -> dict:
    path = _metadata_path(refind_dir)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_theme_metadata(refind_dir: Path, installed: InstalledTheme) -> None:
    path = _metadata_path(refind_dir)
    data = _read_metadata(refind_dir)
    data[installed.name] = asdict(installed)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _remove_theme_metadata(refind_dir: Path, name: str) -> None:
    path = _metadata_path(refind_dir)
    data = _read_metadata(refind_dir)
    if name not in data:
        return
    data.pop(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def theme_conf_path(refind_dir: Path, theme_name: str) -> Optional[Path]:
    candidate = themes_dir(refind_dir) / theme_name / "theme.conf"
    return candidate if candidate.is_file() else None


def list_installed(refind_dir: Path) -> List[str]:
    directory = themes_dir(refind_dir)
    names = []
    if directory.is_dir():
        names.extend(child.name for child in directory.iterdir() if child.is_dir() and (child / "theme.conf").is_file())
    # Recognize legacy special-theme layouts created by refindmgr <=1.0.2.
    for legacy in ("rose-pine", "refind-sublime"):
        if (Path(refind_dir) / legacy / "theme.conf").is_file() and legacy not in names:
            names.append(legacy)
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
    try:
        os.replace(target, trash)
        if conf_path.is_file() and new_lines != lines:
            conf_mod.write_lines(conf_path, new_lines)
        shutil.rmtree(trash)
    except OSError as exc:
        if trash.exists() and not target.exists():
            os.replace(trash, target)
        raise ThemeError(f"Gagal menghapus tema; perubahan dibatalkan: {exc}") from exc
    _remove_theme_metadata(refind_dir, theme_name)


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
