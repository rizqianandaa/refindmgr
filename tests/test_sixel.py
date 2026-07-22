import io
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from refindmgr import sixel


class _TTYOutput:
    def __init__(self):
        self.buffer = io.BytesIO()
        self.text = ""

    def isatty(self):
        return True

    def write(self, value):
        self.text += value
        return len(value)

    def flush(self):
        pass


class _TTYInput:
    def isatty(self):
        return True


class TestSixelDetection(unittest.TestCase):
    def test_primary_da_parser_detects_sixel_parameter(self):
        self.assertTrue(sixel._parse_primary_da(b"\x1b[?62;1;4;6;22c"))
        self.assertFalse(sixel._parse_primary_da(b"\x1b[?62;1;6;22c"))
        self.assertIsNone(sixel._parse_primary_da(b"not-a-terminal-response"))

    def test_environment_override_enables_sixel(self):
        with patch.dict(os.environ, {"REFINDMGR_SIXEL": "1"}, clear=True):
            self.assertTrue(sixel.terminal_supports_sixel())

    def test_windows_terminal_session_is_recognized(self):
        with patch.dict(os.environ, {"TERM": "xterm-256color", "WT_SESSION": "uuid"}, clear=True):
            self.assertTrue(sixel.terminal_supports_sixel())

    def test_known_linux_sixel_terminal_is_recognized(self):
        with patch.dict(os.environ, {"TERM": "wezterm"}, clear=True):
            self.assertTrue(sixel.terminal_supports_sixel())

    def test_foot_is_not_assumed_to_support_sixel(self):
        with patch.dict(os.environ, {"TERM": "foot"}, clear=True), patch.object(
            sixel, "_query_terminal", return_value=None
        ):
            self.assertIsNone(sixel.terminal_supports_sixel())

    def test_unknown_xterm_is_not_rejected(self):
        with patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=True), patch.object(
            sixel, "_query_terminal", return_value=None
        ):
            self.assertIsNone(sixel.terminal_supports_sixel())

    def test_da1_without_parameter_four_is_inconclusive(self):
        with patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=True), patch.object(
            sixel, "_query_terminal", return_value=False
        ):
            self.assertIsNone(sixel.terminal_supports_sixel())

    def test_detection_reports_unknown_instead_of_unsupported(self):
        output = _TTYOutput()
        with patch.object(sixel.sys, "stdout", output), patch(
            "refindmgr.sixel.shutil.which", return_value="/usr/bin/img2sixel"
        ), patch.object(sixel, "terminal_supports_sixel", return_value=None):
            self.assertEqual(
                sixel.detection_status(),
                ("unknown", "dukungan Sixel tidak dapat dideteksi otomatis"),
            )

    def test_force_render_uses_img2sixel_after_user_confirmation(self):
        output = _TTYOutput()
        completed = SimpleNamespace(returncode=0, stderr=b"")
        with TemporaryDirectory() as tmp, patch.object(sixel.sys, "stdout", output), patch(
            "refindmgr.sixel.shutil.which", return_value="/usr/bin/img2sixel"
        ), patch("refindmgr.sixel.subprocess.run", return_value=completed) as run:
            image = Path(tmp) / "preview.png"
            image.write_bytes(b"png")
            shown, reason = sixel.show(image, width=280, force=True, column=73)
        self.assertTrue(shown)
        self.assertEqual(reason, "")
        self.assertIn("\x1b[73G", output.text)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], ["/usr/bin/img2sixel", "-w", "280", str(image)])


if __name__ == "__main__":
    unittest.main()
