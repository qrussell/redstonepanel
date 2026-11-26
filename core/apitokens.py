"""API tokens (1.25): scripts and bots use the panel's own API with a token an admin made.

A token is sent as `Authorization: Bearer rpa_...`. It acts like a panel account with the
`user` role whose per-server permissions (core/grants.py: view, console, power, players, files,
mods, settings, backups) are set on the token itself, for chosen servers or every server
("*"). So a token reaches exactly what such an account could, and never what needs an admin:
users, panel settings, creating or deleting servers, Java, cluster membership. It can't
change an account (sign-in, passwords, two-factor, passkeys). Everything it changes is logged
under its name, "token:NAME".

Tokens live in DATA_DIR/api-tokens.json (0600): {"tokens": [{"id", "name", "hash", "grants",
"created", "created_by", "expires", "last_used", "last_ip"}]}. Only an scrypt hash is kept; the
token is shown once, when it's made. A token is "rpa_" + 8 hex id + "_" + 43 random characters.
Wrong tokens count toward the sign-in throttle of the address they came from.
"""
import hashlib
import json
import os
import re
import secrets
import threading
import time

from core import auth
from core import grants as grants_mod
from core.settings import DATA_DIR

TOKENS_FILE = os.path.join(DATA_DIR, "api-tokens.json")
PREFIX = "rpa_"
TOKEN_RE = re.compile(r"^rpa_([0-9a-f]{8})_[A-Za-z0-9_-]{43}$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}$")
MAX_TOKENS = 50
MAX_DAYS = 3650
CACHE_SECONDS = 60          # a checked token isn't run through scrypt again for this long
LAST_USED_EVERY = 60

_lock = threading.RLock()
_ok_cache = {}              # sha256(token) -> (cache expiry, token id)


class TokenError(ValueError):
    """A token request that can't be done (message is safe to show)."""


def _load():
    try:
        with open(TOKENS_FILE) as f:
            data = json.load(f)
        tokens = data.get("tokens") if isinstance(data, dict) else None
        return [t for t in tokens if isinstance(t, dict) and t.get("id")] if isinstance(tokens, list) else []
    except (OSError, ValueError):
        return []


def _save(tokens):
    os.makedirs(os.path.dirname(TOKENS_FILE), exist_ok=True)
    tmp = TOKENS_FILE + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump({"tokens": tokens}, f, indent=1)
    os.replace(tmp, TOKENS_FILE)


def clean_grants(raw):
    """{server key or "*": [perms]} -> the same, checked. "view" comes with any other permission."""
    if not isinstance(raw, dict) or not raw:
        raise TokenError("Choose at least one server and what the token may do there.")
    out = {}
    for key, perms in raw.items():
        if not isinstance(key, str) or not grants_mod.KEY_RE.match(key):
            raise TokenError(f"Not a server: {str(key)[:60]}")
        if not isinstance(perms, (list, tuple)) or not all(p in grants_mod.PERMS for p in perms):
            raise TokenError(f"Unknown permission for {key}.")
        chosen = set(perms)
        if chosen:
            chosen.add("view")
            out[key] = [p for p in grants_mod.PERMS if p in chosen]
    if not out:
        raise TokenError("Choose at least one thing the token may do.")
    return out


def public(t, now=None):
    now = time.time() if now is None else now
    return {"id": t["id"], "name": t["name"], "grants": t.get("grants") or {}, "created": t.get("created"),
            "created_by": t.get("created_by"), "expires": t.get("expires"), "last_used": t.get("last_used"),
            "last_ip": t.get("last_ip"), "expired": bool(t.get("expires") and t["expires"] <= now)}


def list_tokens():
    return [public(t) for t in _load()]


def create(name, grants, days=None, actor="?"):
    """Makes a token. Returns its public details plus "token", the only time it's shown."""
    name = str(name or "").strip()
    if not NAME_RE.match(name):
        raise TokenError("Give the token a name of up to 40 letters, digits, spaces, dots, dashes or underscores.")
    grants = clean_grants(grants)
    expires = None
    if days not in (None, "", 0):
        try:
            days = int(days)
        except (TypeError, ValueError):
            raise TokenError("Expiry must be a number of days.")
        if not 1 <= days <= MAX_DAYS:
            raise TokenError(f"Expiry must be between 1 and {MAX_DAYS} days, or never.")
        expires = int(time.time()) + days * 86400
    with _lock:
        tokens = _load()
        if any(t["name"].lower() == name.lower() for t in tokens):
            raise TokenError(f"There's already a token called {name}.")
        if len(tokens) >= MAX_TOKENS:
            raise TokenError(f"A panel can have at most {MAX_TOKENS} tokens. Revoke one first.")
        tid = secrets.token_hex(4)
        while any(t["id"] == tid for t in tokens):
            tid = secrets.token_hex(4)
        token = f"{PREFIX}{tid}_{secrets.token_urlsafe(32)}"
        rec = {"id": tid, "name": name, "hash": auth.hash_password(token), "grants": grants,
               "created": int(time.time()), "created_by": actor, "expires": expires,
               "last_used": None, "last_ip": None}
        tokens.append(rec)
        _save(tokens)
    return dict(public(rec), token=token)


def revoke(token_id):
    """Removes a token; requests with it are refused at once. Returns its name."""
    with _lock:
        tokens = _load()
        rec = next((t for t in tokens if t["id"] == token_id), None)
        if not rec:
            raise TokenError("No such token.")
        _save([t for t in tokens if t is not rec])
        _ok_cache.clear()
    return rec["name"]


def forget_server(key):
    """A server deleted for good: tokens lose their permissions on it (as accounts do)."""
    with _lock:
        tokens = _load()
        changed = False
        for t in tokens:
            if key in (t.get("grants") or {}):
                del t["grants"][key]
                changed = True
        if changed:
            _save(tokens)


def rename_server(old_key, new_key):
    """A server moved to another cluster member keeps the tokens' permissions on it."""
    if old_key == new_key or not grants_mod.KEY_RE.match(new_key):
        return
    with _lock:
        tokens = _load()
        changed = False
        for t in tokens:
            g = t.get("grants") or {}
            if old_key in g:
                both = set(g.pop(old_key)) | set(g.get(new_key) or ())
                g[new_key] = [p for p in grants_mod.PERMS if p in both]
                changed = True
        if changed:
            _save(tokens)


def read(token, ip):
    """The identity a request with this token acts as, or None. Counts failures toward the
    sign-in throttle for the address."""
    m = TOKEN_RE.match(token or "")
    keys = (f"ip:{ip}",)
    if not m:
        return None
    if auth._throttled(keys):
        return None
    tid = m.group(1)
    digest = hashlib.sha256(token.encode()).hexdigest()
    now = time.time()
    with _lock:
        rec = next((t for t in _load() if t["id"] == tid), None)
        hit = _ok_cache.get(digest)
    if rec is None:
        auth._record_fail(keys)
        return None
    if not (hit and hit[0] > now and hit[1] == tid):
        if not auth.verify_password(token, rec.get("hash", "")):
            auth._record_fail(keys)
            return None
        with _lock:
            _ok_cache[digest] = (now + CACHE_SECONDS, tid)
    if rec.get("expires") and rec["expires"] <= now:
        return None
    if now - int(rec.get("last_used") or 0) > LAST_USED_EVERY or rec.get("last_ip") != ip:
        with _lock:
            tokens = _load()
            for t in tokens:
                if t["id"] == tid:
                    t.update(last_used=int(now), last_ip=ip)
            _save(tokens)
    return {"username": f"token:{rec['name']}", "source": "token", "role": "user", "token_id": tid,
            "grants": rec.get("grants") or {}}
