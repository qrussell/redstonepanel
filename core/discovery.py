"""Finding other RedstonePanels on the local network (1.16.13), so a master can add them.

An admin on the master presses Find panels: every address of this machine's private IPv4
networks (each capped to the /24 around this machine, at most MAX_NETWORKS of them) is asked
for GET /api/panel-info on the panel's usual ports, HTTPS 8443 then HTTP 8080. That route is
public and only says the panel's name, that it is a RedstonePanel, and whether it is standalone,
a master or a node. Nothing else is sent, and nothing found is trusted: certificates aren't
checked (a standalone panel's is self-signed), answers are capped at 4 KB, and a panel is only
ever added by an admin pasting a join code into it, as before.
"""
import http.client
import ipaddress
import json
import socket
import ssl
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

PORTS = (("https", 8443), ("http", 8080))
MAX_NETWORKS = 4
TIMEOUT = 1.5
WORKERS = 128
MAX_ANSWER = 4096

_running = threading.Lock()


class DiscoveryError(ValueError):
    """The search can't be done (message is safe to show)."""


def _ip_json():
    try:
        out = subprocess.run(["ip", "-j", "-4", "addr", "show"], capture_output=True, text=True, timeout=10)
        data = json.loads(out.stdout or "[]")
        if data:
            return data
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    # No iproute2 (a minimal system): the address used for the default route, as a /24
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))         # TEST-NET-1: nothing is sent, it only picks a route
        return [{"addr_info": [{"local": s.getsockname()[0], "prefixlen": 24}]}]
    except OSError:
        return []
    finally:
        s.close()


def networks(ip_json=None):
    """([networks to search], {this machine's addresses}) from `ip -j -4 addr`."""
    try:
        data = _ip_json() if ip_json is None else ip_json
    except (OSError, ValueError, subprocess.SubprocessError):
        data = []
    nets, own = [], set()
    for iface in data if isinstance(data, list) else []:
        for a in iface.get("addr_info") or []:
            try:
                ip = ipaddress.ip_address(a.get("local", ""))
                prefix = int(a.get("prefixlen", 24))
            except (ValueError, TypeError):
                continue
            own.add(str(ip))
            if ip.version != 4 or not ip.is_private or ip.is_loopback or ip.is_link_local:
                continue
            net = ipaddress.ip_network(f"{ip}/{max(prefix, 24)}", strict=False)
            if net not in nets:
                nets.append(net)
    return nets[:MAX_NETWORKS], own


def _context():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE          # only the public name is read; nothing is sent
    return ctx


def read_info(body):
    """The panel-info answer, if it is a RedstonePanel's: {name, cluster}, else None. Panels
    before 1.16.13 answer only {"name": ...}; they can still join, so they count."""
    try:
        d = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(d, dict) or not isinstance(d.get("name"), str):
        return None
    if d.get("app") != "RedstonePanel" and set(d) != {"name"}:
        return None
    cluster = d.get("cluster") if d.get("cluster") in ("standalone", "master", "node") else None
    name = "".join(c for c in d["name"] if c.isprintable())[:60].strip() or "RedstonePanel"
    return {"name": name, "cluster": cluster}


def probe(host, scheme, port, timeout=TIMEOUT):
    """{url, name, cluster} if a RedstonePanel answers at host:port, else None."""
    conn = (http.client.HTTPSConnection(host, port, timeout=timeout, context=_context()) if scheme == "https"
            else http.client.HTTPConnection(host, port, timeout=timeout))
    try:
        conn.request("GET", "/api/panel-info", headers={"Accept": "application/json",
                                                        "User-Agent": "RedstonePanel discovery"})
        res = conn.getresponse()
        if res.status != 200:
            return None
        info = read_info(res.read(MAX_ANSWER + 1)[:MAX_ANSWER])
    except (OSError, http.client.HTTPException, ssl.SSLError):
        return None
    finally:
        conn.close()
    if info:
        info["url"] = f"{scheme}://{host}:{port}"
    return info


def _host_of(url):
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return set()
    out = {host}
    try:
        out.add(socket.gethostbyname(host))
    except OSError:
        pass
    return out


def find(nodes=(), ip_json=None, probe_fn=None):
    """Searches this machine's networks. nodes: this cluster's nodes, to say which are already
    in it. Returns {networks, panels, seconds}."""
    if not _running.acquire(blocking=False):
        raise DiscoveryError("A search is already running. Wait for it to finish.")
    try:
        started = time.time()
        nets, own = networks(ip_json)
        if not nets:
            raise DiscoveryError("This machine has no private network (like 192.168.x.x) to search.")
        hosts = [str(h) for n in nets for h in n.hosts() if str(h) not in own]
        probe_fn = probe_fn or probe
        found = {}
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            jobs = [(h, pool.submit(probe_fn, h, scheme, port)) for scheme, port in PORTS for h in hosts]
            for host, job in jobs:
                info = job.result()
                if info and host not in found:      # HTTPS first, so it wins
                    found[host] = dict(info, host=host)
        ours = {}
        for n in nodes:
            for h in _host_of(n.get("url", "")):
                ours[h] = n.get("name")
        panels = []
        for host in sorted(found, key=lambda h: ipaddress.ip_address(h)):
            p = found[host]
            p["node"] = ours.get(host)
            panels.append(p)
        return {"networks": [str(n) for n in nets], "panels": panels, "seconds": round(time.time() - started, 1)}
    finally:
        _running.release()
