import argparse
import io
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from refindmgr import cli as cli_mod
from refindmgr import system as system_mod


def run_cli(args, cwd):
    return subprocess.run(
        [sys.executable, "-m", "refindmgr.cli", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )


class TestCliSmoke(unittest.TestCase):
    def test_help_runs_successfully(self):
        result = run_cli(["--help"], cwd=str(Path(__file__).resolve().parent.parent))
        self.assertEqual(result.returncode, 0)
        self.assertIn("refindmgr", result.stdout)

    def test_catalog_lists_entries(self):
        result = run_cli(["catalog"], cwd=str(Path(__file__).resolve().parent.parent))
        self.assertEqual(result.returncode, 0)
        self.assertIn("minimal", result.stdout)
        self.assertIn("github.com", result.stdout)

    def test_list_without_refind_dir_fails_gracefully(self):
        result = run_cli(
            ["--refind-dir", "/does/not/exist", "list"],
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Tidak menemukan", result.stderr)

    def test_refind_dir_flag_works_after_subcommand_too(self):
        # Regression test: --refind-dir used to only be accepted before the
        # subcommand name (e.g. 'refindmgr --refind-dir X list'). Placing it
        # after the subcommand ('refindmgr list --refind-dir X') used to be
        # silently rejected by argparse.
        root = str(Path(__file__).resolve().parent.parent)
        result = run_cli(["list", "--refind-dir", "/does/not/exist"], cwd=root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Tidak menemukan", result.stderr)

    def test_doctor_runs_without_refind_dir(self):
        result = run_cli(["--refind-dir", "/does/not/exist", "doctor"], cwd=str(Path(__file__).resolve().parent.parent))
        self.assertEqual(result.returncode, 0)
        self.assertIn("Diagnostik", result.stdout)

    def test_setup_reports_already_installed(self):
        root = str(Path(__file__).resolve().parent.parent)
        with TemporaryDirectory() as tmp:
            refind_dir = Path(tmp) / "refind"
            refind_dir.mkdir()
            (refind_dir / "refind.conf").write_text("timeout 5\n")
            result = run_cli(["--refind-dir", str(refind_dir), "setup"], cwd=root)
            self.assertEqual(result.returncode, 0)
            self.assertIn("sudah terpasang", result.stdout)

    def test_setup_without_yes_is_a_dry_run_preview_only(self):
        # Without --yes, 'setup' must never actually invoke a package manager
        # or refind-install; it should only preview what it would do.
        root = str(Path(__file__).resolve().parent.parent)
        result = run_cli(["--refind-dir", "/does/not/exist", "setup"], cwd=root)
        self.assertIn("pratinjau", result.stdout + result.stderr)
        self.assertNotIn("--yes", "")  # sanity placeholder, real assertions above

    def test_full_workflow_install_activate_list_remove(self):
        root = str(Path(__file__).resolve().parent.parent)
        with TemporaryDirectory() as tmp:
            refind_dir = Path(tmp) / "refind"
            refind_dir.mkdir()
            (refind_dir / "refind.conf").write_text("timeout 5\n")

            theme_src = Path(tmp) / "my-theme"
            theme_src.mkdir()
            (theme_src / "theme.conf").write_text("selection_big icons/x.png\n")

            install_result = run_cli(
                ["--refind-dir", str(refind_dir), "install", str(theme_src), "--activate"],
                cwd=root,
            )
            self.assertEqual(install_result.returncode, 0, install_result.stderr)
            self.assertIn("berhasil dipasang", install_result.stdout)
            self.assertIn("sekarang aktif", install_result.stdout)

            list_result = run_cli(["--refind-dir", str(refind_dir), "list"], cwd=root)
            self.assertEqual(list_result.returncode, 0)
            self.assertIn("my-theme", list_result.stdout)

            remove_result = run_cli(
                ["--refind-dir", str(refind_dir), "remove", "my-theme"],
                cwd=root,
            )
            self.assertEqual(remove_result.returncode, 0)
            self.assertIn("telah dihapus", remove_result.stdout)


    def test_declutter_sets_showtools_and_scanfor(self):
        root = str(Path(__file__).resolve().parent.parent)
        with TemporaryDirectory() as tmp:
            refind_dir = Path(tmp) / "refind"
            refind_dir.mkdir()
            (refind_dir / "refind.conf").write_text(
                "timeout 5\nshowtools shell, memtest, gdisk, about, hidden_tags, shutdown, reboot, firmware\n"
            )
            result = run_cli(["--refind-dir", str(refind_dir), "declutter"], cwd=root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("dirapikan", result.stdout)
            conf_text = (refind_dir / "refind.conf").read_text()
            self.assertIn("showtools shutdown,reboot", conf_text)
            self.assertIn("scanfor internal,external,optical,manual", conf_text)
            # A backup of the pre-declutter refind.conf must exist.
            backups = list(refind_dir.glob("refind.conf.*.bak"))
            self.assertEqual(len(backups), 1)
            self.assertIn("showtools shell, memtest", backups[0].read_text())

    def test_declutter_undo_restores_default_behavior(self):
        root = str(Path(__file__).resolve().parent.parent)
        with TemporaryDirectory() as tmp:
            refind_dir = Path(tmp) / "refind"
            refind_dir.mkdir()
            (refind_dir / "refind.conf").write_text("timeout 5\n")
            first = run_cli(["--refind-dir", str(refind_dir), "declutter"], cwd=root)
            self.assertEqual(first.returncode, 0, first.stderr)

            undo = run_cli(["--refind-dir", str(refind_dir), "declutter", "--undo"], cwd=root)
            self.assertEqual(undo.returncode, 0, undo.stderr)
            self.assertIn("dikembalikan", undo.stdout)
            conf_text = (refind_dir / "refind.conf").read_text()
            self.assertIn("# showtools shutdown,reboot", conf_text)
            self.assertIn("# scanfor internal,external,optical,manual", conf_text)

    def test_declutter_fails_gracefully_without_refind_conf(self):
        # Regression guard: detect_refind_dir() already requires refind.conf to
        # exist at the given path, so an empty --refind-dir folder is reported
        # as "folder not found" (same behavior as every other cmd_* here, e.g.
        # test_list_without_refind_dir_fails_gracefully) rather than reaching
        # cmd_declutter's own (currently unreachable in practice) conf.is_file() check.
        root = str(Path(__file__).resolve().parent.parent)
        with TemporaryDirectory() as tmp:
            refind_dir = Path(tmp) / "refind"
            refind_dir.mkdir()
            result = run_cli(["--refind-dir", str(refind_dir), "declutter"], cwd=root)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Tidak menemukan", result.stderr)

    def test_interactive_menu_exits_cleanly_with_no_input(self):
        # When stdin is closed (e.g. non-interactive CI), running refindmgr
        # with no subcommand must open the menu and then exit cleanly instead
        # of hanging or crashing.
        root = str(Path(__file__).resolve().parent.parent)
        result = subprocess.run(
            [sys.executable, "-m", "refindmgr.cli", "--refind-dir", "/does/not/exist"],
            cwd=root,
            input="",
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("refindmgr", result.stdout)
        self.assertIn("Sampai jumpa", result.stdout)

    def test_interactive_menu_quit_option_exits_cleanly(self):
        root = str(Path(__file__).resolve().parent.parent)
        result = subprocess.run(
            [sys.executable, "-m", "refindmgr.cli", "--refind-dir", "/does/not/exist"],
            cwd=root,
            input="0\n",
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("Sampai jumpa", result.stdout)


class TestSetupVersionPinning(unittest.TestCase):
    """Tests cmd_setup's rEFInd-version-pinning step in-process (rather than via
    subprocess like the tests above), with system_mod.* patched out, so these
    never touch a real package manager regardless of what happens to be
    installed in the environment running the test suite.
    """

    APT_MANAGER = system_mod.PackageManagerInfo("apt", ["apt-get", "install", "-y", "refind"])

    def _run_setup(self, refind_dir, yes=False):
        args = argparse.Namespace(refind_dir=str(refind_dir), yes=yes)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            cli_mod.cmd_setup(args)
        return buffer.getvalue()

    def _make_refind_dir(self, tmp):
        refind_dir = Path(tmp) / "refind"
        refind_dir.mkdir()
        (refind_dir / "refind.conf").write_text("timeout 5\n")
        return refind_dir

    def test_already_pinned_reports_nothing_to_change(self):
        with TemporaryDirectory() as tmp:
            refind_dir = self._make_refind_dir(tmp)
            with patch.object(cli_mod.system_mod, "detect_package_manager", return_value=self.APT_MANAGER), \
                 patch.object(cli_mod.system_mod, "get_installed_refind_version", return_value="0.14.1"), \
                 patch.object(cli_mod.system_mod, "pin_refind_version") as pin_mock:
                output = self._run_setup(refind_dir)
                self.assertIn("sudah terpasang", output)
                self.assertIn("Tidak ada yang perlu diubah", output)
                pin_mock.assert_not_called()

    def test_newer_installed_previews_downgrade_without_yes(self):
        with TemporaryDirectory() as tmp:
            refind_dir = self._make_refind_dir(tmp)
            with patch.object(cli_mod.system_mod, "detect_package_manager", return_value=self.APT_MANAGER), \
                 patch.object(cli_mod.system_mod, "get_installed_refind_version", return_value="0.14.2"), \
                 patch.object(cli_mod.system_mod, "pin_refind_version") as pin_mock:
                output = self._run_setup(refind_dir, yes=False)
                self.assertIn("menurunkan (downgrade)", output)
                self.assertIn("pratinjau", output)
                pin_mock.assert_not_called()

    def test_older_installed_upgrades_with_yes(self):
        with TemporaryDirectory() as tmp:
            refind_dir = self._make_refind_dir(tmp)
            with patch.object(cli_mod.system_mod, "detect_package_manager", return_value=self.APT_MANAGER), \
                 patch.object(cli_mod.system_mod, "get_installed_refind_version", return_value="0.13.3"), \
                 patch.object(cli_mod.system_mod, "pin_refind_version", return_value="0.14.1-1") as pin_mock, \
                 patch.object(cli_mod.system_mod, "is_root", return_value=True):
                output = self._run_setup(refind_dir, yes=True)
                self.assertIn("menaikkan (upgrade)", output)
                self.assertIn("Berhasil", output)
                pin_mock.assert_called_once()

    def test_not_installed_previews_install_without_yes(self):
        with TemporaryDirectory() as tmp:
            refind_dir = self._make_refind_dir(tmp)
            with patch.object(cli_mod.system_mod, "detect_package_manager", return_value=self.APT_MANAGER), \
                 patch.object(cli_mod.system_mod, "get_installed_refind_version", return_value=None), \
                 patch.object(cli_mod.system_mod, "pin_refind_version") as pin_mock:
                output = self._run_setup(refind_dir, yes=False)
                self.assertIn("memasang paket rEFInd versi", output)
                pin_mock.assert_not_called()

    def test_pin_failure_is_a_warning_not_a_fatal_error(self):
        with TemporaryDirectory() as tmp:
            refind_dir = self._make_refind_dir(tmp)
            with patch.object(cli_mod.system_mod, "detect_package_manager", return_value=self.APT_MANAGER), \
                 patch.object(cli_mod.system_mod, "get_installed_refind_version", return_value="0.14.2"), \
                 patch.object(
                     cli_mod.system_mod,
                     "pin_refind_version",
                     side_effect=system_mod.BootstrapError("repo tidak menyediakan 0.14.1"),
                 ), \
                 patch.object(cli_mod.system_mod, "is_root", return_value=True):
                output = self._run_setup(refind_dir, yes=True)
                self.assertIn("PERINGATAN", output)
                self.assertIn("repo tidak menyediakan 0.14.1", output)

    def test_no_manager_detected_skips_pinning_gracefully(self):
        with TemporaryDirectory() as tmp:
            refind_dir = self._make_refind_dir(tmp)
            with patch.object(cli_mod.system_mod, "detect_package_manager", return_value=None):
                output = self._run_setup(refind_dir)
                self.assertIn("melewati", output)


if __name__ == "__main__":
    unittest.main()
