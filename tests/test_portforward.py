"""1.16.14 port forwarding: what to forward on the internet router per server and cluster member
(core/portforward.py), the public address from the router, the reachability check and the API.
Run: python -m unittest discover -s tests"""
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import unittest
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-ports-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from core import portforward as P  # noqa: E402
from core import cluster_client  # noqa: E402
from test_twofactor import HAVE_FASTAPI, _isolate  # noqa: E402


def java(name="minecraft-survival", port="25565", motd="Survival"):
    return {"name": name, "edition": "Java", "status": "online", "port": port, "motd": motd,
            "directory": "/nonexistent", "firewall_rules": [[port, "tcp"]]}


def bedrock(name="minecraft-bedrock", port="19132"):
    return {"name": name, "edition": "Bedrock", "status": "online", "port": port, "motd": "Bedrock",
            "directory": "/nonexistent"}


def kinds(rows):
    return {(r["port"], r["proto"], r["kind"]) for r in rows}


class Parsing(unittest.TestCase):
    def test_udp_port_pairs(self):
        cases = {"19141": [("19141", "19141")], "19141:19141": [("19141", "19141")],
                 "19200:19141": [("19200", "19141")], "203.0.113.7:19141:19141": [("19141", "19141")],
                 "21200-21300": [("21200:21300", "21200:21300")],
                 "21200-21300:19400-19500": [("21200:21300", "19400:19500")],
                 "19141,19150": [("19141", "19141"), ("19150", "19150")],
                 "": [], "abc": [], "300-200": [], "70000": []}
        for value, want in cases.items():
            self.assertEqual(P.udp_port_pairs(value), want, value)

    def test_overlaps(self):
        self.assertTrue(P.overlaps("25565", "25565"))
        self.assertTrue(P.overlaps("21200:21300", "21250"))
        self.assertFalse(P.overlaps("19132", "19133"))

    def test_geyser_port(self):
        cfg = "# Geyser\nbedrock:\n  address: 0.0.0.0\n  # The port\n  port: 19140\n  clone-remote-port: false\nremote:\n  port: 25565\n"
        self.assertEqual(P.geyser_port(cfg, "25565"), "19140")
        self.assertEqual(P.geyser_port(cfg.replace("clone-remote-port: false", "clone-remote-port: true"), "25570"),
                         "25570")
        self.assertEqual(P.geyser_port(None, "25565"), "19132", "jar without a config yet: Geyser's default")
        self.assertEqual(P.geyser_port("remote:\n  port: 25000\n", "25565"), "19132", "only the bedrock: block")

    def test_voice_port(self):
        self.assertEqual(P.voice_port("# x\nport=24460\nbind_address=\n", "25565"), "24460")
        self.assertEqual(P.voice_port("port=-1\n", "25570"), "25570")
        self.assertEqual(P.voice_port("", "25565"), "24454")

    def test_classify(self):
        self.assertEqual(P.classify("8.8.8.8"), "public")
        self.assertEqual(P.classify("192.168.0.10"), "private")
        self.assertEqual(P.classify("100.72.1.2"), "cgnat")
        self.assertIsNone(P.classify("play.example.com"))

    def test_clean_address(self):
        self.assertEqual(P.clean_address(" 8.8.8.8 "), "8.8.8.8")
        self.assertEqual(P.clean_address("[2001:db8::1]"), "2001:db8::1")
        self.assertEqual(P.clean_address("Play.Example.com."), "play.example.com")
        self.assertEqual(P.clean_address(""), "")
        for bad in ("http://x", "a b", "1.2.3", "x" * 300, "a;rm"):
            with self.assertRaises(P.ForwardError, msg=bad):
                P.clean_address(bad)


class ServerRows(unittest.TestCase):
    def setUp(self):
        self.files = {}
        p1 = mock.patch.object(P, "_read", lambda server, path: self.files.get(path))
        p2 = mock.patch.object(P, "_list", lambda server, path: [n.split("/")[-1] for n in self.files
                                                                  if n.startswith(path + "/")])
        for p in (p1, p2):
            p.start()
            self.addCleanup(p.stop)

    def test_java_plain(self):
        rows, issues = P.server_rows(java(), {"server-port": "25565"})
        self.assertEqual(kinds(rows), {("25565", "tcp", "forward")})
        self.assertEqual(rows[0]["check"], "java")
        self.assertEqual(issues, [])

    def test_java_extras(self):
        self.files = {"plugins/Geyser-Spigot.jar": "", "plugins/Geyser-Spigot/config.yml": "bedrock:\n  port: 19140\n",
                      "plugins/voicechat/voicechat-server.properties": "port=24454\n"}
        rows, _ = P.server_rows(java(), {"server-port": "25566", "enable-query": "true", "query.port": "25566",
                                         "enable-rcon": "true", "rcon.port": "25575"})
        self.assertEqual(kinds(rows), {("25566", "tcp", "forward"), ("25566", "udp", "optional"),
                                       ("25575", "tcp", "never"), ("19140", "udp", "forward"),
                                       ("24454", "udp", "forward")})

    def test_geyser_jar_only(self):
        self.files = {"mods/geyser-fabric-2.4.jar": ""}
        rows, _ = P.server_rows(java(), {"server-port": "25565"})
        self.assertIn(("19132", "udp", "forward"), kinds(rows))

    def test_bedrock_nethernet(self):
        # No transport line: NetherNet (MR !83). 7551 is LAN-only and isn't forwarded.
        rows, issues = P.server_rows(bedrock(), {"server-port": "19132", "server-udp-ports": "19132"})
        self.assertEqual(kinds(rows), {("19132", "tcp", "forward"), ("19132", "udp", "forward"),
                                       ("7551", "udp", "lan")})
        self.assertEqual(issues, [])

    def test_bedrock_public_local_udp(self):
        rows, _ = P.server_rows(bedrock(), {"server-port": "19132", "server-udp-ports": "19200:19141"})
        play = [r for r in rows if r["proto"] == "udp" and r["kind"] == "forward"][0]
        self.assertEqual((play["port"], play["inside"]), ("19200", "19141"))

    def test_bedrock_random_udp_ports(self):
        rows, issues = P.server_rows(bedrock(), {"server-port": "19132"})
        self.assertEqual(kinds(rows), {("19132", "tcp", "forward"), ("7551", "udp", "lan")})
        self.assertTrue(any("random UDP ports" in i for i in issues))

    def test_bedrock_address_form_warned(self):
        _, issues = P.server_rows(bedrock(), {"server-port": "19132", "server-udp-ports": "203.0.113.7:19141:19141"})
        self.assertTrue(any("public address" in i for i in issues))

    def test_bedrock_raknet(self):
        rows, _ = P.server_rows(bedrock(), {"server-port": "19132", "server-portv6": "19133", "transport": "raknet"})
        self.assertEqual(kinds(rows), {("19132", "udp", "forward"), ("19133", "udp", "ipv6")})
        self.assertEqual(rows[0]["check"], "raknet")

    def test_no_port(self):
        rows, issues = P.server_rows(java(port="Unknown"), {})
        self.assertEqual(rows, [])
        self.assertTrue(issues)

    def test_old_member_rows(self):
        rows = P._from_firewall_rules({"firewall_rules": [["19132", "tcp"], ["7551", "udp"], ["19132", "udp"]]})
        self.assertEqual(kinds(rows), {("19132", "tcp", "forward"), ("19132", "udp", "forward"),
                                       ("7551", "udp", "lan")})


class Conflicts(unittest.TestCase):
    def member(self, name, *servers):
        return {"name": name, "servers": [{"name": s, "rows": rows} for s, rows in servers]}

    def test_same_port_on_two_members(self):
        fwd = lambda p, q="tcp": {"port": p, "proto": q, "kind": "forward"}  # noqa: E731
        lan = {"port": "7551", "proto": "udp", "kind": "lan"}
        ms = [self.member("master", ("minecraft-a", [fwd("25565")]), ("minecraft-be", [fwd("19132"), fwd("19132", "udp"), lan])),
              self.member("node1", ("minecraft-b", [fwd("25565")]), ("minecraft-be2", [fwd("19140"), fwd("19140", "udp"), lan]))]
        c = P.conflicts(ms)
        self.assertEqual(len(c), 1, "7551 is shared but never forwarded, so it isn't a clash")
        self.assertEqual((c[0]["proto"], c[0]["port"]), ("tcp", "25565"))
        self.assertEqual({u["server"] for u in c[0]["users"]}, {"minecraft-a", "minecraft-b"})

    def test_tcp_and_udp_same_number_is_fine(self):
        ms = [self.member("m", ("minecraft-a", [{"port": "25565", "proto": "tcp", "kind": "forward"}])),
              self.member("n", ("minecraft-b", [{"port": "25565", "proto": "udp", "kind": "forward"}]))]
        self.assertEqual(P.conflicts(ms), [])


class Members(unittest.TestCase):
    def test_old_node_falls_back_to_its_server_list(self):
        node = {"id": "n-1", "name": "pi", "url": "https://192.168.1.31:8443"}

        def call(n, method, path, **kw):
            if path == "/api/port-forwarding/local":
                raise cluster_client.NodeError("too_old", "Refused", 403)
            return [bedrock("minecraft-pi") | {"firewall_rules": [["19132", "tcp"], ["19132", "udp"], ["7551", "udp"]]}]
        with mock.patch.object(cluster_client, "call_node", side_effect=call):
            rep = P._node_report(node)
        self.assertTrue(rep["old"])
        self.assertEqual(rep["url_host"], "192.168.1.31")
        self.assertEqual(kinds(rep["servers"][0]["rows"]), {("19132", "tcp", "forward"), ("19132", "udp", "forward"),
                                                            ("7551", "udp", "lan")})

    def test_unreachable_node(self):
        node = {"id": "n-1", "name": "pi", "url": "https://192.168.1.31:8443"}
        with mock.patch.object(cluster_client, "call_node",
                               side_effect=cluster_client.NodeError("unreachable", "timed out")):
            rep = P._node_report(node)
        self.assertEqual(rep["state"], "unreachable")
        self.assertEqual(rep["servers"], [])

    def test_node_asked_as_admin(self):
        node = {"id": "n-1", "name": "pi", "url": "https://192.168.1.31:8443"}
        seen = {}

        def call(n, method, path, **kw):
            seen.update(kw, path=path)
            return {"lan_ip": "192.168.1.31", "servers": [], "machine": []}
        with mock.patch.object(cluster_client, "call_node", side_effect=call):
            rep = P._node_report(node, actor="alex")
        self.assertEqual((seen["path"], seen["role"], seen["actor"]), ("/api/port-forwarding/local", "admin", "alex"))
        self.assertEqual(rep["lan_ip"], "192.168.1.31")


class Network(unittest.TestCase):
    def test_gateway_from_proc(self):
        route = ("Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\n"
                 "eth0\t0001A8C0\t00000000\t0001\t0\t0\t0\t00FFFFFF\n"
                 "eth0\t00000000\t0101A8C0\t0003\t0\t0\t100\t00000000\n")
        with mock.patch("builtins.open", mock.mock_open(read_data=route)):
            self.assertEqual(P.default_gateway(), "192.168.1.1")

    def _udp_server(self, reply):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        s.settimeout(3)

        def run():
            try:
                data, src = s.recvfrom(2048)
                s.sendto(reply(data), src)
            except OSError:
                pass
            finally:
                s.close()
        threading.Thread(target=run, daemon=True).start()
        return s.getsockname()[1]

    def test_natpmp(self):
        port = self._udp_server(lambda d: bytes([0, 128]) + b"\x00\x00" + b"\x00\x00\x01\x00"
                                + socket.inet_aton("203.0.113.7"))
        self.assertEqual(P.natpmp_address("127.0.0.1", port=port), "203.0.113.7")

    def test_natpmp_error_result(self):
        port = self._udp_server(lambda d: bytes([0, 128]) + b"\x00\x03" + b"\x00" * 8)
        self.assertIsNone(P.natpmp_address("127.0.0.1", port=port))

    def test_raknet_ping(self):
        def pong(d):
            self.assertEqual(d[0], 0x01)
            info = b"MCPE;Family Island;800;1.26.66;0;10;123;world;Survival;1;19132;19133;"
            return b"\x1c" + d[1:9] + b"\x00" * 8 + P.RAKNET_MAGIC + struct.pack(">H", len(info)) + info
        port = self._udp_server(pong)
        self.assertEqual(P.raknet_status("127.0.0.1", port), {"motd": "Family Island", "version": "1.26.66"})

    def test_java_status(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        srv.settimeout(3)

        def run():
            c, _ = srv.accept()
            with c:
                c.recv(1024)
                body = json.dumps({"version": {"name": "1.21.4"},
                                   "description": {"text": "§aSurvival", "extra": [{"text": " world"}]}}).encode()
                pkt = b"\x00" + P._varint(len(body)) + body
                c.sendall(P._varint(len(pkt)) + pkt)
            srv.close()
        threading.Thread(target=run, daemon=True).start()
        got = P.java_status("127.0.0.1", srv.getsockname()[1])
        self.assertEqual(got, {"motd": "Survival world", "version": "1.21.4"})

    def test_check_reports_each_forward(self):
        rep = {"public_address": "127.0.0.1", "members": [{"id": "local", "servers": [
            {"name": "minecraft-a", "motd": "Survival", "rows": [
                {"port": "25565", "proto": "tcp", "kind": "forward", "check": "java"},
                {"port": "24454", "proto": "udp", "kind": "forward", "check": None},
                {"port": "25575", "proto": "tcp", "kind": "never", "check": None}]}]}]}
        with mock.patch.dict(P.CHECKS, {"java": lambda h, p: {"motd": "Survival", "version": "1.21"}}):
            out = P.check(rep)
        res = {r["port"]: r for r in out["results"]}
        self.assertEqual(set(res), {"25565", "24454"}, "only rows to forward are tried")
        self.assertEqual(res["25565"]["result"], "reached")
        self.assertTrue(res["25565"]["same_server"])
        self.assertEqual(res["24454"]["result"], "unchecked")

    def test_check_no_answer(self):
        rep = {"public_address": "127.0.0.1", "members": [{"id": "local", "servers": [
            {"name": "minecraft-a", "motd": "x", "rows": [{"port": "1", "proto": "tcp", "kind": "forward", "check": "tcp"}]}]}]}
        self.assertEqual(P.check(rep)["results"][0]["result"], "no_answer")

    def test_check_needs_address(self):
        with self.assertRaises(P.ForwardError):
            P.check({"public_address": "", "members": []})


@unittest.skipUnless(HAVE_FASTAPI, "needs fastapi")
class Api(unittest.TestCase):
    PASSWORD = "correct horse"

    def setUp(self):
        import main
        from fastapi.testclient import TestClient
        d = _isolate(self, auth=main.auth, settings=main.panel_settings)
        for p in (mock.patch.object(P.manager, "DATA_DIR", d),
                  mock.patch.object(P.manager, "list_servers", lambda: [java()]),
                  mock.patch.object(P.manager, "read_properties", lambda d: {"server-port": "25565"}),
                  mock.patch.object(P.manager, "phantom_firewall_rules", lambda: set()),
                  mock.patch.object(P, "_read", lambda s, p: None), mock.patch.object(P, "_list", lambda s, p: [])):
            p.start()
            self.addCleanup(p.stop)
        main.auth.create_panel_user("alex", self.PASSWORD, "admin")
        main.auth.create_panel_user("sam", self.PASSWORD, "user")
        self.admin, self.user = TestClient(main.app), TestClient(main.app)
        for c, n in ((self.admin, "alex"), (self.user, "sam")):
            self.assertEqual(c.post("/api/auth/login", json={"username": n, "password": self.PASSWORD}).status_code, 200)

    def test_overview(self):
        r = self.admin.get("/api/port-forwarding")
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual(d["members"][0]["id"], "local")
        self.assertTrue(d["members"][0]["name"], "the page labels each member by name")
        self.assertEqual(kinds(d["members"][0]["servers"][0]["rows"]), {("25565", "tcp", "forward")})
        self.assertIn("web", d["members"][0])

    def test_admins_only(self):
        for method, path in (("get", "/api/port-forwarding"), ("get", "/api/port-forwarding/local"),
                             ("put", "/api/port-forwarding/settings"), ("post", "/api/port-forwarding/lookup"),
                             ("post", "/api/port-forwarding/check")):
            kw = {"json": {}} if method == "put" else {}
            self.assertEqual(getattr(self.user, method)(path, **kw).status_code, 403, path)

    def test_public_address(self):
        r = self.admin.put("/api/port-forwarding/settings", json={"public_address": "Play.Example.com"})
        self.assertEqual(r.json(), {"public_address": "play.example.com"})
        self.assertEqual(self.admin.get("/api/port-forwarding").json()["public_address"], "play.example.com")
        self.assertEqual(self.admin.put("/api/port-forwarding/settings", json={"public_address": "a b"}).status_code, 400)
        self.assertEqual(self.admin.get("/api/port-forwarding").json()["public_kind"], None)
        self.admin.put("/api/port-forwarding/settings", json={"public_address": "100.70.0.1"})
        self.assertEqual(self.admin.get("/api/port-forwarding").json()["public_kind"], "cgnat")

    def test_lookup_without_router(self):
        with mock.patch.object(P, "default_gateway", lambda: "127.0.0.1"), \
                mock.patch.object(P, "natpmp_address", lambda gw: None), \
                mock.patch.object(P, "upnp_address", lambda gw: None):
            r = self.admin.post("/api/port-forwarding/lookup")
        self.assertEqual(r.status_code, 400)
        self.assertIn("type it in", r.json()["detail"])

    def test_lookup_natpmp(self):
        with mock.patch.object(P, "default_gateway", lambda: "192.168.1.1"), \
                mock.patch.object(P, "natpmp_address", lambda gw: "81.2.69.160"):
            r = self.admin.post("/api/port-forwarding/lookup")
        self.assertEqual(r.json(), {"address": "81.2.69.160", "method": "NAT-PMP", "gateway": "192.168.1.1",
                                    "kind": "public"})


if __name__ == "__main__":
    unittest.main()
