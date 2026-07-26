"""Tests for the terminal capability probe and the layered preview backends."""
import base64
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from refindmgr import preview as preview_mod
from refindmgr import sixel as sixel_mod
from refindmgr import terminal as term_mod


def _caps(**kwargs) -> term_mod.TerminalCapabilities:
    caps = term_mod.TerminalCapabilities()
    for key, value in kwargs.items():
        setattr(caps, key, value)
    return caps


class TestProbeParsing(unittest.TestCase):
    """The probe reads the terminal's reply, never the environment.

    sudo's default env_reset deletes TERM_PROGRAM, VTE_VERSION, WT_SESSION,
    TMUX and STY, so any detection based on them is dead in the documented
    'sudo refindmgr' flow.
    """

    def test_kitty_ok_reply_enables_kitty_backend(self):
        response = b"\x1b_Gi=31;OK\x1b\\\x1b[?62;1;6;22c"
        caps = term_mod._interpret(response, term_mod.TerminalCapabilities())
        self.assertTrue(caps.kitty_graphics)
        self.assertFalse(caps.sixel)

    def test_da1_parameter_four_enables_sixel(self):
        caps = term_mod._interpret(b"\x1b[?62;1;4;6;9;22c", term_mod.TerminalCapabilities())
        self.assertTrue(caps.sixel)

    def test_da1_without_parameter_four_is_not_sixel(self):
        caps = term_mod._interpret(b"\x1b[?62;1;6;9;22c", term_mod.TerminalCapabilities())
        self.assertFalse(caps.sixel)
        self.assertTrue(caps.responded)

    def test_xtversion_identifies_iterm_capable_terminal(self):
        response = b"\x1bP>|WezTerm 20240203\x1b\\\x1b[?62;22c"
        caps = term_mod._interpret(response, term_mod.TerminalCapabilities())
        self.assertTrue(caps.iterm_images)
        self.assertIn("WezTerm", caps.name)

    def test_silent_terminal_reports_no_capability(self):
        caps = term_mod._interpret(b"", term_mod.TerminalCapabilities())
        self.assertFalse(caps.responded)
        self.assertFalse(caps.any_graphics)

    def test_probe_ends_with_da1_sentinel(self):
        # DA1 is answered by every terminal and arrives last, so it marks the
        # end of the reply instead of a guessed timeout that used to leak
        # stray escape bytes into the menu prompt.
        self.assertTrue(term_mod.PROBE.endswith(b"\x1b[c"))


class TestMultiplexerHandling(unittest.TestCase):
    def test_tmux_detected_from_term_when_sudo_stripped_the_env(self):
        with patch.dict("os.environ", {"TERM": "tmux-256color"}, clear=True):
            self.assertEqual(term_mod.detect_multiplexer(), "tmux")

    def test_screen_detected_from_term_when_sudo_stripped_the_env(self):
        with patch.dict("os.environ", {"TERM": "screen-256color"}, clear=True):
            self.assertEqual(term_mod.detect_multiplexer(), "screen")

    def test_screen_passthrough_doubles_every_escape(self):
        # The old screen branch omitted the doubling, so screen's DCS parser
        # terminated on the payload's own string terminator: the image was
        # truncated and the trailing ESC \ leaked as garbage.
        wrapped = term_mod.wrap_passthrough(b"\x1bPq#0;2\x1b\\", "screen")
        self.assertTrue(wrapped.startswith(b"\x1bP"))
        self.assertTrue(wrapped.endswith(b"\x1b\\"))
        self.assertIn(b"\x1b\x1bPq", wrapped)

    def test_tmux_passthrough_doubles_every_escape(self):
        wrapped = term_mod.wrap_passthrough(b"\x1bPq#0\x1b\\", "tmux")
        self.assertTrue(wrapped.startswith(b"\x1bPtmux;"))
        self.assertIn(b"\x1b\x1bPq#0", wrapped)

    def test_disabled_tmux_passthrough_falls_back_to_character_art(self):
        caps = _caps(kitty_graphics=True, multiplexer="tmux", passthrough_ok=False)
        with patch.object(preview_mod, "_chafa", return_value="/usr/bin/chafa"):
            engine = preview_mod.resolve(caps)
        self.assertEqual(engine.backend, "chafa")


class TestBackendSelection(unittest.TestCase):
    def test_prefers_kitty_over_everything_else(self):
        caps = _caps(kitty_graphics=True, sixel=True, iterm_images=True)
        self.assertEqual(preview_mod.resolve(caps).backend, "kitty")

    def test_falls_back_to_iterm_then_sixel(self):
        caps = _caps(iterm_images=True, sixel=True)
        self.assertEqual(preview_mod.resolve(caps).backend, "iterm")
        with patch.object(preview_mod, "_chafa", return_value="/usr/bin/chafa"):
            self.assertEqual(preview_mod.resolve(_caps(sixel=True)).backend, "sixel")

    def test_terminal_without_graphics_still_gets_chafa(self):
        # GNOME Terminal and Alacritty support none of the three protocols;
        # they must not be left with a plain text list.
        with patch.object(preview_mod, "_chafa", return_value="/usr/bin/chafa"):
            engine = preview_mod.resolve(_caps())
        self.assertEqual(engine.backend, "chafa")

    def test_no_backend_reports_an_actionable_reason(self):
        with patch.object(preview_mod, "_chafa", return_value=None), \
             patch.object(preview_mod, "_img2sixel", return_value=None):
            engine = preview_mod.resolve(_caps())
        self.assertFalse(engine.available)
        self.assertIn("chafa", engine.reason)

    def test_sixel_without_a_renderer_is_not_offered(self):
        with patch.object(preview_mod, "_chafa", return_value=None), \
             patch.object(preview_mod, "_img2sixel", return_value=None):
            engine = preview_mod.resolve(_caps(sixel=True))
        self.assertFalse(engine.available)

    def test_non_tty_never_emits_graphics(self):
        engine = preview_mod.resolve(_caps(is_tty=False))
        self.assertFalse(engine.available)

    def test_auto_means_probe_not_a_backend_name(self):
        # '--preview auto' is the CLI default; treating it as a backend name
        # made resolve() fail with "backend preview tidak dikenal: auto" and
        # silently disabled every preview.
        with patch.object(preview_mod, "_chafa", return_value="/usr/bin/chafa"):
            self.assertEqual(preview_mod.resolve(_caps(kitty_graphics=True), requested="auto").backend, "kitty")
            self.assertEqual(preview_mod.resolve(_caps(), requested="auto").backend, "chafa")
            self.assertEqual(preview_mod.resolve(_caps(kitty_graphics=True), requested="").backend, "kitty")

    def test_explicit_request_overrides_probing(self):
        engine = preview_mod.resolve(_caps(), requested="kitty")
        self.assertEqual(engine.backend, "kitty")
        self.assertFalse(preview_mod.resolve(_caps(kitty_graphics=True), requested="none").available)

    def test_legacy_sixel_env_switch_still_disables_previews(self):
        with patch.dict("os.environ", {"REFINDMGR_SIXEL": "0"}, clear=True):
            self.assertFalse(preview_mod.resolve(_caps(kitty_graphics=True)).available)


class TestZeroDependencyBackends(unittest.TestCase):
    """Kitty f=100 and the iTerm2 protocol need no external program."""

    def setUp(self):
        self.png = (
            Path(__file__).resolve().parent.parent
            / "refindmgr" / "assets" / "previews" / "lite.png"
        )

    def test_kitty_transmits_the_png_file_verbatim(self):
        engine = preview_mod.PreviewEngine("kitty", "", _caps(kitty_graphics=True))
        written = []
        with patch.object(preview_mod.term_mod, "write_stdout", written.append):
            ok, note = preview_mod.render(engine, self.png, columns=40, rows=12)
        self.assertTrue(ok, note)
        payload = b"".join(written)
        self.assertIn(b"\x1b_Ga=T,f=100", payload)
        self.assertIn(b"c=40,r=12", payload)
        self.assertTrue(payload.rstrip().endswith(b"\x1b\\"))
        chunks = b"".join(
            part.split(b";", 1)[1].replace(b"\x1b\\", b"")
            for part in payload.split(b"\x1b_G")[1:] if b";" in part
        )
        self.assertEqual(base64.b64decode(chunks), self.png.read_bytes())

    def test_kitty_chunks_stay_within_the_protocol_limit(self):
        engine = preview_mod.PreviewEngine("kitty", "", _caps(kitty_graphics=True))
        written = []
        with patch.object(preview_mod.term_mod, "write_stdout", written.append):
            preview_mod.render(engine, self.png, columns=40, rows=12)
        for part in b"".join(written).split(b"\x1b_G")[1:]:
            self.assertLessEqual(len(part.split(b";", 1)[1].replace(b"\x1b\\", b"")), 4096)

    def test_iterm_sends_osc_1337_with_the_raw_file(self):
        engine = preview_mod.PreviewEngine("iterm", "", _caps(iterm_images=True))
        written = []
        with patch.object(preview_mod.term_mod, "write_stdout", written.append):
            ok, note = preview_mod.render(engine, self.png, columns=40, rows=12)
        self.assertTrue(ok, note)
        payload = b"".join(written)
        self.assertIn(b"\x1b]1337;File=inline=1", payload)
        self.assertIn(b"width=40;height=12", payload)
        self.assertTrue(payload.endswith(b"\x07"))

    def test_kitty_refuses_a_non_png_without_a_png_sibling(self):
        engine = preview_mod.PreviewEngine("kitty", "", _caps(kitty_graphics=True))
        ok, note = preview_mod.render(engine, Path(__file__), columns=10, rows=4)
        self.assertFalse(ok)
        self.assertIn("PNG", note)

    def test_missing_file_is_reported_not_raised(self):
        engine = preview_mod.PreviewEngine("kitty", "", _caps(kitty_graphics=True))
        ok, note = preview_mod.render(engine, Path("/nonexistent/x.png"), columns=10, rows=4)
        self.assertFalse(ok)
        self.assertIn("tidak ditemukan", note)


class TestChafaInvocation(unittest.TestCase):
    def test_symbol_fallback_forces_truecolor_and_sextants(self):
        # chafa's own auto-detection reads variables sudo removes, so left alone
        # it degrades to a 16-colour palette: the coarse blocks users complain
        # about. The flags below are what make the fallback look acceptable.
        engine = preview_mod.PreviewEngine(
            "chafa", "", _caps(colors=16777216, rich_glyphs=True), renderer="/usr/bin/chafa"
        )
        with patch.object(preview_mod, "_run_renderer", return_value=(b"art", "")) as run, \
             patch.object(preview_mod.term_mod, "write_stdout"):
            preview_mod.render(engine, Path(__file__), columns=40, rows=12)
        command = run.call_args[0][0]
        self.assertEqual(command[command.index("--colors") + 1], "full")
        self.assertIn("sextant+block+space", command)
        self.assertEqual(command[command.index("-f") + 1], "symbols")
        self.assertEqual(command[command.index("--size") + 1], "40x12")

    def test_old_chafa_without_sextants_retries_with_blocks(self):
        engine = preview_mod.PreviewEngine(
            "chafa", "", _caps(colors=16777216, rich_glyphs=True), renderer="/usr/bin/chafa"
        )
        calls = []

        def fake(command):
            calls.append(command)
            if "sextant+block+space" in command:
                return None, "unrecognized symbol class"
            return b"art", ""

        with patch.object(preview_mod, "_run_renderer", side_effect=fake), \
             patch.object(preview_mod.term_mod, "write_stdout"):
            ok, _ = preview_mod.render(engine, Path(__file__), columns=40, rows=12)
        self.assertTrue(ok)
        self.assertEqual(len(calls), 2)
        self.assertIn("block+space", calls[1])

    def test_sixel_output_without_a_dcs_payload_is_not_success(self):
        engine = preview_mod.PreviewEngine("sixel", "", _caps(sixel=True), renderer="/usr/bin/chafa")
        with patch.object(preview_mod, "_run_renderer", return_value=(b"", "")):
            ok, note = preview_mod.render(engine, Path(__file__), columns=40, rows=12)
        self.assertFalse(ok)
        self.assertIn("Sixel", note)

    def test_sixel_payload_is_wrapped_for_tmux(self):
        engine = preview_mod.PreviewEngine(
            "sixel", "", _caps(sixel=True, multiplexer="tmux"), renderer="/usr/bin/chafa"
        )
        written = []
        with patch.object(preview_mod, "_run_renderer", return_value=(b"\x1bPq#0\x1b\\", "")), \
             patch.object(preview_mod.term_mod, "write_stdout", written.append):
            ok, _ = preview_mod.render(engine, Path(__file__), columns=40, rows=12)
        self.assertTrue(ok)
        self.assertTrue(written[0].startswith(b"\x1bPtmux;"))


class TestColumnAnchoredPlacement(unittest.TestCase):
    """A side-by-side list needs images pinned to a fixed screen column."""

    def setUp(self):
        self.png = (
            Path(__file__).resolve().parent.parent
            / "refindmgr" / "assets" / "previews" / "lite.png"
        )

    def _emit(self, engine, **kwargs):
        written = []
        with patch.object(preview_mod.term_mod, "write_stdout", written.append):
            ok, note = preview_mod.render(engine, self.png, columns=14, rows=4, **kwargs)
        return ok, note, b"".join(written)

    def test_anchor_saves_moves_restores_and_advances(self):
        engine = preview_mod.PreviewEngine("kitty", "", _caps(kitty_graphics=True))
        ok, note, payload = self._emit(engine, column=29)
        self.assertTrue(ok, note)
        self.assertTrue(payload.startswith(b"\x1b7\x1b[29G"))   # DECSC + column
        self.assertIn(b"\x1b8", payload)                        # DECRC
        # Exactly four line advances: the caller reserved four rows.
        self.assertTrue(payload.endswith(b"\r\n" * 4))

    def test_unanchored_render_emits_no_cursor_save(self):
        engine = preview_mod.PreviewEngine("kitty", "", _caps(kitty_graphics=True))
        _ok, _note, payload = self._emit(engine)
        self.assertNotIn(b"\x1b7", payload)
        self.assertNotIn(b"\x1b8", payload)

    def test_failed_render_does_not_advance_rows(self):
        engine = preview_mod.PreviewEngine("kitty", "", _caps(kitty_graphics=True))
        written = []
        with patch.object(preview_mod.term_mod, "write_stdout", written.append):
            ok, _ = preview_mod.render(engine, Path(__file__), columns=14, rows=4, column=29)
        payload = b"".join(written)
        self.assertFalse(ok)
        self.assertIn(b"\x1b8", payload)
        self.assertNotIn(b"\r\n", payload)

    def test_character_art_anchors_every_line(self):
        # chafa output is plain text: without re-anchoring, line 2 onwards
        # starts at column 1 and paints over the titles on the left.
        engine = preview_mod.PreviewEngine("chafa", "", _caps(), renderer="/usr/bin/chafa")
        art = b"AAA\nBBB\nCCC\n"
        with patch.object(preview_mod, "_run_renderer", return_value=(art, "")):
            _ok, _note, payload = self._emit(engine, column=29)
        self.assertEqual(payload.count(b"\x1b[29G"), 4)  # 1 anchor + 3 lines
        self.assertIn(b"\x1b[29GAAA\r\n", payload)
        self.assertIn(b"\x1b[29GCCC\r\n", payload)


class TestFixedFontConsole(unittest.TestCase):
    """A headless Ubuntu Server console draws from a 256-glyph font."""

    def test_linux_console_gets_ascii_and_safe_colours(self):
        symbols, colors, fill = preview_mod.chafa_profile(
            _caps(colors=16, rich_glyphs=False)
        )
        self.assertTrue(symbols.startswith("ascii"))
        self.assertEqual(fill, "ascii")
        # Plain '16' emits bright backgrounds (SGR 100-107) that the Linux
        # console renders inconsistently; 16/8 keeps backgrounds to the eight
        # it always supports.
        self.assertEqual(colors, "16/8")

    def test_capable_terminal_keeps_sextants_and_truecolor(self):
        symbols, colors, fill = preview_mod.chafa_profile(
            _caps(colors=16777216, rich_glyphs=True)
        )
        self.assertIn("sextant", symbols)
        self.assertEqual(colors, "full")
        self.assertEqual(fill, "block")

    def test_term_linux_is_classified_as_fixed_font(self):
        with patch.dict("os.environ", {"TERM": "linux"}, clear=True):
            self.assertFalse(term_mod.detect_rich_glyphs())
            self.assertEqual(term_mod.detect_color_depth(), 16)

    def test_vt102_console_is_detected_even_when_term_lies(self):
        # The Linux console answers DA1 as a VT102 and ignores XTVERSION.
        caps = term_mod.TerminalCapabilities(colors=16777216, rich_glyphs=True)
        caps = term_mod._interpret(b"\x1b[?6c", caps)
        self.assertFalse(caps.rich_glyphs)
        self.assertEqual(caps.colors, 16)

    def test_tmux_low_da1_is_not_mistaken_for_a_console(self):
        # tmux answers CSI ?1;2c but relays to a terminal with a real font.
        caps = term_mod.TerminalCapabilities(colors=16777216, rich_glyphs=True,
                                             multiplexer="tmux")
        caps = term_mod._interpret(b"\x1b[?1;2c", caps)
        self.assertTrue(caps.rich_glyphs)

    def test_named_terminal_is_not_mistaken_for_a_console(self):
        caps = term_mod.TerminalCapabilities(colors=16777216, rich_glyphs=True)
        caps = term_mod._interpret(b"\x1bP>|XTerm(390)\x1b\\\x1b[?6c", caps)
        self.assertTrue(caps.rich_glyphs)

    def test_truecolor_terminal_is_detected_from_colorterm(self):
        with patch.dict("os.environ", {"TERM": "xterm-256color",
                                       "COLORTERM": "truecolor"}, clear=True):
            self.assertEqual(term_mod.detect_color_depth(), 16777216)

    def test_user_can_force_unicode_on_a_console(self):
        # Some console fonts do carry block elements; let the user say so.
        symbols, _colors, _fill = preview_mod.chafa_profile(
            _caps(colors=16, rich_glyphs=False), "unicode"
        )
        self.assertIn("sextant", symbols)

    def test_user_can_force_ascii_anywhere(self):
        symbols, _colors, _fill = preview_mod.chafa_profile(
            _caps(colors=16777216, rich_glyphs=True), "ascii"
        )
        self.assertTrue(symbols.startswith("ascii"))

    def test_override_reaches_the_command_line(self):
        engine = preview_mod.PreviewEngine(
            "chafa", "", _caps(colors=16777216, rich_glyphs=True),
            renderer="/usr/bin/chafa", symbols="ascii",
        )
        with patch.object(preview_mod, "_run_renderer", return_value=(b"art", "")) as run, \
             patch.object(preview_mod.term_mod, "write_stdout"):
            preview_mod.render(engine, Path(__file__), columns=20, rows=5)
        command = run.call_args[0][0]
        self.assertIn("ascii+space", command)


class TestFramebufferEscapeHatch(unittest.TestCase):
    """The Linux console has no image protocol at all -- but it has pixels."""

    def test_not_offered_under_x11_or_wayland(self):
        # There the terminal is a normal emulator; seizing the framebuffer
        # would blank the user's desktop session.
        with patch.dict("os.environ", {"DISPLAY": ":0"}, clear=True), \
             patch.object(preview_mod.shutil, "which", return_value="/usr/bin/fim"):
            self.assertIsNone(preview_mod.framebuffer_viewer())
        with patch.dict("os.environ", {"WAYLAND_DISPLAY": "wayland-0"}, clear=True), \
             patch.object(preview_mod.shutil, "which", return_value="/usr/bin/fim"):
            self.assertIsNone(preview_mod.framebuffer_viewer())

    def test_not_offered_without_a_framebuffer_device(self):
        with patch.dict("os.environ", {}, clear=True), \
             patch.object(preview_mod.Path, "exists", lambda self: False), \
             patch.object(preview_mod.shutil, "which", return_value="/usr/bin/fim"):
            self.assertIsNone(preview_mod.framebuffer_viewer())

    def test_offered_on_a_console_with_a_viewer(self):
        with patch.dict("os.environ", {"TERM": "linux"}, clear=True), \
             patch.object(preview_mod.Path, "exists", lambda self: True), \
             patch.object(preview_mod.shutil, "which",
                          side_effect=lambda n: "/usr/bin/fim" if n == "fim" else None):
            viewer = preview_mod.framebuffer_viewer()
        self.assertIsNotNone(viewer)
        self.assertEqual(viewer[0], "/usr/bin/fim")

    def test_falls_back_through_the_viewer_list(self):
        with patch.dict("os.environ", {"TERM": "linux"}, clear=True), \
             patch.object(preview_mod.Path, "exists", lambda self: True), \
             patch.object(preview_mod.shutil, "which",
                          side_effect=lambda n: "/usr/bin/mpv" if n == "mpv" else None):
            viewer = preview_mod.framebuffer_viewer()
        self.assertEqual(viewer[0], "/usr/bin/mpv")
        self.assertIn("--vo=drm", viewer[1])

    def test_missing_viewer_reports_what_to_install(self):
        with patch.object(preview_mod, "framebuffer_viewer", return_value=None):
            ok, note = preview_mod.show_fullscreen(Path("/tmp/x.png"))
        self.assertFalse(ok)
        self.assertIn("fim", note)


class TestFramebufferBackend(unittest.TestCase):
    """Real pixels inline on a console, where no protocol exists."""

    def _console(self):
        return _caps(colors=16, rich_glyphs=False)

    def test_chosen_on_a_console_over_character_art(self):
        # This is the whole point: ASCII can never resemble a photograph.
        with patch.object(preview_mod.fb_mod, "available", return_value=True), \
             patch.object(preview_mod.fb_mod, "probe",
                          return_value=preview_mod.fb_mod.parse_geometry("1024x768x32")), \
             patch.object(preview_mod, "_chafa", return_value="/usr/bin/chafa"):
            engine = preview_mod.resolve(self._console())
        self.assertEqual(engine.backend, "framebuffer")
        self.assertTrue(engine.is_graphical)

    def test_never_preferred_over_a_real_terminal_protocol(self):
        caps = _caps(kitty_graphics=True, colors=16777216, rich_glyphs=True)
        with patch.object(preview_mod.fb_mod, "available", return_value=True):
            self.assertEqual(preview_mod.resolve(caps).backend, "kitty")

    def test_not_used_on_a_capable_terminal_even_without_graphics(self):
        # A GUI terminal with a real font gets character art, not the console
        # framebuffer, which is not what its user is looking at.
        caps = _caps(colors=16777216, rich_glyphs=True)
        with patch.object(preview_mod.fb_mod, "available", return_value=True), \
             patch.object(preview_mod, "_chafa", return_value="/usr/bin/chafa"):
            self.assertEqual(preview_mod.resolve(caps).backend, "chafa")

    def test_falls_back_to_chafa_when_the_device_is_unusable(self):
        with patch.object(preview_mod.fb_mod, "available", return_value=False), \
             patch.object(preview_mod, "_chafa", return_value="/usr/bin/chafa"):
            self.assertEqual(preview_mod.resolve(self._console()).backend, "chafa")

    def test_needs_the_cell_row_as_well_as_the_column(self):
        info = preview_mod.fb_mod.parse_geometry("1024x768x32")
        engine = preview_mod.PreviewEngine("framebuffer", "", self._console(), screen=info)
        ok, note = preview_mod.render(engine, Path(__file__), columns=10, rows=4, column=5)
        self.assertFalse(ok)
        self.assertIn("posisi sel", note)

    def test_describes_the_actual_screen(self):
        info = preview_mod.fb_mod.parse_geometry("1024x768x32")
        engine = preview_mod.PreviewEngine("framebuffer", "", self._console(), screen=info)
        self.assertIn("1024x768", preview_mod.describe(engine))
        self.assertIn("gambar asli", preview_mod.describe(engine))

    def test_drawn_regions_are_erased_on_clear(self):
        info = preview_mod.fb_mod.parse_geometry("64x64x32")
        engine = preview_mod.PreviewEngine("framebuffer", "", self._console(), screen=info)
        preview_mod._DRAWN_RECTS.append((0, 0, 8, 8))
        with patch.object(preview_mod.fb_mod, "fill_rects", return_value=True) as fill:
            preview_mod.clear_graphics(engine)
        fill.assert_called_once()
        self.assertEqual(preview_mod._DRAWN_RECTS, [])


class TestPixelAccurateSizing(unittest.TestCase):
    """Thumbnails are laid out in cells but encoded in pixels."""

    def setUp(self):
        self.png = (
            Path(__file__).resolve().parent.parent
            / "refindmgr" / "assets" / "previews" / "demon-slayer.png"
        )

    def test_reads_png_dimensions_without_an_image_library(self):
        self.assertEqual(preview_mod._image_pixels(self.png), (512, 288))

    def test_reads_jpeg_dimensions_without_an_image_library(self):
        self.assertEqual(
            preview_mod._image_pixels(self.png.with_suffix(".jpg")), (640, 360)
        )

    def test_unreadable_file_does_not_raise(self):
        self.assertIsNone(preview_mod._image_pixels(Path("/nonexistent.png")))

    def test_fit_preserves_aspect_ratio(self):
        self.assertEqual(preview_mod.fit_pixels((512, 288), (136, 76)), (135, 76))
        self.assertEqual(preview_mod.fit_pixels((512, 288), (100, 500)), (100, 56))

    def test_sixel_is_sized_from_the_real_cell_geometry(self):
        # chafa assumes a square 8x8 cell when its stdout is a pipe, so a
        # 4-row thumbnail came out 24px tall instead of 76px and squashed.
        engine = preview_mod.PreviewEngine(
            "sixel", "", _caps(sixel=True, cell_pixels=(8, 19)), renderer="/usr/bin/img2sixel"
        )
        with patch.object(preview_mod, "_img2sixel", return_value="/usr/bin/img2sixel"), \
             patch.object(preview_mod, "_run_renderer", return_value=(b"\x1bPq#0\x1b\\", "")) as run, \
             patch.object(preview_mod.term_mod, "write_stdout"):
            preview_mod.render(engine, self.png, columns=17, rows=4)
        command = run.call_args[0][0]
        self.assertEqual(command[command.index("-w") + 1], "135")
        self.assertEqual(command[command.index("-h") + 1], "76")

    def test_layout_uses_the_real_cell_aspect(self):
        # A tall 8x19 cell needs more columns per row than a square one.
        narrow = preview_mod.columns_per_row((8, 16))
        tall = preview_mod.columns_per_row((8, 19))
        self.assertGreater(tall, narrow)
        self.assertAlmostEqual(tall, (19 / 8) * (16 / 9), places=6)

    def test_cell_size_falls_back_when_the_terminal_is_silent(self):
        self.assertEqual(_caps().cell, term_mod.DEFAULT_CELL_PIXELS)
        self.assertEqual(_caps(cell_pixels=(10, 22)).cell, (10, 22))

    def test_probe_asks_for_the_cell_size(self):
        self.assertIn(b"\x1b[16t", term_mod.PROBE)

    def test_cell_size_reply_is_parsed(self):
        caps = term_mod._interpret(b"\x1b[6;19;8t\x1b[?62;4;22c", term_mod.TerminalCapabilities())
        self.assertEqual(caps.cell_pixels, (8, 19))

    def test_absurd_cell_size_reply_is_ignored(self):
        caps = term_mod._interpret(b"\x1b[6;0;0t\x1b[?62;c", term_mod.TerminalCapabilities())
        self.assertIsNone(caps.cell_pixels)


class TestPreviewGeometry(unittest.TestCase):
    def test_box_keeps_the_preview_inside_the_terminal(self):
        columns, rows = preview_mod.preview_box(80, 24)
        self.assertLessEqual(columns, 74)
        self.assertLessEqual(rows, 14)
        self.assertGreaterEqual(rows, 6)

    def test_box_survives_a_tiny_terminal(self):
        columns, rows = preview_mod.preview_box(20, 10)
        self.assertGreaterEqual(columns, 20)
        self.assertGreaterEqual(rows, 6)


class TestSixelCompatibilityShim(unittest.TestCase):
    def test_env_override_still_forces_support(self):
        with patch.dict("os.environ", {"REFINDMGR_SIXEL": "1"}, clear=True):
            self.assertTrue(sixel_mod.terminal_supports_sixel())
        with patch.dict("os.environ", {"REFINDMGR_SIXEL": "0"}, clear=True):
            self.assertFalse(sixel_mod.terminal_supports_sixel())

    def test_primary_da_parser_is_still_exposed(self):
        self.assertTrue(sixel_mod._parse_primary_da(b"\x1b[?62;4;22c"))
        self.assertFalse(sixel_mod._parse_primary_da(b"\x1b[?62;22c"))
        self.assertIsNone(sixel_mod._parse_primary_da(b""))

    def test_availability_wrapper_reports_a_reason(self):
        with patch.object(preview_mod, "resolve",
                          return_value=preview_mod.PreviewEngine("none", "alasan")):
            ok, reason = sixel_mod.availability()
        self.assertFalse(ok)
        self.assertEqual(reason, "alasan")


if __name__ == "__main__":
    unittest.main()
