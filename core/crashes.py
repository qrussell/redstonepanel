"""Why did it crash? (1.17.1)

When a game server crashes or stops without being asked to (a failed start included), the
systemd drop-in's ExecStopPost runs `redstonepanel notify-unit stop NAME` as root, and
core/notify.py calls record() here. It reads the newest crash report (if one was written in the
last few minutes) or the end of the server's log, matches it against a table of known causes,
names the mod or plugin it points at, and keeps the result in DATA_DIR/crashes/NAME.json.

The server's Overview shows it as the "Last crash" card, the crash webhook gets one line with
the cause, and Needs attention links to the card. Nothing is changed without a click: the card's
**Turn off and restart** and **Undo the last mod update** are separate, admin-only API calls.

Files in the server folder belong to the minecraft user and this runs as root, so they're
opened one path component at a time with O_NOFOLLOW (as core/files.py does), never by path.
"""
import json
import os
import re
import time

from core.settings import DATA_DIR

CRASH_DIR = os.path.join(DATA_DIR, "crashes")
NAME_RE = re.compile(r"^minecraft[A-Za-z0-9_.@-]*$")
READ_BYTES = 256 * 1024           # how much of a log or crash report is read (its end)
EXCERPT_LINES = 400               # lines kept for the card
REPORT_FRESH = 600                # a crash report this many seconds old still belongs to this crash
SHOW_FOR = 7 * 86400              # Needs attention lists a crash for a week (or until dismissed)
UNDO_WINDOW = 3 * 86400           # "Undo the last mod update" is offered when it was this recent


class CrashError(ValueError):
    """A crash action that can't be done (message is safe to show)."""


# ---------------------------------------------------------------------------
# Known causes. Each: id, regex (searched line by line), title, and a function giving the
# plain-words explanation and what to do from the match. The first match of each id counts.
# ---------------------------------------------------------------------------
def _java_for_class(v):
    try:
        return int(float(v)) - 44          # class file 52 = Java 8, 61 = 17, 65 = 21, 69 = 25
    except ValueError:
        return None

def _wrong_java(m, ctx):
    need, have = _java_for_class(m.group("need")), _java_for_class(m.group("have") or "0")
    have_txt = f" It was started with Java {have}." if have and have > 0 else ""
    return (f"The server (or a mod) needs Java {need} or newer.{have_txt}",
            f"Install Java {need} under Dashboard → Supporting software, or pick it for this server in "
            f"Maintenance → Java. If only one mod needs it, turn that mod off instead.")

FRIENDLY = {"fabric": "Fabric API", "fabric-api": "Fabric API", "fabricloader": "Fabric Loader",
            "fabric-language-kotlin": "Fabric Language Kotlin", "cloth-config": "Cloth Config API"}

def _missing_fabric(m, ctx):
    dep = m.group("depname") or FRIENDLY.get(m.group("dep") or m.group("dep2"), m.group("dep") or m.group("dep2"))
    wrong = m.group("have")
    if (m.group("dep") or m.group("dep2")) == "minecraft":
        return (f"{m.group('name')} is made for a different Minecraft version (this server runs {wrong}).",
                f"Turn {m.group('name')} off, or install the version made for Minecraft {wrong}.")
    if wrong:
        return (f"{m.group('name')} needs a different version of {dep} (found {wrong}).",
                f"Update {dep}, or turn {m.group('name')} off.")
    deps = [dep]
    for o in FABRIC_DEP_RE.finditer(ctx.get("text") or ""):      # everything else this mod lacks
        d = o.group("depname") or FRIENDLY.get(o.group("dep") or o.group("dep2"), o.group("dep") or o.group("dep2"))
        if o.group("name") == m.group("name") and not o.group("have") and d not in deps:
            deps.append(d)
    both = " and ".join([", ".join(deps[:-1]), deps[-1]] if len(deps) > 1 else deps)
    return (f"{m.group('name')} requires {both}, which {'are' if len(deps) > 1 else 'is'}n't installed.",
            f"Install {both} on the Mods tab, or turn {m.group('name')} off.")

def _missing_plugin(m, ctx):
    deps = m.group("deps").strip()
    who = m.group("who")
    return (f"{who} requires {deps}, which isn't installed." if who else f"A plugin requires {deps}, which isn't installed.",
            f"Install {deps} on the Plugins tab, or turn {who or 'the plugin'} off.")

def _wrong_api(m, ctx):
    return (f"A plugin is made for a newer Minecraft (API version {m.group('api')}).",
            "Turn it off, or install the version made for this server's Minecraft.")

def _port(m, ctx):
    port = ctx.get("port")
    where = f" {port}" if port else ""
    return (f"Another program (or another server) is already using port{where}.",
            "Stop the other server, or give this one a free port in Properties (server-port), then start it.")

def _oom(m, ctx):
    return ("The server ran out of memory.",
            "Give it more memory (Maintenance → Memory and limits), or remove the mods or plugins that use the most.")

def _eula(m, ctx):
    return ("The Minecraft EULA hasn't been accepted.",
            "Open Files, set eula=true in eula.txt (that accepts minecraft.net/eula), then start the server.")

def _chunk(m, ctx):
    return ("Part of the world is damaged (a chunk or region file couldn't be read).",
            "Restore a recent backup on the Backups tab. The panel backs up the current world first.")

def _level(m, ctx):
    return ("The world's level.dat is damaged or missing.",
            "Restore a recent backup on the Backups tab. The panel backs up the current world first.")

FABRIC_DEP_RE = re.compile(
    r"Mod '(?P<name>[^']+)' \((?P<id>[^)]+)\) \S+ requires .*? of "
    r"(?:mod '(?P<depname>[^']+)' \((?P<dep>[^)]+)\)|(?P<dep2>[A-Za-z0-9_.-]+))"
    r"(?:, which is missing|, but only the wrong version is present: (?P<have>[^!\s]+))")

CAUSES = [
    ("wrong_java", re.compile(r"UnsupportedClassVersionError.*?class file version (?P<need>\d+(?:\.\d+)?)"
                              r"(?:.*?versions up to (?P<have>\d+(?:\.\d+)?))?"), "Wrong Java version", _wrong_java),
    ("missing_dependency", FABRIC_DEP_RE,
     "Missing or wrong dependency", _missing_fabric),
    ("missing_dependency", re.compile(r"Unknown/missing dependency plugins: \[(?P<deps>[^\]]+)\]"
                                      r"(?:.*?to run '(?P<who>[^']+)')?"), "Missing plugin dependency", _missing_plugin),
    ("wrong_version", re.compile(r"Unsupported API version (?P<api>[0-9][0-9.]*)"), "Plugin for another version",
     _wrong_api),
    ("port_in_use", re.compile(r"FAILED TO BIND TO PORT|Address already in use|Network port occupied|"
                               r"Port \[?\d+\]? may be in use"), "Port already in use", _port),
    ("out_of_memory", re.compile(r"java\.lang\.OutOfMemoryError"), "Out of memory", _oom),
    ("eula", re.compile(r"You need to agree to the EULA"), "EULA not accepted", _eula),
    ("corrupt_level", re.compile(r"level\.dat.*(?:Exception|corrupt|Failed|failed|Couldn't)|"
                                 r"(?:Exception|Failed|failed|Couldn't) (?:reading|to read|to load|load) .*level\.dat"),
     "Damaged level.dat", _level),
    ("corrupt_chunk", re.compile(r"Couldn't load chunk|Failed to read chunk|Couldn't read chunk|"
                                 r"Chunk file at .* is (?:in the wrong location|corrupt)|RegionFile.*(?:corrupt|invalid)|"
                                 r"Corrupted chunk|Error loading chunk"), "Damaged world chunk", _chunk),
]

# Lines worth highlighting on the card even when no known cause matches
ERROR_RE = re.compile(r"(?:\b(?:ERROR|FATAL|SEVERE)\b|Exception|Error:|^\s*Caused by:|\bfailed\b|\bcrash|which is missing|wrong version is present)", re.I)

# Where crash reports and logs name a mod or plugin
SUSPECT_RES = [
    re.compile(r"Suspected Mods?: (?P<name>[^,(\n]+?)\s*\((?P<id>[^)]+)\)"),
    re.compile(r"(?:Mixin apply for mod|from mod) (?P<id>[A-Za-z0-9_-]+)"),
    re.compile(r"Error occurred while enabling (?P<name>\S+)"),
    re.compile(r"Could not pass event \S+ to (?P<name>\S+)"),
    re.compile(r"Could not load '(?:plugins|mods)/(?P<file>[^']+\.jar)'"),
    re.compile(r"Mod '(?P<name>[^']+)' \((?P<id>[^)]+)\) \S+ requires"),
    re.compile(r"to run '(?P<name>[^']+)'"),
]


# ---------------------------------------------------------------------------
# Reading the server folder safely (as root, files owned by minecraft)
# ---------------------------------------------------------------------------
def _open_dir(base, parts):
    fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for p in parts:
            nfd = os.open(p, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nfd
        return fd
    except BaseException:
        os.close(fd)
        raise

def _read_tail(base, rel, limit=READ_BYTES):
    """(text, mtime) of the end of base/rel, or (None, None)."""
    parts = rel.split("/")
    try:
        dfd = _open_dir(base, parts[:-1])
    except OSError:
        return None, None
    try:
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
    except OSError:
        return None, None
    finally:
        os.close(dfd)
    try:
        st = os.fstat(fd)
        if not (st.st_mode & 0o170000 == 0o100000):        # regular files only
            return None, None
        os.lseek(fd, max(0, st.st_size - limit), os.SEEK_SET)
        data = b""
        while len(data) < limit:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            data += chunk
        text = data.decode("utf-8", "replace")
        if st.st_size > limit:
            text = text.split("\n", 1)[-1]                # drop the partial first line
        return text, st.st_mtime
    finally:
        os.close(fd)

def _newest_report(base, now):
    """('crash-reports/NAME', text) of a crash report written in the last REPORT_FRESH seconds."""
    try:
        dfd = _open_dir(base, ["crash-reports"])
    except OSError:
        return None, None
    try:
        best = None
        with os.scandir(dfd) as it:
            for e in it:
                if not e.name.endswith(".txt"):
                    continue
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                if st.st_mode & 0o170000 == 0o100000 and now - st.st_mtime <= REPORT_FRESH:
                    if not best or st.st_mtime > best[1]:
                        best = (e.name, st.st_mtime)
    finally:
        os.close(dfd)
    if not best:
        return None, None
    rel = f"crash-reports/{best[0]}"
    text, _ = _read_tail(base, rel)
    return (rel, text) if text is not None else (None, None)


# ---------------------------------------------------------------------------
# Analysis (pure: text in, findings out)
# ---------------------------------------------------------------------------
def _port_from(base):
    text, _ = _read_tail(base, "server.properties", 64 * 1024)
    m = re.search(r"^server-port\s*=\s*(\d+)", text or "", re.M)
    return m.group(1) if m else None

def find_causes(text, ctx=None):
    ctx = dict(ctx or {}, text=text)
    found, seen = [], set()
    lines = text.splitlines()
    for cid, rx, title, explain in CAUSES:
        if cid in seen:
            continue
        for i, line in enumerate(lines):
            m = rx.search(line)
            if m:
                what, fix = explain(m, ctx)
                found.append({"id": cid, "title": title, "text": what, "fix": fix, "line": i})
                seen.add(cid)
                break
    if ctx.get("service_result") == "oom-kill" and "out_of_memory" not in seen:
        limit = ctx.get("memory_limit")
        found.append({"id": "out_of_memory", "title": "Out of memory",
                      "text": "The system stopped the server because it used more memory than it may" +
                              (f" (its hard limit is {limit})." if limit else "."),
                      "fix": "Raise the hard memory limit or lower the heap in Maintenance → Memory and limits.",
                      "line": None})
    return found

def suspects_named(text):
    """Names, ids and jar files that the text blames, most specific first (lowercased)."""
    out = []
    for rx in SUSPECT_RES:
        for m in rx.finditer(text):
            g = m.groupdict()
            for k in ("file", "id", "name"):
                v = (g.get(k) or "").strip().strip("'\"")
                if v and v.lower() not in ("none", "unknown") and v.lower() not in out:
                    out.append(v.lower())
    return out

def match_installed(names, items):
    """The installed mods or plugins (from core.mods.list_installed) that the names point at."""
    hits = []
    for n in names:
        for it in items:
            keys = {(it.get("id") or "").lower(), (it.get("name") or "").lower(), it["file"].lower(),
                    re.sub(r"\.jar(\.disabled)?$", "", it["file"].lower())}
            keys.discard("")
            if (n in keys or n.endswith(".jar") and n == it["file"].lower()) and it not in hits:
                hits.append(it)
    return hits

def excerpt(text, causes):
    """The last EXCERPT_LINES lines, with the error lines marked for highlighting."""
    lines = text.splitlines()
    start = max(0, len(lines) - EXCERPT_LINES)
    cause_lines = {c["line"] for c in causes if c.get("line") is not None}
    keep = lines[start:]
    # a cause found earlier than the window: keep its line too, at the top
    early = sorted(l for l in cause_lines if l < start)
    out = [{"text": lines[l][:500], "hl": True} for l in early]
    if early:
        out.append({"text": "…", "hl": False})
    out += [{"text": l[:500], "hl": (start + i) in cause_lines or bool(ERROR_RE.search(l))}
            for i, l in enumerate(keep)]
    return out


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------
def _path(server):
    if not NAME_RE.fullmatch(server or ""):
        raise CrashError("Unknown server.")
    return os.path.join(CRASH_DIR, f"{server}.json")

def _save(server, rec):
    os.makedirs(CRASH_DIR, mode=0o700, exist_ok=True)
    p = _path(server)
    tmp = p + ".tmp"
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(rec, f)
    os.replace(tmp, p)

def load(server):
    try:
        with open(_path(server)) as f:
            rec = json.load(f)
        return rec if isinstance(rec, dict) else None
    except (OSError, ValueError):
        return None

def _installed(server):
    try:
        from core import mods
        return mods.list_installed(server)
    except Exception:
        return None

def _last_update(server):
    """The last mod/plugin update that can be undone: {"at", "files"} or None."""
    try:
        from core import catalog
        prev = catalog.read_tracking(server).get("previous") or None
    except Exception:
        return None
    if prev and prev.get("items"):
        return {"at": prev.get("at"), "files": [p.get("new") for p in prev["items"]]}
    return None

def journal(server, env):
    """What the game server printed to systemd's journal in this run (early failures such as the
    wrong Java happen before it opens its own log)."""
    import subprocess
    inv = (env or {}).get("INVOCATION_ID") or ""
    sel = [f"_SYSTEMD_INVOCATION_ID={inv}"] if re.fullmatch(r"[0-9a-f]{32}", inv) else ["-u", f"{server}.service"]
    try:
        r = subprocess.run(["journalctl", "--no-pager", "-o", "cat", "-n", "300", *sel],
                           capture_output=True, text=True, timeout=10)
        return r.stdout if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""

def gather(server, base, edition=None, env=None, now=None):
    """[(source, text)] to look at, the one to show first: a fresh crash report, the log, the journal."""
    now = now or time.time()
    bedrock = edition == "Bedrock" or os.path.exists(os.path.join(base, "bedrock_server"))
    out = []
    if not bedrock:
        rel, text = _newest_report(base, now)
        if text:
            out.append((rel, text))
    log_rel = "server.log" if bedrock else "logs/latest.log"
    log, mtime = _read_tail(base, log_rel)
    jtext = journal(server, env) if NAME_RE.fullmatch(server or "") else ""
    fresh = log and mtime and now - mtime <= REPORT_FRESH
    if log and (fresh or not jtext.strip()):
        out.append((log_rel, log))
    if jtext.strip():
        out.append(("journal", jtext))
    if log and not fresh and jtext.strip():
        out.append((log_rel, log))                 # older: the last run's log, looked at last
    return out, bedrock

def analyze_texts(server, sources, env=None, now=None, installed=None, last_update=None, port=None):
    """The crash record (not saved) from [(source, text)], first one shown on the card."""
    now = now or time.time()
    env = env or {}
    ctx = {"service_result": env.get("SERVICE_RESULT"), "memory_limit": env.get("memory_limit"), "port": port}
    source, text = sources[0] if sources else (None, "")
    causes = find_causes(text, ctx)
    seen = {c["id"] for c in causes}
    for _, other in sources[1:]:
        for c in find_causes(other, ctx):
            if c["id"] not in seen:
                causes.append(dict(c, line=None))
                seen.add(c["id"])
    rec = {"server": server, "at": int(now), "source": source, "result": env.get("SERVICE_RESULT"),
           "exit": " ".join(filter(None, [env.get("EXIT_CODE"), env.get("EXIT_STATUS")])) or None,
           "causes": causes, "lines": excerpt(text, causes), "suspects": [], "undo": None, "dismissed": False}
    if installed and installed.get("items"):
        hits = match_installed(suspects_named("\n".join(t for _, t in sources)), installed["items"])
        rec["suspects"] = [{"file": h["file"], "name": h.get("name") or h["file"], "enabled": h.get("enabled", True)}
                           for h in hits[:3]]
        rec["noun"] = installed.get("noun")
    if last_update and last_update.get("at") and now - last_update["at"] <= UNDO_WINDOW:
        rec["undo"] = last_update
    return rec

def analyze(server, base, edition=None, env=None, now=None):
    sources, bedrock = gather(server, base, edition, env, now)
    return analyze_texts(server, sources, env, now,
                         installed=None if bedrock else _installed(server),
                         last_update=None if bedrock else _last_update(server), port=_port_from(base))

def summary(rec):
    """One line for the webhook and Needs attention."""
    if not rec:
        return None
    c = rec.get("causes") or []
    s = rec.get("suspects") or []
    if c:
        line = c[0]["text"]
        if s and not any(x["name"].lower() in line.lower() for x in s):
            line += f" Likely {rec.get('noun') or 'mod'}: {s[0]['name']}."
        return line
    if s:
        return f"Likely cause: the {rec.get('noun') or 'mod'} {s[0]['name']}."
    return None

def record(server, base, edition=None, env=None, crashed=True):
    """Called from the ExecStopPost hook. Saves and returns the record, or None if a clean,
    unrequested stop shows no known cause (a player typing `stop`, say). Never raises."""
    try:
        rec = analyze(server, base, edition, env)
        if not crashed and not rec["causes"]:
            return None
        rec["crashed"] = crashed
        _save(server, rec)
        return rec
    except Exception as e:
        try:
            from core import manager
            manager.write_log(f"[CRASH] Couldn't read why {server} stopped: {e}")
        except Exception:
            pass
        return None

def get(server):
    rec = load(server)
    if rec and not (rec.get("server") == server):
        return None
    if rec and rec.get("undo"):
        cur = _last_update(server)          # still there? (it may have been undone already)
        if not cur or cur.get("at") != rec["undo"].get("at"):
            rec["undo"] = None
    if rec and rec.get("suspects"):
        inst = _installed(server)
        if inst and inst.get("items") is not None:
            have = {i["file"]: i for i in inst["items"]}
            for s in rec["suspects"]:
                cur = have.get(s["file"]) or have.get(s["file"] + ".disabled")
                s["enabled"] = bool(cur and cur.get("enabled"))
                s["present"] = bool(cur)
    return rec

def dismiss(server):
    _path(server)
    rec = load(server)
    if rec:
        rec["dismissed"] = True
        _save(server, rec)
    return {"dismissed": True}

def attention(servers, now=None):
    """Needs attention items for recent, undismissed crashes of the given servers."""
    now = now or time.time()
    out = []
    for name in servers:
        rec = load(name) if NAME_RE.fullmatch(name or "") else None
        if not rec or rec.get("dismissed") or now - rec.get("at", 0) > SHOW_FOR:
            continue
        why = summary(rec)
        verb = "crashed" if rec.get("crashed", True) else "stopped"
        out.append({"server": name, "text": f"{name} {verb}" + (f": {why}" if why else ". See Last crash on its Overview.")})
    return out


# ---------------------------------------------------------------------------
# Fixes (admin, one click each)
# ---------------------------------------------------------------------------
def turn_off_and_restart(server, file, actor="?"):
    from core import manager, mods
    rec = load(server)
    if not rec or file not in [s["file"] for s in rec.get("suspects") or []]:
        raise CrashError("That mod or plugin isn't the one the last crash points at.")
    res = mods.set_enabled(server, file, False, actor=actor)
    manager.write_log(f"[CRASH] {actor} turned off {file} on {server} after a crash; restarting it.")
    manager.control_server(server, "restart")
    rec["fixed"] = {"action": "disabled", "file": file, "by": actor, "at": int(time.time())}
    rec["dismissed"] = True
    _save(server, rec)
    return {"file": res.get("file"), "restarted": True}

def undo_and_restart(server, actor="?"):
    from core import catalog, manager
    rec = load(server)
    if not rec or not rec.get("undo"):
        raise CrashError("There's no recent mod or plugin update to undo.")
    res = catalog.undo(server, actor=actor)
    manager.write_log(f"[CRASH] {actor} undid the last mod update on {server} after a crash; restarting it.")
    manager.control_server(server, "restart")
    rec["fixed"] = {"action": "undo", "by": actor, "at": int(time.time())}
    rec["dismissed"] = True
    _save(server, rec)
    return {"undone": res, "restarted": True}
