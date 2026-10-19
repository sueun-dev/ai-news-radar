import json
import gzip
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from radar import Store, Radar, item, iso, parse_source, parse_time, cache_delay, fetch_source


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.source = {"id": "fixture", "name": "Local fixture", "url": "https://example.test/feed", "kind": "feed", "interval": .1}
        self.config = {"sources": [self.source], "port": 8741, "x_accounts": ["OpenAI"], "notify_prerelease": False}
        self.store = Store(Path(self.tmp.name)/"test.sqlite", self.config)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def apply(self, entries, now, status=200, headers=None, error=None):
        return self.store.apply(self.source, dict(started=now-.02,finished=now,status=status,headers=headers or {},
                                                  entries=entries,fetch_ms=20,error=error))

    def test_baseline_restart_dedup_and_true_new_item(self):
        first=item("old","Old","https://example.test/old","body",iso(900),"published")
        self.assertEqual(self.apply([first],1000),[])
        self.assertEqual(self.apply([first],1001),[])
        new=item("new","New","https://example.test/new","body",iso(1005),"published")
        self.assertEqual(len(self.apply([new,first],1006)),1)
        self.assertEqual(self.apply([new,first],1007),[])
        row=dict(self.store.db.execute("SELECT * FROM events").fetchone())
        self.assertEqual(row["delay_seconds"],1)
        self.assertEqual(row["observation_gap_seconds"],5)
        reopened=Store(Path(self.tmp.name)/"test.sqlite",self.config)
        self.assertIsNotNone(reopened.source("fixture")["initialized_at"])
        reopened.db.close()

    def test_unchanged_304_keeps_baseline_and_updates_observation(self):
        self.apply([item("a","A","https://example.test/a")],1000)
        self.apply([],1005,304)
        self.assertEqual(self.store.source("fixture")["last_success"],1005)
        self.assertEqual(self.store.source("fixture")["item_count"],1)
        self.assertEqual(len(self.store.state()["events"]),0)

    def test_meaningful_edit_after_new_item_still_notifies(self):
        self.apply([item("a","A","https://example.test/a")],1000)
        self.apply([item("b","B","https://example.test/b","first")],1001)
        self.apply([item("b","B","https://example.test/b","corrected")],1002)
        rows=self.store.state()["events"]
        self.assertEqual(rows[0]["kind"],"updated")
        self.assertEqual(rows[0]["notify_state"],"pending")

    def test_empty_or_broken_response_does_not_replace_validators(self):
        self.apply([item("a","A","https://example.test/a")],1000,headers={"etag":"old"})
        self.apply([],1005,headers={"etag":"bad"})
        s=self.store.source("fixture")
        self.assertEqual(s["etag"],"old")
        self.assertEqual(s["last_success"],1000)
        self.assertEqual(s["status"],"error")

    def test_time_precision_old_and_future_are_not_claimed_as_latency(self):
        self.apply([item("a","A","https://example.test/a")],1000)
        entries=[item("date","D","https://example.test/date",raw_time="2026-10-19",basis="date_only"),
                 item("future","F","https://example.test/f",raw_time=iso(2000),basis="published"),
                 item("old","O","https://example.test/o",raw_time=iso(990),basis="published")]
        self.apply(entries,1010)
        self.assertEqual(self.store.state()["metrics"]["precise_samples"],0)
        self.assertTrue(all(x["delay_seconds"] is None for x in self.store.state()["events"]))
        self.assertEqual(parse_time("2026-10-19T12:00:00")[1],"timezone_unknown")

    def test_edit_without_new_timestamp_and_synthetic_are_excluded(self):
        self.apply([item("a","A","https://example.test/a","one",iso(900),"published")],1000)
        self.apply([item("a","A","https://example.test/a","two",iso(900),"published")],1010)
        self.store.test_event()
        self.assertEqual(self.store.state()["metrics"]["precise_samples"],0)
        self.assertEqual(self.store.state()["events"][0]["is_test"],1)

    def test_retry_after_and_cache_control(self):
        self.apply([],1000,429,{"retry-after":"120"},"HTTP 429")
        self.assertGreaterEqual(self.store.source("fixture")["next_due"],1120)
        self.assertEqual(cache_delay({"cache-control":"public, max-age=60","age":"15"}),45)
        self.assertEqual(cache_delay({"cache-control":"no-cache, max-age=60"}),0)

    def test_atom_updated_is_not_relabelled_published(self):
        data=b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>id</id><title>v1</title><link href="https://github.com/a/b/releases/tag/v1"/><updated>2026-10-19T00:00:00Z</updated><content>Body</content></entry></feed>'
        parsed=parse_source(self.source,data)
        self.assertEqual(parsed[0]["basis"],"feed_updated")
        self.assertEqual(parsed[0]["precision"],"instant")

    def test_html_parsers_ignore_unrelated_chrome(self):
        news=dict(self.source,kind="anthropic",url="https://www.anthropic.com/news")
        items=parse_source(news,b'<a href="/privacy">Privacy</a><a href="/news/test">A <b>release</b></a>')
        self.assertEqual(len(items),1)
        self.assertEqual(items[0]["title"],"A release")
        change=dict(self.source,kind="changelog")
        data=b'<script>noise</script><h2 id="oct">October 19, 2026</h2><p>New model</p><h2>Related articles</h2><p>random</p>'
        self.assertEqual(parse_source(change,data)[0]["content_hash"],item("","", "", "New model")["content_hash"])

    def test_manual_x_url_validation_and_no_automatic_push_claim(self):
        eid=self.store.record_x("https://x.com/OpenAI/status/123","")
        row=dict(self.store.db.execute("SELECT * FROM events WHERE id=?",(eid,)).fetchone())
        self.assertEqual(row["measurement_note"],"manual_record_time_not_push_receipt")
        self.assertRaises(ValueError,self.store.record_x,"https://evil.test/OpenAI/status/123","")
        self.assertRaises(ValueError,self.store.record_x,"https://x.com/Other/status/123","")

    def test_csv_formula_escape(self):
        self.apply([item("a","A","https://example.test/a")],1000)
        self.apply([item("b","=1+1","https://example.test/b")],1010)
        self.assertIn("'=1+1",self.store.export("events").decode())

    def test_http_fixture_change_to_record_and_notification_queue(self):
        fixture={"etag":"one","body":b'<rss><channel><item><title>Baseline</title><link>https://example.test/old</link></item></channel></rss>'}
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def do_GET(self):
                if self.headers.get("If-None-Match")==fixture["etag"]:
                    self.send_response(304);self.end_headers();return
                self.send_response(200);self.send_header("ETag",fixture["etag"])
                self.send_header("Content-Encoding","gzip");self.end_headers()
                self.wfile.write(gzip.compress(fixture["body"]))
        server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        self.source["url"]="http://127.0.0.1:%s/feed"%server.server_port
        try:
            self.store.apply(self.source,fetch_source(self.source,self.store.source("fixture")))
            self.assertEqual(self.store.pending(),[])
            unchanged=fetch_source(self.source,self.store.source("fixture"))
            self.assertEqual(unchanged["status"],304)
            self.store.apply(self.source,unchanged)
            time.sleep(.05)
            published=time.time()
            fixture["etag"]="two"
            fixture["body"]=('<rss><channel><item><title>SYNTHETIC HTTP FIXTURE</title><link>https://example.test/new</link><pubDate>'+
                             iso(published)+'</pubDate></item></channel></rss>').encode()
            time.sleep(.15)
            self.store.apply(self.source,fetch_source(self.source,self.store.source("fixture")))
            pending=self.store.pending()
            self.assertEqual(len(pending),1)
            delay=pending[0]["delay_seconds"]
            self.assertIsNotNone(delay)
            self.assertGreaterEqual(delay,0)
            self.assertLess(delay,3)
            self.store.notification_result(pending[0]["id"],"accepted")
            self.assertEqual(self.store.pending(),[])
            proof={"synthetic":True,"source":"local HTTP fixture","detection_delay_seconds":delay,
                   "http_304_verified":True,"notification_queue_and_ack_verified":True,
                   "actual_macos_banner_test":"separate live bridge test",
                   "included_in_live_metrics":False,"observed_at":iso()}
            evidence=Path(self.tmp.name)/"synthetic-http-test.json"
            evidence.write_text(json.dumps(proof,indent=2))
        finally:
            server.shutdown();server.server_close()


if __name__=="__main__":
    unittest.main()
