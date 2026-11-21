"""Backup copies on another cluster member (1.16.8).

A backup on the same disk doesn't survive losing the disk. In a cluster, each server's backups
can also be kept on another member: the master, or another node.

Nodes only ever talk to the master (never to each other, and never to the master unasked), so
the master does the copying, the same way it moves servers (docs/design/move.md):

    source member: its own backups, as always (systemd timer or Back up now), plus each
                   archive's SHA-256 on request (GET /api/servers/<server>/backups/<file>/sha256)
    master:        every few minutes, and right after Back up now, lists each source's backups,
                   streams the missing ones from the source to the holding member, and records
                   the result. A failure is a Needs attention item and a webhook event.
    holder:        receives the archive, checks its length and SHA-256, keeps it under
                   /var/backups/redstonepanel/remote/<panel>/<server>/ and prunes scheduled
                   copies to the source's own Keep.

<panel> is the source's cluster member id ("master" for the master, "n-…" for a node), so
copies of two servers with the same name on different members never mix. Copies stay on the
holder when the source goes away; they can be downloaded, or put into any server's backups
(on any member) and restored from there.

The master keeps the settings in DATA_DIR/backup-copies.json ({"<member>/<server>": "<holder>"})
and the results in DATA_DIR/backup-copies-status.json. A holder keeps nothing but the files.
"""
import calendar
import hashlib
import json
import os
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from core import backups, cluster, cluster_client, manager, notify
from core import settings as panel_settings
from core.settings import DATA_DIR

CONFIG_FILE = os.path.join(DATA_DIR, "backup-copies.json")
STATUS_FILE = os.path.join(DATA_DIR, "backup-copies-status.json")
COPY_API = 4                    # a member needs this cluster contract to hold or send copies
COPY_KINDS = ("scheduled", "manual")
PANEL_RE = re.compile(r"^(master|n-[0-9a-f]{4,32})$")
MEMBER_RE = re.compile(r"^(local|n-[0-9a-f]{4,32})$")
SWEEP_EVERY = 300
FIRST_SWEEP = 90
CHUNK = 1 << 16
PANEL_INFO = ".panel.json"
SHA_SUFFIX = ".sha256"
ACTOR = "backup copies"

_lock = threading.Lock()
_running = set()                # "<member>/<server>" being copied right now


class CopyError(ValueError):
    """Something a user can act on (message is safe to show)."""
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Holder: the copies this panel keeps for other members
# ---------------------------------------------------------------------------
def hold_root():
    return os.path.join(backups.BACKUP_ROOT, "remote")


def _check_panel(panel):
    if not PANEL_RE.fullmatch(panel or ""):
        raise CopyError("Bad cluster member.")


def _check_file(server, filename):
    m = backups.FILE_RE.fullmatch(filename or "")
    if not m or m.group("server") != server:
        raise CopyError("No such backup.", 404)
    return m


def held_dir(panel, server):
    _check_panel(panel)
    if not backups.NAME_RE.fullmatch(server or ""):
        raise CopyError("Bad server name.")
    return os.path.join(hold_root(), panel, server)


def held_path(panel, server, filename):
    _check_file(server, filename)
    path = os.path.join(held_dir(panel, server), filename)
    if not os.path.isfile(path):
        raise CopyError("No such backup copy.", 404)
    return path


def _panel_name(panel):
    try:
        with open(os.path.join(hold_root(), panel, PANEL_INFO)) as f:
            name = json.load(f).get("name")
        return name if isinstance(name, str) else panel
    except (OSError, ValueError, AttributeError):
        return panel


def _read_sha(path):
    try:
        with open(path + SHA_SUFFIX) as f:
            sha = f.read().strip()
        return sha if re.fullmatch(r"[0-9a-f]{64}", sha) else None
    except OSError:
        return None


def list_held(panel=None, server=None):
    """Every copy kept here, newest first: panel, panel_name, server, file, kind, size, created."""
    root = hold_root()
    out = []
    if not os.path.isdir(root):
        return out
    panels = [panel] if panel else sorted(os.listdir(root))
    for p in panels:
        if not PANEL_RE.fullmatch(p) or not os.path.isdir(os.path.join(root, p)):
            continue
        pname = _panel_name(p)
        servers = [server] if server else sorted(os.listdir(os.path.join(root, p)))
        for s in servers:
            d = os.path.join(root, p, s)
            if not backups.NAME_RE.fullmatch(s) or not os.path.isdir(d):
                continue
            for f in os.listdir(d):
                m = backups.FILE_RE.fullmatch(f)
                if not m or m.group("server") != s:
                    continue
                st = os.stat(os.path.join(d, f))
                out.append({"panel": p, "panel_name": pname, "server": s, "file": f, "kind": m.group("kind"),
                            "size": st.st_size, "created": _utc(m.group("stamp")), "copied": int(st.st_mtime)})
    out.sort(key=lambda b: (b["created"], b["file"]), reverse=True)
    return out


def _utc(stamp):
    return calendar.timegm(time.strptime(stamp, "%Y%m%d-%H%M%S"))


class Receiver:
    """Writes an archive as it arrives to final_path, checking its length and SHA-256 before it
    takes the final name. An archive already there with the same checksum is kept as it is."""
    def __init__(self, final_path, size, sha256):
        if not isinstance(size, int) or size <= 0:
            raise CopyError("Bad archive size.")
        if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise CopyError("Bad archive checksum.")
        self.final, self.size, self.sha = final_path, size, sha256
        d = os.path.dirname(final_path)
        os.makedirs(d, mode=0o700, exist_ok=True)
        os.chmod(backups.BACKUP_ROOT, 0o700)
        free = shutil.disk_usage(d).free
        if size * 1.05 > free:
            raise CopyError(f"Not enough disk space on {panel_settings.display_name()} for the copy: it needs "
                            f"{size / 1e9:.1f} GB, {free / 1e9:.1f} GB is free.", 507)
        self.partial = final_path + ".partial"
        fd = os.open(self.partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        self.f = os.fdopen(fd, "wb")
        self.h, self.n = hashlib.sha256(), 0

    def write(self, chunk):
        self.n += len(chunk)
        if self.n > self.size:
            self.abort()
            raise CopyError("More data arrived than the archive's size.")
        self.h.update(chunk)
        self.f.write(chunk)

    def close(self):
        self.f.close()
        if self.n != self.size or self.h.hexdigest() != self.sha:
            os.remove(self.partial)
            raise CopyError(f"The backup arrived damaged ({self.n} of {self.size} bytes, checksum "
                            f"{'matches' if self.h.hexdigest() == self.sha else 'differs'}).")
        os.replace(self.partial, self.final)
        return {"file": os.path.basename(self.final), "size": self.n}

    def abort(self):
        try:
            self.f.close()
        except OSError:
            pass
        try:
            os.remove(self.partial)
        except OSError:
            pass


def _feed(receiver, chunks):
    try:
        for chunk in chunks:
            if chunk:
                receiver.write(chunk)
    except BaseException:
        receiver.abort()
        raise
    return receiver.close()


class HoldReceiver(Receiver):
    """A copy of another member's backup, kept here."""
    def __init__(self, panel, panel_name, server, filename, size, sha256, keep):
        _check_file(server, filename)
        d = held_dir(panel, server)
        try:
            keep = int(keep)
        except (TypeError, ValueError):
            keep = 0
        self.panel, self.server, self.keep = panel, server, max(0, min(keep, backups.MAX_KEEP))
        self.panel_name = str(panel_name or panel)[:100]
        super().__init__(os.path.join(d, filename), size, sha256)

    def close(self):
        out = super().close()
        os.chmod(self.final, 0o600)
        with open(self.final + SHA_SUFFIX, "w") as f:
            f.write(self.sha)
        tmp = os.path.join(hold_root(), self.panel, PANEL_INFO + ".tmp")
        with open(tmp, "w") as f:
            json.dump({"name": self.panel_name}, f)
        os.replace(tmp, os.path.join(hold_root(), self.panel, PANEL_INFO))
        manager.write_log(f"[BACKUP] Kept a copy of {self.server}'s backup {out['file']} from {self.panel_name} "
                          f"({out['size'] / 1048576:.1f} MB).")
        if self.keep:
            prune_held(self.panel, self.server, self.keep)
        return out


def receive_held(panel, panel_name, server, filename, size, sha256, keep, chunks):
    return _feed(HoldReceiver(panel, panel_name, server, filename, size, sha256, keep), chunks)


def prune_held(panel, server, keep):
    """Scheduled copies beyond keep go, oldest first, as on the source. Manual ones stay."""
    scheduled = [b for b in list_held(panel, server) if b["kind"] == "scheduled"]
    for b in scheduled[keep:]:
        _remove(os.path.join(held_dir(panel, server), b["file"]))
        manager.write_log(f"[BACKUP] Removed old copy {b['file']} of {server} from {b['panel_name']} (keeping {keep}).")


def _remove(path):
    for p in (path, path + SHA_SUFFIX):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass


def delete_held(panel, server, filename):
    path = held_path(panel, server, filename)
    _remove(path)
    manager.write_log(f"[BACKUP] Deleted the copy {filename} of {server} from {_panel_name(panel)}.")
    return {"status": "success"}


def held_sha256(panel, server, filename):
    path = held_path(panel, server, filename)
    sha = _read_sha(path)
    if not sha:
        sha = sha256_file(path)
        with open(path + SHA_SUFFIX, "w") as f:
            f.write(sha)
    return sha


class IntoReceiver(Receiver):
    """A backup copy put into one of this panel's own servers' backups, to restore from there."""
    def __init__(self, server, filename, size, sha256):
        manager._check_server(server)
        _check_file(server, filename)
        super().__init__(os.path.join(backups.server_backup_dir(server), filename), size, sha256)

    def close(self):
        if os.path.exists(self.final):
            self.abort()
            return {"file": os.path.basename(self.final), "size": os.path.getsize(self.final), "existed": True}
        out = super().close()
        os.chmod(self.final, 0o600)
        manager.write_log(f"[BACKUP] Put the backup copy {out['file']} into {os.path.basename(os.path.dirname(self.final))}'s backups.")
        return out


def receive_into(server, filename, size, sha256, chunks):
    return _feed(IntoReceiver(server, filename, size, sha256), chunks)


# ---------------------------------------------------------------------------
# Source: checksums of this panel's own backups
# ---------------------------------------------------------------------------
def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def backup_sha256(server, filename):
    try:
        return sha256_file(backups.backup_path(server, filename))
    except backups.BackupError as e:
        raise CopyError(str(e), 404)


# ---------------------------------------------------------------------------
# The master's two ends: this panel, or a node through its token
# ---------------------------------------------------------------------------
def _read_file(path):
    with open(path, "rb") as f:
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                return
            yield chunk


class Local:
    id = "local"
    panel = "master"

    def __init__(self):
        self.name = panel_settings.display_name()

    def overview(self, server):
        manager._check_server(server)
        return backups.overview(server)

    def sha256(self, server, filename):
        return backup_sha256(server, filename)

    def backup_chunks(self, server, filename):
        return _read_file(backups.backup_path(server, filename))

    def held(self, panel=None, server=None):
        return list_held(panel, server)

    def held_sha256(self, panel, server, filename):
        return held_sha256(panel, server, filename)

    def held_chunks(self, panel, server, filename):
        return _read_file(held_path(panel, server, filename))

    def hold(self, panel, panel_name, server, filename, size, sha256, keep, chunks):
        return receive_held(panel, panel_name, server, filename, size, sha256, keep, chunks)

    def delete_held(self, panel, server, filename):
        return delete_held(panel, server, filename)

    def put_into(self, server, filename, size, sha256, chunks):
        return receive_into(server, filename, size, sha256, chunks)


def _q(text):
    from urllib.parse import quote
    return quote(str(text), safe="")


class Remote:
    def __init__(self, node, actor=ACTOR, role="admin"):
        self.node, self.id, self.name = node, node["id"], node["name"]
        self.panel = node["id"]
        self.who = {"actor": actor, "role": role}

    def _call(self, method, path, body=None, timeout=60):
        return cluster_client.call_node(self.node, method, path, body, timeout=timeout, **self.who)

    def _stream(self, path):
        res = cluster_client.open_proxy(self.node, "GET", path, **self.who)
        if res.status != 200:
            cluster_client._answer(res.status, res.read_all())
            raise cluster_client.NodeError("error", f"HTTP {res.status}")
        return res.chunks()

    def overview(self, server):
        return self._call("GET", f"/api/servers/{_q(server)}/backups", timeout=30)

    def sha256(self, server, filename):
        return self._call("GET", f"/api/servers/{_q(server)}/backups/{_q(filename)}/sha256", timeout=900)["sha256"]

    def backup_chunks(self, server, filename):
        return self._stream(f"/api/servers/{_q(server)}/backups/{_q(filename)}")

    def held(self, panel=None, server=None):
        query = "&".join(f"{k}={_q(v)}" for k, v in (("panel", panel), ("server", server)) if v)
        return self._call("GET", "/api/cluster/held-backups" + (f"?{query}" if query else ""), timeout=30)

    def held_sha256(self, panel, server, filename):
        return self._call("GET", f"/api/cluster/held-backups/{_q(panel)}/{_q(server)}/{_q(filename)}/sha256",
                          timeout=900)["sha256"]

    def held_chunks(self, panel, server, filename):
        return self._stream(f"/api/cluster/held-backups/{_q(panel)}/{_q(server)}/{_q(filename)}")

    def hold(self, panel, panel_name, server, filename, size, sha256, keep, chunks):
        query = f"size={size}&sha256={sha256}&keep={int(keep or 0)}&panel_name={_q(panel_name)}"
        return cluster_client.upload(self.node, f"/api/cluster/held-backups/{_q(panel)}/{_q(server)}/"
                                     f"{_q(filename)}?{query}", chunks, size, **self.who)

    def delete_held(self, panel, server, filename):
        return self._call("DELETE", f"/api/cluster/held-backups/{_q(panel)}/{_q(server)}/{_q(filename)}")

    def put_into(self, server, filename, size, sha256, chunks):
        return cluster_client.upload(self.node, f"/api/cluster/held-backups/into/{_q(server)}/{_q(filename)}"
                                     f"?size={size}&sha256={sha256}", chunks, size, **self.who)


def member(member_id, actor=ACTOR, role="admin"):
    """The Local or Remote end for a member id ('local' is this master). Nodes need 1.16.8."""
    if member_id in ("local", "", None):
        return Local()
    node = cluster.find_node(member_id)
    if not node:
        raise CopyError("No such cluster member.", 404)
    if (node.get("cluster_api") or 1) < COPY_API:
        fields = cluster_client._hello_fields(node)          # it may have been updated since
        if fields:
            cluster.update_node(node["id"], **fields)
            node.update(fields)
    if (node.get("cluster_api") or 1) < COPY_API:
        raise CopyError(f"{node['name']} runs RedstonePanel {node.get('version') or 'older than 1.16.8'}. "
                        "Update it to 1.16.8 or later to keep backup copies on it or send them from it.", 409)
    return Remote(node, actor, role)


def members():
    """Every member a copy can go to: this master first, then the nodes."""
    return [{"id": "local", "name": panel_settings.display_name()}] + \
        [{"id": n["id"], "name": n["name"]} for n in cluster.load_nodes()]


def _member_name(member_id):
    if member_id == "local":
        return panel_settings.display_name()
    node = cluster.find_node(member_id)
    return node["name"] if node else member_id


def why(e):
    if isinstance(e, (cluster_client.NodeError, CopyError, backups.BackupError, manager.ServerError)):
        return str(e)
    return f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# Master: settings and status
# ---------------------------------------------------------------------------
def _key(member_id, server):
    return f"{member_id}/{server}"


def _read(path):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def load_config():
    return {k: v for k, v in _read(CONFIG_FILE).items() if isinstance(v, str)}


def target(member_id, server):
    return load_config().get(_key(member_id or "local", server))


def _check_master():
    if cluster.role() != "master":
        raise CopyError("Backup copies are set on the cluster's master.", 404)


def _check_member_server(member_id, server):
    if not MEMBER_RE.fullmatch(member_id or ""):
        raise CopyError("No such cluster member.", 404)
    if not backups.NAME_RE.fullmatch(server or ""):
        raise CopyError("Bad server name.")


def set_target(member_id, server, to, actor):
    """Keeps copies of member_id's server on `to` (a member id), or stops (to = None)."""
    _check_master()
    member_id = member_id or "local"
    _check_member_server(member_id, server)
    if member_id != "local" and not cluster.find_node(member_id):
        raise CopyError("No such cluster member.", 404)
    if to:
        if to == member_id:
            raise CopyError("Choose another cluster member: a copy on the same machine doesn't protect against "
                            "losing its disk.")
        member(to)                  # exists, and new enough to hold copies
        if member_id != "local":
            member(member_id)       # new enough to give checksums
    with _lock:
        config = load_config()
        if to:
            config[_key(member_id, server)] = to
        else:
            config.pop(_key(member_id, server), None)
        _write(CONFIG_FILE, config)
        if not to:
            status = _read(STATUS_FILE)
            if status.pop(_key(member_id, server), None) is not None:
                _write(STATUS_FILE, status)
    where = _member_name(member_id)
    if to:
        manager.write_log(f"[BACKUP] {actor}: keeping copies of {server}'s backups on {_member_name(to)} "
                          f"(the server is on {where}).")
        sync_soon(member_id, server)
    else:
        manager.write_log(f"[BACKUP] {actor}: no longer copying {server}'s backups (on {where}) to another member. "
                          f"Copies already made stay where they are.")
    return view(member_id, server)


def _status(member_id, server):
    return _read(STATUS_FILE).get(_key(member_id, server))


def _set_status(member_id, server, **fields):
    with _lock:
        data = _read(STATUS_FILE)
        cur = data.get(_key(member_id, server)) or {}
        cur.update(fields)
        data[_key(member_id, server)] = cur
        _write(STATUS_FILE, data)
        return cur


def forget_member(member_id):
    """A node left the cluster: drop the settings of its servers and those that copied to it."""
    with _lock:
        config = load_config()
        keep = {k: v for k, v in config.items() if v != member_id and not k.startswith(f"{member_id}/")}
        if keep != config:
            _write(CONFIG_FILE, keep)
            status = {k: v for k, v in _read(STATUS_FILE).items() if k in keep}
            _write(STATUS_FILE, status)


# ---------------------------------------------------------------------------
# Master: copying
# ---------------------------------------------------------------------------
def _wanted(backup_list, keep):
    """The source backups that should have a copy: every manual one, the newest `keep` scheduled."""
    scheduled = [b for b in backup_list if b["kind"] == "scheduled"][:keep]
    return [b for b in backup_list if b["kind"] in COPY_KINDS and (b["kind"] != "scheduled" or b in scheduled)]


def sync(member_id, server, actor=ACTOR):
    """Copies what's missing of member_id's server's backups to its holder. Returns
    {"copied": [files], "to": name}. Records the result; raises CopyError on failure."""
    member_id = member_id or "local"
    to = target(member_id, server)
    if not to:
        raise CopyError(f"Copies of {server}'s backups aren't kept on another member.")
    key = _key(member_id, server)
    with _lock:
        if key in _running:
            raise CopyError(f"{server}'s backups are being copied already.", 409)
        _running.add(key)
    started = time.time()
    copied = []
    try:
        src, dst = member(member_id, actor), member(to, actor)
        info = src.overview(server)
        keep = int((info.get("schedule") or {}).get("keep") or backups.DEFAULT_SCHEDULE["keep"])
        have = {b["file"] for b in dst.held(src.panel, server)}
        for b in sorted(_wanted(info.get("backups") or [], keep), key=lambda x: x["created"]):
            if b["file"] in have:
                continue
            sha = src.sha256(server, b["file"])
            dst.hold(src.panel, src.name, server, b["file"], b["size"], sha, keep,
                     src.backup_chunks(server, b["file"]))
            copied.append(b["file"])
    except Exception as e:
        msg = why(e)
        prev = _status(member_id, server) or {}
        _set_status(member_id, server, ok=False, at=int(started), error=msg, to=to,
                    failing_since=prev.get("failing_since") if prev.get("ok") is False else int(started),
                    copied=len(copied))
        name = _member_name(to)
        manager.write_log(f"[BACKUP] Copying {server}'s backups to {name} failed: {msg}")
        if prev.get("ok") is not False:         # once per failure, not every few minutes
            notify.send("backup_copy_failed", server, f"Copying {server}'s backups to {name} failed: {msg}")
        raise CopyError(f"Copying to {name} failed: {msg}", 502)
    finally:
        with _lock:
            _running.discard(key)
    prev = _status(member_id, server) or {}
    fields = {"ok": True, "at": int(started), "error": None, "failing_since": None, "to": to}
    if copied:
        fields.update(last_copied=copied[-1], last_copied_at=int(time.time()))
    _set_status(member_id, server, **fields)
    if prev.get("ok") is False:
        manager.write_log(f"[BACKUP] Copying {server}'s backups to {_member_name(to)} works again.")
    if copied:
        manager.write_log(f"[BACKUP] Copied {len(copied)} backup{'s' if len(copied) != 1 else ''} of {server} "
                          f"to {_member_name(to)}.")
    return {"copied": copied, "to": _member_name(to)}


def sync_soon(member_id, server, actor=ACTOR):
    """sync() in a background thread (Back up now and turning copies on don't wait for it)."""
    def run():
        try:
            sync(member_id, server, actor)
        except CopyError:
            pass                                # recorded and logged by sync()
        except Exception as e:
            manager.write_log(f"[BACKUP] Copying {server}'s backups failed: {why(e)}")
    threading.Thread(target=run, daemon=True, name=f"backup-copy-{server}").start()


def sweep():
    """One pass on the master: every server with copies turned on."""
    if cluster.role() != "master":
        return
    for key, to in sorted(load_config().items()):
        member_id, _, server = key.partition("/")
        try:
            sync(member_id, server)
        except CopyError:
            pass
        except Exception as e:
            manager.write_log(f"[BACKUP] Copying {server}'s backups failed: {why(e)}")


def sweeper(every=SWEEP_EVERY, first=FIRST_SWEEP):
    """Runs sweep() in a daemon thread: shortly after start, then every few minutes."""
    def loop():
        time.sleep(first)
        while True:
            try:
                sweep()
            except Exception as e:             # never let this take the panel down
                manager.write_log(f"[BACKUP] Backup copies failed: {why(e)}")
            time.sleep(every)
    t = threading.Thread(target=loop, name="backup-copies", daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------
# Master: what the Backups tab and the dashboard show
# ---------------------------------------------------------------------------
def _gather(fn):
    """fn(end) on every member that can hold copies, in parallel: [(end, result or None, error)]."""
    ends = []
    for m in members():
        try:
            ends.append((member(m["id"]), None))
        except (CopyError, cluster_client.NodeError) as e:
            ends.append((m, why(e)))

    def one(pair):
        end, err = pair
        if err:
            return end, None, err
        try:
            return end, fn(end), None
        except Exception as e:
            return end, None, why(e)
    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(one, ends))


def _name_of(end):
    return end["name"] if isinstance(end, dict) else end.name


def _id_of(end):
    return end["id"] if isinstance(end, dict) else end.id


def copies(member_id=None, server=None):
    """Copies kept anywhere in the cluster: all of them, or those of one member's server.
    Returns (copies, members that didn't answer)."""
    panel = None
    if member_id:
        panel = "master" if member_id == "local" else member_id
    out, missing = [], []
    for end, held, err in _gather(lambda e: e.held(panel, server)):
        if err:
            missing.append({"id": _id_of(end), "name": _name_of(end), "error": err})
            continue
        for c in held if isinstance(held, list) else []:
            out.append({**c, "holder": _id_of(end), "holder_name": _name_of(end)})
    out.sort(key=lambda c: (c["created"], c["file"]), reverse=True)
    return out, missing


def view(member_id, server):
    """The Backups tab's copy section for member_id's server."""
    member_id = member_id or "local"
    _check_member_server(member_id, server)
    if cluster.role() != "master":
        return {"available": False, "role": cluster.role()}
    found, missing = copies(member_id, server)
    to = target(member_id, server)
    return {"available": True, "role": "master", "to": to, "to_name": _member_name(to) if to else None,
            "members": [m for m in members() if m["id"] != member_id],
            "status": _status(member_id, server), "copies": found, "unreachable": missing}


def attention():
    """Needs attention items on the master's dashboard: copies that keep failing."""
    if cluster.role() != "master":
        return []
    out = []
    config = load_config()
    for key, st in _read(STATUS_FILE).items():
        if key not in config or not isinstance(st, dict) or st.get("ok") is not False:
            continue
        member_id, _, server = key.partition("/")
        where = "" if member_id == "local" else f" on {_member_name(member_id)}"
        out.append({"level": "warn", "server": server if member_id == "local" else None, "where": "backups",
                    "node": None if member_id == "local" else member_id,
                    "text": f"Copying the backups of {server}{where} to {_member_name(config[key])} failed: "
                            f"{st.get('error') or 'unknown error'}"})
    return out


# ---------------------------------------------------------------------------
# Master: downloading a copy, putting it into a server, deleting it
# ---------------------------------------------------------------------------
def download(holder, panel, server, filename, actor, role):
    """(chunks, size) of a copy kept on `holder`."""
    _check_master()
    _check_panel(panel)
    _check_file(server, filename)
    end = member(holder, actor, role)
    found = [c for c in end.held(panel, server) if c["file"] == filename]
    if not found:
        raise CopyError("No such backup copy.", 404)
    return end.held_chunks(panel, server, filename), found[0]["size"]


def put_into(holder, panel, server, filename, member_id, target_server, actor):
    """Copies a kept copy into target_server's own backups on member_id, so it can be restored
    there like any other backup. Returns the file name it has there."""
    _check_master()
    _check_panel(panel)
    m = _check_file(server, filename)
    member_id = member_id or "local"
    _check_member_server(member_id, target_server)
    src = member(holder, actor)
    found = [c for c in src.held(panel, server) if c["file"] == filename]
    if not found:
        raise CopyError("No such backup copy.", 404)
    dst = member(member_id, actor)
    new_name = f"{target_server}-{m.group('stamp')}-{m.group('kind')}.tar.gz"
    sha = src.held_sha256(panel, server, filename)
    result = dst.put_into(target_server, new_name, found[0]["size"], sha, src.held_chunks(panel, server, filename))
    manager.write_log(f"[BACKUP] {actor}: put the copy {filename} (kept on {src.name}) into {target_server}'s "
                      f"backups on {dst.name} as {new_name}.")
    return {"file": new_name, "existed": bool(result.get("existed"))}


def remove(holder, panel, server, filename, actor):
    _check_master()
    end = member(holder, actor)
    end.delete_held(panel, server, filename)
    manager.write_log(f"[BACKUP] {actor}: deleted the copy {filename} of {server} kept on {end.name}.")
    return {"status": "success"}
