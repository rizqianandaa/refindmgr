import json
import os
import subprocess
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from refindmgr import boot_diagnostics as diag
from refindmgr import boot_recovery as recovery

PART = "2ed98278-3386-45cd-b94c-31da228539c0"
OLD = "e4d6505f-0222-43c9-bf8e-2ffa118859c7"


def cp(command, stdout="", stderr="", code=0):
    return subprocess.CompletedProcess(command, code, stdout, stderr)


class FakeFirmware:
    def __init__(self):
        self.current = "0002"
        self.order = ["0002", "0000", "000A", "0003", "2001", "2002", "2004"]
        self.next = None
        self.commands = []
        self.deleted = set()
        self.created = None

    def output(self):
        next_line = f"BootNext: {self.next}\n" if self.next else ""
        lines = (
            f"BootCurrent: {self.current}\n{next_line}BootOrder: {','.join(self.order)}\n"
            f"Boot0000* Windows Boot Manager HD(6,GPT,{PART},0x1,0x2)/File(\\EFI\\Microsoft\\Boot\\bootmgfw.efi)\n"
            f"Boot0002* ubuntu HD(6,GPT,{PART},0x1,0x2)/File(\\EFI\\ubuntu\\shimx64.efi)\n"
            f"Boot000A* rEFInd Boot Manager HD(6,GPT,{PART},0x1,0x2)/File(\\EFI\\refind\\refind_x64.efi)\n"
            "Boot2001* EFI USB Device\n"
        )
        if "0003" not in self.deleted:
            lines += f"Boot0003* ubuntu HD(6,GPT,{PART},0x1,0x2)/File(\\EFI\\ubuntu\\shimx64.efi)\n"
        if self.created:
            number, label, efi_path = self.created
            lines += f"Boot{number}* {label} HD(6,GPT,{PART},0x1,0x2)/File({efi_path})\n"
        return lines

    def run(self, command, **kwargs):
        self.commands.append(command)
        if command[:2] == ["efibootmgr", "-v"]:
            return cp(command, self.output())
        if command[:2] == ["efibootmgr", "-n"]:
            self.next = command[2]
            return cp(command)
        if command[:2] == ["efibootmgr", "-o"]:
            self.order = command[2].split(",")
            return cp(command)
        if command[:2] == ["efibootmgr", "-N"]:
            self.next = None
            return cp(command)
        if len(command) == 4 and command[:2] == ["efibootmgr", "-b"] and command[3] == "-B":
            self.deleted.add(command[2])
            return cp(command)
        if command[:2] == ["efibootmgr", "-c"]:
            label = command[command.index("-L") + 1]
            efi_path = command[command.index("-l") + 1]
            self.created = ("000B", label, efi_path)
            return cp(command)
        raise AssertionError(command)


class TestBootStateMachine(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "boot-test.json"
        self.verify = self.root / "verified.json"
        self.fw = FakeFirmware()
        self.env = patch.dict(os.environ, {"REFINDMGR_STATE_DIR": str(self.root)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_preview_does_not_write_nvram_or_state(self):
        result = recovery.start_boot_test("000A", apply=False, run_fn=self.fw.run, path=self.state)
        self.assertEqual(result["phase"], "preview")
        self.assertFalse(self.state.exists())
        self.assertFalse(any(command[1:2] == ["-n"] for command in self.fw.commands))

    def test_bootnext_then_bootorder_across_reboots(self):
        start = recovery.start_boot_test("000A", apply=True, run_fn=self.fw.run, path=self.state)
        self.assertEqual(start["phase"], "bootnext_pending")
        self.assertEqual(self.fw.next, "000A")

        self.fw.current = "000A"
        self.fw.next = None
        observed = recovery.observe_boot_test(run_fn=self.fw.run, path=self.state)
        self.assertEqual(observed["phase"], "bootnext_passed")

        bundle = self.root / "recovery.zip"
        self.make_minimal_bundle(bundle)
        preview = recovery.promote_boot_order(apply=False, run_fn=self.fw.run, path=self.state)
        self.assertEqual(preview["proposed_order"][0], "000A")
        promoted = recovery.promote_boot_order(apply=True, recovery_bundle=bundle, run_fn=self.fw.run, path=self.state)
        self.assertEqual(promoted["phase"], "bootorder_pending")
        self.assertEqual(self.fw.order[0], "000A")

        self.fw.current = "000A"
        completed = recovery.observe_boot_test(run_fn=self.fw.run, path=self.state)
        self.assertEqual(completed["phase"], "completed")
        self.assertIn("mode standar", completed["recommendation"])

    def test_auto_observe_only_runs_after_boot_id_changes(self):
        with patch.object(recovery, "_current_boot_id", return_value="boot-a"):
            recovery.start_boot_test("000A", apply=True, run_fn=self.fw.run, path=self.state)
            self.assertIsNone(recovery.auto_observe_boot_test(run_fn=self.fw.run, path=self.state))
        self.fw.current = "000A"
        self.fw.next = None
        with patch.object(recovery, "_current_boot_id", return_value="boot-b"):
            observed = recovery.auto_observe_boot_test(run_fn=self.fw.run, path=self.state)
        self.assertEqual(observed["phase"], "bootnext_passed")

    def test_firmware_reset_recommends_compatibility(self):
        recovery.start_boot_test("000A", apply=True, run_fn=self.fw.run, path=self.state)
        self.fw.current = "000A"
        recovery.observe_boot_test(run_fn=self.fw.run, path=self.state)
        bundle = self.root / "recovery.zip"
        self.make_minimal_bundle(bundle)
        recovery.promote_boot_order(apply=True, recovery_bundle=bundle, run_fn=self.fw.run, path=self.state)
        self.fw.current = "0002"
        self.fw.order = ["0002", "0000", "000A"]
        result = recovery.observe_boot_test(run_fn=self.fw.run, path=self.state)
        self.assertEqual(result["phase"], "bootorder_failed")
        self.assertEqual(result["firmware_behavior"], "rebuilt")
        self.assertIn("mode kompatibilitas", result["recommendation"])

    def test_firmware_ignored_order_is_distinct_from_rebuilt(self):
        recovery.start_boot_test("000A", apply=True, run_fn=self.fw.run, path=self.state)
        self.fw.current = "000A"
        recovery.observe_boot_test(run_fn=self.fw.run, path=self.state)
        bundle = self.root / "recovery.zip"
        self.make_minimal_bundle(bundle)
        recovery.promote_boot_order(apply=True, recovery_bundle=bundle, run_fn=self.fw.run, path=self.state)
        original = recovery.load_boot_test(self.state)["writable_original_order"]
        self.fw.current = "0002"
        self.fw.order = original
        result = recovery.observe_boot_test(run_fn=self.fw.run, path=self.state)
        self.assertEqual(result["firmware_behavior"], "ignored")

    def test_restore_clears_pending_bootnext_and_restores_order(self):
        original = list(self.fw.order)
        recovery.start_boot_test("000A", apply=True, run_fn=self.fw.run, path=self.state)
        recovery.restore_boot_order(apply=True, run_fn=self.fw.run, path=self.state)
        self.assertIsNone(self.fw.next)
        self.assertEqual(self.fw.order, original)
        self.assertEqual(recovery.load_boot_test(self.state)["phase"], "restored")

    def test_restore_keeps_firmware_placeholders_without_boot_variables(self):
        # 2002/2004 are firmware-reserved CD/USB/network slots that have no
        # matching Boot#### variable, so writable_boot_order drops them by
        # design. Restoring from that filtered list removed them from BootOrder
        # permanently, silently deleting the user's USB/network boot options.
        self.assertIn("2002", self.fw.order)
        self.assertIn("2004", self.fw.order)
        recovery.start_boot_test("000A", apply=True, run_fn=self.fw.run, path=self.state)
        state = recovery.load_boot_test(self.state)
        self.assertNotIn("2002", state["writable_original_order"])
        self.assertIn("2002", state["original_order"])
        recovery.restore_boot_order(apply=True, run_fn=self.fw.run, path=self.state)
        self.assertIn("2002", self.fw.order)
        self.assertIn("2004", self.fw.order)

    def make_minimal_bundle(self, path):
        data = b"diagnostic"
        manifest = {"version": 1, "files": {"diagnostic.txt": {"size": len(data), "sha256": recovery._sha256_bytes(data)}}}
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("diagnostic.txt", data)
            archive.writestr("manifest.json", json.dumps(manifest))


class TestRecoveryAndCleanup(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fw = FakeFirmware()
        self.env = patch.dict(os.environ, {"REFINDMGR_STATE_DIR": str(self.root / "state")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def make_report(self):
        boot = diag.parse_efibootmgr(self.fw.output())
        esp = diag.EspInfo(device="/dev/nvme0n1p6", partuuid=PART, inspected=True, mountpoint="/boot/efi")
        return diag.DiagnosticReport(
            generated_at="2026-07-25T00:00:00Z", uefi_runtime=True, secure_boot=False,
            esps=[esp], boot=boot, files=[], refind_configs=[], compat_manifests=[],
            active_entry=next(entry for entry in boot.entries if entry.number == "0002"),
            active_esp_device=esp.device, active_loader="/dev/nvme0n1p6:/EFI/ubuntu/shimx64.efi",
            active_loader_identity="refind", active_refind_conf=None,
            ambiguities=[], warnings=[], commands={},
        )

    def test_cleanup_only_marks_non_primary_duplicate(self):
        report = self.make_report()
        candidates = recovery.analyze_nvram_cleanup(report.boot, report.esps)
        self.assertEqual([item["entry"] for item in candidates], ["0003"])
        self.assertNotIn("0002", [item["entry"] for item in candidates])

    def test_cleanup_classifies_all_entries_and_protects_fallback(self):
        text = self.fw.output() + f"Boot0004* Fallback HD(6,GPT,{PART},0x1,0x2)/File(\\EFI\\BOOT\\BOOTX64.EFI)\n"
        boot = diag.parse_efibootmgr(text)
        esp = diag.EspInfo(device="/dev/nvme0n1p6", partuuid=PART, inspected=True, mountpoint="/boot/efi")
        classified = {item["entry"]: item for item in recovery.classify_nvram_entries(boot, [esp])}
        self.assertEqual(classified["0002"]["classification"], "kept")
        self.assertEqual(classified["0003"]["classification"], "duplicate")
        self.assertEqual(classified["0004"]["classification"], "fallback")
        self.assertFalse(classified["0004"]["deletable"])
        self.assertEqual(classified["2001"]["classification"], "firmware")

    def test_writable_order_filters_firmware_placeholders_without_boot_variables(self):
        report = self.make_report()
        self.assertEqual(
            recovery.writable_boot_order(report.boot, preferred="000A"),
            ["000A", "0002", "0000", "0003", "2001"],
        )

    def test_recovery_bundle_hash_validation_and_tamper_detection(self):
        bundle = self.root / "recovery.zip"
        created = recovery.create_recovery_bundle(self.make_report(), bundle, run_fn=self.fw.run)
        manifest = recovery.validate_recovery_bundle(created)
        self.assertIn("efibootmgr.txt", manifest["files"])
        with zipfile.ZipFile(created, "a") as archive:
            archive.writestr("diagnostic.txt", b"tampered")
        with self.assertRaises(recovery.BootRecoveryError):
            recovery.validate_recovery_bundle(created)

    def test_delete_requires_exact_confirmation_recovery_and_os_verification(self):
        bundle = self.root / "recovery.zip"
        recovery.create_recovery_bundle(self.make_report(), bundle, run_fn=self.fw.run)
        report = self.make_report()
        with self.assertRaises(recovery.BootRecoveryError):
            recovery.delete_nvram_entry("0003", "WRONG", bundle, report=report, run_fn=self.fw.run)
        with self.assertRaises(recovery.BootRecoveryError):
            recovery.delete_nvram_entry("0003", "0003", bundle, report=report, run_fn=self.fw.run)

        recovery.record_verified_entry("0002", "ubuntu", method="test")
        recovery.record_verified_entry("0000", "Windows Boot Manager", method="test")
        preview = recovery.delete_nvram_entry("0003", "0003", bundle, report=report, run_fn=self.fw.run)
        self.assertFalse(preview["apply"])
        self.assertEqual(preview["rollback"]["disk"], "/dev/nvme0n1")
        self.assertEqual(preview["rollback"]["partition"], 6)
        applied = recovery.delete_nvram_entry("0003", "0003", bundle, apply=True, report=report, run_fn=self.fw.run)
        self.assertEqual(applied["deleted"], "0003")
        self.assertIn(["efibootmgr", "-b", "0003", "-B"], self.fw.commands)
        preview_restore = recovery.restore_deleted_nvram_entry(apply=False, run_fn=self.fw.run)
        self.assertIn("-c", preview_restore["command"])
        restored = recovery.restore_deleted_nvram_entry(apply=True, run_fn=self.fw.run)
        self.assertEqual(restored["new_entry"], "000B")
        self.assertIn("000B", restored["restored_order"])

    def test_manual_verification_checks_real_entry_and_exact_phrase(self):
        with self.assertRaises(recovery.BootRecoveryError):
            recovery.confirm_manual_boot("0000", "Windows", "yes", run_fn=self.fw.run)
        data = recovery.confirm_manual_boot("0000", "Windows", "BOOT-BERHASIL", run_fn=self.fw.run)
        self.assertEqual(data["entries"]["0000"]["label"], "Windows Boot Manager")

    def _prepare_deletion(self, firmware=None):
        firmware = firmware or self.fw
        bundle = self.root / "recovery.zip"
        recovery.create_recovery_bundle(self.make_report(), bundle, run_fn=firmware.run)
        recovery.record_verified_entry("0002", "ubuntu", method="test")
        recovery.record_verified_entry("0000", "Windows Boot Manager", method="test")
        return bundle

    def test_delete_write_ahead_journal_survives_interruption(self):
        bundle = self._prepare_deletion()
        original_run = self.fw.run

        def crash_after_delete(command, **kwargs):
            result = original_run(command, **kwargs)
            if len(command) == 4 and command[:2] == ["efibootmgr", "-b"]:
                raise KeyboardInterrupt("simulated power loss boundary")
            return result

        with self.assertRaises(KeyboardInterrupt):
            recovery.delete_nvram_entry(
                "0003", "0003", bundle, apply=True,
                report=self.make_report(), run_fn=crash_after_delete,
            )
        journal_path = recovery.cleanup_journal_path()
        journal = json.loads(journal_path.read_text())
        self.assertEqual(journal["phase"], "deleting")
        restored = recovery.restore_deleted_nvram_entry(apply=True, run_fn=self.fw.run)
        self.assertEqual(restored["new_entry"], "000B")
        self.assertEqual(json.loads(journal_path.read_text())["phase"], "restored")

    def test_restore_reconciles_interrupted_entry_creation_without_duplicate(self):
        bundle = self._prepare_deletion()
        recovery.delete_nvram_entry(
            "0003", "0003", bundle, apply=True,
            report=self.make_report(), run_fn=self.fw.run,
        )
        original_run = self.fw.run

        def crash_after_create(command, **kwargs):
            result = original_run(command, **kwargs)
            if command[:2] == ["efibootmgr", "-c"]:
                raise KeyboardInterrupt("simulated interruption after create")
            return result

        with self.assertRaises(KeyboardInterrupt):
            recovery.restore_deleted_nvram_entry(apply=True, run_fn=crash_after_create)
        self.assertEqual(json.loads(recovery.cleanup_journal_path().read_text())["phase"], "restore-creating")
        restored = recovery.restore_deleted_nvram_entry(apply=True, run_fn=self.fw.run)
        self.assertEqual(restored["new_entry"], "000B")
        creates = [command for command in self.fw.commands if command[:2] == ["efibootmgr", "-c"]]
        self.assertEqual(len(creates), 1)


if __name__ == "__main__":
    unittest.main()
