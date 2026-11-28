"""Maps the panel draws itself (1.30), for servers that can't run a map plugin: vanilla Java and
Bedrock.

BlueMap's standalone renderer (BlueMap CLI, MIT) draws a Java world into files the join site
serves (core/worldmap.py, core/mapsite.py), so no map web server runs at all. A Bedrock world is
first copied the way a backup copies it (save hold / save query / save resume) and the copy is
converted to a Java world with Chunker (Hive Games' converter, MIT), which BlueMap then draws; the
copies are deleted afterwards. The real world is only ever read.

How a drawing runs:
- `redstonepanel map-draw NAME` does one drawing. The schedule is a timer,
  redstonepanel-map@NAME.timer (systemd on Linux, Task Scheduler on Windows, through
  core.platform), so it runs while the panel is stopped; **Draw now** starts the same command in
  a process of its own. After each scheduled backup is the other choice (core/backups.py).
- The tools are pinned releases, downloaded from GitHub over HTTPS and checked against the SHA-256
  below, into MAPTOOLS_DIR (root-owned, readable by the game account).
- They run as the game account (Linux) or an account of their own (Windows), at the lowest
  priority, with the server's Java when it is new enough for BlueMap (25), else the newest Java
  that is.
- Bedrock: a copy of the world (made like a backup's) is converted by Chunker, which writes no
  light data, so those maps are drawn evenly lit (BEDROCK_MAP_EXTRA).
- A world nobody has walked around in has no finished chunks, and BlueMap draws nothing from it
  without complaint; the status says so (hires_tiles).
- A drawing is skipped when the world hasn't changed since the last one, and waits for a backup,
  restore, move or type change of the same server (the backup lock).
- Everything is in DRAWN_DIR in the server folder (left out of backups, rebuilt if lost); the
  status and output of the last drawing are in DATA_DIR/map-draw/.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request

from core import manager, worldmap
from core.platform import paths, services, system
from core.settings import DATA_DIR

STATE_DIR = os.path.join(DATA_DIR, "map-draw")
TOOLS_DIR = paths.MAPTOOLS_DIR
UNIT_DIR = paths.UNIT_DIR
SERVICE_TEMPLATE = "redstonepanel-map@.service"
DRAWN = worldmap.DRAWN_DIR
HOSTS = {"github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"}
DRAW_TIMEOUT = 12 * 3600
CONVERT_TIMEOUT = 6 * 3600
SETUP_TIMEOUT = 600
TAIL = 40

# Pinned releases, checked on Superman on 28 Nov 2026 (SHA-256 = GitHub's own digest). BlueMap 5.29
# reads Minecraft 26.x worlds (world/dimensions/minecraft/...) and older ones, and needs Java 25;
# Chunker needs Java 17 and writes the newest Java world layout, which BlueMap reads.
BLUEMAP = {"version": "5.29", "file": "bluemap-5.29-cli.jar", "java": 25,
           "url": "https://github.com/BlueMap-Minecraft/BlueMap/releases/download/v5.29/bluemap-5.29-cli.jar",
           "sha256": "e830767fa6364b52b9310fb598508ebde7eec5ebe8360a989e240d991af89a3a"}
CHUNKER = {"version": "1.21.0", "file": "chunker-cli-1.21.0.jar", "java": 17,
           "url": "https://github.com/HiveGamesOSS/Chunker/releases/download/1.21.0/chunker-cli-1.21.0.jar",
           "sha256": "63148e587f5ee5a2ca7ebae1079b235c82ae4d1eff8f7a8a8a3398332513dda6"}
CHUNKER_FORMAT = "JAVA_26_3"            # the Java world format Chunker writes (BlueMap reads it)
# A converted Bedrock world has no light data: BlueMap would skip it (or draw it black), and its
# cave removal (which goes by sky light) would cut holes. So: drawn evenly lit, caves kept.
BEDROCK_MAP_EXTRA = ("ignore-missing-light-data: true\n"
                     "ambient-light: 1.0\n"
                     "remove-caves-below-y: -10000\n")
DIMENSIONS = {"overworld": ("Overworld", "minecraft:overworld", 0),
              "nether": ("Nether", "minecraft:the_nether", 1),
              "end": ("The End", "minecraft:the_end", 2)}


class DrawError(ValueError):
    """A drawing that can't be done (message is safe to show)."""


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
def _state_path(server):
    return os.path.join(STATE_DIR, f"{server}.json")


def log_path(server):
    return os.path.join(STATE_DIR, f"{server}.log")


def status(server):
    try:
        with open(_state_path(server)) as f:
            d = json.load(f)
        d = d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        d = {}
    d.setdefault("state", "never")
    if d["state"] == "drawing" and d.get("pid") and not _alive(d["pid"]):
        d["state"], d["error"] = "failed", "The drawing stopped before it finished."
    d["next"] = next_run(server)
    return d


def _alive(pid):
    try:
        return system.pid_alive(int(pid)) if hasattr(system, "pid_alive") else _posix_alive(int(pid))
    except Exception:
        return True


def _posix_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _write_state(server, **fields):
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    try:
        with open(_state_path(server)) as f:
            d = json.load(f)
        d = d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        d = {}
    d.update(fields)
    tmp = _state_path(server) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f)
    os.replace(tmp, _state_path(server))
    return d


def _tail(server, n=TAIL):
    try:
        text, _ = system.tail(log_path(server), n)
        return text
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# The schedule
# ---------------------------------------------------------------------------
def _timer_name(server):
    return f"redstonepanel-map@{server}.timer"


def _service_unit():
    return f"""[Unit]
Description=RedstonePanel world map of %i
After=network.target

[Service]
Type=oneshot
ExecStart={paths.PANEL_CMD} map-draw %i
TimeoutStartSec={DRAW_TIMEOUT + CONVERT_TIMEOUT + 1800}
Nice=19
IOSchedulingClass=idle
"""


def _timer_unit(server, at):
    return f"""[Unit]
Description=RedstonePanel world map drawing of {server}

[Timer]
OnCalendar=*-*-* {at}:00
RandomizedDelaySec=300
Persistent=false

[Install]
WantedBy=timers.target
"""


def apply_schedule(server):
    """The timer follows the Map tab: nightly at the chosen time, else none."""
    from core import backups
    s = worldmap.settings(server)
    timer = _timer_name(server)
    path = os.path.join(UNIT_DIR, timer)
    if s["drawn"] and s["schedule"] == "nightly":
        backups._write_if_changed(os.path.join(UNIT_DIR, SERVICE_TEMPLATE), _service_unit())
        backups._write_if_changed(path, _timer_unit(server, s["time"]))
        services.reload()
        services.enable_schedule(timer)
    elif os.path.exists(path):
        services.disable_schedule(timer)
        os.remove(path)
        services.reload()


def next_run(server):
    if not os.path.exists(os.path.join(UNIT_DIR, _timer_name(server))):
        return None
    try:
        return services.next_run_epoch(_timer_name(server))
    except Exception:
        return None


def on_server_removed(server, purged):
    if not backups_name_ok(server):
        return
    timer = _timer_name(server)
    if os.path.exists(os.path.join(UNIT_DIR, timer)):
        services.disable_schedule(timer)
        os.remove(os.path.join(UNIT_DIR, timer))
        services.reload()
    if purged:
        worldmap.forget(server)
        for p in (_state_path(server), log_path(server)):
            try:
                os.remove(p)
            except OSError:
                pass


def on_server_restored(server):
    if backups_name_ok(server) and worldmap.settings(server)["drawn"]:
        apply_schedule(server)


def backups_name_ok(server):
    from core import backups
    return bool(backups.NAME_RE.fullmatch(server or ""))


def after_backup(server):
    """A scheduled backup of the server finished: draw now if that's the chosen schedule."""
    s = worldmap.settings(server)
    if s["drawn"] and s["schedule"] == "backup":
        start(server, actor="the backup schedule")


def start(server, actor="?"):
    """Draw now: the same command as the timer, in a process of its own."""
    manager._check_server(server)
    s = worldmap.settings(server)
    if not s["drawn"]:
        raise DrawError("Turn on the map for this server first.")
    if status(server).get("state") == "drawing":
        raise DrawError("The map is being drawn right now.")
    r = services.run_detached(f"redstonepanel-map-{server}", [paths.PANEL_CMD, "map-draw", server, "--now"],
                              description=f"RedstonePanel world map of {server}", low_priority=True, no_block=True)
    if r.returncode != 0:
        raise DrawError(f"The drawing couldn't start: {(r.stderr or r.stdout or '').strip()[:300]}")
    manager.write_log(f"[MAP] {actor} started drawing the map of {server}.")
    return {"started": True}


# ---------------------------------------------------------------------------
# The tools
# ---------------------------------------------------------------------------
def _host(url):
    from urllib.parse import urlsplit
    return (urlsplit(url).hostname or "").lower()


class _Redirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _host(newurl) not in HOSTS or not newurl.startswith("https://"):
            raise urllib.error.URLError(f"refused a redirect to {_host(newurl)}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def tool_path(tool):
    return os.path.join(TOOLS_DIR, tool["file"])


def ensure_tool(tool, download=None):
    """The jar of a pinned tool, downloaded and checked the first time. Returns its path."""
    path = tool_path(tool)
    if os.path.isfile(path) and _sha256(path) == tool["sha256"]:
        return path
    if not tool["sha256"]:
        raise DrawError(f"No checksum is known for {tool['file']}; the panel won't download it.")
    os.makedirs(TOOLS_DIR, exist_ok=True)
    if not paths.WINDOWS:
        os.chmod(TOOLS_DIR, 0o755)
    tmp = path + ".part"
    try:
        if download:
            download(tool["url"], tmp)
        else:
            if _host(tool["url"]) not in HOSTS:
                raise DrawError(f"{tool['url']} isn't an allowed download.")
            opener = urllib.request.build_opener(_Redirects)
            req = urllib.request.Request(tool["url"], headers={"User-Agent": "RedstonePanel"})
            with opener.open(req, timeout=120) as r, open(tmp, "wb") as f:
                shutil.copyfileobj(r, f, 1 << 16)
    except (urllib.error.URLError, OSError) as e:
        _rm(tmp)
        raise DrawError(f"Downloading {tool['file']} from GitHub failed: {getattr(e, 'reason', e)}")
    if _sha256(tmp) != tool["sha256"]:
        _rm(tmp)
        raise DrawError(f"{tool['file']} from GitHub didn't match its checksum; nothing was changed.")
    if not paths.WINDOWS:
        os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    manager.write_log(f"[MAP] Downloaded {tool['file']} (SHA-256 checked).")
    return path


def _sha256(path):
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def _rm(path):
    try:
        os.remove(path)
    except OSError:
        pass


def choose_java(server, need=None):
    """The java for BlueMap (25 or newer) and Chunker: the server's own when it is new enough,
    else the newest installed one that is."""
    need = need or BLUEMAP["java"]
    from core import javaservers
    javas = javaservers.installed_javas()
    own = None
    try:
        if manager._is_java(server):
            own = manager.unit_java(manager._unit_exec_start(server))
    except Exception:
        own = None
    for major, path in javas.items():
        if own and os.path.realpath(path) == os.path.realpath(own) and major >= need:
            return path
    fits = sorted(m for m in javas if m >= need)
    if not fits:
        raise DrawError(f"Drawing a map needs Java {need} or newer on this machine. Install it under "
                        "Panel Settings, Java, then press Draw now.")
    return javas[fits[-1]]


def _heap():
    from core import host
    try:
        total = host.memory()["total"]
    except Exception:
        total = 0
    mb = max(512, min(4096, int(total / 4 / 1048576))) if total else 1024
    return f"-Xmx{mb}m"


def _threads():
    n = os.cpu_count() or 1
    return 1 if n <= 4 else max(1, n // 4)


# ---------------------------------------------------------------------------
# BlueMap's config folder
# ---------------------------------------------------------------------------
def _fwd(path):
    return path.replace("\\", "/")


def _hocon_set(text, key, value, quoted=True):
    new = worldmap.set_value(text, "hocon", (key,), value, quote_it=quoted)
    return new if new is not None else text


def _write_text(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _read_text(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def map_config(world_dir, world, converted=False):
    """One map's .conf for BlueMap: the world folder and its dimension (converted: a Bedrock world
    Chunker converted, which has no light data)."""
    name, dim, order = DIMENSIONS[world]
    return (f'# Written by RedstonePanel (1.30). The Map tab sets what is drawn.\n'
            f'world: "{_fwd(world_dir)}"\n'
            f'dimension: "{dim}"\n'
            f'name: "{name}"\n'
            f'sorting: {order}\n'
            f'storage: "file"\n' + (BEDROCK_MAP_EXTRA if converted else ""))


def prepare_config(server, d, world_dirs, java, jar, worlds, run=None, converted=False):
    """Makes DRAWN/config: BlueMap's own defaults (it writes them on its first run), with the
    panel's choices on top: textures agreed to, files in DRAWN, no web server, one map per world."""
    base = os.path.join(d, DRAWN)
    conf = os.path.join(base, "config")
    for sub in ("", "config", "data", "web"):
        os.makedirs(os.path.join(base, sub), exist_ok=True)
    if not os.path.isfile(os.path.join(conf, "core.conf")):
        r = (run or _run)(server, [java, _heap(), "-jar", jar, "-c", conf], cwd=base, writable=(base,),
                          readable=(TOOLS_DIR, os.path.dirname(os.path.dirname(java))), timeout=SETUP_TIMEOUT)
        if not os.path.isfile(os.path.join(conf, "core.conf")):
            raise DrawError("BlueMap didn't write its settings on its first run; see the drawing's output.")
    core = _read_text(os.path.join(conf, "core.conf")) or ""
    core = _hocon_set(core, "accept-download", "true", quoted=False)
    core = _hocon_set(core, "data", _fwd(os.path.join(base, "data")))
    core = _hocon_set(core, "render-thread-count", str(_threads()), quoted=False)
    core = _hocon_set(core, "metrics", "false", quoted=False)
    _write_text(os.path.join(conf, "core.conf"), core)
    for name in ("webapp.conf", "webserver.conf"):
        p = os.path.join(conf, name)
        text = _read_text(p)
        if text is None:
            continue
        text = _hocon_set(text, "webroot", _fwd(os.path.join(base, "web")))
        if name == "webserver.conf":
            text = _hocon_set(text, "enabled", "false", quoted=False)
        _write_text(p, text)
    storages = os.path.join(conf, "storages")
    p = os.path.join(storages, "file.conf")
    text = _read_text(p)
    if text is not None:
        _write_text(p, _hocon_set(text, "root", _fwd(os.path.join(base, "web", "maps"))))
    maps = os.path.join(conf, "maps")
    os.makedirs(maps, exist_ok=True)
    for f in os.listdir(maps):
        if f.endswith(".conf"):
            os.replace(os.path.join(maps, f), os.path.join(maps, f + ".off"))
    for w in worlds:
        wd = world_dirs.get(w)
        if wd:
            _write_text(os.path.join(maps, f"{w}.conf"), map_config(wd, w, converted))
    return conf


def _run(server, argv, cwd, writable=(), readable=(), timeout=DRAW_TIMEOUT):
    with open(log_path(server), "a", encoding="utf-8") as f:
        f.write(f"\n$ {' '.join(os.path.basename(a) if i == 0 else a for i, a in enumerate(argv))}\n")
    return system.run_map_tool(server, argv, cwd, log_path(server), readable=readable, writable=writable,
                               timeout=timeout)


# ---------------------------------------------------------------------------
# Worlds
# ---------------------------------------------------------------------------
def java_worlds(d):
    """{"overworld", "nether", "end": folder} of a vanilla world (DIM-1 and DIM1 inside it)."""
    level = (manager.read_properties(d).get("level-name") or "world").strip() or "world"
    if any(x in ("", ".", "..") for x in level.replace("\\", "/").split("/")):
        level = "world"
    w = os.path.join(d, level)
    return {"overworld": w, "nether": w, "end": w} if os.path.isdir(w) else {}


def bedrock_world(d):
    level = (manager.read_properties(d).get("level-name") or "Bedrock level").strip()
    w = os.path.join(d, "worlds", level)
    return w if os.path.isdir(w) and "/" not in level and "\\" not in level and level not in (".", "..") else None


def world_stamp(folder):
    """The newest change in a world's region (Java) or database (Bedrock) files."""
    newest = 0.0
    for root, dirs, files in os.walk(folder):
        dirs[:] = [x for x in dirs if not os.path.islink(os.path.join(root, x))]
        for n in files:
            if n.endswith((".mca", ".mcr", ".ldb", ".log")) or n.startswith("MANIFEST") or n == "level.dat":
                try:
                    newest = max(newest, os.lstat(os.path.join(root, n)).st_mtime)
                except OSError:
                    pass
    return newest


def _folder_size(folder):
    total = 0
    for root, dirs, files in os.walk(folder):
        dirs[:] = [x for x in dirs if not os.path.islink(os.path.join(root, x))]
        for n in files:
            try:
                total += os.lstat(os.path.join(root, n)).st_size
            except OSError:
                pass
    return total


def _save_java(server, d):
    """A running Java server writes everything to disk first (RCON), so the drawing is current."""
    from core import backups, rcon
    if not worldmap._running(server):
        return
    send = backups._java_rcon(d)
    if not send:
        return
    try:
        send("save-all flush", timeout=backups.JAVA_SAVE_TIMEOUT)
    except rcon.RconError:
        pass


def copy_bedrock_world(server, d, src, dest):
    """A consistent copy of a Bedrock world: from Bedrock's own held file list while it runs."""
    from core import backups
    if os.path.exists(dest):
        shutil.rmtree(dest, ignore_errors=True)
    files = None
    running = worldmap._running(server)
    if running:
        files = backups._bedrock_hold(server, d)
    try:
        world_name = os.path.basename(src)
        if files:
            for rel, size in files:
                parts = rel.replace("\\", "/").split("/")
                if parts[0] != world_name or ".." in parts:
                    continue
                target = os.path.join(dest, *parts[1:])
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with system.game_files(), open(os.path.join(d, "worlds", *parts), "rb") as f, open(target, "wb") as out:
                    left = size
                    while left > 0:
                        chunk = f.read(min(1 << 20, left))
                        if not chunk:
                            break
                        out.write(chunk)
                        left -= len(chunk)
        else:
            with system.game_files():
                shutil.copytree(src, dest, symlinks=True,
                                ignore=lambda folder, names: [n for n in names if os.path.islink(os.path.join(folder, n))])
    finally:
        if running:
            backups._screen(server, "save resume")


# ---------------------------------------------------------------------------
# One drawing
# ---------------------------------------------------------------------------
def run(server, now=False, run_tool=None):
    """Draws the server's map (the timer's `redstonepanel map-draw NAME`). now: Draw now, which
    draws even when the world hasn't changed. Returns the status."""
    manager._check_server(server)
    s = worldmap.settings(server)
    if not s["drawn"]:
        raise DrawError(f"The map of {server} is off.")
    d = manager.get_server_dir(server)
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    lock_fd = os.open(os.path.join(STATE_DIR, f".{server}.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        system.lock(lock_fd)
    except BlockingIOError:
        os.close(lock_fd)
        raise DrawError("The map is being drawn right now.")
    started = time.time()
    try:
        with open(log_path(server), "w", encoding="utf-8") as f:
            f.write(f"RedstonePanel: drawing the map of {server}, {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        _write_state(server, state="drawing", started=int(started), pid=os.getpid(), error=None)
        result = _draw(server, d, s, now, run_tool or _run)
    except Exception as e:
        msg = str(e) if isinstance(e, DrawError) else f"{type(e).__name__}: {e}"
        manager.write_log(f"[MAP] Drawing the map of {server} failed: {msg}")
        st = _write_state(server, state="failed", finished=int(time.time()), error=msg, tail=_tail(server))
        try:
            from core import notify
            notify.send("map_failed", server, f"Drawing the map of {server} failed: {msg}")
        except Exception:
            pass
        return st
    finally:
        os.close(lock_fd)
    if result.get("skipped"):
        manager.write_log(f"[MAP] {server}'s world hasn't changed since the last drawing; skipped.")
        return _write_state(server, state="done", skipped_at=int(time.time()), pid=None)
    secs = int(time.time() - started)
    manager.write_log(f"[MAP] {server} drawn in {secs // 60} min {secs % 60} s.")
    return _write_state(server, state="done", finished=int(time.time()), seconds=secs, stamp=result["stamp"],
                        pid=None, error=None, tail=_tail(server))


def _wait_for_backup(server, timeout=3600):
    """Waits while a backup, restore, move or type change holds the server's backup lock."""
    from core import backups
    deadline = time.time() + timeout
    while True:
        try:
            fd = backups._lock(server)
        except backups.BackupError:
            if time.time() > deadline:
                raise DrawError("A backup of this server has been running for an hour; the drawing waits for next time.")
            time.sleep(10)
            continue
        os.close(fd)
        return


def _draw(server, d, s, now, run_tool):
    kind, _ = worldmap._kind(server)
    _wait_for_backup(server)
    java = choose_java(server)
    jar = ensure_tool(BLUEMAP)
    base = os.path.join(d, DRAWN)
    readable = [d, TOOLS_DIR, os.path.dirname(os.path.dirname(java))]
    last = status(server).get("stamp")
    if kind == "bedrock":
        src = bedrock_world(d)
        if not src:
            raise DrawError("This Bedrock server has no world yet; start it once first.")
        stamp = world_stamp(src)
        if not now and last and stamp <= last:
            return {"skipped": True}
        need = _folder_size(src) * 3 + 512 * 1048576
        if shutil.disk_usage(d).free < need:
            raise DrawError(f"Not enough free disk space to convert the world for its map (needs about "
                            f"{need // 1048576} MB).")
        chunker = ensure_tool(CHUNKER)
        copy = os.path.join(base, "bedrock-copy")
        converted = os.path.join(base, "converted")
        os.makedirs(base, exist_ok=True)
        try:
            copy_bedrock_world(server, d, src, copy)
            if not paths.WINDOWS:
                system.take_for_game_account(base)
            shutil.rmtree(converted, ignore_errors=True)
            r = run_tool(server, [java, _heap(), "-jar", chunker, "-i", copy, "-f", CHUNKER_FORMAT, "-o", converted],
                         cwd=base, writable=(base,), readable=readable, timeout=CONVERT_TIMEOUT)
            if r.returncode != 0 or not os.path.isfile(os.path.join(converted, "level.dat")):
                raise DrawError("Chunker couldn't convert the world; see the drawing's output.")
            worlds = {"overworld": converted, "nether": converted, "end": converted}
            conf = prepare_config(server, d, worlds, java, jar, s["worlds"], run=run_tool, converted=True)
            r = run_tool(server, [java, _heap(), "-jar", jar, "-c", conf, "-r", "-g"],
                         cwd=base, writable=(base,), readable=readable, timeout=DRAW_TIMEOUT)
        finally:
            shutil.rmtree(copy, ignore_errors=True)
            shutil.rmtree(converted, ignore_errors=True)
    else:
        worlds = java_worlds(d)
        if not worlds:
            raise DrawError("This server has no world yet; start it once first.")
        stamp = world_stamp(worlds["overworld"])
        if not now and last and stamp <= last:
            return {"skipped": True}
        _save_java(server, d)
        os.makedirs(base, exist_ok=True)
        if not paths.WINDOWS:
            system.take_for_game_account(base)
        conf = prepare_config(server, d, worlds, java, jar, s["worlds"], run=run_tool)
        r = run_tool(server, [java, _heap(), "-jar", jar, "-c", conf, "-r", "-g"],
                     cwd=base, writable=(base,), readable=readable, timeout=DRAW_TIMEOUT)
    if r.returncode != 0:
        raise DrawError("BlueMap stopped with an error; see the drawing's output.")
    if not os.path.isfile(os.path.join(base, "web", "index.html")):
        raise DrawError("BlueMap finished but wrote no map; see the drawing's output.")
    return {"stamp": stamp, "tiles": hires_tiles(base)}


def hires_tiles(base, limit=1000):
    """How many detailed tiles the drawing has (up to limit). BlueMap finishes happily with none
    when the world has no fully made chunks yet, which is a world nobody has walked around in."""
    n = 0
    maps = os.path.join(base, "web", "maps")
    try:
        names = os.listdir(maps)
    except OSError:
        return 0
    for m in names:
        for root, dirs, files in os.walk(os.path.join(maps, m, "tiles", "0")):
            n += sum(1 for f in files if f.startswith("z"))
            if n >= limit:
                return n
    return n


def describe(st):
    """One line for the Map tab and the CLI."""
    if st.get("state") == "drawing":
        return "Drawing now"
    if st.get("state") == "failed":
        return f"The last drawing failed: {st.get('error')}"
    if st.get("finished"):
        when = "Drawn " + time.strftime("%Y-%m-%d %H:%M", time.localtime(st["finished"]))
        if st.get("tiles") == 0:
            return when + ", but there's nothing to show yet: the map shows the places players have explored"
        return when
    return "Not drawn yet"


