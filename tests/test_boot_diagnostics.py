import json
import os
import subprocess
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from refindmgr import boot_diagnostics as diag


PARTUUID = "2ed98278-3386-45cd-b94c-31da228539c0"
OLD_PARTUUID = "e4d6505f-0222-43c9-bf8e-2ffa118859c7"


def completed(command, stdout="", stderr="", code=0):
    return subprocess.CompletedProcess(command, code, stdout, stderr)


class TestParsers(unittest.TestCase):
    def test_parse_lsblk_finds_esp_and_mountpoint(self):
        raw = json.dumps({"blockdevices": [{
            "name": "nvme0n1", "path": "/dev/nvme0n1", "type": "disk",
            "children": [{
                "name": "nvme0n1p6", "path": "/dev/nvme0n1p6", "type": "part",
                "fstype": "vfat", "parttype": diag.ESP_PARTTYPE.upper(),
                "partuuid": PARTUUID.upper(), "uuid": "B645-0531",
                "mountpoints": ["/boot/efi"], "size": "512M", "ro": False,
            }],
        }]})
        esps = diag.parse_lsblk_json(raw)
        self.assertEqual(len(esps), 1)
        self.assertEqual(esps[0].partuuid, PARTUUID)
        self.assertEqual(esps[0].mountpoint, "/boot/efi")

    def test_parse_lsblk_keeps_unmounted_fat_with_wrong_gpt_type_as_candidate(self):
        raw = json.dumps({"blockdevices": [{
            "name": "nvme0n1p1", "path": "/dev/nvme0n1p1", "type": "part",
            "fstype": "vfat", "parttype": "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7",
            "partuuid": OLD_PARTUUID, "uuid": "E4BA-75BD", "mountpoints": [None],
            "size": "260M", "ro": False,
        }]})
        esps = diag.parse_lsblk_json(raw)
        self.assertEqual([item.device for item in esps], ["/dev/nvme0n1p1"])

    def test_parse_efibootmgr_maps_current_entry(self):
        raw = (
            "BootCurrent: 0002\n"
            "BootNext: 000A\n"
            "BootOrder: 0002,0000,0001\n"
            f"Boot0002* ubuntu HD(6,GPT,{PARTUUID},0x800,0x100000)/File(\\EFI\\ubuntu\\shimx64.efi)\n"
        )
        state = diag.parse_efibootmgr(raw)
        self.assertEqual(state.current, "0002")
        self.assertEqual(state.next, "000A")
        self.assertEqual(state.order, ["0002", "0000", "0001"])
        self.assertEqual(state.entries[0].label, "ubuntu")
        self.assertEqual(state.entries[0].partuuid, PARTUUID)
        self.assertEqual(state.entries[0].efi_path, "/EFI/ubuntu/shimx64.efi")


class TestForensicReport(unittest.TestCase):
    def make_esp(self, root: Path):
        dedicated = root / "EFI" / "refind"
        active = root / "EFI" / "ubuntu"
        windows = root / "EFI" / "Microsoft" / "Boot"
        dedicated.mkdir(parents=True)
        active.mkdir(parents=True)
        windows.mkdir(parents=True)
        (dedicated / "refind_x64.efi").write_bytes(b"REFIND")
        (dedicated / "refind.conf").write_text("timeout 5\n")
        (active / "shimx64.efi").write_bytes(b"REFIND")
        (active / "grubx64.efi").write_bytes(b"GRUB")
        (active / "refind.conf").write_text("timeout 3\n")
        (windows / "bootmgfw.efi").write_bytes(b"WINDOWS")

    def run_fixture(self, mountpoint: Path, multiple=False):
        devices = [{
            "name": "nvme0n1p6", "path": "/dev/nvme0n1p6", "type": "part",
            "fstype": "vfat", "parttype": diag.ESP_PARTTYPE,
            "partuuid": PARTUUID, "uuid": "B645-0531",
            "mountpoints": [str(mountpoint)], "size": "512M", "ro": False,
        }]
        if multiple:
            devices.append({
                "name": "nvme0n1p1", "path": "/dev/nvme0n1p1", "type": "part",
                "fstype": "vfat", "parttype": diag.ESP_PARTTYPE,
                "partuuid": OLD_PARTUUID, "uuid": "E4BA-75BD",
                "mountpoints": [None], "size": "260M", "ro": False,
            })
        lsblk = json.dumps({"blockdevices": devices})
        efiboot = (
            "BootCurrent: 0002\n"
            "BootOrder: 0002,0000\n"
            f"Boot0002* ubuntu HD(6,GPT,{PARTUUID},0x800,0x100000)/File(\\EFI\\ubuntu\\shimx64.efi)\n"
            f"Boot0000* Windows Boot Manager HD(6,GPT,{PARTUUID},0x800,0x100000)/File(\\EFI\\Microsoft\\Boot\\bootmgfw.efi)\n"
        )

        def fake_run(command, **kwargs):
            if command[0] == "lsblk":
                return completed(command, lsblk)
            if command[:2] == ["efibootmgr", "-v"]:
                return completed(command, efiboot)
            raise AssertionError(command)

        return fake_run

    def test_detects_disguised_refind_and_active_config(self):
        with TemporaryDirectory() as tmp:
            esp = Path(tmp) / "esp"
            self.make_esp(esp)
            report = diag.collect_report(run_fn=self.run_fixture(esp), uefi_runtime=True, secure_boot=False)
            self.assertFalse(report.setup_safe)
            self.assertEqual(report.active_esp_device, "/dev/nvme0n1p6")
            self.assertEqual(report.active_loader_identity, "refind")
            self.assertEqual(report.active_loader, "/dev/nvme0n1p6:/EFI/ubuntu/shimx64.efi")
            self.assertEqual(report.active_refind_conf, "/dev/nvme0n1p6:/EFI/ubuntu/refind.conf")
            self.assertIn("rEFInd aktif memakai path vendor/nonkanonis tanpa bukti manifest", report.ambiguities)
            self.assertNotIn("Ditemukan 2 kandidat konfigurasi refind.conf", "\n".join(report.ambiguities))
            disguised = next(item for item in report.files if item.relative_path == "/EFI/ubuntu/shimx64.efi")
            self.assertEqual(disguised.identity, "refind")

    def test_managed_manifest_resolves_noncanonical_active_path(self):
        with TemporaryDirectory() as tmp:
            esp = Path(tmp) / "esp"
            self.make_esp(esp)
            state = esp / "EFI" / "ubuntu" / ".refindmgr"
            state.mkdir()
            (state / "firmware-compat.json").write_text(json.dumps({"mode": "firmware-compat"}))
            report = diag.collect_report(run_fn=self.run_fixture(esp), uefi_runtime=True, secure_boot=False)
            self.assertTrue(report.setup_safe)
            self.assertEqual(
                report.compat_manifests,
                ["/dev/nvme0n1p6:/EFI/ubuntu/.refindmgr/firmware-compat.json"],
            )
            self.assertNotIn("path vendor/nonkanonis", "\n".join(report.ambiguities))

    def test_multiple_esps_block_setup(self):
        with TemporaryDirectory() as tmp:
            esp = Path(tmp) / "esp"
            self.make_esp(esp)
            report = diag.collect_report(run_fn=self.run_fixture(esp, multiple=True), uefi_runtime=True, secure_boot=False)
            self.assertFalse(report.setup_safe)
            self.assertIn("Ditemukan 2 EFI System Partition", report.ambiguities)
            text = diag.format_report(report)
            self.assertIn("setup otomatis TIDAK AMAN", text)

    def test_unmounted_active_esp_blocks_setup_until_inspected(self):
        devices = [{
            "name": "nvme0n1p6", "path": "/dev/nvme0n1p6", "type": "part",
            "fstype": "vfat", "parttype": diag.ESP_PARTTYPE,
            "partuuid": PARTUUID, "uuid": "B645-0531", "mountpoints": [None],
            "size": "512M", "ro": False,
        }]
        lsblk = json.dumps({"blockdevices": devices})
        efiboot = (
            "BootCurrent: 0002\nBootOrder: 0002\n"
            f"Boot0002* ubuntu HD(6,GPT,{PARTUUID},0x800,0x100000)/File(\\EFI\\ubuntu\\shimx64.efi)\n"
        )

        def fake_run(command, **kwargs):
            return completed(command, lsblk if command[0] == "lsblk" else efiboot)

        report = diag.collect_report(run_fn=fake_run, uefi_runtime=True, secure_boot=False)
        self.assertFalse(report.setup_safe)
        self.assertIn("ESP aktif belum dapat diperiksa secara read-only", report.ambiguities)

    def test_export_contains_redacted_text_and_json_only(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            esp = root / "home" / "alice" / "esp"
            self.make_esp(esp)
            report = diag.collect_report(run_fn=self.run_fixture(esp), uefi_runtime=True, secure_boot=False)
            destination = root / "report.zip"
            with patch.object(diag.getpass, "getuser", return_value="alice"), \
                 patch.object(diag.socket, "gethostname", return_value="secret-host"):
                result = diag.export_report(report, destination)
            self.assertEqual(result, destination.resolve())
            with zipfile.ZipFile(result) as archive:
                self.assertEqual(sorted(archive.namelist()), ["diagnostic.json", "diagnostic.txt"])
                combined = archive.read("diagnostic.json") + archive.read("diagnostic.txt")
            self.assertNotIn(b"alice", combined)
            self.assertNotIn(b"secret-host", combined)
            self.assertIn(b"<redacted>", combined)

    def test_scan_skips_symlinked_loader(self):
        with TemporaryDirectory() as tmp:
            esp_root = Path(tmp)
            self.make_esp(esp_root)
            (esp_root / "EFI" / "ubuntu" / "linked.efi").symlink_to("shimx64.efi")
            esp = diag.EspInfo(device="/dev/test", partuuid=PARTUUID)
            files, _, _ = diag.scan_esp(esp_root, esp)
            self.assertNotIn("/EFI/ubuntu/linked.efi", [item.relative_path for item in files])

    def test_secure_boot_unknown_or_enabled_blocks_setup(self):
        with TemporaryDirectory() as tmp:
            esp = Path(tmp) / "esp"
            self.make_esp(esp)
            state = esp / "EFI" / "ubuntu" / ".refindmgr"
            state.mkdir()
            (state / "firmware-compat.json").write_text(json.dumps({"mode": "firmware-compat"}))
            unknown = diag.collect_report(
                run_fn=self.run_fixture(esp), uefi_runtime=True, secure_boot=None
            )
            enabled = diag.collect_report(
                run_fn=self.run_fixture(esp), uefi_runtime=True, secure_boot=True
            )
            self.assertIn("Status Secure Boot tidak dapat dipastikan", unknown.ambiguities)
            self.assertIn("Secure Boot aktif", "\n".join(enabled.ambiguities))


if __name__ == "__main__":
    unittest.main()
