"""SFTP access to a server's files (1.18), for FileZilla, WinSCP and the like.

A small SFTP server built on paramiko (python3-paramiko, a Recommends), run as its own systemd
unit, redstonepanel-sftp.service, so a slow upload never holds up the panel and the panel can
restart without cutting a transfer. Off until an admin turns it on (Panel Settings > SFTP);
port 2022 by default.

  * Sign in as  ACCOUNT.SERVER  (alex.minecraft-2) with the panel password or an SSH key added
    under Password > SSH keys. Accounts are the panel's own: Linux accounts in the sudo or
    redstonepanel group, and panel accounts. An account with two-factor sign-in must use a key,
    since an SFTP program can't ask for a code.
  * A session sees one server's folder and nothing else. It needs the Files permission on that
    server (core/grants.py); admins reach every server. The account and its permission are
    checked again while the session is open, so removing either ends it within half a minute.
  * Every path goes through core/files.py: opened one component at a time with O_NOFOLLOW,
    symlinks never followed, hard-linked files refused, new files owned by the folder's owner
    (minecraft). No shell, no commands, no port forwarding: only the sftp subsystem.
  * Wrong passwords count towards the panel's sign-in limit (5 in 10 minutes per account and per
    address), shared with the browser sign-in.
  * Sign-ins, uploads, deletes, renames and new folders are in the panel log with who did them
    ([SFTP] alex uploaded minecraft-2/plugins/x.jar).

The panel process only reads the status and writes the unit (it never imports paramiko).
"""
import errno
import importlib.util
import os
import posixpath
import socket
import threading
import time

from core import auth, files, grants, manager, sshkeys
from core import settings as panel_settings
from core.settings import APP_DIR, DATA_DIR
from core.platform import paths, services, TimeoutExpired

UNIT = "redstonepanel-sftp.service"
UNIT_DIR = paths.UNIT_DIR
HOST_KEY = os.path.join(DATA_DIR, "sftp_host_key")
PACKAGE = "python3-paramiko"
SERVER_PREFIX = "minecraft"
RECHECK_SECONDS = 30            # how often an open session's account and permission are looked up
MAX_CONNECTIONS = 40
MAX_PER_ADDRESS = 10            # FileZilla opens several at once
MAX_CHANNELS = 10               # per connection
AUTH_SECONDS = 60               # a connection that hasn't signed in by then is dropped


class SftpError(ValueError):
    """Something about SFTP that can't be done (message is safe to show)."""


# ---------------------------------------------------------------------------
# What the panel shows and switches (no paramiko needed)
# ---------------------------------------------------------------------------
def available():
    return importlib.util.find_spec("paramiko") is not None


def _systemctl(*args):
    try:
        return services.systemctl(*args, timeout=30)
    except (OSError, TimeoutExpired):
        return None


def running():
    r = _systemctl("is-active", UNIT)
    return bool(r and r.stdout.strip() == "active")


def host_key_fingerprint():
    """SHA256 fingerprint of the SFTP host key, as SFTP programs show it on first connect."""
    try:
        with open(HOST_KEY + ".pub") as f:
            return sshkeys.parse(f.read())["fingerprint"]
    except (OSError, sshkeys.KeyError_):
        return None


def status(s=None):
    s = s or panel_settings.load()
    ok = available()
    return {"enabled": bool(s["sftp_enabled"]), "port": s["sftp_port"], "available": ok,
            "package": PACKAGE, "running": running() if s["sftp_enabled"] and ok else False,
            "host_key": host_key_fingerprint()}


def login_name(account, server):
    return f"{account}.{server}"


def parse_login(name):
    """'alex.minecraft-2' -> ('alex', 'minecraft-2'), or None. Server names always start with
    'minecraft', so an account name with dots in it still splits in one way."""
    name = name or ""
    i = name.rfind("." + SERVER_PREFIX)
    if i <= 0:
        return None
    account, server = name[:i], name[i + 1:]
    if not manager.SERVICE_NAME_RE.fullmatch(server) or "/" in account or len(account) > 64:
        return None
    return account, server


def _unit_text():
    return f"""[Unit]
Description=RedstonePanel SFTP - server files for panel accounts (Panel Settings > SFTP)
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
# Runs as root like the panel: it checks Linux passwords (PAM) and writes files as their owner.
ExecStart={paths.PYTHON} {APP_DIR}/run.py sftp-serve
Environment=PYTHONDONTWRITEBYTECODE=1
NoNewPrivileges=yes
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


def apply(new, old=None, actor="the panel"):
    """Makes the service match the settings: writes the unit, starts or stops it, opens or
    closes its port in ufw. Called when the setting changes and when the panel starts."""
    if not services.available():
        return
    path = os.path.join(UNIT_DIR, UNIT)
    port, old_port = new["sftp_port"], (old or {}).get("sftp_port")
    was_on = bool((old or {}).get("sftp_enabled"))
    if new["sftp_enabled"] and available():
        text = _unit_text()
        try:
            with open(path) as f:
                changed = f.read() != text
        except OSError:
            changed = True
        if changed:
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                f.write(text)
            os.replace(tmp, path)
            _systemctl("daemon-reload")
        if was_on and old_port and old_port != port:
            manager.close_firewall_rules({(str(old_port), "tcp")})
        manager.open_firewall(port, "tcp")
        _systemctl("enable", UNIT)
        # A changed port or setting needs a restart; otherwise leave running transfers alone
        _systemctl("restart" if old is not None or changed else "start", UNIT)
        if old is not None:
            manager.write_log(f"[SFTP] {actor} turned SFTP on, port {port}.")
    else:
        if os.path.exists(path):
            _systemctl("disable", "--now", UNIT)
            try:
                os.unlink(path)
            except OSError:
                pass
            _systemctl("daemon-reload")
        if was_on:
            manager.close_firewall_rules({(str(old_port or port), "tcp")})
            manager.write_log(f"[SFTP] {actor} turned SFTP off.")


def firewall_rules():
    """The ufw rule SFTP needs while it's on (so the dashboard doesn't call it a leftover)."""
    s = panel_settings.load()
    return {(str(s["sftp_port"]), "tcp")} if s["sftp_enabled"] else set()


def check_can_enable(s, old):
    """Raises SftpError if SFTP can't be turned on (or moved) with these settings."""
    if not s["sftp_enabled"]:
        return
    if not available():
        raise SftpError(f"SFTP needs the {PACKAGE} package. Install it with: sudo apt install {PACKAGE}")
    moving = not old.get("sftp_enabled") or old.get("sftp_port") != s["sftp_port"] or not running()
    if moving and not panel_settings.port_free(s["sftp_port"]):
        raise SftpError(f"Port {s['sftp_port']} is already in use on this server.")


# ---------------------------------------------------------------------------
# Signing in
# ---------------------------------------------------------------------------
class Session:
    """Who signed in and to which server; looked up again every RECHECK_SECONDS."""

    def __init__(self, source, account, server, ip):
        self.source, self.account, self.server, self.ip = source, account, server, ip
        self.checked = 0.0
        self.ok = False

    def user(self):
        role = auth.account_role(self.source, self.account)
        return {"username": self.account, "source": self.source, "role": role} if role else None

    def allowed(self):
        now = time.monotonic()
        if now - self.checked >= RECHECK_SECONDS or not self.ok:
            self.checked = now
            user = self.user()
            self.ok = bool(user) and grants.can(user, "files", self.server)
            if self.ok:
                try:
                    manager._check_server(self.server)
                except manager.ServerError:
                    self.ok = False
        return self.ok


def authorize(source, account, server, ip, how):
    """A Session if this (already proven) account may use `server`'s files, else None."""
    sess = Session(source, account, server, ip)
    if sess.allowed():
        manager.write_log(f"[SFTP] {account} signed in to {server} from {ip} ({how}).")
        return sess
    manager.write_log(f"[SFTP] Refused {account} from {ip}: no Files permission on {server}, or no such server.")
    return None


def password_sign_in(login, password, ip):
    """A Session, or None. Wrong passwords count towards the panel's sign-in limit."""
    parsed = parse_login(login)
    if not parsed:
        auth._record_fail((f"ip:{ip}",))
        manager.write_log(f"[SFTP] Refused a sign-in from {ip}: the user name must be ACCOUNT.SERVER.")
        return None
    account, server = parsed
    try:
        identity = auth.login(account, password, ip)
    except auth.AuthError as e:
        manager.write_log(f"[SFTP] Failed sign-in for {account} from {ip}: {e}")
        return None
    if identity.get("pending"):
        manager.write_log(f"[SFTP] Refused {account} from {ip}: the account has two-factor sign-in, so SFTP "
                          "needs an SSH key (Password > SSH keys).")
        return None
    return authorize(identity["src"], identity["u"], server, ip, "password")


def key_account(login, key_type, blob, ip):
    """(source, account, server) if this public key belongs to the account in the login name."""
    parsed = parse_login(login)
    if not parsed:
        return None
    account, server = parsed
    if auth._throttled(auth.fail_keys(account, ip)):
        return None
    found = auth.find_account(account)
    if not found or not auth.account_role(*found):
        return None
    if not sshkeys.matches(sshkeys.account_key(*found), key_type, blob):
        return None
    return found[0], found[1], server


# ---------------------------------------------------------------------------
# The server (paramiko is imported only here)
# ---------------------------------------------------------------------------
def ensure_host_key():
    """The service's host key (ECDSA P-256), made once and kept, so SFTP programs don't warn
    after an update."""
    import paramiko
    if not os.path.exists(HOST_KEY):
        os.makedirs(DATA_DIR, exist_ok=True)
        key = paramiko.ECDSAKey.generate(bits=256)
        tmp = HOST_KEY + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.close(fd)
        key.write_private_key_file(tmp)
        os.chmod(tmp, 0o600)
        os.replace(tmp, HOST_KEY)
    key = paramiko.ECDSAKey.from_private_key_file(HOST_KEY)
    pub = f"{key.get_name()} {key.get_base64()} redstonepanel-sftp\n"
    try:
        with open(HOST_KEY + ".pub") as f:
            same = f.read() == pub
    except OSError:
        same = False
    if not same:
        with open(HOST_KEY + ".pub", "w") as f:
            f.write(pub)
    return key


def _classes():
    import paramiko
    from paramiko import SFTPAttributes, SFTPHandle, SFTPServer, SFTPServerInterface
    from paramiko.sftp import SFTP_FAILURE, SFTP_NO_SUCH_FILE, SFTP_OK, SFTP_OP_UNSUPPORTED, SFTP_PERMISSION_DENIED

    def fail(e):
        if isinstance(e, files.FileError):
            return SFTP_NO_SUCH_FILE if e.status == 404 else SFTP_FAILURE
        if isinstance(e, manager.ServerError):
            return SFTP_NO_SUCH_FILE
        if isinstance(e, OSError) and e.errno:
            return SFTPServer.convert_errno(e.errno)
        return SFTP_FAILURE

    def guarded(fn):
        """Every operation: the session must still be allowed, and errors become SFTP codes."""
        def wrapper(self, *args, **kw):
            if not self.session.allowed():
                return SFTP_PERMISSION_DENIED
            try:
                return fn(self, *args, **kw)
            except (files.FileError, manager.ServerError, OSError) as e:
                return fail(e)
        return wrapper

    def rel(path):
        """An SFTP path ('/plugins/../x') as a path inside the server folder ('x')."""
        return posixpath.normpath("/" + (path or "")).lstrip("/")

    class Handle(SFTPHandle):
        def __init__(self, session, path, fd, flags, created):
            super().__init__(flags)
            self.session, self.path, self.fd, self.created = session, path, fd, created
            self.wrote = False

        def read(self, offset, length):
            if not self.session.allowed():
                return SFTP_PERMISSION_DENIED
            try:
                return os.pread(self.fd, min(length, 1 << 20), offset)
            except OSError as e:
                return fail(e)

        def write(self, offset, data):
            if not self.session.allowed():
                return SFTP_PERMISSION_DENIED
            try:
                view = memoryview(data)
                while view:
                    n = os.pwrite(self.fd, view, offset)
                    view, offset = view[n:], offset + n
                self.wrote = True
                return SFTP_OK
            except OSError as e:
                return fail(e)

        def stat(self):
            try:
                return SFTPAttributes.from_stat(os.fstat(self.fd))
            except OSError as e:
                return fail(e)

        def chattr(self, attr):
            # Times are kept (uploads keep their dates); owner and mode stay the folder's
            try:
                if attr.st_mtime is not None:
                    os.utime(self.fd, (attr.st_atime or attr.st_mtime, attr.st_mtime))
                if attr.st_size is not None:
                    os.ftruncate(self.fd, attr.st_size)
                    self.wrote = True
                return SFTP_OK
            except OSError as e:
                return fail(e)

        def close(self):
            fd, self.fd = self.fd, None
            if fd is None:
                return
            try:
                size = os.fstat(fd).st_size
            except OSError:
                size = 0
            os.close(fd)
            if self.wrote or self.created:
                s = self.session
                manager.write_log(f"[SFTP] {s.account} uploaded {s.server}/{self.path} ({size} bytes).")

    class Files(SFTPServerInterface):
        def __init__(self, server, *args, **kwargs):
            super().__init__(server, *args, **kwargs)
            self.session = server.session

        @guarded
        def list_folder(self, path):
            out = []
            for name, st in files.scan(self.session.server, rel(path)):
                out.append(SFTPAttributes.from_stat(st, name))
            return out

        @guarded
        def stat(self, path):
            # Symlinks are never followed here: a link shows as a link
            return SFTPAttributes.from_stat(files.stat_path(self.session.server, rel(path)))

        lstat = stat

        @guarded
        def open(self, path, flags, attr):
            p = rel(path)
            fd, created = files.open_fd(self.session.server, p, flags)
            return Handle(self.session, p, fd, flags, created)

        @guarded
        def remove(self, path):
            s = self.session
            files.remove_file(s.server, rel(path), actor=s.account, via="SFTP")
            return SFTP_OK

        @guarded
        def rename(self, oldpath, newpath):
            s = self.session
            files.move(s.server, rel(oldpath), rel(newpath), actor=s.account, via="SFTP")
            return SFTP_OK

        @guarded
        def posix_rename(self, oldpath, newpath):
            s = self.session
            files.move(s.server, rel(oldpath), rel(newpath), actor=s.account, via="SFTP", replace=True)
            return SFTP_OK

        @guarded
        def mkdir(self, path, attr):
            s = self.session
            try:
                files.make_dir(s.server, rel(path), actor=s.account, via="SFTP")
            except files.FileError as e:
                if e.status == 409:
                    return SFTPServer.convert_errno(errno.EEXIST)
                raise
            return SFTP_OK

        @guarded
        def rmdir(self, path):
            s = self.session
            files.remove_empty_dir(s.server, rel(path), actor=s.account, via="SFTP")
            return SFTP_OK

        @guarded
        def chattr(self, path, attr):
            if attr.st_mtime is not None:
                files.set_mtime(self.session.server, rel(path), attr.st_mtime, attr.st_atime)
            else:
                files.stat_path(self.session.server, rel(path))
            return SFTP_OK

        def canonicalize(self, path):
            return posixpath.normpath("/" + (path or ""))

        def readlink(self, path):
            return SFTP_OP_UNSUPPORTED

        def symlink(self, target_path, path):
            return SFTP_PERMISSION_DENIED

    class Gate(paramiko.ServerInterface):
        """One connection's sign-in. Only the sftp subsystem is ever offered."""

        def __init__(self, ip, transport):
            self.ip, self.transport = ip, transport
            self.session = None
            self.key_login = False
            self.channels = 0

        def get_allowed_auths(self, username):
            return "publickey,password"

        def check_auth_password(self, username, password):
            self.session = password_sign_in(username, password, self.ip)
            return paramiko.AUTH_SUCCESSFUL if self.session else paramiko.AUTH_FAILED

        def check_auth_publickey(self, username, key):
            who = key_account(username, key.get_name(), key.asbytes(), self.ip)
            if not who:
                return paramiko.AUTH_FAILED
            # paramiko asks twice: once to see if the key is known, then with the signature it
            # verifies itself. The session is only used after that.
            sess = Session(who[0], who[1], who[2], self.ip)
            if not sess.allowed():
                return paramiko.AUTH_FAILED
            self.session, self.key_login = sess, True
            return paramiko.AUTH_SUCCESSFUL

        def check_channel_request(self, kind, chanid):
            if kind == "session" and self.session and self.transport.is_authenticated() \
                    and self.channels < MAX_CHANNELS:
                self.channels += 1
                return paramiko.OPEN_SUCCEEDED
            return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

        # No shell, commands, terminals or forwarding
        def check_channel_shell_request(self, channel):
            return False

        def check_channel_exec_request(self, channel, command):
            return False

        def check_channel_pty_request(self, *a, **kw):
            return False

        def check_port_forward_request(self, address, port):
            return False

        def check_channel_direct_tcpip_request(self, chanid, origin, destination):
            return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    return paramiko, SFTPServer, Files, Gate


_conn_lock = threading.Lock()
_conns = {}                     # address -> open connections


def _handle(sock, ip, host_key, classes):
    paramiko, SFTPServer, Files, Gate = classes
    t = None
    try:
        t = paramiko.Transport(sock)
        t.local_version = "SSH-2.0-RedstonePanel"
        t.add_server_key(host_key)
        t.set_subsystem_handler("sftp", SFTPServer, Files)
        t.banner_timeout = t.handshake_timeout = 30
        t.auth_timeout = AUTH_SECONDS
        gate = Gate(ip, t)
        t.start_server(server=gate)
        deadline = time.monotonic() + AUTH_SECONDS
        while t.is_active() and not t.is_authenticated():
            if time.monotonic() > deadline:
                return
            time.sleep(0.05)
        if not t.is_active():
            return
        if gate.key_login and gate.session:
            manager.write_log(f"[SFTP] {gate.session.account} signed in to {gate.session.server} from {ip} "
                              "(SSH key).")
        t.set_keepalive(60)
        while t.is_active():
            time.sleep(0.5)
            if gate.session and not gate.session.allowed():
                manager.write_log(f"[SFTP] Ended {gate.session.account}'s session on {gate.session.server}: "
                                  "the account or its Files permission was removed.")
                break
    except Exception as e:                   # one bad connection never stops the service
        if not isinstance(e, (EOFError, OSError)) and e.__class__.__name__ != "SSHException":
            manager.write_log(f"[SFTP] Connection from {ip} ended with an error: {e.__class__.__name__}: {e}")
    finally:
        try:
            if t is not None:
                t.close()
            else:
                sock.close()
        except Exception:
            pass
        with _conn_lock:
            _conns[ip] = _conns.get(ip, 1) - 1
            if _conns[ip] <= 0:
                _conns.pop(ip, None)


def _listen(port):
    try:
        sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("::", port))
    except OSError:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
    sock.listen(64)
    return sock


def serve(port=None, ready=None):
    """Runs the SFTP server until the process is stopped (redstonepanel-sftp.service)."""
    s = panel_settings.load()
    if port is None:
        if not s["sftp_enabled"]:
            print("SFTP is off (Panel Settings > SFTP).")
            return 0
        port = s["sftp_port"]
    if not available():
        print(f"SFTP needs the {PACKAGE} package: sudo apt install {PACKAGE}")
        return 1
    import logging
    logging.getLogger("paramiko").setLevel(logging.CRITICAL)     # scanners make it noisy
    classes = _classes()
    host_key = ensure_host_key()
    sock = _listen(port)
    manager.write_log(f"[SFTP] Listening on port {sock.getsockname()[1]}.")
    if ready:
        ready(sock)
    while True:
        try:
            conn, addr = sock.accept()
        except OSError:
            return 0                        # the socket was closed: stopping
        ip = addr[0]
        if ip.startswith("::ffff:"):
            ip = ip[7:]
        with _conn_lock:
            if sum(_conns.values()) >= MAX_CONNECTIONS or _conns.get(ip, 0) >= MAX_PER_ADDRESS:
                conn.close()
                continue
            _conns[ip] = _conns.get(ip, 0) + 1
        conn.settimeout(None)
        threading.Thread(target=_handle, args=(conn, ip, host_key, classes), daemon=True).start()
