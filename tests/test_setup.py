"""Tests for core/setup.py: the first-run setup guide's steps are worked out from the panel's
state, and only "dismissed" and "keep HTTP" are stored. Run: python -m unittest discover -s tests"""
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-setup-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import setup as S  # noqa: E402

try:
    import fastapi  # noqa: F401
    HAVE_FASTAPI = True
except ImportError:
    HAVE_FASTAPI = False


class Setup(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(dir=TMP)
        self.servers = []
        self.schedules = {}
        self.https = "off"
        self.role = "standalone"
        self.webhook = False
        self.fw = {"installed": True, "active": True, "rules": ""}
        for p in (mock.patch.object(S, "STATE_FILE", os.path.join(self.root, "setup.json")),
                  mock.patch.object(S.host, "report", lambda: {"servers": [{"name": n} for n in self.servers]}),
                  mock.patch.object(S.host, "firewall", lambda: self.fw),
                  mock.patch.object(S.cluster, "role", lambda: self.role),
                  mock.patch.object(S.panel_settings, "load", lambda: {"https_mode": self.https}),
                  mock.patch.object(S.notify, "public", lambda: {"configured": self.webhook}),
                  mock.patch.object(S.backups, "get_schedule",
                                    lambda n: {"enabled": self.schedules.get(n, False)})):
            p.start()
            self.addCleanup(p.stop)

    def steps(self):
        st = S.status()
        return st, {s["id"]: s for s in st["steps"]}

    def test_fresh_install_has_nothing_done(self):
        st, steps = self.steps()
        self.assertEqual((st["done"], st["total"], st["complete"], st["dismissed"]), (0, 4, False, False))
        self.assertFalse(steps["backups"]["done"])      # no servers yet: nothing to back up
        self.assertEqual(steps["server"]["firewall"], "on")

    def test_steps_tick_from_state(self):
        self.servers = ["minecraft-a", "minecraft-b"]
        self.schedules = {"minecraft-a": True}
        _, steps = self.steps()
        self.assertTrue(steps["server"]["done"])
        self.assertEqual(steps["server"]["servers"], 2)
        self.assertFalse(steps["backups"]["done"])
        self.assertEqual(steps["backups"]["unscheduled"], ["minecraft-b"])

        self.schedules["minecraft-b"] = True
        self.https = "self_signed"
        self.webhook = True
        st, steps = self.steps()
        self.assertTrue(steps["backups"]["done"])
        self.assertTrue(st["complete"])

    def test_keeping_http_counts_and_is_stored(self):
        _, steps = self.steps()
        self.assertFalse(steps["connection"]["done"])
        S.save_state(http_ok=True)
        _, steps = self.steps()
        self.assertTrue(steps["connection"]["done"])
        self.assertTrue(steps["connection"]["http_ok"])
        S.save_state(http_ok=False)
        self.assertFalse(self.steps()[1]["connection"]["done"])

    def test_cluster_node_https_comes_with_joining(self):
        self.role = "node"
        self.https = "cluster"
        self.assertTrue(self.steps()[1]["connection"]["done"])

    def test_firewall_states(self):
        self.fw = {"installed": False, "active": False, "rules": ""}
        self.assertEqual(self.steps()[1]["server"]["firewall"], "missing")
        self.fw = {"installed": True, "active": False, "rules": ""}
        self.assertEqual(self.steps()[1]["server"]["firewall"], "off")

    def test_dismissal_keeps_the_other_answer(self):
        S.save_state(http_ok=True)
        S.save_state(dismissed=True)
        self.assertEqual(S.load_state(), {"dismissed": True, "http_ok": True})
        S.save_state(dismissed=False, http_ok=None)
        self.assertEqual(S.load_state(), {"dismissed": False, "http_ok": True})

    def test_bad_state_file_falls_back_to_defaults(self):
        with open(S.STATE_FILE, "w") as f:
            f.write("{not json")
        self.assertEqual(S.load_state(), S.DEFAULT_STATE)
        with open(S.STATE_FILE, "w") as f:
            json.dump({"dismissed": "yes", "extra": 1}, f)
        self.assertEqual(S.load_state(), S.DEFAULT_STATE)


@unittest.skipUnless(HAVE_FASTAPI, "needs fastapi")
class Routes(unittest.TestCase):
    def test_setup_api_is_admin_only(self):
        import main
        # The access-control middleware refuses non-admins everything under /api/settings
        self.assertTrue("/api/settings/setup".startswith(main.ADMIN_API))

    def test_put_saves_and_logs(self):
        import main
        logs = []
        req = SimpleNamespace(state=SimpleNamespace(user={"username": "root", "role": "admin", "source": "linux"}))
        with mock.patch.object(main.setup_guide, "save_state") as save, \
                mock.patch.object(main.setup_guide, "status", lambda: {"ok": True}), \
                mock.patch.object(main.manager, "write_log", logs.append):
            out = main.set_setup(req, main.SetupAnswers(dismissed=True))
        save.assert_called_once_with(dismissed=True, http_ok=None)
        self.assertEqual(out, {"ok": True})
        self.assertEqual(logs, ["[SETTINGS] root closed the setup guide."])


if __name__ == "__main__":
    unittest.main()
