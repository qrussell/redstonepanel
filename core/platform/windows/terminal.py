"""The terminal on Windows: left out by design. A Windows panel usually runs on the computer its
owner sits at, where a command prompt is one click away, so the panel hides Terminal here
(supported() is False) and refuses a session if one is asked for. See docs/design/windows.md."""

NO_LOGIN = ("nologin", "false")


def supported():
    return False


def login_shell(user):
    return None


def account_uid(user):
    return None


def check_password(user, password):
    return False


class Shell:
    """Never made on Windows; here so every system has the same names."""

    def __init__(self, *args, **kwargs):
        raise PermissionError("The terminal is only on Linux; on Windows, open a command prompt on the computer itself.")

    def read(self, n=16384):
        return b""

    def write(self, data):
        raise PermissionError("The terminal is only on Linux; on Windows, open a command prompt on the computer itself.")

    def resize(self, rows, cols):
        pass

    def foreground_uid(self):
        return None

    def alive(self):
        return False

    def close(self):
        pass


def spawn(user, rows, cols, remote_host):
    raise PermissionError("The terminal is only on Linux; on Windows, open a command prompt on the computer itself.")
