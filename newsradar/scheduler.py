"""The polling loop: fetch due sources concurrently, one request per host at a time.

Ordering is "oldest due first" with a bounded head start for high-priority sources, so core sources
are fresh while a low-priority retry still overtakes them after ``12 * priority_step`` seconds.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple
from urllib.parse import urlsplit

from .policy import host_throttle

LOG = logging.getLogger("radar")

DEFAULT_WORKERS = 6
DEFAULT_HOST_GAP = 3            # seconds between two requests to the same host
DEFAULT_PRIORITY_STEP = 10      # seconds of head start per priority level
DEFAULT_PRIORITY = 10
MAX_PRIORITY_LEVEL = 12
TICK = 0.5
INTERNAL_ERROR_RETRY = 120

Source = Mapping[str, Any]
Due = Tuple[float, Source, Dict[str, Any], str]


def host_of(source: Source) -> str:
    return urlsplit(source["url"]).netloc


def due_sources(sources: Sequence[Source], rows: Mapping[str, Dict[str, Any]], now: float, *,
                busy_hosts: Set[str], host_ready_at: Mapping[str, float], startup_due: float,
                priority_step: float) -> List[Due]:
    """Sources ready to fetch, in dispatch order: ``(due_at, source, stored_row, host)``.

    A source is ready when its ``next_due`` has passed, no request to its host is in flight and the
    host's politeness gap has elapsed. Never-polled sources (``next_due`` 0) count as due at startup.
    """
    due = []
    for source in sources:
        row = rows.get(source["id"])
        if row is None:
            continue
        host = host_of(source)
        if row["next_due"] <= now and host not in busy_hosts and host_ready_at.get(host, 0) <= now:
            due.append((row["next_due"] or startup_due, source, row, host))
    # Priority is a finite head start, not permission to starve older work.
    level = lambda source: max(0, min(MAX_PRIORITY_LEVEL, source.get("priority", DEFAULT_PRIORITY)))  # noqa: E731
    due.sort(key=lambda d: (d[0] + level(d[1]) * priority_step, d[0]))
    return due


class Poller:
    """Owns the in-flight bookkeeping of one polling run. ``run`` blocks until ``stop`` is set."""

    def __init__(self, config: Mapping[str, Any], store, fetch: Callable[..., Dict[str, Any]],
                 stop: threading.Event, heartbeat: Callable[[float], None]) -> None:
        self.config, self.store, self.fetch, self.stop, self.heartbeat = config, store, fetch, stop, heartbeat
        self.workers = config.get("workers", DEFAULT_WORKERS)
        self.host_gap = config.get("host_gap_seconds", DEFAULT_HOST_GAP)
        self.priority_step = config.get("priority_step_seconds", DEFAULT_PRIORITY_STEP)
        self.active: Dict[Future, Tuple[Source, str]] = {}
        self.busy_hosts: Set[str] = set()
        self.host_ready_at: Dict[str, float] = {}

    def run(self) -> None:
        startup_due = time.time()
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            while not self.stop.is_set():
                now = time.time()
                self.heartbeat(now)
                self._collect_finished(now)
                self._dispatch_due(pool, now, startup_due)
                self.stop.wait(TICK)

    def _collect_finished(self, now: float) -> None:
        for future in [f for f in self.active if f.done()]:
            source, host = self.active.pop(future)
            self.busy_hosts.discard(host)
            self.host_ready_at[host] = now + self.host_gap
            try:
                result = future.result()
                pause = host_throttle(result.get("status"), result.get("headers", {}), now)
                if pause is not None:
                    self.host_ready_at[host] = now + pause
                self.store.apply(source, result)
            except Exception:
                LOG.exception("poll processing failed: %s", source["id"])
                self.store.mark_internal_error(source["id"], time.time() + INTERNAL_ERROR_RETRY)

    def _dispatch_due(self, pool: ThreadPoolExecutor, now: float, startup_due: float) -> None:
        due = due_sources(self.config["sources"], self.store.source_rows(), now, busy_hosts=self.busy_hosts,
                          host_ready_at=self.host_ready_at, startup_due=startup_due,
                          priority_step=self.priority_step)
        for _, source, row, host in due:
            if len(self.active) >= self.workers:
                break
            if host in self.busy_hosts:  # another source on this host was dispatched in this pass
                continue
            self.active[pool.submit(self.fetch, source, row)] = (source, host)
            self.busy_hosts.add(host)
