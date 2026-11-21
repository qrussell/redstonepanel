"""deploy_server: a new Bedrock server keeps the server.properties the official zip ships and
only gets its ports set. Download and system commands are stubbed.
Run: python -m unittest discover -s tests"""
import os
import sys
import tempfile
import unittest
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-deploy-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
os.environ.setdefault("RP_BACKUP_DIR", os.path.join(TMP, "backups"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import manager as M  # noqa: E402

SHIPPED = """server-name=Dedicated Server
# Used as the server name
gamemode=survival
difficulty=easy
allow-cheats=false
max-players=10
online-mode=true
server-port=19132
server-portv6=19133
server-udp-ports=19132
transport=nethernet
level-name=Bedrock level
view-distance=32
tick-distance=4
"""


class DeployBedrock(unittest.TestCase):
    def setUp(self):
        self.opt = tempfile.mkdtemp(dir=TMP)

        def fetch(d):
            with open(os.path.join(d, "server.properties"), "w") as f:
                f.write(SHIPPED)
            open(os.path.join(d, "bedrock_server"), "w").close()
        patches = [
            mock.patch.object(M, "SERVERS_ROOT", self.opt),
            mock.patch.object(M, "_reserve_name", lambda edition: ("minecraft-bedrock-2", 19134)),
            mock.patch.object(M, "_release_name", lambda name: None),
            mock.patch.object(M, "require_tools", lambda *a: None),
            mock.patch.object(M, "ensure_minecraft_user", lambda d: None),
            mock.patch.object(M, "_fetch_bedrock", fetch),
            mock.patch.object(M, "write_screenrc", lambda d: None),
            mock.patch.object(M, "run_and_log", lambda *a, **k: 0),
            mock.patch.object(M, "_write_unit", lambda *a, **k: None),
            mock.patch.object(M, "open_firewall_rules", lambda rules: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_keeps_shipped_settings_and_sets_ports(self):
        name = M.deploy_server("bedrock")
        d = os.path.join(self.opt, name)
        props = M.read_properties(d)
        self.assertEqual(props["server-port"], "19134")
        self.assertEqual(props["server-portv6"], "19135")
        self.assertEqual(props["query.port"], "19134")
        # Mojang ships server-udp-ports=19132; each server gets its own gameplay port
        self.assertEqual(props["server-udp-ports"], "19134")
        self.assertEqual(props["transport"], "nethernet")
        for key in ("server-name", "gamemode", "difficulty", "max-players", "level-name", "view-distance"):
            self.assertIn(key, props)
        self.assertEqual(props["level-name"], "Bedrock level")
        with open(os.path.join(d, "server.properties")) as f:
            self.assertIn("# Used as the server name", f.read())


class BedrockUdpPorts(unittest.TestCase):
    """server-udp-ports: counted as taken, kept by templates, and moved along with server-port."""

    def setUp(self):
        self.opt = tempfile.mkdtemp(dir=TMP)
        p = mock.patch.object(M, "SERVERS_ROOT", self.opt)
        p.start()
        self.addCleanup(p.stop)

    def server(self, name, props):
        d = os.path.join(self.opt, name)
        os.makedirs(d)
        open(os.path.join(d, "bedrock_server"), "w").close()
        with open(os.path.join(d, "server.properties"), "w") as f:
            f.write("".join(f"{k}={v}" + chr(10) for k, v in props.items()))
        return d

    def test_used_ports_count_udp_ports(self):
        self.server("minecraft-bedrock", {"server-port": 19132, "server-portv6": 19133, "server-udp-ports": 19140})
        self.server("minecraft-bedrock-2", {"server-port": 19134, "server-portv6": 19135,
                                            "server-udp-ports": "203.0.113.7:19150-19152:19150-19152"})
        used = M._used_ports()
        self.assertTrue({19132, 19133, 19140, 19150, 19151, 19152} <= used)

    def test_follow_udp_port(self):
        self.assertEqual(M._follow_udp_port({"server-udp-ports": "19132"}, "19132", 19134), "19134")
        self.assertIsNone(M._follow_udp_port({"server-udp-ports": "19140"}, "19132", 19134))
        self.assertIsNone(M._follow_udp_port({}, "19132", 19134))
        self.assertIsNone(M._follow_udp_port({"server-udp-ports": "19132"}, "19132", 19132))

    def test_restore_moves_udp_port_with_server_port(self):
        self.server("minecraft-bedrock", {"server-port": 19132, "server-portv6": 19133, "server-udp-ports": 19132})
        d = self.server("minecraft-bedrock-2", {"server-port": 19132, "server-portv6": 19133, "server-udp-ports": 19132})
        port, old = M._restore_ports(d, "bedrock")
        self.assertEqual((port, old), (19134, "19132"))
        self.assertEqual(M.read_properties(d)["server-udp-ports"], "19134")

    def test_template_keeps_the_servers_udp_port(self):
        d = self.server("minecraft-bedrock-2", {"server-port": 19134, "server-portv6": 19135, "server-udp-ports": 19134})
        template = {"properties": {"difficulty": "hard", "server-port": "19132", "server-udp-ports": "19132"},
                    "permissions": "[]"}
        with mock.patch.object(M, "get_server_dir", lambda n: d),                 mock.patch.object(M, "get_checked_template", lambda t, e: template),                 mock.patch.object(M, "write_permissions", lambda n, c: None):
            M.apply_template_to_server("minecraft-bedrock-2", "t1", edition="bedrock")
        props = M.read_properties(d)
        self.assertEqual(props["difficulty"], "hard")
        self.assertEqual(props["server-port"], "19134")
        self.assertEqual(props["server-udp-ports"], "19134")


if __name__ == "__main__":
    unittest.main()
