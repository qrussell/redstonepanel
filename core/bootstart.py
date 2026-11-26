"""Start at boot (1.25): whether a game server starts by itself when the machine starts.

On by default, as every server always was. Turned off, the server's unit is disabled (Linux:
`systemctl disable`; Windows: the service's start type is Manual) and the panel remembers the
choice in DATA_DIR/boot.json ({"off": ["minecraft-x", ...]}, root-only), so the places that set
a server up again keep it: restore, import, Restore of a removed server, taking over a server
found on the machine, and a move to another cluster member (the move's details carry it).
Starting, stopping and restarting by hand, schedules and crash restarts are not affected.

Sleep mode (1.26): a server with sleep mode on is never started by the system at boot, whatever
this choice says. The panel puts it to sleep after the machine starts when the choice is on, and
leaves it stopped when it's off (core/sleep.py). The choice is kept as it is meanwhile.
"""
import json
import os
import threading

from core.settings import DATA_DIR
from core.platform import services

BOOT_FILE = os.path.join(DATA_DIR, "boot.json")
_lock = threading.Lock()


def _load():
    try:
        with open(BOOT_FILE) as f:
            data = json.load(f)
        off = data.get("off") if isinstance(data, dict) else None
        return {s for s in off if isinstance(s, str)} if isinstance(off, list) else set()
    except (OSError, ValueError):
        return set()


def _save(off):
    os.makedirs(os.path.dirname(BOOT_FILE), exist_ok=True)
    tmp = BOOT_FILE + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump({"off": sorted(off)}, f)
    os.replace(tmp, BOOT_FILE)


def wanted(server):
    """True unless Start at boot was turned off for this server."""
    return server not in _load()


def remember(server, on):
    """Stores the choice without touching the unit (a server being set up)."""
    with _lock:
        off = _load()
        if on and server in off:
            off.discard(server)
        elif not on and server not in off:
            off.add(server)
        else:
            return
        _save(off)


def forget(server):
    """A server deleted for good: no choice left behind for a new server of the same name."""
    remember(server, True)


def _sleeps(server):
    try:
        from core import sleep
        return sleep.enabled(server)
    except Exception:
        return False


def apply(server, quiet=False):
    """Enables or disables the server's unit as chosen. Where the panel used to enable every
    server it set up, it calls this instead. Sleep mode on: always disabled (the panel wakes it)."""
    if wanted(server) and not _sleeps(server):
        services.enable(server, quiet=quiet)
    else:
        services.disable(server, quiet=quiet)


def enabled(server):
    """What the system says now: True when the unit starts at boot."""
    return services.unit_state(services.unit(server), "is-enabled", timeout=10) == "enabled"


def shown(server):
    """What the Start at boot switch shows: the choice while sleep mode is on (the unit is
    disabled then), else what the system says."""
    return wanted(server) if _sleeps(server) else enabled(server)


def turn(server, on):
    """Turns Start at boot on or off for a server now. Returns what the switch shows after."""
    remember(server, bool(on))
    apply(server, quiet=True)          # the caller logs who did it; systemctl's "Created symlink" is noise
    return shown(server)
