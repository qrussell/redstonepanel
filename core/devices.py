"""Remember this device (1.24): a sign-in that lasts 30 days on one browser, until it is forgotten.

Ticking "Remember this device" when signing in (password, code or passkey) makes a device record
and a session cookie that carries its id ("d") and lasts REMEMBER_DAYS instead of 12 hours.
Every request checks the record still exists, so Forget (one device, or all) under 🔑 Password
signs that browser out at once. Signing out on the device forgets it too. A new password, a
disabled or deleted account, or an admin's two-factor reset forgets every device of the account.

data/devices.json (0600): {"accounts": {"panel:alex": [{"id", "name", "created", "last_used",
"last_ip"}]}}. Nothing secret is stored: the cookie is signed with the panel's key, and the id
only says which record it belongs to.
"""
import json
import os
import re
import secrets
import threading
import time

from core.settings import DATA_DIR

DEVICES_FILE = os.path.join(DATA_DIR, "devices.json")
REMEMBER_DAYS = 30
REMEMBER_SECONDS = REMEMBER_DAYS * 86400
MAX_PER_ACCOUNT = 20          # the oldest is forgotten past this
TOUCH_EVERY = 600             # "last used" is written at most this often (seconds)

_lock = threading.Lock()
_cache = {"mtime": None, "data": None}


def _load():
    try:
        mtime = os.stat(DEVICES_FILE).st_mtime_ns
    except OSError:
        return {"accounts": {}}
    if _cache["mtime"] == mtime and _cache["data"] is not None:
        return _cache["data"]
    try:
        with open(DEVICES_FILE) as f:
            data = json.load(f)
        if not (isinstance(data, dict) and isinstance(data.get("accounts"), dict)):
            data = {"accounts": {}}
    except (OSError, ValueError):
        data = {"accounts": {}}     # damaged: remembered devices sign in again
    _cache.update(mtime=mtime, data=data)
    return data


def _save(data):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = DEVICES_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"accounts": data["accounts"]}, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, DEVICES_FILE)
    _cache.update(mtime=None, data=None)


def _copy():
    return json.loads(json.dumps(_load()))


def describe(user_agent):
    """A short name for the browser, from its User-Agent: "Edge on Windows", "Safari on iPhone"."""
    ua = str(user_agent or "")
    system = next((name for pat, name in ((r"iPhone", "iPhone"), (r"iPad", "iPad"), (r"Android", "Android"),
                                          (r"Windows", "Windows"), (r"Macintosh|Mac OS X", "Mac"),
                                          (r"CrOS", "ChromeOS"), (r"Linux", "Linux")) if re.search(pat, ua)), "")
    browser = next((name for pat, name in ((r"Edg(e|A|iOS)?/", "Edge"), (r"OPR/|Opera", "Opera"),
                                           (r"Firefox/|FxiOS/", "Firefox"), (r"SamsungBrowser/", "Samsung Internet"),
                                           (r"Chrome/|CriOS/", "Chrome"), (r"Safari/", "Safari")) if re.search(pat, ua)),
                   "A browser")
    return f"{browser} on {system}" if system else browser


def add(key, name="", ip=""):
    """A new remembered device for the account. Returns its id."""
    now = int(time.time())
    dev = {"id": secrets.token_urlsafe(12), "name": str(name or "A browser")[:60], "created": now,
           "last_used": now, "last_ip": str(ip or "")[:64]}
    with _lock:
        data = _copy()
        devs = [d for d in data["accounts"].get(key, []) if now - d.get("last_used", 0) < REMEMBER_SECONDS]
        devs.append(dev)
        data["accounts"][key] = devs[-MAX_PER_ACCOUNT:]
        _save(data)
    return dev["id"]


def valid(key, device_id, ip=""):
    """True while the device is remembered. Notes when it was last used (not on every request)."""
    devs = _load()["accounts"].get(key) or []
    dev = next((d for d in devs if d.get("id") == device_id), None)
    if not dev:
        return False
    now = int(time.time())
    if now - dev.get("last_used", 0) >= TOUCH_EVERY or (ip and ip != dev.get("last_ip")):
        with _lock:
            data = _copy()
            for d in data["accounts"].get(key, []):
                if d.get("id") == device_id:
                    d["last_used"], d["last_ip"] = now, str(ip or d.get("last_ip", ""))[:64]
            try:
                _save(data)
            except OSError:
                pass
    return True


def list_for(key):
    return [{k: d.get(k) for k in ("id", "name", "created", "last_used", "last_ip")}
            for d in _load()["accounts"].get(key) or []]


def remove(key, device_id):
    """Forgets one device. True if it was there."""
    with _lock:
        data = _copy()
        devs = data["accounts"].get(key) or []
        left = [d for d in devs if d.get("id") != device_id]
        if len(left) == len(devs):
            return False
        if left:
            data["accounts"][key] = left
        else:
            data["accounts"].pop(key, None)
        _save(data)
    return True


def remove_all(key, keep=None):
    """Forgets every device of the account (but `keep`, an id). Returns how many."""
    with _lock:
        data = _copy()
        devs = data["accounts"].get(key) or []
        left = [d for d in devs if keep and d.get("id") == keep]
        if len(left) == len(devs):
            return 0
        if left:
            data["accounts"][key] = left
        else:
            data["accounts"].pop(key, None)
        _save(data)
    return len(devs) - len(left)
