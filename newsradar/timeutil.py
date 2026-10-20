"""Timestamps: ISO formatting and parsing of the times sources publish."""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional, Tuple

UTC = timezone.utc
_DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}")

# Precision labels stored with every item and event.
INSTANT = "instant"                    # an exact moment with a timezone
DATE = "date"                          # a calendar date without a time
TIMEZONE_UNKNOWN = "timezone_unknown"  # a time without an offset: the moment is ambiguous
UNKNOWN = "unknown"                    # missing or unparseable


def iso(ts: Optional[float] = None) -> str:
    """UTC ISO-8601 with milliseconds; ``ts`` defaults to now."""
    return datetime.fromtimestamp(time.time() if ts is None else ts, UTC).isoformat(timespec="milliseconds")


def parse_time(value) -> Tuple[Optional[float], str]:
    """Parse an ISO-8601 or RFC 2822 source timestamp into ``(epoch seconds, precision)``.

    Only an ``instant`` carries epoch seconds; dates, naive times and garbage return ``None``
    so they can never be mistaken for a measurable publication time.
    """
    if not value or not isinstance(value, str):
        return None, UNKNOWN
    value = value.strip()
    if _DATE_ONLY.fullmatch(value):
        return None, DATE
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            moment = parsedate_to_datetime(value)
        except (ValueError, TypeError, OverflowError):
            return None, UNKNOWN
    if moment.tzinfo is None:
        return None, TIMEZONE_UNKNOWN
    try:
        return moment.timestamp(), INSTANT
    except (OverflowError, ValueError, OSError):  # e.g. year 1 with a positive offset
        return None, UNKNOWN
