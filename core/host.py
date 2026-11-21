"""Host and resource status for the dashboard (the screen shown when no server is selected).

Everything comes from /proc, statvfs and `systemctl show`: no psutil, no database.

  * CPU %, network rates and per-server CPU need two samples. The previous sample is kept in
    memory between requests; the very first request takes two samples a quarter second apart.
  * Folder sizes are walked by a background thread at low priority and cached for
    SIZE_EVERY seconds, never during a request.
  * Slow or rarely changing facts (apt, ufw, certificates, player counts) are cached.
  * report() is cached for REPORT_TTL seconds, so many open tabs cost one report.

A cluster master reads its nodes' reports through GET /api/host (read-only, on the node
allowlist) and shows summary() of each.
"""
import calendar
import os
import re
import shutil
import socket
import subprocess
import threading
import time

from core import backups, javaservers, manager, notify, proxies, restarts, runtimes, tls
from core import cluster
from core import settings as panel_settings
from core.version import panel_version

PROC = "/proc"
OS_RELEASE = "/etc/os-release"
LOCALTIME = "/etc/localtime"
SIZE_EVERY = 600              # folder sizes are re-walked after this many seconds
SIZER_PAUSE = 15
REPORT_TTL = 3
SAMPLE_GAP = 0.25             # first request: gap between the two CPU/network samples
SAMPLE_MAX_AGE = 120          # an older previous sample is too stale for a rate
DISK_WARN, DISK_ERROR, DISK_MIN_FREE = 90, 95, 5 * 1024 ** 3
MEMORY_WARN = 10              # % available
SWAP_WARN = 80                # % used
CERT_WARN_DAYS = 14
UNSET = 18446744073709551615  # systemd's "[not set]" for counters

UNIT_PROPS = ("Id", "LoadState", "ActiveState", "SubState", "Result", "NRestarts", "MemoryCurrent",
              "CPUUsageNSec", "ActiveEnterTimestampMonotonic", "NextElapseUSecRealtime", "LastTriggerUSec")

_lock = threading.Lock()
_prev = {}                    # "cpu" -> (t, busy, total); "net" -> (t, {iface: (rx, tx)}); "units" -> (t, {unit: nsec})
_cache = {}                   # key -> (t, value)
_sizes = {}                   # folder -> (t, bytes)
_wanted = set()               # folders the sizer should keep measured
_sizer = None


# ---------------------------------------------------------------------------
# Small readers (pure parsers are separate so the tests can feed them text)
# ---------------------------------------------------------------------------
def _read(path, default=""):
    try:
        with open(path, errors="replace") as f:
            return f.read()
    except OSError:
        return default


def _cached(key, ttl, compute):
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    value = compute()
    with _lock:
        _cache[key] = (time.time(), value)
    return value


def parse_meminfo(text):
    out = {}
    for line in text.splitlines():
        k, _, v = line.partition(":")
        parts = v.split()
        if parts and parts[0].isdigit():
            out[k.strip()] = int(parts[0]) * (1024 if len(parts) > 1 and parts[1] == "kB" else 1)
    return out


def memory(text=None):
    m = parse_meminfo(_read(os.path.join(PROC, "meminfo")) if text is None else text)
    total = m.get("MemTotal", 0)
    available = m.get("MemAvailable", m.get("MemFree", 0) + m.get("Cached", 0))
    swap_total, swap_free = m.get("SwapTotal", 0), m.get("SwapFree", 0)
    return {"total": total, "available": available, "used": max(0, total - available),
            "percent": round(100 * (total - available) / total, 1) if total else None,
            "swap_total": swap_total, "swap_used": max(0, swap_total - swap_free),
            "swap_percent": round(100 * (swap_total - swap_free) / swap_total, 1) if swap_total else None}


def parse_cpu_stat(text):
    """(busy, total) jiffies from the 'cpu' line of /proc/stat."""
    for line in text.splitlines():
        if line.startswith("cpu "):
            f = [int(x) for x in line.split()[1:9]]
            f += [0] * (8 - len(f))
            idle = f[3] + f[4]                      # idle + iowait
            total = sum(f)
            return total - idle, total
    return None


def parse_net_dev(text):
    """{iface: (rx_bytes, tx_bytes)}, without loopback and container veths."""
    out = {}
    for line in text.splitlines()[2:]:
        name, _, rest = line.partition(":")
        name, f = name.strip(), rest.split()
        if not name or len(f) < 9 or name == "lo" or name.startswith(("veth", "docker", "br-")):
            continue
        out[name] = (int(f[0]), int(f[8]))
    return out


def cpu_model(text=None):
    text = _read(os.path.join(PROC, "cpuinfo")) if text is None else text
    for key in ("model name", "Model", "Hardware", "cpu model"):
        m = re.search(rf"^{key}\s*:\s*(.+)$", text, re.M)
        if m:
            return re.sub(r"\s+", " ", m.group(1)).strip()
    return None


def os_name(text=None):
    text = _read(OS_RELEASE) if text is None else text
    m = re.search(r'^PRETTY_NAME="?([^"\n]*)"?', text, re.M)
    return m.group(1) if m else None


def timezone():
    target = os.path.realpath(LOCALTIME)
    if "zoneinfo/" in target:
        return target.split("zoneinfo/", 1)[1]
    return time.strftime("%Z")


def _uptime():
    try:
        return float(_read(os.path.join(PROC, "uptime"), "0").split()[0])
    except (ValueError, IndexError):
        return None


def _ips():
    try:
        r = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=3)
        return r.stdout.split()
    except (OSError, subprocess.SubprocessError):
        return []


# ---------------------------------------------------------------------------
# systemd
# ---------------------------------------------------------------------------
def _int(v):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return None if n == UNSET else n


UTC_STAMP_RE = re.compile(r"^(?:\w{3} )?(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) UTC$")


def _unix(v):
    """A systemctl timestamp -> Unix time. systemctl_show runs with TZ=UTC, so timers print
    'Thu 2026-11-19 04:00:36 UTC'; '@1763438400' (--timestamp=unix) is understood too."""
    if not isinstance(v, str):
        return None
    if v.startswith("@") and v[1:].isdigit():
        return int(v[1:]) or None
    m = UTC_STAMP_RE.match(v.strip())
    if m:
        return int(calendar.timegm(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")))
    return None


def parse_show(text):
    """`systemctl show` output for several units -> {Id: {prop: value}}."""
    units = {}
    for block in text.strip().split("\n\n"):
        props = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        if props.get("Id"):
            units[props["Id"]] = props
    return units


def systemctl_show(units, props=UNIT_PROPS):
    if not units:
        return {}
    try:
        r = subprocess.run(["systemctl", "show", "-p", ",".join(props), *units], capture_output=True,
                           text=True, timeout=10, env={**os.environ, "TZ": "UTC"})
    except (OSError, subprocess.SubprocessError):
        return {}
    return parse_show(r.stdout)


def failed_units():
    """Failed panel and game server units. A unit whose file is gone (a removed server that
    had failed before) is only systemd's memory of the failure: it's cleared, not reported."""
    try:
        r = subprocess.run(["systemctl", "list-units", "--failed", "--all", "--plain", "--no-legend",
                            "minecraft*", "redstonepanel*"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    failed, gone = [], []
    for line in r.stdout.splitlines():
        parts = line.split()
        if not parts:
            continue
        (gone if len(parts) > 1 and parts[1] == "not-found" else failed).append(parts[0])
    if gone:
        try:
            subprocess.run(["systemctl", "reset-failed", *gone], capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass
    return failed


def _unit_uptime(props):
    mono = _int(props.get("ActiveEnterTimestampMonotonic"))
    if props.get("ActiveState") != "active" or not mono:
        return None
    return max(0, int(time.monotonic() - mono / 1e6))


# ---------------------------------------------------------------------------
# Samples and rates
# ---------------------------------------------------------------------------
def _sample(server_units):
    cpu = parse_cpu_stat(_read(os.path.join(PROC, "stat")))
    net = parse_net_dev(_read(os.path.join(PROC, "net/dev")))
    shown = systemctl_show(server_units, ("Id", "CPUUsageNSec")) if server_units else {}
    units = {u: _int(p.get("CPUUsageNSec")) for u, p in shown.items()}
    return time.monotonic(), cpu, net, units


def rates(server_units):
    """CPU %, network bytes/s per interface and CPU % per unit (of the whole machine)."""
    with _lock:
        prev = _prev.get("sample")
    now = _sample(server_units)
    if not prev or now[0] - prev[0] > SAMPLE_MAX_AGE or now[0] - prev[0] < 0.05:
        time.sleep(SAMPLE_GAP)
        prev, now = now, _sample(server_units)
    with _lock:
        _prev["sample"] = now
    dt = now[0] - prev[0]
    cores = os.cpu_count() or 1
    cpu = None
    if now[1] and prev[1] and now[1][1] > prev[1][1]:
        cpu = round(100 * (now[1][0] - prev[1][0]) / (now[1][1] - prev[1][1]), 1)
    net = {}
    for name, (rx, tx) in now[2].items():
        if name in prev[2]:
            net[name] = {"rx": max(0, int((rx - prev[2][name][0]) / dt)),
                         "tx": max(0, int((tx - prev[2][name][1]) / dt)),
                         "rx_total": rx, "tx_total": tx}
    units = {}
    for unit, nsec in now[3].items():
        before = prev[3].get(unit)
        if nsec is not None and before is not None and nsec >= before:
            units[unit] = round(100 * (nsec - before) / (dt * 1e9) / cores, 1)
    return cpu, net, units


# ---------------------------------------------------------------------------
# Disks and folder sizes
# ---------------------------------------------------------------------------
def _mounts():
    out = []
    for line in _read(os.path.join(PROC, "self/mounts")).splitlines():
        f = line.split()
        if len(f) >= 3:
            out.append((f[1].replace("\\040", " "), f[2]))
    return out


def _existing(path):
    while path and not os.path.exists(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return path or "/"


def disks():
    """The file systems holding the servers, backups and panel data (each listed once)."""
    places = (("Game servers", manager.SERVERS_ROOT), ("Backups", backups.BACKUP_ROOT),
              ("Panel data", panel_settings.DATA_DIR), ("System", "/"))
    mounts = _mounts()
    found = {}
    for label, path in places:
        p = _existing(path)
        try:
            dev, st = os.stat(p).st_dev, os.statvfs(p)
        except OSError:
            continue
        if dev in found:
            found[dev]["holds"].append(label)
            continue
        mount = max((m for m in mounts if p == m[0] or p.startswith(m[0].rstrip("/") + "/")),
                    key=lambda m: len(m[0]), default=("/", None))
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used = (st.f_blocks - st.f_bfree) * st.f_frsize
        found[dev] = {"mount": mount[0], "fstype": mount[1], "holds": [label], "total": total,
                      "used": used, "free": free,
                      "percent": round(100 * used / (used + free), 1) if used + free else None}
    return list(found.values())


def folder_size(path):
    """Bytes in a folder, without following symlinks or crossing into other file systems."""
    total = 0
    try:
        dev = os.lstat(path).st_dev
    except OSError:
        return None
    for root, dirs, names in os.walk(path, onerror=lambda e: None):
        for n in names:
            try:
                st = os.lstat(os.path.join(root, n))
            except OSError:
                continue
            total += st.st_size
        keep = []
        for d in dirs:
            try:
                st = os.lstat(os.path.join(root, d))
            except OSError:
                continue
            if st.st_dev == dev and not os.path.islink(os.path.join(root, d)):
                keep.append(d)
        dirs[:] = keep
    return total


def _sizer_loop():
    try:
        os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 19)   # this thread only
    except (AttributeError, OSError):
        pass
    while True:
        for path in list(_wanted):
            hit = _sizes.get(path)
            if not hit or time.time() - hit[0] > SIZE_EVERY:
                _sizes[path] = (time.time(), folder_size(path))
        time.sleep(SIZER_PAUSE)


def cached_size(path):
    """(bytes or None, measured at or None). Starts the background sizer on first use."""
    global _sizer
    _wanted.add(path)
    with _lock:
        if _sizer is None:
            _sizer = threading.Thread(target=_sizer_loop, name="folder-sizes", daemon=True)
            _sizer.start()
    hit = _sizes.get(path)
    return (hit[1], int(hit[0])) if hit else (None, None)


# ---------------------------------------------------------------------------
# Slow facts, cached
# ---------------------------------------------------------------------------
def parse_apt_policy(text):
    inst = re.search(r"Installed:\s*(\S+)", text)
    cand = re.search(r"Candidate:\s*(\S+)", text)
    return (inst.group(1) if inst else None), (cand.group(1) if cand else None)


def panel_update():
    """A newer redstonepanel apt knows about (from the last `apt update`), or None."""
    def compute():
        if not panel_settings.PACKAGED or not shutil.which("apt-cache"):
            return None
        try:
            r = subprocess.run(["apt-cache", "policy", "redstonepanel"], capture_output=True, text=True, timeout=20)
            inst, cand = parse_apt_policy(r.stdout)
            if not inst or not cand or "none" in inst or "none" in cand or inst == cand:
                return None
            newer = subprocess.run(["dpkg", "--compare-versions", inst, "lt", cand], timeout=10).returncode == 0
            return cand if newer else None
        except (OSError, subprocess.SubprocessError):
            return None
    return _cached("apt", 600, compute)


def forget_panel_update():
    """After `apt-get update`: look again instead of answering from the 10-minute cache."""
    with _lock:
        _cache.pop("apt", None)
        _cache.pop("report", None)


def ufw_allows(status_text, port, proto):
    """Whether `ufw status` text has an ALLOW rule covering port/proto."""
    for line in status_text.splitlines():
        if "ALLOW" not in line:
            continue
        target = line.split()[0]
        ports, _, rule_proto = target.partition("/")
        if rule_proto and rule_proto != proto:
            continue
        for part in ports.split(","):
            lo, _, hi = part.partition(":")
            if lo.isdigit() and int(lo) <= port <= int(hi if hi.isdigit() else lo):
                return True
    return False


def ufw_allows_spec(status_text, spec, proto):
    """Whether every port of a spec ('19141' or '21200:21300') is allowed."""
    lo, _, hi = str(spec).partition(":")
    if not lo.isdigit():
        return False
    ports = range(int(lo), int(hi or lo) + 1)
    return all(ufw_allows(status_text, p, proto) for p in (ports if len(ports) <= 50 else (ports[0], ports[-1])))


# Ports that look like a game server's: Bedrock (and NetherNet LAN discovery) and Java
GAME_PORTS = ((7551, 7551), (19132, 19999), (25565, 25999))


def ufw_rules(status_text):
    """The ALLOW rules in `ufw status` text as (port spec, proto or None), once each (the (v6)
    copies and rules limited to an address or interface are left out)."""
    out = []
    for line in status_text.splitlines():
        parts = line.split()
        if len(parts) < 3 or "ALLOW" not in parts[1:3] or "(v6)" in line.split("ALLOW")[0]:
            continue
        if parts[1] != "ALLOW" or not re.fullmatch(r"[0-9:,]+(/(tcp|udp))?", parts[0]):
            continue
        ports, _, proto = parts[0].partition("/")
        if (ports, proto or None) not in out:
            out.append((ports, proto or None))
    return out


def leftover_rules(status_text, in_use):
    """ufw rules on game ports that no server on the panel or Phantom needs: ['19140/udp', ...]."""
    def covered(lo, hi, proto):
        for spec, p in in_use:
            a, _, b = str(spec).partition(":")
            if p == proto and a.isdigit() and int(a) <= hi and lo <= int(b or a):
                return True
        return False
    out = []
    for ports, proto in ufw_rules(status_text):
        for part in ports.split(","):
            a, _, b = part.partition(":")
            if not a.isdigit() or (b and not b.isdigit()):
                continue
            lo, hi = int(a), int(b or a)
            if not any(lo <= g_hi and g_lo <= hi for g_lo, g_hi in GAME_PORTS):
                continue
            protos = [proto] if proto else ["tcp", "udp"]
            if not all(covered(lo, hi, p) for p in protos):
                out.append(f"{part}/{proto}" if proto else part)
    return out


def firewall():
    def compute():
        if not shutil.which("ufw"):
            return {"installed": False, "active": False, "rules": ""}
        try:
            out = subprocess.run(["ufw", "status"], capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        return {"installed": True, "active": "Status: active" in out, "rules": out}
    return _cached("ufw", 60, compute)


def _cert_expiry(path):
    return _cached(f"cert:{path}", 3600, lambda: cluster.cert_expires(path) if os.path.isfile(path) else None)


def https():
    s = panel_settings.load()
    mode = s["https_mode"]
    path = {"self_signed": tls.SRV_CRT, "cluster": cluster.node_crt(), "custom": s.get("cert_file")}.get(mode)
    return {"mode": mode, "serving": panel_settings.RUNTIME.get("scheme"),
            "error": panel_settings.RUNTIME.get("https_error"),
            "cert_expires": _cert_expiry(path) if path else None}


PLAYERS_RE = re.compile(r"There are (\d+) of a max(?: of)? (\d+) players")


def java_players(server, d):
    """(online, max) over RCON for a running Java server, or None."""
    def compute():
        call = backups._java_rcon(d)
        if not call:
            return None
        try:
            m = PLAYERS_RE.search(call("list", timeout=2) or "")
        except Exception:
            return None
        return (int(m.group(1)), int(m.group(2))) if m else None
    return _cached(f"players:{server}", 15, compute)


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------
CRASH_RESTARTS = 3


def crash_looping(props):
    """systemd keeps restarting it: waiting to restart, or starting again after several tries."""
    if props.get("ActiveState") != "activating":
        return False
    return props.get("SubState") == "auto-restart" or (_int(props.get("NRestarts")) or 0) >= CRASH_RESTARTS


def _attention(level, text, server=None, where=None):
    return {"level": level, "text": text, "server": server, "where": where}


def _servers(listed, shown, unit_cpu, fw):
    rows = []
    for s in listed:
        name = s["name"]
        u = shown.get(f"{name}.service", {})
        bt = shown.get(f"redstonepanel-backup@{name}.timer", {})
        rt = shown.get(f"redstonepanel-restart@{name}.timer", {})
        size, measured = cached_size(s["directory"])
        backup_files = backups.list_backups(name) if backups.NAME_RE.fullmatch(name) else []
        players = java_players(name, s["directory"]) if s["edition"] == "Java" and s["status"] == "online" else None
        next_restart = _unix(rt.get("NextElapseUSecRealtime")) if rt.get("LoadState") == "loaded" else None
        if next_restart:
            next_restart += restarts.get_schedule(name)["warn"] * 60
        port = s.get("port")
        missing = None
        if fw["active"]:
            missing = [f"{p}/{proto}" for p, proto in s.get("firewall_rules") or []
                       if not ufw_allows_spec(fw["rules"], p, proto)]
        rows.append({
            "name": name, "edition": s["edition"], "version": s.get("version"), "status": s["status"],
            "state": u.get("ActiveState"), "sub_state": u.get("SubState"), "restarts": _int(u.get("NRestarts")),
            "crash_loop": crash_looping(u),
            "uptime": _unit_uptime(u), "cpu_percent": unit_cpu.get(f"{name}.service"),
            "memory": _int(u.get("MemoryCurrent")), "heap": s.get("ram"),
            "players": players[0] if players else None, "max_players": players[1] if players else None,
            "port": port, "directory": s["directory"], "folder_size": size, "folder_measured": measured,
            "backups_size": sum(b["size"] for b in backup_files), "backups": len(backup_files),
            "last_backup": backups.read_status(name) if backups.NAME_RE.fullmatch(name) else None,
            "next_backup": _unix(bt.get("NextElapseUSecRealtime")) if bt.get("LoadState") == "loaded" else None,
            "next_restart": next_restart,
            "java_required": s.get("java_required"), "java_installed": s.get("java_installed"),
            "firewall_open": None if missing is None else not missing,
            "firewall_missing": missing or [],
            "update": s.get("update"),
            "problems": bool(s.get("signaling_closed") or s.get("transport_rejected")
                             or any(p.get("problems") for p in s.get("proxies") or [])),
        })
    return rows


def _needs_attention(rep, failed, phantoms):
    out = []
    for d in rep["disks"]:
        p = d["percent"] or 0
        if p >= DISK_WARN or d["free"] < DISK_MIN_FREE:
            out.append(_attention("error" if p >= DISK_ERROR else "warn",
                                  f"{d['mount']} is {p:.0f}% full ({_gb(d['free'])} free). It holds: {', '.join(d['holds']).lower()}.",
                                  where="disk"))
    m = rep["memory"]
    if m["total"] and 100 * m["available"] / m["total"] < MEMORY_WARN:
        out.append(_attention("warn", f"Memory is nearly full: {_gb(m['available'])} of {_gb(m['total'])} available.",
                              where="memory"))
    if m["swap_percent"] is not None and m["swap_percent"] >= SWAP_WARN:
        out.append(_attention("warn", f"Swap is {m['swap_percent']:.0f}% used; the server may be short of memory.",
                              where="memory"))
    for unit in failed:
        server = unit[:-8] if unit.startswith("minecraft") and unit.endswith(".service") else None
        out.append(_attention("error", f"{unit} has failed.", server=server, where="services"))
    for s in rep["servers"]:
        if s["crash_loop"]:
            out.append(_attention("error", f"{s['name']} keeps crashing (restarted {s['restarts'] or 0} times).",
                                  server=s["name"]))
        lb = s["last_backup"]
        if lb and not lb.get("ok"):
            out.append(_attention("warn", f"The last backup of {s['name']} failed: {lb.get('error') or 'unknown error'}",
                                  server=s["name"], where="backups"))
        if s["edition"] == "Java" and s["java_required"] and (s["java_installed"] or 0) < s["java_required"]:
            out.append(_attention("error", f"{s['name']} needs Java {s['java_required']}, which isn't installed. "
                                           f"Install it under Supporting services → Java, or with: "
                                           f"sudo apt install {javaservers.java_package(s['java_required'])}",
                                  server=s["name"], where="java"))
        if s["firewall_open"] is False:
            miss = s["firewall_missing"]
            lan = f"{manager.NETHERNET_LAN_PORT}/udp" in miss
            why = ("consoles on your network won't find it under LAN Games" if lan and len(miss) == 1
                   else "players outside this machine can't connect"
                   + (", and consoles won't find it under LAN Games" if lan else ""))
            out.append(_attention("warn", f"The firewall (ufw) doesn't allow {', '.join(miss)} for {s['name']}, so "
                                          f"{why}. Open {'them' if len(miss) > 1 else 'it'} with: "
                                          + " && ".join(f"sudo ufw allow {m}" for m in miss),
                                  server=s["name"], where="services"))
        if s["problems"]:
            out.append(_attention("warn", f"Players may not be able to connect to {s['name']}: see its Overview.",
                                  server=s["name"]))
    left = rep["services"]["firewall"].get("leftover") or []
    if left:
        out.append(_attention("info", f"The firewall (ufw) still allows {', '.join(left)}, which no server on this "
                                      f"panel uses. If nothing else on this machine needs "
                                      f"{'them' if len(left) > 1 else 'it'}, close {'them' if len(left) > 1 else 'it'} with: "
                                      + " && ".join(f"sudo ufw delete allow {r}" for r in left),
                              where="services"))
    for p in phantoms:
        if p.get("active") != "active":
            out.append(_attention("warn", f"Phantom proxy {p['unit']} is {p.get('active')}.", where="services"))
    for j in rep["services"]["java"]:
        if j.get("removable"):
            out.append(_attention("info", f"Java {j['major']} isn't used by any server. Remove it under Supporting "
                                          f"services → Java if nothing else on this machine needs it.", where="java"))
    for s in rep.get("servers", []):
        try:
            from core import playerlog
            d = s.get("directory") or ""
            allowed = [e.get("name", "") for f in ("whitelist.json", "allowlist.json")
                       for e in manager._load_json_list(os.path.join(d, f)) if isinstance(e, dict)]
            away = playerlog.turned_away(s["name"], allowed)
        except Exception:
            away = []
        if away:
            names = ", ".join(f"{n} ({c}×)" if c > 1 else n for n, c, _ in away[:5])
            more = f" and {len(away) - 5} more" if len(away) > 5 else ""
            out.append(_attention("info", f"{len(away)} player{'s were' if len(away) != 1 else ' was'} turned away "
                                          f"from {s['name']} in the last 24 hours: {names}{more}. Allow them "
                                          f"under Configuration → Whitelist & Access.",
                                  server=s["name"], where="access"))
    if rep["services"]["screen"] is False and any(s["edition"] == "Bedrock" for s in rep["servers"]):
        out.append(_attention("warn", "screen isn't installed; Bedrock chat and consistent backups need it. "
                                      "Install it with: sudo apt install screen", where="services"))
    now = time.time()
    for label, exp in (("HTTPS", rep["services"]["https"]["cert_expires"]),
                       ("cluster", rep["services"]["cluster"].get("cert_expires"))):
        if exp and exp - now < CERT_WARN_DAYS * 86400:
            days = int((exp - now) // 86400)
            out.append(_attention("error" if exp < now else "warn",
                                  f"This panel's {label} certificate " + ("has expired." if exp < now
                                                                          else f"expires in {days} day{'s' if days != 1 else ''}."),
                                  where="settings"))
    if rep["services"]["https"]["error"]:
        out.append(_attention("error", f"HTTPS isn't working: {rep['services']['https']['error']}", where="settings"))
    if rep["host"].get("update"):
        out.append(_attention("info", f"RedstonePanel {rep['host']['update']} is available. "
                                      "Update it with Update now, or: sudo apt upgrade redstonepanel", where="update"))
    try:
        from core import backup_copies
        out.extend(backup_copies.attention())
    except Exception:
        pass                                    # never let this break the dashboard
    try:
        from core import updates
        out.extend(updates.attention())
        from core import backup_targets
        out.extend(backup_targets.attention())
    except Exception:
        pass
    order = {"error": 0, "warn": 1, "info": 2}
    return sorted(out, key=lambda a: order[a["level"]])


def _gb(n):
    return f"{n / 1024 ** 3:.1f} GB"


def _build():
    try:
        # Opening the dashboard also follows server.properties edited outside the panel
        if manager.sync_firewall():
            _cache.pop("ufw", None)
    except Exception as e:
        manager.write_log(f"[FIREWALL] Checking the firewall failed: {e}")
    listed = manager.list_servers()
    names = [s["name"] for s in listed]
    server_units = [f"{n}.service" for n in names]
    timers = [f"redstonepanel-{k}@{n}.timer" for n in names for k in ("backup", "restart")]
    shown = systemctl_show(server_units + timers + ["redstonepanel.service"])
    cpu, net, unit_cpu = rates(server_units)
    fw = firewall()
    phantoms = proxies.find_phantoms()
    failed = failed_units()
    panel = shown.get("redstonepanel.service", {})
    java = runtimes.status(listed)
    try:
        load = [round(x, 2) for x in os.getloadavg()]
    except OSError:
        load = None
    st = cluster.state()
    role = st.get("role", "standalone")
    servers = _servers(listed, shown, unit_cpu, fw)
    rep = {
        "generated": int(time.time()),
        "host": {"hostname": socket.gethostname(), "name": panel_settings.display_name(), "os": os_name(),
                 "kernel": os.uname().release, "uptime": _uptime(), "timezone": timezone(), "ips": _ips(),
                 "version": panel_version(), "update": panel_update()},
        "cpu": {"model": cpu_model(), "cores": os.cpu_count(), "load": load, "percent": cpu},
        "memory": memory(),
        "disks": disks(),
        "network": [{"name": k, **v} for k, v in sorted(net.items())],
        "servers": servers,
        "services": {
            "panel": {"state": panel.get("ActiveState") or "active", "uptime": _unit_uptime(panel),
                      "memory": _int(panel.get("MemoryCurrent")), "pid": os.getpid()},
            "failed_units": failed,
            "phantoms": [{"unit": p["unit"], "active": p.get("active"), "enabled": p.get("enabled")} for p in phantoms],
            "firewall": {"installed": fw["installed"], "active": fw["active"],
                         "leftover": leftover_rules(fw["rules"], manager.rules_in_use(listed)) if fw["active"] else []},
            "java": java["installed"],
            "java_needed": java["needed"],
            "java_installable": java["installable"],
            "java_job": java["job"],
            "screen": bool(shutil.which("screen")),
            "https": https(),
            "notifications": {k: v for k, v in notify.public().items() if k in ("configured", "kind")},
            "cluster": {"role": role, "name": st.get("name") or st.get("cluster_name"),
                        "cert_expires": _cert_expiry(cluster.node_crt()) if role != "standalone" else None,
                        "nodes": len(cluster.load_nodes()) if role == "master" else None},
        },
    }
    rep["attention"] = _needs_attention(rep, failed, phantoms)
    return rep


def redact(rep):
    """What a trusted-LAN visitor (not signed in) sees: no addresses, versions or paths."""
    out = dict(rep, host={k: v for k, v in rep["host"].items() if k not in ("ips", "kernel", "version", "update")},
               redacted=True)
    out["servers"] = [{k: v for k, v in s.items() if k != "directory"} for s in rep["servers"]]
    out["services"] = {k: v for k, v in rep["services"].items() if k in ("panel", "failed_units", "screen")}
    out["services"]["java"] = [{"major": j["major"], "servers": j["servers"]} for j in rep["services"]["java"]]
    out["attention"] = [a for a in rep["attention"] if a["where"] not in ("update", "settings")]
    return out


def report(redacted=False):
    rep = _cached("report", REPORT_TTL, _build)
    return redact(rep) if redacted else rep


def summary(rep):
    """The few numbers a cluster master shows for each node."""
    servers_disk = next((d for d in rep.get("disks", []) if "Game servers" in d.get("holds", [])), None)
    srv = rep.get("servers", [])
    return {"name": rep.get("host", {}).get("name"), "version": rep.get("host", {}).get("version"),
            "update": rep.get("host", {}).get("update"), "uptime": rep.get("host", {}).get("uptime"),
            "cpu_percent": rep.get("cpu", {}).get("percent"), "cores": rep.get("cpu", {}).get("cores"),
            "load": rep.get("cpu", {}).get("load"),
            "memory": {k: rep.get("memory", {}).get(k) for k in ("total", "available", "percent")},
            "disk": servers_disk and {k: servers_disk.get(k) for k in ("mount", "total", "free", "percent")},
            "servers_online": sum(1 for s in srv if s.get("status") == "online"), "servers_total": len(srv),
            "attention": [a for a in rep.get("attention", []) if a.get("level") in ("error", "warn")]}
