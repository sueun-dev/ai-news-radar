"""Property tests with seeded random input: no crash outside the documented errors, invariants always hold."""
import json
import math
import random
import string
import unittest
import xml.etree.ElementTree as ET

from support import T0, StoreTestCase, make_source, rss, rss_item
from newsradar.items import canonical_url, clean, is_web_url, item
from newsradar.parsers import parse_source
from newsradar.policy import MAX_SERVER_DELAY, cache_delay, next_poll_interval, retry_after
from newsradar.timeutil import parse_time

PARSE_ERRORS = (ValueError, ET.ParseError)   # JSON/Unicode errors are ValueErrors
PRECISIONS = {"instant", "date", "timezone_unknown", "unknown"}
ALPHABET = string.ascii_letters + string.digits + " <>/=\"'&;#?%:.-_[]{}()@!\t\n" + "가é中 "

SOURCES = {
    "feed": make_source(url="https://a.test/feed"),
    "sitemap": make_source(kind="sitemap", url="https://a.test/sitemap.xml", link_pattern=r"/news/"),
    "hf_models": make_source(kind="hf_models", url="https://huggingface.co/api/models", author="lab"),
    "anthropic": make_source(kind="anthropic", url="https://www.anthropic.com/news"),
    "links": make_source(kind="links", url="https://a.test/blog", link_pattern=r"^https://a\.test/blog/[^/?#]+$",
                         url_title_fallback=True),
    "changelog": make_source(kind="changelog", url="https://a.test/changelog"),
}
SEEDS = {
    "feed": rss(rss_item("AI news", "https://a.test/1", pubDate="Mon, 19 Oct 2026 12:00:00 GMT", description="<p>x</p>"),
                rss_item("Second", "/2")),
    "sitemap": b'<urlset><url><loc>https://a.test/news/a</loc><lastmod>2026-10-19</lastmod></url></urlset>',
    "hf_models": json.dumps([{"id": "lab/m1", "createdAt": "2026-10-19T00:00:00Z"}, {"id": "lab/m2"}]).encode(),
    "anthropic": b'<a href="/news/a">Claude news</a><a href="/news/b#x">Other</a>',
    "links": b'<nav><a href="/blog/n">nav link title</a></nav><a href="/blog/a">Article title that is long</a>',
    "changelog": b"<h2 id='d'>October 19, 2026</h2><p>Body</p><h2>2026-09 notes</h2><p>More</p>",
}


def mutate(rng, data):
    data = bytearray(data)
    for _ in range(rng.randint(1, 6)):
        op = rng.random()
        pos = rng.randint(0, len(data))
        if op < .3 and data:
            del data[pos:pos + rng.randint(1, 8)]
        elif op < .6:
            data[pos:pos] = rng.choice([b"<", b">", b"&", b"&amp;", b"]]>", b"<a href='http://[::1'>", b"\x00", b"\xff",
                                        b"<item>", b"</entry>", b'"', b"\xe2\x80", b"<a href>", b"javascript:", b"{", b"null"])
        else:
            data[pos:pos] = "".join(rng.choice(ALPHABET) for _ in range(rng.randint(1, 10))).encode()
    return bytes(data)


def assert_items(case, records, kind):
    case.assertIsInstance(records, list)
    for record in records:
        case.assertTrue(is_web_url(record["url"]), (kind, record["url"]))
        case.assertLessEqual(len(record["title"]), 300)
        case.assertRegex(record["content_hash"], r"^[0-9a-f]{64}$")
        case.assertIn(record["precision"], PRECISIONS)
        if record["precision"] == "instant":
            case.assertIsInstance(record["source_time"], float)
        else:
            case.assertIsNone(record["source_time"])
        case.assertIsInstance(record["ai_match"], bool)


class ParserFuzzTests(unittest.TestCase):
    def test_mutated_documents(self):
        rng = random.Random(1)
        for kind, seed in SEEDS.items():
            for _ in range(400):
                data = mutate(rng, seed)
                try:
                    records = parse_source(SOURCES[kind], data)
                except PARSE_ERRORS:
                    continue
                assert_items(self, records, kind)

    def test_random_bytes(self):
        rng = random.Random(2)
        for kind in SEEDS:
            for _ in range(150):
                data = bytes(rng.randrange(256) for _ in range(rng.randint(0, 200)))
                try:
                    assert_items(self, parse_source(SOURCES[kind], data), kind)
                except PARSE_ERRORS:
                    pass

    def test_html_parsers_never_raise(self):
        rng = random.Random(3)
        for kind in ("anthropic", "links", "changelog"):
            for _ in range(300):
                html = "".join(rng.choice(["<a href='/news/x'>", "<a href='/blog/y'>", "</a>", "<nav>", "</nav>", "<h2>",
                                           "</h2>", "<script>", "</script>", "October 1, 2026", "text ", "&lt;", "<a href>",
                                           "<a href='http://[::1'>", "<footer>", "<p>", rng.choice(ALPHABET)])
                               for _ in range(rng.randint(0, 40)))
                assert_items(self, parse_source(SOURCES[kind], html.encode()), kind)


class NormalisationFuzzTests(unittest.TestCase):
    def test_canonical_url_is_idempotent_or_rejects(self):
        rng = random.Random(4)
        # Whitespace is excluded: stored keys are always computed from the raw href in one pass.
        parts = ["https://", "http://", "HTTP://", "a.test", "A.Test:80", "[::1]", "[bad", "/", "/path/", "?", "&", "=",
                 "utm_source=x", "UTM_x=y", "fbclid=1", "id=2", "#", "frag", "%20", "+", "..", "@", "가"]
        for _ in range(3000):
            url = "".join(rng.choice(parts) for _ in range(rng.randint(0, 9)))
            try:
                once = canonical_url(url)
            except ValueError:
                continue
            self.assertEqual(canonical_url(once), once, url)

    def test_clean_output_shape(self):
        rng = random.Random(5)
        for _ in range(3000):
            text = "".join(rng.choice(ALPHABET + "<b></b>&nbsp;&#x41;") for _ in range(rng.randint(0, 60)))
            out = clean(text)
            self.assertEqual(out, out.strip())
            self.assertNotIn("  ", out)

    def test_parse_time_total(self):
        rng = random.Random(6)
        values = [None, 0, 1.5, [], {}, True, "", " "]
        values += ["".join(rng.choice(string.digits + "-:TZ+ .,WOctMon") for _ in range(rng.randint(0, 30))) for _ in range(3000)]
        for value in values:
            ts, precision = parse_time(value)
            self.assertIn(precision, PRECISIONS)
            self.assertEqual(ts is not None, precision == "instant")
            if ts is not None:
                self.assertTrue(math.isfinite(ts))


class PolicyFuzzTests(unittest.TestCase):
    def test_delays_are_finite_and_bounded(self):
        rng = random.Random(7)
        junk = ["", "0", "-1", "60", "1e400", "inf", "-inf", "nan", "abc", "Mon, 19 Oct 2026 12:00:00 GMT", "max-age=5",
                "max-age=99999999999", "no-cache", ' max-age="7"', "9" * 40, "1.5", "  12  "]
        source = make_source(interval=300, respect_cache_ttl=True)
        for _ in range(3000):
            headers = {name: rng.choice(junk) for name in ("retry-after", "cache-control", "age", "expires", "date",
                                                         "x-poll-interval") if rng.random() < .6}
            for value in (retry_after(headers, T0), cache_delay(headers)):
                self.assertTrue(math.isfinite(value) and 0 <= value <= MAX_SERVER_DELAY, headers)
            delay = next_poll_interval(source, ok=rng.random() < .5, status=rng.choice([None, 200, 304, 404, 429, 503]),
                                       failures=rng.randint(1, 30), error_kind=rng.choice([None, "transport", "parse"]),
                                       headers=headers, now=T0)
            self.assertTrue(math.isfinite(delay) and 0 < delay <= MAX_SERVER_DELAY, headers)


class StoreFuzzTests(StoreTestCase):
    def test_random_results_keep_invariants(self):
        rng = random.Random(8)
        t = T0
        pool = [item("https://a.test/%d" % i, "Title %d" % i, "https://a.test/%d" % i) for i in range(8)]
        for _ in range(300):
            t += rng.choice([1, 60, 3600])
            status = rng.choice([200, 200, 200, 304, 404, 429, 500, None])
            entries = [dict(e, content_hash=rng.choice(["a" * 64, "b" * 64]), title=rng.choice([e["title"], e["title"] + "!"]))
                       for e in rng.sample(pool, rng.randint(0, 5))] if status == 200 else []
            headers = {k: rng.choice(["120", "inf", "abc", "max-age=60", "e1"]) for k in ("retry-after", "etag", "x-poll-interval")
                       if rng.random() < .3}
            error = None if status in (200, 304) and rng.random() < .9 else "boom"
            self.store.apply(self.source, dict(started=t - 1, finished=t, status=status, headers=headers, entries=entries,
                                               fetch_ms=1, error=error, error_kind=rng.choice([None, "transport", "parse"]),
                                               parsed_count=rng.randint(0, 3), valid_empty_feed=rng.random() < .2))
            row = self.row()
            self.assertIn(row["status"], ("ok", "error"))
            self.assertGreaterEqual(row["failures"], 0)
            self.assertTrue(t < row["next_due"] <= t + MAX_SERVER_DELAY)
            self.assertEqual(row["last_checked"], t)
        events = self.events()
        if events:
            baseline = self.row()["initialized_at"]
            self.assertTrue(all(e["detected_at"] > baseline for e in events))
            self.assertTrue(all(e["kind"] in ("new", "updated", "backfill") for e in events))


if __name__ == "__main__":
    unittest.main()
