"""Scheduled and manual backups of game servers.

Archives live outside the server folders, so uninstalling or purging a server never deletes
them:
    /var/backups/redstonepanel/<server>/<server>-YYYYmmdd-HHMMSS-<kind>.tar.gz
kind is "scheduled", "manual", "pre-restore", "pre-change" (before a mod update),
"pre-update" (before an update; the newest three are kept) or "pre-convert" (before a
change of server type, core/convert.py). Only
scheduled backups are pruned to the configured count; the others stay until someone deletes
them.

Schedules are kept in DATA_DIR/backups.json (written only by the panel) and turned into one
systemd timer per server, redstonepanel-backup@<server>.timer. The timer runs
`redstonepanel backup <server>` as root, so backups happen even while the panel is stopped.
Each run writes its result to DATA_DIR/backup-status/<server>.json.

Consistency: a running Java server is paused with save-off / save-all flush over RCON while
its files are archived; a running Bedrock server is paused with save hold / save query
through its screen session, and only the bytes it reports as ready are copied. Without
either (no RCON, screen not answering) the files are copied live and the backup is marked
as such.
"""
import datetime
import fcntl
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time

from core import manager, notify, properties, rcon
from core.settings import DATA_DIR

BACKUP_ROOT = os.environ.get("RP_BACKUP_DIR") or "/var/backups/redstonepanel"
SCHEDULE_FILE = os.path.join(DATA_DIR, "backups.json")
STATUS_DIR = os.path.join(DATA_DIR, "backup-status")
UNIT_DIR = "/etc/systemd/system"
SERVICE_TEMPLATE = "redstonepanel-backup@.service"

# A server name is also a systemd instance name here, so no '@'
NAME_RE = re.compile(r"^minecraft[A-Za-z0-9_.-]*$")
KINDS = ("scheduled", "manual", "pre-restore", "pre-change", "pre-update", "pre-convert")
QUIET_KINDS = ("pre-restore", "pre-change", "pre-update", "pre-convert")     # no webhook of their own
FILE_RE = re.compile(r"^(?P<server>minecraft[A-Za-z0-9_.-]*)-(?P<stamp>\d{8}-\d{6})-"
                     r"(?P<kind>scheduled|manual|pre-restore|pre-change|pre-update|pre-convert)\.tar\.gz$")

# id -> (label, OnCalendar with {h} {m} {h6} {h12} filled in from the chosen time)
FREQUENCIES = {
    "hourly": ("Every hour", "*-*-* *:{m}:00"),
    "6h": ("Every 6 hours", "*-*-* {h6}/6:{m}:00"),
    "12h": ("Every 12 hours", "*-*-* {h12}/12:{m}:00"),
    "daily": ("Every day", "*-*-* {h}:{m}:00"),
    "weekly": ("Every Sunday", "Sun *-*-* {h}:{m}:00"),
}
DEFAULT_SCHEDULE = {"enabled": False, "frequency": "daily", "time": "04:00", "keep": 7, "keep_days": 0}
TIME_RE = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])$")
MAX_KEEP = 365
MAX_KEEP_DAYS = 3650

# Not worth backing up: downloaded again by Update, regenerated, or logs.
SKIP_TOP = {"server.jar", "server.jar.previous", "bedrock_server", "bedrock_server_symbols.debug", "bedrock-server.zip",
            "libraries", "versions", "logs", "crash-reports", "debug", "server.log",
            manager.REMOVED_INFO}
SKIP_TOP_PREFIXES = (".redstonepanel-",)
# What a Quilt or (older) Forge install leaves next to libraries/: installed again with it (1.20)
LOADER_JAR_RE = re.compile(r"^(quilt-server-launch\.jar|minecraft_server\.[0-9.]+\.jar|"
                           r"forge-[0-9.]+-[0-9.]+(-universal|-shim)?\.jar)$")
# ...except the Java server's loader and version (1.16.11): a server made from an off-site copy on
# a fresh machine downloads the same Minecraft version and loader again.
KEEP_TOP = {".redstonepanel-server.json"}
# Bedrock's own packs come with the server zip (custom packs sit next to them and are kept)
SKIP_PACK_PREFIXES = ("behavior_packs/vanilla", "resource_packs/vanilla", "behavior_packs/chemistry",
                      "resource_packs/chemistry", "behavior_packs/experimental", "resource_packs/experimental",
                      "definitions")

# 1.19: the first member of every archive says what wrote the world in it (never restored)
MANIFEST = ".redstonepanel-backup.json"

BEDROCK_READY = "Data saved. Files are now ready to be copied."
LOG_PREFIX_RE = re.compile(r"^\s*\[[^\]]*\]\s*")
JAVA_SAVE_TIMEOUT = 300


class BackupError(RuntimeError):
    """Something a user can act on (message is safe to show)."""


# ---------------------------------------------------------------------------
# Paths and listing
# ---------------------------------------------------------------------------
def _check_name(server):
    if not NAME_RE.fullmatch(server or ""):
        raise BackupError(f"'{server}' isn't a server name the panel can back up.")

def server_backup_dir(server):
    _check_name(server)
    return os.path.join(BACKUP_ROOT, server)

def backup_path(server, filename):
    """The archive's full path, only for one of this server's own backup file names."""
    m = FILE_RE.fullmatch(filename or "")
    if not m or m.group("server") != server:
        raise BackupError("No such backup.")
    path = os.path.join(server_backup_dir(server), filename)
    if not os.path.isfile(path):
        raise BackupError("No such backup.")
    return path

def list_backups(server):
    d = server_backup_dir(server)
    out = []
    if os.path.isdir(d):
        for f in os.listdir(d):
            m = FILE_RE.fullmatch(f)
            if not m or m.group("server") != server:
                continue
            st = os.stat(os.path.join(d, f))
            created = datetime.datetime.strptime(m.group("stamp"), "%Y%m%d-%H%M%S").replace(
                tzinfo=datetime.timezone.utc).timestamp()
            out.append({"file": f, "kind": m.group("kind"), "size": st.st_size, "created": int(created),
                        "made_by": made_by(os.path.join(d, f), st)})
    out.sort(key=lambda b: (b["created"], b["file"]), reverse=True)
    return out


# ---------------------------------------------------------------------------
# What made a backup (1.19)
# ---------------------------------------------------------------------------
_made_cache = {}               # path -> (mtime_ns, size, manifest or None)
_OLD_SCAN = 400                # members read looking for an older backup's server info

def _manifest(server, d, edition, kind):
    """What runs the world being archived: {edition, loader, minecraft, build, panel, kind}."""
    from core.version import panel_version
    out = {"edition": edition, "kind": kind, "panel": panel_version()}
    if edition == "java":
        from core import javaservers
        info = javaservers.read_info(d) or {}
        out.update({k: info.get(k) for k in ("loader", "minecraft", "build") if info.get(k)})
    else:
        try:
            from core import updates
            version = updates.bedrock_installed(d)
        except Exception:
            version = None
        if version:
            out["minecraft"] = version
    return out

def _add_manifest(tar, manifest):
    data = json.dumps(manifest, sort_keys=True).encode()
    ti = tarfile.TarInfo(MANIFEST)
    ti.size, ti.mode, ti.mtime = len(data), 0o600, int(time.time())
    tar.addfile(ti, io.BytesIO(data))

def _clean_manifest(data):
    if not isinstance(data, dict):
        return None
    out = {k: str(data[k])[:60] for k in ("edition", "loader", "minecraft", "build", "panel") if data.get(k)}
    return out if out.get("minecraft") else None

def read_manifest(path):
    """The archive's manifest, or for a Java backup from before 1.19 what its server info file
    says. None when the archive doesn't say."""
    try:
        with tarfile.open(path, "r:gz") as tar:
            for i, m in enumerate(tar):
                if i >= _OLD_SCAN or (i and "/" in m.name and m.isfile()):
                    break                    # the top folder's own files come before any folder's
                if m.name in (MANIFEST, ".redstonepanel-server.json") and m.isfile() and m.size < 65536:
                    data = json.loads(tar.extractfile(m).read().decode("utf-8", "replace"))
                    if m.name != MANIFEST and isinstance(data, dict):
                        data = dict(data, edition="java")
                    return _clean_manifest(data)
    except (OSError, ValueError, tarfile.TarError, EOFError):
        pass
    return None

def made_by(path, st=None):
    """read_manifest() with a label ("Paper 1.21.4"), remembered until the file changes."""
    try:
        st = st or os.stat(path)
    except OSError:
        return None
    hit = _made_cache.get(path)
    if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
        return hit[2]
    m = read_manifest(path)
    if m:
        loader = {"paper": "Paper", "fabric": "Fabric", "vanilla": "Vanilla"}.get(m.get("loader"))
        m["label"] = " ".join(x for x in (loader or ("Bedrock" if m.get("edition") == "bedrock" else None),
                                          m["minecraft"]) if x)
    if len(_made_cache) > 5000:
        _made_cache.clear()
    _made_cache[path] = (st.st_mtime_ns, st.st_size, m)
    return m

def check_not_newer(server, d, path):
    """Refuses a Java backup whose world was written by a newer Minecraft than the server runs:
    the older version would open it and damage it."""
    from core import javaservers
    m = made_by(path)
    current = (javaservers.read_info(d) or {}).get("minecraft")
    if not m or not current or m.get("edition") == "bedrock":
        return
    try:
        newer = javaservers._older(current, m["minecraft"])
    except Exception:
        return
    if newer:
        raise BackupError(f"This backup's world was last opened by Minecraft {m['minecraft']}, and {server} runs "
                          f"{current}. An older version can't open a newer world without damaging it. "
                          f"Update the server to {m['minecraft']} or newer first, then restore.")


# ---------------------------------------------------------------------------
# Taking a backup
# ---------------------------------------------------------------------------
def _is_active(server):
    return subprocess.run(["systemctl", "is-active", f"{server}.service"],
                          capture_output=True, text=True).stdout.strip() == "active"

def _edition(server, d):
    if os.path.isfile(os.path.join(d, "bedrock_server")):
        return "bedrock"
    from core import javaservers
    return "java" if manager._is_java(server) or javaservers.is_java_folder(d) else "bedrock"

def _skipped(rel, full=False):
    """rel: path inside the server folder, '/'-separated. full=True (moving a server to another
    cluster member) keeps everything but the removed-server note."""
    top = rel.split("/", 1)[0]
    if full:
        return rel == manager.REMOVED_INFO
    if rel in KEEP_TOP:
        return False
    if top in SKIP_TOP or top.startswith(SKIP_TOP_PREFIXES):
        return True
    if rel == top and LOADER_JAR_RE.match(top):
        return True
    return any(rel == p or rel.startswith(p) for p in SKIP_PACK_PREFIXES)

def _add_tree(tar, d, skip_dirs=(), full=False):
    """Adds the server folder to tar, minus _skipped() paths and skip_dirs (relative)."""
    for root, dirs, files in os.walk(d):
        rel_root = os.path.relpath(root, d).replace(os.sep, "/")
        rel_root = "" if rel_root == "." else rel_root
        keep_dirs = []
        for name in sorted(dirs):
            rel = f"{rel_root}/{name}" if rel_root else name
            path = os.path.join(root, name)
            if _skipped(rel, full) or rel in skip_dirs:
                continue
            if os.path.islink(path):
                tar.add(path, arcname=rel, recursive=False)   # the link itself, never its target
                continue
            tar.add(path, arcname=rel, recursive=False)
            keep_dirs.append(name)
        dirs[:] = keep_dirs
        for name in sorted(files):
            rel = f"{rel_root}/{name}" if rel_root else name
            if _skipped(rel, full):
                continue
            path = os.path.join(root, name)
            try:
                tar.add(path, arcname=rel, recursive=False)
            except FileNotFoundError:
                pass          # deleted by the running server while we walked

def _java_rcon(d):
    props = manager.read_properties(d)
    port = str(props.get("rcon.port", "")).strip()
    password = props.get("rcon.password", "")
    if str(props.get("enable-rcon", "false")).lower() != "true" or not password or not port.isdigit():
        return None
    return lambda cmd, timeout=10: rcon.command("127.0.0.1", int(port), password, cmd, timeout=timeout)

def _screen(server, cmd):
    screen = shutil.which("screen")
    if not screen:
        return False
    r = subprocess.run(["sudo", "-u", "minecraft", screen, "-S", server, "-p", "0", "-X", "stuff", cmd + "\r"],
                       capture_output=True, text=True)
    return r.returncode == 0

def parse_bedrock_file_list(text):
    """The file list Bedrock prints after BEDROCK_READY, as [(path under worlds/, bytes)].
    Returns None if the ready line or a usable list isn't there yet."""
    i = text.find(BEDROCK_READY)
    if i < 0:
        return None
    lines = [l for l in text[i + len(BEDROCK_READY):].splitlines() if l.strip()]
    if not lines:
        return None
    line = LOG_PREFIX_RE.sub("", lines[0]).strip()
    entries = re.findall(r"(.+?):(\d+)(?:,\s*|$)", line)
    files = []
    for path, size in entries:
        path = path.strip()
        parts = path.replace("\\", "/").split("/")
        if not path or path.startswith("/") or ".." in parts:
            return None
        files.append(("/".join(parts), int(size)))
    return files or None

def _bedrock_hold(server, d, timeout=60):
    """save hold, then save query until Bedrock lists the files to copy. None if it never does."""
    log = os.path.join(d, "server.log")
    if not _screen(server, "save hold"):
        return None
    deadline = time.time() + timeout
    while time.time() < deadline:
        offset = os.path.getsize(log) if os.path.exists(log) else 0
        if not _screen(server, "save query"):
            return None
        for _ in range(10):          # the screen log is flushed every second
            time.sleep(0.5)
            try:
                with open(log, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(offset)
                    files = parse_bedrock_file_list(f.read())
            except OSError:
                files = None
            if files:
                return files
    return None

def _add_held_files(tar, d, files):
    """Adds Bedrock's held world files, each cut to the length the server reported."""
    for rel, size in files:
        full = os.path.join(d, "worlds", rel)
        info = tarfile.TarInfo(f"worlds/{rel}")
        info.size = size
        info.mtime = int(time.time())
        info.mode = 0o644
        with open(full, "rb") as f:
            # tarfile copies exactly info.size bytes, so the part written after the hold is left out
            tar.addfile(info, f)

def _lock(server):
    d = server_backup_dir(server)
    os.makedirs(d, mode=0o700, exist_ok=True)
    os.chmod(BACKUP_ROOT, 0o700)
    fd = os.open(os.path.join(d, ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise BackupError(f"A backup of {server} is already running.")
    return fd

def _write_status(server, status):
    try:
        os.makedirs(STATUS_DIR, mode=0o700, exist_ok=True)
        tmp = os.path.join(STATUS_DIR, f".{server}.json.tmp")
        with open(tmp, "w") as f:
            json.dump(status, f)
        os.replace(tmp, os.path.join(STATUS_DIR, f"{server}.json"))
    except OSError:
        pass

def read_status(server):
    try:
        with open(os.path.join(STATUS_DIR, f"{server}.json")) as f:
            s = json.load(f)
        return s if isinstance(s, dict) else None
    except (OSError, ValueError):
        return None

def run_backup(server, kind="manual"):
    """Takes one backup now. Returns its list entry plus 'consistent' (False = copied live)."""
    if kind not in KINDS:
        raise BackupError(f"Unknown backup kind '{kind}'.")
    _check_name(server)
    if not os.path.isfile(os.path.join(UNIT_DIR, f"{server}.service")):
        raise BackupError(f"No Minecraft server called '{server}' on this panel.")
    started = time.time()
    try:
        info = _run_backup_locked(server, kind)
    except Exception as e:
        msg = str(e) if isinstance(e, BackupError) else f"{type(e).__name__}: {e}"
        manager.write_log(f"[BACKUP] {kind.capitalize()} backup of {server} failed: {msg}")
        _write_status(server, {"at": int(started), "ok": False, "kind": kind, "error": msg})
        if kind not in QUIET_KINDS:
            notify.send("backup_failed", server, f"{kind.capitalize()} backup of {server} failed: {msg}")
        raise
    _write_status(server, {"at": int(started), "ok": True, "kind": kind, "file": info["file"],
                           "size": info["size"], "consistent": info["consistent"],
                           "seconds": round(time.time() - started, 1)})
    if kind not in QUIET_KINDS:
        notify.send("backup_ok", server, f"{kind.capitalize()} backup of {server} saved "
                    f"({info['size'] / 1048576:.1f} MB)" + ("." if info["consistent"] else
                    ", but copied while the server was writing: turn RCON on for consistent backups."))
    return info

def _run_backup_locked(server, kind):
    d = manager.get_server_dir(server)
    if not os.path.isdir(d):
        raise BackupError(f"{server}'s folder {d} is missing.")
    fd = _lock(server)
    try:
        edition = _edition(server, d)
        running = _is_active(server)
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
        name = f"{server}-{stamp}-{kind}.tar.gz"
        final = os.path.join(server_backup_dir(server), name)
        partial = final + ".partial"
        consistent = True
        manager.write_log(f"[BACKUP] {kind.capitalize()} backup of {server} started"
                          f"{' (server running)' if running else ''}.")
        try:
            with tarfile.open(partial, "w:gz", compresslevel=6) as tar:
                _add_manifest(tar, _manifest(server, d, edition, kind))
                if not running:
                    _add_tree(tar, d)
                elif edition == "java":
                    consistent = _backup_running_java(server, d, tar)
                else:
                    consistent = _backup_running_bedrock(server, d, tar)
            os.chmod(partial, 0o600)
            os.replace(partial, final)
        finally:
            if os.path.exists(partial):
                os.remove(partial)
        size = os.path.getsize(final)
        manager.write_log(f"[BACKUP] Saved {final} ({size / 1048576:.1f} MB)"
                          + ("" if consistent else "; copied while the server was writing, see the warning above") + ".")
        if kind == "scheduled":
            s = get_schedule(server)
            prune(server, s["keep"], s["keep_days"])
        entry = next(b for b in list_backups(server) if b["file"] == name)
        return {**entry, "consistent": consistent}
    finally:
        os.close(fd)

def _backup_running_java(server, d, tar, full=False):
    send = _java_rcon(d)
    if not send:
        manager.write_log(f"[WARNING] {server} is running without RCON, so the backup copies files the server may be "
                          f"writing. Open its Live Chat tab once to set RCON up for consistent backups.")
        _add_tree(tar, d, full=full)
        return False
    try:
        send("save-off")
        send("save-all flush", timeout=JAVA_SAVE_TIMEOUT)
    except rcon.RconError as e:
        manager.write_log(f"[WARNING] RCON to {server} failed ({e}); copying its files live.")
        try:
            send("save-on")
        except rcon.RconError:
            pass
        _add_tree(tar, d, full=full)
        return False
    try:
        _add_tree(tar, d, full=full)
    finally:
        try:
            send("save-on")
        except rcon.RconError as e:
            manager.write_log(f"[ERROR] Couldn't turn saving back on for {server} ({e}). "
                              f"Run 'save-on' in its console, or restart it.")
    return True

def _backup_running_bedrock(server, d, tar, full=False):
    files = _bedrock_hold(server, d)
    try:
        if files is None:
            manager.write_log(f"[WARNING] {server} didn't answer 'save query' through screen; copying its files live.")
            _add_tree(tar, d, full=full)
            return False
        # The world comes only from the held list; everything else from the folder as usual.
        # The world folder is taken from that list, not level-name: without a level-name line,
        # Bedrock 1.26 names the folder "level".
        held = {f"worlds/{p.split('/', 1)[0]}" for p, _ in files}
        _add_tree(tar, d, skip_dirs=held, full=full)
        _add_held_files(tar, d, files)
        return True
    finally:
        _screen(server, "save resume")

def prune(server, keep, keep_days=0, now=None):
    """Deletes the oldest scheduled backups beyond keep, and (keep_days) those older than that
    many days, but never the newest one. Manual and pre-restore ones stay."""
    now = now or time.time()
    scheduled = [b for b in list_backups(server) if b["kind"] == "scheduled"]
    for i, b in enumerate(scheduled):
        too_old = keep_days and i > 0 and now - b["created"] > keep_days * 86400
        if i < keep and not too_old:
            continue
        os.remove(os.path.join(server_backup_dir(server), b["file"]))
        manager.write_log(f"[BACKUP] Removed old backup {b['file']} (keeping {keep}"
                          + (f", at most {keep_days} days old" if keep_days else "") + ").")

def delete_backup(server, filename):
    path = backup_path(server, filename)
    os.remove(path)
    manager.write_log(f"[BACKUP] Deleted backup {filename}.")


# ---------------------------------------------------------------------------
# Restoring a backup over the current world
# ---------------------------------------------------------------------------
def _archive_world(tar, edition):
    """(level-name, [world folders in the archive]) from the archive's own server.properties."""
    names = tar.getnames()
    level = None
    if "server.properties" in names:
        text = tar.extractfile("server.properties").read().decode("utf-8", errors="replace")
        level = properties.parse(text, java=(edition == "java")).get("level-name")
    if edition == "java":
        level = level or "world"
        candidates = [level, f"{level}_nether", f"{level}_the_end"]
    else:
        # Bedrock's folder for a missing level-name has changed between releases ("Bedrock level",
        # now "level"), so when level-name doesn't name a folder in the backup, use the backup's
        # only world folder.
        folders = sorted({n.split("/")[1] for n in names if n.startswith("worlds/") and n.count("/") >= 2})
        if (not level or level not in folders) and len(folders) == 1:
            level = folders[0]
        level = level or "Bedrock level"
        candidates = [f"worlds/{level}"]
    present = [c for c in candidates if any(n == c or n.startswith(c + "/") for n in names)]
    return level, present

def restore_backup(server, filename):
    """Replaces the server's world with the one in the backup. Takes a pre-restore backup first,
    stops the server while swapping, and starts it again if it was running."""
    path = backup_path(server, filename)
    d = manager.get_server_dir(server)
    edition = _edition(server, d)
    with tarfile.open(path, "r:gz") as tar:
        level, worlds = _archive_world(tar, edition)
    if not worlds:
        raise BackupError(f"{filename} has no world folder ('{level}') to restore.")
    if edition == "java":
        check_not_newer(server, d, path)

    was_running = _is_active(server)
    manager.write_log(f"[BACKUP] Restoring {server}'s world from {filename}.")
    if was_running:
        manager.run_and_log(["systemctl", "stop", f"{server}.service"])
    try:
        pre = run_backup(server, "pre-restore")
        staging = tempfile.mkdtemp(prefix=".redstonepanel-restore-", dir=d)
        try:
            with tarfile.open(path, "r:gz") as tar:
                members = [m for m in tar.getmembers()
                           if any(m.name == w or m.name.startswith(w + "/") for w in worlds)]
                tar.extractall(os.path.join(staging, "new"), members=members, filter="data")
            old = os.path.join(staging, "old")
            os.makedirs(old)
            for w in worlds:
                target = os.path.join(d, w)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                if os.path.lexists(target):
                    os.rename(target, os.path.join(old, w.replace("/", "_")))
                os.rename(os.path.join(staging, "new", w), target)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        if manager.read_properties(d).get("level-name") != level:
            manager.write_properties(d, {"level-name": level})
        manager.run_and_log(["chown", "-R", "minecraft:minecraft", d])
        manager.write_log(f"[BACKUP] Restored {', '.join(worlds)} from {filename}. "
                          f"The world it replaced is in {pre['file']}.")
        return {"restored": worlds, "pre_restore": pre["file"]}
    finally:
        if was_running:
            manager.run_and_log(["systemctl", "start", f"{server}.service"])


# ---------------------------------------------------------------------------
# Schedules -> systemd timers
# ---------------------------------------------------------------------------
def _load_schedules():
    try:
        with open(SCHEDULE_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}

def _save_schedules(data):
    os.makedirs(os.path.dirname(SCHEDULE_FILE), exist_ok=True)
    tmp = SCHEDULE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, SCHEDULE_FILE)

def get_schedule(server):
    s = dict(DEFAULT_SCHEDULE)
    stored = _load_schedules().get(server)
    if isinstance(stored, dict):
        s.update({k: stored[k] for k in DEFAULT_SCHEDULE if k in stored})
    return s

def on_calendar(frequency, time_str):
    m = TIME_RE.fullmatch(time_str or "")
    if frequency not in FREQUENCIES or not m:
        raise BackupError("Choose how often, and a time as HH:MM.")
    h, mi = int(m.group(1)), int(m.group(2))
    return FREQUENCIES[frequency][1].format(h=f"{h:02d}", m=f"{mi:02d}", h6=f"{h % 6:02d}", h12=f"{h % 12:02d}")

def _timer_name(server):
    return f"redstonepanel-backup@{server}.timer"

def _service_unit():
    return """[Unit]
Description=RedstonePanel backup of %i
After=network.target

[Service]
Type=oneshot
Environment=RP_BACKUP_KIND=scheduled
ExecStart=/usr/sbin/redstonepanel backup %i
Nice=10
IOSchedulingClass=idle
"""

def _timer_unit(server, calendar):
    return f"""[Unit]
Description=RedstonePanel backups of {server}

[Timer]
OnCalendar={calendar}
Persistent=true
RandomizedDelaySec=60

[Install]
WantedBy=timers.target
"""

def _write_if_changed(path, content):
    try:
        with open(path) as f:
            if f.read() == content:
                return False
    except OSError:
        pass
    with open(path, "w") as f:
        f.write(content)
    return True

def _apply_schedule(server, schedule):
    timer = _timer_name(server)
    timer_path = os.path.join(UNIT_DIR, timer)
    if schedule["enabled"]:
        _write_if_changed(os.path.join(UNIT_DIR, SERVICE_TEMPLATE), _service_unit())
        _write_if_changed(timer_path, _timer_unit(server, on_calendar(schedule["frequency"], schedule["time"])))
        manager.run_and_log(["systemctl", "daemon-reload"])
        manager.run_and_log(["systemctl", "enable", timer])
        manager.run_and_log(["systemctl", "restart", timer])
    else:
        _remove_timer(server)

def _remove_timer(server):
    timer = _timer_name(server)
    timer_path = os.path.join(UNIT_DIR, timer)
    if os.path.exists(timer_path):
        subprocess.run(["systemctl", "disable", "--now", timer], capture_output=True)
        os.remove(timer_path)
        manager.run_and_log(["systemctl", "daemon-reload"])

def set_schedule(server, enabled, frequency, time_str, keep, keep_days=0):
    _check_name(server)
    try:
        keep = int(keep)
    except (TypeError, ValueError):
        keep = 0
    if not 1 <= keep <= MAX_KEEP:
        raise BackupError(f"Keep between 1 and {MAX_KEEP} scheduled backups.")
    try:
        keep_days = int(keep_days or 0)
    except (TypeError, ValueError):
        keep_days = -1
    if not 0 <= keep_days <= MAX_KEEP_DAYS:
        raise BackupError(f"Delete scheduled backups after 1 to {MAX_KEEP_DAYS} days, or leave it empty.")
    on_calendar(frequency, time_str)            # validates both
    schedule = {"enabled": bool(enabled), "frequency": frequency, "time": time_str, "keep": keep,
                "keep_days": keep_days}
    data = _load_schedules()
    data[server] = schedule
    _save_schedules(data)
    _apply_schedule(server, schedule)
    manager.write_log(f"[BACKUP] Backups of {server}: " + (
        f"{FREQUENCIES[frequency][0].lower()} at {time_str}, keeping {keep}"
        + (f", at most {keep_days} days old" if keep_days else "") + "." if enabled else "off."))
    if enabled:
        prune(server, keep, keep_days)
    return schedule

def next_run(server):
    if not os.path.exists(os.path.join(UNIT_DIR, _timer_name(server))):
        return None
    out = subprocess.run(["systemctl", "show", "-p", "NextElapseUSecRealtime", "--value", _timer_name(server)],
                         capture_output=True, text=True).stdout.strip()
    return out if out and out != "n/a" else None

def overview(server):
    """Everything the Backups tab shows."""
    backups = list_backups(server)
    return {
        "schedule": get_schedule(server),
        "frequencies": {k: v[0] for k, v in FREQUENCIES.items()},
        "next_run": next_run(server),
        "timezone": time.strftime("%Z"),
        "last": read_status(server),
        "backups": backups,
        "total_size": sum(b["size"] for b in backups),
        "directory": server_backup_dir(server),
    }

# Called by manager when a server leaves or comes back
def on_server_removed(server, purged):
    if not NAME_RE.fullmatch(server or ""):
        return
    _remove_timer(server)
    if purged:
        data = _load_schedules()
        if data.pop(server, None) is not None:
            _save_schedules(data)

def on_server_restored(server):
    if NAME_RE.fullmatch(server or "") and get_schedule(server)["enabled"]:
        _apply_schedule(server, get_schedule(server))
