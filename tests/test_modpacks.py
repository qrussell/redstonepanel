"""Tests for core/modpacks.py (1.20): reading a Modrinth pack, what it may and may not bring,
working out what a server pack needs, making a server from a pack and updating it. Downloads,
the loader install and systemctl are stubbed; servers live in a temp folder.
Run: python -m unittest discover -s tests"""
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-modpacks-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import backups as B, files as F, javaservers as J, manager as M, modpacks as P, modrinth  # noqa: E402
from core.sources import SourceError  # noqa: E402

CDN = "https://cdn.modrinth.com/data/x/versions/y/"


def sha512(data):
    return hashlib.sha512(data).hexdigest()


def entry(path, data=b"jar", server="required", url=None, **extra):
    e = {"path": path, "hashes": {"sha512": sha512(data), "sha1": hashlib.sha1(data).hexdigest()},
         "downloads": [url or CDN + os.path.basename(path)], "fileSize": len(data)}
    if server:
        e["env"] = {"client": "required", "server": server}
    e.update(extra)
    return e


def mrpack(files=None, deps=None, extra=None, index=True, **top):
    """A .mrpack on disk; returns its path."""
    idx = {"formatVersion": 1, "game": "minecraft", "versionId": "1.2.0", "name": "Test Pack",
           "files": files if files is not None else [entry("mods/a.jar"), entry("mods/b.jar")],
           "dependencies": deps if deps is not None else {"minecraft": "1.21.1", "neoforge": "21.1.200"}}
    idx.update(top)
    fd, path = tempfile.mkstemp(dir=TMP, suffix=".mrpack")
    with os.fdopen(fd, "wb") as f, zipfile.ZipFile(f, "w") as z:
        if index:
            z.writestr("modrinth.index.json", json.dumps(idx))
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return path


class ReadMrpack(unittest.TestCase):
    def test_a_good_pack(self):
        plan = P.read_mrpack(mrpack(files=[entry("mods/a.jar"), entry("mods/shaders.jar", server="unsupported"),
                                           entry("config/x.toml", server="optional"), entry("mods/noenv.jar", server=None)]))
        self.assertEqual((plan["name"], plan["version"], plan["minecraft"], plan["loader"], plan["loader_version"]),
                         ("Test Pack", "1.2.0", "1.21.1", "neoforge", "21.1.200"))
        self.assertEqual([f["path"] for f in plan["files"]], ["mods/a.jar", "config/x.toml", "mods/noenv.jar"])
        self.assertEqual(plan["client_only"], 1)
        self.assertEqual(plan["files"][0]["algorithm"], "sha512")

    def test_loaders(self):
        for dep, loader in (("fabric-loader", "fabric"), ("quilt-loader", "quilt"), ("forge", "forge")):
            self.assertEqual(P.read_mrpack(mrpack(deps={"minecraft": "1.20.1", dep: "1.0.0"}))["loader"], loader)
        self.assertEqual(P.read_mrpack(mrpack(files=[], deps={"minecraft": "1.20.1"}))["loader"], "vanilla")
        for deps in ({"minecraft": "1.20.1", "forge": "1", "fabric-loader": "2"}, {"forge": "47.2.0"},
                     {"minecraft": "24w14a", "forge": "1"}, {"minecraft": "1.20.1", "forge": "1; rm -rf /"}):
            with self.assertRaises(P.PackError, msg=deps):
                P.read_mrpack(mrpack(deps=deps))

    def test_not_a_pack(self):
        fd, path = tempfile.mkstemp(dir=TMP)
        os.write(fd, b"not a zip")
        os.close(fd)
        with self.assertRaises(P.PackError):
            P.read_mrpack(path)
        with self.assertRaises(P.PackError) as c:
            P.read_mrpack(mrpack(index=False, extra={"mods/a.jar": "x"}))
        self.assertIn("server pack", str(c.exception))
        for top in ({"formatVersion": 2}, {"game": "terraria"}):
            with self.assertRaises(P.PackError):
                P.read_mrpack(mrpack(**top))

    def test_paths_that_leave_the_folder_are_refused(self):
        for bad in ("../x.jar", "/etc/cron.d/x", "mods/../../x", "C:/x", "mods\\x.jar", "", "mods//x", "a/./b", 7):
            with self.assertRaises(P.PackError, msg=bad):
                P.read_mrpack(mrpack(files=[entry("mods/a.jar") | {"path": bad}]))

    def test_server_software_and_scripts_are_left_out(self):
        plan = P.read_mrpack(mrpack(files=[entry("server.jar"), entry("start.sh"), entry("libraries/x/y.jar"),
                                           entry(".redstonepanel-server.json"), entry("evil.jar"),
                                           entry("mods/a.jar"), entry("mods/A.jar"), entry("eula.txt")]))
        self.assertEqual([f["path"] for f in plan["files"]], ["mods/a.jar"])
        self.assertEqual(len(plan["refused"]), 7)

    def test_only_allowed_hosts_and_checked_files(self):
        for url in ("http://cdn.modrinth.com/x.jar", "https://evil.example/x.jar", "https://cdn.modrinth.com.evil.io/x",
                    "https://user@cdn.modrinth.com/x.jar", "file:///etc/passwd"):
            with self.assertRaises(P.PackError, msg=url):
                P.read_mrpack(mrpack(files=[entry("mods/a.jar", url=url)]))
        ok = P.read_mrpack(mrpack(files=[entry("mods/a.jar", url="https://github.com/o/r/releases/download/v/a.jar")]))
        self.assertEqual(len(ok["files"]), 1)
        with self.assertRaises(P.PackError):
            P.read_mrpack(mrpack(files=[entry("mods/a.jar") | {"hashes": {}}]))
        with self.assertRaises(P.PackError):
            P.read_mrpack(mrpack(files=[entry("mods/a.jar") | {"hashes": {"sha512": "zz"}}]))
        with self.assertRaises(P.PackError):
            P.read_mrpack(mrpack(files=[entry(f"mods/{i}.jar") for i in range(P.MAX_PACK_FILES + 1)]))


class Stage(unittest.TestCase):
    def setUp(self):
        self.served = {}
        self.asked = []

        def download(url, allowed, algorithm, expected, size=None):
            self.asked.append((url, algorithm))
            data = self.served.get(url)
            if data is None:
                raise SourceError("gone", 502)
            if hashlib.new(algorithm, data).hexdigest() != expected:
                raise SourceError("didn't match its checksum", 502)
            return io.BytesIO(data)
        p = mock.patch.object(P, "download", download)
        p.start()
        self.addCleanup(p.stop)

    def test_downloads_and_overrides(self):
        self.served = {CDN + "a.jar": b"mod a", CDN + "x.toml": b"x=1"}
        path = mrpack(files=[entry("mods/a.jar", b"mod a"), entry("config/x.toml", b"x=1")],
                      extra={"overrides/config/y.toml": "y=1", "overrides/config/z.toml": "client z",
                             "server-overrides/config/z.toml": "server z", "client-overrides/options.txt": "fov",
                             "overrides/server.properties": "motd=Pack\nserver-port=1234\nenable-rcon=true\n"
                                                            "rcon.password=hunter2\ndifficulty=hard\n",
                             "overrides/start.sh": "rm -rf /", "overrides/server.jar": "x",
                             "overrides/libraries/a.jar": "x", "overrides/../../escape": "x"})
        staging = tempfile.mkdtemp(dir=TMP)
        with self.assertRaises(P.PackError):                    # the entry that tries to leave the folder
            P.stage_mrpack(path, P.read_mrpack(path), staging)
        path = mrpack(files=[entry("mods/a.jar", b"mod a"), entry("config/x.toml", b"x=1")],
                      extra={"overrides/config/y.toml": "y=1", "overrides/config/z.toml": "client z",
                             "server-overrides/config/z.toml": "server z", "client-overrides/options.txt": "fov",
                             "overrides/server.properties": "motd=Pack\nserver-port=1234\nenable-rcon=true\n"
                                                            "rcon.password=hunter2\ndifficulty=hard\n",
                             "overrides/start.sh": "rm -rf /", "overrides/server.jar": "x",
                             "overrides/libraries/a.jar": "x"})
        staging = tempfile.mkdtemp(dir=TMP)
        staged = P.stage_mrpack(path, P.read_mrpack(path), staging)
        self.assertEqual(staged["files"], ["config/x.toml", "mods/a.jar"])
        self.assertEqual(staged["overrides"], ["config/y.toml", "config/z.toml"])
        self.assertEqual(staged["properties"], {"motd": "Pack", "difficulty": "hard"})   # no port, no RCON
        found = sorted(os.path.relpath(os.path.join(r, f), staging) for r, _, fs in os.walk(staging) for f in fs)
        self.assertEqual(found, ["config/x.toml", "config/y.toml", "config/z.toml", "mods/a.jar"])
        with open(os.path.join(staging, "config", "z.toml")) as f:
            self.assertEqual(f.read(), "server z")                                  # server-overrides win

    def test_a_file_that_fails_its_hash_stops_the_install(self):
        self.served = {CDN + "a.jar": b"tampered"}
        path = mrpack(files=[entry("mods/a.jar", b"mod a")])
        with self.assertRaises(P.PackError) as c:
            P.stage_mrpack(path, P.read_mrpack(path), tempfile.mkdtemp(dir=TMP))
        self.assertIn("mods/a.jar", str(c.exception))

    def test_second_address_is_tried(self):
        self.served = {CDN + "mirror.jar": b"mod a"}
        e = entry("mods/a.jar", b"mod a")
        e["downloads"] = [CDN + "a.jar", CDN + "mirror.jar"]
        path = mrpack(files=[e])
        staging = tempfile.mkdtemp(dir=TMP)
        P.stage_mrpack(path, P.read_mrpack(path), staging)
        self.assertTrue(os.path.isfile(os.path.join(staging, "mods", "a.jar")))


def tree(spec):
    root = tempfile.mkdtemp(dir=TMP)
    for rel, data in spec.items():
        p = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        if rel.endswith("/"):
            os.makedirs(p, exist_ok=True)
            continue
        with open(p, "wb") as f:
            f.write(data if isinstance(data, bytes) else data.encode())
    return root


def modjar(kind="forge"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr({"forge": "META-INF/mods.toml", "neoforge": "META-INF/neoforge.mods.toml",
                    "fabric": "fabric.mod.json"}[kind], "{}" if kind == "fabric" else "")
    return buf.getvalue()


class ServerPack(unittest.TestCase):
    def test_what_names_the_loader(self):
        cases = [
            ({"libraries/net/minecraftforge/forge/1.20.1-47.2.0/unix_args.txt": "x"}, ("forge", "47.2.0", "1.20.1")),
            ({"libraries/net/neoforged/neoforge/21.1.200/x": "x"}, ("neoforge", "21.1.200", "1.21.1")),
            ({"forge-1.12.2-14.23.5.2860.jar": "x", "minecraft_server.1.12.2.jar": "x"},
             ("forge", "14.23.5.2860", "1.12.2")),
            ({"forge-1.16.5-36.2.39-installer.jar": "x"}, ("forge", "36.2.39", "1.16.5")),
            ({"neoforge-21.1.73-installer.jar": "x"}, ("neoforge", "21.1.73", "1.21.1")),
            ({"fabric-server-mc.1.20.1-loader.0.15.7-launcher.1.0.0.jar": "x"}, ("fabric", "0.15.7", "1.20.1")),
            ({"variables.txt": 'MINECRAFT_VERSION=1.20.1\nMODLOADER=Forge\nMODLOADER_VERSION=47.3.0\nJAVA_ARGS="-Xmx4G"\n'},
             ("forge", "47.3.0", "1.20.1")),
            ({"manifest.json": json.dumps({"minecraft": {"version": "1.19.2", "modLoaders": [{"id": "forge-43.3.0"}]}})},
             ("forge", "43.3.0", "1.19.2")),
            ({"startserver.sh": "#!/bin/bash\nFORGE_VERSION=47.2.20\nMINECRAFT_VERSION=1.20.1\njava -jar x\n"},
             ("forge", "47.2.20", "1.20.1")),
            ({"start.bat": "set NEOFORGE_VERSION=21.1.90\r\njava @user_jvm_args.txt\r\n"}, ("neoforge", "21.1.90", "1.21.1")),
            ({"run.sh": "java @user_jvm_args.txt @libraries/net/minecraftforge/forge/1.18.2-40.2.0/unix_args.txt"},
             ("forge", "40.2.0", "1.18.2")),
        ]
        for spec, want in cases:
            f = P.detect_server_pack(tree(spec))
            self.assertEqual((f["loader"], f["loader_version"], f["minecraft"]), want, spec)

    def test_mods_say_the_loader_when_nothing_else_does(self):
        root = tree({"mods/a.jar": modjar("neoforge"), "mods/b.jar": modjar("neoforge"), "mods/c.jar": modjar("forge")})
        self.assertEqual(P.detect_server_pack(root)["loader"], "neoforge")
        with self.assertRaises(P.PackError) as c:
            P.read_server_pack(root, "x.zip")
        self.assertIn("which Minecraft version", str(c.exception))
        plan = P.read_server_pack(root, "My Pack", {"minecraft": "1.21.1"})
        self.assertEqual((plan["loader"], plan["minecraft"], plan["loader_version"], plan["name"]),
                         ("neoforge", "1.21.1", None, "My Pack"))

    def test_packs_that_arent_server_packs(self):
        for spec, word in (({"manifest.json": "{}", "overrides/config/x": "x"}, "Server Pack"),
                           ({"server-setup-config.yaml": "x"}, "start script"),
                           ({"modrinth.index.json": "{}"}, ".mrpack"), ({"readme.txt": "hi"}, "no mods folder")):
            with self.assertRaises(P.PackError) as c:
                P.read_server_pack(tree(spec))
            self.assertIn(word, str(c.exception), spec)
        with self.assertRaises(P.PackError) as c:
            P.read_server_pack(tree({"mods/a.jar": b"not a zip"}))
        self.assertIn("which mod loader", str(c.exception))

    def test_scripts_jars_and_libraries_are_never_copied(self):
        root = tree({"mods/a.jar": modjar(), "mods/readme.txt": "x", "config/a.toml": "a", "kubejs/server_scripts/x.js": "x",
                     "start.sh": "evil", "run.bat": "evil", "install.ps1": "evil", "forge-1.20.1-47.2.0-installer.jar": "x",
                     "server.jar": "x", "libraries/a/b.jar": "x", "user_jvm_args.txt": "-Xmx99G", "eula.txt": "eula=true",
                     ".redstonepanel-server.json": "{}", "variables.txt": "x", "server-icon.png": "png",
                     "server.properties": "motd=Hi\nserver-port=25599\nserver-ip=10.0.0.9\nmax-players=40\n",
                     "defaultconfigs/x.toml": "x", "logs/latest.log": "x"})
        os.symlink("/etc/passwd", os.path.join(root, "config", "link"))
        os.symlink("/etc", os.path.join(root, "linked-dir"))
        staging = tempfile.mkdtemp(dir=TMP)
        staged = P.stage_server_pack(root, staging)
        self.assertEqual(staged["files"], ["mods/a.jar"])
        self.assertEqual(staged["overrides"], ["config/a.toml", "defaultconfigs/x.toml", "kubejs/server_scripts/x.js",
                                               "mods/readme.txt", "server-icon.png"])
        self.assertEqual(staged["properties"], {"motd": "Hi", "max-players": "40"})
        found = sorted(os.path.relpath(os.path.join(r, f), staging) for r, _, fs in os.walk(staging) for f in fs)
        self.assertEqual(found, sorted(staged["files"] + staged["overrides"]))


class Servers(unittest.TestCase):
    """create() and update() against temp folders."""

    def setUp(self):
        self.root = tempfile.mkdtemp(dir=TMP)
        self.deploys, self.reinstalls, self.commands, self.backups = [], [], [], []
        self.active = True

        def deploy(edition, template_id=None, loader="vanilla", version=None, advanced=None, is_admin=True,
                   actor=None, prepare=None):
            self.deploys.append({"loader": loader, "version": version, "advanced": advanced})
            d = os.path.join(self.root, "minecraft-pack")
            os.makedirs(d)
            with open(os.path.join(d, J.INFO_FILE), "w") as f:
                json.dump({"loader": loader, "minecraft": version, "build": (advanced or {}).get("build")}, f)
            with open(os.path.join(d, "server.properties"), "w") as f:
                f.write("server-port=25565\n")
            open(os.path.join(d, "server.jar"), "w").close()
            os.makedirs(os.path.join(d, "libraries"))
            prepare(d)
            return "minecraft-pack"

        def reinstall(server, loader, version=None, build=None, current=None, before_swap=None):
            self.reinstalls.append((loader, version, build))
            with open(os.path.join(self.root, server, J.INFO_FILE), "w") as f:
                json.dump({"loader": loader, "minecraft": version, "build": build}, f)

        def backup(server, kind):
            self.backups.append(kind)
            return {"file": f"{server}_pre-change.tar.gz"}
        self.served = {}

        def download(url, allowed, algorithm, expected, size=None):
            return io.BytesIO(self.served[url])
        for p in (mock.patch.object(M, "SERVERS_ROOT", self.root),
                  mock.patch.object(M, "deploy_server", deploy),
                  mock.patch.object(M, "reinstall_java", reinstall),
                  mock.patch.object(M, "run_and_log", lambda cmd, shell=False: self.commands.append(cmd)),
                  mock.patch.object(F, "server_root", lambda server: os.path.join(self.root, server)),
                  mock.patch.object(B, "run_backup", backup),
                  mock.patch.object(B, "_is_active", lambda server: self.active),
                  mock.patch.object(J, "_as_minecraft", lambda cmd: cmd),
                  mock.patch.object(J, "resolve", self.resolve),
                  mock.patch.object(P, "_pack_heap", lambda: "4G"),
                  mock.patch.object(P, "download", download)):
            p.start()
            self.addCleanup(p.stop)

    def resolve(self, loader, minecraft=None, build=None, snapshot=False):
        if build == "0.0.1":
            raise J.JavaServerError(f"NeoForge has no version {build} for Minecraft {minecraft}.")
        return {"loader": loader, "minecraft": minecraft, "build": build}

    def pack(self, mods, version="1.0", deps=None, extra=None):
        files = []
        for name, data in mods.items():
            self.served[CDN + name] = data
            files.append(entry(f"mods/{name}", data))
        return mrpack(files=files, versionId=version, deps=deps, extra=extra)

    def d(self, *parts):
        return os.path.join(self.root, "minecraft-pack", *parts)

    def test_create_from_an_uploaded_mrpack(self):
        path = self.pack({"a.jar": b"a1", "b.jar": b"b1"},
                         extra={"overrides/config/a.toml": "a=1", "overrides/server.properties": "motd=Pack\n"})
        out = P.create("mrpack", path, "pack.mrpack", {"name": "skyblock", "port": "25570"}, actor="quentin")
        self.assertEqual(out["name"], "minecraft-pack")
        self.assertEqual(self.deploys, [{"loader": "neoforge", "version": "1.21.1",
                                         "advanced": {"name": "skyblock", "port": "25570", "heap": "4G",
                                                      "build": "21.1.200"}}])
        self.assertEqual(sorted(os.listdir(self.d("mods"))), ["a.jar", "b.jar"])
        props = M.read_properties(self.d())
        self.assertEqual((props["motd"], props["server-port"]), ("Pack", "25565"))
        rec = P.read_record(self.d())
        self.assertEqual((rec["kind"], rec["name"], rec["version"], rec["files"], rec["overrides"]),
                         ("mrpack", "Test Pack", "1.0", ["mods/a.jar", "mods/b.jar"], ["config/a.toml"]))
        self.assertEqual(out["pack"]["files"], 2)
        self.assertFalse(out["pack"]["can_check"])
        # Nothing of the staging folder is left in /opt
        self.assertEqual(os.listdir(self.root), ["minecraft-pack"])

    def test_a_loader_version_that_is_gone_falls_back(self):
        path = self.pack({"a.jar": b"a"}, deps={"minecraft": "1.21.1", "neoforge": "0.0.1"})
        P.create("mrpack", path, "p.mrpack", {"memory": "6G"})
        self.assertEqual(self.deploys[0]["advanced"], {"heap": "6G"})

    def test_create_from_a_server_pack(self):
        fd, zpath = tempfile.mkstemp(dir=TMP, suffix=".zip")
        with os.fdopen(fd, "wb") as f, zipfile.ZipFile(f, "w") as z:
            z.writestr("All the Mods 9 Server/mods/a.jar", modjar())
            z.writestr("All the Mods 9 Server/config/x.toml", "x")
            z.writestr("All the Mods 9 Server/startserver.sh", "FORGE_VERSION=47.2.20\nMINECRAFT_VERSION=1.20.1\n")
            z.writestr("All the Mods 9 Server/forge-1.20.1-47.2.20-installer.jar", "x")
        out = P.create("serverpack", zpath, "ATM9-Server-0.2.zip", {})
        self.assertEqual(self.deploys[0]["loader"], "forge")
        self.assertEqual(self.deploys[0]["version"], "1.20.1")
        self.assertEqual(self.deploys[0]["advanced"]["build"], "47.2.20")
        self.assertEqual(sorted(os.listdir(self.d())), sorted([J.INFO_FILE, P.PACK_FILE, "config", "libraries", "mods",
                                                               "server.jar", "server.properties"]))
        self.assertEqual(out["pack"]["name"], "ATM9-Server-0.2")
        self.assertEqual(os.listdir(self.root), ["minecraft-pack"])

    def test_nothing_is_left_when_the_pack_is_refused(self):
        with self.assertRaises(P.PackError):
            P.create("mrpack", mrpack(files=[entry("mods/a.jar", url="https://evil.example/a.jar")]), "x.mrpack", {})
        self.assertEqual(os.listdir(self.root), [])
        self.assertEqual(self.deploys, [])

    def test_update_swaps_the_packs_mods_and_keeps_everything_else(self):
        P.create("mrpack", self.pack({"a.jar": b"a1", "b.jar": b"b1", "old.jar": b"old"},
                                     extra={"overrides/config/a.toml": "a=1"}), "p.mrpack", {})
        os.makedirs(self.d("world"))
        for rel, data in (("world/level.dat", "world"), ("mods/mine.jar", "added by hand"), ("ops.json", "[me]"),
                          ("config/a.toml", "a=changed by owner")):
            with open(self.d(*rel.split("/")), "w") as f:
                f.write(data)
        new = self.pack({"a.jar": b"a2", "b.jar": b"b1", "c.jar": b"c1"}, version="2.0",
                        extra={"overrides/config/a.toml": "a=2", "overrides/world/level.dat": "pack world",
                               "overrides/ops.json": "[]", "overrides/server.properties": "motd=New\n"})
        out = P.update("minecraft-pack", "mrpack", new, "p2.mrpack", actor="quentin")
        self.assertEqual(sorted(os.listdir(self.d("mods"))), ["a.jar", "b.jar", "c.jar", "mine.jar"])
        with open(self.d("mods", "a.jar"), "rb") as f:
            self.assertEqual(f.read(), b"a2")
        for rel, want in (("world/level.dat", "world"), ("ops.json", "[me]"), ("config/a.toml", "a=2")):
            with open(self.d(*rel.split("/"))) as f:
                self.assertEqual(f.read(), want, rel)
        self.assertNotIn("motd", M.read_properties(self.d()))      # the owner's settings aren't replaced
        self.assertEqual((out["removed"], out["backup"], out["restarted"]), (1, "minecraft-pack_pre-change.tar.gz", True))
        self.assertEqual(self.backups, ["pre-change"])
        self.assertEqual(self.reinstalls, [])                      # same loader and Minecraft version
        self.assertEqual(P.read_record(self.d())["version"], "2.0")
        self.assertIn(["systemctl", "stop", "minecraft-pack.service"], self.commands)
        self.assertEqual(self.commands[-1], ["systemctl", "start", "minecraft-pack.service"])

    def test_update_to_a_new_loader_version_reinstalls_it(self):
        P.create("mrpack", self.pack({"a.jar": b"a1"}), "p.mrpack", {})
        self.active = False
        new = self.pack({"a.jar": b"a2"}, version="2.0", deps={"minecraft": "1.21.4", "neoforge": "21.4.100"})
        out = P.update("minecraft-pack", "mrpack", new, "p2.mrpack", backup=False)
        self.assertEqual(self.reinstalls, [("neoforge", "1.21.4", "21.4.100")])
        self.assertEqual((out["backup"], out["restarted"]), (None, False))
        self.assertNotIn(["systemctl", "start", "minecraft-pack.service"], self.commands)

    def test_update_refuses_another_loader_or_an_older_minecraft(self):
        P.create("mrpack", self.pack({"a.jar": b"a1"}), "p.mrpack", {})
        with self.assertRaises(P.PackError) as c:
            P.update("minecraft-pack", "mrpack", self.pack({"a.jar": b"a"}, deps={"minecraft": "1.21.1",
                                                                                  "fabric-loader": "0.16.0"}), "x")
        self.assertIn("Make a new server", str(c.exception))
        with self.assertRaises(P.PackError) as c:
            P.update("minecraft-pack", "mrpack", self.pack({"a.jar": b"a"}, deps={"minecraft": "1.20.4",
                                                                                  "neoforge": "20.4.237"}), "x")
        self.assertIn("can't be moved back", str(c.exception))
        self.assertEqual(self.backups, [])                         # refused before anything was touched
        with self.assertRaises(P.PackError):
            P.update("minecraft-pack", "modrinth")                 # an uploaded pack has no project to ask

    def test_a_server_without_a_pack(self):
        os.makedirs(os.path.join(self.root, "minecraft-plain"))
        self.assertEqual(P.status("minecraft-plain"), {"pack": None})
        with self.assertRaises(P.PackError):
            P.update("minecraft-plain", "mrpack", self.pack({"a.jar": b"a"}), "x")

    def test_modrinth_pack_and_its_update_check(self):
        pack_v1 = self.pack({"a.jar": b"a1"})
        pack_v2 = self.pack({"a.jar": b"a2"}, version="2.0")
        with open(pack_v1, "rb") as f:
            self.served["https://cdn.modrinth.com/p/v1.mrpack"] = f.read()
        with open(pack_v2, "rb") as f:
            self.served["https://cdn.modrinth.com/p/v2.mrpack"] = f.read()

        def version(vid, number, channel="release"):
            return {"id": vid, "name": f"Pack {number}", "version": number, "channel": channel,
                    "minecraft": ["1.21.1"], "loaders": ["neoforge"], "published": "2026-11-01",
                    "file": {"url": f"https://cdn.modrinth.com/p/{vid}.mrpack", "filename": f"{vid}.mrpack",
                             "size": 10, "hash": "x", "algorithm": "sha512"}}
        listing = {"project": "PROJ", "slug": "pack", "title": "Great Pack", "url": "https://modrinth.com/modpack/pack",
                   "versions": [version("v3", "3.0-beta", "beta"), version("v2", "2.0"), version("v1", "1.0")]}
        with mock.patch.object(modrinth, "modpack_versions", lambda project, limit=40: listing):
            out = P.create("modrinth", None, None, {"project": "pack", "version_id": "v1"})
            self.assertEqual((out["pack"]["name"], out["pack"]["version"], out["pack"]["can_check"]),
                             ("Great Pack", "1.0", True))
            check = P.check_update("minecraft-pack")
            self.assertEqual((check["available"], check["latest"]["version"], check["current"]), (True, "2.0", "1.0"))
            P.update("minecraft-pack", "modrinth")                 # the newest release, not the beta
            rec = P.read_record(self.d())
            self.assertEqual((rec["version"], rec["version_id"], rec["project"]), ("2.0", "v2", "PROJ"))
            check = P.check_update("minecraft-pack")
            self.assertEqual((check["available"], check["latest"]["version"]), (True, "3.0-beta"))
            with self.assertRaises(P.PackError):
                P.update("minecraft-pack", "modrinth", options={"version_id": "v2"})   # already on it


if __name__ == "__main__":
    unittest.main()
