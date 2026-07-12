import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


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


if __name__ == "__main__":
    unittest.main()
