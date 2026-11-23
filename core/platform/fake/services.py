"""Pretend services: states live in fake.state, changes are recorded in fake.calls."""
from core.platform import fake, paths
from core.platform.fake._result import done

SUFFIX = ".service"


def unit(server):
    return f"{server}{SUFFIX}"


def _entry(unit_name):
    if unit_name.endswith(SUFFIX) and unit_name[:-len(SUFFIX)] in fake.state.servers:
        return fake.state.servers[unit_name[:-len(SUFFIX)]]
    if unit_name in fake.state.servers:
        return fake.state.servers[unit_name]
    return fake.state.units.get(unit_name, {})


def _set(unit_name, **values):
    name = unit_name[:-len(SUFFIX)] if unit_name.endswith(SUFFIX) else unit_name
    target = fake.state.servers if name in fake.state.servers or name.startswith("minecraft") else fake.state.units
    target.setdefault(name if target is fake.state.servers else unit_name, {}).update(values)


def available():
    return fake.state.service_manager


def prepare():
    fake.record("services.prepare")


def systemctl(*args, timeout=None, env=None):
    fake.record("services.systemctl", *args)
    return done()


def unit_state(unit_name, verb="is-active", timeout=None):
    e = _entry(unit_name)
    if verb == "is-enabled":
        return "enabled" if e.get("enabled") else "disabled"
    return e.get("state", "inactive")


def state(server, timeout=None):
    return unit_state(unit(server))


def is_active(server):
    return state(server) == "active"


def show(unit_name, prop, *options, timeout=None):
    return str(_entry(unit_name).get(prop, ""))


def exec_start(server):
    return fake.state.servers.get(server, {}).get("exec_start", "")


def working_dir(server):
    return fake.state.servers.get(server, {}).get("working_dir", "")


def show_many(units, props, timeout=10):
    blocks = []
    for u in units:
        e = dict(_entry(u), Id=u)
        blocks.append("\n".join(f"{p}={e.get(p, '')}" for p in props))
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def started_at(server, timeout=10):
    return fake.state.servers.get(server, {}).get("started_at")


def failed(*patterns, timeout=10):
    out = [(unit(n), "loaded") for n, e in fake.state.servers.items() if e.get("state") == "failed"]
    return out + [(u, e.get("load", "loaded")) for u, e in fake.state.units.items() if e.get("state") == "failed"]


def reset_failed(*units, timeout=None):
    fake.record("services.reset_failed", *units)
    for u in units:
        if _entry(u).get("state") == "failed":
            _set(u, state="inactive")
    return done()


def queued_jobs(server, timeout=5):
    return list(fake.state.servers.get(server, {}).get("jobs", []))


def name_free(server):
    return server not in getattr(fake.state, "foreign_services", ())


def unit_files(prefix="minecraft", suffix=SUFFIX):
    return [unit(n) for n in fake.state.servers if unit(n).startswith(prefix) and unit(n).endswith(suffix)]


_AFTER = {"start": {"state": "active"}, "restart": {"state": "active"}, "stop": {"state": "inactive"},
          "enable": {"enabled": True}, "disable": {"enabled": False}}


def control(unit_name, action, elevate=False, quiet=False):
    fake.record(f"services.{action}", unit_name[:-len(SUFFIX)] if unit_name.endswith(SUFFIX) else unit_name)
    _set(unit_name, **_AFTER.get(action, {}))
    return done() if quiet else 0


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
    fake.record("services.reload")
    return done() if quiet else 0


def restart_panel():
    fake.record("services.restart_panel")
    return 0


def panel_state():
    return unit_state("redstonepanel.service")


def enable_schedule(timer):
    fake.record("services.enable_schedule", timer)
    fake.state.schedules.setdefault(timer, {"next": None})["enabled"] = True


def enable_schedule_now(timer):
    enable_schedule(timer)
    return done()


def disable_schedule(timer):
    fake.record("services.disable_schedule", timer)
    fake.state.schedules.setdefault(timer, {"next": None})["enabled"] = False
    return done()


def schedule_enabled(timer):
    return bool(fake.state.schedules.get(timer, {}).get("enabled"))


def next_run_epoch(timer):
    s = fake.state.schedules.get(timer, {})
    return s.get("next") if s.get("enabled") else None


def next_run(timer):
    at = next_run_epoch(timer)
    if at is None:
        return None
    import time
    return time.strftime("%a %Y-%m-%d %H:%M:%S UTC", time.gmtime(at))


def start_later(server, seconds=20, timeout=10):
    fake.record("services.start_later", server, seconds)
    return done()


def can_detach():
    return fake.state.service_manager


def run_detached(name, command, description=None, low_priority=False, no_block=False, timeout=30, program=None):
    fake.record("services.run_detached", name, tuple(command))
    fake.state.detached.append((name, list(command)))
    return done()


def set_runtime_limits(server, memory_max=None, cpu=None):
    fake.record("services.set_runtime_limits", server, memory_max, cpu)
    fake.state.limits[server] = (memory_max, cpu)
    return done()


def log_result(unit_name=None, lines=None, output="cat", since=None, invocation=None, quiet=False, timeout=None):
    text = list(_entry(unit_name or "").get("log", []))
    if lines is not None:
        text = text[-int(lines):]
    return done("\n".join(text) + ("\n" if text else ""))


def log(unit_name=None, lines=None, output="cat", since=None, invocation=None, quiet=False, timeout=None):
    return log_result(unit_name, lines, output, since, invocation, quiet, timeout).stdout


def run_log(server, invocation=None, lines=300, timeout=10):
    return log(unit(server), lines=lines)


assert paths      # the fake keeps the real path names; tests point them at temp folders
