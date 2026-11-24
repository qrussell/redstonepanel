"""A pretend terminal: accounts and passwords from fake.state.terminal_accounts, and a "shell"
that echoes what is typed (b"exit\r" ends it)."""
from core.platform import fake

NO_LOGIN = ("nologin", "false")


def supported():
    return fake.state.terminal_supported


def login_shell(user):
    a = fake.state.terminal_accounts.get(user)
    if not a or a.get("shell", "/bin/bash").rsplit("/", 1)[-1] in NO_LOGIN:
        return None
    return a.get("shell", "/bin/bash")


def account_uid(user):
    a = fake.state.terminal_accounts.get(user)
    return a["uid"] if a else None


def check_password(user, password):
    fake.record("terminal.check_password", user)
    a = fake.state.terminal_accounts.get(user)
    return bool(a) and not a.get("locked") and a.get("password") == password


class Shell:
    def __init__(self, user, rows, cols):
        self.pid, self.fd = 4242, -1
        self.user, self.size = user, (rows, cols)
        self.out = bytearray(f"welcome {user}\r\n".encode())
        self.ended = self.closed = False
        self.uid = fake.state.terminal_accounts[user]["uid"] if fake.state.terminal_uid is None \
            else fake.state.terminal_uid

    def read(self, n=16384):
        if self.out:
            data = bytes(self.out[:n])
            del self.out[:n]
            return data
        return b"" if self.ended else None

    def write(self, data):
        self.out += data
        if b"exit\r" in data:
            self.ended = True

    def resize(self, rows, cols):
        self.size = (rows, cols)

    def foreground_uid(self):
        return self.uid

    def alive(self):
        return not (self.ended or self.closed)

    def close(self):
        self.closed = True
        fake.record("terminal.close", self.user)


def spawn(user, rows, cols, remote_host):
    if account_uid(user) is None or not login_shell(user):
        raise PermissionError(f"{user} can't log in on this machine.")
    fake.record("terminal.spawn", user, remote_host)
    shell = Shell(user, rows, cols)
    fake.state.shells.append(shell)
    return shell
