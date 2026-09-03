import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


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


if __name__ == "__main__":
    unittest.main()
