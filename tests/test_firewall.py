"""Firewall rules each server needs, from its server.properties (core/manager.py), and the
dashboard's check of them (core/host.py). Run: python -m unittest discover -s tests"""
import os
import sys
import tempfile
import unittest
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-firewall-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import manager as M  # noqa: E402


class Rules(unittest.TestCase):
    def test_java_and_raknet(self):
        self.assertEqual(M.firewall_rules("java", {"server-port": "25565"}), {("25565", "tcp")})
        self.assertEqual(M.firewall_rules("bedrock", {"server-port": "19132"}), {("19132", "udp")})
        self.assertEqual(M.firewall_rules("bedrock", {"server-port": "19140", "transport": "raknet"}),
                         {("19140", "udp")})
        self.assertEqual(M.firewall_rules("bedrock", {}), set())

    def test_nethernet(self):
        r = M.firewall_rules("bedrock", {"server-port": "19132", "transport": "NetherNet",
                                         "server-udp-ports": "19141"})
        self.assertEqual(r, {("19132", "tcp"), ("19141", "udp"), ("7551", "udp")})

    def test_udp_port_forms(self):
        cases = {"19141": ["19141"], "19141:19141": ["19141"], "203.0.113.7:19141:19141": ["19141"],
                 "21200-21300": ["21200:21300"], "203.0.113.7:21200-21300:19400-19500": ["19400:19500"],
                 "19141,19150": ["19141", "19150"], "": [], "abc": [], "300-200": []}
        for value, want in cases.items():
            self.assertEqual(M._udp_port_specs(value), want, value)


class Changes(unittest.TestCase):
    def setUp(self):
        self.cmds = []
        self.servers = []
        for p in (mock.patch.object(M, "run_and_log", lambda cmd, shell=False: self.cmds.append(" ".join(cmd))),
                  mock.patch.object(M.shutil, "which", lambda n: "/usr/sbin/ufw"),
                  mock.patch.object(M, "list_servers", lambda: self.servers)):
            p.start()
            self.addCleanup(p.stop)

    def test_switch_to_nethernet_opens_and_closes(self):
        before = {("19140", "udp")}
        after = {("19140", "tcp"), ("19141", "udp"), ("7551", "udp")}
        self.servers = [{"name": "minecraft-b", "firewall_rules": sorted(map(list, after))}]
        with mock.patch.object(M, "server_firewall_rules", lambda s: after):
            M.properties_changed("minecraft-b", before)
        self.assertEqual(self.cmds, ["ufw allow 19140/tcp", "ufw allow 19141/udp", "ufw allow 7551/udp",
                                     "ufw delete allow 19140/udp"])

    def test_shared_lan_port_stays_while_another_server_needs_it(self):
        self.servers = [{"name": "minecraft-other", "firewall_rules": [["19150", "tcp"], ["7551", "udp"]]}]
        M.close_firewall_rules({("19132", "tcp"), ("19141", "udp"), ("7551", "udp")})
        self.assertEqual(self.cmds, ["ufw delete allow 19132/tcp", "ufw delete allow 19141/udp"])
        self.cmds.clear()
        self.servers = []
        M.close_firewall_rules({("7551", "udp")})
        self.assertEqual(self.cmds, ["ufw delete allow 7551/udp"])

    def test_nothing_changed(self):
        with mock.patch.object(M, "server_firewall_rules", lambda s: {("25565", "tcp")}):
            M.properties_changed("minecraft-java", {("25565", "tcp")})
        self.assertEqual(self.cmds, [])


class Dashboard(unittest.TestCase):
    STATUS = """Status: active

To                         Action      From
--                         ------      ----
19132/tcp                  ALLOW       Anywhere
19141/udp                  ALLOW       Anywhere
21200:21300/udp            ALLOW       Anywhere
"""

    def test_specs(self):
        from core import host
        self.assertTrue(host.ufw_allows_spec(self.STATUS, "19132", "tcp"))
        self.assertFalse(host.ufw_allows_spec(self.STATUS, "19132", "udp"))
        self.assertFalse(host.ufw_allows_spec(self.STATUS, "7551", "udp"))
        self.assertTrue(host.ufw_allows_spec(self.STATUS, "21200:21300", "udp"))
        self.assertFalse(host.ufw_allows_spec(self.STATUS, "21200:21400", "udp"))


if __name__ == "__main__":
    unittest.main()
