"""The long-running collector: poller thread + loopback web server, and the command-line entry point.

Supervision contract (launchd ``KeepAlive``): the process exits when the poller dies or stops
heartbeating for 90 seconds, so the supervisor restarts it instead of serving stale data.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import signal
import subprocess
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence

from . import ROOT
from .fetcher import fetch_source
from .scheduler import Poller
from .store import Store
from .web import HEARTBEAT_STALE, RadarHTTPServer, make_handler

LOG = logging.getLogger("radar")

NOTIFIER_APP = Path("native") / "AI News Radar.app"
LOG_MAX_BYTES = 2_000_000
LOG_BACKUPS = 3
STOP_JOIN_TIMEOUT = 45


class Radar:
    def __init__(self, root=ROOT, config: Optional[Mapping[str, Any]] = None, database=None,
                 fetch: Optional[Callable[..., Dict[str, Any]]] = None) -> None:
        self.root = Path(root)
        self.config = config if config is not None else json.loads((self.root / "config.json").read_text())
        data_dir = self.root / "data"
        self.store = Store(database or data_dir / "radar.sqlite3", self.config,
                           x_setup_path=data_dir / "x-setup.json")
        self.fetch = fetch or fetch_source
        self.stop = threading.Event()
        self.last_test = float("-inf")  # monotonic time of the last synthetic notification
        self.poller_thread: Optional[threading.Thread] = None
        self.poller_heartbeat: Optional[float] = None

    def _beat(self, now: float) -> None:
        self.poller_heartbeat = now

    def run_polls(self) -> None:
        Poller(self.config, self.store, self.fetch, self.stop, self._beat).run()

    def run_once(self) -> None:
        """Poll every source once, sequentially, and print a line per source."""
        for source in self.config["sources"]:
            result = self.fetch(source, self.store.source(source["id"]))
            self.store.apply(source, result)
            print(source["id"], result["status"], len(result["entries"]), result.get("error"))

    def handler(self):
        return make_handler(self)

    def poller_failed(self) -> bool:
        thread = self.poller_thread
        stalled = self.poller_heartbeat is not None and time.time() - self.poller_heartbeat > HEARTBEAT_STALE
        return thread is None or not thread.is_alive() or stalled

    def _open_notifier(self) -> None:
        app = self.root / NOTIFIER_APP
        if app.exists():
            subprocess.Popen(["/usr/bin/open", "-g", str(app)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def serve(self, bridge: bool = True) -> None:
        """Serve until ``stop`` is set; raise if the poller dies or stalls so the supervisor restarts us."""
        server = RadarHTTPServer(("127.0.0.1", self.config["port"]), self.handler())
        self.poller_thread = threading.Thread(target=self.run_polls, daemon=True)
        self.poller_thread.start()
        if bridge:
            self._open_notifier()
        server.timeout = 1
        try:
            while not self.stop.is_set():
                server.handle_request()
                if not self.stop.is_set() and self.poller_failed():
                    raise RuntimeError("Polling worker stopped or stalled; supervisor should restart collector")
        finally:
            self.stop.set()
            server.server_close()
            self.poller_thread.join(timeout=STOP_JOIN_TIMEOUT)


def _lock_data_directory(data_dir: Path):
    """Hold an exclusive lock for the process lifetime: one collector per data directory."""
    lock = (data_dir / "radar.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise SystemExit("A collector already owns this data directory.")
    return lock


def _configure_logging(data_dir: Path) -> None:
    handler = RotatingFileHandler(data_dir / "service.log", maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)


def main(argv: Optional[Sequence[str]] = None, root: Path = ROOT) -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="AI News Radar collector")
    parser.add_argument("--once", action="store_true", help="poll every source once and exit")
    parser.add_argument("--no-bridge", action="store_true", help="do not launch the macOS notification helper")
    args = parser.parse_args(argv)
    data_dir = Path(root) / "data"
    data_dir.mkdir(exist_ok=True)
    lock = _lock_data_directory(data_dir)
    try:
        _configure_logging(data_dir)
        radar = Radar(root=root)
        if args.once:
            radar.run_once()
            return
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: radar.stop.set())
        radar.serve(bridge=not args.no_bridge)
    finally:
        lock.close()
