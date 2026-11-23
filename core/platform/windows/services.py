"""Services, schedules and their logs on Windows.

A game server is the Windows service `minecraft-<name>`, hosted by the runner (runner.py) and
described by the unit file `minecraft-<name>.service` in <home>\\services, the same file systemd
reads on Linux. A schedule is a Task Scheduler task in the RedstonePanel folder, made from a
`.timer` file. What a unit printed is in <home>\\journal\\<unit>\\output.log.

The functions and their answers are the ones the Linux side gives ("active", "inactive",
"failed"; `systemctl show`'s KEY=VALUE blocks), so the rest of the panel is the same code.
"""
import datetime
import json
import os
import subprocess
import tempfile
import time

from core.platform import paths
from core.platform.windows import _win, units

SUFFIX = ".service"
TASK_FOLDER = "\\" + _win.GROUP + "\\"
GAME_PREFIX = "minecraft"
OWNED_PREFIXES = ("minecraft", "phantom-", "redstonepanel-")       # services the panel may create and remove


def unit(server):
    return f"{server}{SUFFIX}"


def available():
    """True on Windows: the service manager is always there."""
    return os.name == "nt"


def _done(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def _log(text):
    try:
        from core import manager
        manager.write_log(text)
    except Exception:
        pass


DROPIN = """# Written by RedstonePanel for its game servers (minecraft-*.service) on Windows.
# The runner types `stop` into the server's console and waits for it to save and leave when the
# service is stopped or Windows shuts down, and writes an entry to the event log when a server
# starts, stops or crashes; a task sends the panel's notifications for those entries.
[Service]
X-RedstonePanel-StopCommand=stop
X-RedstonePanel-Events=yes
TimeoutStopSec=90
"""


def prepare():
    """Called when the panel starts: the folders, the drop-in every game server's unit gets,
    the notification task, and a Windows service for every unit file."""
    for d in (paths.UNIT_DIR, paths.JOURNAL_DIR, paths.SERVERS_ROOT, paths.JVM_ROOT, paths.RUN_DIR):
        os.makedirs(d, exist_ok=True)
    dropin = os.path.join(paths.UNIT_DIR, "minecraft-.service.d")
    os.makedirs(dropin, exist_ok=True)
    path = os.path.join(dropin, "redstonepanel.conf")
    try:
        with open(path, encoding="utf-8") as f:
            current = f.read()
    except OSError:
        current = None
    if current != DROPIN:
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(DROPIN)
    try:
        r = _ensure_event_task()
        if r.returncode != 0:
            _log(f"[WARNING] Couldn't set up the notification task: {(r.stderr or r.stdout).strip()[:300]}")
    except (OSError, subprocess.SubprocessError) as e:
        _log(f"[WARNING] Couldn't set up the notification task: {e}")
    reload(quiet=True)


# ---------------------------------------------------------------------------
# What the runner recorded
# ---------------------------------------------------------------------------
def _full(unit_name):
    """"minecraft-java" -> "minecraft-java.service": systemctl takes either, so callers pass either."""
    unit_name = str(unit_name)
    return unit_name if unit_name.endswith((SUFFIX, ".timer")) else unit_name + SUFFIX


def _json(unit_name, name):
    try:
        with open(os.path.join(paths.journal_dir(unit_name), name), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _service_name(unit_name):
    return unit_name[:-len(SUFFIX)] if unit_name.endswith(SUFFIX) else unit_name


def _task_name(timer):
    return TASK_FOLDER + os.path.splitext(timer)[0]


def _is_service_unit(unit_name, spec=None):
    """True for a unit that is a Windows service: a .service that isn't a template instance or a
    one-off (those are started by a task)."""
    if not unit_name.endswith(SUFFIX) or "@" in unit_name:
        return False
    spec = units.load(unit_name) if spec is None else spec
    return spec is not None and units.get(spec, "Service", "Type", "simple") != "oneshot"


def _states(unit_name):
    """(ActiveState, SubState, status.json) of a unit."""
    unit_name = _full(unit_name)
    if unit_name.endswith(".timer"):
        on = _task_exists(unit_name)
        return ("active", "waiting", {}) if on else ("inactive", "dead", {})
    status = _json(unit_name, "status.json")
    if "@" in unit_name or not unit_name.endswith(SUFFIX):
        return status.get("state", "inactive"), status.get("sub", "dead"), status
    scm, _pid = _win.service_status(_service_name(unit_name))
    if scm == "running":
        if status.get("state") == "activating":
            return "activating", status.get("sub", "auto-restart"), status
        return "active", "running", status
    if scm == "starting":
        return "activating", "start", status
    if scm == "stopping":
        return "deactivating", "stop", status
    if status.get("state") == "failed" or (scm == "stopped" and status.get("state") in ("active", "activating",
                                                                                         "deactivating")):
        return "failed", "failed", status          # ended badly, or the runner itself was killed
    return "inactive", "dead", status


# ---------------------------------------------------------------------------
# The raw command: the few systemctl verbs the panel's features still spell out
# ---------------------------------------------------------------------------
def systemctl(*args, timeout=None, env=None):
    """Does what `systemctl ARGS` would, for the verbs the panel uses. Returns a CompletedProcess."""
    now = "--now" in args
    args = [a for a in args if a not in ("--now", "--quiet", "-q", "--no-block")]
    if not args:
        return _done(1, stderr="No verb.")
    verb, rest = args[0], args[1:]
    try:
        if verb == "daemon-reload":
            reload(quiet=True)
            return _done()
        if verb in ("is-active", "is-enabled") and rest:
            out = unit_state(rest[0], verb)
            return _done(0 if out in ("active", "enabled") else 3, out + "\n")
        if verb in ("start", "stop", "restart", "enable", "disable") and rest:
            for u in rest:
                if u.endswith(".timer"):
                    enable_schedule_now(u) if verb in ("start", "restart", "enable") else disable_schedule(u)
                    continue
                control(u, verb, quiet=True)
                if now and verb in ("enable", "disable"):
                    control(u, "start" if verb == "enable" else "stop", quiet=True)
            return _done()
        if verb == "show" and rest:
            props = [rest[i + 1] for i, a in enumerate(rest[:-1]) if a == "-p"]
            names = [a for i, a in enumerate(rest) if not a.startswith("-") and (i == 0 or rest[i - 1] != "-p")]
            if "--value" in rest and len(props) == 1 and len(names) == 1:
                return _done(0, show(names[0], props[0]) + "\n")
            return _done(0, show_many(names, ",".join(props).split(",")))
        if verb == "reset-failed":
            reset_failed(*rest)
            return _done()
    except OSError as e:
        return _done(1, stderr=str(e))
    return _done(1, stderr=f"systemctl {verb} isn't something the panel does on Windows.")


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
def unit_state(unit_name, verb="is-active", timeout=None):
    """systemd's one-word answer about a unit: "active", "inactive", "failed", "enabled", ..."""
    unit_name = _full(unit_name)
    if verb == "is-enabled":
        if unit_name.endswith(".timer"):
            return "enabled" if _task_exists(unit_name) else "disabled"
        try:
            cfg = _win.service_config(_service_name(unit_name))
        except OSError:
            cfg = None
        if cfg is None:
            return "not-found"
        return "enabled" if cfg["start"] == "auto" else "disabled"
    return _states(unit_name)[0]


def state(server, timeout=None):
    """"active", "inactive", "failed", "activating", ... for a game server."""
    return unit_state(unit(server), timeout=timeout)


def is_active(server):
    return state(server) == "active"


def _utc(epoch):
    if not epoch:
        return ""
    t = datetime.datetime.fromtimestamp(float(epoch), datetime.timezone.utc)
    return f"{units.DAYS[t.weekday()]} {t:%Y-%m-%d %H:%M:%S} UTC"


def _properties(unit_name):
    unit_name = _full(unit_name)
    spec = units.load(unit_name)
    active, sub, status = _states(unit_name)
    props = {"Id": unit_name, "LoadState": "loaded" if spec is not None else "not-found",
             "ActiveState": active, "SubState": sub, "Result": status.get("result", "success") or "success",
             "NRestarts": str(status.get("restarts", 0) or 0), "MainPID": str(status.get("pid", 0) or 0),
             "ExecMainPID": str(status.get("pid", 0) or 0),
             "InvocationID": status.get("invocation", ""),
             "ExecMainStatus": str(status.get("exit_status", "") or 0),
             "ExecStart": units.get(spec, "Service", "ExecStart", ""),
             "WorkingDirectory": units.get(spec, "Service", "WorkingDirectory", ""),
             "Description": units.get(spec, "Unit", "Description", ""),
             "MemoryCurrent": "[not set]", "CPUUsageNSec": "[not set]", "MemoryMax": "infinity",
             "CPUQuotaPerSecUSec": "infinity", "ActiveEnterTimestampMonotonic": "0",
             "ActiveEnterTimestamp": "", "NextElapseUSecRealtime": "", "LastTriggerUSec": ""}
    if active == "active":
        usage = _json(unit_name, "usage.json")
        if usage and time.time() - float(usage.get("at", 0) or 0) < 30:
            props["MemoryCurrent"] = str(int(usage.get("memory") or 0))
            props["CPUUsageNSec"] = str(int(usage.get("cpu_nsec") or 0))
        if status.get("started_monotonic"):
            props["ActiveEnterTimestampMonotonic"] = str(int(float(status["started_monotonic"]) * 1e6))
        props["ActiveEnterTimestamp"] = _utc(status.get("started"))
    if unit_name.endswith(".timer"):
        nxt = _next(unit_name)
        props["NextElapseUSecRealtime"] = _utc(nxt.timestamp()) if nxt else ""
        last = _json(_timer_service(unit_name), "status.json").get("last_trigger")
        props["LastTriggerUSec"] = _utc(last)
    return props


def show(unit_name, prop, *options, timeout=None):
    """One property of a unit, as `systemctl show -p PROP --value` prints it."""
    value = _properties(unit_name).get(prop, "")
    if prop == "ActiveEnterTimestamp" and "--timestamp=unix" in options:
        started = _states(unit_name)[2].get("started")
        return f"@{int(started)}" if started and value else ""
    return value


def exec_start(server):
    """The command line the server's unit starts."""
    return units.get(units.load(unit(server)), "Service", "ExecStart", "")


def working_dir(server):
    """The folder the server's unit runs in, or "" when there is no such unit."""
    return units.get(units.load(unit(server)), "Service", "WorkingDirectory", "")


def show_many(units, props, timeout=10):
    """Several units and properties at once, in `systemctl show`'s form: blocks of KEY=VALUE
    separated by blank lines. Times are in UTC."""
    blocks = []
    for u in units:
        p = _properties(u)
        blocks.append("\n".join(f"{k}={p.get(k, '')}" for k in props))
    return "\n\n".join(blocks) + "\n"


def started_at(server, timeout=10):
    """When the server last started (Unix time), or None."""
    active, _sub, status = _states(unit(server))
    return int(status["started"]) if active == "active" and status.get("started") else None


def _owned_units():
    try:
        return [n for n in os.listdir(paths.UNIT_DIR) if n.endswith(SUFFIX) and "@" not in n]
    except OSError:
        return []


def failed(*patterns, timeout=10):
    """[(unit, load state)] for failed units matching the patterns ("minecraft*")."""
    import fnmatch
    out = []
    for u in sorted(_owned_units()):
        if patterns and not any(fnmatch.fnmatch(u, p) or fnmatch.fnmatch(u, p + SUFFIX) for p in patterns):
            continue
        if _states(u)[0] == "failed":
            out.append((u, "loaded"))
    return out


def reset_failed(*units, timeout=None):
    """Forgets that these units failed."""
    for u in map(_full, units):
        path = os.path.join(paths.journal_dir(u), "status.json")
        data = _json(u, "status.json")
        if data.get("state") in ("failed", "active", "activating", "deactivating") \
                and _win.service_status(_service_name(u))[0] in ("stopped", None):
            data.update(state="inactive", sub="dead", result="success")
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f)
            except OSError:
                pass
    return _done()


def queued_jobs(server, timeout=5):
    """What the server has been asked to do and hasn't finished: ["stop"] while it is being
    stopped on request, else []. (In the notification hook, the runner says whether the stop
    was asked for.)"""
    if os.environ.get("RP_STOP_REQUESTED") == "1":
        return ["stop"]
    status = _json(unit(server), "status.json")
    return ["stop"] if status.get("state") == "deactivating" else []


def name_free(server):
    """False when Windows already has a service of this name, whoever made it."""
    try:
        return _win.service_config(server) is None
    except OSError:
        return False


def unit_files(prefix="minecraft", suffix=SUFFIX):
    """The unit files in the panel's unit folder whose name starts with prefix, e.g.
    ["minecraft-java.service"]. Top level only."""
    try:
        with os.scandir(paths.UNIT_DIR) as it:
            return [e.name for e in it if e.name.startswith(prefix) and e.name.endswith(suffix)]
    except OSError:
        return []


# ---------------------------------------------------------------------------
# Changing things
# ---------------------------------------------------------------------------
def _register(unit_name, spec=None):
    """Makes the Windows service for a unit file (or brings it in line with the file) and gives
    its account its folders. Returns the service name, or None when the unit isn't a service."""
    unit_name = _full(unit_name)
    spec = units.load(unit_name) if spec is None else spec
    if not _is_service_unit(unit_name, spec):
        return None
    name = _service_name(unit_name)
    game = name.startswith(GAME_PREFIX)
    command = subprocess.list2cmdline(_win.runner_command("service", unit_name))
    description = units.get(spec, "Unit", "Description", name)
    journal = paths.journal_dir(unit_name)
    os.makedirs(journal, exist_ok=True)
    account = _win.service_account(name) if game else None
    created = _win.ensure_service(name, command, f"RedstonePanel: {description}",
                                  f"{description}. Managed by RedstonePanel; described by "
                                  f"{paths.unit_file(unit_name)}.", account)
    if game:
        # The server's own account: its folder and its output, nothing else
        folder = units.get(spec, "Service", "WorkingDirectory", "")
        marker = os.path.join(journal, "granted.json")
        try:
            with open(marker, encoding="utf-8") as f:
                granted = json.load(f).get("folder")
        except (OSError, ValueError, AttributeError):
            granted = None
        if created or granted != folder:
            sid = _win.service_sid(name)
            _win.grant(journal, sid)
            if folder and os.path.isdir(folder):
                _win.grant(folder, sid)
                with open(marker, "w", encoding="utf-8") as f:
                    json.dump({"folder": folder}, f)
    return name


def _change(args, elevate=False, quiet=False, timeout=None):
    return systemctl(*args, timeout=timeout)


def control(unit_name, action, elevate=False, quiet=False):
    """start, stop, restart, enable or disable any unit by its full name."""
    unit_name = _full(unit_name)
    if unit_name.endswith(".timer"):
        return systemctl(action, unit_name)
    name = _service_name(unit_name)
    if not quiet:
        _log(f"[SERVICE] {action} {name}")
    try:
        if "@" not in unit_name:
            existing = _win.service_config(name)
            if existing is not None and not _win.is_ours(existing):
                raise _win.NotOurs(f"Windows has a service named {name} that RedstonePanel didn't make; it was left alone.")
        if action in ("start", "restart", "enable") and _register(unit_name) is None:
            if "@" in unit_name or units.load(unit_name) is not None:      # a one-off unit: run it now
                if action != "enable":
                    _win.spawn_detached(_win.runner_command("run", unit_name))
                return _done()
            raise OSError(f"There is no unit {unit_name}.")
        if action == "start":
            _win.start_service(name)
        elif action == "stop":
            _win.stop_service(name)
        elif action == "restart":
            _win.stop_service(name)
            _win.start_service(name)
        elif action == "enable":
            _win.set_start(name, "auto")
        elif action == "disable":
            if _win.service_config(name) is not None:
                _win.set_start(name, "manual")
        else:
            raise OSError(f"Unknown action '{action}'.")
    except OSError as e:
        if not quiet:
            _log(f"[WARNING] {action} {name}: {e}")
        return _done(1, stderr=str(e))
    return _done()


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
    """Brings Windows in line with the unit files: a service for every unit that should have
    one, and none left behind for a unit whose file is gone (what daemon-reload is for)."""
    wanted = set()
    for u in _owned_units():
        try:
            name = _register(u)
        except OSError as e:
            _log(f"[WARNING] Couldn't set up the Windows service for {u}: {e}")
            continue
        if name:
            wanted.add(name.lower())
    try:
        existing = _win.list_services(OWNED_PREFIXES)
    except OSError:
        existing = []
    for name in existing:
        if name.lower() in wanted or name.lower() == _win.PANEL_SERVICE:
            continue
        try:
            cfg = _win.service_config(name)
            if not _win.is_ours(cfg):
                continue                              # not one of ours
            _win.stop_service(name, wait=120)
            _win.delete_service(name)
        except OSError as e:
            _log(f"[WARNING] Couldn't remove the Windows service {name}: {e}")
    return _done()


def restart_panel():
    """Restarts the panel's own service, from outside it. Returns the exit code."""
    _win.spawn_detached(_win.runner_command("exec", "redstonepanel-restart.service", "--",
                                            "python", "-c",
                                            "import sys,time;sys.path.insert(0,r'%s');"
                                            "from core.platform.windows import _win;"
                                            "_win.stop_service('redstonepanel');time.sleep(1);"
                                            "_win.start_service('redstonepanel')" % paths.APP_DIR))
    return 0


def panel_state():
    return {"running": "active", "starting": "activating", "stopping": "deactivating",
            "stopped": "inactive"}.get(_win.service_status(_win.PANEL_SERVICE)[0], "inactive")


# ---------------------------------------------------------------------------
# Schedules (Task Scheduler)
# ---------------------------------------------------------------------------
def _timer_service(timer):
    """The unit a timer starts: its own name with .service."""
    return os.path.splitext(timer)[0] + SUFFIX


def _task_exists(timer):
    try:
        return _win.quiet(["schtasks", "/Query", "/TN", _task_name(timer)], timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _create_task(name, xml):
    fd, path = tempfile.mkstemp(suffix=".xml", prefix="rp-task-")
    try:
        with os.fdopen(fd, "w", encoding="utf-16") as f:
            f.write(xml)
        return _win.quiet(["schtasks", "/Create", "/TN", name, "/XML", path, "/F"], timeout=60)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _next(timer):
    spec = units.load(timer)
    calendar = units.get(spec, "Timer", "OnCalendar")
    if not calendar or not _task_exists(timer):
        return None
    try:
        return units.next_run(calendar)
    except units.CalendarError:
        return None


def enable_schedule_now(timer):
    """Makes (or replaces) the task for a .timer file, without the activity log."""
    spec = units.load(timer)
    calendar = units.get(spec, "Timer", "OnCalendar")
    if not calendar:
        return _done(1, stderr=f"{timer} has no schedule.")
    service = _timer_service(timer)
    run = _win.runner_command("run", service)
    try:
        xml = units.task_xml(run[0], subprocess.list2cmdline(run[1:]), calendar,
                             units.get(spec, "Unit", "Description", timer),
                             catch_up=units.get(spec, "Timer", "Persistent", "false").lower() == "true",
                             start=datetime.datetime.now(), working_dir=paths.APP_DIR)
    except units.CalendarError as e:
        return _done(1, stderr=str(e))
    return _create_task(_task_name(timer), xml)


def enable_schedule(timer):
    """Turns a schedule on with what its file says now."""
    r = enable_schedule_now(timer)
    _log(f"[SERVICE] schedule {os.path.splitext(timer)[0]} "
         + ("set in Task Scheduler" if r.returncode == 0 else f"could not be set: {(r.stderr or r.stdout).strip()}"))
    return r


def disable_schedule(timer):
    """Turns a schedule off: its task is removed."""
    return _win.quiet(["schtasks", "/Delete", "/TN", _task_name(timer), "/F"], timeout=60)


def schedule_enabled(timer):
    return _task_exists(timer)


def next_run(timer):
    """When the schedule fires next ("Mon 2026-11-23 04:00:00", this machine's time), or None."""
    nxt = _next(timer)
    return units.systemd_time(nxt) if nxt else None


def next_run_epoch(timer):
    """When the schedule fires next as Unix time, or None."""
    nxt = _next(timer)
    return int(nxt.timestamp()) if nxt else None


def _ensure_event_task():
    """The task that sends started/stopped/crashed notifications for what game servers write to
    the event log, as the system (so it works while the panel is stopped)."""
    run = _win.runner_command("event")
    xml = units.event_task_xml(run[0], subprocess.list2cmdline(run[1:]), _win.GROUP, (10, 11),
                               "RedstonePanel: notifications when a game server starts, stops or crashes")
    return _create_task(TASK_FOLDER + "notifications", xml)


# ---------------------------------------------------------------------------
# One-off work outside the panel's own service
# ---------------------------------------------------------------------------
def start_later(server, seconds=20, timeout=10):
    """Starts the server in `seconds` seconds, whatever happens to the caller meanwhile."""
    _win.spawn_detached(_win.runner_command("later", str(int(seconds)), unit(server)))
    return _done()


def can_detach():
    return True


def run_detached(name, command, description=None, low_priority=False, no_block=False, timeout=30,
                 program="systemd-run"):
    """Runs command in a process of its own, its output kept as unit `name`, so it outlives the
    panel's service. Returns a CompletedProcess."""
    unit_name = name if name.endswith(SUFFIX) else name + SUFFIX
    try:
        _win.spawn_detached(_win.runner_command("exec", unit_name, "--", *command))
    except OSError as e:
        return _done(1, stderr=str(e))
    return _done()


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------
def set_runtime_limits(server, memory_max=None, cpu=None):
    """Not in the Windows preview: limits on a running server come with 1.23 (Job Objects)."""
    return _done(1, stderr="Limits on a running server aren't in the Windows preview yet.")


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------
def _records(unit_name, since=None, invocation=None):
    """[(time, run id, text)] the unit printed, oldest first, from output.log and its rotations."""
    unit_name = _full(unit_name) if unit_name else unit_name
    base = os.path.join(paths.journal_dir(unit_name), "output.log")
    out = []
    for path in [f"{base}.{i}" for i in (3, 2, 1)] + [base]:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    parts = line.rstrip("\n").split(" ", 2)
                    if len(parts) < 2:
                        continue
                    try:
                        t = float(parts[0])
                    except ValueError:
                        continue
                    if since and t < since:
                        continue
                    if invocation and parts[1] != invocation:
                        continue
                    out.append((t, parts[1], parts[2] if len(parts) > 2 else ""))
        except OSError:
            continue
    return out


def log(unit_name=None, lines=None, output="cat", since=None, invocation=None, quiet=False, timeout=None):
    """What a unit printed, as one string. See log_result()."""
    return log_result(unit_name, lines, output, since, invocation, quiet, timeout).stdout


def log_result(unit_name=None, lines=None, output="cat", since=None, invocation=None, quiet=False, timeout=None):
    """What a unit printed, as a CompletedProcess (stdout, stderr, returncode).

    lines: only the last N. output: "cat" (the text alone), "short-unix" (with a Unix time in
    front) or None (a readable time in front). since: a Unix time. invocation: one run of the
    unit in place of the unit name."""
    if invocation and not unit_name:
        return _done(1, stderr="A run needs its unit on Windows.")
    records = _records(unit_name, since, invocation)
    if lines is not None:
        records = records[-int(lines):] if int(lines) > 0 else []
    name = _service_name(unit_name or "")
    if output == "cat":
        text = "".join(f"{r[2]}\n" for r in records)
    elif output == "short-unix":
        text = "".join(f"{r[0]:.6f} {os.environ.get('COMPUTERNAME', 'windows')} {name}[0]: {r[2]}\n" for r in records)
    else:
        text = "".join(f"{time.strftime('%b %d %H:%M:%S', time.localtime(r[0]))} "
                       f"{os.environ.get('COMPUTERNAME', 'windows')} {name}[0]: {r[2]}\n" for r in records)
    return _done(0, text)


def run_log(server, invocation=None, lines=300, timeout=10):
    """What the server printed in one run, or its last lines when the run isn't known."""
    records = _records(unit(server), invocation=invocation)
    return "".join(f"{r[2]}\n" for r in records[-int(lines):])
