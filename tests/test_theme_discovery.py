import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from refindmgr import themes


class TestGenericVariantDiscovery(unittest.TestCase):
    def test_public_github_inputs_are_anonymous_https(self):
        expected = "https://github.com/catppuccin/refind.git"
        self.assertEqual(themes._public_git_source("github.com/catppuccin/refind"), expected)
        self.assertEqual(themes._public_git_source("git@github.com:catppuccin/refind.git"), expected)
        self.assertEqual(themes._public_git_source("ssh://git@github.com/catppuccin/refind"), expected)

    def test_catppuccin_style_root_configs(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "icons").mkdir()
            (root / "icons" / "os.png").write_bytes(b"png")
            for flavour in ("latte", "frappe", "macchiato", "mocha"):
                (root / f"{flavour}.png").write_bytes(b"png")
                (root / f"{flavour}.conf").write_text(
                    f"banner {flavour}.png\nicons_dir icons\nhideui badges\n"
                )
            variants = themes.discover_variants(root)
            self.assertEqual({v.key for v in variants}, {"latte", "frappe", "macchiato", "mocha"})

    def test_digital_void_style_background_variants(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "icons").mkdir()
            (root / "theme.conf").write_text(
                "banner themes/rEFInd-digital-void/background.png\nicons_dir themes/rEFInd-digital-void/icons\n"
            )
            for colour in ("green", "red", "blue"):
                (root / f"background.{colour}.png").write_bytes(b"png")
            variants = themes.discover_variants(root)
            self.assertEqual({v.key for v in variants}, {"green", "red", "blue"})

    def test_sublime_prefixed_paths_are_rewritten_and_validated(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "refind-sublime"
            source.mkdir()
            (source / "icons").mkdir()
            (source / "background.png").write_bytes(b"png")
            (source / "selection_big.png").write_bytes(b"png")
            (source / "selection_small.png").write_bytes(b"png")
            (source / "theme.conf").write_text(
                "banner refind-sublime/background.png\n"
                "icons_dir refind-sublime/icons\n"
                "selection_big refind-sublime/selection_big.png\n"
                "selection_small refind-sublime/selection_small.png\n"
            )
            refind = base / "EFI" / "refind"
            refind.mkdir(parents=True)
            (refind / "refind.conf").write_text("timeout 5\n")
            installed = themes.install_theme(refind, str(source))
            conf = (refind / "themes" / installed / "theme.conf").read_text()
            self.assertIn("banner themes/refind-sublime/background.png", conf)
            self.assertIn("icons_dir themes/refind-sublime/icons", conf)

    def test_realistic_catppuccin_archive_wrapper_and_paths(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            wrapper = base / "refind-main"
            assets = wrapper / "assets" / "mocha"
            (assets / "icons").mkdir(parents=True)
            for name in ("background.png", "selection_big.png", "selection_small.png"):
                (assets / name).write_bytes(b"png")
            (wrapper / "mocha.conf").write_text(
                "icons_dir themes/catppuccin/assets/mocha/icons\n"
                "banner themes/catppuccin/assets/mocha/background.png\n"
                "selection_big themes/catppuccin/assets/mocha/selection_big.png\n"
                "selection_small themes/catppuccin/assets/mocha/selection_small.png\n"
            )
            refind = base / "EFI" / "refind"
            refind.mkdir(parents=True)
            (refind / "refind.conf").write_text("timeout 5\n")
            self.assertEqual(
                themes.install_theme(refind, str(wrapper), name="catppuccin"),
                "catppuccin",
            )
            conf = (refind / "themes" / "catppuccin" / "theme.conf").read_text()
            self.assertIn("banner themes/catppuccin/assets/mocha/background.png", conf)

    def test_boot_directives_are_sanitized_not_silently_applied(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "sublime"
            source.mkdir()
            (source / "background.png").write_bytes(b"png")
            (source / "theme.conf").write_text(
                "banner sublime/background.png\n"
                "scan_all_linux_kernels false\n"
                'default_selection "Arch"\n'
            )
            refind = base / "refind"
            refind.mkdir()
            (refind / "refind.conf").write_text("timeout 5\n")
            themes.install_theme(refind, str(source))
            conf = (refind / "themes" / "sublime" / "theme.conf").read_text()
            self.assertIn("# refindmgr-sanitized: scan_all_linux_kernels false", conf)
            self.assertIn('# refindmgr-sanitized: default_selection "Arch"', conf)

    def test_multiple_variants_require_selection_non_interactively(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "asset.png").write_bytes(b"png")
            for name in ("light", "dark"):
                (root / f"{name}.conf").write_text("banner asset.png\nhideui badges\n")
            refind = root / "refind"
            refind.mkdir()
            (refind / "refind.conf").write_text("timeout 5\n")
            with self.assertRaises(themes.ThemeError):
                themes.install_theme(refind, str(root), name="multi")
            self.assertEqual(themes.install_theme(refind, str(root), name="multi", variant="dark"), "multi")


if __name__ == "__main__":
    unittest.main()
