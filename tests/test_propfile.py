"""The Files tab's Visual Editor (1.21.7): the logic is static/js/propfile.js, tested with Node
(tests/js/propfile.test.js). Here: the package ships it, the page loads it before app.js, and
it agrees with core/properties.py on Java's escapes."""
import json
import os
import shutil
import subprocess
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import properties  # noqa: E402

try:
    import fastapi  # noqa: F401
    HAVE_FASTAPI = True
except ImportError:
    HAVE_FASTAPI = False


class Shipped(unittest.TestCase):
    def test_the_package_ships_the_script_and_the_page_loads_it_first(self):
        with open(os.path.join(ROOT, "debian", "redstonepanel.install")) as f:
            self.assertIn("static/js/propfile.js", f.read())
        with open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8") as f:
            page = f.read()
        self.assertLess(page.index('"/static/js/propfile.js"'), page.index('"/static/js/app.js"'))


@unittest.skipUnless(shutil.which("node"), "needs Node.js")
class Logic(unittest.TestCase):
    def test_node_tests_pass(self):
        r = subprocess.run(["node", "--test", os.path.join(ROOT, "tests", "js", "propfile.test.js")],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout[-3000:] + r.stderr[-1000:])

    def test_it_reads_and_writes_java_values_as_the_panel_does(self):
        values = {"motd": "A é Server: hi! #1", "rcon.password": " p=1:2\\3 ", "level-name": "my world", "empty": ""}
        text = properties.update("#header\n", values)
        script = ("const PF = require(process.argv[1]); const t = require('fs').readFileSync(0, 'utf8');"
                  "const p = PF.parse(t, 'server.properties'); let out = t;"
                  "for (const e of p.entries) out = PF.setValue(out, e.line, e.value + '!', 'server.properties');"
                  "console.log(JSON.stringify({read: Object.fromEntries(p.entries.map(e => [e.key, e.value])), out}));")
        r = subprocess.run(["node", "-e", script, os.path.join(ROOT, "static", "js", "propfile.js")],
                           input=text, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        got = json.loads(r.stdout)
        self.assertEqual(got["read"], values)
        self.assertEqual(properties.parse(got["out"]), {k: v + "!" for k, v in values.items()})
        self.assertTrue(got["out"].startswith("#header\n"))


@unittest.skipUnless(HAVE_FASTAPI, "needs fastapi")
class Page(unittest.TestCase):
    def test_the_page_links_the_script_with_the_version(self):
        import main
        main._page_cache.clear()
        with mock.patch.object(main, "panel_version", lambda: "1.21.7"):
            body = main.index_page().body.decode()
        main._page_cache.clear()
        self.assertIn('"/static/js/propfile.js?v=1.21.7"', body)


if __name__ == "__main__":
    unittest.main()
