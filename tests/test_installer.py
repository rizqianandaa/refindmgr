import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class TestInstallerSixelDependency(unittest.TestCase):
    def test_installer_auto_installs_img2sixel_on_supported_distros(self):
        script = (ROOT / "install.sh").read_text()
        expected_commands = (
            "apt-get install -y libsixel-bin",
            "dnf install -y libsixel-utils",
            "yum install -y libsixel-utils",
            "pacman -S --noconfirm --needed libsixel",
            "zypper --non-interactive install libsixel",
            "apk add --no-cache libsixel-tools",
        )
        for command in expected_commands:
            with self.subTest(command=command):
                self.assertIn(command, script)

    def test_installer_verifies_img2sixel_after_package_install(self):
        script = (ROOT / "install.sh").read_text()
        self.assertGreaterEqual(script.count("command -v img2sixel"), 3)
        self.assertIn("Sixel renderer berhasil dipasang", script)
        self.assertIn("instalasi CLI tetap dilanjutkan", script)


if __name__ == "__main__":
    unittest.main()
