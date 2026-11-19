import importlib.util
import io
import json
import os
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load_script(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The wizard speaks the desktop's language, and module-level strings such as
# GOOGLE_READONLY_HINT are built at import; the assertions below are English.
os.environ["LC_ALL"] = "C"

setup = load_script("chronica_setup", "setup.py")
fetch_events = setup.fe
fetch_events._secret_tool = lambda args, value=None: None
# The backend speaks the desktop's language; the assertions below are English.
fetch_events._ui_lang = lambda: "en"
setup.ga.backend._ui_lang = lambda: "en"

from test_caldav import FakeResponse  # noqa: E402


class PickTests(unittest.TestCase):
    def test_enter_takes_everything(self):
        self.assertEqual(setup.parse_pick("", 3), [0, 1, 2])

    def test_numbers_are_one_based_and_deduplicated(self):
        self.assertEqual(setup.parse_pick("3, 1,3", 3), [0, 2])

    def test_out_of_range_or_junk_is_refused(self):
        self.assertIsNone(setup.parse_pick("4", 3))
        self.assertIsNone(setup.parse_pick("0", 3))
        self.assertIsNone(setup.parse_pick("two", 3))


class AddEntriesTests(unittest.TestCase):
    def test_sample_entry_is_replaced_and_names_stay_unique(self):
        config = [
            {"name": "Personal Calendar", "url": "", "color": "#4A90E2", "enabled": True},
            {"name": "Work", "url": "https://example.com/a.ics"},
        ]
        result = setup.add_entries(config, [{"name": "Work", "type": "local"}, {"name": "work", "type": "local"}])
        self.assertEqual([c["name"] for c in result], ["Work", "Work (2)", "work (3)"])
        self.assertTrue(all(c.get("enabled") for c in result[1:]))

    def test_an_entry_with_credentials_is_never_treated_as_the_sample(self):
        entry = {"name": "Personal Calendar", "url": "", "caldavUrl": "https://x/", "username": "u", "password": "p"}
        self.assertEqual(len(setup.add_entries([entry], [])), 1)


class GoogleApiTests(unittest.TestCase):
    def test_list_follows_every_page(self):
        pages = [{"items": [1, 2], "nextPageToken": "t2"}, {"items": [3]}]
        urls = []

        def fake_urlopen(req, timeout=None):
            urls.append(req.full_url)
            return FakeResponse(json.dumps(pages.pop(0)))

        with mock.patch.object(fetch_events.urllib.request, "urlopen", fake_urlopen):
            items = fetch_events.google_list("https://g/list", {"a": "b"}, "tok")
        self.assertEqual(items, [1, 2, 3])
        self.assertIn("pageToken=t2", urls[1])

    def test_only_scope_403_asks_to_reconnect(self):
        def refuse(reason, message):
            body = io.BytesIO(json.dumps({"error": {"errors": [{"reason": reason}], "message": message}}).encode())
            err = fetch_events.urllib.error.HTTPError("https://g", 403, "Forbidden", {}, body)
            return mock.patch.object(fetch_events.urllib.request, "urlopen", mock.Mock(side_effect=err))

        with refuse("insufficientPermissions", "Request had insufficient authentication scopes."):
            with self.assertRaisesRegex(ValueError, "Reconnect Google"):
                fetch_events.google_request("POST", "https://g", "tok", {})
        with refuse("rateLimitExceeded", "Rate Limit Exceeded"):
            with self.assertRaisesRegex(ValueError, "Rate Limit Exceeded"):
                fetch_events.google_request("POST", "https://g", "tok", {})


class WizardFlowTests(unittest.TestCase):
    """Each provider end to end, with the network, browser and keyboard faked."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.config = os.path.join(self.dir.name, "calendars.json")
        for obj, attr, value in [
            (fetch_events, "CONFIG_PATH", self.config),
            (setup.webbrowser, "open", lambda url: True),
            (fetch_events, "sync_all_events", lambda: {}),
            (fetch_events, "sync_in_child", lambda: {}),
        ]:
            patcher = mock.patch.object(obj, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self.dir.cleanup()

    def run_wizard(self, argv, answers, secrets=()):
        answers, secrets = list(answers), list(secrets)
        out = io.StringIO()
        with mock.patch.object(setup.sys, "argv", ["setup.py"] + argv), \
                mock.patch("builtins.input", lambda prompt="": answers.pop(0)), \
                mock.patch.object(setup.getpass, "getpass", lambda prompt="": secrets.pop(0)), \
                redirect_stdout(out):
            code = setup.main()
        return code, out.getvalue()

    def saved(self):
        with open(self.config) as f:
            return json.load(f)

    def test_menu_number_picks_the_provider(self):
        code, _ = self.run_wizard([], ["6", "Offline"])
        self.assertEqual(code, 0)
        self.assertEqual(self.saved(), [{"name": "Offline", "type": "local", "color": "#a6e3a1", "enabled": True}])

    def test_google_uses_the_downloaded_client_and_adds_picked_calendars(self):
        items = [
            {"id": "me@gmail.com", "summary": "Me", "primary": True, "backgroundColor": "#123456"},
            {"id": "team@group.calendar.google.com", "summary": "Team", "accessRole": "reader"},
        ]
        with mock.patch.object(setup.ga, "load_saved_auth", lambda: {}), \
                mock.patch.object(setup.ga, "find_downloaded_client", lambda: ("cid", "csecret", "client_secret_x.json")), \
                mock.patch.object(setup.ga, "authorize") as authorize, \
                mock.patch.object(setup, "google_calendars", lambda: items):
            code, _ = self.run_wizard(["google"], ["", "2"])
        self.assertEqual(code, 0)
        authorize.assert_called_once_with("cid", "csecret")
        self.assertEqual([(c["name"], c["googleCalendarId"]) for c in self.saved()],
                         [("Team", "team@group.calendar.google.com")])

    def test_google_waits_for_the_download_then_finds_it(self):
        downloads = iter([None, None, ("cid", "s", "client_secret_y.json")])
        with mock.patch.object(setup.ga, "load_saved_auth", lambda: {}), \
                mock.patch.object(setup.ga, "find_downloaded_client", lambda: next(downloads)), \
                mock.patch.object(setup.ga, "authorize") as authorize, \
                mock.patch.object(setup, "google_calendars", lambda: [{"id": "a", "summary": "A"}]):
            code, out = self.run_wizard(["google"], ["", "", ""])
        self.assertEqual(code, 0)
        self.assertIn("No client_secret_*.json in ~/Downloads yet", out)
        authorize.assert_called_once_with("cid", "s")

    def test_google_listing_failure_points_at_the_api_switch(self):
        def boom():
            raise OSError("HTTP Error 403")
        with mock.patch.object(setup.ga, "load_saved_auth", lambda: {"client_id": "c", "client_secret": "s"}), \
                mock.patch.object(setup.ga, "authorize"), \
                mock.patch.object(setup, "google_calendars", boom):
            code, out = self.run_wizard(["google"], ["y"])
        self.assertEqual(code, 1)
        self.assertIn("Google Calendar API enabled", out)
        self.assertFalse(os.path.exists(self.config))

    def test_reconnect_reuses_the_saved_app_and_adds_nothing(self):
        with mock.patch.object(setup.ga, "load_saved_auth", lambda: {"client_id": "c", "client_secret": "s"}), \
                mock.patch.object(setup.ga, "authorize") as authorize:
            code, out = self.run_wizard(["reconnect"], [])
        self.assertEqual(code, 0)
        authorize.assert_called_once_with("c", "s")
        self.assertFalse(os.path.exists(self.config))

    def test_icloud_discovers_from_apple_and_stores_the_login(self):
        seen = {}

        def discover(probe):
            seen.update(probe)
            return [{"name": "Home", "caldavUrl": "https://p01-caldav.icloud.com/1/calendars/home/"},
                    {"name": "Work", "caldavUrl": "https://p01-caldav.icloud.com/1/calendars/work/"}]
        with mock.patch.object(fetch_events, "caldav_discover", discover):
            code, _ = self.run_wizard(["icloud"], ["me@icloud.com", "1"], ["abcd-efgh-ijkl-mnop"])
        self.assertEqual(code, 0)
        self.assertEqual(seen["caldavUrl"], fetch_events.CALDAV_DEFAULT_HOST)
        [entry] = self.saved()
        self.assertEqual((entry["name"], entry["username"], entry["password"]),
                         ("Home", "me@icloud.com", "abcd-efgh-ijkl-mnop"))
        self.assertEqual(os.stat(self.config).st_mode & 0o777, 0o600)

    def test_caldav_bare_host_becomes_the_nextcloud_dav_root(self):
        seen = {}
        with mock.patch.object(fetch_events, "caldav_discover",
                               lambda probe: seen.update(probe) or [{"name": "", "caldavUrl": "https://cloud.example.com/remote.php/dav/calendars/me/personal/"}]):
            code, _ = self.run_wizard(["caldav"], ["cloud.example.com", "", "me", ""], ["pw"])
        self.assertEqual(code, 0)
        self.assertEqual(seen["caldavUrl"], "https://cloud.example.com/remote.php/dav/")
        self.assertEqual(self.saved()[0]["name"], "cloud.example.com")

    def test_rejected_jmap_token_saves_nothing(self):
        with mock.patch.object(fetch_events, "fetch_jmap_calendar", lambda *a: {"status": "auth_failed (401)"}):
            code, out = self.run_wizard(["jmap"], ["", ""], ["bad"])
        self.assertEqual(code, 1)
        self.assertIn("auth_failed (401)", out)
        self.assertFalse(os.path.exists(self.config))

    def test_working_jmap_token_is_saved(self):
        with mock.patch.object(fetch_events, "fetch_jmap_calendar", lambda *a: {"status": "ok", "count": 3}):
            code, _ = self.run_wizard(["jmap"], ["", ""], ["fmu1-good"])
        self.assertEqual(code, 0)
        self.assertEqual(self.saved()[0]["name"], "Fastmail")

    def test_broken_ics_link_saves_nothing(self):
        with mock.patch.object(fetch_events, "fetch_calendar", lambda *a: {"status": "error: HTTP Error 404"}):
            code, out = self.run_wizard(["ics"], ["https://example.com/x.ics", ""])
        self.assertEqual(code, 1)
        self.assertIn("404", out)
        self.assertFalse(os.path.exists(self.config))

    def test_ctrl_c_cancels_cleanly(self):
        def interrupt(prompt=""):
            raise KeyboardInterrupt
        out = io.StringIO()
        with mock.patch.object(setup.sys, "argv", ["setup.py", "ics"]), \
                mock.patch("builtins.input", interrupt), redirect_stdout(out):
            self.assertEqual(setup.main(), 1)
        self.assertIn("no calendars were added", out.getvalue())


if __name__ == "__main__":
    unittest.main()
