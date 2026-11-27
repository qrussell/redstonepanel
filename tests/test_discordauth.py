"""Sign in with Discord (1.28): the OAuth2 flow, Discord roles to permissions, panel sessions,
linked accounts, and join pages that need sign-in (panel accounts and Discord) with See, Start
and Stop by permission. Discord itself is a stand-in (discordauth._http)."""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import urllib.parse
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-discordauth-")
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import auth                  # noqa: E402
from core import cloudflare as CF     # noqa: E402
from core import discordauth as D     # noqa: E402
from core import discordbot           # noqa: E402
from core import grants               # noqa: E402
from core import joinpage as J        # noqa: E402
from core import joinsite as S        # noqa: E402
from core import manager              # noqa: E402
from core import portforward          # noqa: E402
from core import settings as P        # noqa: E402
from core import twofactor            # noqa: E402

try:
    import fastapi  # noqa: F401
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except (ImportError, RuntimeError):
    HAVE_FASTAPI = False

GUILD = "900000000000000001"
MEMBER, MOD = "900000000000000011", "900000000000000012"
ALICE, BOB, CAROL = "100000000000000001", "100000000000000002", "100000000000000003"
SECRET = "s" * 32
PANEL = "https://panel.example.org:8443"
JOIN = "https://play.example.org:8444"


class FakeDiscord:
    """Discord's OAuth2 and REST endpoints, as much as the panel uses."""

    def __init__(self):
        # code -> (user, member roles or None for "not in the server")
        self.codes = {"alice": ({"id": ALICE, "username": "alice", "global_name": "Alice", "mfa_enabled": True}, [MEMBER]),
                      "bob": ({"id": BOB, "username": "bob", "mfa_enabled": False}, [MEMBER, MOD]),
                      "carol": ({"id": CAROL, "username": "carol", "mfa_enabled": True}, None)}
        self.bot_roles = {}             # discord id -> roles (or None) as the bot sees them now
        self.calls, self.revoked, self.redirects = [], [], []
        self.fail_token = None

    def __call__(self, method, url, data=None, headers=None, timeout=10):
        self.calls.append((method, url))
        path = url[len(D.API):]
        auth_h = (headers or {}).get("Authorization", "")
        if path == "/oauth2/token" and method == "POST":
            assert auth_h.startswith("Basic ")
            if self.fail_token:
                return 401, {"error": self.fail_token}
            self.redirects.append(data["redirect_uri"])
            if data.get("code") not in self.codes:
                return 400, {"error": "invalid_grant"}
            return 200, {"access_token": "at-" + data["code"], "token_type": "Bearer"}
        if path == "/oauth2/token/revoke":
            self.revoked.append(data["token"])
            return 200, None
        if auth_h.startswith("Bearer at-"):
            user, roles = self.codes[auth_h[len("Bearer at-"):]]
            if path == "/users/@me":
                return 200, user
            if path == f"/users/@me/guilds/{GUILD}/member":
                return (404, {"message": "Unknown Guild"}) if roles is None else (200, {"roles": roles})
        if auth_h.startswith("Bot ") and path.startswith(f"/guilds/{GUILD}/members/"):
            did = path.rsplit("/", 1)[1]
            if did not in self.bot_roles:
                return 500, None
            roles = self.bot_roles[did]
            return (404, {"message": "Unknown Member"}) if roles is None else (200, {"roles": roles})
        return 404, None


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(dir=TMP)
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.fake = FakeDiscord()
        self.log = []
        self.bot_online = False
        self.settings = dict(P.DEFAULTS, join_enabled=True)
        self.summaries = {"minecraft-survival": {"name": "minecraft-survival", "edition": "Java", "loader": "",
                                                 "minecraft": "1.21.11", "status": "online", "port": "25565",
                                                 "motd": "Survival", "players": {"online": 2, "max": 20, "names": []}}}
        d = self.dir
        patches = [
            mock.patch.object(D, "SIGNIN_FILE", os.path.join(d, "discord-signin.json")),
            mock.patch.object(D, "_http", self.fake),
            mock.patch.dict(D._states, clear=True),
            mock.patch.dict(D._member_cache, clear=True),
            mock.patch.object(D, "_bot_for", lambda cfg: "bot-token" if self.bot_online and cfg["guild"] else None),
            mock.patch.object(discordbot, "DISCORD_FILE", os.path.join(d, "discord.json")),
            mock.patch.object(auth, "USERS_FILE", os.path.join(d, "users.json")),
            mock.patch.object(auth, "SECRET_FILE", os.path.join(d, "secret.key")),
            mock.patch.object(auth, "_secret", None),
            mock.patch.object(auth, "FAILS_FILE", os.path.join(d, "fails.json")),
            mock.patch.dict(auth._fails, clear=True),
            mock.patch.object(auth, "linux_account_exists", lambda name: name == "quent"),
            mock.patch.object(auth, "linux_user_allowed", lambda name: name == "quent"),
            mock.patch.object(auth, "pam_authenticate", self._no_pam),
            mock.patch.object(twofactor, "TOTP_FILE", os.path.join(d, "totp.json")),
            mock.patch.dict(twofactor._cache, {"mtime": None, "data": None}),
            mock.patch.object(grants, "GRANTS_FILE", os.path.join(d, "grants.json")),
            mock.patch.object(manager, "DATA_DIR", d),
            mock.patch.object(manager, "write_log", lambda m: self.log.append(m)),
            mock.patch.object(P, "load", lambda: dict(self.settings)),
            mock.patch.object(J, "summary", lambda key, fresh=False: self.summaries[key]),
            mock.patch.object(portforward, "load", lambda: {"public_address": "play.example.org"}),
            mock.patch.object(CF, "refresh_if_old", lambda: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        S._rate.clear()
        J._starts.clear()
        J._starts_by_ip.clear()
        J._stops.clear()
        auth.create_panel_user("alex", "alex-password", "admin")
        auth.create_panel_user("bob", "bob-password", "user")
        auth.create_panel_user("pat", "pat-password", "user")

    def _no_pam(self, *a):
        raise AssertionError("PAM must not be asked")

    def configure(self, **kw):
        new = dict({"client_id": "123456789012345678", "secret": SECRET, "panel_url": PANEL, "join_url": JOIN,
                    "guild": GUILD, "enabled": True,
                    "roles": [{"role": MEMBER, "servers": ["minecraft-survival"], "perms": ["view", "power"]}]}, **kw)
        return D.save_settings(new, "alex")

    def discord(self, code, purpose="panel", base=PANEL, **kw):
        state, url = D.begin(purpose, base, **kw)
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertEqual(q["state"], [state])
        self.assertEqual(q["scope"], ["identify guilds.members.read"])
        return D.finish(state, state, code)


class Settings(Base):
    def test_secret_stays_here(self):
        pub = self.configure()
        self.assertNotIn(SECRET, json.dumps(pub))
        self.assertEqual(pub["secret_hint"], SECRET[-4:])
        self.assertEqual(pub["redirects"], [PANEL + "/api/auth/discord/callback", JOIN + "/join/_discord/callback"])
        self.assertEqual(oct(os.stat(D.SIGNIN_FILE).st_mode & 0o777), "0o600")
        # an empty secret keeps the saved one; forgetting turns it off
        self.assertTrue(D.save_settings({"secret": ""}, "alex")["has_secret"])
        pub = D.save_settings({"forget_secret": True}, "alex")
        self.assertFalse(pub["has_secret"] or pub["enabled"])

    def test_refused_settings(self):
        for bad in ({"secret": "short"}, {"secret": "x" * 20 + " ; rm"}, {"client_id": "abc"},
                    {"panel_url": "ftp://x"}, {"panel_url": "https://x.example/path"},
                    {"roles": [{"role": "nope", "servers": ["*"], "perms": ["view"]}]},
                    {"roles": [{"role": MEMBER, "servers": ["../etc"], "perms": ["view"]}]},
                    {"roles": [{"role": MEMBER, "servers": ["*"], "perms": ["root"]}]},
                    {"roles": [{"role": MEMBER, "servers": ["*"], "perms": []}]}):
            with self.assertRaises(D.DiscordAuthError, msg=bad):
                self.configure(**bad)
        with self.assertRaises(D.DiscordAuthError):
            D.save_settings({"enabled": True}, "alex")                     # nothing set up
        with self.assertRaises(D.DiscordAuthError):
            self.configure(guild="")                                         # roles need the Discord server
        self.assertEqual(D.clean_base("Panel.Example.org:8443/", "x"), "https://panel.example.org:8443")

    def test_grants_from_roles_add_up(self):
        self.configure(roles=[{"role": "*", "servers": ["*"], "perms": ["view"]},
                              {"role": MOD, "servers": ["minecraft-survival"], "perms": ["power", "console"]}])
        self.assertEqual(D.grants_for([MEMBER]), {"*": ["view"]})
        self.assertEqual(D.grants_for([MOD]), {"*": ["view"], "minecraft-survival": ["view", "console", "power"]})
        self.assertEqual(D.grants_for([MOD], member=False), {})
        grants.forget_server("minecraft-survival")
        self.assertEqual(D.grants_for([MOD]), {"*": ["view"]})

    def test_moved_server_keeps_its_rows(self):
        self.configure()
        grants.rename_server("minecraft-survival", "n-abcd/minecraft-survival")
        self.assertEqual(D.load()["roles"][0]["servers"], ["n-abcd/minecraft-survival"])


class Flow(Base):
    def test_sign_in_reads_who_and_drops_the_token(self):
        self.configure()
        rec, who = self.discord("alice")
        self.assertEqual(rec["purpose"], "panel")
        self.assertEqual(who, {"id": ALICE, "name": "alice", "display": "Alice", "mfa": True, "member": True,
                               "roles": [MEMBER]})
        self.assertEqual(self.fake.revoked, ["at-alice"])
        self.assertEqual(self.fake.redirects, [PANEL + "/api/auth/discord/callback"])
        _, who = self.discord("carol")
        self.assertFalse(who["member"])

    def test_state_is_one_use_and_tied_to_the_browser(self):
        self.configure()
        state, _ = D.begin("panel", PANEL)
        with self.assertRaises(D.DiscordAuthError):
            D.finish(state, "someone-elses", "alice")
        state, _ = D.begin("panel", PANEL)
        D.finish(state, state, "alice")
        with self.assertRaises(D.DiscordAuthError):
            D.finish(state, state, "alice")                                 # used
        state, _ = D.begin("panel", PANEL)
        D._states[state]["exp"] = time.time() - 1
        with self.assertRaises(D.DiscordAuthError):
            D.finish(state, state, "alice")                                 # expired
        state, _ = D.begin("panel", PANEL)
        with self.assertRaisesRegex(D.DiscordAuthError, "cancelled"):
            D.finish(state, state, "", "access_denied")
        state, _ = D.begin("panel", PANEL)
        self.fake.fail_token = "invalid_client"
        with self.assertRaisesRegex(D.DiscordAuthError, "client ID or secret"):
            D.finish(state, state, "alice")

    def test_off_means_no_flow(self):
        with self.assertRaises(D.DiscordAuthError):
            D.begin("panel", PANEL)
        self.configure(enabled=False)
        with self.assertRaises(D.DiscordAuthError):
            D.begin("panel", PANEL)
        D.begin("join", JOIN)                                                # join pages only need the app set up

    def test_who_may_sign_in_on_roles(self):
        self.configure(require_mfa=True)
        ident = D.panel_identity(self.discord("alice")[1])
        self.assertEqual((ident["u"], ident["src"], ident["dn"], ident["r"]), (ALICE, "discord", "alice", [MEMBER]))
        with self.assertRaisesRegex(D.DiscordAuthError, "two-factor"):
            D.panel_identity(self.discord("bob")[1])                          # no Discord 2FA
        with self.assertRaisesRegex(D.DiscordAuthError, "not in"):
            D.panel_identity(self.discord("carol")[1])
        self.configure(roles=[{"role": MOD, "servers": ["*"], "perms": ["view"]}])
        with self.assertRaisesRegex(D.DiscordAuthError, "role"):
            D.panel_identity(self.discord("alice")[1])
        D.set_blocked(BOB, True, "alex")
        with self.assertRaisesRegex(D.DiscordAuthError, "can't sign in"):
            D.panel_identity(self.discord("bob")[1])

    def test_session_is_rechecked_and_never_admin(self):
        self.configure()
        ident = D.panel_identity(self.discord("alice")[1])
        token = auth.make_session_token(ident)
        user = auth.read_session_token(token)
        self.assertEqual((user["username"], user["source"], user["role"]), ("discord:alice", "discord", "user"))
        self.assertEqual(user["grants"], {"minecraft-survival": ["view", "power"]})
        self.assertTrue(grants.can(user, "power", "minecraft-survival"))
        self.assertFalse(grants.can(user, "files", "minecraft-survival"))
        self.assertTrue(grants.limited(user))
        # no bot: the roles from sign-in last the session
        self.fake.bot_roles[ALICE] = []
        self.assertIsNotNone(auth.read_session_token(token))
        # with the bot in the server, losing the role (or leaving) signs out at the next check
        self.bot_online = True
        D._member_cache.clear()
        self.assertIsNone(auth.read_session_token(token))
        self.fake.bot_roles[ALICE] = [MEMBER]
        D._member_cache.clear()
        self.assertIsNotNone(auth.read_session_token(token))
        self.fake.bot_roles[ALICE] = None
        D._member_cache.clear()
        self.assertIsNone(auth.read_session_token(token))
        # blocking, and turning sign-in off, end it too
        self.fake.bot_roles[ALICE] = [MEMBER]
        D._member_cache.clear()
        D.set_blocked(ALICE, True, "alex")
        self.assertIsNone(auth.read_session_token(token))
        D.set_blocked(ALICE, False, "alex")
        self.assertIsNone(auth.read_session_token(token))                    # unblocking doesn't revive old sessions
        token = auth.make_session_token(D.panel_identity(self.discord("alice")[1]))
        self.assertIsNotNone(auth.read_session_token(token))
        self.configure(enabled=False)
        self.assertIsNone(auth.read_session_token(token))

    def test_linking(self):
        self.configure()
        rec, who = self.discord("bob", "link", account="panel:alex")
        D.link("panel:alex", who, "alex")
        self.assertEqual(D.linked_account(BOB), "panel:alex")
        self.assertEqual(D.link_of("panel:alex"), (BOB, "bob"))
        with self.assertRaises(D.DiscordAuthError):
            D.link("panel:pat", who, "pat")                                 # one account per Discord ID
        ident = auth.linked_identity("panel:alex")
        self.assertEqual((ident["u"], ident["src"]), ("alex", "panel"))
        self.assertNotIn("m", ident)                                          # one step, like a password
        auth.delete_panel_user("alex")
        self.assertIsNone(D.linked_account(BOB))


@unittest.skipUnless(HAVE_FASTAPI, "needs FastAPI")
class Routes(Base):
    def setUp(self):
        super().setUp()
        import main
        self.main = main
        for p in (mock.patch.object(main.panel_settings, "same_origin", lambda h: True),):
            p.start()
            self.addCleanup(p.stop)

    def client(self, base=PANEL):
        return TestClient(self.main.app, base_url=base, follow_redirects=False)

    def go(self, c, code, start="/api/auth/discord/start"):
        r = c.get(start) if start.startswith("/") else None
        self.assertEqual(r.status_code, 303, r.text)
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(r.headers["location"]).query)
        self.assertTrue(r.headers["location"].startswith(D.AUTHORIZE))
        return c.get("/api/auth/discord/callback", params={"state": q["state"][0], "code": code})

    def test_button_and_start(self):
        c = self.client()
        self.assertFalse(c.get("/api/auth/discord/available").json()["panel"])
        r = c.get("/api/auth/discord/start")
        self.assertIn("discord=", r.text)                                    # off: back to the sign-in page
        self.configure()
        self.assertTrue(c.get("/api/auth/discord/available").json()["panel"])
        other = self.client("https://192.168.1.5:8443")
        r = other.get("/api/auth/discord/start")
        self.assertEqual(r.headers["location"], PANEL + "/api/auth/discord/start")
        r = c.get("/api/auth/discord/start")
        self.assertIn("rp_dstate=", r.headers["set-cookie"])
        self.assertIn("samesite=lax", r.headers["set-cookie"].lower())

    def test_sign_in_on_roles(self):
        self.configure()
        c = self.client()
        r = self.go(c, "alice")
        self.assertEqual(r.status_code, 200)
        self.assertIn("/static/index.html", r.text)
        me = c.get("/api/auth/me").json()
        self.assertEqual((me["username"], me["source"], me["role"]), ("discord:alice", "discord", "user"))
        self.assertEqual(me["grants"], {"minecraft-survival": ["view", "power"]})
        self.assertEqual(c.get("/api/users").status_code, 403)
        self.assertEqual(c.get("/api/settings/discord-signin").status_code, 403)
        self.assertEqual(c.post("/api/auth/password", json={"current_password": "x", "new_password": "y" * 9}).status_code, 400)
        self.assertEqual(c.get("/api/auth/2fa").status_code, 400)
        self.assertTrue(any("discord:alice" in m and "signed in with Discord" in m for m in self.log))

    def test_join_rules_offer_roles_without_the_bot(self):
        self.configure()
        self.go(self.client(), "alice")                     # alice's roles are remembered at sign-in
        req = mock.Mock(headers={"host": "panel.example.org:8443"})
        roles = self.main._join_view(req)["signin"]["roles"]
        ids = [r["id"] for r in roles]
        self.assertIn(MEMBER, ids)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertNotIn(D.ANY_MEMBER, ids)

    def test_refused_sign_ins_count(self):
        self.configure()
        for _ in range(auth.MAX_FAILS):
            r = self.go(self.client(), "carol")                              # not in the Discord server
            self.assertIn("discord=", r.text)
        r = self.client().get("/api/auth/discord/start")
        self.assertIn("Too%20many", r.text)

    def test_link_then_sign_in_as_the_account_with_its_code(self):
        self.configure(roles=[])
        alex = self.client()
        self.assertEqual(alex.post("/api/auth/login", json={"username": "alex", "password": "alex-password"}).status_code, 200)
        r = alex.post("/api/auth/discord/link")
        self.assertEqual(r.status_code, 200, r.text)
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(r.json()["url"]).query)
        r = alex.get("/api/auth/discord/callback", params={"state": q["state"][0], "code": "bob"})
        self.assertIn("discord-linked", r.text)
        self.assertEqual(alex.get("/api/auth/discord/link").json()["linked"], "bob")
        # Sign in with Discord is now alex, an admin
        c = self.client()
        self.go(c, "bob")
        me = c.get("/api/auth/me").json()
        self.assertEqual((me["username"], me["source"], me["role"]), ("alex", "panel", "admin"))
        # with two-factor on, Discord is only the first step
        key = twofactor.account_key("panel", "alex")
        secret = twofactor.begin_setup(key)
        step = int(time.time() // 30)
        self.assertTrue(twofactor.confirm_setup(key, twofactor.code_at(secret, step)))
        c = self.client()
        r = self.go(c, "bob")
        self.assertIn("#code", r.text)
        self.assertEqual(c.get("/api/auth/me").status_code, 401)
        r = c.post("/api/auth/login/code", json={"code": twofactor.code_at(secret, step + 1)})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(c.get("/api/auth/me").json()["username"], "alex")

    def test_link_needs_the_panel_address(self):
        self.configure()
        c = self.client("https://192.168.1.5:8443")
        c.post("/api/auth/login", json={"username": "alex", "password": "alex-password"})
        r = c.post("/api/auth/discord/link")
        self.assertEqual(r.status_code, 400)
        self.assertIn(PANEL, r.json()["detail"])

    def test_discord_cookie_isnt_a_join_cookie_and_back(self):
        self.configure()
        ident = D.panel_identity(self.discord("alice")[1])
        panel_cookie = auth.make_session_token(ident)
        self.assertEqual(S.read_visitor(panel_cookie), (None, False))
        join_cookie = S.make_visitor_cookie({"k": "panel", "u": "alex", "v": 1})
        self.assertIsNone(auth.read_session_token(join_cookie))


class JoinPages(Base):
    def setUp(self):
        super().setUp()
        self.origin = {"origin": JOIN, "host": "play.example.org:8444"}

    def page(self, **kw):
        return J.save_page("local", "minecraft-survival", dict({"enabled": True}, **kw), "alex")

    def req(self, path, method="GET", cookie=None, body=None, origin=None, host="play.example.org:8444", query="",
            start=None):
        return S.handle(method, path, "203.0.113.9", cookie=cookie, body=body, origin=origin, host=host, query=query,
                        start_header=start)

    def sign_in(self, user, password, next_path="/join/survival"):
        body = urllib.parse.urlencode({"username": user, "password": password, "next": next_path}).encode()
        return self.req("/join/_signin", "POST", body=body, origin=JOIN)

    @staticmethod
    def cookie_of(answer, name=S.VISITOR_COOKIE):
        for c in answer.cookies:
            if c.startswith(name + "="):
                return f"{name}={c.split(';')[0].split('=', 1)[1]}"
        return None

    def test_open_page_is_as_before(self):
        self.page()
        a = self.req("/join/survival")
        self.assertIn(b"play.example.org", a.body)
        self.assertEqual(a.cookies, [])
        self.assertEqual(self.req("/join/_signin", "POST", body=b"", origin=JOIN).status, 404)

    def test_signed_in_page_with_a_panel_account(self):
        self.page(access="signed", signin_panel=True, allow_start=True)
        a = self.req("/join/survival")
        self.assertNotIn(b"play.example.org", a.body)
        self.assertIn(b'action="/join/_signin"', a.body)
        st = json.loads(self.req("/join/survival/status.json").body)
        self.assertIsNone(st["players"])
        self.assertFalse(st["can_start"])
        # a form from another site, and this computer's own accounts, are refused
        body = urllib.parse.urlencode({"username": "bob", "password": "bob-password", "next": "/join/survival"}).encode()
        self.assertEqual(self.req("/join/_signin", "POST", body=body, origin="https://evil.example").status, 400)
        self.assertEqual(self.sign_in("quent", "whatever").status, 400)
        self.assertEqual(self.sign_in("bob", "wrong").status, 400)
        a = self.sign_in("bob", "bob-password")
        self.assertEqual((a.status, a.extra["Location"]), (303, "/join/survival"))
        cookie = self.cookie_of(a)
        self.assertIn("HttpOnly", a.cookies[0])
        page = self.req("/join/survival", cookie=cookie)
        self.assertIn(b"play.example.org", page.body)
        self.assertIn(b"Signed in as <b>bob</b>", page.body)
        # sign-out clears the cookie; a deleted account's cookie stops working
        out = self.req("/join/_signout", "POST", body=b"next=/join/survival", origin=JOIN)
        self.assertIn("Max-Age=0", out.cookies[0])
        auth.delete_panel_user("bob")
        self.assertNotIn(b"play.example.org", self.req("/join/survival", cookie=cookie).body)

    def test_two_factor_on_the_join_page(self):
        key = twofactor.account_key("panel", "bob")
        with mock.patch.object(twofactor, "is_enabled", lambda k: k == key), \
                mock.patch.object(twofactor, "verify", lambda k, code: "app" if code == "123456" else None):
            self.page(access="signed", signin_panel=True)
            a = self.sign_in("bob", "bob-password")
            self.assertIn(b"/join/_signin/code", a.body)
            ticket = self.cookie_of(a, S.CODE_COOKIE)
            bad = self.req("/join/_signin/code", "POST", cookie=ticket, body=b"code=000000&next=/join/survival", origin=JOIN)
            self.assertIn(b"wrong", bad.body)
            ok = self.req("/join/_signin/code", "POST", cookie=ticket, body=b"code=123456&next=/join/survival", origin=JOIN)
            self.assertEqual(ok.status, 303)
            self.assertIn(b"play.example.org", self.req("/join/survival", cookie=self.cookie_of(ok)).body)

    def test_start_only_on_the_users_page_starts_from_the_page_too(self):
        grants.set_user("pat", {"minecraft-survival": ["view", "start"]})
        self.page(access="permission", signin_panel=True,
                  rules=[{"who": "panel_perms", "see": True, "start": True, "stop": True}])
        pat = self.cookie_of(self.sign_in("pat", "pat-password"))
        self.assertIn(b"play.example.org", self.req("/join/survival", cookie=pat).body)
        self.assertNotIn(b'id="stop"', self.req("/join/survival", cookie=pat).body)
        self.assertEqual(self.req("/join/survival/stop", "POST", cookie=pat, start="1").status, 403)

    def test_where_a_form_came_from(self):
        # Browsers send "Origin: null" under Referrer-Policy no-referrer; Sec-Fetch-Site still says
        def head_of(**h):
            return lambda name: h.get(name.decode().replace("-", "_"))
        self.assertEqual(S._origin_of(head_of(origin="http://localhost:18444")), "http://localhost:18444")
        self.assertEqual(S._origin_of(head_of(origin="null", sec_fetch_site="same-origin", host="localhost:18444")),
                         "//localhost:18444")
        self.assertIsNone(S._origin_of(head_of(origin="null", sec_fetch_site="cross-site", host="localhost:18444")))
        self.assertTrue(S._same_site(S._origin_of(head_of(origin="null", sec_fetch_site="same-origin",
                                                          host="localhost:18444")), "localhost:18444"))
        self.assertEqual(S.SECURITY_HEADERS["Referrer-Policy"], "same-origin")

    def test_permissions_see_start_stop(self):
        grants.set_user("pat", {"minecraft-survival": ["view", "power"]})
        self.page(access="permission", signin_panel=True,
                  rules=[{"who": "panel:bob", "see": True, "start": False, "stop": False},
                         {"who": "panel_perms", "see": True, "start": True, "stop": True}])
        bob = self.cookie_of(self.sign_in("bob", "bob-password"))
        pat = self.cookie_of(self.sign_in("pat", "pat-password"))
        alex = self.cookie_of(self.sign_in("alex", "alex-password"))
        self.assertIn(b"play.example.org", self.req("/join/survival", cookie=bob).body)
        self.assertNotIn(b'id="stop"', self.req("/join/survival", cookie=bob).body)
        self.assertIn(b'id="stop"', self.req("/join/survival", cookie=pat).body)
        self.assertIn(b'id="stop"', self.req("/join/survival", cookie=alex).body)    # admins: everything
        self.assertEqual(self.req("/join/survival/stop", "POST", cookie=bob, start="1").status, 403)
        self.assertEqual(self.req("/join/survival/stop", "POST", cookie=pat).status, 400)      # no header
        with mock.patch.object(J, "stop_server") as stop, mock.patch.object(J, "_busy", lambda s: None):
            a = self.req("/join/survival/stop", "POST", cookie=pat, start="1")
            self.assertEqual((a.status, json.loads(a.body)), (202, {"status": "stopping"}))
            time.sleep(0.2)
            stop.assert_called_once_with("local", "minecraft-survival", "pat")
            # one stop every 2 minutes
            self.assertEqual(json.loads(self.req("/join/survival/stop", "POST", cookie=pat, start="1").body),
                             {"status": "stopping"})
            self.assertEqual(stop.call_count, 1)
        self.assertTrue(any("pat (from 203.0.113.9) stopped minecraft-survival" in m for m in self.log))
        # Start: pat may, bob may not; per visitor, not per address
        self.summaries["minecraft-survival"]["status"] = "offline"
        with mock.patch.object(J, "_start_server"):
            self.assertEqual(self.req("/join/survival/start", "POST", cookie=bob, start="1").status, 403)
            self.assertEqual(self.req("/join/survival/start", "POST", cookie=pat, start="1").status, 202)
        # a backup running: no stop
        self.summaries["minecraft-survival"]["status"] = "online"
        J._stops.clear()
        with mock.patch.object(J, "_busy", lambda s: "A backup of the server is running."):
            self.assertEqual(self.req("/join/survival/stop", "POST", cookie=pat, start="1").status, 403)
        # anyone-pages never stop
        self.page(access="anyone")
        self.assertEqual(self.req("/join/survival/stop", "POST", cookie=pat, start="1").status, 404)

    def test_visitors_who_arent_signed_in_see_and_start(self):
        grants.set_user("pat", {"minecraft-survival": ["view", "power"]})
        self.page(access="permission", signin_panel=True,
                  rules=[{"who": "anyone", "see": True, "start": True, "stop": True},     # stop is dropped
                         {"who": "panel_perms", "see": True, "start": True, "stop": True}])
        cfg = J.page(J.page_key("local", "minecraft-survival"))
        self.assertFalse(cfg["rules"][0]["stop"])
        self.assertEqual(J._anonymous(cfg), {"see": True, "start": True, "stop": False})
        page = self.req("/join/survival").body
        self.assertIn(b"play.example.org", page)                 # no sign-in needed to see it
        self.assertIn(b"signin-more", page)                      # but one is offered
        self.assertNotIn(b'id="stop"', page)
        self.summaries["minecraft-survival"]["status"] = "offline"
        with mock.patch.object(J, "_start_server"):
            self.assertEqual(self.req("/join/survival/start", "POST", start="1").status, 202)
        self.summaries["minecraft-survival"]["status"] = "online"
        self.assertEqual(self.req("/join/survival/stop", "POST", start="1").status, 403)
        pat = self.cookie_of(self.sign_in("pat", "pat-password"))
        self.assertIn(b'id="stop"', self.req("/join/survival", cookie=pat).body)
        self.assertNotIn(b"signin-more", self.req("/join/survival", cookie=pat).body)

    def discord_visit(self, code, next_path="/join/survival"):
        a = self.req("/join/_discord/start", query="next=" + next_path)
        self.assertEqual(a.status, 302, a.body)
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(a.extra["Location"]).query)
        state = q["state"][0]
        back = self.req("/join/_discord/callback", cookie=self.cookie_of(a, S.STATE_COOKIE),
                        query=urllib.parse.urlencode({"state": state, "code": code}))
        return back

    def test_discord_members_and_roles(self):
        self.configure(enabled=False)
        self.page(access="permission", signin_panel=False, signin_discord=True, discord_member=True,
                  discord_invite="https://discord.gg/abc123",
                  rules=[{"who": "discord_member", "see": True, "start": True, "stop": False},
                         {"who": f"discord_role:{MOD}", "see": True, "start": True, "stop": True}])
        a = self.req("/join/survival")
        self.assertIn(b"Sign in with Discord", a.body)
        self.assertIn(b"https://discord.gg/abc123", a.body)
        self.assertNotIn(b'action="/join/_signin"', a.body)
        # the visitor starts at the join pages address Discord sends people back to
        other = self.req("/join/_discord/start", query="next=/join/survival", host="192.168.1.5:8444")
        self.assertEqual(other.extra["Location"], JOIN + "/join/_discord/start?next=/join/survival")
        back = self.discord_visit("alice")
        self.assertEqual((back.status, back.extra["Location"]), (303, "/join/survival"))
        self.assertEqual(self.fake.redirects[-1], JOIN + "/join/_discord/callback")
        alice = self.cookie_of(back)
        page = self.req("/join/survival", cookie=alice)
        self.assertIn(b"play.example.org", page.body)
        self.assertNotIn(b'id="stop"', page.body)
        bob = self.cookie_of(self.discord_visit("bob"))
        self.assertIn(b'id="stop"', self.req("/join/survival", cookie=bob).body)
        carol = self.cookie_of(self.discord_visit("carol"))
        page = self.req("/join/survival", cookie=carol)
        self.assertIn(b"Join our Discord first", page.body)
        self.assertNotIn(b"play.example.org", page.body)
        # the bot sees bob lose the Mod role: his Stop goes at once
        self.bot_online = True
        self.fake.bot_roles[BOB] = [MEMBER]
        D._member_cache.clear()
        self.assertEqual(self.req("/join/survival/stop", "POST", cookie=bob, start="1").status, 403)
        # blocked
        D.set_blocked(ALICE, True, "alex")
        self.assertNotIn(b"play.example.org", self.req("/join/survival", cookie=alice).body)

    def test_discord_roles_get_old_without_the_bot(self):
        self.configure()
        self.page(access="signed", signin_panel=False, signin_discord=True)
        alice = self.cookie_of(self.discord_visit("alice"))
        self.assertIn(b"play.example.org", self.req("/join/survival", cookie=alice).body)
        later = time.time() + S.DISCORD_FRESH + 60
        with mock.patch.object(S.time, "time", lambda: later), mock.patch.object(auth.time, "time", lambda: later):
            page = self.req("/join/survival", cookie=alice)
        self.assertIn(b"ran out", page.body)
        self.assertNotIn(b"play.example.org", page.body)

    def test_page_settings_are_checked(self):
        for bad in ({"access": "everyone"},
                    {"access": "signed", "signin_panel": False, "signin_discord": False},
                    {"access": "signed", "signin_discord": True},                        # Discord not set up
                    {"access": "permission", "signin_panel": True, "rules": []},
                    {"access": "permission", "signin_panel": True, "rules": [{"who": "panel:../x", "see": True}]},
                    {"access": "permission", "signin_panel": True, "rules": [{"who": "root", "see": True}]},
                    {"access": "signed", "signin_panel": True, "discord_invite": "https://evil.example/invite/x"}):
            with self.assertRaises(J.JoinError, msg=bad):
                self.page(**bad)
        self.configure(guild="", roles=[])
        with self.assertRaises(J.JoinError):
            self.page(access="signed", signin_discord=True, discord_member=True)
        cfg = self.page(access="signed", signin_panel=True)
        self.assertEqual(J.page("minecraft-survival")["access"], "signed")
        self.assertTrue(cfg["signin_panel"])
        # an older form without "access" leaves it as it was
        J.save_page("local", "minecraft-survival", {"enabled": True}, "alex")
        self.assertEqual(J.page("minecraft-survival")["access"], "signed")


if __name__ == "__main__":
    unittest.main()
