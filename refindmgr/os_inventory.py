"""Profile-driven, read-only OS and EFI loader inventory.

The inventory deliberately reports only loaders supported by a known profile.
It never guesses that an arbitrary EFI binary is an operating system and never
writes the ESP.  Higher-level commands remain responsible for checking that a
candidate is not rEFInd itself before generating a manual menu entry.
"""
from __future__ import annotations

import platform
import re
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Optional

from . import system as system_mod


BASELINE_SCHEMA = 1
BASELINE_FILENAME = "os-loader-baseline.json"


@dataclass(frozen=True)
class DistroProfile:
    key: str
    label: str
    folders: tuple[str, ...]
    ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class RuntimeOs:
    distro_id: str = ""
    id_like: tuple[str, ...] = ()
    pretty_name: str = ""
    version_id: str = ""


@dataclass
class OsLoader:
    key: str
    label: str
    path: str
    kind: str
    architecture: str
    confidence: str
    evidence: list[str] = field(default_factory=list)
    current_os: bool = False
    healthy: bool = True
    issues: list[str] = field(default_factory=list)


@dataclass
class OsInventory:
    architecture: str
    firmware_bits: Optional[int]
    runtime: RuntimeOs
    loaders: list[OsLoader]
    warnings: list[str] = field(default_factory=list)

    def menu_entries(self) -> list[tuple[str, str]]:
        """Return unique, healthy loaders safe enough for an OS-only menu."""
        result: list[tuple[str, str]] = []
        seen_paths: set[str] = set()
        for item in self.loaders:
            normalized = item.path.lower()
            if not item.healthy or item.confidence not in {"verified", "high"}:
                continue
            if normalized in seen_paths:
                continue
            seen_paths.add(normalized)
            result.append((item.label, item.path))
        return result


PROFILES: tuple[DistroProfile, ...] = (
    DistroProfile("ubuntu", "Ubuntu", ("ubuntu",), ("ubuntu",)),
    DistroProfile("debian", "Debian", ("debian",), ("debian",)),
    DistroProfile("linuxmint", "Linux Mint", ("linuxmint",), ("linuxmint", "mint")),
    DistroProfile("fedora", "Fedora", ("fedora",), ("fedora",)),
    DistroProfile("opensuse", "openSUSE", ("opensuse", "opensuse-secureboot"), ("opensuse", "opensuse-leap", "opensuse-tumbleweed")),
    DistroProfile("arch", "Arch Linux", ("arch",), ("arch",)),
    DistroProfile("manjaro", "Manjaro", ("manjaro",), ("manjaro",)),
    DistroProfile("pop_os", "Pop!_OS", ("pop_os", "pop"), ("pop", "pop_os")),
    DistroProfile("zorin", "Zorin OS", ("zorin",), ("zorin",)),
    DistroProfile("elementary", "elementary OS", ("elementary",), ("elementary",)),
    DistroProfile("kali", "Kali Linux", ("kali",), ("kali",)),
    DistroProfile("nixos", "NixOS", ("nixos",), ("nixos",)),
    DistroProfile("endeavouros", "EndeavourOS", ("endeavouros",), ("endeavouros",)),
    DistroProfile("garuda", "Garuda Linux", ("garuda",), ("garuda",)),
)

_ARCH_ALIASES = {
    "x86_64": "x86_64", "amd64": "x86_64", "x64": "x86_64",
    "aarch64": "arm64", "arm64": "arm64", "aa64": "arm64",
    "i386": "ia32", "i486": "ia32", "i586": "ia32", "i686": "ia32", "ia32": "ia32",
}
_ARCH_SUFFIX = {"x86_64": "x64", "arm64": "aa64", "ia32": "ia32"}
_SUFFIX_ARCH = {value: key for key, value in _ARCH_SUFFIX.items()}


def normalize_architecture(machine: str, firmware_bits: Optional[int] = None) -> str:
    architecture = _ARCH_ALIASES.get((machine or "").strip().lower(), "unknown")
    if firmware_bits == 32 and architecture == "x86_64":
        return "ia32"
    if firmware_bits == 64 and architecture == "ia32":
        return "unknown"
    return architecture


def read_firmware_bits(path: Path = Path("/sys/firmware/efi/fw_platform_size")) -> Optional[int]:
    try:
        value = int(path.read_text(encoding="utf-8").strip())
    except (OSError, UnicodeError, ValueError):
        return None
    return value if value in {32, 64} else None


def parse_os_release(text: str) -> RuntimeOs:
    values: dict[str, str] = {}
    for original in text.splitlines():
        line = original.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key.strip().upper()] = value
    return RuntimeOs(
        distro_id=values.get("ID", "").lower(),
        id_like=tuple(values.get("ID_LIKE", "").lower().split()),
        pretty_name=values.get("PRETTY_NAME", ""),
        version_id=values.get("VERSION_ID", ""),
    )


OS_RELEASE_ENV = "REFINDMGR_OS_RELEASE"


def detect_runtime_os(paths: Iterable[Path] = (Path("/etc/os-release"), Path("/usr/lib/os-release"))) -> RuntimeOs:
    """Identify the running distribution.

    ``REFINDMGR_OS_RELEASE`` overrides the lookup so a chroot, a live USB, or a
    test can describe the target system rather than the host it runs on.
    """
    override = os.environ.get(OS_RELEASE_ENV)
    if override:
        paths = (Path(override),)
    for path in paths:
        try:
            return parse_os_release(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            continue
    return RuntimeOs()


def _loader_arch(path: str) -> str:
    match = re.search(r"(x64|aa64|ia32)\.efi$", path.lower())
    return _SUFFIX_ARCH.get(match.group(1), "unknown") if match else "unknown"


def pe_architecture(path: Path) -> str:
    """Read the PE/COFF machine field without executing the EFI binary."""
    machines = {0x8664: "x86_64", 0xAA64: "arm64", 0x014C: "ia32"}
    try:
        with path.open("rb") as stream:
            header = stream.read(64)
            if len(header) < 64 or header[:2] != b"MZ":
                return "unknown"
            offset = int.from_bytes(header[0x3C:0x40], "little")
            if offset < 64 or offset > 16 * 1024 * 1024:
                return "unknown"
            stream.seek(offset)
            pe = stream.read(6)
            if len(pe) != 6 or pe[:4] != b"PE\x00\x00":
                return "unknown"
            return machines.get(int.from_bytes(pe[4:6], "little"), "unknown")
    except OSError:
        return "unknown"


def _resolved_loader_arch(path: str, esp_root: Optional[Path]) -> str:
    from_name = _loader_arch(path)
    if from_name != "unknown" or esp_root is None:
        return from_name
    return pe_architecture(esp_root / path)


from .hashing import sha256_file_or_none as _sha256


def _known_refind_hashes(refind_dir: Path) -> set[str]:
    hashes: set[str] = set()
    for name in ("refind_x64.efi", "refind_aa64.efi", "refind_ia32.efi"):
        value = _sha256(refind_dir / name)
        if value:
            hashes.add(value)
    manifest = refind_dir / ".refindmgr" / "firmware-compat.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        value = data.get("refind_sha256")
        if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value):
            hashes.add(value.lower())
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    return hashes


def _list_inventory_efi_files(refind_dir: Path) -> list[str]:
    """List the whole ESP, including a vendor directory hosting rEFInd.

    The older generic loader audit excludes ``refind_dir`` completely. That is
    correct for a standard ``EFI/refind`` installation, but compatibility mode
    can place rEFInd beside the real GRUB loader in ``EFI/ubuntu``. Inventory
    therefore scans the complete ESP and rejects rEFInd copies by hash later.
    """
    esp_root = system_mod.esp_root_from_refind_dir(refind_dir)
    if esp_root is None or not esp_root.is_dir():
        return []
    tools = (esp_root / "EFI" / "tools").resolve()
    results: list[str] = []
    try:
        for path in esp_root.rglob("*"):
            if not path.is_file() or path.suffix.lower() != ".efi":
                continue
            resolved = path.resolve()
            if resolved == tools or tools in resolved.parents:
                continue
            results.append(path.relative_to(esp_root).as_posix())
    except OSError:
        return []
    return sorted(results)


def _choices(folder: str, architecture: str) -> tuple[str, ...]:
    suffixes = [_ARCH_SUFFIX[architecture]] if architecture in _ARCH_SUFFIX else ["x64", "aa64", "ia32"]
    choices: list[str] = []
    for suffix in suffixes:
        choices.extend((
            f"efi/{folder}/shim{suffix}.efi",
            f"efi/{folder}/grub{suffix}.efi",
            f"efi/{folder}/systemd-boot{suffix}.efi",
        ))
    # openSUSE and a few vendor packages also install an architecture-neutral
    # shim.efi filename. Its PE architecture is not inferred from the name.
    choices.extend((f"efi/{folder}/shim.efi", f"efi/{folder}/grub.efi"))
    return tuple(choices)


def _runtime_matches(profile: DistroProfile, runtime: RuntimeOs) -> bool:
    # ID_LIKE describes package-family compatibility, not identity. Amazon
    # Linux, Rocky, and many others say ID_LIKE=fedora but must not relabel a
    # Fedora EFI folder as the currently running OS. Only explicit ID aliases
    # are strong enough to mark a profile as verified/current.
    return bool(runtime.distro_id and runtime.distro_id in profile.ids)


def _display_label(profile: DistroProfile, runtime: RuntimeOs, current: bool) -> str:
    # profile.label is the fallback whenever PRETTY_NAME is missing or blank --
    # common on minimal images that set ID= but no PRETTY_NAME.
    if current and runtime.pretty_name.strip():
        return runtime.pretty_name.strip()
    return profile.label


def _read_loader_entry_titles(esp_root: Optional[Path]) -> list[str]:
    if esp_root is None:
        return []
    titles: list[str] = []
    entries = esp_root / "loader" / "entries"
    try:
        files = sorted(entries.glob("*.conf"))
    except OSError:
        return []
    for path in files:
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                key, _, value = line.strip().partition(" ")
                if key.lower() == "title" and value.strip():
                    titles.append(value.strip())
                    break
        except OSError:
            continue
    return titles


def build_inventory(
    refind_dir: Path,
    *,
    loader_paths: Optional[Iterable[str]] = None,
    runtime: Optional[RuntimeOs] = None,
    machine: Optional[str] = None,
    firmware_bits: Optional[int] = None,
) -> OsInventory:
    """Build an inventory from one ESP without mutating files or NVRAM."""
    refind_dir = Path(refind_dir)
    runtime = runtime if runtime is not None else detect_runtime_os()
    firmware_bits = read_firmware_bits() if firmware_bits is None else firmware_bits
    architecture = normalize_architecture(machine or platform.machine(), firmware_bits)
    paths = list(loader_paths) if loader_paths is not None else _list_inventory_efi_files(refind_dir)
    available: Mapping[str, str] = {path.lower().lstrip("/"): path.lstrip("/") for path in paths}
    esp_root = system_mod.esp_root_from_refind_dir(refind_dir)
    refind_hashes = _known_refind_hashes(refind_dir)
    loaders: list[OsLoader] = []
    warnings: list[str] = []

    def is_refind_copy(relative_path: str) -> bool:
        if not refind_hashes or esp_root is None:
            return False
        value = _sha256(esp_root / relative_path)
        return value in refind_hashes if value else False

    windows_path = available.get("efi/microsoft/boot/bootmgfw.efi")
    if windows_path and is_refind_copy(windows_path):
        warnings.append(f"/{windows_path} bernama Windows loader tetapi byte-identik dengan rEFInd; kandidat dilewati.")
    elif windows_path:
        loaders.append(OsLoader(
            key="windows", label="Windows", path=windows_path, kind="windows-boot-manager",
            architecture=_resolved_loader_arch(windows_path, esp_root), confidence="high",
            evidence=["path Windows Boot Manager kanonis"],
        ))

    for profile in PROFILES:
        selected: Optional[str] = None
        selected_folder = ""
        for folder in profile.folders:
            for candidate in _choices(folder, architecture):
                if candidate not in available:
                    continue
                proposed = available[candidate]
                if is_refind_copy(proposed):
                    warnings.append(f"/{proposed} berada di path {profile.label} tetapi byte-identik dengan rEFInd; mencoba loader berikutnya.")
                    continue
                selected = proposed
                break
            if selected:
                selected_folder = folder
                break
        if not selected:
            continue
        current = _runtime_matches(profile, runtime)
        loader_arch = _resolved_loader_arch(selected, esp_root)
        healthy = loader_arch in {"unknown", architecture} or architecture == "unknown"
        issues: list[str] = []
        if not healthy:
            issues.append(f"arsitektur loader {loader_arch} berbeda dari firmware {architecture}")
        kind = "shim" if Path(selected).name.lower().startswith("shim") else (
            "grub" if Path(selected).name.lower().startswith("grub") else "systemd-boot"
        )
        loaders.append(OsLoader(
            key=profile.key,
            label=_display_label(profile, runtime, current),
            path=selected,
            kind=kind,
            architecture=loader_arch,
            confidence="verified" if current else "high",
            evidence=[f"folder vendor EFI/{selected_folder}", f"loader {kind}"] + (["cocok dengan /etc/os-release"] if current else []),
            current_os=current,
            healthy=healthy,
            issues=issues,
        ))

    suffix = _ARCH_SUFFIX.get(architecture)
    systemd_paths = [f"efi/systemd/systemd-boot{suffix}.efi"] if suffix else [
        "efi/systemd/systemd-bootx64.efi", "efi/systemd/systemd-bootaa64.efi", "efi/systemd/systemd-bootia32.efi"
    ]
    systemd_path = next((available[path] for path in systemd_paths if path in available), None)
    if systemd_path and is_refind_copy(systemd_path):
        warnings.append(f"/{systemd_path} byte-identik dengan rEFInd; systemd-boot tidak dipilih.")
    elif systemd_path:
        titles = _read_loader_entry_titles(esp_root)
        label = f"{titles[0]} (systemd-boot)" if len(titles) == 1 else "Linux (systemd-boot)"
        evidence = ["systemd-boot pada path kanonis"]
        if titles:
            evidence.append("entry: " + ", ".join(titles[:4]))
        loaders.append(OsLoader(
            key="systemd-boot", label=label, path=systemd_path, kind="systemd-boot",
            architecture=_resolved_loader_arch(systemd_path, esp_root), confidence="high" if titles else "medium",
            evidence=evidence,
        ))

    for normalized, original in sorted(available.items()):
        if not normalized.startswith("efi/linux/"):
            continue
        if is_refind_copy(original):
            warnings.append(f"/{original} byte-identik dengan rEFInd; UKI tidak dipilih.")
            continue
        arch = _resolved_loader_arch(original, esp_root)
        healthy = arch in {"unknown", architecture} or architecture == "unknown"
        stem = Path(original).stem.replace("_", " ").replace("-", " ").strip()
        single_uki = sum(1 for item in available if item.startswith("efi/linux/")) == 1
        label = ""
        if runtime.distro_id and single_uki:
            label = runtime.pretty_name.strip()
        # An empty PRETTY_NAME used to reach refind.conf verbatim as
        # 'menuentry "" {', producing an unlabelled boot entry under
        # 'scanfor manual' -- i.e. a menu with no usable OS name.
        label = label or stem or "Linux UKI"
        loaders.append(OsLoader(
            key="uki", label=label, path=original, kind="uki", architecture=arch,
            confidence="high", evidence=["Unified Kernel Image di EFI/Linux"],
            healthy=healthy,
            issues=[] if healthy else [f"arsitektur UKI {arch} berbeda dari firmware {architecture}"],
        ))

    if architecture == "unknown":
        warnings.append("Arsitektur firmware tidak dapat dipastikan; loader tidak boleh dipilih lintas-arsitektur.")
    if not loaders:
        warnings.append("Tidak ditemukan loader OS dengan profil yang didukung pada ESP ini.")
    return OsInventory(architecture, firmware_bits, runtime, loaders, warnings)


def health_summary(inventory: OsInventory) -> tuple[list[str], list[str]]:
    ok: list[str] = []
    problems: list[str] = list(inventory.warnings)
    for loader in inventory.loaders:
        if loader.healthy:
            ok.append(f"{loader.label}: /{loader.path}")
        else:
            problems.append(f"{loader.label}: " + "; ".join(loader.issues))
    return ok, problems


def baseline_path(path: Optional[Path] = None) -> Path:
    if path is not None:
        return Path(path)
    root = Path(os.environ.get("REFINDMGR_STATE_DIR", "/var/lib/refindmgr"))
    return root / BASELINE_FILENAME


def _tracked_file(path: Path) -> dict:
    try:
        stat = path.stat()
    except OSError:
        return {"path": str(path), "present": False, "sha256": None, "size": None}
    return {
        "path": str(path),
        "present": path.is_file(),
        "sha256": _sha256(path) if path.is_file() else None,
        "size": stat.st_size if path.is_file() else None,
    }


def create_baseline(
    refind_dir: Path,
    inventory: Optional[OsInventory] = None,
    *,
    boot_dir: Path = Path("/boot"),
) -> dict:
    """Create an update-aware snapshot without writing it anywhere."""
    refind_dir = Path(refind_dir)
    inventory = inventory or build_inventory(refind_dir)
    esp_root = system_mod.esp_root_from_refind_dir(refind_dir)
    tracked: dict[str, dict] = {}

    def add(key: str, path: Path) -> None:
        tracked[key] = _tracked_file(path)

    if esp_root is not None:
        for loader in inventory.loaders:
            add("loader:" + loader.path.casefold(), esp_root / loader.path)
    for name in ("refind_x64.efi", "refind_aa64.efi", "refind_ia32.efi"):
        candidate = refind_dir / name
        if candidate.is_file():
            add("refind:" + name, candidate)

    compat_manifest = refind_dir / ".refindmgr" / "firmware-compat.json"
    compat: Optional[dict] = None
    try:
        raw = json.loads(compat_manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raw = None
    if isinstance(raw, dict) and raw.get("mode") == "firmware-compat":
        for key in ("active_loader", "source_binary", "grub_loader", "windows_loader"):
            value = raw.get(key)
            if isinstance(value, str) and value:
                add("compat:" + key, Path(value))
        active_hash = tracked.get("compat:active_loader", {}).get("sha256")
        expected = raw.get("refind_sha256")
        original = raw.get("original_loader_sha256")
        compat = {
            "active_loader": raw.get("active_loader"),
            "active_sha256": active_hash,
            "expected_refind_sha256": expected,
            "original_loader_sha256": original,
            "state": "healthy" if active_hash == expected else (
                "original-loader-restored" if active_hash == original else "changed-loader"
            ),
            "linux_mode": raw.get("linux_mode"),
        }
        if raw.get("linux_mode") == "direct":
            for name in ("refind-vmlinuz", "refind-initrd.img"):
                path = Path(boot_dir) / name
                add("direct:" + name, path)
                try:
                    target = path.resolve(strict=True)
                except OSError:
                    continue
                add("direct-target:" + name, target)

    return {
        "schema": BASELINE_SCHEMA,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "refind_dir": str(refind_dir),
        "architecture": inventory.architecture,
        "firmware_bits": inventory.firmware_bits,
        "runtime_os": inventory.runtime.distro_id,
        "files": tracked,
        "compatibility": compat,
    }


def save_baseline(snapshot: dict, path: Optional[Path] = None) -> Path:
    destination = baseline_path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(snapshot, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def load_baseline(path: Optional[Path] = None) -> Optional[dict]:
    source = baseline_path(path)
    if not source.is_file():
        return None
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Baseline OS/loader rusak: {exc}") from exc
    if data.get("schema") != BASELINE_SCHEMA or not isinstance(data.get("files"), dict):
        raise ValueError("Format baseline OS/loader tidak dikenali.")
    return data


def compare_baseline(previous: dict, current: dict) -> tuple[list[str], list[str]]:
    """Return (unchanged, changes) with explicit compatibility drift."""
    unchanged: list[str] = []
    changes: list[str] = []
    old_files = previous.get("files") or {}
    new_files = current.get("files") or {}
    for key in sorted(set(old_files) | set(new_files)):
        old = old_files.get(key)
        new = new_files.get(key)
        if old is None:
            changes.append(f"Loader baru terdeteksi: {new.get('path', key)}")
            continue
        if new is None or not new.get("present"):
            changes.append(f"File baseline hilang: {old.get('path', key)}")
            continue
        if old.get("sha256") != new.get("sha256"):
            changes.append(f"Isi file berubah: {new.get('path', key)}")
        else:
            unchanged.append(str(new.get("path", key)))
    compat = current.get("compatibility")
    if isinstance(compat, dict) and compat.get("state") != "healthy":
        if compat.get("state") == "original-loader-restored":
            changes.append(
                "Pembaruan sistem mengembalikan loader vendor asli pada path kompatibilitas; "
                "reapply rEFInd tersedia secara eksplisit."
            )
        else:
            changes.append(
                "Loader pada path kompatibilitas berubah ke hash yang belum dikenal; "
                "jangan overwrite tanpa meninjau hash dan backup."
            )
    if previous.get("architecture") != current.get("architecture"):
        changes.append("Arsitektur firmware berbeda dari baseline.")
    return unchanged, changes
