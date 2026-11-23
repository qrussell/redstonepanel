"""Tests for core/found.py: Minecraft servers on the machine that the panel doesn't manage.
Run: python -m unittest discover -s tests"""
import os
import sys
import tempfile
import unittest
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-found-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import found as F  # noqa: E402
from core import manager as M  # noqa: E402
from core.platform.linux import system as linux_system  # noqa: E402


def folder(name, port=None, extra=""):
    d = os.path.join(TMP, name)
    os.makedirs(d, exist_ok=True)
    if port is not None:
        with open(os.path.join(d, "server.properties"), "w") as f:
            f.write(f"server-port={port}\nlevel-name=mountains\n{extra}")
    return d


def java(cwd, service="", **kw):
    return dict({"pid": 4242, "exe": "/usr/lib/jvm/java-21-openjdk-amd64/bin/java",
                 "argv": ["java", "-Xms1G", "-Xmx4G", "-jar", "server.jar", "nogui"], "cwd": cwd, "user": "steve",
                 "service": service, "started": 1700000000.0}, **kw)


class Describe(unittest.TestCase):
    def test_a_java_server_someone_started_themselves(self):
        d = folder("own-java", 25570)
        info = F.describe(java(d, "mc-survival.service"))
        self.assertEqual((info["edition"], info["port"], info["folder"], info["world"]), ("Java", 25570, d, "mountains"))
        self.assertEqual((info["service"], info["started_by"], info["user"], info["heap"], info["jar"]),
                         ("mc-survival.service", "mc-survival.service", "steve", "4G", "server.jar"))

    def test_started_from_a_terminal(self):
        info = F.describe(java(folder("screen-java", 25571)))
        self.assertEqual(info["service"], "")
        self.assertIn("terminal", info["started_by"])

    def test_bedrock(self):
        d = folder("own-bedrock", 19140)
        info = F.describe({"pid": 7, "exe": os.path.join(d, "bedrock_server"), "argv": ["./bedrock_server"], "cwd": d,
                           "user": "steve", "service": "", "started": None})
        self.assertEqual((info["edition"], info["port"], info["java"]), ("Bedrock", 19140, None))

    def test_other_java_programs_are_not_servers(self):
        self.assertIsNone(F.describe(java(folder("jenkins"))))                       # no server.properties
        self.assertIsNone(F.describe({"exe": "/usr/bin/python3", "argv": ["python3"], "cwd": folder("py", 1)}))
        self.assertIsNone(F.describe(java("")))

    def test_the_panels_own_servers_are_left_out(self):
        d = folder("minecraft-java", 25565)
        self.assertIsNone(F.describe(java(d), managed=[d]))
        with mock.patch.object(M.os.path, "exists", side_effect=lambda p: p == M._unit_path("minecraft-java")):
            self.assertIsNone(F.describe(java(folder("elsewhere", 25572), "minecraft-java.service")))


class Scan(unittest.TestCase):
    def test_one_entry_per_folder_sorted_by_port(self):
        a, b = folder("scan-a", 25580), folder("scan-b", 25575)
        programs = [java(a, pid=1), java(a, pid=2), java(b, pid=3), java(folder("scan-none"), pid=4)]
        with mock.patch.object(F.system, "game_programs", return_value=programs), \
             mock.patch.object(F, "_managed_folders", return_value=[]):
            got = F.scan()
        self.assertEqual([(g["port"], g["pid"]) for g in got], [(25575, 3), (25580, 1)])


class HandOverAndBack(unittest.TestCase):
    """Real panel code against the fake system: a server run by someone's own unit is handed to
    the panel in place, and handed back."""

    def setUp(self):
        from core import platform
        from core.platform import fake, paths
        platform.use("fake")
        self.addCleanup(platform.use)
        fake.reset()
        self.fake = fake
        self.units = tempfile.mkdtemp(dir=TMP)
        self.data = tempfile.mkdtemp(dir=TMP)
        for target, name, value in ((paths, "UNIT_DIR", self.units), (F, "RECORD_DIR", os.path.join(self.data, "found")),
                                    (F, "SEEN_FILE", os.path.join(self.data, "seen.json")), (F, "STOP_WAIT", 2)):
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        for name in ("java_for_folder", "ensure_minecraft_user", "_used_ports"):
            p = mock.patch.object(M, name, return_value=set() if name == "_used_ports" else None)
            p.start()
            self.addCleanup(p.stop)
        self.d = tempfile.mkdtemp(dir=TMP, prefix="own-")
        with open(os.path.join(self.d, "server.properties"), "w") as f:
            f.write("server-port=25590\nlevel-name=mountains\n")
        open(os.path.join(self.d, "paper-1.21.4.jar"), "w").close()
        fake.state.units["mc-survival.service"] = {"state": "active", "enabled": True}
        fake.state.game_programs = [java(self.d, "mc-survival.service", argv=["java", "-Xmx3G", "-jar", "paper-1.21.4.jar", "nogui"])]
        fake.state.owners = {self.d: "1001:1001"}
        fake.state.rules = {("25590", "tcp")}                      # its owner had opened the port

    def test_handed_over_in_place_and_back(self):
        fake = self.fake
        got = F.manage(self.d, actor="alex")
        name = got["name"]
        self.assertEqual((got["port"], got["folder"]), (25590, self.d))
        self.assertTrue(name.startswith("minecraft-java"))
        # The old service: stopped and turned off, never removed
        self.assertEqual(fake.state.units["mc-survival.service"], {"state": "inactive", "enabled": False})
        self.assertIn(("services.stop", "mc-survival"), fake.calls)
        self.assertIn(("services.disable", "mc-survival"), fake.calls)
        # The same folder, nothing copied: the jar has the name the panel uses, the unit points here
        self.assertTrue(os.path.isfile(os.path.join(self.d, "server.jar")))
        self.assertFalse(os.path.exists(os.path.join(self.d, "paper-1.21.4.jar")))
        unit = open(os.path.join(self.units, f"{name}.service")).read()
        self.assertIn(f"WorkingDirectory={self.d}", unit)
        self.assertIn("-Xmx3G", unit)
        self.assertIn(("services.start", name), fake.calls)
        self.assertEqual(F.listing(), [])                           # no longer "found"
        self.assertEqual([t["name"] for t in F.taken_over()], [name])
        rec = F.record(name)
        self.assertEqual((rec["owner"], rec["service"], rec["was_enabled"], rec["jar_was"], rec["by"]),
                         ("1001:1001", "mc-survival.service", True, "paper-1.21.4.jar", "alex"))
        self.assertEqual(rec["firewall_had"], [["25590", "tcp"]])

        fake.state.servers.setdefault(name, {}).update(working_dir=self.d, exec_start="/usr/bin/java -Xmx3G -jar server.jar nogui")
        back = F.undo(name, actor="alex")
        self.assertEqual(back, {"folder": self.d, "service": "mc-survival.service"})
        self.assertFalse(os.path.exists(os.path.join(self.units, f"{name}.service")))
        self.assertTrue(os.path.isfile(os.path.join(self.d, "paper-1.21.4.jar")))      # its own name again
        self.assertFalse(os.path.exists(os.path.join(self.d, "server.jar")))
        self.assertFalse(os.path.exists(os.path.join(self.d, M.REMOVED_INFO)))
        self.assertTrue(os.path.isfile(os.path.join(self.d, "server.properties")))     # the folder is never deleted
        self.assertIn(("system.give_back", self.d, "1001:1001"), fake.calls)
        self.assertEqual(fake.state.units["mc-survival.service"], {"state": "active", "enabled": True})
        self.assertIn(("25590", "tcp"), fake.state.rules)           # the port its owner had opened is open again
        self.assertIsNone(F.record(name))
        self.assertEqual(F.taken_over(), [])

    def test_a_service_that_will_not_stop_changes_nothing(self):
        fake = self.fake
        with mock.patch.object(F, "_running_in", return_value=[{"pid": 1}]):
            with self.assertRaises(F.FoundError) as e:
                F.manage(self.d)
        self.assertIn("didn't stop", str(e.exception))
        self.assertEqual(fake.state.units["mc-survival.service"]["state"], "active")
        self.assertTrue(fake.state.units["mc-survival.service"]["enabled"])
        self.assertTrue(os.path.isfile(os.path.join(self.d, "paper-1.21.4.jar")))
        self.assertEqual(os.listdir(self.units), [])
        self.assertEqual(F.taken_over(), [])

    def test_refusals_change_nothing(self):
        fake = self.fake
        with self.assertRaises(F.FoundError):
            F.manage(os.path.join(TMP, "nowhere"))                  # not a found server: no path is taken on trust
        fake.state.game_programs[0]["service"] = ""                 # started from a terminal and still running
        with self.assertRaises(F.FoundError) as e:
            F.manage(self.d)
        self.assertIn("Stop it where you started it", str(e.exception))
        fake.state.game_programs[0].update(service="mc-survival.service", argv=["java", "@user_jvm_args.txt"])
        with self.assertRaises(F.FoundError) as e:
            F.manage(self.d)
        self.assertIn("-jar", str(e.exception))
        with mock.patch.object(M, "_used_ports", return_value={25590}):
            fake.state.game_programs[0]["argv"] = ["java", "-jar", "paper-1.21.4.jar"]
            with self.assertRaises(F.FoundError) as e:
                F.manage(self.d)
        self.assertIn("25590", str(e.exception))
        self.assertEqual([c for c in fake.calls if c[0].startswith("services.")], [])
        self.assertEqual(os.listdir(self.units), [])

    def test_stopped_by_hand_stays_listed_for_an_hour(self):
        fake = self.fake
        fake.state.game_programs[0]["service"] = ""
        self.assertEqual([(f["folder"], f["running"]) for f in F.listing(now=1000)], [(self.d, True)])
        fake.state.game_programs = []                               # its owner typed stop
        self.assertEqual([(f["folder"], f["running"]) for f in F.listing(now=1600)], [(self.d, False)])
        self.assertEqual(F.listing(now=1000 + F.SEEN_FOR + 1), [])

    def test_undo_only_for_what_was_taken_over(self):
        with self.assertRaises(F.FoundError):
            F.undo("minecraft-java")
        with self.assertRaises(F.FoundError):
            F.undo("../etc")


class LinuxPrograms(unittest.TestCase):
    def test_reads_this_very_process(self):
        me = [p for p in linux_system.game_programs((os.path.basename(os.path.realpath(sys.executable)),))
              if p["pid"] == os.getpid()]
        self.assertEqual(len(me), 1)
        self.assertEqual(me[0]["cwd"], os.getcwd())
        self.assertTrue(me[0]["argv"])
        self.assertLess(abs(me[0]["started"] - __import__("time").time()), 3600)

    def test_nothing_called_that(self):
        self.assertEqual(linux_system.game_programs(("no-such-program-xyz",)), [])


if __name__ == "__main__":
    unittest.main()
