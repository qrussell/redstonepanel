"""Join pages (1.21.5): a public page per game server that tells players how to join.

Each server can have one page, off until an admin turns it on. The pages are served by
core/joinsite.py on their own port (Panel Settings > Join pages), so the panel itself never
has to be reachable from the internet. This module holds what each page is allowed to say:

  * the page's settings (data/join-pages.json): its link, the address players type, a note,
    a link of the admin's choice, whether player names are shown;
  * local_summary(): what this machine knows about one of its servers (type, version, ports,
    the modpack it was made from, who is online). A cluster master reads the same from a node
    (GET /api/servers/NAME/join-info, node token) and serves the pages for the node's servers; nodes
    open nothing new;
  * public_view(): the fixed list of fields that leaves the machine. Never a LAN address, a
    machine or node name, a folder, the panel's version, an account name or a plugin list.

The address players type is only ever what an admin typed (here, or as the public address on
the Port forwarding page). The panel never publishes an address it worked out by itself.
"""
import base64
import ipaddress
import json
import os
import re
import secrets
import socket
import struct
import threading
import time
from urllib.parse import urlsplit, quote

from core import javaservers
from core import manager
from core import portforward
from core import settings as panel_settings

DATA_FILE = "join-pages.json"
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")
RESERVED_SLUGS = {"assets", "index", "status", "api", "static"}
NOTE_MAX = 600
LABEL_MAX = 40
URL_MAX = 300
ICON_MAX = 256 * 1024
STATUS_TTL = 30                 # seconds a server's summary is reused, so the page can't hammer the game server
PING_TIMEOUT = 2
NAMES_MAX = 20
DEFAULT_PORTS = {"Java": "25565", "Bedrock": "19132"}

_lock = threading.RLock()
_summary_cache = {}             # key -> (time, summary)
_pack_cache = {}                # key -> ((pack version, title), .mrpack bytes) for a node's server


class JoinError(ValueError):
    """A request that can't be done (message is safe to show)."""


# ---------------------------------------------------------------------------
# Saved pages: {"pages": {"<key>": {...}}}; key = server name, or "<node id>/<server>" on a master
# ---------------------------------------------------------------------------
def _path():
    return os.path.join(manager.DATA_DIR, DATA_FILE)


def page_key(member, server):
    return server if member in (None, "", "local") else f"{member}/{server}"


def split_key(key):
    member, sep, server = key.rpartition("/")
    return (member, server) if sep else ("local", key)


def load():
    try:
        with open(_path()) as f:
            data = json.load(f)
        pages = data.get("pages") if isinstance(data, dict) else None
        return {k: v for k, v in (pages or {}).items() if isinstance(k, str) and isinstance(v, dict)}
    except (OSError, ValueError):
        return {}


def _save(pages):
    os.makedirs(manager.DATA_DIR, exist_ok=True)
    tmp = _path() + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"pages": pages}, f, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, _path())


def default_slug(server):
    """'minecraft-survival' -> 'survival'."""
    slug = re.sub(r"[^a-z0-9]+", "-", server.lower()).strip("-")
    slug = slug[len("minecraft-"):] if slug.startswith("minecraft-") and len(slug) > len("minecraft-") else slug
    return (slug or "server")[:40].strip("-") or "server"


def defaults(server):
    return {"enabled": False, "slug": default_slug(server), "unlisted": False, "token": "",
            "address": "", "note": "", "link_url": "", "link_label": "", "pack_url": "",
            "show_players": False, "allow_start": False}


def page(key, pages=None):
    member, server = split_key(key)
    cfg = defaults(server)
    cfg.update({k: v for k, v in ((pages if pages is not None else load()).get(key) or {}).items() if k in cfg})
    return cfg


def _text(value, limit, what):
    text = " ".join(str(value or "").split()) if limit <= LABEL_MAX else str(value or "").strip()
    text = "".join(c for c in text if c == "\n" or ord(c) >= 32 and ord(c) != 127)
    if len(text) > limit:
        raise JoinError(f"{what} can be at most {limit} characters.")
    return text


def clean_url(value, what):
    """An https:// (or http://) link, or ''. Nothing else can become a link on a public page."""
    url = str(value or "").strip()
    if not url:
        return ""
    if len(url) > URL_MAX or any(c.isspace() or ord(c) < 32 or c in "\"'<>`\\" for c in url):
        raise JoinError(f"{what} isn't a usable link.")
    u = urlsplit(url)
    if u.scheme not in ("https", "http") or not u.hostname or u.username or u.password:
        raise JoinError(f"{what} must start with https://")
    return url


def clean_address(value):
    """What players type: a host name or IP address, optionally with :port. '' = the public
    address from the Port forwarding page and the server's own port."""
    text = str(value or "").strip()
    if not text:
        return ""
    host, port = text, ""
    if text.startswith("["):
        host, _, rest = text[1:].partition("]")
        port = rest[1:] if rest.startswith(":") else ""
        if rest and not rest.startswith(":"):
            raise JoinError("Enter an address like play.example.com or play.example.com:25566.")
    elif text.count(":") == 1:
        host, port = text.split(":")
    try:
        host = portforward.clean_address(host)
    except portforward.ForwardError as e:
        raise JoinError(str(e))
    if not host:
        raise JoinError("Enter an address like play.example.com or play.example.com:25566.")
    if port and not (port.isdigit() and 0 < int(port) < 65536):
        raise JoinError("The port after the address must be a number between 1 and 65535.")
    shown = f"[{host}]" if ":" in host else host
    return f"{shown}:{int(port)}" if port else shown


def save_page(member, server, new, actor="?"):
    """Stores one server's page settings. Returns the cleaned settings."""
    key = page_key(member, server)
    with _lock:
        pages = load()
        cfg = page(key, pages)
        cfg["enabled"] = bool(new.get("enabled"))
        cfg["unlisted"] = bool(new.get("unlisted"))
        cfg["show_players"] = bool(new.get("show_players"))
        cfg["allow_start"] = bool(new.get("allow_start"))        # 1.26: a Start button for visitors
        slug = str(new.get("slug") or "").strip().lower() or default_slug(server)
        if not SLUG_RE.match(slug) or slug in RESERVED_SLUGS:
            raise JoinError("The link name can use small letters, numbers and dashes, up to 40 characters "
                            "(for example survival or family-world).")
        for other, o in pages.items():
            if other != key and page(other, pages)["slug"] == slug and (o.get("enabled") or cfg["enabled"]):
                raise JoinError(f"Another server's join page already uses the link name '{slug}'.")
        cfg["slug"] = slug
        if cfg["unlisted"] and (not cfg["token"] or new.get("new_link")):
            cfg["token"] = secrets.token_urlsafe(12)
        cfg["address"] = clean_address(new.get("address"))
        cfg["note"] = _text(new.get("note"), NOTE_MAX, "The note")
        cfg["link_url"] = clean_url(new.get("link_url"), "The link")
        cfg["link_label"] = _text(new.get("link_label"), LABEL_MAX, "The link's name")
        cfg["pack_url"] = clean_url(new.get("pack_url"), "The modpack link")
        pages[key] = cfg
        _save(pages)
    manager.write_log(f"[JOIN] {actor} turned the join page for {server} {'on' if cfg['enabled'] else 'off'}"
                      + (f" (link /join/{path_of(cfg)})" if cfg["enabled"] else "") + ".")
    return cfg


def forget(member, server):
    """A server that was removed: its page goes with it."""
    key = page_key(member, server)
    with _lock:
        pages = load()
        if pages.pop(key, None) is not None:
            _save(pages)
    _summary_cache.pop(key, None)


def path_of(cfg):
    return cfg["token"] if cfg["unlisted"] and cfg["token"] else cfg["slug"]


def find(path_part):
    """The enabled page served at /join/<path_part>: (key, settings) or None. An unlisted page
    answers only to its random link, never to its name."""
    if not path_part or len(path_part) > 64:
        return None
    pages = load()
    for key in pages:
        cfg = page(key, pages)
        if cfg["enabled"] and secrets.compare_digest(path_of(cfg).encode(), path_part.encode()):
            return key, cfg
    return None


def listed():
    """Enabled pages that may appear on the index: [(key, settings)]."""
    pages = load()
    out = [(k, page(k, pages)) for k in pages]
    return sorted(((k, c) for k, c in out if c["enabled"] and not c["unlisted"]), key=lambda x: x[1]["slug"])


# ---------------------------------------------------------------------------
# Who is online: asked of the game server itself, on this machine
# ---------------------------------------------------------------------------
def java_ping(host, port, timeout=PING_TIMEOUT):
    """Server List Ping: {"online", "max", "names", "version", "motd", "icon"} or raises OSError."""
    with socket.create_connection((host, int(port)), timeout=timeout) as s:
        s.settimeout(timeout)
        h = host.encode()[:255]
        hs = (b"\x00" + portforward._varint(767) + portforward._varint(len(h)) + h
              + struct.pack(">H", int(port)) + b"\x01")
        s.sendall(portforward._varint(len(hs)) + hs + b"\x01\x00")
        length = portforward._read_varint(s)
        if not 0 < length < 1 << 20:
            raise OSError("bad length")
        data = b""
        while len(data) < length:
            chunk = s.recv(length - len(data))
            if not chunk:
                break
            data += chunk
    i, n, shift = 1, 0, 0
    while i < len(data):
        b = data[i]; i += 1
        n |= (b & 0x7F) << shift; shift += 7
        if not b & 0x80:
            break
    try:
        js = json.loads(data[i:i + n].decode("utf-8", "replace"))
    except ValueError:
        raise OSError("not a Minecraft server")
    if not isinstance(js, dict):
        raise OSError("not a Minecraft server")
    players = js.get("players") if isinstance(js.get("players"), dict) else {}
    names = [str(p.get("name"))[:16] for p in players.get("sample") or []
             if isinstance(p, dict) and re.fullmatch(r"[A-Za-z0-9_.*]{1,16}", str(p.get("name") or ""))]
    icon = js.get("favicon") if isinstance(js.get("favicon"), str) else ""
    return {"online": _count(players.get("online")), "max": _count(players.get("max")), "names": names[:NAMES_MAX],
            "version": str((js.get("version") or {}).get("name") or "")[:40] if isinstance(js.get("version"), dict) else "",
            "motd": portforward._text(js.get("description")).strip()[:200],
            "icon": icon[len("data:image/png;base64,"):] if icon.startswith("data:image/png;base64,") else ""}


def _count(v):
    return v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 1000000 else None


def bedrock_ping(host, port, timeout=PING_TIMEOUT):
    """RakNet unconnected ping: {"online", "max", "version", "motd"} or raises OSError."""
    fam = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(fam, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        s.sendto(b"\x01" + struct.pack(">Q", int(time.time() * 1000) & (2 ** 63 - 1)) + portforward.RAKNET_MAGIC
                 + os.urandom(8), (host, int(port)))
        data, _ = s.recvfrom(2048)
    if not data or data[0] != 0x1c or portforward.RAKNET_MAGIC not in data:
        raise OSError("not a Bedrock server")
    i = data.index(portforward.RAKNET_MAGIC) + len(portforward.RAKNET_MAGIC)
    n = struct.unpack(">H", data[i:i + 2])[0]
    parts = data[i + 2:i + 2 + n].decode("utf-8", "replace").split(";")
    num = lambda k: _count(int(parts[k])) if len(parts) > k and parts[k].isdigit() else None
    return {"online": num(4), "max": num(5), "names": [], "version": parts[3][:40] if len(parts) > 3 else "",
            "motd": re.sub("§.", "", parts[1])[:200] if len(parts) > 1 else "", "icon": ""}


def _bind_host(props):
    ip = str(props.get("server-ip") or "").strip()
    try:
        return str(ipaddress.ip_address(ip)) if ip and ip not in ("0.0.0.0", "::") else "127.0.0.1"
    except ValueError:
        return "127.0.0.1"


def _icon_file(server):
    """server-icon.png from the server's folder (never through a symlink), base64, or ''."""
    from core import files
    try:
        fd, size, _ = files.open_for_download(server, "server-icon.png")
    except Exception:
        return ""
    try:
        if not 0 < size <= ICON_MAX:
            return ""
        with os.fdopen(fd, "rb") as f:
            fd = None
            data = f.read(ICON_MAX + 1)
    finally:
        if fd is not None:
            os.close(fd)
    return base64.b64encode(data).decode() if data.startswith(b"\x89PNG\r\n\x1a\n") else ""


def _modpack(server_dir):
    from core import modpacks
    rec = modpacks.read_record(server_dir)
    if not rec:
        return None
    return {"name": str(rec.get("name") or "")[:80], "version": str(rec.get("version") or "")[:40],
            "source": "modrinth" if rec.get("project") else "file",
            "slug": str(rec.get("slug") or rec.get("project") or "")[:64] if rec.get("project") else "",
            "version_id": str(rec.get("version_id") or "")[:16] if rec.get("project") else ""}


# ---------------------------------------------------------------------------
# What this machine knows about one of its servers
# ---------------------------------------------------------------------------
def local_summary(server, list_servers=None):
    """Everything a join page may need about a server on this machine. Raises JoinError if
    there's no such server."""
    s = next((x for x in (list_servers or manager.list_servers)() if x["name"] == server), None)
    if not s:
        raise JoinError(f"No Minecraft server called '{server}' on this panel.")
    d = s.get("directory") or manager.get_server_dir(server)
    props = manager.read_properties(d)
    port = str(props.get("server-port", s.get("port", ""))).strip()
    edition = s.get("edition")
    loader = str(s.get("loader") or "").lower() if edition == "Java" else ""
    loader = loader if loader in javaservers.LOADERS else ("vanilla" if edition == "Java" else "")
    out = {"name": server, "edition": edition, "loader": loader,
           "minecraft": str(s.get("minecraft") or s.get("version") or "")[:40],
           "status": s.get("status") if s.get("status") in ("online", "offline") else "offline",
           "port": port if port.isdigit() else "", "motd": str(s.get("motd") or "")[:200],
           "transport": s.get("transport"), "geyser_port": "", "modpack": None, "icon": "",
           "players": None, "player_pack": None, "sleeping": bool(s.get("sleeping"))}
    if edition == "Java":
        out["modpack"] = _modpack(d)
        if loader in javaservers.MOD_LOADERS:
            try:
                from core import playerpack
                out["player_pack"] = playerpack.public_part(playerpack.manifest(server))
            except Exception:
                out["player_pack"] = None            # the page still says which loader and version
        try:
            for r in portforward.java_extras(server, props):
                if r["what"].startswith("Geyser"):
                    out["geyser_port"] = r["port"]
        except Exception:
            pass
    ping = None
    if out["status"] == "online" and out["port"]:
        try:
            if edition == "Java":
                ping = java_ping(_bind_host(props), out["port"])
            elif edition == "Bedrock" and s.get("transport") != "nethernet":
                ping = bedrock_ping(_bind_host(props), out["port"])
        except (OSError, ValueError):
            ping = None                          # still starting, or not answering: no player count
    if ping:
        out["players"] = {"online": ping["online"], "max": ping["max"], "names": ping["names"]}
        out["motd"] = ping["motd"] or out["motd"]
        out["icon"] = ping["icon"] if len(ping["icon"]) <= ICON_MAX * 4 // 3 + 4 else ""
    if edition == "Java" and not out["icon"]:
        out["icon"] = _icon_file(server)
    return out


def _node_summary(member, server):
    from core import cluster, cluster_client
    node = cluster.find_node(member)
    if not node:
        raise JoinError("That cluster member is gone.")
    try:
        got = cluster_client.call_node(node, "GET", f"/api/servers/{quote(server, safe='')}/join-info", timeout=10,
                                       role="admin")
        if isinstance(got, dict) and got.get("name") == server:
            return got
        raise JoinError("The answer was incomplete.")
    except cluster_client.NodeError as e:
        if e.state != "too_old" and e.status != 404:
            raise JoinError(str(e))
    # A node from before 1.21.5: its server list still says the type, version and whether it runs
    try:
        servers = cluster_client.call_node(node, "GET", "/api/servers", timeout=10)
    except cluster_client.NodeError as e:
        raise JoinError(str(e))
    s = next((x for x in servers if isinstance(x, dict) and x.get("name") == server), None)
    if not s:
        raise JoinError(f"No Minecraft server called '{server}' on that member.")
    edition = s.get("edition")
    loader = str(s.get("loader") or "").lower()
    port = str(s.get("port") or "")
    return {"name": server, "edition": edition,
            "loader": loader if loader in javaservers.LOADERS else ("vanilla" if edition == "Java" else ""),
            "minecraft": str(s.get("minecraft") or s.get("version") or "")[:40],
            "status": "online" if s.get("status") == "online" else "offline",
            "port": port if port.isdigit() else "", "motd": str(s.get("motd") or "")[:200],
            "transport": s.get("transport"), "geyser_port": "", "modpack": None, "icon": "", "players": None}


def summary(key, fresh=False):
    """local_summary for a local server, the node's for a node's server; reused for STATUS_TTL
    seconds. When a member can't be reached the last answer is shown as offline."""
    now = time.time()
    hit = _summary_cache.get(key)
    if hit and not fresh and now - hit[0] < STATUS_TTL:
        return hit[1]
    member, server = split_key(key)
    try:
        got = local_summary(server) if member == "local" else _node_summary(member, server)
    except JoinError:
        if member == "local" or not hit:
            raise
        got = dict(hit[1], status="offline", players=None)
    except Exception:
        if not hit:
            raise JoinError("That server can't be read right now.")
        got = dict(hit[1], status="offline", players=None)
    _summary_cache[key] = (now, got)
    if len(_summary_cache) > 500:
        for k in sorted(_summary_cache, key=lambda k: _summary_cache[k][0])[:100]:
            _summary_cache.pop(k, None)
    return got


# ---------------------------------------------------------------------------
# What leaves the machine
# ---------------------------------------------------------------------------
def _split_address(text):
    """'host', 'host:port' or '[v6]:port' -> (host as shown, port or '')."""
    if not text:
        return "", ""
    if text.startswith("["):
        host, _, rest = text.partition("]")
        return host + "]", rest[1:] if rest.startswith(":") else ""
    if text.count(":") == 1:
        host, port = text.split(":")
        return host, port
    return (f"[{text}]" if ":" in text else text), ""


def addresses(cfg, s, public_address=None):
    """What players type. Java: host, or host:port when it isn't 25565. Bedrock: host and port
    apart. Empty when no address was typed anywhere."""
    typed = cfg["address"]
    if not typed:
        typed = public_address if public_address is not None else portforward.load()["public_address"]
        typed = f"[{typed}]" if typed and ":" in typed and not typed.startswith("[") else typed
    host, port = _split_address(typed or "")
    out = {"host": host, "java": "", "bedrock": None}
    if not host:
        return out
    game_port = port or s.get("port") or DEFAULT_PORTS.get(s.get("edition"), "")
    if s.get("edition") == "Java":
        out["java"] = host if game_port in ("", "25565") else f"{host}:{game_port}"
        if s.get("geyser_port"):
            out["bedrock"] = {"host": host.strip("[]"), "port": s["geyser_port"]}
    elif s.get("edition") == "Bedrock":
        out["bedrock"] = {"host": host.strip("[]"), "port": game_port or "19132"}
    return out


def pack_links(cfg, s):
    """Where players get the mods: the Modrinth page of the exact pack version the server was
    made from, or the link the admin typed. None when there's nothing to link."""
    mp = s.get("modpack") or None
    if mp and mp.get("source") == "modrinth" and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", mp.get("slug") or ""):
        url = f"https://modrinth.com/modpack/{mp['slug']}"
        if re.fullmatch(r"[A-Za-z0-9]{1,16}", mp.get("version_id") or ""):
            url += f"/version/{mp['version_id']}"
        return {"name": mp.get("name") or "Modpack", "version": mp.get("version") or "", "source": "modrinth",
                "url": url, "app_url": f"modrinth://modpack/{mp['slug']}"}
    if cfg["pack_url"]:
        return {"name": (mp or {}).get("name") or "Modpack", "version": (mp or {}).get("version") or "",
                "source": "link", "url": cfg["pack_url"], "app_url": ""}
    if mp:
        return {"name": mp.get("name") or "Modpack", "version": mp.get("version") or "", "source": "none",
                "url": "", "app_url": ""}
    return None


def _pack_title(cfg, s, p):
    title = re.sub("§.", "", s.get("motd") or "").strip() or default_slug(s.get("name") or "server")
    return (p.get("name") or title)[:60] or "Player pack"


def player_pack(key):
    """(public part, title, .mrpack bytes) of a page's player pack, or (None, None, None).
    A node's pack is built here from what the node said; only the jars an admin chose to hand
    out are fetched from the node, and each is checked against its checksum."""
    from core import playerpack
    member, server = split_key(key)
    s = summary(key)
    p = playerpack.clean_public(s.get("player_pack"))
    if not p:
        return None, None, None
    title = _pack_title(page(key), s, p)
    if member == "local":
        got, data = playerpack.local_pack(server, title)
        return (playerpack.clean_public(got), title, data) if got else (None, None, None)
    hit = _pack_cache.get(key)
    if hit and hit[0] == (p["version"], title):
        return p, title, hit[1]
    read_jar = None
    if any(l["state"] == "bundled" for l in p["mods"]):
        from core import cluster, cluster_client
        node = cluster.find_node(member)
        raw = None
        if node:
            try:
                res = cluster_client.open_proxy(node, "GET", f"/api/servers/{quote(server, safe='')}/player-pack/file",
                                                role="admin")
                raw = res.read_all(playerpack.MAX_BUNDLED + (1 << 20)) if res.status == 200 else (res.close() or None)
            except Exception:
                raw = None
        read_jar = (lambda name: playerpack.bundled_jar(raw, name)) if raw else None
    data = playerpack.mrpack(p, title, read_jar)
    _pack_cache[key] = ((p["version"], title), data)
    if len(_pack_cache) > 50:
        _pack_cache.pop(next(iter(_pack_cache)))
    return p, title, data


def _player_pack_view(cfg, s):
    from core import playerpack
    p = playerpack.clean_public(s.get("player_pack"))
    if not p or not p["mods"]:
        return None
    return {"title": _pack_title(cfg, s, p), "version": p["version"],
            "file": playerpack.file_name(_pack_title(cfg, s, p), p["version"]),
            "count": len(p["mods"]), "optional": sum(1 for l in p["mods"] if l["state"] == "optional"),
            "mods": [{"name": l["name"], "version": l["version"], "optional": l["state"] == "optional",
                      "url": f"https://modrinth.com/mod/{l['slug']}" if l["slug"] and l["state"] != "bundled" else ""}
                     for l in sorted(p["mods"], key=lambda l: l["name"].lower())][:400],
            "ask": p["ask"]}


def public_view(cfg, s, public_address=None, key=None):
    """The page's whole content, from the fixed list of fields that may leave the machine."""
    loader = s.get("loader") or ""
    players = s.get("players") if s.get("status") == "online" else None
    title = s.get("motd") or default_slug(s.get("name") or "server")
    needs = "bedrock" if s.get("edition") == "Bedrock" else "mods" if loader in javaservers.MOD_LOADERS else "none"
    return {
        "path": path_of(cfg),
        "title": re.sub("§.", "", title).strip()[:80] or "Minecraft server",
        "edition": s.get("edition") if s.get("edition") in ("Java", "Bedrock") else "",
        "loader": javaservers.LOADERS.get(loader, "") if loader else "",
        "loader_key": loader if needs == "mods" else "",
        "minecraft": s.get("minecraft") or "",
        "needs": needs,
        "status": _public_state(key, s),
        "can_start": bool(cfg.get("allow_start")) and s.get("status") != "online",
        "players": None if not players else {
            "online": players.get("online"), "max": players.get("max"),
            "names": list(players.get("names") or [])[:NAMES_MAX] if cfg["show_players"] else None},
        "address": addresses(cfg, s, public_address),
        "geyser": bool(s.get("geyser_port")) and s.get("edition") == "Java",
        "pack": pack_links(cfg, s) if needs == "mods" else None,
        "player_pack": _player_pack_view(cfg, s) if needs == "mods" else None,
        "note": cfg["note"],
        "link": {"url": cfg["link_url"], "label": cfg["link_label"] or "More about this server"} if cfg["link_url"] else None,
        "has_icon": str(s.get("icon") or "").startswith("iVBORw0KGgo"),     # a PNG, in base64
    }


def public_status(view):
    """The part of the page that refreshes by itself."""
    return {"status": view["status"], "players": view["players"], "minecraft": view["minecraft"],
            "can_start": view["can_start"]}


# ---------------------------------------------------------------------------
# 1.26: starting a stopped (or sleeping) server from its page, when the admin allows it
# ---------------------------------------------------------------------------
START_COOLDOWN = 120            # seconds: one start per server in this time, however many visitors press
START_SHOWN = 240               # seconds the page says "Starting" after a start, until the server answers
START_PER_IP = 3                # starts one address may ask for in an hour, over all servers
_starts = {}                    # key -> time of the last start asked from a page
_starts_by_ip = {}              # ip -> [times]


def _public_state(key, s):
    """online, starting, sleeping or offline: what a visitor may know."""
    if s.get("status") == "online":
        return "online"
    if key and time.time() - _starts.get(key, 0) < START_SHOWN:
        return "starting"
    return "sleeping" if s.get("sleeping") else "offline"


def start_from_page(key, ip, now=None):
    """A visitor pressed Start on the page of `key`. Returns "online", "starting" or raises JoinError.
    The admin must have allowed it on that page. A start already asked in the last
    START_COOLDOWN seconds is not repeated; an address gets START_PER_IP starts an hour."""
    now = time.time() if now is None else now
    cfg = page(key)
    if not cfg["enabled"] or not cfg.get("allow_start"):
        raise JoinError("This server can't be started from its page.")
    s = summary(key, fresh=True)
    if s.get("status") == "online":
        return "online"
    with _lock:
        if now - _starts.get(key, 0) < START_COOLDOWN:
            return "starting"
        times = [t for t in _starts_by_ip.get(ip, []) if now - t < 3600]
        if len(times) >= START_PER_IP:
            raise JoinError("You've started servers here a few times in the last hour. Try again later.")
        if len(_starts_by_ip) > 5000:
            _starts_by_ip.clear()
        _starts_by_ip[ip] = times + [now]
        _starts[key] = now
    member, server = split_key(key)
    manager.write_log(f"[JOIN] A visitor from {ip} started {server}"
                      + ("" if member == "local" else f" on cluster member {member}") + " from its join page.")
    threading.Thread(target=_start_server, args=(member, server, ip), name=f"join-start-{server}", daemon=True).start()
    return "starting"


def _start_server(member, server, ip):
    try:
        if member == "local":
            from core import sleep
            if not sleep.wake(server, f"a visitor of its join page ({ip})"):
                manager.control_server(server, "start")
        else:
            from core import cluster, cluster_client
            node = cluster.find_node(member)
            if not node:
                raise JoinError("That cluster member is gone.")
            cluster_client.call_node(node, "POST", f"/api/servers/{quote(server, safe='')}/action", {"action": "start"},
                                     actor="join page", timeout=cluster_client.ACTION_TIMEOUT)
            cluster_client.forget_cached(member)
    except Exception as e:
        manager.write_log(f"[JOIN] Starting {server} from its join page failed: {e}")
    finally:
        _summary_cache.pop(page_key(member, server), None)


# ---------------------------------------------------------------------------
# The admin's page in the panel
# ---------------------------------------------------------------------------
def admin_overview(list_servers=None):
    """Every server a page can be made for (the master's own and its nodes'), with its settings."""
    from core import cluster, cluster_client
    pages = load()
    members = []
    if cluster.role() == "master":
        ov = cluster_client.overview(list_servers or manager.list_servers)
        for n in ov["nodes"]:
            members.append({"id": n["id"], "name": n["name"], "local": bool(n.get("local")), "state": n.get("state"),
                            "error": n.get("error"), "servers": n.get("servers") or []})
    else:
        members.append({"id": "local", "name": panel_settings.display_name(), "local": True, "state": "ok",
                        "error": None, "servers": (list_servers or manager.list_servers)()})
    out = []
    for m in members:
        rows = []
        for s in m["servers"]:
            if not isinstance(s, dict) or not s.get("name"):
                continue
            key = page_key(m["id"], s["name"])
            cfg = page(key, pages)
            loader = str(s.get("loader") or "").lower()
            rows.append({"name": s["name"], "edition": s.get("edition"), "loader": javaservers.LOADERS.get(loader, ""),
                         "mods": loader in javaservers.MOD_LOADERS, "minecraft": s.get("minecraft") or s.get("version"),
                         "status": s.get("status"), "port": s.get("port"), "motd": s.get("motd"),
                         "sleep": bool(s.get("sleep")), "sleeping": bool(s.get("sleeping")),
                         "page": {k: v for k, v in cfg.items() if k != "token"}, "path": path_of(cfg)})
        out.append({"id": m["id"], "name": m["name"], "local": m["local"], "state": m["state"], "error": m["error"],
                    "servers": rows})
    return {"members": out, "public_address": portforward.load()["public_address"]}
