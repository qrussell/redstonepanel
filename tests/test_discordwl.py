"""1.28.1: Whitelist by Discord (core/discordwl.py and the bot's side, core/discordbot_wl.py).
Run: python -m unittest discover -s tests

Who may play where (off, anyone, roles, moderator approval, trust, exceptions, blocked, left),
keeping whitelists in step without ever touching names added by hand, linking names (limits,
one name per account), requests and approvals, invites (which one a new member used, trusted or
not, account age) and the bot's answers to the button, the form, /minecraft and Approve/Deny.
"""
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-discordwl-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import discordauth, discordbot as D, discordbot_wl as BW, discordwl as W  # noqa: E402

GUILD = "111111111111111111"
MEMBER, MOD, APPLICANT = "333333333333333333", "444444444444444444", "555555555555555555"
ALICE, BOB = "1100000000000000000", "1200000000000000000"
JAVA_SERVER = {"key": "minecraft-survival", "member": "local", "name": "minecraft-survival", "edition": "Java", "where": ""}
BEDROCK_SERVER = {"key": "minecraft-bedrock", "member": "local", "name": "minecraft-bedrock", "edition": "Bedrock", "where": ""}
ANARCHY = {"key": "minecraft-anarchy", "member": "local", "name": "minecraft-anarchy", "edition": "Java", "where": ""}


def fake_uuid(name):
    return "0000-" + name.lower()


class Fresh(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp(dir=TMP)
        self.log, self.changes, self.lists = [], [], {}
        self.servers = [JAVA_SERVER, BEDROCK_SERVER, ANARCHY]

        def change(s, action, name):
            names = self.lists.setdefault(s["key"], [])
            if action == "add":
                if name.lower() in (n.lower() for n in names):
                    return {"message": f"{name} is already on the whitelist."}
                names.append(name)
            else:
                if name.lower() not in (n.lower() for n in names):
                    raise W.WhitelistError(f"{name} isn't on the whitelist.")
                names[:] = [n for n in names if n.lower() != name.lower()]
            self.changes.append((action, s["key"], name))
            return {"message": "ok"}

        from core import manager
        for p in (mock.patch.object(W, "WL_FILE", os.path.join(d, "discord-whitelist.json")),
                  mock.patch.object(W, "_log", self.log.append),
                  mock.patch.object(W, "_after_change", lambda: None),
                  mock.patch.object(W, "_change", change),
                  mock.patch.object(W, "_servers", lambda: list(self.servers)),
                  mock.patch.object(D, "apply", lambda: None),
                  mock.patch.object(discordauth, "blocked", lambda did: did in self.blocked),
                  mock.patch.object(discordauth, "load", lambda: {"guild": GUILD}),
                  mock.patch.object(manager, "get_java_uuid", fake_uuid)):
            p.start()
            self.addCleanup(p.stop)
        self.blocked = set()
        W._link_times.clear()

    def setup(self, servers, **kw):
        W.save_settings(dict(enabled=True, servers=servers, **kw), actor="quentin")

    def link(self, did, java=None, bedrock=None, roles=(MEMBER,), who="alice"):
        return W.link(did, who, java, bedrock, roles=list(roles))


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
class Settings(Fresh):
    def test_saved_private_and_checked(self):
        self.setup({"minecraft-survival": {"mode": "roles", "roles": [MEMBER]}, "minecraft-anarchy": {"mode": "off"}})
        self.assertEqual(stat.S_IMODE(os.stat(W.WL_FILE).st_mode), 0o600)
        cfg = W.load()
        self.assertEqual(cfg["servers"], {"minecraft-survival": {"mode": "roles", "roles": [MEMBER], "skip": []},
                                          "minecraft-anarchy": {"mode": "off", "roles": [], "skip": []}})
        self.assertEqual(W.guild(cfg), GUILD)          # Sign in with Discord's server when none is set
        with self.assertRaises(W.WhitelistError):
            W.save_settings({"servers": {"minecraft-survival": {"mode": "roles", "roles": []}}})
        with self.assertRaises(W.WhitelistError):
            W.save_settings({"servers": {"minecraft-survival": {"mode": "everyone"}}})
        with self.assertRaises(W.WhitelistError):
            W.save_settings({"approvers": ["not-an-id"]})
        with self.assertRaises(W.WhitelistError):
            W.save_settings({"invites": {"bad code!": {"trust": "trusted"}}})
        self.assertTrue(any("turned Whitelist by Discord on" in l for l in self.log))

    def test_needs_a_discord_server(self):
        with mock.patch.object(discordauth, "load", lambda: {"guild": ""}):
            with self.assertRaises(W.WhitelistError):
                W.save_settings({"enabled": True})

    def test_bot_reconnects_only_when_intents_change(self):
        calls = []
        with mock.patch.object(D, "apply", lambda: calls.append(1)):
            self.setup({})
            W.save_settings({"min_age_days": 7})
            W.save_settings({"members_intent": True})
        self.assertEqual(len(calls), 2)

    def test_intents_and_commands_follow_the_setting(self):
        self.assertEqual(D.intents(), D.INTENTS)
        self.assertEqual([c["name"] for c in D.commands()], ["start"])
        self.setup({}, members_intent=True)
        self.assertEqual(D.intents(), D.INTENTS | D.INTENT_INVITES | D.INTENT_MEMBERS)
        self.assertEqual([c["name"] for c in D.commands()], ["start", "minecraft"])


# ---------------------------------------------------------------------------
# Who may play where
# ---------------------------------------------------------------------------
class MayPlay(Fresh):
    def test_each_choice(self):
        self.setup({"minecraft-survival": {"mode": "anyone"}, "minecraft-anarchy": {"mode": "roles", "roles": [MOD]},
                    "minecraft-bedrock": {"mode": "approval", "skip": [MOD]}})
        self.link(ALICE, java="Alice", roles=[MEMBER])
        cfg = W.load()
        self.assertEqual(W.may_play(cfg, ALICE, "minecraft-survival"), (True, "in the Discord server"))
        self.assertEqual(W.may_play(cfg, ALICE, "minecraft-anarchy"), (False, "no role for this server"))
        self.assertEqual(W.may_play(cfg, ALICE, "minecraft-bedrock"), (False, "waiting for approval"))
        self.assertEqual(W.may_play(cfg, ALICE, "unknown"), (False, "off"))
        W.member_update(ALICE, roles=[MOD])
        cfg = W.load()
        self.assertTrue(W.may_play(cfg, ALICE, "minecraft-anarchy")[0])
        self.assertEqual(W.may_play(cfg, ALICE, "minecraft-bedrock"), (True, "has a role that skips approval"))

    def test_every_server_row_is_the_default(self):
        self.setup({"*": {"mode": "roles", "roles": [MEMBER]}, "minecraft-anarchy": {"mode": "approval"}})
        self.link(ALICE, java="Alice")
        cfg = W.load()
        self.assertTrue(W.may_play(cfg, ALICE, "minecraft-survival")[0])
        self.assertFalse(W.may_play(cfg, ALICE, "minecraft-anarchy")[0])

    def test_a_server_can_stay_off_under_every_server(self):
        self.setup({"*": {"mode": "anyone"}, "minecraft-anarchy": {"mode": "off"}})
        self.link(ALICE, java="Alice")
        self.assertEqual(sorted(self.lists), ["minecraft-survival"])
        self.assertEqual(W.may_play(W.load(), ALICE, "minecraft-anarchy"), (False, "off"))

    def test_trust_approve_needs_approval_before_roles_count(self):
        self.setup({"minecraft-survival": {"mode": "roles", "roles": [MEMBER]}}, trust="approve")
        self.link(ALICE, java="Alice")
        self.assertEqual(W.may_play(W.load(), ALICE, "minecraft-survival"), (False, "waiting for approval"))
        W.decide(ALICE, "*", True, "discord:mod")
        self.assertTrue(W.may_play(W.load(), ALICE, "minecraft-survival")[0])

    def test_exceptions_blocked_and_leaving(self):
        self.setup({"minecraft-survival": {"mode": "anyone"}})
        self.link(ALICE, java="Alice")
        W.set_account(ALICE, {"exceptions": {"minecraft-survival": "deny", "minecraft-anarchy": "allow"}}, "quentin")
        cfg = W.load()
        self.assertEqual(W.may_play(cfg, ALICE, "minecraft-survival"), (False, "kept off by a moderator"))
        self.assertEqual(W.may_play(cfg, ALICE, "minecraft-anarchy"), (True, "allowed by a moderator"))
        W.set_account(ALICE, {"exceptions": {}}, "quentin")
        W.member_update(ALICE, member=False)
        self.assertEqual(W.may_play(W.load(), ALICE, "minecraft-survival"), (False, "not in the Discord server"))
        W.member_update(ALICE, roles=[MEMBER])
        self.blocked.add(ALICE)
        self.assertEqual(W.may_play(W.load(), ALICE, "minecraft-survival"), (False, "blocked"))

    def test_nothing_when_turned_off(self):
        self.setup({"minecraft-survival": {"mode": "anyone"}})
        self.link(ALICE, java="Alice")
        W.save_settings({"enabled": False})
        self.assertFalse(W.may_play(W.load(), ALICE, "minecraft-survival")[0])


# ---------------------------------------------------------------------------
# Keeping whitelists in step
# ---------------------------------------------------------------------------
class Sync(Fresh):
    def test_adds_each_edition_its_own_name(self):
        self.setup({"*": {"mode": "anyone"}})
        self.link(ALICE, java="Alice", bedrock="Alice Gamer")
        self.assertEqual(self.lists, {"minecraft-survival": ["Alice"], "minecraft-anarchy": ["Alice"], "minecraft-bedrock": ["Alice Gamer"]})
        self.assertEqual(W.load()["entries"]["minecraft-bedrock"], {"alice gamer": ALICE})
        self.assertTrue(any("Added Alice (discord:alice) to the whitelist of minecraft-survival" in l for l in self.log))

    def test_removes_only_what_it_added(self):
        self.lists["minecraft-survival"] = ["Steve"]                   # added by hand
        self.setup({"minecraft-survival": {"mode": "roles", "roles": [MEMBER]}})
        self.link(BOB, java="Steve", who="bob")              # already there by hand: never claimed
        self.link(ALICE, java="Alice")
        self.assertEqual(W.load()["entries"]["minecraft-survival"], {"alice": ALICE})
        W.member_update(ALICE, roles=[])
        W.member_update(BOB, roles=[])
        self.assertEqual(self.lists["minecraft-survival"], ["Steve"])
        self.assertNotIn("minecraft-survival", W.load()["entries"])
        self.assertTrue(any("Removed Alice" in l and "no role for this server" in l for l in self.log))

    def test_renamed_link_removes_the_old_name(self):
        self.setup({"minecraft-survival": {"mode": "anyone"}})
        self.link(ALICE, java="Alice")
        self.link(ALICE, java="Alice2")
        self.assertEqual(self.lists["minecraft-survival"], ["Alice2"])

    def test_turning_off_removes_its_names(self):
        self.setup({"minecraft-survival": {"mode": "anyone"}})
        self.link(ALICE, java="Alice")
        W.save_settings({"enabled": False})
        W.sync()
        self.assertEqual(self.lists["minecraft-survival"], [])

    def test_forget_and_rename_server(self):
        self.setup({"minecraft-survival": {"mode": "anyone"}})
        self.link(ALICE, java="Alice")
        W.rename_server("minecraft-survival", "minecraft-survival2")
        cfg = W.load()
        self.assertIn("minecraft-survival2", cfg["servers"])
        self.assertIn("minecraft-survival2", cfg["entries"])
        W.forget_server("minecraft-survival2")
        self.assertNotIn("minecraft-survival2", W.load()["entries"])

    def test_forget_account_takes_its_names_off(self):
        self.setup({"minecraft-survival": {"mode": "anyone"}})
        self.link(ALICE, java="Alice")
        W.set_account(ALICE, {"forget": True}, "quentin")
        self.assertEqual(self.lists["minecraft-survival"], [])
        self.assertEqual(W.load()["links"], {})


# ---------------------------------------------------------------------------
# Linking, requests, approvals
# ---------------------------------------------------------------------------
class Link(Fresh):
    def test_names_are_checked(self):
        self.setup({"minecraft-survival": {"mode": "anyone"}})
        for bad in ("ab", "has space", "x" * 17):
            with self.assertRaises(W.WhitelistError):
                self.link(ALICE, java=bad)
        with mock.patch("core.manager.get_java_uuid", lambda n: None):
            with self.assertRaises(W.WhitelistError):
                self.link(ALICE, java="Nobody")
        with self.assertRaises(W.WhitelistError):
            self.link(ALICE)

    def test_one_name_one_account(self):
        self.setup({"minecraft-survival": {"mode": "anyone"}})
        self.link(ALICE, java="Alice")
        with self.assertRaises(W.WhitelistError) as e:
            self.link(BOB, java="alice", who="bob")      # same uuid
        self.assertIn("another Discord account", str(e.exception))

    def test_limit_per_hour(self):
        self.setup({"minecraft-survival": {"mode": "anyone"}})
        for i in range(W.LINKS_PER_HOUR):
            self.link(ALICE, java=f"Alice{i}")
        with self.assertRaises(W.WhitelistError):
            self.link(ALICE, java="Alice9")

    def test_blocked_and_off(self):
        self.setup({"minecraft-survival": {"mode": "anyone"}})
        self.blocked.add(ALICE)
        with self.assertRaises(W.WhitelistError):
            self.link(ALICE, java="Alice")
        W.save_settings({"enabled": False})
        with self.assertRaises(W.WhitelistError):
            self.link(BOB, java="Bob")

    def test_ask_and_decide_per_server(self):
        self.setup({"minecraft-anarchy": {"mode": "approval"}})
        self.link(ALICE, java="Alice")
        _, play, ask = W.status_for(ALICE)
        self.assertEqual((play, ask), ([], [("minecraft-anarchy", "minecraft-anarchy")]))
        self.assertTrue(W.ask(ALICE, "minecraft-anarchy"))
        self.assertFalse(W.ask(ALICE, "minecraft-anarchy"))
        self.assertEqual(W.public()["waiting"], 1)
        self.assertEqual(W.decide(ALICE, "minecraft-anarchy", True, "discord:mod"), "Approved by discord:mod.")
        self.assertEqual(self.lists["minecraft-anarchy"], ["Alice"])
        self.assertEqual(W.public()["waiting"], 0)
        with self.assertRaises(W.WhitelistError):
            W.ask(BOB, "minecraft-anarchy")

    def test_turned_down_lets_nobody_in(self):
        self.setup({"minecraft-anarchy": {"mode": "approval"}})
        self.link(ALICE, java="Alice")
        W.ask(ALICE, "minecraft-anarchy")
        W.decide(ALICE, "minecraft-anarchy", False, "discord:mod")
        self.assertEqual(self.lists.get("minecraft-anarchy", []), [])
        self.assertTrue(any("turned down discord:alice" in l for l in self.log))

    def test_who_may_approve(self):
        cfg = dict(W.load(), approvers=[MOD])
        self.assertTrue(W.may_approve(cfg, [MOD]))
        self.assertTrue(W.may_approve(cfg, [], is_admin=True))
        self.assertFalse(W.may_approve(cfg, [MEMBER]))


class Invites(Fresh):
    def test_trusted_invite_approves_and_gives_a_role(self):
        self.setup({"minecraft-survival": {"mode": "roles", "roles": [MEMBER]}}, trust="approve",
                   invites={"friends": {"trust": "trusted", "role": MEMBER}, "reddit": {"trust": "approval"}})
        self.assertEqual(W.joined(ALICE, "alice", "friends"), ("trusted", MEMBER))
        self.link(ALICE, java="Alice")
        self.assertEqual(self.lists["minecraft-survival"], ["Alice"])
        self.assertEqual(W.load()["links"][ALICE]["approved_by"], "invite friends")
        self.assertEqual(W.joined(BOB, "bob", "reddit"), ("approval", ""))
        self.assertEqual(W.joined(BOB, "bob", None), ("approval", ""))

    def test_young_accounts_need_approval(self):
        self.setup({}, invites={"friends": {"trust": "trusted"}}, min_age_days=30)
        now = time.time()
        young = str(int((now * 1000 - W.DISCORD_EPOCH_MS - 86400000)) << 22)    # a day old
        old = str(int((now * 1000 - W.DISCORD_EPOCH_MS - 90 * 86400000)) << 22)
        self.assertAlmostEqual(W.account_age_days(young, now), 1, places=2)
        self.assertEqual(W.invite_trust(W.load(), "friends", young, now)[0], "approval")
        self.assertEqual(W.invite_trust(W.load(), "friends", old, now)[0], "trusted")

    def test_which_invite_a_member_used(self):
        counts = [{"a": 1, "b": 5}]
        with mock.patch.object(BW, "_uses", lambda g, t: dict(counts[0])):
            BW._invites.clear()
            BW._invites[GUILD] = {"a": 1, "b": 5}
            counts[0] = {"a": 2, "b": 5}
            self.assertEqual(BW.which_invite(GUILD, "t"), "a")
            counts[0] = {"b": 5}                        # a's last use deleted it
            self.assertEqual(BW.which_invite(GUILD, "t"), "a")
            counts[0] = {"b": 6, "c": 1}                # two went up: can't tell
            self.assertIsNone(BW.which_invite(GUILD, "t"))


# ---------------------------------------------------------------------------
# The bot's answers
# ---------------------------------------------------------------------------
def interaction(kind, did=ALICE, roles=(MEMBER,), perms="0", **data):
    return {"type": kind, "guild_id": GUILD, "id": "1", "token": "itoken", "data": data,
            "member": {"roles": list(roles), "permissions": perms, "user": {"id": did, "username": "alice"}},
            "message": {"content": "<@1> asks"}}


class Bot(Fresh):
    def setUp(self):
        super().setUp()
        self.edits, self.api = [], []
        for p in (mock.patch.object(BW, "_edit", lambda t, text, comps=None: self.edits.append((text, comps))),
                  mock.patch.object(BW, "notify_moderators", lambda did, key: self.api.append(("notify", did, key))),
                  mock.patch.object(BW, "_approve_roles", lambda did, cfg: self.api.append(("roles", did)))):
            p.start()
            self.addCleanup(p.stop)

    def test_off_and_other_servers(self):
        r, _ = BW.answer(interaction(3, custom_id=BW.LINK_ID))
        self.assertIn("isn't turned on", r["data"]["content"])
        self.setup({})
        d = interaction(3, custom_id=BW.LINK_ID)
        d["guild_id"] = "999999999999999999"
        self.assertIn("community's Discord", BW.answer(d)[0]["data"]["content"])

    def test_button_opens_the_form_and_the_form_links(self):
        self.setup({"minecraft-survival": {"mode": "anyone"}, "minecraft-anarchy": {"mode": "approval"}})
        r, after = BW.answer(interaction(3, custom_id=BW.LINK_ID))
        self.assertEqual((r["type"], r["data"]["custom_id"], after), (BW.MODAL, BW.FORM_ID, None))
        form = interaction(5, custom_id=BW.FORM_ID, components=[
            {"type": 1, "components": [{"type": 4, "custom_id": "java", "value": " Alice "}]},
            {"type": 1, "components": [{"type": 4, "custom_id": "bedrock", "value": ""}]}])
        r, after = D.answer(form)
        self.assertEqual(r, {"type": BW.DEFERRED, "data": {"flags": D.EPHEMERAL}})
        after()
        text, comps = self.edits[-1]
        self.assertIn("Linked **Alice**. You can play on **minecraft-survival**", text)
        self.assertEqual(comps[0]["components"][0]["custom_id"], "rpwl:ask:minecraft-anarchy")
        self.assertEqual(self.lists["minecraft-survival"], ["Alice"])

    def test_slash_command(self):
        self.setup({"minecraft-survival": {"mode": "anyone"}})
        r, _ = D.answer(interaction(2, name="minecraft", options=[]))
        self.assertIn("/minecraft java:", r["data"]["content"])
        r, after = D.answer(interaction(2, name="minecraft", options=[{"name": "java", "value": "ab"}]))
        after()
        self.assertIn("isn't a Java player name", self.edits[-1][0])

    def test_approve_first_asks_the_moderators(self):
        self.setup({"minecraft-survival": {"mode": "roles", "roles": [MEMBER]}}, trust="approve", approvers=[MOD])
        _, after = D.answer(interaction(2, name="minecraft", options=[{"name": "java", "value": "Alice"}]))
        after()
        self.assertIn("A moderator will check", self.edits[-1][0])
        self.assertEqual(self.api, [("notify", ALICE, "*")])
        # A member can't press Approve; a moderator can
        r, _ = BW.answer(interaction(3, did=BOB, roles=[MEMBER], custom_id=f"rpwl:ok:{ALICE}:*"))
        self.assertIn("Only moderators", r["data"]["content"])
        r, _ = BW.answer(interaction(3, did=BOB, roles=[MOD], custom_id=f"rpwl:ok:{ALICE}:*"))
        self.assertEqual(r["type"], BW.UPDATE_MESSAGE)
        self.assertIn("Approved by discord:alice", r["data"]["content"])
        self.assertEqual(r["data"]["components"], [])
        self.assertEqual(self.lists["minecraft-survival"], ["Alice"])

    def test_discord_admins_may_approve(self):
        self.setup({"minecraft-anarchy": {"mode": "approval"}})
        self.link(ALICE, java="Alice")
        r, _ = BW.answer(interaction(3, did=BOB, roles=[], perms=str(BW.ADMINISTRATOR),
                                     custom_id=f"rpwl:no:{ALICE}:minecraft-anarchy"))
        self.assertIn("Turned down", r["data"]["content"])

    def test_ask_button(self):
        self.setup({"minecraft-anarchy": {"mode": "approval"}, "minecraft-survival": {"mode": "anyone"}})
        r, _ = BW.answer(interaction(3, custom_id="rpwl:ask:minecraft-anarchy"))
        self.assertIn("Link your Minecraft name first", r["data"]["content"])
        self.link(ALICE, java="Alice")
        r, _ = BW.answer(interaction(3, custom_id="rpwl:ask:minecraft-survival"))
        self.assertIn("already play there", r["data"]["content"])
        r, _ = BW.answer(interaction(3, custom_id="rpwl:ask:minecraft-anarchy"))
        self.assertIn("Asked", r["data"]["content"])
        r, _ = BW.answer(interaction(3, custom_id="rpwl:ask:minecraft-anarchy"))
        self.assertIn("already asked", r["data"]["content"])

    def test_member_events(self):
        self.setup({"minecraft-survival": {"mode": "roles", "roles": [MEMBER]}},
                   invites={"friends": {"trust": "trusted", "role": MEMBER}})
        calls = []
        with mock.patch.object(BW, "which_invite", lambda g, t: "friends"), \
                mock.patch.object(D, "api", lambda *a, **k: calls.append(a)):
            BW.event("GUILD_MEMBER_ADD", {"guild_id": GUILD, "user": {"id": ALICE, "username": "alice"},
                                          "roles": []}, "t")
        self.assertEqual(calls, [("PUT", f"/guilds/{GUILD}/members/{ALICE}/roles/{MEMBER}")])
        self.link(ALICE, java="Alice", roles=[MEMBER])
        self.assertEqual(self.lists["minecraft-survival"], ["Alice"])
        BW.event("GUILD_MEMBER_UPDATE", {"guild_id": GUILD, "user": {"id": ALICE}, "roles": []}, "t")
        self.assertEqual(self.lists["minecraft-survival"], [])
        BW.event("GUILD_MEMBER_UPDATE", {"guild_id": GUILD, "user": {"id": ALICE}, "roles": [MEMBER]}, "t")
        BW.event("GUILD_MEMBER_REMOVE", {"guild_id": GUILD, "user": {"id": ALICE}}, "t")
        self.assertEqual(self.lists["minecraft-survival"], [])
        self.assertTrue(any("left the Discord server" in l for l in self.log))


class Routes(Fresh):
    """The settings routes (called directly; admin-only through grants.ADMIN_PREFIXES like every
    /api/settings route)."""
    def setUp(self):
        super().setUp()
        try:
            import main
        except (ImportError, RuntimeError):
            self.skipTest("needs FastAPI")
        self.main = main
        for p in (mock.patch.object(BW, "channels", lambda g=None: [{"id": "1", "name": "general"}]),
                  mock.patch.object(BW, "invites", lambda g=None: [])):
            p.start()
            self.addCleanup(p.stop)
        self.req = mock.Mock()
        self.req.state.user = {"username": "quentin"}

    def test_save_account_and_decide(self):
        from fastapi import HTTPException
        m = self.main
        out = m.discord_whitelist_save_api(self.req, m.DiscordWhitelistUpdate(
            enabled=True, servers={"minecraft-anarchy": {"mode": "approval"}}))
        self.assertTrue(out["enabled"])
        self.assertEqual(out["channels"], [{"id": "1", "name": "general"}])
        self.assertEqual(out["guild_used"], GUILD)
        self.assertIn("bot_invite", out)
        with self.assertRaises(HTTPException):
            m.discord_whitelist_save_api(self.req, m.DiscordWhitelistUpdate(trust="maybe"))
        self.link(ALICE, java="Alice")
        W.ask(ALICE, "minecraft-anarchy")
        out = m.discord_whitelist_decide_api(self.req, ALICE, m.DiscordWhitelistDecision(server="minecraft-anarchy",
                                                                                         approve=True))
        self.assertEqual(out["accounts"][0]["servers"], ["minecraft-anarchy"])
        out = m.discord_whitelist_account_api(self.req, ALICE, m.DiscordWhitelistAccount(
            exceptions={"minecraft-anarchy": "deny"}))
        self.assertEqual(out["accounts"][0]["servers"], [])
        with self.assertRaises(HTTPException):
            m.discord_whitelist_account_api(self.req, BOB, m.DiscordWhitelistAccount(forget=True))


if __name__ == "__main__":
    unittest.main()
