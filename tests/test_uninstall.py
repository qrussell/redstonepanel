"""Tests for core/uninstall.py: removing the panel without leaving game servers behind by accident.
Run: python -m unittest discover -s tests"""
import io
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-uninstall-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import uninstall as U  # noqa: E402

NOW = time.time()


def server(name, running=True, enabled=True, last_backup=NOW - 600, port=19132):
    return {"name": name, "edition": "Bedrock", "running": running, "enabled": enabled, "port": port,
            "directory": f"/opt/{name}", "last_backup": last_backup}


class Base(unittest.TestCase):
    def setUp(self):
        self.plan = os.path.join(TMP, f"plan-{self._testMethodName}.json")
        self.patches = [mock.patch.object(U, "PLAN_FILE", self.plan),
                        mock.patch.object(U.manager, "write_log")]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def machine(self, *servers, windows=False):
        self.patches += [mock.patch.object(U, "servers", return_value=list(servers)),
                         mock.patch.object(U, "on_windows", return_value=windows)]
        for p in self.patches[-2:]:
            p.start()


class Check(Base):
    def test_every_server_needs_a_choice(self):
        self.machine(server("minecraft-a"), server("minecraft-b"))
        with self.assertRaisesRegex(U.UninstallError, "minecraft-b"):
            U.check({"minecraft-a": "stop"})

    def test_unknown_server_or_choice(self):
        self.machine(server("minecraft-a"))
        with self.assertRaisesRegex(U.UninstallError, "no server"):
            U.check({"minecraft-a": "stop", "minecraft-x": "stop"})
        with self.assertRaises(U.UninstallError):
            U.check({"minecraft-a": "explode"})

    def test_keep_running_is_linux_only(self):
        self.machine(server("minecraft-a"), windows=True)
        with self.assertRaisesRegex(U.UninstallError, "Windows"):
            U.check({"minecraft-a": "keep"})
        U.check({"minecraft-a": "stop"})

    def test_delete_needs_a_recent_backup_and_the_word(self):
        self.machine(server("minecraft-old", last_backup=NOW - 3 * 86400), server("minecraft-new"))
        with self.assertRaisesRegex(U.UninstallError, "Back up minecraft-old"):
            U.check({"minecraft-old": "delete", "minecraft-new": "stop"}, confirm="DELETE")
        with self.assertRaisesRegex(U.UninstallError, "Type DELETE"):
            U.check({"minecraft-old": "stop", "minecraft-new": "delete"}, confirm="delete it")
        U.check({"minecraft-old": "stop", "minecraft-new": "delete"}, confirm="DELETE")

    def test_never_backed_up_cant_be_deleted(self):
        self.machine(server("minecraft-a", last_backup=None))
        with self.assertRaisesRegex(U.UninstallError, "Back up"):
            U.check({"minecraft-a": "delete"}, confirm="DELETE")


class Apply(Base):
    def test_stop_keep_delete(self):
        self.machine(server("minecraft-a"), server("minecraft-b"), server("minecraft-c"))
        backups_dir = os.path.join(TMP, "bk-c")
        os.makedirs(backups_dir)
        with mock.patch.object(U.manager, "remove_server") as rm, \
                mock.patch("core.backups.server_backup_dir", return_value=backups_dir), \
                mock.patch("core.platform.system.delete_tree") as wipe:
            done = U.apply({"minecraft-a": "stop", "minecraft-b": "keep", "minecraft-c": "delete"},
                           actor="alex", confirm="DELETE", delete_backups=True)
        rm.assert_has_calls([mock.call("minecraft-a", keep_world=True), mock.call("minecraft-c", keep_world=False)])
        self.assertEqual(rm.call_count, 2)                     # the kept one is left alone
        wipe.assert_called_once_with(backups_dir)
        self.assertEqual((done["stopped"], done["kept"], done["deleted"]), (["minecraft-a"], ["minecraft-b"], ["minecraft-c"]))
        self.assertEqual(U.load_plan()["keep"], ["minecraft-b"])

    def test_backups_stay_unless_asked(self):
        self.machine(server("minecraft-c"))
        with mock.patch.object(U.manager, "remove_server"), mock.patch("core.platform.system.delete_tree") as wipe:
            U.apply({"minecraft-c": "delete"}, confirm="DELETE")
        wipe.assert_not_called()
        self.assertIsNone(U.load_plan())

    def test_nothing_happens_when_a_choice_is_refused(self):
        self.machine(server("minecraft-a", last_backup=None))
        with mock.patch.object(U.manager, "remove_server") as rm, self.assertRaises(U.UninstallError):
            U.apply({"minecraft-a": "delete"}, confirm="DELETE")
        rm.assert_not_called()


class PackageHook(Base):
    def test_warns_about_every_server_left_behind_and_stops_none(self):
        self.machine(server("minecraft-bedrock"), server("minecraft-java", running=False, enabled=True, port=25565),
                     server("minecraft-off", running=False, enabled=False))
        out = io.StringIO()
        with mock.patch.object(U.manager, "remove_server") as rm, \
                mock.patch.object(U.manager, "list_removed_servers", return_value=[]), \
                mock.patch("core.backups.on_server_removed") as b, mock.patch("core.restarts.on_server_removed") as r, \
                mock.patch("core.updates.on_server_removed"), \
                mock.patch.object(U, "turn_off_own_units") as own, redirect_stdout(out):
            left = U.package_hook()
        rm.assert_not_called()
        own.assert_called_once()
        self.assertEqual(left, ["minecraft-bedrock", "minecraft-java"])
        b.assert_any_call("minecraft-bedrock", purged=False)    # timers off, schedules kept
        r.assert_any_call("minecraft-off", purged=False)
        text = out.getvalue()
        self.assertIn("nothing to", text)
        self.assertIn("minecraft-bedrock", text)
        self.assertIn("running, starts at boot", text)
        self.assertIn("sudo redstonepanel uninstall", text)
        self.assertNotIn("minecraft-off ", text)
        self.assertTrue(U.load_plan()["removed"])

    def test_quiet_when_nothing_is_left(self):
        self.machine(server("minecraft-off", running=False, enabled=False))
        out = io.StringIO()
        with mock.patch.object(U.manager, "list_removed_servers", return_value=[{"name": "minecraft-a"}]), \
                mock.patch("core.backups.on_server_removed"), mock.patch("core.restarts.on_server_removed"), \
                mock.patch("core.updates.on_server_removed"), mock.patch.object(U, "turn_off_own_units"), \
                redirect_stdout(out):
            self.assertEqual(U.package_hook(), [])
        self.assertNotIn("nothing to manage", out.getvalue())
        self.assertIn("Removed servers", out.getvalue())

    def test_back_after_reinstall(self):
        U._save_plan({"keep": ["minecraft-a", "minecraft-gone"], "removed": True})
        with mock.patch.object(U.manager, "list_servers", return_value=[{"name": "minecraft-a"}]), \
                mock.patch("core.backups.on_server_restored") as b, mock.patch("core.restarts.on_server_restored") as r:
            self.assertEqual(U.after_reinstall(), ["minecraft-a"])
        b.assert_called_once_with("minecraft-a")
        r.assert_called_once_with("minecraft-a")
        self.assertIsNone(U.load_plan())

    def test_a_plan_before_removal_is_left_for_the_package(self):
        U._save_plan({"keep": ["minecraft-a"]})
        self.assertEqual(U.after_reinstall(), [])
        self.assertIsNotNone(U.load_plan())


class OwnUnits(Base):
    def test_turns_off_the_panels_timers_and_services_not_templates(self):
        files = {".timer": ["redstonepanel-backup@minecraft-a.timer", "redstonepanel-updates.timer"],
                 ".service": ["redstonepanel-backup@.service", "redstonepanel-sftp.service"]}
        self.machine()
        ok = mock.Mock(returncode=0)
        with mock.patch.object(U.services, "unit_files", side_effect=lambda prefix, suffix: files[suffix]), \
                mock.patch.object(U.services, "systemctl", return_value=ok) as sc, \
                mock.patch.object(U.manager, "close_firewall_rules"):
            turned = U.turn_off_own_units()
        self.assertEqual(turned, ["redstonepanel-backup@minecraft-a.timer", "redstonepanel-sftp.service",
                                  "redstonepanel-updates.timer"])
        sc.assert_any_call("disable", "--now", "redstonepanel-updates.timer", timeout=60)

    def test_nothing_on_windows(self):
        self.machine(windows=True)
        self.assertEqual(U.turn_off_own_units(), [])


class Packaging(unittest.TestCase):
    def test_prerm_runs_the_hook_only_on_remove(self):
        with open(os.path.join(ROOT, "debian", "redstonepanel.prerm")) as f:
            text = f.read()
        self.assertIn('[ "$1" = "remove" ]', text)
        self.assertIn("redstonepanel uninstall-hook || true", text)
        self.assertLess(text.index("uninstall-hook"), text.index("#DEBHELPER#"))

    def test_postinst_says_how_to_remove_it(self):
        with open(os.path.join(ROOT, "debian", "redstonepanel.postinst")) as f:
            self.assertIn("sudo redstonepanel uninstall", f.read())

    def test_listed_in_the_package(self):
        with open(os.path.join(ROOT, "debian", "redstonepanel.install")) as f:
            self.assertIn("core/uninstall.py", f.read())


if __name__ == "__main__":
    unittest.main()
