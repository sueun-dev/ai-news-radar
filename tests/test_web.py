import http.client
import json
import logging
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from support import REPO, T0, entry, make_config, make_result, make_source
from newsradar.app import Radar
from newsradar.web import CSP, MAX_POST_BYTES, RadarHTTPServer


class WebTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "static").symlink_to(REPO / "static")
        (self.tmp / "data").mkdir()
        self.config = make_config(make_source(), make_source(id="b", name="Beta", url="https://b.test/f", group="research"))
        self.radar = Radar(root=self.tmp, config=self.config)
        self.server = RadarHTTPServer(("127.0.0.1", 0), self.radar.handler())
        self.config["port"] = self.port = self.server.server_port
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.host = "127.0.0.1:%d" % self.port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.radar.store.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def request(self, method, path, body=None, host="default", headers=None, length=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            if host:
                conn.putheader("Host", self.host if host == "default" else host)
            for key, value in (headers or {}).items():
                conn.putheader(key, value)
            if body is not None:
                conn.putheader("Content-Length", length if length is not None else str(len(body)))
            conn.endheaders()
            if body is not None and length is None:
                conn.send(body)
            response = conn.getresponse()
            data = response.read()
            return response.status, dict(response.getheaders()), data
        finally:
            conn.close()

    def get_json(self, path, **kw):
        status, headers, data = self.request("GET", path, **kw)
        return status, json.loads(data)

    def post(self, path, payload=b"{}", content_type="application/json", **kw):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        headers = dict(kw.pop("headers", {}))
        if content_type:
            headers["Content-Type"] = content_type
        status, _, data = self.request("POST", path, body, headers=headers, **kw)
        return status, (json.loads(data) if data else None)


class HostAndHeaderTests(WebTestCase):
    def test_accepted_hosts(self):
        for host in (self.host, "localhost:%d" % self.port):
            self.assertEqual(self.request("GET", "/api/state", host=host)[0], 200)

    def test_rejected_hosts(self):
        for host in ("evil.test", "127.0.0.1", "127.0.0.1:1", "LOCALHOST:%d" % self.port, None, "100.71.139.101:%d" % self.port):
            with self.subTest(host=host):
                status, _, data = self.request("GET", "/api/state", host=host)
                self.assertEqual((status, json.loads(data)), (403, {"error": "Invalid host"}))
                self.assertEqual(self.post("/api/test", host=host)[0], 403)

    def test_security_headers_on_every_response(self):
        for method, path in (("GET", "/"), ("GET", "/api/state"), ("GET", "/nope"), ("GET", "/events.csv")):
            _, headers, _ = self.request(method, path)
            self.assertEqual(headers["Content-Security-Policy"], CSP)
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(headers["Cache-Control"], "no-store")


class GetRouteTests(WebTestCase):
    def test_static_files(self):
        for path, mime, needle in (("/", "text/html", b"<!doctype html>"), ("/app.js", "text/javascript", b"use strict"),
                                   ("/style.css", "text/css", b":root")):
            status, headers, data = self.request("GET", path)
            self.assertEqual((status, headers["Content-Type"]), (200, mime + "; charset=utf-8"))
            self.assertIn(needle, data[:200])

    def test_unknown_paths_and_query_strings(self):
        self.assertEqual(self.get_json("/api/nope"), (404, {"error": "Not found"}))
        self.assertEqual(self.get_json("/../config.json")[0], 404)
        self.assertEqual(self.request("GET", "/api/state?cache=1")[0], 200)

    def test_state_and_pending(self):
        status, state = self.get_json("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual([s["id"] for s in state["sources"]], ["a", "b"])
        self.assertEqual(self.get_json("/api/pending"), (200, []))

    def test_library_parameters(self):
        self.radar.store.apply(self.config["sources"][0], make_result(T0, entries=[entry(i) for i in range(3)]))
        self.assertEqual(self.get_json("/api/library?limit=2")[1]["limit"], 2)
        status, page = self.get_json("/api/library?offset=-5&limit=999")
        self.assertEqual((status, page["offset"], page["limit"], page["total"]), (200, 0, 100, 3))
        self.assertEqual(self.get_json("/api/library?limit=0")[1]["limit"], 1)
        self.assertEqual(self.get_json("/api/library?q=Story%201")[1]["total"], 1)
        self.assertEqual(self.get_json("/api/library?group=research")[1]["total"], 0)
        for bad in ("offset=x", "limit=1.5", "offset=1e3"):
            self.assertEqual(self.get_json("/api/library?" + bad), (400, {"error": "Invalid pagination"}))
        self.assertEqual(self.get_json("/api/library?limit=")[1]["limit"], 50)  # blank values are dropped

    def test_library_query_is_truncated(self):
        with patch.object(self.radar.store, "library", wraps=self.radar.store.library) as spy:
            self.request("GET", "/api/library?q=" + "a" * 500)
        self.assertEqual(len(spy.call_args[0][0]), 200)

    def test_coverage(self):
        self.assertEqual(self.get_json("/api/coverage"), (200, {"excluded": [], "notes": []}))
        (self.tmp / "coverage.json").write_text(json.dumps({"excluded": [{"name": "X"}], "notes": ["n"]}))
        self.assertEqual(self.get_json("/api/coverage")[1]["notes"], ["n"])

    def test_csv_exports(self):
        for table in ("events", "checks", "items"):
            status, headers, data = self.request("GET", "/%s.csv?download=1" % table)
            self.assertEqual(status, 200)
            self.assertEqual(headers["Content-Type"], "text/csv; charset=utf-8")
            self.assertEqual(headers["Content-Disposition"], 'attachment; filename="%s.csv"' % table)
            self.assertTrue(data.startswith("﻿".encode()))

    def test_health(self):
        status, health = self.get_json("/health")
        self.assertEqual((status, health["running"], health["poller_alive"]), (503, False, False))
        self.assertEqual(health["root"], str(self.tmp.resolve()))
        alive = threading.Event()
        worker = threading.Thread(target=alive.wait, daemon=True)
        worker.start()
        self.radar.poller_thread = worker
        self.radar.poller_heartbeat = time.time()
        status, health = self.get_json("/health")
        self.assertEqual((status, health["running"]), (200, True))
        self.radar.poller_heartbeat = time.time() - 120
        self.assertEqual(self.get_json("/health")[1]["running"], False)
        alive.set()


class PostGuardTests(WebTestCase):
    def test_origin(self):
        for origin, expected in (("http://%s" % self.host, 200), ("http://localhost:%d" % self.port, 200),
                                 ("https://%s" % self.host, 403), ("http://evil.test", 403), ("null", 403)):
            with self.subTest(origin=origin):
                status, body = self.post("/api/bridge", {"a": 1}, headers={"Origin": origin})
                self.assertEqual(status, expected)
                if expected == 403:
                    self.assertEqual(body, {"error": "Invalid origin"})

    def test_content_type(self):
        self.assertEqual(self.post("/api/bridge", {"a": 1}, "application/json; charset=utf-8")[0], 200)
        for ctype in ("text/plain", "application/x-www-form-urlencoded", None):
            self.assertEqual(self.post("/api/bridge", {"a": 1}, ctype), (415, {"error": "JSON required"}))

    def test_length_limits(self):
        self.assertEqual(self.post("/api/bridge", b"{}", length="-1"), (413, {"error": "Request too large"}))
        self.assertEqual(self.post("/api/bridge", b"{}", length=str(MAX_POST_BYTES + 1)), (413, {"error": "Request too large"}))
        self.assertEqual(self.post("/api/bridge", b"{}", length="abc")[0], 400)
        big = json.dumps({"pad": "x" * (MAX_POST_BYTES - 20)}).encode()
        self.assertEqual(self.post("/api/bridge", big)[0], 200)

    def test_bad_json_and_unknown_route(self):
        for payload in (b"not json", b"\xff\xfe", b'{"a":'):
            self.assertEqual(self.post("/api/bridge", payload)[0], 400)
        self.assertEqual(self.post("/api/unknown"), (404, {"error": "Not found"}))
        self.assertEqual(self.post("/api/unknown", b"oops")[0], 400)  # the body is parsed first


class PostRouteTests(WebTestCase):
    def test_test_event_is_rate_limited(self):
        with patch("newsradar.web.time.monotonic", return_value=1000.0):
            status, body = self.post("/api/test")
            self.assertEqual(status, 200)
            self.assertGreater(body["id"], 0)
            self.assertEqual(self.post("/api/test"), (429, {"error": "5초 후 다시 시험하세요."}))
        with patch("newsradar.web.time.monotonic", return_value=1006.0):
            self.assertEqual(self.post("/api/test")[0], 200)

    def test_notification_result(self):
        eid = self.radar.store.test_event()
        self.assertEqual(self.post("/api/notification-result", {"id": eid, "state": "accepted"}), (200, {"ok": True}))
        self.assertEqual(self.radar.store.pending(), [])
        for payload in ({"id": eid, "state": "maybe"}, {"state": "accepted"}, {"id": "x", "state": "accepted"}, [1, 2], "s"):
            self.assertEqual(self.post("/api/notification-result", payload)[0], 400, payload)

    def test_bridge_and_opened(self):
        self.assertEqual(self.post("/api/bridge", {"authorization": 2}), (200, {"ok": True}))
        self.assertEqual(self.get_json("/api/state")[1]["meta"]["notification_bridge"]["authorization"], 2)
        self.assertEqual(self.post("/api/bridge", "text")[0], 400)
        eid = self.radar.store.test_event()
        self.assertEqual(self.post("/api/opened", {"id": eid}), (200, {"ok": True}))
        for payload in ({}, {"id": None}, {"id": "abc"}):
            self.assertEqual(self.post("/api/opened", payload)[0], 400)

    def test_x_log(self):
        status, body = self.post("/api/x-log", {"url": " https://x.com/OpenAI/status/12 "})
        self.assertEqual(status, 200)
        self.assertGreater(body["id"], 0)
        status, body = self.post("/api/x-log", {"url": "https://evil.test/a"})
        self.assertEqual(status, 400)
        self.assertIn("x.com", body["error"])
        # Previously these crashed the request thread and dropped the connection without a response.
        for payload in ({"url": 5}, {"url": "https://x.com/OpenAI/status/1", "published_at": None}, {}, ["url"]):
            self.assertEqual(self.post("/api/x-log", payload)[0], 400, payload)

    def test_source_settings(self):
        self.assertEqual(self.post("/api/source-settings", {"id": "a", "notify": False}), (200, {"ok": True}))
        self.assertFalse(self.get_json("/api/state")[1]["sources"][0]["notify"])
        for payload in ({"id": "zzz", "notify": True}, {"id": "a", "notify": "yes"}, {"id": ["a"], "notify": True}, {"id": "a"}):
            self.assertEqual(self.post("/api/source-settings", payload)[0], 400, payload)


class ServerErrorHandlingTests(unittest.TestCase):
    def setUp(self):
        self.server = RadarHTTPServer(("127.0.0.1", 0), None)
        self.addCleanup(self.server.server_close)

    def test_client_disconnects_are_silent(self):
        for exc in (BrokenPipeError(), ConnectionResetError(), ConnectionAbortedError()):
            with patch("sys.stderr") as stderr, patch("newsradar.web.LOG") as log:
                try:
                    raise exc
                except OSError:
                    self.server.handle_error(None, ("127.0.0.1", 1))
            log.exception.assert_not_called()
            stderr.write.assert_not_called()

    def test_other_errors_are_logged(self):
        with patch("sys.stderr"), self.assertLogs("radar", logging.ERROR) as logs:
            try:
                raise RuntimeError("handler bug")
            except RuntimeError:
                self.server.handle_error(None, ("127.0.0.1", 1))
        self.assertIn("request from ('127.0.0.1', 1) failed", logs.output[0])


if __name__ == "__main__":
    unittest.main()
