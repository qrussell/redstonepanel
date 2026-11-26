"""The Port check (1.23.1): every port the panel's servers need, next to every port that is
really in use on this machine, and what to do where they clash.

The panel already picks free ports for new servers and moves known mods off shared ports
(core/modports.py). This catches what happens outside that: another program on a server's port
(a Docker container, a server started by hand, another panel), two servers edited by hand onto
one port, a mod the panel doesn't know opening a port, and a panel port a server also wants.

What it does about each one follows one rule: it fixes by itself only what nobody outside this
machine relies on, and only in the panel's own servers.
  automatic   RCON and Query ports (resolve(), every 2 minutes and before a start), known mod
              ports (modports, now also off ports outside programs hold), and a port opened by
              a mod the panel doesn't know when exactly one config line sets it
  one click   a server's game ports (move()): players, the router and the join page use them
  explained   another program's port (named: program, process id, path, command line, its
              systemd unit or Windows service, or its Docker container), never stopped or changed

Listening sockets come from system.listeners() (Linux: /proc; Windows: Get-NetTCPConnection and
Get-NetUDPEndpoint). A start from the panel is refused while another program holds the server's
game port (start_check()), instead of systemd restarting it in a loop.
"""
import json
import os
import re
import threading
import time

from core import manager
from core.platform import services, system
from core.settings import DATA_DIR

STATE = os.path.join(DATA_DIR, "port-check.json")
LISTEN_CACHE = 30                 # seconds a list of listening sockets is reused (dashboard, page)
ATTENTION_AGE = 180               # Needs attention reuses the firewall watcher's list (made every 2 minutes)
_lock = threading.RLock()
_cache = {"at": 0.0, "listeners": None}

GAME_KINDS = ("game", "ipv6", "gameplay")          # players and the router use these: one click
AUTO_KINDS = ("rcon", "query")                     # only the panel and scripts use these: automatic
LABELS = {"game": "game port", "ipv6": "IPv6 game port", "gameplay": "gameplay port", "query": "Query port",
          "rcon": "RCON port", "lan": "LAN discovery port"}
CONFIG_FOLDERS = ("config", "plugins", "mods/config", "world/serverconfig")
CONFIG_EXT = (".yml", ".yaml", ".toml", ".json", ".json5", ".properties", ".conf", ".cfg", ".txt", ".ini", ".hocon")
CONFIG_MAX_FILES = 600
CONFIG_MAX_BYTES = 256 * 1024
PORT_LINE_RE = re.compile(r'^(?P<lead>\s*"?(?P<key>[A-Za-z0-9_.\-]*port[A-Za-z0-9_.\-]*)"?\s*[:=]\s*"?)(?P<port>\d{2,5})'
                          r'(?P<tail>"?\s*,?\s*(#.*)?)$', re.I)


# ---------------------------------------------------------------------------
# What the panel's servers and the panel itself are set to use
# ---------------------------------------------------------------------------
def _flag(props, key):
    return str(props.get(key, "")).strip().lower() == "true"


def server_claims(name, edition, props, mods=None):
    """[{server, kind, key, port, proto, label, shared?, path?}] for one server, from its
    server.properties (and its known mods' config files)."""
    out = []
    port = str(props.get("server-port", "")).strip()
    add = lambda kind, key, p, proto, **kw: out.append(dict({"server": name, "kind": kind, "key": key,
                                                             "port": int(p), "proto": proto,
                                                             "label": LABELS.get(kind, kind)}, **kw))
    if str(edition).lower() == "java":
        if port.isdigit():
            add("game", "server-port", port, "tcp")
        q = str(props.get("query.port", "")).strip() or port
        if _flag(props, "enable-query") and q.isdigit():
            add("query", "query.port", q, "udp")
        r = str(props.get("rcon.port", "")).strip() or "25575"
        if _flag(props, "enable-rcon") and r.isdigit():
            add("rcon", "rcon.port", r, "tcp")
        for c in mods if mods is not None else _mod_claims(name):
            out.append({"server": name, "kind": "mod", "key": c["mod"], "port": int(c["port"]), "proto": c["proto"],
                        "label": c["name"], "path": c["path"]})
        return out
    if not port.isdigit():
        return out
    if manager.is_nethernet(props):
        add("game", "server-port", port, "tcp")
        add("lan", "", manager.NETHERNET_LAN_PORT, "udp", shared=True)    # every Bedrock server shares it
        for spec in manager._udp_port_specs(props.get("server-udp-ports")):
            lo, _, hi = spec.partition(":")
            if not hi or hi == lo:
                add("gameplay", "server-udp-ports", lo, "udp")
            else:
                add("gameplay", "server-udp-ports", lo, "udp", last=int(hi))
    else:
        add("game", "server-port", port, "udp")
        v6 = str(props.get("server-portv6", "")).strip()
        if v6.isdigit():
            add("ipv6", "server-portv6", v6, "udp")
    return out


def _mod_claims(name):
    try:
        from core import modports
        return modports.server_ports(name)
    except Exception:
        return []


def panel_claims():
    """The ports the panel itself listens on: its web page, join pages, SFTP, Phantom."""
    from core import settings as panel_settings
    out = []
    try:
        st = panel_settings.load()
        scheme = panel_settings.RUNTIME.get("scheme") or ("https" if st["https_mode"] != "off" else "http")
        web = panel_settings.RUNTIME.get("port") or (st["https_port"] if scheme == "https" else st["http_port"])
        out.append({"server": None, "kind": "panel", "key": "web", "port": int(web), "proto": "tcp",
                    "label": "the panel's web page"})
        if st.get("join_enabled") and str(st.get("join_port") or "").isdigit():
            out.append({"server": None, "kind": "panel", "key": "join", "port": int(st["join_port"]), "proto": "tcp",
                        "label": "join pages"})
        if st.get("sftp_enabled") and str(st.get("sftp_port") or "").isdigit():
            out.append({"server": None, "kind": "panel", "key": "sftp", "port": int(st["sftp_port"]), "proto": "tcp",
                        "label": "SFTP"})
    except Exception:
        pass
    try:
        for p, q in manager.phantom_firewall_rules():
            out.append({"server": None, "kind": "phantom", "key": "phantom", "port": int(p), "proto": q,
                        "label": "Phantom", "shared": True})
    except Exception:
        pass
    return out


def all_claims(servers=None):
    servers = manager.list_servers() if servers is None else servers
    out = []
    for s in servers:
        try:
            props = manager.read_properties(s["directory"])
            out += server_claims(s["name"], s["edition"], props)
        except Exception:
            continue
    return out + panel_claims()


def _covers(claim, port):
    return claim["port"] <= port <= claim.get("last", claim["port"])


# ---------------------------------------------------------------------------
# What is really listening, and whose it is
# ---------------------------------------------------------------------------
def parse_docker(text):
    """`docker ps --format "{{.Names}}\\t{{.Ports}}"` -> [{"container", "port", "proto", "inside"}]
    for every published port ("0.0.0.0:25565->25565/tcp", ranges "8000-8002->8000-8002/tcp")."""
    out = []
    for line in (text or "").splitlines():
        name, _, ports = line.partition("\t")
        for m in re.finditer(r"(?:[0-9a-fA-F:.\[\]]*:)?(\d+)(?:-(\d+))?->(\d+)(?:-(\d+))?/(tcp|udp)", ports):
            lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
            inside = int(m.group(3))
            for p in range(lo, min(hi, lo + 999) + 1):
                row = {"container": name.strip(), "port": p, "proto": m.group(5), "inside": inside + (p - lo)}
                if row not in out:
                    out.append(row)
    return out


def listeners(fresh=False, max_age=LISTEN_CACHE):
    """system.listeners(), reused for max_age seconds, plus ports Docker publishes."""
    with _lock:
        if not fresh and _cache["listeners"] is not None and time.monotonic() - _cache["at"] < max_age:
            return _cache["listeners"]
    try:
        found = list(system.listeners())
    except Exception:
        found = []
    try:
        docker = parse_docker(system.docker_ps())
    except Exception:
        docker = []
    for d in docker:
        found.append({"proto": d["proto"], "address": "", "port": d["port"], "pid": None, "program": "Docker",
                      "exe": "", "command": "", "service": "", "cwd": "", "user": "",
                      "container": d["container"], "inside": d["inside"]})
    with _lock:
        _cache.update(at=time.monotonic(), listeners=found)
    return found


def _asleep_on(port, proto):
    """The server with sleep mode on whose game port the panel holds (1.26), or None."""
    try:
        from core import sleep
        return sleep.holds(port, proto) or sleep.sleep_ports().get((port, proto))
    except Exception:
        return None


def owner(listener, server_names):
    """Whose a listening socket is: ("server", name), ("panel", None), ("phantom", None) or
    ("other", None)."""
    if listener.get("container"):
        return ("other", None)
    svc = listener.get("service") or ""
    name = svc[:-len(services.SUFFIX)] if services.SUFFIX and svc.endswith(services.SUFFIX) else svc
    if name in server_names:
        return ("server", name)
    if name == "redstonepanel" or name.startswith("redstonepanel-") or listener.get("pid") == os.getpid():
        sleeper = _asleep_on(listener.get("port"), listener.get("proto"))
        if sleeper in server_names:
            return ("server", sleeper)         # sleep mode (1.26): the panel holds it for the server
        return ("panel", None)
    if name.startswith("phantom"):
        return ("phantom", None)
    return ("other", None)


def describe(holder):
    """One line that names the program holding a port, for the admin to find and change it."""
    if holder.get("container"):
        return f"the Docker container {holder['container']} (published port {holder['port']}->{holder.get('inside')}/{holder['proto']})"
    if holder.get("pid") == 4 and (holder.get("program") or "").lower() == "system":
        return ("Windows itself (process 4, usually the HTTP service on behalf of a program that reserved a web "
                "address; see netsh http show urlacl and netsh http show servicestate)")
    if not holder.get("pid"):
        return ("a program the panel can't see (the kernel, or a program in a container; on Linux try  "
                f"sudo ss -lpn 'sport = :{holder['port']}')")
    who = holder.get("program") or os.path.basename(holder.get("exe") or "") or "a program"
    bits = [f"process {holder['pid']}"]
    if holder.get("service"):
        bits.append(("service " if not holder["service"].endswith(".service") else "systemd unit ") + holder["service"])
    if holder.get("user"):
        bits.append(f"user {holder['user']}")
    return f"{who} ({', '.join(bits)})"


def _holder_info(h):
    return {k: h.get(k) for k in ("program", "pid", "exe", "command", "service", "user", "address", "container",
                                  "inside", "cwd")} | {"text": describe(h), "minecraft": _looks_like_minecraft(h)}


def _looks_like_minecraft(h):
    text = " ".join(str(h.get(k) or "") for k in ("program", "exe", "command")).lower()
    return bool(re.search(r"bedrock_server|server\.jar|minecraft|paper|purpur|fabric-server|forge|velocity|"
                          r"bungeecord|waterfall", text))


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------
def _ephemeral():
    try:
        return system.ephemeral_ports()
    except Exception:
        return (49152, 65535)


def report(servers=None, fresh=False, max_age=LISTEN_CACHE):
    """{"problems": [...], "unknown": [...], "checked": time} for this machine.

    problem: {"id", "type", "level", "server", "port", "proto", "kind", "key", "label", "text",
              "fix": "auto" | "move" | "edit" | None, "holders": [...], "with": [...], "path"?}
      type  taken    another program holds a port a server needs
            shared   two servers (or a server and the panel) are set to the same port
    unknown: ports a server's own process opened that it isn't known to need (a mod or plugin)."""
    servers = manager.list_servers() if servers is None else servers
    names = {s["name"] for s in servers}
    status = {s["name"]: s.get("status") for s in servers}
    claims = all_claims(servers)
    held = {}
    for l in listeners(fresh=fresh, max_age=max_age):
        held.setdefault((l["port"], l["proto"]), []).append(l)
    problems = []
    seen = set()

    def holders_of(port, proto, server=None):
        out = []
        for h in held.get((port, proto), []):
            kind, who = owner(h, names)
            if kind == "server" and who == server:
                continue
            if kind == "panel" and server is None:
                continue
            out.append((kind, who, h))
        return out

    # 1. another program (or another server's process) holds a port a server or the panel needs
    for c in claims:
        if c.get("shared") or c.get("last"):
            continue
        others = holders_of(c["port"], c["proto"], c["server"])
        if not others:
            if c["server"] and c["kind"] in GAME_KINDS and unseen_holder(c, held.get((c["port"], c["proto"]), []), names):
                problems.append(_problem("taken", "error", c["server"], c,
                                         f"{c['server']}: its {c['label']} {c['proto'].upper()} {c['port']} can't be "
                                         f"opened: {UNSEEN}. Stop that program, or use Move to a free port here.",
                                         "move", []))
            continue
        outside = [o for o in others if o[0] == "other"]
        mine = [o for o in others if o[0] in ("server", "panel", "phantom")]
        if c["server"] is None:
            if outside:
                who = "; ".join(describe(h) for _, _, h in _dedupe(outside))
                problems.append(_problem("taken", "error", None, c, f"Port {c['port']}/{c['proto']}, which "
                                         f"{c['label']} uses, is held by {who}.", None, outside))
            continue
        if outside:
            who = "; ".join(describe(h) for _, _, h in _dedupe(outside))
            fix = "auto" if c["kind"] in AUTO_KINDS + ("mod",) else "move" if c["kind"] in GAME_KINDS else None
            what = "its " + c["label"] if c["kind"] != "mod" else c["label"]
            text = (f"{c['server']}: {what} is {c['proto'].upper()} {c['port']}, but {who} already uses it. "
                    + ({"auto": "The panel moves it to a free port by itself.",
                        "move": "Stop or change that program, or use Move to a free port here.",
                        None: ""}[fix]))
            problems.append(_problem("taken", "error" if c["kind"] in GAME_KINDS else "warn", c["server"], c,
                                     text.strip(), fix, outside))
        for kind, who, h in mine:
            if kind == "server":
                mine_claims = [x for x in claims if x["server"] == who and x["proto"] == c["proto"] and _covers(x, c["port"])]
                if mine_claims:
                    continue             # set in both servers' files: the "shared" check below says so
                key = ("unknown-clash", who, c["server"], c["port"], c["proto"])
                if key in seen:
                    continue
                seen.add(key)
                where = find_port_lines(who, c["port"])
                fix = "edit" if len(where) == 1 else None
                text = (f"{who} opened {c['proto'].upper()} {c['port']} (a mod or plugin the panel doesn't know), "
                        f"which {c['server']} needs for its {c['label']}. ")
                text += (f"It is set in {who}'s {where[0]['path']} (line {where[0]['line']}); the panel moves it to a "
                         f"free port there. Restart {who}, then start {c['server']}." if fix == "edit" else
                         f"Find the setting in {who}'s config files (Files tab) and give it another port."
                         + (f" Lines with that number: " + ", ".join(f"{w['path']}:{w['line']}" for w in where[:5])
                            + "." if where else ""))
                p = _problem("taken", "error" if c["kind"] in GAME_KINDS else "warn", c["server"], c, text, fix,
                             [(kind, who, h)])
                p.update(by=who, where=where[:5])
                problems.append(p)
            elif kind in ("panel", "phantom") and c["server"]:
                problems.append(_problem("taken", "error", c["server"], c,
                                         f"{c['server']}: its {c['label']} {c['proto'].upper()} {c['port']} is used by "
                                         f"{'the panel itself' if kind == 'panel' else 'Phantom'}."
                                         + (" Use Move to give the server a free port." if c["kind"] in GAME_KINDS else
                                            " The panel moves it to a free port by itself."),
                                         "move" if c["kind"] in GAME_KINDS else "auto", [(kind, who, h)]))

    # 2. two servers, or a server and the panel, set to the same port
    by_port = {}
    for c in claims:
        if c.get("shared") or c["kind"] == "mod":       # mods: modports moves them every 2 minutes
            continue
        by_port.setdefault((c["port"], c["proto"]), []).append(c)
    for (port, proto), cs in sorted(by_port.items()):
        owners = {c["server"] for c in cs}
        if len(owners) < 2:
            continue
        keeper = _keeper(cs, held.get((port, proto), []), names, status)
        for c in cs:
            if c is keeper or c["server"] is None:
                continue
            fix = "auto" if c["kind"] in AUTO_KINDS else "move" if c["kind"] in GAME_KINDS else None
            others = sorted({x["server"] or "the panel" for x in cs if x is not c})
            text = (f"{c['server']}: its {c['label']} is {proto.upper()} {port}, the same as {', '.join(others)}'s, "
                    f"so only one of them can open it. "
                    + ("The panel moves it to a free port by itself." if fix == "auto" else
                       "Use Move to give it a free port."))
            p = _problem("shared", "error" if c["kind"] in GAME_KINDS else "warn", c["server"], c, text, fix, [])
            p["with"] = others
            problems.append(p)

    # 3. ports a server opened that it isn't known to need (a mod or plugin the panel doesn't know)
    lo, hi = _ephemeral()
    unknown = []
    for (port, proto), hs in sorted(held.items()):
        for h in hs:
            kind, who = owner(h, names)
            if kind != "server" or lo <= port <= hi:
                continue
            if any(c["server"] == who and c["proto"] == proto and _covers(c, port) for c in claims):
                continue
            row = {"server": who, "port": port, "proto": proto, "address": h.get("address") or "",
                   "program": h.get("program") or ""}
            if row not in unknown:
                unknown.append(row)
    _learn(unknown)
    dedup = {}
    for p in problems:
        dedup.setdefault(p["id"], p)
    return {"problems": list(dedup.values()), "unknown": unknown, "moved": pending(), "checked": int(time.time())}


UNSEEN = ("another program holds it that the system can't name (often WSL with networkingMode=mirrored, "
          "a Linux server running inside WSL, or Docker publishing the port through firewall rules)")


def unseen_holder(claim, holders, names):
    """True when nothing listed holds the port, the server itself doesn't either, and yet a real
    bind is refused: a holder the socket lists can't see (WSL mirrored networking takes ports in
    the Windows port space without showing in netstat or Get-NetTCPConnection)."""
    if any(owner(h, names) == ("server", claim["server"]) for h in holders):
        return False
    try:
        return not system.can_bind(claim["port"], claim["proto"])
    except Exception:
        return False


def _dedupe(holders):
    out, seen = [], set()
    for kind, who, h in holders:
        k = (h.get("pid"), h.get("container"), h.get("program"))
        if k not in seen:
            seen.add(k)
            out.append((kind, who, h))
    return out


def _problem(type_, level, server, c, text, fix, holders):
    pid = f"{type_}:{server or 'panel'}:{c['key']}:{c['port']}/{c['proto']}"
    return {"id": pid, "type": type_, "level": level, "server": server, "port": c["port"], "proto": c["proto"],
            "kind": c["kind"], "key": c["key"], "label": c["label"], "text": text, "fix": fix,
            "holders": [_holder_info(h) for _, _, h in _dedupe(holders)], "with": []}


def _keeper(cs, holders, names, status):
    """Which of several claims on one port keeps it: the panel, then the server whose process
    holds it now, then a running server, then by name."""
    for c in cs:
        if c["server"] is None:
            return c
    holding = {who for h in holders for kind, who in [owner(h, names)] if kind == "server"}
    return sorted(cs, key=lambda c: (c["server"] not in holding, status.get(c["server"]) != "online",
                                     c["kind"] not in GAME_KINDS, c["server"]))[0]


# ---------------------------------------------------------------------------
# Config files that set a port (for mods the panel doesn't know)
# ---------------------------------------------------------------------------
def find_port_lines(server, port):
    """[{"path", "line", "key"}] for each line in the server's config folders that sets a key
    with "port" in its name to this number. Read as the game account, nothing followed."""
    from core import files
    import stat as st_
    found, count = [], 0
    stack = [p for p in CONFIG_FOLDERS]
    while stack and count < CONFIG_MAX_FILES:
        folder = stack.pop()
        try:
            entries = files.scan(server, folder)
        except Exception:
            continue
        for name, st in entries:
            path = f"{folder}/{name}"
            if st_.S_ISDIR(st.st_mode):
                if path.count("/") < 6:
                    stack.append(path)
                continue
            if not st_.S_ISREG(st.st_mode) or st.st_size > CONFIG_MAX_BYTES or not name.lower().endswith(CONFIG_EXT):
                continue
            count += 1
            try:
                text = files.read_text(server, path)["content"]
            except Exception:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                m = PORT_LINE_RE.match(line)
                if m and int(m.group("port")) == port:
                    found.append({"path": path, "line": i, "key": m.group("key")})
    return sorted(found, key=lambda f: (f["path"], f["line"]))


def _rewrite_line(server, where, new_port, actor):
    from core import files
    f = files.read_text(server, where["path"])
    lines = f["content"].split("\n")
    m = PORT_LINE_RE.match(lines[where["line"] - 1].rstrip("\r"))
    if not m:
        return False
    cr = "\r" if lines[where["line"] - 1].endswith("\r") else ""
    lines[where["line"] - 1] = m.group("lead") + str(new_port) + m.group("tail") + cr
    files.write_text(server, where["path"], "\n".join(lines), expected_modified=f["modified"], actor=actor)
    return True


# ---------------------------------------------------------------------------
# State: moves the panel made (until the server's next start), ports learnt, webhooks sent
# ---------------------------------------------------------------------------
def _load():
    try:
        with open(STATE) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(d):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=1, sort_keys=True)
    os.replace(tmp, STATE)


def _learn(unknown):
    """Remembers ports servers opened through mods the panel doesn't know, so no new server or
    mod is given them (manager._used_ports() counts learned_ports())."""
    with _lock:
        st = _load()
        old = st.get("learned") or {}
        new = {}
        for u in unknown:
            new.setdefault(u["server"], [])
            if [u["port"], u["proto"]] not in new[u["server"]]:
                new[u["server"]].append([u["port"], u["proto"]])
        # a server that is stopped keeps what it opened last time it ran
        for s, ports in old.items():
            if s not in new and s.startswith("minecraft") and not _running(s):
                new[s] = ports
        if new != old:
            st["learned"] = new
            _save(st)


def _running(server):
    try:
        return services.is_active(server)
    except Exception:
        return False


def learned_ports(exclude=None):
    st = _load()
    return {int(p) for s, ps in (st.get("learned") or {}).items() if s != exclude for p, _ in ps
            if str(p).isdigit()}


def pending(server=None):
    st = _load().get("moved") or {}
    return {s: m for s, m in st.items() if m and (server is None or s == server)}


def started(server):
    """The server started: it now uses the ports the panel moved, so Needs attention forgets them."""
    with _lock:
        st = _load()
        if (st.get("moved") or {}).pop(server, None) is not None:
            _save(st)


def _remember_move(server, move):
    with _lock:
        st = _load()
        moved = st.setdefault("moved", {})
        moved[server] = [m for m in moved.get(server, []) if m.get("key") != move["key"]] + [move]
        _save(st)


# ---------------------------------------------------------------------------
# Fixing
# ---------------------------------------------------------------------------
def _taken_ports(rep_claims, held_ports, exclude=None):
    taken = {c["port"] for c in rep_claims if c["server"] != exclude or exclude is None}
    for c in rep_claims:
        if c.get("last"):
            taken.update(range(c["port"], c["last"] + 1))
    return taken | held_ports | learned_ports()


def _free_port(start, taken, step=1, need=1):
    """The first port after start (by step) where `need` ports in a row are free, bindable and
    not reserved by the system."""
    from core import modports
    try:
        reserved = system.reserved_ports()
    except Exception:
        reserved = []
    def ok(p):
        if any(a <= p <= b for _, a, b in reserved):
            return False
        return modports._bindable(p)
    p = start + step
    while p + need - 1 <= 65535:
        if all((p + i) not in taken and ok(p + i) for i in range(need)):
            return p
        p += step
    return None


def resolve(only=None, actor="panel"):
    """Moves RCON and Query ports off ports another server or program uses, and a mod's port off
    one another program holds (core/modports.py), and a port set on exactly one config line of a
    mod the panel doesn't know. Returns the moves made."""
    done = []
    with _lock:
        rep = report(fresh=True)
        servers = {s["name"]: s for s in manager.list_servers()}
        claims = all_claims(list(servers.values()))
        held_ports = {l["port"] for l in listeners()}
        for p in rep["problems"]:
            if p["fix"] not in ("auto", "edit") or (only and p["server"] not in only and p.get("by") not in only):
                continue
            try:
                if p["fix"] == "auto" and p["kind"] in AUTO_KINDS and p["server"] in servers:
                    d = servers[p["server"]]["directory"]
                    new = _free_port(p["port"], _taken_ports(claims, held_ports))
                    if new is None:
                        continue
                    manager.write_properties(d, {p["key"]: str(new)})
                    move = {"key": p["key"], "label": p["label"], "old": p["port"], "new": new, "proto": p["proto"],
                            "why": _why(p), "at": int(time.time())}
                    _remember_move(p["server"], move)
                    claims.append(dict(p, port=new))
                    manager.write_log(f"[PORTS] {p['server']}: its {p['label']} {p['proto'].upper()} {p['port']} was "
                                      f"in use ({_why(p)}); moved it to {new} in server.properties.")
                    done.append(dict(move, server=p["server"]))
                elif p["fix"] == "edit" and p.get("by") in servers and p.get("where"):
                    by = p["by"]
                    new = _free_port(p["port"], _taken_ports(claims, held_ports))
                    if new is None or not _rewrite_line(by, p["where"][0], new, actor):
                        continue
                    move = {"key": p["where"][0]["path"], "label": f"{p['where'][0]['key']} in {p['where'][0]['path']}",
                            "old": p["port"], "new": new, "proto": p["proto"], "why": f"{p['server']} needs it",
                            "at": int(time.time())}
                    _remember_move(by, move)
                    manager.write_log(f"[PORTS] {by}: {p['where'][0]['path']} line {p['where'][0]['line']} set "
                                      f"{p['proto'].upper()} {p['port']}, which {p['server']} needs for its "
                                      f"{p['label']}; changed it to {new}.")
                    done.append(dict(move, server=by))
            except Exception as e:
                manager.write_log(f"[PORTS] Couldn't move {p['server']}'s {p['label']} off {p['port']}: {e}")
    try:                                  # known mods: also off ports outside programs hold
        from core import modports
        outside = {(l["port"]) for l in listeners() if owner(l, set(servers))[0] == "other"}
        done += modports.resolve(only=only, actor=actor, held=outside)
    except Exception as e:
        manager.write_log(f"[PORTS] Checking mod and plugin ports failed: {e}")
    return done


def _why(p):
    if p["holders"]:
        return "held by " + p["holders"][0]["text"]
    if p.get("with"):
        return "also set on " + ", ".join(p["with"])
    return "in use"


def move(server, actor="?", restart=None):
    """Moves a server's game port (and its IPv6, Query and gameplay ports with it) to the next
    free one. A running server is stopped first and started again after (restart=None: if it
    was running). Returns {"server", "old", "new", "proto", "restarted", "note"}."""
    manager._check_server(server)
    s = next((x for x in manager.list_servers() if x["name"] == server), None)
    if not s:
        raise manager.ServerError(f"No Minecraft server called '{server}' on this panel.")
    d = s["directory"]
    props = manager.read_properties(d)
    old = str(props.get("server-port", "")).strip()
    if not old.isdigit():
        raise manager.ServerError(f"{server}'s server.properties has no server-port to move.")
    old = int(old)
    java = s["edition"] == "Java"
    raknet = not java and not manager.is_nethernet(props)
    claims = [c for c in all_claims() if c["server"] != server]
    held = {l["port"] for l in listeners(fresh=True) if owner(l, {x["name"] for x in manager.list_servers()}) != ("server", server)}
    taken = _taken_ports(claims, held) | manager._used_ports(exclude_dir=d)
    new = _free_port(old, taken, step=1 if java else 2, need=2 if raknet else 1)
    if new is None:
        raise manager.ServerError("No free port was found after this one.")
    running = services.is_active(server) or services.unit_state(services.unit(server)) == "activating"
    restart = running if restart is None else restart
    before = manager.server_firewall_rules(server)
    if running:
        services.stop(server)
    changes = {"server-port": str(new)}
    if raknet:
        changes["server-portv6"] = str(new + 1)
    if not java:
        udp = manager._follow_udp_port(props, old, new)
        if udp:
            changes["server-udp-ports"] = udp
    if java and str(props.get("query.port", "")).strip() in ("", str(old)):
        changes["query.port"] = str(new)
    manager.write_properties(d, changes)
    manager.properties_changed(server, before)
    manager.write_log(f"[PORTS] {actor} moved {server} from port {old} to {new}.")
    if restart:
        manager.control_server(server, "start")
    proto = "TCP" if java or not raknet else "UDP"
    note = (f"Players use port {new} now. If your router forwards {proto} {old} to this server, change it to {new}"
            + (" (and the gameplay ports)" if not java and not raknet else "") + ". The join page shows the new "
            f"port by itself.")
    return {"server": server, "old": old, "new": new, "proto": proto.lower(), "restarted": bool(restart), "note": note}


# ---------------------------------------------------------------------------
# Starting, Needs attention, webhooks
# ---------------------------------------------------------------------------
def start_check(server):
    """Before the panel starts a server: moves what it may move by itself, then refuses with
    the reason when another program holds the server's game port."""
    resolve(only=[server])
    names = {s["name"] for s in manager.list_servers()}
    try:
        props = manager.read_properties(manager.get_server_dir(server))
        edition = "Java" if manager._is_java(server) else "Bedrock"
    except Exception:
        return
    found = listeners(fresh=True)
    for c in server_claims(server, edition, props, mods=[]):
        if c["kind"] not in ("game", "ipv6") :
            continue
        others = [l for l in found if l["port"] == c["port"] and l["proto"] == c["proto"]
                  and owner(l, names) != ("server", server)]
        if others:
            raise manager.ServerError(
                f"{server} can't start: its {c['label']} {c['proto'].upper()} {c['port']} is used by "
                f"{describe(others[0])}. Stop or change that program, or move {server} to a free port under "
                f"Port forwarding → Port check.")
        mine = [l for l in found if l["port"] == c["port"] and l["proto"] == c["proto"]]
        if unseen_holder(c, mine, names):
            raise manager.ServerError(
                f"{server} can't start: its {c['label']} {c['proto'].upper()} {c['port']} can't be opened: {UNSEEN}. "
                f"Stop that program, or move {server} to a free port under Port forwarding → Port check.")


def attention(rep=None):
    """Needs attention items: [(level, text, server)]."""
    try:
        rep = report(max_age=ATTENTION_AGE) if rep is None else rep
    except Exception:
        return []
    out = [(p["level"], p["text"], p["server"]) for p in rep["problems"] if p["fix"] != "auto"]
    for server, moves in (rep.get("moved") or {}).items():
        parts = [f"{m['label']} from {m['proto'].upper()} {m['old']} to {m['new']} ({m['why']})" for m in moves]
        out.append(("info", f"The panel moved {server}'s " + "; ".join(parts) + f". Restart {server} to use the new "
                    + ("ports." if len(moves) > 1 else "port."), server))
    return out


def sweep():
    """Every 2 minutes (from manager.sync_firewall): automatic fixes, then a port_conflict
    webhook for each problem that is new."""
    resolve()
    try:
        rep = report()
    except Exception as e:
        manager.write_log(f"[PORTS] The port check failed: {e}")
        return
    with _lock:
        st = _load()
        sent = set(st.get("notified") or [])
        now = {p["id"] for p in rep["problems"] if p["fix"] != "auto"}
        new = [p for p in rep["problems"] if p["id"] in now - sent]
        if now != sent:
            st["notified"] = sorted(now)
            _save(st)
    if new:
        from core import notify
        for p in new:
            try:
                notify.send("port_conflict", p["server"] or "panel", p["text"])
            except Exception:
                pass


def text_report(rep=None):
    """The report as plain text, for  redstonepanel ports."""
    rep = report(fresh=True) if rep is None else rep
    lines = []
    if not rep["problems"]:
        lines.append("No port conflicts found.")
    for p in rep["problems"]:
        lines.append(f"[{p['level'].upper()}] {p['text']}")
        for h in p["holders"]:
            if h.get("exe") or h.get("command"):
                lines.append(f"    program: {h.get('exe') or ''}")
                if h.get("command"):
                    lines.append(f"    command: {h['command']}")
    for s, moves in (rep.get("moved") or {}).items():
        for m in moves:
            lines.append(f"[MOVED] {s}: {m['label']} {m['old']} -> {m['new']} (used from its next start)")
    if rep["unknown"]:
        lines.append("")
        lines.append("Ports servers opened through mods or plugins the panel doesn't know:")
        for u in rep["unknown"]:
            lines.append(f"    {u['server']}: {u['proto'].upper()} {u['port']}")
    claims = all_claims()
    names = {s["name"] for s in manager.list_servers()}
    held = {}
    for l in listeners():
        held.setdefault((l["port"], l["proto"]), []).append(l)
    lines.append("")
    lines.append(f"{'PORT':<12}{'NEEDED BY':<34}{'HELD BY'}")
    for c in sorted(claims, key=lambda c: (c["port"], c["proto"])):
        hs = held.get((c["port"], c["proto"]), [])
        who = ", ".join(sorted({(owner(h, names)[1] or describe(h)) if owner(h, names)[0] == "server" else
                                ("the panel" if owner(h, names)[0] == "panel" else describe(h)) for h in hs})) or "-"
        need = f"{c['server'] or 'panel'} ({c['label']})"
        lines.append(f"{str(c['port']) + '/' + c['proto']:<12}{need:<34}{who}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The router's forwards (asked only when an admin presses Check the router)
# ---------------------------------------------------------------------------
def compare_router(members, mappings):
    """Forwards on the router that don't match what the servers need: [{"level", "text"}].
    members: portforward.overview()["members"]; mappings: portforward.upnp_mappings()."""
    from core import portforward
    out = []
    wanted = []
    ips = set()
    for m in members:
        mine = set(m.get("lan_ips") or []) | ({m["lan_ip"]} if m.get("lan_ip") else set())
        ips |= mine
        for s in m.get("servers") or []:
            for r in s.get("rows") or []:
                if r.get("kind") == "forward":
                    lo, hi = portforward.spec_range(r["port"])
                    wanted.append((m, s, r, lo, hi, mine))
    for mp in mappings:
        hits = [w for w in wanted if w[2]["proto"] == mp["proto"] and w[3] <= mp["outside"] <= w[4]]
        if not hits:
            if mp["inside_ip"] in ips:
                out.append({"level": "info", "text": f"The router forwards {mp['proto'].upper()} {mp['outside']} to "
                            f"{mp['inside_ip']}:{mp['inside']} ({mp['description'] or 'no description'}), but no "
                            f"server here uses that port now. Remove the forward if nothing else needs it."})
            continue
        m, s, r, lo, hi, mine = hits[0]
        inside_lo, _ = portforward.spec_range(r["inside"])
        want_inside = inside_lo + (mp["outside"] - lo)
        member = "" if m.get("local") else f" on {m.get('name')}"
        if mine and mp["inside_ip"] not in mine:
            out.append({"level": "error", "text": f"The router sends {mp['proto'].upper()} {mp['outside']} to "
                        f"{mp['inside_ip']}, but {s['name']}{member} is on {sorted(mine)[0]}. Change the forward's "
                        f"inside address to {sorted(mine)[0]}."})
        elif mp["inside"] != want_inside:
            out.append({"level": "error", "text": f"The router sends {mp['proto'].upper()} {mp['outside']} to port "
                        f"{mp['inside']}, but {s['name']}{member} listens on {want_inside}. Change the forward's "
                        f"inside port to {want_inside}."})
        elif not mp["enabled"]:
            out.append({"level": "warn", "text": f"The router's forward of {mp['proto'].upper()} {mp['outside']} to "
                        f"{s['name']}{member} is turned off."})
    return out


def router_check(actor=None):
    """Reads the router's forwards over UPnP and compares them with every member's servers."""
    from core import portforward
    rep = portforward.overview(actor=actor)
    gw = rep["members"][0].get("gateway") if rep.get("members") else None
    if not gw:
        return {"answered": False, "gateway": None, "problems": [], "mappings": 0,
                "note": "No router (default gateway) was found on this machine."}
    mappings = portforward.upnp_mappings(gw)
    if mappings is None:
        return {"answered": False, "gateway": gw, "problems": [], "mappings": 0,
                "note": f"The router at {gw} didn't answer UPnP, so its forwards can't be read from here. Use Check "
                        f"from here below, or compare the list with the router's own page."}
    return {"answered": True, "gateway": gw, "problems": compare_router(rep["members"], mappings),
            "mappings": len(mappings), "checked": int(time.time()),
            "note": "Only forwards the router lists over UPnP are compared. pfSense lists only those UPnP made, "
                    "not the ones under Firewall > NAT > Port Forward."}
