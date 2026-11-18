from fastapi import FastAPI, HTTPException, Body, UploadFile, File, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.concurrency import run_in_threadpool
from core.models import ServerDeployRequest, ServerActionRequest, ServerPropertiesUpdate, ServerUpdateRequest
from pydantic import BaseModel
from typing import Optional
import core.manager as manager
import core.files as files
import urllib.parse
import core.javaservers as javaservers
import core.backups as backups
import core.notify as notify
import core.restarts as restarts
import core.auth as auth
import core.settings as panel_settings
from core.version import panel_version
import core.cluster as cluster
import core.cluster_client as cluster_client
from starlette.background import BackgroundTask
import os
import shutil
import signal
import subprocess
import tempfile
import threading

app = FastAPI(title="RedstonePanel API", version=panel_version())

class RawPropertiesUpdate(BaseModel):
    content: str

app.mount("/static", StaticFiles(directory=os.path.join(panel_settings.APP_DIR, "static")), name="static")

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
NODE_ONLY_API = ("/api/cluster/hello", "/api/cluster/membership", "/api/cluster/cert")
CLUSTER_USER_API = ("/api/cluster/overview", "/api/cluster/nodes/")
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
                if request.method in STATE_CHANGING:
                    actor = "".join(c for c in request.headers.get("x-redstonepanel-actor", "?")[:64] if c.isprintable())
                    manager.write_log(f"[CLUSTER] {user['username'][7:]} ({ip}) as {actor}: {request.method} {path}")
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
    in_cluster = cluster.role() != "standalone"
    if in_cluster and (s["https_mode"] != old["https_mode"] or s["https_port"] != old["https_port"]):
        raise HTTPException(status_code=400, detail="This panel is in a cluster, so its HTTPS mode and port can't "
                                                    "change. Leave the cluster first (Panel Settings > Cluster).")
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
    manager.write_properties(manager.get_server_dir(server_name), update.properties)
    return {"status": "success"}

@app.get("/api/servers/{server_name}/properties/raw")
def get_raw_properties(server_name: str):
    return {"content": manager.read_raw_properties(manager.get_server_dir(server_name))}

@app.post("/api/servers/{server_name}/properties/raw")
def update_raw_properties(server_name: str, update: RawPropertiesUpdate):
    manager.write_raw_properties(manager.get_server_dir(server_name), update.content)
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

@app.post("/api/servers/{server_name}/update")
def update_server(server_name: str, req: Optional[ServerUpdateRequest] = None):
    try:
        manager.update_server_binary(server_name, req.version if req else None)
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
        manager.apply_template_to_server(server_name, template_id)
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

# --- Master: the cluster dashboard (signed-in users; trusted-LAN visitors are refused)
def _need_master():
    if cluster.role() != "master":
        raise HTTPException(status_code=404, detail="This panel isn't a cluster master.")

@app.get("/api/cluster/overview")
def cluster_overview_api():
    _need_master()
    return cluster_client.overview(manager.list_servers)

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
    manager.write_log(f"[CLUSTER] {me}: {req.action} {server_name} on {node['name']}")
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
                    joins=cluster.list_joins(), join_url=cluster.default_join_url(request.headers.get("host")))
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
        n = cluster.update_node(node_id, name=body.get("name"), url=body.get("url"))
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
        req = cluster_client.call_node(node, "POST", "/api/cluster/cert/csr", actor=me)
        pem, sans = cluster.sign_renewal(node, req)
        cluster_client.call_node(node, "PUT", "/api/cluster/cert", {"cert": pem}, actor=me)
    except cluster_client.NodeError as e:
        raise HTTPException(status_code=502, detail=f"{node['name']}: {e}")
    except cluster.ClusterError as e:
        _cluster_fail(e)
    expires = cluster.cert_expires_pem(pem)
    cluster.update_node(node_id, sans=sans, cert_expires=expires)
    cluster_client.forget_cached(node_id)
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
