"""The Port check (1.23.1, core/portaudit.py): reading who listens on a port (Linux /proc,
Windows, Docker), finding clashes, what is fixed by itself and what is only explained, refusing
a start, and comparing the router's forwards. Run: python -m unittest discover -s tests"""
import os
import sys
import tempfile
import unittest
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-portaudit-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import manager  # noqa: E402
from core import modports as MP  # noqa: E402
from core import portaudit as PA  # noqa: E402
from core.platform.linux import system as linux_system  # noqa: E402
from core.platform.windows import system as windows_system  # noqa: E402

# /proc/net/tcp and udp from a real Ubuntu 24.04 machine, trimmed: a Java server on 25565, the
# panel on 8443 (IPv6 any), sshd on 22, a connected UDP socket (state 01, left out) and a bound one
PROC_TCP = """\
  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 00000000:63DD 00000000:0000 0A 00000000:00000000 00:00000000 00000000   999        0 41001 1 0 100 0 0 10 0
   1: 0100007F:0016 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 41002 1 0 100 0 0 10 0
   2: 0A00000A:63DD 0A000001:C350 01 00000000:00000000 00:00000000 00000000   999        0 41003 1 0 100 0 0 10 0
"""
PROC_TCP6 = """\
  sl  local_address                         remote_address                        st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 00000000000000000000000000000000:20FB 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 42001 1 0 100 0 0 10 0
"""
PROC_UDP = """\
   sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode ref pointer drops
  100: 00000000:5E36 00000000:0000 07 00000000:00000000 00:00000000 00000000   999        0 43001 2 0000000000000000 0
  101: 0A00000A:D431 08080808:0035 01 00000000:00000000 00:00000000 00000000     0        0 43002 2 0000000000000000 0
"""


def L(port, proto="tcp", **kw):
    """A listening socket as system.listeners() gives it."""
    base = {"proto": proto, "address": "0.0.0.0", "port": port, "pid": None, "program": "", "exe": "",
            "command": "", "service": "", "cwd": "", "user": ""}
    return dict(base, **kw)


JAVA = {"server-port": "25565", "enable-query": "false", "enable-rcon": "true", "rcon.port": "25575"}


class Fixture(unittest.TestCase):
    """Servers, their server.properties and what listens, all stood in."""
    servers = {}
    props = {}
    held = []

    def setUp(self):
        self.servers = {}
        self.props = {}
        self.held = []
        self.lines = {}
        self.unbindable = set()
        os.makedirs(os.environ["RP_DATA_DIR"], exist_ok=True)
        if os.path.exists(PA.STATE):
            os.remove(PA.STATE)
        patches = [
            mock.patch.object(manager, "list_servers", lambda: [dict(name=n, directory=f"/srv/{n}", **v)
                                                                for n, v in self.servers.items()]),
            mock.patch.object(manager, "read_properties", lambda d: dict(self.props[os.path.basename(d)])),
            mock.patch.object(PA, "listeners", lambda fresh=False, max_age=None: list(self.held)),
            mock.patch.object(PA, "_mod_claims", lambda name: []),
            mock.patch.object(PA, "panel_claims", lambda: [{"server": None, "kind": "panel", "key": "web",
                                                            "port": 8443, "proto": "tcp",
                                                            "label": "the panel's web page"}]),
            mock.patch.object(PA, "_ephemeral", lambda: (32768, 60999)),
            mock.patch.object(PA, "find_port_lines", lambda server, port: list(self.lines.get((server, port), []))),
            mock.patch.object(PA.system, "can_bind", lambda port, proto: (port, proto) not in self.unbindable),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def java(self, name, status="online", **props):
        self.servers[name] = {"edition": "Java", "status": status}
        self.props[name] = dict(JAVA, **props)

    def bedrock(self, name, status="online", **props):
        self.servers[name] = {"edition": "Bedrock", "status": status}
        self.props[name] = dict({"server-port": "19132", "server-udp-ports": "19132"}, **props)

    def problems(self, **kw):
        return [p for p in PA.report()["problems"] if all(p.get(k) == v for k, v in kw.items())]


class ReadingSockets(unittest.TestCase):
    def test_proc_net_listening_tcp_and_bound_udp_only(self):
        self.assertEqual(linux_system._parse_proc_net(PROC_TCP, "tcp"),
                         [("0.0.0.0", 25565, 41001), ("127.0.0.1", 22, 41002)])
        self.assertEqual(linux_system._parse_proc_net(PROC_TCP6, "tcp"), [("::", 8443, 42001)])
        self.assertEqual(linux_system._parse_proc_net(PROC_UDP, "udp"), [("0.0.0.0", 24118, 43001)])

    def test_this_machine(self):
        """The real /proc of whatever runs the tests: every row has the shape, and a socket we
        open ourselves is found with our own process id."""
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            port = s.getsockname()[1]
            if not os.path.exists("/proc/net/tcp"):
                self.skipTest("no /proc")
            found = [x for x in linux_system.listeners() if x["port"] == port and x["proto"] == "tcp"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["pid"], os.getpid())
        self.assertEqual(set(found[0]), {"proto", "address", "port", "pid", "program", "exe", "command",
                                         "service", "cwd", "user"})

    def test_windows_answer(self):
        data = {"sockets": [["tcp", "0.0.0.0", 25565, 5120], ["udp", "::", 19132, 6000], ["tcp", "0.0.0.0", 80, 4]],
                "processes": [[5120, 5000, "java.exe", "C:\\RP\\jre\\bin\\java.exe", "java -jar server.jar"],
                              [5000, 700, "python.exe", "C:\\RP\\python.exe", "runner service minecraft-java"],
                              [6000, 1, "bedrock_server.exe", "D:\\other\\bedrock_server.exe", ""]],
                "services": [[5000, "minecraft-java"], [700, "services"]]}
        got = {(r["port"], r["proto"]): r for r in windows_system.parse_listeners(data)}
        self.assertEqual(got[(25565, "tcp")]["service"], "minecraft-java", "java.exe under the runner")
        self.assertEqual(got[(19132, "udp")]["program"], "bedrock_server.exe")
        self.assertEqual(got[(19132, "udp")]["service"], "", "started by hand: no service")
        self.assertEqual(got[(80, "tcp")]["program"], "System")
        self.assertIn("netsh http show urlacl", PA.describe(got[(80, "tcp")]))

    def test_docker_published_ports(self):
        text = ("mc-old\t0.0.0.0:25565->25565/tcp, :::25565->25565/tcp\n"
                "web\t0.0.0.0:8000-8001->80-81/tcp\nquiet\t\n")
        got = PA.parse_docker(text)
        self.assertIn({"container": "mc-old", "port": 25565, "proto": "tcp", "inside": 25565}, got)
        self.assertEqual(len([g for g in got if g["container"] == "mc-old"]), 1, "IPv4 and IPv6 once")
        self.assertIn({"container": "web", "port": 8001, "proto": "tcp", "inside": 81}, got)

    def test_whose_socket(self):
        names = {"minecraft-java"}
        self.assertEqual(PA.owner(L(1, service="minecraft-java.service"), names), ("server", "minecraft-java"))
        self.assertEqual(PA.owner(L(1, service="minecraft-java"), names), ("server", "minecraft-java"), "Windows")
        self.assertEqual(PA.owner(L(1, service="redstonepanel.service"), names), ("panel", None))
        self.assertEqual(PA.owner(L(1, service="redstonepanel-sftp.service"), names), ("panel", None))
        self.assertEqual(PA.owner(L(1, service="phantom-minecraft-bedrock.service"), names), ("phantom", None))
        self.assertEqual(PA.owner(L(1, service="minecraft-java.service", container="x"), names), ("other", None))
        self.assertEqual(PA.owner(L(1, service="nginx.service"), names), ("other", None))


class Finding(Fixture):
    def test_nothing_wrong(self):
        self.java("minecraft-java")
        self.held = [L(25565, pid=10, program="java", service="minecraft-java.service"),
                     L(25575, pid=10, program="java", service="minecraft-java.service"),
                     L(8443, pid=1, service="redstonepanel.service")]
        rep = PA.report()
        self.assertEqual(rep["problems"], [])
        self.assertEqual(rep["unknown"], [])

    def test_another_program_on_the_game_port_is_named(self):
        self.java("minecraft-java", status="offline")
        self.held = [L(25565, pid=4121, program="java", exe="/usr/lib/jvm/java-21/bin/java",
                       command="java -jar paper.jar --port 25565", service="old-mc.service", user="steve")]
        [p] = self.problems(type="taken")
        self.assertEqual((p["level"], p["fix"], p["server"], p["port"]), ("error", "move", "minecraft-java", 25565))
        self.assertIn("java (process 4121, systemd unit old-mc.service, user steve)", p["text"])
        h = p["holders"][0]
        self.assertEqual(h["command"], "java -jar paper.jar --port 25565")
        self.assertEqual(h["exe"], "/usr/lib/jvm/java-21/bin/java")
        self.assertTrue(h["minecraft"], "recognised as a Minecraft server the panel didn't start")

    def test_a_holder_no_list_shows(self):
        """Found on a real PC: WSL with networkingMode=mirrored held TCP 19132 without showing in
        Get-NetTCPConnection; only a bind was refused."""
        self.bedrock("minecraft-bedrock", status="offline")
        self.unbindable = {(19132, "tcp")}
        [p] = self.problems(type="taken")
        self.assertEqual((p["port"], p["proto"], p["fix"], p["level"]), (19132, "tcp", "move", "error"))
        self.assertIn("WSL", p["text"])
        self.held = [L(19132, pid=20, service="minecraft-bedrock.service")]
        self.assertEqual(self.problems(type="taken"), [], "its own server holding it is fine")

    def test_a_server_started_after_the_list_was_made(self):
        """Found in 1.26 testing: the dashboard reuses a list of listeners for minutes; a server
        that started since then (a sleeping one that woke) isn't a holder nobody can name."""
        self.bedrock("minecraft-bedrock")
        self.unbindable = {(19132, "tcp")}
        now = [L(19132, pid=20, service="minecraft-bedrock.service")]
        def listeners(fresh=False, max_age=None):
            return list(now) if fresh else []
        with mock.patch.object(PA, "listeners", listeners):
            self.assertEqual(self.problems(type="taken"), [])

    def test_a_docker_container(self):
        self.java("minecraft-java", status="offline")
        self.held = [L(25565, program="Docker", container="mc-old", inside=25565)]
        [p] = self.problems(type="taken")
        self.assertIn("the Docker container mc-old", p["text"])

    def test_rcon_held_elsewhere_is_fixed_by_itself(self):
        self.java("minecraft-java")
        self.held = [L(25565, pid=10, service="minecraft-java.service"), L(25575, pid=99, program="mcrcon-proxy")]
        [p] = self.problems(type="taken")
        self.assertEqual((p["fix"], p["level"]), ("auto", "warn"))
        self.assertEqual(PA.attention({"problems": [p], "moved": {}}), [], "automatic fixes aren't Needs attention")

    def test_two_servers_edited_onto_one_port(self):
        self.java("minecraft-java")
        self.java("minecraft-java-2", status="offline", **{"rcon.port": "25576"})
        self.held = [L(25565, pid=10, service="minecraft-java.service")]
        [p] = self.problems(type="shared")
        self.assertEqual((p["server"], p["fix"], p["with"]), ("minecraft-java-2", "move", ["minecraft-java"]),
                         "the server holding the port keeps it")

    def test_two_rcon_ports_alike(self):
        self.java("minecraft-java")
        self.java("minecraft-java-2", **{"server-port": "25566"})
        [p] = self.problems(type="shared")
        self.assertEqual((p["kind"], p["fix"]), ("rcon", "auto"))

    def test_a_server_on_the_panels_port(self):
        self.java("minecraft-java", **{"server-port": "8443"})
        self.held = [L(8443, pid=1, service="redstonepanel.service")]
        got = self.problems(server="minecraft-java")
        self.assertTrue(got and all(p["fix"] == "move" for p in got))
        self.assertTrue(any("the panel" in p["text"] for p in got))

    def test_bedrock_lan_port_is_shared_on_purpose(self):
        self.bedrock("minecraft-bedrock")
        self.bedrock("minecraft-bedrock-2", **{"server-port": "19134", "server-udp-ports": "19134"})
        self.held = [L(7551, "udp", pid=20, service="minecraft-bedrock.service"),
                     L(7551, "udp", pid=21, service="minecraft-bedrock-2.service")]
        self.assertEqual(PA.report()["problems"], [])

    def test_a_port_a_server_opened_that_it_isnt_known_to_need(self):
        self.java("minecraft-java")
        self.held = [L(25565, pid=10, service="minecraft-java.service"),
                     L(25575, pid=10, service="minecraft-java.service"),
                     L(8090, pid=10, program="java", service="minecraft-java.service"),
                     L(45000, "udp", pid=10, service="minecraft-java.service")]      # a random port: left out
        rep = PA.report()
        self.assertEqual(rep["unknown"], [{"server": "minecraft-java", "port": 8090, "proto": "tcp",
                                           "address": "0.0.0.0", "program": "java"}])
        self.assertIn(8090, PA.learned_ports(), "no new server or mod is given it")
        self.assertNotIn(8090, PA.learned_ports(exclude="minecraft-java"))

    def test_an_unknown_mod_on_another_servers_port(self):
        self.java("minecraft-java", status="offline", **{"enable-rcon": "false"})
        self.java("minecraft-java-2", **{"server-port": "25566", "enable-rcon": "false"})
        self.held = [L(25566, pid=11, service="minecraft-java-2.service"),
                     L(25565, pid=11, program="java", service="minecraft-java-2.service")]
        self.lines[("minecraft-java-2", 25565)] = [{"path": "config/webmap.yml", "line": 3, "key": "port"}]
        [p] = self.problems(type="taken")
        self.assertEqual((p["fix"], p["by"]), ("edit", "minecraft-java-2"))
        self.assertIn("config/webmap.yml (line 3)", p["text"])
        self.lines[("minecraft-java-2", 25565)] += [{"path": "config/other.toml", "line": 9, "key": "port"}]
        [p] = self.problems(type="taken")
        self.assertIsNone(p["fix"], "two lines: explained, not changed")
        self.assertIn("config/webmap.yml:3, config/other.toml:9", p["text"])


class Starting(Fixture):
    def setUp(self):
        super().setUp()
        for name, fn in (("resolve", lambda only=None, actor="panel": []),
                         ("listeners", lambda fresh=False, max_age=None: list(self.held))):
            p = mock.patch.object(PA, name, fn)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(manager, "get_server_dir", lambda n: f"/srv/{n}")
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(manager, "_is_java", lambda n: self.servers[n]["edition"] == "Java")
        p.start()
        self.addCleanup(p.stop)

    def test_refused_while_another_program_holds_the_game_port(self):
        self.java("minecraft-java", status="offline")
        self.held = [L(25565, pid=4121, program="java", service="old-mc.service")]
        with self.assertRaises(manager.ServerError) as e:
            PA.start_check("minecraft-java")
        self.assertIn("java (process 4121, systemd unit old-mc.service)", str(e.exception))
        self.assertIn("Port check", str(e.exception))

    def test_its_own_process_is_fine(self):
        self.java("minecraft-java")
        self.held = [L(25565, pid=10, service="minecraft-java.service")]
        PA.start_check("minecraft-java")

    def test_refused_when_only_a_bind_shows_the_holder(self):
        self.bedrock("minecraft-bedrock", status="offline")
        self.unbindable = {(19132, "tcp")}
        with self.assertRaises(manager.ServerError) as e:
            PA.start_check("minecraft-bedrock")
        self.assertIn("WSL", str(e.exception))

    def test_bedrock_raknet_ipv6_port(self):
        self.bedrock("minecraft-bedrock", status="offline", transport="raknet", **{"server-portv6": "19133"})
        self.held = [L(19133, "udp", pid=7, program="bedrock_server")]
        with self.assertRaises(manager.ServerError):
            PA.start_check("minecraft-bedrock")


class ConfigLines(unittest.TestCase):
    def test_port_lines(self):
        m = PA.PORT_LINE_RE.match('  "webPort": 8100,')
        self.assertEqual((m.group("key"), m.group("port")), ("webPort", "8100"))
        m = PA.PORT_LINE_RE.match("web-port = 8123  # map")
        self.assertEqual(m.group("lead") + "8124" + m.group("tail"), "web-port = 8124  # map")
        self.assertIsNone(PA.PORT_LINE_RE.match("max-players: 20"))
        self.assertIsNone(PA.PORT_LINE_RE.match("timeout: 25565"), "only keys that say port")


class KnownModsAndOutsidePrograms(unittest.TestCase):
    def test_a_mod_port_another_program_holds_moves(self):
        claims = {"minecraft-a": [{"mod": "voicechat", "port": 24454, "proto": "udp"}]}
        moves = MP.plan(claims, {25565}, bindable=lambda p: True, keeper=lambda s, p: (0, s), held={24454})
        self.assertEqual([(s, n) for s, c, n in moves], [("minecraft-a", 24455)])
        self.assertEqual(MP.plan(claims, {25565}, bindable=lambda p: True, keeper=lambda s, p: (0, s)), [])


class Router(unittest.TestCase):
    MEMBERS = [{"local": True, "name": "home", "lan_ip": "10.0.0.193", "lan_ips": ["10.0.0.193"],
                "servers": [{"name": "minecraft-java", "rows": [
                    {"kind": "forward", "port": "25565", "inside": "25565", "proto": "tcp"},
                    {"kind": "optional", "port": "25565", "inside": "25565", "proto": "udp"}]}]}]

    def test_wrong_machine_wrong_port_off_and_left_over(self):
        maps = [{"outside": 25565, "proto": "tcp", "inside_ip": "10.0.0.12", "inside": 25565, "enabled": True,
                 "description": "old"},
                {"outside": 24454, "proto": "udp", "inside_ip": "10.0.0.193", "inside": 24454, "enabled": True,
                 "description": "voice"}]
        got = PA.compare_router(self.MEMBERS, maps)
        self.assertEqual([g["level"] for g in got], ["error", "info"])
        self.assertIn("sends TCP 25565 to 10.0.0.12, but minecraft-java is on 10.0.0.193", got[0]["text"])
        got = PA.compare_router(self.MEMBERS, [dict(maps[0], inside_ip="10.0.0.193", inside=25570)])
        self.assertIn("listens on 25565", got[0]["text"])
        got = PA.compare_router(self.MEMBERS, [dict(maps[0], inside_ip="10.0.0.193", enabled=False)])
        self.assertEqual(got[0]["level"], "warn")
        self.assertEqual(PA.compare_router(self.MEMBERS, [dict(maps[0], inside_ip="10.0.0.193")]), [])


if __name__ == "__main__":
    unittest.main()
