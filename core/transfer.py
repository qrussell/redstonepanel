"""Moving and copying a server between cluster members (1.16): what each member does.

The master runs a move (core/moves.py) by calling these on the source (A) and the target (B),
directly when it is one of them and through /api/cluster/transfers/... otherwise:

    A: export(server, stop)        -> the whole server folder as a .tar.gz, with its SHA-256
    B: begin_import(size, sha, meta), then receive(id, chunks): the archive, checked
    B: install(id, ...)            -> the server, under its own name and ports when they're free
    A: finish(id, "moved"|"copied"|"failed"), B: confirm(id) or abort(id)

Nothing is deleted on A: a moved server stays as a removed server (marked moved_to) until an
admin deletes it (delete_moved). Records live in DATA_DIR/transfers, next to their archives.
"""
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tarfile
import threading
import time
import uuid

from core import archive, backups, manager, restarts
from core.settings import DATA_DIR

TRANSFER_DIR = os.path.join(DATA_DIR, "transfers")
ID_RE = re.compile(r"^[0-9a-f]{32}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
STALE = 86400
CHUNK = 1 << 16
START_CHECK = 20            # seconds a started server must stay running
JAVA_WAIT = 1800

_lock = threading.Lock()
_exporting = set()          # servers being archived right now


class TransferError(ValueError):
    """A transfer step that can't be done (message is safe to show)."""
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------
def _dir():
    os.makedirs(TRANSFER_DIR, mode=0o700, exist_ok=True)
    return TRANSFER_DIR


def _rec_path(tid):
    return os.path.join(_dir(), f"{tid}.json")


def archive_path(tid):
    return os.path.join(_dir(), f"{tid}.tar.gz")


def _save(rec):
    tmp = _rec_path(rec["id"]) + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(rec, f)
    os.replace(tmp, _rec_path(rec["id"]))


def _load(tid, kind=None):
    if not ID_RE.match(tid or ""):
        raise TransferError("No such transfer.", 404)
    try:
        with open(_rec_path(tid)) as f:
            rec = json.load(f)
    except (OSError, ValueError):
        raise TransferError("No such transfer.", 404)
    if kind and rec.get("kind") != kind:
        raise TransferError("No such transfer.", 404)
    return rec


def _drop(tid):
    for p in (_rec_path(tid), archive_path(tid), archive_path(tid) + ".partial"):
        try:
            os.remove(p)
        except OSError:
            pass


def _records():
    out = []
    if not os.path.isdir(TRANSFER_DIR):
        return out
    for f in os.listdir(TRANSFER_DIR):
        if f.endswith(".json") and ID_RE.match(f[:-5]):
            try:
                out.append(_load(f[:-5]))
            except TransferError:
                pass
    return out


def is_moving(server):
    """True while a move has stopped this server here and not finished."""
    return any(r.get("kind") == "export" and r.get("stop") and r.get("server") == server for r in _records())


def cleanup_stale(now=None):
    """Forgets transfers a master never finished (it was restarted or lost the node). Never
    deletes or starts a server: an unfinished import stays installed, an unfinished move's
    server stays stopped, and both are logged for an admin."""
    now = time.time() if now is None else now
    for rec in _records():
        if now - rec.get("created", 0) < STALE:
            continue
        if rec.get("kind") == "import" and rec.get("installed"):
            manager.write_log(f"[MOVE] A move into {rec['installed']} was never confirmed by the master; keeping it.")
        elif rec.get("kind") == "export" and rec.get("stop"):
            manager.write_log(f"[MOVE] A move of {rec.get('server')} never finished; it can be started here again.")
        _drop(rec["id"])


# ---------------------------------------------------------------------------
# Source (A)
# ---------------------------------------------------------------------------
def _phantom_installed(server):
    try:
        from core import phantom
        text = phantom._read_unit(phantom.unit_name(server))
        return text is not None and phantom.managed_server(text) == server
    except Exception:
        return False


def _meta(server, edition):
    exec_start = manager._unit_exec_start(server)
    return {"name": server, "edition": edition, **manager.unit_heap(exec_start),
            "backup": backups.get_schedule(server), "restart": restarts.get_schedule(server),
            "phantom": edition == "bedrock" and _phantom_installed(server)}


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def export(server, stop):
    """Archives the server folder. stop=True (a move) stops the server first and keeps it from
    being started here until finish(); otherwise a live snapshot is taken, as backups do."""
    manager._check_server(server)
    cleanup_stale()
    with _lock:
        if server in _exporting or is_moving(server):
            raise TransferError(f"{server} is already being moved or copied.", 409)
        _exporting.add(server)
    tid = uuid.uuid4().hex
    rec = {"id": tid, "kind": "export", "server": server, "stop": bool(stop), "created": int(time.time())}
    fd = None
    try:
        d = manager.get_server_dir(server)
        if not os.path.isdir(d):
            raise TransferError(f"{server}'s folder {d} is missing.")
        edition = backups._edition(server, d)
        rec["was_running"] = backups._is_active(server)
        rec["meta"] = _meta(server, edition)
        _save(rec)                              # from here on, is_moving() refuses Start
        try:
            fd = backups._lock(server)          # never while a backup of it runs
        except backups.BackupError as e:
            raise TransferError(f"{e} Try again when it's done.", 409)
        if stop and rec["was_running"]:
            manager.write_log(f"[MOVE] Stopping {server} to move it.")
            manager.run_and_log(["systemctl", "stop", f"{server}.service"])
        partial = archive_path(tid) + ".partial"
        consistent = True
        with tarfile.open(partial, "w:gz", compresslevel=1) as tar:
            if stop or not rec["was_running"]:
                backups._add_tree(tar, d, full=True)
            elif edition == "java":
                consistent = backups._backup_running_java(server, d, tar, full=True)
            else:
                consistent = backups._backup_running_bedrock(server, d, tar, full=True)
        os.chmod(partial, 0o600)
        os.replace(partial, archive_path(tid))
        rec.update(size=os.path.getsize(archive_path(tid)), sha256=_sha256(archive_path(tid)),
                   consistent=consistent, state="ready")
        _save(rec)
        manager.write_log(f"[MOVE] Packed {server} for {'a move' if stop else 'a copy'} "
                          f"({rec['size'] / 1048576:.1f} MB).")
        return {k: rec[k] for k in ("id", "size", "sha256", "was_running", "consistent", "meta")}
    except Exception:
        _finish_failed(rec)
        _drop(tid)
        raise
    finally:
        if fd is not None:
            os.close(fd)
        with _lock:
            _exporting.discard(server)


def _finish_failed(rec):
    if rec.get("stop") and rec.get("was_running"):
        _drop(rec["id"])                        # so is_moving() lets it start
        manager.write_log(f"[MOVE] The move of {rec['server']} didn't happen; starting it here again.")
        manager.run_and_log(["systemctl", "start", f"{rec['server']}.service"])


def open_archive(tid):
    rec = _load(tid, "export")
    if rec.get("state") != "ready":
        raise TransferError("That archive isn't ready.", 409)
    return archive_path(tid), rec["size"]


def finish(tid, outcome, moved_to=None):
    """The end of an export: 'moved' takes the server off this panel (its files stay as a
    removed server marked moved_to), 'copied' just cleans up, 'failed' starts it again."""
    rec = _load(tid, "export")
    if outcome not in ("moved", "copied", "failed"):
        raise TransferError("Unknown outcome.")
    if outcome == "moved":
        if not rec.get("stop"):
            raise TransferError("That was a copy, not a move.")
        note = {"moved_to": str(moved_to or "another cluster member")[:100], "moved_at": int(time.time())}
        _drop(tid)
        manager.remove_server(rec["server"], keep_world=True, note=note)
        manager.write_log(f"[MOVE] {rec['server']} moved to {note['moved_to']}. Its files stay here as a "
                          f"removed server until an admin deletes them.")
    elif outcome == "failed":
        _finish_failed(rec)
    _drop(tid)
    return {"status": "success"}


def delete_moved(name):
    """Deletes a removed server's folder, only if a move left it behind."""
    match = next((s for s in manager.list_removed_servers() if s["name"] == name), None)
    if not match or not match.get("moved_to"):
        raise TransferError(f"There's no moved server called '{name}' here.", 404)
    manager.delete_removed_server(name)
    return {"status": "success"}


# ---------------------------------------------------------------------------
# Target (B)
# ---------------------------------------------------------------------------
def _clean_meta(meta):
    if not isinstance(meta, dict) or meta.get("edition") not in ("java", "bedrock"):
        raise TransferError("Bad server details.")
    if meta["edition"] == "bedrock" and not manager.bedrock_supported():
        raise TransferError(f"This member is an {platform.machine()} machine, and Bedrock servers only run on "
                            f"x86-64 (64-bit Intel or AMD). Move it to another member.")
    out = {"edition": meta["edition"], "phantom": meta.get("phantom") is True}
    name = meta.get("name")
    out["name"] = name if isinstance(name, str) and manager.SERVICE_NAME_RE.match(name) else None
    for k in ("xms", "xmx"):
        if isinstance(meta.get(k), str) and manager.HEAP_RE.match(meta[k]):
            out[k] = meta[k]
    for k in ("backup", "restart"):
        v = meta.get(k)
        if isinstance(v, dict) and len(v) <= 8 and all(isinstance(x, (str, int, bool)) for x in v.values()):
            out[k] = {str(a)[:20]: b for a, b in v.items()}
    return out


def begin_import(size, sha256, meta):
    cleanup_stale()
    if not isinstance(size, int) or size <= 0:
        raise TransferError("Bad archive size.")
    if not isinstance(sha256, str) or not SHA_RE.match(sha256):
        raise TransferError("Bad archive checksum.")
    meta = _clean_meta(meta)
    free_here = shutil.disk_usage(_dir()).free
    free_opt = shutil.disk_usage(manager.SERVERS_ROOT).free
    # The archive, then the folder it unpacks to (at least as big; unpacking checks again)
    if os.stat(_dir()).st_dev == os.stat(manager.SERVERS_ROOT).st_dev:
        enough = size * 2.1 <= free_here
    else:
        enough = size * 1.05 <= free_here and size * 1.05 <= free_opt
    if not enough:
        raise TransferError(f"Not enough disk space here: the server needs about {size * 2 / 1e9:.1f} GB "
                            f"while it's copied, {min(free_here, free_opt) / 1e9:.1f} GB free.", 507)
    tid = uuid.uuid4().hex
    _save({"id": tid, "kind": "import", "size": size, "sha256": sha256, "meta": meta,
           "created": int(time.time()), "state": "receiving"})
    return {"id": tid}


class Receiver:
    """Writes an archive as it arrives, counting and hashing it."""
    def __init__(self, tid):
        self.rec = _load(tid, "import")
        if self.rec.get("state") != "receiving":
            raise TransferError("That transfer isn't waiting for an archive.", 409)
        self.path = archive_path(tid) + ".partial"
        self.f = open(self.path, "wb")
        os.chmod(self.path, 0o600)
        self.h, self.n = hashlib.sha256(), 0

    def write(self, chunk):
        self.n += len(chunk)
        if self.n > self.rec["size"]:
            self.abort()
            raise TransferError("More data arrived than the archive's size.")
        self.h.update(chunk)
        self.f.write(chunk)

    def close(self):
        self.f.close()
        if self.n != self.rec["size"] or self.h.hexdigest() != self.rec["sha256"]:
            os.remove(self.path)
            raise TransferError(f"The archive arrived damaged ({self.n} of {self.rec['size']} bytes, "
                                f"checksum {'matches' if self.h.hexdigest() == self.rec['sha256'] else 'differs'}).")
        os.replace(self.path, archive_path(self.rec["id"]))
        self.rec["state"] = "received"
        _save(self.rec)
        return {"size": self.n}

    def abort(self):
        try:
            self.f.close()
            os.remove(self.path)
        except OSError:
            pass


def receive(tid, chunks):
    r = Receiver(tid)
    try:
        for chunk in chunks:
            r.write(chunk)
    except BaseException:
        r.abort()
        raise
    return r.close()


def _java_for(root, actor):
    """The Java this folder needs, installing it first if it's missing."""
    from core import runtimes
    try:
        return manager.java_for_folder(root)
    except manager.JavaMissing as e:
        required = e.required
    manager.write_log(f"[MOVE] The server needs Java {required}; installing it.")
    deadline = time.time() + JAVA_WAIT
    while True:
        try:
            runtimes.install(required, actor=actor)
            break
        except runtimes.RuntimeJobError as e:
            if "already installed" in str(e):
                break
            if time.time() > deadline:
                raise TransferError(str(e))
            time.sleep(5)                       # another Java job is running; wait for it
    while (runtimes.job() or {}).get("state") == "running" and time.time() < deadline:
        time.sleep(3)
    job = runtimes.job() or {}
    if job.get("state") == "failed":
        raise TransferError(f"Installing Java {required} failed: {job.get('error')}")
    try:
        return manager.java_for_folder(root)
    except manager.JavaMissing as e:
        raise TransferError(str(e))


def _stays_running(name):
    deadline = time.time() + START_CHECK
    state = ""
    while time.time() < deadline:
        state = subprocess.run(["systemctl", "is-active", f"{name}.service"],
                               capture_output=True, text=True).stdout.strip()
        if state == "failed":
            break
        time.sleep(2)
    if state != "active":
        tail = subprocess.run(["journalctl", "-u", f"{name}.service", "-n", "8", "--no-pager", "-o", "cat"],
                              capture_output=True, text=True).stdout.strip()
        raise TransferError(f"{name} didn't keep running here ({state or 'unknown'})." + (f" Last lines: {tail}" if tail else ""))


def _carry(name, meta, actor):
    """Backup and restart schedules and Phantom, as the server had them. Never fails the move."""
    b = meta.get("backup") or {}
    if b.get("enabled"):
        try:
            backups.set_schedule(name, True, b.get("frequency"), b.get("time"), b.get("keep"),
                                  b.get("keep_days") or 0)
        except Exception as e:
            manager.write_log(f"[MOVE] Couldn't set {name}'s backup schedule: {e}")
    r = meta.get("restart") or {}
    if r.get("enabled"):
        try:
            restarts.set_schedule(name, True, r.get("frequency"), r.get("time"), r.get("warn"))
        except Exception as e:
            manager.write_log(f"[MOVE] Couldn't set {name}'s restart schedule: {e}")
    if meta.get("phantom") and meta["edition"] == "bedrock":
        try:
            from core import phantom
            phantom.install(name, actor=actor)
        except Exception as e:
            manager.write_log(f"[MOVE] Couldn't set up Phantom for {name}: {e}")


def install(tid, keep_name=True, start=True, carry=True, actor="master"):
    """Turns a received archive into a server here. On any failure, what was made is removed."""
    rec = _load(tid, "import")
    if rec.get("state") != "received":
        raise TransferError("The archive hasn't arrived yet.", 409)
    meta = rec["meta"]
    rec["state"] = "installing"
    _save(rec)
    staging = None
    try:
        try:
            staging, root = archive.unpack(archive_path(tid), manager.SERVERS_ROOT)
        except archive.ArchiveError as e:
            raise TransferError(str(e))
        java = _java_for(root, actor) if meta["edition"] == "java" else None

        def placed(name):
            rec["installed"] = name
            _save(rec)
        result = manager.import_transferred(root, meta["edition"], prefer_name=meta.get("name") if keep_name else None,
                                            heap={k: meta[k] for k in ("xms", "xmx") if k in meta}, java=java,
                                            start=start, on_placed=placed)
        if start:
            _stays_running(result["name"])
        if carry:
            _carry(result["name"], meta, actor)
        rec["state"] = "installed"
        _save(rec)
        try:
            os.remove(archive_path(tid))
        except OSError:
            pass
        return result
    except Exception:
        if rec.get("installed"):
            manager.write_log(f"[MOVE] Removing {rec['installed']} again: the move into it failed.")
            try:
                manager.discard_server(rec["installed"])
            except Exception as e:
                manager.write_log(f"[MOVE] Couldn't remove {rec['installed']}: {e}")
        _drop(tid)
        raise
    finally:
        if staging:
            shutil.rmtree(staging, ignore_errors=True)


def confirm(tid):
    _load(tid, "import")
    _drop(tid)
    return {"status": "success"}


def abort(tid):
    """Undoes a transfer: an import's server and folder go, an export's server starts again."""
    rec = _load(tid)
    if rec["kind"] == "import" and rec.get("installed"):
        manager.write_log(f"[MOVE] The master cancelled the move into {rec['installed']}; removing it.")
        manager.discard_server(rec["installed"])
    elif rec["kind"] == "export":
        _finish_failed(rec)
    _drop(tid)
    return {"status": "success"}
