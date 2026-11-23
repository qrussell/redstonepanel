"""Scheduled restarts: a systemd timer per server, with in-game warnings before the restart.

redstonepanel-restart@<server>.timer starts redstonepanel-restart@<server>.service, which runs
`redstonepanel restart-server <server>`. The timer fires `warn` minutes before the chosen time, so
players are warned (Java over RCON, Bedrock through its screen session) and the restart happens
at the time shown in the panel. A stopped server is left stopped, and a restart waits for a
backup of the same server to finish.
"""
import fcntl
import json
import os
import re
import subprocess    # noqa: F401  (unused here since 1.21; tests replace subprocess.run through this module)
import time

from core import backups, manager, notify, rcon
from core.settings import DATA_DIR
from core.platform import paths, services

SCHEDULE_FILE = os.path.join(DATA_DIR, "restarts.json")
UNIT_DIR = backups.UNIT_DIR
SERVICE_TEMPLATE = "redstonepanel-restart@.service"
NAME_RE = backups.NAME_RE
TIME_RE = backups.TIME_RE

# id -> label; backups.FREQUENCIES has the OnCalendar patterns
FREQUENCIES = {"daily": "Every day", "12h": "Every 12 hours", "6h": "Every 6 hours", "weekly": "Every Sunday"}
WARN_CHOICES = (0, 1, 5, 10, 15)
DEFAULT_SCHEDULE = {"enabled": False, "frequency": "daily", "time": "05:00", "warn": 5}
BACKUP_WAIT = 30 * 60


class RestartError(ValueError):
    """A restart schedule that can't be saved or run (message is safe to show)."""


def _check_name(server):
    if not NAME_RE.fullmatch(server or ""):
        raise RestartError("Invalid server name.")


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------
def _load():
    try:
        with open(SCHEDULE_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}

def _save(data):
    os.makedirs(os.path.dirname(SCHEDULE_FILE), exist_ok=True)
    tmp = SCHEDULE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, SCHEDULE_FILE)

def get_schedule(server):
    s = dict(DEFAULT_SCHEDULE)
    stored = _load().get(server)
    if isinstance(stored, dict):
        s.update({k: stored[k] for k in DEFAULT_SCHEDULE if k in stored})
    return s

def on_calendar(frequency, time_str, warn):
    """OnCalendar for the timer: `warn` minutes before the restart time."""
    m = TIME_RE.fullmatch(time_str or "")
    if frequency not in FREQUENCIES or not m:
        raise RestartError("Choose how often, and a time as HH:MM.")
    if warn not in WARN_CHOICES:
        raise RestartError("Choose a warning time from the list.")
    minutes = int(m.group(1)) * 60 + int(m.group(2)) - warn
    day = "Sun"
    if minutes < 0:                         # warning starts the evening before
        minutes += 24 * 60
        day = "Sat"
    shifted = f"{minutes // 60:02d}:{minutes % 60:02d}"
    calendar = backups.on_calendar(frequency, shifted)
    return calendar.replace("Sun ", f"{day} ", 1) if frequency == "weekly" else calendar

def _timer_name(server):
    return f"redstonepanel-restart@{server}.timer"

def _service_unit():
    return f"""[Unit]
Description=RedstonePanel scheduled restart of %i
After=network.target

[Service]
Type=oneshot
ExecStart={paths.PANEL_CMD} restart-server %i
TimeoutStartSec={max(WARN_CHOICES) * 60 + BACKUP_WAIT + 600}
"""

def _timer_unit(server, calendar):
    return f"""[Unit]
Description=RedstonePanel scheduled restarts of {server}

[Timer]
OnCalendar={calendar}
Persistent=false

[Install]
WantedBy=timers.target
"""

def _apply(server, schedule):
    timer = _timer_name(server)
    if schedule["enabled"]:
        backups._write_if_changed(os.path.join(UNIT_DIR, SERVICE_TEMPLATE), _service_unit())
        backups._write_if_changed(os.path.join(UNIT_DIR, timer),
                                  _timer_unit(server, on_calendar(schedule["frequency"], schedule["time"],
                                                                  schedule["warn"])))
        services.reload()
        services.enable_schedule(timer)
    else:
        _remove_timer(server)

def _remove_timer(server):
    timer = _timer_name(server)
    path = os.path.join(UNIT_DIR, timer)
    if os.path.exists(path):
        services.disable_schedule(timer)
        os.remove(path)
        services.reload()

def set_schedule(server, enabled, frequency, time_str, warn):
    _check_name(server)
    try:
        warn = int(warn)
    except (TypeError, ValueError):
        warn = -1
    on_calendar(frequency, time_str, warn)        # validates all three
    schedule = {"enabled": bool(enabled), "frequency": frequency, "time": time_str, "warn": warn}
    data = _load()
    data[server] = schedule
    _save(data)
    _apply(server, schedule)
    manager.write_log(f"[RESTART] Scheduled restarts of {server}: " + (
        f"{FREQUENCIES[frequency].lower()} at {time_str}"
        + (f", warning players {warn} min before." if warn else ", without a warning.") if enabled else "off."))
    return schedule

def next_restart(server):
    """When the next restart happens (the timer fires `warn` minutes earlier)."""
    path = os.path.join(UNIT_DIR, _timer_name(server))
    if not os.path.exists(path):
        return None
    at = services.next_run_epoch(_timer_name(server))
    if at is None:
        return None
    return at + get_schedule(server)["warn"] * 60

def overview(server):
    return {"schedule": get_schedule(server), "frequencies": FREQUENCIES, "warn_choices": list(WARN_CHOICES),
            "next_restart": next_restart(server), "timezone": time.strftime("%Z")}

def on_server_removed(server, purged):
    if not NAME_RE.fullmatch(server or ""):
        return
    _remove_timer(server)
    if purged:
        data = _load()
        if data.pop(server, None) is not None:
            _save(data)

def on_server_restored(server):
    if NAME_RE.fullmatch(server or "") and get_schedule(server)["enabled"]:
        _apply(server, get_schedule(server))


# ---------------------------------------------------------------------------
# Running a restart
# ---------------------------------------------------------------------------
def warning_steps(warn):
    """[(seconds before the restart, message)] for a warning of `warn` minutes."""
    steps = []
    if warn:
        steps.append((warn * 60, f"Server restarts in {warn} minute{'s' if warn != 1 else ''}."))
        if warn > 1:
            steps.append((60, "Server restarts in 1 minute."))
        steps.append((10, "Server restarts in 10 seconds."))
    return steps

def _announcer(server, d):
    """A function that says something in game, or None if this server can't be reached."""
    if backups._edition(server, d) == "java":
        send = backups._java_rcon(d)
        if not send:
            return None
        def say(msg):
            try:
                send(f"say {msg}")
            except rcon.RconError as e:
                manager.write_log(f"[RESTART] Couldn't warn {server}'s players over RCON: {e}")
        return say
    return lambda msg: backups._screen(server, f"say {msg}")

def _wait_for_backup(server):
    """Holds the server's backup lock (so no backup is mid-copy); returns the fd, or None."""
    d = backups.server_backup_dir(server)
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        fd = os.open(os.path.join(d, ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        return None
    deadline = time.time() + BACKUP_WAIT
    logged = False
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if time.time() > deadline:
                os.close(fd)
                raise RestartError(f"A backup of {server} has been running for {BACKUP_WAIT // 60} minutes; "
                                   f"skipped this restart.")
            if not logged:
                manager.write_log(f"[RESTART] Waiting for the backup of {server} to finish before restarting it.")
                logged = True
            time.sleep(5)

def run_restart(server, sleep=time.sleep):
    """What the timer runs. Returns 'restarted' or 'skipped'."""
    _check_name(server)
    if not os.path.isfile(os.path.join(UNIT_DIR, f"{server}.service")):
        raise RestartError(f"No Minecraft server called '{server}' on this panel.")
    if not backups._is_active(server):
        manager.write_log(f"[RESTART] {server} isn't running; skipped its scheduled restart.")
        return "skipped"
    d = manager.get_server_dir(server)
    warn = get_schedule(server)["warn"]
    steps = warning_steps(warn)
    say = _announcer(server, d) if steps else None
    if steps and not say:
        manager.write_log(f"[WARNING] {server} has no RCON, so players weren't warned before its scheduled "
                          f"restart. Open its Live Chat tab once to set RCON up.")
    if say:
        manager.write_log(f"[RESTART] Warning {server}'s players: restart in {warn} min.")
        for i, (before, msg) in enumerate(steps):
            say(msg)
            sleep(before - (steps[i + 1][0] if i + 1 < len(steps) else 0))
    elif steps:
        sleep(warn * 60)                    # keep to the time shown in the panel
    fd = _wait_for_backup(server)
    try:
        manager.write_log(f"[RESTART] Restarting {server} (scheduled).")
        r = services.restart(server, quiet=True)
    finally:
        if fd is not None:
            os.close(fd)
    if r.returncode != 0:
        err = (r.stderr or r.stdout).strip() or f"systemctl exited with {r.returncode}"
        manager.write_log(f"[RESTART] Restarting {server} failed: {err}")
        notify.send("restarted", server, f"Scheduled restart of {server} failed: {err}")
        raise RestartError(f"Restarting {server} failed: {err}")
    notify.send("restarted", server, f"{server} restarted on schedule.")
    return "restarted"
