import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]

# plugins.omarchy.org clips the card description to one 21px line with a fade
# (`white-space: nowrap`), and Omarchy's own widget picker elides it the same
# way. Roughly 50 characters survive, so the lead clause has to stand alone.
LISTING_LINE_BUDGET = 50


class PluginManifestContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))

    def test_bar_widget_entry_point_exists(self):
        self.assertEqual(self.manifest["schemaVersion"], 1)
        self.assertIn("bar-widget", self.manifest["kinds"])
        entry_point = self.manifest["entryPoints"]["barWidget"]
        self.assertTrue((ROOT / entry_point).is_file())

    def test_declares_exclusive_clock_provider_contract(self):
        metadata = self.manifest["barWidget"]
        self.assertFalse(metadata["allowMultiple"])
        self.assertEqual(metadata["defaultSection"], "center")
        self.assertIn("clock", metadata["semanticCapabilities"])

    def test_descriptions_land_their_point_on_the_visible_line(self):
        for field, text in (
            ("description", self.manifest["description"]),
            ("barWidget.description", self.manifest["barWidget"]["description"]),
        ):
            head = text[:LISTING_LINE_BUDGET]
            self.assertTrue(
                len(text) <= LISTING_LINE_BUDGET or any(mark in head for mark in ":;,"),
                f"{field} has no clause break in its first {LISTING_LINE_BUDGET} "
                f"characters, so the card shows a fragment: {head!r}",
            )


if __name__ == "__main__":
    unittest.main()
