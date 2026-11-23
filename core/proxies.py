"""Phantom (github.com/jhead/phantom) proxies installed as systemd units.

Phantom makes a remote Bedrock server show up as a LAN game. Installed as a service next to the
Bedrock server it points at, it gets in the way: Phantom always binds UDP 19132 (plus its own
-bind_port), so it takes the port the server normally uses, and pointed at 127.0.0.1:19132 it
forwards to itself and answers every ping with "Server offline". The panel doesn't create these
units, so it finds them by what they run, whatever they are named.
"""
import glob
import os
import shlex
from core.platform import paths, services, system

UNIT_DIR = paths.UNIT_DIR
PHANTOM_LAN_PORT = 19132          # Phantom binds this in addition to -bind_port
LOCAL_HOSTS = {"127.0.0.1", "localhost", "0.0.0.0", "::1", "[::1]", ""}


def parse_exec_start(unit_text):
    """argv of the unit's ExecStart (first one), or None. Strips systemd's -@:+! prefixes."""
    for line in unit_text.splitlines():
        line = line.strip()
        if line.startswith("ExecStart=") and line[len("ExecStart="):].strip():
            cmd = line[len("ExecStart="):].strip().lstrip("-@:+!")
            try:
                return shlex.split(cmd)
            except ValueError:
                return cmd.split()
    return None


def parse_phantom(argv):
    """Phantom settings from its argv, or None if argv doesn't run phantom."""
    if not argv or "phantom" not in os.path.basename(argv[0]).lower():
        return None
    opts = {}
    i = 1
    while i < len(argv):
        a = argv[i].lstrip("-")
        if "=" in a:
            k, v = a.split("=", 1)
            opts[k] = v
        elif i + 1 < len(argv) and not argv[i + 1].startswith("-"):
            opts[a] = argv[i + 1]
            i += 1
        else:
            opts[a] = True
        i += 1
    server = str(opts.get("server", ""))
    host, _, port = server.rpartition(":")
    try:
        bind_port = int(opts.get("bind_port", 0))
    except (TypeError, ValueError):
        bind_port = 0
    return {
        "target": server,
        "target_host": host,
        "target_port": int(port) if port.isdigit() else None,
        "bind": str(opts.get("bind", "0.0.0.0")),
        "bind_port": bind_port,
    }


def ports_held(p):
    """UDP ports Phantom listens on: always 19132, plus -bind_port when set."""
    return sorted({PHANTOM_LAN_PORT} | ({p["bind_port"]} if p["bind_port"] else set()))


def is_local(host, local_ips=()):
    return host in LOCAL_HOSTS or host.startswith("127.") or host in local_ips


def problems_for(p, server_port, local_ips=()):
    """What's wrong with this Phantom for a Bedrock server on server_port (list of sentences)."""
    out = []
    held = ports_held(p)
    local = is_local(p["target_host"], local_ips)
    if local and p["target_port"] in held:
        out.append(f"It forwards to {p['target']}, a port Phantom itself listens on, "
                   "so it talks to itself and answers players with \"Server offline\".")
    if server_port in held:
        out.append(f"It listens on UDP {server_port}, this server's port, so the server can't use that port.")
    return out


def related_to(p, server_port, local_ips=()):
    """Phantom points at this server, or holds its port."""
    targets_it = is_local(p["target_host"], local_ips) and p["target_port"] == server_port
    return targets_it or server_port in ports_held(p)


def _unit_state(unit):
    def q(*args):
        return services.unit_state(unit, *args) or "unknown"
    return q("is-active"), q("is-enabled")


def _local_ips():
    return tuple(system.addresses())


def find_phantoms(unit_dir=UNIT_DIR, state=_unit_state):
    """Every systemd unit in unit_dir that runs phantom, with its settings and state."""
    found = []
    for path in sorted(glob.glob(os.path.join(unit_dir, "*.service"))):
        try:
            with open(path, "r", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        if "phantom" not in text.lower():
            continue
        p = parse_phantom(parse_exec_start(text))
        if not p:
            continue
        unit = os.path.basename(path)
        active, enabled = state(unit)
        found.append({"unit": unit, "active": active, "enabled": enabled, **p})
    return found


def proxies_for_server(phantoms, server_port, local_ips=None):
    """Phantom units that matter to a Bedrock server on server_port, each with its problems."""
    try:
        port = int(server_port)
    except (TypeError, ValueError):
        return []
    if local_ips is None:
        local_ips = _local_ips()
    out = []
    for p in phantoms:
        if not related_to(p, port, local_ips):
            continue
        out.append({**p, "listens": ports_held(p), "problems": problems_for(p, port, local_ips)})
    return out
