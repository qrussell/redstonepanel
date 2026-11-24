"""The terminal on Windows: not in the preview. The panel's Terminal needs a login shell on a
pseudo-terminal as the signed-in account; Windows' ConPTY and its accounts come later, so the
panel hides Terminal here (supported() is False) and refuses a session if one is asked for."""

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
        raise PermissionError("The terminal isn't in the Windows preview yet.")

    def read(self, n=16384):
        return b""

    def write(self, data):
        raise PermissionError("The terminal isn't in the Windows preview yet.")

    def resize(self, rows, cols):
        pass

    def foreground_uid(self):
        return None

    def alive(self):
        return False

    def close(self):
        pass


def spawn(user, rows, cols, remote_host):
    raise PermissionError("The terminal isn't in the Windows preview yet.")
