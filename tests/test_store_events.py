import logging
import sqlite3
import unittest

from support import T0, StoreTestCase, entry, make_config, make_result, make_source
from newsradar.items import item
from newsradar.store import (BACKFILL_GRACE, CHECK_RETENTION, DUPLICATE_WINDOW, INTERNAL_ERROR, ZERO_ENTRIES_ERROR,
                             Store, delay_summary, is_prerelease, measure_delay, notify_state, percentile95)
from newsradar.timeutil import iso


def ent(source_time=None, precision="unknown", basis="published"):
    return dict(source_time=source_time, precision=precision, basis=basis)


class MeasureDelayTests(unittest.TestCase):
    INIT = T0 - 1000

    def measure(self, e, old=None, changed=False, now=T0):
        return measure_delay(e, old, changed, now, self.INIT)

    def test_precise_publication_delay(self):
        self.assertEqual(self.measure(ent(T0 - 30, "instant", "published")), (30, "published"))
        self.assertEqual(self.measure(ent(T0 - 30, "instant", "feed_updated")), (30, "feed_updated"))

    def test_edits_never_claim_latency(self):
        old = dict(source_time=T0 - 30)
        self.assertEqual(self.measure(ent(T0 - 30, "instant", "feed_updated"), old, True),
                         (None, "edit_without_new_precise_timestamp"))
        self.assertEqual(self.measure(ent(T0 - 5, "instant", "published"), dict(source_time=T0 - 30), True),
                         (None, "edit_without_new_precise_timestamp"))
        # An edit that carries a new precise "updated" time is a real measurement.
        self.assertEqual(self.measure(ent(T0 - 5, "instant", "feed_updated"), old, True), (5, "feed_updated"))

    def test_non_original_clocks(self):
        for basis in ("index_published", "community_posted", "batch_announcement", "sitemap_modified", "repository_created"):
            with self.subTest(basis=basis):
                self.assertEqual(self.measure(ent(T0 - 5, "instant", basis)), (None, "not_original_publication_time"))

    def test_implausible_times(self):
        self.assertEqual(self.measure(ent(T0 + 60, "instant")), (None, "source_clock_in_future"))
        self.assertEqual(self.measure(ent(self.INIT - 1, "instant")), (None, "older_than_initial_baseline"))
        self.assertEqual(self.measure(ent(self.INIT, "instant")), (T0 - self.INIT, "published"))

    def test_imprecise_times(self):
        self.assertEqual(self.measure(ent(None, "date", "date_only")), (None, "date_only_or_unavailable"))
        self.assertEqual(self.measure(ent(None, "unknown", "date_or_unknown")), (None, "date_only_or_unavailable"))
        self.assertEqual(self.measure(ent(None, "timezone_unknown", "published")), (None, "source_time_unknown"))
        self.assertEqual(self.measure(ent(None, "unknown", "unknown")), (None, "source_time_unknown"))


class NotifyStateTests(unittest.TestCase):
    def state(self, **kw):
        args = dict(backfill=False, duplicate=False, enabled=True, prerelease=False, notify_prerelease=False)
        args.update(kw)
        return notify_state(**args)

    def test_precedence(self):
        self.assertEqual(self.state(), "pending")
        self.assertEqual(self.state(backfill=True, duplicate=True, enabled=False, prerelease=True), "historical_silent")
        self.assertEqual(self.state(duplicate=True, enabled=False, prerelease=True), "duplicate")
        self.assertEqual(self.state(enabled=False, prerelease=True), "source_silent")
        self.assertEqual(self.state(prerelease=True), "prerelease_silent")
        self.assertEqual(self.state(prerelease=True, notify_prerelease=True), "pending")

    def test_prerelease_detection(self):
        releases, other = make_source(group="releases"), make_source(group="official")
        for title in ("v1.0-alpha", "2.0 Beta", "rc1", "v3.0rc.2", "1.0.dev3", "dev.4"):
            with self.subTest(title=title):
                self.assertTrue(is_prerelease(releases, title))
                self.assertTrue(is_prerelease(make_source(id="codex"), title))
                self.assertFalse(is_prerelease(other, title))
        for title in ("v1.0", "Release candidate", "rc", "device update", "Source release notes"):
            self.assertFalse(is_prerelease(releases, title), title)
        # Known false positives of the substring rule. A word-boundary rule would miss "1.0rc1", and the
        # result only silences a desktop notification, so the rule is kept.
        for title in ("Alphabet earnings", "src.zip"):
            self.assertTrue(is_prerelease(releases, title), title)


class StatisticsTests(unittest.TestCase):
    def test_percentile95_nearest_rank(self):
        self.assertIsNone(percentile95([]))
        self.assertEqual(percentile95([7]), 7)
        self.assertEqual(percentile95(list(range(1, 21))), 19)
        self.assertEqual(percentile95(list(range(1, 101))), 95)
        self.assertEqual(percentile95([1, 2]), 2)

    def test_delay_summary(self):
        self.assertEqual(delay_summary([]), {"count": 0, "median": None, "p95": None})
        self.assertEqual(delay_summary([5, 1, 3]), {"count": 3, "median": 3, "p95": 5})


class BaselineAndEventTests(StoreTestCase):
    def test_first_collection_is_a_silent_baseline(self):
        self.assertEqual(self.apply([entry(1), entry(2)], T0), [])
        self.assertEqual(self.events(), [])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM items").fetchone()[0], 2)
        self.assertEqual(self.row()["initialized_at"], T0)

    def test_new_item_after_baseline(self):
        self.apply([entry(1)], T0)
        created = self.apply([entry(1), entry(2, raw_time=iso(T0 + 50))], T0 + 60)
        [event] = self.events()
        self.assertEqual(created, [event["id"]])
        self.assertEqual((event["kind"], event["notify_state"], event["delay_seconds"], event["observation_gap_seconds"]),
                         ("new", "pending", 10, 60))
        self.assertEqual(event["measurement_note"], "published")

    def test_title_or_body_change_is_an_update(self):
        self.apply([entry(1)], T0)
        self.apply([entry(1, title="Story 1 (corrected)")], T0 + 1)
        self.apply([entry(1, title="Story 1 (corrected)", body="new body")], T0 + 2)
        self.assertEqual([e["kind"] for e in self.events()], ["updated", "updated"])

    def test_detect_updates_false_ignores_changes_but_not_new_items(self):
        source = dict(self.source, detect_updates=False)
        self.apply([entry(1)], T0, source=source)
        self.apply([entry(1, body="changed"), entry(2)], T0 + 1, source=source)
        self.assertEqual([e["kind"] for e in self.events()], ["new"])

    def test_same_url_within_a_day_is_a_duplicate(self):
        other = make_source(id="b", name="Beta", url="https://b.test/feed")
        self.store = self._reopen(make_config(self.source, other))
        self.apply([entry(0)], T0)
        self.apply([entry(0, host="b.test")], T0, source=other)
        self.apply([entry(1)], T0 + 10)
        self.apply([entry(1)], T0 + 20, source=other)  # same URL https://a.test/post/1 from another source
        self.apply([entry(2)], T0 + 30)
        self.apply([entry(2)], T0 + 30 + DUPLICATE_WINDOW + 1, source=other)
        states = [(e["source_id"], e["notify_state"]) for e in self.events()]
        self.assertEqual(states, [("a", "pending"), ("b", "duplicate"), ("a", "pending"), ("b", "pending")])

    def test_test_events_do_not_make_duplicates(self):
        self.apply([entry(0)], T0)
        self.store.test_event()
        url = "http://127.0.0.1:8741/"
        self.apply([entry(0), item(url, "Same as test URL", url)], T0 + 1)
        self.assertEqual(self.events()[-1]["notify_state"], "pending")

    def test_old_rediscovery_is_backfill(self):
        self.apply([entry(0)], T0)
        self.apply([entry(1, raw_time=iso(T0 - BACKFILL_GRACE - 1))], T0 + 10)
        self.apply([entry(2, raw_time=iso(T0 - BACKFILL_GRACE + 1))], T0 + 20)
        kinds = [(e["kind"], e["notify_state"], e["measurement_note"]) for e in self.events()]
        self.assertEqual(kinds, [("backfill", "historical_silent", "older_than_initial_baseline"),
                                 ("new", "pending", "older_than_initial_baseline")])

    def test_source_notify_flag_and_preferences(self):
        quiet = dict(self.source, notify=False)
        self.apply([entry(0)], T0, source=quiet)
        self.apply([entry(1)], T0 + 1, source=quiet)
        self.store.set_notification("a", True)
        self.apply([entry(2)], T0 + 2, source=quiet)
        self.store.set_notification("a", False)
        self.apply([entry(3)], T0 + 3, source=self.source)
        self.assertEqual([e["notify_state"] for e in self.events()], ["source_silent", "pending", "source_silent"])

    def test_prerelease_titles_from_release_sources_are_silent_unless_configured(self):
        releases = dict(self.source, group="releases")
        self.apply([entry(0)], T0, source=releases)
        self.apply([entry(1, title="v2.0.0-beta.1")], T0 + 1, source=releases)
        self.store.config["notify_prerelease"] = True
        self.apply([entry(2, title="v2.0.0-rc.1")], T0 + 2, source=releases)
        self.assertEqual([e["notify_state"] for e in self.events()], ["prerelease_silent", "pending"])

    def test_items_keep_first_seen_and_update_last_seen(self):
        self.apply([entry(1)], T0)
        self.apply([entry(1, title="Renamed")], T0 + 100)
        row = self.store.db.execute("SELECT * FROM items").fetchone()
        self.assertEqual((row["first_seen"], row["last_seen"], row["title"]), (T0, T0 + 100, "Renamed"))

    def test_preference_lookup_happens_once_per_check(self):
        self.apply([entry(0)], T0)
        statements = []
        self.store.db.set_trace_callback(statements.append)
        self.apply([entry(n) for n in range(1, 6)], T0 + 1)
        self.store.db.set_trace_callback(None)
        self.assertEqual(sum("source_preferences" in s for s in statements), 1)
        self.assertEqual(len(self.events()), 5)

    def _reopen(self, config):
        self.store.db.close()
        self.config = config
        return Store(self.tmp / "radar.sqlite", config)


class ApplyOutcomeTests(StoreTestCase):
    def test_304_before_baseline_is_not_success(self):
        self.apply([], T0, status=304)
        row = self.row()
        self.assertEqual((row["status"], row["failures"], row["initialized_at"]), ("error", 1, None))

    def test_304_after_baseline_is_success_and_keeps_item_count(self):
        self.apply([entry(1), entry(2)], T0)
        self.apply([], T0 + 60, status=304, headers={"etag": "new"})
        row = self.row()
        self.assertEqual((row["status"], row["last_success"], row["item_count"], row["etag"]), ("ok", T0 + 60, 2, "new"))

    def test_zero_entries_is_an_error_that_preserves_state(self):
        self.apply([entry(1)], T0, headers={"etag": "good", "last-modified": "lm"})
        self.apply([], T0 + 60, headers={"etag": "bad", "last-modified": "bad", "cache-control": "max-age=1"})
        row = self.row()
        self.assertEqual(row["error"], ZERO_ENTRIES_ERROR)
        self.assertEqual((row["etag"], row["last_modified"], row["last_success"], row["item_count"]), ("good", "lm", T0, 1))
        self.assertEqual(row["cache_control"], "max-age=1")  # informational, always the latest

    def test_filtered_to_zero_and_valid_empty_feed_are_success(self):
        self.apply([entry(1)], T0)
        self.apply([], T0 + 1, source=dict(self.source, filter_ai=True), parsed_count=9)
        self.assertEqual(self.row()["status"], "ok")
        self.apply([], T0 + 2, valid_empty_feed=True)
        self.assertEqual((self.row()["status"], self.row()["item_count"]), ("ok", 0))
        self.apply([], T0 + 3, parsed_count=9)  # parsed but not filtered: still suspicious
        self.assertEqual(self.row()["status"], "error")

    def test_failures_count_up_and_reset(self):
        for n in range(3):
            self.apply([], T0 + n, status=500, error="HTTP 500")
        self.assertEqual(self.row()["failures"], 3)
        self.apply([entry(1)], T0 + 10)
        self.assertEqual((self.row()["failures"], self.row()["error"], self.row()["status"]), (0, None, "ok"))

    def test_next_due_follows_policy(self):
        self.apply([entry(1)], T0)
        self.assertEqual(self.row()["next_due"], T0 + 60)
        self.apply([], T0 + 60, status=None, error="URLError", error_kind="transport")
        self.assertEqual(self.row()["next_due"], T0 + 60 + 15)
        self.apply([], T0 + 100, status=429, headers={"retry-after": "900"}, error="HTTP 429")
        self.assertEqual(self.row()["next_due"], T0 + 100 + 900)

    def test_check_rows_carry_telemetry_and_are_pruned(self):
        self.store.db.execute("INSERT INTO checks(source_id,finished_at) VALUES('a',?)", (T0 - CHECK_RETENTION - 1,))
        self.store.db.execute("INSERT INTO checks(source_id,finished_at) VALUES('a',?)", (T0 - CHECK_RETENTION + 1,))
        self.apply([entry(1)], T0, headers={"content-encoding": "gzip", "age": "5"}, queue_delay_seconds=2.5,
                   transferred_bytes=10, decoded_bytes=40)
        rows = [dict(r) for r in self.store.db.execute("SELECT * FROM checks ORDER BY finished_at")]
        self.assertEqual(len(rows), 2)
        latest = rows[-1]
        self.assertEqual((latest["status"], latest["item_count"], latest["content_encoding"], latest["cache_age"]),
                         (200, 1, "gzip", "5"))
        self.assertEqual((latest["queue_delay_seconds"], latest["transferred_bytes"], latest["decoded_bytes"]), (2.5, 10, 40))
        self.assertEqual(latest["next_due"], T0 + 60)

    def test_log_line_format(self):
        with self.assertLogs("radar", logging.INFO) as logs:
            self.apply([entry(1)], T0)
        self.assertEqual(logs.output, ["INFO:radar:a status=200 items=1 new_events=0 next=60.0s error=None"])

    def test_internal_error_marker(self):
        self.store.mark_internal_error("a", T0 + 120)
        row = self.row()
        self.assertEqual((row["status"], row["error"], row["next_due"]), ("error", INTERNAL_ERROR, T0 + 120))

    def test_unknown_source(self):
        self.assertRaises(KeyError, self.store.source, "missing")


class MigrationTests(StoreTestCase):
    def test_wal_and_schema(self):
        self.assertEqual(self.store.db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        tables = {r[0] for r in self.store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables, {"sources", "items", "events", "checks", "meta", "source_preferences"})

    def test_reopen_is_idempotent_and_records_meta(self):
        self.apply([entry(1)], T0)
        self.store.db.close()
        self.store = Store(self.tmp / "radar.sqlite", self.config)
        meta = {r["key"] for r in self.store.db.execute("SELECT key FROM meta")}
        self.assertTrue({"canonical_v2", "service_started", "pid"} <= meta)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM items").fetchone()[0], 1)

    def test_config_sync_updates_metadata_and_respects_waits(self):
        self.apply([entry(1)], T0)
        self.store.db.close()
        changed = dict(self.source, name="Renamed", url="https://a.test/v2", interval=600)
        self.store = Store(self.tmp / "radar.sqlite", make_config(changed))
        row = self.row()
        self.assertEqual((row["name"], row["url"], row["interval"]), ("Renamed", "https://a.test/v2", 600))
        self.assertEqual(row["next_due"], T0 + 600)  # last_success + new interval, never sooner

    def test_interval_change_of_failing_source_keeps_schedule(self):
        self.apply([], T0, status=500, error="HTTP 500")
        due = self.row()["next_due"]
        self.store.db.close()
        self.store = Store(self.tmp / "radar.sqlite", make_config(dict(self.source, interval=999)))
        self.assertEqual(self.row()["next_due"], due)

    def test_canonical_url_migration_merges_duplicates(self):
        # A pre-canonical database: full schema, no 'canonical_v2' marker, tracking parameters in keys.
        with self.store.db:
            self.store.db.execute("DELETE FROM meta WHERE key='canonical_v2'")
            self.store.db.executemany("INSERT INTO items(source_id,key,url,title) VALUES(?,?,?,?)", [
                ("a", "https://A.test/x/?utm_source=feed", "https://A.test/x/?utm_source=feed", "tracked"),
                ("a", "https://a.test/x", "https://a.test/x", "canonical"),
                ("a", "https://a.test/y/?utm_medium=m", "https://a.test/y/?utm_medium=m", "only"),
                ("a", "opaque-key", "HTTPS://A.TEST/z/", "opaque")])
            self.store.db.execute("INSERT INTO events(source_id,title,url) VALUES('a','e','https://a.test/x/?fbclid=1')")
        self.store.db.close()
        self.store = Store(self.tmp / "radar.sqlite", self.config)
        items = {r["key"]: (r["url"], r["title"]) for r in self.store.db.execute("SELECT * FROM items")}
        self.assertEqual(items, {"https://a.test/x": ("https://a.test/x", "canonical"),
                                 "https://a.test/y": ("https://a.test/y", "only"),
                                 "opaque-key": ("https://a.test/z", "opaque")})
        self.assertEqual(self.store.db.execute("SELECT url FROM events").fetchone()[0], "https://a.test/x")
        # The marker makes the migration one-time: a later non-canonical row is left alone.
        with self.store.db:
            self.store.db.execute("INSERT INTO items(source_id,key,url) VALUES('a','https://a.test/q/?utm_x=1','u')")
        self.store.db.close()
        self.store = Store(self.tmp / "radar.sqlite", self.config)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM items WHERE key LIKE '%utm_x%'").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
