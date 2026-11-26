"""1.16: moving and copying a server between cluster members. core/transfer.py (each member's
part) runs on temp folders with systemctl replaced; core/moves.py (the master's job) runs with
stand-in members. Run: python -m unittest discover -s tests"""
import io
import json
import os
import sys
import tarfile
import tempfile
import time
import unittest
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-move-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
os.environ.setdefault("RP_BACKUP_DIR", os.path.join(TMP, "backups"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import backups, manager as M, moves, transfer as T  # noqa: E402

try:
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except (ImportError, RuntimeError):
    HAVE_FASTAPI = False


class Member(unittest.TestCase):
    """Two members (A and B) on one machine: each has its own /opt and units folder."""

    def setUp(self):
        self.root = tempfile.mkdtemp(dir=TMP)
        self.opt = {k: os.path.join(self.root, k, "opt") for k in "AB"}
        self.member = "A"
        for k in "AB":
            os.makedirs(self.opt[k])
            os.makedirs(os.path.join(self.root, k, "units"))
        self.commands, self.active = [], set()
        self.transfers = os.path.join(self.root, "transfers")

        def run(cmd, shell=False):
            self.commands.append(cmd)
            if cmd[:2] == ["systemctl", "stop"] or cmd[:3] == ["sudo", "systemctl", "stop"]:
                self.active.discard(cmd[-1].replace(".service", ""))
            if cmd[:2] == ["systemctl", "start"]:
                self.active.add(cmd[-1].replace(".service", ""))
        self.removed = []

        def write_unit(name, d, edition, xms="1G", xmx="2G", java="/usr/bin/java", jvm=None):
            with open(M._unit_path(name), "w") as f:
                f.write(f"WorkingDirectory={d}\nExecStart={java} -Xms{xms} -Xmx{xmx} -jar server.jar\n")
        patches = [
            # Another test module may have imported core.backups first, with the real
            # /var/backups, which only root can create (the GitHub runner isn't root)
            mock.patch.object(backups, "BACKUP_ROOT", os.path.join(self.root, "backups")),
            mock.patch.object(T, "TRANSFER_DIR", self.transfers),
            mock.patch.object(M, "SERVERS_ROOT", self.opt["A"]),
            mock.patch.object(M, "_unit_path", lambda n: os.path.join(self.root, self.member, "units", f"{n}.service")),
            mock.patch.object(M, "_check_server", self._check),
            mock.patch.object(M, "get_server_dir", lambda n: os.path.join(M.SERVERS_ROOT, n)),
            mock.patch.object(M, "_unit_exec_start", lambda n: "/usr/bin/java -Xms2G -Xmx6G -jar server.jar nogui"),
            mock.patch.object(M, "run_and_log", run),
            mock.patch.object(M, "_is_java", lambda n: True),
            mock.patch.object(M, "write_log", lambda m: None),
            mock.patch.object(M, "_write_unit", write_unit),
            mock.patch.object(M, "open_firewall_rules", lambda r: None),
            mock.patch.object(M, "ensure_minecraft_user", lambda h: None),
            mock.patch.object(M, "_used_ports", lambda exclude_dir=None: set()),
            mock.patch.object(M, "java_for_folder", lambda d: "/usr/lib/jvm/java-21/bin/java"),
            mock.patch.object(M, "remove_server", lambda n, keep_world, note=None: self.removed.append((n, keep_world, note))),
            mock.patch.object(backups, "_is_active", lambda n: n in self.active),
            mock.patch.object(T, "_stays_running", lambda n: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _check(self, name):
        if not os.path.exists(M._unit_path(name)):
            raise M.ServerError(f"No Minecraft server called '{name}' on this panel.")

    def on(self, member):
        self.member = member
        p = mock.patch.object(M, "SERVERS_ROOT", self.opt[member])
        p.start()
        self.addCleanup(p.stop)

    def server(self, name="minecraft-java", running=True):
        d = os.path.join(self.opt["A"], name)
        os.makedirs(os.path.join(d, "world", "region"))
        os.makedirs(os.path.join(d, "logs"))
        for rel, data in (("server.jar", b"jar"), ("world/region/r.0.0.mca", b"x" * 5000),
                          ("server.properties", b"server-port=25570\nmotd=Home\n"), ("logs/latest.log", b"log"),
                          (M.REMOVED_INFO, b"{}"), (".redstonepanel-mods.json", b"{}")):
            with open(os.path.join(d, rel), "wb") as f:
                f.write(data)
        with open(M._unit_path(name), "w") as f:
            f.write(f"WorkingDirectory={d}\n")
        if running:
            self.active.add(name)
        return d

    def move_to_b(self, name="minecraft-java", stop=True, **install):
        exp = T.export(name, stop)
        with open(T.archive_path(exp["id"]), "rb") as f:
            data = f.read()
        self.on("B")
        imp = T.begin_import(exp["size"], exp["sha256"], exp["meta"])
        T.receive(imp["id"], [data[i:i + 1000] for i in range(0, len(data), 1000)])
        return exp, imp, T.install(imp["id"], **install)


class Transfer(Member):
    def test_export_packs_the_whole_folder_and_blocks_start(self):
        self.server()
        exp = T.export("minecraft-java", stop=True)
        self.assertTrue(exp["was_running"])
        self.assertNotIn("minecraft-java", self.active, "a move stops the server first")
        with tarfile.open(T.archive_path(exp["id"])) as t:
            names = set(t.getnames())
        self.assertTrue({"server.jar", "logs/latest.log", ".redstonepanel-mods.json", "world/region/r.0.0.mca"} <= names,
                        "a move takes everything a backup leaves out")
        self.assertNotIn(M.REMOVED_INFO, names)
        self.assertEqual(exp["meta"]["xmx"], "6G")
        self.assertTrue(T.is_moving("minecraft-java"))
        with self.assertRaises(M.ServerError):
            M.control_server("minecraft-java", "start")

    def test_a_copy_leaves_the_server_running(self):
        self.server()
        with mock.patch.object(backups, "_backup_running_java", return_value=True) as live:
            exp = T.export("minecraft-java", stop=False)
        live.assert_called_once()
        self.assertIn("minecraft-java", self.active)
        self.assertFalse(T.is_moving("minecraft-java"))

    def test_move_keeps_name_port_and_memory(self):
        self.server()
        exp, imp, result = self.move_to_b()
        self.assertEqual((result["name"], result["port"], result["renamed"]), ("minecraft-java", 25570, False))
        d = os.path.join(self.opt["B"], "minecraft-java")
        self.assertTrue(os.path.isfile(os.path.join(d, "world", "region", "r.0.0.mca")))
        self.assertIn("-Xmx6G", open(M._unit_path("minecraft-java")).read())
        self.assertIn("minecraft-java", self.active, "it was running, so it runs on B")
        self.assertFalse(os.path.exists(T.archive_path(imp["id"])), "the archive is deleted once installed")
        T.finish(exp["id"], "moved", "minecraft-java on Basement")
        self.assertEqual(self.removed, [("minecraft-java", True, {"moved_to": "minecraft-java on Basement",
                                                                   "moved_at": mock.ANY})])
        self.assertFalse(T.is_moving("minecraft-java"))

    def test_a_taken_name_gets_another(self):
        self.server()
        os.makedirs(os.path.join(self.opt["B"], "minecraft-java"))
        _, _, result = self.move_to_b()
        self.assertEqual(result["name"], "minecraft-java-2")
        self.assertTrue(result["renamed"])

    def test_damaged_or_oversized_archives_are_refused(self):
        self.server()
        exp = T.export("minecraft-java", stop=False)
        data = open(T.archive_path(exp["id"]), "rb").read()
        imp = T.begin_import(exp["size"], exp["sha256"], exp["meta"])
        with self.assertRaises(T.TransferError):
            T.receive(imp["id"], [data[:-1] + b"?"])
        with self.assertRaises(T.TransferError):
            T.receive(imp["id"], [data, b"more"])
        with self.assertRaises(T.TransferError):
            T.install(imp["id"])
        for bad in ((0, exp["sha256"]), (10, "x" * 64)):
            with self.assertRaises(T.TransferError):
                T.begin_import(bad[0], bad[1], exp["meta"])
        with self.assertRaises(T.TransferError):
            T.begin_import(10, exp["sha256"], {"edition": "forge"})

    def test_not_enough_disk(self):
        with mock.patch.object(T.shutil, "disk_usage", return_value=mock.Mock(free=1000)):
            with self.assertRaises(T.TransferError) as e:
                T.begin_import(800, "a" * 64, {"edition": "java"})
        self.assertEqual(e.exception.status, 507)

    def test_a_failed_install_removes_what_it_made(self):
        self.server()
        discarded = []
        with mock.patch.object(T, "_stays_running", side_effect=T.TransferError("crashed")), \
                mock.patch.object(M, "discard_server", discarded.append):
            with self.assertRaises(T.TransferError):
                self.move_to_b()
        self.assertEqual(discarded, ["minecraft-java"])

    def test_a_failed_move_starts_the_server_again(self):
        self.server()
        exp = T.export("minecraft-java", stop=True)
        T.finish(exp["id"], "failed")
        self.assertIn("minecraft-java", self.active)
        self.assertFalse(T.is_moving("minecraft-java"))
        self.assertEqual(self.removed, [])

    def test_only_moved_servers_can_be_deleted_by_the_master(self):
        listed = [{"name": "minecraft-java", "moved_to": None}, {"name": "minecraft-java-2", "moved_to": "x on B"}]
        deleted = []
        with mock.patch.object(M, "list_removed_servers", lambda: listed), \
                mock.patch.object(M, "delete_removed_server", deleted.append):
            with self.assertRaises(T.TransferError):
                T.delete_moved("minecraft-java")
            T.delete_moved("minecraft-java-2")
        self.assertEqual(deleted, ["minecraft-java-2"])

    def test_stale_transfers_never_delete_or_start_servers(self):
        self.server()
        exp = T.export("minecraft-java", stop=True)
        discarded = []
        with mock.patch.object(M, "discard_server", discarded.append):
            T.cleanup_stale(now=time.time() + T.STALE + 1)
        self.assertFalse(os.path.exists(T.archive_path(exp["id"])))
        self.assertNotIn("minecraft-java", self.active)
        self.assertFalse(T.is_moving("minecraft-java"))
        self.assertEqual(discarded, [])

    def test_carried_schedules(self):
        self.server()
        meta_backup = {"enabled": True, "frequency": "daily", "time": "03:00", "keep": 5, "keep_days": 30}
        set_b, set_r = [], []
        with mock.patch.object(backups, "get_schedule", return_value=meta_backup), \
                mock.patch.object(backups, "set_schedule", lambda *a: set_b.append(a)), \
                mock.patch.object(T.restarts, "set_schedule", lambda *a: set_r.append(a)):
            self.move_to_b()
        self.assertEqual(set_b, [("minecraft-java", True, "daily", "03:00", 5, 30)])
        self.assertEqual(set_r, [], "restarts weren't scheduled")


class Ends:
    """A stand-in member for the master's job."""
    def __init__(self, id, name, fail=None, system="linux"):
        self.id, self.name, self.fail, self.calls = id, name, fail or {}, []
        self.system, self.for_windows = system, []

    def _do(self, what, *args):
        self.calls.append((what,) + args)
        if what in self.fail:
            raise T.TransferError(self.fail[what])

    def export(self, server, stop, for_windows=False):
        self.for_windows.append(for_windows)
        self._do("export", server, stop)
        return {"id": "e" * 32, "size": 6, "sha256": "f" * 64, "was_running": True, "meta": {"edition": "java"}}

    def chunks(self, tid):
        yield b"abc"
        yield b"def"

    def finish(self, tid, outcome, moved_to=None):
        self._do("finish", outcome, moved_to)

    def begin_import(self, size, sha, meta):
        self._do("begin_import", size)
        return {"id": "i" * 32}

    def upload(self, tid, chunks, size):
        self._do("upload", b"".join(chunks))

    def install(self, tid, keep_name, start, carry, actor):
        self._do("install", keep_name, start, carry)
        return {"name": "minecraft-java", "port": 25565, "old_port": None, "renamed": False}

    def confirm(self, tid):
        self._do("confirm")

    def abort(self, tid):
        self._do("abort")


class Job(unittest.TestCase):
    def run_job(self, kind, a, b, start_copy=False):
        job = {"id": "j", "kind": kind, "server": "minecraft-java", "actor": "alex", "bytes": 0, "total": None,
               "state": "running", "from": {"id": a.id}, "to": {"id": b.id}}
        with mock.patch.object(moves.manager, "write_log", lambda m: None), \
                mock.patch.object(moves.cluster_client, "forget_cached", lambda *a: None):
            moves._run(job, a, b, start_copy)
        return job

    def test_a_move_in_order(self):
        a, b = Ends("local", "Main"), Ends("n1", "Basement")
        job = self.run_job("move", a, b)
        self.assertEqual(job["state"], "done", job.get("error"))
        self.assertEqual(job["bytes"], 6)
        self.assertEqual([c[0] for c in a.calls], ["export", "finish"])
        self.assertEqual(a.calls[1], ("finish", "moved", "minecraft-java on Basement"))
        self.assertEqual(b.calls, [("begin_import", 6), ("upload", b"abcdef"), ("install", True, True, True),
                                   ("confirm",)])

    def test_a_copy_to_the_same_member_gets_a_new_name(self):
        a, b = Ends("n1", "Basement"), Ends("n1", "Basement")
        job = self.run_job("copy", a, b, start_copy=False)
        self.assertEqual(job["state"], "done")
        self.assertIn(("install", False, False, False), b.calls)
        self.assertEqual(a.calls[1], ("finish", "copied", None))

    def test_a_failed_install_rolls_back_both(self):
        a, b = Ends("local", "Main"), Ends("n1", "Basement", fail={"install": "Java 21 isn't available"})
        job = self.run_job("move", a, b)
        self.assertEqual(job["state"], "failed")
        self.assertIn("Java 21", job["error"])
        self.assertEqual(b.calls[-1], ("abort",))
        self.assertEqual(a.calls[-1], ("finish", "failed", None))

    def test_if_a_cant_let_go_b_keeps_it(self):
        a, b = Ends("local", "Main", fail={"finish": "disk error"}), Ends("n1", "Basement")
        job = self.run_job("move", a, b)
        self.assertEqual(job["state"], "done")
        self.assertIn("uninstall it on Main yourself", job["warning"])
        self.assertEqual(b.calls[-1], ("confirm",))

    def test_to_windows_the_source_checks_names_first(self):
        """1.24: a move to a Windows member asks the source to refuse names Windows can't hold."""
        a, b = Ends("local", "Main"), Ends("n1", "Gaming PC", system="windows")
        job = self.run_job("move", a, b)
        self.assertEqual(job["state"], "done", job.get("error"))
        self.assertEqual(a.for_windows, [True])
        a, b = Ends("n1", "Gaming PC", system="windows"), Ends("local", "Main")
        self.run_job("move", a, b)
        self.assertEqual(a.for_windows, [False])

    def test_a_refusal_from_the_source_fails_the_job_before_anything_is_sent(self):
        a = Ends("local", "Main", fail={"export": "2 file names in it can't be kept on Windows: a:b"})
        b = Ends("n1", "Gaming PC", system="windows")
        job = self.run_job("move", a, b)
        self.assertEqual(job["state"], "failed")
        self.assertIn("can't be kept on Windows", job["error"])
        self.assertEqual(b.calls, [], "nothing was sent")

    def test_refusals(self):
        with self.assertRaises(moves.MoveError):
            moves.start("move", "local", "minecraft-java", "local")
        with self.assertRaises(moves.MoveError):
            moves.start("teleport", "local", "minecraft-java", "n1")
        old = {"id": "n1", "name": "Basement", "cluster_api": 2, "version": "1.15.0"}
        with mock.patch.object(moves.cluster, "find_node", lambda i: dict(old)), \
                mock.patch.object(moves.cluster_client, "_hello_fields", lambda n: {}):
            with self.assertRaises(moves.MoveError) as e:
                moves.member("n1", "alex", "admin")
        self.assertIn("1.16", str(e.exception))


@unittest.skipUnless(HAVE_FASTAPI, "needs fastapi")
class Routes(unittest.TestCase):
    def setUp(self):
        import main
        self.main = main
        self.user = {"username": "alex", "role": "user", "source": "panel"}
        for p in (mock.patch.object(main.auth, "read_session_token", lambda t: dict(self.user)),
                  mock.patch.object(main.cluster, "role", lambda: "master")):
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(main.app)
        self.client.cookies.set(main.auth.SESSION_COOKIE, "session")

    def test_browsers_never_reach_the_node_side(self):
        self.user["role"] = "admin"
        r = self.client.post("/api/cluster/transfers/export", json={"server": "minecraft-java", "stop": True})
        self.assertEqual(r.status_code, 403)

    def test_only_admins_move(self):
        r = self.client.post("/api/cluster/moves", json={"kind": "move", "server": "minecraft-java", "to_node": "n1"})
        self.assertEqual(r.status_code, 403)
        r = self.client.delete("/api/cluster/moved/n1/minecraft-java")
        self.assertEqual(r.status_code, 403)
        # 1.18: the list of moves names every server, so it's for admins too
        self.assertEqual(self.client.get("/api/cluster/moves").status_code, 403)
        self.user["role"] = "admin"
        self.assertEqual(self.client.get("/api/cluster/moves").status_code, 200)


if __name__ == "__main__":
    unittest.main()
