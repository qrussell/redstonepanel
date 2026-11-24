"""The public side of the join pages (1.21.5): a second, tiny web server inside the panel.

It listens on its own port (Panel Settings > Join pages, 8444 by default) and answers only:

    GET /join                       the list of public pages, if the admin turned the list on
    GET /join/<link>                one server's page
    GET /join/<link>/status.json    the part that refreshes (online, player count)
    GET /join/<link>/icon.png       the server's icon
    GET /join/assets/join.css|js    the page's own stylesheet and script

Everything else is "Not found": there is no sign-in page, no API and none of the panel's files
on this port, so forwarding it on the router shows the internet nothing but these pages. It
sets no cookies, reads no request bodies and takes nothing from the visitor but the path (and,
only from a reverse proxy the admin listed, X-Forwarded-For: see client_ip()).
Pages are built here from joinpage.public_view(), with every value escaped.

It runs as a thread of the panel, not as a service of its own, so there is nothing to install
per operating system; the port is opened in the firewall through the platform layer.
"""
import base64
import html
import ipaddress
import os
import threading
import time
from collections import deque
from urllib.parse import quote

from core import joinpage
from core import manager
from core import settings as panel_settings

ASSET_DIR = os.path.join(panel_settings.APP_DIR, "static", "join")
ASSETS = {"join.css": "text/css; charset=utf-8", "join.js": "text/javascript; charset=utf-8"}
RATE_WINDOW = 60                # seconds
RATE_MAX = 240                  # requests per address in that window (a page load is five)
RATE_TRACKED = 5000
WEBSITE = "https://redstonepanel.cielocloud.org"

SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'self'; script-src 'self'; "
                               "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "geolocation=(), camera=(), microphone=()",
}

_rate = {}
_rate_lock = threading.Lock()
_state = {"server": None, "thread": None, "port": None, "scheme": None, "error": None}
_state_lock = threading.RLock()


# ---------------------------------------------------------------------------
# Answers
# ---------------------------------------------------------------------------
class Answer:
    def __init__(self, body=b"", status=200, content_type="text/plain; charset=utf-8", cache="no-store", extra=None):
        self.body = body if isinstance(body, bytes) else str(body).encode("utf-8")
        self.status, self.content_type, self.cache, self.extra = status, content_type, cache, extra or {}

    def headers(self):
        h = dict(SECURITY_HEADERS)
        h.update({"Content-Type": self.content_type, "Content-Length": str(len(self.body)),
                  "Cache-Control": self.cache})
        h.update(self.extra)
        return [(k.lower().encode(), v.encode()) for k, v in h.items()]


def not_found():
    return Answer("Not found\n", 404)


def allowed(ip, now=None):
    """False once an address has asked more than RATE_MAX times in RATE_WINDOW seconds."""
    now = time.monotonic() if now is None else now
    with _rate_lock:
        q = _rate.get(ip)
        if q is None:
            if len(_rate) >= RATE_TRACKED:
                for k in [k for k, v in _rate.items() if not v or now - v[-1] > RATE_WINDOW]:
                    _rate.pop(k, None)
                if len(_rate) >= RATE_TRACKED:
                    _rate.clear()
            q = _rate[ip] = deque()
        while q and now - q[0] > RATE_WINDOW:
            q.popleft()
        if len(q) >= RATE_MAX:
            return False
        q.append(now)
        return True


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
def e(text):
    return html.escape(str(text if text is not None else ""), quote=True)


def _shell(title, body, refresh_path=None):
    v = quote(_asset_version(), safe="")
    data = f' data-status="{e(refresh_path)}"' if refresh_path else ""
    return ("<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<meta name=\"robots\" content=\"noindex\">"
            f"<title>{e(title)}</title>"
            f"<link rel=\"stylesheet\" href=\"/join/assets/join.css?v={v}\">"
            f"<script src=\"/join/assets/join.js?v={v}\" defer></script></head>"
            f"<body{data}><main>{body}</main>"
            f"<footer>Hosted with <a href=\"{WEBSITE}\" rel=\"noopener noreferrer\">RedstonePanel</a></footer>"
            "</body></html>\n")


_asset_v = {"at": 0.0, "v": "0"}

def _asset_version():
    """Changes when the stylesheet or script does, so browsers fetch the new one. Says nothing
    about the panel's version."""
    now = time.time()
    if now - _asset_v["at"] > 60:
        stamp = 0
        for name in ASSETS:
            try:
                stamp = max(stamp, int(os.path.getmtime(os.path.join(ASSET_DIR, name))))
            except OSError:
                pass
        _asset_v.update(at=now, v=format(stamp & 0xFFFFFF, "x"))
    return _asset_v["v"]


def _status_pill(view):
    on = view["status"] == "online"
    return (f'<span id="status" class="pill {"on" if on else "off"}">{"Online" if on else "Offline"}</span>')


def _players_text(view):
    p = view["players"]
    if view["status"] != "online" or not p or p.get("online") is None:
        return ""
    return f'{p["online"]} of {p["max"]} playing' if p.get("max") is not None else f'{p["online"]} playing'


def _names_html(view):
    p = view["players"] or {}
    names = p.get("names")
    if not names:
        return ""
    return "Playing now: " + ", ".join(e(n) for n in names)


def _copy_row(label, value, what):
    return (f'<div class="copy"><div><span class="label">{e(label)}</span>'
            f'<code class="value">{e(value)}</code></div>'
            f'<button type="button" class="btn" data-copy="{e(value)}" aria-label="Copy {e(what)}">Copy</button></div>')


def _steps(items):
    return "<ol>" + "".join(f"<li>{i}</li>" for i in items) + "</ol>"


def _java_address_block(view):
    a = view["address"]
    if not a["java"]:
        return ('<p class="warn">The address isn\'t set up on this page yet. Ask whoever runs the server for it.</p>')
    return _copy_row("Server address", a["java"], "the server address")


def _bedrock_block(view, heading):
    a = view["address"]["bedrock"]
    if not a:
        return ""
    name = view["title"].replace("|", " ").replace(":", " ")[:40]
    link = "minecraft://?addExternalServer=" + quote(f"{name}|{a['host']}:{a['port']}", safe="")
    return (f'<section class="card"><h2>{e(heading)}</h2>'
            + _copy_row("Server address", a["host"], "the server address")
            + _copy_row("Port", a["port"], "the port")
            + f'<p><a class="btn primary" href="{e(link)}">Add this server to Minecraft</a></p>'
            '<p class="small">The button works on phones, tablets and Windows with Minecraft installed. '
            'Or add it by hand:</p>'
            + _steps(["Open Minecraft and choose <b>Play</b>.",
                      "Open the <b>Servers</b> tab and choose <b>Add Server</b>.",
                      "Type the address and port shown above, then <b>Save</b> and join."])
            + '<p class="small">Xbox, PlayStation and Switch have no <b>Add Server</b> button. On the same home '
              'network the server shows up by itself under <b>Friends</b> or <b>Worlds</b>.</p></section>')


def _need_block(view):
    mc = e(view["minecraft"] or "the server's version")
    if view["needs"] == "none":
        return (f'<section class="card"><h2>What you need</h2>'
                f'<p><b>Minecraft: Java Edition {mc}</b>. No mods are needed.</p></section>')
    loader = e(view["loader"] or "mods")
    pack = view["pack"]
    out = f'<section class="card"><h2>What you need</h2><p><b>Minecraft: Java Edition {mc}</b> with <b>{loader}</b> and this server\'s mods.</p>'
    pp = view.get("player_pack")
    if pp:
        base = f'/join/{quote(view["path"], safe="")}'
        n, opt = pp["count"], pp["optional"]
        out += (f'<p class="packname">{e(pp["title"])} <span class="muted">player pack {e(pp["version"])}</span></p>'
                f'<p class="buttons"><a class="btn primary" href="{e(base)}/pack.mrpack" download="{e(pp["file"])}">'
                f'Download the player pack</a></p>'
                f'<p class="small">{n} mod{"" if n == 1 else "s"}'
                f'{f", {opt} of them optional" if opt else ""}, at the versions this server runs. '
                'The pack is made from the server itself, so it changes when the server\'s mods do: if you '
                'can\'t join after an update, download it again.</p>')
        rows = "".join(
            "<li>" + (f'<a href="{e(m["url"])}" rel="noopener noreferrer">{e(m["name"])}</a>' if m["url"] else e(m["name"]))
            + (f' <span class="muted">{e(m["version"])}</span>' if m["version"] else "")
            + (' <span class="muted">(optional)</span>' if m["optional"] else "") + "</li>" for m in pp["mods"])
        out += f'<details><summary>The mods in the pack</summary><ul class="mods">{rows}</ul></details>'
        if pp["ask"]:
            out += ('<p class="warn">Not in the pack, because they aren\'t on Modrinth: '
                    + ", ".join(e(x) for x in pp["ask"])
                    + ". Ask whoever runs the server whether you need them.</p>")
        return out + "</section>"
    if pack and pack["url"]:
        label = e(pack["name"]) + (f' <span class="muted">{e(pack["version"])}</span>' if pack["version"] else "")
        out += f'<p class="packname">{label}</p><p class="buttons">'
        out += f'<a class="btn primary" href="{e(pack["url"])}" rel="noopener noreferrer">Get the modpack</a>'
        if pack["app_url"]:
            out += f' <a class="btn" href="{e(pack["app_url"])}">Open in Modrinth App</a>'
        out += "</p>"
        if pack["source"] == "modrinth":
            out += ('<p class="small">The modpack installs every mod for you, at the version this server runs.</p>')
    elif pack:
        label = e(pack["name"]) + (f' {e(pack["version"])}' if pack["version"] else "")
        out += (f'<p>This server runs the modpack <b>{label}</b>. Ask whoever runs the server where to get it.</p>')
    else:
        out += ('<p class="warn">You need the same mods as the server. Ask whoever runs it for the list.</p>')
    return out + "</section>"


def _howto_block(view):
    if view["needs"] == "none":
        mc = e(view["minecraft"] or "the version above")
        return ('<section class="card"><h2>How to join</h2>'
                + _steps([f"Open the Minecraft Launcher and start <b>Java Edition {mc}</b>. "
                          "If it isn't in the list, add it under <b>Installations</b>.",
                          "Choose <b>Multiplayer</b>, then <b>Add Server</b>.",
                          "Paste the server address, choose <b>Done</b>, and join."])
                + "</section>")
    pack = view["pack"]
    out = '<section class="card"><h2>How to join</h2>'
    if view.get("player_pack"):
        toml = f'/join/{quote(view["path"], safe="")}/packwiz/pack.toml'
        last = "Press <b>Play</b>, then <b>Multiplayer</b> and <b>Add Server</b>, and paste the server address."
        out += ('<details open><summary>With Modrinth App</summary>'
                + _steps(["Install <a href=\"https://modrinth.com/app\" rel=\"noopener noreferrer\">Modrinth App</a> "
                          "and sign in with your Minecraft account.",
                          "Press <b>Download the player pack</b> above.",
                          "In Modrinth App press <b>+</b> (new instance), choose <b>From file</b>, and pick the "
                          "<code>.mrpack</code> file you downloaded.", last])
                + '</details><details><summary>With Prism Launcher</summary>'
                + _steps(["Install <a href=\"https://prismlauncher.org\" rel=\"noopener noreferrer\">Prism Launcher</a> "
                          "and add your Minecraft account.",
                          "Press <b>Download the player pack</b> above.",
                          "Choose <b>Add Instance</b>, then <b>Import</b>, and pick the <code>.mrpack</code> file "
                          "you downloaded.",
                          "Launch it, then <b>Multiplayer</b> and <b>Add Server</b>, and paste the server address."])
                + '</details><details><summary>Keep the mods up to date by themselves (packwiz)</summary>'
                  '<p class="small">For people who know their launcher: Prism Launcher and MultiMC can check this '
                  'server\'s mods every time the game starts, with '
                  '<a href="https://packwiz.infra.link/tutorials/installing/packwiz-installer/" '
                  'rel="noopener noreferrer">packwiz-installer</a>. Point it at this address:</p>'
                + f'<div class="copy"><div><span class="label">packwiz pack</span>'
                  f'<code class="value" data-abs="{e(toml)}">{e(toml)}</code></div>'
                  f'<button type="button" class="btn" data-copy="{e(toml)}" data-abs-copy="{e(toml)}" '
                  f'aria-label="Copy the packwiz address">Copy</button></div></details>')
        return out + ('<p class="small">The official Minecraft Launcher can\'t install modpacks by itself.</p></section>')
    if pack and pack["source"] == "modrinth":
        name = e(pack["name"])
        out += ('<details open><summary>With Modrinth App</summary>'
                + _steps(["Install <a href=\"https://modrinth.com/app\" rel=\"noopener noreferrer\">Modrinth App</a> "
                          "and sign in with your Minecraft account.",
                          "Press <b>Open in Modrinth App</b> above, or <b>Get the modpack</b> and choose "
                          "<b>Install</b> there.",
                          "Press <b>Play</b>, then <b>Multiplayer</b> and <b>Add Server</b>, and paste the server address."])
                + '</details><details><summary>With Prism Launcher</summary>'
                + _steps(["Install <a href=\"https://prismlauncher.org\" rel=\"noopener noreferrer\">Prism Launcher</a> "
                          "and add your Minecraft account.",
                          f"Choose <b>Add Instance</b>, then <b>Modrinth</b>, and search for <b>{name}</b>.",
                          "Pick the version shown above and choose <b>OK</b>.",
                          "Launch it, then <b>Multiplayer</b> and <b>Add Server</b>, and paste the server address."])
                + "</details>")
    else:
        out += _steps(["Get the mods this server uses (see above) into a launcher that handles mods, such as "
                       "<a href=\"https://modrinth.com/app\" rel=\"noopener noreferrer\">Modrinth App</a> or "
                       "<a href=\"https://prismlauncher.org\" rel=\"noopener noreferrer\">Prism Launcher</a>.",
                       "Start the game from that launcher.",
                       "Choose <b>Multiplayer</b>, then <b>Add Server</b>, and paste the server address."])
    return out + ('<p class="small">The official Minecraft Launcher can\'t install modpacks by itself.</p></section>')


def render_page(view):
    icon = (f'<img class="icon" src="/join/{e(quote(view["path"], safe=""))}/icon.png" alt="" width="64" height="64">'
            if view["has_icon"] else '<div class="icon blank" aria-hidden="true"></div>')
    kind = "Bedrock Edition" if view["edition"] == "Bedrock" else "Java Edition"
    sub = " · ".join(x for x in (kind, e(view["loader"]) if view["needs"] == "mods" else "",
                                 e(view["minecraft"])) if x)
    head = (f'<header class="top">{icon}<div><h1>{e(view["title"])}</h1>'
            f'<p class="sub"><span id="version">{sub}</span></p>'
            f'<p class="state">{_status_pill(view)} <span id="players">{e(_players_text(view))}</span></p>'
            f'<p id="names" class="small">{_names_html(view)}</p></div></header>')
    body = head
    if view["note"] or view["link"]:
        body += '<section class="card note">'
        if view["note"]:
            body += "".join(f"<p>{e(line)}</p>" for line in view["note"].split("\n") if line.strip())
        if view["link"]:
            body += (f'<p><a class="btn" href="{e(view["link"]["url"])}" rel="noopener noreferrer nofollow">'
                     f'{e(view["link"]["label"])}</a></p>')
        body += "</section>"
    if view["edition"] == "Bedrock":
        if view["address"]["bedrock"]:
            body += _bedrock_block(view, "Join this server")
        else:
            body += ('<section class="card"><h2>Join this server</h2><p class="warn">The address isn\'t set up on '
                     'this page yet. Ask whoever runs the server for it.</p></section>')
        body += (f'<section class="card"><h2>What you need</h2><p><b>Minecraft</b> (Bedrock Edition) on a phone, '
                 f'tablet or Windows{", version " + e(view["minecraft"]) + " or newer" if view["minecraft"] else ""}. '
                 'Any packs the server uses are downloaded when you join.</p></section>')
    else:
        body += f'<section class="card"><h2>Server address</h2>{_java_address_block(view)}</section>'
        body += _need_block(view)
        body += _howto_block(view)
        if view["geyser"] and view["address"]["bedrock"]:
            body += _bedrock_block(view, "Playing on a phone, tablet or Windows (Bedrock)?")
    return _shell(view["title"], body, refresh_path=f'/join/{quote(view["path"], safe="")}/status.json')


def render_index(views):
    rows = "".join(
        f'<a class="row" href="/join/{e(quote(v["path"], safe=""))}"><span><b>{e(v["title"])}</b>'
        f'<span class="muted">{e(" · ".join(x for x in (v["edition"], v["loader"] if v["needs"] == "mods" else "", v["minecraft"]) if x))}</span></span>'
        f'<span class="pill {"on" if v["status"] == "online" else "off"}">{"Online" if v["status"] == "online" else "Offline"}</span></a>'
        for v in views)
    body = ('<header class="top"><div><h1>Minecraft servers</h1><p class="sub">Pick a server to see how to join.</p>'
            '</div></header><section class="card list">' + (rows or "<p>No servers are listed yet.</p>") + "</section>")
    return _shell("Minecraft servers", body)


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------
def _view(key, cfg):
    return joinpage.public_view(cfg, joinpage.summary(key))


def _ip(text):
    try:
        ip = ipaddress.ip_address(str(text or "").strip().strip("[]"))
    except ValueError:
        return None
    return ip.ipv4_mapped if ip.version == 6 and ip.ipv4_mapped else ip


def _from_proxy(ip, proxies):
    return ip is not None and any(ip in ipaddress.ip_network(n) for n in proxies)


def client_ip(peer, forwarded, proxies):
    """Who the visitor is (1.21.8). A request from one of the admin's proxies may say who it is
    forwarding for in X-Forwarded-For; the list is read from the right, skipping the proxies
    themselves, and the first other address is the player. Anyone else's header is ignored,
    so nobody can dodge the rate limit by writing their own. Returns (address text, via proxy)."""
    ip = _ip(peer)
    if not proxies or not _from_proxy(ip, proxies):
        return (str(ip) if ip else (peer or "?")), False
    for part in reversed([p for p in (forwarded or "").split(",") if p.strip()][-20:]):
        hop = _ip(part.strip().rsplit(":", 1)[0] if part.count(":") == 1 else part)
        if hop is None:
            break                       # a broken entry: stop trusting the list here
        if not _from_proxy(hop, proxies):
            return str(hop), True
    return str(ip), True


def handle(method, path, ip, forwarded=None, s=None):
    """One request -> Answer. Only GET and HEAD, only under /join. `ip` is the address the
    connection came from, `forwarded` its X-Forwarded-For header (used only from a proxy)."""
    if method not in ("GET", "HEAD") or len(path) > 200:
        return not_found()
    s = s or panel_settings.load()
    ip, via_proxy = client_ip(ip, forwarded, s.get("join_proxies") or [])
    if s.get("join_proxy_only") and not via_proxy:
        return not_found()
    if not allowed(ip or "?"):
        return Answer("Too many requests. Try again in a minute.\n", 429, extra={"Retry-After": "60"})
    parts = path.split("/")[1:]
    if not parts or parts[0] != "join" or any(p in (".", "..") for p in parts):
        return not_found()
    parts = parts[1:]
    if parts and parts[-1] == "" and len(parts) <= 2:      # /join/ and /join/<link>/
        parts = parts[:-1]
    if not s["join_enabled"]:
        return not_found()
    if not parts:
        if not s["join_index"]:
            return not_found()
        views = []
        for key, cfg in joinpage.listed():
            try:
                views.append(_view(key, cfg))
            except joinpage.JoinError:
                continue
        return Answer(render_index(views), content_type="text/html; charset=utf-8")
    if parts[0] == "assets":
        if len(parts) != 2 or parts[1] not in ASSETS:
            return not_found()
        try:
            with open(os.path.join(ASSET_DIR, parts[1]), "rb") as f:
                return Answer(f.read(), content_type=ASSETS[parts[1]], cache="public, max-age=3600")
        except OSError:
            return not_found()
    if len(parts) > 2 and not (parts[1] == "packwiz" and len(parts) <= 4):
        return not_found()
    hit = joinpage.find(parts[0])
    if not hit:
        return not_found()
    key, cfg = hit
    try:
        if len(parts) == 1:
            return Answer(render_page(_view(key, cfg)), content_type="text/html; charset=utf-8")
        if parts[1] == "status.json":
            import json
            return Answer(json.dumps(joinpage.public_status(_view(key, cfg))),
                          content_type="application/json; charset=utf-8")
        if parts[1] == "icon.png":
            icon = joinpage.summary(key).get("icon")
            data = base64.b64decode(icon, validate=True) if icon else b""
            if not data.startswith(b"\x89PNG\r\n\x1a\n"):
                return not_found()
            return Answer(data, content_type="image/png", cache="public, max-age=300")
        if parts[1] == "pack.mrpack" or parts[1] == "packwiz":
            from core import playerpack
            p, title, data = joinpage.player_pack(key)
            if not p or not p["mods"]:
                return not_found()
            if parts[1] == "pack.mrpack":
                name = playerpack.file_name(title, p["version"])
                return Answer(data, content_type="application/x-modrinth-modpack+zip", cache="no-cache",
                              extra={"Content-Disposition": f'attachment; filename="{name}"'})
            got = playerpack.packwiz(p, title, data).get("/".join(parts[2:]))
            if got is None:
                return not_found()
            jar = parts[-1].endswith(".jar")
            return Answer(got, content_type="application/java-archive" if jar else "text/plain; charset=utf-8",
                          cache="no-cache")
    except joinpage.JoinError:
        return Answer("This server's page isn't available right now.\n", 503)
    except Exception:
        return Answer("This server's page isn't available right now.\n", 503)
    return not_found()


async def app(scope, receive, send):
    """The whole public web server: every request goes through handle()."""
    if scope["type"] != "http":
        return
    from starlette.concurrency import run_in_threadpool
    client = scope.get("client") or (None, None)
    forwarded = ",".join(v.decode("latin-1") for k, v in scope.get("headers") or [] if k == b"x-forwarded-for")
    answer = await run_in_threadpool(handle, scope.get("method", ""), scope.get("path", ""), client[0], forwarded)
    await send({"type": "http.response.start", "status": answer.status, "headers": answer.headers()})
    await send({"type": "http.response.body", "body": b"" if scope.get("method") == "HEAD" else answer.body})


# ---------------------------------------------------------------------------
# The listener
# ---------------------------------------------------------------------------
def status(s=None):
    s = s or panel_settings.load()
    with _state_lock:
        running = bool(_state["thread"] and _state["thread"].is_alive() and _state["server"]
                       and getattr(_state["server"], "started", False))
        return {"enabled": s["join_enabled"], "port": s["join_port"], "index": s["join_index"],
                "running": running, "serving_port": _state["port"] if running else None,
                "scheme": _state["scheme"] or (panel_settings.RUNTIME.get("scheme") or "http"),
                "error": _state["error"],
                # False when the panel wasn't started by run.py (tests, a hand-run uvicorn)
                "managed": panel_settings.RUNTIME.get("scheme") is not None}


def _tls_args():
    cert, key = panel_settings.TLS_FILES.get("cert"), panel_settings.TLS_FILES.get("key")
    if panel_settings.RUNTIME.get("scheme") == "https" and cert and key:
        return {"ssl_certfile": cert, "ssl_keyfile": key}, "https"
    return {}, "http"


def stop():
    with _state_lock:
        server, thread = _state["server"], _state["thread"]
        _state.update(server=None, thread=None, port=None)
    if server:
        server.should_exit = True
    if thread and thread is not threading.current_thread():
        thread.join(8)


def start(port):
    """Starts listening on `port` (stopping an earlier listener first). Returns an error text
    or None."""
    import uvicorn
    stop()
    if not panel_settings.port_free(port):
        with _state_lock:
            _state["error"] = f"Port {port} is already in use on this server."
        return _state["error"]
    ssl_args, scheme = _tls_args()
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning", access_log=False,
                            server_header=False, date_header=True, lifespan="off",
                            timeout_keep_alive=5, timeout_graceful_shutdown=3, limit_concurrency=200, **ssl_args)
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None          # the panel's own server owns the signals
    thread = threading.Thread(target=server.run, name="join-pages", daemon=True)
    with _state_lock:
        _state.update(server=server, thread=thread, port=port, scheme=scheme, error=None)
    thread.start()
    for _ in range(50):
        if getattr(server, "started", False) or not thread.is_alive():
            break
        time.sleep(0.1)
    if not getattr(server, "started", False):
        with _state_lock:
            _state.update(server=None, thread=None, port=None, error=f"The join pages couldn't start on port {port}.")
        server.should_exit = True
        return _state["error"]
    return None


def haproxy_config(backend_host, s=None):
    """Lines for haproxy.cfg that send /join on a shared HTTPS frontend to these pages (1.21.8).
    backend_host is how the proxy reaches this machine."""
    s = s or panel_settings.load()
    st = status(s)
    tls = " ssl verify none" if st["scheme"] == "https" else ""
    return "\n".join([
        "# Inside your HTTPS frontend (mode http):",
        "    option forwardfor",
        "    acl redstonepanel_join path /join or path_beg /join/",
        "    use_backend redstonepanel_join if redstonepanel_join",
        "",
        "backend redstonepanel_join",
        "    mode http",
        "    option httpchk GET /join/assets/join.css",
        f"    server panel {backend_host}:{s['join_port']} check{tls}",
    ])


def firewall_rules(s=None):
    """The firewall rule the join pages need while they're on."""
    s = s or panel_settings.load()
    return {(str(s["join_port"]), "tcp")} if s["join_enabled"] else set()


def check_can_enable(s, old):
    """Raises JoinError if the pages can't be turned on (or moved) with these settings."""
    if not s["join_enabled"]:
        return
    with _state_lock:
        serving = _state["port"] if _state["thread"] and _state["thread"].is_alive() else None
    if serving != s["join_port"] and not panel_settings.port_free(s["join_port"]):
        raise joinpage.JoinError(f"Port {s['join_port']} is already in use on this server.")


def apply(new, old=None, actor="the panel"):
    """Makes the listener match the settings: starts, moves or stops it, and opens or closes
    its port in the firewall. Called when the setting changes and when the panel starts."""
    port, old_port = new["join_port"], (old or {}).get("join_port")
    was_on = bool((old or {}).get("join_enabled"))
    if new["join_enabled"]:
        error = None
        with _state_lock:
            serving = _state["port"] if _state["thread"] and _state["thread"].is_alive() else None
        if serving != port:
            error = start(port)
        if was_on and old_port and old_port != port:
            manager.close_firewall_rules({(str(old_port), "tcp")})
        if not error:
            manager.open_firewall(port, "tcp")
        if old is not None:
            manager.write_log(f"[JOIN] {actor} turned the join pages on, port {port}."
                              + (f" They couldn't start: {error}" if error else ""))
        elif error:
            manager.write_log(f"[JOIN] {error}")
    else:
        stop()
        with _state_lock:
            _state["error"] = None
        if was_on:
            manager.close_firewall_rules({(str(old_port or port), "tcp")})
            manager.write_log(f"[JOIN] {actor} turned the join pages off.")
