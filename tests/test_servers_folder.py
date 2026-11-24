"""Tests for the servers folder setting (1.21.1): where new servers go, and that servers in a
folder chosen earlier still count everywhere. Nothing here needs root or systemd.
Run: python -m unittest discover -s tests"""
import os
import sys
import tempfile
import unittest
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-folder-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import manager as M  # noqa: E402
from core import settings as S  # noqa: E402
from core.platform.linux import system as linux_system  # noqa: E402


class Folders(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(dir=TMP)
        self.usual = os.path.join(self.root, "opt")
        self.second = os.path.join(self.root, "disk2", "minecraft")
        self.units = os.path.join(self.root, "units")
        for d in (self.usual, self.second, self.units):
            os.makedirs(d)
        self.settings = dict(S.DEFAULTS)
        for p in (mock.patch.object(M, "SERVERS_ROOT", self.usual),
                  mock.patch.object(M, "_unit_path", lambda n: os.path.join(self.units, f"{n}.service")),
                  mock.patch.object(M, "_unit_dirs", lambda: set()),
                  mock.patch.object(S, "load", lambda: dict(self.settings))):
            p.start()
            self.addCleanup(p.stop)

    def choose(self, folder):
        self.settings["servers_folders_used"] = [M.servers_root()] + self.settings["servers_folders_used"]
        self.settings["servers_folder"] = folder

    def server(self, root, name, props=None, unit=True):
        d = os.path.join(root, name)
        if unit:
            open(os.path.join(self.units, f"{name}.service"), "w").close()
        os.makedirs(d)
        open(os.path.join(d, "eula.txt"), "w").close()
        with open(os.path.join(d, "server.properties"), "w") as f:
            f.write("".join(f"{k}={v}\n" for k, v in (props or {}).items()))
        return d

    def test_the_usual_place_until_one_is_chosen(self):
        self.assertEqual(M.servers_root(), self.usual)
        self.assertEqual(M.server_roots(), [self.usual])
        self.choose(self.second)
        self.assertEqual(M.servers_root(), self.second)
        self.assertEqual(M.server_roots(), [self.second, self.usual])

    def test_a_folder_chosen_before_is_remembered(self):
        third = os.path.join(self.root, "disk3")
        self.choose(self.second)
        self.choose(third)
        self.assertEqual(M.server_roots(), [third, self.usual, self.second])

    def test_a_new_server_never_takes_a_name_used_in_another_folder(self):
        self.server(self.usual, "minecraft-java", {"server-port": 25565})
        self.choose(self.second)
        with mock.patch.object(M, "_port_bindable", return_value=True):
            name, port = M._reserve_name("java")
        M._release_name(name)
        self.assertEqual((name, port), ("minecraft-java-2", 25566))
        self.assertFalse(M._reserve_exact("minecraft-java"))

    def test_ports_of_servers_in_every_folder_count(self):
        self.server(self.usual, "minecraft-java", {"server-port": 25565, "rcon.port": 25575})
        self.choose(self.second)
        self.server(self.second, "minecraft-java-2", {"server-port": 25570})
        self.assertEqual(M._used_ports(), {25565, 25575, 25570})

    def test_removed_servers_are_found_in_every_folder(self):
        self.server(self.usual, "minecraft-old", unit=False)
        self.choose(self.second)
        self.server(self.second, "minecraft-new", unit=False)
        found = {s["name"]: s["directory"] for s in M.list_removed_servers()}
        self.assertEqual(found, {"minecraft-old": os.path.join(self.usual, "minecraft-old"),
                                 "minecraft-new": os.path.join(self.second, "minecraft-new")})

    def test_only_folders_directly_inside_a_servers_folder_may_be_deleted(self):
        self.choose(self.second)
        self.assertTrue(M.in_servers_folder(os.path.join(self.usual, "minecraft-a")))
        self.assertTrue(M.in_servers_folder(os.path.join(self.second, "minecraft-a")))
        self.assertFalse(M.in_servers_folder(os.path.join(self.root, "minecraft-a")))
        self.assertFalse(M.in_servers_folder(os.path.join(self.second, "minecraft-a", "world")))
        self.assertFalse(M.in_servers_folder(self.second))

    def test_an_unpacked_backup_becomes_a_server_on_the_disk_it_is_on(self):
        self.choose(self.second)
        self.assertEqual(M._root_holding(os.path.join(self.usual, ".redstonepanel-import-x", "srv")), self.usual)
        self.assertEqual(M._root_holding(os.path.join(self.second, ".redstonepanel-import-x")), self.second)
        self.assertEqual(M._root_holding(os.path.join(self.root, "elsewhere")), self.second)


class WhatLinuxAccepts(unittest.TestCase):
    def test_system_folders_and_odd_names_are_refused(self):
        for bad in ("minecraft", "~/minecraft", "/", "/home", "/var", "/mnt", "/etc/minecraft", "/usr/local/mc",
                    "/var/lib/redstonepanel/servers", "/var/backups/redstonepanel", "/tmp/mc", "/root/mc",
                    "/srv/my servers", "/srv/mc;rm", "/srv/../etc/mc", "/proc/1"):
            clean, error = linux_system.check_servers_folder(bad)
            self.assertIsNone(clean, bad)
            self.assertTrue(error, bad)

    def test_a_folder_of_its_own_is_accepted_and_nothing_is_made(self):
        for good in ("/srv/minecraft-rp-test", "/mnt/disk2/minecraft-rp-test", "/home/minecraft-rp-test/servers",
                     "/opt/minecraft-rp-test/"):
            clean, error = linux_system.check_servers_folder(good)
            self.assertEqual((clean, error), (os.path.realpath(good.rstrip("/")), ""), good)
            self.assertFalse(os.path.exists(good))

    def test_a_file_is_not_a_folder(self):
        with tempfile.NamedTemporaryFile(dir="/var/tmp") as f:
            with mock.patch.object(linux_system, "_NOT_FOR_SERVERS", ()):
                clean, error = linux_system.check_servers_folder(f.name)
        self.assertIsNone(clean)
        self.assertIn("file", error)

    def test_a_noexec_or_read_only_disk_is_refused(self):
        real = os.statvfs("/")
        for flag, word in ((os.ST_RDONLY, "read-only"), (getattr(os, "ST_NOEXEC", 8), "noexec")):
            fake = mock.Mock(f_flag=real.f_flag | flag)
            with mock.patch.object(os, "statvfs", return_value=fake):
                clean, error = linux_system.check_servers_folder("/srv/minecraft-rp-test")
            self.assertIsNone(clean)
            self.assertIn(word, error)


class TheSetting(unittest.TestCase):
    def check(self, folder):
        from core.platform import system
        with mock.patch.object(system, "check_servers_folder", linux_system.check_servers_folder):
            return S.clean_servers_folder(folder)

    def test_empty_and_the_usual_place_mean_the_same(self):
        from core.platform import paths
        self.assertEqual(self.check(""), "")
        self.assertEqual(self.check("  "), "")
        self.assertEqual(self.check(paths.SERVERS_ROOT), "")

    def test_words_the_panel_tells_editions_apart_by_are_refused(self):
        for bad in ("/srv/java-servers", "/srv/Bedrock", "/mnt/JAVA/mc"):
            with self.assertRaises(S.SettingsError):
                self.check(bad)

    def test_a_refusal_reaches_the_person_as_it_is(self):
        with self.assertRaises(S.SettingsError) as e:
            self.check("/etc/minecraft")
        self.assertIn("/etc", str(e.exception))

    def test_folders_used_before_are_kept_once(self):
        from core.platform import system
        with mock.patch.object(system, "check_servers_folder", linux_system.check_servers_folder):
            s = S.validate(dict(S.DEFAULTS, servers_folder="/srv/minecraft-rp-test",
                                servers_folders_used=["/opt", "/opt", "/srv/minecraft-rp-test", "", 5]))
        self.assertEqual(s["servers_folder"], "/srv/minecraft-rp-test")
        self.assertEqual(s["servers_folders_used"], ["/opt"])


if __name__ == "__main__":
    unittest.main()
