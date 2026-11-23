"""Mods, plugins and Bedrock add-ons: what's installed, and installing a dropped file.

  * Fabric, Quilt, Forge and NeoForge servers load mods from mods/, Paper and Purpur servers
    plugins from plugins/. A jar is read as a zip (its metadata only; nothing in it is run)
    to tell what it is, so a Forge mod on Fabric, a plugin on a mod loader or anything on
    vanilla is refused with the reason instead of making the server fail to start. Quilt also
    loads Fabric mods; NeoForge up to Minecraft 1.20.4 also loads Forge mods.
  * Disabling renames x.jar to x.jar.disabled, which both loaders skip.
  * Bedrock .mcpack / .mcaddon files are unpacked into behavior_packs/ or resource_packs/
    (by the manifest's module types) and added to the active world's
    world_behavior_packs.json / world_resource_packs.json. Disabling takes a pack out of the
    world's list; its folder stays.

Every write goes through core/files.py: opened one folder at a time without following
symlinks, written atomically, owned by the server folder's owner.
"""
import io
import json
import os
import re
import secrets
import stat
import zipfile

from core import files, javaservers, manager
from core.platform import system

os = system.fs()          # Python's os; on Windows, the same calls made safe without dir_fd (core/platform)

FOLDERS = {"fabric": "mods", "quilt": "mods", "forge": "mods", "neoforge": "mods",
           "paper": "plugins", "purpur": "plugins"}
NOUNS = {**{k: "plugin" if v == "plugins" else "mod" for k, v in FOLDERS.items()}, "bedrock": "add-on"}
LABELS = javaservers.LOADERS
DISABLED = ".disabled"
MAX_META = 256 * 1024                 # a metadata file inside a jar
MAX_NESTED = 16 * 1024 * 1024         # a jar inside a jar (Fabric jar-in-jar)
MAX_NESTED_COUNT = 150
MAX_PACK_FILES = 20000
MAX_PACK_BYTES = files.MAX_UPLOAD
BUILTIN_PACKS = ("vanilla", "chemistry", "experimental", "editor")
# Fabric dependencies that aren't mods in mods/
FABRIC_PROVIDED = {"minecraft", "fabricloader", "fabric-loader", "java"}
NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")

_meta_cache = {}                      # (server, file, size, mtime) -> metadata


class ModError(files.FileError):
    """A file that can't be installed or changed (message is safe to show)."""


# ---------------------------------------------------------------------------
# Which kind of server
# ---------------------------------------------------------------------------
def server_kind(server):
    """(a key of javaservers.LOADERS | "bedrock", Minecraft version or None)."""
    d = files.server_root(server)
    if not manager._is_java(server):
        return "bedrock", None
    info = javaservers.read_info(d) or {}
    return info.get("loader") or "vanilla", info.get("minecraft")


# ---------------------------------------------------------------------------
# Reading jar metadata
# ---------------------------------------------------------------------------
def _read_member(z, name, limit=MAX_META):
    info = z.getinfo(name)
    if info.file_size > limit:
        return None
    with z.open(info) as f:
        return f.read(limit + 1)[:limit]


def _json(data):
    if data is None:
        return None
    try:
        v = json.loads(data.decode("utf-8", "replace"), strict=False)
        return v if isinstance(v, dict) else None
    except ValueError:
        return None


def parse_yaml_top(text):
    """The top-level keys of a plugin.yml: scalars, inline [a, b] lists and '- x' lists.
    Enough for name/version/author(s)/api-version/depend without a YAML library."""
    out, key = {}, None
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0].rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if m:
            key, value = m.group(1), m.group(2).strip()
            if value.startswith("[") and value.endswith("]"):
                out[key] = [v.strip().strip("'\"") for v in value[1:-1].split(",") if v.strip()]
            elif value in ("", "|", ">"):
                out[key] = []
            else:
                out[key] = value.strip("'\"")
            continue
        m = re.match(r"^\s+-\s*(.+)$", line)
        if m and key and isinstance(out.get(key), list):
            out[key].append(m.group(1).strip().strip("'\""))
    return out


def _as_list(v):
    if v is None:
        return []
    return [str(x) for x in v] if isinstance(v, list) else [str(v)]


def _fabric_authors(meta):
    out = []
    for a in meta.get("authors") or []:
        out.append(a.get("name") if isinstance(a, dict) else str(a))
    return [a for a in out if a]


def _nested_ids(z, meta, depth=0):
    """Mod ids bundled inside a Fabric jar (META-INF/jars/*.jar), which count as installed."""
    ids = set()
    if depth > 1:
        return ids
    for entry in (meta.get("jars") or [])[:MAX_NESTED_COUNT]:
        name = entry.get("file") if isinstance(entry, dict) else None
        if not name or name not in z.namelist():
            continue
        data = _read_member(z, name, MAX_NESTED)
        if data is None:
            continue
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as inner:
                if "fabric.mod.json" in inner.namelist():
                    m = _json(_read_member(inner, "fabric.mod.json")) or {}
                    if m.get("id"):
                        ids.add(m["id"])
                    ids.update(_as_list(m.get("provides")))
                    ids.update(_nested_ids(inner, m, depth + 1))
        except zipfile.BadZipFile:
            continue
    return ids


def _quilt_meta(meta):
    """id, name, version and dependencies from a quilt.mod.json."""
    q = meta.get("quilt_loader") if isinstance(meta.get("quilt_loader"), dict) else {}
    md = q.get("metadata") if isinstance(q.get("metadata"), dict) else {}
    depends, minecraft = [], None
    for dep in q.get("depends") or []:
        did = dep.get("id") if isinstance(dep, dict) else dep
        if isinstance(dep, dict) and dep.get("optional"):
            continue
        if not isinstance(did, str):
            continue
        did = did.split(":")[-1]
        if did == "minecraft":
            v = dep.get("versions") if isinstance(dep, dict) else None
            minecraft = v if isinstance(v, str) else None
        elif did not in FABRIC_PROVIDED | {"quilt_loader", "quilt_base"}:
            depends.append(did)
    contributors = md.get("contributors") if isinstance(md.get("contributors"), dict) else {}
    return {"id": q.get("id"), "name": md.get("name") or q.get("id"),
            "version": str(q.get("version") or "") or None, "authors": [str(a) for a in contributors][:10],
            "description": md.get("description") if isinstance(md.get("description"), str) else None,
            "minecraft": minecraft, "depends": sorted(set(depends)),
            "provides": sorted(str(p.get("id") if isinstance(p, dict) else p) for p in q.get("provides") or [])}


FORGE_PROVIDED = {"minecraft", "forge", "neoforge", "javafml", "lowcodefml", "fml", "java"}


def _toml_meta(z, name, names):
    """id, name, version and required dependencies from a Forge or NeoForge mods.toml."""
    try:
        import tomllib
        data = tomllib.loads((_read_member(z, name) or b"").decode("utf-8", "replace"))
    except Exception:
        return {}
    mods_list = [m for m in data.get("mods") or [] if isinstance(m, dict)]
    if not mods_list:
        return {}
    first = mods_list[0]
    version = str(first.get("version") or "")
    if "${" in version:                              # "${file.jarVersion}": the jar's manifest has it
        version = ""
        if "META-INF/MANIFEST.MF" in names:
            m = re.search(r"^Implementation-Version:\s*(\S+)", (_read_member(z, "META-INF/MANIFEST.MF") or b"")
                          .decode("utf-8", "replace"), re.M)
            version = m.group(1) if m else ""
    depends, minecraft = set(), None
    deps = data.get("dependencies") if isinstance(data.get("dependencies"), dict) else {}
    for dep in deps.get(str(first.get("modId")), []) or []:
        if not isinstance(dep, dict) or not isinstance(dep.get("modId"), str):
            continue
        required = dep.get("mandatory") if "mandatory" in dep else str(dep.get("type", "required")).lower() == "required"
        if str(dep.get("side", "BOTH")).upper() == "CLIENT":
            continue
        if dep["modId"] == "minecraft":
            minecraft = dep.get("versionRange") if isinstance(dep.get("versionRange"), str) else None
        elif required and dep["modId"] not in FORGE_PROVIDED:
            depends.add(dep["modId"])
    authors = first.get("authors")
    desc = first.get("description")
    return {"id": first.get("modId"), "name": first.get("displayName") or first.get("modId"),
            "version": version or None, "authors": [authors] if isinstance(authors, str) and authors else [],
            "description": desc.strip() if isinstance(desc, str) else None, "minecraft": minecraft,
            "depends": sorted(depends),
            "provides": sorted(str(m.get("modId")) for m in mods_list[1:] if m.get("modId"))}


def _mcmod_meta(data):
    """An old Forge mod's mcmod.info (Minecraft 1.12.2 and older)."""
    try:
        v = json.loads((data or b"").decode("utf-8", "replace"), strict=False)
    except ValueError:
        return {}
    if isinstance(v, dict):
        v = v.get("modList")
    first = v[0] if isinstance(v, list) and v and isinstance(v[0], dict) else {}
    mc = first.get("mcversion")
    return {"id": first.get("modid"), "name": first.get("name") or first.get("modid"),
            "version": str(first.get("version") or "") or None,
            "authors": [str(a) for a in first.get("authorList") or first.get("authors") or []][:10],
            "description": first.get("description") if isinstance(first.get("description"), str) else None,
            "minecraft": mc if isinstance(mc, str) and re.fullmatch(r"[0-9]+(\.[0-9]+)+", mc) else None}


def inspect_jar(fileobj):
    """What a jar is. type: fabric | paper | bukkit | quilt | forge | neoforge | velocity |
    bungee | unknown. Raises ModError for a file that isn't a jar."""
    try:
        z = zipfile.ZipFile(fileobj)
    except (zipfile.BadZipFile, OSError):
        raise ModError("This isn't a valid .jar file (it isn't a zip archive).")
    with z:
        names = set(z.namelist())
        info = {"type": "unknown", "id": None, "name": None, "version": None, "authors": [],
                "description": None, "minecraft": None, "depends": [], "provides": []}
        if "fabric.mod.json" in names:
            meta = _json(_read_member(z, "fabric.mod.json")) or {}
            deps = meta.get("depends") if isinstance(meta.get("depends"), dict) else {}
            info.update(type="fabric", id=meta.get("id"), name=meta.get("name") or meta.get("id"),
                        version=str(meta.get("version") or "") or None, authors=_fabric_authors(meta),
                        description=meta.get("description"),
                        minecraft=" || ".join(_as_list(deps.get("minecraft"))) or None,
                        depends=sorted(k for k in deps if k not in FABRIC_PROVIDED),
                        provides=sorted(set(_as_list(meta.get("provides"))) | _nested_ids(z, meta)))
        elif "paper-plugin.yml" in names or "plugin.yml" in names:
            paper = "paper-plugin.yml" in names
            text = (_read_member(z, "paper-plugin.yml" if paper else "plugin.yml") or b"").decode("utf-8", "replace")
            y = parse_yaml_top(text)
            authors = _as_list(y.get("authors")) or _as_list(y.get("author"))
            info.update(type="paper" if paper else "bukkit", id=y.get("name"), name=y.get("name"),
                        version=str(y.get("version") or "") or None, authors=authors,
                        description=y.get("description") if isinstance(y.get("description"), str) else None,
                        minecraft=y.get("api-version") if isinstance(y.get("api-version"), str) else None,
                        depends=_as_list(y.get("depend")) if not paper else [])
        elif "quilt.mod.json" in names:
            info["type"] = "quilt"
            info.update(_quilt_meta(_json(_read_member(z, "quilt.mod.json")) or {}))
        elif "META-INF/neoforge.mods.toml" in names:
            info["type"] = "neoforge"
            info.update(_toml_meta(z, "META-INF/neoforge.mods.toml", names))
        elif "META-INF/mods.toml" in names:
            info["type"] = "forge"
            info.update(_toml_meta(z, "META-INF/mods.toml", names))
        elif "mcmod.info" in names:
            info["type"] = "forge"
            info.update(_mcmod_meta(_read_member(z, "mcmod.info")))
        elif "velocity-plugin.json" in names:
            info["type"] = "velocity"
        elif "bungee.yml" in names:
            info["type"] = "bungee"
        return info


# ---------------------------------------------------------------------------
# Does it fit the server?
# ---------------------------------------------------------------------------
WHAT = {"fabric": "a Fabric mod", "paper": "a Paper plugin", "bukkit": "a Bukkit/Spigot plugin",
        "quilt": "a Quilt mod", "forge": "a Forge mod", "neoforge": "a NeoForge mod",
        "velocity": "a Velocity proxy plugin", "bungee": "a BungeeCord proxy plugin"}


# NeoForge kept loading Forge's META-INF/mods.toml up to Minecraft 1.20.4; from 1.20.5 it only
# reads its own neoforge.mods.toml
NEOFORGE_OWN_FILE = (1, 20, 5)


def accepted(kind, minecraft=None):
    """The jar types (see inspect_jar) this kind of server loads."""
    if kind in ("paper", "purpur"):
        return {"paper", "bukkit"}
    if kind == "quilt":
        return {"quilt", "fabric"}
    if kind == "neoforge":
        old = bool(minecraft) and javaservers.is_release(minecraft) \
            and javaservers.version_key(minecraft) < NEOFORGE_OWN_FILE
        return {"neoforge", "forge"} if old else {"neoforge"}
    return {kind} if kind in FOLDERS else set()


def refusal(kind, jar_type, minecraft=None):
    """Why this jar can't be used on this kind of server, or None if it can."""
    if jar_type in accepted(kind, minecraft):
        return None
    what = WHAT.get(jar_type)
    if kind == "vanilla":
        return ("Vanilla servers can't load mods or plugins. Make a Paper server for plugins or a "
                "Fabric server for mods (➕ New Server), then move the world over with a backup.")
    if kind == "bedrock":
        return "Bedrock servers don't use .jar files. Add-ons for Bedrock are .mcpack or .mcaddon files."
    label = LABELS.get(kind, kind.capitalize())
    if what is None:
        return (f"This jar has no {'plugin' if NOUNS.get(kind) == 'plugin' else label + ' mod or plugin'} "
                f"information, so it isn't a {NOUNS.get(kind, 'mod')} for this {label} server.")
    hint = f" Download the {label}{' (or Bukkit)' if NOUNS.get(kind) == 'plugin' else ''} version of it."
    if kind == "neoforge" and jar_type == "forge":
        hint = (f" NeoForge loads Forge mods only up to Minecraft 1.20.4. Download the NeoForge version of it.")
    elif jar_type in ("forge", "neoforge", "fabric", "quilt") and NOUNS.get(kind) == "mod":
        hint += f" {label} can't load {WHAT[jar_type][2:]}s."
    return f"This is {what}, and this is a {label} server.{hint}"


def _maven_range(declared, version):
    """A Forge/NeoForge versionRange such as "[1.20.1,1.21)", "[1.21.1]" or "1.20.1"
    (several ranges separated by commas are alternatives). True / False / None (can't tell)."""
    text = declared.strip()
    if not text:
        return None
    if text[0] not in "[(":
        return _cmp(version, text) >= 0 if re.fullmatch(r"[0-9]+(\.[0-9]+)*", text) else None
    found = re.findall(r"([\[(])\s*([0-9.]*)\s*(?:(,)\s*([0-9.]*))?\s*([\])])", text)
    if not found:
        return None
    for lo_b, lo, comma, hi, hi_b in found:
        if not comma:
            if lo and _cmp(version, lo) == 0:
                return True
            continue
        if lo and (_cmp(version, lo) < 0 or (lo_b == "(" and _cmp(version, lo) == 0)):
            continue
        if hi and (_cmp(version, hi) > 0 or (hi_b == ")" and _cmp(version, hi) == 0)):
            continue
        return True
    return False


def _cmp(a, b):
    ka, kb = javaservers.version_key(a), javaservers.version_key(b)
    n = max(len(ka), len(kb))
    ka, kb = ka + (0,) * (n - len(ka)), kb + (0,) * (n - len(kb))
    return (ka > kb) - (ka < kb)


def _fabric_clause(clause, version):
    """One space-separated Fabric version predicate set, all of which must hold."""
    for p in clause.split():
        m = re.match(r"^(>=|<=|>|<|=|~|\^)?(.+)$", p)
        op, want = m.group(1) or "", m.group(2)
        if want in ("*", "x", "X"):
            continue
        if re.search(r"[xX*]", want):                     # 1.21.x
            prefix = re.split(r"\.[xX*]", want)[0]
            if not (version == prefix or version.startswith(prefix + ".")):
                return False
            continue
        if not re.fullmatch(r"\d+(\.\d+)*(-[\w.+-]*)?(\+.*)?", want):
            return None                                   # snapshots (24w14a), odd forms: unknown
        want = re.match(r"^\d+(\.\d+)*", want).group(0)
        c = _cmp(version, want)
        if op in ("", "=") and c != 0:
            return False
        if (op == ">=" and c < 0) or (op == ">" and c <= 0) or (op == "<=" and c > 0) or (op == "<" and c >= 0):
            return False
        if op == "~":                                     # same major.minor, at least want
            base = want.split(".")[:2]
            if c < 0 or version.split(".")[:2] != base:
                return False
        if op == "^":                                     # same major, at least want
            if c < 0 or version.split(".")[0] != want.split(".")[0]:
                return False
    return True


def fits_minecraft(jar_type, declared, minecraft):
    """True / False, or None when it can't be told."""
    if not declared or not minecraft:
        return None
    if not javaservers.is_release(minecraft):
        return None
    if jar_type == "fabric":
        results = [_fabric_clause(c.strip(), minecraft) for c in declared.split("||") if c.strip()]
        if any(r is True for r in results):
            return True
        return None if any(r is None for r in results) else False
    if jar_type in ("paper", "bukkit"):                   # api-version: the oldest API it needs
        return _cmp(minecraft, declared) >= 0
    if jar_type in ("forge", "neoforge"):
        if re.fullmatch(r"[0-9]+(\.[0-9]+)+", declared.strip()):     # mcmod.info: one exact version
            return _cmp(minecraft, declared.strip()) == 0
        return _maven_range(declared, minecraft)
    if jar_type == "quilt":
        results = [_fabric_clause(c.strip(), minecraft) for c in declared.split("||") if c.strip()]
        if any(r is True for r in results):
            return True
        return None if any(r is None for r in results) else False
    return None


# ---------------------------------------------------------------------------
# The installed list (Java)
# ---------------------------------------------------------------------------
def _jar_meta(server, folder, entry):
    key = (server, folder, entry["name"], entry["size"], entry["modified"])
    hit = _meta_cache.get(key)
    if hit is not None:
        return hit
    try:
        fd, _ = files._open_file(server, f"{folder}/{entry['name']}")
    except files.FileError as e:
        return {"type": "unknown", "error": str(e)}
    try:
        with os.fdopen(fd, "rb") as f:
            meta = inspect_jar(f)
    except ModError as e:
        meta = {"type": "unknown", "error": str(e)}
    if len(_meta_cache) > 2000:
        _meta_cache.clear()
    _meta_cache[key] = meta
    return meta


def _is_jar_name(name):
    return name.endswith(".jar") or name.endswith(".jar" + DISABLED)


def list_installed(server):
    kind, minecraft = server_kind(server)
    out = {"server": server, "kind": kind, "minecraft": minecraft, "folder": FOLDERS.get(kind),
           "noun": NOUNS.get(kind), "accepts": [".jar"] if kind in FOLDERS else
           [".mcpack", ".mcaddon"] if kind == "bedrock" else [], "items": []}
    if kind == "bedrock":
        out["items"] = _bedrock_list(server)
        return out
    if kind not in FOLDERS:
        return out
    folder = FOLDERS[kind]
    try:
        entries = files.list_dir(server, folder)["entries"]
    except files.FileError as e:
        if e.status == 404:
            return out
        raise
    items = []
    for e in entries:
        if e["type"] != "file" or not _is_jar_name(e["name"]):
            continue
        meta = _jar_meta(server, folder, e)
        enabled = e["name"].endswith(".jar")
        items.append({"file": e["name"], "size": e["size"], "modified": e["modified"], "enabled": enabled, **meta,
                      "problem": refusal(kind, meta.get("type"), minecraft) if meta.get("type") else None,
                      "fits": fits_minecraft(meta.get("type"), meta.get("minecraft"), minecraft)})
    present = set()
    for i in items:
        if i["enabled"]:
            present.update(filter(None, [i.get("id")] + list(i.get("provides") or [])))
    for i in items:
        missing = [d for d in i.get("depends") or [] if d not in present]
        if kind in ("fabric", "quilt") and present & {"fabric-api", "quilted_fabric_api", "qsl"}:
            missing = [d for d in missing if not d.startswith("fabric-") and d != "fabric"]
        i["missing"] = missing
    items.sort(key=lambda i: (i.get("name") or i["file"]).lower())
    out["items"] = items
    return out


# ---------------------------------------------------------------------------
# Installing (Java)
# ---------------------------------------------------------------------------
def _ensure_folder(server, folder, actor):
    try:
        files.list_dir(server, folder)
    except files.FileError as e:
        if e.status != 404:
            raise
        files.make_dir(server, folder, actor=actor)


def _chunks(fileobj):
    fileobj.seek(0)
    while True:
        chunk = fileobj.read(1 << 20)
        if not chunk:
            break
        yield chunk


def install(server, name, fileobj, overwrite=False, actor="?"):
    """Installs a dropped file: a jar into mods/ or plugins/, or a Bedrock pack. fileobj is a
    seekable binary file. Raises ModError (409) when it would replace something and
    overwrite is False."""
    name = os.path.basename((name or "").replace("\\", "/"))
    files._check_name(name)
    kind, minecraft = server_kind(server)
    lower = name.lower()
    if kind == "bedrock":
        if not lower.endswith((".mcpack", ".mcaddon", ".zip")):
            raise ModError("Bedrock servers take add-ons as .mcpack or .mcaddon files.")
        return install_bedrock(server, name, fileobj, overwrite, actor)
    if not lower.endswith(".jar"):
        raise ModError("Drop .jar files here. To upload other files, use the Files tab.")
    meta = inspect_jar(fileobj)
    why = refusal(kind, meta["type"], minecraft)
    if why:
        raise ModError(why)
    folder = FOLDERS[kind]
    _ensure_folder(server, folder, actor)
    # The same mod or plugin under another file name (an older version): replacing it, not
    # adding a second copy, which the loaders refuse to start with.
    current = list_installed(server)["items"]
    same = [i for i in current if meta.get("id") and i.get("id") == meta["id"] and i["file"] != name]
    exists = any(i["file"] == name for i in current) or _exists(server, f"{folder}/{name}")
    if (same or exists) and not overwrite:
        old = same[0] if same else None
        what = f"{meta.get('name') or name}"
        if old:
            raise ModError(f"{what} {old.get('version') or ''} is already installed as {old['file']}. "
                           f"Replace it with {meta.get('version') or name}?".replace("  ", " "), 409)
        raise ModError(f"{folder}/{name} already exists. Replace it?", 409)
    files.upload(server, folder, name, _chunks(fileobj), overwrite=True, actor=actor)
    for old in same:
        files.delete(server, f"{folder}/{old['file']}", actor=actor)
    fit = fits_minecraft(meta["type"], meta.get("minecraft"), minecraft)
    manager.write_log(f"[MODS] {actor} installed {meta.get('name') or name} {meta.get('version') or ''}".rstrip()
                      + f" on {server} ({folder}/{name})" + (f", replacing {', '.join(o['file'] for o in same)}" if same else "")
                      + ". Restart the server to load it.")
    return {"installed": [{"file": name, "folder": folder, **meta}], "replaced": [o["file"] for o in same],
            "fits": fit, "restart": True}


def _exists(server, path):
    dfd, name = files._parent(server, path)
    try:
        os.stat(name, dir_fd=dfd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False
    finally:
        os.close(dfd)


def _java_item(server, file):
    kind, _ = server_kind(server)
    if kind not in FOLDERS:
        raise ModError(f"{server} has no mods or plugins folder.")
    files._check_name(file)
    if not _is_jar_name(file):
        raise ModError("That isn't a mod or plugin file.")
    return FOLDERS[kind]


def set_enabled(server, item, enabled, actor="?"):
    kind, _ = server_kind(server)
    if kind == "bedrock":
        return _bedrock_set_enabled(server, item, enabled, actor)
    folder = _java_item(server, item)
    is_on = item.endswith(".jar")
    if is_on == enabled:
        return {"file": item}
    new = item + DISABLED if not enabled else item[: -len(DISABLED)]
    files.rename(server, f"{folder}/{item}", new, actor=actor)
    manager.write_log(f"[MODS] {actor} {'enabled' if enabled else 'disabled'} {folder}/{new if enabled else item} "
                      f"on {server}. Restart the server for this to take effect.")
    return {"file": new}


def remove(server, item, actor="?"):
    kind, _ = server_kind(server)
    if kind == "bedrock":
        return _bedrock_remove(server, item, actor)
    folder = _java_item(server, item)
    files.delete(server, f"{folder}/{item}", actor=actor)
    manager.write_log(f"[MODS] {actor} removed {folder}/{item} from {server}. Restart the server to unload it.")
    return {"removed": item}


# ---------------------------------------------------------------------------
# Bedrock packs
# ---------------------------------------------------------------------------
def _strip_comments(text):
    """Bedrock manifests sometimes carry // and /* */ comments, which JSON doesn't allow."""
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
            out.append(c)
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        else:
            out.append(c)
        i += 1
    return "".join(out)


def parse_manifest(data):
    try:
        m = json.loads(_strip_comments(data.decode("utf-8-sig", "replace")), strict=False)
    except ValueError:
        raise ModError("A pack's manifest.json isn't valid JSON.")
    header = m.get("header") if isinstance(m, dict) else None
    if not isinstance(header, dict) or not header.get("uuid"):
        raise ModError("A pack's manifest.json has no header uuid.")
    types = {mod.get("type") for mod in m.get("modules") or [] if isinstance(mod, dict)}
    if types & {"resources"} and not types & {"data", "script", "client_data"}:
        pack_type = "resource"
    elif types & {"data", "script", "client_data"}:
        pack_type = "behavior"
    elif types & {"skin_pack"}:
        raise ModError("This is a skin pack; skin packs are for players, not servers.")
    elif types & {"world_template"}:
        raise ModError("This is a world template. Import it as a world instead.")
    else:
        raise ModError("A pack's manifest.json doesn't say whether it's a behavior or resource pack.")
    version = header.get("version")
    if isinstance(version, str):
        version = [int(x) for x in version.split(".")[:3] if x.isdigit()]
    if not isinstance(version, list):
        version = [1, 0, 0]
    return {"uuid": str(header["uuid"]).lower(), "version": version, "name": _plain(header.get("name")) or "pack",
            "description": _plain(header.get("description")), "type": pack_type}


def _plain(text):
    """Pack names use §-codes and pack.name translation keys; keep something readable."""
    if not isinstance(text, str):
        return None
    return re.sub(r"§.", "", text).strip() or None


def _clean_entry(name):
    name = name.replace("\\", "/")
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        raise ModError(f"The add-on has an entry with an absolute path: {name}")
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if ".." in parts or any("\x00" in p or len(p.encode()) > files.MAX_NAME for p in parts):
        raise ModError(f"The add-on has an entry that points outside it: {name}")
    return parts


def _packs_in_zip(z, depth=0):
    """[(manifest info, {relative parts tuple: ZipInfo} , zipfile)] for each pack in an .mcpack or
    .mcaddon (packs can be folders, or nested .mcpack files)."""
    infos = z.infolist()
    if len(infos) > MAX_PACK_FILES:
        raise ModError(f"The add-on has more than {MAX_PACK_FILES} files.")
    if sum(i.file_size for i in infos) > MAX_PACK_BYTES:
        raise ModError("The add-on unpacks to more than 512 MB.")
    entries = {}
    for i in infos:
        if stat.S_ISLNK(i.external_attr >> 16):
            continue                                     # never create links from an upload
        parts = _clean_entry(i.filename)
        if parts and not i.is_dir() and parts[0] not in ("__MACOSX",):
            entries[tuple(parts)] = i
    packs = []
    roots = sorted({k[:-1] for k in entries if k[-1] == "manifest.json"}, key=len)
    for root in roots:
        if any(root[:len(r)] == r for r in roots if r != root and len(r) < len(root)):
            continue                                     # a manifest inside another pack
        meta = parse_manifest(z.read(entries[root + ("manifest.json",)]))
        members = {k[len(root):]: v for k, v in entries.items() if k[:len(root)] == root}
        packs.append((meta, members, z))
    if depth == 0:
        for k, info in entries.items():
            if k[-1].lower().endswith(".mcpack"):
                data = z.read(info)
                packs.extend(_packs_in_zip(zipfile.ZipFile(io.BytesIO(data)), depth + 1))
    return packs


def _folder_slug(meta):
    slug = NAME_RE.sub("_", meta["name"]).strip("._")[:40] or "pack"
    return f"{slug}_{meta['uuid'][:8]}"


def _mkdir(server, path):
    """Like files.make_dir, without a log line per folder; an existing folder is fine."""
    dfd, name = files._parent(server, path)
    try:
        files._check_name(name)
        try:
            os.mkdir(name, 0o755, dir_fd=dfd)
        except FileExistsError:
            return
        uid, gid = files._owner(dfd)
        os.chown(name, uid, gid, dir_fd=dfd, follow_symlinks=False)
    finally:
        os.close(dfd)


def _write_tree(server, base, members, z):
    """Writes the pack's files under `base` (a path in the server folder that doesn't exist yet)."""
    _mkdir(server, base)
    made = {()}
    for parts, info in sorted(members.items()):
        for n in range(1, len(parts)):
            if parts[:n] not in made:
                _mkdir(server, "/".join((base,) + parts[:n]))
                made.add(parts[:n])
        dfd = files._open_dir(server, files._parts(base) + list(parts[:-1]))
        try:
            with z.open(info) as src:
                files._write_into(dfd, parts[-1], iter(lambda: src.read(1 << 20), b""), overwrite=True)
        finally:
            os.close(dfd)


def _world_dir(server):
    return f"worlds/{manager.get_active_world_name(server)}"


def _world_list(server, pack_type):
    path = f"{_world_dir(server)}/world_{pack_type}_packs.json"
    try:
        data = files.read_text(server, path)["content"]
        v = json.loads(data)
        return path, (v if isinstance(v, list) else [])
    except files.FileError as e:
        if e.status == 404:
            return path, []
        raise
    except ValueError:
        return path, []


def _save_world_list(server, pack_type, packs, actor):
    world = _world_dir(server)
    for p in ("worlds", world):
        _mkdir(server, p)
    path = f"{world}/world_{pack_type}_packs.json"
    dfd, name = files._parent(server, path)
    try:
        files._write_into(dfd, name, [json.dumps(packs, indent=2).encode()], overwrite=True)
    finally:
        os.close(dfd)


def _installed_packs(server):
    """{uuid: {folder, type, name, version, description}} for the packs in the server folder."""
    found = {}
    for pack_type in ("behavior", "resource"):
        top = f"{pack_type}_packs"
        try:
            entries = files.list_dir(server, top)["entries"]
        except files.FileError:
            continue
        for e in entries:
            if e["type"] != "dir" or e["name"].lower().startswith(BUILTIN_PACKS):
                continue
            try:
                data = files.read_text(server, f"{top}/{e['name']}/manifest.json")["content"].encode()
                meta = parse_manifest(data)
            except (files.FileError, ModError):
                continue
            found[meta["uuid"]] = {**meta, "folder": f"{top}/{e['name']}", "type": pack_type}
    return found


def _bedrock_list(server):
    packs = _installed_packs(server)
    enabled = {}
    for pack_type in ("behavior", "resource"):
        for p in _world_list(server, pack_type)[1]:
            if isinstance(p, dict) and p.get("pack_id"):
                enabled[str(p["pack_id"]).lower()] = pack_type
    items = [{"file": uuid, "id": uuid, "name": p["name"], "version": ".".join(map(str, p["version"])),
              "description": p.get("description"), "type": p["type"], "folder": p["folder"],
              "enabled": uuid in enabled, "missing": [], "problem": None, "fits": None}
             for uuid, p in packs.items()]
    for uuid, pack_type in enabled.items():
        if uuid not in packs:
            items.append({"file": uuid, "id": uuid, "name": f"Missing {pack_type} pack", "version": None,
                          "type": pack_type, "folder": None, "enabled": True, "missing": [], "fits": None,
                          "problem": "The world uses this pack, but it isn't in the server folder."})
    items.sort(key=lambda i: (i["name"] or "").lower())
    return items


def install_bedrock(server, name, fileobj, overwrite=False, actor="?"):
    try:
        z = zipfile.ZipFile(fileobj)
    except (zipfile.BadZipFile, OSError):
        raise ModError("This isn't a valid .mcpack or .mcaddon file (it isn't a zip archive).")
    with z:
        packs = _packs_in_zip(z)
        if not packs:
            raise ModError("No pack found: the file has no manifest.json.")
        installed = _installed_packs(server)
        clash = [m for m, _, _ in packs if m["uuid"] in installed]
        if clash and not overwrite:
            names = ", ".join(f"{m['name']} {'.'.join(map(str, installed[m['uuid']]['version']))}" for m in clash)
            raise ModError(f"{names} {'is' if len(clash) == 1 else 'are'} already installed. Replace "
                           f"{'it' if len(clash) == 1 else 'them'}?", 409)
        done = []
        for meta, members, zf in packs:
            top = f"{meta['type']}_packs"
            _ensure_folder(server, top, actor)
            staging = f"{top}/.redstonepanel-pack-{secrets.token_hex(4)}"
            try:
                _write_tree(server, staging, members, zf)
                old = installed.get(meta["uuid"])
                if old:
                    files.delete(server, old["folder"], actor=actor)
                final = old["folder"] if old and old["type"] == meta["type"] else f"{top}/{_folder_slug(meta)}"
                if _exists(server, final):
                    final = f"{top}/{_folder_slug(meta)}_{secrets.token_hex(2)}"
                files.rename(server, staging, final.rsplit("/", 1)[1], actor=actor)
            except BaseException:
                try:
                    files.delete(server, staging, actor=actor)
                except files.FileError:
                    pass
                raise
            path, world = _world_list(server, meta["type"])
            world = [p for p in world if not (isinstance(p, dict) and str(p.get("pack_id", "")).lower() == meta["uuid"])]
            world.append({"pack_id": meta["uuid"], "version": meta["version"]})
            _save_world_list(server, meta["type"], world, actor)
            done.append({**meta, "folder": final})
    manager.write_log(f"[MODS] {actor} installed {', '.join(d['name'] + ' (' + d['type'] + ')' for d in done)} on "
                      f"{server} and enabled {'it' if len(done) == 1 else 'them'} for the world. Restart the server "
                      "to load them.")
    return {"installed": done, "replaced": [m["name"] for m in clash], "fits": None, "restart": True}


def _bedrock_find(server, uuid):
    uuid = str(uuid or "").lower()
    if not re.fullmatch(r"[0-9a-f-]{8,64}", uuid):
        raise ModError("That isn't a pack id.")
    return uuid, _installed_packs(server).get(uuid)


def _bedrock_set_enabled(server, uuid, enabled, actor):
    uuid, pack = _bedrock_find(server, uuid)
    types = [pack["type"]] if pack else ["behavior", "resource"]
    for pack_type in types:
        path, world = _world_list(server, pack_type)
        world = [p for p in world if not (isinstance(p, dict) and str(p.get("pack_id", "")).lower() == uuid)]
        if enabled:
            if not pack:
                raise ModError("That pack isn't in the server folder.", 404)
            world.append({"pack_id": uuid, "version": pack["version"]})
        _save_world_list(server, pack_type, world, actor)
    label = pack["name"] if pack else uuid
    manager.write_log(f"[MODS] {actor} {'enabled' if enabled else 'disabled'} {label} for {server}'s world. "
                      "Restart the server for this to take effect.")
    return {"file": uuid}


def _bedrock_remove(server, uuid, actor):
    uuid, pack = _bedrock_find(server, uuid)
    _bedrock_set_enabled(server, uuid, False, actor)
    if pack:
        files.delete(server, pack["folder"], actor=actor)
    manager.write_log(f"[MODS] {actor} removed {pack['name'] if pack else uuid} from {server}.")
    return {"removed": uuid}
