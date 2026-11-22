"""Tests for New server's Advanced options (1.17.2): core/deployopts.py's checks, startup flags
in the unit, pinned builds and snapshots in core/javaservers.py, and deploy_server() carrying
out a plan. No network, systemctl or root: downloads are stubbed and units go to a temp folder.
Run: python -m unittest discover -s tests"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-deployopts-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import deployopts as D, javaservers as J, manager as M  # noqa: E402

G = 1024 ** 3


class Validate(unittest.TestCase):
    def test_empty_is_no_plan(self):
        self.assertEqual(D.validate(None, "java"), {})
        self.assertEqual(D.validate({}, "java"), {})
        self.assertEqual(D.validate({"seed": "", "port": None}, "java"), {})

    def test_full_java_plan(self):
        plan = D.validate({"name": "Survival-1", "port": "25600", "build": "232", "java": "21", "heap": "4g",
                           "flags": {"preset": "aikar", "extra": ["-XX:+UseZGC", "-Dfile.encoding=UTF-8"]},
                           "seed": "-12345 abc", "level_type": "amplified", "level_name": "My World",
                           "motd": "Hello §aworld", "gamemode": "creative", "difficulty": "hard",
                           "max_players": "12", "online_mode": False, "whitelist_players": "Alex Steve_2, alex",
                           "ops": ["Alex"]}, "java", "paper")
        self.assertEqual(plan["name"], "minecraft-survival-1")
        self.assertEqual(plan["port"], 25600)
        self.assertEqual(plan["build"], "232")
        self.assertEqual(plan["java"], 21)
        self.assertEqual(plan["heap"], "4G")
        self.assertEqual(plan["flags"], {"preset": "aikar", "extra": ["-XX:+UseZGC", "-Dfile.encoding=UTF-8"]})
        self.assertEqual(plan["level_type"], "amplified")
        self.assertEqual(plan["whitelist_players"], ["Alex", "Steve_2"])     # duplicates dropped
        self.assertEqual(plan["ops"], ["Alex"])
        self.assertEqual(plan["properties"], {"level-seed": "-12345 abc", "level-name": "My World",
                                              "motd": "Hello §aworld", "max-players": 12,
                                              "online-mode": "false", "white-list": "true"})
        self.assertIn("flags: Aikar's flags, -XX:+UseZGC", D.summary(plan))

    def test_name_rules(self):
        self.assertEqual(D.validate({"name": "minecraft-lobby"}, "java")["name"], "minecraft-lobby")
        for bad in ("-x", "a b", "x" * 31, "../etc", "naïve", "a_b"):
            with self.assertRaises(D.OptionError, msg=bad):
                D.validate({"name": bad}, "java")
        # The panel tells editions apart by "java" in a unit's command line
        with self.assertRaises(D.OptionError):
            D.validate({"name": "javaland"}, "bedrock")
        self.assertEqual(D.validate({"name": "javaland"}, "java")["name"], "minecraft-javaland")

    def test_ports(self):
        for bad in ("80", "70000", "abc", "1023"):
            with self.assertRaises(D.OptionError):
                D.validate({"port": bad}, "java")
        with self.assertRaises(D.OptionError):
            D.validate({"port": 65535}, "bedrock")          # it needs port + 1 for IPv6

    def test_bedrock_refuses_java_only_options(self):
        for k, v in (("heap", "2G"), ("java", 21), ("build", "1"), ("flags", {"preset": "aikar"}), ("ops", "Alex")):
            with self.assertRaises(D.OptionError, msg=k):
                D.validate({k: v}, "bedrock")
        plan = D.validate({"motd": "Hi", "level_type": "flat", "gamemode": "adventure",
                           "whitelist_players": "Some Gamer"}, "bedrock")
        self.assertEqual(plan["properties"], {"server-name": "Hi", "allow-list": "true"})
        with self.assertRaises(D.OptionError):
            D.validate({"level_type": "amplified"}, "bedrock")
        with self.assertRaises(D.OptionError):
            D.validate({"gamemode": "spectator"}, "bedrock")

    def test_builds_and_snapshots_by_type(self):
        with self.assertRaises(D.OptionError):
            D.validate({"build": "232"}, "java", "vanilla")
        self.assertNotIn("build", D.validate({"build": "latest"}, "java", "paper"))
        with self.assertRaises(D.OptionError):
            D.validate({"snapshot": True}, "java", "paper")
        self.assertTrue(D.validate({"snapshot": "on"}, "java", "vanilla")["snapshot"])
        self.assertNotIn("snapshot", D.validate({"snapshot": False}, "java", "vanilla"))

    def test_hard_limits(self):
        self.assertEqual(D.validate({"memory_limit": "auto", "cpu": "1.5"}, "java"), {"memory_limit": "auto", "cpu": 1.5})
        self.assertEqual(D.validate({"memory_limit": "3g"}, "bedrock"), {"memory_limit": "3G"})
        self.assertEqual(D.validate({"memory_limit": "off", "cpu": 0}, "java"), {})
        with self.assertRaises(D.OptionError):
            D.validate({"memory_limit": "auto"}, "bedrock")        # nothing to follow
        for bad in ({"memory_limit": "100M"}, {"memory_limit": "lots"}, {"cpu": "0.05"}, {"cpu": "x"},
                    {"cpu": (os.cpu_count() or 1) + 1}):
            with self.assertRaises(D.OptionError, msg=bad):
                D.validate(bad, "java")
        self.assertIn("hard memory limit auto; CPU 1.5 cores", D.summary({"memory_limit": "auto", "cpu": 1.5}))

    def test_heap_bounds(self):
        for bad in ("256M", "300G", "2T", "2", "1.5G"):
            with self.assertRaises(D.OptionError, msg=bad):
                D.validate({"heap": bad}, "java")
        self.assertEqual(D.validate({"heap": "1536m"}, "java")["heap"], "1536M")

    def test_unknown_keys_and_lines(self):
        with self.assertRaises(D.OptionError):
            D.validate({"docker_network": "x"}, "java")
        with self.assertRaises(D.OptionError):
            D.validate({"seed": "1\nrcon.password=x"}, "java")
        with self.assertRaises(D.OptionError):
            D.validate({"motd": "a\rb"}, "java")
        with self.assertRaises(D.OptionError):
            D.validate({"whitelist_players": "ab"}, "java")    # Java names are 3 to 16 characters

    def test_extra_flags_admins_only(self):
        with self.assertRaises(D.OptionError):
            D.validate({"flags": {"preset": "none", "extra": ["-XX:+UseZGC"]}}, "java", is_admin=False)
        self.assertEqual(D.validate({"flags": {"preset": "aikar"}}, "java", is_admin=False)["flags"]["preset"], "aikar")
        self.assertNotIn("flags", D.validate({"flags": {"preset": "none", "extra": []}}, "java"))


class Flags(unittest.TestCase):
    def test_allowed_shapes(self):
        for ok in ("-XX:+UseZGC", "-XX:-UseBiasedLocking", "-XX:MaxGCPauseMillis=100", "-XX:HeapDumpPath=/opt/x",
                   "-Dfile.encoding=UTF-8", "-Dpaper.disableChannelLimit", "-Xss1M", "-Dlog4j2.formatMsgNoLookups=true"):
            self.assertEqual(D.check_flag(ok), ok)

    def test_refused(self):
        for bad in ("-Xmx4G", "-Xms1G", "-javaagent:/tmp/a.jar", "-jar", "--add-opens=x", "-XX:OnError=sh",
                    "-XX:OnOutOfMemoryError=kill", "-XX:Flags=/tmp/f", "-XX:VMOptionsFile=/tmp/f",
                    "-Dx=$HOME", "-Dx=%h", "-Dx='a'", '-Dx="a"', "-Dx=a;b", "-Dx=a\\b", "-XX:+Use ZGC", "-Dx=a\nb",
                    "-cp", "@/tmp/argfile"):
            with self.assertRaises(D.OptionError, msg=bad):
                D.check_flag(bad)
        with self.assertRaises(D.OptionError):
            D.clean_flags({"preset": "meowice"})
        with self.assertRaises(D.OptionError):
            D.clean_flags({"extra": ["-Xss1M"] * 25})

    def test_aikar_sizes(self):
        small = D.aikar_flags(4 * G)
        self.assertIn("-XX:G1HeapRegionSize=8M", small)
        self.assertIn("-XX:G1NewSizePercent=30", small)
        big = D.aikar_flags(16 * G)
        self.assertIn("-XX:G1HeapRegionSize=16M", big)
        self.assertIn("-XX:G1NewSizePercent=40", big)
        self.assertIn("-XX:InitiatingHeapOccupancyPercent=20", big)
        for f in small:                             # every preset flag passes the same check
            D.check_flag(f)

    def test_store(self):
        with mock.patch.object(D, "FLAGS_FILE", os.path.join(tempfile.mkdtemp(dir=TMP), "jvm-flags.json")):
            self.assertIsNone(D.get_flags("minecraft-a"))
            D.set_flags("minecraft-a", {"preset": "aikar", "extra": []})
            D.set_flags("minecraft-b", {"preset": "none", "extra": ["-XX:+UseZGC"]})
            self.assertEqual(D.get_flags("minecraft-a")["preset"], "aikar")
            self.assertEqual(oct(os.stat(D.FLAGS_FILE).st_mode & 0o777), "0o600")
            D.set_flags("minecraft-a", {})
            self.assertIsNone(D.get_flags("minecraft-a"))
            self.assertEqual(D.get_flags("minecraft-b")["extra"], ["-XX:+UseZGC"])
            # A hand-edited bad entry is ignored rather than written into a unit
            with open(D.FLAGS_FILE, "w") as f:
                json.dump({"minecraft-b": {"preset": "none", "extra": ["-XX:OnError=sh"]}}, f)
            self.assertIsNone(D.get_flags("minecraft-b"))


class Values(unittest.TestCase):
    def test_level_type_by_version(self):
        self.assertEqual(D.level_type_value("flat", "java", "1.21.4"), "minecraft:flat")
        self.assertEqual(D.level_type_value("large_biomes", "java", "26.1"), "minecraft:large_biomes")
        self.assertEqual(D.level_type_value("large_biomes", "java", "1.16.5"), "largeBiomes")
        self.assertEqual(D.level_type_value("flat", "java", "25w14a"), "minecraft:flat")
        self.assertEqual(D.level_type_value("flat", "bedrock"), "FLAT")

    def test_modes_by_version(self):
        self.assertEqual(D.mode_value("gamemode", "creative", "java", "1.21.4"), "creative")
        self.assertEqual(D.mode_value("gamemode", "creative", "java", "1.12.2"), "1")
        self.assertEqual(D.mode_value("difficulty", "hard", "java", "1.8.9"), "3")
        self.assertEqual(D.mode_value("difficulty", "hard", "bedrock"), "hard")


MANIFEST = {"versions": [
    {"id": "26.1-snapshot-2", "type": "snapshot", "url": "u/s2", "releaseTime": "2026-11-18T10:00:00+00:00"},
    {"id": "26.1-pre1", "type": "snapshot", "url": "u/p1", "releaseTime": "2026-11-20T10:00:00+00:00"},
    {"id": "1.21.11", "type": "release", "url": "u/r11", "releaseTime": "2026-10-01T10:00:00+00:00"},
    {"id": "25w45a", "type": "snapshot", "url": "u/old", "releaseTime": "2026-09-01T10:00:00+00:00"},
    {"id": "1.21.10", "type": "release", "url": "u/r10", "releaseTime": "2026-07-01T10:00:00+00:00"},
]}
VERSION = {"downloads": {"server": {"url": "https://piston-data.mojang.com/x/server.jar", "sha1": "ab"}},
           "javaVersion": {"majorVersion": 21}}
PAPER = {"versions": {"1.21": ["1.21.11", "1.21.10"]}}
PAPER_BUILDS = [
    {"id": 120, "channel": "STABLE", "downloads": {"server:default": {"url": "https://fill-data.papermc.io/120.jar",
                                                                      "checksums": {"sha256": "aa"}}}},
    {"id": 121, "channel": "ALPHA", "downloads": {"server:default": {"url": "https://fill-data.papermc.io/121.jar",
                                                                      "checksums": {"sha256": "bb"}}}},
]
FABRIC_LOADERS = [{"version": "0.18.1", "stable": False}, {"version": "0.18.0", "stable": True},
                  {"version": "0.17.2", "stable": True}]


def fake_json(url, cache=True):
    if url == J.MOJANG_MANIFEST:
        return MANIFEST
    if url.startswith("u/"):
        return VERSION
    if url == J.PAPER_API:
        return PAPER
    if url.endswith("/builds"):
        return PAPER_BUILDS
    if url.endswith("/versions/loader"):
        return FABRIC_LOADERS
    if url.endswith("/versions/installer"):
        return [{"version": "1.1.0", "stable": True}]
    if url.endswith("/versions/game"):
        return [{"version": "1.21.11", "stable": True}, {"version": "1.21.10", "stable": True}]
    raise AssertionError(url)


class Software(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(J, "_get_json", fake_json)
        p.start()
        self.addCleanup(p.stop)

    def test_snapshots_of_the_current_cycle(self):
        self.assertEqual(J.list_snapshots(), ["26.1-snapshot-2", "26.1-pre1"])
        t = J.resolve("vanilla", "26.1-pre1", snapshot=True)
        self.assertEqual((t["minecraft"], t["java_required"]), ("26.1-pre1", 21))
        self.assertEqual(J.resolve("vanilla", None, snapshot=True)["minecraft"], "26.1-snapshot-2")
        with self.assertRaises(J.JavaServerError):
            J.resolve("vanilla", "25w45a", snapshot=True)     # superseded by 1.21.11
        with self.assertRaises(J.JavaServerError):
            J.resolve("vanilla", "26.1-pre1")                 # not without allowing snapshots
        self.assertEqual(J.resolve("vanilla", "1.21.10", snapshot=True)["minecraft"], "1.21.10")

    def test_no_going_back_from_a_snapshot(self):
        self.assertTrue(J._older("1.21.11", "26.1-pre1"))
        self.assertFalse(J._older("26.1-pre1", "1.21.11"))
        self.assertTrue(J._older("1.21.10", "1.21.11"))

    def test_paper_build_pin(self):
        self.assertEqual(J.resolve("paper", "1.21.11")["build"], "120")          # newest stable
        t = J.resolve("paper", "1.21.11", build="121")
        self.assertEqual((t["build"], t["url"], t["sha256"]), ("121", "https://fill-data.papermc.io/121.jar", "bb"))
        with self.assertRaises(J.JavaServerError):
            J.resolve("paper", "1.21.11", build="999")
        self.assertEqual([b["label"] for b in J.list_builds("paper", "1.21.11")], ["121 (alpha)", "120"])

    def test_fabric_loader_pin(self):
        self.assertEqual(J.resolve("fabric", "1.21.11")["build"], "loader 0.18.0")
        t = J.resolve("fabric", "1.21.11", build="0.17.2")
        self.assertEqual(t["build"], "loader 0.17.2")
        self.assertIn("/1.21.11/0.17.2/1.1.0/server/jar", t["url"])
        with self.assertRaises(J.JavaServerError):
            J.resolve("fabric", "1.21.11", build="0.1.0")
        self.assertEqual(J.list_builds("fabric", "")[0]["label"], "0.18.1 (unstable)")

    def test_exact_java(self):
        with mock.patch.object(J, "installed_javas", lambda: {17: "/j17", 21: "/j21", 25: "/j25"}):
            self.assertEqual(J.choose_exact_java(25, 21), "/j25")
            with self.assertRaises(J.JavaServerError):
                J.choose_exact_java(17, 21)
            with self.assertRaises(J.MissingJavaError):
                J.choose_exact_java(22, 21)


class Deploy(unittest.TestCase):
    """deploy_server() with a plan, against temp folders."""

    def setUp(self):
        self.root = tempfile.mkdtemp(dir=TMP)
        self.servers = os.path.join(self.root, "opt")
        self.units = os.path.join(self.root, "units")
        os.makedirs(self.servers)
        os.makedirs(self.units)
        self.commands = []
        self.installs = []

        def install_java(d, loader="vanilla", version=None, current=None, build=None, snapshot=False, java_major=None):
            self.installs.append({"loader": loader, "version": version, "build": build, "snapshot": snapshot,
                                  "java": java_major})
            os.makedirs(d, exist_ok=True)
            open(os.path.join(d, "server.jar"), "w").close()
            return {"loader": loader, "minecraft": version or "1.21.11"}, "/usr/lib/jvm/java-21/bin/java"
        patches = [
            mock.patch.object(M, "SERVERS_ROOT", self.servers),
            mock.patch.object(M, "_unit_path", lambda n: os.path.join(self.units, f"{n}.service")),
            mock.patch.object(M, "run_and_log", lambda cmd, shell=False: self.commands.append(cmd)),
            mock.patch.object(M, "ensure_minecraft_user", lambda home: None),
            mock.patch.object(M, "open_server_firewall", lambda *a, **k: None),
            mock.patch.object(M, "_install_java", install_java),
            mock.patch.object(M, "_port_bindable", lambda port: port != 25999),
            mock.patch.object(M, "_heap_fits", lambda xmx: None),
            mock.patch.object(M, "_is_java", lambda name: True),
            mock.patch.object(M, "get_server_dir", lambda name: os.path.join(self.servers, name)),
            mock.patch.object(M, "get_java_uuid", lambda name: "11111111-2222-3333-4444-555555555555"),
            mock.patch.object(D, "FLAGS_FILE", os.path.join(self.root, "jvm-flags.json")),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def unit(self, name):
        with open(M._unit_path(name)) as f:
            return next(l for l in f.read().splitlines() if l.startswith("ExecStart="))

    def props(self, name):
        return M.read_properties(os.path.join(self.servers, name))

    def test_without_options_as_before(self):
        name = M.deploy_server("java", loader="paper")
        self.assertEqual(name, "minecraft-java")
        self.assertEqual(self.props(name)["server-port"], "25565")
        self.assertRegex(self.unit(name), r"^ExecStart=/usr/lib/jvm/java-21/bin/java -Xms\S+ -Xmx\S+ -jar server.jar nogui$")

    def test_with_every_option(self):
        name = M.deploy_server("java", loader="paper", version="1.21.11", advanced={
            "name": "survival", "port": 25610, "build": "121", "java": 21, "heap": "3G",
            "flags": {"preset": "aikar", "extra": ["-Dfile.encoding=UTF-8"]}, "seed": "42", "level_type": "flat",
            "level_name": "main", "motd": "Welcome", "gamemode": "creative", "difficulty": "hard", "max_players": 8,
            "online_mode": True, "whitelist_players": "Alex", "ops": "Alex"}, actor="quentin")
        self.assertEqual(name, "minecraft-survival")
        self.assertEqual(self.installs[-1], {"loader": "paper", "version": "1.21.11", "build": "121",
                                             "snapshot": False, "java": 21})
        u = self.unit(name)
        self.assertIn(" -Xms3G -Xmx3G -XX:+UseG1GC ", u)          # Aikar's: the whole heap up front
        self.assertTrue(u.endswith("-Daikars.new.flags=true -Dfile.encoding=UTF-8 -jar server.jar nogui"), u)
        p = self.props(name)
        for k, v in {"server-port": "25610", "level-seed": "42", "level-type": "minecraft:flat",
                     "level-name": "main", "motd": "Welcome", "gamemode": "creative", "difficulty": "hard",
                     "max-players": "8", "online-mode": "true", "white-list": "true"}.items():
            self.assertEqual(p.get(k), v, k)
        d = os.path.join(self.servers, name)
        with open(os.path.join(d, "whitelist.json")) as f:
            self.assertEqual([e["name"] for e in json.load(f)], ["Alex"])
        with open(os.path.join(d, "ops.json")) as f:
            self.assertEqual(json.load(f), [{"uuid": "11111111-2222-3333-4444-555555555555", "name": "Alex",
                                             "level": 4, "bypassesPlayerLimit": False}])
        with open(M.LOG_FILE) as f:
            self.assertIn("quentin created minecraft-survival with advanced options", f.read())

    def test_flags_survive_a_unit_rewrite(self):
        name = M.deploy_server("java", advanced={"flags": {"preset": "none", "extra": ["-XX:+UseZGC"]}, "heap": "2G"})
        self.assertIn("-Xms1G -Xmx2G -XX:+UseZGC -jar", self.unit(name))
        # Update, Memory (1.17), a Java switch or a restore rewrite the unit without passing flags
        M._write_unit(name, os.path.join(self.servers, name), "java", xms="2G", xmx="4G", java="/j25/bin/java")
        self.assertEqual(self.unit(name), "ExecStart=/j25/bin/java -Xms2G -Xmx4G -XX:+UseZGC -jar server.jar nogui")
        # An import under the same name later starts clean
        M._write_unit(name, os.path.join(self.servers, name), "java", jvm={})
        self.assertNotIn("UseZGC", self.unit(name))

    def test_chosen_name_and_port_clashes(self):
        M.deploy_server("java", advanced={"name": "lobby"})
        with self.assertRaisesRegex(M.ServerError, "already a server"):
            M.deploy_server("java", advanced={"name": "lobby"})
        with self.assertRaisesRegex(M.ServerError, "Port 25565"):
            M.deploy_server("java", advanced={"port": 25565})
        with self.assertRaisesRegex(M.ServerError, "listening on port 25999"):
            M.deploy_server("java", advanced={"port": 25999})
        # A chosen name with no port gets the next free one
        M.deploy_server("java", advanced={"name": "hub"})
        self.assertEqual(self.props("minecraft-hub")["server-port"], "25566")
        self.assertFalse(M._busy)                     # every reservation released, failures included

    def test_bad_options_fail_before_downloading(self):
        for adv in ({"heap": "2T"}, {"flags": {"extra": ["-javaagent:x"]}}, {"name": "x y"}):
            with self.assertRaises(M.ServerError):
                M.deploy_server("java", advanced=adv)
        with self.assertRaisesRegex(M.ServerError, "Only admins"):
            M.deploy_server("java", advanced={"flags": {"extra": ["-XX:+UseZGC"]}}, is_admin=False)
        with self.assertRaisesRegex(M.ServerError, "allow snapshots"):
            M.deploy_server("java", version="26.1-pre1")
        with self.assertRaisesRegex(M.ServerError, "version a Paper build"):
            M.deploy_server("java", loader="paper", advanced={"build": "121"})
        self.assertEqual(self.installs, [])
        self.assertEqual(os.listdir(self.servers), [])

    def test_template_then_advanced_wins(self):
        with mock.patch.object(M, "get_checked_template", lambda t, e: {"properties": {"motd": "From template",
                                                                                       "pvp": "false"},
                                                                        "permissions": "[]"}):
            name = M.deploy_server("java", template_id="t1", advanced={"motd": "Chosen"})
        p = self.props(name)
        self.assertEqual((p["motd"], p["pvp"]), ("Chosen", "false"))

    def test_hard_limits_set_through_1_17(self):
        from core import perf
        calls = []
        with mock.patch.object(perf, "set_resources", lambda server, **kw: calls.append((server, kw))):
            name = M.deploy_server("java", advanced={"heap": "2G", "memory_limit": "auto", "cpu": 2}, actor="quentin")
            self.assertEqual(calls, [(name, {"memory": "auto", "cpu": 2.0, "actor": "quentin"})])
            # A size too close to the heap is refused before anything is downloaded
            with self.assertRaisesRegex(M.ServerError, "well above the heap"):
                M.deploy_server("java", advanced={"heap": "2G", "memory_limit": "2G"})
        self.assertEqual(len(self.installs), 1)

    def test_server_flags_on_maintenance(self):
        name = M.deploy_server("java")
        with mock.patch.object(M, "_unit_exec_start", lambda n: self.unit(n)[len("ExecStart="):]), \
                mock.patch.object(M, "_check_server", lambda n: None):
            out = M.set_server_flags(name, {"preset": "aikar"}, actor="quentin")
        self.assertEqual(out["flags"]["preset"], "aikar")
        self.assertIn("-XX:+UseG1GC", self.unit(name))
        self.assertIn("/usr/lib/jvm/java-21/bin/java", self.unit(name))
        with self.assertRaises(M.ServerError):
            with mock.patch.object(M, "_check_server", lambda n: None):
                M.set_server_flags(name, {"extra": ["-XX:OnError=sh"]})


if __name__ == "__main__":
    unittest.main()
