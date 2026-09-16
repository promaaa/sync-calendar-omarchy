import importlib.util
import io
import json
import os
from pathlib import Path
from unittest import mock
import stat
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def load_script(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fetch_events = load_script("fetch_events", "fetch-events.py")
google_auth = load_script("google_auth", "google-auth.py")


class SecureJsonMixin:
    module = None

    def test_secure_json_round_trip_uses_owner_only_regular_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "state.json")
            self.module.write_secure_json(path, {"token": "secret"})
            self.assertEqual(self.module.safe_load_json(path), {"token": "secret"})
            file_stat = os.stat(path, follow_symlinks=False)
            self.assertTrue(stat.S_ISREG(file_stat.st_mode))
            self.assertEqual(stat.S_IMODE(file_stat.st_mode), 0o600)
            self.assertEqual(os.listdir(directory), ["state.json"])

    def test_secure_json_read_rejects_symlink_and_oversize_file(self):
        with tempfile.TemporaryDirectory() as directory:
            target = os.path.join(directory, "target.json")
            link = os.path.join(directory, "link.json")
            with open(target, "w", encoding="utf-8") as stream:
                json.dump({"secret": True}, stream)
            os.symlink(target, link)
            with self.assertRaises(OSError):
                self.module.safe_load_json(link)
            with self.assertRaises(ValueError):
                self.module.safe_load_json(target, max_bytes=2)

    def test_secure_json_write_refuses_replaceable_non_file(self):
        with tempfile.TemporaryDirectory() as directory:
            target = os.path.join(directory, "target.json")
            link = os.path.join(directory, "state.json")
            with open(target, "w", encoding="utf-8") as stream:
                stream.write("keep")
            os.symlink(target, link)
            with self.assertRaises(ValueError):
                self.module.write_secure_json(link, {"new": True})
            with open(target, "r", encoding="utf-8") as stream:
                self.assertEqual(stream.read(), "keep")

    def test_secure_json_write_enforces_size_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "state.json")
            with self.assertRaises(ValueError):
                self.module.write_secure_json(path, {"large": "value"}, max_bytes=2)
            self.assertFalse(os.path.exists(path))

    def test_secure_json_temp_creation_is_exclusive(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "state.json")
            collision = os.path.join(directory, ".state.json.tmp-aaaa")
            with open(collision, "w", encoding="utf-8") as stream:
                stream.write("sentinel")
            with mock.patch.object(self.module.secrets, "token_hex", side_effect=["aaaa", "bbbb"]):
                self.module.write_secure_json(path, {"ok": True})
            with open(collision, "r", encoding="utf-8") as stream:
                self.assertEqual(stream.read(), "sentinel")

    def test_secure_json_supports_symlinked_parent_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            real_dir = os.path.join(directory, "real_config")
            os.makedirs(real_dir)
            link_dir = os.path.join(directory, "link_config")
            os.symlink(real_dir, link_dir)
            cfg_path = os.path.join(link_dir, "calendars.json")
            self.module.write_secure_json(cfg_path, {"test": "symlink_ok"})
            self.assertEqual(self.module.safe_load_json(cfg_path), {"test": "symlink_ok"})
            real_file = os.path.join(real_dir, "calendars.json")
            self.assertTrue(os.path.exists(real_file))


class FetchEventsSecureJsonTests(SecureJsonMixin, unittest.TestCase):
    module = fetch_events


class GoogleAuthSecureJsonTests(SecureJsonMixin, unittest.TestCase):
    module = google_auth


class JmapTransportTests(unittest.TestCase):
    class FakeResponse:
        def __init__(self, url, payload=b"{}"):
            self.url = url
            self.payload = payload
            self.offset = 0
            self.closed = False

        def geturl(self):
            return self.url

        def read(self, size=-1):
            if size < 0:
                size = len(self.payload) - self.offset
            chunk = self.payload[self.offset:self.offset + size]
            self.offset += len(chunk)
            return chunk

        def close(self):
            self.closed = True

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    def test_only_credential_free_https_urls_are_accepted(self):
        _, origin = fetch_events.validate_jmap_https_url("https://calendar.example/jmap/session")
        self.assertEqual(origin, ("https", "calendar.example", 443))
        for url in (
            "http://calendar.example/jmap/session",
            "https://user:pass@calendar.example/jmap/session",
            "https://calendar.example/jmap/session#fragment",
            "https://calendar.example\\@attacker.example/session",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                fetch_events.validate_jmap_https_url(url)

    def test_api_and_redirect_urls_must_keep_the_session_origin(self):
        _, origin = fetch_events.validate_jmap_https_url("https://calendar.example/session")
        fetch_events.validate_jmap_https_url("https://calendar.example/api", origin)
        with self.assertRaises(ValueError):
            fetch_events.validate_jmap_https_url("https://attacker.example/api", origin)
        handler = fetch_events.JmapSameOriginRedirectHandler(origin)
        with self.assertRaises(ValueError):
            handler.redirect_request(None, None, 302, "Found", {}, "https://attacker.example/api")

    def test_http_session_is_rejected_before_request_construction(self):
        calendar = {"name": "unsafe", "type": "jmap", "jmapUrl": "http://calendar.example/session", "jmapToken": "secret"}
        with mock.patch.object(fetch_events.urllib.request, "build_opener") as build_opener:
            result = fetch_events.fetch_jmap_calendar(calendar, fetch_events.datetime.now(), fetch_events.datetime.now())
        build_opener.assert_not_called()
        self.assertIn("must use HTTPS", result["status"])

    def test_untrusted_api_url_is_rejected_before_second_authenticated_request(self):
        session = json.dumps({"apiUrl": "https://attacker.example/api"}).encode("utf-8")
        opener = mock.Mock()
        opener.open.return_value = self.FakeResponse("https://calendar.example/session", session)
        calendar = {"name": "unsafe", "type": "jmap", "jmapUrl": "https://calendar.example/session", "jmapToken": "secret"}
        with mock.patch.object(fetch_events.urllib.request, "build_opener", return_value=opener):
            result = fetch_events.fetch_jmap_calendar(calendar, fetch_events.datetime.now(), fetch_events.datetime.now())
        self.assertEqual(opener.open.call_count, 1)
        self.assertIn("configured session origin", result["status"])

    def test_final_response_origin_is_checked_and_closed(self):
        _, origin = fetch_events.validate_jmap_https_url("https://calendar.example/session")
        response = self.FakeResponse("https://attacker.example/session")
        opener = mock.Mock()
        opener.open.return_value = response
        request = fetch_events.urllib.request.Request("https://calendar.example/session")
        with self.assertRaises(ValueError):
            fetch_events.open_trusted_jmap(opener, request, origin, timeout=1)
        self.assertTrue(response.closed)


class MeetingUrlSecurityTests(unittest.TestCase):
    def test_validate_meeting_url_accepts_clean_http_and_https_urls(self):
        valid_urls = [
            "https://meet.google.com/abc-defg-hij",
            "https://us02web.zoom.us/j/1234567890",
            "https://teams.microsoft.com/l/meetup-join/19%3ameeting",
            "https://custom.server.com/rooms/101",
            "http://local.meeting.net/join/test",
        ]
        for url in valid_urls:
            with self.subTest(url=url):
                self.assertEqual(fetch_events.validate_meeting_url(url), url)

    def test_validate_meeting_url_rejects_dangerous_and_malformed_schemes(self):
        invalid_urls = [
            "javascript:alert(1)",
            "file:///bin/sh",
            "data:text/html,<script>alert(1)</script>",
            "<h1>Meeting Title</h1>",
            "<script src='http://evil.com'></script>",
            "https://meet.google.com/<script>",
            "https://user:pass@meet.google.com/room",
            "https://zoom.us/j/123\nnewline",
            "ftp://files.example.com/meet",
            "",
            None,
            12345,
            "https://",
            "https://[invalid-host",
        ]
        for url in invalid_urls:
            with self.subTest(url=url):
                self.assertEqual(fetch_events.validate_meeting_url(url), "")

    def test_jmap_virtual_locations_unsafe_uri_is_sanitized(self):
        raw_events = [
            {
                "id": "jmap_unsafe_1",
                "title": "Malicious Meeting",
                "start": "2026-08-25T10:00:00Z",
                "virtualLocations": {
                    "loc1": {"uri": "file:///bin/sh"},
                    "loc2": {"uri": "<img src=x onerror=alert(1)>"},
                    "loc3": {"uri": "javascript:window.open()"},
                },
            },
            {
                "id": "jmap_safe_1",
                "title": "Safe Meeting",
                "start": "2026-08-25T11:00:00Z",
                "virtualLocations": {
                    "loc1": {"uri": "https://meet.jit.si/SafeRoom"},
                },
            },
        ]
        cal_info = {"name": "Test JMAP", "type": "jmap", "jmapToken": "secret"}
        start = fetch_events.datetime(2026, 8, 25, 0, 0, 0)
        end = fetch_events.datetime(2026, 8, 25, 23, 59, 59)

        # Mock JMAP network responses to return raw_events
        session_json = json.dumps({
            "apiUrl": "https://calendar.example/api",
            "accounts": {"acc1": {"accountCapabilities": {"urn:ietf:params:jmap:calendars": {}}}},
            "primaryAccounts": {"urn:ietf:params:jmap:calendars": "acc1"},
        }).encode("utf-8")

        query_get_json = json.dumps({
            "methodResponses": [
                ["CalendarEvent/get", {"list": raw_events}, "get0"]
            ]
        }).encode("utf-8")

        fake_session = JmapTransportTests.FakeResponse("https://calendar.example/session", session_json)
        fake_api = JmapTransportTests.FakeResponse("https://calendar.example/api", query_get_json)

        opener = mock.Mock()
        opener.open.side_effect = [fake_session, fake_api]

        with mock.patch.object(fetch_events.urllib.request, "build_opener", return_value=opener):
            cal_info["jmapUrl"] = "https://calendar.example/session"
            result = fetch_events.fetch_jmap_calendar(cal_info, start, end)

        self.assertEqual(result["status"], "ok")
        events = result["events"]
        self.assertEqual(len(events), 2)
        # Event 1 with dangerous URIs should have empty meetingUrl and meetingProvider
        self.assertEqual(events[0]["meetingUrl"], "")
        self.assertEqual(events[0]["meetingProvider"], "")
        # Event 2 with safe Jitsi URI should be extracted
        self.assertEqual(events[1]["meetingUrl"], "https://meet.jit.si/SafeRoom")
        self.assertEqual(events[1]["meetingProvider"], "Jitsi")


class RecurrenceCpuCeilingTests(unittest.TestCase):
    def test_multiday_expansion_on_multi_century_event_is_clamped_and_bounded(self):
        window_start = fetch_events.datetime(2026, 8, 1)
        window_end = fetch_events.datetime(2026, 8, 31)
        # Event spanning 1000 years
        event = {
            "id": "centuries_evt",
            "title": "Century Span",
            "start_dt": fetch_events.datetime(1900, 1, 1),
            "end_dt": fetch_events.datetime(2900, 1, 1),
            "all_day": True,
            "calendar": "Test",
            "color": "#4A90E2",
            "location": "",
            "description": "",
        }
        instances = fetch_events.expand_multiday_event(event, window_start, window_end)
        # Should be bounded to days within the window (31 days) and not loop millions of times
        self.assertLessEqual(len(instances), 31)
        self.assertGreater(len(instances), 0)
        for inst in instances:
            dt = fetch_events.datetime.strptime(inst["date_key"], "%Y-%m-%d")
            self.assertTrue(window_start <= dt <= window_end)

    def test_multiday_expansion_outside_window_returns_empty_immediately(self):
        window_start = fetch_events.datetime(2026, 8, 1)
        window_end = fetch_events.datetime(2026, 8, 31)
        event = {
            "id": "past_evt",
            "title": "Past",
            "start_dt": fetch_events.datetime(2020, 1, 1),
            "end_dt": fetch_events.datetime(2020, 1, 10),
            "all_day": True,
            "calendar": "Test",
            "color": "#4A90E2",
            "location": "",
            "description": "",
        }
        instances = fetch_events.expand_multiday_event(event, window_start, window_end)
        self.assertEqual(instances, [])

    def test_daily_recurrence_from_distant_past_terminates_under_cpu_ceiling(self):
        window_start = fetch_events.datetime(2026, 8, 1)
        window_end = fetch_events.datetime(2026, 8, 31)
        event = {
            "id": "old_rec",
            "title": "Ancient Daily Recurrence",
            "start_dt": fetch_events.datetime(1000, 1, 1, 9, 0, 0),
            "end_dt": fetch_events.datetime(1000, 1, 1, 10, 0, 0),
            "all_day": False,
            "calendar": "Test",
            "color": "#4A90E2",
            "location": "",
            "description": "",
            "rrule": {"FREQ": "DAILY", "INTERVAL": "1"},
        }
        # Should fast-forward and expand within window without timeout
        instances = fetch_events.expand_recurring_event(event, window_start, window_end)
        self.assertGreater(len(instances), 0)
        self.assertLessEqual(len(instances), 31)
        for inst in instances:
            self.assertTrue(window_start <= inst["start_dt"] <= window_end)

    def test_weekly_recurrence_from_distant_past_terminates_under_cpu_ceiling(self):
        window_start = fetch_events.datetime(2026, 8, 1)
        window_end = fetch_events.datetime(2026, 8, 31)
        event = {
            "id": "old_weekly",
            "title": "Ancient Weekly Recurrence",
            "start_dt": fetch_events.datetime(1500, 1, 1, 9, 0, 0),
            "end_dt": fetch_events.datetime(1500, 1, 1, 10, 0, 0),
            "all_day": False,
            "calendar": "Test",
            "color": "#4A90E2",
            "location": "",
            "description": "",
            "rrule": {"FREQ": "WEEKLY", "INTERVAL": "1"},
        }
        instances = fetch_events.expand_recurring_event(event, window_start, window_end)
        self.assertGreater(len(instances), 0)
        for inst in instances:
            self.assertTrue(window_start <= inst["start_dt"] <= window_end)


class ConfigAndStateSecurityTests(unittest.TestCase):
    def test_save_config_from_stdin(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = os.path.join(directory, "calendars.json")
            with mock.patch.object(fetch_events, "CONFIG_PATH", config_path):
                stdin_payload = json.dumps([{"name": "Fastmail", "type": "jmap", "jmapToken": "secret-token"}])
                with mock.patch("sys.argv", ["fetch-events.py", "--save-config"]), \
                     mock.patch("sys.stdin.readline", return_value=stdin_payload + "\n"), \
                     self.assertRaises(SystemExit) as cm:
                    fetch_events.main()
                self.assertEqual(cm.exception.code, 0)
                saved = fetch_events.safe_load_json(config_path)
                self.assertEqual(saved, [{"name": "Fastmail", "type": "jmap", "jmapToken": "secret-token"}])
                file_stat = os.stat(config_path)
                self.assertEqual(stat.S_IMODE(file_stat.st_mode), 0o600)

    def test_purge_plugin_data_removes_config_and_oauth_state(self):
        with tempfile.TemporaryDirectory() as directory:
            config_file = os.path.join(directory, "calendars.json")
            auth_file = os.path.join(directory, "google-auth.json")
            events_file = os.path.join(directory, "calendar-events.json")
            cache_file = os.path.join(directory, "translation-cache.json")

            local_file = os.path.join(directory, "local-events.json")

            with open(config_file, "w") as f:
                f.write('{"jmapToken": "secret"}')
            with open(auth_file, "w") as f:
                f.write('{"refresh_token": "secret"}')
            with open(events_file, "w") as f:
                f.write('{"events": []}')
            with open(cache_file, "w") as f:
                f.write('{}')
            with open(local_file, "w") as f:
                f.write('[]')

            with mock.patch.object(fetch_events, "CONFIG_PATH", config_file), \
                 mock.patch.object(fetch_events, "AUTH_FILE", auth_file), \
                 mock.patch.object(fetch_events, "OUTPUT_PATH", events_file), \
                 mock.patch.object(fetch_events, "TRANSLATION_CACHE_PATH", cache_file), \
                 mock.patch.object(fetch_events, "LOCAL_EVENTS_PATH", local_file), \
                 mock.patch.object(fetch_events, "STATE_DIR", directory):
                res = fetch_events.purge_plugin_data()
                self.assertEqual(res["status"], "success")
                self.assertFalse(os.path.exists(config_file))
                self.assertFalse(os.path.exists(auth_file))
                self.assertFalse(os.path.exists(events_file))
                self.assertFalse(os.path.exists(cache_file))
                self.assertFalse(os.path.exists(local_file))


class TwoWayEventSyncTests(unittest.TestCase):
    def test_get_writable_calendars_filtering(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg_path = os.path.join(directory, "calendars.json")
            sample_cfg = [
                {"name": "iCal Feed", "url": "https://example.com/feed.ics", "enabled": True},
                {"name": "My Google", "googleCalendarId": "xyz@group.calendar.google.com", "enabled": True},
                {"name": "My JMAP", "type": "jmap", "jmapToken": "tok_123", "enabled": True},
            ]
            fetch_events.write_secure_json(cfg_path, sample_cfg)
            with mock.patch.object(fetch_events, "CONFIG_PATH", cfg_path):
                writables = fetch_events.get_writable_calendars()
                names = [w["name"] for w in writables]
                self.assertIn("My Google", names)
                self.assertIn("My JMAP", names)
                self.assertIn("Local Calendar", names)
                self.assertNotIn("iCal Feed", names)

    def test_local_calendar_crud_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory:
            local_path = os.path.join(directory, "local-events.json")
            cfg_path = os.path.join(directory, "calendars.json")
            out_path = os.path.join(directory, "calendar-events.json")
            fetch_events.write_secure_json(cfg_path, [])

            with mock.patch.object(fetch_events, "LOCAL_EVENTS_PATH", local_path), \
                 mock.patch.object(fetch_events, "CONFIG_PATH", cfg_path), \
                 mock.patch.object(fetch_events, "OUTPUT_PATH", out_path), \
                 mock.patch.object(fetch_events, "STATE_DIR", directory):
                
                # 1. Create event
                evt_data = {
                    "title": "Architecture Review",
                    "start": "2026-09-01T10:00:00",
                    "end": "2026-09-01T11:00:00",
                    "allDay": False,
                    "location": "HQ Conference Room 3",
                    "description": "Review Q3 milestone architecture",
                    "calendar": "Local Calendar"
                }
                res = fetch_events.create_event(evt_data)
                self.assertEqual(res["status"], "success")
                event_id = res["id"]
                self.assertTrue(event_id.startswith("loc_"))

                # Verify file permissions
                file_stat = os.stat(local_path)
                self.assertEqual(stat.S_IMODE(file_stat.st_mode), 0o600)

                # 2. Fetch local calendar
                from datetime import datetime, timedelta
                now = datetime(2026, 9, 1)
                fetched = fetch_events.fetch_local_calendar(
                    {"name": "Local Calendar", "color": "#a6e3a1"},
                    now - timedelta(days=5),
                    now + timedelta(days=5)
                )
                self.assertEqual(fetched["status"], "ok")
                self.assertEqual(len(fetched["events"]), 1)
                self.assertEqual(fetched["events"][0]["title"], "Architecture Review")
                self.assertEqual(fetched["events"][0]["writable"], True)

                # 3. Delete event
                del_res = fetch_events.delete_event({"id": event_id, "calendar": "Local Calendar"})
                self.assertEqual(del_res["status"], "success")

                # Verify event is gone
                fetched_after = fetch_events.fetch_local_calendar(
                    {"name": "Local Calendar", "color": "#a6e3a1"},
                    now - timedelta(days=5),
                    now + timedelta(days=5)
                )
                self.assertEqual(len(fetched_after["events"]), 0)

    def test_create_event_on_readonly_feed_fails_cleanly(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg_path = os.path.join(directory, "calendars.json")
            sample_cfg = [{"name": "Public Holidays", "url": "https://example.com/holidays.ics", "enabled": True}]
            fetch_events.write_secure_json(cfg_path, sample_cfg)

            with mock.patch.object(fetch_events, "CONFIG_PATH", cfg_path):
                with self.assertRaises(ValueError) as cm:
                    fetch_events.create_event({
                        "title": "New Event",
                        "start": "2026-09-01T10:00:00",
                        "calendar": "Public Holidays"
                    })
                self.assertIn("read-only", str(cm.exception))

    def test_cli_create_and_delete_event(self):
        with tempfile.TemporaryDirectory() as directory:
            local_path = os.path.join(directory, "local-events.json")
            cfg_path = os.path.join(directory, "calendars.json")
            out_path = os.path.join(directory, "calendar-events.json")
            fetch_events.write_secure_json(cfg_path, [])

            with mock.patch.object(fetch_events, "LOCAL_EVENTS_PATH", local_path), \
                 mock.patch.object(fetch_events, "CONFIG_PATH", cfg_path), \
                 mock.patch.object(fetch_events, "OUTPUT_PATH", out_path), \
                 mock.patch.object(fetch_events, "STATE_DIR", directory):

                # Test --writable-calendars CLI
                with mock.patch("sys.argv", ["fetch-events.py", "--writable-calendars"]), \
                     self.assertRaises(SystemExit) as cm:
                    fetch_events.main()
                self.assertEqual(cm.exception.code, 0)

                # Test --create-event CLI
                create_payload = json.dumps({
                    "title": "CLI Standup",
                    "start": "2026-09-01T09:00:00",
                    "end": "2026-09-01T09:30:00",
                    "calendar": "Local Calendar"
                })
                with mock.patch("sys.argv", ["fetch-events.py", "--create-event"]), \
                     mock.patch("sys.stdin.readline", return_value=create_payload + "\n"), \
                     self.assertRaises(SystemExit) as cm:
                    fetch_events.main()
                self.assertEqual(cm.exception.code, 0)

                saved_local = fetch_events.safe_load_json(local_path)
                self.assertEqual(len(saved_local), 1)
                evt_id = saved_local[0]["id"]

                # Test --delete-event CLI
                del_payload = json.dumps({"id": evt_id, "calendar": "Local Calendar"})
                with mock.patch("sys.argv", ["fetch-events.py", "--delete-event"]), \
                     mock.patch("sys.stdin.readline", return_value=del_payload + "\n"), \
                     self.assertRaises(SystemExit) as cm:
                    fetch_events.main()
                self.assertEqual(cm.exception.code, 0)

                saved_after = fetch_events.safe_load_json(local_path)
                self.assertEqual(len(saved_after), 0)

    def test_create_and_delete_google_event_mocked(self):
        cal_info = {"name": "Work", "googleCalendarId": "work@domain.com"}
        event_data = {
            "title": "Quarterly Planning",
            "start": "2026-09-01T14:00:00",
            "end": "2026-09-01T15:00:00",
            "allDay": False,
            "location": "Boardroom",
            "description": "Discuss Q4 goals",
        }

        with mock.patch.object(fetch_events, "get_google_access_token", return_value="mock-token"), \
             mock.patch("urllib.request.urlopen") as mock_urlopen:
            # Mock create response
            resp_create = io.BytesIO(json.dumps({"id": "g_evt_999", "summary": "Quarterly Planning"}).encode("utf-8"))
            resp_create.__enter__ = lambda s: s
            resp_create.__exit__ = lambda s, *args: None

            mock_urlopen.return_value = resp_create
            res = fetch_events.create_google_event(cal_info, event_data)
            self.assertEqual(res["status"], "success")
            self.assertEqual(res["id"], "g_evt_999")

            # Check request details
            req = mock_urlopen.call_args[0][0]
            self.assertIn("https://www.googleapis.com/calendar/v3/calendars/work%40domain.com/events", req.full_url)
            self.assertEqual(req.get_method(), "POST")
            sent_body = json.loads(req.data.decode("utf-8"))
            self.assertEqual(sent_body["summary"], "Quarterly Planning")
            self.assertEqual(sent_body["location"], "Boardroom")

            # Mock delete response
            resp_del = io.BytesIO(b"")
            resp_del.__enter__ = lambda s: s
            resp_del.__exit__ = lambda s, *args: None
            mock_urlopen.return_value = resp_del

            del_res = fetch_events.delete_google_event(cal_info, "g_evt_999")
            self.assertEqual(del_res["status"], "success")
            self.assertEqual(del_res["id"], "g_evt_999")
            del_req = mock_urlopen.call_args[0][0]
            self.assertEqual(del_req.get_method(), "DELETE")
            self.assertIn("/events/g_evt_999", del_req.full_url)

    def test_create_and_delete_jmap_event_mocked(self):
        cal_info = {"name": "Fastmail", "type": "jmap", "jmapToken": "jmap-tok", "jmapUrl": "https://api.fastmail.com/jmap/session"}
        event_data = {
            "title": "Design Sync",
            "start": "2026-09-01T11:00:00",
            "end": "2026-09-01T12:00:00",
            "allDay": False,
            "location": "Online",
            "description": "Figma review",
        }

        session_resp_bytes = json.dumps({
            "apiUrl": "https://api.fastmail.com/jmap/api",
            "primaryAccounts": {"urn:ietf:params:jmap:calendars": "acc_123"},
            "capabilities": {"urn:ietf:params:jmap:calendars": {}}
        }).encode("utf-8")

        set_resp_bytes = json.dumps({
            "methodResponses": [
                [
                    "CalendarEvent/set",
                    {
                        "accountId": "acc_123",
                        "created": {
                            "c_cid": {"id": "jmap_evt_456", "title": "Design Sync"}
                        }
                    },
                    "set0"
                ]
            ]
        }).encode("utf-8")

        del_resp_bytes = json.dumps({
            "methodResponses": [
                [
                    "CalendarEvent/set",
                    {
                        "accountId": "acc_123",
                        "destroyed": ["jmap_evt_456"]
                    },
                    "del0"
                ]
            ]
        }).encode("utf-8")

        def make_stream(b):
            s = io.BytesIO(b)
            s.__enter__ = lambda self: self
            s.__exit__ = lambda self, *args: None
            return s

        s1 = make_stream(session_resp_bytes)
        s2 = make_stream(set_resp_bytes)
        s3 = make_stream(session_resp_bytes)
        s4 = make_stream(del_resp_bytes)

        with mock.patch.object(fetch_events, "open_trusted_jmap", side_effect=[s1, s2, s3, s4]), \
             mock.patch.object(fetch_events, "secrets") as mock_secrets:
            mock_secrets.token_hex.return_value = "cid"
            create_res = fetch_events.create_jmap_event(cal_info, event_data)
            self.assertEqual(create_res["status"], "success")
            self.assertEqual(create_res["id"], "jmap_evt_456")

            del_res = fetch_events.delete_jmap_event(cal_info, "jmap_evt_456")
            self.assertEqual(del_res["status"], "success")
            self.assertEqual(del_res["id"], "jmap_evt_456")

        # Also test format_duration_iso helper
        self.assertEqual(fetch_events.format_duration_iso(3600), "PT1H")
        self.assertEqual(fetch_events.format_duration_iso(1800), "PT30M")
        self.assertEqual(fetch_events.format_duration_iso(5400), "PT1H30M")


if __name__ == "__main__":
    unittest.main()
