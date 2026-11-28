"""Cloudflare Tunnel set up by the panel (1.29): finding cloudflared, the API token, routes merged
into a tunnel managed in Cloudflare, config.yml for one managed in a file, DNS names, Cloudflare
Access, Remove, the visitor's address through the tunnel, and clusters (only the master). Cloudflare
itself is a stand-in (cftunnel.api); cloudflared is the fake platform's."""
import base64
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

TMP = tempfile.mkdtemp(prefix="rp-test-cftunnel-")
os.environ.setdefault("RP_DATA_DIR", os.path.join(TMP, "data"))
os.environ.setdefault("RP_LOG_FILE", os.path.join(TMP, "panel.log"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import cftunnel as T          # noqa: E402
from core import cluster                # noqa: E402
from core import discordauth as D       # noqa: E402
from core import manager                # noqa: E402
from core import platform               # noqa: E402
from core import settings as P          # noqa: E402
from core.platform import fake          # noqa: E402

ACCOUNT = "a" * 32
ZONE = "c" * 32
OTHER_ZONE = "d" * 32
TUNNEL = "6ff42ae2-765d-4adf-8112-31c55c1551ef"
API_TOKEN = "Abc123_" + "x" * 33
SECRET = "c2VjcmV0LXRoYXQtbXVzdC1uZXZlci1iZS1rZXB0"


def tunnel_token(account=ACCOUNT, tunnel=TUNNEL, secret=SECRET):
    return base64.b64encode(json.dumps({"a": account, "t": tunnel, "s": secret}).encode()).decode()


class FakeCloudflare:
    """api.cloudflare.com, as much as the panel uses."""

    def __init__(self):
        self.zones = {ZONE: {"id": ZONE, "name": "example.org", "account": {"id": ACCOUNT}},
                      OTHER_ZONE: {"id": OTHER_ZONE, "name": "example.net", "account": {"id": ACCOUNT}}}
        self.config = {"ingress": [{"hostname": "nas.example.org", "service": "http://10.0.0.9:5000"},
                                   {"service": "http_status:404"}], "warp-routing": {"enabled": False}}
        self.dns = {"r-nas": {"id": "r-nas", "type": "CNAME", "name": "nas.example.org",
                              "content": f"{TUNNEL}.cfargotunnel.com", "proxied": True}}
        self.apps = {}
        self.calls = []
        self.tokens = {API_TOKEN}
        self.access_allowed = True
        self.n = 0

    def __call__(self, method, path, body=None, token=None, query=None, timeout=20):
        self.calls.append((method, path, json.loads(json.dumps(body)) if body is not None else None))
        if token not in self.tokens:
            raise T.ApiError(403, "Cloudflare: Authentication error")
        if path == "/zones":
            return list(self.zones.values())
        if path == f"/accounts/{ACCOUNT}/cfd_tunnel/{TUNNEL}/configurations":
            if method == "GET":
                return {"tunnel_id": TUNNEL, "config": json.loads(json.dumps(self.config))}
            self.config = body["config"]
            return {"tunnel_id": TUNNEL}
        if path == f"/accounts/{ACCOUNT}/cfd_tunnel/{TUNNEL}":
            return {"id": TUNNEL, "status": "healthy", "connections": [{}, {}, {}, {}]}
        if path == f"/accounts/{ACCOUNT}/cfd_tunnel":
            if query and query.get("name"):
                return [{"id": TUNNEL, "name": "home"}] if query["name"] == "home" else []
            return [{"id": TUNNEL, "name": "home", "status": "healthy"}]
        if path == f"/zones/{ZONE}/dns_records":
            if method == "GET":
                return [r for r in self.dns.values() if r["name"] == query["name"]]
            self.n += 1
            rec = dict(body, id=f"r{self.n}")
            self.dns[rec["id"]] = rec
            return rec
        if path.startswith(f"/zones/{ZONE}/dns_records/"):
            rid = path.rsplit("/", 1)[1]
            if rid not in self.dns:
                raise T.ApiError(404, "Cloudflare: Record does not exist.")
            if method == "DELETE":
                del self.dns[rid]
                return {"id": rid}
            self.dns[rid].update(body)
            return self.dns[rid]
        if path.startswith(f"/accounts/{ACCOUNT}/access/apps"):
            if not self.access_allowed:
                raise T.ApiError(403, "Cloudflare: Access denied")
            if method == "POST":
                self.n += 1
                self.apps[f"app{self.n}"] = body
                return {"id": f"app{self.n}"}
            aid = path.rsplit("/", 1)[1]
            if method == "DELETE":
                self.apps.pop(aid, None)
                return {"id": aid}
            self.apps[aid] = body
            return {"id": aid}
        raise AssertionError(f"unexpected call {method} {path}")


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(dir=TMP)
        self.addCleanup(shutil.rmtree, self.dir, True)
        platform.use("fake")
        self.addCleanup(platform.use)
        fake.reset()
        self.cf = FakeCloudflare()
        self.log = []
        self.role = "standalone"
        d = self.dir
        for p in (mock.patch.object(T, "STATE_FILE", os.path.join(d, "cloudflare-tunnel.json")),
                  mock.patch.object(T, "api", self.cf),
                  mock.patch.object(T, "_metrics_ready", lambda timeout=30, quiet_after=5: None),
                  mock.patch.dict(T._state_cache, {"mtime": None, "data": None}),
                  mock.patch.dict(T._detect_cache, {"at": 0.0, "value": None}),
                  mock.patch.object(P, "SETTINGS_FILE", os.path.join(d, "settings.json")),
                  mock.patch.object(P, "DATA_DIR", d),
                  mock.patch.dict(P._cache, {"mtime": None, "data": None}),
                  mock.patch.object(D, "SIGNIN_FILE", os.path.join(d, "discord-signin.json")),
                  mock.patch.object(manager, "write_log", lambda m: self.log.append(m)),
                  mock.patch.object(cluster, "role", lambda: self.role)):
            p.start()
            self.addCleanup(p.stop)
        P.save(P.validate(dict(P.DEFAULTS, join_enabled=True, https_mode="self_signed")))

    def remote_service(self, token=None):
        fake.state.tunnel_services = [{"service": "cloudflared.service", "running": True,
                                       "argv": ["/usr/bin/cloudflared", "--no-autoupdate", "tunnel", "run",
                                                "--token", token or tunnel_token()]}]

    def local_service(self, text):
        path = os.path.join(self.dir, "config.yml")
        with open(path, "w") as f:
            f.write(text)
        fake.state.tunnel_services = [{"service": "cloudflared.service", "running": True,
                                       "argv": ["/usr/bin/cloudflared", "--config", path, "tunnel", "run"]}]
        return path

    def configure(self, **kw):
        new = dict({"token": API_TOKEN, "zone_id": ZONE, "panel_host": "panel", "join_host": "play"}, **kw)
        return T.save_settings(new, "alex")


class FindingCloudflared(Base):
    def test_the_tunnel_token_gives_account_and_tunnel_and_its_secret_is_dropped(self):
        self.assertEqual(T.token_ids(tunnel_token()), (ACCOUNT, TUNNEL))
        self.assertEqual(T.token_ids("not a token"), (None, None))
        self.remote_service()
        det = T.detect(fresh=True)
        self.assertEqual((det["mode"], det["account"], det["tunnel_id"], det["running"]), ("remote", ACCOUNT, TUNNEL, True))
        self.assertNotIn(SECRET, json.dumps(det))
        self.assertNotIn(tunnel_token(), json.dumps(det))
        self.configure()
        pub = json.dumps(T.public())
        self.assertNotIn(SECRET, pub)
        self.assertNotIn(API_TOKEN, pub)
        self.assertIn(API_TOKEN[-4:], pub)

    def test_command_lines(self):
        self.assertEqual(T.parse_command(["cf", "tunnel", "run", "--token=abc"])["token"], "abc")
        self.assertEqual(T.parse_command(["cf", "--config", "/x.yml", "tunnel", "run"])["config"], "/x.yml")
        self.assertEqual(T.parse_command(["cf", "tunnel", "run", "--token-file", "/t"])["token_file"], "/t")

    def test_a_token_file(self):
        path = os.path.join(self.dir, "token")
        with open(path, "w") as f:
            f.write(tunnel_token() + "\n")
        fake.state.tunnel_services = [{"service": "cloudflared.service", "running": False,
                                       "argv": ["cloudflared", "tunnel", "run", "--token-file", path]}]
        det = T.detect(fresh=True)
        self.assertEqual((det["mode"], det["tunnel_id"], det["running"]), ("remote", TUNNEL, False))

    def test_a_file_managed_tunnel(self):
        path = self.local_service(f"tunnel: {TUNNEL}\ncredentials-file: /root/.cloudflared/x.json\n"
                                  "ingress:\n  - service: http_status:404\n")
        det = T.detect(fresh=True)
        self.assertEqual((det["mode"], det["config"], det["tunnel_id"]), ("local", path, TUNNEL))

    def test_the_default_file_when_the_service_names_none(self):
        path = os.path.join(self.dir, "default.yml")
        with open(path, "w") as f:
            f.write("tunnel: home\n")
        fake.state.tunnel_configs = [os.path.join(self.dir, "missing.yml"), path]
        fake.state.tunnel_services = [{"service": "cloudflared.service", "running": True,
                                       "argv": ["cloudflared", "tunnel", "run"]}]
        det = T.detect(fresh=True)
        self.assertEqual((det["mode"], det["config"], det["tunnel_id"]), ("local", path, "home"))

    def test_not_installed(self):
        det = T.detect(fresh=True)
        self.assertFalse(det["installed"])
        self.configure()
        with self.assertRaisesRegex(T.TunnelError, "isn't installed"):
            T.apply("alex")


class Settings(Base):
    def test_the_tunnel_token_is_not_an_api_token(self):
        with self.assertRaisesRegex(T.TunnelError, "tunnel's own token"):
            T.save_settings({"token": tunnel_token()}, "alex")
        with self.assertRaisesRegex(T.TunnelError, "didn't accept"):
            T.save_settings({"token": "Z" * 40}, "alex")

    def test_names_inside_the_domain(self):
        pub = self.configure()
        self.assertEqual((pub["panel_host"], pub["join_host"], pub["zone"]), ("panel.example.org", "play.example.org",
                                                                              "example.org"))
        with self.assertRaisesRegex(T.TunnelError, "must be in example.org"):
            T.save_settings({"panel_host": "panel.example.net"}, "alex")
        with self.assertRaisesRegex(T.TunnelError, "name alone"):
            T.save_settings({"panel_host": "https://panel.example.org"}, "alex")
        with self.assertRaisesRegex(T.TunnelError, "one of the domains"):
            T.save_settings({"zone_id": "e" * 32}, "alex")

    def test_the_token_never_reaches_the_log(self):
        self.remote_service()
        self.configure()
        T.apply("alex")
        T.remove("alex")
        self.assertTrue(self.log)
        for line in self.log:
            self.assertNotIn(API_TOKEN, line)
            self.assertNotIn(SECRET, line)

    def test_forgetting_the_token_needs_remove_first(self):
        self.remote_service()
        self.configure()
        T.apply("alex")
        with self.assertRaisesRegex(T.TunnelError, "Remove"):
            T.save_settings({"forget_token": True}, "alex")
        T.remove("alex")
        self.assertFalse(T.save_settings({"forget_token": True}, "alex")["has_token"])


class RemoteTunnel(Base):
    def setUp(self):
        super().setUp()
        self.remote_service()
        self.configure()

    def test_apply_puts_the_panels_routes_first_and_keeps_the_rest(self):
        out = T.apply("alex")
        ing = self.cf.config["ingress"]
        self.assertEqual([r.get("hostname") for r in ing], ["play.example.org", "panel.example.org", "nas.example.org", None])
        self.assertEqual(ing[0], {"hostname": "play.example.org", "path": T.JOIN_PATH, "service": "https://localhost:8444",
                                  "originRequest": {"noTLSVerify": True}})
        self.assertEqual(ing[1]["service"], "https://localhost:8443")
        self.assertEqual(ing[-1], {"service": "http_status:404"})
        self.assertEqual(self.cf.config["warp-routing"], {"enabled": False}, "the rest of the tunnel's settings stay")
        made = [r for r in self.cf.dns.values() if r.get("comment") == T.DNS_COMMENT]
        self.assertEqual(sorted(r["name"] for r in made), ["panel.example.org", "play.example.org"])
        self.assertTrue(all(r["proxied"] and r["content"] == f"{TUNNEL}.cfargotunnel.com" for r in made))
        self.assertTrue(out["done"])
        s = P.load()
        self.assertEqual(s["join_url"], "https://play.example.org")
        self.assertEqual(s["join_proxies"], ["127.0.0.1", "::1"])
        self.assertEqual(T.public_panel_url(), "https://panel.example.org")
        self.assertEqual(P.panel_url(s, "panel.example.org"), "https://panel.example.org")
        self.assertEqual(P.panel_url(s, "10.0.0.5:8443"), "https://10.0.0.5:8443")

    def test_apply_twice_changes_nothing_twice(self):
        T.apply("alex")
        first = json.dumps(self.cf.config)
        n = len(self.cf.dns)
        T.apply("alex")
        self.assertEqual(json.dumps(self.cf.config), first)
        self.assertEqual(len(self.cf.dns), n)

    def test_routes_follow_the_join_port_and_https(self):
        T.apply("alex")
        P.save(P.validate(dict(P.load(), join_port=9000, https_mode="off")))
        self.assertTrue(T.sync())
        ing = self.cf.config["ingress"]
        self.assertEqual([r["service"] for r in ing[:2]], ["http://localhost:9000", "http://localhost:8080"])
        self.assertNotIn("originRequest", ing[0])
        self.assertEqual(len(ing), 4)
        self.assertFalse(T.sync(), "nothing to change")

    def test_turning_public_access_to_the_admin_panel_off(self):
        T.apply("alex")
        T.save_settings({"panel_public": False}, "alex")
        T.apply("alex")
        self.assertEqual([r.get("hostname") for r in self.cf.config["ingress"]], ["play.example.org", "nas.example.org", None])
        self.assertEqual(sorted(r["name"] for r in self.cf.dns.values()), ["nas.example.org", "play.example.org"])
        self.assertEqual(T.public_panel_url(), "")

    def test_someone_elses_route_for_the_same_name(self):
        self.cf.config["ingress"].insert(0, {"hostname": "panel.example.org", "service": "http://10.0.0.7:3000"})
        with self.assertRaisesRegex(T.TunnelError, "isn't this panel"):
            T.apply("alex")
        self.assertEqual(self.cf.config["ingress"][0]["service"], "http://10.0.0.7:3000", "nothing changed")

    def test_the_1_21_9_route_is_taken_over(self):
        self.cf.config["ingress"].insert(0, {"hostname": "play.example.org", "path": T.JOIN_PATH,
                                             "service": "https://localhost:8444"})
        T.apply("alex")
        self.assertEqual([r.get("hostname") for r in self.cf.config["ingress"]].count("play.example.org"), 1)

    def test_someone_elses_dns_record(self):
        self.cf.dns["r-a"] = {"id": "r-a", "type": "A", "name": "play.example.org", "content": "203.0.113.5", "proxied": False}
        with self.assertRaisesRegex(T.TunnelError, "didn't make"):
            T.apply("alex")
        self.assertEqual(self.cf.dns["r-a"]["content"], "203.0.113.5")

    def test_an_existing_record_for_the_tunnel_is_used_but_not_deleted(self):
        self.cf.dns["r-x"] = {"id": "r-x", "type": "CNAME", "name": "play.example.org",
                              "content": f"{TUNNEL}.cfargotunnel.com", "proxied": True}
        T.apply("alex")
        T.remove("alex")
        self.assertIn("r-x", self.cf.dns, "a record the panel didn't make stays")

    def test_remove_takes_away_only_the_panels_own(self):
        P.save(P.validate(dict(P.load(), join_proxies=["10.0.0.1"])))
        T.apply("alex")
        T.remove("alex")
        self.assertEqual(self.cf.config["ingress"], [{"hostname": "nas.example.org", "service": "http://10.0.0.9:5000"},
                                                     {"service": "http_status:404"}])
        self.assertEqual(list(self.cf.dns), ["r-nas"])
        s = P.load()
        self.assertEqual((s["join_url"], s["join_proxies"]), ("", ["10.0.0.1"]))
        self.assertIsNone(T.load()["applied"])

    def test_a_join_url_the_admin_typed_is_kept(self):
        P.save(P.validate(dict(P.load(), join_url="https://mc.example.com")))
        T.apply("alex")
        self.assertEqual(P.load()["join_url"], "https://mc.example.com")
        T.remove("alex")
        self.assertEqual(P.load()["join_url"], "https://mc.example.com")

    def test_join_pages_must_be_on(self):
        P.save(P.validate(dict(P.load(), join_enabled=False)))
        with self.assertRaisesRegex(T.TunnelError, "join pages on"):
            T.apply("alex")
        T.save_settings({"join_host": ""}, "alex")
        T.apply("alex")
        self.assertEqual([r.get("hostname") for r in self.cf.config["ingress"]][0], "panel.example.org")

    def test_check_from_outside(self):
        T.apply("alex")
        with mock.patch.object(T, "_outside", lambda url: (url.endswith("join.css"), "x")):
            out = T.check("alex")
        self.assertEqual([(n["name"], n["ok"]) for n in out["names"]],
                         [("play.example.org/join", True), ("panel.example.org", False)])
        self.assertEqual(out["tunnel"], {"status": "healthy", "connections": 4})

    def test_a_failed_sync_shows_in_needs_attention(self):
        T.apply("alex")
        self.cf.tokens.clear()
        P.save(P.validate(dict(P.load(), join_port=9001)))
        self.assertFalse(T.sync())
        self.assertFalse(T.load()["last"]["ok"])
        self.assertTrue(any("Cloudflare Tunnel" in t for t in T.attention()))


class Access(Base):
    def setUp(self):
        super().setUp()
        self.remote_service()
        self.configure(access_emails="me@example.org, Friend@Example.org")

    def test_access_in_front_of_the_panel_only(self):
        T.set_access(True, "alex")
        T.apply("alex")
        (app,) = self.cf.apps.values()
        self.assertEqual(app["domain"], "panel.example.org")
        self.assertEqual(app["policies"][0]["include"], [{"email": {"email": "me@example.org"}},
                                                         {"email": {"email": "friend@example.org"}}])
        T.set_access(False, "alex")
        T.apply("alex")
        self.assertEqual(self.cf.apps, {})

    def test_access_needs_addresses_and_its_own_join_name(self):
        T.save_settings({"access_emails": ""}, "alex")
        with self.assertRaisesRegex(T.TunnelError, "email"):
            T.set_access(True, "alex")
        T.save_settings({"access_emails": "me@example.org", "join_host": "panel"}, "alex")
        with self.assertRaisesRegex(T.TunnelError, "own name"):
            T.set_access(True, "alex")
        with self.assertRaisesRegex(T.TunnelError, "isn't an email"):
            T.save_settings({"access_emails": "nope"}, "alex")

    def test_a_token_without_access(self):
        self.cf.access_allowed = False
        T.set_access(True, "alex")
        with self.assertRaisesRegex(T.TunnelError, "can't change Cloudflare Access"):
            T.apply("alex")

    def test_remove_deletes_the_access_app(self):
        T.set_access(True, "alex")
        T.apply("alex")
        T.remove("alex")
        self.assertEqual(self.cf.apps, {})


class ConfigFile(unittest.TestCase):
    RULES = [{"hostname": "play.example.org", "path": T.JOIN_PATH, "service": "https://localhost:8444",
              "originRequest": {"noTLSVerify": True}}]
    FILE = ("tunnel: home\ncredentials-file: /root/.cloudflared/home.json\n\n"
            "ingress:\n  # my own\n  - hostname: nas.example.org\n    service: http://10.0.0.9:5000\n"
            "  - service: http_status:404\n")

    def test_the_block_goes_first_and_comes_out_again(self):
        new = T.edit_config(self.FILE, self.RULES)
        lines = new.split("\n")
        i = lines.index("ingress:")
        self.assertEqual(lines[i + 1], "  " + T.BEGIN)
        self.assertEqual(lines[i + 2], "  - hostname: play.example.org")
        self.assertIn("    path: '^/join(/.*)?$'", lines)
        self.assertIn("      noTLSVerify: true", lines)
        self.assertIn("  - hostname: nas.example.org", lines, "the admin's own route stays")
        again = T.edit_config(new, self.RULES)
        self.assertEqual(again, new, "applying again changes nothing")
        self.assertEqual(T.edit_config(new, []), self.FILE)

    def test_list_items_at_the_left_edge(self):
        text = "tunnel: home\ningress:\n- hostname: a.example.org\n  service: http://x:1\n- service: http_status:404\n"
        new = T.edit_config(text, self.RULES)
        self.assertIn("\n" + T.BEGIN + "\n- hostname: play.example.org\n", new)

    def test_no_ingress_list_yet(self):
        new = T.edit_config("tunnel: home\ncredentials-file: /x.json\n", self.RULES)
        self.assertTrue(new.endswith("  - service: http_status:404\n"))
        self.assertIn("ingress:\n  " + T.BEGIN, new)

    def test_files_left_alone(self):
        for text, why in (("tunnel: x\nurl: http://localhost:8000\n", "url:"),
                          ("ingress:\n\t- service: http_status:404\n", "tabs"),
                          ("ingress: [{service: http_status:404}]\n", "one line"),
                          ("ingress:\n  - service: a\ningress:\n  - service: b\n", "more than one"),
                          (f"ingress:\n  {T.BEGIN}\n  - service: a\n", "last")):
            with self.assertRaises(T.TunnelError, msg=why):
                T.edit_config(text, self.RULES)


class LocalTunnel(Base):
    def setUp(self):
        super().setUp()
        self.path = self.local_service(ConfigFile.FILE.replace("tunnel: home", f"tunnel: {TUNNEL}"))

    def read(self):
        with open(self.path) as f:
            return f.read()

    def test_apply_writes_checks_and_restarts(self):
        T.save_settings({"panel_host": "panel.example.org", "join_host": "play.example.org"}, "alex")
        out = T.apply("alex")
        text = self.read()
        self.assertIn("- hostname: play.example.org", text)
        self.assertIn("- hostname: panel.example.org", text)
        self.assertIn(("tunnel.restart", "cloudflared.service"), fake.calls)
        self.assertTrue(os.path.exists(self.path + ".redstonepanel-old"))
        self.assertEqual(out["commands"], [f"cloudflared tunnel route dns {TUNNEL} play.example.org",
                                           f"cloudflared tunnel route dns {TUNNEL} panel.example.org"],
                         "without a token the DNS names are commands to run")
        T.remove("alex")
        self.assertNotIn("play.example.org", self.read())

    def test_with_a_token_the_dns_names_are_made(self):
        self.configure()
        T.apply("alex")
        self.assertEqual(sorted(r["name"] for r in self.cf.dns.values() if r.get("comment") == T.DNS_COMMENT),
                         ["panel.example.org", "play.example.org"])
        self.assertFalse(any(c[1].endswith("/configurations") for c in self.cf.calls), "routes stay in the file")

    def test_a_file_cloudflared_refuses_is_never_used(self):
        before = self.read()
        fake.state.tunnel_validate = False
        T.save_settings({"join_host": "play.example.org"}, "alex")
        with self.assertRaisesRegex(T.TunnelError, "found a problem"):
            T.apply("alex")
        self.assertEqual(self.read(), before)
        self.assertNotIn(("tunnel.restart", "cloudflared.service"), fake.calls)

    def test_cloudflared_not_coming_back_puts_the_old_file_back(self):
        before = self.read()
        fake.state.tunnel_restart_ok = False
        T.save_settings({"join_host": "play.example.org"}, "alex")
        with self.assertRaisesRegex(T.TunnelError, "put the old file back"):
            T.apply("alex")
        self.assertEqual(self.read(), before)


class Visitors(Base):
    def test_the_visitors_own_address_only_from_the_local_cloudflared(self):
        h = {"cf-connecting-ip": "198.51.100.7"}
        self.assertEqual(T.visitor_ip("127.0.0.1", h), "127.0.0.1", "nothing applied yet")
        self.remote_service()
        self.configure()
        T.apply("alex")
        self.assertEqual(T.visitor_ip("127.0.0.1", h), "198.51.100.7")
        self.assertEqual(T.visitor_ip("::1", h), "198.51.100.7")
        self.assertEqual(T.visitor_ip("10.0.0.20", h), "10.0.0.20", "anyone else's header means nothing")
        self.assertEqual(T.visitor_ip("127.0.0.1", {"cf-connecting-ip": "nope"}), "127.0.0.1")

    def test_cloudflared_on_another_machine(self):
        self.configure(where="elsewhere", cloudflared_ip="10.0.0.2", origin_host="10.0.0.5", tunnel_id=TUNNEL)
        T.apply("alex")
        self.assertEqual(self.cf.config["ingress"][0]["service"], "https://10.0.0.5:8444")
        self.assertEqual(P.load()["join_proxies"], ["10.0.0.2"])
        h = {"cf-connecting-ip": "198.51.100.7"}
        self.assertEqual(T.visitor_ip("10.0.0.2", h), "198.51.100.7")
        self.assertEqual(T.visitor_ip("127.0.0.1", h), "127.0.0.1")
        with self.assertRaisesRegex(T.TunnelError, "address of the machine"):
            T.save_settings({"where": "elsewhere", "cloudflared_ip": ""}, "alex")

    def test_never_trusted_lan_through_the_tunnel(self):
        P.save(P.validate(dict(P.load(), login_mode="trusted_networks")))
        with mock.patch.object(P, "own_hosts", lambda: {"localhost", "10.0.0.5"}):
            self.assertIsNotNone(P.trusted_network_user("10.0.0.2", {"host": "10.0.0.5"}, "GET"))
            self.assertIsNone(P.trusted_network_user("10.0.0.2", {"host": "10.0.0.5", "cf-connecting-ip": "1.2.3.4"}, "GET"))


class Clusters(Base):
    def test_a_node_is_never_published(self):
        self.role = "node"
        with self.assertRaisesRegex(T.TunnelError, "only the master"):
            T.save_settings({"token": API_TOKEN}, "alex")
        with self.assertRaisesRegex(T.TunnelError, "only the master"):
            T.apply("alex")
        self.assertIsNone(T.node_warning())
        self.remote_service()
        T.detect(fresh=True)
        self.assertIn("only the master", T.node_warning())

    def test_the_master_and_standalone_panels_may(self):
        self.remote_service()
        for role in ("master", "standalone"):
            self.role = role
            self.configure()
            self.assertIsNone(T.node_warning())


class Discord(Base):
    def test_the_panel_name_becomes_discords_panel_address(self):
        self.remote_service()
        self.configure()
        T.apply("alex")
        self.assertEqual(D.load()["panel_url"], "https://panel.example.org")
        T.remove("alex")
        self.assertEqual(D.load()["panel_url"], "")

    def test_a_typed_address_is_kept(self):
        D._save(dict(D._defaults(), panel_url="https://mine.example.com"))
        self.remote_service()
        self.configure()
        T.apply("alex")
        self.assertEqual(D.load()["panel_url"], "https://mine.example.com")


class ThePlan(Base):
    def test_preview_says_what_apply_would_do(self):
        self.remote_service()
        pub = self.configure()
        self.assertEqual(pub["preview"]["changes"][:2], ["Route play.example.org/join/... → https://localhost:8444",
                                                          "Route panel.example.org → https://localhost:8443"])
        self.assertEqual(self.cf.config["ingress"][0]["hostname"], "nas.example.org", "nothing changed yet")


if __name__ == "__main__":
    unittest.main()
