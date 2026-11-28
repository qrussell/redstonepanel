#!/usr/bin/env python3
"""RedstonePanel launcher and admin command (installed as /usr/sbin/redstonepanel by the .deb).

    redstonepanel                     start the panel the way settings.json says
                                      (HTTP by default; HTTPS when an admin turned it on)
    redstonepanel status              show the address, HTTPS/sign-in mode and panel users
    redstonepanel add-user NAME [--role admin|user]
                                      create a panel account (asks for the password)
    redstonepanel reset-password NAME set a panel account's new password (asks for it, or reads one
                                      line from stdin) and sign it out everywhere
    redstonepanel token list          API tokens for scripts and bots (Panel Settings > API tokens)
    redstonepanel token create NAME --perms view,power [--servers minecraft-a,minecraft-b] [--days 90]
                                      make one (every server unless --servers); prints it once
    redstonepanel token revoke NAME   revoke one; requests with it are refused at once
    redstonepanel boot NAME on|off    start a game server when the machine starts, or not
    redstonepanel sleep NAME on [MINUTES]|off
                                      sleep mode: stop a game server after MINUTES (15) with nobody on,
                                      and start it when a player joins (the panel listens on its port)
    redstonepanel set-name "NAME"     name shown in the panel to tell servers apart
                                      ("" = use this server's host name)
    redstonepanel reset-security      recovery: plain HTTP on 8080, sign-in required, restart
    redstonepanel reset-2fa NAME      recovery: turn off two-factor sign-in for an account
                                      (panel or Linux) that lost its authenticator app, and
                                      remove its passkeys (Windows Hello)
    redstonepanel terminal on|off     turn the panel's terminal on or off (Panel Settings > Terminal);
                                      off closes open terminals within a minute
    redstonepanel removed             list game servers uninstalled with "Keep World"
    redstonepanel restore NAME        give a removed server a systemd service again and start it
    redstonepanel backup NAME         back up a game server now (its backup timer runs this too)
    redstonepanel offsite-copy NAME   copy a game server's backups to its backup storage now
                                      (SSH or S3, Panel Settings > Backup storage)
    redstonepanel offsite-key         show this panel's SSH public key for backup storage
    redstonepanel restart-server NAME warn players, then restart a game server (its restart timer
                                      runs this)
    redstonepanel map-draw NAME [--now]  draw a vanilla or Bedrock server's world map (its map timer
                                      runs this; --now draws even when the world hasn't changed)
    redstonepanel check-updates       look for newer server versions and update the Bedrock servers
                                      set to update automatically (its timer runs this every 6 hours)
    redstonepanel notify-test         send a test message to the notification webhook
    redstonepanel ports               the Port check: which program holds each port the servers and
                                      the panel need, and the conflicts found (works while the
                                      panel itself is stopped)
    redstonepanel sftp-serve          run the SFTP server (its service, redstonepanel-sftp.service,
                                      runs this while SFTP is on under Panel Settings > SFTP)
    redstonepanel upgrade             update the redstonepanel package (apt), as the panel's Update button does
    redstonepanel uninstall           remove the panel, choosing first what happens to each game server:
                                      stop it and keep its files (recommended), keep it running
                                      without the panel (Linux), or delete it (after a backup);
                                      on Windows it then says to finish in Settings > Apps
        [--servers stop|keep|delete] [--purge] [--yes]   the same without questions

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
import time

APP_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(APP_DIR)
sys.path.insert(0, APP_DIR)

from core import settings  # noqa: E402
from core.platform import paths, services, system  # noqa: E402


def reset_security():
    s = settings.load()
    s.update(https_mode="off", http_port=settings.DEFAULTS["http_port"], login_mode="required")
    settings.save(s)
    print(f"Security settings reset: HTTP on port {s['http_port']}, sign-in required.")
    if system.is_admin() and services.available():
        print("Restarted redstonepanel." if services.restart_panel() == 0
              else system.hint("restart-panel"))


def terminal_cmd(what):
    """Turns the panel's terminal (1.21.10) on or off from SSH, e.g. in a hurry."""
    from core import terminal
    if not terminal.platform_terminal.supported():
        sys.exit("This machine's system doesn't have the terminal yet.")
    s = terminal.turn(what == "on", "the redstonepanel command")
    print("The terminal is " + ("on. Admins open it from the Dashboard (over HTTPS)." if s["terminal_enabled"]
                                else "off. Open terminals close within a minute."))


def reset_2fa(name):
    """Turns two-factor sign-in off for one account, e.g. after a lost phone. Takes effect at once."""
    from core import twofactor
    if twofactor._load().get("damaged"):
        os.replace(twofactor.TOTP_FILE, twofactor.TOTP_FILE + ".damaged")
        print(f"{twofactor.TOTP_FILE} could not be read, so every account was being asked for a "
              f"code it can't give. Moved it to {twofactor.TOTP_FILE}.damaged: every account now signs in "
              "with its password only and can set up two-factor again.")
        return
    from core import passkeys
    name = name.strip()
    removed = [src for src in ("panel", "linux") if twofactor.remove(twofactor.account_key(src, name))]
    keys_removed = [src for src in ("panel", "linux") if passkeys.remove_all(twofactor.account_key(src, name))]
    if keys_removed:
        print(f"Removed {name}'s passkeys (Windows Hello); they can add them again after signing in.")
    if not removed:
        if keys_removed:
            return
        sys.exit(f"'{name}' doesn't have two-factor sign-in set up.")
    print(f"Two-factor sign-in turned off for {name}. They sign in with their password and can set it up "
          "again under the Password button" + (" (it is required for admins on this panel, so they will be "
                                                "asked to)." if settings.load().get("require_2fa_admins") else "."))
    try:
        from core import manager
        manager.write_log(f"[AUTH] two-factor sign-in reset for '{name}' from the command line")
    except Exception:
        pass


def local_url(scheme, port):
    """The panel's address in a browser on this computer."""
    default = 443 if scheme == "https" else 80
    return f"{scheme}://localhost" + ("" if int(port) == default else f":{port}") + "/"


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
        settings.TLS_FILES.update(cert=ssl_args["ssl_certfile"], key=ssl_args["ssl_keyfile"])
        port = s["https_port"]
    else:
        settings.RUNTIME.update(scheme="http", port=s["http_port"], https_error=error)
        port = s["http_port"]
    # 1.24, Windows: the Start menu's RedstonePanel and the installer's Finish page open this
    try:
        system.write_open_link(local_url(settings.RUNTIME["scheme"], port))
    except OSError as e:
        print(f"[redstonepanel] Couldn't write the link to the panel: {e}", file=sys.stderr)

    from main import app
    from core import playerlog
    playerlog.on_new_events(playerlog.notify_events)
    playerlog.start()
    # Sleep mode (1.26): stop servers nobody plays on, listen on their ports, start them on a join
    from core import sleep
    sleep.start()
    # Discord /start (1.27): the panel's own bot, when an admin turned it on
    from core import discordbot
    discordbot.start()
    # Cluster masters: renew node certificates 30 days before they expire, nightly cluster backup
    from core import cluster_client, manager
    import main as main_module
    cluster_client.maintenance(manager.write_log, main_module._restart_soon)
    # 1.24: "HTTPS with my own certificate" renewed by certbot or win-acme: restart to serve it
    if ssl_args and s["https_mode"] == "custom":
        from core import tls
        tls.watch_own_certificate(ssl_args["ssl_certfile"], ssl_args["ssl_keyfile"], manager.write_log,
                                  main_module._restart_soon)
    # Cluster masters: copy backups to the member chosen on each server's Backups tab (1.16.8)
    from core import backup_copies
    backup_copies.sweeper()
    # Any panel: copy backups to SSH or S3 storage (1.16.11), retrying what's missing or failed
    from core import backup_targets
    backup_targets.sweeper()
    # Once after upgrading to 1.21.8: files the panel wrote in server folders as root go to the
    # game account, since it now works there as that account (before anything below writes there)
    try:
        manager.upgrade_game_folder_owners()
    except Exception as e:
        manager.write_log(f"[SYSTEM] Handing server files to the game account failed: {e}")
    # Once after upgrading to 1.16.11: open TCP server-port and UDP 7551 for Bedrock servers
    # without a transport line (NetherNet by default), which only had UDP server-port
    try:
        manager.upgrade_bedrock_firewall()
    except Exception as e:
        manager.write_log(f"[FIREWALL] Bedrock firewall check failed: {e}")
    # Windows: the panel's own firewall rule opens its HTTP and HTTPS ports (1.24.1: a cluster
    # created or joined on 1.24.0 turned HTTPS on without opening it). Changes nothing when it matches.
    settings.sync_panel_firewall()
    # Follow server.properties edited outside the panel, and servers removed by hand (1.16.11)
    manager.firewall_watcher()
    # The update check timer (1.16.12): written here so upgrading the package sets it up
    from core import updates
    updates.ensure_timer()
    # 1.23.2: back after being uninstalled: servers that were left alone get their timers again
    try:
        from core import uninstall
        uninstall.after_reinstall()
    except Exception as e:
        manager.write_log(f"[UNINSTALL] Couldn't turn the schedules back on: {e}")
    # Performance history: every server and the host, once a minute (1.17)
    from core import perf
    perf.sampler()
    # 1.18: sign-in failures are shared with the SFTP service; a start clears them as before
    from core import auth, grants, sftp
    auth.reset_fails()
    # Once, on the first start of 1.18: `user` accounts made before per-server permissions keep
    # what they could do, on every server
    try:
        kept = grants.migrate_existing_users([u["username"] for u in auth.list_panel_users() if u["role"] == "user"])
        if kept:
            manager.write_log(f"[AUTH] Per-server permissions: {', '.join(kept)} kept what user accounts could "
                              "always do, on every server. Change it under Users.")
    except Exception as e:
        manager.write_log(f"[AUTH] Setting up per-server permissions failed: {e}")
    # The SFTP service follows the setting (and an updated package's unit)
    try:
        sftp.apply(s)
    except Exception as e:
        manager.write_log(f"[SFTP] Couldn't set up the SFTP service: {e}")
    # 1.21.5: the public join pages, on their own port, while they are on (Panel Settings)
    try:
        from core import joinsite
        joinsite.apply(s)
    except Exception as e:
        manager.write_log(f"[JOIN] Couldn't start the join pages: {e}")
    uvicorn.run(app, host="0.0.0.0", port=port, timeout_graceful_shutdown=5, **ssl_args)


def status():
    from core import auth
    s = settings.load()
    active = services.panel_state() or "unknown"
    ip = (system.addresses() or ["localhost"])[0]
    print(f"Name:      {settings.display_name(s)}" + ("" if s["panel_name"] else " (host name)"))
    print(f"Service:   {active}")
    print(f"Address:   {settings.panel_url(s, ip)}/")
    print(f"HTTPS:     {s['https_mode']}" + (f" (HTTP port {s['http_port']} redirects)" if s["https_mode"] != "off" else ""))
    print(f"Sign-in:   {'required' if s['login_mode'] == 'required' else 'not required from ' + ', '.join(s['trusted_networks'])}"
          + ("; two-factor required for admins" if s.get("require_2fa_admins") else ""))
    if paths.WINDOWS:
        print("Windows:   members of Administrators or RedstonePanel can sign in (as admins)")
    else:
        print(f"Linux:     members of {' or '.join(auth.ALLOWED_LINUX_GROUPS)} can sign in (as admins)")
    users = auth.list_panel_users()
    print(f"Panel users: {', '.join(u['username'] + ' (' + u['role'] + (', 2FA' if u['two_factor'] else '') + (', disabled' if u['disabled'] else '') + ')' for u in users) or 'none'}")
    from core import twofactor
    linux_2fa = sorted(k.split(":", 1)[1] for k in twofactor.enabled_keys() if k.startswith("linux:"))
    if linux_2fa:
        print(f"Two-factor ({'Windows' if paths.WINDOWS else 'Linux'} accounts): {', '.join(linux_2fa)}")
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


def reset_password(args):
    """Sets a panel account's password from an admin prompt or SSH (forgotten password, no other
    admin). Signs the account out everywhere. Windows and Linux accounts change theirs in the system."""
    import getpass
    from core import auth
    if len(args) != 1:
        sys.exit("Usage: redstonepanel reset-password NAME")
    name = args[0].strip().lower()
    if not any(u["username"] == name for u in auth.list_panel_users()):
        where = "in Windows" if paths.WINDOWS else "with passwd"
        sys.exit(f"'{name}' isn't a panel account. " + (f"Windows and Linux accounts change their password {where}."
                                                        if auth.linux_account_exists(args[0].strip()) else
                                                        "See the accounts with: redstonepanel status"))
    if sys.stdin.isatty():
        pw = getpass.getpass(f"New password for {name}: ")
        if pw != getpass.getpass("Repeat it: "):
            sys.exit("Passwords do not match.")
    else:
        pw = sys.stdin.readline().rstrip("\r\n")   # e.g. echo "$PW" | redstonepanel reset-password NAME
    try:
        auth.update_panel_user(name, password=pw)
    except auth.AuthError as e:
        sys.exit(str(e))
    print(f"Password changed for '{name}'. They were signed out everywhere; two-factor sign-in, if on, still applies.")
    try:
        from core import manager
        manager.write_log(f"[AUTH] password reset for panel user '{name}' from the command line")
    except Exception:
        pass


def token_cmd(args):
    """API tokens (1.25) from an admin prompt or SSH."""
    from core import apitokens, grants
    if args == ["list"]:
        tokens = apitokens.list_tokens()
        if not tokens:
            print("No API tokens.")
        for tk in tokens:
            what = "; ".join(f"{'every server' if k == '*' else k}: {','.join(v)}" for k, v in tk["grants"].items())
            when = time.strftime("%Y-%m-%d", time.localtime(tk["expires"])) if tk["expires"] else "never"
            used = time.strftime("%Y-%m-%d %H:%M", time.localtime(tk["last_used"])) if tk["last_used"] else "never"
            print(f"{tk['name']}  ({what})  expires {when}{' (expired)' if tk['expired'] else ''}  last used {used}")
        return
    if len(args) >= 2 and args[0] == "revoke":
        name = " ".join(args[1:])
        match = [tk for tk in apitokens.list_tokens() if tk["name"].lower() == name.lower()]
        if not match:
            sys.exit(f"No API token called {name}. See them with: redstonepanel token list")
        apitokens.revoke(match[0]["id"])
        _log_cli(f"[API] revoked the API token {match[0]['name']} from the command line")
        print(f"Revoked {match[0]['name']}.")
        return
    if len(args) >= 2 and args[0] == "create":
        opts, name = {"--perms": None, "--servers": None, "--days": None}, []
        it = iter(args[1:])
        for a in it:
            if a in opts:
                opts[a] = next(it, None)
            else:
                name.append(a)
        if not opts["--perms"]:
            sys.exit(f"Say what it may do: --perms with any of {','.join(grants.PERMS)}")
        perms = [x.strip() for x in opts["--perms"].split(",") if x.strip()]
        keys = [x.strip() for x in (opts["--servers"] or "*").split(",") if x.strip()]
        try:
            out = apitokens.create(" ".join(name), {k: perms for k in keys}, opts["--days"], actor="command line")
        except apitokens.TokenError as e:
            sys.exit(str(e))
        _log_cli(f"[API] made the API token {out['name']} from the command line")
        print(f"Token {out['name']} (shown only now):\n{out['token']}")
        return
    sys.exit("Usage: redstonepanel token list | create NAME --perms view,power [--servers a,b] [--days N] | revoke NAME")


def sleep_cmd(args):
    """Sleep mode (1.26) for one game server."""
    from core import manager, sleep
    name, on = args[0], args[1] == "on"
    mins = args[2] if len(args) > 2 else None
    if mins is not None and not mins.isdigit():
        sys.exit("MINUTES must be a whole number (1 to 1440).")
    try:
        info = sleep.turn(name, on, int(mins) if mins else None, actor="command line")
    except (manager.ServerError, sleep.SleepError) as e:
        sys.exit(str(e))
    if on:
        print(f"{name} sleeps after {info['minutes']} minute{'s' if info['minutes'] != 1 else ''} with nobody on, "
              f"and starts when a player joins.")
    else:
        print(f"Sleep mode is off for {name}.")


def boot_cmd(args):
    """Start at boot (1.25) for one game server."""
    from core import bootstart, manager
    name, on = args[0], args[1]
    try:
        manager._check_server(name)
    except manager.ServerError as e:
        sys.exit(str(e))
    now = bootstart.turn(name, on == "on")
    _log_cli(f"[SERVICE] Start at boot turned {on} for {name} from the command line")
    print(f"{name} {'starts' if now else 'does not start'} when the machine starts.")


def _log_cli(text):
    try:
        from core import manager
        manager.write_log(text)
    except Exception:
        pass


def _restart_service(what):
    """The panel only picks up HTTPS changes when it restarts."""
    if system.is_admin() and services.available():
        if services.restart_panel() == 0:
            print(f"Restarted redstonepanel {what}.")
            return
    print("Restart the panel to apply it: " + ("Restart-Service redstonepanel (PowerShell as administrator)"
                                               if paths.WINDOWS else "sudo systemctl restart redstonepanel"))


def _log(msg):
    from core import manager
    manager.write_log(f"[CLUSTER] {msg}")


def _actor():
    if paths.WINDOWS:
        return f"{os.environ.get('USERNAME') or 'administrator'} (command line)"
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
            print("Add nodes with: " + ("" if paths.WINDOWS else "sudo ") + "redstonepanel cluster join-code")
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
            print("On the new server (with RedstonePanel installed), run on Linux:\n")
            print("  " + j["command"] + "\n")
            print("or on Windows, in a command prompt opened as administrator:\n")
            print("  " + j["windows_command"])
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
    # 1.16.11: copies to backup storage run in their own unit, after this one has finished
    from core import backup_targets
    try:
        if backup_targets.start_after_backup(name):
            print("Copying it to backup storage (see the panel log).")
    except Exception as e:
        print(f"Couldn't start copying to backup storage: {e}", file=sys.stderr)
    if kind == "scheduled":                     # 1.30: a world map drawn after each scheduled backup
        from core import mapdraw
        try:
            mapdraw.after_backup(name)
        except Exception as e:
            print(f"Couldn't start drawing the world map: {e}", file=sys.stderr)


def offsite_copy_cmd(name):
    from core import backup_targets, backups
    if not backups.NAME_RE.fullmatch(name):
        sys.exit(f"'{name}' isn't a server name.")
    if not backup_targets.targets_for(name):
        sys.exit(f"No backup storage keeps copies of {name}. Add one in Panel Settings > Backup storage.")
    failed = False
    for tid, r in backup_targets.sync(name, force=True).items():
        label = backup_targets.load()["targets"].get(tid, {}).get("name", tid)
        if r.get("error"):
            failed = True
            print(f"{label}: {r['error']}")
        else:
            print(f"{label}: copied {len(r['copied'])}, deleted {len(r['deleted'])} old.")
    sys.exit(1 if failed else 0)


def offsite_key_cmd():
    from core import backup_targets
    try:
        print(backup_targets.public_key())
    except backup_targets.TargetError as e:
        sys.exit(str(e))


def restart_server_cmd(name):
    from core import restarts
    try:
        result = restarts.run_restart(name)
    except restarts.RestartError as e:
        sys.exit(str(e))
    print(f"{name}: {result}.")


def map_draw_cmd(name, now=False):
    """1.30: draw a server's world map (its map timer runs this; Draw now adds --now)."""
    from core import mapdraw, manager
    try:
        st = mapdraw.run(name, now=now)
    except (mapdraw.DrawError, manager.ServerError) as e:
        sys.exit(str(e))
    print(f"{name}: {mapdraw.describe(st)}.")
    if st.get("state") == "failed":
        sys.exit(1)


def ports_cmd():
    from core import portaudit
    print(portaudit.text_report())


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


def stop_unit_cmd(name, mainpid=""):
    # Run by the minecraft-*.service drop-in's ExecStop; must never fail the unit's stop
    try:
        from core import notify
        notify.stop_bedrock(name, int(mainpid) if mainpid.isdigit() else None)
    except Exception as e:
        print(f"redstonepanel stop-unit: {e}", file=sys.stderr)


def restore_cmd(name):
    from core import manager
    try:
        r = manager.restore_server(name)
    except (manager.ServerError, manager.DependencyError) as e:
        sys.exit(str(e))
    print(f"Restored {r['name']} on port {r['port']}"
          + (f" (port {r['old_port']} is used by another server)" if r["old_port"] else "") + ".")


def _ask(question, answers, default=None):
    """One line of input until it's one of answers (lower case); default on an empty line."""
    while True:
        try:
            a = input(question).strip().lower()
        except EOFError:
            sys.exit("\nStopped: nothing was changed.")
        if not a and default:
            return default
        if a in answers:
            return a
        print("Please answer " + ", ".join(answers) + ".")


def uninstall_cmd(args):
    """Removes RedstonePanel after choosing what happens to every game server (1.23.2)."""
    from core import backups, uninstall
    choice = purge = None
    yes = False
    it = iter(args)
    for a in it:
        if a == "--servers":
            choice = next(it, None)
            if choice not in uninstall.CHOICES:
                sys.exit("--servers takes stop, keep or delete.")
        elif a == "--purge":
            purge = True
        elif a == "--yes":
            yes = True
        else:
            sys.exit(f"Unknown option {a}. See: redstonepanel help")
    info = uninstall.overview()
    found = info["servers"]
    print("Remove RedstonePanel\n")
    print("Uninstalling the panel while game servers are running or start at boot leaves them on this")
    print("machine with nothing to manage them: they keep holding their ports, with no backups,")
    print("restarts or updates. Choose what happens to them first.\n")
    print("Recommended order: 1. back up every server  2. choose what happens to them  3. remove the panel\n")
    if found:
        for s in found:
            state = ", ".join(w for w, on in (("running", s["running"]), ("starts at boot", s["enabled"])) if on) or "stopped"
            print(f"  {s['name']:<24} {s['edition'] or '':<8} port {s['port'] or '?':<6} {state:<26} "
                  f"last backup: {_when(s['last_backup']) if s['last_backup'] else 'never'}")
        print()
        if choice is None:
            if yes:
                sys.exit("Say what happens to the servers with --servers stop|keep|delete.")
            print("  1  Stop them and keep their files (recommended). Install the panel again and restore")
            print("     them from New server > Removed servers.")
            if info["keep_possible"]:
                print("  2  Keep them running without the panel.")
            print("  3  Delete them completely (after a backup; their backups are kept).")
            pick = _ask("What should happen to them? [1] ", ["1", "2", "3"] if info["keep_possible"] else ["1", "3"], "1")
            choice = {"1": "stop", "2": "keep", "3": "delete"}[pick]
        if choice == "delete":
            stale = [s["name"] for s in found if not s["last_backup"]
                     or time.time() - s["last_backup"] > uninstall.RECENT_BACKUP]
            if stale and (yes or _ask(f"Back up {', '.join(stale)} now? [Y/n] ", ["y", "n"], "y") == "y"):
                for name in stale:
                    print(f"Backing up {name}...")
                    try:
                        b = backups.run_backup(name, "manual")
                        print(f"  saved {backups.server_backup_dir(name)}/{b['file']}")
                    except backups.BackupError as e:
                        sys.exit(f"  {e}")
            confirm = uninstall.CONFIRM_WORD if yes else input(f"Type {uninstall.CONFIRM_WORD} to delete "
                                                               f"{', '.join(s['name'] for s in found)}: ").strip()
        else:
            confirm = None
        try:
            done = uninstall.apply({s["name"]: choice for s in found}, actor=_actor(), confirm=confirm)
        except uninstall.UninstallError as e:
            sys.exit(str(e))
        for what, label in (("stopped", "Stopped, files kept"), ("kept", "Left running without the panel"),
                            ("deleted", "Deleted")):
            if done[what]:
                print(f"{label}: {', '.join(done[what])}")
    else:
        print("There are no game servers on this machine.")
    if info["windows"]:
        print(f"\nNow remove the panel: {uninstall.remove_command()}")
        return
    if purge is None:
        purge = False if yes else _ask("Also delete the panel's accounts and settings (purge)? [y/N] ", ["y", "n"], "n") == "y"
    if not yes and _ask(f"Remove the panel now ({uninstall.remove_command(purge)})? [y/N] ", ["y", "n"], "n") != "y":
        print(f"\nRemove it when you're ready with: {uninstall.remove_command(purge)}")
        return
    sys.exit(uninstall.remove_package(purge))


def uninstall_hook_cmd():
    """Run by the package's prerm when the package is removed; must never fail the removal."""
    try:
        from core import uninstall
        uninstall.package_hook()
    except Exception as e:
        print(f"redstonepanel uninstall-hook: {e}", file=sys.stderr)


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
    elif cmd == "terminal" and rest in (["on"], ["off"]):
        terminal_cmd(rest[0])
    elif cmd == "status" and not rest:
        status()
    elif cmd == "reset-password":
        reset_password(rest)
    elif cmd == "token":
        token_cmd(rest)
    elif cmd == "boot" and len(rest) == 2 and rest[1] in ("on", "off"):
        boot_cmd(rest)
    elif cmd == "sleep" and (len(rest) == 2 and rest[1] in ("on", "off") or len(rest) == 3 and rest[1] == "on"):
        sleep_cmd(rest)
    elif cmd == "add-user":
        add_user(rest)
    elif cmd == "removed" and not rest:
        removed_cmd()
    elif cmd == "backup" and len(rest) == 1:
        backup_cmd(rest[0])
    elif cmd == "offsite-copy" and len(rest) == 1:
        offsite_copy_cmd(rest[0])
    elif cmd == "offsite-key" and not rest:
        offsite_key_cmd()
    elif cmd == "restart-server" and len(rest) == 1:
        restart_server_cmd(rest[0])
    elif cmd == "map-draw" and len(rest) in (1, 2) and (len(rest) == 1 or rest[1] == "--now"):
        map_draw_cmd(rest[0], now=len(rest) == 2)
    elif cmd == "check-updates" and not rest:
        check_updates_cmd()
    elif cmd == "ports" and not rest:
        ports_cmd()
    elif cmd == "notify-test" and not rest:
        notify_test_cmd()
    elif cmd == "sftp-serve" and not rest:
        from core import sftp
        sys.exit(sftp.serve())
    elif cmd == "notify-unit" and len(rest) == 2 and rest[0] in ("start", "stop"):
        notify_unit_cmd(*rest)
    elif cmd == "stop-unit" and len(rest) in (1, 2):
        stop_unit_cmd(*rest)
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
    elif cmd == "uninstall":
        uninstall_cmd(rest)
    elif cmd == "remove-package" and rest in ([], ["--purge"]):
        # The panel's Uninstall now button runs this in a unit of its own
        from core import uninstall
        sys.exit(uninstall.remove_package(purge=bool(rest)))
    elif cmd == "uninstall-hook" and not rest:
        uninstall_hook_cmd()
    elif cmd == "cluster-https" and not rest:
        cluster_https()
    elif cmd in ("-h", "--help", "help"):
        print(__doc__)
    else:
        sys.exit(__doc__)
