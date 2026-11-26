"""1.24: Remember this device (a 30-day sign-in on one browser, until it is forgotten).
Run: python -m unittest discover -s tests
"""
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-devices-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import devices, twofactor  # noqa: E402

try:
    import fastapi  # noqa: F401
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except (ImportError, RuntimeError):
    HAVE_FASTAPI = False

UA_EDGE = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
           "Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0")
UA_IPHONE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1")


class Names(unittest.TestCase):
    def test_describe(self):
        self.assertEqual(devices.describe(UA_EDGE), "Edge on Windows")
        self.assertEqual(devices.describe(UA_IPHONE), "Safari on iPhone")
        self.assertEqual(devices.describe("Mozilla/5.0 (Linux; Android 15) Chrome/140.0 Mobile Safari/537.36"),
                         "Chrome on Android")
        self.assertEqual(devices.describe(""), "A browser")


@unittest.skipUnless(HAVE_FASTAPI, "needs FastAPI")
class Remember(unittest.TestCase):
    def setUp(self):
        import main
        self.main, self.auth = main, main.auth
        d = tempfile.mkdtemp(dir=TMP)
        self.log = []
        for p in (mock.patch.object(devices, "DEVICES_FILE", os.path.join(d, "devices.json")),
                  mock.patch.dict(devices._cache, {"mtime": None, "data": None}),
                  mock.patch.object(main.auth, "USERS_FILE", os.path.join(d, "users.json")),
                  mock.patch.object(main.auth, "SECRET_FILE", os.path.join(d, "secret.key")),
                  mock.patch.object(main.auth, "_secret", None),
                  mock.patch.dict(main.auth._fails, clear=True),
                  mock.patch.object(twofactor, "TOTP_FILE", os.path.join(d, "totp.json")),
                  mock.patch.dict(twofactor._cache, {"mtime": None, "data": None}),
                  mock.patch.object(main.manager, "write_log", self.log.append)):
            p.start()
            self.addCleanup(p.stop)
        self.auth.create_panel_user("alex", "alex password 1", "admin")

    def client(self, ua=UA_EDGE):
        return TestClient(self.main.app, base_url="http://127.0.0.1:8080", headers={"User-Agent": ua})

    def sign_in(self, c, remember):
        r = c.post("/api/auth/login", json={"username": "alex", "password": "alex password 1", "remember": remember})
        self.assertEqual(r.status_code, 200, r.text)
        return r

    def cookie_age(self, r):
        h = [v for v in r.headers.get_list("set-cookie") if v.startswith("rp_session=")][0]
        return int(h.split("Max-Age=")[1].split(";")[0])

    def test_plain_sign_in_is_12_hours_and_not_listed(self):
        c = self.client()
        r = self.sign_in(c, False)
        self.assertEqual(self.cookie_age(r), self.auth.SESSION_SECONDS)
        self.assertEqual(c.get("/api/auth/devices").json()["devices"], [])

    def test_remembered_for_30_days_and_listed(self):
        c = self.client()
        r = self.sign_in(c, True)
        self.assertEqual(self.cookie_age(r), devices.REMEMBER_SECONDS)
        self.assertEqual(devices.REMEMBER_DAYS, 30)
        d = c.get("/api/auth/devices").json()
        self.assertEqual(d["days"], 30)
        self.assertEqual(len(d["devices"]), 1)
        self.assertEqual(d["devices"][0]["name"], "Edge on Windows")
        self.assertTrue(d["devices"][0]["current"])
        self.assertTrue(any("remembering this device" in l for l in self.log), self.log)
        # still valid after 12 hours, gone after 30 days
        token = c.cookies.get("rp_session")
        with mock.patch("time.time", return_value=time.time() + 13 * 3600):
            self.assertIsNotNone(self.auth.read_session_token(token))
        with mock.patch("time.time", return_value=time.time() + 31 * 86400):
            self.assertIsNone(self.auth.read_session_token(token))

    def test_forget_signs_that_device_out(self):
        pc, phone = self.client(), self.client(UA_IPHONE)
        self.sign_in(pc, True)
        self.sign_in(phone, True)
        listed = pc.get("/api/auth/devices").json()["devices"]
        self.assertEqual(len(listed), 2)
        other = next(x for x in listed if not x["current"])
        self.assertEqual(other["name"], "Safari on iPhone")
        self.assertEqual(pc.delete(f"/api/auth/devices/{other['id']}").status_code, 200)
        self.assertEqual(phone.get("/api/auth/me").status_code, 401)
        self.assertEqual(pc.get("/api/auth/me").status_code, 200)
        self.assertEqual(pc.delete(f"/api/auth/devices/{other['id']}").status_code, 404)

    def test_forget_all_others_keeps_this_one(self):
        pc, phone, laptop = self.client(), self.client(UA_IPHONE), self.client()
        for c in (pc, phone, laptop):
            self.sign_in(c, True)
        self.assertEqual(pc.delete("/api/auth/devices").json(), {"forgotten": 2})
        self.assertEqual(pc.get("/api/auth/me").status_code, 200)
        self.assertEqual(phone.get("/api/auth/me").status_code, 401)
        self.assertEqual(laptop.get("/api/auth/me").status_code, 401)
        self.assertEqual(len(pc.get("/api/auth/devices").json()["devices"]), 1)

    def test_signing_out_forgets_the_device(self):
        c = self.client()
        self.sign_in(c, True)
        token = c.cookies.get("rp_session")
        c.post("/api/auth/logout")
        self.assertEqual(devices.list_for("panel:alex"), [])
        self.assertIsNone(self.auth.read_session_token(token))     # a copy of the cookie is no use either

    def test_new_password_forgets_every_device(self):
        c, phone = self.client(), self.client(UA_IPHONE)
        self.sign_in(c, True)
        self.sign_in(phone, True)
        r = c.post("/api/auth/password", json={"current_password": "alex password 1", "new_password": "alex password 2"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(devices.list_for("panel:alex"), [])
        self.assertEqual(phone.get("/api/auth/me").status_code, 401)
        self.assertEqual(c.get("/api/auth/me").status_code, 200)    # this browser gets a fresh (12-hour) cookie

    def test_deleted_account_forgets_its_devices(self):
        self.sign_in(self.client(), True)
        self.auth.create_panel_user("boss", "boss password 1", "admin")
        self.auth.delete_panel_user("alex")
        self.assertEqual(devices.list_for("panel:alex"), [])

    def test_two_factor_step_keeps_the_choice(self):
        key = twofactor.account_key("panel", "alex")
        secret = twofactor.begin_setup(key)
        step = int(time.time()) // 30
        twofactor.confirm_setup(key, twofactor.code_at(secret, step))
        c = self.client()
        r = self.sign_in(c, True)
        self.assertTrue(r.json().get("code_needed"))
        r = c.post("/api/auth/login/code", json={"code": twofactor.code_at(secret, step + 1)})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.cookie_age(r), devices.REMEMBER_SECONDS)
        self.assertEqual(len(devices.list_for(key)), 1)

    def test_others_devices_are_not_mine(self):
        self.auth.create_panel_user("sam", "sam password 1", "user")
        c = self.client()
        self.sign_in(c, True)
        sam = self.client()
        sam.post("/api/auth/login", json={"username": "sam", "password": "sam password 1"})
        alex_dev = devices.list_for("panel:alex")[0]["id"]
        self.assertEqual(sam.delete(f"/api/auth/devices/{alex_dev}").status_code, 404)
        self.assertEqual(sam.get("/api/auth/devices").json()["devices"], [])
        self.assertEqual(c.get("/api/auth/me").status_code, 200)

    def test_needs_sign_in(self):
        c = self.client()
        self.assertEqual(c.get("/api/auth/devices").status_code, 401)
        self.assertEqual(c.delete("/api/auth/devices").status_code, 401)

    def test_store_is_private(self):
        self.sign_in(self.client(), True)
        if os.name != "nt":
            self.assertEqual(os.stat(devices.DEVICES_FILE).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
