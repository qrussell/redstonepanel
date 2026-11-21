"""The panel's pages and files are revalidated on every load and link app.js and the stylesheet
with the panel version, so a browser never keeps running an old app.js after an update.
Run: python -m unittest discover -s tests"""
import asyncio
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-static-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import fastapi  # noqa: F401
    HAVE_FASTAPI = True
except ImportError:
    HAVE_FASTAPI = False


@unittest.skipUnless(HAVE_FASTAPI, "needs fastapi")
class Pages(unittest.TestCase):
    def setUp(self):
        import main
        self.main = main
        main._page_cache.clear()

    def test_index_links_versioned_assets(self):
        with mock.patch.object(self.main, "panel_version", lambda: "1.13.1"):
            r = self.main.index_page()
        body = r.body.decode()
        self.assertEqual(r.headers["cache-control"], "no-cache")
        self.assertIn('"/static/js/app.js?v=1.13.1"', body)
        self.assertIn('"/static/css/tailwind.css?v=1.13.1"', body)
        self.assertNotIn('"/static/js/app.js"', body)

    def test_login_page(self):
        with mock.patch.object(self.main, "panel_version", lambda: "2.0.0~rc1"):
            r = self.main.login_page()
        self.assertIn('"/static/css/tailwind.css?v=2.0.0~rc1"', r.body.decode())
        self.assertEqual(r.headers["cache-control"], "no-cache")

    def test_other_static_files_get_no_cache(self):
        async def call_next(request):
            return SimpleNamespace(headers={})
        for path, expected in (("/static/js/app.js", "no-cache"), ("/api/servers", None)):
            req = SimpleNamespace(url=SimpleNamespace(path=path))
            r = asyncio.run(self.main.no_stale_static(req, call_next))
            self.assertEqual(r.headers.get("Cache-Control"), expected, path)


class Packaging(unittest.TestCase):
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def test_every_static_file_is_installed(self):
        # debian/redstonepanel.install names each file, so a new icon or page left out of it
        # would be missing from the .deb (a broken image or 404 only on real installs)
        with open(os.path.join(self.ROOT, "debian", "redstonepanel.install")) as f:
            listed = {p for line in f for p in line.split()[:-1]}
        for dirpath, _, files in os.walk(os.path.join(self.ROOT, "static")):
            for name in files:
                rel = os.path.relpath(os.path.join(dirpath, name), self.ROOT).replace(os.sep, "/")
                self.assertIn(rel, listed, f"{rel} is not in debian/redstonepanel.install")

    def test_pages_link_the_icons(self):
        for page in ("index.html", "login.html"):
            with open(os.path.join(self.ROOT, "static", page), encoding="utf-8") as f:
                html = f.read()
            for href in ("/static/img/redstonepanel-icon.svg", "/static/img/favicon.ico",
                         "/static/img/apple-touch-icon.png", "/static/manifest.json"):
                self.assertIn(f'href="{href}"', html, page)
                self.assertTrue(os.path.isfile(os.path.join(self.ROOT, href.lstrip("/"))), href)


if __name__ == "__main__":
    unittest.main()
