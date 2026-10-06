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
# Keep the sync cache out of the real ~/.local/state, and the keyring off:
# a test must never write to the desktop keyring.
fetch_events.SYNC_CACHE_DIR = tempfile.mkdtemp(prefix="chronica-test-cache-")
fetch_events._secret_tool = lambda args, value=None: None

ICLOUD = {
    "name": "Apple iCloud",
    "url": "webcal://p01-caldav.icloud.com/published/2/abc",
    "caldavUrl": "https://p01-caldav.icloud.com/1234/calendars/home",
    "username": "someone@icloud.com",
    "password": "abcd-efgh-ijkl-mnop",
}
GOOGLE = {"name": "Work", "googleCalendarId": "work@group.calendar.google.com"}
JMAP = {"name": "Fastmail", "type": "jmap", "jmapToken": "tok", "jmapUrl": "https://api.fastmail.com/jmap/session"}

EDIT = {
    "title": "Dentist (moved)",
    "start": "2026-09-14T16:00:00",
    "end": "2026-09-14T17:00:00",
    "allDay": False,
    "location": "",
    "description": "",
}


class FakeResponse:
    def __init__(self, body=b"", headers=None):
        self.stream = io.BytesIO(body if isinstance(body, bytes) else body.encode("utf-8"))
        self.headers = headers or {}
        self.url = "https://p01-caldav.icloud.com/"

    def read(self, size=-1):
        return self.stream.read(size)

    def geturl(self):
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def capture_transport(responses):
    """Patch the origin-pinned transport shared by CalDAV and JMAP writes."""
    sent = []

    def fake_open(opener, request, origin, timeout=None):
        sent.append(request)
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    return sent, mock.patch.object(fetch_events, "open_trusted_jmap", fake_open)


STORED_ICS = "\r\n".join([
    "BEGIN:VCALENDAR",
    "VERSION:2.0",
    "PRODID:-//Apple Inc.//macOS//EN",
    "BEGIN:VEVENT",
    "UID:dentist-1",
    "DTSTAMP:20260901T080000Z",
    "SEQUENCE:3",
    "SUMMARY:Dentist",
    "DTSTART;TZID=Europe/Paris:20260914T090000",
    "DTEND;TZID=Europe/Paris:20260914T100000",
    "LOCATION:Rue de Rivoli",
    "X-APPLE-STRUCTURED-LOCATION;VALUE=URI:geo:48.85,2.35",
    "ATTENDEE;CN=Dr Smith:mailto:smith@example.com",
    "BEGIN:VALARM",
    "ACTION:DISPLAY",
    "DESCRIPTION:Reminder",
    "TRIGGER:-PT15M",
    "END:VALARM",
    "END:VEVENT",
    "END:VCALENDAR",
]) + "\r\n"


class CaldavEditTests(unittest.TestCase):
    def test_edit_rewrites_form_fields_and_keeps_everything_else(self):
        sent, patch = capture_transport([
            FakeResponse(STORED_ICS, headers={"ETag": '"v3"'}),
            FakeResponse(b""),
        ])
        with patch:
            res = fetch_events.update_caldav_event(ICLOUD, "dentist-1", EDIT)

        self.assertEqual(res["status"], "success")
        self.assertEqual([r.get_method() for r in sent], ["GET", "PUT"])
        put = sent[1]
        self.assertEqual(put.full_url, ICLOUD["caldavUrl"] + "/dentist-1.ics")
        self.assertEqual(put.get_header("If-match"), '"v3"')

        lines = fetch_events.unfold_lines(put.data.decode("utf-8"))
        self.assertIn("UID:dentist-1", lines)
        self.assertIn("SUMMARY:Dentist (moved)", lines)
        self.assertIn("SEQUENCE:4", lines)
        self.assertIn("ATTENDEE;CN=Dr Smith:mailto:smith@example.com", lines)
        # The alarm survives with its own DESCRIPTION intact.
        self.assertIn("TRIGGER:-PT15M", lines)
        self.assertIn("DESCRIPTION:Reminder", lines)
        for gone in ("SUMMARY:Dentist", "LOCATION:Rue de Rivoli", "SEQUENCE:3"):
            self.assertNotIn(gone, lines)
        self.assertFalse(any(l.startswith("X-APPLE-STRUCTURED-LOCATION") for l in lines))
        self.assertEqual(sum(l.startswith("DTSTART") for l in lines), 1)
        self.assertEqual(sum(l.startswith("DTEND") for l in lines), 1)
        self.assertEqual(lines.count("BEGIN:VEVENT"), 1)

    def test_a_change_made_elsewhere_meanwhile_is_reported_not_overwritten(self):
        conflict = urllib.error.HTTPError(ICLOUD["caldavUrl"], 412, "Precondition Failed", {}, None)
        _, patch = capture_transport([FakeResponse(STORED_ICS, headers={"ETag": '"v3"'}), conflict])
        with patch, self.assertRaises(ValueError) as ctx:
            fetch_events.update_caldav_event(ICLOUD, "dentist-1", EDIT)
        self.assertIn("changed elsewhere", str(ctx.exception))

    def test_recurring_series_is_refused_before_anything_is_written(self):
        recurring = STORED_ICS.replace("SEQUENCE:3", "RRULE:FREQ=WEEKLY")
        sent, patch = capture_transport([FakeResponse(recurring)])
        with patch, self.assertRaises(ValueError):
            fetch_events.update_caldav_event(ICLOUD, "dentist-1", EDIT)
        self.assertEqual([r.get_method() for r in sent], ["GET"])

    def test_event_at_a_server_chosen_href_is_located_by_uid(self):
        missing = urllib.error.HTTPError(ICLOUD["caldavUrl"], 404, "Not Found", {}, None)
        multistatus = """<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"><d:response>
          <d:href>/1234/calendars/home/ABC-123.ics</d:href></d:response></d:multistatus>"""
        sent, patch = capture_transport([
            missing, FakeResponse(multistatus), FakeResponse(STORED_ICS), FakeResponse(b""),
        ])
        with patch:
            res = fetch_events.update_caldav_event(ICLOUD, "dentist-1", EDIT)
        self.assertEqual(res["status"], "success")
        self.assertEqual([r.get_method() for r in sent], ["GET", "REPORT", "GET", "PUT"])
        self.assertEqual(sent[3].full_url, "https://p01-caldav.icloud.com/1234/calendars/home/ABC-123.ics")
        self.assertIsNone(sent[3].get_header("If-match"))


class GoogleEditTests(unittest.TestCase):
    def _patch(self, event_data):
        with mock.patch.object(fetch_events, "get_google_access_token", return_value="tok"), \
             mock.patch.object(fetch_events, "get_local_tz_name", return_value="Europe/Paris"), \
             mock.patch.object(fetch_events.urllib.request, "urlopen",
                               return_value=FakeResponse(b'{"id": "evt123"}')) as urlopen:
            fetch_events.update_google_event(GOOGLE, "evt123_20260914T070000Z", event_data)
        request = urlopen.call_args[0][0]
        return request, json.loads(request.data.decode("utf-8"))

    def test_patch_targets_the_occurrence_and_clears_emptied_fields(self):
        request, body = self._patch(EDIT)
        self.assertEqual(request.get_method(), "PATCH")
        self.assertTrue(request.full_url.endswith(
            "/calendars/work%40group.calendar.google.com/events/evt123_20260914T070000Z"))
        self.assertEqual(body["summary"], "Dentist (moved)")
        self.assertEqual(body["location"], "")
        self.assertEqual(body["description"], "")
        # A patch merges nested objects: the all-day form must be nulled out.
        self.assertIsNone(body["start"]["date"])
        self.assertEqual(body["start"]["timeZone"], "Europe/Paris")

    def test_switching_to_all_day_nulls_the_timed_form(self):
        _, body = self._patch(dict(EDIT, allDay=True, start="2026-09-14", end="2026-09-14"))
        self.assertEqual(body["start"], {"date": "2026-09-14", "dateTime": None, "timeZone": None})
        self.assertEqual(body["end"]["date"], "2026-09-15")


class JmapEditTests(unittest.TestCase):
    SESSION = json.dumps({
        "apiUrl": "https://api.fastmail.com/jmap/api",
        "primaryAccounts": {"urn:ietf:params:jmap:calendars": "acc"},
    }).encode()

    def _update(self, set_reply):
        reply = json.dumps({"methodResponses": [["CalendarEvent/set", set_reply, "upd0"]]}).encode()
        sent, patch = capture_transport([FakeResponse(self.SESSION), FakeResponse(reply)])
        with patch, mock.patch.object(fetch_events, "get_local_tz_name", return_value="Europe/Paris"):
            res = fetch_events.update_jmap_event(JMAP, "ev1", EDIT)
        return res, json.loads(sent[1].data.decode("utf-8"))

    def test_update_replaces_form_properties_and_drops_the_location(self):
        res, payload = self._update({"updated": {"ev1": None}})
        self.assertEqual(res["status"], "success")
        method, args, _ = payload["methodCalls"][0]
        self.assertEqual(method, "CalendarEvent/set")
        patch = args["update"]["ev1"]
        self.assertEqual(patch["start"], "2026-09-14T16:00:00")
        self.assertEqual(patch["duration"], "PT1H")
        self.assertEqual(patch["timeZone"], "Europe/Paris")
        self.assertIsNone(patch["locations"])
        self.assertNotIn("calendarIds", patch)

    def test_server_rejection_is_surfaced(self):
        with self.assertRaises(ValueError) as ctx:
            self._update({"notUpdated": {"ev1": {"type": "invalidProperties", "description": "bad start"}}})
        self.assertIn("bad start", str(ctx.exception))


class LocalEditAndDispatchTests(unittest.TestCase):
    def test_update_event_rewrites_the_local_entry_and_rejects_bad_times(self):
        with tempfile.TemporaryDirectory() as directory:
            local_path = os.path.join(directory, "local-events.json")
            fetch_events.write_secure_json(local_path, [
                {"id": "loc_1", "title": "Dentist", "start": "2026-09-14T09:00:00",
                 "end": "2026-09-14T10:00:00", "allDay": False, "location": "Old", "createdAt": 1},
            ])
            payload = dict(EDIT, id="loc_1", calendar="Local Calendar", calendarType="local")
            with mock.patch.object(fetch_events, "LOCAL_EVENTS_PATH", local_path), \
                 mock.patch.object(fetch_events, "find_calendar_config", return_value={"name": "Local Calendar", "type": "local"}), \
                 mock.patch.object(fetch_events, "sync_all_events"):
                res = fetch_events.update_event(payload)
                with self.assertRaises(ValueError):
                    fetch_events.update_event(dict(payload, start="2026-09-14T4pm:00"))
                saved = fetch_events.safe_load_json(local_path)

        self.assertEqual(res["status"], "success")
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["id"], "loc_1")
        self.assertEqual(saved[0]["createdAt"], 1)
        self.assertEqual(saved[0]["start"], "2026-09-14T16:00:00")
        self.assertEqual(saved[0]["location"], "")

    def test_read_only_feed_events_cannot_be_edited(self):
        with mock.patch.object(fetch_events, "find_calendar_config", return_value={"name": "Feed", "url": "https://x/y.ics"}), \
             self.assertRaises(ValueError):
            fetch_events.update_event(dict(EDIT, id="uid-1", calendar="Feed", calendarType="ical"))


class EditabilityTests(unittest.TestCase):
    """Only events the one-day form can represent are offered for editing."""

    ICS = "\r\n".join([
        "BEGIN:VCALENDAR",
        "BEGIN:VEVENT", "UID:single", "SUMMARY:Single",
        "DTSTART:20260914T090000", "DTEND:20260914T100000", "END:VEVENT",
        "BEGIN:VEVENT", "UID:weekly", "SUMMARY:Weekly", "RRULE:FREQ=WEEKLY;COUNT=2",
        "DTSTART:20260915T090000", "DTEND:20260915T100000", "END:VEVENT",
        "BEGIN:VEVENT", "UID:trip", "SUMMARY:Trip",
        "DTSTART;VALUE=DATE:20260916", "DTEND;VALUE=DATE:20260919", "END:VEVENT",
        "BEGIN:VEVENT", "UID:holiday", "SUMMARY:Holiday",
        "DTSTART;VALUE=DATE:20260920", "DTEND;VALUE=DATE:20260921", "END:VEVENT",
        "END:VCALENDAR",
    ])

    def test_single_day_events_are_editable_series_and_spans_are_not(self):
        window = (fetch_events.datetime(2026, 9, 1), fetch_events.datetime(2026, 9, 30))
        events = fetch_events.parse_ics(self.ICS, ICLOUD, *window)
        with tempfile.TemporaryDirectory() as directory:
            cfg_path = os.path.join(directory, "calendars.json")
            out_path = os.path.join(directory, "calendar-events.json")
            fetch_events.write_secure_json(cfg_path, [ICLOUD])
            with mock.patch.object(fetch_events, "CONFIG_PATH", cfg_path), \
                 mock.patch.object(fetch_events, "OUTPUT_PATH", out_path), \
                 mock.patch.object(fetch_events, "LOCAL_EVENTS_PATH", os.path.join(directory, "none.json")), \
                 mock.patch.object(fetch_events, "STATE_DIR", directory), \
                 mock.patch.object(fetch_events, "fetch_calendar_item",
                                   return_value={"name": ICLOUD["name"], "events": events, "status": "ok"}):
                fetch_events.sync_all_events()
                saved = fetch_events.safe_load_json(out_path, max_bytes=fetch_events.MAX_OUTPUT_JSON_BYTES)

        editable = {
            (e["title"], day): e["editable"]
            for day, day_events in saved["eventsByDate"].items() for e in day_events
        }
        self.assertTrue(editable[("Single", "2026-09-14")])
        self.assertTrue(editable[("Holiday", "2026-09-20")])
        self.assertFalse(editable[("Weekly", "2026-09-15")])
        self.assertFalse(editable[("Trip", "2026-09-17")])
        single = saved["eventsByDate"]["2026-09-14"][0]
        self.assertEqual(single["endIso"], "2026-09-14T10:00:00")


if __name__ == "__main__":
    unittest.main()
