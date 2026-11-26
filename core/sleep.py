"""Sleep mode (1.26): a server nobody plays on stops by itself and starts when someone joins.

Per server, off by default (the server's Settings tab, or PUT /api/servers/NAME/sleep). With it on:

  * the panel asks the running server how many players are online every CHECK_EVERY seconds
    (the Server List Ping for Java, the RakNet ping for Bedrock, the player log for NetherNet
    Bedrock); after `minutes` with nobody on, it stops the server and the server is *asleep*;
  * while asleep the panel itself listens on the server's game port, on this machine, in this
    process (no service, no program of its own, so Linux and Windows work the same):
      - Java (TCP): the server list shows the server's MOTD with "Sleeping: join to start it",
        its icon and 0 players. Joining starts the server and tells the player to join again in
        about 30 seconds. Players the whitelist or ban list would refuse are told so and don't
        wake it.
      - Bedrock over RakNet (UDP server-port and server-portv6): the server list shows it as
        sleeping; the first connection attempt starts it.
      - Bedrock over NetherNet (TCP server-port, the default since BDS 1.26.50): there is no
        status to answer, so the first connection that sends anything starts it.
    The listener lets go of the port before the server starts: every start or restart through
    the platform layer calls before_start() first, and the loop notices a start from outside
    the panel within WATCH_EVERY seconds.
  * Start at boot (1.25): a server with sleep mode on is never started by the system at boot.
    After the machine starts, the panel puts it to sleep instead if Start at boot is on, and
    leaves it stopped if it's off.
  * Stop (by hand, from the panel or the API) means stopped: no listener until the next Start
    or "Sleep now". A crash is the same.

State lives in DATA_DIR/sleep.json (root-only), so the notify-unit and `redstonepanel ports`
processes can see which servers are asleep:

    {"servers": {"minecraft-x": {"minutes": 15}},
     "state": {"minecraft-x": {"state": "asleep" | "awake" | "off", "at": time, "boot": boot time,
                               "pong": [bedrock pong fields], "why": text}}}

Cluster nodes do this on their own; the master only shows what the node's server list says.
"""
import json
import os
import re
import select
import socket
import struct
import threading
import time

from core.settings import DATA_DIR
from core import platform
from core.platform import services, system

SLEEP_FILE = os.path.join(DATA_DIR, "sleep.json")
DEFAULT_MINUTES = 15
MIN_MINUTES = 1
MAX_MINUTES = 24 * 60
CHECK_EVERY = 30                # seconds between player counts of awake servers
WATCH_EVERY = 2                 # seconds between looks at asleep servers' units (started from outside?)
CONNECT_TIMEOUT = 5             # a Java client gets this long to say what it wants
MAX_CLIENTS = 32                # Java connections handled at once; more are closed
BOOT_SLACK = 5                  # seconds two Windows boot times may differ and still be the same boot
BOOT_SLACK_EXACT = 0.5         # the same elsewhere: Linux reads it exactly from the kernel
RAKNET_MAGIC = bytes.fromhex("00ffff00fefefefefdfdfdfd12345678")
NAME_RE = re.compile(r"^minecraft[A-Za-z0-9_.@-]*$")

_lock = threading.RLock()
_listeners = {}                 # server -> _Listener
_empty_since = {}               # server -> when it was first seen with nobody on
_clients = threading.BoundedSemaphore(MAX_CLIENTS)
_thread = None
_guid = struct.unpack(">q", os.urandom(8))[0]


class SleepError(ValueError):
    """A sleep mode change that can't be done (message is safe to show)."""


def _log(text):
    from core import manager
    manager.write_log(text)


# ---------------------------------------------------------------------------
# Saved settings and state
# ---------------------------------------------------------------------------
def _load():
    try:
        with open(SLEEP_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    servers = data.get("servers") if isinstance(data.get("servers"), dict) else {}
    state = data.get("state") if isinstance(data.get("state"), dict) else {}
    return {"servers": {k: v for k, v in servers.items() if isinstance(k, str) and isinstance(v, dict)},
            "state": {k: v for k, v in state.items() if isinstance(k, str) and isinstance(v, dict)}}


def _save(data):
    os.makedirs(os.path.dirname(SLEEP_FILE), exist_ok=True)
    tmp = SLEEP_FILE + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, SLEEP_FILE)


def _minutes(value):
    try:
        m = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MINUTES
    return min(MAX_MINUTES, max(MIN_MINUTES, m))


def enabled(server):
    """True when sleep mode is on for the server."""
    return server in _load()["servers"]


def minutes(server):
    return _minutes(_load()["servers"].get(server, {}).get("minutes", DEFAULT_MINUTES))


def state(server):
    """"asleep", "awake", "off" or None (sleep mode off, or never seen)."""
    data = _load()
    if server not in data["servers"]:
        return None
    return data["state"].get(server, {}).get("state")


def is_asleep(server):
    return state(server) == "asleep"


def asleep_servers():
    """Names of the servers asleep now (readable by any of the panel's processes)."""
    data = _load()
    return sorted(s for s, st in data["state"].items() if st.get("state") == "asleep" and s in data["servers"])


def _set_state(server, new, why=None, **extra):
    with _lock:
        data = _load()
        st = data["state"].get(server, {})
        boot = _boot_time()
        if st.get("state") == new and not extra and why is None and _same_boot(st.get("boot"), boot):
            return
        if st.get("state") != new:
            st["at"] = int(time.time())
        st.update(state=new, boot=boot, **extra)
        if why is not None:
            st["why"] = why
        data["state"][server] = st
        _save(data)


def _remember_pong(server, pong):
    with _lock:
        data = _load()
        st = data["state"].get(server)
        if st is None or st.get("pong") == pong:
            return
        st["pong"] = pong
        _save(data)


def _boot_time():
    try:
        return system.boot_time()
    except Exception:
        return None


def info(server):
    """What the server's Settings tab shows."""
    data = _load()
    on = server in data["servers"]
    st = data["state"].get(server, {}) if on else {}
    with _lock:
        lst = _listeners.get(server)
    since = _empty_since.get(server)
    return {"enabled": on, "minutes": _minutes(data["servers"].get(server, {}).get("minutes", DEFAULT_MINUTES)),
            "state": st.get("state") if on else None, "since": st.get("at") if on else None,
            "listening": bool(lst and lst.socks), "error": (lst.error if lst else None) if on else None,
            "empty_since": int(since) if on and since else None, "why_not": why_not(server),
            "min": MIN_MINUTES, "max": MAX_MINUTES, "default": DEFAULT_MINUTES}


def list_fields(server):
    """What manager.list_servers() adds to a server's row: {"sleep": bool, "sleeping": bool}."""
    data = _load()
    on = server in data["servers"]
    return {"sleep": on, "sleeping": on and data["state"].get(server, {}).get("state") == "asleep"}


def why_not(server):
    """Why sleep mode can't be used for this server, or None."""
    try:
        from core import manager, proxies
        d = manager.get_server_dir(server)
        props = manager.read_properties(d)
        if not manager._is_java(server):
            port = str(props.get("server-port", "19132"))
            if proxies.proxies_for_server(proxies.find_phantoms(), port):
                return ("Phantom (for consoles) forwards to this server. Sleep mode isn't tested with Phantom yet, "
                        "so it is off for servers that use it.")
    except Exception:
        return None
    return None


def turn(server, on, mins=None, actor="?"):
    """Turns sleep mode on or off for a server and saves the minutes. Returns info()."""
    from core import bootstart, manager
    manager._check_server(server)
    if on:
        why = why_not(server)
        if why:
            raise SleepError(why)
    m = _minutes(mins if mins is not None else minutes(server))
    with _lock:
        data = _load()
        was = server in data["servers"]
        if on:
            data["servers"][server] = {"minutes": m}
            if not was:
                data["state"][server] = {"state": "awake" if _active(server) else "off", "at": int(time.time()),
                                         "boot": _boot_time()}
        else:
            data["servers"].pop(server, None)
            data["state"].pop(server, None)
        _save(data)
    _empty_since.pop(server, None)
    if not on:
        _close(server)
    # Start at boot: the system never starts a server with sleep mode on; the panel does
    bootstart.apply(server, quiet=True)
    if on != was:
        _log(f"[SLEEP] {actor} turned sleep mode {'on' if on else 'off'} for {server}"
             + (f" (after {m} minute{'s' if m != 1 else ''} with nobody on)." if on else "."))
    elif on:
        _log(f"[SLEEP] {actor} set {server} to sleep after {m} minute{'s' if m != 1 else ''} with nobody on.")
    return info(server)


def forget(server):
    """A server deleted for good, or uninstalled: nothing left behind for a new one of the name."""
    _close(server)
    _empty_since.pop(server, None)
    with _lock:
        data = _load()
        if data["servers"].pop(server, None) is not None or data["state"].pop(server, None) is not None:
            _save(data)


def remember(server, mins):
    """A server set up from a move or a restore with sleep mode on (mins) or off (None)."""
    with _lock:
        data = _load()
        if mins is None:
            data["servers"].pop(server, None)
            data["state"].pop(server, None)
        else:
            data["servers"][server] = {"minutes": _minutes(mins)}
        _save(data)


# ---------------------------------------------------------------------------
# Hooks: starts, Stop by hand
# ---------------------------------------------------------------------------
def _unit_server(unit_name):
    suffix = services.SUFFIX or ""
    name = unit_name[:-len(suffix)] if suffix and unit_name.endswith(suffix) else unit_name
    return name if NAME_RE.match(name or "") else None


def before_start(unit_name):
    """Anything starting the server (the panel, a schedule in this process, a wake): let go of
    the port first. The server is awake from now on."""
    server = _unit_server(unit_name)
    if not server:
        return
    with _lock:
        lst = _listeners.get(server)
    if lst is None and not enabled(server):
        return
    _close(server)
    _empty_since.pop(server, None)
    if enabled(server):
        _set_state(server, "awake")


def stopped_by_hand(server, actor=None):
    """Stop from the panel or the API: stopped means stopped, until the next Start or Sleep now."""
    if not enabled(server):
        return
    _close(server)
    _empty_since.pop(server, None)
    if state(server) == "asleep" and actor:
        _log(f"[SLEEP] {actor} stopped {server} while it was asleep: it no longer wakes when a player joins.")
    _set_state(server, "off")


def sleep_now(server, actor="?"):
    """Puts a server with sleep mode on to sleep now (stopping it first if it runs)."""
    from core import manager
    manager._check_server(server)
    if not enabled(server):
        raise SleepError(f"Sleep mode is off for {server}. Turn it on first.")
    busy = _busy(server)
    if busy:
        raise SleepError(busy)
    if server in _going:
        return
    _going.add(server)
    threading.Thread(target=_sleep_thread, args=(server, f"{actor} put {server} to sleep"), name=f"sleep-{server}",
                     daemon=True).start()


# ---------------------------------------------------------------------------
# Going to sleep, waking up
# ---------------------------------------------------------------------------
def _active(server):
    try:
        return services.is_active(server)
    except Exception:
        return False


def _busy(server):
    """Why the server shouldn't be stopped for sleeping right now, or None."""
    try:
        from core import transfer
        if transfer.is_moving(server):
            return f"{server} is being moved to another cluster member."
    except Exception:
        pass
    try:
        from core import convert
        if convert._busy(server):
            return f"{server}'s type or version is being changed."
    except Exception:
        pass
    return None


def _put_to_sleep(server, why):
    """Stops the server if it runs (never in the middle of a backup) and listens for it."""
    from core import backups
    fd = None
    if _active(server):
        try:
            fd = backups._lock(server)              # no backup runs while it stops, none starts meanwhile
        except Exception as e:
            raise SleepError(f"{server} wasn't put to sleep: {e}")
    try:
        _set_state(server, "asleep", why=why)           # before the stop, so the stop message says why
        if _active(server):
            _log(f"[SLEEP] {why}: stopping it. It starts again when a player joins.")
            services.stop(server, elevate=True, quiet=True)
            for _ in range(120):
                if not _active(server):
                    break
                time.sleep(1)
        else:
            _log(f"[SLEEP] {why}. It starts when a player joins.")
    finally:
        if fd is not None:
            os.close(fd)
    _empty_since.pop(server, None)
    _open(server)


def wake(server, who="a player"):
    """Someone wants to play: let go of the port and start the server. True if it was asleep."""
    with _lock:
        if server not in _listeners and not is_asleep(server):
            return False
        _close(server)
    _set_state(server, "awake")
    _log(f"[SLEEP] {who} wants to play on {server}: starting it.")

    def run():
        from core import manager
        try:
            manager.control_server(server, "start")
        except Exception as e:
            _log(f"[SLEEP] Couldn't wake {server}: {e}. It goes back to sleep.")
            _set_state(server, "asleep", why=f"waking it failed: {e}")
            _open(server)
    threading.Thread(target=run, name=f"wake-{server}", daemon=True).start()
    return True


# ---------------------------------------------------------------------------
# Who is online
# ---------------------------------------------------------------------------
def _bedrock_pong(host, port, timeout=2):
    """The RakNet pong's fields: ["MCPE", motd, protocol, version, online, max, ...] or raises OSError."""
    fam = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(fam, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        s.sendto(b"\x01" + struct.pack(">Q", int(time.time() * 1000) & (2 ** 63 - 1)) + RAKNET_MAGIC
                 + os.urandom(8), (host, int(port)))
        data, _ = s.recvfrom(2048)
    if not data or data[0] != 0x1c or RAKNET_MAGIC not in data:
        raise OSError("not a Bedrock server")
    i = data.index(RAKNET_MAGIC) + len(RAKNET_MAGIC)
    n = struct.unpack(">H", data[i:i + 2])[0]
    return data[i + 2:i + 2 + n].decode("utf-8", "replace").split(";")


def players_online(server):
    """How many players are on the running server, or None when it can't tell (still starting,
    or not answering): None never counts as empty."""
    from core import joinpage, manager, moderation
    d = manager.get_server_dir(server)
    props = manager.read_properties(d)
    java = manager._is_java(server)
    host = joinpage._bind_host(props)
    port = str(props.get("server-port", "25565" if java else "19132")).strip()
    try:
        if java:
            return joinpage.java_ping(host, port)["online"]
        if manager.bedrock_transport(props) == "raknet":
            parts = _bedrock_pong(host, port)
            _remember_pong(server, parts[:12])
            return int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else None
    except (OSError, ValueError, KeyError, struct.error):
        return None
    # NetherNet Bedrock answers no ping: who joined and hasn't left, from the player log
    started = moderation._started_at(server)
    if not started:
        return None
    return len(moderation.online_from_log(server, started))


# ---------------------------------------------------------------------------
# The listener
# ---------------------------------------------------------------------------
class _Listener:
    def __init__(self, server, kind, info):
        self.server, self.kind, self.info = server, kind, info
        self.socks = []             # [(socket, "tcp" | "udp")]
        self.error = None


def _props_info(server):
    from core import joinpage, manager
    d = manager.get_server_dir(server)
    props = manager.read_properties(d)
    java = manager._is_java(server)
    motd = str(props.get("motd" if java else "server-name", "") or "").strip() or "A Minecraft Server"
    try:
        mx = int(props.get("max-players", 20))
    except (TypeError, ValueError):
        mx = 20
    out = {"java": java, "props": props, "motd": motd[:200], "max": mx, "host": str(props.get("server-ip") or "").strip(),
           "port": int(str(props.get("server-port", "25565" if java else "19132")).strip() or 0),
           "portv6": int(str(props.get("server-portv6", "19133")).strip() or 0) if not java else 0,
           "transport": None if java else manager.bedrock_transport(props), "icon": "", "version": "",
           "loader": ""}
    for s in manager.list_servers():
        if s["name"] == server:
            out["version"] = str(s.get("minecraft") or s.get("version") or "")[:40]
            out["loader"] = str(s.get("loader") or "").lower()
            break
    if java:
        try:
            out["icon"] = joinpage._icon_file(server)
        except Exception:
            out["icon"] = ""
        try:
            from core import whitelist, moderation
            wl = whitelist.info(server)
            out["whitelist"] = {p["name"].lower() for p in wl["players"]} if wl["enabled"] else None
            out["banned"] = {b["name"].lower() for b in moderation.banned(server)}
        except Exception:
            out["whitelist"], out["banned"] = None, set()
    return out


def _tcp_socket(host, port):
    if host and host not in ("0.0.0.0", "::"):
        fam = socket.AF_INET6 if ":" in host else socket.AF_INET
        return socket.create_server((host, port), family=fam, backlog=16)
    if socket.has_dualstack_ipv6():
        try:
            return socket.create_server(("", port), family=socket.AF_INET6, dualstack_ipv6=True, backlog=16)
        except OSError:
            pass
    return socket.create_server(("", port), family=socket.AF_INET, backlog=16)


def _udp_socket(host, port, v6=False):
    s = socket.socket(socket.AF_INET6 if v6 else socket.AF_INET, socket.SOCK_DGRAM)
    try:
        if v6:
            s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        s.bind((host or ("::" if v6 else "0.0.0.0"), port))
    except OSError:
        s.close()
        raise
    return s


def _open(server):
    """Listens on the server's game port(s) for it. Errors are kept and retried by the loop."""
    with _lock:
        if server in _listeners and _listeners[server].socks:
            return _listeners[server]
        try:
            info = _props_info(server)
        except Exception as e:
            lst = _listeners[server] = _Listener(server, "?", {})
            lst.error = f"Couldn't read its settings: {e}"
            return lst
        kind = "java" if info["java"] else f"bedrock-{info['transport']}"
        lst = _listeners.get(server) or _Listener(server, kind, info)
        lst.kind, lst.info, lst.error = kind, info, None
        _listeners[server] = lst
        try:
            if kind == "bedrock-raknet":
                lst.socks.append((_udp_socket(info["host"], info["port"]), "udp"))
                if info["portv6"] and socket.has_ipv6:
                    try:
                        lst.socks.append((_udp_socket("", info["portv6"], v6=True), "udp"))
                    except OSError:
                        pass            # no IPv6 here: IPv4 is enough
            else:
                lst.socks.append((_tcp_socket(info["host"], info["port"]), "tcp"))
        except OSError as e:
            for sock, _ in lst.socks:
                sock.close()
            lst.socks = []
            if lst.error is None:
                lst.error = f"Port {info['port']} is in use ({e.strerror or e})"
        return lst


def _close(server):
    with _lock:
        lst = _listeners.pop(server, None)
    if lst:
        for sock, _ in lst.socks:
            try:
                sock.close()
            except OSError:
                pass
        lst.socks = []
        _let_go()


# A socket closed while the loop's select() waits on it stays open in the kernel until select
# returns (it holds a reference): the server's start would find its port still taken. So the
# loop also waits on a wake-up socket, and _let_go() pokes it and waits for the round to end.
_round = {"n": 0}
_round_cv = threading.Condition()
_waker = {"r": None, "w": None}


def _let_go(timeout=3.0):
    t = _thread
    if t is None or not t.is_alive() or threading.current_thread() is t or _waker["w"] is None:
        return
    with _round_cv:
        start = _round["n"]
        try:
            _waker["w"].send(b"x")
        except OSError:
            pass
        _round_cv.wait_for(lambda: _round["n"] > start, timeout)


def holds(port, proto):
    """The server whose sleep listener holds this port in this process, or None."""
    with _lock:
        for server, lst in _listeners.items():
            for sock, p in lst.socks:
                try:
                    if p == proto and sock.getsockname()[1] == port:
                        return server
                except OSError:
                    continue
    return None


def asleep_ports():
    """{(port, proto): server} for the servers asleep, from their settings (any process)."""
    from core import manager
    out = {}
    for server in asleep_servers():
        try:
            props = manager.read_properties(manager.get_server_dir(server))
            java = manager._is_java(server)
            port = int(str(props.get("server-port", "25565" if java else "19132")).strip())
            if java or manager.bedrock_transport(props) != "raknet":
                out[(port, "tcp")] = server
            else:
                out[(port, "udp")] = server
                v6 = str(props.get("server-portv6", "19133")).strip()
                if v6.isdigit():
                    out[(int(v6), "udp")] = server
        except Exception:
            continue
    return out


# --- Java: the Server List Ping and the login, enough of both to answer and wake --------------
def _read_varint(sock):
    n = shift = 0
    for _ in range(5):
        b = sock.recv(1)
        if not b:
            raise OSError("closed")
        n |= (b[0] & 0x7F) << shift
        shift += 7
        if not b[0] & 0x80:
            return n
    raise OSError("bad varint")


def _varint(n):
    n &= 0xFFFFFFFF
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def _parse_varint(data, i):
    n = shift = 0
    for _ in range(5):
        if i >= len(data):
            raise ValueError("short")
        b = data[i]
        i += 1
        n |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            return n, i
    raise ValueError("bad varint")


def _string(data, i):
    n, i = _parse_varint(data, i)
    if n > 32767 * 4 or i + n > len(data):
        raise ValueError("bad string")
    return data[i:i + n].decode("utf-8", "replace"), i + n


def _packet(sock):
    n = _read_varint(sock)
    if not 0 < n < 1 << 16:
        raise OSError("bad length")
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise OSError("closed")
        data += chunk
    pid, i = _parse_varint(data, 0)
    return pid, data, i


def _send(sock, pid, payload):
    body = _varint(pid) + payload
    sock.sendall(_varint(len(body)) + body)


def _mc_string(text):
    raw = text.encode("utf-8")
    return _varint(len(raw)) + raw


def status_json(info, protocol):
    out = {"version": {"name": f"Sleeping ({info.get('version') or 'Minecraft'})"[:60], "protocol": protocol},
           "players": {"max": info.get("max", 20), "online": 0},
           "description": {"text": f"{info.get('motd', '')}\n§6Sleeping: join to start it"}}
    if info.get("icon"):
        out["favicon"] = "data:image/png;base64," + info["icon"]
    return json.dumps(out)


def starting_message(info):
    slow = info.get("loader") in ("forge", "neoforge", "fabric", "quilt")
    return json.dumps({"text": "", "extra": [
        {"text": "The server was asleep and is starting up for you.\n\n", "color": "gold"},
        {"text": f"Join again in about {'a minute' if slow else '30 seconds'}."}]})


def refused_message(text):
    return json.dumps({"text": text})


def handle_java(conn, server, info, wake_fn=None, peer="?"):
    """One Java connection to a sleeping server. Returns "status", "woke", "refused" or None."""
    wake_fn = wake_fn or wake
    conn.settimeout(CONNECT_TIMEOUT)
    first = conn.recv(1, socket.MSG_PEEK)
    if not first or first[0] == 0xFE:            # legacy (1.6 and older) ping: nothing to say
        return None
    pid, data, i = _packet(conn)
    if pid != 0:
        return None
    protocol, i = _parse_varint(data, i)
    _addr, i = _string(data, i)
    i += 2
    nxt, i = _parse_varint(data, i)
    if nxt == 1:
        pid, data, i = _packet(conn)
        if pid != 0:
            return None
        _send(conn, 0, _mc_string(status_json(info, protocol)))
        try:
            pid, data, i = _packet(conn)
            if pid == 1:
                _send(conn, 1, data[i:i + 8])
        except OSError:
            pass
        return "status"
    if nxt not in (2, 3):
        return None
    pid, data, i = _packet(conn)
    if pid != 0:
        return None
    name, i = _string(data, i)
    name = name[:16]
    low = name.lower()
    if low in (info.get("banned") or set()):
        _send(conn, 0, _mc_string(refused_message("You are banned from this server.")))
        return "refused"
    wl = info.get("whitelist")
    if wl is not None and low not in wl:
        _send(conn, 0, _mc_string(refused_message("You are not white-listed on this server.")))
        return "refused"
    _send(conn, 0, _mc_string(starting_message(info)))
    wake_fn(server, f"{name} ({peer})" if re.fullmatch(r"[A-Za-z0-9_.*]{1,16}", name) else f"a player ({peer})")
    return "woke"


def _java_client(conn, addr, server, info):
    try:
        handle_java(conn, server, info, peer=addr[0] if addr else "?")
    except (OSError, ValueError):
        pass
    finally:
        try:
            conn.close()
        finally:
            _clients.release()


# --- Bedrock -------------------------------------------------------------------------------
def bedrock_pong(info, pong, port=None):
    """The answer to a RakNet unconnected ping for a sleeping server."""
    p = list(pong or [])
    protocol = p[2] if len(p) > 2 and p[2].isdigit() else "0"
    version = p[3] if len(p) > 3 and p[3] else (info.get("version") or "1.0")
    mode = p[8] if len(p) > 8 and p[8] else "Survival"
    name = re.sub(r"[;\x00-\x1f]", " ", info.get("motd") or "Bedrock server")[:40]
    port = port or info.get("port") or 19132
    fields = ["MCPE", f"{name} (sleeping, join to start)", protocol, version, "0", str(info.get("max", 10)),
              str(_guid & 0x7FFFFFFFFFFFFFFF), "Sleeping: join to start", mode, "1", str(port),
              str(info.get("portv6") or 19133), ""]
    return ";".join(fields).encode("utf-8")


def handle_raknet(sock, data, addr, server, info, pong=None, wake_fn=None):
    """One RakNet datagram to a sleeping Bedrock server. Returns "pong", "woke" or None."""
    wake_fn = wake_fn or wake
    if not data or RAKNET_MAGIC not in data:
        return None
    if data[0] in (0x01, 0x02) and len(data) >= 25:
        text = bedrock_pong(info, pong, sock.getsockname()[1])
        reply = b"\x1c" + data[1:9] + struct.pack(">q", _guid) + RAKNET_MAGIC + struct.pack(">H", len(text)) + text
        sock.sendto(reply, addr)
        return "pong"
    if data[0] == 0x05:                       # Open Connection Request 1: someone is joining
        wake_fn(server, f"a player ({addr[0]})")
        return "woke"
    return None


def _nethernet_client(conn, addr, server):
    """NetherNet's signaling is TCP: a connection that sends something is someone joining."""
    try:
        conn.settimeout(CONNECT_TIMEOUT)
        if conn.recv(1):
            wake(server, f"a player ({addr[0]})")
    except OSError:
        pass
    finally:
        try:
            conn.close()
        finally:
            _clients.release()


def _serve_ready(ready):
    with _lock:
        by_sock = {sock: (server, lst) for server, lst in _listeners.items() for sock, _ in lst.socks}
    for sock in ready:
        hit = by_sock.get(sock)
        if not hit:
            continue
        server, lst = hit
        try:
            if lst.kind == "bedrock-raknet":
                data, addr = sock.recvfrom(2048)
                pong = (_load()["state"].get(server) or {}).get("pong")
                handle_raknet(sock, data, addr, server, lst.info, pong)
                continue
            conn, addr = sock.accept()
        except OSError:
            continue
        if not _clients.acquire(blocking=False):
            conn.close()
            continue
        target = _java_client if lst.kind == "java" else _nethernet_client
        args = (conn, addr, server, lst.info) if lst.kind == "java" else (conn, addr, server)
        threading.Thread(target=target, args=args, name=f"sleep-{server}", daemon=True).start()


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------
def _same_boot(a, b):
    """Windows works its boot time out from the clock and the uptime (whole seconds, and the clock
    moves); Linux reads it exactly, and a container restarts in a couple of seconds."""
    slack = BOOT_SLACK if platform.name() == "windows" else BOOT_SLACK_EXACT
    return a is not None and b is not None and abs(float(a) - float(b)) <= slack


def resume(now_boot=None):
    """When the panel starts: put back to sleep what was asleep. After the machine restarted, a
    server with sleep mode on and Start at boot on goes to sleep instead of starting (the system
    didn't start it); one with Start at boot off stays stopped."""
    from core import bootstart
    now_boot = _boot_time() if now_boot is None else now_boot
    data = _load()
    for server in list(data["servers"]):
        try:
            if _active(server):
                _set_state(server, "awake")
                continue
            st = data["state"].get(server, {})
            rebooted = not _same_boot(st.get("boot"), now_boot)
            if st.get("state") == "asleep":
                if rebooted and not bootstart.wanted(server):
                    _set_state(server, "off")
                    continue
                _open(server)
                _set_state(server, "asleep")
            elif rebooted and bootstart.wanted(server):
                _set_state(server, "asleep", why="the machine started")
                _log(f"[SLEEP] The machine started: {server} is asleep and starts when a player joins.")
                _open(server)
            elif st.get("state") != "off":
                _set_state(server, "off")
        except Exception as e:
            _log(f"[SLEEP] Couldn't put {server} back to sleep: {e}")


_going = set()                  # servers being stopped for sleeping right now


def _sleep_thread(server, why):
    try:
        _put_to_sleep(server, why)
    except SleepError as e:
        _log(f"[SLEEP] {e}")
    except Exception as e:
        _log(f"[SLEEP] Putting {server} to sleep failed: {e}")
    finally:
        _going.discard(server)


def check(now=None):
    """One look at every server with sleep mode on (the loop runs it every CHECK_EVERY s)."""
    now = time.time() if now is None else now
    data = _load()
    with _lock:
        gone = [s for s in _listeners if s not in data["servers"]]
    for server in gone:                     # turned off elsewhere (the command line): let go
        _close(server)
    for server, cfg in data["servers"].items():
        if server in _going:
            continue
        try:
            st = data["state"].get(server, {}).get("state")
            active = _active(server)
            if st == "asleep":
                if active:              # started from outside the panel
                    _close(server)
                    _set_state(server, "awake")
                else:
                    lst = _listeners.get(server)
                    if not lst or not lst.socks:
                        _close(server)
                        _open(server)
                continue
            if not active:
                if st == "awake":
                    _set_state(server, "off")       # stopped from outside, or crashed: stays stopped
                _empty_since.pop(server, None)
                continue
            if st != "awake":
                _set_state(server, "awake")
            if why_not(server):
                continue
            count = players_online(server)
            if count is None or count > 0:
                _empty_since.pop(server, None)
                continue
            since = _empty_since.setdefault(server, now)
            mins = _minutes(cfg.get("minutes"))
            if now - since >= mins * 60 and not _busy(server) and server not in _going:
                _going.add(server)
                why = f"Nobody has played on {server} for {mins} minute{'s' if mins != 1 else ''}"
                threading.Thread(target=_sleep_thread, args=(server, why), name=f"sleep-{server}",
                                 daemon=True).start()
        except SleepError as e:
            _log(f"[SLEEP] {e}")
        except Exception as e:
            _log(f"[SLEEP] Checking {server} failed: {e}")


def watch_asleep():
    """Every WATCH_EVERY s: an asleep server started from outside the panel gets its port back."""
    with _lock:
        names = [s for s, l in _listeners.items() if l.socks]
    for server in names:
        if _active(server):
            _close(server)
            _set_state(server, "awake")
            _log(f"[SLEEP] {server} was started from outside the panel while asleep; the panel let go of its port.")


def _loop():
    try:
        resume()
    except Exception as e:
        _log(f"[SLEEP] Resuming sleep mode failed: {e}")
    last_check = last_watch = 0.0
    r, w = socket.socketpair()
    r.setblocking(False)
    _waker.update(r=r, w=w)
    while True:
        with _lock:
            socks = [sock for lst in _listeners.values() for sock, _ in lst.socks]
        try:
            ready = select.select([r] + socks, [], [], 1.0)[0]
        except (OSError, ValueError):
            ready = []
            time.sleep(0.1)
        with _round_cv:
            _round["n"] += 1
            _round_cv.notify_all()
        if r in ready:
            ready.remove(r)
            try:
                r.recv(64)
            except OSError:
                pass
        if ready:
            _serve_ready(ready)
        now = time.monotonic()
        if now - last_watch >= WATCH_EVERY:
            last_watch = now
            try:
                watch_asleep()
            except Exception:
                pass
        if now - last_check >= CHECK_EVERY:
            last_check = now
            check()


def start():
    """Starts sleep mode's loop in the panel (run.py). Safe to call twice."""
    global _thread
    platform.on_before_start(before_start)
    if _thread is None:
        _thread = threading.Thread(target=_loop, name="sleep-mode", daemon=True)
        _thread.start()
