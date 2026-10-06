import importlib.util
import io
import json
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("fetch_events", ROOT / "fetch-events.py")
fetch_events = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetch_events)
# Keep the sync cache out of the real ~/.local/state.
fetch_events.SYNC_CACHE_DIR = tempfile.mkdtemp(prefix="chronica-test-cache-")

WINDOW = (datetime(2026, 9, 1), datetime(2026, 9, 30))


def parse(body):
    ics = "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\n" + body + "END:VEVENT\r\nEND:VCALENDAR\r\n"
    return fetch_events.parse_ics(ics, {"name": "Feed"}, *WINDOW)


class _Resp:
    def __init__(self, obj, headers=None):
        self.stream = io.BytesIO(obj.encode() if isinstance(obj, str) else json.dumps(obj).encode())
        self.headers = headers or {}

    def read(self, size=-1):
        return self.stream.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class IcsParsingTests(unittest.TestCase):
    def test_value_date_time_is_not_all_day(self):
        events = parse("UID:a\r\nSUMMARY:Call\r\nDTSTART;VALUE=DATE-TIME:20260910T140000\r\n"
                       "DTEND;VALUE=DATE-TIME:20260910T150000\r\n")
        self.assertEqual(len(events), 1)
        self.assertFalse(events[0]["all_day"])
        self.assertEqual(events[0]["start_dt"].hour, 14)

    def test_valarm_does_not_overwrite_event_fields(self):
        events = parse("UID:a\r\nSUMMARY:Dentist\r\nDESCRIPTION:Bring card\r\n"
                       "DTSTART:20260910T140000\r\nDTEND:20260910T150000\r\n"
                       "BEGIN:VALARM\r\nACTION:DISPLAY\r\nDESCRIPTION:This is an event reminder\r\n"
                       "SUMMARY:Alarm\r\nTRIGGER:-PT10M\r\nEND:VALARM\r\n")
        self.assertEqual(events[0]["title"], "Dentist")
        self.assertEqual(events[0]["description"], "Bring card")

    def test_unescape_keeps_escaped_backslash_before_n(self):
        self.assertEqual(fetch_events.unescape_ical_text(r"C:\\new\nline\, ok"), "C:\\new\nline, ok")

    def test_ics_escape_lone_carriage_return(self):
        self.assertEqual(fetch_events.ics_escape("a\rb"), "a\\nb")


class GooglePagingTests(unittest.TestCase):
    CAL = {"name": "Work", "googleCalendarId": "work@example.com"}

    def item(self, i):
        return {"id": f"e{i}", "summary": f"E{i}",
                "start": {"dateTime": "2026-09-10T14:00:00Z"}, "end": {"dateTime": "2026-09-10T15:00:00Z"}}

    def test_follows_next_page_token_and_reads_access_role(self):
        pages = [_Resp({"items": [self.item(1)], "nextPageToken": "p2", "accessRole": "reader"}),
                 _Resp({"items": [self.item(2)], "accessRole": "reader"})]
        with mock.patch.object(fetch_events, "get_google_access_token", return_value="tok"), \
             mock.patch.object(fetch_events.urllib.request, "urlopen", side_effect=pages) as urlopen:
            result = fetch_events.fetch_google_api_calendar(self.CAL, *WINDOW)
        self.assertEqual([e["id"] for e in result["events"]], ["e1", "e2"])
        self.assertIn("pageToken=p2", urlopen.call_args_list[1][0][0].full_url)
        self.assertFalse(result["writable"])
        self.assertFalse(result["events"][0]["writable"])


def not_modified(url="https://example.com/"):
    return fetch_events.urllib.error.HTTPError(url, 304, "Not Modified", {}, None)


class ConditionalSyncTests(unittest.TestCase):
    ICS = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:a\r\nSUMMARY:Gym\r\n"
           "DTSTART:20260910T070000\r\nDTEND:20260910T080000\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")

    def setUp(self):
        patcher = mock.patch.object(fetch_events, "SYNC_CACHE_DIR", tempfile.mkdtemp())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_ics_304_reuses_the_cached_feed(self):
        cal = {"name": "Feed", "url": "https://example.com/cal.ics"}
        with mock.patch.object(fetch_events.urllib.request, "urlopen",
                               return_value=_Resp(self.ICS, {"ETag": '"v1"'})):
            fetch_events.fetch_calendar(cal, *WINDOW)
        with mock.patch.object(fetch_events.urllib.request, "urlopen", side_effect=not_modified()) as urlopen:
            result = fetch_events.fetch_calendar(cal, *WINDOW)
        self.assertEqual(urlopen.call_args[0][0].get_header("If-none-match"), '"v1"')
        self.assertEqual([e["title"] for e in result["events"]], ["Gym"])

    def test_google_304_reuses_the_cached_list(self):
        cal = {"name": "Work", "googleCalendarId": "work@example.com"}
        item = {"id": "e1", "summary": "Sync", "start": {"dateTime": "2026-09-10T14:00:00Z"},
                "end": {"dateTime": "2026-09-10T15:00:00Z"}}
        with mock.patch.object(fetch_events, "get_google_access_token", return_value="tok"):
            with mock.patch.object(fetch_events.urllib.request, "urlopen",
                                   return_value=_Resp({"items": [item], "accessRole": "reader"}, {"ETag": '"g1"'})):
                fetch_events.fetch_google_api_calendar(cal, *WINDOW)
            with mock.patch.object(fetch_events.urllib.request, "urlopen", side_effect=not_modified()) as urlopen:
                result = fetch_events.fetch_google_api_calendar(cal, *WINDOW)
        self.assertEqual(urlopen.call_args[0][0].get_header("If-none-match"), '"g1"')
        self.assertEqual([e["id"] for e in result["events"]], ["e1"])
        self.assertFalse(result["writable"])

    def test_jmap_reuses_session_and_list_when_nothing_changed(self):
        cal = {"name": "Fastmail", "type": "jmap", "jmapUrl": "https://api.example.com/jmap/session", "jmapToken": "t"}
        session = {"apiUrl": "https://api.example.com/jmap/api", "primaryAccounts": {"urn:ietf:params:jmap:calendars": "A1"}}
        event = {"id": "j1", "title": "Review", "start": "2026-09-10T10:00:00", "duration": "PT1H"}
        listing = {"methodResponses": [["CalendarEvent/query", {"ids": ["j1"]}, "q0"],
                                       ["CalendarEvent/get", {"list": [event], "state": "s1"}, "get0"]]}
        unchanged = {"methodResponses": [["CalendarEvent/changes",
                                          {"oldState": "s1", "newState": "s1", "created": [], "updated": [], "destroyed": []}, "c0"]]}
        sent = []

        def answer(responses):
            def fake_open(opener, request, origin, timeout=None):
                sent.append(json.loads(request.data)["methodCalls"][0][0] if request.data else "session")
                return _Resp(responses.pop(0))
            return mock.patch.object(fetch_events, "open_trusted_jmap", fake_open)

        with answer([session, listing]):
            first = fetch_events.fetch_jmap_calendar(cal, *WINDOW)
        with answer([unchanged]):
            second = fetch_events.fetch_jmap_calendar(cal, *WINDOW)
        self.assertEqual(sent, ["session", "CalendarEvent/query", "CalendarEvent/changes"])
        self.assertEqual([e["title"] for e in first["events"]], ["Review"])
        self.assertEqual([e["title"] for e in second["events"]], ["Review"])


class EventFormValidationTests(unittest.TestCase):
    def test_end_before_start_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "after the start"):
            fetch_events.validate_event_times({"start": "2026-09-10T23:00:00", "end": "2026-09-10T01:00:00"})

    def test_all_day_same_date_is_accepted(self):
        fetch_events.validate_event_times({"start": "2026-09-10", "end": "2026-09-10", "allDay": True})


if __name__ == "__main__":
    unittest.main()
