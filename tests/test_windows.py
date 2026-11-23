"""Tests for the Windows side of core/platform (1.22) that need no Windows: it has every
function the Linux side has, and the parts that are plain logic (reading unit files, schedules,
firewall text, file names Windows won't take, service account names) give the right answers.
What needs Windows itself is tested by packaging/test/test-windows.py on Windows machines.
Run: python -m unittest discover -s tests"""
import datetime
import importlib
import inspect
import os
import sys
import tempfile
import unittest
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-windows-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import platform                                   # noqa: E402
from core.platform import paths                             # noqa: E402
from core.platform.windows import _win, dirfd, firewall, units   # noqa: E402


class SameFunctions(unittest.TestCase):
    def test_windows_has_every_function_with_the_same_arguments(self):
        def public(mod):
            return {n: v for n, v in vars(mod).items()
                    if inspect.isfunction(v) and v.__module__ == mod.__name__ and not n.startswith("_")}
        for area in platform.AREAS:
            linux = public(importlib.import_module(f"core.platform.linux.{area}"))
            windows = public(importlib.import_module(f"core.platform.windows.{area}"))
            self.assertEqual(sorted(set(linux) - set(windows)), [], f"missing from windows/{area}.py")
            for name in linux:
                self.assertEqual(list(inspect.signature(linux[name]).parameters),
                                 list(inspect.signature(windows[name]).parameters), f"{area}.{name}")

    def test_windows_is_a_system(self):
        self.assertIn("windows", platform.SYSTEMS)

    def test_layout_keeps_everything_under_the_home_folder(self):
        plain = {k: v for k, v in os.environ.items() if not k.startswith("RP_")}
        with mock.patch.dict(os.environ, plain, clear=True):
            layout = paths.windows_layout(r"D:\RedstonePanel")
        for key in ("SERVERS_ROOT", "BACKUP_ROOT", "UNIT_DIR", "JVM_ROOT", "JOURNAL_DIR"):
            self.assertTrue(layout[key].replace("/", "\\").startswith("D:\\RedstonePanel\\"), f"{key}={layout[key]}")


class UnitFiles(unittest.TestCase):
    def write(self, name, text):
        path = os.path.join(self.dir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def setUp(self):
        self.dir = tempfile.mkdtemp(dir=TMP)

    def test_a_game_server_unit_with_its_drop_in(self):
        self.write("minecraft-java.service", "[Unit]\nDescription=Minecraft\n\n[Service]\nUser=minecraft\n"
                   "WorkingDirectory=C:\\rp\\servers\\minecraft-java\n"
                   "ExecStart=C:\\rp\\java\\java-21-openjdk-amd64\\bin\\java.exe -Xmx2G -jar server.jar nogui\n"
                   "Restart=on-failure\nRestartSec=20\n\n[Install]\nWantedBy=multi-user.target\n")
        self.write("minecraft-.service.d/redstonepanel.conf", "[Service]\nExecStopPost=notify %N\nTimeoutStopSec=2min\n")
        spec = units.load("minecraft-java.service", self.dir)
        self.assertEqual(units.get(spec, "Service", "Restart"), "on-failure")
        self.assertEqual(units.split_command(units.get(spec, "Service", "ExecStart"))[-3:], ["-jar", "server.jar", "nogui"])
        self.assertEqual(units.get(spec, "Service", "ExecStopPost"), "notify minecraft-java")
        self.assertEqual(units.seconds(units.get(spec, "Service", "TimeoutStopSec"), 90), 120)
        self.assertEqual(units.seconds(units.get(spec, "Service", "RestartSec"), 0), 20)
        self.assertIsNone(units.load("minecraft-none.service", self.dir))

    def test_a_template_instance(self):
        self.write("redstonepanel-backup@.service", "[Service]\nType=oneshot\nExecStart=redstonepanel backup %i\n")
        self.assertEqual(units.template_of("redstonepanel-backup@minecraft-java.service"),
                         ("redstonepanel-backup@.service", "minecraft-java"))
        spec = units.load("redstonepanel-backup@minecraft-java.service", self.dir)
        self.assertEqual(units.get(spec, "Service", "ExecStart"), "redstonepanel backup minecraft-java")

    def test_command_lines_and_environment(self):
        self.assertEqual(units.split_command('-"C:\\a b\\java.exe" -jar x.jar'), ["C:\\a b\\java.exe", "-jar", "x.jar"])
        self.assertEqual(units.environment(units.parse('[Service]\nEnvironment=A=1 "B=two words"\nEnvironment=C=3\n')),
                         {"A": "1", "B": "two words", "C": "3"})
        cleared = units.parse("[Service]\nExecStart=a\nExecStart=\nExecStart=b\n")
        self.assertEqual(cleared["Service"]["ExecStart"], ["b"])


class Schedules(unittest.TestCase):
    def test_the_schedules_the_panel_writes(self):
        self.assertEqual(units.parse_calendar("*-*-* 04:00:00"), {"days": None, "hours": [4], "minute": 0, "every": None})
        self.assertEqual(units.parse_calendar("Sun *-*-* 05:00:00")["days"], [6])
        self.assertEqual(units.parse_calendar("Mon,Thu *-*-* 03:30:00")["days"], [0, 3])
        self.assertEqual(units.parse_calendar("*-*-* 00/6:00:00")["hours"], [0, 6, 12, 18])
        with self.assertRaises(ValueError):
            units.parse_calendar("*-*-01 02:00:00")

    def test_next_run(self):
        monday = datetime.datetime(2026, 11, 23, 12, 0)
        self.assertEqual(units.next_run("*-*-* 04:00:00", monday), datetime.datetime(2026, 11, 24, 4, 0))
        self.assertEqual(units.next_run("*-*-* 00/6:00:00", monday), datetime.datetime(2026, 11, 23, 18, 0))
        self.assertEqual(units.next_run("Sun *-*-* 05:00:00", monday), datetime.datetime(2026, 11, 29, 5, 0))
        self.assertEqual(units.systemd_time(datetime.datetime(2026, 11, 24, 4, 0)), "Tue 2026-11-24 04:00:00")

    def test_the_task_runs_as_the_system_at_those_times(self):
        xml = units.task_xml("C:\\p\\python.exe", "run.py backup minecraft-java", "Sun *-*-* 05:00:00", "Backup")
        self.assertIn("S-1-5-18", xml)
        self.assertIn("<Sunday", xml)
        self.assertIn("run.py backup minecraft-java", xml)
        daily = units.task_xml("x", "y & <z>", "*-*-* 04:00:00", "d")
        self.assertIn("ScheduleByDay", daily)
        self.assertIn("y &amp; &lt;z&gt;", daily)


class FirewallText(unittest.TestCase):
    def test_rules_read_like_ufw_status(self):
        text = firewall.to_ufw_text("profiles=3\nrule=TCP|25565\nrule=UDP|19132,19133\nrule=TCP|8000-8010\nrule=ICMPv4|Any\n")
        self.assertTrue(text.startswith("Status: active"))
        for want in ("25565/tcp", "19132/udp", "19133/udp", "8000:8010/tcp"):
            self.assertIn(want, text)
        self.assertNotIn("icmp", text.lower())
        self.assertTrue(firewall.to_ufw_text("profiles=0\n").startswith("Status: inactive"))


class Names(unittest.TestCase):
    def test_names_windows_would_misread_are_refused(self):
        for bad in ("CON", "con.txt", "nul.tar.gz", "LPT1", "COM3.log", "notes.txt:hidden", "world.", "world ", "a<b", "a|b", "x\x01"):
            self.assertTrue(dirfd.name_problem(bad), bad)
        for good in ("server.properties", "world", "console.log", "com.example.jar", "auxiliary", ".hidden", "My World (1)"):
            self.assertIsNone(dirfd.name_problem(good), good)

    def test_each_server_has_its_own_account(self):
        a, b = _win.service_sid("minecraft-java"), _win.service_sid("minecraft-other")
        self.assertRegex(a, r"^S-1-5-80(-\d+){5}$")
        self.assertNotEqual(a, b)
        self.assertEqual(a, _win.service_sid("MINECRAFT-JAVA"))          # Windows ignores case in service names
        self.assertEqual(_win.service_sid("TrustedInstaller"),            # a well-known one, to check the sum itself
                         "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464")
        self.assertEqual(_win.service_account("minecraft-java"), "NT SERVICE\\minecraft-java")


class Preview(unittest.TestCase):
    """What the Windows preview doesn't do yet is refused in one sentence; reading still works."""

    def test_changes_outside_the_preview_are_refused_on_windows_only(self):
        try:
            import main
        except ImportError:
            self.skipTest("needs fastapi")
        for method, path in (("POST", "/api/settings/cluster"), ("POST", "/api/cluster/moves"),
                             ("PUT", "/api/settings/sftp"), ("PUT", "/api/servers/minecraft-a/resources"),
                             ("POST", "/api/panel/update"), ("POST", "/api/settings/backup-storage"),
                             ("PUT", "/api/servers/minecraft-a/offsite/t1"), ("POST", "/api/servers/modpack"),
                             ("POST", "/api/servers/minecraft-a/modpack/update"),
                             ("POST", "/api/servers/minecraft-a/phantom/install")):
            self.assertIn("Windows preview", main.not_yet(method, path, windows=True) or "", f"{method} {path}")
            self.assertIsNone(main.not_yet(method, path, windows=False), f"{method} {path}")
        for method, path in (("GET", "/api/settings/cluster"), ("POST", "/api/servers/deploy"),
                             ("POST", "/api/servers/minecraft-a/backups"), ("PUT", "/api/settings"),
                             ("PUT", "/api/settings/notifications"), ("POST", "/api/java/runtimes/install"),
                             ("GET", "/api/servers/minecraft-a/modpack")):
            self.assertIsNone(main.not_yet(method, path, windows=True), f"{method} {path}")

    def test_loaders_set_up_by_an_installer_are_refused_where_it_cannot_be_confined(self):
        from core import javaservers
        with mock.patch.object(javaservers.system, "can_run_as_game_account", return_value=False):
            for loader in ("quilt", "forge", "neoforge"):
                with self.assertRaises(javaservers.JavaServerError) as e:
                    javaservers.list_versions(loader)
                self.assertIn("Windows preview", str(e.exception))
            javaservers.require_installer_support("fabric")
            javaservers.require_installer_support("paper")

    def test_bedrock_is_refused_on_windows(self):
        from core import manager
        with mock.patch.object(manager.paths, "WINDOWS", True):
            self.assertFalse(manager.bedrock_supported())
            self.assertIn("Windows preview", manager.bedrock_unsupported_message())


if __name__ == "__main__":
    unittest.main()
