"""Tests for core/phantom.py (Phantom LAN proxies the panel installs for Bedrock servers).
Run: python -m unittest discover -s tests"""
import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-phantom-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import manager, phantom  # noqa: E402

HAND_MADE = """[Service]
ExecStart=/home/me/phantom-linux -server 127.0.0.1:19132 -bind 0.0.0.0 -bind_port 19132
"""


class Host:
    """A fake host: Bedrock servers (name -> port), units in a temp folder, systemctl/ufw recorded."""

    def __init__(self, test, servers):
        self.dir = tempfile.mkdtemp(dir=TMP)
        self.bin = tempfile.mkdtemp(dir=TMP)
        self.servers = servers
        self.cmds = []
        self.active = {}
        patches = [
            mock.patch.object(phantom, "UNIT_DIR", self.dir),
            mock.patch.object(phantom, "BIN_DIR", self.bin),
            mock.patch.object(phantom, "_unit_state", lambda u: (self.active.get(u, "inactive"), "enabled")),
            mock.patch.object(phantom, "_local_ips", lambda: ("10.0.0.5",)),
            mock.patch.object(phantom, "_journal", lambda u, n=15: ["started"]),
            mock.patch.object(phantom, "_systemctl", lambda *a: self.cmds.append(["systemctl", *a])),
            mock.patch.object(phantom.platform, "machine", lambda: "x86_64"),
            mock.patch.object(phantom.shutil, "which", lambda name: "/usr/sbin/" + name),
            mock.patch.object(manager, "_check_server", self.check),
            mock.patch.object(manager, "list_servers", self.list_servers),
            mock.patch.object(manager, "read_properties", lambda d: {"server-port": str(self.servers[d])}),
            mock.patch.object(manager, "run_and_log", self.run),
            mock.patch.object(manager, "write_log", lambda m: None),
            mock.patch.object(manager, "open_firewall", lambda port, proto: self.cmds.append(["ufw", "allow", f"{port}/{proto}"])),
        ]
        for m in patches:
            m.start()
            test.addCleanup(m.stop)

    def check(self, name):
        if name not in self.servers:
            raise manager.ServerError(f"No Minecraft server called '{name}' on this panel.")

    def list_servers(self):
        return [{"name": n, "edition": "Java" if n.startswith("minecraft-java") else "Bedrock",
                 "port": str(port), "directory": n} for n, port in self.servers.items()]

    def run(self, cmd, shell=False):
        self.cmds.append(cmd)
        if cmd[:2] == ["systemctl", "restart"]:
            self.active[cmd[2]] = "active"

    def write_unit(self, name, text):
        with open(os.path.join(self.dir, name), "w") as f:
            f.write(text)

    def unit(self, name):
        with open(os.path.join(self.dir, name)) as f:
            return f.read()


def fake_download(url, allowed, algorithm, expected, size=None):
    assert url.endswith("/v0.5.4/phantom-linux"), url
    assert "github.com" in allowed and algorithm == "sha256"
    assert expected == phantom.ASSETS["x86_64"][1]
    return io.BytesIO(b"\x7fELF fake phantom")


class Install(unittest.TestCase):
    def setUp(self):
        self.h = Host(self, {"minecraft-island": 19140, "minecraft-cave": 19150})
        m = mock.patch("core.sources.download", side_effect=fake_download)
        self.download = m.start()
        self.addCleanup(m.stop)

    def test_status_before_install(self):
        st = phantom.status("minecraft-island")
        self.assertIsNone(st["phantom"])
        self.assertTrue(st["can_install"])
        self.assertEqual(st["port"], 19140)

    def test_install_writes_unit_opens_ports_and_starts(self):
        st = phantom.install("minecraft-island", actor="alice")
        text = self.h.unit("phantom-minecraft-island.service")
        self.assertIn("X-RedstonePanel-Server=minecraft-island", text)
        self.assertIn("User=minecraft", text)
        self.assertIn(f"ExecStart={phantom.binary_path()} -server 127.0.0.1:19140 -bind_port 19300", text)
        self.assertTrue(os.access(phantom.binary_path(), os.X_OK) or os.name == "nt")
        self.assertIn(["ufw", "allow", "19132/udp"], self.h.cmds)
        self.assertIn(["ufw", "allow", "19300/udp"], self.h.cmds)
        self.assertIn(["systemctl", "enable", "phantom-minecraft-island.service"], self.h.cmds)
        self.assertEqual(st["phantom"]["active"], "active")
        self.assertTrue(st["phantom"]["managed"])
        self.assertEqual(st["phantom"]["listens"], [19132, 19300])
        self.assertEqual(st["phantom"]["problems"], [])

    def test_two_servers_get_different_proxy_ports_and_one_download(self):
        phantom.install("minecraft-island")
        phantom.install("minecraft-cave")
        self.assertIn("-bind_port 19301", self.h.unit("phantom-minecraft-cave.service"))
        self.assertEqual(self.download.call_count, 1)

    def test_repair_keeps_proxy_port_and_follows_new_server_port(self):
        phantom.install("minecraft-island")
        self.h.servers["minecraft-island"] = 19145
        st = phantom.status("minecraft-island")
        self.assertTrue(any("now uses 19145" in p for p in st["phantom"]["problems"]))
        phantom.install("minecraft-island")
        text = self.h.unit("phantom-minecraft-island.service")
        self.assertIn("-server 127.0.0.1:19145 -bind_port 19300", text)

    def test_checksum_failure_installs_nothing(self):
        from core import sources
        self.download.side_effect = sources.SourceError("didn't match its checksum", 502)
        with self.assertRaises(sources.SourceError):
            phantom.install("minecraft-island")
        self.assertFalse(os.path.exists(os.path.join(self.h.dir, "phantom-minecraft-island.service")))

    def test_control_and_uninstall(self):
        phantom.install("minecraft-island")
        phantom.install("minecraft-cave")
        unit = "phantom-minecraft-island.service"
        phantom.control("minecraft-island", unit, "stop")
        self.assertIn(["systemctl", "stop", unit], self.h.cmds)
        with self.assertRaises(phantom.PhantomError):
            phantom.control("minecraft-island", unit, "kill")
        with self.assertRaises(phantom.PhantomError):         # another server's Phantom
            phantom.control("minecraft-island", "phantom-minecraft-cave.service", "stop")
        self.h.cmds.clear()
        st = phantom.uninstall("minecraft-island")
        self.assertIsNone(st["phantom"])
        self.assertFalse(os.path.exists(os.path.join(self.h.dir, unit)))
        self.assertIn(["ufw", "delete", "allow", "19300/udp"], self.h.cmds)
        # the cave's Phantom still needs 19132
        self.assertNotIn(["ufw", "delete", "allow", "19132/udp"], self.h.cmds)
        self.h.cmds.clear()
        phantom.uninstall("minecraft-cave")
        self.assertIn(["ufw", "delete", "allow", "19132/udp"], self.h.cmds)

    def test_removed_with_server(self):
        phantom.install("minecraft-island")
        phantom.on_server_removed("minecraft-island")
        self.assertFalse(os.path.exists(os.path.join(self.h.dir, "phantom-minecraft-island.service")))
        self.assertIn(["ufw", "delete", "allow", "19300/udp"], self.h.cmds)
        phantom.on_server_removed("minecraft-cave")              # had none: nothing happens

    def test_unit_names_are_checked(self):
        for bad in ("../x.service", "a b.service", "x.timer", ""):
            with self.assertRaises(phantom.PhantomError):
                phantom.control("minecraft-island", bad, "start")


class Refusals(unittest.TestCase):
    def test_server_on_19132_needs_no_phantom(self):
        h = Host(self, {"minecraft-bedrock": 19132})
        st = phantom.status("minecraft-bedrock")
        self.assertFalse(st["can_install"])
        self.assertIn("already shows up as a LAN game", st["blocker"])
        with self.assertRaises(phantom.PhantomError):
            phantom.install("minecraft-bedrock")
        self.assertEqual(os.listdir(h.dir), [])

    def test_other_server_holding_19132(self):
        Host(self, {"minecraft-bedrock": 19132, "minecraft-island": 19140})
        st = phantom.status("minecraft-island")
        self.assertFalse(st["can_install"])
        self.assertIn("minecraft-bedrock uses UDP 19132", st["blocker"])

    def test_java_server(self):
        Host(self, {"minecraft-java": 25565})
        with self.assertRaises(phantom.PhantomError):
            phantom.status("minecraft-java")

    def test_unknown_server(self):
        Host(self, {})
        with self.assertRaises(manager.ServerError):
            phantom.status("minecraft-nope")

    def test_unsupported_machine(self):
        Host(self, {"minecraft-island": 19140})
        with mock.patch.object(phantom.platform, "machine", lambda: "riscv64"):
            st = phantom.status("minecraft-island")
        self.assertFalse(st["can_install"])
        self.assertIn("riscv64", st["blocker"])


class HandMade(unittest.TestCase):
    def test_same_name_unit_made_by_hand(self):
        h = Host(self, {"minecraft-island": 19140})
        h.write_unit("phantom-minecraft-island.service",
                     HAND_MADE.replace("127.0.0.1:19132", "127.0.0.1:19140").replace("-bind_port 19132", "-bind_port 19300"))
        st = phantom.status("minecraft-island")
        self.assertIsNone(st["phantom"])
        self.assertEqual([o["unit"] for o in st["others"]], ["phantom-minecraft-island.service"])
        self.assertFalse(st["others"][0]["managed"])
        self.assertIn("wasn't made by the panel", st["blocker"])
        with self.assertRaises(phantom.PhantomError):
            phantom.install("minecraft-island")
        # Removing it deletes only the unit file; the panel can install its own afterwards
        phantom.uninstall("minecraft-island", "phantom-minecraft-island.service")
        self.assertEqual(os.listdir(h.dir), [])
        self.assertTrue(phantom.status("minecraft-island")["can_install"])

    def test_loop_unit_reported_and_kept_on_server_removal(self):
        h = Host(self, {"minecraft-bedrock": 19132})
        h.write_unit("phantom-minecraft-bedrock.service", HAND_MADE)
        st = phantom.status("minecraft-bedrock")
        probs = " ".join(st["others"][0]["problems"])
        self.assertIn("talks to itself", probs)
        phantom.on_server_removed("minecraft-bedrock")         # not the panel's: left alone
        self.assertEqual(os.listdir(h.dir), ["phantom-minecraft-bedrock.service"])

    def test_unrelated_phantom_is_not_controllable(self):
        h = Host(self, {"minecraft-island": 19140})
        h.write_unit("other.service", HAND_MADE.replace("127.0.0.1:19132", "10.9.9.9:19999").replace("-bind_port 19132", "-bind_port 19500"))
        st = phantom.status("minecraft-island")
        self.assertEqual(st["others"], [])
        with self.assertRaises(phantom.PhantomError):
            phantom.control("minecraft-island", "other.service", "stop")


class Journal(unittest.TestCase):
    def test_colour_codes_are_stripped(self):
        out = "\x1b[90m3:01AM\x1b[0m \x1b[32mINF\x1b[0m Listener starting up: 0.0.0.0:19300\n\n"
        with mock.patch.object(phantom.subprocess, "run", return_value=mock.Mock(stdout=out)):
            self.assertEqual(phantom._journal("phantom-x.service"), ["3:01AM INF Listener starting up: 0.0.0.0:19300"])


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
