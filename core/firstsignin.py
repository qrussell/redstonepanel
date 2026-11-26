"""The first sign-in after installing on Windows (1.24): choose your own password.

On a first install the Windows installer makes the panel account `admin` with a random password
(kept in first-sign-in.txt as a fallback) and a one-use link, valid for 7 days. The installer's
Finish page opens the link (open-panel.url in the home folder, readable only by administrators,
the system and the person who ran the installer), and the sign-in page asks for a new password
for `admin` instead of the random one. Using the link sets that password, signs in, forgets the
link, deletes first-sign-in.txt and points open-panel.url at the plain address.

The link carries the token after "#", so it never reaches a server log or another site.
data/first-sign-in.json (0600): {"user": "admin", "hash": "sha256 hex", "exp": 1700000000}
"""
import hashlib
import hmac
import json
import os
import secrets
import threading
import time

from core.settings import DATA_DIR
from core.platform import paths

FIRST_FILE = os.path.join(DATA_DIR, "first-sign-in.json")
OPEN_FILE = "open-panel.url"
NOTE_FILE = "first-sign-in.txt"
DAYS = 7

_lock = threading.Lock()


class FirstSignInError(ValueError):
    """Refused (message is safe to show)."""


def _home():
    return paths.HOME if paths.WINDOWS else os.path.dirname(DATA_DIR)


def _hash(token):
    return hashlib.sha256(str(token).encode()).hexdigest()


def create(username, days=DAYS):
    """A new one-use token for `username` (replaces an earlier one). Returns the token."""
    token = secrets.token_urlsafe(24)
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = FIRST_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"user": username, "hash": _hash(token), "exp": int(time.time() + days * 86400)}, f)
    os.replace(tmp, FIRST_FILE)
    return token


def _read():
    try:
        with open(FIRST_FILE) as f:
            d = json.load(f)
        if isinstance(d, dict) and d.get("user") and d.get("hash") and d.get("exp", 0) > time.time():
            return d
    except (OSError, ValueError):
        pass
    return None


def pending():
    """The account a valid first sign-in link is for, or None."""
    d = _read()
    return d["user"] if d else None


def check(token):
    """The account the token is for, or raises."""
    d = _read()
    if not d or not token or not hmac.compare_digest(d["hash"], _hash(token)):
        raise FirstSignInError("This first sign-in link was already used or has expired. Sign in with your "
                               "password (first-sign-in.txt in the RedstonePanel folder, if you never changed it).")
    return d["user"]


def link_text(url):
    return f"[InternetShortcut]\r\nURL={url}\r\n"


def write_open_link(url, home=None):
    """open-panel.url in the home folder: what the installer's Finish page opens."""
    path = os.path.join(home or _home(), OPEN_FILE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(link_text(url))
    os.replace(tmp, path)
    return path


def use(token, new_password, set_password, plain_url=None):
    """Checks the token, sets the account's password with set_password(user, pw) (which applies the
    password rules), and forgets the link. Returns the account name."""
    with _lock:
        user = check(token)
        set_password(user, new_password)
        try:
            os.remove(FIRST_FILE)
        except OSError:
            pass
    home = _home()
    try:
        os.remove(os.path.join(home, NOTE_FILE))          # its random password no longer works
    except OSError:
        pass
    if plain_url and os.path.exists(os.path.join(home, OPEN_FILE)):
        try:
            write_open_link(plain_url, home)
        except OSError:
            pass
    return user
