"""Phantom LAN proxies for Bedrock servers, managed by the panel (admins).

Phantom (github.com/jhead/phantom, MIT) answers the LAN discovery pings consoles broadcast to
UDP 19132 and forwards players to a Bedrock server. A Bedrock server on any other port doesn't
appear under Friends → LAN Games on its own; a Phantom pointed at it makes it appear.

How the panel runs it:
- One systemd unit per server, `phantom-<server>.service`, as the minecraft user, marked
  `X-RedstonePanel-Server=` so the panel knows it made it. Phantom runs on this host and points
  at 127.0.0.1:<server port>, listening on a fixed proxy port (`-bind_port`, 19300 and up) so
  the firewall can allow it.
- The binary is a pinned release, downloaded from GitHub over HTTPS and checked against the
  SHA-256 below (Phantom publishes no checksums), into /usr/local/lib/redstonepanel/phantom/
  (root-owned, readable by the minecraft user; the panel's data folder is root-only).
- Every Phantom also listens on 19132. Several Phantoms share it (they set SO_REUSEPORT), but a
  RakNet Bedrock server on 19132 doesn't, so a Phantom can't run while one does, and the
  reverse. Install refuses in that case and says which server holds the port.
- Phantom 0.5.4 speaks RakNet only. A server with transport=nethernet takes connections over
  TCP (signaling) and WebRTC, which Phantom can't relay, and announces itself to consoles on
  UDP 7551 on its own, so Install refuses for it and says so.
- Units that run phantom but weren't made by the panel (core/proxies.py finds those) can be
  stopped, started and removed here too; their binaries are left alone.
"""
import os
import platform
import re
import shutil
import subprocess    # noqa: F401  (unused here since 1.21; tests replace subprocess.run through this module)

from core import manager, proxies
from core.platform import firewall, paths, services, CommandError

VERSION = "0.5.4"
RELEASE = f"https://github.com/jhead/phantom/releases/download/v{VERSION}"
ASSETS = {      # machine -> (release file, sha256)
    "x86_64": ("phantom-linux", "8677271ac0b2b19b7ea8b526331ddca6bf5e95a91eac3dd8588b7871e810b1ed"),
    "aarch64": ("phantom-linux-arm8", "86ef4ad5f9313d5a1f7e8c73c954dfc066076141eb55844693d54d501248fe85"),
    "armv7l": ("phantom-linux-arm7", "d04f4522e2dd8b693b7f6d9713f7490f8f6f09fecdfcc7d95adea174bd550db6"),
}
HOSTS = {"github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"}
BIN_DIR = paths.PHANTOM_DIR
UNIT_DIR = proxies.UNIT_DIR
MARK = "X-RedstonePanel-Server"
LAN_PORT = proxies.PHANTOM_LAN_PORT
PROXY_PORTS = range(19300, 19400)
ACTIONS = ("start", "stop", "restart")
UNIT_RE = re.compile(r"^[A-Za-z0-9_.@-]+\.service$")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class PhantomError(ValueError):
    """Something that can't be done (message is safe to show)."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def unit_name(server):
    return f"phantom-{server}.service"


def binary_path():
    return os.path.join(BIN_DIR, f"phantom-{VERSION}")


# ---------------------------------------------------------------------------
# The server, and the other servers and Phantoms on this host
# ---------------------------------------------------------------------------
def _bedrock(server, servers=None):
    manager._check_server(server)
    servers = manager.list_servers() if servers is None else servers
    s = next((s for s in servers if s["name"] == server), None)
    if not s or s["edition"] != "Bedrock":
        raise PhantomError("Phantom is for Bedrock servers.")
    try:
        port = int(s["port"])
    except (TypeError, ValueError):
        raise PhantomError(f"{server} has no server-port in server.properties.")
    return s, port


MAX_RANGE = 2000


def _server_ports(servers=None):
    """UDP ports Bedrock servers on this host use: {port: server}. RakNet servers listen on
    server-port and server-portv6; NetherNet servers on server-udp-ports (their server-port is
    TCP) and the shared LAN port 7551."""
    out = {}
    for s in (manager.list_servers() if servers is None else servers):
        if s["edition"] != "Bedrock":
            continue
        props = manager.read_properties(s["directory"])
        if manager.is_nethernet(props):
            for spec in manager._udp_port_specs(props.get("server-udp-ports")):
                lo, _, hi = spec.partition(":")
                for p in range(int(lo), min(int(hi or lo), int(lo) + MAX_RANGE) + 1):
                    out.setdefault(p, s["name"])
            out.setdefault(manager.NETHERNET_LAN_PORT, s["name"])
            continue
        for key in ("server-port", "server-portv6"):
            if str(props.get(key, "")).isdigit():
                out[int(props[key])] = s["name"]
    return out


def _unit_path(unit):
    return os.path.join(UNIT_DIR, unit)


def _read_unit(unit):
    try:
        with open(_unit_path(unit), encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def managed_server(unit_text):
    """The server a panel-made unit belongs to (None for units made by hand)."""
    m = re.search(rf"^{MARK}=(\S+)\s*$", unit_text or "", re.M)
    return m.group(1) if m else None


def _unit_state(unit):
    return proxies._unit_state(unit)


def _phantoms():
    return proxies.find_phantoms(UNIT_DIR, _unit_state)


def _local_ips():
    return proxies._local_ips()


def _systemctl(*args):
    try:
        return services.systemctl(*args, timeout=30)
    except CommandError as e:
        raise PhantomError(f"systemctl failed: {e}", 500)


def _journal(unit, n=15):
    try:
        out = services.log(unit, lines=n, timeout=15)
        lines = (ANSI_RE.sub("", l) for l in out.splitlines())    # Phantom colours its log
        return [l for l in lines if l.strip()][-n:]
    except CommandError:
        return []


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
def status(server):
    """The panel's Phantom for this server, other Phantom units that point at it or hold its
    port, and whether one can be installed."""
    servers = manager.list_servers()
    s, port = _bedrock(server, servers)
    nethernet = manager.is_nethernet(manager.read_properties(s["directory"]))
    phantoms = _phantoms()
    local_ips = _local_ips()
    unit = unit_name(server)
    mine, others = None, []
    for p in phantoms:
        text = _read_unit(p["unit"])
        owner = managed_server(text)
        entry = {**p, "listens": proxies.ports_held(p), "managed": owner is not None,
                 "problems": proxies.problems_for(p, port, local_ips)}
        if p["unit"] == unit and owner == server:
            if p["target_port"] != port:
                entry["problems"].append(f"It points at port {p['target_port']}, but {server} now uses "
                                         f"{port}. Click Repair to point it at the server again.")
            mine = entry
        elif p["unit"] == unit or proxies.related_to(p, port, local_ips):
            others.append(entry)
    blocker = None
    if nethernet:
        blocker = (f"{server} uses the NetherNet transport (Bedrock's default since 1.26.50), and Phantom only relays the "
                   f"older RakNet protocol, so it can't carry this server's players. A NetherNet server "
                   f"announces itself to consoles on your network by itself, on UDP "
                   f"{manager.NETHERNET_LAN_PORT}, which the panel opens in ufw. If it doesn't show under "
                   f"LAN Games, check that the consoles are on the same network as this machine.")
    elif port == LAN_PORT:
        blocker = (f"{server} uses port {LAN_PORT}, so it already shows up as a LAN game and Phantom "
                   f"can't run next to it. Phantom is for servers on other ports.")
    elif not mine and _read_unit(unit) is not None:
        blocker = (f"{unit} already exists but wasn't made by the panel. Remove it below, then install "
                   f"Phantom here so the panel manages it.")
    else:
        holder = _server_ports(servers).get(LAN_PORT)
        if holder:
            blocker = (f"{holder} uses UDP {LAN_PORT}, which every Phantom needs for LAN discovery. Move "
                       f"{holder} to another port (Settings → server-port) to run Phantoms on this machine.")
    if mine:
        mine["logs"] = _journal(unit)
    return {"server": server, "port": port, "version": VERSION, "phantom": mine, "others": others,
            "transport": "nethernet" if nethernet else "raknet",
            "can_install": blocker is None and machine_supported(), "blocker": blocker or (
                None if machine_supported() else f"Phantom has no build for this machine ({platform.machine()}).")}


def machine_supported():
    return platform.machine() in ASSETS


# ---------------------------------------------------------------------------
# Install, repair, uninstall
# ---------------------------------------------------------------------------
def ensure_binary():
    """Downloads the pinned Phantom release once. Returns its path."""
    path = binary_path()
    if os.path.isfile(path) and os.access(path, os.X_OK):
        return path
    arch = platform.machine()
    if arch not in ASSETS:
        raise PhantomError(f"Phantom has no build for this machine ({arch}).")
    name, sha256 = ASSETS[arch]
    from core import sources
    f = sources.download(f"{RELEASE}/{name}", HOSTS, "sha256", sha256)
    try:
        os.makedirs(BIN_DIR, mode=0o755, exist_ok=True)
        tmp = path + ".part"
        with open(tmp, "wb") as out:
            shutil.copyfileobj(f, out)
        os.chmod(tmp, 0o755)
        os.replace(tmp, path)
    finally:
        f.close()
    manager.write_log(f"[PHANTOM] Downloaded Phantom {VERSION} ({name}, SHA-256 checked) to {path}.")
    return path


def _free_proxy_port(server, current=None):
    used = set(_server_ports())
    for p in _phantoms():
        if p["unit"] != unit_name(server) and p["bind_port"]:
            used.add(p["bind_port"])
    if current and current not in used:
        return current
    for port in PROXY_PORTS:
        if port not in used:
            return port
    raise PhantomError("No free proxy port between 19300 and 19399.")


def _unit_text(server, port, bind_port, binary):
    return f"""[Unit]
Description=Phantom LAN proxy for {server} (RedstonePanel)
After=network-online.target {server}.service
Wants=network-online.target
{MARK}={server}

[Service]
User=minecraft
Group=minecraft
ExecStart={binary} -server 127.0.0.1:{port} -bind_port {bind_port}
Restart=on-failure
RestartSec=15
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
"""


def install(server, actor="admin"):
    """Installs (or repairs) the server's Phantom and starts it. Returns the status."""
    st = status(server)
    if not st["can_install"]:
        raise PhantomError(st["blocker"])
    port = st["port"]
    unit = unit_name(server)
    existing = _read_unit(unit)
    if existing is not None and managed_server(existing) != server:
        raise PhantomError(f"{unit} already exists and wasn't made by the panel; remove it first.")
    current = (st["phantom"] or {}).get("bind_port")
    bind_port = _free_proxy_port(server, current)
    binary = ensure_binary()
    with open(_unit_path(unit), "w", encoding="utf-8") as f:
        f.write(_unit_text(server, port, bind_port, binary))
    services.reload()
    for p, proto in ((LAN_PORT, "udp"), (bind_port, "udp")):
        manager.open_firewall(p, proto)
    services.control(unit, "enable")
    services.control(unit, "restart")
    what = "Repaired" if existing is not None else "Installed"
    manager.write_log(f"[PHANTOM] {actor}: {what} Phantom for {server} (forwards to port {port}; listens on UDP "
                      f"{LAN_PORT} and {bind_port}).")
    return status(server)


def _target(server, unit):
    """A Phantom unit this server's tab may control: its own, or one that points at it or holds
    its port (made by hand)."""
    if not UNIT_RE.match(unit or ""):
        raise PhantomError("Not a service name.")
    st = status(server)
    if st["phantom"] and st["phantom"]["unit"] == unit:
        return st["phantom"]
    p = next((p for p in st["others"] if p["unit"] == unit), None)
    if not p:
        raise PhantomError(f"{unit} isn't a Phantom for {server}.", 404)
    return p


def control(server, unit, action, actor="admin"):
    if action not in ACTIONS:
        raise PhantomError(f"Unknown action '{action}'.")
    _target(server, unit)
    services.control(unit, action)
    manager.write_log(f"[PHANTOM] {actor}: {action} {unit}.")
    return status(server)


def uninstall(server, unit=None, actor="admin"):
    """Stops and removes a Phantom unit (the panel's own by default). The binary stays for other
    servers; a unit made by hand loses only its unit file."""
    unit = unit or unit_name(server)
    p = _target(server, unit)
    _remove_unit(unit, p.get("bind_port"))
    manager.write_log(f"[PHANTOM] {actor}: removed {unit}.")
    return status(server)


def _remove_unit(unit, bind_port=None):
    _systemctl("stop", unit)
    _systemctl("disable", unit)
    try:
        os.remove(_unit_path(unit))
    except FileNotFoundError:
        pass
    services.reload()
    _systemctl("reset-failed", unit)
    remaining = _phantoms()
    if bind_port and not any(p["bind_port"] == bind_port for p in remaining):
        _close(bind_port)
    if not remaining and LAN_PORT not in _server_ports():
        _close(LAN_PORT)


def _close(port):
    if firewall.installed():
        firewall.remove(port, "udp")
        manager._firewall_changed()


def on_server_removed(server):
    """The server is leaving the panel: its Phantom goes too (units made by hand stay)."""
    unit = unit_name(server)
    text = _read_unit(unit)
    if text is None or managed_server(text) != server:
        return
    p = proxies.parse_phantom(proxies.parse_exec_start(text)) or {}
    _remove_unit(unit, p.get("bind_port"))
    manager.write_log(f"[PHANTOM] Removed {unit} with {server}.")
