"""1.16.8: backup copies on another cluster member. core/backup_copies.py's holder side runs on
temp folders; the master's copying runs with stand-in members. Run:
python -m unittest discover -s tests"""
import hashlib
import os
import sys
import tempfile
import unittest
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-copies-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
os.environ.setdefault("RP_BACKUP_DIR", os.path.join(TMP, "backups"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import backup_copies as BC, backups, cluster, manager as M  # noqa: E402

try:
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except (ImportError, RuntimeError):
    HAVE_FASTAPI = False


def sha(data):
    return hashlib.sha256(data).hexdigest()


def name(server, stamp, kind):
    return f"{server}-{stamp}-{kind}.tar.gz"


class Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(dir=TMP)
        self.log = []
        patches = [
            mock.patch.object(backups, "BACKUP_ROOT", os.path.join(self.root, "backups")),
            mock.patch.object(BC, "CONFIG_FILE", os.path.join(self.root, "copies.json")),
            mock.patch.object(BC, "STATUS_FILE", os.path.join(self.root, "copies-status.json")),
            mock.patch.object(M, "write_log", self.log.append),
            mock.patch.object(BC.panel_settings, "display_name", lambda *a: "Master"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        os.makedirs(backups.BACKUP_ROOT)


class Holder(Base):
    def test_keeps_a_checked_copy(self):
        data = b"world" * 1000
        f = name("minecraft-java", "20261121-040000", "scheduled")
        out = BC.receive_held("n-abc123", "Basement", "minecraft-java", f, len(data), sha(data), 7,
                              [data[:100], data[100:]])
        self.assertEqual(out["size"], len(data))
        path = os.path.join(backups.BACKUP_ROOT, "remote", "n-abc123", "minecraft-java", f)
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), data)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        held = BC.list_held()
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0]["panel_name"], "Basement")
        self.assertEqual(held[0]["kind"], "scheduled")
        self.assertEqual(held[0]["created"], 1795233600)       # 2026-11-21 04:00 UTC
        self.assertEqual(BC.held_sha256("n-abc123", "minecraft-java", f), sha(data))

    def test_damaged_copy_is_refused(self):
        data = b"world" * 100
        f = name("minecraft-java", "20261121-040000", "manual")
        with self.assertRaises(BC.CopyError):
            BC.receive_held("master", "Master", "minecraft-java", f, len(data), sha(b"other"), 7, [data])
        with self.assertRaises(BC.CopyError):
            BC.receive_held("master", "Master", "minecraft-java", f, len(data), sha(data), 7, [data[:-1]])
        with self.assertRaises(BC.CopyError):          # more than it said
            BC.receive_held("master", "Master", "minecraft-java", f, len(data), sha(data), 7, [data, b"x"])
        self.assertEqual(BC.list_held(), [])
        d = os.path.join(backups.BACKUP_ROOT, "remote", "master", "minecraft-java")
        self.assertEqual([x for x in os.listdir(d) if x.endswith(".partial")], [])

    def test_names_are_checked(self):
        data = b"x"
        for panel, server, f in (("../etc", "minecraft-java", name("minecraft-java", "20261121-040000", "manual")),
                                 ("master", "minecraft-java", "../../etc/passwd"),
                                 ("master", "minecraft-java", name("minecraft-other", "20261121-040000", "manual")),
                                 ("master", "evil", name("evil", "20261121-040000", "manual"))):
            with self.assertRaises(BC.CopyError):
                BC.receive_held(panel, "x", server, f, 1, sha(data), 7, [data])
        self.assertFalse(os.path.exists(os.path.join(backups.BACKUP_ROOT, "remote")) and BC.list_held())

    def test_same_retention_as_the_source(self):
        for day in range(1, 6):
            data = f"day{day}".encode()
            BC.receive_held("master", "Master", "minecraft-java", name("minecraft-java", f"2026112{day}-040000", "scheduled"),
                            len(data), sha(data), 3, [data])
        data = b"manual"
        BC.receive_held("master", "Master", "minecraft-java", name("minecraft-java", "20261120-120000", "manual"),
                        len(data), sha(data), 3, [data])
        held = BC.list_held("master", "minecraft-java")
        self.assertEqual([h["file"][15:30] for h in held if h["kind"] == "scheduled"],
                         ["20261125-040000", "20261124-040000", "20261123-040000"])
        self.assertEqual(sum(h["kind"] == "manual" for h in held), 1)
        d = os.path.join(backups.BACKUP_ROOT, "remote", "master", "minecraft-java")
        self.assertEqual(sum(x.endswith(".sha256") for x in os.listdir(d)), 4)     # pruned with their copy

    def test_delete(self):
        data = b"x" * 10
        f = name("minecraft-java", "20261121-040000", "manual")
        BC.receive_held("master", "Master", "minecraft-java", f, len(data), sha(data), 7, [data])
        BC.delete_held("master", "minecraft-java", f)
        self.assertEqual(BC.list_held(), [])
        with self.assertRaises(BC.CopyError):
            BC.delete_held("master", "minecraft-java", f)

    def test_put_into_a_server(self):
        data = b"world"
        f = name("minecraft-new", "20261121-040000", "scheduled")
        with mock.patch.object(M, "_check_server", lambda n: None):
            out = BC.receive_into("minecraft-new", f, len(data), sha(data), [data])
            self.assertFalse(out.get("existed"))
            self.assertEqual([b["file"] for b in backups.list_backups("minecraft-new")], [f])
            again = BC.receive_into("minecraft-new", f, len(data), sha(data), [data])
            self.assertTrue(again["existed"])
        self.assertEqual(BC.backup_sha256("minecraft-new", f), sha(data))


class FakeEnd:
    """A cluster member: its own backups (server -> {file: bytes}) and the copies it keeps."""
    def __init__(self, mid, panel, label, keep=7):
        self.id, self.panel, self.name = mid, panel, label
        self.backups, self.copies, self.keep, self.fail = {}, {}, keep, None

    def add(self, server, stamp, kind, data):
        self.backups.setdefault(server, {})[name(server, stamp, kind)] = data

    def overview(self, server):
        if self.fail:
            raise BC.cluster_client.NodeError("unreachable", self.fail)
        files = self.backups.get(server, {})
        out = []
        for f, data in files.items():
            m = backups.FILE_RE.fullmatch(f)
            out.append({"file": f, "kind": m.group("kind"), "size": len(data), "created": BC._utc(m.group("stamp"))})
        out.sort(key=lambda b: (b["created"], b["file"]), reverse=True)
        return {"schedule": {"keep": self.keep}, "backups": out}

    def sha256(self, server, f):
        return sha(self.backups[server][f])

    def backup_chunks(self, server, f):
        return iter([self.backups[server][f]])

    def held(self, panel=None, server=None):
        if self.fail:
            raise BC.cluster_client.NodeError("unreachable", self.fail)
        return [{"panel": p, "server": s, "file": f, "kind": backups.FILE_RE.fullmatch(f).group("kind"),
                 "size": len(d), "created": BC._utc(backups.FILE_RE.fullmatch(f).group("stamp")), "panel_name": p}
                for (p, s, f), d in self.copies.items() if (not panel or p == panel) and (not server or s == server)]

    def held_sha256(self, panel, server, f):
        return sha(self.copies[(panel, server, f)])

    def held_chunks(self, panel, server, f):
        return iter([self.copies[(panel, server, f)]])

    def hold(self, panel, panel_name, server, f, size, digest, keep, chunks):
        if self.fail:
            raise BC.cluster_client.NodeError("unreachable", self.fail)
        data = b"".join(chunks)
        assert len(data) == size and sha(data) == digest
        self.copies[(panel, server, f)] = data
        self.last_keep = keep
        return {"file": f, "size": size}

    def delete_held(self, panel, server, f):
        del self.copies[(panel, server, f)]

    def put_into(self, server, f, size, digest, chunks):
        self.backups.setdefault(server, {})[f] = b"".join(chunks)
        return {"file": f, "size": size}


class Master(Base):
    def setUp(self):
        super().setUp()
        self.master = FakeEnd("local", "master", "Master")
        self.node = FakeEnd("n-abc123", "n-abc123", "Basement")
        self.node2 = FakeEnd("n-def456", "n-def456", "Attic")
        ends = {"local": self.master, "n-abc123": self.node, "n-def456": self.node2}
        self.sent = []
        patches = [
            mock.patch.object(BC, "member", lambda mid, *a, **k: ends[mid or "local"]),
            mock.patch.object(BC, "members", lambda: [{"id": e.id, "name": e.name} for e in ends.values()]),
            mock.patch.object(BC, "_member_name", lambda mid: ends[mid].name if mid in ends else mid),
            mock.patch.object(BC.cluster, "role", lambda: "master"),
            mock.patch.object(BC.cluster, "find_node", lambda i: {"id": i} if i in ends else None),
            mock.patch.object(BC.notify, "send", lambda *a: self.sent.append(a)),
            mock.patch.object(BC, "sync_soon", lambda *a, **k: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_copies_whats_missing_within_keep(self):
        self.node.keep = 2
        for day in range(1, 5):
            self.node.add("minecraft-java", f"2026112{day}-040000", "scheduled", f"s{day}".encode())
        self.node.add("minecraft-java", "20261120-120000", "manual", b"m")
        self.node.add("minecraft-java", "20261120-130000", "pre-restore", b"p")
        BC.set_target("n-abc123", "minecraft-java", "local", "alex")
        out = BC.sync("n-abc123", "minecraft-java")
        self.assertEqual(out["copied"], [name("minecraft-java", "20261120-120000", "manual"),
                                         name("minecraft-java", "20261123-040000", "scheduled"),
                                         name("minecraft-java", "20261124-040000", "scheduled")])
        self.assertEqual(self.master.last_keep, 2)
        self.assertEqual({k[0] for k in self.master.copies}, {"n-abc123"})
        self.assertEqual(BC.sync("n-abc123", "minecraft-java")["copied"], [])
        st = BC._status("n-abc123", "minecraft-java")
        self.assertTrue(st["ok"])
        self.assertEqual(BC.attention(), [])

    def test_failure_is_reported_once_and_recovers(self):
        self.master.add("minecraft-java", "20261121-040000", "scheduled", b"s")
        BC.set_target("local", "minecraft-java", "n-abc123", "alex")
        self.node.fail = "timed out"
        for _ in range(3):
            with self.assertRaises(BC.CopyError):
                BC.sync("local", "minecraft-java")
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][0], "backup_copy_failed")
        att = BC.attention()
        self.assertEqual(len(att), 1)
        self.assertIn("timed out", att[0]["text"])
        self.assertEqual(att[0]["server"], "minecraft-java")
        self.assertEqual(att[0]["where"], "backups")
        self.node.fail = None
        self.assertEqual(len(BC.sync("local", "minecraft-java")["copied"]), 1)
        self.assertEqual(BC.attention(), [])
        self.assertTrue(any("works again" in m for m in self.log))

    def test_node_servers_attention_has_no_local_link(self):
        self.node.add("minecraft-java", "20261121-040000", "scheduled", b"s")
        BC.set_target("n-abc123", "minecraft-java", "n-def456", "alex")
        self.node2.fail = "refused"
        with self.assertRaises(BC.CopyError):
            BC.sync("n-abc123", "minecraft-java")
        att = BC.attention()
        self.assertIsNone(att[0]["server"])
        self.assertIn("on Basement", att[0]["text"])

    def test_target_rules(self):
        with self.assertRaises(BC.CopyError):
            BC.set_target("local", "minecraft-java", "local", "alex")
        with self.assertRaises(BC.CopyError):
            BC.set_target("n-999999", "minecraft-java", "local", "alex")
        with self.assertRaises(BC.CopyError):
            BC.set_target("local", "../x", "n-abc123", "alex")
        BC.set_target("local", "minecraft-java", "n-abc123", "alex")
        self.assertEqual(BC.target("local", "minecraft-java"), "n-abc123")
        v = BC.view("local", "minecraft-java")
        self.assertEqual(v["to"], "n-abc123")
        self.assertNotIn("local", [m["id"] for m in v["members"]])
        BC.set_target("local", "minecraft-java", None, "alex")
        self.assertIsNone(BC.target("local", "minecraft-java"))
        with self.assertRaises(BC.CopyError):
            BC.sync("local", "minecraft-java")
        with mock.patch.object(BC.cluster, "role", lambda: "node"):
            with self.assertRaises(BC.CopyError):
                BC.set_target("local", "minecraft-java", "n-abc123", "alex")
            self.assertFalse(BC.view("local", "minecraft-java")["available"])

    def test_forget_member(self):
        BC.set_target("local", "minecraft-a", "n-abc123", "alex")
        BC.set_target("n-abc123", "minecraft-b", "local", "alex")
        BC.set_target("n-def456", "minecraft-c", "local", "alex")
        BC.forget_member("n-abc123")
        self.assertEqual(BC.load_config(), {"n-def456/minecraft-c": "local"})

    def test_copies_view_and_put_back(self):
        self.node.add("minecraft-java", "20261121-040000", "scheduled", b"old world")
        BC.set_target("n-abc123", "minecraft-java", "n-def456", "alex")
        BC.sync("n-abc123", "minecraft-java")
        found, missing = BC.copies("n-abc123", "minecraft-java")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["holder"], "n-def456")
        self.assertEqual(missing, [])
        # Basement is gone: put Attic's copy into a new server on the master
        self.node.fail = "unreachable"
        found, missing = BC.copies()
        self.assertEqual([m["id"] for m in missing], ["n-abc123"])
        c = found[0]
        out = BC.put_into("n-def456", c["panel"], c["server"], c["file"], "local", "minecraft-new", "alex")
        self.assertEqual(out["file"], name("minecraft-new", "20261121-040000", "scheduled"))
        self.assertEqual(self.master.backups["minecraft-new"][out["file"]], b"old world")
        chunks, size = BC.download("n-def456", c["panel"], c["server"], c["file"], "alex", "user")
        self.assertEqual(b"".join(chunks), b"old world")
        self.assertEqual(size, 9)
        BC.remove("n-def456", c["panel"], c["server"], c["file"], "alex")
        self.assertEqual(self.node2.copies, {})

    def test_sweep_runs_every_configured_server(self):
        self.master.add("minecraft-a", "20261121-040000", "manual", b"a")
        self.node.add("minecraft-b", "20261121-040000", "manual", b"b")
        BC.set_target("local", "minecraft-a", "n-abc123", "alex")
        BC.set_target("n-abc123", "minecraft-b", "local", "alex")
        self.node.fail = "down"
        BC.sweep()                      # one failure doesn't stop the others
        self.assertFalse(BC._status("local", "minecraft-a")["ok"])
        self.assertFalse(BC._status("n-abc123", "minecraft-b")["ok"])
        self.node.fail = None
        BC.sweep()
        self.assertEqual(len(self.node.copies), 1)
        self.assertEqual(len(self.master.copies), 1)


@unittest.skipUnless(HAVE_FASTAPI, "needs fastapi")
class Routes(Base):
    """The holder's routes: only the master's token reaches them; browsers never do."""
    def setUp(self):
        super().setUp()
        import main
        self.main = main
        self.client = TestClient(main.app)

    def test_browsers_cant_reach_holder_routes(self):
        user = {"username": "alex", "role": "admin", "source": "local"}
        with mock.patch.object(self.main.auth, "read_session_token", lambda t: user):
            self.client.cookies.set(self.main.auth.SESSION_COOKIE, "x")
            r = self.client.get("/api/cluster/held-backups")
            self.assertEqual(r.status_code, 403)
            r = self.client.put("/api/cluster/held-backups/master/minecraft-x/minecraft-x-20261121-040000-manual.tar.gz"
                                "?size=1&sha256=" + "0" * 64, content=b"x")
            self.assertEqual(r.status_code, 403)

    def test_master_token_sends_and_lists_a_copy(self):
        st = {"role": "node", "token_hash": "h", "epoch": 1, "master_name": "Master"}
        data = b"world"
        f = "minecraft-x-20261121-040000-manual.tar.gz"
        with mock.patch.object(cluster, "state", lambda: st), \
                mock.patch.object(cluster, "_token_ok", lambda t, s: True), \
                mock.patch.object(cluster, "_write_json", lambda *a: None):
            h = {"Authorization": "Bearer rpn_" + "a" * 43, "X-RedstonePanel-Epoch": "1",
                 "X-RedstonePanel-Role": "admin"}
            r = self.client.put(f"/api/cluster/held-backups/master/minecraft-x/{f}?size={len(data)}&sha256={sha(data)}"
                                "&keep=3&panel_name=Master", content=data, headers=h)
            self.assertEqual(r.status_code, 200, r.text)
            r = self.client.get("/api/cluster/held-backups?panel=master", headers=h)
            self.assertEqual([c["file"] for c in r.json()], [f])
            r = self.client.get(f"/api/cluster/held-backups/master/minecraft-x/{f}", headers=h)
            self.assertEqual(r.content, data)
            r = self.client.get(f"/api/cluster/held-backups/master/minecraft-x/{f}/sha256", headers=h)
            self.assertEqual(r.json()["sha256"], sha(data))
            r = self.client.put(f"/api/cluster/held-backups/master/minecraft-x/{f}?size=5&sha256={'0' * 64}",
                                content=data, headers=h)
            self.assertEqual(r.status_code, 400)
            r = self.client.delete(f"/api/cluster/held-backups/master/minecraft-x/{f}", headers=h)
            self.assertEqual(r.status_code, 200)

    def test_standalone_has_no_copies(self):
        user = {"username": "alex", "role": "admin", "source": "local"}
        with mock.patch.object(self.main.auth, "read_session_token", lambda t: user), \
                mock.patch.object(cluster, "role", lambda: "standalone"):
            self.client.cookies.set(self.main.auth.SESSION_COOKIE, "x")
            r = self.client.get("/api/cluster/backup-copies/local/minecraft-x")
            self.assertEqual(r.json(), {"available": False, "role": "standalone"})
            r = self.client.put("/api/cluster/backup-copies/local/minecraft-x", json={"to": "n-abc123"})
            self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
