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
            "show_players": False, "allow_start": False,
            # 1.28: who can see the page (anyone / signed / permission), how visitors sign in, and
            # what each signed-in visitor may do (rules)
            "access": "anyone", "signin_panel": True, "signin_discord": False, "discord_member": False,
            "discord_invite": "", "rules": [],
            # 1.30: a Map button on the page (the server's world map, core/worldmap.py), and
            # whether that map shows where players are
            "show_map": False, "map_players": False}


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
        if "show_map" in new:                                     # 1.30; older forms leave it as it was
            cfg["show_map"] = bool(new.get("show_map"))
            cfg["map_players"] = bool(new.get("map_players"))
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
        if "access" in new:                                       # 1.28; older forms leave it as it was
            _clean_access(cfg, new)
        pages[key] = cfg
        _save(pages)
    manager.write_log(f"[JOIN] {actor} turned the join page for {server} {'on' if cfg['enabled'] else 'off'}"
                      + (f" (link /join/{path_of(cfg)})" if cfg["enabled"] else "")
                      + (f", {ACCESS_LABELS[cfg['access']]}" if cfg["enabled"] and cfg["access"] != "anyone" else "")
                      + ".")
    return cfg


# ---------------------------------------------------------------------------
# 1.28: who can see a page, and what a signed-in visitor may do
# ---------------------------------------------------------------------------
ACCESS = ("anyone", "signed", "permission")
ACCESS_LABELS = {"anyone": "open to anyone with the link", "signed": "for visitors who sign in",
                 "permission": "by permission"}
RULES_MAX = 50
INVITE_RE = re.compile(r"^https://(?:discord\.gg|(?:www\.)?discord(?:app)?\.com/invite)/[A-Za-z0-9-]{2,32}/?$")
NOTHING = {"see": False, "start": False, "stop": False, "map": False}
ANYONE = "anyone"     # 1.28 row: visitors who aren't signed in (see and Start, never Stop)


def clean_invite(value):
    url = str(value or "").strip()
    if not url:
        return ""
    if not INVITE_RE.match(url):
        raise JoinError("The Discord invite must be a link like https://discord.gg/abc123 (Discord: Invite People, "
                        "then Edit invite link and set it to never expire).")
    return url.rstrip("/")


def _clean_rule(r):
    from core import auth, discordbot
    if not isinstance(r, dict):
        raise JoinError("Each permission row needs who it's for.")
    who = str(r.get("who") or "").strip()
    if who.startswith("panel:"):
        if not auth.USERNAME_RE.match(who[6:]):
            raise JoinError(f"'{who[6:][:40]}' isn't a panel account name.")
    elif who.startswith("discord_role:"):
        if not discordbot.ID_RE.match(who[13:]):
            raise JoinError("Choose a Discord role for each Discord row.")
    elif who not in ("signed", "panel_perms", "discord_member", ANYONE):
        raise JoinError("Unknown kind of permission row.")
    row = {"who": who, "see": bool(r.get("see")), "start": bool(r.get("start")), "stop": bool(r.get("stop")),
           "map": bool(r.get("map")), "name": " ".join(str(r.get("name") or "").split())[:100]}
    if who == ANYONE:
        row["stop"] = False                  # stopping always needs a sign-in
        row["see"] = row["see"] or row["start"]
    return row


def _clean_access(cfg, new):
    from core import discordauth
    access = str(new.get("access") or "anyone")
    if access not in ACCESS:
        raise JoinError("Choose who can see the page.")
    cfg["access"] = access
    cfg["signin_panel"] = bool(new.get("signin_panel"))
    cfg["signin_discord"] = bool(new.get("signin_discord"))
    cfg["discord_member"] = bool(new.get("discord_member")) and cfg["signin_discord"]
    cfg["discord_invite"] = clean_invite(new.get("discord_invite"))
    rules = new.get("rules") or []
    if not isinstance(rules, (list, tuple)) or len(rules) > RULES_MAX:
        raise JoinError(f"Use at most {RULES_MAX} permission rows.")
    rows, seen = [], set()
    for r in rules:
        row = _clean_rule(r)
        if row["who"] in seen:
            raise JoinError("Each account or role can have one row.")
        seen.add(row["who"])
        rows.append(row)
    cfg["rules"] = rows
    if access == "anyone":
        return
    if not cfg["signin_panel"] and not cfg["signin_discord"]:
        raise JoinError("Choose how visitors sign in: a panel account, Discord, or both.")
    if cfg["signin_discord"]:
        dcfg = discordauth.load()
        if not discordauth.configured(dcfg):
            raise JoinError("Set up Sign in with Discord first (Panel Settings, Discord).")
        if not discordauth.join_base(dcfg):
            raise JoinError("Enter the join pages address under Panel Settings, Discord, so Discord can send "
                            "visitors back to the page.")
        needs_guild = cfg["discord_member"] or any(r["who"].startswith("discord_") for r in rows)
        if needs_guild and not dcfg["guild"]:
            raise JoinError("Choose your Discord server under Panel Settings, Discord, to check who's a member.")
    if access == "permission" and not any(r["see"] or r["start"] or r["stop"] or r["map"] for r in rows):
        raise JoinError("Add at least one row that gives something, or choose another option.")


def _row_matches(who, visitor, key):
    if who == ANYONE:
        return NOTHING_ALL                   # whoever may open the page without signing in
    if visitor["kind"] == "panel":
        if who == "signed":
            return NOTHING_ALL
        if who == "panel:" + visitor["name"]:
            return NOTHING_ALL
        if who == "panel_perms":
            from core import grants
            perms = grants.perms_for({"role": visitor["role"], "source": "panel", "username": visitor["name"]}, key)
            return {"see": "view" in perms, "start": "start" in perms, "stop": "power" in perms, "map": "view" in perms}
        return None
    if who == "signed":
        return NOTHING_ALL
    if not visitor.get("member"):
        return None
    if who == "discord_member" or (who.startswith("discord_role:") and who[13:] in (visitor.get("roles") or ())):
        return NOTHING_ALL
    return None


NOTHING_ALL = {"see": True, "start": True, "stop": True, "map": True}       # the row's own ticks apply


def _anonymous(cfg):
    """What the "Visitors who aren't signed in" row gives on a page by permission."""
    if cfg.get("access") != "permission":
        return dict(NOTHING)
    out = dict(NOTHING)
    for row in cfg.get("rules") or []:
        if row["who"] == ANYONE:
            out = {"see": row["see"] or row["start"], "start": row["start"], "stop": False,
                   "map": bool(row.get("map"))}
    return out


def gate(cfg, visitor):
    """Why a visitor (or nobody) can't use a page at all, or None. "signin", "member" (signed in
    with Discord but not in the Discord server) or "nothing". A page whose "Visitors who aren't
    signed in" row lets them see it is open to everyone: signing in only adds to that."""
    why = _gate(cfg, visitor)
    return None if why and _anonymous(cfg)["see"] else why


def _gate(cfg, visitor):
    if cfg.get("access", "anyone") == "anyone":
        return None
    if not visitor:
        return "signin"
    if visitor["kind"] == "panel" and not cfg["signin_panel"]:
        return "signin"
    if visitor["kind"] == "discord" and not cfg["signin_discord"]:
        return "signin"
    if visitor["kind"] == "discord" and cfg["discord_member"] and not visitor.get("member"):
        return "member"
    return None


def abilities(cfg, key, visitor):
    """{"see", "start", "stop", "map"} for this visitor (None = not signed in) on the page of
    `key`. "map" (1.30) is never more than the page's own Show the map switch."""
    out = _abilities(cfg, key, visitor)
    out["map"] = bool(out.get("map") and cfg.get("show_map"))
    return out


def _abilities(cfg, key, visitor):
    access = cfg.get("access", "anyone")
    if access == "anyone":
        return {"see": True, "start": bool(cfg.get("allow_start")), "stop": False, "map": True}
    if _gate(cfg, visitor):
        return _anonymous(cfg)               # not signed in (or not in a way this page takes)
    if access == "signed":
        return {"see": True, "start": bool(cfg.get("allow_start")), "stop": False, "map": True}
    if visitor["kind"] == "panel" and visitor.get("role") == "admin":
        return {"see": True, "start": True, "stop": True, "map": True}
    out = dict(NOTHING)
    for row in cfg.get("rules") or []:
        got = _row_matches(row["who"], visitor, key)
        if got:
            for k in out:
                out[k] = out[k] or bool(got[k] and row.get(k))
    return out


def visitor_label(visitor):
    if not visitor:
        return "a visitor"
    if visitor["kind"] == "discord":
        return f"discord:{visitor['name']} ({visitor['id']})"
    return visitor["name"]


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


def public_view(cfg, s, public_address=None, key=None, can=None):
    """The page's whole content, from the fixed list of fields that may leave the machine.
    can (1.28): what this visitor may do, from abilities(); left out = an open page's."""
    can = can or abilities(cfg, key, None)
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
        "can_start": bool(can["start"]) and s.get("status") != "online",
        "can_stop": bool(can["stop"]) and s.get("status") == "online",
        "can_see": bool(can["see"]),
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
    return {"status": view["status"], "players": view["players"] if view.get("can_see", True) else None,
            "minecraft": view["minecraft"], "can_start": view["can_start"], "can_stop": view.get("can_stop", False)}


# ---------------------------------------------------------------------------
# 1.26: starting a stopped (or sleeping) server from its page, when the admin allows it
# ---------------------------------------------------------------------------
START_COOLDOWN = 120            # seconds: one start per server in this time, however many visitors press
START_SHOWN = 240               # seconds the page says "Starting" after a start, until the server answers
START_PER_IP = 3                # starts one address may ask for in an hour, over all servers
START_PER_VISITOR = 5           # 1.28: starts one signed-in visitor may ask for in an hour (like Discord /start)
STOP_COOLDOWN = 120             # 1.28: one stop per server from its page in this time
STOP_SHOWN = 90                 # seconds the page says "Stopping" after a stop, until the server is down
_starts = {}                    # key -> time of the last start asked from a page
_starts_by_ip = {}              # ip (or "v:<visitor>") -> [times]
_stops = {}                     # key -> time of the last stop asked from a page


def _public_state(key, s):
    """online, stopping, starting, sleeping or offline: what a visitor may know."""
    if s.get("status") == "online":
        if key and time.time() - _stops.get(key, 0) < STOP_SHOWN:
            return "stopping"
        return "online"
    if key and time.time() - _starts.get(key, 0) < START_SHOWN:
        return "starting"
    return "sleeping" if s.get("sleeping") else "offline"


def start_from_page(key, ip, now=None, visitor=None):
    """A visitor pressed Start on the page of `key`. Returns "online", "starting" or raises JoinError.
    The admin must have allowed it on that page (1.26), or the visitor's permission must give it
    (1.28). A start already asked in the last START_COOLDOWN seconds is not repeated; an address
    gets START_PER_IP starts an hour, a signed-in visitor START_PER_VISITOR."""
    now = time.time() if now is None else now
    cfg = page(key)
    if not cfg["enabled"] or not abilities(cfg, key, visitor)["start"]:
        raise JoinError("This server can't be started from its page.")
    s = summary(key, fresh=True)
    if s.get("status") == "online":
        return "online"
    who_key = f"v:{visitor['kind']}:{visitor.get('id') or visitor['name']}" if visitor else ip
    limit = START_PER_VISITOR if visitor else START_PER_IP
    with _lock:
        if now - _starts.get(key, 0) < START_COOLDOWN:
            return "starting"
        times = [t for t in _starts_by_ip.get(who_key, []) if now - t < 3600]
        if len(times) >= limit:
            raise JoinError("You've started servers here a few times in the last hour. Try again later.")
        if len(_starts_by_ip) > 5000:
            _starts_by_ip.clear()
        _starts_by_ip[who_key] = times + [now]
        _starts[key] = now
        _stops.pop(key, None)
    member, server = split_key(key)
    who = f"{visitor_label(visitor)} (from {ip})" if visitor else f"A visitor from {ip}"
    manager.write_log(f"[JOIN] {who} started {server}"
                      + ("" if member == "local" else f" on cluster member {member}") + " from its join page.")
    threading.Thread(target=_start_server, args=(member, server, ip), name=f"join-start-{server}", daemon=True).start()
    return "starting"


def stop_from_page(key, ip, visitor, now=None):
    """1.28: a signed-in visitor whose permission gives Stop pressed it. Returns "offline",
    "stopping" or raises JoinError. A clean stop, like the panel's Stop (sleep mode then counts it
    as stopped by hand); never while a backup, a move or a type change runs."""
    now = time.time() if now is None else now
    cfg = page(key)
    if not cfg["enabled"] or not visitor or not abilities(cfg, key, visitor)["stop"]:
        raise JoinError("This server can't be stopped from its page.")
    s = summary(key, fresh=True)
    if s.get("status") != "online":
        return "offline"
    member, server = split_key(key)
    if member == "local":
        busy = _busy(server)
        if busy:
            raise JoinError(busy)
    with _lock:
        if now - _stops.get(key, 0) < STOP_COOLDOWN:
            return "stopping"
        _stops[key] = now
        _starts.pop(key, None)
    manager.write_log(f"[JOIN] {visitor_label(visitor)} (from {ip}) stopped {server}"
                      + ("" if member == "local" else f" on cluster member {member}") + " from its join page.")
    threading.Thread(target=stop_server, args=(member, server, visitor_label(visitor)), name=f"join-stop-{server}",
                     daemon=True).start()
    return "stopping"


def _busy(server):
    """Why a server can't be stopped from its page right now, or None."""
    from core import backups, sleep
    why = sleep._busy(server)
    if why:
        return why
    try:
        fd = backups._lock(server)
    except Exception:
        return "A backup of the server is running. Try again in a few minutes."
    os.close(fd)
    return None


def stop_server(member, server, who):
    """Stops a server here (holding its backup lock, so no backup starts meanwhile) or on a
    cluster member. A failure is logged, never raised."""
    try:
        if member == "local":
            from core import backups
            fd = backups._lock(server)
            try:
                manager.control_server(server, "stop")
            finally:
                os.close(fd)
        else:
            from core import cluster, cluster_client
            node = cluster.find_node(member)
            if not node:
                raise JoinError("That cluster member is gone.")
            cluster_client.call_node(node, "POST", f"/api/servers/{quote(server, safe='')}/action", {"action": "stop"},
                                     actor="join page", timeout=cluster_client.ACTION_TIMEOUT)
            cluster_client.forget_cached(member)
        return True
    except Exception as e:
        manager.write_log(f"[JOIN] Stopping {server} for {who} from its join page failed: {e}")
        return False
    finally:
        _summary_cache.pop(page_key(member, server), None)


def _start_server(member, server, ip):
    start_server(member, server, f"a visitor of its join page ({ip})", actor="join page", tag="JOIN",
                 where="from its join page")


def start_server(member, server, who, actor, tag, where):
    """Starts (or wakes) a server on this panel or on a cluster member. Used by the join page's
    Start button (1.26) and Discord's /start (1.27). Runs in the caller's thread; a failure is
    logged under `tag`, never raised. True when the start was asked for."""
    try:
        if member == "local":
            from core import sleep
            if not sleep.wake(server, who):
                manager.control_server(server, "start")
        else:
            from core import cluster, cluster_client
            node = cluster.find_node(member)
            if not node:
                raise JoinError("That cluster member is gone.")
            cluster_client.call_node(node, "POST", f"/api/servers/{quote(server, safe='')}/action", {"action": "start"},
                                     actor=actor, timeout=cluster_client.ACTION_TIMEOUT)
            cluster_client.forget_cached(member)
        return True
    except Exception as e:
        manager.write_log(f"[{tag}] Starting {server} {where} failed: {e}")
        return False
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
