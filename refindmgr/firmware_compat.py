"""Safe firmware-compatibility mode for UEFI implementations that ignore BootOrder.

The mode deliberately reuses a firmware-recognised vendor path (currently
EFI/ubuntu/shimx64.efi) to launch rEFInd, while preserving the genuine shim and
GRUB files.  Every mutation is backed up and described by a JSON manifest so it
can be audited and reversed.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

MODE = "firmware-compat"
SCHEMA_VERSION = 1
STATE_FILENAME = "firmware-compat.json"
LEGACY_STATE_FILENAME = "hp-compat-state.txt"
STATE_DIRNAME = ".refindmgr"
COMPAT_BEGIN = "# refindmgr-firmware-compat: begin"
COMPAT_END = "# refindmgr-firmware-compat: end"


class FirmwareCompatError(RuntimeError):
    pass


@dataclass(frozen=True)
class CompatStatus:
    active_dir: Path
    mode: str
    managed: bool
    state_path: Path
    data: dict


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def state_path(active_dir: Path) -> Path:
    return Path(active_dir) / STATE_DIRNAME / STATE_FILENAME


def legacy_state_path(active_dir: Path) -> Path:
    return Path(active_dir) / STATE_DIRNAME / LEGACY_STATE_FILENAME


def _parse_legacy(path: Path) -> dict:
    data: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" not in raw or raw.lstrip().startswith("#"):
            continue
        key, value = raw.split("=", 1)
        data[key.strip()] = value.strip()
    return data


def load_status(active_dir: Path) -> Optional[CompatStatus]:
    active_dir = Path(active_dir)
    manifest = state_path(active_dir)
    if manifest.is_file():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FirmwareCompatError(f"Manifest mode kompatibilitas rusak: {exc}") from exc
        if data.get("schema") != SCHEMA_VERSION or data.get("mode") != MODE:
            raise FirmwareCompatError("Manifest mode kompatibilitas tidak dikenali.")
        return CompatStatus(active_dir, MODE, True, manifest, data)

    legacy = legacy_state_path(active_dir)
    if legacy.is_file():
        data = _parse_legacy(legacy)
        if data.get("mode") == "hp-firmware-compat":
            return CompatStatus(active_dir, "hp-firmware-compat-legacy", False, legacy, data)
    return None


def _default_esp_roots() -> list[Path]:
    roots: list[Path] = []
    env = os.environ.get("REFINDMGR_ESP_ROOTS")
    if env:
        roots.extend(Path(item) for item in env.split(os.pathsep) if item)
    roots.extend(Path(item) for item in ("/boot/efi", "/boot", "/efi"))
    unique: list[Path] = []
    for root in roots:
        if root not in unique:
            unique.append(root)
    return unique


def detect_compat_dir(esp_roots: Optional[Iterable[Path]] = None) -> Optional[Path]:
    for root in esp_roots or _default_esp_roots():
        efi = Path(root) / "EFI"
        try:
            children = sorted(path for path in efi.iterdir() if path.is_dir())
        except OSError:
            continue
        # A managed JSON manifest wins over a legacy marker.
        children.sort(key=lambda path: not state_path(path).is_file())
        for child in children:
            try:
                status = load_status(child)
                if status and (child / "refind.conf").is_file():
                    return child
            except (OSError, FirmwareCompatError):
                continue
    return None


def refind_binary(refind_dir: Path) -> Path:
    for name in ("refind_x64.efi", "refind_aa64.efi", "refind_ia32.efi"):
        candidate = Path(refind_dir) / name
        if candidate.is_file():
            return candidate
    raise FirmwareCompatError(f"Binari rEFInd tidak ditemukan di {refind_dir}")


def secure_boot_enabled() -> Optional[bool]:
    mokutil = shutil.which("mokutil")
    if mokutil:
        result = subprocess.run([mokutil, "--sb-state"], capture_output=True, text=True)
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
            pass
    return None


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.refindmgr-{os.getpid()}.tmp")
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _copy_assets(source_dir: Path, target_dir: Path) -> list[str]:
    copied: list[str] = []
    for name in ("icons", "icons-backup", "drivers_x64", "drivers_aa64", "drivers_ia32", "themes", "fonts", "keys"):
        source = source_dir / name
        if not source.is_dir():
            continue
        shutil.copytree(source, target_dir / name, dirs_exist_ok=True)
        copied.append(name)
    return copied


def _active_theme_include(*configs: Path) -> Optional[str]:
    for config in configs:
        if not config.is_file():
            continue
        for line in config.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if stripped.lower().startswith("include themes/") and not stripped.startswith("#"):
                return stripped
    return None


def _compat_config(vendor: str, linux_mode: str, root_uuid: Optional[str], part_guid: Optional[str], theme_include: Optional[str]) -> str:
    lines = [
        "# Managed by refindmgr. Restore with: refindmgr firmware-compat restore --apply",
        "timeout 20",
        "use_nvram false",
    ]
    if theme_include:
        lines.extend([theme_include, ""])
    lines.extend([COMPAT_BEGIN, "scanfor manual", "showtools shutdown,reboot", "", 'menuentry "Ubuntu" {'])
    if linux_mode == "direct":
        if not root_uuid or not part_guid:
            raise FirmwareCompatError("Direct boot membutuhkan root UUID dan partition GUID.")
        lines.extend([
            f"    volume {part_guid}",
            "    loader /boot/refind-vmlinuz",
            "    initrd /boot/refind-initrd.img",
            f'    options "root=UUID={root_uuid} ro quiet splash vt.handoff=7"',
        ])
    else:
        lines.append(f"    loader /EFI/{vendor}/grubx64.efi")
    lines.extend([
        "}", "", 'menuentry "Windows" {',
        "    loader /EFI/Microsoft/Boot/bootmgfw.efi", "}", COMPAT_END, "",
    ])
    return "\n".join(lines)


def _write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.refindmgr-{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def detect_linux_boot(root_mount: str = "/", boot_dir: Path = Path("/boot")) -> dict:
    result = subprocess.run(
        ["findmnt", "-n", "-o", "UUID,PARTUUID", root_mount],
        capture_output=True, text=True,
    )
    fields = result.stdout.split()
    if result.returncode != 0 or len(fields) < 2:
        raise FirmwareCompatError("Tidak dapat mendeteksi UUID/PARTUUID filesystem root.")
    version = platform.uname().release
    kernel = Path(boot_dir) / f"vmlinuz-{version}"
    initrd = Path(boot_dir) / f"initrd.img-{version}"
    config = Path(boot_dir) / f"config-{version}"
    if not kernel.is_file() or not initrd.is_file():
        raise FirmwareCompatError("Kernel atau initrd aktif tidak ditemukan di /boot.")
    if not config.is_file() or "CONFIG_EFI_STUB=y" not in config.read_text(errors="replace"):
        raise FirmwareCompatError("Kernel aktif tidak mendukung EFI Stub.")
    return {"root_uuid": fields[0], "part_guid": fields[1], "kernel_version": version}


def _helper_content() -> str:
    return """#!/usr/bin/env bash
# Managed by refindmgr firmware-compat.
set -euo pipefail
requested="${1:-}"
use_version() {
  local version="$1"
  [ -s "/boot/vmlinuz-$version" ] || return 1
  [ -s "/boot/initrd.img-$version" ] || return 1
  [ -s "/boot/config-$version" ] || return 1
  grep -q '^CONFIG_EFI_STUB=y$' "/boot/config-$version" || return 1
  ln -sfn "vmlinuz-$version" /boot/refind-vmlinuz
  ln -sfn "initrd.img-$version" /boot/refind-initrd.img
}
if [ -n "$requested" ] && use_version "$requested"; then exit 0; fi
while IFS= read -r kernel; do
  if use_version "${kernel#vmlinuz-}"; then exit 0; fi
done < <(find /boot -maxdepth 1 -type f -name 'vmlinuz-*' -printf '%f\\n' | sort -V -r)
echo 'Tidak menemukan pasangan kernel/initrd EFI Stub.' >&2
exit 1
"""


def install_kernel_link_manager(kernel_version: str, system_root: Path = Path("/"), boot_dir: Path = Path("/boot")) -> list[str]:
    helper = Path(system_root) / "usr/local/sbin/refindmgr-refresh-kernel-links"
    postinst = Path(system_root) / "etc/kernel/postinst.d/zz-refindmgr-direct"
    postrm = Path(system_root) / "etc/kernel/postrm.d/zz-refindmgr-direct"
    kernel = Path(boot_dir) / f"vmlinuz-{kernel_version}"
    initrd = Path(boot_dir) / f"initrd.img-{kernel_version}"
    if not kernel.is_file() or not initrd.is_file():
        raise FirmwareCompatError("Kernel/initrd untuk symlink tidak ditemukan.")
    _write_text_atomic(helper, _helper_content())
    _write_text_atomic(postinst, "#!/bin/sh\nset -eu\nexec /usr/local/sbin/refindmgr-refresh-kernel-links \"${1:-}\"\n")
    _write_text_atomic(postrm, "#!/bin/sh\nset -eu\nexec /usr/local/sbin/refindmgr-refresh-kernel-links\n")
    for path in (helper, postinst, postrm):
        path.chmod(0o755)
    for link, target in ((Path(boot_dir) / "refind-vmlinuz", kernel.name), (Path(boot_dir) / "refind-initrd.img", initrd.name)):
        link.unlink(missing_ok=True)
        link.symlink_to(target)
    return [str(helper), str(postinst), str(postrm), str(Path(boot_dir) / "refind-vmlinuz"), str(Path(boot_dir) / "refind-initrd.img")]


def refresh_kernel_links(status: CompatStatus, boot_dir: Path = Path("/boot")) -> str:
    if status.data.get("linux_mode") != "direct":
        raise FirmwareCompatError("Mode aktif tidak menggunakan direct Linux boot.")
    versions: list[str] = []
    for kernel in Path(boot_dir).glob("vmlinuz-*"):
        version = kernel.name.removeprefix("vmlinuz-")
        if (Path(boot_dir) / f"initrd.img-{version}").is_file() and (Path(boot_dir) / f"config-{version}").is_file():
            if "CONFIG_EFI_STUB=y" in (Path(boot_dir) / f"config-{version}").read_text(errors="replace"):
                versions.append(version)
    if not versions:
        raise FirmwareCompatError("Tidak menemukan kernel EFI Stub yang lengkap.")
    versions.sort(key=lambda value: tuple(int(part) for part in re.findall(r"\d+", value)), reverse=True)
    chosen = versions[0]
    for link, target in ((Path(boot_dir) / "refind-vmlinuz", f"vmlinuz-{chosen}"), (Path(boot_dir) / "refind-initrd.img", f"initrd.img-{chosen}")):
        link.unlink(missing_ok=True)
        link.symlink_to(target)
    return chosen


def plan_install(source_dir: Path, target_dir: Optional[Path] = None, vendor: str = "ubuntu", linux_mode: str = "grub", linux_info: Optional[dict] = None) -> dict:
    source_dir = Path(source_dir)
    if source_dir.parent.name.upper() != "EFI":
        raise FirmwareCompatError("Source rEFInd harus berada di ESP/EFI/<folder>.")
    target_dir = Path(target_dir) if target_dir else source_dir.parent / vendor
    if source_dir.resolve() == target_dir.resolve():
        raise FirmwareCompatError("Source rEFInd dan target vendor tidak boleh direktori yang sama.")
    source_binary = refind_binary(source_dir)
    active_loader = target_dir / "shimx64.efi"
    grub = target_dir / "grubx64.efi"
    windows = source_dir.parent / "Microsoft/Boot/bootmgfw.efi"
    for path, label in ((source_binary, "rEFInd"), (active_loader, "shim vendor"), (grub, "GRUB"), (windows, "Windows Boot Manager")):
        if not path.is_file():
            raise FirmwareCompatError(f"{label} tidak ditemukan: {path}")
    refind_hash = sha256(source_binary)
    shim_hash = sha256(active_loader)
    if refind_hash == shim_hash and load_status(target_dir) is None:
        raise FirmwareCompatError("Loader vendor sudah identik dengan rEFInd tetapi tidak memiliki manifest; gunakan adopt terlebih dahulu.")
    if linux_mode not in {"grub", "direct"}:
        raise FirmwareCompatError("linux_mode harus 'grub' atau 'direct'.")
    linux_info = linux_info or {}
    return {
        "source_dir": str(source_dir), "active_dir": str(target_dir), "vendor": vendor,
        "source_binary": str(source_binary), "active_loader": str(active_loader),
        "grub_loader": str(grub), "windows_loader": str(windows),
        "refind_sha256": refind_hash, "original_loader_sha256": shim_hash,
        "linux_mode": linux_mode, "root_uuid": linux_info.get("root_uuid"),
        "part_guid": linux_info.get("part_guid"), "kernel_version": linux_info.get("kernel_version"),
    }


def apply_install(plan: dict, system_root: Path = Path("/"), boot_dir: Path = Path("/boot")) -> CompatStatus:
    source_dir = Path(plan["source_dir"])
    active_dir = Path(plan["active_dir"])
    if load_status(active_dir):
        raise FirmwareCompatError("Mode kompatibilitas sudah aktif.")
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = active_dir / STATE_DIRNAME / "firmware-compat" / "backups" / timestamp
    backup_dir.mkdir(parents=True, exist_ok=False)
    active_loader = Path(plan["active_loader"])
    config = active_dir / "refind.conf"
    original_loader_backup = backup_dir / active_loader.name
    config_backup: Optional[Path] = backup_dir / "refind.conf"
    original_config_present = config.is_file()
    shutil.copy2(active_loader, original_loader_backup)
    if original_config_present:
        shutil.copy2(config, config_backup)
    else:
        config_backup = None
    managed_linux: list[str] = []
    try:
        copied_assets = _copy_assets(source_dir, active_dir)
        _atomic_copy(Path(plan["source_binary"]), active_loader)
        _atomic_copy(Path(plan["source_binary"]), active_dir / Path(plan["source_binary"]).name)
        theme = _active_theme_include(config, source_dir / "refind.conf")
        content = _compat_config(plan["vendor"], plan["linux_mode"], plan.get("root_uuid"), plan.get("part_guid"), theme)
        _write_text_atomic(config, content)
        if plan["linux_mode"] == "direct":
            version = plan.get("kernel_version")
            if not version:
                raise FirmwareCompatError("Versi kernel direct boot tidak tersedia.")
            managed_linux = install_kernel_link_manager(version, system_root=system_root, boot_dir=boot_dir)
        data = {
            "schema": SCHEMA_VERSION, "mode": MODE, "created": timestamp,
            **plan,
            "original_loader_backup": str(original_loader_backup),
            "config_backup": str(config_backup) if config_backup else None,
            "original_config_present": original_config_present,
            "copied_assets": copied_assets, "managed_linux_files": managed_linux,
        }
        manifest = state_path(active_dir)
        _write_text_atomic(manifest, json.dumps(data, indent=2, sort_keys=True) + "\n")
        return CompatStatus(active_dir, MODE, True, manifest, data)
    except Exception:
        _atomic_copy(original_loader_backup, active_loader)
        if config_backup and config_backup.is_file():
            _atomic_copy(config_backup, config)
        elif not original_config_present:
            config.unlink(missing_ok=True)
        for raw in managed_linux:
            path = Path(raw)
            if path.is_symlink() or path.is_file():
                path.unlink(missing_ok=True)
        raise


def adopt_legacy(active_dir: Path, source_dir: Path, apply: bool = False) -> CompatStatus:
    active_dir, source_dir = Path(active_dir), Path(source_dir)
    status = load_status(active_dir)
    if status is None or status.managed:
        raise FirmwareCompatError("Tidak menemukan mode kompatibilitas legacy yang dapat diadopsi.")
    source_binary = refind_binary(source_dir)
    active_loader = active_dir / "shimx64.efi"
    if sha256(active_loader) != sha256(source_binary):
        raise FirmwareCompatError("Loader aktif tidak identik dengan rEFInd dedicated; adopsi ditolak.")
    backup_raw = status.data.get("shim_backup")
    backup = Path(backup_raw) if backup_raw else active_dir / "shimx64.efi.bak"
    if not backup.is_file():
        raise FirmwareCompatError("Backup shim asli tidak ditemukan.")
    config_backups = sorted(active_dir.glob("refind.conf.pre-hp-compat-*"))
    data = {
        "schema": SCHEMA_VERSION, "mode": MODE, "created": status.data.get("created", "legacy"),
        "adopted_legacy": True, "source_dir": str(source_dir), "active_dir": str(active_dir),
        "vendor": active_dir.name, "source_binary": str(source_binary),
        "active_loader": str(active_loader), "grub_loader": str(active_dir / "grubx64.efi"),
        "windows_loader": str(active_dir.parent / "Microsoft/Boot/bootmgfw.efi"),
        "refind_sha256": sha256(source_binary), "original_loader_sha256": sha256(backup),
        "original_loader_backup": str(backup),
        "config_backup": str(config_backups[-1]) if config_backups else None,
        "linux_mode": "direct" if status.data.get("linux_mode") == "efi-stub-direct" else "grub",
        "root_uuid": status.data.get("linux_root_uuid"), "part_guid": status.data.get("linux_volume_guid"),
        "managed_linux_files": [value for key, value in status.data.items() if key in {"linux_kernel", "linux_initrd", "kernel_link_helper"}],
    }
    if data["linux_mode"] == "direct":
        for path in (
            "/etc/kernel/postinst.d/zz-refindmgr-direct",
            "/etc/kernel/postrm.d/zz-refindmgr-direct",
        ):
            if path not in data["managed_linux_files"]:
                data["managed_linux_files"].append(path)
    if apply:
        _write_text_atomic(state_path(active_dir), json.dumps(data, indent=2, sort_keys=True) + "\n")
        return load_status(active_dir)  # type: ignore[return-value]
    return CompatStatus(active_dir, MODE, False, state_path(active_dir), data)


def restore(status: CompatStatus, apply: bool = False) -> dict:
    if not status.managed:
        raise FirmwareCompatError("Mode legacy harus di-adopt sebelum dapat dipulihkan otomatis.")
    data = status.data
    active_loader = Path(data["active_loader"])
    loader_backup = Path(data["original_loader_backup"])
    if not loader_backup.is_file() or sha256(loader_backup) != data["original_loader_sha256"]:
        raise FirmwareCompatError("Backup loader asli hilang atau hash berubah; restore ditolak.")
    if not active_loader.is_file() or sha256(active_loader) != data["refind_sha256"]:
        raise FirmwareCompatError("Loader aktif berubah sejak mode dipasang; restore ditolak agar tidak menimpa file tak dikenal.")
    result = {"active_loader": str(active_loader), "loader_backup": str(loader_backup), "config_backup": data.get("config_backup")}
    if not apply:
        return result
    config = Path(data["active_dir"]) / "refind.conf"
    rollback_dir = status.state_path.parent / "firmware-compat" / f"restore-rollback-{time.strftime('%Y%m%d-%H%M%S')}"
    rollback_dir.mkdir(parents=True, exist_ok=False)
    shutil.copy2(active_loader, rollback_dir / active_loader.name)
    if config.is_file():
        shutil.copy2(config, rollback_dir / "refind.conf")
    _atomic_copy(loader_backup, active_loader)
    config_backup = data.get("config_backup")
    if config_backup and Path(config_backup).is_file():
        _atomic_copy(Path(config_backup), config)
    elif not data.get("original_config_present", True):
        config.unlink(missing_ok=True)
    for raw in data.get("managed_linux_files", []):
        path = Path(raw)
        try:
            if path.is_symlink() or path.is_file():
                path.unlink()
        except OSError:
            pass
    restored_manifest = status.state_path.with_name(f"firmware-compat.restored-{time.strftime('%Y%m%d-%H%M%S')}.json")
    os.replace(status.state_path, restored_manifest)
    result["rollback_dir"] = str(rollback_dir)
    result["restored_manifest"] = str(restored_manifest)
    return result
