"""Discord /start (1.27): people in your Discord start a Minecraft server with a slash command.

The panel itself is the bot. An admin pastes a Discord bot token under Panel Settings ->
Discord, picks the servers /start may start, the Discord servers (guilds) it answers in and,
if they like, the Discord roles that may use it. The panel then keeps one connection open to
Discord's gateway (an outgoing websocket, so nothing has to be forwarded or published and it
works behind a home router), registers the /start command, and answers it:

    /start server:<name>   starts the server, or wakes it from sleep mode (1.26), and edits its
                           answer to "online" when the server answers (up to 5 minutes)

It only ever starts servers. Stopping is left to sleep mode, the panel and the API. Like an API
token (1.25), it may touch only the servers ticked for it (or every server, "*", including
later ones) and everything it does is logged, as `discord:<Discord user name>`. A server is
started at most once every START_COOLDOWN seconds from Discord, and each Discord user may ask
START_PER_USER times an hour.

Settings live in DATA_DIR/discord.json (0600): {"enabled", "token", "servers": [keys], "guilds":
[ids], "roles": [ids], "updated", "updated_by"}. The bot token is never sent back to the browser;
the settings show its last four characters.

Everything here is the standard library: a small websocket client (RFC 6455, client side
only), Discord's gateway (v10, JSON, heartbeat, resume) and its REST API through urllib. No
system programs are run, so Linux and Windows are the same code.
"""
import base64
import hashlib
import json
import os
import random
import re
import select
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from core import grants as grants_mod
from core.settings import DATA_DIR

DISCORD_FILE = os.path.join(DATA_DIR, "discord.json")
API = "https://discord.com/api/v10"
GATEWAY_QUERY = "?v=10&encoding=json"
INTENTS = 1                         # GUILDS: the names and roles of the Discord servers the bot is in
INTENT_MEMBERS = 2                  # GUILD_MEMBERS (privileged, 1.28): role changes, joins and leaves
INTENT_INVITES = 64                 # GUILD_INVITES (1.28): which invite a new member used
WL_PERMISSIONS = 1024 + 2048 + 32 + 268435456   # view and send in channels, Manage Server (invites), Manage Roles
TOKEN_RE = re.compile(r"^[A-Za-z0-9_.\-]{50,100}$")
ID_RE = re.compile(r"^[0-9]{15,22}$")
START_COOLDOWN = 120                # seconds: one start per server from Discord in this time
START_PER_USER = 5                  # starts one Discord user may ask for in an hour
READY_WAIT = 300                    # seconds the answer waits for the server to come online
READY_POLL = 5
SERVERS_TTL = 30
MAX_MESSAGE = 8 * 1024 * 1024
BACKOFF = (1, 60)                   # seconds between reconnects: first, longest
INVALID_SESSION_WAIT = 1            # Discord asks for 1 to 5 seconds before identifying again
FATAL_CLOSE = {4004: "Discord refused the bot token. Copy a new one from the Developer Portal (Bot, Reset Token).",
               4010: "Discord refused the connection (invalid shard).",
               4011: "This bot is in too many Discord servers to connect without sharding.",
               4012: "Discord refused the gateway version.",
               4013: "Discord refused the gateway intents.",
               4014: "Discord refused the Server Members Intent. Turn it on in the Developer Portal (Bot, "
                     "Privileged Gateway Intents), or untick it under Whitelist by Discord."}
FRESH_CLOSE = {4007, 4009}          # the session can't be resumed: identify again
COMMANDS = [{
    "name": "start", "type": 1,
    "description": "Start a Minecraft server (or wake it up)",
    "options": [{"type": 3, "name": "server", "description": "Which server", "required": False,
                 "autocomplete": True}],
    "contexts": [0], "integration_types": [0],
}]
MINECRAFT_COMMAND = {
    "name": "minecraft", "type": 1,
    "description": "Link your Minecraft name, to be let on this community's servers",
    "options": [{"type": 3, "name": "java", "description": "Your Java Edition name", "required": False,
                 "min_length": 3, "max_length": 16},
                {"type": 3, "name": "bedrock", "description": "Your Bedrock (Xbox) gamertag", "required": False,
                 "min_length": 1, "max_length": 16}],
    "contexts": [0], "integration_types": [0],
}
EPHEMERAL = 64

_lock = threading.RLock()
_bot = {"thread": None, "stop": None, "ws": None}
_status = {"state": "off", "error": None, "user": None, "app_id": None, "guilds": {}, "since": None, "warning": None}
_members_refused = threading.Event()  # 1.28: Discord refused the members intent; run without it until the next save
_starts = {}                        # server key -> time of the last /start
_starts_by_user = {}                # Discord user id -> [times]
_servers_cache = {"at": 0, "list": []}


class DiscordError(ValueError):
    """A Discord settings change that can't be done (message is safe to show)."""


class Fatal(Exception):
    """Reconnecting won't help (a refused token): the bot stops and says why."""


class Reconnect(Exception):
    """Connect again, resuming the session when Discord allows it."""


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def _defaults():
    return {"enabled": False, "token": "", "servers": [grants_mod.ALL_SERVERS], "guilds": [], "roles": [],
            "updated": None, "updated_by": None}


def load():
    out = _defaults()
    try:
        with open(DISCORD_FILE) as f:
            data = json.load(f)
        if isinstance(data, dict):
            out.update({k: data[k] for k in out if k in data})
    except (OSError, ValueError):
        pass
    out["enabled"] = bool(out["enabled"])
    out["token"] = out["token"] if isinstance(out["token"], str) else ""
    for k in ("servers", "guilds", "roles"):
        out[k] = [str(x) for x in out[k]] if isinstance(out[k], list) else []
    return out


def _save(cfg):
    os.makedirs(os.path.dirname(DISCORD_FILE), exist_ok=True)
    tmp = DISCORD_FILE + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(cfg, f, indent=1)
    os.replace(tmp, DISCORD_FILE)


def _ids(values, what):
    if not isinstance(values, (list, tuple)):
        raise DiscordError(f"Choose the {what} as a list.")
    out = []
    for v in values:
        v = str(v).strip()
        if not ID_RE.match(v):
            raise DiscordError(f"Not a Discord {what[:-1]} ID: {v[:40]}")
        if v not in out:
            out.append(v)
    return out[:100]


def save_settings(new, actor="?"):
    """Admin's change from Panel Settings. `new`: enabled, token (empty keeps the saved one),
    forget_token, servers, guilds, roles. Restarts the bot. Returns public()."""
    with _lock:
        cfg = load()
        token = str(new.get("token") or "").strip()
        if token.lower().startswith("bot "):
            token = token[4:].strip()
        if new.get("forget_token"):
            cfg["token"], cfg["enabled"] = "", False
        elif token:
            if not TOKEN_RE.match(token):
                raise DiscordError("That doesn't look like a Discord bot token. Copy it from the Developer "
                                   "Portal: your application, Bot, Reset Token.")
            cfg["token"] = token
        if "servers" in new:
            servers = new["servers"]
            if not isinstance(servers, (list, tuple)) or not all(isinstance(k, str) and grants_mod.KEY_RE.match(k)
                                                                 for k in servers):
                raise DiscordError("Choose the servers /start may start.")
            cfg["servers"] = [grants_mod.ALL_SERVERS] if grants_mod.ALL_SERVERS in servers else list(dict.fromkeys(servers))
        if "guilds" in new:
            cfg["guilds"] = _ids(new["guilds"], "servers")
        if "roles" in new:
            cfg["roles"] = _ids(new["roles"], "roles")
        if "enabled" in new and not new.get("forget_token"):
            cfg["enabled"] = bool(new["enabled"])
        if cfg["enabled"] and not cfg["token"]:
            raise DiscordError("Paste the bot token first.")
        if cfg["enabled"] and not cfg["servers"]:
            raise DiscordError("Choose at least one server /start may start.")
        cfg.update(updated=int(time.time()), updated_by=actor)
        _save(cfg)
    _log(f"[DISCORD] {actor} " + ("turned Discord /start on" if cfg["enabled"] else "turned Discord /start off")
         + (f" for {', '.join(cfg['servers'])}" if cfg["enabled"] else "")
         + (" and removed the bot token" if new.get("forget_token") else "") + ".")
    apply()
    return public()


def public(cfg=None):
    """What the settings page shows. Never the token itself."""
    cfg = cfg or load()
    with _lock:
        st = json.loads(json.dumps(_status))
    guilds = [dict(id=gid, **g) for gid, g in sorted(st["guilds"].items(), key=lambda x: x[1].get("name", "").lower())]
    from core import discordwl
    perms = WL_PERMISSIONS if discordwl.load()["enabled"] else 0
    invite = (f"https://discord.com/oauth2/authorize?client_id={st['app_id']}&scope=bot%20applications.commands"
              f"&permissions={perms}") if st["app_id"] else None
    return {"enabled": cfg["enabled"], "has_token": bool(cfg["token"]), "token_hint": cfg["token"][-4:] if cfg["token"] else "",
            "servers": cfg["servers"], "guilds": cfg["guilds"], "roles": cfg["roles"],
            "updated": cfg["updated"], "updated_by": cfg["updated_by"],
            "status": {"state": st["state"], "error": st["error"], "user": st["user"], "app_id": st["app_id"],
                       "since": st["since"], "guilds": guilds, "warning": st.get("warning")},
            "invite": invite}


def forget_server(key):
    """A server deleted for good is no longer one /start may start."""
    with _lock:
        cfg = load()
        if key in cfg["servers"]:
            cfg["servers"] = [k for k in cfg["servers"] if k != key]
            _save(cfg)


def rename_server(old_key, new_key):
    """A server moved to another cluster member stays one /start may start."""
    if old_key == new_key or not grants_mod.KEY_RE.match(new_key):
        return
    with _lock:
        cfg = load()
        if old_key in cfg["servers"]:
            cfg["servers"] = list(dict.fromkeys(new_key if k == old_key else k for k in cfg["servers"]))
            _save(cfg)


def _log(msg):
    try:
        from core import manager
        manager.write_log(msg)
    except Exception:
        pass


def _set_status(**kw):
    with _lock:
        _status.update(kw)


# ---------------------------------------------------------------------------
# A websocket client (RFC 6455), just what the gateway needs
# ---------------------------------------------------------------------------
class Closed(Exception):
    def __init__(self, code=None, reason=""):
        super().__init__(f"closed ({code}) {reason}".strip())
        self.code = code
        self.reason = reason


_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _proxy_for(host):
    """An https proxy from the environment (HTTPS_PROXY / NO_PROXY), as (host, port) or None."""
    proxies = urllib.request.getproxies()
    url = proxies.get("https") or proxies.get("wss")
    if not url or urllib.request.proxy_bypass(host):
        return None
    p = urllib.parse.urlsplit(url if "://" in url else "http://" + url)
    return (p.hostname, p.port or 80) if p.hostname else None


class WebSocket:
    def __init__(self, url, timeout=15, ssl_context=None, headers=None):
        u = urllib.parse.urlsplit(url)
        if u.scheme not in ("ws", "wss") or not u.hostname:
            raise ValueError("Not a websocket address.")
        port = u.port or (443 if u.scheme == "wss" else 80)
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        proxy = _proxy_for(u.hostname) if u.scheme == "wss" else None
        sock = socket.create_connection(proxy or (u.hostname, port), timeout=timeout)
        try:
            if proxy:
                sock.sendall(f"CONNECT {u.hostname}:{port} HTTP/1.1\r\nHost: {u.hostname}:{port}\r\n\r\n".encode())
                head, _ = self._read_head(sock, b"")
                if not re.match(rb"HTTP/1\.[01] 200", head):
                    first = head.split(b"\r\n")[0][:80].decode("latin-1")
                    raise OSError(f"The proxy refused the connection: {first}")
            if u.scheme == "wss":
                ctx = ssl_context or ssl.create_default_context()
                sock = ctx.wrap_socket(sock, server_hostname=u.hostname)
            key = base64.b64encode(os.urandom(16)).decode()
            extra = "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items())
            host = u.hostname if u.port in (None, 80, 443) else f"{u.hostname}:{u.port}"
            sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                          f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n{extra}\r\n").encode())
            head, rest = self._read_head(sock, b"")
            lines = head.decode("latin-1").split("\r\n")
            if not re.match(r"HTTP/1\.1 101", lines[0]):
                raise OSError(f"The websocket was refused: {lines[0][:80]}")
            fields = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:])}
            want = base64.b64encode(hashlib.sha1((key + _WS_GUID).encode()).digest()).decode()
            if fields.get("sec-websocket-accept") != want:
                raise OSError("The websocket answer didn't match.")
        except Exception:
            sock.close()
            raise
        sock.settimeout(timeout)
        self.sock = sock
        self.buf = rest
        self.timeout = timeout
        self._send_lock = threading.Lock()
        self.closed = False

    @staticmethod
    def _read_head(sock, buf):
        while b"\r\n\r\n" not in buf:
            if len(buf) > 16384:
                raise OSError("The answer's headers are too long.")
            got = sock.recv(4096)
            if not got:
                raise OSError("The connection closed during the handshake.")
            buf += got
        head, _, rest = buf.partition(b"\r\n\r\n")
        return head, rest

    def _exact(self, n):
        while len(self.buf) < n:
            got = self.sock.recv(max(65536, n - len(self.buf)))
            if not got:
                raise Closed(1006, "connection lost")
            self.buf += got
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def readable(self, timeout):
        """True when a frame can be read now (or within `timeout` seconds)."""
        if self.buf or (isinstance(self.sock, ssl.SSLSocket) and self.sock.pending()):
            return True
        r, _, _ = select.select([self.sock], [], [], max(0.0, timeout))
        return bool(r)

    def _frame(self):
        b1, b2 = self._exact(2)
        n = b2 & 0x7F
        if n == 126:
            n = int.from_bytes(self._exact(2), "big")
        elif n == 127:
            n = int.from_bytes(self._exact(8), "big")
        if n > MAX_MESSAGE:
            raise Closed(1009, "message too big")
        mask = self._exact(4) if b2 & 0x80 else None
        data = self._exact(n)
        if mask:
            data = _xor(data, mask)
        return bool(b1 & 0x80), b1 & 0x0F, data

    def recv(self):
        """The next text or binary message (str or bytes). Answers pings; raises Closed."""
        parts, kind = [], None
        while True:
            fin, op, data = self._frame()
            if op == 0x9:
                self._send(0xA, data)
                continue
            if op == 0xA:
                continue
            if op == 0x8:
                code = int.from_bytes(data[:2], "big") if len(data) >= 2 else 1005
                try:
                    self._send(0x8, data[:2])
                except OSError:
                    pass
                self._shut()
                raise Closed(code, data[2:].decode("utf-8", "replace"))
            if op in (0x1, 0x2):
                kind, parts = op, [data]
            elif op == 0x0 and kind:
                parts.append(data)
            else:
                raise Closed(1002, "unexpected frame")
            if sum(map(len, parts)) > MAX_MESSAGE:
                raise Closed(1009, "message too big")
            if fin:
                whole = b"".join(parts)
                return whole.decode("utf-8") if kind == 0x1 else whole

    def _send(self, op, data):
        n = len(data)
        head = bytes([0x80 | op])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + n.to_bytes(2, "big")
        else:
            head += bytes([0x80 | 127]) + n.to_bytes(8, "big")
        mask = os.urandom(4)
        with self._send_lock:
            self.sock.sendall(head + mask + _xor(data, mask))

    def send(self, text):
        self._send(0x1, text.encode("utf-8") if isinstance(text, str) else text)

    def _shut(self):
        self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass

    def close(self, code=1000):
        if self.closed:
            return
        try:
            self._send(0x8, code.to_bytes(2, "big"))
        except OSError:
            pass
        self._shut()


def _xor(data, mask):
    if not data:
        return b""
    n = len(data)
    key = (mask * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(key, "big")).to_bytes(n, "big")


# ---------------------------------------------------------------------------
# Discord's REST API
# ---------------------------------------------------------------------------
def _user_agent():
    try:
        from core import version
        v = version.panel_version() or "1"
    except Exception:
        v = "1"
    return f"DiscordBot (https://github.com/qrussell/redstonepanel, {v})"


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def api(method, path, body=None, token=None, timeout=10, _retry=True):
    """One Discord REST call -> parsed JSON (or None). Raises ApiError. Waits once on a 429."""
    headers = {"User-Agent": _user_agent()}
    if token:
        headers["Authorization"] = f"Bot {token}"
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(API + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(MAX_MESSAGE)
        return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read(65536)
        try:
            got = json.loads(raw)
        except ValueError:
            got = {}
        if e.code == 429 and _retry:
            time.sleep(min(10.0, float(got.get("retry_after") or 1)))
            return api(method, path, body, token, timeout, _retry=False)
        raise ApiError(e.code, str(got.get("message") or e.reason or e.code)[:200])
    except (OSError, ValueError) as e:
        raise ApiError(0, f"Discord can't be reached ({e}).")


# ---------------------------------------------------------------------------
# The servers /start may start
# ---------------------------------------------------------------------------
def _all_servers(fresh=False):
    """[{key, member, name, edition, where}] of every server on this panel and, on a master, its
    nodes. Reused for SERVERS_TTL seconds (autocomplete has to answer within 3 seconds)."""
    now = time.time()
    if not fresh and now - _servers_cache["at"] < SERVERS_TTL:
        return _servers_cache["list"]
    from core import cluster, manager
    out = []
    if cluster.role() == "master":
        from core import cluster_client
        for n in cluster_client.overview(manager.list_servers)["nodes"]:
            for s in n.get("servers") or []:
                if isinstance(s, dict) and s.get("name"):
                    member = "local" if n.get("local") else n["id"]
                    out.append({"key": grants_mod.server_key(s["name"], None if member == "local" else member),
                                "member": member, "name": s["name"], "edition": s.get("edition") or "",
                                "where": "" if member == "local" else n.get("name") or member})
    else:
        for s in manager.list_servers():
            if s.get("name"):
                out.append({"key": s["name"], "member": "local", "name": s["name"], "edition": s.get("edition") or "",
                            "where": ""})
    _servers_cache.update(at=now, list=out)
    return out


def allowed_servers(cfg=None, fresh=False):
    cfg = cfg or load()
    keys = set(cfg["servers"])
    return [s for s in _all_servers(fresh) if grants_mod.ALL_SERVERS in keys or s["key"] in keys]


def _label(s):
    text = s["name"] + (f" on {s['where']}" if s["where"] else "") + (f" ({s['edition']})" if s["edition"] else "")
    return text[:100]


def _find(value, servers):
    value = (value or "").strip()
    hit = [s for s in servers if s["key"] == value]
    if not hit:
        low = value.lower()
        hit = [s for s in servers if s["name"].lower() == low] or \
              [s for s in servers if s["name"].lower() == "minecraft-" + low]
    return hit[0] if len(hit) == 1 else None


# ---------------------------------------------------------------------------
# Answering /start
# ---------------------------------------------------------------------------
def _reply(content, ephemeral=False):
    data = {"content": content[:1900], "allowed_mentions": {"parse": []}}
    if ephemeral:
        data["flags"] = EPHEMERAL
    return {"type": 4, "data": data}


def _who(d):
    user = (d.get("member") or {}).get("user") or d.get("user") or {}
    name = str(user.get("global_name") or user.get("username") or "someone")
    name = re.sub(r"[^\w .\-]", "", name)[:40] or "someone"
    return str(user.get("id") or ""), name


def _address(key, s):
    from core import joinpage
    try:
        a = joinpage.addresses(joinpage.page(key), s)
    except Exception:
        return ""
    if a.get("java"):
        return f"`{a['java']}`"
    if a.get("bedrock"):
        return f"`{a['bedrock']['host']}` port `{a['bedrock']['port']}`"
    return ""


def _ready(s):
    """True when players can join: online and answering its ping (NetherNet has no ping)."""
    if s.get("status") != "online":
        return False
    return s.get("players") is not None or (s.get("edition") == "Bedrock" and s.get("transport") == "nethernet") \
        or not s.get("port")


def _discord_admin(d):
    """The Discord server's owner and anyone with Administrator there (Discord sends the member's
    resolved permissions with each interaction) may use /start without a listed role (1.28)."""
    try:
        return bool(int((d.get("member") or {}).get("permissions") or 0) & 8)
    except (TypeError, ValueError):
        return False


def answer(d, cfg=None, now=None):
    """An INTERACTION_CREATE -> (the response to send, a function to run afterwards or None)."""
    cfg = cfg or load()
    now = time.time() if now is None else now
    data = d.get("data") or {}
    if d.get("type") in (3, 5) or (d.get("type") == 2 and data.get("name") == "minecraft"):
        from core import discordbot_wl
        return discordbot_wl.answer(d, now)
    if data.get("name") != "start" or d.get("type") not in (2, 4):
        return _reply("This panel only knows /start and /minecraft.", True), None
    guild = str(d.get("guild_id") or "")
    if not guild:
        return _reply("Use /start in a Discord server, not in a direct message.", True), None
    if guild not in cfg["guilds"]:
        if d.get("type") == 4:
            return {"type": 8, "data": {"choices": []}}, None
        return _reply("This Discord server isn't allowed to start servers on this panel yet. An admin can allow it "
                      "under Panel Settings, Discord.", True), None
    roles = set((d.get("member") or {}).get("roles") or ())
    if cfg["roles"] and not roles & set(cfg["roles"]) and not _discord_admin(d):
        if d.get("type") == 4:
            return {"type": 8, "data": {"choices": []}}, None
        return _reply("You don't have a role that may start servers here.", True), None
    opts = {o.get("name"): o for o in data.get("options") or [] if isinstance(o, dict)}
    typed = str((opts.get("server") or {}).get("value") or "")
    try:
        servers = allowed_servers(cfg)
    except Exception:
        servers = []
    if d.get("type") == 4:
        low = typed.lower()
        hits = [s for s in servers if low in s["name"].lower() or low in s["where"].lower()][:25]
        return {"type": 8, "data": {"choices": [{"name": _label(s), "value": s["key"]} for s in hits]}}, None
    if not servers:
        return _reply("There's no server /start may start.", True), None
    if not typed and len(servers) == 1:
        target = servers[0]
    else:
        target = _find(typed, servers)
    if not target:
        names = ", ".join(_label(s) for s in servers[:10]) + (" and more" if len(servers) > 10 else "")
        return _reply(("Which server? " if not typed else f"There's no server called {typed[:60]} that /start may "
                       "start. ") + f"Pick one of: {names}.", True), None
    return _start(d, cfg, target, now)


def _start(d, cfg, target, now):
    from core import joinpage
    key, label = target["key"], _label(target)
    user_id, user = _who(d)
    try:
        s = joinpage.summary(key, fresh=True)
    except Exception as e:
        return _reply(f"{label} can't be read right now ({str(e)[:120]}).", True), None
    if s.get("status") == "online":
        where = _address(key, s)
        return _reply(f"**{label}** is already online." + (f" Join at {where}." if where else "")), None
    with _lock:
        if now - _starts.get(key, 0) < START_COOLDOWN:
            return _reply(f"**{label}** is already starting. It's usually ready in about a minute."), None
        times = [t for t in _starts_by_user.get(user_id, []) if now - t < 3600]
        if len(times) >= START_PER_USER:
            return _reply("You've started servers a few times in the last hour. Try again later.", True), None
        if len(_starts_by_user) > 5000:
            _starts_by_user.clear()
        _starts_by_user[user_id] = times + [now]
        _starts[key] = now
    guild = (_status["guilds"].get(str(d.get("guild_id"))) or {}).get("name") or str(d.get("guild_id"))
    asleep = bool(s.get("sleeping"))
    _log(f"[DISCORD] discord:{user} ({user_id}) " + ("woke" if asleep else "started") + f" {target['name']}"
         + ("" if target["member"] == "local" else f" on cluster member {target['where']}") + f" from Discord ({guild}).")
    token = d.get("token")

    def after():
        ok = joinpage.start_server(target["member"], target["name"], f"discord:{user}", actor=f"discord:{user}",
                                   tag="DISCORD", where="from Discord")
        if not ok:
            _edit(token, f"**{label}** couldn't be started. An admin can see why in the panel's log.")
            return
        _wait_ready(token, key, label)

    verb = "Waking" if asleep else "Starting"
    return _reply(f"{verb} **{label}** for {user}. It's usually ready in about a minute; this message says when."), after


def _edit(itoken, content):
    app_id = _status.get("app_id")
    if not itoken or not app_id:
        return
    try:
        api("PATCH", f"/webhooks/{app_id}/{itoken}/messages/@original",
            {"content": content[:1900], "allowed_mentions": {"parse": []}})
    except ApiError as e:
        _log(f"[DISCORD] Couldn't update the /start answer: {e}")


def _wait_ready(itoken, key, label, wait=READY_WAIT, poll=READY_POLL):
    from core import joinpage
    end = time.time() + wait
    while time.time() < end:
        time.sleep(poll)
        try:
            s = joinpage.summary(key, fresh=True)
        except Exception:
            continue
        if _ready(s):
            where = _address(key, s)
            _edit(itoken, f"**{label}** is online." + (f" Join at {where}." if where else ""))
            return True
    _edit(itoken, f"**{label}** hasn't come online after {wait // 60} minutes. An admin can check it in the panel.")
    return False


def _handle_interaction(d, token):
    try:
        response, after = answer(d)
    except Exception as e:                       # noqa: BLE001  (always answer something)
        response, after = _reply(f"Something went wrong: {str(e)[:200]}", True), None
    try:
        api("POST", f"/interactions/{d.get('id')}/{d.get('token')}/callback", response, token=token)
    except ApiError as e:
        _log(f"[DISCORD] Couldn't answer /start: {e}")
        return
    if after:
        after()


# ---------------------------------------------------------------------------
# The gateway
# ---------------------------------------------------------------------------
class Gateway:
    """One bot connection: connects, identifies or resumes, heartbeats, and dispatches. run()
    returns when stop is set; raises Fatal when the token is refused."""

    def __init__(self, token, stop, url=None, connect=WebSocket, on_interaction=None):
        self.token = token
        self.stop = stop
        self.url = url
        self.connect = connect
        self.on_interaction = on_interaction or (lambda d: threading.Thread(
            target=_handle_interaction, args=(d, token), name="discord-start", daemon=True).start())
        self.session_id = None
        self.resume_url = None
        self.seq = None
        self.ws = None
        self.acked = True

    def gateway_url(self):
        if self.url:
            return self.url
        try:
            got = api("GET", "/gateway/bot", token=self.token)
        except ApiError as e:
            if e.status == 401:
                raise Fatal(FATAL_CLOSE[4004])
            raise Reconnect(str(e))
        self.url = (got or {}).get("url") or "wss://gateway.discord.gg"
        return self.url

    def run(self):
        backoff = BACKOFF[0]
        while not self.stop.is_set():
            try:
                self.session()
                backoff = BACKOFF[0]
            except Fatal:
                raise
            except (Reconnect, Closed, OSError, ValueError) as e:
                if self.stop.is_set():
                    break
                _set_status(state="connecting", error=f"Reconnecting to Discord: {str(e).rstrip('.')}.")
                if isinstance(e, Closed) and e.code in FRESH_CLOSE:
                    self.session_id = self.seq = None
                self.stop.wait(backoff * (1 + random.random()))
                backoff = min(BACKOFF[1], backoff * 2)

    def _send(self, payload):
        self.ws.send(json.dumps(payload))

    def session(self):
        url = (self.resume_url if self.session_id and self.resume_url else self.gateway_url()) + GATEWAY_QUERY
        self.ws = ws = self.connect(url)
        with _lock:
            _bot["ws"] = ws
        try:
            hello = json.loads(ws.recv())
            if hello.get("op") != 10:
                raise Reconnect("no hello")
            interval = float(hello["d"]["heartbeat_interval"]) / 1000.0
            if self.session_id and self.seq is not None:
                self._send({"op": 6, "d": {"token": self.token, "session_id": self.session_id, "seq": self.seq}})
            else:
                self._send({"op": 2, "d": {"token": self.token, "intents": intents(),
                                           "properties": {"os": os.name, "browser": "redstonepanel",
                                                          "device": "redstonepanel"}}})
            next_beat = time.monotonic() + interval * random.random()
            self.acked = True
            while not self.stop.is_set():
                if ws.readable(min(1.0, next_beat - time.monotonic())):
                    if self.message(json.loads(ws.recv())) == "beat":
                        self._send({"op": 1, "d": self.seq})     # Discord asked: answer now, ack or not
                        continue
                if time.monotonic() >= next_beat:
                    if not self.acked:
                        raise Reconnect("Discord stopped answering the heartbeat")
                    self._send({"op": 1, "d": self.seq})
                    self.acked = False
                    next_beat = time.monotonic() + interval
        except Closed as e:
            if e.code == 4014 and intents() & INTENT_MEMBERS:
                # Ticked in the panel but not turned on in the Developer Portal: keep /start and the
                # whitelist running without it (roles are then re-read every few minutes)
                _members_refused.set()
                _set_status(warning=FATAL_CLOSE[4014] + " Running without it for now.")
                _log("[DISCORD] Discord refused the Server Members Intent; the bot runs without it. Turn it on "
                     "in the Developer Portal (Bot, Privileged Gateway Intents), then save the Discord settings.")
                self.session_id = self.seq = None
                raise Reconnect("the Server Members Intent isn't turned on in the Developer Portal")
            if e.code in FATAL_CLOSE:
                raise Fatal(FATAL_CLOSE[e.code])
            raise
        finally:
            ws.close(1000 if self.stop.is_set() else 4000)

    def message(self, m):
        op = m.get("op")
        if m.get("s") is not None:
            self.seq = m["s"]
        if op == 11:
            self.acked = True
        elif op == 1:
            return "beat"
        elif op == 7:
            raise Reconnect("Discord asked to reconnect")
        elif op == 9:
            if not m.get("d"):
                self.session_id = self.seq = None
            self.stop.wait(INVALID_SESSION_WAIT * (1 + 4 * random.random()))
            raise Reconnect("Discord ended the session")
        elif op == 0:
            self.dispatch(m.get("t"), m.get("d") or {})
        return None

    def dispatch(self, t, d):
        if t == "READY":
            self.session_id = d.get("session_id")
            self.resume_url = d.get("resume_gateway_url")
            user = d.get("user") or {}
            app_id = str((d.get("application") or {}).get("id") or user.get("id") or "")
            _set_status(state="online", error=None, user=user.get("username"), app_id=app_id or None,
                        since=int(time.time()), guilds={})
            threading.Thread(target=register_commands, args=(self.token, app_id), name="discord-commands",
                             daemon=True).start()
        elif t == "RESUMED":
            _set_status(state="online", error=None)
        elif t in ("GUILD_CREATE", "GUILD_UPDATE"):
            gid = str(d.get("id") or "")
            if gid and not d.get("unavailable"):
                roles = sorted((r for r in d.get("roles") or [] if str(r.get("id")) != gid and not r.get("managed")),
                               key=lambda r: -int(r.get("position") or 0))
                with _lock:
                    old = _status["guilds"].get(gid) or {}
                    _status["guilds"][gid] = {"name": str(d.get("name") or old.get("name") or gid)[:100],
                                              "roles": [{"id": str(r["id"]), "name": str(r.get("name"))[:100]}
                                                        for r in roles] if "roles" in d else old.get("roles", [])}
        elif t == "GUILD_DELETE":
            if not d.get("unavailable"):
                with _lock:
                    _status["guilds"].pop(str(d.get("id")), None)
        elif t in ("GUILD_ROLE_CREATE", "GUILD_ROLE_UPDATE", "GUILD_ROLE_DELETE"):
            gid, role = str(d.get("guild_id") or ""), d.get("role") or {}
            with _lock:
                g = _status["guilds"].get(gid)
                if g is not None:
                    rid = str(role.get("id") or d.get("role_id") or "")
                    g["roles"] = [r for r in g["roles"] if r["id"] != rid]
                    if t != "GUILD_ROLE_DELETE" and not role.get("managed"):
                        g["roles"].append({"id": rid, "name": str(role.get("name"))[:100]})
        elif t == "INTERACTION_CREATE":
            self.on_interaction(d)
        elif t in ("GUILD_MEMBER_ADD", "GUILD_MEMBER_UPDATE", "GUILD_MEMBER_REMOVE", "INVITE_CREATE", "INVITE_DELETE"):
            from core import discordbot_wl
            threading.Thread(target=discordbot_wl.event, args=(t, d, self.token), name="discord-members",
                             daemon=True).start()
        if t in ("READY", "GUILD_CREATE"):
            from core import discordbot_wl
            threading.Thread(target=discordbot_wl.ready, args=(self.token, str(d.get("id") or "") if t == "GUILD_CREATE"
                             else None), name="discord-invites", daemon=True).start()


def intents():
    """GUILDS, and with Whitelist by Discord the members intent (when the admin turned it on in the
    Developer Portal) and invites."""
    from core import discordwl
    wl = discordwl.load()
    if not wl["enabled"]:
        return INTENTS
    return INTENTS | INTENT_INVITES | (INTENT_MEMBERS if wl["members_intent"] and not _members_refused.is_set() else 0)


def commands():
    from core import discordwl
    return COMMANDS + ([MINECRAFT_COMMAND] if discordwl.load()["enabled"] else [])


def register_commands(token, app_id):
    """Puts /start (and /minecraft) in place for the bot (a bulk overwrite: running it again
    changes nothing)."""
    if not app_id:
        return
    try:
        api("PUT", f"/applications/{app_id}/commands", commands(), token=token)
    except ApiError as e:
        _set_status(error=f"The /start command couldn't be registered: {e}")
        _log(f"[DISCORD] The /start command couldn't be registered: {e}")


def _run(token, stop):
    def mine():
        return _bot["stop"] is stop
    from core import discordbot_wl
    threading.Thread(target=discordbot_wl.recheck_loop, args=(token, stop), name="discord-recheck",
                     daemon=True).start()
    try:
        Gateway(token, stop).run()
    except Fatal as e:
        if mine():
            _set_status(state="error", error=str(e))
        _log(f"[DISCORD] The bot stopped: {e}")
    except Exception as e:                       # noqa: BLE001
        if mine():
            _set_status(state="error", error=f"The bot stopped: {e}")
        _log(f"[DISCORD] The bot stopped: {e}")


def apply():
    """Starts, restarts or stops the bot to match the settings. Called at panel start and on
    save. An old connection notices its stop within a second and closes itself."""
    cfg = load()
    with _lock:
        if _bot["stop"]:
            _bot["stop"].set()
        _bot.update(thread=None, stop=None, ws=None)
        _members_refused.clear()
        _status["warning"] = None
        if not (cfg["enabled"] and cfg["token"]):
            _status.update(state="off", error=None, user=None, since=None, guilds={})
            return
        stop = threading.Event()
        thread = threading.Thread(target=_run, args=(cfg["token"], stop), name="discord-bot", daemon=True)
        _bot.update(thread=thread, stop=stop)
        _status.update(state="connecting", error=None, guilds={})
    thread.start()


def start():
    """At panel start (run.py)."""
    try:
        apply()
    except Exception as e:                       # noqa: BLE001
        _log(f"[DISCORD] Couldn't start the bot: {e}")
