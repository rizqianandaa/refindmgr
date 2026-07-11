import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

from refindtm import themes as themes_mod
from refindtm import conf as conf_mod
from refindtm.paths import refind_conf_path, themes_dir


def _make_refind_dir(tmp: str) -> Path:
    refind_dir = Path(tmp) / "refind"
    refind_dir.mkdir(parents=True)
    (refind_dir / "refind.conf").write_text("timeout 5\n")
    return refind_dir


def _make_fake_theme(tmp: str, name: str = "fake-theme") -> Path:
    theme_src = Path(tmp) / "theme_src" / name
    theme_src.mkdir(parents=True)
    (theme_src / "theme.conf").write_text("selection_big icons/selection-big.png\n")
    (theme_src / "icons").mkdir()
    (theme_src / "icons" / "selection-big.png").write_bytes(b"\x89PNG\r\n")
    return theme_src


class TestListInstalled(unittest.TestCase):
    def test_empty_when_no_themes_dir(self):
        with TemporaryDirectory() as tmp:
            refind_dir = _make_refind_dir(tmp)
            self.assertEqual(themes_mod.list_installed(refind_dir), [])

    def test_lists_only_dirs_with_theme_conf(self):
        with TemporaryDirectory() as tmp:
            refind_dir = _make_refind_dir(tmp)
            t_dir = themes_dir(refind_dir)
            t_dir.mkdir()
            (t_dir / "good-theme").mkdir()
            (t_dir / "good-theme" / "theme.conf").write_text("x")
            (t_dir / "not-a-theme").mkdir()
            self.assertEqual(themes_mod.list_installed(refind_dir), ["good-theme"])


class TestInstallFromLocalDir(unittest.TestCase):
    def test_install_copies_theme_and_returns_name(self):
        with TemporaryDirectory() as tmp:
            refind_dir = _make_refind_dir(tmp)
            theme_src = _make_fake_theme(tmp)
            installed_name = themes_mod.install_theme(refind_dir, str(theme_src))
            self.assertEqual(installed_name, "fake-theme")
            dest = themes_dir(refind_dir) / "fake-theme"
            self.assertTrue((dest / "theme.conf").is_file())
            self.assertTrue((dest / "icons" / "selection-big.png").is_file())

    def test_install_with_custom_name(self):
        with TemporaryDirectory() as tmp:
            refind_dir = _make_refind_dir(tmp)
            theme_src = _make_fake_theme(tmp)
            installed_name = themes_mod.install_theme(refind_dir, str(theme_src), name="custom")
            self.assertEqual(installed_name, "custom")
            self.assertTrue((themes_dir(refind_dir) / "custom" / "theme.conf").is_file())

    def test_install_duplicate_raises(self):
        with TemporaryDirectory() as tmp:
            refind_dir = _make_refind_dir(tmp)
            theme_src = _make_fake_theme(tmp)
            themes_mod.install_theme(refind_dir, str(theme_src))
            with self.assertRaises(themes_mod.ThemeError):
                themes_mod.install_theme(refind_dir, str(theme_src))

    def test_install_missing_theme_conf_raises(self):
        with TemporaryDirectory() as tmp:
            refind_dir = _make_refind_dir(tmp)
            empty_src = Path(tmp) / "empty_theme"
            empty_src.mkdir()
            with self.assertRaises(themes_mod.ThemeError):
                themes_mod.install_theme(refind_dir, str(empty_src))

    def test_install_nonexistent_source_raises(self):
        with TemporaryDirectory() as tmp:
            refind_dir = _make_refind_dir(tmp)
            with self.assertRaises(themes_mod.ThemeError):
                themes_mod.install_theme(refind_dir, str(Path(tmp) / "does-not-exist"))


class TestInstallFromZip(unittest.TestCase):
    def test_install_from_zip_extracts_and_copies(self):
        with TemporaryDirectory() as tmp:
            refind_dir = _make_refind_dir(tmp)
            theme_src = _make_fake_theme(tmp, name="zip-theme")
            zip_path = Path(tmp) / "zip-theme.zip"
            with zipfile.ZipFile(zip_path, "w") as zf:
                for file in theme_src.rglob("*"):
                    if file.is_file():
                        zf.write(file, file.relative_to(theme_src.parent))
            installed_name = themes_mod.install_theme(refind_dir, str(zip_path))
            self.assertEqual(installed_name, "zip-theme")
            self.assertTrue((themes_dir(refind_dir) / "zip-theme" / "theme.conf").is_file())


class TestRemoveTheme(unittest.TestCase):
    def test_remove_deletes_folder_and_conf_line(self):
        with TemporaryDirectory() as tmp:
            refind_dir = _make_refind_dir(tmp)
            theme_src = _make_fake_theme(tmp)
            themes_mod.install_theme(refind_dir, str(theme_src))

            conf_path = refind_conf_path(refind_dir)
            lines = conf_mod.read_lines(conf_path)
            conf_mod.write_lines(conf_path, conf_mod.activate_theme(lines, "fake-theme"))

            themes_mod.remove_theme(refind_dir, "fake-theme")

            self.assertFalse((themes_dir(refind_dir) / "fake-theme").exists())
            remaining = conf_mod.read_lines(conf_path)
            self.assertIsNone(conf_mod.get_active_theme(remaining))

    def test_remove_nonexistent_raises(self):
        with TemporaryDirectory() as tmp:
            refind_dir = _make_refind_dir(tmp)
            with self.assertRaises(themes_mod.ThemeError):
                themes_mod.remove_theme(refind_dir, "ghost-theme")


class TestGitAvailability(unittest.TestCase):
    def test_is_git_available_returns_bool(self):
        self.assertIsInstance(themes_mod.is_git_available(), bool)


class TestValidateThemeName(unittest.TestCase):
    def test_rejects_empty(self):
        with self.assertRaises(themes_mod.ThemeError):
            themes_mod.validate_theme_name("")

    def test_rejects_dot_and_dotdot(self):
        with self.assertRaises(themes_mod.ThemeError):
            themes_mod.validate_theme_name(".")
        with self.assertRaises(themes_mod.ThemeError):
            themes_mod.validate_theme_name("..")

    def test_rejects_path_separators(self):
        with self.assertRaises(themes_mod.ThemeError):
            themes_mod.validate_theme_name("../../etc")
        with self.assertRaises(themes_mod.ThemeError):
            themes_mod.validate_theme_name("foo/bar")
        with self.assertRaises(themes_mod.ThemeError):
            themes_mod.validate_theme_name("foo\\bar")

    def test_accepts_normal_name(self):
        themes_mod.validate_theme_name("my-cool-theme")


class TestPathTraversalProtection(unittest.TestCase):
    def test_install_rejects_traversal_name(self):
        # Regression test: --name '../../evil' must never be able to make
        # install_theme() write outside of the themes/ folder, since this
        # tool commonly runs as root against the EFI partition.
        with TemporaryDirectory() as tmp:
            refind_dir = _make_refind_dir(tmp)
            theme_src = _make_fake_theme(tmp)
            with self.assertRaises(themes_mod.ThemeError):
                themes_mod.install_theme(refind_dir, str(theme_src), name="../../evil")
            # Confirm nothing escaped the themes/ dir.
            self.assertFalse((Path(tmp) / "evil").exists())

    def test_remove_rejects_traversal_name(self):
        with TemporaryDirectory() as tmp:
            refind_dir = _make_refind_dir(tmp)
            with self.assertRaises(themes_mod.ThemeError):
                themes_mod.remove_theme(refind_dir, "../../evil")


if __name__ == "__main__":
    unittest.main()
