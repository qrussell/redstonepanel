"""Player access log: who joined, left or was turned away, per game server.

Read from what the servers already write, never from the network:
- Java (vanilla, Paper, Fabric): the systemd journal (the server's console), plus logs/latest.log
  and the dated logs/*.log.gz files for the first scan.
- Bedrock: server.log (its console, written through screen) and the journal.

Events are kept as JSON lines in DATA_DIR/players/<server>.jsonl:
    {"ts": 1731500000, "ev": "join" | "leave" | "refused", "name": "Steve", "id": "uuid/xuid",
     "reason": "not_whitelisted" | "banned" | "wrong_version" | "server_full"}   (reason: refused only)
Only names and IDs are stored. Log lines carry the player's address; it is never kept.

A background thread (started by run.py) scans every SCAN_EVERY seconds. The first scan of a
server also reads its older logs, so last-seen dates go back as far as the logs do. Events older
than RETENTION_DAYS are dropped.
"""
import datetime as dt
import gzip
import json
import os
import re
import subprocess
import threading
import time

from core import manager
from core.settings import DATA_DIR

LOG_DIR = os.path.join(DATA_DIR, "players")
STATE_FILE = os.path.join(LOG_DIR, "state.json")
SCAN_EVERY = 30
RETENTION_DAYS = 90
NAME_OK = re.compile(r"^minecraft[A-Za-z0-9_.-]*$")

REASONS = {
    "not_whitelisted": "not on the whitelist",
    "banned": "banned",
    "wrong_version": "wrong game version",
    "server_full": "server full",
}

ANSI_RE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
# journalctl -o short-unix:  1731500000.123 host java[123]: <message>
JOURNAL_RE = re.compile(r'^(\d+(?:\.\d+)?)\s+\S+\s+[^\s:\[]+(?:\[\d+\])?:\s?(.*)$')
# Java console prefix:
#   vanilla  [10:00:00] [Server thread/INFO]: <message>
#   Paper    [10:00:00 INFO]: <message>
#   Fabric   [10:00:00] [Server thread/INFO] (Minecraft) <message>
JAVA_PREFIX_RE = re.compile(r'^\[(\d{2}:\d{2}:\d{2})(?:\s+\w+)?\]:?\s*(?:\[[^\]]*\]:?\s*)?(?:\([^)]*\)\s*)?(.*)$')
# Bedrock console:  [2026-11-13 10:00:00:123 INFO] <message>
BEDROCK_PREFIX_RE = re.compile(r'^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?::\d+)?\s+\w+\]\s*(.*)$')

JAVA_NAME = r'[A-Za-z0-9_]{1,16}'
J_UUID = re.compile(rf'UUID of player ({JAVA_NAME}) is ([0-9a-fA-F-]{{32,36}})')
J_JOIN = re.compile(rf'^({JAVA_NAME})(?:\[[^\]]*\])? joined the game')
J_LEAVE = re.compile(rf'^({JAVA_NAME}) left the game')
# Turned away at login. The address in the brackets is matched and thrown away.
#   Disconnecting Steve (/203.0.113.7:51234): You are not whitelisted on this server!
#   Disconnecting com.mojang.authlib.GameProfile@1a2b[id=<uuid>,name=Steve,...] (/203.0.113.7:1): ...
#   Steve (/203.0.113.7:51234) lost connection: You are not white-listed on this server!
#   Disconnecting Steve (8f2c...-uuid) (/203.0.113.7:1): ...       (Paper)
J_REFUSED = re.compile(
    r'^(?:Disconnecting\s+)?'
    r'(?:com\.mojang\.authlib\.GameProfile@\w+\[id=(?P<gid>[0-9a-fA-F-]*|<null>|null),\s*name=(?P<gname>' + JAVA_NAME + r')[^\]]*\]'
    r'|(?P<name>' + JAVA_NAME + r')(?:\s+\((?P<pid>[0-9a-fA-F-]{32,36})\))?)'
    r'\s+\(/?[^)]*\)(?:\s+lost connection)?:\s*(?P<reason>.+)$')
B_JOIN = re.compile(r'Player connected:\s*(.+?),\s*xuid:\s*(\d*)')
B_LEAVE = re.compile(r'Player disconnected:\s*(.+?),\s*xuid:\s*(\d*)')


def refusal_reason(text):
    """Turns a disconnect message into one of REASONS, or None if it isn't a refused login."""
    t = text.lower()
    if "whitelist" in t or "white-list" in t or "allowlist" in t or "allow-list" in t or "not invited" in t:
        return "not_whitelisted"
    if "banned" in t:
        return "banned"
    if "outdated" in t or "incompatible" in t or "please use" in t or "version" in t:
        return "wrong_version"
    if "server is full" in t or "server full" in t:
        return "server_full"
    return None


class Parser:
    """Turns console messages into events. Remembers Java UUIDs announced before a join."""

    def __init__(self, java):
        self.java = java
        self.uuids = {}

    def parse(self, msg):
        msg = ANSI_RE.sub("", msg).strip()
        if not msg:
            return None
        if self.java:
            m = J_UUID.search(msg)
            if m:
                self.uuids[m.group(1).lower()] = m.group(2).lower()
                return None
            m = J_JOIN.match(msg)
            if m:
                return {"ev": "join", "name": m.group(1), "id": self.uuids.get(m.group(1).lower())}
            m = J_LEAVE.match(msg)
            if m:
                return {"ev": "leave", "name": m.group(1), "id": self.uuids.get(m.group(1).lower())}
            m = J_REFUSED.match(msg)
            if m:
                reason = refusal_reason(m.group("reason"))
                if not reason:
                    return None            # an ordinary disconnect; the "left the game" line covers it
                name = m.group("gname") or m.group("name")
                pid = m.group("gid") or m.group("pid")
                if pid in ("<null>", "null", ""):
                    pid = None
                return {"ev": "refused", "name": name, "id": (pid or self.uuids.get(name.lower()) or None),
                        "reason": reason}
            return None
        m = B_JOIN.search(msg)
        if m:
            return {"ev": "join", "name": m.group(1).strip(), "id": m.group(2) or None}
        m = B_LEAVE.search(msg)
        if m:
            return {"ev": "leave", "name": m.group(1).strip(), "id": m.group(2) or None}
        return None


# ---------------------------------------------------------------------------
# Reading the sources: each gives [(ts, message)]
# ---------------------------------------------------------------------------
def journal_lines(server, since=None):
    cmd = ["journalctl", "-u", f"{server}.service", "-o", "short-unix", "--no-pager", "-q"]
    if since:
        cmd += ["--since", f"@{int(since)}"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    return parse_journal(out)


def parse_journal(text):
    rows = []
    for line in text.splitlines():
        m = JOURNAL_RE.match(line)
        if not m:
            continue
        msg = m.group(2)
        p = JAVA_PREFIX_RE.match(msg) or BEDROCK_PREFIX_RE.match(msg)
        rows.append((float(m.group(1)), p.group(2) if p else msg))
    return rows


def java_file_lines(text, day):
    """Lines of a Java log that only carry HH:MM:SS, starting on `day`; the day moves on when the
    clock goes backwards (the log ran past midnight)."""
    rows, prev = [], None
    for line in text.replace("\r", "").splitlines():
        m = JAVA_PREFIX_RE.match(ANSI_RE.sub("", line).strip())
        if not m:
            continue
        h, mi, s = (int(x) for x in m.group(1).split(":"))
        secs = h * 3600 + mi * 60 + s
        if prev is not None and secs + 60 < prev:
            day += dt.timedelta(days=1)
        prev = secs
        rows.append((dt.datetime.combine(day, dt.time(h, mi, s)).timestamp(), m.group(2)))
    return rows


def latest_log_lines(text, mtime):
    """latest.log: walk back from the file's last change to find each line's day."""
    lines = text.replace("\r", "").splitlines()
    rows, prev, day = [], None, dt.date.fromtimestamp(mtime)
    for line in reversed(lines):
        m = JAVA_PREFIX_RE.match(ANSI_RE.sub("", line).strip())
        if not m:
            continue
        h, mi, s = (int(x) for x in m.group(1).split(":"))
        secs = h * 3600 + mi * 60 + s
        if prev is not None and secs > prev + 60:
            day -= dt.timedelta(days=1)
        prev = secs
        rows.append((dt.datetime.combine(day, dt.time(h, mi, s)).timestamp(), m.group(2)))
    rows.reverse()
    return rows


def bedrock_file_lines(text):
    rows = []
    for line in ANSI_RE.sub("", text).replace("\r", "").splitlines():
        m = BEDROCK_PREFIX_RE.match(line.strip())
        if m:
            try:
                ts = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
            except ValueError:
                continue
            rows.append((ts, m.group(2)))
    return rows


def _read(path):
    try:
        if path.endswith(".gz"):
            with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
                return f.read()
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except (OSError, EOFError):
        return ""


def file_lines(d, java, history=False):
    """The server's own log files. history=True also reads Java's rotated logs."""
    rows = []
    if java:
        logs = os.path.join(d, "logs")
        if history and os.path.isdir(logs):
            for f in sorted(os.listdir(logs)):
                m = re.match(r"(\d{4}-\d{2}-\d{2})-\d+\.log(\.gz)?$", f)
                if m:
                    rows += java_file_lines(_read(os.path.join(logs, f)),
                                            dt.date.fromisoformat(m.group(1)))
        latest = os.path.join(logs, "latest.log")
        if os.path.isfile(latest):
            rows += latest_log_lines(_read(latest), os.path.getmtime(latest))
    else:
        for f in ("server.log", "server.log.1"):
            p = os.path.join(d, f)
            if os.path.isfile(p) and (history or f == "server.log"):
                rows += bedrock_file_lines(_read(p))
    return rows


def events_from(rows, java):
    """[(ts, msg)] in any order -> sorted events, duplicates (same second, event and name) removed."""
    parser = Parser(java)
    out, seen = [], set()
    for ts, msg in sorted(rows, key=lambda r: r[0]):
        e = parser.parse(msg)
        if not e:
            continue
        e = {"ts": int(ts), **e}
        key = (e["ts"], e["ev"], e["name"].lower())
        if key in seen:
            continue
        seen.add(key)
        if not e.get("id"):
            e.pop("id", None)
        out.append(e)
    return out


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
_lock = threading.Lock()


def _file(server):
    if not NAME_OK.match(server or ""):
        raise ValueError("bad server name")
    return os.path.join(LOG_DIR, f"{server}.jsonl")


def _load_state():
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
        return s if isinstance(s, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state):
    os.makedirs(LOG_DIR, mode=0o750, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


def load(server):
    out = []
    try:
        with open(_file(server)) as f:
            for line in f:
                try:
                    e = json.loads(line)
                    if isinstance(e, dict) and "ts" in e and "ev" in e and "name" in e:
                        out.append(e)
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def _write_all(server, events):
    os.makedirs(LOG_DIR, mode=0o750, exist_ok=True)
    path = _file(server)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        for e in events:
            f.write(json.dumps(e, separators=(",", ":")) + "\n")
    os.chmod(tmp, 0o640)
    os.replace(tmp, path)


def merge(server, new, now=None):
    """Adds events not already stored, drops those past retention. Returns the events added."""
    now = now or time.time()
    cutoff = now - RETENTION_DAYS * 86400
    with _lock:
        old = load(server)
        keys = {(e["ts"], e["ev"], e["name"].lower()) for e in old}
        added = [e for e in new if (e["ts"], e["ev"], e["name"].lower()) not in keys and e["ts"] >= cutoff]
        kept = [e for e in old if e["ts"] >= cutoff]
        if added or len(kept) != len(old):
            _write_all(server, sorted(kept + added, key=lambda e: e["ts"]))
    return added


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------
_listeners = []      # callables(server, [events]) for new events after the first scan


def on_new_events(fn):
    _listeners.append(fn)


def notify_events(server, events):
    """Webhook messages for new joins and turned-away players (each event is opt-in)."""
    from core import notify
    for e in events:
        if e["ev"] == "join":
            notify.send("player_joined", server, f"{e['name']} joined {server}.")
        elif e["ev"] == "refused":
            notify.send("player_refused", server,
                        f"{e['name']} was turned away from {server}: {REASONS.get(e.get('reason'), 'refused')}.")


def scan(server, d, java, state):
    """Reads what's new for one server and stores it. Returns the new events."""
    st = state.setdefault(server, {})
    first = not st.get("backfilled")
    if first:
        rows = journal_lines(server) + file_lines(d, java, history=True)
    else:
        since = st.get("last", time.time() - 3600) - 120          # overlap; duplicates are dropped
        rows = journal_lines(server, since) + [r for r in file_lines(d, java) if r[0] >= since]
    added = merge(server, events_from(rows, java))
    newest = max((r[0] for r in rows), default=None)
    st["last"] = max(st.get("last") or 0, newest or 0) or time.time()
    st["backfilled"] = True
    if not first and added:
        for fn in _listeners:
            try:
                fn(server, added)
            except Exception as e:      # a listener (webhook) must never stop the scanner
                manager.write_log(f"[PLAYERS] Listener failed for {server}: {e}")
    return added


def scan_all():
    state = _load_state()
    for s in manager.list_servers():
        name = s.get("name", "")
        if not NAME_OK.match(name) or not s.get("directory"):
            continue
        try:
            scan(name, s["directory"], s.get("edition") != "Bedrock", state)
        except Exception as e:
            manager.write_log(f"[PLAYERS] Couldn't read {name}'s player log: {e}")
    _save_state(state)


_thread = None


def _loop():
    while True:
        try:
            scan_all()
        except Exception as e:
            manager.write_log(f"[PLAYERS] Player log scan failed: {e}")
        time.sleep(SCAN_EVERY)


def start():
    global _thread
    if _thread is None:
        _thread = threading.Thread(target=_loop, name="player-log", daemon=True)
        _thread.start()


def forget(server):
    """A removed and purged server: drop its log and scan state."""
    with _lock:
        try:
            os.remove(_file(server))
        except OSError:
            pass
        state = _load_state()
        if state.pop(server, None) is not None:
            _save_state(state)


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------
def history(server, player=None, ev=None, days=None, limit=500, now=None):
    """Newest first. player: part of a name or an exact ID; ev: join / leave / refused."""
    now = now or time.time()
    p = (player or "").strip().lower()
    out = []
    for e in reversed(load(server)):
        if ev and e["ev"] != ev:
            continue
        if days and e["ts"] < now - days * 86400:
            continue
        if p and p not in e["name"].lower() and p != str(e.get("id", "")).lower():
            continue
        out.append(e)
        if len(out) >= limit:
            break
    return out


def summary(server, now=None):
    """{name_lower: {name, id, last_join, last_refused, refused_24h, reason}}."""
    now = now or time.time()
    out = {}
    for e in load(server):
        k = e["name"].lower()
        p = out.setdefault(k, {"name": e["name"], "id": None, "last_join": None, "last_refused": None,
                               "refused_24h": 0, "reason": None})
        p["name"] = e["name"]
        if e.get("id"):
            p["id"] = e["id"]
        if e["ev"] == "join":
            p["last_join"] = e["ts"]
        elif e["ev"] == "refused":
            p["last_refused"] = e["ts"]
            p["reason"] = e.get("reason")
            if e["ts"] >= now - 86400:
                p["refused_24h"] += 1
    return out


def turned_away(server, allowed=(), hours=24, now=None):
    """Players turned away in the last `hours` who still aren't allowed: [(name, count, reason)]."""
    now = now or time.time()
    allowed = {a.lower() for a in allowed}
    out = []
    for k, p in summary(server, now).items():
        if not p["last_refused"] or p["last_refused"] < now - hours * 3600 or k in allowed:
            continue
        if p["last_join"] and p["last_join"] > p["last_refused"]:
            continue                           # got in since
        n = sum(1 for e in load(server) if e["ev"] == "refused" and e["name"].lower() == k
                and e["ts"] >= now - hours * 3600)
        out.append((p["name"], n, p["reason"]))
    return sorted(out, key=lambda x: -x[1])
