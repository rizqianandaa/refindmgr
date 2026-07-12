import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from refindmgr.paths import detect_refind_dir, refind_conf_path, themes_dir, ENV_OVERRIDE


class TestDetectRefindDir(unittest.TestCase):
    def setUp(self):
        self._old_env = os.environ.pop(ENV_OVERRIDE, None)

    def tearDown(self):
        if self._old_env is not None:
            os.environ[ENV_OVERRIDE] = self._old_env

    def test_explicit_path_used_when_valid(self):
        with TemporaryDirectory() as tmp:
            (Path(tmp) / "refind.conf").write_text("# empty\n")
            found = detect_refind_dir(explicit=tmp)
            self.assertEqual(found, Path(tmp))

    def test_explicit_path_ignored_when_no_conf(self):
        with TemporaryDirectory() as tmp:
            found = detect_refind_dir(explicit=tmp)
            self.assertIsNone(found)

    def test_env_override_used(self):
        with TemporaryDirectory() as tmp:
            (Path(tmp) / "refind.conf").write_text("# empty\n")
            os.environ[ENV_OVERRIDE] = tmp
            found = detect_refind_dir()
            self.assertEqual(found, Path(tmp))

    def test_returns_none_when_nothing_matches(self):
        found = detect_refind_dir(explicit="/definitely/does/not/exist/anywhere")
        self.assertIsNone(found)


class TestPathHelpers(unittest.TestCase):
    def test_refind_conf_path(self):
        self.assertEqual(refind_conf_path(Path("/x/refind")), Path("/x/refind/refind.conf"))

    def test_themes_dir(self):
        self.assertEqual(themes_dir(Path("/x/refind")), Path("/x/refind/themes"))


if __name__ == "__main__":
    unittest.main()
