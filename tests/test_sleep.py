"""1.26: sleep mode (core/sleep.py) and Start on the join page.
Run: python -m unittest discover -s tests

A server with sleep mode on stops after N minutes with nobody on; the panel then listens on its
game port, answers the server list as "sleeping" and starts it when someone joins. Stop means
stopped. After a reboot it sleeps instead of starting (when Start at boot is on).
"""
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-sleep-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import platform  # noqa: E402
from core.platform import fake  # noqa: E402
from core import sleep as Z  # noqa: E402
from core import bootstart, joinpage, manager, portaudit  # noqa: E402


def free_port(kind=socket.SOCK_STREAM):
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def varint(n):
    return Z._varint(n)


def mc_string(t):
    return Z._mc_string(t)


def packet(pid, payload=b""):
    body = varint(pid) + payload
    return varint(len(body)) + body


def handshake(nxt, protocol=767, port=25565):
    return packet(0, varint(protocol) + mc_string("localhost") + struct.pack(">H", port) + varint(nxt))


def read_packet(sock):
    return Z._packet(sock)


JAVA_INFO = {"java": True, "motd": "Family World", "max": 20, "icon": "", "version": "1.21.11", "loader": "paper",
             "whitelist": None, "banned": {"griefer"}}


class Base(unittest.TestCase):
    def setUp(self):
        platform.use("fake")
        self.addCleanup(platform.use)
        fake.reset()
        d = tempfile.mkdtemp(dir=TMP)
        self.logs = []
        for p in (mock.patch.object(Z, "SLEEP_FILE", os.path.join(d, "sleep.json")),
                  mock.patch.object(bootstart, "BOOT_FILE", os.path.join(d, "boot.json")),
                  mock.patch.object(manager, "write_log", lambda t, *a, **k: self.logs.append(t)),
                  mock.patch.object(manager, "_check_server", lambda s: None),
                  mock.patch.object(Z, "why_not", lambda s: None)):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._close_all)

    def _close_all(self):
        for s in list(Z._listeners):
            Z._close(s)
        Z._empty_since.clear()
        Z._going.clear()

    def server(self, name="minecraft-java", state="inactive"):
        fake.state.servers[name] = {"state": state, "exec_start": "/usr/bin/java -jar server.jar"}


# ---------------------------------------------------------------------------
class JavaProtocol(Base):
    def talk(self, client_bytes, info=JAVA_INFO, woke=None):
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        a.sendall(client_bytes)
        got = Z.handle_java(b, "minecraft-java", info, wake_fn=lambda s, who: (woke if woke is not None else []).append((s, who)))
        a.settimeout(2)
        return got, a

    def test_server_list_ping_says_sleeping(self):
        got, a = self.talk(handshake(1, protocol=770) + packet(0) + packet(1, struct.pack(">q", 42)))
        self.assertEqual(got, "status")
        pid, data, i = read_packet(a)
        self.assertEqual(pid, 0)
        text, _ = Z._string(data, i)
        js = json.loads(text)
        self.assertEqual(js["version"]["protocol"], 770)          # the client's own: no "outdated" warning
        self.assertIn("Sleeping", js["description"]["text"])
        self.assertIn("Family World", js["description"]["text"])
        self.assertEqual(js["players"], {"max": 20, "online": 0})
        pid, data, i = read_packet(a)
        self.assertEqual((pid, data[i:]), (1, struct.pack(">q", 42)))   # the ping is answered too

    def test_the_join_pages_ping_reads_it(self):
        info = dict(JAVA_INFO, icon="iVBORw0KGgo=")
        self.assertIn("favicon", json.loads(Z.status_json(info, 767)))

    def test_joining_wakes_it_and_says_join_again(self):
        woke = []
        got, a = self.talk(handshake(2) + packet(0, mc_string("Steve") + b"\x00" * 16), woke=woke)
        self.assertEqual(got, "woke")
        self.assertEqual(woke[0][0], "minecraft-java")
        self.assertIn("Steve", woke[0][1])
        pid, data, i = read_packet(a)
        self.assertEqual(pid, 0)                                   # Login Disconnect
        text, _ = Z._string(data, i)
        self.assertIn("Join again in about 30 seconds", text)

    def test_modded_servers_say_a_minute(self):
        self.assertIn("a minute", Z.starting_message(dict(JAVA_INFO, loader="fabric")))

    def test_banned_and_not_whitelisted_dont_wake_it(self):
        woke = []
        got, a = self.talk(handshake(2) + packet(0, mc_string("Griefer")), woke=woke)
        self.assertEqual(got, "refused")
        self.assertIn("banned", Z._string(*read_packet(a)[1:])[0])
        got, a = self.talk(handshake(2) + packet(0, mc_string("Stranger")),
                           info=dict(JAVA_INFO, whitelist={"steve"}), woke=woke)
        self.assertEqual(got, "refused")
        self.assertIn("white-listed", Z._string(*read_packet(a)[1:])[0])
        got, _ = self.talk(handshake(2) + packet(0, mc_string("steve")), info=dict(JAVA_INFO, whitelist={"steve"}),
                           woke=woke)
        self.assertEqual(got, "woke")
        self.assertEqual(len(woke), 1)

    def test_legacy_ping_and_junk_are_ignored(self):
        woke = []
        self.assertIsNone(self.talk(b"\xfe\x01", woke=woke)[0])
        self.assertIsNone(self.talk(packet(5, b"x"), woke=woke)[0])
        self.assertEqual(woke, [])


class BedrockProtocol(Base):
    INFO = {"java": False, "motd": "Bedrock; World", "max": 10, "port": 19132, "portv6": 19133, "version": "1.26.66"}

    def test_pong_says_sleeping_with_the_last_known_version(self):
        a = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        b = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        b.bind(("127.0.0.1", 0))
        a.bind(("127.0.0.1", 0))
        ping = b"\x01" + struct.pack(">Q", 123) + Z.RAKNET_MAGIC + os.urandom(8)
        pong = ["MCPE", "Real", "924", "1.26.66", "0", "10", "1", "World", "Creative"]
        got = Z.handle_raknet(b, ping, a.getsockname(), "minecraft-bedrock", self.INFO, pong, wake_fn=None)
        self.assertEqual(got, "pong")
        a.settimeout(2)
        data, _ = a.recvfrom(2048)
        self.assertEqual(data[0], 0x1c)
        self.assertEqual(data[1:9], struct.pack(">Q", 123))
        text = data[data.index(Z.RAKNET_MAGIC) + 18:].decode()
        parts = text.split(";")
        self.assertEqual(parts[0], "MCPE")
        self.assertIn("sleeping", parts[1])
        self.assertNotIn("World;", parts[1])                    # the motd's ; can't break the fields
        self.assertEqual((parts[2], parts[3], parts[4], parts[8]), ("924", "1.26.66", "0", "Creative"))

    def test_open_connection_request_wakes_it(self):
        woke = []
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(s.close)
        req = b"\x05" + Z.RAKNET_MAGIC + b"\x0b" + b"\x00" * 100
        self.assertEqual(Z.handle_raknet(s, req, ("192.0.2.5", 5000), "minecraft-bedrock", self.INFO,
                                         wake_fn=lambda srv, who: woke.append(srv)), "woke")
        self.assertEqual(woke, ["minecraft-bedrock"])
        self.assertIsNone(Z.handle_raknet(s, b"\x05nomagic", ("192.0.2.5", 1), "x", self.INFO, wake_fn=woke.append))


# ---------------------------------------------------------------------------
class Listener(Base):
    def open_java(self, port):
        info = dict(JAVA_INFO, host="127.0.0.1", port=port, portv6=0, transport=None)
        with mock.patch.object(Z, "_props_info", lambda s: info):
            return Z._open("minecraft-java")

    def test_listens_answers_and_lets_go(self):
        port = free_port()
        lst = self.open_java(port)
        self.assertTrue(lst.socks, lst.error)
        self.assertEqual(Z.holds(port, "tcp"), "minecraft-java")
        t = threading.Thread(target=lambda: Z._serve_ready([lst.socks[0][0]]))
        t.start()
        got = joinpage.java_ping("127.0.0.1", port, timeout=3)
        t.join(5)
        self.assertIn("Sleeping", got["motd"])
        self.assertEqual(got["online"], 0)
        Z._close("minecraft-java")
        self.assertIsNone(Z.holds(port, "tcp"))
        with socket.socket() as s:                     # the server can take its port at once
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port))

    def test_a_taken_port_is_an_error_not_a_crash(self):
        with socket.socket() as other:
            other.bind(("127.0.0.1", 0))
            other.listen()
            lst = self.open_java(other.getsockname()[1])
            self.assertEqual(lst.socks, [])
            self.assertIn("in use", lst.error)

    def test_bedrock_raknet_listens_on_udp(self):
        port = free_port(socket.SOCK_DGRAM)
        info = {"java": False, "motd": "B", "max": 10, "host": "127.0.0.1", "port": port, "portv6": 0,
                "transport": "raknet", "version": "1.26"}
        with mock.patch.object(Z, "_props_info", lambda s: info):
            lst = Z._open("minecraft-bedrock")
        self.assertEqual([p for _, p in lst.socks], ["udp"])
        self.assertEqual(Z.holds(port, "udp"), "minecraft-bedrock")


# ---------------------------------------------------------------------------
class Lifecycle(Base):
    def test_turning_it_on_disables_start_at_boot_and_off_restores_it(self):
        self.server(state="active")
        fake.state.servers["minecraft-java"]["enabled"] = True
        info = Z.turn("minecraft-java", True, 10, actor="alex")
        self.assertEqual((info["enabled"], info["minutes"], info["state"]), (True, 10, "awake"))
        self.assertFalse(fake.state.servers["minecraft-java"].get("enabled"))
        self.assertTrue(bootstart.shown("minecraft-java"))          # the choice is kept and shown
        Z.turn("minecraft-java", False, actor="alex")
        self.assertTrue(fake.state.servers["minecraft-java"].get("enabled"))
        self.assertTrue(any("turned sleep mode on" in l for l in self.logs))

    def test_minutes_are_kept_in_range(self):
        self.server()
        self.assertEqual(Z.turn("minecraft-java", True, 0)["minutes"], Z.MIN_MINUTES)
        self.assertEqual(Z.turn("minecraft-java", True, 99999)["minutes"], Z.MAX_MINUTES)

    def test_empty_for_the_time_puts_it_to_sleep(self):
        self.server(state="active")
        Z.turn("minecraft-java", True, 15)
        slept = []
        with mock.patch.object(Z, "players_online", lambda s: 0), \
                mock.patch.object(Z.threading, "Thread", lambda target, args, **k: mock.Mock(
                    start=lambda: slept.append(args))):
            Z.check(now=1000)
            Z.check(now=1000 + 14 * 60)
            self.assertEqual(slept, [])
            Z.check(now=1000 + 15 * 60)
        self.assertEqual(slept[0][0], "minecraft-java")
        self.assertIn("15 minutes", slept[0][1])

    def test_someone_on_or_no_answer_resets_the_clock(self):
        self.server(state="active")
        Z.turn("minecraft-java", True, 15)
        counts = iter([0, 1, 0, None, 0])
        with mock.patch.object(Z, "players_online", lambda s: next(counts)), \
                mock.patch.object(Z, "_put_to_sleep") as put:
            for t in (0, 600, 700, 1200, 1300):
                Z.check(now=t)
        put.assert_not_called()
        self.assertEqual(Z._empty_since["minecraft-java"], 1300)

    def test_put_to_sleep_stops_it_and_listens(self):
        self.server(state="active")
        Z.turn("minecraft-java", True, 15)
        with mock.patch("core.backups._lock", lambda s: os.open(os.devnull, os.O_RDONLY)), \
                mock.patch.object(Z, "_open") as op:
            Z._put_to_sleep("minecraft-java", "Nobody has played")
        self.assertIn(("services.stop", "minecraft-java"), fake.calls)
        op.assert_called_once_with("minecraft-java")
        self.assertEqual(Z.state("minecraft-java"), "asleep")
        self.assertEqual(Z.list_fields("minecraft-java"), {"sleep": True, "sleeping": True})

    def test_a_running_backup_keeps_it_awake(self):
        from core import backups
        self.server(state="active")
        Z.turn("minecraft-java", True, 15)
        with mock.patch("core.backups._lock", side_effect=backups.BackupError("A backup of x is already running.")):
            with self.assertRaises(Z.SleepError):
                Z._put_to_sleep("minecraft-java", "x")
        self.assertNotIn(("services.stop", "minecraft-java"), fake.calls)

    def test_any_start_lets_go_of_the_port_first(self):
        self.server()
        Z.turn("minecraft-java", True, 15)
        Z._set_state("minecraft-java", "asleep")
        port = free_port()
        info = dict(JAVA_INFO, host="127.0.0.1", port=port, portv6=0, transport=None)
        with mock.patch.object(Z, "_props_info", lambda s: info):
            Z._open("minecraft-java")
        self.assertEqual(Z.holds(port, "tcp"), "minecraft-java")
        platform.on_before_start(Z.before_start)
        from core.platform import services
        services.start("minecraft-java")
        self.assertIsNone(Z.holds(port, "tcp"))
        self.assertEqual(Z.state("minecraft-java"), "awake")

    def test_stop_by_hand_means_stopped(self):
        self.server(state="active")
        Z.turn("minecraft-java", True, 15)
        Z._set_state("minecraft-java", "asleep")
        Z.stopped_by_hand("minecraft-java", "alex")
        self.assertEqual(Z.state("minecraft-java"), "off")
        self.assertEqual(Z.list_fields("minecraft-java")["sleeping"], False)

    def test_wake_starts_it_through_the_panel(self):
        self.server()
        Z.turn("minecraft-java", True, 15)
        Z._set_state("minecraft-java", "asleep")
        started = threading.Event()
        with mock.patch.object(manager, "control_server", lambda s, a: started.set()):
            self.assertTrue(Z.wake("minecraft-java", "Steve"))
            self.assertTrue(started.wait(3))
        self.assertEqual(Z.state("minecraft-java"), "awake")
        self.assertFalse(Z.wake("minecraft-java"))            # once only

    def test_a_failed_wake_goes_back_to_sleep(self):
        self.server()
        Z.turn("minecraft-java", True, 15)
        Z._set_state("minecraft-java", "asleep")
        done = threading.Event()
        with mock.patch.object(manager, "control_server", side_effect=manager.ServerError("its port is taken")), \
                mock.patch.object(Z, "_open", lambda s: done.set()):
            Z.wake("minecraft-java")
            self.assertTrue(done.wait(3))
        self.assertEqual(Z.state("minecraft-java"), "asleep")

    def test_started_from_outside_lets_go(self):
        self.server()
        Z.turn("minecraft-java", True, 15)
        Z._set_state("minecraft-java", "asleep")
        Z._listeners["minecraft-java"] = Z._Listener("minecraft-java", "java", {})
        Z._listeners["minecraft-java"].socks = [(socket.socket(), "tcp")]
        fake.state.servers["minecraft-java"]["state"] = "active"
        Z.watch_asleep()
        self.assertNotIn("minecraft-java", Z._listeners)
        self.assertEqual(Z.state("minecraft-java"), "awake")

    def test_a_crash_or_outside_stop_is_off(self):
        self.server(state="active")
        Z.turn("minecraft-java", True, 15)
        fake.state.servers["minecraft-java"]["state"] = "failed"
        Z.check(now=0)
        self.assertEqual(Z.state("minecraft-java"), "off")


class Boot(Base):
    def test_after_a_reboot_it_sleeps_if_start_at_boot_is_on(self):
        self.server(state="active")
        Z.turn("minecraft-java", True, 15)                   # awake when the machine went down
        fake.state.servers["minecraft-java"]["state"] = "inactive"
        fake.state.boot_time += 3600
        with mock.patch.object(Z, "_open") as op:
            Z.resume()
        op.assert_called_once_with("minecraft-java")
        self.assertEqual(Z.state("minecraft-java"), "asleep")

    def test_after_a_reboot_it_stays_stopped_if_start_at_boot_is_off(self):
        self.server(state="active")
        Z.turn("minecraft-java", True, 15)
        Z._set_state("minecraft-java", "asleep")
        bootstart.remember("minecraft-java", False)
        fake.state.servers["minecraft-java"]["state"] = "inactive"
        fake.state.boot_time += 3600
        with mock.patch.object(Z, "_open") as op:
            Z.resume()
        op.assert_not_called()
        self.assertEqual(Z.state("minecraft-java"), "off")

    def test_a_panel_restart_keeps_what_it_was(self):
        self.server()
        Z.turn("minecraft-java", True, 15)
        Z._set_state("minecraft-java", "asleep")
        self.server("minecraft-two")
        Z.turn("minecraft-two", True, 15)                   # stopped by hand: stays stopped
        with mock.patch.object(Z, "_open") as op:
            Z.resume()
        op.assert_called_once_with("minecraft-java")
        self.assertEqual((Z.state("minecraft-java"), Z.state("minecraft-two")), ("asleep", "off"))

    def test_apply_never_enables_a_sleeping_server(self):
        self.server()
        Z.turn("minecraft-java", True, 15)
        bootstart.apply("minecraft-java")
        self.assertIn(("services.disable", "minecraft-java"), fake.calls)
        self.assertNotIn(("services.enable", "minecraft-java"), fake.calls)


class Elsewhere(Base):
    def test_port_check_counts_the_listener_as_the_server(self):
        self.server()
        Z.turn("minecraft-java", True, 15)
        Z._set_state("minecraft-java", "asleep")
        with mock.patch.object(Z, "asleep_ports", lambda: {(25565, "tcp"): "minecraft-java"}):
            got = portaudit.owner({"port": 25565, "proto": "tcp", "pid": os.getpid(), "service": "redstonepanel.service"},
                                  {"minecraft-java"})
            self.assertEqual(got, ("server", "minecraft-java"))
            got = portaudit.owner({"port": 8080, "proto": "tcp", "pid": os.getpid(), "service": ""}, {"minecraft-java"})
            self.assertEqual(got, ("panel", None))

    def test_a_move_carries_it(self):
        from core import transfer
        self.server()
        Z.turn("minecraft-java", True, 20)
        self.assertEqual(transfer._sleep_minutes("minecraft-java"), 20)
        meta = transfer._clean_meta({"edition": "java", "name": "minecraft-java", "sleep": 20})
        self.assertEqual(meta["sleep"], 20)
        for bad in (True, "20", 0, 99999, None):
            self.assertIsNone(transfer._clean_meta({"edition": "java", "sleep": bad})["sleep"], bad)
        Z.remember("minecraft-new", meta["sleep"])
        self.assertEqual(Z.minutes("minecraft-new"), 20)

    def test_removing_a_server_forgets_it(self):
        self.server()
        Z.turn("minecraft-java", True, 15)
        Z.forget("minecraft-java")
        self.assertFalse(Z.enabled("minecraft-java"))

    def test_stop_message_says_asleep(self):
        from core import notify
        self.server()
        Z.turn("minecraft-java", True, 15)
        Z._set_state("minecraft-java", "asleep")
        sent = []
        with mock.patch.object(notify, "_is_panel_server", lambda s: True), \
                mock.patch.object(notify, "stop_requested", lambda s: True), \
                mock.patch.object(notify, "send", lambda e, s, m: sent.append((e, m))):
            notify.unit_event("stop", "minecraft-java", env={"SERVICE_RESULT": "success"})
        self.assertEqual(sent[0][0], "stopped")
        self.assertIn("went to sleep", sent[0][1])


# ---------------------------------------------------------------------------
class JoinPageStart(unittest.TestCase):
    """The join page's Start button (1.26): only where allowed, cooldown, a few per visitor."""

    def setUp(self):
        from core import settings as P
        from core import joinsite as S
        self.S, self.J = S, joinpage
        self.dir = tempfile.mkdtemp(dir=TMP)
        self.status = {"minecraft-survival": "offline"}
        self.started = []
        patches = [
            mock.patch.object(manager, "DATA_DIR", self.dir),
            mock.patch.object(manager, "write_log", lambda *a, **k: None),
            mock.patch.object(P, "load", lambda: dict(P.DEFAULTS, join_enabled=True)),
            mock.patch.object(joinpage, "summary", lambda key, fresh=False: {
                "name": "minecraft-survival", "edition": "Java", "loader": "paper", "minecraft": "1.21.11",
                "status": self.status["minecraft-survival"], "port": "25565", "motd": "Family", "transport": None,
                "geyser_port": "", "modpack": None, "icon": "", "players": None, "sleeping": False}),
            mock.patch.object(joinpage, "_start_server", lambda m, s, ip: self.started.append((m, s, ip))),
            mock.patch.object(joinpage.threading, "Thread", lambda target, args, **k: mock.Mock(
                start=lambda: target(*args))),
            mock.patch.dict(joinpage._starts, {}, clear=True),
            mock.patch.dict(joinpage._starts_by_ip, {}, clear=True),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        S._rate.clear()
        joinpage._summary_cache.clear()

    def on(self, **kw):
        return joinpage.save_page("local", "minecraft-survival", dict({"enabled": True}, **kw), "admin")

    def post(self, ip="203.0.113.9", header="1", path="/join/survival/start"):
        return self.S.handle("POST", path, ip, start_header=header)

    def test_off_by_default_and_then_not_found(self):
        self.assertFalse(self.on()["allow_start"])
        self.assertEqual(self.post().status, 404)
        self.assertNotIn('id="start"', self.S.handle("GET", "/join/survival", "203.0.113.9").body.decode())
        self.assertEqual(self.started, [])

    def test_start_from_the_page(self):
        self.on(allow_start=True)
        page = self.S.handle("GET", "/join/survival", "203.0.113.9").body.decode()
        self.assertIn('data-start="/join/survival/start"', page)
        r = self.post()
        self.assertEqual((r.status, json.loads(r.body)), (202, {"status": "starting"}))
        self.assertEqual(self.started, [("local", "minecraft-survival", "203.0.113.9")])
        st = json.loads(self.S.handle("GET", "/join/survival/status.json", "203.0.113.9").body)
        self.assertEqual(st["status"], "starting")
        self.assertTrue(st["can_start"])

    def test_needs_the_header_so_other_sites_cant_post(self):
        self.on(allow_start=True)
        self.assertEqual(self.post(header=None).status, 400)
        self.assertEqual(self.S.handle("GET", "/join/survival/start", "203.0.113.9").status, 404)
        self.assertEqual(self.S.handle("POST", "/join/survival", "203.0.113.9", start_header="1").status, 404)
        self.assertEqual(self.S.handle("POST", "/join/survival/status.json", "203.0.113.9", start_header="1").status, 404)
        self.assertEqual(self.started, [])

    def test_one_start_per_cooldown_and_a_few_per_visitor(self):
        self.on(allow_start=True)
        self.assertEqual(self.post().status, 202)
        self.assertEqual(self.post(ip="198.51.100.1").status, 202)     # within the cooldown: not repeated
        self.assertEqual(len(self.started), 1)
        for i in range(2):
            joinpage._starts.clear()
            self.assertEqual(self.post().status, 202)
        joinpage._starts.clear()
        r = self.post()
        self.assertEqual(r.status, 429)
        self.assertIn("Try again later", json.loads(r.body)["error"])
        self.assertEqual(len(self.started), 3)

    def test_online_server_isnt_started(self):
        self.on(allow_start=True)
        self.status["minecraft-survival"] = "online"
        r = self.post()
        self.assertEqual(json.loads(r.body), {"status": "online"})
        self.assertEqual(self.started, [])


if __name__ == "__main__":
    unittest.main()
