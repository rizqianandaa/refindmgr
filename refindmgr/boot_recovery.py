"""Controlled cross-reboot boot testing, NVRAM cleanup, and recovery bundles.

Unlike boot_diagnostics, this module can perform narrowly-scoped mutations, but
only behind explicit --apply style calls, exact confirmations, persisted state,
and a recovery bundle.  It never creates or guesses a Boot#### entry.
"""
from __future__ import annotations

import json
import os
import re
import subprocess

from . import procs
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from . import boot_diagnostics as diag
from . import firmware_compat as compat


class BootRecoveryError(RuntimeError):
    pass


RunFn = Callable[..., subprocess.CompletedProcess]
STATE_VERSION = 1
RUN_TIMEOUT_SECONDS = 20
DEVICE_PARTITION_RE = re.compile(r"^(.*?)(?:p)?(\d+)$")
# A device path with no trailing optional data, e.g. HD(...)/File(...).
PLAIN_DEVICE_PATH_RE = re.compile(r"^(?:[A-Za-z][A-Za-z0-9]*\([^)]*\))(?:/(?:[A-Za-z][A-Za-z0-9]*\([^)]*\)))*$")


def _current_boot_id() -> Optional[str]:
    """Return the Linux boot ID, or None when it cannot be read."""
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    return value or None


def state_root() -> Path:
    override = os.environ.get("REFINDMGR_STATE_DIR")
    return Path(override) if override else Path("/var/lib/refindmgr")


def boot_test_path() -> Path:
    return state_root() / "boot-test.json"


def verification_path() -> Path:
    return state_root() / "verified-boot-entries.json"


def cleanup_journal_path() -> Path:
    return state_root() / "nvram-cleanup.json"


def _run(
    command: Sequence[str],
    run_fn: RunFn = subprocess.run,
    timeout: int = RUN_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess:
    return procs.run_command(command, BootRecoveryError, run_fn=run_fn, timeout=timeout)


def _prepare_state_dir(directory: Path) -> None:
    """Create the state directory privately without touching a foreign one.

    REFINDMGR_STATE_DIR can point anywhere, so an unconditional chmod would
    happily lock down /tmp.  Only a directory this call creates is chmod-ed.
    """
    if directory.is_dir():
        return
    os.makedirs(directory, mode=0o700, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:
        pass


def _atomic_json(path: Path, data: dict) -> None:
    _prepare_state_dir(path.parent)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temp = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temp.chmod(0o600)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def load_boot_test(path: Optional[Path] = None) -> Optional[dict]:
    path = path or boot_test_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BootRecoveryError(f"State boot test rusak: {exc}") from exc
    if data.get("version") != STATE_VERSION:
        raise BootRecoveryError("Versi state boot test tidak didukung")
    return data


def _entry_map(state: diag.BootState) -> Dict[str, diag.BootEntry]:
    return {entry.number.upper(): entry for entry in state.entries}


def _boot_snapshot(state: diag.BootState) -> dict:
    return {
        "current": state.current,
        "next": state.next,
        "order": list(state.order),
        "entries": [
            {
                "number": entry.number,
                "label": entry.label,
                "partuuid": entry.partuuid,
                "efi_path": entry.efi_path,
            }
            for entry in state.entries
        ],
    }


def writable_boot_order(state: diag.BootState, preferred: Optional[str] = None) -> List[str]:
    """Return only Boot#### values that efibootmgr can actually write.

    Some firmware exposes reserved values such as 2002/2004 in BootOrder but
    does not expose matching Boot#### variables. Passing those values back to
    `efibootmgr -o` rejects the whole update, so they must not be reconstructed.
    """
    known = {entry.number.upper() for entry in state.entries}
    values = ([preferred.upper()] if preferred else []) + list(state.order)
    result: List[str] = []
    for value in values:
        value = value.upper()
        if value in known and value not in result:
            result.append(value)
    return result


def validate_boot_target(report: diag.DiagnosticReport, entry: str) -> diag.EfiFile:
    entry = entry.upper().removeprefix("BOOT")
    target = _entry_map(report.boot).get(entry)
    if target is None or not target.partuuid or not target.efi_path:
        raise BootRecoveryError("Entry target tidak memiliki pemetaan GPT/File yang lengkap")
    matches = [
        item for item in report.files
        if item.partuuid == target.partuuid.lower()
        and item.relative_path.lower() == target.efi_path.lower()
    ]
    if len(matches) != 1:
        raise BootRecoveryError("File loader target tidak ditemukan secara unik pada ESP yang diperiksa")
    if matches[0].identity == "unknown":
        raise BootRecoveryError("Identitas loader target belum dapat dipastikan")
    return matches[0]


def _read_boot(run_fn: RunFn = subprocess.run) -> diag.BootState:
    state, _ = diag.discover_boot_state(run_fn)
    if state.error:
        raise BootRecoveryError(f"NVRAM tidak dapat dibaca: {state.error}")
    return state


def start_boot_test(entry: str, *, apply: bool = False, run_fn: RunFn = subprocess.run, path: Optional[Path] = None) -> dict:
    entry = entry.upper().removeprefix("BOOT")
    if len(entry) != 4 or any(ch not in "0123456789ABCDEF" for ch in entry):
        raise BootRecoveryError("Entry harus empat digit heksadesimal, misalnya 000A")
    current = load_boot_test(path)
    if current and current.get("phase") not in {
        "completed", "restored", "failed", "bootnext_failed",
        "bootnext_write_failed", "bootorder_write_failed",
    }:
        raise BootRecoveryError("Masih ada boot test aktif. Selesaikan atau restore terlebih dahulu.")
    boot = _read_boot(run_fn)
    entries = _entry_map(boot)
    if entry not in entries:
        raise BootRecoveryError(f"Boot{entry} tidak ditemukan")
    target = entries[entry]
    if not target.partuuid or not target.efi_path:
        raise BootRecoveryError("Entry target tidak memiliki path GPT/File yang dapat diverifikasi")
    result = {
        "version": STATE_VERSION,
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "phase": "preview",
        "target": entry,
        "target_label": target.label,
        "target_partuuid": target.partuuid,
        "target_path": target.efi_path,
        "original_current": boot.current,
        "original_order": boot.order,
        "writable_original_order": writable_boot_order(boot),
        "origin_boot_id": _current_boot_id(),
        "observations": [],
    }
    if not apply:
        return result
    state_path = path or boot_test_path()
    result["phase"] = "bootnext_writing"
    result["pending_boot_id"] = _current_boot_id()
    result["pre_bootnext_snapshot"] = _boot_snapshot(boot)
    _atomic_json(state_path, result)
    command = ["efibootmgr", "-n", entry]
    changed = _run(command, run_fn)
    if changed.returncode != 0:
        result["phase"] = "bootnext_write_failed"
        result["error"] = (changed.stderr or changed.stdout or "gagal menulis BootNext").strip()
        _atomic_json(state_path, result)
        raise BootRecoveryError((changed.stderr or changed.stdout or "gagal menulis BootNext").strip())
    result["phase"] = "bootnext_pending"
    result["bootnext_command"] = " ".join(command)
    _atomic_json(state_path, result)
    return result


def observe_boot_test(*, run_fn: RunFn = subprocess.run, path: Optional[Path] = None) -> dict:
    path = path or boot_test_path()
    state = load_boot_test(path)
    if state is None:
        raise BootRecoveryError("Belum ada boot test aktif")
    boot = _read_boot(run_fn)
    observation = {
        "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "boot_current": boot.current,
        "boot_order": boot.order,
        "boot_next": boot.next,
    }
    state.setdefault("observations", []).append(observation)
    target = state["target"]
    phase = state.get("phase")
    if phase in {"bootnext_writing", "bootnext_pending"}:
        if boot.current == target:
            state["phase"] = "bootnext_passed"
            state["recommendation"] = "BootNext berhasil. BootOrder permanen boleh diuji."
            record_verified_entry(target, state.get("target_label", ""), method="bootnext-cross-reboot", path=verification_path())
        else:
            state["phase"] = "bootnext_failed"
            state["recommendation"] = "BootNext tidak menjalankan target; pertahankan boot standar dan periksa firmware."
    elif phase in {"bootorder_writing", "bootorder_pending"}:
        actual_order = writable_boot_order(boot)
        proposed_order = state.get("proposed_order") or []
        original_order = state.get("writable_original_order") or []
        if boot.current == target and boot.order and boot.order[0] == target:
            state["phase"] = "completed"
            state["firmware_behavior"] = "accepted"
            state["recommendation"] = "BootOrder permanen bertahan; mode standar direkomendasikan."
            record_verified_entry(target, state.get("target_label", ""), method="cross-reboot", path=verification_path())
        else:
            state["phase"] = "bootorder_failed"
            if actual_order == original_order:
                state["firmware_behavior"] = "ignored"
                state["recommendation"] = "Firmware mengabaikan BootOrder uji dan mempertahankan urutan awal; mode kompatibilitas direkomendasikan."
            elif actual_order != proposed_order:
                state["firmware_behavior"] = "rebuilt"
                state["recommendation"] = "Firmware membangun ulang BootOrder; mode kompatibilitas direkomendasikan."
            else:
                state["firmware_behavior"] = "target-not-booted"
                state["recommendation"] = "BootOrder tersimpan tetapi target tidak menjadi BootCurrent; mode kompatibilitas direkomendasikan."
    _atomic_json(path, state)
    return state


def auto_observe_boot_test(*, run_fn: RunFn = subprocess.run, path: Optional[Path] = None) -> Optional[dict]:
    """Observe a pending test only after the machine has actually rebooted."""
    state = load_boot_test(path)
    if state is None or state.get("phase") not in {
        "bootnext_writing", "bootnext_pending", "bootorder_writing", "bootorder_pending",
    }:
        return None
    pending_boot_id = state.get("pending_boot_id")
    current_boot_id = _current_boot_id()
    if not pending_boot_id or not current_boot_id or pending_boot_id == current_boot_id:
        return None
    return observe_boot_test(run_fn=run_fn, path=path)


def promote_boot_order(*, apply: bool = False, recovery_bundle: Optional[Path] = None, run_fn: RunFn = subprocess.run, path: Optional[Path] = None) -> dict:
    path = path or boot_test_path()
    state = load_boot_test(path)
    if state is None or state.get("phase") != "bootnext_passed":
        raise BootRecoveryError("BootNext belum terbukti berhasil; BootOrder permanen tidak boleh diuji")
    boot = _read_boot(run_fn)
    target = state["target"]
    order = writable_boot_order(boot, preferred=target)
    if target not in order:
        raise BootRecoveryError(f"Boot{target} tidak memiliki variable NVRAM yang dapat ditulis")
    preview = dict(state)
    preview["proposed_order"] = order
    if not apply:
        return preview
    if recovery_bundle is None:
        raise BootRecoveryError("Uji BootOrder permanen membutuhkan --bundle paket recovery tervalidasi")
    validate_recovery_bundle(recovery_bundle)
    state["phase"] = "bootorder_writing"
    state["proposed_order"] = order
    state["pending_boot_id"] = _current_boot_id()
    state["pre_bootorder_snapshot"] = _boot_snapshot(boot)
    state["recovery_bundle"] = str(Path(recovery_bundle).resolve())
    _atomic_json(path, state)
    command = ["efibootmgr", "-o", ",".join(order)]
    changed = _run(command, run_fn)
    if changed.returncode != 0:
        state["phase"] = "bootorder_write_failed"
        state["error"] = (changed.stderr or changed.stdout or "gagal menulis BootOrder").strip()
        _atomic_json(path, state)
        raise BootRecoveryError((changed.stderr or changed.stdout or "gagal menulis BootOrder").strip())
    state["phase"] = "bootorder_pending"
    state["bootorder_command"] = " ".join(command)
    _atomic_json(path, state)
    return state


def restore_boot_order(*, apply: bool = False, run_fn: RunFn = subprocess.run, path: Optional[Path] = None) -> dict:
    path = path or boot_test_path()
    state = load_boot_test(path)
    if state is None:
        raise BootRecoveryError("Tidak ada state boot test")
    boot = _read_boot(run_fn)
    # The untouched original order is the only faithful restore target.  The
    # writable list deliberately drops firmware placeholders such as
    # 2001/2002/2003 (USB/CD/network), which would be lost forever if it were
    # written back as-is; it is kept only as a fallback.
    stored = list(dict.fromkeys(state.get("original_order") or []))
    known = {entry.number for entry in boot.entries}
    writable = [item for item in (state.get("writable_original_order") or stored) if item in known]
    order = stored or writable
    if not order:
        raise BootRecoveryError("BootOrder awal tidak tersimpan")
    preview = {
        "original_order": order,
        "fallback_order": writable,
        "phase": state.get("phase"),
    }
    if not apply:
        return preview
    if state.get("phase") in {"bootnext_writing", "bootnext_pending"}:
        cleared = _run(["efibootmgr", "-N"], run_fn)
        if cleared.returncode != 0:
            raise BootRecoveryError((cleared.stderr or cleared.stdout or "gagal membersihkan BootNext").strip())
    command = ["efibootmgr", "-o", ",".join(order)]
    changed = _run(command, run_fn)
    fallback_used = False
    if changed.returncode != 0:
        failure = (changed.stderr or changed.stdout or "gagal memulihkan BootOrder").strip()
        if not writable or writable == order:
            raise BootRecoveryError(failure)
        # Firmware rejected at least one entry from the original order; retry
        # with the subset that has real Boot#### variables and say so.
        command = ["efibootmgr", "-o", ",".join(writable)]
        changed = _run(command, run_fn)
        if changed.returncode != 0:
            raise BootRecoveryError((changed.stderr or changed.stdout or failure).strip())
        fallback_used = True
        dropped = [item for item in order if item not in writable]
        state["restore_warning"] = (
            "Firmware menolak BootOrder asli (" + failure + "); dipulihkan tanpa entry "
            + ",".join(dropped)
            + ". Entry placeholder firmware tersebut harus dikembalikan lewat menu firmware."
        )
        state["restore_dropped_entries"] = dropped
        order = writable
    state["phase"] = "restored"
    state["restored_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    state["restored_order"] = order
    state["restore_fallback_used"] = fallback_used
    _atomic_json(path, state)
    return state


def load_verified_entries(path: Optional[Path] = None) -> dict:
    path = path or verification_path()
    if not path.is_file():
        return {"version": 1, "entries": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BootRecoveryError(f"Ledger verifikasi rusak: {exc}") from exc
    return data


def record_verified_entry(entry: str, label: str, *, method: str, path: Optional[Path] = None) -> dict:
    path = path or verification_path()
    data = load_verified_entries(path)
    data.setdefault("entries", {})[entry.upper()] = {
        "label": label,
        "method": method,
        "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _atomic_json(path, data)
    return data


def confirm_manual_boot(entry: str, label: str, confirmation: str, *, path: Optional[Path] = None, run_fn: RunFn = subprocess.run) -> dict:
    if confirmation != "BOOT-BERHASIL":
        raise BootRecoveryError("Konfirmasi harus persis BOOT-BERHASIL")
    entry = entry.upper().removeprefix("BOOT")
    boot = _read_boot(run_fn)
    target = _entry_map(boot).get(entry)
    if target is None:
        raise BootRecoveryError(f"Boot{entry} tidak ditemukan")
    if label and label.lower() not in target.label.lower():
        raise BootRecoveryError(f"Label tidak cocok; Boot{entry} bernama '{target.label}'")
    return record_verified_entry(entry, target.label, method="manual-user-confirmed", path=path)


def classify_nvram_entries(boot: diag.BootState, esps: List[diag.EspInfo]) -> List[dict]:
    """Classify every Boot#### entry without making unknown entries deletable."""
    active = boot.current
    first = boot.order[0] if boot.order else None
    known_parts = {esp.partuuid for esp in esps}
    groups: Dict[tuple, List[diag.BootEntry]] = {}
    for entry in boot.entries:
        if entry.partuuid and entry.efi_path:
            groups.setdefault((entry.partuuid.lower(), entry.efi_path.lower()), []).append(entry)
    results: List[dict] = []
    for entry in boot.entries:
        classification = "kept"
        reason = "entry dipertahankan"
        deletable = False
        key = (entry.partuuid.lower(), entry.efi_path.lower()) if entry.partuuid and entry.efi_path else None
        path_lower = (entry.efi_path or "").replace("\\", "/").lower()
        fallback = path_lower.startswith("/efi/boot/boot")
        if entry.number == active:
            reason = "BootCurrent aktif"
        elif entry.number == first:
            reason = "urutan boot utama"
        elif int(entry.number, 16) >= 0x2000:
            classification = "firmware"
            reason = "entry khusus firmware"
        elif fallback:
            classification = "fallback"
            reason = "loader fallback UEFI dilindungi"
        elif not entry.partuuid or not entry.efi_path:
            classification = "unknown"
            reason = "identitas GPT/path tidak lengkap"
        elif key and len(groups.get(key, [])) > 1:
            ordered = sorted(groups[key], key=lambda item: boot.order.index(item.number) if item.number in boot.order else len(boot.order) + int(item.number, 16))
            preferred = ordered[0]
            if entry.number != preferred.number:
                classification = "duplicate"
                reason = f"duplikat Boot{preferred.number} dengan PARTUUID dan path yang sama"
                deletable = True
        elif entry.partuuid and entry.partuuid.lower() not in known_parts:
            classification = "stale"
            reason = "merujuk partisi yang tidak terdeteksi"
        results.append({
            "entry": entry.number,
            "label": entry.label,
            "partuuid": entry.partuuid,
            "path": entry.efi_path,
            "classification": classification,
            "reason": reason,
            "deletable": deletable,
        })
    return results


def analyze_nvram_cleanup(boot: diag.BootState, esps: List[diag.EspInfo]) -> List[dict]:
    """Return non-kept entries for compatibility with the cleanup command."""
    return [
        item for item in classify_nvram_entries(boot, esps)
        if item["classification"] in {"duplicate", "stale"}
    ]


from .hashing import sha256_bytes as _sha256_bytes


def _raw_entry_line(text: str, entry: str) -> Optional[str]:
    """Return the verbatim `efibootmgr -v` line describing Boot####."""
    prefix = f"boot{entry.lower()}"
    for line in (text or "").splitlines():
        stripped = line.strip()
        low = stripped.lower()
        if low.startswith(prefix) and (len(low) == len(prefix) or low[len(prefix)] in {"*", " ", "\t"}):
            return stripped
    return None


def _rollback_is_complete(entry: diag.BootEntry) -> bool:
    """True only when `efibootmgr -c` can reproduce the entry faithfully.

    Anything trailing the device path (Windows BCD arguments, vendor blobs)
    is optional data that -c cannot write back without --append-binary-args.
    """
    raw = (entry.raw_path or "").strip()
    if not raw:
        return False
    return bool(PLAIN_DEVICE_PATH_RE.match(raw))


def create_recovery_bundle(
    report: diag.DiagnosticReport,
    destination: Path,
    *,
    refind_dir: Optional[Path] = None,
    run_fn: RunFn = subprocess.run,
) -> Path:
    destination = Path(destination).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    boot_result = _run(["efibootmgr", "-v"], run_fn)
    if boot_result.returncode != 0:
        raise BootRecoveryError("efibootmgr -v gagal; paket recovery tidak dibuat")
    payloads: Dict[str, bytes] = {
        "diagnostic.json": (json.dumps(diag.report_dict(report), indent=2, ensure_ascii=False) + "\n").encode(),
        "diagnostic.txt": (diag.format_report(report) + "\n").encode(),
        "efibootmgr.txt": (boot_result.stdout or "").encode(),
    }
    instructions = (
        "refindmgr recovery bundle\n\n"
        "1. Validate first: refindmgr recovery validate <zip>\n"
        "2. From a live USB, mount the Linux root and active ESP read-write only after validation.\n"
        "3. Do not copy EFI files manually unless manifest.json records the same SHA-256.\n"
        "4. For managed compatibility mode, prefer: refindmgr firmware-compat restore --apply\n"
        "5. For a deleted NVRAM entry, prefer: refindmgr nvram-cleanup restore --apply\n"
        "6. efibootmgr.txt records the pre-change NVRAM state and BootOrder.\n"
    )
    payloads["RESTORE.txt"] = instructions.encode()
    if refind_dir:
        refind_dir = Path(refind_dir)
        for name in ("refind.conf", "refind_x64.efi", "refind_aa64.efi", "refind_ia32.efi"):
            source = refind_dir / name
            if source.is_file() and not source.is_symlink():
                payloads[f"files/active/{name}"] = source.read_bytes()
        status = compat.load_status(refind_dir)
        if status and status.managed:
            payloads["files/firmware-compat.json"] = status.state_path.read_bytes()
            for key in ("loader_backup", "config_backup"):
                value = status.data.get(key)
                if value:
                    source = Path(value)
                    if source.is_file() and not source.is_symlink():
                        payloads[f"files/backups/{key}-{source.name}"] = source.read_bytes()
    manifest = {
        "version": 1,
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "files": {name: {"sha256": _sha256_bytes(data), "size": len(data)} for name, data in sorted(payloads.items())},
        "boot_order": report.boot.order,
        "boot_current": report.boot.current,
        "verified_boot_entries": load_verified_entries(),
    }
    payloads["manifest.json"] = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    temp = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in sorted(payloads.items()):
                archive.writestr(name, data)
        validate_recovery_bundle(temp)
        os.replace(temp, destination)
    finally:
        temp.unlink(missing_ok=True)
    return destination


def validate_recovery_bundle(path: Path) -> dict:
    path = Path(path)
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or len(names) > 64:
                raise BootRecoveryError("Jumlah/nama file dalam paket recovery tidak aman")
            total_size = sum(item.file_size for item in archive.infolist())
            if total_size > 64 * 1024 * 1024 or any(item.file_size > 32 * 1024 * 1024 for item in archive.infolist()):
                raise BootRecoveryError("Ukuran isi paket recovery melewati batas aman")
            if "manifest.json" not in names:
                raise BootRecoveryError("manifest.json tidak ditemukan")
            if any(name.startswith("/") or ".." in Path(name).parts for name in names):
                raise BootRecoveryError("Paket recovery memiliki path tidak aman")
            manifest = json.loads(archive.read("manifest.json"))
            for name, expected in manifest.get("files", {}).items():
                data = archive.read(name)
                if len(data) != expected["size"] or _sha256_bytes(data) != expected["sha256"]:
                    raise BootRecoveryError(f"Hash paket recovery tidak cocok: {name}")
    except (OSError, zipfile.BadZipFile, KeyError, json.JSONDecodeError) as exc:
        raise BootRecoveryError(f"Paket recovery tidak valid: {exc}") from exc
    return manifest


def delete_nvram_entry(
    entry: str,
    confirmation: str,
    recovery_bundle: Path,
    *,
    apply: bool = False,
    run_fn: RunFn = subprocess.run,
    report: Optional[diag.DiagnosticReport] = None,
) -> dict:
    entry = entry.upper().removeprefix("BOOT")
    if confirmation != entry:
        raise BootRecoveryError(f"Konfirmasi harus persis {entry}")
    validate_recovery_bundle(recovery_bundle)
    report = report or diag.collect_report(scan_unmounted=False, run_fn=run_fn)
    candidates = {item["entry"]: item for item in classify_nvram_entries(report.boot, report.esps)}
    if entry not in candidates:
        raise BootRecoveryError("Entry bukan kandidat cleanup aman")
    if not candidates[entry].get("deletable"):
        raise BootRecoveryError("Entry hanya informasional dan belum aman untuk dihapus")
    if entry == report.boot.current or (report.boot.order and entry == report.boot.order[0]):
        raise BootRecoveryError("Entry aktif/utama tidak boleh dihapus")
    verified = load_verified_entries().get("entries", {})
    # Require evidence for at least one Linux-like and one Windows-like entry.
    windows_ok = any("windows" in data.get("label", "").lower() for data in verified.values())
    linux_ok = any(any(token in data.get("label", "").lower() for token in ("ubuntu", "linux", "refind")) for data in verified.values())
    if not (windows_ok and linux_ok):
        raise BootRecoveryError("Cleanup membutuhkan verifikasi boot Linux/rEFInd dan Windows terlebih dahulu")
    target = next(item for item in report.boot.entries if item.number == entry)
    target_path = (target.efi_path or "").replace("\\", "/").lower()
    if target_path.startswith("/efi/boot/boot"):
        raise BootRecoveryError("Loader fallback UEFI tidak boleh dihapus")
    esp = next((item for item in report.esps if target.partuuid and item.partuuid == target.partuuid.lower()), None)
    if esp is None:
        raise BootRecoveryError("Device partisi entry tidak dapat dipetakan untuk rollback")
    match = DEVICE_PARTITION_RE.match(esp.device)
    if not match:
        raise BootRecoveryError("Nama device tidak dapat dipetakan ke disk/nomor partisi")
    disk, partition = match.groups()
    if disk.endswith(("nvme", "mmcblk")):
        raise BootRecoveryError("Nama device ambigu; cleanup dibatalkan")
    # Capture the verbatim NVRAM line before anything is removed; optional
    # data cannot be recreated by `efibootmgr -c`, so the user must know.
    raw_state = _run(["efibootmgr", "-v"], run_fn)
    raw_entry_line = _raw_entry_line(raw_state.stdout or "", entry) if raw_state.returncode == 0 else None
    rollback_complete = _rollback_is_complete(target)
    rollback_warning = None
    if not rollback_complete:
        rollback_warning = (
            f"Rollback Boot{entry} TIDAK akan identik byte-per-byte: entry ini memiliki optional data "
            "atau device path yang tidak dapat dibuat ulang oleh 'efibootmgr -c'. "
            "Entry Windows khususnya dapat gagal boot setelah restore; "
            "siapkan media instalasi/repair Windows sebelum melanjutkan."
        )
    journal = {
        "version": 1,
        "phase": "preview",
        "entry": entry,
        "label": target.label,
        "partuuid": target.partuuid,
        "efi_path": target.efi_path,
        "raw_entry_line": raw_entry_line,
        "active": bool(target.active),
        "rollback_complete": rollback_complete,
        "rollback_warning": rollback_warning,
        "device": esp.device,
        "disk": disk,
        "partition": int(partition),
        "original_order": report.boot.order,
        "pre_change_snapshot": _boot_snapshot(report.boot),
        "preexisting_equivalent_entries": [
            item.number for item in report.boot.entries
            if item.number != entry
            and item.partuuid == target.partuuid
            and item.efi_path and target.efi_path
            and item.efi_path.lower() == target.efi_path.lower()
        ],
        "recovery_bundle": str(Path(recovery_bundle).resolve()),
    }
    result = {
        "candidate": candidates[entry],
        "recovery_bundle": str(recovery_bundle),
        "apply": apply,
        "rollback": {
            "disk": disk,
            "partition": int(partition),
            "label": target.label,
            "efi_path": target.efi_path,
            "active": bool(target.active),
            "raw_entry_line": raw_entry_line,
            "complete": rollback_complete,
        },
        "rollback_complete": rollback_complete,
        "warnings": [rollback_warning] if rollback_warning else [],
    }
    if not apply:
        return result
    journal_path = cleanup_journal_path()
    if journal_path.is_file():
        try:
            previous = json.loads(journal_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise BootRecoveryError(f"Journal cleanup lama rusak: {exc}") from exc
        if previous.get("phase") not in {"restored", "not-deleted", "delete-failed"}:
            raise BootRecoveryError("Masih ada transaksi cleanup yang belum dipulihkan")
    fresh_backup = _run(["efibootmgr", "-v"], run_fn)
    if fresh_backup.returncode != 0:
        raise BootRecoveryError("Backup NVRAM terbaru gagal; penghapusan dibatalkan")
    journal["phase"] = "deleting"
    journal["pre_change_efibootmgr"] = fresh_backup.stdout or ""
    journal["delete_command"] = ["efibootmgr", "-b", entry, "-B"]
    _atomic_json(journal_path, journal)
    changed = _run(journal["delete_command"], run_fn)
    if changed.returncode != 0:
        journal["phase"] = "delete-failed"
        journal["error"] = (changed.stderr or changed.stdout or "penghapusan NVRAM gagal").strip()
        _atomic_json(journal_path, journal)
        raise BootRecoveryError((changed.stderr or changed.stdout or "penghapusan NVRAM gagal").strip())
    after_delete = _read_boot(run_fn)
    if entry in _entry_map(after_delete):
        journal["phase"] = "delete-unverified"
        _atomic_json(journal_path, journal)
        raise BootRecoveryError("efibootmgr selesai tetapi entry masih ditemukan; jalankan restore/status sebelum mencoba lagi")
    journal["phase"] = "deleted"
    journal["deleted_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _atomic_json(journal_path, journal)
    result["deleted"] = entry
    result["journal"] = str(journal_path)
    return result


def restore_deleted_nvram_entry(*, apply: bool = False, run_fn: RunFn = subprocess.run, path: Optional[Path] = None) -> dict:
    path = path or cleanup_journal_path()
    if not path.is_file():
        raise BootRecoveryError("Journal cleanup NVRAM tidak ditemukan")
    try:
        journal = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BootRecoveryError(f"Journal cleanup rusak: {exc}") from exc
    if journal.get("phase") not in {
        "deleting", "deleted", "delete-failed", "delete-unverified",
        "restore-creating", "restore-order", "restore-partial",
    }:
        raise BootRecoveryError("Journal tidak berada pada fase yang dapat dipulihkan")
    validate_recovery_bundle(Path(journal["recovery_bundle"]))
    command = [
        "efibootmgr", "-c", "-d", journal["disk"], "-p", str(journal["partition"]),
        "-L", journal["label"], "-l", journal["efi_path"].replace("/", "\\"),
    ]
    rollback_complete = bool(journal.get("rollback_complete"))
    was_active = bool(journal.get("active", True))
    not_reproduced: List[str] = []
    if not rollback_complete:
        not_reproduced.append(
            "optional data asli (mis. argumen BCD Windows Boot Manager) tidak dapat ditulis ulang "
            "oleh 'efibootmgr -c'"
        )
    if not was_active:
        not_reproduced.append(
            "flag aktif: entry asli nonaktif, tetapi 'efibootmgr -c' selalu membuat entry aktif"
        )
    result = {
        "command": command,
        "entry": journal["entry"],
        "apply": apply,
        "rollback_complete": rollback_complete,
        "active": was_active,
        "raw_entry_line": journal.get("raw_entry_line"),
        "not_reproduced": not_reproduced,
        "warnings": [journal["rollback_warning"]] if journal.get("rollback_warning") else [],
    }
    if not apply:
        return result
    before = _read_boot(run_fn)
    original = _entry_map(before).get(journal["entry"])
    if (
        original is not None
        and original.partuuid == journal["partuuid"]
        and original.efi_path and original.efi_path.lower() == journal["efi_path"].lower()
    ):
        journal["phase"] = "not-deleted"
        journal["new_entry"] = journal["entry"]
        journal["restored_order"] = before.order
        _atomic_json(path, journal)
        result.update({"new_entry": journal["entry"], "restored_order": before.order, "no_change": True})
        return result

    excluded = set(journal.get("preexisting_equivalent_entries") or [])
    matching = [
        item for item in before.entries
        if item.number not in excluded
        and item.partuuid == journal["partuuid"]
        and item.efi_path and item.efi_path.lower() == journal["efi_path"].lower()
    ]
    if len(matching) > 1:
        journal["phase"] = "restore-partial"
        _atomic_json(path, journal)
        raise BootRecoveryError("Lebih dari satu entry hasil restore ditemukan; BootOrder tidak diubah")
    if not matching:
        journal["phase"] = "restore-creating"
        _atomic_json(path, journal)
        created = _run(command, run_fn)
        if created.returncode != 0:
            journal["phase"] = "restore-partial"
            journal["error"] = (created.stderr or created.stdout or "gagal membuat ulang entry").strip()
            _atomic_json(path, journal)
            raise BootRecoveryError(journal["error"])
        after = _read_boot(run_fn)
        before_numbers = {item.number for item in before.entries}
        matching = [
            item for item in after.entries
            if item.number not in before_numbers
            and item.partuuid == journal["partuuid"]
            and item.efi_path and item.efi_path.lower() == journal["efi_path"].lower()
        ]
        if len(matching) != 1:
            journal["phase"] = "restore-partial"
            _atomic_json(path, journal)
            raise BootRecoveryError("Entry dibuat tetapi nomor baru tidak dapat dipastikan; BootOrder tidak diubah")
    else:
        after = before
    new_number = matching[0].number
    known_after = {item.number for item in after.entries}
    restored_order = [new_number if item == journal["entry"] else item for item in journal["original_order"]]
    restored_order = [item for item in restored_order if item in known_after]
    restored_order = list(dict.fromkeys(restored_order))
    journal["phase"] = "restore-order"
    journal["new_entry"] = new_number
    journal["planned_restored_order"] = restored_order
    _atomic_json(path, journal)
    order_result = _run(["efibootmgr", "-o", ",".join(restored_order)], run_fn)
    if order_result.returncode != 0:
        journal["phase"] = "restore-partial"
        journal["new_entry"] = new_number
        _atomic_json(path, journal)
        raise BootRecoveryError("Entry dibuat ulang tetapi BootOrder gagal dipulihkan")
    journal["phase"] = "restored"
    journal["new_entry"] = new_number
    journal["restored_order"] = restored_order
    journal["restore_not_reproduced"] = not_reproduced
    _atomic_json(path, journal)
    result.update({"new_entry": new_number, "restored_order": restored_order})
    return result
