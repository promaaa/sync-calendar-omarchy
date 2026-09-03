import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import urllib.error

ROOT = Path(__file__).resolve().parents[1]


def load_script(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fetch_events = load_script("fetch_events", "fetch-events.py")


class SafeIntParamTests(unittest.TestCase):
    def test_safe_int_param_valid_and_invalid_inputs(self):
        self.assertEqual(fetch_events.safe_int_param("5"), 5)
        self.assertEqual(fetch_events.safe_int_param("  10  "), 10)
        self.assertEqual(fetch_events.safe_int_param("2;COUNT=5"), 2)
        self.assertEqual(fetch_events.safe_int_param(42), 42)
        self.assertEqual(fetch_events.safe_int_param(None, default=1), 1)
        self.assertEqual(fetch_events.safe_int_param("invalid", default=3), 3)
        self.assertEqual(fetch_events.safe_int_param("", default=1), 1)
        self.assertEqual(fetch_events.safe_int_param(["not", "scalar"], default=7), 7)


class ParseDatetimeSubsecondsTests(unittest.TestCase):
    def test_parse_datetime_with_fractional_seconds(self):
        # 2026-08-16 14:30:00.123456 UTC
        all_day, parsed = fetch_events.parse_datetime_value("20260816T143000.123Z")
        self.assertFalse(all_day)
        expected_utc = fetch_events.datetime(2026, 8, 16, 14, 30, 0, tzinfo=fetch_events.timezone.utc)
        expected_local = expected_utc.astimezone().replace(tzinfo=None)
        self.assertEqual(parsed, expected_local)

        all_day_iso, parsed_iso = fetch_events.parse_datetime_value("2026-08-16T14:30:00.000Z")
        self.assertFalse(all_day_iso)
        self.assertEqual(parsed_iso, expected_local)


class FetchCalendarRetryTests(unittest.TestCase):
    def test_fetch_calendar_retries_transient_error_and_succeeds(self):
        cal_info = {"name": "iCloud Test", "url": "https://p100-caldav.icloud.com/published/2/abc"}
        start = fetch_events.datetime(2026, 8, 1)
        end = fetch_events.datetime(2026, 8, 31)

        ics_payload = b"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:icloud-1@example.com
SUMMARY:iCloud Sync Event
DTSTART:20260815T100000Z
DTEND:20260815T110000Z
END:VEVENT
END:VCALENDAR"""

        class FakeResponse:
            def __init__(self, data):
                self.stream = io.BytesIO(data)

            def read(self, size=-1):
                return self.stream.read(size)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        call_count = 0

        def fake_urlopen(req, timeout=12):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionResetError("Connection reset by peer")
            return FakeResponse(ics_payload)

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen), \
             mock.patch("time.sleep") as mock_sleep:
            res = fetch_events.fetch_calendar(cal_info, start, end)

        self.assertEqual(call_count, 2)
        mock_sleep.assert_called_once()
        self.assertEqual(res["status"], "ok")
        self.assertEqual(len(res["events"]), 1)
        self.assertEqual(res["events"][0]["title"], "iCloud Sync Event")


class DefensiveEventSortingAndSyncTests(unittest.TestCase):
    def test_sync_all_events_handles_none_titles_and_corrupted_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg_path = os.path.join(directory, "calendars.json")
            out_path = os.path.join(directory, "calendar-events.json")
            local_path = os.path.join(directory, "local-events.json")

            sample_cfg = [{"name": "Test Cal", "url": "https://example.com/test.ics", "enabled": True}]
            fetch_events.write_secure_json(cfg_path, sample_cfg)

            # Mock fetch_calendar_item to return events with None title / strange types
            mock_events = [
                {
                    "id": "evt_1",
                    "title": None,
                    "calendar": "Test Cal",
                    "start_dt": fetch_events.datetime(2026, 8, 20, 10, 0),
                    "end_dt": fetch_events.datetime(2026, 8, 20, 11, 0),
                    "date_key": "2026-08-20",
                    "all_day": False,
                    "color": "#4A90E2",
                    "location": None,
                },
                {
                    "id": "evt_2",
                    "title": "Alpha Meeting",
                    "calendar": "Test Cal",
                    "start_dt": fetch_events.datetime(2026, 8, 20, 9, 0),
                    "end_dt": fetch_events.datetime(2026, 8, 20, 10, 0),
                    "date_key": "2026-08-20",
                    "all_day": False,
                    "color": "#4A90E2",
                    "location": "Room 1",
                },
            ]

            with mock.patch.object(fetch_events, "CONFIG_PATH", cfg_path), \
                 mock.patch.object(fetch_events, "OUTPUT_PATH", out_path), \
                 mock.patch.object(fetch_events, "LOCAL_EVENTS_PATH", local_path), \
                 mock.patch.object(fetch_events, "STATE_DIR", directory), \
                 mock.patch.object(fetch_events, "fetch_calendar_item", return_value={"name": "Test Cal", "color": "#4A90E2", "events": mock_events, "status": "ok", "count": 2}):
                
                result = fetch_events.sync_all_events()
                self.assertEqual(result["status"], "success")
                self.assertEqual(result["totalEvents"], 2)

                saved = fetch_events.safe_load_json(out_path, max_bytes=fetch_events.MAX_OUTPUT_JSON_BYTES)
                day_events = saved["eventsByDate"]["2026-08-20"]
                self.assertEqual(len(day_events), 2)
                # Ensure Alpha Meeting at 09:00 sorted before 10:00 event with None title
                self.assertEqual(day_events[0]["title"], "Alpha Meeting")
                self.assertEqual(day_events[1]["title"], "(Untitled Event)")


class MainCrashShieldTests(unittest.TestCase):
    def test_main_exits_cleanly_on_unexpected_sync_exception(self):
        with mock.patch.object(fetch_events, "sync_all_events", side_effect=RuntimeError("Simulated critical failure")), \
             mock.patch("sys.argv", ["fetch-events.py"]), \
             self.assertRaises(SystemExit) as cm:
            fetch_events.main()
        # Clean exit code 0 prevents OS-level crash notifications
        self.assertEqual(cm.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
