"""1.18: SFTP (core/sftp.py) and SSH keys (core/sshkeys.py).
Run: python -m unittest discover -s tests

The server tests start the real SFTP server on a free local port and talk to it with paramiko's
client: panel accounts in a temp data folder, a temp server folder, and files, symlinks and hard
links outside it that a session must never reach. They need paramiko (python3-paramiko).
"""
import base64
import io
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-sftp-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import auth as A, grants as G, manager as M, sftp as S, sshkeys as K, twofactor as T  # noqa: E402
from core import settings as P  # noqa: E402

try:
    import paramiko
    HAVE_PARAMIKO = True
except ImportError:
    HAVE_PARAMIKO = False

ED25519 = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl laptop"


class Keys(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(K, "KEYS_FILE", os.path.join(tempfile.mkdtemp(dir=TMP), "ssh_keys.json"))
        p.start()
        self.addCleanup(p.stop)

    def test_add_list_remove(self):
        k = K.add("panel:alex", ED25519, "")
        self.assertEqual((k["type"], k["label"]), ("ssh-ed25519", "laptop"))
        self.assertTrue(k["fingerprint"].startswith("SHA256:"))
        self.assertEqual(os.stat(K.KEYS_FILE).st_mode & 0o777, 0o600)
        self.assertEqual(len(K.list_keys("panel:alex")), 1)
        self.assertNotIn("blob", K.list_keys("panel:alex")[0])
        blob = base64.b64decode(ED25519.split()[1])
        self.assertTrue(K.matches("panel:alex", "ssh-ed25519", blob))
        self.assertFalse(K.matches("linux:alex", "ssh-ed25519", blob), "a Linux account of the same name is another account")
        self.assertFalse(K.matches("panel:alex", "ssh-ed25519", blob[:-1] + b"x"))
        with self.assertRaises(K.KeyError_):
            K.add("panel:alex", ED25519)                     # twice
        self.assertTrue(K.remove("panel:alex", k["fingerprint"]))
        self.assertFalse(K.remove("panel:alex", k["fingerprint"]))
        self.assertEqual(K.list_keys("panel:alex"), [])

    def test_what_isnt_a_public_key(self):
        short_rsa = "ssh-rsa " + base64.b64encode(
            b"\x00\x00\x00\x07ssh-rsa\x00\x00\x00\x03\x01\x00\x01\x00\x00\x00\x81\x00" + b"\xff" * 128).decode()
        for bad, word in (("", "Paste"), ("-----BEGIN OPENSSH PRIVATE KEY-----", "private key"),
                          ("ssh-dss AAAAB3NzaC1kc3M=", "accepts"), ("ssh-ed25519 not-base64!", "damaged"),
                          ("ssh-ed25519 " + ED25519.split()[1][:-8], "damaged"),
                          ("ssh-rsa " + ED25519.split()[1], "doesn't match"), (short_rsa, "1024 bits"),
                          (ED25519 + "\nssh-ed25519 AAAA", "one line"),
                          ('command="x" ' + ED25519, "accepts")):
            with self.assertRaises(K.KeyError_, msg=bad[:40]) as cm:
                K.parse(bad)
            self.assertIn(word, str(cm.exception))

    def test_ten_keys_at_most(self):
        with mock.patch.object(K, "MAX_KEYS", 1):
            K.add("panel:alex", ED25519)
            other = "ssh-ed25519 " + base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + b"\x01" * 32).decode()
            with self.assertRaises(K.KeyError_):
                K.add("panel:alex", other)


class Names(unittest.TestCase):
    def test_login_names(self):
        self.assertEqual(S.parse_login("alex.minecraft-2"), ("alex", "minecraft-2"))
        self.assertEqual(S.parse_login("alex.minecraft"), ("alex", "minecraft"))
        self.assertEqual(S.parse_login("a.b-c.minecraft-java.1"), ("a.b-c", "minecraft-java.1"))
        self.assertEqual(S.parse_login("x.minecraft.minecraft-2"), ("x.minecraft", "minecraft-2"))
        for bad in ("alex", "alex.survival", ".minecraft", "alex.minecraft/../x", "alex.minecraft 2", "", None,
                    "../x.minecraft"):
            self.assertIsNone(S.parse_login(bad), bad)
        self.assertEqual(S.login_name("alex", "minecraft-2"), "alex.minecraft-2")

    def test_settings(self):
        s = P.validate({"sftp_enabled": True, "sftp_port": "2222"})
        self.assertEqual((s["sftp_enabled"], s["sftp_port"]), (True, 2222))
        self.assertEqual(P.DEFAULTS["sftp_enabled"], False, "off until an admin turns it on")
        for bad in ({"sftp_port": 22}, {"sftp_port": 8080}, {"sftp_port": 0}, {"sftp_port": "x"}, {"sftp_enabled": "yes"}):
            with self.assertRaises(P.SettingsError, msg=bad):
                P.validate(bad)
        with mock.patch.object(S, "available", lambda: False):
            with self.assertRaises(S.SftpError) as cm:
                S.check_can_enable(s, P.DEFAULTS)
            self.assertIn("apt install python3-paramiko", str(cm.exception))
        with mock.patch.object(P, "load", lambda: s):
            self.assertEqual(S.firewall_rules(), {("2222", "tcp")})
        self.assertEqual(S.firewall_rules(), set())


@unittest.skipUnless(HAVE_PARAMIKO and sys.platform.startswith("linux"), "needs paramiko and Linux")
class Server(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(dir=TMP)
        cls.servers = os.path.join(cls.root, "opt")
        cls.patches = [
            mock.patch.object(A, "USERS_FILE", os.path.join(cls.root, "users.json")),
            mock.patch.object(G, "GRANTS_FILE", os.path.join(cls.root, "grants.json")),
            mock.patch.object(K, "KEYS_FILE", os.path.join(cls.root, "ssh_keys.json")),
            mock.patch.object(T, "TOTP_FILE", os.path.join(cls.root, "totp.json")),
            mock.patch.object(S, "HOST_KEY", os.path.join(cls.root, "host_key")),
            mock.patch.object(S, "RECHECK_SECONDS", 0),
            mock.patch.object(S, "MAX_PER_ADDRESS", 1000),
            mock.patch.object(S, "MAX_CONNECTIONS", 1000),
            mock.patch.object(A, "linux_account_exists", lambda u: False),
            mock.patch.object(M, "get_server_dir", lambda s: os.path.join(cls.servers, s)),
            mock.patch.object(M, "_check_server", cls.check_server),
            mock.patch.object(M, "write_log", lambda m: cls.log.append(m)),
        ]
        cls.log = []
        for p in cls.patches:
            p.start()
        G._cache.update(mtime=None, data=None)
        A.create_panel_user("alex", "correct horse", "user")
        A.create_panel_user("sam", "battery staple", "user")
        A.create_panel_user("root.admin", "admin password", "admin")
        got = []
        threading.Thread(target=S.serve, kwargs={"port": 0, "ready": got.append}, daemon=True).start()
        for _ in range(100):
            if got:
                break
            time.sleep(0.05)
        cls.sock = got[0]
        cls.port = cls.sock.getsockname()[1]

    @classmethod
    def tearDownClass(cls):
        cls.sock.close()
        for p in cls.patches:
            p.stop()

    @classmethod
    def check_server(cls, name):
        if not M.SERVICE_NAME_RE.fullmatch(name or "") or not os.path.isdir(os.path.join(cls.servers, name)):
            raise M.ServerError(f"No such server: {name}")

    def setUp(self):
        import shutil
        shutil.rmtree(self.servers, ignore_errors=True)
        self.dir = os.path.join(self.servers, "minecraft-2")
        os.makedirs(os.path.join(self.dir, "plugins"))
        os.makedirs(os.path.join(self.servers, "minecraft-3"))
        self.write(os.path.join(self.dir, "server.properties"), "motd=hi\n")
        self.write(os.path.join(self.servers, "minecraft-3", "secret.txt"), "another server")
        self.outside = os.path.join(self.root, "outside.txt")
        self.write(self.outside, "root only")
        G.set_user("alex", {"minecraft-2": ["files"]})
        G.set_user("sam", {"minecraft-2": ["view", "console"]})
        A._fails.clear()
        self.log.clear()
        self.clients = []

    def tearDown(self):
        for t in self.clients:
            t.close()

    def write(self, path, text):
        with open(path, "w") as f:
            f.write(text)

    def connect(self, username, password=None, pkey=None):
        """A signed-in transport, or None if the sign-in was refused."""
        t = paramiko.Transport(socket.create_connection(("127.0.0.1", self.port), timeout=10))
        self.clients.append(t)
        t.start_client(timeout=10)
        try:
            if pkey is not None:
                t.auth_publickey(username, pkey)
            else:
                t.auth_password(username, password)
        except paramiko.AuthenticationException:
            return None
        return t

    def sftp(self, username="alex.minecraft-2", password="correct horse", pkey=None):
        t = self.connect(username, password, pkey)
        self.assertIsNotNone(t, f"{username} should sign in")
        return paramiko.SFTPClient.from_transport(t)

    def logged(self, text):
        for _ in range(40):                                  # the server writes some lines a moment later
            if any(text in line for line in list(self.log)):
                return True
            time.sleep(0.05)
        return False

    # --- signing in ---
    def test_password_sign_in_sees_only_that_server(self):
        c = self.sftp()
        self.assertEqual(sorted(c.listdir("/")), ["plugins", "server.properties"])
        self.assertEqual(c.normalize("."), "/")
        self.assertEqual(sorted(c.listdir("/../../..")), ["plugins", "server.properties"], "there is nothing above /")
        with self.assertRaises(IOError):
            c.stat("/../minecraft-3/secret.txt")
        with self.assertRaises(IOError):
            c.open("/../../outside.txt")
        self.assertTrue(self.logged("[SFTP] alex signed in to minecraft-2 from 127.0.0.1 (password)."))

    def test_wrong_passwords_are_refused_and_counted(self):
        self.assertIsNone(self.connect("alex.minecraft-2", "wrong"))
        self.assertTrue(self.logged("[SFTP] Failed sign-in for alex from 127.0.0.1"))
        for _ in range(A.MAX_FAILS - 1):
            self.assertIsNone(self.connect("alex.minecraft-2", "wrong"))
        self.assertIsNone(self.connect("alex.minecraft-2", "correct horse"), "the limit is the panel's: 5 in 10 minutes")
        with self.assertRaises(A.AuthError) as cm:
            A.login("alex", "correct horse", "10.9.9.9")
        self.assertIn("Too many failed", str(cm.exception), "and the browser sign-in is limited by it too")

    def test_the_account_needs_files_on_that_server(self):
        self.assertIsNone(self.connect("sam.minecraft-2", "battery staple"), "console isn't files")
        self.assertTrue(self.logged("Refused sam from 127.0.0.1: no Files permission on minecraft-2"))
        self.assertIsNone(self.connect("alex.minecraft-3", "correct horse"), "a server alex has nothing on")
        self.assertIsNone(self.connect("alex.minecraft-9", "correct horse"), "no such server")
        self.assertIsNone(self.connect("alex", "correct horse"), "the name must say which server")
        self.assertIsNone(self.connect("nobody.minecraft-2", "x"))

    def test_admins_reach_every_server(self):
        c = self.sftp("root.admin.minecraft-3", "admin password")
        self.assertEqual(c.listdir("/"), ["secret.txt"])

    def test_disabled_accounts_are_refused(self):
        A.update_panel_user("sam", disabled=True)
        self.addCleanup(A.update_panel_user, "sam", disabled=False)
        G.set_user("sam", {"minecraft-2": ["files"]})
        self.assertIsNone(self.connect("sam.minecraft-2", "battery staple"))

    def test_ssh_keys(self):
        key = paramiko.ECDSAKey.generate(bits=256)
        self.assertIsNone(self.connect("alex.minecraft-2", pkey=key), "a key nobody added")
        K.add("panel:alex", f"{key.get_name()} {key.get_base64()} test")
        c = self.sftp("alex.minecraft-2", pkey=key)
        self.assertIn("server.properties", c.listdir("/"))
        self.assertTrue(self.logged("[SFTP] alex signed in to minecraft-2 from 127.0.0.1 (SSH key)."))
        self.assertIsNone(self.connect("sam.minecraft-2", pkey=key), "alex's key isn't sam's")
        self.assertIsNone(self.connect("alex.minecraft-3", pkey=key), "nor a pass to other servers")
        self.assertIsNone(self.connect("alex.minecraft-2", pkey=paramiko.ECDSAKey.generate(bits=256)))

    def test_two_factor_accounts_must_use_a_key(self):
        with mock.patch.object(T, "is_enabled", lambda key: key == "panel:alex"):
            self.assertIsNone(self.connect("alex.minecraft-2", "correct horse"))
            self.assertTrue(self.logged("two-factor sign-in, so SFTP needs an SSH key"))
            key = paramiko.ECDSAKey.generate(bits=256)
            K.add("panel:alex", f"{key.get_name()} {key.get_base64()}")
            self.assertIn("plugins", self.sftp("alex.minecraft-2", pkey=key).listdir("/"))

    def test_only_sftp_no_shell_or_commands(self):
        t = self.connect("alex.minecraft-2", "correct horse")
        chan = t.open_session(timeout=10)
        with self.assertRaises(paramiko.SSHException):
            chan.exec_command("id")
        chan = t.open_session(timeout=10)
        with self.assertRaises(paramiko.SSHException):
            chan.invoke_shell()
        with self.assertRaises(paramiko.SSHException):
            t.open_channel("direct-tcpip", ("127.0.0.1", 22), ("127.0.0.1", 0), timeout=10)
        with self.assertRaises(paramiko.SSHException):
            t.request_port_forward("127.0.0.1", 0)

    # --- files ---
    def test_upload_download_rename_delete(self):
        c = self.sftp()
        data = os.urandom(300_000)
        c.putfo(io.BytesIO(data), "/plugins/x.jar")
        with open(os.path.join(self.dir, "plugins", "x.jar"), "rb") as f:
            self.assertEqual(f.read(), data)
        self.assertTrue(self.logged("[SFTP] alex uploaded minecraft-2/plugins/x.jar (300000 bytes)."))
        out = io.BytesIO()
        c.getfo("/plugins/x.jar", out)
        self.assertEqual(out.getvalue(), data)
        with c.open("/plugins/x.jar", "r+") as f:           # in place, at an offset (resuming an upload)
            f.seek(299_990)
            f.write(b"0123456789tail")
        self.assertEqual(c.stat("/plugins/x.jar").st_size, 300_004)
        c.rename("/plugins/x.jar", "/y.jar")
        self.assertTrue(os.path.exists(os.path.join(self.dir, "y.jar")))
        self.assertTrue(self.logged("[SFTP] alex renamed minecraft-2/plugins/x.jar to y.jar."))
        with self.assertRaises(IOError):
            c.rename("/y.jar", "/server.properties")          # never over another file
        c.posix_rename("/y.jar", "/server.properties")       # unless the program asks to replace
        c.mkdir("/world")
        self.assertTrue(self.logged("[SFTP] alex created folder minecraft-2/world."))
        c.putfo(io.BytesIO(b"x"), "/world/level.dat")
        with self.assertRaises(IOError):
            c.rmdir("/world")                                # not empty
        c.remove("/world/level.dat")
        c.rmdir("/world")
        self.assertTrue(self.logged("[SFTP] alex deleted minecraft-2/world/level.dat."))
        self.assertTrue(self.logged("[SFTP] alex deleted folder minecraft-2/world."))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "world")))
        with self.assertRaises(IOError):
            c.remove("/plugins")                             # a folder isn't removed as a file
        with self.assertRaises(IOError):
            c.rmdir("/")

    def test_new_files_belong_to_the_folders_owner_and_keep_their_date(self):
        c = self.sftp()
        c.putfo(io.BytesIO(b"hello"), "/new.txt")
        c.utime("/new.txt", (1_700_000_000, 1_700_000_000))
        st = os.stat(os.path.join(self.dir, "new.txt"))
        self.assertEqual((st.st_uid, st.st_gid), (os.stat(self.dir).st_uid, os.stat(self.dir).st_gid))
        self.assertEqual(int(st.st_mtime), 1_700_000_000)
        c.chmod("/new.txt", 0o4777)                           # accepted, ignored: no setuid files
        self.assertEqual(os.stat(os.path.join(self.dir, "new.txt")).st_mode & 0o7777, 0o644)

    def test_symlinks_are_never_followed(self):
        os.symlink(self.outside, os.path.join(self.dir, "link.txt"))
        os.symlink(self.root, os.path.join(self.dir, "up"))
        os.symlink(os.path.join(self.servers, "minecraft-3"), os.path.join(self.dir, "plugins", "other"))
        c = self.sftp()
        import stat as st
        self.assertTrue(st.S_ISLNK(c.stat("/link.txt").st_mode), "shown as a link, not as its target")
        for op in (lambda: c.open("/link.txt"), lambda: c.open("/link.txt", "w"), lambda: c.listdir("/up"),
                   lambda: c.open("/up/outside.txt"), lambda: c.putfo(io.BytesIO(b"x"), "/up/planted.txt"),
                   lambda: c.listdir("/plugins/other"), lambda: c.remove("/plugins/other/secret.txt"),
                   lambda: c.mkdir("/up/dir"), lambda: c.rename("/server.properties", "/up/moved"),
                   lambda: c.readlink("/link.txt"), lambda: c.symlink("/etc/passwd", "/mine")):
            with self.assertRaises(IOError):
                op()
        with open(self.outside) as f:
            self.assertEqual(f.read(), "root only")
        self.assertFalse(os.path.exists(os.path.join(self.root, "planted.txt")))
        self.assertTrue(os.path.exists(os.path.join(self.servers, "minecraft-3", "secret.txt")))
        c.remove("/link.txt")                                # removes the link itself
        self.assertTrue(os.path.exists(self.outside))

    def test_hard_links_are_refused(self):
        os.link(self.outside, os.path.join(self.dir, "hard.txt"))
        c = self.sftp()
        for mode in ("r", "w", "a"):
            with self.assertRaises(IOError):
                c.open("/hard.txt", mode)
        with open(self.outside) as f:
            self.assertEqual(f.read(), "root only", "opening for writing didn't empty it")

    def test_taking_the_permission_away_ends_the_session(self):
        c = self.sftp()
        self.assertIn("plugins", c.listdir("/"))
        G.set_user("alex", {"minecraft-2": ["view"]})
        with self.assertRaises(IOError):
            c.listdir("/")
        with self.assertRaises(IOError):
            c.putfo(io.BytesIO(b"x"), "/late.txt")
        self.assertFalse(os.path.exists(os.path.join(self.dir, "late.txt")))

    def test_the_host_key_is_kept(self):
        with open(S.HOST_KEY + ".pub") as f:
            pub = f.read()
        self.assertEqual(os.stat(S.HOST_KEY).st_mode & 0o777, 0o600)
        S.ensure_host_key()
        with open(S.HOST_KEY + ".pub") as f:
            self.assertEqual(f.read(), pub)
        self.assertTrue(S.host_key_fingerprint().startswith("SHA256:"))


if __name__ == "__main__":
    unittest.main()
