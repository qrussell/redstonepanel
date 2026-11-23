"""Tests for core/convert.py (1.19): changing a Java server's type, undo, and rolling back an
update. Server folders, units and backups live in temp folders; systemctl, downloads and
Modrinth are stubbed. Run: python -m unittest discover -s tests"""
import gzip
import io
import json
import os
import struct
import sys
import tarfile
import tempfile
import unittest
import zipfile
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-convert-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import fcntl  # noqa: F401
    from core import backups as B, catalog as K, convert as C, javaservers as J, manager as M, notify as N
except ImportError:          # Windows
    C = None

S = "minecraft-java"
VERSIONS = ["26.1", "1.21.11", "1.21.4", "1.20.1"]


def write(path, content="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb" if isinstance(content, bytes) else "w") as f:
        f.write(content)


def read(path):
    with open(path) as f:
        return f.read()


def make_jar(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def nbt_string(s):
    b = s.encode()
    return struct.pack(">H", len(b)) + b


def level_dat(packs):
    """A gzipped level.dat with Data.DataPacks.Enabled = packs (and other tags to skip over)."""
    enabled = b"\x09" + nbt_string("Enabled") + b"\x08" + struct.pack(">i", len(packs)) + \
        b"".join(nbt_string(p) for p in packs)
    datapacks = b"\x0a" + nbt_string("DataPacks") + enabled + b"\x00"
    other = (b"\x03" + nbt_string("DataVersion") + struct.pack(">i", 4189)
             + b"\x04" + nbt_string("Time") + struct.pack(">q", 5)
             + b"\x06" + nbt_string("BorderSize") + struct.pack(">d", 1.0)
             + b"\x0b" + nbt_string("Ints") + struct.pack(">i", 2) + struct.pack(">ii", 1, 2)
             + b"\x08" + nbt_string("LevelName") + nbt_string("world"))
    data = b"\x0a" + nbt_string("Data") + other + datapacks + b"\x00"
    return gzip.compress(b"\x0a" + nbt_string("") + data + b"\x00")


@unittest.skipIf(C is None, "needs Linux (fcntl)")
class Base(unittest.TestCase):
    loader, minecraft = "vanilla", "1.21.4"

    def setUp(self):
        self.root = tempfile.mkdtemp(dir=TMP)
        self.d = os.path.join(self.root, "opt", S)
        self.units = os.path.join(self.root, "units")
        os.makedirs(self.units)
        write(os.path.join(self.units, f"{S}.service"), "[Service]\n")
        self.running = True
        self.starts = True               # does the software "start" (log Done)?
        self.commands, self.units_written, self.sent = [], [], []
        self.java = "/usr/lib/jvm/java-21/bin/java"
        write(os.path.join(self.d, "server.jar"), f"jar of {self.loader} {self.minecraft}")
        write(os.path.join(self.d, J.INFO_FILE), json.dumps(
            {"loader": self.loader, "minecraft": self.minecraft, "build": None, "java_required": 21}))
        write(os.path.join(self.d, "server.properties"), "level-name=world\nserver-port=25565\n")
        write(os.path.join(self.d, "world", "level.dat"), level_dat(["vanilla"]))
        write(os.path.join(self.d, "world", "region", "r.0.0.mca"), "overworld")
        write(os.path.join(self.d, "world", "playerdata", "p.dat"), "player")
        p = [
            mock.patch.object(B, "BACKUP_ROOT", os.path.join(self.root, "backups")),
            mock.patch.object(B, "UNIT_DIR", self.units),
            mock.patch.object(B, "STATUS_DIR", os.path.join(self.root, "data", "status")),
            mock.patch.object(B, "_is_active", lambda s: self.running),
            mock.patch.object(B, "_edition", lambda s, d: "java"),
            mock.patch.object(C, "STATE_DIR", os.path.join(self.root, "data", "convert")),
            mock.patch.object(C, "START_WAIT", 3),
            mock.patch.object(C, "_state", lambda s: "active" if self.running else "failed"),
            mock.patch.object(C, "_projects", lambda ids: {}),
            mock.patch.object(K, "_identify", lambda *a, **k: {}),
            mock.patch.object(M, "_check_server", lambda s: None),
            mock.patch.object(M, "_is_java", lambda s: True),
            mock.patch.object(M, "get_server_dir", lambda s: self.d),
            mock.patch.object(M, "run_and_log", self._run),
            mock.patch.object(M, "_unit_exec_start", lambda s: f"{self.java} -Xms1G -Xmx2G -jar server.jar nogui"),
            mock.patch.object(M, "_write_unit", self._write_unit),
            mock.patch.object(M, "write_log", lambda msg: None),
            mock.patch.object(J, "list_versions", lambda loader: [v for v in VERSIONS
                                                                   if loader != "paper" or v != "26.1"]),
            mock.patch.object(J, "resolve", self._resolve),
            mock.patch.object(J, "install", self._install),
            mock.patch.object(J, "installed_javas", lambda: {21: "/usr/lib/jvm/java-21/bin/java",
                                                             25: "/usr/lib/jvm/java-25/bin/java"}),
            mock.patch.object(N, "send", lambda *a, **k: self.sent.append(a)),
        ]
        for x in p:
            x.start()
            self.addCleanup(x.stop)

    # --- stand-ins ---
    def _run(self, cmd, shell=False):
        self.commands.append(cmd)
        if "stop" in cmd:
            self.running = False
        if "start" in cmd:
            info = json.loads(read(os.path.join(self.d, J.INFO_FILE)))
            good = self.starts or info["loader"] == self.loader       # the old software always starts
            if os.path.exists(os.path.join(self.d, "logs", "latest.log")):
                os.remove(os.path.join(self.d, "logs", "latest.log"))          # a start begins a new log
            write(os.path.join(self.d, "logs", "latest.log"),
                  'Starting\nDone (3.2s)! For help, type "help"\n' if good else
                  "Starting\n[main/ERROR]: Mod broken requires fabric-api, which is missing!\n")
            self.running = good
            if info["loader"] == "paper" and good:                     # Paper's own first-start move
                for suffix, dim in C.PAPER_DIMS:
                    src = os.path.join(self.d, "world", dim)
                    if os.path.isdir(src) and not os.path.exists(os.path.join(self.d, "world" + suffix)):
                        os.makedirs(os.path.join(self.d, "world" + suffix))
                        os.rename(src, os.path.join(self.d, "world" + suffix, dim))

    def _write_unit(self, svc, d, edition, java="/usr/bin/java", **kw):
        self.units_written.append((java, kw))
        self.java = java

    def _resolve(self, loader, minecraft=None, **kw):
        if loader == "paper" and minecraft == "26.1":
            raise J.JavaServerError("Paper has no stable build for Minecraft 26.1 yet; choose an older version.")
        return {"loader": loader, "minecraft": minecraft, "build": "232" if loader == "paper" else None,
                "url": "https://example.invalid/server.jar", "java_required": 25 if minecraft == "26.1" else 21}

    def _install(self, d, loader, minecraft=None, current=None, **kw):
        t = self._resolve(loader, minecraft)
        write(os.path.join(d, "server.jar"), f"jar of {loader} {minecraft}")
        info = {k: t[k] for k in ("loader", "minecraft", "build", "java_required")}
        write(os.path.join(d, J.INFO_FILE), json.dumps(info))
        return info, f"/usr/lib/jvm/java-{t['java_required']}/bin/java"

    def info(self):
        return json.loads(read(os.path.join(self.d, J.INFO_FILE)))

    def change(self, to, minecraft=None, **kw):
        return C.start_change(S, to, minecraft, actor="alex", wait=True, sleep=lambda s: None, **kw)

    def has(self, rel):
        return os.path.exists(os.path.join(self.d, rel))


class Layout(Base):
    loader = "paper"

    def paper_world(self):
        write(os.path.join(self.d, "world_nether", "DIM-1", "region", "r.0.0.mca"), "nether")
        write(os.path.join(self.d, "world_nether", "level.dat"), "x")
        write(os.path.join(self.d, "world_the_end", "DIM1", "region", "r.0.0.mca"), "end")

    def test_layouts(self):
        self.assertEqual(C.world_layout(S, "world"), "vanilla")
        self.paper_world()
        self.assertEqual(C.world_layout(S, "world"), "paper")
        os.makedirs(os.path.join(self.d, "world", "dimensions", "minecraft", "overworld"))
        self.assertEqual(C.world_layout(S, "world"), "new")
        self.assertEqual(C.world_layout(S, "nope"), "none")

    def test_leaving_paper_moves_dimensions_inside(self):
        self.paper_world()
        moves, blockers, _ = C.world_moves(S, "world", "paper", "vanilla")
        self.assertEqual(moves, [{"from": "world_nether/DIM-1", "to": "world/DIM-1"},
                                 {"from": "world_the_end/DIM1", "to": "world/DIM1"}])
        self.assertEqual(blockers, [])
        C._apply_moves(S, moves)
        self.assertEqual(read(os.path.join(self.d, "world", "DIM-1", "region", "r.0.0.mca")), "nether")
        self.assertEqual(read(os.path.join(self.d, "world", "DIM1", "region", "r.0.0.mca")), "end")
        self.assertFalse(self.has("world_nether/DIM-1"))

    def test_refuses_when_both_places_hold_a_dimension(self):
        self.paper_world()
        write(os.path.join(self.d, "world", "DIM-1", "region", "r.1.1.mca"), "other nether")
        moves, blockers, _ = C.world_moves(S, "world", "paper", "fabric")
        self.assertEqual(len(blockers), 1)
        self.assertIn("world_nether/DIM-1", blockers[0])
        self.assertEqual([m["to"] for m in moves], ["world/DIM1"])
        self.assertFalse(C.plan(S, "fabric")["ok"])

    def test_an_empty_folder_in_the_way_is_no_conflict(self):
        self.paper_world()
        os.makedirs(os.path.join(self.d, "world", "DIM-1", "data"))
        moves, blockers, _ = C.world_moves(S, "world", "paper", "vanilla")
        self.assertEqual(blockers, [])
        C._apply_moves(S, moves)
        self.assertEqual(read(os.path.join(self.d, "world", "DIM-1", "region", "r.0.0.mca")), "nether")

    def test_entering_paper_is_left_to_paper_unless_its_folder_exists(self):
        write(os.path.join(self.d, "world", "DIM-1", "region", "r.0.0.mca"), "nether")
        self.assertEqual(C.world_moves(S, "world", "vanilla", "paper")[0], [])
        os.makedirs(os.path.join(self.d, "world_nether"))            # left from an earlier Paper
        self.assertEqual(C.world_moves(S, "world", "vanilla", "paper")[0],
                         [{"from": "world/DIM-1", "to": "world_nether/DIM-1"}])

    def test_new_layout_moves_only_papers_settings_files(self):
        ow = os.path.join(self.d, "world", "dimensions", "minecraft", "overworld")
        os.makedirs(ow)
        self.assertEqual(C.world_moves(S, "world", "paper", "vanilla")[:2], ([], []))
        self.assertEqual(C.world_moves(S, "world", "vanilla", "paper")[:2], ([], []))
        for name in ("world_gen_settings.dat", "game_rules.dat", "raids.dat"):
            write(os.path.join(ow, "data", "minecraft", name), name)
        write(os.path.join(self.d, "world", "data", "minecraft", "game_rules.dat"), "already there")
        moves, blockers, _ = C.world_moves(S, "world", "paper", "fabric")
        self.assertEqual((moves, blockers), ([{
            "from": "world/dimensions/minecraft/overworld/data/minecraft/world_gen_settings.dat",
            "to": "world/data/minecraft/world_gen_settings.dat"}], []))
        C._apply_moves(S, moves)
        self.assertEqual(read(os.path.join(self.d, "world", "data", "minecraft", "world_gen_settings.dat")),
                         "world_gen_settings.dat")
        self.assertEqual(read(os.path.join(self.d, "world", "data", "minecraft", "game_rules.dat")), "already there")
        self.assertTrue(self.has("world/dimensions/minecraft/overworld/data/minecraft/raids.dat"))
        self.paper_world()                                           # an unfinished upgrade
        self.assertEqual(len(C.world_moves(S, "world", "paper", "vanilla")[1]), 1)

    def test_symlinks_are_not_followed(self):
        outside = os.path.join(self.root, "outside")
        write(os.path.join(outside, "DIM-1", "region", "r.mca"), "x")
        os.symlink(outside, os.path.join(self.d, "world_nether"))
        self.assertEqual(C.world_moves(S, "world", "paper", "vanilla")[0], [])
        os.symlink(outside, os.path.join(self.d, "link"))
        with self.assertRaises(Exception):
            C._rename(S, "world/region", "link/region")
        self.assertTrue(self.has("world/region"))

    def test_level_name_must_be_a_folder_name(self):
        write(os.path.join(self.d, "server.properties"), "level-name=../etc\n")
        with self.assertRaises(C.ConvertError):
            C.plan(S, "vanilla")


class Content(Base):
    loader = "fabric"

    def test_jar_content(self):
        lib = make_jar({"fabric.mod.json": "{}", "com/x/A.class": "x", "data/c/worldgen/x.json": "{}"})
        self.assertEqual(C.jar_content(io.BytesIO(lib))["reasons"], [])
        blocks = make_jar({"assets/farm/blockstates/crop.json": "{}", "assets/farm/models/item/seed.json": "{}",
                           "data/farm/worldgen/placed_feature/x.json": "{}"})
        found = C.jar_content(io.BytesIO(blocks))
        self.assertEqual(found["reasons"], ["blocks", "items", "world generation"])
        self.assertEqual(found["namespaces"], ["farm"])
        outer = make_jar({"fabric.mod.json": "{}",
                          "META-INF/jars/inner.jar": make_jar({"data/aether/dimension/sky.json": "{}"})})
        self.assertEqual(C.jar_content(io.BytesIO(outer)), {"reasons": ["dimensions"], "namespaces": ["aether"]})
        self.assertEqual(C.jar_content(io.BytesIO(b"not a zip"))["reasons"], [])
        own = make_jar({"assets/minecraft/blockstates/stone.json": "{}"})      # a resource override
        self.assertEqual(C.jar_content(io.BytesIO(own))["reasons"], [])

    def test_level_dat_packs(self):
        data = level_dat(["vanilla", "file/mine.zip", "fabric", "farm", "bundle"])
        self.assertEqual(C.level_packs(data), ["vanilla", "file/mine.zip", "fabric", "farm", "bundle"])
        self.assertEqual(C.level_packs(b"junk"), [])
        self.assertEqual(C.level_packs(gzip.compress(b"\x0a\x00\x00\x09")), [])
        write(os.path.join(self.d, "world", "level.dat"), data)
        self.assertEqual(C.mod_packs(S, "world"), ["farm"])

    def test_content_mods_need_the_name_typed(self):
        write(os.path.join(self.d, "mods", "lithium.jar"), make_jar({"fabric.mod.json": '{"id": "lithium"}'}))
        p = C.plan(S, "vanilla")
        self.assertFalse(p["confirm_name"])
        self.assertEqual(p["addons"]["action"], "off")
        write(os.path.join(self.d, "mods", "farm.jar"), make_jar(
            {"fabric.mod.json": '{"id": "farm", "name": "Farm Life"}', "assets/farm/blockstates/crop.json": "{}",
             "data/farm/dimension/fields.json": "{}"}))
        os.makedirs(os.path.join(self.d, "world", "dimensions", "farm", "fields"))
        os.makedirs(os.path.join(self.d, "world", "dimensions", "datapackns", "x"))
        p = C.plan(S, "paper")
        self.assertTrue(p["confirm_name"])
        self.assertEqual([m["file"] for m in p["content"]["mods"]], ["farm.jar"])
        self.assertEqual(p["content"]["dimensions"], ["farm"])
        with self.assertRaises(C.ConvertError):
            self.change("paper")
        with self.assertRaises(C.ConvertError):
            self.change("paper", confirm="minecraft-other")
        self.assertEqual(self.info()["loader"], "fabric")
        self.assertEqual(self.change("paper", confirm=S)["state"], "done")
        self.assertEqual(self.info()["loader"], "paper")

    def test_a_disabled_content_mod_doesnt_count(self):
        write(os.path.join(self.d, "mods", "farm.jar.disabled"), make_jar(
            {"fabric.mod.json": '{"id": "farm"}', "assets/farm/blockstates/crop.json": "{}"}))
        self.assertFalse(C.plan(S, "vanilla")["confirm_name"])

    def test_modrinth_says_content_and_equivalents(self):
        write(os.path.join(self.d, "mods", "luckperms.jar"), make_jar({"fabric.mod.json": '{"id": "luckperms"}'}))
        write(os.path.join(self.d, "mods", "create.jar"), make_jar({"fabric.mod.json": '{"id": "create"}'}))
        known = {"luckperms.jar": ({"source": "modrinth", "project": "LP"}, "h1"),
                 "create.jar": ({"source": "modrinth", "project": "CR"}, "h2")}
        projects = {"LP": {"id": "LP", "slug": "luckperms", "title": "LuckPerms", "categories": ["management"],
                           "client_side": "unsupported", "loaders": ["fabric", "paper", "bukkit"],
                           "game_versions": ["1.21.4"]},
                    "CR": {"id": "CR", "slug": "create", "title": "Create", "categories": ["technology"],
                           "client_side": "required", "loaders": ["fabric"], "game_versions": ["1.21.4"]}}
        with mock.patch.object(K, "_identify", lambda *a, **k: known), \
                mock.patch.object(C, "_projects", lambda ids: projects):
            p = C.plan(S, "paper")
            items = {i["file"]: i for i in p["addons"]["items"]}
            self.assertEqual(items["luckperms.jar"]["equivalent"]["project"], "LP")
            self.assertIsNone(items["create.jar"]["equivalent"])
            self.assertEqual(items["create.jar"]["content"], ["technology"])
            self.assertTrue(p["confirm_name"])
            self.assertIsNone({i["file"]: i for i in C.plan(S, "vanilla")["addons"]["items"]}
                              ["luckperms.jar"]["equivalent"])
            installed = []
            with mock.patch.object(K, "install", lambda server, source, project, actor="?":
                                   installed.append(project) or {"installed": [{"file": "LuckPerms.jar"}], "notes": []}):
                job = self.change("paper", confirm=S, install=["LP", "CR", "EVIL"])
            self.assertEqual(installed, ["LP"])                       # only what the plan offered
            self.assertIn("Installed LuckPerms.jar.", job["notes"])


class Plan(Base):
    def test_plan_changes_nothing(self):
        before = sorted(os.listdir(self.d))
        p = C.plan(S, "paper")
        self.assertTrue(p["ok"])
        self.assertEqual((p["from"]["name"], p["to"]["name"], p["to"]["minecraft"], p["to"]["build"]),
                         ("Vanilla", "Paper", "1.21.4", "232"))
        self.assertEqual(p["versions"], ["1.21.11", "1.21.4"])        # never older, only what Paper has
        self.assertEqual(p["java"], {"required": 21, "installed": True, "used": 21, "major": 21,
                                     "package": J.java_package(21)})
        self.assertEqual(p["addons"]["action"], "none")
        self.assertGreater(p["backup"]["needed"], 0)
        self.assertEqual(sorted(os.listdir(self.d)), before)
        self.assertIsNone(C.load(S)["job"])

    def test_refusals(self):
        with self.assertRaises(C.ConvertError) as e:
            C.plan(S, "vanilla")
        self.assertIn("Update Server", str(e.exception))
        with self.assertRaises(C.ConvertError):
            C.plan(S, "sponge")
        with self.assertRaises(C.ConvertError) as e:
            C.plan(S, "paper", "1.20.1")
        self.assertIn("can't go back", str(e.exception))
        with mock.patch.object(M, "_is_java", lambda s: False):
            with self.assertRaises(C.ConvertError) as e:
                C.plan(S, "paper")
            self.assertIn("Bedrock", str(e.exception))
        os.remove(os.path.join(self.d, J.INFO_FILE))
        with self.assertRaises(C.ConvertError) as e:                  # server.jar isn't a real jar
            C.plan(S, "paper")
        self.assertIn("can't tell", str(e.exception))

    def test_a_version_the_new_type_lacks_blocks_with_the_ones_it_has(self):
        p = C.plan(S, "paper", "26.1")
        self.assertFalse(p["ok"])
        self.assertIn("1.21.11", p["blockers"][0])
        with self.assertRaises(C.ConvertError):
            self.change("paper", "26.1")

    def test_no_room_for_the_backup_blocks(self):
        with mock.patch.object(C, "_disk", lambda s, d: {"needed": 5 << 30, "free": 1 << 30, "ok": False}):
            p = C.plan(S, "paper")
        self.assertFalse(p["ok"])
        self.assertIn("backup", p["blockers"][0])

    def test_missing_java_is_offered(self):
        with mock.patch.object(J, "installed_javas", lambda: {21: "/usr/lib/jvm/java-21/bin/java"}), \
                mock.patch.object(J, "java_major_for", lambda need: 25):
            p = C.plan(S, "fabric", "26.1")
            self.assertEqual((p["java"]["required"], p["java"]["installed"]), (25, False))
            self.assertTrue(p["ok"])
            with self.assertRaises(M.JavaMissing):
                self.change("fabric", "26.1")
        self.assertEqual(self.info()["loader"], "vanilla")
        self.assertIsNone(C.load(S)["job"])

    def test_leftover_off_folder_blocks(self):
        with mock.patch.object(J, "read_info", lambda d: {"loader": "fabric", "minecraft": "1.21.4"}):
            os.makedirs(os.path.join(self.d, "mods"))
            os.makedirs(os.path.join(self.d, "mods.off"))
            p = C.plan(S, "paper")
        self.assertFalse(p["ok"])
        self.assertIn("mods.off", p["blockers"][0])


class Change(Base):
    def test_vanilla_to_paper_and_undo(self):
        write(os.path.join(self.d, "world", "DIM-1", "region", "r.0.0.mca"), "nether")
        job = self.change("paper")
        self.assertEqual((job["state"], job["error"]), ("done", None))
        self.assertEqual([s["state"] for s in job["steps"]], ["done"] * 6)
        self.assertEqual(self.info(), {"loader": "paper", "minecraft": "1.21.4", "build": "232", "java_required": 21})
        self.assertEqual(read(os.path.join(self.d, "server.jar")), "jar of paper 1.21.4")
        self.assertEqual(read(os.path.join(self.d, C.PREVIOUS_JAR)), "jar of vanilla 1.21.4")
        self.assertEqual(json.loads(read(os.path.join(self.d, C.PREVIOUS_INFO)))["loader"], "vanilla")
        self.assertTrue(self.has("world_nether/DIM-1/region/r.0.0.mca"))      # Paper's own move
        self.assertTrue(self.running)
        kinds = [b["kind"] for b in B.list_backups(S)]
        self.assertEqual(kinds, ["pre-convert"])
        self.assertEqual(B.list_backups(S)[0]["made_by"]["label"], "Vanilla 1.21.4")
        self.assertEqual(self.sent[-1][0], "type_changed")
        self.assertFalse([n for n in os.listdir(self.d) if n.startswith(".redstonepanel-convert-")])
        st = C.status(S)
        self.assertEqual((st["current"]["name"], st["undo"]["kind"], st["undo"]["available"]),
                         ("Paper 1.21.4", "convert", True))
        self.assertEqual([t["id"] for t in st["types"]], ["vanilla", "purpur", "fabric", "quilt", "forge", "neoforge"])
        # built on Paper, then undone: the world from before is back, the Paper folders are gone
        write(os.path.join(self.d, "world", "region", "r.9.9.mca"), "built on paper")
        with self.assertRaises(C.ConvertError):
            C.start_undo(S, "update", wait=True)
        job = C.start_undo(S, "convert", actor="alex", wait=True, sleep=lambda s: None)
        self.assertEqual((job["state"], job["action"]), ("done", "undo"))
        self.assertEqual(self.info()["loader"], "vanilla")
        self.assertEqual(read(os.path.join(self.d, "server.jar")), "jar of vanilla 1.21.4")
        self.assertEqual(read(os.path.join(self.d, "world", "DIM-1", "region", "r.0.0.mca")), "nether")
        self.assertFalse(self.has("world/region/r.9.9.mca"))
        self.assertFalse(self.has("world_nether"))
        self.assertFalse(self.has(C.PREVIOUS_JAR))
        self.assertTrue(self.running)
        self.assertEqual(sorted(b["kind"] for b in B.list_backups(S)), ["pre-convert", "pre-restore"])
        self.assertIsNone(C.status(S)["undo"])
        with self.assertRaises(C.ConvertError):
            C.start_undo(S, "convert", wait=True)

    def test_new_log_with_the_old_inode_is_read_from_its_start(self):
        log = os.path.join(self.d, "logs", "latest.log")
        write(log, "[10:00:00] old log, stopped early\n")
        mark = C._log_mark(S)
        os.remove(log)                               # the new file usually gets the same inode
        write(log, '[10:05:00] Done (2.1s)! For help, type "help"\n' + "[10:05:01] more lines\n" * 5)
        self.assertIn("Done (2.1s)!", C._log_since(S, mark))
        mark = C._log_mark(S)
        with open(log, "a") as f:
            f.write("[10:06:00] appended\n")
        self.assertEqual(C._log_since(S, mark), "[10:06:00] appended\n")

    def test_paper_to_fabric_moves_world_and_sets_plugins_aside(self):
        with open(os.path.join(self.d, J.INFO_FILE), "w") as f:
            json.dump({"loader": "paper", "minecraft": "1.21.4", "build": "200", "java_required": 21}, f)
        write(os.path.join(self.d, "world_nether", "DIM-1", "region", "r.0.0.mca"), "nether")
        write(os.path.join(self.d, "world_the_end", "DIM1", "region", "r.0.0.mca"), "end")
        write(os.path.join(self.d, "plugins", "LuckPerms.jar"), make_jar({"plugin.yml": "name: LuckPerms\n"}))
        write(os.path.join(self.d, "plugins", "LuckPerms", "config.yml"), "x")
        write(os.path.join(self.d, K.TRACK_FILE), '{"files": {"LuckPerms.jar": {}}}')
        write(os.path.join(self.d, "mods.off", "lithium.jar"), "x")           # from an earlier Fabric
        job = self.change("fabric")
        self.assertEqual(job["state"], "done", job["error"])
        self.assertEqual(read(os.path.join(self.d, "world", "DIM-1", "region", "r.0.0.mca")), "nether")
        self.assertEqual(read(os.path.join(self.d, "world", "DIM1", "region", "r.0.0.mca")), "end")
        self.assertTrue(self.has("plugins.off/LuckPerms/config.yml"))
        self.assertFalse(self.has("plugins"))
        self.assertTrue(self.has("mods/lithium.jar"))
        self.assertFalse(self.has("mods.off"))
        self.assertTrue(self.has(".redstonepanel-mods.paper.json"))
        self.assertFalse(self.has(K.TRACK_FILE))
        # Fabric installs something of its own, tracked in a new file
        write(os.path.join(self.d, K.TRACK_FILE), '{"files": {"fabric-api.jar": {}}}')
        # ...and undo puts every one of them back
        job = C.start_undo(S, "convert", wait=True, sleep=lambda s: None)
        self.assertEqual(job["state"], "done", job["error"])
        self.assertTrue(self.has("plugins/LuckPerms.jar"))
        self.assertTrue(self.has("mods.off/lithium.jar"))
        self.assertIn("LuckPerms.jar", read(os.path.join(self.d, K.TRACK_FILE)))
        self.assertIn("fabric-api.jar", read(os.path.join(self.d, ".redstonepanel-mods.fabric.json")))
        self.assertFalse(self.has(".redstonepanel-mods.paper.json"))
        # the "Before restore" backup holds the world Fabric wrote, and says so
        pre = [b for b in B.list_backups(S) if b["kind"] == "pre-restore"][0]
        self.assertEqual(pre["made_by"]["label"], "Fabric 1.21.4")
        self.assertTrue(self.has("world_nether/DIM-1/region/r.0.0.mca"))
        self.assertFalse(self.has("world/DIM-1"))
        self.assertEqual(self.info()["build"], "200")
        # changing to Fabric again replaces the stale tracking file instead of stopping on it
        job = self.change("fabric")
        self.assertEqual(job["state"], "done", job["error"])
        self.assertIn("fabric-api.jar", read(os.path.join(self.d, K.TRACK_FILE)))

    def test_type_and_version_together_changes_java(self):
        job = self.change("fabric", "26.1")
        self.assertEqual(job["state"], "done", job["error"])
        self.assertEqual((self.info()["loader"], self.info()["minecraft"]), ("fabric", "26.1"))
        self.assertEqual(self.units_written[-1], ("/usr/lib/jvm/java-25/bin/java", {"xms": "1G", "xmx": "2G"}))
        with mock.patch.object(os, "access", lambda p, m: True):
            C.start_undo(S, wait=True, sleep=lambda s: None)
        self.assertEqual(self.units_written[-1][0], "/usr/lib/jvm/java-21/bin/java")

    def test_a_failed_start_puts_everything_back(self):
        self.starts = False
        write(os.path.join(self.d, "world", "DIM-1", "region", "r.0.0.mca"), "nether")
        job = self.change("fabric")
        self.assertEqual(job["state"], "failed")
        self.assertTrue(job["rolled_back"])
        self.assertIn("stopped while starting", job["error"])
        self.assertIn("back on Vanilla 1.21.4", job["error"])
        self.assertTrue(any("fabric-api" in l for l in job["why"]["lines"]))
        self.assertEqual(self.info()["loader"], "vanilla")
        self.assertEqual(read(os.path.join(self.d, "server.jar")), "jar of vanilla 1.21.4")
        self.assertEqual(read(os.path.join(self.d, "world", "region", "r.0.0.mca")), "overworld")
        self.assertTrue(self.running)
        self.assertIsNone(C.status(S)["undo"])
        self.assertEqual(self.sent[-1][0], "update_failed")
        self.assertEqual(sorted(b["kind"] for b in B.list_backups(S)), ["pre-convert", "pre-restore"])

    def test_a_failed_download_or_backup_changes_nothing(self):
        def boom(*a, **k):
            raise J.JavaServerError("Couldn't reach fill.papermc.io.")
        with mock.patch.object(J, "install", boom):
            job = self.change("paper")
        self.assertEqual(job["state"], "failed")
        self.assertIn("nothing was changed", job["error"])
        self.assertIsNone(job["rolled_back"])
        self.assertEqual(self.commands, [])                           # never stopped
        with mock.patch.object(B, "run_backup", mock.Mock(side_effect=B.BackupError("disk full"))):
            job = self.change("paper")
        self.assertIn("nothing was changed", job["error"])
        self.assertEqual(self.info()["loader"], "vanilla")
        self.assertTrue(self.running)                                 # started again
        self.assertFalse(self.has(C.PREVIOUS_JAR))

    def test_a_stopped_server_is_checked_and_stopped_again(self):
        self.running = False
        job = self.change("paper")
        self.assertEqual(job["state"], "done", job["error"])
        self.assertFalse(self.running)
        self.assertTrue(any("start" in c for c in self.commands))

    def test_one_change_at_a_time_and_interrupted_jobs(self):
        t = mock.Mock(is_alive=lambda: True)
        with mock.patch.dict(C._threads, {S: t}):
            with self.assertRaises(C.ConvertError) as e:
                C._launch(S, lambda: None)
            self.assertEqual(e.exception.status, 409)
        C._Job(S, "convert", "alex", {}, {})
        self.assertEqual(C.status(S)["job"]["state"], "failed")       # no thread: the panel restarted

    def test_undo_needs_its_backup_and_the_same_server(self):
        self.change("paper")
        os.remove(os.path.join(B.server_backup_dir(S), B.list_backups(S)[0]["file"]))
        u = C.status(S)["undo"]
        self.assertFalse(u["available"])
        self.assertIn("deleted", u["reason"])
        with self.assertRaises(C.ConvertError):
            C.start_undo(S, wait=True)
        with open(os.path.join(self.d, J.INFO_FILE), "w") as f:      # changed by something else since
            json.dump({"loader": "paper", "minecraft": "1.21.11"}, f)
        self.assertIsNone(C.status(S)["undo"])


class NewTypes(Base):
    """1.20: Purpur, Quilt, Forge and NeoForge in type changes."""
    loader = "fabric"

    def _install(self, d, loader, minecraft=None, current=None, **kw):
        if loader not in J.INSTALLER_LOADERS:
            return super()._install(d, loader, minecraft, current, **kw)
        launch = {"quilt": "-jar quilt-server-launch.jar"}.get(loader, f"@libraries/{loader}/unix_args.txt")
        write(os.path.join(d, "libraries", loader, "unix_args.txt"), f"args of {loader} {minecraft}")
        if loader == "quilt":
            write(os.path.join(d, "quilt-server-launch.jar"), f"quilt {minecraft}")
            write(os.path.join(d, "server.jar"), f"vanilla jar for quilt {minecraft}")
        info = {"loader": loader, "minecraft": minecraft, "build": kw.get("build") or "0.29.1", "java_required": 21,
                "launch": launch}
        write(os.path.join(d, J.INFO_FILE), json.dumps(info))
        return info, "/usr/lib/jvm/java-21/bin/java"

    def test_fabric_to_quilt_keeps_the_mods_and_undo_brings_the_jar_back(self):
        write(os.path.join(self.d, "mods", "lithium.jar"), make_jar({"fabric.mod.json": '{"id": "lithium"}'}))
        write(os.path.join(self.d, K.TRACK_FILE), '{"files": {"lithium.jar": {}}}')
        p = C.plan(S, "quilt")
        self.assertTrue(p["ok"], p["blockers"])
        self.assertEqual((p["addons"]["action"], p["confirm_name"], p["world"]["moves"]), ("keep", False, []))
        self.assertIn("stay where they are", p["addons"]["note"])
        job = self.change("quilt")
        self.assertEqual(job["state"], "done", job["error"])
        self.assertEqual(self.info()["launch"], "-jar quilt-server-launch.jar")
        self.assertTrue(self.has("mods/lithium.jar") and self.has(K.TRACK_FILE) and self.has("quilt-server-launch.jar"))
        self.assertFalse(self.has("mods.off"))
        self.assertEqual(read(os.path.join(self.d, C.PREVIOUS_JAR)), "jar of fabric 1.21.4")
        self.assertTrue(self.units_written, "the unit must start Quilt's own jar")
        job = C.start_undo(S, "convert", actor="alex", wait=True, sleep=lambda s: None)
        self.assertEqual(job["state"], "done", job["error"])
        self.assertEqual(self.info()["loader"], "fabric")
        self.assertEqual(read(os.path.join(self.d, "server.jar")), "jar of fabric 1.21.4")
        self.assertFalse(self.has("quilt-server-launch.jar"))
        self.assertTrue(self.has("mods/lithium.jar") and self.has(K.TRACK_FILE))

    def test_quilt_to_fabric_and_undo_runs_the_installer_again(self):
        self.change("quilt")
        job = self.change("fabric")
        self.assertEqual(job["state"], "done", job["error"])
        self.assertFalse(self.has("quilt-server-launch.jar") or self.has(C.PREVIOUS_JAR))
        u = C.status(S)["undo"]
        self.assertEqual((u["from"]["name"], u["available"]), ("Quilt 1.21.4", True))
        job = C.start_undo(S, "convert", actor="alex", wait=True, sleep=lambda s: None)
        self.assertEqual(job["state"], "done", job["error"])
        self.assertEqual((self.info()["loader"], self.info()["build"]), ("quilt", "0.29.1"))
        self.assertTrue(self.has("quilt-server-launch.jar"))

    def test_fabric_to_neoforge_sets_the_mods_aside(self):
        write(os.path.join(self.d, "mods", "lithium.jar"), make_jar({"fabric.mod.json": '{"id": "lithium"}'}))
        p = C.plan(S, "neoforge")
        self.assertEqual((p["addons"]["action"], p["addons"]["restore"]), ("off", None))
        job = self.change("neoforge")
        self.assertEqual(job["state"], "done", job["error"])
        self.assertTrue(self.has("mods.off/lithium.jar") and self.has("libraries/neoforge/unix_args.txt"))
        self.assertFalse(self.has("mods/lithium.jar"))

    def test_paper_to_purpur_keeps_plugins_and_the_world_where_they_are(self):
        with open(os.path.join(self.d, J.INFO_FILE), "w") as f:
            json.dump({"loader": "paper", "minecraft": "1.21.4", "build": "200", "java_required": 21}, f)
        write(os.path.join(self.d, "world_nether", "DIM-1", "region", "r.0.0.mca"), "nether")
        write(os.path.join(self.d, "plugins", "LuckPerms.jar"), make_jar({"plugin.yml": "name: LuckPerms\n"}))
        p = C.plan(S, "purpur")
        self.assertEqual((p["addons"]["action"], p["world"]["moves"], p["blockers"]), ("keep", [], []))
        job = self.change("purpur")
        self.assertEqual(job["state"], "done", job["error"])
        self.assertTrue(self.has("plugins/LuckPerms.jar") and self.has("world_nether/DIM-1/region/r.0.0.mca"))
        self.assertEqual(self.info()["loader"], "purpur")

    def test_a_modpack_server_keeps_its_type(self):
        write(os.path.join(self.d, ".redstonepanel-modpack.json"), '{"kind": "mrpack", "name": "Adrenaserver", "version": "1"}')
        with self.assertRaises(C.ConvertError) as e:
            C.plan(S, "quilt")
        self.assertIn("modpack", str(e.exception))


class UpdateRollback(Base):
    def setUp(self):
        super().setUp()
        for x in (mock.patch.object(M, "_install_java", lambda d, loader, version, current, **k:
                                    self._install(d, loader, version or "1.21.11")),
                  mock.patch.object(M.subprocess, "run", lambda *a, **k: mock.Mock(stdout="", returncode=0))):
            x.start()
            self.addCleanup(x.stop)
        from core import updates
        p = mock.patch.object(updates, "forget", lambda s: None)
        p.start()
        self.addCleanup(p.stop)

    def test_update_backs_up_and_can_be_rolled_back(self):
        M.update_server_binary(S, "1.21.11", actor="alex")
        self.assertEqual(self.info()["minecraft"], "1.21.11")
        self.assertEqual([b["kind"] for b in B.list_backups(S)], ["pre-update"])
        u = C.status(S)["undo"]
        self.assertEqual((u["kind"], u["from"]["name"], u["to"]["name"], u["by"], u["available"]),
                         ("update", "Vanilla 1.21.4", "Vanilla 1.21.11", "alex", True))
        with self.assertRaises(C.ConvertError):
            C.start_undo(S, "convert", wait=True)
        write(os.path.join(self.d, "world", "region", "r.0.0.mca"), "upgraded by 1.21.11")
        job = C.start_undo(S, "update", actor="alex", wait=True, sleep=lambda s: None)
        self.assertEqual((job["state"], job["action"]), ("done", "rollback"))
        self.assertEqual(self.info()["minecraft"], "1.21.4")
        self.assertEqual(read(os.path.join(self.d, "server.jar")), "jar of vanilla 1.21.4")
        self.assertEqual(read(os.path.join(self.d, "world", "region", "r.0.0.mca")), "overworld")
        # the world 1.21.11 wrote is kept as "Before restore", and can't be restored onto 1.21.4
        pre = [b for b in B.list_backups(S) if b["kind"] == "pre-restore"][0]
        self.assertEqual(pre["made_by"]["label"], "Vanilla 1.21.11")
        with self.assertRaises(B.BackupError):
            B.restore_backup(S, pre["file"])

    def test_update_without_a_backup_cant_be_rolled_back(self):
        M.update_server_binary(S, "1.21.11", backup=False)
        self.assertEqual(B.list_backups(S), [])
        u = C.status(S)["undo"]
        self.assertFalse(u["available"])
        self.assertIn("no backup", u["reason"])

    def test_a_failed_backup_stops_the_update(self):
        with mock.patch.object(B, "run_backup", mock.Mock(side_effect=B.BackupError("disk full"))):
            with self.assertRaises(M.ServerError):
                M.update_server_binary(S, "1.21.11")
        self.assertEqual(self.info()["minecraft"], "1.21.4")
        self.assertTrue(self.running)


class Manifest(Base):
    def test_backups_say_what_made_them(self):
        b = B.run_backup(S, "manual")
        path = B.backup_path(S, b["file"])
        with tarfile.open(path) as tar:
            first = tar.next()
            self.assertEqual(first.name, B.MANIFEST)
            m = json.loads(tar.extractfile(first).read())
        self.assertEqual((m["edition"], m["loader"], m["minecraft"], m["kind"]), ("java", "vanilla", "1.21.4", "manual"))
        self.assertEqual(b["made_by"]["label"], "Vanilla 1.21.4")
        self.assertFalse(self.has(B.MANIFEST))

    def test_older_backups_are_read_from_their_server_info(self):
        os.makedirs(B.server_backup_dir(S))
        old = os.path.join(B.server_backup_dir(S), f"{S}-20260101-000000-manual.tar.gz")
        with tarfile.open(old, "w:gz") as tar:
            for name, data in (("world", None), (J.INFO_FILE, b'{"loader": "paper", "minecraft": "1.21.4"}'),
                               ("world/level.dat", b"x")):
                ti = tarfile.TarInfo(name)
                if data is None:
                    ti.type = tarfile.DIRTYPE
                    tar.addfile(ti)
                else:
                    ti.size = len(data)
                    tar.addfile(ti, io.BytesIO(data))
        self.assertEqual(B.made_by(old)["label"], "Paper 1.21.4")
        bare = os.path.join(B.server_backup_dir(S), f"{S}-20250101-000000-manual.tar.gz")
        with tarfile.open(bare, "w:gz") as tar:
            ti = tarfile.TarInfo("world/level.dat")
            ti.size = 1
            tar.addfile(ti, io.BytesIO(b"x"))
        self.assertIsNone(B.made_by(bare))
        self.assertIsNone(B.made_by(os.path.join(self.root, "missing.tar.gz")))

    def test_a_newer_world_isnt_restored_onto_an_older_server(self):
        with open(os.path.join(self.d, J.INFO_FILE), "w") as f:
            json.dump({"loader": "vanilla", "minecraft": "1.21.11"}, f)
        newer = B.run_backup(S, "manual")
        with open(os.path.join(self.d, J.INFO_FILE), "w") as f:
            json.dump({"loader": "vanilla", "minecraft": "1.21.4"}, f)
        with self.assertRaises(B.BackupError) as e:
            B.restore_backup(S, newer["file"])
        self.assertIn("1.21.11", str(e.exception))
        self.assertEqual(self.commands, [])                           # refused before stopping anything
        with open(os.path.join(self.d, J.INFO_FILE), "w") as f:
            json.dump({"loader": "paper", "minecraft": "26.1"}, f)
        self.assertEqual(B.restore_backup(S, newer["file"])["restored"], ["world"])


class PermissionRulesTest(unittest.TestCase):
    """1.18's per-server permissions: seeing the type needs View, changing it needs an admin."""

    def test_rules(self):
        import core.grants as grants
        S = "/api/servers/{server_name}"
        self.assertEqual(grants.rule_for("GET", S + "/convert"), "view")
        for path in ("/convert", "/convert/undo", "/update/rollback"):
            self.assertEqual(grants.rule_for("POST", S + path), "admin")


if __name__ == "__main__":
    unittest.main()
