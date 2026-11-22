"""1.18: per-server permissions (core/grants.py and the middleware in main.py).
Run: python -m unittest discover -s tests

Every API route must have a rule, and each rule is tried: an account with every permission but
the one a route needs is refused there, and one with only that permission isn't. The rest goes
through the real middleware and routes with FastAPI's TestClient; sign-in is a stand-in.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-grants-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import grants as G, host as H  # noqa: E402

try:
    import fastapi  # noqa: F401
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except (ImportError, RuntimeError):
    HAVE_FASTAPI = False

NODE = {"id": "n-abc123", "name": "Basement", "url": "https://192.168.1.20:8443", "token": "rpn_" + "x" * 43,
        "cluster_api": 6, "version": "1.18.0"}


def alex(role="user", source="panel"):
    return {"username": "alex", "role": role, "source": source}


class Store(unittest.TestCase):
    def setUp(self):
        self.file = os.path.join(tempfile.mkdtemp(dir=TMP), "grants.json")
        p = mock.patch.object(G, "GRANTS_FILE", self.file)
        p.start()
        self.addCleanup(p.stop)
        G._cache.update(mtime=None, data=None)

    def test_set_get_and_view_is_implied(self):
        saved = G.set_user("alex", {"minecraft-2": ["console"], "n-abc123/minecraft": ["files", "view"], "*": []})
        self.assertEqual(saved, {"minecraft-2": ["view", "console"], "n-abc123/minecraft": ["view", "files"]})
        self.assertEqual(G.get("alex"), saved)
        self.assertEqual(os.stat(self.file).st_mode & 0o777, 0o600)
        self.assertEqual(G.get("nobody"), {})

    def test_bad_grants_are_refused(self):
        for bad in ({"../etc": ["view"]}, {"minecraft-2": ["root"]}, {"minecraft-2": "view"}, ["view"],
                    {"n-zz/minecraft": ["view"]}, {"survival": ["view"]}):
            with self.assertRaises(G.GrantError, msg=bad):
                G.set_user("alex", bad)
        self.assertEqual(G.get("alex"), {})

    def test_what_an_account_may_do(self):
        G.set_user("alex", {"minecraft-2": ["console"], "n-abc123/minecraft": ["files"]})
        self.assertEqual(G.perms_for(alex(), "minecraft-2"), {"view", "console"})
        self.assertEqual(G.perms_for(alex(), "minecraft"), set(), "a node's server isn't this panel's server")
        self.assertTrue(G.can(alex(), "files", "minecraft", "n-abc123"))
        self.assertFalse(G.can(alex(), "files", "minecraft-2"))
        self.assertFalse(G.can(alex(), "view", "minecraft-20"), "no prefix matching")
        self.assertEqual(G.perms_for(alex("admin"), "minecraft-9"), set(G.PERMS))
        self.assertEqual(G.perms_for({"username": "sam", "role": "user", "source": "panel"}, "minecraft-2"), set())

    def test_every_server(self):
        G.set_user("alex", {"*": ["view"], "minecraft-2": ["power"]})
        self.assertEqual(G.perms_for(alex(), "minecraft-7"), {"view"})
        self.assertEqual(G.perms_for(alex(), "n-abc123/minecraft"), {"view"})
        self.assertEqual(G.perms_for(alex(), "minecraft-2"), {"view", "power"})

    def test_other_identities(self):
        trusted = {"username": "local network", "role": "user", "source": "trusted"}
        self.assertEqual(G.perms_for(trusted, "minecraft"), set(G.LEGACY))
        self.assertNotIn("files", G.LEGACY)
        self.assertNotIn("mods", G.LEGACY)
        node_user = {"username": "alex@Main", "role": "user", "source": "cluster", "perms": {"view", "files"}}
        self.assertEqual(G.perms_for(node_user, "minecraft"), {"view", "files"})
        self.assertEqual(G.perms_for(dict(node_user, perms=set()), "minecraft"), set())
        self.assertEqual(G.perms_for(dict(node_user, perms=None), "minecraft"), set(G.LEGACY), "a master before 1.18")
        self.assertEqual(G.parse_header("view,files,root"), {"view", "files"})
        self.assertEqual(G.parse_header(""), set())
        self.assertIsNone(G.parse_header(None))

    def test_a_deleted_servers_grants_go_and_a_moved_servers_follow(self):
        G.set_user("alex", {"minecraft-2": ["files"], "minecraft-3": ["view"]})
        G.set_user("sam", {"minecraft-2": ["view"]})
        G.forget_server("minecraft-2")
        self.assertEqual(G.get("alex"), {"minecraft-3": ["view"]})
        self.assertEqual(G.get("sam"), {})
        G.rename_server("minecraft-3", "n-abc123/minecraft-3")
        self.assertEqual(G.get("alex"), {"n-abc123/minecraft-3": ["view"]})
        G.remove_user("alex")
        self.assertEqual(G.get("alex"), {})

    def test_accounts_from_before_1_18_keep_what_they_could_do(self):
        self.assertEqual(G.migrate_existing_users(["alex", "sam"]), ["alex", "sam"])
        self.assertEqual(G.get("alex"), {"*": list(G.LEGACY)})
        self.assertEqual(G.migrate_existing_users(["new"]), [], "only once")
        self.assertEqual(G.get("new"), {})

    def test_an_unreadable_file_grants_nothing(self):
        with open(self.file, "w") as f:
            f.write("{not json")
        self.assertEqual(G.get("alex"), {})
        self.assertEqual(G.perms_for(alex("admin"), "minecraft"), set(G.PERMS), "admins don't depend on it")


class Report(unittest.TestCase):
    def test_the_dashboard_of_a_limited_account(self):
        rep = {"servers": [{"name": "minecraft"}, {"name": "minecraft-2"}],
               "attention": [{"server": "minecraft", "where": "backups", "text": "a"},
                             {"server": "minecraft-2", "where": "crash", "text": "b"},
                             {"server": None, "where": "disk", "text": "c"},
                             {"server": None, "where": "update", "text": "d"},
                             {"server": None, "where": "services", "text": "Phantom proxy x is failed"}],
               "services": {"java": [{"major": 21, "servers": ["minecraft", "minecraft-2"]}],
                            "java_needed": [{"major": 25, "servers": ["minecraft"]}], "failed_units": ["x"],
                            "phantoms": [{"unit": "phantom-minecraft"}], "screen": True}}
        out = H.for_user(rep, lambda n: n == "minecraft-2")
        self.assertEqual([s["name"] for s in out["servers"]], ["minecraft-2"])
        self.assertEqual([a["text"] for a in out["attention"]], ["b", "c"])
        self.assertEqual(out["services"]["java"], [{"major": 21, "servers": ["minecraft-2"]}])
        self.assertNotIn("failed_units", out["services"])
        self.assertNotIn("java_needed", out["services"])
        self.assertTrue(out["limited"])
        self.assertEqual(len(rep["servers"]), 2, "the cached report isn't changed")


@unittest.skipUnless(HAVE_FASTAPI, "needs fastapi")
class RouteCoverage(unittest.TestCase):
    """Every API route has a rule on purpose, and every rule does what it says."""

    @classmethod
    def setUpClass(cls):
        import main
        cls.main = main
        cls.routes = sorted({(m, r.path) for r in main.app.routes if getattr(r, "path", "").startswith("/api/")
                             for m in (getattr(r, "methods", None) or ()) if m != "HEAD"})

    @staticmethod
    def sample(path):
        return (path.replace("{server_name}", "minecraft-x").replace("{path:path}", "api/servers")
                .replace("{node_id}", "n-abc123").replace("{member_id}", "n-abc123").replace("{panel}", "n-abc123")
                .replace("{tid}", "a" * 32).replace("{", "").replace("}", ""))

    def refusal(self, user, method, path):
        return self.main._grant_refusal(method, {"path": self.sample(path)}, user)

    def test_every_route_has_a_rule(self):
        missing = [(m, p) for m, p in self.routes if (m, p) not in G.RULES and not p.startswith(G.ADMIN_PREFIXES)]
        self.assertEqual(missing, [], "add the route to RULES in core/grants.py (unlisted routes need an admin)")

    def test_every_rule_has_a_route(self):
        stale = sorted(set(G.RULES) - set(self.routes))
        self.assertEqual(stale, [], "rules for routes that no longer exist")

    def test_rules_are_known(self):
        for (m, p), rule in G.RULES.items():
            perm, _, param = rule.partition("@")
            self.assertIn(perm, G.PERMS + ("public", "user", "signed", "admin", "node", "proxy"), f"{m} {p}")
            if param:
                self.assertIn("{" + param + "}", p, f"{m} {p}")
            if perm in G.PERMS:
                self.assertIn("{server_name}", p, f"{m} {p}")
        self.assertEqual({p for (m, p), r in G.RULES.items() if r == "public"}, set(self.main.PUBLIC_API))

    def test_each_per_server_route_needs_its_permission(self):
        tried = 0
        for (m, p), rule in sorted(G.RULES.items()):
            need = G.needs(rule)
            if not need:
                continue
            perm, param = need
            key = G.server_key("minecraft-x", "n-abc123" if param else None)
            others = [x for x in G.PERMS if x != perm and (perm != "view" or x == "nothing")]
            with mock.patch.object(G, "get", lambda name: {key: others} if others else {}):
                self.assertIn("Users", self.refusal(alex(), m, p) or "", f"{m} {p} without {perm}")
            with mock.patch.object(G, "get", lambda name: {key: ["view", perm]}):
                self.assertIsNone(self.refusal(alex(), m, p), f"{m} {p} with {perm}")
            # A grant on another server, or on this panel's server of the same name, doesn't count
            wrong = "minecraft-x" if param else "n-abc123/minecraft-x"
            with mock.patch.object(G, "get", lambda name: {wrong: list(G.PERMS), "minecraft-y": list(G.PERMS)}):
                self.assertIsNotNone(self.refusal(alex(), m, p), f"{m} {p} with a grant elsewhere")
            self.assertIsNone(self.refusal(alex("admin"), m, p))
            tried += 1
        self.assertGreater(tried, 60)

    def test_admin_routes_refuse_every_user(self):
        everything = {"*": list(G.PERMS)}
        tried = 0
        for m, p in self.routes:
            if G.rule_for(m, p) != "admin":
                continue
            with mock.patch.object(G, "get", lambda name: everything):
                self.assertEqual(self.refusal(alex(), m, p), "Only admins can do this.", f"{m} {p}")
            trusted = {"username": "local network", "role": "user", "source": "trusted"}
            self.assertEqual(self.refusal(trusted, m, p), "Sign in as an admin to do this.", f"{m} {p}")
            self.assertIsNone(self.refusal(alex("admin"), m, p))
            tried += 1
        self.assertGreater(tried, 50)
        for path in ("/api/users", "/api/users/{username}/grants", "/api/settings/sftp", "/api/settings/new-thing"):
            self.assertEqual(G.rule_for("PUT", path), "admin")
        self.assertEqual(G.rule_for("POST", "/api/some/new/route"), "admin", "a route nobody listed needs an admin")

    def test_what_stays_with_admins(self):
        for m, p in (("DELETE", "/api/servers/{server_name}"), ("POST", "/api/servers/deploy"),
                     ("POST", "/api/servers/import"), ("POST", "/api/servers/{server_name}/backups/{filename}/restore"),
                     ("DELETE", "/api/servers/{server_name}/backups/{filename}"), ("POST", "/api/cluster/moves"),
                     ("POST", "/api/servers/{server_name}/java"), ("PUT", "/api/servers/{server_name}/resources"),
                     ("DELETE", "/api/servers/removed/{server_name}"), ("POST", "/api/panel/update")):
            self.assertEqual(G.rule_for(m, p), "admin", f"{m} {p}")

    def test_node_plumbing_is_never_for_browsers(self):
        for (m, p), rule in G.RULES.items():
            if rule == "node":
                self.assertTrue(p.startswith(self.main.NODE_ONLY_API), f"{m} {p}")


@unittest.skipUnless(HAVE_FASTAPI, "needs fastapi")
class Api(unittest.TestCase):
    """Through the real middleware."""
    SERVERS = [{"name": "minecraft", "edition": "Java"}, {"name": "minecraft-2", "edition": "Bedrock"}]

    def setUp(self):
        import main
        self.main = main
        self.user = alex()
        self.grants = {}
        self.log = []
        G._cache.update(mtime=None, data=None)
        for p in (mock.patch.object(main.auth, "read_session_token", lambda t: dict(self.user)),
                  mock.patch.object(G, "GRANTS_FILE", os.path.join(tempfile.mkdtemp(dir=TMP), "grants.json")),
                  mock.patch.object(main.manager, "list_servers", lambda: [dict(s) for s in self.SERVERS]),
                  mock.patch.object(main.manager, "write_log", self.log.append),
                  mock.patch.object(main.manager, "control_server", lambda s, a: None),
                  mock.patch.object(main.files, "list_dir", lambda s, p="": {"path": p, "entries": []}),
                  mock.patch.object(main.auth, "_load_users", lambda: [{"username": "alex", "role": "user"}])):
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(main.app)
        self.client.cookies.set(main.auth.SESSION_COOKIE, "session")

    def give(self, grants):
        G.set_user("alex", grants)

    def test_no_grants_means_no_servers(self):
        self.assertEqual(self.client.get("/api/servers").json(), [])
        r = self.client.post("/api/servers/minecraft/action", json={"action": "restart"})
        self.assertEqual(r.status_code, 403)
        self.assertIn("Start/stop/restart", r.json()["detail"])
        self.assertEqual(self.client.get("/api/auth/me").json()["grants"], {})

    def test_the_list_holds_only_granted_servers_and_says_what_is_allowed(self):
        self.give({"minecraft-2": ["power", "files"]})
        listed = self.client.get("/api/servers").json()
        self.assertEqual([(s["name"], s["perms"]) for s in listed], [("minecraft-2", ["view", "power", "files"])])
        self.assertEqual(self.client.post("/api/servers/minecraft-2/action", json={"action": "restart"}).status_code, 200)
        self.assertEqual(self.client.post("/api/servers/minecraft/action", json={"action": "restart"}).status_code, 403)
        self.assertEqual(self.client.get("/api/servers/minecraft-2/files").status_code, 200)
        self.assertEqual(self.client.get("/api/servers/minecraft/files").status_code, 403)
        self.assertEqual(self.client.get("/api/servers/minecraft-2/chat").status_code, 403)
        self.assertEqual(self.client.get("/api/auth/me").json()["grants"], {"minecraft-2": ["view", "power", "files"]})

    def test_admins_see_everything_unmarked(self):
        self.user["role"] = "admin"
        listed = self.client.get("/api/servers").json()
        self.assertEqual([s["name"] for s in listed], ["minecraft", "minecraft-2"])
        self.assertNotIn("perms", listed[0])
        self.assertNotIn("grants", self.client.get("/api/auth/me").json())

    def test_what_users_lost_to_admins(self):
        self.give({"*": list(G.PERMS)})
        for method, path in (("POST", "/api/servers/import"), ("GET", "/api/servers/removed"),
                             ("DELETE", "/api/templates/t-1"), ("GET", "/api/java/runtimes"),
                             ("POST", "/api/servers/minecraft/backups/x.tar.gz/restore"),
                             ("DELETE", "/api/servers/minecraft")):
            r = self.client.request(method, path)
            self.assertEqual((r.status_code, r.json()["detail"]), (403, "Only admins can do this."), f"{method} {path}")

    def test_admins_set_grants(self):
        r = self.client.put("/api/users/alex/grants", json={"grants": {"minecraft": ["view"]}})
        self.assertEqual(r.status_code, 403, "not a user for themselves")
        self.user = alex("admin", "linux")
        self.user["username"] = "quentin"
        r = self.client.put("/api/users/alex/grants", json={"grants": {"minecraft": ["console"], "*": ["view"]}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["grants"], {"*": ["view"], "minecraft": ["view", "console"]})
        self.assertTrue(any("quentin set what 'alex' may do" in line and "every server: view" in line
                            for line in self.log), self.log)
        self.assertEqual(self.client.put("/api/users/alex/grants", json={"grants": {"x": ["view"]}}).status_code, 400)
        self.assertEqual(self.client.put("/api/users/nobody/grants", json={"grants": {}}).status_code, 404)
        with mock.patch.object(self.main.auth, "list_panel_users",
                               lambda: [{"username": "alex", "role": "user"}]):
            d = self.client.get("/api/users").json()
        self.assertEqual(d["users"][0]["grants"], {"*": ["view"], "minecraft": ["view", "console"]})
        self.assertEqual([p["id"] for p in d["perms"]], list(G.PERMS))
        self.assertEqual(d["presets"]["moderator"], ["view", "console", "players"])

    def test_deleting_an_account_or_a_server_drops_its_grants(self):
        self.give({"minecraft": ["files"], "minecraft-2": ["view"]})
        self.user = alex("admin", "linux")
        with mock.patch.object(self.main.manager, "_check_server", lambda s: None), \
                mock.patch.object(self.main.manager, "remove_server", lambda s, keep: None), \
                mock.patch.object(self.main.playerlog, "forget", lambda s: None):
            self.assertEqual(self.client.delete("/api/servers/minecraft?keep_world=true").status_code, 200)
            self.assertIn("minecraft", G.get("alex"), "an uninstalled server can come back: its grants wait")
            self.assertEqual(self.client.delete("/api/servers/minecraft?keep_world=false").status_code, 200)
        self.assertEqual(G.get("alex"), {"minecraft-2": ["view"]})
        with mock.patch.object(self.main.auth, "delete_panel_user", lambda u: None):
            self.assertEqual(self.client.delete("/api/users/alex").status_code, 200)
        self.assertEqual(G.get("alex"), {})

    def test_the_action_console_shows_only_their_servers(self):
        self.give({"minecraft-2": ["view"]})
        lines = ["[POWER] restart minecraft-2", "[POWER] restart minecraft", "[AUTH] quentin set what 'alex' may do: "
                 "minecraft-2: view", "[BACKUP] minecraft-2.service backed up", "apt output without a server",
                 "[MOVE] minecraft-2 and minecraft-20", "[FILES] sam saved minecraft-2/plugins/x.yml (3 bytes)."]
        with mock.patch.object(self.main.manager, "get_panel_logs", lambda n: "\n".join(lines)):
            got = self.client.get("/api/logs").json()["content"].splitlines()
            self.assertEqual(got, [lines[0], lines[3], lines[6]])
            self.user["role"] = "admin"
            self.assertEqual(len(self.client.get("/api/logs").json()["content"].splitlines()), len(lines))

    def test_the_dashboard_is_cut_down(self):
        self.give({"minecraft-2": ["view"]})
        rep = {"servers": [{"name": "minecraft"}, {"name": "minecraft-2"}], "attention": [], "services": {}}
        with mock.patch.object(self.main.host, "report", lambda redacted=False: rep):
            got = self.client.get("/api/host").json()
            self.assertEqual([s["name"] for s in got["servers"]], ["minecraft-2"])
            self.user["role"] = "admin"
            self.assertEqual(len(self.client.get("/api/host").json()["servers"]), 2)


@unittest.skipUnless(HAVE_FASTAPI, "needs fastapi")
class Cluster(unittest.TestCase):
    """Grants for a node's servers are kept and checked on the master; the node is told the result."""

    def setUp(self):
        import main
        self.main = main
        self.user = alex()
        self.calls = []
        self.body = b'{"ok": true}'
        G._cache.update(mtime=None, data=None)

        class Answer:
            status, headers = 200, {"content-type": "application/json"}

            def chunks(inner):
                yield self.body

            def read_all(inner, limit=None):
                return self.body

            def close(inner):
                pass

        def open_proxy(node, method, path, body, length, content_type, actor, role, perms=None):
            self.calls.append({"path": path, "role": role, "perms": perms})
            return Answer()
        for p in (mock.patch.object(main.auth, "read_session_token", lambda t: dict(self.user)),
                  mock.patch.object(G, "GRANTS_FILE", os.path.join(tempfile.mkdtemp(dir=TMP), "grants.json")),
                  mock.patch.object(main.cluster, "role", lambda: "master"),
                  mock.patch.object(main.cluster, "find_node", lambda i: dict(NODE) if i == NODE["id"] else None),
                  mock.patch.object(main.cluster_client, "open_proxy", open_proxy),
                  mock.patch.object(main.cluster_client, "forget_cached", lambda *a: None),
                  mock.patch.object(main.manager, "write_log", lambda m: None),
                  mock.patch.object(main.panel_settings, "display_name", lambda *a: "Main")):
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(main.app)
        self.client.cookies.set(main.auth.SESSION_COOKIE, "session")

    def url(self, tail):
        return f"/api/cluster/nodes/{NODE['id']}/proxy{tail}"

    def test_a_grant_on_the_nodes_server_passes_and_the_node_is_told(self):
        G.set_user("alex", {"n-abc123/minecraft-java-1": ["files"]})
        self.assertEqual(self.client.get(self.url("/api/servers/minecraft-java-1/files")).status_code, 200)
        self.assertEqual(self.calls, [{"path": "/api/servers/minecraft-java-1/files", "role": "user",
                                       "perms": {"view", "files"}}])

    def test_without_it_nothing_reaches_the_node(self):
        G.set_user("alex", {"minecraft-java-1": list(G.PERMS), "n-ffff/minecraft-java-1": list(G.PERMS)})
        r = self.client.get(self.url("/api/servers/minecraft-java-1/files"))
        self.assertEqual(r.status_code, 403)
        self.assertIn("Files", r.json()["detail"])
        self.assertEqual(self.client.post(self.url("/api/servers/deploy"), json={}).status_code, 403)
        self.assertEqual(self.client.delete(self.url("/api/servers/minecraft-java-1")).status_code, 403)
        self.assertEqual(self.client.post(f"/api/cluster/nodes/{NODE['id']}/servers/minecraft-java-1/action",
                                          json={"action": "stop"}).status_code, 403)
        self.assertEqual(self.calls, [])

    def test_admins_pass_as_admins(self):
        self.user["role"] = "admin"
        self.assertEqual(self.client.get(self.url("/api/servers/minecraft-java-1/files")).status_code, 200)
        self.assertEqual(self.calls[0]["role"], "admin")
        self.assertIsNone(self.calls[0]["perms"])

    def test_a_nodes_server_list_and_console_are_cut_down(self):
        G.set_user("alex", {"n-abc123/minecraft-2": ["console"]})
        self.body = json.dumps([{"name": "minecraft"}, {"name": "minecraft-2"}]).encode()
        got = self.client.get(self.url("/api/servers")).json()
        self.assertEqual(got, [{"name": "minecraft-2", "perms": ["view", "console"]}])
        self.body = json.dumps({"content": "[POWER] stop minecraft\n[POWER] stop minecraft-2"}).encode()
        self.assertEqual(self.client.get(self.url("/api/logs")).json(), {"content": "[POWER] stop minecraft-2"})

    def test_the_overview_lists_only_granted_servers(self):
        G.set_user("alex", {"n-abc123/minecraft-2": ["view"], "minecraft": ["power"]})
        ov = {"cluster": "Home", "epoch": 1, "nodes": [
            {"id": "local", "local": True, "servers": [{"name": "minecraft"}, {"name": "minecraft-2"}]},
            {"id": "n-abc123", "servers": [{"name": "minecraft"}, {"name": "minecraft-2"}]}]}
        with mock.patch.object(self.main.cluster_client, "overview", lambda fn: ov):
            got = self.client.get("/api/cluster/overview").json()["nodes"]
        self.assertEqual([[(s["name"], s["perms"]) for s in n["servers"]] for n in got],
                         [[("minecraft", ["view", "power"])], [("minecraft-2", ["view"])]])


@unittest.skipUnless(HAVE_FASTAPI, "needs fastapi")
class NodeSide(unittest.TestCase):
    """A node acting on its master's word about what the master's user may do."""

    def setUp(self):
        import main
        self.main = main
        base = {"username": "master:Main", "source": "cluster", "role": "user", "master": "Main"}
        for p in (mock.patch.object(main.cluster, "read_node_token", lambda *a: dict(base)),
                  mock.patch.object(main.manager, "write_log", lambda m: None),
                  mock.patch.object(main.manager, "control_server", lambda s, a: None),
                  mock.patch.object(main.files, "list_dir", lambda s, p="": {"path": p, "entries": []})):
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(main.app)

    def call(self, method, path, perms=None, role="user", **kw):
        headers = {"Authorization": "Bearer rpn_" + "x" * 43, "X-RedstonePanel-Epoch": "1",
                   "X-RedstonePanel-Actor": "alex", "X-RedstonePanel-Role": role}
        if perms is not None:
            headers["X-RedstonePanel-Perms"] = perms
        return self.client.request(method, path, headers=headers, **kw)

    def test_the_masters_word_decides(self):
        self.assertEqual(self.call("GET", "/api/servers/minecraft/files", perms="view,files").status_code, 200)
        self.assertEqual(self.call("GET", "/api/servers/minecraft/files", perms="view,console").status_code, 403)
        self.assertEqual(self.call("POST", "/api/servers/minecraft/action", perms="view", json={"action": "stop"})
                         .status_code, 403)
        self.assertEqual(self.call("POST", "/api/servers/minecraft/action", perms="", json={"action": "stop"})
                         .status_code, 403)

    def test_a_master_before_1_18_keeps_the_old_rules(self):
        self.assertEqual(self.call("POST", "/api/servers/minecraft/action", json={"action": "stop"}).status_code, 200)
        self.assertEqual(self.call("GET", "/api/servers/minecraft/files").status_code, 403, "files were admin-only")
        self.assertEqual(self.call("GET", "/api/servers/minecraft/files", role="admin").status_code, 200)


if __name__ == "__main__":
    unittest.main()
