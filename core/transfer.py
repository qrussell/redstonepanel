"""Moving and copying a server between cluster members (1.16): what each member does.

The master runs a move (core/moves.py) by calling these on the source (A) and the target (B),
directly when it is one of them and through /api/cluster/transfers/... otherwise:

    A: export(server, stop)        -> the whole server folder as a .tar.gz, with its SHA-256
    B: begin_import(size, sha, meta), then receive(id, chunks): the archive, checked
    B: install(id, ...)            -> the server, under its own name and ports when they're free
    A: finish(id, "moved"|"copied"|"failed"), B: confirm(id) or abort(id)

A move with a pre-copy (1.16.13) first sends the folder while the server keeps running, so the
server is only down while the files that changed since are copied:

    A: export(server, False, precopy=True) -> the folder as it is, plus a list of every file
    B: begin_import + receive, then stage(id): unpacked next to the servers, not yet a server
    A: export(server, True, base=pre id)   -> stops it; only what changed, and what was deleted
    B: begin_import(..., base=staged id) + receive, then install(id): applied over the staged copy

Nothing is deleted on A: a moved server stays as a removed server (marked moved_to) until an
admin deletes it (delete_moved). Records live in DATA_DIR/transfers, next to their archives.
"""
import hashlib
import io
import json
import os
import platform
import re
import shutil
import stat
import tarfile
import threading
import time
import uuid

from core import archive, backups, manager, restarts
from core.settings import DATA_DIR
from core.platform import services

TRANSFER_DIR = os.path.join(DATA_DIR, "transfers")
ID_RE = re.compile(r"^[0-9a-f]{32}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
STALE = 86400
CHUNK = 1 << 16
START_CHECK = 20            # seconds a started server must stay running
JAVA_WAIT = 1800
RECENT = 2                  # seconds: a file changed this close to the pre-copy is sent again anyway
DELTA_LIST = ".redstonepanel-precopy.json"   # inside a pre-copy's second archive: what was deleted

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


def list_path(tid):
    return os.path.join(_dir(), f"{tid}.list.json")


def _staging_ok(path):
    """A staged pre-copy folder this module made (never anything else gets removed)."""
    return (isinstance(path, str) and os.path.dirname(path) == manager.SERVERS_ROOT.rstrip("/")
            and os.path.basename(path).startswith(".redstonepanel-import-"))


def _drop(tid):
    try:
        staging = _load(tid).get("staging")
    except TransferError:
        staging = None
    if _staging_ok(staging):
        shutil.rmtree(staging, ignore_errors=True)
    for p in (_rec_path(tid), archive_path(tid), archive_path(tid) + ".partial", list_path(tid)):
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
    from core import deployopts
    return {"name": server, "edition": edition, **manager.unit_heap(exec_start),
            "jvm": deployopts.get_flags(server) if edition == "java" else None,
            "backup": backups.get_schedule(server), "restart": restarts.get_schedule(server),
            "phantom": edition == "bedrock" and _phantom_installed(server)}


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _listed(d, full):
    """(rel, lstat) for everything _add_tree would pack, parents before children."""
    for root, dirs, files in os.walk(d):
        rel_root = os.path.relpath(root, d).replace(os.sep, "/")
        rel_root = "" if rel_root == "." else rel_root
        keep = []
        for name in sorted(dirs):
            rel = f"{rel_root}/{name}" if rel_root else name
            if backups._skipped(rel, full):
                continue
            try:
                st = os.lstat(os.path.join(root, name))
            except FileNotFoundError:
                continue
            yield rel, st
            if stat.S_ISDIR(st.st_mode):
                keep.append(name)
        dirs[:] = keep
        for name in sorted(files):
            rel = f"{rel_root}/{name}" if rel_root else name
            if backups._skipped(rel, full):
                continue
            try:
                yield rel, os.lstat(os.path.join(root, name))
            except FileNotFoundError:
                pass          # deleted by the running server while we walked


def _stamp(st):
    """What says a file hasn't changed: size, times (ctime can't be set back) and inode."""
    return [st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino]


class _Exact:
    """Reads exactly n bytes of a file the server may be writing: if it shrank while being read,
    the rest is zeros (the archive stays valid) and .short says the copy is no good."""
    def __init__(self, f, n):
        self.f, self.left, self.short = f, n, False

    def read(self, size=-1):
        size = self.left if size is None or size < 0 else min(size, self.left)
        data = self.f.read(size)
        if len(data) < size:
            self.short = True
            data += b"\0" * (size - len(data))
        self.left -= len(data)
        return data


def _pack_listed(tar, d):
    """The pre-copy: everything, live, with each file's stamp taken before it is read, so a file
    written while (or after) it is read is sent again by _pack_changed. Returns the list."""
    listing = {}
    for rel, st in _listed(d, True):
        path = os.path.join(d, rel)
        if not stat.S_ISREG(st.st_mode):
            try:
                tar.add(path, arcname=rel, recursive=False)
            except FileNotFoundError:
                continue
            listing[rel] = ["d" if stat.S_ISDIR(st.st_mode) else "l", time.time()]
            continue
        listed_at = time.time()
        try:
            f = open(path, "rb", opener=lambda p, fl: os.open(p, fl | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)))
        except OSError:
            continue                            # gone or replaced: the second archive sends it
        with f:
            info = tar.gettarinfo(arcname=rel, fileobj=f)
            src = _Exact(f, info.size)
            tar.addfile(info, src)
        # the stamp from before reading: any write since shows as a newer ctime
        listing[rel] = ["f", listed_at] + ([] if src.short else _stamp(st))
    return listing


def unchanged(old, st):
    """True if a pre-copied file (its listing entry) is certainly the same now. Timestamps are
    only as fine as the kernel's clock tick, so a file changed within RECENT seconds of being
    listed counts as changed."""
    if not old or old[0] != "f" or not stat.S_ISREG(st.st_mode):
        return False
    listed_at, stamp = old[1], old[2:]
    return stamp == _stamp(st) and st.st_ctime_ns / 1e9 < listed_at - RECENT


def _pack_changed(tar, d, listing):
    """The second archive of a pre-copy (server stopped): folders, links and the files that are
    new or changed, then DELTA_LIST naming what was deleted. Returns (files sent, deleted)."""
    seen, sent = {}, 0
    for rel, st in _listed(d, True):
        seen[rel] = "d" if stat.S_ISDIR(st.st_mode) else "f" if stat.S_ISREG(st.st_mode) else "l"
        if unchanged(listing.get(rel), st):
            continue
        tar.add(os.path.join(d, rel), arcname=rel, recursive=False)
        sent += seen[rel] != "d"
    # deleted since, or now another kind of thing (a folder became a file): deleted, then sent again
    gone = sorted(rel for rel, old in listing.items() if seen.get(rel) is None
                  or (seen[rel] == "d") != (old[0] == "d") or seen[rel] == "l" or old[0] == "l")
    data = json.dumps({"deleted": gone}).encode()
    info = tarfile.TarInfo(DELTA_LIST)
    info.size, info.mtime, info.mode = len(data), int(time.time()), 0o600
    tar.addfile(info, io.BytesIO(data))
    return sent, len(gone)


def export(server, stop, precopy=False, base=None):
    """Archives the server folder. stop=True (a move) stops the server first and keeps it from
    being started here until finish(); otherwise a live snapshot is taken, as backups do.
    precopy=True (with stop=False) packs it as it is while it runs and lists every file; a later
    export(stop=True, base=that id) packs only what changed since (see the module docstring)."""
    manager._check_server(server)
    cleanup_stale()
    listing = None
    if precopy and (stop or base):
        raise TransferError("A pre-copy doesn't stop the server.")
    if base:
        pre = _load(base, "export")
        if not stop or not pre.get("precopy") or pre.get("server") != server or pre.get("state") != "ready":
            raise TransferError("That pre-copy can't be used.", 409)
        try:
            with open(list_path(base)) as f:
                listing = json.load(f)
        except (OSError, ValueError):
            raise TransferError("The pre-copy's file list is missing.", 409)
    with _lock:
        if server in _exporting or is_moving(server):
            raise TransferError(f"{server} is already being moved or copied.", 409)
        _exporting.add(server)
    tid = uuid.uuid4().hex
    rec = {"id": tid, "kind": "export", "server": server, "stop": bool(stop), "created": int(time.time()),
           "precopy": bool(precopy), "base": base}
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
            services.stop(server)
        partial = archive_path(tid) + ".partial"
        consistent, what = True, "a move" if stop else "a copy"
        with tarfile.open(partial, "w:gz", compresslevel=1) as tar:
            if listing is not None:
                sent, gone = _pack_changed(tar, d, listing)
                what = f"the end of a move ({sent} changed file{'s' if sent != 1 else ''}, {gone} deleted)"
            elif precopy:
                listing = _pack_listed(tar, d)
                consistent, what = False, "a pre-copy while it runs"
            elif stop or not rec["was_running"]:
                backups._add_tree(tar, d, full=True)
            elif edition == "java":
                consistent = backups._backup_running_java(server, d, tar, full=True)
            else:
                consistent = backups._backup_running_bedrock(server, d, tar, full=True)
        os.chmod(partial, 0o600)
        os.replace(partial, archive_path(tid))
        if precopy:
            fd2 = os.open(list_path(tid), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd2, "w") as f:
                json.dump(listing, f)
        rec.update(size=os.path.getsize(archive_path(tid)), sha256=_sha256(archive_path(tid)),
                   consistent=consistent, state="ready")
        _save(rec)
        manager.write_log(f"[MOVE] Packed {server} for {what} ({rec['size'] / 1048576:.1f} MB).")
        if base:
            _drop(base)                         # its archive is on the target already
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
        services.start(rec["server"])


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
    if meta["edition"] == "java" and isinstance(meta.get("jvm"), dict):
        from core import deployopts
        try:
            out["jvm"] = deployopts.clean_flags(meta["jvm"])     # startup flags (1.17.2), checked again here
        except deployopts.OptionError:
            pass
    for k in ("backup", "restart"):
        v = meta.get(k)
        if isinstance(v, dict) and len(v) <= 8 and all(isinstance(x, (str, int, bool)) for x in v.values()):
            out[k] = {str(a)[:20]: b for a, b in v.items()}
    return out


def begin_import(size, sha256, meta, base=None):
    """base: the id of a staged pre-copy here that this archive's changes go on top of."""
    cleanup_stale()
    if base is not None:
        pre = _load(base, "import")
        if pre.get("state") != "staged":
            raise TransferError("That pre-copy isn't ready here.", 409)
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
           "created": int(time.time()), "state": "receiving", "base": base})
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
        state = services.state(name)
        if state == "failed":
            break
        time.sleep(2)
    if state != "active":
        tail = services.log(services.unit(name), lines=8).strip()
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


def stage(tid):
    """A pre-copy has arrived: unpack it next to the servers (a hidden folder, not a server yet)
    for the second archive to be applied over by install()."""
    rec = _load(tid, "import")
    if rec.get("state") != "received":
        raise TransferError("The archive hasn't arrived yet.", 409)
    try:
        staging, root = archive.unpack(archive_path(tid), manager.SERVERS_ROOT)
    except archive.ArchiveError as e:
        raise TransferError(str(e))
    rec.update(state="staged", staging=staging, root=root)
    _save(rec)
    try:
        os.remove(archive_path(tid))
    except OSError:
        pass
    return {"status": "success"}


def _inside(root, rel):
    """root/rel, or None if rel is unsafe or a folder on the way isn't a real folder (a link
    there could lead outside root, so nothing under it is touched)."""
    parts = [p for p in str(rel).replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts or ".." in parts or str(rel).startswith("/"):
        return None
    cur = root
    for p in parts[:-1]:
        cur = os.path.join(cur, p)
        try:
            if not stat.S_ISDIR(os.lstat(cur).st_mode):
                return None
        except FileNotFoundError:
            return None
    return os.path.join(cur, parts[-1])


def _remove(path):
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(st.st_mode):
        shutil.rmtree(path)                     # never follows links inside
    else:
        os.remove(path)


def apply_changes(archive_file, root):
    """Applies a pre-copy's second archive over the staged folder at root: deletes what was
    deleted, then unpacks what changed (with the same 'data' filter as every import)."""
    with tarfile.open(archive_file) as tar:
        members = tar.getmembers()
        listed = next((m for m in members if m.name == DELTA_LIST and m.isfile()), None)
        if listed is None:
            raise TransferError("The second part of the pre-copy has no list of deleted files.")
        try:
            deleted = json.loads(tar.extractfile(listed).read(1 << 26)).get("deleted", [])
        except (ValueError, AttributeError):
            raise TransferError("The pre-copy's list of deleted files is damaged.")
        members = [m for m in members if m is not listed]
        for m in members:
            archive._clean_name(m.name)
        needed = sum(m.size for m in members if m.isfile())
        if needed > shutil.disk_usage(root).free * 0.9:
            raise TransferError(f"Not enough disk space here for the files that changed ({needed / 1e9:.1f} GB).", 507)
        for rel in deleted if isinstance(deleted, list) else []:
            target = _inside(root, rel)
            if target:
                _remove(target)
        for m in members:
            target = _inside(root, m.name)
            if target and os.path.lexists(target) and (os.path.islink(target) or not (m.isdir() and os.path.isdir(target))):
                _remove(target)                 # replaced: never write through what's there
        try:
            tar.extractall(root, members=members, filter="data")
        except tarfile.FilterError as e:
            raise TransferError(f"Unsafe archive entry refused: {e}")
    return len(deleted), sum(1 for m in members if not m.isdir())


def install(tid, keep_name=True, start=True, carry=True, actor="master"):
    """Turns a received archive into a server here. On any failure, what was made is removed."""
    rec = _load(tid, "import")
    if rec.get("state") != "received":
        raise TransferError("The archive hasn't arrived yet.", 409)
    meta = rec["meta"]
    pre = None
    if rec.get("base"):
        pre = _load(rec["base"], "import")
        if pre.get("state") != "staged" or not _staging_ok(pre.get("staging")):
            raise TransferError("The pre-copy isn't here any more.", 409)
    rec["state"] = "installing"
    _save(rec)
    staging = None
    try:
        if pre:
            staging, root = pre["staging"], pre["root"]
            deleted, changed = apply_changes(archive_path(tid), root)
            manager.write_log(f"[MOVE] Applied the changes since the pre-copy: {changed} changed, {deleted} deleted.")
        else:
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
                                            start=start, on_placed=placed, jvm=meta.get("jvm") or {})
        if start:
            checked = time.time()
            _stays_running(result["name"])
            result["checked_for"] = round(time.time() - checked)    # running all along: not downtime
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
        if pre:
            _drop(pre["id"])


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
