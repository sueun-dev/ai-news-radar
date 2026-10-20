import csv
import io
import json
import time
import unittest
from unittest.mock import patch

from support import T0, StoreTestCase, entry, make_config, make_source
from newsradar import store as store_module
from newsradar.items import item
from newsradar.store import PENDING_BATCH, RECENT_EVENTS, Store
from newsradar.timeutil import iso

ALPHA = make_source(id="a", name="Alpha Official", group="official", region="global")
BETA = make_source(id="b", name="Beta Papers", url="https://b.test/feed", group="research", region="korea", notify=False,
                   filter_ai=True)


def rows_of(csv_bytes):
    text = csv_bytes.decode("utf-8")
    assert text.startswith("﻿")
    return list(csv.reader(io.StringIO(text[1:])))


class StateTests(StoreTestCase):
    sources = [ALPHA, BETA]

    def test_sources_carry_config_flags_and_preferences(self):
        self.store.set_notification("a", False)
        state = self.store.state()
        by_id = {s["id"]: s for s in state["sources"]}
        self.assertEqual([s["name"] for s in state["sources"]], ["Alpha Official", "Beta Papers"])
        self.assertEqual((by_id["a"]["notify"], by_id["a"]["group"], by_id["a"]["filter_ai"]), (False, "official", False))
        self.assertEqual((by_id["b"]["notify"], by_id["b"]["region"], by_id["b"]["filter_ai"]), (False, "korea", True))
        self.assertEqual(state["coverage"]["groups"], {"official": 1, "research": 1})
        self.assertEqual(state["coverage"]["active_sources"], 2)
        self.assertEqual(state["x_accounts"], ["OpenAI"])

    def test_sources_removed_from_config_are_hidden_but_kept(self):
        self.apply([entry(1)], T0)
        self.store.db.close()
        self.store = Store(self.tmp / "radar.sqlite", make_config(BETA))
        self.assertEqual([s["id"] for s in self.store.state()["sources"]], ["b"])
        self.assertEqual(self.store.state()["coverage"]["item_count"], 1)

    def test_events_are_newest_first_and_bounded(self):
        self.apply([entry(0)], T0)
        self.apply([entry(n) for n in range(1, RECENT_EVENTS + 6)], T0 + 1)
        events = self.store.state()["events"]
        self.assertEqual(len(events), RECENT_EVENTS)
        self.assertGreater(events[0]["id"], events[-1]["id"])

    def test_latency_metrics(self):
        self.apply([entry(0)], T0)
        self.apply([entry(1, raw_time=iso(T0 + 50)), entry(2, raw_time=iso(T0 + 90))], T0 + 100)
        self.apply([entry(3, raw_time=iso(T0 + 1000), basis="feed_updated")], T0 + 1010)
        self.store.test_event()
        with patch.object(store_module.time, "time", return_value=T0 + 2000):
            self.store.record_x("https://x.com/OpenAI/status/1", iso(T0))  # has a delay, but is never a sample
        metrics = self.store.state()["metrics"]
        self.assertEqual(metrics["precise_samples"], 3)
        self.assertEqual((metrics["published_samples"], metrics["updated_samples"]), (2, 1))
        self.assertEqual(metrics["by_basis"]["published"], {"count": 2, "median": 30, "p95": 50})
        self.assertEqual(metrics["by_basis"]["feed_updated"], {"count": 1, "median": 10, "p95": 10})
        self.assertEqual((metrics["median_seconds"], metrics["p95_seconds"]), (10, 50))
        self.assertEqual(metrics["long_observation_samples"], 1)  # the 910 s gap before entry 3

    def test_empty_metrics(self):
        metrics = self.store.state()["metrics"]
        self.assertEqual((metrics["precise_samples"], metrics["median_seconds"], metrics["p95_seconds"]), (0, None, None))

    def test_meta_is_decoded(self):
        self.store.record_bridge({"authorization": 2})
        meta = self.store.state()["meta"]
        self.assertEqual(meta["notification_bridge"]["authorization"], 2)
        self.assertIn("heartbeat", meta["notification_bridge"])
        self.assertTrue(meta["canonical_v2"])

    def test_x_setup_file(self):
        self.assertEqual(self.store.state()["x_setup"], {"status": "not_verified", "accounts": []})
        (self.tmp / "x-setup.json").write_text(json.dumps({"status": "verified", "accounts": [{"handle": "OpenAI"}]}))
        self.assertEqual(self.store.state()["x_setup"]["status"], "verified")
        (self.tmp / "x-setup.json").write_text("{broken")
        self.assertEqual(self.store.state()["x_setup"]["status"], "not_verified")


class ExportTests(StoreTestCase):
    def test_tables_header_order_and_bom(self):
        self.apply([entry(1)], T0)
        self.apply([entry(1), entry(2)], T0 + 5)
        items = rows_of(self.store.export("items"))
        self.assertEqual(items[0][:4], ["source_id", "key", "title", "url"])
        self.assertEqual([r[2] for r in items[1:]], ["Story 2", "Story 1"])  # newest first
        self.assertEqual(len(rows_of(self.store.export("events"))), 2)
        self.assertEqual(rows_of(self.store.export("checks"))[0][0], "id")

    def test_formula_injection_is_neutralised(self):
        self.apply([entry(0)], T0)
        titles = ["=1+1", "+cmd", "-2", "@SUM(A1)", "plain"]
        self.apply([item("k%d" % i, t, "https://a.test/%d" % i) for i, t in enumerate(titles + ["tab"])], T0 + 1)
        with self.store.db:  # cleaned titles never start with whitespace; other columns might
            self.store.db.execute("UPDATE items SET title=? WHERE key='k5'", ("\tTabbed",))
        exported = {r[2] for r in rows_of(self.store.export("items"))[1:]}
        self.assertTrue({"'=1+1", "'+cmd", "'-2", "'@SUM(A1)", "plain", "'\tTabbed"} <= exported)

    def test_quotes_and_newlines_round_trip(self):
        self.apply([item("k", 'Say "hi", ok', "https://a.test/q")], T0)
        self.assertEqual(rows_of(self.store.export("items"))[1][2], 'Say "hi", ok')

    def test_only_known_tables(self):
        for table in ("sources", "meta", "items; DROP TABLE items", ""):
            self.assertRaisesRegex(ValueError, "Invalid export", self.store.export, table)


class LibraryTests(StoreTestCase):
    sources = [ALPHA, BETA]

    def seed(self):
        self.apply([item("k%d" % i, "AI title %d" % i, "https://a.test/%d" % i) for i in range(5)], T0, source=ALPHA)
        self.apply([item("p", "Paper 100%_done \\ path", "https://b.test/p")], T0 + 1, source=BETA)

    def test_pagination_and_totals(self):
        self.seed()
        page = self.store.library(limit=2)
        self.assertEqual((page["total"], len(page["items"]), page["offset"], page["limit"]), (6, 2, 0, 2))
        self.assertEqual(len(self.store.library(offset=4, limit=2)["items"]), 2)
        beyond = self.store.library(offset=50)
        self.assertEqual((beyond["items"], beyond["total"]), ([], 6))

    def test_newest_first_with_source_metadata(self):
        self.seed()
        first = self.store.library()["items"][0]
        self.assertEqual((first["source_id"], first["source_name"], first["group"], first["region"]),
                         ("b", "Beta Papers", "research", "korea"))

    def test_group_filter(self):
        self.seed()
        self.assertEqual(self.store.library(group="research")["total"], 1)
        self.assertEqual(self.store.library(group="nonexistent"), dict(items=[], total=0, offset=0, limit=50))

    def test_query_matches_title_source_id_and_name(self):
        self.seed()
        self.assertEqual(self.store.library(query="ai title")["total"], 5)
        self.assertEqual(self.store.library(query="alpha off")["total"], 5)   # source name, case-insensitive
        self.assertEqual(self.store.library(query="b")["total"], 1)           # source id LIKE
        self.assertEqual(self.store.library(query="nothing-here")["total"], 0)

    def test_like_wildcards_are_literal(self):
        self.seed()
        self.assertEqual(self.store.library(query="%")["total"], 1)
        self.assertEqual(self.store.library(query="%_done")["total"], 1)
        self.assertEqual(self.store.library(query="_")["total"], 1)
        self.assertEqual(self.store.library(query="\\")["total"], 1)
        self.assertEqual(self.store.library(query="AI%title")["total"], 0)

    def test_same_url_is_listed_once_preferring_a_real_title(self):
        url = "https://shared.test/story"
        self.apply([item(url, "[제목 미제공] /story", url)], T0, source=ALPHA)
        self.apply([item(url, "The real headline", url)], T0 + 1, source=BETA)
        page = self.store.library()
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["items"][0]["title"], "The real headline")

    def test_korean_query(self):
        self.apply([item("k", "인공지능 정책 발표", "https://a.test/ko")], T0, source=ALPHA)
        self.assertEqual(self.store.library(query="인공지능")["total"], 1)


class PreferenceTests(StoreTestCase):
    def test_set_notification_validation(self):
        self.store.set_notification("a", False)
        self.assertEqual(self.store.db.execute("SELECT notify FROM source_preferences").fetchone()[0], 0)
        for sid, value in (("missing", True), ("a", 1), ("a", "true"), ("a", None)):
            with self.subTest(sid=sid, value=value):
                self.assertRaises(ValueError, self.store.set_notification, sid, value)
        self.assertRaises(TypeError, self.store.set_notification, ["a"], True)


class NotificationBridgeTests(StoreTestCase):
    def test_pending_queue_and_results(self):
        self.apply([entry(0)], T0)
        self.apply([entry(n) for n in range(1, PENDING_BATCH + 4)], T0 + 1)
        pending = self.store.pending()
        self.assertEqual(len(pending), PENDING_BATCH)
        self.assertEqual([p["id"] for p in pending], sorted(p["id"] for p in pending))
        first = pending[0]["id"]
        self.store.notification_result(first, "accepted")
        self.store.notification_result(first, "error", "too late")  # only pending events change
        row = self.store.db.execute("SELECT notify_state,notify_error,notify_enqueued_at FROM events WHERE id=?", (first,)).fetchone()
        self.assertEqual((row[0], row[1]), ("accepted", None))
        self.assertIsNotNone(row[2])
        self.assertRaisesRegex(ValueError, "invalid notification state", self.store.notification_result, first, "seen")

    def test_error_detail_is_stored_as_text(self):
        eid = self.store.test_event()
        self.store.notification_result(eid, "error", {"code": 5})
        self.assertEqual(self.store.db.execute("SELECT notify_error FROM events").fetchone()[0], "{'code': 5}")

    def test_test_event(self):
        eid = self.store.test_event()
        row = dict(self.store.db.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone())
        self.assertEqual((row["source_id"], row["is_test"], row["notify_state"], row["measurement_note"], row["url"]),
                         ("self-test", 1, "pending", "synthetic_excluded", "http://127.0.0.1:8741/"))

    def test_opened_and_bridge(self):
        eid = self.store.test_event()
        self.store.mark_opened(eid)
        self.assertIsNotNone(self.store.db.execute("SELECT opened_at FROM events").fetchone()[0])
        self.store.record_bridge({"authorization": 3, "version": "1"})
        self.store.record_bridge([("authorization", 2)])
        bridge = json.loads(self.store.db.execute("SELECT value FROM meta WHERE key='notification_bridge'").fetchone()[0])
        self.assertEqual(bridge["authorization"], 2)
        self.assertNotIn("version", bridge)


class ManualXTests(StoreTestCase):
    def test_accepted_urls(self):
        for url in ("https://x.com/OpenAI/status/123", "https://www.x.com/openai/status/5/", "https://twitter.com/OPENAI/status/9",
                    "https://x.com/OpenAI/status/7?s=20"):
            with self.subTest(url=url):
                self.assertGreater(self.store.record_x(url, ""), 0)

    def test_rejected_urls(self):
        for url in ("http://x.com/OpenAI/status/1", "https://evil.test/OpenAI/status/1", "https://x.com/Other/status/1",
                    "https://x.com/OpenAI/post/1", "https://x.com/OpenAI/status/abc", "https://x.com/OpenAI/status/1/photo/1",
                    "https://x.com/OpenAI/status/١٢٣", "https://x.com.evil.test/OpenAI/status/1", ""):
            with self.subTest(url=url):
                self.assertRaises(ValueError, self.store.record_x, url, "")

    def test_publication_time(self):
        with patch.object(store_module.time, "time", return_value=T0):
            eid = self.store.record_x("https://x.com/OpenAI/status/1", iso(T0 - 90))
        row = dict(self.store.db.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone())
        self.assertEqual((row["delay_seconds"], row["kind"], row["notify_state"], row["basis"]), (90, "manual", "manual", "manual_input"))
        self.assertEqual(row["title"], "X 수동 도착 기록 · @OpenAI")
        for raw in (iso(time.time() + 3600), "2026-10-19", "2026-10-19T10:00:00", "soon"):
            self.assertRaises(ValueError, self.store.record_x, "https://x.com/OpenAI/status/1", raw)


if __name__ == "__main__":
    unittest.main()
