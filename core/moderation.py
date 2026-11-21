"""Kick, ban and unban players on a game server (1.16.2), next to the whitelist.

A running server is told through its console, so the change applies at once: Java over RCON
(kick / ban / pardon), Bedrock through screen (kick). A stopped Java server's ban list is
edited directly (banned-players.json), and the server reads it when it starts. A running Java
server without RCON is refused instead: it only reads its ban list at start and writes it back
when it changes, so a file edit could be lost.

Bedrock has no ban list, so it gets kick only; the allowlist is how to keep someone out.
Bans are by name and UUID only, never by IP address (the player log keeps no addresses either).
"""
import datetime as dt
import os
import re
import subprocess

from core import backups, manager, playerlog, rcon, whitelist

BAN_FILE = "banned-players.json"
REASON_MAX = 100
LIST_RE = re.compile(r"players online:\s*(.*)$", re.S)


class ModerationError(ValueError):
    """An action that can't be done; the message is safe to show."""


def _clean_reason(reason):
    reason = " ".join(str(reason or "").split())        # no newlines or control spacing
    reason = "".join(c for c in reason if c.isprintable())[:REASON_MAX]
    return reason


def _server(server):
    d = manager.get_server_dir(server)
    return d, manager._is_java(server)


def _rcon(d):
    send = backups._java_rcon(d)
    if not send:
        return None

    def call(cmd):
        try:
            return send(cmd) or ""
        except rcon.RconError as e:
            raise ModerationError(f"The server didn't answer over RCON ({e}).")
    return call


def _started_at(server):
    """When the server's unit last started (Unix time), or None."""
    try:
        out = subprocess.run(["systemctl", "show", "-p", "ActiveEnterTimestamp", "--value", "--timestamp=unix",
                              f"{server}.service"], capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return int(out[1:]) if out.startswith("@") and out[1:].isdigit() else None


def online_from_log(server, since=None):
    """Who joined and hasn't left since `since`, from the player log (lags up to 30 s)."""
    last = {}
    for e in playerlog.load(server):
        if e["ev"] in ("join", "leave") and (since is None or e["ts"] >= since):
            last[e["name"].lower()] = e
    return sorted((e["name"] for e in last.values() if e["ev"] == "join"), key=str.lower)


def parse_list(out):
    """Java `list`: 'There are 2 of a max of 20 players online: Steve, Alex' -> ['Steve', 'Alex']."""
    m = LIST_RE.search(rcon.COLOR_RE.sub("", out or ""))
    if not m:
        return None
    return sorted((n.strip() for n in m.group(1).split(",") if n.strip()), key=str.lower)


def online(server):
    """(names, source): source is "rcon" (exact), "log" (from the player log) or None (stopped)."""
    d, java = _server(server)
    if not backups._is_active(server):
        return [], None
    if java:
        call = _rcon(d)
        if call:
            try:
                names = parse_list(call("list"))
            except ModerationError:
                names = None
            if names is not None:
                return names, "rcon"
    return online_from_log(server, _started_at(server)), "log"


def banned(server):
    """The Java ban list: [{name, id, reason, source, created, expires}]; [] for Bedrock."""
    d, java = _server(server)
    if not java:
        return []
    out = []
    for e in manager._load_json_list(os.path.join(d, BAN_FILE)):
        if isinstance(e, dict) and e.get("name"):
            out.append({"name": str(e["name"]), "id": e.get("uuid"), "reason": e.get("reason") or "",
                        "source": e.get("source") or "", "created": e.get("created") or "",
                        "expires": e.get("expires") or "forever"})
    return sorted(out, key=lambda b: b["name"].lower())


def info(server):
    d, java = _server(server)
    names, source = online(server)
    running = backups._is_active(server)
    return {"edition": "java" if java else "bedrock", "running": running,
            "live": running and (not java or backups._java_rcon(d) is not None),
            "online": names, "online_source": source,
            "bans": java, "banned": banned(server), "reason_max": REASON_MAX}


def kick(server, name, reason=""):
    d, java = _server(server)
    name = whitelist._check_name(name, java)
    reason = _clean_reason(reason)
    if not backups._is_active(server):
        raise ModerationError(f"The server isn't running, so nobody is online to kick.")
    if java:
        call = _rcon(d)
        if not call:
            raise ModerationError("RCON isn't set up on this server, so the panel can't kick. "
                                  "Open Live Chat once to set it up.")
        out = call(f"kick {name} {reason}".strip())
        if "no player was found" in out.lower():
            raise ModerationError(f"{name} isn't online.")
    else:
        # Bedrock gives nothing back through screen; a gamertag may contain spaces
        if not backups._screen(server, f'kick "{name}" {reason}'.strip()):
            raise ModerationError("Couldn't reach the server's console (screen).")
    return {"message": f"Kicked {name}." if java else f"Sent the kick for {name}."}


def ban(server, name, reason="", by=""):
    d, java = _server(server)
    if not java:
        raise ModerationError("Bedrock servers have no ban list. To keep someone out, turn on the allowlist "
                              "and leave them off it.")
    name = whitelist._check_name(name, True)
    reason = _clean_reason(reason)
    path = os.path.join(d, BAN_FILE)
    if backups._is_active(server):
        call = _rcon(d)
        if not call:
            raise ModerationError("RCON isn't set up on this server, so the panel can't ban while it runs. "
                                  "Open Live Chat once to set it up, or stop the server first.")
        out = call(f"ban {name} {reason}".strip()).lower()
        if "already banned" in out:
            return {"message": f"{name} is already banned."}
        if "does not exist" in out or "unknown" in out:
            raise ModerationError(f"Minecraft doesn't know a player called {name}.")
        return {"message": f"Banned {name}. They can't join until you unban them."}

    entries = manager._load_json_list(path)
    if any(isinstance(e, dict) and str(e.get("name", "")).lower() == name.lower() for e in entries):
        return {"message": f"{name} is already banned."}
    pid = whitelist._java_uuid(server, d, name)
    if not pid:
        raise ModerationError(f"Couldn't find the UUID for {name}. Check the spelling, or start the server "
                              f"and try again.")
    entries.append({
        "uuid": pid, "name": name,
        "created": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S +0000"),
        "source": f"RedstonePanel ({by})" if by else "RedstonePanel",
        "expires": "forever",
        "reason": reason or "Banned by an operator.",
    })
    whitelist._write(path, entries, d)
    return {"message": f"Banned {name}. It applies when the server starts."}


def unban(server, name):
    d, java = _server(server)
    if not java:
        raise ModerationError("Bedrock servers have no ban list.")
    name = whitelist._check_name(name, True)
    path = os.path.join(d, BAN_FILE)
    entries = manager._load_json_list(path)
    keep = [e for e in entries if not (isinstance(e, dict) and str(e.get("name", "")).lower() == name.lower())]
    if backups._is_active(server):
        call = _rcon(d)
        if not call:
            raise ModerationError("RCON isn't set up on this server, so the panel can't unban while it runs. "
                                  "Open Live Chat once to set it up, or stop the server first.")
        out = call(f"pardon {name}").lower()
        if "nothing changed" in out:
            raise ModerationError(f"{name} isn't banned.")
        return {"message": f"Unbanned {name}. They can join again."}
    if len(keep) == len(entries):
        raise ModerationError(f"{name} isn't banned.")
    whitelist._write(path, keep, d)
    return {"message": f"Unbanned {name}. It applies when the server starts."}
