"""1.16.7: two-factor sign-in (TOTP + recovery codes) and the QR code drawn for setup.
Run: python -m unittest discover -s tests

The sign-in tests go through the real middleware and routes with FastAPI's TestClient, with a
panel account in a temporary data folder (PAM is not involved).
"""
import hashlib
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-2fa-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import qr, twofactor  # noqa: E402

try:
    import fastapi  # noqa: F401
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except (ImportError, RuntimeError):
    HAVE_FASTAPI = False


def _isolate(test, auth=None, settings=None):
    """Points totp.json (and users.json, settings.json, secret.key) at a fresh folder."""
    d = tempfile.mkdtemp(dir=TMP)
    patches = [mock.patch.object(twofactor, "TOTP_FILE", os.path.join(d, "totp.json")),
               mock.patch.dict(twofactor._cache, {"mtime": None, "data": None})]
    if auth:
        patches += [mock.patch.object(auth, "USERS_FILE", os.path.join(d, "users.json")),
                    mock.patch.object(auth, "SECRET_FILE", os.path.join(d, "secret.key")),
                    mock.patch.object(auth, "_secret", None),
                    mock.patch.dict(auth._fails, clear=True)]
    if settings:
        patches += [mock.patch.object(settings, "SETTINGS_FILE", os.path.join(d, "settings.json")),
                    mock.patch.dict(settings._cache, {"mtime": None, "data": None})]
    for p in patches:
        p.start()
        test.addCleanup(p.stop)
    return d


class Totp(unittest.TestCase):
    # RFC 6238 appendix B, SHA-1 secret "12345678901234567890" (last six of the eight digits)
    RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"

    def setUp(self):
        _isolate(self)

    def test_rfc6238_vectors(self):
        for t, code in ((59, "287082"), (1111111109, "081804"), (1111111111, "050471"),
                        (1234567890, "005924"), (2000000000, "279037"), (20000000000, "353130")):
            self.assertEqual(twofactor.code_at(self.RFC_SECRET, t // 30), code, t)

    def test_new_secret_is_160_bits_base32(self):
        s = twofactor.new_secret()
        self.assertEqual(len(s), 32)
        self.assertEqual(len(twofactor._key_bytes(s)), 20)

    def test_provisioning_uri(self):
        uri = twofactor.provisioning_uri("ABC", "alex", "Basement PC")
        self.assertTrue(uri.startswith("otpauth://totp/RedstonePanel%20%28Basement%20PC%29%3Aalex?"))
        self.assertIn("secret=ABC", uri)
        self.assertIn("issuer=RedstonePanel", uri)

    def _enable(self, key="panel:alex", now=None):
        now = time.time() if now is None else now
        secret = twofactor.begin_setup(key)
        self.assertFalse(twofactor.is_enabled(key), "not on until a code confirms it")
        codes = twofactor.confirm_setup(key, twofactor.code_at(secret, int(now // 30)), now=now)
        return secret, codes

    def test_setup_needs_a_matching_code(self):
        secret = twofactor.begin_setup("panel:alex")
        with self.assertRaises(twofactor.TwoFactorError):
            now = int(time.time() // 30)
            wrong = next(c for c in ("000000", "111111", "222222", "333333")
                         if c not in {twofactor.code_at(secret, s) for s in (now - 1, now, now + 1)})
            twofactor.confirm_setup("panel:alex", wrong)
        self.assertFalse(twofactor.is_enabled("panel:alex"))

    def test_setup_expires(self):
        secret = twofactor.begin_setup("panel:alex")
        later = time.time() + twofactor.SETUP_SECONDS + 60
        with self.assertRaises(twofactor.TwoFactorError):
            twofactor.confirm_setup("panel:alex", twofactor.code_at(secret, int(later // 30)), now=later)

    def test_ten_recovery_codes_stored_hashed(self):
        _, codes = self._enable()
        self.assertEqual(len(codes), 10)
        self.assertEqual(len(set(codes)), 10)
        with open(twofactor.TOTP_FILE) as f:
            raw = f.read()
        for c in codes:
            self.assertNotIn(c, raw)
        self.assertEqual(os.stat(twofactor.TOTP_FILE).st_mode & 0o777, 0o600)
        self.assertEqual(twofactor.status("panel:alex"), {"enabled": True, "recovery_left": 10})

    def test_code_window_and_replay(self):
        now = time.time()
        secret, _ = self._enable(now=now)
        step = int(now // 30)
        # The confirming code is used up
        self.assertIsNone(twofactor.verify("panel:alex", twofactor.code_at(secret, step), now=now))
        # Next step (clock drift) works once
        nxt = twofactor.code_at(secret, step + 1)
        self.assertEqual(twofactor.verify("panel:alex", nxt, now=now), "totp")
        self.assertIsNone(twofactor.verify("panel:alex", nxt, now=now))
        # Two steps away is too far
        self.assertIsNone(twofactor.verify("panel:alex", twofactor.code_at(secret, step + 3), now=now))
        # Spaces are fine
        c = twofactor.code_at(secret, step + 10)
        self.assertEqual(twofactor.verify("panel:alex", c[:3] + " " + c[3:], now=now + 300), "totp")

    def test_recovery_codes_work_once(self):
        _, codes = self._enable()
        self.assertEqual(twofactor.verify("panel:alex", codes[0].upper()), "recovery")
        self.assertIsNone(twofactor.verify("panel:alex", codes[0]))
        self.assertEqual(twofactor.status("panel:alex")["recovery_left"], 9)
        self.assertEqual(twofactor.verify("panel:alex", codes[1].replace("-", "")), "recovery")
        self.assertIsNone(twofactor.verify("panel:alex", "abcde-fghjk"))

    def test_new_recovery_codes_replace_old(self):
        _, old = self._enable()
        new = twofactor.new_recovery_codes("panel:alex")
        self.assertIsNone(twofactor.verify("panel:alex", old[0]))
        self.assertEqual(twofactor.verify("panel:alex", new[0]), "recovery")

    def test_accounts_are_separate_and_removable(self):
        self._enable("linux:alex")
        self.assertFalse(twofactor.is_enabled("panel:alex"))
        self.assertEqual(twofactor.enabled_keys(), {"linux:alex"})
        with self.assertRaises(twofactor.TwoFactorError):
            twofactor.begin_setup("linux:alex")
        self.assertTrue(twofactor.remove("linux:alex"))
        self.assertFalse(twofactor.remove("linux:alex"))
        self.assertFalse(twofactor.is_enabled("linux:alex"))

    def test_damaged_file_asks_everyone_for_a_code(self):
        with open(twofactor.TOTP_FILE, "w") as f:
            f.write("{not json")
        self.assertTrue(twofactor.is_enabled("panel:anyone"))
        self.assertIsNone(twofactor.verify("panel:anyone", "123456"))


class QrCode(unittest.TestCase):
    def test_sizes(self):
        self.assertEqual(len(qr.matrix("A")), 21)                     # version 1
        self.assertEqual(len(qr.matrix("x" * 14)), 21)                # version 1-M holds 14 bytes
        self.assertEqual(len(qr.matrix("x" * 15)), 25)
        self.assertEqual(len(qr.matrix("x" * 213)), 57)               # version 10
        with self.assertRaises(qr.QRError):
            qr.matrix("x" * 214)

    def test_finder_and_timing_patterns(self):
        m = qr.matrix("otpauth://totp/RedstonePanel:alex?secret=" + "A" * 32 + "&issuer=RedstonePanel")
        n = len(m)
        for ox, oy in ((0, 0), (n - 7, 0), (0, n - 7)):
            for y in range(7):
                for x in range(7):
                    ring = max(abs(x - 3), abs(y - 3))
                    self.assertEqual(m[oy + y][ox + x], ring != 2, (ox, oy, x, y))
        for i in range(8, n - 8):
            self.assertEqual(m[6][i], i % 2 == 0)
            self.assertEqual(m[i][6], i % 2 == 0)
        self.assertTrue(m[n - 8][8], "dark module")

    def test_known_output(self):
        # Checked against an independent decoder (zxing-cpp) when written: every version 1-10 and
        # every mask decoded back to its text. This pins the result so a change shows up here.
        m = qr.matrix("otpauth://totp/RedstonePanel:alex?secret=JBSWY3DPEHPK3PXP&issuer=RedstonePanel")
        bits = "".join("1" if v else "0" for row in m for v in row)
        self.assertEqual(len(m), 37)
        self.assertEqual(hashlib.sha256(bits.encode()).hexdigest()[:16], KNOWN_QR_HASH)

    def test_svg(self):
        s = qr.svg("hello")
        self.assertTrue(s.startswith("<svg "))
        self.assertIn('viewBox="0 0 29 29"', s)


KNOWN_QR_HASH = "c22da2f42bd3f9c3"


@unittest.skipUnless(HAVE_FASTAPI, "needs fastapi")
class SignIn(unittest.TestCase):
    PASSWORD = "correct horse"

    def setUp(self):
        import main
        self.main, self.auth = main, main.auth
        _isolate(self, auth=main.auth, settings=main.panel_settings)
        self.log = []
        p = mock.patch.object(main.manager, "write_log", self.log.append)
        p.start()
        self.addCleanup(p.stop)
        self.auth.create_panel_user("alex", self.PASSWORD, "admin")
        self.auth.create_panel_user("sam", self.PASSWORD, "admin")
        self.client = self.new_client()

    def new_client(self, user="alex"):
        c = TestClient(self.main.app)
        r = c.post("/api/auth/login", json={"username": user, "password": self.PASSWORD})
        self.assertEqual(r.status_code, 200, r.text)
        return c

    def code(self, secret, offset=0):
        return twofactor.code_at(secret, int(time.time() // 30) + offset)

    def wrong(self, secret):
        now = int(time.time() // 30)
        near = {twofactor.code_at(secret, s) for s in range(now - 2, now + 3)}
        return next(c for c in ("000000", "111111", "222222", "333333") if c not in near)

    def turn_on(self, client=None):
        c = client or self.client
        r = c.post("/api/auth/2fa/setup")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertIn("<svg", data["qr_svg"])
        self.assertIn(data["secret"], data["uri"])
        r = c.post("/api/auth/2fa/confirm", json={"code": self.code(data["secret"])})
        self.assertEqual(r.status_code, 200, r.text)
        return data["secret"], r.json()["recovery_codes"]

    def test_turning_on_keeps_this_browser_and_signs_out_others(self):
        other = self.new_client()
        secret, codes = self.turn_on()
        self.assertEqual(len(codes), 10)
        me = self.client.get("/api/auth/me")
        self.assertEqual(me.status_code, 200)
        self.assertTrue(me.json()["two_factor"])
        self.assertEqual(other.get("/api/auth/me").status_code, 401)
        self.assertEqual(self.client.get("/api/users").status_code, 200)

    def test_sign_in_needs_the_code(self):
        secret, _ = self.turn_on()
        c = TestClient(self.main.app)
        r = c.post("/api/auth/login", json={"username": "alex", "password": self.PASSWORD})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"code_needed": True})
        self.assertNotIn(self.auth.SESSION_COOKIE, c.cookies)
        self.assertEqual(c.get("/api/auth/me").status_code, 401)
        # The pending ticket is not a session
        c.cookies.set(self.auth.SESSION_COOKIE, c.cookies.get(self.auth.PENDING_COOKIE))
        self.assertEqual(c.get("/api/auth/me").status_code, 401)
        c.cookies.delete(self.auth.SESSION_COOKIE)
        self.assertEqual(c.post("/api/auth/login/code", json={"code": "12345"}).status_code, 401)
        r = c.post("/api/auth/login/code", json={"code": self.code(secret, 1)})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["username"], "alex")
        self.assertEqual(c.get("/api/auth/me").status_code, 200)
        self.assertTrue(any("signed in" in line and "app code" in line for line in self.log))

    def test_code_without_password_step_is_refused(self):
        self.turn_on()
        c = TestClient(self.main.app)
        r = c.post("/api/auth/login/code", json={"code": "123456"})
        self.assertEqual(r.status_code, 401)
        self.assertIn("password again", r.json()["detail"])

    def test_recovery_code_signs_in_once(self):
        _, codes = self.turn_on()
        for expect in (200, 401):
            c = TestClient(self.main.app)
            c.post("/api/auth/login", json={"username": "alex", "password": self.PASSWORD})
            r = c.post("/api/auth/login/code", json={"code": codes[3]})
            self.assertEqual(r.status_code, expect)
        self.assertEqual(r.status_code, 401)
        c = TestClient(self.main.app)
        c.post("/api/auth/login", json={"username": "alex", "password": self.PASSWORD})
        r = c.post("/api/auth/login/code", json={"code": codes[4]})
        self.assertEqual(r.json()["recovery_left"], 8)

    def test_wrong_codes_count_towards_the_rate_limit(self):
        secret, _ = self.turn_on()
        c = TestClient(self.main.app)
        for _ in range(self.auth.MAX_FAILS):
            # Re-entering the right password must not reset the count
            self.assertEqual(c.post("/api/auth/login", json={"username": "alex", "password": self.PASSWORD}).status_code, 200)
            self.assertEqual(c.post("/api/auth/login/code", json={"code": self.wrong(secret)}).status_code, 401)
        r = c.post("/api/auth/login", json={"username": "alex", "password": self.PASSWORD})
        self.assertEqual(r.status_code, 401)
        self.assertIn("Too many", r.json()["detail"])

    def test_turning_off_needs_a_code(self):
        secret, _ = self.turn_on()
        self.assertEqual(self.client.post("/api/auth/2fa/disable", json={"code": self.wrong(secret)}).status_code, 400)
        self.assertEqual(self.client.post("/api/auth/2fa/disable", json={"code": self.code(secret, 1)}).status_code, 200)
        self.assertFalse(self.client.get("/api/auth/2fa").json()["enabled"])
        self.assertEqual(self.client.get("/api/auth/me").status_code, 200)

    def test_new_recovery_codes(self):
        secret, old = self.turn_on()
        r = self.client.post("/api/auth/2fa/recovery-codes", json={"code": self.code(secret, 1)})
        self.assertEqual(r.status_code, 200)
        self.assertNotEqual(set(r.json()["recovery_codes"]), set(old))

    def test_password_change_keeps_a_two_factor_session(self):
        self.turn_on()
        r = self.client.post("/api/auth/password", json={"current_password": self.PASSWORD,
                                                         "new_password": "another good one"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get("/api/auth/me").status_code, 200)

    def test_require_for_admins(self):
        r = self.client.get("/api/settings")
        s = r.json()["settings"]
        self.assertFalse(s["require_2fa_admins"])
        s["require_2fa_admins"] = True
        # Not before the admin turning it on has it themselves
        r = self.client.put("/api/settings", json=s)
        self.assertEqual(r.status_code, 400)
        self.assertIn("your own account first", r.json()["detail"])
        self.turn_on()
        self.assertEqual(self.client.put("/api/settings", json=s).status_code, 200, r.text)
        # Another admin is held at setup until they do it
        sam = self.new_client("sam")
        me = sam.get("/api/auth/me").json()
        self.assertTrue(me["needs_2fa_setup"])
        self.assertEqual(sam.get("/api/servers").status_code, 403)
        self.assertEqual(sam.get("/api/users").status_code, 403)
        self.assertEqual(sam.get("/api/auth/2fa").json()["required"], True)
        self.turn_on(sam)
        self.assertFalse(sam.get("/api/auth/me").json()["needs_2fa_setup"])
        self.assertEqual(sam.get("/api/users").status_code, 200)
        # ...and can't turn it off while it's required
        self.assertEqual(sam.post("/api/auth/2fa/disable", json={"code": "123456"}).status_code, 400)
        # A user (not admin) isn't required to
        self.auth.create_panel_user("kid", self.PASSWORD, "user")
        kid = self.new_client("kid")
        self.assertFalse(kid.get("/api/auth/me").json()["needs_2fa_setup"])
        self.assertEqual(kid.get("/api/servers").status_code, 200)

    def test_admin_resets_another_accounts_two_factor(self):
        sam = self.new_client("sam")
        self.turn_on(sam)
        users = {u["username"]: u for u in self.client.get("/api/users").json()["users"]}
        self.assertTrue(users["sam"]["two_factor"])
        self.assertFalse(users["alex"]["two_factor"])
        self.assertEqual(self.client.post("/api/users/sam/reset-2fa").status_code, 200)
        self.assertEqual(self.client.post("/api/users/sam/reset-2fa").status_code, 400)
        self.assertEqual(self.client.post("/api/users/alex/reset-2fa").status_code, 400)
        self.assertEqual(self.client.post("/api/users/nobody/reset-2fa").status_code, 404)
        self.assertTrue(any("reset two-factor sign-in for panel user 'sam'" in line for line in self.log))
        # A user can't
        self.auth.create_panel_user("kid", self.PASSWORD, "user")
        self.assertEqual(self.new_client("kid").post("/api/users/sam/reset-2fa").status_code, 403)

    def test_deleting_a_user_forgets_their_two_factor(self):
        sam = self.new_client("sam")
        self.turn_on(sam)
        self.assertEqual(self.client.delete("/api/users/sam").status_code, 200)
        self.assertFalse(twofactor.is_enabled("panel:sam"))

    def test_trusted_lan_visitors_cant_set_it_up(self):
        trusted = {"username": "Local network", "source": "trusted", "role": "user"}
        with mock.patch.object(self.main.panel_settings, "trusted_network_user", lambda *a: dict(trusted)):
            r = TestClient(self.main.app).post("/api/auth/2fa/setup")
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
