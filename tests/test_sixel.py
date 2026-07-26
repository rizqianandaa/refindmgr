import io
import os
import sys
import unittest
from pathlib import Path
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

    def test_probe_result_controls_sixel_support(self):
        from refindmgr import terminal
        with patch.object(sixel.terminal_mod, "probe", return_value=terminal.TerminalCapabilities(sixel=True)):
            self.assertTrue(sixel.terminal_supports_sixel())
        with patch.object(sixel.terminal_mod, "probe", return_value=terminal.TerminalCapabilities(responded=True)):
            self.assertFalse(sixel.terminal_supports_sixel())
        with patch.object(sixel.terminal_mod, "probe", return_value=terminal.TerminalCapabilities()):
            self.assertIsNone(sixel.terminal_supports_sixel())

    def test_detection_status_delegates_to_preview_stack(self):
        ready = sixel.preview_mod.PreviewEngine("sixel")
        unavailable = sixel.preview_mod.PreviewEngine("none", "terminal tidak mendukung Sixel")
        with patch.object(sixel.preview_mod, "resolve", return_value=ready):
            self.assertEqual(sixel.detection_status(), ("ready", ""))
        with patch.object(sixel.preview_mod, "resolve", return_value=unavailable):
            self.assertEqual(
                sixel.detection_status(),
                ("unavailable", "terminal tidak mendukung Sixel"),
            )

    def test_force_render_keeps_legacy_sixel_api_working(self):
        output = _TTYOutput()
        from refindmgr import terminal
        caps = terminal.TerminalCapabilities(sixel=True, cell_pixels=(8, 19))
        engine = sixel.preview_mod.PreviewEngine("sixel", "", caps, renderer="/usr/bin/img2sixel")
        with patch("sys.stdout", output), \
             patch.object(sixel.terminal_mod, "probe", return_value=caps), \
             patch.object(sixel.preview_mod, "_build", return_value=engine), \
             patch.object(sixel.preview_mod, "render", return_value=(True, "")) as render:
            shown, reason = sixel.show(Path("preview.png"), width=280, force=True, column=73)
        self.assertTrue(shown)
        self.assertEqual(reason, "")
        self.assertIn("\x1b[73G", output.text)
        render.assert_called_once()
        self.assertEqual(render.call_args.kwargs["columns"], 35)


if __name__ == "__main__":
    unittest.main()
