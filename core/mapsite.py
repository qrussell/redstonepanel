"""World maps on the join site (1.30): the part of core/joinsite.py's port that shows maps.

    GET /map/<member>/<server>/?t=<ticket>   the panel's Map tab: a one-minute ticket from the
                                             panel becomes this map's own cookie, then a redirect
    GET /map/<member>/<server>/<path>        the map, for whoever has that cookie
    GET /join/<link>/map/<path>              the map on a join page that shows it

member is "local" for this panel's own servers, or a cluster node's id: on a master, a node's map
comes from the node through the cluster connection (GET /api/cluster/maps/{name}/..., which
only the master's node token reaches), so nodes stay private.

The cookie `rp_map` is signed with a purpose of its own (never a panel session), HttpOnly, and
limited to the map's own path, so each map has its own and a map page can't read it. Map pages
get a Content-Security-Policy of their own: scripts, styles, images and data only from this
address. Only GET and HEAD are answered.
"""
import re
import time
from urllib.parse import parse_qs, quote, unquote

from core import joinpage, worldmap

MAP_COOKIE = "rp_map"
PANEL_RE = re.compile(r"^/map/(local|n-[0-9a-f]{4,32})/(minecraft[A-Za-z0-9_.@-]{0,100})(/.*)?$")
JOIN_RE = re.compile(r"^/join/([A-Za-z0-9_-]{1,64})/map(/.*)?$")
RATE_MAX = 3000                # requests per address a minute: a map loads hundreds of tiles
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval' blob:; "
       "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; "
       "connect-src 'self'; worker-src 'self' blob:; media-src 'self'; object-src 'none'; base-uri 'self'; "
       "form-action 'none'")
HEADERS = {"X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin",
           "Cross-Origin-Resource-Policy": "same-origin", "Permissions-Policy": "geolocation=(), camera=(), microphone=()"}

_rate = {}


def is_map_path(path):
    return bool(PANEL_RE.match(path) or JOIN_RE.match(path))


def _allowed(ip, now=None):
    now = time.monotonic() if now is None else now
    q = _rate.get(ip)
    if q is None:
        if len(_rate) > 5000:
            _rate.clear()
        q = _rate[ip] = []
    while q and now - q[0] > 60:
        q.pop(0)
    if len(q) >= RATE_MAX:
        return False
    q.append(now)
    return True


def _finish(reply, frame_ok=True):
    h = dict(HEADERS)
    h["Content-Security-Policy"] = CSP + ("; frame-ancestors *" if frame_ok else "; frame-ancestors 'none'")
    h.update(reply.headers)
    reply.headers = h
    return reply


def _text(status, text):
    return worldmap.Reply(status, {"Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store"},
                          (text + "\n").encode())


def _sub(rest):
    """The path inside the map ('' for its first page), decoded and checked."""
    sub = unquote((rest or "/")[1:])
    if "\x00" in sub or "\\" in sub or len(sub) > 400 or any(p in (".", "..") for p in sub.split("/")):
        return None
    return sub


def _cookie(value, path, secure, max_age):
    parts = [f"{MAP_COOKIE}={value}", f"Path={path}", "HttpOnly", "SameSite=Lax", f"Max-Age={int(max_age)}"]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def _remote(member, server, sub, method, headers, query, players):
    """A node's map, through the cluster connection (on a master)."""
    from core import cluster, cluster_client
    node = cluster.find_node(member) if cluster.role() == "master" else None
    if not node:
        return _text(404, "Not found")
    path = f"/api/cluster/maps/{quote(server, safe='')}/{quote(sub, safe='/')}"
    q = [query] if query else []
    if not players:
        q.append("players=0")
    if q:
        path += "?" + "&".join(q)
    try:
        res = cluster_client.open_proxy(node, method, path, actor="map", role="admin")
    except Exception:
        return worldmap.Reply(503, {"Content-Type": "text/html; charset=utf-8"},
                              worldmap.page("Map not available", "The machine this server runs on can't be reached right now."))
    status = res.status
    keep = ("content-type", "content-encoding", "content-length", "last-modified", "etag", "cache-control")
    h = {k.title(): v for k, v in res.res.getheaders() if k.lower() in keep}
    if method == "HEAD":
        res.close()
        return worldmap.Reply(status, h)
    return worldmap.Reply(status, h, chunks=res.chunks(), close=res.close)


def _serve(member, server, sub, method, headers, query, players):
    if member == "local":
        return worldmap.serve_local(server, sub, method, headers, query, players)
    return _remote(member, server, sub, method, headers, query, players)


def handle(method, path, query, ip, cookie_header, headers, secure, s=None):
    """One map request on the join site -> worldmap.Reply (status, headers, body or chunks)."""
    from core import joinsite
    if method not in ("GET", "HEAD"):
        return _finish(_text(405, "Maps can only be looked at."))
    if not _allowed(ip or "?"):
        return _finish(_text(429, "Too many requests. Try again in a minute."))
    cookies = joinsite._cookies(cookie_header)
    m = PANEL_RE.match(path)
    if m:
        member, server, rest = m.group(1), m.group(2), m.group(3)
        base = f"/map/{member}/{server}/"
        if rest is None:
            return _finish(worldmap.Reply(303, {"Location": base}, b""))
        sub = _sub(rest)
        if sub is None:
            return _finish(_text(404, "Not found"))
        qs = parse_qs(query or "", max_num_fields=5)
        ticket = (qs.get("t") or [None])[0]
        if ticket:
            got = worldmap.read_ticket(ticket, member, server)
            if not got:
                return _finish(worldmap.Reply(403, {"Content-Type": "text/html; charset=utf-8"}, worldmap.page(
                    "Link expired", "Open the map again from the panel's Map tab.")))
            r = worldmap.Reply(303, {"Location": base + sub, "Cache-Control": "no-store"}, b"")
            r.headers["Set-Cookie"] = _cookie(worldmap.make_viewer(got), base, secure, worldmap.VIEWER_SECONDS)
            return _finish(r)
        viewer = worldmap.read_viewer(cookies.get(MAP_COOKIE), member, server)
        if not viewer:
            return _finish(worldmap.Reply(403, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"},
                                          worldmap.page("Sign in to the panel", "Open this map from the panel's Map tab.")))
        return _finish(_serve(member, server, sub, method, headers, query, True))
    m = JOIN_RE.match(path)
    if not m:
        return _finish(_text(404, "Not found"))
    s = s or joinsite.panel_settings.load()
    if not s["join_enabled"]:
        return _finish(_text(404, "Not found"))
    hit = joinpage.find(m.group(1))
    if not hit:
        return _finish(_text(404, "Not found"))
    key, cfg = hit
    if m.group(2) is None:
        return _finish(worldmap.Reply(303, {"Location": f"/join/{m.group(1)}/map/"}, b""))
    sub = _sub(m.group(2))
    if sub is None:
        return _finish(_text(404, "Not found"))
    gated = cfg.get("access", "anyone") != "anyone"
    visitor = joinsite.read_visitor(cookies.get(joinsite.VISITOR_COOKIE))[0] if gated else None
    if not joinpage.abilities(cfg, key, visitor)["map"]:
        return _finish(_text(404, "Not found"))
    member, server = joinpage.split_key(key)
    return _finish(_serve(member, server, sub, method, headers, query, bool(cfg.get("map_players"))))
