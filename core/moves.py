"""Moving and copying servers between cluster members (1.16): the master's side.

A move runs in a background job: the source (A) packs the server, the master streams the
archive from A to the target (B), B installs it, then A takes its copy off the panel (keeping
the files). Either member may be the master itself, which then calls core/transfer.py directly.
See docs/design/move.md.
"""
import threading
import time
import uuid

from core import cluster, cluster_client, manager, transfer
from core import settings as panel_settings

MOVE_API = 3                    # a node needs this cluster contract to move servers
PRECOPY_API = 5                 # ... and this one for a move with a pre-copy (1.16.13)
LONG = 1800                     # packing or installing a big server can take a while
CHUNK = 1 << 16
KEEP_JOBS = 20

_lock = threading.Lock()
_jobs = {}                      # id -> job (newest last)


class MoveError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# The two ends: this panel, or a node through its token
# ---------------------------------------------------------------------------
class Local:
    id = "local"

    def __init__(self):
        self.name = panel_settings.display_name()

    def export(self, server, stop, precopy=False, base=None):
        return transfer.export(server, stop, precopy, base)

    def chunks(self, tid):
        path, _ = transfer.open_archive(tid)
        with open(path, "rb") as f:
            while True:
                chunk = f.read(CHUNK)
                if not chunk:
                    return
                yield chunk

    def finish(self, tid, outcome, moved_to=None):
        return transfer.finish(tid, outcome, moved_to)

    def begin_import(self, size, sha256, meta, base=None):
        return transfer.begin_import(size, sha256, meta, base)

    def upload(self, tid, chunks, size):
        return transfer.receive(tid, chunks)

    def stage(self, tid):
        return transfer.stage(tid)

    def install(self, tid, keep_name, start, carry, actor):
        return transfer.install(tid, keep_name, start, carry, actor)

    def confirm(self, tid):
        return transfer.confirm(tid)

    def abort(self, tid):
        return transfer.abort(tid)

    def delete_moved(self, name):
        return transfer.delete_moved(name)


class Remote:
    def __init__(self, node, actor, role):
        self.node, self.id, self.name = node, node["id"], node["name"]
        self.who = {"actor": actor, "role": role}

    def _call(self, method, path, body=None, timeout=60):
        return cluster_client.call_node(self.node, method, path, body, timeout=timeout, **self.who)

    def export(self, server, stop, precopy=False, base=None):
        body = {"server": server, "stop": stop}
        if precopy or base:
            body.update(precopy=precopy, base=base)
        return self._call("POST", "/api/cluster/transfers/export", body, LONG)

    def chunks(self, tid):
        res = cluster_client.open_proxy(self.node, "GET", f"/api/cluster/transfers/{tid}/archive", **self.who)
        if res.status != 200:
            cluster_client._answer(res.status, res.read_all())
            raise cluster_client.NodeError("error", f"HTTP {res.status}")
        return res.chunks()

    def finish(self, tid, outcome, moved_to=None):
        return self._call("POST", f"/api/cluster/transfers/{tid}/finish", {"outcome": outcome, "moved_to": moved_to},
                          LONG)

    def begin_import(self, size, sha256, meta, base=None):
        body = {"size": size, "sha256": sha256, "meta": meta}
        if base:
            body["base"] = base
        return self._call("POST", "/api/cluster/transfers/import", body)

    def upload(self, tid, chunks, size):
        return cluster_client.upload(self.node, f"/api/cluster/transfers/{tid}/archive", chunks, size, **self.who)

    def stage(self, tid):
        return self._call("POST", f"/api/cluster/transfers/{tid}/stage", timeout=LONG)

    def install(self, tid, keep_name, start, carry, actor):
        return self._call("POST", f"/api/cluster/transfers/{tid}/install",
                          {"keep_name": keep_name, "start": start, "carry": carry}, LONG + 600)

    def confirm(self, tid):
        return self._call("POST", f"/api/cluster/transfers/{tid}/confirm")

    def abort(self, tid):
        return self._call("DELETE", f"/api/cluster/transfers/{tid}", timeout=LONG)

    def delete_moved(self, name):
        return self._call("DELETE", f"/api/cluster/transfers/moved/{name}", timeout=LONG)


def member(member_id, actor, role, need=MOVE_API):
    """The Local or Remote end for a member id ('local' is this master)."""
    if member_id in ("local", "", None):
        return Local()
    node = cluster.find_node(member_id)
    if not node:
        raise MoveError("No such cluster member.", 404)
    if (node.get("cluster_api") or 1) < need:
        fields = cluster_client._hello_fields(node)          # it may have been updated since
        if fields:
            cluster.update_node(node["id"], **fields)
            node.update(fields)
    if (node.get("cluster_api") or 1) < MOVE_API:
        raise MoveError(f"{node['name']} runs RedstonePanel {node.get('version') or 'older than 1.16'}. "
                        "Update it to 1.16 or later to move servers to or from it.", 409)
    if (node.get("cluster_api") or 1) < need:
        raise MoveError(f"{node['name']} runs RedstonePanel {node.get('version') or 'older than 1.16.13'}. "
                        "Update it to 1.16.13 or later for a pre-copy, or move without one.", 409)
    return Remote(node, actor, role)


def _why(e):
    if isinstance(e, cluster_client.NodeError):
        return str(e)
    if isinstance(e, (transfer.TransferError, manager.ServerError, manager.DependencyError, MoveError)):
        return str(e)
    return f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------
def _public(job):
    return {k: v for k, v in job.items() if not k.startswith("_")}


def jobs():
    with _lock:
        return [_public(j) for j in _jobs.values()][::-1]


def get(job_id):
    with _lock:
        j = _jobs.get(job_id)
        return _public(j) if j else None


def _set(job, **fields):
    with _lock:
        job.update(fields)


def start(kind, src_id, server, dst_id, start_copy=False, actor="?", role="admin", precopy=False):
    """Starts moving or copying `server` from member src_id to dst_id. Returns the job.
    precopy (moves only): copy it while it runs first, so it's only down while what changed
    since is copied."""
    if kind not in ("move", "copy"):
        raise MoveError("Unknown kind: move or copy.")
    if precopy and kind != "move":
        raise MoveError("A pre-copy is for moves: a copy doesn't stop the server anyway.")
    if not manager.SERVICE_NAME_RE.match(server or ""):
        raise MoveError("Bad server name.")
    src_id, dst_id = src_id or "local", dst_id or "local"
    if kind == "move" and src_id == dst_id:
        raise MoveError("Choose another cluster member to move it to.")
    need = PRECOPY_API if precopy else MOVE_API
    a, b = member(src_id, actor, role, need), member(dst_id, actor, role, need)
    with _lock:
        if any(j["state"] == "running" and j["from"]["id"] == a.id and j["server"] == server for j in _jobs.values()):
            raise MoveError(f"{server} is already being moved or copied.", 409)
        job = {"id": uuid.uuid4().hex[:12], "kind": kind, "server": server, "actor": actor,
               "from": {"id": a.id, "name": a.name}, "to": {"id": b.id, "name": b.name},
               "state": "running", "step": "Starting", "bytes": 0, "total": None,
               "started": int(time.time()), "finished": None, "error": None, "warning": None, "result": None,
               "precopy": bool(precopy), "downtime": None}
        _jobs[job["id"]] = job
        for old in [k for k, j in _jobs.items() if j["state"] != "running"][:-KEEP_JOBS]:
            _jobs.pop(old, None)
    manager.write_log(f"[MOVE] {actor}: {'moving' if kind == 'move' else 'copying'} {server} from {a.name} to {b.name}.")
    threading.Thread(target=_run, args=(job, a, b, bool(start_copy)), daemon=True, name=f"move-{job['id']}").start()
    return _public(job)


def _counted(job, chunks):
    for chunk in chunks:
        with _lock:
            job["bytes"] += len(chunk)
        yield chunk


def _precopy(job, a, b, server):
    """Sends the server while it runs and has B unpack it. Returns (A's id, B's id)."""
    pre = pimp = None
    try:
        _set(job, step=f"Pre-copy: packing {server} on {a.name} while it runs")
        pre = a.export(server, False, precopy=True)
        _set(job, total=pre["size"], bytes=0, step=f"Checking space on {b.name}")
        pimp = b.begin_import(pre["size"], pre["sha256"], pre["meta"])
        _set(job, step=f"Pre-copy: copying to {b.name} while {server} keeps running")
        b.upload(pimp["id"], _counted(job, a.chunks(pre["id"])), pre["size"])
        _set(job, step=f"Pre-copy: unpacking on {b.name}")
        b.stage(pimp["id"])
        return pre, pimp
    except Exception:
        job["_pre"] = (pre, pimp)               # for the clean-up in _run
        raise


def _run(job, a, b, start_copy):
    move, server = job["kind"] == "move", job["server"]
    exp = imp = None
    pre = pimp = None
    down = None
    try:
        if job.get("precopy"):
            pre, pimp = _precopy(job, a, b, server)
            down = time.time()
            _set(job, bytes=0, total=None, step=f"Stopping {server} on {a.name} and packing what changed")
            exp = a.export(server, True, base=pre["id"])
            pre = None                          # A dropped it once the changes were packed
            _set(job, total=exp["size"], step=f"Checking space on {b.name}")
            imp = b.begin_import(exp["size"], exp["sha256"], exp["meta"], base=pimp["id"])
            pimp = None                         # B now drops it with imp
            _set(job, step=f"Copying what changed to {b.name}")
        else:
            _set(job, step=f"Stopping and packing {server} on {a.name}" if move else f"Packing {server} on {a.name}")
            down = time.time()
            exp = a.export(server, move)
            _set(job, total=exp["size"], step=f"Checking space on {b.name}")
            imp = b.begin_import(exp["size"], exp["sha256"], exp["meta"])
            _set(job, step=f"Copying to {b.name}")
        b.upload(imp["id"], _counted(job, a.chunks(exp["id"])), exp["size"])
        _set(job, step=f"Installing on {b.name}")
        result = b.install(imp["id"], keep_name=a.id != b.id, start=exp["was_running"] if move else start_copy,
                           carry=move, actor=job["actor"])
        if move and exp["was_running"]:
            _set(job, downtime=max(0, int(round(time.time() - down - (result.get("checked_for") or 0)))))
    except Exception as e:
        why = _why(e)
        manager.write_log(f"[MOVE] {'Moving' if move else 'Copying'} {server} to {b.name} failed: {why}")
        pre, pimp = job.pop("_pre", (pre, pimp))
        for end, tid, call in ((b, imp, "abort"), (b, pimp, "abort"), (a, exp, "finish"), (a, pre, "copied")):
            if not tid:
                continue
            try:
                if call == "copied":
                    end.finish(tid["id"], "copied")     # a pre-copy never stopped anything
                    continue
                getattr(end, call)(tid["id"], *(("failed",) if call == "finish" else ()))
            except Exception as e2:
                manager.write_log(f"[MOVE] Cleaning up on {end.name} failed: {_why(e2)}")
        _set(job, state="failed", error=why, finished=int(time.time()),
             step="Nothing was changed" + (f"; {server} runs on {a.name} again." if move and exp and exp["was_running"]
                                            else "."))
        return
    warning = None
    try:
        if move:
            _set(job, step=f"Taking {server} off {a.name}")
            a.finish(exp["id"], "moved", moved_to=f"{result['name']} on {b.name}")
        else:
            a.finish(exp["id"], "copied")
    except Exception as e:
        warning = (f"{result['name']} runs on {b.name}, but {a.name} couldn't take its copy off the panel: {_why(e)}. "
                   f"It's stopped there; uninstall it on {a.name} yourself.") if move else None
        manager.write_log(f"[MOVE] {warning or _why(e)}")
    try:
        b.confirm(imp["id"])
    except Exception as e:
        manager.write_log(f"[MOVE] Confirming on {b.name} failed (it keeps the server anyway): {_why(e)}")
    cluster_client.forget_cached()
    if move:
        # 1.18: what `user` accounts may do on the server follows it to its new machine and name
        try:
            from core import grants
            grants.rename_server(grants.server_key(server, a.id), grants.server_key(result["name"], b.id))
        except Exception as e:
            manager.write_log(f"[MOVE] Couldn't carry over the accounts' permissions on {server}: {_why(e)}")
    manager.write_log(f"[MOVE] {'Moved' if move else 'Copied'} {server} from {a.name} to {b.name} as "
                      f"{result['name']} (port {result['port']}).")
    _set(job, state="done", step="Done", finished=int(time.time()), warning=warning, result=result)
