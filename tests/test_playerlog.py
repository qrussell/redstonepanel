"""Player access log (core/playerlog.py) and whitelist edits (core/whitelist.py).
Run: python -m unittest discover -s tests"""
import datetime as dt
import gzip
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-playerlog-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import playerlog as PL  # noqa: E402
from core import whitelist as WL  # noqa: E402

UUID = "069a79f4-44e9-4726-a5be-fca90e38aaf5"


class Parse(unittest.TestCase):
    def java(self, *msgs):
        p = PL.Parser(True)
        return [e for e in (p.parse(m) for m in msgs) if e]

    def test_join_and_leave_with_uuid(self):
        ev = self.java(f"UUID of player Steve is {UUID}", "Steve[/203.0.113.7:51234] logged in with entity id 7",
                       "Steve joined the game", "Steve left the game")
        self.assertEqual(ev, [{"ev": "join", "name": "Steve", "id": UUID},
                              {"ev": "leave", "name": "Steve", "id": UUID}])

    def test_refused_forms_never_keep_the_address(self):
        cases = [
            "Disconnecting Alex (/203.0.113.7:51234): You are not whitelisted on this server!",
            "Alex (/203.0.113.7:51234) lost connection: You are not white-listed on this server!",
            f"Disconnecting com.mojang.authlib.GameProfile@1a2b[id={UUID},name=Alex,properties={{}},legacy=false] "
            f"(/203.0.113.7:1): You are not whitelisted on this server!",
            f"Disconnecting Alex ({UUID}) (/203.0.113.7:1): You are not whitelisted on this server!",
        ]
        for c in cases:
            ev = self.java(c)
            self.assertEqual(len(ev), 1, c)
            self.assertEqual((ev[0]["ev"], ev[0]["name"], ev[0]["reason"]), ("refused", "Alex", "not_whitelisted"), c)
            self.assertNotIn("203.0.113.7", json.dumps(ev[0]))

    def test_other_reasons_and_plain_disconnects(self):
        self.assertEqual(self.java("Disconnecting Bob (/1.2.3.4:5): You are banned from this server.")[0]["reason"], "banned")
        self.assertEqual(self.java("Disconnecting Bob (/1.2.3.4:5): Outdated client! Please use 1.21.4")[0]["reason"],
                         "wrong_version")
        self.assertEqual(self.java("Disconnecting Bob (/1.2.3.4:5): The server is full!")[0]["reason"], "server_full")
        self.assertEqual(self.java("Bob (/1.2.3.4:5) lost connection: Disconnected"), [])
        self.assertEqual(self.java("Bob lost connection: Timed out"), [])

    def test_bedrock(self):
        p = PL.Parser(False)
        self.assertEqual(p.parse("Player connected: Gamer Tag, xuid: 2535400000000001"),
                         {"ev": "join", "name": "Gamer Tag", "id": "2535400000000001"})
        self.assertEqual(p.parse("Player disconnected: Gamer Tag, xuid: 2535400000000001, pfid: abc"),
                         {"ev": "leave", "name": "Gamer Tag", "id": "2535400000000001"})


class Sources(unittest.TestCase):
    def test_journal_strips_each_loader_prefix(self):
        text = ("1731500000.5 host java[1]: [10:00:00] [Server thread/INFO]: Steve joined the game\n"
                "1731500001.0 host java[1]: [10:00:01 INFO]: Alex joined the game\n"
                "1731500002.0 host java[1]: [10:00:02] [Server thread/INFO] (Minecraft) Kim joined the game\n"
                "1731500003.0 host bedrock[2]: [2026-11-13 10:00:03:123 INFO] Player connected: Tag, xuid: 1\n")
        rows = PL.parse_journal(text)
        self.assertEqual([r[1] for r in rows], ["Steve joined the game", "Alex joined the game",
                                                "Kim joined the game", "Player connected: Tag, xuid: 1"])
        self.assertEqual(rows[0][0], 1731500000.5)

    def test_java_file_crosses_midnight(self):
        rows = PL.java_file_lines("[23:59:00] [Server thread/INFO]: A joined the game\n"
                                  "[00:01:00] [Server thread/INFO]: B joined the game\n", dt.date(2026, 11, 1))
        self.assertEqual(dt.datetime.fromtimestamp(rows[1][0]).date(), dt.date(2026, 11, 2))

    def test_latest_log_walks_back_from_mtime(self):
        mtime = dt.datetime(2026, 11, 20, 0, 30).timestamp()
        rows = PL.latest_log_lines("[23:50:00] [Server thread/INFO]: A joined the game\n"
                                   "[00:10:00] [Server thread/INFO]: A left the game\n", mtime)
        self.assertEqual([dt.datetime.fromtimestamp(r[0]).date() for r in rows],
                         [dt.date(2026, 11, 19), dt.date(2026, 11, 20)])

    def test_history_files(self):
        d = tempfile.mkdtemp(dir=TMP)
        os.makedirs(os.path.join(d, "logs"))
        with gzip.open(os.path.join(d, "logs", "2026-09-14-1.log.gz"), "wt") as f:
            f.write("[11:40:00] [Server thread/INFO]: Old joined the game\n")
        with open(os.path.join(d, "logs", "latest.log"), "w") as f:
            f.write("[09:30:00] [Server thread/INFO]: New joined the game\n")
        names = [e["name"] for e in PL.events_from(PL.file_lines(d, True, history=True), True)]
        self.assertEqual(names, ["Old", "New"])
        self.assertEqual([e["name"] for e in PL.events_from(PL.file_lines(d, True), True)], ["New"])


class Store(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(PL.LOG_DIR, ignore_errors=True)
        self.now = time.time()

    def ev(self, ago_h, ev, name, **kw):
        return {"ts": int(self.now - ago_h * 3600), "ev": ev, "name": name, **kw}

    def test_merge_dedupes_and_expires(self):
        a = [self.ev(1, "join", "Steve"), self.ev(24 * 100, "join", "Ancient")]
        self.assertEqual(len(PL.merge("minecraft-x", a, self.now)), 1)
        self.assertEqual(PL.merge("minecraft-x", [self.ev(1, "join", "steve")], self.now), [])
        self.assertEqual([e["name"] for e in PL.load("minecraft-x")], ["Steve"])
        with self.assertRaises(ValueError):
            PL._file("../etc")

    def test_queries(self):
        PL.merge("minecraft-x", [self.ev(50, "join", "Steve", id=UUID), self.ev(49, "leave", "Steve"),
                                 self.ev(5, "refused", "Alex", reason="not_whitelisted"),
                                 self.ev(3, "refused", "Alex", reason="not_whitelisted"),
                                 self.ev(2, "refused", "Kim", reason="not_whitelisted"),
                                 self.ev(1, "join", "Kim")], self.now)
        self.assertEqual([e["ev"] for e in PL.history("minecraft-x", "ste", now=self.now)], ["leave", "join"])
        self.assertEqual(len(PL.history("minecraft-x", ev="refused", now=self.now)), 3)
        self.assertEqual(len(PL.history("minecraft-x", days=1, now=self.now)), 4)
        self.assertEqual(PL.history("minecraft-x", UUID, now=self.now)[0]["name"], "Steve")
        s = PL.summary("minecraft-x", self.now)
        self.assertEqual((s["steve"]["id"], s["alex"]["refused_24h"]), (UUID, 2))
        # Kim got in after being refused; Alex is allowed now in the second call
        self.assertEqual(PL.turned_away("minecraft-x", now=self.now), [("Alex", 2, "not_whitelisted")])
        self.assertEqual(PL.turned_away("minecraft-x", ["alex"], now=self.now), [])

    def test_scan_backfills_then_follows_and_notifies(self):
        d = tempfile.mkdtemp(dir=TMP)
        heard = []
        PL._listeners[:] = [lambda s, e: heard.append((s, [x["name"] for x in e]))]
        state = {}
        t = int(self.now)
        rows = [(t - 100, "Steve joined the game")]
        with mock.patch.object(PL, "journal_lines", lambda s, since=None: [r for r in rows if not since or r[0] >= since]):
            PL.scan("minecraft-x", d, True, state)
            self.assertEqual(heard, [])                    # the first scan is history, not news
            rows.append((t - 10, "Disconnecting Alex (/1.2.3.4:5): You are not whitelisted on this server!"))
            PL.scan("minecraft-x", d, True, state)
            PL.scan("minecraft-x", d, True, state)          # nothing new the second time
        self.assertEqual(heard, [("minecraft-x", ["Alex"])])
        self.assertEqual([e["ev"] for e in PL.load("minecraft-x")], ["join", "refused"])
        PL._listeners[:] = []


class Whitelist(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(PL.LOG_DIR, ignore_errors=True)
        self.d = tempfile.mkdtemp(dir=TMP)
        with open(os.path.join(self.d, "server.properties"), "w") as f:
            f.write("white-list=true\nonline-mode=true\n")
        self.java = True
        for p in (mock.patch.object(WL.manager, "get_server_dir", lambda s: self.d),
                  mock.patch.object(WL.manager, "_is_java", lambda s: self.java),
                  mock.patch.object(WL.backups, "_is_active", lambda s: False),
                  mock.patch.object(WL.manager, "write_log", lambda m: None)):
            p.start()
            self.addCleanup(p.stop)

    def read(self, f):
        with open(os.path.join(self.d, f)) as fh:
            return json.load(fh)

    def test_offline_uuid(self):
        self.assertEqual(WL.offline_uuid("Notch"), "b50ad385-829d-3141-a216-7e7d7539ba7f")

    def test_java_add_uses_known_uuid_then_remove(self):
        with open(os.path.join(self.d, "usercache.json"), "w") as f:
            json.dump([{"name": "Steve", "uuid": UUID, "expiresOn": "x"}], f)
        with mock.patch.object(WL.manager, "get_java_uuid", side_effect=AssertionError("no network needed")):
            r = WL.add("minecraft-x", "Steve")
        self.assertFalse(r["restart"])
        self.assertEqual(self.read("whitelist.json"), [{"uuid": UUID, "name": "Steve"}])
        self.assertIn("already", WL.add("minecraft-x", "steve")["message"])
        info = WL.info("minecraft-x")
        self.assertEqual((info["enabled"], info["file"], [p["name"] for p in info["players"]]),
                         (True, "whitelist.json", ["Steve"]))
        WL.remove("minecraft-x", "STEVE")
        self.assertEqual(self.read("whitelist.json"), [])
        with self.assertRaises(WL.WhitelistError):
            WL.remove("minecraft-x", "Steve")

    def test_java_names_are_checked_and_unknown_players_refused(self):
        for bad in ("a", "has space", "x" * 17, 'q"uote'):
            with self.assertRaises(WL.WhitelistError):
                WL.add("minecraft-x", bad)
        with mock.patch.object(WL.manager, "get_java_uuid", lambda n: None):
            with self.assertRaises(WL.WhitelistError):
                WL.add("minecraft-x", "Nobody123")

    def test_bedrock_allowlist_by_gamertag(self):
        self.java = False
        with open(os.path.join(self.d, "server.properties"), "w") as f:
            f.write("allow-list=true\n")
        WL.add("minecraft-b", "Gamer Tag")
        self.assertEqual(self.read("allowlist.json"), [{"ignoresPlayerLimit": False, "name": "Gamer Tag"}])
        self.assertEqual(WL.info("minecraft-b")["setting"], "allow-list")

    def test_running_java_goes_through_rcon(self):
        sent = []
        with mock.patch.object(WL.backups, "_is_active", lambda s: True), \
             mock.patch.object(WL.backups, "_java_rcon", lambda d: lambda cmd: sent.append(cmd) or "Added Steve to the whitelist"):
            r = WL.add("minecraft-x", "Steve")
        self.assertEqual(sent, ["whitelist add Steve"])
        self.assertIn("applies now", r["message"])
        self.assertFalse(os.path.exists(os.path.join(self.d, "whitelist.json")))   # the server writes it


if __name__ == "__main__":
    unittest.main()
