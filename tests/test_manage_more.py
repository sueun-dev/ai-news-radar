"""Additional installer/migration coverage: staging, validation, archive limits and the command line."""
import hashlib
import io
import json
import shutil
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from support import REPO, make_config, make_source
import manage
from newsradar.parsers import SOURCE_KINDS


class ValidateConfigTests(unittest.TestCase):
    def test_repository_config_is_valid(self):
        config = json.loads((REPO / "config.json").read_text())
        self.assertIs(manage.validate_config(config), config)

    def test_every_parser_kind_is_accepted_and_nothing_else(self):
        for kind in SOURCE_KINDS:
            manage.validate_config(make_config(make_source(kind=kind)))
        self.assertRaisesRegex(ValueError, "unknown parser", manage.validate_config, make_config(make_source(kind="podcast")))

    def test_rejections(self):
        cases = {"port": [make_config(port=80), make_config(port="8741"), make_config(port=70000), []],
                 "nonempty sources": [make_config(sources=[]), dict(port=8741)],
                 "unique": [make_config(make_source(id="")), make_config(make_source(id=3))],
                 "interval": [make_config(make_source(interval=0)), make_config(make_source(interval="60"))],
                 "HTTP": [make_config(make_source(url="ftp://a.test")), make_config(make_source(url=""))]}
        for message, configs in cases.items():
            for config in configs:
                with self.subTest(config=config):
                    self.assertRaisesRegex(ValueError, message, manage.validate_config, config)


class StagingTests(unittest.TestCase):
    def test_stage_contains_the_whole_program(self):
        with tempfile.TemporaryDirectory() as td:
            stage = Path(td)
            with patch.object(manage, "build") as build:
                manage.stage_program(stage, make_config(), notifications=False)
            build.assert_not_called()
            for name in manage.PROGRAM_FILES + manage.PROGRAM_DIRS + ("config.json",):
                self.assertTrue((stage / name).exists(), name)
            self.assertTrue((stage / "newsradar" / "app.py").exists())
            self.assertFalse(list(stage.rglob("__pycache__")))
            self.assertEqual(json.loads((stage / "config.json").read_text()), make_config())

    def test_staged_program_imports_from_its_own_directory(self):
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as td:
            stage = Path(td)
            with patch.object(manage, "build"):
                manage.stage_program(stage, make_config(), notifications=False)
            out = subprocess.run([sys.executable, "-c", "import radar,newsradar;print(newsradar.ROOT)"], cwd=stage,
                                 capture_output=True, text=True, timeout=30)
            self.assertEqual(out.stdout.strip(), str(stage.resolve()))

    def test_notifications_build_the_helper_against_the_runtime_data_dir(self):
        with tempfile.TemporaryDirectory() as td, patch.object(manage, "build") as build:
            manage.stage_program(Path(td), make_config(port=9999), notifications=True)
        args = build.call_args[0]
        self.assertEqual((args[1], args[2]), (9999, manage.ROOT / "data"))

    def test_files_list_covers_program_and_config_but_never_data(self):
        self.assertIn("newsradar", manage.FILES)
        self.assertIn("config.json", manage.FILES)
        self.assertNotIn("data", manage.FILES)


class RestoreValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def archive(self, **overrides):
        contents = {"config.json": json.dumps(make_config()).encode(), "radar.sqlite3": b"not really sqlite"}
        manifest = {"format": 1, "sha256": {k: hashlib.sha256(v).hexdigest() for k, v in contents.items()}}
        manifest.update(overrides.pop("manifest", {}))
        path = self.tmp / "backup.zip"
        with zipfile.ZipFile(path, "w") as z:
            for name, data in dict(contents, **overrides).items():
                z.writestr(name, data)
            z.writestr("manifest.json", json.dumps(manifest))
        return path

    def test_unknown_format(self):
        self.assertRaisesRegex(ValueError, "Unknown backup version", manage.restore, self.archive(manifest={"format": 2}))

    def test_invalid_config_inside_backup(self):
        bad = json.dumps({"port": 1}).encode()
        path = self.archive(**{"config.json": bad}, manifest={"sha256": {
            "config.json": hashlib.sha256(bad).hexdigest(),
            "radar.sqlite3": hashlib.sha256(b"not really sqlite").hexdigest()}})
        self.assertRaisesRegex(ValueError, "port", manage.restore, path)

    def test_invalid_sqlite_is_refused_without_touching_the_target(self):
        target = self.tmp / "fresh"
        with patch.object(manage, "ROOT", target), patch.object(manage, "loaded", return_value=False):
            self.assertRaises(Exception, manage.restore, self.archive())
        self.assertFalse((target / "data" / "radar.sqlite3").exists())

    def test_oversized_entry(self):
        from types import SimpleNamespace
        huge = [SimpleNamespace(file_size=manage.BACKUP_FILE_LIMIT + 1)]
        with patch.object(zipfile.ZipFile, "infolist", return_value=huge):
            self.assertRaisesRegex(ValueError, "512 MB", manage.restore, self.archive())

    def test_running_jobs_block_restore(self):
        target = self.tmp / "fresh"
        with patch.object(manage, "ROOT", target), patch.object(manage, "loaded", return_value=True):
            self.assertRaisesRegex(ValueError, "Stop the installed jobs", manage.restore, self.archive())


class CommandLineTests(unittest.TestCase):
    def test_errors_exit_with_status_one(self):
        with patch.object(manage, "backup", side_effect=ValueError("Backup output already exists")):
            with self.assertRaises(SystemExit) as raised, patch("sys.stderr", new_callable=io.StringIO) as err:
                manage.main(["backup", "--output", "/tmp/x.zip"])
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("already exists", err.getvalue())

    def test_status_reports_an_unreachable_collector(self):
        import socket
        with socket.socket() as probe:  # a valid port nobody listens on
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config.json").write_text(json.dumps(make_config(port=port)))
            out = io.StringIO()
            with patch.object(manage, "ROOT", root), patch.object(manage, "loaded", return_value=False), redirect_stdout(out):
                manage.main(["status"])
        report = json.loads(out.getvalue())
        self.assertEqual((report["collector_loaded"], report["notifier_loaded"]), (False, False))
        self.assertIn("error", report)

    def test_status_reports_a_running_collector(self):
        import threading
        from newsradar.app import Radar
        from newsradar.web import RadarHTTPServer
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "data").mkdir()
            config = make_config(make_source(), port=0)
            radar = Radar(root=root, config=config)
            server = RadarHTTPServer(("127.0.0.1", 0), radar.handler())
            config["port"] = server.server_port
            (root / "config.json").write_text(json.dumps(config))
            threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
            hold = threading.Event()  # a live, heartbeating poller so /health answers 200
            radar.poller_thread = threading.Thread(target=hold.wait, daemon=True)
            radar.poller_thread.start()
            radar.poller_heartbeat = __import__("time").time()
            try:
                # urllib sends Host 127.0.0.1:<port>, which the loopback-only server accepts.
                out = io.StringIO()
                with patch.object(manage, "ROOT", root), patch.object(manage, "loaded", return_value=True), redirect_stdout(out):
                    manage.main(["status"])
            finally:
                hold.set()
                server.shutdown()
                server.server_close()
                radar.store.db.close()
        report = json.loads(out.getvalue())
        self.assertNotIn("error", report)
        self.assertEqual((report["source_count"], report["errors"]), (1, [{"id": "a", "error": None}]))
        self.assertEqual((report["health"]["running"], report["health"]["poller_alive"]), (True, True))
        self.assertIn("by_basis", report["metrics"])

    def test_job_specs_without_keep_awake_or_notifier(self):
        specs = manage.job_specs("/usr/bin/python3", notifications=False, keep_awake=False)
        self.assertEqual(list(specs), [manage.LABEL])
        self.assertEqual(specs[manage.LABEL]["ProgramArguments"][0], "/usr/bin/python3")
        self.assertTrue(specs[manage.LABEL]["StandardErrorPath"].endswith("data/collector.err.log"))


if __name__ == "__main__":
    unittest.main()
