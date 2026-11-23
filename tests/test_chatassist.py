"""Tests for Live Chat's command help (1.21.4): core/chatassist.py (who is online or was seen,
and a Java server's own commands and game rules from `help`), the command list in
static/commands.json, and the suggestion logic in static/js/chat-commands.js (run with Node
when it is installed). Run: python -m unittest discover -s tests"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-chatassist-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import chatassist as CA  # noqa: E402
from core import grants, rcon  # noqa: E402

try:
    import fastapi  # noqa: F401
    HAVE_FASTAPI = True
except ImportError:
    HAVE_FASTAPI = False

# What a real 1.21.11 server answers over RCON (no line breaks), shortened
HELP = ("/advancement (grant|revoke)/clear [<targets>]/xp -> experience/gamemode <gamemode> [<target>]"
        "/gamerule (spawn_wardens|minecraft:spawn_wardens|" + "|".join(f"rule_{i}|minecraft:rule_{i}" for i in range(40))
        + ")/minecraft:gamemode <gamemode>/save-all [flush]/stop/tp -> teleport/place (feature|jigsaw)")
HELP_GAMERULE = ("/gamerule spawn_wardens [<value>]/gamerule minecraft:spawn_wardens [<value>]"
                 "/gamerule keep_inventory [<value>]/gamerule minecraft:keep_inventory [<value>]")
PAPER_HELP = "--------- Help: Index (1/12) --------------------\nUse /help [n] to get page n of help.\nAliases: Lists command aliases"


class Parsing(unittest.TestCase):
    def test_help_gives_each_command_with_its_usage(self):
        got = CA.parse_help(HELP)
        self.assertEqual([c["name"] for c in got],
                         ["advancement", "clear", "xp", "gamemode", "gamerule", "save-all", "stop", "tp", "place"])
        by = {c["name"]: c["usage"] for c in got}
        self.assertEqual(by["gamemode"], "<gamemode> [<target>]")
        self.assertEqual(by["xp"], "-> experience")
        self.assertEqual(by["stop"], "")
        self.assertEqual(by["place"], "(feature|jigsaw)")
        self.assertLessEqual(len(by["gamerule"]), CA.USAGE_MAX + 2)
        self.assertTrue(by["gamerule"].endswith("|…"))

    def test_colour_codes_and_line_breaks(self):
        self.assertEqual([c["name"] for c in CA.parse_help("§6/list [uuids]\n/seed")], ["list", "seed"])

    def test_paged_help_is_not_a_command_list(self):
        self.assertEqual(CA.parse_help(PAPER_HELP), [])
        self.assertEqual(CA.parse_help(""), [])
        self.assertEqual(CA.parse_help("Unknown or incomplete command"), [])

    def test_gamerules_without_the_namespaced_copies(self):
        self.assertEqual(CA.parse_gamerules(HELP_GAMERULE), ["keep_inventory", "spawn_wardens"])
        self.assertEqual(CA.parse_gamerules("/gamerule keepInventory [<value>]/gamerule pvp [<value>]"), ["keepInventory", "pvp"])
        self.assertEqual(CA.parse_gamerules(PAPER_HELP), [])


class Info(unittest.TestCase):
    def setUp(self):
        self.java, self.running, self.rcon_on, self.started = True, True, True, 1000
        self.sent = []
        self.answers = {"list": "There are 2 of a max of 20 players online: Steve, Alex",
                        "help gamerule": HELP_GAMERULE, "help": HELP}
        self.events = [{"ts": 1, "ev": "join", "name": "Notch"}, {"ts": 2, "ev": "refused", "name": "Griefer"},
                       {"ts": 3, "ev": "join", "name": "Steve"}, {"ts": 4, "ev": "leave", "name": "Notch"},
                       {"ts": 5, "ev": "join", "name": "Stella"}, {"ts": 6, "ev": "join", "name": "notch"}]
        CA._help_cache.clear()

        def rcon_call(d):
            if not self.rcon_on:
                return None

            def send(cmd, timeout=10):
                self.sent.append(cmd)
                if isinstance(self.answers.get(cmd), Exception):
                    raise self.answers[cmd]
                return self.answers.get(cmd, "")
            return send
        for p in (mock.patch.object(CA.manager, "get_server_dir", lambda s: TMP),
                  mock.patch.object(CA.manager, "_is_java", lambda s: self.java),
                  mock.patch.object(CA.backups, "_is_active", lambda s: self.running),
                  mock.patch.object(CA.backups, "_java_rcon", rcon_call),
                  mock.patch.object(CA.playerlog, "load", lambda s: list(self.events)),
                  mock.patch.object(CA.moderation, "_started_at", lambda s: self.started)):
            p.start()
            self.addCleanup(p.stop)

    def test_a_running_java_server(self):
        got = CA.info("minecraft")
        self.assertEqual((got["edition"], got["running"], got["online"], got["online_source"]),
                         ("java", True, ["Alex", "Steve"], "rcon"))
        self.assertEqual(got["known"], ["notch", "Stella"])            # newest first, not the ones online, never refused ones
        self.assertEqual(got["gamerules"], ["keep_inventory", "spawn_wardens"])
        self.assertIn({"name": "save-all", "usage": "[flush]"}, got["commands"])

    def test_only_reading_commands_are_sent_and_help_is_asked_once_per_start(self):
        CA.info("minecraft")
        CA.info("minecraft")
        self.assertEqual(self.sent, ["list", "help", "help gamerule", "list"])
        self.started = 2000                                            # the server restarted: mods may have changed
        CA.info("minecraft")
        self.assertEqual(self.sent[-3:], ["list", "help", "help gamerule"])

    def test_paper_gets_players_only(self):
        self.answers["help"] = PAPER_HELP
        got = CA.info("minecraft")
        self.assertEqual((got["commands"], got["gamerules"], got["online"]), ([], [], ["Alex", "Steve"]))
        self.assertNotIn("help gamerule", self.sent)

    def test_a_server_that_is_still_starting_is_asked_again(self):
        self.answers["help"] = rcon.RconError("refused")
        self.assertEqual(CA.info("minecraft")["commands"], [])
        self.answers["help"] = HELP
        self.assertTrue(CA.info("minecraft")["commands"])

    def test_java_without_rcon_and_a_stopped_server(self):
        self.rcon_on, self.started = False, 0
        got = CA.info("minecraft")
        self.assertEqual((got["online"], got["online_source"], got["commands"]), (["notch", "Stella", "Steve"], "log", []))
        self.running = False
        got = CA.info("minecraft")
        self.assertEqual((got["running"], got["online"], got["known"]), (False, [], ["notch", "Stella", "Steve"]))

    def test_bedrock_uses_the_player_log_and_sends_nothing(self):
        self.java = False
        self.events = [{"ts": 1500, "ev": "join", "name": "Steve Smith"}, {"ts": 900, "ev": "join", "name": "Old Friend"}]
        got = CA.info("minecraft-bedrock")
        self.assertEqual((got["edition"], got["online"], got["known"], got["commands"]),
                         ("bedrock", ["Steve Smith"], ["Old Friend"], []))
        self.assertEqual(self.sent, [])


class Catalog(unittest.TestCase):
    """static/commands.json is read by the browser; a slip in it would only show there."""
    TYPES = {"player", "target", "destination", "position", "choice", "suggest", "number", "text", "text1"}

    def setUp(self):
        with open(os.path.join(ROOT, "static", "commands.json"), encoding="utf-8") as f:
            self.doc = json.load(f)

    def test_every_command_is_complete(self):
        seen = set()
        for c in self.doc["commands"]:
            where = c.get("name")
            for key in ("name", "label", "group", "about", "editions", "args"):
                self.assertIn(key, c, where)
            self.assertRegex(c["name"], r"^[a-z-]+( [a-z]+)?$")
            self.assertIn(c["group"], self.doc["groups"], where)
            self.assertFalse(c["about"].endswith("."), where)             # the page adds the full stop
            self.assertTrue(c["editions"] and set(c["editions"]) <= {"java", "bedrock"}, where)
            for e in c["editions"]:
                self.assertNotIn((c["name"], e), seen, "listed twice")
                seen.add((c["name"], e))
            optional = False
            for i, a in enumerate(c["args"]):
                self.assertIn(a["type"], self.TYPES, where)
                self.assertRegex(a["name"], r"^[a-z]+$")
                if a["type"] in ("choice", "suggest"):
                    self.assertTrue(a.get("choices") or a.get("list") == "gamerules" or self.doc["lists"].get(a.get("list")), where)
                if a["type"] == "text":
                    self.assertEqual(i, len(c["args"]) - 1, f"{where}: free text must come last")
                self.assertFalse(optional and not a.get("optional"), f"{where}: a needed value after an optional one")
                optional = optional or bool(a.get("optional"))
            self.assertEqual(len({a["name"] for a in c["args"]}), len(c["args"]), where)

    def test_both_editions_have_the_everyday_commands(self):
        for edition in ("java", "bedrock"):
            names = {c["name"] for c in self.doc["commands"] if edition in c["editions"]}
            for must in ("gamemode", "tp", "give", "kick", "op", "deop", "say", "tell", "time set", "weather",
                         "difficulty", "gamerule", "list", "stop"):
                self.assertIn(must, names, edition)
        java = {c["name"] for c in self.doc["commands"] if "java" in c["editions"]}
        bedrock = {c["name"] for c in self.doc["commands"] if "bedrock" in c["editions"]}
        self.assertTrue({"ban", "pardon", "whitelist add", "save-all"} <= java)
        self.assertFalse({"ban", "whitelist add", "save-all"} & bedrock)   # Bedrock has no ban list; its list is the allowlist
        self.assertIn("allowlist add", bedrock)

    def test_dangerous_commands_ask_first_and_are_never_one_click(self):
        for c in self.doc["commands"]:
            if c["name"] in ("stop", "kill", "clear"):
                self.assertTrue(c.get("confirm"), c["name"])
        for q in self.doc["quick"]:
            self.assertRegex(q["command"], r"^(list|save-all|time set (day|night)|weather clear)$")
            self.assertTrue(set(q["editions"]) <= {"java", "bedrock"})

    def test_game_rule_lists(self):
        lists = self.doc["lists"]
        self.assertIn("keep_inventory", lists["gamerules_java"])
        self.assertIn("keepInventory", lists["gamerules_java_old"])
        self.assertIn("keepinventory", lists["gamerules_bedrock"])

    def test_the_package_ships_the_list_and_the_script(self):
        with open(os.path.join(ROOT, "debian", "redstonepanel.install")) as f:
            install = f.read()
        self.assertIn("static/commands.json", install)
        self.assertIn("static/js/chat-commands.js", install)
        self.assertIn("core/chatassist.py", install)
        with open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8") as f:
            page = f.read()
        self.assertLess(page.index('"/static/js/chat-commands.js"'), page.index('"/static/js/app.js"'))


@unittest.skipUnless(shutil.which("node"), "needs Node.js")
class SuggestionLogic(unittest.TestCase):
    def test_node_tests_pass(self):
        r = subprocess.run(["node", "--test", os.path.join(ROOT, "tests", "js", "chat-commands.test.js")],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout[-3000:] + r.stderr[-1000:])


@unittest.skipUnless(HAVE_FASTAPI, "needs fastapi")
class Routes(unittest.TestCase):
    def test_route_answers_and_needs_console(self):
        import main
        with mock.patch.object(main.chatassist, "info", lambda s: {"online": ["Steve"], "server": s}):
            self.assertEqual(main.server_chat_assist("minecraft-2"), {"online": ["Steve"], "server": "minecraft-2"})
        self.assertEqual(grants.rule_for("GET", "/api/servers/{server_name}/chat/assist"), "console")
        self.assertTrue(main.cluster.proxy_allowed("GET", "/api/servers/minecraft-2/chat/assist"))

    def test_the_page_links_the_script_with_the_version(self):
        import main
        main._page_cache.clear()
        with mock.patch.object(main, "panel_version", lambda: "1.21.4"):
            body = main.index_page().body.decode()
        self.assertIn('"/static/js/chat-commands.js?v=1.21.4"', body)


if __name__ == "__main__":
    unittest.main()
