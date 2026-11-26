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


class Notifications(unittest.TestCase):
    """What a server's service leaves for the notification task, and what the task takes from it."""

    def setUp(self):
        from core.platform.windows import runner
        self.runner = runner
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        units_dir = os.path.join(self.tmp.name, "services")
        os.makedirs(units_dir)
        for unit in ("minecraft-java.service", "redstonepanel.service"):
            open(os.path.join(units_dir, unit), "w").close()
        p = mock.patch.multiple(paths, UNIT_DIR=units_dir, JOURNAL_DIR=os.path.join(self.tmp.name, "journal"))
        p.start()
        self.addCleanup(p.stop)
        self.record = {"kind": "stop", "requested": "0", "result": "exit-code", "code": "exited", "status": "1",
                       "invocation": "0" * 32}

    def test_a_record_is_found_once(self):
        r = self.runner
        r.note_event("minecraft-java.service", self.record)
        found = r.new_events({})
        self.assertEqual([(u, rec) for u, _, rec in found], [("minecraft-java.service", self.record)])
        self.assertEqual(r.new_events({"minecraft-java.service": found[0][1]}), [])

    def test_only_game_servers_and_only_what_a_runner_writes(self):
        r = self.runner
        r.note_event("redstonepanel.service", self.record)
        r.note_event("minecraft-java.service", dict(self.record, kind="rm -rf"))
        r.note_event("minecraft-java.service", dict(self.record, status="1; calc"))
        self.assertEqual([rec for _, _, rec in r.new_events({})], [None, None])

    def test_old_news_is_skipped_and_few_files_are_kept(self):
        r = self.runner
        for _ in range(r.EVENTS_KEPT + 5):
            r.note_event("minecraft-java.service", self.record)
        self.assertEqual(len(os.listdir(r.events_dir("minecraft-java.service"))), r.EVENTS_KEPT)
        late = r.new_events({}, now=__import__("time").time() + r.EVENT_MAX_AGE + 60)
        self.assertTrue(late and all(rec is None for _, _, rec in late))

    def test_the_task_carries_nothing_from_the_entry(self):
        xml = units.event_task_xml("python.exe", "runner.py event", "RedstonePanel", (10, 11), "x")
        self.assertIn("EventID=10 or EventID=11", xml)
        self.assertNotIn("$(", xml)
        self.assertNotIn("ValueQueries", xml)


class ServersFolder(unittest.TestCase):
    """What can be told from the path alone; the drive itself is looked at on Windows."""
    def setUp(self):
        from core.platform.windows import system
        self.system = system

    def test_paths_windows_would_misread_are_refused_with_a_reason(self):
        for bad in ("D:\\My Servers", "D:", "D:\\", "servers", "\\\\nas\\share\\mc", "D:\\mc\\..\\x", "D:\\mc;x",
                    "/srv/minecraft", "D:\\" + "a" * 120):
            clean, error = self.system.check_servers_folder(bad)
            self.assertIsNone(clean, bad)
            self.assertTrue(error, bad)

    def test_windows_own_folders_and_the_panels_are_refused(self):
        with mock.patch.object(self.system, "_drive_type", return_value=3), \
             mock.patch.object(self.system, "filesystem", return_value={"fstype": "NTFS"}), \
             mock.patch.object(self.system.os.path, "isdir", return_value=True), \
             mock.patch.object(self.system.os.path, "normcase", lambda p: p.lower()), \
             mock.patch.object(self.system.os.path, "abspath", lambda p: p), \
             mock.patch.object(self.system.os, "sep", "\\"), \
             mock.patch.object(self.system.os.path, "exists", return_value=False), \
             mock.patch.object(self.system.os.path, "lexists", return_value=False):
            for bad in ("C:\\Windows\\servers", "c:\\users\\quent\\servers", "C:\\Users"):
                clean, error = self.system.check_servers_folder(bad)
                self.assertIsNone(clean, bad)
                self.assertIn("belongs to Windows", error)
            self.assertEqual(self.system.check_servers_folder("d:/Minecraft/servers"), ("D:\\Minecraft\\servers", ""))

    def test_a_drive_that_cannot_hold_servers_is_refused(self):
        with mock.patch.object(self.system.os.path, "isdir", return_value=True), \
             mock.patch.object(self.system.os.path, "exists", return_value=False), \
             mock.patch.object(self.system.os.path, "lexists", return_value=False):
            with mock.patch.object(self.system, "_drive_type", return_value=2):
                self.assertIn("removable", self.system.check_servers_folder("E:\\servers")[1])
            with mock.patch.object(self.system, "_drive_type", return_value=1):
                self.assertIn("no drive", self.system.check_servers_folder("Q:\\servers")[1])
            with mock.patch.object(self.system, "_drive_type", return_value=3), \
                 mock.patch.object(self.system, "filesystem", return_value={"fstype": "exFAT"}):
                self.assertIn("NTFS", self.system.check_servers_folder("E:\\servers")[1])


class ServersDrives(unittest.TestCase):
    def test_fixed_ntfs_drives_with_the_same_folder_on_each(self):
        from core.platform.windows import system
        kinds = {"C:\\": 3, "D:\\": 3, "E:\\": 2, "F:\\": 3, "Z:\\": 4}
        volumes = {"C:\\": ("Windows", "NTFS"), "D:\\": ("Disk01", "NTFS"), "F:\\": ("Stick", "exFAT")}
        usage = mock.Mock(free=5 << 30, total=10 << 30)
        with mock.patch.object(system, "_drive_letters", return_value=["C", "D", "E", "F", "Z"]), \
             mock.patch.object(system, "_drive_type", side_effect=lambda m: kinds[m]), \
             mock.patch.object(system, "_volume", side_effect=lambda m: volumes[m]), \
             mock.patch.object(system.shutil, "disk_usage", return_value=usage), \
             mock.patch.object(system.os.path, "splitdrive", return_value=("C:", "\\ProgramData\\RedstonePanel\\servers")):
            drives = system.servers_drives()
        self.assertEqual([d["drive"] for d in drives], ["C:", "D:"])          # not removable, network or exFAT
        self.assertEqual(drives[0]["suggested"], "")                           # the panel's own drive: the usual place
        self.assertEqual(drives[1]["suggested"], "D:\\Minecraft\\servers")
        self.assertEqual(drives[1]["label"], "Disk01")
        self.assertEqual(drives[1]["free"], 5 << 30)

    def test_linux_has_no_list(self):
        from core.platform.linux import system
        self.assertEqual(system.servers_drives(), [])


class OtherPeoplesServices(unittest.TestCase):
    """A PC may already have services called minecraft-java that something else made."""

    def test_only_the_runners_services_are_the_panels(self):
        ours = r'"C:\Program Files\RedstonePanel\python\python.exe" -X utf8 ' \
               r'"C:\Program Files\RedstonePanel\app\core\platform\windows\runner.py" service minecraft-java.service'
        self.assertTrue(_win.is_ours({"command": ours}))
        for other in (r"C:\tools\nssm.exe", r'"C:\Users\me\Projects\mc\runner.py" service x', "", None):
            self.assertFalse(_win.is_ours({"command": other}), other)
        self.assertFalse(_win.is_ours(None))

    def test_a_service_of_that_name_is_never_changed(self):
        with mock.patch.object(_win, "service_config", return_value={"command": r"C:\tools\nssm.exe", "start": "auto"}), \
                mock.patch.object(_win, "_scm", side_effect=AssertionError("the service manager was opened")):
            with self.assertRaises(_win.NotOurs):
                _win.ensure_service("minecraft-java", "x", "x", "x")

    def test_stop_and_start_leave_it_alone(self):
        from core.platform.windows import services
        with mock.patch.object(_win, "service_config", return_value={"command": r"C:\tools\nssm.exe", "start": "auto"}), \
                mock.patch.object(_win, "stop_service", side_effect=AssertionError("stopped")), \
                mock.patch.object(_win, "start_service", side_effect=AssertionError("started")), \
                mock.patch.object(_win, "set_start", side_effect=AssertionError("changed")):
            for action in ("stop", "start", "restart", "enable", "disable"):
                self.assertNotEqual(services.control("minecraft-java.service", action, quiet=True).returncode, 0, action)

    def test_a_new_server_takes_another_name(self):
        from core import manager
        from core.platform import services
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(manager, "SERVERS_ROOT", tmp), \
                mock.patch.object(manager, "_used_ports", return_value=set()), \
                mock.patch.object(manager, "_unit_path", side_effect=lambda n: os.path.join(tmp, n + ".service")), \
                mock.patch.object(services, "name_free", side_effect=lambda n: n != "minecraft-java"):
            name, port = manager._reserve_name("java")
            manager._release_name(name)
        self.assertEqual(name, "minecraft-java-2")


class FoundServers(unittest.TestCase):
    """Servers somebody set up outside the panel (core/found.py), as Windows shows them."""

    def listing(self):
        # Quentin's own setup on Superman: a WinSW wrapper is the service, the server is its child
        return {"programs": [{"pid": 500, "name": "java.exe", "exe": "C:\\Java\\bin\\java.exe", "user": "SYSTEM", "started": 1010,
                              "command": '"C:\\Java\\bin\\java.exe" -Xmx4G -jar server.jar nogui'},
                             {"pid": 600, "name": "java.exe", "exe": "C:\\Java\\bin\\java.exe", "user": "quent", "started": 2000,
                              "command": "java -jar paper.jar"},
                             {"pid": 700, "name": "java.exe", "exe": "", "user": "", "started": 3000, "command": "java -jar x.jar"},
                             {"pid": 800, "name": "java.exe", "exe": "C:\\Java\\bin\\java.exe", "user": "", "started": 3000,
                              "command": "java -jar server.jar nogui"},
                             {"pid": 900, "name": "java.exe", "exe": "", "user": "", "started": 4000, "command": "java -jar server.jar"},
                             {"pid": 950, "name": "java.exe", "exe": "", "user": "", "started": 5000, "command": "java -jar server.jar"}],
                # 900: started by a script the service runs; 950: started from a scheduled task, a build
                # agent or an SSH session, whose service is NOT the server's (the panel would stop it)
                "parents": {500: (400, 1010, "java.exe"), 400: (4, 1000, "minecraft-java.exe"), 600: (650, 2000, "java.exe"),
                            650: (4, 2500, "nssm.exe"), 700: (4, 3000, "java.exe"), 800: (780, 3000, "java.exe"),
                            780: (4, 2990, "python.exe"), 900: (890, 4000, "java.exe"), 890: (880, 3990, "cmd.exe"),
                            880: (4, 3980, "nssm.exe"), 950: (940, 5000, "java.exe"), 940: (930, 4990, "python.exe"),
                            930: (920, 4980, "pwsh.exe"), 920: (4, 4000, "runner.worker.exe")},
                "services": {400: "minecraft-java", 650: "other", 780: "minecraft-java-2", 880: "mc-nssm", 920: "actions.runner"}}

    def test_services_named_for_minecraft_that_are_not_the_panels(self):
        from core.platform.windows import system
        listed = [{"name": "minecraft-java2", "path": '"C:\\Users\\quent\\Projects\\minecraft-java2\\minecraft-java2.exe"', "state": "Stopped"},
                  {"name": "phantom-minecraft-bedrock", "path": "C:\\phantom\\phantom-windows.exe -server 1.2.3.4:19132", "state": "Running"},
                  {"name": "minecraft-java-2", "state": "Running",
                   "path": '"C:\\Program Files\\RedstonePanel\\python\\python.exe" "C:\\Program Files\\RedstonePanel\\app\\core\\platform\\windows\\runner.py" service minecraft-java-2'}]
        with mock.patch.object(_win, "services_like", return_value=listed) as asked:
            got = system.other_game_services()
        self.assertEqual(asked.call_args[0][0], "minecraft|bedrock")
        self.assertEqual([(g["service"], g["running"], g["folder"]) for g in got],                # the panel's own is left out
                         [("minecraft-java2", False, "C:\\Users\\quent\\Projects\\minecraft-java2"),
                          ("phantom-minecraft-bedrock", True, "C:\\phantom")])

    def test_a_wrapped_server_is_found_under_its_service(self):
        from core.platform.windows import system
        folders = {500: "C:\\Users\\quent\\Projects\\minecraft-java", 600: "D:\\mc", 800: "D:\\minecraft\\servers\\minecraft-java-2",
                   900: "D:\\nssm-mc", 950: "D:\\ci"}
        with mock.patch.object(_win, "program_list", return_value=self.listing()) as asked, \
             mock.patch.object(_win, "process_cwd", side_effect=lambda pid: folders.get(pid, "")):
            got = {g["pid"]: g for g in system.game_programs()}
        self.assertEqual(asked.call_args[0][0], ["java.exe", "bedrock_server.exe", "javaw.exe"])
        self.assertEqual(sorted(got), [500, 600, 700, 800, 900, 950])
        self.assertEqual(got[700]["cwd"], "")                                # its folder can't be read: noted, never handed over
        self.assertEqual(got[900]["service"], "mc-nssm")                     # through the service's start script
        self.assertEqual(got[950]["service"], "")                            # under a build agent: not its server
        self.assertEqual((got[500]["service"], got[500]["cwd"], got[500]["user"]),
                         ("minecraft-java", "C:\\Users\\quent\\Projects\\minecraft-java", "SYSTEM"))
        self.assertEqual(got[500]["argv"], ["C:\\Java\\bin\\java.exe", "-Xmx4G", "-jar", "server.jar", "nogui"])
        # Its "parent" started after it did: a number Windows used again, not the service that runs it
        self.assertEqual(got[600]["service"], "")
        self.assertEqual(got[800]["service"], "minecraft-java-2")

    def test_the_folder_it_is_in(self):
        from core.platform.windows import system
        with mock.patch.object(system, "_drive_type", return_value=3):
            self.assertEqual(system.found_folder_problem("C:\\Users\\quent\\Projects\\minecraft-java"), "")
            self.assertIn("space", system.found_folder_problem("C:\\My Servers\\survival"))
            self.assertIn("space", system.found_folder_problem("\\\\nas\\mc"))
        with mock.patch.object(system, "_drive_type", return_value=2):
            self.assertIn("removable", system.found_folder_problem("E:\\mc"))

    def test_only_somebody_elses_service_is_stopped_this_way(self):
        from core.platform.windows import services
        theirs = {"start": "auto", "command": "C:\\mc\\minecraft-java.exe", "account": "LocalSystem"}
        ours = {"start": "auto", "command": 'python.exe "C:\\Program Files\\RedstonePanel\\core\\platform\\windows\\runner.py" service minecraft-java-2.service',
                "account": "x"}
        with mock.patch.object(services, "_log"), mock.patch.object(_win, "stop_service") as stop, \
             mock.patch.object(_win, "start_service") as start, mock.patch.object(_win, "set_start") as set_start:
            with mock.patch.object(_win, "service_config", return_value=theirs):
                self.assertTrue(services.other_enabled("minecraft-java"))
                services.other_control("minecraft-java", "stop")
                services.other_control("minecraft-java", "disable")
                services.other_control("minecraft-java", "enable")
                services.other_control("minecraft-java", "start")
                with self.assertRaises(ValueError):
                    services.other_control("minecraft-java", "delete")
            self.assertEqual([c[0] for c in set_start.call_args_list], [("minecraft-java", "manual"), ("minecraft-java", "auto")])
            stop.assert_called_once_with("minecraft-java")
            start.assert_called_once_with("minecraft-java")
            with mock.patch.object(_win, "service_config", return_value=ours):
                with self.assertRaises(OSError):
                    services.other_control("minecraft-java-2", "stop")
            windows = {"start": "auto", "command": "C:\\Windows\\system32\\svchost.exe -k netsvcs -p", "account": "LocalSystem"}
            with mock.patch.object(_win, "service_config", return_value=windows), \
                 mock.patch.dict(os.environ, {"SystemRoot": "C:\\Windows"}):
                with self.assertRaises(OSError):                             # Task Scheduler and the like: never
                    services.other_control("Schedule", "stop")
            stop.assert_called_once_with("minecraft-java")
            with mock.patch.object(_win, "service_config", return_value=None):
                self.assertFalse(services.other_enabled("gone"))
                with self.assertRaises(OSError):
                    services.other_control("gone", "start")


class NoUnixOnlyCalls(unittest.TestCase):
    """Windows' Python has no os.major, os.chown and the like: a call outside core/platform must
    be guarded, or the page it serves answers HTTP 500 there (the Performance card did)."""

    def test_disk_kind_is_unknown_without_device_numbers(self):
        from core import perf
        with mock.patch.object(perf.os, "major", create=True) as major:
            del perf.os.major
            self.assertIsNone(perf.rotational(os.getcwd()))
        self.assertTrue(major is not None)


class Limits(unittest.TestCase):
    """1.23: hard limits are Job Object limits read from the same drop-in systemd reads."""

    def test_the_drop_in_values(self):
        from core.platform.windows import jobs
        self.assertEqual(jobs.memory_bytes("3221225472"), 3 << 30)
        self.assertEqual(jobs.memory_bytes("3G"), 3 << 30)
        self.assertEqual(jobs.memory_bytes("1536M"), 1536 << 20)
        self.assertIsNone(jobs.memory_bytes("infinity"))
        self.assertIsNone(jobs.memory_bytes(""))
        self.assertEqual(jobs.cpu_cores("150%"), 1.5)
        self.assertIsNone(jobs.cpu_cores(""))
        self.assertIsNone(jobs.cpu_cores("0%"))

    def test_processor_rate_is_a_share_of_the_whole_machine(self):
        from core.platform.windows import jobs
        self.assertEqual(jobs.cpu_rate(1.5, 4), 3750)
        self.assertEqual(jobs.cpu_rate(0.01, 64), 2)
        self.assertEqual(jobs.cpu_rate(0.0001, 64), 1)
        self.assertIsNone(jobs.cpu_rate(4, 4))          # the whole machine: no cap
        self.assertIsNone(jobs.cpu_rate(None, 4))
        self.assertEqual(jobs.systemd_quota(1.5), "1.500000s")
        self.assertEqual(jobs.systemd_quota(None), "infinity")

    def test_the_runner_reads_them_from_the_limits_drop_in(self):
        from core import perf
        from core.platform.windows import jobs
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "minecraft-java.service"), "w") as f:
                f.write("[Service]\nExecStart=java -jar server.jar\n")
            os.makedirs(os.path.join(d, "minecraft-java.service.d"))
            with open(os.path.join(d, "minecraft-java.service.d", perf.DROPIN_NAME), "w") as f:
                f.write(perf.dropin_text(3 << 30, 1.5))
            spec = units.load("minecraft-java.service", d)
            self.assertEqual(jobs.limits_from_spec(spec, units.get), (3 << 30, 1.5))

    def test_a_job_is_named_for_its_server_and_closed_to_the_game(self):
        from core.platform.windows import jobs
        self.assertEqual(jobs.job_name("minecraft-java.service"), "Global\\RedstonePanel.job.minecraft-java")
        self.assertEqual(jobs.JOB_SDDL, "D:P(A;;GA;;;SY)(A;;GA;;;BA)")    # the system and administrators only

    def test_reaching_the_limit(self):
        from core.platform.windows import jobs
        self.assertTrue(jobs.at_limit(1 << 30, (1 << 30) - (1 << 20)))
        self.assertFalse(jobs.at_limit(1 << 30, 512 << 20))
        self.assertFalse(jobs.at_limit(None, 1 << 30))

    def test_a_stop_at_the_limit_is_news_the_notification_task_takes(self):
        from core.platform.windows import runner
        ok = {"kind": "stop", "requested": "0", "result": "oom-kill", "code": "exited", "status": "1",
              "invocation": "a" * 32}
        self.assertTrue(runner._valid_event(ok))
        self.assertFalse(runner._valid_event(dict(ok, result="whatever")))


class NotAPreview(unittest.TestCase):
    """1.24: clusters and SFTP came to Windows, so nothing is refused there any more for being a
    preview (the terminal is Linux-only by design, through terminal.supported())."""

    def test_nothing_is_refused_as_not_yet(self):
        try:
            import main
        except ImportError:
            self.skipTest("needs fastapi")
        self.assertFalse(hasattr(main, "WINDOWS_NOT_YET"))
        self.assertFalse(hasattr(main, "not_yet"))
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "js",
                               "app.js"), encoding="utf-8") as f:
            page = f.read()
        self.assertNotIn("notYet(", page)
        self.assertNotIn("Windows preview", page)

    def test_loaders_set_up_by_an_installer_are_refused_where_it_cannot_be_confined(self):
        from core import javaservers
        with mock.patch.object(javaservers.system, "can_run_as_game_account", return_value=False):
            for loader in ("quilt", "forge", "neoforge"):
                with self.assertRaises(javaservers.JavaServerError) as e:
                    javaservers.list_versions(loader)
                self.assertIn("can't be run as the game account", str(e.exception))
            javaservers.require_installer_support("fabric")
            javaservers.require_installer_support("paper")

    def test_bedrock_runs_on_x64_windows_only(self):
        from core import manager
        with mock.patch.object(manager.paths, "WINDOWS", True):
            self.assertTrue(manager.bedrock_supported("AMD64"))
            self.assertFalse(manager.bedrock_supported("ARM64"))
            self.assertIn("x64) Windows", manager.bedrock_unsupported_message())


class Installers(unittest.TestCase):
    """1.23: Quilt, Forge and NeoForge installers run as the server's own account, through its
    service, once."""

    def test_a_marked_command_runs_through_the_servers_service(self):
        from core.platform.windows import services, system
        cmd = system.as_game_account(["java", "-jar", "installer.jar", "--installServer", "D:\\s\\minecraft-a"])
        self.assertTrue(system.can_run_as_game_account())
        self.assertEqual(cmd[0], system.AS_SERVER)
        seen = {}

        def fake_run(server, argv, cwd, env, timeout):
            seen.update(server=server, argv=argv, cwd=cwd, env=env, timeout=timeout)
            return "done"
        with mock.patch.object(system, "_server_for", return_value="minecraft-a"), \
                mock.patch.object(services, "run_as_server", fake_run), \
                mock.patch.dict(os.environ, {"KEEP": "same"}):
            r = system.run_program(cmd, cwd="D:\\s\\minecraft-a", env={"KEEP": "same", "HOME": "D:\\s\\minecraft-a"},
                                   timeout=900)
        self.assertEqual(r, "done")
        self.assertEqual(seen["server"], "minecraft-a")
        self.assertEqual(seen["argv"][:2], ["java", "-jar"])
        self.assertEqual(seen["env"], {"HOME": "D:\\s\\minecraft-a"})       # only what differs
        with mock.patch.object(system, "_server_for", return_value=None):
            self.assertEqual(system.run_program(cmd, cwd="C:\\Windows").returncode, 1)

    def test_the_task_rides_on_a_command_line_that_is_still_the_panels(self):
        from core.platform.windows import runner, services
        task = {"argv": ["java", "-jar", "x.jar"], "cwd": "D:\\s\\minecraft-a", "env": {"TEMP": "t"}, "timeout": 60}
        line = services.task_command("minecraft-a.service", task)
        self.assertIn(" service minecraft-a.service --task ", line)
        self.assertTrue(_win.is_ours({"command": line}))
        self.assertEqual(runner.read_task(line.rsplit(" ", 1)[1]), task)
        with self.assertRaises(ValueError):
            runner.read_task("e30=")                               # {} is not a task

    def test_forge_starts_from_its_windows_arguments_file(self):
        from core import javaservers
        with tempfile.TemporaryDirectory() as d:
            base = os.path.join(d, "libraries", "net", "minecraftforge", "forge", "1.21.1-52.0.1")
            os.makedirs(base)
            for n in ("unix_args.txt", "win_args.txt"):
                open(os.path.join(base, n), "w").close()
            info = {"loader": "forge", "launch": "@libraries/net/minecraftforge/forge/1.21.1-52.0.1/unix_args.txt"}
            self.assertTrue(javaservers.launch_args(d, info).endswith("/unix_args.txt"))
            with mock.patch.object(javaservers.paths, "WINDOWS", True):
                self.assertTrue(javaservers.launch_args(d, info).endswith("/win_args.txt"))
                self.assertTrue(os.path.isfile(javaservers.launch_file(d, info)))

    def test_copying_as_the_system_never_goes_through_a_link(self):
        from core.platform.windows import system
        with tempfile.TemporaryDirectory() as t:
            src, dest, outside = (os.path.join(t, n) for n in ("src", "dest", "outside"))
            os.makedirs(os.path.join(src, "mods"))
            open(os.path.join(src, "mods", "a.jar"), "w").close()
            os.makedirs(dest)
            os.makedirs(outside)
            os.symlink(outside, os.path.join(dest, "mods"))
            r = system.copy_into(src, dest)
            self.assertEqual(r.returncode, 1)
            self.assertEqual(os.listdir(outside), [])
            open(os.path.join(outside, "keep.txt"), "w").close()
            system.remove_files(dest, [os.path.join("mods", "keep.txt")])
            self.assertTrue(os.path.exists(os.path.join(outside, "keep.txt")))


class OffsiteOverSftp(unittest.TestCase):
    """1.23: SSH storage from Windows uses sftp (Windows has ssh and sftp, not rsync)."""

    def test_upload_resumes_a_part_with_reput_and_download_uses_get(self):
        from core import backup_targets as bt
        t = {"type": "ssh", "host": "nas.lan", "port": 2222, "user": "rp", "folder": "~/backups"}
        target, batches, calls = bt.SSHTarget(t), [], []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            if cmd[0] == bt.SFTP:
                with open(cmd[cmd.index("-b") + 1]) as f:
                    batches.append(f.read())
                return mock.Mock(returncode=0, stdout="", stderr="")
            text = cmd[-1]
            out = "PARTIAL\n" if "-f" in text and ".partial" in text else ""
            return mock.Mock(returncode=0, stdout=out, stderr="")
        with tempfile.TemporaryDirectory() as d, mock.patch.object(bt, "USE_SFTP", True), \
                mock.patch.object(bt, "SSH_DIR", d), mock.patch.object(bt, "ensure_key", return_value="k"), \
                mock.patch.object(bt.subprocess, "run", fake_run):
            target.upload("C:\\RP\\backups\\minecraft-a\\b.tar.gz", "pc-1a2b", "minecraft-a", "b.tar.gz", "0" * 64, 10)
            target.download("pc-1a2b", "minecraft-a", "b.tar.gz", "C:\\RP\\tmp\\b.tar.gz")
        self.assertIn('reput "C:/RP/backups/minecraft-a/b.tar.gz" "backups/pc-1a2b/minecraft-a/b.tar.gz.partial"', batches[0])
        self.assertIn("chmod 600", batches[0])
        self.assertIn('get "backups/pc-1a2b/minecraft-a/b.tar.gz" "C:/RP/tmp/b.tar.gz"', batches[1])
        sftp = next(c for c in calls if c[0] == bt.SFTP)
        self.assertIn("-P", sftp)                     # sftp's port option
        self.assertNotIn("-p", sftp)
        self.assertFalse(any(c[0] == bt.RSYNC for c in calls))

    def test_the_openssh_client_that_comes_with_windows(self):
        layout = paths.windows_layout("C:\\RP", environ={"SystemRoot": "C:\\Windows"})
        self.assertTrue(layout["SSH_PROGRAM"].replace("/", "\\").lower().endswith("system32\\openssh\\ssh.exe"))
        self.assertTrue(layout["SFTP_PROGRAM"].lower().endswith("sftp.exe"))


class Bedrock(unittest.TestCase):
    """1.23: Bedrock Dedicated Server's Windows build, from the same download-links API."""
    LINKS = {"result": {"links": [
        {"downloadType": "serverBedrockLinux",
         "downloadUrl": "https://www.minecraft.net/bedrockdedicatedserver/bin-linux/bedrock-server-1.26.66.1.zip"},
        {"downloadType": "serverBedrockWindows",
         "downloadUrl": "https://www.minecraft.net/bedrockdedicatedserver/bin-win/bedrock-server-1.26.66.1.zip"}]}}

    def test_the_windows_download_is_picked_on_windows(self):
        from core import updates
        win = updates.parse_bedrock_links(self.LINKS, "serverBedrockWindows")
        self.assertIn("/bin-win/", win["url"])
        self.assertEqual(win["version"], "1.26.66.1")
        self.assertIn("/bin-linux/", updates.parse_bedrock_links(self.LINKS, "serverBedrockLinux")["url"])
        self.assertEqual(paths.windows_layout("C:\\RP")["BEDROCK_DOWNLOAD"], "serverBedrockWindows")
        self.assertEqual(paths.windows_layout("C:\\RP")["BEDROCK_PROGRAM"], "bedrock_server.exe")

    def test_a_folder_from_either_system_is_a_bedrock_server(self):
        from core import manager
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(manager.is_bedrock_folder(d))
            open(os.path.join(d, "bedrock_server.exe"), "w").close()
            self.assertTrue(manager.is_bedrock_folder(d))
            self.assertFalse(manager._java_properties(d))
            with mock.patch.object(manager.paths, "BEDROCK_PROGRAM", "bedrock_server.exe"):
                self.assertTrue(manager.has_bedrock_program(d))
            self.assertFalse(manager.has_bedrock_program(d))          # Linux needs its own program

    def test_the_console_is_logged_to_server_log_for_bedrock_only(self):
        from core.platform.windows import runner
        with tempfile.TemporaryDirectory() as d:
            def program(exe):
                spec = units.parse(f"[Service]\nWorkingDirectory={d}\nExecStart={os.path.join(d, exe)}\n")
                return runner.Program("minecraft-x.service", spec, runner.Journal("minecraft-x.service", d))
            log = program("bedrock_server.exe")._console_log()
            self.assertIsNotNone(log)
            log.close()
            self.assertIsNone(program("java.exe")._console_log())

    def test_phantom_has_a_pinned_windows_build(self):
        from core import phantom
        name, sha = phantom.WINDOWS_ASSETS["AMD64"]
        self.assertEqual(name, "phantom-windows.exe")
        self.assertEqual(len(sha), 64)
        with mock.patch.object(phantom.paths, "WINDOWS", True), mock.patch.object(phantom.platform, "machine",
                                                                                  return_value="AMD64"):
            self.assertTrue(phantom.machine_supported())
            self.assertTrue(phantom.binary_path().endswith(".exe"))


class PanelUpdate(unittest.TestCase):
    """1.23: the panel updates itself from the MSI on the GitHub release, checked by SHA-256."""
    MSI = "RedstonePanel-1.23.0-x64.msi"

    def release(self, **extra):
        msi = {"name": self.MSI, "browser_download_url": f"https://github.com/qrussell/redstonepanel/releases/download/v1.23.0/{self.MSI}",
               "size": 40 << 20}
        msi.update(extra)
        return {"tag_name": "v1.23.0", "html_url": "https://github.com/qrussell/redstonepanel/releases/tag/v1.23.0",
                "assets": [{"name": "redstonepanel_1.23.0_all.deb", "browser_download_url": "https://github.com/x.deb"}, msi,
                           {"name": self.MSI + ".sha256", "browser_download_url": msi["browser_download_url"] + ".sha256"}]}

    def setUp(self):
        from core.platform.windows import panelpkg
        self.p = panelpkg
        self.d = tempfile.mkdtemp(dir=TMP)
        patcher = mock.patch.object(panelpkg, "folder", return_value=self.d)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_release_must_have_an_installer_and_its_checksum(self):
        info = self.p.parse_release(self.release(digest="sha256:" + "AB" * 32))
        self.assertEqual(info["version"], "1.23.0")
        self.assertEqual(info["digest"], "ab" * 32)
        r = self.release()
        r["assets"] = r["assets"][:2]                       # no .sha256
        self.assertIsNone(self.p.parse_release(r))
        self.assertIsNone(self.p.parse_release(dict(self.release(), prerelease=True)))
        self.assertIsNone(self.p.parse_release({"assets": [{"name": "RedstonePanel-1.23.0-x64.msi.exe"}]}))

    def test_github_is_asked_once_a_day(self):
        calls = []
        fetch = lambda: calls.append(1) or self.release()            # noqa: E731
        self.assertEqual(self.p.latest(fetch=fetch)["version"], "1.23.0")
        self.assertEqual(self.p.latest(fetch=fetch)["version"], "1.23.0")
        self.assertEqual(len(calls), 1)
        self.p.latest(refresh=True, fetch=fetch)
        self.assertEqual(len(calls), 2)

        def down():
            raise OSError("offline")
        self.assertEqual(self.p.latest(fetch=down)["version"], "1.23.0")      # the saved answer
        with self.assertRaises(OSError):
            self.p.latest(refresh=True, fetch=down)                          # Check for updates says so

    def test_checksum_lines(self):
        h = "0123456789abcdef" * 4
        self.assertEqual(self.p.expected_sha256(f"{h}  {self.MSI}\n", self.MSI), h)
        self.assertEqual(self.p.expected_sha256(f"{h.upper()} *{self.MSI}", self.MSI), h)
        self.assertIsNone(self.p.expected_sha256(f"{h}  other.msi", self.MSI))
        self.assertIsNone(self.p.expected_sha256("not a hash", self.MSI))

    def test_only_github_addresses(self):
        for url in ("http://github.com/x.msi", "https://example.com/x.msi", "https://github.com.evil.net/x.msi"):
            with self.assertRaises(self.p.Problem):
                self.p._open(url)

    def fake_open(self, body):
        import hashlib
        import io
        good = hashlib.sha256(body).hexdigest()

        class R(io.BytesIO):
            def __enter__(self):
                return self

        def opener(sums):
            def _open(url, timeout=60):
                return R(sums.encode() if url.endswith(".sha256") else body)
            return _open
        return good, opener

    def test_a_download_that_does_not_match_is_refused(self):
        good, opener = self.fake_open(b"MSI" * 1000)
        info = self.p.parse_release(self.release())
        with mock.patch.object(self.p, "_open", opener(f"{'0' * 64}  {self.MSI}")):
            with self.assertRaises(self.p.Problem):
                self.p.download(info, out=lambda *_: None)
        self.assertEqual(os.listdir(self.d), [])
        with mock.patch.object(self.p, "_open", opener(f"{good}  {self.MSI}")):
            with self.assertRaises(self.p.Problem):                         # GitHub's digest disagrees
                self.p.download(dict(info, digest="1" * 64), out=lambda *_: None)
            path = self.p.download(dict(info, digest=good), out=lambda *_: None)
        self.assertEqual(os.path.basename(path), self.MSI)

    def test_install_starts_the_installer_on_its_own_and_restarts_what_ran(self):
        from core.platform.windows import packages
        good, opener = self.fake_open(b"MSI" * 1000)
        spawned, lines = [], []
        with mock.patch.object(self.p, "_open", opener(f"{good}  {self.MSI}")), \
                mock.patch.object(self.p, "latest", return_value=self.p.parse_release(self.release())), \
                mock.patch.object(packages, "installed_version", return_value="1.22.0"):
            self.assertTrue(self.p.install(out=lines.append, spawn=lambda argv, cwd=None: spawned.append(argv),
                                           servers=["minecraft-java", "phantom-minecraft-bedrock"]))
        self.assertTrue(spawned[0][0].endswith("powershell.exe"))
        with open(spawned[0][-1], encoding="utf-8-sig") as f:
            script = f.read()
        self.assertIn("msiexec.exe", script)
        self.assertIn("'/qn'", script)
        self.assertIn(self.MSI, script)
        self.assertIn("Start-Service -Name 'redstonepanel'", script)
        self.assertIn("'minecraft-java', 'phantom-minecraft-bedrock'", script)
        self.assertIn("result.json", script)
        self.assertTrue(any(l.startswith("Setting up redstonepanel (1.23.0)") for l in lines))
        with mock.patch.object(self.p, "latest", return_value=self.p.parse_release(self.release())), \
                mock.patch.object(packages, "installed_version", return_value="1.23.0"):
            self.assertFalse(self.p.install(out=lines.append, spawn=lambda *a, **k: self.fail("spawned")))

    def test_the_candidate_is_the_newest_release(self):
        from core.platform.windows import packages
        with mock.patch.object(packages, "installed_version", return_value="1.22.0"), \
                mock.patch.object(self.p, "candidate", return_value="1.23.0"):
            self.assertIn("Candidate: 1.23.0", packages.policy("redstonepanel"))
        with mock.patch.object(packages, "installed_version", return_value="1.23.1"), \
                mock.patch.object(self.p, "candidate", return_value="1.23.0"):
            self.assertIn("Candidate: 1.23.1", packages.policy("redstonepanel"))

    def test_the_new_panel_reads_the_installers_outcome(self):
        import json
        import time
        from core import selfupdate
        now = int(time.time())
        st = {"state": "running", "from": "1.22.0", "to": "1.23.0", "installer": now - 5, "started": now - 60}
        with mock.patch.object(selfupdate, "_read_state", return_value=dict(st)), \
                mock.patch.object(selfupdate, "_write_state"), mock.patch.object(selfupdate.manager, "write_log"), \
                mock.patch.object(selfupdate, "_installed_version", return_value="1.23.0"):
            self.assertEqual(selfupdate.last_upgrade()["state"], "running")       # still installing
            with open(os.path.join(self.d, "result.json"), "w") as f:
                json.dump({"code": 0, "finished": now}, f)
            self.assertEqual(selfupdate.last_upgrade()["state"], "done")
            with open(os.path.join(self.d, "result.json"), "w") as f:
                json.dump({"code": 1603, "finished": now}, f)
        with mock.patch.object(selfupdate, "_read_state", return_value=dict(st)), \
                mock.patch.object(selfupdate, "_write_state"), mock.patch.object(selfupdate.manager, "write_log"), \
                mock.patch.object(selfupdate, "_installed_version", return_value="1.22.0"):
            out = selfupdate.last_upgrade()
        self.assertEqual(out["state"], "failed")
        self.assertIn("code 1603", out["error"])


class WindowsAccounts(unittest.TestCase):
    """1.23: Windows accounts sign in (Administrators or a RedstonePanel group), under one spelling."""

    def test_account_names(self):
        from core.platform.windows import system as w
        self.assertEqual(w._account_parts("quent"), (None, "quent"))
        self.assertEqual(w._account_parts(".\\quent"), (None, "quent"))
        self.assertEqual(w._account_parts("SUPERMAN\\quent"), ("SUPERMAN", "quent"))
        self.assertEqual(w._account_parts("someone@outlook.com"), ("MicrosoftAccount", "someone@outlook.com"))
        for bad in ("", "a/b", "x" * 80, "A\\b\\c", "a:b", "a*"):
            self.assertIsNone(w._account_parts(bad), bad)
        self.assertFalse(w.check_system_password("a/b", "pw"))
        self.assertFalse(w.check_system_password("quent", ""))

    def test_the_linux_groups_stand_for_administrators_and_redstonepanel(self):
        from core.platform.windows import system as w
        with mock.patch.object(w, "_sid_name", return_value="Administratoren"):
            self.assertEqual(w.allowed_groups(("sudo", "redstonepanel")), {"administratoren", "redstonepanel"})

    def test_group_answers_are_kept_briefly(self):
        from core.platform.windows import system as w
        w._groups_cache.clear()
        calls = []
        with mock.patch.object(w, "allowed_groups", return_value={"administrators"}), \
                mock.patch.object(w, "local_groups", side_effect=lambda u: calls.append(u) or {"Administrators"}):
            self.assertTrue(w.system_account_allowed("Quent"))
            self.assertTrue(w.system_account_allowed("quent"))
        self.assertEqual(len(calls), 1)
        w._groups_cache.clear()
        with mock.patch.object(w, "allowed_groups", return_value={"administrators"}), \
                mock.patch.object(w, "_lookup", return_value=None), \
                mock.patch.object(w, "local_groups", side_effect=lambda u: calls.append(u) or set()):
            self.assertFalse(w.system_account_allowed("new"))
            self.assertFalse(w.system_account_allowed("new"))      # a no is asked again
        self.assertEqual(len(calls), 3)
        w._groups_cache.clear()

    def test_a_session_and_two_factor_key_on_one_spelling(self):
        from core import auth
        with mock.patch.object(auth.system, "system_account_exists", return_value=True), \
                mock.patch.object(auth.system, "canonical_account", return_value="quent"), \
                mock.patch.object(auth.system, "system_account_allowed", return_value=True), \
                mock.patch.object(auth.system, "check_system_password", return_value=True), \
                mock.patch.object(auth, "_load_users", return_value=[]):
            who = auth.login("QUENT", "pw", "203.0.113.9")
            self.assertEqual(who["u"], "quent")
            self.assertEqual(auth.find_account(".\\Quent"), ("linux", "quent"))


class RouterAndPanelRule(unittest.TestCase):
    """1.23, found on a real PC: the router's address on Windows, and the panel's firewall rule
    covering HTTPS."""

    def test_the_default_route_with_the_lowest_metric(self):
        from core.platform.windows import system as w
        rows = [("10.0.0.0", "255.255.255.0", "0.0.0.0", 25), ("0.0.0.0", "0.0.0.0", "10.0.0.1", 35),
                ("0.0.0.0", "0.0.0.0", "192.168.56.1", 281), ("0.0.0.0", "0.0.0.0", "0.0.0.0", 5)]
        self.assertEqual(w.pick_gateway(rows), "10.0.0.1")
        self.assertIsNone(w.pick_gateway([("10.0.0.0", "255.255.255.0", "0.0.0.0", 25)]))

    def test_portforward_asks_the_platform(self):
        from core import portforward
        with mock.patch.object(platform.area("system"), "default_gateway", lambda: "10.0.0.1", create=True):
            self.assertEqual(portforward.default_gateway(), "10.0.0.1")

    def test_the_panel_rule_opens_http_and_https(self):
        script = firewall.panel_rule_script([8080, 8443])
        self.assertIn("-LocalPort 8080,8443", script)
        self.assertIn("'RedstonePanel web panel (8080, 8443/tcp)'", script)
        self.assertTrue(script.startswith("if (Get-NetFirewallRule -Group 'RedstonePanel panel'"))
        self.assertNotIn("if (", firewall.panel_rule_script([8080], only_if_present=False))

    def test_setup_and_settings_use_it(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "packaging", "windows", "setup.py"), encoding="utf-8") as f:
            self.assertIn("firewall.panel_rule_script(ports, only_if_present=False)", f.read())
        with open(os.path.join(here, "main.py"), encoding="utf-8") as f:
            self.assertIn("firewall.panel_ports(", f.read())


class MachineNotes(unittest.TestCase):
    """1.23: reserved port ranges (Hyper-V, WSL, Docker), PC sleep and a pending Windows Update restart."""
    NETSH = """
Protocol tcp Port Exclusion Ranges

Start Port    End Port
----------    --------
      5357        5357
     50000       50059     *
     25500       25599

* - Administered port exclusions.
"""
    POWERCFG = """    Power Setting GUID: 29f6c1db-86da-48c5-9fdb-f2b67b1f44da  (Sleep after)
      Current AC Power Setting Index: 0x00000708
      Current DC Power Setting Index: 0x00000384
"""

    def test_reading_netsh_and_powercfg(self):
        from core.platform.windows import system as w
        self.assertEqual(w.parse_excluded_ranges(self.NETSH), [(5357, 5357), (50000, 50059), (25500, 25599)])
        self.assertEqual(w.parse_powercfg_minutes(self.POWERCFG), (30, 15))
        self.assertEqual(w.parse_powercfg_minutes(""), (None, None))

    def test_a_port_inside_a_reserved_range(self):
        from core import portforward
        reserved = [("tcp", 25500, 25599), ("udp", 19100, 19199)]
        self.assertEqual(portforward.reserved_clash("25565", "tcp", reserved), (25500, 25599))
        self.assertIsNone(portforward.reserved_clash("25565", "udp", reserved))
        self.assertEqual(portforward.reserved_clash("19000:19132", "udp", reserved), (19100, 19199))
        self.assertIsNone(portforward.reserved_clash("25600", "tcp", reserved))
        self.assertIn("25500–25599", portforward.reserved_text("25565", "tcp", (25500, 25599)))

    def test_sleep_and_a_pending_restart_need_attention(self):
        from core.platform.windows import system as w
        w._machine_cache.clear()
        with mock.patch.object(w, "_sleep_minutes", return_value=30), mock.patch.object(w, "_restart_pending", return_value=True):
            notes = w.machine_notes()
        self.assertEqual([n["where"] for n in notes], ["power", "windows-update"])
        self.assertIn("after 30 minutes", notes[0]["text"])
        w._machine_cache.clear()
        with mock.patch.object(w, "_sleep_minutes", return_value=0), mock.patch.object(w, "_restart_pending", return_value=False):
            self.assertEqual(w.machine_notes(), [])
        w._machine_cache.clear()


class OpenLink(unittest.TestCase):
    """1.24: the installer's Finish page and the Start menu open RedstonePanel.url next to the
    program, which the panel writes on every start."""

    def test_the_link_points_at_the_panel_and_follows_a_change(self):
        from core.platform.windows import system as w
        with tempfile.TemporaryDirectory() as top:
            path = w.write_open_link("https://localhost:8443/", folder=top)
            self.assertEqual(path, os.path.join(top, "RedstonePanel.url"))
            with open(path, encoding="utf-8", newline="") as f:
                text = f.read()
            self.assertTrue(text.startswith("[InternetShortcut]\r\nURL=https://localhost:8443/\r\n"))
            self.assertIn("IconFile=" + os.path.join(top, "app", "static", "img", "favicon.ico"), text)
            w.write_open_link("http://localhost:8080/", folder=top)
            with open(path, encoding="utf-8") as f:
                self.assertIn("URL=http://localhost:8080/", f.read())
            self.assertEqual(sorted(os.listdir(top)), ["RedstonePanel.url"])

    def test_a_checkout_writes_nothing(self):
        from core.platform.windows import system as w
        self.assertIsNone(w._program_folder())          # tests run from a checkout, not Program Files
        self.assertIsNone(w.write_open_link("http://localhost:8080/"))
        for side in ("linux", "fake"):
            mod = importlib.import_module(f"core.platform.{side}.system")
            self.assertIsNone(mod.write_open_link("http://localhost:8080/"))

    def test_the_address_on_this_computer(self):
        import run
        self.assertEqual(run.local_url("http", 8080), "http://localhost:8080/")
        self.assertEqual(run.local_url("https", 8443), "https://localhost:8443/")
        self.assertEqual(run.local_url("https", 443), "https://localhost/")

    def test_the_installer_has_the_start_menu_entry_and_the_finish_box(self):
        with open(os.path.join(ROOT, "packaging", "windows", "redstonepanel.wxs"), encoding="utf-8") as f:
            wxs = f.read()
        self.assertIn('Target="[INSTALLDIR]RedstonePanel.url"', wxs)
        self.assertIn('WixShellExecTarget" Value="[DATAFOLDER]open-panel.url"', wxs)
        self.assertIn('--for-sid &quot;[UserSID]&quot;', wxs)
        self.assertIn('Dialog="ExitDialog" Control="Finish" Event="DoAction" Value="OpenPanel"', wxs)
        self.assertNotIn("localhost:8080", wxs)        # the address depends on the panel's settings


if __name__ == "__main__":
    unittest.main()
