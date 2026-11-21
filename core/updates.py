"""Update alerts for every server, and Bedrock servers that update themselves (1.16.12).

One systemd timer for the machine, redstonepanel-updates.timer, runs `redstonepanel
check-updates` every few hours (so it works while the panel is stopped). Each run:

  1. looks up the newest Bedrock Dedicated Server (Mojang's download-links API), Minecraft
     release, Paper build and Fabric loader for each server on this machine, and caches what's
     newer than what the server runs in DATA_DIR/updates.json, so the dashboard never waits on
     the network. Snapshots and pre-releases are never offered.
  2. sends one webhook message per new version per server (update_available), never repeated.
  3. updates the Bedrock servers that have Update automatically on: warn players in game, wait
     five minutes, stop, take a "Before update" backup, swap the new files in, start, and wait
     for "Server started.". If it doesn't start, the old files and the world from the backup go
     back, the server is started on the old version, and the failure shows under Needs
     attention and as the update_failed webhook event.

Bedrock downloads come only from minecraft.net hosts. The zip is downloaded and checked before
anyone is warned or anything is stopped, so a failed download changes nothing. The zip's own
server.properties, permissions.json and allowlist.json never replace the server's.

Cluster nodes run their own timer; a master shows each node's alerts through the node's
server list (the `update` field of manager.list_servers()).
"""
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

from core import javaservers, manager, notify
from core.settings import DATA_DIR
from core.version import panel_version

STATE_FILE = os.path.join(DATA_DIR, "updates.json")
AUTO_FILE = os.path.join(DATA_DIR, "auto-updates.json")
UNIT_DIR = "/etc/systemd/system"
TIMER = "redstonepanel-updates.timer"
SERVICE = "redstonepanel-updates.service"
CHECK_CALENDAR = "*-*-* 00/6:15:00"           # every 6 hours: Bedrock players can't join an older server
VERSION_FILE = ".redstonepanel-bedrock.json"
NAME_RE = re.compile(r"^minecraft[A-Za-z0-9_.-]*$")

BEDROCK_LINKS = os.environ.get("RP_BEDROCK_LINKS") or \
    "https://net-secondary.web.minecraft-services.net/api/v1.0/download/links"
BEDROCK_PAGE = "https://www.minecraft.net/en-us/download/server/bedrock"
BEDROCK_HOSTS = ("minecraft.net", "minecraft-services.net")
BEDROCK_ZIP_RE = re.compile(r"bedrock-server-([0-9]+(?:\.[0-9]+)+)\.zip$")
BEDROCK_KEEP_FILES = ("server.properties", "permissions.json", "allowlist.json", "whitelist.json")
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) RedstonePanel"   # minecraft.net refuses some bare clients

WARN_MINUTES = int(os.environ.get("RP_UPDATE_WARN_MINUTES") or 5)   # the end-to-end test uses 1
START_WAIT = 180
STOP_WAIT = 60
KEEP_PRE_UPDATE = 3
TIMEOUT = 60
ANNOUNCED_MAX = 500


class UpdateError(ValueError):
    """An update that can't be done or failed (message is safe to show)."""


def _check_name(server):
    if not NAME_RE.fullmatch(server or ""):
        raise UpdateError("Invalid server name.")


# ---------------------------------------------------------------------------
# Small JSON files
# ---------------------------------------------------------------------------
def _read(path):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}

def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)

def load_state():
    s = _read(STATE_FILE)
    for key in ("servers", "results", "errors"):
        if not isinstance(s.get(key), dict):
            s[key] = {}
    if not isinstance(s.get("announced"), list):
        s["announced"] = []
    return s

def auto_enabled(server):
    return bool(_read(AUTO_FILE).get(server))


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------
def version_key(v):
    return javaservers.version_key(v)

def _allowed_url(url):
    override = os.environ.get("RP_BEDROCK_LINKS")    # a test stand-in for Mojang's API (root sets it)
    own = override and re.match(r"^(https?://[^/@?#]+)/", override)
    if own and (url or "").startswith(own.group(1) + "/"):
        return True
    m = re.match(r"^https://([^/:@?#]+)(/|$)", url or "")
    if not m:
        return False
    host = m.group(1).lower()
    return any(host == h or host.endswith("." + h) for h in BEDROCK_HOSTS)

def _get(url, accept="application/json"):
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA, "Accept": accept})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.read(4 << 20)
    except urllib.error.HTTPError as e:
        raise UpdateError(f"{_host(url)} answered HTTP {e.code}.")
    except (urllib.error.URLError, OSError) as e:
        raise UpdateError(f"Couldn't reach {_host(url)}: {getattr(e, 'reason', e)}")

def _host(url):
    return (url.split("/") + ["?"] * 3)[2]

def parse_bedrock_links(data):
    """The Linux server's {version, url} from the download-links API answer, or None."""
    try:
        links = data["result"]["links"]
    except (KeyError, TypeError):
        return None
    for link in links if isinstance(links, list) else []:
        if isinstance(link, dict) and link.get("downloadType") == "serverBedrockLinux":
            url = str(link.get("downloadUrl") or "")
            m = BEDROCK_ZIP_RE.search(url)
            if m and _allowed_url(url):
                return {"version": m.group(1), "url": url}
    return None

def bedrock_latest():
    """The newest Bedrock Dedicated Server release for Linux: {version, url}."""
    found, err = None, None
    try:
        found = parse_bedrock_links(json.loads(_get(BEDROCK_LINKS)))
    except (UpdateError, ValueError) as e:
        err = e
    if not found and not os.environ.get("RP_BEDROCK_LINKS"):
        try:
            page = _get(BEDROCK_PAGE, accept="text/html").decode("utf-8", "replace")
            for url in re.findall(r'https://[^"\s]*/bin-linux/bedrock-server-[0-9.]+\.zip', page):
                m = BEDROCK_ZIP_RE.search(url)
                if m and _allowed_url(url):
                    found = {"version": m.group(1), "url": url}
                    break
        except UpdateError as e:
            err = err or e
    if not found:
        raise UpdateError(f"Couldn't find the newest Bedrock server: {err or 'Mojang listed no Linux download'}.")
    return found

def bedrock_installed(d):
    """The Bedrock version in a server folder: what the panel installed, else what it logged."""
    info = _read(os.path.join(d, VERSION_FILE))
    if isinstance(info.get("version"), str) and info["version"]:
        return info["version"]
    return manager.server_version(d, False).get("version")

def _java_current(info):
    return {"loader": info.get("loader") or "vanilla", "minecraft": info.get("minecraft"),
            "build": str(info["build"]) if info.get("build") else None}

def java_update(info):
    """What's newer for a Java server ({label, latest, kind}), or None. Never a snapshot, never a
    downgrade. Vanilla: a newer Minecraft release. Paper: a newer release with a stable build,
    or a newer build of the same one. Fabric: a newer release, or a newer stable loader."""
    cur = _java_current(info)
    loader, mc = cur["loader"], cur["minecraft"]
    if loader not in javaservers.LOADERS or not mc or not javaservers.RELEASE_RE.match(str(mc)):
        return None
    try:
        target = javaservers.resolve(loader)
    except javaservers.JavaServerError as e:
        raise UpdateError(str(e))
    name = javaservers.LOADERS[loader]
    if version_key(target["minecraft"]) > version_key(mc):
        label = f"Minecraft {target['minecraft']}" if loader == "vanilla" else f"{name} {target['minecraft']}"
        return {"kind": "minecraft", "latest": target["minecraft"], "label": label}
    if version_key(target["minecraft"]) < version_key(mc):
        return None
    if loader == "paper":
        b = target.get("build")
        if b and cur["build"] and str(b).isdigit() and str(cur["build"]).isdigit() and int(b) > int(cur["build"]):
            return {"kind": "build", "latest": f"{mc} build {b}", "label": f"Paper {mc} build {b}"}
    if loader == "fabric":
        new = str(target.get("build") or "").replace("loader ", "")
        old = str(cur["build"] or "").replace("loader ", "")
        if new and old and version_key(new) > version_key(old):
            return {"kind": "build", "latest": f"loader {new}", "label": f"Fabric loader {new}"}
    return None

def _current_of(d, is_java):
    """A string naming what the folder runs now; a cached alert only counts while it matches."""
    if is_java:
        c = _java_current(javaservers.read_info(d))
        return f"{c['loader']} {c['minecraft']} {c['build'] or ''}".strip()
    return bedrock_installed(d)

def server_update(server, d, is_java):
    """The cached alert for a server ({label, latest, current, checked}), or None. Cheap: reads a
    file. Stale alerts (the server changed version since the check) are dropped."""
    entry = load_state()["servers"].get(server)
    if not isinstance(entry, dict):
        return None
    try:
        if entry.get("current") != _current_of(d, is_java):
            return None
    except Exception:
        return None
    return {k: entry.get(k) for k in ("label", "latest", "current", "kind", "checked")}


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------
def _servers():
    """[(name, folder, is_java)] for this machine's game servers."""
    out = []
    try:
        names = sorted(n[:-8] for n in os.listdir(UNIT_DIR)
                       if n.startswith("minecraft") and n.endswith(".service") and NAME_RE.fullmatch(n[:-8]))
    except OSError:
        names = []
    for name in names:
        try:
            with open(os.path.join(UNIT_DIR, f"{name}.service")) as f:
                unit = f.read()
        except OSError:
            continue
        if "Description=Minecraft Server (" not in unit:
            continue
        m = re.search(r"^WorkingDirectory=(.+)$", unit, re.M)
        d = m.group(1).strip() if m else f"/opt/{name}"
        is_java = "bedrock_server" not in unit
        out.append((name, d, is_java))
    return out

def check(only=None, announce=True):
    """Looks for updates for every server here (or just `only`). Returns the new state."""
    state = load_state()
    now = int(time.time())
    bedrock = None
    for name, d, is_java in _servers():
        if only and name != only:
            continue
        try:
            current = _current_of(d, is_java)
            if is_java:
                found = java_update(javaservers.read_info(d))
            elif not manager.bedrock_supported():
                found = None
            else:
                if bedrock is None:
                    bedrock = bedrock_latest()
                found = None
                if not current or version_key(bedrock["version"]) > version_key(current):
                    found = {"kind": "bedrock", "latest": bedrock["version"], "label": f"Bedrock {bedrock['version']}"}
            state["errors"].pop(name, None)
        except (UpdateError, javaservers.JavaServerError, OSError) as e:
            state["errors"][name] = {"at": now, "error": str(e)}
            continue
        if found:
            state["servers"][name] = {**found, "current": current, "checked": now}
            key = f"{name} {found['label']}"
            if announce and key not in state["announced"]:
                state["announced"].append(key)
                notify.send("update_available", name, f"{found['label']} is out; {name} runs "
                            f"{current or 'an unknown version'}. Update it on its Maintenance tab.")
                manager.write_log(f"[UPDATE] {found['label']} is available for {name} (runs {current}).")
        else:
            state["servers"].pop(name, None)
    present = {n for n, _, _ in _servers()}
    for key in ("servers", "errors"):
        for gone in [n for n in state[key] if n not in present]:
            state[key].pop(gone)
    state["announced"] = state["announced"][-ANNOUNCED_MAX:]
    if not only:
        state["checked"] = now
    _write(STATE_FILE, state)
    return state

def forget(server):
    """After a manual update: drop the server's alert until the next check."""
    state = load_state()
    if state["servers"].pop(server, None) is not None:
        _write(STATE_FILE, state)

def run_timer(sleep=time.sleep):
    """What the timer runs: check everything, then update the Bedrock servers that opted in."""
    ensure_timer()
    state = check()
    done = {}
    for name, d, is_java in _servers():
        entry = state["servers"].get(name)
        if is_java or not entry or not auto_enabled(name):
            continue
        try:
            done[name] = run_update(name, warn=WARN_MINUTES, actor="automatic update", sleep=sleep)
        except (UpdateError, manager.ServerError, manager.DependencyError) as e:
            done[name] = {"ok": False, "error": str(e)}
    return done


# ---------------------------------------------------------------------------
# Timer (one per machine)
# ---------------------------------------------------------------------------
def _service_unit():
    return f"""[Unit]
Description=RedstonePanel update check (and automatic Bedrock updates)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/sbin/redstonepanel check-updates
TimeoutStartSec={WARN_MINUTES * 60 + START_WAIT * 2 + 3600}
"""

def _timer_unit():
    return f"""[Unit]
Description=RedstonePanel update check every 6 hours

[Timer]
OnCalendar={CHECK_CALENDAR}
RandomizedDelaySec=900
Persistent=true

[Install]
WantedBy=timers.target
"""

def _write_if_changed(path, content):
    try:
        with open(path) as f:
            if f.read() == content:
                return False
    except OSError:
        pass
    with open(path, "w") as f:
        f.write(content)
    return True

def ensure_timer():
    """Writes and enables the check timer (the panel calls this at start). Never raises."""
    try:
        if not os.path.isdir(UNIT_DIR) or not os.path.isdir("/run/systemd/system"):
            return False
        changed = _write_if_changed(os.path.join(UNIT_DIR, SERVICE), _service_unit())
        changed = _write_if_changed(os.path.join(UNIT_DIR, TIMER), _timer_unit()) or changed
        if changed:
            subprocess.run(["systemctl", "daemon-reload"], capture_output=True)
        enabled = subprocess.run(["systemctl", "is-enabled", TIMER], capture_output=True, text=True).stdout.strip()
        if changed or enabled != "enabled":
            subprocess.run(["systemctl", "enable", "--now", TIMER], capture_output=True)
        return True
    except OSError:
        return False

def next_check():
    out = subprocess.run(["systemctl", "show", "-p", "NextElapseUSecRealtime", "--value", TIMER],
                         capture_output=True, text=True).stdout.strip()
    if not out or out == "n/a":
        return None
    r = subprocess.run(["date", "-d", out, "+%s"], capture_output=True, text=True)
    return int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip().isdigit() else None


# ---------------------------------------------------------------------------
# Per-server setting and what the Maintenance tab shows
# ---------------------------------------------------------------------------
def _edition_of(server):
    for name, d, is_java in _servers():
        if name == server:
            return d, is_java
    raise UpdateError(f"No Minecraft server called '{server}' on this panel.")

def set_auto(server, enabled, actor="?"):
    _check_name(server)
    d, is_java = _edition_of(server)
    if enabled and is_java:
        raise UpdateError("Only Bedrock servers update themselves: a new Java version can break mods "
                          "and plugins, so Java servers get an alert instead.")
    if enabled and not manager.bedrock_supported():
        raise UpdateError(manager.bedrock_unsupported_message())
    data = _read(AUTO_FILE)
    if enabled:
        data[server] = True
    else:
        data.pop(server, None)
    _write(AUTO_FILE, data)
    ensure_timer()
    manager.write_log(f"[UPDATE] {actor} turned automatic updates of {server} {'on' if enabled else 'off'}.")
    return overview(server)

def overview(server):
    _check_name(server)
    d, is_java = _edition_of(server)
    state = load_state()
    result = state["results"].get(server)
    return {"edition": "java" if is_java else "bedrock",
            "supported": not is_java and manager.bedrock_supported(),
            "auto": auto_enabled(server),
            "current": _current_of(d, is_java),
            "update": server_update(server, d, is_java),
            "error": (state["errors"].get(server) or {}).get("error"),
            "checked": state.get("checked"),
            "next_check": next_check(),
            "last": result if isinstance(result, dict) else None,
            "warn_minutes": WARN_MINUTES}

def check_one(server):
    """The Maintenance tab's Check now: this server only, right away."""
    _check_name(server)
    _edition_of(server)
    state = check(only=server)
    err = state["errors"].get(server)
    if err:
        raise UpdateError(err["error"])
    return overview(server)

def on_server_removed(server, purged):
    if not NAME_RE.fullmatch(server or ""):
        return
    if purged:
        data = _read(AUTO_FILE)
        if data.pop(server, None) is not None:
            _write(AUTO_FILE, data)
    state = load_state()
    changed = False
    for key in ("servers", "errors", "results"):
        if state[key].pop(server, None) is not None:
            changed = True
    if purged:                                   # a new server of the same name hears about versions again
        kept = [k for k in state["announced"] if not k.startswith(server + " ")]
        changed = changed or len(kept) != len(state["announced"])
        state["announced"] = kept
    if changed:
        _write(STATE_FILE, state)

def attention():
    """Needs attention items: automatic updates that failed (until the next one succeeds)."""
    out = []
    for server, r in load_state()["results"].items():
        if isinstance(r, dict) and not r.get("ok"):
            what = f"to Bedrock {r['to']}" if r.get("to") else ""
            back = (f" It was put back on {r['from']} and started again." if r.get("rolled_back") and r.get("from")
                    else "")
            out.append({"level": "error", "server": server, "where": "maintenance",
                        "text": f"Updating {server} {what} failed: {(r.get('error') or 'unknown error').rstrip('.')}.{back}"})
    return out


# ---------------------------------------------------------------------------
# Installing Bedrock files
# ---------------------------------------------------------------------------
def download_bedrock(latest, dest_dir):
    """Downloads the release zip into dest_dir and checks it. Returns the zip's path."""
    url = latest["url"]
    if not _allowed_url(url):
        raise UpdateError(f"Refused to download Bedrock from {_host(url)}: not a minecraft.net address.")
    path = os.path.join(dest_dir, "bedrock-server.zip")
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
    manager.write_log(f"[UPDATE] Downloading Bedrock {latest['version']} from {_host(url)} ...")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r, open(path, "wb") as f:
            if not _allowed_url(r.geturl()):
                raise UpdateError(f"The Bedrock download was redirected to {_host(r.geturl())}; refused.")
            shutil.copyfileobj(r, f, 1 << 20)
    except urllib.error.HTTPError as e:
        raise UpdateError(f"Downloading Bedrock {latest['version']} failed: {_host(url)} answered HTTP {e.code}.")
    except (urllib.error.URLError, OSError) as e:
        raise UpdateError(f"Downloading Bedrock {latest['version']} failed: {getattr(e, 'reason', e)}")
    members(path)                                     # refuses a broken or unexpected zip
    return path

def members(zip_path):
    """The zip's files as (zip member, relative path), refusing anything that would land outside
    the server folder and a zip without bedrock_server."""
    try:
        with zipfile.ZipFile(zip_path) as z:
            bad = z.testzip()
            if bad:
                raise UpdateError(f"The Bedrock download is damaged ({bad}); nothing was changed.")
            out = []
            for info in z.infolist():
                rel = info.filename.replace("\\", "/")
                parts = [p for p in rel.split("/") if p not in ("", ".")]
                if rel.startswith("/") or ".." in parts or not parts or ":" in parts[0]:
                    raise UpdateError(f"The Bedrock download has an unsafe path ({info.filename}); nothing was changed.")
                if stat.S_ISLNK(info.external_attr >> 16):
                    raise UpdateError("The Bedrock download contains a symbolic link; nothing was changed.")
                if not info.is_dir():
                    out.append((info, "/".join(parts)))
    except zipfile.BadZipFile:
        raise UpdateError("The Bedrock download isn't a zip file; nothing was changed.")
    if not any(rel == "bedrock_server" for _, rel in out):
        raise UpdateError("The Bedrock download has no bedrock_server; nothing was changed.")
    return out

def _safe_target(d, rel):
    """d/rel, refusing a path that goes through a symbolic link inside the server folder."""
    path = d
    for part in rel.split("/")[:-1]:
        path = os.path.join(path, part)
        if os.path.islink(path) or (os.path.exists(path) and not os.path.isdir(path)):
            raise UpdateError(f"{os.path.relpath(path, d)} in the server folder is a link or a file; "
                              f"move it away and try again.")
    target = os.path.join(d, rel)
    if os.path.islink(target) or os.path.isdir(target):
        raise UpdateError(f"{rel} in the server folder is a link or a folder; move it away and try again.")
    return target

def install_files(d, zip_path, stash=None):
    """Puts the zip's files into d, keeping the server's own settings files. With stash, every file
    it replaces is moved there first; returns the files it created, for put_back()."""
    files = members(zip_path)
    for _, rel in files:                             # check every path before changing anything
        _safe_target(d, rel)
    created = []
    kept = []
    with zipfile.ZipFile(zip_path) as z:
        for info, rel in files:
            target = os.path.join(d, rel)
            if rel in BEDROCK_KEEP_FILES and os.path.exists(target):
                kept.append(rel)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if os.path.exists(target):
                if stash:
                    old = os.path.join(stash, rel)
                    os.makedirs(os.path.dirname(old), exist_ok=True)
                    os.replace(target, old)
            else:
                created.append(rel)
            tmp = target + ".rp-new"
            with z.open(info) as src, open(tmp, "wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)
            mode = (info.external_attr >> 16) & 0o777
            os.chmod(tmp, 0o755 if rel == "bedrock_server" or mode & 0o111 else 0o644)
            os.replace(tmp, target)
    if kept:
        manager.write_log(f"[SYSTEM] Kept the server's own {', '.join(kept)}.")
    return created

def put_back(d, stash, created):
    """Undoes install_files(): removes what it created, moves the replaced files back."""
    for rel in created:
        try:
            os.remove(os.path.join(d, rel))
        except OSError:
            pass
    for root, _, names in os.walk(stash):
        for n in names:
            old = os.path.join(root, n)
            rel = os.path.relpath(old, stash)
            target = os.path.join(d, rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            os.replace(old, target)

def _write_version(d, version):
    with open(os.path.join(d, VERSION_FILE), "w") as f:
        json.dump({"version": version, "installed": int(time.time())}, f)

def fetch_bedrock(d):
    """New or binary-less servers (deploy, restore, import): the newest release into d."""
    latest = bedrock_latest()
    os.makedirs(d, exist_ok=True)
    staging = tempfile.mkdtemp(prefix=".redstonepanel-update-", dir=d)
    try:
        install_files(d, download_bedrock(latest, staging))
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    _write_version(d, latest["version"])
    manager.write_log(f"[SYSTEM] Installed Bedrock {latest['version']}.")
    return latest["version"]


# ---------------------------------------------------------------------------
# Updating a Bedrock server
# ---------------------------------------------------------------------------
def warning_steps(warn):
    steps = []
    if warn:
        steps.append((warn * 60, f"Server updates to the new Minecraft version in {warn} minute{'s' if warn != 1 else ''}."))
        if warn > 1:
            steps.append((60, "Server updates in 1 minute. It will be back shortly."))
        steps.append((10, "Server updates in 10 seconds."))
    return steps

def _log_size(d):
    try:
        return os.path.getsize(os.path.join(d, "server.log"))
    except OSError:
        return 0

def _started_since(d, offset):
    """True once BDS logged "Server started." after offset, else None."""
    try:
        with open(os.path.join(d, "server.log"), "rb") as f:
            size = os.fstat(f.fileno()).st_size
            f.seek(offset if offset <= size else 0)       # screen may have started a new log
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    if "Server started." in text:
        return True
    return None

def _active(server):
    return subprocess.run(["systemctl", "is-active", f"{server}.service"],
                          capture_output=True, text=True).stdout.strip()

def wait_started(server, d, offset, timeout=START_WAIT, sleep=time.sleep):
    """True once the server says it started; "stopped" if the unit stops, "timeout" if it takes
    too long (both false-y only in the sense that they aren't True: compare with `is True`)."""
    deadline = time.time() + timeout
    seen_inactive = 0
    while time.time() < deadline:
        if _started_since(d, offset):
            return True
        st = _active(server)
        if st in ("inactive", "failed"):
            seen_inactive += 1
            if seen_inactive >= 3:                    # screen exits 0 when bedrock_server dies
                return "stopped"
        sleep(2)
    return "timeout"

def stop_gracefully(server, timeout=STOP_WAIT, sleep=time.sleep):
    """Bedrock's own `stop` (it saves the world and logs "Quit correctly"), then systemd's stop
    for whatever is left."""
    from core import backups
    if backups._screen(server, "stop"):
        for _ in range(timeout):
            if _active(server) != "active":
                break
            sleep(1)
    manager.run_and_log(["systemctl", "stop", f"{server}.service"])

def _say(server, msg):
    from core import backups
    return backups._screen(server, f"say {msg}")

def _record(server, result):
    state = load_state()
    if result.get("ok") and result.get("skipped"):
        return
    state["results"][server] = result
    if result.get("ok"):
        state["servers"].pop(server, None)
    _write(STATE_FILE, state)

def _prune_pre_update(server):
    from core import backups
    old = [b for b in backups.list_backups(server) if b["kind"] == "pre-update"]
    for b in old[KEEP_PRE_UPDATE:]:
        try:
            os.remove(os.path.join(backups.server_backup_dir(server), b["file"]))
            manager.write_log(f"[BACKUP] Removed old backup {b['file']} (keeping {KEEP_PRE_UPDATE} from before updates).")
        except OSError:
            pass

def run_update(server, warn=0, actor="?", sleep=time.sleep, force=False):
    """Updates a Bedrock server to the newest release, safely. Returns the result it records.
    warn: minutes of in-game warnings first (automatic updates). force: download even if the
    server already runs the newest version (the Update button)."""
    from core import backups
    _check_name(server)
    d, is_java = _edition_of(server)
    if is_java:
        raise UpdateError("Java servers are updated with the Update button, choosing the version.")
    manager.require_bedrock_machine()
    if not os.path.isdir(d):
        raise UpdateError(f"{server}'s folder {d} is missing.")
    latest = bedrock_latest()
    before = bedrock_installed(d)
    if not force and before and version_key(latest["version"]) <= version_key(before):
        return {"ok": True, "skipped": True, "from": before, "to": latest["version"]}
    started_at = int(time.time())
    result = {"at": started_at, "ok": False, "from": before, "to": latest["version"], "by": actor}
    staging = tempfile.mkdtemp(prefix=".redstonepanel-update-", dir=d)
    stash = os.path.join(staging, "old")
    created = None
    pre = None
    was_running = _active(server) == "active"
    try:
        try:
            zip_path = download_bedrock(latest, staging)     # nothing is stopped if this fails
        except UpdateError as e:
            result["error"] = str(e)
            raise
        manager.write_log(f"[UPDATE] Updating {server} from Bedrock {before or '(unknown)'} to "
                          f"{latest['version']} ({actor}).")
        if was_running and warn:
            for i, (before_s, msg) in enumerate(warning_steps(warn)):
                _say(server, msg)
                steps = warning_steps(warn)
                sleep(before_s - (steps[i + 1][0] if i + 1 < len(steps) else 0))
            was_running = _active(server) == "active"
        if was_running:
            stop_gracefully(server, sleep=sleep)
        try:
            pre = backups.run_backup(server, "pre-update")
        except Exception as e:
            result["error"] = f"the backup before updating failed ({e}), so nothing was changed"
            raise UpdateError(result["error"])
        result["backup"] = pre["file"]
        _prune_pre_update(server)
        offset = _log_size(d)
        try:
            created = install_files(d, zip_path, stash=stash)
            _write_version(d, latest["version"])
        except (UpdateError, OSError, zipfile.BadZipFile) as e:
            result["error"] = f"installing the new files failed: {e}"
            raise UpdateError(result["error"])
        manager.run_and_log(["chown", "-R", "minecraft:minecraft", d])
        if was_running:
            manager.run_and_log(["systemctl", "start", f"{server}.service"])
            how = wait_started(server, d, offset, sleep=sleep)
            if how is not True:
                result["error"] = (f"Bedrock {latest['version']} stopped while starting" if how == "stopped" else
                                   f"Bedrock {latest['version']} didn't finish starting within {START_WAIT // 60} minutes")
                raise UpdateError(result["error"])
        result["ok"] = True
        manager.write_log(f"[UPDATE] {server} now runs Bedrock {latest['version']}"
                          + (f" (was {before})." if before else "."))
        notify.send("updated", server, f"{server} was updated to Bedrock {latest['version']}"
                    + (f" (from {before})." if before else "."))
        return result
    except Exception as e:
        result.setdefault("error", str(e) or type(e).__name__)
        if created is not None:
            result["rolled_back"] = _roll_back(server, d, stash, created, before, pre, was_running)
        elif was_running and _active(server) != "active":
            manager.run_and_log(["systemctl", "start", f"{server}.service"])
        manager.write_log(f"[UPDATE] Updating {server} failed: {result.get('error')}")
        notify.send("update_failed", server, f"Updating {server} to Bedrock {latest['version']} failed: "
                    f"{result['error'].rstrip('.')}." + (f" It's back on {before}." if result.get("rolled_back") else ""))
        raise UpdateError(result["error"]) from e
    finally:
        _record(server, result)
        shutil.rmtree(staging, ignore_errors=True)

def _roll_back(server, d, stash, created, before, pre, was_running):
    """Puts the old files and the world from the "Before update" backup back. Returns True if the
    old version is back (and running, if it was)."""
    from core import backups
    manager.write_log(f"[UPDATE] Putting {server} back on Bedrock {before or 'the previous version'}.")
    subprocess.run(["systemctl", "stop", f"{server}.service"], capture_output=True)
    try:
        put_back(d, stash, created)
        if before:
            _write_version(d, before)
        else:
            try:
                os.remove(os.path.join(d, VERSION_FILE))
            except OSError:
                pass
        if pre:
            # The new version may have converted the world before it failed; the old one can't read that
            backups.restore_backup(server, pre["file"])
        manager.run_and_log(["chown", "-R", "minecraft:minecraft", d])
    except Exception as e:
        manager.write_log(f"[UPDATE] Putting {server} back failed: {e}. Its \"Before update\" backup is "
                          f"{pre['file'] if pre else 'missing'}.")
        return False
    if was_running:
        offset = _log_size(d)
        manager.run_and_log(["systemctl", "start", f"{server}.service"])
        return wait_started(server, d, offset) is True
    return True
