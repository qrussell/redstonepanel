"""1.24: passkey sign-in (WebAuthn, Windows Hello) on Windows panels.
Run: python -m unittest discover -s tests

A synthetic authenticator (tests/ only) makes real ES256 and RS256 keys with the cryptography
library and answers like a browser with Windows Hello: attestation "none", user present and
verified. The routes are tested through the real middleware with paths.WINDOWS patched on.
"""
import base64
import hashlib
import json
import os
import struct
import sys
import tempfile
import unittest
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-passkeys-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import passkeys, twofactor  # noqa: E402

try:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
    HAVE_CRYPTO = True
except ImportError:
    HAVE_CRYPTO = False

try:
    import fastapi  # noqa: F401
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except (ImportError, RuntimeError):
    HAVE_FASTAPI = False


def b64u(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def cbor(v):
    """Just enough CBOR to build attestation objects and COSE keys."""
    def head(major, n):
        if n < 24:
            return bytes([major << 5 | n])
        for info, size in ((24, 1), (25, 2), (26, 4), (27, 8)):
            if n < 1 << (8 * size):
                return bytes([major << 5 | info]) + n.to_bytes(size, "big")
    if isinstance(v, bool):
        return bytes([0xF5 if v else 0xF4])
    if isinstance(v, int):
        return head(0, v) if v >= 0 else head(1, -1 - v)
    if isinstance(v, bytes):
        return head(2, len(v)) + v
    if isinstance(v, str):
        return head(3, len(v.encode())) + v.encode()
    if isinstance(v, list):
        return head(4, len(v)) + b"".join(cbor(x) for x in v)
    if isinstance(v, dict):
        return head(5, len(v)) + b"".join(cbor(k) + cbor(x) for k, x in v.items())
    raise TypeError(v)


class Authenticator:
    """A platform authenticator like Windows Hello, for one passkey."""

    def __init__(self, alg=-7, flags=0x05, counter=0):
        self.alg, self.flags, self.counter = alg, flags, counter
        self.cred_id = os.urandom(32)
        if alg == -7:
            self.key = ec.generate_private_key(ec.SECP256R1())
            n = self.key.public_key().public_numbers()
            self.cose = {1: 2, 3: -7, -1: 1, -2: n.x.to_bytes(32, "big"), -3: n.y.to_bytes(32, "big")}
        else:
            self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            n = self.key.public_key().public_numbers()
            self.cose = {1: 3, 3: -257, -1: n.n.to_bytes(256, "big"), -2: n.e.to_bytes(3, "big")}
        self.handle = None

    def _auth_data(self, rp_id, with_cred=False, flags=None):
        f = self.flags if flags is None else flags
        out = hashlib.sha256(rp_id.encode()).digest() + bytes([f | (0x40 if with_cred else 0)])
        out += struct.pack(">I", self.counter)
        if with_cred:
            out += b"\0" * 16 + struct.pack(">H", len(self.cred_id)) + self.cred_id + cbor(self.cose)
        return out

    @staticmethod
    def client_data(kind, challenge, origin):
        return json.dumps({"type": kind, "challenge": challenge, "origin": origin, "crossOrigin": False}).encode()

    def create(self, options, origin, rp_id=None):
        self.handle = options["user"]["id"]
        cd = self.client_data("webauthn.create", options["challenge"], origin)
        att = cbor({"fmt": "none", "attStmt": {}, "authData": self._auth_data(rp_id or options["rp"]["id"], True)})
        return {"client_data": b64u(cd), "attestation_object": b64u(att)}

    def get(self, options, origin, rp_id=None, flags=None, bump=True):
        if bump and self.counter:
            self.counter += 1
        cd = self.client_data("webauthn.get", options["challenge"], origin)
        ad = self._auth_data(rp_id or options["rpId"], flags=flags)
        signed = ad + hashlib.sha256(cd).digest()
        if self.alg == -7:
            sig = self.key.sign(signed, ec.ECDSA(hashes.SHA256()))
        else:
            sig = self.key.sign(signed, padding.PKCS1v15(), hashes.SHA256())
        return {"id": b64u(self.cred_id), "client_data": b64u(cd), "authenticator_data": b64u(ad),
                "signature": b64u(sig), "user_handle": self.handle}


def _isolate(test):
    d = tempfile.mkdtemp(dir=TMP)
    for p in (mock.patch.object(passkeys, "PASSKEYS_FILE", os.path.join(d, "passkeys.json")),
              mock.patch.dict(passkeys._challenges, clear=True)):
        p.start()
        test.addCleanup(p.stop)
    return d


RP, ORIGIN = "localhost", "https://localhost:8443"


class Site(unittest.TestCase):
    def test_names_and_localhost(self):
        self.assertEqual(passkeys.site("https", "localhost", 8443), ("localhost", "https://localhost:8443"))
        self.assertEqual(passkeys.site("http", "localhost", 8080), ("localhost", "http://localhost:8080"))
        self.assertEqual(passkeys.site("https", "Panel.Example.org", 443), ("panel.example.org", "https://panel.example.org"))

    def test_refused(self):
        for scheme, host in (("https", "10.0.0.5"), ("https", "127.0.0.1"), ("https", "::1"), ("https", "[::1]"),
                             ("http", "panel.example.org"), ("https", "")):
            with self.assertRaises(passkeys.PasskeyError, msg=(scheme, host)):
                passkeys.site(scheme, host, None)

    def test_only_windows(self):
        with mock.patch.object(passkeys.paths, "WINDOWS", False):
            self.assertFalse(passkeys.supported())
        with mock.patch.object(passkeys.paths, "WINDOWS", True):
            self.assertTrue(passkeys.supported())


@unittest.skipUnless(HAVE_CRYPTO, "needs the cryptography library (bundled in the Windows installer)")
class Ceremonies(unittest.TestCase):
    def setUp(self):
        _isolate(self)

    def add(self, auth, key="linux:Quent"):
        opts = passkeys.registration_options(key, "Quent", "Quent", RP, ORIGIN)
        self.assertEqual(opts["authenticatorSelection"]["userVerification"], "required")
        # this PC, a phone (QR code) or a security key: not limited to Windows Hello on the PC
        self.assertNotIn("authenticatorAttachment", opts["authenticatorSelection"])
        self.assertEqual([p["alg"] for p in opts["pubKeyCredParams"]], [-7, -257])
        a = auth.create(opts, ORIGIN)
        return passkeys.register(key, a["client_data"], a["attestation_object"], RP, ORIGIN, "Windows Hello")

    def sign_in(self, auth, **kw):
        a = auth.get(passkeys.login_options(RP, ORIGIN), ORIGIN, **kw)
        return passkeys.authenticate(a["id"], a["client_data"], a["authenticator_data"], a["signature"],
                                     a["user_handle"], RP, ORIGIN)

    def test_es256_and_rs256(self):
        for alg in (-7, -257):
            auth = Authenticator(alg)
            pk = self.add(auth, key=f"panel:u{-alg}")
            self.assertEqual(pk["name"], "Windows Hello")
            self.assertEqual(self.sign_in(auth), f"panel:u{-alg}")
        self.assertEqual(os.stat(passkeys.PASSKEYS_FILE).st_mode & 0o777, 0o600)
        with open(passkeys.PASSKEYS_FILE) as f:
            stored = json.load(f)
        self.assertNotIn("PRIVATE", json.dumps(stored))
        self.assertTrue(all(c["last_used"] for a in stored["accounts"].values() for c in a["credentials"]))

    def test_challenge_is_one_use(self):
        auth = Authenticator()
        self.add(auth)
        a = auth.get(passkeys.login_options(RP, ORIGIN), ORIGIN)
        args = (a["id"], a["client_data"], a["authenticator_data"], a["signature"], a["user_handle"], RP, ORIGIN)
        self.assertEqual(passkeys.authenticate(*args), "linux:Quent")
        with self.assertRaisesRegex(passkeys.PasskeyError, "expired"):
            passkeys.authenticate(*args)

    def test_expired_challenge(self):
        auth = Authenticator()
        self.add(auth)
        opts = passkeys.login_options(RP, ORIGIN)
        passkeys._challenges[opts["challenge"]]["exp"] = 0
        a = auth.get(opts, ORIGIN)
        with self.assertRaisesRegex(passkeys.PasskeyError, "expired"):
            passkeys.authenticate(a["id"], a["client_data"], a["authenticator_data"], a["signature"],
                                  a["user_handle"], RP, ORIGIN)

    def test_made_up_challenge(self):
        auth = Authenticator()
        self.add(auth)
        a = auth.get({"challenge": b64u(os.urandom(32)), "rpId": RP}, ORIGIN)
        with self.assertRaises(passkeys.PasskeyError):
            passkeys.authenticate(a["id"], a["client_data"], a["authenticator_data"], a["signature"],
                                  a["user_handle"], RP, ORIGIN)

    def test_wrong_origin_or_rp(self):
        auth = Authenticator()
        self.add(auth)
        a = auth.get(passkeys.login_options(RP, ORIGIN), "https://evil.example")
        with self.assertRaisesRegex(passkeys.PasskeyError, "another address"):
            passkeys.authenticate(a["id"], a["client_data"], a["authenticator_data"], a["signature"],
                                  a["user_handle"], RP, ORIGIN)
        a = auth.get(passkeys.login_options(RP, ORIGIN), ORIGIN, rp_id="evil.example")
        with self.assertRaisesRegex(passkeys.PasskeyError, "another address"):
            passkeys.authenticate(a["id"], a["client_data"], a["authenticator_data"], a["signature"],
                                  a["user_handle"], RP, ORIGIN)

    def test_needs_user_verification(self):
        auth = Authenticator()
        self.add(auth)
        with self.assertRaisesRegex(passkeys.PasskeyError, "face, fingerprint or PIN"):
            self.sign_in(auth, flags=0x01)
        bad = Authenticator(flags=0x01)
        with self.assertRaisesRegex(passkeys.PasskeyError, "face, fingerprint or PIN"):
            self.add(bad)

    def test_bad_signature(self):
        auth = Authenticator()
        self.add(auth)
        a = auth.get(passkeys.login_options(RP, ORIGIN), ORIGIN)
        other = Authenticator()
        a["signature"] = other.get({"challenge": "x", "rpId": RP}, ORIGIN)["signature"]
        with self.assertRaisesRegex(passkeys.PasskeyError, "signature"):
            passkeys.authenticate(a["id"], a["client_data"], a["authenticator_data"], a["signature"],
                                  a["user_handle"], RP, ORIGIN)

    def test_unknown_or_removed(self):
        auth = Authenticator()
        pk = self.add(auth)
        stranger = Authenticator()
        stranger.handle = auth.handle
        with self.assertRaisesRegex(passkeys.PasskeyError, "isn't known"):
            self.sign_in(stranger)
        self.assertTrue(passkeys.remove("linux:Quent", pk["id"]))
        with self.assertRaisesRegex(passkeys.PasskeyError, "isn't known"):
            self.sign_in(auth)

    def test_counter_must_go_up(self):
        auth = Authenticator(counter=5)
        self.add(auth)
        self.sign_in(auth)                          # 6
        with self.assertRaisesRegex(passkeys.PasskeyError, "copied"):
            self.sign_in(auth, bump=False)          # 6 again
        zero = Authenticator(counter=0)             # Windows Hello may keep no counter: 0 every time
        self.add(zero, key="panel:z")
        self.assertEqual(self.sign_in(zero), "panel:z")
        self.assertEqual(self.sign_in(zero), "panel:z")

    def test_same_passkey_twice_and_limit(self):
        auth = Authenticator()
        self.add(auth)
        with self.assertRaisesRegex(passkeys.PasskeyError, "already added"):
            self.add(auth)
        for _ in range(passkeys.MAX_PER_ACCOUNT - 1):
            self.add(Authenticator())
        with self.assertRaisesRegex(passkeys.PasskeyError, "Remove one first"):
            passkeys.registration_options("linux:Quent", "Quent", "Quent", RP, ORIGIN)

    def test_registration_challenge_belongs_to_the_account(self):
        opts = passkeys.registration_options("panel:alex", "alex", "alex", RP, ORIGIN)
        a = Authenticator().create(opts, ORIGIN)
        with self.assertRaisesRegex(passkeys.PasskeyError, "expired"):
            passkeys.register("panel:sam", a["client_data"], a["attestation_object"], RP, ORIGIN)

    def test_remove_all(self):
        self.add(Authenticator())
        self.assertTrue(passkeys.remove_all("linux:Quent"))
        self.assertEqual(passkeys.list_for("linux:Quent"), [])
        self.assertFalse(passkeys.remove_all("linux:Quent"))

    def test_garbage_is_refused_cleanly(self):
        deep = 1
        for _ in range(40):
            deep = [deep]
        for junk in (b"", b"\xff" * 40, b"\x9f", b"\x5b\xff\xff\xff\xff\xff\xff\xff\xff", cbor(deep), cbor({"authData": 5})):
            with self.assertRaises(passkeys.PasskeyError, msg=junk[:10]):
                opts = passkeys.registration_options("linux:Quent", "Quent", "Quent", RP, ORIGIN)
                cd = Authenticator.client_data("webauthn.create", opts["challenge"], ORIGIN)
                passkeys.register("linux:Quent", b64u(cd), b64u(junk), RP, ORIGIN)
        with self.assertRaises(passkeys.PasskeyError):
            passkeys.unb64u("x" * 70000)


@unittest.skipUnless(HAVE_FASTAPI and HAVE_CRYPTO, "needs FastAPI and cryptography")
class Routes(unittest.TestCase):
    PASSWORD = "correct horse"

    def setUp(self):
        import main
        self.main, self.auth = main, main.auth
        d = _isolate(self)
        for p in (mock.patch.object(main.auth, "USERS_FILE", os.path.join(d, "users.json")),
                  mock.patch.object(main.auth, "SECRET_FILE", os.path.join(d, "secret.key")),
                  mock.patch.object(main.auth, "_secret", None),
                  mock.patch.dict(main.auth._fails, clear=True),
                  mock.patch.object(twofactor, "TOTP_FILE", os.path.join(d, "totp.json")),
                  mock.patch.dict(twofactor._cache, {"mtime": None, "data": None}),
                  mock.patch.object(main.panel_settings, "SETTINGS_FILE", os.path.join(d, "settings.json")),
                  mock.patch.dict(main.panel_settings._cache, {"mtime": None, "data": None}),
                  mock.patch.object(passkeys.paths, "WINDOWS", True),
                  mock.patch.object(main.manager, "write_log", lambda m: self.log.append(m))):
            p.start()
            self.addCleanup(p.stop)
        self.log = []
        self.auth.create_panel_user("alex", self.PASSWORD, "admin")

    def client(self, base="https://localhost:8443", sign_in=True):
        c = TestClient(self.main.app, base_url=base)
        if sign_in:
            r = c.post("/api/auth/login", json={"username": "alex", "password": self.PASSWORD})
            self.assertEqual(r.status_code, 200, r.text)
        return c

    def add(self, c, auth, origin="https://localhost:8443"):
        r = c.post("/api/auth/passkeys/options", json={"password": self.PASSWORD})
        self.assertEqual(r.status_code, 200, r.text)
        r = c.post("/api/auth/passkeys", json=dict(auth.create(r.json(), origin), name="Desk PC"))
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def passkey_sign_in(self, c, auth, origin="https://localhost:8443"):
        r = c.post("/api/auth/passkey/options")
        self.assertEqual(r.status_code, 200, r.text)
        return c.post("/api/auth/passkey/login", json=auth.get(r.json(), origin))

    def test_add_list_sign_in_remove(self):
        c = self.client()
        auth = Authenticator()
        pk = self.add(c, auth)
        listed = c.get("/api/auth/passkeys").json()
        self.assertTrue(listed["supported"] and listed["usable"])
        self.assertEqual([p["name"] for p in listed["passkeys"]], ["Desk PC"])

        fresh = self.client(sign_in=False)
        self.assertEqual(fresh.get("/api/auth/me").status_code, 401)
        r = self.passkey_sign_in(fresh, auth)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["username"], "alex")
        me = fresh.get("/api/auth/me").json()
        self.assertTrue(me["passkey"])
        self.assertTrue(any("with a passkey" in m for m in self.log))

        self.assertEqual(c.delete(f"/api/auth/passkeys/{pk['id']}").status_code, 200)
        self.assertEqual(self.passkey_sign_in(self.client(sign_in=False), auth).status_code, 401)

    def test_adding_needs_the_password(self):
        c = self.client()
        r = c.post("/api/auth/passkeys/options", json={"password": "wrong one"})
        self.assertEqual(r.status_code, 400)

    def test_passkey_session_survives_two_factor(self):
        """A passkey sign-in counts as both steps: valid with two-factor on, and meets 'required for admins'."""
        c = self.client()
        auth = Authenticator()
        self.add(c, auth)
        s = self.main.panel_settings.load()
        s["require_2fa_admins"] = True
        self.main.panel_settings.save(s)
        fresh = self.client(sign_in=False)
        self.assertEqual(self.passkey_sign_in(fresh, auth).status_code, 200)
        self.assertFalse(fresh.get("/api/auth/me").json()["needs_2fa_setup"])
        self.assertEqual(fresh.get("/api/servers").status_code != 403, True)

    def test_disabled_account_cant_use_its_passkey(self):
        auth = Authenticator()
        self.add(self.client(), auth)
        self.auth.update_panel_user("alex", disabled=True)
        self.assertEqual(self.passkey_sign_in(self.client(sign_in=False), auth).status_code, 401)

    def test_admin_reset_removes_passkeys(self):
        self.auth.create_panel_user("sam", self.PASSWORD, "admin")
        auth = Authenticator()
        self.add(self.client(), auth)
        sam = TestClient(self.main.app, base_url="https://localhost:8443")
        sam.post("/api/auth/login", json={"username": "sam", "password": self.PASSWORD})
        self.assertEqual(sam.post("/api/users/alex/reset-2fa").status_code, 200)
        self.assertEqual(passkeys.list_for("panel:alex"), [])

    def test_deleting_the_account_removes_passkeys(self):
        self.auth.create_panel_user("sam", self.PASSWORD, "admin")
        sam = TestClient(self.main.app, base_url="https://localhost:8443")
        sam.post("/api/auth/login", json={"username": "sam", "password": self.PASSWORD})
        self.add(sam, Authenticator())
        self.auth.delete_panel_user("sam")
        self.assertEqual(passkeys.list_for("panel:sam"), [])

    def test_ip_address_and_plain_http_are_refused(self):
        for base in ("https://127.0.0.1:8443", "http://panel.example.org:8080"):
            c = self.client(base)
            self.assertFalse(c.get("/api/auth/passkeys/available").json()["usable"], base)
            self.assertEqual(c.post("/api/auth/passkey/options").status_code, 400, base)
            self.assertEqual(c.post("/api/auth/passkeys/options", json={"password": self.PASSWORD}).status_code,
                             400, base)
        self.assertTrue(self.client("http://localhost:8080").get("/api/auth/passkeys/available").json()["usable"])

    def test_linux_panels_have_none(self):
        with mock.patch.object(passkeys.paths, "WINDOWS", False):
            c = self.client()
            self.assertEqual(c.get("/api/auth/passkeys/available").json(), {"supported": False})
            self.assertEqual(c.post("/api/auth/passkey/options").status_code, 404)
            self.assertEqual(c.post("/api/auth/passkeys/options", json={"password": self.PASSWORD}).status_code, 404)

    def test_failures_are_rate_limited(self):
        auth = Authenticator()
        self.add(self.client(), auth)
        c = self.client(sign_in=False)
        for _ in range(self.auth.MAX_FAILS):
            r = c.post("/api/auth/passkey/options")
            a = Authenticator().get(r.json(), "https://localhost:8443")
            self.assertEqual(c.post("/api/auth/passkey/login", json=a).status_code, 401)
        self.assertEqual(c.post("/api/auth/passkey/options").status_code, 429)


if __name__ == "__main__":
    unittest.main()
