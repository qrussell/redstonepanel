"""Two-factor sign-in (TOTP, RFC 6238) and recovery codes.

Works for panel accounts and Linux (PAM) accounts alike. Each account is keyed by where it comes
from ("panel:alex", "linux:quentin"), so a Linux account and a panel account never share a secret.

data/totp.json (0600, root only):
    {"accounts": {"panel:alex": {"secret": "BASE32", "enabled": true, "created": 1700000000,
                                  "last_step": 56666666, "recovery": ["sha256 hex", ...]},
                  "linux:quentin": {"secret": "BASE32", "enabled": false, ...}}}   # setup not confirmed yet

Codes are the six digits from any authenticator app: SHA-1, 30-second steps, one step of clock
drift either way. A code can't be used twice (last_step). Recovery codes are ten one-use codes
shown once at setup; only their SHA-256 is stored.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import struct
import threading
import time
import urllib.parse

from core.settings import DATA_DIR

TOTP_FILE = os.path.join(DATA_DIR, "totp.json")
STEP = 30
DIGITS = 6
DRIFT = 1                      # accept the previous and next 30-second code too
RECOVERY_COUNT = 10
SETUP_SECONDS = 15 * 60        # an unconfirmed setup is forgotten after this
ISSUER = "RedstonePanel"

_lock = threading.Lock()
_cache = {"mtime": None, "data": None}


class TwoFactorError(ValueError):
    """A request the two-factor layer refuses (message is safe to show)."""


def account_key(source, username):
    return f"{source}:{username}"


# ---------------------------------------------------------------------------
# totp.json
# ---------------------------------------------------------------------------
def _load():
    try:
        mtime = os.path.getmtime(TOTP_FILE)
    except OSError:
        return {"accounts": {}}
    if _cache["data"] is None or _cache["mtime"] != mtime:
        try:
            with open(TOTP_FILE) as f:
                data = json.load(f)
            if not isinstance(data, dict) or not isinstance(data.get("accounts"), dict):
                raise ValueError
        except Exception:
            # Unreadable: treat every account as having two-factor on but no usable secret, so a
            # damaged file locks the affected accounts (recoverable with reset-2fa) rather than
            # silently dropping the second step. Accounts without a stored entry are unaffected.
            data = {"accounts": {}, "damaged": True}
        _cache.update(mtime=mtime, data=data)
    return json.loads(json.dumps(_cache["data"]))


def _save(data):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = TOTP_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"accounts": data["accounts"]}, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, TOTP_FILE)
    _cache["data"] = None


# ---------------------------------------------------------------------------
# TOTP
# ---------------------------------------------------------------------------
def new_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _key_bytes(secret):
    s = secret.upper().replace(" ", "")
    return base64.b32decode(s + "=" * (-len(s) % 8))


def code_at(secret, step):
    digest = hmac.new(_key_bytes(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    off = digest[-1] & 0x0F
    num = struct.unpack(">I", digest[off:off + 4])[0] & 0x7FFFFFFF
    return str(num % 10 ** DIGITS).zfill(DIGITS)


def _matching_step(secret, code, now=None):
    """The time step the code belongs to (within the drift window), or None."""
    step = int((time.time() if now is None else now) // STEP)
    for s in range(step - DRIFT, step + DRIFT + 1):
        if hmac.compare_digest(code_at(secret, s), code):
            return s
    return None


def provisioning_uri(secret, username, panel_name):
    label = urllib.parse.quote(f"{ISSUER} ({panel_name}):{username}" if panel_name else f"{ISSUER}:{username}",
                               safe="")
    q = urllib.parse.urlencode({"secret": secret, "issuer": ISSUER, "digits": DIGITS, "period": STEP},
                               quote_via=urllib.parse.quote)
    return f"otpauth://totp/{label}?{q}"


def _clean(code):
    return re.sub(r"[\s-]", "", str(code or "")).lower()


# ---------------------------------------------------------------------------
# Recovery codes
# ---------------------------------------------------------------------------
_RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"   # no 0/o, 1/l/i


def _new_recovery_codes():
    codes = []
    for _ in range(RECOVERY_COUNT):
        raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(10))
        codes.append(f"{raw[:5]}-{raw[5:]}")
    return codes


def _hash_recovery(code):
    return hashlib.sha256(_clean(code).encode()).hexdigest()


# ---------------------------------------------------------------------------
# Account state
# ---------------------------------------------------------------------------
def status(key):
    """{"enabled": bool, "recovery_left": int}"""
    data = _load()
    a = data["accounts"].get(key)
    if data.get("damaged"):
        return {"enabled": True, "recovery_left": 0, "damaged": True}
    if not a or not a.get("enabled"):
        return {"enabled": False, "recovery_left": 0}
    return {"enabled": True, "recovery_left": len(a.get("recovery", []))}


def is_enabled(key):
    return status(key)["enabled"]


def enabled_keys():
    return {k for k, a in _load()["accounts"].items() if a.get("enabled")}


def begin_setup(key):
    """Start (or restart) setup: a new secret, not used for sign-in until confirm_setup."""
    with _lock:
        data = _load()
        a = data["accounts"].get(key)
        if a and a.get("enabled"):
            raise TwoFactorError("Two-factor sign-in is already on for this account. Turn it off first to set up a new app.")
        secret = new_secret()
        data["accounts"][key] = {"secret": secret, "enabled": False, "created": int(time.time())}
        _save(data)
    return secret


def confirm_setup(key, code, now=None):
    """Checks a code from the app; turns two-factor on and returns the ten recovery codes."""
    with _lock:
        data = _load()
        a = data["accounts"].get(key)
        if not a or a.get("enabled"):
            raise TwoFactorError("Start the setup again.")
        if (time.time() if now is None else now) - a.get("created", 0) > SETUP_SECONDS:
            raise TwoFactorError("This setup has expired. Start again.")
        step = _matching_step(a["secret"], _clean(code), now)
        if step is None:
            raise TwoFactorError("That code doesn't match. Check the phone's clock and try the newest code.")
        codes = _new_recovery_codes()
        a.update(enabled=True, last_step=step, recovery=[_hash_recovery(c) for c in codes],
                 enabled_at=int(time.time()))
        _save(data)
    return codes


def verify(key, code, now=None):
    """Checks a sign-in code: a current app code, or an unused recovery code (used up on success).
    Returns "totp", "recovery" or None."""
    c = _clean(code)
    if not c:
        return None
    with _lock:
        data = _load()
        a = data["accounts"].get(key)
        if not a or not a.get("enabled"):
            return None
        if re.fullmatch(r"\d{%d}" % DIGITS, c):
            step = _matching_step(a["secret"], c, now)
            if step is None or step <= a.get("last_step", -1):
                return None   # wrong, or already used (replay)
            a["last_step"] = step
            _save(data)
            return "totp"
        h = _hash_recovery(c)
        stored = a.get("recovery", [])
        match = next((x for x in stored if hmac.compare_digest(x, h)), None)
        if not match:
            return None
        stored.remove(match)
        a["recovery"] = stored
        _save(data)
        return "recovery"


def new_recovery_codes(key):
    """Replaces the recovery codes (caller has checked a code first)."""
    with _lock:
        data = _load()
        a = data["accounts"].get(key)
        if not a or not a.get("enabled"):
            raise TwoFactorError("Two-factor sign-in is not on for this account.")
        codes = _new_recovery_codes()
        a["recovery"] = [_hash_recovery(c) for c in codes]
        _save(data)
    return codes


def remove(key):
    """Turns two-factor off for an account (also forgets an unfinished setup). True if there was one."""
    with _lock:
        data = _load()
        if key not in data["accounts"]:
            return False
        del data["accounts"][key]
        _save(data)
    return True


def setup_secret(key):
    """The secret of an unfinished setup (to show again), or None."""
    a = _load()["accounts"].get(key)
    return a["secret"] if a and not a.get("enabled") else None
