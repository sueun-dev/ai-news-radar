import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import support  # noqa: F401  (puts the repository on sys.path)
from newsradar import timeutil
from newsradar.timeutil import DATE, INSTANT, TIMEZONE_UNKNOWN, UNKNOWN, iso, parse_time


def epoch(*args, offset_hours=0):
    return datetime(*args, tzinfo=timezone(timedelta(hours=offset_hours))).timestamp()


class IsoTests(unittest.TestCase):
    def test_formats_epoch_as_utc_with_milliseconds(self):
        self.assertEqual(iso(0), "1970-01-01T00:00:00.000+00:00")
        self.assertEqual(iso(1.2345), "1970-01-01T00:00:01.234+00:00")

    def test_defaults_to_now(self):
        with patch.object(timeutil.time, "time", return_value=86400.0):
            self.assertEqual(iso(), "1970-01-02T00:00:00.000+00:00")

    def test_round_trips_through_parse_time(self):
        self.assertEqual(parse_time(iso(1_800_000_000.5)), (1_800_000_000.5, INSTANT))


class ParseTimeTests(unittest.TestCase):
    def test_instants_with_timezones(self):
        cases = {
            "2026-10-19T12:00:00Z": epoch(2026, 10, 19, 12),
            "2026-10-19T12:00:00+09:00": epoch(2026, 10, 19, 12, offset_hours=9),
            "2026-10-19T12:00:00-05:00": epoch(2026, 10, 19, 12, offset_hours=-5),
            "2026-10-19 12:00:00+00:00": epoch(2026, 10, 19, 12),
            "2026-10-19T12:00:00.250Z": epoch(2026, 10, 19, 12) + .25,
            "Mon, 19 Oct 2026 12:00:00 GMT": epoch(2026, 10, 19, 12),
            "Mon, 19 Oct 2026 12:00:00 +0900": epoch(2026, 10, 19, 12, offset_hours=9),
            "19 Oct 2026 12:00:00 -0500": epoch(2026, 10, 19, 12, offset_hours=-5),
            "  2026-10-19T12:00:00Z\n": epoch(2026, 10, 19, 12),
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(parse_time(raw), (expected, INSTANT))

    def test_date_only_is_not_an_instant(self):
        for raw in ("2026-10-19", " 2026-10-19 "):
            self.assertEqual(parse_time(raw), (None, DATE))

    def test_times_without_offset_are_ambiguous(self):
        for raw in ("2026-10-19T12:00:00", "2026-10-19 12:00", "Mon, 19 Oct 2026 12:00:00 -0000"):
            with self.subTest(raw=raw):
                self.assertEqual(parse_time(raw), (None, TIMEZONE_UNKNOWN))

    def test_missing_or_garbage_is_unknown(self):
        for raw in (None, "", "   ", "yesterday", "2026-13-45T00:00:00Z", "Tue, 31 Feb 2026 00:00:00 GMT", "now"):
            with self.subTest(raw=raw):
                self.assertEqual(parse_time(raw), (None, UNKNOWN))

    def test_non_string_values_are_unknown(self):
        # JSON sources can carry numbers or objects where a timestamp string is expected.
        for raw in (123, 1.5, ["2026-10-19"], {"t": 1}, True):
            with self.subTest(raw=raw):
                self.assertEqual(parse_time(raw), (None, UNKNOWN))

    def test_unrepresentable_instant_is_unknown_instead_of_raising(self):
        class Moment:
            tzinfo = timezone.utc

            def timestamp(self):
                raise OverflowError("out of range")

        with patch.object(timeutil, "datetime") as fake:
            fake.fromisoformat.return_value = Moment()
            self.assertEqual(parse_time("2026-10-19T00:00:00Z"), (None, UNKNOWN))


if __name__ == "__main__":
    unittest.main()
