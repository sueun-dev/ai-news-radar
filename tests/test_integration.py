"""End to end: a local feed server, the real fetcher, poller, store and web server running together."""
import http.client
import json
import shutil
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from support import REPO, http_server, make_config, make_source, respond, rss, rss_item
from newsradar.app import Radar
from newsradar.timeutil import iso


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(predicate, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError("condition not reached in %.1fs" % timeout)


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "data").mkdir()
        (self.root / "static").symlink_to(REPO / "static")
        self.addCleanup(shutil.rmtree, self.root, True)

    def get(self, port, path):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.request("GET", path, headers={"Host": "127.0.0.1:%d" % port})
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def test_new_story_flows_from_feed_to_dashboard(self):
        feed = {"body": rss(rss_item("Baseline story", "https://a.test/old")), "etag": '"1"'}
        requests = []

        def handle(h):
            requests.append(h.headers.get("If-None-Match"))
            if h.headers.get("If-None-Match") == feed["etag"]:
                return respond(h, 304)
            respond(h, 200, feed["body"], {"ETag": feed["etag"], "Content-Type": "application/rss+xml"})

        with http_server(handle) as base:
            port = free_port()
            config = make_config(make_source(url=base + "/feed", interval=0.3), port=port, host_gap_seconds=0)
            radar = Radar(root=self.root, config=config)
            self.addCleanup(radar.store.db.close)
            server = threading.Thread(target=radar.serve, kwargs={"bridge": False}, daemon=True)
            server.start()
            try:
                wait_for(lambda: radar.store.source("a")["initialized_at"])
                status, health = wait_for(lambda: self._health(port))
                self.assertEqual((status, health["running"]), (200, True))
                wait_for(lambda: '"1"' in requests)  # revalidation with the stored ETag

                feed["body"] = rss(rss_item("Brand new AI model", "https://a.test/new", pubDate=iso(time.time())),
                                   rss_item("Baseline story", "https://a.test/old"))
                feed["etag"] = '"2"'
                event = wait_for(lambda: next((e for e in self.get(port, "/api/state")[1]["events"]
                                               if e["url"] == "https://a.test/new"), None))
                self.assertEqual((event["kind"], event["notify_state"], event["measurement_note"]), ("new", "pending", "published"))
                self.assertGreaterEqual(event["delay_seconds"], 0)
                library = self.get(port, "/api/library?q=Brand")[1]
                self.assertEqual(library["total"], 1)
                self.assertEqual(self.get(port, "/api/pending")[1][0]["url"], "https://a.test/new")
            finally:
                radar.stop.set()
                server.join(10)
            self.assertFalse(server.is_alive())

    def _health(self, port):
        try:
            status, body = self.get(port, "/health")
        except OSError:
            return None
        return (status, body) if status == 200 else None


if __name__ == "__main__":
    unittest.main()
