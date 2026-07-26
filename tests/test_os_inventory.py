import subprocess
import sys
import unittest
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from refindmgr import os_inventory as inventory


ROOT = str(Path(__file__).resolve().parent.parent)


class TestOsReleaseAndArchitecture(unittest.TestCase):
    def test_parse_os_release(self):
        runtime = inventory.parse_os_release(
            'ID=linuxmint\nID_LIKE="ubuntu debian"\nPRETTY_NAME="Linux Mint 22"\nVERSION_ID=22\n'
        )
        self.assertEqual(runtime.distro_id, "linuxmint")
        self.assertEqual(runtime.id_like, ("ubuntu", "debian"))
        self.assertEqual(runtime.pretty_name, "Linux Mint 22")

    def test_architecture_aliases_and_32bit_firmware(self):
        self.assertEqual(inventory.normalize_architecture("x86_64", 64), "x86_64")
        self.assertEqual(inventory.normalize_architecture("aarch64", 64), "arm64")
        self.assertEqual(inventory.normalize_architecture("x86_64", 32), "ia32")
        self.assertEqual(inventory.normalize_architecture("mips", 64), "unknown")

    def test_reads_pe_coff_architecture(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "loader.efi"
            raw = bytearray(0x88)
            raw[:2] = b"MZ"
            raw[0x3C:0x40] = (0x80).to_bytes(4, "little")
            raw[0x80:0x84] = b"PE\x00\x00"
            raw[0x84:0x86] = (0xAA64).to_bytes(2, "little")
            path.write_bytes(raw)
            self.assertEqual(inventory.pe_architecture(path), "arm64")

    def test_id_like_does_not_impersonate_parent_distro(self):
        runtime = inventory.RuntimeOs(
            distro_id="amzn", id_like=("fedora",), pretty_name="Amazon Linux"
        )
        fedora = next(item for item in inventory.PROFILES if item.key == "fedora")
        self.assertFalse(inventory._runtime_matches(fedora, runtime))


class TestProfileInventory(unittest.TestCase):
    def _refind(self, tmp):
        refind = Path(tmp) / "esp" / "EFI" / "refind"
        refind.mkdir(parents=True)
        (refind / "refind.conf").write_text("timeout 5\n")
        return refind

    def test_detects_multiple_supported_distros_and_windows(self):
        with TemporaryDirectory() as tmp:
            refind = self._refind(tmp)
            paths = [
                "EFI/Microsoft/Boot/bootmgfw.efi",
                "EFI/debian/shimx64.efi",
                "EFI/fedora/grubx64.efi",
                "EFI/opensuse/shimx64.efi",
                "EFI/arch/systemd-bootx64.efi",
                "EFI/BOOT/BOOTX64.EFI",
                "EFI/fedora/mmx64.efi",
            ]
            report = inventory.build_inventory(
                refind, loader_paths=paths, runtime=inventory.RuntimeOs(),
                machine="x86_64", firmware_bits=64,
            )
            entries = report.menu_entries()
            self.assertEqual([name for name, _ in entries], [
                "Windows", "Debian", "Fedora", "openSUSE", "Arch Linux",
            ])
            self.assertNotIn("EFI/BOOT/BOOTX64.EFI", [path for _, path in entries])
            self.assertNotIn("EFI/fedora/mmx64.efi", [path for _, path in entries])

    def test_runtime_os_upgrades_matching_profile_to_verified(self):
        with TemporaryDirectory() as tmp:
            refind = self._refind(tmp)
            runtime = inventory.RuntimeOs(
                distro_id="ubuntu", id_like=("debian",),
                pretty_name="Ubuntu 24.04.2 LTS", version_id="24.04",
            )
            report = inventory.build_inventory(
                refind, loader_paths=["EFI/ubuntu/shimx64.efi"], runtime=runtime,
                machine="x86_64", firmware_bits=64,
            )
            loader = report.loaders[0]
            self.assertEqual(loader.label, "Ubuntu 24.04.2 LTS")
            self.assertEqual(loader.confidence, "verified")
            self.assertTrue(loader.current_os)

    def test_foreign_architecture_uki_is_reported_but_not_selected(self):
        with TemporaryDirectory() as tmp:
            refind = self._refind(tmp)
            report = inventory.build_inventory(
                refind, loader_paths=["EFI/Linux/fedora-aa64.efi"],
                runtime=inventory.RuntimeOs(), machine="x86_64", firmware_bits=64,
            )
            self.assertEqual(len(report.loaders), 1)
            self.assertFalse(report.loaders[0].healthy)
            self.assertEqual(report.menu_entries(), [])
            self.assertIn("berbeda", report.loaders[0].issues[0])

    def test_systemd_boot_uses_loader_entry_evidence(self):
        with TemporaryDirectory() as tmp:
            refind = self._refind(tmp)
            entries = refind.parents[1] / "loader" / "entries"
            entries.mkdir(parents=True)
            (entries / "arch.conf").write_text("title Arch Linux\nlinux /vmlinuz-linux\n")
            report = inventory.build_inventory(
                refind, loader_paths=["EFI/systemd/systemd-bootx64.efi"],
                runtime=inventory.RuntimeOs(distro_id="arch", pretty_name="Arch Linux"),
                machine="x86_64", firmware_bits=64,
            )
            loader = report.loaders[0]
            self.assertEqual(loader.kind, "systemd-boot")
            self.assertEqual(loader.label, "Arch Linux (systemd-boot)")
            self.assertEqual(loader.confidence, "high")
            self.assertTrue(any("Arch Linux" in item for item in loader.evidence))

    def test_no_known_loader_returns_warning(self):
        with TemporaryDirectory() as tmp:
            refind = self._refind(tmp)
            report = inventory.build_inventory(
                refind, loader_paths=["EFI/vendor/customx64.efi"],
                runtime=inventory.RuntimeOs(), machine="x86_64", firmware_bits=64,
            )
            self.assertEqual(report.menu_entries(), [])
            self.assertTrue(report.warnings)

    def test_compatibility_refind_disguised_as_shim_falls_back_to_real_grub(self):
        with TemporaryDirectory() as tmp:
            esp = Path(tmp) / "esp"
            active = esp / "EFI" / "ubuntu"
            active.mkdir(parents=True)
            (active / "refind.conf").write_text("timeout 5\n")
            (active / "shimx64.efi").write_bytes(b"refind-binary")
            (active / "grubx64.efi").write_bytes(b"real-grub")
            state = active / ".refindmgr"
            state.mkdir()
            state.joinpath("firmware-compat.json").write_text(json.dumps({
                "refind_sha256": hashlib.sha256(b"refind-binary").hexdigest(),
            }))
            report = inventory.build_inventory(
                active,
                loader_paths=["EFI/ubuntu/shimx64.efi", "EFI/ubuntu/grubx64.efi"],
                runtime=inventory.RuntimeOs(distro_id="ubuntu", pretty_name="Ubuntu"),
                machine="x86_64", firmware_bits=64,
            )
            self.assertEqual(report.menu_entries(), [("Ubuntu", "EFI/ubuntu/grubx64.efi")])
            self.assertTrue(any("byte-identik dengan rEFInd" in item for item in report.warnings))

    def test_default_scan_includes_real_grub_beside_compatibility_refind(self):
        with TemporaryDirectory() as tmp:
            esp = Path(tmp) / "esp"
            active = esp / "EFI" / "ubuntu"
            active.mkdir(parents=True)
            (active / "refind.conf").write_text("timeout 5\n")
            (active / "shimx64.efi").write_bytes(b"refind-binary")
            (active / "grubx64.efi").write_bytes(b"real-grub")
            state = active / ".refindmgr"
            state.mkdir()
            state.joinpath("firmware-compat.json").write_text(json.dumps({
                "refind_sha256": hashlib.sha256(b"refind-binary").hexdigest(),
            }))
            report = inventory.build_inventory(
                active,
                runtime=inventory.RuntimeOs(distro_id="ubuntu", pretty_name="Ubuntu"),
                machine="x86_64", firmware_bits=64,
            )
            self.assertEqual(report.menu_entries(), [("Ubuntu", "EFI/ubuntu/grubx64.efi")])


class TestOsCommand(unittest.TestCase):
    def test_os_doctor_is_read_only_and_lists_profiles(self):
        with TemporaryDirectory() as tmp:
            esp = Path(tmp) / "esp"
            refind = esp / "EFI" / "refind"
            fedora = esp / "EFI" / "fedora"
            windows = esp / "EFI" / "Microsoft" / "Boot"
            refind.mkdir(parents=True)
            fedora.mkdir(parents=True)
            windows.mkdir(parents=True)
            (refind / "refind.conf").write_text("timeout 5\n")
            (fedora / "shimx64.efi").write_bytes(b"shim")
            (windows / "bootmgfw.efi").write_bytes(b"windows")
            result = subprocess.run(
                [sys.executable, "-m", "refindmgr.cli", "--refind-dir", str(refind), "os", "doctor"],
                cwd=ROOT, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Windows", result.stdout)
            self.assertIn("Fedora", result.stdout)
            self.assertIn("Health check", result.stdout)
            self.assertIn("read-only", result.stdout)
            self.assertEqual((refind / "refind.conf").read_text(), "timeout 5\n")

    def test_dynamic_clean_menu_writes_multiple_profiled_os_loaders(self):
        with TemporaryDirectory() as tmp:
            esp = Path(tmp) / "esp"
            refind = esp / "EFI" / "refind"
            for folder in (refind, esp / "EFI" / "debian", esp / "EFI" / "fedora", esp / "EFI" / "Microsoft" / "Boot"):
                folder.mkdir(parents=True)
            (refind / "refind.conf").write_text("timeout 5\n")
            (esp / "EFI" / "debian" / "shimx64.efi").write_bytes(b"debian")
            (esp / "EFI" / "fedora" / "grubx64.efi").write_bytes(b"fedora")
            (esp / "EFI" / "Microsoft" / "Boot" / "bootmgfw.efi").write_bytes(b"windows")
            result = subprocess.run(
                [sys.executable, "-m", "refindmgr.cli", "--refind-dir", str(refind), "clean-menu", "--auto", "--apply"],
                cwd=ROOT, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            written = (refind / "refind.conf").read_text()
            self.assertIn('menuentry "Windows" {', written)
            self.assertIn('menuentry "Debian" {', written)
            self.assertIn('menuentry "Fedora" {', written)
            self.assertIn("loader /EFI/debian/shimx64.efi", written)
            self.assertIn("loader /EFI/fedora/grubx64.efi", written)


class TestLoaderBaseline(unittest.TestCase):
    def test_baseline_detects_compatibility_loader_restored(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            esp = root / "esp"
            active = esp / "EFI" / "ubuntu"
            windows = esp / "EFI" / "Microsoft" / "Boot"
            active.mkdir(parents=True)
            windows.mkdir(parents=True)
            refind_bytes, shim_bytes = b"REFIND", b"GENUINE-SHIM"
            (active / "shimx64.efi").write_bytes(refind_bytes)
            (active / "refind_x64.efi").write_bytes(refind_bytes)
            (active / "grubx64.efi").write_bytes(b"GRUB")
            (active / "refind.conf").write_text("timeout 5\n")
            (windows / "bootmgfw.efi").write_bytes(b"WINDOWS")
            state = active / ".refindmgr"
            state.mkdir()
            manifest = {
                "schema": 1, "mode": "firmware-compat", "linux_mode": "grub",
                "active_loader": str(active / "shimx64.efi"),
                "source_binary": str(active / "refind_x64.efi"),
                "grub_loader": str(active / "grubx64.efi"),
                "windows_loader": str(windows / "bootmgfw.efi"),
                "refind_sha256": hashlib.sha256(refind_bytes).hexdigest(),
                "original_loader_sha256": hashlib.sha256(shim_bytes).hexdigest(),
            }
            (state / "firmware-compat.json").write_text(json.dumps(manifest))
            runtime = inventory.RuntimeOs(distro_id="ubuntu", pretty_name="Ubuntu")
            report = inventory.build_inventory(active, runtime=runtime, machine="x86_64", firmware_bits=64)
            before = inventory.create_baseline(active, report, boot_dir=root / "boot")
            saved = inventory.save_baseline(before, root / "baseline.json")
            self.assertEqual(inventory.load_baseline(saved)["compatibility"]["state"], "healthy")
            (active / "shimx64.efi").write_bytes(shim_bytes)
            after_report = inventory.build_inventory(active, runtime=runtime, machine="x86_64", firmware_bits=64)
            after = inventory.create_baseline(active, after_report, boot_dir=root / "boot")
            _, changes = inventory.compare_baseline(before, after)
            self.assertEqual(after["compatibility"]["state"], "original-loader-restored")
            self.assertTrue(any("mengembalikan loader vendor asli" in item for item in changes))


if __name__ == "__main__":
    unittest.main()
