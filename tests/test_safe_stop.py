"""Tests for the game servers' ExecStop hook (`redstonepanel stop-unit`, core/notify.stop_bedrock):
Bedrock gets its own `stop` and time to save before systemd stops it; Java is left to SIGTERM.
Units and server folders live in temp folders; screen is stubbed.
Run: python -m unittest discover -s tests"""
import os
import sys
import tempfile
import unittest
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-safe-stop-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import notify as N  # noqa: E402


class Result:
    def __init__(self, code):
        self.returncode, self.stdout, self.stderr = code, "", ""


class SafeStop(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(dir=TMP)
        self.units = os.path.join(self.root, "units")
        os.makedirs(self.units)
        self.logs, self.calls, self.slept = [], [], 0
        for p in (mock.patch.object(N, "UNIT_DIR", self.units),
                  mock.patch.object(N, "_log", self.logs.append)):
            p.start()
            self.addCleanup(p.stop)

    def unit(self, name, edition):
        d = os.path.join(self.root, name)
        os.makedirs(d)
        start = (f"/usr/bin/screen -c {d}/screenrc -L -Logfile {d}/server.log -DmS {name} ./bedrock_server"
                 if edition == "bedrock" else "/usr/bin/java -Xms1G -Xmx2G -jar server.jar nogui")
        with open(os.path.join(self.units, f"{name}.service"), "w") as f:
            f.write(f"[Unit]\nDescription=Minecraft Server ({name})\n\n[Service]\n"
                    f"WorkingDirectory={d}\nExecStart={start}\n")
        return d

    def run_stub(self, code=0, on_stop=None):
        def run(cmd, **kw):
            self.calls.append(cmd)
            if on_stop:
                on_stop()
            return Result(code)
        return run

    def sleep(self, s):
        self.slept += s

    def test_bedrock_gets_stop_and_waits_for_quit(self):
        d = self.unit("minecraft-island", "bedrock")
        ticks = []
        def sleep(s):
            ticks.append(s)
            if len(ticks) == 3:                 # BDS saves for a few seconds, then quits
                with open(os.path.join(d, "server.log"), "w") as f:
                    f.write("[2026-11-21 10:00:00:000 INFO] Server stop requested.\nQuit correctly\n")
        r = N.stop_bedrock("minecraft-island", None, run=self.run_stub(), sleep=sleep)
        self.assertEqual(r, "saved")
        self.assertEqual(self.calls, [["sudo", "-u", "minecraft", "screen", "-S", "minecraft-island",
                                       "-p", "0", "-X", "stuff", "stop\r"]])
        self.assertEqual(len(ticks), 3)
        self.assertTrue(any("save its world" in l for l in self.logs))

    def test_bedrock_waits_for_main_process(self):
        self.unit("minecraft-island", "bedrock")
        alive = iter([True, True, False])
        with mock.patch.object(N, "_pid_alive", lambda pid: next(alive)):
            r = N.stop_bedrock("minecraft-island", 4242, run=self.run_stub(), sleep=self.sleep)
        self.assertEqual(r, "saved")
        self.assertEqual(self.slept, 2)

    def test_bedrock_that_never_quits_times_out(self):
        self.unit("minecraft-island", "bedrock")
        with mock.patch.object(N, "_pid_alive", lambda pid: True):
            r = N.stop_bedrock("minecraft-island", 4242, timeout=5, run=self.run_stub(), sleep=self.sleep)
        self.assertEqual(r, "timeout")
        self.assertEqual(self.slept, 5)
        self.assertTrue(any("didn't stop within 5 s" in l for l in self.logs))

    def test_no_screen_session_returns_at_once(self):
        self.unit("minecraft-island", "bedrock")
        r = N.stop_bedrock("minecraft-island", 4242, run=self.run_stub(code=1), sleep=self.sleep)
        self.assertEqual(r, "not-running")
        self.assertEqual(self.slept, 0)

    def test_java_is_left_to_sigterm(self):
        self.unit("minecraft-paper", "java")
        r = N.stop_bedrock("minecraft-paper", 4242, run=self.run_stub(), sleep=self.sleep)
        self.assertIsNone(r)
        self.assertEqual(self.calls, [])

    def test_not_a_panel_unit(self):
        for name in ("minecraft-ghost", "sshd", "minecraft-x; rm -rf /", ""):
            self.assertIsNone(N.stop_bedrock(name, None, run=self.run_stub(), sleep=self.sleep))
        self.assertEqual(self.calls, [])

    def test_real_pid_check(self):
        self.assertTrue(N._pid_alive(os.getpid()))
        self.assertFalse(N._pid_alive(2 ** 22 + 12345))


class DropIn(unittest.TestCase):
    def test_drop_in_runs_stop_hook_as_root_ignoring_failures(self):
        with open(os.path.join(ROOT, "packaging", "minecraft-.service.d", "redstonepanel.conf")) as f:
            lines = [l.strip() for l in f if not l.startswith("#")]
        self.assertIn("ExecStop=-+/usr/sbin/redstonepanel stop-unit %N $MAINPID", lines)

    def test_stop_unit_command_never_fails(self):
        import run as R
        with mock.patch.object(N, "stop_bedrock", side_effect=RuntimeError("boom")) as s:
            R.stop_unit_cmd("minecraft-island", "77")        # no exception
        s.assert_called_once_with("minecraft-island", 77)
        with mock.patch.object(N, "stop_bedrock") as s:
            R.stop_unit_cmd("minecraft-island")
        s.assert_called_once_with("minecraft-island", None)


if __name__ == "__main__":
    unittest.main()
