import importlib.util
import io
import json
from datetime import datetime
from pathlib import Path
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("fetch_events", ROOT / "fetch-events.py")
fetch_events = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetch_events)

WINDOW = (datetime(2026, 9, 1), datetime(2026, 9, 30))


def parse(body):
    ics = "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\n" + body + "END:VEVENT\r\nEND:VCALENDAR\r\n"
    return fetch_events.parse_ics(ics, {"name": "Feed"}, *WINDOW)


class _Resp:
    def __init__(self, obj):
        self.stream = io.BytesIO(json.dumps(obj).encode())

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


if __name__ == "__main__":
    unittest.main()
