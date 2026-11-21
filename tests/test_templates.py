"""Templates never carry a server's RCON password: saving leaves it out, applying keeps the
server's own, and templates saved by older versions lose it when read.
Run: python -m unittest discover -s tests"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-templates-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import cluster as C  # noqa: E402
from core import manager as M  # noqa: E402


class Templates(unittest.TestCase):
    def setUp(self):
        root = tempfile.mkdtemp(dir=TMP)
        self.dirs = {}
        for name, password in (("minecraft-java", "secret-a"), ("minecraft-java-2", "secret-b")):
            d = os.path.join(root, name)
            os.makedirs(d)
            with open(os.path.join(d, "server.properties"), "w") as f:
                f.write(f"server-port=2556{len(self.dirs) + 5}\nenable-rcon=true\nrcon.port=2557{len(self.dirs) + 5}\n"
                        f"rcon.password={password}\ndifficulty=hard\n")
            self.dirs[name] = d
        patches = [
            mock.patch.object(M, "TEMPLATE_FILE", os.path.join(root, "templates.json")),
            mock.patch.object(M, "get_server_dir", lambda n: self.dirs[n]),
            mock.patch.object(M, "read_permissions", lambda n: "[]"),
            mock.patch.object(M, "write_permissions", lambda n, c: None),
            mock.patch.object(M, "_is_java", lambda n: True),
            mock.patch.object(M.subprocess, "run", lambda *a, **k: mock.Mock(stdout="/usr/bin/java -jar server.jar")),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_saving_leaves_the_password_out(self):
        tid = M.save_server_as_template("minecraft-java", "Hard")
        with open(M.TEMPLATE_FILE) as f:
            stored = json.load(f)[0]
        self.assertEqual(stored["id"], tid)
        self.assertNotIn("rcon.password", stored["properties"])
        self.assertEqual(stored["properties"]["difficulty"], "hard")
        self.assertEqual(stored["properties"]["enable-rcon"], "true")

    def test_applying_keeps_the_servers_own_password(self):
        # A template saved before the fix, still holding another server's password
        with open(M.TEMPLATE_FILE, "w") as f:
            json.dump([{"id": "t1", "name": "Old", "edition": "Java", "permissions": "[]",
                        "properties": {"difficulty": "peaceful", "rcon.password": "secret-a"}}], f)
        self.assertNotIn("rcon.password", M.load_templates()[0]["properties"])
        M.apply_template_to_server("minecraft-java-2", "t1", edition="java")
        props = M.read_properties(self.dirs["minecraft-java-2"])
        self.assertEqual(props["rcon.password"], "secret-b")
        self.assertEqual(props["difficulty"], "peaceful")

    def test_editing_a_template_cant_add_one(self):
        M.save_server_as_template("minecraft-java", "Hard")
        tid = M.load_templates()[0]["id"]
        M.update_template(tid, {"difficulty": "easy", "rcon.password": "typed-in"}, "[]")
        with open(M.TEMPLATE_FILE) as f:
            self.assertNotIn("rcon.password", json.load(f)[0]["properties"])

    def test_a_template_pushed_to_a_node_has_none(self):
        t = {"id": "1f0c-aa", "name": "Survival", "edition": "Java", "permissions": "[]",
             "properties": {"difficulty": "hard", "rcon.password": "secret-a"}}
        self.assertEqual(C.clean_template(t, "Main")["properties"], {"difficulty": "hard"})


if __name__ == "__main__":
    unittest.main()
