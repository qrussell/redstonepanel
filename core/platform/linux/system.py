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
