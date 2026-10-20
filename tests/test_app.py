import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from support import REPO, entry, make_config, make_source
from newsradar import app as app_module
from newsradar.app import LOG, Radar, main


def fake_fetch(source, previous):
    now = time.time()
    return dict(started=now, finished=now, status=200, headers={}, entries=[entry(1)], fetch_ms=1.0)


class RadarTestCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "data").mkdir()
        self.addCleanup(shutil.rmtree, self.root, True)

    def radar(self, config=None, **kw):
        radar = Radar(root=self.root, config=config or make_config(make_source(), port=0), **kw)
        self.addCleanup(radar.store.db.close)
        return radar


class RadarTests(RadarTestCase):
    def test_reads_config_and_uses_root_data_directory(self):
        (self.root / "config.json").write_text(json.dumps(make_config(make_source(id="from-file"))))
        radar = Radar(root=self.root)
        self.addCleanup(radar.store.db.close)
        self.assertEqual(radar.config["sources"][0]["id"], "from-file")
        self.assertTrue((self.root / "data" / "radar.sqlite3").exists())
        self.assertEqual(radar.store.x_setup_path, self.root / "data" / "x-setup.json")

    def test_x_setup_comes_from_this_root(self):
        (self.root / "data" / "x-setup.json").write_text(json.dumps({"status": "verified", "accounts": []}))
        self.assertEqual(self.radar().store.state()["x_setup"]["status"], "verified")

    def test_run_once_polls_every_source(self):
        config = make_config(make_source(id="a"), make_source(id="b", url="https://b.test/f"))
        radar = self.radar(config, fetch=fake_fetch)
        out = io.StringIO()
        with redirect_stdout(out):
            radar.run_once()
        self.assertEqual(out.getvalue().splitlines(), ["a 200 1 None", "b 200 1 None"])
        self.assertEqual(radar.store.source("b")["status"], "ok")

    def test_poller_failed(self):
        radar = self.radar()
        self.assertTrue(radar.poller_failed())
        done = threading.Thread(target=lambda: None)
        done.start()
        done.join()
        radar.poller_thread = done
        self.assertTrue(radar.poller_failed())
        hold = threading.Event()
        live = threading.Thread(target=hold.wait, daemon=True)
        live.start()
        radar.poller_thread = live
        self.assertFalse(radar.poller_failed())
        radar.poller_heartbeat = time.time()
        self.assertFalse(radar.poller_failed())
        radar.poller_heartbeat = time.time() - 91
        self.assertTrue(radar.poller_failed())
        hold.set()


class ServeTests(RadarTestCase):
    def test_orderly_stop(self):
        radar = self.radar()
        with patch.object(radar, "run_polls", side_effect=radar.stop.set):
            radar.serve(bridge=False)
        self.assertTrue(radar.stop.is_set())

    def test_dead_poller_stops_the_server(self):
        radar = self.radar()
        with patch.object(radar, "run_polls", return_value=None):
            self.assertRaisesRegex(RuntimeError, "Polling worker stopped", radar.serve, False)
        self.assertTrue(radar.stop.is_set())

    def test_stalled_poller_stops_the_server(self):
        radar = self.radar()

        def stalled():  # alive but no longer heartbeating; exits once told to stop, like the real loop
            radar.poller_heartbeat = time.time() - 100
            radar.stop.wait(5)
        with patch.object(radar, "run_polls", side_effect=stalled):
            started = time.monotonic()
            self.assertRaisesRegex(RuntimeError, "stalled", radar.serve, False)
        self.assertLess(time.monotonic() - started, 3)

    def test_notifier_is_opened_only_when_built_and_requested(self):
        radar = self.radar()
        with patch.object(app_module.subprocess, "Popen") as popen, \
                patch.object(radar, "run_polls", side_effect=radar.stop.set):
            radar.serve(bridge=True)
        popen.assert_not_called()
        app = self.root / "native" / "AI News Radar.app"
        app.mkdir(parents=True)
        for bridge, calls in ((False, 0), (True, 1)):
            radar.stop.clear()
            with patch.object(app_module.subprocess, "Popen") as popen, \
                    patch.object(radar, "run_polls", side_effect=radar.stop.set):
                radar.serve(bridge=bridge)
            self.assertEqual(popen.call_count, calls)
        self.assertEqual(popen.call_args[0][0], ["/usr/bin/open", "-g", str(app)])


class MainTests(RadarTestCase):
    def setUp(self):
        super().setUp()
        (self.root / "config.json").write_text(json.dumps(make_config(port=0, sources=[])))
        handlers = list(LOG.handlers)

        def drop_new_handlers():
            for handler in [h for h in LOG.handlers if h not in handlers]:
                LOG.removeHandler(handler)
                handler.close()
        self.addCleanup(drop_new_handlers)

    def test_once_mode_creates_runtime_files(self):
        shutil.rmtree(self.root / "data")
        main(["--once"], root=self.root)
        for name in ("radar.sqlite3", "radar.lock", "service.log"):
            self.assertTrue((self.root / "data" / name).exists(), name)

    def test_second_collector_on_the_same_data_directory_is_refused(self):
        held = app_module._lock_data_directory(self.root / "data")
        self.addCleanup(held.close)
        with self.assertRaisesRegex(SystemExit, "already owns"):
            main(["--once"], root=self.root)

    def test_serve_mode_wires_signals_and_bridge_flag(self):
        with patch.object(Radar, "serve") as serve, patch.object(app_module.signal, "signal") as signal_:
            main(["--no-bridge"], root=self.root)
        serve.assert_called_once_with(bridge=False)
        self.assertEqual({c[0][0] for c in signal_.call_args_list}, {app_module.signal.SIGTERM, app_module.signal.SIGINT})

    def test_logging_goes_to_the_service_log(self):
        main(["--once"], root=self.root)
        LOG.info("probe line")
        for handler in LOG.handlers:
            handler.flush()
        self.assertIn("INFO probe line", (self.root / "data" / "service.log").read_text())

    def test_files_are_private(self):
        main(["--once"], root=self.root)
        mode = (self.root / "data" / "radar.sqlite3").stat().st_mode & 0o777
        self.assertEqual(mode & 0o077, 0)


class CompatibilityTests(unittest.TestCase):
    def test_radar_module_reexports_the_public_api(self):
        import radar
        import newsradar.store
        for name in radar.__all__:
            self.assertTrue(hasattr(radar, name), name)
        self.assertIs(radar.Store, newsradar.store.Store)
        self.assertIs(radar.main, main)
        self.assertEqual(radar.ROOT, REPO)

    def test_command_line_help(self):
        result = subprocess.run([sys.executable, str(REPO / "radar.py"), "--help"], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0)
        self.assertIn("--no-bridge", result.stdout)
        self.assertIn("--once", result.stdout)


if __name__ == "__main__":
    unittest.main()
