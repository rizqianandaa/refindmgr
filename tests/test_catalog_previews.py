import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from refindmgr import catalog
from refindmgr import cli
from refindmgr import preview as preview_mod


class TestBundledCatalogPreviews(unittest.TestCase):
    def test_every_catalog_entry_ships_a_png_preview(self):
        # PNG is required because the Kitty protocol's f=100 direct transfer
        # accepts PNG only -- that backend is what lets previews work with no
        # external image tooling installed.
        for entry in catalog.CATALOG:
            with self.subTest(entry=entry.key):
                image = cli._catalog_preview_path(entry)
                self.assertIsNotNone(image)
                self.assertEqual(image.suffix, ".png")
                self.assertLess(image.stat().st_size, 100 * 1024)

    def test_jpeg_copies_remain_for_the_other_backends(self):
        base = Path(cli.__file__).resolve().parent / "assets" / "previews"
        for entry in catalog.CATALOG:
            with self.subTest(entry=entry.key):
                self.assertTrue((base / f"{entry.key}.jpg").is_file())

    def test_preview_lookup_does_not_clone_or_download(self):
        entry = catalog.CATALOG[0]
        with patch("refindmgr.themes.prepare_theme_source") as prepare:
            image = cli._catalog_preview_path(entry)
        self.assertTrue(image.is_file())
        prepare.assert_not_called()

    def test_preview_engine_is_probed_once_per_interactive_session(self):
        args = SimpleNamespace(preview="auto")
        engine = preview_mod.PreviewEngine("kitty", "")
        with patch.object(cli.preview_mod, "resolve", return_value=engine) as resolve:
            self.assertIs(cli._cached_preview_engine(args), engine)
            self.assertIs(cli._cached_preview_engine(args), engine)
        resolve.assert_called_once()

    def test_cached_sixel_status_still_reports_readiness(self):
        args = SimpleNamespace(preview="auto")
        with patch.object(cli.preview_mod, "resolve",
                          return_value=preview_mod.PreviewEngine("kitty", "")):
            self.assertEqual(cli._cached_sixel_status(args), ("ready", ""))
        args = SimpleNamespace(preview="auto")
        with patch.object(cli.preview_mod, "resolve",
                          return_value=preview_mod.PreviewEngine("none", "tidak ada backend")):
            self.assertEqual(cli._cached_sixel_status(args), ("unavailable", "tidak ada backend"))

    def test_titles_are_numbered_for_every_entry(self):
        titles = cli._catalog_titles()
        self.assertEqual(len(titles), len(catalog.CATALOG))
        self.assertTrue(titles[0].strip().startswith("1."))
        self.assertIn(catalog.CATALOG[0].name, titles[0])

    def test_grid_shows_every_entry_on_one_screen(self):
        # Comparing themes is the point of the catalog, so all of them must be
        # visible at once instead of paged one at a time.
        widest = max(len(t) for t in cli._catalog_titles())
        g = preview_mod.grid_layout(113, 37, len(catalog.CATALOG), widest, cell=(8, 19))
        self.assertLessEqual(g.grid_rows * g.pitch, 37 - 3)
        self.assertLessEqual(g.grid_columns * g.cell_width, 113)
        self.assertGreaterEqual(g.grid_columns * g.grid_rows, len(catalog.CATALOG))

    def test_wide_terminal_uses_extra_columns_for_bigger_thumbnails(self):
        # A single column wastes a wide terminal: the height budget divided by
        # eight is what caps the thumbnail size.
        wide = preview_mod.grid_layout(113, 37, 8, 24, cell=(8, 19))
        narrow = preview_mod.grid_layout(60, 37, 8, 24, cell=(8, 19))
        self.assertEqual(wide.grid_columns, 2)
        self.assertEqual(narrow.grid_columns, 1)
        self.assertGreater(wide.area, narrow.area)

    def test_more_columns_are_only_used_when_they_help(self):
        # Splitting a narrow terminal would shrink each cell, not grow it.
        g = preview_mod.grid_layout(60, 40, 8, 24, cell=(8, 19))
        self.assertEqual(g.grid_columns, 1)

    def test_grid_positions_do_not_overlap(self):
        g = preview_mod.grid_layout(113, 37, 8, 24, cell=(8, 19))
        seen = set()
        for index in range(8):
            row, column = g.position(index)
            self.assertNotIn((row, column), seen)
            seen.add((row, column))
            self.assertLessEqual(column + g.cell_width, 113 + g.cell_width)
        self.assertEqual(len(seen), 8)

    def test_narrow_terminal_falls_back_to_plain_titles(self):
        self.assertIsNone(preview_mod.grid_layout(30, 40, 8, 24))

    def test_thumbnail_height_uses_the_whole_budget(self):
        # Reserving a gap up front cost every thumbnail a row even when there
        # was room for both, which is what made the previews look tiny.
        g = preview_mod.grid_layout(113, 37, 8, 24, cell=(8, 19))
        self.assertGreaterEqual(g.box_rows, 6)

    def test_thumbnails_keep_a_landscape_aspect(self):
        g = preview_mod.grid_layout(113, 37, 8, 24, cell=(8, 19))
        # A cell is about 2.4x taller than wide, so a 16:9 image needs roughly
        # 4x as many columns as rows.
        self.assertGreater(g.box_columns, g.box_rows * 3)

    def test_loading_label_was_removed(self):
        source = Path(cli.__file__).read_text()
        self.assertNotIn("Memuat preview", source)

    def test_catalog_has_no_sixel_confirmation_prompt(self):
        source = Path(cli.__file__).read_text()
        self.assertNotIn("Coba tampilkan preview Sixel?", source)

    def test_menu_no_longer_stacks_one_preview_per_entry(self):
        # Eight entries at seven reserved rows each needed 56 rows, so every
        # image had scrolled away before the prompt appeared on a 24-row
        # terminal. The menu now shows one preview at a time.
        source = Path(cli.__file__).read_text()
        self.assertNotIn("_CATALOG_PREVIEW_ROWS", source)
        self.assertNotIn("_catalog_preview_column", source)

    def test_preview_box_fits_a_standard_terminal(self):
        columns, rows = preview_mod.preview_box(80, 24)
        self.assertLess(columns, 80)
        self.assertLess(rows, 24)


if __name__ == "__main__":
    unittest.main()
