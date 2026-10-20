import gzip
import io
import socket
import time
import unittest
from http.client import HTTPException, IncompleteRead
from urllib.error import HTTPError

from support import T0, http_server, make_source, respond, rss, rss_item
from newsradar.fetcher import ACCEPT, USER_AGENT, decode_body, fetch_source, read_response, request_headers

FEED = rss(rss_item("AI release", "https://a.test/new"), rss_item("Weather report", "https://a.test/weather"))


def fetch(base, path="/feed", previous=None, **source):
    return fetch_source(make_source(url=base + path, **source), previous or {})


class RequestHeaderTests(unittest.TestCase):
    def test_defaults(self):
        headers = request_headers(make_source(), {})
        self.assertEqual(headers["User-Agent"], USER_AGENT)
        self.assertEqual(headers["Accept"], ACCEPT)
        self.assertEqual(headers["Accept-Encoding"], "gzip")
        self.assertEqual(headers["Cache-Control"], "no-cache")
        self.assertNotIn("If-None-Match", headers)

    def test_validators_and_cache_opt_in(self):
        headers = request_headers(make_source(respect_cache_ttl=True), {"etag": '"v1"', "last_modified": "Mon, 19 Oct 2026 00:00:00 GMT"})
        self.assertEqual(headers["If-None-Match"], '"v1"')
        self.assertEqual(headers["If-Modified-Since"], "Mon, 19 Oct 2026 00:00:00 GMT")
        self.assertNotIn("Cache-Control", headers)


class FetchOverHttpTests(unittest.TestCase):
    def test_ok_feed(self):
        seen = {}

        def handle(h):
            seen.update(h.headers)
            respond(h, 200, FEED, {"ETag": '"abc"', "Content-Type": "application/rss+xml"})
        with http_server(handle) as base:
            result = fetch(base, previous={"next_due": time.time() - 5})
        self.assertIsNone(result["error"])
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["headers"]["etag"], '"abc"')  # header names are lower-cased
        self.assertEqual([e["url"] for e in result["entries"]], ["https://a.test/new", "https://a.test/weather"])
        self.assertEqual((result["parsed_count"], result["transferred_bytes"], result["decoded_bytes"]), (2, len(FEED), len(FEED)))
        self.assertGreaterEqual(result["queue_delay_seconds"], 5)
        self.assertGreater(result["finished"], result["started"])
        self.assertGreaterEqual(result["fetch_ms"], 0)
        self.assertEqual(seen["User-Agent"], USER_AGENT)

    def test_conditional_request_and_304(self):
        def handle(h):
            if h.headers.get("If-None-Match") == '"v1"':
                return respond(h, 304)
            respond(h, 200, FEED, {"ETag": '"v1"'})
        with http_server(handle) as base:
            first = fetch(base)
            second = fetch(base, previous={"etag": first["headers"]["etag"]})
        self.assertEqual(second["status"], 304)
        self.assertIsNone(second["error"])
        self.assertEqual((second["entries"], second["transferred_bytes"], second["decoded_bytes"]), ([], 0, 0))

    def test_http_errors_are_recorded_with_headers(self):
        def handle(h):
            respond(h, 429 if h.path == "/limited" else 404, b"nope", {"Retry-After": "120"})
        with http_server(handle) as base:
            limited, missing = fetch(base, "/limited"), fetch(base, "/missing")
        self.assertEqual((limited["status"], limited["error"], limited["error_kind"]), (429, "HTTP 429", None))
        self.assertEqual(limited["headers"]["retry-after"], "120")
        self.assertEqual(missing["error"], "HTTP 404")

    def test_gzip_body(self):
        packed = gzip.compress(FEED)
        with http_server(lambda h: respond(h, 200, packed, {"Content-Encoding": "gzip"})) as base:
            result = fetch(base)
        self.assertIsNone(result["error"])
        self.assertEqual((result["transferred_bytes"], result["decoded_bytes"]), (len(packed), len(FEED)))

    def test_unsupported_encoding_is_a_parse_error(self):
        with http_server(lambda h: respond(h, 200, b"xx", {"Content-Encoding": "br"})) as base:
            result = fetch(base)
        self.assertEqual((result["error"], result["error_kind"]), ("ValueError: Unsupported content encoding", "parse"))
        self.assertEqual(result["status"], 200)

    def test_decompression_bomb_is_refused(self):
        bomb = gzip.compress(b"\0" * 8_000_001)
        with http_server(lambda h: respond(h, 200, bomb, {"Content-Encoding": "gzip"})) as base:
            result = fetch(base)
        self.assertIn("Decompressed response exceeds 8 MB", result["error"])

    def test_truncated_body_is_a_transport_error(self):
        def handle(h):
            h.send_response(200)
            h.send_header("Content-Length", "5000")
            h.end_headers()
            h.wfile.write(FEED[:50])
            h.wfile.flush()
            h.close_connection = True
        with http_server(handle) as base:
            result = fetch(base)
        self.assertEqual(result["error_kind"], "transport")
        self.assertTrue(result["error"].startswith("IncompleteRead"), result["error"])

    def test_slow_server_times_out(self):
        def handle(h):
            time.sleep(1.0)
            respond(h, 200, FEED)
        with http_server(handle) as base:
            result = fetch(base, timeout=0.2)
        self.assertEqual(result["error_kind"], "transport")
        self.assertIsNone(result["status"])

    def test_redirect_resolves_relative_links_against_final_url(self):
        def handle(h):
            if h.path == "/old":
                return respond(h, 301, b"", {"Location": "/blog/feed"})
            respond(h, 200, rss(rss_item("T", "post/1")))
        with http_server(handle) as base:
            result = fetch(base, "/old")
        self.assertEqual(result["entries"][0]["url"], base + "/blog/post/1")

    def test_connection_refused(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        result = fetch("http://127.0.0.1:%d" % port)
        self.assertEqual(result["error_kind"], "transport")
        self.assertIsNone(result["status"])

    def test_invalid_document_is_a_parse_error(self):
        with http_server(lambda h: respond(h, 200, b"<rss><channel><item>")) as base:
            result = fetch(base)
        self.assertEqual((result["status"], result["error_kind"]), (200, "parse"))
        self.assertTrue(result["error"].startswith("ParseError"))

    def test_filter_ai_keeps_only_matching_entries(self):
        with http_server(lambda h: respond(h, 200, FEED)) as base:
            result = fetch(base, filter_ai=True)
        self.assertEqual([e["title"] for e in result["entries"]], ["AI release"])
        self.assertEqual(result["parsed_count"], 2)

    def test_allow_empty_distinguishes_real_empty_feeds(self):
        empty = b"<rss><channel></channel></rss>"
        with http_server(lambda h: respond(h, 200, empty)) as base:
            self.assertTrue(fetch(base, allow_empty=True)["valid_empty_feed"])
            self.assertFalse(fetch(base)["valid_empty_feed"])


class FakeResponse:
    def __init__(self, body=b"", status=200, headers=None, url="https://a.test/feed"):
        self.body, self.status, self.url = io.BytesIO(body), status, url
        self.headers = headers or {}

    def read1(self, size):
        return self.body.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FetchWithFakeOpenerTests(unittest.TestCase):
    def fetch_raising(self, exc):
        def opener(*args, **kwargs):
            raise exc
        return fetch_source(make_source(), {}, opener=opener)

    def test_error_kinds(self):
        cases = [(OSError("down"), "transport"), (socket.timeout("slow"), "transport"), (HTTPException("bad"), "transport"),
                 (ValueError("bad url"), "parse"), (KeyError("x"), "parse")]
        for exc, kind in cases:
            with self.subTest(exc=exc):
                result = self.fetch_raising(exc)
                self.assertEqual(result["error_kind"], kind)
                self.assertTrue(result["error"].startswith(type(exc).__name__))

    def test_error_messages_are_bounded(self):
        self.assertLessEqual(len(self.fetch_raising(ValueError("x" * 5000))["error"]), 260)

    def test_http_error_without_headers(self):
        result = self.fetch_raising(HTTPError("https://a.test", 500, "boom", None, None))
        self.assertEqual((result["status"], result["error"], result["headers"]), (500, "HTTP 500", {}))

    def test_opener_receives_timeout_and_tls_context(self):
        calls = []

        def opener(request, **kwargs):
            calls.append((request, kwargs))
            return FakeResponse(FEED, headers={"Content-Type": "text/xml"})
        result = fetch_source(make_source(timeout=7), {}, opener=opener)
        self.assertIsNone(result["error"])
        request, kwargs = calls[0]
        self.assertEqual(kwargs["timeout"], 7)
        self.assertIn("context", kwargs)
        self.assertEqual(request.get_header("User-agent"), USER_AGENT)

    def test_queue_delay_absent_without_schedule(self):
        result = fetch_source(make_source(), {"next_due": 0}, opener=lambda *a, **k: FakeResponse(FEED))
        self.assertIsNone(result["queue_delay_seconds"])


class ReadResponseTests(unittest.TestCase):
    def test_reads_everything_in_chunks(self):
        data = b"x" * 200_000
        self.assertEqual(read_response(FakeResponse(data), time.monotonic() + 10), data)

    def test_size_limit(self):
        self.assertRaisesRegex(ValueError, "exceeds 8 MB", read_response, FakeResponse(b"x" * 101), time.monotonic() + 10, 100)
        self.assertEqual(read_response(FakeResponse(b"x" * 100), time.monotonic() + 10, 100), b"x" * 100)

    def test_content_length_mismatch(self):
        self.assertRaises(IncompleteRead, read_response, FakeResponse(b"<rss/>", headers={"Content-Length": "100"}),
                          time.monotonic() + 10)
        self.assertEqual(read_response(FakeResponse(b"ab", headers={"Content-Length": "2"}), time.monotonic() + 10), b"ab")

    def test_expired_budget(self):
        self.assertRaises(TimeoutError, read_response, FakeResponse(b"x"), time.monotonic() - 1)


class DecodeBodyTests(unittest.TestCase):
    def test_identity_and_gzip(self):
        self.assertEqual(decode_body(b"abc", ""), b"abc")
        self.assertEqual(decode_body(b"abc", "identity"), b"abc")
        self.assertEqual(decode_body(gzip.compress(b"abc"), "GZIP"), b"abc")

    def test_limits_and_errors(self):
        self.assertRaisesRegex(ValueError, "Decompressed", decode_body, gzip.compress(b"a" * 11), "gzip", 10)
        self.assertRaisesRegex(ValueError, "Unsupported", decode_body, b"abc", "deflate")
        self.assertRaises(OSError, decode_body, b"not gzip", "gzip")


if __name__ == "__main__":
    unittest.main()
