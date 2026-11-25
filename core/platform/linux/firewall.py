"""The firewall on Linux: ufw, when it's installed (it's optional; without it nothing is done)."""
import shutil

from core.platform.linux._run import logged, quiet

NAME = "ufw"


def installed():
    return bool(shutil.which("ufw"))


def allow(port, proto):
    """Opens a port ("25565" or a range "21200:21300") for "tcp" or "udp"."""
    return logged(["ufw", "allow", f"{port}/{proto}"])


def remove(port, proto):
    """Takes back a rule made by allow()."""
    return logged(["ufw", "delete", "allow", f"{port}/{proto}"])


def status_text(timeout=10):
    """`ufw status` as text: "Status: active" and the rules."""
    return quiet(["ufw", "status"], timeout=timeout).stdout


def is_active(text):
    return "Status: active" in (text or "")



def panel_ports(ports, only_if_present=True):
    """The panel's own web ports: left to the administrator on Linux (ufw is optional and off by
    default). Windows keeps a rule for them."""
    return 0
