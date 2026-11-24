"""Cloudflare's own addresses (1.21.8), for join pages behind Cloudflare's proxy ("orange cloud").

When an admin says Cloudflare sends players to the join pages, a request that comes from one of
Cloudflare's networks may name the player in its CF-Connecting-IP header; from anywhere else that
header means nothing. Cloudflare publishes the list at https://www.cloudflare.com/ips-v4 and
/ips-v6. The panel ships a copy and refreshes it from there once a day while the setting is on,
keeping the last good list in data/cloudflare-ips.json. A list that looks wrong is never used.

A Cloudflare Tunnel (cloudflared) needs none of this: the tunnel connects from its own machine,
which the admin enters as the proxy's address like any other reverse proxy.
"""
import ipaddress
import json
import os
import threading
import time
import urllib.request

from core import manager

DATA_FILE = "cloudflare-ips.json"
SOURCES = ("https://www.cloudflare.com/ips-v4", "https://www.cloudflare.com/ips-v6")
REFRESH_EVERY = 24 * 3600
RETRY_AFTER = 3600
MAX_BODY = 64 * 1024

# The copy that ships with the panel, used until a refresh succeeds.
BUILT_IN = (
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22", "141.101.64.0/18",
    "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20", "197.234.240.0/22", "198.41.128.0/17",
    "162.158.0.0/15", "104.16.0.0/13", "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
    "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32", "2405:b500::/32", "2405:8100::/32",
    "2a06:98c0::/29", "2c0f:f248::/32",
)

_lock = threading.Lock()
_state = {"nets": None, "checked": 0.0, "refreshing": False}


def _path():
    return os.path.join(manager.DATA_DIR, DATA_FILE)


def parse(texts):
    """The networks in Cloudflare's lists, or None if they don't look like Cloudflare's lists:
    nothing but CIDRs, at least a few of each kind, none of them huge."""
    nets = []
    for text in texts:
        for line in str(text or "").split():
            try:
                net = ipaddress.ip_network(line.strip(), strict=True)
            except ValueError:
                return None
            if net.prefixlen < (8 if net.version == 4 else 20):
                return None
            nets.append(net)
    v4 = sum(1 for n in nets if n.version == 4)
    if v4 < 5 or len(nets) - v4 < 3 or len(nets) > 200:
        return None
    return nets


def _load_saved():
    try:
        with open(_path()) as f:
            data = json.load(f)
        nets = parse([" ".join(data.get("networks") or [])])
        return nets, float(data.get("fetched") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return None, 0.0


def networks():
    """Cloudflare's networks: the last list fetched, or the built-in copy."""
    with _lock:
        if _state["nets"] is None:
            nets, fetched = _load_saved()
            _state["nets"] = nets or [ipaddress.ip_network(n) for n in BUILT_IN]
            _state["checked"] = fetched
        return _state["nets"]


def contains(ip):
    return ip is not None and any(ip.version == n.version and ip in n for n in networks())


def _fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "RedstonePanel"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read(MAX_BODY).decode("ascii", "replace")


def refresh(fetch=_fetch):
    """Fetches the lists now. True when a good list was saved."""
    try:
        nets = parse([fetch(u) for u in SOURCES])
    except Exception:
        nets = None
    with _lock:
        _state["checked"] = time.time() - (REFRESH_EVERY - RETRY_AFTER if nets is None else 0)
        if nets is None:
            return False
        _state["nets"] = nets
    try:
        os.makedirs(manager.DATA_DIR, exist_ok=True)
        tmp = _path() + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"fetched": time.time(), "networks": [str(n) for n in nets]}, f, indent=1)
        os.replace(tmp, _path())
    except OSError:
        pass
    return True


def refresh_if_old():
    """Starts a refresh in the background when the list is a day old (or never fetched)."""
    networks()
    with _lock:
        if _state["refreshing"] or time.time() - _state["checked"] < REFRESH_EVERY:
            return
        _state["refreshing"] = True

    def run():
        try:
            refresh()
        finally:
            with _lock:
                _state["refreshing"] = False
    threading.Thread(target=run, name="cloudflare-ips", daemon=True).start()
