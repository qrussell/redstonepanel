"""1.27: Discord /start (core/discordbot.py).
Run: python -m unittest discover -s tests

The panel is its own Discord bot: a small websocket client (tested here against a real socket
server), the gateway (hello, identify, heartbeat, resume, close codes) driven by a stand-in
connection, and the /start answers: only the allowed Discord servers and roles, only the
allowed game servers, a cooldown per server and a limit per Discord user, logged.
"""
import base64
import hashlib
import json
import os
import socket
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-discord-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import discordbot as D, grants as G, joinpage  # noqa: E402

try:
    import fastapi  # noqa: F401
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except (ImportError, RuntimeError):
    HAVE_FASTAPI = False

# Not a real token, and built so secret scanners (GitHub push protection) don't take it for one
TOKEN = "-".join(["not", "a", "real", "discord", "bot", "token"]) + "_" + "x" * 40
GUILD, OTHER_GUILD, ROLE = "111111111111111111", "222222222222222222", "333333333333333333"


class Fresh(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp(dir=TMP)
        self.log = []
        for p in (mock.patch.object(D, "DISCORD_FILE", os.path.join(d, "discord.json")),
                  mock.patch.object(D, "_log", self.log.append),
                  mock.patch.object(D, "apply", lambda: None)):
            p.start()
            self.addCleanup(p.stop)
        D._starts.clear()
        D._starts_by_user.clear()
        D._servers_cache.update(at=0, list=[])
        D._status.update(state="off", error=None, user=None, app_id="999999999999999999", guilds={})


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
class Settings(Fresh):
    def test_token_kept_private_and_shown_only_by_its_end(self):
        out = D.save_settings({"token": "Bot " + TOKEN, "enabled": True, "guilds": [GUILD]}, actor="quentin")
        self.assertTrue(out["enabled"])
        self.assertTrue(out["has_token"])
        self.assertEqual(out["token_hint"], TOKEN[-4:])
        self.assertNotIn(TOKEN, json.dumps(out))
        self.assertEqual(D.load()["token"], TOKEN, "a pasted 'Bot ' in front is dropped")
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(os.stat(D.DISCORD_FILE).st_mode), 0o600)
        self.assertTrue(any("quentin turned Discord /start on for *" in line for line in self.log), self.log)
        self.assertIn("client_id=999999999999999999", out["invite"])

    def test_empty_token_keeps_the_saved_one_and_forget_removes_it(self):
        D.save_settings({"token": TOKEN, "enabled": True})
        D.save_settings({"token": "", "servers": ["minecraft"]})
        self.assertEqual(D.load()["token"], TOKEN)
        self.assertTrue(D.load()["enabled"])
        out = D.save_settings({"forget_token": True, "enabled": True})
        self.assertFalse(out["has_token"])
        self.assertFalse(out["enabled"])

    def test_bad_settings(self):
        for new in ({"token": "short"}, {"token": "x" * 60 + "!"}, {"enabled": True},
                    {"token": TOKEN, "servers": ["../etc"]}, {"token": TOKEN, "guilds": ["abc"]},
                    {"token": TOKEN, "roles": "333333333333333333"},
                    {"token": TOKEN, "enabled": True, "servers": []}):
            with self.assertRaises(D.DiscordError, msg=new):
                D.save_settings(new)

    def test_every_server_wins_over_a_list(self):
        D.save_settings({"servers": ["minecraft", "*"]})
        self.assertEqual(D.load()["servers"], ["*"])

    def test_deleted_and_moved_servers(self):
        D.save_settings({"servers": ["minecraft", "minecraft-2"]})
        with mock.patch.object(G, "GRANTS_FILE", os.path.join(TMP, "grants-none.json")):
            G.rename_server("minecraft-2", "n-abcd/minecraft-2")
            self.assertEqual(D.load()["servers"], ["minecraft", "n-abcd/minecraft-2"])
            G.forget_server("minecraft")
        self.assertEqual(D.load()["servers"], ["n-abcd/minecraft-2"])


# ---------------------------------------------------------------------------
# Answering /start
# ---------------------------------------------------------------------------
SERVERS = [{"key": "minecraft", "member": "local", "name": "minecraft", "edition": "Java", "where": ""},
           {"key": "minecraft-bedrock", "member": "local", "name": "minecraft-bedrock", "edition": "Bedrock", "where": ""},
           {"key": "n-abcd/minecraft-modded", "member": "n-abcd", "name": "minecraft-modded", "edition": "Java",
            "where": "Basement PC"}]


def command(server=None, guild=GUILD, roles=(), user_id="444444444444444444", kind=2, name="start"):
    d = {"id": "555555555555555555", "token": "itoken", "type": kind, "guild_id": guild,
         "data": {"name": name, "options": [{"name": "server", "value": server, "focused": True}] if server is not None else []},
         "member": {"roles": list(roles), "user": {"id": user_id, "username": "steve", "global_name": "Steve"}}}
    if guild is None:
        d.pop("guild_id")
        d["user"] = d.pop("member")["user"]
    return d


class Answers(Fresh):
    def setUp(self):
        super().setUp()
        self.summaries = {"minecraft": {"status": "offline", "edition": "Java", "port": "25565"},
                          "minecraft-bedrock": {"status": "offline", "edition": "Bedrock", "sleeping": True, "port": "19132"},
                          "n-abcd/minecraft-modded": {"status": "online", "edition": "Java", "port": "25566",
                                                      "players": {"online": 0}}}
        self.started = []
        for p in (mock.patch.object(D, "_all_servers", lambda fresh=False: [dict(s) for s in SERVERS]),
                  mock.patch.object(joinpage, "summary", lambda key, fresh=False: dict(self.summaries[key])),
                  mock.patch.object(joinpage, "start_server",
                                    lambda m, s, who, actor, tag, where: self.started.append((m, s, actor)) or True),
                  mock.patch.object(D, "_address", lambda key, s: "`play.example.com`"),
                  mock.patch.object(D, "_wait_ready", lambda *a, **k: True)):
            p.start()
            self.addCleanup(p.stop)
        D.save_settings({"token": TOKEN, "enabled": True, "guilds": [GUILD]})

    def text(self, response):
        return response["data"].get("content", "")

    def test_starts_a_stopped_server_and_logs_who(self):
        resp, after = D.answer(command("minecraft"))
        self.assertEqual(resp["type"], 4)
        self.assertIn("Starting **minecraft (Java)** for Steve", self.text(resp))
        self.assertNotIn("flags", resp["data"], "a start is shown to everyone in the channel")
        self.assertEqual(resp["data"]["allowed_mentions"], {"parse": []})
        self.assertEqual(self.started, [], "the start runs after the answer, not before")
        after()
        self.assertEqual(self.started, [("local", "minecraft", "discord:Steve")])
        self.assertTrue(any(line.startswith("[DISCORD] discord:Steve (444444444444444444) started minecraft from Discord")
                            for line in self.log), self.log)

    def test_wakes_a_sleeping_server(self):
        resp, after = D.answer(command("minecraft-bedrock"))
        self.assertIn("Waking **minecraft-bedrock (Bedrock)**", self.text(resp))
        self.assertTrue(any(" woke minecraft-bedrock " in line for line in self.log))

    def test_an_online_server_says_where_to_join(self):
        resp, after = D.answer(command("n-abcd/minecraft-modded"))
        self.assertIsNone(after)
        self.assertIn("is already online. Join at `play.example.com`.", self.text(resp))

    def test_a_node_server_starts_on_its_node(self):
        self.summaries["n-abcd/minecraft-modded"]["status"] = "offline"
        resp, after = D.answer(command("minecraft-modded"))
        after()
        self.assertEqual(self.started, [("n-abcd", "minecraft-modded", "discord:Steve")])
        self.assertTrue(any("on cluster member Basement PC" in line for line in self.log))

    def test_names_can_be_typed_without_minecraft_in_front(self):
        resp, after = D.answer(command("Bedrock"))
        self.assertIsNotNone(after)

    def test_only_allowed_game_servers(self):
        D.save_settings({"servers": ["minecraft-bedrock"]})
        resp, after = D.answer(command("minecraft"))
        self.assertIsNone(after)
        self.assertEqual(resp["data"]["flags"], D.EPHEMERAL)
        self.assertIn("Pick one of: minecraft-bedrock (Bedrock)", self.text(resp))
        resp, after = D.answer(command())
        self.assertIsNotNone(after, "with one server allowed, /start alone starts it")

    def test_which_server_when_several(self):
        resp, after = D.answer(command())
        self.assertIsNone(after)
        self.assertIn("Which server?", self.text(resp))

    def test_only_allowed_discord_servers(self):
        resp, after = D.answer(command("minecraft", guild=OTHER_GUILD))
        self.assertIsNone(after)
        self.assertIn("isn't allowed", self.text(resp))
        resp, _ = D.answer(command("minecraft", guild=None))
        self.assertIn("not in a direct message", self.text(resp))
        resp, _ = D.answer(command("mine", guild=OTHER_GUILD, kind=4))
        self.assertEqual(resp, {"type": 8, "data": {"choices": []}}, "no server names leak to other Discord servers")

    def test_only_allowed_roles(self):
        D.save_settings({"roles": [ROLE]})
        resp, after = D.answer(command("minecraft"))
        self.assertIsNone(after)
        self.assertIn("don't have a role", self.text(resp))
        resp, after = D.answer(command("minecraft", roles=[ROLE]))
        self.assertIsNotNone(after)

    def test_autocomplete_lists_allowed_servers(self):
        resp, after = D.answer(command("mod", kind=4))
        self.assertEqual(resp, {"type": 8, "data": {"choices": [
            {"name": "minecraft-modded on Basement PC (Java)", "value": "n-abcd/minecraft-modded"}]}})
        D.save_settings({"servers": ["minecraft"]})
        resp, _ = D.answer(command("", kind=4))
        self.assertEqual([c["value"] for c in resp["data"]["choices"]], ["minecraft"])

    def test_one_start_per_server_in_the_cooldown(self):
        now = 1000.0
        D.answer(command("minecraft"), now=now)
        resp, after = D.answer(command("minecraft", user_id="666666666666666666"), now=now + 30)
        self.assertIsNone(after)
        self.assertIn("already starting", self.text(resp))
        resp, after = D.answer(command("minecraft"), now=now + D.START_COOLDOWN + 1)
        self.assertIsNotNone(after)

    def test_a_user_gets_a_few_starts_an_hour(self):
        now = 1000.0
        for i in range(D.START_PER_USER):
            _, after = D.answer(command("minecraft"), now=now + i * (D.START_COOLDOWN + 1))
            self.assertIsNotNone(after)
        resp, after = D.answer(command("minecraft"), now=now + D.START_PER_USER * (D.START_COOLDOWN + 1))
        self.assertIsNone(after)
        self.assertIn("a few times in the last hour", self.text(resp))

    def test_other_commands(self):
        resp, after = D.answer(command("x", name="stop"))
        self.assertIsNone(after)
        self.assertIn("only knows /start", self.text(resp))

    def test_a_failed_start_says_so(self):
        edits = []
        with mock.patch.object(joinpage, "start_server", lambda *a, **k: False), \
                mock.patch.object(D, "_edit", lambda tok, text: edits.append(text)):
            _, after = D.answer(command("minecraft"))
            after()
        self.assertEqual(len(edits), 1)
        self.assertIn("couldn't be started", edits[0])


class Ready(Fresh):
    def test_waits_for_the_server_to_answer_then_edits_the_message(self):
        seen = iter([{"status": "offline"}, {"status": "online", "port": "25565", "players": None},
                     {"status": "online", "port": "25565", "players": {"online": 0}}])
        edits = []
        with mock.patch.object(joinpage, "summary", lambda key, fresh=False: next(seen)), \
                mock.patch.object(D, "_edit", lambda tok, text: edits.append(text)), \
                mock.patch.object(D, "_address", lambda key, s: "`play.example.com`"):
            self.assertTrue(D._wait_ready("t", "minecraft", "minecraft (Java)", wait=5, poll=0.01))
        self.assertEqual(edits, ["**minecraft (Java)** is online. Join at `play.example.com`."])

    def test_gives_up_after_the_wait(self):
        edits = []
        with mock.patch.object(joinpage, "summary", lambda key, fresh=False: {"status": "offline"}), \
                mock.patch.object(D, "_edit", lambda tok, text: edits.append(text)):
            self.assertFalse(D._wait_ready("t", "minecraft", "minecraft", wait=0.05, poll=0.01))
        self.assertIn("hasn't come online", edits[0])

    def test_nethernet_bedrock_is_ready_when_running(self):
        self.assertTrue(D._ready({"status": "online", "edition": "Bedrock", "transport": "nethernet", "port": "19132"}))
        self.assertFalse(D._ready({"status": "online", "edition": "Bedrock", "transport": "raknet", "port": "19132",
                                   "players": None}))


# ---------------------------------------------------------------------------
# The websocket client against a real socket
# ---------------------------------------------------------------------------
def _server_frame(op, data, fin=True):
    n = len(data)
    head = bytes([(0x80 if fin else 0) | op])
    if n < 126:
        head += bytes([n])
    elif n < 65536:
        head += bytes([126]) + n.to_bytes(2, "big")
    else:
        head += bytes([127]) + n.to_bytes(8, "big")
    return head + data


class FakeServer:
    """Accepts one websocket client, records what it sends (unmasked) and runs `script`."""

    def __init__(self, script):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.got = []
        self.script = script
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def read_frame(self, c):
        def exact(n):
            b = b""
            while len(b) < n:
                part = c.recv(n - len(b))
                if not part:
                    raise EOFError
                b += part
            return b
        b1, b2 = exact(2)
        n = b2 & 0x7F
        if n == 126:
            n = int.from_bytes(exact(2), "big")
        elif n == 127:
            n = int.from_bytes(exact(8), "big")
        assert b2 & 0x80, "client frames must be masked"
        mask = exact(4)
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(exact(n)))
        return b1 & 0x0F, data

    def run(self):
        c, _ = self.sock.accept()
        req = b""
        while b"\r\n\r\n" not in req:
            req += c.recv(4096)
        key = next(line.split(":", 1)[1].strip() for line in req.decode().split("\r\n")
                   if line.lower().startswith("sec-websocket-key"))
        accept = base64.b64encode(hashlib.sha1((key + D._WS_GUID).encode()).digest()).decode()
        c.sendall(f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                  f"Sec-WebSocket-Accept: {accept}\r\n\r\n".encode())
        try:
            self.script(self, c)
        except EOFError:
            pass
        c.close()
        self.sock.close()


class WebSocketClient(unittest.TestCase):
    def test_text_both_ways_sizes_fragments_ping_and_close(self):
        big = ("x" * 70000).encode()

        def script(srv, c):
            c.sendall(_server_frame(1, b'{"op":10}'))
            srv.got.append(srv.read_frame(c))
            c.sendall(_server_frame(1, b"a" * 300))
            c.sendall(_server_frame(1, big))
            c.sendall(_server_frame(9, b"hi"))                         # ping: the client answers pong
            c.sendall(_server_frame(1, b"part one ", fin=False) + _server_frame(0, b"part two"))
            srv.got.append(srv.read_frame(c))
            srv.got.append(srv.read_frame(c))                          # the big one from the client
            c.sendall(_server_frame(8, (4004).to_bytes(2, "big") + b"Authentication failed."))
            srv.got.append(srv.read_frame(c))

        srv = FakeServer(script)
        ws = D.WebSocket(f"ws://127.0.0.1:{srv.port}/?v=10&encoding=json", timeout=5)
        self.assertEqual(ws.recv(), '{"op":10}')
        ws.send('{"op":2}')
        self.assertTrue(ws.readable(2))
        self.assertEqual(ws.recv(), "a" * 300)
        self.assertEqual(ws.recv(), big.decode())
        self.assertEqual(ws.recv(), "part one part two")
        ws.send(big.decode())
        with self.assertRaises(D.Closed) as e:
            ws.recv()
        self.assertEqual(e.exception.code, 4004)
        srv.thread.join(5)
        self.assertEqual(srv.got[0], (1, b'{"op":2}'))
        self.assertEqual(srv.got[1], (0xA, b"hi"))
        self.assertEqual(srv.got[2], (1, big))
        self.assertEqual(srv.got[3][0], 8)

    def test_refused_handshake(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)

        def run():
            c, _ = s.accept()
            c.recv(4096)
            c.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            c.close()
        threading.Thread(target=run, daemon=True).start()
        with self.assertRaises(OSError):
            D.WebSocket(f"ws://127.0.0.1:{s.getsockname()[1]}/", timeout=5)
        s.close()

    def test_mask_xor(self):
        self.assertEqual(D._xor(b"", b"abcd"), b"")
        data = os.urandom(1001)
        self.assertEqual(D._xor(D._xor(data, b"\x01\x02\x03\x04"), b"\x01\x02\x03\x04"), data)


# ---------------------------------------------------------------------------
# The gateway, with a stand-in connection
# ---------------------------------------------------------------------------
class FakeWS:
    def __init__(self, incoming, url):
        self.url = url
        self.incoming = list(incoming)
        self.sent = []
        self.closed_with = None

    IDLE = 0.3                      # then the stand-in Discord closes the connection

    def readable(self, timeout):
        if self.incoming:
            return True
        if not hasattr(self, "idle_since"):
            self.idle_since = time.monotonic()
        time.sleep(min(max(timeout, 0), 0.01))
        return time.monotonic() - self.idle_since > self.IDLE

    def recv(self):
        if not self.incoming:
            raise D.Closed(1001, "going away")
        item = self.incoming.pop(0)
        if isinstance(item, Exception):
            raise item
        return json.dumps(item)

    def send(self, text):
        self.sent.append(json.loads(text))

    def close(self, code=1000):
        self.closed_with = code


class GatewayFlow(Fresh):
    def setUp(self):
        super().setUp()
        self.registered = []
        for p in (mock.patch.object(D, "register_commands", lambda token, app: self.registered.append(app)),
                  mock.patch.object(D, "BACKOFF", (0.01, 0.05)), mock.patch.object(D, "INVALID_SESSION_WAIT", 0.01)):
            p.start()
            self.addCleanup(p.stop)

    def run_gateway(self, sessions, stop_after=None):
        stop = threading.Event()
        conns, interactions = [], []

        def connect(url):
            if not sessions:
                stop.set()
                raise OSError("no more")
            ws = FakeWS(sessions.pop(0), url)
            conns.append(ws)
            return ws
        g = D.Gateway(TOKEN, stop, url="wss://gateway.test", connect=connect, on_interaction=interactions.append)
        with mock.patch.object(D.random, "random", lambda: 0.0):
            t = threading.Thread(target=self._run, args=(g,), daemon=True)
            t.start()
            t.join(10)
        stop.set()
        return g, conns, interactions

    def _run(self, g):
        try:
            g.run()
        except D.Fatal as e:
            self.fatal = e

    fatal = None

    def test_identify_ready_guilds_interactions_then_resume(self):
        hello = {"op": 10, "d": {"heartbeat_interval": 60000}}
        first = [hello,
                 {"op": 11},
                 {"op": 0, "s": 1, "t": "READY", "d": {"session_id": "sess", "resume_gateway_url": "wss://resume.test",
                                                       "user": {"id": "999999999999999999", "username": "RedstoneBot"},
                                                       "application": {"id": "999999999999999999"}}},
                 {"op": 0, "s": 2, "t": "GUILD_CREATE", "d": {"id": GUILD, "name": "Cielo Cloud", "roles": [
                     {"id": GUILD, "name": "@everyone", "position": 0}, {"id": ROLE, "name": "Players", "position": 2},
                     {"id": "777777777777777777", "name": "SomeBot", "position": 3, "managed": True}]}},
                 {"op": 0, "s": 3, "t": "INTERACTION_CREATE", "d": command("minecraft")},
                 {"op": 7, "d": None}]
        second = [hello, {"op": 0, "s": 4, "t": "RESUMED", "d": {}}]
        g, conns, interactions = self.run_gateway([first, second])
        identify = conns[0].sent[0]
        self.assertEqual(identify["op"], 2)
        self.assertEqual(identify["d"]["token"], TOKEN)
        self.assertEqual(identify["d"]["intents"], D.INTENTS)
        self.assertEqual(conns[0].sent[1], {"op": 1, "d": None}, "first heartbeat at once (jitter 0)")
        self.assertEqual(conns[0].url, "wss://gateway.test" + D.GATEWAY_QUERY)
        self.assertEqual(conns[1].url, "wss://resume.test" + D.GATEWAY_QUERY)
        self.assertEqual(conns[1].sent[0], {"op": 6, "d": {"token": TOKEN, "session_id": "sess", "seq": 3}})
        self.assertEqual(len(interactions), 1)
        self.assertEqual(self.registered, ["999999999999999999"])
        self.assertEqual(D._status["user"], "RedstoneBot")
        self.assertEqual(D._status["guilds"][GUILD], {"name": "Cielo Cloud",
                                                      "roles": [{"id": ROLE, "name": "Players"}]})

    def test_a_refused_token_stops_the_bot(self):
        g, conns, _ = self.run_gateway([[{"op": 10, "d": {"heartbeat_interval": 60000}},
                                         D.Closed(4004, "Authentication failed.")]])
        self.assertIsInstance(self.fatal, D.Fatal)
        self.assertIn("refused the bot token", str(self.fatal))
        self.assertEqual(len(conns), 1, "no reconnecting with a refused token")

    def test_no_heartbeat_ack_reconnects(self):
        hello = {"op": 10, "d": {"heartbeat_interval": 50}}
        g, conns, _ = self.run_gateway([[hello], [hello]])
        self.assertGreaterEqual(len(conns), 2)
        self.assertEqual(conns[0].sent[1]["op"], 1)
        self.assertEqual(conns[0].closed_with, 4000)

    def test_an_invalid_session_identifies_again(self):
        hello = {"op": 10, "d": {"heartbeat_interval": 60000}}
        first = [hello, {"op": 0, "s": 1, "t": "READY", "d": {"session_id": "s", "resume_gateway_url": "wss://r.test",
                                                               "user": {"id": "1", "username": "b"}}},
                 {"op": 9, "d": False}]
        g, conns, _ = self.run_gateway([first, [hello]])
        self.assertEqual(conns[1].sent[0]["op"], 2)

    def test_discord_asks_for_a_heartbeat(self):
        hello = {"op": 10, "d": {"heartbeat_interval": 60000}}
        g, conns, _ = self.run_gateway([[hello, {"op": 11}, {"op": 1}]])
        beats = [m for m in conns[0].sent if m["op"] == 1]
        self.assertEqual(len(beats), 2)


class Interaction(Fresh):
    def test_answers_through_the_callback_then_runs_the_start(self):
        calls, ran = [], []
        with mock.patch.object(D, "answer", lambda d: ({"type": 4, "data": {"content": "ok"}}, lambda: ran.append(1))), \
                mock.patch.object(D, "api", lambda *a, **k: calls.append(a)):
            D._handle_interaction({"id": "1", "token": "tok"}, TOKEN)
        self.assertEqual(calls, [("POST", "/interactions/1/tok/callback", {"type": 4, "data": {"content": "ok"}})])
        self.assertEqual(ran, [1])

    def test_an_error_still_answers(self):
        calls = []

        def boom(d):
            raise RuntimeError("bad")
        with mock.patch.object(D, "answer", boom), mock.patch.object(D, "api", lambda *a, **k: calls.append(a)):
            D._handle_interaction({"id": "1", "token": "tok"}, TOKEN)
        self.assertEqual(calls[0][2]["data"]["flags"], D.EPHEMERAL)

    def test_commands_registered_once_as_a_bulk_overwrite(self):
        calls = []
        with mock.patch.object(D, "api", lambda *a, **k: calls.append((a, k))):
            D.register_commands(TOKEN, "999999999999999999")
        (method, path, body), kw = calls[0]
        self.assertEqual((method, path), ("PUT", "/applications/999999999999999999/commands"))
        self.assertEqual([c["name"] for c in body], ["start"])
        self.assertEqual(body[0]["contexts"], [0], "guilds only, not direct messages")
        self.assertEqual(kw["token"], TOKEN)


@unittest.skipUnless(HAVE_FASTAPI, "needs fastapi")
class Api(Fresh):
    def setUp(self):
        super().setUp()
        import main
        self.main = main
        self.who = {"username": "quentin", "role": "admin", "source": "linux"}
        p = mock.patch.object(main.auth, "read_session_token", lambda t: dict(self.who))
        p.start()
        self.addCleanup(p.stop)
        self.client = TestClient(main.app)
        self.client.cookies.set(main.auth.SESSION_COOKIE, "session")

    def test_admins_set_it_up(self):
        r = self.client.put("/api/settings/discord", json={"token": TOKEN, "enabled": True, "guilds": [GUILD]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn(TOKEN, r.text)
        got = self.client.get("/api/settings/discord").json()
        self.assertEqual((got["enabled"], got["guilds"], got["servers"]), (True, [GUILD], ["*"]))
        r = self.client.put("/api/settings/discord", json={"token": "nope"})
        self.assertEqual(r.status_code, 400)

    def test_users_cant(self):
        self.who = {"username": "alex", "role": "user", "source": "panel"}
        self.assertEqual(self.client.get("/api/settings/discord").status_code, 403)
        self.assertEqual(self.client.put("/api/settings/discord", json={"enabled": False}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
