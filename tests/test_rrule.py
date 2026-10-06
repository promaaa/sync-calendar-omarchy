"""Recurrence rules (RFC 5545 3.3.10). Run with python-dateutil installed to
also compare many rules against it; the plugin itself does not need it."""
import importlib.util
from datetime import datetime
from pathlib import Path
import itertools
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("fetch_events", ROOT / "fetch-events.py")
fetch_events = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetch_events)
# Keep the sync cache out of the real ~/.local/state.
fetch_events.SYNC_CACHE_DIR = tempfile.mkdtemp(prefix="chronica-test-cache-")

try:
    from dateutil.rrule import rrulestr
except ImportError:
    rrulestr = None


def occurrences(dtstart, rule, window_start, window_end):
    event = {"start_dt": dtstart, "end_dt": dtstart, "rrule": fetch_events.parse_rrule(rule), "exdates": []}
    return [i["start_dt"] for i in fetch_events.expand_rrule_in_wall_time(event, window_start, window_end)]


class RruleCaseTests(unittest.TestCase):
    def test_monthly_interval_does_not_drift_after_fast_forward(self):
        got = occurrences(datetime(2020, 1, 15, 9), "FREQ=MONTHLY;INTERVAL=7",
                          datetime(2026, 1, 1), datetime(2027, 12, 31))
        self.assertEqual(got, [datetime(2026, 6, 15, 9), datetime(2027, 1, 15, 9), datetime(2027, 8, 15, 9)])

    def test_last_weekday_of_month(self):
        got = occurrences(datetime(2026, 1, 30, 9), "FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1",
                          datetime(2026, 1, 1), datetime(2026, 4, 30, 23, 59))
        self.assertEqual(got, [datetime(2026, 1, 30, 9), datetime(2026, 2, 27, 9),
                               datetime(2026, 3, 31, 9), datetime(2026, 4, 30, 9)])

    def test_friday_the_13th(self):
        got = occurrences(datetime(2026, 2, 13, 20), "FREQ=MONTHLY;BYDAY=FR;BYMONTHDAY=13",
                          datetime(2026, 1, 1), datetime(2026, 12, 31))
        self.assertEqual([d.date().isoformat() for d in got], ["2026-02-13", "2026-03-13", "2026-11-13"])

    def test_count_counts_from_dtstart_before_the_window(self):
        got = occurrences(datetime(2026, 1, 1, 9), "FREQ=DAILY;COUNT=10",
                          datetime(2026, 1, 8), datetime(2026, 1, 31))
        self.assertEqual([d.day for d in got], [8, 9, 10])

    def test_byhour_expands_times(self):
        got = occurrences(datetime(2026, 3, 2, 9), "FREQ=WEEKLY;BYDAY=MO;BYHOUR=9,14",
                          datetime(2026, 3, 1), datetime(2026, 3, 9, 23))
        self.assertEqual([(d.day, d.hour) for d in got], [(2, 9), (2, 14), (9, 9), (9, 14)])

    def test_impossible_rule_terminates(self):
        self.assertEqual(occurrences(datetime(2026, 1, 1), "FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=30",
                                     datetime(2026, 1, 2), datetime(2027, 12, 31)), [])

    def test_rdate_adds_occurrences(self):
        ics = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:r\r\nSUMMARY:Class\r\n"
               "DTSTART:20260907T100000\r\nDTEND:20260907T110000\r\n"
               "RRULE:FREQ=WEEKLY;COUNT=2\r\nRDATE:20260910T100000,20260907T100000\r\n"
               "END:VEVENT\r\nEND:VCALENDAR\r\n")
        events = fetch_events.parse_ics(ics, {"name": "Feed"}, datetime(2026, 9, 1), datetime(2026, 9, 30))
        self.assertEqual(sorted(e["start_dt"].day for e in events), [7, 10, 14])
        self.assertTrue(all(e["end_dt"] - e["start_dt"] == fetch_events.timedelta(hours=1) for e in events))


@unittest.skipIf(rrulestr is None, "python-dateutil not installed")
class RruleAgainstDateutilTests(unittest.TestCase):
    RULES = [
        "FREQ=DAILY;INTERVAL=2", "FREQ=DAILY;INTERVAL=3", "FREQ=DAILY;BYDAY=MO,WE,FR", "FREQ=DAILY;BYMONTH=1,7",
        "FREQ=WEEKLY", "FREQ=WEEKLY;INTERVAL=2;BYDAY=TU,TH", "FREQ=WEEKLY;BYDAY=SU,MO;WKST=SU",
        "FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,SU;WKST=SU", "FREQ=WEEKLY;INTERVAL=2;BYDAY=MO;BYHOUR=8,17;BYMINUTE=0,30",
        "FREQ=MONTHLY", "FREQ=MONTHLY;INTERVAL=5", "FREQ=MONTHLY;BYMONTHDAY=-1", "FREQ=MONTHLY;BYMONTHDAY=1,15,31",
        "FREQ=MONTHLY;BYDAY=2TU", "FREQ=MONTHLY;BYDAY=-1FR", "FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=1,-1",
        "FREQ=MONTHLY;BYDAY=SA,SU;BYSETPOS=2", "FREQ=MONTHLY;BYDAY=FR;BYMONTHDAY=13",
        "FREQ=MONTHLY;BYMONTH=3,9;BYDAY=1MO",
        "FREQ=YEARLY", "FREQ=YEARLY;INTERVAL=2", "FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU", "FREQ=YEARLY;BYDAY=20MO",
        "FREQ=YEARLY;BYDAY=-1MO", "FREQ=YEARLY;BYYEARDAY=1,100,-1", "FREQ=YEARLY;BYWEEKNO=1;BYDAY=MO",
        "FREQ=YEARLY;BYWEEKNO=20", "FREQ=YEARLY;BYWEEKNO=-1;BYDAY=FR", "FREQ=YEARLY;BYMONTH=11;BYDAY=TU;BYMONTHDAY=2,3,4,5,6,7,8",
        "FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=29", "FREQ=YEARLY;BYMONTH=1,2;BYDAY=MO,TU;BYSETPOS=-2",
    ]
    STARTS = [datetime(2019, 2, 28, 9, 30), datetime(2024, 1, 31, 18), datetime(2025, 12, 29, 7)]

    def test_rules_match_dateutil(self):
        # Two years stay under MAX_EXPANDED_INSTANCES for the densest rule.
        window_start, window_end = datetime(2026, 1, 1), datetime(2027, 12, 31, 23, 59)
        for rule, start in itertools.product(self.RULES, self.STARTS):
            for suffix in ("", ";COUNT=40"):
                with self.subTest(rule=rule + suffix, start=start):
                    expected = [d for d in rrulestr(rule + suffix, dtstart=start).between(window_start, window_end, inc=True)
                                if d > start]
                    got = [d for d in occurrences(start, rule + suffix, window_start, window_end) if d > start]
                    if ";COUNT=" in suffix and start not in rrulestr(rule, dtstart=start)[:1]:
                        # RFC 5545 counts DTSTART as the first occurrence even
                        # when the rule does not produce it; dateutil does not.
                        expected = [d for d in rrulestr(rule + ";COUNT=39", dtstart=start).between(window_start, window_end, inc=True)
                                    if d > start]
                    self.assertEqual(got, expected)


if __name__ == "__main__":
    unittest.main()
