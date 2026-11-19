"""Tests for core/mods.py: reading jar and pack metadata, deciding what fits a server, and
installing into a temp server folder. Install tests are Linux only (they use core/files.py,
which opens paths with openat). Run: python -m unittest discover -s tests"""
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from types import SimpleNamespace
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-mods-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import files as F, manager as M, mods as X  # noqa: E402

LINUX = sys.platform.startswith("linux")


def jar(entries):
    """A zip in memory: {name: bytes|str|dict}."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in entries.items():
            if isinstance(data, dict):
                data = json.dumps(data)
            z.writestr(name, data)
    buf.seek(0)
    return buf


def fabric(mod_id, version="1.0.0", mc="~1.21.4", depends=None, name=None, nested=None, provides=None):
    deps = {"fabricloader": ">=0.16", "minecraft": mc}
    deps.update(depends or {})
    meta = {"schemaVersion": 1, "id": mod_id, "version": version, "name": name or mod_id,
            "authors": ["Alex", {"name": "Sam"}], "depends": deps}
    entries = {}
    if provides:
        meta["provides"] = provides
    if nested:
        meta["jars"] = [{"file": f"META-INF/jars/{n}.jar"} for n in nested]
        for n in nested:
            entries[f"META-INF/jars/{n}.jar"] = jar({"fabric.mod.json": {"schemaVersion": 1, "id": n}}).getvalue()
    entries["fabric.mod.json"] = meta
    return jar(entries)


PLUGIN_YML = """name: EssentialsX
version: '2.21.0'
main: com.earth2me.essentials.Essentials
api-version: "1.21"
authors: [zenexer, ementalo]
description: Provides essential commands
depend:
  - Vault
softdepend: [LuckPerms]
commands:
  home:
    description: Go home
"""


def plugin(text=PLUGIN_YML, paper=False):
    return jar({"paper-plugin.yml" if paper else "plugin.yml": text, "com/x/Main.class": b"\xca\xfe"})


def pack(name, uuid, kind="data", version=(1, 0, 0), comment=False, root=""):
    manifest = {"format_version": 2,
                "header": {"name": name, "uuid": uuid, "version": list(version), "description": "§aGreen"},
                "modules": [{"type": kind, "uuid": uuid[::-1], "version": list(version)}]}
    text = json.dumps(manifest, indent=2)
    if comment:
        text = "// made by hand\n" + text.replace('"format_version"', '/* fmt */ "format_version"')
    return {f"{root}manifest.json": text, f"{root}entities/cow.json": "{}", f"{root}pack_icon.png": b"\x89PNG"}


class Metadata(unittest.TestCase):
    def test_fabric(self):
        m = X.inspect_jar(fabric("sodium", "0.6.0", depends={"fabric-api": "*"}))
        self.assertEqual((m["type"], m["id"], m["version"], m["minecraft"]), ("fabric", "sodium", "0.6.0", "~1.21.4"))
        self.assertEqual(m["authors"], ["Alex", "Sam"])
        self.assertEqual(m["depends"], ["fabric-api"])           # minecraft, loader and java aren't mods

    def test_fabric_nested_jars_count_as_provided(self):
        m = X.inspect_jar(fabric("fabric-api", provides=["fabric"], nested=["fabric-api-base", "fabric-networking-api-v1"]))
        self.assertEqual(set(m["provides"]), {"fabric", "fabric-api-base", "fabric-networking-api-v1"})

    def test_plugin_yml(self):
        m = X.inspect_jar(plugin())
        self.assertEqual((m["type"], m["name"], m["version"], m["minecraft"]), ("bukkit", "EssentialsX", "2.21.0", "1.21"))
        self.assertEqual(m["authors"], ["zenexer", "ementalo"])
        self.assertEqual(m["depends"], ["Vault"])
        self.assertEqual(X.inspect_jar(plugin("name: Foo\nversion: 1\napi-version: 1.20\n", paper=True))["type"], "paper")

    def test_other_jars(self):
        self.assertEqual(X.inspect_jar(jar({"META-INF/mods.toml": "modLoader='javafml'"}))["type"], "forge")
        self.assertEqual(X.inspect_jar(jar({"META-INF/neoforge.mods.toml": ""}))["type"], "neoforge")
        self.assertEqual(X.inspect_jar(jar({"quilt.mod.json": "{}"}))["type"], "quilt")
        self.assertEqual(X.inspect_jar(jar({"velocity-plugin.json": "{}"}))["type"], "velocity")
        self.assertEqual(X.inspect_jar(jar({"a.class": b""}))["type"], "unknown")
        with self.assertRaises(X.ModError):
            X.inspect_jar(io.BytesIO(b"not a zip"))

    def test_yaml_top(self):
        y = X.parse_yaml_top("name: A # comment\nauthor: 'Bo'\nlist:\n  - one\n  - two\nnested:\n  key: v\n")
        self.assertEqual((y["name"], y["author"], y["list"]), ("A", "Bo", ["one", "two"]))


class Fit(unittest.TestCase):
    def test_refusals(self):
        self.assertIsNone(X.refusal("fabric", "fabric"))
        self.assertIsNone(X.refusal("paper", "bukkit"))
        self.assertIsNone(X.refusal("paper", "paper"))
        self.assertIn("Forge mod", X.refusal("fabric", "forge"))
        self.assertIn("Fabric version", X.refusal("fabric", "forge"))
        self.assertIn("Fabric mod", X.refusal("paper", "fabric"))
        self.assertIn("Paper plugin", X.refusal("fabric", "paper"))
        self.assertIn("Vanilla servers can't", X.refusal("vanilla", "fabric"))
        self.assertIn("Velocity", X.refusal("paper", "velocity"))
        self.assertIn("no Fabric mod or plugin information", X.refusal("fabric", "unknown"))
        self.assertIn(".mcpack", X.refusal("bedrock", "fabric"))

    def test_fabric_versions(self):
        f = lambda decl, v: X.fits_minecraft("fabric", decl, v)
        self.assertTrue(f("~1.21.4", "1.21.4"))
        self.assertFalse(f("~1.21.4", "1.21.3"))
        self.assertFalse(f("~1.21.4", "1.22"))
        self.assertTrue(f(">=1.21 <1.22", "1.21.10"))
        self.assertFalse(f(">=1.21 <1.22", "1.20.6"))
        self.assertTrue(f("1.21.x", "1.21.4"))
        self.assertFalse(f("1.21.x", "1.20.4"))
        self.assertTrue(f("1.20.1 || 1.21.4", "1.21.4"))
        self.assertTrue(f("*", "26.3"))
        self.assertIsNone(f("24w14a", "1.21.4"))
        self.assertTrue(f("^1.21", "1.21.11"))
        self.assertIsNone(f("~1.21.4", None))

    def test_plugin_api_version(self):
        self.assertTrue(X.fits_minecraft("bukkit", "1.20", "1.21.4"))
        self.assertTrue(X.fits_minecraft("paper", "1.21", "1.21"))
        self.assertFalse(X.fits_minecraft("paper", "1.21", "1.20.6"))

    def test_manifest(self):
        data = pack("Cool Mobs", "aaaaaaaa-1111-2222-3333-444444444444", comment=True)["manifest.json"].encode()
        m = X.parse_manifest(data)
        self.assertEqual((m["type"], m["version"], m["description"]), ("behavior", [1, 0, 0], "Green"))
        res = X.parse_manifest(pack("Tex", "bbbbbbbb-1", kind="resources")["manifest.json"].encode())
        self.assertEqual(res["type"], "resource")
        with self.assertRaises(X.ModError):
            X.parse_manifest(pack("Skin", "c", kind="skin_pack")["manifest.json"].encode())


@unittest.skipUnless(LINUX, "needs Linux (openat)")
class Install(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(dir=TMP)
        self.server = os.path.join(self.root, "minecraft-java")
        os.makedirs(self.server)
        with open(os.path.join(self.server, "server.properties"), "w") as f:
            f.write("level-name=Bedrock level\n")
        self.kind = ("fabric", "1.21.4")
        X._meta_cache.clear()
        for p in (mock.patch.object(M, "_check_server", lambda s: None),
                  mock.patch.object(M, "get_server_dir", lambda s: self.server),
                  mock.patch.object(M, "write_log", lambda m: None),
                  mock.patch.object(X, "server_kind", lambda s: self.kind)):
            p.start()
            self.addCleanup(p.stop)

    def path(self, *p):
        return os.path.join(self.server, *p)

    def test_fabric_install_list_disable_remove(self):
        r = X.install("minecraft-java", "sodium-0.6.0.jar", fabric("sodium", "0.6.0", depends={"fabric-api": "*"}))
        self.assertTrue(os.path.isfile(self.path("mods", "sodium-0.6.0.jar")))       # mods/ was created
        self.assertTrue(r["fits"])
        items = X.list_installed("minecraft-java")["items"]
        self.assertEqual(items[0]["missing"], ["fabric-api"])
        X.install("minecraft-java", "fabric-api.jar", fabric("fabric-api", provides=["fabric"], nested=["fabric-api-base"]))
        items = {i["id"]: i for i in X.list_installed("minecraft-java")["items"]}
        self.assertEqual(items["sodium"]["missing"], [])
        X.set_enabled("minecraft-java", "sodium-0.6.0.jar", False)
        self.assertTrue(os.path.isfile(self.path("mods", "sodium-0.6.0.jar.disabled")))
        items = {i["id"]: i for i in X.list_installed("minecraft-java")["items"]}
        self.assertFalse(items["sodium"]["enabled"])
        X.set_enabled("minecraft-java", "sodium-0.6.0.jar.disabled", True)
        X.remove("minecraft-java", "sodium-0.6.0.jar")
        self.assertFalse(os.path.exists(self.path("mods", "sodium-0.6.0.jar")))

    def test_new_version_replaces_the_old_file(self):
        X.install("minecraft-java", "sodium-0.5.jar", fabric("sodium", "0.5"))
        with self.assertRaises(X.ModError) as cm:
            X.install("minecraft-java", "sodium-0.6.jar", fabric("sodium", "0.6"))
        self.assertEqual(cm.exception.status, 409)
        self.assertIn("already installed as sodium-0.5.jar", str(cm.exception))
        r = X.install("minecraft-java", "sodium-0.6.jar", fabric("sodium", "0.6"), overwrite=True)
        self.assertEqual(r["replaced"], ["sodium-0.5.jar"])
        self.assertEqual(os.listdir(self.path("mods")), ["sodium-0.6.jar"])

    def test_wrong_kind_is_refused_and_nothing_written(self):
        for name, data in (("forge.jar", jar({"META-INF/mods.toml": ""})), ("ess.jar", plugin()),
                           ("lib.jar", jar({"a.class": b""})), ("notes.txt", io.BytesIO(b"hi"))):
            with self.assertRaises(X.ModError):
                X.install("minecraft-java", name, data)
        self.assertFalse(os.listdir(self.path("mods")) if os.path.isdir(self.path("mods")) else [])
        self.kind = ("vanilla", "1.21.4")
        with self.assertRaises(X.ModError) as cm:
            X.install("minecraft-java", "sodium.jar", fabric("sodium"))
        self.assertIn("Vanilla", str(cm.exception))

    def test_paper_plugin_and_wrong_version_mark(self):
        self.kind = ("paper", "1.20.6")
        r = X.install("minecraft-java", "EssentialsX.jar", plugin())
        self.assertFalse(r["fits"])                              # api-version 1.21 on a 1.20.6 server
        self.assertTrue(os.path.isfile(self.path("plugins", "EssentialsX.jar")))
        item = X.list_installed("minecraft-java")["items"][0]
        self.assertEqual((item["fits"], item["missing"]), (False, ["Vault"]))

    def test_names_cant_escape(self):
        for bad in ("..", ""):
            with self.assertRaises(F.FileError):
                X.install("minecraft-java", bad, fabric("x"))
        X.install("minecraft-java", "../../x.jar", fabric("x"))     # only the file name is used
        self.assertEqual(os.listdir(self.path("mods")), ["x.jar"])
        self.assertFalse(os.path.exists(os.path.join(self.root, "x.jar")))
        with self.assertRaises(F.FileError):
            X.remove("minecraft-java", "../server.properties")
        with self.assertRaises(F.FileError):
            X.remove("minecraft-java", "server.properties")

    def test_symlinked_mods_folder_is_not_followed(self):
        outside = tempfile.mkdtemp(dir=TMP)
        os.symlink(outside, self.path("mods"))
        with self.assertRaises(F.FileError):
            X.install("minecraft-java", "sodium.jar", fabric("sodium"))
        self.assertEqual(os.listdir(outside), [])

    # --- Bedrock ---
    def test_bedrock_mcpack(self):
        self.kind = ("bedrock", None)
        uuid = "aaaaaaaa-1111-2222-3333-444444444444"
        r = X.install("minecraft-java", "mobs.mcpack", jar(pack("Cool Mobs", uuid, root="Cool Mobs BP/")))
        folder = r["installed"][0]["folder"]
        self.assertTrue(folder.startswith("behavior_packs/Cool_Mobs_aaaaaaaa"))
        self.assertTrue(os.path.isfile(self.path(folder, "entities", "cow.json")))
        with open(self.path("worlds", "Bedrock level", "world_behavior_packs.json")) as f:
            self.assertEqual(json.load(f), [{"pack_id": uuid, "version": [1, 0, 0]}])
        item = X.list_installed("minecraft-java")["items"][0]
        self.assertEqual((item["name"], item["enabled"], item["type"]), ("Cool Mobs", True, "behavior"))
        # same pack again: asks, then replaces in place
        with self.assertRaises(X.ModError) as cm:
            X.install("minecraft-java", "mobs.mcpack", jar(pack("Cool Mobs", uuid, version=(1, 1, 0))))
        self.assertEqual(cm.exception.status, 409)
        X.install("minecraft-java", "mobs.mcpack", jar(pack("Cool Mobs", uuid, version=(1, 1, 0))), overwrite=True)
        with open(self.path("worlds", "Bedrock level", "world_behavior_packs.json")) as f:
            self.assertEqual(json.load(f), [{"pack_id": uuid, "version": [1, 1, 0]}])
        self.assertEqual(len([n for n in os.listdir(self.path("behavior_packs")) if not n.startswith(".")]), 1)
        X.set_enabled("minecraft-java", uuid, False)
        with open(self.path("worlds", "Bedrock level", "world_behavior_packs.json")) as f:
            self.assertEqual(json.load(f), [])
        X.remove("minecraft-java", uuid)
        self.assertEqual(os.listdir(self.path("behavior_packs")), [])

    def test_bedrock_mcaddon_with_two_packs(self):
        self.kind = ("bedrock", None)
        bp = jar(pack("Addon BP", "11111111-0000-0000-0000-000000000001")).getvalue()
        rp = jar(pack("Addon RP", "22222222-0000-0000-0000-000000000002", kind="resources")).getvalue()
        r = X.install("minecraft-java", "addon.mcaddon", jar({"Addon BP.mcpack": bp, "Addon RP.mcpack": rp}))
        self.assertEqual(sorted(i["type"] for i in r["installed"]), ["behavior", "resource"])
        with open(self.path("worlds", "Bedrock level", "world_resource_packs.json")) as f:
            self.assertEqual(json.load(f)[0]["pack_id"], "22222222-0000-0000-0000-000000000002")

    def test_bedrock_unsafe_entries(self):
        self.kind = ("bedrock", None)
        entries = pack("Bad", "33333333-0000-0000-0000-000000000003")
        entries["../../escape.txt"] = "x"
        with self.assertRaises(X.ModError):
            X.install("minecraft-java", "bad.mcpack", jar(entries))
        self.assertFalse(os.path.exists(os.path.join(self.root, "escape.txt")))
        with self.assertRaises(X.ModError):
            X.install("minecraft-java", "skin.mcpack", jar(pack("Skin", "4444", kind="skin_pack")))
        with self.assertRaises(X.ModError):
            X.install("minecraft-java", "x.jar", fabric("x"))


try:
    import fastapi  # noqa: F401
    HAVE_FASTAPI = True
except ImportError:
    HAVE_FASTAPI = False


@unittest.skipUnless(HAVE_FASTAPI, "needs fastapi")
class AdminOnly(unittest.TestCase):
    def test_users_cannot_manage_addons(self):
        import main
        req = SimpleNamespace(state=SimpleNamespace(user={"username": "alex", "role": "user", "source": "panel"}))
        upload = SimpleNamespace(filename="x.jar", file=io.BytesIO(b""))
        for call in (lambda: main.list_addons(req, "minecraft-java"),
                     lambda: main.install_addon(req, "minecraft-java", False, upload),
                     lambda: main.toggle_addon(req, "minecraft-java", main.AddonToggle(item="x.jar", enabled=False)),
                     lambda: main.remove_addon(req, "minecraft-java", "x.jar")):
            with self.assertRaises(main.HTTPException) as cm:
                call()
            self.assertEqual(cm.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
