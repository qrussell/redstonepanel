"""Player packs (1.21.6): which mods players get, the .mrpack and packwiz files, and how the
join page hands them out."""
import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest
import zipfile
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="rp-test-pack-")
os.environ.setdefault("RP_DATA_DIR", os.path.join(_TMP, "data"))
os.environ.setdefault("RP_LOG_FILE", os.path.join(_TMP, "panel.log"))

from core import files                # noqa: E402
from core import javaservers          # noqa: E402
from core import joinpage as J        # noqa: E402
from core import joinsite as S        # noqa: E402
from core import manager              # noqa: E402
from core import modrinth             # noqa: E402
from core import mods                 # noqa: E402
from core import playerpack as K      # noqa: E402
from core import portforward          # noqa: E402
from core import settings as P        # noqa: E402
from core import sources              # noqa: E402

SERVER = "minecraft-modded"


def jar(mod_id, name=None, payload=b""):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("fabric.mod.json", json.dumps({"id": mod_id, "name": name or mod_id, "version": "1.0"}))
        z.writestr("x.bin", payload)
    return buf.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rp-pack-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.server = os.path.join(self.dir, SERVER)
        os.makedirs(os.path.join(self.server, "mods"))
        self.data = os.path.join(self.dir, "data")
        self.known = {}              # sha512 -> (project id, client_side, server_side, title)
        self.info = {"loader": "fabric", "minecraft": "1.21.1", "build": "loader 0.16.9"}
        self.modrinth_up = True
        self.settings = dict(P.DEFAULTS, join_enabled=True)
        for p in (mock.patch.object(manager, "DATA_DIR", self.data),
                  mock.patch.object(manager, "_check_server", lambda s: None),
                  mock.patch.object(manager, "get_server_dir", lambda s: self.server),
                  mock.patch.object(manager, "write_log", lambda *a, **k: None),
                  mock.patch.object(mods, "server_kind", lambda s: (self.info["loader"], self.info["minecraft"])),
                  mock.patch.object(javaservers, "read_info", lambda d: dict(self.info)),
                  mock.patch.object(modrinth, "by_hashes", self._by_hashes),
                  mock.patch.object(modrinth, "project_sides", self._sides),
                  mock.patch.object(P, "load", lambda: dict(self.settings)),
                  mock.patch.object(portforward, "load", lambda: {"public_address": "play.example.org"})):
            p.start()
            self.addCleanup(p.stop)
        for c in (K._manifests, K._packs, K._hash_cache, mods._meta_cache, J._summary_cache, J._pack_cache, S._rate):
            c.clear()

    def put(self, name, mod_id, client="required", server="required", on_modrinth=True, payload=b""):
        data = jar(mod_id, mod_id.title(), payload)
        with open(os.path.join(self.server, "mods", name), "wb") as f:
            f.write(data)
        if on_modrinth:
            self.known[hashlib.sha512(data).hexdigest()] = (f"P{mod_id}", client, server, mod_id.title(), name, data)
        return data

    def _by_hashes(self, hashes):
        if not self.modrinth_up:
            raise sources.SourceError("Couldn't reach api.modrinth.com", 502)
        out = {}
        for h in hashes:
            if h in self.known:
                pid, _, _, _, name, data = self.known[h]
                out[h] = {"id": "V" + pid[1:5], "project_id": pid, "version_number": "1.0",
                          "files": [{"primary": True, "filename": name, "size": len(data),
                                     "url": f"https://cdn.modrinth.com/data/{pid}/versions/v/{name}",
                                     "hashes": {"sha1": hashlib.sha1(data).hexdigest(), "sha512": h}}]}
        return out

    def _sides(self, ids):
        return {k[0]: {"slug": k[0][1:].lower(), "title": k[3], "client_side": k[1], "server_side": k[2]}
                for k in self.known.values() if k[0] in ids}

    def states(self, **kw):
        return {l["file"]: l["state"] for l in K.manifest(SERVER, **kw)["mods"]}


class Sorting(Base):
    def test_sides_decide(self):
        self.put("sodium.jar", "sodium", "required", "unsupported")
        self.put("lithium.jar", "lithium", "optional", "required")
        self.put("spark.jar", "spark", "unsupported", "required")
        self.put("create.jar", "create", "required", "required")
        self.put("odd.jar", "odd", "unknown", "unknown")
        self.put("homemade.jar", "homemade", on_modrinth=False)
        self.put("off.jar.disabled", "off")
        m = K.manifest(SERVER)
        self.assertEqual(self.states(), {"sodium.jar": "in", "lithium.jar": "optional", "spark.jar": "out",
                                         "create.jar": "in", "odd.jar": "in", "homemade.jar": "out"})
        self.assertEqual(m["not_on_modrinth"], ["Homemade"])
        self.assertEqual(m["counts"], {"in": 3, "optional": 1, "bundled": 0, "out": 2})
        self.assertEqual(m["loader_version"], "0.16.9")
        self.assertTrue(m["active"])

    def test_what_a_required_mod_needs_is_required(self):
        def with_deps(name, mod_id, depends, client):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                z.writestr("fabric.mod.json", json.dumps({"id": mod_id, "name": mod_id.title(), "version": "1",
                                                          "depends": {d: "*" for d in depends}}))
            data = buf.getvalue()
            with open(os.path.join(self.server, "mods", name), "wb") as f:
                f.write(data)
            self.known[hashlib.sha512(data).hexdigest()] = (f"P{mod_id}", client, "required", mod_id.title(), name, data)
        with_deps("create.jar", "create", ["flywheel", "minecraft"], "required")
        with_deps("flywheel.jar", "flywheel", ["fabric-api"], "optional")
        with_deps("fabric-api.jar", "fabric-api", [], "optional")
        with_deps("lithium.jar", "lithium", [], "optional")
        m = K.manifest(SERVER)
        self.assertEqual({l["file"]: l["state"] for l in m["mods"]},
                         {"create.jar": "in", "flywheel.jar": "in", "fabric-api.jar": "in", "lithium.jar": "optional"})
        self.assertEqual(next(l["why"] for l in m["mods"] if l["file"] == "fabric-api.jar"), "Needed by Flywheel")
        K.save_config(SERVER, {"mods": {"flywheel.jar": "optional"}}, "admin")      # the admin's word stands
        self.assertEqual(self.states()["flywheel.jar"], "optional")
        self.assertEqual(self.states()["fabric-api.jar"], "optional")

    def test_admin_overrules(self):
        self.put("spark.jar", "spark", "unsupported", "required")
        self.put("create.jar", "create")
        self.put("homemade.jar", "homemade", on_modrinth=False)
        before = K.manifest(SERVER)["version"]
        K.save_config(SERVER, {"mods": {"spark.jar": "in", "create.jar": "out", "homemade.jar": "in"}}, "admin")
        self.assertEqual(self.states(), {"spark.jar": "in", "create.jar": "out", "homemade.jar": "bundled"})
        self.assertNotEqual(K.manifest(SERVER)["version"], before)
        self.assertEqual(K.manifest(SERVER)["not_on_modrinth"], [])
        K.save_config(SERVER, {"mods": {"spark.jar": "auto"}}, "admin")
        self.assertEqual(self.states()["spark.jar"], "out")
        for bad in ({"mods": {"../x.jar": "in"}}, {"mods": {"a.jar": "maybe"}}, {"mode": "sometimes"}, []):
            with self.assertRaises(K.PackError, msg=bad):
                K.save_config(SERVER, bad, "admin")

    def test_version_follows_the_folder(self):
        self.put("create.jar", "create")
        v1 = K.manifest(SERVER)["version"]
        self.assertEqual(K.manifest(SERVER)["version"], v1)
        self.put("sodium.jar", "sodium")
        v2 = K.manifest(SERVER)["version"]
        self.assertNotEqual(v1, v2)
        os.remove(os.path.join(self.server, "mods", "sodium.jar"))
        self.assertEqual(K.manifest(SERVER)["version"], v1)

    def test_modrinth_down(self):
        self.put("create.jar", "create")
        good = K.manifest(SERVER)
        self.modrinth_up = False
        self.assertEqual(K.manifest(SERVER, fresh=True)["version"], good["version"])    # what we knew holds
        self.put("new.jar", "new")
        m = K.manifest(SERVER)
        self.assertFalse(m["looked_up"])
        self.assertTrue(m["problems"])
        self.assertEqual(m["not_on_modrinth"], [])           # unknown isn't "not on Modrinth"
        self.modrinth_up = True
        self.assertEqual(self.states(), {"create.jar": "in", "new.jar": "in"})

    def test_mode(self):
        self.put("create.jar", "create")
        with open(os.path.join(self.server, ".redstonepanel-modpack.json"), "w") as f:
            json.dump({"kind": "mrpack", "name": "Big Pack", "project": "abc", "files": []}, f)
        self.assertFalse(K.manifest(SERVER)["active"])       # the Modrinth pack is the better link
        self.assertIsNone(K.public_part(K.manifest(SERVER)))
        K.save_config(SERVER, {"mode": "on"}, "admin")
        self.assertTrue(K.manifest(SERVER)["active"])
        K.save_config(SERVER, {"mode": "off"}, "admin")
        self.assertEqual(K.local_pack(SERVER), (None, None))

    def test_only_mod_servers(self):
        self.info["loader"] = "paper"
        with self.assertRaises(K.PackError):
            K.manifest(SERVER)

    def test_loader_versions(self):
        self.assertEqual(K._loader_version({"build": "loader 0.16.9"}, "1.21.1"), "0.16.9")
        self.assertEqual(K._loader_version({"build": "1.20.1-47.3.0"}, "1.20.1"), "47.3.0")
        self.assertEqual(K._loader_version({"build": "21.1.200"}, "1.21.1"), "21.1.200")
        self.assertEqual(K._loader_version({"build": None}, "1.21.1"), "")
        self.assertEqual(K._loader_version({"build": "x y\n"}, "1.21.1"), "")


class Extras(Base):
    def setUp(self):
        super().setUp()
        data = jar("iris")
        self.iris = data
        pick = {"project": "Piris", "slug": "iris", "title": "Iris Shaders", "version_id": "Viris", "version": "1.8",
                "file": {"url": "x"}}

        def resolve(project, kind, minecraft):
            if project != "iris":
                raise sources.SourceError(f"Modrinth has no project '{project}'.", 404)
            return pick
        version = {"id": "Viris", "project_id": "Piris", "files": [{
            "primary": True, "filename": "iris-1.8.jar", "size": len(data),
            "url": "https://cdn.modrinth.com/data/Piris/versions/Viris/iris-1.8.jar",
            "hashes": {"sha1": hashlib.sha1(data).hexdigest(), "sha512": hashlib.sha512(data).hexdigest()}}]}
        for p in (mock.patch.object(modrinth, "resolve", resolve),
                  mock.patch.object(sources, "get_json", lambda url, **k: version)):
            p.start()
            self.addCleanup(p.stop)

    def test_add_and_remove(self):
        self.put("create.jar", "create")
        K.save_config(SERVER, {"add_extra": "https://modrinth.com/mod/iris"}, "admin")
        m = K.manifest(SERVER)
        self.assertEqual([(x["name"], x["state"]) for x in m["extras"]], [("Iris Shaders", "optional")])
        index = json.loads(zipfile.ZipFile(io.BytesIO(K.local_pack(SERVER)[1])).read("modrinth.index.json"))
        iris = next(f for f in index["files"] if f["path"] == "mods/iris-1.8.jar")
        self.assertEqual(iris["env"], {"client": "optional", "server": "unsupported"})
        with self.assertRaises(files.FileError):
            K.save_config(SERVER, {"add_extra": "nothing-here"}, "admin")
        with self.assertRaises(K.PackError):
            K.save_config(SERVER, {"add_extra": "bad name!/.."}, "admin")
        K.save_config(SERVER, {"remove_extra": "iris"}, "admin")
        self.assertEqual(K.manifest(SERVER)["extras"], [])

    def test_extra_the_server_already_has(self):
        self.put("iris.jar", "iris")
        K.save_config(SERVER, {"add_extra": "iris"}, "admin")
        self.assertEqual(K.manifest(SERVER)["extras"][0]["state"], "out")


class Files(Base):
    def setUp(self):
        super().setUp()
        self.put("sodium.jar", "sodium", "required", "unsupported")
        self.put("lithium.jar", "lithium", "optional", "required")
        self.put("spark.jar", "spark", "unsupported", "required")
        self.home = self.put("homemade.jar", "homemade", on_modrinth=False, payload=b"mine" * 50)
        K.save_config(SERVER, {"mods": {"homemade.jar": "in"}, "name": 'Family "Pack"'}, "admin")

    def test_mrpack(self):
        p, data = K.local_pack(SERVER, "ignored")
        z = zipfile.ZipFile(io.BytesIO(data))
        index = json.loads(z.read("modrinth.index.json"))
        self.assertEqual((index["formatVersion"], index["game"], index["name"]), (1, "minecraft", 'Family "Pack"'))
        self.assertEqual(index["versionId"], p["version"])
        self.assertEqual(index["dependencies"], {"minecraft": "1.21.1", "fabric-loader": "0.16.9"})
        by = {f["path"]: f for f in index["files"]}
        self.assertEqual(sorted(by), ["mods/lithium.jar", "mods/sodium.jar"])
        self.assertEqual(by["mods/lithium.jar"]["env"], {"client": "optional", "server": "required"})
        self.assertEqual(by["mods/sodium.jar"]["env"], {"client": "required", "server": "unsupported"})
        for f in index["files"]:
            self.assertTrue(f["downloads"][0].startswith("https://cdn.modrinth.com/"))
            self.assertEqual(len(f["hashes"]["sha1"]), 40)
            self.assertEqual(len(f["hashes"]["sha512"]), 128)
        self.assertEqual(z.read("overrides/mods/homemade.jar"), self.home)
        self.assertEqual(K.local_pack(SERVER)[1], data)                 # same folder, same file
        # the panel's own pack reader takes it (server-side files only)
        from core import modpacks
        path = os.path.join(self.dir, "p.mrpack")
        with open(path, "wb") as f:
            f.write(data)
        plan = modpacks.read_mrpack(path)
        self.assertEqual((plan["loader"], plan["loader_version"], plan["client_only"]), ("fabric", "0.16.9", 1))

    def test_packwiz(self):
        import tomllib
        p, data = K.local_pack(SERVER)
        out = K.packwiz(K.clean_public(p), "Family Pack", data)
        pack = tomllib.loads(out["pack.toml"].decode())
        index = tomllib.loads(out["index.toml"].decode())
        self.assertEqual(pack["versions"], {"minecraft": "1.21.1", "fabric": "0.16.9"})
        self.assertEqual(pack["index"]["hash"], hashlib.sha256(out["index.toml"]).hexdigest())
        self.assertEqual(sorted(f["file"] for f in index["files"]),
                         ["mods/homemade.jar", "mods/lithium.pw.toml", "mods/sodium.pw.toml"])
        for f in index["files"]:
            self.assertEqual(f["hash"], hashlib.sha256(out[f["file"]]).hexdigest())
            self.assertEqual(bool(f.get("metafile")), f["file"].endswith(".pw.toml"))
        lithium = tomllib.loads(out["mods/lithium.pw.toml"].decode())
        self.assertEqual(lithium["option"], {"optional": True, "default": True})
        self.assertEqual(lithium["side"], "both")
        self.assertEqual(lithium["download"]["hash-format"], "sha512")
        self.assertEqual(lithium["update"]["modrinth"]["mod-id"], "Plithium")
        self.assertEqual(tomllib.loads(out["mods/sodium.pw.toml"].decode())["side"], "client")
        self.assertEqual(out["mods/homemade.jar"], self.home)

    def test_a_nodes_answer_is_checked(self):
        p = K.public_part(K.manifest(SERVER))
        bad = json.loads(json.dumps(p))
        by = {l["filename"]: l for l in bad["mods"]}
        by["sodium.jar"]["url"] = "https://evil.example/x.jar"
        by["lithium.jar"]["filename"] = "../../up.jar"
        by["homemade.jar"]["url"] = "https://evil.example/y.jar"
        bad["name"] = "x\n<script>"
        got = K.clean_public(bad)
        self.assertEqual({l["filename"]: l["url"][:25] for l in got["mods"]},
                         {"up.jar": "https://cdn.modrinth.com/", "homemade.jar": ""})
        self.assertNotIn("\n", got["name"])
        self.assertIsNone(K.clean_public({"loader": "paper", "mods": []}))
        self.assertIsNone(K.clean_public("x"))
        swapped = K.mrpack(K.clean_public(p), "T", lambda name: b"something else")
        self.assertNotIn("overrides/mods/homemade.jar", zipfile.ZipFile(io.BytesIO(swapped)).namelist())


class Page(Base):
    def setUp(self):
        super().setUp()
        self.put("sodium.jar", "sodium", "required", "unsupported")
        self.put("lithium.jar", "lithium", "optional", "required")
        self.put("homemade.jar", "homemade", on_modrinth=False)
        listed = [{"name": SERVER, "edition": "Java", "loader": "fabric", "minecraft": "1.21.1",
                   "status": "offline", "port": "25565", "motd": "Modded <Fun>", "directory": self.server}]
        for p in (mock.patch.object(manager, "list_servers", lambda: listed),
                  mock.patch.object(manager, "read_properties", lambda d: {"server-port": "25565"}),
                  mock.patch.object(portforward, "java_extras", lambda *a: [])):
            p.start()
            self.addCleanup(p.stop)
        J.save_page("local", SERVER, {"enabled": True}, "admin")

    def get(self, path, method="GET"):
        return S.handle(method, path, "203.0.113.9")

    def test_page_offers_the_pack(self):
        a = self.get("/join/modded")
        self.assertEqual(a.status, 200)
        page = a.body.decode()
        self.assertIn('href="/join/modded/pack.mrpack"', page)
        self.assertIn("2 mods, 1 of them optional", page)
        self.assertIn("https://modrinth.com/mod/sodium", page)
        self.assertIn("Not in the pack", page)
        self.assertIn("Homemade", page)
        self.assertIn("/join/modded/packwiz/pack.toml", page)
        self.assertNotIn("<Fun>", page)

    def test_downloads(self):
        a = self.get("/join/modded/pack.mrpack")
        self.assertEqual(a.status, 200)
        heads = dict(a.headers())
        self.assertEqual(heads[b"content-type"], b"application/x-modrinth-modpack+zip")
        self.assertRegex(heads[b"content-disposition"].decode(), r'^attachment; filename="Modded-Fun-[0-9a-f]{8}\.mrpack"$')
        index = json.loads(zipfile.ZipFile(io.BytesIO(a.body)).read("modrinth.index.json"))
        self.assertEqual(len(index["files"]), 2)
        self.assertEqual(self.get("/join/modded/packwiz/pack.toml").status, 200)
        self.assertEqual(self.get("/join/modded/packwiz/index.toml").status, 200)
        self.assertEqual(self.get("/join/modded/packwiz/mods/sodium.pw.toml").status, 200)
        for bad in ("/join/modded/packwiz/mods/../pack.toml", "/join/modded/packwiz/mods/a/b",
                    "/join/modded/packwiz/nope.toml", "/join/modded/packwiz", "/join/modded/pack.mrpack/x",
                    "/join/modded/mods/sodium.jar"):
            self.assertEqual(self.get(bad).status, 404, bad)
        self.assertEqual(self.get("/join/modded/pack.mrpack", "POST").status, 404)

    def test_off_means_gone(self):
        K.save_config(SERVER, {"mode": "off"}, "admin")
        J._summary_cache.clear()
        self.assertEqual(self.get("/join/modded/pack.mrpack").status, 404)
        self.assertEqual(self.get("/join/modded/packwiz/pack.toml").status, 404)
        self.assertNotIn("pack.mrpack", self.get("/join/modded").body.decode())

    def test_a_nodes_server(self):
        p = K.public_part(K.manifest(SERVER))
        node = {"name": SERVER, "edition": "Java", "loader": "fabric", "minecraft": "1.21.1", "status": "offline",
                "port": "25565", "motd": "On a node", "transport": None, "geyser_port": "", "modpack": None,
                "icon": "", "players": None, "player_pack": p}
        with mock.patch.object(J, "_node_summary", lambda member, server: node):
            J.save_page("n1", SERVER, {"enabled": True, "slug": "far"}, "admin")
            a = self.get("/join/far/pack.mrpack")
            self.assertEqual(a.status, 200)
            index = json.loads(zipfile.ZipFile(io.BytesIO(a.body)).read("modrinth.index.json"))
            self.assertEqual(index["name"], "On a node")
            self.assertEqual(len(index["files"]), 2)
            self.assertIn("pack.mrpack", self.get("/join/far").body.decode())


if __name__ == "__main__":
    unittest.main()
