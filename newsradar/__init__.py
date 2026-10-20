"""AI News Radar: a local monitor for public AI news sources (Python stdlib only).

Modules, in dependency order:

- ``timeutil``  timestamps and source time parsing
- ``items``     text/URL normalisation and the collected-item record
- ``parsers``   one parser per source ``kind`` (feeds, HTML indexes, sitemaps, model lists)
- ``policy``    how long to wait before the next poll (cache, Retry-After, backoff)
- ``fetcher``   bounded conditional HTTP fetches
- ``store``     SQLite state: sources, items, events, checks, preferences
- ``scheduler`` which due sources to fetch next
- ``web``       the loopback-only dashboard and JSON API
- ``app``       the long-running service and the command-line entry point
"""
from pathlib import Path

# The repository root holds config.json, coverage.json, static/ and the runtime data/ directory.
ROOT = Path(__file__).resolve().parent.parent
