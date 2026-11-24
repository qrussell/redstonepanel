"""Whitelist (Java whitelist.json) and allowlist (Bedrock allowlist.json) of a game server.

A running server is changed through its console, so the change applies at once and the server
writes the file itself: Java over RCON ("whitelist add NAME"), Bedrock through screen
("allowlist add NAME"). Otherwise the panel edits the file, which the server reads when it next
starts. Java entries need the player's UUID; it comes from the player log, usercache.json, the
offline-mode formula, or Mojang's API, in that order.
"""
import hashlib
import json
import os
import re
import uuid

from core import backups, manager, playerlog, rcon
from core.platform import system

os = system.fs()          # Python's os; on Windows, the same calls made safe without dir_fd (core/platform)

JAVA_NAME = re.compile(r"^[A-Za-z0-9_]{3,16}$")
GAMERTAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ]{0,14}[A-Za-z0-9]?(#\d{1,4})?$")


class WhitelistError(ValueError):
    """A change that can't be made; the message is safe to show."""


def _paths(server):
    d = manager.get_server_dir(server)
    java = manager._is_java(server)
    return d, java, os.path.join(d, "whitelist.json" if java else "allowlist.json")


def _prop_key(props, java):
    if java:
        return "white-list"
    return "white-list" if "white-list" in props and "allow-list" not in props else "allow-list"


def info(server):
    d, java, path = _paths(server)
    props = manager.read_properties(d)
    key = _prop_key(props, java)
    entries = [e for e in manager._load_json_list(path) if isinstance(e, dict) and e.get("name")]
    seen = playerlog.summary(server)
    players = []
    for e in entries:
        s = seen.get(str(e["name"]).lower(), {})
        players.append({"name": e["name"], "id": e.get("uuid") or e.get("xuid") or None,
                        "last_join": s.get("last_join")})
    players.sort(key=lambda p: p["name"].lower())
    return {
        "enabled": str(props.get(key, "false")).strip().lower() == "true",
        "setting": key,
        "file": os.path.basename(path),
        "edition": "java" if java else "bedrock",
        "running": backups._is_active(server),
        "live": _console(server, d, java) is not None,
        "players": players,
    }


def _console(server, d, java):
    """A function that runs one console command, or None if this server can't take one now."""
    if not backups._is_active(server):
        return None
    if java:
        send = backups._java_rcon(d)
        return (lambda cmd: send(cmd)) if send else None
    return (lambda cmd: "" if backups._screen(server, cmd) else None)


def _check_name(name, java):
    name = str(name or "").strip()
    if not (JAVA_NAME if java else GAMERTAG).match(name):
        raise WhitelistError(f"'{name}' isn't a valid {'Java player name' if java else 'gamertag'}.")
    return name


def offline_uuid(name):
    """The UUID an offline-mode Java server gives a player."""
    h = bytearray(hashlib.md5(f"OfflinePlayer:{name}".encode()).digest())
    h[6] = (h[6] & 0x0F) | 0x30
    h[8] = (h[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(h)))


def _java_uuid(server, d, name):
    n = name.lower()
    seen = playerlog.summary(server).get(n, {})
    if seen.get("id"):
        return seen["id"]
    for e in manager._load_json_list(os.path.join(d, "usercache.json")):
        if isinstance(e, dict) and str(e.get("name", "")).lower() == n and e.get("uuid"):
            return e["uuid"]
    if str(manager.read_properties(d).get("online-mode", "true")).strip().lower() == "false":
        return offline_uuid(name)
    return manager.get_java_uuid(name)


def _write(path, entries, d):
    # As the game account: the file is the game's (a link there can't reach anything else)
    with system.game_files(), open(path, "w") as f:
        json.dump(entries, f, indent=2)


def add(server, name):
    d, java, path = _paths(server)
    name = _check_name(name, java)
    entries = manager._load_json_list(path)
    if any(isinstance(e, dict) and str(e.get("name", "")).lower() == name.lower() for e in entries):
        return {"message": f"{name} is already on the {'whitelist' if java else 'allowlist'}.", "restart": False}
    console = _console(server, d, java)
    if console:
        try:
            out = console(f"whitelist add {name}" if java else f'allowlist add "{name}"')
        except rcon.RconError as e:
            out = None
            manager.write_log(f"[PLAYERS] RCON whitelist add failed on {server}: {e}")
        if out is not None:
            if java and out and "does not exist" in out.lower():
                raise WhitelistError(f"Minecraft doesn't know a player called {name}.")
            manager.write_log(f"[PLAYERS] Added {name} to {server}'s {'whitelist' if java else 'allowlist'}.")
            return {"message": f"Added {name}. It applies now.", "restart": False}
    if java:
        pid = _java_uuid(server, d, name)
        if not pid:
            raise WhitelistError(f"Couldn't find the UUID for {name}. Check the spelling, or start the "
                                 f"server and try again.")
        entries.append({"uuid": pid, "name": name})
    else:
        e = {"ignoresPlayerLimit": False, "name": name}
        xuid = playerlog.summary(server).get(name.lower(), {}).get("id")
        if xuid:
            e["xuid"] = xuid
        entries.append(e)
    _write(path, entries, d)
    manager.write_log(f"[PLAYERS] Added {name} to {server}'s {os.path.basename(path)}.")
    running = backups._is_active(server)
    return {"message": f"Added {name}." + (" Restart the server for it to apply." if running else ""),
            "restart": running}


def remove(server, name):
    d, java, path = _paths(server)
    name = str(name or "").strip()
    entries = manager._load_json_list(path)
    keep = [e for e in entries if not (isinstance(e, dict) and str(e.get("name", "")).lower() == name.lower())]
    if len(keep) == len(entries):
        raise WhitelistError(f"{name} isn't on the {'whitelist' if java else 'allowlist'}.")
    name = next(str(e["name"]) for e in entries if e not in keep)
    console = _console(server, d, java)
    if console:
        try:
            out = console(f"whitelist remove {name}" if java else f'allowlist remove "{name}"')
        except rcon.RconError as e:
            out = None
            manager.write_log(f"[PLAYERS] RCON whitelist remove failed on {server}: {e}")
        if out is not None:
            manager.write_log(f"[PLAYERS] Removed {name} from {server}'s {'whitelist' if java else 'allowlist'}.")
            return {"message": f"Removed {name}. It applies now; a player already online stays until they leave.",
                    "restart": False}
    _write(path, keep, d)
    manager.write_log(f"[PLAYERS] Removed {name} from {server}'s {os.path.basename(path)}.")
    running = backups._is_active(server)
    return {"message": f"Removed {name}." + (" Restart the server for it to apply." if running else ""),
            "restart": running}
