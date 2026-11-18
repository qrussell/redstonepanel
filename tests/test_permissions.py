"""Who may uninstall or purge a server. Run: python -m unittest discover -s tests"""
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-perms-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import fastapi  # noqa: F401
    HAVE_FASTAPI = True
except ImportError:
    HAVE_FASTAPI = False


def request(username, role, source):
    return SimpleNamespace(state=SimpleNamespace(user={"username": username, "role": role, "source": source}))


@unittest.skipUnless(HAVE_FASTAPI, "needs fastapi")
class Uninstall(unittest.TestCase):
    def setUp(self):
        import main
        self.main = main
        self.removed = []
        for p in (mock.patch.object(main.manager, "_check_server", lambda s: None),
                  mock.patch.object(main.manager, "write_log", lambda msg: None),
                  mock.patch.object(main.manager, "remove_server",
                                    lambda s, keep: self.removed.append((s, keep)))):
            p.start()
            self.addCleanup(p.stop)

    def refused(self, req, keep_world):
        with self.assertRaises(self.main.HTTPException) as cm:
            self.main.uninstall_server(req, "minecraft-test", keep_world)
        self.assertEqual(cm.exception.status_code, 403)
        return cm.exception.detail

    def test_users_cannot_uninstall_or_purge(self):
        for keep in (True, False):
            self.assertIn("Only admins", self.refused(request("alex", "user", "panel"), keep))
        self.assertEqual(self.removed, [])

    def test_trusted_lan_visitors_are_asked_to_sign_in(self):
        for keep in (True, False):
            self.assertIn("Sign in as an admin", self.refused(request("lan", "user", "trusted"), keep))
        self.assertEqual(self.removed, [])

    def test_admins_can(self):
        self.main.uninstall_server(request("root", "admin", "linux"), "minecraft-test", True)
        self.main.uninstall_server(request("root", "admin", "linux"), "minecraft-test", False)
        self.assertEqual(self.removed, [("minecraft-test", True), ("minecraft-test", False)])


if __name__ == "__main__":
    unittest.main()
