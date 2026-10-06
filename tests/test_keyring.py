import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("fetch_events", ROOT / "fetch-events.py")
fetch_events = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetch_events)


class FakeKeyring:
    """In-memory stand-in for secret-tool: store, lookup, search, clear."""

    def __init__(self, working=True):
        self.items = {}
        self.working = working

    def __call__(self, args, value=None):
        if not self.working:
            return None
        cmd, rest = args[0], args[1:]
        if cmd == "store":
            rest = rest[2:]  # --label X
        attrs = tuple(zip(rest[::2], rest[1::2]))
        if cmd == "store":
            self.items[attrs] = value
            return ""
        if cmd == "lookup":
            return self.items.get(attrs)
        if cmd == "search":
            return "".join(f"attribute.secret-id = {dict(k)['secret-id']}\n" for k in self.items)
        if cmd == "clear":
            self.items = {k: v for k, v in self.items.items() if not set(attrs) <= set(k)}
            return ""


class KeyringTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.mkdtemp()
        self.config = os.path.join(directory, "calendars.json")
        self.auth = os.path.join(directory, "google-auth.json")
        for name, value in (("CONFIG_PATH", self.config), ("AUTH_FILE", self.auth)):
            patcher = mock.patch.object(fetch_events, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def use(self, keyring):
        patcher = mock.patch.object(fetch_events, "_secret_tool", keyring)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_saved_secrets_go_to_the_keyring_and_come_back(self):
        self.use(FakeKeyring())
        fetch_events.save_calendars([{"name": "Fastmail", "type": "jmap", "jmapToken": "tok-1"}])
        on_disk = json.load(open(self.config))
        self.assertEqual(on_disk[0]["jmapToken"], fetch_events.KEYRING_MARK)
        self.assertNotIn("tok-1", open(self.config).read())
        self.assertEqual(fetch_events.load_calendars()[0]["jmapToken"], "tok-1")

    def test_panel_round_trip_keeps_the_marker_and_removal_clears_the_secret(self):
        keyring = FakeKeyring()
        self.use(keyring)
        fetch_events.save_calendars([{"name": "A", "password": "p", "username": "u"}])
        # The panel reads the file (markers) and saves it back unchanged.
        fetch_events.save_calendars(json.load(open(self.config)))
        self.assertEqual(fetch_events.load_calendars()[0]["password"], "p")
        fetch_events.save_calendars([])
        self.assertEqual(keyring.items, {})

    def test_without_a_keyring_the_secret_stays_in_the_file(self):
        self.use(FakeKeyring(working=False))
        fetch_events.save_calendars([{"name": "A", "password": "p", "username": "u"}])
        self.assertEqual(json.load(open(self.config))[0]["password"], "p")
        self.assertEqual(fetch_events.load_calendars()[0]["password"], "p")

    def test_old_plaintext_config_and_google_auth_migrate(self):
        self.use(FakeKeyring())
        fetch_events.write_secure_json(self.config, [{"name": "A", "password": "p", "username": "u"}])
        fetch_events.write_secure_json(self.auth, {"client_id": "id", "client_secret": "cs", "refresh_token": "rt"})
        fetch_events.migrate_secrets_to_keyring()
        self.assertNotIn('"p"', open(self.config).read())
        auth = json.load(open(self.auth))
        self.assertEqual(auth["refresh_token"], fetch_events.KEYRING_MARK)
        revealed = fetch_events.reveal_secrets(auth, fetch_events.GOOGLE_SECRET_FIELDS, secret_id="google")
        self.assertEqual((revealed["client_secret"], revealed["refresh_token"]), ("cs", "rt"))


if __name__ == "__main__":
    unittest.main()
