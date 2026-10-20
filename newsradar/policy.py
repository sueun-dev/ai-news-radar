"""When to poll a source next: its own interval, server hints (Cache-Control, Retry-After,
X-Poll-Interval) and exponential backoff after failures.

Server-provided delays are untrusted input. A malformed or absurd value (``Retry-After: inf``,
``X-Poll-Interval: soon``) is ignored or capped so one bad response can never stall a source forever.
"""
from __future__ import annotations

import math
import re
import time
from typing import Any, Mapping, Optional

from .timeutil import parse_time

MAX_SERVER_DELAY = 86400.0       # cap for any delay a server asks for
TRANSPORT_RETRY_BASE = 15        # first retry after a network/transport failure (seconds)
TRANSPORT_RETRY_CAP = 900
HTTP_BACKOFF_CAP = 3600          # cap for exponential backoff after HTTP/parse errors
HOST_THROTTLE_MIN = 60           # minimum pause for a whole host after 429/503+Retry-After

_MAX_AGE = re.compile(r"(?:^|,)\s*max-age\s*=\s*\"?(\d+)")

Headers = Mapping[str, str]


def _finite(raw: Any) -> Optional[float]:
    """``raw`` as a finite float, or None when it is not a number."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _seconds(raw: Any) -> Optional[float]:
    """A finite, non-negative number of seconds (capped), or None when ``raw`` is not one."""
    value = _finite(raw)
    return None if value is None else min(max(0.0, value), MAX_SERVER_DELAY)


def header_seconds(headers: Headers, name: str) -> float:
    return _seconds(headers.get(name) or 0) or 0.0


def retry_after(headers: Headers, now: float) -> float:
    """Seconds requested by ``Retry-After`` (delta-seconds or HTTP-date); 0 when absent or invalid."""
    raw = headers.get("retry-after", "")
    seconds = _seconds(raw)
    if seconds is not None:
        return seconds
    when, _ = parse_time(raw)
    return min(max(0.0, when - now), MAX_SERVER_DELAY) if when else 0.0


def cache_delay(headers: Headers) -> float:
    """Remaining freshness lifetime: ``max-age`` minus ``Age``, else ``Expires`` minus ``Date``."""
    cache_control = headers.get("cache-control", "").lower()
    if "no-store" in cache_control or "no-cache" in cache_control:
        return 0.0
    match = _MAX_AGE.search(cache_control)
    if match:
        digits = match.group(1)
        if len(digits) > 12:  # ~31,000 years: past the cap whatever Age says, and too big to convert safely
            return MAX_SERVER_DELAY
        age = max(0.0, _finite(headers.get("age") or 0) or 0.0)  # junk Age counts as 0, real ones are not capped
        return min(max(0, int(digits) - age), MAX_SERVER_DELAY)
    # HTTP Expires is considered only when no Cache-Control max-age exists.
    expires, _ = parse_time(headers.get("expires"))
    date, _ = parse_time(headers.get("date"))
    return min(max(0, expires - (date or time.time())), MAX_SERVER_DELAY) if expires else 0.0


def next_poll_interval(source: Mapping[str, Any], *, ok: bool, status: Optional[int], failures: int,
                       error_kind: Optional[str], headers: Headers, now: float) -> float:
    """Seconds until the next poll of ``source`` after a check with this outcome.

    - Success: the configured interval, stretched by cache freshness (only for ``respect_cache_ttl``
      sources; freshness never stops conditional revalidation otherwise) and by ``X-Poll-Interval``.
    - Transport failure (no HTTP status): prompt exponential retry 15s, 30s, ... capped at 15 minutes.
    - HTTP or parse failure: exponential backoff on the configured interval, capped at an hour,
      and never sooner than ``Retry-After``.
    """
    cache_ttl = cache_delay(headers) if source.get("respect_cache_ttl", False) else 0
    poll_hint = header_seconds(headers, "x-poll-interval")
    interval = max(source["interval"], cache_ttl, poll_hint)
    if ok:
        return interval
    if error_kind == "transport" or status is None:
        backoff = min(TRANSPORT_RETRY_CAP, TRANSPORT_RETRY_BASE * 2 ** min(failures - 1, 6))
        return max(backoff, retry_after(headers, now), poll_hint)
    backoff = min(HTTP_BACKOFF_CAP, source["interval"] * 2 ** min(failures, 7))
    return max(interval, backoff, retry_after(headers, now))


def host_throttle(status: Optional[int], headers: Headers, now: float) -> Optional[float]:
    """Pause for the whole host after 429, or 503 with Retry-After; None when no pause applies."""
    if status == 429 or (status == 503 and headers.get("retry-after")):
        return max(HOST_THROTTLE_MIN, retry_after(headers, now))
    return None
