import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from refindmgr import firmware_compat as compat
from refindmgr.paths import detect_refind_dir


class FirmwareCompatFixture(unittest.TestCase):
    def make_layout(self, root: Path):
        esp = root / "esp"
        source = esp / "EFI" / "refind"
        target = esp / "EFI" / "ubuntu"
        windows = esp / "EFI" / "Microsoft" / "Boot"
        source.mkdir(parents=True)
        target.mkdir(parents=True)
        windows.mkdir(parents=True)
        (source / "refind_x64.efi").write_bytes(b"REFIND-BINARY")
        (source / "refind.conf").write_text("timeout 5\ninclude themes/demo/theme.conf\n")
        (source / "themes" / "demo").mkdir(parents=True)
        (source / "themes" / "demo" / "theme.conf").write_text("banner banner.png\n")
        (target / "shimx64.efi").write_bytes(b"GENUINE-SHIM")
        (target / "grubx64.efi").write_bytes(b"GENUINE-GRUB")
        (target / "refind.conf").write_text("ORIGINAL TARGET CONFIG\n")
        (windows / "bootmgfw.efi").write_bytes(b"WINDOWS")
        return esp, source, target


class TestFirmwareCompat(FirmwareCompatFixture):
    def test_grub_mode_install_detect_and_restore(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            esp, source, target = self.make_layout(root)
            original_shim = (target / "shimx64.efi").read_bytes()
            original_conf = (target / "refind.conf").read_text()

            plan = compat.plan_install(source, target, linux_mode="grub")
            status = compat.apply_install(plan, system_root=root / "system", boot_dir=root / "boot")

            self.assertTrue(status.managed)
            self.assertEqual((target / "shimx64.efi").read_bytes(), b"REFIND-BINARY")
            self.assertIn("loader /EFI/ubuntu/grubx64.efi", (target / "refind.conf").read_text())
            self.assertTrue((target / "themes" / "demo" / "theme.conf").is_file())
            self.assertEqual(compat.detect_compat_dir([esp]), target)

            with patch.dict(os.environ, {"REFINDMGR_ESP_ROOTS": str(esp)}, clear=False):
                self.assertEqual(detect_refind_dir(), target)

            preview = compat.restore(status, apply=False)
            self.assertEqual(preview["active_loader"], str(target / "shimx64.efi"))
            result = compat.restore(status, apply=True)
            self.assertTrue(Path(result["rollback_dir"]).is_dir())
            self.assertEqual((target / "shimx64.efi").read_bytes(), original_shim)
            self.assertEqual((target / "refind.conf").read_text(), original_conf)
            self.assertFalse(compat.state_path(target).exists())

    def test_refuses_unmanaged_vendor_loader_that_is_already_refind(self):
        with TemporaryDirectory() as tmp:
            _, source, target = self.make_layout(Path(tmp))
            (target / "shimx64.efi").write_bytes(b"REFIND-BINARY")
            with self.assertRaises(compat.FirmwareCompatError):
                compat.plan_install(source, target)

    def test_restore_refuses_changed_active_loader(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, source, target = self.make_layout(root)
            status = compat.apply_install(compat.plan_install(source, target))
            (target / "shimx64.efi").write_bytes(b"UNKNOWN-NEW-LOADER")
            with self.assertRaises(compat.FirmwareCompatError):
                compat.restore(status, apply=True)

    def test_source_and_target_must_differ(self):
        with TemporaryDirectory() as tmp:
            _, source, _ = self.make_layout(Path(tmp))
            with self.assertRaises(compat.FirmwareCompatError):
                compat.plan_install(source, source)

    def test_direct_mode_creates_stable_links_and_hooks(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, source, target = self.make_layout(root)
            boot = root / "boot"
            boot.mkdir()
            version = "7.0.0-28-generic"
            (boot / f"vmlinuz-{version}").write_bytes(b"KERNEL")
            (boot / f"initrd.img-{version}").write_bytes(b"INITRD")
            (boot / f"config-{version}").write_text("CONFIG_EFI_STUB=y\n")
            info = {"root_uuid": "root-uuid", "part_guid": "part-guid", "kernel_version": version}
            plan = compat.plan_install(source, target, linux_mode="direct", linux_info=info)
            status = compat.apply_install(plan, system_root=root / "system", boot_dir=boot)
            conf = (target / "refind.conf").read_text()
            self.assertIn("volume part-guid", conf)
            self.assertIn("loader /boot/refind-vmlinuz", conf)
            self.assertEqual(os.readlink(boot / "refind-vmlinuz"), f"vmlinuz-{version}")
            self.assertTrue((root / "system/usr/local/sbin/refindmgr-refresh-kernel-links").is_file())
            self.assertEqual(status.data["linux_mode"], "direct")

    def test_adopts_legacy_state_without_rewriting_loader(self):
        with TemporaryDirectory() as tmp:
            _, source, target = self.make_layout(Path(tmp))
            original = b"GENUINE-SHIM"
            (target / "shimx64.efi.bak").write_bytes(original)
            (target / "shimx64.efi").write_bytes(b"REFIND-BINARY")
            (target / "refind.conf.pre-hp-compat-20260723").write_text("OLD CONFIG\n")
            state_dir = target / ".refindmgr"
            state_dir.mkdir()
            (state_dir / "hp-compat-state.txt").write_text(
                "mode=hp-firmware-compat\n"
                "created=20260723\n"
                f"shim_backup={target / 'shimx64.efi.bak'}\n"
                "linux_mode=efi-stub-direct\n"
                "linux_root_uuid=root-uuid\n"
                "linux_volume_guid=part-guid\n"
            )
            before = (target / "shimx64.efi").read_bytes()
            preview = compat.adopt_legacy(target, source, apply=False)
            self.assertFalse(preview.managed)
            adopted = compat.adopt_legacy(target, source, apply=True)
            self.assertTrue(adopted.managed)
            self.assertEqual((target / "shimx64.efi").read_bytes(), before)
            data = json.loads(compat.state_path(target).read_text())
            self.assertTrue(data["adopted_legacy"])
            self.assertEqual(data["linux_mode"], "direct")


if __name__ == "__main__":
    unittest.main()
