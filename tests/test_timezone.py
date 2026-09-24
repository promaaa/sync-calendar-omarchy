import importlib.util
from datetime import datetime, date, timedelta, timezone
import json
import os
from pathlib import Path
import unittest
from unittest import mock

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

ROOT = Path(__file__).resolve().parents[1]


def load_script(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fetch_events = load_script("fetch_events", "fetch-events.py")


class TimezoneResolutionTests(unittest.TestCase):
    def setUp(self):
        fetch_events._zone_cache.clear()

    def test_resolve_standard_iana_timezone(self):
        if ZoneInfo is None:
            self.skipTest("ZoneInfo not available")
        zone = fetch_events.resolve_timezone("America/New_York")
        self.assertIsNotNone(zone)
        self.assertEqual(zone.key, "America/New_York")

    def test_resolve_quoted_timezone(self):
        if ZoneInfo is None:
            self.skipTest("ZoneInfo not available")
        zone = fetch_events.resolve_timezone('"America/Chicago"')
        self.assertIsNotNone(zone)
        self.assertEqual(zone.key, "America/Chicago")

    def test_resolve_prefixed_timezone(self):
        if ZoneInfo is None:
            self.skipTest("ZoneInfo not available")
        zone = fetch_events.resolve_timezone("/mozilla.org/20050126_1/America/New_York")
        self.assertIsNotNone(zone)
        self.assertEqual(zone.key, "America/New_York")

    def test_resolve_windows_exchange_aliases(self):
        if ZoneInfo is None:
            self.skipTest("ZoneInfo not available")
        zone_est = fetch_events.resolve_timezone("Eastern Standard Time")
        self.assertIsNotNone(zone_est)
        self.assertEqual(zone_est.key, "America/New_York")

        zone_cet = fetch_events.resolve_timezone("Central European Standard Time")
        self.assertIsNotNone(zone_cet)
        self.assertEqual(zone_cet.key, "Europe/Warsaw")

        zone_tokyo = fetch_events.resolve_timezone("Tokyo Standard Time")
        self.assertIsNotNone(zone_tokyo)
        self.assertEqual(zone_tokyo.key, "Asia/Tokyo")

    def test_resolve_unknown_or_empty_timezone_returns_none(self):
        self.assertIsNone(fetch_events.resolve_timezone(""))
        self.assertIsNone(fetch_events.resolve_timezone(None))
        self.assertIsNone(fetch_events.resolve_timezone("   "))
        self.assertIsNone(fetch_events.resolve_timezone("NonExistent/Custom_Zone_12345"))

    def test_extract_tzid_from_params(self):
        self.assertEqual(fetch_events.extract_tzid(["TZID=America/New_York"]), "America/New_York")
        self.assertEqual(fetch_events.extract_tzid(["tzid=UTC"]), "UTC")
        self.assertEqual(fetch_events.extract_tzid(['VALUE=DATE-TIME', 'TZID="America/Chicago"']), '"America/Chicago"')
        self.assertIsNone(fetch_events.extract_tzid(["VALUE=DATE"]))
        self.assertIsNone(fetch_events.extract_tzid([]))
        self.assertIsNone(fetch_events.extract_tzid(None))


class TimezoneConversionTests(unittest.TestCase):
    def test_parse_datetime_utc_trailing_z(self):
        # 2026-08-16 14:30:00 UTC
        utc_dt = datetime(2026, 8, 16, 14, 30, 0, tzinfo=timezone.utc)
        expected_local = utc_dt.astimezone().replace(tzinfo=None)

        all_day, parsed = fetch_events.parse_datetime_value("20260816T143000Z")
        self.assertFalse(all_day)
        self.assertEqual(parsed, expected_local)

    def test_parse_datetime_explicit_numeric_offsets(self):
        # Offset +02:00
        tz_plus2 = timezone(timedelta(hours=2))
        dt_plus2 = datetime(2026, 8, 16, 14, 30, 0, tzinfo=tz_plus2)
        expected_local = dt_plus2.astimezone().replace(tzinfo=None)

        all_day, parsed = fetch_events.parse_datetime_value("2026-08-16T14:30:00+02:00")
        self.assertFalse(all_day)
        self.assertEqual(parsed, expected_local)

        # Offset -05:00
        tz_minus5 = timezone(timedelta(hours=-5))
        dt_minus5 = datetime(2026, 8, 16, 14, 30, 0, tzinfo=tz_minus5)
        expected_local_minus5 = dt_minus5.astimezone().replace(tzinfo=None)

        all_day2, parsed2 = fetch_events.parse_datetime_value("20260816T143000-0500")
        self.assertFalse(all_day2)
        self.assertEqual(parsed2, expected_local_minus5)

    def test_parse_datetime_without_seconds(self):
        tz_plus1 = timezone(timedelta(hours=1))
        dt_plus1 = datetime(2026, 8, 16, 14, 30, 0, tzinfo=tz_plus1)
        expected_local = dt_plus1.astimezone().replace(tzinfo=None)

        all_day, parsed = fetch_events.parse_datetime_value("2026-08-16T14:30+01:00")
        self.assertFalse(all_day)
        self.assertEqual(parsed, expected_local)

    def test_parse_datetime_tzid_param(self):
        if ZoneInfo is None:
            self.skipTest("ZoneInfo not available")
        ny_tz = ZoneInfo("America/New_York")
        ny_dt = datetime(2026, 8, 16, 14, 30, 0, tzinfo=ny_tz)
        expected_local = ny_dt.astimezone().replace(tzinfo=None)

        all_day, parsed = fetch_events.parse_datetime_value("20260816T143000", ["TZID=America/New_York"])
        self.assertFalse(all_day)
        self.assertEqual(parsed, expected_local)

    def test_parse_datetime_windows_tz_alias_param(self):
        if ZoneInfo is None:
            self.skipTest("ZoneInfo not available")
        ny_tz = ZoneInfo("America/New_York")
        ny_dt = datetime(2026, 8, 16, 14, 30, 0, tzinfo=ny_tz)
        expected_local = ny_dt.astimezone().replace(tzinfo=None)

        all_day, parsed = fetch_events.parse_datetime_value("20260816T143000", ["TZID=Eastern Standard Time"])
        self.assertFalse(all_day)
        self.assertEqual(parsed, expected_local)

    def test_parse_datetime_floating_remains_naive(self):
        # Floating time per RFC 5545 (no Z, no offset, no TZID) is local to whoever views it
        all_day, parsed = fetch_events.parse_datetime_value("20260816T143000")
        self.assertFalse(all_day)
        self.assertEqual(parsed, datetime(2026, 8, 16, 14, 30, 0))

    def test_parse_datetime_all_day_formats(self):
        all_day, parsed = fetch_events.parse_datetime_value("20260816", ["VALUE=DATE"])
        self.assertTrue(all_day)
        self.assertEqual(parsed, datetime(2026, 8, 16, 0, 0, 0))

        all_day2, parsed2 = fetch_events.parse_datetime_value("20260816")
        self.assertTrue(all_day2)
        self.assertEqual(parsed2, datetime(2026, 8, 16, 0, 0, 0))

    def test_parse_jmap_datetime_with_timezone(self):
        if ZoneInfo is None:
            self.skipTest("ZoneInfo not available")
        ny_tz = ZoneInfo("America/New_York")
        ny_dt = datetime(2026, 8, 25, 10, 0, 0, tzinfo=ny_tz)
        expected_local = ny_dt.astimezone().replace(tzinfo=None)

        parsed = fetch_events.parse_jmap_datetime("2026-08-25T10:00:00", "America/New_York")
        self.assertEqual(parsed, expected_local)

    def test_parse_jmap_datetime_floating_and_date_only(self):
        parsed_floating = fetch_events.parse_jmap_datetime("2026-08-25T10:00:00")
        self.assertEqual(parsed_floating, datetime(2026, 8, 25, 10, 0, 0))

        parsed_date = fetch_events.parse_jmap_datetime("2026-08-25")
        self.assertEqual(parsed_date, datetime(2026, 8, 25, 0, 0, 0))


class IcalTimezoneIntegrationTests(unittest.TestCase):
    def test_parse_ics_converts_utc_events_to_local_wall_time(self):
        ics_data = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Example Corp.//EN
BEGIN:VEVENT
UID:utc-event-1@example.com
SUMMARY:UTC Team Standup
DTSTART:20260825T140000Z
DTEND:20260825T150000Z
END:VEVENT
END:VCALENDAR"""

        window_start = datetime(2026, 8, 1, 0, 0, 0)
        window_end = datetime(2026, 8, 31, 23, 59, 59)
        cal_info = {"name": "Work Calendar", "color": "#4A90E2"}

        events = fetch_events.parse_ics(ics_data, cal_info, window_start, window_end)
        self.assertEqual(len(events), 1)

        expected_start = datetime(2026, 8, 25, 14, 0, 0, tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
        expected_end = datetime(2026, 8, 25, 15, 0, 0, tzinfo=timezone.utc).astimezone().replace(tzinfo=None)

        self.assertEqual(events[0]["start_dt"], expected_start)
        self.assertEqual(events[0]["end_dt"], expected_end)
        self.assertEqual(events[0]["date_key"], expected_start.strftime("%Y-%m-%d"))

    def test_parse_ics_converts_tzid_events_to_local_wall_time(self):
        if ZoneInfo is None:
            self.skipTest("ZoneInfo not available")

        ics_data = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:tzid-event-1@example.com
SUMMARY:New York Meeting
DTSTART;TZID=America/New_York:20260825T090000
DTEND;TZID=America/New_York:20260825T100000
END:VEVENT
END:VCALENDAR"""

        window_start = datetime(2026, 8, 1, 0, 0, 0)
        window_end = datetime(2026, 8, 31, 23, 59, 59)
        cal_info = {"name": "NY Calendar", "color": "#4A90E2"}

        events = fetch_events.parse_ics(ics_data, cal_info, window_start, window_end)
        self.assertEqual(len(events), 1)

        ny_tz = ZoneInfo("America/New_York")
        expected_start = datetime(2026, 8, 25, 9, 0, 0, tzinfo=ny_tz).astimezone().replace(tzinfo=None)
        expected_end = datetime(2026, 8, 25, 10, 0, 0, tzinfo=ny_tz).astimezone().replace(tzinfo=None)

        self.assertEqual(events[0]["start_dt"], expected_start)
        self.assertEqual(events[0]["end_dt"], expected_end)
        self.assertEqual(events[0]["date_key"], expected_start.strftime("%Y-%m-%d"))

    def test_parse_ics_recurring_event_with_utc_until(self):
        ics_data = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:rec-daily@example.com
SUMMARY:Daily Standup
DTSTART:20260820T090000Z
DTEND:20260820T093000Z
RRULE:FREQ=DAILY;UNTIL=20260823T235959Z
END:VEVENT
END:VCALENDAR"""

        window_start = datetime(2026, 8, 1, 0, 0, 0)
        window_end = datetime(2026, 8, 31, 23, 59, 59)
        cal_info = {"name": "Daily", "color": "#4A90E2"}

        events = fetch_events.parse_ics(ics_data, cal_info, window_start, window_end)
        self.assertEqual(len(events), 4)  # 20th, 21st, 22nd, 23rd


class IcalRecurrenceOverrideTests(unittest.TestCase):
    """An override VEVENT (same UID + RECURRENCE-ID) replaces one occurrence."""

    MASTER = """BEGIN:VEVENT
UID:series-1@example.com
SUMMARY:Fortnightly meeting
DTSTART:20260826T150000
DTEND:20260826T160000
RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=WE;UNTIL=20261021T150000
END:VEVENT
"""

    def parse(self, *vevents):
        ics_data = "BEGIN:VCALENDAR\nVERSION:2.0\n" + "".join(vevents) + "END:VCALENDAR"
        return fetch_events.parse_ics(
            ics_data, {"name": "Work"}, datetime(2026, 8, 1), datetime(2026, 11, 30, 23, 59, 59)
        )

    @staticmethod
    def override(extra="", uid="series-1@example.com", rid="RECURRENCE-ID:20260923T150000",
                 start="20260923T150000", end="20260923T160000"):
        return (f"BEGIN:VEVENT\nUID:{uid}\n{rid}\nSUMMARY:Edited meeting\n"
                f"DTSTART:{start}\nDTEND:{end}\n{extra}END:VEVENT\n")

    @staticmethod
    def on(events, day):
        return [(e["title"], e["start_dt"].strftime("%H:%M")) for e in events if e["date_key"] == day]

    def test_edited_occurrence_replaces_master_instance(self):
        events = self.parse(self.MASTER, self.override())
        self.assertEqual(self.on(events, "2026-09-23"), [("Edited meeting", "15:00")])
        self.assertEqual(self.on(events, "2026-10-07"), [("Fortnightly meeting", "15:00")])

    def test_moved_occurrence_leaves_original_date(self):
        events = self.parse(self.MASTER, self.override(start="20260924T100000", end="20260924T110000"))
        self.assertEqual(self.on(events, "2026-09-23"), [])
        self.assertEqual(self.on(events, "2026-09-24"), [("Edited meeting", "10:00")])

    def test_cancelled_occurrence_removes_master_instance(self):
        events = self.parse(self.MASTER, self.override("STATUS:CANCELLED\n"))
        self.assertEqual(self.on(events, "2026-09-23"), [])
        self.assertEqual(len(events), 4)  # 26 Aug, 9 Sep, 7 Oct, 21 Oct

    def test_utc_recurrence_id_matches_tzid_series(self):
        if ZoneInfo is None:
            self.skipTest("ZoneInfo not available")
        # No BYDAY: the weekday comes from DTSTART, so this stays about matching
        # the override to its occurrence in whatever zone the tests run in.
        master = """BEGIN:VEVENT
UID:series-1@example.com
SUMMARY:Fortnightly meeting
DTSTART;TZID=America/New_York:20260826T150000
DTEND;TZID=America/New_York:20260826T160000
RRULE:FREQ=WEEKLY;INTERVAL=2;COUNT=5
END:VEVENT
"""
        # 15:00 EDT on 23 Sep is 19:00 UTC.
        events = self.parse(master, self.override(
            "STATUS:CANCELLED\n", rid="RECURRENCE-ID:20260923T190000Z",
            start="20260923T190000Z", end="20260923T200000Z"))
        occurrence = datetime(2026, 9, 23, 15, tzinfo=ZoneInfo("America/New_York")).astimezone()
        self.assertEqual(self.on(events, occurrence.strftime("%Y-%m-%d")), [])
        self.assertEqual(len(events), 4)

    def test_override_for_another_uid_leaves_series_intact(self):
        events = self.parse(self.MASTER, self.override("STATUS:CANCELLED\n", uid="other@example.com"))
        self.assertEqual(self.on(events, "2026-09-23"), [("Fortnightly meeting", "15:00")])



class GoogleAndJmapTimezoneIntegrationTests(unittest.TestCase):
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

    def test_google_api_timezone_conversion(self):
        if ZoneInfo is None:
            self.skipTest("ZoneInfo not available")

        api_payload = json.dumps({
            "items": [
                {
                    "id": "g_evt_1",
                    "summary": "Google Meeting",
                    "start": {
                        "dateTime": "2026-08-25T10:00:00-04:00",
                        "timeZone": "America/New_York",
                    },
                    "end": {
                        "dateTime": "2026-08-25T11:00:00-04:00",
                        "timeZone": "America/New_York",
                    },
                }
            ]
        }).encode("utf-8")

        cal_info = {"name": "Google Cal", "googleCalendarId": "primary"}
        window_start = datetime(2026, 8, 1, 0, 0, 0)
        window_end = datetime(2026, 8, 31, 23, 59, 59)

        with mock.patch.object(fetch_events, "get_google_access_token", return_value="fake-token"), \
             mock.patch.object(fetch_events.urllib.request, "urlopen", return_value=self.FakeResponse("https://googleapis.com/cal", api_payload)):
            result = fetch_events.fetch_google_api_calendar(cal_info, window_start, window_end)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["events"]), 1)
        event = result["events"][0]

        ny_tz = ZoneInfo("America/New_York")
        expected_start = datetime(2026, 8, 25, 10, 0, 0, tzinfo=ny_tz).astimezone().replace(tzinfo=None)
        expected_end = datetime(2026, 8, 25, 11, 0, 0, tzinfo=ny_tz).astimezone().replace(tzinfo=None)

        self.assertEqual(event["start_dt"], expected_start)
        self.assertEqual(event["end_dt"], expected_end)
        self.assertEqual(event["date_key"], expected_start.strftime("%Y-%m-%d"))

    def test_jmap_calendar_timezone_conversion(self):
        if ZoneInfo is None:
            self.skipTest("ZoneInfo not available")

        raw_events = [
            {
                "id": "jmap_tz_1",
                "title": "JMAP Planning",
                "start": "2026-08-25T14:00:00",
                "timeZone": "America/New_York",
                "duration": "PT1H",
            }
        ]
        cal_info = {"name": "JMAP Cal", "type": "jmap", "jmapUrl": "https://calendar.example/session", "jmapToken": "secret"}
        window_start = datetime(2026, 8, 1, 0, 0, 0)
        window_end = datetime(2026, 8, 31, 23, 59, 59)

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

        fake_session = self.FakeResponse("https://calendar.example/session", session_json)
        fake_api = self.FakeResponse("https://calendar.example/api", query_get_json)

        opener = mock.Mock()
        opener.open.side_effect = [fake_session, fake_api]

        with mock.patch.object(fetch_events.urllib.request, "build_opener", return_value=opener):
            result = fetch_events.fetch_jmap_calendar(cal_info, window_start, window_end)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["events"]), 1)
        event = result["events"][0]

        ny_tz = ZoneInfo("America/New_York")
        expected_start = datetime(2026, 8, 25, 14, 0, 0, tzinfo=ny_tz).astimezone().replace(tzinfo=None)
        expected_end = datetime(2026, 8, 25, 15, 0, 0, tzinfo=ny_tz).astimezone().replace(tzinfo=None)

        self.assertEqual(event["start_dt"], expected_start)
        self.assertEqual(event["end_dt"], expected_end)
        self.assertEqual(event["date_key"], expected_start.strftime("%Y-%m-%d"))


if __name__ == "__main__":
    unittest.main()
