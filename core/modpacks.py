"""Modpacks (1.20): a new server from a Modrinth modpack (.mrpack) or a server pack zip, and
updating it to a newer version of the pack.

  * A Modrinth pack (.mrpack, from Modrinth's search or uploaded) is a zip with
    modrinth.index.json: the Minecraft version, the loader and its version, and a list of files
    with their download addresses and hashes. Only files the pack marks as usable on a server
    are downloaded, only over HTTPS from the hosts the format allows, and each is checked
    against the pack's SHA-512 (or SHA-1) before it's kept. overrides/ and server-overrides/
    are copied in; client-overrides/ is not.
  * A server pack (what most CurseForge modpacks publish as "Server Pack", downloaded by the
    owner and uploaded here) is a zip of a server folder with the pack's mods/ and config/
    and a start script. The panel works out the loader and versions from the pack's files,
    installs that loader itself and copies the pack's files. Its scripts and jars are never
    run and never copied.

Either way the server is made by manager.deploy_server like any other, so its loader comes
from the loader's own site, checked, and it starts from a systemd unit the panel writes.

What a server was made from is recorded in <server>/.redstonepanel-modpack.json, with the
files the pack brought. An update downloads the new version first, takes a backup, stops the
server, removes the mods the old version brought that the new one doesn't have, copies the
new files in and starts it again. The world, the players' data and the server's own settings
are never touched, and mods added by hand stay.

Everything is built in a staging folder owned by root. Copying into an existing server folder
(an update) and removing old files is done as the minecraft user, so a link planted in the
folder can't lead anywhere that user couldn't already reach.
"""
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import urllib.parse
import zipfile
from concurrent.futures import ThreadPoolExecutor

from core import archive, files, javaservers, manager, modrinth, properties
from core.sources import SourceError, download
from core.platform import paths, services, system

PACK_FILE = ".redstonepanel-modpack.json"
INDEX = "modrinth.index.json"
# The hosts the .mrpack format allows downloads from
MRPACK_HOSTS = {"cdn.modrinth.com", "github.com", "raw.githubusercontent.com", "gitlab.com"}
# ...and where GitHub sends a release download on to
REDIRECT_HOSTS = {"objects.githubusercontent.com", "release-assets.githubusercontent.com"}
MAX_PACK_FILES = 3000
MAX_PACK_BYTES = 8 * 1024 ** 3            # everything a pack downloads and unpacks
MAX_INDEX = 8 * 1024 * 1024
DOWNLOADS_AT_ONCE = 6
DEPENDENCY_LOADERS = {"fabric-loader": "fabric", "quilt-loader": "quilt", "forge": "forge", "neoforge": "neoforge"}
VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+_-]{0,39}$")
# Never taken from a pack: the server software and what the panel writes itself
PROTECTED_TOP = {"server.jar", "eula.txt", "libraries", "versions", "run.sh", "run.bat", "user_jvm_args.txt",
                 "quilt-server-launch.jar", "fabric-server-launch.jar", "logs", "crash-reports", ".fabric",
                 ".quilt", ".mixin.out", "installer.log"}
SCRIPT_ENDINGS = (".sh", ".bat", ".cmd", ".ps1", ".command", ".exe")
# From a pack's server.properties: everything but where the server listens and remote control
PROPERTY_SKIP = {"server-port", "server-ip", "query.port", "rcon.port", "rcon.password", "enable-rcon",
                 "enable-query", "management-server-port", "management-server-secret"}
# On an update these stay as the owner has them
KEEP_ON_UPDATE = {"server.properties", "ops.json", "whitelist.json", "banned-players.json", "banned-ips.json",
                  "usercache.json", "server-icon.png"}

_lock = threading.Lock()                  # one modpack install or update at a time


class PackError(files.FileError):
    """A pack that can't be used (message is safe to show)."""


def _log(msg):
    manager.write_log(f"[MODPACK] {msg}")


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
def safe_rel(path):
    """A pack's file path as a clean relative path, or PackError."""
    if not isinstance(path, str) or not path or "\x00" in path or "\\" in path or path.startswith("/") \
            or re.match(r"^[A-Za-z]:", path):
        raise PackError(f"The pack has a file path the panel won't use: {str(path)[:80]!r}")
    parts = path.split("/")
    if any(p in ("", ".", "..") or len(p.encode()) > files.MAX_NAME for p in parts) or len(parts) > 24:
        raise PackError(f"The pack has a file path the panel won't use: {path[:80]!r}")
    return "/".join(parts)


def protected(rel):
    """True for a path a pack may not bring: the server software, scripts, the panel's files."""
    top = rel.split("/", 1)[0]
    low = top.lower()
    if low in PROTECTED_TOP or low.startswith(".redstonepanel-"):
        return True
    if "/" not in rel and (low.endswith(".jar") or low.endswith(SCRIPT_ENDINGS)):
        return True
    return False


def _as_minecraft(cmd):
    return javaservers._as_minecraft(cmd)


# ---------------------------------------------------------------------------
# Modrinth packs (.mrpack)
# ---------------------------------------------------------------------------
def read_mrpack(path):
    """What a .mrpack says: {name, version, minecraft, loader, loader_version, files, client_only,
    refused}. files: [{path, url, hash, algorithm, size}], server-side only."""
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        raise PackError("This isn't a Modrinth modpack: a .mrpack is a zip file.")
    with z:
        try:
            info = z.getinfo(INDEX)
        except KeyError:
            raise PackError("This zip has no modrinth.index.json, so it isn't a Modrinth modpack (.mrpack). "
                            "For a CurseForge pack, upload its server pack instead.")
        if info.file_size > MAX_INDEX:
            raise PackError("The pack's index is too large.")
        try:
            index = json.loads(z.read(info).decode("utf-8-sig"))
        except (ValueError, UnicodeDecodeError):
            raise PackError("The pack's modrinth.index.json isn't valid JSON.")
    if not isinstance(index, dict) or index.get("formatVersion") != 1:
        raise PackError("This pack uses a .mrpack format version the panel doesn't know.")
    if index.get("game") != "minecraft":
        raise PackError("This pack isn't for Minecraft.")
    deps = index.get("dependencies") if isinstance(index.get("dependencies"), dict) else {}
    minecraft = str(deps.get("minecraft") or "")
    if not javaservers.is_release(minecraft):
        raise PackError(f"The pack is for Minecraft {minecraft or '(not said)'}, which isn't a release the "
                        f"panel can install.")
    named = [(k, v) for k, v in deps.items() if k in DEPENDENCY_LOADERS]
    if len(named) > 1:
        raise PackError("The pack names more than one mod loader.")
    loader, loader_version = "vanilla", None
    if named:
        loader, loader_version = DEPENDENCY_LOADERS[named[0][0]], str(named[0][1] or "")
        if not VERSION_RE.match(loader_version):
            raise PackError("The pack's loader version isn't one the panel can look up.")
    entries = index.get("files") if isinstance(index.get("files"), list) else []
    if len(entries) > MAX_PACK_FILES:
        raise PackError(f"The pack lists more than {MAX_PACK_FILES} files.")
    out, client_only, refused, total, seen = [], 0, [], 0, set()
    for e in entries:
        if not isinstance(e, dict):
            raise PackError("The pack's file list is damaged.")
        env = e.get("env") if isinstance(e.get("env"), dict) else {}
        if env.get("server") == "unsupported":
            client_only += 1
            continue
        rel = safe_rel(e.get("path"))
        if protected(rel) or rel.lower() in seen:
            refused.append(rel)
            continue
        seen.add(rel.lower())
        hashes = e.get("hashes") if isinstance(e.get("hashes"), dict) else {}
        sha512, sha1 = str(hashes.get("sha512") or "").lower(), str(hashes.get("sha1") or "").lower()
        if re.fullmatch(r"[0-9a-f]{128}", sha512):
            algorithm, digest = "sha512", sha512
        elif re.fullmatch(r"[0-9a-f]{40}", sha1):
            algorithm, digest = "sha1", sha1
        else:
            raise PackError(f"The pack gives no checksum for {rel}.")
        urls = [u for u in e.get("downloads") or [] if isinstance(u, str) and _allowed(u)]
        if not urls:
            raise PackError(f"The pack downloads {rel} from somewhere the .mrpack format doesn't allow "
                            f"(only {', '.join(sorted(MRPACK_HOSTS))} over HTTPS).")
        size = e.get("fileSize") if isinstance(e.get("fileSize"), int) and e["fileSize"] >= 0 else 0
        total += size
        out.append({"path": rel, "urls": urls[:4], "hash": digest, "algorithm": algorithm, "size": size})
    if total > MAX_PACK_BYTES:
        raise PackError(f"The pack downloads more than {MAX_PACK_BYTES // 1024 ** 3} GB.")
    return {"kind": "mrpack", "name": _text(index.get("name"), 80) or "Modpack",
            "version": _text(index.get("versionId"), 60), "summary": _text(index.get("summary"), 200),
            "minecraft": minecraft, "loader": loader, "loader_version": loader_version, "files": out,
            "client_only": client_only, "refused": refused, "download_bytes": total}


def _text(v, limit):
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(v)).strip()[:limit] if isinstance(v, (str, int, float)) and v != "" else None


def _allowed(url):
    parts = urllib.parse.urlsplit(url)
    return parts.scheme == "https" and (parts.hostname or "").lower() in MRPACK_HOSTS and not parts.username


def _fetch_one(entry, staging):
    last = None
    for url in entry["urls"]:
        try:
            with download(url, MRPACK_HOSTS | REDIRECT_HOSTS, entry["algorithm"], entry["hash"],
                          entry["size"] or None) as src:
                dest = os.path.join(staging, *entry["path"].split("/"))
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "wb") as out:
                    shutil.copyfileobj(src, out, 1 << 20)
                return os.path.getsize(dest)
        except SourceError as e:
            last = e
    raise PackError(f"Couldn't download {entry['path']}: {last}")


def stage_mrpack(path, plan, staging):
    """Downloads the pack's server files into staging and unpacks its overrides there.
    Returns {"files": [...downloaded...], "overrides": [...], "properties": {...}}."""
    todo = plan["files"]
    _log(f"Downloading {len(todo)} file{'s' if len(todo) != 1 else ''} for {plan['name']}"
         + (f" ({plan['client_only']} client-only left out)" if plan["client_only"] else "") + " ...")
    done, lock = [0], threading.Lock()

    def one(entry):
        n = _fetch_one(entry, staging)
        with lock:
            done[0] += 1
            if done[0] % 25 == 0 and done[0] < len(todo):
                _log(f"{done[0]} of {len(todo)} files downloaded ...")
        return n

    if todo:
        with ThreadPoolExecutor(max_workers=DOWNLOADS_AT_ONCE) as pool:
            list(pool.map(one, todo))               # the first failure is raised here
    overrides, props, total = [], {}, plan.get("download_bytes") or 0
    with zipfile.ZipFile(path) as z:
        members = {}
        for prefix in ("overrides/", "server-overrides/"):       # server-overrides win
            for info in z.infolist():
                name = info.filename.replace("\\", "/")
                if not name.startswith(prefix) or name.endswith("/") or info.is_dir():
                    continue
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    continue                                    # never a link from a pack
                rel = safe_rel(name[len(prefix):])
                if protected(rel):
                    continue
                members[rel] = info
        if len(members) + len(todo) > MAX_PACK_FILES * 10:
            raise PackError("The pack has too many files.")
        for rel, info in members.items():
            total += info.file_size
            if total > MAX_PACK_BYTES:
                raise PackError(f"The pack unpacks to more than {MAX_PACK_BYTES // 1024 ** 3} GB.")
            if rel == "server.properties":
                props = _pack_properties(z.read(info))
                continue
            dest = os.path.join(staging, *rel.split("/"))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with z.open(info) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)
            overrides.append(rel)
    return {"files": sorted(e["path"] for e in todo), "overrides": sorted(overrides), "properties": props}


def _pack_properties(data):
    """A pack's server.properties as the settings to apply: not its ports or remote control."""
    out = {}
    for line in data.decode("utf-8", "replace").splitlines()[:500]:
        line = line.strip()
        if not line or line[0] in "#!" or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", k) and k not in PROPERTY_SKIP and len(v) <= 2000 \
                and "\n" not in v and "\r" not in v:
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# Server packs (a zip of a server folder)
# ---------------------------------------------------------------------------
_FORGE_NAME = re.compile(r"forge-([0-9]+\.[0-9]+(?:\.[0-9]+)?)-([0-9]+(?:\.[0-9]+)+)(?:-installer|-universal|-shim)?(?:\.jar)?")
_NEOFORGE_NAME = re.compile(r"neoforge-([0-9]+\.[0-9]+\.[0-9]+(?:\.[0-9]+)?(?:-beta)?)(?:-installer)?(?:\.jar)?")
_FABRIC_NAME = re.compile(r"fabric-server-mc\.([0-9.]+)-loader\.([0-9.]+)-launcher")


def _subdirs(path):
    try:
        return sorted(n for n in os.listdir(path) if os.path.isdir(os.path.join(path, n)))
    except OSError:
        return []


def _read_small(path, limit=256 * 1024):
    try:
        if os.path.islink(path) or os.path.getsize(path) > limit:
            return ""
        with open(path, "rb") as f:
            return f.read(limit).decode("utf-8", "replace")
    except OSError:
        return ""


def detect_server_pack(root):
    """The loader, its version and the Minecraft version a server pack is for, from its files
    (nothing in it is run). Missing parts are None: {loader, loader_version, minecraft, how}."""
    found = {"loader": None, "loader_version": None, "minecraft": None, "how": None}

    def take(loader, loader_version, minecraft, how):
        if found["loader"] and loader and loader != found["loader"]:
            return
        if loader and not found["loader"]:
            found["loader"], found["how"] = loader, how
        if loader_version and not found["loader_version"] and VERSION_RE.match(str(loader_version)):
            found["loader_version"] = str(loader_version)
        if minecraft and not found["minecraft"] and javaservers.is_release(str(minecraft)):
            found["minecraft"] = str(minecraft)

    # 1. An installed loader's libraries
    lib = os.path.join(root, "libraries")
    for name in reversed(_subdirs(os.path.join(lib, "net", "neoforged", "neoforge"))):
        if javaservers.neoforge_minecraft(name):
            take("neoforge", name, javaservers.neoforge_minecraft(name), "the libraries folder")
            break
    for name in reversed(_subdirs(os.path.join(lib, "net", "minecraftforge", "forge"))):
        m = re.fullmatch(r"([0-9]+(?:\.[0-9]+)+)-([0-9]+(?:\.[0-9]+)+)", name)
        if m:
            take("forge", m.group(2), m.group(1), "the libraries folder")
            break
    for group, loader in (("net/fabricmc/fabric-loader", "fabric"), ("org/quiltmc/quilt-loader", "quilt")):
        names = _subdirs(os.path.join(lib, *group.split("/")))
        if names:
            take(loader, names[-1], None, "the libraries folder")
    for name in reversed(_subdirs(os.path.join(lib, "net", "minecraft", "server"))):
        m = re.match(r"^([0-9]+(?:\.[0-9]+)+)", name)
        if m:
            take(None, None, m.group(1), None)
            break
    # 2. File names at the top of the pack
    try:
        top = sorted(os.listdir(root))
    except OSError:
        top = []
    for name in top:
        m = _NEOFORGE_NAME.match(name)
        if m and javaservers.neoforge_minecraft(m.group(1)):
            take("neoforge", m.group(1), javaservers.neoforge_minecraft(m.group(1)), name)
        m = _FORGE_NAME.match(name)
        if m:
            take("forge", m.group(2), m.group(1), name)
        m = _FABRIC_NAME.match(name)
        if m:
            take("fabric", m.group(2), m.group(1), name)
        m = re.fullmatch(r"minecraft_server\.([0-9]+(?:\.[0-9]+)+)\.jar", name)
        if m:
            take(None, None, m.group(1), None)
    # 3. ServerPackCreator's variables.txt
    text = _read_small(os.path.join(root, "variables.txt"))
    if text:
        v = dict(re.findall(r"^\s*([A-Z_]+)\s*=\s*\"?([^\"\r\n]*)\"?\s*$", text, re.M))
        loader = str(v.get("MODLOADER", "")).strip().lower()
        take(loader if loader in modrinth.PACK_LOADERS else None, v.get("MODLOADER_VERSION", "").strip(),
             v.get("MINECRAFT_VERSION", "").strip(), "variables.txt")
    # 4. A CurseForge manifest.json
    try:
        manifest = json.loads(_read_small(os.path.join(root, "manifest.json"), 4 * 1024 * 1024) or "null")
    except ValueError:
        manifest = None
    mc = manifest.get("minecraft") if isinstance(manifest, dict) and isinstance(manifest.get("minecraft"), dict) else {}
    for ml in mc.get("modLoaders") or []:
        m = re.fullmatch(r"(forge|neoforge|fabric|quilt)-(.+)", str(ml.get("id") if isinstance(ml, dict) else ""))
        if m:
            take(m.group(1), m.group(2), mc.get("version"), "manifest.json")
            break
    # 5. What the pack's own start scripts and notes name (read, never run)
    for name in top:
        if not name.lower().endswith((".sh", ".bat", ".ps1", ".cmd", ".txt", ".properties", ".toml", ".yaml", ".yml",
                                      ".conf", ".cfg")) or name in ("server.properties", "eula.txt"):
            continue
        text = _read_small(os.path.join(root, name))
        if not text:
            continue
        m = _NEOFORGE_NAME.search(text) or re.search(r"net/neoforged/neoforge/([0-9.]+(?:-beta)?)/", text)
        if m and javaservers.neoforge_minecraft(m.group(1)):
            take("neoforge", m.group(1), javaservers.neoforge_minecraft(m.group(1)), name)
        m = _FORGE_NAME.search(text) or re.search(
            r"net/minecraftforge/forge/([0-9]+\.[0-9]+(?:\.[0-9]+)?)-([0-9]+(?:\.[0-9]+)+)/", text)
        if m:
            take("forge", m.group(2), m.group(1), name)
        v = {k.upper(): val for k, val in re.findall(
            r"(?im)^\s*(?:set\s+|export\s+)?\"?((?:NEO)?FORGE_?VERSION|MINECRAFT_?VERSION|MC_?VERSION|"
            r"FABRIC_?(?:LOADER_?)?VERSION)\"?\s*[=:]\s*\"?([0-9][0-9A-Za-z.+_-]*)\"?", text)}
        mcv = v.get("MINECRAFT_VERSION") or v.get("MINECRAFTVERSION") or v.get("MC_VERSION") or v.get("MCVERSION")
        if v.get("NEOFORGE_VERSION") or v.get("NEOFORGEVERSION"):
            nv = v.get("NEOFORGE_VERSION") or v.get("NEOFORGEVERSION")
            take("neoforge", nv, javaservers.neoforge_minecraft(nv) or mcv, name)
        elif v.get("FORGE_VERSION") or v.get("FORGEVERSION"):
            take("forge", v.get("FORGE_VERSION") or v.get("FORGEVERSION"), mcv, name)
        elif mcv:
            take(None, None, mcv, None)
    # 6. The mods themselves, when nothing names the loader
    if not found["loader"]:
        kinds = _mod_kinds(os.path.join(root, "mods"))
        if kinds:
            best = max(kinds, key=kinds.get)
            take(best, None, None, "the mods in the pack")
    return found


def _mod_kinds(mods_dir, limit=60):
    from core import mods
    counts = {}
    try:
        names = sorted(n for n in os.listdir(mods_dir) if n.lower().endswith(".jar"))[:limit]
    except OSError:
        return counts
    for n in names:
        p = os.path.join(mods_dir, n)
        if os.path.islink(p):
            continue
        try:
            with open(p, "rb") as f:
                t = mods.inspect_jar(f)["type"]
        except (mods.ModError, OSError):
            continue
        if t in modrinth.PACK_LOADERS:
            counts[t] = counts.get(t, 0) + 1
    return counts


def read_server_pack(root, name=None, choices=None):
    """The plan for an unpacked server pack. choices: {loader, minecraft, loader_version} the
    admin gave for what the pack doesn't say."""
    choices = choices or {}
    found = detect_server_pack(root)
    for k in ("loader", "minecraft", "loader_version"):
        v = str(choices.get(k) or "").strip()
        if v:
            found[k] = v.lower() if k == "loader" else v
    if not os.path.isdir(os.path.join(root, "mods")) or not any(
            n.lower().endswith(".jar") for n in os.listdir(os.path.join(root, "mods"))):
        if os.path.isfile(os.path.join(root, "server-setup-config.yaml")):
            raise PackError("This pack has no mods in it: it downloads them when its own start script runs, "
                            "which the panel never does. Use the pack's Modrinth version, or a server pack "
                            "that includes its mods folder.")
        if os.path.isfile(os.path.join(root, INDEX)):
            raise PackError("This is a Modrinth modpack: upload it as a .mrpack instead.")
        if os.path.isfile(os.path.join(root, "manifest.json")):
            raise PackError("This is the pack's client download, which has no mods in it. On CurseForge, open "
                            "the pack's Files tab and download its Server Pack (under Additional Files).")
        raise PackError("This zip has no mods folder with mods in it, so it isn't a server pack.")
    if found["loader"] not in modrinth.PACK_LOADERS:
        raise PackError("Couldn't tell which mod loader this pack needs. Choose the loader and Minecraft "
                        "version under 'If the pack doesn't say' and upload it again.")
    if not found["minecraft"] or not javaservers.is_release(found["minecraft"]):
        raise PackError(f"This looks like a {javaservers.LOADERS[found['loader']]} pack, but nothing in it says "
                        f"which Minecraft version. Choose it under 'If the pack doesn't say' and upload it again.")
    if found["loader_version"] and not VERSION_RE.match(found["loader_version"]):
        raise PackError("That loader version isn't one the panel can look up.")
    return {"kind": "serverpack", "name": _text(name, 80) or "Server pack", "version": None, "summary": None,
            "minecraft": found["minecraft"], "loader": found["loader"], "loader_version": found["loader_version"],
            "how": found["how"]}


def stage_server_pack(root, staging):
    """Copies what a server pack brings (its mods, config and other data; never its scripts,
    jars or libraries) into staging. Returns {"files", "overrides", "properties"}."""
    brought, overrides, props, total, count = [], [], {}, 0, 0
    for cur, dirs, names in os.walk(root, followlinks=False):
        rel_dir = os.path.relpath(cur, root).replace(os.sep, "/")
        rel_dir = "" if rel_dir == "." else rel_dir
        dirs[:] = sorted(d for d in dirs if not os.path.islink(os.path.join(cur, d))
                         and not protected(f"{rel_dir}/{d}".lstrip("/") + "/x"))
        for n in sorted(names):
            src = os.path.join(cur, n)
            rel = f"{rel_dir}/{n}".lstrip("/")
            if os.path.islink(src) or not os.path.isfile(src):
                continue
            try:
                rel = safe_rel(rel)
            except PackError:
                continue
            if protected(rel) or rel in ("variables.txt", "manifest.json", "modlist.html", "server-setup-config.yaml"):
                continue
            count += 1
            total += os.path.getsize(src)
            if count > MAX_PACK_FILES * 10 or total > MAX_PACK_BYTES:
                raise PackError("The pack is too large.")
            if rel == "server.properties":
                with open(src, "rb") as f:
                    props = _pack_properties(f.read(256 * 1024))
                continue
            dest = os.path.join(staging, *rel.split("/"))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            os.replace(src, dest) if os.stat(src).st_dev == os.stat(os.path.dirname(dest)).st_dev \
                else shutil.copyfile(src, dest)
            (brought if rel.startswith("mods/") and rel.lower().endswith(".jar") else overrides).append(rel)
    return {"files": sorted(brought), "overrides": sorted(overrides), "properties": props}


# ---------------------------------------------------------------------------
# The record in the server folder
# ---------------------------------------------------------------------------
def read_record(d):
    try:
        with system.game_files(), open(os.path.join(d, PACK_FILE)) as f:
            rec = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(rec, dict) or rec.get("kind") not in ("mrpack", "serverpack"):
        return None
    return rec


def _write_record(d, plan, staged, source):
    rec = {"kind": plan["kind"], "name": plan["name"], "version": plan.get("version"),
           "minecraft": plan["minecraft"], "loader": plan["loader"], "loader_version": plan.get("loader_version"),
           "installed_at": int(time.time()), "files": staged["files"], "overrides": staged["overrides"],
           **{k: v for k, v in (source or {}).items() if v}}
    tmp = os.path.join(d, PACK_FILE + ".part")
    with system.game_files():
        with open(tmp, "w") as f:
            json.dump(rec, f, indent=1)
        os.replace(tmp, os.path.join(d, PACK_FILE))
    return rec


def status(server):
    """What Maintenance shows: the pack this server was made from, or {"pack": None}."""
    d = files.server_root(server)
    rec = read_record(d)
    if not rec:
        return {"pack": None}
    return {"pack": {k: rec.get(k) for k in ("kind", "name", "version", "minecraft", "loader", "loader_version",
                                             "installed_at", "project", "slug", "version_id", "url")}
            | {"files": len(rec.get("files") or []), "can_check": bool(rec.get("project"))}}


# ---------------------------------------------------------------------------
# Getting a pack: from Modrinth or an upload
# ---------------------------------------------------------------------------
def _modrinth_file(project, version_id):
    """(version entry, project summary) of a Modrinth modpack version (the newest release, or
    else the newest, without a version_id)."""
    info = modrinth.modpack_versions(str(project))
    versions = info["versions"]
    if not versions:
        raise PackError(f"{info.get('title') or project} has no .mrpack to download.")
    if version_id:
        pick = next((v for v in versions if v["id"] == version_id), None)
        if not pick:
            raise PackError("That version of the pack wasn't found on Modrinth.")
    else:
        pick = next((v for v in versions if v["channel"] == "release"), versions[0])
    return pick, info


def _download_mrpack(pick):
    f = pick["file"]
    src = download(f["url"], modrinth.HOSTS, f["algorithm"], f["hash"], f.get("size"))
    fd, path = tempfile.mkstemp(prefix="redstonepanel-pack-", suffix=".mrpack")
    with src, os.fdopen(fd, "wb") as out:
        shutil.copyfileobj(src, out, 1 << 20)
    return path


def _pack_heap():
    """The heap a modpack server gets when none is chosen: 4 GB when the machine has room for it
    (1 GB is kept for the system), else the most that fits in whole GB; None leaves the default."""
    try:
        total = int(re.search(r"^MemTotal:\s+([0-9]+) kB", manager.meminfo_text(), re.M).group(1)) * 1024
    except (AttributeError, ValueError):
        return None
    room = (total - 1024 ** 3) // 1024 ** 3
    return f"{min(4, room)}G" if room >= 3 else None


def _loader_build(plan):
    """The pack's own loader version if the loader's site still has it, else None (the
    recommended one is used, with a line in the log)."""
    want = plan.get("loader_version")
    if not want or plan["loader"] == "vanilla":
        return None
    try:
        javaservers.resolve(plan["loader"], plan["minecraft"], build=want)
        return want
    except javaservers.JavaServerError as e:
        if "no version" in str(e) or "no loader version" in str(e) or "no build" in str(e):
            _log(f"{plan['name']} asks for {javaservers.LOADERS[plan['loader']]} {want}, which can't be "
                 f"downloaded any more; using the recommended one for Minecraft {plan['minecraft']}.")
            return None
        raise PackError(str(e))


def _staging_root():
    return tempfile.mkdtemp(prefix=".redstonepanel-pack-", dir=manager._made_servers_root())


def _prepare(source, upload_path, upload_name, choices, staging):
    """Reads and stages a pack. source: 'modrinth' (project, version_id in choices), 'mrpack' or
    'serverpack' (an uploaded file). Returns (plan, staged, where it came from)."""
    origin = {}
    if source == "modrinth":
        pick, info = _modrinth_file(choices.get("project"), choices.get("version_id"))
        _log(f"Downloading {info.get('title')} {pick.get('version') or ''} from Modrinth ...")
        path = _download_mrpack(pick)
        try:
            plan = read_mrpack(path)
            plan["name"] = _text(info.get("title"), 80) or plan["name"]
            plan["version"] = _text(pick.get("version"), 60) or plan["version"]
            staged = stage_mrpack(path, plan, staging)
        finally:
            os.remove(path)
        origin = {"project": info["project"], "slug": info.get("slug"), "version_id": pick["id"],
                  "url": info.get("url")}
    elif source == "mrpack":
        plan = read_mrpack(upload_path)
        staged = stage_mrpack(upload_path, plan, staging)
    elif source == "serverpack":
        unpacked = None
        try:
            try:
                unpacked, root = archive.unpack(upload_path, manager._made_servers_root())
            except archive.ArchiveError as e:
                raise PackError(str(e))
            label = re.sub(r"\.(zip|tar\.gz|tgz|tar)$", "", os.path.basename(upload_name or ""), flags=re.I)
            plan = read_server_pack(root, label, choices)
            staged = stage_server_pack(root, staging)
        finally:
            if unpacked:
                shutil.rmtree(unpacked, ignore_errors=True)
    else:
        raise PackError("Choose a Modrinth modpack, a .mrpack file or a server pack.")
    if plan["loader"] != "vanilla" and plan["loader"] not in modrinth.PACK_LOADERS:
        raise PackError("That isn't a mod loader the panel installs.")
    return plan, staged, origin


def _copy_in(staging, d, skip=()):
    """Copies the staged files into the server folder. skip: relative paths left alone."""
    for rel in skip:
        try:
            os.remove(os.path.join(staging, *rel.split("/")))
        except OSError:
            pass
    if system.is_admin():
        system.take_for_game_account(staging)
        os.chmod(staging, 0o755)
    r = system.copy_into(staging, d, wrap=_as_minecraft)
    if r.returncode != 0:
        raise PackError(f"Couldn't copy the pack's files into the server folder: {r.stderr.strip()[-300:]}")


# ---------------------------------------------------------------------------
# A new server from a pack
# ---------------------------------------------------------------------------
def create(source, upload_path=None, upload_name=None, options=None, actor="admin"):
    """Makes a server from a pack. options: project, version_id (Modrinth); loader, minecraft,
    loader_version (what a server pack doesn't say); name, port, memory. Returns
    {"name", "pack", "notes"}. Raises PackError, manager.ServerError, manager.JavaMissing."""
    options = options or {}
    with _lock:
        staging = _staging_root()
        try:
            plan, staged, origin = _prepare(source, upload_path, upload_name, options, staging)
            advanced = {}
            if options.get("name"):
                advanced["name"] = options["name"]
            if options.get("port"):
                advanced["port"] = options["port"]
            heap = str(options.get("memory") or "").strip() or _pack_heap()
            if heap:
                advanced["heap"] = heap
            build = _loader_build(plan)
            if build:
                advanced["build"] = build
            what = plan["name"] + (f" {plan['version']}" if plan.get("version") else "")
            _log(f"{actor} is creating a server from {what}: "
                 f"{javaservers.LOADERS[plan['loader']]} for Minecraft {plan['minecraft']}, "
                 f"{len(staged['files'])} mod files, {len(staged['overrides'])} other files.")

            def prepare(mc_dir):
                _copy_in(staging, mc_dir)
                if staged["properties"]:
                    manager.write_properties(mc_dir, staged["properties"])
                _write_record(mc_dir, plan, staged, origin)

            name = manager.deploy_server("java", None, plan["loader"], plan["minecraft"], advanced=advanced,
                                         is_admin=True, actor=actor, prepare=prepare)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    notes = _notes(plan)
    _log(f"{name} was created from {what}." + (" " + " ".join(notes) if notes else ""))
    return {"name": name, "pack": status(name)["pack"], "notes": notes}


def _notes(plan):
    notes = []
    if plan.get("client_only"):
        notes.append(f"{plan['client_only']} client-only file{'s were' if plan['client_only'] != 1 else ' was'} "
                     f"left out.")
    if plan.get("refused"):
        notes.append(f"{len(plan['refused'])} file{'s' if len(plan['refused']) != 1 else ''} the pack may not "
                     f"bring (server software or scripts) {'were' if len(plan['refused']) != 1 else 'was'} left out.")
    return notes


# ---------------------------------------------------------------------------
# Updating
# ---------------------------------------------------------------------------
def check_update(server):
    """For a server made from a Modrinth pack: the newest version of the pack and whether it's
    newer than what's installed. {"current", "latest", "available", "versions"}."""
    d = files.server_root(server)
    rec = read_record(d)
    if not rec:
        raise PackError(f"{server} wasn't made from a modpack.", 404)
    if not rec.get("project"):
        raise PackError("This pack was uploaded, so the panel can't look for newer versions. "
                        "Upload the newer version of it instead.")
    info = modrinth.modpack_versions(str(rec["project"]))
    # For this server's loader, and not for an older Minecraft than its world is from (packs
    # often publish one version per Minecraft version)
    here = rec.get("minecraft") if javaservers.is_release(rec.get("minecraft")) else None
    versions = [v for v in info["versions"] if rec["loader"] in (v.get("loaders") or [rec["loader"]])
                and (not here or not v.get("minecraft") or any(
                    javaservers.is_release(m) and javaservers.version_key(m) >= javaservers.version_key(here)
                    for m in v["minecraft"]))]
    ids = [v["id"] for v in versions]
    at = ids.index(rec.get("version_id")) if rec.get("version_id") in ids else None
    newer = versions[:at] if at is not None else versions
    releases = [v for v in newer if v["channel"] == "release"]
    latest = (releases or newer or [None])[0]
    brief = lambda v: {k: v.get(k) for k in ("id", "name", "version", "channel", "minecraft", "published")}
    return {"current": rec.get("version"), "latest": brief(latest) if latest else None,
            "available": latest is not None, "versions": [brief(v) for v in versions[:15]]}


def _is_world(d, top):
    return os.path.isfile(os.path.join(d, top, "level.dat"))


def update(server, source, upload_path=None, upload_name=None, options=None, backup=True, actor="admin"):
    """Moves a modpack server to another version of its pack (the newest release on Modrinth,
    a chosen version_id, or an uploaded file). Returns {"pack", "backup", "removed", "notes"}."""
    from core import backups
    options = dict(options or {})
    d = files.server_root(server)
    old = read_record(d)
    if not old:
        raise PackError(f"{server} wasn't made from a modpack.", 404)
    if source == "modrinth":
        if not old.get("project"):
            raise PackError("This pack was uploaded; upload the newer version of it.")
        options["project"] = old["project"]
    with _lock:
        staging = _staging_root()
        try:
            if source == "serverpack":
                # The pack's last version said what it runs on; a new one usually still does
                options = {"loader": old.get("loader"), **{k: v for k, v in options.items() if v}}
            plan, staged, origin = _prepare(source, upload_path, upload_name, options, staging)
            if plan["loader"] != old.get("loader"):
                raise PackError(f"This server runs {javaservers.LOADERS.get(old.get('loader'), old.get('loader'))} and "
                                f"that pack is for {javaservers.LOADERS[plan['loader']]}. Make a new server from it "
                                f"instead.")
            current = javaservers.read_info(d)
            if current.get("minecraft") and javaservers._older(plan["minecraft"], current["minecraft"]):
                raise PackError(f"This server's world is from Minecraft {current['minecraft']}; that version of "
                                f"the pack is for {plan['minecraft']}, and worlds can't be moved back.")
            what = plan["name"] + (f" {plan['version']}" if plan.get("version") else "")
            if source == "modrinth" and origin.get("version_id") == old.get("version_id"):
                raise PackError(f"{server} already runs {what}.")
            pre = backups.run_backup(server, "pre-change")["file"] if backup else None
            was_running = backups._is_active(server)
            if was_running:
                services.stop(server)
            try:
                # The loader first: the pack's mods are built for it
                build = _loader_build(plan)
                same_loader = current.get("minecraft") == plan["minecraft"] and (
                    not build or str(current.get("build") or "").replace("loader ", "") == build)
                if not same_loader or not javaservers.runnable(d, current):
                    manager.reinstall_java(server, plan["loader"], plan["minecraft"], build, current)
                # What the old version brought and the new one doesn't
                keep = {p.lower() for p in staged["files"]}
                gone = []
                for rel in old.get("files") or []:
                    try:
                        rel = safe_rel(rel)
                    except PackError:
                        continue
                    if rel.lower() not in keep and not protected(rel):
                        gone.append(rel)
                for i in range(0, len(gone), 200):
                    system.remove_files(d, gone[i:i + 200], wrap=_as_minecraft)
                # The owner's own settings and any world stay as they are
                skip = [rel for rel in staged["overrides"]
                        if rel in KEEP_ON_UPDATE or _is_world(d, rel.split("/", 1)[0])]
                _copy_in(staging, d, skip)
                # An uploaded newer version of a Modrinth pack keeps the link to its project
                _write_record(d, plan, staged, origin or {k: old.get(k) for k in ("project", "slug", "url")})
                system.give_to_game_account(d)
            finally:
                if was_running:
                    services.start(server)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    notes = _notes(plan)
    _log(f"{actor} updated {server} to {what}: {len(staged['files'])} mod files, {len(gone)} removed"
         + (f"; backup {pre}" if pre else "; no backup taken") + ".")
    return {"pack": status(server)["pack"], "backup": pre, "removed": len(gone), "notes": notes,
            "restarted": was_running}
