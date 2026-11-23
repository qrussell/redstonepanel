"""The platform layer: everything the panel asks of the operating system goes through here.

Until 1.21 the panel called `systemctl`, `ufw`, `screen`, `apt` and friends from wherever it
needed them. Those calls now live in one place per system, so a second system is a second folder
next to `linux/`, not a change to every feature. `windows/` is that second system (a preview
from 1.22).

    from core.platform import services, firewall, console, packages, system, certs, paths

    services.start("minecraft-java")        # systemctl start minecraft-java.service
    firewall.allow(25565, "tcp")            # ufw allow 25565/tcp
    console.send("minecraft-bedrock", "save hold")

Each name is a small stand-in that hands the call to the system in use, so
`from core.platform import services` keeps working after `use("fake")` in a test.

What each area is for:

    services   game server and panel services, schedules (timers), their logs
    firewall   opening and closing ports
    console    typing a command into a server that has no RCON (Bedrock)
    packages   the system's package manager: Java, and the panel's own package
    system     this machine: addresses, accounts, file ownership
    certs      making and checking certificates
    paths      where things live (core/platform/paths.py)

Rules for code outside this package:
  * never run a system program yourself (tests/test_platform.py checks the source for it);
  * never write a system path as a string; take it from `paths`;
  * catch `CommandError` (the program is missing, timed out or couldn't be run).
"""
import importlib
import os
import subprocess

from core.platform import paths            # noqa: F401  (re-exported)

# What a platform call raises when its program is missing, can't be run or times out.
CommandError = (OSError, subprocess.SubprocessError)
TimeoutExpired = subprocess.TimeoutExpired

AREAS = ("services", "firewall", "console", "packages", "system", "certs")
SYSTEMS = ("linux", "windows", "fake")

_state = {"name": None, "areas": {}}


def default_system():
    """The system this machine runs: "windows" (a preview from 1.22) or "linux"."""
    return "windows" if os.name == "nt" else "linux"


def use(name=None):
    """Switches every area to another system ("linux", or "fake" for tests). Returns the name."""
    name = name or default_system()
    if name not in SYSTEMS:
        raise ValueError(f"Unknown platform '{name}'.")
    _state["areas"] = {a: importlib.import_module(f"core.platform.{name}.{a}") for a in AREAS}
    _state["name"] = name
    return name


def name():
    """"linux", "windows" or "fake"."""
    if _state["name"] is None:
        use()
    return _state["name"]


def area(which):
    """The module doing `which` ("services", ...) on the system in use."""
    if _state["name"] is None:
        use()
    return _state["areas"][which]


class _Area:
    """`services.start(...)` -> the current system's services.start(...)."""

    def __init__(self, which):
        object.__setattr__(self, "_which", which)

    def __getattr__(self, attr):
        return getattr(area(self._which), attr)

    def __setattr__(self, attr, value):          # so mock.patch.object(services, "start", ...) works
        setattr(area(self._which), attr, value)

    def __delattr__(self, attr):
        delattr(area(self._which), attr)

    def __repr__(self):
        return f"<core.platform.{name()}.{self._which}>"


services = _Area("services")
firewall = _Area("firewall")
console = _Area("console")
packages = _Area("packages")
system = _Area("system")
certs = _Area("certs")

__all__ = ["services", "firewall", "console", "packages", "system", "certs", "paths",
           "CommandError", "TimeoutExpired", "use", "name", "area"]
