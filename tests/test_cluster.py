"""Tests for core/cluster.py. Run: python -m unittest discover -s tests

The flow tests run a master and a node in one process by pointing the data paths at a
different temporary directory for each panel (see Panels.use).
"""
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="rp-test-cluster-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(_TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(_TMP, "data"))
sys.path.insert(0,os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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
                   ("POST", "/api/cluster/cert/csr"), ("PUT", "/api/cluster/cert"),
                   # phase 2: every tab of a server, creating, removing and restoring servers
                   ("GET", "/api/servers/minecraft-java-1/properties"),
                   ("POST", "/api/servers/minecraft-java-1/whitelist/add"),
                   ("PUT", "/api/servers/minecraft-java-1/files/text"),
                   ("GET", "/api/servers/minecraft-java-1/backups/backup-2026.tar.gz"),
                   ("POST", "/api/servers/minecraft-bedrock-1/phantom/install"),
                   ("DELETE", "/api/servers/minecraft-java-1"),
                   ("GET", "/api/servers/minecraft-java-1/export"),
                   ("POST", "/api/servers/deploy"), ("POST", "/api/servers/import"),
                   ("GET", "/api/servers/removed"), ("POST", "/api/servers/removed/minecraft-old/restore"),
                   ("POST", "/api/cluster/templates"), ("GET", "/api/java/runtimes"),
                   ("POST", "/api/java/runtimes/install"), ("GET", "/api/java/requirement")]
        refused = [("GET", "/api/users"), ("POST", "/api/users"), ("PUT", "/api/settings"),
                   ("GET", "/api/settings/cluster"), ("POST", "/api/settings/cluster/backup"),
                   ("PUT", "/api/settings/notifications"), ("POST", "/api/panel/update"),
                   ("POST", "/api/servers/a/b/action"), ("POST", "/api/servers/redstonepanel/action"),
                   ("POST", "/api/auth/password"), ("GET", "/api/cluster/overview"),
                   ("POST", "/api/cluster/nodes/n-1/servers/x/action"), ("POST", "/api/servers"),
                   ("DELETE", "/api/servers/removed/minecraft-old"), ("PATCH", "/api/servers/minecraft-java-1"),
                   ("GET", "/api/servers/"), ("GET", "/api/serversX"), ("PUT", "/api/templates/abc"),
                   ("GET", "/api/servers/minecraft-a/../../users"), ("GET", "/api/servers/minecraft-a//x"),
                   ("GET", "/api/servers/minecraft-a/files/..")]
        for m, p in allowed:
            self.assertTrue(C.path_allowed(m, p), f"{m} {p}")
        for m, p in refused:
            self.assertFalse(C.path_allowed(m, p), f"{m} {p}")

    def test_proxy_passes_on_only_what_users_may_reach(self):
        self.assertTrue(C.proxy_allowed("GET", "/api/servers/minecraft-a/chat"))
        self.assertTrue(C.proxy_allowed("POST", "/api/servers/deploy"))
        for m, p in (("DELETE", "/api/cluster/membership"), ("PUT", "/api/cluster/cert"),
                     ("POST", "/api/cluster/cert/csr"), ("POST", "/api/cluster/templates"),
                     ("GET", "/api/cluster/hello"), ("GET", "/api/users"), ("GET", "/api/settings")):
            self.assertFalse(C.proxy_allowed(m, p), f"{m} {p}")

    def test_master_identity(self):
        base = {"username": "master:Main", "source": "cluster", "role": "user", "master": "Main"}
        alex = C.master_identity(base, "alex", "user")
        self.assertEqual((alex["username"], alex["role"], alex["source"]), ("alex@Main", "user", "cluster"))
        self.assertEqual(C.master_identity(base, "root", "admin")["role"], "admin")
        self.assertEqual(C.master_identity(base, "x", "superuser")["role"], "user", "only 'admin' is an admin")
        self.assertEqual(C.master_identity(base, None, None)["username"], "?@Main")
        self.assertEqual(C.master_identity(base, "a@b\nc", "user")["username"], "abc@Main",
                         "no line breaks or extra @ from a header end up in the activity log")

    def test_templates_from_the_master(self):
        t = {"id": "1f0c-aa", "name": " Survival ", "edition": "Java",
             "properties": {"difficulty": "hard", "pvp": False, "max-players": 20}, "permissions": "[]"}
        clean = C.clean_template(t, "Main")
        self.assertEqual(clean["name"], "Survival")
        self.assertEqual(clean["from_master"], "Main")
        for bad in ({**t, "id": "../x"}, {**t, "edition": "Forge"}, {**t, "properties": {"a\nb": "1"}},
                    {**t, "properties": {"motd": "x\nrcon.password=1"}}, {**t, "properties": []},
                    {**t, "name": ""}, {**t, "permissions": 5}, "nope"):
            with self.assertRaises(C.ClusterError, msg=str(bad)):
                C.clean_template(bad, "Main")

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
        self.assertEqual(h["machine"], platform.machine())
        self.p.use("master")
        self.assertEqual(C.public_node(C.load_nodes()[0])["machine"], platform.machine(), "the join records it")
        self.p.use("node")
        self.assertEqual(C.clean_machine("aarch64"), "aarch64")
        self.assertIsNone(C.clean_machine("x86_64; rm -rf /"))

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


def own_certificate(directory, name):
    """A certificate and key for a public name, standing in for a certbot certificate."""
    cert, key = os.path.join(directory, "own.crt"), os.path.join(directory, "own.key")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                    "-nodes", "-keyout", key, "-out", cert, "-days", "90", "-subj", f"/CN={name}",
                    "-addext", f"subjectAltName=DNS:{name}"], check=True, capture_output=True)
    return cert, key


def use_own_certificate(directory, name):
    cert, key = own_certificate(directory, name)
    settings.save(dict(settings.load(), https_mode="custom", cert_file=cert, key_file=key))


@unittest.skipUnless(HAVE_OPENSSL, "needs the openssl command")
class Phase2Certificates(unittest.TestCase):
    """1.15: a master or node with its own (certbot) certificate, nightly backups."""
    def setUp(self):
        self.p = Panels()

    def tearDown(self):
        self.p.close()

    def test_master_keeps_its_own_certificate(self):
        self.p.use("master")
        use_own_certificate(os.path.join(self.p.root, "master"), "panel.example.com")
        C.create_cluster("Home", "alice")
        self.assertEqual(C.role(), "master")
        self.assertEqual(settings.load()["https_mode"], "custom", "browsers keep seeing the real certificate")
        self.assertTrue(os.path.exists(C.ca_crt()))
        # join codes point at the name the certificate is for, whatever address the browser used
        url = C.default_join_url("10.0.0.5:8443")
        self.assertTrue(url.startswith("https://panel.example.com"), url)
        # and a node can still join (it checks the master with the system CAs plus the cluster CA)
        code = C.create_join("alice", "https://192.168.1.10:8443")["code"]
        self.p.use("node")
        C.join(code, "bob", FakeClient(self.p))
        self.assertEqual(C.role(), "node")
        self.p.use("master")
        C.forget_node(C.load_nodes()[0]["id"])
        C.dissolve("alice")
        self.assertEqual(settings.load()["https_mode"], "custom", "dissolving keeps the panel's own certificate")

    def test_node_with_its_own_certificate_is_checked_with_the_system_cas(self):
        self.p.use("master")
        C.create_cluster("Home", "alice")
        code = C.create_join("alice", "https://192.168.1.10:8443")["code"]
        self.p.use("other")
        use_own_certificate(os.path.join(self.p.root, "other"), "mc.example.com")
        # a public address is fine for such a node: the master reaches it by its public name
        C.join(code, "carol", FakeClient(self.p, node_ip="203.0.113.5"))
        self.assertEqual(C.role(), "node")
        self.assertEqual(settings.load()["https_mode"], "custom")
        self.assertFalse(os.path.exists(C.node_crt()), "no cluster certificate needed")
        self.assertGreater(C.hello()["cert_expires"], 0, "hello reports the panel's own certificate")
        self.p.use("master")
        [node] = C.load_nodes()
        self.assertEqual(node["url"], "https://mc.example.com:8443")
        self.assertEqual(C.node_verify(node), "system")
        self.assertEqual(C.public_node(node)["verify"], "system")
        with self.assertRaisesRegex(C.ClusterError, "its own certificate"):
            C.sign_renewal(node, {"csr": "", "names": [], "ips": []})
        C.update_node(node["id"], verify="cluster")
        self.assertEqual(C.node_verify(C.find_node(node["id"])), "cluster")
        with self.assertRaises(C.ClusterError):
            C.update_node(node["id"], verify="none")

    def test_system_node_names(self):
        for bad in ("https://203.0.113.5:8443", "https://mc:8443", "http://mc.example.com:8443",
                    "https://mc.example.com:9999", "https://mc.example.com"):
            with self.assertRaises(C.ClusterError, msg=bad):
                C._system_node_url(bad, 8443, [])
        self.assertEqual(C._system_node_url("https://MC.example.com:8443", 8443, []), "https://mc.example.com:8443")
        with self.assertRaisesRegex(C.ClusterError, "already"):
            C._system_node_url("https://mc.example.com:8443", 8443, [{"url": "https://mc.example.com:8443"}])

    def test_nightly_backups(self):
        import time
        self.p.use("master")
        C.create_cluster("Home", "alice")
        self.assertFalse(C.nightly_view()["enabled"])
        self.assertFalse(C.nightly_due())
        with self.assertRaisesRegex(C.ClusterError, "passphrase"):
            C.set_nightly(True, None, "alice")
        with self.assertRaisesRegex(C.ClusterError, "at least"):
            C.set_nightly(True, "short", "alice")
        self.assertTrue(C.set_nightly(True, "a long nightly passphrase", "alice")["enabled"])
        with open(os.path.join(C.CLUSTER_DIR, "nightly.json")) as f:
            self.assertIn("a long nightly passphrase", f.read())
        self.assertEqual(os.stat(os.path.join(C.CLUSTER_DIR, "nightly.json")).st_mode & 0o777, 0o600)

        day = time.mktime((2026, 11, 1, 4, 0, 0, 0, 0, -1))
        self.assertFalse(C.nightly_due(day - 2 * 3600), "not before 3:00")
        for i in range(C.NIGHTLY_KEEP + 2):
            t = day + i * 86400
            self.assertTrue(C.nightly_due(t))
            C.run_nightly(t)
            self.assertFalse(C.nightly_due(t + 3600), "once a day")
        view = C.nightly_view()
        self.assertEqual(len(view["files"]), C.NIGHTLY_KEEP)
        self.assertEqual(view["files"][0]["file"], "redstonepanel-cluster-20261109.rpbackup")
        path = C.nightly_file(view["files"][0]["file"])
        with open(path, "rb") as f:
            manifest, files = C.read_backup(f.read(), "a long nightly passphrase")
        self.assertEqual(manifest["cluster"], "Home")
        for bad in ("../cluster.json", "nightly.json", "redstonepanel-cluster-20200101.rpbackup"):
            with self.assertRaises(C.ClusterError):
                C.nightly_file(bad)
        # turning it off forgets the passphrase
        C.set_nightly(False, None, "alice")
        with open(os.path.join(C.CLUSTER_DIR, "nightly.json")) as f:
            self.assertNotIn("passphrase", f.read())
        self.assertFalse(C.nightly_due(day + 30 * 86400))


@unittest.skipUnless(HAVE_FASTAPI, "needs fastapi")
class RouteCoverage(unittest.TestCase):
    """Every API route is either reachable with a node token on purpose, or not at all.

    Phase 2 (1.15) gives the master every route of a single server (/api/servers/{server_name}/...),
    so each server tab works from the master. Anything else must be listed here on purpose."""
    EXPECTED = {("GET", "/api/cluster/hello"), ("DELETE", "/api/cluster/membership"),
                ("POST", "/api/cluster/cert/csr"), ("PUT", "/api/cluster/cert"),
                ("GET", "/api/servers"), ("GET", "/api/logs"), ("GET", "/api/templates"), ("GET", "/api/host"),
                ("POST", "/api/servers/deploy"), ("POST", "/api/servers/import"),
                ("GET", "/api/servers/removed"), ("POST", "/api/servers/removed/{server_name}/restore"),
                ("POST", "/api/cluster/templates"),
                ("GET", "/api/java/versions"), ("GET", "/api/java/requirement"), ("GET", "/api/java/runtimes"),
                ("GET", "/api/java/runtimes/job"), ("POST", "/api/java/runtimes/install"),
                ("POST", "/api/java/runtimes/remove"),
                # 1.16: moving servers (cluster plumbing, see core/transfer.py)
                ("POST", "/api/cluster/transfers/export"), ("POST", "/api/cluster/transfers/import"),
                ("GET", "/api/cluster/transfers/{tid}/archive"), ("PUT", "/api/cluster/transfers/{tid}/archive"),
                ("POST", "/api/cluster/transfers/{tid}/finish"), ("POST", "/api/cluster/transfers/{tid}/install"),
                ("POST", "/api/cluster/transfers/{tid}/confirm"), ("POST", "/api/cluster/transfers/{tid}/stage"),
                ("DELETE", "/api/cluster/transfers/{tid}"),
                ("DELETE", "/api/cluster/transfers/moved/{server_name}"),
                # 1.16.8: backup copies kept for other members (cluster plumbing, core/backup_copies.py)
                ("GET", "/api/cluster/held-backups"),
                ("GET", "/api/cluster/held-backups/{panel}/{server_name}/{filename}"),
                ("GET", "/api/cluster/held-backups/{panel}/{server_name}/{filename}/sha256"),
                ("PUT", "/api/cluster/held-backups/{panel}/{server_name}/{filename}"),
                ("DELETE", "/api/cluster/held-backups/{panel}/{server_name}/{filename}"),
                ("PUT", "/api/cluster/held-backups/into/{server_name}/{filename}"),
                # 1.16.14: a node's own port forwarding rows (read-only, admins)
                ("GET", "/api/port-forwarding/local")}
    NEVER = ("/api/users", "/api/settings", "/api/auth", "/api/panel", "/api/cluster/overview",
             "/api/cluster/nodes", "/api/cluster/join", "/api/templates/", "/api/cluster/backup-copies",
             "/api/port-forwarding/settings", "/api/port-forwarding/lookup", "/api/port-forwarding/check")

    @staticmethod
    def _sample(path):
        return (path.replace("{server_name}", "minecraft-x").replace("{path:path}", "x").replace("{tid}", "a" * 32)
                .replace("{panel}", "n-abc123").replace("{", "").replace("}", ""))

    def _reachable(self):
        import main
        out = set()
        for r in main.app.routes:
            path = getattr(r, "path", "")
            if path.startswith("/api/"):
                for m in getattr(r, "methods", None) or ():
                    if C.path_allowed(m, self._sample(path)):
                        out.add((m, path))
        return out

    def test_routes(self):
        server_routes = {(m, p) for m, p in self._reachable() if p.startswith("/api/servers/{server_name}")}
        self.assertGreater(len(server_routes), 50, "every tab of a server")
        self.assertIn(("DELETE", "/api/servers/{server_name}"), server_routes)
        self.assertEqual(self._reachable() - server_routes, self.EXPECTED)

    def test_never_reachable(self):
        for m, p in self._reachable():
            self.assertFalse(p.startswith(self.NEVER), f"{m} {p}")
        self.assertNotIn(("DELETE", "/api/servers/removed/{server_name}"), self._reachable())

    def test_node_only_routes_are_not_public(self):
        import main
        for p in main.NODE_ONLY_API:
            self.assertNotIn(p, main.PUBLIC_API)


if __name__ == "__main__":
    unittest.main()
