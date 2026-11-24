"""RedstonePanel authentication.

Two kinds of accounts can sign in:
  * Linux accounts on this host, checked through PAM (same rules as a console login:
    locked/expired accounts are refused). Only members of ALLOWED_LINUX_GROUPS may use
    the panel, because the panel runs as root. Linux users are always panel admins.
  * Panel accounts stored in data/users.json, managed from the panel's Users screen.
    Passwords are never stored: only a salted scrypt hash (one-way, can't be decrypted).

Sessions are a signed cookie (HMAC-SHA256 with a random key in data/secret.key).

Two-factor sign-in (core/twofactor.py): when an account has it on, the right password only earns
a short-lived "code needed" ticket (its own cookie, signed for that purpose only); the session
cookie comes after a code from the authenticator app or a recovery code. Wrong codes count
towards the same rate limit as wrong passwords. A session records that it passed the second
step, so turning two-factor on signs out sessions that never did.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time

from core import settings as panel_settings
from core import twofactor
from core.settings import DATA_DIR   # <app>/data (RP_DATA_DIR overrides it for test instances)
from core.platform import paths, system
USERS_FILE = os.path.join(DATA_DIR, "users.json")
SECRET_FILE = os.path.join(DATA_DIR, "secret.key")

ALLOWED_LINUX_GROUPS = ("sudo", "redstonepanel")
PAM_SERVICE = "login"
PANEL_ROLES = ("admin", "user")          # admin = everything incl. user management
SESSION_COOKIE = "rp_session"
SESSION_SECONDS = 12 * 3600
PENDING_COOKIE = "rp_2fa"                # password checked, waiting for the second step
PENDING_SECONDS = 5 * 60
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
            "created": u.get("created"), "last_login": u.get("last_login"),
            "two_factor": twofactor.is_enabled(twofactor.account_key("panel", u["username"]))}

def linux_account_exists(username):
    return system.system_account_exists(username)

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
        # 1.18's one-time step gives `user` accounts made before per-server permissions their old
        # rights. The panel runs it as it starts, but an account can be added before that (the
        # `redstonepanel add-user` right after installing): run it here first, or the new account
        # would be counted as an old one and get every server
        from core import grants
        grants.migrate_existing_users([u["username"] for u in users if u.get("role") == "user"])
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
    twofactor.remove(twofactor.account_key("panel", username))

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
# Accounts of the system itself (Linux: PAM and the sudo group). The check lives in
# core/platform; these names stay because the rest of the panel and its tests use them.
# ---------------------------------------------------------------------------
def pam_authenticate(username, password):
    """True if the system accepts the password and the account is usable (not locked/expired)."""
    return system.check_system_password(username, password)

def linux_user_allowed(username):
    """The system account exists and may sign in to the panel (Linux: in ALLOWED_LINUX_GROUPS)."""
    return system.system_account_allowed(username, ALLOWED_LINUX_GROUPS)


# ---------------------------------------------------------------------------
# Login + throttling
# ---------------------------------------------------------------------------
# The failures are shared with the SFTP service (core/sftp.py, its own process): on an installed
# panel they are kept in a small file under /run, so guessing over SFTP and in the browser counts
# towards one limit. Elsewhere (tests, a copy run by hand) they stay in memory. Starting the panel
# clears them, as it always did.
FAILS_FILE = os.environ.get("RP_FAILS_FILE") or (os.path.join(paths.RUN_DIR, "signin-fails.json")
                                                 if panel_settings.PACKAGED else None)

def _with_fails(fn):
    """Runs fn(store) on the failure store ({key: [timestamps]}) and saves what it changed."""
    with _lock:
        if not FAILS_FILE:
            return fn(_fails)
        try:
            os.makedirs(os.path.dirname(FAILS_FILE), mode=0o700, exist_ok=True)
            fd = os.open(FAILS_FILE, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except OSError:
            return fn(_fails)
        with os.fdopen(fd, "r+") as f:
            system.lock(f.fileno(), wait=True)
            try:
                store = json.load(f)
                if not isinstance(store, dict):
                    store = {}
            except ValueError:
                store = {}
            before = json.dumps(store, sort_keys=True)
            out = fn(store)
            after = json.dumps({k: v for k, v in store.items() if v}, sort_keys=True)
            if after != before:
                f.seek(0)
                f.truncate()
                f.write(after)
            return out

def reset_fails():
    """Called when the panel starts."""
    with _lock:
        _fails.clear()
        if FAILS_FILE:
            try:
                os.unlink(FAILS_FILE)
            except OSError:
                pass

def _recent_fails(store, key, now):
    lst = [t for t in store.get(key, []) if isinstance(t, (int, float)) and now - t < FAIL_WINDOW]
    store[key] = lst
    return lst

def _throttled(keys):
    now = time.time()
    return _with_fails(lambda store: any(len(_recent_fails(store, k, now)) >= MAX_FAILS for k in keys))

def _record_fail(keys):
    now = time.time()
    def add(store):
        for k in keys:
            _recent_fails(store, k, now).append(now)
    _with_fails(add)

def _clear_fails(keys):
    def drop(store):
        for k in keys:
            store.pop(k, None)
    _with_fails(drop)

# The same store, for other kinds of passwords (the terminal's: core/terminal.py)
def throttled(keys):
    return _throttled(keys)

def record_fail(keys):
    _record_fail(keys)

def clear_fails(keys):
    _clear_fails(keys)

def fail_keys(username, client_ip):
    return (f"user:{(username or '').lower()}", f"ip:{client_ip}")

def account_role(source, username):
    """'admin' or 'user' for an account that may use the panel right now, else None (deleted,
    disabled, or a Linux account no longer in an allowed group)."""
    if source == "linux":
        return "admin" if linux_user_allowed(username) else None
    if source == "panel":
        u = _find(_load_users(), username)
        return u["role"] if u and not u.get("disabled") else None
    return None

def find_account(username):
    """(source, name) of the account a sign-in name means, as login() picks it, or None."""
    name = (username or "").strip()
    if _find(_load_users(), name.lower()):
        return "panel", name.lower()
    if name and linux_account_exists(name):
        return "linux", name
    return None

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
    if twofactor.is_enabled(twofactor.account_key(identity["src"], identity["u"])):
        # Not signed in yet, so the failure count stays: guessing codes and re-entering the
        # password must not reset the limit.
        return dict(identity, pending=True)
    _clear_fails(keys)
    return identity


def complete_two_factor(ticket, code, client_ip):
    """Second step of signing in. Returns the session identity, or raises AuthError."""
    identity = read_pending_token(ticket)
    if not identity:
        raise AuthError("The sign-in took too long. Enter your password again.")
    keys = (f"user:{identity['u'].lower()}", f"ip:{client_ip}")
    if _throttled(keys):
        raise AuthError("Too many failed sign-in attempts. Try again in a few minutes.")
    used = twofactor.verify(twofactor.account_key(identity["src"], identity["u"]), code)
    if not used:
        _record_fail(keys)
        raise AuthError("That code is wrong or was already used.")
    _clear_fails(keys)
    return dict(identity, m=1, used=used)


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

# Session cookies and "code needed" tickets are signed with different keys derived from the
# same secret, so one can never be used as the other.
def _signing_key(purpose):
    if purpose == "session":
        return _get_secret()
    return hmac.new(_get_secret(), f"redstonepanel {purpose}".encode(), hashlib.sha256).digest()

def _sign(payload, purpose):
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    sig = hmac.new(_signing_key(purpose), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"

def _unsign(token, purpose):
    if not token or "." not in token:
        return None
    body, sig = token.rsplit(".", 1)
    expected = hmac.new(_signing_key(purpose), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("exp", 0) < time.time():
        return None
    return payload

def _identity_fields(identity):
    return {k: identity[k] for k in ("u", "src", "v", "m") if k in identity}

def make_session_token(identity):
    # n: each sign-in's cookie is its own, even two in the same second (signing one out, which
    # closes its terminals, must not touch the other)
    return _sign(dict(_identity_fields(identity), exp=int(time.time()) + SESSION_SECONDS, n=secrets.token_hex(8)),
                 "session")

def session_started(token):
    """When a valid session cookie was signed in (epoch seconds), else None. A session is only
    ever made at sign-in, so it is its expiry less its length."""
    payload = _unsign(token, "session")
    return payload["exp"] - SESSION_SECONDS if payload else None

def make_pending_token(identity):
    fields = {k: identity[k] for k in ("u", "src", "v")}
    return _sign(dict(fields, exp=int(time.time()) + PENDING_SECONDS), "two-factor pending")

def read_pending_token(token):
    payload = _unsign(token, "two-factor pending")
    if not payload or payload.get("src") not in ("panel", "linux"):
        return None
    return {k: payload.get(k) for k in ("u", "src", "v")}

def two_factor_required_for(role):
    return role == "admin" and bool(panel_settings.load().get("require_2fa_admins"))

def read_session_token(token):
    """Returns the current user {username, source, role, two_factor, needs_2fa_setup} or None.
    Re-checks the account every time, so deleting/disabling a user or removing them from the
    allowed groups takes effect at once."""
    payload = _unsign(token, "session")
    if not payload:
        return None

    username, src = payload.get("u"), payload.get("src")
    if src == "linux":
        if not linux_user_allowed(username):
            return None
        user = {"username": username, "source": "linux", "role": "admin"}
    elif src == "panel":
        u = _find(_load_users(), username)
        if not u or u.get("disabled") or u.get("session_version", 1) != payload.get("v"):
            return None
        user = {"username": username, "source": "panel", "role": u["role"]}
    else:
        return None
    # Two-factor is on for this account but this session never passed it (signed in before it was
    # turned on, elsewhere): not valid any more.
    tf = twofactor.is_enabled(twofactor.account_key(src, username))
    if tf and not payload.get("m"):
        return None
    user["two_factor"] = tf
    user["needs_2fa_setup"] = not tf and two_factor_required_for(user["role"])
    return user
