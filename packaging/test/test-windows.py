"""The Windows install test: what test-install.sh is for the .deb.

Run on a Windows machine where RedstonePanel was just installed (the MSI, or
`python packaging\\windows\\setup.py install` from a staged build), from an elevated prompt:

    python packaging\\test\\test-windows.py [--port 8080] [--keep]

It signs in with the account the installer made, then does what a person would: installs the
Java a server needs, makes a real Java server, and checks that Windows has what the panel says
(a service running as its own account, a firewall rule, a scheduled task), that the console,
backups, restore, the file manager and crash handling work, and that removing the server
removes all of it. Downloads the server jar from Mojang and Java from Adoptium.
Exit status 0 when every check passed.
"""
import argparse
import http.server
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

PASSED, FAILED = [], []
HOOKS = []


def check(what, ok, detail=""):
    (PASSED if ok else FAILED).append(what)
    print(("  ok    " if ok else "  FAIL  ") + what + (f"   [{detail}]" if detail and not ok else ""), flush=True)
    return ok


def section(title):
    print(f"\n== {title}", flush=True)


class Panel:
    def __init__(self, base):
        self.base, self.cookie = base, None

    def context(self):
        """For https: one that takes the panel's own self-signed certificate."""
        if not self.base.startswith("https://"):
            return None
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def call(self, method, path, body=None, timeout=120, raw=False):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.cookie:
            req.add_header("Cookie", self.cookie)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=self.context()) as r:
                for h in r.headers.get_all("Set-Cookie") or []:
                    if h.startswith("rp_session="):
                        self.cookie = h.split(";", 1)[0]
                payload = r.read()
                return r.status, (payload if raw else _json(payload))
        except urllib.error.HTTPError as e:
            return e.code, _json(e.read())
        except (urllib.error.URLError, OSError) as e:
            return 0, {"detail": str(e)}

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.call("POST", path, body if body is not None else {}, **kw)

    def put(self, path, body=None, **kw):
        return self.call("PUT", path, body if body is not None else {}, **kw)

    def delete(self, path, **kw):
        return self.call("DELETE", path, **kw)


def _json(payload):
    try:
        return json.loads(payload.decode("utf-8", "replace"))
    except ValueError:
        return {"raw": payload[:500].decode("utf-8", "replace")}


def run(cmd, timeout=120):
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def ps(script, timeout=120):
    return run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], timeout)


def wait(what, fn, seconds, every=2):
    deadline = time.time() + seconds
    last = None
    while time.time() < deadline:
        try:
            last = fn()
        except Exception as e:                      # noqa: BLE001 (a test helper: keep polling)
            last = None
            print(f"        ({what}: {e})", flush=True)
        if last:
            return last
        time.sleep(every)
    return last


def service_state(name):
    code, out = run(["sc", "query", name])
    if code != 0:
        return None
    for line in out.splitlines():
        if "STATE" in line:
            return line.split()[-1]
    return "?"


NEIGHBOUR = "minecraft-java"      # the name the panel would give its first Java server
NEIGHBOUR_MADE = []


def service_command(name):
    code, out = run(["sc", "qc", name])
    for line in out.splitlines():
        if "BINARY_PATH_NAME" in line:
            return line.split(":", 1)[1].strip()
    return None


def neighbour_before():
    """A service called minecraft-java that the panel didn't make: the one the PC already has, or
    one made here. Returns what it runs, to compare at the end."""
    if service_state(NEIGHBOUR) is None:
        code, out = run(["sc", "create", NEIGHBOUR, "binPath=", r"C:\Windows\System32\cmd.exe /c rem someone else's server",
                         "start=", "demand", "DisplayName=", "Someone else's Minecraft server"])
        if code == 0:
            NEIGHBOUR_MADE.append(NEIGHBOUR)
    return service_command(NEIGHBOUR), service_state(NEIGHBOUR)


def neighbour_after(before):
    section("A service the panel didn't make")
    now = (service_command(NEIGHBOUR), service_state(NEIGHBOUR))
    check(f"the {NEIGHBOUR} service that was here before is exactly as it was", before[0] and now == before,
          f"before {before} now {now}")
    for name in NEIGHBOUR_MADE:
        run(["sc", "delete", name])
    del NEIGHBOUR_MADE[:]


def service_pid(name):
    code, out = run(["sc", "queryex", name])
    for line in out.splitlines():
        if line.strip().startswith("PID"):
            v = line.split(":", 1)[1].strip()
            return int(v) if v.isdigit() and v != "0" else None
    return None


def server_java_pids(name):
    """The java.exe processes the server's own service started: nothing else on the PC."""
    parent = service_pid(name)
    if not parent:
        return []
    code, out = ps(f"Get-CimInstance Win32_Process -Filter \"Name='java.exe' AND ParentProcessId={parent}\" | "
                   "Select-Object -ExpandProperty ProcessId")
    return [x for x in out.split() if x.isdigit()]


def _read_bytes(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def restore_notifications(p, args):
    """Puts back the webhook the PC's owner had before the test (or none)."""
    if getattr(args, "notifications_restored", False):
        return
    args.notifications_restored = True
    saved = getattr(args, "saved_notifications", None)
    p.put("/api/settings/notifications", {"clear": True})
    if saved is None:
        return
    with open(os.path.join(args.home, "data", "notifications.json"), "r+b") as f:   # in place: keeps its permissions
        f.write(saved)
        f.truncate()
    code, n = p.get("/api/settings/notifications")
    check("the owner's own webhook is back as it was", code == 200 and _read_bytes(
        os.path.join(args.home, "data", "notifications.json")) == saved, f"{code} {n}")


class Hook(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8", "replace")
        HOOKS.append(body)
        self.send_response(204)
        self.end_headers()

    def log_message(self, *a):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--url", help="the panel's address when it isn't http://127.0.0.1:PORT, e.g. https://127.0.0.1:8443 "
                                  "on a PC where HTTPS is on (its self-signed certificate is accepted)")
    ap.add_argument("--home", default=os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "RedstonePanel"))
    ap.add_argument("--keep", action="store_true", help="leave the test server in place")
    ap.add_argument("--loader", default="vanilla")
    ap.add_argument("--after-upgrade", action="store_true",
                    help="after installing again over a run with --keep: the account and the server are still there")
    args = ap.parse_args()
    home = args.home
    p = Panel(args.url or f"http://127.0.0.1:{args.port}")
    if args.after_upgrade:
        return after_upgrade(p, args)

    # ------------------------------------------------------------------ the install itself
    section("Install")
    check("the redstonepanel service is running", wait("service", lambda: service_state("redstonepanel") == "RUNNING", 60),
          str(service_state("redstonepanel")))
    code, out = run(["sc", "qc", "redstonepanel"])
    check("it starts with Windows", "AUTO_START" in out, out[-300:])
    check("the panel answers on its port", wait("health", lambda: p.get("/api/health", timeout=5)[0] == 200, 90))
    note = os.path.join(home, "first-sign-in.txt")
    check("the installer left first-sign-in.txt", os.path.isfile(note))
    password = ""
    if os.path.isfile(note):
        with open(note, encoding="utf-8") as f:
            for line in f:
                if line.startswith("Password:"):
                    password = line.split(":", 1)[1].strip()
    code, out = run(["icacls", home])
    check("the home folder is for administrators and the system only",
          "BUILTIN\\Administrators" in out and "Everyone" not in out and "(F)" in out
          and not any("Users:" in l and ("(M)" in l or "(F)" in l or "(W)" in l) for l in out.splitlines()), out[:600])
    code, out = run(["icacls", os.path.join(home, "data")])
    check("ordinary users can't read the panel's data", "BUILTIN\\Users" not in out, out[:400])
    password = first_sign_in(p, home, note, password)
    code, me = p.get("/api/auth/me")
    check("the API refuses a visitor who isn't signed in", code == 401, f"{code} {me}")
    code, who = p.post("/api/auth/login", {"username": "admin", "password": "wrong-password"})
    check("a wrong password is refused", code == 401, f"{code}")
    args.password = password
    code, who = p.post("/api/auth/login", {"username": "admin", "password": password})
    check("admin signs in with the password from first-sign-in.txt", code == 200 and p.cookie, f"{code} {who}")
    code, me = p.get("/api/auth/me")
    check("the session works", code == 200, f"{code} {me}")
    code, info = p.get("/api/panel-info")
    print(f"        panel-info: {json.dumps(info)[:300]}")
    check("the Start menu's link goes to the panel on this computer", open_link() == local_url(p.base), open_link())
    windows_accounts(p)
    # Where new servers go: <home>\servers, or the folder the owner chose in Panel Settings
    code, view = p.get("/api/settings/servers-folder")
    args.servers_dir = (view or {}).get("folder") or os.path.join(home, "servers")
    args.servers_setting = "" if (view or {}).get("is_default", True) else args.servers_dir
    print(f"        new servers go to {args.servers_dir}" + ("" if (view or {}).get("is_default", True) else " (chosen by the owner)"))
    # The owner's webhook, put back at the end (the test sets its own)
    args.saved_notifications = _read_bytes(os.path.join(home, "data", "notifications.json"))

    # ------------------------------------------------------------------ dashboard
    section("Dashboard")
    code, host = p.get("/api/host")
    check("the dashboard loads", code == 200, f"{code} {str(host)[:300]}")
    if code == 200:
        h = host.get("host", {})
        print(f"        os: {h.get('os')} | kernel: {h.get('kernel')} | cpu: {host.get('cpu')} | "
              f"memory: {host.get('memory')} | disks: {host.get('disks')} | net: {host.get('network')}")
        check("it names the system as Windows", "Windows" in str(h.get("os")), str(h.get("os")))
        check("it shows memory", (host.get("memory") or {}).get("total", 0) > 256 * 1024 * 1024, str(host.get("memory")))
        check("it shows the processor", bool((host.get("cpu") or {}).get("model")) and (host.get("cpu") or {}).get("cores"),
              str(host.get("cpu")))
        check("it shows a disk", bool(host.get("disks")) and host["disks"][0].get("total", 0) > 0, str(host.get("disks")))
        check("it shows an address", bool(h.get("ips")), str(h.get("ips")))
        fw = (host.get("services") or {}).get("firewall") or {}
        check("it sees Windows Defender Firewall on", fw.get("installed") and fw.get("active"), str(fw))
        time.sleep(4)
        code, host2 = p.get("/api/host")
        check("processor use is measured", code == 200 and (host2.get("cpu") or {}).get("percent") is not None,
              str((host2 or {}).get("cpu")))

    section("Port forwarding guide and the panel's firewall rule")
    code, pf = p.get("/api/port-forwarding/local")
    check("the guide knows this PC's router (default gateway)", code == 200 and bool((pf or {}).get("gateway"))
          and bool((pf or {}).get("lan_ip")), f"{code} {str(pf)[:300]}")
    web = int(((pf or {}).get("web") or {}).get("port") or 0)
    code, out = ps("Get-NetFirewallRule -Group 'RedstonePanel panel' | Get-NetFirewallPortFilter | "
                   "Select-Object -ExpandProperty LocalPort")
    check(f"the panel's firewall rule opens the port it answers on ({web})", str(web) in out.replace(",", " ").split(), out[-300:])

    # ------------------------------------------------------------------ Java and a server
    section("Java from Adoptium and a new server")
    code, rt = p.get("/api/java/runtimes")
    check("the Java page loads", code == 200, f"{code} {str(rt)[:300]}")
    print(f"        installable: {(rt or {}).get('installable')}  installed: {(rt or {}).get('installed')}")
    body = {"edition": "java", "loader": args.loader}
    args.neighbour = neighbour_before()
    print(f"        a service named {NEIGHBOUR} the panel didn't make: {args.neighbour}")
    code, dep = p.post("/api/servers/deploy", body, timeout=900)
    if code == 409 and isinstance(dep.get("java"), dict):
        major = dep["java"].get("major") or dep["java"].get("required")
        check(f"a new server asks for the Java it needs (Java {major})", bool(major), str(dep))
        code, job = p.post("/api/java/runtimes/install", {"major": major})
        check(f"installing Java {major} starts", code == 200, f"{code} {job}")

        def done():
            c, j = p.get("/api/java/runtimes/job")
            j = (j or {}).get("job") or {}
            return j if j.get("state") not in ("running", None) else None
        job = wait("java install", done, 600, every=3) or {}
        check(f"Java {major} was downloaded, checked and installed", job.get("state") in ("done", "ok", "finished", "success"),
              json.dumps(job)[-800:])
        java_exe = os.path.join(home, "java", f"java-{major}-openjdk-amd64", "bin", "java.exe")
        check("java.exe is in the panel's Java folder", os.path.isfile(java_exe), java_exe)
        code, dep = p.post("/api/servers/deploy", body, timeout=900)
    name = (dep or {}).get("name")
    check("the server is deployed", code == 200 and bool(name), f"{code} {dep}")
    check("... under a name no other Windows service has", name != NEIGHBOUR, str(name))
    if not name:
        return finish(p, args, None)
    d = os.path.join(args.servers_dir, name)
    unit = os.path.join(home, "services", f"{name}.service")
    check("its unit file is written (the same file as on Linux)", os.path.isfile(unit))
    if os.path.isfile(unit):
        print("        " + open(unit).read().replace("\n", "\n        "))
    if not check("Windows has a service for it", wait("svc", lambda: service_state(name) == "RUNNING", 60), str(service_state(name))):
        return finish(p, args, name)
    code, out = run(["sc", "qc", name])
    check("the service runs as the server's own account", f"NT SERVICE\\{name}".lower() in out.lower(), out[-400:])
    check("the service starts with Windows", "AUTO_START" in out, out[-400:])
    code, out = run(["icacls", d])
    check("that account can change the server's folder", f"NT SERVICE\\{name}".lower() in out.lower(), out[:500])
    code, out = run(["icacls", os.path.join(home, "data")])
    check("... and not the panel's data", "NT SERVICE" not in out, out[:300])

    def port_of():
        c, lst = p.get("/api/servers")
        for s in lst if isinstance(lst, list) else lst.get("servers", []):
            if s.get("name") == name:
                return s
        return None
    srv = wait("online", lambda: (port_of() or {}).get("status") == "online" and port_of(), 60) or port_of() or {}
    port = int(srv.get("port") or 25565)
    print(f"        {json.dumps(srv)[:300]}")
    check("the panel lists it as a Java server, online", srv.get("edition") == "Java" and srv.get("status") == "online", str(srv)[:300])
    check("the panel knows which Java it runs", bool(srv.get("java_installed")), str(srv.get("java_installed")))

    def listening():
        try:
            socket.create_connection(("127.0.0.1", port), 2).close()
            return True
        except OSError:
            return False
    if not check(f"Minecraft is listening on port {port}", wait("listen", listening, 300, every=5)):
        return finish(p, args, name, port)          # nothing below can pass without a running server
    journal = os.path.join(home, "journal", f"{name}.service", "output.log")
    check("the console output is kept",
          wait("journal", lambda: "Done (" in open(journal, encoding="utf-8", errors="replace").read(), 120), journal)
    code, out = ps(f"(Get-NetFirewallRule -DisplayName 'RedstonePanel {port}/tcp').Group")
    check("Defender Firewall has a rule for its port, in the RedstonePanel group", "RedstonePanel" in out, out[-300:])
    java_home = os.path.join(home, "java").replace("'", "''")
    code, out = ps(f"Get-CimInstance Win32_Process -Filter \"Name='java.exe'\" | Where-Object {{ $_.ExecutablePath -like '{java_home}\\*' }} | "
                   f"ForEach-Object {{ (Invoke-CimMethod -InputObject $_ -MethodName GetOwner).User }}")
    check("the panel's java.exe runs as that account, not as the system", name.lower() in out.lower() and "SYSTEM" not in out.upper().split(), out[-300:])

    # ------------------------------------------------------------------ console
    section("Console and commands")
    def say():
        c, r = p.post(f"/api/servers/{name}/chat", {"message": "/list"}, timeout=60)
        return r if r.get("status") == "success" else None
    first = p.post(f"/api/servers/{name}/chat", {"message": "/list"}, timeout=60)[1]
    print(f"        first answer: {str(first)[:200]}")
    if first.get("status") != "success":
        time.sleep(20)
        wait("listen again", listening, 300, every=5)
    r = wait("command", say, 240, every=10) or {}
    check("a console command gets the server's answer", "players online" in str(r.get("response", "")), str(r)[:300])
    code, logs = p.get("/api/logs")
    check("the activity log is served", code == 200, f"{code}")

    # ------------------------------------------------------------------ files
    section("File manager")
    code, lst = p.get(f"/api/servers/{name}/files")
    names = [e.get("name") for e in (lst or {}).get("entries", [])]
    check("the server's files are listed", code == 200 and "server.properties" in names, f"{code} {names[:20]}")
    code, w = p.put(f"/api/servers/{name}/files/text", {"path": "notes.txt", "content": "hello from the test\n"})
    check("a text file can be written", code == 200, f"{code} {w}")
    code, t = p.get(f"/api/servers/{name}/files/text?path=notes.txt")
    check("... and read back", code == 200 and "hello from the test" in str(t.get("content")), f"{code} {str(t)[:200]}")
    code, out = run(["icacls", os.path.join(d, "notes.txt")])
    check("the server's account can change a file the panel wrote", f"NT SERVICE\\{name}".lower() in out.lower(), out[:300])
    code, w = p.put(f"/api/servers/{name}/files/text", {"path": "CON.txt", "content": "x"})
    check("a device name (CON.txt) is refused", code >= 400, f"{code} {w}")
    code, w = p.put(f"/api/servers/{name}/files/text", {"path": "notes.txt:hidden", "content": "x"})
    check("an alternate data stream (name:stream) is refused", code >= 400, f"{code} {w}")
    run(["cmd", "/c", "mklink", "/J", os.path.join(d, "way-out"), os.environ.get("SystemRoot", r"C:\Windows")])
    code, lst = p.get(f"/api/servers/{name}/files")
    kinds = {e.get("name"): e.get("type") for e in (lst or {}).get("entries", [])}
    check("a junction is shown as a link", kinds.get("way-out") == "link", str(kinds.get("way-out")))
    code, t = p.get(f"/api/servers/{name}/files?path=way-out")
    check("... and the file manager won't go through it", code >= 400, f"{code} {str(t)[:200]}")
    code, t = p.get(f"/api/servers/{name}/files/text?path=way-out/win.ini")
    check("... nor read a file behind it", code >= 400, f"{code} {str(t)[:200]}")
    code, r = p.call("DELETE", f"/api/servers/{name}/files?path=way-out")
    check("deleting the junction removes the link only", code == 200 and not os.path.lexists(os.path.join(d, "way-out"))
          and os.path.isdir(os.environ.get("SystemRoot", r"C:\Windows")), f"{code} {r}")

    # ------------------------------------------------------------------ backups
    section("Backups, restore and schedules")
    p.put(f"/api/servers/{name}/files/text", {"path": "world/restore-test.txt", "content": "hello from the test\n"})
    code, b = p.post(f"/api/servers/{name}/backups", timeout=240)
    check("Back up now works while the server runs", code == 200 and b.get("file"), f"{code} {str(b)[:300]}")
    code, ov = p.get(f"/api/servers/{name}/backups")
    check("the backup is listed", code == 200 and len(ov.get("backups", [])) >= 1, f"{code} {str(ov)[:300]}")
    code, s = p.put(f"/api/servers/{name}/backups/schedule", {"enabled": True, "frequency": "daily", "time": "04:00", "keep": 3})
    check("a daily backup schedule is saved", code == 200, f"{code} {s}")
    task = f"\\RedstonePanel\\redstonepanel-backup@{name}"
    code, out = run(["schtasks", "/Query", "/TN", task, "/XML"])
    check("Task Scheduler has the task, run by the system", code == 0 and "S-1-5-18" in out, out[-400:])
    code, ov = p.get(f"/api/servers/{name}/backups")
    check("the panel shows when it runs next", bool(ov.get("next_run")), str(ov.get("next_run")))
    before = len(ov.get("backups", []))
    code, out = run(["schtasks", "/Run", "/TN", task])
    def scheduled():
        c, o = p.get(f"/api/servers/{name}/backups")
        return any("scheduled" in str(x.get("file") or x.get("name")) for x in o.get("backups", []))
    check("running the task makes a scheduled backup (the panel isn't involved)", wait("task", scheduled, 180, every=5),
          out[-200:])
    code, w = p.put(f"/api/servers/{name}/files/text", {"path": "world/restore-test.txt",
                                                           "content": "changed after the backup\n", "modified": None})
    first_backup = b.get("file") if isinstance(b, dict) else None
    code, r = p.post(f"/api/servers/{name}/backups/{first_backup}/restore", timeout=300)
    check("a backup restores", code == 200, f"{code} {str(r)[:300]}")
    code, t = p.get(f"/api/servers/{name}/files/text?path=world/restore-test.txt")
    check("... and the world is the one from the backup", "hello from the test" in str(t.get("content")), f"{code} {str(t)[:200]}")
    code, out = run(["icacls", os.path.join(d, "world")])
    check("... and the server's own account can change it", f"NT SERVICE\\{name}".lower() in out.lower(), out[-400:])
    check("the server is running again after the restore", wait("svc", lambda: service_state(name) == "RUNNING", 120)
          and wait("listen", listening, 300, every=5))
    code, s = p.put(f"/api/servers/{name}/restarts", {"enabled": True, "frequency": "weekly", "time": "05:00", "warn": 5})
    code2, out = run(["schtasks", "/Query", "/TN", f"\\RedstonePanel\\redstonepanel-restart@{name}", "/XML"])
    check("a weekly restart schedule becomes a task too", code == 200 and code2 == 0 and "Sunday" in out, f"{code} {s} {out[-300:]}")
    code, s = p.put(f"/api/servers/{name}/backups/schedule", {"enabled": False, "frequency": "daily", "time": "04:00", "keep": 3})
    code2, out = run(["schtasks", "/Query", "/TN", task])
    check("turning the backup schedule off removes its task", code == 200 and code2 != 0, f"{code} {out[-200:]}")

    # ------------------------------------------------------------------ crash, stop, start
    section("Crashes, notifications, stop and start")
    hook = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Hook)
    threading.Thread(target=hook.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{hook.server_address[1]}/hook"
    code, n = p.put("/api/settings/notifications", {"url": url, "events": ["crashed", "started", "stopped"]})
    check("a webhook is saved", code == 200, f"{code} {n}")
    code, out = run(["schtasks", "/Query", "/TN", "\\RedstonePanel\\notifications"])
    check("the notification task exists", code == 0, out[-300:])
    # Only this server's java.exe (a child of its service's process): the PC's other servers are left alone
    pids = server_java_pids(name)
    print(f"        killing java.exe {pids}")
    for pid in pids:
        run(["taskkill", "/F", "/PID", pid])
    check("a killed server is reported as crashed by webhook",
          wait("hook", lambda: any("crashed" in h for h in HOOKS), 90), str(HOOKS)[-400:])
    check("... and is started again by itself", wait("listen", listening, 400, every=5))
    code, pf = p.get(f"/api/servers/{name}/perf")
    check("the Performance card loads", code == 200 and isinstance(pf, dict) and pf.get("server") == name, f"{code} {str(pf)[:200]}")
    code, cr = p.get(f"/api/servers/{name}/crash")
    print(f"        crash card: {str(cr)[:400]}")
    code, out = ps("Get-WinEvent -FilterHashtable @{LogName='Application'; ProviderName='RedstonePanel'} -MaxEvents 5 | "
                   "ForEach-Object { \"$($_.Id) $($_.Message)\" }")
    check("the event log has the server's entries", name in out, out[-400:])
    HOOKS.clear()
    code, r = p.post(f"/api/servers/{name}/action", {"action": "stop"}, timeout=300)
    check("Stop works", code == 200 and wait("stopped", lambda: service_state(name) == "STOPPED", 150), f"{code} {r} {service_state(name)}")
    text = open(journal, encoding="utf-8", errors="replace").read()
    check("the server saved and stopped by its own `stop` command",
          "Stopping server" in text.split("[runner]")[-1] or "Stopping the server" in text[-3000:] or "All dimensions are saved" in text[-3000:],
          text[-600:])
    check("a stop that was asked for is reported as stopped, not crashed",
          wait("hook", lambda: any("stopped" in h for h in HOOKS), 60) and not any("crashed" in h for h in HOOKS), str(HOOKS)[-400:])
    srv = port_of() or {}
    check("the panel shows it offline", srv.get("status") == "offline", str(srv.get("status")))
    code, r = p.post(f"/api/servers/{name}/action", {"action": "start"}, timeout=120)
    check("Start works", code == 200 and wait("svc", lambda: service_state(name) == "RUNNING", 60), f"{code} {r}")
    check("Minecraft is listening again", wait("listen", listening, 300, every=5))
    code, r = p.post(f"/api/servers/{name}/action", {"action": "restart"}, timeout=300)
    check("Restart works", code == 200 and wait("listen", listening, 300, every=5), f"{code} {r}")
    restore_notifications(p, args)

    limits(p, args, name, listening)
    offsite_s3(p, args, name)
    join_and_terminal(p, name)
    bedrock(p, args)
    loaders(p, args)
    servers_folder(p, args, body)
    found_servers(p, args)
    sftp(p, args, name)
    windows_names(p)
    cluster_alone(p, args)

    # ------------------------------------------------------------------ the command line
    port = port_check(p, name, port) or port

    section("The redstonepanel command")
    code, out = run(["cmd", "/c", "redstonepanel", "status"])
    check("`redstonepanel status` runs from a prompt", code == 0 and "admin" in out, out[-400:])
    code, out = run(["cmd", "/c", "redstonepanel", "ports"], timeout=180)
    check("`redstonepanel ports` lists the server's port and who holds it",
          code == 0 and f"{port}/tcp" in out and name in out, out[-600:])

    return finish(p, args, name, port)


def port_check(p, name, port):
    """1.23.1: another program on the server's game port is named, the start is refused, and
    Move gives the server a free port. Returns the server's new port."""
    section("Port check")
    code, r = p.post(f"/api/servers/{name}/action", {"action": "stop"}, timeout=300)
    check("the server stops", code == 200 and wait("stopped", lambda: service_state(name) == "STOPPED", 150), f"{code} {r}")
    holder = subprocess.Popen([sys.executable, "-c", "import socket,time; s=socket.socket(); "
                               f"s.bind(('0.0.0.0', {port})); s.listen(); time.sleep(900)"])
    try:
        time.sleep(3)
        code, rep = p.get("/api/port-check", timeout=180)
        mine = [x for x in (rep or {}).get("problems", []) if x.get("server") == name and x.get("port") == port]
        check("the Port check finds another program on its game port", code == 200 and bool(mine), f"{code} {str(rep)[:500]}")
        h = (mine[0]["holders"] if mine and mine[0].get("holders") else [{}])[0]
        check("... and names it: python, its process id and path",
              h.get("pid") == holder.pid and "python" in str(h.get("program")).lower() and bool(h.get("exe")), str(h)[:400])
        check("... with Move offered", bool(mine) and mine[0].get("fix") == "move", str(mine)[:300])
        code, r = p.post(f"/api/servers/{name}/action", {"action": "start"}, timeout=180)
        check("a start from the panel is refused with the reason", code == 400 and "can't start" in str(r)
              and str(holder.pid) in str(r), f"{code} {str(r)[:400]}")
        check("... and the service wasn't started", service_state(name) == "STOPPED", str(service_state(name)))
        code, host = p.get("/api/host", timeout=180)
        check("Needs attention shows it", any(a.get("where") == "ports" and a.get("server") == name
                                              for a in (host or {}).get("attention", [])), str((host or {}).get("attention"))[:400])
        code, m = p.post(f"/api/servers/{name}/ports/move", {}, timeout=300)
        new = (m or {}).get("new")
        check("Move gives it the next free port", code == 200 and isinstance(new, int) and new != port, f"{code} {m}")
        if not new:
            return None
        code, out = ps(f"@(Get-NetFirewallRule -DisplayName 'RedstonePanel {new}/tcp' -ErrorAction SilentlyContinue).Count")
        check("Defender Firewall opens the new port", out.strip().endswith("1"), out[-200:])
        code, r = p.post(f"/api/servers/{name}/action", {"action": "start"}, timeout=180)
        def listening_new():
            try:
                socket.create_connection(("127.0.0.1", new), 2).close()
                return True
            except OSError:
                return False
        check("it starts on the new port", code == 200 and wait("listen", listening_new, 300, every=5), f"{code} {r}")
        return new
    finally:
        holder.kill()


def _usage(home, name):
    try:
        with open(os.path.join(home, "journal", f"{name}.service", "usage.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def limits(p, args, name, listening):
    """1.23: Maintenance > Memory and limits, as Job Object limits."""
    section("Memory and processor limits (Job Objects)")
    code, r = p.get(f"/api/servers/{name}/resources")
    check("Memory and limits loads", code == 200 and r.get("cores"), f"{code} {str(r)[:300]}")
    code, r = p.put(f"/api/servers/{name}/resources", {"memory": "auto", "cpu": 1})
    check("an automatic memory limit and a 1-core CPU limit are saved", code == 200, f"{code} {str(r)[:300]}")
    dropin = os.path.join(args.home, "services", f"{name}.service.d", "redstonepanel-limits.conf")
    text = open(dropin, encoding="utf-8").read() if os.path.isfile(dropin) else ""
    check("they are written in the same drop-in as on Linux", "MemoryMax=" in text and "CPUQuota=100%" in text, text)
    want = (r or {}).get("memory_max") or 0
    check("the running server's job has the limit at once (no restart)",
          wait("job limit", lambda: _usage(args.home, name).get("memory_max") == want and want, 30),
          f"want {want} have {_usage(args.home, name)}")
    code, r = p.post(f"/api/servers/{name}/action", {"action": "restart"}, timeout=300)
    check("after a restart the server runs inside its limits",
          code == 200 and wait("listen", listening, 300, every=5)
          and wait("job limit", lambda: _usage(args.home, name).get("memory_max") == want, 30),
          f"{code} {_usage(args.home, name)}")
    code, out = ps("$h = [Runtime.InteropServices.Marshal]; "
                   "Add-Type -Namespace RP -Name J -MemberDefinition '[DllImport(\"kernel32.dll\", SetLastError=true)] "
                   "public static extern IntPtr OpenJobObjectW(uint a, bool i, [MarshalAs(UnmanagedType.LPWStr)] string n);'; "
                   f"[RP.J]::OpenJobObjectW(4, $false, 'Global\\RedstonePanel.job.{name}')")
    check("an administrator can open the server's job by its name", out.strip() not in ("", "0"), out[-200:])
    code, pf = p.get(f"/api/servers/{name}/perf")
    check("the Performance card shows the limit", code == 200 and (pf.get("memory") or {}).get("limit") == want,
          f"{code} {str(pf)[:300]}")
    code, r = p.put(f"/api/servers/{name}/resources", {"memory": "off", "cpu": 0})
    check("turning the limits off works", code == 200 and not os.path.exists(dropin)
          and wait("job limit", lambda: not _usage(args.home, name).get("memory_max"), 30),
          f"{code} {_usage(args.home, name)}")


def bedrock(p, args):
    """1.23: a Bedrock server: the Windows build, its console, its firewall rules, a clean stop,
    automatic updates and Phantom."""
    section("A Bedrock server")
    code, dep = p.post("/api/servers/deploy", {"edition": "bedrock"}, timeout=900)
    name = (dep or {}).get("name")
    if not check("a Bedrock server is deployed", code == 200 and bool(name), f"{code} {dep}"):
        return
    d = os.path.join(args.servers_dir, name)
    check("it has Mojang's Windows program", os.path.isfile(os.path.join(d, "bedrock_server.exe")))
    check("its service runs as its own account",
          wait("svc", lambda: service_state(name) == "RUNNING", 60)
          and f"NT SERVICE\\{name}".lower() in run(["sc", "qc", name])[1].lower())
    log = os.path.join(d, "server.log")

    def started():
        return os.path.isfile(log) and "Server started." in open(log, encoding="utf-8", errors="replace").read()
    check("it starts, and its console is kept in server.log", wait("bedrock start", started, 180, every=3),
          open(log, encoding="utf-8", errors="replace").read()[-500:] if os.path.isfile(log) else "no server.log")

    def srv():
        c, lst = p.get("/api/servers")
        return next((s for s in (lst if isinstance(lst, list) else lst.get("servers", [])) if s.get("name") == name), {})
    s = wait("online", lambda: srv().get("status") == "online" and srv(), 60) or srv()
    port = int(s.get("port") or 19132)
    check("the panel lists it as a Bedrock server, online, with its version",
          s.get("edition") == "Bedrock" and s.get("status") == "online" and bool(s.get("version")), str(s)[:300])
    code, out = ps(f"@(Get-NetFirewallRule -Group RedstonePanel | Get-NetFirewallPortFilter | "
                   f"Where-Object {{ $_.LocalPort -eq '{port}' }}).Protocol -join ','")
    check(f"Defender Firewall has rules for port {port}", "TCP" in out.upper() or "UDP" in out.upper(), out[-200:])
    size = os.path.getsize(log)
    code, r = p.post(f"/api/servers/{name}/chat", {"message": "/list"}, timeout=60)
    check("a console command reaches the server",
          code == 200 and wait("list", lambda: "players online" in open(log, encoding="utf-8", errors="replace").read()[size:], 30),
          f"{code} {r}")
    code, b = p.post(f"/api/servers/{name}/backups", timeout=300)
    check("Back up now works while it runs (save hold / query / resume)", code == 200 and b.get("file"), f"{code} {str(b)[:300]}")
    code, u = p.get(f"/api/servers/{name}/updates")
    check("Maintenance shows its Bedrock version and the newest", code == 200 and (u or {}).get("current"),
          f"{code} {str(u)[:300]}")
    code, u = p.put(f"/api/servers/{name}/updates", {"enabled": True})
    check("automatic updates can be turned on", code == 200 and (u or {}).get("auto"), f"{code} {str(u)[:300]}")
    code, out = run(["schtasks", "/Query", "/TN", "\\RedstonePanel\\redstonepanel-updates"])
    check("the update check is a scheduled task", code == 0, out[-300:])

    section("Phantom for the Bedrock server")
    # Phantom relays RakNet only; a NetherNet server (the default) is refused, as on Linux
    code, ph = p.post(f"/api/servers/{name}/phantom/install", timeout=120)
    check("Phantom is refused for a NetherNet server", code == 400 and "NetherNet" in str(ph), f"{code} {str(ph)[:300]}")
    # ... and so is one on 19132, which already shows as a LAN game: Phantom is for other ports
    code, r = p.post(f"/api/servers/{name}/properties", {"properties": {"transport": "raknet", "server-port": "19140"}})
    check("the server switches to RakNet on port 19140", code == 200, f"{code} {str(r)[:300]}")
    code, r = p.post(f"/api/servers/{name}/action", {"action": "restart"}, timeout=300)
    check("it restarts on the new port", code == 200 and wait("running", lambda: service_state(name) == "RUNNING", 120),
          f"{code} {str(r)[:200]}")
    code, ph = p.post(f"/api/servers/{name}/phantom/install", timeout=300)
    check("Phantom installs (pinned Windows build, SHA-256 checked)", code == 200 and (ph or {}).get("phantom"),
          f"{code} {str(ph)[:400]}")
    unit = f"phantom-{name}"
    check("Phantom runs as its own service and account",
          wait("phantom", lambda: service_state(unit) == "RUNNING", 60)
          and f"NT SERVICE\\{unit}".lower() in run(["sc", "qc", unit])[1].lower(), run(["sc", "qc", unit])[1][-300:])
    code, ph = p.post(f"/api/servers/{name}/phantom-remove", {"unit": f"{unit}.service"}, timeout=120)
    check("Phantom is removed", code == 200 and wait("gone", lambda: service_state(unit) is None, 60), f"{code} {ph}")

    code, r = p.post(f"/api/servers/{name}/action", {"action": "stop"}, timeout=300)
    check("Stop saves the world: Bedrock quits cleanly",
          code == 200 and wait("stopped", lambda: service_state(name) == "STOPPED", 150)
          and "Quit correctly" in open(log, encoding="utf-8", errors="replace").read()[-2000:],
          open(log, encoding="utf-8", errors="replace").read()[-400:])
    if True:                                    # --keep keeps the main server only
        code, r = p.call("DELETE", f"/api/servers/{name}?keep_world=false", timeout=300)
        check("the Bedrock server is removed", code == 200 and wait("gone", lambda: service_state(name) is None, 60),
              f"{code} {r}")


S3_KEY, S3_SECRET = "rptestaccess", "rptest-secret-key-123"
WEED_URL = "https://github.com/seaweedfs/seaweedfs/releases/download/3.80/windows_amd64.zip"


def offsite_s3(p, args, name):
    """1.23: backup copies to S3-compatible storage from Windows. SeaweedFS's S3 server (it checks
    request signatures as real S3 does) runs next to the panel."""
    section("Off-site backup copies to S3 storage")
    import tempfile
    import zipfile
    top = tempfile.mkdtemp(prefix="rp-s3-")
    try:
        urllib.request.urlretrieve(WEED_URL, os.path.join(top, "weed.zip"))
        with zipfile.ZipFile(os.path.join(top, "weed.zip")) as z:
            z.extractall(top)
    except (OSError, zipfile.BadZipFile) as e:
        check("SeaweedFS (the S3 stand-in) downloads", False, str(e))
        return
    weed = next((os.path.join(r, f) for r, _d, fs in os.walk(top) for f in fs if f.lower() == "weed.exe"), None)
    conf = os.path.join(top, "s3.json")
    with open(conf, "w") as f:
        json.dump({"identities": [{"name": "rp", "credentials": [{"accessKey": S3_KEY, "secretKey": S3_SECRET}],
                                   "actions": ["Admin", "Read", "Write", "List", "Tagging"]}]}, f)
    os.makedirs(os.path.join(top, "data"))
    proc = subprocess.Popen([weed, "server", "-s3", f"-s3.config={conf}", "-dir=" + os.path.join(top, "data"),
                             "-ip=127.0.0.1", "-master.port=19333", "-volume.port=18080", "-filer.port=18888",
                             "-s3.port=8333"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        def up():
            try:
                urllib.request.urlopen("http://127.0.0.1:8333/", timeout=3)
            except urllib.error.HTTPError:
                return True
            except OSError:
                return False
            return True
        if not check("the S3 stand-in answers", wait("s3", up, 120, every=2)):
            return
        target = {"type": "s3", "name": "Bucket", "endpoint": "http://127.0.0.1:8333", "bucket": "rp-backups",
                  "prefix": "mc", "region": "us-east-1", "access_key": S3_KEY, "secret_key": S3_SECRET}
        app = os.path.join(r"C:\Program Files\RedstonePanel", "app")
        py = os.path.join(r"C:\Program Files\RedstonePanel", "python", "python.exe")
        code, out = run([py, "-X", "utf8", "-c",
                         "import sys; sys.path.insert(0, sys.argv[1]); from core import backup_targets as T; "
                         "T.S3Target({'endpoint': 'http://127.0.0.1:8333', 'bucket': 'rp-backups', 'prefix': '', "
                         f"'region': 'us-east-1', 'access_key': '{S3_KEY}', 'secret_key': '{S3_SECRET}'}}).request('PUT', '')",
                         app])
        check("the bucket is made with the panel's own S3 client", code == 0, out[-400:])
        code, st = p.post("/api/settings/backup-storage", target)
        tid = next((t["id"] for t in (st or {}).get("targets", []) if t.get("name") == "Bucket"), None)
        check("S3 storage is added", code == 200 and tid, f"{code} {str(st)[:300]}")
        check("the secret is never sent back", S3_SECRET not in json.dumps(st))
        code, t = p.post("/api/settings/backup-storage/test", {"id": tid})
        check("Test writes, lists and deletes", code == 200 and (t or {}).get("ok"), f"{code} {t}")
        code, r = p.put(f"/api/servers/{name}/offsite/{tid}", {"enabled": True})
        check("the server's backups are copied there", code == 200, f"{code} {r}")
        code, b = p.post(f"/api/servers/{name}/backups", timeout=300)
        newest = (b or {}).get("file")

        def copied():
            c, v = p.get(f"/api/servers/{name}/offsite")
            return any(c2.get("file") == newest for t2 in (v or {}).get("targets", []) if t2.get("id") == tid
                       for c2 in t2.get("copies", []))
        check("Back up now is copied to S3, checked", newest and wait("copy", copied, 240, every=4), str(newest))
        os.remove(os.path.join(args.home, "backups", name, newest))
        code, f = p.post(f"/api/servers/{name}/offsite/{tid}/fetch", {"file": newest}, timeout=300)
        check("... and comes back from S3", code == 200 and (f or {}).get("file") == newest
              and os.path.isfile(os.path.join(args.home, "backups", name, newest)), f"{code} {f}")
        p.put(f"/api/servers/{name}/offsite/{tid}", {"enabled": False})
        p.call("DELETE", f"/api/settings/backup-storage/{tid}")
    finally:
        proc.kill()


def _deploy(p, body):
    """Deploys, installing the Java the server asks for first when it asks."""
    code, dep = p.post("/api/servers/deploy", body, timeout=1800)
    if code == 409 and isinstance((dep or {}).get("java"), dict):
        major = dep["java"].get("major") or dep["java"].get("required")
        p.post("/api/java/runtimes/install", {"major": major})

        def done():
            c, j = p.get("/api/java/runtimes/job")
            j = (j or {}).get("job") or {}
            return j if j.get("state") not in ("running", None) else None
        wait("java install", done, 600, every=3)
        code, dep = p.post("/api/servers/deploy", body, timeout=1800)
    return code, dep


def loaders(p, args):
    """1.23: Forge's installer runs as the new server's own account, through its service."""
    section("A Forge server (its installer as the server's account)")
    code, dep = _deploy(p, {"edition": "java", "loader": "forge"})
    name = (dep or {}).get("name")
    if not check("a Forge server is deployed", code == 200 and bool(name), f"{code} {str(dep)[:600]}"):
        return
    d = os.path.join(args.servers_dir, name)
    code, out = run(["cmd", "/c", "dir", "/q", os.path.join(d, "libraries")])
    check("the installer's files belong to the server's own account", name.lower() in out.lower(), out[-600:])
    cmd = service_command(name) or ""
    check("its service runs the server again, not the installer", " service " in cmd and "--task" not in cmd, cmd)
    unit = open(os.path.join(args.home, "services", f"{name}.service"), encoding="utf-8").read()
    check("it starts from Forge's Windows arguments file", "win_args.txt" in unit, unit[-400:])
    journal = os.path.join(args.home, "journal", f"{name}.service", "output.log")
    check("Forge starts (Done)", wait("forge", lambda: os.path.isfile(journal) and "Done (" in
                                      open(journal, encoding="utf-8", errors="replace").read(), 600, every=5),
          open(journal, encoding="utf-8", errors="replace").read()[-800:] if os.path.isfile(journal) else "no output")
    if True:                                    # --keep keeps the main server only
        code, r = p.call("DELETE", f"/api/servers/{name}?keep_world=false", timeout=300)
        check("the Forge server is removed", code == 200 and wait("gone", lambda: service_state(name) is None, 60),
              f"{code} {r}")


def read_password(home):
    try:
        with open(os.path.join(home, "first-sign-in.txt"), encoding="utf-8") as f:
            for line in f:
                if line.startswith("Password:"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return ""


def after_upgrade(p, args):
    section("After installing again")
    check("the redstonepanel service is running", wait("service", lambda: service_state("redstonepanel") == "RUNNING", 90),
          str(service_state("redstonepanel")))
    check("the panel answers", wait("health", lambda: p.get("/api/health", timeout=5)[0] == 200, 90))
    code, who = p.post("/api/auth/login", {"username": "admin", "password": read_password(args.home)})
    check("the same admin account signs in (no new one was made)", code == 200 and p.cookie, f"{code} {who}")
    code, lst = p.get("/api/servers")
    servers = lst if isinstance(lst, list) else (lst or {}).get("servers", [])
    check("the server from before is still listed", len(servers) == 1, str(servers)[:300])
    if not servers:
        return finish(p, args, None)
    name, port = servers[0]["name"], int(servers[0].get("port") or 25565)
    check("its service was started again", wait("svc", lambda: service_state(name) == "RUNNING", 120), str(service_state(name)))
    code, out = run(["sc", "qc", name])
    check("it still starts with Windows", "AUTO_START" in out, out[-300:])

    def listening():
        try:
            socket.create_connection(("127.0.0.1", port), 2).close()
            return True
        except OSError:
            return False
    check(f"Minecraft is listening on port {port} again", wait("listen", listening, 300, every=5))
    code, ov = p.get(f"/api/servers/{name}/backups")
    check("its backups are still there", code == 200 and len(ov.get("backups", [])) >= 1, f"{code} {str(ov)[:200]}")
    args.keep = False
    return finish(p, args, name, port)


def join_and_terminal(p, name):
    """1.21.5 join pages work on Windows; 1.21.10's terminal is hidden there."""
    section("Join pages and the terminal")
    code, r = p.put("/api/settings/join", {"enabled": True, "port": 8444})
    listener = (r or {}).get("listener") or {}
    check("the join pages' port is turned on", code == 200 and listener.get("running"), f"{code} {str(r)[:300]}")
    code, r = p.put("/api/join-pages/page", {"server": name, "enabled": True})
    path = (r or {}).get("path") or ""
    check("the server's join page is turned on", code == 200 and bool(path), f"{code} {str(r)[:200]}")
    try:
        scheme = "https" if p.base.startswith("https:") else "http"     # join pages follow the panel's HTTPS
        with urllib.request.urlopen(f"{scheme}://127.0.0.1:8444/join/{urllib.parse.quote(path)}", timeout=20,
                                    context=p.context()) as resp:
            page_code, page = resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        page_code, page = e.code, ""
    except (urllib.error.URLError, OSError) as e:
        page_code, page = 0, str(e)
    check("its public page answers on port 8444", page_code == 200 and "<html" in page.lower(), f"{page_code} {page[:200]}")
    code, out = ps("Get-NetFirewallRule -Group RedstonePanel | Get-NetFirewallPortFilter | Select-Object -ExpandProperty LocalPort")
    check("... and the firewall lets players reach it", "8444" in out.split(), out[-300:])
    p.put("/api/join-pages/page", {"server": name, "enabled": False})
    code, r = p.put("/api/settings/join", {"enabled": False, "port": 8444})
    check("the join pages' port is turned off again", code == 200, f"{code} {str(r)[:200]}")
    code, t = p.get("/api/terminal")
    check("the terminal says it isn't on Windows yet", code == 200 and (t or {}).get("supported") is False, f"{code} {str(t)[:200]}")


def servers_folder(p, args, body):
    """Panel Settings > Servers folder: new servers go to a folder of the owner's choosing."""
    section("Servers folder")
    home = args.home
    code, view = p.get("/api/settings/servers-folder")
    usual = os.path.join(home, "servers")
    if args.servers_setting:
        print(f"        this PC's owner chose {args.servers_setting}; it is put back at the end")
        check("the panel shows the folder the owner chose", code == 200 and not view.get("is_default")
              and os.path.normcase(view.get("folder", "")) == os.path.normcase(args.servers_setting), f"{code} {view}")
        usual = args.servers_setting
    else:
        check("the usual place is the servers folder in the panel's home", code == 200 and view.get("is_default")
              and os.path.normcase(view.get("folder", "")) == os.path.normcase(usual), f"{code} {view}")
    drive = os.path.splitdrive(os.path.abspath(home))[0]
    drives = {v.get("drive"): v for v in view.get("drives") or []}
    check("the drives to choose from include the panel's own, which suggests the usual place",
          drive.upper() in drives and drives[drive.upper()].get("suggested") == "" and drives[drive.upper()].get("total", 0) > 0, str(view.get("drives")))
    check("every other drive suggests \\Minecraft\\servers on it",
          all(v.get("suggested") == f"{k}\\Minecraft\\servers" for k, v in drives.items() if k != drive.upper()), str(view.get("drives")))
    for bad, why in ((drive + "\\rp test\\servers", "a space"), (drive, "a whole drive"), ("servers", "no drive letter"),
                     ("\\\\nas\\share\\servers", "a network share"),
                     (os.path.join(os.environ.get("SystemRoot", "C:\\Windows"), "rp-servers"), "inside Windows"),
                     (os.path.join(home, "data", "servers"), "inside the panel's data"), ("Q:\\servers", "a drive that isn't there")):
        code, r = p.put("/api/settings/servers-folder", {"folder": bad})
        check(f"a folder with {why} is refused, with a reason", code == 400 and bool(r.get("detail")), f"{bad}: {code} {r}")
    top = drive + "\\rp-test-disk2"
    other = top + "\\Minecraft"
    code, view = p.put("/api/settings/servers-folder", {"folder": other})
    if not check("another folder is accepted and made", code == 200 and os.path.isdir(other)
                 and os.path.normcase(view.get("folder", "")) == os.path.normcase(other), f"{code} {view}"):
        return
    code, out = run(["icacls", other])
    check("the new folder is left to administrators and the system", "BUILTIN\\Administrators" in out and "Everyone" not in out
          and "Authenticated Users" not in out, out[:500])
    name = None
    try:
        code, dep = p.post("/api/servers/deploy", body, timeout=900)
        name = (dep or {}).get("name")
        d = os.path.join(other, name or "?")
        check("a new server is made in that folder", code == 200 and os.path.isfile(os.path.join(d, "server.jar")), f"{code} {dep}")
        check("... and not in the usual place", bool(name) and not os.path.exists(os.path.join(usual, name)))
        if not name:
            return
        check("its service runs", wait("svc", lambda: service_state(name) == "RUNNING", 60), str(service_state(name)))
        code, out = run(["icacls", d])
        check("its own account can change its folder", f"NT SERVICE\\{name}".lower() in out.lower(), out[:500])
        log = os.path.join(d, "logs", "latest.log")
        check("the server writes there (logs\\latest.log)", wait("log", lambda: os.path.isfile(log), 180, every=3), log)
        code, lst = p.get("/api/servers")
        names = [s.get("name") for s in (lst if isinstance(lst, list) else lst.get("servers", []))]
        check("the panel lists servers from both folders", name in names and len(names) >= 2, str(names))
        code, view = p.put("/api/settings/servers-folder", {"folder": args.servers_setting})
        check("back to the folder before; the other folder is still looked in",
              code == 200 and bool(view.get("is_default")) == (not args.servers_setting)
              and any(os.path.normcase(f.get("folder", "")) == os.path.normcase(other) for f in view.get("folders", [])), f"{code} {view}")
        code, lst = p.get("/api/servers")
        names = [s.get("name") for s in (lst if isinstance(lst, list) else lst.get("servers", []))]
        check("... so its server is still listed", name in names, str(names))
    finally:
        if name:
            code, r = p.call("DELETE", f"/api/servers/{name}?keep_world=false", timeout=300)
            if code >= 400:
                code, r = p.call("DELETE", f"/api/servers/{name}", {"keep_world": False}, timeout=300)
            check("removing it deletes its folder there", code == 200 and wait("gone", lambda: not os.path.exists(os.path.join(other, name)), 60),
                  f"{code} {r}")
            check("... and its Windows service", wait("gone", lambda: service_state(name) is None, 60), str(service_state(name)))
        p.put("/api/settings/servers-folder", {"folder": args.servers_setting})
        if not args.keep:
            run(["cmd", "/c", "rmdir", "/s", "/q", top])


STAND_IN = r"""
import java.io.*; import java.net.*; import java.nio.file.*; import java.util.*;
public class Main {
    public static void main(String[] a) throws Exception {
        Properties p = new Properties();
        try (InputStream in = new FileInputStream("server.properties")) { p.load(in); }
        int port = Integer.parseInt(p.getProperty("server-port", "25565"));
        Files.createDirectories(Paths.get("logs"));
        Files.write(Paths.get("logs/latest.log"), ("Done (0.1s)! on " + port + "\n").getBytes());
        try (ServerSocket s = new ServerSocket(port)) { while (true) { s.accept().close(); } }
    }
}
"""


def _jdk():
    """(java, javac, jar) from a JDK on this machine, or None."""
    import glob
    import shutil
    homes = [os.environ.get(k) for k in sorted(os.environ, reverse=True) if k.startswith("JAVA_HOME")]
    found = shutil.which("javac")
    if found:
        homes.insert(0, os.path.dirname(os.path.dirname(found)))
    homes += glob.glob(r"C:\Program Files\Eclipse Adoptium\jdk-*") + glob.glob(r"C:\Program Files\Java\jdk-*")
    for h in homes:
        tools = [os.path.join(h or "", "bin", t + ".exe") for t in ("java", "javac", "jar")]
        if h and all(os.path.isfile(t) for t in tools):
            return tools
    return None


def found_servers(p, args):
    """Found on this computer: a server somebody set up outside the panel is found, handed
    over where it is, and handed back. (Started from a prompt here; one run by somebody's own
    Windows service takes the same road once its service is stopped.)"""
    section("Found servers")
    jdk = _jdk()
    if not jdk:
        print("  SKIP  no JDK on this machine to build a stand-in server with", flush=True)
        return
    java, javac, jar_tool = jdk
    drive = os.path.splitdrive(os.path.abspath(args.home))[0]
    top = drive + "\\rp-test-found"
    d, port, jar = top + "\\survival", 25655, "paper-1.21.4.jar"
    run(["cmd", "/c", "rmdir", "/s", "/q", top])
    os.makedirs(os.path.join(d, "world"))
    build = os.path.join(top, "build")
    os.makedirs(build)
    with open(os.path.join(build, "Main.java"), "w") as f:
        f.write(STAND_IN)
    with open(os.path.join(build, "m.txt"), "w") as f:
        f.write("Main-Class: Main\n")
    code, out = subprocess.run([javac, "--release", "8", "Main.java"], cwd=build, capture_output=True, text=True).returncode, ""
    if code != 0:
        r = subprocess.run([javac, "Main.java"], cwd=build, capture_output=True, text=True)
        code, out = r.returncode, r.stdout + r.stderr
    r = subprocess.run([jar_tool, "cfm", os.path.join(d, jar), "m.txt", "Main.class"], cwd=build, capture_output=True, text=True)
    if not check("a stand-in server is built", code == 0 and r.returncode == 0 and os.path.isfile(os.path.join(d, jar)), out + r.stderr):
        return
    with open(os.path.join(d, "server.properties"), "w") as f:
        f.write(f"server-port={port}\nlevel-name=world\n")
    with open(os.path.join(d, "eula.txt"), "w") as f:
        f.write("eula=true\n")
    with open(os.path.join(d, "world", "level.dat"), "w") as f:
        f.write("keep")

    def listening():
        try:
            socket.create_connection(("127.0.0.1", port), 2).close()
            return True
        except OSError:
            return False

    def entry():
        code, view = p.get("/api/found-servers")
        mine = [f for f in (view or {}).get("servers", []) if os.path.normcase(f.get("folder", "")) == os.path.normcase(d)]
        return (mine[0] if mine else None), view

    proc = subprocess.Popen([java, "-Xmx200M", "-jar", jar, "nogui"], cwd=d, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=0x08000000)
    name = None
    try:
        check("it runs, started from a prompt", wait("port", listening, 60), "nothing on its port")
        e, view = entry()
        check("the panel finds it, with its folder, port, memory and jar", bool(e) and e.get("port") == port and e.get("edition") == "Java"
              and e.get("heap") == "200M" and e.get("jar") == jar and e.get("running") is True and e.get("service") == "", str(view))
        code, lst = p.get("/api/servers")
        names = [s.get("name") for s in (lst if isinstance(lst, list) else lst.get("servers", []))]
        code, r = p.post("/api/found-servers/manage", {"folder": d})
        check("while it runs, Manage asks its owner to stop it", code == 400 and "Stop it where you started it" in str(r.get("detail")), f"{code} {r}")
        check("... and leaves it running", listening() and proc.poll() is None)
        code, r = p.post("/api/found-servers/manage", {"folder": top + "\\nowhere"})
        check("a folder that isn't a found server is refused", code == 400, f"{code} {r}")
        proc.terminate()
        proc.wait(30)
        e, view = entry()
        check("once stopped it stays listed, as stopped", bool(e) and e.get("running") is False, str(view))
        code, r = p.post("/api/found-servers/manage", {"folder": d}, timeout=300)
        name = (r or {}).get("name")
        if not check("Manage with RedstonePanel works", code == 200 and bool(name), f"{code} {r}"):
            print("  diag  folder: " + " ".join(run(["icacls", d])[1].split()), flush=True)
            print("  diag  jar: " + " ".join(run(["icacls", os.path.join(d, jar)])[1].split()) + " java=" + java, flush=True)
            print("  diag  files: " + str(os.listdir(d)) + " temp=" + os.environ.get("TEMP", ""), flush=True)
            return
        check("the panel's service runs it from the same folder", wait("svc", lambda: service_state(name) == "RUNNING", 60)
              and wait("port", listening, 90), str(service_state(name)))
        check("the jar has the name the panel uses; nothing was copied", os.path.isfile(os.path.join(d, "server.jar"))
              and not os.path.exists(os.path.join(d, jar)) and not os.path.exists(os.path.join(args.servers_dir, name)))
        code, out = run(["icacls", d])
        check("the server's own account can change the folder", f"NT SERVICE\\{name}".lower() in out.lower(), out[:500])
        code, lst = p.get("/api/servers")
        now = {s.get("name"): s for s in (lst if isinstance(lst, list) else lst.get("servers", []))}
        check("it is on the panel's list with its port", name in now and name not in names and str(now[name].get("port")) == str(port), str(now.get(name)))
        e, view = entry()
        check("it is no longer among the found ones, and listed as taken over", e is None
              and [t.get("name") for t in view.get("taken_over", [])] == [name], str(view))
        code, r = p.post(f"/api/found-servers/{name}/undo", timeout=300)
        check("Undo works", code == 200, f"{code} {r}")
        check("the panel's service is gone and nothing runs in its place", wait("gone", lambda: service_state(name) is None, 60)
              and wait("closed", lambda: not listening(), 30), str(service_state(name)))
        check("the jar has its own name again and the world is untouched", os.path.isfile(os.path.join(d, jar))
              and not os.path.exists(os.path.join(d, "server.jar")) and open(os.path.join(d, "world", "level.dat")).read() == "keep")
        code, lst = p.get("/api/servers")
        check("it left the panel's list", name not in [s.get("name") for s in (lst if isinstance(lst, list) else lst.get("servers", []))])
        name = None
    finally:
        if proc.poll() is None:
            proc.kill()
        if name:
            p.post(f"/api/found-servers/{name}/undo", timeout=300)
        if not args.keep:
            run(["cmd", "/c", "rmdir", "/s", "/q", top])


def windows_accounts(p):
    """1.23: Windows accounts in Administrators or a RedstonePanel group sign in, as Linux ones do."""
    section("Windows accounts")
    pw = "Rp-" + os.urandom(4).hex() + "!a"           # 13 characters: `net user` asks a question above 14
    run(["net", "user", "rptester", "/delete"])
    run(["net", "user", "rpother", "/delete"])
    run(["net", "localgroup", "RedstonePanel", "/delete"])
    code, out = run(["net", "user", "rptester", pw, "/add"])
    code2, out2 = run(["net", "user", "rpother", pw, "/add"])
    check("two Windows test accounts", code == 0 and code2 == 0, (out + out2)[-300:])
    q = Panel(p.base)
    code, who = q.post("/api/auth/login", {"username": "rptester", "password": pw})
    check("a Windows account outside both groups is refused", code == 401, f"{code} {who}")
    run(["net", "localgroup", "RedstonePanel", "/add"])
    code, out = run(["net", "localgroup", "RedstonePanel", "rptester", "/add"])
    check("the RedstonePanel group", code == 0, out[-300:])
    code, who = q.post("/api/auth/login", {"username": "RPTester", "password": "wrong-" + pw})
    check("a wrong Windows password is refused", code == 401, f"{code} {who}")
    code, who = q.post("/api/auth/login", {"username": "RPTester", "password": pw})
    code, me = q.get("/api/auth/me")
    if not check("a member of the RedstonePanel group signs in as an admin, under one spelling",
                 code == 200 and me.get("source") == "linux" and me.get("role") == "admin"
                 and me.get("username") == "rptester", f"{code} {str(me)[:300]}"):
        app = os.path.join(r"C:\Program Files\RedstonePanel", "app")
        py = os.path.join(r"C:\Program Files\RedstonePanel", "python", "python.exe")
        _c, out = run([py, "-X", "utf8", "-c",
                       "import sys; sys.path.insert(0, sys.argv[1]); from core.platform.windows import system as w; "
                       "n, pw = sys.argv[2], sys.argv[3]; print('lookup', w._lookup(n)); print('canonical', w.canonical_account(n)); "
                       "print('groups', w.local_groups(n)); print('allowed', w.allowed_groups(('sudo', 'redstonepanel'))); "
                       "print('password', w.check_system_password(n, pw)); import ctypes; print('err', ctypes.GetLastError())",
                       app, "RPTester", pw])
        print("   diag " + out.replace("\n", " | ")[-1200:])
    r = Panel(p.base)
    code, who = r.post("/api/auth/login", {"username": "rpother", "password": pw})
    check("an account in neither group still can't", code == 401, f"{code} {who}")
    run(["net", "localgroup", "Administrators", "rpother", "/add"])
    code, who = r.post("/api/auth/login", {"username": f".\\rpother", "password": pw})
    check("a member of Administrators signs in", code == 200 and r.cookie, f"{code} {who}")
    run(["net", "localgroup", "RedstonePanel", "rptester", "/delete"])
    time.sleep(31)                                          # the panel keeps group answers for 30 seconds
    code, me = q.get("/api/auth/me")
    check("leaving the group ends the session", code == 401, f"{code} {str(me)[:200]}")
    run(["net", "user", "rptester", "/delete"])
    run(["net", "user", "rpother", "/delete"])
    run(["net", "localgroup", "RedstonePanel", "/delete"])


PROGRAM = r"C:\Program Files\RedstonePanel"


def panel_python(code, *argv, timeout=180):
    """Runs Python code with the panel's own Python and its libraries (paramiko, cryptography)."""
    py = os.path.join(PROGRAM, "python", "python.exe")
    return run([py, "-X", "utf8", "-c", "import sys; sys.path.insert(0, sys.argv[1]); " + code,
                os.path.join(PROGRAM, "app"), *argv], timeout)


SFTP_CLIENT = """
import paramiko, io, stat as S
host, port, user, pw = sys.argv[2], int(sys.argv[3]), sys.argv[4], sys.argv[5]
t = paramiko.Transport((host, port)); t.connect(username=user, password=pw)
c = paramiko.SFTPClient.from_transport(t)
print('LIST', sorted(c.listdir('/')))
c.putfo(io.BytesIO(b'hello from sftp' * 1000), '/sftp-test.txt')
c.posix_rename('/sftp-test.txt', '/sftp-moved.txt')
print('SIZE', c.stat('/sftp-moved.txt').st_size)
with c.open('/sftp-moved.txt', 'rb') as f:
    f.seek(5); print('READ', f.read(10))
c.mkdir('/sftp-dir')
for bad in ('/aux.txt', '/x:y'):
    try:
        c.putfo(io.BytesIO(b'x'), bad); print('MADE', bad)
    except IOError as e:
        print('REFUSED', bad)
try:
    print('UP', 'server.properties' in c.listdir('/../..'))
except IOError:
    print('UP refused')
c.rmdir('/sftp-dir')
c.close(); t.close()
print('DONE')
"""


def sftp(p, args, name):
    """1.24: SFTP on Windows, as the service redstonepanel-sftp run as the system account."""
    section("SFTP")
    if not name:
        return
    port = 2022
    code, r = p.put("/api/settings/sftp", {"enabled": True, "port": port})
    check("SFTP turns on", code == 200, f"{code} {r}")
    check("its Windows service runs", wait("sftp", lambda: service_state("redstonepanel-sftp") == "RUNNING", 60),
          str(service_state("redstonepanel-sftp")))
    code, out = run(["sc", "qc", "redstonepanel-sftp"])
    check("as the system account, like the panel", "LocalSystem" in out, out[-300:])
    code, out = ps(f"@(Get-NetFirewallRule -DisplayName 'RedstonePanel {port}/tcp' -ErrorAction SilentlyContinue).Count")
    check("its port has a firewall rule", out.strip().endswith("1"), out[-200:])
    wait("listening", lambda: socket.create_connection(("127.0.0.1", port), 3).close() or True, 30)
    code, out = panel_python(SFTP_CLIENT, "127.0.0.1", str(port), f"admin.{name}", args.password)
    print("        " + out.strip().replace("\n", " | ")[-900:])
    check("an admin signs in with ACCOUNT.SERVER and the panel password, and sees the server's folder",
          code == 0 and "LIST" in out and "server.properties" in out, out[-500:])
    check("uploads, renames and reads at an offset", "SIZE 15000" in out and "READ b' from sftp'" in out, out[-500:])
    check("names Windows can't hold are refused", "REFUSED /aux.txt" in out and "REFUSED /x:y" in out, out[-500:])
    check("the session can't leave the server's folder ('/../..' is its own folder)", "UP True" in out, out[-300:])
    folder = os.path.join(args.servers_dir, name)
    moved = os.path.join(folder, "sftp-moved.txt")
    check("the file is in the server's folder", os.path.isfile(moved) and os.path.getsize(moved) == 15000)
    code, out = run(["icacls", moved])
    check("and the server's account can change it (inherited rights)", f"NT SERVICE\\{name}".lower() in out.lower()
          and "(I)" in out, out[-400:])
    code, out = panel_python(SFTP_CLIENT, "127.0.0.1", str(port), f"admin.{name}", "wrong-password")
    check("a wrong password is refused", code != 0 and "Authentication failed" in out, out[-300:])
    code, log = p.get("/api/logs?lines=400")
    text = json.dumps(log)
    check("uploads are in the panel log with who did them", f"[SFTP] admin uploaded {name}/sftp-test.txt" in text,
          text[-400:])
    try:
        os.remove(moved)
    except OSError:
        pass
    code, r = p.put("/api/settings/sftp", {"enabled": False, "port": port})
    check("SFTP turns off", code == 200, f"{code} {r}")
    check("its service is gone", wait("gone", lambda: service_state("redstonepanel-sftp") is None, 60),
          str(service_state("redstonepanel-sftp")))
    code, out = ps(f"@(Get-NetFirewallRule -DisplayName 'RedstonePanel {port}/tcp' -ErrorAction SilentlyContinue).Count")
    check("and its firewall rule", out.strip().endswith("0"), out[-200:])


def windows_names(p):
    """1.24: an archive from Linux with names Windows can't hold is refused with the list."""
    section("File names Windows can't hold")
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for n in ("server.properties", "world/level.dat", "plugins/aux.yml", "world/Level.dat", "notes:1.txt"):
            info = tarfile.TarInfo(n)
            info.size = 1
            t.addfile(info, io.BytesIO(b"x"))
    boundary = "rpboundary" + os.urandom(6).hex()
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"edition\"\r\n\r\njava\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"linux.tar.gz\"\r\n"
            f"Content-Type: application/gzip\r\n\r\n").encode() + buf.getvalue() + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(p.base + "/api/servers/import", data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    req.add_header("Cookie", p.cookie or "")
    try:
        with urllib.request.urlopen(req, timeout=120, context=p.context()) as r:
            code, out = r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        code, out = e.code, e.read().decode("utf-8", "replace")
    check("importing a Linux archive with such names is refused, listing them",
          code == 400 and "plugins/aux.yml" in out and "notes:1.txt" in out and "world/Level.dat" in out, f"{code} {out[:400]}")


def first_link(home):
    """1.24: the URL in open-panel.url in the home folder (what the installer's Finish page opens)."""
    try:
        with open(os.path.join(home, "open-panel.url"), encoding="utf-8") as f:
            return next((l.strip()[4:] for l in f if l.startswith("URL=")), "")
    except OSError as e:
        return str(e)


def first_sign_in(p, home, note, password):
    """1.24: the one-use link to choose the first password. Returns the password admin has now."""
    link = first_link(home)
    check("the Finish page's link is the one-use 'choose your password' link", "/static/login.html#first=" in link, link)
    code, out = run(["icacls", os.path.join(home, "open-panel.url")])
    check("ordinary users can't read that link", "BUILTIN\\Users" not in out and "Everyone" not in out, out[:400])
    if "#first=" not in link:
        return password
    token = link.split("#first=", 1)[1]
    code, r = p.post("/api/auth/first-sign-in/check", {"token": token + "x"})
    check("a wrong first sign-in token is refused", code == 400, f"{code} {r}")
    code, r = p.post("/api/auth/first-sign-in/check", {"token": token})
    check("the link is for the admin account", code == 200 and r.get("username") == "admin", f"{code} {r}")
    code, r = p.post("/api/auth/first-sign-in", {"token": token, "password": "short"})
    check("a too-short password is refused, and the link still works", code == 400
          and p.post("/api/auth/first-sign-in/check", {"token": token})[0] == 200, f"{code} {r}")
    chosen = password + "-mine"
    code, r = p.post("/api/auth/first-sign-in", {"token": token, "password": chosen})
    check("choosing a password with the link signs in as admin", code == 200 and p.cookie
          and (r or {}).get("username") == "admin", f"{code} {r}")
    check("the link works only once", p.post("/api/auth/first-sign-in/check", {"token": token})[0] == 400)
    check("first-sign-in.txt is deleted (its random password no longer works)", not os.path.isfile(note))
    check("open-panel.url now goes to the panel's address", first_link(home) == local_url(p.base), first_link(home))
    code, r = Panel(p.base).post("/api/auth/login", {"username": "admin", "password": password})
    check("the random password no longer signs in", code == 401, f"{code}")
    code, r = Panel(local_url(p.base).rstrip("/")).get("/api/auth/passkeys/available")
    check("Windows Hello sign-in is offered at localhost", code == 200 and r.get("supported") and r.get("usable"),
          f"{code} {r}")
    code, r = p.get("/api/auth/passkeys/available")
    check("... and explained, not offered, at an IP address", code == 200 and r.get("supported")
          and not r.get("usable") and "IP address" in (r.get("why") or ""), f"{code} {r}")
    # Later steps, and the run after an upgrade, read admin's password from first-sign-in.txt
    with open(note, "w", encoding="utf-8") as f:
        f.write(f"Password:  {chosen}\n")
    p.cookie = None
    return chosen


def open_link():
    """1.24: the URL in RedstonePanel.url next to the program (the Start menu's RedstonePanel and
    the installer's Finish page open it)."""
    try:
        with open(os.path.join(PROGRAM, "RedstonePanel.url"), encoding="utf-8") as f:
            return next((l.strip()[4:] for l in f if l.startswith("URL=")), "")
    except OSError as e:
        return str(e)


def local_url(base):
    """http://127.0.0.1:8080 -> http://localhost:8080/"""
    return re.sub(r"//127\.0\.0\.1(?=[:/]|$)", "//localhost", base.rstrip("/")) + "/"


def _reconnect(p, base, password, seconds=120):
    """After the panel restarted itself: the new address, signed in again if needed."""
    p.base = base
    if not wait("restarted", lambda: p.get("/api/health", timeout=5)[0] == 200, seconds, every=3):
        return False
    if p.get("/api/auth/me")[0] != 200:
        p.post("/api/auth/login", {"username": "admin", "password": password})
    return p.get("/api/auth/me")[0] == 200


def cluster_alone(p, args):
    """1.24: a Windows panel becomes a cluster master (the cluster CA and certificates made with
    the cryptography library), makes join codes for both systems and an encrypted cluster backup,
    then goes back to how it was. Joining needs a second machine: Quentin's PC with Linux nodes."""
    section("A cluster master on Windows")
    code, view = p.get("/api/settings")
    old = (view or {}).get("settings") or {}
    old_base = p.base
    code, r = p.post("/api/settings/cluster", {"name": "Windows CI"})
    if not check("a cluster is created", code == 200 and r.get("restart"), f"{code} {r}"):
        return
    time.sleep(4)
    https = f"https://127.0.0.1:{old.get('https_port') or 8443}"
    check("the panel comes back with cluster HTTPS", _reconnect(p, https, args.password), p.base)
    check("the Start menu's link follows it to HTTPS", wait("link", lambda: open_link() == local_url(https), 30),
          open_link())
    code, cl = p.get("/api/settings/cluster")
    check("it is the master", code == 200 and (cl or {}).get("role") == "master", f"{code} {str(cl)[:300]}")
    import ssl
    try:
        pem = ssl.get_server_certificate(("127.0.0.1", int(old.get("https_port") or 8443)))
    except OSError as e:
        pem = str(e)
    ca = os.path.join(args.home, "data", "cluster", "ca.crt")
    code, out = panel_python(
        "from cryptography import x509; c = x509.load_pem_x509_certificate(sys.argv[2].encode()); "
        "ca = x509.load_pem_x509_certificate(open(sys.argv[3], 'rb').read()); "
        "print('ISSUER', c.issuer == ca.subject); "
        "print('NC', ca.extensions.get_extension_for_class(x509.NameConstraints).critical); "
        "print('SAN', c.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.IPAddress))",
        pem, ca)
    check("it serves a certificate from its own cluster CA, which has name constraints",
          "ISSUER True" in out and "NC True" in out and "SAN [" in out, out[-400:])
    code, j = p.post("/api/settings/cluster/joins", {"url": cl.get("join_url") if cl else ""})
    check("a join code says how to join from Linux and from Windows",
          code == 200 and j.get("command", "").startswith("sudo redstonepanel join '")
          and j.get("windows_command", "").startswith("redstonepanel join rpjoin1:"), f"{code} {str(j)[:300]}")
    code, out = run(["cmd", "/c", "redstonepanel", "cluster", "join-code"])
    check("`redstonepanel cluster join-code` prints both", code == 0 and "on Windows" in out and "redstonepanel join rpjoin1:" in out,
          out[-400:])
    code, data = p.call("POST", "/api/settings/cluster/backup", {"passphrase": "a long test passphrase"}, raw=True)
    check("an encrypted cluster backup downloads", code == 200 and isinstance(data, bytes) and data[:8] == b"Salted__",
          f"{code} {str(data)[:100]}")
    if code == 200 and isinstance(data, bytes):
        backup = os.path.join(args.home, "run", "test-cluster.rpbackup")
        with open(backup, "wb") as f:
            f.write(data)
        code, out = panel_python(
            "from core import cluster; m, files = cluster.read_backup(open(sys.argv[2], 'rb').read(), sys.argv[3]); "
            "print('CLUSTER', m['cluster']); print('KEY', b'PRIVATE KEY' in files['cluster/ca.key'])",
            backup, "a long test passphrase")
        check("it opens with the passphrase (OpenSSL's format, as on Linux)", "CLUSTER Windows CI" in out and "KEY True" in out,
              out[-400:])
        os.remove(backup)
    code, r = p.post("/api/settings/cluster/leave")
    check("the cluster is dissolved", code == 200 and r.get("restart"), f"{code} {r}")
    time.sleep(4)
    check("the panel comes back standalone", _reconnect(p, https, args.password), p.base)
    code, cl = p.get("/api/settings/cluster")
    check("standalone again", code == 200 and (cl or {}).get("role") == "standalone", f"{code} {str(cl)[:200]}")
    # Back to how the panel was before (plain HTTP on the runner; HTTPS on a PC that had it)
    if old.get("https_mode") and old["https_mode"] != "self_signed":
        code, view = p.get("/api/settings")
        now = (view or {}).get("settings") or {}
        code, r = p.put("/api/settings", dict(now, https_mode=old["https_mode"], cert_file=old.get("cert_file", ""),
                                              key_file=old.get("key_file", "")))
        time.sleep(4)
        check("the panel's own connection settings are put back", code == 200 and _reconnect(p, old_base, args.password),
              f"{code} {r}")
    else:
        _reconnect(p, old_base if old_base.startswith("https") else https, args.password)


def finish(p, args, name, port=None):
    if name and not args.keep:
        section("Removing the server")
        home = args.home
        code, r = p.call("DELETE", f"/api/servers/{name}?keep_world=false", timeout=300)
        if code >= 400:
            code, r = p.call("DELETE", f"/api/servers/{name}", {"keep_world": False}, timeout=300)
        check("the server is removed", code == 200, f"{code} {r}")
        check("its Windows service is gone", wait("gone", lambda: service_state(name) is None, 60), str(service_state(name)))
        left = []
        folder = os.path.join(getattr(args, "servers_dir", None) or os.path.join(home, "servers"), name)
        for root, dirs, files in os.walk(folder):
            left += [os.path.join(root, n) for n in files + dirs]
        check("its folder is gone", not os.path.exists(folder), f"{r} left: {left[:12]}")
        if port:
            code, out = ps(f"@(Get-NetFirewallRule -DisplayName 'RedstonePanel {port}/tcp' -ErrorAction SilentlyContinue).Count")
            check("its firewall rule is gone", out.strip().endswith("0"), out[-200:])
        code, out = run(["schtasks", "/Query", "/TN", f"\\RedstonePanel\\redstonepanel-restart@{name}"])
        check("its scheduled tasks are gone", code != 0, out[-200:])
    if hasattr(args, "saved_notifications"):
        restore_notifications(p, args)
    if getattr(args, "neighbour", None):
        neighbour_after(args.neighbour)
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    for f in FAILED:
        print(f"  FAILED: {f}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
