from fastapi import FastAPI, HTTPException, Body, UploadFile, File, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.concurrency import run_in_threadpool
from core.models import ServerDeployRequest, ServerActionRequest, ServerPropertiesUpdate, ServerUpdateRequest
from pydantic import BaseModel
from typing import Optional
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
import core.backups as backups
import core.notify as notify
import core.restarts as restarts
import core.auth as auth
import core.settings as panel_settings
from core.version import panel_version
import core.cluster as cluster
import core.cluster_client as cluster_client
import core.transfer as transfer
import core.moves as moves
from starlette.background import BackgroundTask
import json
import uuid
import os
import shutil
import signal
import subprocess
import tempfile
import threading

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
        for asset in ("/static/js/app.js", "/static/css/tailwind.css"):
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
PUBLIC_API = {"/api/auth/login", "/api/auth/logout", "/api/health", "/api/panel-info",
              "/api/cluster/ca", "/api/cluster/ca.crt", "/api/cluster/join"}
ADMIN_API = ("/api/users", "/api/settings")
NODE_ONLY_API = ("/api/cluster/hello", "/api/cluster/membership", "/api/cluster/cert", "/api/cluster/templates",
                 "/api/cluster/transfers")
CLUSTER_USER_API = ("/api/cluster/overview", "/api/cluster/hosts", "/api/cluster/nodes/", "/api/cluster/moves",
                    "/api/cluster/moved/")
STATE_CHANGING = {"POST", "PUT", "PATCH", "DELETE"}

@app.middleware("http")
async def access_control(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/"):
        if request.method in STATE_CHANGING and panel_settings.same_origin(request.headers) is False:
            return JSONResponse({"detail": "Request blocked: it came from another website."}, status_code=403)
        if path not in PUBLIC_API:
            ip = request.client.host if request.client else None
            bearer = request.headers.get("authorization", "")
            if bearer.lower().startswith("bearer "):
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
                if request.method in STATE_CHANGING:
                    manager.write_log(f"[CLUSTER] {user['username']}: {request.method} {path} (from {ip})")
            else:
                token = request.cookies.get(auth.SESSION_COOKIE)
                user = await run_in_threadpool(auth.read_session_token, token) if token else None
                if not user:
                    user = panel_settings.trusted_network_user(ip, request.headers, request.method)
                if not user:
                    return JSONResponse({"detail": "Not signed in."}, status_code=401)
                if path.startswith(NODE_ONLY_API):
                    return JSONResponse({"detail": "Only the cluster's master can use this."}, status_code=403)
                if path.startswith(CLUSTER_USER_API) and user["source"] == "trusted":
                    return JSONResponse({"detail": "Sign in to see the cluster."}, status_code=403)
                if path.startswith(ADMIN_API) and user["role"] != "admin":
                    detail = ("Sign in as an admin to change this." if user["source"] == "trusted"
                              else "Only admins can do this.")
                    return JSONResponse({"detail": detail}, status_code=403)
            request.state.user = user
    return await call_next(request)

@app.get("/api/health")
def health():
    return {"status": "ok"}

@app.get("/api/panel-info")
def panel_info():
    """Which panel this is, so several servers' panels can be told apart (also before sign-in)."""
    return {"name": panel_settings.display_name()}

class LoginRequest(BaseModel):
    username: str
    password: str

@app.post("/api/auth/login")
def login_api(req: LoginRequest, request: Request, response: Response):
    ip = request.client.host if request.client else "unknown"
    try:
        identity = auth.login(req.username, req.password, ip)
    except auth.AuthError as e:
        manager.write_log(f"[AUTH] Failed sign-in for '{req.username[:64]}' from {ip}")
        raise HTTPException(status_code=401, detail=str(e))
    manager.write_log(f"[AUTH] {identity['u']} ({identity['src']}) signed in from {ip}")
    response.set_cookie(auth.SESSION_COOKIE, auth.make_session_token(identity), max_age=auth.SESSION_SECONDS,
                        httponly=True, samesite="strict", secure=request.url.scheme == "https", path="/")
    return auth.read_session_token(auth.make_session_token(identity))

@app.post("/api/auth/logout")
def logout_api(response: Response):
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return {"status": "success"}

@app.get("/api/auth/me")
def me_api(request: Request):
    user = dict(request.state.user)
    # The release this panel runs; signed-in users only (the sign-in page doesn't advertise it)
    user["panel_version"] = panel_version()
    # Admins get told when HTTPS was requested but the panel had to fall back to HTTP
    if user["role"] == "admin":
        user["https_error"] = panel_settings.RUNTIME.get("https_error")
    st = cluster.state()
    user["cluster"] = {"role": st["role"], "name": st.get("name") or st.get("cluster_name"),
                       "master_name": st.get("master_name")}
    return user

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

    panel_settings.save(s)
    me = request.state.user["username"]
    manager.write_log(f"[SETTINGS] {me} changed panel settings: HTTPS={s['https_mode']}, "
                      f"ports {s['http_port']}/{s['https_port']}, sign-in={s['login_mode']}, "
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
        raise HTTPException(status_code=400, detail="Linux accounts change their password on the server (passwd).")
    try:
        auth.change_own_password(user["username"], req.current_password, req.new_password)
    except auth.AuthError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # The change signs out all sessions, so issue a fresh cookie for this browser
    u = next(x for x in auth._load_users() if x["username"] == user["username"])
    response.set_cookie(auth.SESSION_COOKIE,
                        auth.make_session_token({"u": u["username"], "src": "panel", "v": u["session_version"]}),
                        max_age=auth.SESSION_SECONDS, httponly=True, samesite="strict",
                        secure=request.url.scheme == "https", path="/")
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
    return {"users": auth.list_panel_users(), "linux_groups": list(auth.ALLOWED_LINUX_GROUPS)}

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

@app.delete("/api/users/{username}")
def delete_user_api(username: str, request: Request):
    me = request.state.user
    if me["source"] == "panel" and me["username"] == username:
        raise HTTPException(status_code=400, detail="You can't delete your own account.")
    try:
        auth.delete_panel_user(username)
    except auth.AuthError as e:
        raise HTTPException(status_code=400, detail=str(e))
    manager.write_log(f"[AUTH] {me['username']} deleted panel user '{username}'")
    return {"status": "success"}

@app.get("/")
def index():
    return RedirectResponse(url="/static/index.html")

@app.get("/api/servers")
def get_servers():
    return manager.list_servers()

# --- The dashboard (no server selected): this machine and what the panel relies on.
# Trusted-LAN visitors get it without addresses, versions or paths; the cluster master reads it too.
@app.get("/api/host")
def host_api(request: Request):
    return host.report(redacted=request.state.user.get("source") == "trusted")

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

@app.post("/api/servers/{server_name}/update")
def update_server(server_name: str, req: Optional[ServerUpdateRequest] = None):
    try:
        manager.update_server_binary(server_name, req.version if req else None)
    except manager.JavaMissing:
        raise                                    # 409, see java_missing() below
    except (manager.DependencyError, manager.ServerError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "success"}

# Java server software: which Minecraft versions each loader offers, and the Java one needs
@app.get("/api/java/versions")
def java_versions(loader: str = "vanilla"):
    try:
        versions = javaservers.list_versions(loader)
    except javaservers.JavaServerError as e:
        raise HTTPException(status_code=502 if "reach" in str(e) or "HTTP" in str(e) else 400, detail=str(e))
    return {"loader": loader, "versions": versions[:150], "loaders": javaservers.LOADERS}

@app.get("/api/java/requirement")
def java_requirement(version: str):
    if not javaservers.RELEASE_RE.match(version or ""):
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
    return JSONResponse({"detail": str(e), "java": {"required": e.required, "package": e.package}},
                        status_code=409)

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
    return {"status": "success"}

# Backups: /var/backups/redstonepanel/<server>/, scheduled by a systemd timer per server
class BackupSchedule(BaseModel):
    enabled: bool
    frequency: str
    time: str
    keep: int

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
    return _backup_call(backups.set_schedule, server_name, req.enabled, req.frequency, req.time, req.keep)

@app.post("/api/servers/{server_name}/backups")
def backup_now(server_name: str):
    _backup_call(manager._check_server, server_name)
    return _backup_call(backups.run_backup, server_name, "manual")

@app.get("/api/servers/{server_name}/backups/{filename}")
def download_backup(server_name: str, filename: str):
    path = _backup_call(backups.backup_path, server_name, filename)
    return FileResponse(path, filename=filename, media_type="application/gzip")

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
    _require_admin(request, "use the file manager")
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
    return {"status": "success"}
    
@app.get("/api/logs")
def get_logs():
    """Returns the most recent 100 lines of the live action console."""
    return {"content": manager.get_panel_logs(100)}

# Update the deploy endpoint to log its output
@app.post("/api/servers/deploy")
def deploy_server(req: ServerDeployRequest):
    # This executes the native python deployment and logs output to the web console
    try:
        manager.deploy_server(req.edition, req.template_id, req.loader, req.version)
    except manager.JavaMissing:
        raise                                    # 409, see java_missing() below
    except (manager.TemplateError, manager.DependencyError, manager.ServerError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "success"}

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

# --- Master: the cluster dashboard (signed-in users; trusted-LAN visitors are refused)
def _need_master():
    if cluster.role() != "master":
        raise HTTPException(status_code=404, detail="This panel isn't a cluster master.")

@app.get("/api/cluster/overview")
def cluster_overview_api():
    _need_master()
    return cluster_client.overview(manager.list_servers)

@app.get("/api/cluster/hosts")
def cluster_hosts_api():
    """Each panel's CPU, memory, disk and servers online, the master's own first."""
    _need_master()
    return cluster_client.hosts(lambda: host.summary(host.report()), host.summary)

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
        cluster_client.call_node(node, "POST", f"/api/servers/{server_name}/action",
                                 {"action": req.action}, actor=me)
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
    if await run_in_threadpool(_node_too_old, node):
        raise HTTPException(status_code=409, detail=_too_old_detail(node))
    # Pass on the path as the browser encoded it (server and file names), with its query string
    raw = request.scope.get("raw_path", b"").decode("latin-1")
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
                                      request.headers.get("content-type"), user["username"], user["role"])
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

class TransferImport(BaseModel):
    size: int
    sha256: str
    meta: dict

class TransferFinish(BaseModel):
    outcome: str
    moved_to: Optional[str] = None

class TransferInstall(BaseModel):
    keep_name: bool = True
    start: bool = True
    carry: bool = True

@app.post("/api/cluster/transfers/export")
def transfer_export_api(req: TransferExport, request: Request):
    return _transfer_call(request, transfer.export, req.server, req.stop)

@app.get("/api/cluster/transfers/{tid}/archive")
def transfer_archive_api(tid: str, request: Request):
    path, _ = _transfer_call(request, transfer.open_archive, tid)
    return FileResponse(path, media_type="application/gzip", filename=f"{tid}.tar.gz")

@app.post("/api/cluster/transfers/{tid}/finish")
def transfer_finish_api(tid: str, req: TransferFinish, request: Request):
    return _transfer_call(request, transfer.finish, tid, req.outcome, req.moved_to)

@app.post("/api/cluster/transfers/import")
def transfer_import_api(req: TransferImport, request: Request):
    return _transfer_call(request, transfer.begin_import, req.size, req.sha256, req.meta)

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

@app.post("/api/cluster/transfers/{tid}/confirm")
def transfer_confirm_api(tid: str, request: Request):
    return _transfer_call(request, transfer.confirm, tid)

@app.delete("/api/cluster/transfers/{tid}")
def transfer_abort_api(tid: str, request: Request):
    return _transfer_call(request, transfer.abort, tid)

@app.delete("/api/cluster/transfers/moved/{server_name}")
def transfer_delete_moved_api(server_name: str, request: Request):
    return _transfer_call(request, transfer.delete_moved, server_name)

# Master side: master admins start a move or copy and follow it
class MoveRequest(BaseModel):
    kind: str
    server: str
    from_node: str = "local"
    to_node: str = "local"
    start: bool = False

def _need_master_admin(request):
    _need_master()
    if request.state.user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Only admins can move or copy servers between cluster members.")

@app.post("/api/cluster/moves")
def cluster_move_api(req: MoveRequest, request: Request):
    _need_master_admin(request)
    user = request.state.user
    try:
        return moves.start(req.kind, req.from_node, req.server, req.to_node, req.start, user["username"], user["role"])
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
