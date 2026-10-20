import unittest
from email.utils import formatdate
from unittest.mock import patch

from support import T0, make_source
from newsradar import policy
from newsradar.policy import (HOST_THROTTLE_MIN, MAX_SERVER_DELAY, cache_delay, header_seconds, host_throttle,
                              next_poll_interval, retry_after)


def http_date(ts):
    return formatdate(ts, usegmt=True)


class RetryAfterTests(unittest.TestCase):
    def test_delta_seconds(self):
        for raw, expected in (("120", 120), (" 30 ", 30), ("0", 0), ("-5", 0), ("1.5", 1.5)):
            with self.subTest(raw=raw):
                self.assertEqual(retry_after({"retry-after": raw}, T0), expected)

    def test_http_date(self):
        self.assertEqual(retry_after({"retry-after": http_date(T0 + 600)}, T0), 600)
        self.assertEqual(retry_after({"retry-after": http_date(T0 - 600)}, T0), 0)

    def test_missing_or_invalid_is_zero(self):
        for headers in ({}, {"retry-after": ""}, {"retry-after": "soon"}, {"retry-after": "nan"},
                        {"retry-after": "inf"}, {"retry-after": "-inf"}):
            with self.subTest(headers=headers):
                self.assertEqual(retry_after(headers, T0), 0)

    def test_absurd_delays_are_capped(self):
        self.assertEqual(retry_after({"retry-after": "1e12"}, T0), MAX_SERVER_DELAY)
        self.assertEqual(retry_after({"retry-after": http_date(T0 + 30 * 86400)}, T0), MAX_SERVER_DELAY)


class CacheDelayTests(unittest.TestCase):
    def test_max_age_minus_age(self):
        self.assertEqual(cache_delay({"cache-control": "public, max-age=60", "age": "15"}), 45)
        self.assertEqual(cache_delay({"cache-control": 'max-age="30"'}), 30)
        self.assertEqual(cache_delay({"cache-control": "max-age=10", "age": "99"}), 0)

    def test_no_cache_and_no_store_win(self):
        for value in ("no-cache, max-age=60", "max-age=60, no-store", "NO-CACHE"):
            self.assertEqual(cache_delay({"cache-control": value}), 0)

    def test_only_max_age_counts(self):
        self.assertEqual(cache_delay({"cache-control": "s-maxage=600"}), 0)

    def test_expires_relative_to_date_or_now(self):
        self.assertEqual(cache_delay({"expires": http_date(T0 + 90), "date": http_date(T0)}), 90)
        with patch.object(policy.time, "time", return_value=T0):
            self.assertEqual(cache_delay({"expires": http_date(T0 + 30)}), 30)
        self.assertEqual(cache_delay({"expires": "0"}), 0)

    def test_absurd_max_age_does_not_overflow(self):
        for digits in (13, 400, 5000):
            self.assertEqual(cache_delay({"cache-control": "max-age=" + "9" * digits, "age": "5"}), MAX_SERVER_DELAY)

    def test_age_is_subtracted_uncapped(self):
        self.assertEqual(cache_delay({"cache-control": "max-age=100000", "age": "200000"}), 0)
        self.assertEqual(cache_delay({"cache-control": "max-age=60", "age": "-5"}), 60)

    def test_invalid_age_is_ignored_and_values_are_capped(self):
        self.assertEqual(cache_delay({"cache-control": "max-age=10", "age": "abc"}), 10)
        self.assertEqual(cache_delay({"cache-control": "max-age=99999999"}), MAX_SERVER_DELAY)

    def test_header_seconds(self):
        self.assertEqual(header_seconds({"x-poll-interval": "60"}, "x-poll-interval"), 60)
        for value in ("", "abc", "-3", "inf", "nan", None):
            self.assertEqual(header_seconds({"x-poll-interval": value}, "x-poll-interval"), 0)


class NextPollIntervalTests(unittest.TestCase):
    SOURCE = make_source(interval=60)

    def interval(self, ok=True, status=200, failures=0, error_kind=None, headers=None, source=None):
        return next_poll_interval(source or self.SOURCE, ok=ok, status=status, failures=failures,
                                  error_kind=error_kind, headers=headers or {}, now=T0)

    def test_success_uses_interval_and_poll_hint(self):
        self.assertEqual(self.interval(), 60)
        self.assertEqual(self.interval(headers={"x-poll-interval": "90"}), 90)
        self.assertEqual(self.interval(headers={"x-poll-interval": "30"}), 60)

    def test_cache_freshness_only_for_opted_in_sources(self):
        headers = {"cache-control": "max-age=600"}
        self.assertEqual(self.interval(headers=headers), 60)
        self.assertEqual(self.interval(headers=headers, source=dict(self.SOURCE, respect_cache_ttl=True)), 600)

    def test_transport_backoff_sequence(self):
        delays = [self.interval(ok=False, status=None, failures=n, error_kind="transport") for n in range(1, 10)]
        self.assertEqual(delays, [15, 30, 60, 120, 240, 480, 900, 900, 900])

    def test_missing_status_counts_as_transport(self):
        self.assertEqual(self.interval(ok=False, status=None, failures=1, error_kind="parse"), 15)

    def test_transport_respects_server_hints(self):
        self.assertEqual(self.interval(ok=False, status=None, failures=1, error_kind="transport",
                                       headers={"retry-after": "120"}), 120)
        self.assertEqual(self.interval(ok=False, status=None, failures=1, error_kind="transport",
                                       headers={"x-poll-interval": "45"}), 45)

    def test_http_error_backoff(self):
        delays = [self.interval(ok=False, status=500, failures=n) for n in range(1, 9)]
        self.assertEqual(delays, [120, 240, 480, 960, 1920, 3600, 3600, 3600])
        slow = dict(self.SOURCE, interval=1800)
        self.assertEqual(self.interval(ok=False, status=404, failures=1, source=slow), 3600)

    def test_http_error_never_sooner_than_retry_after(self):
        self.assertEqual(self.interval(ok=False, status=429, failures=1, headers={"retry-after": "7200"}), 7200)

    def test_parse_error_with_status_uses_http_backoff(self):
        self.assertEqual(self.interval(ok=False, status=200, failures=2, error_kind="parse"), 240)

    def test_hostile_hints_cannot_stall_a_source(self):
        for headers in ({"retry-after": "inf"}, {"x-poll-interval": "inf"}, {"retry-after": "1e30"}):
            with self.subTest(headers=headers):
                self.assertLessEqual(self.interval(ok=False, status=503, failures=1, headers=headers), MAX_SERVER_DELAY)
                self.assertLessEqual(self.interval(headers=headers), MAX_SERVER_DELAY)


class HostThrottleTests(unittest.TestCase):
    def test_rate_limits_pause_the_whole_host(self):
        self.assertEqual(host_throttle(429, {}, T0), HOST_THROTTLE_MIN)
        self.assertEqual(host_throttle(429, {"retry-after": "600"}, T0), 600)
        self.assertEqual(host_throttle(503, {"retry-after": "5"}, T0), HOST_THROTTLE_MIN)

    def test_other_outcomes_do_not(self):
        for status, headers in ((503, {}), (200, {"retry-after": "60"}), (None, {}), (404, {})):
            self.assertIsNone(host_throttle(status, headers, T0))


if __name__ == "__main__":
    unittest.main()
