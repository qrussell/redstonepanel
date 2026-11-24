"""A shell for the panel's terminal (1.21.10), as a Linux account and never as the panel's root.

The panel checks the account's password itself (PAM), then starts `login -f` on a new
pseudo-terminal. `login -f` is how a root service starts a login session for an account it has
already checked (getty's autologin uses it): it sets the account's groups, uid, home folder,
environment and limits, and its PAM session step (logged by the system, `session opened for
user ...`) moves the shell into the account's own session, so the shell is what a console
login would give that account.
"""
import fcntl
import os
import pty
import pwd
import signal
import struct
import termios
import time

from core.platform import paths

# Shells that mean "this account may not log in"
NO_LOGIN = ("nologin", "false")


def supported():
    """True when this system can open a terminal for the panel."""
    return True


def login_shell(user):
    """The account's login shell, or None when the account doesn't exist or may not log in
    (nologin/false, or a shell this machine doesn't list in /etc/shells)."""
    try:
        shell = pwd.getpwnam(user).pw_shell
    except (KeyError, TypeError, ValueError):
        return None
    if not shell or os.path.basename(shell) in NO_LOGIN:
        return None
    try:
        with open(paths.SHELLS_FILE, encoding="utf-8", errors="replace") as f:
            allowed = {line.strip() for line in f if line.strip() and not line.startswith("#")}
    except OSError:
        allowed = None
    if allowed is not None and shell not in allowed:
        return None
    return shell


def account_uid(user):
    try:
        return pwd.getpwnam(user).pw_uid
    except (KeyError, TypeError, ValueError):
        return None


def check_password(user, password):
    """True when PAM accepts the password and the account is usable (not locked or expired)."""
    from core import auth                       # PAM through ctypes, shared with panel sign-in
    return auth.pam_authenticate(user, password)


class Shell:
    """A running login session on a pseudo-terminal: bytes in, bytes out."""

    def __init__(self, pid, fd):
        self.pid, self.fd = pid, fd
        self._closed = False

    def read(self, n=16384):
        """Up to n bytes of output, b"" when the shell has ended, None when there's nothing yet."""
        try:
            return os.read(self.fd, n)
        except BlockingIOError:
            return None
        except OSError:                          # EIO: the other side closed
            return b""

    def write(self, data):
        view = memoryview(data)
        deadline = time.monotonic() + 5
        while view:
            try:
                n = os.write(self.fd, view)
                view = view[n:]
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.01)

    def resize(self, rows, cols):
        try:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            pass

    def foreground_uid(self):
        """The uid of whatever runs in the terminal's foreground right now (the shell, or a
        program started from it), or None while `login` is still starting it."""
        try:
            pgrp = os.tcgetpgrp(self.fd)
        except OSError:
            return None
        if pgrp == self.pid:                     # still `login` itself (root, before the shell)
            return None
        try:
            with open(f"{paths.PROC}/{pgrp}/status", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line.startswith("Uid:"):
                        return int(line.split()[1])     # the real uid
        except (OSError, ValueError, IndexError):
            return None
        return None

    def alive(self):
        try:
            pid, _status = os.waitpid(self.pid, os.WNOHANG)
            return pid == 0
        except ChildProcessError:
            return False

    def close(self):
        """Hangs up (as closing an SSH window does) and makes sure nothing is left behind."""
        if self._closed:
            return
        self._closed = True
        try:
            os.close(self.fd)                    # SIGHUP to the session
        except OSError:
            pass
        for sig, wait in ((signal.SIGHUP, 2.0), (signal.SIGTERM, 2.0), (signal.SIGKILL, 1.0)):
            end = time.monotonic() + wait
            while time.monotonic() < end:
                if not self.alive():
                    return
                time.sleep(0.05)
            try:
                os.kill(self.pid, sig)
            except ProcessLookupError:
                return
        self.alive()


def spawn(user, rows, cols, remote_host):
    """Starts a login session for `user` (already checked) on a new pseudo-terminal.
    remote_host: the browser's address, given to `login -h` (an IP address only)."""
    if account_uid(user) is None or not login_shell(user):
        raise PermissionError(f"{user} can't log in on this machine.")
    pid, fd = pty.fork()
    if pid == 0:                                 # the child: becomes `login`, never returns
        try:
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            env = {"TERM": "xterm-256color", "LANG": os.environ.get("LANG", "C.UTF-8")}
            if os.path.exists(paths.LOGIN):
                os.execve(paths.LOGIN, [paths.LOGIN, "-h", remote_host, "-f", user], env)
            _become(user, env)
        finally:
            os._exit(127)
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    return Shell(pid, fd)


def _become(user, env):
    """No `login` program (every supported system has one): drop to the account and run its
    login shell, as `su -` would."""
    pw = pwd.getpwnam(user)
    os.initgroups(user, pw.pw_gid)
    os.setgid(pw.pw_gid)
    os.setuid(pw.pw_uid)
    if os.getuid() != pw.pw_uid or (pw.pw_uid != 0 and os.geteuid() == 0):
        os._exit(126)
    env = dict(env, HOME=pw.pw_dir, USER=user, LOGNAME=user, SHELL=pw.pw_shell,
               PATH="/usr/local/bin:/usr/bin:/bin")
    try:
        os.chdir(pw.pw_dir)
    except OSError:
        os.chdir("/")
    os.execve(pw.pw_shell, ["-" + os.path.basename(pw.pw_shell)], env)
