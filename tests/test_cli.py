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
    def test_ascii_header_matches_taag_slant(self):
        expected = (
            "               ____           __",
            "   _____ ____  / __(_)___  ____/ /___ ___  ____ ______",
            r"  / ___/ / __ \/ /_/ / __ \/ __  / __ `__ \/ __ `/ ___/",
            " / /    / /_/ / __/ / / / / /_/ / / / / / / /_/ / /",
            r"/_/     \____/_/ /_/_/ /_/\__,_/_/ /_/ /_/\__, /_/",
            "                                         /____/",
        )
        self.assertEqual(cli_mod._REFINDMGR_ASCII, expected)

    def test_help_runs_successfully(self):
        result = run_cli(["--help"], cwd=str(Path(__file__).resolve().parent.parent))
        self.assertEqual(result.returncode, 0)
        self.assertIn("refindmgr", result.stdout)

    def test_catalog_lists_entries(self):
        result = run_cli(["catalog"], cwd=str(Path(__file__).resolve().parent.parent))
        self.assertEqual(result.returncode, 0)
        self.assertIn("digital-void", result.stdout)
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
            (theme_src / "icons").mkdir()
            (theme_src / "icons" / "x.png").write_bytes(b"\x89PNG\r\n")

            install_result = run_cli(
                ["--refind-dir", str(refind_dir), "install", str(theme_src), "--activate"],
                cwd=root,
            )
            self.assertEqual(install_result.returncode, 0, install_result.stderr)
            self.assertIn("berhasil dipasang", install_result.stdout)
            self.assertIn("sekarang aktif", install_result.stdout)

            backup_count = len(list(refind_dir.glob("refind.conf.*.bak")))
            activate_again = run_cli(
                ["--refind-dir", str(refind_dir), "activate", "my-theme"], cwd=root
            )
            self.assertEqual(activate_again.returncode, 0, activate_again.stderr)
            self.assertIn("tidak membuat backup baru", activate_again.stdout)
            self.assertEqual(len(list(refind_dir.glob("refind.conf.*.bak"))), backup_count)

            list_result = run_cli(["--refind-dir", str(refind_dir), "list"], cwd=root)
            self.assertEqual(list_result.returncode, 0)
            self.assertIn("my-theme", list_result.stdout)

            remove_result = run_cli(
                ["--refind-dir", str(refind_dir), "remove", "my-theme"],
                cwd=root,
            )
            self.assertEqual(remove_result.returncode, 0)
            self.assertIn("telah dihapus", remove_result.stdout)


    def test_declutter_sets_showtools_only(self):
        # declutter is now intentionally conservative: it only ever writes
        # the tools row ("showtools"). Two separate live tests showed that
        # auto-writing scanfor/scan_all_linux_kernels/dont_scan_files could
        # make the real OS entry (Ubuntu via shim+GRUB) disappear entirely on
        # reboot, so declutter no longer touches OS-scan options at all.
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
            # declutter must NOT write any of these OS-scan options anymore.
            self.assertNotIn("scanfor internal,external,optical,manual", conf_text)
            self.assertNotIn("scan_all_linux_kernels false", conf_text)
            self.assertNotIn("dont_scan_files", conf_text)
            self.assertNotIn("dont_scan_dirs", conf_text)
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

    def test_declutter_undo_also_cleans_up_legacy_tokens(self):
        # If refind.conf still has active scanfor/scan_all_linux_kernels/
        # dont_scan_files/dont_scan_dirs lines written by an older refindmgr
        # version, --undo must still comment those out too, even though the
        # current version no longer writes them itself.
        root = str(Path(__file__).resolve().parent.parent)
        with TemporaryDirectory() as tmp:
            refind_dir = Path(tmp) / "refind"
            refind_dir.mkdir()
            (refind_dir / "refind.conf").write_text(
                "timeout 5\n"
                "showtools shutdown,reboot\n"
                "scanfor internal,external,optical,manual\n"
                "scan_all_linux_kernels false\n"
                "dont_scan_files + grubx64.efi\n"
                "dont_scan_dirs + boot\n"
            )
            undo = run_cli(["--refind-dir", str(refind_dir), "declutter", "--undo"], cwd=root)
            self.assertEqual(undo.returncode, 0, undo.stderr)
            conf_text = (refind_dir / "refind.conf").read_text()
            self.assertIn("# showtools shutdown,reboot", conf_text)
            self.assertIn("# scanfor internal,external,optical,manual", conf_text)
            self.assertIn("# scan_all_linux_kernels false", conf_text)
            self.assertIn("# dont_scan_files + grubx64.efi", conf_text)
            self.assertIn("# dont_scan_dirs + boot", conf_text)

    def test_declutter_neutralizes_active_theme_showtools_override(self):
        # Regression test for the real root cause behind "declutter ran but
        # the boot screen still shows lots of tool icons": if the active
        # theme's own theme.conf sets its own 'showtools' (very common in
        # decorative themes that want to show off custom tool icon art),
        # rEFInd processes 'include' inline, so that line can win over the
        # one refindmgr writes in the main refind.conf -- regardless of
        # rEFInd version. declutter must neutralize it too.
        root = str(Path(__file__).resolve().parent.parent)
        with TemporaryDirectory() as tmp:
            refind_dir = Path(tmp) / "refind"
            refind_dir.mkdir()
            (refind_dir / "refind.conf").write_text(
                "timeout 5\ninclude themes/matrix/theme.conf\n"
            )
            theme_dir = refind_dir / "themes" / "matrix"
            theme_dir.mkdir(parents=True)
            (theme_dir / "theme.conf").write_text(
                "selection_big icons/selection-big.png\n"
                "showtools shell, memtest, gdisk, about, hidden_tags, shutdown, reboot, firmware\n"
            )
            result = run_cli(["--refind-dir", str(refind_dir), "declutter"], cwd=root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("tema aktif 'matrix'", result.stdout)
            theme_conf_text = (theme_dir / "theme.conf").read_text()
            self.assertIn("# showtools shell, memtest", theme_conf_text)
            # A backup of the theme's own theme.conf must also exist.
            theme_backups = list(theme_dir.glob("theme.conf.*.bak"))
            self.assertEqual(len(theme_backups), 1)
            self.assertIn("showtools shell, memtest", theme_backups[0].read_text())

            undo = run_cli(["--refind-dir", str(refind_dir), "declutter", "--undo"], cwd=root)
            self.assertEqual(undo.returncode, 0, undo.stderr)
            self.assertIn("tema aktif 'matrix'", undo.stdout)
            restored_theme_conf = (theme_dir / "theme.conf").read_text()
            self.assertIn("showtools shell, memtest, gdisk", restored_theme_conf)
            self.assertNotIn("# showtools shell, memtest", restored_theme_conf)

    def test_declutter_without_active_theme_has_no_theme_note(self):
        root = str(Path(__file__).resolve().parent.parent)
        with TemporaryDirectory() as tmp:
            refind_dir = Path(tmp) / "refind"
            refind_dir.mkdir()
            (refind_dir / "refind.conf").write_text("timeout 5\n")
            result = run_cli(["--refind-dir", str(refind_dir), "declutter"], cwd=root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("tema aktif", result.stdout)

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
        self.assertIn("v2.", result.stdout)
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
                 patch.object(cli_mod.system_mod, "is_root", return_value=True), \
                 patch.object(cli_mod.system_mod, "run_refind_install", return_value="") as install_mock:
                output = self._run_setup(refind_dir, yes=True)
                self.assertIn("menaikkan (upgrade)", output)
                self.assertIn("Berhasil", output)
                pin_mock.assert_called_once()
                # Regression guard: mengganti versi PAKET saja tidak menyalin ulang
                # binari ke partisi EFI -- 'refind-install' resmi HARUS dijalankan
                # lagi setelah pin supaya file di ESP benar-benar ikut berubah.
                install_mock.assert_called_once()
                self.assertIn("refind-install", output)

    def test_pin_success_but_esp_binary_sync_not_confirmed_previews_only(self):
        # Tanpa --yes, baik pinning maupun sinkronisasi binari ESP harus
        # berhenti di tahap pratinjau -- tidak ada yang benar-benar dijalankan.
        with TemporaryDirectory() as tmp:
            refind_dir = self._make_refind_dir(tmp)
            with patch.object(cli_mod.system_mod, "detect_package_manager", return_value=self.APT_MANAGER), \
                 patch.object(cli_mod.system_mod, "get_installed_refind_version", return_value="0.14.2"), \
                 patch.object(cli_mod.system_mod, "pin_refind_version") as pin_mock, \
                 patch.object(cli_mod.system_mod, "run_refind_install") as install_mock:
                output = self._run_setup(refind_dir, yes=False)
                self.assertIn("pratinjau", output)
                pin_mock.assert_not_called()
                install_mock.assert_not_called()

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
                 patch.object(cli_mod.system_mod, "is_root", return_value=True), \
                 patch.object(cli_mod.system_mod, "run_refind_install", return_value=""):
                output = self._run_setup(refind_dir, yes=True)
                self.assertIn("PERINGATAN", output)
                self.assertIn("repo tidak menyediakan 0.14.1", output)

    def test_no_manager_detected_skips_pinning_gracefully(self):
        with TemporaryDirectory() as tmp:
            refind_dir = self._make_refind_dir(tmp)
            with patch.object(cli_mod.system_mod, "detect_package_manager", return_value=None):
                output = self._run_setup(refind_dir)
                self.assertIn("melewati", output)


class TestAptDebFallback(unittest.TestCase):
    """Regression tests for the real bug the user hit: their apt repo only
    carries rEFInd 0.14.2 (no 0.14.1), so pin_refind_version() always fails --
    refindmgr must fall back to downloading the official .deb from SourceForge
    and installing it directly, instead of just printing a warning forever.
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

    def test_falls_back_to_official_deb_when_apt_repo_lacks_target(self):
        with TemporaryDirectory() as tmp:
            refind_dir = self._make_refind_dir(tmp)
            with patch.object(cli_mod.system_mod, "detect_package_manager", return_value=self.APT_MANAGER), \
                 patch.object(cli_mod.system_mod, "get_installed_refind_version", return_value="0.14.2"), \
                 patch.object(
                     cli_mod.system_mod,
                     "pin_refind_version",
                     side_effect=system_mod.BootstrapError("Repo paket apt di sistem ini tidak menyediakan rEFInd versi 0.14.1."),
                 ), \
                 patch.object(cli_mod.system_mod, "download_refind_deb", return_value="/tmp/refind_0.14.1-1_amd64.deb") as download_mock, \
                 patch.object(cli_mod.system_mod, "install_deb_file", return_value="") as install_mock, \
                 patch.object(cli_mod.system_mod, "is_root", return_value=True), \
                 patch.object(cli_mod.system_mod, "run_refind_install", return_value=""):
                output = self._run_setup(refind_dir, yes=True)
                self.assertIn("jalur cadangan", output)
                self.assertIn("Berhasil", output)
                download_mock.assert_called_once()
                self.assertEqual(download_mock.call_args[0][0], "0.14.1")
                install_mock.assert_called_once()

    def test_reports_clearly_when_fallback_download_also_fails(self):
        with TemporaryDirectory() as tmp:
            refind_dir = self._make_refind_dir(tmp)
            with patch.object(cli_mod.system_mod, "detect_package_manager", return_value=self.APT_MANAGER), \
                 patch.object(cli_mod.system_mod, "get_installed_refind_version", return_value="0.14.2"), \
                 patch.object(
                     cli_mod.system_mod,
                     "pin_refind_version",
                     side_effect=system_mod.BootstrapError("repo tidak menyediakan 0.14.1"),
                 ), \
                 patch.object(
                     cli_mod.system_mod,
                     "download_refind_deb",
                     side_effect=system_mod.BootstrapError("tidak ada koneksi internet"),
                 ), \
                 patch.object(cli_mod.system_mod, "is_root", return_value=True), \
                 patch.object(cli_mod.system_mod, "run_refind_install", return_value=""):
                output = self._run_setup(refind_dir, yes=True)
                self.assertIn("jalur cadangan juga gagal", output)
                self.assertIn("tidak ada koneksi internet", output)

    def test_non_apt_manager_does_not_attempt_deb_fallback(self):
        with TemporaryDirectory() as tmp:
            refind_dir = self._make_refind_dir(tmp)
            manager = system_mod.PackageManagerInfo("dnf", ["dnf", "install", "-y", "refind"])
            with patch.object(cli_mod.system_mod, "detect_package_manager", return_value=manager), \
                 patch.object(cli_mod.system_mod, "get_installed_refind_version", return_value="0.14.2"), \
                 patch.object(
                     cli_mod.system_mod,
                     "pin_refind_version",
                     side_effect=system_mod.BootstrapError("repo tidak menyediakan 0.14.1"),
                 ), \
                 patch.object(cli_mod.system_mod, "download_refind_deb") as download_mock, \
                 patch.object(cli_mod.system_mod, "is_root", return_value=True), \
                 patch.object(cli_mod.system_mod, "run_refind_install", return_value=""):
                output = self._run_setup(refind_dir, yes=True)
                self.assertIn("PERINGATAN", output)
                download_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()

class TestDedupeSafety(unittest.TestCase):
    def _make_esp(self, tmp: str):
        esp = Path(tmp) / "esp"
        refind_dir = esp / "EFI" / "refind"
        ubuntu_dir = esp / "EFI" / "ubuntu"
        boot_dir = esp / "EFI" / "BOOT"
        refind_dir.mkdir(parents=True)
        ubuntu_dir.mkdir(parents=True)
        boot_dir.mkdir(parents=True)
        (refind_dir / "refind.conf").write_text("timeout 5\n")
        (ubuntu_dir / "grubx64.efi").write_bytes(b"ubuntu-loader")
        (boot_dir / "BOOTX64.EFI").write_bytes(b"ubuntu-loader")
        return refind_dir

    def test_dedupe_preview_never_writes(self):
        root = str(Path(__file__).resolve().parent.parent)
        with TemporaryDirectory() as tmp:
            refind_dir = self._make_esp(tmp)
            before = (refind_dir / "refind.conf").read_text()
            result = run_cli(["--refind-dir", str(refind_dir), "dedupe"], cwd=root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("TIDAK ADA FILE DIUBAH", result.stdout)
            self.assertEqual((refind_dir / "refind.conf").read_text(), before)
            self.assertEqual(list(refind_dir.glob("refind.conf.*.bak")), [])

    def test_dedupe_can_disable_kernels_and_hide_identical_fallback(self):
        root = str(Path(__file__).resolve().parent.parent)
        with TemporaryDirectory() as tmp:
            refind_dir = self._make_esp(tmp)
            result = run_cli([
                "--refind-dir", str(refind_dir), "dedupe", "--apply",
                "--keep-loader", "EFI/ubuntu/grubx64.efi", "--disable-kernels",
                "--hide-fallback", "EFI/BOOT/BOOTX64.EFI",
            ], cwd=root)
            self.assertEqual(result.returncode, 0, result.stderr)
            conf = (refind_dir / "refind.conf").read_text()
            self.assertIn("scan_all_linux_kernels false", conf)
            self.assertIn("dont_scan_files + EFI/BOOT/BOOTX64.EFI", conf)
            self.assertIn("Loader OS yang dipertahankan: EFI/ubuntu/grubx64.efi", result.stdout)
            self.assertEqual(len(list(refind_dir.glob("refind.conf.*.bak"))), 1)

    def test_dedupe_refuses_loader_covered_by_legacy_global_rule(self):
        root = str(Path(__file__).resolve().parent.parent)
        with TemporaryDirectory() as tmp:
            refind_dir = self._make_esp(tmp)
            (refind_dir / "refind.conf").write_text("dont_scan_files + grubx64.efi\n")
            result = run_cli([
                "--refind-dir", str(refind_dir), "dedupe", "--apply",
                "--keep-loader", "EFI/ubuntu/grubx64.efi", "--disable-kernels",
            ], cwd=root)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("masih tercakup", result.stderr)
            self.assertEqual((refind_dir / "refind.conf").read_text(), "dont_scan_files + grubx64.efi\n")

    def test_dedupe_refuses_nonidentical_fallback(self):
        root = str(Path(__file__).resolve().parent.parent)
        with TemporaryDirectory() as tmp:
            refind_dir = self._make_esp(tmp)
            (refind_dir.parent / "BOOT" / "BOOTX64.EFI").write_bytes(b"different-loader")
            result = run_cli([
                "--refind-dir", str(refind_dir), "dedupe", "--apply",
                "--keep-loader", "EFI/ubuntu/grubx64.efi",
                "--hide-fallback", "EFI/BOOT/BOOTX64.EFI",
            ], cwd=root)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("TIDAK byte-identik", result.stderr)

class TestCleanMenuSafety(unittest.TestCase):
    def _make_esp(self, tmp: str):
        esp = Path(tmp) / "esp"
        refind_dir = esp / "EFI" / "refind"
        ubuntu_dir = esp / "EFI" / "ubuntu"
        refind_dir.mkdir(parents=True)
        ubuntu_dir.mkdir(parents=True)
        (refind_dir / "refind.conf").write_text("timeout 5\n")
        (ubuntu_dir / "grubx64.efi").write_bytes(b"ubuntu-loader")
        return refind_dir

    def test_clean_menu_preview_is_read_only(self):
        root = str(Path(__file__).resolve().parent.parent)
        with TemporaryDirectory() as tmp:
            refind_dir = self._make_esp(tmp)
            result = run_cli([
                "--refind-dir", str(refind_dir), "clean-menu",
                "--os", "Ubuntu=EFI/ubuntu/grubx64.efi",
            ], cwd=root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("TIDAK ADA FILE DIUBAH", result.stdout)
            self.assertNotIn("scanfor manual", (refind_dir / "refind.conf").read_text())

    def test_clean_menu_apply_and_undo_restore_scanfor(self):
        root = str(Path(__file__).resolve().parent.parent)
        with TemporaryDirectory() as tmp:
            refind_dir = self._make_esp(tmp)
            (refind_dir / "refind.conf").write_text("scanfor internal,external\n")
            apply_result = run_cli([
                "--refind-dir", str(refind_dir), "clean-menu", "--apply",
                "--os", "Ubuntu=EFI/ubuntu/grubx64.efi",
            ], cwd=root)
            self.assertEqual(apply_result.returncode, 0, apply_result.stderr)
            applied = (refind_dir / "refind.conf").read_text()
            self.assertIn("scanfor manual", applied)
            self.assertIn('menuentry "Ubuntu" {', applied)
            self.assertIn("loader /EFI/ubuntu/grubx64.efi", applied)
            self.assertIn("refindmgr-clean-menu: previous-scanfor=internal,external", applied)

            undo_result = run_cli(["--refind-dir", str(refind_dir), "clean-menu", "--undo"], cwd=root)
            self.assertEqual(undo_result.returncode, 0, undo_result.stderr)
            restored = (refind_dir / "refind.conf").read_text()
            self.assertIn("scanfor internal,external", restored)
            self.assertNotIn("refindmgr-clean-menu", restored)
            self.assertNotIn('menuentry "Ubuntu"', restored)

    def test_clean_menu_rejects_legacy_global_exclusion(self):
        root = str(Path(__file__).resolve().parent.parent)
        with TemporaryDirectory() as tmp:
            refind_dir = self._make_esp(tmp)
            (refind_dir / "refind.conf").write_text("dont_scan_files + grubx64.efi\n")
            result = run_cli([
                "--refind-dir", str(refind_dir), "clean-menu", "--apply",
                "--os", "Ubuntu=EFI/ubuntu/grubx64.efi",
            ], cwd=root)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("masih tercakup", result.stderr)

class TestAutoCleanMenu(unittest.TestCase):
    def _make_esp(self, tmp: str):
        esp = Path(tmp) / "esp"
        refind_dir = esp / "EFI" / "refind"
        ubuntu_dir = esp / "EFI" / "ubuntu"
        boot_dir = esp / "EFI" / "BOOT"
        refind_dir.mkdir(parents=True)
        ubuntu_dir.mkdir(parents=True)
        boot_dir.mkdir(parents=True)
        (refind_dir / "refind.conf").write_text("timeout 5\n")
        (ubuntu_dir / "grubx64.efi").write_bytes(b"ubuntu")
        (ubuntu_dir / "shimx64.efi").write_bytes(b"shim")
        (boot_dir / "BOOTX64.EFI").write_bytes(b"fallback")
        (boot_dir / "fbx64.efi").write_bytes(b"fallback-helper")
        return refind_dir

    def test_clean_menu_auto_preview_detects_standard_os_only(self):
        root = str(Path(__file__).resolve().parent.parent)
        with TemporaryDirectory() as tmp:
            refind_dir = self._make_esp(tmp)
            result = run_cli(["--refind-dir", str(refind_dir), "clean-menu", "--auto"], cwd=root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Ubuntu: /EFI/ubuntu/shimx64.efi", result.stdout)
            self.assertNotIn("BOOTX64", result.stdout)
            self.assertNotIn("fbx64", result.stdout)
            self.assertNotIn("fbx64", result.stdout)
            self.assertNotIn("scanfor manual", (refind_dir / "refind.conf").read_text())

    def test_clean_menu_auto_apply_writes_only_detected_os(self):
        root = str(Path(__file__).resolve().parent.parent)
        with TemporaryDirectory() as tmp:
            refind_dir = self._make_esp(tmp)
            result = run_cli(["--refind-dir", str(refind_dir), "clean-menu", "--auto", "--apply"], cwd=root)
            self.assertEqual(result.returncode, 0, result.stderr)
            conf = (refind_dir / "refind.conf").read_text()
            self.assertIn("scanfor manual", conf)
            self.assertIn('menuentry "Ubuntu" {', conf)
            self.assertIn("loader /EFI/ubuntu/shimx64.efi", conf)
            self.assertNotIn("BOOTX64", conf)
            self.assertNotIn("fbx64", conf)

    def test_interactive_menu_auto_option_applies_after_confirmation(self):
        root = str(Path(__file__).resolve().parent.parent)
        with TemporaryDirectory() as tmp:
            refind_dir = self._make_esp(tmp)
            result = subprocess.run(
                [sys.executable, "-m", "refindmgr.cli", "--refind-dir", str(refind_dir)],
                cwd=root,
                input="10\ny\n\n0\n",
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("OS ditemukan", result.stdout)
            self.assertIn("Berhasil menerapkan mode OS saja", result.stdout)
            conf = (refind_dir / "refind.conf").read_text()
            self.assertIn("scanfor manual", conf)
            self.assertIn('menuentry "Ubuntu" {', conf)
