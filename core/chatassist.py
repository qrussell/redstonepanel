"""What Live Chat's suggestions need to know about one server (1.21.4).

The command list people pick from is static/commands.json; this adds what only the running
server knows: who is online (and who has been seen before), and, for a Java server that
answers over RCON, the commands and game rules it really has. Vanilla, Fabric, Quilt, Forge
and NeoForge answer `help` with one usage line per command, which also brings in the commands
mods add and the game rule names of that Minecraft version (they were renamed in 1.21.11).
Paper and Purpur answer with paged help text instead, so they get the curated list only.
Bedrock gives nothing back through screen, so it gets players from the player log only.

Nothing here changes a server: it sends `list`, `help` and `help gamerule`, nothing else.
"""
import re
import threading
import time

from core import backups, manager, moderation, playerlog, rcon

KNOWN_MAX = 100              # names of players seen before, newest first
COMMANDS_MAX = 400
USAGE_MAX = 120
HELP_TTL = 600               # the command list only changes when the server restarts or mods change

HELP_LINE_RE = re.compile(r"^/([A-Za-z0-9_.-]+)(?:\s+(.*))?$")
GAMERULE_RE = re.compile(r"/gamerule\s+([A-Za-z0-9_.]+)\s+\[")

_help_cache = {}             # server -> (started_at, fetched, {"commands": [...], "gamerules": [...]})
_lock = threading.Lock()


def parse_help(out):
    """Brigadier `help`: '/advancement (grant|revoke)/attribute <target> ...' -> [{name, usage}].
    RCON drops the line breaks, so a new command starts at each '/' that follows a non-space.
    Names with a namespace (minecraft:tp) are left out: they repeat the plain ones."""
    out = rcon.COLOR_RE.sub("", out or "").strip()
    if not out.startswith("/"):
        return []            # Paper's paged help, or an error
    found, seen = [], set()
    for chunk in re.split(r"\n|(?<=\S)(?=/[A-Za-z])", out):
        m = HELP_LINE_RE.match(chunk.strip())
        if not m or m.group(1).lower() in seen:
            continue
        seen.add(m.group(1).lower())
        usage = (m.group(2) or "").strip()
        if len(usage) > USAGE_MAX:
            usage = usage[:USAGE_MAX].rsplit("|", 1)[0] + "|…"
        found.append({"name": m.group(1), "usage": usage})
        if len(found) >= COMMANDS_MAX:
            break
    return found


def parse_gamerules(out):
    """`help gamerule`: '/gamerule keep_inventory [<value>]/gamerule minecraft:keep_inventory ...'."""
    names = {n for n in GAMERULE_RE.findall(rcon.COLOR_RE.sub("", out or ""))}
    return sorted(names, key=str.lower)


def _server_help(server, d):
    """The running Java server's own commands and game rules, asked once per start."""
    send = backups._java_rcon(d)
    if not send:
        return {"commands": [], "gamerules": []}
    started = moderation._started_at(server)
    now = time.time()
    with _lock:
        hit = _help_cache.get(server)
        if hit and hit[0] == started and now - hit[1] < HELP_TTL:
            return hit[2]
    try:
        commands = parse_help(send("help"))
        gamerules = parse_gamerules(send("help gamerule")) if commands else []
    except rcon.RconError:
        return {"commands": [], "gamerules": []}     # still starting: ask again next time
    data = {"commands": commands, "gamerules": gamerules}
    with _lock:
        _help_cache[server] = (started, now, data)
    return data


def known_players(server, online=()):
    """Players who joined before and aren't online now, newest first."""
    skip = {n.lower() for n in online}
    out = []
    for e in playerlog.history(server, ev="join", limit=5000):
        name = e.get("name") or ""
        if name and name.lower() not in skip:
            skip.add(name.lower())
            out.append(name)
            if len(out) >= KNOWN_MAX:
                break
    return out


def info(server):
    d = manager.get_server_dir(server)
    java = manager._is_java(server)
    running = backups._is_active(server)
    names, source = moderation.online(server)
    helped = _server_help(server, d) if running and java else {"commands": [], "gamerules": []}
    return {"edition": "java" if java else "bedrock", "running": running,
            "online": names, "online_source": source, "known": known_players(server, names),
            "commands": helped["commands"], "gamerules": helped["gamerules"]}
