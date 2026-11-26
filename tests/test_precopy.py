"""1.16.13: moving a big server with a pre-copy (core/transfer.py, core/moves.py) and finding
panels on the network (core/discovery.py). Run: python -m unittest discover -s tests"""
import http.server
import io
import json
import os
import sys
import tarfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from test_move import HAVE_FASTAPI, Ends, Member  # noqa: E402
from core import cluster, discovery, moves, transfer as T  # noqa: E402


def tree(d):
    """{relative path: bytes, or 'dir', or '-> target'} for comparing two server folders."""
    out = {}
    for root, dirs, files in os.walk(d):
        for name in dirs + files:
            p = os.path.join(root, name)
            rel = os.path.relpath(p, d)
            if os.path.islink(p):
                out[rel] = "-> " + os.readlink(p)
            elif os.path.isdir(p):
                out[rel] = "dir"
            else:
                out[rel] = open(p, "rb").read()
    return out


class PreCopy(Member):
    def precopy_to_b(self, name="minecraft-java"):
        pre = T.export(name, False, precopy=True)
        data = open(T.archive_path(pre["id"]), "rb").read()
        self.on("B")
        pimp = T.begin_import(pre["size"], pre["sha256"], pre["meta"])
        T.receive(pimp["id"], [data])
        T.stage(pimp["id"])
        self.on("A")
        return pre, pimp

    def finish_on_b(self, pre, pimp, name="minecraft-java"):
        exp = T.export(name, True, base=pre["id"])
        data = open(T.archive_path(exp["id"]), "rb").read()
        self.on("B")
        imp = T.begin_import(exp["size"], exp["sha256"], exp["meta"], base=pimp["id"])
        T.receive(imp["id"], [data])
        return exp, imp, T.install(imp["id"])

    def test_only_what_changed_is_sent_after_the_stop(self):
        d = self.server()
        os.makedirs(os.path.join(d, "plugins", "old"))
        open(os.path.join(d, "plugins", "old", "a.yml"), "w").write("a")
        open(os.path.join(d, "becomes-a-folder"), "w").write("file")
        with mock.patch.object(T, "RECENT", -1e12):       # timestamps here are fine-grained enough
            pre, pimp = self.precopy_to_b()
            self.assertIn("minecraft-java", self.active, "a pre-copy never stops the server")
            self.assertFalse(T.is_moving("minecraft-java"), "and it can still be started and stopped")
            staged = T._load(pimp["id"])
            self.assertEqual(staged["state"], "staged")
            self.assertTrue(os.path.basename(staged["staging"]).startswith(".redstonepanel-import-"))
            self.assertFalse(os.path.exists(T.archive_path(pimp["id"])), "unpacked, so the archive goes")

            # The world keeps changing while the pre-copy travels
            region = os.path.join(d, "world", "region", "r.0.0.mca")
            open(region, "r+b").write(b"Y" * 5000)        # same size, new content
            open(os.path.join(d, "world", "region", "r.1.0.mca"), "wb").write(b"new")
            os.remove(os.path.join(d, "logs", "latest.log"))
            os.remove(os.path.join(d, "plugins", "old", "a.yml"))
            os.rmdir(os.path.join(d, "plugins", "old"))
            os.remove(os.path.join(d, "becomes-a-folder"))
            os.makedirs(os.path.join(d, "becomes-a-folder"))
            open(os.path.join(d, "becomes-a-folder", "x"), "w").write("x")
            os.symlink("server.jar", os.path.join(d, "link.jar"))
            expected = tree(d)

            exp, imp, result = self.finish_on_b(pre, pimp)
        self.assertTrue(exp["was_running"])
        self.on("A")
        for p in (T.archive_path(pre["id"]), T.list_path(pre["id"]), T._rec_path(pre["id"])):
            self.assertFalse(os.path.exists(p), "A forgets the pre-copy once the changes are packed")
        self.on("B")
        got = tree(os.path.join(self.opt["B"], "minecraft-java"))
        for extra in ("eula.txt",):                   # added by every import
            got.pop(extra, None)
        expected.pop(M_REMOVED(), None)                # never moved, as before
        self.assertEqual(got, expected, "B ends up with exactly A's folder")
        self.assertFalse(os.path.exists(staged["staging"]), "the staging folder is gone")
        self.assertEqual(result["name"], "minecraft-java")

    def test_the_second_archive_skips_unchanged_files(self):
        d = self.server()
        with mock.patch.object(T, "RECENT", -1e12):
            pre, _ = self.precopy_to_b()
            open(os.path.join(d, "logs", "latest.log"), "ab").write(b" more")
            exp = T.export("minecraft-java", True, base=pre["id"])
        with tarfile.open(T.archive_path(exp["id"])) as t:
            files = {m.name for m in t.getmembers() if not m.isdir()}
            listed = json.load(t.extractfile(T.DELTA_LIST))
        self.assertEqual(files, {"logs/latest.log", T.DELTA_LIST})
        self.assertEqual(listed, {"deleted": []})
        self.assertNotIn("minecraft-java", self.active, "the second part stops it")

    def test_files_changed_around_the_pre_copy_count_as_changed(self):
        d = self.server()
        st = os.lstat(os.path.join(d, "server.jar"))
        now = time.time()
        self.assertFalse(T.unchanged(["f", now] + T._stamp(st), st), "written just before being listed")
        self.assertTrue(T.unchanged(["f", now + 3600] + T._stamp(st), st))
        self.assertFalse(T.unchanged(["f", now + 3600], st), "a file that shrank while read")
        self.assertFalse(T.unchanged(["d", now + 3600], st))

    def test_a_file_that_shrinks_while_read_keeps_the_archive_valid(self):
        src = T._Exact(io.BytesIO(b"abc"), 6)
        self.assertEqual(src.read(4) + src.read(10), b"abc\0\0\0")
        self.assertTrue(src.short)

    def test_the_changes_cant_reach_outside_the_staged_folder(self):
        outside = os.path.join(self.root, "outside")
        os.makedirs(outside)
        open(os.path.join(outside, "secret"), "w").write("keep")
        staged = os.path.join(self.root, "staged")
        os.makedirs(staged)
        os.symlink(outside, os.path.join(staged, "link"))

        def delta(deleted, files=()):
            path = os.path.join(self.root, "delta.tar.gz")
            with tarfile.open(path, "w:gz") as t:
                for name, data in list(files) + [(T.DELTA_LIST, json.dumps({"deleted": deleted}).encode())]:
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    t.addfile(info, io.BytesIO(data))
            return path
        T.apply_changes(delta(["link/secret", "../outside/secret", "/etc/passwd"]), staged)
        self.assertTrue(os.path.exists(os.path.join(outside, "secret")))
        with self.assertRaises(T.TransferError):
            T.apply_changes(delta([], [("link/evil", b"x")]), staged)
        self.assertFalse(os.path.exists(os.path.join(outside, "evil")))
        # a link in the way of a file is replaced, never written through
        T.apply_changes(delta([], [("link", b"now a file")]), staged)
        self.assertEqual(open(os.path.join(staged, "link"), "rb").read(), b"now a file")
        self.assertEqual(open(os.path.join(outside, "secret")).read(), "keep")
        with self.assertRaises(T.TransferError):
            T.apply_changes(_no_list(self.root), staged)

    def test_cancelling_removes_the_staged_copy(self):
        self.server()
        pre, pimp = self.precopy_to_b()
        self.on("B")
        staging = T._load(pimp["id"])["staging"]
        T.abort(pimp["id"])
        self.assertFalse(os.path.exists(staging))
        self.on("A")
        T.finish(pre["id"], "copied")
        self.assertFalse(os.path.exists(T.list_path(pre["id"])))
        self.assertIn("minecraft-java", self.active)

    def test_refusals(self):
        self.server()
        with self.assertRaises(T.TransferError):
            T.export("minecraft-java", True, precopy=True)
        plain = T.export("minecraft-java", False)
        with self.assertRaises(T.TransferError):
            T.export("minecraft-java", True, base=plain["id"])          # not a pre-copy
        with self.assertRaises(T.TransferError):
            T.begin_import(10, "a" * 64, {"edition": "java"}, base="b" * 32)


def M_REMOVED():
    from core import manager
    return manager.REMOVED_INFO


def _no_list(root):
    path = os.path.join(root, "nolist.tar.gz")
    with tarfile.open(path, "w:gz") as t:
        info = tarfile.TarInfo("a")
        t.addfile(info, io.BytesIO(b""))
    return path


class PreEnds(Ends):
    """A stand-in member that knows pre-copies."""
    def export(self, server, stop, precopy=False, base=None, for_windows=False):
        self.for_windows.append(for_windows)
        self._do("export", server, stop, precopy, base)
        if "final" in self.fail and base:
            raise T.TransferError(self.fail["final"])
        return {"id": ("p" if precopy else "e") * 32, "size": 6, "sha256": "f" * 64, "was_running": True,
                "meta": {"edition": "java"}}

    def begin_import(self, size, sha, meta, base=None):
        self._do("begin_import", size, base)
        return {"id": ("s" if base is None else "i") * 32}

    def stage(self, tid):
        self._do("stage", tid)

    def abort(self, tid):
        self._do("abort", tid)

    def finish(self, tid, outcome, moved_to=None):
        self._do("finish", tid, outcome, moved_to)


class PreCopyJob(unittest.TestCase):
    def run_job(self, a, b):
        job = {"id": "j", "kind": "move", "server": "minecraft-java", "actor": "alex", "bytes": 0, "total": None,
               "state": "running", "from": {"id": a.id}, "to": {"id": b.id}, "precopy": True, "downtime": None}
        with mock.patch.object(moves.manager, "write_log", lambda m: None), \
                mock.patch.object(moves.cluster_client, "forget_cached", lambda *a: None):
            moves._run(job, a, b, False)
        return job

    def test_in_order(self):
        a, b = PreEnds("local", "Main"), PreEnds("n1", "Basement")
        job = self.run_job(a, b)
        self.assertEqual(job["state"], "done", job.get("error"))
        self.assertEqual(a.calls, [("export", "minecraft-java", False, True, None),
                                   ("export", "minecraft-java", True, False, "p" * 32),
                                   ("finish", "e" * 32, "moved", "minecraft-java on Basement")])
        self.assertEqual(b.calls, [("begin_import", 6, None), ("upload", b"abcdef"), ("stage", "s" * 32),
                                   ("begin_import", 6, "s" * 32), ("upload", b"abcdef"),
                                   ("install", True, True, True), ("confirm",)])
        self.assertIsInstance(job["downtime"], int)

    def test_to_windows_both_exports_check_names(self):
        """1.24: the pre-copy (before anything stops) and the final pass both check."""
        a, b = PreEnds("local", "Main"), PreEnds("n1", "Gaming PC", system="windows")
        job = self.run_job(a, b)
        self.assertEqual(job["state"], "done", job.get("error"))
        self.assertEqual(a.for_windows, [True, True])

    def test_a_failure_after_the_pre_copy_cleans_up_both(self):
        a, b = PreEnds("local", "Main", fail={"final": "disk full"}), PreEnds("n1", "Basement")
        job = self.run_job(a, b)
        self.assertEqual(job["state"], "failed")
        self.assertIn(("abort", "s" * 32), b.calls, "B drops the staged copy")
        self.assertEqual(a.calls[-1], ("finish", "p" * 32, "copied", None), "A drops the pre-copy")

    def test_a_failure_during_the_pre_copy_cleans_up(self):
        a, b = PreEnds("local", "Main"), PreEnds("n1", "Basement", fail={"stage": "unpack failed"})
        job = self.run_job(a, b)
        self.assertEqual(job["state"], "failed")
        self.assertEqual(b.calls[-1], ("abort", "s" * 32))
        self.assertEqual(a.calls[-1], ("finish", "p" * 32, "copied", None))
        self.assertFalse(any(c[0] == "export" and c[2] for c in a.calls), "the server was never stopped")

    def test_refusals(self):
        with self.assertRaises(moves.MoveError):
            moves.start("copy", "local", "minecraft-java", "n1", precopy=True)
        node = {"id": "n1", "name": "Basement", "cluster_api": 4, "version": "1.16.12"}
        with mock.patch.object(moves.cluster, "find_node", lambda i: dict(node)), \
                mock.patch.object(moves.cluster_client, "_hello_fields", lambda n: {}):
            moves.member("n1", "alex", "admin")                         # a plain move is fine
            with self.assertRaises(moves.MoveError) as e:
                moves.member("n1", "alex", "admin", moves.PRECOPY_API)
        self.assertIn("1.16.13", str(e.exception))

    def test_the_stage_route_is_cluster_plumbing(self):
        self.assertTrue(cluster.path_allowed("POST", "/api/cluster/transfers/" + "a" * 32 + "/stage"))
        self.assertGreaterEqual(cluster.CLUSTER_API, moves.PRECOPY_API)


IP_JSON = [
    {"ifname": "lo", "addr_info": [{"family": "inet", "local": "127.0.0.1", "prefixlen": 8}]},
    {"ifname": "eth0", "addr_info": [{"family": "inet", "local": "192.168.1.20", "prefixlen": 24}]},
    {"ifname": "wlan0", "addr_info": [{"family": "inet", "local": "10.4.7.9", "prefixlen": 16}]},
    {"ifname": "wan", "addr_info": [{"family": "inet", "local": "81.2.69.160", "prefixlen": 24}]},
    {"ifname": "ll", "addr_info": [{"family": "inet", "local": "169.254.3.3", "prefixlen": 16}]},
]


class Discovery(unittest.TestCase):
    def test_only_private_networks_each_at_most_a_slash_24(self):
        nets, own = discovery.networks(IP_JSON)
        self.assertEqual([str(n) for n in nets], ["192.168.1.0/24", "10.4.7.0/24"])
        self.assertIn("192.168.1.20", own)
        many = [{"addr_info": [{"local": f"192.168.{i}.2", "prefixlen": 24}]} for i in range(9)]
        self.assertEqual(len(discovery.networks(many)[0]), discovery.MAX_NETWORKS)

    def test_what_counts_as_a_panel(self):
        self.assertEqual(discovery.read_info(b'{"name": "Basement", "app": "RedstonePanel", "cluster": "standalone"}'),
                         {"name": "Basement", "cluster": "standalone"})
        self.assertEqual(discovery.read_info(b'{"name": "Old"}'), {"name": "Old", "cluster": None})
        for bad in (b'{"name": "x", "app": "Other"}', b"<html>", b"[]", b'{"name": 3}'):
            self.assertIsNone(discovery.read_info(bad))
        self.assertEqual(discovery.read_info(b'{"name": "a\\u0000b' + b"c" * 100 + b'", "app": "RedstonePanel"}')["name"],
                         "ab" + "c" * 58, "names are cleaned and capped")

    def test_find_marks_members_and_prefers_https(self):
        answers = {("192.168.1.30", "https"): {"name": "Basement", "cluster": "node"},
                   ("192.168.1.30", "http"): {"name": "Basement", "cluster": "node"},
                   ("192.168.1.7", "http"): {"name": "Attic", "cluster": "standalone"},
                   ("192.168.1.20", "https"): {"name": "Me", "cluster": "master"}}
        seen = []

        def probe(host, scheme, port):
            seen.append(host)
            info = answers.get((host, scheme))
            return dict(info, url=f"{scheme}://{host}:{port}") if info else None
        found = discovery.find([{"name": "Basement", "url": "https://192.168.1.30:8443"}],
                               ip_json=IP_JSON[:2], probe_fn=probe)
        self.assertEqual(found["networks"], ["192.168.1.0/24"])
        self.assertNotIn("192.168.1.20", seen, "never this machine")
        self.assertEqual([(p["name"], p["url"], p["node"]) for p in found["panels"]],
                         [("Attic", "http://192.168.1.7:8080", None),
                          ("Basement", "https://192.168.1.30:8443", "Basement")])

    def test_one_search_at_a_time_and_none_without_a_network(self):
        with self.assertRaises(discovery.DiscoveryError):
            discovery.find(ip_json=IP_JSON[:1], probe_fn=lambda *a: None)
        discovery._running.acquire()
        try:
            with self.assertRaises(discovery.DiscoveryError):
                discovery.find(ip_json=IP_JSON[:2], probe_fn=lambda *a: None)
        finally:
            discovery._running.release()

    def test_probe_reads_a_real_answer(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = (json.dumps({"name": "Shed", "app": "RedstonePanel", "cluster": "standalone"}).encode()
                        if self.path == "/api/panel-info" else b"no")
                self.send_response(200 if self.path == "/api/panel-info" else 404)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        port = srv.server_address[1]
        self.assertEqual(discovery.probe("127.0.0.1", "http", port),
                         {"name": "Shed", "cluster": "standalone", "url": f"http://127.0.0.1:{port}"})
        self.assertIsNone(discovery.probe("127.0.0.1", "https", port, timeout=2), "not HTTPS there")
        srv.shutdown()
        self.assertIsNone(discovery.probe("127.0.0.1", "http", port, timeout=1), "nobody there")


@unittest.skipUnless(HAVE_FASTAPI, "needs fastapi")
class Routes(unittest.TestCase):
    def setUp(self):
        import main
        self.main = main
        self.user = {"username": "alex", "role": "admin", "source": "panel"}
        self.role = "master"
        for p in (mock.patch.object(main.auth, "read_session_token", lambda t: dict(self.user)),
                  mock.patch.object(main.cluster, "role", lambda: self.role),
                  mock.patch.object(main.cluster, "load_nodes", lambda: []),
                  mock.patch.object(main.manager, "write_log", lambda m: None)):
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(main.app)
        self.client.cookies.set(main.auth.SESSION_COOKIE, "session")

    def test_panel_info_says_it_is_a_redstonepanel(self):
        d = TestClient(self.main.app).get("/api/panel-info").json()      # no sign-in needed
        self.assertEqual((d["app"], d["cluster"]), ("RedstonePanel", "master"))
        self.assertEqual(set(d), {"name", "app", "cluster"}, "and nothing more")

    def test_only_a_masters_admins_search(self):
        found = {"networks": ["192.168.1.0/24"], "panels": [], "seconds": 0.1}
        with mock.patch.object(discovery, "find", return_value=found):
            self.assertEqual(self.client.post("/api/settings/cluster/discover").json(), found)
            self.role = "standalone"
            self.assertEqual(self.client.post("/api/settings/cluster/discover").status_code, 400)
            self.role = "master"
            self.user["role"] = "user"
            self.assertEqual(self.client.post("/api/settings/cluster/discover").status_code, 403)

    def test_the_move_route_takes_a_pre_copy(self):
        with mock.patch.object(moves, "start", return_value={"id": "j"}) as start:
            r = self.client.post("/api/cluster/moves", json={"kind": "move", "server": "minecraft-java",
                                                              "to_node": "n1", "precopy": True})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(start.call_args[0][-1])


if HAVE_FASTAPI:
    from fastapi.testclient import TestClient  # noqa: E402

if __name__ == "__main__":
    unittest.main()
