"""Tests for core/platform, the layer between the panel and the operating system (1.21).

Three things are checked:
  * the rule: nothing outside core/platform runs a system program or spells out a system path;
  * the Linux side runs the same commands the panel always ran;
  * the fake side has the same functions, and real panel code runs against it with no
    systemd, ufw, screen or apt on the machine.
Run: python -m unittest discover -s tests"""
import ast
import glob
import importlib
import inspect
import os
import sys
import tempfile
import unittest
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-platform-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import platform                                   # noqa: E402
from core.platform import fake, paths                       # noqa: E402
from core.platform import services, firewall, console, packages, system   # noqa: E402

# Programs only the platform layer may start
SYSTEM_PROGRAMS = {"systemctl", "journalctl", "systemd-run", "ufw", "screen", "apt", "apt-get", "apt-cache",
                   "dpkg", "dpkg-query", "openssl", "sudo", "hostname", "ip", "id", "useradd", "chown", "chmod",
                   "rm", "cp", "mv", "find", "grep", "tail", "date", "curl", "wget", "jq", "unzip", "tar"}
# Files outside the layer that still start a program themselves, and why
MAY_RUN_PROGRAMS = {
    "core/manager.py": "run_and_log (the one place that writes command output to the panel log) and "
                       "Bedrock's own program, started to look up an XUID",
    "core/backup_targets.py": "ssh, ssh-keygen and rsync for off-site backups (OpenSSH exists on both systems; "
                              "rsync gives way to sftp on Windows in 1.23)",
}
# Spelled-out paths that are not places on this machine
NOT_SYSTEM_PATHS = ("/bin/java",)          # the end of a Java path inside its own folder
SYSTEM_ROOTS = ("/opt", "/var/", "/etc/", "/usr/", "/run/", "/proc", "/sys/", "/bin/", "/sbin/", "/home/", "/tmp/")


def panel_sources():
    files = sorted(glob.glob(os.path.join(ROOT, "core", "*.py")))
    files += [os.path.join(ROOT, n) for n in ("main.py", "run.py", "https_redirect.py")]
    return [(os.path.relpath(f, ROOT).replace(os.sep, "/"), f) for f in files]


def docstrings(tree):
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                out.add(id(first.value))
    return out


class TheRule(unittest.TestCase):
    """Guards the port: a new systemctl call or /opt path outside core/platform fails here."""

    def test_no_system_programs_outside_the_layer(self):
        found = []
        for rel, path in panel_sources():
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                if isinstance(node, ast.List) and len(node.elts) > 1 and isinstance(node.elts[0], ast.Constant) \
                        and node.elts[0].value in SYSTEM_PROGRAMS:
                    found.append(f"{rel}:{node.lineno} runs {node.elts[0].value}")
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "os" \
                        and node.attr in ("system", "popen"):
                    found.append(f"{rel}:{node.lineno} uses os.{node.attr}")
        self.assertEqual(found, [], "System programs are started from core/platform only.")

    def test_only_listed_files_start_programs(self):
        found = set()
        for rel, path in panel_sources():
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                        and node.value.id == "subprocess" and node.attr in ("run", "Popen", "call", "check_call",
                                                                             "check_output"):
                    found.add(rel)
        self.assertEqual(sorted(found), sorted(MAY_RUN_PROGRAMS))

    def test_no_system_paths_outside_the_layer(self):
        found = []
        for rel, path in panel_sources():
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read())
            skip = docstrings(tree)
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Constant) and isinstance(node.value, str)) or id(node) in skip:
                    continue
                for line in node.value.splitlines():
                    text = line.strip()
                    for prefix in ("ExecStart=", "WorkingDirectory="):
                        text = text[len(prefix):] if text.startswith(prefix) else text
                    if text.startswith(SYSTEM_ROOTS) and text not in NOT_SYSTEM_PATHS and " " not in text.split("/")[1]:
                        found.append(f"{rel}:{node.lineno} {text[:60]!r}")
        self.assertEqual(found, [], "System paths come from core/platform/paths.py.")

    def test_every_platform_file_is_packaged(self):
        with open(os.path.join(ROOT, "debian", "redstonepanel.install"), encoding="utf-8") as f:
            install = f.read()
        for path in glob.glob(os.path.join(ROOT, "core", "platform", "**", "*.py"), recursive=True):
            rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
            self.assertIn(rel + " ", install.replace("\n", " ") + " ", f"{rel} is missing from debian/redstonepanel.install")


class TwoSides(unittest.TestCase):
    def test_fake_has_every_function_with_the_same_arguments(self):
        def public(mod):
            return {n: v for n, v in vars(mod).items()
                    if inspect.isfunction(v) and v.__module__ == mod.__name__ and not n.startswith("_")}
        for area in platform.AREAS:
            real = public(importlib.import_module(f"core.platform.linux.{area}"))
            pretend = public(importlib.import_module(f"core.platform.fake.{area}"))
            self.assertEqual(sorted(real), sorted(pretend), area)
            for name in real:
                self.assertEqual(list(inspect.signature(real[name]).parameters),
                                 list(inspect.signature(pretend[name]).parameters), f"{area}.{name}")

    def test_switching(self):
        self.addCleanup(platform.use)
        self.assertEqual(platform.use(), "linux")
        self.assertIs(platform.area("services"), importlib.import_module("core.platform.linux.services"))
        self.assertEqual(platform.use("fake"), "fake")
        self.assertEqual(platform.name(), "fake")
        self.assertEqual(services.unit("minecraft-java"), "minecraft-java.service")   # the name imported above follows
        self.assertIs(platform.area("services"), importlib.import_module("core.platform.fake.services"))
        with self.assertRaises(ValueError):
            platform.use("solaris")

    def test_an_area_can_be_patched(self):
        with mock.patch.object(services, "is_active", lambda server: True):
            self.assertTrue(services.is_active("minecraft-nope"))
        self.assertEqual(services.is_active.__module__, "core.platform.linux.services")


class LinuxCommands(unittest.TestCase):
    """The Linux side with the programs themselves replaced: what would be run."""

    def setUp(self):
        from core import manager
        platform.use("linux")
        self.ran, self.logged = [], []
        self.out = {}

        def run(cmd, **kw):
            self.ran.append(list(cmd))
            return mock.Mock(stdout=self.out.get(cmd[0], ""), stderr="", returncode=0)
        for p in (mock.patch("subprocess.run", run),
                  mock.patch.object(manager, "run_and_log", lambda cmd: self.logged.append(cmd) or 0)):
            p.start()
            self.addCleanup(p.stop)

    def test_services(self):
        services.start("minecraft-java")
        services.stop("minecraft-java", elevate=True)
        services.control("phantom-minecraft-island.service", "restart")
        services.reload()
        self.assertEqual(self.logged, [["systemctl", "start", "minecraft-java.service"],
                                       ["sudo", "systemctl", "stop", "minecraft-java.service"],
                                       ["systemctl", "restart", "phantom-minecraft-island.service"],
                                       ["systemctl", "daemon-reload"]])
        services.restart("minecraft-java", quiet=True)
        services.state("minecraft-java")
        services.exec_start("minecraft-java")
        services.set_runtime_limits("minecraft-java", 2 << 30, 1.5)
        services.set_runtime_limits("minecraft-java")
        services.start_later("minecraft-bedrock", 20)
        self.assertEqual(self.ran, [
            ["systemctl", "restart", "minecraft-java.service"],
            ["systemctl", "is-active", "minecraft-java.service"],
            ["systemctl", "show", "-p", "ExecStart", "--value", "minecraft-java.service"],
            ["systemctl", "set-property", "--runtime", "minecraft-java.service", "MemoryMax=2147483648", "CPUQuota=150%"],
            ["systemctl", "set-property", "--runtime", "minecraft-java.service", "MemoryMax=infinity", "CPUQuota="],
            ["systemd-run", "--quiet", "--on-active=20", "--timer-property=AccuracySec=1s",
             "/usr/bin/systemctl", "start", "minecraft-bedrock.service"]])
        self.assertEqual(self.logged[4:], [], "quiet calls stay out of the activity log")

    def test_schedules(self):
        timer = "redstonepanel-backup@minecraft-java.timer"
        services.enable_schedule(timer)
        self.assertEqual(self.logged, [["systemctl", "enable", timer], ["systemctl", "restart", timer]])
        services.disable_schedule(timer)
        self.assertEqual(self.ran[-1], ["systemctl", "disable", "--now", timer])
        self.out = {"systemctl": "Mon 2030-03-18 04:00:00 UTC\n", "date": "1899950400\n"}
        self.assertEqual(services.next_run(timer), "Mon 2030-03-18 04:00:00 UTC")
        self.assertEqual(services.next_run_epoch(timer), 1899950400)
        self.assertEqual(self.ran[-1], ["date", "-d", "Mon 2030-03-18 04:00:00 UTC", "+%s"])
        self.out = {"systemctl": "n/a\n"}
        self.assertIsNone(services.next_run_epoch(timer))

    def test_detached_work(self):
        services.run_detached("redstonepanel-upgrade", ["/usr/sbin/redstonepanel", "upgrade"],
                              description="RedstonePanel update")
        services.run_detached("redstonepanel-offsite-minecraft-java",
                              ["/usr/sbin/redstonepanel", "offsite-copy", "minecraft-java"],
                              low_priority=True, no_block=True)
        self.assertEqual(self.ran, [
            ["systemd-run", "--unit=redstonepanel-upgrade", "--collect", "--quiet",
             "--description=RedstonePanel update", "/usr/sbin/redstonepanel", "upgrade"],
            ["systemd-run", "--no-block", "--quiet", "--collect", "--unit=redstonepanel-offsite-minecraft-java",
             "--property=Nice=10", "--property=IOSchedulingClass=idle",
             "/usr/sbin/redstonepanel", "offsite-copy", "minecraft-java"]])

    def test_logs_and_jobs(self):
        services.log("minecraft-java.service", lines=8)
        services.log("minecraft-java.service", output="short-unix", quiet=True, since=1700000000.7)
        services.run_log("minecraft-java", "0123456789abcdef0123456789abcdef")
        self.assertEqual(self.ran, [
            ["journalctl", "-u", "minecraft-java.service", "-n", "8", "--no-pager", "-o", "cat"],
            ["journalctl", "-u", "minecraft-java.service", "--no-pager", "-o", "short-unix", "-q", "--since", "@1700000000"],
            ["journalctl", "--no-pager", "-o", "cat", "-n", "300",
             "_SYSTEMD_INVOCATION_ID=0123456789abcdef0123456789abcdef"]])
        self.out = {"systemctl": "101 minecraft-java.service restart waiting\n102 other.service stop running\n"}
        self.assertEqual(services.queued_jobs("minecraft-java"), ["restart"])
        self.out = {"systemctl": "minecraft-a.service loaded failed failed A\nminecraft-b.service not-found failed failed B\n"}
        self.assertEqual(services.failed("minecraft*"), [("minecraft-a.service", "loaded"), ("minecraft-b.service", "not-found")])

    def test_unit_files(self):
        d = tempfile.mkdtemp(dir=TMP)
        for name in ("minecraft-java.service", "minecraft.service", "minecraft-.service.d", "phantom-minecraft.service",
                     "redstonepanel-backup@minecraft.timer"):
            open(os.path.join(d, name), "w").close()
        with mock.patch.object(paths, "UNIT_DIR", d):
            self.assertEqual(sorted(services.unit_files()), ["minecraft-java.service", "minecraft.service"])
        with mock.patch.object(paths, "UNIT_DIR", os.path.join(d, "missing")):
            self.assertEqual(services.unit_files(), [])

    def test_firewall_console_packages_system(self):
        firewall.allow(25565, "tcp")
        firewall.remove("21200:21300", "udp")
        system.give_to_game_account("/opt/minecraft-java")
        self.assertEqual(self.logged, [["ufw", "allow", "25565/tcp"], ["ufw", "delete", "allow", "21200:21300/udp"],
                                       ["chown", "-R", "-h", "minecraft:minecraft", "/opt/minecraft-java"]])
        with mock.patch("shutil.which", lambda name: f"/usr/bin/{name}"):
            console.send("minecraft-bedrock", "save hold")
        console.send("minecraft-bedrock", "stop", timeout=10, find=False)
        self.assertEqual(self.ran, [
            ["sudo", "-u", "minecraft", "/usr/bin/screen", "-S", "minecraft-bedrock", "-p", "0", "-X", "stuff", "save hold\r"],
            ["sudo", "-u", "minecraft", "screen", "-S", "minecraft-bedrock", "-p", "0", "-X", "stuff", "stop\r"]])
        with mock.patch("shutil.which", lambda name: None):
            self.assertIsNone(console.send("minecraft-bedrock", "list"))
            self.assertFalse(firewall.installed())
        self.assertEqual(console.start_command("minecraft-bedrock", "/opt/minecraft-bedrock"),
                         "/usr/bin/screen -c /opt/minecraft-bedrock/screenrc -L -Logfile /opt/minecraft-bedrock/server.log "
                         "-DmS minecraft-bedrock ./bedrock_server")
        del self.ran[:]
        self.out = {"apt-cache": "openjdk-21-jre-headless\nopenjdk-25-jre-headless\n", "dpkg-query": "1.21.0\n",
                    "hostname": "192.168.1.20 172.17.0.1 \n"}
        self.assertEqual(packages.names("openjdk-"), ["openjdk-21-jre-headless", "openjdk-25-jre-headless"])
        self.assertEqual(packages.installed_version(), "1.21.0")
        self.assertEqual(system.addresses(timeout=3), ["192.168.1.20", "172.17.0.1"])
        self.assertEqual(self.ran, [["apt-cache", "pkgnames", "openjdk-"],
                                    ["dpkg-query", "-W", "-f=${Version}", "redstonepanel"], ["hostname", "-I"]])
        self.assertEqual(packages.manager_command(["-q"], ["install", "-y", "openjdk-21-jre-headless"]),
                         ["apt-get", "-q", "install", "-y", "openjdk-21-jre-headless"])


class PanelOnTheFake(unittest.TestCase):
    """Real panel code against the fake system: nothing is run, the calls are recorded."""

    def setUp(self):
        platform.use("fake")
        self.addCleanup(platform.use)
        fake.reset()
        self.units = tempfile.mkdtemp(dir=TMP)
        p = mock.patch.object(paths, "UNIT_DIR", self.units)
        p.start()
        self.addCleanup(p.stop)

    def server(self, name="minecraft-java", **values):
        open(os.path.join(self.units, f"{name}.service"), "w").close()
        fake.state.servers[name] = dict({"state": "active", "exec_start": "/usr/bin/java -Xms1G -Xmx2G -jar server.jar nogui",
                                         "working_dir": os.path.join(TMP, name)}, **values)

    def test_power_buttons(self):
        from core import manager
        self.server()
        manager.control_server("minecraft-java", "stop")
        self.assertEqual(fake.calls, [("services.stop", "minecraft-java")])
        self.assertEqual(services.state("minecraft-java"), "inactive")
        manager.control_server("minecraft-java", "start")
        self.assertTrue(services.is_active("minecraft-java"))
        with self.assertRaises(manager.ServerError):
            manager.control_server("minecraft-other", "start")        # no such server
        with self.assertRaises(manager.ServerError):
            manager.control_server("minecraft-java", "poweroff")

    def test_server_folder_and_command(self):
        from core import manager
        self.server()
        self.assertEqual(manager.get_server_dir("minecraft-java"), os.path.join(TMP, "minecraft-java"))
        self.assertEqual(manager.get_server_dir("minecraft-unknown"), "/opt/minecraft-unknown")
        self.assertEqual(manager.unit_heap(manager._unit_exec_start("minecraft-java")), {"xms": "1G", "xmx": "2G"})

    def test_firewall(self):
        from core import host, manager
        manager.open_firewall(25565, "tcp")
        manager.open_firewall("21200:21300", "udp")
        self.assertEqual(fake.state.rules, {("25565", "tcp"), ("21200:21300", "udp")})
        host._cache.clear()
        fw = host.firewall()
        self.assertEqual((fw["installed"], fw["active"]), (True, True))
        self.assertIn("25565/tcp", fw["rules"])
        fake.state.firewall_installed = False
        fake.calls.clear()
        manager.open_firewall(25566, "tcp")
        self.assertEqual(fake.calls, [], "no firewall program: nothing is done")
        host._cache.clear()

    def test_bedrock_console(self):
        from core import backups
        self.server("minecraft-bedrock", exec_start="/usr/bin/screen -DmS minecraft-bedrock ./bedrock_server")
        self.assertTrue(backups._screen("minecraft-bedrock", "save hold"))
        self.assertEqual(fake.state.console, [("minecraft-bedrock", "save hold")])
        fake.state.servers["minecraft-bedrock"]["state"] = "inactive"
        self.assertFalse(backups._screen("minecraft-bedrock", "save resume"))
        fake.state.console_installed = False
        self.assertFalse(backups._screen("minecraft-bedrock", "save resume"))

    def test_stop_or_crash(self):
        from core import moderation, notify
        self.server(started_at=1700000000, jobs=[])
        self.assertFalse(notify.stop_requested("minecraft-java"))
        fake.state.servers["minecraft-java"]["jobs"] = ["start"]
        self.assertFalse(notify.stop_requested("minecraft-java"), "a start job isn't a stop")
        fake.state.servers["minecraft-java"]["jobs"] = ["restart"]
        self.assertTrue(notify.stop_requested("minecraft-java"))
        self.assertEqual(moderation._started_at("minecraft-java"), 1700000000)
        notify.revive("minecraft-java")
        self.assertEqual(fake.calls[-1], ("services.start_later", "minecraft-java", 20))

    def test_java_versions_on_offer(self):
        from core import javaservers
        fake.state.packages = {"openjdk-21-jre-headless": {"candidate": "21.0.9"}, "openjdk-25-jre-headless": {"candidate": "25.0.1"},
                               "openjdk-25-jdk": {"candidate": "25.0.1"}}
        self.assertEqual(javaservers.packaged_javas(refresh=True), (21, 25))
        javaservers._packaged.update(at=0, majors=())

    def test_addresses(self):
        from core import discovery, tls
        fake.state.addresses = ["192.168.1.20"]
        self.assertIn("192.168.1.20", tls.identity()["ips"])
        nets, own = discovery.networks()
        self.assertEqual(([str(n) for n in nets], own), (["192.168.1.0/24"], {"192.168.1.20"}))


if __name__ == "__main__":
    unittest.main()
