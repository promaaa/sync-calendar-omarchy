import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]

# plugins.omarchy.org clips the card description to one 21px line with a fade
# (`white-space: nowrap`), and Omarchy's own widget picker elides it the same
# way. Roughly 50 characters survive, so the lead clause has to stand alone.
LISTING_LINE_BUDGET = 50

SHIPPED_SUFFIXES = {".qml", ".js", ".py", ".json", ".md"}
CONFLICT_MARKER = re.compile(r"^(<{7}|={7}|>{7})(?: |$)", re.MULTILINE)


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


class ShippedSourceContractTests(unittest.TestCase):
    def test_stdin_payloads_are_sent_on_a_single_line(self):
        # fetch-events.py reads one line from the still-open pipe, so an indented
        # JSON.stringify(x, null, 2) payload is truncated to "[" and never saved.
        panel = (ROOT / "Panel.qml").read_text(encoding="utf-8")
        self.assertNotRegex(panel, r"JSON\.stringify\([^)]*,\s*null\s*,")

    def test_a_single_line_config_payload_is_saved(self):
        import os, subprocess, tempfile
        with tempfile.TemporaryDirectory() as home:
            payload = json.dumps([{"name": "Personal", "type": "caldav"}]) + "\n"
            result = subprocess.run(
                ["python3", str(ROOT / "fetch-events.py"), "--save-config"],
                input=payload, capture_output=True, text=True, timeout=30,
                env={**os.environ, "HOME": home},
            )
            self.assertEqual(json.loads(result.stdout)["status"], "success", result.stdout)
            saved = json.loads((Path(home) / ".config/omarchy/calendars.json").read_text())
            self.assertEqual(saved[0]["name"], "Personal")

    def test_no_leftover_merge_conflict_markers(self):
        # One stray marker in Panel.qml shipped in v1.4.1 and v1.4.2: QML refused
        # the whole file, so the popup never opened while the bar still looked fine.
        for path in sorted(ROOT.rglob("*")):
            if path.suffix not in SHIPPED_SUFFIXES or ".git" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            match = CONFLICT_MARKER.search(text)
            if match:
                line = text.count("\n", 0, match.start()) + 1
                self.fail(f"{path.relative_to(ROOT)}:{line} has a merge conflict marker")


if __name__ == "__main__":
    unittest.main()
