"""Tests for core/crashes.py (1.17.1): known causes, the suspected mod, the record, the webhook line.
The sample texts are trimmed from real logs and crash reports of each kind.
Run: python -m unittest discover -s tests"""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-crashes-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import crashes as C  # noqa: E402
from core import notify as N  # noqa: E402

FABRIC_MISSING = """\
[14:02:11] [main/INFO]: Loading Minecraft 1.21.4 with Fabric Loader 0.16.9
[14:02:11] [main/ERROR]: Incompatible mods found!
net.fabricmc.loader.impl.FormattedException: Some of your mods are incompatible with the game or each other!
A potential solution has been determined, this may resolve your problem:
\t - Install fabric-api, any version.
More details:
\t - Mod 'Sodium Extra' (sodium-extra) 0.6.0+mc1.21.4 requires any version of fabric-api, which is missing!
\tat net.fabricmc.loader.impl.FabricLoaderImpl.load(FabricLoaderImpl.java:202)
"""
FABRIC_NEEDS_MOD = """\
\t - Mod 'Iris' (iris) 1.8.1+mc1.21.4 requires any version of mod 'Sodium' (sodium), which is missing!
"""
FABRIC_WRONG_MC = """\
\t - Mod 'Lithium' (lithium) 0.11.2 requires version 1.20.1 of mod 'Minecraft' (minecraft), but only the wrong version is present: 1.21.4!
"""
WRONG_JAVA = """\
Error: LinkageError occurred while loading main class net.minecraft.bundler.Main
\tjava.lang.UnsupportedClassVersionError: net/minecraft/bundler/Main has been compiled by a more recent version of the Java Runtime (class file version 65.0), this version of the Java Runtime only recognizes class file versions up to 61.0
"""
PORT = """\
[14:10:02] [Server thread/INFO]: Starting Minecraft server on *:25565
[14:10:02] [Server thread/WARN]: **** FAILED TO BIND TO PORT!
[14:10:02] [Server thread/WARN]: The exception was: java.net.BindException: Address already in use
[14:10:02] [Server thread/WARN]: Perhaps a server is already running on that port?
"""
BEDROCK_PORT = "NO LOG FILE! - [2026-11-22 14:11:00 ERROR] Network port occupied, can't start server.\n"
OOM = """\
[15:01:44] [Server thread/ERROR]: Encountered an unexpected exception
java.lang.OutOfMemoryError: Java heap space
"""
EULA = """\
[14:00:00] [ServerMain/INFO]: Loading properties
[14:00:00] [ServerMain/WARN]: Failed to load eula.txt
[14:00:00] [ServerMain/INFO]: You need to agree to the EULA in order to run the server. Go to eula.txt for more info.
"""
CHUNK = """\
[16:20:01] [Server thread/ERROR]: Couldn't load chunk [12, -4]
net.minecraft.nbt.NbtException: Invalid tag id: 87
"""
LEVEL = """\
[16:00:00] [main/ERROR]: Exception reading ./world/level.dat
java.util.zip.ZipException: Not in GZIP format
"""
PAPER_DEP = """\
[14:20:00] [Server thread/ERROR]: [DirectoryProviderSource] Error loading plugin: org.bukkit.plugin.UnknownDependencyException: Unknown/missing dependency plugins: [Vault]. Please download and install these plugins to run 'Essentials'.
"""
PAPER_API = """\
[14:21:00] [Server thread/ERROR]: Could not load 'plugins/FancyNpcs-2.4.jar' in folder 'plugins'
org.bukkit.plugin.InvalidPluginException: Unsupported API version 1.22
"""
CRASH_REPORT = """\
---- Minecraft Crash Report ----
// Who set us up the TNT?

Time: 2026-11-22 14:30:00
Description: Ticking entity

java.lang.NullPointerException: Cannot invoke "Object.hashCode()" because "key" is null
\tat net.minecraft.class_1297.method_5773(class_1297.java:615)
\tat net.minecraft.class_1297.handler$zzf000$sodium$onTick(class_1297.java:9000)

-- Mod loading issue for: --
Suspected Mods: Sodium (sodium), Version: 0.6.5+mc1.21.4
"""

ITEMS = {"kind": "fabric", "noun": "mod", "items": [
    {"file": "sodium-fabric-0.6.5.jar", "id": "sodium", "name": "Sodium", "enabled": True},
    {"file": "sodium-extra-0.6.0.jar", "id": "sodium-extra", "name": "Sodium Extra", "enabled": True},
    {"file": "lithium-0.11.2.jar", "id": "lithium", "name": "Lithium", "enabled": True}]}
PLUGINS = {"kind": "paper", "noun": "plugin", "items": [
    {"file": "EssentialsX-2.20.1.jar", "id": "Essentials", "name": "Essentials", "enabled": True},
    {"file": "FancyNpcs-2.4.jar", "id": "FancyNpcs", "name": "FancyNpcs", "enabled": True}]}


def ids(text, **ctx):
    return [c["id"] for c in C.find_causes(text, ctx)]


class Causes(unittest.TestCase):
    def test_each_known_cause(self):
        self.assertEqual(ids(FABRIC_MISSING), ["missing_dependency"])
        self.assertEqual(ids(WRONG_JAVA), ["wrong_java"])
        self.assertEqual(ids(PORT), ["port_in_use"])
        self.assertEqual(ids(BEDROCK_PORT), ["port_in_use"])
        self.assertEqual(ids(OOM), ["out_of_memory"])
        self.assertEqual(ids(EULA), ["eula"])
        self.assertEqual(ids(CHUNK), ["corrupt_chunk"])
        self.assertEqual(ids(LEVEL), ["corrupt_level"])
        self.assertEqual(ids(PAPER_DEP), ["missing_dependency"])
        self.assertEqual(ids(PAPER_API), ["wrong_version"])
        self.assertEqual(ids("[12:00] [Server thread/INFO]: Done (3.2s)! For help, type \"help\"\n"), [])

    def test_plain_words(self):
        c = C.find_causes(FABRIC_MISSING)[0]
        self.assertEqual(c["text"], "Sodium Extra requires Fabric API, which isn't installed.")
        self.assertIn("Install Fabric API", c["fix"])
        self.assertEqual(C.find_causes(FABRIC_NEEDS_MOD)[0]["text"], "Iris requires Sodium, which isn't installed.")
        self.assertIn("different Minecraft version (this server runs 1.21.4)", C.find_causes(FABRIC_WRONG_MC)[0]["text"])
        j = C.find_causes(WRONG_JAVA)[0]
        self.assertIn("needs Java 21", j["text"])
        self.assertIn("started with Java 17", j["text"])
        self.assertIn("port 25565", C.find_causes(PORT, {"port": "25565"})[0]["text"])
        self.assertEqual(C.find_causes(PAPER_DEP)[0]["text"], "Essentials requires Vault, which isn't installed.")

    def test_oom_kill_from_systemd(self):
        c = C.find_causes("", {"service_result": "oom-kill", "memory_limit": "3G"})
        self.assertEqual(c[0]["id"], "out_of_memory")
        self.assertIn("hard limit is 3G", c[0]["text"])

    def test_suspects(self):
        self.assertEqual([i["file"] for i in C.match_installed(C.suspects_named(CRASH_REPORT), ITEMS["items"])],
                         ["sodium-fabric-0.6.5.jar"])
        self.assertEqual([i["file"] for i in C.match_installed(C.suspects_named(FABRIC_MISSING), ITEMS["items"])],
                         ["sodium-extra-0.6.0.jar"])
        self.assertEqual([i["file"] for i in C.match_installed(C.suspects_named(PAPER_API), PLUGINS["items"])],
                         ["FancyNpcs-2.4.jar"])
        self.assertEqual([i["file"] for i in C.match_installed(C.suspects_named(PAPER_DEP), PLUGINS["items"])],
                         ["EssentialsX-2.20.1.jar"])
        self.assertEqual(C.match_installed(C.suspects_named("Suspected Mods: None\n"), ITEMS["items"]), [])

    def test_excerpt_highlights_error_lines(self):
        lines = C.excerpt(PORT, C.find_causes(PORT))
        self.assertEqual([l["hl"] for l in lines], [False, True, True, False])
        long = "\n".join(f"line {i}" for i in range(1000)) + "\n"
        text = "Exception reading ./world/level.dat\n" + long
        out = C.excerpt(text, C.find_causes(text))
        self.assertTrue(out[0]["hl"] and "level.dat" in out[0]["text"])
        self.assertEqual(out[1]["text"], "…")
        self.assertEqual(len(out), C.EXCERPT_LINES + 2)


class Records(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(dir=TMP)
        self.base = os.path.join(self.root, "minecraft-fab")
        os.makedirs(os.path.join(self.base, "logs"))
        os.makedirs(os.path.join(self.base, "crash-reports"))
        for p in (mock.patch.object(C, "CRASH_DIR", os.path.join(self.root, "crashes")),
                  mock.patch.object(C, "journal", lambda server, env: self.journal),
                  mock.patch.object(C, "_installed", lambda server: ITEMS),
                  mock.patch.object(C, "_last_update", lambda server: self.last_update)):
            p.start()
            self.addCleanup(p.stop)
        self.journal = ""
        self.last_update = None

    def write(self, rel, text, age=0):
        p = os.path.join(self.base, rel)
        with open(p, "w") as f:
            f.write(text)
        t = time.time() - age
        os.utime(p, (t, t))

    def test_crash_report_and_log_together(self):
        self.write("logs/latest.log", "[14:29] [Server thread/INFO]: hello\n")
        self.write("crash-reports/crash-2026-11-22_14.30.00-server.txt", CRASH_REPORT)
        self.write("crash-reports/crash-2026-11-01_10.00.00-server.txt", OOM, age=86400)
        rec = C.record("minecraft-fab", self.base, env={"SERVICE_RESULT": "exit-code", "EXIT_CODE": "exited",
                                                        "EXIT_STATUS": "255"})
        self.assertEqual(rec["source"], "crash-reports/crash-2026-11-22_14.30.00-server.txt")
        self.assertEqual(rec["causes"], [])                  # the old OOM report isn't this crash
        self.assertEqual([s["file"] for s in rec["suspects"]], ["sodium-fabric-0.6.5.jar"])
        self.assertEqual(rec["exit"], "exited 255")
        self.assertEqual(C.summary(rec), "Likely cause: the mod Sodium.")
        self.assertEqual(C.load("minecraft-fab")["server"], "minecraft-fab")
        self.assertEqual(oct(os.stat(os.path.join(self.root, "crashes", "minecraft-fab.json")).st_mode & 0o777), "0o600")

    def test_wrong_java_comes_from_the_journal(self):
        self.write("logs/latest.log", "[old run] [Server thread/INFO]: Stopping server\n", age=7200)
        self.journal = WRONG_JAVA
        rec = C.record("minecraft-fab", self.base, env={"SERVICE_RESULT": "exit-code"})
        self.assertEqual(rec["source"], "journal")
        self.assertEqual(rec["causes"][0]["id"], "wrong_java")
        self.assertTrue(C.summary(rec).startswith("The server (or a mod) needs Java 21"))

    def test_missing_dependency_names_the_mod(self):
        self.write("logs/latest.log", FABRIC_MISSING)
        rec = C.record("minecraft-fab", self.base, env={"SERVICE_RESULT": "exit-code"})
        self.assertEqual(rec["causes"][0]["id"], "missing_dependency")
        self.assertEqual([s["file"] for s in rec["suspects"]], ["sodium-extra-0.6.0.jar"])
        self.assertEqual(C.summary(rec), "Sodium Extra requires Fabric API, which isn't installed.")

    def test_undo_offered_only_for_a_recent_update(self):
        self.write("logs/latest.log", OOM)
        self.last_update = {"at": int(time.time()) - 600, "files": ["lithium-0.11.2.jar"]}
        self.assertEqual(C.record("minecraft-fab", self.base)["undo"]["files"], ["lithium-0.11.2.jar"])
        self.last_update = {"at": int(time.time()) - 30 * 86400, "files": ["lithium-0.11.2.jar"]}
        self.assertIsNone(C.record("minecraft-fab", self.base)["undo"])

    def test_clean_unrequested_stop_without_a_cause_is_not_kept(self):
        self.write("logs/latest.log", "[14:00] [Server thread/INFO]: Stopping server\n")
        self.assertIsNone(C.record("minecraft-fab", self.base, crashed=False))
        self.assertIsNone(C.load("minecraft-fab"))
        self.write("logs/latest.log", EULA)                  # but a failed start is
        rec = C.record("minecraft-fab", self.base, crashed=False)
        self.assertEqual(rec["causes"][0]["id"], "eula")
        self.assertEqual(C.attention(["minecraft-fab"])[0]["text"],
                         "minecraft-fab stopped: The Minecraft EULA hasn't been accepted.")

    def test_attention_dismiss_and_age(self):
        self.write("logs/latest.log", PORT)
        C.record("minecraft-fab", self.base)
        self.assertEqual(len(C.attention(["minecraft-fab", "minecraft-other"])), 1)
        C.dismiss("minecraft-fab")
        self.assertEqual(C.attention(["minecraft-fab"]), [])
        C.record("minecraft-fab", self.base)
        self.assertEqual(C.attention(["minecraft-fab"], now=time.time() + C.SHOW_FOR + 60), [])

    def test_symlinked_log_is_not_followed(self):
        secret = os.path.join(self.root, "secret.txt")
        with open(secret, "w") as f:
            f.write("You need to agree to the EULA\n")
        os.symlink(secret, os.path.join(self.base, "logs", "latest.log"))
        rec = C.record("minecraft-fab", self.base)
        self.assertEqual(rec["causes"], [])
        self.assertNotIn("EULA", json.dumps(rec["lines"]))

    def test_bad_names_refused(self):
        with self.assertRaises(C.CrashError):
            C.dismiss("../../etc/passwd")
        self.assertIsNone(C.load("../../etc/passwd"))

    def test_turn_off_only_the_suspect(self):
        self.write("logs/latest.log", FABRIC_MISSING)
        C.record("minecraft-fab", self.base)
        calls = []
        from core import manager, mods
        with mock.patch.object(mods, "set_enabled", lambda s, f, e, actor: calls.append(("off", f)) or {"file": f + ".disabled"}), \
             mock.patch.object(manager, "control_server", lambda s, a: calls.append((a, s))), \
             mock.patch.object(manager, "write_log", lambda m: None):
            with self.assertRaises(C.CrashError):
                C.turn_off_and_restart("minecraft-fab", "lithium-0.11.2.jar", actor="quentin")
            r = C.turn_off_and_restart("minecraft-fab", "sodium-extra-0.6.0.jar", actor="quentin")
        self.assertEqual(calls, [("off", "sodium-extra-0.6.0.jar"), ("restart", "minecraft-fab")])
        self.assertEqual(r["file"], "sodium-extra-0.6.0.jar.disabled")
        self.assertTrue(C.load("minecraft-fab")["dismissed"])


class Webhook(unittest.TestCase):
    """The crash webhook gains one line with the cause."""
    def setUp(self):
        self.root = tempfile.mkdtemp(dir=TMP)
        self.units = os.path.join(self.root, "units")
        os.makedirs(self.units)
        self.base = os.path.join(self.root, "opt", "minecraft-java")
        os.makedirs(os.path.join(self.base, "logs"))
        with open(os.path.join(self.units, "minecraft-java.service"), "w") as f:
            f.write(f"[Unit]\nDescription=Minecraft Server (minecraft-java)\n[Service]\nWorkingDirectory={self.base}\n")
        self.posts = []
        for p in (mock.patch.object(N, "CONFIG_FILE", os.path.join(self.root, "notifications.json")),
                  mock.patch.object(N, "STATE_FILE", os.path.join(self.root, "notify-state.json")),
                  mock.patch.object(N, "UNIT_DIR", self.units),
                  mock.patch.object(N, "_post", lambda url, body: self.posts.append(body)),
                  mock.patch.object(N, "_log", lambda msg: None),
                  mock.patch.object(N, "stop_requested", lambda server: False),
                  mock.patch.object(N.settings, "display_name", lambda s=None: "Box"),
                  mock.patch.object(C, "CRASH_DIR", os.path.join(self.root, "crashes")),
                  mock.patch.object(C, "journal", lambda server, env: ""),
                  mock.patch.object(C, "_installed", lambda server: None),
                  mock.patch.object(C, "_last_update", lambda server: None)):
            p.start()
            self.addCleanup(p.stop)
        N.save("https://example.org/hook", ["crashed", "stopped"])

    def test_crash_message_has_the_cause(self):
        with open(os.path.join(self.base, "logs", "latest.log"), "w") as f:
            f.write(PORT)
        N.unit_event("stop", "minecraft-java", {"SERVICE_RESULT": "exit-code", "EXIT_CODE": "exited", "EXIT_STATUS": "1"})
        self.assertEqual(self.posts[0]["message"],
                         "minecraft-java crashed (exit status 1). systemd starts it again in 20 seconds. "
                         "Another program (or another server) is already using port.")
        self.assertEqual(C.load("minecraft-java")["causes"][0]["id"], "port_in_use")

    def test_requested_stop_is_not_analysed(self):
        with mock.patch.object(N, "stop_requested", lambda server: True):
            N.unit_event("stop", "minecraft-java", {"SERVICE_RESULT": "success"})
        self.assertEqual(self.posts[0]["message"], "minecraft-java stopped.")
        self.assertIsNone(C.load("minecraft-java"))


SAMPLES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crash_samples")


class RealSamples(unittest.TestCase):
    """Logs and crash reports from real servers (vanilla 1.21.4 and 26.3, Fabric 0.20.1 with Ledger),
    made to fail on purpose with Java 21: see the 1.17.1 As built notes."""
    def cause(self, name, **ctx):
        with open(os.path.join(SAMPLES, name)) as f:
            return [c["id"] for c in C.find_causes(f.read(), ctx)]

    def test_real_failures(self):
        self.assertEqual(self.cause("wrong-java-journal.txt"), ["wrong_java"])
        self.assertEqual(self.cause("eula-latest.log"), ["eula"])
        # The crash report only says "Failed to initialize server"; the log says why
        self.assertEqual(self.cause("port-crash-report.txt"), [])
        self.assertEqual(self.cause("port-latest.log"), ["port_in_use"])
        srcs = []
        for n in ("port-crash-report.txt", "port-latest.log"):
            with open(os.path.join(SAMPLES, n)) as f:
                srcs.append((n, f.read()))
        rec = C.analyze_texts("minecraft-x", srcs, port="25565")
        self.assertEqual(rec["source"], "port-crash-report.txt")
        self.assertEqual(C.summary(rec), "Another program (or another server) is already using port 25565.")
        self.assertEqual(self.cause("level-dat-latest.log"), ["corrupt_level"])
        self.assertEqual(self.cause("oom-journal.txt"), ["out_of_memory"])
        self.assertEqual(self.cause("fabric-missing-dep-latest.log"), ["missing_dependency"])
        with open(os.path.join(SAMPLES, "wrong-java-journal.txt")) as f:
            self.assertIn("needs Java 25 or newer. It was started with Java 21", C.find_causes(f.read())[0]["text"])
        with open(os.path.join(SAMPLES, "fabric-missing-dep-latest.log")) as f:
            text = f.read()
        self.assertEqual(C.find_causes(text)[0]["text"], "Ledger requires Fabric API and Fabric Language Kotlin, which aren't installed.")
        self.assertEqual(C.suspects_named(text)[:1], ["ledger"])


class StopRequested(unittest.TestCase):
    """Only a queued stop or restart job means someone asked; a start job still waiting for
    ExecStartPost doesn't (a server that dies at once, such as with the wrong Java)."""
    def jobs(self, out):
        import subprocess
        res = subprocess.CompletedProcess([], 0, stdout=out, stderr="")
        with mock.patch("subprocess.run", lambda *a, **k: res):
            return N.stop_requested("minecraft-x")

    def test_job_types(self):
        self.assertFalse(self.jobs(""))
        self.assertFalse(self.jobs("393 minecraft-x.service start running\n"))
        self.assertTrue(self.jobs("394 minecraft-x.service stop running\n"))
        self.assertTrue(self.jobs("395 minecraft-x.service restart running\n"))
        self.assertFalse(self.jobs("396 minecraft-xy.service stop running\n"))


if __name__ == "__main__":
    unittest.main()
