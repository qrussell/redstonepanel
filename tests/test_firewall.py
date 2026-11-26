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
        self.assertEqual(M.firewall_rules("bedrock", {"server-port": "19140", "transport": "raknet"}),
                         {("19140", "udp")})
        # RakNet listens on server-portv6 too
        self.assertEqual(M.firewall_rules("bedrock", {"server-port": "19140", "server-portv6": "19141",
                                                      "transport": "RakNet"}),
                         {("19140", "udp"), ("19141", "udp")})
        self.assertEqual(M.firewall_rules("bedrock", {}), set())

    def test_nethernet(self):
        r = M.firewall_rules("bedrock", {"server-port": "19132", "transport": "NetherNet",
                                         "server-udp-ports": "19141"})
        self.assertEqual(r, {("19132", "tcp"), ("19141", "udp"), ("7551", "udp")})

    def test_no_transport_line_is_nethernet(self):
        # BDS 1.26.66.1 with no transport line: TCP 19140 (dual-stack) and UDP 7551, no UDP 19140
        self.assertEqual(M.firewall_rules("bedrock", {"server-port": "19140", "server-portv6": "19141"}),
                         {("19140", "tcp"), ("7551", "udp")})
        self.assertEqual(M.firewall_rules("bedrock", {"server-port": "19140", "server-portv6": "19141",
                                                      "transport": "", "server-udp-ports": "19140"}),
                         {("19140", "tcp"), ("19140", "udp"), ("7551", "udp")})
        self.assertEqual(M.bedrock_transport({}), "nethernet")
        self.assertEqual(M.bedrock_transport({"transport": "raknet"}), "raknet")
        self.assertEqual(M.bedrock_transport({"transport": "nonsense"}), "nethernet")

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


class FixedUdpPorts(unittest.TestCase):
    def server(self, text):
        d = tempfile.mkdtemp(dir=TMP)
        with open(os.path.join(d, "server.properties"), "w") as f:
            f.write(text)
        return d

    def test_missing_gets_server_port(self):
        d = self.server("server-port=19140\nserver-portv6=19141\n")
        self.assertTrue(M.fixed_udp_ports(d))
        self.assertEqual(M.read_properties(d)["server-udp-ports"], "19140")
        self.assertFalse(M.fixed_udp_ports(d))                  # once is enough

    def test_own_setting_kept(self):
        d = self.server("server-port=19140\nserver-udp-ports=19400-19500\n")
        self.assertFalse(M.fixed_udp_ports(d))
        self.assertEqual(M.read_properties(d)["server-udp-ports"], "19400-19500")


class UpgradeOnce(unittest.TestCase):
    """1.16.11: Bedrock servers without a transport line had only UDP server-port opened."""
    def setUp(self):
        self.data = tempfile.mkdtemp(dir=TMP)
        self.cmds = []
        self.bedrock = tempfile.mkdtemp(dir=TMP)
        with open(os.path.join(self.bedrock, "server.properties"), "w") as f:
            f.write("server-port=19140\nserver-portv6=19141\n")
        self.servers = [{"name": "minecraft-bedrock", "edition": "Bedrock", "port": "19140",
                         "directory": self.bedrock},
                        {"name": "minecraft-java", "edition": "Java", "port": "25565", "directory": TMP}]
        dirs = {"minecraft-bedrock": self.bedrock}
        for p in (mock.patch.object(M, "DATA_DIR", self.data),
                  mock.patch.object(M, "run_and_log", lambda cmd, shell=False: self.cmds.append(" ".join(cmd))),
                  mock.patch.object(M.shutil, "which", lambda n: "/usr/sbin/ufw"),
                  mock.patch.object(M, "list_servers", lambda: self.servers),
                  mock.patch.object(M, "_is_java", lambda n: n not in dirs),
                  mock.patch.object(M, "get_server_dir", lambda n: dirs.get(n, TMP))):
            p.start()
            self.addCleanup(p.stop)

    def test_opens_once(self):
        M.upgrade_bedrock_firewall()
        self.assertEqual(self.cmds, ["ufw allow 19140/tcp", "ufw allow 19140/udp", "ufw allow 7551/udp"])
        self.assertEqual(M.read_properties(self.bedrock)["server-udp-ports"], "19140")
        self.cmds.clear()
        M.upgrade_bedrock_firewall()
        self.assertEqual(self.cmds, [])

    def test_without_ufw_still_fixes_the_port(self):
        with mock.patch.object(M.shutil, "which", lambda n: None):
            M.upgrade_bedrock_firewall()
        self.assertEqual(self.cmds, [])
        self.assertEqual(M.read_properties(self.bedrock)["server-udp-ports"], "19140")


class Sync(unittest.TestCase):
    """Following server.properties edited outside the panel, and servers removed by hand."""
    def setUp(self):
        self.data = tempfile.mkdtemp(dir=TMP)
        self.cmds = []
        self.servers = []
        self.phantoms = []
        for p in (mock.patch.object(M, "DATA_DIR", self.data),
                  mock.patch.object(M, "run_and_log", lambda cmd, shell=False: self.cmds.append(" ".join(cmd))),
                  mock.patch.object(M.shutil, "which", lambda n: "/usr/sbin/ufw"),
                  mock.patch.object(M, "list_servers", lambda: self.servers),
                  mock.patch.object(M.proxies, "find_phantoms", lambda: self.phantoms)):
            p.start()
            self.addCleanup(p.stop)

    def serve(self, **rules):
        self.servers = [{"name": n, "firewall_rules": sorted(map(list, r))} for n, r in rules.items()]

    def log(self):
        with open(os.environ["RP_LOG_FILE"]) as f:
            return f.read()

    def test_first_sight_only_notes(self):
        self.serve(**{"minecraft-b": {("19132", "tcp"), ("7551", "udp")}})
        self.assertEqual(M.sync_firewall(), [])
        self.assertEqual(self.cmds, [])
        self.assertEqual(M._fw_load(), {"minecraft-b": {("19132", "tcp"), ("7551", "udp")}})

    def test_the_dashboard_leaves_the_port_check_to_the_watcher(self):
        """1.24.1: listing every socket takes seconds on Windows, so a dashboard or cluster
        request doesn't run the port check; the watcher (every 2 minutes) does."""
        from core import portaudit, host
        swept = []
        with mock.patch.object(portaudit, "sweep", lambda: swept.append(1)):
            M.sync_firewall(ports=False)
            self.assertEqual(swept, [])
            M.sync_firewall()
            self.assertEqual(swept, [1])
        with open(host.__file__, encoding="utf-8") as f:
            self.assertIn("manager.sync_firewall(ports=False)", f.read())

    def test_hand_edited_port(self):
        M.firewall_record("minecraft-b", {("19132", "tcp"), ("19132", "udp"), ("7551", "udp")})
        self.serve(**{"minecraft-b": {("19140", "tcp"), ("19140", "udp"), ("7551", "udp")}})
        self.assertEqual(M.sync_firewall(), ["minecraft-b"])
        self.assertEqual(self.cmds, ["ufw allow 19140/tcp", "ufw allow 19140/udp",
                                     "ufw delete allow 19132/tcp", "ufw delete allow 19132/udp"])
        self.assertIn("minecraft-b's server.properties changed outside the panel: allowed 19140/tcp, 19140/udp; "
                      "removed 19132/tcp, 19132/udp.", self.log())
        self.cmds.clear()
        self.assertEqual(M.sync_firewall(), [])                  # nothing more to do
        self.assertEqual(self.cmds, [])

    def test_shared_rules_stay(self):
        # 7551 is still another server's; 19300 is a Phantom's
        M.firewall_record("minecraft-a", {("19132", "tcp"), ("7551", "udp"), ("19300", "udp")})
        self.serve(**{"minecraft-a": {("19132", "udp")}, "minecraft-b": {("19150", "tcp"), ("7551", "udp")}})
        self.phantoms = [{"bind_port": 19300}]
        M.firewall_record("minecraft-b", {("19150", "tcp"), ("7551", "udp")})
        M.sync_firewall()
        self.assertEqual(self.cmds, ["ufw allow 19132/udp", "ufw delete allow 19132/tcp"])

    def test_server_gone_without_the_panel(self):
        M.firewall_record("minecraft-gone", {("25570", "tcp")})
        M.firewall_record("minecraft-b", {("7551", "udp"), ("19132", "tcp")})
        self.serve(**{"minecraft-b": {("7551", "udp"), ("19132", "tcp")}})
        self.assertEqual(M.sync_firewall(), ["minecraft-gone"])
        self.assertEqual(self.cmds, ["ufw delete allow 25570/tcp"])
        self.assertNotIn("minecraft-gone", M._fw_load())
        self.assertIn("minecraft-gone is no longer on the panel: removed 25570/tcp.", self.log())

    def test_busy_server_left_alone(self):
        M.firewall_record("minecraft-new", {("19134", "tcp")})
        with mock.patch.object(M, "_busy", {"minecraft-new"}):
            M.sync_firewall()                    # being deployed or moved: its unit isn't there yet
        self.assertEqual(self.cmds, [])
        self.assertIn("minecraft-new", M._fw_load())

    def test_without_ufw(self):
        with mock.patch.object(M.shutil, "which", lambda n: None):
            self.assertEqual(M.sync_firewall(), [])
        self.assertEqual(M._fw_load(), {})

    def test_panel_changes_are_noted(self):
        # A change made in the panel isn't reported as made outside it
        after = {("19140", "tcp"), ("7551", "udp")}
        self.serve(**{"minecraft-b": after})
        with mock.patch.object(M, "server_firewall_rules", lambda s: after):
            M.properties_changed("minecraft-b", {("19132", "tcp"), ("7551", "udp")})
        self.cmds.clear()
        self.assertEqual(M.sync_firewall(), [])
        self.assertEqual(self.cmds, [])
