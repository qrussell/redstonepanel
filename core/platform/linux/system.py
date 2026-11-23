"""This machine on Linux: its addresses, accounts, file ownership and locks, and the files the
dashboard reads (/proc)."""
import ctypes
import ctypes.util
import fcntl
import grp
import os
import pwd
import subprocess

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
    """Hands a folder and everything in it to the game account."""
    return logged(["chown", "-R", f"{GAME_ACCOUNT}:{GAME_ACCOUNT}", path])


def java_version_text(java="java"):
    """What `java -version` prints (on stderr)."""
    return subprocess.run([java, "-version"], capture_output=True, text=True).stderr


def delete_tree(path):
    """Deletes a folder and everything in it (written to the activity log)."""
    return logged(["rm", "-rf", path])


def make_executable(path):
    return logged(["chmod", "+x", path])


def tail(path, lines=3000):
    """The last lines of a file the game account owns. Returns (text, error)."""
    err = ""
    for cmd in (["sudo", "-n", "tail", "-n", str(lines), path], ["tail", "-n", str(lines), path]):
        r = quiet(cmd)
        if r.returncode == 0:
            return r.stdout, ""
        err = (r.stderr or "").strip()
    return "", err


def is_admin():
    """True when the panel runs with full rights (root), as an installed panel does."""
    return os.geteuid() == 0


def as_game_account(cmd):
    """cmd, changed so that it runs as the game account and not as the panel (root)."""
    if not is_admin():
        return cmd
    return ["runuser", "-u", GAME_ACCOUNT, "--"] + list(cmd)


def take_for_game_account(path):
    """give_to_game_account() without the activity log."""
    return quiet(["chown", "-R", f"{GAME_ACCOUNT}:{GAME_ACCOUNT}", path])


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
_libpam = _libc = None

def _load_pam():
    global _libpam, _libc
    if _libpam is None:
        _libpam = ctypes.CDLL(ctypes.util.find_library("pam") or "libpam.so.0")
        _libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6")
        _libc.calloc.restype = ctypes.c_void_p
        _libc.calloc.argtypes = [ctypes.c_size_t, ctypes.c_size_t]
        _libc.strdup.restype = ctypes.c_void_p
        _libc.strdup.argtypes = [ctypes.c_char_p]
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
        addr = _libc.calloc(n, ctypes.sizeof(_PamResponse))
        if not addr:
            return 5   # PAM_BUF_ERR
        resp = ctypes.cast(addr, ctypes.POINTER(_PamResponse))
        p_response[0] = resp
        for i in range(n):
            if messages[i].contents.msg_style in (_PAM_PROMPT_ECHO_OFF, _PAM_PROMPT_ECHO_ON):
                resp[i].resp = _libc.strdup(pw)
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


def hint(what, *args):
    """A sentence telling an administrator what to type or do, in this system's terms."""
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
