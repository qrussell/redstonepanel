"""Minecraft servers on this machine that the panel doesn't manage ("custom installations").

Someone who installs the panel on a machine that already runs a server, started by their own
systemd unit, Windows service, a screen session or a script, should see it in the panel and be
able to hand it over. This module only LOOKS: it changes nothing. A running program counts as a
Minecraft server when it is Java or the Bedrock server and the folder it runs in has a
server.properties; servers the panel manages (by folder or by service name) are left out.
"""
import json
import os
import time

from core import manager
from core.platform import services, system
from core.settings import DATA_DIR

RECORD_DIR = os.path.join(DATA_DIR, "found")          # one NAME.json per server taken over: how to undo it
STOP_WAIT = 120
START_WAIT = 5                                        # a server that can't start fails within seconds


class FoundError(ValueError):
    """Why a found server can't be handed to the panel, or handed back (safe to show)."""

BEDROCK_PROGRAMS = ("bedrock_server", "bedrock_server.exe")
JAVA_PROGRAMS = ("java", "java.exe", "javaw.exe")


def _same(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def _managed_folders():
    folders = []
    for unit in services.unit_files():
        name = unit[:-len(".service")] if unit.endswith(".service") else unit
        try:
            d = manager.get_server_dir(name)
        except Exception:
            d = ""
        if d:
            folders.append(d)
    return folders


def _jar(argv):
    """The jar a Java command line runs ("" for a class path or an @args file start)."""
    for i, a in enumerate(argv):
        if a == "-jar" and i + 1 < len(argv):
            return argv[i + 1]
    return ""


def _heap(argv):
    for a in argv:
        if a.startswith("-Xmx"):
            return a[4:]
    return ""


def _xms(argv):
    for a in argv:
        if a.startswith("-Xms"):
            return a[4:]
    return ""


def _size(heap):
    """A heap size ("512M", "2G") in bytes."""
    return int(heap[:-1]) * {"k": 1 << 10, "m": 1 << 20, "g": 1 << 30}[heap[-1].lower()]


def _heap_args(info):
    """The heap the server had, for the panel's unit. A server started with only -Xmx gets a
    starting heap no bigger than that (Java refuses to start otherwise)."""
    from core.manager import HEAP_RE, default_heap
    xmx, xms = info.get("heap") or "", info.get("heap_start") or ""
    if not HEAP_RE.match(xmx):
        return {}
    if not HEAP_RE.match(xms) or _size(xms) > _size(xmx):
        xms = default_heap()["xms"]
        if _size(xms) > _size(xmx):
            xms = xmx
    return {"xms": xms, "xmx": xmx}


def _panel_units():
    return set(services.unit_files())


def describe(program, managed=(), panel_units=None):
    """What the panel can tell about one running program, or None when it isn't a Minecraft
    server or is one of the panel's own."""
    cwd, argv = program.get("cwd") or "", program.get("argv") or []
    base = os.path.basename((program.get("exe") or (argv[0] if argv else "")).replace("\\", "/")).lower()
    if base in BEDROCK_PROGRAMS:
        edition = "Bedrock"
    elif base in JAVA_PROGRAMS:
        edition = "Java"
    else:
        return None
    if not cwd or not os.path.isfile(os.path.join(cwd, "server.properties")):
        return None
    service = program.get("service") or ""
    # The panel's own: by folder, or by a service whose unit file is one of the panel's. Somebody
    # else's unit lives in the same systemd folder, so "the unit file exists" says nothing.
    units = _panel_units() if panel_units is None else panel_units
    if any(_same(cwd, d) for d in managed) or (service and (service in units or f"{service}.service" in units)):
        return None
    props = manager.read_properties(cwd)
    port = str(props.get("server-port", "")).strip()
    try:
        version = manager.server_version(cwd, edition == "Java").get("version")
    except Exception:
        version = None
    jar = _jar(argv)
    return {"folder": cwd, "edition": edition, "port": int(port) if port.isdigit() else (25565 if edition == "Java" else 19132),
            "version": version, "world": props.get("level-name") or "world", "service": service,
            "started_by": service or "a terminal, a script or screen", "user": program.get("user") or "",
            "pid": program.get("pid"), "since": program.get("started"), "jar": jar, "heap": _heap(argv), "heap_start": _xms(argv),
            "java": program.get("exe") if edition == "Java" else None}


def scan():
    """[{"folder", "edition", "port", "version", "world", "service", "started_by", "user", "pid",
    "since", "jar", "heap", "heap_start", "java"}] for each Minecraft server running here that isn't the
    panel's, one per folder, sorted by port."""
    managed, units = _managed_folders(), _panel_units()
    found = {}
    for program in system.game_programs():
        info = describe(program, managed, units)
        if info and os.path.normcase(os.path.realpath(info["folder"])) not in found:
            found[os.path.normcase(os.path.realpath(info["folder"]))] = info
    return sorted(found.values(), key=lambda f: (f["port"], f["folder"]))


# ---------------------------------------------------------------------------
# Handing one to the panel, and back
# ---------------------------------------------------------------------------
def _record_path(name):
    if not manager.SERVICE_NAME_RE.match(name or ""):
        raise FoundError("Unknown server.")
    return os.path.join(RECORD_DIR, f"{name}.json")


def record(name):
    """How the server called `name` was running before the panel took it over, or None."""
    try:
        with open(_record_path(name)) as f:
            rec = json.load(f)
        return rec if isinstance(rec, dict) else None
    except (OSError, ValueError, FoundError):
        return None


def _save(name, rec):
    os.makedirs(RECORD_DIR, mode=0o700, exist_ok=True)
    tmp = _record_path(name) + ".tmp"
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(rec, f)
    os.replace(tmp, _record_path(name))


def _running_in(folder):
    return [p for p in system.game_programs() if p.get("cwd") and _same(p["cwd"], folder)]


def check(info):
    """Raises FoundError with the reason when the panel can't run this found server as it is.
    Changes nothing."""
    folder, edition = info["folder"], info["edition"]
    error = system.found_folder_problem(folder)
    if error:
        raise FoundError(error)
    if edition == "Bedrock":
        manager.require_bedrock_machine()
        if "java" in folder.lower():
            raise FoundError('A Bedrock server\'s folder can\'t have "java" in its path: the panel uses that word to tell '
                             'the two kinds of server apart.')
        if not os.path.isfile(os.path.join(folder, "bedrock_server")):
            raise FoundError(f"There is no bedrock_server program in {folder}.")
    else:
        jar = info.get("jar") or ""
        if not jar:
            raise FoundError("This Java server isn't started with -jar (a mod loader's own start script, most likely). "
                             "The panel can't take it over as it is; importing it from a backup file still works.")
        if not os.path.isfile(jar if os.path.isabs(jar) else os.path.join(folder, jar)):
            raise FoundError(f"Can't find the server's jar file, {jar}, in {folder}.")
        if os.path.isabs(jar) and not _same(os.path.dirname(jar), folder):
            raise FoundError(f"The server's jar file, {jar}, is outside its folder.")
        if os.path.basename(jar) != "server.jar" and os.path.lexists(os.path.join(folder, "server.jar")):
            raise FoundError(f"The server runs {os.path.basename(jar)}, but its folder also has a server.jar. "
                             f"Remove or rename the one that isn't used.")
    if info["port"] in manager._used_ports():
        raise FoundError(f"Port {info['port']} is already used by a server the panel manages.")
    if not info.get("service") and _running_in(folder):
        raise FoundError("This server wasn't started by a service, so the panel can't stop it safely. Stop it where you "
                         "started it (type stop in its console), then choose Manage again within the hour.")


def _rules_already_open(edition, folder):
    """The firewall rules the server needs that its owner had opened before the panel came."""
    from core import host
    from core.platform import firewall
    if not firewall.installed():
        return []
    text = firewall.status_text()
    return [[port, proto] for port, proto in sorted(manager.firewall_rules(edition, manager.read_properties(folder)))
            if host.ufw_allows_spec(text, port, proto)]


def _stop_old(info):
    """Stops the service that ran it and keeps it from starting again. The unit itself stays."""
    service = info.get("service") or ""
    if not service:
        return {"service": "", "was_enabled": False}
    was_enabled = services.other_enabled(service)
    services.other_control(service, "stop")
    deadline = time.time() + STOP_WAIT
    while _running_in(info["folder"]) and time.time() < deadline:
        time.sleep(1)
    if _running_in(info["folder"]):
        services.other_control(service, "start")
        raise FoundError(f"{service} didn't stop within {STOP_WAIT} seconds, so nothing was changed.")
    services.other_control(service, "disable")
    return {"service": service, "was_enabled": was_enabled}


def _last_words(name):
    """What the server printed before it gave up, for the message (" It said: ...", or "")."""
    try:
        text = services.run_log(name, lines=40)
        lines = [l.strip() for l in (text if isinstance(text, str) else "\n".join(text or [])).splitlines() if l.strip()]
    except Exception:
        lines = []
    return (" It said: " + " | ".join(lines[-4:])[:600]) if lines else ""


def _java_for(info):
    """The Java the panel's service runs the server with: the one the panel would pick for its
    version, or, when the version doesn't say or that Java isn't on this machine, the very Java
    the server was running with."""
    import re
    java = manager.java_for_folder(info["folder"])
    if java and os.path.isfile(java):
        return java
    own = info.get("java") or ""
    if os.path.isfile(own) and re.fullmatch(r"[A-Za-z0-9_.:+\\/-]+", own) \
            and os.path.basename(own.replace("\\", "/")).lower() in JAVA_PROGRAMS:
        return own
    raise FoundError("The panel has no Java to run this server with, and can't use the one it was started with"
                     + (f" ({own})" if own else "") + ". Install a Java version on the Maintenance tab of any Java "
                     "server, or under New server, then choose Manage again.")


def manage(folder, actor="?"):
    """Hands a found server to the panel, in place: its own service is stopped and turned off
    (not deleted), and the panel runs the server from the same folder under a service of its
    own. No file is copied or moved. Returns {"name", "port", "folder"}; undo() reverses it."""
    info = next((f for f in listing() if _same(f["folder"], folder)), None)
    if not info:
        raise FoundError("That server isn't among the ones found on this machine any more. Look again.")
    folder, edition = info["folder"], info["edition"].lower()
    check(info)
    name, _ = manager._reserve_name(edition)
    rec = {"name": name, "folder": folder, "edition": edition, "port": info["port"], "owner": system.owner_of(folder),
           "service": info.get("service") or "", "was_enabled": False, "jar_was": "", "at": int(time.time()), "by": actor}
    try:
        manager.write_log(f"[FOUND] {actor} is handing the {info['edition']} server in {folder} to the panel as {name}.")
        rec.update(_stop_old(info))
        _save(name, rec)                                  # from here on undo() can put things back
        try:
            if edition == "java":
                jar = os.path.basename(info["jar"])
                if jar != "server.jar":
                    os.rename(os.path.join(folder, jar), os.path.join(folder, "server.jar"))
                    rec["jar_was"] = jar
                    _save(name, rec)
                java = _java_for(info)
            else:
                java = None
                system.make_executable(os.path.join(folder, "bedrock_server"))
                manager.write_screenrc(folder)
            heap = _heap_args(info)
            rec["firewall_had"] = _rules_already_open(edition, folder)
            _save(name, rec)
            manager.open_server_firewall(name, edition, folder)
            manager.ensure_minecraft_user(folder)
            system.give_to_game_account(folder)
            manager._write_unit(name, folder, edition, **heap, **({"java": java} if java else {}), jvm={})
            services.enable(name)
            system.give_to_game_account(folder)       # Windows: the server's account exists only now
            services.start(name)
            deadline = time.time() + START_WAIT
            while time.time() < deadline and services.state(name) in ("active", "activating"):
                time.sleep(1)
            if not services.is_active(name):
                raise FoundError("The server didn't start under the panel's service, so it was put back as it was. "
                                 "Nothing in its folder was changed." + _last_words(name))
        except Exception as e:
            try:
                undo(name, actor="the panel, because taking it over failed")
            except Exception as again:
                manager.write_log(f"[FOUND] Couldn't put {folder} back as it was: {again}")
            raise e if isinstance(e, FoundError) else FoundError(f"Couldn't take the server over: {e}. It was put back as it was.")
        _forget_seen(folder)
        manager.write_log(f"[FOUND] {name} now runs {folder} (port {info['port']})."
                          + (f" {rec['service']} was stopped and turned off, not deleted." if rec["service"] else ""))
        return {"name": name, "port": info["port"], "folder": folder}
    finally:
        manager._release_name(name)


def undo(name, actor="?"):
    """Hands a taken-over server back: the panel's service goes, the folder goes back to its
    owner, the jar gets its name back, and the service that ran it is turned on and started
    again. The folder and the world are never deleted."""
    rec = record(name)
    if not rec:
        raise FoundError(f"{name} wasn't taken over from another setup, so there is nothing to undo.")
    folder = rec["folder"]
    if os.path.exists(manager._unit_path(name)):
        manager.remove_server(name, keep_world=True)
    # Removing it from the panel closes its ports; the ones its owner had opened are opened again
    manager.open_firewall_rules({(str(p), str(proto)) for p, proto in rec.get("firewall_had") or []})
    note = os.path.join(folder, manager.REMOVED_INFO)
    if os.path.isfile(note):
        os.remove(note)                                   # it isn't a "removed server": it goes back to its owner
    if rec.get("jar_was") and os.path.isfile(os.path.join(folder, "server.jar")) \
            and not os.path.lexists(os.path.join(folder, rec["jar_was"])):
        os.rename(os.path.join(folder, "server.jar"), os.path.join(folder, rec["jar_was"]))
    if os.path.isdir(folder):
        system.give_back(folder, rec["owner"])
    if rec.get("service"):
        if rec.get("was_enabled"):
            services.other_control(rec["service"], "enable")
        services.other_control(rec["service"], "start")
    try:
        os.remove(_record_path(name))
    except OSError:
        pass
    manager.write_log(f"[FOUND] {actor} handed {name} back: {folder} runs "
                      + (f"under {rec['service']} again." if rec.get("service") else "as it did before (start it yourself)."))
    return {"folder": folder, "service": rec.get("service") or ""}


def taken_over():
    """[{"name", "folder", "service", "at", "by"}] for servers the panel took over and still runs."""
    out = []
    try:
        names = sorted(n[:-len(".json")] for n in os.listdir(RECORD_DIR) if n.endswith(".json"))
    except OSError:
        return out
    for name in names:
        rec = record(name)
        if rec and os.path.exists(manager._unit_path(name)):
            out.append({k: rec.get(k) for k in ("name", "folder", "service", "at", "by")})
    return out


# ---------------------------------------------------------------------------
# Seen lately: a server someone stops by hand, to hand it over, stays listed for an hour
# ---------------------------------------------------------------------------
SEEN_FILE = os.path.join(DATA_DIR, "found-seen.json")
SEEN_FOR = 3600


def _load_seen():
    try:
        with open(SEEN_FILE) as f:
            seen = json.load(f)
        return seen if isinstance(seen, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_seen(seen):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = SEEN_FILE + ".tmp"
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(seen, f)
    os.replace(tmp, SEEN_FILE)


def _forget_seen(folder):
    seen = _load_seen()
    if seen.pop(os.path.normcase(os.path.realpath(folder)), None) is not None:
        _save_seen(seen)


def listing(now=None):
    """scan(), plus servers seen running within the last hour that have stopped since (each with
    "running": False), so one that had to be stopped by hand can still be handed over."""
    now = now or time.time()
    running = scan()
    seen, managed = _load_seen(), _managed_folders()
    fresh = {}
    for f in running:
        fresh[os.path.normcase(os.path.realpath(f["folder"]))] = dict(f, seen_at=now)
    out = [dict(f, running=True) for f in running]
    for key, f in seen.items():
        if key in fresh or not isinstance(f, dict) or now - f.get("seen_at", 0) > SEEN_FOR:
            continue
        if not os.path.isfile(os.path.join(f.get("folder", ""), "server.properties")) \
                or any(_same(f["folder"], d) for d in managed):
            continue
        fresh[key] = f
        out.append(dict({k: v for k, v in f.items() if k != "seen_at"}, running=False, pid=None))
    if fresh != seen:
        _save_seen(fresh)
    return sorted(out, key=lambda f: (f["port"], f["folder"]))
