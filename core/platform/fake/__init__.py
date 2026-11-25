"""A pretend system for tests: nothing is run, everything is remembered.

    from core import platform
    platform.use("fake")
    from core.platform import fake
    fake.reset()
    fake.state.servers["minecraft-java"] = {"state": "active", "exec_start": "/usr/bin/java -jar server.jar"}
    ...code under test...
    fake.calls            # [("services.stop", "minecraft-java"), ("firewall.allow", 25565, "tcp"), ...]
    platform.use()        # back to the real system

Every area has the same functions as the Linux side (tests/test_platform.py compares them), so
code written against one runs against the other. With the fake in use the unit tests need no
systemd, ufw, screen, apt or openssl, on any operating system.
"""
from types import SimpleNamespace

calls = []          # every call that would have changed something, in order
state = SimpleNamespace()


def reset():
    """Forget everything: a machine with no servers, no firewall rules and nothing installed."""
    calls.clear()
    state.servers = {}              # name -> {"state", "enabled", "exec_start", "working_dir", "started_at", "log": [...], "jobs": [...]}
    state.units = {}                # other units (timers, the panel's own): unit name -> same shape
    state.schedules = {}            # timer -> {"enabled": bool, "next": epoch or None}
    state.detached = []             # [(name, command)]
    state.limits = {}               # server -> (memory_max, cpu)
    state.firewall_installed = True
    state.firewall_active = True
    state.rules = set()             # {(port, proto)}
    state.console_installed = True
    state.console = []              # [(server, text)]
    state.packages = {}             # package -> {"installed": version or None, "candidate": version or None}
    state.file_owners = {}          # path -> package
    state.addresses = ["192.0.2.10"]
    state.accounts = {"minecraft"}
    state.owned = []                # folders handed to the game account
    state.deleted = []
    state.service_manager = True
    state.locks = set()             # file descriptors holding a lock
    state.passwords = {}            # system account -> password
    state.machine_files = {}        # "meminfo" -> text
    state.processes = set()         # pids that are alive
    state.terminal_supported = True     # False: a system with no terminal (Windows, for now)
    state.terminal_accounts = {}        # name -> {"password", "uid", "shell", "locked"}
    state.terminal_uid = None           # what foreground_uid() says, when not the account's own
    state.shells = []                   # every Shell spawn() returned
    state.listeners = []                # [{"proto", "port", "pid", "program", "service", ...}] for system.listeners()
    state.docker_ps = ""                # what `docker ps --format "{{.Names}}\t{{.Ports}}"` prints
    return state


def record(*call):
    calls.append(tuple(call))


reset()
