"""Other systems than Ubuntu 24.04 on x86-64 (1.16.9): the Java versions a system packages
(Debian 13 has only 21 and 25), and Bedrock refused on ARM64 machines such as a Raspberry Pi,
where Mojang makes no Bedrock Dedicated Server. apt-cache and the machine type are faked.
Run: python -m unittest discover -s tests"""
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-platforms-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
os.environ.setdefault("RP_BACKUP_DIR", os.path.join(TMP, "backups"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import javaservers as J  # noqa: E402
from core import manager as M  # noqa: E402
from core import runtimes as R  # noqa: E402
from core import transfer as T  # noqa: E402

# `apt-cache pkgnames openjdk-` on Debian 13 (trimmed) and on Ubuntu 26.10
DEBIAN_13 = """openjdk-21-jre openjdk-25-jdk-headless openjdk-21-jre-headless openjdk-25-jre-headless
openjdk-21-jdk openjdk-25-jre openjdk-21-jre-zero"""
UBUNTU_2610 = """openjdk-8-jre-headless openjdk-11-jre-headless openjdk-17-jre-headless openjdk-21-jre-headless
openjdk-25-jre-headless openjdk-27-jre-headless openjdk-28-jre-headless openjdk-28-jdk"""


class PackagedJava(unittest.TestCase):
    def setUp(self):
        self.out = DEBIAN_13
        self.calls = []

        def run(cmd, **kw):
            self.calls.append(cmd)
            if isinstance(self.out, Exception):
                raise self.out
            return SimpleNamespace(returncode=0, stdout=self.out, stderr="")
        p = mock.patch.object(J.subprocess, "run", run)
        p.start()
        self.addCleanup(p.stop)
        J._packaged.update(at=0.0, majors=None)
        self.addCleanup(J._packaged.update, at=0.0, majors=None)

    def test_reads_what_apt_has(self):
        self.assertEqual(J.packaged_javas(), (21, 25))
        self.assertEqual(self.calls, [["apt-cache", "pkgnames", "openjdk-"]])
        J.packaged_javas()
        self.assertEqual(len(self.calls), 1, "cached")
        self.out = UBUNTU_2610
        self.assertEqual(J.packaged_javas(refresh=True), (8, 11, 17, 21, 25, 27, 28))

    def test_falls_back_to_the_ubuntu_list(self):
        self.out = ""
        self.assertEqual(J.packaged_javas(), J.UBUNTU_JAVA)
        self.out = OSError("no apt-cache")
        self.assertEqual(J.packaged_javas(refresh=True), J.UBUNTU_JAVA)

    def test_debian_13_gets_the_closest_newer_java(self):
        self.assertEqual(J.java_package(8), "openjdk-21-jre-headless")
        self.assertEqual(J.java_package(17), "openjdk-21-jre-headless")
        self.assertEqual(J.java_package(25), "openjdk-25-jre-headless")
        self.assertEqual(J.java_major_for(16), 21)
        self.assertIsNone(J.java_major_for(26))

    def test_missing_java_says_when_the_system_has_none_new_enough(self):
        e = J.MissingJavaError(26, "27.1")
        self.assertIsNone(e.major)
        self.assertIn("no Java 26 or newer (they have: 21, 25)", str(e))
        self.assertNotIn("apt install", str(e))
        e = J.MissingJavaError(17, "1.20.4")
        self.assertEqual((e.major, e.package), (21, "openjdk-21-jre-headless"))
        self.assertIn("sudo apt install openjdk-21-jre-headless", str(e))

    def test_requirement_names_what_would_be_installed(self):
        with mock.patch.object(J, "mojang_version", lambda v: {"java_required": 8}), \
                mock.patch.object(J, "installed_javas", lambda: {}):
            r = J.requirement("1.16.5")
        self.assertEqual((r["java_required"], r["java_used"], r["package_major"], r["packaged"]), (8, None, 21, [21, 25]))

    def test_runtimes_offer_what_the_system_packages(self):
        with mock.patch.object(R.javaservers, "installed_javas", lambda: {21: "/usr/lib/jvm/java-21/bin/java"}), \
                mock.patch.object(R.manager, "list_servers", lambda: []), \
                mock.patch.object(R, "_default_major", lambda: 21):
            st = R.status(packages=False)
        self.assertEqual([i["major"] for i in st["installable"]], [25])
        self.assertEqual(R.package_for(25), "openjdk-25-jre-headless")
        self.assertEqual(R.package_for(17), "openjdk-17-jre-headless", "Ubuntu's list is still accepted; apt decides")
        with self.assertRaisesRegex(R.RuntimeJobError, r"\(21, 25\)"):
            R.package_for(22)


class BedrockOnArm(unittest.TestCase):
    def setUp(self):
        self.opt = tempfile.mkdtemp(dir=TMP)
        self.machine = "aarch64"
        self.made = []
        patches = [
            mock.patch.object(M.platform, "machine", lambda: self.machine),
            mock.patch.object(M, "SERVERS_ROOT", self.opt),
            mock.patch.object(M, "_reserve_name", lambda edition: self.made.append(edition) or ("minecraft-bedrock", 19132)),
            mock.patch.object(M, "_release_name", lambda name: None),
            mock.patch.object(M, "require_tools", lambda *a: None),
            mock.patch.object(M, "_fetch_bedrock", lambda d: self.fail("downloaded Bedrock on ARM64")),
            mock.patch.object(M, "ensure_minecraft_user", lambda d: None),
            mock.patch.object(M, "run_and_log", lambda *a, **k: 0),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_machines(self):
        self.assertFalse(M.bedrock_supported())
        self.assertTrue(M.bedrock_supported("x86_64"))
        self.assertTrue(M.bedrock_supported("AMD64"))
        self.assertFalse(M.bedrock_supported("armv7l"))

    def test_deploy_is_refused_before_anything_is_made(self):
        with self.assertRaisesRegex(M.ServerError, r"aarch64.*x86-64.*Geyser"):
            M.deploy_server("bedrock")
        self.assertEqual(self.made, [])
        self.assertEqual(os.listdir(self.opt), [])

    def test_import_of_a_bedrock_backup_is_refused(self):
        from core import archive
        staging = tempfile.mkdtemp(dir=TMP)
        open(os.path.join(staging, "bedrock_server"), "w").close()
        with self.assertRaisesRegex(archive.ArchiveError, "x86-64"):
            M._import_unpacked(staging, staging)
        self.assertEqual(self.made, [])

    def test_a_bedrock_server_cant_move_here(self):
        with mock.patch.object(T.platform, "machine", lambda: "aarch64"):
            with self.assertRaisesRegex(T.TransferError, "x86-64"):
                T._clean_meta({"edition": "bedrock", "name": "minecraft-bedrock"})
            self.assertEqual(T._clean_meta({"edition": "java", "name": "minecraft-java"})["edition"], "java")

    def test_x86_64_still_deploys(self):
        self.machine = "x86_64"
        with mock.patch.object(M, "_fetch_bedrock", lambda d: (_ for _ in ()).throw(M.ServerError("download stub"))):
            with self.assertRaisesRegex(M.ServerError, "download stub"):
                M.deploy_server("bedrock")
        self.assertEqual(self.made, ["bedrock"])


class DefaultHeap(unittest.TestCase):
    def meminfo(self, kb):
        path = os.path.join(tempfile.mkdtemp(dir=TMP), "meminfo")
        with open(path, "w") as f:
            f.write(f"MemTotal:       {kb} kB\nMemFree:         1000 kB\n")
        return path

    def test_small_machines_get_a_smaller_heap(self):
        self.assertEqual(M.default_heap(self.meminfo(1_900_000)), {"xms": "512M", "xmx": "1G"})   # 2 GB Pi
        self.assertEqual(M.default_heap(self.meminfo(3_900_000)), {"xms": "1G", "xmx": "2G"})     # 4 GB Pi
        self.assertEqual(M.default_heap(self.meminfo(16_000_000)), {"xms": "1G", "xmx": "2G"})
        self.assertEqual(M.default_heap("/nonexistent"), {"xms": "1G", "xmx": "2G"})


if __name__ == "__main__":
    unittest.main()
