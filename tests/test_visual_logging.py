import io
import logging
import os
import stat
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from refindmgr import app_logging
from refindmgr import cli


class FakeTerminal(io.StringIO):
    def __init__(self, *, encoding="utf-8", tty=True):
        super().__init__()
        self._encoding = encoding
        self._tty = tty

    @property
    def encoding(self):
        return self._encoding

    def isatty(self):
        return self._tty


class TestVisualFallbacks(unittest.TestCase):
    def test_unicode_prompt_and_rule_on_utf8_tty(self):
        stream = FakeTerminal()
        with patch.object(sys, "stdout", stream):
            self.assertEqual(cli._prompt_arrow(), "❯")
            self.assertEqual(cli._rule_character(), "─")

    def test_ascii_fallback_on_non_tty(self):
        stream = FakeTerminal(tty=False)
        with patch.object(sys, "stdout", stream):
            self.assertEqual(cli._prompt_arrow(), ">")
            self.assertEqual(cli._rule_character(), "-")

    def test_ascii_fallback_on_ascii_terminal(self):
        stream = FakeTerminal(encoding="ascii")
        with patch.object(sys, "stdout", stream):
            self.assertEqual(cli._prompt_arrow(), ">")
            self.assertEqual(cli._rule_character(), "-")

    def test_prompt_has_no_question_badge(self):
        stream = FakeTerminal()
        with patch.object(sys, "stdout", stream), patch("builtins.input", return_value="0") as mocked:
            cli._prompt("Pilih menu")
        rendered = mocked.call_args.args[0]
        self.assertIn("Pilih menu", rendered)
        self.assertIn("❯", rendered)
        self.assertNotIn("[?]", rendered)

    def test_plain_loading_does_not_emit_cursor_controls(self):
        stream = FakeTerminal(tty=False)
        with patch.object(sys, "stdout", stream):
            with cli._menu_loading("Memasang"):
                pass
        self.assertEqual(stream.getvalue(), "\nMemasang...\n")


class TestSafeLogging(unittest.TestCase):
    def tearDown(self):
        logger = logging.getLogger(app_logging.LOGGER_NAME)
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)
        app_logging._CONFIGURED_PATH = None

    def test_non_root_path_respects_xdg_state_home(self):
        with patch.dict(os.environ, {"XDG_STATE_HOME": "/tmp/state"}, clear=False):
            self.assertEqual(
                app_logging.default_log_path(root=False),
                Path("/tmp/state/refindmgr/refindmgr.log"),
            )

    def test_filter_redacts_credentials_email_and_home(self):
        record = logging.LogRecord(
            "refindmgr.test", logging.INFO, __file__, 1,
            "url=https://user:secret@example.test/repo?token=abc email=user@example.com path=%s/private",
            (str(Path.home()),), None,
        )
        self.assertTrue(app_logging.RedactingFilter().filter(record))
        message = record.getMessage()
        self.assertNotIn("secret", message)
        self.assertNotIn("token=abc", message)
        self.assertNotIn("user@example.com", message)
        self.assertNotIn(str(Path.home()), message)
        # The originals are kept rather than discarded, and re-filtering the
        # same record must not redact twice.
        self.assertEqual(record.refindmgr_raw_args, (str(Path.home()),))
        self.assertTrue(app_logging.RedactingFilter().filter(record))
        self.assertEqual(record.getMessage(), message)

    def test_git_clone_source_is_not_mistaken_for_an_email(self):
        record = logging.LogRecord(
            "refindmgr.test", logging.INFO, __file__, 1,
            "source=git@github.com:catppuccin/refind.git", (), None,
        )
        app_logging.RedactingFilter().filter(record)
        self.assertIn("git@github.com:catppuccin/refind.git", record.getMessage())

    def test_log_is_private_and_rotates(self):
        with TemporaryDirectory() as tmp, patch.object(app_logging, "MAX_BYTES", 300):
            path = Path(tmp) / "refindmgr.log"
            configured = app_logging.configure(path)
            self.assertEqual(configured, path)
            logger = logging.getLogger("refindmgr.test")
            for _ in range(8):
                logger.info("x" * 120)
            for handler in logging.getLogger(app_logging.LOGGER_NAME).handlers:
                handler.flush()
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertTrue(Path(str(path) + ".1").is_file())


if __name__ == "__main__":
    unittest.main()
