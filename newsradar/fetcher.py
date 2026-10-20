"""Bounded, conditional HTTP fetches of one source.

Every fetch is a revalidation (ETag / Last-Modified), reads at most 8 MB within a time budget,
accepts only identity or gzip bodies, and reports failures in the result instead of raising.
"""
from __future__ import annotations

import gzip
import io
import ssl
import time
from http.client import HTTPException, IncompleteRead
from typing import Any, Callable, Dict, Mapping, Optional
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .parsers import is_feed_document, parse_source

MAX_BODY_BYTES = 8_000_000
CHUNK_BYTES = 65536
DEFAULT_TIMEOUT = 15
DEFAULT_READ_BUDGET = 30
USER_AGENT = "AI-News-Radar/1.0 (personal conditional public-feed monitor)"
ACCEPT = "application/atom+xml,application/rss+xml,application/xml,text/html;q=0.9,*/*;q=0.1"
TLS_CONTEXT = ssl.create_default_context()

Result = Dict[str, Any]


def read_response(response, deadline: float, limit: int = MAX_BODY_BYTES) -> bytes:
    """Read bounded chunks so a trickling or oversized body cannot occupy a worker indefinitely."""
    parts, size = [], 0
    while True:
        if time.monotonic() > deadline:
            raise TimeoutError("Response read budget exceeded")
        chunk = response.read1(min(CHUNK_BYTES, limit + 1 - size))
        if time.monotonic() > deadline:
            raise TimeoutError("Response read budget exceeded")
        if not chunk:
            break
        parts.append(chunk)
        size += len(chunk)
        if size > limit:
            raise ValueError("Response exceeds 8 MB limit")
    # read1 does not always raise on a prematurely terminated Content-Length body.
    length = response.headers.get("Content-Length")
    if length and size != int(length):
        raise IncompleteRead(b"", max(0, int(length) - size))
    return b"".join(parts)


def decode_body(data: bytes, content_encoding: str, limit: int = MAX_BODY_BYTES) -> bytes:
    """Undo gzip transfer compression, refusing bodies that expand beyond ``limit``."""
    encoding = (content_encoding or "").lower()
    if encoding == "gzip":
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as compressed:
            data = compressed.read(limit + 1)
        if len(data) > limit:
            raise ValueError("Decompressed response exceeds 8 MB limit")
        return data
    if encoding not in ("", "identity"):
        raise ValueError("Unsupported content encoding")
    return data


def request_headers(source: Mapping[str, Any], previous: Mapping[str, Any]) -> Dict[str, str]:
    headers = {"User-Agent": USER_AGENT, "Accept": ACCEPT, "Accept-Encoding": "gzip"}
    if previous.get("etag"):
        headers["If-None-Match"] = previous["etag"]
    if previous.get("last_modified"):
        headers["If-Modified-Since"] = previous["last_modified"]
    if not source.get("respect_cache_ttl", False):
        headers["Cache-Control"] = "no-cache"  # ask intermediaries to revalidate, not to serve stale copies
    return headers


def _lower_keys(headers) -> Dict[str, str]:
    return {key.lower(): value for key, value in headers.items()}


def fetch_source(source: Mapping[str, Any], previous: Mapping[str, Any],
                 opener: Callable[..., Any] = urlopen) -> Result:
    """Fetch and parse ``source``. ``previous`` is its stored row (validators and schedule).

    The result always has the same keys; ``error`` is set instead of raising. ``error_kind`` is
    ``"transport"`` for network-level failures (retried promptly) and ``"parse"`` for bad content.
    """
    started, tick = time.time(), time.monotonic()
    result: Result = dict(status=None, headers={}, entries=[], parsed_count=0, valid_empty_feed=False,
                          error=None, error_kind=None, transferred_bytes=None, decoded_bytes=None)
    try:
        request = Request(source["url"], headers=request_headers(source, previous))
        with opener(request, timeout=source.get("timeout", DEFAULT_TIMEOUT), context=TLS_CONTEXT) as response:
            result["status"] = response.status
            result["headers"] = headers = _lower_keys(response.headers)
            data = read_response(response, tick + source.get("read_budget_seconds", DEFAULT_READ_BUDGET))
            result["transferred_bytes"] = len(data)
            data = decode_body(data, headers.get("content-encoding", ""))
            result["decoded_bytes"] = len(data)
            # Relative links resolve against the final URL after redirects.
            entries = parse_source(dict(source, url=response.url), data)
            result["parsed_count"] = len(entries)
            if not entries and source.get("allow_empty") and source["kind"] == "feed":
                result["valid_empty_feed"] = is_feed_document(data)
            if source.get("filter_ai"):
                # Each entry was matched on its own title/body, never on the whole feed.
                entries = [entry for entry in entries if entry["ai_match"]]
            result["entries"] = entries
    except HTTPError as exc:
        result["status"] = exc.code
        result["headers"] = _lower_keys(exc.headers or {})
        if exc.code == 304:
            result["transferred_bytes"] = result["decoded_bytes"] = 0
        else:
            result["error"] = "HTTP %s" % exc.code
    except Exception as exc:  # any failure becomes a recorded error, never a crashed worker
        result["error"] = type(exc).__name__ + ": " + str(exc)[:240]
        result["error_kind"] = "transport" if isinstance(exc, (OSError, HTTPException)) else "parse"
    result.update(started=started, finished=time.time(), fetch_ms=round((time.monotonic() - tick) * 1000, 1),
                  queue_delay_seconds=max(0, started - previous["next_due"]) if previous.get("next_due") else None)
    return result
