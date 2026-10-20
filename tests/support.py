"""Shared fixtures for the test suite: temporary stores, fetch results and a local HTTP server."""
import contextlib
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from newsradar.items import item  # noqa: E402
from newsradar.store import Store  # noqa: E402

T0 = 1_800_000_000.0  # a fixed "now" for deterministic tests


def make_source(**overrides):
    source = dict(id="a", name="Alpha Official", url="https://a.test/feed", kind="feed", interval=60,
                  group="official")
    source.update(overrides)
    return source


def make_config(*sources, **overrides):
    config = dict(port=8741, x_accounts=["OpenAI"], notify_prerelease=False,
                  sources=list(sources) or [make_source()])
    config.update(overrides)
    return config


def make_result(finished, status=200, entries=(), headers=None, **extra):
    result = dict(started=finished - 0.1, finished=finished, status=status, headers=headers or {},
                  entries=list(entries), fetch_ms=1.0)
    result.update(extra)
    return result


def entry(n, title=None, body="body", raw_time=None, basis="published", host="a.test"):
    url = "https://%s/post/%s" % (host, n)
    return item(url, title or "Story %s" % n, url, body, raw_time, basis)


class StoreTestCase(unittest.TestCase):
    """A fresh Store on a temporary database for every test."""
    sources = None

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.source = make_source()
        self.config = make_config(*(self.sources or [self.source]))
        self.store = Store(self.tmp / "radar.sqlite", self.config)

    def tearDown(self):
        self.store.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def apply(self, entries, t, source=None, **kw):
        return self.store.apply(source or self.source, make_result(t, entries=entries, **kw))

    def events(self):
        return [dict(r) for r in self.store.db.execute("SELECT * FROM events ORDER BY id")]

    def row(self, sid=None):
        return self.store.source(sid or self.source["id"])


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        if not isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            super().handle_error(request, client_address)  # a client giving up early is part of some tests


@contextlib.contextmanager
def http_server(handle):
    """Serve ``handle(request_handler)`` for every GET on a free loopback port; yields the base URL."""
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_):
            pass

        def do_GET(self):
            handle(self)

    server = QuietServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % server.server_port
    finally:
        server.shutdown()
        server.server_close()


def respond(handler, status=200, body=b"", headers=None):
    handler.send_response(status)
    headers = dict(headers or {})
    headers.setdefault("Content-Length", str(len(body)))
    for key, value in headers.items():
        handler.send_header(key, value)
    handler.end_headers()
    if body:
        handler.wfile.write(body)


def rss(*items_xml, channel=""):
    return ("<rss version='2.0'><channel>%s%s</channel></rss>" % (channel, "".join(items_xml))).encode()


def rss_item(title, link=None, **children):
    parts = ["<title>%s</title>" % title]
    if link is not None:
        parts.append("<link>%s</link>" % link)
    parts += ["<%s>%s</%s>" % (k, v, k) for k, v in children.items()]
    return "<item>%s</item>" % "".join(parts)
