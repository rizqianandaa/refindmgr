import unittest

from refindtm import catalog as catalog_mod


class TestCatalog(unittest.TestCase):
    def test_find_known_key(self):
        entry = catalog_mod.find("minimal")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.name, "rEFInd-minimal")
        self.assertTrue(entry.git_url.startswith("https://github.com/"))

    def test_find_unknown_key_returns_none(self):
        self.assertIsNone(catalog_mod.find("does-not-exist"))

    def test_all_entries_have_required_fields(self):
        for entry in catalog_mod.CATALOG:
            self.assertTrue(entry.key)
            self.assertTrue(entry.name)
            self.assertTrue(entry.git_url)
            self.assertTrue(entry.description)

    def test_keys_are_unique(self):
        keys = [entry.key for entry in catalog_mod.CATALOG]
        self.assertEqual(len(keys), len(set(keys)))


if __name__ == "__main__":
    unittest.main()
