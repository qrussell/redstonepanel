"""Advanced options for a new server (1.17.2): its name and port, a pinned build, snapshots,
the Java it runs on, its heap, startup flags, the world and the common server.properties
settings, all chosen on New server before the first start.

validate() turns the request's "advanced" object into a checked plan before anything is
downloaded; manager.deploy_server() carries it out. Nothing here trusts the browser: every
value is matched against a pattern or a fixed list, and anything that ends up in the systemd
unit (startup flags) is limited to characters systemd and Java take literally.

Startup flags are kept by the panel, not in the server folder (the folder belongs to the
minecraft user): /var/lib/redstonepanel/jvm-flags.json, {server: {"preset", "extra"}}.
manager._write_unit() reads them every time it writes a Java unit, so Update, Memory (1.17),
restore and a move all keep them.
"""
import json
import os
import re
import threading

from core.settings import DATA_DIR

FLAGS_FILE = os.path.join(DATA_DIR, "jvm-flags.json")
_flags_lock = threading.Lock()

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,29}$")           # becomes minecraft-<name>
HEAP_CHOICE_RE = re.compile(r"^([0-9]{3,5})M$|^([0-9]{1,3})G$")
SEED_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,64}$")
LEVEL_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_ -]{0,31}$")
MOTD_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,120}$")
BUILD_RE = re.compile(r"^[0-9A-Za-z.+_-]{1,40}$")

PRESETS = ("none", "aikar")
GAMEMODES = ("survival", "creative", "adventure", "spectator")
DIFFICULTIES = ("peaceful", "easy", "normal", "hard")
LEVEL_TYPES = {"java": ("default", "flat", "large_biomes", "amplified"), "bedrock": ("default", "flat")}

# Extra flags: only these shapes, and never one that runs a command or reads a file of more
# options. The unit runs them as the minecraft user, but they're written by root into a unit,
# so the characters systemd would interpret (%, $, quotes, backslashes, ;) are never allowed.
FLAG_RES = (
    re.compile(r"^-XX:[+-][A-Za-z][A-Za-z0-9]{1,80}$"),
    re.compile(r"^-XX:[A-Za-z][A-Za-z0-9]{1,80}=[A-Za-z0-9._:/,+-]{1,120}$"),
    re.compile(r"^-D[A-Za-z][A-Za-z0-9._-]{0,80}(=[A-Za-z0-9._:/,+@-]{0,120})?$"),
    re.compile(r"^-Xss[0-9]{1,5}[kKmM]?$"),
)
BLOCKED_XX = {"onerror", "onoutofmemoryerror", "flags", "vmoptionsfile", "compilecommandfile",
              "jvmcilibpath", "errorfile"}
MAX_EXTRA = 24


class OptionError(ValueError):
    """An advanced option that can't be used (message is safe to show)."""


# ---------------------------------------------------------------------------
# Aikar's flags (https://docs.papermc.io/paper/aikars-flags), the usual G1 tuning for Paper.
# Above 12 GB of heap the young generation and region size go up, as the page says.
# ---------------------------------------------------------------------------
def aikar_flags(xmx_bytes):
    big = (xmx_bytes or 0) > 12 * 1024 ** 3
    return ["-XX:+UseG1GC", "-XX:+ParallelRefProcEnabled", "-XX:MaxGCPauseMillis=200",
            "-XX:+UnlockExperimentalVMOptions", "-XX:+DisableExplicitGC", "-XX:+AlwaysPreTouch",
            f"-XX:G1NewSizePercent={40 if big else 30}", f"-XX:G1MaxNewSizePercent={50 if big else 40}",
            f"-XX:G1HeapRegionSize={'16M' if big else '8M'}", f"-XX:G1ReservePercent={15 if big else 20}",
            "-XX:G1HeapWastePercent=5", "-XX:G1MixedGCCountTarget=4",
            f"-XX:InitiatingHeapOccupancyPercent={20 if big else 15}",
            "-XX:G1MixedGCLiveThresholdPercent=90", "-XX:SurvivorRatio=32",
            "-XX:+PerfDisableSharedMem", "-XX:MaxTenuringThreshold=1",
            "-Dusing.aikars.flags=https://mcflags.emc.gs", "-Daikars.new.flags=true"]


def heap_bytes(text):
    m = re.match(r"^([0-9]+)([KkMmGg])$", str(text or ""))
    if not m:
        return None
    return int(m.group(1)) * {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}[m.group(2).lower()]


def check_flag(flag):
    flag = str(flag or "").strip()
    if not any(r.match(flag) for r in FLAG_RES):
        raise OptionError(f"'{flag[:60]}' isn't a startup flag the panel accepts. Use -XX: options, "
                          f"-D properties or -Xss; set memory with Heap instead of -Xms/-Xmx.")
    if flag.startswith("-XX:"):
        name = re.match(r"^-XX:[+-]?([A-Za-z0-9]+)", flag).group(1).lower()
        if name in BLOCKED_XX:
            raise OptionError(f"-XX:{name} runs a command or reads options from a file, so it isn't allowed.")
    return flag


def clean_flags(value):
    """{"preset", "extra"} from a stored or sent value; raises OptionError on anything else."""
    value = value if isinstance(value, dict) else {}
    preset = str(value.get("preset") or "none").lower()
    if preset not in PRESETS:
        raise OptionError(f"Unknown startup flag preset '{preset}'.")
    extra = value.get("extra") or []
    if isinstance(extra, str):
        extra = extra.split()
    if not isinstance(extra, list) or len(extra) > MAX_EXTRA:
        raise OptionError(f"Give at most {MAX_EXTRA} extra startup flags.")
    return {"preset": preset, "extra": [check_flag(f) for f in extra]}


def unit_flags(flags, xmx):
    """The flags to put between -Xmx and -jar in a unit, from clean_flags() output."""
    if not flags:
        return []
    out = aikar_flags(heap_bytes(xmx)) if flags.get("preset") == "aikar" else []
    return out + list(flags.get("extra") or [])


# ---------------------------------------------------------------------------
# The store (root-only, like the rest of /var/lib/redstonepanel)
# ---------------------------------------------------------------------------
def _load():
    try:
        with open(FLAGS_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def get_flags(server):
    """The server's stored flags, re-checked (a bad entry is ignored), or None."""
    try:
        v = _load().get(server)
        return clean_flags(v) if v else None
    except OptionError:
        return None


def set_flags(server, flags):
    """Stores flags for a server; None or no preset and no extra flags removes the entry."""
    with _flags_lock:
        data = _load()
        if flags and (flags.get("preset") != "none" or flags.get("extra")):
            data[server] = clean_flags(flags)
        elif server in data:
            del data[server]
        else:
            return
        os.makedirs(os.path.dirname(FLAGS_FILE), exist_ok=True)
        tmp = FLAGS_FILE + ".tmp"
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, FLAGS_FILE)


def describe_flags(flags):
    if not flags:
        return None
    parts = (["Aikar's flags"] if flags.get("preset") == "aikar" else []) + list(flags.get("extra") or [])
    return ", ".join(parts) or None


# ---------------------------------------------------------------------------
# Checking a request
# ---------------------------------------------------------------------------
def _num(value, lo, hi, what):
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise OptionError(f"{what} must be a whole number.")
    if not lo <= n <= hi:
        raise OptionError(f"{what} must be between {lo} and {hi}.")
    return n


def _names(value, pattern, what):
    if value in (None, "", []):
        return []
    items = value.replace(",", " ").split() if isinstance(value, str) else value
    if not isinstance(items, list) or len(items) > 50:
        raise OptionError(f"Give at most 50 {what}.")
    out = []
    for n in items:
        n = str(n).strip()
        if not pattern.match(n):
            raise OptionError(f"'{n[:40]}' isn't a valid player name for {what}.")
        if n.lower() not in (x.lower() for x in out):
            out.append(n)
    return out


def _bool(value):
    if isinstance(value, bool):
        return value
    if str(value).lower() in ("true", "1", "on", "yes"):
        return True
    if str(value).lower() in ("false", "0", "off", "no", ""):
        return False
    raise OptionError(f"'{value}' isn't on or off.")


def validate(advanced, edition, loader="vanilla", is_admin=True):
    """The request's advanced object as a checked plan (a dict with only known keys), or {}.
    Raises OptionError. Checks that depend on the machine (port free, name free, heap fits,
    Java installed) happen in manager.deploy_server()."""
    if advanced in (None, {}):
        return {}
    if not isinstance(advanced, dict):
        raise OptionError("Advanced options must be an object.")
    a = {k: v for k, v in advanced.items() if v not in (None, "")}
    known = {"name", "port", "build", "snapshot", "java", "heap", "memory_limit", "cpu", "flags", "seed", "level_type", "level_name",
             "motd", "gamemode", "difficulty", "max_players", "online_mode", "whitelist", "whitelist_players",
             "ops"}
    unknown = sorted(set(a) - known)
    if unknown:
        raise OptionError(f"Unknown advanced option: {', '.join(unknown)}.")
    java = edition == "java"
    if not java:
        for k in ("build", "snapshot", "java", "heap", "flags", "ops"):
            if k in a and a[k] not in (False, [], {}):
                raise OptionError("Bedrock servers have no builds, Java, heap, startup flags or ops list to choose.")
    plan = {}
    if "name" in a:
        name = str(a["name"]).strip().lower()
        if name.startswith("minecraft-"):
            name = name[len("minecraft-"):]
        if not NAME_RE.match(name):
            raise OptionError("A name is 1 to 30 lower-case letters, digits and dashes, starting with a "
                              "letter or digit (it becomes minecraft-NAME).")
        # Bedrock units run from /opt/minecraft-NAME, and the panel tells editions apart by "java"
        # in a unit's command line, so a Bedrock name can't contain it.
        if not java and "java" in name:
            raise OptionError("A Bedrock server's name can't contain 'java'.")
        plan["name"] = f"minecraft-{name}"
    if "port" in a:
        plan["port"] = _num(a["port"], 1024, 65535 if java else 65534, "The game port")
    if java and "build" in a:
        if loader not in ("paper", "fabric"):
            raise OptionError("Only Paper and Fabric servers have builds to choose.")
        build = str(a["build"]).strip()
        if build.lower() != "latest":
            if not BUILD_RE.match(build):
                raise OptionError("That isn't a build the panel can look up.")
            plan["build"] = build
    if java and _bool(a.get("snapshot", False)):
        if loader != "vanilla":
            raise OptionError("Snapshots and pre-releases are offered for Vanilla only.")
        plan["snapshot"] = True
    if java and "java" in a and str(a["java"]).lower() != "auto":
        plan["java"] = _num(a["java"], 8, 99, "The Java version")
    if java and "heap" in a:
        heap = str(a["heap"]).strip().upper()
        if not HEAP_CHOICE_RE.match(heap):
            raise OptionError("Heap is a size such as 2G or 1536M.")
        b = heap_bytes(heap)
        if b < 512 * 1024 ** 2 or b > 256 * 1024 ** 3:
            raise OptionError("Heap must be between 512M and 256G.")
        plan["heap"] = heap
    if "memory_limit" in a:
        lim = str(a["memory_limit"]).strip().upper()
        if lim in ("OFF", "NONE"):
            pass
        elif lim == "AUTO":
            if not java:
                raise OptionError("Automatic follows a Java server's heap; give a Bedrock server a size such as 2G.")
            plan["memory_limit"] = "auto"
        elif HEAP_CHOICE_RE.match(lim) and 256 * 1024 ** 2 <= heap_bytes(lim) <= 512 * 1024 ** 3:
            plan["memory_limit"] = lim
        else:
            raise OptionError("The hard memory limit is Automatic, Off or a size such as 3G or 2560M.")
    if "cpu" in a:
        try:
            cpu = round(float(a["cpu"]), 2)
        except (TypeError, ValueError):
            raise OptionError("Write the CPU limit as a number of cores, like 1 or 1.5.")
        cores = os.cpu_count() or 1
        if cpu and not 0.1 <= cpu <= cores:
            raise OptionError(f"The CPU limit is between 0.1 and {cores} cores (this machine has {cores}), or 0 for none.")
        if cpu:
            plan["cpu"] = cpu
    if java and "flags" in a:
        flags = clean_flags(a["flags"])
        if flags["extra"] and not is_admin:
            raise OptionError("Only admins can add extra startup flags.")
        if flags["preset"] != "none" or flags["extra"]:
            plan["flags"] = flags
    props = {}
    if "seed" in a:
        seed = str(a["seed"])
        if not SEED_RE.match(seed):
            raise OptionError("A seed is up to 64 characters on one line.")
        props["level-seed"] = seed
    if "level_type" in a:
        lt = str(a["level_type"]).lower()
        if lt not in LEVEL_TYPES[edition]:
            raise OptionError(f"World type is one of: {', '.join(LEVEL_TYPES[edition])}.")
        if lt != "default":
            plan["level_type"] = lt
    if "level_name" in a:
        level = str(a["level_name"]).strip()
        if not LEVEL_NAME_RE.match(level):
            raise OptionError("A world name is up to 32 letters, digits, spaces, dashes and underscores.")
        props["level-name"] = level
    if "motd" in a:
        motd = str(a["motd"])
        if not MOTD_RE.match(motd):
            raise OptionError("The server message is one line of up to 120 characters.")
        props["motd" if java else "server-name"] = motd
    if "gamemode" in a:
        gm = str(a["gamemode"]).lower()
        if gm not in (GAMEMODES if java else GAMEMODES[:3]):
            raise OptionError("Unknown game mode.")
        plan["gamemode"] = gm
    if "difficulty" in a:
        diff = str(a["difficulty"]).lower()
        if diff not in DIFFICULTIES:
            raise OptionError("Unknown difficulty.")
        plan["difficulty"] = diff
    if "max_players" in a:
        props["max-players"] = _num(a["max_players"], 1, 1000, "Max players")
    if "online_mode" in a:
        props["online-mode"] = "true" if _bool(a["online_mode"]) else "false"
    from core import whitelist                   # its name patterns (imports manager lazily below)
    players = _names(a.get("whitelist_players"), whitelist.JAVA_NAME if java else whitelist.GAMERTAG,
                     "the whitelist" if java else "the allowlist")
    if "whitelist" in a or players:
        on = _bool(a.get("whitelist", True)) or bool(players)
        props["white-list" if java else "allow-list"] = "true" if on else "false"
    if players:
        plan["whitelist_players"] = players
    if java:
        ops = _names(a.get("ops"), whitelist.JAVA_NAME, "operators")
        if ops:
            plan["ops"] = ops
    if props:
        plan["properties"] = props
    return plan


def level_type_value(lt, edition, minecraft=None):
    """server.properties' level-type for a world type: namespaced from Java 1.19, the old names
    before it; Bedrock's own upper-case names."""
    if edition == "bedrock":
        return {"default": "DEFAULT", "flat": "FLAT"}[lt]
    from core import javaservers
    modern = not minecraft or not javaservers.RELEASE_RE.match(str(minecraft)) \
        or javaservers.version_key(minecraft) >= (1, 19)
    if modern:
        return {"default": "minecraft:normal", "flat": "minecraft:flat",
                "large_biomes": "minecraft:large_biomes", "amplified": "minecraft:amplified"}[lt]
    return {"default": "default", "flat": "flat", "large_biomes": "largeBiomes", "amplified": "amplified"}[lt]


def mode_value(kind, value, edition, minecraft=None):
    """gamemode/difficulty as server.properties wants them: names, or numbers before Java 1.14."""
    from core import javaservers
    if edition == "java" and minecraft and javaservers.RELEASE_RE.match(str(minecraft)) \
            and javaservers.version_key(minecraft) < (1, 14):
        return str((GAMEMODES if kind == "gamemode" else DIFFICULTIES).index(value))
    return value


def summary(plan):
    """One line for the activity log: the choices that differ from the defaults."""
    bits = []
    for k, label in (("port", "port"), ("build", "build"), ("java", "Java"), ("heap", "heap"),
                     ("level_type", "world type"), ("gamemode", "game mode"), ("difficulty", "difficulty")):
        if k in plan:
            bits.append(f"{label} {plan[k]}")
    if plan.get("snapshot"):
        bits.append("snapshots allowed")
    if plan.get("memory_limit"):
        bits.append(f"hard memory limit {plan['memory_limit'].lower() if plan['memory_limit'] == 'auto' else plan['memory_limit']}")
    if plan.get("cpu"):
        bits.append(f"CPU {plan['cpu']} cores")
    if plan.get("flags"):
        bits.append(f"flags: {describe_flags(plan['flags'])}")
    for k, v in (plan.get("properties") or {}).items():
        bits.append(f"{k}={v}")
    if plan.get("whitelist_players"):
        bits.append(f"whitelist: {', '.join(plan['whitelist_players'])}")
    if plan.get("ops"):
        bits.append(f"ops: {', '.join(plan['ops'])}")
    return "; ".join(bits)
