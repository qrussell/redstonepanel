"""World maps (1.30): a Map tab for every server.

Three ways a server gets a map:
  - a map plugin or mod the panel installs with one click (BlueMap, or squaremap as the light
    choice for a Raspberry Pi), from Modrinth through core/catalog.py;
  - a map tool the admin installed by hand (BlueMap, squaremap, Pl3xMap, Dynmap), recognised from
    its jar and its config file;
  - for vanilla Java and Bedrock, which can't run plugins, a map the panel draws itself on a
    schedule with BlueMap's standalone renderer (core/mapdraw.py; Bedrock worlds are converted
    to Java first with Chunker).

A map is someone else's web page (the tool's HTML and JavaScript, with text from the game in it),
so it is never served at the panel's own address, where a mistake in it could act as the signed-in
admin. The join site (core/joinsite.py, its own port and so its own origin) serves maps through
core/mapsite.py: /map/<member>/<server>/ for people signed in to the panel (a one-minute ticket
from the panel becomes a cookie of the map's own), and /join/<link>/map/ on join pages that show
the map. Each tool's own web server is set to listen on 127.0.0.1 only, so its port is never
opened in the firewall or forwarded; the join site passes requests on to it. When the server is
stopped or asleep, maps that live in files (BlueMap, squaremap, Pl3xMap, and every map the panel
draws) are served from those files, so opening a map never starts or wakes a server.

Only GET and HEAD reach a map: nothing on the web can write to the game (Dynmap's web chat stays
off). On a join page the players' positions are left out unless the admin chose to show them.
"""
import email.utils
import http.client
import json
import os
import re
import threading
import time
from urllib.parse import quote

from core import manager, modports
from core.platform import paths, services
from core.settings import DATA_DIR

SETTINGS_FILE = os.path.join(DATA_DIR, "world-maps.json")
DRAWN_DIR = ".redstonepanel-map"            # in the server folder: the map the panel draws (never backed up)
LOOPBACK = ("127.0.0.1", "localhost", "::1")
TICKET_SECONDS = 60
VIEWER_SECONDS = 2 * 3600
PROXY_TIMEOUT = 15
JSON_MAX = 4 * 1024 * 1024                 # a live data file filtered for a join page
CHUNK = 1 << 16
_lock = threading.Lock()

# id: what the panel knows about each tool. configs: where its web server settings are (first
# found is used; the plugin folder, then the mod one); fmt/bind/port: the keys (modports' reader);
# web: where its files are, relative to the config file's folder (None: only its own web server
# can show it); webroot_key: a key in the config that moves the files; players: paths (relative
# to the map's root) whose JSON lists where players are; ids: mod/plugin ids its jars declare.
TOOLS = {
    "bluemap": {"name": "BlueMap", "slug": "bluemap", "ids": ("bluemap",),
                "configs": ("plugins/BlueMap/webserver.conf", "config/bluemap/webserver.conf"),
                "fmt": "hocon", "bind": ("ip",), "port": ("port",), "default_port": 8100,
                "web": "bluemap/web", "web_from_root": True, "webroot_key": ("webroot",),
                "players": (r"^maps/[^/]+/live/players\.json$",), "blurb": "3D map"},
    "squaremap": {"name": "squaremap", "slug": "squaremap", "ids": ("squaremap",),
                  "configs": ("plugins/squaremap/config.yml", "config/squaremap/config.yml"),
                  "fmt": "yaml", "bind": ("settings", "internal-webserver", "bind"),
                  "port": ("settings", "internal-webserver", "port"), "default_port": 8080,
                  "web": "web", "web_from_root": False, "webroot_key": None,
                  "players": (r"^tiles/players\.json$",), "blurb": "flat 2D map, light enough for a Raspberry Pi"},
    "pl3xmap": {"name": "Pl3xMap", "slug": "pl3xmap", "ids": ("pl3xmap",),
                "configs": ("plugins/Pl3xMap/config.yml", "config/pl3xmap/config.yml"),
                "fmt": "yaml", "bind": ("settings", "internal-webserver", "bind"),
                "port": ("settings", "internal-webserver", "port"), "default_port": 8080,
                "web": "web", "web_from_root": False, "webroot_key": None,
                "players": (r"^tiles/players\.json$",), "blurb": "flat 2D map"},
    "dynmap": {"name": "Dynmap", "slug": "dynmap", "ids": ("dynmap",),
               "configs": ("plugins/dynmap/configuration.txt", "dynmap/configuration.txt"),
               "fmt": "yaml", "bind": ("webserver-bindaddress",), "port": ("webserver-port",), "default_port": 8123,
               "web": None, "web_from_root": False, "webroot_key": None,
               "players": (r"^up/world/[^/]+/[0-9]+$",), "blurb": "2D map"},
}
# What Add a map installs (Quentin's choice, 27 Nov 2026): BlueMap, and squaremap as the light one
ADDABLE = ("bluemap", "squaremap")
PLUGIN_KINDS = ("paper", "purpur", "fabric", "quilt", "forge", "neoforge")
ADD_KINDS = {"bluemap": ("paper", "purpur", "fabric", "forge", "neoforge"),
             "squaremap": ("paper", "purpur", "fabric", "forge", "neoforge")}
SCHEDULES = {"nightly": "Every night", "backup": "After each scheduled backup", "manual": "Only when I press Draw now"}
WORLDS = ("overworld", "nether", "end")
# local_only: the panel keeps the tool's web server on 127.0.0.1. On for maps the panel added; a
# tool added by hand keeps its own setting until the admin chooses "Only through the panel".
DEFAULTS = {"drawn": False, "local_only": False, "accept_download": False, "schedule": "nightly",
            "time": "03:00", "worlds": ["overworld"]}
TIME_RE = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])$")


class MapError(ValueError):
    """Something about a map that can't be done (message is safe to show)."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Settings: {server: {...}}
# ---------------------------------------------------------------------------
def _load():
    try:
        with open(SETTINGS_FILE) as f:
            d = json.load(f)
        return {k: v for k, v in d.items() if isinstance(v, dict)} if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(d):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=1, sort_keys=True)
    os.replace(tmp, SETTINGS_FILE)


def settings(server):
    s = dict(DEFAULTS)
    s.update({k: v for k, v in (_load().get(server) or {}).items() if k in DEFAULTS})
    s["worlds"] = [w for w in (s.get("worlds") or []) if w in WORLDS] or ["overworld"]
    return s


def _store(server, **changes):
    with _lock:
        d = _load()
        cur = d.get(server) or {}
        cur.update(changes)
        d[server] = cur
        _save(d)
    return settings(server)


def forget(server):
    """A server was removed or renamed away: its map settings go with it."""
    with _lock:
        d = _load()
        if d.pop(server, None) is not None:
            _save(d)


def rename(old, new):
    with _lock:
        d = _load()
        if old in d:
            d[new] = d.pop(old)
            _save(d)


# ---------------------------------------------------------------------------
# Reading the tools' config files (modports' line reader: text in, text out)
# ---------------------------------------------------------------------------
def _read(server, path):
    from core import files
    try:
        return files.read_text(server, path)
    except Exception:
        return None


def _value(text, fmt, key):
    _, v = modports._find(text, fmt, key)
    return None if v is None else v.strip().strip("'\"")


def listens_locally(mod, text, server=None):
    """True when this tool's web server listens on this machine only: its config says so, or
    (server given) the panel keeps it there, which it does within two minutes of the tool writing
    its config, so its port is never opened in between."""
    t = TOOLS.get(mod)
    if not t:
        return False
    if (_value(text, t["fmt"], t["bind"]) or "").lower() in LOOPBACK:
        return True
    return bool(server) and settings(server)["local_only"]


def set_value(text, fmt, key, value, quote_it=True):
    """The config text with key set to value: the existing line changed (the rest of it kept), or
    for a top-level key, a line added at the end. None when a nested key isn't there (the tool
    writes it on its first start; the panel tries again after that)."""
    shown = f'"{value}"' if quote_it and fmt == "hocon" else str(value)
    idx, old = modports._find(text, fmt, key)
    if idx is not None:
        lines = text.split("\n")
        line = lines[idx]
        m = re.match(r"^(\s*(?:\"[^\"]+\"|[A-Za-z0-9_.-]+)\s*[:=]\s*)(.*)$", line)
        if not m:
            return None
        rest = m.group(2)
        cm = re.search(r"\s+(#|//).*$", rest)
        comment = cm.group(0) if cm else ""
        lines[idx] = m.group(1) + shown + comment
        return "\n".join(lines)
    if len(key) != 1:
        return None
    sep = ": " if fmt in ("yaml", "hocon") else "="
    return text.rstrip("\n") + "\n" + key[0] + sep + shown + "\n"


def _jar_ids(server):
    """The ids (and file names) of the enabled mods or plugins in the server, lower case."""
    from core import mods
    try:
        items = mods.list_installed(server)["items"]
    except Exception:
        return set()
    out = set()
    for i in items:
        if not i.get("enabled"):
            continue
        if i.get("id"):
            out.add(str(i["id"]).lower())
        out.add(str(i.get("file") or "").lower())
    return out


def _has_jar(tool, ids):
    return any(x in ids for x in tool["ids"]) or any(
        n.endswith(".jar") and n.startswith(tool["ids"]) for n in ids)


def _clean_rel(path):
    """A path relative to the server folder, or None if it tries to leave it."""
    p = str(path or "").replace("\\", "/").strip().strip("/")
    if not p or p.startswith("~") or re.match(r"^[A-Za-z]:", p):
        return None
    parts = p.split("/")
    if any(x in ("", ".", "..") for x in parts):
        return None
    return "/".join(parts)


def detect(server):
    """[{id, name, config, port, bind, local, web, jar}] of the map tools in this server (by its
    jar or its config file), in TOOLS order."""
    ids = _jar_ids(server)
    out = []
    for tid, t in TOOLS.items():
        found = None
        for p in t["configs"]:
            f = _read(server, p)
            if f is not None:
                found = (p, f["content"])
                break
        jar = _has_jar(t, ids)
        if not found and not jar:
            continue
        port, bind, web = t["default_port"], None, None
        if found:
            pt = _value(found[1], t["fmt"], t["port"])
            port = int(pt) if (pt or "").isdigit() else t["default_port"]
            bind = _value(found[1], t["fmt"], t["bind"])
        if t["web"]:
            if t["web_from_root"]:
                web = t["web"]
                if found and t["webroot_key"]:
                    web = _clean_rel(_value(found[1], t["fmt"], t["webroot_key"])) or t["web"]
            elif found:
                web = _clean_rel(os.path.dirname(found[0]) + "/" + t["web"])
            else:
                web = _clean_rel(("plugins/squaremap" if tid == "squaremap" else "plugins/Pl3xMap") + "/web")
        out.append({"id": tid, "name": t["name"], "config": found[0] if found else None, "port": port,
                    "bind": bind, "local": (bind or "").lower() in LOOPBACK, "web": web, "jar": jar})
    return out


def _kind(server):
    from core import mods
    try:
        return mods.server_kind(server)
    except Exception:
        return None, None


def current(server, kind=None):
    """The map this server shows, or None: the panel's drawing for vanilla and Bedrock, else the
    first map tool found (one with a jar first)."""
    kind = kind if kind is not None else _kind(server)[0]
    s = settings(server)
    if s["drawn"] and kind in ("vanilla", "bedrock"):
        return {"id": "bluemap", "name": "BlueMap", "drawn": True, "config": None, "port": None, "bind": None,
                "local": True, "web": DRAWN_DIR + "/web", "jar": False}
    found = detect(server)
    if not found:
        return None
    best = next((f for f in found if f["jar"]), found[0])
    return dict(best, drawn=False)


def _running(server):
    try:
        return services.state(server) == "active"
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Keeping each tool's web server on this machine only, and BlueMap's texture download
# ---------------------------------------------------------------------------
def _write(server, path, old, content, actor):
    from core import files
    files.write_text(server, path, content, expected_modified=old["modified"], actor=actor)


def _bluemap_core(tool_cfg_path):
    return os.path.dirname(tool_cfg_path) + "/core.conf"


def sync(server, actor="panel"):
    """Makes the server's map tools match its map settings: web server on 127.0.0.1 (unless the
    admin kept it reachable), and BlueMap's accept-download once the admin agreed. Returns what
    changed: [text]. A running server picks the change up at its next start (BlueMap: at once,
    through `bluemap reload` when RCON is on)."""
    s = settings(server)
    stored = _load().get(server) or {}
    changed = []
    for f in detect(server):
        t = TOOLS[f["id"]]
        if not f["config"]:
            continue                      # the tool writes its files on its first start
        if "local_only" in stored and not stored["local_only"] and f["bind"] == "127.0.0.1":
            cur = _read(server, f["config"])
            new = set_value(cur["content"], t["fmt"], t["bind"], "0.0.0.0") if cur else None
            if new and new != cur["content"]:
                try:
                    _write(server, f["config"], cur, new, actor)
                    changed.append(f"{t['name']} can be reached from the network again")
                except Exception as e:
                    manager.write_log(f"[MAP] Couldn't change where {t['name']} of {server} listens: {e}")
        if s["local_only"] and not f["local"]:
            cur = _read(server, f["config"])
            new = set_value(cur["content"], t["fmt"], t["bind"], "127.0.0.1") if cur else None
            if new and new != cur["content"]:
                try:
                    _write(server, f["config"], cur, new, actor)
                    changed.append(f"{t['name']} now listens only on this machine")
                except Exception as e:
                    manager.write_log(f"[MAP] Couldn't set {t['name']} of {server} to listen on 127.0.0.1: {e}")
        if f["id"] == "bluemap" and s["accept_download"]:
            path = _bluemap_core(f["config"])
            cur = _read(server, path)
            if cur and (_value(cur["content"], "hocon", ("accept-download",)) or "").lower() != "true":
                new = set_value(cur["content"], "hocon", ("accept-download",), "true", quote_it=False)
                if new:
                    try:
                        _write(server, path, cur, new, actor)
                        changed.append("BlueMap may now download Minecraft's textures")
                    except Exception as e:
                        manager.write_log(f"[MAP] Couldn't turn on BlueMap's texture download for {server}: {e}")
    if changed:
        manager.write_log(f"[MAP] {server}: " + "; ".join(changed) + ".")
        if _running(server) and any("BlueMap" in c for c in changed):
            _bluemap_reload(server)
    return changed


def _bluemap_reload(server):
    from core import backups, rcon
    try:
        send = backups._java_rcon(manager.get_server_dir(server))
        if send:
            send("bluemap reload", timeout=30)
            return True
    except (rcon.RconError, OSError):
        pass
    return False


def tick():
    """Every two minutes (manager.firewall_watcher): map tools kept on 127.0.0.1, and the join
    site started or stopped when a map tool was added or removed by hand."""
    sync_all()
    before = _wanted["value"]
    now = wanted(fresh=True)
    from core import joinsite
    from core import settings as panel_settings
    s = panel_settings.load()
    st = joinsite.status(s)
    if not st["managed"]:
        return                            # tests, or a panel not started by run.py
    if now != before or (now and not st["running"] and not st["error"]) or (st["running"] and not joinsite.wanted(s, now)):
        joinsite.apply(s)


def sync_all():
    """Every server with a map tool (the panel's 2-minute firewall round calls this)."""
    for name in modports._servers():
        try:
            if manager._is_java(name):
                sync(name)
        except Exception:
            continue


_wanted = {"at": 0.0, "value": False}
WANTED_TTL = 120


def wanted(fresh=False):
    """True when any server on this panel has a map to show (the join site then listens).
    Remembered for two minutes: finding map tools reads every server's mods."""
    now = time.monotonic()
    if not fresh and _wanted["at"] and now - _wanted["at"] < WANTED_TTL:
        return _wanted["value"]
    value = any(v.get("drawn") for v in _load().values())
    if not value:
        for name in modports._servers():
            try:
                if manager._is_java(name) and detect(name):
                    value = True
                    break
            except Exception:
                continue
    _wanted.update(at=now, value=value)
    return value


# ---------------------------------------------------------------------------
# What the Map tab shows
# ---------------------------------------------------------------------------
def _dir_size(server, rel):
    d = os.path.join(manager.get_server_dir(server), *rel.split("/"))
    total = 0
    for root, dirs, fs in os.walk(d):
        dirs[:] = [x for x in dirs if not os.path.islink(os.path.join(root, x))]
        for n in fs:
            try:
                st = os.lstat(os.path.join(root, n))
                total += st.st_size
            except OSError:
                pass
    return total


def _machine():
    import platform as _p
    from core import host
    try:
        mem = host.memory()["total"] or None
    except Exception:
        mem = None
    m = _p.machine().lower()
    return {"arm": m in ("aarch64", "arm64", "armv7l"), "cpus": os.cpu_count() or 1, "memory": mem}


def view(server):
    """Everything the Map tab needs."""
    manager._check_server(server)
    kind, minecraft = _kind(server)
    s = settings(server)
    cur = current(server, kind)
    from core import mapdraw
    out = {"server": server, "kind": kind, "minecraft": minecraft, "settings": s, "map": cur,
           "running": _running(server), "found": [], "schedules": SCHEDULES,
           "can_add": [{"id": t, "name": TOOLS[t]["name"], "blurb": TOOLS[t]["blurb"]}
                       for t in ADDABLE if kind in ADD_KINDS[t]],
           "can_draw": kind in ("vanilla", "bedrock"), "machine": _machine(),
           "draw": mapdraw.status(server) if kind in ("vanilla", "bedrock") else None,
           "size": None, "needs_restart": False, "accept_needed": False}
    if kind not in ("vanilla", "bedrock"):
        out["found"] = detect(server)
    if cur:
        if cur.get("web"):
            try:
                out["size"] = _dir_size(server, cur["web"])
            except Exception:
                out["size"] = None
        if not cur["drawn"]:
            out["needs_restart"] = cur["jar"] and not cur["config"]
            if cur["id"] == "bluemap" and cur["config"]:
                core = _read(server, _bluemap_core(cur["config"]))
                out["accept_needed"] = bool(core) and (
                    _value(core["content"], "hocon", ("accept-download",)) or "").lower() != "true"
        else:
            out["accept_needed"] = not s["accept_download"]
    from core import joinsite
    from core import settings as panel_settings
    ps = panel_settings.load()
    st = joinsite.status(ps)
    out["site"] = {"port": st["port"], "scheme": st["scheme"], "running": st["running"],
                   "join_url": ps.get("join_url") or ""}
    return out


def add(server, tool, accept_download=False, actor="?"):
    """Add a map: installs BlueMap or squaremap from Modrinth (Paper, Purpur, Fabric, Forge,
    NeoForge), or for vanilla and Bedrock turns on the panel's own drawing."""
    manager._check_server(server)
    kind, _ = _kind(server)
    if kind in ("vanilla", "bedrock"):
        if not accept_download:
            raise MapError("BlueMap needs Minecraft's block textures, which it downloads from Mojang. "
                           "Tick the box to agree, then press Draw a map.")
        s = _store(server, drawn=True, accept_download=True)
        from core import mapdraw
        mapdraw.apply_schedule(server)
        manager.write_log(f"[MAP] {actor} turned on the panel's map drawing for {server} "
                          f"({SCHEDULES[s['schedule']].lower()}).")
        _site_changed()
        return {"started": False, "restart": False}
    if tool not in ADDABLE:
        raise MapError("Choose BlueMap or squaremap.")
    if kind not in ADD_KINDS[tool]:
        raise MapError(f"{TOOLS[tool]['name']} isn't available for this kind of server.")
    if tool == "bluemap" and not accept_download:
        raise MapError("BlueMap needs Minecraft's block textures, which it downloads from Mojang. "
                       "Tick the box to agree, then press Add.")
    from core import catalog
    res = catalog.install(server, "modrinth", TOOLS[tool]["slug"], actor=actor)
    _store(server, local_only=True, accept_download=bool(accept_download) or settings(server)["accept_download"])
    manager.write_log(f"[MAP] {actor} added {TOOLS[tool]['name']} to {server}.")
    sync(server, actor=actor)
    _site_changed()
    return {"installed": res.get("installed"), "notes": res.get("notes"), "restart": True}


def update(server, new, actor="?"):
    """Saves the Map tab's settings."""
    manager._check_server(server)
    changes = {}
    if "local_only" in new:
        changes["local_only"] = bool(new["local_only"])
    if "accept_download" in new:
        changes["accept_download"] = bool(new["accept_download"])
    if "schedule" in new:
        if new["schedule"] not in SCHEDULES:
            raise MapError("Choose when the map is drawn.")
        changes["schedule"] = new["schedule"]
    if "time" in new:
        if not TIME_RE.match(str(new["time"] or "")):
            raise MapError("Enter a time as HH:MM.")
        changes["time"] = new["time"]
    if "worlds" in new:
        worlds = [w for w in (new["worlds"] or []) if w in WORLDS]
        if not worlds:
            raise MapError("Choose at least one world to draw.")
        changes["worlds"] = worlds
    s = _store(server, **changes)
    if s["drawn"]:
        from core import mapdraw
        mapdraw.apply_schedule(server)
    sync(server, actor=actor)
    manager.write_log(f"[MAP] {actor} changed the map settings of {server}.")
    return s


def turn_off_drawing(server, actor="?"):
    from core import mapdraw
    _store(server, drawn=False)
    mapdraw.apply_schedule(server)
    manager.write_log(f"[MAP] {actor} turned off the map drawing of {server}.")
    _site_changed()


def delete_files(server, actor="?"):
    """Delete the map: its drawn files go (the tool, its settings and markers stay); the next
    drawing starts from scratch."""
    manager._check_server(server)
    cur = current(server)
    if not cur or not cur.get("web"):
        raise MapError("This server has no map files the panel can delete.")
    from core import files, mapdraw
    if cur["drawn"]:
        if mapdraw.status(server).get("state") == "drawing":
            raise MapError("The map is being drawn right now; try again when it has finished.")
        targets = [DRAWN_DIR + "/web", DRAWN_DIR + "/data", DRAWN_DIR + "/state.json"]
    else:
        targets = [cur["web"] + "/maps"] if cur["id"] == "bluemap" else [cur["web"] + "/tiles"]
    gone = 0
    for t in targets:
        try:
            files.delete(server, t, actor=actor)
            gone += 1
        except files.FileError as e:
            if e.status != 404:
                raise MapError(str(e))
    manager.write_log(f"[MAP] {actor} deleted the map files of {server}.")
    return {"deleted": gone}


def _site_changed():
    """A map was added or removed: the join site starts (or stops) listening for maps."""
    try:
        from core import joinsite
        from core import settings as panel_settings
        wanted(fresh=True)
        s = panel_settings.load()
        if joinsite.status(s)["managed"]:
            joinsite.apply(s)
    except Exception as e:
        manager.write_log(f"[MAP] The join site couldn't follow the map change: {e}")


# ---------------------------------------------------------------------------
# Tickets and the map's own cookie
# ---------------------------------------------------------------------------
def make_ticket(member, server, user):
    """A one-minute ticket the Map tab hands to the join site, for this server's map only."""
    from core import auth
    return auth._sign({"m": member, "s": server, "u": user["username"], "src": user.get("source"),
                       "role": user.get("role"), "exp": int(time.time()) + TICKET_SECONDS}, "map ticket")


def read_ticket(value, member, server):
    from core import auth
    p = auth._unsign(value, "map ticket")
    if not p or p.get("m") != member or p.get("s") != server:
        return None
    return p


def make_viewer(ticket):
    from core import auth
    return auth._sign({k: ticket.get(k) for k in ("m", "s", "u", "src", "role")} |
                      {"exp": int(time.time()) + VIEWER_SECONDS}, "map viewer")


def read_viewer(value, member, server):
    """The panel account behind a map cookie, checked again now, or None."""
    from core import auth, grants
    p = auth._unsign(value, "map viewer") if value else None
    if not p or p.get("m") != member or p.get("s") != server:
        return None
    src, name = p.get("src"), str(p.get("u") or "")
    role = p.get("role")
    if src in ("panel", "linux"):
        role = auth.account_role(src, name)
        if not role:
            return None
    if role != "admin" and src == "panel":
        key = grants.server_key(server, None if member == "local" else member)
        if "view" not in grants.perms_for({"source": "panel", "username": name, "role": role}, key):
            return None
    return {"username": name, "source": src, "role": role}


# ---------------------------------------------------------------------------
# Serving a map (core/mapsite.py calls these)
# ---------------------------------------------------------------------------
TYPES = {".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".mjs": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".json": "application/json; charset=utf-8", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
         ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml", ".ico": "image/x-icon",
         ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf", ".txt": "text/plain; charset=utf-8",
         ".prbm": "application/octet-stream", ".bin": "application/octet-stream", ".map": "application/json"}


class Reply:
    """An answer for the join site: status, headers and either body bytes or chunks to stream."""

    def __init__(self, status, headers=None, body=b"", chunks=None, close=None):
        self.status, self.headers, self.body, self.chunks, self._close = status, dict(headers or {}), body, chunks, close

    def close(self):
        if self._close:
            try:
                self._close()
            except Exception:
                pass
            self._close = None


def _type_of(name):
    return TYPES.get(os.path.splitext(name)[1].lower(), "application/octet-stream")


def _is_players(tool_id, sub):
    return any(re.match(p, sub) for p in TOOLS[tool_id]["players"])


def _no_players(data):
    """Live data with the player list emptied (join pages that don't show players)."""
    try:
        d = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return b'{"players":[]}'
    if isinstance(d, dict):
        if "players" in d:
            d["players"] = []
        if "currentcount" in d:
            d["currentcount"] = 0
        if isinstance(d.get("updates"), list):          # Dynmap: chat and player events go too
            d["updates"] = [u for u in d["updates"] if isinstance(u, dict) and u.get("type") == "tile"]
    elif isinstance(d, list):
        d = []
    return json.dumps(d, separators=(",", ":")).encode()


def _fd_chunks(fd, size):
    left = size
    while left > 0:
        data = os.read(fd, min(CHUNK, left))
        if not data:
            break
        left -= len(data)
        yield data


def _static(server, web, sub, method, headers, tool_id, players):
    from core import files
    rel = sub if sub and not sub.endswith("/") else sub + "index.html"
    for path, encoding in ((rel, None), (rel + ".gz", "gzip")):
        full = f"{web}/{path}"
        try:
            fd, size, _ = files.open_for_download(server, full)
        except files.FileError as e:
            if e.status == 404:
                continue
            return Reply(403, {"Content-Type": "text/plain; charset=utf-8"}, b"This file can't be shown.\n")
        except Exception:
            return Reply(404, {"Content-Type": "text/plain; charset=utf-8"}, b"Not found\n")
        try:
            mtime = os.fstat(fd).st_mtime
        except OSError:
            mtime = time.time()
        h = {"Content-Type": _type_of(rel), "Last-Modified": email.utils.formatdate(mtime, usegmt=True),
             "Cache-Control": "no-cache"}
        if encoding:
            h["Content-Encoding"] = encoding
        if not players and _is_players(tool_id, rel):
            data = b"".join(_fd_chunks(fd, min(size, JSON_MAX)))
            os.close(fd)
            if encoding:
                import gzip
                try:
                    data = gzip.decompress(data)
                except OSError:
                    data = b"{}"
                h.pop("Content-Encoding", None)
            return Reply(200, h, _no_players(data))
        since = headers.get("if-modified-since")
        if since:
            try:
                if int(mtime) <= int(email.utils.parsedate_to_datetime(since).timestamp()):
                    os.close(fd)
                    return Reply(304, h)
            except (TypeError, ValueError, OverflowError):
                pass
        h["Content-Length"] = str(size)
        if method == "HEAD":
            os.close(fd)
            return Reply(200, h)
        return Reply(200, h, chunks=_fd_chunks(fd, size), close=lambda: os.close(fd))
    if "/tiles/" in "/" + rel or rel.startswith("tiles/"):
        return Reply(204, {"Cache-Control": "no-cache"})     # a part of the world not drawn yet
    if tool_id == "bluemap" and re.match(r"^maps/[^/]+/live/(players|markers)\.json$", rel):
        return Reply(200, {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-cache"},
                     b'{"players":[]}' if rel.endswith("players.json") else b"{}")
    return Reply(404, {"Content-Type": "text/plain; charset=utf-8"}, b"Not found\n")


PASS_REQUEST = ("accept", "accept-encoding", "if-none-match", "if-modified-since", "accept-language")
PASS_RESPONSE = ("content-type", "content-encoding", "content-length", "etag", "last-modified", "cache-control",
                 "expires")


def _proxy(port, sub, query, method, headers, tool_id, players):
    """The tool's own web server on 127.0.0.1:port. None when it doesn't answer."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=PROXY_TIMEOUT)
    target = "/" + quote(sub, safe="/-_.~,;:@!$&'()*+=") + (("?" + query) if query else "")
    try:
        conn.request(method, target, headers={k: v for k, v in headers.items() if k in PASS_REQUEST})
        r = conn.getresponse()
    except (OSError, http.client.HTTPException):
        conn.close()
        return None
    h = {k: v for k, v in ((k.lower(), v) for k, v in r.getheaders()) if k in PASS_RESPONSE}
    h = {k.title(): v for k, v in h.items()}
    if not players and _is_players(tool_id, sub):
        data = r.read(JSON_MAX)
        conn.close()
        if (h.get("Content-Encoding") or "").lower() == "gzip":
            import gzip
            try:
                data = gzip.decompress(data)
            except OSError:
                data = b"{}"
        h.pop("Content-Encoding", None)
        h.pop("Content-Length", None)
        return Reply(r.status, h, _no_players(data))

    def chunks():
        while True:
            data = r.read(CHUNK)
            if not data:
                break
            yield data
    if method == "HEAD":
        conn.close()
        return Reply(r.status, h)
    return Reply(r.status, h, chunks=chunks(), close=conn.close)


def serve_local(server, sub, method="GET", headers=None, query="", players=True):
    """One request for this panel's server's map. sub: the path inside the map, no leading /."""
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    if method not in ("GET", "HEAD"):
        return Reply(405, {"Allow": "GET, HEAD"}, b"Maps can only be looked at.\n")
    try:
        manager._check_server(server)
    except Exception:
        return Reply(404, {"Content-Type": "text/plain; charset=utf-8"}, b"Not found\n")
    cur = current(server)
    if not cur:
        return Reply(404, {"Content-Type": "text/html; charset=utf-8"}, _page(
            "No map yet", "This server has no map yet. An admin can add one in the server's Map tab."))
    if not cur["drawn"] and cur.get("port") and _running(server):
        got = _proxy(cur["port"], sub, query, method, headers, cur["id"], players)
        if got is not None:
            return got
    if cur.get("web"):
        return _static(server, cur["web"], sub, method, headers, cur["id"], players)
    return Reply(503, {"Content-Type": "text/html; charset=utf-8"}, _page(
        "The map is resting", f"{cur['name']} shows its map only while the server is running. "
        "It will be back when the server starts."))


def _page(title, text):
    import html
    return (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><body style='font-family:system-ui,sans-serif;background:#111827;"
            f"color:#e5e7eb;display:flex;align-items:center;justify-content:center;min-height:90vh;margin:0'>"
            f"<div style='max-width:28rem;padding:1rem;text-align:center'><h1 style='font-size:1.25rem'>"
            f"{html.escape(title)}</h1><p>{html.escape(text)}</p></div>").encode()


def page(title, text):
    return _page(title, text)
