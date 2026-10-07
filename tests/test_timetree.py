import importlib.util
import io
import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load_script(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fetch_events = load_script("fetch_events_timetree", "fetch-events.py")

CAL = {"name": "Work", "type": "timetree", "email": "a@example.com", "calendarId": "123"}


def utc_ms(y, m, d):
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp() * 1000)


def sync_page(events):
    body = json.dumps({"events": events, "since": 0, "chunk": False}).encode()
    resp = io.BytesIO(body)
    resp.__enter__ = lambda self=resp: self
    resp.__exit__ = lambda *a: None
    return resp


def fetch(events):
    window = (datetime(2026, 10, 1), datetime(2026, 11, 30))
    with mock.patch.object(fetch_events, "timetree_credentials", return_value=("a@example.com", "pw")), \
         mock.patch.object(fetch_events, "timetree_login", return_value=("sid", "csrf")), \
         mock.patch.object(fetch_events.urllib.request, "urlopen", return_value=sync_page(events)):
        return fetch_events.fetch_timetree_calendar(CAL, *window)


class TimeTreeAllDayTest(unittest.TestCase):
    def setUp(self):
        fetch_events.STATE_DIR = tempfile.mkdtemp(prefix="chronica-test-timetree-")

    def test_multi_day_all_day_event_includes_its_last_day(self):
        result = fetch([{
            "uuid": "multi", "title": "Reserve training", "all_day": True,
            "start_at": utc_ms(2026, 10, 27), "end_at": utc_ms(2026, 10, 29),
            "updated_at": 1,
        }])
        days = sorted(e["date_key"] for e in result["events"])
        self.assertEqual(days, ["2026-10-27", "2026-10-28", "2026-10-29"])

    def test_single_day_all_day_event_stays_one_day(self):
        result = fetch([{
            "uuid": "single", "title": "Leave", "all_day": True,
            "start_at": utc_ms(2026, 10, 6), "end_at": utc_ms(2026, 10, 6),
            "updated_at": 2,
        }])
        self.assertEqual([e["date_key"] for e in result["events"]], ["2026-10-06"])


class TimeTreeSecretTest(unittest.TestCase):
    def test_password_is_moved_to_the_keyring_and_read_back(self):
        store = {}

        def fake_secret_tool(args, value=None):
            key = tuple(args[args.index("secret-id"):])
            if args[0] == "store":
                store[key] = value
                return ""
            if args[0] == "lookup":
                return store.get(key)
            return None

        entry = dict(CAL, password="hunter2")
        with mock.patch.object(fetch_events, "_secret_tool", fake_secret_tool):
            fetch_events.stash_secrets(entry, fetch_events.CALENDAR_SECRET_FIELDS)
            self.assertEqual(entry["password"], fetch_events.KEYRING_MARK)
            revealed = fetch_events.reveal_secrets(entry, fetch_events.CALENDAR_SECRET_FIELDS)
        self.assertEqual(fetch_events.timetree_credentials(revealed), ("a@example.com", "hunter2"))

    def test_unrevealed_password_is_rejected(self):
        entry = dict(CAL, password=fetch_events.KEYRING_MARK)
        with self.assertRaises(ValueError):
            fetch_events.timetree_credentials(entry)


if __name__ == "__main__":
    unittest.main()
