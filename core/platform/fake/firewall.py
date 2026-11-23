"""A pretend firewall: rules are a set in fake.state.rules."""
from core.platform import fake

NAME = "fake"


def installed():
    return fake.state.firewall_installed


def allow(port, proto):
    fake.record("firewall.allow", str(port), proto)
    fake.state.rules.add((str(port), proto))
    return 0


def remove(port, proto):
    fake.record("firewall.remove", str(port), proto)
    fake.state.rules.discard((str(port), proto))
    return 0


def status_text(timeout=10):
    """The same shape as `ufw status`, so the panel's rule parser reads it."""
    lines = ["Status: active" if fake.state.firewall_active else "Status: inactive", "",
             "To                         Action      From", "--                         ------      ----"]
    lines += [f"{port}/{proto:<21}ALLOW       Anywhere" for port, proto in sorted(fake.state.rules)]
    return "\n".join(lines) + "\n"


def is_active(text):
    return "Status: active" in (text or "")
