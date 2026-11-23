"""Reading the panel's unit files on Windows.

The panel describes every game server and every schedule in a small text file, the format
systemd uses (`minecraft-<name>.service`, `redstonepanel-backup@<name>.timer`). On Linux systemd
reads them. Windows has no systemd, so the panel keeps writing the same files (in
<home>\\services) and this module reads the handful of settings the panel ever writes; the
runner (runner.py) and Task Scheduler then do what they say. One description of a server,
whatever the system: that is why the rest of the panel didn't have to change.

Pure Python, no Windows calls: tested on any system.
"""
import datetime
import os
import re

from core.platform import paths

DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def parse(text):
    """Unit text -> {"Section": {"Key": [values...]}}. An empty value clears the key, as in systemd."""
    out, section = {}, None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("[") and line.endswith("]"):
            section = out.setdefault(line[1:-1], {})
            continue
        if section is None or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if value:
            section.setdefault(key, []).append(value)
        else:
            section[key] = []
    return out


def _read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def template_of(unit):
    """("redstonepanel-backup@.service", "minecraft-java") for "redstonepanel-backup@minecraft-java.service";
    (unit, "") when the unit isn't an instance of a template."""
    m = re.fullmatch(r"([^@]+@)(.+)(\.[a-z]+)", unit)
    return (m.group(1) + m.group(3), m.group(2)) if m else (unit, "")


def dropin_dirs(unit, unit_dir):
    """The folders whose *.conf files add to a unit, least specific first:
    minecraft-.service.d (every minecraft-* unit), then <unit>.d."""
    name, suffix = os.path.splitext(unit)
    dirs = []
    parts = name.split("-")
    for i in range(1, len(parts)):
        dirs.append(os.path.join(unit_dir, "-".join(parts[:i]) + "-" + suffix + ".d"))
    dirs.append(os.path.join(unit_dir, unit + ".d"))
    return dirs


def load(unit, unit_dir=None):
    """The unit's settings with its drop-ins applied and %i / %N filled in, or None when it has
    no file. A template instance (x@name.service) reads x@.service."""
    unit_dir = unit_dir or paths.UNIT_DIR
    template, instance = template_of(unit)
    text = _read(os.path.join(unit_dir, unit))
    if text is None and instance:
        text = _read(os.path.join(unit_dir, template))
    if text is None:
        return None
    texts = [text]
    for d in dropin_dirs(unit, unit_dir):
        try:
            names = sorted(n for n in os.listdir(d) if n.endswith(".conf"))
        except OSError:
            continue
        texts += [t for t in (_read(os.path.join(d, n)) for n in names) if t is not None]
    merged = {}
    for t in texts:
        for section, keys in parse(t).items():
            dest = merged.setdefault(section, {})
            for key, values in keys.items():
                if values:
                    dest.setdefault(key, []).extend(values)
                else:
                    dest[key] = []
    name = os.path.splitext(unit)[0]
    for keys in merged.values():
        for key, values in keys.items():
            keys[key] = [v.replace("%i", instance).replace("%N", name).replace("%n", unit) for v in values]
    return merged


def get(spec, section, key, default=None):
    """The last value of a key (systemd's rule for single-valued settings)."""
    values = (spec or {}).get(section, {}).get(key) or []
    return values[-1] if values else default


def split_command(line):
    """An ExecStart line -> argv. Words are split on spaces; "double quotes" keep spaces together.
    systemd's prefixes (-, +, @, !, :) in front of the program are dropped."""
    line = (line or "").strip().lstrip("-+@!:")
    argv, cur, quoted, had = [], "", False, False
    for ch in line:
        if ch == '"':
            quoted, had = not quoted, True
        elif ch in " \t" and not quoted:
            if cur or had:
                argv.append(cur)
            cur, had = "", False
        else:
            cur += ch
    if cur or had:
        argv.append(cur)
    return argv


def seconds(value, default):
    """"20", "20s", "5min" -> seconds."""
    m = re.fullmatch(r"\s*([0-9]+)\s*(s|sec|m|min|h)?\s*", str(value or ""))
    if not m:
        return default
    return int(m.group(1)) * {"m": 60, "min": 60, "h": 3600}.get(m.group(2) or "s", 1)


def environment(spec):
    """Environment="A=1" "B=2" lines -> {"A": "1", "B": "2"}."""
    env = {}
    for line in (spec or {}).get("Service", {}).get("Environment", []):
        for word in split_command(line):
            if "=" in word:
                k, v = word.split("=", 1)
                env[k] = v
    return env


# ---------------------------------------------------------------------------
# OnCalendar: the patterns the panel writes
# ---------------------------------------------------------------------------
class CalendarError(ValueError):
    pass


def parse_calendar(text):
    """A systemd OnCalendar value the panel writes -> {"days": [0..6] or None, "hours": [...],
    "minute": m, "every": hours between runs or None}.

    The panel writes "*-*-* HH:MM:00" (daily), "*-*-* *:MM:00" (hourly), "*-*-* HH/6:MM:00" and
    "HH/12" (every 6 or 12 hours), "Sun *-*-* HH:MM:00" (weekly, any day name) and
    "*-*-* 00/6:17:00"-style lists; nothing else is understood."""
    m = re.fullmatch(r"\s*(?:([A-Za-z,]+)\s+)?\*-\*-\*\s+(\*|[0-9]{1,2}(?:/[0-9]{1,2})?(?:,[0-9]{1,2})*):([0-9]{1,2})"
                     r"(?::([0-9]{1,2}))?\s*", text or "")
    if not m:
        raise CalendarError(f"Can't read the schedule '{text}'.")
    days = None
    if m.group(1):
        try:
            days = sorted({DAYS.index(d[:3].title()) for d in m.group(1).split(",")})
        except ValueError:
            raise CalendarError(f"Can't read the days in '{text}'.")
    hour, minute = m.group(2), int(m.group(3))
    every = None
    if hour == "*":
        hours, every = list(range(24)), 1
    elif "/" in hour:
        start, step = (int(x) for x in hour.split("/"))
        if not step:
            raise CalendarError(f"Can't read the hours in '{text}'.")
        hours, every = list(range(start, 24, step)), step
    else:
        hours = sorted({int(h) for h in hour.split(",")})
    if minute > 59 or any(h > 23 for h in hours) or not hours:
        raise CalendarError(f"Can't read the time in '{text}'.")
    return {"days": days, "hours": hours, "minute": minute, "every": every}


def next_run(calendar, now=None):
    """The next time (a naive local datetime) the schedule fires after `now`."""
    cal = parse_calendar(calendar) if isinstance(calendar, str) else calendar
    now = now or datetime.datetime.now()
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for offset in range(0, 9):
        d = day + datetime.timedelta(days=offset)
        if cal["days"] is not None and d.weekday() not in cal["days"]:
            continue
        for h in cal["hours"]:
            t = d.replace(hour=h, minute=cal["minute"])
            if t > now:
                return t
    return None


def systemd_time(when):
    """A datetime as systemd prints a timer's next run: "Mon 2026-11-23 04:00:00"."""
    return f"{DAYS[when.weekday()]} {when:%Y-%m-%d %H:%M:%S}"


def task_xml(command, arguments, calendar, description, catch_up=False, start=None, working_dir=None,
             time_limit="PT4H"):
    """The Task Scheduler definition (XML) that runs `command arguments` as the system account on
    a schedule written as OnCalendar. catch_up: run as soon as possible after a missed start
    (systemd's Persistent=true)."""
    cal = parse_calendar(calendar)
    start = start or datetime.datetime(2026, 1, 1)
    first = start.replace(hour=cal["hours"][0], minute=cal["minute"], second=0, microsecond=0)
    if cal["days"] is not None:
        names = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
        days = "".join(f"<{names[d]} />" for d in cal["days"])
        schedule = f"<ScheduleByWeek><DaysOfWeek>{days}</DaysOfWeek><WeeksInterval>1</WeeksInterval></ScheduleByWeek>"
    else:
        schedule = "<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>"
    triggers = []
    if cal["every"] and cal["days"] is None:
        # One trigger that repeats through the day
        triggers.append(
            f"<CalendarTrigger><Repetition><Interval>PT{cal['every']}H</Interval><Duration>P1D</Duration>"
            f"<StopAtDurationEnd>false</StopAtDurationEnd></Repetition>"
            f"<StartBoundary>{first:%Y-%m-%dT%H:%M:%S}</StartBoundary><Enabled>true</Enabled>{schedule}</CalendarTrigger>")
    else:
        for h in cal["hours"]:
            t = first.replace(hour=h)
            triggers.append(f"<CalendarTrigger><StartBoundary>{t:%Y-%m-%dT%H:%M:%S}</StartBoundary>"
                            f"<Enabled>true</Enabled>{schedule}</CalendarTrigger>")
    return _task(command, arguments, "".join(triggers), description, catch_up, working_dir, time_limit)


def event_task_xml(command, arguments, source, event_ids, description, values=None):
    """A task started by entries this source writes to the Application event log. values:
    {name: XPath into the event}, usable as $(name) in arguments."""
    ids = " or ".join(f"EventID={int(i)}" for i in event_ids)
    query = (f"<QueryList><Query Id=\"0\" Path=\"Application\"><Select Path=\"Application\">"
             f"*[System[Provider[@Name='{source}'] and ({ids})]]</Select></Query></QueryList>")
    vq = "".join(f"<Value name=\"{_esc(k)}\">{_esc(v)}</Value>" for k, v in (values or {}).items())
    trigger = (f"<EventTrigger><Enabled>true</Enabled><Subscription>{_esc(query)}</Subscription>"
               + (f"<ValueQueries>{vq}</ValueQueries>" if vq else "") + "</EventTrigger>")
    return _task(command, arguments, trigger, description, False, None, "PT10M", parallel=True)


def _esc(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def _task(command, arguments, triggers, description, catch_up, working_dir, time_limit, parallel=False):
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>{_esc(description)}</Description><Author>RedstonePanel</Author></RegistrationInfo>
  <Triggers>{triggers}</Triggers>
  <Principals><Principal id="Author"><UserId>S-1-5-18</UserId><RunLevel>HighestAvailable</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>{'Parallel' if parallel else 'IgnoreNew'}</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>{'true' if catch_up else 'false'}</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>{time_limit}</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author"><Exec><Command>{_esc(command)}</Command><Arguments>{_esc(arguments)}</Arguments>{
      f'<WorkingDirectory>{_esc(working_dir)}</WorkingDirectory>' if working_dir else ''}</Exec></Actions>
</Task>
"""
