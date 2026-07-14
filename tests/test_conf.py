import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from refindmgr import conf as conf_mod


SAMPLE_CONF = [
    "timeout 5",
    "# a comment",
    "include themes/old-theme/theme.conf",
    "showtools shell, memtest, gdisk",
]


class TestFindThemeIncludes(unittest.TestCase):
    def test_finds_active_line(self):
        result = conf_mod.find_theme_includes(SAMPLE_CONF)
        self.assertEqual(result, [(2, "old-theme", True)])

    def test_finds_commented_line(self):
        lines = ["# include themes/foo/theme.conf"]
        result = conf_mod.find_theme_includes(lines)
        self.assertEqual(result, [(0, "foo", False)])

    def test_no_matches_returns_empty(self):
        self.assertEqual(conf_mod.find_theme_includes(["timeout 5"]), [])


class TestGetActiveTheme(unittest.TestCase):
    def test_returns_active_name(self):
        self.assertEqual(conf_mod.get_active_theme(SAMPLE_CONF), "old-theme")

    def test_returns_none_when_no_active(self):
        lines = ["# include themes/foo/theme.conf"]
        self.assertIsNone(conf_mod.get_active_theme(lines))


class TestGetActiveThemes(unittest.TestCase):
    def test_returns_all_active_names(self):
        lines = [
            "include themes/foo/theme.conf",
            "include themes/bar/theme.conf",
            "# include themes/baz/theme.conf",
        ]
        self.assertEqual(conf_mod.get_active_themes(lines), ["foo", "bar"])

    def test_empty_when_none_active(self):
        lines = ["# include themes/foo/theme.conf"]
        self.assertEqual(conf_mod.get_active_themes(lines), [])


class TestActivateTheme(unittest.TestCase):
    def test_switches_active_theme_and_comments_old(self):
        new_lines = conf_mod.activate_theme(SAMPLE_CONF, "new-theme")
        self.assertIn("# include themes/old-theme/theme.conf", new_lines)
        self.assertIn("include themes/new-theme/theme.conf", new_lines)
        self.assertEqual(conf_mod.get_active_theme(new_lines), "new-theme")

    def test_reactivating_commented_theme(self):
        lines = ["# include themes/foo/theme.conf", "# include themes/bar/theme.conf"]
        new_lines = conf_mod.activate_theme(lines, "bar")
        self.assertEqual(conf_mod.get_active_theme(new_lines), "bar")
        self.assertIn("# include themes/foo/theme.conf", new_lines)

    def test_appends_when_theme_not_present(self):
        lines = ["timeout 5"]
        new_lines = conf_mod.activate_theme(lines, "brand-new")
        self.assertEqual(conf_mod.get_active_theme(new_lines), "brand-new")
        self.assertIn("include themes/brand-new/theme.conf", new_lines)

    def test_idempotent_when_already_active(self):
        new_lines = conf_mod.activate_theme(SAMPLE_CONF, "old-theme")
        self.assertEqual(conf_mod.get_active_theme(new_lines), "old-theme")
        self.assertEqual(new_lines.count("include themes/old-theme/theme.conf"), 1)


class TestDeactivateAll(unittest.TestCase):
    def test_comments_out_active_theme(self):
        new_lines = conf_mod.deactivate_all(SAMPLE_CONF)
        self.assertIsNone(conf_mod.get_active_theme(new_lines))
        self.assertIn("# include themes/old-theme/theme.conf", new_lines)

    def test_no_op_when_nothing_active(self):
        lines = ["# include themes/foo/theme.conf"]
        new_lines = conf_mod.deactivate_all(lines)
        self.assertEqual(lines, new_lines)


class TestRemoveThemeIncludes(unittest.TestCase):
    def test_removes_matching_lines_only(self):
        lines = [
            "include themes/keep-me/theme.conf",
            "include themes/remove-me/theme.conf",
            "# include themes/remove-me/theme.conf",
            "timeout 5",
        ]
        new_lines = conf_mod.remove_theme_includes(lines, "remove-me")
        self.assertEqual(
            new_lines,
            ["include themes/keep-me/theme.conf", "timeout 5"],
        )


class TestBackupRestore(unittest.TestCase):
    def test_backup_then_restore_roundtrip(self):
        with TemporaryDirectory() as tmp:
            conf_path = Path(tmp) / "refind.conf"
            conf_path.write_text("timeout 5\n")
            backup_path = conf_mod.backup(conf_path)
            self.assertTrue(backup_path.is_file())

            conf_path.write_text("timeout 20\n")
            self.assertEqual(conf_path.read_text(), "timeout 20\n")

            conf_mod.restore(conf_path, backup_path)
            self.assertEqual(conf_path.read_text(), "timeout 5\n")

    def test_list_backups_sorted(self):
        with TemporaryDirectory() as tmp:
            conf_path = Path(tmp) / "refind.conf"
            conf_path.write_text("timeout 5\n")
            b1 = conf_mod.backup(conf_path)
            b2 = conf_mod.backup(conf_path)
            backups = conf_mod.list_backups(conf_path)
            self.assertEqual(set(backups), {b1, b2})

    def test_rapid_backups_never_collide(self):
        # Regression test: backup() used to name files with second-resolution
        # timestamps only, so calling it multiple times within the same
        # second silently overwrote the previous backup and lost history.
        with TemporaryDirectory() as tmp:
            conf_path = Path(tmp) / "refind.conf"
            conf_path.write_text("timeout 5\n")
            backup_paths = [conf_mod.backup(conf_path) for _ in range(5)]
            self.assertEqual(len(backup_paths), len(set(backup_paths)))
            for path in backup_paths:
                self.assertTrue(path.is_file())


class TestFindGlobalOption(unittest.TestCase):
    def test_finds_active_line(self):
        lines = ["timeout 5", "showtools shell, memtest, gdisk"]
        result = conf_mod.find_global_option(lines, "showtools")
        self.assertEqual(result, [(1, True, "shell, memtest, gdisk")])

    def test_finds_commented_line(self):
        lines = ["# showtools shell, memtest"]
        result = conf_mod.find_global_option(lines, "showtools")
        self.assertEqual(result, [(0, False, "shell, memtest")])

    def test_no_matches_returns_empty(self):
        self.assertEqual(conf_mod.find_global_option(["timeout 5"], "showtools"), [])

    def test_does_not_match_unrelated_token_with_same_prefix(self):
        # 'scanfor' must not match a 'scanfor2' style token (defensive check,
        # even though rEFInd itself has no such token).
        lines = ["scanfor2 something"]
        self.assertEqual(conf_mod.find_global_option(lines, "scanfor"), [])


class TestGetGlobalOption(unittest.TestCase):
    def test_returns_active_value(self):
        lines = ["showtools shell, memtest"]
        self.assertEqual(conf_mod.get_global_option(lines, "showtools"), "shell, memtest")

    def test_returns_none_when_only_commented(self):
        lines = ["# showtools shell, memtest"]
        self.assertIsNone(conf_mod.get_global_option(lines, "showtools"))

    def test_returns_none_when_absent(self):
        self.assertIsNone(conf_mod.get_global_option(["timeout 5"], "showtools"))


class TestSetGlobalOption(unittest.TestCase):
    def test_replaces_existing_active_line(self):
        lines = ["timeout 5", "showtools shell, memtest, gdisk"]
        new_lines = conf_mod.set_global_option(lines, "showtools", "shutdown,reboot")
        self.assertIn("showtools shutdown,reboot", new_lines)
        self.assertNotIn("showtools shell, memtest, gdisk", new_lines)
        self.assertEqual(len(new_lines), 2)

    def test_uncomments_and_replaces_commented_line(self):
        lines = ["# showtools shell, memtest"]
        new_lines = conf_mod.set_global_option(lines, "showtools", "shutdown,reboot")
        self.assertEqual(new_lines, ["showtools shutdown,reboot"])

    def test_appends_when_token_absent(self):
        lines = ["timeout 5"]
        new_lines = conf_mod.set_global_option(lines, "showtools", "shutdown,reboot")
        self.assertIn("showtools shutdown,reboot", new_lines)
        self.assertEqual(conf_mod.get_global_option(new_lines, "showtools"), "shutdown,reboot")

    def test_comments_out_duplicate_active_lines(self):
        lines = ["showtools shell", "showtools memtest"]
        new_lines = conf_mod.set_global_option(lines, "showtools", "shutdown,reboot")
        active = conf_mod.find_global_option(new_lines, "showtools")
        active_only = [rest for _, is_active, rest in active if is_active]
        self.assertEqual(active_only, ["shutdown,reboot"])

    def test_idempotent_when_run_twice(self):
        lines = ["showtools shell, memtest"]
        once = conf_mod.set_global_option(lines, "showtools", "shutdown,reboot")
        twice = conf_mod.set_global_option(once, "showtools", "shutdown,reboot")
        self.assertEqual(once, twice)


class TestUnsetGlobalOption(unittest.TestCase):
    def test_comments_out_active_line(self):
        lines = ["showtools shutdown,reboot"]
        new_lines = conf_mod.unset_global_option(lines, "showtools")
        self.assertIsNone(conf_mod.get_global_option(new_lines, "showtools"))
        self.assertIn("# showtools shutdown,reboot", new_lines)

    def test_no_op_when_already_commented(self):
        lines = ["# showtools shutdown,reboot"]
        new_lines = conf_mod.unset_global_option(lines, "showtools")
        self.assertEqual(lines, new_lines)

    def test_no_op_when_absent(self):
        lines = ["timeout 5"]
        self.assertEqual(conf_mod.unset_global_option(lines, "showtools"), lines)


class TestReadWriteLines(unittest.TestCase):
    def test_write_then_read_roundtrip(self):
        with TemporaryDirectory() as tmp:
            conf_path = Path(tmp) / "refind.conf"
            conf_mod.write_lines(conf_path, ["a", "b", "c"])
            self.assertEqual(conf_mod.read_lines(conf_path), ["a", "b", "c"])


if __name__ == "__main__":
    unittest.main()
