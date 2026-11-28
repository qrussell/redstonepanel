"""World maps (1.30): finding a server's map tool, keeping its web server on this machine,
serving the map on the join site's address (never the panel's), the map cookie, join page maps
without players, and the panel's own drawing. Run: python -m unittest discover -s tests"""
import gzip
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-worldmap-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import auth                  # noqa: E402
from core import backups               # noqa: E402
from core import cluster               # noqa: E402
from core import grants as G           # noqa: E402
from core import joinpage as J         # noqa: E402
from core import manager as M          # noqa: E402
from core import mapdraw as D          # noqa: E402
from core import mapsite as S          # noqa: E402
from core import modports as MP        # noqa: E402
from core import worldmap as W         # noqa: E402

BLUEMAP_WEB = 'accept-download: false\nip: "0.0.0.0"\nport: 8100\nwebroot: "bluemap/web"\n'
SQUAREMAP = "settings:\n  internal-webserver:\n    enabled: true\n    bind: 0.0.0.0\n    port: 8080\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rp-map-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.server = os.path.join(self.tmp, "minecraft-java")
        os.makedirs(self.server)
        self.jars = []
        patches = [
            mock.patch.object(M, "_check_server", lambda s: None),
            mock.patch.object(M, "get_server_dir", lambda s: self.server),
            mock.patch.object(M, "write_log", lambda *a, **k: None),
            mock.patch.object(W, "SETTINGS_FILE", os.path.join(self.tmp, "world-maps.json")),
            mock.patch.object(W, "_jar_ids", lambda s: set(self.jars)),
            mock.patch.object(W, "_kind", lambda s: ("paper", None)),
            mock.patch.object(W, "_running", lambda s: False),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def put(self, rel, data):
        path = os.path.join(self.server, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data if isinstance(data, bytes) else data.encode())
        return path

    def text(self, rel):
        with open(os.path.join(self.server, rel)) as f:
            return f.read()

    def body(self, reply):
        if reply.chunks is None:
            return reply.body
        try:
            return b"".join(reply.chunks)
        finally:
            reply.close()


class ConfigText(unittest.TestCase):
    def test_set_value_keeps_the_rest_of_the_line(self):
        out = W.set_value('ip: "0.0.0.0" # who can reach it\nport: 8100\n', "hocon", ("ip",), "127.0.0.1")
        self.assertEqual(out, 'ip: "127.0.0.1" # who can reach it\nport: 8100\n')

    def test_nested_yaml(self):
        out = W.set_value(SQUAREMAP, "yaml", ("settings", "internal-webserver", "bind"), "127.0.0.1", False)
        self.assertIn("    bind: 127.0.0.1\n", out)
        self.assertIn("    port: 8080\n", out)

    def test_missing_nested_key_waits_for_the_tool(self):
        self.assertIsNone(W.set_value("settings:\n  other: 1\n", "yaml",
                                      ("settings", "internal-webserver", "bind"), "127.0.0.1"))

    def test_missing_top_level_key_is_added(self):
        self.assertEqual(W.set_value("port: 8100\n", "hocon", ("ip",), "127.0.0.1"), 'port: 8100\nip: "127.0.0.1"\n')

    def test_listens_locally(self):
        self.assertTrue(W.listens_locally("bluemap", 'ip: "127.0.0.1"\n'))
        self.assertTrue(W.listens_locally("dynmap", "webserver-bindaddress: localhost\n"))
        self.assertFalse(W.listens_locally("bluemap", 'ip: "0.0.0.0"\n'))
        self.assertFalse(W.listens_locally("nothing", 'ip: "127.0.0.1"\n'))


class Finding(Base):
    def test_nothing(self):
        self.assertEqual(W.detect("minecraft-java"), [])
        self.assertIsNone(W.current("minecraft-java"))

    def test_bluemap_by_config(self):
        self.put("plugins/BlueMap/webserver.conf", BLUEMAP_WEB)
        found = W.detect("minecraft-java")
        self.assertEqual([f["id"] for f in found], ["bluemap"])
        self.assertEqual(found[0]["port"], 8100)
        self.assertFalse(found[0]["local"])

    def test_squaremap_by_jar_before_config(self):
        self.jars = ["squaremap"]
        self.assertEqual(W.current("minecraft-java")["id"], "squaremap")

    def test_web_folder_never_leaves_the_server(self):
        self.put("plugins/BlueMap/webserver.conf", BLUEMAP_WEB.replace("bluemap/web", "../../etc"))
        found = W.detect("minecraft-java")[0]
        self.assertNotIn("..", found["web"])

    def test_drawn_map_for_vanilla(self):
        W._store("minecraft-java", drawn=True)
        cur = W.current("minecraft-java", kind="vanilla")
        self.assertTrue(cur["drawn"])
        self.assertEqual(cur["web"], W.DRAWN_DIR + "/web")
        self.assertIsNone(W.current("minecraft-java", kind="paper"))     # a plugin server uses its plugin


class KeepingItLocal(Base):
    def test_sync_moves_the_web_server_to_this_machine(self):
        self.put("plugins/BlueMap/webserver.conf", BLUEMAP_WEB)
        W._store("minecraft-java", local_only=True)
        with mock.patch.object(W, "_bluemap_reload", lambda s: None):
            W.sync("minecraft-java")
        self.assertIn('ip: "127.0.0.1"', self.text("plugins/BlueMap/webserver.conf"))

    def test_a_map_added_by_hand_keeps_its_address(self):
        self.put("plugins/BlueMap/webserver.conf", BLUEMAP_WEB)
        with mock.patch.object(W, "_bluemap_reload", lambda s: None):
            W.sync("minecraft-java")
        self.assertIn('ip: "0.0.0.0"', self.text("plugins/BlueMap/webserver.conf"))

    def test_textures_only_after_the_admin_agreed(self):
        self.put("plugins/BlueMap/webserver.conf", BLUEMAP_WEB)
        self.put("plugins/BlueMap/core.conf", "accept-download: false\n")
        with mock.patch.object(W, "_bluemap_reload", lambda s: None):
            W.sync("minecraft-java")
            self.assertIn("accept-download: false", self.text("plugins/BlueMap/core.conf"))
            W._store("minecraft-java", accept_download=True)
            W.sync("minecraft-java")
        self.assertIn("accept-download: true", self.text("plugins/BlueMap/core.conf"))

    def test_a_local_map_port_is_never_opened(self):
        self.put("plugins/BlueMap/webserver.conf", BLUEMAP_WEB.replace("0.0.0.0", "127.0.0.1"))
        ports = [p for p in MP.server_ports("minecraft-java") if p.get("mod") == "bluemap"]
        self.assertEqual([p["local"] for p in ports], [True])
        self.assertEqual(MP.firewall_rules("minecraft-java"), set())


class Serving(Base):
    def setUp(self):
        super().setUp()
        self.put("plugins/BlueMap/webserver.conf", BLUEMAP_WEB)

    def test_first_page(self):
        self.put("bluemap/web/index.html", "<html>map</html>")
        r = W.serve_local("minecraft-java", "")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.headers["Content-Type"], "text/html; charset=utf-8")
        self.assertEqual(self.body(r), b"<html>map</html>")

    def test_gzip_tile(self):
        self.put("bluemap/web/maps/world/tiles/0/x1/z2.prbm.gz", gzip.compress(b"tile"))
        r = W.serve_local("minecraft-java", "maps/world/tiles/0/x1/z2.prbm")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.headers["Content-Encoding"], "gzip")
        self.assertEqual(gzip.decompress(self.body(r)), b"tile")

    def test_tile_not_drawn_yet(self):
        self.assertEqual(W.serve_local("minecraft-java", "maps/world/tiles/0/x9/z9.prbm").status, 204)

    def test_resting_server_has_empty_live_data(self):
        r = W.serve_local("minecraft-java", "maps/world/live/players.json")
        self.assertEqual(json.loads(r.body), {"players": []})

    def test_players_hidden_for_join_visitors(self):
        live = {"players": [{"name": "Alex", "position": {"x": 1}}], "other": 1}
        self.put("bluemap/web/maps/world/live/players.json", json.dumps(live))
        shown = json.loads(self.body(W.serve_local("minecraft-java", "maps/world/live/players.json")))
        self.assertEqual(shown["players"][0]["name"], "Alex")
        hidden = json.loads(self.body(W.serve_local("minecraft-java", "maps/world/live/players.json", players=False)))
        self.assertEqual(hidden, {"players": [], "other": 1})

    def test_dynmap_updates_lose_players_and_chat(self):
        d = {"currentcount": 2, "players": [{"name": "Alex"}],
             "updates": [{"type": "tile", "name": "t"}, {"type": "chat", "message": "hi"}]}
        self.assertEqual(json.loads(W._no_players(json.dumps(d).encode())),
                         {"currentcount": 0, "players": [], "updates": [{"type": "tile", "name": "t"}]})

    @unittest.skipIf(os.name == "nt", "symlinks")
    def test_symlink_out_of_the_server_is_refused(self):
        outside = os.path.join(self.tmp, "secret.txt")
        with open(outside, "w") as f:
            f.write("secret")
        os.makedirs(os.path.join(self.server, "bluemap/web"), exist_ok=True)
        os.symlink(outside, os.path.join(self.server, "bluemap/web/leak.txt"))
        r = W.serve_local("minecraft-java", "leak.txt")
        self.assertNotEqual(r.status, 200)
        self.assertNotIn(b"secret", self.body(r) or b"")

    def test_only_looking(self):
        self.assertEqual(W.serve_local("minecraft-java", "", method="POST").status, 405)

    def test_no_map(self):
        os.remove(os.path.join(self.server, "plugins/BlueMap/webserver.conf"))
        self.assertEqual(W.serve_local("minecraft-java", "").status, 404)


class Cookie(Base):
    USER = {"username": "alex", "source": "panel", "role": "admin"}

    def setUp(self):
        super().setUp()
        p = mock.patch.object(auth, "account_role", lambda src, name: "admin" if name == "alex" else None)
        p.start()
        self.addCleanup(p.stop)

    def test_ticket_is_for_one_map(self):
        t = W.make_ticket("local", "minecraft-java", self.USER)
        self.assertTrue(W.read_ticket(t, "local", "minecraft-java"))
        self.assertIsNone(W.read_ticket(t, "local", "minecraft-other"))
        self.assertIsNone(W.read_ticket(t, "n-abcd", "minecraft-java"))

    def test_ticket_is_not_a_cookie_and_cookie_is_not_a_session(self):
        t = W.make_ticket("local", "minecraft-java", self.USER)
        self.assertIsNone(W.read_viewer(t, "local", "minecraft-java"))
        v = W.make_viewer(W.read_ticket(t, "local", "minecraft-java"))
        self.assertIsNone(W.read_ticket(v, "local", "minecraft-java"))
        self.assertEqual(W.read_viewer(v, "local", "minecraft-java")["username"], "alex")

    def test_ticket_expires(self):
        t = W.make_ticket("local", "minecraft-java", self.USER)
        with mock.patch.object(time, "time", lambda: 10 ** 12):
            self.assertIsNone(W.read_ticket(t, "local", "minecraft-java"))

    def test_removed_account_loses_the_map(self):
        v = W.make_viewer(W.read_ticket(W.make_ticket("local", "minecraft-java", dict(self.USER, username="gone")),
                                        "local", "minecraft-java"))
        self.assertIsNone(W.read_viewer(v, "local", "minecraft-java"))

    def test_site_flow(self):
        self.put("plugins/BlueMap/webserver.conf", BLUEMAP_WEB)
        self.put("bluemap/web/index.html", "<html>map</html>")
        S._rate.clear()
        base = "/map/local/minecraft-java/"
        self.assertEqual(S.handle("GET", base, "", "198.51.100.1", "", {}, True).status, 403)
        t = W.make_ticket("local", "minecraft-java", self.USER)
        r = S.handle("GET", base, "t=" + t, "198.51.100.1", "", {}, True)
        self.assertEqual(r.status, 303)
        cookie = r.headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn(f"Path={base}", cookie)
        self.assertIn("Secure", cookie)
        value = cookie.split(";")[0].split("=", 1)[1]
        r = S.handle("GET", base, "", "198.51.100.1", f"{S.MAP_COOKIE}={value}", {}, True)
        self.assertEqual(r.status, 200)
        self.assertIn("frame-ancestors", r.headers["Content-Security-Policy"])
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        other = S.handle("GET", "/map/local/minecraft-other/", "", "198.51.100.1", f"{S.MAP_COOKIE}={value}", {}, True)
        self.assertEqual(other.status, 403)
        self.assertEqual(S.handle("POST", base, "", "198.51.100.1", "", {}, True).status, 405)

    def test_paths(self):
        self.assertTrue(S.is_map_path("/map/local/minecraft-java/"))
        self.assertTrue(S.is_map_path("/join/survival/map/"))
        self.assertFalse(S.is_map_path("/map/local/../etc/"))
        self.assertFalse(S.is_map_path("/api/servers"))
        self.assertIsNone(S._sub("/a/../b"))
        self.assertIsNone(S._sub("/a%2F..%2Fb"))


class JoinPages(unittest.TestCase):
    KEY = "local/minecraft-java"

    def cfg(self, **kw):
        return dict(J.defaults("minecraft-java"), **kw)

    def test_map_is_off_until_the_page_shows_it(self):
        self.assertFalse(J.abilities(self.cfg(), self.KEY, None)["map"])
        self.assertTrue(J.abilities(self.cfg(show_map=True), self.KEY, None)["map"])

    def test_by_permission_the_map_needs_its_tick(self):
        rows = [{"who": J.ANYONE, "see": True, "start": False, "stop": False, "map": False}]
        cfg = self.cfg(show_map=True, access="permission", rules=rows)
        self.assertFalse(J.abilities(cfg, self.KEY, None)["map"])
        cfg["rules"][0]["map"] = True
        self.assertTrue(J.abilities(cfg, self.KEY, None)["map"])
        cfg["show_map"] = False
        self.assertFalse(J.abilities(cfg, self.KEY, None)["map"])

    def test_players_hidden_by_default(self):
        self.assertFalse(self.cfg()["map_players"])

    def test_nothing_includes_map(self):
        self.assertIn("map", J.NOTHING)


class Plumbing(unittest.TestCase):
    def test_map_tiles_are_left_out_of_backups(self):
        for p in ("bluemap/web/maps/world/tiles/0/x.prbm", "plugins/squaremap/web/tiles/world/0/0_0.png"):
            self.assertTrue(any(p.startswith(x) for x in backups.SKIP_MAP_PREFIXES), p)

    def test_node_map_files_are_cluster_only(self):
        self.assertEqual(G.rule_for("GET", "/api/cluster/maps/{server_name}/{path:path}"), "node")
        self.assertFalse(cluster.proxy_allowed("GET", "/api/cluster/maps/minecraft-java/index.html"))

    def test_routes(self):
        self.assertEqual(G.rule_for("GET", "/api/servers/{server_name}/map"), "view")
        self.assertEqual(G.rule_for("POST", "/api/servers/{server_name}/map/add"), "admin")
        self.assertEqual(G.rule_for("POST", "/api/servers/{server_name}/map/draw"), "settings")


class Drawing(Base):
    def test_world_stamp_changes_with_the_world(self):
        world = os.path.join(self.server, "world")
        self.put("world/region/r.0.0.mca", b"a")
        first = D.world_stamp(world)
        self.assertEqual(first, D.world_stamp(world))
        os.utime(os.path.join(world, "region/r.0.0.mca"), (time.time() + 60, time.time() + 60))
        self.assertNotEqual(first, D.world_stamp(world))

    def test_timer_unit(self):
        text = D._timer_unit("minecraft-java", "03:15")
        self.assertIn("*-*-* 03:15:00", text)

    def test_pins_are_checked(self):
        for tool in (D.BLUEMAP, D.CHUNKER):
            self.assertTrue(tool["url"].startswith("https://github.com/"), tool)


if __name__ == "__main__":
    unittest.main()
