"""Changing a Java server's type (Vanilla, Paper, Fabric) in place, and undoing it (1.19).

A server keeps its world, port, settings, players and schedules; only the server software and
what belongs to it changes:

  * World layout. Before Minecraft 26.1 Paper kept the Nether and the End in their own folders
    (<level>_nether/DIM-1, <level>_the_end/DIM1) while Vanilla and Fabric keep them inside the
    world (<level>/DIM-1, <level>/DIM1). Paper moves a vanilla world into its layout by itself
    on its first start, but nothing moves it back, so the panel does when a server leaves
    Paper (and refuses if both places hold a dimension). From 26.1 every type stores all
    dimensions under <level>/dimensions/, but Paper keeps the world's own settings (game
    rules, weather, world generation) with each dimension, so those files move back to
    <level>/data/minecraft when a server leaves Paper.
  * Add-ons. Fabric loads mods/, Paper loads plugins/, Vanilla neither. The folder that the
    new type can't load is renamed <folder>.off and comes back when the server returns to that
    type. Mods and plugins whose Modrinth project also has a build for the new type can be
    installed in the same step.

How a change runs (run_change): the new server.jar is downloaded into a staging folder first,
the server is stopped, a "Before type change" backup is taken (always), the old jar and info
are kept as server.jar.previous and .redstonepanel-server.previous.json, the new ones go in,
the world and add-on folders move, and the server is started. If it doesn't say "Done" within
START_WAIT seconds, everything is put back from those files and the backup. The same
"put back" is offered afterwards as Undo, until the next change or update.

A Java update (manager.update_server_binary) leaves the same record behind, so "Roll back the
last update" is the same undo with the "Before update" backup.

State: DATA_DIR/convert/<server>.json holds {"job": the running or last change, "undo": what
Undo would put back}. Everything in the server folder is reached through core/files.py's
no-symlink walks, because the folder belongs to the minecraft user.
"""
import gzip
import io
import json
import os
import re
import shutil
import struct
import tempfile
import threading
import time
import zipfile

from core import backups, files, javaservers, manager, mods, notify
from core.settings import DATA_DIR
from core.platform import paths, services, system

STATE_DIR = os.path.join(DATA_DIR, "convert")
PREVIOUS_JAR = "server.jar.previous"
PREVIOUS_INFO = ".redstonepanel-server.previous.json"
OFF = ".off"
START_WAIT = int(os.environ.get("RP_CONVERT_START_WAIT") or 300)
NAME_RE = re.compile(r"^minecraft[A-Za-z0-9_.-]*$")
DONE_RE = re.compile(r"Done \([0-9.,]+ ?s\)!")
MAX_JAR_SCAN = 64 * 1024 * 1024        # nested jars larger than this aren't looked into
MAX_LEVEL_DAT = 16 * 1024 * 1024
BACKUP_MARGIN = 200 * 1024 * 1024

# Paper's own world folders before 26.1: (suffix of the folder, the dimension folder inside)
PAPER_DIMS = (("_nether", "DIM-1"), ("_the_end", "DIM1"))
# From 26.1 the folders are the same for every type, but Paper keeps these files of
# <level>/data/minecraft in each dimension's own data folder instead
PAPER_LEVEL_FILES = ("game_rules.dat", "scheduled_events.dat", "weather.dat", "world_gen_settings.dat")
# Modrinth categories that mean a mod adds things to the world (blocks, items, mobs, terrain)
CONTENT_CATEGORIES = {"adventure", "decoration", "equipment", "food", "magic", "mobs", "technology",
                      "transportation", "worldgen", "storage"}
# Data packs Minecraft and the loaders themselves enable (anything else in level.dat is a mod's)
BUILTIN_PACKS = {"vanilla", "fabric", "bukkit", "paper", "bundle", "trade_rebalance", "update_1_20",
                 "update_1_21", "minecart_improvements", "redstone_experiments", "winter_drop",
                 "fabric-convention-tags-v2", "fabric-gametest-api-v1"}
ACTION_NAMES = {"convert": "type change", "undo": "undo", "rollback": "roll back"}

_lock = threading.Lock()
_threads = {}                   # server -> the thread changing it now


class ConvertError(ValueError):
    """A change that can't be made (message is safe to show). status: HTTP status to use."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
def _path(server):
    if not NAME_RE.fullmatch(server or ""):
        raise ConvertError("Unknown server.", 404)
    return os.path.join(STATE_DIR, f"{server}.json")

def load(server):
    try:
        with open(_path(server)) as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    if not isinstance(st, dict):
        st = {}
    st.setdefault("job", None)
    st.setdefault("undo", None)
    return st

def _save(server, st):
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    path = _path(server)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, path)

def _update(server, **changes):
    with _lock:
        st = load(server)
        for k, v in changes.items():
            st[k] = v
        _save(server, st)
        return st

def _busy(server):
    t = _threads.get(server)
    return bool(t and t.is_alive())

def forget(server):
    """A server was removed: its record goes with it."""
    try:
        os.remove(_path(server))
    except (OSError, ConvertError):
        pass


# ---------------------------------------------------------------------------
# The server as it is
# ---------------------------------------------------------------------------
def _java_server(server):
    """(folder, info) of a Java server, or ConvertError."""
    try:
        manager._check_server(server)
    except manager.ServerError as e:
        raise ConvertError(str(e), 404)
    if not manager._is_java(server):
        raise ConvertError("Bedrock has one server type, so there is nothing to change. Java and Bedrock "
                           "worlds are different formats; the Bedrock players bundle (Geyser) lets Bedrock "
                           "players join a Java server instead.")
    d = manager.get_server_dir(server)
    info = javaservers.read_info(d) or {}
    return d, info

def _label(info):
    if not info or not info.get("loader"):
        return "an unknown type"
    return f"{javaservers.LOADERS.get(info['loader'], info['loader'])} {info.get('minecraft') or ''}".strip()

def _level(d):
    name = str(manager.read_properties(d).get("level-name") or "world").strip() or "world"
    try:
        parts = files._parts(name)
    except files.FileError:
        raise ConvertError(f"This server's level-name ('{name}') isn't a plain folder name, so the panel "
                           f"won't move its world folders.")
    if len(parts) != 1:
        raise ConvertError(f"This server's level-name ('{name}') is a path, not a folder name; the panel "
                           f"can't tell where its dimensions are kept.")
    return parts[0]

def _is_dir(server, path):
    """True for a real folder at path (inside the server folder, no symlinks on the way)."""
    try:
        fd = files._open_dir(server, files._parts(path))
    except files.FileError:
        return False
    os.close(fd)
    return True

def _exists(server, path):
    try:
        dfd, name = files._parent(server, path)
    except files.FileError:
        return False
    try:
        os.stat(name, dir_fd=dfd, follow_symlinks=False)
        return True
    except OSError:
        return False
    finally:
        os.close(dfd)

def _rename(server, src, dst, overwrite=False):
    """Moves src to dst inside the server folder; neither path may cross a symlink, and dst
    must not exist (overwrite: a file there is replaced)."""
    sfd, sname = files._parent(server, src)
    try:
        dfd, dname = files._parent(server, dst)
        try:
            try:
                os.stat(dname, dir_fd=dfd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                if not overwrite:
                    raise ConvertError(f"'{dst}' already exists.")
            os.rename(sname, dname, src_dir_fd=sfd, dst_dir_fd=dfd)
        finally:
            os.close(dfd)
    finally:
        os.close(sfd)

def _dir_has_files(server, path):
    """True if the folder holds any file at all (an empty dimension folder is no conflict)."""
    try:
        fd = files._open_dir(server, files._parts(path))
    except files.FileError:
        return False
    try:
        for _root, _dirs, names, _rootfd in os.fwalk(dir_fd=fd, follow_symlinks=False):
            if names:
                return True
        return False
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# World layout
# ---------------------------------------------------------------------------
def world_layout(server, level):
    """"new" (26.1+: <level>/dimensions/minecraft/...), "paper" (the Nether or End in a folder
    of its own), "vanilla" (inside the world), or "none" (no world yet)."""
    if _is_dir(server, f"{level}/dimensions/minecraft/overworld"):
        return "new"
    if any(_is_dir(server, f"{level}{suffix}/{dim}") for suffix, dim in PAPER_DIMS):
        return "paper"
    return "vanilla" if _is_dir(server, level) else "none"

def world_moves(server, level, frm, to):
    """The folder moves this change needs: ([{"from", "to"}], [blockers], note)."""
    layout = world_layout(server, level)
    moves, blockers = [], []
    to_name = javaservers.LOADERS.get(to, to)
    frm, to = _layout(frm), _layout(to)
    if layout == "none":
        return moves, blockers, "No world has been made yet."
    if layout == "new":
        if any(_dir_has_files(server, f"{level}{s}/{dim}") for s, dim in PAPER_DIMS):
            blockers.append(f"The world still has Paper's older folders ({level}_nether or {level}_the_end) next to "
                            f"the new layout. Start the server once and let it finish upgrading the world, "
                            f"then try again.")
        # Same folders for every type, but Paper keeps the world's own settings with each
        # dimension, and Vanilla and Fabric look for them in <level>/data/minecraft
        if frm == "paper" and to != "paper":
            for name in PAPER_LEVEL_FILES:
                src = f"{level}/dimensions/minecraft/overworld/data/minecraft/{name}"
                dst = f"{level}/data/minecraft/{name}"
                if _exists(server, src) and not _exists(server, dst):
                    moves.append({"from": src, "to": dst})
            if moves:
                return moves, blockers, ("Paper keeps the world's game rules, weather and world generation "
                                         "settings with the Overworld; they move back to where "
                                         f"{to_name} looks for them.")
        elif to == "paper" and frm != "paper":
            return moves, blockers, "Paper moves the world's settings in with each dimension on its first start."
        return moves, blockers, "Every type keeps this world the same way, so nothing moves."
    leaving_paper = frm == "paper" and to != "paper"
    entering_paper = to == "paper" and frm != "paper"
    for suffix, dim in PAPER_DIMS:
        split, inside = f"{level}{suffix}/{dim}", f"{level}/{dim}"
        if leaving_paper and _is_dir(server, split):
            if _dir_has_files(server, inside):
                blockers.append(f"Both {split} and {inside} exist, so the panel can't tell which one is the "
                                f"real {'Nether' if dim == 'DIM-1' else 'End'}. Move one away in Files first.")
            else:
                moves.append({"from": split, "to": inside})
        elif entering_paper and _is_dir(server, inside):
            if _dir_has_files(server, split):
                blockers.append(f"Both {inside} and {split} exist, so Paper wouldn't know which one is the "
                                f"real {'Nether' if dim == 'DIM-1' else 'End'}. Move one away in Files first.")
            elif _is_dir(server, f"{level}{suffix}"):
                # Paper only moves a dimension into a folder it can create itself
                moves.append({"from": inside, "to": split})
    if leaving_paper:
        note = ("The Nether and the End move back inside the world folder." if moves
                else "This world has no separate Nether or End folders to move.")
    elif entering_paper:
        note = ("The Nether and the End move into Paper's own folders." if moves else
                "Paper moves the Nether and the End into their own folders on its first start.")
    else:
        note = "Nothing moves."
    return moves, blockers, note

def _apply_moves(server, moves):
    for m in moves:
        if _exists(server, m["to"]):
            # an empty leftover folder (checked in the plan): out of the way
            dfd, name = files._parent(server, m["to"])
            try:
                shutil.rmtree(name, dir_fd=dfd)
            finally:
                os.close(dfd)
        _rename(server, m["from"], m["to"])


# ---------------------------------------------------------------------------
# Content mods: what a world loses without them
# ---------------------------------------------------------------------------
def _scan_names(names, reasons, namespaces):
    for n in names:
        p = n.split("/")
        if len(p) < 4:
            continue
        if p[0] == "assets" and p[1] != "minecraft" and p[2] == "blockstates":
            reasons.add("blocks")
        elif p[0] == "assets" and p[1] != "minecraft" and (p[2] == "items" or p[2:4] == ["models", "item"]):
            reasons.add("items")
        elif p[0] == "data" and p[2] in ("dimension", "dimension_type"):
            reasons.add("dimensions")
            namespaces.add(p[1])
        elif p[0] == "data" and p[2] == "worldgen" and p[1] != "c":
            reasons.add("world generation")
        else:
            continue
        if p[1] != "minecraft":
            namespaces.add(p[1])

def jar_content(fileobj):
    """What a mod jar adds to worlds: {"reasons": ["blocks", ...], "namespaces": [...]}. Looks one
    level into the jars it carries (Fabric's META-INF/jars)."""
    reasons, namespaces = set(), set()
    try:
        with zipfile.ZipFile(fileobj) as z:
            names = z.namelist()
            _scan_names(names, reasons, namespaces)
            for n in names:
                if n.startswith("META-INF/jars/") and n.endswith(".jar"):
                    if z.getinfo(n).file_size > MAX_JAR_SCAN:
                        continue
                    try:
                        with zipfile.ZipFile(io.BytesIO(z.read(n))) as inner:
                            _scan_names(inner.namelist(), reasons, namespaces)
                    except (zipfile.BadZipFile, OSError, RuntimeError):
                        pass
    except (zipfile.BadZipFile, OSError, RuntimeError):
        pass
    return {"reasons": sorted(reasons), "namespaces": sorted(namespaces)}

def _nbt_payload(buf, pos, tag, depth=0):
    """Reads one NBT payload; returns (value, new position). Only what level.dat needs is kept."""
    if depth > 64:
        raise ValueError("nesting")
    if tag == 1:
        return buf[pos], pos + 1
    if tag == 2:
        return struct.unpack_from(">h", buf, pos)[0], pos + 2
    if tag == 3:
        return struct.unpack_from(">i", buf, pos)[0], pos + 4
    if tag == 4:
        return struct.unpack_from(">q", buf, pos)[0], pos + 8
    if tag == 5:
        return None, pos + 4
    if tag == 6:
        return None, pos + 8
    if tag == 7:
        n = struct.unpack_from(">i", buf, pos)[0]
        return None, pos + 4 + n
    if tag == 8:
        n = struct.unpack_from(">H", buf, pos)[0]
        return buf[pos + 2:pos + 2 + n].decode("utf-8", "replace"), pos + 2 + n
    if tag == 9:
        inner = buf[pos]
        n = struct.unpack_from(">i", buf, pos + 1)[0]
        pos += 5
        out = []
        for _ in range(max(n, 0)):
            v, pos = _nbt_payload(buf, pos, inner, depth + 1)
            out.append(v)
        return out, pos
    if tag == 10:
        out = {}
        while True:
            t = buf[pos]
            pos += 1
            if t == 0:
                return out, pos
            n = struct.unpack_from(">H", buf, pos)[0]
            name = buf[pos + 2:pos + 2 + n].decode("utf-8", "replace")
            out[name], pos = _nbt_payload(buf, pos + 2 + n, t, depth + 1)
    if tag == 11:
        n = struct.unpack_from(">i", buf, pos)[0]
        return None, pos + 4 + 4 * n
    if tag == 12:
        n = struct.unpack_from(">i", buf, pos)[0]
        return None, pos + 4 + 8 * n
    raise ValueError(f"tag {tag}")

def level_packs(data):
    """The enabled data packs a level.dat (gzipped NBT bytes) lists, or []."""
    try:
        buf = gzip.decompress(data)
        if not buf or buf[0] != 10:
            return []
        n = struct.unpack_from(">H", buf, 1)[0]
        root, _ = _nbt_payload(buf, 3 + n, 10)
        packs = ((root.get("Data") or {}).get("DataPacks") or {}).get("Enabled") or []
        return [p for p in packs if isinstance(p, str)]
    except (OSError, ValueError, IndexError, struct.error, EOFError, AttributeError):
        return []

def _read_small(server, path, limit):
    try:
        fd, st = files._open_file(server, path)
    except files.FileError:
        return None
    with os.fdopen(fd, "rb") as f:
        return f.read(limit) if st.st_size <= limit else None

def mod_packs(server, level):
    """Data packs a mod registered in this world (not Minecraft's own, not ones in datapacks/)."""
    data = _read_small(server, f"{level}/level.dat", MAX_LEVEL_DAT)
    out = []
    for p in level_packs(data) if data else []:
        if p.startswith("file/") or p in BUILTIN_PACKS or p.startswith("fabric"):
            continue
        out.append(p)
    return sorted(set(out))

def custom_dimensions(server, level):
    """Namespaces other than minecraft under <level>/dimensions/."""
    try:
        entries = files.list_dir(server, f"{level}/dimensions")["entries"]
    except files.FileError:
        return []
    return sorted(e["name"] for e in entries if e["type"] == "dir" and e["name"] != "minecraft")


# ---------------------------------------------------------------------------
# Add-ons
# ---------------------------------------------------------------------------
def _layout(loader):
    """Purpur keeps a world the way Paper does; every other type the way Vanilla does."""
    return "paper" if loader in javaservers.PLUGIN_LOADERS else loader

def _addon_group(loader):
    """Types whose mods or plugins the other one loads too (1.20): Purpur runs Paper's plugins,
    Quilt runs Fabric's mods. A change inside a group leaves the add-ons where they are."""
    return {"purpur": "paper", "quilt": "fabric"}.get(loader, loader)

def _installer(info):
    return (info or {}).get("loader") in javaservers.INSTALLER_LOADERS

def _tracking_name(loader):
    return f".redstonepanel-mods.{_addon_group(loader)}.json"

def _projects(ids):
    """Modrinth project id -> full project object (best effort: {} when Modrinth can't be reached)."""
    from core import modrinth
    from core.sources import SourceError
    try:
        return {p["id"]: p for p in modrinth.catalog_projects(sorted(set(ids)))}
    except (SourceError, OSError, KeyError, TypeError):
        return {}

def addon_plan(server, frm, to, minecraft):
    """What happens to the installed mods or plugins: {"folder", "noun", "action", "items",
    "restore", "blockers", "note"}. Each item: {"file", "name", "content": [reasons],
    "equivalent": {"project", "title"} or None, "known": bool}."""
    from core import catalog, modrinth
    from core.sources import SourceError
    out = {"folder": mods.FOLDERS.get(frm), "noun": mods.NOUNS.get(frm), "action": "none", "items": [],
           "restore": None, "blockers": [], "note": None, "lookup_failed": False}
    new_folder = mods.FOLDERS.get(to)
    same = _addon_group(frm) == _addon_group(to)
    if new_folder and not same and new_folder != out["folder"] and _is_dir(server, new_folder + OFF):
        if _exists(server, new_folder):
            if _dir_has_files(server, new_folder):
                out["note"] = (f"{new_folder}{OFF} from an earlier change stays where it is, because a "
                               f"{new_folder} folder already exists.")
            else:
                out["restore"] = new_folder
        else:
            out["restore"] = new_folder
    if same or not out["folder"] or not _is_dir(server, out["folder"]):
        if same and frm != to and out["folder"] and _dir_has_files(server, out["folder"]):
            names = javaservers.LOADERS
            out["action"] = "keep"
            out["note"] = (f"The {out['noun']}s stay where they are. Mods made only for Quilt don't load on Fabric."
                           if (frm, to) == ("quilt", "fabric") else
                           f"The {out['noun']}s stay where they are: {names.get(to, to)} runs "
                           f"{names.get(_addon_group(to), frm)} {out['noun']}s.")
        return out
    folder = out["folder"]
    if _exists(server, folder + OFF):
        out["blockers"].append(f"A {folder}{OFF} folder is left from an earlier change. Rename or delete it in "
                               f"Files first, so this server's {out['noun']}s can be set aside.")
    out["action"] = "off"
    listing = mods.list_installed(server)
    known = {}
    try:
        known = catalog._identify(server, frm, listing, catalog.read_tracking(server))
    except (SourceError, files.FileError, OSError):
        out["lookup_failed"] = True
    ids = [rec["project"] for rec, _ in known.values() if rec.get("source") == "modrinth" and rec.get("project")]
    projects = _projects(ids)
    if ids and not projects:
        out["lookup_failed"] = True
    for i in listing["items"]:
        item = {"file": i["file"], "name": i.get("name") or i["file"], "enabled": bool(i.get("enabled")),
                "content": [], "namespaces": [], "equivalent": None, "known": False}
        if frm in javaservers.MOD_LOADERS:
            try:
                fd, _ = files._open_file(server, f"{folder}/{i['file']}")
                with os.fdopen(fd, "rb") as f:
                    found = jar_content(f)
                item["content"], item["namespaces"] = found["reasons"], found["namespaces"]
            except files.FileError:
                pass
        rec = (known.get(i["file"]) or (None,))[0]
        p = projects.get(rec.get("project")) if rec and rec.get("source") == "modrinth" else None
        if p:
            item["known"] = True
            item["name"] = p.get("title") or item["name"]
            cats = set(p.get("categories") or []) & CONTENT_CATEGORIES
            if frm in javaservers.MOD_LOADERS and not item["content"] and (cats or p.get("client_side") == "required"):
                item["content"] = sorted(cats) or ["players' games need it too"]
            if to in modrinth.LOADERS and set(p.get("loaders") or []) & set(modrinth.LOADERS[to]) \
                    and (not minecraft or minecraft in (p.get("game_versions") or [])):
                item["equivalent"] = {"project": p["id"], "slug": p.get("slug"), "title": p.get("title")}
        out["items"].append(item)
    return out


# ---------------------------------------------------------------------------
# The plan (preview): changes nothing
# ---------------------------------------------------------------------------
def _tree_size(d):
    """Bytes a backup of this folder would read (what core/backups.py skips left out)."""
    total = 0
    for root, dirs, names in os.walk(d):
        rel_root = os.path.relpath(root, d).replace(os.sep, "/")
        rel_root = "" if rel_root == "." else rel_root
        dirs[:] = [n for n in dirs if not backups._skipped(f"{rel_root}/{n}" if rel_root else n)
                   and not os.path.islink(os.path.join(root, n))]
        for n in names:
            if backups._skipped(f"{rel_root}/{n}" if rel_root else n):
                continue
            try:
                total += os.lstat(os.path.join(root, n)).st_size
            except OSError:
                pass
    return total

def _disk(server, d):
    need = _tree_size(d)
    target = backups.server_backup_dir(server)
    probe = target
    while probe and not os.path.isdir(probe):
        probe = os.path.dirname(probe)
    try:
        st = os.statvfs(probe or "/")
        free = st.f_bavail * st.f_frsize
    except OSError:
        free = None
    return {"needed": need, "free": free, "ok": free is None or free >= need + BACKUP_MARGIN}

def target_versions(loader, current):
    """The Minecraft versions `loader` offers that aren't older than `current`, newest first."""
    versions = javaservers.list_versions(loader)
    if not current:
        return versions
    return [v for v in versions if not javaservers._older(v, current)]

def plan(server, to, minecraft=None):
    """Everything the preview shows for changing `server` to type `to` (and, optionally, a
    newer Minecraft version). Raises ConvertError when the question itself makes no sense."""
    d, info = _java_server(server)
    frm, current = info.get("loader"), info.get("minecraft")
    if to not in javaservers.LOADERS:
        raise ConvertError(f"Unknown server type '{to}'.")
    if not frm or not current:
        raise ConvertError("The panel can't tell which type and Minecraft version this server runs "
                           "(its server.jar isn't one it recognises), so it can't change it safely.")
    from core import modpacks
    pack = modpacks.read_record(d)
    if pack:
        raise ConvertError(f"This server was made from the modpack {pack.get('name') or ''}".rstrip() + ", which decides "
                           "its type. Update the pack under Maintenance, or make a new server for another type.")
    if to == frm:
        raise ConvertError(f"This server already is {javaservers.LOADERS[to]}. To change only its Minecraft "
                           f"version, use Update Server.")
    blockers, warnings = [], []
    try:
        versions = target_versions(to, current)
    except javaservers.JavaServerError as e:
        raise ConvertError(str(e), 502)
    name = javaservers.LOADERS[to]
    want = (minecraft or "").strip() or current
    target = None
    if want != current and javaservers._older(want, current):
        raise ConvertError(f"This server runs Minecraft {current}; its world can't go back to {want}.")
    if want not in versions:
        offered = ", ".join(versions[:6]) or "none"
        blockers.append(f"{name} has no version for Minecraft {want}"
                        + (f". Versions both can use: {offered}." if versions else
                           f", and none newer either. {name} may not have caught up with {current} yet."))
    else:
        try:
            target = javaservers.resolve(to, want)
        except javaservers.JavaServerError as e:
            blockers.append(str(e) + (f" Versions {name} has: {', '.join(v for v in versions[:6] if v != want)}."
                                      if len(versions) > 1 else ""))
    java = None
    if target:
        javas = javaservers.installed_javas()
        need = int(target["java_required"] or 8)
        most = target.get("java_max")                    # Forge before 1.17 doesn't start on a newer Java
        usable = sorted(m for m in javas if m >= need and (not most or m <= int(most)))
        java = {"required": need, "installed": bool(usable), "used": usable[0] if usable else None,
                "major": javaservers.java_major_for(need), "package": javaservers.java_package(need)}
        if not usable and java["major"] is None:
            blockers.append(f"Minecraft {want} needs Java {need}, and this system's package sources have none "
                            f"that new.")
    level = _level(d)
    moves, move_blockers, world_note = world_moves(server, level, frm, to)
    blockers += move_blockers
    addons = addon_plan(server, frm, to, want)
    blockers += addons.pop("blockers")
    content = {"mods": [], "packs": [], "dimensions": []}
    if frm in javaservers.MOD_LOADERS and _addon_group(frm) != _addon_group(to):
        content["mods"] = [{"file": i["file"], "name": i["name"], "adds": i["content"]}
                           for i in addons["items"] if i["content"] and i["enabled"]]
        spaces = {ns for i in addons["items"] for ns in i["namespaces"]}
        content["packs"] = mod_packs(server, level)
        content["dimensions"] = [ns for ns in custom_dimensions(server, level) if ns in spaces]
    risky = bool(content["mods"] or content["dimensions"])
    if addons["lookup_failed"]:
        warnings.append(f"Modrinth couldn't be reached, so the panel can't say which {addons['noun'] or 'add-on'}s "
                        f"have a {name} build.")
    disk = _disk(server, d)
    if not disk["ok"]:
        blockers.append(f"The backup that's always taken first needs about {disk['needed'] / 1048576:.0f} MB, and "
                        f"{backups.BACKUP_ROOT} has {disk['free'] / 1048576:.0f} MB free.")
    if want != current:
        warnings.append(f"The world is upgraded to Minecraft {want} and can't be opened by {current} again, "
                        f"except by undoing this change.")
    running = backups._is_active(server)
    return {
        "server": server,
        "from": {"loader": frm, "name": javaservers.LOADERS.get(frm, frm), "minecraft": current,
                 "build": info.get("build")},
        "to": {"loader": to, "name": name, "minecraft": want, "build": target and target.get("build")},
        "versions": versions[:60],
        "java": java,
        "world": {"level": level, "moves": moves, "note": world_note},
        "addons": {k: v for k, v in addons.items() if k != "lookup_failed"},
        "content": content,
        "confirm_name": risky,
        "suggest": {"paper": "Server essentials", "purpur": "Server essentials", "fabric": "Performance pack",
                    "quilt": "Performance pack", "forge": "Performance pack",
                    "neoforge": "Performance pack"}.get(to) if _addon_group(frm) != _addon_group(to) else None,
        "backup": disk,
        "running": running,
        "blockers": blockers,
        "warnings": warnings,
        "ok": not blockers,
    }


# ---------------------------------------------------------------------------
# Putting the old software aside, and back
# ---------------------------------------------------------------------------
def keep_previous(d, info):
    """Keeps the current server.jar and what it is as *.previous (replacing an older pair).
    Quilt, Forge and NeoForge (1.20) aren't one jar: only what they are is kept, and putting
    them back runs their installer again for the same build."""
    jar = os.path.join(d, "server.jar")
    if _installer(info):
        try:
            os.remove(os.path.join(d, PREVIOUS_JAR))
        except OSError:
            pass
    elif os.path.isfile(jar) and not os.path.islink(jar):
        os.replace(jar, os.path.join(d, PREVIOUS_JAR))
    prev = os.path.join(d, PREVIOUS_INFO)
    tmp = prev + ".tmp"
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w") as f:
        json.dump({k: info.get(k) for k in ("loader", "minecraft", "build", "java_required")}, f)
    os.replace(tmp, prev)

def remember(server, kind, frm, to, backup, java, renames=(), actor="?", was_running=True):
    """Records what Undo (or Roll back the last update) would put back."""
    rec = {"kind": kind, "at": int(time.time()), "by": actor,
           "from": {k: frm.get(k) for k in ("loader", "minecraft", "build", "java_required")},
           "to": {k: to.get(k) for k in ("loader", "minecraft", "build", "java_required")},
           "backup": backup, "java": java, "renames": [list(r) for r in renames], "was_running": bool(was_running)}
    _update(server, undo=rec)
    return rec

def undo_info(server, d=None, info=None):
    """The undo record if it can still be used, with why not otherwise:
    {"kind", "at", "by", "from", "to", "backup", "available", "reason"} or None."""
    rec = load(server).get("undo")
    if not rec:
        return None
    if d is None:
        d = manager.get_server_dir(server)
    if info is None:
        info = javaservers.read_info(d) or {}
    reason = None
    to = rec.get("to") or {}
    if (info.get("loader"), info.get("minecraft")) != (to.get("loader"), to.get("minecraft")):
        return None                              # something else changed the server since
    if not _installer(rec.get("from")) and not os.path.isfile(os.path.join(d, PREVIOUS_JAR)):
        reason = "the previous server.jar is gone"
    elif not rec.get("backup"):
        reason = "no backup was taken before it"
    else:
        try:
            backups.backup_path(server, rec["backup"])
        except backups.BackupError:
            reason = f"its backup ({rec['backup']}) was deleted"
    return {"kind": rec.get("kind"), "at": rec.get("at"), "by": rec.get("by"), "backup": rec.get("backup"),
            "from": dict(rec.get("from") or {}, name=_label(rec.get("from"))),
            "to": dict(to, name=_label(to)), "available": reason is None, "reason": reason}

def _unit_java(server):
    return manager.unit_java(manager._unit_exec_start(server)) or paths.DEFAULT_JAVA

def _set_java(server, d, java):
    exec_start = manager._unit_exec_start(server)
    if os.path.realpath(manager.unit_java(exec_start) or "") != os.path.realpath(java) \
            or f" {javaservers.launch_args(d)} " not in f"{exec_start} ":
        manager._write_unit(server, d, "java", java=java, **manager.unit_heap(exec_start))

def _write_info(d, info):
    path = os.path.join(d, javaservers.INFO_FILE)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w") as f:
        json.dump({k: v for k, v in info.items() if k in ("loader", "minecraft", "build", "java_required")}, f)
    os.replace(tmp, path)

def _chown(d):
    system.give_to_game_account(d)

def _stop(server):
    services.stop(server)

def _state(server):
    return services.state(server)

def _log_tail(fd, size, n=64):
    return os.pread(fd, min(n, size), max(0, size - n))

def _log_mark(server):
    """Where the server's log ends now: (inode, size, its last bytes, mtime). A start usually
    begins a new file, and a new file can get the old one's inode, so the inode isn't enough."""
    try:
        fd, st = files._open_file(server, "logs/latest.log")
    except files.FileError:
        return (0, 0, b"", 0)
    try:
        return (st.st_ino, st.st_size, _log_tail(fd, st.st_size), st.st_mtime_ns)
    finally:
        os.close(fd)

def _log_since(server, mark, limit=512 * 1024):
    """The log's text written after mark (all of it when the file is a new one)."""
    try:
        fd, st = files._open_file(server, "logs/latest.log")
    except files.FileError:
        return ""
    with os.fdopen(fd, "rb") as f:
        start = 0
        if mark and st.st_ino == mark[0] and mark[1] <= st.st_size:
            same = len(mark) < 4 or (_log_tail(f.fileno(), mark[1]) == mark[2]
                                     and (st.st_size > mark[1] or st.st_mtime_ns == mark[3]))
            start = mark[1] if same else 0
        if st.st_size - start > limit:
            start = st.st_size - limit
        f.seek(start)
        return f.read(limit).decode("utf-8", "replace")

def wait_done(server, offset, timeout=None, sleep=time.sleep):
    """True once the server logs "Done (…s)!"; "stopped" when its unit keeps failing, "timeout"
    when it takes too long."""
    deadline = time.time() + (START_WAIT if timeout is None else timeout)
    down = 0
    while time.time() < deadline:
        if DONE_RE.search(_log_since(server, offset)):
            return True
        if _state(server) != "active":
            down += 1
            if down >= 3:
                return "stopped"
        else:
            down = 0
        sleep(2)
    return "timeout"

def explain(server, offset):
    """Why a start failed: causes the crash explainer recognises, and the log's last error lines."""
    text = _log_since(server, offset)
    causes, lines = [], []
    try:
        from core import crashes
        causes = [{"title": c["title"], "text": c["text"], "fix": c["fix"]} for c in crashes.find_causes(text)][:3]
    except Exception:
        pass
    wanted = re.compile(r"ERROR|FATAL|Exception|Caused by|incompatible|requires|Failed", re.I)
    for line in text.splitlines():
        if wanted.search(line):
            lines.append(line[:300])
    if not lines:
        lines = [l[:300] for l in text.splitlines()[-8:]]
    return {"causes": causes, "lines": lines[-12:]}

def _stale_world_folders(server, d, level, filename):
    """After a world restore: the Nether/End folders the backup doesn't have were made by the
    software being undone, and would confuse the next change."""
    import tarfile
    with tarfile.open(backups.backup_path(server, filename), "r:gz") as tar:
        tops = {n.split("/", 1)[0] for n in tar.getnames()}
    for suffix, _dim in PAPER_DIMS:
        name = f"{level}{suffix}"
        path = os.path.join(d, name)
        if name not in tops and os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)
            manager.write_log(f"[UPDATE] Removed {name} from {server}: the backup put back has that dimension "
                              f"inside {level}.")

def put_back(server, d, rec, start, sleep=time.sleep):
    """Puts the software, add-on folders and world of an undo record back. Returns True when the
    old server is back (and, if start, running). Raises ConvertError when it can't be done."""
    prev = os.path.join(d, PREVIOUS_JAR)
    old = rec["from"]
    staging = None
    if _installer(old):
        # Nothing was kept for these: their installer runs again, before anything is touched
        staging = tempfile.mkdtemp(prefix=".redstonepanel-convert-", dir=d)
        try:
            javaservers.install(staging, old["loader"], old.get("minecraft"),
                                build=str(old.get("build") or "").replace("loader ", "") or None)
        except (javaservers.JavaServerError, javaservers.MissingJavaError, OSError) as e:
            shutil.rmtree(staging, ignore_errors=True)
            raise ConvertError(f"{_label(old)} couldn't be downloaded again, so nothing was put back: {e}")
    elif not os.path.isfile(prev) or os.path.islink(prev):
        raise ConvertError("The previous server.jar is gone, so the panel can't put the old software back.")
    try:
        return _put_back(server, d, rec, start, sleep, prev, staging)
    finally:
        if staging:
            shutil.rmtree(staging, ignore_errors=True)

def _put_back(server, d, rec, start, sleep, prev, staging):
    from core import catalog
    _stop(server)
    level = _level(d)
    # The world first, while the info file still names the software that last wrote it: the
    # "Before restore" backup taken here is then labelled with that (newer) software.
    if rec.get("backup"):
        try:
            backups.restore_backup(server, rec["backup"])
            _stale_world_folders(server, d, level, rec["backup"])
        except backups.BackupError as e:
            raise ConvertError(f"The world couldn't be restored from {rec['backup']}, so the software was left "
                               f"as it is: {e}")
    if staging:
        javaservers.adopt(staging, d)
    else:
        now = javaservers.read_info(d) or {}
        launch = javaservers.launch_file(d, now) if _installer(now) else None
        os.replace(prev, os.path.join(d, "server.jar"))
        _write_info(d, rec["from"])
        if launch and os.path.dirname(launch) == d and os.path.basename(launch) != "server.jar":
            try:
                os.remove(launch)                        # the undone loader's own jar
            except OSError:
                pass
    try:
        os.remove(os.path.join(d, PREVIOUS_INFO))
    except OSError:
        pass
    renames = [tuple(r) for r in rec.get("renames") or []]
    # What the software being undone installed is tracked in a file of its own; keep it for
    # switching to that type again, out of the old type's way
    if _exists(server, catalog.TRACK_FILE) and not any(dst == catalog.TRACK_FILE for _src, dst in renames) \
            and _addon_group((rec.get("to") or {}).get("loader")) != _addon_group(rec["from"].get("loader")):
        _rename(server, catalog.TRACK_FILE, _tracking_name((rec.get("to") or {}).get("loader") or "other"),
                overwrite=True)
    for src, dst in reversed(renames):
        if _exists(server, dst) and not _exists(server, src):
            _rename(server, dst, src)
    if rec.get("java") and os.access(rec["java"], os.X_OK):
        _set_java(server, d, rec["java"])
    else:
        try:
            _set_java(server, d, manager.java_for_folder(d))
        except manager.JavaMissing as e:
            raise ConvertError(str(e))
    _chown(d)
    try:
        from core import updates
        updates.forget(server)
    except Exception:
        pass
    if not start:
        return True
    offset = _log_mark(server)
    services.start(server)
    return wait_done(server, offset, sleep=sleep) is True


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------
STEPS = {
    "convert": [("download", "Download the new server"), ("stop", "Stop the server"),
                ("backup", "Back up (Before type change)"), ("swap", "Swap the server software"),
                ("addons", "Mods and plugins"), ("start", "Start and check")],
    "undo": [("stop", "Stop the server"), ("restore", "Put the old software and world back"),
             ("start", "Start and check")],
}

class _Job:
    def __init__(self, server, action, actor, frm, to):
        self.server = server
        self.data = {"id": int(time.time() * 1000), "action": action, "state": "running", "by": actor,
                     "started": int(time.time()), "finished": None, "error": None, "rolled_back": None,
                     "from": frm, "to": to, "why": None, "notes": [], "backup": None,
                     "steps": [{"key": k, "label": l, "state": "waiting"}
                               for k, l in STEPS["convert" if action == "convert" else "undo"]]}
        self.save()

    def save(self):
        _update(self.server, job=self.data)

    def step(self, key, state="running"):
        for s in self.data["steps"]:
            if s["key"] == key:
                s["state"] = state
            elif s["state"] == "running" and state == "running":
                s["state"] = "done"
        self.save()

    def finish(self, error=None, **more):
        for s in self.data["steps"]:
            if s["state"] == "running":
                s["state"] = "failed" if error else "done"
            elif s["state"] == "waiting" and not error:
                s["state"] = "skipped"
        self.data.update(state="failed" if error else "done", error=error, finished=int(time.time()), **more)
        self.save()

def status(server):
    """What the Maintenance card shows: the running or last job, and what can be undone."""
    d, info = _java_server(server)
    st = load(server)
    job = st.get("job")
    if job and job.get("state") == "running" and not _busy(server):
        job = dict(job, state="failed", error="The panel restarted while this was running. Check the server, and "
                                              "use Undo if it doesn't start.")
    return {"server": server, "current": {"loader": info.get("loader"), "minecraft": info.get("minecraft"),
                                          "name": _label(info), "build": info.get("build")},
            "types": [{"id": k, "name": v} for k, v in javaservers.LOADERS.items() if k != info.get("loader")],
            "job": job, "busy": _busy(server), "undo": undo_info(server, d, info), "start_wait": START_WAIT}

def _launch(server, work):
    with _lock:
        if _busy(server):
            raise ConvertError("This server is being changed right now; wait for it to finish.", 409)
        t = threading.Thread(target=work, daemon=True, name=f"convert-{server}")
        _threads[server] = t
    t.start()

def start_change(server, to, minecraft=None, confirm=None, install=(), actor="?", wait=False, sleep=time.sleep):
    """Checks the plan and starts the change in the background (wait=True: runs it here).
    Raises ConvertError for a blocked plan and manager.JavaMissing when the Java it needs
    isn't installed (so the browser can offer to install it)."""
    p = plan(server, to, minecraft)
    if p["blockers"]:
        raise ConvertError(p["blockers"][0])
    if p["confirm_name"] and (confirm or "").strip() != server:
        raise ConvertError(f"This change removes things mods added to the world. Type the server's name "
                           f"({server}) to confirm.")
    try:
        javaservers.choose_java(p["java"]["required"], p["to"]["minecraft"])
    except javaservers.MissingJavaError as e:
        raise manager.JavaMissing(e)
    wanted = {i["equivalent"]["project"] for i in p["addons"]["items"] if i.get("equivalent")}
    install = [x for x in dict.fromkeys(str(i) for i in install or ()) if x in wanted]
    job = _Job(server, "convert", actor,
               {k: p["from"][k] for k in ("loader", "minecraft", "build")},
               {k: p["to"][k] for k in ("loader", "minecraft", "build")})
    work = lambda: run_change(server, p, install, actor, job, sleep)      # noqa: E731
    if wait:
        work()
        return load(server)["job"]
    _launch(server, work)
    return job.data

def run_change(server, p, install, actor, job, sleep=time.sleep):
    d = manager.get_server_dir(server)
    frm, to = p["from"], p["to"]
    old_info = javaservers.read_info(d) or {}
    what = f"{server} from {_label(frm)} to {_label(to)}"
    staging = tempfile.mkdtemp(prefix=".redstonepanel-convert-", dir=d)
    was_running = backups._is_active(server)
    rec, pre, offset, starting = None, None, None, False
    manager.write_log(f"[UPDATE] {actor} is changing {what}.")
    try:
        job.step("download")
        try:
            info, java = javaservers.install(staging, to["loader"], to["minecraft"], current=old_info)
        except (javaservers.JavaServerError, javaservers.MissingJavaError, OSError) as e:
            raise ConvertError(f"Downloading {_label(to)} failed, so nothing was changed: {e}")
        job.step("stop")
        old_java = _unit_java(server)
        if was_running:
            _stop(server)
        job.step("backup")
        try:
            pre = backups.run_backup(server, "pre-convert")
        except Exception as e:
            raise ConvertError(f"The backup before the change failed, so nothing was changed: {e}")
        job.data["backup"] = pre["file"]
        job.step("swap")
        renames = []
        folder = p["addons"]["folder"]
        if p["addons"]["action"] == "off":
            renames.append((folder, folder + OFF))
        if p["addons"]["restore"]:
            renames.append((p["addons"]["restore"] + OFF, p["addons"]["restore"]))
        from core import catalog
        if _addon_group(frm["loader"]) != _addon_group(to["loader"]):
            if _exists(server, catalog.TRACK_FILE):
                renames.append((catalog.TRACK_FILE, _tracking_name(frm["loader"])))
            if _exists(server, _tracking_name(to["loader"])):
                renames.append((_tracking_name(to["loader"]), catalog.TRACK_FILE))
        # From here on something has changed: the record lets a failure (or the admin) undo it
        keep_previous(d, old_info)
        rec = remember(server, "convert", old_info, info, pre["file"], old_java, renames, actor, was_running)
        javaservers.adopt(staging, d)                    # the jar, or everything an installer made
        for src, dst in renames:
            if dst == p["addons"]["restore"] and _exists(server, dst):
                dfd, name = files._parent(server, dst)           # an empty folder in the way
                try:
                    os.rmdir(name, dir_fd=dfd)
                finally:
                    os.close(dfd)
            # a tracking file left by an earlier change is replaced; folders never are
            _rename(server, src, dst, overwrite=src == catalog.TRACK_FILE)
        _apply_moves(server, p["world"]["moves"])
        _set_java(server, d, java)
        _chown(d)
        try:
            from core import updates
            updates.forget(server)
        except Exception:
            pass
        job.step("addons")
        if install and to["loader"] == "fabric":
            install = ["fabric-api"] + [x for x in install if x != "fabric-api"]     # most Fabric mods need it
        for project in install:
            try:
                res = catalog.install(server, "modrinth", project, actor=actor)
                job.data["notes"] += [f"Installed {i.get('name') or i['file']}." for i in res["installed"]]
                job.data["notes"] += list(res.get("notes") or [])
            except Exception as e:
                job.data["notes"].append(f"Couldn't install {project}: {e}")
        job.step("start")
        offset, starting = _log_mark(server), True
        services.start(server)
        how = wait_done(server, offset, sleep=sleep)
        if how is not True:
            raise ConvertError(f"{_label(to)} stopped while starting" if how == "stopped" else
                               f"{_label(to)} didn't finish starting within {START_WAIT // 60} minutes")
        if not was_running:
            _stop(server)
        build = f" (build {info['build']})" if info.get("build") and to["loader"] != "fabric" else ""
        manager.write_log(f"[UPDATE] {actor} changed {what}{build}. Backup from before: {pre['file']}.")
        notify.send("type_changed", server, f"{server} was changed from {_label(frm)} to {_label(to)}.")
        job.finish()
    except Exception as e:
        error = str(e) if isinstance(e, ConvertError) else f"{type(e).__name__}: {e}"
        more = {}
        if rec is not None:
            more["why"] = explain(server, offset) if starting else None
            try:
                back = put_back(server, d, rec, was_running, sleep=sleep)
                _update(server, undo=None)
            except Exception as e2:
                back = False
                error += f". Putting the old server back failed too: {e2}"
            more["rolled_back"] = bool(back)
            if back:
                error += f". {server} is back on {_label(frm)} with its world from before the change"
        elif was_running and not backups._is_active(server):
            services.start(server)
        manager.write_log(f"[UPDATE] Changing {what} failed: {error}.")
        notify.send("update_failed", server, f"Changing {server} to {_label(to)} failed: {error.rstrip('.')}.")
        job.finish(error=error.rstrip(".") + ".", **more)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

def start_undo(server, kind=None, actor="?", wait=False, sleep=time.sleep):
    """Undo type change / Roll back the last update. kind: "convert" or "update" to insist on
    which one the record must be."""
    d, info = _java_server(server)
    u = undo_info(server, d, info)
    if not u or (kind and u["kind"] != kind):
        raise ConvertError("There is no update to roll back." if kind == "update" else
                           "There is no type change to undo.", 404)
    if not u["available"]:
        raise ConvertError(f"This can't be undone any more: {u['reason']}.")
    rec = load(server)["undo"]
    job = _Job(server, "undo" if rec["kind"] == "convert" else "rollback", actor,
               {k: rec["to"].get(k) for k in ("loader", "minecraft", "build")},
               {k: rec["from"].get(k) for k in ("loader", "minecraft", "build")})
    work = lambda: run_undo(server, d, rec, actor, job, sleep)            # noqa: E731
    if wait:
        work()
        return load(server)["job"]
    _launch(server, work)
    return job.data

def run_undo(server, d, rec, actor, job, sleep=time.sleep):
    what = f"{server} back from {_label(rec['to'])} to {_label(rec['from'])}"
    was_running = backups._is_active(server)
    manager.write_log(f"[UPDATE] {actor} is putting {what}, with the world from {rec['backup']}.")
    try:
        job.step("stop")
        job.data["backup"] = rec["backup"]
        job.step("restore")
        # put_back stops the server itself and restores the world (after a "Before restore" backup)
        put_back(server, d, rec, False, sleep=sleep)
        _update(server, undo=None)
        job.step("start")
        if was_running:
            offset = _log_mark(server)
            services.start(server)
            ok = wait_done(server, offset, sleep=sleep)
            if ok is not True:
                job.data["why"] = explain(server, offset)
                raise ConvertError(f"The old software and world are back, but {_label(rec['from'])} "
                                   + ("stopped while starting" if ok == "stopped" else "didn't finish starting"))
        manager.write_log(f"[UPDATE] {actor} put {what}.")
        job.finish()
    except Exception as e:
        error = str(e) if isinstance(e, ConvertError) else f"{type(e).__name__}: {e}"
        manager.write_log(f"[UPDATE] Putting {what} failed: {error}.")
        job.finish(error=error.rstrip(".") + ".")
