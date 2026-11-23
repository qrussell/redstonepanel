"""This machine on Linux: its addresses, accounts and file ownership."""
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
    import os
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
