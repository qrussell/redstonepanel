"""Services, schedules and their logs on Linux: systemd (`systemctl`, `systemd-run`, `journalctl`).

A game server is the unit `minecraft-<name>.service`; functions that take `server` add
`.service` themselves, functions that take `unit` want the whole unit name (a timer, Phantom's
service, the panel's own units).

Calls that change something and belong in the activity log go through the panel log
(start, stop, restart, enable, disable, reload); `quiet=True` runs the same command without it.
`elevate=True` puts `sudo` in front, for the call sites that have always done so.
"""
import os
import subprocess

from core.platform import paths
from core.platform.linux._run import logged, quiet as _quiet, _opt

SUFFIX = ".service"


def unit(server):
    return f"{server}{SUFFIX}"


def available():
    """True when systemd runs this machine (not in a plain container or a chroot)."""
    return paths.service_manager_running()


def prepare():
    """Called when the panel starts: whatever the service manager needs from the panel before
    it is used. Nothing on Linux (the package installs the drop-in and systemd does the rest)."""
    return None


# ---------------------------------------------------------------------------
# The raw command (for the few callers that need their own arguments)
# ---------------------------------------------------------------------------
def systemctl(*args, timeout=None, env=None):
    """`systemctl ARGS` -> CompletedProcess (text). Raises CommandError if it can't be run."""
    return _quiet(["systemctl", *args], **_opt(timeout=timeout, env=env))


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
def unit_state(unit_name, verb="is-active", timeout=None):
    """systemd's one-word answer about a unit: "active", "inactive", "failed", "enabled", ..."""
    return systemctl(verb, unit_name, timeout=timeout).stdout.strip()


def state(server, timeout=None):
    """"active", "inactive", "failed", "activating", ... for a game server."""
    return unit_state(unit(server), timeout=timeout)


def is_active(server):
    return state(server) == "active"


def show(unit_name, prop, *options, timeout=None):
    """One property of a unit (`systemctl show -p PROP --value`), stripped."""
    return systemctl("show", "-p", prop, "--value", *options, unit_name, timeout=timeout).stdout.strip()


def exec_start(server):
    """The command line the server's unit starts (systemd's ExecStart, as it prints it)."""
    return show(unit(server), "ExecStart")


def working_dir(server):
    """The folder the server's unit runs in, or "" when systemd doesn't know the unit."""
    d = show(unit(server), "WorkingDirectory")
    if not d:
        # Very old systemd has no --value: take what follows "WorkingDirectory="
        out = systemctl("show", "-p", "WorkingDirectory", unit(server)).stdout.strip()
        d = out.split("=", 1)[1].strip() if "=" in out else ""
    return d


def show_many(units, props, timeout=10):
    """`systemctl show` for several units and properties at once, as systemd prints it (blocks
    of KEY=VALUE separated by blank lines). Times are in UTC."""
    return systemctl("show", "-p", ",".join(props), *units, timeout=timeout,
                     env={**os.environ, "TZ": "UTC"}).stdout


def started_at(server, timeout=10):
    """When the server last started (Unix time), or None."""
    out = show(unit(server), "ActiveEnterTimestamp", "--timestamp=unix", timeout=timeout)
    return int(out[1:]) if out.startswith("@") and out[1:].isdigit() else None


def failed(*patterns, timeout=10):
    """[(unit, load state)] for failed units matching the patterns ("minecraft*")."""
    r = systemctl("list-units", "--failed", "--all", "--plain", "--no-legend", *patterns, timeout=timeout)
    out = []
    for line in r.stdout.splitlines():
        parts = line.split()
        if parts:
            out.append((parts[0], parts[1] if len(parts) > 1 else ""))
    return out


def reset_failed(*units, timeout=None):
    """Forgets that these units failed."""
    return systemctl("reset-failed", *units, timeout=timeout)


def queued_jobs(server, timeout=5):
    """The jobs systemd has queued for the server: ["stop"], ["restart"], ["start"], or []."""
    r = systemctl("list-jobs", "--no-legend", "--plain", unit(server), timeout=timeout)
    jobs = []
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[1] == unit(server):
            jobs.append(parts[2])
    return jobs


def name_free(server):
    """False when the machine already has a service of this name that the panel didn't make."""
    r = systemctl("cat", unit(server), timeout=10)
    return r.returncode != 0


def unit_files(prefix="minecraft", suffix=SUFFIX):
    """The unit files in the panel's unit folder whose name starts with prefix, e.g.
    ["minecraft-java.service"]. Top level only, in the folder's own order."""
    try:
        with os.scandir(paths.UNIT_DIR) as it:
            return [e.name for e in it if e.name.startswith(prefix) and e.name.endswith(suffix)]
    except OSError:
        return []


# ---------------------------------------------------------------------------
# Changing things
# ---------------------------------------------------------------------------
def _change(args, elevate=False, quiet=False, timeout=None):
    cmd = (["sudo"] if elevate else []) + ["systemctl", *args]
    if quiet:
        return _quiet(cmd, **_opt(timeout=timeout))
    return logged(cmd)


def control(unit_name, action, elevate=False, quiet=False):
    """start, stop, restart, enable or disable any unit by its full name."""
    return _change([action, unit_name], elevate, quiet)


def other_enabled(service):
    """True when a service the panel didn't make ("mc-survival.service") starts with the machine."""
    return unit_state(service, "is-enabled") == "enabled"


def other_control(service, action):
    """start, stop, enable or disable a service the panel didn't make. Only core/found.py uses
    this, and only after an admin chose to hand that server to the panel (or back)."""
    if action not in ("start", "stop", "enable", "disable"):
        raise ValueError(action)
    return control(service, action, elevate=True)


def start(server, elevate=False, quiet=False):
    return control(unit(server), "start", elevate, quiet)


def stop(server, elevate=False, quiet=False):
    return control(unit(server), "stop", elevate, quiet)


def restart(server, elevate=False, quiet=False):
    return control(unit(server), "restart", elevate, quiet)


def enable(server, elevate=False, quiet=False):
    return control(unit(server), "enable", elevate, quiet)


def disable(server, elevate=False, quiet=False):
    return control(unit(server), "disable", elevate, quiet)


def reload(elevate=False, quiet=False):
    """Makes systemd read unit files again after one was written or removed."""
    return _change(["daemon-reload"], elevate, quiet)


def restart_panel():
    """Restarts the panel's own service. Returns the exit code."""
    return subprocess.run(["systemctl", "restart", "redstonepanel.service"]).returncode


def panel_state():
    return unit_state("redstonepanel.service")


# ---------------------------------------------------------------------------
# Schedules (timers)
# ---------------------------------------------------------------------------
def enable_schedule(timer):
    """Turns a timer on and (re)starts it with what its file says now."""
    logged(["systemctl", "enable", timer])
    logged(["systemctl", "restart", timer])


def enable_schedule_now(timer):
    """`enable --now`, without the activity log (the panel's own housekeeping timers)."""
    return _quiet(["systemctl", "enable", "--now", timer])


def disable_schedule(timer):
    """Turns a timer off and stops it."""
    return _quiet(["systemctl", "disable", "--now", timer])


def schedule_enabled(timer):
    return unit_state(timer, "is-enabled") == "enabled"


def next_run(timer):
    """When the timer fires next, as systemd writes it ("Mon 2026-11-23 04:00:00 UTC"), or None."""
    out = show(timer, "NextElapseUSecRealtime")
    return out if out and out != "n/a" else None


def next_run_epoch(timer):
    """When the timer fires next as Unix time, or None."""
    out = next_run(timer)
    if not out:
        return None
    r = _quiet(["date", "-d", out, "+%s"])
    return int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip().isdigit() else None


# ---------------------------------------------------------------------------
# One-off work outside the panel's own service
# ---------------------------------------------------------------------------
def start_later(server, seconds=20, timeout=10):
    """Starts the server in `seconds` seconds, whatever happens to the caller meanwhile."""
    return _quiet(["systemd-run", "--quiet", f"--on-active={seconds}", "--timer-property=AccuracySec=1s",
                   paths.SYSTEMCTL, "start", unit(server)], timeout=timeout)


def can_detach():
    import shutil
    return bool(shutil.which("systemd-run"))


def run_detached(name, command, description=None, low_priority=False, no_block=False, timeout=30,
                 program="systemd-run"):
    """Runs command in its own transient unit `name`, so it outlives the panel's service (an
    update restarts the panel) or the unit that asked for it. Returns the CompletedProcess."""
    if no_block:
        cmd = [program, "--no-block", "--quiet", "--collect", f"--unit={name}"]
    else:
        cmd = [program, f"--unit={name}", "--collect", "--quiet"]
    if description:
        cmd.append(f"--description={description}")
    if low_priority:
        cmd += ["--property=Nice=10", "--property=IOSchedulingClass=idle"]
    return _quiet(cmd + list(command), timeout=timeout)


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------
def set_runtime_limits(server, memory_max=None, cpu=None):
    """Applies a memory cap (bytes) and CPU share (cores, e.g. 1.5) to a running server now,
    without a restart. None lifts that limit."""
    return _quiet(["systemctl", "set-property", "--runtime", unit(server),
                   f"MemoryMax={memory_max or 'infinity'}",
                   f"CPUQuota={int(round(cpu * 100)) if cpu else ''}{'%' if cpu else ''}"])


# ---------------------------------------------------------------------------
# Logs (the journal)
# ---------------------------------------------------------------------------
def log(unit_name=None, lines=None, output="cat", since=None, invocation=None, quiet=False, timeout=None):
    """What a unit printed, from the journal, as one string. See log_result()."""
    return log_result(unit_name, lines, output, since, invocation, quiet, timeout).stdout


def log_result(unit_name=None, lines=None, output="cat", since=None, invocation=None, quiet=False, timeout=None):
    """What a unit printed, from the journal, as a CompletedProcess (stdout, stderr, returncode).

    lines: only the last N. output: "cat" (the text alone), "short-unix" (with a Unix time in
    front) or None (journalctl's own format). since: a Unix time. invocation: one run of the
    unit (systemd's INVOCATION_ID) in place of the unit name."""
    cmd = ["journalctl"] + ([f"_SYSTEMD_INVOCATION_ID={invocation}"] if invocation else ["-u", unit_name])
    if lines is not None:
        cmd += ["-n", str(lines)]
    cmd.append("--no-pager")
    if output:
        cmd += ["-o", output]
    if quiet:
        cmd.append("-q")
    if since:
        cmd += ["--since", f"@{int(since)}"]
    return _quiet(cmd, **_opt(timeout=timeout))


def run_log(server, invocation=None, lines=300, timeout=10):
    """What the server printed in one run (systemd's INVOCATION_ID), or its last lines when
    the run isn't known. "" when the journal can't be read."""
    cmd = ["journalctl", "--no-pager", "-o", "cat", "-n", str(lines)]
    cmd += [f"_SYSTEMD_INVOCATION_ID={invocation}"] if invocation else ["-u", unit(server)]
    r = _quiet(cmd, timeout=timeout)
    return r.stdout if r.returncode == 0 else ""
