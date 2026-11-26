"""Passkey sign-in (WebAuthn), for Windows panels: Windows Hello (face, fingerprint or PIN).

Linux panels keep Linux accounts, panel accounts and two-factor codes; supported() is False there
and every route refuses.

A passkey is a key pair made by the authenticator (Windows Hello keeps the private key in the
TPM). The panel stores only the public key, and a sign-in is the authenticator signing a fresh
one-use challenge after checking the person (user verification), so a passkey sign-in is two
factors on its own: something you have (this PC's key) and something you are or know (face,
finger, PIN). The PIN never reaches the panel.

data/passkeys.json (0600, root/SYSTEM only):
    {"accounts": {"linux:Quent": {"handle": "b64url", "credentials": [
        {"id": "b64url credential id", "public_key": "PEM", "alg": -7, "sign_count": 0,
         "rp_id": "localhost", "name": "Windows Hello", "created": 1700000000, "last_used": null}]}}}

Browsers allow passkeys only on https://, or http://localhost, and for a host name, not a bare IP
address: the passkey belongs to that name (the "relying party ID"), so one added at
https://localhost works at localhost only.

Checked at each step: the challenge is one we gave out (one use, 2 minutes), the origin is this
panel's own address, the authenticator data is for this host name, the person was present and
verified, the signature verifies with the stored key (ES256 or RS256), and the signature counter
never goes backwards when the authenticator keeps one. Attestation is not asked for ("none").
"""
import base64
import hashlib
import ipaddress
import json
import os
import secrets
import struct
import threading
import time

from core.settings import DATA_DIR
from core.platform import paths

PASSKEYS_FILE = os.path.join(DATA_DIR, "passkeys.json")
CHALLENGE_SECONDS = 120
MAX_CHALLENGES = 500
MAX_PER_ACCOUNT = 10
ALGS = (-7, -257)            # ES256 (P-256), RS256: what Windows Hello and other platform keys use
RP_NAME = "RedstonePanel"

_FLAG_UP, _FLAG_UV, _FLAG_AT = 0x01, 0x04, 0x40

_lock = threading.Lock()
_challenges = {}             # challenge (b64url) -> {"purpose", "key", "rp_id", "origin", "exp"}


class PasskeyError(ValueError):
    """A request the passkey layer refuses (message is safe to show)."""


def supported():
    """Passkeys are a Windows-panel feature (1.24)."""
    return paths.WINDOWS


def b64u(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def unb64u(text):
    if not isinstance(text, str) or len(text) > 65536:
        raise PasskeyError("The passkey answer was not understood.")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except Exception:
        raise PasskeyError("The passkey answer was not understood.")


# ---------------------------------------------------------------------------
# Where the browser is: the relying party ID and the origin
# ---------------------------------------------------------------------------
def site(scheme, host, port):
    """(rp_id, origin) for a request to scheme://host:port, or raises PasskeyError with what to do."""
    host = (host or "").strip().lower().rstrip(".")
    if host.startswith("[") or ":" in host:
        raise PasskeyError("Passkeys don't work at an IP address. Open the panel by its name "
                           "(https://localhost on this PC, or a host name with a trusted certificate).")
    try:
        ipaddress.ip_address(host)
        is_ip = True
    except ValueError:
        is_ip = False
    if is_ip:
        raise PasskeyError("Passkeys don't work at an IP address. Open the panel by its name "
                           "(https://localhost on this PC, or a host name with a trusted certificate).")
    if not host:
        raise PasskeyError("Open the panel by its name to use a passkey.")
    if scheme != "https" and host != "localhost":
        raise PasskeyError("Passkeys need HTTPS (or http://localhost on this PC).")
    default = 443 if scheme == "https" else 80
    origin = f"{scheme}://{host}" + ("" if not port or int(port) == default else f":{int(port)}")
    return host, origin


# ---------------------------------------------------------------------------
# passkeys.json
# ---------------------------------------------------------------------------
def _load():
    try:
        with open(PASSKEYS_FILE) as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("accounts"), dict):
            return data
    except FileNotFoundError:
        pass
    except Exception:
        pass   # damaged: no passkeys work (password sign-in still does); adding one rewrites it
    return {"accounts": {}}


def _save(data):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = PASSKEYS_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"accounts": data["accounts"]}, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, PASSKEYS_FILE)


def _public(c):
    return {"id": c["id"], "name": c.get("name") or "Passkey", "created": c.get("created"),
            "last_used": c.get("last_used"), "rp_id": c.get("rp_id")}


def list_for(key):
    a = _load()["accounts"].get(key) or {}
    return [_public(c) for c in a.get("credentials", [])]


def count(key):
    return len((_load()["accounts"].get(key) or {}).get("credentials", []))


def remove(key, cred_id):
    with _lock:
        data = _load()
        a = data["accounts"].get(key)
        if not a:
            return False
        left = [c for c in a.get("credentials", []) if c["id"] != cred_id]
        if len(left) == len(a.get("credentials", [])):
            return False
        a["credentials"] = left
        _save(data)
    return True


def remove_all(key):
    """Forgets every passkey of an account (reset-2fa, deleted account). True if it had any."""
    with _lock:
        data = _load()
        a = data["accounts"].pop(key, None)
        if a is None:
            return False
        _save(data)
    return bool(a.get("credentials"))


# ---------------------------------------------------------------------------
# Challenges
# ---------------------------------------------------------------------------
def _new_challenge(purpose, key, rp_id, origin):
    now = time.time()
    with _lock:
        for c in [c for c, v in _challenges.items() if v["exp"] < now]:
            del _challenges[c]
        if len(_challenges) >= MAX_CHALLENGES:
            raise PasskeyError("Too many passkey sign-ins in progress. Try again in a minute.")
        c = b64u(secrets.token_bytes(32))
        _challenges[c] = {"purpose": purpose, "key": key, "rp_id": rp_id, "origin": origin,
                          "exp": now + CHALLENGE_SECONDS}
    return c


def _take_challenge(client_data, purpose, rp_id, origin):
    """Checks clientDataJSON and uses up its challenge. Returns the challenge's record."""
    try:
        cd = json.loads(client_data)
    except Exception:
        raise PasskeyError("The passkey answer was not understood.")
    if not isinstance(cd, dict):
        raise PasskeyError("The passkey answer was not understood.")
    with _lock:
        rec = _challenges.pop(str(cd.get("challenge", "")), None)
    if not rec or rec["exp"] < time.time() or rec["purpose"] != purpose:
        raise PasskeyError("The passkey request expired. Try again.")
    want_type = "webauthn.create" if purpose == "register" else "webauthn.get"
    if cd.get("type") != want_type:
        raise PasskeyError("The passkey answer was not understood.")
    if cd.get("origin") != origin or rec["origin"] != origin or rec["rp_id"] != rp_id:
        raise PasskeyError("That passkey answer came from another address.")
    if cd.get("crossOrigin"):
        raise PasskeyError("That passkey answer came from another address.")
    return rec


# ---------------------------------------------------------------------------
# CBOR (RFC 8949), just what attestation objects and COSE keys use
# ---------------------------------------------------------------------------
def _cbor(buf, i=0, depth=0):
    if depth > 16 or i >= len(buf):
        raise PasskeyError("The passkey answer was not understood.")
    ib = buf[i]
    major, info = ib >> 5, ib & 0x1F
    i += 1
    if info < 24:
        val = info
    elif info in (24, 25, 26, 27):
        n = 1 << (info - 24)
        if i + n > len(buf):
            raise PasskeyError("The passkey answer was not understood.")
        val = int.from_bytes(buf[i:i + n], "big")
        i += n
    else:
        raise PasskeyError("The passkey answer was not understood.")   # indefinite lengths: not used
    if major == 0:
        return val, i
    if major == 1:
        return -1 - val, i
    if major in (2, 3):
        if i + val > len(buf):
            raise PasskeyError("The passkey answer was not understood.")
        raw = bytes(buf[i:i + val])
        return (raw if major == 2 else raw.decode("utf-8", "replace")), i + val
    if major == 4:
        out = []
        for _ in range(val):
            item, i = _cbor(buf, i, depth + 1)
            out.append(item)
        return out, i
    if major == 5:
        out = {}
        for _ in range(val):
            k, i = _cbor(buf, i, depth + 1)
            v, i = _cbor(buf, i, depth + 1)
            if isinstance(k, (list, dict)):
                raise PasskeyError("The passkey answer was not understood.")
            out[k] = v
        return out, i
    if major == 6:
        return _cbor(buf, i, depth + 1)          # tag: the value inside
    if major == 7 and info in (20, 21, 22):
        return {20: False, 21: True, 22: None}[info], i
    raise PasskeyError("The passkey answer was not understood.")


def _auth_data(raw, rp_id, need_credential):
    """Parses and checks authenticator data. Returns (sign_count, credential id or None, COSE key or None)."""
    if len(raw) < 37:
        raise PasskeyError("The passkey answer was not understood.")
    if raw[:32] != hashlib.sha256(rp_id.encode()).digest():
        raise PasskeyError("That passkey is for another address.")
    flags = raw[32]
    if not flags & _FLAG_UP or not flags & _FLAG_UV:
        raise PasskeyError("The passkey didn't check who you are (face, fingerprint or PIN). Try again.")
    sign_count = struct.unpack(">I", raw[33:37])[0]
    if not need_credential:
        return sign_count, None, None
    if not flags & _FLAG_AT or len(raw) < 55:
        raise PasskeyError("The passkey answer was not understood.")
    n = struct.unpack(">H", raw[53:55])[0]
    cred_id = raw[55:55 + n]
    if len(cred_id) != n or not 16 <= n <= 1023:
        raise PasskeyError("The passkey answer was not understood.")
    cose, _ = _cbor(raw, 55 + n)
    if not isinstance(cose, dict):
        raise PasskeyError("The passkey answer was not understood.")
    return sign_count, cred_id, cose


def _cose_to_pem(cose):
    """(PEM, alg) for an ES256 or RS256 COSE public key."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec, rsa
    alg, kty = cose.get(3), cose.get(1)
    if alg == -7 and kty == 2 and cose.get(-1) == 1:
        x, y = cose.get(-2), cose.get(-3)
        if not (isinstance(x, bytes) and isinstance(y, bytes) and len(x) == 32 and len(y) == 32):
            raise PasskeyError("This passkey's key type isn't supported.")
        try:
            key = ec.EllipticCurvePublicNumbers(int.from_bytes(x, "big"), int.from_bytes(y, "big"),
                                                ec.SECP256R1()).public_key()
        except ValueError:
            raise PasskeyError("This passkey's key type isn't supported.")
    elif alg == -257 and kty == 3:
        n, e = cose.get(-1), cose.get(-2)
        if not (isinstance(n, bytes) and isinstance(e, bytes) and len(n) >= 256):
            raise PasskeyError("This passkey's key type isn't supported.")
        key = rsa.RSAPublicNumbers(int.from_bytes(e, "big"), int.from_bytes(n, "big")).public_key()
    else:
        raise PasskeyError("This passkey's key type isn't supported (ES256 or RS256 only).")
    pem = key.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return pem, alg


def _verify(pem, alg, signature, signed):
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, padding
    key = serialization.load_pem_public_key(pem.encode())
    try:
        if alg == -7:
            key.verify(signature, signed, ec.ECDSA(hashes.SHA256()))
        elif alg == -257:
            key.verify(signature, signed, padding.PKCS1v15(), hashes.SHA256())
        else:
            return False
    except (InvalidSignature, ValueError):
        return False
    return True


# ---------------------------------------------------------------------------
# Adding a passkey (signed in)
# ---------------------------------------------------------------------------
def registration_options(key, username, display, rp_id, origin):
    """The options for navigator.credentials.create (binary fields as b64url)."""
    with _lock:
        data = _load()
        a = data["accounts"].setdefault(key, {})
        if not a.get("handle"):
            a["handle"] = b64u(secrets.token_bytes(16))
            a.setdefault("credentials", [])
            _save(data)
        handle, creds = a["handle"], a.get("credentials", [])
    if len(creds) >= MAX_PER_ACCOUNT:
        raise PasskeyError(f"This account already has {MAX_PER_ACCOUNT} passkeys. Remove one first.")
    return {
        "challenge": _new_challenge("register", key, rp_id, origin),
        "rp": {"id": rp_id, "name": RP_NAME},
        "user": {"id": handle, "name": username, "displayName": display or username},
        "pubKeyCredParams": [{"type": "public-key", "alg": a} for a in ALGS],
        "timeout": CHALLENGE_SECONDS * 1000,
        "attestation": "none",
        "authenticatorSelection": {"authenticatorAttachment": "platform", "residentKey": "required",
                                   "requireResidentKey": True, "userVerification": "required"},
        "excludeCredentials": [{"type": "public-key", "id": c["id"]} for c in creds],
    }


def register(key, client_data_b64, attestation_b64, rp_id, origin, name=""):
    """Checks the browser's answer and stores the new passkey. Returns it (public fields)."""
    client_data = unb64u(client_data_b64)
    rec = _take_challenge(client_data, "register", rp_id, origin)
    if rec["key"] != key:
        raise PasskeyError("The passkey request expired. Try again.")
    att, _ = _cbor(unb64u(attestation_b64))
    if not isinstance(att, dict) or not isinstance(att.get("authData"), bytes):
        raise PasskeyError("The passkey answer was not understood.")
    sign_count, cred_id, cose = _auth_data(att["authData"], rp_id, need_credential=True)
    pem, alg = _cose_to_pem(cose)
    name = " ".join(str(name or "").split())[:60] or "Windows Hello"
    cred = {"id": b64u(cred_id), "public_key": pem, "alg": alg, "sign_count": sign_count, "rp_id": rp_id,
            "name": name, "created": int(time.time()), "last_used": None}
    with _lock:
        data = _load()
        for other in data["accounts"].values():
            if any(c["id"] == cred["id"] for c in other.get("credentials", [])):
                raise PasskeyError("That passkey is already added.")
        a = data["accounts"].setdefault(key, {"handle": b64u(secrets.token_bytes(16))})
        a.setdefault("credentials", [])
        if len(a["credentials"]) >= MAX_PER_ACCOUNT:
            raise PasskeyError(f"This account already has {MAX_PER_ACCOUNT} passkeys. Remove one first.")
        a["credentials"].append(cred)
        _save(data)
    return _public(cred)


# ---------------------------------------------------------------------------
# Signing in with a passkey
# ---------------------------------------------------------------------------
def login_options(rp_id, origin):
    """Options for navigator.credentials.get: no account named, the passkey says whose it is."""
    return {"challenge": _new_challenge("login", None, rp_id, origin), "rpId": rp_id,
            "timeout": CHALLENGE_SECONDS * 1000, "userVerification": "required", "allowCredentials": []}


def authenticate(cred_id_b64, client_data_b64, auth_data_b64, signature_b64, user_handle_b64, rp_id, origin):
    """Checks a sign-in answer. Returns the account key ("linux:Quent") or raises PasskeyError."""
    client_data = unb64u(client_data_b64)
    auth_data = unb64u(auth_data_b64)
    signature = unb64u(signature_b64)
    _take_challenge(client_data, "login", rp_id, origin)
    data = _load()
    found = None
    for key, a in data["accounts"].items():
        for c in a.get("credentials", []):
            if c["id"] == cred_id_b64:
                found = (key, a, c)
    if not found:
        raise PasskeyError("This passkey isn't known to this panel (it may have been removed).")
    key, a, c = found
    if user_handle_b64 and user_handle_b64 != a.get("handle"):
        raise PasskeyError("This passkey isn't known to this panel (it may have been removed).")
    if c.get("rp_id") != rp_id:
        raise PasskeyError("That passkey is for another address.")
    sign_count, _, _ = _auth_data(auth_data, rp_id, need_credential=False)
    if not _verify(c["public_key"], c["alg"], signature, auth_data + hashlib.sha256(client_data).digest()):
        raise PasskeyError("The passkey's signature didn't check out.")
    with _lock:
        data = _load()
        a = data["accounts"].get(key) or {}
        c = next((x for x in a.get("credentials", []) if x["id"] == cred_id_b64), None)
        if not c:
            raise PasskeyError("This passkey isn't known to this panel (it may have been removed).")
        old = c.get("sign_count", 0)
        if (sign_count or old) and sign_count <= old:
            # The counter went backwards: this key may have been copied. Refuse (remove and add it again).
            raise PasskeyError("This passkey's counter went backwards, so it may have been copied. "
                               "Sign in with your password, remove this passkey and add it again.")
        c.update(sign_count=sign_count, last_used=int(time.time()))
        _save(data)
    return key
