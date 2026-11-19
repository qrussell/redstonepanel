"""Tests for core/catalog.py, core/modrinth.py, core/hangar.py and core/sources.py with fake
Modrinth and Hangar APIs: install with dependencies, tracking, updates, undo, the check before a
Minecraft update, and the download rules. Install tests are Linux only (core/files.py uses
openat). Run: python -m unittest discover -s tests"""
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.parse
import zipfile
from types import SimpleNamespace
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-catalog-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import catalog as C, hangar as H, manager as M, modrinth as R, mods as X, sources as S  # noqa: E402

LINUX = sys.platform.startswith("linux")
try:
    import fastapi  # noqa: F401
    HAVE_FASTAPI = True
except ImportError:
    HAVE_FASTAPI = False


def fabric_jar(mod_id, version, mc="1.21.4"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("fabric.mod.json", json.dumps({"schemaVersion": 1, "id": mod_id, "version": version,
                                                  "name": mod_id.title(), "depends": {"minecraft": mc}}))
    return buf.getvalue()


def plugin_jar(name, version):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("plugin.yml", f"name: {name}\nversion: {version}\nmain: x.Y\napi-version: '1.21'\n")
    return buf.getvalue()


class FakeAPIs:
    """Just enough of Modrinth and Hangar. Files are served from self.blobs by URL."""

    def __init__(self):
        self.projects = {}        # modrinth id -> {"slug", "title", "versions": [newest first]}
        self.hangar = {}          # slug -> {"id": n, "versions": [...]}
        self.blobs = {}
        self.calls = []

    def mversion(self, pid, vid, number, data, mc=("1.21.4",), deps=(), vtype="release", loaders=("fabric",)):
        url = f"https://cdn.modrinth.com/data/{pid}/versions/{vid}/{self.projects[pid]['slug']}-{number}.jar"
        self.blobs[url] = data
        v = {"id": vid, "project_id": pid, "version_number": number, "version_type": vtype,
             "game_versions": list(mc), "loaders": list(loaders),
             "dependencies": [{"project_id": d, "dependency_type": "required"} for d in deps],
             "files": [{"url": url, "filename": url.rsplit("/", 1)[1], "primary": True, "size": len(data),
                        "hashes": {"sha512": hashlib.sha512(data).hexdigest()}}]}
        self.projects[pid]["versions"].insert(0, v)
        return v

    def mproject(self, pid, slug):
        self.projects[pid] = {"id": pid, "slug": slug, "title": slug.replace("-", " ").title(), "versions": []}

    def hversion(self, slug, vid, name, data, mc=("1.21.4",), channel="Release", deps=(), external=None):
        url = f"https://hangarcdn.papermc.io/plugins/x/{slug}/versions/{name}/PAPER/{slug}-{name}.jar"
        self.blobs[url] = data
        v = {"id": vid, "name": name, "channel": {"name": channel},
             "platformDependencies": {"PAPER": list(mc)},
             "pluginDependencies": {"PAPER": list(deps)},
             "downloads": {"PAPER": {"fileInfo": {"name": f"{slug}-{name}.jar", "sizeBytes": len(data),
                                                  "sha256Hash": hashlib.sha256(data).hexdigest()},
                                     "externalUrl": external, "downloadUrl": None if external else url}}}
        self.hangar[slug]["versions"].insert(0, v)

    def _versions(self, pid, loaders, mc):
        return [v for v in self.projects[pid]["versions"]
                if (not loaders or set(v["loaders"]) & set(loaders)) and (not mc or set(v["game_versions"]) & set(mc))]

    def _find(self, key):
        for p in self.projects.values():
            if key in (p["id"], p["slug"]):
                return p
        return None

    def get_json(self, url, body=None, cache=True):
        self.calls.append(url)
        parts = urllib.parse.urlsplit(url)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(parts.query).items()}
        path = urllib.parse.unquote(parts.path)
        if parts.hostname == "api.modrinth.com":
            path = path[len("/v2"):]
            if path == "/search":
                hits = [{"project_id": p["id"], "slug": p["slug"], "title": p["title"], "author": "a",
                         "description": "d", "downloads": 5} for p in self.projects.values()
                        if q.get("query", "") in p["slug"]]
                return {"total_hits": len(hits), "hits": hits}
            if path == "/projects":
                return [{"id": p["id"], "slug": p["slug"], "title": p["title"]} for p in self.projects.values()
                        if p["id"] in json.loads(q["ids"])]
            if path.startswith("/project/") and path.endswith("/version"):
                p = self._find(path.split("/")[2])
                return self._versions(p["id"], json.loads(q.get("loaders", "[]")), json.loads(q.get("game_versions", "[]")))
            if path.startswith("/project/"):
                p = self._find(path.split("/")[2])
                return p and {"id": p["id"], "slug": p["slug"], "title": p["title"]}
            all_versions = [v for p in self.projects.values() for v in p["versions"]]
            by_hash = {v["files"][0]["hashes"]["sha512"]: v for v in all_versions}
            if path == "/version_files":
                return {h: by_hash[h] for h in body["hashes"] if h in by_hash}
            if path == "/version_files/update":
                out = {}
                for h in body["hashes"]:
                    if h in by_hash:
                        vs = self._versions(by_hash[h]["project_id"], body.get("loaders"), body.get("game_versions"))
                        if vs:
                            out[h] = vs[0]
                return out
        if parts.hostname == "hangar.papermc.io":
            path = path[len("/api/v1"):]
            if path == "/projects":
                return {"pagination": {"count": len(self.hangar)},
                        "result": [{"name": s, "namespace": {"owner": "o", "slug": s}, "stats": {"downloads": 1}}
                                   for s in self.hangar]}
            bits = path.split("/")
            key = bits[2]
            slug = next((s for s, p in self.hangar.items() if key in (s, str(p["id"]))), None)
            if slug is None:
                return None
            if len(bits) == 3:
                return {"name": slug, "namespace": {"owner": "o", "slug": slug}}
            vs = [v for v in self.hangar[slug]["versions"]
                  if (not q.get("platformVersion") or q["platformVersion"] in v["platformDependencies"]["PAPER"])
                  and (not q.get("channel") or v["channel"]["name"] == q["channel"])]
            return {"result": vs}
        raise AssertionError(f"unexpected URL {url}")

    def download(self, url, allowed, algorithm, expected, size=None):
        S.check_url(url, allowed)
        data = self.blobs[url]
        assert hashlib.new(algorithm, data).hexdigest() == expected
        return io.BytesIO(data)


class Base(unittest.TestCase):
    kind = ("fabric", "1.21.4")

    def setUp(self):
        self.root = tempfile.mkdtemp(dir=TMP)
        self.server = os.path.join(self.root, "minecraft-java")
        os.makedirs(self.server)
        self.api = FakeAPIs()
        X._meta_cache.clear()
        C._hash_cache.clear()
        self.logs = []
        for p in (mock.patch.object(M, "_check_server", lambda s: None),
                  mock.patch.object(M, "get_server_dir", lambda s: self.server),
                  mock.patch.object(M, "write_log", self.logs.append),
                  mock.patch.object(X, "server_kind", lambda s: self.kind),
                  mock.patch.object(R, "get_json", self.api.get_json),
                  mock.patch.object(H, "get_json", self.api.get_json),
                  mock.patch.object(C, "download", self.api.download)):
            p.start()
            self.addCleanup(p.stop)

    def path(self, *p):
        return os.path.join(self.server, *p)

    def tracking(self):
        with open(self.path(C.TRACK_FILE)) as f:
            return json.load(f)


@unittest.skipUnless(LINUX, "needs Linux (openat)")
class ModrinthInstall(Base):
    def setUp(self):
        super().setUp()
        a = self.api
        a.mproject("P7dR8mSH", "fabric-api")
        a.mversion("P7dR8mSH", "fa1", "0.119.4", fabric_jar("fabric-api", "0.119.4"))
        a.mproject("gvQqBUqZ", "lithium")
        a.mversion("gvQqBUqZ", "li-old", "0.14.0", fabric_jar("lithium", "0.14.0"), deps=["P7dR8mSH"])
        a.mversion("gvQqBUqZ", "li-alpha", "0.16.0-alpha", fabric_jar("lithium", "0.16.0"), vtype="alpha", deps=["P7dR8mSH"])
        a.mversion("gvQqBUqZ", "li-121", "0.15.0", fabric_jar("lithium", "0.15.0", "1.21"), mc=("1.21",))

    def test_install_brings_dependencies_first_and_skips_alpha(self):
        r = C.install("minecraft-java", "modrinth", "lithium", actor="alex")
        self.assertEqual([i["id"] for i in r["installed"]], ["fabric-api", "lithium"])
        self.assertEqual(r["installed"][1]["version"], "0.14.0")             # release, not the newer alpha
        self.assertTrue(os.path.isfile(self.path("mods", "lithium-0.14.0.jar")))
        t = self.tracking()["files"]
        self.assertEqual(t["lithium-0.14.0.jar"]["project"], "gvQqBUqZ")
        self.assertEqual(t["fabric-api-0.119.4.jar"]["version_id"], "fa1")
        self.assertTrue(any("came from Modrinth" in m for m in self.logs))

    def test_dependency_added_by_hand_isnt_installed_again(self):
        X.install("minecraft-java", "my-fabric-api.jar", io.BytesIO(fabric_jar("fabric-api", "0.100")))
        r = C.install("minecraft-java", "modrinth", "gvQqBUqZ")
        self.assertEqual([i["id"] for i in r["installed"]], ["lithium"])
        self.assertEqual(sorted(os.listdir(self.path("mods"))), ["lithium-0.14.0.jar", "my-fabric-api.jar"])

    def test_no_build_for_this_version(self):
        self.api.mproject("x1", "old-mod")
        self.api.mversion("x1", "o1", "1.0", fabric_jar("old-mod", "1.0", "1.20"), mc=("1.20.1",))
        with self.assertRaises(S.SourceError) as cm:
            C.install("minecraft-java", "modrinth", "old-mod")
        self.assertIn("no release or beta build for Fabric 1.21.4", str(cm.exception))
        self.assertFalse(os.path.exists(self.path("mods")))

    def test_search_marks_installed_and_checks_the_source(self):
        C.install("minecraft-java", "modrinth", "fabric-api")
        hits = {h["slug"]: h for h in C.search("minecraft-java", "modrinth", "")["hits"]}
        self.assertTrue(hits["fabric-api"]["installed"])
        self.assertFalse(hits["lithium"]["installed"])
        with self.assertRaises(X.ModError):
            C.search("minecraft-java", "hangar", "")                         # Fabric: Modrinth only

    def test_updates_update_and_undo(self):
        C.install("minecraft-java", "modrinth", "lithium")
        # a jar added by hand that Modrinth knows
        self.api.mproject("AANobbMI", "sodium")
        old = fabric_jar("sodium", "0.5")
        self.api.mversion("AANobbMI", "so5", "0.5", old)
        X.install("minecraft-java", "sodium-by-hand.jar", io.BytesIO(old))
        X.install("minecraft-java", "secret-sauce.jar", io.BytesIO(fabric_jar("secret-sauce", "1")))
        u = C.updates("minecraft-java")
        self.assertIsNone(u["items"]["lithium-0.14.0.jar"]["update"])
        self.assertTrue(u["items"]["sodium-by-hand.jar"]["matched"])
        self.assertEqual(u["unknown"], ["secret-sauce.jar"])
        # new releases appear
        self.api.mversion("gvQqBUqZ", "li-new", "0.14.2", fabric_jar("lithium", "0.14.2"), deps=["P7dR8mSH"])
        self.api.mversion("AANobbMI", "so6", "0.6", fabric_jar("sodium", "0.6"))
        u = C.updates("minecraft-java")
        self.assertEqual(u["items"]["lithium-0.14.0.jar"]["update"]["version"], "0.14.2")
        self.assertEqual(u["items"]["sodium-by-hand.jar"]["update"]["version"], "0.6")
        with mock.patch.object(C.backups, "run_backup", return_value={"file": "b.tar.gz"}) as backup:
            r = C.update("minecraft-java", [], backup=True, actor="alex")
        backup.assert_called_once_with("minecraft-java", "pre-change")
        self.assertEqual(sorted(d["to"] for d in r["updated"]), ["0.14.2", "0.6"])
        mods = sorted(os.listdir(self.path("mods")))
        self.assertIn("lithium-0.14.0.jar.previous", mods)
        self.assertIn("sodium-0.6.jar", mods)
        self.assertNotIn("sodium-by-hand.jar", mods)
        listed = [i["file"] for i in X.list_installed("minecraft-java")["items"]]
        self.assertNotIn("lithium-0.14.0.jar.previous", listed)              # loaders skip it too
        self.assertIsNone(C.updates("minecraft-java")["items"]["lithium-0.14.2.jar"]["update"])
        self.assertEqual(self.tracking()["files"]["sodium-0.6.jar"]["title"], "Sodium")    # named, not just a file
        self.assertEqual(sorted(C.updates("minecraft-java")["undo"]["files"]), ["lithium-0.14.2.jar", "sodium-0.6.jar"])
        # undo puts the old files back
        C.undo("minecraft-java", actor="alex")
        mods = sorted(os.listdir(self.path("mods")))
        self.assertEqual(mods, ["fabric-api-0.119.4.jar", "lithium-0.14.0.jar", "secret-sauce.jar", "sodium-by-hand.jar"])
        self.assertIsNone(self.tracking()["previous"])
        with self.assertRaises(X.ModError):
            C.undo("minecraft-java")

    def test_only_chosen_files_update(self):
        C.install("minecraft-java", "modrinth", "lithium")
        self.api.mversion("gvQqBUqZ", "li-new", "0.14.2", fabric_jar("lithium", "0.14.2"), deps=["P7dR8mSH"])
        self.api.mversion("P7dR8mSH", "fa2", "0.120", fabric_jar("fabric-api", "0.120"))
        r = C.update("minecraft-java", ["lithium-0.14.0.jar"])
        self.assertEqual([d["file"] for d in r["updated"]], ["lithium-0.14.0.jar"])
        self.assertTrue(os.path.isfile(self.path("mods", "fabric-api-0.119.4.jar")))

    def test_compatibility_before_a_minecraft_update(self):
        C.install("minecraft-java", "modrinth", "lithium")
        X.install("minecraft-java", "secret-sauce.jar", io.BytesIO(fabric_jar("secret-sauce", "1")))
        self.api.mversion("P7dR8mSH", "fa5", "0.130", fabric_jar("fabric-api", "0.130", "1.21.5"), mc=("1.21.5",))
        c = C.compatibility("minecraft-java", "1.21.5")
        self.assertEqual([m["file"] for m in c["ok"]], ["fabric-api-0.119.4.jar"])
        self.assertEqual([m["file"] for m in c["missing"]], ["lithium-0.14.0.jar"])
        self.assertEqual([m["file"] for m in c["unknown"]], ["secret-sauce.jar"])


@unittest.skipUnless(LINUX, "needs Linux (openat)")
class HangarInstall(Base):
    kind = ("paper", "1.21.4")

    def setUp(self):
        super().setUp()
        a = self.api
        a.hangar["ViaBackwards"] = {"id": 12, "versions": []}
        a.hversion("ViaBackwards", 2, "5.1", plugin_jar("ViaBackwards", "5.1"))
        a.hangar["ViaVersion"] = {"id": 10, "versions": []}
        a.hversion("ViaVersion", 1, "5.0", plugin_jar("ViaVersion", "5.0"),
                   deps=[{"name": "ViaBackwards", "projectId": 12, "required": True},
                         {"name": "ViaRewind", "projectId": 112, "required": False},
                         {"name": "Vault", "projectId": None, "required": True, "externalUrl": "https://example.org/vault"}])
        a.hversion("ViaVersion", 3, "5.1-SNAPSHOT", plugin_jar("ViaVersion", "5.1"), channel="Snapshot")
        a.hangar["Outside"] = {"id": 20, "versions": []}
        a.hversion("Outside", 4, "1.0", b"", external="https://example.org/outside.jar")

    def test_release_channel_and_required_dependencies(self):
        r = C.install("minecraft-java", "hangar", "ViaVersion")
        self.assertEqual([(i["name"], i["version"]) for i in r["installed"]], [("ViaBackwards", "5.1"), ("ViaVersion", "5.0")])
        self.assertTrue(os.path.isfile(self.path("plugins", "ViaVersion-5.0.jar")))
        self.assertTrue(any("Vault" in n and "install it yourself" in n for n in r["notes"]))
        self.assertEqual(self.tracking()["files"]["ViaVersion-5.0.jar"]["source"], "hangar")

    def test_externally_hosted_is_refused(self):
        with self.assertRaises(S.SourceError) as cm:
            C.install("minecraft-java", "hangar", "Outside")
        self.assertIn("downloaded from example.org", str(cm.exception))

    def test_hangar_update(self):
        C.install("minecraft-java", "hangar", "ViaBackwards")
        self.assertIsNone(C.updates("minecraft-java")["items"]["ViaBackwards-5.1.jar"]["update"])
        self.api.hversion("ViaBackwards", 5, "5.2", plugin_jar("ViaBackwards", "5.2"))
        self.assertEqual(C.updates("minecraft-java")["items"]["ViaBackwards-5.1.jar"]["update"]["version"], "5.2")
        C.update("minecraft-java", [])
        self.assertTrue(os.path.isfile(self.path("plugins", "ViaBackwards-5.2.jar")))


class Kinds(unittest.TestCase):
    def test_bedrock_and_vanilla_have_no_catalog(self):
        for kind in (("bedrock", None), ("vanilla", "1.21.4")):
            with mock.patch.object(X, "server_kind", lambda s, k=kind: k):
                with self.assertRaises(X.ModError):
                    C.search("minecraft-x", "modrinth", "")


class Downloads(unittest.TestCase):
    def test_only_https_on_the_cdn(self):
        for url in ("http://cdn.modrinth.com/x.jar", "https://evil.example/x.jar",
                    "https://cdn.modrinth.com.evil.example/x.jar", "file:///etc/passwd"):
            with self.assertRaises(S.SourceError):
                S.check_url(url, R.HOSTS)
        S.check_url("https://cdn.modrinth.com/data/x.jar", R.HOSTS)

    def _fake(self, data):
        resp = mock.MagicMock()
        resp.__enter__.return_value = resp
        resp.read.side_effect = [data, b""]
        opener = mock.MagicMock()
        opener.open.return_value = resp
        return mock.patch.object(S.urllib.request, "build_opener", return_value=opener)

    def test_checksum_and_size(self):
        data = b"jar bytes"
        with self._fake(data):
            f = S.download("https://cdn.modrinth.com/a.jar", R.HOSTS, "sha512", hashlib.sha512(data).hexdigest())
            with f:
                self.assertEqual(f.read(), data)
        with self._fake(data), self.assertRaises(S.SourceError) as cm:
            S.download("https://cdn.modrinth.com/a.jar", R.HOSTS, "sha512", "00")
        self.assertIn("didn't match its checksum", str(cm.exception))
        with self.assertRaises(S.SourceError):
            S.download("https://cdn.modrinth.com/a.jar", R.HOSTS, "sha512", None)
        with self.assertRaises(S.SourceError):
            S.download("https://cdn.modrinth.com/a.jar", R.HOSTS, "sha512", "00", size=S.MAX_DOWNLOAD + 1)

    def test_redirect_off_the_cdn_is_refused(self):
        handler = S._Redirects(R.HOSTS)
        with self.assertRaises(S.SourceError):
            handler.redirect_request(mock.Mock(), None, 302, "Found", {}, "https://evil.example/x.jar")


@unittest.skipUnless(HAVE_FASTAPI, "needs fastapi")
class AdminOnly(unittest.TestCase):
    def test_users_cannot_use_the_catalog(self):
        import main
        req = SimpleNamespace(state=SimpleNamespace(user={"username": "alex", "role": "user", "source": "panel"}))
        for call in (lambda: main.search_addons(req, "minecraft-java", "modrinth", "", 0),
                     lambda: main.install_from_catalog(req, "minecraft-java", main.CatalogInstall(source="modrinth", project="x")),
                     lambda: main.addon_updates(req, "minecraft-java"),
                     lambda: main.update_addons(req, "minecraft-java", main.CatalogUpdate()),
                     lambda: main.undo_addon_update(req, "minecraft-java"),
                     lambda: main.addon_compatibility(req, "minecraft-java", "1.21.5")):
            with self.assertRaises(main.HTTPException) as cm:
                call()
            self.assertEqual(cm.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
