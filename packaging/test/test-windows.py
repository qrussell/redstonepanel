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
import socket
import subprocess
import sys
import threading
import time
import urllib.error
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

    def call(self, method, path, body=None, timeout=120, raw=False):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.cookie:
            req.add_header("Cookie", self.cookie)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
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
    ap.add_argument("--home", default=os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "RedstonePanel"))
    ap.add_argument("--keep", action="store_true", help="leave the test server in place")
    ap.add_argument("--loader", default="vanilla")
    ap.add_argument("--after-upgrade", action="store_true",
                    help="after installing again over a run with --keep: the account and the server are still there")
    args = ap.parse_args()
    home = args.home
    p = Panel(f"http://127.0.0.1:{args.port}")
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
    code, me = p.get("/api/auth/me")
    check("the API refuses a visitor who isn't signed in", code == 401, f"{code} {me}")
    code, who = p.post("/api/auth/login", {"username": "admin", "password": "wrong-password"})
    check("a wrong password is refused", code == 401, f"{code}")
    code, who = p.post("/api/auth/login", {"username": "admin", "password": password})
    check("admin signs in with the password from first-sign-in.txt", code == 200 and p.cookie, f"{code} {who}")
    code, me = p.get("/api/auth/me")
    check("the session works", code == 200, f"{code} {me}")
    code, info = p.get("/api/panel-info")
    print(f"        panel-info: {json.dumps(info)[:300]}")

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
    d = os.path.join(home, "servers", name)
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
    code, out = ps(f"Get-CimInstance Win32_Process -Filter \"Name='java.exe'\" | ForEach-Object {{ (Invoke-CimMethod -InputObject $_ -MethodName GetOwner).User }}")
    check("java.exe runs as that account, not as the system", name.lower() in out.lower() and "SYSTEM" not in out.upper().split(), out[-300:])

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
    # Only the Java the panel installed for this server: other Minecraft servers on the machine are left alone
    code, out = ps("Get-CimInstance Win32_Process -Filter \"Name='java.exe'\" | Where-Object { $_.ExecutablePath -like '"
                   + os.path.join(args.home, "java") + "\\*' } | Select-Object -ExpandProperty ProcessId")
    pids = [x for x in out.split() if x.isdigit()]
    print(f"        killing java.exe {pids}")
    for pid in pids:
        run(["taskkill", "/F", "/PID", pid])
    check("a killed server is reported as crashed by webhook",
          wait("hook", lambda: any("crashed" in h for h in HOOKS), 90), str(HOOKS)[-400:])
    check("... and is started again by itself", wait("listen", listening, 400, every=5))
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
    p.put("/api/settings/notifications", {"clear": True})

    # ------------------------------------------------------------------ the command line
    section("The redstonepanel command")
    code, out = run(["cmd", "/c", "redstonepanel", "status"])
    check("`redstonepanel status` runs from a prompt", code == 0 and "admin" in out, out[-400:])

    return finish(p, args, name, port)


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
        for root, dirs, files in os.walk(os.path.join(home, "servers", name)):
            left += [os.path.join(root, n) for n in files + dirs]
        check("its folder is gone", not os.path.exists(os.path.join(home, "servers", name)), f"{r} left: {left[:12]}")
        if port:
            code, out = ps(f"@(Get-NetFirewallRule -DisplayName 'RedstonePanel {port}/tcp' -ErrorAction SilentlyContinue).Count")
            check("its firewall rule is gone", out.strip().endswith("0"), out[-200:])
        code, out = run(["schtasks", "/Query", "/TN", f"\\RedstonePanel\\redstonepanel-restart@{name}"])
        check("its scheduled tasks are gone", code != 0, out[-200:])
    if getattr(args, "neighbour", None):
        neighbour_after(args.neighbour)
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    for f in FAILED:
        print(f"  FAILED: {f}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
