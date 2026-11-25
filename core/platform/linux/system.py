"""This machine on Linux: its addresses, accounts, file ownership and locks, and the files the
dashboard reads (/proc)."""
import contextlib
import contextvars
import ctypes
import ctypes.util
import fcntl
import grp
import os
import pwd
import subprocess
import threading

from core.platform import paths
from core.platform.linux._run import logged, quiet, _opt

GAME_ACCOUNT = "minecraft"          # game servers run as this account and own their folders


def addresses(timeout=None):
    """This machine's IP addresses, as `hostname -I` lists them."""
    return quiet(["hostname", "-I"], **_opt(timeout=timeout)).stdout.split()


def interfaces_json(timeout=10):
    """`ip -j -4 addr show` as text (JSON): each interface's IPv4 addresses and prefix lengths."""
    return quiet(["ip", "-j", "-4", "addr", "show"], timeout=timeout).stdout


def whoami():
    """Who the panel runs as (`id`), for the chat troubleshooting view."""
    return quiet(["id"]).stdout.strip()


def account_exists(name=GAME_ACCOUNT):
    try:
        pwd.getpwnam(name)
        return True
    except KeyError:
        return False


def ensure_game_account(home):
    """The package creates the `minecraft` system account; older and manual installs get it on
    their first deploy."""
    if not account_exists(GAME_ACCOUNT):
        logged(["useradd", "-r", "-m", "-U", "-d", home, "-s", paths.LOGIN_SHELL, GAME_ACCOUNT])


def give_to_game_account(path):
    """Hands a folder and everything in it to the game account. -h: a link in it changes
    owner itself; what it points to never does."""
    return logged(["chown", "-R", "-h", f"{GAME_ACCOUNT}:{GAME_ACCOUNT}", path])


def java_version_text(java="java"):
    """What `java -version` prints (on stderr)."""
    return subprocess.run([java, "-version"], capture_output=True, text=True).stderr


def delete_tree(path):
    """Deletes a folder and everything in it (written to the activity log)."""
    return logged(["rm", "-rf", path])


def make_executable(path):
    """chmod +x, on the file itself: a link there is refused, not followed."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            os.fchmod(fd, os.fstat(fd).st_mode & 0o7777 | 0o111)
        finally:
            os.close(fd)
    except OSError:
        return 1
    return 0


def tail(path, lines=3000):
    """The last lines of a file the game account owns. Returns (text, error). Read as the game
    account, so a log replaced by a link to a root-only file shows nothing."""
    r = quiet(as_game_account(["tail", "-n", str(lines), "--", path]))
    if r.returncode == 0:
        return r.stdout, ""
    return "", (r.stderr or "").strip()


def is_admin():
    """True when the panel runs with full rights (root), as an installed panel does."""
    return os.geteuid() == 0


def as_game_account(cmd):
    """cmd, changed so that it runs as the game account and not as the panel (root)."""
    if not is_admin():
        return cmd
    return ["runuser", "-u", GAME_ACCOUNT, "--"] + list(cmd)


# ---------------------------------------------------------------------------
# Touching files in a game server's folder
#
# The game account owns its servers' folders, and the code it runs (the server, its plugins
# and mods) can put a link anywhere in them: server.properties -> /etc/shadow. The panel runs
# as root, so a plain open() there would follow the link with root's rights. Inside
# game_files() this thread uses the game account's rights for every file operation instead
# (Linux's per-thread filesystem user and group, setfsuid/setfsgid), so a link only reaches
# what the game account could reach anyway, and new files belong to it. Other threads and
# everything else about the process stay root.
# ---------------------------------------------------------------------------
_fs = threading.local()


def _libc():
    lib = getattr(_libc, "lib", None)
    if lib is None:
        lib = _libc.lib = ctypes.CDLL(None, use_errno=True)
    return lib


def _set_fs_ids(uid, gid):
    lib = _libc()
    # Group first, while this thread may still change it; each call returns the previous id,
    # and asking for -1 (invalid) only reports the current one
    lib.setfsgid(gid)
    lib.setfsuid(uid)
    if lib.setfsuid(-1) != uid or lib.setfsgid(-1) != gid:
        raise OSError(f"Couldn't switch file access to uid {uid}, gid {gid}.")


@contextlib.contextmanager
def game_files(as_owner_of=None):
    """with game_files(): ... touches files with the game account's rights, not root's.
    as_owner_of=folder: with the rights of whoever owns that folder instead (a server the panel
    doesn't manage yet, found on this machine); nothing changes for a folder root owns.
    Nests; does nothing when the panel doesn't run as root or there is no game account yet
    (then there are no game server folders either)."""
    if getattr(_fs, "uid", None) is not None or os.geteuid() != 0:
        yield                 # already switched, or nothing to switch from
        return
    if as_owner_of is not None:
        try:
            st = os.lstat(as_owner_of)
        except OSError:
            st = None
        pw = pwd.struct_passwd(("", "", st.st_uid, st.st_gid, "", "", "")) if st and st.st_uid else None
    else:
        pw = _account_entry(_server_account.get())
    if pw is None:
        yield
        return
    if os.getgroups():
        # The root group (and any other) would still count for file access; root itself never
        # needs them
        os.setgroups([])
    _set_fs_ids(pw.pw_uid, pw.pw_gid)
    _fs.uid, _fs.gid = pw.pw_uid, pw.pw_gid
    try:
        yield
    finally:
        _fs.uid = _fs.gid = None
        _set_fs_ids(0, 0)


# The account the game server a request is about runs as (its unit's User=), set by the panel
# for that request; None: not known, use the game account
_server_account = contextvars.ContextVar("rp_server_account", default=None)


def _account_entry(name):
    """The passwd entry game_files() switches to: the server's own account, or the game account
    when that isn't known. None (stay root) for a server that runs as root: it has root's rights
    itself, so nothing it leaves in its folder can reach more than it already could."""
    if name is None:
        name = GAME_ACCOUNT
    if name in ("", "root", "0"):
        return None
    for n in (name, GAME_ACCOUNT):
        try:
            pw = pwd.getpwnam(n)
        except KeyError:
            continue
        return None if pw.pw_uid == 0 else pw
    return None


@contextlib.contextmanager
def server_account(user):
    """Inside: game_files() works as `user`, the account a game server runs as ("" or "root": as
    root; None: the game account). The panel sets it for each request about one server."""
    token = _server_account.set(user)
    try:
        yield
    finally:
        _server_account.reset(token)


@contextlib.contextmanager
def panel_files():
    """Inside game_files(): back to the panel's own rights for a moment, to write its own log or
    data. Does nothing outside game_files()."""
    uid, gid = getattr(_fs, "uid", None), getattr(_fs, "gid", None)
    if uid is None:
        yield
        return
    _set_fs_ids(0, 0)
    _fs.uid = _fs.gid = None
    try:
        yield
    finally:
        _set_fs_ids(uid, gid)
        _fs.uid, _fs.gid = uid, gid


def take_for_game_account(path):
    """give_to_game_account() without the activity log."""
    return quiet(["chown", "-R", "-h", f"{GAME_ACCOUNT}:{GAME_ACCOUNT}", path])


def run_program(cmd, cwd=None, env=None, timeout=None):
    """Runs a program the panel was handed (a mod loader's installer under Java) and returns the
    CompletedProcess, output as text. Raises TimeoutExpired and OSError."""
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, errors="replace", timeout=timeout)


def copy_into(source, dest, wrap=None):
    """Copies everything in folder `source` over folder `dest`, replacing files that are there
    and never following links. wrap changes the command (as_game_account). CompletedProcess."""
    cmd = ["cp", "-r", "--remove-destination", "--no-dereference", "--", source + "/.", dest + "/"]
    return quiet(wrap(cmd) if wrap else cmd)


def remove_files(folder, names, wrap=None):
    """Deletes files (paths relative to folder); ones that aren't there are skipped."""
    cmd = ["rm", "-f", "--"] + list(names)
    return quiet(wrap(cmd) if wrap else cmd, cwd=folder)


def can_run_as_game_account():
    """True: a program can be started as the game account (runuser), so what a mod loader's
    installer does is confined to what that account may touch."""
    return True


# ---------------------------------------------------------------------------
# Locks
# ---------------------------------------------------------------------------
def lock(fd, wait=False):
    """Takes the lock on an open file (one holder at a time, released when the file is closed
    or its process ends). wait=False raises BlockingIOError when someone else holds it."""
    fcntl.flock(fd, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))


def unlock(fd):
    fcntl.flock(fd, fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# System accounts (sign-in): PAM through ctypes, no extra packages
# ---------------------------------------------------------------------------
class _PamHandle(ctypes.Structure):
    _fields_ = [("handle", ctypes.c_void_p)]

class _PamMessage(ctypes.Structure):
    _fields_ = [("msg_style", ctypes.c_int), ("msg", ctypes.c_char_p)]

class _PamResponse(ctypes.Structure):
    _fields_ = [("resp", ctypes.c_void_p), ("resp_retcode", ctypes.c_int)]

_CONV_FUNC = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int,
                              ctypes.POINTER(ctypes.POINTER(_PamMessage)),
                              ctypes.POINTER(ctypes.POINTER(_PamResponse)), ctypes.c_void_p)

class _PamConv(ctypes.Structure):
    _fields_ = [("conv", _CONV_FUNC), ("appdata_ptr", ctypes.c_void_p)]

_PAM_PROMPT_ECHO_OFF, _PAM_PROMPT_ECHO_ON = 1, 2
# Its own name: _libc above is the file-access helper
_libpam = _pam_libc = None

def _load_pam():
    global _libpam, _pam_libc
    if _libpam is None:
        _libpam = ctypes.CDLL(ctypes.util.find_library("pam") or "libpam.so.0")
        _pam_libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6")
        _pam_libc.calloc.restype = ctypes.c_void_p
        _pam_libc.calloc.argtypes = [ctypes.c_size_t, ctypes.c_size_t]
        _pam_libc.strdup.restype = ctypes.c_void_p
        _pam_libc.strdup.argtypes = [ctypes.c_char_p]
        _libpam.pam_start.restype = ctypes.c_int
        _libpam.pam_start.argtypes = [ctypes.c_char_p, ctypes.c_char_p,
                                      ctypes.POINTER(_PamConv), ctypes.POINTER(_PamHandle)]
        for fn in (_libpam.pam_authenticate, _libpam.pam_acct_mgmt, _libpam.pam_end):
            fn.restype = ctypes.c_int
            fn.argtypes = [_PamHandle, ctypes.c_int]

def check_system_password(username, password):
    """True if PAM accepts the password and the account is usable (not locked/expired)."""
    _load_pam()
    pw = password.encode()

    @_CONV_FUNC
    def conv(n, messages, p_response, _appdata):
        # PAM frees these with free(), so they must come from calloc/strdup
        addr = _pam_libc.calloc(n, ctypes.sizeof(_PamResponse))
        if not addr:
            return 5   # PAM_BUF_ERR
        resp = ctypes.cast(addr, ctypes.POINTER(_PamResponse))
        p_response[0] = resp
        for i in range(n):
            if messages[i].contents.msg_style in (_PAM_PROMPT_ECHO_OFF, _PAM_PROMPT_ECHO_ON):
                resp[i].resp = _pam_libc.strdup(pw)
                resp[i].resp_retcode = 0
        return 0

    handle = _PamHandle()
    conv_struct = _PamConv(conv, None)
    rc = _libpam.pam_start(PAM_SERVICE.encode(), username.encode(), ctypes.byref(conv_struct), ctypes.byref(handle))
    if rc != 0:
        return False
    try:
        rc = _libpam.pam_authenticate(handle, 0)
        if rc == 0:
            rc = _libpam.pam_acct_mgmt(handle, 0)
        return rc == 0
    finally:
        _libpam.pam_end(handle, rc)


PAM_SERVICE = "login"
SYSTEM_ACCOUNTS = True              # accounts of this machine can sign in to the panel
ACCOUNT_KIND = "Linux"


def canonical_account(username):
    """The one spelling of an account (Windows has several); on Linux the name itself, if it exists."""
    return username if system_account_exists(username) else None


def system_account_exists(username):
    try:
        pwd.getpwnam(username)
        return True
    except KeyError:
        return False


def system_account_allowed(username, groups=("sudo",)):
    """The account exists and is in one of the groups that may sign in to the panel."""
    try:
        pw = pwd.getpwnam(username)
    except KeyError:
        return False
    gids = set(os.getgrouplist(username, pw.pw_gid))
    for name in groups:
        try:
            if grp.getgrnam(name).gr_gid in gids:
                return True
        except KeyError:
            continue
    return False


# ---------------------------------------------------------------------------
# The machine's numbers
# ---------------------------------------------------------------------------
def machine_file(name, root=None):
    """The text of a file the dashboard reads: "meminfo", "stat", "net/dev", "cpuinfo",
    "uptime", "self/mounts" under /proc (root: another folder, for tests), and "os-release"."""
    path = paths.OS_RELEASE if name == "os-release" and root is None else os.path.join(root or paths.PROC, name)
    try:
        with open(path, errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def kernel():
    return os.uname().release


def load_average():
    """[1, 5, 15 minute] load averages, or None."""
    try:
        return [round(x, 2) for x in os.getloadavg()]
    except OSError:
        return None


def filesystem(path):
    """{"device", "mount", "fstype", "total", "used", "free"} of the file system holding path."""
    dev, st = os.stat(path).st_dev, os.statvfs(path)
    mounts = []
    for line in machine_file("self/mounts").splitlines():
        f = line.split()
        if len(f) >= 3:
            mounts.append((f[1].replace("\\040", " "), f[2]))
    mount = max((m for m in mounts if path == m[0] or path.startswith(m[0].rstrip("/") + "/")),
                key=lambda m: len(m[0]), default=("/", None))
    return {"device": dev, "mount": mount[0], "fstype": mount[1], "total": st.f_blocks * st.f_frsize,
            "used": (st.f_blocks - st.f_bfree) * st.f_frsize, "free": st.f_bavail * st.f_frsize}


def root_path():
    return "/"


def process_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def secure_folder(path):
    """Leaves a folder to its owner (the panel, root): mode 0700."""
    os.chmod(path, 0o700)
    return 0


def fs():
    """The module the file manager uses for files: Python's own `os`, which on Linux can open a
    name relative to an open folder without following a link (dir_fd, O_NOFOLLOW)."""
    return os


def default_gateway():
    """The IPv4 default gateway from /proc/net/route, or None."""
    import socket
    import struct
    try:
        with open(os.path.join(paths.PROC, "net/route")) as f:
            for line in f.readlines()[1:]:
                p = line.split()
                if len(p) > 3 and p[1] == "00000000" and int(p[3], 16) & 2:
                    return socket.inet_ntoa(struct.pack("<L", int(p[2], 16)))
    except (OSError, ValueError):
        pass
    return None


def reserved_ports():
    """Port ranges the system keeps for itself, [(proto, start, end)]: none on Linux (Windows:
    Hyper-V, WSL and Docker Desktop reserve some)."""
    return []


def machine_notes():
    """Needs attention notes about the machine itself (Windows: sleep, a pending Windows Update restart)."""
    return []


def hint(what, *args):
    """A sentence telling an administrator what to type or do, in this system's terms."""
    if what == "ssh-client":
        return "Install it with: sudo apt install openssh-client rsync"
    if what == "restart-panel":
        return "Restart it with: sudo systemctl restart redstonepanel"
    if what == "install-tools":
        return f"Missing {', '.join(args)}. Install with: sudo apt install {' '.join(args)}"
    if what == "allow-port":
        return f"sudo ufw allow {args[0]}/{args[1]}"
    if what == "service-manager":
        return "systemd starts it again"
    if what == "firewall-name":
        return "ufw"
    if what == "allow-ports":                       # args: "25565/tcp", ...
        return " && ".join(f"sudo ufw allow {a}" for a in args)
    if what == "close-ports":
        return " && ".join(f"sudo ufw delete allow {a}" for a in args)
    if what == "install-package":                   # "" where there is nothing to type
        return f"sudo apt install {' '.join(args)}"


# Folders the panel won't keep game servers in: the system's own, and its own data
_NOT_FOR_SERVERS = ("/etc", "/proc", "/sys", "/dev", "/boot", "/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64",
                    "/run", "/root", "/tmp", "/var/lib", "/var/log", "/var/run", "/var/tmp", "/var/cache", "/var/backups", "/snap")
_NOT_ALONE = ("/", "/home", "/var", "/mnt", "/media", "/srv")      # fine above a servers folder, not as one


def check_servers_folder(folder):
    """(the folder as it will be stored, "") when game servers can be kept there, else
    (None, why not). Nothing is created: prepare_servers_folder() does that."""
    import os
    import re
    folder = os.path.normpath(folder)
    if not folder.startswith("/"):
        return None, "Give the full path of a folder, for example /srv/minecraft."
    if not re.fullmatch(r"/[A-Za-z0-9._/-]+", folder) and folder != "/":
        return None, "The folder's name can only have letters, digits, dots, dashes and underscores (no spaces)."
    real = os.path.realpath(folder)
    for path in {folder, real}:
        if path in _NOT_ALONE:
            return None, f"{path} holds other things. Give a folder of its own inside it, for example {path.rstrip('/')}/minecraft."
        if any(path == bad or path.startswith(bad + "/") for bad in _NOT_FOR_SERVERS + (paths.BACKUP_ROOT, paths.DATA_DIR)):
            return None, f"{path} belongs to the system or to the panel itself; choose a folder somewhere else."
    if os.path.lexists(real) and not os.path.isdir(real):
        return None, f"{real} is a file, not a folder."
    # The nearest folder that exists says what disk this is
    there = real
    while not os.path.isdir(there):
        there = os.path.dirname(there)
    try:
        flags = os.statvfs(there).f_flag
    except OSError as e:
        return None, f"Can't look at {there}: {e.strerror or e}."
    if flags & os.ST_RDONLY:
        return None, f"{there} is on a read-only disk."
    if flags & getattr(os, "ST_NOEXEC", 8):
        return None, (f"{there} is on a disk mounted with noexec, and a Bedrock server is a program that has to "
                      f"run from its own folder. Mount it without noexec, or choose another folder.")
    return real, ""


def prepare_servers_folder(folder):
    """Makes the folder (and those above it) and checks the game servers' account can get in.
    Returns "" or why it couldn't."""
    import os
    try:
        os.makedirs(folder, mode=0o755, exist_ok=True)
    except OSError as e:
        return f"Couldn't make {folder}: {e.strerror or e}."
    r = quiet(as_game_account(["test", "-x", folder]), timeout=20)
    if r.returncode != 0:
        return (f"The {GAME_ACCOUNT} account, which runs the game servers, can't get into {folder}. "
                f"A folder above it is closed to other accounts: run  sudo chmod o+x  on it, or choose another folder.")
    return ""


def servers_drives():
    """The drives a servers folder can be on, for a list to choose from: none here, where a
    disk is a folder like any other and the owner types its path."""
    return []


def adopt_folder(path):
    """Hands a folder that somebody else filled to the server that now runs from it, down to
    the last file. Here that is what give_to_game_account() does."""
    return give_to_game_account(path)


def found_folder_problem(folder):
    """"" when the panel can run a server somebody else set up from the folder it is in, else
    why not. Changes nothing."""
    import os
    import re
    if not re.fullmatch(r"/[A-Za-z0-9._/-]+", folder or ""):
        return (f"The folder's path, {folder}, has a space or another character the panel can't put in a "
                f"service. Rename the folder (with the server stopped), start it again, and it can be managed.")
    above = os.path.dirname(os.path.realpath(folder))
    r = quiet(as_game_account(["test", "-x", above]), timeout=20)
    if r.returncode != 0:
        return (f"The {GAME_ACCOUNT} account, which runs the game servers, can't get into {above}. "
                f"A folder above it is closed to other accounts: run  sudo chmod o+x  on it, or move the server.")
    return ""


def game_programs(names=("java", "bedrock_server")):
    """Running programs that could be game servers: [{"pid", "exe", "argv", "cwd", "user",
    "service", "started"}] for every process whose program is called one of `names`.
    "service" is the systemd unit that started it ("" when none did: a terminal, screen, cron).
    Read from /proc; a process that ends meanwhile, or can't be read, is left out."""
    import os
    import pwd
    found = []
    try:
        pids = [n for n in os.listdir(paths.PROC) if n.isdigit()]
    except OSError:
        return found
    try:
        with open(os.path.join(paths.PROC, "uptime")) as f:
            boot = __import__("time").time() - float(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        boot = None
    for pid in pids:
        base = os.path.join(paths.PROC, pid)
        try:
            with open(os.path.join(base, "cmdline"), "rb") as f:
                argv = [a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a]
            if not argv:
                continue
            try:
                exe = os.readlink(os.path.join(base, "exe"))
            except OSError:
                exe = argv[0]
            if os.path.basename(exe.replace(" (deleted)", "")) not in names and os.path.basename(argv[0]) not in names:
                continue
            cwd = os.readlink(os.path.join(base, "cwd"))
            st = os.stat(base)
            try:
                user = pwd.getpwuid(st.st_uid).pw_name
            except KeyError:
                user = str(st.st_uid)
            service = ""
            with open(os.path.join(base, "cgroup")) as f:
                for line in f:
                    last = line.strip().rsplit("/", 1)[-1]
                    if last.endswith(".service"):
                        service = last
                        break
            started = None
            if boot is not None:
                with open(os.path.join(base, "stat")) as f:
                    ticks = int(f.read().rsplit(")", 1)[1].split()[19])
                started = boot + ticks / os.sysconf("SC_CLK_TCK")
        except (OSError, ValueError, IndexError):
            continue
        found.append({"pid": int(pid), "exe": exe, "argv": argv, "cwd": cwd, "user": user,
                      "service": service, "started": started})
    return found


def other_game_services():
    """Services the panel didn't make that are named for Minecraft: [{"service", "running",
    "folder", "program"}], from the unit files in the unit folder whose name or command says
    minecraft or bedrock and whose name isn't one of the panel's (minecraft-NAME.service).
    Looking only."""
    import os
    import re
    out = []
    try:
        names = sorted(n for n in os.listdir(paths.UNIT_DIR) if n.endswith(".service"))
    except OSError:
        return out
    for name in names:
        if name.startswith(("minecraft-", "redstonepanel")):
            continue
        try:
            with open(os.path.join(paths.UNIT_DIR, name), errors="replace") as f:
                text = f.read(65536)
        except OSError:
            continue
        if not re.search(r"minecraft|bedrock", name + "\n" + text, re.I):
            continue
        folder = re.search(r"^WorkingDirectory=-?(.+)$", text, re.M)
        command = re.search(r"^ExecStart=[-+@!:]*(\S+)", text, re.M)
        r = quiet(["systemctl", "is-active", name])
        out.append({"service": name, "running": r.stdout.strip() == "active",
                    "folder": folder.group(1).strip() if folder else "",
                    "program": command.group(1) if command else ""})
    return out


def owner_of(path):
    """Who a folder belongs to, as text give_back() understands ("uid:gid")."""
    import os
    st = os.stat(path)
    return f"{st.st_uid}:{st.st_gid}"


def give_back(path, owner):
    """Hands a folder and everything in it back to the owner owner_of() named."""
    import re
    if not re.fullmatch(r"[0-9]+:[0-9]+", owner or ""):
        raise ValueError("not an owner this system gave")
    return logged(["chown", "-R", owner, path])
