"""Tests for core/cluster.py. Run: python -m unittest discover -s tests

The flow tests run a master and a node in one process by pointing the data paths at a
different temporary directory for each panel (see Panels.use).
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import auth, settings  # noqa: E402
from core import cluster as C  # noqa: E402

HAVE_OPENSSL = shutil.which("openssl") is not None
try:
    import fastapi  # noqa: F401
    HAVE_FASTAPI = True
except ImportError:
    HAVE_FASTAPI = False


class Panels:
    """Several panels' data directories; use(name) makes one of them current."""
    IDENT = {"master": {"names": ["master.lan"], "ips": ["192.168.1.10"]},
             "node": {"names": ["node.lan"], "ips": ["192.168.1.20"]},
             "other": {"names": ["other.lan"], "ips": ["192.168.1.30"]}}

    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="rp-cluster-test-")
        self.current = None
        self._saved = (C.CLUSTER_DIR, C.DATA_DIR, C.own_identity, auth.USERS_FILE, auth.SECRET_FILE,
                       settings.SETTINGS_FILE)

    def use(self, name):
        d = os.path.join(self.root, name)
        os.makedirs(d, exist_ok=True)
        C.CLUSTER_DIR = os.path.join(d, "cluster")
        C.DATA_DIR = d
        C.own_identity = lambda: {k: list(v) for k, v in self.IDENT[name].items()}
        auth.USERS_FILE = os.path.join(d, "users.json")
        auth.SECRET_FILE = os.path.join(d, "secret.key")
        settings.SETTINGS_FILE = os.path.join(d, "settings.json")
        settings._cache.update(mtime=None, data=None)
        C._token_cache.clear()
        auth._fails.clear()
        self.current = name

    def close(self):
        (C.CLUSTER_DIR, C.DATA_DIR, C.own_identity, auth.USERS_FILE, auth.SECRET_FILE,
         settings.SETTINGS_FILE) = self._saved
        settings._cache.update(mtime=None, data=None)
        shutil.rmtree(self.root, ignore_errors=True)


class FakeClient:
    """Stands in for core.cluster_client: 'sends' the node's requests to the master panel."""
    def __init__(self, panels, master="master", node_ip="192.168.1.20"):
        self.panels, self.master, self.node_ip = panels, master, node_ip

    def _on_master(self, fn):
        back = self.panels.current
        self.panels.use(self.master)
        try:
            return fn()
        finally:
            self.panels.use(back)

    def fetch_master_ca(self, url):
        return self._on_master(C.read_ca)

    def post_join(self, url, ca_pem, body):
        return self._on_master(lambda: C.accept_join(body, self.node_ip)[0])


class CodesAndRules(unittest.TestCase):
    def test_join_code_round_trip(self):
        code = C.encode_code("https://192.168.1.10:8443", "rpj_abcd1234xyz", "sha256:00ff")
        self.assertTrue(code.startswith("rpjoin1:"))
        # line breaks picked up when pasting are ignored
        d = C.decode_code(code[:20] + "\n  " + code[20:])
        self.assertEqual(d, {"u": "https://192.168.1.10:8443", "s": "rpj_abcd1234xyz", "ca": "sha256:00ff"})

    def test_bad_join_codes(self):
        for bad in ("", "hello", "rpjoin1:!!!", "rpjoin1:" + C._b64(b'{"u":"http://x","s":"rpj_1","ca":"sha256:1"}'),
                    "rpjoin1:" + C._b64(b'{"u":"https://x","s":"nope","ca":"sha256:1"}'),
                    "rpjoin1:" + C._b64(b'[1,2]')):
            with self.assertRaises(C.ClusterError, msg=bad):
                C.decode_code(bad)

    def test_node_token_allowlist(self):
        allowed = [("GET", "/api/servers"), ("POST", "/api/servers/minecraft-java-1/action"),
                   ("GET", "/api/cluster/hello"), ("DELETE", "/api/cluster/membership"),
                   ("GET", "/api/logs"), ("GET", "/api/templates"),
                   ("POST", "/api/cluster/cert/csr"), ("PUT", "/api/cluster/cert")]
        refused = [("GET", "/api/users"), ("POST", "/api/users"), ("PUT", "/api/settings"),
                   ("GET", "/api/settings/cluster"), ("POST", "/api/settings/cluster/backup"),
                   ("DELETE", "/api/servers/minecraft-java-1"), ("POST", "/api/servers/a/b/action"),
                   ("POST", "/api/auth/password"), ("GET", "/api/cluster/overview"),
                   ("POST", "/api/cluster/nodes/n-1/servers/x/action"), ("POST", "/api/servers"),
                   ("GET", "/api/servers/x/export"), ("POST", "/api/servers/deploy"),
                   ("GET", "/api/servers/"), ("GET", "/api/serversX")]
        for m, p in allowed:
            self.assertTrue(C.path_allowed(m, p), f"{m} {p}")
        for m, p in refused:
            self.assertFalse(C.path_allowed(m, p), f"{m} {p}")

    def test_certificate_names(self):
        for ok in ("box.lan", "basement.local", "mc.home.arpa", "a.b.internal", "lan"):
            self.assertTrue(C.allowed_name(ok), ok)
        for bad in ("basement", "example.com", "evil.lan.example.com", "localhost", "", "a..lan"):
            self.assertFalse(C.allowed_name(bad), bad)
        for ok in ("10.1.2.3", "172.16.0.1", "172.31.255.254", "192.168.1.5", "100.64.0.1", "::ffff:192.168.1.5"):
            self.assertTrue(C.allowed_ip(ok), ok)
        for bad in ("8.8.8.8", "127.0.0.1", "172.32.0.1", "fd00::1", "nope", "100.128.0.1"):
            self.assertFalse(C.allowed_ip(bad), bad)


@unittest.skipUnless(HAVE_OPENSSL, "needs the openssl command")
class ClusterFlow(unittest.TestCase):
    def setUp(self):
        self.p = Panels()
        self.p.use("master")
        C.create_cluster("Home", "alice")
        self.master_ca = C.read_ca()
        code = C.create_join("alice", "https://192.168.1.10:8443")["code"]
        self.p.use("node")
        auth.create_panel_user("nodeadmin", "node-password-1", "admin")
        self.node_state = C.join(code, "bob", FakeClient(self.p))
        self.p.use("master")
        self.token = C.load_nodes()[0]["token"]
        self.p.use("node")

    def tearDown(self):
        self.p.close()

    def test_roles_and_files(self):
        self.assertEqual(C.role(), "node")
        self.assertEqual(settings.load()["https_mode"], "cluster")
        self.assertEqual(self.node_state["cluster_name"], "Home")
        with open(os.path.join(C.CLUSTER_DIR, "cluster.json")) as f:
            self.assertNotIn(self.token, f.read())
        self.assertEqual(os.stat(C.CLUSTER_DIR).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(C.node_key()).st_mode & 0o777, 0o600)
        subprocess.run(["openssl"] + C.verify_args(C.node_crt()), check=True, capture_output=True)
        san = subprocess.run(["openssl", "x509", "-in", C.node_crt(), "-noout", "-ext", "subjectAltName"],
                             capture_output=True, text=True).stdout
        self.assertIn("node.lan", san)
        self.assertIn("192.168.1.20", san)
        self.p.use("master")
        self.assertEqual(C.role(), "master")
        [node] = C.load_nodes()
        self.assertEqual(node["url"], "https://192.168.1.20:8443")
        self.assertEqual(node["name"], settings.host_name())   # the node's panel name (default: host name)
        self.assertEqual(C.list_joins(), [], "the join code is used up")
        self.assertNotIn("token", C.public_node(node))

    def test_join_code_is_single_use(self):
        self.p.use("master")
        code = C.create_join("alice", "https://192.168.1.10:8443")["code"]
        secret = C.decode_code(code)["s"]
        body = {"secret": secret, "name": "x", "token": C.new_token("rpn_"), "csr": "", "names": [], "ips": [],
                "https_port": 8443, "cluster_api": 1}
        with self.assertRaises(C.ClusterError):      # bad CSR, but the code is spent anyway
            C.accept_join(body, "192.168.1.30")
        with self.assertRaisesRegex(C.ClusterError, "already used or expired"):
            C.accept_join(body, "192.168.1.30")

    def test_shared_addresses_are_left_out(self):
        # Hosts often share an address (Docker's 172.17.0.1): it is dropped, not a reason to refuse
        Panels.IDENT["other"] = {"names": ["other.lan", "node.lan"], "ips": ["192.168.1.30", "172.17.0.1"]}
        Panels.IDENT["node"] = {"names": ["node.lan"], "ips": ["192.168.1.20", "172.17.0.1"]}
        try:
            self.p.use("master")
            C.update_node(C.load_nodes()[0]["id"], sans={"names": ["node.lan"], "ips": ["192.168.1.20", "172.17.0.1"]})
            code = C.create_join("alice", "https://192.168.1.10:8443")["code"]
            self.p.use("other")
            C.join(code, "carol", FakeClient(self.p, node_ip="192.168.1.30"))
            san = subprocess.run(["openssl", "x509", "-in", C.node_crt(), "-noout", "-ext", "subjectAltName"],
                                 capture_output=True, text=True).stdout
            self.assertIn("192.168.1.30", san)
            self.assertIn("other.lan", san)
            self.assertNotIn("172.17.0.1", san)
            self.assertNotIn("node.lan", san)
        finally:
            Panels.IDENT["other"] = {"names": ["other.lan"], "ips": ["192.168.1.30"]}
            Panels.IDENT["node"] = {"names": ["node.lan"], "ips": ["192.168.1.20"]}

    def test_names_already_taken_are_refused(self):
        self.p.use("master")
        code = C.create_join("alice", "https://192.168.1.10:8443")["code"]
        self.p.use("other")
        Panels.IDENT["other"] = {"names": ["other.lan"], "ips": ["192.168.1.30"]}
        with self.assertRaisesRegex(C.ClusterError, "192.168.1.20"):
            C.join(code, "carol", FakeClient(self.p, node_ip="192.168.1.20"))
        self.assertEqual(C.role(), "standalone", "a failed join leaves the panel standalone")

    def test_public_address_is_refused(self):
        self.p.use("master")
        code = C.create_join("alice", "https://192.168.1.10:8443")["code"]
        self.p.use("other")
        with self.assertRaisesRegex(C.ClusterError, "private address"):
            C.join(code, "carol", FakeClient(self.p, node_ip="203.0.113.5"))

    def test_wrong_ca_fingerprint_is_refused(self):
        self.p.use("master")
        code = C.create_join("alice", "https://192.168.1.10:8443")["code"]
        d = C.decode_code(code)
        forged = C.encode_code(d["u"], d["s"], "sha256:" + "0" * 64)
        self.p.use("other")
        with self.assertRaisesRegex(C.ClusterError, "doesn't match"):
            C.join(forged, "carol", FakeClient(self.p, node_ip="192.168.1.30"))

    def test_node_token_and_epoch(self):
        ip = "192.168.1.10"
        who = C.read_node_token(self.token, "1", ip)
        self.assertEqual(who["source"], "cluster")
        self.assertEqual(who["role"], "user")
        self.assertIsNone(C.read_node_token(self.token[:-2] + "xx", "1", ip))
        self.assertIsNone(C.read_node_token(self.token, None, ip), "the epoch header is required")
        self.assertIsNotNone(C.read_node_token(self.token, "2", ip), "a promoted master is accepted")
        self.assertEqual(C.state()["epoch"], 2)
        self.assertIsNone(C.read_node_token(self.token, "1", ip), "and the old master is locked out")

    def test_failed_tokens_are_throttled(self):
        for _ in range(auth.MAX_FAILS):
            C.read_node_token("rpn_" + "x" * 43, "1", "192.168.1.99")
        self.assertIsNone(C.read_node_token(self.token, "1", "192.168.1.99"))
        self.assertIsNotNone(C.read_node_token(self.token, "1", "192.168.1.98"))

    def test_hello(self):
        h = C.hello()
        self.assertEqual(h["cluster_api"], C.CLUSTER_API)
        self.assertEqual(h["cluster_id"], self.node_state["cluster_id"])
        self.assertGreater(h["cert_expires"], 0)

    def test_renewal(self):
        req = C.cert_csr()
        self.p.use("master")
        node = C.load_nodes()[0]
        pem, sans = C.sign_renewal(node, req)
        self.assertEqual(sans["ips"], ["192.168.1.20"])
        self.p.use("node")
        with open(C.node_crt()) as f:
            old = f.read()
        C.install_cert(pem)
        with open(C.node_crt()) as f:
            self.assertNotEqual(f.read(), old)
        subprocess.run(["openssl"] + C.verify_args(C.node_crt()), check=True, capture_output=True)
        with self.assertRaises(C.ClusterError):
            C.install_cert(pem)   # nothing pending any more

    def test_ca_name_constraints_block_public_names(self):
        self.p.use("master")
        with tempfile.TemporaryDirectory() as tmp:
            csr = C.new_key_and_csr(os.path.join(tmp, "k"))
            pem = C.sign_csr(csr, {"example.com"}, set())   # sign_csr trusts its caller; the CA doesn't
            path = os.path.join(tmp, "c.crt")
            with open(path, "w") as f:
                f.write(pem)
            r = subprocess.run(["openssl"] + C.verify_args(path), capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("subtree violation", (r.stdout + r.stderr).lower())

    def test_leave(self):
        C.leave()
        self.assertEqual(C.role(), "standalone")
        self.assertEqual(settings.load()["https_mode"], "self_signed")
        self.assertIsNone(C.read_node_token(self.token, "1", "192.168.1.10"))

    def test_dissolve_needs_no_nodes(self):
        self.p.use("master")
        with self.assertRaisesRegex(C.ClusterError, "Remove the nodes"):
            C.dissolve("alice")
        C.forget_node(C.load_nodes()[0]["id"])
        C.dissolve("alice")
        self.assertEqual(C.role(), "standalone")

    def test_backup_and_promote(self):
        self.p.use("master")
        auth.create_panel_user("masteradmin", "master-password-1", "admin")
        with self.assertRaises(C.ClusterError):
            C.make_backup("short", "alice")
        data = C.make_backup("a long passphrase", "alice")
        self.assertNotIn(b"BEGIN", data, "the backup is encrypted")
        self.p.use("node")
        with self.assertRaisesRegex(C.ClusterError, "Wrong passphrase"):
            C.promote(data, "not the passphrase", "bob")
        self.assertEqual(C.role(), "node", "a failed promotion changes nothing")
        st = C.promote(data, "a long passphrase", "bob")
        self.assertEqual(C.role(), "master")
        self.assertEqual(st["epoch"], 2)
        self.assertEqual(C.read_ca(), self.master_ca, "same CA, so nodes still trust the new master")
        self.assertEqual(C.load_nodes(), [], "this panel was the only node; it isn't its own node")
        self.assertEqual([u["username"] for u in auth.list_panel_users()], ["masteradmin"])
        subprocess.run(["openssl"] + C.verify_args(C.node_crt()), check=True, capture_output=True)

    def test_promote_refuses_other_clusters(self):
        self.p.use("master")
        data = C.make_backup("a long passphrase", "alice")
        self.p.use("other")
        C.create_cluster("Elsewhere", "dave")
        with self.assertRaisesRegex(C.ClusterError, "already a master"):
            C.promote(data, "a long passphrase", "dave")


@unittest.skipUnless(HAVE_FASTAPI, "needs fastapi")
class RouteCoverage(unittest.TestCase):
    """Every API route is either reachable with a node token on purpose, or not at all."""
    EXPECTED = {("GET", "/api/cluster/hello"), ("DELETE", "/api/cluster/membership"),
                ("POST", "/api/cluster/cert/csr"), ("PUT", "/api/cluster/cert"),
                ("GET", "/api/servers"), ("GET", "/api/logs"), ("GET", "/api/templates"), ("GET", "/api/host"),
                ("POST", "/api/servers/{server_name}/action")}

    def test_routes(self):
        import main
        reachable = set()
        for r in main.app.routes:
            path = getattr(r, "path", "")
            if not path.startswith("/api/"):
                continue
            sample = path.replace("{", "").replace("}", "")
            for m in getattr(r, "methods", None) or ():
                if C.path_allowed(m, sample):
                    reachable.add((m, path))
        self.assertEqual(reachable, self.EXPECTED)

    def test_node_only_routes_are_not_public(self):
        import main
        for p in main.NODE_ONLY_API:
            self.assertNotIn(p, main.PUBLIC_API)


if __name__ == "__main__":
    unittest.main()
