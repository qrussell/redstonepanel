"""1.21.10: the terminal (core/terminal.py, the `terminal` platform area and its routes in main.py).
Run: python -m unittest discover -s tests

The login prompt, rate limits, idle close and the rest run against the fake platform (no PAM, no
pseudo-terminal); the routes and websockets go through the real app with FastAPI's TestClient.
packaging/test/test-terminal.sh tests a real shell on an installed panel.
"""
import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-terminal-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import platform  # noqa: E402
from core.platform import fake  # noqa: E402
from core import auth, terminal as T, twofactor  # noqa: E402
from core import settings as panel_settings  # noqa: E402

try:
    import fastapi  # noqa: F401
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect
    HAVE_FASTAPI = True
except (ImportError, RuntimeError):
    HAVE_FASTAPI = False

ALEX = {"password": "alex-pw", "uid": 1001, "shell": "/bin/bash"}
SAM = {"password": "sam-pw", "uid": 1002, "shell": "/bin/bash"}
ROOT = {"password": "root-pw", "uid": 0, "shell": "/bin/bash"}
MC = {"password": "x", "uid": 999, "shell": "/usr/sbin/nologin"}


def isolate(test, **settings):
    d = tempfile.mkdtemp(dir=TMP)
    patches = [mock.patch.object(panel_settings, "SETTINGS_FILE", os.path.join(d, "settings.json")),
               mock.patch.dict(panel_settings._cache, {"mtime": None, "data": None}),
               mock.patch.object(auth, "USERS_FILE", os.path.join(d, "users.json")),
               mock.patch.object(auth, "SECRET_FILE", os.path.join(d, "secret.key")),
               mock.patch.object(auth, "_secret", None),
               mock.patch.object(auth, "FAILS_FILE", None),
               mock.patch.dict(auth._fails, clear=True),
               mock.patch.object(twofactor, "TOTP_FILE", os.path.join(d, "totp.json")),
               mock.patch.dict(twofactor._cache, {"mtime": None, "data": None}),
               mock.patch.dict(T._tickets, clear=True), mock.patch.dict(T._recent, clear=True),
               mock.patch.dict(T._signed_out, clear=True), mock.patch.dict(T.SESSIONS, clear=True),
               mock.patch.object(T, "WRONG_DELAY", 0), mock.patch.object(T, "TICK", 0.02),
               mock.patch.object(T, "WARN_SECONDS", 0.05)]
    for p in patches:
        p.start()
        test.addCleanup(p.stop)
    platform.use("fake")
    test.addCleanup(platform.use)
    fake.reset()
    fake.state.terminal_accounts.update(alex=dict(ALEX), sam=dict(SAM), root=dict(ROOT), minecraft=dict(MC))
    s = dict(panel_settings.DEFAULTS, terminal_enabled=True)
    s.update(settings)
    panel_settings.save(s)
    test.log = []
    p = mock.patch.object(T.manager, "write_log", test.log.append)
    p.start()
    test.addCleanup(p.stop)


class Conn:
    """The browser's side of a websocket, scripted."""

    def __init__(self):
        self.inbox = asyncio.Queue()
        self.out = bytearray()
        self.texts = []
        self.closed = None

    def type(self, text):
        self.inbox.put_nowait(("bytes", text.encode() if isinstance(text, str) else text))

    def control(self, msg):
        self.inbox.put_nowait(("text", json.dumps(msg)))

    def hang_up(self):
        self.inbox.put_nowait(("closed", None))

    async def recv(self):
        return await self.inbox.get()

    async def send_bytes(self, data):
        self.out += data

    async def send_text(self, text):
        self.texts.append(json.loads(text))

    async def close(self, code=1000):
        if self.closed is None:
            self.closed = code

    @property
    def screen(self):
        return self.out.decode("utf-8", "replace")

    async def wait_for(self, text, timeout=3):
        end = time.monotonic() + timeout
        while text not in self.screen:
            if time.monotonic() > end:
                raise AssertionError(f"{text!r} never showed; screen: {self.screen!r}")
            await asyncio.sleep(0.01)


def run(script, who=None, **extra):
    """Runs serve() with a scripted browser. script(conn) is a coroutine that types and waits."""
    conn = Conn()
    who = dict({"panel_user": "boss", "ip": "192.0.2.50"}, **(who or {}))

    async def main():
        task = asyncio.ensure_future(T.serve(conn, who))
        try:
            await script(conn)
            await asyncio.wait([task], timeout=1)
        finally:
            if not task.done():
                conn.hang_up()
            await asyncio.wait_for(task, 5)
    asyncio.run(main())
    return conn


async def login(conn, user, password):
    await conn.wait_for("login: ")
    conn.type(user + "\r")
    await conn.wait_for("Password: ")
    conn.type(password + "\r")


class LoginPrompt(unittest.TestCase):
    def setUp(self):
        isolate(self)

    def test_a_panel_admin_logs_in_and_gets_a_shell_as_that_account(self):
        async def script(c):
            await login(c, "alex", "alex-pw")
            await c.wait_for("welcome alex")
            c.type("id -u\r")
            await c.wait_for("id -u\r")
            self.assertEqual(c.texts[0]["type"], "started")
            self.assertEqual(c.texts[0]["account"], "alex")
            c.type("exit\r")
        c = run(script)
        self.assertIn(("terminal.spawn", "alex", "192.0.2.50"), fake.calls)
        self.assertTrue(fake.state.shells[0].closed)
        self.assertTrue(any("boss opened a shell as alex from 192.0.2.50" in line for line in self.log))
        self.assertTrue(any("closed after 0 min (the shell ended)" in line for line in self.log))
        self.assertEqual(T.SESSIONS, {})
        self.assertEqual(c.closed, 1000)

    def test_the_password_is_never_shown_or_logged(self):
        async def script(c):
            await login(c, "alex", "alex-pw")
            await c.wait_for("welcome alex")
            c.type("exit\r")
        c = run(script)
        self.assertIn("login: alex\r\n", c.screen)
        self.assertNotIn("alex-pw", c.screen)
        self.assertFalse(any("alex-pw" in line for line in self.log))

    def test_wrong_password_unknown_account_and_nologin_look_the_same(self):
        for user, pw in (("alex", "nope"), ("ghost", "x"), ("minecraft", "x")):
            async def script(c, user=user, pw=pw):
                await login(c, user, pw)
                await c.wait_for("Login incorrect")
                c.hang_up()
            c = run(script)
            self.assertNotIn("welcome", c.screen)
        self.assertEqual(fake.state.shells, [])
        self.assertTrue(any("Login failed for 'ghost'" in line for line in self.log))

    def test_three_wrong_passwords_close_the_window(self):
        async def script(c):
            c.type("alex\rnope\r" * 3)                          # typed ahead: each prompt takes its line
            for _ in range(300):
                if c.screen.count("Login incorrect") == 3:
                    break
                await asyncio.sleep(0.01)
        c = run(script)
        self.assertEqual(c.screen.count("Login incorrect"), 3)
        self.assertEqual(c.closed, 1000)
        self.assertEqual(fake.state.shells, [])

    def test_five_failures_block_the_account_and_the_address(self):
        for _ in range(5):
            auth.record_fail(T.fail_keys("alex", "192.0.2.99"))

        async def script(c):
            await login(c, "alex", "alex-pw")                   # right password, but blocked
            await c.wait_for("Too many wrong passwords")
        run(script)
        self.assertEqual(fake.state.shells, [])

        async def other(c):                                    # same address, other account: blocked too
            await login(c, "sam", "sam-pw")
            await c.wait_for("Too many wrong passwords")
        for _ in range(5):
            auth.record_fail(T.fail_keys("nobody", "192.0.2.77"))
        run(other, {"ip": "192.0.2.77"})
        self.assertEqual(fake.state.shells, [])

    def test_root_only_when_allowed(self):
        async def script(c):
            await login(c, "root", "root-pw")
            await c.wait_for("root can't sign in here")
            c.hang_up()
        run(script)
        self.assertEqual(fake.state.shells, [])
        panel_settings.save(dict(panel_settings.load(), terminal_allow_root=True))

        async def allowed(c):
            await login(c, "root", "root-pw")
            await c.wait_for("welcome root")
            c.type("exit\r")
        run(allowed)
        self.assertEqual(fake.state.shells[0].user, "root")

    def test_any_uid_0_account_is_root_and_odd_names_are_refused(self):
        fake.state.terminal_accounts["toor"] = {"password": "t", "uid": 0, "shell": "/bin/bash"}
        self.assertEqual(T.refusal("toor"), T.ROOT_REFUSED)
        for name in ("-froot", "alex bob", "al\x01ex", ""):
            self.assertIsNotNone(T.refusal(name), name)
        self.assertIsNone(T.refusal("alex"))

    def test_a_linked_root_account_is_refused_too(self):
        async def script(c):
            await c.wait_for("root can't sign in here")
        run(script, {"linked": "root", "recent": True})
        self.assertEqual(fake.state.shells, [])

    def test_two_factor_code_after_the_password(self):
        key = twofactor.account_key("panel", "boss")
        with mock.patch.object(T.twofactor, "verify", side_effect=lambda k, code: code == "123456") as verify:
            async def wrong(c):
                await login(c, "alex", "alex-pw")
                await c.wait_for("Authenticator code: ")
                c.type("000000\r")
                await c.wait_for("That code is wrong")
            run(wrong, {"two_factor_key": key})
            self.assertEqual(fake.state.shells, [])

            async def right(c):
                await login(c, "alex", "alex-pw")
                await c.wait_for("Authenticator code: ")
                c.type("123456\r")
                await c.wait_for("welcome alex")
                c.type("exit\r")
            run(right, {"two_factor_key": key})
            verify.assert_called_with(key, "123456")

    def test_ctrl_c_at_the_prompt_closes(self):
        async def script(c):
            await c.wait_for("login: ")
            c.type(b"\x03")
            await c.wait_for("^C")
        c = run(script)
        self.assertEqual(c.closed, 1000)

    def test_a_shell_running_as_the_wrong_account_is_closed(self):
        fake.state.terminal_uid = 0

        async def script(c):
            await login(c, "alex", "alex-pw")
            await c.wait_for("welcome alex")
            for _ in range(200):
                if fake.state.shells[0].closed:
                    break
                await asyncio.sleep(0.01)
        run(script)
        self.assertTrue(fake.state.shells[0].closed)
        self.assertTrue(any("ran as uid 0, not alex" in line for line in self.log))


class LinkedAccount(unittest.TestCase):
    """A Linux sign-in opens as that same account; panel-only accounts never link by name."""

    def setUp(self):
        isolate(self)

    def test_recent_linux_sign_in_opens_with_no_prompt(self):
        async def script(c):
            await c.wait_for("Signed in as sam (your Linux account)")
            await c.wait_for("welcome sam")
            c.type("exit\r")
        c = run(script, {"linked": "sam", "recent": True})
        self.assertNotIn("Password", c.screen)
        self.assertEqual(fake.state.shells[0].user, "sam")
        self.assertNotIn(("terminal.check_password", "sam"), fake.calls)

    def test_older_sign_in_asks_that_accounts_password_only(self):
        async def script(c):
            await c.wait_for("Password for sam: ")
            c.type("sam-pw\r")
            await c.wait_for("welcome sam")
            c.type("exit\r")
        c = run(script, {"linked": "sam", "recent": False})
        self.assertNotIn("login:", c.screen)
        self.assertIn(("terminal.check_password", "sam"), fake.calls)

    def test_the_window_comes_from_the_sign_in_and_from_typing_the_password(self):
        cookie = auth.make_session_token({"u": "sam", "src": "linux", "v": 0})
        self.assertTrue(T.recent(cookie))
        later = time.time() + 16 * 60
        with mock.patch("core.terminal.time.time", return_value=later):
            self.assertFalse(T.recent(cookie))
        T._mark_recent(cookie)
        with mock.patch("core.terminal.time.time", return_value=time.time() + 14 * 60):
            self.assertTrue(T.recent(cookie))
        panel_settings.save(dict(panel_settings.load(), terminal_recent_minutes=0))
        self.assertFalse(T.recent(cookie))

    def test_only_a_linux_sign_in_links(self):
        self.assertEqual(T.linked_account({"username": "sam", "source": "linux", "role": "admin"}), "sam")
        self.assertIsNone(T.linked_account({"username": "sam", "source": "panel", "role": "admin"}))


class Limits(unittest.TestCase):
    def setUp(self):
        isolate(self)

    def test_idle_close_with_a_warning_first(self):
        with mock.patch.object(T, "idle_seconds", return_value=0.5), mock.patch.object(T, "WARN_SECONDS", 0.3):
            async def script(c):
                await login(c, "alex", "alex-pw")
                await c.wait_for("welcome alex")
                await c.wait_for("Closed after no typing", timeout=5)
            c = run(script)
        self.assertEqual(c.texts[1]["type"], "warning")
        self.assertTrue(fake.state.shells[0].closed)
        self.assertTrue(any("(idle)" in line for line in self.log))

    def test_ended_from_settings_or_sign_out(self):
        cookie = auth.make_session_token({"u": "boss", "src": "panel", "v": 1})

        async def script(c):
            await login(c, "alex", "alex-pw")
            await c.wait_for("welcome alex")
            sid = list(T.SESSIONS)[0]
            self.assertEqual(T.list_sessions()[0]["account"], "alex")
            T.signed_out(cookie)
            await c.wait_for("[Closed: you signed out]")
            self.assertTrue(sid)
        run(script, {"cookie": cookie})
        self.assertTrue(T.is_signed_out(cookie))
        self.assertTrue(fake.state.shells[0].closed)

        async def ended(c):
            await login(c, "alex", "alex-pw")
            await c.wait_for("welcome alex")
            self.assertTrue(T.end_session(list(T.SESSIONS)[0], "root-admin"))
            await c.wait_for("[Closed: ended by root-admin]")
        run(ended)

    def test_turning_it_off_closes_every_terminal(self):
        async def script(c):
            await login(c, "alex", "alex-pw")
            await c.wait_for("welcome alex")
            T.turn(False, "boss")
            await c.wait_for("[Closed: the terminal was turned off by boss]")
        run(script)
        self.assertIn("[TERMINAL] boss turned the terminal off.", self.log)

    def test_no_more_than_three_per_admin(self):
        for i in range(3):
            s = T.Session("boss", "192.0.2.1")
            T.SESSIONS[s.id] = s
        with self.assertRaises(T.Refused):
            T.issue_ticket({"username": "boss", "role": "admin", "source": "panel"}, "c", True, "192.0.2.1")

        async def script(c):
            await c.wait_for("already have 3 terminals")
        c = run(script)
        self.assertEqual(c.closed, 4003)

    def test_still_allowed_is_checked(self):
        with mock.patch.object(T, "RECHECK_SECONDS", 0):
            async def script(c):
                await login(c, "alex", "alex-pw")
                await c.wait_for("welcome alex")
                await c.wait_for("[Closed: you aren't an admin any more]")
            run(script, {"still_allowed": lambda: "you aren't an admin any more"})

    def test_resize_reaches_the_shell(self):
        async def script(c):
            c.control({"type": "resize", "rows": 40, "cols": 120})
            await login(c, "alex", "alex-pw")
            await c.wait_for("welcome alex")
            c.control({"type": "resize", "rows": 50, "cols": 9999})
            c.type("Q")
            await c.wait_for("Q")
            self.assertEqual(fake.state.shells[0].size, (50, 1000))
            c.type("exit\r")
        run(script)


class Status(unittest.TestCase):
    def setUp(self):
        isolate(self)

    def test_who_sees_the_button(self):
        admin = {"username": "boss", "role": "admin", "source": "panel"}
        self.assertTrue(T.status(admin, True, "192.0.2.1")["available"])
        st = T.status({"username": "helper", "role": "user", "source": "panel"}, True, "192.0.2.1")
        self.assertFalse(st["available"])
        self.assertIn("Only admins", st["why"])
        st = T.status({"username": "lan", "role": "user", "source": "trusted"}, True, "192.0.2.1")
        self.assertFalse(st["available"])
        self.assertIn("HTTPS", T.status(admin, False, "192.0.2.1")["why"])
        self.assertTrue(T.status(admin, False, "127.0.0.1")["available"])
        self.assertEqual(T.status({"username": "sam", "role": "admin", "source": "linux"}, True, "x")["linked"], "sam")
        fake.state.terminal_supported = False                   # e.g. a Windows host
        st = T.status(admin, True, "192.0.2.1")
        self.assertFalse(st["available"])
        self.assertFalse(st["supported"])
        fake.state.terminal_supported = True
        panel_settings.save(dict(panel_settings.load(), terminal_enabled=False))
        self.assertIn("off", T.status(admin, True, "192.0.2.1")["why"])

    def test_tickets_are_one_use_short_lived_and_for_one_session(self):
        admin = {"username": "boss", "role": "admin", "source": "panel"}
        t = T.issue_ticket(admin, "cookie-a", True, "192.0.2.1")
        self.assertIsNone(T.take_ticket(t, "cookie-b"))         # another session can't use it
        t = T.issue_ticket(admin, "cookie-a", True, "192.0.2.1")
        self.assertEqual(T.take_ticket(t, "cookie-a")["user"], "boss")
        self.assertIsNone(T.take_ticket(t, "cookie-a"))         # once
        t = T.issue_ticket(admin, "cookie-a", True, "192.0.2.1")
        with mock.patch("core.terminal.time.time", return_value=time.time() + 31):
            self.assertIsNone(T.take_ticket(t, "cookie-a"))
        t = T.issue_ticket(admin, "cookie-a", True, "192.0.2.1", node="n-abc123")
        self.assertIsNone(T.take_ticket(t, "cookie-a"))          # a node's ticket isn't for this machine

    def test_settings_are_checked(self):
        for bad in ({"terminal_idle_minutes": 1}, {"terminal_idle_minutes": 500}, {"terminal_recent_minutes": -1},
                    {"terminal_recent_minutes": 61}, {"terminal_allow_root": "yes"}):
            with self.assertRaises(panel_settings.SettingsError, msg=bad):
                panel_settings.validate(dict(panel_settings.load(), **bad))
        self.assertFalse(panel_settings.DEFAULTS["terminal_enabled"])
        self.assertFalse(panel_settings.DEFAULTS["terminal_allow_root"])
        self.assertFalse(panel_settings.DEFAULTS["terminal_from_master"])


@unittest.skipUnless(HAVE_FASTAPI, "needs fastapi")
class Routes(unittest.TestCase):
    """The HTTP routes and the websocket, through the real app."""

    def setUp(self):
        isolate(self)
        import main
        self.main = main
        auth.create_panel_user("boss", "boss-password-1", "admin")
        auth.create_panel_user("helper", "helper-password-1", "user")

    def client(self, user="boss", password=None, https=True):
        c = TestClient(self.main.app, base_url="https://testserver" if https else "http://testserver")
        pw = password or {"boss": "boss-password-1", "helper": "helper-password-1"}[user]
        self.assertEqual(c.post("/api/auth/login", json={"username": user, "password": pw}).status_code, 200)
        return c

    def ws(self, c, path, origin="https://testserver"):
        """TestClient keeps neither wss:// nor the cookie for a websocket on its own."""
        cookie = c.cookies.get(auth.SESSION_COOKIE)
        headers = {"origin": origin}
        if cookie:
            headers["cookie"] = f"{auth.SESSION_COOKIE}={cookie}"
        return c.websocket_connect("wss://testserver" + path, headers=headers)

    def ticket(self, c):
        r = c.post("/api/terminal/ticket", json={}, headers={"origin": "https://testserver"})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["ticket"]

    def test_a_panel_admin_opens_a_terminal(self):
        c = self.client()
        self.assertTrue(c.get("/api/terminal").json()["available"])
        with self.ws(c, f"/api/terminal/ws?ticket={self.ticket(c)}") as ws:
            screen = b""
            while b"login: " not in screen:
                screen += ws.receive_bytes()
            ws.send_bytes(b"alex\r")
            while b"Password: " not in screen:
                screen += ws.receive_bytes()
            ws.send_bytes(b"alex-pw\r")
            msg = ws.receive()
            while "text" not in msg or msg["text"] is None:
                msg = ws.receive()
            self.assertEqual(json.loads(msg["text"])["account"], "alex")
            ws.send_bytes(b"exit\r")
        self.assertEqual(fake.state.shells[0].user, "alex")
        self.assertEqual(c.get("/api/terminal/sessions").status_code, 200)

    def test_refused_without_a_ticket_from_another_site_or_over_http(self):
        c = self.client()
        for url, origin in (("/api/terminal/ws", "https://testserver"),
                            ("/api/terminal/ws?ticket=made-up", "https://testserver"),
                            (f"/api/terminal/ws?ticket={self.ticket(c)}", "https://evil.example")):
            with self.assertRaises(WebSocketDisconnect, msg=url):
                with self.ws(c, url, origin) as ws:
                    ws.receive_bytes()
        plain = self.client(https=False)
        r = plain.post("/api/terminal/ticket", json={}, headers={"origin": "http://testserver"})
        self.assertEqual(r.status_code, 403)
        self.assertIn("HTTPS", r.json()["detail"])

    def test_helpers_and_signed_out_browsers_get_nothing(self):
        h = self.client("helper")
        self.assertFalse(h.get("/api/terminal").json()["available"])
        self.assertEqual(h.post("/api/terminal/ticket", json={}, headers={"origin": "https://testserver"}).status_code, 403)
        self.assertEqual(h.get("/api/terminal/sessions").status_code, 403)
        self.assertEqual(h.put("/api/settings/terminal", json={"terminal_enabled": True},
                               headers={"origin": "https://testserver"}).status_code, 403)
        anon = TestClient(self.main.app, base_url="https://testserver")
        self.assertEqual(anon.get("/api/terminal").status_code, 401)
        with self.assertRaises(WebSocketDisconnect):
            with self.ws(anon, "/api/terminal/ws?ticket=x") as ws:
                ws.receive_bytes()

    def test_signing_out_stops_the_cookie_opening_terminals(self):
        c = self.client()
        cookie = c.cookies.get(auth.SESSION_COOKIE)
        t = self.ticket(c)
        c.post("/api/auth/logout", headers={"origin": "https://testserver"})
        c.cookies.set(auth.SESSION_COOKIE, cookie)              # a copy kept somewhere
        with self.assertRaises(WebSocketDisconnect):
            with self.ws(c, f"/api/terminal/ws?ticket={t}") as ws:
                ws.receive_bytes()

    def test_settings_route(self):
        c = self.client()
        o = {"origin": "https://testserver"}
        r = c.put("/api/settings/terminal", json={"terminal_enabled": False}, headers=o)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["terminal_enabled"])
        self.assertEqual(c.post("/api/terminal/ticket", json={}, headers=o).status_code, 403)
        r = c.put("/api/settings/terminal", json={"terminal_enabled": True, "terminal_idle_minutes": 1}, headers=o)
        self.assertEqual(r.status_code, 400)
        # the main settings form never changes the terminal
        s = c.get("/api/settings").json()
        c.put("/api/settings", json=dict(s, terminal_enabled=True), headers=o)
        self.assertFalse(panel_settings.load()["terminal_enabled"])

    def test_not_on_the_join_pages_port(self):
        from core import joinsite
        j = TestClient(joinsite.app, base_url="https://testserver")
        for path in ("/api/terminal", "/api/terminal/ticket"):
            self.assertEqual(j.get(path).status_code, 404, path)
        sent = []

        async def ws_upgrade():                                 # a websocket is never accepted there
            async def receive():
                return {"type": "websocket.connect"}

            async def send(msg):
                sent.append(msg)
            await joinsite.app({"type": "websocket", "path": "/api/terminal/ws", "headers": []}, receive, send)
        asyncio.run(ws_upgrade())
        self.assertEqual(sent, [])

    def test_a_node_refuses_the_master_unless_allowed(self):
        c = TestClient(self.main.app, base_url="https://testserver")
        with mock.patch.object(self.main.cluster, "read_node_token",
                               return_value={"username": "master:Main", "source": "cluster", "role": "user",
                                             "master": "Main"}):
            hdr = {"authorization": "Bearer rpn_x", "x-redstonepanel-role": "admin", "x-redstonepanel-actor": "boss",
                   "x-redstonepanel-client-ip": "192.0.2.80"}
            with self.assertRaises(WebSocketDisconnect):
                with c.websocket_connect("/api/terminal/node-ws", headers=hdr) as ws:
                    ws.receive_bytes()
            panel_settings.save(dict(panel_settings.load(), terminal_from_master=True))
            with self.assertRaises(WebSocketDisconnect):          # a master user, not an admin
                with c.websocket_connect("/api/terminal/node-ws", headers=dict(hdr, **{"x-redstonepanel-role": "user"})) as ws:
                    ws.receive_bytes()
            with c.websocket_connect("/api/terminal/node-ws", headers=hdr) as ws:
                screen = b""
                while b"login: " not in screen:
                    screen += ws.receive_bytes()
                ws.send_bytes(b"alex\ralex-pw\r")
                while b"welcome alex" not in screen:
                    screen += ws.receive().get("bytes") or b""
                ws.send_bytes(b"exit\r")
        self.assertIn(("terminal.spawn", "alex", "192.0.2.80"), fake.calls)
        self.assertTrue(any("boss (master Main) opened a shell as alex" in line for line in self.log))


class Cluster(unittest.TestCase):
    def setUp(self):
        isolate(self)

    def test_hello_says_whether_the_master_may_open_one(self):
        from core import cluster
        h = cluster.hello()
        self.assertEqual(h["terminal"], {"supported": True, "enabled": True, "from_master": False})
        self.assertGreaterEqual(h["cluster_api"], cluster.TERMINAL_API)
        self.assertEqual(cluster.clean_terminal({"supported": True, "enabled": "yes", "x": 1}),
                         {"supported": True, "enabled": False, "from_master": False})
        self.assertIsNone(cluster.clean_terminal(None))

    def test_relay_copies_both_ways_and_closes_both(self):
        browser, node = Conn(), Conn()

        async def main():
            task = asyncio.ensure_future(T.relay(browser, node))
            browser.type(b"ls\r")
            node.inbox.put_nowait(("bytes", b"file.txt\r\n"))
            await browser.wait_for("file.txt")
            await asyncio.sleep(0.05)
            node.hang_up()
            await asyncio.wait_for(task, 2)
        asyncio.run(main())
        self.assertEqual(bytes(node.out), b"ls\r")
        self.assertEqual(browser.closed, 1000)
        self.assertEqual(node.closed, 1000)


if __name__ == "__main__":
    unittest.main()
