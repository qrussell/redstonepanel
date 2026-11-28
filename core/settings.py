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
    # 1.21.1: where new game servers are kept. "" = this system's usual place. Servers made
    # earlier stay where they are, so every folder that was ever used is remembered.
    "servers_folder": "",
    "servers_folders_used": [],
    # 1.21.5: public join pages for players (core/joinpage.py), on their own port so the panel
    # itself never has to face the internet. Off until an admin turns them on.
    "join_enabled": False,
    "join_port": 8444,
    "join_index": False,
    # 1.21.9: join pages behind a reverse proxy such as HAProxy (on pfSense or anywhere else).
    # join_proxies: the proxy's own addresses (IPs or networks); requests from them may say who
    # the player is in X-Forwarded-For. join_proxy_only: answer nobody else on the join port.
    # join_url: the address players use through the proxy ("" = the port-forwarding address).
    "join_proxies": [],
    "join_proxy_only": False,
    "join_url": "",
    # join_cloudflare: Cloudflare's proxy (orange cloud) sends players here; requests from
    # Cloudflare's own networks may name the player in CF-Connecting-IP (core/cloudflare.py).
    "join_cloudflare": False,
    # 1.21.10: the terminal (core/terminal.py): a shell as a Linux account, for admins. Off until
    # an admin turns it on under Panel Settings > Terminal.
    "terminal_enabled": False,
    "terminal_idle_minutes": 15,          # closes after this long with nothing typed
    "terminal_recent_minutes": 15,        # a Linux sign-in opens with no password for this long
    "terminal_allow_root": False,         # typing root at the login prompt
    "terminal_from_master": False,        # on a cluster node: the master may open terminals here
    "master_sign_in": True,               # 1.25, on a cluster node: the master's admins may open it signed in
}
JOIN_PROXIES_MAX = 8
TERMINAL_KEYS = ("terminal_enabled", "terminal_idle_minutes", "terminal_recent_minutes", "terminal_allow_root",
                 "terminal_from_master")
PANEL_NAME_MAX = 40
CONNECTION_KEYS = ("https_mode", "http_port", "https_port", "cert_file", "key_file")
HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,62}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,62}[A-Za-z0-9])?)*$")

# Filled in by run.py at startup: how the panel is actually being served right now
RUNTIME = {"scheme": None, "port": None, "https_error": None}
# The certificate the panel is serving with right now (run.py); the join pages use the same one
TLS_FILES = {"cert": None, "key": None}

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

    for key in ("join_enabled", "join_index"):
        if not isinstance(s[key], bool):
            raise SettingsError("Join pages must be on or off.")
    try:
        s["join_port"] = int(s["join_port"])
    except (TypeError, ValueError):
        raise SettingsError("The join pages port must be a number.")
    if not 1 <= s["join_port"] <= 65535:
        raise SettingsError("The join pages port must be between 1 and 65535.")
    if s["join_enabled"] and s["join_port"] in (s["http_port"], s["https_port"], s["sftp_port"], 22):
        raise SettingsError("Choose a join pages port that isn't the panel's own port, the SFTP port or 22.")
    if s["sftp_enabled"] and s["join_enabled"] and s["sftp_port"] == s["join_port"]:
        raise SettingsError("SFTP and the join pages need different ports.")
    s["join_proxies"] = clean_join_proxies(s["join_proxies"])
    if not isinstance(s["join_proxy_only"], bool) or not isinstance(s["join_cloudflare"], bool):
        raise SettingsError("The proxy settings must be on or off.")
    if s["join_proxy_only"] and not s["join_proxies"] and not s["join_cloudflare"]:
        raise SettingsError("Enter the proxy's address (or turn on Cloudflare) before allowing only the proxy.")
    s["join_url"] = clean_join_url(s["join_url"])

    if not isinstance(s["master_sign_in"], bool):
        raise SettingsError("Letting the master's admins sign in must be on or off.")
    for key in ("terminal_enabled", "terminal_allow_root", "terminal_from_master"):
        if not isinstance(s[key], bool):
            raise SettingsError("Terminal settings must be on or off.")
    for key, low, high, what in (("terminal_idle_minutes", 5, 120, "Close after"),
                                 ("terminal_recent_minutes", 0, 60, "Skip the password for")):
        try:
            s[key] = int(s[key])
        except (TypeError, ValueError):
            raise SettingsError(f"{what} must be a number of minutes.")
        if not low <= s[key] <= high:
            raise SettingsError(f"{what} must be {low} to {high} minutes.")

    s["servers_folder"] = clean_servers_folder(s["servers_folder"])
    used = []
    for raw in s["servers_folders_used"] if isinstance(s["servers_folders_used"], list) else []:
        if isinstance(raw, str) and raw and raw not in used and raw != s["servers_folder"]:
            used.append(raw)
    s["servers_folders_used"] = used
    return s


def clean_join_proxies(value):
    """The reverse proxy's addresses as a list of networks ("10.0.0.1/32" style is shortened to
    "10.0.0.1"). Accepts a list or text separated by commas, spaces or new lines."""
    if isinstance(value, str):
        value = re.split(r"[\s,]+", value)
    if not isinstance(value, list):
        raise SettingsError("The proxy addresses must be a list.")
    out = []
    for raw in value:
        text = str(raw or "").strip()
        if not text:
            continue
        try:
            net = ipaddress.ip_network(text, strict=False)
        except ValueError:
            raise SettingsError(f"'{text[:60]}' isn't an IP address or network (like 10.0.0.1 or 10.0.0.0/24).")
        if net.prefixlen == 0:
            raise SettingsError("Trusting every address as a proxy would let anyone pretend to be anyone. "
                                "Enter only the proxy's own address.")
        if net.num_addresses == 1 and net.version == 6 and net.network_address.ipv4_mapped:
            net = ipaddress.ip_network(net.network_address.ipv4_mapped)
        name = str(net.network_address) if net.num_addresses == 1 else str(net)
        if name not in out:
            out.append(name)
    if len(out) > JOIN_PROXIES_MAX:
        raise SettingsError(f"Enter at most {JOIN_PROXIES_MAX} proxy addresses.")
    return out


def clean_join_url(value):
    """The address players open through the proxy, like https://play.example.com, or "".
    Only the scheme, host and port: the pages are always under /join."""
    url = str(value or "").strip().rstrip("/")
    if not url:
        return ""
    if len(url) > 200 or any(c.isspace() or ord(c) < 32 or c in "\"'<>`\\" for c in url):
        raise SettingsError("The join pages address isn't usable.")
    if "://" not in url:
        url = "https://" + url
    u = urlsplit(url)
    try:
        port = u.port
    except ValueError:
        raise SettingsError("The port in the join pages address must be a number between 1 and 65535.")
    if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password or u.query or u.fragment:
        raise SettingsError("Enter the join pages address like https://play.example.com.")
    if u.path not in ("", "/join"):
        raise SettingsError("Enter the join pages address without a path, like https://play.example.com. "
                            "The pages are always under /join.")
    host = u.hostname
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not HOSTNAME_RE.match(host):
            raise SettingsError("Enter the join pages address like https://play.example.com.")
    else:
        host = f"[{host}]" if ":" in host else host
    return f"{u.scheme}://{host.lower()}" + (f":{port}" if port else "")


def clean_servers_folder(folder):
    """The folder as it is stored ("" = the usual place). Raises SettingsError when the panel
    can't keep servers there; what is and isn't a fit place is the operating system's business."""
    folder = str(folder or "").strip()
    if not folder:
        return ""
    from core.platform import paths, system
    # The panel tells a Java server from a Bedrock one by those words in how the server is started
    if "java" in folder.lower() or "bedrock" in folder.lower():
        raise SettingsError('The folder\'s path can\'t contain "java" or "bedrock": the panel uses those words to '
                            'tell the two kinds of server apart. "minecraft" or "servers" is fine.')
    clean, error = system.check_servers_folder(folder)
    if error:
        raise SettingsError(error)
    return "" if clean == paths.SERVERS_ROOT else clean


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


def panel_ports(s=None):
    """The TCP ports the panel itself listens on: HTTP, and HTTPS when it's on."""
    s = s or load()
    return [s["http_port"]] + ([s["https_port"]] if s["https_mode"] != "off" else [])


def sync_panel_firewall(s=None):
    """Windows: makes the panel's own firewall rule open exactly panel_ports() (Linux leaves the
    panel's ports to the administrator). Called whenever HTTPS or the ports change, and when the
    panel starts. 1.24.1: creating or joining a cluster turned HTTPS on without opening its port,
    so a fresh Windows node couldn't be reached by its master. Never raises; returns 0 when done."""
    try:
        from core.platform import firewall
        return firewall.panel_ports(panel_ports(s))
    except Exception:
        return 1


def panel_url(s, host):
    """The address the panel will be reachable at with settings s, for a browser that used `host`."""
    name = host_only(host) or "localhost"
    try:                                    # 1.29: opened through Cloudflare Tunnel, which has no port
        from core import cftunnel
        public = cftunnel.public_panel_url()
        if public and urlsplit(public).hostname == name:
            return public
    except Exception:
        pass
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
    if headers.get("x-forwarded-for") or headers.get("forwarded") or headers.get("x-real-ip") \
            or headers.get("cf-connecting-ip"):          # 1.29: never through Cloudflare Tunnel either
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
