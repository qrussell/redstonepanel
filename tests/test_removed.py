"""Tests for removed servers in core/manager.py: listing folders without a systemd unit,
restoring them, and name/port reservation. No systemctl or root needed: units live in a temp
folder and commands are recorded instead of run. Run: python -m unittest discover -s tests"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-removed-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import manager as M  # noqa: E402


class RemovedServers(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(dir=TMP)
        self.servers = os.path.join(self.root, "opt")
        self.units = os.path.join(self.root, "units")
        os.makedirs(self.servers)
        os.makedirs(self.units)
        self.commands = []
        patches = [
            mock.patch.object(M, "SERVERS_ROOT", self.servers),
            mock.patch.object(M, "_unit_path", lambda n: os.path.join(self.units, f"{n}.service")),
            mock.patch.object(M, "_unit_dirs", lambda: set()),
            mock.patch.object(M, "run_and_log", lambda cmd, shell=False: self.commands.append(cmd)),
            mock.patch.object(M, "require_tools", lambda *t: None),
            mock.patch.object(M, "open_firewall", lambda port, proto: self.commands.append(["ufw", port, proto])),
            mock.patch.object(M, "ensure_minecraft_user", lambda home: None),
            mock.patch.object(M, "_free_rcon_port", lambda d, start=25575: "25599"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.written_units = {}
        def write_unit(name, d, edition, xms="1G", xmx="2G", java="/usr/bin/java"):
            self.written_units[name] = (d, edition, xms, xmx)
            open(M._unit_path(name), "w").close()
        p = mock.patch.object(M, "_write_unit", write_unit)
        p.start()
        self.addCleanup(p.stop)

    def server(self, name, files=None, props=None, unit=False):
        d = os.path.join(self.servers, name)
        os.makedirs(d)
        for f, content in (files or {}).items():
            with open(os.path.join(d, f), "w") as fh:
                fh.write(content)
        if props is not None:
            with open(os.path.join(d, "server.properties"), "w") as fh:
                fh.write("".join(f"{k}={v}\n" for k, v in props.items()))
        if unit:
            open(M._unit_path(name), "w").close()
        return d

    def names(self):
        return [s["name"] for s in M.list_removed_servers()]

    def test_lists_only_folders_without_a_unit(self):
        self.server("minecraft-java", {"server.jar": ""}, {"server-port": 25565}, unit=True)
        self.server("minecraft-java-2", {"server.jar": ""}, {"server-port": 25566})
        self.server("not-minecraft", {"server.jar": ""})
        self.server(".redstonepanel-import-abc", {"server.jar": ""})
        self.assertEqual(self.names(), ["minecraft-java-2"])

    def test_skips_folders_in_use_by_a_hand_written_unit(self):
        d = self.server("minecraft-java-2", {"server.jar": ""})
        with mock.patch.object(M, "_unit_dirs", lambda: {os.path.realpath(d)}):
            self.assertEqual(self.names(), [])

    def test_skips_names_being_deployed(self):
        self.server("minecraft-java-2", {"server.jar": ""})
        M._busy.add("minecraft-java-2")
        try:
            self.assertEqual(self.names(), [])
        finally:
            M._release_name("minecraft-java-2")

    def test_edition_detection(self):
        self.server("minecraft-bedrock", {"bedrock_server": ""})
        self.server("minecraft-java", {"eula.txt": "eula=true"})          # binary removed (pre-1.6)
        self.server("minecraft-custom", {"README": ""})
        os.makedirs(os.path.join(self.server("minecraft-other"), "worlds"))
        found = {s["name"]: s for s in M.list_removed_servers()}
        self.assertEqual(found["minecraft-bedrock"]["edition"], "Bedrock")
        self.assertTrue(found["minecraft-bedrock"]["has_binary"])
        self.assertEqual(found["minecraft-java"]["edition"], "Java")
        self.assertFalse(found["minecraft-java"]["has_binary"])
        self.assertEqual(found["minecraft-other"]["edition"], "Bedrock")
        self.assertEqual(found["minecraft-custom"]["edition"], "Unknown")
        self.assertFalse(found["minecraft-custom"]["can_restore"])

    def test_restore_keeps_port_and_saved_heap(self):
        d = self.server("minecraft-java-2", {"server.jar": "",
                                             M.REMOVED_INFO: json.dumps({"edition": "java", "xms": "2G", "xmx": "6G"})},
                        {"server-port": 25570, "query.port": 25570})
        r = M.restore_server("minecraft-java-2")
        self.assertEqual(r["port"], 25570)
        self.assertIsNone(r["old_port"])
        self.assertFalse(r["downloaded"])
        self.assertEqual(self.written_units["minecraft-java-2"], (d, "java", "2G", "6G"))
        self.assertTrue(os.path.exists(os.path.join(d, "eula.txt")))
        self.assertFalse(os.path.exists(os.path.join(d, M.REMOVED_INFO)))
        self.assertIn(["systemctl", "start", "minecraft-java-2.service"], self.commands)
        self.assertEqual(self.names(), [])

    def test_restore_ignores_unsafe_heap_values(self):
        self.server("minecraft-java-2", {"server.jar": "",
                                         M.REMOVED_INFO: json.dumps({"xms": "1G -Dx=y", "xmx": "2G\nUser=root"})},
                    {"server-port": 25570})
        M.restore_server("minecraft-java-2")
        self.assertEqual(self.written_units["minecraft-java-2"][2:], ("1G", "2G"))

    def test_restore_moves_a_taken_port(self):
        self.server("minecraft-java", {"server.jar": ""}, {"server-port": 25565, "rcon.port": 25575}, unit=True)
        d = self.server("minecraft-java-2", {"server.jar": ""},
                        {"server-port": 25565, "query.port": 25565, "rcon.port": 25575})
        r = M.restore_server("minecraft-java-2")
        self.assertEqual(r["old_port"], "25565")
        self.assertEqual(r["port"], 25566)
        props = M.read_properties(d)
        self.assertEqual(props["server-port"], "25566")
        self.assertEqual(props["query.port"], "25566")
        self.assertEqual(props["rcon.port"], "25599")

    def test_restore_bedrock_moves_both_ports(self):
        self.server("minecraft-bedrock", {"bedrock_server": ""}, {"server-port": 19132, "server-portv6": 19133}, unit=True)
        d = self.server("minecraft-bedrock-2", {"bedrock_server": ""}, {"server-port": 19133, "server-portv6": 19134})
        r = M.restore_server("minecraft-bedrock-2")
        self.assertEqual(r["port"], 19134)
        props = M.read_properties(d)
        self.assertEqual((props["server-port"], props["server-portv6"]), ("19134", "19135"))
        self.assertTrue(os.path.exists(os.path.join(d, "screenrc")))
        self.assertEqual(self.written_units["minecraft-bedrock-2"][1], "bedrock")

    def test_restore_downloads_a_missing_binary(self):
        self.server("minecraft-java-2", {"eula.txt": "eula=true"}, {"server-port": 25570})
        with mock.patch.object(M, "_install_java", return_value=({}, "/usr/bin/java")) as fetch:
            r = M.restore_server("minecraft-java-2")
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args[0][1], "vanilla")
        self.assertTrue(r["downloaded"])

    def test_restore_refuses_unknown_names(self):
        self.server("minecraft-java", {"server.jar": ""}, unit=True)
        for name in ("minecraft-java", "minecraft-nope", "../etc"):
            with self.assertRaises(M.ServerError):
                M.restore_server(name)

    def test_delete_removed_server(self):
        d = self.server("minecraft-java-2", {"server.jar": ""})
        M.delete_removed_server("minecraft-java-2")
        self.assertIn(["rm", "-rf", os.path.realpath(d)], self.commands)
        with self.assertRaises(M.ServerError):
            M.delete_removed_server("minecraft-nope")

    def test_reserve_name_skips_folders_units_and_reserved_names(self):
        self.server("minecraft-java", {"server.jar": ""}, {"server-port": 25565})   # removed: still holds its port
        name, port = M._reserve_name("java")
        try:
            self.assertEqual((name, port), ("minecraft-java-2", 25566))
            self.assertEqual(M._reserve_name("java"), ("minecraft-java-3", 25567))
        finally:
            M._release_name("minecraft-java-2")
            M._release_name("minecraft-java-3")


if __name__ == "__main__":
    unittest.main()
