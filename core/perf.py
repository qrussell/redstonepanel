"""Server performance and tuning (1.17).

  * Tick health over RCON: Paper's `tps` and `mspt`; Vanilla and Fabric 1.20.3+ `tick query`.
    Older Java releases and Bedrock have no command for it, so the answer is "not available"
    rather than a guess. Only read-only commands are sent.
  * History: a background sampler writes every 60 s to a small fixed-size ring file per server
    and one for the host under /var/lib/redstonepanel/metrics/: 24 hours at one minute and
    7 days at 15 minutes. No database, no psutil.
  * Memory (the Java heap, -Xms/-Xmx in the unit) and optional hard limits (systemd
    MemoryMax= and CPUQuota= in a drop-in next to the unit, so rewriting the unit keeps them).
  * Suggestions, each shown only when what it's about was measured, and two webhook alerts
    (TPS under 15 for 5 minutes, disk over 95 %), off by default.
"""
import json
import math
import os
import re
import struct
import subprocess    # noqa: F401  (unused here since 1.21; tests replace subprocess.run through this module)
import threading
import time

from core import backups, host, javaservers, manager, notify
from core.settings import DATA_DIR
from core.platform import paths, services

METRICS_DIR = os.path.join(DATA_DIR, "metrics")
LIMITS_FILE = os.path.join(DATA_DIR, "limits.json")
UNIT_DIR = paths.UNIT_DIR
CGROUP_ROOT = paths.CGROUP_ROOT
DROPIN_NAME = "redstonepanel-limits.conf"
RUNTIME_CONTROL = paths.RUNTIME_CONTROL   # where `systemctl set-property --runtime` writes
RUNTIME_FILES = ("50-MemoryMax.conf", "50-CPUQuota.conf")
NAME_RE = re.compile(r"^minecraft[A-Za-z0-9_.-]*$")

SAMPLE_EVERY = 60
FIRST_SAMPLE = 20
TICK_TTL = 25                 # tick health is polled at most every 25-30 s per server
SYSTEM_RESERVE = 1024 ** 3    # all heaps together must leave this much for the system...
SMALL_RESERVE = 768 * 1024 ** 2   # ...or this on a machine with 3 GB or less (a 2 GB Raspberry Pi
SMALL_HOST = 3 * 1024 ** 3        # reports about 1.8 GB, and its default 1 GB heap must still fit)
MB_ = 1024 ** 2
MIN_HEAP = 256 * 1024 ** 2
LIMIT_HEADROOM = 256 * 1024 ** 2   # a hard limit must be at least the heap plus this
MIN_LIMIT = 256 * 1024 ** 2

# Ring files: (step in seconds, slots). 24 h at 1 min, 7 days at 15 min.
TIERS = ((60, 1440), (900, 672))
RANGES = {"day": 0, "week": 1}
SERVER_FIELDS = ("cpu", "memory", "players", "tps", "mspt")
HOST_FIELDS = ("cpu", "memory", "disk", "rx", "tx")
MAGIC = b"RPM1"
HEADER = 8

TPS_WARN = 18                 # suggestion: under this for 10 minutes
TPS_ALERT = 15                # webhook: under this for 5 minutes
DISK_ALERT = 95
BIG_WORLD = 4 * 1024 ** 3
BIG_LOGS = 1024 ** 3
NO_BORDER = 1_000_000         # wider than this counts as "no world border" (default 59,999,968)


class PerfError(ValueError):
    """A memory or limit setting that can't be saved (message is safe to show)."""


_lock = threading.Lock()
_ticks = {}                   # server -> (t, tick health)
_latest = {}                  # server -> what the sampler saw last
_host_latest = {}
_sampler = None


# ---------------------------------------------------------------------------
# Sizes
# ---------------------------------------------------------------------------
UNITS = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}


def to_bytes(size):
    """'512M' or '2G' (as in -Xmx) to bytes, or None."""
    m = re.fullmatch(r"\s*([0-9]{1,6})\s*([KkMmGg])\s*", str(size or ""))
    return int(m.group(1)) * UNITS[m.group(2).lower()] if m else None


def heap_text(n):
    """Bytes as -Xmx would take them: whole gigabytes as G, otherwise M."""
    if n % 1024 ** 3 == 0:
        return f"{n // 1024 ** 3}G"
    return f"{max(1, n // 1024 ** 2)}M"


def system_reserve(total):
    """Memory the Java heaps must leave for the system and the panel."""
    return SMALL_RESERVE if 0 < total <= SMALL_HOST else SYSTEM_RESERVE


def _reserve_text(total):
    return "768 MB" if system_reserve(total) == SMALL_RESERVE else "1 GB"


def auto_limit(xmx_bytes):
    """The hard memory limit "Automatic" means: the heap plus half again (at least 512 MB) for
    what Java uses outside the heap (code, threads, buffers), so a healthy server never reaches it."""
    return xmx_bytes + max(512 * 1024 ** 2, xmx_bytes // 2)


# ---------------------------------------------------------------------------
# Tick health (pure parsers first, so the tests can feed them text)
# ---------------------------------------------------------------------------
COLOUR_RE = re.compile(r"§.")
NUM = r"([0-9]+(?:\.[0-9]+)?)"


def _clean(text):
    return COLOUR_RE.sub("", text or "").replace("*", "")


def parse_paper_tps(text):
    """Paper's `tps`: 'TPS from last 1m, 5m, 15m: 20.0, 19.98, 20.0' -> [1m, 5m, 15m] or None."""
    m = re.search(r"TPS from last 1m, 5m, 15m:\s*" + NUM + r",\s*" + NUM + r",\s*" + NUM, _clean(text))
    return [float(m.group(i)) for i in (1, 2, 3)] if m else None


def parse_paper_mspt(text):
    """Paper's `mspt`: 'Server tick times (avg/min/max) from last 5s, 10s, 1m: ◴ 2.1/1.0/5.3, ...'
    -> {"avg", "min", "max"} of the last 5 s, or None."""
    clean = _clean(text)
    i = clean.find("from last")
    m = re.search(NUM + "/" + NUM + "/" + NUM, clean[i:] if i >= 0 else clean)
    return {"avg": float(m.group(1)), "min": float(m.group(2)), "max": float(m.group(3))} if m else None


def parse_tick_query(text):
    """Vanilla 1.20.3+ `tick query` -> {"mspt", "p50", "p95", "p99", "rate", "state"} or None."""
    clean = _clean(text)
    avg = re.search(r"Average time per tick:\s*" + NUM + r"\s*ms", clean)
    if not avg:
        return None
    out = {"mspt": float(avg.group(1))}
    for p in ("P50", "P95", "P99"):
        m = re.search(p + r":\s*" + NUM + r"\s*ms", clean)
        out[p.lower()] = float(m.group(1)) if m else None
    rate = re.search(r"Target tick rate:\s*" + NUM, clean)
    out["rate"] = float(rate.group(1)) if rate else 20.0
    low = clean.lower()
    out["state"] = "frozen" if "frozen" in low else "sprinting" if "sprint" in low else "normal"
    return out


def tps_from_mspt(mspt, rate=20.0):
    """TPS from milliseconds per tick: min(rate, 1000 / ms)."""
    if mspt is None or mspt <= 0:
        return rate
    return round(min(rate, 1000.0 / mspt), 2)


def _round(v, n=2):
    return None if v is None else round(v, n)


def query_tick(call, loader):
    """Tick health from an RCON call. Paper (and its forks) answer `tps`/`mspt`; everything
    else is asked `tick query`, which Paper 1.20.3+ also understands."""
    if loader in ("paper", "purpur", "folia"):
        tps = parse_paper_tps(call("tps", timeout=3) or "")
        if tps:
            mspt = parse_paper_mspt(call("mspt", timeout=3) or "")
            return {"available": True, "source": "tps", "tps": tps[0], "tps_5m": tps[1], "tps_15m": tps[2],
                    "mspt": mspt["avg"] if mspt else None, "mspt_max": mspt["max"] if mspt else None}
    q = parse_tick_query(call("tick query", timeout=3) or "")
    if q:
        return {"available": True, "source": "tick query", "tps": tps_from_mspt(q["mspt"], q["rate"]),
                "mspt": _round(q["mspt"]), "p95": q["p95"], "p99": q["p99"], "state": q["state"]}
    return {"available": False, "why": "This Minecraft version has no command that reports it (Java 1.20.3 and newer do)."}


def tick_health(server, srv):
    """Tick health of a server from list_servers(), cached for TICK_TTL seconds."""
    if srv.get("edition") != "Java":
        return {"available": False, "why": "Bedrock servers have no command that reports it."}
    if srv.get("status") != "online":
        return {"available": False, "why": "The server isn't running."}
    now = time.time()
    with _lock:
        hit = _ticks.get(server)
        if hit and now - hit[0] < TICK_TTL:
            return hit[1]
    call = backups._java_rcon(srv["directory"])
    if not call:
        out = {"available": False, "why": "RCON is off. Turn it on in Live Chat to see tick health."}
    else:
        try:
            out = query_tick(call, srv.get("loader") or "vanilla")
        except Exception:
            out = {"available": False, "why": "The server didn't answer over RCON (it may still be starting)."}
    with _lock:
        _ticks[server] = (time.time(), out)
    return out


# ---------------------------------------------------------------------------
# Ring files
# ---------------------------------------------------------------------------
class Ring:
    """A fixed-size history file: for each tier, one slot per step, overwritten in a circle.
    A slot holds its time and float32 values (NaN = not measured). Old slots are recognised by
    their time, so there is no head pointer to get wrong after a crash."""

    def __init__(self, path, nfields):
        self.path, self.n = path, nfields
        self.rec = struct.Struct("<I" + "f" * nfields)
        self.size = HEADER + sum(slots for _, slots in TIERS) * self.rec.size

    def _offset(self, tier, slot):
        before = sum(s for _, s in TIERS[:tier])
        return HEADER + (before + slot) * self.rec.size

    def _open(self):
        """The file opened for update, made (or remade, if it isn't one of ours) at full size."""
        try:
            f = open(self.path, "r+b")
            head = f.read(HEADER)
            if head[:4] == MAGIC and head[4] == self.n and os.fstat(f.fileno()).st_size == self.size:
                return f
            f.close()
        except OSError:
            pass
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600)
        f = os.fdopen(fd, "r+b")
        f.write(MAGIC + bytes([self.n, 0, 0, 0]))
        f.truncate(self.size)
        return f

    def _read_tier(self, f, tier):
        step, slots = TIERS[tier]
        f.seek(self._offset(tier, 0))
        data = f.read(slots * self.rec.size)
        out = []
        for i in range(len(data) // self.rec.size):
            t, *vals = self.rec.unpack_from(data, i * self.rec.size)
            if t:
                out.append((t, vals))
        return out

    def write(self, t, values):
        t = int(t)
        vals = [float("nan") if v is None else float(v) for v in values]
        with _lock, self._open() as f:
            step, slots = TIERS[0]
            t0 = t - t % step
            f.seek(self._offset(0, (t0 // step) % slots))
            f.write(self.rec.pack(t0, *vals))
            # The coarser tiers hold the average of the finer samples in their bucket
            for tier in range(1, len(TIERS)):
                bstep, bslots = TIERS[tier]
                b0 = t - t % bstep
                inside = [v for ts, v in self._read_tier(f, tier - 1) if b0 <= ts < b0 + bstep]
                avg = []
                for i in range(self.n):
                    xs = [v[i] for v in inside if not math.isnan(v[i])]
                    avg.append(sum(xs) / len(xs) if xs else float("nan"))
                f.seek(self._offset(tier, (b0 // bstep) % bslots))
                f.write(self.rec.pack(b0, *avg))

    def read(self, tier=0, now=None):
        """[(time, [values with None for not measured])] of one tier, oldest first."""
        now = int(now or time.time())
        step, slots = TIERS[tier]
        try:
            with _lock, open(self.path, "rb") as f:
                head = f.read(HEADER)
                if head[:4] != MAGIC or head[4] != self.n:
                    return []
                rows = self._read_tier(f, tier)
        except OSError:
            return []
        start = now - step * slots
        rows = [(t, [None if math.isnan(v) else round(v, 3) for v in vals])
                for t, vals in rows if start < t <= now]
        return sorted(rows)


def server_ring(server):
    if not NAME_RE.fullmatch(server or ""):
        raise PerfError("Invalid server name.")
    return Ring(os.path.join(METRICS_DIR, f"{server}.ring"), len(SERVER_FIELDS))


def host_ring():
    return Ring(os.path.join(METRICS_DIR, "host.ring"), len(HOST_FIELDS))


def history(ring, fields, rng="day"):
    tier = RANGES.get(rng)
    if tier is None:
        raise PerfError("The range is 'day' or 'week'.")
    return {"range": rng, "step": TIERS[tier][0], "fields": list(fields), "points": [[t] + v for t, v in ring.read(tier)]}


def host_history(rng="day"):
    return history(host_ring(), HOST_FIELDS, rng)


# ---------------------------------------------------------------------------
# Small readers
# ---------------------------------------------------------------------------
def parse_proc_net_tcp(text, port):
    """Established connections whose local port is `port`, from /proc/net/tcp or tcp6."""
    n = 0
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 4 or parts[3] != "01":
            continue
        try:
            if int(parts[1].rsplit(":", 1)[1], 16) == port:
                n += 1
        except (ValueError, IndexError):
            continue
    return n


def connections(port):
    try:
        port = int(port)
    except (TypeError, ValueError):
        return None
    return sum(parse_proc_net_tcp(host._read(os.path.join(host.PROC, f"net/{p}")), port) for p in ("tcp", "tcp6"))


def parse_kv(text):
    out = {}
    for line in (text or "").splitlines():
        k, _, v = line.partition(" ")
        if v.strip().isdigit():
            out[k] = int(v)
    return out


def cgroup_stats(server):
    """CPU throttling and memory-limit counters of a running server (the cgroup's on Linux, the
    Job Object's on Windows)."""
    return services.limit_counters(server)


def addon_count(d, loader):
    folder = "plugins" if loader in ("paper", "purpur", "folia") else "mods" if loader in ("fabric", "quilt", "forge", "neoforge") else None
    if not folder:
        return None, None
    try:
        names = os.listdir(os.path.join(d, folder))
    except OSError:
        return folder, 0
    return folder, sum(1 for n in names if n.lower().endswith(".jar"))


def _addon_names(d, folder):
    try:
        return [n.lower() for n in os.listdir(os.path.join(d, folder)) if n.lower().endswith(".jar")]
    except OSError:
        return []


def rotational(path):
    """True if the file system holding path is on a spinning disk, None if unknown."""
    if not hasattr(os, "major"):        # Windows: no device numbers, so not known
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    base = f"{paths.BLOCK_DEVICES}/{os.major(st.st_dev)}:{os.minor(st.st_dev)}"
    for p in (os.path.join(base, "queue/rotational"), os.path.join(base, "../queue/rotational")):
        v = host._read(p).strip()
        if v in ("0", "1"):
            return v == "1"
    return None


# ---------------------------------------------------------------------------
# Memory and limits
# ---------------------------------------------------------------------------
def _load_limits():
    try:
        with open(LIMITS_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_limits(data):
    os.makedirs(os.path.dirname(LIMITS_FILE), exist_ok=True)
    tmp = LIMITS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, LIMITS_FILE)


def get_limits(server):
    """{"memory": "off" | "auto" | bytes, "cpu": None | cores (float)}"""
    stored = _load_limits().get(server)
    out = {"memory": "off", "cpu": None}
    if isinstance(stored, dict):
        mem = stored.get("memory")
        if mem in ("off", "auto") or (isinstance(mem, int) and mem >= MIN_LIMIT):
            out["memory"] = mem
        cpu = stored.get("cpu")
        if isinstance(cpu, (int, float)) and 0 < cpu <= 1024:
            out["cpu"] = float(cpu)
    return out


def _dropin_path(server):
    return os.path.join(UNIT_DIR, f"{server}.service.d", DROPIN_NAME)


def _memory_max(limits, xmx):
    if limits["memory"] == "auto":
        return auto_limit(xmx) if xmx else None
    return limits["memory"] if isinstance(limits["memory"], int) else None


def dropin_text(memory_max, cpu):
    lines = ["# Written by RedstonePanel (Maintenance > Memory and limits). Changes here are overwritten.",
             "[Service]"]
    if memory_max:
        lines.append(f"MemoryMax={memory_max}")
    if cpu:
        lines.append(f"CPUQuota={int(round(cpu * 100))}%")
    return "\n".join(lines) + "\n"


def _apply_limits(server, limits, xmx, running):
    """Writes (or removes) the drop-in and, for a running server, sets the same limits on its
    cgroup now, so they apply without a restart."""
    memory_max = _memory_max(limits, xmx)
    path = _dropin_path(server)
    if memory_max or limits["cpu"]:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write(dropin_text(memory_max, limits["cpu"]))
        os.replace(tmp, path)
    else:
        try:
            os.remove(path)
            os.rmdir(os.path.dirname(path))
        except OSError:
            pass
    if running:
        services.set_runtime_limits(server, memory_max, limits["cpu"])
    # set-property's runtime files outrank the drop-in until the next boot; the cgroup already
    # has the values, so take them away and let the drop-in be the only place limits live
    _clear_runtime(server)
    services.reload(quiet=True)
    return memory_max


def _clear_runtime(server):
    d = os.path.join(RUNTIME_CONTROL, f"{server}.service.d")
    for name in RUNTIME_FILES:
        try:
            os.remove(os.path.join(d, name))
        except OSError:
            pass
    try:
        os.rmdir(d)
    except OSError:
        pass


def _unit_props(server):
    shown = host.systemctl_show([f"{server}.service"], ("Id", "ExecStart", "ActiveState", "SubState", "Result",
                                                       "NRestarts", "MemoryCurrent", "MemoryMax", "CPUQuotaPerSecUSec"))
    return shown.get(f"{server}.service", {})


def _exec_start(server):
    return services.exec_start(server)


def _host_total():
    return host.memory()["total"] or 0


def _heaps(exclude=None, running_only=True):
    """{server: -Xmx bytes} of the Java servers on this panel (running ones by default)."""
    out = {}
    for s in manager.list_servers():
        if s["name"] == exclude or s["edition"] != "Java" or (running_only and s["status"] != "online"):
            continue
        b = to_bytes(s.get("ram"))
        if b:
            out[s["name"]] = b
    return out


def _find(server):
    manager._check_server(server)
    srv = next((s for s in manager.list_servers() if s["name"] == server), None)
    if not srv:
        raise PerfError(f"No Minecraft server called '{server}' on this panel.")
    return srv


def resources(server, srv=None):
    """What Maintenance > Memory and limits shows."""
    srv = srv or _find(server)
    java = srv["edition"] == "Java"
    heap = manager.unit_heap(_exec_start(server)) if java else {}
    xmx = to_bytes(heap.get("xmx"))
    limits = get_limits(server)
    total = _host_total()
    others = _heaps(exclude=server)
    return {"server": server, "java": java, "running": srv["status"] == "online",
            "xms": heap.get("xms"), "xmx": heap.get("xmx"), "xmx_bytes": xmx,
            "host_memory": total, "cores": os.cpu_count() or 1,
            "others_running": sum(others.values()), "others": others,
            "room": max(0, total - system_reserve(total) - sum(others.values())) if total else None,
            "reserve": system_reserve(total),
            "memory_limit": limits["memory"], "cpu_limit": limits["cpu"],
            "memory_max": _memory_max(limits, xmx), "auto_limit": auto_limit(xmx) if xmx else None,
            "default_heap": manager.default_heap()}


def set_resources(server, xms=None, xmx=None, memory=None, cpu=None, restart=False, actor="?"):
    """Saves Memory (Java heap) and the hard limits. memory: "off", "auto" or a size like "3G";
    cpu: cores (e.g. 1.5) or None/0 for no limit. A running Java server needs a restart for a new
    heap; limits apply at once."""
    srv = _find(server)
    view = resources(server, srv)
    total = view["host_memory"]
    changed = []
    new_xmx = view["xmx_bytes"]
    if srv["edition"] == "Java" and (xms or xmx) and (xms, xmx) != (view["xms"], view["xmx"]):
        xms, xmx = (xms or view["xms"] or "").strip().upper(), (xmx or view["xmx"] or "").strip().upper()
        xms_b, xmx_b = to_bytes(xms), to_bytes(xmx)
        if not xms_b or not xmx_b or not manager.HEAP_RE.match(xms) or not manager.HEAP_RE.match(xmx):
            raise PerfError("Write memory sizes like 1G or 1536M.")
        if xmx_b < MIN_HEAP:
            raise PerfError("The most memory must be at least 256M.")
        if xms_b > xmx_b:
            raise PerfError("The starting memory can't be more than the most memory.")
        reserve = system_reserve(total)
        if total and xmx_b > total - reserve:
            raise PerfError(f"This machine has {_gb(total)} of memory; leave at least {_reserve_text(total)} for the "
                            f"system (at most {heap_text(max(MIN_HEAP, (total - reserve) // MB_ * MB_))}).")
        if total and view["others_running"] + xmx_b > total - reserve:
            raise PerfError(f"With the other running servers ({_gb(view['others_running'])} together) that leaves "
                            f"under {_reserve_text(total)} of this machine's {_gb(total)} for the system. Lower this or another server, "
                            "or move one to another cluster member.")
        java = manager.unit_java(_exec_start(server)) or paths.DEFAULT_JAVA
        manager._write_unit(server, srv["directory"], "java", xms=xms, xmx=xmx, java=java)
        new_xmx = xmx_b
        changed.append(f"memory {xms}-{xmx}")
    elif srv["edition"] != "Java" and (xms or xmx):
        raise PerfError("Bedrock servers have no memory setting; use a hard memory limit instead.")

    limits = get_limits(server)
    new = dict(limits)
    if memory is not None:
        m = str(memory).strip()
        if m.lower() in ("off", "", "none"):
            new["memory"] = "off"
        elif m.lower() == "auto":
            if not new_xmx:
                raise PerfError("Automatic follows a Java server's memory; give a size for a Bedrock server.")
            new["memory"] = "auto"
        else:
            b = to_bytes(m.upper())
            if not b:
                raise PerfError("Write the hard memory limit like 3G or 2560M, or choose Automatic or Off.")
            if b < MIN_LIMIT:
                raise PerfError("The hard memory limit must be at least 256M.")
            new["memory"] = b
    if isinstance(new["memory"], int) and new_xmx and new["memory"] < new_xmx + LIMIT_HEADROOM:
        raise PerfError(f"The hard memory limit must be well above the server's most memory ({heap_text(new_xmx)}): "
                        f"Java needs room outside its heap, and at the limit the server is stopped. "
                        f"Use at least {heap_text(new_xmx + LIMIT_HEADROOM)}, or Automatic ({heap_text(auto_limit(new_xmx))}).")
    if total and isinstance(new["memory"], int) and new["memory"] > total:
        raise PerfError(f"That's more than this machine's {_gb(total)} of memory.")
    if cpu is not None:
        try:
            c = float(cpu or 0)
        except (TypeError, ValueError):
            raise PerfError("Write the CPU limit as a number of cores, like 1 or 1.5.")
        cores = os.cpu_count() or 1
        if c < 0 or (c and c < 0.1) or c > cores:
            raise PerfError(f"The CPU limit is between 0.1 and {cores} cores (this machine has {cores}), or 0 for none.")
        new["cpu"] = round(c, 2) if c else None
    if new != limits or (changed and limits["memory"] == "auto"):
        data = _load_limits()
        if new == {"memory": "off", "cpu": None}:
            data.pop(server, None)
        else:
            data[server] = new
        _save_limits(data)
        _apply_limits(server, new, new_xmx, srv["status"] == "online")
        if new != limits:
            changed.append(f"hard memory limit {_limit_text(new['memory'], new_xmx)}, "
                           f"CPU {str(new['cpu']) + ' cores' if new['cpu'] else 'unlimited'}")
    restarted = False
    if changed and restart and srv["status"] == "online" and any(c.startswith("memory") for c in changed):
        manager.control_server(server, "restart")
        restarted = True
    if changed:
        manager.write_log(f"[PERF] {actor} set {server}'s {'; '.join(changed)}"
                          f"{' and restarted it' if restarted else ''}.")
    out = resources(server)
    out["restarted"] = restarted
    out["restart_needed"] = (srv["status"] == "online" and not restarted and any(c.startswith("memory") for c in changed))
    return out


def _limit_text(mem, xmx):
    if mem == "auto":
        return f"automatic ({heap_text(auto_limit(xmx))})" if xmx else "automatic"
    return heap_text(mem) if isinstance(mem, int) else "off"


def _gb(n):
    return f"{n / 1024 ** 3:.1f} GB"


def on_server_removed(server, purged):
    """The unit went; its limits drop-in goes too. Purged: forget the limits and the history."""
    if not NAME_RE.fullmatch(server or ""):
        return
    path = _dropin_path(server)
    try:
        os.remove(path)
        os.rmdir(os.path.dirname(path))
    except OSError:
        pass
    _clear_runtime(server)
    with _lock:
        _latest.pop(server, None)
        _ticks.pop(server, None)
    if purged:
        data = _load_limits()
        if data.pop(server, None) is not None:
            _save_limits(data)
        try:
            os.remove(os.path.join(METRICS_DIR, f"{server}.ring"))
        except OSError:
            pass


def on_server_restored(server):
    """A restored server gets its limits back (the unit is new, the stored limits aren't)."""
    if not NAME_RE.fullmatch(server or ""):
        return
    limits = get_limits(server)
    if limits != {"memory": "off", "cpu": None}:
        xmx = to_bytes(manager.unit_heap(_exec_start(server)).get("xmx"))
        _apply_limits(server, limits, xmx, False)


# ---------------------------------------------------------------------------
# Sampler
# ---------------------------------------------------------------------------
_prev_sample = None
_prev_throttle = {}


def _rates(prev, now):
    """CPU %, network totals per second and per-unit CPU % between two host._sample()s."""
    dt = now[0] - prev[0]
    if dt <= 0:
        return None, 0, 0, {}
    cpu = None
    if now[1] and prev[1] and now[1][1] > prev[1][1]:
        cpu = round(100 * (now[1][0] - prev[1][0]) / (now[1][1] - prev[1][1]), 1)
    rx = tx = 0
    for name, (r, t) in now[2].items():
        if name == "lo" or name not in prev[2]:
            continue
        rx += max(0, r - prev[2][name][0])
        tx += max(0, t - prev[2][name][1])
    cores = os.cpu_count() or 1
    units = {}
    for unit, nsec in now[3].items():
        before = prev[3].get(unit)
        if nsec is not None and before is not None and nsec >= before:
            units[unit] = round(100 * (nsec - before) / (dt * 1e9) / cores, 1)
    return cpu, rx / dt, tx / dt, units


def sample_once(now=None):
    """One round: measure every server and the host, write the ring files, send alerts."""
    global _prev_sample
    now = int(now or time.time())
    listed = manager.list_servers()
    units = [f"{s['name']}.service" for s in listed]
    current = host._sample(units)
    prev, _prev_sample = _prev_sample, current
    cpu, rx, tx, unit_cpu = _rates(prev, current) if prev else (None, None, None, {})
    shown = host.systemctl_show(units, ("Id", "MemoryCurrent", "NRestarts", "Result")) if units else {}
    mem = host.memory()
    disks = host.disks()
    servers_disk = next((d for d in disks if "Game servers" in d["holds"]), None)
    host_ring().write(now, [cpu, mem.get("percent"), servers_disk and servers_disk.get("percent"), rx, tx])
    with _lock:
        _host_latest.update(time=now, cpu=cpu, memory=mem.get("percent"), disk=servers_disk and servers_disk.get("percent"))
    for s in listed:
        name = s["name"]
        if not NAME_RE.fullmatch(name):
            continue
        online = s["status"] == "online"
        u = shown.get(f"{name}.service", {})
        tick = tick_health(name, s) if online and s["edition"] == "Java" else {"available": False}
        players = host.java_players(name, s["directory"]) if online and s["edition"] == "Java" else None
        throttled = None
        if online:
            st = cgroup_stats(name)
            before = _prev_throttle.get(name)
            if st["throttled_usec"] is not None and before and before[1] is not None and st["throttled_usec"] >= before[1]:
                throttled = round(100 * (st["throttled_usec"] - before[1]) / max(1, (now - before[0]) * 1e6), 1)
            _prev_throttle[name] = (now, st["throttled_usec"])
        memory = host._int(u.get("MemoryCurrent")) if online else None
        server_ring(name).write(now, [unit_cpu.get(f"{name}.service") if online else None, memory,
                                      players[0] if players else (0 if online else None),
                                      tick.get("tps") if tick.get("available") else None,
                                      tick.get("mspt") if tick.get("available") else None])
        with _lock:
            _latest[name] = {"time": now, "throttled_percent": throttled}
    try:
        alerts(listed, disks, now)
    except Exception as e:                 # alerts never stop the sampler
        manager.write_log(f"[PERF] Checking alerts failed: {e}")


def latest(server):
    with _lock:
        return dict(_latest.get(server) or {})


def sampler(every=SAMPLE_EVERY, first=FIRST_SAMPLE):
    """Runs sample_once() in a daemon thread, at low priority, for as long as the panel runs."""
    global _sampler

    def loop():
        try:
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)
        except (AttributeError, OSError):
            pass
        time.sleep(first)
        while True:
            started = time.time()
            try:
                sample_once()
            except Exception as e:          # never let this take the panel down
                manager.write_log(f"[PERF] Sampling failed: {e}")
            time.sleep(max(5, every - (time.time() - started)))
    with _lock:
        if _sampler is None:
            _sampler = threading.Thread(target=loop, name="perf-sampler", daemon=True)
            _sampler.start()
    return _sampler


# ---------------------------------------------------------------------------
# Alerts (webhook events tps_low and disk_full, off by default, at most once an hour each)
# ---------------------------------------------------------------------------
def _recent(rows, field, minutes, now):
    i = SERVER_FIELDS.index(field)
    return [v[i] for t, v in rows if t > now - minutes * 60]


def low_for(rows, minutes, below, now, field="tps"):
    """True if every minute of the last `minutes` was measured and under `below`."""
    vals = _recent(rows, field, minutes, now)
    return len(vals) >= minutes - 1 and all(v is not None and v < below for v in vals)


def alerts(listed, disks, now):
    for s in listed:
        if s["status"] != "online" or s["edition"] != "Java":
            continue
        rows = server_ring(s["name"]).read(0, now)
        if low_for(rows, 5, TPS_ALERT, now):
            lowest = min(_recent(rows, "tps", 5, now))
            notify.send("tps_low", s["name"],
                        f"{s['name']} has run under {TPS_ALERT} TPS for 5 minutes (lowest {lowest:.1f}). "
                        "Players will notice lag; open its Performance card for what to change.")
    for d in disks:
        if d.get("percent") is not None and d["percent"] > DISK_ALERT:
            notify.send("disk_full", None,
                        f"The disk holding {', '.join(d['holds']).lower()} ({d['mount']}) is {d['percent']:.0f} % full; "
                        f"{_gb(d['free'])} left. Backups and worlds can fail to save when it fills up.",
                        quiet_key=f"disk:{d['mount']}")


# ---------------------------------------------------------------------------
# What the Performance card shows
# ---------------------------------------------------------------------------
def _world_border(call):
    """The world border's width over RCON (read-only `worldborder get`), or None."""
    try:
        m = re.search(r"([0-9][0-9,.]*)\s+block", _clean(call("worldborder get", timeout=3) or ""))
    except Exception:
        return None
    return float(m.group(1).replace(",", "")) if m else None


def suggestions(srv, rows, view, unit, now=None, measured=None):
    """What to change, each only when its condition was measured. measured: facts that cost
    something to find (world size, logs size, world border, rotating disk, host memory), so the
    tests can pass them in."""
    now = int(now or time.time())
    m = measured or {}
    out = []
    name, java, loader = srv["name"], srv["edition"] == "Java", srv.get("loader") or ("vanilla" if srv["edition"] == "Java" else None)
    props = manager.read_properties(srv["directory"])
    online = srv["status"] == "online"

    def add(key, text, where, level="info"):
        out.append({"id": key, "level": level, "text": text, "where": where})

    if java and online and low_for(rows, 10, TPS_WARN, now):
        lowest = min(_recent(rows, "tps", 10, now))
        sim, view_d = props.get("simulation-distance", "10"), props.get("view-distance", "10")
        add("tps_low", f"TPS has been under {TPS_WARN} for 10 minutes (lowest {lowest:.1f}). Lower simulation-distance "
            f"first (now {sim}), then view-distance (now {view_d}). To find what's slow, profile with spark "
            f"({'a Paper plugin' if loader == 'paper' else 'a Fabric mod'}).", "properties", "warn")
        if loader == "vanilla":
            add("vanilla_slow", "Paper and Fabric (with Lithium) run the same world much faster than Vanilla. "
                "Change this server to one of them and keep its world: Maintenance, Server type.", "type")
    if loader == "fabric":
        mods = _addon_names(srv["directory"], "mods")
        if mods is not None and not any("lithium" in n for n in mods):
            add("lithium", "This Fabric server has no performance mods. Lithium speeds up game logic without "
                "changing how the game plays.", "addons")
        if m.get("memory_tight") and not any("ferritecore" in n for n in mods):
            add("ferritecore", "Memory is tight: FerriteCore cuts how much memory Fabric uses.", "addons")
    if loader == "paper":
        plugins = _addon_names(srv["directory"], "plugins")
        if any("worldedit" in n and "fastasync" not in n for n in plugins) and not any("fastasync" in n for n in plugins):
            add("fawe", "WorldEdit is installed: FastAsyncWorldEdit does the same edits off the main thread, "
                "so big edits don't freeze the server.", "addons")
    restarts_n = host._int(unit.get("NRestarts")) or 0
    if unit.get("Result") == "oom-kill" or (view.get("memory_max") and unit.get("memory") and unit["memory"] >= 0.95 * view["memory_max"]):
        add("limit_hit", "It reached its hard memory limit" + (" and was stopped" if unit.get("Result") == "oom-kill" else "")
            + ". Raise the limit, or set it to Automatic, in Maintenance > Memory and limits.", "memory", "warn")
    elif java and restarts_n >= 2 and view.get("xmx_bytes") and unit.get("memory") and unit["memory"] >= view["xmx_bytes"]:
        room = view.get("room") or 0
        add("raise_memory", f"It uses all of its {view['xmx']} and has restarted {restarts_n} times. "
            + (f"This machine has room: raise Memory in Maintenance." if room >= 512 * 1024 ** 2 else
               "This machine has no room to give it more; lower another server or move one to another cluster member."),
            "memory", "warn")
    total = view.get("host_memory") or 0
    all_heaps = m.get("all_heaps")
    if java and total and all_heaps and all_heaps > total - system_reserve(total):
        add("overcommit", f"All the Java servers' memory together ({_gb(all_heaps)}) is more than this machine's "
            f"{_gb(total)} minus {_reserve_text(total)} for the system. If they all run at once, lower one or move one to another "
            "cluster member.", "memory", "warn")
    elif m.get("swap_percent") and m["swap_percent"] > 25:
        add("swap", f"The machine is using swap ({m['swap_percent']:.0f} %), which makes servers stutter. "
            "Lower a server's memory or move one to another cluster member.", "memory", "warn")
    if java and m.get("world_size") and m["world_size"] > BIG_WORLD and m.get("border") and m["border"] > NO_BORDER:
        add("border", f"The world is {_gb(m['world_size'])} and has no world border. Set one (worldborder set), and "
            "pre-generate it with Chunky so exploring doesn't generate chunks during play.", "chat")
    if not m.get("backups_scheduled", True):
        add("backups", "It has no backup schedule. Turn one on in Backups.", "backups")
    if m.get("logs_size") and m["logs_size"] > BIG_LOGS:
        add("logs", f"Its logs folder is {_gb(m['logs_size'])}. Old logs can be deleted in Files > logs.", "files")
    if m.get("rotational"):
        add("hdd", "Its world is on a spinning hard disk. Chunk loading and saving are much faster on an SSD.", "files")
    return out


def server_perf(server, rng="day"):
    """Everything the Performance card shows for one server."""
    srv = _find(server)
    now = int(time.time())
    online = srv["status"] == "online"
    unit = _unit_props(server)
    unit["memory"] = host._int(unit.get("MemoryCurrent")) if online else None
    view = resources(server, srv)
    tick = tick_health(server, srv)
    players = host.java_players(server, srv["directory"]) if online and srv["edition"] == "Java" else None
    folder, addons = addon_count(srv["directory"], srv.get("loader"))
    level = manager.read_properties(srv["directory"]).get("level-name", "world") if srv["edition"] == "Java" else None
    world_dir = os.path.join(srv["directory"], level) if level and "/" not in level and ".." not in level \
        else os.path.join(srv["directory"], "worlds")
    world_size, _ = host.cached_size(world_dir) if os.path.isdir(world_dir) else (None, None)
    logs_size, _ = host.cached_size(os.path.join(srv["directory"], "logs")) \
        if os.path.isdir(os.path.join(srv["directory"], "logs")) else (None, None)
    mem = host.memory()
    border = None
    if online and srv["edition"] == "Java" and world_size and world_size > BIG_WORLD:
        border = host._cached(f"border:{server}", 3600,
                              lambda: _world_border(backups._java_rcon(srv["directory"])) if backups._java_rcon(srv["directory"]) else None)
    all_heaps = sum(_heaps(running_only=False).values())
    rows = server_ring(server).read(0, now)
    try:
        scheduled = backups.get_schedule(server).get("enabled", False)
    except Exception:
        scheduled = True
    held = latest(server)
    measured = {"world_size": world_size, "logs_size": logs_size, "border": border, "all_heaps": all_heaps,
                "swap_percent": mem.get("swap_percent"), "rotational": rotational(srv["directory"]),
                "memory_tight": bool(mem.get("percent") and mem["percent"] > 90),
                "backups_scheduled": scheduled}
    held_back = []
    if held.get("throttled_percent") and held["throttled_percent"] >= 5:
        held_back.append(f"Its CPU limit held it back {held['throttled_percent']:.0f} % of the last minute.")
    if view["memory_max"] and unit["memory"] and unit["memory"] >= 0.95 * view["memory_max"]:
        held_back.append("It's at its hard memory limit.")
    elif online and view["memory_max"] and (cgroup_stats(server).get("memory_max_events") or 0) > 0:
        held_back.append("It has reached its hard memory limit since it started.")
    if unit.get("Result") == "oom-kill":
        held_back.append("It was stopped for going over its hard memory limit.")
    return {
        "server": server, "edition": srv["edition"], "loader": srv.get("loader"), "status": srv["status"],
        "updated": now, "port": srv.get("port"), "address": f"{_address()}:{srv.get('port')}",
        "tick": tick,
        "memory": {"used": unit["memory"], "heap": srv.get("ram") if srv["edition"] == "Java" else None,
                   "heap_bytes": view["xmx_bytes"], "limit": view["memory_max"]},
        "cpu_limit": view["cpu_limit"], "held_back": held_back,
        "players": players[0] if players else None, "max_players": players[1] if players else None,
        "connections": connections(srv.get("port")) if online and srv["edition"] == "Java" else None,
        "world_size": world_size, "addons": addons, "addons_folder": folder,
        "history": history(server_ring(server), SERVER_FIELDS, rng),
        "suggestions": suggestions(srv, rows, view, unit, now, measured),
    }


def _address():
    ips = host._ips()
    return ips[0] if ips else "localhost"
