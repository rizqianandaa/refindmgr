import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from refindmgr import catalog
from refindmgr import cli


class TestBundledCatalogPreviews(unittest.TestCase):
    def test_every_catalog_entry_has_small_local_jpeg(self):
        for entry in catalog.CATALOG:
            with self.subTest(entry=entry.key):
                image = cli._catalog_preview_path(entry)
                self.assertIsNotNone(image)
                self.assertEqual(image.suffix, ".jpg")
                self.assertLess(image.stat().st_size, 100 * 1024)

    def test_preview_lookup_does_not_clone_or_download(self):
        entry = catalog.CATALOG[0]
        with patch("refindmgr.themes.prepare_theme_source") as prepare:
            image = cli._catalog_preview_path(entry)
        self.assertTrue(image.is_file())
        prepare.assert_not_called()

    def test_wide_terminal_aligns_every_preview_column(self):
        first_title = "  1. rEFInd-lite"
        second_title = "  2. rEFInd Demon Slayer"
        with patch("refindmgr.cli.shutil.get_terminal_size", return_value=SimpleNamespace(columns=100)):
            first_column = cli._catalog_preview_column(first_title, cli._CATALOG_PREVIEW_WIDTH)
            second_column = cli._catalog_preview_column(second_title, cli._CATALOG_PREVIEW_WIDTH)
        self.assertIsNotNone(first_column)
        self.assertEqual(first_column, second_column)
        self.assertGreater(first_column, len(second_title))

    def test_narrow_terminal_falls_back_below(self):
        title = "  2. rEFInd Demon Slayer"
        with patch("refindmgr.cli.shutil.get_terminal_size", return_value=SimpleNamespace(columns=45)):
            column = cli._catalog_preview_column(title, cli._CATALOG_PREVIEW_WIDTH)
        self.assertIsNone(column)

    def test_loading_label_was_removed(self):
        source = Path(cli.__file__).read_text()
        self.assertNotIn("Memuat preview", source)


if __name__ == "__main__":
    unittest.main()
