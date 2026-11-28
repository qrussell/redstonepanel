"""Port forwarding (1.16.14): what to forward on the internet router (pfSense or a home router)
so players outside the house can reach each game server, per server and per cluster member.

This is about the router in front of the machines, not ufw on them (core.manager keeps ufw in
step). The game-port rules come from manager.firewall_rules(), the same logic ufw uses, so the
two never disagree. On top of those, Java servers can have extra listeners the panel doesn't
open in ufw: Query, Geyser (Bedrock players on a Java server) and Simple Voice Chat.

Each row says what to do with a port:
  forward   players outside need it
  optional  only for a reason the row names (Query for server-list sites)
  lan       only used on the home network (NetherNet LAN discovery, Phantom): don't forward
  ipv6      IPv6 isn't NAT-ed: allow it in the router's firewall instead of forwarding
  never     keep closed (RCON, the panel itself unless you mean to)

The public address is only learnt from the router itself (NAT-PMP or UPnP on the default
gateway) or typed in, never from an outside "what's my IP" service. The reachability check
connects to the public address from this machine, so it relies on the router looping the
connection back (pfSense: NAT reflection), and says so.
"""
import ipaddress
import json
import os
import re
import socket
import struct
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit, urljoin

from core import bedrock_net
from core import manager
from core import settings as panel_settings
from core.platform import paths, system

DATA_FILE = "port-forwarding.json"
NETHERNET_LAN_PORT = manager.NETHERNET_LAN_PORT
CHECK_TIMEOUT = 3

_lock = threading.Lock()


class ForwardError(ValueError):
    """A request that can't be done (message is safe to show)."""


# ---------------------------------------------------------------------------
# Saved settings: the public address typed in or found on the router
# ---------------------------------------------------------------------------
def _path():
    return os.path.join(manager.DATA_DIR, DATA_FILE)


def load():
    try:
        with open(_path()) as f:
            data = json.load(f)
        addr = data.get("public_address") if isinstance(data, dict) else ""
        return {"public_address": addr if isinstance(addr, str) else ""}
    except (OSError, ValueError):
        return {"public_address": ""}


def clean_address(text):
    """An IPv4/IPv6 address or a host name (a dynamic DNS name), or '' to forget it."""
    t = (text or "").strip().strip("[]")
    t = t[:-1] if t.endswith(".") and not t.replace(".", "").isdigit() else t
    if not t:
        return ""
    try:
        return str(ipaddress.ip_address(t))
    except ValueError:
        pass
    if len(t) > 253 or not panel_settings.HOSTNAME_RE.match(t) or t.replace(".", "").isdigit():
        raise ForwardError("Enter an IP address or a host name like play.example.com.")
    return t.lower()


def save(public_address, actor="?"):
    addr = clean_address(public_address)
    with _lock:
        os.makedirs(manager.DATA_DIR, exist_ok=True)
        tmp = _path() + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"public_address": addr}, f, indent=1)
        os.replace(tmp, _path())
    manager.write_log(f"[PORTS] {actor} set the public address to {addr or '(none)'}.")
    return load()


# ---------------------------------------------------------------------------
# Port specs
# ---------------------------------------------------------------------------
def _spec(text):
    """'19141' or '21200-21300' (or '21200:21300') -> '19141' / '21200:21300', else None."""
    lo, sep, hi = str(text).strip().replace(":", "-").partition("-")
    if not lo.isdigit() or not 0 < int(lo) < 65536:
        return None
    if not sep:
        return lo
    if not hi.isdigit() or not int(lo) <= int(hi) < 65536:
        return None
    return lo if lo == hi else f"{lo}:{hi}"


def udp_port_pairs(value):
    """server-udp-ports -> [(outside spec, inside spec)]. The plain form '19141' is the same port
    on both sides; 'public:local' (also with ranges, '21200-21300:19400-19500') names the port the
    router shows the internet and the one BDS listens on. An address prefix is ignored here."""
    out = []
    for part in bedrock_net.plain_udp_ports(value or "").split(","):
        bits = part.strip().split(":")
        if len(bits) == 1:
            s = _spec(bits[0])
            if s:
                out.append((s, s))
        elif len(bits) == 2:
            pub, loc = _spec(bits[0]), _spec(bits[1])
            if pub and loc:
                out.append((pub, loc))
    return out


def spec_range(spec):
    lo, _, hi = str(spec).partition(":")
    return int(lo), int(hi or lo)


def overlaps(a, b):
    (a1, a2), (b1, b2) = spec_range(a), spec_range(b)
    return a1 <= b2 and b1 <= a2


def reserved_clash(spec, proto, reserved):
    """The (start, end) of a range the system keeps for itself that spec/proto falls in, or None.
    reserved: system.reserved_ports() ([(proto, start, end)]; Windows: Hyper-V, WSL, Docker)."""
    for rproto, a, b in reserved or []:
        if rproto == proto and overlaps(spec, f"{a}:{b}"):
            return a, b
    return None


def reserved_text(spec, proto, rng):
    a, b = rng
    span = f"{a}" if a == b else f"{a}–{b}"
    return (f"Windows keeps port{'s' if a != b else ''} {span} for itself (Hyper-V, WSL or Docker Desktop reserve "
            f"them at start-up), and that includes {spec.replace(':', '-')}/{proto}: nothing can listen there, even "
            f"with the port free. Give the server a port outside that range, or restart Windows and start the "
            f"server before WSL or Docker.")


def _row(port, proto, kind, what, note="", inside=None, check=None):
    return {"port": str(port), "inside": str(inside or port), "proto": proto, "kind": kind,
            "what": what, "note": note, "check": check}


# ---------------------------------------------------------------------------
# Java extras: Query, RCON, Geyser, Simple Voice Chat
# ---------------------------------------------------------------------------
GEYSER_CONFIGS = ("plugins/Geyser-Spigot/config.yml", "plugins/Geyser-Velocity/config.yml",
                  "config/Geyser-Fabric/config.yml", "config/Geyser-NeoForge/config.yml",
                  "plugins/Geyser-BungeeCord/config.yml", "config/geyser/config.yml", "plugins/geyser/config.yml")
VOICE_CONFIGS = ("plugins/voicechat/voicechat-server.properties", "config/voicechat/voicechat-server.properties")


def _read(server, path):
    """A small text file inside the server's folder, never through a symlink, or None."""
    from core import files
    try:
        return files.read_text(server, path)["content"]
    except Exception:
        return None


def _list(server, path):
    from core import files
    try:
        return [e["name"] for e in files.list_dir(server, path)["entries"] if e.get("type") == "file"]
    except Exception:
        return []


def geyser_port(config_text, java_port):
    """Geyser's Bedrock listener from its config.yml: bedrock: port: (19132 by default), or the Java
    port when clone-remote-port is true. Only the top-level bedrock: block is read."""
    port, clone, inside = "19132", False, False
    for line in (config_text or "").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0:
            inside = line.strip().startswith("bedrock:")
            continue
        if not inside:
            continue
        key, _, val = line.strip().partition(":")
        val = val.split("#")[0].strip().strip("'\"")
        if key == "port" and val.isdigit():
            port = val
        elif key == "clone-remote-port":
            clone = val.lower() == "true"
    return str(java_port) if clone and str(java_port).isdigit() else port


def voice_port(props_text, java_port):
    """Simple Voice Chat's UDP port (24454 by default; -1 means the game port)."""
    port = "24454"
    for line in (props_text or "").splitlines():
        k, sep, v = line.partition("=")
        if sep and k.strip() == "port":
            port = v.strip()
    if port == "-1":
        return str(java_port)
    return port if port.isdigit() else "24454"


def java_extras(server, props):
    rows = []
    port = str(props.get("server-port", "25565")).strip()
    if str(props.get("enable-query", "")).strip().lower() == "true":
        q = str(props.get("query.port", port)).strip()
        if q.isdigit():
            rows.append(_row(q, "udp", "optional", "Query",
                             "Only if a server-list website or status tool should read player counts.",
                             check=None))
    if str(props.get("enable-rcon", "")).strip().lower() == "true":
        r = str(props.get("rcon.port", "25575")).strip()
        if r.isdigit():
            rows.append(_row(r, "tcp", "never", "RCON",
                             "Remote console with full control of the server. The panel uses it on this machine only.",
                             check=None))
    try:
        from core import modports
        claimed = modports.server_ports(server)
    except Exception:
        claimed = []
    for c in claimed:
        if c.get("local"):
            continue                    # 1.30: a web map shown through the join site, never forwarded
        if c["mod"] not in ("geyser", "voicechat"):           # those two are read below, as before
            rows.append(_row(str(c["port"]), c["proto"], c["kind"], c["name"], c["note"], check=None))
    geyser_jar = any(n.lower().startswith("geyser") and n.endswith(".jar")
                     for n in _list(server, "plugins") + _list(server, "mods"))
    text = next((t for t in (_read(server, p) for p in GEYSER_CONFIGS) if t is not None), None)
    if text is not None or geyser_jar:
        g = geyser_port(text, port)
        rows.append(_row(g, "udp", "forward", "Geyser (Bedrock players)",
                         "Bedrock players join this Java server through Geyser.", check="raknet"))
    vtext = next((t for t in (_read(server, p) for p in VOICE_CONFIGS) if t is not None), None)
    if vtext is not None:
        rows.append(_row(voice_port(vtext, port), "udp", "forward", "Simple Voice Chat",
                         "Voice chat for players outside."))
    return rows


# ---------------------------------------------------------------------------
# One server's rows, from what manager.list_servers() returns
# ---------------------------------------------------------------------------
def server_rows(s, props=None, extras=True):
    """The rows for one server. props: its server.properties (read here when None); extras:
    read the Java extras (Query, RCON, Geyser, voice chat) from its folder."""
    props = props if props is not None else manager.read_properties(s.get("directory") or manager.get_server_dir(s["name"]))
    port = str(props.get("server-port", s.get("port", ""))).strip()
    if not port.isdigit():
        return [], ["Its server.properties has no server-port, so there's nothing to forward yet."]
    rows, issues = [], []
    if s.get("edition") == "Java":
        rows.append(_row(port, "tcp", "forward", "Game (Java)", check="java"))
        if extras:
            rows += java_extras(s["name"], props)
    elif s.get("edition") == "Bedrock":
        if manager.is_nethernet(props):
            rows.append(_row(port, "tcp", "forward", "Game: connection setup (NetherNet)",
                             "Bedrock 1.26.50 and later set up each connection over TCP.", check="tcp"))
            pairs = udp_port_pairs(props.get("server-udp-ports"))
            for pub, loc in pairs:
                rows.append(_row(pub, "udp", "forward", "Game: play (NetherNet)",
                                 "Gameplay after the connection is set up. Keep the outside port the same "
                                 "as the inside port." if pub == loc else
                                 "server-udp-ports gives a different outside port; forward it to the inside port.",
                                 inside=loc))
            if not pairs:
                issues.append("server-udp-ports isn't set, so Bedrock plays over random UDP ports that no "
                              "router rule can match. Set it to the server port in Configuration and restart.")
            if bedrock_net.udp_ports_have_address(props.get("server-udp-ports", "")):
                issues.append("server-udp-ports has a public address in front of the ports. Bedrock 1.26.63 "
                              "stops accepting players with that form; use just the port.")
            rows.append(_row(NETHERNET_LAN_PORT, "udp", "lan", "LAN discovery",
                             "Shows the server in Friends > LAN on the home network. Shared by every Bedrock "
                             "server on this machine. Not needed from the internet."))
        else:
            rows.append(_row(port, "udp", "forward", "Game (RakNet)", check="raknet"))
            v6 = str(props.get("server-portv6", "")).strip()
            if v6.isdigit():
                rows.append(_row(v6, "udp", "ipv6", "Game over IPv6 (RakNet)",
                                 "IPv6 isn't forwarded. If your internet has IPv6, allow this port to this "
                                 "machine's IPv6 address in the router's firewall instead."))
    else:
        for spec, proto in s.get("firewall_rules") or []:
            rows.append(_row(spec, proto, "forward", "Game"))
    return rows, issues


def _from_firewall_rules(s):
    """For a member too old to answer: the rows its ufw rules imply (no extras)."""
    rows = []
    for spec, proto in s.get("firewall_rules") or []:
        spec = str(spec)
        if spec == str(NETHERNET_LAN_PORT) and proto == "udp":
            rows.append(_row(spec, proto, "lan", "LAN discovery", "Not needed from the internet."))
        else:
            rows.append(_row(spec, proto, "forward", "Game"))
    return rows


# ---------------------------------------------------------------------------
# This machine
# ---------------------------------------------------------------------------
def default_gateway():
    """The IPv4 default gateway (the router), or None."""
    return system.default_gateway()


def primary_ip(gateway=None):
    """The address this machine uses towards its gateway: what the router forwards to.
    connect() on a UDP socket sends nothing; it only picks the route."""
    target = gateway or default_gateway() or "192.0.2.1"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((target, 9))
            ip = s.getsockname()[0]
        return ip if ip and not ip.startswith("127.") and ip != "0.0.0.0" else None
    except OSError:
        return None


def lan_ips():
    from core import tls
    out = []
    for ip in tls.identity()["ips"]:
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if a.version == 4 and not a.is_loopback:
            out.append(ip)
    return out


def classify(address):
    """'public', 'private' (another router in front: double NAT), 'cgnat' (the provider shares
    one address between customers) or None for a host name."""
    try:
        a = ipaddress.ip_address(address)
    except ValueError:
        return None
    if a.version == 4 and a in ipaddress.ip_network("100.64.0.0/10"):
        return "cgnat"
    return "private" if (a.is_private or a.is_loopback or a.is_link_local) else "public"


def local_report(list_servers=None):
    """This panel's rows: every server, the machine-wide LAN-only ports, and the panel's own port."""
    from core import host
    servers = (list_servers or manager.list_servers)()
    fw = host.firewall()
    active = fw.get("active")
    reserved = system.reserved_ports()
    out = []
    for s in servers:
        try:
            rows, issues = server_rows(s)
        except Exception as e:                     # one odd server mustn't break the page
            rows, issues = _from_firewall_rules(s), [f"Couldn't read its settings: {e}"]
        for r in rows:
            r["ufw"] = host.ufw_allows_spec(fw["rules"], r["inside"], r["proto"]) if active else None
            clash = reserved_clash(r["inside"], r["proto"], reserved) if r["kind"] != "never" else None
            if clash:
                issues = list(issues) + [reserved_text(r["inside"], r["proto"], clash)]
        out.append({"name": s["name"], "edition": s.get("edition"), "status": s.get("status"),
                    "motd": s.get("motd"), "port": s.get("port"), "transport": s.get("transport"),
                    "rows": rows, "issues": issues})
    machine = [dict(r, ufw=None) for r in
               ({"port": str(p), "inside": str(p), "proto": q, "kind": "lan", "what": "Phantom (consoles on the LAN)",
                 "note": "Phantom only helps consoles on the home network. Don't forward it.", "check": None}
                for p, q in sorted(manager.phantom_firewall_rules()))]
    st = panel_settings.load()
    scheme = panel_settings.RUNTIME.get("scheme") or ("https" if st["https_mode"] != "off" else "http")
    web = panel_settings.RUNTIME.get("port") or (st["https_port"] if scheme == "https" else st["http_port"])
    gw = default_gateway()
    return {"name": panel_settings.display_name(), "lan_ip": primary_ip(gw), "lan_ips": lan_ips(), "gateway": gw,
            "ufw": {"installed": fw.get("installed"), "active": active},
            "web": {"port": web, "scheme": scheme},
            "servers": out, "machine": machine,
            "reserved": [{"proto": q, "start": a, "end": b} for q, a, b in reserved],
            "port_check": _port_check(list_servers)}


def _port_check(list_servers=None):
    """The Port check's report for this machine (1.23.1), or {"error"} when it couldn't run."""
    from core import portaudit
    try:
        return portaudit.report(servers=list_servers() if list_servers else None)
    except Exception as e:
        return {"error": str(e), "problems": [], "unknown": [], "moved": {}}


# ---------------------------------------------------------------------------
# Every member, and ports two members would both need on the one public address
# ---------------------------------------------------------------------------
def _node_report(node, actor=None):
    from core import cluster_client
    host_ip = urlsplit(node.get("url") or "").hostname
    base = {"id": node["id"], "name": node["name"], "url_host": host_ip}
    try:
        rep = cluster_client.call_node(node, "GET", "/api/port-forwarding/local", timeout=20,
                                       actor=actor, role="admin")
        if not isinstance(rep, dict) or not isinstance(rep.get("servers"), list):
            raise cluster_client.NodeError("error", "The answer was incomplete.")
        return dict(base, state="ok", **{k: rep.get(k) for k in
                                         ("lan_ip", "lan_ips", "gateway", "ufw", "web", "servers", "machine",
                                          "reserved", "port_check")})
    except cluster_client.NodeError as e:
        if e.state not in ("too_old",) and e.status != 404:
            return dict(base, state=e.state, error=str(e), servers=[], machine=[])
    # A node from before 1.16.14: its server list and ufw rules still say what it listens on.
    try:
        servers = cluster_client.call_node(node, "GET", "/api/servers", timeout=20, actor=actor)
    except cluster_client.NodeError as e:
        return dict(base, state=e.state, error=str(e), servers=[], machine=[])
    out = [{"name": s.get("name"), "edition": s.get("edition"), "status": s.get("status"), "motd": s.get("motd"),
            "port": s.get("port"), "transport": s.get("transport"), "rows": [dict(r, ufw=None) for r in _from_firewall_rules(s)],
            "issues": []} for s in servers if isinstance(s, dict)]
    return dict(base, state="ok", old=True, lan_ip=None, lan_ips=[], servers=out, machine=[])


def conflicts(members):
    """Rows to forward that two servers both want on the one public address:
    [{"proto", "port", "users": [{"member", "server", "port"}]}]."""
    wanted = []
    for m in members:
        for s in m.get("servers") or []:
            for r in s.get("rows") or []:
                if r["kind"] == "forward":
                    wanted.append((m.get("name"), s["name"], r["port"], r["proto"]))
    found = []
    for i, (m1, s1, p1, q1) in enumerate(wanted):
        for m2, s2, p2, q2 in wanted[i + 1:]:
            if q1 == q2 and (m1, s1) != (m2, s2) and overlaps(p1, p2):
                key = (q1, min(spec_range(p1)[0], spec_range(p2)[0]))
                hit = next((c for c in found if (c["proto"], c["key"]) == key), None)
                if not hit:
                    hit = {"proto": q1, "key": key, "port": p1, "users": []}
                    found.append(hit)
                for u in ({"member": m1, "server": s1, "port": p1}, {"member": m2, "server": s2, "port": p2}):
                    if u not in hit["users"]:
                        hit["users"].append(u)
    for c in found:
        c.pop("key")
    return found


def overview(list_servers=None, actor=None):
    from core import cluster
    local = dict(local_report(list_servers), id="local", local=True, state="ok")
    members = [local]
    role = cluster.role()
    if role == "master":
        nodes = cluster.load_nodes()
        if nodes:
            with ThreadPoolExecutor(max_workers=8) as pool:
                members += list(pool.map(lambda n: _node_report(n, actor), nodes))
    for m in members:
        m["forward_to"] = m.get("lan_ip") or m.get("url_host")
    addr = load()["public_address"]
    return {"role": role, "public_address": addr, "public_kind": classify(addr) if addr else None,
            "members": members, "conflicts": conflicts(members)}


# ---------------------------------------------------------------------------
# The public address, asked of the router itself (nothing leaves the home network)
# ---------------------------------------------------------------------------
def natpmp_address(gateway, timeout=1.5, port=5351):
    """NAT-PMP (RFC 6886) external address request to the gateway, or None.
    pfSense answers when UPnP & NAT-PMP is enabled under Services."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            s.sendto(b"\x00\x00", (gateway, port))
            data, src = s.recvfrom(64)
    except OSError:
        return None
    if src[0] != gateway or len(data) < 12 or data[0] != 0 or data[1] != 128:
        return None
    if struct.unpack("!H", data[2:4])[0] != 0:
        return None
    return socket.inet_ntoa(data[8:12])


_no_proxy = urllib.request.build_opener(urllib.request.ProxyHandler({}))
WAN_SERVICE_RE = re.compile(r"<serviceType>\s*(urn:schemas-upnp-org:service:WAN(?:IP|PPP)Connection:\d)\s*</serviceType>"
                            r".*?<controlURL>\s*([^<\s]+)\s*</controlURL>", re.S)


def _upnp_control(gateway, timeout=2.0):
    """(service type, control URL) of the gateway's UPnP WAN connection, or None. Only a
    description on the gateway's own address is fetched."""
    msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\nMX: 1\r\n"
           "ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n\r\n").encode()
    location = None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            s.sendto(msg, ("239.255.255.250", 1900))
            end = time.time() + timeout
            while time.time() < end and not location:
                data, src = s.recvfrom(4096)
                if src[0] != gateway:
                    continue
                m = re.search(rb"(?im)^location:\s*(\S+)", data)
                if m:
                    location = m.group(1).decode("ascii", "replace")
    except OSError:
        pass
    if not location:
        return None
    u = urlsplit(location)
    if u.scheme != "http" or u.hostname != gateway:
        return None
    try:
        desc = _no_proxy.open(location, timeout=timeout).read(200000).decode("utf-8", "replace")
    except Exception:
        return None
    m = WAN_SERVICE_RE.search(desc)
    if not m:
        return None
    service, control = m.group(1), urljoin(location, m.group(2))
    if urlsplit(control).hostname != gateway:
        return None
    return service, control


def _soap(control, service, action, args="", timeout=2.0):
    body = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
            f'<u:{action} xmlns:u="{service}">{args}</u:{action}></s:Body></s:Envelope>').encode()
    req = urllib.request.Request(control, data=body, headers={
        "Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": f'"{service}#{action}"'})
    return _no_proxy.open(req, timeout=timeout).read(20000).decode("utf-8", "replace")


def upnp_address(gateway, timeout=2.0):
    """UPnP IGD GetExternalIPAddress on the gateway, or None."""
    found = _upnp_control(gateway, timeout)
    if not found:
        return None
    try:
        ans = _soap(found[1], found[0], "GetExternalIPAddress", timeout=timeout)
    except Exception:
        return None
    m = re.search(r"<NewExternalIPAddress>\s*([0-9.]+)\s*</NewExternalIPAddress>", ans)
    try:
        return str(ipaddress.ip_address(m.group(1))) if m else None
    except ValueError:
        return None


def _tag(xml, name):
    m = re.search(rf"<{name}>\s*([^<]*?)\s*</{name}>", xml)
    return m.group(1) if m else ""


def upnp_mappings(gateway, timeout=2.0, limit=256):
    """The router's port forwards, read over UPnP (GetGenericPortMappingEntry):
    [{"outside", "proto", "inside_ip", "inside", "enabled", "description"}], or None when the
    router doesn't answer UPnP. Only forwards made over UPnP or listed by it show here; pfSense
    lists only the ones UPnP made, not those typed under Firewall > NAT."""
    found = _upnp_control(gateway, timeout)
    if not found:
        return None
    out = []
    for i in range(limit):
        try:
            ans = _soap(found[1], found[0], "GetGenericPortMappingEntry",
                        f"<NewPortMappingIndex>{i}</NewPortMappingIndex>", timeout=timeout)
        except Exception:
            break                      # a SOAP fault (index past the end) arrives as an HTTP error
        ext, inside = _tag(ans, "NewExternalPort"), _tag(ans, "NewInternalPort")
        if not ext.isdigit() or not inside.isdigit():
            break
        out.append({"outside": int(ext), "proto": _tag(ans, "NewProtocol").lower(),
                    "inside_ip": _tag(ans, "NewInternalClient"), "inside": int(inside),
                    "enabled": _tag(ans, "NewEnabled") not in ("0", "false"),
                    "description": _tag(ans, "NewPortMappingDescription")[:120]})
    return out


def lookup_public_address():
    """Asks the router for its internet address. Nothing is saved."""
    gw = default_gateway()
    if not gw:
        raise ForwardError("This machine has no default gateway, so there's no router to ask.")
    for method, fn in (("NAT-PMP", natpmp_address), ("UPnP", upnp_address)):
        addr = fn(gw)
        if addr:
            return {"address": addr, "method": method, "gateway": gw, "kind": classify(addr)}
    raise ForwardError(f"The router at {gw} didn't answer NAT-PMP or UPnP (both are often off, which is "
                       "safer). Look up the WAN address on the router's status page and type it in. "
                       "On pfSense it's on the Dashboard under Interfaces > WAN.")


# ---------------------------------------------------------------------------
# Reachability: connect to the public address from here (needs NAT reflection on the router)
# ---------------------------------------------------------------------------
def _varint(n):
    n &= 0xFFFFFFFF
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def _read_varint(sock):
    n = shift = 0
    for _ in range(5):
        b = sock.recv(1)
        if not b:
            raise OSError("closed")
        n |= (b[0] & 0x7F) << shift
        if not b[0] & 0x80:
            return n
        shift += 7
    raise OSError("bad varint")


def _text(desc):
    """A Java status description (string or chat component) as plain text."""
    if isinstance(desc, str):
        return re.sub("§.", "", desc)
    if isinstance(desc, dict):
        return _text(desc.get("text", "")) + "".join(_text(x) for x in desc.get("extra") or [])
    if isinstance(desc, list):
        return "".join(_text(x) for x in desc)
    return ""


def java_status(host, port, timeout=CHECK_TIMEOUT):
    """Server List Ping: {"motd", "version"} or raises OSError."""
    with socket.create_connection((host, int(port)), timeout=timeout) as s:
        s.settimeout(timeout)
        h = host.encode()[:255]
        hs = b"\x00" + _varint(767) + _varint(len(h)) + h + struct.pack(">H", int(port)) + b"\x01"
        s.sendall(_varint(len(hs)) + hs + b"\x01\x00")
        length = _read_varint(s)
        if not 0 < length < 1 << 20:
            raise OSError("bad length")
        data = b""
        while len(data) < length:
            chunk = s.recv(length - len(data))
            if not chunk:
                break
            data += chunk
    i = 1                                        # packet id 0, then a varint-prefixed string
    n = shift = 0
    while i < len(data):
        b = data[i]; i += 1
        n |= (b & 0x7F) << shift; shift += 7
        if not b & 0x80:
            break
    try:
        js = json.loads(data[i:i + n].decode("utf-8", "replace"))
    except ValueError:
        raise OSError("not a Minecraft server")
    return {"motd": _text(js.get("description")).strip(), "version": (js.get("version") or {}).get("name")}


RAKNET_MAGIC = bytes.fromhex("00ffff00fefefefefdfdfdfd12345678")


def raknet_status(host, port, timeout=CHECK_TIMEOUT):
    """RakNet unconnected ping (Bedrock over RakNet, Geyser): {"motd", "version"} or raises OSError."""
    fam = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(fam, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        s.sendto(b"\x01" + struct.pack(">Q", int(time.time() * 1000) & (2**63 - 1)) + RAKNET_MAGIC
                 + os.urandom(8), (host, int(port)))
        data, _ = s.recvfrom(2048)
    if not data or data[0] != 0x1c or RAKNET_MAGIC not in data:
        raise OSError("not a Bedrock server")
    i = data.index(RAKNET_MAGIC) + len(RAKNET_MAGIC)
    n = struct.unpack(">H", data[i:i + 2])[0]
    parts = data[i + 2:i + 2 + n].decode("utf-8", "replace").split(";")
    return {"motd": parts[1] if len(parts) > 1 else "", "version": parts[3] if len(parts) > 3 else None}


def tcp_open(host, port, timeout=CHECK_TIMEOUT):
    with socket.create_connection((host, int(port)), timeout=timeout):
        return {}


CHECKS = {"java": java_status, "raknet": raknet_status, "tcp": tcp_open}


def _check_one(host, item):
    fn = CHECKS.get(item.get("check"))
    if not fn:
        return dict(item, result="unchecked")
    try:
        got = fn(host, spec_range(item["port"])[0])
    except (OSError, ValueError) as e:
        return dict(item, result="no_answer", error=str(e) or type(e).__name__)
    motd = (item.get("motd") or "").strip()
    same = None if not motd or "motd" not in got else (motd in got["motd"] or got["motd"] in motd)
    return dict(item, result="reached", answer=got, same_server=same)


def check(report=None):
    """Tries each forward with a check from here, through the public address."""
    rep = report or overview()
    addr = rep.get("public_address")
    if not addr:
        raise ForwardError("Set the public address first.")
    try:
        host = socket.getaddrinfo(addr, None)[0][4][0]
    except OSError:
        raise ForwardError(f"Couldn't look up {addr}.")
    items = []
    for m in rep["members"]:
        for s in m.get("servers") or []:
            for r in s.get("rows") or []:
                if r["kind"] == "forward":
                    items.append({"member": m["id"], "server": s["name"], "port": r["port"], "proto": r["proto"],
                                  "check": r.get("check"), "motd": s.get("motd")})
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda it: _check_one(host, it), items))
    return {"address": addr, "resolved": host, "results": results, "checked": int(time.time())}
