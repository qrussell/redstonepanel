"""Tests for core/files.py, the file manager. Linux only (openat via dir_fd).
The server folder is a temp folder; the tests plant symlinks and hard links that try to reach
files outside it. Run: python -m unittest discover -s tests"""
import os
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-files-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import files as F, manager as M  # noqa: E402

LINUX = sys.platform.startswith("linux")


@unittest.skipUnless(LINUX, "needs Linux (openat)")
class Files(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(dir=TMP)
        self.server = os.path.join(self.root, "minecraft-java")
        self.outside = os.path.join(self.root, "outside")
        os.makedirs(os.path.join(self.server, "plugins"))
        os.makedirs(self.outside)
        self.secret = os.path.join(self.outside, "secret.txt")
        self.write(self.secret, "root only")
        self.write(os.path.join(self.server, "server.properties"), "motd=hi\n")
        for p in (mock.patch.object(M, "_check_server", lambda s: None),
                  mock.patch.object(M, "get_server_dir", lambda s: self.server),
                  mock.patch.object(M, "write_log", lambda m: None)):
            p.start()
            self.addCleanup(p.stop)

    def write(self, path, text):
        with open(path, "w") as f:
            f.write(text)

    def read(self, path):
        with open(path) as f:
            return f.read()

    def refused(self, fn, *args, status=None):
        with self.assertRaises(F.FileError) as cm:
            fn("minecraft-java", *args)
        if status:
            self.assertEqual(cm.exception.status, status)
        return str(cm.exception)

    # --- basics ---
    def test_list_read_write(self):
        names = [e["name"] for e in F.list_dir("minecraft-java")["entries"]]
        self.assertEqual(names, ["plugins", "server.properties"])          # folders first
        r = F.read_text("minecraft-java", "server.properties")
        self.assertEqual(r["content"], "motd=hi\n")
        F.write_text("minecraft-java", "server.properties", "motd=bye\n", r["modified"])
        self.assertEqual(self.read(os.path.join(self.server, "server.properties")), "motd=bye\n")
        F.write_text("minecraft-java", "plugins/config.yml", "a: 1\n")
        self.assertEqual(self.read(os.path.join(self.server, "plugins", "config.yml")), "a: 1\n")
        self.assertFalse([n for n in os.listdir(self.server) if n.endswith(".tmp")])

    def test_keeps_the_file_mode(self):
        p = os.path.join(self.server, "start.sh")
        self.write(p, "echo hi\n")
        os.chmod(p, 0o750)
        F.write_text("minecraft-java", "start.sh", "echo bye\n")
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o750)

    def test_edit_conflict(self):
        r = F.read_text("minecraft-java", "server.properties")
        os.utime(os.path.join(self.server, "server.properties"), (time.time() + 5, time.time() + 5))
        self.refused(F.write_text, "server.properties", "x", r["modified"], status=409)

    def test_mkdir_rename_delete(self):
        F.make_dir("minecraft-java", "plugins/Essentials")
        F.write_text("minecraft-java", "plugins/Essentials/config.yml", "x")
        F.rename("minecraft-java", "plugins/Essentials", "EssentialsX")
        self.assertTrue(os.path.isfile(os.path.join(self.server, "plugins", "EssentialsX", "config.yml")))
        self.refused(F.rename, "server.properties", "plugins", status=409)
        F.delete("minecraft-java", "plugins/EssentialsX")
        self.assertFalse(os.path.exists(os.path.join(self.server, "plugins", "EssentialsX")))
        self.refused(F.delete, "", )

    def test_upload(self):
        F.upload("minecraft-java", "plugins", "x.jar", [b"PK", b"\x03\x04"])
        self.assertEqual(os.path.getsize(os.path.join(self.server, "plugins", "x.jar")), 4)
        self.refused(F.upload, "plugins", "x.jar", [b"new"], status=409)
        F.upload("minecraft-java", "plugins", "x.jar", [b"new"], overwrite=True)
        self.assertEqual(self.read(os.path.join(self.server, "plugins", "x.jar")), "new")
        for bad in ("../x.jar", "a/b.jar", "..", ""):
            self.refused(F.upload, "plugins", bad, [b"x"])

    def test_limits(self):
        with mock.patch.object(F, "MAX_TEXT", 10):
            self.write(os.path.join(self.server, "big.txt"), "x" * 11)
            self.assertIn("downloaded", self.refused(F.read_text, "big.txt"))
            self.refused(F.write_text, "big.txt", "y" * 11, status=413)
        with mock.patch.object(F, "MAX_UPLOAD", 5):
            self.refused(F.upload, "", "big.bin", [b"123", b"456"], status=413)
        self.assertFalse(os.path.exists(os.path.join(self.server, "big.bin")))

    def test_binary_and_non_utf8_are_download_only(self):
        with open(os.path.join(self.server, "level.dat"), "wb") as f:
            f.write(b"\x1f\x8b\x00\x00")
        self.assertIn("binary", self.refused(F.read_text, "level.dat"))
        with open(os.path.join(self.server, "latin1.txt"), "wb") as f:
            f.write("caf\xe9".encode("latin-1"))
        self.assertIn("UTF-8", self.refused(F.read_text, "latin1.txt"))

    # --- escaping the folder ---
    def test_dot_dot_and_absolute_paths(self):
        for p in ("../outside/secret.txt", "plugins/../../outside/secret.txt", "./server.properties",
                  "plugins//x", "a\x00b"):
            self.refused(F.read_text, p)
        # a leading slash means the server folder, never /
        self.assertEqual(F.read_text("minecraft-java", "/server.properties")["content"], "motd=hi\n")

    def test_symlinked_file_is_not_followed(self):
        os.symlink(self.secret, os.path.join(self.server, "secret.txt"))
        self.assertIn("symlink", self.refused(F.read_text, "secret.txt"))
        self.refused(F.open_for_download, "secret.txt")
        self.refused(F.write_text, "secret.txt", "pwned")
        self.refused(F.upload, "", "secret.txt", [b"pwned"], True)
        self.assertEqual(self.read(self.secret), "root only")
        F.delete("minecraft-java", "secret.txt")                  # removes the link only
        self.assertEqual(self.read(self.secret), "root only")

    def test_symlinked_folder_is_not_followed(self):
        os.symlink(self.outside, os.path.join(self.server, "world"))
        entry = next(e for e in F.list_dir("minecraft-java")["entries"] if e["name"] == "world")
        self.assertEqual((entry["type"], entry["target"]), ("link", self.outside))
        self.refused(F.list_dir, "world")
        self.refused(F.read_text, "world/secret.txt")
        self.refused(F.write_text, "world/new.txt", "x")
        self.refused(F.upload, "world", "new.txt", [b"x"])
        self.refused(F.make_dir, "world/sub")
        self.refused(F.delete, "world/secret.txt")
        self.assertEqual(os.listdir(self.outside), ["secret.txt"])

    def test_deleting_a_folder_keeps_what_its_symlinks_point_to(self):
        os.makedirs(os.path.join(self.server, "old"))
        os.symlink(self.outside, os.path.join(self.server, "old", "link"))
        F.delete("minecraft-java", "old")
        self.assertEqual(self.read(self.secret), "root only")

    def test_symlink_swapped_in_after_listing(self):
        # The folder is checked when it's opened; a symlink put there later is still refused
        os.rename(os.path.join(self.server, "plugins"), os.path.join(self.server, "plugins.real"))
        os.symlink(self.outside, os.path.join(self.server, "plugins"))
        self.refused(F.upload, "plugins", "secret.txt", [b"pwned"], True)
        self.assertEqual(self.read(self.secret), "root only")

    def test_hard_link_is_refused(self):
        os.link(self.secret, os.path.join(self.server, "hard.txt"))
        self.assertIn("hard links", self.refused(F.read_text, "hard.txt"))
        self.refused(F.write_text, "hard.txt", "pwned")
        self.assertEqual(self.read(self.secret), "root only")

    def test_fifo_is_not_opened(self):
        os.mkfifo(os.path.join(self.server, "pipe"))
        self.refused(F.read_text, "pipe")                       # doesn't hang
        self.assertEqual(next(e for e in F.list_dir("minecraft-java")["entries"] if e["name"] == "pipe")["type"],
                         "other")


try:
    import fastapi  # noqa: F401
    HAVE_FASTAPI = True
except ImportError:
    HAVE_FASTAPI = False


@unittest.skipUnless(HAVE_FASTAPI, "needs fastapi")
class AdminOnly(unittest.TestCase):
    def test_users_cannot_use_the_file_manager(self):
        import main
        req = SimpleNamespace(state=SimpleNamespace(user={"username": "alex", "role": "user", "source": "panel"}))
        calls = [lambda: main.list_files(req, "minecraft-java", ""),
                 lambda: main.read_text_file(req, "minecraft-java", "server.properties"),
                 lambda: main.write_text_file(req, "minecraft-java", main.TextFile(path="x", content="y")),
                 lambda: main.download_file(req, "minecraft-java", "server.properties"),
                 lambda: main.delete_file(req, "minecraft-java", "x"),
                 lambda: main.make_folder(req, "minecraft-java", main.FilePath(path="x")),
                 lambda: main.rename_file(req, "minecraft-java", main.FileRename(path="x", name="y"))]
        for call in calls:
            with self.assertRaises(main.HTTPException) as cm:
                call()
            self.assertEqual(cm.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
