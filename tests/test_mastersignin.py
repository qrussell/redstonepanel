"""1.25: Open dashboard from the master, signed in (core/mastersignin.py).
Run: python -m unittest discover -s tests

The node gives the master's admin a one-use ticket worth a minute; the browser turns it into a
session for "<admin>@<master>" with the admin role. It ends when the node leaves the cluster,
gets a new master or turns the setting off, and it has no account here to change.
"""
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-msi-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
os.environ.setdefault("RP_FAILS_FILE", os.path.join(TMP, "fails.json"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import auth, cluster, mastersignin as M  # noqa: E402
from core import settings as panel_settings  # noqa: E402

try:
    import fastapi  # noqa: F401
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except (ImportError, RuntimeError):
    HAVE_FASTAPI = False

ADMIN = {"username": "quentin@Home", "source": "cluster", "role": "admin", "master": "Home", "actor": "quentin"}


class Base(unittest.TestCase):
    def setUp(self):
        self.st = {"role": "node", "epoch": 3, "master_name": "Home"}
        self.settings = dict(panel_settings.DEFAULTS)
        for p in (mock.patch.object(cluster, "state", lambda: dict(self.st)),
                  mock.patch.object(panel_settings, "load", lambda: dict(self.settings))):
            p.start()
            self.addCleanup(p.stop)
        M._used.clear()
        auth.reset_fails()


class Tickets(Base):
    def test_a_ticket_gives_an_admin_session_once(self):
        identity = M.redeem(M.issue(ADMIN))
        self.assertEqual(identity, {"u": "quentin@Home", "src": "master", "m": 1, "e": 3})
        user = auth.read_session_token(auth.make_session_token(identity))
        self.assertEqual((user["username"], user["source"], user["role"]), ("quentin@Home", "master", "admin"))
        self.assertFalse(user["needs_2fa_setup"])
        t = M.issue(ADMIN)
        M.redeem(t)
        with self.assertRaises(M.SignInError):
            M.redeem(t)

    def test_only_a_master_admin_on_a_node_that_allows_it(self):
        with self.assertRaises(M.SignInError):
            M.issue(dict(ADMIN, role="user"))
        with self.assertRaises(M.SignInError):
            M.issue({"username": "alex", "source": "panel", "role": "admin"})
        self.settings["master_sign_in"] = False
        with self.assertRaises(M.SignInError):
            M.issue(ADMIN)
        self.settings["master_sign_in"] = True
        self.st["role"] = "standalone"
        with self.assertRaises(M.SignInError):
            M.issue(ADMIN)

    def test_old_forged_or_from_another_master(self):
        t = M.issue(ADMIN)
        with self.assertRaises(M.SignInError):
            M.redeem(t[:-2] + ("aa" if not t.endswith("aa") else "bb"))
        later = time.time() + M.TICKET_SECONDS + 5
        with mock.patch("core.auth.time.time", lambda: later):
            with self.assertRaises(M.SignInError):
                M.redeem(t)
        self.st["epoch"] = 4                                    # a newer master since
        with self.assertRaises(M.SignInError):
            M.redeem(t)
        session = auth.make_session_token({"u": "quentin@Home", "src": "master", "m": 1, "e": 4})
        self.assertEqual(auth.read_session_token(session)["role"], "admin")
        with self.assertRaises(M.SignInError):                  # someone else's panel's ticket
            M.redeem(auth._sign({"a": "x", "m": "y", "e": 4, "j": "1", "exp": later}, "two-factor pending"))

    def test_the_session_ends_with_the_cluster_or_the_setting(self):
        token = auth.make_session_token(M.redeem(M.issue(ADMIN)))
        self.assertIsNotNone(auth.read_session_token(token))
        self.settings["master_sign_in"] = False
        self.assertIsNone(auth.read_session_token(token))
        self.settings["master_sign_in"] = True
        self.st["epoch"] = 4
        self.assertIsNone(auth.read_session_token(token))
        self.st.update(epoch=3, role="standalone")
        self.assertIsNone(auth.read_session_token(token))


@unittest.skipUnless(HAVE_FASTAPI, "needs fastapi")
class NodeApi(Base):
    def setUp(self):
        super().setUp()
        import main
        self.main = main
        self.log = []
        base = {"username": "master:Home", "source": "cluster", "role": "user", "master": "Home"}
        for p in (mock.patch.object(main.cluster, "read_node_token", lambda *a: dict(base)),
                  mock.patch.object(main.manager, "write_log", self.log.append)):
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(main.app)

    def ticket(self, role="admin"):
        return self.client.post("/api/cluster/sign-in", json={}, headers={
            "Authorization": "Bearer rpn_" + "x" * 43, "X-RedstonePanel-Epoch": "3",
            "X-RedstonePanel-Actor": "quentin", "X-RedstonePanel-Role": role})

    def test_ticket_link_and_the_session(self):
        self.assertEqual(self.ticket(role="user").status_code, 403, "a master user isn't enough")
        r = self.ticket()
        self.assertEqual(r.status_code, 200, r.text)
        page = self.client.get("/api/auth/master-sign-in", params={"ticket": r.json()["ticket"]})
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn("url=/", page.text)
        self.assertIn(auth.SESSION_COOKIE, page.cookies)
        self.assertTrue(any("quentin@Home signed in from" in line for line in self.log), self.log)
        me = self.client.get("/api/auth/me").json()
        self.assertEqual((me["username"], me["role"], me["source"]), ("quentin@Home", "admin", "master"))
        self.assertEqual(self.client.get("/api/users").status_code, 200, "an admin here")
        for method, path in (("POST", "/api/auth/password"), ("POST", "/api/auth/2fa/setup"),
                             ("GET", "/api/auth/passkeys"), ("GET", "/api/auth/devices")):
            self.assertEqual(self.client.request(method, path, json={}).status_code, 403, path)
        self.assertEqual(self.client.post("/api/cluster/sign-in", json={}).status_code, 403, "never from a browser")

    def test_a_used_or_wrong_link(self):
        t = self.ticket().json()["ticket"]
        self.client.get("/api/auth/master-sign-in", params={"ticket": t})
        self.client.cookies.clear()
        again = self.client.get("/api/auth/master-sign-in", params={"ticket": t})
        self.assertEqual(again.status_code, 403)
        self.assertIn("used already", again.text)
        self.assertNotIn(auth.SESSION_COOKIE, again.cookies)
        bad = self.client.get("/api/auth/master-sign-in", params={"ticket": "<script>x</script>.00"})
        self.assertEqual(bad.status_code, 403)
        self.assertNotIn("<script>", bad.text)

    def test_the_nodes_own_switch(self):
        saved = {}
        user = {"username": "root", "source": "linux", "role": "admin"}
        with mock.patch.object(self.main.auth, "read_session_token", lambda t: dict(user)), \
                mock.patch.object(self.main.panel_settings, "save", lambda s: saved.update(s)):
            self.client.cookies.set(auth.SESSION_COOKIE, "s")
            r = self.client.put("/api/settings/master-sign-in", json={"enabled": False})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertIs(saved["master_sign_in"], False)
            self.assertIn("[SETTINGS] root turned signing in from the cluster's master off.", self.log)
            self.client.cookies.clear()
        self.settings["master_sign_in"] = False
        self.assertEqual(self.ticket().status_code, 403)


@unittest.skipUnless(HAVE_FASTAPI, "needs fastapi")
class MasterApi(unittest.TestCase):
    NODE = {"id": "n-abc123", "name": "Basement", "url": "https://192.168.1.20:8443", "token": "rpn_" + "x" * 43,
            "cluster_api": 10, "version": "1.25.0", "master_sign_in": True}

    def setUp(self):
        import main
        self.main = main
        self.user = {"username": "quentin", "role": "admin", "source": "linux"}
        self.calls, self.node = [], dict(self.NODE)

        def call_node(node, method, path, body=None, actor=None, timeout=None, role=None):
            self.calls.append((method, path, actor, role))
            return {"ticket": "abc.def"}
        for p in (mock.patch.object(main.auth, "read_session_token", lambda t: dict(self.user)),
                  mock.patch.object(main.cluster, "role", lambda: "master"),
                  mock.patch.object(main.cluster, "find_node", lambda i: dict(self.node) if i == self.node["id"] else None),
                  mock.patch.object(main.cluster_client, "call_node", call_node),
                  mock.patch.object(main.manager, "write_log", lambda m: None)):
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(main.app)
        self.client.cookies.set(main.auth.SESSION_COOKIE, "session")

    def test_an_admin_gets_the_nodes_link(self):
        r = self.client.post("/api/cluster/nodes/n-abc123/sign-in")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["url"], "https://192.168.1.20:8443/api/auth/master-sign-in?ticket=abc.def")
        self.assertEqual(self.calls, [("POST", "/api/cluster/sign-in", "quentin", "admin")])

    def test_not_for_users_or_older_nodes(self):
        self.user = {"username": "alex", "role": "user", "source": "panel"}
        self.assertEqual(self.client.post("/api/cluster/nodes/n-abc123/sign-in").status_code, 403)
        self.user = {"username": "quentin", "role": "admin", "source": "linux"}
        self.node["cluster_api"] = 9
        r = self.client.post("/api/cluster/nodes/n-abc123/sign-in")
        self.assertEqual(r.status_code, 400)
        self.assertIn("1.25", r.json()["detail"])
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
