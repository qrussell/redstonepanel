"""Minecraft servers on this machine that the panel doesn't manage ("custom installations").

Someone who installs the panel on a machine that already runs a server, started by their own
systemd unit, Windows service, a screen session or a script, should see it in the panel and be
able to hand it over. This module only LOOKS: it changes nothing. A running program counts as a
Minecraft server when it is Java or the Bedrock server and the folder it runs in has a
server.properties; servers the panel manages (by folder or by service name) are left out.
"""
import os

from core import manager
from core.platform import services, system

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


def describe(program, managed=()):
    """What the panel can tell about one running program, or None when it isn't a Minecraft
    server or is one of the panel's own."""
    cwd, argv = program.get("cwd") or "", program.get("argv") or []
    base = os.path.basename((program.get("exe") or (argv[0] if argv else "")).replace("\\", "/"))
    if base in BEDROCK_PROGRAMS:
        edition = "Bedrock"
    elif base in JAVA_PROGRAMS:
        edition = "Java"
    else:
        return None
    if not cwd or not os.path.isfile(os.path.join(cwd, "server.properties")):
        return None
    service = program.get("service") or ""
    if any(_same(cwd, d) for d in managed) or (service and os.path.exists(manager._unit_path(service[:-len(".service")] if service.endswith(".service") else service))):
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
            "pid": program.get("pid"), "since": program.get("started"), "jar": jar, "heap": _heap(argv),
            "java": program.get("exe") if edition == "Java" else None}


def scan():
    """[{"folder", "edition", "port", "version", "world", "service", "started_by", "user", "pid",
    "since", "jar", "heap", "java"}] for each Minecraft server running here that isn't the
    panel's, one per folder, sorted by port."""
    managed = _managed_folders()
    found = {}
    for program in system.game_programs():
        info = describe(program, managed)
        if info and os.path.normcase(os.path.realpath(info["folder"])) not in found:
            found[os.path.normcase(os.path.realpath(info["folder"]))] = info
    return sorted(found.values(), key=lambda f: (f["port"], f["folder"]))
