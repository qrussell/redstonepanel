"""Player packs (1.21.6): the mods a player needs to join a modded server, worked out from the
server's own mods folder and handed out by its join page.

    the mods folder ──hash──> Modrinth "which file is this?" ──> each mod's client/server sides
                                   │
                                   ▼
       manifest: every jar, whether players get it and why (the Mods tab shows it, an admin
       can overrule a line and add client-only extras)
                                   │
                 ┌─────────────────┴─────────────────┐
                 ▼                                   ▼
        a .mrpack (Modrinth App,            a packwiz folder (pack.toml, index.toml,
        Prism Launcher, ATLauncher...)      mods/*.pw.toml) for launchers that re-check
                                            the pack every time the game starts

    Which mods go in, with no choice made by the admin:
      on Modrinth, client "required"      in
      on Modrinth, client "optional"      in, marked optional (the launcher lets the player skip it)
      on Modrinth, client "unsupported"   out (server-only)
      not on Modrinth                     out, and named on the page so players know to ask;
                                          the admin can put the jar itself in the pack

Nothing here downloads a mod: the pack points at Modrinth's own files by checksum. Only a jar the
admin chose to hand out is copied into the pack. The manifest is plain JSON so a cluster master
can build the same pack for a node's server from what the node tells it.
"""
import hashlib
import io
import json
import os
import re
import threading
import time
import zipfile

from core import files
from core import javaservers
from core import manager
from core import modrinth
from core import mods
from core import sources

DATA_FILE = "player-packs.json"
MODES = ("auto", "on", "off")            # auto: on unless the server came from a Modrinth modpack
CHOICES = ("auto", "in", "optional", "out")
MAX_EXTRAS = 40
MAX_BUNDLED = 64 * 1024 * 1024           # jars copied into the pack, all together
NAME_MAX = 60
LOADER_KEYS = {"fabric": "fabric-loader", "quilt": "quilt-loader", "forge": "forge", "neoforge": "neoforge"}
PACKWIZ_LOADERS = {"fabric": "fabric", "quilt": "quilt", "forge": "forge", "neoforge": "neoforge"}
PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._!@$()`+,-]{0,63}$")
FILE_RE = re.compile(r"^[A-Za-z0-9._+()\[\] !@#$%&',;=~-]{1,120}\.jar$")
STALE_AFTER = 900                        # a manifest is rebuilt at least this often (new Modrinth answers)

_lock = threading.RLock()
_hash_cache = {}                         # (server, file, size, modified) -> {"sha1", "sha512"}
_manifests = {}                          # server -> (signature, time, manifest)
_packs = {}                              # server -> (version, bytes)


class PackError(files.FileError):
    """A player pack that can't be made or changed (message is safe to show)."""


# ---------------------------------------------------------------------------
# What the admin chose: {"servers": {"<server>": {mode, name, mods: {file: choice}, extras: [project]}}}
# ---------------------------------------------------------------------------
def _path():
    return os.path.join(manager.DATA_DIR, DATA_FILE)


def _load():
    try:
        with open(_path()) as f:
            data = json.load(f)
        servers = data.get("servers") if isinstance(data, dict) else None
        return {k: v for k, v in (servers or {}).items() if isinstance(k, str) and isinstance(v, dict)}
    except (OSError, ValueError):
        return {}


def _save(servers):
    os.makedirs(manager.DATA_DIR, exist_ok=True)
    tmp = _path() + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"servers": servers}, f, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, _path())


def config(server, servers=None):
    raw = (_load() if servers is None else servers).get(server) or {}
    chosen = raw.get("mods") if isinstance(raw.get("mods"), dict) else {}
    extras = raw.get("extras") if isinstance(raw.get("extras"), list) else []
    return {"mode": raw.get("mode") if raw.get("mode") in MODES else "auto",
            "name": str(raw.get("name") or "")[:NAME_MAX],
            "mods": {k: v for k, v in chosen.items() if isinstance(k, str) and v in CHOICES and v != "auto"},
            "extras": [x for x in extras if isinstance(x, str) and PROJECT_RE.match(x)][:MAX_EXTRAS]}


def _project_name(text):
    """A Modrinth project as typed: its id, its slug, or its page's address."""
    text = str(text or "").strip()
    m = re.match(r"^https://modrinth\.com/[a-z]+/([^/?#]+)", text)
    text = m.group(1) if m else text
    if not PROJECT_RE.match(text):
        raise PackError("Type a Modrinth project's name as it is in its address (such as 'sodium'), "
                        "or paste the address of its page.", 400)
    return text.lower()


def save_config(server, new, actor="?"):
    """Changes what's given: mode, name, mods {file: choice}, extras (the whole list),
    add_extra / remove_extra (one project)."""
    if not isinstance(new, dict):
        raise PackError("Nothing to change.", 400)
    with _lock:
        servers = _load()
        cfg = config(server, servers)
        if "mode" in new:
            if new["mode"] not in MODES:
                raise PackError("The player pack is on, off or automatic.", 400)
            cfg["mode"] = new["mode"]
        if "name" in new:
            cfg["name"] = re.sub(r"[\x00-\x1f\x7f]", " ", str(new["name"] or "")).strip()[:NAME_MAX]
        if "mods" in new:
            if not isinstance(new["mods"], dict) or len(new["mods"]) > 2000:
                raise PackError("The list of choices is damaged.", 400)
            for name, choice in new["mods"].items():
                if not isinstance(name, str) or not FILE_RE.match(name) or choice not in CHOICES:
                    raise PackError(f"'{str(name)[:80]}' can't be set to that.", 400)
                if choice == "auto":
                    cfg["mods"].pop(name, None)
                else:
                    cfg["mods"][name] = choice
        if "extras" in new:
            if not isinstance(new["extras"], list):
                raise PackError("The list of extra mods is damaged.", 400)
            cfg["extras"] = list(dict.fromkeys(_project_name(x) for x in new["extras"]))
        if new.get("add_extra"):
            name = _project_name(new["add_extra"])
            _check_extra(server, name)
            if name not in cfg["extras"]:
                cfg["extras"].append(name)
        if new.get("remove_extra"):
            name = str(new["remove_extra"]).lower()
            cfg["extras"] = [x for x in cfg["extras"] if x != name]
        if len(cfg["extras"]) > MAX_EXTRAS:
            raise PackError(f"A player pack can carry up to {MAX_EXTRAS} extra mods.", 400)
        servers[server] = cfg
        _save(servers)
        _manifests.pop(server, None)
    manager.write_log(f"[PLAYERPACK] {actor} changed the player pack of {server}")
    return cfg


def forget(server):
    """A server that was removed: its choices go with it."""
    with _lock:
        servers = _load()
        if servers.pop(server, None) is not None:
            _save(servers)
        _manifests.pop(server, None)
        _packs.pop(server, None)


# ---------------------------------------------------------------------------
# Reading the mods folder
# ---------------------------------------------------------------------------
def _hashes(server, folder, entry):
    key = (server, entry["name"], entry["size"], entry["modified"])
    hit = _hash_cache.get(key)
    if hit:
        return hit
    fd, _ = files._open_file(server, f"{folder}/{entry['name']}")
    sha1, sha512 = hashlib.sha1(), hashlib.sha512()
    with os.fdopen(fd, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            sha1.update(chunk)
            sha512.update(chunk)
    out = {"sha1": sha1.hexdigest(), "sha512": sha512.hexdigest()}
    if len(_hash_cache) > 4000:
        _hash_cache.clear()
    _hash_cache[key] = out
    return out


def _jars(server, folder):
    """The enabled jars in the mods folder: [{name, size, modified}]."""
    try:
        entries = files.list_dir(server, folder)["entries"]
    except files.FileError as e:
        if e.status == 404:
            return []
        raise
    return sorted(({"name": e["name"], "size": e["size"], "modified": e["modified"]} for e in entries
                   if e["type"] == "file" and e["name"].endswith(".jar")), key=lambda e: e["name"].lower())


def _clean(text, limit=80):
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(text or "")).strip()[:limit]


def _version_file(version, sha512=None):
    """The file of a Modrinth version (the one with this checksum, else its main file)."""
    fs = [f for f in version.get("files") or [] if isinstance(f, dict)]
    f = next((f for f in fs if sha512 and (f.get("hashes") or {}).get("sha512") == sha512), None) \
        or next((f for f in fs if f.get("primary")), fs[0] if fs else None)
    if not f:
        return None
    h = f.get("hashes") or {}
    url, name = str(f.get("url") or ""), os.path.basename(str(f.get("filename") or ""))
    if not (re.fullmatch(r"[0-9a-f]{40}", str(h.get("sha1") or "")) and FILE_RE.match(name)
            and re.fullmatch(r"[0-9a-f]{128}", str(h.get("sha512") or ""))):
        return None
    try:
        sources.check_url(url, modrinth.HOSTS)
    except files.FileError:
        return None
    return {"url": url, "filename": name, "size": f.get("size") if isinstance(f.get("size"), int) else 0,
            "sha1": h["sha1"], "sha512": h["sha512"]}


def _side(value):
    return value if value in ("required", "optional", "unsupported") else "unknown"


def _check_extra(server, project):
    kind, minecraft = mods.server_kind(server)
    if kind not in javaservers.MOD_LOADERS:
        raise PackError("Only a server that runs mods has a player pack.", 400)
    pick = modrinth.resolve(project, kind, minecraft)          # raises SourceError with the reason
    if not pick.get("file"):
        raise PackError(f"{project} has no file to download for this server's version.", 404)
    return pick


def _extra_line(project, kind, minecraft):
    line = {"key": project, "project": project, "slug": project, "name": project, "version": "", "state": "out",
            "why": "", "extra": True}
    try:
        pick = modrinth.resolve(project, kind, minecraft)
        v = sources.get_json(f"{modrinth.API}/version/{pick['version_id']}") or {}
        f = _version_file(v)
        if not f:
            raise PackError("Modrinth lists no file for it.", 404)
    except files.FileError as e:
        line["why"] = str(e)
        return line
    line.update(project=pick.get("project") or project, slug=pick.get("slug") or project,
                name=_clean(pick.get("title") or project), version=_clean(pick.get("version"), 40),
                version_id=pick.get("version_id"), state="optional", why="Added for players",
                client_side="optional", server_side="unsupported", **f)
    return line


# ---------------------------------------------------------------------------
# The manifest
# ---------------------------------------------------------------------------
def _signature(server, kind, info, jars, cfg):
    return json.dumps([kind, info.get("minecraft"), info.get("build"), [(j["name"], j["size"], j["modified"]) for j in jars],
                       cfg], sort_keys=True)


def _decide(choice, on_modrinth, client_side):
    """(state, why). state: in | optional | out | bundled."""
    if choice == "out":
        return "out", "Left out by an admin"
    if not on_modrinth:
        if choice in ("in", "optional"):
            return "bundled", "Not on Modrinth, so the file itself is in the pack"
        return "out", "Not on Modrinth, so the pack can't point players to it"
    if choice == "in":
        return "in", "Included by an admin"
    if choice == "optional":
        return "optional", "Marked optional by an admin"
    if client_side == "unsupported":
        return "out", "Server-only: players don't need it"
    if client_side == "optional":
        return "optional", "Works without it on the player's side"
    return "in", "Players need it" if client_side == "required" else "Modrinth doesn't say, so it's included"


def _loader_version(info, minecraft):
    """The loader's own version as launchers name it ("loader 0.16.9" -> "0.16.9",
    "1.20.1-47.3.0" -> "47.3.0"), or "" when the server's record doesn't say."""
    v = str(info.get("build") or "").strip()
    v = v[len("loader "):] if v.startswith("loader ") else v
    if minecraft and v.startswith(f"{minecraft}-"):
        v = v[len(minecraft) + 1:]
    return v if re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,39}", v) else ""


def manifest(server, fresh=False):
    """Everything about a server's player pack. Raises PackError when the server doesn't run
    mods; Modrinth being out of reach gives the last manifest, or one with every jar unknown."""
    kind, minecraft = mods.server_kind(server)
    if kind not in javaservers.MOD_LOADERS:
        raise PackError("Only a server that runs mods (Fabric, Quilt, Forge or NeoForge) has a player pack.", 400)
    info = javaservers.read_info(files.server_root(server)) or {}
    folder = mods.FOLDERS[kind]
    jars = _jars(server, folder)
    cfg = config(server)
    sig = _signature(server, kind, info, jars, cfg)
    with _lock:
        hit = _manifests.get(server)
    now = time.time()
    if hit and hit[0] == sig and not fresh and now - hit[1] < STALE_AFTER:
        return hit[2]
    lines, problems, needs = [], [], {}          # needs: file -> (ids it is, ids it depends on)
    for j in jars:
        try:
            h = _hashes(server, folder, j)
        except (files.FileError, OSError) as e:
            problems.append(f"{j['name']} couldn't be read: {e}")
            continue
        meta = mods._jar_meta(server, folder, j)
        lines.append({"file": j["name"], "size": j["size"], "name": _clean(meta.get("name") or j["name"]),
                      "version": _clean(meta.get("version"), 40), **h})
        needs[j["name"]] = (set(filter(None, [meta.get("id")] + list(meta.get("provides") or []))),
                            set(meta.get("depends") or []))
    looked_up = True
    try:
        found = modrinth.by_hashes([l["sha512"] for l in lines])
        sides = modrinth.project_sides([v.get("project_id") for v in found.values() if isinstance(v, dict)])
    except files.FileError as e:
        if hit and hit[0] == sig:
            return hit[2]                              # Modrinth is out of reach: what we knew still holds
        found, sides, looked_up = {}, {}, False
        problems.append(f"Modrinth couldn't be asked which mods these are ({e}), so the pack is empty for now.")
    bundled = 0
    for l in lines:
        v = found.get(l["sha512"]) if isinstance(found.get(l["sha512"]), dict) else None
        f = _version_file(v, l["sha512"]) if v else None
        p = sides.get(v.get("project_id")) if v else None
        choice = cfg["mods"].get(l["file"], "auto")
        l["choice"] = choice
        if f and p:
            l.update(project=v["project_id"], slug=p.get("slug") or v["project_id"],
                     name=_clean(p.get("title") or l["name"]), version=_clean(v.get("version_number"), 40) or l["version"],
                     version_id=v.get("id"), url=f["url"], filename=f["filename"],
                     client_side=_side(p.get("client_side")), server_side=_side(p.get("server_side")))
        l["on_modrinth"] = bool(f and p)
        l["state"], l["why"] = _decide(choice, l["on_modrinth"], l.get("client_side"))
        if not looked_up and l["state"] == "out" and choice == "auto":
            l["why"] = "Modrinth couldn't be asked"
        if l["state"] == "bundled":
            if not FILE_RE.match(l["file"]):
                l["state"], l["why"] = "out", "Its file name has characters a pack can't carry"
            elif bundled + l["size"] > MAX_BUNDLED:
                l["state"], l["why"] = "out", f"Too large to copy into the pack (over {MAX_BUNDLED >> 20} MB together)"
            else:
                bundled += l["size"]
    # A mod the pack requires makes what it depends on required too (Modrinth calls Fabric API
    # "optional" on the player's side, but a mod that needs it won't start without it)
    again = True
    while again:
        again = False
        wanted = {}
        for l in lines:
            if l["state"] in ("in", "bundled"):
                for dep in needs[l["file"]][1]:
                    wanted.setdefault(dep, l["name"])
        for l in lines:
            if l["state"] == "optional" and l["choice"] == "auto":
                by = next((wanted[i] for i in sorted(needs[l["file"]][0]) if i in wanted), None)
                if by:
                    l["state"], l["why"], again = "in", f"Needed by {by}", True
    have = {l.get("project") for l in lines if l.get("project")} | {l.get("slug") for l in lines if l.get("slug")}
    extras = []
    for project in cfg["extras"]:
        line = _extra_line(project, kind, minecraft)
        if line["state"] != "out" and (line["project"] in have or line["slug"] in have):
            line.update(state="out", why="The server already has this mod")
        extras.append(line)
    given = [l for l in lines + extras if l["state"] != "out"]
    digest = hashlib.sha1(json.dumps([kind, minecraft, info.get("build"),
                                      sorted((l.get("filename") or l["file"], l["sha512"], l["state"]) for l in given)]
                                     ).encode()).hexdigest()[:8]
    modpack = None
    try:
        from core import modpacks
        rec = modpacks.read_record(files.server_root(server))
        modpack = bool(rec and rec.get("project"))
    except Exception:
        modpack = False
    out = {"server": server, "name": cfg["name"], "mode": cfg["mode"],
           "active": cfg["mode"] == "on" or (cfg["mode"] == "auto" and not modpack),
           "from_modrinth_pack": bool(modpack), "version": digest, "loader": kind,
           "loader_name": javaservers.LOADERS.get(kind, kind), "minecraft": minecraft or "",
           "loader_version": _loader_version(info, minecraft), "mods": lines, "extras": extras,
           "problems": problems, "looked_up": looked_up, "built_at": int(now),
           "counts": {s: sum(1 for l in lines + extras if l["state"] == s) for s in ("in", "optional", "bundled", "out")},
           "not_on_modrinth": [l["name"] for l in lines if not l["on_modrinth"] and l["state"] == "out"
                               and l["choice"] != "out"] if looked_up else []}
    with _lock:
        if looked_up:
            _manifests[server] = (sig, now, out)
        if len(_manifests) > 300:
            _manifests.clear()
    return out


def public_part(m):
    """The part of a manifest a join page shows and builds packs from (and a node hands its
    master): no sizes of what's left out, no reasons, no admin's choices."""
    if not m or not m.get("active"):
        return None
    given = []
    for l in list(m["mods"]) + list(m["extras"]):
        if l["state"] == "out":
            continue
        row = {"name": l["name"], "version": l.get("version") or "", "state": l["state"],
               "slug": l.get("slug") or "", "project": l.get("project") or "",
               "version_id": l.get("version_id") or "", "extra": bool(l.get("extra")),
               "filename": l.get("filename") or l.get("file"), "size": l.get("size") or 0,
               "sha1": l["sha1"], "sha512": l["sha512"], "url": l.get("url") or "",
               "server_side": l.get("server_side") or "unknown"}
        given.append(row)
    return {"name": m.get("name") or "", "version": m["version"], "loader": m["loader"],
            "loader_version": m.get("loader_version") or "", "minecraft": m.get("minecraft") or "",
            "mods": given, "ask": list(m.get("not_on_modrinth") or [])[:50]}


def clean_public(p):
    """A node's public part, checked field by field before a master builds a pack from it."""
    if not isinstance(p, dict) or p.get("loader") not in LOADER_KEYS or not isinstance(p.get("mods"), list):
        return None
    rows = []
    for l in p["mods"][:3000]:
        if not isinstance(l, dict) or l.get("state") not in ("in", "optional", "bundled"):
            continue
        name = os.path.basename(str(l.get("filename") or ""))
        if not FILE_RE.match(name) or not re.fullmatch(r"[0-9a-f]{40}", str(l.get("sha1") or "")) \
                or not re.fullmatch(r"[0-9a-f]{128}", str(l.get("sha512") or "")):
            continue
        url = str(l.get("url") or "")
        if l["state"] != "bundled":
            try:
                sources.check_url(url, modrinth.HOSTS)
            except files.FileError:
                continue
        rows.append({"name": _clean(l.get("name") or name), "version": _clean(l.get("version"), 40), "state": l["state"],
                     "slug": str(l.get("slug") or "") if PROJECT_RE.match(str(l.get("slug") or "")) else "",
                     "project": str(l.get("project") or "") if PROJECT_RE.match(str(l.get("project") or "")) else "",
                     "version_id": str(l.get("version_id") or "") if re.fullmatch(r"[A-Za-z0-9]{1,16}", str(l.get("version_id") or "")) else "",
                     "extra": bool(l.get("extra")), "filename": name,
                     "size": l.get("size") if isinstance(l.get("size"), int) and l["size"] >= 0 else 0,
                     "sha1": l["sha1"], "sha512": l["sha512"], "url": url if l["state"] != "bundled" else "",
                     "server_side": _side(l.get("server_side"))})
    version = str(p.get("version") or "")
    return {"name": _clean(p.get("name"), NAME_MAX), "version": version if re.fullmatch(r"[0-9a-f]{8}", version) else "0",
            "loader": p["loader"], "minecraft": _clean(p.get("minecraft"), 40),
            "loader_version": str(p.get("loader_version") or "") if re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,39}", str(p.get("loader_version") or "")) else "",
            "mods": rows, "ask": [_clean(x) for x in (p.get("ask") or [])[:50] if isinstance(x, str)]}


# ---------------------------------------------------------------------------
# The .mrpack
# ---------------------------------------------------------------------------
def _index(p, title):
    deps = {"minecraft": p["minecraft"]}
    if p.get("loader_version"):
        deps[LOADER_KEYS[p["loader"]]] = p["loader_version"]
    entries = []
    for l in p["mods"]:
        if l["state"] == "bundled":
            continue
        server = "unsupported" if l["extra"] else l["server_side"] if l["server_side"] != "unknown" else "required"
        entries.append({"path": f"mods/{l['filename']}", "hashes": {"sha1": l["sha1"], "sha512": l["sha512"]},
                        "env": {"client": "optional" if l["state"] == "optional" else "required", "server": server},
                        "downloads": [l["url"]], "fileSize": l["size"]})
    return {"formatVersion": 1, "game": "minecraft", "versionId": p["version"], "name": title,
            "summary": f"The mods for joining {title}, as the server runs them.", "files": entries,
            "dependencies": deps}


def mrpack(p, title, read_jar=None):
    """The pack as bytes. read_jar(filename) -> bytes for the jars copied in."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        def put(name, data):
            zi = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))      # same input, same file
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o644 << 16
            z.writestr(zi, data)
        put("modrinth.index.json", json.dumps(_index(p, title), indent=1))
        for l in p["mods"]:
            if l["state"] == "bundled" and read_jar:
                data = read_jar(l["filename"])
                if data is not None and hashlib.sha512(data).hexdigest() == l["sha512"]:
                    put(f"overrides/mods/{l['filename']}", data)
    return buf.getvalue()


def _read_local_jar(server):
    folder = mods.FOLDERS[mods.server_kind(server)[0]]

    def read(name):
        try:
            fd, _ = files._open_file(server, f"{folder}/{name}")
            with os.fdopen(fd, "rb") as f:
                return f.read(MAX_BUNDLED + 1)[:MAX_BUNDLED]
        except (files.FileError, OSError):
            return None
    return read


def local_pack(server, title=None):
    """(public part, .mrpack bytes) of a server on this machine; (None, None) when its player
    pack is off."""
    p = public_part(manifest(server))
    if not p:
        return None, None
    title = _clean(p["name"] or title or server, NAME_MAX) or "Player pack"
    with _lock:
        hit = _packs.get(server)
    if hit and hit[0] == (p["version"], title):
        return p, hit[1]
    data = mrpack(p, title, _read_local_jar(server))
    with _lock:
        _packs[server] = ((p["version"], title), data)
        if len(_packs) > 100:
            _packs.pop(next(iter(_packs)))
    return p, data


def bundled_jar(pack_bytes, filename):
    """A jar that was copied into the pack, back out of it (for the packwiz folder)."""
    try:
        with zipfile.ZipFile(io.BytesIO(pack_bytes)) as z:
            return z.read(f"overrides/mods/{filename}")
    except (KeyError, zipfile.BadZipFile):
        return None


def file_name(title, version):
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", title).strip("-.")[:40] or "player-pack"
    return f"{stem}-{version}.mrpack"


# ---------------------------------------------------------------------------
# packwiz: the same pack as a folder a launcher re-checks before every start
# ---------------------------------------------------------------------------
def _t(text):
    """A TOML basic string."""
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(text))
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _pw_name(l):
    stem = l["slug"] or l["filename"][:-4]
    return re.sub(r"[^a-z0-9._-]+", "-", stem.lower()).strip("-.")[:60] or l["sha1"][:12]


def packwiz(p, title, pack_bytes=None):
    """{relative path: bytes}: pack.toml, index.toml, mods/<name>.pw.toml, and the jars that
    were copied into the pack."""
    out, index, seen = {}, [], set()
    for l in p["mods"]:
        if l["state"] == "bundled":
            data = bundled_jar(pack_bytes, l["filename"]) if pack_bytes else None
            if data is None:
                continue
            path = f"mods/{l['filename']}"
            out[path] = data
            index.append((path, hashlib.sha256(data).hexdigest(), False))
            continue
        name = _pw_name(l)
        while name in seen:
            name += "-" + l["sha1"][:6]
        seen.add(name)
        text = (f"name = {_t(l['name'])}\nfilename = {_t(l['filename'])}\n"
                f"side = {_t('client' if l['extra'] or l['server_side'] == 'unsupported' else 'both')}\n\n"
                f"[download]\nurl = {_t(l['url'])}\nhash-format = \"sha512\"\nhash = {_t(l['sha512'])}\n")
        if l["state"] == "optional":
            text += "\n[option]\noptional = true\ndefault = true\n"
        if l["project"] and l["version_id"]:
            text += f"\n[update]\n[update.modrinth]\nmod-id = {_t(l['project'])}\nversion = {_t(l['version_id'])}\n"
        data = text.encode()
        path = f"mods/{name}.pw.toml"
        out[path] = data
        index.append((path, hashlib.sha256(data).hexdigest(), True))
    text = 'hash-format = "sha256"\n'
    for path, digest, meta in sorted(index):
        text += f"\n[[files]]\nfile = {_t(path)}\nhash = {_t(digest)}\n" + ("metafile = true\n" if meta else "")
    out["index.toml"] = text.encode()
    versions = f"minecraft = {_t(p['minecraft'])}\n"
    if p.get("loader_version"):
        versions += f"{PACKWIZ_LOADERS[p['loader']]} = {_t(p['loader_version'])}\n"
    out["pack.toml"] = (f"name = {_t(title)}\nversion = {_t(p['version'])}\npack-format = \"packwiz:1.1.0\"\n\n"
                        f"[index]\nfile = \"index.toml\"\nhash-format = \"sha256\"\n"
                        f"hash = {_t(hashlib.sha256(out['index.toml']).hexdigest())}\n\n"
                        f"[versions]\n{versions}").encode()
    return out


# ---------------------------------------------------------------------------
# What the Mods tab shows
# ---------------------------------------------------------------------------
def admin_view(server, fresh=False):
    m = manifest(server, fresh=fresh)
    keep = ("file", "name", "version", "size", "choice", "state", "why", "slug", "on_modrinth", "client_side",
            "server_side", "extra", "project", "key")
    return {**{k: m[k] for k in ("server", "name", "mode", "active", "from_modrinth_pack", "version", "loader",
                                 "loader_name", "minecraft", "loader_version", "problems", "looked_up", "counts")},
            "mods": [{k: l.get(k) for k in keep} for l in m["mods"]],
            "extras": [{k: l.get(k) for k in keep} for l in m["extras"]]}
