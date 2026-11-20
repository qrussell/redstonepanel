"""Tests for core/selfupdate.py: starting the panel's own update in a separate systemd unit,
the `redstonepanel upgrade` command it runs, and reading the outcome back after the restart.
systemctl, systemd-run, dpkg and apt are faked. Run: python -m unittest discover -s tests"""
import json
import os
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-selfupdate-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import host, runtimes, selfupdate as S, version  # noqa: E402

try:
    import fastapi  # noqa: F401
    HAVE_FASTAPI = True
except ImportError:
    HAVE_FASTAPI = False


class FakePopen:
    def __init__(self, test, cmd):
        self.test = test
        test.popen.append(cmd)
        out = test.apt_out.get(cmd[-1] if cmd[-1] != "redstonepanel" else "install", ("", 0))
        self.stdout = iter(out[0].splitlines(keepends=True))
        self.code = out[1]
        if cmd[-1] == "redstonepanel" and self.code == 0:
            test.installed = test.available

    def wait(self):
        return self.code


class Base(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp(dir=TMP)
        self.ran, self.popen = [], []
        self.active = "inactive"
        self.installed, self.available = "1.13.1", "1.13.2"
        self.systemd_run_code = 0
        self.apt_out = {}

        def run(cmd, **kw):
            self.ran.append(cmd)
            if cmd[0] == "systemctl":
                return SimpleNamespace(returncode=0, stdout=self.active + "\n", stderr="")
            if cmd[0] == "dpkg-query":
                return SimpleNamespace(returncode=0, stdout=self.installed, stderr="")
            if cmd[0] == "systemd-run":
                return SimpleNamespace(returncode=self.systemd_run_code, stdout="",
                                       stderr="" if self.systemd_run_code == 0 else "Unit already exists")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        patches = [
            mock.patch.object(S, "PACKAGED", True),
            mock.patch.object(S, "DATA_DIR", d),
            mock.patch.object(S, "STATE_FILE", os.path.join(d, "upgrade.json")),
            mock.patch.object(S, "LOG_FILE", os.path.join(d, "upgrade.log")),
            mock.patch.object(S.shutil, "which", lambda name: f"/usr/bin/{name}"),
            mock.patch.object(S.subprocess, "run", run),
            mock.patch.object(S.subprocess, "Popen", lambda cmd, **kw: FakePopen(self, cmd)),
            mock.patch.object(host, "panel_update", lambda: self.available if self.available != self.installed else None),
            mock.patch.object(version, "panel_version", lambda: self.installed),
            mock.patch.object(runtimes, "job", lambda: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def state(self):
        with open(S.STATE_FILE) as f:
            return json.load(f)


class Start(Base):
    def test_starts_its_own_unit(self):
        st = S.start(actor="alex")
        cmd = next(c for c in self.ran if c[0] == "systemd-run")
        self.assertEqual(cmd[:3], ["systemd-run", "--unit=redstonepanel-upgrade", "--collect"])
        self.assertEqual(cmd[-2:], ["/usr/sbin/redstonepanel", "upgrade"])
        s = self.state()
        self.assertEqual((s["state"], s["from"], s["to"], s["actor"]), ("running", "1.13.1", "1.13.2", "alex"))
        self.assertEqual(st["upgrade"]["state"], "running")

    def test_refusals(self):
        with mock.patch.object(S, "PACKAGED", False), self.assertRaises(S.UpdateError) as cm:
            S.start()
        self.assertIn("source checkout", str(cm.exception))
        self.active = "active"
        with self.assertRaises(S.UpdateError) as cm:
            S.start()
        self.assertEqual(cm.exception.status, 409)
        self.active = "inactive"
        with mock.patch.object(runtimes, "job", lambda: {"state": "running"}), self.assertRaises(S.UpdateError):
            S.start()
        self.available = "1.13.1"
        with self.assertRaises(S.UpdateError) as cm:
            S.start()
        self.assertIn("no newer", str(cm.exception))
        self.assertFalse([c for c in self.ran if c[0] == "systemd-run"])

    def test_systemd_run_fails(self):
        self.systemd_run_code = 1
        with self.assertRaises(S.UpdateError):
            S.start()
        self.assertEqual(self.state()["state"], "failed")
        self.assertIn("Unit already exists", self.state()["error"])

    def test_check_updates_lists(self):
        with mock.patch.object(host, "forget_panel_update") as forget:
            S.check()
        self.assertIn("update", next(c for c in self.ran if c[0] == "apt-get"))
        forget.assert_called_once()


class Run(Base):
    def test_upgrade_done(self):
        S.start(actor="alex")
        self.apt_out = {"update": ("Hit:1 file:/repo ./ InRelease\n", 0),
                        "install": ("Setting up redstonepanel (1.13.2) ...\n", 0)}
        self.assertEqual(S.run(), 0)
        self.assertEqual([c[-1] for c in self.popen], ["update", "redstonepanel"])
        install = self.popen[1]
        self.assertIn("--only-upgrade", install)
        self.assertIn("Dpkg::Options::=--force-confold", install)
        s = self.state()
        self.assertEqual((s["state"], s["installed"], s["error"]), ("done", "1.13.2", None))
        st = S.status()
        self.assertIn("Setting up redstonepanel (1.13.2) ...", st["upgrade"]["lines"])

    def test_upgrade_failed(self):
        S.start()
        self.apt_out = {"install": ("E: Unable to fetch some archives\n", 100)}
        self.assertEqual(S.run(), 100)
        s = self.state()
        self.assertEqual(s["state"], "failed")
        self.assertEqual(s["error"], "Unable to fetch some archives")

    def test_run_by_hand(self):
        self.apt_out = {"install": ("", 0)}
        self.assertEqual(S.run(), 0)
        s = self.state()
        self.assertEqual((s["state"], s["actor"], s["from"], s["to"]), ("done", "command line", "1.13.1", "1.13.2"))

    def test_unit_gone_without_result(self):
        S.start()
        s = self.state()
        s["started"] = int(time.time()) - 120
        S._write_state(s)
        self.assertEqual(S.last_upgrade()["state"], "failed")      # 1.13.1 is still installed
        self.installed = "1.13.2"
        self.assertEqual(S.last_upgrade()["state"], "done")
        self.active = "active"
        self.installed = "1.13.1"
        self.assertEqual(S.last_upgrade()["state"], "running")


@unittest.skipUnless(HAVE_FASTAPI, "needs fastapi")
class Api(unittest.TestCase):
    def test_admins_only(self):
        import main
        req = SimpleNamespace(state=SimpleNamespace(user={"username": "alex", "role": "user", "source": "panel"}))
        for call in (lambda: main.panel_update_status(req), lambda: main.check_panel_update(req),
                     lambda: main.start_panel_update(req)):
            with self.assertRaises(main.HTTPException) as cm:
                call()
            self.assertEqual(cm.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
