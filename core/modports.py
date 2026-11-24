"""Ports that mods and plugins open next to the game port (1.21.x).

Simple Voice Chat, Geyser, BlueMap, Dynmap and others each open a port of their own, with the
same default on every server. Two servers on one machine with the same mod then fight over it:
the second one can't open it (Simple Voice Chat on Fabric even stops the whole server, so systemd
restarts it in a loop).

KNOWN lists where each mod keeps its port. The panel reads those files (never through a
symlink, as the game account), and:
  - counts the ports as used, so a new server or another mod never gets them;
  - opens them in the firewall with the server's game port (manager.server_firewall_rules);
  - when two servers (or a mod and any game port) want the same port, moves the one that
    doesn't hold it now to the next free port, in its own config file (resolve()). A server
    whose port moved uses the new one from its next start; Needs attention says so until then.

Mods that aren't in KNOWN are left alone; if one fails to bind, the crash card (core/crashes.py)
says a mod's port was taken and names the port when the log does.
"""

import json
import os
import re
import socket
import threading
import time

from core import manager
from core.platform import services
from core.settings import DATA_DIR

STATE = os.path.join(DATA_DIR, "mod-ports.json")
MIN_PORT, MAX_PORT = 1024, 65535
_lock = threading.Lock()

# id: (name, config files (first found is used), format, key path, protocol, default port, firewall/forward kind,
#      what it's for). Formats: "properties" (key=value), "yaml" (key: value, nested by indent), "hocon"
#      (top-level key: value or key = value).
KNOWN = {
    "voicechat": ("Simple Voice Chat", ("config/voicechat/voicechat-server.properties",
                                        "plugins/voicechat/voicechat-server.properties"),
                  "properties", ("port",), "udp", 24454, "forward", "Voice chat for players outside."),
    "geyser": ("Geyser (Bedrock players)", ("plugins/Geyser-Spigot/config.yml", "plugins/Geyser-Velocity/config.yml",
                                            "config/Geyser-Fabric/config.yml", "config/geyser/config.yml",
                                            "plugins/geyser/config.yml"),
               "yaml", ("bedrock", "port"), "udp", 19132, "forward", "Bedrock players join this Java server through Geyser."),
    "bluemap": ("BlueMap web map", ("config/bluemap/webserver.conf", "plugins/BlueMap/webserver.conf"),
                "hocon", ("port",), "tcp", 8100, "optional", "Only if players should open the web map from outside."),
    "dynmap": ("Dynmap web map", ("plugins/dynmap/configuration.txt", "dynmap/configuration.txt"),
               "yaml", ("webserver-port",), "tcp", 8123, "optional", "Only if players should open the web map from outside."),
    "plan": ("Plan web page", ("plugins/Plan/config.yml",),
             "yaml", ("Webserver", "Port"), "tcp", 8804, "optional", "Only if the statistics page should be reachable from outside."),
    "votifier": ("NuVotifier", ("plugins/nuvotifier/config.yml", "plugins/Votifier/config.yml"),
                 "yaml", ("port",), "tcp", 8192, "optional", "Only if server-list websites send votes."),
}


# ---------------------------------------------------------------------------
# Reading and changing one config file's port line (text in, text out)
# ---------------------------------------------------------------------------
def _find(text, fmt, key):
    """(line index, port text) of the key's value, or (None, None). Nested yaml keys are matched
    one indent level at a time; comments and blank lines are skipped."""
    lines = (text or "").splitlines()
    if fmt == "properties":
        for i, line in enumerate(lines):
            k, sep, v = line.partition("=")
            if sep and not line.lstrip().startswith(("#", "!")) and k.strip() == key[0]:
                return i, v.strip()
        return None, None
    stack = []                                    # (indent, key) of the blocks we're in
    for i, line in enumerate(lines):
        bare = line.strip()
        if not bare or bare.startswith(("#", "//")):
            continue
        indent = len(line) - len(line.lstrip())
        while stack and indent <= stack[-1][0]:
            stack.pop()
        m = re.match(r"(\"[^\"]+\"|[A-Za-z0-9_.-]+)\s*[:=]\s*(.*)$", bare)
        if not m:
            continue
        name, val = m.group(1).strip('"'), m.group(2)
        if [k for _, k in stack] + [name] == list(key):
            return i, re.split(r"\s+#|\s+//", val, maxsplit=1)[0].strip().strip("'\"")
        if val.strip() in ("", "{"):
            stack.append((indent, name))
    return None, None


def _replace(text, idx, new_port):
    """The text with the number on line idx changed to new_port (the rest of the line kept)."""
    lines = text.split("\n")
    line = lines[idx]
    sep = re.search(r"[=:]", line)
    head, tail = line[:sep.end()], line[sep.end():]
    lines[idx] = head + re.sub(r"-?\d+", str(new_port), tail, count=1)
    return "\n".join(lines)


def _skip(mod, text, port_text):
    """True when the mod's port isn't its own: voice chat's -1 and Geyser's clone-remote-port use
    the game port (another protocol on the same number)."""
    if mod == "voicechat" and port_text == "-1":
        return True
    if mod == "geyser":
        _, clone = _find(text, "yaml", ("bedrock", "clone-remote-port"))
        return (clone or "").lower() == "true"
    return False


# ---------------------------------------------------------------------------
# What every server on this machine claims
# ---------------------------------------------------------------------------
def _read(server, path):
    from core import files
    try:
        return files.read_text(server, path)
    except Exception:
        return None


def server_ports(server):
    """[{mod, name, path, port, proto, kind, note}] for the known mods with a config file in this
    server's folder. A config without the key means the mod's default."""
    out = []
    for mod, (name, paths_, fmt, key, proto, default, kind, note) in KNOWN.items():
        for p in paths_:
            f = _read(server, p)
            if f is None:
                continue
            _, val = _find(f["content"], fmt, key)
            if not _skip(mod, f["content"], val):
                port = int(val) if (val or "").isdigit() else default
                out.append({"mod": mod, "name": name, "path": p, "port": port, "proto": proto,
                            "kind": kind, "note": note})
            break
    return out


def firewall_rules(server):
    """The (port, proto) rules the server's mods need, as manager.firewall_rules gives them."""
    return {(str(c["port"]), c["proto"]) for c in server_ports(server)}


def _servers():
    return sorted(u.replace(services.SUFFIX, "") for u in services.unit_files("minecraft"))


def claims():
    """{server: [claim, ...]} of every Java server on this panel."""
    out = {}
    for s in _servers():
        try:
            if manager._is_java(s):
                out[s] = server_ports(s)
        except Exception:
            continue
    return out


def used_ports(all_claims=None):
    """Every port a mod of any server on this panel is set to use."""
    all_claims = claims() if all_claims is None else all_claims
    return {c["port"] for cs in all_claims.values() for c in cs}


def _bindable(port):
    """True if nothing on this machine listens on the port right now, over TCP and UDP."""
    for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
        with socket.socket(socket.AF_INET, kind) as s:
            try:
                s.bind(("0.0.0.0", port))
            except OSError:
                return False
    return True


def next_free(start, taken, bindable=None):
    """The first port after start that no server or mod uses and nothing on this machine holds."""
    bindable = bindable or _bindable
    p = start + 1
    while p <= MAX_PORT:
        if p not in taken and bindable(p):
            return p
        p += 1
    p = MIN_PORT
    while p < start:
        if p not in taken and bindable(p):
            return p
        p += 1
    return None


VOICE_FAIL_RE = re.compile(r"Failed to run voice chat at (?:UDP|TCP) port (\d+)")


def _failed_here(server, port):
    """True when the server's current log shows its voice chat couldn't open this port."""
    try:
        from core import crashes
        text, _ = crashes._read_tail(manager.get_server_dir(server), "logs/latest.log")
    except Exception:
        return False
    return any(int(m.group(1)) == port for m in VOICE_FAIL_RE.finditer(text or ""))


def _active(server):
    try:
        return services.unit_state(server) == "active"
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Moving clashing ports
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


def _keeper_order(server, port):
    """Who keeps a shared port: a running server that opened it, then a stopped one, then one
    whose log shows it couldn't open it; by name within each."""
    if _failed_here(server, port):
        return (2, server)
    return (0 if _active(server) else 1, server)


def plan(all_claims, game_ports, bindable=None, keeper=None):
    """[(server, claim, new port)] that make every mod port unique: no two servers' mods on
    one port, and no mod on a port a server uses for the game (or RCON/query). Pure apart from
    the bindable() check of new ports."""
    keeper = keeper or _keeper_order
    taken = set(game_ports) | {c["port"] for cs in all_claims.values() for c in cs}
    by_port = {}
    for s, cs in all_claims.items():
        for c in cs:
            by_port.setdefault(c["port"], []).append((s, c))
    moves = []
    for port in sorted(by_port):
        users = sorted(by_port[port], key=lambda sc: keeper(sc[0], port))
        losers = users if port in game_ports else users[1:]
        for s, c in losers:
            new = next_free(port, taken, bindable)
            if new is None:
                continue
            taken.add(new)
            moves.append((s, c, new))
    return moves


def resolve(only=None, actor="panel"):
    """Moves every clashing mod port to the next free one (see plan()). only: just the moves of
    these servers (the others are still counted). Returns the moves made, as
    [{server, mod, name, path, old, new, proto, at}], and remembers them for Needs attention."""
    from core import files
    with _lock:
        all_claims = claims()
        if not any(all_claims.values()):
            return []
        game = manager._used_ports(mods=False)
        done = []
        for s, c, new in plan(all_claims, game):
            if only and s not in only:
                continue
            fmt, key = KNOWN[c["mod"]][2], KNOWN[c["mod"]][3]
            f = _read(s, c["path"])
            if f is None:
                continue
            idx, _ = _find(f["content"], fmt, key)
            if idx is None:                       # the default is used: write the key
                content = f["content"].rstrip("\n") + "\n" + _new_line(fmt, key, new) + "\n" if \
                    len(key) == 1 else None
                if content is None:
                    continue                      # a nested key we can't add safely
            else:
                content = _replace(f["content"], idx, new)
            try:
                files.write_text(s, c["path"], content, expected_modified=f["modified"], actor=actor)
            except Exception as e:
                manager.write_log(f"[PORTS] Couldn't move {c['name']} of {s} off port {c['port']}: {e}")
                continue
            others = [o for o, cs in all_claims.items() for x in cs if x["port"] == c["port"] and o != s]
            manager.write_log(f"[PORTS] {s}: {c['name']} was set to {c['proto'].upper()} {c['port']}, which "
                              + (f"{', '.join(others)} also use" if others else "a server uses for the game")
                              + f"; moved it to {new} in {c['path']}.")
            done.append({"server": s, "mod": c["mod"], "name": c["name"], "path": c["path"], "old": c["port"],
                         "new": new, "proto": c["proto"], "with": others, "at": int(time.time())})
        if done:
            st = _load()
            for m in done:
                st.setdefault(m["server"], [])
                st[m["server"]] = [x for x in st[m["server"]] if x.get("mod") != m["mod"]] + [m]
            _save(st)
    return done


def _new_line(fmt, key, port):
    return f"{key[0]}={port}" if fmt == "properties" else f"{key[0]}: {port}"


def started(server):
    """The server started: it now uses its moved ports, so Needs attention forgets them."""
    with _lock:
        st = _load()
        if st.pop(server, None) is not None:
            _save(st)


def pending(server=None):
    """{server: [move, ...]} of moves the server hasn't started with yet."""
    st = _load()
    return {s: m for s, m in st.items() if m and (server is None or s == server)}


def attention_text(server, moves):
    parts = [f"{m['name']} from {m['proto'].upper()} {m['old']}"
             + (f" (also set on {', '.join(m['with'])})" if m.get("with") else "")
             + f" to {m['new']}" for m in moves]
    return (f"The panel moved {server}'s " + "; ".join(parts) + ", so its servers no longer share a port. "
            f"Restart {server} to use the new port" + ("s" if len(moves) > 1 else "") +
            ", and forward it on your router if players join from outside.")
