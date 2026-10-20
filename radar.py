#!/usr/bin/env python3
"""AI News Radar collector entry point (the LaunchAgent runs this file).

The implementation lives in the ``newsradar`` package next to this file; the names below are
re-exported so existing scripts that ``import radar`` keep working.
"""
from newsradar import ROOT
from newsradar.app import Radar, main
from newsradar.fetcher import fetch_source, read_response
from newsradar.items import AI_TERMS, canonical_url, clean, digest, item
from newsradar.parsers import parse_source
from newsradar.policy import cache_delay, retry_after
from newsradar.store import Store
from newsradar.timeutil import iso, parse_time

__all__ = ["ROOT", "Radar", "main", "fetch_source", "read_response", "AI_TERMS", "canonical_url", "clean",
           "digest", "item", "parse_source", "cache_delay", "retry_after", "Store", "iso", "parse_time"]

if __name__ == "__main__":
    main()
