"""SQLite state: configured sources, collected items, detection events, HTTP checks and preferences.

One connection is shared by the poller and the web threads and every access holds ``Store.lock``.
Schema changes are additive only, so an older database always opens with its history intact.
"""
from __future__ import annotations

import collections
import csv
import io
import json
import logging
import os
import re
import sqlite3
import statistics
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from .items import canonical_url
from .policy import next_poll_interval
from .timeutil import iso, parse_time

LOG = logging.getLogger("radar")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources(
  id TEXT PRIMARY KEY,name TEXT,url TEXT,kind TEXT,interval REAL,
  initialized_at REAL,last_checked REAL,last_success REAL,next_due REAL DEFAULT 0,
  status TEXT DEFAULT 'starting',http_status INTEGER,error TEXT,failures INTEGER DEFAULT 0,
  etag TEXT,last_modified TEXT,cache_control TEXT,cache_age TEXT,fetch_ms REAL,item_count INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS items(
  source_id TEXT,key TEXT,title TEXT,url TEXT,content_hash TEXT,first_seen REAL,last_seen REAL,
  source_time REAL,source_time_raw TEXT,precision TEXT,basis TEXT,PRIMARY KEY(source_id,key));
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY,source_id TEXT,title TEXT,url TEXT,kind TEXT,
  detected_at REAL,source_time REAL,source_time_raw TEXT,precision TEXT,basis TEXT,
  delay_seconds REAL,observation_gap_seconds REAL,measurement_note TEXT,
  is_test INTEGER DEFAULT 0,notify_state TEXT DEFAULT 'pending',notify_enqueued_at REAL,
  notify_error TEXT,opened_at REAL);
CREATE TABLE IF NOT EXISTS checks(
  id INTEGER PRIMARY KEY,source_id TEXT,started_at REAL,finished_at REAL,status INTEGER,
  fetch_ms REAL,item_count INTEGER,error TEXT,cache_control TEXT,cache_age TEXT,next_due REAL);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
CREATE TABLE IF NOT EXISTS source_preferences(source_id TEXT PRIMARY KEY,notify INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS events_url_time ON events(url,detected_at);
CREATE INDEX IF NOT EXISTS checks_finished ON checks(finished_at);
CREATE INDEX IF NOT EXISTS items_time ON items(source_time,first_seen);
"""
# Transfer telemetry added after the first release; older databases gain the columns on open.
CHECK_TELEMETRY_COLUMNS = (("queue_delay_seconds", "REAL"), ("transferred_bytes", "INTEGER"),
                           ("decoded_bytes", "INTEGER"), ("content_encoding", "TEXT"))

# Clocks that are real but are not the original publication time (search index, community post,
# paper batch, sitemap edit, repository creation); they never count as publication latency.
NON_ORIGINAL_BASES = frozenset({"index_published", "community_posted", "batch_announcement",
                                "sitemap_modified", "repository_created"})
DATE_ONLY_BASES = ("date_only", "date_or_unknown")
PRERELEASE = re.compile(r"alpha|beta|rc[.\d]|dev[.\d]", re.I)
DUPLICATE_WINDOW = 86400       # the same URL announced again within a day is a duplicate
BACKFILL_GRACE = 300           # items timestamped this long before the baseline are history, not news
CHECK_RETENTION = 30 * 86400   # raw polling log retention; items and events are kept forever
RECENT_EVENTS = 300
PENDING_BATCH = 10
LONG_OBSERVATION = 300
ZERO_ENTRIES_ERROR = "Parsed zero entries; baseline/state was preserved."
INTERNAL_ERROR = "Internal processing error; see service log"
CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
EXPORT_TABLES = {"events": "id DESC", "checks": "id DESC", "items": "first_seen DESC"}
PLACEHOLDER_TITLE_SQL = "CASE WHEN title LIKE '[제목 미제공]%' OR title LIKE '[사이트맵%' THEN 1 ELSE 0 END"
X_HOSTS = ("x.com", "www.x.com", "twitter.com")


# --- event classification (pure) --------------------------------------------------------------

def measure_delay(entry: Mapping[str, Any], old: Optional[Mapping[str, Any]], changed: bool,
                  now: float, initialized_at: float) -> Tuple[Optional[float], str]:
    """``(publication delay or None, measurement note)`` for a newly detected or changed entry.

    A delay is claimed only for a precise, plausible timestamp of the original publication.
    """
    source_time = entry["source_time"]
    if changed and (old["source_time"] == source_time or entry["basis"] == "published"):
        return None, "edit_without_new_precise_timestamp"
    if entry.get("basis") in NON_ORIGINAL_BASES:
        return None, "not_original_publication_time"
    if entry["precision"] == "instant" and source_time is not None:
        if source_time > now:
            return None, "source_clock_in_future"
        if source_time < initialized_at:
            return None, "older_than_initial_baseline"
        return now - source_time, entry["basis"]
    if entry["basis"] in DATE_ONLY_BASES:
        return None, "date_only_or_unavailable"
    return None, "source_time_unknown"


def is_prerelease(source: Mapping[str, Any], title: str) -> bool:
    return (source["id"] == "codex" or source.get("group") == "releases") and bool(PRERELEASE.search(title))


def notify_state(*, backfill: bool, duplicate: bool, enabled: bool, prerelease: bool,
                 notify_prerelease: bool) -> str:
    """Whether an event should raise a desktop notification, and if not, why."""
    if backfill:
        return "historical_silent"
    if duplicate:
        return "duplicate"
    if not enabled:
        return "source_silent"
    if prerelease and not notify_prerelease:
        return "prerelease_silent"
    return "pending"


def percentile95(sorted_values: Sequence[float]) -> Optional[float]:
    """Nearest-rank 95th percentile (rank rounded up) of already sorted values."""
    if not sorted_values:
        return None
    return sorted_values[max(0, int(len(sorted_values) * .95 + .999) - 1)]


def delay_summary(values: Sequence[float]) -> Dict[str, Any]:
    ordered = sorted(values)
    return {"count": len(ordered), "median": statistics.median(ordered) if ordered else None,
            "p95": percentile95(ordered)}


def _csv_safe(value: Any) -> Any:
    """Keep spreadsheet applications from interpreting untrusted text as a formula."""
    return "'" + value if isinstance(value, str) and value.startswith(CSV_FORMULA_PREFIXES) else value


class Store:
    def __init__(self, path, config: Mapping[str, Any], x_setup_path=None) -> None:
        self.config = config
        self.lock = threading.RLock()
        self.x_setup_path = Path(x_setup_path) if x_setup_path else Path(path).parent / "x-setup.json"
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self._add_missing_check_columns()
        with self.lock, self.db:
            self._sync_configured_sources()
            self._canonicalize_urls_once()
            self.set_meta("service_started", iso())
            self.set_meta("pid", os.getpid())

    # --- migrations ---------------------------------------------------------------------------

    def _add_missing_check_columns(self) -> None:
        existing = {row["name"] for row in self.db.execute("PRAGMA table_info(checks)")}
        for name, kind in CHECK_TELEMETRY_COLUMNS:
            if name not in existing:
                self.db.execute("ALTER TABLE checks ADD COLUMN " + name + " " + kind)

    def _sync_configured_sources(self) -> None:
        """Upsert configured sources. A changed interval keeps the existing wait: the next poll is at
        least ``last_success + new interval`` so shortening an interval never triggers a burst."""
        for source in self.config["sources"]:
            old = self.db.execute("SELECT interval,last_success,status FROM sources WHERE id=?",
                                  (source["id"],)).fetchone()
            self.db.execute("""INSERT INTO sources(id,name,url,kind,interval) VALUES(?,?,?,?,?)
              ON CONFLICT(id) DO UPDATE SET name=excluded.name,url=excluded.url,
              kind=excluded.kind,interval=excluded.interval""",
                            (source["id"], source["name"], source["url"], source["kind"], source["interval"]))
            if old and old["interval"] != source["interval"] and old["status"] == "ok" and old["last_success"]:
                self.db.execute("UPDATE sources SET next_due=MAX(next_due,?) WHERE id=?",
                                (old["last_success"] + source["interval"], source["id"]))

    def _canonicalize_urls_once(self) -> None:
        """One-time migration to canonical URLs (``canonical_v2``); merges rows that become duplicates."""
        if self.db.execute("SELECT 1 FROM meta WHERE key='canonical_v2'").fetchone():
            return
        for old in list(self.db.execute("SELECT source_id,key,url FROM items")):
            key = canonical_url(old["key"]) if old["key"].startswith(("http://", "https://")) else old["key"]
            if key != old["key"] and self.db.execute("SELECT 1 FROM items WHERE source_id=? AND key=?",
                                                     (old["source_id"], key)).fetchone():
                self.db.execute("DELETE FROM items WHERE source_id=? AND key=?", (old["source_id"], old["key"]))
                continue
            self.db.execute("UPDATE items SET key=?,url=? WHERE source_id=? AND key=?",
                            (key, canonical_url(old["url"]), old["source_id"], old["key"]))
        for event in list(self.db.execute("SELECT id,url FROM events")):
            self.db.execute("UPDATE events SET url=? WHERE id=?", (canonical_url(event["url"]), event["id"]))
        self.set_meta("canonical_v2", True)

    # --- basic access ---------------------------------------------------------------------------

    def set_meta(self, key: str, value: Any) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, json.dumps(value, ensure_ascii=False)))

    def source(self, sid: str) -> Dict[str, Any]:
        with self.lock:
            row = self.db.execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()
        if row is None:
            raise KeyError("Unknown source: %s" % sid)
        return dict(row)

    def source_rows(self) -> Dict[str, Dict[str, Any]]:
        """Every stored source row by id, in one query (the scheduler reads this twice a second)."""
        with self.lock:
            return {row["id"]: dict(row) for row in self.db.execute("SELECT * FROM sources")}

    def _notifications_enabled(self, source: Mapping[str, Any]) -> bool:
        pref = self.db.execute("SELECT notify FROM source_preferences WHERE source_id=?", (source["id"],)).fetchone()
        return bool(pref[0]) if pref is not None else source.get("notify", True)

    # --- recording a check --------------------------------------------------------------------

    def apply(self, source: Mapping[str, Any], result: Mapping[str, Any]) -> List[int]:
        """Record one fetch result for ``source``; returns the ids of events it created.

        A failed or empty response never replaces the last good validators, item count or baseline.
        """
        now, status = result["finished"], result["status"]
        with self.lock, self.db:
            prev = self.source(source["id"])
            headers = result["headers"]
            entries = result.get("entries", [])
            error = result.get("error")
            filtered_to_zero = source.get("filter_ai") and result.get("parsed_count", 0) > 0
            if status == 200 and not entries and not filtered_to_zero and not result.get("valid_empty_feed"):
                error = error or ZERO_ENTRIES_ERROR
            ok = not error and (status == 200 or (status == 304 and prev["initialized_at"] is not None))
            failures = 0 if ok else prev["failures"] + 1
            interval = next_poll_interval(source, ok=ok, status=status, failures=failures,
                                          error_kind=result.get("error_kind"), headers=headers, now=now)
            next_due = now + interval
            created = self.record_entries(source, entries, now, prev) if ok and status == 200 else []
            self.db.execute("""UPDATE sources SET initialized_at=?,last_checked=?,last_success=?,
              next_due=?,status=?,http_status=?,error=?,failures=?,etag=?,last_modified=?,
              cache_control=?,cache_age=?,fetch_ms=?,item_count=? WHERE id=?""",
                            (prev["initialized_at"] or (now if ok else None), now, now if ok else prev["last_success"],
                             next_due, "ok" if ok else "error", status, error, failures,
                             headers.get("etag", prev["etag"]) if ok else prev["etag"],
                             headers.get("last-modified", prev["last_modified"]) if ok else prev["last_modified"],
                             headers.get("cache-control", prev["cache_control"]), headers.get("age"),
                             result["fetch_ms"], len(entries) if status == 200 and ok else prev["item_count"],
                             source["id"]))
            self.db.execute("""INSERT INTO checks(source_id,started_at,finished_at,status,fetch_ms,item_count,
              error,cache_control,cache_age,next_due,queue_delay_seconds,transferred_bytes,decoded_bytes,
              content_encoding) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (source["id"], result["started"], now, status, result["fetch_ms"], len(entries),
                             error, headers.get("cache-control"), headers.get("age"), next_due,
                             result.get("queue_delay_seconds"), result.get("transferred_bytes"),
                             result.get("decoded_bytes"), headers.get("content-encoding")))
            self.db.execute("DELETE FROM checks WHERE finished_at<?", (now - CHECK_RETENTION,))
            LOG.info("%s status=%s items=%s new_events=%s next=%.1fs error=%s",
                     source["id"], status, len(entries), len(created), interval, error)
            return created

    def record_entries(self, source: Mapping[str, Any], entries: Sequence[Mapping[str, Any]], now: float,
                       previous: Mapping[str, Any]) -> List[int]:
        """Upsert items; after the first (baseline) collection, new or changed items become events."""
        baseline = previous["initialized_at"] is None
        detect_updates = source.get("detect_updates", True)
        enabled = None  # looked up once, on the first event
        created = []
        for entry in entries:
            old = self.db.execute("SELECT * FROM items WHERE source_id=? AND key=?",
                                  (source["id"], entry["key"])).fetchone()
            changed = old is not None and (old["content_hash"] != entry["content_hash"] or old["title"] != entry["title"])
            if not baseline and (old is None or (changed and detect_updates)):
                if enabled is None:
                    enabled = self._notifications_enabled(source)
                created.append(self._insert_event(source, entry, old, changed, now, previous, enabled))
            self.db.execute("""INSERT INTO items(source_id,key,title,url,content_hash,first_seen,last_seen,
              source_time,source_time_raw,precision,basis) VALUES(?,?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(source_id,key) DO UPDATE SET title=excluded.title,url=excluded.url,
              content_hash=excluded.content_hash,last_seen=excluded.last_seen,
              source_time=excluded.source_time,source_time_raw=excluded.source_time_raw,
              precision=excluded.precision,basis=excluded.basis""",
                            (source["id"], entry["key"], entry["title"], entry["url"], entry["content_hash"], now, now,
                             entry["source_time"], entry["source_time_raw"], entry["precision"], entry["basis"]))
        return created

    def _insert_event(self, source, entry, old, changed: bool, now: float, previous, enabled: bool) -> int:
        delay, note = measure_delay(entry, old, changed, now, previous["initialized_at"])
        source_time = entry["source_time"]
        gap = now - previous["last_success"] if previous["last_success"] else None
        duplicate = old is None and self.db.execute(
            "SELECT id FROM events WHERE url=? AND is_test=0 AND detected_at>? LIMIT 1",
            (entry["url"], now - DUPLICATE_WINDOW)).fetchone() is not None
        backfill = old is None and source_time is not None and source_time < previous["initialized_at"] - BACKFILL_GRACE
        notify = notify_state(backfill=backfill, duplicate=duplicate, enabled=enabled,
                              prerelease=is_prerelease(source, entry["title"]),
                              notify_prerelease=bool(self.config.get("notify_prerelease")))
        kind = "backfill" if backfill else ("updated" if changed else "new")
        cursor = self.db.execute("""INSERT INTO events(
          source_id,title,url,kind,detected_at,source_time,source_time_raw,precision,basis,
          delay_seconds,observation_gap_seconds,measurement_note,notify_state)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                 (source["id"], entry["title"], entry["url"], kind, now, source_time,
                                  entry["source_time_raw"], entry["precision"], entry["basis"], delay, gap, note, notify))
        return cursor.lastrowid

    def mark_internal_error(self, sid: str, retry_at: float) -> None:
        with self.lock, self.db:
            self.db.execute("UPDATE sources SET next_due=?,status='error',error=? WHERE id=?",
                            (retry_at, INTERNAL_ERROR, sid))

    # --- notification bridge --------------------------------------------------------------------

    def test_event(self) -> int:
        with self.lock, self.db:
            cursor = self.db.execute("""INSERT INTO events(
              source_id,title,url,kind,detected_at,source_time,precision,basis,
              measurement_note,is_test,notify_state) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                                     ("self-test", "자체 시험 알림 — 실제 AI 소식이 아닙니다",
                                      "http://127.0.0.1:%s/" % self.config["port"], "test", time.time(), None,
                                      "unknown", "synthetic", "synthetic_excluded", 1, "pending"))
            return cursor.lastrowid

    def pending(self) -> List[Dict[str, Any]]:
        with self.lock:
            return [dict(row) for row in self.db.execute(
                "SELECT * FROM events WHERE notify_state='pending' ORDER BY id LIMIT ?", (PENDING_BATCH,))]

    def notification_result(self, eid: int, state: str, error: Any = None) -> None:
        if state not in ("accepted", "denied", "error"):
            raise ValueError("invalid notification state")
        error = None if error is None else str(error)[:1000]
        with self.lock, self.db:
            self.db.execute("""UPDATE events SET notify_state=?,notify_enqueued_at=?,
              notify_error=? WHERE id=? AND notify_state='pending'""", (state, time.time(), error, eid))

    def mark_opened(self, eid: int) -> None:
        with self.lock, self.db:
            self.db.execute("UPDATE events SET opened_at=? WHERE id=?", (time.time(), eid))

    def record_bridge(self, info: Mapping[str, Any]) -> None:
        with self.lock, self.db:
            self.set_meta("notification_bridge", dict(info, heartbeat=iso()))

    # --- manual X record --------------------------------------------------------------------------

    def record_x(self, url: str, raw_time: str) -> int:
        """Record when the owner saw an official X post. Never counted as automatic latency."""
        parsed = urlsplit(url)
        allowed = {account.lower() for account in self.config["x_accounts"]}
        parts = parsed.path.strip("/").split("/")
        valid_path = (len(parts) == 3 and parts[0].lower() in allowed and parts[1] == "status"
                      and parts[2].isascii() and parts[2].isdigit())
        if parsed.scheme != "https" or parsed.hostname not in X_HOSTS or not valid_path:
            raise ValueError("설정된 공식 계정의 https://x.com/계정/status/숫자 URL만 입력하세요.")
        now = time.time()
        published, precision = parse_time(raw_time)
        if raw_time and (precision != "instant" or published > now):
            raise ValueError("공개 시각은 시간대가 포함된 과거 ISO 시각이어야 합니다.")
        with self.lock, self.db:
            cursor = self.db.execute("""INSERT INTO events(source_id,title,url,kind,detected_at,source_time,
              source_time_raw,precision,basis,delay_seconds,measurement_note,notify_state)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                                     ("x-manual", "X 수동 도착 기록 · @" + parts[0], url, "manual", now, published,
                                      raw_time, precision, "manual_input", now - published if published else None,
                                      "manual_record_time_not_push_receipt", "manual"))
            return cursor.lastrowid

    # --- dashboard queries ----------------------------------------------------------------------

    def _configured(self) -> Dict[str, Mapping[str, Any]]:
        return {source["id"]: source for source in self.config["sources"]}

    def _x_setup(self) -> Dict[str, Any]:
        try:
            return json.loads(self.x_setup_path.read_text())
        except (OSError, ValueError):
            return {"status": "not_verified", "accounts": []}

    def state(self) -> Dict[str, Any]:
        """Everything the dashboard polls: sources, recent events, latency metrics, metadata."""
        now = time.time()
        configured = self._configured()
        with self.lock:
            preferences = {row["source_id"]: bool(row["notify"])
                           for row in self.db.execute("SELECT * FROM source_preferences")}
            sources = []
            for row in self.db.execute("SELECT * FROM sources ORDER BY name"):
                config = configured.get(row["id"])
                if config is None:  # removed from config.json; its history stays in the database
                    continue
                sources.append(dict(row, group=config.get("group", "official"), region=config.get("region", "global"),
                                    notify=preferences.get(row["id"], config.get("notify", True)),
                                    filter_ai=config.get("filter_ai", False)))
            events = [dict(row) for row in self.db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?",
                                                           (RECENT_EVENTS,))]
            precise = [dict(row) for row in self.db.execute("""SELECT delay_seconds,basis,observation_gap_seconds
              FROM events WHERE is_test=0 AND source_id!='x-manual' AND delay_seconds IS NOT NULL""")]
            meta = {row["key"]: json.loads(row["value"]) for row in self.db.execute("SELECT * FROM meta")}
            item_count = self.db.execute("SELECT count(*) FROM items").fetchone()[0]
        delays = [x["delay_seconds"] for x in precise]
        return dict(
            now=now, sources=sources, events=events, meta=meta, x_setup=self._x_setup(),
            coverage=dict(groups=dict(collections.Counter(s["group"] for s in sources)), item_count=item_count,
                          active_sources=len(sources),
                          scope="Configured public sources only; completeness is not guaranteed"),
            x_accounts=self.config["x_accounts"],
            metrics={
                "by_basis": {basis: delay_summary([x["delay_seconds"] for x in precise if x["basis"] == basis])
                             for basis in ("published", "feed_updated")},
                "precise_samples": len(precise),
                "published_samples": sum(x["basis"] == "published" for x in precise),
                "updated_samples": sum(x["basis"] == "feed_updated" for x in precise),
                "long_observation_samples": sum((x["observation_gap_seconds"] or 0) > LONG_OBSERVATION for x in precise),
                "median_seconds": statistics.median(delays) if delays else None,
                "p95_seconds": percentile95(sorted(delays)),
            })

    def export(self, table: str) -> bytes:
        """A table as UTF-8 CSV with BOM (newest first)."""
        if table not in EXPORT_TABLES:
            raise ValueError("Invalid export")
        out = io.StringIO()
        writer = csv.writer(out)
        with self.lock:
            rows = self.db.execute("SELECT * FROM " + table + " ORDER BY " + EXPORT_TABLES[table])
            writer.writerow([column[0] for column in rows.description])
            for row in rows:
                writer.writerow([_csv_safe(value) for value in row])
        return ("﻿" + out.getvalue()).encode("utf-8")

    def set_notification(self, sid: str, enabled: bool) -> None:
        if sid not in self._configured() or not isinstance(enabled, bool):
            raise ValueError("Unknown source or invalid notification value")
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO source_preferences VALUES(?,?)", (sid, int(enabled)))

    def library(self, query: str = "", group: str = "", offset: int = 0, limit: int = 50) -> Dict[str, Any]:
        """Collected items, one row per URL (a real title beats a placeholder), newest first.

        ``query`` matches titles, source ids (SQL LIKE, wildcards escaped) and source names.
        """
        configured = self._configured()
        ids = [sid for sid, s in configured.items() if not group or s.get("group", "official") == group]
        if not ids:
            return dict(items=[], total=0, offset=offset, limit=limit)
        where = "source_id IN (" + ",".join("?" for _ in ids) + ")"
        params: List[Any] = list(ids)
        if query:
            named = [sid for sid in ids if query.casefold() in configured[sid]["name"].casefold()]
            by_name = " OR source_id IN (" + ",".join("?" for _ in named) + ")" if named else ""
            where += " AND (title LIKE ? ESCAPE '\\' OR source_id LIKE ? ESCAPE '\\'" + by_name + ")"
            pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            params += [pattern, pattern] + named
        sql = ("WITH matched AS (SELECT *,row_number() OVER(PARTITION BY url ORDER BY " + PLACEHOLDER_TITLE_SQL
               + ",first_seen,source_id,key) AS pick FROM items WHERE " + where + ") SELECT * FROM matched"
               " WHERE pick=1 ORDER BY first_seen DESC,source_time DESC,source_id,key LIMIT ? OFFSET ?")
        with self.lock:
            total = self.db.execute("SELECT count(DISTINCT url) FROM items WHERE " + where, params).fetchone()[0]
            rows = [dict(row) for row in self.db.execute(sql, params + [limit, offset])]
        for row in rows:
            source = configured[row["source_id"]]
            row.update(source_name=source["name"], group=source.get("group", "official"),
                       region=source.get("region", "global"))
        return dict(items=rows, total=total, offset=offset, limit=limit)
