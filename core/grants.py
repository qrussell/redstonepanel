"""Per-server permissions for panel accounts with the `user` role (1.18).

An admin is effectively root and can do everything. A `user` can do, on each server, only what
an admin ticked for them under Users -> Servers:

    view      see the server, its status, performance and schedules
    console   Live Chat and console commands
    power     start, stop, restart
    players   player roles, whitelist, kick, ban, the access log
    files     the file manager and SFTP
    mods      mods, plugins and add-ons
    settings  server.properties, restart schedule, updates, templates, world name
    backups   make, schedule and download backups (restore and delete stay admin-only)

Anything else (creating, importing, deleting and moving servers, Java, panel settings, users,
port forwarding) needs an admin. The action console shows such an account only the log lines
about its servers.

Grants live in DATA_DIR/grants.json: {"users": {"alex": {"minecraft-2": ["view", "console"],
"n-1a2b/minecraft": [...], "*": [...]}}}. A server on this panel is keyed by its name, a
cluster node's server by "<node id>/<name>" (kept on the master, as the master's users are),
and "*" means every server, including ones made later.

Every API route has a rule in RULES; main.py's middleware checks it before the route runs, and
tests/test_grants.py fails when a route has no rule. A route that isn't listed needs an admin.
"""
import json
import os
import re
import tempfile
import threading

from core.settings import DATA_DIR

GRANTS_FILE = os.path.join(DATA_DIR, "grants.json")

PERMS = ("view", "console", "power", "players", "files", "mods", "settings", "backups")
PERM_LABELS = {"view": "View", "console": "Console & chat", "power": "Start/stop/restart",
               "players": "Players", "files": "Files", "mods": "Mods & plugins", "settings": "Settings",
               "backups": "Backups"}
PRESETS = {"moderator": ("view", "console", "players"),
           "co-owner": PERMS}
# What every `user` account could do to every server before 1.18 (files and mods were admin-only).
# Accounts that existed before the update keep exactly this on every server; so do trusted-LAN
# visitors and a cluster master older than 1.18 acting for one of its users.
LEGACY = ("view", "console", "power", "players", "settings", "backups")
ALL_SERVERS = "*"
KEY_RE = re.compile(r"^(?:\*|(?:n-[0-9a-f]{4,32}/)?minecraft[A-Za-z0-9_.@-]*)$")

_lock = threading.RLock()
_cache = {"mtime": None, "data": None}


class GrantError(ValueError):
    """A grant that can't be saved (message is safe to show)."""


def server_key(server, member=None):
    """The key a server's grants are kept under: its name here, "<node id>/<name>" on a node."""
    return server if not member or member in ("local", "master") else f"{member}/{server}"


# ---------------------------------------------------------------------------
# grants.json
# ---------------------------------------------------------------------------
def _load():
    try:
        mtime = os.path.getmtime(GRANTS_FILE)
    except OSError:
        return {}
    with _lock:
        if _cache["mtime"] != mtime or _cache["data"] is None:
            try:
                with open(GRANTS_FILE) as f:
                    raw = json.load(f)
                users = raw.get("users") if isinstance(raw, dict) else None
                data = users if isinstance(users, dict) else {}
            except (OSError, ValueError):
                data = {}                    # unreadable: nobody is granted anything (admins still are)
            _cache.update(mtime=mtime, data=data)
        return _cache["data"]


def _save(users):
    os.makedirs(os.path.dirname(GRANTS_FILE), exist_ok=True)
    # A temporary name of its own: the panel and `redstonepanel add-user` may both write at once
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(GRANTS_FILE), prefix=".grants-", suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump({"version": 1, "users": users}, f, indent=2, sort_keys=True)
    os.replace(tmp, GRANTS_FILE)
    with _lock:
        _cache.update(mtime=None, data=None)


def _clean(perms):
    perms = {p for p in perms or () if p in PERMS}
    if perms:
        perms.add("view")                    # anything else implies seeing the server
    return [p for p in PERMS if p in perms]


def get(username):
    """{server key: [perms]} for one account."""
    g = _load().get(username)
    return {k: _clean(v) for k, v in g.items() if KEY_RE.match(k) and _clean(v)} if isinstance(g, dict) else {}


def set_user(username, grants):
    """Replaces one account's grants. grants: {server key: [perms]}."""
    if not isinstance(grants, dict):
        raise GrantError("Send the servers and what the account may do on each.")
    clean = {}
    for key, perms in grants.items():
        if not isinstance(key, str) or not KEY_RE.match(key):
            raise GrantError(f"'{key}' isn't a server.")
        if not isinstance(perms, (list, tuple)) or any(p not in PERMS for p in perms):
            raise GrantError(f"Unknown permission for {key}.")
        if _clean(perms):
            clean[key] = _clean(perms)
    with _lock:
        users = dict(_load())
        if clean:
            users[username] = clean
        else:
            users.pop(username, None)
        _save(users)
    return clean


def remove_user(username):
    with _lock:
        users = dict(_load())
        if users.pop(username, None) is not None:
            _save(users)


def forget_server(key):
    """A deleted server's grants go with it, so a new server that gets the same name isn't
    opened to whoever could use the old one."""
    with _lock:
        users, changed = {}, False
        for name, g in _load().items():
            if isinstance(g, dict) and key in g:
                g = {k: v for k, v in g.items() if k != key}
                changed = True
            if g:
                users[name] = g
        if changed:
            _save(users)
    from core import apitokens, discordauth, discordbot   # API tokens (1.25), Discord /start (1.27) and
    apitokens.forget_server(key)                          # Discord roles (1.28) lose it too
    discordbot.forget_server(key)
    discordauth.forget_server(key)


def rename_server(old_key, new_key):
    """A server moved to another cluster member (or renamed on the way) keeps its grants."""
    if old_key == new_key or not KEY_RE.match(new_key):
        return
    with _lock:
        users, changed = {}, False
        for name, g in _load().items():
            if isinstance(g, dict) and old_key in g:
                g = dict(g)
                g[new_key] = _clean(set(g.pop(old_key)) | set(g.get(new_key) or ()))
                changed = True
            users[name] = g
        if changed:
            _save(users)
    from core import apitokens, discordauth, discordbot   # API tokens (1.25), Discord /start (1.27) and
    apitokens.rename_server(old_key, new_key)             # Discord roles (1.28) follow it too
    discordbot.rename_server(old_key, new_key)
    discordauth.rename_server(old_key, new_key)


def migrate_existing_users(usernames):
    """Run once, the first time a 1.18 panel starts: `user` accounts made before per-server
    permissions keep what they could do (LEGACY on every server). Returns the accounts changed."""
    with _lock:
        if os.path.exists(GRANTS_FILE):
            return []
        users = {u: {ALL_SERVERS: list(LEGACY)} for u in usernames}
        _save(users)
        return sorted(users)


# ---------------------------------------------------------------------------
# What an identity may do
# ---------------------------------------------------------------------------
def perms_for(user, key):
    """The set of permissions `user` (request.state.user) has on the server with this key."""
    if user.get("role") == "admin":
        return set(PERMS)
    src = user.get("source")
    if src == "panel":
        g = get(user["username"])
        return set(g.get(ALL_SERVERS, ())) | set(g.get(key, ()))
    if src in ("token", "discord"):               # API tokens (1.25) and Discord roles (1.28) carry their own
        g = user.get("grants") or {}
        return set(g.get(ALL_SERVERS, ())) | set(g.get(key, ()))
    if src == "cluster":
        # A node trusts its master, which checked its own user's grants before passing the
        # request on and says what they are for this server. A master older than 1.18 sends
        # nothing: its users keep what users could always do.
        sent = user.get("perms")
        return set(LEGACY) if sent is None else set(sent)
    if src == "trusted":
        return set(LEGACY)
    return set()


def can(user, perm, server, member=None):
    return perm in perms_for(user, server_key(server, member))


def limited(user):
    """True for an account whose view is cut down to the servers it was granted."""
    return user.get("role") != "admin" and user.get("source") in ("panel", "token", "discord")


def parse_header(value):
    """X-RedstonePanel-Perms from the master -> a set, or None when it wasn't sent."""
    if value is None:
        return None
    return {p for p in value.split(",") if p in PERMS}


# ---------------------------------------------------------------------------
# One rule per API route
#   public    no sign-in (the middleware's PUBLIC_API)
#   user      any signed-in account (lists are cut down to what the account may see)
#   signed    like user, but not a trusted-LAN visitor: it's about one's own account
#   admin     admins only
#   node      cluster plumbing: only the master's node token (never a browser session)
#   proxy     a master user's request for a node: the rule of the route inside it applies
#   <perm>    that permission on {server_name}; "<perm>@<param>" when the server lives on the
#             cluster member named by that path parameter
# ---------------------------------------------------------------------------
_S = "/api/servers/{server_name}"
_RULES_TEXT = f"""
public   GET     /api/health
public   GET     /api/panel-info
public   POST    /api/auth/login
public   POST    /api/auth/login/code
public   GET     /api/auth/passkeys/available
public   POST    /api/auth/passkey/options
public   POST    /api/auth/passkey/login
public   POST    /api/auth/first-sign-in/check
public   POST    /api/auth/first-sign-in
public   POST    /api/auth/logout
public   GET     /api/auth/master-sign-in
public   GET     /api/auth/discord/available
public   GET     /api/auth/discord/start
public   GET     /api/auth/discord/callback
public   GET     /api/cluster/ca
public   GET     /api/cluster/ca.crt
public   POST    /api/cluster/join
user     GET     /api/phone-qr
user     GET     /api/auth/me
user     GET     /api/auth/2fa
user     POST    /api/auth/2fa/setup
user     POST    /api/auth/2fa/confirm
user     POST    /api/auth/2fa/recovery-codes
user     POST    /api/auth/2fa/disable
signed   GET     /api/auth/passkeys
signed   POST    /api/auth/passkeys/options
signed   POST    /api/auth/passkeys
signed   DELETE  /api/auth/passkeys/{{cred_id}}
signed   GET     /api/auth/devices
signed   DELETE  /api/auth/devices
signed   DELETE  /api/auth/devices/{{device_id}}
signed   POST    /api/auth/password
signed   GET     /api/auth/discord/link
signed   POST    /api/auth/discord/link
signed   DELETE  /api/auth/discord/link
signed   GET     /api/auth/ssh-keys
signed   POST    /api/auth/ssh-keys
signed   DELETE  /api/auth/ssh-keys/{{fingerprint}}
user     GET     /api/sftp
user     GET     /api/terminal
admin    POST    /api/terminal/ticket
admin    GET     /api/terminal/sessions
admin    DELETE  /api/terminal/sessions/{{sid}}
admin    GET     /api/uninstall
admin    POST    /api/uninstall/apply
admin    DELETE  /api/uninstall/plan
admin    POST    /api/uninstall/run
user     GET     /api/servers
user     GET     /api/host
user     GET     /api/host/history
user     GET     /api/logs
user     GET     /api/templates
user     GET     /api/java/versions
user     GET     /api/java/snapshots
user     GET     /api/java/builds
user     GET     /api/java/requirement
user     GET     /api/cluster/overview
user     GET     /api/cluster/hosts

view     GET     {_S}/perf
view     GET     {_S}/resources
view     GET     {_S}/crash
view     GET     {_S}/restarts
view     GET     {_S}/updates
power    POST    {_S}/action
power    DELETE  {_S}/crash
console  GET     {_S}/chat
console  POST    {_S}/chat
console  GET     {_S}/chat/assist
console  POST    {_S}/chat/setup
console  POST    {_S}/chat/clear
console  GET     {_S}/chat/debug
players  GET     {_S}/players
players  POST    {_S}/players/role
players  GET     {_S}/players/log
players  GET     {_S}/whitelist
players  POST    {_S}/whitelist/add
players  POST    {_S}/whitelist/remove
players  GET     {_S}/moderation
players  POST    {_S}/players/kick
players  POST    {_S}/players/ban
players  POST    {_S}/players/unban
players  GET     {_S}/permissions/raw
players  POST    {_S}/permissions/raw
settings GET     {_S}/properties
settings POST    {_S}/properties
settings GET     {_S}/properties/raw
settings POST    {_S}/properties/raw
settings PUT     {_S}/restarts
view     GET     {_S}/boot
settings PUT     {_S}/boot
view     GET     {_S}/sleep
settings PUT     {_S}/sleep
power    POST    {_S}/sleep/now
settings PUT     {_S}/updates
settings POST    {_S}/updates/check
view     GET     {_S}/convert
view     GET     {_S}/modpack
settings POST    {_S}/update
settings POST    {_S}/template/save
settings POST    {_S}/template/apply
settings POST    {_S}/worlds/rename
backups  GET     {_S}/backups
backups  POST    {_S}/backups
backups  PUT     {_S}/backups/schedule
backups  GET     {_S}/backups/{{filename}}
backups  GET     {_S}/backups/{{filename}}/sha256
backups  GET     {_S}/offsite
backups  POST    {_S}/offsite/sync
backups  GET     {_S}/worlds/export
files    GET     {_S}/export
files    GET     {_S}/files
files    GET     {_S}/files/text
files    PUT     {_S}/files/text
files    GET     {_S}/files/download
files    POST    {_S}/files/upload
files    POST    {_S}/files/mkdir
files    POST    {_S}/files/rename
files    DELETE  {_S}/files
mods     GET     {_S}/addons
mods     POST    {_S}/addons
mods     POST    {_S}/addons/toggle
mods     DELETE  {_S}/addons
mods     GET     {_S}/addons/search
mods     GET     {_S}/addons/catalog
mods     GET     {_S}/addons/details
mods     POST    {_S}/addons/bundle
mods     POST    {_S}/addons/install
mods     GET     {_S}/addons/updates
mods     GET     {_S}/player-pack
mods     PUT     {_S}/player-pack
mods     GET     {_S}/player-pack/file
mods     POST    {_S}/addons/update
mods     POST    {_S}/addons/undo
mods     GET     {_S}/addons/compatibility
admin    DELETE  {_S}
admin    PUT     {_S}/resources
admin    POST    {_S}/crash/turn-off
admin    POST    {_S}/crash/undo
admin    POST    {_S}/convert
admin    POST    {_S}/modpack/check
admin    POST    {_S}/modpack/update
admin    POST    {_S}/convert/undo
admin    POST    {_S}/update/rollback
admin    POST    {_S}/java
admin    POST    {_S}/jvm-flags
admin    GET     {_S}/phantom
admin    POST    {_S}/phantom/install
admin    POST    {_S}/phantom/{{action}}
admin    POST    {_S}/phantom-remove
admin    POST    {_S}/backups/{{filename}}/restore
admin    DELETE  {_S}/backups/{{filename}}
admin    PUT     {_S}/offsite/{{target_id}}
admin    POST    {_S}/offsite/{{target_id}}/fetch
admin    DELETE  {_S}/offsite/{{target_id}}/{{filename}}

admin    POST    /api/servers/deploy
admin    POST    /api/servers/modpack
admin    GET     /api/modpacks/search
admin    GET     /api/modpacks/versions
admin    POST    /api/servers/import
admin    GET     /api/servers/removed
admin    GET     /api/found-servers
admin    POST    /api/found-servers/manage
admin    POST    /api/found-servers/{{server_name}}/undo
admin    POST    /api/servers/removed/{{server_name}}/restore
admin    DELETE  /api/servers/removed/{{server_name}}
admin    PUT     /api/templates/{{template_id}}
admin    DELETE  /api/templates/{{template_id}}
admin    GET     /api/java/runtimes
admin    GET     /api/java/runtimes/job
admin    POST    /api/java/runtimes/install
admin    POST    /api/java/runtimes/remove
admin    GET     /api/panel/update
admin    POST    /api/panel/update/check
admin    POST    /api/panel/update
admin    GET     /api/join-pages
admin    PUT     /api/join-pages/page
admin    GET     {_S}/join-info
admin    GET     /api/port-forwarding
admin    GET     /api/port-forwarding/local
admin    PUT     /api/port-forwarding/settings
admin    POST    /api/port-forwarding/lookup
admin    POST    /api/port-forwarding/check
admin    GET     /api/port-check
admin    POST    /api/port-check/router
admin    POST    {_S}/ports/move

proxy            GET     /api/cluster/nodes/{{node_id}}/proxy/{{path:path}}
proxy            POST    /api/cluster/nodes/{{node_id}}/proxy/{{path:path}}
proxy            PUT     /api/cluster/nodes/{{node_id}}/proxy/{{path:path}}
proxy            DELETE  /api/cluster/nodes/{{node_id}}/proxy/{{path:path}}
power@node_id    POST    /api/cluster/nodes/{{node_id}}/servers/{{server_name}}/action
settings@node_id POST    /api/cluster/nodes/{{node_id}}/servers/{{server_name}}/template
admin            POST    /api/cluster/nodes/{{node_id}}/templates
admin            POST    /api/cluster/nodes/{{node_id}}/sign-in
backups@member_id GET    /api/cluster/backup-copies/{{member_id}}/{{server_name}}
backups@member_id POST   /api/cluster/backup-copies/{{member_id}}/{{server_name}}/sync
backups@panel    GET     /api/cluster/backup-copies/file/{{holder}}/{{panel}}/{{server_name}}/{{filename}}
admin            GET     /api/cluster/backup-copies
admin            PUT     /api/cluster/backup-copies/{{member_id}}/{{server_name}}
admin            POST    /api/cluster/backup-copies/file/{{holder}}/{{panel}}/{{server_name}}/{{filename}}/into
admin            DELETE  /api/cluster/backup-copies/file/{{holder}}/{{panel}}/{{server_name}}/{{filename}}
admin            POST    /api/cluster/moves
admin            GET     /api/cluster/moves
admin            GET     /api/cluster/moves/{{job_id}}
admin            DELETE  /api/cluster/moved/{{member_id}}/{{server_name}}

node     GET     /api/cluster/hello
node     POST    /api/cluster/sign-in
node     DELETE  /api/cluster/membership
node     POST    /api/cluster/cert/csr
node     PUT     /api/cluster/cert
node     POST    /api/cluster/templates
node     POST    /api/cluster/transfers/export
node     POST    /api/cluster/transfers/import
node     GET     /api/cluster/transfers/{{tid}}/archive
node     PUT     /api/cluster/transfers/{{tid}}/archive
node     POST    /api/cluster/transfers/{{tid}}/finish
node     POST    /api/cluster/transfers/{{tid}}/install
node     POST    /api/cluster/transfers/{{tid}}/stage
node     POST    /api/cluster/transfers/{{tid}}/confirm
node     DELETE  /api/cluster/transfers/{{tid}}
node     DELETE  /api/cluster/transfers/moved/{{server_name}}
node     GET     /api/cluster/held-backups
node     GET     /api/cluster/held-backups/{{panel}}/{{server_name}}/{{filename}}
node     GET     /api/cluster/held-backups/{{panel}}/{{server_name}}/{{filename}}/sha256
node     PUT     /api/cluster/held-backups/into/{{server_name}}/{{filename}}
node     PUT     /api/cluster/held-backups/{{panel}}/{{server_name}}/{{filename}}
node     DELETE  /api/cluster/held-backups/{{panel}}/{{server_name}}/{{filename}}
"""
# /api/users* and /api/settings* are admin-only by prefix (main.ADMIN_API), whatever is added there.
ADMIN_PREFIXES = ("/api/users", "/api/settings")

RULES = {}
for _line in _RULES_TEXT.strip().splitlines():
    if _line.strip():
        _rule, _method, _path = _line.split()
        RULES[(_method, _path)] = _rule


def rule_for(method, template):
    """The rule of a route (its FastAPI path template). Unlisted routes need an admin."""
    if template.startswith(ADMIN_PREFIXES):
        return "admin"
    return RULES.get(("GET" if method == "HEAD" else method, template), "admin")


def needs(rule):
    """(permission, path parameter naming the cluster member or None) for a per-server rule,
    else None."""
    perm, _, param = rule.partition("@")
    return (perm, param or None) if perm in PERMS else None


def refusal(perm):
    return (f"Your account doesn't have \"{PERM_LABELS[perm]}\" on this server. "
            "An admin can change that under Users.")
