"""Tests for core/backup_targets.py (1.16.11): backup copies on SSH or S3-compatible storage.

S3 runs against a small in-process S3 server that checks every request's signature and body
SHA-256 the way real storage does. SSH runs through a stand-in `ssh` that runs the remote command
on this machine, so rsync really copies (those tests need rsync; CI installs it).
Run: python -m unittest discover -s tests"""
import hashlib
import hmac
import io
import json
import os
import shutil
import stat
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-targets-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import fcntl  # noqa: F401
    from core import backup_targets as T, backups as B, manager as M, notify  # noqa: E402
except ImportError:          # Windows
    T = None

ACCESS, SECRET, REGION, BUCKET = "AKIDTEST", "secret/key+TEST", "us-east-1", "rp-bucket"


# ---------------------------------------------------------------------------
# A small S3: objects in a dict, signatures and body checksums checked like the real thing
# ---------------------------------------------------------------------------
class FakeS3(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), S3Handler)
        self.objects, self.uploads, self.requests, self.page = {}, {}, [], 2
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def endpoint(self):
        return f"http://127.0.0.1:{self.server_address[1]}"


class S3Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _fail(self, status, code, msg=""):
        body = f"<Error><Code>{code}</Code><Message>{msg}</Message></Error>".encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _ok(self, body=b"", headers=None):
        self.send_response(200)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self, method):
        s = self.server
        u = urllib.parse.urlsplit(self.path)
        query = dict(urllib.parse.parse_qsl(u.query, keep_blank_values=True))
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        s.requests.append((method, u.path, query))
        # Signature: recompute from what arrived
        auth = self.headers.get("Authorization", "")
        try:
            signed = auth.split("SignedHeaders=")[1].split(",")[0].split(";")
            given = auth.split("Signature=")[1]
        except IndexError:
            return self._fail(403, "AccessDenied", "no signature")
        hdrs = {n: self.headers.get(n) for n in signed if n not in ("host", "x-amz-content-sha256", "x-amz-date")}
        want = T.sign(method, self.headers["Host"], urllib.parse.unquote(u.path), query, hdrs,
                      self.headers["x-amz-content-sha256"], ACCESS, SECRET, REGION, self.headers["x-amz-date"])
        if want["Authorization"].split("Signature=")[1] != given:
            return self._fail(403, "SignatureDoesNotMatch")
        if hashlib.sha256(body).hexdigest() != self.headers["x-amz-content-sha256"]:
            return self._fail(400, "XAmzContentSHA256Mismatch")
        parts = urllib.parse.unquote(u.path).lstrip("/").split("/", 1)
        if parts[0] != BUCKET:
            return self._fail(404, "NoSuchBucket")
        key = parts[1] if len(parts) > 1 else ""
        if method == "GET" and not key:
            prefix = query.get("prefix", "")
            keys = sorted(k for k in s.objects if k.startswith(prefix))
            start = int(query.get("continuation-token", "0"))
            page = keys[start:start + s.page]
            more = start + s.page < len(keys)
            xml = ('<?xml version="1.0"?><ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                   + "".join(f"<Contents><Key>{k}</Key><Size>{len(s.objects[k])}</Size></Contents>" for k in page)
                   + f"<IsTruncated>{'true' if more else 'false'}</IsTruncated>"
                   + (f"<NextContinuationToken>{start + s.page}</NextContinuationToken>" if more else "")
                   + "</ListBucketResult>")
            return self._ok(xml.encode())
        if method == "GET":
            if key not in s.objects:
                return self._fail(404, "NoSuchKey")
            return self._ok(s.objects[key])
        if method == "PUT" and "uploadId" in query:
            s.uploads[query["uploadId"]][int(query["partNumber"])] = body
            return self._ok(headers={"ETag": f'"{hashlib.md5(body).hexdigest()}"'})
        if method == "PUT":
            s.objects[key] = body
            return self._ok(headers={"ETag": '"x"'})
        if method == "POST" and "uploads" in query:
            uid = f"up{len(s.uploads)}"
            s.uploads[uid] = {}
            return self._ok(f"<InitiateMultipartUploadResult><UploadId>{uid}</UploadId></InitiateMultipartUploadResult>".encode())
        if method == "POST" and "uploadId" in query:
            got = s.uploads.pop(query["uploadId"])
            s.objects[key] = b"".join(got[n] for n in sorted(got))
            return self._ok(b"<CompleteMultipartUploadResult/>")
        if method == "DELETE":
            if "uploadId" in query:
                s.uploads.pop(query["uploadId"], None)
            else:
                s.objects.pop(key, None)
            self.send_response(204)
            self.end_headers()
            return
        return self._fail(400, "Unsupported")

    def do_GET(self):
        self._handle("GET")

    def do_PUT(self):
        self._handle("PUT")

    def do_POST(self):
        self._handle("POST")

    def do_DELETE(self):
        self._handle("DELETE")


FAKE_SSH = r'''#!/usr/bin/env python3
# Stand-in for ssh: drops the options and the host and runs the command here, in $FAKE_HOME.
import os, subprocess, sys
args, i = sys.argv[1:], 0
while i < len(args) and args[i].startswith("-"):
    i += 2 if args[i] in ("-i", "-p", "-o", "-l") else 1
cmd = " ".join(args[i + 1:])
if os.environ.get("FAKE_SSH_DOWN"):
    sys.stderr.write("ssh: connect to host example port 22: Connection refused\n"); sys.exit(255)
env = dict(os.environ, HOME=os.environ["FAKE_HOME"])
sys.exit(subprocess.call(["sh", "-c", cmd], cwd=os.environ["FAKE_HOME"], env=env))
'''


def stamp(days_ago, now):
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime(now - days_ago * 86400))


@unittest.skipIf(T is None, "needs Linux (fcntl)")
class Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(dir=TMP)
        self.data = os.path.join(self.root, "data")
        self.backups = os.path.join(self.root, "backups")
        self.sent = []
        patches = [
            mock.patch.object(T, "CONFIG_FILE", os.path.join(self.data, "backup-targets.json")),
            mock.patch.object(T, "STATUS_FILE", os.path.join(self.data, "backup-targets-status.json")),
            mock.patch.object(T, "LOCK_DIR", os.path.join(self.data, "locks")),
            mock.patch.object(T, "SSH_DIR", os.path.join(self.data, "ssh")),
            mock.patch.object(T, "SSH_KEY", os.path.join(self.data, "ssh", "id_ed25519")),
            mock.patch.object(T, "KNOWN_HOSTS", os.path.join(self.data, "ssh", "known_hosts")),
            mock.patch.object(T, "SSH_KEYGEN", os.path.join(self.root, "no-ssh-keygen")),
            mock.patch.object(B, "BACKUP_ROOT", self.backups),
            mock.patch.object(B, "SCHEDULE_FILE", os.path.join(self.data, "backups.json")),
            mock.patch.object(M, "list_servers", lambda: [{"name": "minecraft-java"}, {"name": "minecraft-bedrock"}]),
            mock.patch.object(M, "_check_server", lambda s: None),
            mock.patch.object(notify, "send", lambda *a, **k: self.sent.append(a) or (True, None)),
            mock.patch.object(T.socket, "gethostname", lambda: "Survival Box"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def backup(self, server, days_ago, kind="scheduled", now=None, content=None):
        now = now or time.time()
        name = f"{server}-{stamp(days_ago, now)}-{kind}.tar.gz"
        d = os.path.join(self.backups, server)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, name), "wb") as f:
            f.write(content or os.urandom(300 + int(days_ago * 10)))
        return name


@unittest.skipIf(T is None, "needs Linux (fcntl)")
class Signing(unittest.TestCase):
    def test_aws_documented_example(self):
        # "Example: GET Object" from the AWS Signature Version 4 documentation for S3
        h = T.sign("GET", "examplebucket.s3.amazonaws.com", "/test.txt", {}, {"Range": "bytes=0-9"},
                   T.EMPTY_SHA, "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "us-east-1",
                   "20130524T000000Z")
        self.assertEqual(h["Authorization"],
                         "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, "
                         "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
                         "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41")

    def test_aws_documented_list_example(self):
        # "Example: GET Bucket (List Objects)": query parameters are sorted and encoded
        h = T.sign("GET", "examplebucket.s3.amazonaws.com", "/", {"max-keys": "2", "prefix": "J"}, {},
                   T.EMPTY_SHA, "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "us-east-1",
                   "20130524T000000Z")
        self.assertTrue(h["Authorization"].endswith(
            "Signature=34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7"))


@unittest.skipIf(T is None, "needs Linux (fcntl)")
class Settings(Base):
    def s3_fields(self, **kw):
        return {"type": "s3", "name": "B2", "endpoint": "https://s3.eu-central-003.backblazeb2.com",
                "bucket": BUCKET, "prefix": "mc", "region": "eu-central-003", "access_key": ACCESS,
                "secret_key": SECRET, **kw}

    def test_secret_never_shown_and_kept_on_edit(self):
        v = T.save_target(None, self.s3_fields(), "alex")
        self.assertNotIn("secret_key", v)
        self.assertTrue(v["secret_set"])
        self.assertNotIn(ACCESS, json.dumps(v))
        T.save_target(v["id"], self.s3_fields(name="Backblaze", secret_key="", access_key=""), "alex")
        t = T.get_target(v["id"])
        self.assertEqual((t["name"], t["secret_key"], t["access_key"]), ("Backblaze", SECRET, ACCESS))
        self.assertEqual(stat.S_IMODE(os.stat(T.CONFIG_FILE).st_mode), 0o600)

    def test_bad_settings_refused(self):
        for bad in ({"endpoint": "ftp://x"}, {"endpoint": "https://x/path"}, {"bucket": "Bad_Bucket"},
                    {"prefix": "../up"}, {"region": "US EAST"}, {"keep_days": -1}, {"keep_count": 999}):
            with self.assertRaises(T.TargetError, msg=bad):
                T.save_target(None, self.s3_fields(**bad), "alex")
        ssh = {"type": "ssh", "name": "NAS", "host": "nas.lan", "user": "backup", "folder": "mc"}
        for bad in ({"host": "-oProxyCommand=x"}, {"host": "a b"}, {"user": "-x"}, {"folder": "a/../../etc"},
                    {"folder": "x;rm -rf /"}, {"folder": "$(id)"}, {"port": 70000}):
            with self.assertRaises(T.TargetError, msg=bad):
                T.save_target(None, {**ssh, **bad}, "alex")
        v = T.save_target(None, ssh, "alex")
        self.assertEqual(v["where"], "backup@nas.lan:mc")

    def test_panel_id_made_once(self):
        a = T.panel_id()
        self.assertRegex(a, r"^survival-box-[0-9a-f]{4}$")
        self.assertEqual(T.panel_id(), a)

    def test_servers_all_then_one_off(self):
        v = T.save_target(None, self.s3_fields(), "alex")
        self.assertEqual(T.targets_for("minecraft-java"), [v["id"]])
        with mock.patch.object(T, "sync_soon"):
            T.set_server(v["id"], "minecraft-java", False, "alex")
        self.assertEqual(T.targets_for("minecraft-java"), [])
        self.assertEqual(T.targets_for("minecraft-bedrock"), [v["id"]])
        with mock.patch.object(T, "sync_soon") as soon:
            T.set_server(v["id"], "minecraft-java", True, "alex")
        soon.assert_called_once_with("minecraft-java")
        self.assertEqual(T.targets_for("minecraft-java"), [v["id"]])

    def test_remove_drops_status(self):
        v = T.save_target(None, self.s3_fields(), "alex")
        T._set_status(v["id"], "minecraft-java", ok=False, error="x")
        T.remove_target(v["id"], "alex")
        self.assertEqual(T._read(T.STATUS_FILE), {})


@unittest.skipIf(T is None, "needs Linux (fcntl)")
class Retention(unittest.TestCase):
    def entries(self, now, days, kind="scheduled"):
        return [{"file": f"f{d}", "kind": kind, "created": now - d * 86400} for d in days]

    def test_count_and_age(self):
        now = 1_800_000_000
        e = self.entries(now, [0, 1, 2, 3, 20, 40])
        self.assertEqual([x["file"] for x in T.scheduled_to_keep(e, 4, 0, now)], ["f0", "f1", "f2", "f3"])
        self.assertEqual([x["file"] for x in T.scheduled_to_keep(e, 10, 14, now)], ["f0", "f1", "f2", "f3"])
        self.assertEqual([x["file"] for x in T.to_delete(e, 10, 14, now)], ["f20", "f40"])

    def test_newest_survives_age_limit(self):
        now = 1_800_000_000
        e = self.entries(now, [30, 31, 60])          # the server was stopped for a month
        self.assertEqual([x["file"] for x in T.scheduled_to_keep(e, 7, 14, now)], ["f30"])

    def test_manual_always_copied_never_deleted(self):
        now = 1_800_000_000
        e = self.entries(now, [100], "manual") + self.entries(now, [0, 1, 2])
        self.assertEqual([x["file"] for x in T.wanted(e, 1, 0, now)], ["f100", "f0"])
        self.assertEqual([x["file"] for x in T.to_delete(e, 1, 0, now)], ["f1", "f2"])


@unittest.skipIf(T is None, "needs Linux (fcntl)")
class LocalRetentionByAge(Base):
    def test_prune_by_age_keeps_newest(self):
        now = time.time()
        old = [self.backup("minecraft-java", d, now=now) for d in (20, 30)]
        new = [self.backup("minecraft-java", d, now=now) for d in (0.5, 1)]
        manual = self.backup("minecraft-java", 90, "manual", now=now)
        B.prune("minecraft-java", 7, 14, now=now)
        left = set(os.listdir(os.path.join(self.backups, "minecraft-java")))
        self.assertEqual(left, set(new) | {manual})
        for f in new:
            os.remove(os.path.join(self.backups, "minecraft-java", f))
        B.prune("minecraft-java", 7, 14, now=now)          # only old ones left: the newest stays
        self.assertEqual(set(os.listdir(os.path.join(self.backups, "minecraft-java"))), {manual})

    def test_java_info_file_is_backed_up(self):
        self.assertFalse(B._skipped(".redstonepanel-server.json"))
        self.assertTrue(B._skipped(".redstonepanel-restore-abc"))
        self.assertTrue(B._skipped("server.jar"))


@unittest.skipIf(T is None, "needs Linux (fcntl)")
class S3(Base):
    def setUp(self):
        super().setUp()
        self.s3 = FakeS3()
        self.addCleanup(self.s3.server_close)
        self.addCleanup(self.s3.shutdown)
        self.tid = T.save_target(None, {"type": "s3", "name": "MinIO", "endpoint": self.s3.endpoint, "bucket": BUCKET,
                                        "prefix": "rp/mc", "region": REGION, "access_key": ACCESS,
                                        "secret_key": SECRET, "keep_count": 2}, "alex")["id"]
        self.pid = T.panel_id()

    def test_test_button(self):
        r = T.test_target(self.tid)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["steps"], ["wrote a file", "listed it", "deleted it"])
        self.assertEqual(self.s3.objects, {})
        bad = T.test_target(self.tid, {**T.get_target(self.tid), "secret_key": "wrong"})
        self.assertFalse(bad["ok"])
        self.assertIn("refused the access key", bad["error"])

    def test_sync_copies_checks_and_prunes(self):
        now = time.time()
        manual = self.backup("minecraft-java", 50, "manual", now=now)
        s = [self.backup("minecraft-java", d, now=now) for d in (3, 2, 1)]
        self.backup("minecraft-java", 0, "pre-restore", now=now)
        r = T.sync_one(self.tid, "minecraft-java", force=True)
        self.assertEqual(sorted(r["copied"]), sorted([manual, s[1], s[2]]))
        prefix = f"rp/mc/{self.pid}/minecraft-java/"
        for f in r["copied"]:
            data = self.s3.objects[prefix + f]
            self.assertEqual(self.s3.objects[prefix + f + ".sha256"].decode(), hashlib.sha256(data).hexdigest())
        # A new scheduled backup: copied, and the oldest scheduled copy beyond Keep 2 goes
        newest = self.backup("minecraft-java", 0.1, now=now)
        r = T.sync_one(self.tid, "minecraft-java")
        self.assertEqual((r["copied"], r["deleted"]), ([newest], [s[1]]))
        self.assertNotIn(prefix + s[1], self.s3.objects)
        self.assertNotIn(prefix + s[1] + ".sha256", self.s3.objects)
        self.assertIn(prefix + manual, self.s3.objects)
        st = T.status(self.tid, "minecraft-java")
        self.assertTrue(st["ok"])
        self.assertEqual(st["last_copied"], newest)

    def test_nothing_new_means_no_requests(self):
        self.backup("minecraft-java", 1)
        T.sync_one(self.tid, "minecraft-java", force=True)
        before = len(self.s3.requests)
        self.assertTrue(T.sync_one(self.tid, "minecraft-java").get("skipped"))
        self.assertEqual(len(self.s3.requests), before)

    def test_list_pages(self):
        for d in range(5):
            self.backup("minecraft-java", d, "manual")
        T.sync_one(self.tid, "minecraft-java", force=True)
        self.assertEqual(len(T.client(T.get_target(self.tid)).list(self.pid, "minecraft-java")), 5)

    def test_multipart(self):
        with mock.patch.object(T, "MULTIPART_OVER", 1000), mock.patch.object(T, "PART_SIZE", 700):
            f = self.backup("minecraft-java", 1, "manual", content=os.urandom(2500))
            T.sync_one(self.tid, "minecraft-java", force=True)
        posts = [q for m, p, q in self.s3.requests if m == "PUT" and "partNumber" in q]
        self.assertEqual(len(posts), 4)
        with open(os.path.join(self.backups, "minecraft-java", f), "rb") as fh:
            self.assertEqual(self.s3.objects[f"rp/mc/{self.pid}/minecraft-java/{f}"], fh.read())

    def test_storage_checks_the_body(self):
        path = os.path.join(self.root, "x")
        with open(path, "wb") as f:
            f.write(b"world")
        with self.assertRaises(T.TargetError) as e:
            T.client(T.get_target(self.tid)).upload(path, self.pid, "minecraft-java",
                                                    "minecraft-java-20260101-000000-manual.tar.gz", "0" * 64, 5)
        self.assertIn("XAmzContentSHA256Mismatch", str(e.exception))

    def test_failure_is_attention_and_one_webhook(self):
        self.backup("minecraft-java", 1)
        T.save_target(self.tid, {**T.get_target(self.tid), "endpoint": "http://127.0.0.1:9"}, "alex")
        for _ in range(2):
            with self.assertRaises(T.TargetError):
                T.sync_one(self.tid, "minecraft-java")
        self.assertEqual([a[0] for a in self.sent], ["backup_copy_failed"])
        items = T.attention()
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0]["server"], items[0]["where"]), ("minecraft-java", "backups"))
        T.save_target(self.tid, {**T.get_target(self.tid), "endpoint": self.s3.endpoint}, "alex")
        T.sync_one(self.tid, "minecraft-java")
        self.assertEqual(T.attention(), [])

    def test_fetch_restores_into_backups_and_checks(self):
        f = self.backup("minecraft-java", 1, "manual")
        T.sync_one(self.tid, "minecraft-java", force=True)
        os.remove(os.path.join(self.backups, "minecraft-java", f))
        r = T.fetch(self.tid, "minecraft-java", f, "alex")
        self.assertEqual(r, {"file": f, "existed": False})
        self.assertTrue(os.path.isfile(os.path.join(self.backups, "minecraft-java", f)))
        # Into another server: renamed to it
        r = T.fetch(self.tid, "minecraft-bedrock", f, "alex", from_server="minecraft-java")
        self.assertTrue(r["file"].startswith("minecraft-bedrock-"))
        # A damaged copy is refused and nothing is left behind
        key = f"rp/mc/{self.pid}/minecraft-java/{f}"
        self.s3.objects[key] = b"damaged" + self.s3.objects[key]
        os.remove(os.path.join(self.backups, "minecraft-java", f))
        with self.assertRaises(T.TargetError):
            T.fetch(self.tid, "minecraft-java", f, "alex")
        self.assertEqual([x for x in os.listdir(os.path.join(self.backups, "minecraft-java")) if x.startswith(f)], [])

    def test_view_and_delete_copy(self):
        f = self.backup("minecraft-java", 1, "manual")
        T.sync_one(self.tid, "minecraft-java", force=True)
        v = T.view("minecraft-java")
        self.assertEqual([c["file"] for c in v["targets"][0]["copies"]], [f])
        self.assertNotIn("secret_key", json.dumps(v))
        T.delete_copy(self.tid, "minecraft-java", f, "alex")
        self.assertEqual(T.view("minecraft-java")["targets"][0]["copies"], [])

    def test_import_on_a_fresh_machine(self):
        # A Java backup as the panel makes them: no server.jar, but the loader and version recorded
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for name, data in (("server.properties", b"level-name=world\nserver-port=25565\n"),
                               ("eula.txt", b"eula=true\n"), ("world/level.dat", b"x"),
                               (".redstonepanel-server.json", b'{"loader": "paper", "minecraft": "1.21.4"}')):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        f = self.backup("minecraft-java", 1, "manual", content=buf.getvalue())
        T.sync_one(self.tid, "minecraft-java", force=True)
        opt = os.path.join(self.root, "opt")
        os.makedirs(os.path.join(opt, "minecraft-java"))           # the name is taken here
        units = os.path.join(self.root, "units")
        with mock.patch.object(M, "SERVERS_ROOT", opt), \
                mock.patch.object(M, "_unit_path", lambda n: os.path.join(units, f"{n}.service")), \
                mock.patch.object(M, "restore_server", return_value={"name": "minecraft-java-2"}) as restore:
            T.browse(self.tid)
            r = T.import_copy(self.tid, self.pid, "minecraft-java", f, "alex")
        self.assertEqual(r["name"], "minecraft-java-2")
        restore.assert_called_once_with("minecraft-java-2", same_version=True)
        d = os.path.join(opt, "minecraft-java-2")
        with open(os.path.join(d, M.REMOVED_INFO)) as fh:
            self.assertEqual(json.load(fh)["edition"], "java")
        self.assertTrue(os.path.isfile(os.path.join(d, "world", "level.dat")))
        self.assertEqual(sorted(x for x in os.listdir(opt) if x.startswith(".")), [])

    def test_browse_lists_every_panel(self):
        self.backup("minecraft-java", 1, "manual")
        T.sync_one(self.tid, "minecraft-java", force=True)
        self.s3.objects["rp/mc/old-box-abcd/minecraft-bedrock/minecraft-bedrock-20260101-000000-scheduled.tar.gz"] = b"x"
        found = T.browse(self.tid)["copies"]
        self.assertEqual({c["panel"] for c in found}, {self.pid, "old-box-abcd"})


@unittest.skipIf(T is None or not shutil.which("rsync"), "needs Linux and rsync")
class SSH(Base):
    def setUp(self):
        super().setUp()
        self.home = os.path.join(self.root, "home")
        os.makedirs(self.home)
        bin_dir = os.path.join(self.root, "bin")
        os.makedirs(bin_dir)
        fake = os.path.join(bin_dir, "ssh")
        with open(fake, "w") as f:
            f.write(FAKE_SSH)
        os.chmod(fake, 0o755)
        env = mock.patch.dict(os.environ, {"FAKE_HOME": self.home})
        env.start()
        self.addCleanup(env.stop)
        for p in (mock.patch.object(T, "SSH", fake), mock.patch.object(T, "ensure_key", lambda: "ssh-ed25519 AAAA test")):
            p.start()
            self.addCleanup(p.stop)
        self.tid = T.save_target(None, {"type": "ssh", "name": "NAS", "host": "nas.lan", "user": "backup",
                                        "folder": "~/mc backups".replace(" ", "-"), "keep_count": 1}, "alex")["id"]
        self.pid = T.panel_id()

    def test_test_button(self):
        r = T.test_target(self.tid)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["steps"], ["signed in", "wrote a file", "listed it", "deleted it"])
        self.assertEqual(os.listdir(os.path.join(self.home, "mc-backups")), [])

    def test_sync_fetch_delete(self):
        now = time.time()
        manual = self.backup("minecraft-java", 9, "manual", now=now)
        old, new = (self.backup("minecraft-java", d, now=now) for d in (2, 1))
        r = T.sync_one(self.tid, "minecraft-java", force=True)
        self.assertEqual(sorted(r["copied"]), sorted([manual, new]))
        d = os.path.join(self.home, "mc-backups", self.pid, "minecraft-java")
        self.assertEqual(sorted(os.listdir(d)), sorted([manual, manual + ".sha256", new, new + ".sha256"]))
        with open(os.path.join(d, new + ".sha256")) as f:
            with open(os.path.join(self.backups, "minecraft-java", new), "rb") as g:
                self.assertEqual(f.read(), hashlib.sha256(g.read()).hexdigest())
        newer = self.backup("minecraft-java", 0, now=now)
        r = T.sync_one(self.tid, "minecraft-java")
        self.assertEqual((r["copied"], r["deleted"]), ([newer], [new]))
        os.remove(os.path.join(self.backups, "minecraft-java", manual))
        self.assertEqual(T.fetch(self.tid, "minecraft-java", manual, "alex")["file"], manual)
        T.delete_copy(self.tid, "minecraft-java", manual, "alex")
        self.assertNotIn(manual, os.listdir(d))
        self.assertEqual({c["file"] for c in T.view("minecraft-java")["targets"][0]["copies"]}, {newer})

    def test_damaged_upload_is_deleted(self):
        path = os.path.join(self.root, "w.tar.gz")
        with open(path, "wb") as f:
            f.write(b"world")
        name = "minecraft-java-20260101-000000-manual.tar.gz"
        with self.assertRaises(T.TargetError) as e:
            T.client(T.get_target(self.tid)).upload(path, self.pid, "minecraft-java", name, "0" * 64, 5)
        self.assertIn("damaged", str(e.exception))
        self.assertEqual(os.listdir(os.path.join(self.home, "mc-backups", self.pid, "minecraft-java")), [])

    def test_unreachable_is_explained(self):
        with mock.patch.dict(os.environ, {"FAKE_SSH_DOWN": "1"}):
            r = T.test_target(self.tid)
        self.assertFalse(r["ok"])
        self.assertIn("refused the connection on port 22", r["error"])


@unittest.skipIf(T is None, "needs Linux (fcntl)")
class AfterBackup(Base):
    def test_backup_command_starts_a_separate_unit(self):
        T.save_target(None, {"type": "ssh", "name": "NAS", "host": "nas.lan", "user": "b", "folder": "mc"}, "alex")
        calls = []
        with mock.patch.object(T.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or
                               mock.Mock(returncode=0, stdout="", stderr="")):
            self.assertTrue(T.start_after_backup("minecraft-java"))
        self.assertEqual(calls[0][0], T.SYSTEMD_RUN)
        self.assertIn("--unit=redstonepanel-offsite-minecraft-java", calls[0])
        self.assertEqual(calls[0][-2:], ["offsite-copy", "minecraft-java"])

    def test_no_storage_no_unit(self):
        with mock.patch.object(T.subprocess, "run") as run:
            self.assertFalse(T.start_after_backup("minecraft-java"))
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
