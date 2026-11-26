"""1.24: the first sign-in link the Windows installer opens (choose your own password).
Run: python -m unittest discover -s tests
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-first-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import firstsignin, twofactor  # noqa: E402

try:
    import fastapi  # noqa: F401
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except (ImportError, RuntimeError):
    HAVE_FASTAPI = False


@unittest.skipUnless(HAVE_FASTAPI, "needs FastAPI")
class FirstSignIn(unittest.TestCase):
    RANDOM = "abc123-def456-789abc"

    def setUp(self):
        import main
        self.main, self.auth = main, main.auth
        self.home = tempfile.mkdtemp(dir=TMP)
        d = os.path.join(self.home, "data")
        os.makedirs(d)
        self.log = []
        for p in (mock.patch.object(firstsignin, "FIRST_FILE", os.path.join(d, "first-sign-in.json")),
                  mock.patch.object(firstsignin, "_home", lambda: self.home),
                  mock.patch.object(main.auth, "USERS_FILE", os.path.join(d, "users.json")),
                  mock.patch.object(main.auth, "SECRET_FILE", os.path.join(d, "secret.key")),
                  mock.patch.object(main.auth, "_secret", None),
                  mock.patch.dict(main.auth._fails, clear=True),
                  mock.patch.object(twofactor, "TOTP_FILE", os.path.join(d, "totp.json")),
                  mock.patch.dict(twofactor._cache, {"mtime": None, "data": None}),
                  mock.patch.object(main.manager, "write_log", self.log.append)):
            p.start()
            self.addCleanup(p.stop)
        # What setup.py does on a first install
        self.auth.create_panel_user("admin", self.RANDOM, "admin")
        with open(os.path.join(self.home, "first-sign-in.txt"), "w") as f:
            f.write(f"Password:  {self.RANDOM}\n")
        self.token = firstsignin.create("admin")
        firstsignin.write_open_link(f"http://localhost:8080/static/login.html#first={self.token}", self.home)
        self.c = TestClient(main.app, base_url="http://127.0.0.1:8080")

    def link(self):
        with open(os.path.join(self.home, "open-panel.url"), newline="") as f:
            return f.read()

    def test_choose_password(self):
        r = self.c.post("/api/auth/first-sign-in/check", json={"token": self.token})
        self.assertEqual(r.json(), {"username": "admin"})
        r = self.c.post("/api/auth/first-sign-in", json={"token": self.token, "password": "my own password"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["username"], "admin")
        self.assertEqual(self.c.get("/api/auth/me").status_code, 200)
        # used up; the random password and its note are gone; the Finish link is the plain address
        self.assertEqual(self.c.post("/api/auth/first-sign-in/check", json={"token": self.token}).status_code, 400)
        self.assertFalse(os.path.exists(os.path.join(self.home, "first-sign-in.txt")))
        self.assertIn("URL=http://localhost:8080/\r\n", self.link())
        self.assertNotIn("#first", self.link())
        fresh = TestClient(self.main.app)
        self.assertEqual(fresh.post("/api/auth/login", json={"username": "admin", "password": self.RANDOM}).status_code, 401)
        self.assertEqual(fresh.post("/api/auth/login", json={"username": "admin", "password": "my own password"}).status_code, 200)

    def test_wrong_or_expired_token(self):
        self.assertEqual(self.c.post("/api/auth/first-sign-in", json={"token": "nope", "password": "my own password"}).status_code, 400)
        firstsignin.create("admin", days=-1)
        self.assertEqual(self.c.post("/api/auth/first-sign-in/check", json={"token": self.token}).status_code, 400)

    def test_password_rules_keep_the_link(self):
        r = self.c.post("/api/auth/first-sign-in", json={"token": self.token, "password": "x"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.c.post("/api/auth/first-sign-in/check", json={"token": self.token}).status_code, 200)
        self.assertTrue(os.path.exists(os.path.join(self.home, "first-sign-in.txt")))

    def test_guessing_is_rate_limited(self):
        for _ in range(self.auth.MAX_FAILS):
            self.c.post("/api/auth/first-sign-in/check", json={"token": "guess"})
        self.assertEqual(self.c.post("/api/auth/first-sign-in/check", json={"token": self.token}).status_code, 429)

    def test_token_not_stored(self):
        with open(firstsignin.FIRST_FILE) as f:
            self.assertNotIn(self.token, f.read())


class ResetPassword(unittest.TestCase):
    """redstonepanel reset-password NAME (1.24)."""

    def run_cli(self, env, *args, stdin=""):
        import subprocess
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return subprocess.run([sys.executable, os.path.join(root, "run.py"), *args], input=stdin, env=env,
                              capture_output=True, text=True, timeout=60)

    def test_sets_the_password_and_signs_out(self):
        d = tempfile.mkdtemp(dir=TMP)
        env = dict(os.environ, RP_DATA_DIR=d, RP_LOG_FILE=os.path.join(d, "panel.log"))
        self.assertEqual(self.run_cli(env, "add-user", "bob", stdin="first password\n").returncode, 0)
        r = self.run_cli(env, "reset-password", "Bob", stdin="second password\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("signed out everywhere", r.stdout)
        import json
        with open(os.path.join(d, "users.json")) as f:
            users = json.load(f)
        bob = users[0] if isinstance(users, list) else users["users"][0]
        self.assertEqual(bob.get("session_version"), 2)
        from core import auth
        self.assertTrue(auth.verify_password("second password", bob["password_hash"]))
        with open(os.path.join(d, "panel.log")) as f:
            self.assertIn("password reset for panel user 'bob'", f.read())
        r = self.run_cli(env, "reset-password", "bob", stdin="x\n")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("at least", r.stderr)
        r = self.run_cli(env, "reset-password", "no-such-account-here", stdin="long enough password\n")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("isn't a panel account", r.stderr)


if __name__ == "__main__":
    unittest.main()
