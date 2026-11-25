"""1.24: mixed Linux and Windows clusters, and SFTP on Windows.

What runs anywhere: file names Windows can't hold (core/winnames.py) and where they're checked
(moves, pre-copies, archives, restores), members reporting their system, join commands for both
systems, the Windows certificate code (core/platform/windows/certs.py) against the real openssl
command (a cluster CA, node certificates and encrypted cluster backups made on one system work on
the other), a moved Bedrock server getting this system's program, a renewed certificate being
picked up, and the SFTP service's Windows parts.
Run: python -m unittest discover -s tests"""
import io
import os
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import types
import unittest
import zipfile
from contextlib import nullcontext
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-mixed-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
os.environ.setdefault("RP_BACKUP_DIR", os.path.join(TMP, "backups"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import archive, backups, cluster, manager, moves, tls, transfer, updates, winnames  # noqa: E402
from core.platform import paths  # noqa: E402

try:
    import cryptography  # noqa: F401
    from core.platform.windows import certs as wincerts
    HAVE_CRYPTO = True
except ImportError:
    HAVE_CRYPTO = False
HAVE_OPENSSL = shutil.which("openssl") is not None


def _tar(path, names):
    with tarfile.open(path, "w:gz") as t:
        for n in names:
            info = tarfile.TarInfo(n)
            data = b"x"
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    return path


class WindowsNames(unittest.TestCase):
    def test_names_windows_refuses_or_reads_differently(self):
        found = dict(winnames.problems([
            "world/level.dat", "world/region/r.0.0.mca",          # fine
            "plugins/CON", "plugins/nul.txt", "logs/COM1.log",     # devices
            "notes:old.txt", "a|b", "what?",                       # forbidden characters
            "dots.", "space ",                                     # dropped endings
            "Config/a.yml", "config/a.yml",                        # only the case differs
            "bad:dir/x/y/z.txt",                                   # reported once, at the bad folder
        ]))
        self.assertEqual(set(found), {"plugins/CON", "plugins/nul.txt", "logs/COM1.log", "notes:old.txt", "a|b",
                                      "what?", "dots.", "space ", "config", "bad:dir"})
        self.assertIn("same name as Config", found["config"])
        self.assertIn("device name", found["plugins/nul.txt"])
        self.assertIn("end with a dot", found["dots."])

    def test_a_clean_folder_has_none(self):
        self.assertEqual(winnames.problems(["world", "world/level.dat", "plugins/LuckPerms/config.yml",
                                            "server.properties", "CONFIG.yml.bak", "console.log"]), [])

    def test_the_message_lists_the_first_ones_and_counts_the_rest(self):
        found = [(f"f{i}:x", "colon") for i in range(winnames.SHOWN + 5)]
        msg = winnames.describe(found)
        self.assertIn(f"{winnames.SHOWN + 5} file names", msg)
        self.assertIn("f0:x", msg)
        self.assertNotIn(f"f{winnames.SHOWN}:x", msg)
        self.assertIn("and 5 more", msg)
        self.assertIn("Rename or remove them first", msg)

    def test_folder_problems_walks_a_real_folder(self):
        root = tempfile.mkdtemp(dir=TMP)
        os.makedirs(os.path.join(root, "world"))
        for n in ("world/level.dat", "Readme.txt", "README.txt"):
            open(os.path.join(root, n), "w").close()
        self.assertEqual([p for p, _ in winnames.folder_problems(root)], ["Readme.txt"])

    def test_the_windows_file_manager_uses_the_same_rules(self):
        from core.platform.windows import dirfd
        self.assertIs(dirfd.name_problem, winnames.name_problem)


class Archives(unittest.TestCase):
    """A Windows panel checks an archive before unpacking it; Linux never does."""

    def test_an_archive_with_bad_names_is_refused_on_windows_only(self):
        path = _tar(os.path.join(tempfile.mkdtemp(dir=TMP), "s.tar.gz"),
                    ["server.properties", "world/level.dat", "plugins/aux.yml", "World/level.dat"])
        with self.assertRaises(archive.ArchiveError) as e:
            archive.check_windows_names(path, windows=True)
        self.assertIn("plugins/aux.yml", str(e.exception))
        self.assertIn("World", str(e.exception))
        archive.check_windows_names(path, windows=False)

    def test_unpack_on_windows_refuses_before_writing_anything(self):
        parent = tempfile.mkdtemp(dir=TMP)
        path = _tar(os.path.join(parent, "s.tar.gz"), ["ok.txt", "bad:name.txt"])
        with mock.patch.object(archive.paths, "WINDOWS", True):
            with self.assertRaises(archive.ArchiveError):
                archive.unpack(path, parent)
        self.assertEqual(sorted(os.listdir(parent)), ["s.tar.gz"])

    def test_zip_entries_and_junk_folders(self):
        path = os.path.join(tempfile.mkdtemp(dir=TMP), "s.zip")
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("__MACOSX/._x", b"")
            z.writestr("server/world/level.dat", b"")
        self.assertEqual(archive.windows_problems(path), [])

    def test_a_precopys_changes_are_checked_on_windows(self):
        path = os.path.join(tempfile.mkdtemp(dir=TMP), "delta.tar.gz")
        with tarfile.open(path, "w:gz") as t:
            info = tarfile.TarInfo(transfer.DELTA_LIST)
            data = b'{"deleted": []}'
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
            info = tarfile.TarInfo("logs/x:y.log")
            t.addfile(info, io.BytesIO(b""))
        root = tempfile.mkdtemp(dir=TMP)
        with mock.patch.object(transfer.paths, "WINDOWS", True):
            with self.assertRaises(transfer.TransferError) as e:
                transfer.apply_changes(path, root)
        self.assertIn("logs/x:y.log", str(e.exception))
        self.assertEqual(os.listdir(root), [])


class Restore(unittest.TestCase):
    def test_restoring_a_backup_with_bad_names_on_windows_changes_nothing(self):
        d = tempfile.mkdtemp(dir=TMP)
        path = _tar(os.path.join(d, "b.tar.gz"), ["server.properties", "world/level.dat", "world/data/raids:end.dat"])
        with mock.patch.object(backups, "backup_path", lambda s, f: path), \
                mock.patch.object(backups.manager, "get_server_dir", lambda s: d), \
                mock.patch.object(backups, "_edition", lambda s, d: "java"), \
                mock.patch.object(backups, "_is_active", side_effect=AssertionError("checked before stopping")), \
                mock.patch.object(backups.paths, "WINDOWS", True):
            with self.assertRaises(backups.BackupError) as e:
                backups.restore_backup("minecraft-java", "b.tar.gz")
        self.assertIn("world/data/raids:end.dat", str(e.exception))


class Export(unittest.TestCase):
    """The source of a move to Windows refuses before it stops the server."""

    def setUp(self):
        self.d = tempfile.mkdtemp(dir=TMP)
        os.makedirs(os.path.join(self.d, "world"))
        for n in ("server.jar", "world/level.dat", "logs.txt"):
            open(os.path.join(self.d, n), "w").close()
        for p in (mock.patch.object(transfer.manager, "_check_server", lambda n: None),
                  mock.patch.object(transfer.manager, "get_server_dir", lambda n: self.d),
                  mock.patch.object(transfer.services, "stop", side_effect=AssertionError("stopped"))):
            p.start()
            self.addCleanup(p.stop)

    def test_names_are_listed_and_nothing_is_stopped(self):
        open(os.path.join(self.d, "world", "Level.dat"), "w").close()
        with self.assertRaises(transfer.TransferError) as e:
            transfer.export("minecraft-java", True, for_windows=True)
        self.assertIn("can't go to a Windows computer", str(e.exception))
        self.assertIn("world/level.dat", str(e.exception))

    def test_a_clean_folder_passes_the_check(self):
        self.assertEqual(transfer.windows_problems("minecraft-java"), [])

    def test_the_route_passes_the_flag(self):
        try:
            import main
        except ImportError:
            self.skipTest("needs fastapi")
        self.assertIn("for_windows", main.TransferExport.model_fields)


class Members(unittest.TestCase):
    def test_this_system_and_cleaning_what_members_report(self):
        self.assertEqual(cluster.this_system(), "windows" if paths.WINDOWS else "linux")
        self.assertEqual(cluster.clean_system("Windows"), "windows")
        for other in (None, "", "linux", "freebsd", 7):
            self.assertEqual(cluster.clean_system(other), "linux")
        self.assertEqual(moves.system_of({"platform": "windows"}), "windows")
        self.assertEqual(moves.system_of({}), "linux")

    def test_join_and_leave_commands_for_both_systems(self):
        c = cluster.join_commands("rpjoin1:abc")
        self.assertEqual(c["linux"], "sudo redstonepanel join 'rpjoin1:abc'")
        self.assertEqual(c["windows"], "redstonepanel join rpjoin1:abc")
        self.assertEqual(cluster.leave_command("windows"), "redstonepanel leave")
        self.assertEqual(cluster.leave_command("linux"), "sudo redstonepanel leave")

    def test_the_contract_says_windows_members(self):
        self.assertGreaterEqual(cluster.CLUSTER_API, 9)
        self.assertEqual(cluster.MIXED_API, 9)

    def test_hello_reports_the_system(self):
        with mock.patch.object(cluster, "state", lambda: {"role": "node", "cluster_id": "c"}), \
                mock.patch.object(cluster, "cert_expires", lambda p: None), \
                mock.patch.object(cluster.paths, "WINDOWS", True):
            h = cluster.hello()
        self.assertEqual(h["platform"], "windows")


class RemoteEnd(unittest.TestCase):
    """The master tells a node when the target of a move is Windows (the jobs: tests/test_move.py)."""

    def test_the_remote_end_sends_the_flag_only_when_needed(self):
        node = {"id": "n1", "name": "Gaming PC", "platform": "windows", "url": "https://10.0.0.5:8443"}
        r = moves.Remote(node, "alex", "admin")
        self.assertEqual(r.system, "windows")
        sent = []
        with mock.patch.object(moves.cluster_client, "call_node", lambda n, m, p, body, **kw: sent.append(body) or {}):
            r.export("minecraft-java", True, for_windows=True)
            r.export("minecraft-java", True)
        self.assertTrue(sent[0]["for_windows"])
        self.assertNotIn("for_windows", sent[1])


class BedrockAcross(unittest.TestCase):
    """A Bedrock folder moved from the other system gets this system's program."""

    def test_the_other_systems_program_is_removed(self):
        d = tempfile.mkdtemp(dir=TMP)
        mine, other = (("bedrock_server.exe", "bedrock_server.pdb"), ("bedrock_server", "bedrock_server_symbols.debug")) \
            if paths.WINDOWS else (("bedrock_server", "bedrock_server_symbols.debug"), ("bedrock_server.exe", "bedrock_server.pdb"))
        for n in mine + other + ("server.properties",):
            open(os.path.join(d, n), "w").close()
        os.makedirs(os.path.join(d, "worlds"))
        with mock.patch.object(manager.system, "game_files", nullcontext), \
                mock.patch.object(manager, "write_log", lambda m: None), \
                mock.patch.object(manager.system, "make_executable", lambda p: None), \
                mock.patch.object(manager, "_fetch_bedrock", side_effect=AssertionError("has a program")):
            manager._bedrock_program_for_this_system(d)
        self.assertEqual(sorted(os.listdir(d)), sorted(mine + ("server.properties", "worlds")))

    def test_a_folder_without_this_systems_program_gets_the_version_it_ran(self):
        d = tempfile.mkdtemp(dir=TMP)
        asked = []

        def fetch(folder, prefer=None):
            asked.append(prefer)
            open(os.path.join(folder, paths.BEDROCK_PROGRAM), "w").close()
        with mock.patch.object(manager.system, "game_files", nullcontext), \
                mock.patch.object(manager, "write_log", lambda m: None), \
                mock.patch.object(manager.system, "make_executable", lambda p: None), \
                mock.patch.object(updates, "bedrock_installed", lambda d: "1.26.60.2"), \
                mock.patch.object(manager, "_fetch_bedrock", fetch):
            manager._bedrock_program_for_this_system(d)
        self.assertEqual(asked, ["1.26.60.2"])

    def test_same_version_url(self):
        latest = {"version": "1.26.66.1",
                  "url": "https://www.minecraft.net/bedrockdedicatedserver/bin-win/bedrock-server-1.26.66.1.zip"}
        self.assertEqual(updates.same_version(latest, "1.26.60.2"),
                         {"version": "1.26.60.2",
                          "url": "https://www.minecraft.net/bedrockdedicatedserver/bin-win/bedrock-server-1.26.60.2.zip"})
        for v in (None, "", "1.26.66.1", "1.27.0.1", "1.2.3; rm", "../x"):
            self.assertIsNone(updates.same_version(latest, v), v)

    def test_fetch_prefers_that_version_and_falls_back_to_the_newest(self):
        d = tempfile.mkdtemp(dir=TMP)
        latest = {"version": "1.26.66.1",
                  "url": "https://www.minecraft.net/bedrockdedicatedserver/bin-linux/bedrock-server-1.26.66.1.zip"}
        installed = []
        for available, want in ((True, "1.26.60.2"), (False, "1.26.66.1")):
            def download(rel, staging):
                if rel["version"] != latest["version"] and not available:
                    raise updates.UpdateError("HTTP 404")
                return rel["version"]
            with mock.patch.object(updates, "bedrock_latest", lambda: latest), \
                    mock.patch.object(updates, "download_bedrock", download), \
                    mock.patch.object(updates, "install_files", lambda folder, z: installed.append(z)), \
                    mock.patch.object(updates, "_write_version", lambda folder, v: None), \
                    mock.patch.object(updates.manager, "game_temp_dir", lambda folder, p: tempfile.mkdtemp(dir=TMP)), \
                    mock.patch.object(updates.manager, "remove_game_tree", lambda p: None), \
                    mock.patch.object(updates.manager, "write_log", lambda m: None):
                self.assertEqual(updates.fetch_bedrock(d, prefer="1.26.60.2"), want)
        self.assertEqual(installed, ["1.26.60.2", "1.26.66.1"])


@unittest.skipUnless(HAVE_CRYPTO and HAVE_OPENSSL, "needs the cryptography library and the openssl command")
class WindowsCertificates(unittest.TestCase):
    """The Windows side does the panel's openssl commands with the cryptography library; what it
    makes must be what the real openssl makes and accepts, or Linux and Windows members can't
    trust each other."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(dir=TMP)
        self.env = dict(os.environ, RP_BACKUP_PASS="correct horse battery staple")
        self.enc = ["enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-salt", "-pass", "env:RP_BACKUP_PASS"]

    def openssl(self, args, data=None):
        return subprocess.run(["openssl"] + args, input=data, env=self.env, capture_output=True)

    def p(self, name):
        return os.path.join(self.dir, name)

    def test_encrypted_backups_open_on_the_other_system(self):
        data = os.urandom(70000)
        made_here = wincerts.openssl(self.enc, data, env=self.env)
        self.assertEqual(made_here.returncode, 0, made_here.stderr)
        self.assertTrue(made_here.stdout.startswith(b"Salted__"))
        back = self.openssl(self.enc + ["-d"], made_here.stdout)
        self.assertEqual((back.returncode, back.stdout), (0, data))
        made_there = self.openssl(self.enc, data)
        back = wincerts.openssl(self.enc + ["-d"], made_there.stdout, env=self.env)
        self.assertEqual((back.returncode, back.stdout), (0, data))
        wrong = wincerts.openssl(self.enc + ["-d"], made_there.stdout, env=dict(self.env, RP_BACKUP_PASS="nope"))
        self.assertNotEqual(wrong.returncode, 0)
        self.assertNotEqual(wincerts.openssl(self.enc + ["-d"], b"not a backup", env=self.env).returncode, 0)

    def test_a_cluster_made_on_windows_is_trusted_by_openssl(self):
        """The master's CA (with its name constraints) and a node's certificate, made the Windows
        way; openssl and Python's ssl module (what Linux members use) accept them."""
        with mock.patch.object(cluster, "CLUSTER_DIR", self.dir), mock.patch.object(cluster, "certs", wincerts):
            cluster._create_ca("Home")
            csr = cluster.new_key_and_csr(self.p("node.key"))
            pem = cluster.sign_csr(csr, {"node.local"}, {"10.1.2.3"})
            with open(self.p("node.crt"), "w") as f:
                f.write(pem)
            cluster._check_cert(self.p("node.crt"), self.p("node.key"))
            self.assertEqual(cluster.custom_cert_names(self.p("node.crt")), ["node.local"])
            self.assertIsNotNone(cluster.cert_expires(self.p("node.crt")))
        ok = self.openssl(["verify", "-CAfile", self.p("ca.crt"), self.p("node.crt")])
        self.assertEqual(ok.returncode, 0, ok.stderr)
        text = self.openssl(["x509", "-in", self.p("ca.crt"), "-noout", "-text"]).stdout.decode()
        self.assertIn("Name Constraints: critical", text)
        self.assertIn("Permitted", text)
        ctx = ssl.create_default_context(cafile=self.p("ca.crt"))
        ctx.load_cert_chain(self.p("node.crt"), self.p("node.key"))     # the pair loads

    def test_a_certificate_signed_by_openssl_is_checked_on_windows(self):
        """A Linux master signs a Windows node's request; the Windows side checks the result."""
        self.openssl(["ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", self.p("ca.key")])
        self.openssl(["req", "-x509", "-new", "-key", self.p("ca.key"), "-sha256", "-days", "30", "-subj", "/CN=CA",
                      "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0", "-out", self.p("ca.crt")])
        wincerts.openssl(["ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", self.p("n.key")])
        csr = wincerts.openssl(["req", "-new", "-key", self.p("n.key"), "-subj", "/CN=RedstonePanel node"]).stdout
        with open(self.p("n.csr"), "wb") as f:
            f.write(csr)
        self.assertEqual(self.openssl(["req", "-verify", "-noout", "-in", self.p("n.csr")]).returncode, 0)
        self.assertEqual(wincerts.openssl(["req", "-verify", "-noout", "-in", self.p("n.csr")]).returncode, 0)
        with open(self.p("ext"), "w") as f:
            f.write("basicConstraints=critical,CA:FALSE\nsubjectAltName=DNS:pc.lan,IP:192.168.1.9\n")
        r = self.openssl(["x509", "-req", "-in", self.p("n.csr"), "-CA", self.p("ca.crt"), "-CAkey", self.p("ca.key"),
                          "-set_serial", "0x1234", "-days", "5", "-sha256", "-extfile", self.p("ext"),
                          "-out", self.p("n.crt")])
        self.assertEqual(r.returncode, 0, r.stderr)
        now = str(int(time.time()))
        self.assertEqual(wincerts.openssl(["verify", "-attime", now, "-CAfile", self.p("ca.crt"), self.p("n.crt")])
                         .returncode, 0)
        later = str(int(time.time()) + 10 * 86400)
        self.assertNotEqual(wincerts.openssl(["verify", "-attime", later, "-CAfile", self.p("ca.crt"),
                                              self.p("n.crt")]).returncode, 0)
        earlier = str(int(time.time()) - 86400)
        self.assertNotEqual(wincerts.openssl(["verify", "-attime", earlier, "-CAfile", self.p("ca.crt"),
                                              self.p("n.crt")]).returncode, 0)
        san = wincerts.openssl(["x509", "-in", self.p("n.crt"), "-noout", "-ext", "subjectAltName"]).stdout.decode()
        self.assertEqual(san, self.openssl(["x509", "-in", self.p("n.crt"), "-noout", "-ext",
                                            "subjectAltName"]).stdout.decode())

    def test_a_tampered_request_is_refused(self):
        wincerts.openssl(["ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", self.p("n.key")])
        csr = wincerts.openssl(["req", "-new", "-key", self.p("n.key"), "-subj", "/CN=RedstonePanel node"]).stdout
        der = ssl.PEM_cert_to_DER_cert(csr.decode().replace("CERTIFICATE REQUEST", "CERTIFICATE"))
        bad = bytearray(der)
        bad[-5] ^= 0xFF                                   # inside the signature
        pem = ssl.DER_cert_to_PEM_cert(bytes(bad)).replace("CERTIFICATE", "CERTIFICATE REQUEST")
        with open(self.p("bad.csr"), "w") as f:
            f.write(pem)
        self.assertNotEqual(wincerts.openssl(["req", "-verify", "-noout", "-in", self.p("bad.csr")]).returncode, 0)

    def test_cluster_backups_made_on_one_system_promote_on_the_other(self):
        st = {"role": "master", "cluster_id": "c1", "name": "Home", "epoch": 3}
        files = {}
        for arc in cluster.BACKUP_FILES:
            files[arc] = self.p(arc.replace("/", "_"))
            with open(files[arc], "wb") as f:
                f.write(b'{"role": "master", "cluster_id": "c1", "name": "Home", "epoch": 3}'
                        if arc == "cluster/cluster.json" else b'{"nodes": []}' if arc == "cluster/nodes.json"
                        else arc.encode())
        sources = {arc: (lambda p=p: p) for arc, p in files.items()}
        linux = types.SimpleNamespace(openssl=lambda args, input=None, env=None:
                                      subprocess.run(["openssl"] + args, input=input, env=env, capture_output=True))
        for made_by, read_by in ((linux, wincerts), (wincerts, linux)):
            with mock.patch.object(cluster, "state", lambda: st), \
                    mock.patch.object(cluster, "BACKUP_FILES", sources), \
                    mock.patch.object(cluster.panel_settings, "display_name", lambda *a: "Main"):
                with mock.patch.object(cluster, "certs", made_by):
                    data = cluster.make_backup("a long passphrase", "alex")
                with mock.patch.object(cluster, "certs", read_by):
                    manifest, out = cluster.read_backup(data, "a long passphrase")
                    with self.assertRaises(cluster.ClusterError):
                        cluster.read_backup(data, "the wrong passphrase")
            self.assertEqual(manifest["cluster_id"], "c1")
            self.assertEqual(out["cluster/ca.key"], b"cluster/ca.key")


class OwnCertificate(unittest.TestCase):
    """1.24: a certificate renewed on disk (certbot, win-acme) is served after a restart."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(dir=TMP)
        self.cert, self.key = os.path.join(self.dir, "c.pem"), os.path.join(self.dir, "k.pem")
        for p in (self.cert, self.key):
            with open(p, "w") as f:
                f.write("old")

    def test_renewed_needs_a_change_and_a_pair_that_loads(self):
        stamp = tls.file_stamp(self.cert, self.key)
        with mock.patch.object(tls, "check_pair", lambda c, k: None):
            self.assertFalse(tls.renewed(stamp, self.cert, self.key))
            with open(self.cert, "w") as f:
                f.write("new certificate")
            self.assertTrue(tls.renewed(stamp, self.cert, self.key))
        with mock.patch.object(tls, "check_pair", lambda c, k: "don't match"):
            self.assertFalse(tls.renewed(stamp, self.cert, self.key), "half-written: wait")
        os.remove(self.key)
        with mock.patch.object(tls, "check_pair", lambda c, k: None):
            self.assertFalse(tls.renewed(stamp, self.cert, self.key))

    def test_the_watch_restarts_once(self):
        restarted, logged = threading.Event(), []
        looks = []

        def sleep(s):
            looks.append(s)
            if len(looks) == 2:
                with open(self.cert, "w") as f:
                    f.write("renewed")
            if len(looks) > 5:
                time.sleep(60)
        with mock.patch.object(tls, "check_pair", lambda c, k: None):
            t = tls.watch_own_certificate(self.cert, self.key, logged.append, restarted.set, every=600, sleep=sleep)
            self.assertTrue(restarted.wait(5))
            t.join(5)
        self.assertFalse(t.is_alive())
        self.assertEqual(looks, [600, 600])
        self.assertIn("was renewed", logged[0])


class SftpOnWindows(unittest.TestCase):
    def test_the_service_unit_on_windows(self):
        from core import sftp
        with mock.patch.object(sftp.paths, "WINDOWS", True):
            text = sftp._unit_text()
        self.assertIn("ExecStart=redstonepanel sftp-serve\n", text)
        self.assertNotIn("User=", text)
        self.assertNotIn("NoNewPrivileges", text)
        linux = sftp._unit_text()
        self.assertIn("run.py sftp-serve", linux)

    def test_reading_and_writing_at_an_offset_without_pread(self):
        from core import sftp
        fd, path = tempfile.mkstemp(dir=TMP)
        try:
            os.write(fd, b"0123456789")
            no_pread = types.SimpleNamespace(**{k: getattr(os, k) for k in ("lseek", "read", "write", "SEEK_SET")})
            lock = threading.Lock()
            with mock.patch.object(sftp, "os", no_pread):
                self.assertEqual(sftp._pread(fd, 3, 4, lock), b"456")
                self.assertEqual(sftp._pwrite(fd, b"ab", 8, lock), 2)
                self.assertEqual(sftp._pread(fd, 100, 6, lock), b"67ab")
            self.assertEqual(sftp._pread(fd, 4, 0, lock), b"0123")       # the real os.pread on Linux
        finally:
            os.close(fd)

    def test_a_missing_library_says_how_to_fix_it_on_each_system(self):
        from core import sftp
        with mock.patch.object(sftp.paths, "WINDOWS", True):
            self.assertIn("Install RedstonePanel again", sftp._missing())
        with mock.patch.object(sftp.paths, "WINDOWS", False):
            self.assertIn("sudo apt install python3-paramiko", sftp._missing())

    def test_the_windows_installer_carries_paramiko(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "packaging", "windows", "requirements.txt")) as f:
            pins = {line.split("==")[0] for line in f if "==" in line}
        self.assertTrue({"paramiko", "bcrypt", "pynacl"} <= pins)


if __name__ == "__main__":
    unittest.main()
