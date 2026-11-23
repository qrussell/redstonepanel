"""Join pages (1.21.5): what a page may say, and that the public port answers nothing else."""
import base64
import json
import os
import shutil
import socket
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="rp-test-join-")
os.environ.setdefault("RP_DATA_DIR", os.path.join(_TMP, "data"))
os.environ.setdefault("RP_LOG_FILE", os.path.join(_TMP, "panel.log"))

from core import joinpage as J        # noqa: E402
from core import joinsite as S        # noqa: E402
from core import manager              # noqa: E402
from core import portforward          # noqa: E402
from core import settings as P        # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40


def summary(**kw):
    base = {"name": "minecraft-survival", "edition": "Java", "loader": "paper", "minecraft": "1.21.11",
            "status": "online", "port": "25565", "motd": "Family <b>World</b>", "transport": None,
            "geyser_port": "", "modpack": None, "icon": base64.b64encode(PNG).decode(),
            "players": {"online": 2, "max": 20, "names": ["Alex", "Steve"]}}
    base.update(kw)
    return base


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rp-join-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.summaries = {"minecraft-survival": summary()}
        self.settings = dict(P.DEFAULTS, join_enabled=True)
        patches = [
            mock.patch.object(manager, "DATA_DIR", self.dir),
            mock.patch.object(manager, "write_log", lambda *a, **k: None),
            mock.patch.object(P, "load", lambda: dict(self.settings)),
            mock.patch.object(J, "summary", lambda key, fresh=False: self._summary(key)),
            mock.patch.object(portforward, "load", lambda: {"public_address": "play.example.org"}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        S._rate.clear()
        J._summary_cache.clear()

    def _summary(self, key):
        if key not in self.summaries:
            raise J.JoinError("gone")
        return self.summaries[key]

    def on(self, server="minecraft-survival", member="local", **kw):
        return J.save_page(member, server, dict({"enabled": True}, **kw), "admin")

    def get(self, path, method="GET", ip="203.0.113.9"):
        return S.handle(method, path, ip)


class Settings(Base):
    def test_address_forms(self):
        self.assertEqual(J.clean_address(" Play.Example.org "), "play.example.org")
        self.assertEqual(J.clean_address("play.example.org:25570"), "play.example.org:25570")
        self.assertEqual(J.clean_address("203.0.113.5:19132"), "203.0.113.5:19132")
        self.assertEqual(J.clean_address("[2001:db8::1]:25565"), "[2001:db8::1]:25565")
        self.assertEqual(J.clean_address("2001:db8::1"), "[2001:db8::1]")
        self.assertEqual(J.clean_address(""), "")
        for bad in ("http://x.example", "x.example:0", "x.example:99999", "a b", "<script>", "x.example/path"):
            with self.assertRaises(J.JoinError, msg=bad):
                J.clean_address(bad)

    def test_links_must_be_web_links(self):
        self.assertEqual(J.clean_url("https://discord.gg/abc", "x"), "https://discord.gg/abc")
        for bad in ("javascript:alert(1)", "data:text/html,x", "ftp://x", "https://u:p@x.example", "//x.example",
                    'https://x.example/"onmouseover="x', "https://x.example/ y", "minecraft://x"):
            with self.assertRaises(J.JoinError, msg=bad):
                J.clean_url(bad, "x")

    def test_default_link_name_and_clashes(self):
        self.assertEqual(J.default_slug("minecraft-Survival_2"), "survival-2")
        self.assertEqual(J.default_slug("minecraft"), "minecraft")
        cfg = self.on()
        self.assertEqual(cfg["slug"], "survival")
        with self.assertRaises(J.JoinError):
            self.on("minecraft-other", slug="survival")
        for bad in ("Has Space", "assets", "-x", "x" * 41, "ü"):
            with self.assertRaises(J.JoinError, msg=bad):
                self.on(slug=bad)
        # a node's server with the same name gets its own key
        self.assertNotEqual(J.page_key("n-abc123", "minecraft-survival"), J.page_key("local", "minecraft-survival"))
        self.assertEqual(J.split_key("n-abc123/minecraft-survival"), ("n-abc123", "minecraft-survival"))

    def test_unlisted_page_answers_only_to_its_random_link(self):
        cfg = self.on(unlisted=True)
        token = cfg["token"]
        self.assertGreaterEqual(len(token), 16)
        self.assertEqual(self.get("/join/survival").status, 404)
        self.assertEqual(self.get(f"/join/{token}").status, 200)
        self.assertEqual(J.listed(), [])
        # saving again keeps the link; asking for a new one changes it
        self.assertEqual(self.on(unlisted=True)["token"], token)
        self.assertNotEqual(self.on(unlisted=True, new_link=True)["token"], token)
        self.assertEqual(self.get(f"/join/{token}").status, 404)

    def test_file_is_private_and_forget_removes_the_page(self):
        self.on()
        self.assertEqual(os.stat(os.path.join(self.dir, J.DATA_FILE)).st_mode & 0o777, 0o600)
        J.forget("local", "minecraft-survival")
        self.assertEqual(J.load(), {})

    def test_panel_settings(self):
        ok = P.validate(dict(P.DEFAULTS, join_enabled=True, join_port=8444))
        self.assertTrue(ok["join_enabled"])
        for port in (P.DEFAULTS["http_port"], P.DEFAULTS["https_port"], 22, 0, 70000, "x"):
            with self.assertRaises(P.SettingsError, msg=port):
                P.validate(dict(P.DEFAULTS, join_enabled=True, join_port=port))
        with self.assertRaises(P.SettingsError):
            P.validate(dict(P.DEFAULTS, join_enabled=True, join_port=2022, sftp_enabled=True, sftp_port=2022))
        self.assertFalse(P.DEFAULTS["join_enabled"])


class PublicView(Base):
    ALLOWED = {"path", "title", "edition", "loader", "loader_key", "minecraft", "needs", "status", "players",
               "address", "geyser", "pack", "note", "link", "has_icon"}

    def test_only_the_fixed_fields_leave(self):
        s = summary(directory="/opt/minecraft-survival", rcon_port="25575", lan_ip="192.168.1.20", node="Basement",
                    java_path="/usr/lib/jvm/x", firewall_rules=[["25565", "tcp"]])
        view = J.public_view(self.on(), s)
        self.assertEqual(set(view), self.ALLOWED)
        text = json.dumps(view)
        for secret in ("/opt/", "25575", "192.168.1.20", "Basement", "/usr/lib", "minecraft-survival", "Alex"):
            self.assertNotIn(secret, text)

    def test_player_names_only_when_switched_on(self):
        self.assertIsNone(J.public_view(self.on(), summary())["players"]["names"])
        view = J.public_view(self.on(show_players=True), summary())
        self.assertEqual(view["players"], {"online": 2, "max": 20, "names": ["Alex", "Steve"]})
        self.assertIsNone(J.public_view(self.on(show_players=True), summary(status="offline"))["players"])

    def test_addresses(self):
        cfg = self.on()
        self.assertEqual(J.addresses(cfg, summary())["java"], "play.example.org")
        self.assertEqual(J.addresses(cfg, summary(port="25570"))["java"], "play.example.org:25570")
        self.assertEqual(J.addresses(self.on(address="mc.example.net:30000"), summary())["java"], "mc.example.net:30000")
        self.assertEqual(J.addresses(cfg, summary(geyser_port="19132"))["bedrock"],
                         {"host": "play.example.org", "port": "19132"})
        b = J.addresses(cfg, summary(edition="Bedrock", loader="", port="19140"))
        self.assertEqual((b["java"], b["bedrock"]), ("", {"host": "play.example.org", "port": "19140"}))
        # nothing typed anywhere: nothing is published
        self.assertEqual(J.addresses(cfg, summary(), public_address=""), {"host": "", "java": "", "bedrock": None})

    def test_pack_links(self):
        mod = summary(loader="fabric", modpack={"name": "Cobblemon", "version": "1.6", "source": "modrinth",
                                                 "slug": "cobblemon-fabric", "version_id": "AbC123xy"})
        pack = J.public_view(self.on(), mod)["pack"]
        self.assertEqual(pack["url"], "https://modrinth.com/modpack/cobblemon-fabric/version/AbC123xy")
        self.assertEqual(pack["app_url"], "modrinth://modpack/cobblemon-fabric")
        # a record that was tampered with never becomes a link
        bad = summary(loader="fabric", modpack={"name": "X", "version": "1", "source": "modrinth",
                                                 "slug": "x/../../evil?", "version_id": "1"})
        self.assertEqual(J.public_view(self.on(), bad)["pack"]["url"], "")
        up = summary(loader="forge", modpack={"name": "All the Mods", "version": "", "source": "file", "slug": "",
                                               "version_id": ""})
        self.assertEqual(J.public_view(self.on(), up)["pack"]["source"], "none")
        linked = J.public_view(self.on(pack_url="https://www.curseforge.com/minecraft/modpacks/atm10"), up)["pack"]
        self.assertEqual((linked["source"], linked["name"]), ("link", "All the Mods"))
        self.assertIsNone(J.public_view(self.on(), summary(loader="fabric"))["pack"])
        # plugin servers never show a pack, whatever was typed
        self.assertIsNone(J.public_view(self.on(pack_url="https://x.example/pack"), summary())["pack"])


class PublicPort(Base):
    def test_everything_but_join_is_not_found(self):
        self.on()
        for path in ("/", "/api/health", "/api/panel-info", "/api/auth/login", "/api/servers", "/static/index.html",
                     "/static/js/app.js", "/static/login.html", "/docs", "/openapi.json", "/join/../api/health",
                     "/join/survival/../../api", "/join/assets/../join.css", "/join/assets/app.js",
                     "/join/assets/join.css/x", "/join/survival/status.json/x", "/join/survival/other",
                     "/join/nope", "/joinx", "//join/survival", "/join/" + "a" * 300, "/favicon.ico", "/robots.txt"):
            self.assertEqual(self.get(path).status, 404, path)
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS", "TRACE", "CONNECT"):
            self.assertEqual(self.get("/join/survival", method).status, 404, method)
        self.assertEqual(self.get("/join/survival").status, 200)
        self.assertEqual(self.get("/join/survival/").status, 200)
        self.assertEqual(self.get("/join/survival", "HEAD").status, 200)

    def test_off_means_off(self):
        self.on()
        self.settings["join_enabled"] = False
        for path in ("/join/survival", "/join", "/join/assets/join.css", "/join/survival/status.json"):
            self.assertEqual(self.get(path).status, 404, path)
        self.settings["join_enabled"] = True
        J.save_page("local", "minecraft-survival", {"enabled": False}, "admin")
        self.assertEqual(self.get("/join/survival").status, 404)

    def test_index_only_when_asked_for(self):
        self.on()
        self.assertEqual(self.get("/join").status, 404)
        self.settings["join_index"] = True
        page = self.get("/join")
        self.assertEqual(page.status, 200)
        self.assertIn(b'href="/join/survival"', page.body)
        self.summaries["minecraft-hidden"] = summary(name="minecraft-hidden", motd="Secret world")
        self.on("minecraft-hidden", unlisted=True)
        self.assertNotIn(b"Secret world", self.get("/join").body)

    def test_page_is_escaped_and_locked_down(self):
        self.summaries["minecraft-survival"] = summary(motd='<script>alert(1)</script>"><img src=x>')
        self.on(note="Rules: <b>be kind</b>\n<script>x()</script>", link_url="https://discord.gg/abc",
                link_label='Discord "chat" <i>')
        a = self.get("/join/survival")
        body = a.body.decode()
        self.assertNotIn("<script>alert", body)
        self.assertNotIn("<script>x()", body)
        self.assertNotIn("<b>be kind", body)
        self.assertNotIn("<img src=x>", body)
        self.assertIn("&lt;script&gt;alert(1)", body)
        self.assertIn('href="https://discord.gg/abc"', body)
        headers = {k.decode(): v.decode() for k, v in a.headers()}
        self.assertIn("default-src 'none'", headers["content-security-policy"])
        self.assertNotIn("unsafe-inline", headers["content-security-policy"])
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertEqual(headers["x-frame-options"], "DENY")
        self.assertNotIn("set-cookie", headers)
        # no inline script or style, nothing loaded from the panel or anywhere else
        self.assertNotIn("<style", body)
        self.assertNotIn("onclick", body)
        self.assertEqual(body.count("<script"), 1)
        self.assertIn('<script src="/join/assets/join.js', body)
        self.assertNotIn("/static/", body)
        self.assertNotIn("/api/", body)

    def test_page_says_nothing_about_the_machine(self):
        from core.version import panel_version
        self.on()
        body = self.get("/join/survival").body.decode()
        for secret in (panel_version(), "minecraft-survival", socket.gethostname(), "192.168.", "/opt/", "Alex"):
            self.assertNotIn(secret, body, secret)
        self.assertIn("play.example.org", body)
        self.assertIn("2 of 20 playing", body)
        self.assertIn("No mods are needed", body)

    def test_status_and_icon(self):
        self.on(show_players=True)
        st = self.get("/join/survival/status.json")
        self.assertEqual(json.loads(st.body), {"status": "online", "minecraft": "1.21.11",
                                               "players": {"online": 2, "max": 20, "names": ["Alex", "Steve"]}})
        icon = self.get("/join/survival/icon.png")
        self.assertEqual((icon.status, icon.content_type, icon.body), (200, "image/png", PNG))
        self.summaries["minecraft-survival"] = summary(icon=base64.b64encode(b"<html>not a png").decode())
        self.assertEqual(self.get("/join/survival/icon.png").status, 404)
        self.assertNotIn(b"icon.png", self.get("/join/survival").body.replace(b"icon blank", b""))

    def test_pages_by_kind(self):
        self.summaries["minecraft-survival"] = summary(
            loader="fabric", modpack={"name": "Cobblemon", "version": "1.6", "source": "modrinth",
                                      "slug": "cobblemon-fabric", "version_id": "AbC123xy"})
        self.on()
        body = self.get("/join/survival").body.decode()
        self.assertIn("https://modrinth.com/modpack/cobblemon-fabric/version/AbC123xy", body)
        self.assertIn('href="modrinth://modpack/cobblemon-fabric"', body)
        self.assertIn("Prism Launcher", body)
        self.summaries["minecraft-survival"] = summary(loader="neoforge")
        self.assertIn("Ask whoever runs it for the list", self.get("/join/survival").body.decode())
        self.summaries["minecraft-survival"] = summary(geyser_port="19132")
        body = self.get("/join/survival").body.decode()
        self.assertIn("minecraft://?addExternalServer=", body)
        self.assertIn("19132", body)
        self.summaries["minecraft-survival"] = summary(edition="Bedrock", loader="", port="19132", players=None,
                                                        transport="nethernet", motd="Pocket | world")
        body = self.get("/join/survival").body.decode()
        self.assertIn("Bedrock Edition", body)
        self.assertIn("addExternalServer=Pocket%20%20%20world%7Cplay.example.org%3A19132", body)
        self.assertNotIn("Java Edition", body)

    def test_server_that_cannot_be_read(self):
        self.on()
        del self.summaries["minecraft-survival"]
        a = self.get("/join/survival")
        self.assertEqual(a.status, 503)
        self.assertNotIn(b"Traceback", a.body)

    def test_rate_limit(self):
        self.on()
        for _ in range(S.RATE_MAX):
            self.assertEqual(self.get("/join/assets/join.css", ip="198.51.100.7").status, 200)
        a = self.get("/join/survival", ip="198.51.100.7")
        self.assertEqual(a.status, 429)
        self.assertEqual(self.get("/join/survival", ip="198.51.100.8").status, 200)
        self.assertTrue(S.allowed("198.51.100.7", now=__import__("time").monotonic() + S.RATE_WINDOW + 1))

    def test_assets_are_shipped(self):
        for name in S.ASSETS:
            a = self.get(f"/join/assets/{name}")
            self.assertEqual(a.status, 200, name)
            self.assertGreater(len(a.body), 200)
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "debian",
                               "redstonepanel.install")) as f:
            install = f.read()
        for needed in ("core/joinpage.py", "core/joinsite.py", "static/join/join.css", "static/join/join.js"):
            self.assertIn(needed, install)


class Listener(Base):
    """The real thing: a port, plain HTTP, and only /join answers."""

    def _free_port(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def _fetch(self, port, path, method="GET"):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def test_start_serve_move_stop(self):
        try:
            import uvicorn  # noqa: F401
        except ImportError:
            self.skipTest("needs uvicorn")
        self.on()
        port = self._free_port()
        self.settings["join_port"] = port
        opened, closed = [], []
        with mock.patch.object(manager, "open_firewall", lambda p, q: opened.append((p, q))), \
             mock.patch.object(manager, "close_firewall_rules", lambda rules: closed.append(rules)):
            S.apply(dict(self.settings), dict(self.settings, join_enabled=False), "admin")
            self.addCleanup(S.stop)
            self.assertEqual(opened, [(port, "tcp")])
            self.assertTrue(S.status()["running"])
            code, headers, body = self._fetch(port, "/join/survival")
            self.assertEqual(code, 200)
            self.assertIn(b"play.example.org", body)
            self.assertNotIn("server", {k.lower() for k in headers})
            self.assertNotIn("set-cookie", {k.lower() for k in headers})
            code, headers, body = self._fetch(port, "/join/survival", "HEAD")
            self.assertEqual((code, body), (200, b""))
            for path in ("/", "/api/health", "/static/index.html", "/docs"):
                self.assertEqual(self._fetch(port, path)[0], 404, path)
            self.assertEqual(self._fetch(port, "/join/survival", "POST")[0], 404)
            # a taken port is reported, not fought over
            self.assertFalse(P.port_free(port))
            with self.assertRaises(J.JoinError):
                S.check_can_enable(dict(self.settings, join_port=self._taken()), self.settings)
            # moving it closes the old port's rule and opens the new one
            new = self._free_port()
            old = dict(self.settings)
            self.settings["join_port"] = new
            S.apply(dict(self.settings), old, "admin")
            self.assertEqual(self._fetch(new, "/join/survival")[0], 200)
            self.assertIn({(str(port), "tcp")}, closed)
            self.assertTrue(P.port_free(port))
            S.apply(dict(self.settings, join_enabled=False), dict(self.settings), "admin")
            self.assertFalse(S.status()["running"])
            self.assertTrue(P.port_free(new))

    def _taken(self):
        s = socket.socket()
        s.bind(("0.0.0.0", 0))
        s.listen(1)
        self.addCleanup(s.close)
        return s.getsockname()[1]

    def test_firewall_rule_is_counted_while_on(self):
        self.assertEqual(S.firewall_rules(dict(P.DEFAULTS, join_enabled=True, join_port=8444)), {("8444", "tcp")})
        self.assertEqual(S.firewall_rules(dict(P.DEFAULTS)), set())


class Pings(unittest.TestCase):
    def _java_server(self, reply):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        self.addCleanup(srv.close)

        def run():
            conn, _ = srv.accept()
            with conn:
                conn.recv(1024)
                body = json.dumps(reply).encode()
                payload = b"\x00" + portforward._varint(len(body)) + body
                conn.sendall(portforward._varint(len(payload)) + payload)
        import threading
        threading.Thread(target=run, daemon=True).start()
        return srv.getsockname()[1]

    def test_java_ping_reads_players_and_drops_odd_names(self):
        port = self._java_server({
            "version": {"name": "Paper 1.21.11"}, "description": {"text": "§aHello ", "extra": [{"text": "world"}]},
            "players": {"online": 3, "max": 20, "sample": [{"name": "Alex"}, {"name": "<img src=x>"},
                                                            {"name": "§cSteve"}, {"name": "x" * 40}, "odd"]},
            "favicon": "data:image/png;base64," + base64.b64encode(PNG).decode()})
        got = J.java_ping("127.0.0.1", port)
        self.assertEqual((got["online"], got["max"], got["names"]), (3, 20, ["Alex"]))
        self.assertEqual((got["motd"], got["version"]), ("Hello world", "Paper 1.21.11"))
        self.assertEqual(base64.b64decode(got["icon"]), PNG)

    def test_java_ping_survives_nonsense(self):
        port = self._java_server({"players": {"online": "many", "max": -5}, "favicon": "data:text/html,x"})
        got = J.java_ping("127.0.0.1", port)
        self.assertEqual((got["online"], got["max"], got["names"], got["icon"]), (None, None, [], ""))
        port = self._java_server(["not", "a", "status"])
        with self.assertRaises(OSError):
            J.java_ping("127.0.0.1", port)


if __name__ == "__main__":
    unittest.main()
