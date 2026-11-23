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
