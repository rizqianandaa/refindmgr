"""Read-only UEFI/ESP forensic diagnostics for refindmgr.

The module deliberately separates discovery from repair.  It may inspect
mounted filesystems and, only when explicitly requested, temporarily mount an
unmounted ESP read-only.  It never writes an ESP, NVRAM variable, or BootOrder.
"""
from __future__ import annotations

import getpass
import json
import os
import re
import shutil
import socket
import subprocess

from . import procs
import tempfile
import zipfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

ESP_PARTTYPE = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
FAT_TYPES = {"vfat", "fat", "fat12", "fat16", "fat32", "msdos"}
EFI_SUFFIXES = (".efi", ".efi.bak")
KNOWN_MOUNT_ROOTS = (Path("/boot/efi"), Path("/boot"), Path("/efi"))
RUN_TIMEOUT_SECONDS = 20
BOOT_NUMBER_RE = re.compile(r"^[0-9A-Fa-f]{4}$")
SECURE_BOOT_ENABLED_AMBIGUITY = "Secure Boot aktif; kompatibilitas loader belum dapat dipastikan"


class DiagnosticError(Exception):
    """A read-only diagnostic could not be completed safely."""


@dataclass
class EspInfo:
    device: str
    name: str = ""
    fstype: str = ""
    parttype: str = ""
    partuuid: str = ""
    uuid: str = ""
    size: str = ""
    read_only: bool = False
    mountpoint: Optional[str] = None
    inspected: bool = False
    inspection_error: Optional[str] = None
    # A partition is only a confirmed ESP when it carries the ESP GPT type or
    # a read-only inspection actually found an EFI directory on it.  Plain FAT
    # media (USB sticks, SD cards) stay unconfirmed candidates.
    confirmed_esp: bool = False


@dataclass
class BootEntry:
    number: str
    label: str
    active: bool = False
    partuuid: Optional[str] = None
    efi_path: Optional[str] = None
    raw_path: str = ""


@dataclass
class EfiFile:
    esp_device: str
    partuuid: str
    relative_path: str
    size: int
    sha256: str
    identity: str = "unknown"
    canonical: bool = False


@dataclass
class BootState:
    current: Optional[str] = None
    order: List[str] = field(default_factory=list)
    next: Optional[str] = None
    entries: List[BootEntry] = field(default_factory=list)
    error: Optional[str] = None
    # BootOrder tokens rejected by the Boot#### hex validation; kept so the
    # information is never silently dropped from the report.
    invalid_order: List[str] = field(default_factory=list)


@dataclass
class DiagnosticReport:
    generated_at: str
    uefi_runtime: bool
    secure_boot: Optional[bool]
    esps: List[EspInfo]
    boot: BootState
    files: List[EfiFile]
    refind_configs: List[str]
    compat_manifests: List[str]
    active_entry: Optional[BootEntry]
    active_esp_device: Optional[str]
    active_loader: Optional[str]
    active_loader_identity: Optional[str]
    active_refind_conf: Optional[str]
    ambiguities: List[str]
    warnings: List[str]
    commands: Dict[str, str] = field(default_factory=dict)

    @property
    def setup_safe(self) -> bool:
        return not self.ambiguities


RunFn = Callable[..., subprocess.CompletedProcess]


def _run(
    command: Sequence[str],
    run_fn: RunFn = subprocess.run,
    timeout: int = RUN_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess:
    return procs.run_command(command, DiagnosticError, run_fn=run_fn, timeout=timeout)


def _flatten_lsblk(nodes: Iterable[dict]) -> Iterator[dict]:
    for node in nodes:
        yield node
        yield from _flatten_lsblk(node.get("children") or [])


def _mountpoints(node: dict) -> List[str]:
    values = node.get("mountpoints")
    if values is None:
        values = [node.get("mountpoint")]
    elif not isinstance(values, list):
        values = [values]
    return [str(value) for value in values if value]


def parse_lsblk_json(raw: str) -> List[EspInfo]:
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise DiagnosticError(f"Output lsblk bukan JSON yang valid: {exc}") from exc
    esps: List[EspInfo] = []
    for node in _flatten_lsblk(data.get("blockdevices") or []):
        if str(node.get("type") or "").lower() not in {"part", "crypt", "lvm"}:
            continue
        parttype = str(node.get("parttype") or "").lower()
        fstype = str(node.get("fstype") or "").lower()
        # Broken/repartitioned systems can contain a real old ESP whose GPT
        # type was accidentally changed.  Treat every FAT partition as an ESP
        # candidate and verify it by looking for an EFI directory read-only.
        confirmed = parttype == ESP_PARTTYPE
        is_esp = confirmed or fstype in FAT_TYPES
        if not is_esp:
            continue
        points = _mountpoints(node)
        esps.append(EspInfo(
            device=str(node.get("path") or ("/dev/" + str(node.get("name") or ""))),
            name=str(node.get("name") or ""),
            fstype=fstype,
            parttype=parttype,
            partuuid=str(node.get("partuuid") or "").lower(),
            uuid=str(node.get("uuid") or ""),
            size=str(node.get("size") or ""),
            read_only=str(node.get("ro") or "0").lower() in {"1", "true", "yes"},
            mountpoint=points[0] if points else None,
            confirmed_esp=confirmed,
        ))
    return sorted(esps, key=lambda item: (item.device, item.partuuid))


def discover_esps(run_fn: RunFn = subprocess.run) -> Tuple[List[EspInfo], str]:
    command = [
        "lsblk", "--json", "--paths", "--output",
        "NAME,PATH,TYPE,FSTYPE,PARTTYPE,PARTUUID,UUID,MOUNTPOINTS,SIZE,RO",
    ]
    result = _run(command, run_fn)
    if result.returncode != 0:
        raise DiagnosticError((result.stderr or result.stdout or "lsblk gagal").strip())
    return parse_lsblk_json(result.stdout), " ".join(command)


def _normalize_efi_path(value: str) -> str:
    return "/" + value.replace("\\", "/").strip("/")


def parse_efibootmgr(raw: str) -> BootState:
    state = BootState()
    entry_re = re.compile(r"^Boot([0-9A-Fa-f]{4})(\*)?\s+(.+)$")
    hd_re = re.compile(r"HD\([^,]+,GPT,([0-9A-Fa-f-]{36}),", re.IGNORECASE)
    file_re = re.compile(r"File\(([^)]+)\)", re.IGNORECASE)
    for original in (raw or "").splitlines():
        line = original.strip()
        if line.startswith("BootCurrent:"):
            state.current = line.split(":", 1)[1].strip().upper() or None
            continue
        if line.startswith("BootOrder:"):
            value = line.split(":", 1)[1].strip()
            # Only well-formed Boot#### values may enter the order; anything
            # else would break int(value, 16) further down the pipeline.
            for item in value.split(","):
                token = item.strip()
                if not token:
                    continue
                if BOOT_NUMBER_RE.match(token):
                    state.order.append(token.upper())
                else:
                    state.invalid_order.append(token)
            continue
        if line.startswith("BootNext:"):
            state.next = line.split(":", 1)[1].strip().upper() or None
            continue
        match = entry_re.match(line)
        if not match:
            continue
        number, star, remainder = match.groups()
        boundary = len(remainder)
        for token in ("HD(", "VenHw(", "PciRoot(", "FvFile("):
            pos = remainder.find(token)
            if pos >= 0:
                boundary = min(boundary, pos)
        label = remainder[:boundary].strip()
        raw_path = remainder[boundary:].strip()
        hd_match = hd_re.search(raw_path)
        file_match = file_re.search(raw_path)
        state.entries.append(BootEntry(
            number=number.upper(),
            label=label,
            active=bool(star),
            partuuid=hd_match.group(1).lower() if hd_match else None,
            efi_path=_normalize_efi_path(file_match.group(1)) if file_match else None,
            raw_path=raw_path,
        ))
    return state


def discover_boot_state(run_fn: RunFn = subprocess.run) -> Tuple[BootState, str]:
    command = ["efibootmgr", "-v"]
    try:
        result = _run(command, run_fn)
    except DiagnosticError as exc:
        return BootState(error=str(exc)), " ".join(command)
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "efibootmgr gagal").strip()
        return BootState(error=message), " ".join(command)
    return parse_efibootmgr(result.stdout), " ".join(command)


def discover_secure_boot(run_fn: RunFn = subprocess.run) -> Optional[bool]:
    mokutil = shutil.which("mokutil")
    if mokutil:
        result = _run([mokutil, "--sb-state"], run_fn)
        text = f"{result.stdout}\n{result.stderr}".lower()
        if "secureboot enabled" in text:
            return True
        if "secureboot disabled" in text:
            return False
    for variable in Path("/sys/firmware/efi/efivars").glob("SecureBoot-*"):
        try:
            raw = variable.read_bytes()
            if len(raw) >= 5:
                return raw[4] == 1
        except OSError:
            continue
    return None


from .hashing import sha256_file


def _is_loader_candidate(path: Path) -> bool:
    name = path.name.lower()
    return name.endswith(EFI_SUFFIXES) or ".efi.pre-" in name


def _canonical_identity(relative_path: str) -> Optional[str]:
    low = relative_path.lower()
    name = Path(low).name
    if "/efi/refind/" in low and name.startswith("refind_") and ".efi" in name:
        return "refind"
    if low == "/efi/microsoft/boot/bootmgfw.efi":
        return "windows"
    if name.startswith(("grubx64.efi", "grubaa64.efi", "grubia32.efi")):
        return "grub"
    if name.startswith(("shimx64.efi", "shimaa64.efi", "shimia32.efi")):
        return "shim"
    if low.startswith("/efi/boot/") and name.startswith("boot") and name.endswith(".efi"):
        return "fallback"
    if name.startswith("refind_") and ".efi" in name:
        return "refind"
    return None


def scan_esp(root: Path, esp: EspInfo) -> Tuple[List[EfiFile], List[str], List[str]]:
    files: List[EfiFile] = []
    configs: List[str] = []
    manifests: List[str] = []
    efi_root = root / "EFI"
    if not efi_root.is_dir():
        raise DiagnosticError(f"Direktori EFI tidak ditemukan di {root}")
    try:
        paths = sorted(efi_root.rglob("*"), key=lambda p: str(p).lower())
    except OSError as exc:
        raise DiagnosticError(f"Tidak dapat membaca {efi_root}: {exc}") from exc
    for path in paths:
        try:
            if not path.is_file() or path.is_symlink():
                continue
            relative = "/" + path.relative_to(root).as_posix()
            if path.name.lower() == "refind.conf":
                configs.append(relative)
            if path.name == "firmware-compat.json" and path.parent.name == ".refindmgr":
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError):
                    data = {}
                if data.get("mode") == "firmware-compat":
                    manifests.append(relative)
            if not _is_loader_candidate(path):
                continue
            files.append(EfiFile(
                esp_device=esp.device,
                partuuid=esp.partuuid,
                relative_path=relative,
                size=path.stat().st_size,
                sha256=sha256_file(path),
                identity=_canonical_identity(relative) or "unknown",
                canonical=_canonical_identity(relative) is not None,
            ))
        except OSError:
            continue
    return files, configs, manifests


def classify_hash_matches(files: List[EfiFile]) -> None:
    """Propagate trusted canonical identities to byte-identical copies."""
    priority = {"refind": 5, "windows": 4, "shim": 3, "grub": 2, "fallback": 1, "unknown": 0}
    identities: Dict[str, str] = {}
    for item in files:
        if item.canonical and priority.get(item.identity, 0) > priority.get(identities.get(item.sha256, "unknown"), 0):
            identities[item.sha256] = item.identity
    for item in files:
        known = identities.get(item.sha256)
        if known:
            item.identity = known


@contextmanager
def inspected_root(esp: EspInfo, scan_unmounted: bool, run_fn: RunFn = subprocess.run) -> Iterator[Optional[Path]]:
    if esp.mountpoint:
        yield Path(esp.mountpoint)
        return
    if not scan_unmounted:
        yield None
        return
    if os.name != "posix" or not hasattr(os, "geteuid") or os.geteuid() != 0:
        esp.inspection_error = "butuh root untuk mount read-only"
        yield None
        return
    mount_dir = Path(tempfile.mkdtemp(prefix="refindmgr-esp-"))
    command = ["mount", "-o", "ro,nosuid,nodev,noexec", esp.device, str(mount_dir)]
    try:
        result = _run(command, run_fn, timeout=RUN_TIMEOUT_SECONDS)
    except DiagnosticError as exc:
        esp.inspection_error = str(exc)
        shutil.rmtree(mount_dir, ignore_errors=True)
        yield None
        return
    if result.returncode != 0:
        esp.inspection_error = (result.stderr or result.stdout or "mount read-only gagal").strip()
        shutil.rmtree(mount_dir, ignore_errors=True)
        yield None
        return
    try:
        yield mount_dir
    finally:
        try:
            _run(["umount", str(mount_dir)], run_fn, timeout=RUN_TIMEOUT_SECONDS)
        except DiagnosticError as exc:
            esp.inspection_error = str(exc)
        shutil.rmtree(mount_dir, ignore_errors=True)


def _find_file(files: List[EfiFile], partuuid: Optional[str], efi_path: Optional[str]) -> Optional[EfiFile]:
    if not efi_path:
        return None
    wanted = _normalize_efi_path(efi_path).lower()
    matches = [item for item in files if item.relative_path.lower() == wanted]
    if partuuid:
        same_esp = [item for item in matches if item.partuuid.lower() == partuuid.lower()]
        if same_esp:
            return same_esp[0]
    return matches[0] if len(matches) == 1 else None


def collect_report(
    *,
    scan_unmounted: bool = False,
    run_fn: RunFn = subprocess.run,
    uefi_runtime: Optional[bool] = None,
    secure_boot: object = "auto",
    allow_secure_boot: bool = False,
) -> DiagnosticReport:
    esps, lsblk_command = discover_esps(run_fn)
    boot, boot_command = discover_boot_state(run_fn)
    all_files: List[EfiFile] = []
    configs: List[str] = []
    manifests: List[str] = []
    warnings: List[str] = []
    for esp in esps:
        with inspected_root(esp, scan_unmounted, run_fn) as root:
            if root is None:
                if esp.mountpoint is None:
                    warnings.append(f"ESP {esp.device} belum diperiksa karena tidak ter-mount")
                continue
            try:
                files, found_configs, found_manifests = scan_esp(root, esp)
            except DiagnosticError as exc:
                esp.inspection_error = str(exc)
                warnings.append(str(exc))
                continue
            esp.inspected = True
            # An EFI directory is the evidence that turns a FAT candidate into
            # a real ESP.  Without it the partition stays a plain FAT volume.
            esp.confirmed_esp = True
            all_files.extend(files)
            configs.extend(f"{esp.device}:{path}" for path in found_configs)
            manifests.extend(f"{esp.device}:{path}" for path in found_manifests)
    classify_hash_matches(all_files)

    active_entry = next((entry for entry in boot.entries if entry.number == boot.current), None)
    active_file = _find_file(all_files, active_entry.partuuid if active_entry else None, active_entry.efi_path if active_entry else None)
    active_esp = next((esp for esp in esps if active_entry and active_entry.partuuid and esp.partuuid == active_entry.partuuid), None)
    active_conf = None
    if active_file and active_file.identity == "refind":
        directory = str(Path(active_file.relative_path).parent).lower()
        candidate = next((item for item in configs if item.split(":", 1)[0] == active_file.esp_device and str(Path(item.split(":", 1)[1]).parent).lower() == directory), None)
        active_conf = candidate
    active_manifest = None
    if active_file:
        active_dir = str(Path(active_file.relative_path).parent).lower()
        active_manifest = next((
            item for item in manifests
            if item.split(":", 1)[0] == active_file.esp_device
            and str(Path(item.split(":", 1)[1]).parent.parent).lower() == active_dir
        ), None)

    ambiguities: List[str] = []
    runtime = Path("/sys/firmware/efi").is_dir() if uefi_runtime is None else uefi_runtime
    secure_state = discover_secure_boot(run_fn) if secure_boot == "auto" else secure_boot
    confirmed_esps = [esp for esp in esps if esp.confirmed_esp]
    candidate_esps = [esp for esp in esps if not esp.confirmed_esp]
    for esp in candidate_esps:
        # A FAT USB stick or SD card must never inflate the ESP count.
        warnings.append(
            f"Partisi FAT {esp.device} belum terbukti sebagai ESP "
            "(tipe GPT bukan ESP dan direktori EFI belum ditemukan)"
        )
    if not confirmed_esps:
        ambiguities.append("Tidak ada EFI System Partition yang terdeteksi")
    elif len(confirmed_esps) > 1:
        ambiguities.append(f"Ditemukan {len(confirmed_esps)} EFI System Partition")
    if runtime and boot.error:
        ambiguities.append(f"State NVRAM tidak dapat dibaca: {boot.error}")
    if runtime and secure_state is None:
        ambiguities.append("Status Secure Boot tidak dapat dipastikan")
    elif secure_state is True:
        ambiguities.append(SECURE_BOOT_ENABLED_AMBIGUITY)
    if runtime and not boot.current:
        ambiguities.append("BootCurrent tidak tersedia")
    elif boot.current and active_entry is None:
        ambiguities.append(f"BootCurrent {boot.current} tidak memiliki entry yang cocok")
    if active_entry and active_entry.partuuid and active_esp is None:
        ambiguities.append("ESP untuk BootCurrent tidak cocok dengan hasil lsblk")
    if active_esp is not None and not active_esp.inspected:
        ambiguities.append("ESP aktif belum dapat diperiksa secara read-only")
    if active_entry and active_entry.efi_path and active_esp and active_esp.inspected and active_file is None:
        ambiguities.append("File loader BootCurrent tidak ditemukan pada ESP aktif")
    if active_file and active_file.identity == "unknown":
        ambiguities.append("Identitas loader aktif belum dapat dipastikan")
    if active_file and active_file.identity == "refind":
        low = active_file.relative_path.lower()
        if not active_manifest and not (low.startswith("/efi/refind/") and Path(low).name.startswith("refind_")):
            ambiguities.append("rEFInd aktif memakai path vendor/nonkanonis tanpa bukti manifest")
    refind_candidates = [item for item in all_files if item.identity == "refind"]
    refind_hashes = {item.sha256 for item in refind_candidates}
    if len(refind_hashes) > 1:
        warnings.append(f"Ditemukan {len(refind_hashes)} binari rEFInd berbeda")
    if len(configs) > 1:
        warnings.append(f"Ditemukan {len(configs)} konfigurasi refind.conf")
        if active_conf is None:
            ambiguities.append(f"Ditemukan {len(configs)} kandidat konfigurasi refind.conf tanpa konfigurasi aktif yang pasti")
    if len(set(boot.order)) != len(boot.order):
        ambiguities.append("BootOrder berisi entry duplikat")
    known_numbers = {entry.number for entry in boot.entries}
    # 2001+ are firmware-defined removable/optical/network placeholders on
    # many machines and are not always printed as ordinary Boot#### entries.
    stale_order = [number for number in boot.order if number not in known_numbers and int(number, 16) < 0x2000]
    if stale_order:
        ambiguities.append("BootOrder merujuk entry yang tidak tersedia: " + ",".join(stale_order))
    if boot.invalid_order:
        warnings.append(
            "BootOrder berisi nilai yang bukan Boot#### heksadesimal dan diabaikan: "
            + ",".join(boot.invalid_order)
        )
    if allow_secure_boot and SECURE_BOOT_ENABLED_AMBIGUITY in ambiguities:
        # Only the "enabled" case may be overridden; an unknown Secure Boot
        # state keeps blocking setup because it cannot be reasoned about.
        ambiguities.remove(SECURE_BOOT_ENABLED_AMBIGUITY)
        warnings.append(
            "Secure Boot aktif (di-override oleh --allow-secure-boot); "
            "kompatibilitas loader belum dapat dipastikan"
        )

    return DiagnosticReport(
        generated_at=datetime.now(timezone.utc).isoformat(),
        uefi_runtime=runtime,
        secure_boot=secure_state,
        esps=esps,
        boot=boot,
        files=all_files,
        refind_configs=sorted(configs),
        compat_manifests=sorted(manifests),
        active_entry=active_entry,
        active_esp_device=active_esp.device if active_esp else None,
        active_loader=(f"{active_file.esp_device}:{active_file.relative_path}" if active_file else (active_entry.efi_path if active_entry else None)),
        active_loader_identity=active_file.identity if active_file else None,
        active_refind_conf=active_conf,
        ambiguities=ambiguities,
        warnings=warnings,
        commands={"lsblk": lsblk_command, "efibootmgr": boot_command},
    )


def format_report(report: DiagnosticReport) -> str:
    lines = [
        "=== Diagnosis forensik UEFI/ESP ===",
        f"Runtime UEFI: {'ya' if report.uefi_runtime else 'tidak'}",
        f"Secure Boot: {'aktif' if report.secure_boot is True else 'nonaktif' if report.secure_boot is False else 'tidak diketahui'}",
        f"ESP terdeteksi: {len(report.esps)}",
    ]
    for esp in report.esps:
        state = "diperiksa" if esp.inspected else "belum diperiksa"
        mount = esp.mountpoint or "tidak ter-mount"
        lines.append(f"- {esp.device} | PARTUUID {esp.partuuid or '-'} | {mount} | {state}")
        if esp.inspection_error:
            lines.append(f"  Gagal: {esp.inspection_error}")
    lines.extend([
        "",
        "=== Rantai boot aktif ===",
        f"BootCurrent: {report.boot.current or '-'}",
        f"BootOrder: {','.join(report.boot.order) if report.boot.order else '-'}",
        f"BootNext: {report.boot.next or '-'}",
    ])
    if report.active_entry:
        lines.append(f"Entry aktif: {report.active_entry.number} {report.active_entry.label}")
    lines.extend([
        f"ESP aktif: {report.active_esp_device or '-'}",
        f"Loader aktif: {report.active_loader or '-'}",
        f"Identitas loader: {report.active_loader_identity or 'belum diketahui'}",
        f"refind.conf aktif: {report.active_refind_conf or '-'}",
        "",
        f"Loader EFI diperiksa: {len(report.files)}",
        f"Konfigurasi rEFInd ditemukan: {len(report.refind_configs)}",
        f"Manifest kompatibilitas ditemukan: {len(report.compat_manifests)}",
    ])
    if report.warnings:
        lines.append("\nPeringatan:")
        lines.extend(f"- {item}" for item in report.warnings)
    if report.ambiguities:
        lines.append("\nAmbiguitas yang memblokir setup otomatis:")
        lines.extend(f"- {item}" for item in report.ambiguities)
        lines.append("\nKesimpulan: setup otomatis TIDAK AMAN dilanjutkan.")
    else:
        lines.append("\nKesimpulan: tidak ditemukan ambiguitas yang memblokir setup otomatis.")
    return "\n".join(lines)


def _redact_text(value: str) -> str:
    result = value
    try:
        username = getpass.getuser()
    except Exception:
        username = ""
    try:
        hostname = socket.gethostname()
    except Exception:
        hostname = ""
    if username:
        result = re.sub(rf"/home/{re.escape(username)}(?=/|$)", "/home/<redacted>", result, flags=re.IGNORECASE)
        result = result.replace(username, "<user>")
    if hostname:
        result = result.replace(hostname, "<hostname>")
    return result


def _redact(value):
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items()}
    return value


def report_dict(report: DiagnosticReport) -> dict:
    return _redact(asdict(report))


def export_report(report: DiagnosticReport, destination: Optional[Path] = None) -> Path:
    if destination is None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        destination = Path.cwd() / f"refindmgr-diagnostic-{stamp}.zip"
    destination = Path(destination).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = report_dict(report)
    text = _redact_text(format_report(report))
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("diagnostic.txt", text + "\n")
        archive.writestr("diagnostic.json", json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return destination


