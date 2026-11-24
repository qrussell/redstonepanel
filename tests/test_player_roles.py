"""Player roles (Configuration > Players): names and IDs are checked before they reach
ops.json / permissions.json or a Bedrock server's console, and the XUID fallback runs the
server as the game account. Run: python -m unittest discover -s tests
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-roles-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import manager as M  # noqa: E402


class Roles(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(dir=TMP)
        self.java = False
        for p in (mock.patch.object(M, "get_server_dir", lambda s: self.dir),
                  mock.patch.object(M, "_is_java", lambda s: self.java),
                  mock.patch.object(M, "_players_from_logs", lambda *a: {}),
                  mock.patch.object(M, "write_log", lambda *a: None)):
            p.start()
            self.addCleanup(p.stop)

    def test_bad_names_never_reach_a_lookup(self):
        with mock.patch.object(M, "get_xuid", side_effect=AssertionError("looked up")), \
                mock.patch.object(M, "get_java_uuid", side_effect=AssertionError("looked up")):
            for java, name in ((False, 'Steve"\nop Mallory'), (False, "a\nstop"), (False, ""),
                               (True, "no spaces"), (True, "x" * 17), (True, "ab")):
                self.java = java
                with self.assertRaises(M.PlayerError, msg=repr(name)):
                    M.set_player_role("minecraft", name, "operator")

    def test_bad_ids_are_refused(self):
        with self.assertRaises(M.PlayerError):
            M.set_player_role("minecraft", "Steve", "operator", player_id="123abc")
        self.java = True
        with self.assertRaises(M.PlayerError):
            M.set_player_role("minecraft", "Steve", "operator", player_id="../../etc")

    def test_a_good_name_and_id_are_written(self):
        M.set_player_role("minecraft", "Big Steve#1234", "operator", player_id="2535411111111111")
        with open(os.path.join(self.dir, "permissions.json")) as f:
            self.assertEqual(json.load(f), [{"permission": "operator", "name": "Big Steve#1234",
                                             "xuid": "2535411111111111"}])
        self.java = True
        M.set_player_role("minecraft", "Steve_1", "operator", player_id="069a79f4-44e9-4726-a5be-fca90e38aaf5")
        with open(os.path.join(self.dir, "ops.json")) as f:
            self.assertEqual(json.load(f)[0]["name"], "Steve_1")


class LogPlayers(unittest.TestCase):
    def test_java_players_and_uuids_come_from_the_log(self):
        text = ("[Server thread/INFO]: Steve joined the game\n"
                "[User Authenticator #1/INFO]: UUID of player Alex is 069a79f4-44e9-4726-a5be-fca90e38aaf5\n")
        with mock.patch.object(M, "_player_log_text", lambda *a: text):
            seen = M._players_from_logs("minecraft", "/nowhere", True)
        self.assertEqual(seen["alex"], ("Alex", "069a79f4-44e9-4726-a5be-fca90e38aaf5"))
        self.assertIn("steve", seen)


class XuidFallback(unittest.TestCase):
    def test_a_bad_gamertag_starts_nothing(self):
        with mock.patch.object(M.subprocess, "Popen", side_effect=AssertionError("started")), \
                mock.patch.object(M.services, "stop", side_effect=AssertionError("stopped")):
            self.assertIsNone(M.fallback_allowlist_xuid("minecraft", 'x"\nop Mallory'))

    def test_the_server_runs_as_the_game_account(self):
        d = tempfile.mkdtemp(dir=TMP)
        seen = {}

        class Proc:
            def __init__(self, cmd, **kw):
                seen["cmd"] = cmd
                with open(os.path.join(d, "allowlist.json"), "w") as f:
                    json.dump([{"name": "Steve", "xuid": "2535411111111111"}], f)

            def communicate(self, input=None, timeout=None):
                seen["input"] = input

        with mock.patch.object(M, "get_server_dir", lambda s: d), \
                mock.patch.object(M.services, "stop", lambda *a, **k: None), \
                mock.patch.object(M.services, "start", lambda *a, **k: None), \
                mock.patch.object(M.system, "as_game_account", lambda cmd: ["runuser", "-u", "minecraft", "--"] + cmd), \
                mock.patch.object(M.system, "take_for_game_account", lambda p: None), \
                mock.patch.object(M.subprocess, "Popen", Proc), \
                mock.patch.object(M, "write_log", lambda *a: None):
            self.assertEqual(M.fallback_allowlist_xuid("minecraft", "Steve"), "2535411111111111")
        self.assertEqual(seen["cmd"][:4], ["runuser", "-u", "minecraft", "--"])
        self.assertEqual(seen["input"], 'allowlist add "Steve"\nstop\n')
        with open(os.path.join(d, "allowlist.json")) as f:
            self.assertEqual(f.read(), "[]", "the allowlist is put back")


if __name__ == "__main__":
    unittest.main()
