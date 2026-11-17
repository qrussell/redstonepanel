"""Tests for core/bedrock_net.py. Run: python -m unittest discover -s tests"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import bedrock_net as B  # noqa: E402

# grep output from real 1.26.63.1 logs (trimmed): raknet start, then two NetherNet starts
RAKNET_START = [
    "[2026-11-17 04:07:26:767 INFO] Version: 1.26.63.1",
    "[2026-11-17 04:07:26:783 ERROR] ================ TRANSPORT TYPE ERROR  ===================",
    "[2026-11-17 04:07:30:100 INFO] Server started.",
]
NETHERNET_OK = [
    "[2026-11-17 04:21:23:140 INFO] Version: 1.26.63.1",
    "[2026-11-17 04:21:24:407 INFO] Accepting clients on 0.0.0.0:19132",
    "[2026-11-17 04:21:26:178 INFO] Server started.",
]
NETHERNET_NO_LISTENER = [   # server-udp-ports=173.41.136.110:19141:19141
    "[2026-11-17 04:24:39:022 INFO] Version: 1.26.63.1",
    "[2026-11-17 04:24:43:416 INFO] Server started.",
]


class LastStart(unittest.TestCase):
    def test_transport_rejected(self):
        s = B.last_start_status(RAKNET_START)
        self.assertTrue(s["transport_rejected"])
        self.assertIsNone(s["accepting"])

    def test_only_the_last_start_counts(self):
        s = B.last_start_status(RAKNET_START + NETHERNET_OK)
        self.assertFalse(s["transport_rejected"])
        self.assertEqual(s["accepting"], "0.0.0.0:19132")
        self.assertTrue(s["started"])

    def test_started_without_listener(self):
        s = B.last_start_status(NETHERNET_OK + NETHERNET_NO_LISTENER)
        self.assertTrue(s["started"])
        self.assertIsNone(s["accepting"])

    def test_still_starting(self):
        s = B.last_start_status(NETHERNET_OK + NETHERNET_NO_LISTENER[:1])
        self.assertFalse(s["started"])

    def test_empty_log(self):
        self.assertEqual(B.last_start_status([]), {"started": False, "transport_rejected": False, "accepting": None})

    def test_grep_pattern_matches_the_lines_used(self):
        import re
        pat = re.compile(B.GREP_PATTERN)
        for line in RAKNET_START + NETHERNET_OK:
            self.assertTrue(pat.search(line), line)


class UdpPorts(unittest.TestCase):
    def test_address_forms(self):
        for v in ("173.41.136.110:19141:19141", "12.19.20.18:19489:19489",
                  "[2001:db8::2021:26:03]:21200-21300:19400-19500"):
            self.assertTrue(B.udp_ports_have_address(v), v)

    def test_plain_forms(self):
        for v in ("", None, "19141", "29489:19489", "19400-19500", "21200-21300:19400-19500"):
            self.assertFalse(B.udp_ports_have_address(v), v)

    def test_plain_suggestion(self):
        self.assertEqual(B.plain_udp_ports("173.41.136.110:19141:19141"), "19141")
        self.assertEqual(B.plain_udp_ports("12.19.20.18:29489:19489"), "29489:19489")
        self.assertEqual(B.plain_udp_ports("[2001:db8::1]:21200-21300:19400-19500"), "21200-21300:19400-19500")
        self.assertEqual(B.plain_udp_ports("19141"), "19141")


if __name__ == "__main__":
    unittest.main()
