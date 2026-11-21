"""Tests for core/runtimes.py: which Java each server uses, installing the one a server needs
(and moving waiting servers onto it), removing an unused one, and choosing a server's Java.
apt, dpkg, systemctl and /usr/lib/jvm are faked. Run: python -m unittest discover -s tests"""
import os
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-runtimes-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import runtimes as R  # noqa: E402

try:
    import fastapi  # noqa: F401
    HAVE_FASTAPI = True
except ImportError:
    HAVE_FASTAPI = False

JVM = "/usr/lib/jvm/java-{}-openjdk-amd64/bin/java"


def srv(name, required, using, path=None):
    return {"name": name, "edition": "Java", "java_required": required, "java_installed": using,
            "java_path": path or (JVM.format(using) if using else "/usr/bin/java")}


class Base(unittest.TestCase):
    def setUp(self):
        self.javas = {21: JVM.format(21), 25: JVM.format(25)}
        self.servers = []
        self.owners = {}
        self.candidates = {}
        self.apt_calls = []
        self.apt_code = 0
        self.units = []
        self.ran = []
        self.states = {}
        self.others = []
        self.orphans = {"before": ["old-lib"], "after": ["old-lib", "java-common", "ca-certificates-java"]}

        def apt(args):
            self.apt_calls.append(list(args))
            if args[0] == "update":
                self.candidates.setdefault("openjdk-17-jre-headless", "17.0.13")
            if args[0] == "install" and self.apt_code == 0:
                major = int(args[-1].split("-")[1])
                self.javas[major] = JVM.format(major)
            return self.apt_code

        def write_unit(name, d, edition, java="/usr/bin/java", **heap):
            self.units.append((name, java, heap))
            for s in self.servers:
                if s["name"] == name:
                    s["java_path"] = java
                    s["java_installed"] = int(java.split("java-")[1].split("-")[0])

        R._job.clear()
        patches = [
            mock.patch.object(R.javaservers, "installed_javas", lambda: dict(self.javas)),
            mock.patch.object(R.manager, "list_servers", lambda: [dict(s) for s in self.servers]),
            mock.patch.object(R, "owner", lambda path: self.owners.get(path)),
            mock.patch.object(R, "candidate", lambda pkg: self.candidates.get(pkg)),
            mock.patch.object(R, "_would_remove", lambda *pkgs: list(self.others)),
            mock.patch.object(R, "_autoremovable", lambda: list(self.autoremovable())),
            mock.patch.object(R, "_default_major", lambda: 25),
            mock.patch.object(R.javaservers, "packaged_javas", lambda refresh=False: R.javaservers.UBUNTU_JAVA),
            mock.patch.object(R, "_apt", apt),
            mock.patch.object(R, "_systemctl", lambda verb, name: self.states.get((verb, name), "")),
            mock.patch.object(R.manager, "_write_unit", write_unit),
            mock.patch.object(R.manager, "_unit_exec_start",
                              lambda name: "/usr/bin/java -Xms1G -Xmx3G -jar server.jar nogui"),
            mock.patch.object(R.manager, "get_server_dir", lambda name: f"/opt/{name}"),
            mock.patch.object(R.manager, "_check_server", lambda name: None),
            mock.patch.object(R.manager, "run_and_log", lambda cmd: self.ran.append(cmd)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def autoremovable(self):
        removed = any(c[0] == "purge" for c in self.apt_calls)
        return self.orphans["after" if removed else "before"]

    def wait(self):
        for _ in range(200):
            j = R.job()
            if j and j["state"] != "running":
                return j
            time.sleep(0.01)
        self.fail("job didn't finish")


class Status(Base):
    def test_usage_needed_and_switch(self):
        self.owners = {JVM.format(21): "openjdk-21-jre-headless", JVM.format(25): "openjdk-25-jre-headless"}
        self.servers = [srv("minecraft-a", 21, 21), srv("minecraft-b", 21, 25), srv("minecraft-c", 17, None),
                        {"name": "minecraft-bedrock", "edition": "Bedrock"}]
        st = R.status()
        by = {j["major"]: j for j in st["installed"]}
        self.assertEqual(by[21]["servers"], ["minecraft-a"])
        self.assertEqual(by[25]["servers"], ["minecraft-b"])
        self.assertTrue(by[25]["default"])
        self.assertFalse(by[21]["removable"])
        self.assertEqual(st["needed"], [{"major": 17, "servers": ["minecraft-c"], "package": "openjdk-17-jre-headless"}])
        self.assertEqual(st["switch"], [{"name": "minecraft-b", "using": 25, "best": 21}])
        self.assertEqual([i["major"] for i in st["installable"]], [8, 11, 17])

    def test_unused_and_not_ours(self):
        self.owners = {JVM.format(21): "openjdk-21-jdk-headless", JVM.format(25): "openjdk-25-jre-headless"}
        st = R.status()
        by = {j["major"]: j for j in st["installed"]}
        self.assertTrue(by[25]["removable"])
        self.assertFalse(by[21]["removable"])
        self.assertIn("openjdk-21-jdk-headless", by[21]["why_not"])

    def test_package_names_only_from_the_list(self):
        self.assertEqual(R.package_for(21), "openjdk-21-jre-headless")
        self.assertEqual(R.package_for("17"), "openjdk-17-jre-headless")
        for bad in (16, 99, "21; rm -rf /", None, "x"):
            with self.assertRaises(R.RuntimeJobError):
                R.package_for(bad)


class Install(Base):
    def test_installs_then_moves_waiting_servers(self):
        del self.javas[21]
        self.candidates = {"openjdk-21-jre-headless": "21.0.9"}
        self.servers = [srv("minecraft-imported", 21, None), srv("minecraft-ok", 25, 25),
                        srv("minecraft-stopped", 21, None)]
        self.states = {("is-enabled", "minecraft-imported"): "enabled", ("is-active", "minecraft-imported"): "failed",
                       ("is-enabled", "minecraft-stopped"): "disabled"}
        job = R.install(21, actor="alex")
        self.assertEqual((job["action"], job["major"], job["state"]), ("install", 21, "running"))
        j = self.wait()
        self.assertEqual(j["state"], "done", j)
        self.assertEqual(self.apt_calls, [["install", "-y", "--no-install-recommends", "openjdk-21-jre-headless"]])
        self.assertEqual(self.units, [("minecraft-imported", JVM.format(21), {"xms": "1G", "xmx": "3G"}),
                                      ("minecraft-stopped", JVM.format(21), {"xms": "1G", "xmx": "3G"})])
        self.assertEqual(self.ran, [["systemctl", "restart", "minecraft-imported.service"]])   # not the disabled one
        self.assertEqual([r["name"] for r in j["rebound"]], ["minecraft-imported", "minecraft-stopped"])

    def test_updates_package_lists_when_needed(self):
        self.javas = {}
        R.install(17)
        j = self.wait()
        self.assertEqual(j["state"], "done", j)
        self.assertEqual([c[0] for c in self.apt_calls], ["update", "install"])

    def test_package_not_available(self):
        self.javas = {}
        R.install(8)
        j = self.wait()
        self.assertEqual(j["state"], "failed")
        self.assertIn("don't have openjdk-8-jre-headless", j["error"])
        self.assertEqual([c[0] for c in self.apt_calls], ["update"])

    def test_apt_failure(self):
        self.javas = {}
        self.candidates = {"openjdk-21-jre-headless": "21.0.9"}
        self.apt_code = 100
        R.install(21)
        j = self.wait()
        self.assertEqual(j["state"], "failed")
        self.assertIn("Installing openjdk-21-jre-headless failed", j["error"])

    def test_already_installed_and_one_at_a_time(self):
        with self.assertRaises(R.RuntimeJobError):
            R.install(21)
        R._job.update(state="running", action="remove", major=8)
        with self.assertRaises(R.RuntimeJobError) as cm:
            R.install(17)
        self.assertEqual(cm.exception.status, 409)
        self.assertIn("Already removing Java 8", str(cm.exception))


class Remove(Base):
    def setUp(self):
        super().setUp()
        self.owners = {JVM.format(21): "openjdk-21-jre-headless", JVM.format(25): "openjdk-25-jre-headless"}

    def test_removes_unused(self):
        self.servers = [srv("minecraft-a", 25, 25)]
        R.remove(21)
        j = self.wait()
        self.assertEqual(j["state"], "done", j)
        self.assertEqual(self.apt_calls, [["purge", "-y", "openjdk-21-jre-headless"],
                                          ["purge", "-y", "ca-certificates-java", "java-common"]])   # not old-lib

    def test_refuses_while_used(self):
        self.servers = [srv("minecraft-a", 21, 21), srv("minecraft-b", 17, 21)]
        with self.assertRaises(R.RuntimeJobError) as cm:
            R.remove(21)
        self.assertIn("used by minecraft-a, minecraft-b", str(cm.exception))
        self.assertEqual(self.apt_calls, [])

    def test_refuses_other_packages(self):
        self.owners[JVM.format(21)] = "openjdk-21-jdk-headless"
        with self.assertRaises(R.RuntimeJobError) as cm:
            R.remove(21)
        self.assertIn("leaves it alone", str(cm.exception))
        self.owners[JVM.format(21)] = "openjdk-21-jre-headless"
        self.others = ["openjdk-21-jdk-headless", "some-app"]
        with self.assertRaises(R.RuntimeJobError) as cm:
            R.remove(21)
        self.assertIn("would also remove openjdk-21-jdk-headless, some-app", str(cm.exception))
        with self.assertRaises(R.RuntimeJobError):
            R.remove(17)                                  # not installed
        self.assertEqual(self.apt_calls, [])

    def test_server_started_using_it_meanwhile(self):
        R.removal_check(21)
        self.servers = [srv("minecraft-a", 21, 21)]
        R._start("remove", 21, "openjdk-21-jre-headless", "alex", lambda: R._remove(21, "openjdk-21-jre-headless"))
        self.assertEqual(self.wait()["state"], "failed")
        self.assertEqual(self.apt_calls, [])

    def test_would_remove_parses_dry_run(self):
        out = ("NOTE: This is only a simulation!\nPurg openjdk-21-jdk-headless [21.0.9]\n"
               "Purg openjdk-21-jre-headless:amd64 [21.0.9]\nRemv old-thing [1]\n")
        real = Remove._real_would_remove
        with mock.patch.object(R.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=out, stderr="")):
            self.assertEqual(real("openjdk-21-jre-headless"), ["old-thing", "openjdk-21-jdk-headless"])


Remove._real_would_remove = staticmethod(R._would_remove)


class Use(Base):
    def test_switch_and_restart(self):
        self.servers = [srv("minecraft-a", 21, 25)]
        self.states = {("is-active", "minecraft-a"): "active"}
        r = R.use("minecraft-a", 21)
        self.assertEqual((r["java"], r["restarted"]), (21, True))
        self.assertEqual(self.units, [("minecraft-a", JVM.format(21), {"xms": "1G", "xmx": "3G"})])
        self.assertEqual(self.ran, [["systemctl", "restart", "minecraft-a.service"]])

    def test_refuses_too_old_or_missing(self):
        self.servers = [srv("minecraft-a", 25, 25)]
        with self.assertRaises(R.RuntimeJobError) as cm:
            R.use("minecraft-a", 21)
        self.assertIn("needs Java 25 or newer", str(cm.exception))
        with self.assertRaises(R.RuntimeJobError):
            R.use("minecraft-a", 17)
        self.assertEqual(self.units, [])


@unittest.skipUnless(HAVE_FASTAPI, "needs fastapi")
class Api(unittest.TestCase):
    def test_admins_only(self):
        import main
        req = SimpleNamespace(state=SimpleNamespace(user={"username": "alex", "role": "user", "source": "panel"}))
        for call in (lambda: main.java_runtimes(req), lambda: main.java_runtime_job(req),
                     lambda: main.install_java_runtime(req, main.JavaChoice(major=21)),
                     lambda: main.remove_java_runtime(req, main.JavaChoice(major=21)),
                     lambda: main.choose_server_java(req, "minecraft-java", main.JavaChoice(major=21))):
            with self.assertRaises(main.HTTPException) as cm:
                call()
            self.assertEqual(cm.exception.status_code, 403)

    def test_missing_java_is_409_with_the_package(self):
        import asyncio
        import json
        import main
        from core import javaservers, manager
        e = manager.JavaMissing(javaservers.MissingJavaError(21, "1.21.4"))
        self.assertIsInstance(e, manager.DependencyError)
        res = asyncio.run(main.java_missing(None, e))
        self.assertEqual(res.status_code, 409)
        body = json.loads(res.body)
        self.assertEqual(body["java"], {"required": 21, "major": 21, "package": "openjdk-21-jre-headless"})
        self.assertIn("needs Java 21", body["detail"])


if __name__ == "__main__":
    unittest.main()
