"""Tests for core/moderation.py: kick, ban and unban over RCON or screen, the ban list file of a
stopped Java server, and who's online. Run: python -m unittest discover -s tests"""
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-moderation-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import moderation as MOD  # noqa: E402

try:
    import fastapi  # noqa: F401
    HAVE_FASTAPI = True
except ImportError:
    HAVE_FASTAPI = False

UUID = "069a79f4-44e9-4726-a5be-fca90e38aaf5"


class Moderation(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(dir=TMP)
        self.java = True
        self.running = True
        self.rcon_on = True
        self.sent, self.screen = [], []
        self.answers = {}
        self.events = []

        def rcon_call(d):
            if not self.rcon_on:
                return None

            def send(cmd, timeout=10):
                self.sent.append(cmd)
                for prefix, out in self.answers.items():
                    if cmd.startswith(prefix):
                        return out
                return ""
            return send
        for p in (mock.patch.object(MOD.manager, "get_server_dir", lambda s: self.d),
                  mock.patch.object(MOD.manager, "_is_java", lambda s: self.java),
                  mock.patch.object(MOD.backups, "_is_active", lambda s: self.running),
                  mock.patch.object(MOD.backups, "_java_rcon", rcon_call),
                  mock.patch.object(MOD.backups, "_screen", lambda s, cmd: self.screen.append(cmd) or True),
                  mock.patch.object(MOD.playerlog, "load", lambda s: list(self.events)),
                  mock.patch.object(MOD.whitelist, "_java_uuid", lambda s, d, n: UUID if n == "Notch" else None),
                  mock.patch.object(MOD, "_started_at", lambda s: 1000)):
            p.start()
            self.addCleanup(p.stop)

    def bans(self):
        with open(os.path.join(self.d, MOD.BAN_FILE)) as f:
            return json.load(f)

    # --- who's online
    def test_online_from_rcon_list(self):
        self.answers["list"] = "There are 2 of a max of 20 players online: Steve, §aAlex"
        self.assertEqual(MOD.online("minecraft-java"), (["Alex", "Steve"], "rcon"))
        self.answers["list"] = "There are 0 of a max of 20 players online: "
        self.assertEqual(MOD.online("minecraft-java"), ([], "rcon"))

    def test_online_from_the_log_since_the_server_started(self):
        self.rcon_on = False
        self.events = [
            {"ts": 900, "ev": "join", "name": "Ghost"},        # before this start: ignored
            {"ts": 1010, "ev": "join", "name": "Steve"},
            {"ts": 1020, "ev": "join", "name": "Alex"},
            {"ts": 1030, "ev": "leave", "name": "alex"},
            {"ts": 1040, "ev": "refused", "name": "Eve"},
        ]
        self.assertEqual(MOD.online("minecraft-java"), (["Steve"], "log"))

    def test_nobody_online_when_stopped(self):
        self.running = False
        self.assertEqual(MOD.online("minecraft-java"), ([], None))

    # --- kick
    def test_kick_java_over_rcon(self):
        self.answers["kick"] = "Kicked Steve: Griefing"
        r = MOD.kick("minecraft-java", "Steve", "Grief\ning\x07")
        self.assertEqual(self.sent, ["kick Steve Grief ing"])
        self.assertIn("Kicked Steve", r["message"])

    def test_kick_someone_offline(self):
        self.answers["kick"] = "No player was found"
        with self.assertRaisesRegex(MOD.ModerationError, "isn't online"):
            MOD.kick("minecraft-java", "Steve")

    def test_kick_needs_a_running_server_and_rcon(self):
        self.rcon_on = False
        with self.assertRaisesRegex(MOD.ModerationError, "RCON"):
            MOD.kick("minecraft-java", "Steve")
        self.running = False
        with self.assertRaisesRegex(MOD.ModerationError, "isn't running"):
            MOD.kick("minecraft-java", "Steve")

    def test_kick_bedrock_through_screen(self):
        self.java = False
        MOD.kick("minecraft-bedrock", "Big Steve", "bye")
        self.assertEqual(self.screen, ['kick "Big Steve" bye'])

    def test_names_are_checked(self):
        for bad in ("a;op me", "x", "Steve kick Alex", ""):
            with self.assertRaises(MOD.whitelist.WhitelistError):
                MOD.kick("minecraft-java", bad)
        self.assertEqual(self.sent, [])

    # --- ban and unban, running
    def test_ban_and_unban_over_rcon(self):
        self.answers["ban"] = "Banned Steve: Griefing"
        self.assertIn("Banned Steve", MOD.ban("minecraft-java", "Steve", "Griefing")["message"])
        self.answers["pardon"] = "Unbanned Steve"
        self.assertIn("Unbanned Steve", MOD.unban("minecraft-java", "Steve")["message"])
        self.assertEqual(self.sent, ["ban Steve Griefing", "pardon Steve"])

    def test_rcon_answers(self):
        self.answers["ban"] = "Nothing changed. The player is already banned"
        self.assertIn("already banned", MOD.ban("minecraft-java", "Steve")["message"])
        self.answers["ban"] = "That player does not exist"
        with self.assertRaisesRegex(MOD.ModerationError, "doesn't know"):
            MOD.ban("minecraft-java", "Nobody123")
        self.answers["pardon"] = "Nothing changed. The player isn't banned"
        with self.assertRaisesRegex(MOD.ModerationError, "isn't banned"):
            MOD.unban("minecraft-java", "Steve")

    def test_running_without_rcon_never_edits_the_file(self):
        self.rcon_on = False
        with self.assertRaisesRegex(MOD.ModerationError, "RCON"):
            MOD.ban("minecraft-java", "Notch")
        with self.assertRaisesRegex(MOD.ModerationError, "RCON"):
            MOD.unban("minecraft-java", "Notch")
        self.assertFalse(os.path.exists(os.path.join(self.d, MOD.BAN_FILE)))

    # --- ban and unban, stopped
    def test_stopped_server_ban_list_file(self):
        self.running = False
        r = MOD.ban("minecraft-java", "Notch", "Spam", by="alex")
        self.assertIn("when the server starts", r["message"])
        [e] = self.bans()
        self.assertEqual((e["uuid"], e["name"], e["reason"], e["source"], e["expires"]),
                         (UUID, "Notch", "Spam", "RedstonePanel (alex)", "forever"))
        self.assertRegex(e["created"], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d \+0000$")
        self.assertIn("already banned", MOD.ban("minecraft-java", "notch")["message"])
        self.assertEqual(len(self.bans()), 1)
        self.assertEqual([b["name"] for b in MOD.banned("minecraft-java")], ["Notch"])
        MOD.unban("minecraft-java", "NOTCH")
        self.assertEqual(self.bans(), [])
        with self.assertRaisesRegex(MOD.ModerationError, "isn't banned"):
            MOD.unban("minecraft-java", "Notch")
        self.assertEqual(self.sent, [])

    def test_stopped_ban_needs_a_uuid(self):
        self.running = False
        with self.assertRaisesRegex(MOD.ModerationError, "UUID"):
            MOD.ban("minecraft-java", "Unknown_1")

    def test_no_bans_on_bedrock(self):
        self.java = False
        with self.assertRaisesRegex(MOD.ModerationError, "no ban list"):
            MOD.ban("minecraft-bedrock", "Steve")
        self.assertEqual(MOD.banned("minecraft-bedrock"), [])
        self.assertFalse(MOD.info("minecraft-bedrock")["bans"])

    def test_info(self):
        self.answers["list"] = "There are 1 of a max of 20 players online: Steve"
        i = MOD.info("minecraft-java")
        self.assertEqual((i["online"], i["online_source"], i["bans"], i["live"]), (["Steve"], "rcon", True, True))


@unittest.skipUnless(HAVE_FASTAPI, "needs fastapi")
class Routes(unittest.TestCase):
    def setUp(self):
        import main
        self.main = main
        self.logs = []
        for p in (mock.patch.object(main.manager, "_check_server", lambda s: None),
                  mock.patch.object(main.manager, "write_log", self.logs.append)):
            p.start()
            self.addCleanup(p.stop)

    def req(self, role="user", source="panel"):
        return SimpleNamespace(state=SimpleNamespace(user={"username": "alex", "role": role, "source": source}))

    def test_users_may_kick_and_it_is_logged(self):
        with mock.patch.object(self.main.moderation, "kick", lambda s, p, r: {"message": "ok"}) as _:
            out = self.main.kick_player(self.req(), "minecraft-java",
                                        self.main.ModerationAction(player="Steve", reason="spam\nspam"))
        self.assertEqual(out, {"message": "ok"})
        self.assertEqual(self.logs, ["[PLAYERS] alex kicked Steve (spam spam) on minecraft-java."])

    def test_ban_passes_who_banned(self):
        seen = []
        with mock.patch.object(self.main.moderation, "ban", lambda s, p, r, by: seen.append((p, r, by)) or {}):
            self.main.ban_player(self.req(), "minecraft-java", self.main.ModerationAction(player="Steve"))
        self.assertEqual(seen, [("Steve", "", "alex")])

    def test_errors_are_400_and_not_logged(self):
        def boom(*a):
            raise self.main.moderation.ModerationError("nope")
        with mock.patch.object(self.main.moderation, "unban", boom):
            with self.assertRaises(self.main.HTTPException) as cm:
                self.main.unban_player(self.req(), "minecraft-java", self.main.ModerationAction(player="Steve"))
        self.assertEqual((cm.exception.status_code, cm.exception.detail), (400, "nope"))
        self.assertEqual(self.logs, [])

    def test_the_master_may_moderate_node_servers(self):
        for path in ("/api/servers/minecraft-java/moderation",):
            self.assertTrue(self.main.cluster.path_allowed("GET", path))
        for action in ("kick", "ban", "unban"):
            self.assertTrue(self.main.cluster.path_allowed("POST", f"/api/servers/minecraft-java/players/{action}"))


if __name__ == "__main__":
    unittest.main()
