"""Tests for core/updates.py (1.16.12): update alerts and automatic Bedrock updates. Units, server
folders and downloads live in temp folders; commands, webhooks and Mojang's API are stubbed.
Run: python -m unittest discover -s tests"""
import io
import json
import os
import stat
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-updates-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import fcntl  # noqa: F401
    from core import backups as B, javaservers as J, manager as M, notify as N, updates as U  # noqa: E402
except ImportError:          # Windows
    U = None

LINKS = {"result": {"links": [
    {"downloadType": "serverBedrockWindows", "downloadUrl": "https://www.minecraft.net/bedrockdedicatedserver/bin-win/bedrock-server-1.26.10.4.zip"},
    {"downloadType": "serverBedrockLinux", "downloadUrl": "https://www.minecraft.net/bedrockdedicatedserver/bin-linux/bedrock-server-1.26.10.4.zip"},
    {"downloadType": "serverBedrockPreviewLinux", "downloadUrl": "https://www.minecraft.net/bedrockdedicatedserver/bin-linux-preview/bedrock-server-1.26.20.21.zip"},
]}}


def make_zip(path, files, links=()):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in files.items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (0o755 if name == "bedrock_server" else 0o644) << 16
            z.writestr(info, data)
        for name, target in links:
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            z.writestr(info, target)
    return path


def read(path):
    with open(path) as f:
        return f.read()


@unittest.skipIf(U is None, "needs Linux (fcntl)")
class Versions(unittest.TestCase):
    def test_links_api_linux_release_only(self):
        self.assertEqual(U.parse_bedrock_links(LINKS), {
            "version": "1.26.10.4",
            "url": "https://www.minecraft.net/bedrockdedicatedserver/bin-linux/bedrock-server-1.26.10.4.zip"})
        self.assertIsNone(U.parse_bedrock_links({"result": {"links": []}}))
        self.assertIsNone(U.parse_bedrock_links({"nope": 1}))
        self.assertIsNone(U.parse_bedrock_links(None))

    def test_downloads_only_from_minecraft_hosts(self):
        evil = {"result": {"links": [{"downloadType": "serverBedrockLinux",
                                      "downloadUrl": "https://evil.example/bedrock-server-9.9.9.zip"}]}}
        self.assertIsNone(U.parse_bedrock_links(evil))
        for ok in ("https://www.minecraft.net/x/bedrock-server-1.zip", "https://minecraft.net/a",
                   "https://cdn.minecraft-services.net/a"):
            self.assertTrue(U._allowed_url(ok), ok)
        for bad in ("http://www.minecraft.net/a", "https://minecraft.net.evil.example/a",
                    "https://user@evil.example/a", "https://evilminecraft.net/a", "ftp://minecraft.net/a", ""):
            self.assertFalse(U._allowed_url(bad), bad)

    def test_latest_falls_back_to_the_download_page(self):
        page = b'<a href="https://www.minecraft.net/bedrockdedicatedserver/bin-linux/bedrock-server-1.26.10.4.zip">'
        def get(url, accept="application/json"):
            if url == U.BEDROCK_LINKS:
                raise U.UpdateError("net-secondary answered HTTP 503.")
            return page
        with mock.patch.object(U, "_get", get), mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RP_BEDROCK_LINKS", None)
            self.assertEqual(U.bedrock_latest()["version"], "1.26.10.4")

    def test_latest_error_is_readable(self):
        with mock.patch.object(U, "_get", side_effect=U.UpdateError("Couldn't reach x: timed out")):
            with self.assertRaisesRegex(U.UpdateError, "Couldn't find the newest Bedrock server"):
                U.bedrock_latest()

    def _java(self, info, target):
        with mock.patch.object(J, "resolve", lambda loader, mc=None: {"loader": loader, **target}):
            return U.java_update(info)

    def test_java_alerts(self):
        # vanilla: a newer release, never a snapshot (resolve only returns releases)
        self.assertEqual(self._java({"loader": "vanilla", "minecraft": "1.21.4"}, {"minecraft": "1.21.10"})["label"],
                         "Minecraft 1.21.10")
        self.assertIsNone(self._java({"loader": "vanilla", "minecraft": "1.21.10"}, {"minecraft": "1.21.10"}))
        self.assertIsNone(self._java({"loader": "vanilla", "minecraft": "26.1"}, {"minecraft": "1.21.10"}))
        # paper: newer release, or newer build of the same release
        self.assertEqual(self._java({"loader": "paper", "minecraft": "1.21.4", "build": "100"},
                                    {"minecraft": "1.21.5", "build": "3"})["label"], "Paper 1.21.5")
        self.assertEqual(self._java({"loader": "paper", "minecraft": "1.21.4", "build": "100"},
                                    {"minecraft": "1.21.4", "build": "120"})["label"], "Paper 1.21.4 build 120")
        self.assertIsNone(self._java({"loader": "paper", "minecraft": "1.21.4", "build": "120"},
                                     {"minecraft": "1.21.4", "build": "120"}))
        # fabric: newer release, or newer stable loader
        self.assertEqual(self._java({"loader": "fabric", "minecraft": "1.21.4", "build": "loader 0.16.9"},
                                    {"minecraft": "1.21.4", "build": "loader 0.16.14"})["label"],
                         "Fabric loader 0.16.14")
        self.assertIsNone(self._java({"loader": "fabric", "minecraft": "1.21.4", "build": "loader 0.16.14"},
                                     {"minecraft": "1.21.4", "build": "loader 0.16.14"}))
        # unknown version (an imported jar we couldn't read): no alert rather than a wrong one
        self.assertIsNone(self._java({"loader": "vanilla"}, {"minecraft": "1.21.10"}))
        self.assertIsNone(self._java({}, {"minecraft": "1.21.10"}))

    def test_warnings(self):
        steps = U.warning_steps(5)
        self.assertEqual([s for s, _ in steps], [300, 60, 10])
        self.assertIn("5 minutes", steps[0][1])
        self.assertEqual(U.warning_steps(0), [])


@unittest.skipIf(U is None, "needs Linux (fcntl)")
class Files(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(dir=TMP)
        self.d = os.path.join(self.root, "srv")
        os.makedirs(os.path.join(self.d, "worlds", "w"))
        for name, data in {"bedrock_server": "old binary", "server.properties": "server-port=19140\n",
                           "allowlist.json": "[{\"name\": \"alex\"}]", "permissions.json": "[]",
                           "worlds/w/level.dat": "world", "behavior_packs/vanilla/x.json": "old pack"}.items():
            p = os.path.join(self.d, name)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write(data)
        self.zip = make_zip(os.path.join(self.root, "new.zip"), {
            "bedrock_server": "new binary", "server.properties": "server-port=19132\n", "allowlist.json": "[]",
            "permissions.json": "[]", "behavior_packs/vanilla/x.json": "new pack", "behavior_packs/new/y.json": "y",
            "libnew.so": "lib"})

    def test_install_keeps_settings_and_puts_back(self):
        stash = os.path.join(self.root, "old")
        created = U.install_files(self.d, self.zip, stash=stash)
        self.assertEqual(read(os.path.join(self.d, "bedrock_server")), "new binary")
        self.assertTrue(os.stat(os.path.join(self.d, "bedrock_server")).st_mode & 0o100)
        self.assertEqual(read(os.path.join(self.d, "server.properties")), "server-port=19140\n")
        self.assertIn("alex", read(os.path.join(self.d, "allowlist.json")))
        self.assertEqual(read(os.path.join(self.d, "worlds/w/level.dat")), "world")
        self.assertEqual(sorted(created), ["behavior_packs/new/y.json", "libnew.so"])
        U.put_back(self.d, stash, created)
        self.assertEqual(read(os.path.join(self.d, "bedrock_server")), "old binary")
        self.assertEqual(read(os.path.join(self.d, "behavior_packs/vanilla/x.json")), "old pack")
        self.assertFalse(os.path.exists(os.path.join(self.d, "libnew.so")))
        self.assertEqual(read(os.path.join(self.d, "server.properties")), "server-port=19140\n")

    def test_fresh_folder_gets_the_zip_settings(self):
        fresh = os.path.join(self.root, "fresh")
        os.makedirs(fresh)
        U.install_files(fresh, self.zip)
        self.assertEqual(read(os.path.join(fresh, "server.properties")), "server-port=19132\n")

    def test_unsafe_zips_change_nothing(self):
        cases = {
            "traversal": make_zip(os.path.join(self.root, "a.zip"), {"bedrock_server": "x", "../evil": "x"}),
            "absolute": make_zip(os.path.join(self.root, "b.zip"), {"bedrock_server": "x", "/etc/evil": "x"}),
            "link": make_zip(os.path.join(self.root, "c.zip"), {"bedrock_server": "x"}, links=[("l", "/etc")]),
            "no binary": make_zip(os.path.join(self.root, "d.zip"), {"readme.txt": "x"}),
        }
        with open(os.path.join(self.root, "e.zip"), "wb") as f:
            f.write(b"<html>Access denied</html>")
        cases["not a zip"] = os.path.join(self.root, "e.zip")
        for why, path in cases.items():
            with self.assertRaises(U.UpdateError, msg=why):
                U.install_files(self.d, path)
            self.assertEqual(read(os.path.join(self.d, "bedrock_server")), "old binary", why)

    def test_symlink_in_the_folder_is_refused_before_any_change(self):
        os.rename(os.path.join(self.d, "behavior_packs"), os.path.join(self.root, "elsewhere"))
        os.symlink(os.path.join(self.root, "elsewhere"), os.path.join(self.d, "behavior_packs"))
        with self.assertRaisesRegex(U.UpdateError, "link"):
            U.install_files(self.d, self.zip)
        self.assertEqual(read(os.path.join(self.d, "bedrock_server")), "old binary")
        self.assertEqual(read(os.path.join(self.root, "elsewhere/vanilla/x.json")), "old pack")


@unittest.skipIf(U is None, "needs Linux (fcntl)")
class Updating(unittest.TestCase):
    """The check, the webhook alerts, and run_update() end to end with systemd stubbed."""
    def setUp(self):
        self.root = tempfile.mkdtemp(dir=TMP)
        self.units = os.path.join(self.root, "units")
        os.makedirs(self.units)
        self.commands, self.sent, self.said, self.restored = [], [], [], []
        self.active = {"minecraft-bedrock": "active"}
        self.starts_ok = True
        self.latest = {"version": "1.26.10.4", "url": "https://www.minecraft.net/x/bedrock-server-1.26.10.4.zip"}
        self.d = os.path.join(self.root, "opt", "minecraft-bedrock")
        os.makedirs(os.path.join(self.d, "worlds", "w"))
        with open(os.path.join(self.d, "bedrock_server"), "w") as f:
            f.write("old binary")
        with open(os.path.join(self.d, "server.properties"), "w") as f:
            f.write("server-port=19140\n")
        with open(os.path.join(self.d, U.VERSION_FILE), "w") as f:
            json.dump({"version": "1.26.0.2"}, f)
        self.write_unit("minecraft-bedrock", self.d, bedrock=True)
        self.zip_src = make_zip(os.path.join(self.root, "src.zip"),
                                {"bedrock_server": "new binary", "server.properties": "x=1\n", "libnew.so": "lib"})

        def download(latest, dest):
            p = os.path.join(dest, "bedrock-server.zip")
            with open(self.zip_src, "rb") as a, open(p, "wb") as b:
                b.write(a.read())
            return p

        def run_and_log(cmd, shell=False):
            self.commands.append(cmd)
            if cmd[:2] == ["systemctl", "stop"]:
                self.active[cmd[2][:-8]] = "inactive"
            if cmd[:2] == ["systemctl", "start"]:
                self.active[cmd[2][:-8]] = "active"
            return 0

        def backup(server, kind="manual"):
            self.commands.append(["backup", server, kind])
            return {"file": f"{server}-20261121-000000-{kind}.tar.gz"}

        def restore(server, filename):
            self.restored.append(filename)
            return {}

        def run(cmd, **kw):
            self.commands.append(cmd)
            if cmd[:2] == ["systemctl", "stop"]:
                self.active[cmd[2][:-8]] = "inactive"
            return mock.Mock(returncode=0, stdout="", stderr="")

        for p in (mock.patch.object(U, "UNIT_DIR", self.units),
                  mock.patch.object(U, "STATE_FILE", os.path.join(self.root, "updates.json")),
                  mock.patch.object(U, "AUTO_FILE", os.path.join(self.root, "auto.json")),
                  mock.patch.object(U, "bedrock_latest", lambda: dict(self.latest)),
                  mock.patch.object(U, "download_bedrock", download),
                  mock.patch.object(U, "_active", lambda s: self.active.get(s, "inactive")),
                  mock.patch.object(U, "_say", lambda s, m: self.said.append(m) or True),
                  mock.patch.object(U, "wait_started", lambda *a, **k: self.starts_ok),
                  mock.patch.object(U, "next_check", lambda: None),
                  mock.patch.object(U, "ensure_timer", lambda: True),
                  mock.patch.object(U.subprocess, "run", run),
                  mock.patch.object(M, "run_and_log", run_and_log),
                  mock.patch.object(M, "bedrock_supported", lambda machine=None: True),
                  mock.patch.object(B, "run_backup", backup),
                  mock.patch.object(B, "restore_backup", restore),
                  mock.patch.object(B, "list_backups", lambda s: []),
                  mock.patch.object(B, "_screen", self.screen),
                  mock.patch.object(N, "send", lambda e, s, m, test=False: self.sent.append((e, s, m)))):
            p.start()
            self.addCleanup(p.stop)

    def screen(self, server, cmd):
        self.said.append(cmd)
        if cmd == "stop":
            self.active[server] = "inactive"
        return True

    def write_unit(self, name, d, bedrock):
        start = f"/usr/bin/screen -DmS {name} ./bedrock_server" if bedrock else "/usr/bin/java -jar server.jar nogui"
        with open(os.path.join(self.units, f"{name}.service"), "w") as f:
            f.write(f"[Unit]\nDescription=Minecraft Server ({name})\n[Service]\nWorkingDirectory={d}\nExecStart={start}\n")

    def events(self, kind):
        return [s for s in self.sent if s[0] == kind]

    # --- alerts ---
    def test_check_alerts_once_per_version(self):
        state = U.check()
        self.assertEqual(state["servers"]["minecraft-bedrock"]["label"], "Bedrock 1.26.10.4")
        self.assertEqual(state["servers"]["minecraft-bedrock"]["current"], "1.26.0.2")
        self.assertEqual(len(self.events("update_available")), 1)
        U.check()
        self.assertEqual(len(self.events("update_available")), 1, "never repeated")
        self.latest = {"version": "1.26.11.1", "url": "https://www.minecraft.net/x/bedrock-server-1.26.11.1.zip"}
        U.check()
        self.assertEqual(len(self.events("update_available")), 2, "one message per new version")
        self.assertEqual(U.server_update("minecraft-bedrock", self.d, False)["latest"], "1.26.11.1")

    def test_no_alert_when_current_or_on_arm64(self):
        self.latest["version"] = "1.26.0.2"
        self.assertNotIn("minecraft-bedrock", U.check()["servers"])
        self.latest["version"] = "1.26.10.4"
        with mock.patch.object(M, "bedrock_supported", lambda machine=None: False):
            self.assertNotIn("minecraft-bedrock", U.check()["servers"])
        self.assertEqual(self.events("update_available"), [])

    def test_alert_goes_stale_when_the_server_changes_version(self):
        U.check()
        self.assertIsNotNone(U.server_update("minecraft-bedrock", self.d, False))
        with open(os.path.join(self.d, U.VERSION_FILE), "w") as f:
            json.dump({"version": "1.26.10.4"}, f)
        self.assertIsNone(U.server_update("minecraft-bedrock", self.d, False))

    def test_java_servers_are_checked_too(self):
        jd = os.path.join(self.root, "opt", "minecraft-java")
        os.makedirs(jd)
        with open(os.path.join(jd, J.INFO_FILE), "w") as f:
            json.dump({"loader": "paper", "minecraft": "1.21.4", "build": "100"}, f)
        self.write_unit("minecraft-java", jd, bedrock=False)
        with mock.patch.object(J, "resolve", lambda loader, mc=None: {"loader": loader, "minecraft": "1.21.4", "build": "130"}):
            state = U.check()
        self.assertEqual(state["servers"]["minecraft-java"]["label"], "Paper 1.21.4 build 130")

    def test_a_failed_lookup_is_kept_per_server(self):
        with mock.patch.object(U, "bedrock_latest", side_effect=U.UpdateError("Couldn't reach Mojang")):
            state = U.check()
        self.assertIn("Couldn't reach", state["errors"]["minecraft-bedrock"]["error"])
        self.assertEqual(self.events("update_available"), [])

    def test_removed_servers_are_forgotten(self):
        U.check()
        U.set_auto("minecraft-bedrock", True)
        U.on_server_removed("minecraft-bedrock", purged=True)
        self.assertFalse(U.auto_enabled("minecraft-bedrock"))
        self.assertNotIn("minecraft-bedrock", U.load_state()["servers"])

    # --- setting ---
    def test_auto_update_is_bedrock_only(self):
        jd = os.path.join(self.root, "opt", "minecraft-java")
        os.makedirs(jd)
        self.write_unit("minecraft-java", jd, bedrock=False)
        with self.assertRaisesRegex(U.UpdateError, "Only Bedrock"):
            U.set_auto("minecraft-java", True)
        U.set_auto("minecraft-java", False)
        self.assertTrue(U.set_auto("minecraft-bedrock", True)["auto"])
        with mock.patch.object(M, "bedrock_supported", lambda machine=None: False):
            with self.assertRaises(U.UpdateError):
                U.set_auto("minecraft-bedrock", True)
        with self.assertRaises(U.UpdateError):
            U.set_auto("../etc", True)

    # --- updating ---
    def test_update_warns_backs_up_swaps_and_starts(self):
        slept = []
        r = U.run_update("minecraft-bedrock", warn=5, actor="test", sleep=slept.append)
        self.assertTrue(r["ok"])
        self.assertEqual((r["from"], r["to"]), ("1.26.0.2", "1.26.10.4"))
        self.assertEqual([m for m in self.said if m.startswith("say ")], [], "warnings go through _say")
        self.assertEqual(len([m for m in self.said if not m.startswith("say ") and m != "stop"]), 3)
        self.assertIn("stop", self.said, "Bedrock's own stop first, so it saves the world")
        self.assertEqual(sum(slept), 300, "five minutes of warnings")
        order = [c[:2] if c[0] != "backup" else c for c in self.commands if c[0] in ("systemctl", "backup")]
        self.assertEqual(order[:3], [["systemctl", "stop"], ["backup", "minecraft-bedrock", "pre-update"],
                                     ["systemctl", "start"]])
        self.assertEqual(read(os.path.join(self.d, "bedrock_server")), "new binary")
        self.assertEqual(read(os.path.join(self.d, "server.properties")), "server-port=19140\n")
        self.assertEqual(U.bedrock_installed(self.d), "1.26.10.4")
        self.assertEqual(len(self.events("updated")), 1)
        self.assertEqual(U.attention(), [])
        self.assertFalse([n for n in os.listdir(self.d) if n.startswith(".redstonepanel-update-")], "staging removed")

    def test_already_newest_is_skipped(self):
        self.latest["version"] = "1.26.0.2"
        r = U.run_update("minecraft-bedrock", warn=5)
        self.assertTrue(r.get("skipped"))
        self.assertEqual(self.commands, [])
        self.assertEqual(self.said, [])

    def test_failed_download_changes_nothing(self):
        with mock.patch.object(U, "download_bedrock", side_effect=U.UpdateError("Downloading Bedrock failed: HTTP 403")):
            with self.assertRaisesRegex(U.UpdateError, "403"):
                U.run_update("minecraft-bedrock", warn=5)
        self.assertEqual(self.said, [], "players weren't warned")
        self.assertFalse(any(c[:2] == ["systemctl", "stop"] for c in self.commands))
        self.assertEqual(read(os.path.join(self.d, "bedrock_server")), "old binary")
        self.assertEqual(len(self.events("update_failed")), 1)
        self.assertIn("403", U.attention()[0]["text"])

    def test_failed_backup_changes_nothing_and_restarts(self):
        with mock.patch.object(B, "run_backup", side_effect=B.BackupError("disk full")):
            with self.assertRaisesRegex(U.UpdateError, "backup"):
                U.run_update("minecraft-bedrock", warn=0)
        self.assertEqual(read(os.path.join(self.d, "bedrock_server")), "old binary")
        self.assertEqual(self.active["minecraft-bedrock"], "active", "started again")

    def test_new_version_that_wont_start_is_rolled_back(self):
        self.starts_ok = "stopped"
        with self.assertRaisesRegex(U.UpdateError, "stopped while starting"):
            U.run_update("minecraft-bedrock", warn=0)
        self.assertEqual(read(os.path.join(self.d, "bedrock_server")), "old binary")
        self.assertFalse(os.path.exists(os.path.join(self.d, "libnew.so")))
        self.assertEqual(U.bedrock_installed(self.d), "1.26.0.2")
        self.assertEqual(self.restored, ["minecraft-bedrock-20261121-000000-pre-update.tar.gz"], "world put back")
        self.assertEqual(len(self.events("update_failed")), 1)
        items = U.attention()
        self.assertEqual(items[0]["level"], "error")
        self.assertEqual(items[0]["server"], "minecraft-bedrock")
        # the next successful update clears it
        self.starts_ok = True
        U.run_update("minecraft-bedrock", warn=0)
        self.assertEqual(U.attention(), [])

    def test_stopped_server_stays_stopped(self):
        self.active["minecraft-bedrock"] = "inactive"
        r = U.run_update("minecraft-bedrock", warn=5)
        self.assertTrue(r["ok"])
        self.assertEqual(self.said, [])
        self.assertFalse(any(c[:2] == ["systemctl", "start"] for c in self.commands))

    def test_timer_updates_only_opted_in_bedrock_servers(self):
        done = U.run_timer(sleep=lambda s: None)
        self.assertEqual(done, {}, "off by default: alert only")
        self.assertEqual(read(os.path.join(self.d, "bedrock_server")), "old binary")
        U.set_auto("minecraft-bedrock", True)
        done = U.run_timer(sleep=lambda s: None)
        self.assertTrue(done["minecraft-bedrock"]["ok"])
        self.assertEqual(read(os.path.join(self.d, "bedrock_server")), "new binary")
        self.assertNotIn("minecraft-bedrock", U.load_state()["servers"], "alert cleared")

    def test_java_refused(self):
        jd = os.path.join(self.root, "opt", "minecraft-java")
        os.makedirs(jd)
        self.write_unit("minecraft-java", jd, bedrock=False)
        with self.assertRaises(U.UpdateError):
            U.run_update("minecraft-java")

    def test_timer_unit(self):
        self.assertIn("ExecStart=/usr/sbin/redstonepanel check-updates", U._service_unit())
        self.assertIn("Persistent=true", U._timer_unit())


@unittest.skipIf(U is None, "needs Linux (fcntl)")
class Wiring(unittest.TestCase):
    def test_backup_kind_and_events(self):
        self.assertIn("pre-update", B.KINDS)
        self.assertTrue(B.FILE_RE.match("minecraft-b-20261121-000000-pre-update.tar.gz"))
        for e in ("update_available", "updated", "update_failed"):
            self.assertIn(e, N.EVENTS)

    def test_packaged(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.assertIn("core/updates.py", read(os.path.join(root, "debian", "redstonepanel.install")))
        self.assertIn("check-updates", read(os.path.join(root, "packaging", "redstonepanel.8")))

    def test_routes_reach_nodes(self):
        try:
            import main  # noqa: F401
        except ImportError:
            self.skipTest("needs fastapi")
        from core import cluster as C
        for m, p in (("GET", "/api/servers/minecraft-b/updates"), ("PUT", "/api/servers/minecraft-b/updates"),
                     ("POST", "/api/servers/minecraft-b/updates/check")):
            self.assertTrue(C.path_allowed(m, p), f"{m} {p}")


if __name__ == "__main__":
    unittest.main()
