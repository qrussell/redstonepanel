"""Tests for core/javaservers.py: versions, downloads and Java selection. The download APIs
and /usr/lib/jvm are faked; nothing goes to the network.
Run: python -m unittest discover -s tests"""
import hashlib
import json
import os
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-java-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import javaservers as J  # noqa: E402

JAR = b"fake server jar"
SHA1 = hashlib.sha1(JAR).hexdigest()
SHA256 = hashlib.sha256(JAR).hexdigest()

MANIFEST = {"versions": [
    {"id": "26.4-snapshot-1", "type": "snapshot", "url": "m/snap"},
    {"id": "26.3", "type": "release", "url": "m/26.3"},
    {"id": "1.21.10", "type": "release", "url": "m/1.21.10"},
    {"id": "1.21.4", "type": "release", "url": "m/1.21.4"},
    {"id": "1.16.5", "type": "release", "url": "m/1.16.5"},
]}
API = {
    J.MOJANG_MANIFEST: MANIFEST,
    "m/26.3": {"javaVersion": {"majorVersion": 25}, "downloads": {"server": {"url": "dl/26.3", "sha1": SHA1}}},
    "m/1.21.10": {"javaVersion": {"majorVersion": 21}, "downloads": {"server": {"url": "dl/1.21.10", "sha1": SHA1}}},
    "m/1.21.4": {"javaVersion": {"majorVersion": 21}, "downloads": {"server": {"url": "dl/1.21.4", "sha1": SHA1}}},
    "m/1.16.5": {"javaVersion": {"majorVersion": 8}, "downloads": {"server": {"url": "dl/1.16.5", "sha1": SHA1}}},
    J.PAPER_API: {"versions": {"26.3": ["26.3", "26.3-rc-3"], "1.21": ["1.21.10", "1.21.4"], "1.16": ["1.16.5"]}},
    f"{J.PAPER_API}/versions/26.3/builds": [
        {"id": 3, "channel": "BETA", "downloads": {"server:default": {"url": "dl/paper-26.3-3", "checksums": {"sha256": SHA256}}}}],
    f"{J.PAPER_API}/versions/1.21.10/builds": [
        {"id": 40, "channel": "STABLE", "downloads": {"server:default": {"url": "dl/paper-40", "checksums": {"sha256": SHA256}}}},
        {"id": 41, "channel": "BETA", "downloads": {"server:default": {"url": "dl/paper-41", "checksums": {"sha256": SHA256}}}}],
    f"{J.FABRIC_META}/versions/game": [{"version": "26.4-snapshot-1", "stable": False},
                                       {"version": "26.3", "stable": True}, {"version": "1.21.4", "stable": True}],
    f"{J.FABRIC_META}/versions/loader": [{"version": "0.20.2", "stable": False}, {"version": "0.20.1", "stable": True}],
    f"{J.FABRIC_META}/versions/installer": [{"version": "1.1.2", "stable": True}],
}


class Base(unittest.TestCase):
    def setUp(self):
        self.downloads = []
        self.jvm = tempfile.mkdtemp(dir=TMP)
        def download(url, dest, sha1=None, sha256=None):
            self.downloads.append((url, sha1, sha256))
            with open(dest, "wb") as f:
                f.write(JAR)
        for p in (mock.patch.object(J, "_get_json", lambda url, cache=True: API.get(url)),
                  mock.patch.object(J, "_download", download),
                  mock.patch.object(J, "JVM_ROOT", self.jvm),
                  # Ubuntu 24.04's Java versions, whatever apt on the test machine has
                  mock.patch.object(J, "packaged_javas", lambda refresh=False: J.UBUNTU_JAVA)):
            p.start()
            self.addCleanup(p.stop)

    def add_java(self, dirname, version):
        home = os.path.join(self.jvm, dirname)
        os.makedirs(os.path.join(home, "bin"))
        java = os.path.join(home, "bin", "java")
        with open(java, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(java, 0o755)
        with open(os.path.join(home, "release"), "w") as f:
            f.write(f'IMPLEMENTOR="Ubuntu"\nJAVA_VERSION="{version}"\n')
        return java


class Versions(Base):
    def test_lists_releases_only(self):
        self.assertEqual(J.list_versions("vanilla"), ["26.3", "1.21.10", "1.21.4", "1.16.5"])
        self.assertEqual(J.list_versions("paper"), ["26.3", "1.21.10", "1.21.4", "1.16.5"])
        self.assertEqual(J.list_versions("fabric"), ["26.3", "1.21.4"])
        with self.assertRaises(J.JavaServerError):
            J.list_versions("forge")

    def test_version_order(self):
        self.assertLess(J.version_key("1.21.4"), J.version_key("1.21.10"))
        self.assertLess(J.version_key("1.21.10"), J.version_key("26.3"))

    def test_vanilla_newest(self):
        t = J.resolve("vanilla")
        self.assertEqual((t["minecraft"], t["url"], t["sha1"], t["java_required"]), ("26.3", "dl/26.3", SHA1, 25))

    def test_paper_skips_versions_without_a_stable_build(self):
        t = J.resolve("paper")
        self.assertEqual((t["minecraft"], t["build"], t["url"]), ("1.21.10", "40", "dl/paper-40"))
        self.assertEqual(t["sha256"], SHA256)
        with self.assertRaisesRegex(J.JavaServerError, "no stable build"):
            J.resolve("paper", "26.3")

    def test_fabric_uses_stable_loader_and_installer(self):
        t = J.resolve("fabric", "1.21.4")
        self.assertEqual(t["url"], f"{J.FABRIC_META}/versions/loader/1.21.4/0.20.1/1.1.2/server/jar")
        self.assertEqual(t["java_required"], 21)

    def test_rejects_bad_versions(self):
        for v in ("1.21.4; rm -rf /", "26.4-snapshot-1", "../x"):
            with self.assertRaises(J.JavaServerError, msg=v):
                J.resolve("vanilla", v)
        with self.assertRaisesRegex(J.JavaServerError, "doesn't offer"):
            J.resolve("fabric", "1.16.5")


class Java(Base):
    def test_exact_major_else_closest_newer(self):
        j21 = self.add_java("java-21-openjdk-amd64", "21.0.5")
        j25 = self.add_java("java-25-openjdk-amd64", "25.0.1")
        j8 = self.add_java("java-8-openjdk-amd64", "1.8.0_432")
        os.symlink(os.path.join(self.jvm, "java-21-openjdk-amd64"), os.path.join(self.jvm, "java-1.21.0-openjdk-amd64"))
        self.assertEqual(J.installed_javas(), {8: j8, 21: j21, 25: j25})
        self.assertEqual(J.choose_java(21), j21)
        self.assertEqual(J.choose_java(17), j21)
        self.assertEqual(J.choose_java(8), j8)
        self.assertEqual(J.java_major_of(j25), 25)

    def test_missing_java_names_the_package(self):
        self.add_java("java-21-openjdk-amd64", "21.0.5")
        with self.assertRaises(J.MissingJavaError) as cm:
            J.choose_java(25)
        self.assertIn("sudo apt install openjdk-25-jre-headless", str(cm.exception))

    def test_package_for_versions_ubuntu_lacks(self):
        self.assertEqual(J.java_package(16), "openjdk-17-jre-headless")
        self.assertEqual(J.java_package(21), "openjdk-21-jre-headless")

    def test_requirement(self):
        self.add_java("java-25-openjdk-amd64", "25.0.1")
        r = J.requirement("1.21.4")
        self.assertEqual((r["java_required"], r["java_used"], r["package"]), (21, 25, "openjdk-21-jre-headless"))


class Install(Base):
    def test_install_records_what_it_downloaded(self):
        java = self.add_java("java-21-openjdk-amd64", "21.0.5")
        d = tempfile.mkdtemp(dir=TMP)
        info, path = J.install(d, "paper", "1.21.10")
        self.assertEqual(path, java)
        self.assertEqual(self.downloads, [("dl/paper-40", None, SHA256)])
        with open(os.path.join(d, "server.jar"), "rb") as f:
            self.assertEqual(f.read(), JAR)
        self.assertEqual(J.read_info(d), {"loader": "paper", "minecraft": "1.21.10", "build": "40", "java_required": 21})
        self.assertEqual(J.describe(J.read_info(d)), "Paper 1.21.10")

    def test_checks_java_before_downloading(self):
        self.add_java("java-21-openjdk-amd64", "21.0.5")
        with self.assertRaises(J.MissingJavaError):
            J.install(tempfile.mkdtemp(dir=TMP), "vanilla")          # 26.3 needs Java 25
        self.assertEqual(self.downloads, [])

    def test_refuses_a_downgrade(self):
        self.add_java("java-25-openjdk-amd64", "25.0.1")
        with self.assertRaisesRegex(J.JavaServerError, "can't be moved back"):
            J.install(tempfile.mkdtemp(dir=TMP), "vanilla", "1.21.4", current={"loader": "vanilla", "minecraft": "26.3"})
        self.assertEqual(self.downloads, [])
        J.install(tempfile.mkdtemp(dir=TMP), "vanilla", "26.3", current={"loader": "vanilla", "minecraft": "26.3"})


class Sniff(unittest.TestCase):
    def jar(self, files):
        path = os.path.join(tempfile.mkdtemp(dir=TMP), "server.jar")
        with zipfile.ZipFile(path, "w") as z:
            for name, data in files.items():
                z.writestr(name, data)
        return path

    def test_tells_jars_apart(self):
        self.assertEqual(J.sniff_jar(self.jar({"version.json": json.dumps({"id": "1.21.4", "java_version": 21})})),
                         {"loader": "vanilla", "minecraft": "1.21.4", "java_required": 21})
        self.assertEqual(J.sniff_jar(self.jar({"io/papermc/paperclip/Main.class": "", "version.json": '{"id": "1.21.4"}'})),
                         {"loader": "paper", "minecraft": "1.21.4"})
        self.assertEqual(J.sniff_jar(self.jar({"install.properties": "fabric-loader-version=0.20.1\ngame-version=1.21.4\n"})),
                         {"loader": "fabric", "minecraft": "1.21.4", "build": "0.20.1"})
        self.assertEqual(J.sniff_jar(os.path.join(TMP, "missing.jar")), {})


if __name__ == "__main__":
    unittest.main()
