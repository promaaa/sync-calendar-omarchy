import base64
import importlib.util
import json
import os
from pathlib import Path
import time
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

ICLOUD = {
    "name": "Apple iCloud",
    "url": "webcal://p01-caldav.icloud.com/published/2/abc",
    "caldavUrl": "https://p01-caldav.icloud.com/1234/calendars/home",
    "username": "someone@icloud.com",
    "password": "abcd-efgh-ijkl-mnop",
}


class FakeResponse:
    """Minimal stand-in for the urllib response CalDAV calls read from."""

    def __init__(self, body="", status=200, url="https://p01-caldav.icloud.com/"):
        self.payload = body.encode("utf-8")
        self.offset = 0
        self.status = status
        self.url = url

    def read(self, size=-1):
        if size < 0:
            size = len(self.payload) - self.offset
        chunk = self.payload[self.offset:self.offset + size]
        self.offset += len(chunk)
        return chunk

    def geturl(self):
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def capture_requests(responses):
    """Patch the transport, returning the list that collects sent Requests."""
    sent = []

    def fake_open(opener, request, origin, timeout=None):
        sent.append(request)
        result = responses.pop(0) if responses else FakeResponse()
        if isinstance(result, Exception):
            raise result
        return result

    return sent, mock.patch.object(fetch_events, "open_trusted_jmap", fake_open)


class VeventBuildTests(unittest.TestCase):
    """Stamps are written in UTC, so the suite pins a zone to assert against."""

    @classmethod
    def setUpClass(cls):
        cls._tz = os.environ.get("TZ")
        os.environ["TZ"] = "Asia/Seoul"
        time.tzset()

    @classmethod
    def tearDownClass(cls):
        if cls._tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = cls._tz
        time.tzset()

    def test_timed_event_is_serialized_as_utc(self):
        # 09:00 in a +09:00 zone is midnight UTC.
        ics = fetch_events.build_vevent(
            {"title": "Standup", "start": "2026-09-14T09:00:00", "end": "2026-09-14T09:30:00"},
            "uid-1",
        )
        self.assertIn("UID:uid-1", ics)
        self.assertIn("DTSTART:20260914T000000Z", ics)
        self.assertIn("DTEND:20260914T003000Z", ics)
        self.assertTrue(ics.startswith("BEGIN:VCALENDAR\r\n"))
        self.assertTrue(ics.rstrip().endswith("END:VCALENDAR"))

    def test_all_day_event_gets_an_exclusive_end_date(self):
        ics = fetch_events.build_vevent(
            {"title": "Holiday", "start": "2026-09-14", "end": "2026-09-14", "allDay": True},
            "uid-2",
        )
        self.assertIn("DTSTART;VALUE=DATE:20260914", ics)
        self.assertIn("DTEND;VALUE=DATE:20260915", ics)

    def test_end_before_start_is_pushed_out_instead_of_inverted(self):
        ics = fetch_events.build_vevent(
            {"title": "Oops", "start": "2026-09-14T09:00:00", "end": "2026-09-14T08:00:00"},
            "uid-3",
        )
        self.assertIn("DTSTART:20260914T000000Z", ics)
        self.assertIn("DTEND:20260914T010000Z", ics)

    def test_special_characters_cannot_break_out_of_a_content_line(self):
        ics = fetch_events.build_vevent(
            {
                "title": "Lunch; with, a\nfriend",
                "start": "2026-09-14T12:00:00",
                "description": "back\\slash",
            },
            "uid-4",
        )
        self.assertIn("SUMMARY:Lunch\; with\\, a\\nfriend", ics)
        self.assertIn("DESCRIPTION:back\\\\slash", ics)
        for line in ics.split("\r\n"):
            self.assertNotIn("\n", line)

    def test_long_lines_are_folded_without_splitting_a_character(self):
        ics = fetch_events.build_vevent(
            {"title": "é" * 200, "start": "2026-09-14T12:00:00"}, "uid-5"
        )
        for line in ics.split("\r\n"):
            self.assertLessEqual(len(line.encode("utf-8")), 75)
        # Unfolding must give the summary back intact.
        unfolded = fetch_events.unfold_lines(ics)
        summary = [l for l in unfolded if l.startswith("SUMMARY:")][0]
        self.assertEqual(summary, "SUMMARY:" + "é" * 200)


class CaldavCredentialTests(unittest.TestCase):
    def test_plaintext_and_malformed_urls_are_refused(self):
        for url in (
            "http://p01-caldav.icloud.com/1234/calendars/home",
            "https://user:pass@p01-caldav.icloud.com/1234/",
            "https://p01-caldav.icloud.com/1234/#frag",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                fetch_events.caldav_auth(ICLOUD, url)

    def test_credentials_cannot_smuggle_header_or_field_separators(self):
        for bad in ({"username": "a:b"}, {"username": "a\r\nX-Evil: 1"}, {"password": "p\nq"}):
            cal = dict(ICLOUD, **bad)
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                fetch_events.caldav_auth(cal, ICLOUD["caldavUrl"])

    def test_a_feed_without_credentials_is_not_writable(self):
        feed = {"name": "Apple iCloud", "url": ICLOUD["url"]}
        self.assertFalse(fetch_events.has_caldav_write(feed))
        self.assertTrue(fetch_events.has_caldav_write(ICLOUD))
        with self.assertRaises(ValueError):
            fetch_events.caldav_credentials(feed)

    def test_collection_url_is_normalized_and_authorized(self):
        url, origin, auth = fetch_events.caldav_auth(ICLOUD, ICLOUD["caldavUrl"])
        self.assertTrue(url.endswith("/"))
        self.assertEqual(origin, ("https", "p01-caldav.icloud.com", 443))
        decoded = base64.b64decode(auth.split(" ", 1)[1]).decode("utf-8")
        self.assertEqual(decoded, ICLOUD["username"] + ":" + ICLOUD["password"])


class CaldavWriteTests(unittest.TestCase):
    def test_create_puts_an_ics_resource_named_after_its_uid(self):
        sent, patch = capture_requests([FakeResponse(status=201)])
        with patch:
            res = fetch_events.create_caldav_event(
                ICLOUD, {"title": "Dentist", "start": "2026-09-14T09:00:00", "end": "2026-09-14T10:00:00"}
            )
        self.assertEqual(res["status"], "success")
        self.assertEqual(len(sent), 1)
        req = sent[0]
        self.assertEqual(req.get_method(), "PUT")
        self.assertEqual(req.full_url, ICLOUD["caldavUrl"] + "/" + res["id"] + ".ics")
        self.assertEqual(req.get_header("Content-type"), "text/calendar; charset=utf-8")
        self.assertEqual(req.get_header("If-none-match"), "*")
        self.assertIn("Basic ", req.get_header("Authorization"))
        self.assertIn("UID:" + res["id"], req.data.decode("utf-8"))

    def test_rejected_credentials_become_an_actionable_message(self):
        error = urllib.error.HTTPError(ICLOUD["caldavUrl"], 401, "Unauthorized", {}, None)
        _, patch = capture_requests([error])
        with patch, self.assertRaises(ValueError) as ctx:
            fetch_events.create_caldav_event(ICLOUD, {"title": "x", "start": "2026-09-14T09:00:00"})
        self.assertIn("app-specific password", str(ctx.exception))

    def test_create_requires_a_start(self):
        with self.assertRaises(ValueError):
            fetch_events.create_caldav_event(ICLOUD, {"title": "No date"})

    def test_delete_falls_back_to_a_uid_lookup_when_the_href_differs(self):
        multistatus = """<?xml version="1.0"?>
        <d:multistatus xmlns:d="DAV:">
          <d:response><d:href>/1234/calendars/home/server-chosen-name.ics</d:href></d:response>
        </d:multistatus>"""
        missing = urllib.error.HTTPError(ICLOUD["caldavUrl"], 404, "Not Found", {}, None)
        sent, patch = capture_requests([missing, FakeResponse(multistatus, status=207), FakeResponse(status=204)])
        with patch:
            res = fetch_events.delete_caldav_event(ICLOUD, "apple-made-this")
        self.assertEqual(res["status"], "success")
        self.assertEqual([r.get_method() for r in sent], ["DELETE", "REPORT", "DELETE"])
        self.assertIn("apple-made-this", sent[1].data.decode("utf-8"))
        self.assertEqual(
            sent[2].full_url,
            "https://p01-caldav.icloud.com/1234/calendars/home/server-chosen-name.ics",
        )

    def test_delete_reports_a_miss_when_the_server_knows_no_such_uid(self):
        missing = urllib.error.HTTPError(ICLOUD["caldavUrl"], 404, "Not Found", {}, None)
        empty = """<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"></d:multistatus>"""
        _, patch = capture_requests([missing, FakeResponse(empty, status=207)])
        with patch:
            res = fetch_events.delete_caldav_event(ICLOUD, "ghost")
        self.assertEqual(res["status"], "error")
        self.assertIn("ghost", res["message"])


class CaldavDiscoveryTests(unittest.TestCase):
    def test_discovery_walks_principal_then_home_then_collections(self):
        principal = """<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"><d:response>
          <d:propstat><d:prop><d:current-user-principal><d:href>/1234/principal/</d:href>
          </d:current-user-principal></d:prop></d:propstat></d:response></d:multistatus>"""
        home = """<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
          <d:response><d:propstat><d:prop><c:calendar-home-set><d:href>/1234/calendars/</d:href>
          </c:calendar-home-set></d:prop></d:propstat></d:response></d:multistatus>"""
        collections = """<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
          <d:response><d:href>/1234/calendars/</d:href><d:propstat><d:prop>
            <d:displayname>home</d:displayname><d:resourcetype><d:collection/></d:resourcetype>
          </d:prop></d:propstat></d:response>
          <d:response><d:href>/1234/calendars/work/</d:href><d:propstat><d:prop>
            <d:displayname>Work</d:displayname>
            <d:resourcetype><d:collection/><c:calendar/></d:resourcetype>
          </d:prop></d:propstat></d:response>
        </d:multistatus>"""
        sent, patch = capture_requests([
            FakeResponse(principal, status=207),
            FakeResponse(home, status=207),
            FakeResponse(collections, status=207),
        ])
        with patch:
            found = fetch_events.caldav_discover(ICLOUD)
        self.assertEqual([r.get_method() for r in sent], ["PROPFIND"] * 3)
        self.assertEqual(sent[2].get_header("Depth"), "1")
        # Only the real calendar collection is offered, not its parent container.
        self.assertEqual(
            found,
            [{"name": "Work", "caldavUrl": "https://p01-caldav.icloud.com/1234/calendars/work/"}],
        )


class WritableCalendarListTests(unittest.TestCase):
    def test_a_caldav_entry_is_offered_as_a_push_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = os.path.join(tmp, "calendars.json")
            with open(config, "w", encoding="utf-8") as handle:
                json.dump([ICLOUD, {"name": "Read only", "url": "https://example.com/feed.ics"}], handle)
            with mock.patch.object(fetch_events, "CONFIG_PATH", config):
                writables = fetch_events.get_writable_calendars()
        by_name = {c["name"]: c for c in writables}
        self.assertEqual(by_name["Apple iCloud"]["type"], "caldav")
        self.assertNotIn("Read only", by_name)
        self.assertIn("Local Calendar", by_name)


if __name__ == "__main__":
    unittest.main()
