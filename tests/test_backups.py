"""Tests for core/backups.py. Linux only (fcntl); no root or systemctl needed: units, server
folders and backups live in temp folders and commands are recorded instead of run.
Run: python -m unittest discover -s tests"""
import io
import os
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-backups-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import fcntl  # noqa: F401
    from core import backups as B, manager as M  # noqa: E402
except ImportError:          # Windows
    B = None


def write(path, content="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


@unittest.skipIf(B is None, "needs Linux (fcntl)")
class Backups(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(dir=TMP)
        self.units = os.path.join(self.root, "units")
        self.servers = os.path.join(self.root, "opt")
        os.makedirs(self.units)
        self.commands = []
        self.running = False
        self.edition = "java"
        p = [
            mock.patch.object(B, "BACKUP_ROOT", os.path.join(self.root, "backups")),
            mock.patch.object(B, "UNIT_DIR", self.units),
            mock.patch.object(B, "SCHEDULE_FILE", os.path.join(self.root, "data", "backups.json")),
            mock.patch.object(B, "STATUS_DIR", os.path.join(self.root, "data", "status")),
            mock.patch.object(B, "_is_active", lambda s: self.running),
            mock.patch.object(B, "_edition", lambda s, d: self.edition),
            mock.patch.object(M, "get_server_dir", lambda s: os.path.join(self.servers, s)),
            mock.patch.object(M, "run_and_log", lambda cmd, shell=False: self.commands.append(cmd)),
            mock.patch.object(B.subprocess, "run", lambda cmd, **kw: self.commands.append(cmd) or
                              mock.Mock(returncode=0, stdout="", stderr="")),
        ]
        for x in p:
            x.start()
            self.addCleanup(x.stop)

    def java_server(self, name="minecraft-java", level="world"):
        d = os.path.join(self.servers, name)
        write(os.path.join(d, "server.properties"), f"level-name={level}\nserver-port=25565\n")
        write(os.path.join(d, "server.jar"), "jar" * 100)
        write(os.path.join(d, "logs", "latest.log"), "log")
        write(os.path.join(d, level, "level.dat"), "old-world")
        write(os.path.join(d, level, "region", "r.0.0.mca"), "chunks")
        write(os.path.join(d, "ops.json"), "[]")
        write(os.path.join(self.units, f"{name}.service"), "")
        return d

    def names(self, path):
        with tarfile.open(path) as t:
            return sorted(t.getnames())

    def backup_file(self, server, info):
        return os.path.join(B.server_backup_dir(server), info["file"])

    # --- taking backups ---
    def test_stopped_server_backup_skips_binaries_and_logs(self):
        self.java_server()
        info = B.run_backup("minecraft-java", "manual")
        names = self.names(self.backup_file("minecraft-java", info))
        self.assertIn("world/level.dat", names)
        self.assertIn("world/region/r.0.0.mca", names)
        self.assertIn("server.properties", names)
        self.assertIn("ops.json", names)
        self.assertNotIn("server.jar", names)
        self.assertFalse(any(n.startswith("logs") for n in names))
        self.assertTrue(info["consistent"])
        self.assertEqual(oct(os.stat(self.backup_file("minecraft-java", info)).st_mode & 0o777), "0o600")
        self.assertTrue(B.read_status("minecraft-java")["ok"])

    def test_running_java_pauses_saving_over_rcon(self):
        self.java_server()
        self.running = True
        sent = []
        with mock.patch.object(B, "_java_rcon", lambda d: lambda cmd, timeout=10: sent.append(cmd)):
            info = B.run_backup("minecraft-java", "manual")
        self.assertEqual(sent, ["save-off", "save-all flush", "save-on"])
        self.assertTrue(info["consistent"])

    def test_running_java_without_rcon_is_marked_live(self):
        self.java_server()
        self.running = True
        info = B.run_backup("minecraft-java", "manual")
        self.assertFalse(info["consistent"])
        self.assertFalse(B.read_status("minecraft-java")["consistent"])

    def test_running_bedrock_copies_only_held_bytes(self):
        self.edition = "bedrock"
        d = os.path.join(self.servers, "minecraft-bedrock")
        write(os.path.join(d, "server.properties"), "level-name=Bedrock level\n")
        write(os.path.join(d, "bedrock_server"), "bin")
        write(os.path.join(d, "worlds", "Bedrock level", "db", "000005.ldb"), "A" * 100)
        write(os.path.join(d, "worlds", "Bedrock level", "db", "LOCK"), "")
        write(os.path.join(d, "behavior_packs", "vanilla_1.21", "manifest.json"), "{}")
        write(os.path.join(d, "behavior_packs", "my_pack", "manifest.json"), "{}")
        write(os.path.join(self.units, "minecraft-bedrock.service"), "")
        self.running = True
        screen = []
        with mock.patch.object(B, "_bedrock_hold", lambda s, d: [("Bedrock level/db/000005.ldb", 40)]), \
             mock.patch.object(B, "_screen", lambda s, cmd: screen.append(cmd) or True):
            info = B.run_backup("minecraft-bedrock", "manual")
        path = self.backup_file("minecraft-bedrock", info)
        with tarfile.open(path) as t:
            data = t.extractfile("worlds/Bedrock level/db/000005.ldb").read()
            names = t.getnames()
        self.assertEqual(data, b"A" * 40)
        self.assertNotIn("worlds/Bedrock level/db/LOCK", names)       # not in the held list
        self.assertNotIn("bedrock_server", names)
        self.assertNotIn("behavior_packs/vanilla_1.21/manifest.json", names)
        self.assertIn("behavior_packs/my_pack/manifest.json", names)
        self.assertEqual(screen, ["save resume"])
        self.assertTrue(info["consistent"])

    def bedrock_without_level_name(self):
        """What the panel's deploy makes on Bedrock 1.26: no level-name line, world in worlds/level."""
        self.edition = "bedrock"
        d = os.path.join(self.servers, "minecraft-bedrock-2")
        write(os.path.join(d, "server.properties"), "server-port=19134\nserver-portv6=19135\n")
        write(os.path.join(d, "bedrock_server"), "bin")
        write(os.path.join(d, "worlds", "level", "db", "000003.log"), "B" * 200)
        write(os.path.join(d, "worlds", "level", "db", "CURRENT"), "MANIFEST-000002\n")
        write(os.path.join(d, "worlds", "level", "level.dat"), "old-world")
        write(os.path.join(self.units, "minecraft-bedrock-2.service"), "")
        return d

    def test_running_bedrock_without_level_name(self):
        d = self.bedrock_without_level_name()
        self.running = True
        held = [("level/db/000003.log", 156), ("level/db/CURRENT", 16), ("level/level.dat", 9)]
        with mock.patch.object(B, "_bedrock_hold", lambda s, d: held), \
             mock.patch.object(B, "_screen", lambda s, cmd: True):
            info = B.run_backup("minecraft-bedrock-2", "manual")
        with tarfile.open(self.backup_file("minecraft-bedrock-2", info)) as t:
            names = t.getnames()
            self.assertEqual(len(t.extractfile("worlds/level/db/000003.log").read()), 156)
        # Each world file once: the live copy of the folder is left out
        world = [n for n in names if n.startswith("worlds/level/")]
        self.assertEqual(sorted(world), ["worlds/level/db/000003.log", "worlds/level/db/CURRENT",
                                         "worlds/level/level.dat"])

        # And it restores
        write(os.path.join(d, "worlds", "level", "level.dat"), "griefed")
        self.running = False
        result = B.restore_backup("minecraft-bedrock-2", info["file"])
        self.assertEqual(result["restored"], ["worlds/level"])
        with open(os.path.join(d, "worlds", "level", "level.dat")) as f:
            self.assertEqual(f.read(), "old-world")
        self.assertEqual(M.read_properties(d).get("level-name"), "level")

    def test_bedrock_file_list_from_a_real_server(self):
        # server.log of a 1.26.63.1 server made by the panel (no level-name line)
        log = ("[2026-11-18 04:53:12:092 INFO] Saving...\n"
               "[2026-11-18 04:53:12:092 ERROR] A previous save has not been completed.\n"
               "save query\n"
               "[2026-11-18 04:53:16:613 INFO] Data saved. Files are now ready to be copied.\n"
               "level/db/MANIFEST-000002:50, level/db/CURRENT:16, level/db/000003.log:156, "
               "level/level.dat:3031, level/level.dat_old:3031, level/levelname.txt:5\n")
        self.assertEqual(B.parse_bedrock_file_list(log)[0], ("level/db/MANIFEST-000002", 50))
        self.assertEqual(len(B.parse_bedrock_file_list(log)), 6)

    def test_bedrock_file_list_parsing(self):
        log = ("[2026-11-18 04:00:00:000 INFO] A previous save has not been completed.\n"
               "[2026-11-18 04:00:01:000 INFO] Data saved. Files are now ready to be copied.\n"
               "Bedrock level/db/000005.ldb:1234, Bedrock level/db/CURRENT:16, Bedrock level/level.dat:2000\n")
        self.assertEqual(B.parse_bedrock_file_list(log), [
            ("Bedrock level/db/000005.ldb", 1234), ("Bedrock level/db/CURRENT", 16), ("Bedrock level/level.dat", 2000)])
        self.assertIsNone(B.parse_bedrock_file_list("Data saved. Files are now ready to be copied.\n"))
        self.assertIsNone(B.parse_bedrock_file_list("nothing yet"))
        self.assertIsNone(B.parse_bedrock_file_list(
            "Data saved. Files are now ready to be copied.\n../../etc/passwd:10\n"))

    def test_one_backup_at_a_time(self):
        self.java_server()
        fd = B._lock("minecraft-java")
        try:
            with self.assertRaisesRegex(B.BackupError, "already running"):
                B.run_backup("minecraft-java", "manual")
        finally:
            os.close(fd)
        self.assertFalse(B.read_status("minecraft-java")["ok"])

    def test_unknown_server_refused(self):
        with self.assertRaises(B.BackupError):
            B.run_backup("minecraft-nope", "manual")
        with self.assertRaises(B.BackupError):
            B.run_backup("../etc", "manual")

    # --- listing, retention, paths ---
    def make_files(self, server, specs):
        d = B.server_backup_dir(server)
        os.makedirs(d, exist_ok=True)
        for stamp, kind in specs:
            write(os.path.join(d, f"{server}-{stamp}-{kind}.tar.gz"), "")

    def test_prune_keeps_newest_scheduled_and_all_manual(self):
        self.make_files("minecraft-java", [("20261101-040000", "scheduled"), ("20261102-040000", "scheduled"),
                                           ("20261103-040000", "scheduled"), ("20261101-120000", "manual"),
                                           ("20261101-130000", "pre-restore")])
        B.prune("minecraft-java", 2)
        left = [(b["file"].split("-", 2)[2]) for b in B.list_backups("minecraft-java")]
        self.assertEqual(left, ["20261103-040000-scheduled.tar.gz", "20261102-040000-scheduled.tar.gz",
                                "20261101-130000-pre-restore.tar.gz", "20261101-120000-manual.tar.gz"])

    def test_backup_path_only_for_own_files(self):
        self.make_files("minecraft-java", [("20261101-040000", "manual")])
        self.make_files("minecraft-java-2", [("20261101-040000", "manual")])
        self.assertTrue(B.backup_path("minecraft-java", "minecraft-java-20261101-040000-manual.tar.gz"))
        for bad in ("minecraft-java-2-20261101-040000-manual.tar.gz", "../minecraft-java-2/x.tar.gz",
                    "minecraft-java-20261101-040000-manual.tar.gz/../../x", ".lock"):
            with self.assertRaises(B.BackupError):
                B.backup_path("minecraft-java", bad)

    # --- restore ---
    def test_restore_replaces_world_and_keeps_the_old_one(self):
        d = self.java_server()
        good = B.run_backup("minecraft-java", "manual")
        # The world changes after the backup, and gets a new file
        write(os.path.join(d, "world", "level.dat"), "broken-world")
        write(os.path.join(d, "world", "junk.dat"), "junk")
        self.running = True
        result = B.restore_backup("minecraft-java", good["file"])
        with open(os.path.join(d, "world", "level.dat")) as f:
            self.assertEqual(f.read(), "old-world")
        self.assertFalse(os.path.exists(os.path.join(d, "world", "junk.dat")))
        self.assertEqual(result["restored"], ["world"])
        with tarfile.open(os.path.join(B.server_backup_dir("minecraft-java"), result["pre_restore"])) as t:
            self.assertEqual(t.extractfile("world/level.dat").read(), b"broken-world")
        self.assertEqual(self.commands[0], ["systemctl", "stop", "minecraft-java.service"])
        self.assertEqual(self.commands[-1], ["systemctl", "start", "minecraft-java.service"])
        self.assertFalse(any(n.startswith(".redstonepanel-") for n in os.listdir(d)))

    def test_restore_uses_the_backups_world_name(self):
        d = self.java_server(level="survival")
        good = B.run_backup("minecraft-java", "manual")
        M.write_properties(d, {"level-name": "creative"})
        B.restore_backup("minecraft-java", good["file"])
        self.assertEqual(M.read_properties(d)["level-name"], "survival")
        self.assertTrue(os.path.exists(os.path.join(d, "survival", "level.dat")))

    def test_restore_refuses_a_backup_without_a_world(self):
        self.java_server()
        d = B.server_backup_dir("minecraft-java")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "minecraft-java-20261101-040000-manual.tar.gz")
        with tarfile.open(path, "w:gz") as t:
            data = b"level-name=world\n"
            ti = tarfile.TarInfo("server.properties")
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))
        with self.assertRaisesRegex(B.BackupError, "no world"):
            B.restore_backup("minecraft-java", os.path.basename(path))

    def test_restore_refuses_a_world_name_outside_the_server_folder(self):
        # A backup copy can come from another member or off-site storage: its own level-name
        # must not point the world swap at /etc or above the server's folder
        self.java_server()
        d = B.server_backup_dir("minecraft-java")
        os.makedirs(d, exist_ok=True)
        victim = os.path.join(TMP, "victim")
        os.makedirs(victim, exist_ok=True)
        for n, level in enumerate((victim, "../victim", "world/../../victim")):
            path = os.path.join(d, f"minecraft-java-2026110{n + 1}-040000-manual.tar.gz")
            with tarfile.open(path, "w:gz") as t:
                for name, data in (("server.properties", f"level-name={level}\n".encode()),
                                   (level + "/level.dat", b"evil")):
                    ti = tarfile.TarInfo(name)
                    ti.size = len(data)
                    t.addfile(ti, io.BytesIO(data))
            with self.assertRaisesRegex(B.BackupError, "no world", msg=level):
                B.restore_backup("minecraft-java", os.path.basename(path))
            self.assertTrue(os.path.isdir(victim), level)

    # --- schedules ---
    def test_on_calendar(self):
        self.assertEqual(B.on_calendar("daily", "04:30"), "*-*-* 04:30:00")
        self.assertEqual(B.on_calendar("hourly", "13:15"), "*-*-* *:15:00")
        self.assertEqual(B.on_calendar("6h", "15:00"), "*-*-* 03/6:00:00")
        self.assertEqual(B.on_calendar("12h", "15:00"), "*-*-* 03/12:00:00")
        self.assertEqual(B.on_calendar("weekly", "02:00"), "Sun *-*-* 02:00:00")
        for f, t in (("daily", "24:00"), ("daily", "4:00"), ("monthly", "04:00"), ("daily", "04:00\n")):
            with self.assertRaises(B.BackupError):
                B.on_calendar(f, t)

    def test_schedule_writes_and_removes_the_timer(self):
        B.set_schedule("minecraft-java", True, "daily", "03:15", 5)
        timer = os.path.join(self.units, "redstonepanel-backup@minecraft-java.timer")
        with open(timer) as f:
            self.assertIn("OnCalendar=*-*-* 03:15:00", f.read())
        with open(os.path.join(self.units, "redstonepanel-backup@.service")) as f:
            self.assertIn("ExecStart=/usr/sbin/redstonepanel backup %i", f.read())
        self.assertIn(["systemctl", "enable", "redstonepanel-backup@minecraft-java.timer"], self.commands)
        self.assertEqual(B.get_schedule("minecraft-java")["keep"], 5)

        B.set_schedule("minecraft-java", False, "daily", "03:15", 5)
        self.assertFalse(os.path.exists(timer))

    def test_schedule_validation(self):
        for keep in (0, 366, "x"):
            with self.assertRaises(B.BackupError):
                B.set_schedule("minecraft-java", True, "daily", "03:15", keep)
        with self.assertRaises(B.BackupError):
            B.set_schedule("minecraft@evil", True, "daily", "03:15", 5)

    def test_removed_and_restored_servers(self):
        B.set_schedule("minecraft-java", True, "daily", "03:15", 5)
        timer = os.path.join(self.units, "redstonepanel-backup@minecraft-java.timer")
        B.on_server_removed("minecraft-java", purged=False)
        self.assertFalse(os.path.exists(timer))
        self.assertTrue(B.get_schedule("minecraft-java")["enabled"])        # remembered for a restore
        B.on_server_restored("minecraft-java")
        self.assertTrue(os.path.exists(timer))
        B.on_server_removed("minecraft-java", purged=True)
        self.assertFalse(B.get_schedule("minecraft-java")["enabled"])


@unittest.skipIf(B is None, "needs Linux (fcntl)")
class FileChangedWhileCopied(unittest.TestCase):
    """1.24: a file the running server cuts shorter mid-copy no longer fails the whole backup
    (seen on Windows Server 2025 CI as "OSError: unexpected end of data")."""

    def test_shorter_file_is_filled_out_and_noted(self):
        d = tempfile.mkdtemp(dir=TMP)
        write(os.path.join(d, "a.txt"), "hello")
        write(os.path.join(d, "b.txt"), "world")
        real = tarfile.TarFile.gettarinfo

        def grown(tar, name=None, arcname=None, fileobj=None):
            info = real(tar, name, arcname, fileobj)
            if arcname == "a.txt":
                info.size += 100           # looked at before the server truncated it
            return info

        buf, log = io.BytesIO(), []
        with mock.patch.object(tarfile.TarFile, "gettarinfo", grown), \
                mock.patch.object(M, "write_log", log.append), \
                tarfile.open(fileobj=buf, mode="w:gz") as tar:
            B._add_tree(tar, d)
        buf.seek(0)
        with tarfile.open(fileobj=buf, mode="r:gz") as tar:
            self.assertEqual(tar.extractfile("a.txt").read(), b"hello" + b"\0" * 100)
            self.assertEqual(tar.extractfile("b.txt").read(), b"world")
        self.assertTrue(any("a.txt got shorter" in l for l in log), log)


if __name__ == "__main__":
    unittest.main()
