"""Panel-wide settings (data/settings.json) and the access rules that depend on them.

Defaults keep things simple: plain HTTP on 8080 and sign-in required.
Advanced options (admin only, Panel Settings):
  * https_mode: "off" | "self_signed" | "custom" | "cluster"
      (custom = admin-supplied cert/key, e.g. certbot; cluster = issued by the cluster's CA,
       set only by creating or joining a cluster, see core/cluster.py)
  * login_mode: "required" | "trusted_networks"   (no sign-in from private LAN ranges)
  * require_2fa_admins: admins must set up two-factor sign-in before they can do anything else
"""
import ipaddress
import json
import os
import re
import socket
import threading
import time
from urllib.parse import urlsplit

from core.platform import paths, system

APP_DIR = paths.APP_DIR

# Where state lives. A .deb install puts the code in /usr/share/redstonepanel and uses the
# standard Linux locations; a copy anywhere else (e.g. /opt/redstonepanel) keeps everything
# next to the code. RP_DATA_DIR / RP_TLS_DIR / RP_LOG_FILE override either (test instances).
# The paths themselves are spelled out in core/platform/paths.py.
PACKAGED = paths.PACKAGED
DATA_DIR = paths.DATA_DIR
TLS_DIR = paths.TLS_DIR
LOG_FILE = paths.LOG_FILE
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")

# Private ranges only. Loopback (127.0.0.0/8, ::1) is deliberately NOT included:
# a reverse proxy on this host would make every internet visitor look like 127.0.0.1.
DEFAULT_TRUSTED_NETWORKS = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7"]

DEFAULTS = {
    "https_mode": "off",
    "http_port": 8080,
    "https_port": 8443,
    "cert_file": "",
    "key_file": "",
    "login_mode": "required",
    "require_2fa_admins": False,
    "trusted_networks": DEFAULT_TRUSTED_NETWORKS,
    "extra_hosts": [],
    # Shown in the sidebar, browser tab and sign-in page to tell panels apart. "" = this host's name.
    "panel_name": "",
    # 1.18: the built-in SFTP server (its own service, redstonepanel-sftp.service). Off until an
    # admin turns it on under Panel Settings > SFTP.
    "sftp_enabled": False,
    "sftp_port": 2022,
}
PANEL_NAME_MAX = 40
CONNECTION_KEYS = ("https_mode", "http_port", "https_port", "cert_file", "key_file")
HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,62}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,62}[A-Za-z0-9])?)*$")

# Filled in by run.py at startup: how the panel is actually being served right now
RUNTIME = {"scheme": None, "port": None, "https_error": None}

_lock = threading.Lock()
_cache = {"mtime": None, "data": None}


class SettingsError(ValueError):
    """A setting value that can't be accepted (message is safe to show)."""


def load():
    """Current settings merged over the defaults (cached until the file changes)."""
    try:
        mtime = os.path.getmtime(SETTINGS_FILE)
    except OSError:
        mtime = None
    with _lock:
        if _cache["data"] is None or _cache["mtime"] != mtime:
            data = dict(DEFAULTS)
            if mtime is not None:
                try:
                    with open(SETTINGS_FILE, "r") as f:
                        stored = json.load(f)
                    data.update({k: v for k, v in stored.items() if k in DEFAULTS})
                except Exception:
                    pass   # unreadable file: fall back to safe defaults
            _cache.update(mtime=mtime, data=data)
        return dict(_cache["data"])


def save(data):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({k: data[k] for k in DEFAULTS}, f, indent=2)
    os.replace(tmp, SETTINGS_FILE)
    with _lock:
        _cache["data"] = None


def validate(new):
    """Returns a clean settings dict or raises SettingsError."""
    s = dict(DEFAULTS)
    s.update({k: v for k, v in new.items() if k in DEFAULTS})

    if s["https_mode"] not in ("off", "self_signed", "custom", "cluster"):
        raise SettingsError("Unknown HTTPS mode.")
    for key in ("http_port", "https_port"):
        try:
            s[key] = int(s[key])
        except (TypeError, ValueError):
            raise SettingsError(f"{key.replace('_', ' ').upper()} must be a number.")
        if not 1 <= s[key] <= 65535:
            raise SettingsError(f"{key.replace('_', ' ').upper()} must be between 1 and 65535.")
    if s["https_mode"] != "off" and s["http_port"] == s["https_port"]:
        raise SettingsError("The HTTP and HTTPS ports must be different.")

    s["cert_file"] = str(s["cert_file"] or "").strip()
    s["key_file"] = str(s["key_file"] or "").strip()
    if s["https_mode"] == "custom":
        if not (os.path.isabs(s["cert_file"]) and os.path.isabs(s["key_file"])):
            raise SettingsError("Give full paths to the certificate and key files.")
        from core import tls
        err = tls.check_pair(s["cert_file"], s["key_file"])
        if err:
            raise SettingsError(err)

    if s["login_mode"] not in ("required", "trusted_networks"):
        raise SettingsError("Unknown sign-in mode.")
    if not isinstance(s["require_2fa_admins"], bool):
        raise SettingsError("Require two-factor for admins must be on or off.")
    nets = []
    for raw in _as_list(s["trusted_networks"]):
        try:
            net = ipaddress.ip_network(raw, strict=False)
        except ValueError:
            raise SettingsError(f"'{raw}' is not a valid network (example: 192.168.1.0/24).")
        if not net.is_private or net.is_loopback or net.prefixlen == 0:
            raise SettingsError(f"'{raw}' is not a private network. Only LAN ranges can skip sign-in.")
        nets.append(str(net))
    if s["login_mode"] == "trusted_networks" and not nets:
        raise SettingsError("Add at least one trusted network, or keep sign-in required.")
    s["trusted_networks"] = nets

    hosts = []
    for raw in _as_list(s["extra_hosts"]):
        h = raw.lower()
        if not (HOSTNAME_RE.match(h) or _is_ip(h)):
            raise SettingsError(f"'{raw}' is not a valid host name.")
        hosts.append(h)
    s["extra_hosts"] = hosts

    s["panel_name"] = clean_panel_name(s["panel_name"])

    if not isinstance(s["sftp_enabled"], bool):
        raise SettingsError("SFTP must be on or off.")
    try:
        s["sftp_port"] = int(s["sftp_port"])
    except (TypeError, ValueError):
        raise SettingsError("The SFTP port must be a number.")
    if not 1 <= s["sftp_port"] <= 65535:
        raise SettingsError("The SFTP port must be between 1 and 65535.")
    if s["sftp_port"] in (s["http_port"], s["https_port"]) or s["sftp_port"] == 22:
        raise SettingsError("Choose an SFTP port that isn't the panel's own port or 22 (this machine's SSH).")
    return s


def clean_panel_name(name):
    """Trimmed panel name ("" means: use the host name). Raises SettingsError if unusable."""
    name = " ".join(str(name or "").split())          # collapse whitespace, drop newlines/tabs
    if len(name) > PANEL_NAME_MAX:
        raise SettingsError(f"The panel name can be at most {PANEL_NAME_MAX} characters.")
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise SettingsError("The panel name can't contain control characters.")
    return name


def host_name():
    return socket.gethostname().split(".")[0]


def display_name(s=None):
    """The name to show for this panel: the admin's choice, or this server's host name."""
    return (s or load())["panel_name"] or host_name()


def _as_list(v):
    if isinstance(v, str):
        v = re.split(r"[\s,]+", v)
    return [str(x).strip() for x in (v or []) if str(x).strip()]


def _is_ip(text):
    try:
        ipaddress.ip_address(text.strip("[]"))
        return True
    except ValueError:
        return False


def port_free(port):
    """True if nothing is listening on this TCP port (IPv4)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def panel_url(s, host):
    """The address the panel will be reachable at with settings s, for a browser that used `host`."""
    name = host_only(host) or "localhost"
    if ":" in name:
        name = f"[{name}]"
    if s["https_mode"] != "off":
        return f"https://{name}" + ("" if s["https_port"] == 443 else f":{s['https_port']}")
    return f"http://{name}" + ("" if s["http_port"] == 80 else f":{s['http_port']}")


# ---------------------------------------------------------------------------
# Request checks
# ---------------------------------------------------------------------------
def host_only(host_header):
    """'10.0.0.5:8080' -> '10.0.0.5', '[fd00::1]:8443' -> 'fd00::1', 'Box.lan' -> 'box.lan'."""
    h = (host_header or "").strip().lower()
    if h.startswith("["):
        return h[1:h.find("]")] if "]" in h else ""
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


_hosts_cache = {"at": 0.0, "hosts": set()}

def own_hosts():
    """Names and addresses that really are this machine (refreshed every minute)."""
    now = time.time()
    if now - _hosts_cache["at"] > 60:
        hosts = {"localhost", "127.0.0.1", "::1"}
        name = socket.gethostname().lower()
        hosts |= {name, name.split(".")[0], name.split(".")[0] + ".local"}
        try:
            hosts.add(socket.getfqdn().lower())
            hosts |= {ip.lower() for ip in system.addresses(timeout=3)}
        except Exception:
            pass
        _hosts_cache.update(at=now, hosts=hosts)
    return _hosts_cache["hosts"] | set(load()["extra_hosts"])


def same_origin(headers):
    """For state-changing requests: False if the browser says the request came from another site.
    Requests without Origin/Referer (scripts, curl) return None: 'unknown'."""
    source = headers.get("origin") or headers.get("referer")
    if not source or source == "null":
        return None if not source else False
    host = (headers.get("x-forwarded-host") or headers.get("host") or "").split(",")[0].strip().lower()
    src = urlsplit(source)
    src_host = (src.netloc or "").lower()
    default = {"http": "80", "https": "443"}.get(src.scheme)
    strip = lambda h, d: h[: -len(":" + d)] if d and h.endswith(":" + d) else h
    return bool(src_host) and (src_host == host or strip(src_host, default) == strip(host, default))


def trusted_network_user(client_ip, headers, method):
    """The anonymous 'local network' identity, or None if this request must sign in.
    Never granted through a proxy, for an unknown Host name (DNS rebinding), or for a
    state-changing request that doesn't prove it came from the panel's own pages."""
    s = load()
    if s["login_mode"] != "trusted_networks" or not client_ip:
        return None
    if headers.get("x-forwarded-for") or headers.get("forwarded") or headers.get("x-real-ip"):
        return None
    try:
        ip = ipaddress.ip_address(client_ip)
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
    except ValueError:
        return None
    if not any(ip in ipaddress.ip_network(n) for n in s["trusted_networks"]):
        return None
    if host_only(headers.get("host")) not in own_hosts():
        return None
    if method not in ("GET", "HEAD", "OPTIONS") and same_origin(headers) is not True:
        return None
    return {"username": "local network", "source": "trusted", "role": "user"}
