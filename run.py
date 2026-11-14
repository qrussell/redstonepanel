#!/usr/bin/env python3
"""RedstonePanel launcher and admin command (installed as /usr/sbin/redstonepanel by the .deb).

    redstonepanel                     start the panel the way settings.json says
                                      (HTTP by default; HTTPS when an admin turned it on)
    redstonepanel status              show the address, HTTPS/sign-in mode and panel users
    redstonepanel add-user NAME [--role admin|user]
                                      create a panel account (asks for the password)
    redstonepanel set-name "NAME"     name shown in the panel to tell servers apart
                                      ("" = use this server's host name)
    redstonepanel reset-security      recovery: plain HTTP on 8080, sign-in required, restart

If HTTPS is on but its certificate can't be used or the HTTPS port is taken, the panel starts
on plain HTTP instead and shows admins a warning, so a bad setting never locks you out.
"""
import os
import sys
import threading

APP_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(APP_DIR)
sys.path.insert(0, APP_DIR)

from core import settings  # noqa: E402


def reset_security():
    s = settings.load()
    s.update(https_mode="off", http_port=settings.DEFAULTS["http_port"], login_mode="required")
    settings.save(s)
    print(f"Security settings reset: HTTP on port {s['http_port']}, sign-in required.")
    if os.geteuid() == 0 and os.path.exists("/run/systemd/system"):
        import subprocess
        r = subprocess.run(["systemctl", "restart", "redstonepanel.service"])
        print("Restarted redstonepanel." if r.returncode == 0 else "Restart it with: sudo systemctl restart redstonepanel")


def main():
    import uvicorn
    s = settings.load()
    ssl_args, error = {}, None

    if s["https_mode"] != "off":
        try:
            if s["https_mode"] == "self_signed":
                from core import tls
                cert, key = tls.ensure_self_signed()
            else:
                cert, key = s["cert_file"], s["key_file"]
                from core import tls
                problem = tls.check_pair(cert, key)
                if problem:
                    raise RuntimeError(problem)
            if not settings.port_free(s["https_port"]):
                raise RuntimeError(f"HTTPS port {s['https_port']} is already in use")
            if not settings.port_free(s["http_port"]):
                raise RuntimeError(f"HTTP port {s['http_port']} (for the redirect) is already in use")
            ssl_args = {"ssl_certfile": cert, "ssl_keyfile": key}
        except Exception as e:
            error = f"HTTPS could not start ({e}); serving plain HTTP instead."
            print(f"[redstonepanel] {error}", file=sys.stderr)

    if ssl_args:
        from https_redirect import make_server
        redirect = make_server(s["http_port"], s["https_port"])
        threading.Thread(target=redirect.serve_forever, daemon=True).start()
        settings.RUNTIME.update(scheme="https", port=s["https_port"], https_error=None)
        port = s["https_port"]
    else:
        settings.RUNTIME.update(scheme="http", port=s["http_port"], https_error=error)
        port = s["http_port"]

    from main import app
    uvicorn.run(app, host="0.0.0.0", port=port, timeout_graceful_shutdown=5, **ssl_args)


def status():
    import subprocess
    from core import auth
    s = settings.load()
    active = subprocess.run(["systemctl", "is-active", "redstonepanel.service"],
                            capture_output=True, text=True).stdout.strip() or "unknown"
    ip = (subprocess.run(["hostname", "-I"], capture_output=True, text=True).stdout.split() or ["localhost"])[0]
    print(f"Name:      {settings.display_name(s)}" + ("" if s["panel_name"] else " (host name)"))
    print(f"Service:   {active}")
    print(f"Address:   {settings.panel_url(s, ip)}/")
    print(f"HTTPS:     {s['https_mode']}" + (f" (HTTP port {s['http_port']} redirects)" if s["https_mode"] != "off" else ""))
    print(f"Sign-in:   {'required' if s['login_mode'] == 'required' else 'not required from ' + ', '.join(s['trusted_networks'])}")
    print(f"Linux:     members of {' or '.join(auth.ALLOWED_LINUX_GROUPS)} can sign in")
    users = auth.list_panel_users()
    print(f"Panel users: {', '.join(u['username'] + ' (' + u['role'] + (', disabled' if u['disabled'] else '') + ')' for u in users) or 'none'}")
    print(f"Data:      {settings.DATA_DIR}")


def set_name(name):
    """Takes effect immediately: the panel reads settings.json again when it changes."""
    s = settings.load()
    try:
        s["panel_name"] = settings.clean_panel_name(name)
    except settings.SettingsError as e:
        sys.exit(str(e))
    settings.save(s)
    if s["panel_name"]:
        print(f"Panel name set to: {s['panel_name']}")
    else:
        print(f"Panel name cleared; showing the host name: {settings.host_name()}")


def add_user(args):
    import getpass
    from core import auth
    role = "admin"
    if "--role" in args:
        i = args.index("--role")
        role = args[i + 1] if i + 1 < len(args) else ""
        args = args[:i] + args[i + 2:]
    if len(args) != 1:
        sys.exit("Usage: redstonepanel add-user NAME [--role admin|user]")
    if sys.stdin.isatty():
        pw = getpass.getpass("Password: ")
        if pw != getpass.getpass("Repeat password: "):
            sys.exit("Passwords do not match.")
    else:
        pw = sys.stdin.readline().rstrip("\n")   # e.g. for scripts: echo "$PW" | redstonepanel add-user ...
    try:
        auth.create_panel_user(args[0], pw, role)
    except auth.AuthError as e:
        sys.exit(str(e))
    print(f"Created panel user '{args[0].strip().lower()}' ({role}).")


if __name__ == "__main__":
    cmd, rest = (sys.argv[1], sys.argv[2:]) if len(sys.argv) > 1 else (None, [])
    if cmd is None:
        main()
    elif cmd == "reset-security" and not rest:
        reset_security()
    elif cmd == "status" and not rest:
        status()
    elif cmd == "add-user":
        add_user(rest)
    elif cmd == "set-name" and len(rest) == 1:
        set_name(rest[0])
    elif cmd in ("-h", "--help", "help"):
        print(__doc__)
    else:
        sys.exit(__doc__)
