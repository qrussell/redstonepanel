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
    redstonepanel reset-2fa NAME      recovery: turn off two-factor sign-in for an account
                                      (panel or Linux) that lost its authenticator app
    redstonepanel removed             list game servers uninstalled with "Keep World"
    redstonepanel restore NAME        give a removed server a systemd service again and start it
    redstonepanel backup NAME         back up a game server now (its backup timer runs this too)
    redstonepanel restart-server NAME warn players, then restart a game server (its restart timer
                                      runs this)
    redstonepanel check-updates       look for newer server versions and update the Bedrock servers
                                      set to update automatically (its timer runs this every 6 hours)
    redstonepanel notify-test         send a test message to the notification webhook
    redstonepanel upgrade             update the redstonepanel package (apt), as the panel's Update button does

  Clusters (one master panel controls the game servers of other panels, its nodes):
    redstonepanel cluster create "NAME"   make this panel a cluster master
    redstonepanel cluster status          role, nodes, certificates
    redstonepanel cluster join-code [URL] make a single-use join code for a new node (master)
    redstonepanel join 'CODE'             join the cluster in a join code (from the master)
    redstonepanel leave                   node: leave the cluster; master: dissolve an empty one
    redstonepanel cluster backup FILE     master: encrypted backup of the cluster (asks a passphrase)
    redstonepanel cluster nightly on|off  master: an encrypted backup every night, keeping 7
                                          (on asks the passphrase they are encrypted with)
    redstonepanel cluster promote FILE    make this panel the master from a backup
    redstonepanel cluster-https           turn cluster HTTPS back on after reset-security

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


def reset_2fa(name):
    """Turns two-factor sign-in off for one account, e.g. after a lost phone. Takes effect at once."""
    from core import twofactor
    if twofactor._load().get("damaged"):
        os.replace(twofactor.TOTP_FILE, twofactor.TOTP_FILE + ".damaged")
        print(f"{twofactor.TOTP_FILE} could not be read, so every account was being asked for a "
              f"code it can't give. Moved it to {twofactor.TOTP_FILE}.damaged: every account now signs in "
              "with its password only and can set up two-factor again.")
        return
    name = name.strip()
    removed = [src for src in ("panel", "linux") if twofactor.remove(twofactor.account_key(src, name))]
    if not removed:
        sys.exit(f"'{name}' doesn't have two-factor sign-in set up.")
    print(f"Two-factor sign-in turned off for {name}. They sign in with their password and can set it up "
          "again under the Password button" + (" (it is required for admins on this panel, so they will be "
                                                "asked to)." if settings.load().get("require_2fa_admins") else "."))
    try:
        from core import manager
        manager.write_log(f"[AUTH] two-factor sign-in reset for '{name}' from the command line")
    except Exception:
        pass


def main():
    import uvicorn
    s = settings.load()
    ssl_args, error = {}, None

    if s["https_mode"] != "off":
        try:
            if s["https_mode"] == "self_signed":
                from core import tls
                cert, key = tls.ensure_self_signed()
            elif s["https_mode"] == "cluster":
                from core import cluster, tls
                cert, key = cluster.serving_cert()
                problem = tls.check_pair(cert, key)
                if problem:
                    raise RuntimeError(problem)
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
    from core import playerlog
    playerlog.on_new_events(playerlog.notify_events)
    playerlog.start()
    # Cluster masters: renew node certificates 30 days before they expire, nightly cluster backup
    from core import cluster_client, manager
    import main as main_module
    cluster_client.maintenance(manager.write_log, main_module._restart_soon)
    # Cluster masters: copy backups to the member chosen on each server's Backups tab (1.16.8)
    from core import backup_copies
    backup_copies.sweeper()
    # The update check timer (1.16.12): written here so upgrading the package sets it up
    from core import updates
    updates.ensure_timer()
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
    print(f"Sign-in:   {'required' if s['login_mode'] == 'required' else 'not required from ' + ', '.join(s['trusted_networks'])}"
          + ("; two-factor required for admins" if s.get("require_2fa_admins") else ""))
    print(f"Linux:     members of {' or '.join(auth.ALLOWED_LINUX_GROUPS)} can sign in")
    users = auth.list_panel_users()
    print(f"Panel users: {', '.join(u['username'] + ' (' + u['role'] + (', 2FA' if u['two_factor'] else '') + (', disabled' if u['disabled'] else '') + ')' for u in users) or 'none'}")
    from core import twofactor
    linux_2fa = sorted(k.split(":", 1)[1] for k in twofactor.enabled_keys() if k.startswith("linux:"))
    if linux_2fa:
        print(f"Two-factor (Linux accounts): {', '.join(linux_2fa)}")
    from core import cluster
    st = cluster.state()
    print(f"Cluster:   {st['role']}" + (f" of '{st.get('name') or st.get('cluster_name')}'" if st["role"] != "standalone" else "")
          + (" (details: redstonepanel cluster status)" if st["role"] != "standalone" else ""))
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


def _restart_service(what):
    """The panel only picks up HTTPS changes when it restarts."""
    if os.geteuid() == 0 and os.path.exists("/run/systemd/system"):
        import subprocess
        r = subprocess.run(["systemctl", "restart", "redstonepanel.service"])
        if r.returncode == 0:
            print(f"Restarted redstonepanel {what}.")
            return
    print("Restart the panel to apply it: sudo systemctl restart redstonepanel")


def _log(msg):
    from core import manager
    manager.write_log(f"[CLUSTER] {msg}")


def _actor():
    return f"{os.environ.get('SUDO_USER') or 'root'} (command line)"


def _passphrase(confirm):
    import getpass
    if not sys.stdin.isatty():
        return sys.stdin.readline().rstrip("\n")
    p = getpass.getpass("Backup passphrase: ")
    if confirm and p != getpass.getpass("Repeat passphrase: "):
        sys.exit("Passphrases do not match.")
    return p


def _when(t):
    import time
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else "never"


def cluster_cmd(args):
    from core import cluster
    try:
        if args[:1] == ["create"] and len(args) == 2:
            st = cluster.create_cluster(args[1], _actor())
            _log(f"{_actor()} created cluster '{st['name']}'; this panel is its master.")
            print(f"Created cluster '{st['name']}'. This panel is its master and now uses cluster HTTPS.")
            print("Add nodes with: sudo redstonepanel cluster join-code")
            _restart_service("with cluster HTTPS")
        elif args == ["status"]:
            st = cluster.state()
            print(f"Role:      {st['role']}")
            if st["role"] == "standalone":
                return
            print(f"Cluster:   {st.get('name') or st.get('cluster_name')} (epoch {st.get('epoch')})")
            print(f"CA:        {cluster.ca_fingerprint(cluster.read_ca())}")
            print(f"This cert: expires {_when(cluster.cert_expires(cluster.node_crt()))}")
            if st["role"] == "node":
                print(f"Master:    {st.get('master_name')} ({st.get('master_url')})")
                print(f"Last contact from the master: {_when(st.get('last_used'))}"
                      + (f" from {st['last_ip']}" if st.get("last_ip") else ""))
            else:
                nodes = cluster.load_nodes()
                print(f"Nodes:     {len(nodes)}")
                for n in nodes:
                    print(f"  {n['id']}  {n['name']:<24} {n['url']:<30} cert expires {_when(n.get('cert_expires'))}, "
                          f"last answer {_when(n.get('last_ok'))}")
                for j in cluster.list_joins():
                    print(f"  pending join code {j['id']} (expires {_when(j['expires'])})")
        elif args[:1] == ["join-code"] and len(args) <= 2:
            url = args[1] if len(args) == 2 else cluster.default_join_url(None)
            j = cluster.create_join(_actor(), url)
            _log(f"{_actor()} made a join code ({j['id']}), valid for one hour.")
            print(f"Join code (single use, valid until {_when(j['expires'])}). Keep it secret.\n")
            print(j["code"] + "\n")
            print("On the new server (Ubuntu with RedstonePanel installed), run:\n")
            print("  " + j["command"])
        elif args[:1] == ["backup"] and len(args) == 2:
            data = cluster.make_backup(_passphrase(confirm=True), _actor())
            fd = os.open(args[1], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            _log(f"{_actor()} wrote an encrypted cluster backup to {args[1]}.")
            print(f"Wrote {args[1]} ({len(data)} bytes). Store it somewhere other than this server, and keep")
            print("the passphrase safe: the backup holds the keys that control every node.")
        elif args[:1] == ["nightly"] and len(args) == 2 and args[1] in ("on", "off"):
            on = args[1] == "on"
            view = cluster.set_nightly(on, _passphrase(confirm=True) if on else None, _actor())
            _log(f"{_actor()} turned nightly cluster backups {'on' if view['enabled'] else 'off'}.")
            print(f"Nightly cluster backups are {'on' if view['enabled'] else 'off'}."
                  + (f" Copies go to {cluster.backups_dir()} after {cluster.NIGHTLY_HOUR}:00, keeping "
                     f"{cluster.NIGHTLY_KEEP}. Copy them somewhere else now and then." if view["enabled"] else ""))
        elif args[:1] == ["promote"] and len(args) == 2:
            with open(args[1], "rb") as f:
                data = f.read()
            if sys.stdin.isatty():
                print("This makes this panel the cluster's master. Its panel accounts and templates are")
                print("replaced by the backup's. The old master is locked out once nodes hear from this one.")
                if input("Continue? [y/N] ").strip().lower() != "y":
                    sys.exit("Cancelled.")
            st = cluster.promote(data, _passphrase(confirm=False), _actor())
            _log(f"{_actor()} promoted this panel to master of '{st['name']}' (epoch {st['epoch']}).")
            print(f"This panel is now the master of '{st['name']}' (epoch {st['epoch']}).")
            print("Point your bookmarks (or DNS name) at this server.")
            _restart_service("as the cluster master")
        else:
            sys.exit(__doc__)
    except cluster.ClusterError as e:
        sys.exit(str(e))


def join_cmd(code):
    from core import cluster, cluster_client
    try:
        st = cluster.join(code, _actor(), cluster_client)
    except cluster.ClusterError as e:
        sys.exit(str(e))
    _log(f"{_actor()} joined this panel to cluster '{st['cluster_name']}' (master: {st['master_name']}).")
    print(f"Joined cluster '{st['cluster_name']}'. Its master, {st['master_name']}, can now see and control")
    print("this panel's game servers. This panel now uses cluster HTTPS.")
    _restart_service("with cluster HTTPS")


def leave_cmd():
    from core import cluster
    try:
        if cluster.role() == "master":
            cluster.dissolve(_actor())
            _log(f"{_actor()} dissolved the cluster.")
            print("Cluster dissolved. This panel is standalone again (self-signed HTTPS).")
        else:
            cluster.leave()
            _log(f"{_actor()} left the cluster.")
            print("Left the cluster. This panel is standalone again (self-signed HTTPS).")
            print("Remove it on the master too, so the master stops listing it.")
    except cluster.ClusterError as e:
        sys.exit(str(e))
    _restart_service("as a standalone panel")


def cluster_https():
    from core import cluster
    if cluster.role() == "standalone":
        sys.exit("This panel isn't in a cluster.")
    s = settings.load()
    s["https_mode"] = "cluster"
    settings.save(s)
    print("Cluster HTTPS is on again.")
    _restart_service("with cluster HTTPS")


def removed_cmd():
    from core import manager
    servers = manager.list_removed_servers()
    if not servers:
        print("No removed servers.")
        return
    for s in servers:
        note = "" if s["can_restore"] else "  (can't tell Java from Bedrock)"
        if s["can_restore"] and not s["has_binary"]:
            note = "  (no server binary: restore downloads the latest)"
        print(f"{s['name']:<28} {s['edition']:<8} port {s['port'] or '?':<6} {s['directory']}{note}")
    print("\nRestore one with: sudo redstonepanel restore NAME")


def backup_cmd(name):
    from core import backups
    # The backup timer's service sets RP_BACKUP_KIND=scheduled; run by hand it's a manual backup,
    # which retention never deletes.
    kind = os.environ.get("RP_BACKUP_KIND", "manual")
    try:
        b = backups.run_backup(name, kind if kind in backups.KINDS else "manual")
    except backups.BackupError as e:
        sys.exit(str(e))
    print(f"Saved {backups.server_backup_dir(name)}/{b['file']} ({b['size'] / 1048576:.1f} MB)"
          + ("" if b["consistent"] else "; copied while the server was writing (see the panel log)") + ".")


def restart_server_cmd(name):
    from core import restarts
    try:
        result = restarts.run_restart(name)
    except restarts.RestartError as e:
        sys.exit(str(e))
    print(f"{name}: {result}.")


def check_updates_cmd():
    from core import updates
    try:
        done = updates.run_timer()
    except updates.UpdateError as e:
        sys.exit(str(e))
    state = updates.load_state()
    for name, entry in sorted(state["servers"].items()):
        print(f"{name}: {entry['label']} is available (runs {entry.get('current') or '?'}).")
    for name, err in sorted(state["errors"].items()):
        print(f"{name}: couldn't check ({err['error']}).")
    for name, r in sorted(done.items()):
        print(f"{name}: " + (f"updated to Bedrock {r.get('to')}." if r.get("ok") and not r.get("skipped")
                             else "already up to date." if r.get("ok") else f"update failed: {r.get('error')}"))
    if not state["servers"] and not state["errors"]:
        print("Every server runs the newest version.")
    if any(not r.get("ok") for r in done.values()):
        sys.exit(1)


def notify_test_cmd():
    from core import notify
    ok, err = notify.send_test()
    if not ok:
        sys.exit(err)
    print("Sent a test message to the webhook.")


def notify_unit_cmd(phase, name):
    # Run by the minecraft-*.service drop-in on every start and stop; must never fail the unit
    try:
        from core import notify
        notify.unit_event(phase, name)
    except Exception as e:
        print(f"redstonepanel notify-unit: {e}", file=sys.stderr)


def restore_cmd(name):
    from core import manager
    try:
        r = manager.restore_server(name)
    except (manager.ServerError, manager.DependencyError) as e:
        sys.exit(str(e))
    print(f"Restored {r['name']} on port {r['port']}"
          + (f" (port {r['old_port']} is used by another server)" if r["old_port"] else "") + ".")


def upgrade_cmd():
    """Upgrades the redstonepanel package (the panel's Update button runs this in its own unit)."""
    from core import selfupdate
    sys.exit(selfupdate.run(echo=sys.stdout.isatty()))


if __name__ == "__main__":
    cmd, rest = (sys.argv[1], sys.argv[2:]) if len(sys.argv) > 1 else (None, [])
    if cmd is None:
        main()
    elif cmd == "reset-security" and not rest:
        reset_security()
    elif cmd == "reset-2fa" and len(rest) == 1:
        reset_2fa(rest[0])
    elif cmd == "status" and not rest:
        status()
    elif cmd == "add-user":
        add_user(rest)
    elif cmd == "removed" and not rest:
        removed_cmd()
    elif cmd == "backup" and len(rest) == 1:
        backup_cmd(rest[0])
    elif cmd == "restart-server" and len(rest) == 1:
        restart_server_cmd(rest[0])
    elif cmd == "check-updates" and not rest:
        check_updates_cmd()
    elif cmd == "notify-test" and not rest:
        notify_test_cmd()
    elif cmd == "notify-unit" and len(rest) == 2 and rest[0] in ("start", "stop"):
        notify_unit_cmd(*rest)
    elif cmd == "restore" and len(rest) == 1:
        restore_cmd(rest[0])
    elif cmd == "set-name" and len(rest) == 1:
        set_name(rest[0])
    elif cmd == "cluster":
        cluster_cmd(rest)
    elif cmd == "join" and len(rest) == 1:
        join_cmd(rest[0])
    elif cmd == "leave" and not rest:
        leave_cmd()
    elif cmd == "upgrade" and not rest:
        upgrade_cmd()
    elif cmd == "cluster-https" and not rest:
        cluster_https()
    elif cmd in ("-h", "--help", "help"):
        print(__doc__)
    else:
        sys.exit(__doc__)
