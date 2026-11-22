"""SSH public keys for SFTP sign-in (1.18), added by each account under Password -> SSH keys.

Kept in DATA_DIR/ssh_keys.json (0600), keyed like two-factor: "panel:NAME" / "linux:NAME".
Only public keys are stored. An account with two-factor sign-in on must use a key for SFTP,
because SFTP programs can't ask for a code. Standard library only: the panel doesn't need
paramiko to manage keys, only the SFTP service does to check them.
"""
import base64
import binascii
import hashlib
import json
import os
import struct
import threading
import time

from core.settings import DATA_DIR

KEYS_FILE = os.path.join(DATA_DIR, "ssh_keys.json")
KEY_TYPES = ("ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521")
MAX_KEYS = 10
MAX_LINE = 8192
MIN_RSA_BITS = 2048
LABEL_MAX = 60

_lock = threading.Lock()


class KeyError_(ValueError):
    """A key that can't be added (message is safe to show)."""


def account_key(source, username):
    return f"{source}:{username}"


def _load():
    try:
        with open(KEYS_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = KEYS_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, KEYS_FILE)


def _strings(blob):
    """The length-prefixed fields of an SSH public key blob."""
    out, i = [], 0
    while i < len(blob):
        if i + 4 > len(blob):
            raise ValueError("cut short")
        n = struct.unpack(">I", blob[i:i + 4])[0]
        i += 4
        if i + n > len(blob):
            raise ValueError("cut short")
        out.append(blob[i:i + n])
        i += n
    return out


def fingerprint(blob):
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def parse(line):
    """'ssh-ed25519 AAAA... comment' -> {type, blob (base64), comment, fingerprint}."""
    line = (line or "").strip()
    if not line:
        raise KeyError_("Paste the public key: one line starting with ssh-ed25519, ssh-rsa or ecdsa-sha2-.")
    if len(line) > MAX_LINE or "\n" in line or "\r" in line:
        raise KeyError_("Paste one public key, on one line.")
    if "PRIVATE KEY" in line:
        raise KeyError_("That's a private key. Paste the public key (the .pub file); never share the private one.")
    parts = line.split(None, 2)
    if len(parts) < 2 or parts[0] not in KEY_TYPES:
        raise KeyError_("That isn't a public key this panel accepts. It should start with ssh-ed25519, ssh-rsa "
                        "or ecdsa-sha2-.")
    try:
        blob = base64.b64decode(parts[1], validate=True)
        fields = _strings(blob)
    except (binascii.Error, ValueError):
        raise KeyError_("The key is damaged or cut short. Copy the whole line from the .pub file.")
    if not fields or fields[0].decode("ascii", "replace") != parts[0]:
        raise KeyError_("The key is damaged: its type doesn't match its contents.")
    if parts[0] == "ssh-rsa":
        if len(fields) != 3:
            raise KeyError_("The key is damaged or cut short. Copy the whole line from the .pub file.")
        bits = int.from_bytes(fields[2], "big").bit_length()
        if bits < MIN_RSA_BITS:
            raise KeyError_(f"That RSA key is {bits} bits; use at least {MIN_RSA_BITS}, or an ed25519 key.")
    comment = "".join(c for c in (parts[2] if len(parts) > 2 else "") if c.isprintable())[:LABEL_MAX]
    return {"type": parts[0], "blob": base64.b64encode(blob).decode(), "comment": comment,
            "fingerprint": fingerprint(blob)}


def public(k):
    return {"type": k["type"], "label": k.get("label") or k.get("comment") or "", "fingerprint": k["fingerprint"],
            "added": k.get("added")}


def list_keys(account):
    return [public(k) for k in _load().get(account, []) if isinstance(k, dict) and k.get("fingerprint")]


def add(account, line, label=""):
    key = parse(line)
    label = " ".join("".join(c for c in str(label or "") if c.isprintable()).split())[:LABEL_MAX]
    key.update(label=label or key["comment"], added=int(time.time()))
    with _lock:
        data = _load()
        keys = [k for k in data.get(account, []) if isinstance(k, dict)]
        if any(k.get("fingerprint") == key["fingerprint"] for k in keys):
            raise KeyError_("That key was already added.")
        if len(keys) >= MAX_KEYS:
            raise KeyError_(f"An account can have {MAX_KEYS} keys. Remove one first.")
        data[account] = keys + [key]
        _save(data)
    return public(key)


def remove(account, fp):
    with _lock:
        data = _load()
        keys = [k for k in data.get(account, []) if isinstance(k, dict)]
        left = [k for k in keys if k.get("fingerprint") != fp]
        if len(left) == len(keys):
            return False
        if left:
            data[account] = left
        else:
            data.pop(account, None)
        _save(data)
    return True


def remove_account(account):
    with _lock:
        data = _load()
        if data.pop(account, None) is not None:
            _save(data)


def matches(account, key_type, blob):
    """True if this public key (its raw blob) was added by the account."""
    want = base64.b64encode(blob).decode()
    return any(isinstance(k, dict) and k.get("type") == key_type and k.get("blob") == want
               for k in _load().get(account, []))
