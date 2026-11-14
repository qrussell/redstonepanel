"""RedstonePanel authentication.

Two kinds of accounts can sign in:
  * Linux accounts on this host, checked through PAM (same rules as a console login:
    locked/expired accounts are refused). Only members of ALLOWED_LINUX_GROUPS may use
    the panel, because the panel runs as root. Linux users are always panel admins.
  * Panel accounts stored in data/users.json, managed from the panel's Users screen.
    Passwords are never stored: only a salted scrypt hash (one-way, can't be decrypted).

Sessions are a signed cookie (HMAC-SHA256 with a random key in data/secret.key).
"""
import base64
import ctypes
import ctypes.util
import grp
import hashlib
import hmac
import json
import os
import pwd
import re
import secrets
import threading
import time

from core.settings import DATA_DIR   # <app>/data (RP_DATA_DIR overrides it for test instances)
USERS_FILE = os.path.join(DATA_DIR, "users.json")
SECRET_FILE = os.path.join(DATA_DIR, "secret.key")

ALLOWED_LINUX_GROUPS = ("sudo", "redstonepanel")
PAM_SERVICE = "login"
PANEL_ROLES = ("admin", "user")          # admin = everything incl. user management
SESSION_COOKIE = "rp_session"
SESSION_SECONDS = 12 * 3600
MIN_PASSWORD_LEN = 8
USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_.-]{1,31}$")

# Failed-login throttling (in memory; a restart clears it)
MAX_FAILS = 5
FAIL_WINDOW = 10 * 60

_lock = threading.Lock()
_fails = {}   # key -> [timestamps]


class AuthError(ValueError):
    """Bad credentials or a request the auth layer refuses (message is safe to show)."""


# ---------------------------------------------------------------------------
# Panel password hashing (scrypt)
# ---------------------------------------------------------------------------
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 14, 8, 1

def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    b64 = lambda b: base64.b64encode(b).decode()
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${b64(salt)}${b64(digest)}"

def verify_password(password, stored):
    try:
        algo, n, r, p, salt, digest = stored.split("$")
        if algo != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt),
                                n=int(n), r=int(r), p=int(p), dklen=len(expected))
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False

# A real hash to check against when the user doesn't exist, so timing doesn't reveal valid names
_DUMMY_HASH = hash_password(secrets.token_hex(16))


# ---------------------------------------------------------------------------
# users.json
# ---------------------------------------------------------------------------
def _load_users():
    try:
        with open(USERS_FILE, "r") as f:
            data = json.load(f)
        return data.get("users", []) if isinstance(data, dict) else []
    except FileNotFoundError:
        return []

def _save_users(users):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = USERS_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"users": users}, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, USERS_FILE)

def _find(users, username):
    return next((u for u in users if u["username"] == username), None)

def _public(u):
    return {"username": u["username"], "role": u["role"], "disabled": bool(u.get("disabled")),
            "created": u.get("created"), "last_login": u.get("last_login")}

def linux_account_exists(username):
    try:
        pwd.getpwnam(username)
        return True
    except KeyError:
        return False

def list_panel_users():
    return [_public(u) for u in _load_users()]

def create_panel_user(username, password, role):
    username = (username or "").strip().lower()
    if not USERNAME_RE.match(username):
        raise AuthError("Username must be 2-32 characters: lowercase letters, digits, _ . - (start with a letter or _).")
    if linux_account_exists(username):
        raise AuthError(f"'{username}' is a Linux account on this server; that person can already sign in with it.")
    if role not in PANEL_ROLES:
        raise AuthError("Role must be admin or user.")
    _check_password_rules(password)
    with _lock:
        users = _load_users()
        if _find(users, username):
            raise AuthError(f"Panel user '{username}' already exists.")
        users.append({"username": username, "password_hash": hash_password(password), "role": role,
                      "disabled": False, "session_version": 1,
                      "created": int(time.time()), "last_login": None})
        _save_users(users)

def update_panel_user(username, password=None, role=None, disabled=None):
    with _lock:
        users = _load_users()
        u = _find(users, username)
        if not u:
            raise AuthError(f"Panel user '{username}' not found.")
        if role is not None:
            if role not in PANEL_ROLES:
                raise AuthError("Role must be admin or user.")
            u["role"] = role
        if password is not None:
            _check_password_rules(password)
            u["password_hash"] = hash_password(password)
        if disabled is not None:
            u["disabled"] = bool(disabled)
        if password is not None or disabled:
            # Sign the user out everywhere
            u["session_version"] = int(u.get("session_version", 1)) + 1
        _save_users(users)

def delete_panel_user(username):
    with _lock:
        users = _load_users()
        if not _find(users, username):
            raise AuthError(f"Panel user '{username}' not found.")
        _save_users([u for u in users if u["username"] != username])

def change_own_password(username, current, new):
    with _lock:
        u = _find(_load_users(), username)
    if not u or not verify_password(current, u["password_hash"]):
        raise AuthError("Current password is incorrect.")
    update_panel_user(username, password=new)

def _check_password_rules(password):
    if not password or len(password) < MIN_PASSWORD_LEN:
        raise AuthError(f"Password must be at least {MIN_PASSWORD_LEN} characters.")
    if len(password) > 256:
        raise AuthError("Password is too long.")


# ---------------------------------------------------------------------------
# Linux accounts via PAM (ctypes; no extra packages)
# ---------------------------------------------------------------------------
class _PamHandle(ctypes.Structure):
    _fields_ = [("handle", ctypes.c_void_p)]

class _PamMessage(ctypes.Structure):
    _fields_ = [("msg_style", ctypes.c_int), ("msg", ctypes.c_char_p)]

class _PamResponse(ctypes.Structure):
    _fields_ = [("resp", ctypes.c_void_p), ("resp_retcode", ctypes.c_int)]

_CONV_FUNC = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int,
                              ctypes.POINTER(ctypes.POINTER(_PamMessage)),
                              ctypes.POINTER(ctypes.POINTER(_PamResponse)), ctypes.c_void_p)

class _PamConv(ctypes.Structure):
    _fields_ = [("conv", _CONV_FUNC), ("appdata_ptr", ctypes.c_void_p)]

_PAM_PROMPT_ECHO_OFF, _PAM_PROMPT_ECHO_ON = 1, 2
_libpam = _libc = None

def _load_pam():
    global _libpam, _libc
    if _libpam is None:
        _libpam = ctypes.CDLL(ctypes.util.find_library("pam") or "libpam.so.0")
        _libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6")
        _libc.calloc.restype = ctypes.c_void_p
        _libc.calloc.argtypes = [ctypes.c_size_t, ctypes.c_size_t]
        _libc.strdup.restype = ctypes.c_void_p
        _libc.strdup.argtypes = [ctypes.c_char_p]
        _libpam.pam_start.restype = ctypes.c_int
        _libpam.pam_start.argtypes = [ctypes.c_char_p, ctypes.c_char_p,
                                      ctypes.POINTER(_PamConv), ctypes.POINTER(_PamHandle)]
        for fn in (_libpam.pam_authenticate, _libpam.pam_acct_mgmt, _libpam.pam_end):
            fn.restype = ctypes.c_int
            fn.argtypes = [_PamHandle, ctypes.c_int]

def pam_authenticate(username, password):
    """True if PAM accepts the password and the account is usable (not locked/expired)."""
    _load_pam()
    pw = password.encode()

    @_CONV_FUNC
    def conv(n, messages, p_response, _appdata):
        # PAM frees these with free(), so they must come from calloc/strdup
        addr = _libc.calloc(n, ctypes.sizeof(_PamResponse))
        if not addr:
            return 5   # PAM_BUF_ERR
        resp = ctypes.cast(addr, ctypes.POINTER(_PamResponse))
        p_response[0] = resp
        for i in range(n):
            if messages[i].contents.msg_style in (_PAM_PROMPT_ECHO_OFF, _PAM_PROMPT_ECHO_ON):
                resp[i].resp = _libc.strdup(pw)
                resp[i].resp_retcode = 0
        return 0

    handle = _PamHandle()
    conv_struct = _PamConv(conv, None)
    rc = _libpam.pam_start(PAM_SERVICE.encode(), username.encode(), ctypes.byref(conv_struct), ctypes.byref(handle))
    if rc != 0:
        return False
    try:
        rc = _libpam.pam_authenticate(handle, 0)
        if rc == 0:
            rc = _libpam.pam_acct_mgmt(handle, 0)
        return rc == 0
    finally:
        _libpam.pam_end(handle, rc)

def linux_user_allowed(username):
    """Linux account exists and is in one of ALLOWED_LINUX_GROUPS."""
    try:
        pw = pwd.getpwnam(username)
    except KeyError:
        return False
    gids = set(os.getgrouplist(username, pw.pw_gid))
    for name in ALLOWED_LINUX_GROUPS:
        try:
            if grp.getgrnam(name).gr_gid in gids:
                return True
        except KeyError:
            continue
    return False


# ---------------------------------------------------------------------------
# Login + throttling
# ---------------------------------------------------------------------------
def _recent_fails(key, now):
    lst = [t for t in _fails.get(key, []) if now - t < FAIL_WINDOW]
    _fails[key] = lst
    return lst

def _throttled(keys):
    now = time.time()
    with _lock:
        return any(len(_recent_fails(k, now)) >= MAX_FAILS for k in keys)

def _record_fail(keys):
    now = time.time()
    with _lock:
        for k in keys:
            _recent_fails(k, now).append(now)

def _clear_fails(keys):
    with _lock:
        for k in keys:
            _fails.pop(k, None)

def login(username, password, client_ip):
    """Returns the session identity dict, or raises AuthError with a generic message."""
    username = (username or "").strip()
    keys = (f"user:{username.lower()}", f"ip:{client_ip}")
    if _throttled(keys):
        raise AuthError("Too many failed sign-in attempts. Try again in a few minutes.")
    if not username or not password:
        raise AuthError("Enter a username and password.")

    identity = None
    panel_user = _find(_load_users(), username.lower())
    if panel_user:
        if verify_password(password, panel_user["password_hash"]) and not panel_user.get("disabled"):
            identity = {"u": panel_user["username"], "src": "panel", "v": panel_user.get("session_version", 1)}
            with _lock:
                users = _load_users()
                u = _find(users, panel_user["username"])
                if u:
                    u["last_login"] = int(time.time())
                    _save_users(users)
    elif linux_account_exists(username):
        if linux_user_allowed(username) and pam_authenticate(username, password):
            identity = {"u": username, "src": "linux", "v": 0}
    else:
        verify_password(password, _DUMMY_HASH)   # same work as a real check

    if not identity:
        _record_fail(keys)
        raise AuthError("Invalid username or password.")
    _clear_fails(keys)
    return identity


# ---------------------------------------------------------------------------
# Session cookie
# ---------------------------------------------------------------------------
_secret = None

def _get_secret():
    global _secret
    if _secret is None:
        try:
            with open(SECRET_FILE, "rb") as f:
                _secret = f.read().strip()
        except FileNotFoundError:
            os.makedirs(DATA_DIR, exist_ok=True)
            key = secrets.token_hex(32).encode()
            fd = os.open(SECRET_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(key)
            _secret = key
    return _secret

def make_session_token(identity):
    payload = dict(identity, exp=int(time.time()) + SESSION_SECONDS)
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    sig = hmac.new(_get_secret(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"

def read_session_token(token):
    """Returns the current user {username, source, role} or None. Re-checks the account every time,
    so deleting/disabling a user or removing them from the allowed groups takes effect at once."""
    if not token or "." not in token:
        return None
    body, sig = token.rsplit(".", 1)
    expected = hmac.new(_get_secret(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except Exception:
        return None
    if payload.get("exp", 0) < time.time():
        return None

    username, src = payload.get("u"), payload.get("src")
    if src == "linux":
        if not linux_user_allowed(username):
            return None
        return {"username": username, "source": "linux", "role": "admin"}
    if src == "panel":
        u = _find(_load_users(), username)
        if not u or u.get("disabled") or u.get("session_version", 1) != payload.get("v"):
            return None
        return {"username": username, "source": "panel", "role": u["role"]}
    return None
