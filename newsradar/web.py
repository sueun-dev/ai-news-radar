"""Loopback-only dashboard and JSON API.

Requests must name ``127.0.0.1:<port>`` or ``localhost:<port>`` as Host (DNS-rebinding defence) and,
for POST, a matching Origin when one is sent. POST bodies are small JSON objects. Every response is
``no-store`` with a strict Content-Security-Policy.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Mapping, Optional
from urllib.parse import parse_qsl, urlsplit

LOG = logging.getLogger("radar")

CSP = ("default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
STATIC_FILES = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
                "/style.css": ("style.css", "text/css")}
CSV_EXPORTS = {"/events.csv": "events", "/checks.csv": "checks", "/items.csv": "items"}
MAX_POST_BYTES = 16384
TEST_EVENT_COOLDOWN = 5
HEARTBEAT_STALE = 90
LIBRARY_PAGE_MAX = 100
LIBRARY_QUERY_MAX = 200
CLIENT_DISCONNECTS = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)


class RadarHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        """A client hanging up mid-response is routine; anything else is logged with its traceback."""
        if isinstance(sys.exc_info()[1], CLIENT_DISCONNECTS):
            return
        LOG.exception("request from %s failed", client_address)
        super().handle_error(request, client_address)


def _require_str(data: Mapping[str, Any], key: str, default: Optional[str] = None) -> str:
    value = data.get(key, default) if default is not None else data[key]
    if not isinstance(value, str):
        raise ValueError("%s must be a string" % key)
    return value


def make_handler(radar):
    """Bind a request handler class to a running ``Radar`` (its config, store and poller state)."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        # --- response helpers ---------------------------------------------------------------

        def send(self, status: int, body: Any, mime: str = "application/json") -> None:
            """Send ``body`` (bytes, or a dict/list encoded as JSON) with the security headers."""
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", CSP)
            if mime.startswith("text/csv"):
                self.send_header("Content-Disposition", 'attachment; filename="%s.csv"' % self.path.split("/")[1].split(".")[0])
            self.end_headers()
            self.wfile.write(body)

        def error(self, status: int, message: str) -> None:
            self.send(status, {"error": message})

        def _local_origins(self):
            port = radar.config["port"]
            return ("127.0.0.1:%s" % port, "localhost:%s" % port)

        def valid_host(self) -> bool:
            return self.headers.get("Host") in self._local_origins()

        def valid_origin(self) -> bool:
            origin = self.headers.get("Origin")
            return not origin or origin in tuple("http://" + host for host in self._local_origins())

        # --- GET ------------------------------------------------------------------------------

        def do_GET(self):
            if not self.valid_host():
                return self.error(403, "Invalid host")
            url = urlsplit(self.path)
            route = GET_ROUTES.get(url.path)
            if route:
                return route(self, url.query)
            if url.path in CSV_EXPORTS:
                return self.send(200, radar.store.export(CSV_EXPORTS[url.path]), "text/csv; charset=utf-8")
            if url.path in STATIC_FILES:
                name, mime = STATIC_FILES[url.path]
                return self.send(200, (radar.root / "static" / name).read_bytes(), mime + "; charset=utf-8")
            return self.error(404, "Not found")

        def get_state(self, _query):
            self.send(200, radar.store.state())

        def get_pending(self, _query):
            self.send(200, radar.store.pending())

        def get_library(self, query):
            params = dict(parse_qsl(query))
            try:
                offset = max(0, int(params.get("offset", "0")))
                limit = max(1, min(LIBRARY_PAGE_MAX, int(params.get("limit", "50"))))
            except ValueError:
                return self.error(400, "Invalid pagination")
            self.send(200, radar.store.library(params.get("q", "")[:LIBRARY_QUERY_MAX], params.get("group", ""),
                                               offset, limit))

        def get_coverage(self, _query):
            path = radar.root / "coverage.json"
            self.send(200, json.loads(path.read_text()) if path.exists() else {"excluded": [], "notes": []})

        def get_health(self, _query):
            alive = radar.poller_thread is not None and radar.poller_thread.is_alive()
            fresh = radar.poller_heartbeat is not None and time.time() - radar.poller_heartbeat < HEARTBEAT_STALE
            self.send(200 if alive and fresh else 503, {
                "running": alive and fresh, "pid": os.getpid(), "root": str(radar.root.resolve()),
                "poller_alive": alive, "poller_heartbeat": radar.poller_heartbeat})

        # --- POST -----------------------------------------------------------------------------

        def do_POST(self):
            if not self.valid_host():
                return self.error(403, "Invalid host")
            if not self.valid_origin():
                return self.error(403, "Invalid origin")
            if self.headers.get_content_type() != "application/json":
                return self.error(415, "JSON required")
            try:
                length = int(self.headers.get("Content-Length", 0))
                if length < 0 or length > MAX_POST_BYTES:
                    return self.error(413, "Request too large")
                data = json.loads(self.rfile.read(length) or b"{}")
                route = POST_ROUTES.get(urlsplit(self.path).path)
                if route is None:
                    return self.error(404, "Not found")
                return route(self, data)
            except (KeyError, ValueError, TypeError) as exc:
                return self.error(400, str(exc))

        def post_test(self, _data):
            if time.monotonic() - radar.last_test < TEST_EVENT_COOLDOWN:
                return self.error(429, "5초 후 다시 시험하세요.")
            radar.last_test = time.monotonic()
            self.send(200, {"id": radar.store.test_event()})

        def post_notification_result(self, data):
            radar.store.notification_result(int(data["id"]), data["state"], data.get("error"))
            self.send(200, {"ok": True})

        def post_bridge(self, data):
            radar.store.record_bridge(data)
            self.send(200, {"ok": True})

        def post_opened(self, data):
            radar.store.mark_opened(int(data["id"]))
            self.send(200, {"ok": True})

        def post_x_log(self, data):
            url = _require_str(data, "url").strip()
            published_at = _require_str(data, "published_at", "").strip()
            self.send(200, {"id": radar.store.record_x(url, published_at)})

        def post_source_settings(self, data):
            radar.store.set_notification(data["id"], data["notify"])
            self.send(200, {"ok": True})

    GET_ROUTES = {"/api/state": Handler.get_state, "/api/pending": Handler.get_pending,
                  "/api/library": Handler.get_library, "/api/coverage": Handler.get_coverage,
                  "/health": Handler.get_health}
    POST_ROUTES = {"/api/test": Handler.post_test, "/api/notification-result": Handler.post_notification_result,
                   "/api/bridge": Handler.post_bridge, "/api/opened": Handler.post_opened,
                   "/api/x-log": Handler.post_x_log, "/api/source-settings": Handler.post_source_settings}
    return Handler
