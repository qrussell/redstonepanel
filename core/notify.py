"""Webhook notifications: Discord, or any URL that takes a JSON POST.

Game server events come from a systemd drop-in shipped with the package
(/usr/lib/systemd/system/minecraft-.service.d/redstonepanel.conf). Its ExecStartPost and
ExecStopPost run `redstonepanel notify-unit start|stop NAME` as root, so a crash is reported
even while the panel is stopped. Backups and scheduled restarts call send() themselves.

The webhook URL is a secret (anyone holding it can post to the channel): it's kept in a
root-only file and the API only ever shows a hint of it.
"""
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from core import settings
from core.settings import DATA_DIR
from core.platform import console, paths, services, CommandError

CONFIG_FILE = os.path.join(DATA_DIR, "notifications.json")
STATE_FILE = os.path.join(DATA_DIR, "notify-state.json")
UNIT_DIR = paths.UNIT_DIR

EVENTS = {
    "crashed": "Server crashed",
    "started": "Server started",
    "stopped": "Server stopped",
    "restarted": "Scheduled restart",
    "backup_ok": "Backup finished",
    "backup_failed": "Backup failed",
    "backup_copy_failed": "Backup copy to another member or storage failed",
    "player_joined": "Player joined",
    "player_refused": "Player turned away",
    "update_available": "Update available",
    "updated": "Bedrock server updated",
    "update_failed": "Server update failed",
    "type_changed": "Server type changed",
    "tps_low": "TPS under 15 for 5 minutes",
    "disk_full": "Disk over 95 % full",
}
DEFAULT_EVENTS = ["crashed", "backup_failed", "backup_copy_failed", "restarted", "update_available", "update_failed"]
# A server stuck in a crash loop restarts every 20 s; one message per event per server is enough
QUIET_SECONDS = {"crashed": 300, "started": 60, "stopped": 60, "player_refused": 60,
                 "tps_low": 3600, "disk_full": 3600}   # 1.17's alerts: at most once an hour each
TIMEOUT = 5
DISCORD_HOSTS = {"discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com"}
NAME_RE = re.compile(r"^minecraft[A-Za-z0-9_.-]*$")


class NotifyError(ValueError):
    """A notification setting that can't be saved (message is safe to show)."""


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def load():
    cfg = {"url": "", "events": list(DEFAULT_EVENTS)}
    try:
        with open(CONFIG_FILE) as f:
            stored = json.load(f)
        if isinstance(stored, dict):
            if isinstance(stored.get("url"), str):
                cfg["url"] = stored["url"]
            if isinstance(stored.get("events"), list):
                cfg["events"] = [e for e in stored["events"] if e in EVENTS]
    except (OSError, ValueError):
        pass
    return cfg

def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)

def validate_url(url):
    url = (url or "").strip()
    if len(url) > 2048 or any(c.isspace() for c in url):
        raise NotifyError("That doesn't look like a webhook URL.")
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ("https", "http") or not p.hostname:
        raise NotifyError("The webhook URL must start with https:// (or http:// on your own network).")
    return url

def kind(url):
    p = urllib.parse.urlsplit(url or "")
    if (p.hostname or "").lower() in DISCORD_HOSTS and p.path.startswith("/api/webhooks/"):
        return "discord"
    return "generic" if url else None

def hint(url):
    """Enough of the URL to recognise it, not enough to use it."""
    if not url:
        return ""
    p = urllib.parse.urlsplit(url)
    tail = url.rstrip("/")[-4:]
    return f"{p.scheme}://{p.hostname}/…{tail}"

def public():
    cfg = load()
    return {"configured": bool(cfg["url"]), "kind": kind(cfg["url"]), "url_hint": hint(cfg["url"]),
            "events": cfg["events"], "event_names": EVENTS}

def save(url=None, events=None, clear=False):
    """url None keeps the stored one; clear=True removes it."""
    cfg = load()
    if clear:
        cfg["url"] = ""
    elif url:
        cfg["url"] = validate_url(url)
    if events is not None:
        if not isinstance(events, list) or any(e not in EVENTS for e in events):
            raise NotifyError("Unknown notification event.")
        cfg["events"] = [e for e in EVENTS if e in events]
    _write_json(CONFIG_FILE, cfg)
    return public()


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------
def _log(msg):
    try:
        from core import manager
        manager.write_log(msg)
    except Exception:
        pass

def payload(url, event, server, message):
    panel = settings.display_name()
    if kind(url) == "discord":
        return {"username": "RedstonePanel", "content": f"**{panel}**: {message}"[:2000],
                "allowed_mentions": {"parse": []}}
    return {"text": f"{panel}: {message}", "event": event, "server": server, "panel": panel,
            "message": message, "time": int(time.time())}

def _post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "RedstonePanel"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        r.read(1024)

def _quiet(event, server):
    """True if this event was sent for this server (or disk, for disk_full) moments ago."""
    window = QUIET_SECONDS.get(event)
    if not window or not server:
        return False
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
        if not isinstance(state, dict):
            state = {}
    except (OSError, ValueError):
        state = {}
    key, now = f"{server}:{event}", time.time()
    last = state.get(key, 0)
    if isinstance(last, (int, float)) and now - last < window:
        return True
    state = {k: v for k, v in state.items() if isinstance(v, (int, float)) and now - v < 86400}
    state[key] = now
    try:
        _write_json(STATE_FILE, state)
    except OSError:
        pass
    return False

def send(event, server, message, test=False, quiet_key=None):
    """Posts one notification. Never raises; returns (sent, error). test=True ignores the event list.
    quiet_key: what the once-in-a-while window counts by, if not the server (a disk, for disk_full)."""
    cfg = load()
    if not cfg["url"]:
        return False, "No webhook URL is set."
    if not test and (event not in cfg["events"] or _quiet(event, quiet_key or server)):
        return False, None
    try:
        _post(cfg["url"], payload(cfg["url"], event, server, message))
        return True, None
    except urllib.error.HTTPError as e:
        err = f"the webhook answered HTTP {e.code}"
    except (urllib.error.URLError, OSError, ValueError) as e:
        err = f"couldn't reach the webhook ({getattr(e, 'reason', e)})"
    _log(f"[NOTIFY] Couldn't send '{EVENTS.get(event, event)}' for {server or 'the panel'}: {err}.")
    return False, err[0].upper() + err[1:] + "."

def send_test():
    return send("test", None, "Test message: notifications from this panel work.", test=True)


# ---------------------------------------------------------------------------
# Game server start/stop, from the systemd drop-in
# ---------------------------------------------------------------------------
def _is_panel_server(server):
    if not NAME_RE.fullmatch(server or ""):
        return False
    try:
        with open(os.path.join(UNIT_DIR, f"{server}.service")) as f:
            return "Description=Minecraft Server (" in f.read()
    except OSError:
        return False

def stop_requested(server):
    """True if systemd was asked to stop or restart the unit (a stop or restart job is queued).

    Exit statuses can't tell: Bedrock's screen exits 1 on every normal stop. A crash has no such
    job queued while ExecStopPost runs; `systemctl stop/restart` (and the panel) always does.
    A start job doesn't count (1.17.1): a server that dies within a second of starting (the
    wrong Java, say) exits while its start job is still waiting for ExecStartPost."""
    try:
        jobs = services.queued_jobs(server, timeout=5)
    except CommandError:
        return False
    if any(job != "start" for job in jobs):
        return True
    return False

def describe_stop(env, requested=False):
    """(event, message) for an ExecStopPost run, from the environment systemd sets."""
    result = env.get("SERVICE_RESULT", "")
    if requested or result in ("", "success"):
        return "stopped", "stopped."
    code, status = env.get("EXIT_CODE", ""), env.get("EXIT_STATUS", "")
    how = {"exited": f"exit status {status}", "killed": f"killed by SIG{status}",
           "dumped": f"crashed with SIG{status}"}.get(code, result)
    return "crashed", f"crashed ({how}). systemd starts it again in 20 seconds."

def _unit_text(server):
    try:
        with open(os.path.join(UNIT_DIR, f"{server}.service")) as f:
            return f.read()
    except OSError:
        return ""

def bedrock_dir(server):
    """The server's folder if it's a Bedrock server (run under screen), else None."""
    text = _unit_text(server)
    if "bedrock_server" not in text:
        return None
    for line in text.splitlines():
        if line.startswith("WorkingDirectory="):
            return line.split("=", 1)[1].strip()
    return None

def bedrock_quit_cleanly(d):
    """True if the server's console log ends with Bedrock's "Quit correctly" (a `stop` command)."""
    try:
        with open(os.path.join(d, "server.log"), "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 4096))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return False
    lines = [l.strip() for l in tail.splitlines() if l.strip()]
    return any(l.endswith("Quit correctly") for l in lines[-5:])

BEDROCK_STOP_WAIT = 75        # systemd's own stop timeout is 90 s; leave it time to clean up

def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True

def stop_bedrock(server, mainpid=None, timeout=BEDROCK_STOP_WAIT, sleep=time.sleep, run=None):
    """Run by the drop-in's ExecStop before systemd stops a game server (Stop, Restart, backups,
    restores, moves, updates, scheduled restarts, shutting the machine down).

    Bedrock runs under screen, and systemd's SIGTERM goes to screen, which hangs up on
    bedrock_server: it quits without saving. So type Bedrock's own `stop` into its console
    (it saves the world and logs "Quit correctly") and wait for it to exit; systemd then
    stops whatever is left. Java saves on SIGTERM by itself, so nothing is done for it.
    Returns "saved", "timeout", "not-running" or None (not a panel Bedrock server)."""
    if not _is_panel_server(server):
        return None
    d = bedrock_dir(server)
    if not d:
        return None
    try:
        r = console.send(server, "stop", timeout=10, run=run, find=False)
        sent = r.returncode == 0
    except CommandError:
        sent = False
    if not sent:
        return "not-running"           # no screen session: the game isn't there to save
    _log(f"[SYSTEM] Asked {server} to save its world and stop (Bedrock's own stop command).")
    for _ in range(timeout):
        if mainpid and not _pid_alive(mainpid):
            return "saved"
        if not mainpid and bedrock_quit_cleanly(d):
            return "saved"
        sleep(1)
    _log(f"[WARNING] {server} didn't stop within {timeout} s of its stop command; systemd stops it now.")
    return "timeout"

def revive(server):
    """Starts a crashed Bedrock server again in 20 s, as Restart=on-failure does for Java.

    Bedrock runs under screen, which exits 0 when the game dies, so systemd sees a clean exit
    and doesn't restart it."""
    services.start_later(server, 20, timeout=10)

def unit_event(phase, server, env=None):
    if not _is_panel_server(server):
        return
    if phase == "start":
        event, message = "started", "started."
        try:                                   # it now uses any mod port the panel moved
            from core import modports
            modports.started(server)
        except Exception:
            pass
    else:
        requested = stop_requested(server)
        event, message = describe_stop(os.environ if env is None else env, requested)
        d = None if requested or event == "crashed" else bedrock_dir(server)
        if d and not bedrock_quit_cleanly(d):
            event, message = "crashed", "crashed (the game ended without shutting down). Starting it again in 20 seconds."
            try:
                revive(server)
            except Exception as e:
                _log(f"[CRASH] Couldn't schedule a restart of {server}: {e}")
        if event == "crashed":
            _log(f"[CRASH] {server} {message}")
        if not requested:
            # 1.17.1: why? (a crash, or a stop nobody asked for, such as a failed start)
            why = explain_stop(server, os.environ if env is None else env, event == "crashed")
            if why:
                message = f"{message} {why}"
                if event == "crashed":
                    _log(f"[CRASH] {server}: {why}")
    send(event, server, f"{server} {message}")

def explain_stop(server, env, crashed):
    """core/crashes.py's one-line cause for this stop (saved for the Last crash card), or None."""
    from core import crashes
    d = None
    for line in _unit_text(server).splitlines():
        if line.startswith("WorkingDirectory="):
            d = line.split("=", 1)[1].strip()
    if not d or not os.path.isdir(d):
        return None
    try:                                       # a mod port another server holds: move it before systemd restarts it
        from core import modports
        modports.resolve(only=[server])
    except Exception as e:
        _log(f"[PORTS] Checking {server}'s mod and plugin ports failed: {e}")
    rec = crashes.record(server, d, "Bedrock" if "bedrock_server" in _unit_text(server) else None,
                         dict(env), crashed=crashed)
    return crashes.summary(rec)
