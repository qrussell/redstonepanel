"""Tests for core/proxies.py (Phantom detection). Run: python -m unittest discover -s tests"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import proxies as P  # noqa: E402

# The unit found on a real host: Phantom pointed at the Bedrock server on the same machine
LOOP_UNIT = """[Unit]
Description=Phantom Proxy (minecraft-bedrock)
After=network.target minecraft-bedrock.service
[Service]
Type=simple
User=qrussell
ExecStart=/home/qrussell/phantom-minecraft-bedrock/phantom-linux -server 127.0.0.1:19132 -bind 0.0.0.0 -bind_port 19132
Restart=on-failure
[Install]
WantedBy=multi-user.target
"""

BDS_UNIT = """[Service]
ExecStart=/usr/bin/screen -c /opt/minecraft-bedrock/screenrc -DmS minecraft-bedrock ./bedrock_server
"""


class Parse(unittest.TestCase):
    def test_exec_start(self):
        argv = P.parse_exec_start(LOOP_UNIT)
        self.assertTrue(argv[0].endswith("phantom-linux"))
        self.assertIn("-server", argv)

    def test_exec_start_prefix_and_missing(self):
        self.assertEqual(P.parse_exec_start("ExecStart=-/bin/phantom -server a:1")[0], "/bin/phantom")
        self.assertIsNone(P.parse_exec_start("[Unit]\nDescription=x\n"))

    def test_phantom_args(self):
        p = P.parse_phantom(P.parse_exec_start(LOOP_UNIT))
        self.assertEqual(p["target"], "127.0.0.1:19132")
        self.assertEqual(p["target_host"], "127.0.0.1")
        self.assertEqual(p["target_port"], 19132)
        self.assertEqual(p["bind_port"], 19132)

    def test_equals_form_and_defaults(self):
        p = P.parse_phantom(["/opt/phantom", "--server=play.example.com:19140"])
        self.assertEqual(p["target_host"], "play.example.com")
        self.assertEqual(p["target_port"], 19140)
        self.assertEqual(p["bind_port"], 0)
        self.assertEqual(P.ports_held(p), [19132])

    def test_not_phantom(self):
        self.assertIsNone(P.parse_phantom(P.parse_exec_start(BDS_UNIT)))
        self.assertIsNone(P.parse_phantom(None))


class Problems(unittest.TestCase):
    def setUp(self):
        self.loop = P.parse_phantom(P.parse_exec_start(LOOP_UNIT))

    def test_loop_and_port_clash(self):
        probs = P.problems_for(self.loop, 19132)
        self.assertEqual(len(probs), 2)
        self.assertIn("talks to itself", probs[0])
        self.assertIn("UDP 19132", probs[1])

    def test_remote_target_on_other_port_is_fine(self):
        p = P.parse_phantom(["phantom", "-server", "10.0.0.50:19132", "-bind_port", "19200"])
        # Phantom still holds 19132, so a local server on 19132 clashes; one on 19134 doesn't
        self.assertEqual(len(P.problems_for(p, 19132)), 1)
        self.assertEqual(P.problems_for(p, 19134), [])
        self.assertFalse(P.related_to(p, 19134))

    def test_local_ip_counts_as_local(self):
        p = P.parse_phantom(["phantom", "-server", "10.0.0.193:19134", "-bind_port", "19134"])
        self.assertTrue(P.related_to(p, 19134, local_ips=("10.0.0.193",)))
        self.assertIn("talks to itself", P.problems_for(p, 19134, local_ips=("10.0.0.193",))[0])

    def test_proxies_for_server(self):
        found = P.proxies_for_server([{"unit": "x.service", **self.loop}], "19132", local_ips=())
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["listens"], [19132])
        self.assertEqual(P.proxies_for_server([{"unit": "x", **self.loop}], "not-a-port", local_ips=()), [])


class Find(unittest.TestCase):
    def test_finds_by_command_not_name(self):
        with tempfile.TemporaryDirectory() as d:
            for name, text in {"anything-at-all.service": LOOP_UNIT,
                               "minecraft-bedrock.service": BDS_UNIT,
                               "notes.txt": LOOP_UNIT}.items():
                with open(os.path.join(d, name), "w") as f:
                    f.write(text)
            found = P.find_phantoms(d, state=lambda unit: ("inactive", "disabled"))
        self.assertEqual([f["unit"] for f in found], ["anything-at-all.service"])
        self.assertEqual(found[0]["active"], "inactive")


if __name__ == "__main__":
    unittest.main()
