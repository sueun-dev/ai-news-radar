"""One parser per source ``kind``. Each takes the source config and the raw body and returns items.

Parsers extract only what a source is configured to publish (feed entries, matching article links,
dated changelog sections, model ids) and never arbitrary page text.
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from xml.parsers import expat
from html.parser import HTMLParser
from typing import Any, Callable, Dict, List, Mapping, Optional
from urllib.parse import urljoin, urlsplit

from .items import canonical_url, clean, digest, is_web_url, item

ATOM = "{http://www.w3.org/2005/Atom}"
FEED_ROOTS = ("rss", "feed", "RDF")

Item = Dict[str, Any]
Source = Mapping[str, Any]


def local_name(tag: str) -> str:
    """Drop an XML namespace: ``{ns}entry`` -> ``entry``."""
    return tag.split("}")[-1]


# Entities can only be declared inside a DOCTYPE; these byte patterns find one in UTF-8 and UTF-16 input.
_DOCTYPE_MARKERS = (b"<!DOCTYPE", b"<\x00!\x00", b"\x00<\x00!")


def _refuse_entity_declarations(data: bytes) -> None:
    """Pre-scan with expat (which decodes the document's real encoding) and stop at the first entity
    declaration, before any expansion could happen."""
    def refuse(*_):
        raise ValueError("XML entity declarations are not accepted")
    scanner = expat.ParserCreate()
    scanner.EntityDeclHandler = refuse
    try:
        scanner.Parse(data, True)
    except expat.ExpatError:
        pass  # malformed: ElementTree reports it with its usual ParseError


def xml_root(data: bytes) -> ET.Element:
    """Parse untrusted XML. Entity declarations are refused outright: real feeds never need them, and
    refusing them rules out entity-expansion bombs. ElementTree never fetches external entities or DTDs."""
    if any(marker in data for marker in _DOCTYPE_MARKERS):
        _refuse_entity_declarations(data)
    return ET.fromstring(data)


def is_feed_document(data: bytes) -> bool:
    """True when the body is a well-formed RSS, Atom or RDF document (possibly with no entries)."""
    return local_name(xml_root(data).tag) in FEED_ROOTS


def web_url(base: str, href: str) -> Optional[str]:
    """``href`` resolved against ``base`` and canonicalised, or None unless it is a parseable http(s) URL.

    One malformed link (say an unterminated IPv6 host) is skipped instead of failing the whole page.
    """
    try:
        url = urljoin(base, href)
        return canonical_url(url) if is_web_url(url) else None
    except ValueError:
        return None


# --- XML / JSON sources -------------------------------------------------------------------------

def _child_text(element: ET.Element, name: str) -> str:
    """All text of the first direct child named ``name`` (any namespace), or ''."""
    child = next((c for c in element if local_name(c.tag) == name), None)
    return "".join(child.itertext()) if child is not None else ""


def _atom_alternate_link(entry: ET.Element) -> str:
    links = [link.attrib.get("href", "") for link in entry.findall(ATOM + "link")
             if link.attrib.get("rel", "alternate") == "alternate"]
    return links[0] if links else ""


def parse_feed(source: Source, data: bytes) -> List[Item]:
    """RSS 2.0, RDF/RSS 1.0 and Atom. Entries are keyed by their canonical URL so GUID churn dedupes."""
    root = xml_root(data)
    base = source["url"]
    entries = [e for e in root.iter() if local_name(e.tag) in ("item", "entry")]
    out = []
    for entry in entries[:source.get("max_items", 2000)]:
        text = lambda name: _child_text(entry, name)  # noqa: E731 - bound to this entry
        if entry.tag.startswith(ATOM):
            url = _atom_alternate_link(entry)
            raw_time = text("published") or text("updated")
            basis = "published" if text("published") else "feed_updated"
        else:
            url, raw_time, basis = text("link"), text("pubDate") or text("date"), "published"
            if not url and text("guid").startswith(("https://", "http://")):
                url = text("guid")
        url = web_url(base, url)
        if url is None:
            continue
        body = text("content") or text("encoded") or text("description") or text("summary")
        out.append(item(url, text("title"), url, body, raw_time, source.get("time_basis", basis)))
    return out


def parse_sitemap(source: Source, data: bytes) -> List[Item]:
    """Sitemap ``<url>`` entries whose location matches ``link_pattern``. ``lastmod`` is not a publication time."""
    pattern = re.compile(source["link_pattern"])
    out = []
    for entry in xml_root(data):
        if local_name(entry.tag) != "url":
            continue
        values = {local_name(child.tag): child.text or "" for child in entry}
        try:
            url = canonical_url(values.get("loc", ""))  # trims the whitespace sitemaps often carry
        except ValueError:
            continue
        if is_web_url(url) and pattern.search(url):
            out.append(item(url, "[사이트맵 · 제목 미제공] " + urlsplit(url).path, url, url,
                            values.get("lastmod"), "sitemap_modified"))
    return out


_MODEL_ID = re.compile(r"[\w.-]+/[\w.-]+")


def parse_hf_models(source: Source, data: bytes) -> List[Item]:
    """Public Hugging Face model ids owned by ``author``. Popularity counters are never hashed."""
    rows = json.loads(data)
    if not isinstance(rows, list):
        raise ValueError("Expected public model list")
    author = source["author"].lower()
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        model_id = row.get("id", "")
        if not isinstance(model_id, str) or not _MODEL_ID.fullmatch(model_id) or row.get("private"):
            continue
        if model_id.split("/")[0].lower() != author:
            continue
        out.append(item(model_id, model_id, "https://huggingface.co/" + model_id, model_id,
                        row.get("createdAt"), "repository_created"))
    return out


# --- HTML sources --------------------------------------------------------------------------------

class _LongestAnchorText(HTMLParser):
    """Collect the text of accepted anchors, keeping the longest title seen for each URL."""

    def __init__(self) -> None:
        super().__init__()
        self.active: Optional[str] = None  # URL of the anchor being read
        self.parts: List[str] = []
        self.found: Dict[str, str] = {}

    def keep(self, url: str, title: str) -> None:
        if len(title) > len(self.found.get(url, "")):
            self.found[url] = title


class AnthropicNewsLinks(_LongestAnchorText):
    """Anthropic's news index: links to ``/news/<slug>``."""
    HREF = re.compile(r"^(?:https://www\.anthropic\.com)?/news/[^?#]+")

    def __init__(self, base: str) -> None:
        super().__init__()
        self.base = base

    def handle_starttag(self, tag, attrs):
        href = dict(attrs).get("href") or ""
        if tag == "a" and self.HREF.match(href):
            self.active, self.parts = urljoin(self.base, href).split("#")[0], []

    def handle_data(self, data):
        if self.active:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.active:
            title = clean(" ".join(self.parts))
            if title:
                self.keep(self.active, title)
            self.active, self.parts = None, []


class PatternLinks(_LongestAnchorText):
    """Article links matching the source's ``link_pattern``; page chrome (nav/footer) is skipped."""
    GENERIC_TITLES = ("read more", "learn more", "view details")

    def __init__(self, source: Source) -> None:
        super().__init__()
        self.source = source
        self.pattern = re.compile(source["link_pattern"])
        self.min_title = source.get("min_title_length", 12)
        self.skipped_tags = ("script", "style") if source.get("include_navigation") else (
            "script", "style", "nav", "footer")
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.skipped_tags:
            self.skip_depth += 1
        if tag != "a" or self.skip_depth:
            return
        attrs = dict(attrs)
        # The fragment is dropped before matching, so "/blog/post#comments" is still the article "/blog/post".
        url = web_url(self.source["url"], attrs.get("href") or "")
        url = url and canonical_url(url.split("#")[0])
        if url and self.pattern.search(url):
            self.active, self.parts = url, []
            if attrs.get("aria-label"):
                self.parts.append(attrs["aria-label"])

    def handle_data(self, data):
        if self.active and not self.skip_depth:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.active:
            title = clean(" ".join(self.parts))
            if self.source.get("url_title_fallback") and len(title) < self.min_title:
                title = "[제목 미제공] " + urlsplit(self.active).path
            if len(title) >= self.min_title and title.lower() not in self.GENERIC_TITLES:
                self.keep(self.active, title)
            self.active, self.parts = None, []
        if tag in self.skipped_tags and self.skip_depth:
            self.skip_depth -= 1


class DatedSections(HTMLParser):
    """Changelog pages: each ``<h2>`` that names a date starts a section; its text is the body."""
    YEAR = re.compile(r"\b20\d{2}\b")
    DATE_WORD = re.compile(r"January|February|March|April|May|June|July|August|September|October|November"
                           r"|December|\d{4}-\d{2}")

    def __init__(self) -> None:
        super().__init__()
        self.skip_depth = 0
        self.in_heading, self.heading_parts, self.heading_id = False, [], ""
        self.current = None  # (key, title) of the open dated section
        self.parts: List[str] = []
        self.found = []      # [((key, title), body)]

    def flush(self) -> None:
        if self.current:
            self.found.append((self.current, clean(" ".join(self.parts))))

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip_depth += 1
        if self.skip_depth or tag != "h2":
            return
        self.flush()
        self.current, self.parts = None, []
        self.in_heading, self.heading_parts = True, []
        self.heading_id = dict(attrs).get("id", "")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip_depth:
            self.skip_depth -= 1
            return
        if self.skip_depth or tag != "h2" or not self.in_heading:
            return
        title = clean(" ".join(self.heading_parts))
        if self.YEAR.search(title) and self.DATE_WORD.search(title):
            self.current = (self.heading_id or digest(title)[:12], title)
        self.in_heading = False

    def handle_data(self, data):
        if self.skip_depth:
            return
        if self.in_heading:
            self.heading_parts.append(data)
        elif self.current:
            self.parts.append(data)


def _html(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def parse_anthropic(source: Source, data: bytes) -> List[Item]:
    parser = AnthropicNewsLinks(source["url"])
    parser.feed(_html(data))
    return [item(url, title, url, title, basis="date_or_unknown") for url, title in parser.found.items()]


def parse_links(source: Source, data: bytes) -> List[Item]:
    parser = PatternLinks(source)
    parser.feed(_html(data))
    return [item(url, title, url, title, basis="date_or_unknown") for url, title in parser.found.items()]


CHANGELOG_TITLE_PREFIX = "Gemini API · "  # part of stored titles: changing it would re-announce every section
CHANGELOG_SECTIONS = 12


def parse_changelog(source: Source, data: bytes) -> List[Item]:
    parser = DatedSections()
    parser.feed(_html(data))
    parser.flush()
    base = source["url"]
    return [item(key, CHANGELOG_TITLE_PREFIX + title, base + "#" + key, body, basis="date_only")
            for (key, title), body in parser.found[:CHANGELOG_SECTIONS]]


PARSERS: Dict[str, Callable[[Source, bytes], List[Item]]] = {
    "feed": parse_feed,
    "sitemap": parse_sitemap,
    "hf_models": parse_hf_models,
    "anthropic": parse_anthropic,
    "links": parse_links,
    "changelog": parse_changelog,
}
SOURCE_KINDS = tuple(PARSERS)


def parse_source(source: Source, data: bytes) -> List[Item]:
    parser = PARSERS.get(source["kind"])
    if parser is None:
        raise ValueError("Unknown source kind")
    return parser(source, data)
