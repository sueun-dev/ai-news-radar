import json
import time
import unittest
import xml.etree.ElementTree as ET

from support import make_source, rss, rss_item
from newsradar.items import digest
from newsradar.parsers import (CHANGELOG_SECTIONS, PARSERS, SOURCE_KINDS, is_feed_document, local_name, parse_source,
                               xml_root)

FEED = make_source(url="https://a.test/blog/feed")
ATOM_NS = "http://www.w3.org/2005/Atom"


def atom(*entries):
    return ('<feed xmlns="%s">%s</feed>' % (ATOM_NS, "".join(entries))).encode()


def atom_entry(title, links="", **children):
    body = "".join("<%s>%s</%s>" % (k, v, k) for k, v in children.items())
    return "<entry><title>%s</title>%s%s</entry>" % (title, links, body)


class XmlSafetyTests(unittest.TestCase):
    def test_entity_declarations_are_refused(self):
        bomb = (b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;">]>'
                b'<rss><channel><item><title>&lol2;</title></item></channel></rss>')
        started = time.monotonic()
        with self.assertRaisesRegex(ValueError, "entity declarations"):
            parse_source(FEED, bomb)
        self.assertLess(time.monotonic() - started, 1)

    def test_external_entity_is_refused(self):
        xxe = b'<!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]><rss><channel><item><title>&x;</title></item></channel></rss>'
        self.assertRaises(ValueError, parse_source, FEED, xxe)

    def test_entity_declarations_in_utf16_are_refused(self):
        bomb = ('<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE r [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]>'
                '<rss><channel><item><title>&b;</title></item></channel></rss>').encode("utf-16")
        self.assertRaisesRegex(ValueError, "entity declarations", parse_source, FEED, bomb)

    def test_utf16_feed_with_doctype_parses(self):
        data = ('<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE rss><rss><channel><item><title>Wide</title>'
                '<link>https://a.test/w</link></item></channel></rss>').encode("utf-16")
        self.assertEqual([r["title"] for r in parse_source(FEED, data)], ["Wide"])

    def test_malformed_document_with_doctype_keeps_the_usual_parse_error(self):
        self.assertRaises(ET.ParseError, parse_source, FEED, b"<!DOCTYPE rss><rss><channel><item></rss>")

    def test_doctype_without_entities_is_fine(self):
        data = b'<!DOCTYPE rss><rss><channel><item><title>T</title><link>https://a.test/1</link></item></channel></rss>'
        self.assertEqual(len(parse_source(FEED, data)), 1)

    def test_malformed_and_empty_xml_raise_parse_errors(self):
        for data in (b"", b"<rss><channel><item>", b"not xml at all", b"<html><body><p>unclosed</body></html>"):
            with self.subTest(data=data):
                self.assertRaises(ET.ParseError, parse_source, FEED, data)

    def test_helpers(self):
        self.assertEqual(local_name("{ns}entry"), "entry")
        self.assertEqual(local_name("item"), "item")
        self.assertEqual(xml_root(b"<a/>").tag, "a")
        for root in (b"<rss/>", atom(), b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"/>'):
            self.assertTrue(is_feed_document(root))
        self.assertFalse(is_feed_document(b"<html/>"))


class RssTests(unittest.TestCase):
    def test_basic_item(self):
        data = rss(rss_item("AI <b>launch</b>", "https://a.test/p/1?utm_source=x", pubDate="Mon, 19 Oct 2026 12:00:00 GMT",
                            description="Hello &amp; welcome"))
        [record] = parse_source(FEED, data)
        self.assertEqual(record["url"], "https://a.test/p/1")
        self.assertEqual(record["key"], record["url"])
        self.assertEqual(record["title"], "AI launch")
        self.assertEqual(record["precision"], "instant")
        self.assertEqual(record["basis"], "published")
        self.assertEqual(record["content_hash"], digest("Hello & welcome"))

    def test_relative_links_resolve_against_feed_url(self):
        [record] = parse_source(FEED, rss(rss_item("T", "../post/2/")))
        self.assertEqual(record["url"], "https://a.test/post/2")

    def test_guid_used_only_when_link_missing_and_absolute(self):
        data = rss(rss_item("A", None, guid="https://a.test/guid-url"), rss_item("B", "https://a.test/link", guid="https://a.test/ignored"))
        self.assertEqual([r["url"] for r in parse_source(FEED, data)], ["https://a.test/guid-url", "https://a.test/link"])

    def test_item_without_any_link_falls_back_to_the_feed_url(self):
        # Characterisation: such entries share the feed URL as key (no live source does this).
        [record] = parse_source(FEED, rss(rss_item("No link", None, guid="tag:a.test,2026:1")))
        self.assertEqual(record["url"], "https://a.test/blog/feed")

    def test_non_web_links_are_skipped(self):
        data = rss(*(rss_item(str(i), link) for i, link in enumerate(
            ["javascript:alert(1)", "mailto:a@a.test", "ftp://a.test/f", "data:text/plain,x"])), rss_item("ok", "http://a.test/ok"))
        self.assertEqual([r["title"] for r in parse_source(FEED, data)], ["ok"])

    def test_body_preference_content_then_encoded_then_description(self):
        content = ('<rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel>'
                   '<item><title>T</title><link>https://a.test/1</link><description>desc</description>'
                   '<content:encoded><![CDATA[<p>full <em>text</em></p>]]></content:encoded></item>'
                   '<item><title>T</title><link>https://a.test/2</link><description>only desc</description></item>'
                   '</channel></rss>').encode()
        first, second = parse_source(FEED, content)
        self.assertEqual(first["content_hash"], digest("full text"))
        self.assertEqual(second["content_hash"], digest("only desc"))

    def test_dc_date_and_rdf(self):
        xml = (b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/" '
               b'xmlns:dc="http://purl.org/dc/elements/1.1/"><item><title>T</title><link>https://a.test/x</link>'
               b'<dc:date>2026-10-19T00:00:00Z</dc:date></item></rdf:RDF>')
        [record] = parse_source(FEED, xml)
        self.assertEqual((record["precision"], record["basis"]), ("instant", "published"))

    def test_time_basis_override(self):
        [record] = parse_source(dict(FEED, time_basis="index_published"), rss(rss_item("T", "https://a.test/1")))
        self.assertEqual(record["basis"], "index_published")

    def test_max_items_and_default_limit(self):
        data = rss(*(rss_item("T%d" % i, "https://a.test/%d" % i) for i in range(2005)))
        self.assertEqual(len(parse_source(FEED, data)), 2000)
        self.assertEqual(len(parse_source(dict(FEED, max_items=3), data)), 3)

    def test_declared_encoding_is_honoured(self):
        data = '<?xml version="1.0" encoding="ISO-8859-1"?><rss><channel><item><title>Café</title><link>https://a.test/c</link></item></channel></rss>'
        [record] = parse_source(FEED, data.encode("latin-1"))
        self.assertEqual(record["title"], "Café")

    def test_utf8_bom(self):
        [record] = parse_source(FEED, b"\xef\xbb\xbf" + rss(rss_item("BOM", "https://a.test/b")))
        self.assertEqual(record["title"], "BOM")

    def test_missing_title_gives_empty_title(self):
        data = b"<rss><channel><item><link>https://a.test/x</link></item></channel></rss>"
        self.assertEqual(parse_source(FEED, data)[0]["title"], "")


class AtomTests(unittest.TestCase):
    def test_alternate_link_published_and_summary(self):
        data = atom(atom_entry("A", '<link rel="self" href="https://a.test/self"/><link href="/posts/a/"/>',
                               published="2026-10-19T00:00:00Z", updated="2026-10-20T00:00:00Z", summary="S"))
        [record] = parse_source(FEED, data)
        self.assertEqual(record["url"], "https://a.test/posts/a")
        self.assertEqual(record["basis"], "published")
        self.assertEqual(record["source_time_raw"], "2026-10-19T00:00:00Z")
        self.assertEqual(record["content_hash"], digest("S"))

    def test_updated_only_is_labelled_feed_updated(self):
        [record] = parse_source(FEED, atom(atom_entry("A", '<link href="https://a.test/a"/>', updated="2026-10-19T00:00:00Z")))
        self.assertEqual((record["basis"], record["precision"]), ("feed_updated", "instant"))

    def test_first_alternate_wins_and_no_alternate_falls_back_to_feed_url(self):
        data = atom(atom_entry("A", '<link rel="alternate" href="https://a.test/1"/><link rel="alternate" href="https://a.test/2"/>'),
                    atom_entry("B", '<link rel="enclosure" href="https://a.test/file.mp3"/>'))
        self.assertEqual([r["url"] for r in parse_source(FEED, data)], ["https://a.test/1", "https://a.test/blog/feed"])

    def test_content_preferred_over_summary(self):
        [record] = parse_source(FEED, atom(atom_entry("A", '<link href="https://a.test/a"/>', summary="short", content="long body")))
        self.assertEqual(record["content_hash"], digest("long body"))


class SitemapTests(unittest.TestCase):
    SOURCE = make_source(kind="sitemap", url="https://a.test/sitemap.xml", link_pattern=r"/news/")

    def test_matching_urls_with_lastmod(self):
        xml = (b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
               b'<url><loc> https://A.test/news/one/?utm_campaign=x </loc><lastmod>2026-10-19T00:00:00Z</lastmod></url>'
               b'<url><loc>https://a.test/pricing</loc></url>'
               b'<url><loc>https://a.test/news/two</loc><lastmod>2026-10-19</lastmod></url>'
               b'<url><loc>ftp://a.test/news/three</loc></url></urlset>')
        one, two = parse_source(self.SOURCE, xml)
        self.assertEqual(one["url"], "https://a.test/news/one")
        self.assertEqual(one["title"], "[사이트맵 · 제목 미제공] /news/one")
        self.assertEqual((one["basis"], one["precision"]), ("sitemap_modified", "instant"))
        self.assertEqual(two["precision"], "date")

    def test_sitemap_index_and_unnamespaced(self):
        index = b'<sitemapindex><sitemap><loc>https://a.test/news/sitemap-1.xml</loc></sitemap></sitemapindex>'
        self.assertEqual(parse_source(self.SOURCE, index), [])
        plain = b"<urlset><url><loc>https://a.test/news/x</loc></url></urlset>"
        self.assertEqual(len(parse_source(self.SOURCE, plain)), 1)


class HuggingFaceTests(unittest.TestCase):
    SOURCE = make_source(kind="hf_models", url="https://huggingface.co/api/models?author=Lab", author="Lab")

    def parse(self, rows):
        return parse_source(self.SOURCE, json.dumps(rows).encode())

    def test_author_filter_is_case_insensitive(self):
        rows = self.parse([dict(id="lab/a", createdAt="2026-10-19T00:00:00.000Z"), dict(id="LAB/b"), dict(id="other/c")])
        self.assertEqual([r["key"] for r in rows], ["lab/a", "LAB/b"])
        self.assertEqual(rows[0]["url"], "https://huggingface.co/lab/a")
        self.assertEqual((rows[0]["basis"], rows[0]["precision"]), ("repository_created", "instant"))
        self.assertEqual(rows[1]["precision"], "unknown")

    def test_private_malformed_and_non_object_rows_are_skipped(self):
        rows = [dict(id="lab/private", private=True), dict(id="lab/a/b"), dict(id="lab"), dict(id=" lab/x"), dict(id=5),
                dict(id=None), dict(), "lab/string", 7, None, ["lab/list"], dict(id="lab/ok")]
        self.assertEqual([r["key"] for r in self.parse(rows)], ["lab/ok"])

    def test_payload_must_be_a_list(self):
        for payload in (b'{"id": "lab/a"}', b'"lab/a"', b"null"):
            self.assertRaisesRegex(ValueError, "model list", parse_source, self.SOURCE, payload)
        self.assertRaises(ValueError, parse_source, self.SOURCE, b"not json")


class AnthropicTests(unittest.TestCase):
    SOURCE = make_source(kind="anthropic", url="https://www.anthropic.com/news")

    def parse(self, html):
        return {r["url"]: r["title"] for r in parse_source(self.SOURCE, html.encode())}

    def test_news_links_only(self):
        found = self.parse('<a href="/news/claude-5">Claude <b>5</b></a><a href="/privacy">Privacy</a>'
                           '<a href="https://www.anthropic.com/news/b#top">B</a><a href="https://example.com/news/x">X</a>'
                           '<a href="/news/">Index</a>')
        self.assertEqual(found, {"https://www.anthropic.com/news/claude-5": "Claude 5", "https://www.anthropic.com/news/b": "B"})

    def test_longest_title_wins_and_empty_anchors_are_ignored(self):
        found = self.parse('<a href="/news/a">Short</a><a href="/news/a">A much longer title</a><a href="/news/a">Mid</a>'
                           '<a href="/news/empty"> </a>')
        self.assertEqual(found, {"https://www.anthropic.com/news/a": "A much longer title"})

    def test_valueless_href_and_unclosed_anchor_do_not_crash(self):
        self.assertEqual(self.parse('<a href>x</a><a href="/news/open">never closed'), {})

    def test_records(self):
        [record] = parse_source(self.SOURCE, b'<a href="/news/a">Title</a>')
        self.assertEqual((record["basis"], record["content_hash"]), ("date_or_unknown", digest("Title")))


class PatternLinksTests(unittest.TestCase):
    SOURCE = make_source(kind="links", url="https://a.test/blog", link_pattern=r"^https://a\.test/blog/[^/?#]+$")

    def parse(self, html, **overrides):
        return {r["url"]: r["title"] for r in parse_source(dict(self.SOURCE, **overrides), html.encode())}

    def test_navigation_and_footer_are_skipped_by_default(self):
        html = ('<nav><a href="/blog/nav-story">Navigation story title</a></nav>'
                '<main><a href="/blog/real/?utm_source=x#c">Actual new AI story</a></main>'
                '<footer><a href="/blog/foot">Footer story title here</a></footer>')
        self.assertEqual(self.parse(html), {"https://a.test/blog/real": "Actual new AI story"})
        self.assertEqual(len(self.parse(html, include_navigation=True)), 3)

    def test_script_and_style_text_is_ignored(self):
        html = '<a href="/blog/x">Visible headline here<script>var hidden=1</script><style>.a{}</style></a>'
        self.assertEqual(self.parse(html), {"https://a.test/blog/x": "Visible headline here"})

    def test_title_length_generic_titles_and_aria_label(self):
        html = ('<a href="/blog/short">Too short</a><a href="/blog/more">Read more</a>'
                '<a href="/blog/aria" aria-label="Labelled story about models"></a>')
        self.assertEqual(self.parse(html), {"https://a.test/blog/aria": "Labelled story about models"})
        self.assertIn("https://a.test/blog/short", self.parse(html, min_title_length=5))

    def test_url_title_fallback(self):
        self.assertEqual(self.parse('<a href="/blog/x"></a>', url_title_fallback=True),
                         {"https://a.test/blog/x": "[제목 미제공] /blog/x"})

    def test_pattern_is_applied_to_the_canonical_absolute_url(self):
        html = '<a href="https://A.TEST/blog/Upper">Uppercase host story</a><a href="/blog/a/b">Nested path story</a>'
        self.assertEqual(self.parse(html), {"https://a.test/blog/Upper": "Uppercase host story"})

    def test_nested_nav_depth(self):
        html = '<nav><nav></nav><a href="/blog/in">Still inside nav here</a></nav><a href="/blog/out">Outside of navigation</a>'
        self.assertEqual(list(self.parse(html)), ["https://a.test/blog/out"])

    def test_valueless_href(self):
        self.assertEqual(self.parse("<a href>Some long anchor text</a>"), {})

    def test_fragment_links_match_the_article_pattern(self):
        # Fixed: the fragment used to be checked against an end-anchored pattern, so such links were missed.
        self.assertEqual(self.parse('<a href="/blog/story#comments">Story with comments link</a>'),
                         {"https://a.test/blog/story": "Story with comments link"})


class ChangelogTests(unittest.TestCase):
    SOURCE = make_source(kind="changelog", url="https://ai.test/changelog")

    def test_dated_sections(self):
        html = ('<h2>Overview</h2><p>intro</p>'
                '<h2 id="oct-19">October 19, 2026</h2><p>New model</p><script>noise()</script><p>and more</p>'
                '<h2>2026-09 release</h2><p>September</p>'
                '<h2>Related articles</h2><p>random</p>')
        first, second = parse_source(self.SOURCE, html.encode())
        self.assertEqual(first["key"], "oct-19")
        self.assertEqual(first["title"], "Gemini API · October 19, 2026")
        self.assertEqual(first["url"], "https://ai.test/changelog#oct-19")
        self.assertEqual(first["content_hash"], digest("New model and more"))
        self.assertEqual(first["basis"], "date_only")
        self.assertEqual(second["key"], digest("2026-09 release")[:12])
        self.assertEqual(second["content_hash"], digest("September"))

    def test_heading_needs_a_year_and_a_month(self):
        html = "<h2>Version 2026</h2><p>x</p><h2>October notes</h2><p>y</p>"
        self.assertEqual(parse_source(self.SOURCE, html.encode()), [])

    def test_only_the_newest_sections_are_kept(self):
        html = "".join("<h2>January %d, 2026</h2><p>n%d</p>" % (i, i) for i in range(1, 20))
        self.assertEqual(len(parse_source(self.SOURCE, html.encode())), CHANGELOG_SECTIONS)

    def test_heading_inside_script_is_ignored(self):
        html = "<script><h2>October 1, 2026</h2></script><h2>October 2, 2026</h2><p>real</p>"
        [record] = parse_source(self.SOURCE, html.encode())
        self.assertEqual(record["title"], "Gemini API · October 2, 2026")


class MalformedUrlTests(unittest.TestCase):
    """One unparseable URL (here an unterminated IPv6 host) is skipped; it no longer fails the whole page."""
    BAD = "http://[::1"

    def test_feed(self):
        data = rss(rss_item("Bad", self.BAD), rss_item("Good", "https://a.test/good"))
        self.assertEqual([r["title"] for r in parse_source(FEED, data)], ["Good"])

    def test_sitemap(self):
        source = make_source(kind="sitemap", link_pattern="/")
        xml = ("<urlset><url><loc>%s/x</loc></url><url><loc>https://a.test/ok</loc></url></urlset>" % self.BAD).encode()
        self.assertEqual([r["url"] for r in parse_source(source, xml)], ["https://a.test/ok"])

    def test_links(self):
        source = make_source(kind="links", url="https://a.test/blog", link_pattern=r"/blog/")
        html = ('<a href="%s/blog/x">A broken link with a long title</a><a href="/blog/y">A working link with a long title</a>'
                % self.BAD).encode()
        self.assertEqual([r["url"] for r in parse_source(source, html)], ["https://a.test/blog/y"])


class DispatchTests(unittest.TestCase):
    def test_every_kind_has_a_parser(self):
        self.assertEqual(set(SOURCE_KINDS), set(PARSERS))
        self.assertEqual(set(SOURCE_KINDS), {"feed", "sitemap", "hf_models", "anthropic", "links", "changelog"})

    def test_unknown_kind(self):
        self.assertRaisesRegex(ValueError, "Unknown source kind", parse_source, make_source(kind="podcast"), b"")

    def test_invalid_utf8_html_is_replaced_not_fatal(self):
        source = make_source(kind="anthropic", url="https://www.anthropic.com/news")
        [record] = parse_source(source, b'<a href="/news/x">Bad \xff byte</a>')
        self.assertEqual(record["title"], "Bad � byte")


if __name__ == "__main__":
    unittest.main()
