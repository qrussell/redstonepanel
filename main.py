from fastapi import FastAPI, HTTPException, Body, UploadFile, File, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.concurrency import run_in_threadpool
from core.models import ServerDeployRequest, ServerActionRequest, ServerPropertiesUpdate, ServerUpdateRequest
from pydantic import BaseModel
from typing import List, Optional, Union
import core.manager as manager
import core.files as files
import core.mods as mods
import core.catalog as catalog
import core.runtimes as runtimes
import core.selfupdate as selfupdate
import core.phantom as phantom
import core.host as host
import urllib.parse
import core.javaservers as javaservers
import core.playerlog as playerlog
import core.whitelist as whitelist
import core.moderation as moderation
import core.chatassist as chatassist
import core.backups as backups
import core.notify as notify
import core.setup as setup_guide
import core.restarts as restarts
import core.updates as updates
import core.convert as convert
import core.auth as auth
import core.settings as panel_settings
import core.twofactor as twofactor
import core.passkeys as passkeys
import core.devices as devices
import core.firstsignin as firstsignin
import core.qr as qr
import core.portforward as portforward
import core.portaudit as portaudit
from core.version import panel_version
import core.cluster as cluster
import core.cluster_client as cluster_client
import core.transfer as transfer
import core.moves as moves
import core.backup_copies as backup_copies
import core.backup_targets as backup_targets
import core.perf as perf
import core.crashes as crashes
import core.bootstart as bootstart
import core.apitokens as apitokens
import core.grants as grants
import core.sshkeys as sshkeys
import core.sftp as sftp
from core.platform import paths
from starlette.routing import Match
import core.modpacks as modpacks
import core.joinpage as joinpage
import core.joinsite as joinsite
import core.playerpack as playerpack
import core.terminal as terminal
from fastapi import WebSocket
from starlette.background import BackgroundTask
import json
import re
import uuid
import os
import platform
import shutil
import signal
import tempfile
import threading
import time

app = FastAPI(title="RedstonePanel API", version=panel_version())

class RawPropertiesUpdate(BaseModel):
    content: str

# The panel's own pages and files. Browsers must check them on every load (no-cache: a quick 304
# when nothing changed), or after an update they can keep running the old app.js for days. The
# two pages also link app.js and the stylesheet with ?v=<version>, so an update always fetches
# the new files, even in a browser that cached them before this header existed.
STATIC_DIR = os.path.join(panel_settings.APP_DIR, "static")
PAGES = ("index.html", "login.html")
_page_cache = {}

def _page(name):
    path = os.path.join(STATIC_DIR, name)
    mtime = os.path.getmtime(path)
    hit = _page_cache.get(name)
    if not hit or hit[0] != mtime:
        with open(path, encoding="utf-8") as f:
            html = f.read()
        v = urllib.parse.quote(panel_version(), safe="")
        for asset in ("/static/js/app.js", "/static/js/chat-commands.js", "/static/js/propfile.js", "/static/css/tailwind.css"):
            html = html.replace(f'"{asset}"', f'"{asset}?v={v}"')
        hit = _page_cache[name] = (mtime, html)
    return hit[1]

@app.get("/static/index.html", include_in_schema=False)
def index_page():
    return HTMLResponse(_page("index.html"), headers={"Cache-Control": "no-cache"})

@app.get("/static/login.html", include_in_schema=False)
def login_page():
    return HTMLResponse(_page("login.html"), headers={"Cache-Control": "no-cache"})

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.middleware("http")
async def no_stale_static(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/") and "cache-control" not in response.headers:
        response.headers["Cache-Control"] = "no-cache"
    return response

# ---------------------------------------------------------------------------
# Access control for every /api/ call
#   1. Requests that change something are refused if the browser says they came from another
#      website (cross-site request forgery), signed in or not.
#   2. A valid session cookie, or (if an admin enabled it) a request from a trusted LAN range,
#      which gets the limited "user" role. Users and Panel Settings always need a signed-in admin.
#   3. Or, on a cluster node, the cluster master's node token (Authorization: Bearer rpn_...),
#      which reaches only the routes in cluster.NODE_ROUTES.
# ---------------------------------------------------------------------------
# /api/panel-info only says which panel this is (shown on the sign-in page), nothing more.
# /api/cluster/ca(.crt) is the cluster's public CA certificate; /api/cluster/join checks a join code.
PUBLIC_API = {"/api/auth/login", "/api/auth/login/code", "/api/auth/logout", "/api/health", "/api/panel-info",
              "/api/auth/passkeys/available", "/api/auth/passkey/options", "/api/auth/passkey/login",
              "/api/auth/first-sign-in", "/api/auth/first-sign-in/check",
              "/api/cluster/ca", "/api/cluster/ca.crt", "/api/cluster/join"}
ADMIN_API = ("/api/users", "/api/settings")
# What an API token (1.25) never reaches, whatever its grants: the account routes and the terminal
TOKEN_REFUSED_API = ("/api/auth/", "/api/terminal")
NODE_ONLY_API = ("/api/cluster/hello", "/api/cluster/membership", "/api/cluster/cert", "/api/cluster/templates",
                 "/api/cluster/transfers", "/api/cluster/held-backups")
CLUSTER_USER_API = ("/api/cluster/overview", "/api/cluster/hosts", "/api/cluster/nodes/", "/api/cluster/moves",
                    "/api/cluster/moved/", "/api/cluster/backup-copies")
STATE_CHANGING = {"POST", "PUT", "PATCH", "DELETE"}
# An admin who must set up two-factor (Panel Settings > Sign-in) can reach only these until they do
TWO_FACTOR_SETUP_API = ("/api/auth/me", "/api/auth/2fa", "/api/auth/logout")

@app.middleware("http")
async def access_control(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/"):
        if request.method in STATE_CHANGING and panel_settings.same_origin(request.headers) is False:
            return JSONResponse({"detail": "Request blocked: it came from another website."}, status_code=403)
        if path not in PUBLIC_API:
            ip = request.client.host if request.client else None
            bearer = request.headers.get("authorization", "")
            if bearer.lower().startswith("bearer ") and bearer[7:].strip().startswith(apitokens.PREFIX):
                # 1.25: an API token, acting as a `user` account with the token's own grants
                user = await run_in_threadpool(apitokens.read, bearer[7:].strip(), ip)
                if not user:
                    return JSONResponse({"detail": "This API token is wrong, revoked or expired."}, status_code=401)
                if path.startswith(TOKEN_REFUSED_API) and path != "/api/auth/me":
                    return JSONResponse({"detail": "An API token can't do this. Sign in to the panel."},
                                        status_code=403)
                if path.startswith(NODE_ONLY_API) or path.startswith(ADMIN_API):
                    return JSONResponse({"detail": "Only admins can do this, and an API token is never an admin."},
                                        status_code=403)
                if request.method in STATE_CHANGING:
                    manager.write_log(f"[API] {user['username']}: {request.method} {path} (from {ip})")
            elif bearer.lower().startswith("bearer "):
                user = await run_in_threadpool(cluster.read_node_token, bearer[7:].strip(),
                                               request.headers.get("x-redstonepanel-epoch"), ip)
                if not user:
                    return JSONResponse({"detail": "Not this cluster's master."}, status_code=401)
                if not cluster.path_allowed(request.method, path):
                    return JSONResponse({"detail": "Not allowed for the cluster master."}, status_code=403)
                # The master says which of its users clicked and that user's role there; a master
                # user gets what a user may do here, a master admin what an admin may (within the
                # allowlist, which never reaches users, settings or cluster membership).
                user = cluster.master_identity(user, request.headers.get("x-redstonepanel-actor"),
                                               request.headers.get("x-redstonepanel-role"))
                # 1.18: what the master's user may do on the server this request is about (the
                # master checked its own grants); not sent by an older master
                user["perms"] = grants.parse_header(request.headers.get("x-redstonepanel-perms"))
                if request.method in STATE_CHANGING:
                    manager.write_log(f"[CLUSTER] {user['username']}: {request.method} {path} (from {ip})")
            else:
                token = request.cookies.get(auth.SESSION_COOKIE)
                user = await run_in_threadpool(auth.read_session_token, token) if token else None
                if not user:
                    user = panel_settings.trusted_network_user(ip, request.headers, request.method)
                if not user:
                    return JSONResponse({"detail": "Not signed in."}, status_code=401)
                if user.get("needs_2fa_setup") and not path.startswith(TWO_FACTOR_SETUP_API):
                    return JSONResponse({"detail": "Set up two-factor sign-in first (this panel requires it "
                                                   "for admins)."}, status_code=403)
                if path.startswith(NODE_ONLY_API):
                    return JSONResponse({"detail": "Only the cluster's master can use this."}, status_code=403)
                if path.startswith(CLUSTER_USER_API) and user["source"] == "trusted":
                    return JSONResponse({"detail": "Sign in to see the cluster."}, status_code=403)
                if path.startswith(ADMIN_API) and user["role"] != "admin":
                    detail = ("Sign in as an admin to change this." if user["source"] == "trusted"
                              else "Only admins can do this.")
                    return JSONResponse({"detail": detail}, status_code=403)
            if _bad_server_name(request.method, path):
                return JSONResponse({"detail": "No Minecraft server by that name on this panel."}, status_code=404)
            refused = _grant_refusal(request.method, request.scope, user)
            if refused:
                return JSONResponse({"detail": refused}, status_code=403)
            request.state.user = user
        # Files in a server's folder are touched with the rights of the account that server runs
        # as (system.game_files()): the game account, or root for a server that runs as root
        server = _route_of(request.method, path)[1].get("server_name")
        if server:
            run_as = await run_in_threadpool(manager.server_run_as, server)
            with manager.system.server_account(run_as):
                return await call_next(request)
    return await call_next(request)

# ---------------------------------------------------------------------------
# 1.18: per-server permissions. Every route has a rule in core/grants.py (RULES); an account
# with the `user` role passes only when its grants allow it. Admins pass everything.
# ---------------------------------------------------------------------------
def _route_of(method, path):
    """(path template, path parameters) of the API route that would serve this request."""
    scope = {"type": "http", "method": method, "path": path, "root_path": "", "path_params": {}}
    for r in app.router.routes:
        if getattr(r, "path", "").startswith("/api/"):
            match, child = r.matches(scope)
            if match == Match.FULL:
                return r.path, child.get("path_params") or {}
    return None, {}

def _bad_server_name(method, path):
    """True when the route's {server_name} can't be a game server's name. Every server name is
    a "minecraft..." unit name with no slashes, dots-only or other path tricks, and many routes
    build folder and file paths from it, so a bad one is refused here, for admins too, before
    any route sees it."""
    _template, params = _route_of(method, path)
    name = params.get("server_name")
    return name is not None and not manager.SERVICE_NAME_RE.fullmatch(name)

def _admin_only_detail(user):
    return "Sign in as an admin to do this." if user.get("source") == "trusted" else "Only admins can do this."

def _rule_refusal(user, rule, params, member=None):
    """None when `user` passes this rule, else why not. member: the cluster node the server is
    on, when the path doesn't say (the proxy)."""
    if user["role"] == "admin" or rule in ("user", "public", "proxy"):
        return None
    if rule == "signed":
        if user.get("source") == "token":
            return "An API token can't change an account. Sign in to do this."
        return "Sign in to do this." if user.get("source") == "trusted" else None
    if rule == "node":
        return None if user.get("source") == "cluster" else "Only the cluster's master can use this."
    need = grants.needs(rule)
    if not need:
        return _admin_only_detail(user)
    perm, param = need
    if param:
        member = params.get(param)
    if grants.can(user, perm, params.get("server_name", ""), member):
        return None
    return grants.refusal(perm) if user.get("source") != "trusted" else _admin_only_detail(user)

def _grant_refusal(method, scope, user):
    if user["role"] == "admin":
        return None
    template, params = _route_of(method, scope["path"])
    if template is None:
        return None                       # no such route: the 404 follows
    return _rule_refusal(user, grants.rule_for(method, template), params)

def _visible_servers(user, servers, member=None):
    """A server list cut down to what the account may see, each with what it may do there
    ("perms"). Admins get the list as it is."""
    if user["role"] == "admin":
        return servers
    out = []
    for s in servers:
        perms = grants.perms_for(user, grants.server_key(s.get("name", ""), member))
        if "view" in perms:
            out.append(dict(s, perms=[p for p in grants.PERMS if p in perms]))
    return out

_LOG_SERVER_RE = re.compile(r"(?<![A-Za-z0-9_.@-])minecraft[A-Za-z0-9_.@-]*")
_LOG_PRIVATE = ("[AUTH]", "[SETTINGS]", "[CLUSTER]", "[SFTP]", "[FIREWALL]")

def _visible_log(user, content, member=None, lines=100):
    """The action console for an account limited to some servers: only lines that name a server
    it may see (and no other), and nothing about accounts, settings or the cluster."""
    seen = _can_view(user, member)
    out = []
    for line in (content or "").splitlines():
        if any(tag in line for tag in _LOG_PRIVATE):
            continue
        names = {n.rstrip(".:,") for n in _LOG_SERVER_RE.findall(line)}
        names = {n[:-len(".service")] if n.endswith(".service") else n for n in names}
        if names and all(seen(n) for n in names):
            out.append(line)
    return "\n".join(out[-lines:])

def _can_view(user, member=None):
    return lambda name: bool(name) and grants.can(user, "view", name, member)

@app.get("/api/health")
def health():
    return {"status": "ok"}

@app.get("/api/panel-info")
def panel_info():
    """Which panel this is, so several servers' panels can be told apart (also before sign-in).
    1.16.13: also that it is a RedstonePanel and its cluster role, so a master's Find panels
    (core/discovery.py) can offer to add standalone panels on the network."""
    return {"name": panel_settings.display_name(), "app": "RedstonePanel", "cluster": cluster.role()}

@app.get("/api/phone-qr")
def phone_qr_api(url: str):
    """Desktop → "Open on your phone" (1.16.10): the panel's address as a QR code (SVG), made here
    with core/qr.py so the address never goes to an outside service. Only web addresses."""
    if not url.startswith(("http://", "https://")) or any(c in url for c in "\r\n"):
        raise HTTPException(status_code=400, detail="Only an http:// or https:// address.")
    try:
        return Response(qr.svg(url), media_type="image/svg+xml", headers={"Cache-Control": "no-store"})
    except qr.QRError as e:
        raise HTTPException(status_code=400, detail=str(e))

class LoginRequest(BaseModel):
    username: str
    password: str
    remember: bool = False

def _set_session_cookie(request: Request, response: Response, identity, remember=False):
    device = None
    if remember:
        # Remember this device (1.24): a 30-day sign-in that Forget (or signing out here) ends
        device = devices.add(twofactor.account_key(identity["src"], identity["u"]),
                             devices.describe(request.headers.get("user-agent")),
                             request.client.host if request.client else "")
    token = auth.make_session_token(identity, device)
    response.set_cookie(auth.SESSION_COOKIE, token, max_age=auth.session_seconds(token),
                        httponly=True, samesite="strict", secure=request.url.scheme == "https", path="/")
    return token

def _remembered(remember):
    return " (remembering this device)" if remember else ""

@app.post("/api/auth/login")
def login_api(req: LoginRequest, request: Request, response: Response):
    ip = request.client.host if request.client else "unknown"
    try:
        identity = auth.login(req.username, req.password, ip)
    except auth.AuthError as e:
        manager.write_log(f"[AUTH] Failed sign-in for '{req.username[:64]}' from {ip}")
        raise HTTPException(status_code=401, detail=str(e))
    if identity.get("pending"):
        # Right password; the session comes after the code (POST /api/auth/login/code)
        response.set_cookie(auth.PENDING_COOKIE, auth.make_pending_token(identity, req.remember), max_age=auth.PENDING_SECONDS,
                            httponly=True, samesite="strict", secure=request.url.scheme == "https",
                            path="/api/auth/login")
        return {"code_needed": True}
    manager.write_log(f"[AUTH] {identity['u']} ({identity['src']}) signed in from {ip}{_remembered(req.remember)}")
    return auth.read_session_token(_set_session_cookie(request, response, identity, req.remember))

class CodeRequest(BaseModel):
    code: str

@app.post("/api/auth/login/code")
def login_code_api(req: CodeRequest, request: Request, response: Response):
    ip = request.client.host if request.client else "unknown"
    ticket = request.cookies.get(auth.PENDING_COOKIE)
    who = (auth.read_pending_token(ticket) or {}).get("u", "?")
    try:
        identity = auth.complete_two_factor(ticket, req.code, ip)
    except auth.AuthError as e:
        manager.write_log(f"[AUTH] Failed two-factor code for '{str(who)[:64]}' from {ip}")
        raise HTTPException(status_code=401, detail=str(e))
    response.delete_cookie(auth.PENDING_COOKIE, path="/api/auth/login")
    how = "a recovery code" if identity["used"] == "recovery" else "an app code"
    remember = bool(identity.get("r"))
    manager.write_log(f"[AUTH] {identity['u']} ({identity['src']}) signed in from {ip} with {how}{_remembered(remember)}")
    user = auth.read_session_token(_set_session_cookie(request, response, identity, remember))
    if user and identity["used"] == "recovery":
        user["recovery_left"] = twofactor.status(twofactor.account_key(identity["src"], identity["u"]))["recovery_left"]
    return user

@app.post("/api/auth/logout")
def logout_api(request: Request, response: Response):
    terminal.signed_out(request.cookies.get(auth.SESSION_COOKIE))      # its terminals close too
    remembered = auth.session_device(request.cookies.get(auth.SESSION_COOKIE))
    if remembered and auth.read_session_token(request.cookies.get(auth.SESSION_COOKIE)):
        devices.remove(*remembered)                                     # signing out forgets this device
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    response.delete_cookie(auth.PENDING_COOKIE, path="/api/auth/login")
    return {"status": "success"}

# --- Two-factor sign-in for the signed-in account (panel and Linux accounts alike) ---
def _tf_account(request: Request):
    user = request.state.user
    if user["source"] not in ("panel", "linux"):
        raise HTTPException(status_code=400, detail="Sign in with your account to set up two-factor sign-in.")
    return user, twofactor.account_key(user["source"], user["username"])

def _tf_check_code(user, key, code, request: Request):
    """A current code (or recovery code) before changing two-factor; wrong ones count as failed sign-ins."""
    ip = request.client.host if request.client else "unknown"
    keys = (f"user:{user['username'].lower()}", f"ip:{ip}")
    if auth._throttled(keys):
        raise HTTPException(status_code=429, detail="Too many wrong codes. Try again in a few minutes.")
    if not twofactor.verify(key, code):
        auth._record_fail(keys)
        raise HTTPException(status_code=400, detail="That code is wrong or was already used.")

@app.get("/api/auth/2fa")
def two_factor_status_api(request: Request):
    user, key = _tf_account(request)
    st = twofactor.status(key)
    st["required"] = auth.two_factor_required_for(user["role"])
    return st

@app.post("/api/auth/2fa/setup")
def two_factor_setup_api(request: Request):
    user, key = _tf_account(request)
    try:
        secret = twofactor.begin_setup(key)
    except twofactor.TwoFactorError as e:
        raise HTTPException(status_code=400, detail=str(e))
    uri = twofactor.provisioning_uri(secret, user["username"], panel_settings.display_name())
    return {"secret": secret, "uri": uri, "qr_svg": qr.svg(uri)}

@app.post("/api/auth/2fa/confirm")
def two_factor_confirm_api(req: CodeRequest, request: Request, response: Response):
    user, key = _tf_account(request)
    try:
        codes = twofactor.confirm_setup(key, req.code)
    except twofactor.TwoFactorError as e:
        raise HTTPException(status_code=400, detail=str(e))
    manager.write_log(f"[AUTH] {user['username']} ({user['source']}) turned on two-factor sign-in")
    # Sessions that never passed the second step stop working; this browser just did, so keep it
    v = next((u.get("session_version", 1) for u in auth._load_users() if u["username"] == user["username"]), 1) \
        if user["source"] == "panel" else 0
    _set_session_cookie(request, response, {"u": user["username"], "src": user["source"], "v": v, "m": 1})
    return {"recovery_codes": codes}

@app.post("/api/auth/2fa/recovery-codes")
def two_factor_new_codes_api(req: CodeRequest, request: Request):
    user, key = _tf_account(request)
    if not twofactor.is_enabled(key):
        raise HTTPException(status_code=400, detail="Two-factor sign-in is not on for this account.")
    _tf_check_code(user, key, req.code, request)
    codes = twofactor.new_recovery_codes(key)
    manager.write_log(f"[AUTH] {user['username']} ({user['source']}) made new two-factor recovery codes")
    return {"recovery_codes": codes}

@app.post("/api/auth/2fa/disable")
def two_factor_disable_api(req: CodeRequest, request: Request):
    user, key = _tf_account(request)
    if not twofactor.is_enabled(key):
        twofactor.remove(key)   # forget an unfinished setup
        return {"status": "success"}
    if auth.two_factor_required_for(user["role"]):
        raise HTTPException(status_code=400, detail="This panel requires two-factor sign-in for admins. To move it to "
                                                    "a new phone, ask another admin to reset it, or run "
                                                    f"'sudo redstonepanel reset-2fa {user['username']}' on the server.")
    _tf_check_code(user, key, req.code, request)
    twofactor.remove(key)
    manager.write_log(f"[AUTH] {user['username']} ({user['source']}) turned off two-factor sign-in")
    return {"status": "success"}

# --- 1.24: the first sign-in after installing on Windows: a one-use link to choose a password ---
class FirstSignInRequest(BaseModel):
    token: str
    password: Optional[str] = None

def _first_throttle(request: Request):
    ip = request.client.host if request.client else "unknown"
    keys = (f"ip:{ip}",)
    if auth._throttled(keys):
        raise HTTPException(status_code=429, detail="Too many failed sign-in attempts. Try again in a few minutes.")
    return ip, keys

@app.post("/api/auth/first-sign-in/check")
def first_sign_in_check_api(req: FirstSignInRequest, request: Request):
    ip, keys = _first_throttle(request)
    try:
        return {"username": firstsignin.check(req.token)}
    except firstsignin.FirstSignInError as e:
        auth._record_fail(keys)
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/auth/first-sign-in")
def first_sign_in_api(req: FirstSignInRequest, request: Request, response: Response):
    ip, keys = _first_throttle(request)
    try:
        firstsignin.check(req.token)
    except firstsignin.FirstSignInError as e:
        auth._record_fail(keys)
        raise HTTPException(status_code=400, detail=str(e))
    try:
        name = firstsignin.use(req.token, req.password or "",
                               lambda user, pw: auth.update_panel_user(user, password=pw),
                               plain_url=re.sub(r"//(127\.0\.0\.1|\[::1\])(?=[:/])", "//localhost", str(request.base_url)))
    except (auth.AuthError, firstsignin.FirstSignInError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    u = next((x for x in auth._load_users() if x["username"] == name), None)
    if not u:
        raise HTTPException(status_code=400, detail="That account no longer exists.")
    manager.write_log(f"[AUTH] {name} chose a password with the first sign-in link, from {ip}")
    return auth.read_session_token(_set_session_cookie(request, response, {"u": name, "src": "panel",
                                                                           "v": u.get("session_version", 1)}))

# --- 1.24: passkeys (Windows Hello) on Windows panels (core/passkeys.py) ---
def _pk_site(request: Request):
    if not passkeys.supported():
        raise HTTPException(status_code=404, detail="Passkeys are for panels on Windows.")
    try:
        return passkeys.site(request.url.scheme, request.url.hostname, request.url.port)
    except passkeys.PasskeyError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/auth/passkeys/available")
def passkeys_available_api(request: Request):
    """Whether the sign-in page offers "Sign in with a passkey" (Windows panels, at a name, over HTTPS)."""
    if not passkeys.supported():
        return {"supported": False}
    try:
        passkeys.site(request.url.scheme, request.url.hostname, request.url.port)
    except passkeys.PasskeyError as e:
        return {"supported": True, "usable": False, "why": str(e)}
    return {"supported": True, "usable": True}

@app.post("/api/auth/passkey/options")
def passkey_login_options_api(request: Request):
    rp_id, origin = _pk_site(request)
    ip = request.client.host if request.client else "unknown"
    if auth._throttled((f"ip:{ip}",)):
        raise HTTPException(status_code=429, detail="Too many failed sign-in attempts. Try again in a few minutes.")
    try:
        return passkeys.login_options(rp_id, origin)
    except passkeys.PasskeyError as e:
        raise HTTPException(status_code=400, detail=str(e))

class PasskeyLoginRequest(BaseModel):
    id: str
    client_data: str
    authenticator_data: str
    signature: str
    user_handle: Optional[str] = None
    remember: bool = False

@app.post("/api/auth/passkey/login")
def passkey_login_api(req: PasskeyLoginRequest, request: Request, response: Response):
    rp_id, origin = _pk_site(request)
    ip = request.client.host if request.client else "unknown"
    keys = (f"ip:{ip}",)
    if auth._throttled(keys):
        raise HTTPException(status_code=429, detail="Too many failed sign-in attempts. Try again in a few minutes.")
    try:
        key = passkeys.authenticate(req.id, req.client_data, req.authenticator_data, req.signature,
                                    req.user_handle, rp_id, origin)
        identity = auth.passkey_identity(key)
    except (passkeys.PasskeyError, auth.AuthError) as e:
        auth._record_fail(keys)
        manager.write_log(f"[AUTH] Failed passkey sign-in from {ip}: {e}")
        raise HTTPException(status_code=401, detail=str(e))
    manager.write_log(f"[AUTH] {identity['u']} ({identity['src']}) signed in from {ip} with a passkey"
                      f"{_remembered(req.remember)}")
    return auth.read_session_token(_set_session_cookie(request, response, identity, req.remember))

# --- Remember this device (1.24): the signed-in account's remembered devices ---
def _dev_account(request: Request):
    user = request.state.user
    if user["source"] not in ("panel", "linux"):
        raise HTTPException(status_code=400, detail="Sign in with your account to see its remembered devices.")
    return user, twofactor.account_key(user["source"], user["username"])

@app.get("/api/auth/devices")
def devices_list_api(request: Request):
    user, key = _dev_account(request)
    here = user.get("device")
    return {"days": devices.REMEMBER_DAYS,
            "devices": [dict(d, current=d["id"] == here) for d in reversed(devices.list_for(key))]}

@app.delete("/api/auth/devices/{device_id}")
def device_forget_api(device_id: str, request: Request):
    user, key = _dev_account(request)
    if not devices.remove(key, device_id):
        raise HTTPException(status_code=404, detail="That device isn't remembered.")
    manager.write_log(f"[AUTH] {user['username']} ({user['source']}) forgot a remembered device")
    return {"status": "success"}

@app.delete("/api/auth/devices")
def devices_forget_all_api(request: Request):
    """Forgets every remembered device but this one (sign out here to forget it too)."""
    user, key = _dev_account(request)
    n = devices.remove_all(key, keep=user.get("device"))
    if n:
        manager.write_log(f"[AUTH] {user['username']} ({user['source']}) forgot {n} remembered device(s)")
    return {"forgotten": n}

def _pk_account(request: Request):
    user = request.state.user
    if user["source"] not in ("panel", "linux"):
        raise HTTPException(status_code=400, detail="Sign in with your account to add a passkey.")
    return user, twofactor.account_key(user["source"], user["username"])

@app.get("/api/auth/passkeys")
def passkeys_list_api(request: Request):
    user, key = _pk_account(request)
    out = {"supported": passkeys.supported(), "passkeys": passkeys.list_for(key) if passkeys.supported() else []}
    if passkeys.supported():
        try:
            passkeys.site(request.url.scheme, request.url.hostname, request.url.port)
            out["usable"] = True
        except passkeys.PasskeyError as e:
            out.update(usable=False, why=str(e))
    return out

class PasskeyAddOptionsRequest(BaseModel):
    password: str

@app.post("/api/auth/passkeys/options")
def passkey_add_options_api(req: PasskeyAddOptionsRequest, request: Request):
    """Adding a passkey asks for the account's password again: a passkey outlives the session."""
    rp_id, origin = _pk_site(request)
    user, key = _pk_account(request)
    ip = request.client.host if request.client else "unknown"
    keys = (f"user:{user['username'].lower()}", f"ip:{ip}")
    if auth._throttled(keys):
        raise HTTPException(status_code=429, detail="Too many wrong passwords. Try again in a few minutes.")
    if not auth.check_account_password(user["source"], user["username"], req.password):
        auth._record_fail(keys)
        raise HTTPException(status_code=400, detail="That password is wrong.")
    try:
        return passkeys.registration_options(key, user["username"], user["username"], rp_id, origin)
    except passkeys.PasskeyError as e:
        raise HTTPException(status_code=400, detail=str(e))

class PasskeyAddRequest(BaseModel):
    client_data: str
    attestation_object: str
    name: Optional[str] = ""

@app.post("/api/auth/passkeys")
def passkey_add_api(req: PasskeyAddRequest, request: Request):
    rp_id, origin = _pk_site(request)
    user, key = _pk_account(request)
    try:
        pk = passkeys.register(key, req.client_data, req.attestation_object, rp_id, origin, req.name)
    except passkeys.PasskeyError as e:
        raise HTTPException(status_code=400, detail=str(e))
    manager.write_log(f"[AUTH] {user['username']} ({user['source']}) added a passkey ({pk['name']}) for {rp_id}")
    return pk

@app.delete("/api/auth/passkeys/{cred_id}")
def passkey_remove_api(cred_id: str, request: Request):
    user, key = _pk_account(request)
    if not passkeys.remove(key, cred_id):
        raise HTTPException(status_code=404, detail="No such passkey.")
    manager.write_log(f"[AUTH] {user['username']} ({user['source']}) removed a passkey")
    return {"status": "success"}

@app.get("/api/auth/me")
def me_api(request: Request):
    user = dict(request.state.user)
    # The release this panel runs; signed-in users only (the sign-in page doesn't advertise it)
    user["panel_version"] = panel_version()
    # Bedrock Dedicated Server is x86-64 only: the New Server form hides it on ARM64 (a Raspberry Pi)
    user["machine"] = platform.machine()
    user["bedrock_supported"] = manager.bedrock_supported()
    # Which system the panel runs on (Windows since 1.22; everything works there since 1.24, except
    # the terminal, which is Linux-only by design)
    user["system"] = "windows" if paths.WINDOWS else "linux"
    # Admins get told when HTTPS was requested but the panel had to fall back to HTTP
    if user["role"] == "admin":
        user["https_error"] = panel_settings.RUNTIME.get("https_error")
    st = cluster.state()
    user["cluster"] = {"role": st["role"], "name": st.get("name") or st.get("cluster_name"),
                       "master_name": st.get("master_name")}
    # 1.18: a `user` account's per-server permissions ({server key: [perms]}, "*" = every server)
    user.pop("perms", None)
    if user["role"] != "admin":
        user["grants"] = (grants.get(user["username"]) if user["source"] == "panel"
                          else (user.get("grants") or {}) if user["source"] == "token"
                          else {grants.ALL_SERVERS: list(grants.LEGACY)})
    user.pop("token_id", None)
    return user

# --- 1.18: SSH keys for SFTP (each account's own; Password > SSH keys) ---
class NewSshKey(BaseModel):
    key: str
    label: Optional[str] = ""

@app.get("/api/auth/ssh-keys")
def ssh_keys_api(request: Request):
    _, key = _tf_account(request)
    return {"keys": sshkeys.list_keys(key), "max": sshkeys.MAX_KEYS}

@app.post("/api/auth/ssh-keys")
def add_ssh_key_api(req: NewSshKey, request: Request):
    user, key = _tf_account(request)
    try:
        added = sshkeys.add(key, req.key, req.label)
    except sshkeys.KeyError_ as e:
        raise HTTPException(status_code=400, detail=str(e))
    manager.write_log(f"[AUTH] {user['username']} ({user['source']}) added an SSH key for SFTP ({added['fingerprint']})")
    return {"status": "success", "key": added, "keys": sshkeys.list_keys(key)}

@app.delete("/api/auth/ssh-keys/{fingerprint}")
def remove_ssh_key_api(fingerprint: str, request: Request):
    user, key = _tf_account(request)
    if not sshkeys.remove(key, fingerprint):
        raise HTTPException(status_code=404, detail="No such key.")
    manager.write_log(f"[AUTH] {user['username']} ({user['source']}) removed an SSH key for SFTP ({fingerprint})")
    return {"status": "success", "keys": sshkeys.list_keys(key)}

# --- 1.18: SFTP (core/sftp.py). Anyone signed in sees whether it's on and the port, to connect;
# admins switch it under Panel Settings > SFTP.
def _sftp_view(request: Request):
    user = request.state.user
    st = sftp.status()
    out = {"enabled": st["enabled"], "running": st["running"], "port": st["port"], "host_key": st["host_key"],
           "account": user["username"] if user.get("source") in ("panel", "linux") else None,
           "two_factor": bool(user.get("two_factor"))}
    if user["role"] == "admin":
        out.update(available=st["available"], package=st["package"])
    return out

@app.get("/api/sftp")
def sftp_api(request: Request):
    return _sftp_view(request)

class SftpSettings(BaseModel):
    enabled: bool
    port: int = 2022

@app.put("/api/settings/sftp")
def set_sftp_api(req: SftpSettings, request: Request):
    old = panel_settings.load()
    try:
        s = panel_settings.validate(dict(old, sftp_enabled=req.enabled, sftp_port=req.port))
        sftp.check_can_enable(s, old)
    except (panel_settings.SettingsError, sftp.SftpError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    panel_settings.save(s)
    sftp.apply(s, old, actor=request.state.user["username"])
    return _sftp_view(request)

# --- Panel Settings: where new game servers are kept (1.21.1; admins only) ---
def _folder_view(folder, current):
    servers = sum(1 for n in (os.listdir(folder) if os.path.isdir(folder) else [])
                  if n.startswith("minecraft-") and os.path.isdir(os.path.join(folder, n)))
    there = folder
    while there and not os.path.isdir(there):
        there = os.path.dirname(there) if os.path.dirname(there) != there else ""
    try:
        usage = shutil.disk_usage(there) if there else None
    except OSError:
        usage = None
    return {"folder": folder, "current": folder == current, "servers": servers,
            "free": usage.free if usage else None, "total": usage.total if usage else None}

def _servers_folder_view():
    current = manager.servers_root()
    from core.platform import system
    return {"folder": current, "default": manager.SERVERS_ROOT, "is_default": current == manager.SERVERS_ROOT,
            "folders": [_folder_view(r, current) for r in manager.server_roots()],
            "drives": system.servers_drives()}        # Windows: drives to choose from; [] where paths are typed

@app.get("/api/settings/servers-folder")
def get_servers_folder_api():
    return _servers_folder_view()

class ServersFolder(BaseModel):
    folder: str = ""

@app.put("/api/settings/servers-folder")
def set_servers_folder_api(req: ServersFolder, request: Request):
    old = panel_settings.load()
    before = manager.servers_root()
    try:
        s = panel_settings.validate(dict(old, servers_folder=req.folder,
                                         servers_folders_used=list(old["servers_folders_used"]) + [before]))
    except panel_settings.SettingsError as e:
        raise HTTPException(status_code=400, detail=str(e))
    after = s["servers_folder"] or manager.SERVERS_ROOT
    if after != before:
        from core.platform import system
        error = system.prepare_servers_folder(after)
        if error:
            raise HTTPException(status_code=400, detail=error)
        panel_settings.save(s)
        manager.write_log(f"[SETTINGS] {request.state.user['username']} changed where new servers are kept: "
                          f"{after} (was {before}). Servers already made stay where they are.")
    return _servers_folder_view()

# --- Panel Settings: connection (HTTP/HTTPS) and sign-in mode (admins only) ---
def _restart_soon():
    """systemd (Restart=always) starts the panel again with the new settings."""
    threading.Timer(1.0, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()

def _settings_view(request: Request):
    from core import tls
    s = panel_settings.load()
    return {
        "settings": s,
        "defaults": {"trusted_networks": panel_settings.DEFAULT_TRUSTED_NETWORKS,
                     "panel_name": panel_settings.host_name()},
        "runtime": dict(panel_settings.RUNTIME),
        "managed_restart": panel_settings.RUNTIME.get("scheme") is not None,
        "self_signed": tls.cert_info(tls.CA_CRT) if os.path.exists(tls.META) else None,
        "your_ip": request.client.host if request.client else None,
    }

@app.get("/api/settings")
def get_settings_api(request: Request):
    return _settings_view(request)

@app.put("/api/settings")
def update_settings_api(request: Request, new: dict = Body(...)):
    old = panel_settings.load()
    # SFTP has its own switch (PUT /api/settings/sftp); this form never changes it
    # ... and neither does it move the servers folder (PUT /api/settings/servers-folder)
    new = dict(new, sftp_enabled=old["sftp_enabled"], sftp_port=old["sftp_port"],
               servers_folder=old["servers_folder"], servers_folders_used=old["servers_folders_used"],
               # ... or the join pages (PUT /api/settings/join)
               join_enabled=old["join_enabled"], join_port=old["join_port"], join_index=old["join_index"],
               # ... or the terminal (PUT /api/settings/terminal)
               **{k: old[k] for k in panel_settings.TERMINAL_KEYS})
    try:
        s = panel_settings.validate(new)
    except panel_settings.SettingsError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # In a cluster, HTTPS and its port belong to the cluster (the master reaches nodes there)
    role = cluster.role()
    in_cluster = role != "standalone"
    # A master may switch between cluster HTTPS and its own certificate (certbot or uploaded):
    # browsers only talk to the master, and nodes never call it after joining.
    master_switch = role == "master" and {s["https_mode"], old["https_mode"]} <= {"cluster", "custom"}
    if in_cluster and (s["https_port"] != old["https_port"]
                       or (s["https_mode"] != old["https_mode"] and not master_switch)):
        raise HTTPException(status_code=400, detail="This panel is in a cluster, so its HTTPS port can't change, and its "
                                                    "HTTPS mode only between the cluster certificate and your own "
                                                    "certificate on the master. Leave the cluster first "
                                                    "(Panel Settings > Cluster).")
    if not in_cluster and s["https_mode"] == "cluster" and old["https_mode"] != "cluster":
        raise HTTPException(status_code=400, detail="Create or join a cluster to use cluster HTTPS.")

    restart = any(s[k] != old[k] for k in panel_settings.CONNECTION_KEYS)
    if restart:
        if not panel_settings.RUNTIME.get("scheme"):
            raise HTTPException(status_code=400, detail="The panel was not started by run.py, so it can't switch "
                                                        "HTTP/HTTPS by itself. Update its systemd service first.")
        # Ports the panel isn't already using must be free, or the restart would fail
        in_use = {panel_settings.RUNTIME["port"]}
        if old["https_mode"] != "off":
            in_use.add(old["http_port"])
        wanted = [s["http_port"]] + ([s["https_port"]] if s["https_mode"] != "off" else [])
        for port in wanted:
            if port not in in_use and not panel_settings.port_free(port):
                raise HTTPException(status_code=400, detail=f"Port {port} is already in use on this server.")
        if s["https_mode"] == "self_signed":
            from core import tls
            try:
                tls.ensure_self_signed()
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Could not create the certificate: {e}")

    if s["require_2fa_admins"] and not old["require_2fa_admins"] and not request.state.user.get("two_factor"):
        raise HTTPException(status_code=400, detail="Set up two-factor sign-in for your own account first "
                                                    "(Password button), so this doesn't lock you out.")
    panel_settings.save(s)
    if restart:                                      # Windows: the panel's firewall rule follows its ports
        panel_settings.sync_panel_firewall(s)
    me = request.state.user["username"]
    manager.write_log(f"[SETTINGS] {me} changed panel settings: HTTPS={s['https_mode']}, "
                      f"ports {s['http_port']}/{s['https_port']}, sign-in={s['login_mode']}, "
                      f"2FA for admins={'required' if s['require_2fa_admins'] else 'optional'}, "
                      f"name={panel_settings.display_name(s)!r}")
    url = panel_settings.panel_url(s, request.headers.get("host"))
    if restart:
        manager.write_log(f"[SETTINGS] Restarting the panel; new address: {url}")
        _restart_soon()
    return {"status": "success", "restart": restart, "url": url, "name": panel_settings.display_name(s)}

@app.get("/api/settings/ca.crt")
def download_ca_api():
    from core import tls
    if not os.path.exists(tls.CA_CRT):
        raise HTTPException(status_code=404, detail="No self-signed certificate has been created yet.")
    return FileResponse(tls.CA_CRT, filename="redstonepanel-ca.crt", media_type="application/x-x509-ca-cert")

class PasswordChange(BaseModel):
    current_password: str
    new_password: str

@app.post("/api/auth/password")
def change_password_api(req: PasswordChange, request: Request, response: Response):
    user = request.state.user
    if user["source"] != "panel":
        raise HTTPException(status_code=400, detail=("Windows accounts change their password in Windows." if paths.WINDOWS
                                                    else "Linux accounts change their password on the server (passwd)."))
    try:
        auth.change_own_password(user["username"], req.current_password, req.new_password)
    except auth.AuthError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # The change signs out all sessions, so issue a fresh cookie for this browser
    u = next(x for x in auth._load_users() if x["username"] == user["username"])
    _set_session_cookie(request, response, {"u": u["username"], "src": "panel", "v": u["session_version"],
                                            "m": 1 if user.get("two_factor") or user.get("passkey") else 0,
                                            **({"pk": 1} if user.get("passkey") else {})})
    return {"status": "success"}

# --- Panel user management (admins only; enforced in the middleware) ---
class NewPanelUser(BaseModel):
    username: str
    password: str
    role: str = "user"

class PanelUserUpdate(BaseModel):
    password: Optional[str] = None
    role: Optional[str] = None
    disabled: Optional[bool] = None

@app.get("/api/users")
def list_users_api():
    users = auth.list_panel_users()
    for u in users:
        u["grants"] = grants.get(u["username"])
    return {"users": users, "linux_groups": list(auth.ALLOWED_LINUX_GROUPS),
            "perms": [{"id": p, "label": grants.PERM_LABELS[p]} for p in grants.PERMS],
            "presets": {k: list(v) for k, v in grants.PRESETS.items()}}

class GrantsUpdate(BaseModel):
    grants: dict

@app.put("/api/users/{username}/grants")
def set_user_grants_api(username: str, req: GrantsUpdate, request: Request):
    """What a `user` account may do on each server (1.18). {server key: [perms]}; "*" = every server."""
    if not any(u["username"] == username for u in auth._load_users()):
        raise HTTPException(status_code=404, detail=f"Panel user '{username}' not found.")
    try:
        saved = grants.set_user(username, req.grants)
    except grants.GrantError as e:
        raise HTTPException(status_code=400, detail=str(e))
    summary = "; ".join(f"{'every server' if k == grants.ALL_SERVERS else k}: {', '.join(v)}"
                        for k, v in sorted(saved.items())) or "nothing"
    manager.write_log(f"[AUTH] {request.state.user['username']} set what '{username}' may do: {summary}")
    return {"status": "success", "grants": saved}

@app.post("/api/users")
def create_user_api(req: NewPanelUser, request: Request):
    try:
        auth.create_panel_user(req.username, req.password, req.role)
    except auth.AuthError as e:
        raise HTTPException(status_code=400, detail=str(e))
    manager.write_log(f"[AUTH] {request.state.user['username']} created panel user '{req.username.strip().lower()}' ({req.role})")
    return {"status": "success"}

@app.patch("/api/users/{username}")
def update_user_api(username: str, req: PanelUserUpdate, request: Request):
    me = request.state.user
    if me["source"] == "panel" and me["username"] == username and (req.disabled or req.role == "user"):
        raise HTTPException(status_code=400, detail="You can't disable or demote your own account.")
    try:
        auth.update_panel_user(username, password=req.password, role=req.role, disabled=req.disabled)
    except auth.AuthError as e:
        raise HTTPException(status_code=400, detail=str(e))
    changed = [k for k, v in (("password", req.password), ("role", req.role), ("disabled", req.disabled)) if v is not None]
    manager.write_log(f"[AUTH] {me['username']} updated panel user '{username}': {', '.join(changed)}")
    return {"status": "success"}

@app.post("/api/users/{username}/reset-2fa")
def reset_user_2fa_api(username: str, request: Request):
    """An admin turns off another panel account's two-factor (lost phone). Linux accounts: reset-2fa."""
    me = request.state.user
    if me["source"] == "panel" and me["username"] == username:
        raise HTTPException(status_code=400, detail="Use the Password button to change your own two-factor sign-in.")
    if not any(u["username"] == username for u in auth._load_users()):
        raise HTTPException(status_code=404, detail=f"Panel user '{username}' not found.")
    had_tf = twofactor.remove(twofactor.account_key("panel", username))
    had_pk = passkeys.remove_all(twofactor.account_key("panel", username))
    if not had_tf and not had_pk:
        raise HTTPException(status_code=400, detail=f"'{username}' doesn't have two-factor sign-in or passkeys set up.")
    manager.write_log(f"[AUTH] {me['username']} reset two-factor sign-in" + (" and passkeys" if had_pk else "")
                      + f" for panel user '{username}'")
    return {"status": "success"}

@app.delete("/api/users/{username}")
def delete_user_api(username: str, request: Request):
    me = request.state.user
    if me["source"] == "panel" and me["username"] == username:
        raise HTTPException(status_code=400, detail="You can't delete your own account.")
    try:
        auth.delete_panel_user(username)
    except auth.AuthError as e:
        raise HTTPException(status_code=400, detail=str(e))
    grants.remove_user(username)
    sshkeys.remove_account(sshkeys.account_key("panel", username))
    manager.write_log(f"[AUTH] {me['username']} deleted panel user '{username}'")
    return {"status": "success"}

@app.get("/")
def index():
    return RedirectResponse(url="/static/index.html")

@app.get("/api/servers")
def get_servers(request: Request):
    return _visible_servers(request.state.user, manager.list_servers())

# --- The dashboard (no server selected): this machine and what the panel relies on.
# Trusted-LAN visitors get it without addresses, versions or paths; the cluster master reads it too.
@app.get("/api/host")
def host_api(request: Request):
    user = request.state.user
    rep = host.report(redacted=user.get("source") == "trusted")
    return host.for_user(rep, _can_view(user)) if grants.limited(user) else rep

# --- 1.17: performance, history, memory and hard limits (core/perf.py)
class ResourcesUpdate(BaseModel):
    xms: Optional[str] = None
    xmx: Optional[str] = None
    memory: Optional[str] = None          # hard memory limit: "off", "auto" or a size like "3G"
    cpu: Optional[float] = None           # CPU limit in cores; 0 = none
    restart: bool = False

def _perf_call(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except perf.PerfError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except manager.ServerError as e:
        raise HTTPException(status_code=404, detail=str(e))

@app.get("/api/host/history")
def host_history(range: str = "day"):
    return _perf_call(perf.host_history, range)

@app.get("/api/servers/{server_name}/perf")
def server_perf(request: Request, server_name: str, range: str = "day"):
    out = _perf_call(perf.server_perf, server_name, range)
    if request.state.user.get("source") == "trusted":
        out.pop("address", None)
    return out

@app.get("/api/servers/{server_name}/resources")
def get_resources(server_name: str):
    return _perf_call(perf.resources, server_name)

@app.put("/api/servers/{server_name}/resources")
def set_resources(request: Request, server_name: str, req: ResourcesUpdate):
    _require_admin(request, "change a server's memory or limits")
    return _perf_call(perf.set_resources, server_name, xms=req.xms, xmx=req.xmx, memory=req.memory, cpu=req.cpu,
                      restart=req.restart, actor=request.state.user["username"])

# --- 1.25: Start at boot (core/bootstart.py)
class BootUpdate(BaseModel):
    enabled: bool

def _boot_server(server_name):
    try:
        manager._check_server(server_name)
    except manager.ServerError as e:
        raise HTTPException(status_code=404, detail=str(e))

@app.get("/api/servers/{server_name}/boot")
def get_boot(server_name: str):
    _boot_server(server_name)
    return {"enabled": bootstart.enabled(server_name)}

@app.put("/api/servers/{server_name}/boot")
def set_boot(request: Request, server_name: str, req: BootUpdate):
    _boot_server(server_name)
    on = bootstart.turn(server_name, req.enabled)
    manager.write_log(f"[SERVICE] {request.state.user['username']} turned Start at boot "
                      f"{'on' if req.enabled else 'off'} for {server_name}.")
    if on != req.enabled:
        raise HTTPException(status_code=500, detail=f"The system still says Start at boot is {'on' if on else 'off'} "
                                                    f"for {server_name}; see the action console.")
    return {"enabled": on}

# --- 1.17.1: why did it crash? (core/crashes.py)
class CrashFix(BaseModel):
    file: str

def _crash_call(fn, *args, **kw):
    try:
        manager._check_server(args[0])
        return fn(*args, **kw)
    except crashes.CrashError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except manager.ServerError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except files.FileError as e:
        raise HTTPException(status_code=e.status, detail=str(e))

@app.get("/api/servers/{server_name}/crash")
def last_crash(server_name: str):
    return {"crash": _crash_call(crashes.get, server_name)}

@app.delete("/api/servers/{server_name}/crash")
def dismiss_crash(request: Request, server_name: str):
    return _crash_call(crashes.dismiss, server_name)

@app.post("/api/servers/{server_name}/crash/turn-off")
def crash_turn_off(request: Request, server_name: str, req: CrashFix):
    _require_admin(request, "turn off mods or plugins")
    return _crash_call(crashes.turn_off_and_restart, server_name, req.file, actor=request.state.user["username"])

@app.post("/api/servers/{server_name}/crash/undo")
def crash_undo(request: Request, server_name: str):
    _require_admin(request, "undo mod or plugin updates")
    return _crash_call(crashes.undo_and_restart, server_name, actor=request.state.user["username"])

@app.get("/api/servers/{server_name}/properties")
def get_properties(server_name: str):
    return manager.read_properties(manager.get_server_dir(server_name))

@app.get("/api/servers/{server_name}/permissions/raw")
def get_raw_permissions(server_name: str):
    # Now passes server_name instead of the directory path
    return {"content": manager.read_permissions(server_name)}

@app.post("/api/servers/{server_name}/permissions/raw")
def update_raw_permissions(server_name: str, update: RawPropertiesUpdate):
    # Now passes server_name instead of the directory path
    manager.write_permissions(server_name, update.content)
    return {"status": "success"}

@app.post("/api/servers/{server_name}/properties")
def update_properties(server_name: str, update: ServerPropertiesUpdate):
    before = manager.server_firewall_rules(server_name)
    manager.write_properties(manager.get_server_dir(server_name), update.properties)
    manager.properties_changed(server_name, before)
    return {"status": "success"}

@app.get("/api/servers/{server_name}/properties/raw")
def get_raw_properties(server_name: str):
    return {"content": manager.read_raw_properties(manager.get_server_dir(server_name))}

@app.post("/api/servers/{server_name}/properties/raw")
def update_raw_properties(server_name: str, update: RawPropertiesUpdate):
    before = manager.server_firewall_rules(server_name)
    manager.write_raw_properties(manager.get_server_dir(server_name), update.content)
    manager.properties_changed(server_name, before)
    return {"status": "success"}

@app.get("/api/servers/{server_name}/players")
def get_players(server_name: str):
    # {"players": [...], "roles": [...], "file": "ops.json" | "permissions.json"}
    return manager.get_recent_players(server_name)

@app.post("/api/servers/{server_name}/players/role")
def set_player_role(server_name: str, player: str = Body(...), role: str = Body(...), player_id: str = Body(None)):
    try:
        manager.set_player_role(server_name, player, role, player_id)
    except manager.PlayerError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "success", "message": f"Set {player} to {role}."}

# Player access log and whitelist (Configuration → Whitelist & Access)
def _known_server(server_name):
    try:
        manager._check_server(server_name)
    except manager.ServerError as e:
        raise HTTPException(status_code=404, detail=str(e))

class WhitelistChange(BaseModel):
    player: str

@app.get("/api/servers/{server_name}/players/log")
def player_log(server_name: str, player: str = "", event: str = "", days: int = 7, limit: int = 500):
    _known_server(server_name)
    if event not in ("", "join", "leave", "refused"):
        raise HTTPException(status_code=400, detail="Unknown event type.")
    days = max(1, min(int(days), playerlog.RETENTION_DAYS))
    limit = max(1, min(int(limit), 2000))
    return {"events": playerlog.history(server_name, player, event or None, days, limit),
            "reasons": playerlog.REASONS, "retention_days": playerlog.RETENTION_DAYS}

@app.get("/api/servers/{server_name}/whitelist")
def get_whitelist(server_name: str):
    _known_server(server_name)
    data = whitelist.info(server_name)
    data["turned_away"] = [{"name": n, "count": c, "reason": r}
                           for n, c, r in playerlog.turned_away(server_name, [p["name"] for p in data["players"]],
                                                                hours=24 * 7)]
    return data

@app.post("/api/servers/{server_name}/whitelist/add")
def whitelist_add(request: Request, server_name: str, change: WhitelistChange):
    _known_server(server_name)
    try:
        result = whitelist.add(server_name, change.player)
    except whitelist.WhitelistError as e:
        raise HTTPException(status_code=400, detail=str(e))
    manager.write_log(f"[PLAYERS] {request.state.user['username']} allowed {change.player.strip()} on {server_name}.")
    return result

@app.post("/api/servers/{server_name}/whitelist/remove")
def whitelist_remove(request: Request, server_name: str, change: WhitelistChange):
    _known_server(server_name)
    try:
        result = whitelist.remove(server_name, change.player)
    except whitelist.WhitelistError as e:
        raise HTTPException(status_code=400, detail=str(e))
    manager.write_log(f"[PLAYERS] {request.state.user['username']} removed {change.player.strip()} "
                      f"from {server_name}'s whitelist.")
    return result

# Kick, ban and unban (1.16.2): anyone who may manage the server, like the console.
# Bans are Java only (Bedrock has no ban list); by name and UUID, never IP address.
class ModerationAction(BaseModel):
    player: str
    reason: Optional[str] = ""

def _moderate(request: Request, server_name: str, what: str, fn, *args):
    _known_server(server_name)
    try:
        result = fn(server_name, *args)
    except (moderation.ModerationError, whitelist.WhitelistError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    manager.write_log(f"[PLAYERS] {request.state.user['username']} {what} on {server_name}.")
    return result

@app.get("/api/servers/{server_name}/moderation")
def get_moderation(server_name: str):
    _known_server(server_name)
    return moderation.info(server_name)

@app.post("/api/servers/{server_name}/players/kick")
def kick_player(request: Request, server_name: str, req: ModerationAction):
    reason = moderation._clean_reason(req.reason)
    return _moderate(request, server_name, f"kicked {req.player.strip()}" + (f" ({reason})" if reason else ""),
                     moderation.kick, req.player, reason)

@app.post("/api/servers/{server_name}/players/ban")
def ban_player(request: Request, server_name: str, req: ModerationAction):
    reason = moderation._clean_reason(req.reason)
    return _moderate(request, server_name, f"banned {req.player.strip()}" + (f" ({reason})" if reason else ""),
                     moderation.ban, req.player, reason, request.state.user["username"])

@app.post("/api/servers/{server_name}/players/unban")
def unban_player(request: Request, server_name: str, req: ModerationAction):
    return _moderate(request, server_name, f"unbanned {req.player.strip()}", moderation.unban, req.player)

@app.post("/api/servers/{server_name}/update")
def update_server(request: Request, server_name: str, req: Optional[ServerUpdateRequest] = None):
    try:
        manager.update_server_binary(server_name, req.version if req else None,
                                     backup=req.backup if req else True, actor=request.state.user["username"])
    except manager.JavaMissing:
        raise                                    # 409, see java_missing() below
    except (manager.DependencyError, manager.ServerError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "success"}

# 1.19: change a Java server's type (Vanilla, Paper, Fabric) in place, undo it, and roll back
# the last update (core/convert.py). Reading is for everyone signed in; changing is for admins.
class ConvertRequest(BaseModel):
    to: str
    minecraft: Optional[str] = None
    confirm: Optional[str] = None            # the server's name, when mods added things to the world
    install: list[str] = []                  # Modrinth projects to install for the new type

def _convert_call(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except convert.ConvertError as e:
        raise HTTPException(status_code=e.status, detail=str(e))
    except (files.FileError, backups.BackupError, manager.ServerError) as e:
        raise HTTPException(status_code=getattr(e, "status", 400), detail=str(e))

@app.get("/api/servers/{server_name}/convert")
def convert_status(request: Request, server_name: str, to: str = "", minecraft: str = ""):
    """Without ?to=: what the server runs, the change running now or last, and what can be undone.
    With ?to=paper (and &minecraft=): the preview of that change, which changes nothing."""
    if not to:
        return _convert_call(convert.status, server_name)
    _require_admin(request, "change a server's type")
    return _convert_call(convert.plan, server_name, to, minecraft or None)

@app.post("/api/servers/{server_name}/convert")
def convert_server(request: Request, server_name: str, req: ConvertRequest):
    _require_admin(request, "change a server's type")
    return {"job": _convert_call(convert.start_change, server_name, req.to, req.minecraft or None,
                                 confirm=req.confirm, install=req.install[:20],
                                 actor=request.state.user["username"])}

@app.post("/api/servers/{server_name}/convert/undo")
def convert_undo(request: Request, server_name: str):
    _require_admin(request, "undo a type change")
    return {"job": _convert_call(convert.start_undo, server_name, "convert", actor=request.state.user["username"])}

@app.post("/api/servers/{server_name}/update/rollback")
def update_rollback(request: Request, server_name: str):
    _require_admin(request, "roll back an update")
    return {"job": _convert_call(convert.start_undo, server_name, "update", actor=request.state.user["username"])}

# Java server software: which Minecraft versions each loader offers, and the Java one needs
@app.get("/api/java/versions")
def java_versions(loader: str = "vanilla"):
    try:
        versions = javaservers.list_versions(loader)
    except javaservers.JavaServerError as e:
        raise HTTPException(status_code=502 if "reach" in str(e) or "HTTP" in str(e) else 400, detail=str(e))
    return {"loader": loader, "versions": versions[:150], "loaders": javaservers.LOADERS}

@app.get("/api/java/snapshots")
def java_snapshots():
    """Vanilla snapshots and pre-releases of the current development cycle (Advanced options)."""
    try:
        return {"versions": javaservers.list_snapshots()}
    except javaservers.JavaServerError as e:
        raise HTTPException(status_code=502, detail=str(e))

@app.get("/api/java/builds")
def java_builds(loader: str, version: str = ""):
    """Paper builds of a version, or Fabric loader versions, to pin one (Advanced options)."""
    try:
        return {"loader": loader, "builds": javaservers.list_builds(loader, version)}
    except javaservers.JavaServerError as e:
        raise HTTPException(status_code=502 if "reach" in str(e) or "HTTP" in str(e) else 400, detail=str(e))

@app.get("/api/java/requirement")
def java_requirement(version: str):
    if not javaservers.SNAPSHOT_RE.match(version or ""):
        raise HTTPException(status_code=400, detail="Choose a Minecraft release such as 1.21.4.")
    try:
        return javaservers.requirement(version)
    except javaservers.JavaServerError as e:
        raise HTTPException(status_code=400, detail=str(e))

# Java runtimes (admins): which are installed and used by which server, install one a server
# needs, remove one no server uses, and choose the Java a server runs on
@app.exception_handler(manager.JavaMissing)
async def java_missing(request: Request, e: manager.JavaMissing):
    """A server needs a Java that isn't installed: 409 with what to install, so an admin's
    browser can offer to install it and try again."""
    return JSONResponse({"detail": str(e), "java": {"required": e.required, "major": e.major,
                                                         "package": e.package}},
                        status_code=409)

@app.exception_handler(PermissionError)
async def permission_refused(request: Request, e: PermissionError):
    """The panel works in server folders as the game account (1.21.8): a file there that the game
    account can't open, such as a link the server or a plugin made to a system file, is left alone."""
    name = os.path.basename(getattr(e, "filename", "") or "") or "a file"
    manager.write_log(f"[SECURITY] Refused {request.method} {request.url.path}: {e}")
    return JSONResponse({"detail": f"The panel couldn't open {name} with the game account's rights, so it left "
                                   f"it alone. Check that the file belongs to the minecraft account and "
                                   f"isn't a link to somewhere outside the server folder."},
                        status_code=403)

class JavaChoice(BaseModel):
    major: int

def _runtime_call(request: Request, fn, *args, **kw):
    _require_admin(request, "install, remove or choose Java")
    try:
        return fn(*args, **kw)
    except runtimes.RuntimeJobError as e:
        raise HTTPException(status_code=e.status, detail=str(e))
    except manager.ServerError as e:
        raise HTTPException(status_code=404, detail=str(e))

@app.get("/api/java/runtimes")
def java_runtimes(request: Request):
    return _runtime_call(request, runtimes.status)

@app.get("/api/java/runtimes/job")
def java_runtime_job(request: Request):
    return _runtime_call(request, lambda: {"job": runtimes.job()})

@app.post("/api/java/runtimes/install")
def install_java_runtime(request: Request, req: JavaChoice):
    return _runtime_call(request, runtimes.install, req.major, actor=request.state.user["username"])

@app.post("/api/java/runtimes/remove")
def remove_java_runtime(request: Request, req: JavaChoice):
    return _runtime_call(request, runtimes.remove, req.major, actor=request.state.user["username"])

@app.post("/api/servers/{server_name}/java")
def choose_server_java(request: Request, server_name: str, req: JavaChoice):
    return _runtime_call(request, runtimes.use, server_name, req.major, actor=request.state.user["username"])

class JvmFlags(BaseModel):
    preset: str = "none"
    extra: list[str] = []

@app.post("/api/servers/{server_name}/jvm-flags")
def set_jvm_flags(request: Request, server_name: str, req: JvmFlags):
    """Startup flags (Aikar's, or extra -XX/-D/-Xss flags) of a Java server, admins only (1.17.2)."""
    _require_admin(request, "change startup flags")
    try:
        return manager.set_server_flags(server_name, {"preset": req.preset, "extra": req.extra},
                                        actor=request.state.user["username"])
    except manager.ServerError as e:
        raise HTTPException(status_code=400, detail=str(e))

# Phantom LAN proxies for Bedrock servers (admins): see core/phantom.py
class PhantomUnit(BaseModel):
    unit: str | None = None

def _phantom_call(request: Request, fn, *args, **kw):
    _require_admin(request, "manage Phantom")
    try:
        return fn(*args, **kw)
    except phantom.PhantomError as e:
        raise HTTPException(status_code=e.status, detail=str(e))
    except files.FileError as e:          # download failures (core/sources.py)
        raise HTTPException(status_code=getattr(e, "status", 502), detail=str(e))
    except manager.ServerError as e:
        raise HTTPException(status_code=404, detail=str(e))

@app.get("/api/servers/{server_name}/phantom")
def phantom_status(request: Request, server_name: str):
    return _phantom_call(request, phantom.status, server_name)

@app.post("/api/servers/{server_name}/phantom/install")
def phantom_install(request: Request, server_name: str):
    return _phantom_call(request, phantom.install, server_name, actor=request.state.user["username"])

@app.post("/api/servers/{server_name}/phantom/{action}")
def phantom_control(request: Request, server_name: str, action: str, req: PhantomUnit):
    return _phantom_call(request, phantom.control, server_name, req.unit or phantom.unit_name(server_name),
                         action, actor=request.state.user["username"])

@app.post("/api/servers/{server_name}/phantom-remove")
def phantom_remove(request: Request, server_name: str, req: PhantomUnit):
    return _phantom_call(request, phantom.uninstall, server_name, req.unit, actor=request.state.user["username"])

# Updating RedstonePanel itself (admins): see core/selfupdate.py for why it runs in its own unit
def _update_call(request: Request, fn, *args, **kw):
    _require_admin(request, "update RedstonePanel")
    try:
        return fn(*args, **kw)
    except selfupdate.UpdateError as e:
        raise HTTPException(status_code=e.status, detail=str(e))

@app.get("/api/panel/update")
def panel_update_status(request: Request):
    return _update_call(request, selfupdate.status)

@app.post("/api/panel/update/check")
def check_panel_update(request: Request):
    return _update_call(request, selfupdate.check)

@app.post("/api/panel/update")
def start_panel_update(request: Request):
    return _update_call(request, selfupdate.start, actor=request.state.user["username"])

@app.post("/api/servers/{server_name}/action")
def control_server(server_name: str, req: ServerActionRequest):
    try:
        manager.control_server(server_name, req.action)
    except manager.ServerError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "success"}

@app.delete("/api/servers/{server_name}")
def uninstall_server(request: Request, server_name: str, keep_world: bool = True):
    # Takes the server off the panel (or erases it): admins only, never a trusted-LAN visitor
    if request.state.user["role"] != "admin":
        detail = ("Sign in as an admin to uninstall a server." if request.state.user.get("source") == "trusted"
                  else "Only admins can uninstall a server.")
        raise HTTPException(status_code=403, detail=detail)
    if not keep_world and request.state.user.get("source") == "cluster":
        raise HTTPException(status_code=403, detail="The cluster master can uninstall a server here but not erase its "
                                                    "files: they stay as a removed server you can restore.")
    try:
        manager._check_server(server_name)
    except manager.ServerError as e:
        raise HTTPException(status_code=400, detail=str(e))
    manager.write_log(f"[REMOVE] {request.state.user['username']} is "
                      f"{'uninstalling (keeping its files)' if keep_world else 'purging'} {server_name}.")
    try:
        manager.remove_server(server_name, keep_world)
    except manager.ServerError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not keep_world:
        playerlog.forget(server_name)
        joinpage.forget("local", server_name)
        playerpack.forget(server_name)
        grants.forget_server(server_name)
    return {"status": "success"}

# Backups: /var/backups/redstonepanel/<server>/, scheduled by a systemd timer per server
class BackupSchedule(BaseModel):
    enabled: bool
    frequency: str
    time: str
    keep: int
    keep_days: int = 0

def _backup_call(fn, *args):
    try:
        return fn(*args)
    except manager.ServerError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except backups.BackupError as e:
        raise HTTPException(status_code=400, detail=str(e))

def _require_admin(request: Request, what: str):
    if request.state.user["role"] != "admin":
        raise HTTPException(status_code=403, detail=f"Only admins can {what}.")

@app.get("/api/servers/{server_name}/backups")
def get_backups(server_name: str):
    _backup_call(manager._check_server, server_name)
    return _backup_call(backups.overview, server_name)

@app.put("/api/servers/{server_name}/backups/schedule")
def set_backup_schedule(server_name: str, req: BackupSchedule):
    _backup_call(manager._check_server, server_name)
    return _backup_call(backups.set_schedule, server_name, req.enabled, req.frequency, req.time, req.keep,
                        req.keep_days)

@app.post("/api/servers/{server_name}/backups")
def backup_now(server_name: str):
    _backup_call(manager._check_server, server_name)
    info = _backup_call(backups.run_backup, server_name, "manual")
    info["offsite"] = backup_targets.sync_soon(server_name)     # 1.16.11: copy it to backup storage now
    return info

@app.get("/api/servers/{server_name}/backups/{filename}")
def download_backup(server_name: str, filename: str):
    path = _backup_call(backups.backup_path, server_name, filename)
    return FileResponse(path, filename=filename, media_type="application/gzip")

@app.get("/api/servers/{server_name}/backups/{filename}/sha256")
def backup_sha256_api(server_name: str, filename: str):
    """1.16.8: the master checks each copy it sends to another cluster member against this."""
    try:
        return {"sha256": backup_copies.backup_sha256(server_name, filename)}
    except backup_copies.CopyError as e:
        raise HTTPException(status_code=e.status, detail=str(e))

@app.post("/api/servers/{server_name}/backups/{filename}/restore")
def restore_backup(request: Request, server_name: str, filename: str):
    _require_admin(request, "restore a backup")
    _backup_call(manager._check_server, server_name)
    return _backup_call(backups.restore_backup, server_name, filename)

@app.delete("/api/servers/{server_name}/backups/{filename}")
def delete_backup(request: Request, server_name: str, filename: str):
    _require_admin(request, "delete a backup")
    _backup_call(backups.delete_backup, server_name, filename)
    return {"status": "success"}

# --- 1.16.11: copies on SSH or S3-compatible backup storage (core/backup_targets.py)
# A server's Backups tab (on a node too, through the master's proxy: each panel uses its own storage)
def _offsite_call(fn, *args, **kwargs):
    from core import archive
    try:
        return fn(*args, **kwargs)
    except backup_targets.TargetError as e:
        raise HTTPException(status_code=e.status, detail=str(e))
    except manager.ServerError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except (backups.BackupError, archive.ArchiveError, manager.DependencyError) as e:
        raise HTTPException(status_code=400, detail=str(e))

class OffsiteToggle(BaseModel):
    enabled: bool

class OffsiteFetch(BaseModel):
    file: str
    panel: Optional[str] = None
    server: Optional[str] = None

@app.get("/api/servers/{server_name}/offsite")
def offsite_view_api(server_name: str):
    return _offsite_call(backup_targets.view, server_name)

@app.post("/api/servers/{server_name}/offsite/sync")
def offsite_sync_api(server_name: str):
    _offsite_call(manager._check_server, server_name)
    if not backup_targets.sync_soon(server_name):
        raise HTTPException(status_code=400, detail="No backup storage keeps copies of this server's backups.")
    return {"status": "started"}

@app.put("/api/servers/{server_name}/offsite/{target_id}")
def offsite_toggle_api(server_name: str, target_id: str, req: OffsiteToggle, request: Request):
    _require_admin(request, "choose where backups are copied")
    _offsite_call(manager._check_server, server_name)
    _offsite_call(backup_targets.set_server, target_id, server_name, req.enabled, request.state.user["username"])
    return _offsite_call(backup_targets.view, server_name, list_copies=False)

@app.post("/api/servers/{server_name}/offsite/{target_id}/fetch")
def offsite_fetch_api(server_name: str, target_id: str, req: OffsiteFetch, request: Request):
    _require_admin(request, "restore from backup storage")
    return _offsite_call(backup_targets.fetch, target_id, server_name, req.file, request.state.user["username"],
                         req.panel, req.server)

@app.delete("/api/servers/{server_name}/offsite/{target_id}/{filename}")
def offsite_delete_api(server_name: str, target_id: str, filename: str, request: Request):
    _require_admin(request, "delete a copy on backup storage")
    return _offsite_call(backup_targets.delete_copy, target_id, server_name, filename, request.state.user["username"])

# Panel Settings > Backup storage (admins only: everything under /api/settings is)
def _storage_view():
    try:
        key = backup_targets.public_key()
    except backup_targets.TargetError:
        key = None
    data = backup_targets.load()
    return {"targets": [backup_targets.public({**t, "id": tid}) for tid, t in sorted(data["targets"].items())],
            "panel_id": backup_targets.panel_id(), "public_key": key,
            "tools": backup_targets.tools(),
            "servers": [s["name"] for s in manager.list_servers()]}

@app.get("/api/settings/backup-storage")
def storage_view_api():
    return _storage_view()

@app.post("/api/settings/backup-storage")
def storage_add_api(request: Request, req: dict = Body(...)):
    _offsite_call(backup_targets.save_target, None, req, request.state.user["username"])
    return _storage_view()

@app.post("/api/settings/backup-storage/test")
def storage_test_api(req: dict = Body(...)):
    tid = req.get("id") if isinstance(req.get("id"), str) else None
    fields = req.get("fields") if isinstance(req.get("fields"), dict) else None
    return _offsite_call(backup_targets.test_target, tid, fields)

@app.put("/api/settings/backup-storage/{target_id}")
def storage_change_api(target_id: str, request: Request, req: dict = Body(...)):
    _offsite_call(backup_targets.save_target, target_id, req, request.state.user["username"])
    return _storage_view()

@app.delete("/api/settings/backup-storage/{target_id}")
def storage_remove_api(target_id: str, request: Request):
    _offsite_call(backup_targets.remove_target, target_id, request.state.user["username"])
    return _storage_view()

@app.post("/api/settings/backup-storage/{target_id}/forget-host-key")
def storage_forget_key_api(target_id: str, request: Request):
    _offsite_call(backup_targets.forget_host_key, target_id, request.state.user["username"])
    return _storage_view()

@app.get("/api/settings/backup-storage/{target_id}/browse")
def storage_browse_api(target_id: str):
    return _offsite_call(backup_targets.browse, target_id)

@app.post("/api/settings/backup-storage/{target_id}/import")
def storage_import_api(target_id: str, request: Request, req: dict = Body(...)):
    """Import > From backup storage: a new server here from a copy (the old machine is gone)."""
    return _offsite_call(backup_targets.import_copy, target_id, str(req.get("panel") or ""),
                         str(req.get("server") or ""), str(req.get("file") or ""), request.state.user["username"])

# Scheduled restarts: a systemd timer per server, warning players first
class RestartSchedule(BaseModel):
    enabled: bool
    frequency: str
    time: str
    warn: int

def _restart_call(fn, *args):
    try:
        return fn(*args)
    except manager.ServerError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except (restarts.RestartError, backups.BackupError) as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/servers/{server_name}/restarts")
def get_restarts(server_name: str):
    _restart_call(manager._check_server, server_name)
    return restarts.overview(server_name)

@app.put("/api/servers/{server_name}/restarts")
def set_restarts(server_name: str, req: RestartSchedule):
    _restart_call(manager._check_server, server_name)
    _restart_call(restarts.set_schedule, server_name, req.enabled, req.frequency, req.time, req.warn)
    return restarts.overview(server_name)

# Update alerts and automatic Bedrock updates (1.16.12, core/updates.py)
class AutoUpdateSetting(BaseModel):
    enabled: bool

def _alerts_call(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except manager.ServerError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except updates.UpdateError as e:
        raise HTTPException(status_code=400, detail=str(e))

def _actor(request):
    return ((getattr(request.state, "user", None) or {}).get("username")) or "?"

@app.get("/api/servers/{server_name}/updates")
def get_updates(server_name: str):
    _alerts_call(manager._check_server, server_name)
    return _alerts_call(updates.overview, server_name)

@app.put("/api/servers/{server_name}/updates")
def set_auto_update(request: Request, server_name: str, req: AutoUpdateSetting):
    _alerts_call(manager._check_server, server_name)
    return _alerts_call(updates.set_auto, server_name, req.enabled, actor=_actor(request))

@app.post("/api/servers/{server_name}/updates/check")
def check_updates_now(server_name: str):
    _alerts_call(manager._check_server, server_name)
    return _alerts_call(updates.check_one, server_name)

# Webhook notifications (admins: under /api/settings). The URL is never sent back in full.
class NotificationSettings(BaseModel):
    url: Optional[str] = None
    events: Optional[list] = None
    clear: bool = False

@app.get("/api/settings/notifications")
def get_notifications():
    return notify.public()

@app.put("/api/settings/notifications")
def set_notifications(request: Request, req: NotificationSettings):
    try:
        result = notify.save(req.url, req.events, req.clear)
    except notify.NotifyError as e:
        raise HTTPException(status_code=400, detail=str(e))
    manager.write_log(f"[SETTINGS] {request.state.user['username']} changed notifications: "
                      + (f"{result['kind']} webhook, events {', '.join(result['events']) or 'none'}."
                         if result["configured"] else "no webhook."))
    return result

@app.post("/api/settings/notifications/test")
def test_notifications():
    ok, err = notify.send_test()
    if not ok:
        raise HTTPException(status_code=400, detail=err)
    return {"status": "success"}

# The first-run setup guide on the dashboard (admins: under /api/settings). Steps are worked out
# from the panel's state; only "dismissed" and "keep HTTP" are stored.
class SetupAnswers(BaseModel):
    dismissed: Optional[bool] = None
    http_ok: Optional[bool] = None

@app.get("/api/settings/setup")
def get_setup():
    return setup_guide.status()

@app.put("/api/settings/setup")
def set_setup(request: Request, req: SetupAnswers):
    setup_guide.save_state(dismissed=req.dismissed, http_ok=req.http_ok)
    me = request.state.user["username"]
    if req.dismissed is not None:
        manager.write_log(f"[SETTINGS] {me} {'closed' if req.dismissed else 'reopened'} the setup guide.")
    if req.http_ok is not None:
        manager.write_log(f"[SETTINGS] {me} {'chose to keep' if req.http_ok else 'no longer keeps'} plain HTTP.")
    return setup_guide.status()

# File manager (admins only): everything goes through core/files.py, which never follows symlinks
class TextFile(BaseModel):
    path: str
    content: str
    modified: Optional[int] = None      # mtime the editor loaded; None for a new file

class FilePath(BaseModel):
    path: str

class FileRename(BaseModel):
    path: str
    name: str

def _files_call(request: Request, fn, *args, **kw):
    # Who may: the Files grant for the file manager, Mods & plugins for add-ons. The middleware
    # checks the exact one for the route (core/grants.py); this is the backstop behind it.
    user = request.state.user
    if user["role"] != "admin" and not grants.perms_for(user, str(args[0]) if args else "") & {"files", "mods"}:
        raise HTTPException(status_code=403, detail="Only admins, and accounts an admin gave Files or Mods & "
                                                    "plugins on this server, can do this.")
    try:
        return fn(*args, **kw)
    except files.FileError as e:
        raise HTTPException(status_code=e.status, detail=str(e))
    except manager.ServerError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except OSError as e:
        raise HTTPException(status_code=400, detail=f"{e.strerror or e}")

@app.get("/api/servers/{server_name}/files")
def list_files(request: Request, server_name: str, path: str = ""):
    return _files_call(request, files.list_dir, server_name, path)

@app.get("/api/servers/{server_name}/files/text")
def read_text_file(request: Request, server_name: str, path: str):
    return _files_call(request, files.read_text, server_name, path)

@app.put("/api/servers/{server_name}/files/text")
def write_text_file(request: Request, server_name: str, req: TextFile):
    return _files_call(request, files.write_text, server_name, req.path, req.content, req.modified,
                       actor=request.state.user["username"])

@app.get("/api/servers/{server_name}/files/download")
def download_file(request: Request, server_name: str, path: str):
    fd, size, name = _files_call(request, files.open_for_download, server_name, path)
    def stream():
        try:
            while True:
                chunk = os.read(fd, 1 << 16)
                if not chunk:
                    break
                yield chunk
        finally:
            os.close(fd)
    quoted = urllib.parse.quote(name, safe="")
    return StreamingResponse(stream(), media_type="application/octet-stream", headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{quoted}", "Content-Length": str(size)})

@app.post("/api/servers/{server_name}/files/upload")
def upload_file(request: Request, server_name: str, path: str = "", overwrite: bool = False,
                file: UploadFile = File(...)):
    def chunks():
        while True:
            chunk = file.file.read(1 << 20)
            if not chunk:
                break
            yield chunk
    return _files_call(request, files.upload, server_name, path, os.path.basename(file.filename or ""),
                       chunks(), overwrite, actor=request.state.user["username"])

# --- Mods, plugins and Bedrock add-ons (admins, like the file manager)
class AddonToggle(BaseModel):
    item: str
    enabled: bool

@app.get("/api/servers/{server_name}/addons")
def list_addons(request: Request, server_name: str):
    return _files_call(request, mods.list_installed, server_name)

@app.post("/api/servers/{server_name}/addons")
def install_addon(request: Request, server_name: str, overwrite: bool = False, file: UploadFile = File(...)):
    return _files_call(request, mods.install, server_name, file.filename or "", file.file, overwrite,
                       actor=request.state.user["username"])

@app.post("/api/servers/{server_name}/addons/toggle")
def toggle_addon(request: Request, server_name: str, req: AddonToggle):
    return _files_call(request, mods.set_enabled, server_name, req.item, req.enabled,
                       actor=request.state.user["username"])

@app.delete("/api/servers/{server_name}/addons")
def remove_addon(request: Request, server_name: str, item: str):
    return _files_call(request, mods.remove, server_name, item, actor=request.state.user["username"])

# --- Modrinth and Hangar: search, install with dependencies, updates (admins)
class CatalogInstall(BaseModel):
    source: str
    project: str

class CatalogBundle(BaseModel):
    bundle: str
    backup: bool = False

class CatalogUpdate(BaseModel):
    files: list[str] = []
    backup: bool = False

def _catalog_call(request: Request, fn, *args, **kw):
    try:
        return _files_call(request, fn, *args, **kw)
    except backups.BackupError as e:
        raise HTTPException(status_code=400, detail=f"The world backup before updating failed, so nothing "
                                                    f"was changed: {e}")

@app.get("/api/servers/{server_name}/addons/search")
def search_addons(request: Request, server_name: str, source: str = "modrinth", q: str = "", offset: int = 0,
                  category: str = "", sort: str = ""):
    return _catalog_call(request, catalog.search, server_name, source, q, offset, category, sort)

@app.get("/api/servers/{server_name}/addons/catalog")
def addon_catalog(request: Request, server_name: str):
    return _catalog_call(request, catalog.home, server_name)

@app.get("/api/servers/{server_name}/addons/details")
def addon_details(request: Request, server_name: str, source: str, project: str):
    return _catalog_call(request, catalog.details, server_name, source, project)

@app.post("/api/servers/{server_name}/addons/bundle")
def install_addon_bundle(request: Request, server_name: str, req: CatalogBundle):
    return _catalog_call(request, catalog.install_bundle, server_name, req.bundle, req.backup,
                         actor=request.state.user["username"])

@app.post("/api/servers/{server_name}/addons/install")
def install_from_catalog(request: Request, server_name: str, req: CatalogInstall):
    return _catalog_call(request, catalog.install, server_name, req.source, req.project,
                         actor=request.state.user["username"])

@app.get("/api/servers/{server_name}/addons/updates")
def addon_updates(request: Request, server_name: str):
    return _catalog_call(request, catalog.updates, server_name)

@app.post("/api/servers/{server_name}/addons/update")
def update_addons(request: Request, server_name: str, req: CatalogUpdate):
    return _catalog_call(request, catalog.update, server_name, req.files, req.backup,
                         actor=request.state.user["username"])

@app.post("/api/servers/{server_name}/addons/undo")
def undo_addon_update(request: Request, server_name: str):
    return _catalog_call(request, catalog.undo, server_name, actor=request.state.user["username"])

@app.get("/api/servers/{server_name}/addons/compatibility")
def addon_compatibility(request: Request, server_name: str, minecraft: str):
    if not javaservers.RELEASE_RE.match(minecraft or ""):
        raise HTTPException(status_code=400, detail="Choose a Minecraft release such as 1.21.4.")
    return _catalog_call(request, catalog.compatibility, server_name, minecraft)

@app.post("/api/servers/{server_name}/files/mkdir")
def make_folder(request: Request, server_name: str, req: FilePath):
    _files_call(request, files.make_dir, server_name, req.path, actor=request.state.user["username"])
    return {"status": "success"}

@app.post("/api/servers/{server_name}/files/rename")
def rename_file(request: Request, server_name: str, req: FileRename):
    _files_call(request, files.rename, server_name, req.path, req.name, actor=request.state.user["username"])
    return {"status": "success"}

@app.delete("/api/servers/{server_name}/files")
def delete_file(request: Request, server_name: str, path: str):
    _files_call(request, files.delete, server_name, path, actor=request.state.user["username"])
    return {"status": "success"}

# Removed servers: folders in /opt whose systemd unit is gone (uninstalled with "Keep World")
@app.get("/api/servers/removed")
def list_removed_servers():
    return manager.list_removed_servers()

# Found servers: Minecraft servers running here that the panel doesn't manage (core/found.py).
# Looking changes nothing; admins only, because it shows folders and accounts of the machine.
@app.get("/api/found-servers")
def list_found_servers(request: Request):
    _require_admin(request, "see the Minecraft servers found on this machine")
    from core import found
    from core.platform import system
    programs = system.game_programs()
    return {"servers": found.listing(programs=programs), "taken_over": found.taken_over(),
            "noticed": found.noticed(programs)}

class FoundServer(BaseModel):
    folder: str

def _found_call(fn, *args, **kw):
    from core import found
    try:
        return fn(*args, **kw)
    except (found.FoundError, manager.ServerError) as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/found-servers/manage")
def manage_found_server(req: FoundServer, request: Request):
    """Hands a found server to the panel in place (its own service is stopped and turned off)."""
    _require_admin(request, "hand a server to the panel")
    from core import found
    return _found_call(found.manage, req.folder, actor=request.state.user["username"])

@app.post("/api/found-servers/{server_name}/undo")
def undo_found_server(server_name: str, request: Request):
    """Hands a taken-over server back to the service that ran it. Its folder is never deleted."""
    _require_admin(request, "hand a server back")
    from core import found
    return _found_call(found.undo, server_name, actor=request.state.user["username"])

# 1.23.2: Panel Settings > Remove RedstonePanel. Choose what happens to every game server of this
# machine (stop and keep files, keep running, delete after a backup), then remove the package.
class UninstallChoices(BaseModel):
    choices: dict
    confirm: str = ""
    delete_backups: bool = False

class UninstallRun(BaseModel):
    purge: bool = False

def _uninstall_call(fn, *args, **kw):
    from core import uninstall
    try:
        return fn(*args, **kw)
    except (uninstall.UninstallError, manager.ServerError) as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/uninstall")
def uninstall_overview(request: Request):
    _require_admin(request, "remove the panel")
    from core import uninstall
    return uninstall.overview()

@app.post("/api/uninstall/apply")
def uninstall_apply(req: UninstallChoices, request: Request):
    _require_admin(request, "remove the panel")
    from core import uninstall
    return _uninstall_call(uninstall.apply, req.choices, actor=request.state.user["username"],
                           confirm=req.confirm, delete_backups=req.delete_backups)

@app.delete("/api/uninstall/plan")
def uninstall_cancel(request: Request):
    _require_admin(request, "remove the panel")
    from core import uninstall
    uninstall.clear_plan()
    manager.write_log(f"[UNINSTALL] {request.state.user['username']} isn't removing the panel after all.")
    return {"ok": True}

@app.post("/api/uninstall/run")
def uninstall_run(req: UninstallRun, request: Request):
    _require_admin(request, "remove the panel")
    from core import uninstall
    return _uninstall_call(uninstall.run, purge=req.purge, actor=request.state.user["username"])

@app.post("/api/servers/removed/{server_name}/restore")
def restore_server(server_name: str):
    try:
        result = manager.restore_server(server_name)
    except manager.JavaMissing:
        raise                                    # 409, see java_missing() below
    except (manager.ServerError, manager.DependencyError) as e:
        manager.write_log(f"[RESTORE] Refused: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "success", **result}

@app.delete("/api/servers/removed/{server_name}")
def delete_removed_server(request: Request, server_name: str):
    # Can't be undone: admins only
    if request.state.user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Only admins can delete a removed server's files.")
    try:
        manager.delete_removed_server(server_name)
    except manager.ServerError as e:
        raise HTTPException(status_code=400, detail=str(e))
    grants.forget_server(server_name)
    return {"status": "success"}
    
@app.get("/api/logs")
def get_logs(request: Request):
    """Returns the most recent 100 lines of the live action console."""
    user = request.state.user
    if grants.limited(user):
        return {"content": _visible_log(user, manager.get_panel_logs(400))}
    return {"content": manager.get_panel_logs(100)}

# Update the deploy endpoint to log its output
@app.post("/api/servers/deploy")
def deploy_server(request: Request, req: ServerDeployRequest):
    # This executes the native python deployment and logs output to the web console
    user = request.state.user
    try:
        name = manager.deploy_server(req.edition, req.template_id, req.loader, req.version, advanced=req.advanced,
                                     is_admin=user.get("role") == "admin", actor=user.get("username"))
    except manager.JavaMissing:
        raise                                    # 409, see java_missing() below
    except (manager.TemplateError, manager.DependencyError, manager.ServerError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "success", "name": name}

# --- Modpacks (1.20): a server from a Modrinth modpack or an uploaded pack, and updating it.
# Admins only, like the file manager: a pack puts files into a server folder.
def _modpack_call(request: Request, fn, *args, **kw):
    _require_admin(request, "install or update modpacks")
    try:
        return fn(*args, **kw)
    except manager.JavaMissing:
        raise                                    # 409, see java_missing()
    except files.FileError as e:                 # PackError, SourceError
        manager.write_log(f"[MODPACK] Refused: {e}")
        raise HTTPException(status_code=e.status, detail=str(e))
    except backups.BackupError as e:
        raise HTTPException(status_code=400, detail=f"The backup before updating failed, so nothing was "
                                                    f"changed: {e}")
    except (manager.TemplateError, manager.DependencyError, manager.ServerError) as e:
        manager.write_log(f"[MODPACK] Refused: {e}")
        raise HTTPException(status_code=400, detail=str(e))

def _pack_upload(file: Optional[UploadFile]):
    """Saves an uploaded pack to a temp file (never named after the upload). Returns (path, name)."""
    if file is None:
        return None, None
    fd, tmp_path = tempfile.mkstemp(prefix="redstonepanel-pack-", suffix=".upload")
    size = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = file.file.read(1 << 20)
                if not chunk:
                    break
                size += len(chunk)
                if size > 4 * files.MAX_UPLOAD:
                    raise HTTPException(status_code=413, detail="That pack is over 2 GB.")
                out.write(chunk)
    except BaseException:
        os.remove(tmp_path)
        raise
    return tmp_path, os.path.basename(file.filename or "")

@app.get("/api/modpacks/search")
def search_modpacks(request: Request, q: str = "", offset: int = 0, loader: str = "", minecraft: str = "",
                    sort: str = ""):
    if minecraft and not javaservers.RELEASE_RE.match(minecraft):
        raise HTTPException(status_code=400, detail="Choose a Minecraft release such as 1.21.4.")
    return _modpack_call(request, modpacks.modrinth.search_modpacks, q[:100], offset, 20, loader or None,
                         minecraft or None, sort or None)

@app.get("/api/modpacks/versions")
def modpack_versions(request: Request, project: str):
    if not project or len(project) > 100:
        raise HTTPException(status_code=400, detail="Choose a modpack.")
    return _modpack_call(request, modpacks.modrinth.modpack_versions, project)

@app.post("/api/servers/modpack")
def create_modpack_server(request: Request, source: str = Form(...), file: Optional[UploadFile] = File(None),
                          project: str = Form(""), version_id: str = Form(""), name: str = Form(""),
                          port: str = Form(""), memory: str = Form(""), loader: str = Form(""),
                          minecraft: str = Form(""), loader_version: str = Form("")):
    """A new server from a modpack. source: modrinth (project, version_id), mrpack or serverpack
    (file). loader, minecraft and loader_version fill in what a server pack doesn't say."""
    _require_admin(request, "install or update modpacks")
    if source in ("mrpack", "serverpack") and file is None:
        raise HTTPException(status_code=400, detail="Choose the pack's file to upload.")
    path, filename = _pack_upload(file if source != "modrinth" else None)
    options = {"project": project.strip()[:100], "version_id": version_id.strip()[:40], "name": name.strip(),
               "port": port.strip(), "memory": memory.strip(), "loader": loader.strip().lower(),
               "minecraft": minecraft.strip(), "loader_version": loader_version.strip()}
    try:
        return {"status": "success", **_modpack_call(request, modpacks.create, source, path, filename, options,
                                                      actor=request.state.user["username"])}
    finally:
        if path and os.path.exists(path):
            os.remove(path)

@app.get("/api/servers/{server_name}/modpack")
def server_modpack(request: Request, server_name: str):
    try:
        return modpacks.status(server_name)
    except manager.ServerError as e:
        raise HTTPException(status_code=404, detail=str(e))

@app.post("/api/servers/{server_name}/modpack/check")
def check_modpack_update(request: Request, server_name: str):
    return _modpack_call(request, modpacks.check_update, server_name)

@app.post("/api/servers/{server_name}/modpack/update")
def update_modpack(request: Request, server_name: str, source: str = Form(...),
                   file: Optional[UploadFile] = File(None), version_id: str = Form(""),
                   backup: str = Form("true"), minecraft: str = Form(""), loader_version: str = Form("")):
    """Moves a modpack server to another version of its pack, after a backup (on unless backup=false)."""
    _require_admin(request, "install or update modpacks")
    if source in ("mrpack", "serverpack") and file is None:
        raise HTTPException(status_code=400, detail="Choose the pack's file to upload.")
    path, filename = _pack_upload(file if source != "modrinth" else None)
    options = {"version_id": version_id.strip()[:40], "minecraft": minecraft.strip(),
               "loader_version": loader_version.strip()}
    try:
        return {"status": "success", **_modpack_call(request, modpacks.update, server_name, source, path, filename,
                                                      options, backup=backup.lower() != "false",
                                                      actor=request.state.user["username"])}
    finally:
        if path and os.path.exists(path):
            os.remove(path)

def _send_and_clean(zip_path, filename):
    """Sends a temp zip and deletes its temp folder once the download is done."""
    return FileResponse(zip_path, filename=filename, media_type="application/zip",
                        background=BackgroundTask(shutil.rmtree, os.path.dirname(zip_path), ignore_errors=True))

@app.get("/api/servers/{server_name}/export")
def export_server_api(server_name: str):
    zip_path = manager.export_server(server_name)
    return _send_and_clean(zip_path, f"{server_name}_backup.zip")

@app.post("/api/servers/import")
def import_server_api(file: UploadFile = File(...)):
    # Never build a path from the uploaded file's name: it's chosen by the client.
    # The archive type (.zip / .tar.gz / .tar) is detected from the contents, not the name.
    from core import archive
    fd, tmp_path = tempfile.mkstemp(prefix="redstonepanel-import-", suffix=".upload")
    try:
        with os.fdopen(fd, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        name = manager.import_server(tmp_path)
    except (manager.DependencyError, archive.ArchiveError) as e:
        manager.write_log(f"[IMPORT] Refused: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    return {"status": "success", "server": name}

class TemplateUpdate(BaseModel):
    properties: dict
    permissions: str

@app.get("/api/templates")
def get_templates_api():
    return manager.load_templates()

@app.post("/api/servers/{server_name}/template/save")
def save_template_api(server_name: str, name: str = Body(..., embed=True)):
    manager.save_server_as_template(server_name, name)
    return {"status": "success"}

@app.post("/api/servers/{server_name}/template/apply")
def apply_template_api(server_name: str, template_id: str = Body(..., embed=True)):
    try:
        before = manager.server_firewall_rules(server_name)
        manager.apply_template_to_server(server_name, template_id)
        manager.properties_changed(server_name, before)
    except manager.TemplateError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "success"}

@app.put("/api/templates/{template_id}")
def update_template_api(template_id: str, update: TemplateUpdate):
    manager.update_template(template_id, update.properties, update.permissions)
    return {"status": "success"}
    
@app.delete("/api/templates/{template_id}")
def delete_template_api(template_id: str):
    manager.delete_template(template_id)
    return {"status": "success"}
    
class ChatMessage(BaseModel):
    message: str

@app.get("/api/servers/{server_name}/chat")
def get_server_chat(server_name: str):
    messages = manager.get_chat_history(server_name)
    return {"messages": messages}

@app.post("/api/servers/{server_name}/chat")
def send_server_chat(server_name: str, payload: ChatMessage):
    try:
        status = manager.send_chat_message(server_name, payload.message)
    except manager.ChatError as e:
        return {"status": "error", "message": str(e)}

    if status == "restarting":
        return {"status": "info", "message": "RCON was not set up (or was turned off, or its port clashed with another server), so the panel configured it. The server is restarting; send your message again in a moment."}
    elif status == "error":
        return {"status": "error", "message": "Failed to connect to RCON."}
        
    # 'status' now contains the raw output from the server (e.g., the player list)
    return {"status": "success", "response": status}
    
class RenameWorldRequest(BaseModel):
    new_name: str

@app.get("/api/servers/{server_name}/worlds/export")
def export_world_api(server_name: str):
    try:
        zip_path, world_name = manager.export_world(server_name)
        return _send_and_clean(zip_path, f"{server_name}_{world_name}.zip")
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

@app.post("/api/servers/{server_name}/worlds/rename")
def rename_world_api(server_name: str, req: RenameWorldRequest):
    try:
        manager.rename_world(server_name, req.new_name.strip())
    except manager.WorldError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "success"}

@app.get("/api/servers/{server_name}/chat/assist")
def server_chat_assist(server_name: str):
    """Live Chat's suggestions (1.21.4): who is online or was seen before, and a running Java
    server's own commands and game rules. Reads only; the command list itself is static/commands.json."""
    return chatassist.info(server_name)

@app.post("/api/servers/{server_name}/chat/setup")
def setup_bedrock_chat(server_name: str):
    """Bedrock only: installs the chat relay pack so player chat reaches server.log. Restart the server afterwards."""
    manager.install_bedrock_chat_pack(server_name)
    return {"status": "success"}

@app.post("/api/servers/{server_name}/chat/clear")
def clear_server_chat(server_name: str):
    manager.clear_chat(server_name)
    return {"status": "success"}

@app.get("/api/servers/{server_name}/chat/debug")
def debug_server_chat(server_name: str):
    return manager.get_chat_debug(server_name)



# ---------------------------------------------------------------------------
# 1.21.10: the terminal (core/terminal.py). A shell as a Linux account, for admins, on Linux.
# The browser gets a one-use ticket (a POST, so the cross-site checks above apply), then opens
# the websocket. Websockets don't pass through the HTTP middleware, so each one checks the
# session, the ticket and its Origin itself.
# ---------------------------------------------------------------------------
class _BrowserSocket:
    """A browser's websocket, in the shape core/terminal.py uses."""

    def __init__(self, ws):
        self.ws = ws

    async def recv(self):
        try:
            msg = await self.ws.receive()
        except Exception:                        # noqa: BLE001
            return ("closed", None)
        if msg.get("type") == "websocket.disconnect":
            return ("closed", None)
        if msg.get("bytes") is not None:
            return ("bytes", msg["bytes"])
        return ("text", msg.get("text") or "")

    async def send_bytes(self, data):
        await self.ws.send_bytes(bytes(data))

    async def send_text(self, text):
        await self.ws.send_text(text)

    async def close(self, code=1000):
        try:
            await self.ws.close(code)
        except Exception:                        # noqa: BLE001  (already closed)
            pass


def _secure(request):
    return request.url.scheme in ("https", "wss")

@app.get("/api/terminal")
def terminal_status_api(request: Request):
    return terminal.status(request.state.user, _secure(request), _client_ip(request))

@app.post("/api/terminal/ticket")
def terminal_ticket_api(request: Request, body: dict = Body(default={})):
    user = request.state.user
    node_id = body.get("node") if isinstance(body, dict) else None
    cookie = request.cookies.get(auth.SESSION_COOKIE)
    try:
        if node_id:
            node = _terminal_node(node_id)
            ticket = terminal.issue_ticket(user, cookie, _secure(request), _client_ip(request), node=node["id"])
        else:
            ticket = terminal.issue_ticket(user, cookie, _secure(request), _client_ip(request))
    except terminal.Refused as e:
        raise HTTPException(status_code=403, detail=str(e))
    return {"ticket": ticket}

@app.get("/api/terminal/sessions")
def terminal_sessions_api():
    return {"sessions": terminal.list_sessions()}

@app.delete("/api/terminal/sessions/{sid}")
def terminal_end_api(sid: str, request: Request):
    if not terminal.end_session(sid, request.state.user["username"]):
        raise HTTPException(status_code=404, detail="That terminal isn't open any more.")
    return {"status": "success"}

@app.put("/api/settings/terminal")
def terminal_settings_api(request: Request, body: dict = Body(...)):
    if not terminal.platform_terminal.supported():
        raise HTTPException(status_code=400, detail="This machine's system doesn't have the terminal yet.")
    changes = {k: body[k] for k in panel_settings.TERMINAL_KEYS if k in body and k != "terminal_enabled"}
    old = panel_settings.load()
    try:
        s = terminal.turn(body.get("terminal_enabled", old["terminal_enabled"]), request.state.user["username"],
                          **changes)
    except panel_settings.SettingsError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {k: s[k] for k in panel_settings.TERMINAL_KEYS}

def _terminal_node(node_id):
    """A node the master may open a terminal on, or HTTPException."""
    node = _proxy_node(node_id)
    if (node.get("cluster_api") or 1) < cluster.TERMINAL_API:
        raise HTTPException(status_code=400, detail=f"{node['name']} needs RedstonePanel 1.21.10 or later for a "
                                                    "terminal from the master.")
    try:
        fields = cluster_client._hello_fields(node)          # ask now: its settings may have changed
    except Exception:                                        # noqa: BLE001
        fields = {}
    t = fields.get("terminal")
    if not fields:
        raise HTTPException(status_code=502, detail=f"{node['name']} isn't answering.")
    cluster.update_node(node["id"], **fields)
    if not (t and t["supported"] and t["enabled"] and t["from_master"]):
        raise HTTPException(status_code=403, detail=f"{node['name']} doesn't allow a terminal from the master. Its "
                                                    "own admin can allow it under Panel Settings > Terminal.")
    return node

def _ws_ip(ws):
    return ws.client.host if ws.client else None

async def _ws_user(ws, node=None):
    """(user, cookie, ticket entry) for a browser's terminal websocket, or None after refusing it."""
    cookie = ws.cookies.get(auth.SESSION_COOKIE)
    user = await run_in_threadpool(auth.read_session_token, cookie) if cookie else None
    entry = terminal.take_ticket(ws.query_params.get("ticket"), cookie, node=node) if user else None
    ok = (user and entry and panel_settings.same_origin(ws.headers) is True and user["role"] == "admin"
          and not user.get("needs_2fa_setup") and entry["user"] == user["username"]
          and (_secure(ws) or terminal.is_local(_ws_ip(ws))) and not terminal.is_signed_out(cookie))
    if not ok:
        await ws.close(code=4403)
        return None
    return user, cookie, entry

def _still_allowed(cookie, local=True):
    def check():
        user = auth.read_session_token(cookie)
        if not user or user["role"] != "admin" or terminal.is_signed_out(cookie):
            return "you signed out, or aren't an admin any more"
        if local and not panel_settings.load()["terminal_enabled"]:
            return "the terminal was turned off"
        return None
    return check

@app.websocket("/api/terminal/ws")
async def terminal_ws(ws: WebSocket):
    got = await _ws_user(ws)
    if not got:
        return
    user, cookie, _entry = got
    if not terminal.status(user, True, None)["available"]:
        await ws.close(code=4403)
        return
    await ws.accept()
    linked = terminal.linked_account(user)
    who = {"panel_user": user["username"], "ip": _ws_ip(ws), "linked": linked, "cookie": cookie,
           "recent": bool(linked) and terminal.recent(cookie),
           "two_factor_key": twofactor.account_key(user["source"], user["username"]) if user.get("two_factor") else None,
           "still_allowed": _still_allowed(cookie)}
    await terminal.serve(_BrowserSocket(ws), who)

@app.websocket("/api/cluster/nodes/{node_id}/terminal/ws")
async def cluster_terminal_ws(ws: WebSocket, node_id: str):
    got = await _ws_user(ws, node=node_id)
    if not got:
        return
    user, cookie, _entry = got
    node = await run_in_threadpool(cluster.find_node, node_id)
    if not node or cluster.role() != "master":
        await ws.close(code=4404)
        return
    ip = _ws_ip(ws)
    try:
        link = await cluster_client.open_terminal(node, user["username"], ip)
    except cluster_client.NodeError as e:
        await ws.accept()
        await ws.send_bytes(f"{e}\r\n".encode())
        await ws.close(1000)
        return
    await ws.accept()
    started = time.time()
    await run_in_threadpool(manager.write_log, f"[TERMINAL] {user['username']} opened a terminal on node "
                                               f"{node['name']} from {ip}.")
    try:
        await terminal.relay(_BrowserSocket(ws), link, _still_allowed(cookie, local=False))
    finally:
        await run_in_threadpool(manager.write_log, f"[TERMINAL] {user['username']}'s terminal on node {node['name']} "
                                                   f"closed after {int((time.time() - started) // 60)} min.")

@app.websocket("/api/terminal/node-ws")
async def node_terminal_ws(ws: WebSocket):
    """A node: the master's terminal for one of its admins. The node checks the Linux password."""
    bearer = ws.headers.get("authorization", "")
    ip = _ws_ip(ws)
    master = await run_in_threadpool(cluster.read_node_token, bearer[7:].strip(),
                                     ws.headers.get("x-redstonepanel-epoch"), ip) \
        if bearer.lower().startswith("bearer ") else None
    s = panel_settings.load()
    actor = (ws.headers.get("x-redstonepanel-actor") or "?")[:64]
    client_ip = ws.headers.get("x-redstonepanel-client-ip") or ""
    if not terminal.valid_ip(client_ip):
        client_ip = ip
    if not (master and ws.headers.get("x-redstonepanel-role") == "admin" and terminal.platform_terminal.supported()
            and s["terminal_enabled"] and s["terminal_from_master"]):
        await ws.close(code=4403)
        return
    await ws.accept()

    def still_allowed():
        cur = panel_settings.load()
        return None if cur["terminal_enabled"] and cur["terminal_from_master"] else "the terminal was turned off"

    who = {"panel_user": f"{actor} (master {master.get('master', '?')})", "ip": client_ip, "linked": None,
           "via": f"through the master from {ip}", "still_allowed": still_allowed}
    await terminal.serve(_BrowserSocket(ws), who)

# ---------------------------------------------------------------------------
# Clusters (docs/design/fleet.md): one master panel controls other panels' game servers
# ---------------------------------------------------------------------------
def _client_ip(request: Request):
    return request.client.host if request.client else None

def _cluster_fail(e):
    status = 429 if isinstance(e, cluster.Throttled) else 400
    raise HTTPException(status_code=status, detail=str(e))

def _need_managed_restart():
    if not panel_settings.RUNTIME.get("scheme"):
        raise HTTPException(status_code=400, detail="The panel was not started by run.py, so it can't restart "
                                                    "itself with the new HTTPS settings.")

def _need_https_port_free():
    """Switching to cluster HTTPS starts listening on the HTTPS port (if not already)."""
    s = panel_settings.load()
    if s["https_port"] != panel_settings.RUNTIME.get("port") and not panel_settings.port_free(s["https_port"]):
        raise HTTPException(status_code=400, detail=f"HTTPS port {s['https_port']} is already in use on this server.")

def _restart_view(request: Request):
    url = panel_settings.panel_url(panel_settings.load(), request.headers.get("host"))
    manager.write_log(f"[CLUSTER] Restarting the panel; address: {url}")
    _restart_soon()
    return {"status": "success", "restart": True, "url": url}

# --- Public: the cluster CA certificate, and nodes presenting a join code (master)
@app.get("/api/cluster/ca")
def cluster_ca_api():
    pem = cluster.read_ca() if cluster.role() != "standalone" else None
    if not pem:
        raise HTTPException(status_code=404, detail="This panel isn't in a cluster.")
    return {"ca": pem, "sha256": cluster.ca_fingerprint(pem)}

@app.get("/api/cluster/ca.crt")
def cluster_ca_download_api():
    if cluster.role() == "standalone" or not os.path.exists(cluster.ca_crt()):
        raise HTTPException(status_code=404, detail="This panel isn't in a cluster.")
    return FileResponse(cluster.ca_crt(), filename="redstonepanel-cluster-ca.crt",
                        media_type="application/x-x509-ca-cert")

@app.post("/api/cluster/join")
def cluster_join_api(request: Request, body: dict = Body(...)):
    ip = _client_ip(request)
    try:
        resp, node = cluster.accept_join(body, ip)
    except cluster.ClusterError as e:
        manager.write_log(f"[CLUSTER] Refused a join from {ip}: {e}")
        _cluster_fail(e)
    manager.write_log(f"[CLUSTER] Node '{node['name']}' joined from {ip} as {node['id']} ({node['url']})")
    return resp

# --- Node side: only the master's node token reaches these (see the middleware)
@app.get("/api/cluster/hello")
def cluster_hello_api():
    return cluster.hello()

@app.delete("/api/cluster/membership")
def cluster_membership_api(request: Request):
    try:
        cluster.leave()
    except cluster.ClusterError as e:
        _cluster_fail(e)
    manager.write_log("[CLUSTER] The master removed this panel from the cluster; back to standalone.")
    return _restart_view(request)

@app.post("/api/cluster/cert/csr")
def cluster_cert_csr_api():
    try:
        return cluster.cert_csr()
    except cluster.ClusterError as e:
        _cluster_fail(e)

@app.put("/api/cluster/cert")
def cluster_cert_install_api(request: Request, body: dict = Body(...)):
    try:
        cluster.install_cert(body.get("cert"))
    except cluster.ClusterError as e:
        _cluster_fail(e)
    manager.write_log("[CLUSTER] Installed a renewed cluster certificate.")
    return _restart_view(request)

@app.post("/api/cluster/templates")
def cluster_receive_template_api(request: Request, body: dict = Body(...)):
    """The master pushes one of its templates before creating a server here with it."""
    try:
        t = cluster.clean_template(body.get("template"), request.state.user.get("master"))
    except cluster.ClusterError as e:
        _cluster_fail(e)
    templates = [x for x in manager.load_templates() if x.get("id") != t["id"]] + [t]
    manager.save_templates(templates)
    return {"status": "success", "id": t["id"]}

# --- 1.16.14: what to forward on the internet router, per server and per cluster member (admins)
def _ports_call(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except portforward.ForwardError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/port-forwarding")
def port_forwarding_api(request: Request):
    _require_admin(request, "see port forwarding")
    return portforward.overview(actor=request.state.user["username"])

@app.get("/api/port-forwarding/local")
def port_forwarding_local_api(request: Request):
    """This panel's own rows; the cluster master reads it from each node."""
    _require_admin(request, "see port forwarding")
    return portforward.local_report()

@app.put("/api/port-forwarding/settings")
def port_forwarding_settings_api(request: Request, body: dict = Body(...)):
    _require_admin(request, "change port forwarding")
    return _ports_call(portforward.save, str(body.get("public_address") or ""), request.state.user["username"])

@app.post("/api/port-forwarding/lookup")
def port_forwarding_lookup_api(request: Request):
    """Asks the router (NAT-PMP, then UPnP) for its internet address. Nothing else is asked."""
    _require_admin(request, "change port forwarding")
    return _ports_call(portforward.lookup_public_address)

@app.post("/api/port-forwarding/check")
def port_forwarding_check_api(request: Request):
    _require_admin(request, "check port forwarding")
    return _ports_call(portforward.check, portforward.overview(actor=request.state.user["username"]))

# --- 1.23.1: the Port check (core/portaudit.py): what holds each port the servers need
@app.get("/api/port-check")
def port_check_api(request: Request):
    """This panel's Port check. The cluster master's Port forwarding page reads each node's
    through /api/port-forwarding/local."""
    _require_admin(request, "see the port check")
    return portaudit.report(fresh=True)

@app.post("/api/port-check/router")
def port_check_router_api(request: Request):
    """Reads the router's port forwards over UPnP and compares them with the servers."""
    _require_admin(request, "check the router")
    return portaudit.router_check(actor=request.state.user["username"])

@app.post("/api/servers/{server_name}/ports/move")
def move_server_port_api(request: Request, server_name: str, body: dict = Body(default={})):
    """Moves a server to the next free game port (the Port check's Move button)."""
    _require_admin(request, "change a server's port")
    restart = body.get("restart") if isinstance(body, dict) else None
    try:
        return portaudit.move(server_name, actor=request.state.user["username"],
                              restart=None if restart is None else bool(restart))
    except manager.ServerError as e:
        raise HTTPException(status_code=400, detail=str(e))

# --- 1.21.5: join pages for players (core/joinpage.py), served on their own port by
# core/joinsite.py. Everything here is the admin's side; the public side has no /api at all.
def _join_call(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except joinpage.JoinError as e:
        raise HTTPException(status_code=400, detail=str(e))

def _join_base(request: Request, st=None):
    """Where players open the join pages: the address set for the reverse proxy (1.21.9), else
    the port-forwarding address with the join port. None when neither is known."""
    s = panel_settings.load()
    if s["join_url"]:
        return s["join_url"]
    st = st or joinsite.status(s)
    public = portforward.load()["public_address"]
    if not public:
        return None
    public = f"[{public}]" if ":" in public else public
    default = {"https": 443, "http": 80}[st["scheme"]]
    return f"{st['scheme']}://{public}" + ("" if st["port"] == default else f":{st['port']}")

def _join_view(request: Request):
    s = panel_settings.load()
    st = joinsite.status(s)
    out = joinpage.admin_overview()
    host = panel_settings.host_only(request.headers.get("host")) or "localhost"
    host = f"[{host}]" if ":" in host else host
    default = {"https": 443, "http": 80}[st["scheme"]]
    port = "" if st["port"] == default else f":{st['port']}"
    base = _join_base(request, st)
    return dict(out, listener=st, lan_url=f"{st['scheme']}://{host}{port}/join",
                public_url=f"{base}/join" if base else None,
                proxy={"addresses": s["join_proxies"], "only": s["join_proxy_only"], "url": s["join_url"],
                       "cloudflare": s["join_cloudflare"],
                       "haproxy": joinsite.haproxy_config(host, s), "cloudflared": joinsite.cloudflared_config(s)})

@app.get("/api/join-pages")
def join_pages_api(request: Request):
    _require_admin(request, "see the join pages")
    return _join_view(request)

class JoinSettings(BaseModel):
    enabled: bool
    port: int
    index: bool = False
    # 1.21.9: a reverse proxy in front of the pages. Left out = unchanged.
    proxies: Optional[Union[List[str], str]] = None
    proxy_only: Optional[bool] = None
    url: Optional[str] = None
    cloudflare: Optional[bool] = None

@app.put("/api/settings/join")
def set_join_settings_api(req: JoinSettings, request: Request):
    old = panel_settings.load()
    try:
        new = dict(old, join_enabled=req.enabled, join_port=req.port, join_index=req.index)
        if req.proxies is not None:
            new["join_proxies"] = req.proxies
        if req.proxy_only is not None:
            new["join_proxy_only"] = req.proxy_only
        if req.url is not None:
            new["join_url"] = req.url
        if req.cloudflare is not None:
            new["join_cloudflare"] = req.cloudflare
        s = panel_settings.validate(new)
        joinsite.check_can_enable(s, old)
    except (panel_settings.SettingsError, joinpage.JoinError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    panel_settings.save(s)
    joinsite.apply(s, old, actor=request.state.user["username"])
    return _join_view(request)

@app.put("/api/join-pages/page")
def set_join_page_api(request: Request, body: dict = Body(...)):
    """One server's page. member: "local", or a node's id on a cluster master."""
    _require_admin(request, "change a join page")
    member, server = str(body.get("member") or "local"), str(body.get("server") or "")
    if member == "local":
        _known_server(server)
    else:
        if cluster.role() != "master" or not cluster.find_node(member):
            raise HTTPException(status_code=404, detail="No such cluster member.")
        if not manager.SERVICE_NAME_RE.fullmatch(server):
            raise HTTPException(status_code=404, detail=f"No Minecraft server called '{server}'.")
    cfg = _join_call(joinpage.save_page, member, server, body, request.state.user["username"])
    return {"status": "success", "page": {k: v for k, v in cfg.items() if k != "token"}, "path": joinpage.path_of(cfg)}

@app.get("/api/servers/{server_name}/join-info")
def join_info_api(request: Request, server_name: str):
    """What this machine knows about a server for its join page; the cluster master reads it
    from a node. Admins only: it includes who is online."""
    _require_admin(request, "read a server's join details")
    _known_server(server_name)
    return _join_call(joinpage.local_summary, server_name)

# --- Player packs (1.21.6): the mods players need, worked out from the server's mods folder
#     (core/playerpack.py). The Mods tab shows and changes it; the join page hands it out.
def _player_pack_view(server_name: str, fresh: bool = False):
    view = playerpack.admin_view(server_name, fresh=fresh)
    cfg = joinpage.page(server_name)
    st = joinsite.status()
    view["join_page"] = {"enabled": bool(cfg["enabled"]), "site": bool(st["enabled"]),
                         "path": joinpage.path_of(cfg) if cfg["enabled"] else None,
                         "port": st["port"], "scheme": st["scheme"],
                         "url": panel_settings.load()["join_url"] or None}
    return view

@app.get("/api/servers/{server_name}/player-pack")
def player_pack_api(request: Request, server_name: str, fresh: bool = False):
    return _files_call(request, _player_pack_view, server_name, fresh)

@app.put("/api/servers/{server_name}/player-pack")
def set_player_pack_api(request: Request, server_name: str, body: dict = Body(...)):
    _files_call(request, playerpack.save_config, server_name, body, actor=request.state.user["username"])
    return _files_call(request, _player_pack_view, server_name)

@app.get("/api/servers/{server_name}/player-pack/file")
def player_pack_file_api(request: Request, server_name: str):
    """The .mrpack itself (also what a cluster master fetches for the jars copied into it)."""
    def build(name):
        p, data = playerpack.local_pack(name)
        if not p:
            raise playerpack.PackError("This server's player pack is off.", 404)
        return Response(content=data, media_type="application/x-modrinth-modpack+zip", headers={
            "Content-Disposition": f'attachment; filename="{playerpack.file_name(p["name"] or name, p["version"])}"'})
    return _files_call(request, build, server_name)

# --- Master: the cluster dashboard (signed-in users; trusted-LAN visitors are refused)
def _need_master():
    if cluster.role() != "master":
        raise HTTPException(status_code=404, detail="This panel isn't a cluster master.")

@app.get("/api/cluster/overview")
def cluster_overview_api(request: Request, fresh: bool = False):
    _need_master()
    ov = cluster_client.overview(manager.list_servers, fresh=fresh)
    user = request.state.user
    if user["role"] != "admin":
        ov = dict(ov, nodes=[dict(n, servers=_visible_servers(user, n.get("servers") or [],
                                                              None if n.get("local") else n["id"]))
                             for n in ov["nodes"]])
    return ov

@app.get("/api/cluster/hosts")
def cluster_hosts_api(request: Request):
    """Each panel's CPU, memory, disk and servers online, the master's own first."""
    _need_master()
    out = cluster_client.hosts(lambda: host.summary(host.report()), host.summary)
    user = request.state.user
    if user["role"] != "admin":           # only the problems of servers the account may see
        nodes = []
        for n in out["nodes"]:
            if n.get("summary"):
                seen = _can_view(user, None if n.get("local") else n["id"])
                n = dict(n, summary=dict(n["summary"], attention=[a for a in n["summary"].get("attention") or []
                                                                  if a.get("server") and seen(a["server"])]))
            nodes.append(n)
        out = dict(out, nodes=nodes)
    return out

@app.post("/api/cluster/nodes/{node_id}/servers/{server_name}/action")
def cluster_node_action_api(node_id: str, server_name: str, req: ServerActionRequest, request: Request):
    _need_master()
    me = request.state.user["username"]
    if node_id == "local":
        try:
            manager.control_server(server_name, req.action)
        except manager.ServerError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"status": "success"}
    node = cluster.find_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail="No such node.")
    if req.action not in manager.POWER_ACTIONS:
        raise HTTPException(status_code=400, detail=f"Unknown action '{req.action}'.")
    try:
        # Waits for the node's answer: a start on Windows returns once the service runs, a stop
        # or restart once the server has saved and stopped (1.24: the 4 s default timed out)
        cluster_client.call_node(node, "POST", f"/api/servers/{server_name}/action",
                                 {"action": req.action}, actor=me, timeout=cluster_client.ACTION_TIMEOUT)
    except cluster_client.NodeError as e:
        raise HTTPException(status_code=502, detail=f"{node['name']}: {e}")
    finally:
        cluster_client.forget_cached(node_id)
    manager.write_log(f"[CLUSTER] {me}@{panel_settings.display_name()}: {req.action} {server_name} on {node['name']}")
    return {"status": "success"}

def _proxy_node(node_id):
    _need_master()
    node = cluster.find_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail="No such node.")
    return node

def _node_too_old(node):
    if (node.get("cluster_api") or 1) >= cluster.PROXY_API:
        return False
    fields = cluster_client._hello_fields(node)        # it may have been updated since
    if fields:
        cluster.update_node(node["id"], **fields)
        node.update(fields)
    return (node.get("cluster_api") or 1) < cluster.PROXY_API

def _too_old_detail(node):
    return (f"{node['name']} runs RedstonePanel {node.get('version') or 'older than 1.15'}. Update it to 1.15 or "
            "later to manage its servers from the master.")

# The proxy: every tab of a node's server, from the master. The browser calls
# /api/cluster/nodes/{id}/proxy/api/servers/...; the master passes the request on with the node's
# token and the user's name and role, and streams the answer back (downloads included).
@app.api_route("/api/cluster/nodes/{node_id}/proxy/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def cluster_proxy_api(node_id: str, path: str, request: Request):
    node = await run_in_threadpool(_proxy_node, node_id)
    user = request.state.user
    target = "/" + path
    if not cluster.proxy_allowed(request.method, target):
        raise HTTPException(status_code=403, detail="That can't be done on a node from the master.")
    # 1.18: the route inside decides what a `user` account needs; grants for a node's servers
    # are kept here, on the master, and the node is told what this user may do on that server
    perms, cut = None, None
    if user["role"] != "admin":
        template, params = _route_of(request.method, target)
        rule = grants.rule_for(request.method, template) if template else "admin"
        refused = _rule_refusal(user, "admin" if rule == "proxy" else rule, params, member=node_id)
        if refused:
            raise HTTPException(status_code=403, detail=refused)
        if params.get("server_name"):
            perms = grants.perms_for(user, grants.server_key(params["server_name"], node_id))
        if request.method == "GET" and template in ("/api/servers", "/api/host", "/api/logs"):
            cut = template
    if await run_in_threadpool(_node_too_old, node):
        raise HTTPException(status_code=409, detail=_too_old_detail(node))
    # Pass on the path as the browser encoded it (server and file names), with its query string
    # (raw_path has no query string under uvicorn; Starlette's test client before 0.33 put it in)
    raw = request.scope.get("raw_path", b"").decode("latin-1").split("?", 1)[0]
    raw_target = "/" + raw.split("/proxy/", 1)[1] if "/proxy/" in raw else urllib.parse.quote(target)
    if request.url.query:
        raw_target += "?" + request.url.query

    body, length = None, 0
    if request.method != "GET":
        body = tempfile.SpooledTemporaryFile(max_size=8 << 20)
        async for chunk in request.stream():
            body.write(chunk)
            length += len(chunk)
        body.seek(0)
    try:
        res = await run_in_threadpool(cluster_client.open_proxy, node, request.method, raw_target, body, length,
                                      request.headers.get("content-type"), user["username"], user["role"],
                                      perms=perms)
    except cluster_client.NodeError as e:
        if body:
            body.close()
        raise HTTPException(status_code=502, detail=f"{node['name']}: {e}")
    if request.method in STATE_CHANGING:
        manager.write_log(f"[CLUSTER] {user['username']}@{panel_settings.display_name()}: {request.method} "
                          f"{target} on {node['name']}")
        cluster_client.forget_cached(node["id"])
    if res.status in (401, 403):
        raw_body = await run_in_threadpool(res.read_all)
        if body:
            body.close()
        try:
            detail = json.loads(raw_body).get("detail") or ""
        except (ValueError, AttributeError):
            detail = ""
        if res.status == 401:        # never a 401 to the browser: that would sign the user out here
            return JSONResponse({"detail": f"{node['name']} didn't accept the master: {detail or 'rejected'}"},
                                status_code=502)
        if detail == "Not allowed for the cluster master.":
            detail = _too_old_detail(node)
        return JSONResponse({"detail": detail or "Refused."}, status_code=403)
    if cut and res.status == 200:
        # A node's server list or dashboard, cut down to the servers this account may see
        raw_body = await run_in_threadpool(res.read_all)
        try:
            data = json.loads(raw_body)
            data = (_visible_servers(user, data, node_id) if cut == "/api/servers"
                    else {"content": _visible_log(user, data.get("content"), node_id)} if cut == "/api/logs"
                    else host.for_user(data, _can_view(user, node_id)))
        except (ValueError, TypeError, AttributeError, KeyError):
            return JSONResponse({"detail": f"{node['name']} sent an answer the master couldn't read."}, status_code=502)
        return JSONResponse(data)
    return StreamingResponse(res.chunks(), status_code=res.status, headers=res.headers,
                             background=BackgroundTask(body.close) if body else None)

class TemplatePush(BaseModel):
    template_id: str
    from_node: Optional[str] = None       # 1.16: a node's own template ("local" or none: the master's)

@app.post("/api/cluster/nodes/{node_id}/templates")
def cluster_push_template_api(node_id: str, req: TemplatePush, request: Request):
    """Copies a template to a cluster member: one of the master's to a node (so a server can be
    created there with it), or since 1.16 a node's to the master ("local") or another node."""
    _need_master()
    user = request.state.user
    who = {"actor": user["username"], "role": user["role"]}
    src = None if req.from_node in (None, "", "local") else _proxy_node(req.from_node)
    if src is None and node_id == "local":
        raise HTTPException(status_code=400, detail="That template is already on this panel.")
    if src is not None and src["id"] == node_id:
        raise HTTPException(status_code=400, detail=f"That template is already on {src['name']}.")
    if src is None:
        t = next((x for x in manager.load_templates() if x.get("id") == req.template_id), None)
    else:
        try:
            listed = cluster_client.call_node(src, "GET", "/api/templates", **who)
        except cluster_client.NodeError as e:
            raise HTTPException(status_code=502, detail=f"{src['name']}: {e}")
        t = next((x for x in listed if isinstance(x, dict) and x.get("id") == req.template_id), None) \
            if isinstance(listed, list) else None
    if not t:
        raise HTTPException(status_code=404, detail="Template not found.")
    if node_id == "local":
        try:
            t = cluster.clean_template(t, src["name"])
        except cluster.ClusterError as e:
            raise HTTPException(status_code=400, detail=f"{src['name']}: {e}")
        t.pop("from_master", None)
        templates = [x for x in manager.load_templates() if x.get("id") != t["id"]] + [{**t, "from_node": src["name"]}]
        manager.save_templates(templates)
        manager.write_log(f"[CLUSTER] {user['username']}@{panel_settings.display_name()}: copied template "
                          f"'{t['name']}' from {src['name']} to this panel")
        return {"status": "success"}
    node = _proxy_node(node_id)
    if _node_too_old(node):
        raise HTTPException(status_code=409, detail=_too_old_detail(node))
    try:
        cluster_client.call_node(node, "POST", "/api/cluster/templates", {"template": t}, **who)
    except cluster_client.NodeError as e:
        raise HTTPException(status_code=502, detail=f"{node['name']}: {e}")
    manager.write_log(f"[CLUSTER] {user['username']}@{panel_settings.display_name()}: copied template "
                      f"'{t.get('name')}' to {node['name']}" + (f" from {src['name']}" if src else ""))
    return {"status": "success"}

# --- 1.16: moving and copying servers between cluster members (docs/design/move.md)
# Node side: only the master calls these, with its token (NODE_ONLY_API keeps browsers out),
# and only for a master admin.
def _transfer_call(request, fn, *args, **kwargs):
    from core import archive
    user = request.state.user
    if user.get("source") != "cluster" or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Only a cluster master's admin can move servers.")
    try:
        return fn(*args, **kwargs)
    except transfer.TransferError as e:
        raise HTTPException(status_code=e.status, detail=str(e))
    except (manager.ServerError, manager.DependencyError, backups.BackupError, archive.ArchiveError) as e:
        raise HTTPException(status_code=400, detail=str(e))

class TransferExport(BaseModel):
    server: str
    stop: bool
    precopy: bool = False                   # 1.16.13: pack it while it runs, listing every file
    base: Optional[str] = None              # 1.16.13: only what changed since that pre-copy
    for_windows: bool = False               # 1.24: the target is Windows; refuse names it can't hold

class TransferImport(BaseModel):
    size: int
    sha256: str
    meta: dict
    base: Optional[str] = None              # 1.16.13: a staged pre-copy these changes go on

class TransferFinish(BaseModel):
    outcome: str
    moved_to: Optional[str] = None

class TransferInstall(BaseModel):
    keep_name: bool = True
    start: bool = True
    carry: bool = True

@app.post("/api/cluster/transfers/export")
def transfer_export_api(req: TransferExport, request: Request):
    return _transfer_call(request, transfer.export, req.server, req.stop, req.precopy, req.base, req.for_windows)

@app.get("/api/cluster/transfers/{tid}/archive")
def transfer_archive_api(tid: str, request: Request):
    path, _ = _transfer_call(request, transfer.open_archive, tid)
    return FileResponse(path, media_type="application/gzip", filename=f"{tid}.tar.gz")

@app.post("/api/cluster/transfers/{tid}/finish")
def transfer_finish_api(tid: str, req: TransferFinish, request: Request):
    return _transfer_call(request, transfer.finish, tid, req.outcome, req.moved_to)

@app.post("/api/cluster/transfers/import")
def transfer_import_api(req: TransferImport, request: Request):
    return _transfer_call(request, transfer.begin_import, req.size, req.sha256, req.meta, req.base)

@app.put("/api/cluster/transfers/{tid}/archive")
async def transfer_receive_api(tid: str, request: Request):
    receiver = await run_in_threadpool(_transfer_call, request, transfer.Receiver, tid)
    try:
        async for chunk in request.stream():
            if chunk:
                await run_in_threadpool(_transfer_call, request, receiver.write, chunk)
    except BaseException:
        receiver.abort()
        raise
    return await run_in_threadpool(_transfer_call, request, receiver.close)

@app.post("/api/cluster/transfers/{tid}/install")
def transfer_install_api(tid: str, req: TransferInstall, request: Request):
    return _transfer_call(request, transfer.install, tid, req.keep_name, req.start, req.carry,
                          request.state.user["username"])

@app.post("/api/cluster/transfers/{tid}/stage")
def transfer_stage_api(tid: str, request: Request):
    return _transfer_call(request, transfer.stage, tid)

@app.post("/api/cluster/transfers/{tid}/confirm")
def transfer_confirm_api(tid: str, request: Request):
    return _transfer_call(request, transfer.confirm, tid)

@app.delete("/api/cluster/transfers/{tid}")
def transfer_abort_api(tid: str, request: Request):
    return _transfer_call(request, transfer.abort, tid)

@app.delete("/api/cluster/transfers/moved/{server_name}")
def transfer_delete_moved_api(server_name: str, request: Request):
    return _transfer_call(request, transfer.delete_moved, server_name)

# --- 1.16.8: backup copies on another cluster member (core/backup_copies.py)
# Holder side: only the master calls these, with its token (NODE_ONLY_API keeps browsers out).
def _held_call(request, fn, *args):
    if request.state.user.get("source") != "cluster":
        raise HTTPException(status_code=403, detail="Only the cluster's master can use this.")
    try:
        return fn(*args)
    except backup_copies.CopyError as e:
        raise HTTPException(status_code=e.status, detail=str(e))
    except manager.ServerError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except backups.BackupError as e:
        raise HTTPException(status_code=400, detail=str(e))

def _query_int(request, name):
    try:
        return int(request.query_params.get(name, ""))
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Bad {name}.")

async def _held_receive(request, receiver):
    try:
        async for chunk in request.stream():
            if chunk:
                await run_in_threadpool(_held_call, request, receiver.write, chunk)
    except BaseException:
        receiver.abort()
        raise
    return await run_in_threadpool(_held_call, request, receiver.close)

@app.get("/api/cluster/held-backups")
def held_backups_api(request: Request, panel: Optional[str] = None, server: Optional[str] = None):
    return _held_call(request, backup_copies.list_held, panel or None, server or None)

@app.get("/api/cluster/held-backups/{panel}/{server_name}/{filename}")
def held_backup_download_api(request: Request, panel: str, server_name: str, filename: str):
    path = _held_call(request, backup_copies.held_path, panel, server_name, filename)
    return FileResponse(path, filename=filename, media_type="application/gzip")

@app.get("/api/cluster/held-backups/{panel}/{server_name}/{filename}/sha256")
def held_backup_sha_api(request: Request, panel: str, server_name: str, filename: str):
    return {"sha256": _held_call(request, backup_copies.held_sha256, panel, server_name, filename)}

@app.put("/api/cluster/held-backups/into/{server_name}/{filename}")
async def held_backup_into_api(request: Request, server_name: str, filename: str):
    receiver = await run_in_threadpool(_held_call, request, backup_copies.IntoReceiver, server_name, filename,
                                       _query_int(request, "size"), request.query_params.get("sha256", ""))
    return await _held_receive(request, receiver)

@app.put("/api/cluster/held-backups/{panel}/{server_name}/{filename}")
async def held_backup_receive_api(request: Request, panel: str, server_name: str, filename: str):
    q = request.query_params
    receiver = await run_in_threadpool(_held_call, request, backup_copies.HoldReceiver, panel,
                                       q.get("panel_name") or panel, server_name, filename,
                                       _query_int(request, "size"), q.get("sha256", ""), q.get("keep"))
    return await _held_receive(request, receiver)

@app.delete("/api/cluster/held-backups/{panel}/{server_name}/{filename}")
def held_backup_delete_api(request: Request, panel: str, server_name: str, filename: str):
    if request.state.user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Only a cluster master's admin can delete backup copies.")
    return _held_call(request, backup_copies.delete_held, panel, server_name, filename)

# Master side: the Backups tab's "Also keep a copy on", the copies, and what can be done with them
def _copy_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except backup_copies.CopyError as e:
        raise HTTPException(status_code=e.status, detail=str(e))
    except cluster_client.NodeError as e:
        raise HTTPException(status_code=e.status if e.status in (400, 404, 409, 507) else 502, detail=str(e))
    except (manager.ServerError, backups.BackupError) as e:
        raise HTTPException(status_code=400, detail=str(e))

class CopyTarget(BaseModel):
    to: Optional[str] = None

class CopyInto(BaseModel):
    member: str = "local"
    server: str

@app.get("/api/cluster/backup-copies")
def backup_copies_all_api(request: Request):
    """Every copy kept in the cluster, for putting one into a server (also after its machine is gone)."""
    _need_master_admin_for(request, "see every backup copy in the cluster")
    found, missing = _copy_call(backup_copies.copies)
    return {"copies": found, "unreachable": missing, "members": backup_copies.members()}

@app.get("/api/cluster/backup-copies/{member_id}/{server_name}")
def backup_copies_view_api(member_id: str, server_name: str):
    return _copy_call(backup_copies.view, member_id, server_name)

@app.put("/api/cluster/backup-copies/{member_id}/{server_name}")
def backup_copies_set_api(member_id: str, server_name: str, req: CopyTarget, request: Request):
    _need_master_admin_for(request, "choose where backup copies are kept")
    return _copy_call(backup_copies.set_target, member_id, server_name, req.to or None,
                      request.state.user["username"])

@app.post("/api/cluster/backup-copies/{member_id}/{server_name}/sync")
def backup_copies_sync_api(member_id: str, server_name: str, request: Request):
    """Copies what's missing now (after Back up now), without waiting for the next pass."""
    _need_master()
    if not _copy_call(backup_copies.target, member_id, server_name):
        raise HTTPException(status_code=400, detail="Copies of this server's backups aren't kept on another member.")
    backup_copies.sync_soon(member_id, server_name, request.state.user["username"])
    return {"status": "started"}

@app.get("/api/cluster/backup-copies/file/{holder}/{panel}/{server_name}/{filename}")
def backup_copy_download_api(holder: str, panel: str, server_name: str, filename: str, request: Request):
    user = request.state.user
    chunks, size = _copy_call(backup_copies.download, holder, panel, server_name, filename, user["username"],
                              user["role"])
    return StreamingResponse(chunks, media_type="application/gzip",
                             headers={"Content-Length": str(size),
                                      "Content-Disposition": f'attachment; filename="{filename}"'})

@app.post("/api/cluster/backup-copies/file/{holder}/{panel}/{server_name}/{filename}/into")
def backup_copy_into_api(holder: str, panel: str, server_name: str, filename: str, req: CopyInto, request: Request):
    _need_master_admin_for(request, "put a backup copy into a server")
    return _copy_call(backup_copies.put_into, holder, panel, server_name, filename, req.member, req.server,
                      request.state.user["username"])

@app.delete("/api/cluster/backup-copies/file/{holder}/{panel}/{server_name}/{filename}")
def backup_copy_delete_api(holder: str, panel: str, server_name: str, filename: str, request: Request):
    _need_master_admin_for(request, "delete a backup copy")
    return _copy_call(backup_copies.remove, holder, panel, server_name, filename, request.state.user["username"])

def _need_master_admin_for(request, what):
    _need_master()
    if request.state.user["role"] != "admin":
        raise HTTPException(status_code=403, detail=f"Only admins can {what}.")

# Master side: master admins start a move or copy and follow it
class MoveRequest(BaseModel):
    kind: str
    server: str
    from_node: str = "local"
    to_node: str = "local"
    start: bool = False
    precopy: bool = False

def _need_master_admin(request):
    _need_master()
    if request.state.user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Only admins can move or copy servers between cluster members.")

@app.post("/api/cluster/moves")
def cluster_move_api(req: MoveRequest, request: Request):
    _need_master_admin(request)
    user = request.state.user
    try:
        return moves.start(req.kind, req.from_node, req.server, req.to_node, req.start, user["username"], user["role"],
                           req.precopy)
    except moves.MoveError as e:
        raise HTTPException(status_code=e.status, detail=str(e))

@app.get("/api/cluster/moves")
def cluster_moves_api():
    _need_master()
    return moves.jobs()

@app.get("/api/cluster/moves/{job_id}")
def cluster_move_job_api(job_id: str):
    _need_master()
    job = moves.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="No such move.")
    return job

@app.delete("/api/cluster/moved/{member_id}/{server_name}")
def cluster_delete_moved_api(member_id: str, server_name: str, request: Request):
    """Deletes the files a move left behind on its source, once the admin is happy with the move."""
    _need_master_admin(request)
    user = request.state.user
    end = None
    try:
        end = moves.member(member_id, user["username"], user["role"])
        end.delete_moved(server_name)
    except moves.MoveError as e:
        raise HTTPException(status_code=e.status, detail=str(e))
    except transfer.TransferError as e:
        raise HTTPException(status_code=e.status, detail=str(e))
    except cluster_client.NodeError as e:
        # The node's own refusal (no such moved server) as it is; anything else is the node's fault
        raise HTTPException(status_code=e.status if e.status in (400, 404, 409) else 502, detail=f"{end.name}: {e}")
    except manager.ServerError as e:
        raise HTTPException(status_code=400, detail=str(e))
    manager.write_log(f"[MOVE] {user['username']}: deleted the old copy of {server_name} on {end.name}.")
    cluster_client.forget_cached()
    return {"status": "success"}

@app.post("/api/cluster/nodes/{node_id}/servers/{server_name}/template")
def cluster_save_template_api(node_id: str, server_name: str, request: Request, name: str = Body(..., embed=True)):
    """Save as Template on a node's server: a template on the master, like every other one."""
    node = _proxy_node(node_id)
    if not manager.SERVICE_NAME_RE.match(server_name) or not str(name).strip():
        raise HTTPException(status_code=400, detail="Give the template a name.")
    user = request.state.user
    if _node_too_old(node):
        raise HTTPException(status_code=409, detail=_too_old_detail(node))
    who = {"actor": user["username"], "role": user["role"]}
    try:
        srv = next((s for s in cluster_client.call_node(node, "GET", "/api/servers", **who)
                    if s.get("name") == server_name), None)
        if not srv:
            raise HTTPException(status_code=404, detail=f"No server called {server_name} on {node['name']}.")
        props = cluster_client.call_node(node, "GET", f"/api/servers/{server_name}/properties", **who)
        perms = cluster_client.call_node(node, "GET", f"/api/servers/{server_name}/permissions/raw", **who)
    except cluster_client.NodeError as e:
        raise HTTPException(status_code=502, detail=f"{node['name']}: {e}")
    templates = manager.load_templates()
    templates.append({"id": str(uuid.uuid4()), "name": str(name).strip()[:100],
                      "edition": "Java" if srv.get("edition") == "Java" else "Bedrock",
                      "properties": manager.template_properties(props) if isinstance(props, dict) else {},
                      "permissions": perms.get("content", "") if isinstance(perms, dict) else ""})
    manager.save_templates(templates)
    manager.write_log(f"[CLUSTER] {user['username']}@{panel_settings.display_name()}: saved {server_name} on "
                      f"{node['name']} as template '{str(name).strip()[:100]}'")
    return {"status": "success"}

# --- Panel Settings > Cluster (admins only: under /api/settings)
# --- 1.25: API tokens (core/apitokens.py). Admins make and revoke them here; scripts send
# `Authorization: Bearer rpa_...` and act with the token's per-server permissions.
class TokenCreate(BaseModel):
    name: str
    grants: dict
    days: Optional[int] = None

@app.get("/api/settings/api-tokens")
def api_tokens_api():
    return {"tokens": apitokens.list_tokens(), "perms": list(grants.PERMS), "labels": grants.PERM_LABELS}

@app.post("/api/settings/api-tokens")
def api_token_create_api(request: Request, req: TokenCreate):
    me = request.state.user["username"]
    try:
        out = apitokens.create(req.name, req.grants, req.days, actor=me)
    except apitokens.TokenError as e:
        raise HTTPException(status_code=400, detail=str(e))
    shown = ", ".join(f"{k}: {'/'.join(v)}" for k, v in out["grants"].items())
    manager.write_log(f"[API] {me} made the API token {out['name']} ({shown}"
                      + (f"; expires {time.strftime('%Y-%m-%d', time.localtime(out['expires']))}" if out["expires"] else "")
                      + ").")
    return out

@app.delete("/api/settings/api-tokens/{token_id}")
def api_token_revoke_api(request: Request, token_id: str):
    try:
        name = apitokens.revoke(token_id)
    except apitokens.TokenError as e:
        raise HTTPException(status_code=404, detail=str(e))
    manager.write_log(f"[API] {request.state.user['username']} revoked the API token {name}.")
    return {"status": "success"}

@app.get("/api/settings/cluster")
def cluster_settings_api(request: Request):
    from core import tls
    st = cluster.state()
    view = {"role": st["role"], "managed_restart": panel_settings.RUNTIME.get("scheme") is not None,
            "https_mode": panel_settings.load()["https_mode"], "identity": cluster.own_identity()}
    if st["role"] != "standalone":
        view["ca"] = tls.cert_info(cluster.ca_crt())
        view["cert_expires"] = cluster.cert_expires(cluster.node_crt())
    if st["role"] == "master":
        view.update(name=st["name"], cluster_id=st["cluster_id"], epoch=st["epoch"],
                    nodes=[cluster.public_node(n) for n in cluster.load_nodes()],
                    joins=cluster.list_joins(), join_url=cluster.default_join_url(request.headers.get("host")),
                    nightly=cluster.nightly_view(), renew_within_days=cluster.RENEW_WITHIN // 86400)
    elif st["role"] == "node":
        view.update(name=st.get("cluster_name"), cluster_id=st.get("cluster_id"), epoch=st.get("epoch"),
                    master_name=st.get("master_name"), master_url=st.get("master_url"),
                    joined=st.get("joined"), last_used=st.get("last_used"), last_ip=st.get("last_ip"))
    return view

@app.post("/api/settings/cluster")
def cluster_create_api(request: Request, body: dict = Body(...)):
    _need_managed_restart()
    _need_https_port_free()
    me = request.state.user["username"]
    try:
        st = cluster.create_cluster(body.get("name"), me)
    except (cluster.ClusterError, panel_settings.SettingsError) as e:
        _cluster_fail(e)
    manager.write_log(f"[CLUSTER] {me} created cluster '{st['name']}'; this panel is its master.")
    return _restart_view(request)

@app.post("/api/settings/cluster/joins")
def cluster_new_join_api(request: Request, body: dict = Body(default={})):
    me = request.state.user["username"]
    try:
        j = cluster.create_join(me, body.get("url") or cluster.default_join_url(request.headers.get("host")))
    except cluster.ClusterError as e:
        _cluster_fail(e)
    manager.write_log(f"[CLUSTER] {me} made a join code ({j['id']}), valid for one hour.")
    return j

@app.post("/api/settings/cluster/discover")
def cluster_discover_api(request: Request):
    """Find panels on your network (1.16.13): RedstonePanels on this machine's private networks."""
    from core import discovery
    me = request.state.user["username"]
    if cluster.role() != "master":
        raise HTTPException(status_code=400, detail="Create a cluster first: only its master adds panels.")
    try:
        found = discovery.find(cluster.load_nodes())
    except discovery.DiscoveryError as e:
        raise HTTPException(status_code=409, detail=str(e))
    manager.write_log(f"[CLUSTER] {me} looked for panels on {', '.join(found['networks'])}: found "
                      f"{len(found['panels'])} in {found['seconds']} s.")
    return found

@app.delete("/api/settings/cluster/joins/{join_id}")
def cluster_cancel_join_api(join_id: str):
    cluster.cancel_join(join_id)
    return {"status": "success"}

@app.patch("/api/settings/cluster/nodes/{node_id}")
def cluster_update_node_api(node_id: str, body: dict = Body(...)):
    try:
        n = cluster.update_node(node_id, name=body.get("name"), url=body.get("url"), verify=body.get("verify"))
    except cluster.ClusterError as e:
        _cluster_fail(e)
    cluster_client.forget_cached(node_id)
    return cluster.public_node(n)

@app.post("/api/settings/cluster/nodes/{node_id}/renew")
def cluster_renew_api(node_id: str, request: Request):
    me = request.state.user["username"]
    if node_id == "local":
        _need_managed_restart()
        try:
            cluster.renew_own_cert()
        except cluster.ClusterError as e:
            _cluster_fail(e)
        manager.write_log(f"[CLUSTER] {me} renewed this master's certificate.")
        return _restart_view(request)
    node = cluster.find_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail="No such node.")
    try:
        expires = cluster_client.renew_node(node, me)
    except cluster_client.NodeError as e:
        raise HTTPException(status_code=502, detail=f"{node['name']}: {e}")
    except cluster.ClusterError as e:
        _cluster_fail(e)
    manager.write_log(f"[CLUSTER] {me} renewed the certificate of node '{node['name']}'; it is restarting.")
    return {"status": "success", "cert_expires": expires}

@app.delete("/api/settings/cluster/nodes/{node_id}")
def cluster_remove_node_api(node_id: str, request: Request):
    me = request.state.user["username"]
    node = cluster.find_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail="No such node.")
    confirmed, problem = True, None
    try:
        cluster_client.call_node(node, "DELETE", "/api/cluster/membership", actor=me)
    except cluster_client.NodeError as e:
        confirmed, problem = False, str(e)
    cluster.forget_node(node_id)
    backup_copies.forget_member(node_id)
    cluster_client.forget_cached(node_id)
    manager.write_log(f"[CLUSTER] {me} removed node '{node['name']}'"
                      + ("" if confirmed else f" (it couldn't be told: {problem}; run 'sudo redstonepanel leave' on it)"))
    return {"status": "success", "node_confirmed": confirmed, "problem": problem}

class BackupRequest(BaseModel):
    passphrase: str

@app.post("/api/settings/cluster/backup")
def cluster_backup_api(req: BackupRequest, request: Request):
    me = request.state.user["username"]
    try:
        data = cluster.make_backup(req.passphrase, me)
    except cluster.ClusterError as e:
        _cluster_fail(e)
    manager.write_log(f"[CLUSTER] {me} downloaded an encrypted cluster backup.")
    slug = "".join(c if c.isalnum() else "-" for c in cluster.state()["name"]).strip("-").lower() or "cluster"
    return Response(content=data, media_type="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="redstonepanel-{slug}.rpbackup"'})

class NightlyBackups(BaseModel):
    enabled: bool
    passphrase: Optional[str] = None

@app.put("/api/settings/cluster/nightly")
def cluster_nightly_api(req: NightlyBackups, request: Request):
    me = request.state.user["username"]
    try:
        view = cluster.set_nightly(req.enabled, req.passphrase, me)
    except cluster.ClusterError as e:
        _cluster_fail(e)
    manager.write_log(f"[CLUSTER] {me} turned nightly cluster backups {'on' if view['enabled'] else 'off'}.")
    return view

@app.get("/api/settings/cluster/backups/{filename}")
def cluster_nightly_download_api(filename: str, request: Request):
    try:
        path = cluster.nightly_file(filename)
    except cluster.ClusterError as e:
        raise HTTPException(status_code=404, detail=str(e))
    manager.write_log(f"[CLUSTER] {request.state.user['username']} downloaded the nightly cluster backup {filename}.")
    return FileResponse(path, filename=filename, media_type="application/octet-stream")

@app.post("/api/settings/cluster/join")
def cluster_join_settings_api(request: Request, body: dict = Body(...)):
    _need_managed_restart()
    _need_https_port_free()
    me = request.state.user["username"]
    try:
        st = cluster.join(body.get("code"), me, cluster_client)
    except cluster.ClusterError as e:
        _cluster_fail(e)
    manager.write_log(f"[CLUSTER] {me} joined this panel to cluster '{st['cluster_name']}' (master: {st['master_name']}).")
    return _restart_view(request)

@app.post("/api/settings/cluster/leave")
def cluster_leave_api(request: Request):
    _need_managed_restart()
    me = request.state.user["username"]
    try:
        if cluster.role() == "master":
            cluster.dissolve(me)
            manager.write_log(f"[CLUSTER] {me} dissolved the cluster; this panel is standalone again.")
        else:
            cluster.leave()
            manager.write_log(f"[CLUSTER] {me} left the cluster; this panel is standalone again. "
                              "Remove it on the master too.")
    except cluster.ClusterError as e:
        _cluster_fail(e)
    return _restart_view(request)
