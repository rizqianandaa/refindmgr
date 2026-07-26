import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class TestInstallerSixelDependency(unittest.TestCase):
    def test_installer_supports_cli_only_and_compat_guard(self):
        script = (ROOT / "install.sh").read_text()
        self.assertIn("--cli-only", script)
        self.assertIn("firmware-compat.json", script)
        self.assertIn("hp-compat-state.txt", script)
        self.assertIn("setup/refind-install otomatis dilewati", script)

    def test_installer_runs_read_only_preflight_before_setup(self):
        script = (ROOT / "install.sh").read_text()
        preflight = script.index("refindmgr preflight --setup")
        setup = script.index("refindmgr setup --yes")
        self.assertLess(preflight, setup)
        self.assertIn("ESP dan NVRAM tidak diubah", script)
        self.assertIn("doctor --forensic --scan-unmounted --export", script)

    def test_installer_auto_installs_chafa_on_supported_distros(self):
        # chafa replaces img2sixel: one binary renders Kitty, iTerm2, Sixel and
        # the truecolor character fallback, so a single package covers every
        # backend that needs an external renderer at all.
        script = (ROOT / "install.sh").read_text()
        expected_commands = (
            "apt-get install -y chafa libsixel-bin",
            "dnf install -y chafa libsixel-utils",
            "yum install -y chafa libsixel-utils",
            "pacman -S --noconfirm --needed chafa libsixel",
            "zypper --non-interactive install chafa libsixel",
            "apk add --no-cache chafa libsixel-tools",
        )
        for command in expected_commands:
            with self.subTest(command=command):
                self.assertIn(command, script)

    def test_installer_verifies_the_renderer_after_package_install(self):
        script = (ROOT / "install.sh").read_text()
        self.assertGreaterEqual(script.count("command -v chafa"), 3)
        self.assertIn("Renderer preview berhasil dipasang", script)
        self.assertIn("instalasi CLI tetap dilanjutkan", script)

    def test_installer_also_provides_img2sixel_for_exact_pixel_sizing(self):
        # chafa cannot size a Sixel image in pixels when its stdout is a pipe,
        # so it assumes a square 8x8 cell and the thumbnail comes out squashed.
        script = (ROOT / "install.sh").read_text()
        self.assertGreaterEqual(script.count("command -v img2sixel"), 2)
        self.assertIn("libsixel", script)

    def test_installer_keeps_the_cli_readable_by_non_root_users(self):
        # mktemp -d creates 0700 and mv preserves it, so without an explicit
        # chmod every non-root 'refindmgr catalog' failed with Permission denied.
        script = (ROOT / "install.sh").read_text()
        self.assertIn('chmod 0755 "$STAGING"', script)

    def test_missing_renderer_mentions_the_zero_dependency_backends(self):
        script = (ROOT / "install.sh").read_text()
        self.assertIn("Kitty/iTerm2", script)


if __name__ == "__main__":
    unittest.main()
