"""1.25: API tokens (core/apitokens.py and the middleware in main.py).
Run: python -m unittest discover -s tests

A token acts as a `user` account with its own per-server permissions: it reaches what those
allow and never what needs an admin or changes an account. Revoked and expired tokens are
refused at once, wrong ones count toward the sign-in throttle, and changes are logged.
"""
import os
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-apitokens-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import apitokens as T, auth, grants as G  # noqa: E402

try:
    import fastapi  # noqa: F401
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except (ImportError, RuntimeError):
    HAVE_FASTAPI = False


class Fresh(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp(dir=TMP)
        for p in (mock.patch.object(T, "TOKENS_FILE", os.path.join(d, "api-tokens.json")),
                  mock.patch.object(G, "GRANTS_FILE", os.path.join(d, "grants.json"))):
            p.start()
            self.addCleanup(p.stop)
        T._ok_cache.clear()
        G._cache.update(mtime=None, data=None)
        auth.reset_fails()


class Store(Fresh):
    def test_made_shown_once_and_kept_as_a_hash(self):
        out = T.create("Discord bot", {"minecraft": ["power"]}, actor="quentin")
        self.assertRegex(out["token"], T.TOKEN_RE)
        self.assertEqual(out["grants"], {"minecraft": ["view", "power"]}, "view comes with any permission")
        with open(T.TOKENS_FILE) as f:
            self.assertNotIn(out["token"], f.read())
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(os.stat(T.TOKENS_FILE).st_mode), 0o600)
        listed = T.list_tokens()
        self.assertEqual([t["name"] for t in listed], ["Discord bot"])
        self.assertNotIn("token", listed[0])
        self.assertNotIn("hash", listed[0])
        self.assertEqual(listed[0]["created_by"], "quentin")

    def test_bad_requests(self):
        for name, grants, days in (("", {"*": ["view"]}, None), ("a/b", {"*": ["view"]}, None),
                                   ("ok", {}, None), ("ok", {"../etc": ["view"]}, None),
                                   ("ok", {"minecraft": ["root"]}, None), ("ok", {"minecraft": []}, None),
                                   ("ok", {"*": ["view"]}, 0.5), ("ok", {"*": ["view"]}, 99999)):
            with self.assertRaises(T.TokenError, msg=(name, grants, days)):
                T.create(name, grants, days)
        T.create("bot", {"*": ["view"]})
        with self.assertRaisesRegex(T.TokenError, "already"):
            T.create("BOT", {"*": ["view"]})

    def test_read_revoke_and_expiry(self):
        out = T.create("bot", {"*": ["view", "console"]}, days=1)
        who = T.read(out["token"], "10.0.0.5")
        self.assertEqual(who, {"username": "token:bot", "source": "token", "role": "user", "token_id": out["id"],
                               "grants": {"*": ["view", "console"]}})
        self.assertEqual(T.list_tokens()[0]["last_ip"], "10.0.0.5")
        self.assertIsNone(T.read(out["token"][:-1] + ("A" if out["token"][-1] != "A" else "B"), "10.0.0.5"))
        self.assertIsNone(T.read("rpa_nothing", "10.0.0.5"))
        later = time.time() + 2 * 86400
        with mock.patch.object(T.time, "time", lambda: later):
            self.assertIsNone(T.read(out["token"], "10.0.0.5"), "expired")
        self.assertEqual(T.revoke(out["id"]), "bot")
        self.assertIsNone(T.read(out["token"], "10.0.0.5"), "revoked: refused at once, cache or not")
        with self.assertRaises(T.TokenError):
            T.revoke(out["id"])

    def test_wrong_tokens_are_throttled(self):
        out = T.create("bot", {"*": ["view"]})
        bad = f"rpa_{out['id']}_" + "x" * 43
        for _ in range(auth.MAX_FAILS):
            self.assertIsNone(T.read(bad, "10.0.0.9"))
        self.assertIsNone(T.read(out["token"], "10.0.0.9"), "that address is throttled now")
        self.assertIsNotNone(T.read(out["token"], "10.0.0.8"))

    def test_permissions_follow_the_server(self):
        out = T.create("bot", {"minecraft": ["power"], "minecraft-2": ["files"]})
        G.rename_server("minecraft", "n-abc123/minecraft")
        G.forget_server("minecraft-2")
        self.assertEqual(T.list_tokens()[0]["grants"], {"n-abc123/minecraft": ["view", "power"]})
        who = T.read(out["token"], "10.0.0.5")
        self.assertTrue(G.can(who, "power", "minecraft", "n-abc123"))
        self.assertFalse(G.can(who, "files", "minecraft-2"))
        self.assertTrue(G.limited(who))


@unittest.skipUnless(HAVE_FASTAPI, "needs fastapi")
class Api(Fresh):
    """Through the real middleware and routes."""
    SERVERS = [{"name": "minecraft", "edition": "Java"}, {"name": "minecraft-2", "edition": "Bedrock"}]

    def setUp(self):
        super().setUp()
        import main
        self.main = main
        self.log = []
        self.admin = {"username": "quentin", "role": "admin", "source": "linux"}
        for p in (mock.patch.object(main.auth, "read_session_token", lambda t: dict(self.admin)),
                  mock.patch.object(main.manager, "list_servers", lambda: [dict(s) for s in self.SERVERS]),
                  mock.patch.object(main.manager, "write_log", self.log.append),
                  mock.patch.object(main.manager, "control_server", lambda s, a: None)):
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(main.app)

    def make(self, grants, name="bot"):
        self.client.cookies.set(self.main.auth.SESSION_COOKIE, "session")
        r = self.client.post("/api/settings/api-tokens", json={"name": name, "grants": grants})
        self.assertEqual(r.status_code, 200, r.text)
        self.client.cookies.clear()
        return r.json()

    def call(self, token, method, path, **kw):
        return self.client.request(method, path, headers={"Authorization": f"Bearer {token}"}, **kw)

    def test_admins_make_list_and_revoke(self):
        out = self.make({"minecraft": ["power"]})
        self.assertTrue(any("quentin made the API token bot (minecraft: view/power)" in l for l in self.log), self.log)
        self.client.cookies.set(self.main.auth.SESSION_COOKIE, "session")
        listed = self.client.get("/api/settings/api-tokens").json()
        self.assertEqual([t["name"] for t in listed["tokens"]], ["bot"])
        self.assertEqual(listed["perms"], list(G.PERMS))
        self.assertEqual(self.client.delete(f"/api/settings/api-tokens/{out['id']}").status_code, 200)
        self.assertTrue(any("quentin revoked the API token bot" in l for l in self.log))
        self.client.cookies.clear()
        r = self.call(out["token"], "GET", "/api/servers")
        self.assertEqual(r.status_code, 401)
        self.assertIn("revoked", r.json()["detail"])

    def test_a_user_cant_make_tokens(self):
        self.admin = {"username": "alex", "role": "user", "source": "panel"}
        self.client.cookies.set(self.main.auth.SESSION_COOKIE, "session")
        self.assertEqual(self.client.post("/api/settings/api-tokens",
                                          json={"name": "x", "grants": {"*": ["view"]}}).status_code, 403)

    def test_a_token_does_what_its_grants_allow(self):
        t = self.make({"minecraft-2": ["power"]})["token"]
        listed = self.call(t, "GET", "/api/servers").json()
        self.assertEqual([(s["name"], s["perms"]) for s in listed], [("minecraft-2", ["view", "power"])])
        r = self.call(t, "POST", "/api/servers/minecraft-2/action", json={"action": "restart"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(any(l.startswith("[API] token:bot: POST /api/servers/minecraft-2/action") for l in self.log))
        self.assertEqual(self.call(t, "POST", "/api/servers/minecraft/action", json={"action": "restart"}).status_code, 403)
        self.assertEqual(self.call(t, "GET", "/api/servers/minecraft-2/files").status_code, 403)
        me = self.call(t, "GET", "/api/auth/me").json()
        self.assertEqual((me["username"], me["role"], me["grants"]), ("token:bot", "user", {"minecraft-2": ["view", "power"]}))
        self.assertNotIn("token_id", me)

    def test_never_an_admin_or_an_account(self):
        t = self.make({"*": list(G.PERMS)})["token"]
        for method, path in (("GET", "/api/settings/api-tokens"), ("POST", "/api/settings/api-tokens"),
                             ("GET", "/api/users"), ("POST", "/api/servers/import"), ("DELETE", "/api/servers/minecraft"),
                             ("GET", "/api/java/runtimes"), ("POST", "/api/auth/password"),
                             ("GET", "/api/auth/passkeys"), ("POST", "/api/auth/2fa/setup"), ("GET", "/api/terminal"),
                             ("GET", "/api/cluster/hello")):
            r = self.call(t, method, path, json={})
            self.assertEqual(r.status_code, 403, f"{method} {path}: {r.text}")

    def test_a_wrong_token_is_401_not_a_session(self):
        self.client.cookies.set(self.main.auth.SESSION_COOKIE, "session")
        r = self.call("rpa_00000000_" + "x" * 43, "GET", "/api/servers")
        self.assertEqual(r.status_code, 401, "a bad token isn't rescued by a cookie")



@unittest.skipUnless(HAVE_FASTAPI, "needs fastapi")
class ThroughTheMaster(Api):
    """A token made on the master reaches a node's servers through the master's proxy, with the
    same per-server check as a helper; the node is told the token's name and permissions."""
    NODE = {"id": "n-abc123", "name": "Basement", "url": "https://192.168.1.20:8443",
            "token": "rpn_" + "x" * 43, "cluster_api": 6, "version": "1.25.0"}

    def setUp(self):
        super().setUp()
        self.calls = []

        class Answer:
            status, headers = 200, {"content-type": "application/json"}

            def chunks(inner):
                yield b'{"ok": true}'

            def read_all(inner, limit=None):
                return b'{"ok": true}'

            def close(inner):
                pass

        def open_proxy(node, method, path, body, length, content_type, actor, role, perms=None):
            self.calls.append({"path": path, "actor": actor, "role": role, "perms": perms})
            return Answer()
        main = self.main
        for p in (mock.patch.object(main.cluster, "role", lambda: "master"),
                  mock.patch.object(main.cluster, "find_node", lambda i: dict(self.NODE) if i == self.NODE["id"] else None),
                  mock.patch.object(main.cluster_client, "open_proxy", open_proxy),
                  mock.patch.object(main.cluster_client, "forget_cached", lambda *a: None),
                  mock.patch.object(main.panel_settings, "display_name", lambda *a: "Main")):
            p.start()
            self.addCleanup(p.stop)

    def url(self, tail):
        return f"/api/cluster/nodes/{self.NODE['id']}/proxy{tail}"

    def test_a_nodes_server(self):
        t = self.make({"n-abc123/minecraft-java-1": ["power"]})["token"]
        r = self.call(t, "POST", self.url("/api/servers/minecraft-java-1/action"), json={"action": "restart"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.calls, [{"path": "/api/servers/minecraft-java-1/action", "actor": "token:bot",
                                       "role": "user", "perms": {"view", "power"}}])
        self.assertTrue(any("[API] token:bot: POST /api/cluster/nodes/n-abc123/proxy/api/servers/minecraft-java-1/action"
                            in l for l in self.log), self.log)

    def test_every_server_includes_the_nodes(self):
        t = self.make({"*": ["view", "backups"]})["token"]
        self.assertEqual(self.call(t, "GET", self.url("/api/servers/minecraft-java-1/backups")).status_code, 200)
        self.assertEqual(self.calls[0]["perms"], {"view", "backups"})

    def test_not_another_server_or_the_nodes_admin_side(self):
        t = self.make({"n-abc123/minecraft-java-1": list(G.PERMS), "minecraft": list(G.PERMS)})["token"]
        for method, path in (("POST", "/api/servers/minecraft-other/action"), ("POST", "/api/servers/deploy"),
                             ("DELETE", "/api/servers/minecraft-java-1"), ("GET", "/api/users"),
                             ("PUT", "/api/settings")):
            r = self.call(t, method, self.url(path), json={"action": "stop"})
            self.assertEqual(r.status_code, 403, f"{method} {path}: {r.text}")
        r = self.call(t, "POST", "/api/cluster/nodes/n-ffff/proxy/api/servers/minecraft-java-1/action", json={"action": "stop"})
        self.assertIn(r.status_code, (403, 404), "a grant on one node isn't a grant on another")
        self.assertEqual(self.calls, [])

    def test_the_overview_lists_only_its_servers(self):
        t = self.make({"n-abc123/minecraft-2": ["view"]})["token"]
        ov = {"cluster": "Home", "epoch": 1, "nodes": [
            {"id": "local", "local": True, "servers": [{"name": "minecraft"}]},
            {"id": "n-abc123", "servers": [{"name": "minecraft"}, {"name": "minecraft-2"}]}]}
        with mock.patch.object(self.main.cluster_client, "overview", lambda fn, fresh=False: ov):
            got = self.call(t, "GET", "/api/cluster/overview").json()["nodes"]
        self.assertEqual([[s["name"] for s in n["servers"]] for n in got], [[], ["minecraft-2"]])


if __name__ == "__main__":
    unittest.main()
