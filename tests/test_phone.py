"""1.16.10 phone support: the "Open on your phone" QR code route and the app files the .deb ships."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from core import qr  # noqa: E402
from test_twofactor import HAVE_FASTAPI, _isolate  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")


@unittest.skipUnless(HAVE_FASTAPI, "needs fastapi")
class PhoneQr(unittest.TestCase):
    PASSWORD = "correct horse"

    def setUp(self):
        import main
        from fastapi.testclient import TestClient
        self.main = main
        _isolate(self, auth=main.auth, settings=main.panel_settings)
        main.auth.create_panel_user("alex", self.PASSWORD, "admin")
        self.anon = TestClient(main.app)
        self.client = TestClient(main.app)
        r = self.client.post("/api/auth/login", json={"username": "alex", "password": self.PASSWORD})
        self.assertEqual(r.status_code, 200, r.text)

    def test_svg_of_the_address(self):
        url = "https://192.168.1.20:8443/"
        r = self.client.get("/api/phone-qr", params={"url": url})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.headers["content-type"].startswith("image/svg+xml"))
        self.assertEqual(r.text, qr.svg(url))

    def test_only_web_addresses(self):
        for bad in ("javascript:alert(1)", "otpauth://totp/x", "ftp://a/", "https://a/\nb"):
            self.assertEqual(self.client.get("/api/phone-qr", params={"url": bad}).status_code, 400, bad)
        r = self.client.get("/api/phone-qr", params={"url": "https://a/" + "x" * 300})
        self.assertEqual(r.status_code, 400)

    def test_needs_sign_in(self):
        self.assertEqual(self.anon.get("/api/phone-qr", params={"url": "https://a/"}).status_code, 401)


class AppFiles(unittest.TestCase):
    def test_deb_ships_the_service_worker(self):
        with open(os.path.join(ROOT, "debian", "redstonepanel.install")) as f:
            installed = f.read().split()
        self.assertIn("static/sw.js", installed)
        self.assertIn("static/manifest.json", installed)

    def test_service_worker_caches_nothing(self):
        with open(os.path.join(ROOT, "static", "sw.js")) as f:
            sw = f.read()
        self.assertNotIn("caches.", sw)


class PhoneMenu(unittest.TestCase):
    """On a phone the ☰ menu scrolls as one page: the account footer follows the server list
    instead of being pinned over it, where it hid the last servers on short screens."""
    def _phone_css(self, path):
        with open(os.path.join(ROOT, *path)) as f:
            css = f.read()
        i = css.find("@media (max-width:767.98px)")
        if i < 0:
            i = css.find("@media (max-width: 767.98px)")
        self.assertGreaterEqual(i, 0, "no phone section in " + "/".join(path))
        return css[i:].replace(" ", "").replace("\n", "")

    def test_menu_scrolls_as_one(self):
        for path in (("packaging", "tailwind", "input.css"), ("static", "css", "tailwind.css")):
            css = self._phone_css(path)
            self.assertIn(".rp-sidebar{overflow-y:auto;", css, path)
            self.assertIn(".rp-sidebar>#server-list{flex:00auto;overflow:visible", css, path)
            self.assertIn(".rp-sidebar>.rp-sidebar-footer{margin-top:auto;", css, path)

    def test_footer_is_marked(self):
        with open(os.path.join(ROOT, "static", "index.html")) as f:
            html = f.read()
        self.assertIn('class="rp-sidebar-footer ', html)
        self.assertLess(html.index('id="server-list"'), html.index("rp-sidebar-footer"))


if __name__ == "__main__":
    unittest.main()
