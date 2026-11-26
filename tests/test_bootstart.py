"""1.25: Start at boot (core/bootstart.py).
Run: python -m unittest discover -s tests

On by default. Turned off, the unit is disabled and the choice is remembered, so a restore,
an import of a moved server and taking over a found server keep it; deleting a server for good
forgets it. A move carries it in the server's details; an older member's details mean on.
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-boot-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import bootstart as B  # noqa: E402

try:
    import fastapi  # noqa: F401
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except (ImportError, RuntimeError):
    HAVE_FASTAPI = False


class Base(unittest.TestCase):
    def setUp(self):
        self.enabled = set()
        self.calls = []

        def enable(s, *a, **k):
            self.calls.append(("enable", s))
            self.enabled.add(s)

        def disable(s, *a, **k):
            self.calls.append(("disable", s))
            self.enabled.discard(s)
        for p in (mock.patch.object(B, "BOOT_FILE", os.path.join(tempfile.mkdtemp(dir=TMP), "boot.json")),
                  mock.patch.object(B.services, "enable", enable),
                  mock.patch.object(B.services, "disable", disable),
                  mock.patch.object(B.services, "unit_state",
                                    lambda u, verb="is-active", timeout=None:
                                    "enabled" if u.replace(".service", "") in self.enabled else "disabled")):
            p.start()
            self.addCleanup(p.stop)


class Choice(Base):
    def test_on_by_default(self):
        self.assertTrue(B.wanted("minecraft"))
        B.apply("minecraft")
        self.assertEqual(self.calls, [("enable", "minecraft")])
        self.assertTrue(B.enabled("minecraft"))

    def test_off_is_remembered_and_applied_again(self):
        self.enabled.add("minecraft")
        self.assertFalse(B.turn("minecraft", False))
        self.assertFalse(B.wanted("minecraft"))
        self.calls.clear()
        B.apply("minecraft")                 # a restore sets the server up again
        self.assertEqual(self.calls, [("disable", "minecraft")])
        self.assertTrue(B.wanted("minecraft-2"), "only that server")
        self.assertTrue(B.turn("minecraft", True))
        self.assertTrue(B.wanted("minecraft"))

    def test_deleting_for_good_forgets(self):
        B.turn("minecraft", False)
        B.forget("minecraft")
        self.assertTrue(B.wanted("minecraft"), "a new server of that name starts at boot")

    def test_an_unreadable_file_means_on(self):
        with open(B.BOOT_FILE, "w") as f:
            f.write("{not json")
        self.assertTrue(B.wanted("minecraft"))


class Moves(Base):
    def test_a_move_carries_it(self):
        from core import transfer
        with mock.patch.object(transfer.manager, "bedrock_supported", lambda: True):
            self.assertFalse(transfer._clean_meta({"edition": "java", "name": "minecraft", "boot": False})["boot"])
            self.assertTrue(transfer._clean_meta({"edition": "java", "name": "minecraft"})["boot"],
                            "an older member doesn't say: on")
            self.assertTrue(transfer._clean_meta({"edition": "java", "boot": "no"})["boot"], "only False turns it off")

    def test_import_transferred_remembers_what_came(self):
        from core import manager
        src = inspect_source(manager.import_transferred)
        self.assertIn("bootstart.remember(name, boot is not False)", src)
        self.assertLess(src.index("bootstart.remember"), src.index("bootstart.apply"))

    def test_every_setup_place_asks(self):
        from core import manager, found
        for fn in (manager.restore_server, manager._import_unpacked, manager.deploy_server):
            src = inspect_source(fn)
            self.assertIn("bootstart.apply(", src, fn.__name__)
            self.assertNotIn("services.enable(", src, fn.__name__)
        self.assertIn("bootstart.apply(name)", inspect_source(found))


def inspect_source(obj):
    import inspect
    return inspect.getsource(obj)


@unittest.skipUnless(HAVE_FASTAPI, "needs fastapi")
class Api(Base):
    def setUp(self):
        super().setUp()
        import main
        self.main = main
        self.log = []
        self.user = {"username": "quentin", "role": "admin", "source": "linux"}
        for p in (mock.patch.object(main.auth, "read_session_token", lambda t: dict(self.user)),
                  mock.patch.object(main.manager, "_check_server", lambda s: None),
                  mock.patch.object(main.manager, "write_log", self.log.append)):
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(main.app)
        self.client.cookies.set(main.auth.SESSION_COOKIE, "session")

    def test_turn_off_and_on(self):
        self.enabled.add("minecraft")
        self.assertEqual(self.client.get("/api/servers/minecraft/boot").json(), {"enabled": True})
        r = self.client.put("/api/servers/minecraft/boot", json={"enabled": False})
        self.assertEqual(r.json(), {"enabled": False}, r.text)
        self.assertIn("[SERVICE] quentin turned Start at boot off for minecraft.", self.log)
        self.assertFalse(B.wanted("minecraft"))

    def test_needs_the_settings_permission(self):
        from core import grants
        self.user = {"username": "alex", "role": "user", "source": "panel"}
        with mock.patch.object(grants, "GRANTS_FILE", os.path.join(tempfile.mkdtemp(dir=TMP), "grants.json")):
            grants._cache.update(mtime=None, data=None)
            grants.set_user("alex", {"minecraft": ["view", "power"]})
            self.assertEqual(self.client.get("/api/servers/minecraft/boot").status_code, 200)
            self.assertEqual(self.client.put("/api/servers/minecraft/boot", json={"enabled": False}).status_code, 403)
            grants.set_user("alex", {"minecraft": ["settings"]})
            self.assertEqual(self.client.put("/api/servers/minecraft/boot", json={"enabled": False}).status_code, 200)
            grants._cache.update(mtime=None, data=None)


if __name__ == "__main__":
    unittest.main()
