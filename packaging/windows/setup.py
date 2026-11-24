"""Sets RedstonePanel up on Windows, and takes it out again. Run by the installer (as the
system, elevated); can be run by hand from an administrator prompt.

    python setup.py install [--home FOLDER] [--port 8080] [--no-start] [--no-firewall]
    python setup.py uninstall [--purge]
    python setup.py stop
    python setup.py check

install is safe to run again (an upgrade, a repair). It:
  * records where the panel keeps its things (install.json next to the program), by default
    C:\\ProgramData\\RedstonePanel: servers, backups, data, logs, Java;
  * makes those folders and leaves them to administrators and the system. Each game server
    later gets rights on its own folder only;
  * registers the event source, the `redstonepanel` Windows service (starts with Windows) and
    a Defender Firewall rule for the panel's own port;
  * on a first install, makes the panel account `admin` with a random password and writes it
    to first-sign-in.txt in the home folder (administrators only);
  * starts the panel.

stop stops the panel and the game servers and changes nothing else: the installer runs it
before it replaces the program's files (an upgrade), and the install that follows starts the
panel and the servers that start with Windows again.

uninstall stops and removes the panel's service, the game servers' services, the scheduled
tasks and the firewall rules. Worlds, backups and settings stay in the home folder unless
--purge is given.
"""
import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(os.path.dirname(HERE))            # ...\app
INSTALL_DIR = os.path.dirname(APP_DIR)                      # ...\RedstonePanel
INSTALL_JSON = os.path.join(INSTALL_DIR, "install.json")
EVENT_KEY = r"SYSTEM\CurrentControlSet\Services\EventLog\Application\RedstonePanel"
PANEL_UNIT = "redstonepanel.service"
USERS_SID, ADMINS_SID, SYSTEM_SID = "S-1-5-32-545", "S-1-5-32-544", "S-1-5-18"


def say(text):
    print(text, flush=True)


def run(cmd, **kw):
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    kw.setdefault("errors", "replace")
    return subprocess.run(cmd, creationflags=0x08000000, **kw)


def default_home():
    return os.path.join(os.environ.get("ProgramData") or r"C:\ProgramData", "RedstonePanel")


def saved_home():
    try:
        with open(INSTALL_JSON, encoding="utf-8") as f:
            return json.load(f).get("home")
    except (OSError, ValueError, AttributeError):
        return None


def load_panel(home):
    """Imports the panel's own code with `home` as its folder."""
    os.environ["RP_HOME"] = home
    if APP_DIR not in sys.path:
        sys.path.insert(0, APP_DIR)
    os.chdir(APP_DIR)


def check_home(home):
    home = os.path.abspath(home)
    if " " in home:
        raise SystemExit(f"The folder for servers and data can't have a space in its name: {home}\n"
                         "Choose one such as D:\\RedstonePanel.")
    if len(home) > 100:
        raise SystemExit("Choose a shorter folder for servers and data (100 characters at most).")
    if os.path.splitdrive(home)[1] in ("", "\\"):
        raise SystemExit("Choose a folder, not a whole drive, for servers and data.")
    return home


def install(args):
    home = check_home(args.home or saved_home() or default_home())
    first = not os.path.isfile(os.path.join(home, "data", "users.json"))
    with open(INSTALL_JSON, "w", encoding="utf-8") as f:
        json.dump({"home": home}, f)
    load_panel(home)
    from core.platform import paths
    from core.platform.windows import _win, services

    # Folders: the home is the panel's (administrators and the system); the units and Java are
    # readable by everyone, because each game server's own account has to read them
    say(f"Folders in {home}")
    for d in (home, paths.DATA_DIR, os.path.dirname(paths.LOG_FILE), paths.SERVERS_ROOT, paths.BACKUP_ROOT,
              paths.UNIT_DIR, paths.JOURNAL_DIR, paths.JVM_ROOT, paths.RUN_DIR):
        os.makedirs(d, exist_ok=True)
    _win.lock_down(home)
    run(["icacls", home, "/grant", f"*{USERS_SID}:(RX)", "/C", "/Q"])              # this folder only: pass through
    for d in (paths.UNIT_DIR, paths.JVM_ROOT):
        run(["icacls", d, "/grant", f"*{USERS_SID}:(OI)(CI)RX", "/C", "/Q"])
    for d in (paths.SERVERS_ROOT, paths.JOURNAL_DIR):
        run(["icacls", d, "/grant", f"*{USERS_SID}:(RX)", "/C", "/Q"])             # pass through to one's own folder

    # The event source: entries read properly in Event Viewer (EventCreate.exe carries a plain "%1" message)
    try:
        import winreg
        with winreg.CreateKey(winreg.HKEY_LOCAL_MACHINE, EVENT_KEY) as k:
            winreg.SetValueEx(k, "EventMessageFile", 0, winreg.REG_EXPAND_SZ, r"%SystemRoot%\System32\EventCreate.exe")
            winreg.SetValueEx(k, "TypesSupported", 0, winreg.REG_DWORD, 7)
    except OSError as e:
        say(f"Couldn't register the event source: {e}")

    # The panel's own service, described by a unit file like every other
    unit = os.path.join(paths.UNIT_DIR, PANEL_UNIT)
    text = f"""[Unit]
Description=RedstonePanel - Minecraft server management panel

[Service]
Type=simple
# Runs as the system: it manages services, firewall rules and scheduled tasks.
ExecStart=redstonepanel
WorkingDirectory={APP_DIR}
Environment=PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# Panel Settings changes restart the panel by exiting; the runner brings it back.
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
"""
    with open(unit, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    name = _win.PANEL_SERVICE
    if _win.service_status(name)[0] not in (None, "stopped"):
        say("Stopping the running panel")
        _win.stop_service(name, wait=60)
    command = subprocess.list2cmdline(_win.runner_command("service", PANEL_UNIT))
    _win.ensure_service(name, command, "RedstonePanel",
                        "RedstonePanel web panel for Minecraft servers. Open http://localhost:8080 in a browser.",
                        None, restart_after=5)
    _win.set_start(name, "auto")
    say("Service 'redstonepanel' registered (starts with Windows)")

    # The first account
    from core import auth, settings
    port = int(args.port or settings.load().get("http_port") or 8080)
    if args.port:
        s = settings.load()
        s["http_port"] = port
        settings.save(s)
    note = os.path.join(home, "first-sign-in.txt")
    if first and not auth.list_panel_users():
        password = "-".join(secrets.token_hex(3) for _ in range(3))
        first_name = "admin"
        if auth.linux_account_exists(first_name):          # a Windows account is called admin already
            first_name = "panel-admin"
        auth.create_panel_user(first_name, password, "admin")
        with open(note, "w", encoding="utf-8", newline="\r\n") as f:
            f.write("RedstonePanel\n\n"
                    f"Open:      http://localhost:{port}\n"
                    f"User name: {first_name}\n"
                    f"Password:  {password}\n\n"
                    "Change the password after signing in (the Password button), then delete this file.\n"
                    "More accounts: Users in the panel, or `redstonepanel add-user NAME` in an administrator prompt.\n")
        say(f"Account '{first_name}' created; its password is in {note}")

    if not args.no_firewall:
        rule = _win.ps_quote(f"RedstonePanel web panel ({port}/tcp)")
        r = _win.powershell(
            f"Get-NetFirewallRule -Group 'RedstonePanel panel' -ErrorAction SilentlyContinue | Remove-NetFirewallRule; "
            f"New-NetFirewallRule -DisplayName {rule} -Group 'RedstonePanel panel' -Direction Inbound -Action Allow "
            f"-Protocol TCP -LocalPort {port} -Profile Any | Out-Null", timeout=120)
        say(f"Firewall: port {port} open for the panel" if r.returncode == 0
            else f"Couldn't open port {port} in the firewall: {(r.stderr or r.stdout).strip()[:200]}")

    services.prepare()                               # the drop-in, the notification task, game servers' services
    # Game servers that started with Windows before an uninstall or upgrade do so again
    auto_file = os.path.join(paths.UNIT_DIR, "autostart.json")
    try:
        with open(auto_file, encoding="utf-8") as f:
            for svc in json.load(f):
                try:
                    _win.set_start(str(svc), "auto")
                except OSError:
                    pass
        os.remove(auto_file)
    except (OSError, ValueError):
        pass
    if not args.no_start:
        state = _win.start_service(name, wait=60)
        say(f"Panel service: {state}")
        # ... and the ones set to start with Windows are started now
        for unit_name in services.unit_files("minecraft"):
            svc = unit_name[:-len(".service")]
            try:
                cfg = _win.service_config(svc)
                if _win.is_ours(cfg) and cfg.get("start") == "auto" and _win.service_status(svc)[0] == "stopped":
                    _win.start_service(svc)
            except OSError:
                pass
    say(f"RedstonePanel is installed. Open http://localhost:{port}")
    return 0


def uninstall(args):
    home = saved_home() or default_home()
    load_panel(home)
    from core.platform import paths
    from core.platform.windows import _win
    remembered = {}
    for name in _win.list_services(("minecraft", "phantom-", "redstonepanel")):
        try:
            cfg = _win.service_config(name)
            if not _win.is_ours(cfg):
                continue
            remembered[name] = cfg["start"]
            say(f"Stopping and removing the service {name}")
            _win.stop_service(name, wait=120)
            _win.delete_service(name)
        except OSError as e:
            say(f"  {e}")
    # Which game servers started with Windows: put back by the next install
    try:
        with open(os.path.join(paths.UNIT_DIR, "autostart.json"), "w", encoding="utf-8") as f:
            json.dump([n for n, start in remembered.items() if start == "auto" and n != _win.PANEL_SERVICE], f)
    except OSError:
        pass
    _win.powershell("$s = New-Object -ComObject Schedule.Service; $s.Connect(); "
                    "try { $f = $s.GetFolder('\\RedstonePanel'); $f.GetTasks(1) | ForEach-Object { $f.DeleteTask($_.Name, 0) }; "
                    "$s.GetFolder('\\').DeleteFolder('RedstonePanel', 0) } catch { }", timeout=120)
    _win.powershell("Get-NetFirewallRule -Group 'RedstonePanel panel' -ErrorAction SilentlyContinue | Remove-NetFirewallRule; "
                    + ("Get-NetFirewallRule -Group 'RedstonePanel' -ErrorAction SilentlyContinue | Remove-NetFirewallRule"
                       if args.purge else ""), timeout=120)
    try:
        import winreg
        winreg.DeleteKey(winreg.HKEY_LOCAL_MACHINE, EVENT_KEY)
    except OSError:
        pass
    try:
        os.remove(INSTALL_JSON)
    except OSError:
        pass
    if args.purge:
        say(f"Deleting {home}")
        shutil.rmtree(home, ignore_errors=True)
    else:
        say(f"Worlds, backups and settings were kept in {home}")
    return 0


def stop(args):
    home = saved_home() or default_home()
    load_panel(home)
    from core.platform.windows import _win
    names = _win.list_services(("minecraft", "phantom-", "redstonepanel"))
    for name in sorted(names, key=lambda n: n != _win.PANEL_SERVICE):          # the panel first: it would start servers again
        try:
            cfg = _win.service_config(name)
            if _win.is_ours(cfg) and _win.service_status(name)[0] not in (None, "stopped"):
                say(f"Stopping {name}")
                _win.stop_service(name, wait=120)
        except OSError as e:
            say(f"  {e}")
    return 0


def check(args):
    home = saved_home() or default_home()
    load_panel(home)
    from core.platform.windows import _win
    state, pid = _win.service_status(_win.PANEL_SERVICE)
    say(f"home: {home}\nservice: {state} (pid {pid})")
    return 0 if state == "running" else 1


def main():
    p = argparse.ArgumentParser(description="Set up or remove RedstonePanel on Windows.")
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("install")
    i.add_argument("--home")
    i.add_argument("--port", type=int)
    i.add_argument("--no-start", action="store_true")
    i.add_argument("--no-firewall", action="store_true")
    u = sub.add_parser("uninstall")
    u.add_argument("--purge", action="store_true")
    sub.add_parser("stop")
    sub.add_parser("check")
    args = p.parse_args()
    if os.name != "nt":
        raise SystemExit("This is the Windows setup; on Linux install the redstonepanel package.")
    started = time.time()
    code = {"install": install, "uninstall": uninstall, "stop": stop, "check": check}[args.cmd](args)
    say(f"({args.cmd} took {time.time() - started:.0f} s)")
    return code


if __name__ == "__main__":
    sys.exit(main())
