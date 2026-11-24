"""system.game_files(): the panel (root) touches files in a game server's folder with the game
account's rights, so a link the game left there can't reach anything the game itself can't.
These run only as root on Linux with a 'minecraft' account (the install test's container, a
real install); elsewhere game_files() does nothing and they're skipped.
Run: python -m unittest discover -s tests
"""
import os
import pwd
import shutil
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.platform.linux import system  # noqa: E402


def _game_account():
    try:
        return pwd.getpwnam(system.GAME_ACCOUNT)
    except KeyError:
        return None


@unittest.skipUnless(sys.platform.startswith("linux") and hasattr(os, "geteuid") and os.geteuid() == 0
                     and _game_account(), "needs root and a minecraft account")
class GameFiles(unittest.TestCase):
    def setUp(self):
        self.pw = _game_account()
        # A root-only secret, and a server folder the game account owns with a link to it
        self.secret_dir = tempfile.mkdtemp(prefix="rp-secret-")
        os.chmod(self.secret_dir, 0o700)
        self.secret = os.path.join(self.secret_dir, "shadow")
        with open(self.secret, "w") as f:
            f.write("root only\n")
        os.chmod(self.secret, 0o600)
        self.server = tempfile.mkdtemp(prefix="rp-server-")
        os.chmod(self.server, 0o755)
        os.chown(self.server, self.pw.pw_uid, self.pw.pw_gid)
        os.symlink(self.secret, os.path.join(self.server, "server.properties"))
        self.addCleanup(shutil.rmtree, self.secret_dir, True)
        self.addCleanup(shutil.rmtree, self.server, True)

    def test_a_link_to_a_root_file_is_neither_written_nor_read(self):
        link = os.path.join(self.server, "server.properties")
        with system.game_files():
            with self.assertRaises(PermissionError):
                open(link, "w")
            with self.assertRaises(PermissionError):
                open(link).read()
        with open(self.secret) as f:
            self.assertEqual(f.read(), "root only\n")

    def test_new_files_belong_to_the_game_account_and_root_comes_back(self):
        with system.game_files():
            with system.game_files():                 # nests
                with open(os.path.join(self.server, "eula.txt"), "w") as f:
                    f.write("eula=true\n")
            # its own log and data stay writable for the panel
            with system.panel_files(), open(self.secret, "a") as f:
                f.write("log line\n")
            with self.assertRaises(PermissionError):
                open(self.secret, "a")
        st = os.stat(os.path.join(self.server, "eula.txt"))
        self.assertEqual((st.st_uid, st.st_gid), (self.pw.pw_uid, self.pw.pw_gid))
        with open(self.secret, "a") as f:                # root again
            f.write("after\n")

    def test_other_threads_keep_root(self):
        seen = {}
        inside, done = threading.Event(), threading.Event()

        def other():
            inside.wait(5)
            try:
                with open(self.secret) as f:
                    seen["read"] = f.read()
            except OSError as e:
                seen["read"] = e
            done.set()

        t = threading.Thread(target=other)
        t.start()
        with system.game_files():
            inside.set()
            done.wait(5)
        t.join(5)
        self.assertEqual(seen["read"], "root only\n")

    def test_a_server_that_runs_as_root_is_handled_as_root(self):
        # its unit has no User=: the server has root's rights itself, so its folder is root's to
        # change (a hand-made server, or one from another tool), and the panel doesn't refuse it
        with system.server_account(""), system.game_files(), open(self.secret, "a") as f:
            f.write("root server\n")
        with system.server_account("minecraft"), system.game_files():
            with self.assertRaises(PermissionError):
                open(self.secret, "a")
        with system.server_account("no-such-account-x"), system.game_files():
            with self.assertRaises(PermissionError, msg="an unknown account falls back to the game account"):
                open(self.secret, "a")
        with system.server_account(None), system.game_files():
            with self.assertRaises(PermissionError):
                open(self.secret, "a")

    def test_make_executable_refuses_a_link(self):
        target = os.path.join(self.server, "bedrock_server")
        os.symlink(self.secret, target)
        self.assertEqual(system.make_executable(target), 1)
        self.assertEqual(os.stat(self.secret).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
