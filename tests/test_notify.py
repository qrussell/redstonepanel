"""Tests for core/notify.py: settings, payloads, the crash/stop hook and its rate limit.
Nothing is sent: the HTTP POST is replaced by a recorder.
Run: python -m unittest discover -s tests"""
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-notify-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import notify as N  # noqa: E402

DISCORD = "https://discord.com/api/webhooks/123456/secret-token-abcd"


class Notify(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(dir=TMP)
        self.units = os.path.join(self.root, "units")
        os.makedirs(self.units)
        self.posts = []
        self.fail = None
        self.requested = False
        def post(url, body):
            if self.fail:
                raise self.fail
            self.posts.append((url, body))
        for p in (mock.patch.object(N, "CONFIG_FILE", os.path.join(self.root, "notifications.json")),
                  mock.patch.object(N, "STATE_FILE", os.path.join(self.root, "notify-state.json")),
                  mock.patch.object(N, "UNIT_DIR", self.units),
                  mock.patch.object(N, "_post", post),
                  mock.patch.object(N, "_log", lambda msg: None),
                  mock.patch.object(N, "stop_requested", lambda server: self.requested),
                  mock.patch.object(N.settings, "display_name", lambda s=None: "Survival Box")):
            p.start()
            self.addCleanup(p.stop)

    def unit(self, name="minecraft-java"):
        with open(os.path.join(self.units, f"{name}.service"), "w") as f:
            f.write(f"[Unit]\nDescription=Minecraft Server ({name})\n")

    # --- settings ---
    def test_defaults_and_url_never_shown(self):
        self.assertEqual(N.public()["configured"], False)
        r = N.save(DISCORD, ["crashed", "backup_ok"])
        self.assertTrue(r["configured"])
        self.assertEqual(r["kind"], "discord")
        self.assertNotIn("secret-token", json.dumps(r))
        self.assertTrue(r["url_hint"].endswith("abcd"))
        self.assertEqual(r["events"], ["crashed", "backup_ok"])
        self.assertEqual(oct(os.stat(N.CONFIG_FILE).st_mode & 0o777), "0o600")

    def test_saving_events_keeps_the_url(self):
        N.save(DISCORD, ["crashed"])
        N.save(None, ["stopped"])
        self.assertEqual(N.load()["url"], DISCORD)
        N.save(clear=True)
        self.assertEqual(N.load()["url"], "")

    def test_url_validation(self):
        for bad in ("discord.com/api/webhooks/1", "ftp://x/y", "https://", "https://a b/c", "javascript:alert(1)"):
            with self.assertRaises(N.NotifyError, msg=bad):
                N.save(bad)
        with self.assertRaises(N.NotifyError):
            N.save(None, ["nonsense"])
        self.assertEqual(N.kind("https://hooks.example.com/x"), "generic")
        self.assertEqual(N.kind("https://discord.com/other"), "generic")

    # --- payloads ---
    def test_discord_and_generic_payloads(self):
        p = N.payload(DISCORD, "crashed", "minecraft-java", "minecraft-java crashed.")
        self.assertEqual(p["content"], "**Survival Box**: minecraft-java crashed.")
        self.assertEqual(p["allowed_mentions"], {"parse": []})
        g = N.payload("https://example.com/hook", "crashed", "minecraft-java", "minecraft-java crashed.")
        self.assertEqual((g["event"], g["server"], g["panel"]), ("crashed", "minecraft-java", "Survival Box"))
        self.assertIn("text", g)

    # --- sending ---
    def test_only_chosen_events_are_sent(self):
        N.save(DISCORD, ["backup_failed"])
        self.assertEqual(N.send("backup_ok", "minecraft-java", "ok"), (False, None))
        self.assertEqual(N.send("backup_failed", "minecraft-java", "failed"), (True, None))
        self.assertEqual(len(self.posts), 1)

    def test_no_url_sends_nothing(self):
        self.assertFalse(N.send("crashed", "minecraft-java", "x")[0])
        self.assertEqual(self.posts, [])

    def test_failures_are_reported_not_raised(self):
        N.save(DISCORD, ["crashed"])
        self.fail = urllib.error.HTTPError(DISCORD, 404, "Not Found", {}, None)
        ok, err = N.send_test()
        self.assertFalse(ok)
        self.assertIn("404", err)
        self.assertNotIn("secret-token", err)
        self.fail = urllib.error.URLError("timed out")
        self.assertFalse(N.send("crashed", "minecraft-java", "x")[0])

    def test_test_message_ignores_event_list(self):
        N.save(DISCORD, [])
        self.assertEqual(N.send_test(), (True, None))

    # --- the systemd hook ---
    def test_stop_results(self):
        self.assertEqual(N.describe_stop({"SERVICE_RESULT": "success"})[0], "stopped")
        e, msg = N.describe_stop({"SERVICE_RESULT": "exit-code", "EXIT_CODE": "exited", "EXIT_STATUS": "1"})
        self.assertEqual(e, "crashed")
        self.assertIn("exit status 1", msg)
        e, msg = N.describe_stop({"SERVICE_RESULT": "signal", "EXIT_CODE": "killed", "EXIT_STATUS": "KILL"})
        self.assertIn("SIGKILL", msg)
        self.assertIn("crashed with SIGSEGV",
                      N.describe_stop({"SERVICE_RESULT": "core-dump", "EXIT_CODE": "dumped", "EXIT_STATUS": "SEGV"})[1])

    def test_requested_stop_is_never_a_crash(self):
        # Bedrock's screen exits 1 on every `systemctl stop`
        self.unit("minecraft-bedrock")
        N.save(DISCORD, ["crashed", "stopped"])
        self.requested = True
        N.unit_event("stop", "minecraft-bedrock", {"SERVICE_RESULT": "exit-code", "EXIT_CODE": "exited",
                                                    "EXIT_STATUS": "1"})
        self.assertEqual([b["content"] for _, b in self.posts], ["**Survival Box**: minecraft-bedrock stopped."])

    def bedrock_unit(self, log):
        d = os.path.join(self.root, "opt", "minecraft-bedrock")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "server.log"), "w") as f:
            f.write(log)
        with open(os.path.join(self.units, "minecraft-bedrock.service"), "w") as f:
            f.write(f"[Unit]\nDescription=Minecraft Server (minecraft-bedrock)\n[Service]\n"
                    f"WorkingDirectory={d}\nExecStart=/usr/bin/screen -DmS minecraft-bedrock ./bedrock_server\n")

    def test_bedrock_dying_inside_screen_is_a_crash_and_is_revived(self):
        # screen exits 0 when bedrock_server is killed: systemd says "success" and won't restart it
        self.bedrock_unit("[2026-11-18 06:10:50 INFO] Server started.\n[2026-11-18 06:10:51 INFO] Player connected\n")
        N.save(DISCORD, ["crashed", "stopped"])
        revived = []
        with mock.patch.object(N, "revive", revived.append):
            N.unit_event("stop", "minecraft-bedrock", {"SERVICE_RESULT": "success"})
        self.assertEqual(revived, ["minecraft-bedrock"])
        self.assertIn("minecraft-bedrock crashed", self.posts[-1][1]["content"])

    def test_bedrock_stop_command_stays_stopped(self):
        self.bedrock_unit("[INFO] Server stop requested.\n[INFO] Stopping server...\nQuit correctly\n\n")
        N.save(DISCORD, ["crashed", "stopped"])
        revived = []
        with mock.patch.object(N, "revive", revived.append):
            N.unit_event("stop", "minecraft-bedrock", {"SERVICE_RESULT": "success"})
        self.assertEqual(revived, [])
        self.assertIn("minecraft-bedrock stopped", self.posts[-1][1]["content"])

    def test_requested_bedrock_stop_is_not_revived(self):
        self.bedrock_unit("[INFO] Server started.\n")
        self.requested = True
        revived = []
        with mock.patch.object(N, "revive", revived.append):
            N.unit_event("stop", "minecraft-bedrock", {"SERVICE_RESULT": "exit-code", "EXIT_CODE": "exited",
                                                        "EXIT_STATUS": "1"})
        self.assertEqual(revived, [])

    def test_crash_loop_sends_one_message(self):
        self.unit()
        N.save(DISCORD, ["crashed", "started"])
        crash = {"SERVICE_RESULT": "exit-code", "EXIT_CODE": "exited", "EXIT_STATUS": "1"}
        for _ in range(4):
            N.unit_event("stop", "minecraft-java", crash)
            N.unit_event("start", "minecraft-java")
        events = [b["content"] for _, b in self.posts]
        self.assertEqual(len(events), 2, events)
        self.assertIn("crashed (exit status 1)", events[0])
        self.assertIn("started", events[1])

    def test_hook_ignores_units_that_arent_panel_servers(self):
        N.save(DISCORD, ["crashed", "started", "stopped"])
        with open(os.path.join(self.units, "minecraft-other.service"), "w") as f:
            f.write("[Unit]\nDescription=Something else\n")
        N.unit_event("start", "minecraft-other")
        N.unit_event("start", "minecraft-missing")
        N.unit_event("start", "../etc/passwd")
        self.assertEqual(self.posts, [])


if __name__ == "__main__":
    unittest.main()
