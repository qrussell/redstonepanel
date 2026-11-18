"""Tests for core/host.py, the dashboard's host report. The /proc readers are fed text; the
report is built with systemctl, apt and the server list faked. Run: python -m unittest discover -s tests"""
import os
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-host-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import host as H  # noqa: E402

MEMINFO = """MemTotal:        8000000 kB
MemFree:          500000 kB
MemAvailable:     600000 kB
Cached:          1000000 kB
SwapTotal:       2000000 kB
SwapFree:         200000 kB
"""
NET_DEV = """Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
    lo: 999 1 0 0 0 0 0 0 999 1 0 0 0 0 0 0
  eth0: 1000 10 0 0 0 0 0 0 2000 20 0 0 0 0 0 0
vethab: 5 1 0 0 0 0 0 0 5 1 0 0 0 0 0 0
"""
SHOW = """Id=minecraft-java.service
LoadState=loaded
ActiveState=active
SubState=running
NRestarts=0
MemoryCurrent=1073741824
CPUUsageNSec=5000000000
ActiveEnterTimestampMonotonic=1000000

Id=redstonepanel-backup@minecraft-java.timer
LoadState=loaded
ActiveState=active
NextElapseUSecRealtime=Wed 2030-03-17 17:46:40 UTC

Id=redstonepanel-restart@minecraft-java.timer
LoadState=not-found
ActiveState=inactive
NextElapseUSecRealtime=

Id=redstonepanel.service
ActiveState=active
MemoryCurrent=[not set]
"""
UFW = """Status: active

To                         Action      From
--                         ------      ----
22/tcp                     ALLOW       Anywhere
25565/tcp                  ALLOW       Anywhere
19132:19133/udp            ALLOW       Anywhere
8080                       ALLOW       Anywhere
"""


class Parsers(unittest.TestCase):
    def test_memory(self):
        m = H.memory(MEMINFO)
        self.assertEqual(m["total"], 8000000 * 1024)
        self.assertEqual(m["available"], 600000 * 1024)
        self.assertEqual(m["percent"], 92.5)
        self.assertEqual(m["swap_percent"], 90.0)

    def test_memory_without_memavailable(self):
        m = H.memory("MemTotal: 1000 kB\nMemFree: 100 kB\nCached: 200 kB\n")
        self.assertEqual(m["available"], 300 * 1024)
        self.assertIsNone(m["swap_percent"])

    def test_cpu_stat(self):
        busy, total = H.parse_cpu_stat("cpu  100 0 50 800 50 0 0 0 0 0\ncpu0 1 2 3 4\n")
        self.assertEqual((busy, total), (150, 1000))
        self.assertIsNone(H.parse_cpu_stat("intr 1 2 3"))

    def test_net_dev_skips_loopback_and_veths(self):
        self.assertEqual(H.parse_net_dev(NET_DEV), {"eth0": (1000, 2000)})

    def test_cpu_model_and_os(self):
        self.assertEqual(H.cpu_model("processor: 0\nmodel name\t: Intel(R)  Core(TM) i5\n"), "Intel(R) Core(TM) i5")
        self.assertEqual(H.cpu_model("Hardware\t: BCM2835\n"), "BCM2835")
        self.assertEqual(H.os_name('NAME="Ubuntu"\nPRETTY_NAME="Ubuntu 24.04.3 LTS"\n'), "Ubuntu 24.04.3 LTS")

    def test_systemctl_show(self):
        units = H.parse_show(SHOW)
        self.assertEqual(set(units), {"minecraft-java.service", "redstonepanel-backup@minecraft-java.timer",
                                      "redstonepanel-restart@minecraft-java.timer", "redstonepanel.service"})
        self.assertEqual(H._int(units["minecraft-java.service"]["MemoryCurrent"]), 1 << 30)
        self.assertIsNone(H._int(units["redstonepanel.service"]["MemoryCurrent"]))
        self.assertIsNone(H._int(str(H.UNSET)))
        self.assertEqual(H._unix("@1900000000"), 1900000000)
        self.assertIsNone(H._unix(""))
        self.assertEqual(H._unix("Thu 2026-11-19 04:00:36 UTC"), 1795060836)
        self.assertEqual(H._unix("2026-11-19 04:00:36 UTC"), 1795060836)
        self.assertIsNone(H._unix("Thu 2026-11-19 04:00:36 EST"))

    def test_apt_policy(self):
        text = "redstonepanel:\n  Installed: 1.10.0\n  Candidate: 1.11.0\n  Version table:\n"
        self.assertEqual(H.parse_apt_policy(text), ("1.10.0", "1.11.0"))
        self.assertEqual(H.parse_apt_policy("redstonepanel:\n  Installed: (none)\n"), ("(none)", None))

    def test_ufw(self):
        self.assertTrue(H.ufw_allows(UFW, 25565, "tcp"))
        self.assertFalse(H.ufw_allows(UFW, 25565, "udp"))
        self.assertTrue(H.ufw_allows(UFW, 19133, "udp"))
        self.assertFalse(H.ufw_allows(UFW, 19134, "udp"))
        self.assertTrue(H.ufw_allows(UFW, 8080, "udp"))          # no protocol: both
        self.assertFalse(H.ufw_allows(UFW, 25566, "tcp"))
        self.assertFalse(H.ufw_allows("Status: inactive\n", 22, "tcp"))


class FolderSize(unittest.TestCase):
    def test_counts_files_without_following_symlinks(self):
        d = tempfile.mkdtemp(dir=TMP)
        outside = tempfile.mkdtemp(dir=TMP)
        with open(os.path.join(outside, "big"), "wb") as f:
            f.write(b"x" * 5000)
        os.makedirs(os.path.join(d, "world", "region"))
        with open(os.path.join(d, "world", "region", "r.0.0.mca"), "wb") as f:
            f.write(b"x" * 300)
        with open(os.path.join(d, "server.properties"), "wb") as f:
            f.write(b"x" * 20)
        try:
            os.symlink(outside, os.path.join(d, "link"))
        except (OSError, NotImplementedError):
            pass
        self.assertEqual(H.folder_size(d), 320)              # the linked folder isn't counted
        self.assertIsNone(H.folder_size(os.path.join(d, "missing")))


def _server(name="minecraft-java", **kw):
    s = {"name": name, "edition": "Java", "version": "1.21.4", "status": "online", "port": "25565",
         "directory": os.path.join(TMP, name), "ram": "2G", "java_required": 21, "java_installed": 21,
         "proxies": [], "transport": None}
    s.update(kw)
    return s


class Report(unittest.TestCase):
    def setUp(self):
        H._cache.clear()
        H._prev.clear()
        self.servers = [_server()]
        self.fw = {"installed": True, "active": True, "rules": UFW}
        self.failed = []
        self.disk_percent = 50.0
        patches = [
            mock.patch.object(H.manager, "list_servers", lambda: self.servers),
            mock.patch.object(H, "systemctl_show", lambda units, props=H.UNIT_PROPS: H.parse_show(SHOW)),
            mock.patch.object(H, "rates", lambda units: (12.5, {"eth0": {"rx": 10, "tx": 20, "rx_total": 1, "tx_total": 2}},
                                                         {"minecraft-java.service": 3.2})),
            mock.patch.object(H, "firewall", lambda: self.fw),
            mock.patch.object(H, "failed_units", lambda: self.failed),
            mock.patch.object(H.proxies, "find_phantoms", lambda: []),
            mock.patch.object(H.javaservers, "installed_javas", lambda: {21: "/usr/lib/jvm/java-21/bin/java"}),
            mock.patch.object(H, "panel_update", lambda: None),
            mock.patch.object(H, "java_players", lambda server, d: (2, 20)),
            mock.patch.object(H, "cached_size", lambda path: (12345, 1)),
            mock.patch.object(H, "memory", lambda text=None: {
                "total": 8 << 30, "available": 4 << 30, "used": 4 << 30, "percent": 50.0,
                "swap_total": 0, "swap_used": 0, "swap_percent": None}),
            mock.patch.object(H, "disks", lambda: [{"mount": "/", "fstype": "ext4", "holds": ["Game servers", "System"],
                                                    "total": 100 << 30, "used": 50 << 30,
                                                    "free": (100 - self.disk_percent) * (1 << 30),
                                                    "percent": self.disk_percent}]),
            mock.patch.object(H, "_ips", lambda: ["10.0.0.5"]),
            mock.patch.object(H, "https", lambda: {"mode": "off", "serving": None, "error": None, "cert_expires": None}),
            mock.patch.object(H.cluster, "state", lambda: {"role": "standalone"}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def report(self):
        H._cache.clear()
        return H.report()

    def test_report(self):
        r = self.report()
        s = r["servers"][0]
        self.assertEqual(r["cpu"]["percent"], 12.5)
        self.assertEqual((s["cpu_percent"], s["memory"], s["players"], s["max_players"]), (3.2, 1 << 30, 2, 20))
        self.assertEqual(s["next_backup"], 1900000000)
        self.assertIsNone(s["next_restart"])                     # timer not loaded
        self.assertTrue(s["firewall_open"])
        self.assertEqual(r["services"]["java"], [{"major": 21, "path": "/usr/lib/jvm/java-21/bin/java",
                                                  "servers": ["minecraft-java"]}])
        self.assertEqual(r["attention"], [])

    def test_attention(self):
        self.disk_percent = 96.0
        self.failed = ["minecraft-other.service"]
        self.servers = [_server(java_required=25, java_installed=21),
                        _server("minecraft-bedrock", edition="Bedrock", port="19140", status="offline")]
        r = self.report()
        texts = [a["text"] for a in r["attention"]]
        self.assertEqual(r["attention"][0]["level"], "error")
        self.assertTrue(any("96% full" in t for t in texts))
        self.assertTrue(any("minecraft-other.service has failed" in t for t in texts))
        self.assertTrue(any("needs Java 25" in t and "openjdk-25-jre-headless" in t for t in texts))
        self.assertTrue(any("doesn't allow minecraft-bedrock's port 19140" in t for t in texts))
        failed = next(a for a in r["attention"] if "has failed" in a["text"])
        self.assertEqual(failed["server"], "minecraft-other")

    def test_crash_loop_and_failed_backup(self):
        show = SHOW.replace("ActiveState=active\nSubState=running\nNRestarts=0",
                            "ActiveState=activating\nSubState=auto-restart\nNRestarts=7")
        with mock.patch.object(H, "systemctl_show", lambda units, props=H.UNIT_PROPS: H.parse_show(show)), \
             mock.patch.object(H.backups, "read_status", lambda s: {"ok": False, "at": 1, "error": "disk full"}):
            texts = [a["text"] for a in self.report()["attention"]]
        self.assertIn("minecraft-java keeps crashing (restarted 7 times).", texts)
        self.assertTrue(any("last backup of minecraft-java failed: disk full" in t for t in texts))

    def test_crash_loop_rule(self):
        self.assertTrue(H.crash_looping({"ActiveState": "activating", "SubState": "auto-restart", "NRestarts": "1"}))
        self.assertTrue(H.crash_looping({"ActiveState": "activating", "SubState": "start-post", "NRestarts": "9"}))
        self.assertFalse(H.crash_looping({"ActiveState": "activating", "SubState": "start", "NRestarts": "0"}))
        self.assertFalse(H.crash_looping({"ActiveState": "active", "SubState": "running", "NRestarts": "9"}))

    def test_certificate_expiring(self):
        soon = time.time() + 3 * 86400
        with mock.patch.object(H, "https", lambda: {"mode": "self_signed", "serving": "https", "error": None,
                                                    "cert_expires": soon}):
            texts = [a["text"] for a in self.report()["attention"]]
        self.assertTrue(any("HTTPS certificate expires in" in t for t in texts))

    def test_update_available(self):
        with mock.patch.object(H, "panel_update", lambda: "1.12.0"):
            r = self.report()
        self.assertEqual(r["attention"][-1]["level"], "info")
        self.assertIn("1.12.0", r["attention"][-1]["text"])

    def test_redacted(self):
        with mock.patch.object(H, "panel_update", lambda: "1.12.0"):
            self.report()
            r = H.report(redacted=True)
        self.assertTrue(r["redacted"])
        for key in ("ips", "version", "kernel", "update"):
            self.assertNotIn(key, r["host"])
        self.assertNotIn("directory", r["servers"][0])
        self.assertNotIn("https", r["services"])
        self.assertNotIn("path", r["services"]["java"][0])
        self.assertFalse([a for a in r["attention"] if a["where"] == "update"])

    def test_summary(self):
        s = H.summary(self.report())
        self.assertEqual((s["servers_online"], s["servers_total"], s["cpu_percent"]), (1, 1, 12.5))
        self.assertEqual(s["disk"]["mount"], "/")
        self.assertEqual(H.summary({})["servers_total"], 0)       # an odd answer from a node


class Rates(unittest.TestCase):
    def test_cpu_network_and_unit_rates(self):
        H._prev.clear()
        samples = iter([(100.0, (100, 1000), {"eth0": (0, 0)}, {"a.service": 0}),
                        (102.0, (400, 2000), {"eth0": (2000, 4000)}, {"a.service": 2_000_000_000})])
        with mock.patch.object(H, "_sample", lambda units: next(samples)), \
             mock.patch.object(H.time, "sleep", lambda s: None), \
             mock.patch.object(H.os, "cpu_count", lambda: 2):
            cpu, net, units = H.rates(["a.service"])
        self.assertEqual(cpu, 30.0)
        self.assertEqual((net["eth0"]["rx"], net["eth0"]["tx"]), (1000, 2000))
        self.assertEqual(units["a.service"], 50.0)                # 1 core-second per second of 2 cores


try:
    import fastapi  # noqa: F401
    HAVE_FASTAPI = True
except ImportError:
    HAVE_FASTAPI = False


@unittest.skipUnless(HAVE_FASTAPI, "needs fastapi")
class Api(unittest.TestCase):
    def test_trusted_visitors_get_the_redacted_report(self):
        import main
        calls = []
        with mock.patch.object(main.host, "report", lambda redacted=False: calls.append(redacted) or {}):
            main.host_api(SimpleNamespace(state=SimpleNamespace(user={"username": "local network",
                                                                      "source": "trusted", "role": "user"})))
            main.host_api(SimpleNamespace(state=SimpleNamespace(user={"username": "alex", "source": "panel",
                                                                      "role": "user"})))
        self.assertEqual(calls, [True, False])

    def test_the_master_may_read_a_nodes_host_report(self):
        from core import cluster
        self.assertTrue(cluster.path_allowed("GET", "/api/host"))
        self.assertFalse(cluster.path_allowed("POST", "/api/host"))


if __name__ == "__main__":
    unittest.main()
