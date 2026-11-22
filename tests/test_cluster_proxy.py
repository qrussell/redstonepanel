"""1.15 (clusters phase 2): the master's proxy to its nodes, and how a node treats the master's
requests. Run: python -m unittest discover -s tests

These go through the real middleware and routes with FastAPI's TestClient. The network call to the
node (cluster_client.open_proxy) and sign-in are replaced by stand-ins.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-proxy-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import fastapi  # noqa: F401
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except (ImportError, RuntimeError):     # RuntimeError: starlette's test client without its HTTP client
    HAVE_FASTAPI = False

NODE = {"id": "n-abc123", "name": "Basement", "url": "https://192.168.1.20:8443", "token": "rpn_" + "x" * 43,
        "cluster_api": 2, "version": "1.15.0"}


class FakeAnswer:
    """What open_proxy returns: a node's answer."""
    def __init__(self, status=200, body=b'{"ok": true}', headers=None):
        self.status, self.body = status, body
        self.headers = headers or {"content-type": "application/json"}
        self.closed = False

    def chunks(self):
        yield self.body[:3]
        yield self.body[3:]
        self.closed = True

    def read_all(self, limit=None):
        self.closed = True
        return self.body

    def close(self):
        self.closed = True


@unittest.skipUnless(HAVE_FASTAPI, "needs fastapi")
class MasterProxy(unittest.TestCase):
    def setUp(self):
        import main
        self.main = main
        self.user = {"username": "alex", "role": "user", "source": "panel"}
        self.calls, self.log = [], []
        self.answer = FakeAnswer()

        def open_proxy(node, method, path, body, length, content_type, actor, role, perms=None):
            self.calls.append({"node": node["id"], "method": method, "path": path, "length": length,
                               "body": body.read() if body else None, "content_type": content_type,
                               "actor": actor, "role": role, "perms": perms})
            return self.answer
        for p in (mock.patch.object(main.auth, "read_session_token", lambda t: dict(self.user)),
                  # 1.18: alex was given everything on every server (tests/test_grants.py covers the rest)
                  mock.patch.object(main.grants, "get", lambda name: {"*": list(main.grants.PERMS)}),
                  mock.patch.object(main.cluster, "role", lambda: "master"),
                  mock.patch.object(main.cluster, "find_node", lambda i: dict(NODE) if i == NODE["id"] else None),
                  mock.patch.object(main.cluster_client, "open_proxy", open_proxy),
                  mock.patch.object(main.cluster_client, "forget_cached", lambda *a: None),
                  mock.patch.object(main.manager, "write_log", self.log.append),
                  mock.patch.object(main.panel_settings, "display_name", lambda *a: "Main")):
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(main.app)
        self.client.cookies.set(main.auth.SESSION_COOKIE, "session")

    def url(self, tail):
        return f"/api/cluster/nodes/{NODE['id']}/proxy{tail}"

    def test_get_is_passed_on_with_the_users_name_and_role(self):
        r = self.client.get(self.url("/api/servers/minecraft-java-1/players/log?days=7&player=Steve"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"ok": True})
        [c] = self.calls
        self.assertEqual(c["method"], "GET")
        self.assertEqual(c["path"], "/api/servers/minecraft-java-1/players/log?days=7&player=Steve")
        self.assertEqual((c["actor"], c["role"]), ("alex", "user"))
        self.assertIsNone(c["body"])
        self.assertTrue(self.answer.closed)
        self.assertEqual(self.log, [], "reading isn't logged")

    def test_admins_are_admins_on_the_node(self):
        self.user = {"username": "root", "role": "admin", "source": "linux"}
        self.client.get(self.url("/api/java/runtimes"))
        self.assertEqual(self.calls[0]["role"], "admin")

    def test_bodies_and_downloads_go_through(self):
        r = self.client.post(self.url("/api/servers/minecraft-java-1/whitelist/add"), json={"player": "Steve"})
        self.assertEqual(r.status_code, 200)
        c = self.calls[0]
        self.assertEqual(json.loads(c["body"]), {"player": "Steve"})
        self.assertEqual(c["length"], len(c["body"]))
        self.assertEqual(c["content_type"], "application/json")
        self.assertEqual(self.log, ["[CLUSTER] alex@Main: POST /api/servers/minecraft-java-1/whitelist/add on Basement"])
        # a file comes back as it is, with its name
        self.answer = FakeAnswer(body=b"PK\x03\x04zipdata", headers={
            "content-type": "application/zip", "content-disposition": 'attachment; filename="w.zip"'})
        r = self.client.get(self.url("/api/servers/minecraft-java-1/worlds/export"))
        self.assertEqual(r.content, b"PK\x03\x04zipdata")
        self.assertEqual(r.headers["content-disposition"], 'attachment; filename="w.zip"')

    def test_uploads_keep_their_form(self):
        r = self.client.post(self.url("/api/servers/minecraft-java-1/files/upload?path=plugins"),
                             files={"file": ("a.jar", b"jarbytes")})
        self.assertEqual(r.status_code, 200)
        c = self.calls[0]
        self.assertTrue(c["content_type"].startswith("multipart/form-data; boundary="))
        self.assertIn(b"jarbytes", c["body"])
        self.assertEqual(c["path"], "/api/servers/minecraft-java-1/files/upload?path=plugins")

    def test_node_status_codes_pass_through(self):
        self.answer = FakeAnswer(409, b'{"detail": "Needs Java 21", "java": {"required": 21}}')
        self.user["role"] = "admin"             # creating a server is an admin's job (1.18)
        r = self.client.post(self.url("/api/servers/deploy"), json={"edition": "java"})
        self.assertEqual(r.status_code, 409, "the browser can offer to install Java on the node")
        self.assertEqual(r.json()["java"], {"required": 21})

    def test_a_node_refusing_the_master_never_signs_the_user_out(self):
        self.answer = FakeAnswer(401, b'{"detail": "Not this cluster\'s master."}')
        r = self.client.get(self.url("/api/servers/minecraft-java-1/properties"))
        self.assertEqual(r.status_code, 502)
        self.assertIn("Basement", r.json()["detail"])

    def test_an_old_node_says_to_update(self):
        self.answer = FakeAnswer(403, b'{"detail": "Not allowed for the cluster master."}')
        r = self.client.get(self.url("/api/servers/minecraft-java-1/properties"))
        self.assertEqual(r.status_code, 403)
        self.assertIn("Update it to 1.15", r.json()["detail"])
        with mock.patch.object(self.main.cluster, "find_node", lambda i: dict(NODE, cluster_api=1, version="1.14.2")), \
                mock.patch.object(self.main.cluster_client, "_hello_fields", lambda n: {}):
            r = self.client.get(self.url("/api/servers/minecraft-java-1/properties"))
        self.assertEqual(r.status_code, 409)
        self.assertIn("1.14.2", r.json()["detail"])

    def test_refused_without_asking_the_node(self):
        for method, tail in (("GET", "/api/users"), ("PUT", "/api/settings"), ("DELETE", "/api/cluster/membership"),
                             ("PUT", "/api/cluster/cert"), ("POST", "/api/cluster/templates"),
                             ("POST", "/api/panel/update"), ("DELETE", "/api/servers/removed/minecraft-old"),
                             ("GET", "/api/servers/minecraft-a/../../users")):
            r = self.client.request(method, self.url(tail))
            self.assertIn(r.status_code, (403, 404), f"{method} {tail}")
        self.assertEqual(self.calls, [])

    def test_unknown_node(self):
        self.assertEqual(self.client.get("/api/cluster/nodes/n-nope/proxy/api/servers").status_code, 404)

    def test_trusted_lan_visitors_get_nothing(self):
        self.user = {"username": "lan", "role": "user", "source": "trusted"}
        r = self.client.get(self.url("/api/servers/minecraft-java-1/properties"))
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.calls, [])

    def test_only_on_a_master(self):
        with mock.patch.object(self.main.cluster, "role", lambda: "standalone"):
            self.assertEqual(self.client.get(self.url("/api/servers")).status_code, 404)

    def test_template_push(self):
        sent = []
        tpl = {"id": "t-1", "name": "Survival", "edition": "Java", "properties": {}, "permissions": "[]"}
        self.user["role"] = "admin"             # sending templates to a node is an admin's job (1.18)
        with mock.patch.object(self.main.manager, "load_templates", lambda: [tpl]), \
                mock.patch.object(self.main.cluster_client, "call_node",
                                  lambda node, m, p, body=None, **kw: sent.append((m, p, body, kw)) or {}):
            r = self.client.post(f"/api/cluster/nodes/{NODE['id']}/templates", json={"template_id": "t-1"})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(self.client.post(f"/api/cluster/nodes/{NODE['id']}/templates",
                                              json={"template_id": "nope"}).status_code, 404)
        [(m, p, body, kw)] = sent
        self.assertEqual((m, p, body["template"]["id"], kw["actor"], kw["role"]),
                         ("POST", "/api/cluster/templates", "t-1", "alex", "admin"))


    def test_save_a_node_server_as_a_master_template(self):
        saved = []
        answers = {"/api/servers": [{"name": "minecraft-java-1", "edition": "Java"}],
                   "/api/servers/minecraft-java-1/properties": {"difficulty": "hard"},
                   "/api/servers/minecraft-java-1/permissions/raw": {"content": "[]"}}
        with mock.patch.object(self.main.manager, "load_templates", lambda: []), \
                mock.patch.object(self.main.manager, "save_templates", saved.append), \
                mock.patch.object(self.main.cluster_client, "call_node", lambda node, m, p, **kw: answers[p]):
            r = self.client.post(f"/api/cluster/nodes/{NODE['id']}/servers/minecraft-java-1/template",
                                 json={"name": "Hard survival"})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(self.client.post(f"/api/cluster/nodes/{NODE['id']}/servers/minecraft-nope/template",
                                              json={"name": "x"}).status_code, 404)
        [[t]] = saved
        self.assertEqual((t["name"], t["edition"], t["properties"], t["permissions"]),
                         ("Hard survival", "Java", {"difficulty": "hard"}, "[]"))


class Housekeeping(unittest.TestCase):
    """The master's hourly pass: automatic node certificate renewal and the nightly backup."""
    def setUp(self):
        from core import cluster, cluster_client
        self.C, self.CC = cluster, cluster_client
        self.now = 1_800_000_000
        self.nodes = [dict(NODE, id="n-soon", name="Soon", cert_expires=self.now + 10 * 86400),
                      dict(NODE, id="n-later", name="Later", cert_expires=self.now + 200 * 86400),
                      dict(NODE, id="n-own", name="Own", verify="system", cert_expires=self.now + 86400)]
        self.renewed, self.log, self.updates = [], [], []
        for p in (mock.patch.object(cluster, "role", lambda: "master"),
                  mock.patch.object(cluster, "load_nodes", lambda: [dict(n) for n in self.nodes]),
                  mock.patch.object(cluster, "update_node", lambda i, **kw: self.updates.append((i, kw))),
                  mock.patch.object(cluster, "nightly_due", lambda now=None: False),
                  mock.patch.object(cluster_client, "_hello_fields", lambda n: {}),
                  mock.patch.object(cluster_client, "renew_node",
                                    lambda n, actor: self.renewed.append((n["id"], actor)) or self.now + 825 * 86400),
                  mock.patch.object(cluster_client.panel_settings, "load", lambda: {"https_mode": "custom"})):
            p.start()
            self.addCleanup(p.stop)

    def test_renews_only_cluster_certificates_due_within_30_days(self):
        self.CC.maintain_once(self.log.append, lambda: None, now=self.now)
        self.assertEqual(self.renewed, [("n-soon", "automatic renewal")])
        self.assertIn("Renewed the certificate of node 'Soon' automatically", self.log[0])

    def test_failures_are_recorded_for_the_admin(self):
        def fail(n, actor):
            raise self.CC.NodeError("unreachable", "timed out")
        with mock.patch.object(self.CC, "renew_node", fail):
            self.CC.maintain_once(self.log.append, lambda: None, now=self.now)
        self.assertEqual(self.updates[0][0], "n-soon")
        self.assertIn("timed out", self.updates[0][1]["renew_error"])
        self.assertIn("failed", self.log[0])

    def test_nightly_backup_when_due(self):
        with mock.patch.object(self.C, "nightly_due", lambda now=None: True), \
                mock.patch.object(self.C, "run_nightly", lambda now=None: "redstonepanel-cluster-20270115.rpbackup"):
            self.CC.maintain_once(self.log.append, lambda: None, now=self.now)
        self.assertIn("Nightly cluster backup written: redstonepanel-cluster-20270115.rpbackup.", self.log[-1])

    def test_not_on_nodes(self):
        with mock.patch.object(self.C, "role", lambda: "node"):
            self.CC.maintain_once(self.log.append, lambda: None, now=self.now)
        self.assertEqual((self.renewed, self.log), ([], []))


@unittest.skipUnless(HAVE_FASTAPI, "needs fastapi")
class NodeSide(unittest.TestCase):
    """A node answering its master's requests (Bearer node token)."""
    def setUp(self):
        import main
        self.main = main
        self.log, self.templates = [], []
        base = {"username": "master:Main", "source": "cluster", "role": "user", "master": "Main"}
        for p in (mock.patch.object(main.cluster, "read_node_token", lambda *a: dict(base)),
                  mock.patch.object(main.manager, "write_log", self.log.append),
                  mock.patch.object(main.manager, "_check_server", lambda s: None),
                  mock.patch.object(main.manager, "remove_server", lambda s, keep: None),
                  mock.patch.object(main.manager, "load_templates", lambda: list(self.templates)),
                  mock.patch.object(main.manager, "save_templates", lambda t: self.templates.__setitem__(slice(None), t)),
                  mock.patch.object(main.runtimes, "status", lambda: {"installed": []})):
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(main.app)

    def call(self, method, path, role=None, actor="alex", **kw):
        headers = {"Authorization": "Bearer rpn_" + "x" * 43, "X-RedstonePanel-Epoch": "1",
                   "X-RedstonePanel-Actor": actor}
        if role:
            headers["X-RedstonePanel-Role"] = role
        return self.client.request(method, path, headers=headers, **kw)

    def test_master_users_get_a_users_rights(self):
        self.assertEqual(self.call("GET", "/api/java/runtimes", role="user").status_code, 403)
        self.assertEqual(self.call("GET", "/api/java/runtimes").status_code, 403, "older masters send no role")
        self.assertEqual(self.call("GET", "/api/java/runtimes", role="admin").status_code, 200)

    def test_actions_are_logged_with_the_master_users_name(self):
        r = self.call("DELETE", "/api/servers/minecraft-java-1", role="admin")
        self.assertEqual(r.status_code, 200)
        self.assertIn("[CLUSTER] alex@Main: DELETE /api/servers/minecraft-java-1 (from testclient)", self.log)
        self.assertTrue(any(line.startswith("[REMOVE] alex@Main is uninstalling") for line in self.log), self.log)

    def test_the_master_cant_erase_a_servers_files(self):
        r = self.call("DELETE", "/api/servers/minecraft-java-1?keep_world=false", role="admin")
        self.assertEqual(r.status_code, 403)
        self.assertIn("removed server", r.json()["detail"])

    def test_never_users_settings_or_membership_changes_by_proxy(self):
        for method, path in (("GET", "/api/users"), ("PUT", "/api/settings"), ("GET", "/api/settings/cluster"),
                             ("POST", "/api/panel/update"), ("DELETE", "/api/servers/removed/minecraft-old")):
            self.assertEqual(self.call(method, path, role="admin").status_code, 403, f"{method} {path}")

    def test_templates_pushed_by_the_master(self):
        t = {"id": "t-1", "name": "Survival", "edition": "Java", "properties": {"pvp": "false"}, "permissions": "[]"}
        self.assertEqual(self.call("POST", "/api/cluster/templates", json={"template": t}).status_code, 200)
        self.assertEqual(self.call("POST", "/api/cluster/templates", json={"template": dict(t, name="Hard")}).status_code, 200)
        self.assertEqual([(x["id"], x["name"], x["from_master"]) for x in self.templates], [("t-1", "Hard", "Main")])
        self.assertEqual(self.call("POST", "/api/cluster/templates", json={"template": dict(t, id="../x")}).status_code, 400)

    def test_signed_in_users_cant_push_templates(self):
        with mock.patch.object(self.main.auth, "read_session_token",
                               lambda t: {"username": "root", "role": "admin", "source": "linux"}):
            self.client.cookies.set(self.main.auth.SESSION_COOKIE, "s")
            r = self.client.post("/api/cluster/templates", json={"template": {}})
        self.assertEqual(r.status_code, 403)


@unittest.skipUnless(HAVE_FASTAPI, "needs fastapi")
class MasterHttpsMode(unittest.TestCase):
    """A master may switch between cluster HTTPS and its own certificate; nodes may not."""
    def setUp(self):
        import main
        self.main = main
        self.saved = []
        old = dict(main.panel_settings.DEFAULTS, https_mode="cluster", https_port=8443)
        for p in (mock.patch.object(main.panel_settings, "load", lambda: dict(old)),
                  mock.patch.object(main.panel_settings, "validate", lambda new: dict(old, **new)),
                  mock.patch.object(main.panel_settings, "save", self.saved.append),
                  mock.patch.object(main.panel_settings, "port_free", lambda p: True),
                  mock.patch.object(main.panel_settings, "RUNTIME", {"scheme": "https", "port": 8443}),
                  mock.patch.object(main.manager, "write_log", lambda m: None),
                  mock.patch.object(main, "_restart_soon", lambda: None)):
            p.start()
            self.addCleanup(p.stop)

    def put(self, role, new):
        from types import SimpleNamespace
        req = SimpleNamespace(state=SimpleNamespace(user={"username": "root", "role": "admin", "source": "linux"}),
                              headers={"host": "x"})
        with mock.patch.object(self.main.cluster, "role", lambda: role):
            return self.main.update_settings_api(req, new)

    def test_master_switches_to_its_own_certificate(self):
        r = self.put("master", {"https_mode": "custom", "cert_file": "/etc/letsencrypt/live/x/fullchain.pem",
                                "key_file": "/etc/letsencrypt/live/x/privkey.pem"})
        self.assertTrue(r["restart"])
        self.assertEqual(self.saved[-1]["https_mode"], "custom")

    def test_nodes_and_other_modes_are_refused(self):
        for role, new in (("node", {"https_mode": "custom"}), ("master", {"https_mode": "self_signed"}),
                          ("master", {"https_port": 9443})):
            with self.assertRaises(self.main.HTTPException, msg=f"{role} {new}"):
                self.put(role, new)


if __name__ == "__main__":
    unittest.main()
