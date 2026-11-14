from fastapi import FastAPI, HTTPException, Body, UploadFile, File, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.concurrency import run_in_threadpool
from core.models import ServerDeployRequest, ServerActionRequest, ServerPropertiesUpdate
from pydantic import BaseModel
from typing import Optional
import core.manager as manager
import core.auth as auth
import core.settings as panel_settings
from starlette.background import BackgroundTask
import os
import shutil
import signal
import subprocess
import tempfile
import threading

app = FastAPI(title="RedstonePanel API", version="4.0.0")

class RawPropertiesUpdate(BaseModel):
    content: str

app.mount("/static", StaticFiles(directory=os.path.join(panel_settings.APP_DIR, "static")), name="static")

# ---------------------------------------------------------------------------
# Access control for every /api/ call
#   1. Requests that change something are refused if the browser says they came from another
#      website (cross-site request forgery), signed in or not.
#   2. A valid session cookie, or (if an admin enabled it) a request from a trusted LAN range,
#      which gets the limited "user" role. Users and Panel Settings always need a signed-in admin.
# ---------------------------------------------------------------------------
# /api/panel-info only says which panel this is (shown on the sign-in page), nothing more
PUBLIC_API = {"/api/auth/login", "/api/auth/logout", "/api/health", "/api/panel-info"}
ADMIN_API = ("/api/users", "/api/settings")
STATE_CHANGING = {"POST", "PUT", "PATCH", "DELETE"}

@app.middleware("http")
async def access_control(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/"):
        if request.method in STATE_CHANGING and panel_settings.same_origin(request.headers) is False:
            return JSONResponse({"detail": "Request blocked: it came from another website."}, status_code=403)
        if path not in PUBLIC_API:
            token = request.cookies.get(auth.SESSION_COOKIE)
            user = await run_in_threadpool(auth.read_session_token, token) if token else None
            if not user:
                ip = request.client.host if request.client else None
                user = panel_settings.trusted_network_user(ip, request.headers, request.method)
            if not user:
                return JSONResponse({"detail": "Not signed in."}, status_code=401)
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
    # Admins get told when HTTPS was requested but the panel had to fall back to HTTP
    if user["role"] == "admin":
        user["https_error"] = panel_settings.RUNTIME.get("https_error")
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
def update_server(server_name: str):
    try:
        manager.update_server_binary(server_name)
    except manager.DependencyError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "success"}

@app.post("/api/servers/{server_name}/action")
def control_server(server_name: str, req: ServerActionRequest):
    manager.control_server(server_name, req.action)
    return {"status": "success"}

@app.delete("/api/servers/{server_name}")
def uninstall_server(server_name: str, keep_world: bool = True):
    manager.remove_server(server_name, keep_world)
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
        manager.deploy_server(req.edition, req.template_id)
    except (manager.TemplateError, manager.DependencyError) as e:
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
    status = manager.send_chat_message(server_name, payload.message)
    
    if status == "restarting":
        return {"status": "info", "message": "RCON was not set up (or its port clashed with another server), so the panel configured it. The server is restarting; send your message again in a moment."}
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