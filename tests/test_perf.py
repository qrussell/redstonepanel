"""Tests for core/perf.py (1.17): tick health parsing, ring files, memory and hard limits,
suggestions and alerts. Units, server folders and the ring files live in temp folders; systemctl
is recorded instead of run. Run: python -m unittest discover -s tests"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-perf-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import manager as M, notify as N, perf as P  # noqa: E402

try:
    import fastapi  # noqa: F401
    HAVE_FASTAPI = True
except ImportError:
    HAVE_FASTAPI = False

GB = 1024 ** 3
MB = 1024 ** 2

PAPER_TPS = "§6TPS from last 1m, 5m, 15m: §a*20.0, §a19.87, §e17.5"
PAPER_MSPT = ("§6Server tick times §e(§7avg§e/§7min§e/§7max§e)§6 from last 5s§7,§6 10s§7,§6 1m§e:\n"
              "§6◴ §a12.3§7/§a4.1§7/§e48.0§7, §a11.0§7/§a4.0§7/§a40.2§7, §a10.1§7/§a3.9§7/§c88.4")
TICK_QUERY = ("The game is running normally\nTarget tick rate: 20.0 per second.\n"
              "Average time per tick: 62.5ms (Target: 50.0ms)\n"
              "Percentiles: P50: 60.1ms P95: 80.2ms P99: 95.0ms, sample: 100")


class TickParsing(unittest.TestCase):
    def test_paper_tps_with_colours_and_stars(self):
        self.assertEqual(P.parse_paper_tps(PAPER_TPS), [20.0, 19.87, 17.5])
        self.assertIsNone(P.parse_paper_tps("Unknown or incomplete command"))

    def test_paper_mspt_uses_the_last_5_seconds(self):
        self.assertEqual(P.parse_paper_mspt(PAPER_MSPT), {"avg": 12.3, "min": 4.1, "max": 48.0})
        self.assertIsNone(P.parse_paper_mspt("nope"))

    def test_tick_query(self):
        q = P.parse_tick_query(TICK_QUERY)
        self.assertEqual((q["mspt"], q["p50"], q["p95"], q["p99"], q["rate"], q["state"]),
                         (62.5, 60.1, 80.2, 95.0, 20.0, "normal"))
        self.assertEqual(P.parse_tick_query("The game is frozen\nAverage time per tick: 1.0ms")["state"], "frozen")
        self.assertIsNone(P.parse_tick_query("Unknown command"))

    def test_tps_from_mspt_caps_at_the_tick_rate(self):
        self.assertEqual(P.tps_from_mspt(62.5), 16.0)
        self.assertEqual(P.tps_from_mspt(2.0), 20.0)
        self.assertEqual(P.tps_from_mspt(0), 20.0)

    def test_query_paper_then_fallback(self):
        sent = []
        def paper(cmd, timeout=None):
            sent.append(cmd)
            return {"tps": PAPER_TPS, "mspt": PAPER_MSPT}.get(cmd, "")
        out = P.query_tick(paper, "paper")
        self.assertEqual((out["source"], out["tps"], out["tps_15m"], out["mspt"]), ("tps", 20.0, 17.5, 12.3))
        self.assertEqual(sent, ["tps", "mspt"])
        out = P.query_tick(lambda c, timeout=None: TICK_QUERY if c == "tick query" else "Unknown command", "fabric")
        self.assertEqual((out["source"], out["tps"], out["mspt"]), ("tick query", 16.0, 62.5))
        # Paper that doesn't know `tps` (shouldn't happen) still gets tick query
        out = P.query_tick(lambda c, timeout=None: TICK_QUERY if c == "tick query" else "", "paper")
        self.assertEqual(out["source"], "tick query")
        out = P.query_tick(lambda c, timeout=None: "Unknown or incomplete command", "vanilla")
        self.assertFalse(out["available"])

    def test_only_read_only_commands_are_sent(self):
        sent = []
        def call(cmd, timeout=None):
            sent.append(cmd)
            return ""
        for loader in ("paper", "fabric", "vanilla"):
            P.query_tick(call, loader)
        self.assertLessEqual(set(sent), {"tps", "mspt", "tick query"})

    def test_bedrock_and_stopped_servers(self):
        self.assertFalse(P.tick_health("minecraft-b", {"edition": "Bedrock", "status": "online"})["available"])
        self.assertIn("isn't running", P.tick_health("minecraft-j", {"edition": "Java", "status": "offline"})["why"])

    def test_rcon_off(self):
        P._ticks.clear()
        with mock.patch.object(P.backups, "_java_rcon", return_value=None):
            out = P.tick_health("minecraft-norcon", {"edition": "Java", "status": "online", "directory": TMP})
        self.assertIn("RCON is off", out["why"])


class Rings(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(dir=TMP), "metrics", "s.ring")
        self.ring = P.Ring(self.path, 3)

    def test_fixed_size_and_round_trip(self):
        t = 1_700_000_000 - 1_700_000_000 % 900
        self.ring.write(t + 5, [1.5, None, 3])
        self.assertEqual(os.path.getsize(self.path), self.ring.size)
        self.assertEqual(oct(os.stat(self.path).st_mode & 0o777), "0o600")
        self.assertEqual(self.ring.read(0, now=t + 10), [(t, [1.5, None, 3.0])])

    def test_wraps_and_drops_old_slots(self):
        t0 = 1_700_000_000 - 1_700_000_000 % 900
        for i in range(1500):                         # more than a day of minutes
            self.ring.write(t0 + 60 * i, [i, i, i])
        rows = self.ring.read(0, now=t0 + 60 * 1499)
        self.assertEqual(len(rows), 1440)
        self.assertEqual(rows[0][1][0], 60.0)          # the first 60 minutes were overwritten
        self.assertEqual(rows[-1][1][0], 1499.0)
        self.assertEqual(os.path.getsize(self.path), self.ring.size)
        # a week later the day tier is all too old
        self.assertEqual(self.ring.read(0, now=t0 + 8 * 86400), [])

    def test_week_tier_averages_its_bucket(self):
        t0 = 1_800_000_000 - 1_800_000_000 % 900
        for i in range(15):
            self.ring.write(t0 + 60 * i, [i, None if i % 2 else 10, 0])
        rows = self.ring.read(1, now=t0 + 900)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], t0)
        self.assertAlmostEqual(rows[0][1][0], 7.0)       # mean of 0..14
        self.assertAlmostEqual(rows[0][1][1], 10.0)      # NaNs left out

    def test_a_foreign_file_is_replaced(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "wb") as f:
            f.write(b"garbage")
        self.assertEqual(self.ring.read(0), [])
        self.ring.write(1_700_000_040, [1, 2, 3])
        self.assertEqual(os.path.getsize(self.path), self.ring.size)

    def test_history_ranges(self):
        with mock.patch.object(P, "METRICS_DIR", os.path.dirname(self.path)):
            self.assertEqual(P.host_history("week")["step"], 900)
            self.assertEqual(P.host_history("day")["fields"], list(P.HOST_FIELDS))
            with self.assertRaises(P.PerfError):
                P.host_history("year")
        with self.assertRaises(P.PerfError):
            P.server_ring("../etc/passwd")


class Readers(unittest.TestCase):
    def test_connections_counts_established_on_the_port(self):
        text = ("  sl  local_address rem_address   st\n"
                "   0: 00000000:63DD 00000000:0000 0A 0\n"      # listening on 25565: not counted
                "   1: 0100007F:63DD 0100007F:D431 01 0\n"
                "   2: 0100007F:63DD 0100007F:D432 01 0\n"
                "   3: 0100007F:63DE 0100007F:D433 01 0\n")
        self.assertEqual(P.parse_proc_net_tcp(text, 25565), 2)

    def test_sizes(self):
        self.assertEqual(P.to_bytes("2G"), 2 * GB)
        self.assertEqual(P.to_bytes("1536m"), 1536 * MB)
        self.assertIsNone(P.to_bytes("2 GB"))
        self.assertEqual(P.heap_text(3 * GB), "3G")
        self.assertEqual(P.heap_text(1536 * MB), "1536M")
        self.assertEqual(P.auto_limit(2 * GB), 3 * GB)
        self.assertEqual(P.auto_limit(512 * MB), 1 * GB)      # at least 512 MB on top

    def test_dropin(self):
        self.assertEqual(P.dropin_text(3 * GB, 1.5).splitlines()[1:], ["[Service]", f"MemoryMax={3 * GB}", "CPUQuota=150%"])
        self.assertNotIn("CPUQuota", P.dropin_text(GB, None))


class Resources(unittest.TestCase):
    """Memory and hard limits, against a fake unit folder and server list."""

    def setUp(self):
        self.root = tempfile.mkdtemp(dir=TMP)
        self.units = os.path.join(self.root, "units")
        os.makedirs(self.units)
        self.dir = os.path.join(self.root, "opt", "minecraft-java")
        os.makedirs(self.dir)
        self.servers = [self.srv("minecraft-java", "2G", "online"), self.srv("minecraft-other", "3G", "online"),
                        self.srv("minecraft-bed", None, "online", "Bedrock")]
        self.exec = {"minecraft-java": "/usr/lib/jvm/java-21/bin/java -Xms1G -Xmx2G -jar server.jar nogui"}
        self.commands, self.written = [], []
        self.total = 8 * GB
        run = lambda cmd, **kw: (self.commands.append(cmd), mock.Mock(returncode=0, stdout="", stderr=""))[1]
        def write_unit(name, d, edition, xms=None, xmx=None, java="/usr/bin/java"):
            self.written.append((name, xms, xmx, java))
            self.exec[name] = f"{java} -Xms{xms} -Xmx{xmx} -jar server.jar nogui"
            next(s for s in self.servers if s["name"] == name)["ram"] = xmx
        patches = [
            mock.patch.object(P, "UNIT_DIR", self.units),
            mock.patch.object(P, "RUNTIME_CONTROL", os.path.join(self.root, "run")),
            mock.patch.object(P, "LIMITS_FILE", os.path.join(self.root, "limits.json")),
            mock.patch.object(P, "METRICS_DIR", os.path.join(self.root, "metrics")),
            mock.patch.object(P.subprocess, "run", run),
            mock.patch.object(P, "_exec_start", lambda s: self.exec.get(s, "")),
            mock.patch.object(P, "_host_total", lambda: self.total),
            mock.patch.object(P.manager, "list_servers", lambda: [dict(s) for s in self.servers]),
            mock.patch.object(P.manager, "_check_server", lambda s: None),
            mock.patch.object(P.manager, "_write_unit", write_unit),
            mock.patch.object(P.manager, "control_server", lambda s, a: self.commands.append(["control", s, a])),
            mock.patch.object(P.os, "cpu_count", lambda: 4),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def srv(self, name, ram, status, edition="Java"):
        return {"name": name, "ram": ram or "N/A", "status": status, "edition": edition,
                "directory": self.dir, "loader": "paper" if edition == "Java" else None, "port": "25565"}

    def dropin(self, name="minecraft-java"):
        try:
            with open(P._dropin_path(name)) as f:
                return f.read()
        except OSError:
            return None

    def test_view(self):
        v = P.resources("minecraft-java")
        self.assertEqual((v["xms"], v["xmx"], v["others_running"], v["room"]), ("1G", "2G", 3 * GB, 4 * GB))
        self.assertEqual(v["memory_limit"], "off")
        self.assertEqual(v["auto_limit"], 3 * GB)

    def test_set_memory_keeps_the_java_and_waits_for_a_restart(self):
        out = P.set_resources("minecraft-java", xms="1G", xmx="4g", actor="alex")
        self.assertEqual(self.written, [("minecraft-java", "1G", "4G", "/usr/lib/jvm/java-21/bin/java")])
        self.assertTrue(out["restart_needed"])
        self.assertNotIn(["control", "minecraft-java", "restart"], self.commands)
        P.set_resources("minecraft-java", xmx="3G", restart=True, actor="alex")
        self.assertIn(["control", "minecraft-java", "restart"], self.commands)

    def test_memory_refusals(self):
        for xms, xmx, why in (("1G", "2GB", "like 1G"), ("1G", "128M", "at least 256M"), ("3G", "2G", "starting memory"),
                              ("1G", "8G", "leave at least 1 GB"), ("1G", "5G", "other running servers")):
            with self.subTest(xmx=xmx), self.assertRaises(P.PerfError) as e:
                P.set_resources("minecraft-java", xms=xms, xmx=xmx)
            self.assertIn(why, str(e.exception))
        self.assertEqual(self.written, [])
        # 4G fits: 3G (other) + 4G = 7G = 8G - 1G
        P.set_resources("minecraft-java", xms="1G", xmx="4G")
        # a stopped server's heap doesn't count
        self.servers[1]["status"] = "offline"
        P.set_resources("minecraft-java", xms="1G", xmx="6G")
        with self.assertRaises(P.PerfError):
            P.set_resources("minecraft-bed", xmx="1G")

    def test_a_2gb_raspberry_pi_keeps_its_1gb_heap(self):
        self.total = 1894 * MB                      # what a 2 GB Pi 4 reports
        self.servers = self.servers[:1]
        out = P.set_resources("minecraft-java", xms="512M", xmx="1G")
        self.assertEqual(out["reserve"], 768 * MB)
        with self.assertRaises(P.PerfError) as e:
            P.set_resources("minecraft-java", xms="512M", xmx="1536M")
        self.assertIn("leave at least 768 MB", str(e.exception))
        self.assertIn("at most 1126M", str(e.exception))
        self.assertEqual(P.system_reserve(16 * GB), GB)

    def test_limits_dropin_and_live_change(self):
        P.set_resources("minecraft-java", memory="auto", cpu=1.5, actor="alex")
        self.assertIn(f"MemoryMax={3 * GB}", self.dropin())
        self.assertIn("CPUQuota=150%", self.dropin())
        self.assertIn(["systemctl", "set-property", "--runtime", "minecraft-java.service",
                       f"MemoryMax={3 * GB}", "CPUQuota=150%"], self.commands)
        # automatic follows a new heap
        P.set_resources("minecraft-java", xms="1G", xmx="4G")
        self.assertIn(f"MemoryMax={6 * GB}", self.dropin())
        # a fixed size
        P.set_resources("minecraft-java", memory="5G")
        self.assertIn(f"MemoryMax={5 * GB}", self.dropin())
        with open(os.path.join(self.root, "limits.json")) as f:
            self.assertEqual(json.load(f)["minecraft-java"], {"memory": 5 * GB, "cpu": 1.5})
        # off again: the drop-in goes and the cgroup is set back to no limit
        P.set_resources("minecraft-java", memory="off", cpu=0)
        self.assertIsNone(self.dropin())
        self.assertFalse(os.path.isdir(os.path.dirname(P._dropin_path("minecraft-java"))))
        sets = [c for c in self.commands if c[:2] == ["systemctl", "set-property"]]
        self.assertEqual(sets[-1][-2:], ["MemoryMax=infinity", "CPUQuota="])
        self.assertEqual(self.commands[-1], ["systemctl", "daemon-reload"])

    def test_runtime_overrides_are_cleared(self):
        """set-property --runtime files outrank the drop-in until reboot: they must not linger."""
        run = os.path.join(self.root, "run", "minecraft-java.service.d")
        def fake_run(cmd, **kw):
            self.commands.append(cmd)
            if cmd[:2] == ["systemctl", "set-property"]:
                os.makedirs(run, exist_ok=True)
                for n in P.RUNTIME_FILES:
                    open(os.path.join(run, n), "w").close()
            return mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(P.subprocess, "run", fake_run):
            P.set_resources("minecraft-java", cpu=1)
            self.assertFalse(os.path.exists(run))
            os.makedirs(run)
            open(os.path.join(run, "50-CPUQuota.conf"), "w").close()
            P.on_server_removed("minecraft-java", purged=False)
            self.assertFalse(os.path.exists(run))

    def test_limit_refusals(self):
        for mem, cpu, why in (("2G", None, "well above"), ("100M", None, "at least 256M"), ("lots", None, "like 3G"),
                              ("16G", None, "more than this machine"), (None, 8, "between 0.1 and 4"),
                              (None, 0.05, "between 0.1"), (None, "x", "number of cores")):
            with self.subTest(mem=mem, cpu=cpu), self.assertRaises(P.PerfError) as e:
                P.set_resources("minecraft-java", memory=mem, cpu=cpu)
            self.assertIn(why, str(e.exception))
        self.assertIsNone(self.dropin())

    def test_bedrock_gets_limits_but_not_automatic(self):
        with self.assertRaises(P.PerfError):
            P.set_resources("minecraft-bed", memory="auto")
        P.set_resources("minecraft-bed", memory="1G", cpu=2)
        self.assertIn(f"MemoryMax={GB}", self.dropin("minecraft-bed"))

    def test_remove_and_restore(self):
        P.set_resources("minecraft-java", memory="auto", cpu=1)
        P.on_server_removed("minecraft-java", purged=False)
        self.assertIsNone(self.dropin())
        P.on_server_restored("minecraft-java")
        self.assertIn("CPUQuota=100%", self.dropin())
        P.server_ring("minecraft-java").write(1_700_000_000, [1, 2, 3, 4, 5])
        P.on_server_removed("minecraft-java", purged=True)
        self.assertIsNone(self.dropin())
        self.assertEqual(P.get_limits("minecraft-java"), {"memory": "off", "cpu": None})
        self.assertFalse(os.path.exists(os.path.join(self.root, "metrics", "minecraft-java.ring")))
        P.on_server_restored("minecraft-java")
        self.assertIsNone(self.dropin())

    def test_a_stored_limit_file_is_not_trusted(self):
        with open(os.path.join(self.root, "limits.json"), "w") as f:
            json.dump({"minecraft-java": {"memory": "1; rm -rf /", "cpu": -3}}, f)
        self.assertEqual(P.get_limits("minecraft-java"), {"memory": "off", "cpu": None})


class Suggestions(unittest.TestCase):
    NOW = 1_700_000_000

    def setUp(self):
        self.dir = tempfile.mkdtemp(dir=TMP)
        with open(os.path.join(self.dir, "server.properties"), "w") as f:
            f.write("simulation-distance=12\nview-distance=14\n")

    def srv(self, loader="paper", status="online"):
        return {"name": "minecraft-s", "edition": "Java", "loader": loader, "status": status, "directory": self.dir}

    def rows(self, tps, minutes=12):
        return [(self.NOW - 60 * i, [10, GB, 3, tps, 50]) for i in range(minutes)]

    def ids(self, srv, rows=(), view=None, unit=None, measured=None):
        m = {"backups_scheduled": True}
        m.update(measured or {})
        return [s["id"] for s in P.suggestions(srv, list(rows), view or {}, unit or {}, self.NOW, m)]

    def test_nothing_measured_nothing_suggested(self):
        self.assertEqual(self.ids(self.srv()), [])

    def test_low_tps_for_ten_minutes(self):
        out = P.suggestions(self.srv(), self.rows(15.2), {}, {}, self.NOW, {"backups_scheduled": True})
        self.assertEqual(out[0]["id"], "tps_low")
        self.assertIn("now 12", out[0]["text"])
        self.assertIn("lowest 15.2", out[0]["text"])
        self.assertNotIn("tps_low", self.ids(self.srv(), self.rows(15.2, minutes=5)))     # not 10 minutes yet
        self.assertNotIn("tps_low", self.ids(self.srv(), self.rows(19.9)))
        rows = self.rows(15)
        rows[3] = (rows[3][0], [10, GB, 3, None, None])                                # a gap: not measured
        self.assertNotIn("tps_low", self.ids(self.srv(), rows))
        self.assertIn("vanilla_slow", self.ids(self.srv("vanilla"), self.rows(12)))
        self.assertNotIn("tps_low", self.ids(self.srv(status="offline"), self.rows(12)))

    def test_fabric_without_performance_mods(self):
        self.assertIn("lithium", self.ids(self.srv("fabric")))
        os.makedirs(os.path.join(self.dir, "mods"))
        open(os.path.join(self.dir, "mods", "lithium-fabric-0.14.jar"), "w").close()
        self.assertNotIn("lithium", self.ids(self.srv("fabric")))
        self.assertIn("ferritecore", self.ids(self.srv("fabric"), measured={"memory_tight": True}))

    def test_worldedit_on_paper(self):
        os.makedirs(os.path.join(self.dir, "plugins"))
        open(os.path.join(self.dir, "plugins", "worldedit-bukkit-7.3.jar"), "w").close()
        self.assertIn("fawe", self.ids(self.srv()))
        open(os.path.join(self.dir, "plugins", "FastAsyncWorldEdit-2.12.jar"), "w").close()
        self.assertNotIn("fawe", self.ids(self.srv()))

    def test_memory(self):
        view = {"xmx": "2G", "xmx_bytes": 2 * GB, "room": 4 * GB, "host_memory": 8 * GB}
        unit = {"NRestarts": "3", "memory": 2 * GB + 1}
        out = P.suggestions(self.srv(), [], view, unit, self.NOW, {"backups_scheduled": True})
        self.assertEqual(out[0]["id"], "raise_memory")
        self.assertIn("This machine has room", out[0]["text"])
        view["room"] = 0
        self.assertIn("no room", P.suggestions(self.srv(), [], view, unit, self.NOW, {})[0]["text"])
        self.assertIn("limit_hit", self.ids(self.srv(), view=view, unit={"Result": "oom-kill"}))
        self.assertIn("overcommit", self.ids(self.srv(), view=view, measured={"all_heaps": 8 * GB}))
        self.assertIn("swap", self.ids(self.srv(), view=view, measured={"swap_percent": 40}))

    def test_world_logs_disk_backups(self):
        self.assertIn("border", self.ids(self.srv(), measured={"world_size": 5 * GB, "border": 59999968.0}))
        self.assertNotIn("border", self.ids(self.srv(), measured={"world_size": 5 * GB, "border": 10000.0}))
        self.assertNotIn("border", self.ids(self.srv(), measured={"world_size": GB, "border": 59999968.0}))
        self.assertIn("logs", self.ids(self.srv(), measured={"logs_size": 2 * GB}))
        self.assertIn("hdd", self.ids(self.srv(), measured={"rotational": True}))
        self.assertIn("backups", self.ids(self.srv(), measured={"backups_scheduled": False}))


class Alerts(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(dir=TMP)
        self.sent = []
        for p in (mock.patch.object(P, "METRICS_DIR", os.path.join(self.root, "metrics")),
                  mock.patch.object(P.notify, "send", lambda *a, **kw: self.sent.append((a, kw)))):
            p.start()
            self.addCleanup(p.stop)

    def test_tps_low_after_five_minutes(self):
        now = 1_700_000_000 - 1_700_000_000 % 60
        ring = P.server_ring("minecraft-lag")
        for i in range(6):
            ring.write(now - 60 * i, [50, GB, 2, 12.5 + i * 0.1, 80])
        srv = {"name": "minecraft-lag", "status": "online", "edition": "Java"}
        P.alerts([srv], [], now)
        self.assertEqual(self.sent[0][0][:2], ("tps_low", "minecraft-lag"))
        self.assertIn("lowest 12.5", self.sent[0][0][2])
        self.sent.clear()
        P.alerts([dict(srv, status="offline")], [], now)
        self.assertEqual(self.sent, [])

    def test_disk_full(self):
        P.alerts([], [{"mount": "/", "holds": ["Game servers"], "percent": 96.2, "free": GB}], 1)
        self.assertEqual(self.sent[0][0][0], "disk_full")
        self.assertEqual(self.sent[0][1]["quiet_key"], "disk:/")
        self.sent.clear()
        P.alerts([], [{"mount": "/", "holds": ["System"], "percent": 90, "free": GB}], 1)
        self.assertEqual(self.sent, [])


class AlertEvents(unittest.TestCase):
    def test_new_events_are_off_by_default_and_hourly(self):
        for e in ("tps_low", "disk_full"):
            self.assertIn(e, N.EVENTS)
            self.assertNotIn(e, N.DEFAULT_EVENTS)
            self.assertEqual(N.QUIET_SECONDS[e], 3600)

    def test_quiet_key(self):
        with mock.patch.object(N, "STATE_FILE", os.path.join(tempfile.mkdtemp(dir=TMP), "state.json")):
            self.assertFalse(N._quiet("disk_full", "disk:/"))
            self.assertTrue(N._quiet("disk_full", "disk:/"))
            self.assertFalse(N._quiet("disk_full", "disk:/srv"))


@unittest.skipUnless(HAVE_FASTAPI, "needs fastapi")
class Routes(unittest.TestCase):
    def test_routes_exist_and_changes_are_admin_only(self):
        import main
        paths = {(m, r.path) for r in main.app.routes for m in getattr(r, "methods", ())}
        for want in (("GET", "/api/servers/{server_name}/perf"), ("GET", "/api/servers/{server_name}/resources"),
                     ("PUT", "/api/servers/{server_name}/resources"), ("GET", "/api/host/history")):
            self.assertIn(want, paths)
        import inspect
        self.assertIn("_require_admin", inspect.getsource(main.set_resources))

    def test_packaged(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "debian", "redstonepanel.install")) as f:
            self.assertIn("core/perf.py", f.read())


if __name__ == "__main__":
    unittest.main()
