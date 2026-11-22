"""Tests for the 1.20 server types (Purpur, Quilt, Forge, NeoForge): versions and builds from
faked download APIs, the installer run (a stand-in script, no Java), what the unit starts,
mods that fit, and update alerts. Nothing goes to the network.
Run: python -m unittest discover -s tests"""
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-loaders-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import backups as B, deployopts as D, javaservers as J, manager as M, mods as X, modrinth, updates as U  # noqa: E402
from core import catalog, featured  # noqa: E402

MANIFEST = {"versions": [{"id": v, "type": "release", "url": f"m/{v}"} for v in
                         ("26.3", "1.21.4", "1.21.1", "1.21", "1.20.4", "1.20.2", "1.20.1", "1.16.5", "1.12.2", "1.7.10")]}
JAVA = {"26.3": 25, "1.21.4": 21, "1.21.1": 21, "1.21": 21, "1.20.4": 17, "1.20.2": 17, "1.20.1": 17, "1.16.5": 8, "1.12.2": 8, "1.7.10": 8}
API = {
    J.MOJANG_MANIFEST: MANIFEST,
    **{f"m/{v}": {"javaVersion": {"majorVersion": j}, "downloads": {"server": {"url": f"dl/{v}", "sha1": "0" * 40}}}
       for v, j in JAVA.items()},
    J.PURPUR_API: {"versions": ["1.20.1", "1.21.4", "26.3", "1.21.5-pre1"]},
    f"{J.PURPUR_API}/1.21.4": {"builds": {"latest": "2416", "all": ["2414", "2415", "2416"]}},
    f"{J.PURPUR_API}/1.21.4/2416": {"result": "SUCCESS", "md5": "a" * 32},
    f"{J.PURPUR_API}/1.21.4/2415": {"result": "SUCCESS", "md5": "b" * 32},
    f"{J.PURPUR_API}/26.3": {"builds": {"latest": "2660", "all": ["2660"]}},
    f"{J.PURPUR_API}/26.3/2660": {"result": "FAILURE", "md5": "c" * 32},
    f"{J.PURPUR_API}/1.20.1": {"builds": {"latest": "2062", "all": ["2062"]}},
    f"{J.PURPUR_API}/1.20.1/2062": {"result": "SUCCESS", "md5": "d" * 32},
    f"{J.QUILT_META}/versions/game": [{"version": "26.4-snapshot-1", "stable": False},
                                      {"version": "1.21.4", "stable": True}, {"version": "1.14", "stable": True}],
    f"{J.QUILT_META}/versions/loader": [{"version": "0.31.0-beta.6"}, {"version": "0.30.2"}, {"version": "0.29.0"}],
    f"{J.QUILT_META}/versions/installer": [
        {"version": "0.15.1", "url": "https://maven.quiltmc.org/repository/release/org/quiltmc/quilt-installer/"
                                     "0.15.1/quilt-installer-0.15.1.jar", "hashes": {"sha256": "e" * 64}}],
    f"{J.FORGE_FILES}/maven-metadata.json": {
        "1.20.1": ["1.20.1-47.4.0", "1.20.1-47.4.28", "1.20.1-47.4.29"], "1.16.5": ["1.16.5-36.2.42"],
        "1.12.2": ["1.12.2-14.23.5.2859", "1.12.2-14.23.5.2864"], "1.7.10": ["1.7.10-10.13.4.1614-1.7.10"],
        "1.21.5-pre1": ["x"]},
    f"{J.FORGE_FILES}/promotions_slim.json": {"promos": {"1.20.1-recommended": "47.4.28", "1.20.1-latest": "47.4.29",
                                                         "1.12.2-recommended": "14.23.5.2859"}},
    J.NEOFORGE_VERSIONS: {"versions": ["0.25w14craftmine.3-beta", "20.2.3-beta", "20.4.237", "21.0.167", "21.1.9",
                                       "21.1.200", "21.1.201-beta", "26.3.0.1-beta", "26.3.0.115", "47.1.82"]},
}


def fake_text(url):
    if url.endswith(".sha1"):
        return "1" * 40 if "47.4.0" not in url else None
    if url.endswith(".sha256"):
        return "2" * 64 + "  neoforge-installer.jar"
    return None


class Base(unittest.TestCase):
    def setUp(self):
        self.jvm = tempfile.mkdtemp(dir=TMP)
        for p in (mock.patch.object(J, "_get_json", lambda url, cache=True: API.get(url)),
                  mock.patch.object(J, "_get_text", fake_text),
                  mock.patch.object(J, "JVM_ROOT", self.jvm),
                  mock.patch.object(J, "_as_minecraft", lambda cmd: cmd),   # CI runs as root, without that user
                  mock.patch.object(J, "packaged_javas", lambda refresh=False: J.UBUNTU_JAVA)):
            p.start()
            self.addCleanup(p.stop)

    def add_java(self, major):
        home = os.path.join(self.jvm, f"java-{major}")
        os.makedirs(os.path.join(home, "bin"))
        java = os.path.join(home, "bin", "java")
        with open(java, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(java, 0o755)
        with open(os.path.join(home, "release"), "w") as f:
            f.write(f'JAVA_VERSION="{major if major > 8 else "1.8.0_432"}"\n')
        return java


class Versions(Base):
    def test_every_type_lists_releases_newest_first(self):
        self.assertEqual(J.list_versions("purpur"), ["26.3", "1.21.4", "1.20.1"])
        self.assertEqual(J.list_versions("quilt"), ["1.21.4"])                   # nothing before 1.14.4
        self.assertEqual(J.list_versions("forge"), ["1.20.1", "1.16.5", "1.12.2"])   # nothing before 1.12.2
        self.assertEqual(J.list_versions("neoforge"), ["26.3", "1.21.1", "1.21", "1.20.4", "1.20.2"])
        self.assertEqual(set(J.LOADERS), {"vanilla", "paper", "purpur", "fabric", "quilt", "forge", "neoforge"})

    def test_neoforge_version_names(self):
        for version, mc in (("21.1.200", "1.21.1"), ("20.4.237", "1.20.4"), ("21.0.167", "1.21"),
                            ("20.2.3-beta", "1.20.2"), ("26.1.2.129", "26.1.2"), ("26.3.0.115", "26.3"),
                            ("0.25w14craftmine.3-beta", None), ("47.1.82", None), ("26.3.0", None), ("x", None)):
            self.assertEqual(J.neoforge_minecraft(version), mc, version)

    def test_purpur(self):
        t = J.resolve("purpur", "1.21.4")
        self.assertEqual((t["build"], t["md5"]), ("2416", "a" * 32))
        self.assertEqual(t["url"], f"{J.PURPUR_API}/1.21.4/2416/download")
        self.assertEqual(J.resolve("purpur", "1.21.4", build="2415")["md5"], "b" * 32)
        with self.assertRaises(J.JavaServerError):
            J.resolve("purpur", "1.21.4", build="9999")
        # The newest version's only build failed: the newest that can be downloaded
        self.assertEqual(J.resolve("purpur")["minecraft"], "1.21.4")
        self.assertEqual([b["build"] for b in J.list_builds("purpur", "1.21.4")], ["2416", "2415", "2414"])

    def test_forge_recommended_and_pinned(self):
        t = J.resolve("forge", "1.20.1")
        self.assertEqual((t["build"], t["sha1"], t["java_required"], t["java_max"]), ("47.4.28", "1" * 40, 17, None))
        self.assertEqual(t["installer_url"], f"{J.FORGE_MAVEN}/1.20.1-47.4.28/forge-1.20.1-47.4.28-installer.jar")
        self.assertEqual(J.resolve("forge", "1.20.1", build="47.4.29")["build"], "47.4.29")
        self.assertEqual(J.resolve("forge", "1.16.5")["build"], "36.2.42")       # no promotion: the newest
        with self.assertRaises(J.JavaServerError):
            J.resolve("forge", "1.20.1", build="1.0")
        with self.assertRaises(J.JavaServerError):                                # no checksum, no download
            J.resolve("forge", "1.20.1", build="47.4.0")
        with self.assertRaises(J.JavaServerError):
            J.resolve("forge", "1.7.10")
        labels = [b["label"] for b in J.list_builds("forge", "1.20.1")]
        self.assertEqual(labels, ["47.4.29", "47.4.28 (recommended)", "47.4.0"])

    def test_old_forge_needs_an_old_java(self):
        self.assertEqual((J.forge_java_max("1.12.2"), J.forge_java_max("1.16.5"), J.forge_java_max("1.17"),
                          J.forge_java_max("26.3")), (8, 11, None, None))
        self.assertEqual(J.resolve("forge", "1.12.2")["java_max"], 8)
        self.add_java(21)
        with self.assertRaises(J.MissingJavaError) as c:
            J.choose_java(8, "1.12.2", 8)
        self.assertEqual((c.exception.major, c.exception.package), (8, "openjdk-8-jre-headless"))
        java11 = self.add_java(11)
        self.assertEqual(J.choose_java(8, "1.16.5", 11), java11)
        with self.assertRaises(J.MissingJavaError):
            J.choose_java(8, "1.12.2", 8)
        with self.assertRaises(J.JavaServerError):
            J.choose_exact_java(21, 8, "1.12.2", 8)
        # A system that packages no Java that old (Debian 13) says so instead of offering Java 21
        with mock.patch.object(J, "packaged_javas", lambda refresh=False: (21, 25)):
            e = J.MissingJavaError(8, "1.12.2", 8)
        self.assertIsNone(e.major)
        self.assertIn("only runs on Java 8", str(e))

    def test_neoforge_stable_before_beta(self):
        t = J.resolve("neoforge", "1.21.1")
        self.assertEqual((t["build"], t["sha256"]), ("21.1.200", "2" * 64))
        self.assertEqual(t["installer_url"], f"{J.NEOFORGE_MAVEN}/21.1.200/neoforge-21.1.200-installer.jar")
        self.assertEqual(J.resolve("neoforge", "1.20.2")["build"], "20.2.3-beta")  # only betas exist
        self.assertEqual(J.resolve("neoforge")["build"], "26.3.0.115")
        self.assertEqual([b["build"] for b in J.list_builds("neoforge", "1.21.1")],
                         ["21.1.201-beta", "21.1.200", "21.1.9"])

    def test_quilt(self):
        t = J.resolve("quilt", "1.21.4")
        self.assertEqual((t["build"], t["sha256"]), ("loader 0.30.2", "e" * 64))    # never a beta loader
        self.assertEqual(J.resolve("quilt", "1.21.4", build="0.31.0-beta.6")["build"], "loader 0.31.0-beta.6")
        with self.assertRaises(J.JavaServerError):
            J.resolve("quilt", "1.21.4", build="9.9.9")
        self.assertEqual(J.list_builds("quilt", "")[0]["label"], "0.31.0-beta.6 (beta)")

    def test_builds_can_be_pinned_for_every_type_but_vanilla(self):
        for loader in ("paper", "purpur", "fabric", "quilt", "forge", "neoforge"):
            self.assertEqual(D.validate({"build": "1.2.3"}, "java", loader)["build"], "1.2.3")
        with self.assertRaises(D.OptionError):
            D.validate({"build": "1"}, "java", "vanilla")


# A stand-in for "java": it writes what the real installers leave behind, from its arguments
FAKE_JAVA = r"""#!/bin/sh
# args: -jar <installer> ...
shift 2
case "$1" in
  install)  # quilt: install server <mc> <loader> --install-dir=<d> --download-server
    d="${5#--install-dir=}"; mkdir -p "$d/libraries/org/quiltmc"
    echo launch > "$d/quilt-server-launch.jar"; echo vanilla > "$d/server.jar" ;;
  --install-server)  # neoforge
    [ -n "$FAIL" ] && { echo "Exception: boom"; exit 1; }
    mkdir -p "$2/libraries/net/neoforged/neoforge/$NAME"
    echo args > "$2/libraries/net/neoforged/neoforge/$NAME/unix_args.txt"
    echo run > "$2/run.sh"; echo opts > "$2/user_jvm_args.txt" ;;
  --installServer)  # forge
    mkdir -p "$2/libraries/net/minecraftforge"
    if [ -n "$OLD" ]; then echo jar > "$2/forge-$NAME.jar"; echo v > "$2/minecraft_server.1.12.2.jar"
    else mkdir -p "$2/libraries/net/minecraftforge/forge/$NAME"
         echo args > "$2/libraries/net/minecraftforge/forge/$NAME/unix_args.txt"; fi ;;
esac
"""


class Install(Base):
    def setUp(self):
        super().setUp()
        self.downloads = []

        def download(url, dest, sha1=None, sha256=None, md5=None):
            self.downloads.append((url, sha1, sha256, md5))
            with open(dest, "wb") as f:
                f.write(b"jar")
        p = mock.patch.object(J, "_download", download)
        p.start()
        self.addCleanup(p.stop)

    def fake_java(self, major):
        java = self.add_java(major)
        with open(java, "w") as f:
            f.write(FAKE_JAVA)
        return java

    def test_purpur_is_a_server_jar(self):
        java = self.add_java(21)
        d = tempfile.mkdtemp(dir=TMP)
        info, path = J.install(d, "purpur", "1.21.4")
        self.assertEqual((info["loader"], info["build"], path), ("purpur", "2416", java))
        self.assertEqual(self.downloads[0][3], "a" * 32)                            # checked by MD5
        self.assertEqual(J.launch_args(d), "-jar server.jar")
        self.assertTrue(J.runnable(d))

    def test_neoforge_installer_leaves_an_args_file(self):
        self.fake_java(21)
        d = tempfile.mkdtemp(dir=TMP)
        with mock.patch.dict(os.environ, {"NAME": "21.1.200"}):
            info, _ = J.install(d, "neoforge", "1.21.1")
        self.assertEqual(info["launch"], "@libraries/net/neoforged/neoforge/21.1.200/unix_args.txt")
        self.assertEqual(self.downloads[0][2], "2" * 64)
        self.assertEqual(J.launch_args(d), info["launch"])
        self.assertTrue(J.runnable(d))
        # The installer, its start scripts and its own JVM options are gone
        self.assertEqual(sorted(os.listdir(d)), [J.INFO_FILE, "libraries"])
        self.assertTrue(J.is_java_folder(d))
        self.assertEqual(J.sniff_folder(d)["build"], "21.1.200")

    def test_a_failed_installer_is_an_error_with_its_last_lines(self):
        self.fake_java(21)
        d = tempfile.mkdtemp(dir=TMP)
        with mock.patch.dict(os.environ, {"NAME": "21.1.200", "FAIL": "1"}):
            with self.assertRaises(J.JavaServerError) as c:
                J.install(d, "neoforge", "1.21.1")
        self.assertIn("boom", str(c.exception))
        self.assertFalse(os.path.exists(os.path.join(d, J.INSTALLER_JAR)))

    def test_forge_new_and_old(self):
        self.fake_java(17)
        d = tempfile.mkdtemp(dir=TMP)
        with mock.patch.dict(os.environ, {"NAME": "1.20.1-47.4.28"}):
            info, _ = J.install(d, "forge", "1.20.1")
        self.assertEqual(info["launch"], "@libraries/net/minecraftforge/forge/1.20.1-47.4.28/unix_args.txt")
        self.assertNotIn("java_max", info)
        java8 = self.fake_java(8)
        d = tempfile.mkdtemp(dir=TMP)
        with mock.patch.dict(os.environ, {"NAME": "1.12.2-14.23.5.2859", "OLD": "1"}):
            info, path = J.install(d, "forge", "1.12.2")
        self.assertEqual((info["launch"], info["java_max"], path), ("-jar forge-1.12.2-14.23.5.2859.jar", 8, java8))
        # A backup leaves out libraries/ and the loader's jars: not runnable until installed again
        self.assertTrue(B._skipped("forge-1.12.2-14.23.5.2859.jar"))
        self.assertTrue(B._skipped("minecraft_server.1.12.2.jar"))
        self.assertTrue(B._skipped("quilt-server-launch.jar"))
        self.assertFalse(B._skipped("mods/forge-1.12.2-14.23.5.2859.jar"))
        self.assertFalse(B._skipped(J.INFO_FILE))
        import shutil
        shutil.rmtree(os.path.join(d, "libraries"))
        self.assertFalse(J.runnable(d))

    def test_quilt(self):
        self.fake_java(21)
        d = tempfile.mkdtemp(dir=TMP)
        info, _ = J.install(d, "quilt", "1.21.4")
        self.assertEqual((info["launch"], info["build"]), ("-jar quilt-server-launch.jar", "loader 0.30.2"))
        self.assertTrue(J.runnable(d))
        os.remove(os.path.join(d, "server.jar"))
        self.assertFalse(J.runnable(d))

    def test_only_two_launch_shapes_reach_a_unit(self):
        d = tempfile.mkdtemp(dir=TMP)
        for launch, ok in (("@libraries/net/neoforged/neoforge/21.1.200/unix_args.txt", True),
                           ("-jar forge-1.12.2-14.23.5.2859.jar", True),
                           ("-jar x.jar nogui\nUser=root", False), ("@/etc/passwd", False),
                           ("@libraries/../../etc/unix_args.txt", False), ("-jar ../x.jar", False),
                           ("-jar a.jar -Dx=1", False), ("-cp evil Main", False), (7, False), (None, False)):
            with open(os.path.join(d, J.INFO_FILE), "w") as f:
                json.dump({"loader": "forge", "minecraft": "1.20.1", "launch": launch}, f)
            self.assertEqual(J.launch_args(d), launch if ok else "-jar server.jar", launch)
        # ...and only for the types that are installed that way
        with open(os.path.join(d, J.INFO_FILE), "w") as f:
            json.dump({"loader": "paper", "minecraft": "1.21.4", "launch": "-jar other.jar"}, f)
        self.assertEqual(J.launch_args(d), "-jar server.jar")

    def test_adopt_swaps_an_update_in(self):
        self.fake_java(17)
        d = tempfile.mkdtemp(dir=TMP)
        with mock.patch.dict(os.environ, {"NAME": "1.20.1-47.4.28"}):
            J.install(d, "forge", "1.20.1")
        os.makedirs(os.path.join(d, "mods"))
        open(os.path.join(d, "mods", "a.jar"), "w").close()
        staging = tempfile.mkdtemp(prefix=".redstonepanel-update-", dir=d)
        with mock.patch.dict(os.environ, {"NAME": "1.20.1-47.4.29"}):
            J.install(staging, "forge", "1.20.1", build="47.4.29")
        J.adopt(staging, d)
        self.assertEqual(J.launch_args(d), "@libraries/net/minecraftforge/forge/1.20.1-47.4.29/unix_args.txt")
        self.assertTrue(J.runnable(d))
        self.assertTrue(os.path.exists(os.path.join(d, "mods", "a.jar")))
        self.assertEqual(os.listdir(staging), [])


class Unit(unittest.TestCase):
    def test_unit_starts_what_the_install_recorded(self):
        root = tempfile.mkdtemp(dir=TMP)
        d = os.path.join(root, "minecraft-forge")
        os.makedirs(d)
        with open(os.path.join(d, J.INFO_FILE), "w") as f:
            json.dump({"loader": "neoforge", "minecraft": "1.21.1", "build": "21.1.200",
                       "launch": "@libraries/net/neoforged/neoforge/21.1.200/unix_args.txt"}, f)
        with mock.patch.object(M, "_unit_path", lambda n: os.path.join(root, f"{n}.service")), \
                mock.patch.object(M, "run_and_log", lambda *a, **k: None), \
                mock.patch.object(D, "FLAGS_FILE", os.path.join(root, "flags.json")):
            M._write_unit("minecraft-forge", d, "java", xms="2G", xmx="4G", java="/usr/lib/jvm/j21/bin/java", jvm={})
            with open(os.path.join(root, "minecraft-forge.service")) as f:
                unit = f.read()
        self.assertIn("ExecStart=/usr/lib/jvm/j21/bin/java -Xms2G -Xmx4G "
                      "@libraries/net/neoforged/neoforge/21.1.200/unix_args.txt nogui\n", unit)
        self.assertEqual(M.unit_heap(unit.split("ExecStart=")[1].splitlines()[0]), {"xms": "2G", "xmx": "4G"})
        # The server list reads its type without a server.jar
        v = M.server_version(d, True)
        self.assertEqual((v["loader"], v["minecraft"], v["version"]), ("neoforge", "1.21.1", "NeoForge 1.21.1"))
        self.assertEqual(M._removed_edition(d, "minecraft-forge", {}), "java")


def jar(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in entries.items():
            z.writestr(name, json.dumps(data) if isinstance(data, (dict, list)) else data)
    buf.seek(0)
    return buf


NEO_TOML = '''
modLoader = "javafml"
loaderVersion = "[4,)"
license = "MIT"
[[mods]]
modId = "examplemod"
version = "${file.jarVersion}"
displayName = "Example Mod"
authors = "Alex"
description = """
Does things.
"""
[[dependencies.examplemod]]
modId = "neoforge"
type = "required"
versionRange = "[21.1,)"
[[dependencies.examplemod]]
modId = "minecraft"
type = "required"
versionRange = "[1.21.1,1.22)"
[[dependencies.examplemod]]
modId = "architectury"
type = "required"
versionRange = "[13,)"
[[dependencies.examplemod]]
modId = "jei"
type = "optional"
versionRange = "[19,)"
'''
FORGE_TOML = '''
modLoader = "javafml"
[[mods]]
modId = "oldmod"
version = "2.0"
displayName = "Old Mod"
[[dependencies.oldmod]]
modId = "minecraft"
mandatory = true
versionRange = "[1.20.1]"
[[dependencies.oldmod]]
modId = "curios"
mandatory = true
[[dependencies.oldmod]]
modId = "patchouli"
mandatory = false
'''


class Mods(unittest.TestCase):
    def test_which_jars_each_type_loads(self):
        ok = [("fabric", "fabric"), ("quilt", "quilt"), ("quilt", "fabric"), ("forge", "forge"),
              ("neoforge", "neoforge"), ("paper", "bukkit"), ("purpur", "paper"), ("purpur", "bukkit")]
        for kind, jar_type in ok:
            self.assertIsNone(X.refusal(kind, jar_type, "1.21.1"), (kind, jar_type))
        for kind, jar_type in (("fabric", "quilt"), ("forge", "neoforge"), ("forge", "fabric"), ("neoforge", "fabric"),
                               ("purpur", "fabric"), ("quilt", "forge"), ("fabric", "forge"), ("paper", "neoforge")):
            self.assertTrue(X.refusal(kind, jar_type, "1.21.1"), (kind, jar_type))
        # NeoForge read Forge's mods.toml up to 1.20.4 only
        self.assertIsNone(X.refusal("neoforge", "forge", "1.20.4"))
        self.assertIn("only up to Minecraft 1.20.4", X.refusal("neoforge", "forge", "1.21.1"))
        self.assertIn("This is a Fabric mod, and this is a Forge server. Download the Forge version",
                      X.refusal("forge", "fabric"))
        self.assertIn("Purpur (or Bukkit) version", X.refusal("purpur", "fabric"))
        self.assertEqual({X.FOLDERS[k] for k in J.MOD_LOADERS}, {"mods"})
        self.assertEqual({X.FOLDERS[k] for k in J.PLUGIN_LOADERS}, {"plugins"})

    def test_neoforge_and_forge_metadata(self):
        m = X.inspect_jar(jar({"META-INF/neoforge.mods.toml": NEO_TOML,
                               "META-INF/MANIFEST.MF": "Manifest-Version: 1.0\nImplementation-Version: 3.4.5\n"}))
        self.assertEqual((m["type"], m["id"], m["name"], m["version"]), ("neoforge", "examplemod", "Example Mod", "3.4.5"))
        self.assertEqual((m["depends"], m["minecraft"], m["authors"], m["description"]),
                         (["architectury"], "[1.21.1,1.22)", ["Alex"], "Does things."))
        m = X.inspect_jar(jar({"META-INF/mods.toml": FORGE_TOML}))
        self.assertEqual((m["type"], m["id"], m["version"], m["depends"]), ("forge", "oldmod", "2.0", ["curios"]))
        m = X.inspect_jar(jar({"mcmod.info": [{"modid": "ancient", "name": "Ancient", "version": "1.0",
                                               "mcversion": "1.12.2", "authorList": ["Sam"]}]}))
        self.assertEqual((m["type"], m["id"], m["minecraft"], m["authors"]), ("forge", "ancient", "1.12.2", ["Sam"]))
        self.assertEqual(X.inspect_jar(jar({"META-INF/mods.toml": "not [ toml"}))["type"], "forge")
        m = X.inspect_jar(jar({"quilt.mod.json": {"schema_version": 1, "quilt_loader": {
            "id": "qmod", "version": "1.2", "metadata": {"name": "Q Mod", "contributors": {"Alex": "Owner"}},
            "depends": [{"id": "minecraft", "versions": ">=1.21"}, {"id": "quilt_loader"}, "qsl",
                        {"id": "optional_thing", "optional": True}]}}}))
        self.assertEqual((m["type"], m["id"], m["name"], m["depends"], m["minecraft"]),
                         ("quilt", "qmod", "Q Mod", ["qsl"], ">=1.21"))

    def test_forge_version_ranges(self):
        f = lambda decl, v: X.fits_minecraft("neoforge", decl, v)
        self.assertTrue(f("[1.21.1,1.22)", "1.21.1"))
        self.assertTrue(f("[1.21.1,1.22)", "1.21.4"))
        self.assertFalse(f("[1.21.1,1.22)", "1.22"))
        self.assertFalse(f("[1.21.1,1.22)", "1.21"))
        self.assertTrue(f("[1.20.1]", "1.20.1"))
        self.assertFalse(f("[1.20.1]", "1.20.2"))
        self.assertTrue(f("[1.20,)", "26.3"))
        self.assertFalse(f("(1.20,)", "1.20"))
        self.assertTrue(f("[1.19.2],[1.20.1,1.20.4]", "1.20.2"))
        self.assertIsNone(f("whatever", "1.20.1"))
        self.assertTrue(X.fits_minecraft("forge", "1.12.2", "1.12.2"))
        self.assertFalse(X.fits_minecraft("forge", "1.12.2", "1.16.5"))
        self.assertTrue(X.fits_minecraft("quilt", ">=1.21", "1.21.4"))

    def test_catalog_knows_every_type(self):
        for kind in ("fabric", "quilt", "forge", "neoforge", "paper", "purpur"):
            self.assertIn(kind, modrinth.LOADERS)
            self.assertIn(kind, catalog.SOURCES)
            self.assertTrue(featured.GROUPS[kind] and featured.BUNDLES[kind], kind)
        self.assertEqual(catalog.SOURCES["purpur"], ["modrinth", "hangar"])
        self.assertEqual(modrinth.LOADERS["quilt"], ["quilt", "fabric"])
        self.assertEqual(modrinth.project_url("purpur", "x"), "https://modrinth.com/plugin/x")
        card = modrinth.card({"loaders": ["fabric"], "game_versions": ["1.21.1"]}, "neoforge", "1.21.1")
        self.assertEqual((card["fits"], card["not_fit"]), (False, "No NeoForge build"))
        self.assertTrue(modrinth.card({"loaders": ["fabric"], "game_versions": ["1.21.1"]}, "quilt", "1.21.1")["fits"])


class Alerts(Base):
    def test_forge_and_neoforge_alert_only_for_a_newer_build_of_the_same_minecraft(self):
        up = U.java_update({"loader": "forge", "minecraft": "1.20.1", "build": "47.4.0"})
        self.assertEqual((up["kind"], up["label"], up["latest"]), ("build", "Forge 47.4.28", "1.20.1"))
        self.assertIsNone(U.java_update({"loader": "forge", "minecraft": "1.20.1", "build": "47.4.28"}))
        self.assertIsNone(U.java_update({"loader": "forge", "minecraft": "1.20.1", "build": "47.4.29"}))
        self.assertIsNone(U.java_update({"loader": "neoforge", "minecraft": "1.21.1", "build": "21.1.200"}))
        self.assertEqual(U.java_update({"loader": "neoforge", "minecraft": "1.21.1", "build": "21.1.9"})["label"],
                         "NeoForge 21.1.200")

    def test_purpur_and_quilt_alerts(self):
        self.assertEqual(U.java_update({"loader": "purpur", "minecraft": "1.21.4", "build": "2415"})["label"],
                         "Purpur 1.21.4 build 2416")
        self.assertEqual(U.java_update({"loader": "purpur", "minecraft": "1.20.1", "build": "2062"})["label"],
                         "Purpur 1.21.4")
        self.assertEqual(U.java_update({"loader": "quilt", "minecraft": "1.21.4", "build": "loader 0.29.0"})["label"],
                         "Quilt loader 0.30.2")
        self.assertIsNone(U.java_update({"loader": "quilt", "minecraft": "1.21.4", "build": "loader 0.30.2"}))


if __name__ == "__main__":
    unittest.main()
