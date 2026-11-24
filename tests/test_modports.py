"""Ports that mods and plugins open (core/modports.py) and the crash card for a taken mod port
(core/crashes.py). The voice chat log is trimmed from a real Fabric server whose Simple Voice
Chat port another server held. Run: python -m unittest discover -s tests"""
import os
import sys
import tempfile
import unittest
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-modports-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import crashes as C  # noqa: E402
from core import modports as MP  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "crash_samples", "voicechat-port-latest.log")) as _f:
    VOICE_LOG = _f.read()

VOICE_PROPS = "# Simple Voice Chat server config\n# The port\nport=24454\nbind_address=\nmax_voice_distance=48.0\n"
GEYSER = """\
java:
  port: 25565
bedrock:
  address: 0.0.0.0
  # The port Geyser listens on
  port: 19132 # comment
  clone-remote-port: false
remote:
  port: 25565
"""
BLUEMAP = '##\n# The port\nport: 8100\nwebroot: "bluemap/web"\n'
DYNMAP = "deftemplatesuffix: hires\nwebserver-bindaddress: 0.0.0.0\nwebserver-port: 8123\n"
PLAN = "Webserver:\n  Port: 8804\n  Alternative_IP:\n    Enabled: false\n    Port: 9000\n"


class Parsing(unittest.TestCase):
    def test_each_format(self):
        self.assertEqual(MP._find(VOICE_PROPS, "properties", ("port",)), (2, "24454"))
        self.assertEqual(MP._find(GEYSER, "yaml", ("bedrock", "port")), (5, "19132"))
        self.assertEqual(MP._find(GEYSER, "yaml", ("port",)), (None, None), "never a nested port")
        self.assertEqual(MP._find(BLUEMAP, "hocon", ("port",)), (2, "8100"))
        self.assertEqual(MP._find(DYNMAP, "yaml", ("webserver-port",)), (2, "8123"))
        self.assertEqual(MP._find(PLAN, "yaml", ("Webserver", "Port")), (1, "8804"))

    def test_replace_keeps_the_rest_of_the_file(self):
        out = MP._replace(GEYSER, 5, 19133)
        self.assertIn("  port: 19133 # comment\n", out)
        self.assertEqual(out.replace("19133", "19132"), GEYSER)
        self.assertEqual(MP._replace(VOICE_PROPS, 2, 24455), VOICE_PROPS.replace("port=24454", "port=24455"))

    def test_ports_that_are_the_game_port_are_left_alone(self):
        self.assertTrue(MP._skip("voicechat", "port=-1", "-1"))
        self.assertTrue(MP._skip("geyser", GEYSER.replace("clone-remote-port: false", "clone-remote-port: true"), "19132"))
        self.assertFalse(MP._skip("geyser", GEYSER, "19132"))


def files_of(tree):
    """A stand-in for core.files.read_text over {server: {path: text}}."""
    def read(server, path):
        t = tree.get(server, {}).get(path)
        return None if t is None else {"content": t, "modified": 1}
    return read


class Claims(unittest.TestCase):
    def test_server_ports(self):
        tree = {"minecraft-a": {"plugins/voicechat/voicechat-server.properties": VOICE_PROPS,
                                "plugins/Geyser-Spigot/config.yml": GEYSER, "plugins/BlueMap/webserver.conf": BLUEMAP}}
        with mock.patch.object(MP, "_read", files_of(tree)):
            got = {(c["mod"], c["port"], c["proto"]) for c in MP.server_ports("minecraft-a")}
            self.assertEqual(got, {("voicechat", 24454, "udp"), ("geyser", 19132, "udp"), ("bluemap", 8100, "tcp")})
            self.assertEqual(MP.firewall_rules("minecraft-a"), {("24454", "udp"), ("19132", "udp"), ("8100", "tcp")})

    def test_missing_key_means_the_default(self):
        tree = {"minecraft-a": {"config/voicechat/voicechat-server.properties": "bind_address=\n"}}
        with mock.patch.object(MP, "_read", files_of(tree)):
            self.assertEqual(MP.server_ports("minecraft-a")[0]["port"], 24454)


def claim(mod, port, path="x"):
    proto = MP.KNOWN[mod][4]
    return {"mod": mod, "name": MP.KNOWN[mod][0], "path": path, "port": port, "proto": proto}


class Plan(unittest.TestCase):
    def free(self, port):
        return True

    def test_second_server_moves_to_the_next_free_port(self):
        claims = {"minecraft-java-2": [claim("voicechat", 24454)], "minecraft-java-3": [claim("voicechat", 24454)]}
        moves = MP.plan(claims, {25566, 25567}, self.free, keeper=lambda s, p: s)
        self.assertEqual([(s, c["port"], new) for s, c, new in moves], [("minecraft-java-3", 24454, 24455)])

    def test_the_server_holding_the_port_keeps_it(self):
        claims = {"minecraft-java-2": [claim("voicechat", 24454)], "minecraft-java-3": [claim("voicechat", 24454)]}
        keeper = lambda s, p: (2 if s == "minecraft-java-2" else 0, s)     # java-2's log shows it failed
        moves = MP.plan(claims, set(), self.free, keeper=keeper)
        self.assertEqual([s for s, _, _ in moves], ["minecraft-java-2"])

    def test_skips_used_and_busy_ports(self):
        claims = {"a": [claim("voicechat", 24454)], "b": [claim("voicechat", 24454)], "c": [claim("voicechat", 24455)],
                  "d": [claim("voicechat", 24454)]}
        busy = {24456}                               # held by some other program on this machine
        moves = MP.plan(claims, set(), lambda p: p not in busy, keeper=lambda s, p: s)
        self.assertEqual([(s, new) for s, _, new in moves], [("b", 24457), ("d", 24458)])

    def test_a_mod_on_a_game_port_always_moves(self):
        claims = {"a": [claim("bluemap", 25565)]}
        moves = MP.plan(claims, {25565}, self.free, keeper=lambda s, p: s)
        self.assertEqual([(s, new) for s, _, new in moves], [("a", 25566)])

    def test_nothing_to_do(self):
        claims = {"a": [claim("voicechat", 24454)], "b": [claim("voicechat", 24455), claim("geyser", 19132)]}
        self.assertEqual(MP.plan(claims, {25565, 19133}, self.free), [])


class Resolve(unittest.TestCase):
    def setUp(self):
        self.tree = {"minecraft-java-2": {"config/voicechat/voicechat-server.properties": VOICE_PROPS},
                     "minecraft-java-3": {"plugins/voicechat/voicechat-server.properties": VOICE_PROPS}}
        self.written = []

        def write(server, path, content, expected_modified=None, actor="?"):
            self.written.append((server, path))
            self.tree[server][path] = content
        from core import files
        for p in (mock.patch.object(MP, "_read", files_of(self.tree)),
                  mock.patch.object(MP, "_servers", lambda: sorted(self.tree)),
                  mock.patch.object(MP.manager, "_is_java", lambda s: True),
                  mock.patch.object(MP.manager, "_used_ports", lambda **k: {25566, 25567}),
                  mock.patch.object(MP, "_bindable", lambda p: True),
                  mock.patch.object(MP, "_active", lambda s: True),
                  mock.patch.object(MP, "_failed_here", lambda s, p: s == "minecraft-java-3"),
                  mock.patch.object(files, "write_text", write),
                  mock.patch.object(MP, "STATE", os.path.join(TMP, "mod-ports.json"))):
            p.start()
            self.addCleanup(p.stop)

    def test_moves_the_one_that_failed_and_remembers_it(self):
        done = MP.resolve()
        self.assertEqual([(m["server"], m["old"], m["new"], m["with"]) for m in done],
                         [("minecraft-java-3", 24454, 24455, ["minecraft-java-2"])])
        self.assertIn("port=24455\n", self.tree["minecraft-java-3"]["plugins/voicechat/voicechat-server.properties"])
        self.assertIn("port=24454\n", self.tree["minecraft-java-2"]["config/voicechat/voicechat-server.properties"])
        self.assertEqual(list(MP.pending()), ["minecraft-java-3"])
        text = MP.attention_text("minecraft-java-3", MP.pending()["minecraft-java-3"])
        self.assertIn("Simple Voice Chat from UDP 24454 (also set on minecraft-java-2) to 24455", text)
        self.assertEqual(MP.resolve(), [], "nothing more to do")
        MP.started("minecraft-java-3")
        self.assertEqual(MP.pending(), {})

    def test_only(self):
        self.assertEqual(MP.resolve(only=["minecraft-java-2"]), [])
        self.assertEqual(self.written, [])

    def test_missing_key_is_written(self):
        self.tree["minecraft-java-3"]["plugins/voicechat/voicechat-server.properties"] = "bind_address=\n"
        MP.resolve()
        self.assertEqual(self.tree["minecraft-java-3"]["plugins/voicechat/voicechat-server.properties"],
                         "bind_address=\nport=24455\n")


class Firewall(unittest.TestCase):
    def test_java_server_rules_include_its_mod_ports(self):
        M = MP.manager
        with mock.patch.object(M, "_is_java", lambda s: True), \
                mock.patch.object(M, "read_properties", lambda d: {"server-port": "25566"}), \
                mock.patch.object(M, "get_server_dir", lambda s: "/nonexistent"), \
                mock.patch.object(MP, "_read", files_of({"minecraft-java-2": {
                    "config/voicechat/voicechat-server.properties": VOICE_PROPS}})):
            self.assertEqual(M.server_firewall_rules("minecraft-java-2"), {("25566", "tcp"), ("24454", "udp")})
        with mock.patch.object(M, "_is_java", lambda s: False), \
                mock.patch.object(M, "read_properties", lambda d: {"server-port": "19132", "transport": "raknet"}), \
                mock.patch.object(M, "get_server_dir", lambda s: "/nonexistent"):
            self.assertEqual(M.server_firewall_rules("minecraft-bedrock"), {("19132", "udp")})


class CrashCard(unittest.TestCase):
    def test_voice_port_not_game_port(self):
        causes = C.find_causes(VOICE_LOG, {"port": "25566", "server": "minecraft-java-2"})
        self.assertEqual([c["id"] for c in causes], ["extra_port_in_use"])
        c = causes[0]
        self.assertEqual(c["title"], "Voice chat port already in use")
        self.assertIn("UDP 24454", c["text"])
        self.assertNotIn("25566", c["text"] + c["fix"])
        self.assertIn("The game port itself was fine", c["text"])
        self.assertIn("voicechat", VOICE_LOG.splitlines()[c["line"]])

    def test_names_the_other_server_and_the_move(self):
        c = C.find_causes(VOICE_LOG, {"server": "minecraft-java-2", "voice_users": ["minecraft-java-3"],
                                      "voice_config": "config/voicechat/voicechat-server.properties",
                                      "voice_moved": 24455})[0]
        self.assertIn("minecraft-java-3 on this machine is set to the same voice port", c["text"])
        self.assertIn("moved this server's voice port to 24455 in config/voicechat/voicechat-server.properties", c["fix"])

    def test_paper_plugin(self):
        log = ("[05:51:23 INFO]: Done (29.448s)! For help, type \"help\"\n"
               "[05:51:23 ERROR]: [voicechat] Failed to run voice chat at UDP port 24454, make sure no other "
               "application is running at that port\njava.net.BindException: Address already in use\n")
        self.assertEqual([c["id"] for c in C.find_causes(log)], ["extra_port_in_use"])

    def test_rcon_and_query(self):
        rcon = "[12:00:01] [Server thread/WARN]: Unable to initialise RCON on 0.0.0.0:25575\njava.net.BindException: Address already in use\n"
        c = C.find_causes(rcon)[0]
        self.assertEqual((c["id"], c["title"]), ("extra_port_in_use", "RCON port already in use"))
        self.assertIn("port 25575", c["text"])

    def test_unknown_mod(self):
        log = ("[10:00:00] [Server thread/INFO]: Done (2.1s)! For help, type \"help\"\n"
               "[10:00:01] [Worker/INFO]: [somemap] Starting web server on port 8200\n"
               "java.net.BindException: Address already in use\n")
        c = C.find_causes(log, {"port": "25565"})[0]
        self.assertEqual(c["id"], "port_in_use")
        self.assertEqual(c["title"], "A mod's port is already in use")
        self.assertIn("(port 8200)", c["text"])
        self.assertNotIn("25565", c["text"])

    def test_game_port_still_named(self):
        with open(os.path.join(HERE, "crash_samples", "port-latest.log")) as f:
            game = f.read()
        c = C.find_causes(game, {"port": "25565"})
        self.assertEqual([x["id"] for x in c], ["port_in_use"])
        self.assertIn("port 25565", c[0]["text"])


class Recovered(unittest.TestCase):
    """A crash card closes itself once the server has run cleanly for a while after it."""
    def setUp(self):
        p = mock.patch.object(C, "CRASH_DIR", tempfile.mkdtemp(dir=TMP))
        p.start()
        self.addCleanup(p.stop)
        C._save("minecraft-java-2", {"server": "minecraft-java-2", "at": 1000, "causes": [], "dismissed": False})

    def state(self, active, since):
        from core.platform import services
        return (mock.patch.object(services, "unit_state", lambda s, *a, **k: "active" if active else "failed"),
                mock.patch.object(services, "started_at", lambda s, *a, **k: since))

    def run_with(self, active, since, now):
        a, b = self.state(active, since)
        with a, b:
            return C.attention(["minecraft-java-2"], now=now)

    def test_stays_while_crashing_or_just_restarted(self):
        self.assertEqual(len(self.run_with(False, None, 5000)), 1)
        self.assertEqual(len(self.run_with(True, 1020, 1020 + 60)), 1, "up for only a minute")
        self.assertEqual(len(self.run_with(True, 900, 5000)), 1, "started before the crash")
        self.assertFalse(C.load("minecraft-java-2")["dismissed"])

    def test_closes_after_a_clean_run(self):
        self.assertEqual(self.run_with(True, 1020, 1020 + C.RECOVERED_AFTER), [])
        rec = C.load("minecraft-java-2")
        self.assertTrue(rec["dismissed"])
        self.assertEqual(rec["recovered"], 1020 + C.RECOVERED_AFTER)


if __name__ == "__main__":
    unittest.main()
