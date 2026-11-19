import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
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
# Keep the sync cache out of the real ~/.local/state, and the keyring off:
# a test must never write to the desktop keyring.
fetch_events.SYNC_CACHE_DIR = tempfile.mkdtemp(prefix="chronica-test-cache-")
fetch_events._secret_tool = lambda args, value=None: None
# The backend speaks the desktop's language; the assertions below are English.
fetch_events._ui_lang = lambda: "en"
google_auth = load_script("google_auth", "google-auth.py")
google_auth.backend._secret_tool = lambda args, value=None: None


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

    def test_whole_feed_expansion_is_capped(self):
        # Daily, 60 days long: each event fans out to thousands of day entries.
        now = fetch_events.datetime.now()
        window_start = now - fetch_events.timedelta(days=45)
        window_end = now + fetch_events.timedelta(days=90)
        vevent = (
            "BEGIN:VEVENT\r\nUID:x\r\n"
            f"DTSTART;VALUE=DATE:{window_start:%Y%m%d}\r\n"
            f"DTEND;VALUE=DATE:{window_start + fetch_events.timedelta(days=60):%Y%m%d}\r\n"
            "RRULE:FREQ=DAILY\r\nSUMMARY:a\r\nEND:VEVENT\r\n"
        )
        ics = "BEGIN:VCALENDAR\r\n" + vevent * 50 + "END:VCALENDAR\r\n"
        events = fetch_events.parse_ics(ics, {}, window_start, window_end)
        self.assertEqual(len(events), fetch_events.MAX_FEED_INSTANCES)

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

    def test_every_second_of_the_year_is_never_built_in_memory(self):
        # 366 days x 86,400 times = 31 million candidates in one YEARLY period.
        every_second = ("FREQ=YEARLY;BYDAY=MO,TU,WE,TH,FR,SA,SU;BYHOUR=" + ",".join(map(str, range(24)))
                        + ";BYMINUTE=" + ",".join(map(str, range(60)))
                        + ";BYSECOND=" + ",".join(map(str, range(60))))
        event = {"start_dt": fetch_events.datetime(2025, 1, 1), "end_dt": fetch_events.datetime(2025, 1, 1),
                 "rrule": fetch_events.parse_rrule(every_second + ";BYSETPOS=1,-1"), "exdates": []}
        window_start, window_end = fetch_events.datetime(2026, 1, 1), fetch_events.datetime(2027, 12, 31, 23, 59, 59)
        got = [i["start_dt"] for i in fetch_events.expand_rrule_in_wall_time(event, window_start, window_end)]
        self.assertEqual(got, [fetch_events.datetime(2026, 1, 1), fetch_events.datetime(2026, 12, 31, 23, 59, 59),
                               fetch_events.datetime(2027, 1, 1), fetch_events.datetime(2027, 12, 31, 23, 59, 59)])

        # Without BYSETPOS every candidate is an occurrence: the candidate
        # ceiling ends the walk long before the window.
        event["rrule"] = fetch_events.parse_rrule(every_second)
        self.assertEqual(fetch_events.expand_rrule_in_wall_time(event, window_start, window_end), [])

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


class DesktopNotificationTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("dbus-daemon"), "needs dbus-daemon")
    def test_notify_is_a_well_formed_dbus_call_with_no_child_process(self):
        # Event text must not become a process argument: those are readable
        # by any local user in /proc/<pid>/cmdline. A private bus with no
        # notification daemon answers a well-formed Notify with an error,
        # while it drops the connection on a malformed one (OSError).
        daemon = subprocess.Popen(["dbus-daemon", "--session", "--print-address=1", "--nofork"],
                                  stdout=subprocess.PIPE, text=True)
        self.addCleanup(daemon.wait)
        self.addCleanup(daemon.terminate)
        address = daemon.stdout.readline().strip()
        daemon.stdout.close()
        with mock.patch.dict(os.environ, {"DBUS_SESSION_BUS_ADDRESS": address}), \
             mock.patch.object(fetch_events.subprocess, "run", side_effect=AssertionError("child process")), \
             mock.patch.object(fetch_events.subprocess, "Popen", side_effect=AssertionError("child process")):
            self.assertFalse(fetch_events.dbus_notify("Standup 09:30 \U0001f600", "Room 3, caf\u00e9"))

    def test_body_text_reaches_the_daemon_as_text_not_markup(self):
        seen = {}

        def capture(app, summary, body, hints, timeout):
            seen["body"] = body
            raise OSError("stop before the socket")

        with mock.patch.object(fetch_events, "_dbus_notify_body", side_effect=capture):
            with self.assertRaises(OSError):
                fetch_events.dbus_notify("Standup", '<a href="http://x">Room</a> & co')
        self.assertEqual(seen["body"], '&lt;a href="http://x"&gt;Room&lt;/a&gt; &amp; co')


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

        # No calendar is configured, so the default one is looked up: an event needs at least one calendar.
        cals_resp_bytes = json.dumps({"methodResponses": [["Calendar/get", {"list": [
            {"id": "cal_other", "isDefault": False},
            {"id": "cal_default", "isDefault": True},
        ]}, "c0"]]}).encode("utf-8")
        responses = [make_stream(b) for b in (
            session_resp_bytes, cals_resp_bytes, set_resp_bytes, session_resp_bytes, del_resp_bytes)]
        sent = []

        def fake_open(opener, request, *args, **kwargs):
            sent.append(request)
            return responses.pop(0)

        with mock.patch.object(fetch_events, "open_trusted_jmap", fake_open), \
             mock.patch.object(fetch_events, "secrets") as mock_secrets:
            mock_secrets.token_hex.return_value = "cid"
            create_res = fetch_events.create_jmap_event(cal_info, event_data)
            self.assertEqual(create_res["status"], "success")
            self.assertEqual(create_res["id"], "jmap_evt_456")
            created = json.loads(sent[2].data)["methodCalls"][0][1]["create"]["c_cid"]
            self.assertEqual(created["calendarIds"], {"cal_default": True})

            del_res = fetch_events.delete_jmap_event(cal_info, "jmap_evt_456")
            self.assertEqual(del_res["status"], "success")
            self.assertEqual(del_res["id"], "jmap_evt_456")

        # Also test format_duration_iso helper
        self.assertEqual(fetch_events.format_duration_iso(3600), "PT1H")
        self.assertEqual(fetch_events.format_duration_iso(1800), "PT30M")
        self.assertEqual(fetch_events.format_duration_iso(5400), "PT1H30M")


class RedirectBodyTests(unittest.TestCase):
    def test_a_redirect_with_an_endless_body_is_not_read(self):
        # The stock redirect handler reads the whole 302 body before following
        # it, with no byte limit: this one never ends.
        from datetime import datetime
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading

        feed = b"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:a\r\nSUMMARY:Kept\r\nDTSTART:20260601T090000Z\r\nDTEND:20260601T100000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/feed.ics":
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(feed)))
                    self.end_headers()
                    self.wfile.write(feed)
                    return
                self.send_response(302)
                self.send_header("Location", "/feed.ics")
                self.end_headers()
                try:
                    while not server.stopping:
                        self.wfile.write(b"x" * 65536)
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        server.stopping = False
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.addCleanup(setattr, server, "stopping", True)

        result = {}
        cal = {"name": "Feed", "url": f"http://127.0.0.1:{server.server_port}/start"}
        worker = threading.Thread(target=lambda: result.update(fetch_events.fetch_calendar(
            cal, datetime(2026, 5, 1), datetime(2026, 7, 1))), daemon=True)
        worker.start()
        worker.join(5)
        self.assertFalse(worker.is_alive(), "the fetch is still reading the redirect body")
        self.assertEqual(result["status"], "ok")
        self.assertEqual([e["title"] for e in result["events"]], ["Kept"])



class FeedWorkBoundTests(unittest.TestCase):
    def test_a_long_folded_line_unfolds_in_linear_time(self):
        # 50,000 continuations of one property, 3.75 MB, under the feed cap.
        import threading, time
        raw = "DESCRIPTION:x\r\n" + "".join(" " + "y" * 74 + "\r\n" for _ in range(50000))
        out = {}
        worker = threading.Thread(target=lambda: out.update(lines=fetch_events.unfold_lines(raw)), daemon=True)
        started = time.monotonic()
        worker.start()
        worker.join(10)
        self.assertFalse(worker.is_alive(), "unfolding is still copying the line")
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(len(out["lines"]), 1)
        self.assertEqual(len(out["lines"][0]), len("DESCRIPTION:x") + 50000 * 74)

    def test_a_sync_that_never_ends_is_stopped_at_the_deadline(self):
        import time
        with tempfile.TemporaryDirectory() as directory:
            stuck = os.path.join(directory, "python3")
            with open(stuck, "w") as f:
                f.write("#!/bin/sh\nexec sleep 30\n")
            os.chmod(stuck, 0o755)
            started = time.monotonic()
            with mock.patch.object(fetch_events.sys, "executable", stuck):
                result = fetch_events.sync_in_child(deadline=1)
            self.assertLess(time.monotonic() - started, 5)
            self.assertEqual(result["status"], "error")

    def test_an_event_command_that_never_ends_is_stopped_at_the_deadline(self):
        import time
        with tempfile.TemporaryDirectory() as directory:
            stuck = os.path.join(directory, "python3")
            with open(stuck, "w") as f:
                f.write("#!/bin/sh\nexec sleep 30\n")
            os.chmod(stuck, 0o755)
            started = time.monotonic()
            with mock.patch.object(fetch_events.sys, "executable", stuck):
                result = fetch_events.event_in_child("--create-event", {"title": "x"}, deadline=1)
            self.assertLess(time.monotonic() - started, 5)
            self.assertEqual(result["status"], "error")

    def test_an_event_command_runs_in_a_child_with_its_payload_on_stdin(self):
        # A real child, with HOME in a temporary folder: it creates a local
        # event, syncs, and answers through the size-limited reply.
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.dict(os.environ, {"HOME": home}):
                result = fetch_events.event_in_child("--create-event", {
                    "title": "Child Standup", "start": "2026-09-01T09:00:00",
                    "end": "2026-09-01T09:30:00", "calendar": "Local Calendar"})
            self.assertEqual(result["status"], "success", result)
            with open(os.path.join(home, ".local/state/omarchy/local-events.json")) as f:
                self.assertEqual([e["title"] for e in json.load(f)], ["Child Standup"])

    def test_a_recurring_event_with_huge_text_stays_bounded_in_the_output(self):
        # One daily event with a 9 MB description: each of its ~135 instances
        # in the window used to repeat the whole text in the output JSON.
        import tracemalloc
        from datetime import datetime, timedelta
        start = (datetime.now() - timedelta(days=40)).strftime("%Y%m%dT090000")
        feed = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:big\r\nSUMMARY:Daily\r\n"
                f"DTSTART:{start}\r\nDTEND:{start[:9]}100000\r\nRRULE:FREQ=DAILY\r\n"
                "DESCRIPTION:" + "d" * 9_000_000 + "\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
        with tempfile.TemporaryDirectory() as directory:
            cfg_path = os.path.join(directory, "calendars.json")
            out_path = os.path.join(directory, "calendar-events.json")
            fetch_events.write_secure_json(cfg_path, [{"name": "Feed", "url": "https://example.com/f.ics"}])

            def fetch(cal, window_start, window_end):
                events = fetch_events.parse_ics(feed, cal, window_start, window_end)
                return {"name": "Feed", "events": events, "status": "ok", "count": len(events)}

            with mock.patch.object(fetch_events, "CONFIG_PATH", cfg_path), \
                 mock.patch.object(fetch_events, "OUTPUT_PATH", out_path), \
                 mock.patch.object(fetch_events, "LOCAL_EVENTS_PATH", os.path.join(directory, "local.json")), \
                 mock.patch.object(fetch_events, "STATE_DIR", directory), \
                 mock.patch.object(fetch_events, "fetch_calendar_item", side_effect=fetch):
                tracemalloc.start()
                fetch_events.sync_all_events()
                peak = tracemalloc.get_traced_memory()[1]
                tracemalloc.stop()
            self.assertLess(peak, 300 * 1024 * 1024)
            self.assertLessEqual(os.path.getsize(out_path), fetch_events.MAX_OUTPUT_JSON_BYTES)
            with open(out_path) as f:
                days = json.load(f)["eventsByDate"]
            instances = [e for day in days.values() for e in day]
            self.assertTrue(instances)
            for e in instances:
                self.assertLessEqual(len(e["description"]), fetch_events.MAX_DESCRIPTION_CHARS)
                self.assertFalse(e["editable"])

    def _sync_feed(self, feed):
        """Run sync_all_events over one in-memory feed; returns (peak bytes, written JSON)."""
        import tracemalloc
        with tempfile.TemporaryDirectory() as directory:
            cfg_path = os.path.join(directory, "calendars.json")
            out_path = os.path.join(directory, "calendar-events.json")
            fetch_events.write_secure_json(cfg_path, [{"name": "Feed", "url": "https://example.com/f.ics"}])

            def fetch(cal, window_start, window_end):
                events = fetch_events.parse_ics(feed, cal, window_start, window_end)
                return {"name": "Feed", "events": events, "status": "ok", "count": len(events)}

            with mock.patch.object(fetch_events, "CONFIG_PATH", cfg_path), \
                 mock.patch.object(fetch_events, "OUTPUT_PATH", out_path), \
                 mock.patch.object(fetch_events, "LOCAL_EVENTS_PATH", os.path.join(directory, "local.json")), \
                 mock.patch.object(fetch_events, "STATE_DIR", directory), \
                 mock.patch.object(fetch_events, "fetch_calendar_item", side_effect=fetch):
                tracemalloc.start()
                fetch_events.sync_all_events()
                peak = tracemalloc.get_traced_memory()[1]
                tracemalloc.stop()
            self.assertLessEqual(os.path.getsize(out_path), fetch_events.MAX_OUTPUT_JSON_BYTES)
            with open(out_path) as f:
                return peak, json.load(f)

    def test_every_field_of_a_recurring_event_stays_bounded_in_the_output(self):
        # A daily event whose UID, location and URL are megabytes long: each
        # instance in the window used to repeat them in the output JSON.
        from datetime import datetime, timedelta
        start = (datetime.now() - timedelta(days=40)).strftime("%Y%m%dT090000")
        feed = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:" + "u" * 9_000_000 + "\r\nSUMMARY:Daily\r\n"
                f"DTSTART:{start}\r\nDTEND:{start[:9]}100000\r\nRRULE:FREQ=DAILY\r\n"
                "LOCATION:" + "l" * 3_000_000 + "\r\nURL:https://example.com/" + "p" * 3_000_000 +
                "\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
        peak, out = self._sync_feed(feed)
        self.assertLess(peak, 300 * 1024 * 1024)
        instances = [e for day in out["eventsByDate"].values() for e in day]
        self.assertGreater(len(instances), 100)
        for e in instances:
            for key, value in e.items():
                if isinstance(value, str):
                    self.assertLessEqual(len(value), fetch_events.OUTPUT_FIELD_LIMITS.get(key, fetch_events.MAX_FIELD_CHARS), key)
            self.assertTrue(e["id"].startswith("long-id-"))
            self.assertFalse(e["writable"])
            self.assertFalse(e["editable"])

    def test_rdates_share_the_per_event_instance_ceiling(self):
        from datetime import datetime, timedelta
        base = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        rdates = ",".join((base + timedelta(seconds=37 * i)).strftime("%Y%m%dT%H%M%S") for i in range(200000))
        feed = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:r\r\nSUMMARY:Many\r\n"
                f"DTSTART:{base.strftime('%Y%m%dT%H%M%S')}\r\nDURATION:PT1M\r\nRDATE:{rdates}\r\n"
                "EXDATE:" + ",".join("19990101T000000" for _ in range(50000)) +
                "\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
        events = fetch_events.parse_ics(feed, {"name": "Feed"}, base - timedelta(days=45), base + timedelta(days=90))
        self.assertLessEqual(len(events), fetch_events.MAX_EXPANDED_INSTANCES)

    def test_a_served_answer_is_one_short_line(self):
        out = io.StringIO()
        with mock.patch.dict(fetch_events.SERVE_COMMANDS, {"big": lambda p: {"status": "error", "message": "m" * 5_000_000},
                                                            "huge": lambda p: {"status": "ok", "data": "d" * 5_000_000}}):
            fetch_events.serve(io.StringIO('{"id": 1, "cmd": "big"}\n{"id": 2, "cmd": "huge"}\n'), out)
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertLessEqual(len(json.loads(lines[0])["result"]["message"]), fetch_events.MAX_FIELD_CHARS)
        self.assertEqual(json.loads(lines[1])["result"]["status"], "error")
        self.assertTrue(all(len(line) <= fetch_events.MAX_SERVE_REPLY_CHARS for line in lines))

    def test_google_pages_share_one_byte_limit(self):
        page = json.dumps({"items": [{"id": "x" * 4_000_000}], "nextPageToken": "more"}).encode()

        class Page(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        calls = []

        def urlopen(req, timeout=None):
            calls.append(req)
            return Page(page)

        with mock.patch.object(fetch_events.urllib.request, "urlopen", side_effect=urlopen):
            with self.assertRaises(ValueError):
                fetch_events.google_list("https://www.googleapis.com/x", {}, "token")
        self.assertLessEqual(len(calls), 3)


class GoogleSignInTests(unittest.TestCase):
    def test_pkce_challenge_matches_rfc_7636(self):
        self.assertEqual(google_auth.pkce_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"),
                         "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")
        verifier, challenge = google_auth.pkce_pair()
        self.assertTrue(43 <= len(verifier) <= 128)
        self.assertEqual(challenge, google_auth.pkce_challenge(verifier))

    def test_the_token_request_carries_the_verifier(self):
        sent = {}

        class Answer(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def urlopen(req, timeout=None):
            sent.update(urllib.parse.parse_qs(req.data.decode()))
            return Answer(b'{"access_token": "a"}')

        import urllib.parse
        with mock.patch.object(google_auth.urllib.request, "urlopen", side_effect=urlopen):
            google_auth.exchange_code_for_tokens("cid", "secret", "code", "the-verifier")
        self.assertEqual(sent["code_verifier"], ["the-verifier"])

    def test_a_silent_local_connection_does_not_hold_the_callback_server(self):
        import socket, threading, time
        from http.server import HTTPServer
        server = HTTPServer(("127.0.0.1", 0), google_auth.OAuthCallbackHandler)
        self.addCleanup(server.server_close)
        google_auth.OAuthCallbackHandler.timeout = 1
        self.addCleanup(setattr, google_auth.OAuthCallbackHandler, "timeout", 10)
        silent = socket.create_connection(server.server_address)
        self.addCleanup(silent.close)
        done = threading.Event()
        threading.Thread(target=lambda: (server.handle_request(), done.set()), daemon=True).start()
        started = time.monotonic()
        self.assertTrue(done.wait(5), "the server is still waiting for the silent client")
        self.assertLess(time.monotonic() - started, 5)

if __name__ == "__main__":
    unittest.main()
