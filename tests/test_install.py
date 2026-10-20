"""The installer against a simulated launchd: success, upgrade, rollback, ownership and job control."""
import io
import json
import plistlib
import shutil
import socket
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from support import make_config, make_source
import build_native
import manage


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeLaunchd:
    """Just enough of launchctl/ps/open for manage.py: tracks which labels are loaded."""

    def __init__(self, loaded=(), processes=""):
        self.loaded, self.processes, self.calls = set(loaded), processes, []
        self.unload_ignored = set()

    def __call__(self, *args, check=True):
        self.calls.append(args)
        returncode, stdout = 0, ""
        if args[:2] == ("/bin/launchctl", "print"):
            returncode = 0 if args[2].split("/")[-1] in self.loaded else 113
        elif args[:2] == ("/bin/launchctl", "bootout"):
            label = args[2].split("/")[-1]
            if label not in self.unload_ignored:
                self.loaded.discard(label)
        elif args[:2] == ("/bin/launchctl", "bootstrap"):
            self.loaded.add(Path(args[3]).stem)
        elif args[0] == "/bin/ps":
            stdout = self.processes
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")


class InstallTestCase(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.base, True)
        self.root, self.agents = self.base / "runtime", self.base / "agents"
        self.launchd = FakeLaunchd()
        self.config = make_config(make_source(), port=free_port())
        patches = [patch.object(manage, "ROOT", self.root), patch.object(manage, "AGENTS", self.agents),
                   patch.object(manage, "run", self.launchd), patch.object(manage, "build", self.fake_build),
                   patch.object(manage.time, "sleep", lambda s: None),
                   patch.object(manage.shutil, "which", return_value="/usr/bin/python3"),
                   patch.object(manage.platform, "system", return_value="Darwin")]  # CI also runs on Linux
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    def fake_build(output, port, data_dir):
        binary = Path(output) / "Contents" / "MacOS" / "AIRadar"
        binary.parent.mkdir(parents=True)
        binary.write_text("binary for port %d" % port)
        return binary

    def install(self, ready=True, **kw):
        wait = patch.object(manage, "wait_ready", side_effect=None if ready else RuntimeError("Collector did not become healthy"))
        with wait, redirect_stdout(io.StringIO()) as out:
            manage.install(self.config, **kw)
        return out.getvalue()


class InstallTests(InstallTestCase):
    def test_fresh_install(self):
        message = self.install(keep_awake=True)
        self.assertIn("Installed; data preserved", message)
        for name in manage.FILES:
            self.assertTrue((self.root / name).exists(), name)
        self.assertTrue((self.root / "newsradar" / "store.py").exists())
        self.assertTrue((self.root / manage.NOTIFIER_BINARY).exists())
        self.assertEqual(self.launchd.loaded, {manage.LABEL, manage.HELPER})
        collector = plistlib.loads(manage.plist_path(manage.LABEL).read_bytes())
        self.assertEqual(collector["ProgramArguments"],
                         ["/usr/bin/caffeinate", "-i", "/usr/bin/python3", str(self.root / "radar.py"), "--no-bridge"])
        self.assertEqual(manage.plist_path(manage.LABEL).stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.root / "data").stat().st_mode & 0o777, 0o700)

    def test_without_notifications_no_helper_job(self):
        self.install(notifications=False)
        self.assertEqual(self.launchd.loaded, {manage.LABEL})
        self.assertFalse(manage.plist_path(manage.HELPER).exists())

    def test_upgrade_keeps_data_and_snapshots_the_old_program(self):
        self.install()
        db = self.root / "data" / "radar.sqlite3"
        db.write_text("history")
        (self.root / "radar.py").write_text("# old program")
        self.install()
        self.assertEqual(db.read_text(), "history")
        self.assertNotEqual((self.root / "radar.py").read_text(), "# old program")
        snapshots = list((self.root / "backups").glob("program-*/radar.py"))
        self.assertTrue(any(p.read_text() == "# old program" for p in snapshots))

    def test_failed_readiness_rolls_everything_back(self):
        self.install()
        (self.root / "radar.py").write_text("# known good")
        good_plist = manage.plist_path(manage.LABEL).read_bytes()
        self.launchd.processes = "4242 %s\n" % (self.root / manage.NOTIFIER_BINARY)
        with patch.object(manage.os, "kill") as kill:
            self.assertRaisesRegex(RuntimeError, "did not become healthy", self.install, ready=False)
        kill.assert_called_once()
        self.assertEqual((self.root / "radar.py").read_text(), "# known good")
        self.assertEqual(manage.plist_path(manage.LABEL).read_bytes(), good_plist)
        self.assertEqual(self.launchd.loaded, {manage.LABEL, manage.HELPER})  # previously running jobs restarted
        self.assertIn(("/usr/bin/open", "-g", str(self.root / "native/AI News Radar.app")), self.launchd.calls)

    def test_failed_first_install_leaves_no_program_or_jobs(self):
        self.assertRaises(RuntimeError, self.install, ready=False)
        for name in manage.FILES:
            self.assertFalse((self.root / name).exists(), name)
        self.assertEqual(self.launchd.loaded, set())
        self.assertFalse(manage.plist_path(manage.LABEL).exists())

    def test_refuses_a_job_owned_by_another_program(self):
        self.agents.mkdir()
        manage.plist_path(manage.LABEL).write_bytes(plistlib.dumps({"Label": manage.LABEL, "ProgramArguments": ["/other/radar.py"]}))
        self.assertRaisesRegex(RuntimeError, "different job owns", self.install)
        self.assertFalse(self.root.exists())

    def test_refuses_an_occupied_port(self):
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", self.config["port"]))
            busy.listen()
            self.assertRaisesRegex(RuntimeError, "port is occupied", self.install)

    def test_concurrent_installs_are_refused(self):
        with manage.install_lock():
            self.assertRaisesRegex(RuntimeError, "Another install", self.install)


class PlatformTests(unittest.TestCase):
    def test_install_requires_macos(self):
        with patch.object(manage.platform, "system", return_value="Linux"):
            self.assertRaisesRegex(RuntimeError, "requires macOS", manage.install, make_config())


class JobControlTests(InstallTestCase):
    def test_start_job(self):
        self.assertRaisesRegex(RuntimeError, "Install first", manage.start_job, manage.LABEL)
        self.install()
        self.launchd.loaded.clear()
        manage.start_job(manage.LABEL)
        self.assertIn(manage.LABEL, self.launchd.loaded)
        manage.start_job(manage.LABEL)
        self.assertEqual(self.launchd.calls[-1][1], "kickstart")

    def test_stop_job_waits_and_gives_up(self):
        self.install()
        manage.stop_job(manage.LABEL)
        self.assertNotIn(manage.LABEL, self.launchd.loaded)
        self.launchd.loaded.add(manage.LABEL)
        self.launchd.unload_ignored.add(manage.LABEL)
        self.assertRaisesRegex(RuntimeError, "still unloading", manage.stop_job, manage.LABEL)

    def test_loaded_job_without_plist_is_not_ours(self):
        self.launchd.loaded.add(manage.LABEL)
        self.assertRaisesRegex(RuntimeError, "no owned plist", manage.assert_owned, manage.LABEL)

    def test_legacy_helper(self):
        binary = str(self.root / manage.NOTIFIER_BINARY)
        self.launchd.processes = "10 /bin/zsh\n11 %s\n12 %s --other-flag\n" % (binary, binary)
        with patch.object(manage.os, "kill") as kill:
            self.assertTrue(manage.stop_legacy_helper())
        kill.assert_called_once_with(11, manage.signal.SIGTERM)
        with patch.object(manage.os, "kill", side_effect=ProcessLookupError):
            self.assertFalse(manage.stop_legacy_helper())

    def test_wait_ready(self):
        good = {"root": str(self.root.resolve()), "poller_alive": True}
        with patch.object(manage, "health", side_effect=[OSError("refused"), dict(good, poller_alive=False), good]):
            self.assertEqual(manage.wait_ready(self.config), good)
        with patch.object(manage, "health", side_effect=OSError("refused")):
            self.assertRaisesRegex(RuntimeError, "refused", manage.wait_ready, self.config)
        with patch.object(manage, "health", return_value=dict(good, root="/somewhere/else")):
            self.assertRaises(RuntimeError, manage.wait_ready, self.config)

    def test_command_line_start_stop_disable(self):
        self.install()
        (self.root / "config.json").write_text(json.dumps(self.config))
        with patch.object(manage, "wait_ready"), redirect_stdout(io.StringIO()) as out:
            manage.main(["stop"])
            self.assertEqual(self.launchd.loaded, set())
            manage.main(["start"])
            self.assertEqual(self.launchd.loaded, {manage.LABEL, manage.HELPER})
            manage.main(["disable"])
        self.assertEqual(self.launchd.loaded, set())
        self.assertFalse(manage.plist_path(manage.LABEL).exists())
        self.assertIn("Stopped; data retained.", out.getvalue())

    def test_command_line_test_notification(self):
        (self.root).mkdir(parents=True)
        (self.root / "config.json").write_text(json.dumps(self.config))
        with patch.object(manage, "urlopen") as opened, redirect_stdout(io.StringIO()) as out:
            opened.return_value.__enter__.return_value.read.return_value = b'{"id": 7}'
            manage.main(["test"])
        request = opened.call_args[0][0]
        self.assertEqual(request.full_url, "http://127.0.0.1:%d/api/test" % self.config["port"])
        self.assertEqual(out.getvalue().strip(), '{"id": 7}')


class BuildNativeTests(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp()) / "AI News Radar.app"
        self.addCleanup(shutil.rmtree, self.out.parent, True)

    def fake_run(self, args, **kwargs):
        if args[:2] == ["xcrun", "--find"]:
            return SimpleNamespace(stdout="/usr/bin/swiftc\n")
        if args[:2] == ["xcrun", "--sdk"]:
            return SimpleNamespace(stdout="/SDK\n")
        self.commands.append(args)
        return SimpleNamespace(stdout="")

    def test_build_compiles_writes_info_and_signs(self):
        self.commands = []
        with patch.object(build_native.subprocess, "run", self.fake_run), \
                patch.object(build_native.platform, "system", return_value="Darwin"), \
                patch.object(build_native.platform, "machine", return_value="arm64"):
            binary = build_native.build(self.out, 9000, data_dir="/tmp/radar-data")
        compile_cmd, sign, verify = self.commands
        self.assertEqual(compile_cmd[0], "/usr/bin/swiftc")
        self.assertIn("arm64-apple-macos13.0", compile_cmd)
        self.assertEqual(compile_cmd[-1], str(binary))
        info = plistlib.loads((self.out / "Contents" / "Info.plist").read_bytes())
        self.assertEqual(info["AIRadarBaseURL"], "http://127.0.0.1:9000")
        self.assertEqual(info["AIRadarDataDirectory"], str(Path("/tmp/radar-data").resolve()))
        self.assertEqual(info["CFBundleIdentifier"], build_native.BUNDLE_ID)
        self.assertEqual((sign[:4], verify[:3]), (["codesign", "--force", "--sign", "-"], ["codesign", "--verify", "--strict"]))

    def test_build_rejections(self):
        with patch.object(build_native.platform, "system", return_value="Linux"):
            self.assertRaisesRegex(RuntimeError, "requires macOS", build_native.build, self.out)
        with patch.object(build_native.platform, "system", return_value="Darwin"):
            self.assertRaisesRegex(ValueError, "Invalid local port", build_native.build, self.out, 80)
            self.commands = []
            with patch.object(build_native.subprocess, "run", self.fake_run), \
                    patch.object(build_native.platform, "machine", return_value="ppc"):
                self.assertRaisesRegex(RuntimeError, "Unsupported Mac architecture", build_native.build, self.out)


if __name__ == "__main__":
    unittest.main()
