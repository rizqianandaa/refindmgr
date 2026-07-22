import argparse
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from refindmgr import cli


ROOT = str(Path(__file__).resolve().parent.parent)


def run_cli(args, input_text=None):
    return subprocess.run(
        [sys.executable, "-m", "refindmgr.cli", *args],
        cwd=ROOT,
        input=input_text,
        capture_output=True,
        text=True,
    )


class TestUiRefinements(unittest.TestCase):
    def _refind_dir(self, tmp):
        refind = Path(tmp) / "esp" / "EFI" / "refind"
        refind.mkdir(parents=True)
        (refind / "refind.conf").write_text("timeout 5\n")
        return refind

    def test_menu_section_order_and_numbers(self):
        self.assertEqual(
            [name for name, _ in cli._MENU_SECTIONS],
            ["Tema", "Tampilan boot", "Backup refind.conf", "Sistem"],
        )
        items = {label: key for _, rows in cli._MENU_SECTIONS for key, label, _ in rows}
        self.assertEqual(items["Hanya tampilkan OS saja"], "8")
        self.assertEqual(items["Batalkan mode OS saja"], "9")
        self.assertEqual(items["Buat backup sekarang"], "10")
        self.assertEqual(items["Restore dari backup"], "11")

    def test_list_uses_green_dot_symbol_for_active_theme(self):
        with TemporaryDirectory() as tmp:
            refind = self._refind_dir(tmp)
            theme = refind / "themes" / "alpha"
            theme.mkdir(parents=True)
            (theme / "theme.conf").write_text("timeout 5\n")
            (refind / "refind.conf").write_text("include themes/alpha/theme.conf\n")
            result = run_cli(["--refind-dir", str(refind), "list"])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("● alpha", result.stdout)
            self.assertNotIn("* alpha", result.stdout)

    def test_interactive_activate_uses_numbered_selection(self):
        with TemporaryDirectory() as tmp:
            refind = self._refind_dir(tmp)
            for name in ("alpha", "beta"):
                theme = refind / "themes" / name
                theme.mkdir(parents=True)
                (theme / "theme.conf").write_text("timeout 5\n")
            (refind / "refind.conf").write_text("include themes/alpha/theme.conf\n")
            result = run_cli(
                ["--refind-dir", str(refind)],
                input_text="4\n2\n\n0\n",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Pilih tema yang diaktifkan", result.stdout)
            self.assertIn("1) alpha", result.stdout)
            self.assertIn("2) beta", result.stdout)
            conf = (refind / "refind.conf").read_text()
            self.assertIn("include themes/beta/theme.conf", conf)
            self.assertIn("# include themes/alpha/theme.conf", conf)

    def test_clean_menu_controls_tools_after_theme_include(self):
        with TemporaryDirectory() as tmp:
            refind = self._refind_dir(tmp)
            ubuntu = refind.parent / "ubuntu"
            ubuntu.mkdir()
            (ubuntu / "shimx64.efi").write_bytes(b"shim")
            sublime = refind / "themes" / "sublime"
            sublime.mkdir(parents=True)
            (sublime / "theme.conf").write_text(
                "showtools shell,memtest,about,shutdown,reboot\n"
            )
            (refind / "refind.conf").write_text(
                "timeout 5\ninclude themes/sublime/theme.conf\n"
            )
            result = run_cli([
                "--refind-dir", str(refind), "clean-menu", "--auto", "--apply"
            ])
            self.assertEqual(result.returncode, 0, result.stderr)
            conf = (refind / "refind.conf").read_text()
            self.assertIn("showtools shutdown,reboot", conf)
            self.assertGreater(
                conf.rfind("showtools shutdown,reboot"),
                conf.find("include themes/sublime/theme.conf"),
            )
            self.assertNotIn("Ikon penguin", result.stdout)
            self.assertNotIn("Backup:", result.stdout)
            self.assertNotIn("Reboot untuk melihat hasilnya", result.stdout)
            self.assertTrue(result.stdout.rstrip().endswith("- Ubuntu: /EFI/ubuntu/shimx64.efi"))

    def test_theme_activation_keeps_os_only_block_last(self):
        with TemporaryDirectory() as tmp:
            refind = self._refind_dir(tmp)
            for name in ("alpha", "beta"):
                theme = refind / "themes" / name
                theme.mkdir(parents=True)
                (theme / "theme.conf").write_text("showtools shell,about,reboot\n")
            (refind / "refind.conf").write_text(
                "include themes/alpha/theme.conf\n"
                "# refindmgr-clean-menu: begin\n"
                "# refindmgr-clean-menu: previous-scanfor=__DEFAULT__\n"
                "# refindmgr-clean-menu: previous-showtools=__DEFAULT__\n"
                "scanfor manual\n"
                "showtools shutdown,reboot\n"
                "# refindmgr-clean-menu: end\n"
            )
            result = run_cli([
                "--refind-dir", str(refind), "activate", "beta"
            ])
            self.assertEqual(result.returncode, 0, result.stderr)
            conf = (refind / "refind.conf").read_text()
            self.assertGreater(
                conf.rfind("showtools shutdown,reboot"),
                conf.find("include themes/beta/theme.conf"),
            )
            self.assertTrue(conf.rstrip().endswith("# refindmgr-clean-menu: end"))

    def test_os_only_repairs_affected_refind_version_before_applying(self):
        with TemporaryDirectory() as tmp:
            refind = self._refind_dir(tmp)
            ubuntu = refind.parent / "ubuntu"
            ubuntu.mkdir()
            (ubuntu / "shimx64.efi").write_bytes(b"shim")
            manager = cli.system_mod.PackageManagerInfo(
                "apt", ["apt-get", "install", "-y", "refind"]
            )
            top_args = argparse.Namespace(refind_dir=str(refind))
            with patch.object(cli, "_confirm", return_value=True), \
                 patch.object(cli, "_menu_loading"), \
                 patch.object(cli.system_mod, "detect_package_manager", return_value=manager), \
                 patch.object(
                     cli.system_mod,
                     "get_installed_refind_version",
                     side_effect=["0.14.2", "0.14.1"],
                 ), \
                 patch.object(cli, "cmd_setup") as setup_mock, \
                 patch.object(cli, "cmd_clean_menu") as clean_mock:
                cli._menu_clean_menu_auto(top_args)
            setup_args = setup_mock.call_args.args[0]
            self.assertTrue(setup_args.pin_version)
            self.assertTrue(setup_args.refresh_esp)
            self.assertTrue(setup_args.allow_direct_download)
            clean_mock.assert_called_once()

    def test_os_only_does_not_claim_success_when_version_repair_fails(self):
        with TemporaryDirectory() as tmp:
            refind = self._refind_dir(tmp)
            ubuntu = refind.parent / "ubuntu"
            ubuntu.mkdir()
            (ubuntu / "shimx64.efi").write_bytes(b"shim")
            manager = cli.system_mod.PackageManagerInfo(
                "apt", ["apt-get", "install", "-y", "refind"]
            )
            top_args = argparse.Namespace(refind_dir=str(refind))
            with patch.object(cli, "_confirm", return_value=True), \
                 patch.object(cli.system_mod, "detect_package_manager", return_value=manager), \
                 patch.object(
                     cli.system_mod,
                     "get_installed_refind_version",
                     side_effect=["0.14.2", "0.14.2"],
                 ), \
                 patch.object(cli, "cmd_setup"), \
                 patch.object(cli, "cmd_clean_menu") as clean_mock:
                cli._menu_clean_menu_auto(top_args)
            clean_mock.assert_not_called()

    def test_installer_enables_showtools_compatibility_fix(self):
        installer = (Path(__file__).resolve().parent.parent / "install.sh").read_text()
        self.assertIn(
            "setup --yes --pin-version --refresh-esp --allow-direct-download",
            installer,
        )

    def test_official_deb_fallback_url_is_not_a_placeholder(self):
        template = cli.system_mod.REFIND_DEB_URL_TEMPLATE
        self.assertTrue(template.startswith("https://"))
        self.assertNotIn("{{", template)


if __name__ == "__main__":
    unittest.main()
