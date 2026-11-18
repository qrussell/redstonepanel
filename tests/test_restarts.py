"""Tests for core/restarts.py. Linux only (fcntl); units and server folders live in temp folders
and commands are recorded instead of run.
Run: python -m unittest discover -s tests"""
import os
import sys
import tempfile
import unittest
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-restarts-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import fcntl
    from core import backups as B, manager as M, notify as N, restarts as R  # noqa: E402
except ImportError:          # Windows
    R = None


@unittest.skipIf(R is None, "needs Linux (fcntl)")
class Restarts(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(dir=TMP)
        self.units = os.path.join(self.root, "units")
        os.makedirs(self.units)
        self.commands, self.said, self.slept, self.sent = [], [], [], []
        self.running = True
        self.restart_rc = 0
        def run(cmd, **kw):
            self.commands.append(cmd)
            if cmd[:2] == ["systemctl", "restart"]:
                return mock.Mock(returncode=self.restart_rc, stdout="", stderr="unit failed")
            return mock.Mock(returncode=0, stdout="", stderr="")
        for p in (mock.patch.object(R, "UNIT_DIR", self.units),
                  mock.patch.object(B, "UNIT_DIR", self.units),
                  mock.patch.object(R, "SCHEDULE_FILE", os.path.join(self.root, "restarts.json")),
                  mock.patch.object(B, "BACKUP_ROOT", os.path.join(self.root, "backups")),
                  mock.patch.object(B, "_is_active", lambda s: self.running),
                  mock.patch.object(B, "_edition", lambda s, d: "bedrock"),
                  mock.patch.object(B, "_screen", lambda s, cmd: self.said.append(cmd) or True),
                  mock.patch.object(M, "get_server_dir", lambda s: os.path.join(self.root, "opt", s)),
                  mock.patch.object(M, "run_and_log", lambda cmd, shell=False: self.commands.append(cmd)),
                  mock.patch.object(R.subprocess, "run", run),
                  mock.patch.object(N, "send", lambda e, s, m, test=False: self.sent.append((e, m)))):
            p.start()
            self.addCleanup(p.stop)
        with open(os.path.join(self.units, "minecraft-bedrock.service"), "w") as f:
            f.write("")

    # --- schedule ---
    def test_timer_fires_warning_minutes_early(self):
        self.assertEqual(R.on_calendar("daily", "05:00", 5), "*-*-* 04:55:00")
        self.assertEqual(R.on_calendar("daily", "05:00", 0), "*-*-* 05:00:00")
        self.assertEqual(R.on_calendar("daily", "00:05", 10), "*-*-* 23:55:00")
        self.assertEqual(R.on_calendar("6h", "06:00", 15), "*-*-* 05/6:45:00")
        self.assertEqual(R.on_calendar("weekly", "03:00", 5), "Sun *-*-* 02:55:00")
        self.assertEqual(R.on_calendar("weekly", "00:00", 1), "Sat *-*-* 23:59:00")

    def test_schedule_validation(self):
        for args in (("hourly", "05:00", 5), ("daily", "25:00", 5), ("daily", "05:00", 7),
                     ("daily", "05:00\nExecStart=/bin/sh", 5)):
            with self.assertRaises(R.RestartError, msg=args):
                R.set_schedule("minecraft-bedrock", True, *args)
        with self.assertRaises(R.RestartError):
            R.set_schedule("../x", True, "daily", "05:00", 5)

    def test_schedule_writes_and_removes_the_timer(self):
        R.set_schedule("minecraft-bedrock", True, "daily", "05:00", 5)
        with open(os.path.join(self.units, "redstonepanel-restart@minecraft-bedrock.timer")) as f:
            self.assertIn("OnCalendar=*-*-* 04:55:00", f.read())
        with open(os.path.join(self.units, R.SERVICE_TEMPLATE)) as f:
            self.assertIn("ExecStart=/usr/sbin/redstonepanel restart-server %i", f.read())
        self.assertIn(["systemctl", "enable", "redstonepanel-restart@minecraft-bedrock.timer"], self.commands)
        self.assertEqual(R.get_schedule("minecraft-bedrock")["warn"], 5)

        R.set_schedule("minecraft-bedrock", False, "daily", "05:00", 5)
        self.assertFalse(os.path.exists(os.path.join(self.units, "redstonepanel-restart@minecraft-bedrock.timer")))

    def test_removed_and_restored_servers(self):
        R.set_schedule("minecraft-bedrock", True, "daily", "05:00", 5)
        timer = os.path.join(self.units, "redstonepanel-restart@minecraft-bedrock.timer")
        R.on_server_removed("minecraft-bedrock", purged=False)
        self.assertFalse(os.path.exists(timer))
        R.on_server_restored("minecraft-bedrock")
        self.assertTrue(os.path.exists(timer))
        R.on_server_removed("minecraft-bedrock", purged=True)
        self.assertFalse(R.get_schedule("minecraft-bedrock")["enabled"])

    # --- running ---
    def test_warning_steps(self):
        self.assertEqual(R.warning_steps(0), [])
        self.assertEqual([s for s, _ in R.warning_steps(1)], [60, 10])
        self.assertEqual([s for s, _ in R.warning_steps(5)], [300, 60, 10])
        self.assertIn("1 minute.", R.warning_steps(1)[0][1])

    def test_restart_warns_then_restarts(self):
        R.set_schedule("minecraft-bedrock", True, "daily", "05:00", 5)
        self.commands.clear()
        self.assertEqual(R.run_restart("minecraft-bedrock", sleep=self.slept.append), "restarted")
        self.assertEqual(self.said, ["say Server restarts in 5 minutes.", "say Server restarts in 1 minute.",
                                     "say Server restarts in 10 seconds."])
        self.assertEqual(self.slept, [240, 50, 10])
        self.assertIn(["systemctl", "restart", "minecraft-bedrock.service"], self.commands)
        self.assertEqual(self.sent[-1][0], "restarted")

    def test_stopped_server_is_left_alone(self):
        self.running = False
        self.assertEqual(R.run_restart("minecraft-bedrock", sleep=self.slept.append), "skipped")
        self.assertEqual(self.said, [])
        self.assertFalse(any(c[:2] == ["systemctl", "restart"] for c in self.commands))

    def test_java_without_rcon_restarts_on_time_without_warning(self):
        R.set_schedule("minecraft-bedrock", True, "daily", "05:00", 5)
        with mock.patch.object(B, "_edition", lambda s, d: "java"), \
             mock.patch.object(B, "_java_rcon", lambda d: None):
            R.run_restart("minecraft-bedrock", sleep=self.slept.append)
        self.assertEqual(self.slept, [300])
        self.assertIn(["systemctl", "restart", "minecraft-bedrock.service"], self.commands)

    def test_restart_waits_for_a_backup(self):
        R.set_schedule("minecraft-bedrock", True, "daily", "05:00", 0)
        self.commands.clear()
        fd = B._lock("minecraft-bedrock")             # a backup is running
        with mock.patch.object(R, "BACKUP_WAIT", 0), mock.patch.object(R.time, "sleep", lambda s: None):
            with self.assertRaises(R.RestartError):
                R.run_restart("minecraft-bedrock", sleep=self.slept.append)
        os.close(fd)
        self.assertFalse(any(c[:2] == ["systemctl", "restart"] for c in self.commands))
        self.assertEqual(R.run_restart("minecraft-bedrock", sleep=self.slept.append), "restarted")

    def test_failed_restart_is_reported(self):
        R.set_schedule("minecraft-bedrock", True, "daily", "05:00", 0)
        self.restart_rc = 1
        with self.assertRaises(R.RestartError):
            R.run_restart("minecraft-bedrock", sleep=self.slept.append)
        self.assertIn("failed", self.sent[-1][1])


if __name__ == "__main__":
    unittest.main()
