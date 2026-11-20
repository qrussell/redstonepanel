"""Updating RedstonePanel itself from the panel (admins).

The panel can't run the upgrade in its own process: the package's postinst restarts
redstonepanel.service, and systemd would kill apt along with the panel in the middle of dpkg.
So the panel starts `redstonepanel upgrade` as a transient unit of its own (systemd-run), which
carries on through the restart. That command runs `apt-get update` and upgrades only the
redstonepanel package, writing its output to upgrade.log and the outcome to upgrade.json in the
data folder. The new panel reads them back, and the page reloads once it's answering again.

Game servers are separate units, so they keep running throughout. Changed configuration files
are kept (--force-confold), because nothing may stop to ask a question.
"""
import json
import os
import shutil
import subprocess
import sys
import time

from core import manager
from core.settings import DATA_DIR, PACKAGED

UNIT = "redstonepanel-upgrade"
PACKAGE = "redstonepanel"
STATE_FILE = os.path.join(DATA_DIR, "upgrade.json")
LOG_FILE = os.path.join(DATA_DIR, "upgrade.log")
CLI = "/usr/sbin/redstonepanel"
APT_ENV = dict(os.environ, DEBIAN_FRONTEND="noninteractive", LC_ALL="C", LANG="C")
APT_OPTS = ["-q", "-o", "DPkg::Lock::Timeout=300", "-o", "Dpkg::Use-Pty=0",
            "-o", "Dpkg::Options::=--force-confdef", "-o", "Dpkg::Options::=--force-confold"]
STALE = 3600           # a "running" upgrade whose unit is gone and that is this old failed


class UpdateError(ValueError):
    """An update that can't be started (message is safe to show)."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _read_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
        return st if isinstance(st, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(st):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f)
    os.replace(tmp, STATE_FILE)


def _log_tail(n=25):
    try:
        with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
            return [l.rstrip("\n") for l in f.readlines()[-n:]]
    except OSError:
        return []


def _unit_active():
    try:
        out = subprocess.run(["systemctl", "is-active", f"{UNIT}.service"], capture_output=True, text=True,
                             timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False
    return out in ("active", "activating", "deactivating")


def _installed_version():
    try:
        r = subprocess.run(["dpkg-query", "-W", "-f=${Version}", PACKAGE], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() or None if r.returncode == 0 else None


def last_upgrade():
    """The last upgrade from the panel: {state: running|done|failed, from, to, actor, ...}."""
    st = _read_state()
    if st.get("state") == "running" and not _unit_active() and time.time() - (st.get("started") or 0) > 60:
        # The unit is gone without writing a result (killed, or the machine restarted)
        if time.time() - (st.get("started") or 0) > STALE or _installed_version() != st.get("to"):
            st = dict(st, state="failed", error="The update stopped without finishing; see the log.")
        else:
            st = dict(st, state="done")
    return st or None


def status():
    from core import host
    from core.version import panel_version
    st = last_upgrade()
    return {"packaged": PACKAGED, "version": panel_version(), "installed": _installed_version() if PACKAGED else None,
            "available": host.panel_update() if PACKAGED else None,
            "upgrade": dict(st, lines=_log_tail()) if st else None}


def _require_package():
    if not PACKAGED:
        raise UpdateError("This panel runs from a source checkout, not the redstonepanel package; update it with git.")
    if not shutil.which("apt-get") or not shutil.which("systemd-run"):
        raise UpdateError("apt-get and systemd-run are needed to update from the panel.")


def check():
    """Refreshes apt's package lists, so a new release shows without waiting for the daily update."""
    _require_package()
    if _unit_active():
        raise UpdateError("An update is running.", 409)
    try:
        r = subprocess.run(["apt-get", *APT_OPTS[:3], "update"], capture_output=True, text=True, timeout=300,
                           env=APT_ENV)
    except (OSError, subprocess.SubprocessError) as e:
        raise UpdateError(f"Couldn't check for updates: {e}", 502)
    if r.returncode != 0:
        err = next((l[3:] for l in (r.stdout + r.stderr).splitlines()[::-1] if l.startswith("E: ")), None)
        raise UpdateError(f"Couldn't check for updates: {err or 'apt-get update failed'}", 502)
    from core import host
    host.forget_panel_update()
    return status()


def start(actor="admin"):
    """Starts the upgrade in its own systemd unit. Returns the status."""
    from core import host, runtimes
    from core.version import panel_version
    _require_package()
    if _unit_active():
        raise UpdateError("An update is already running.", 409)
    job = runtimes.job()
    if job and job.get("state") == "running":
        raise UpdateError("Java is being installed or removed; update when that's finished.", 409)
    available = host.panel_update()
    if not available:
        raise UpdateError("There's no newer RedstonePanel to install. Check for updates first.")
    current = panel_version()
    _write_state({"state": "running", "from": current, "to": available, "actor": actor,
                  "started": int(time.time()), "finished": None, "error": None})
    with open(LOG_FILE, "w", encoding="utf-8"):
        pass
    cmd = ["systemd-run", f"--unit={UNIT}", "--collect", "--quiet", "--description=RedstonePanel update",
           CLI, "upgrade"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        r = None
        err = str(e)
    if r is None or r.returncode != 0:
        err = err if r is None else (r.stderr.strip() or f"systemd-run exited with code {r.returncode}")
        _write_state(dict(_read_state(), state="failed", finished=int(time.time()), error=err))
        raise UpdateError(f"Couldn't start the update: {err}", 500)
    manager.write_log(f"[UPDATE] {actor} is updating RedstonePanel {current} -> {available}. The panel restarts; "
                      f"game servers keep running.")
    return status()


def run(echo=False):
    """`redstonepanel upgrade`: apt-get update, then upgrade only redstonepanel. Records the
    outcome in upgrade.json (the panel restarts in the middle of this). Returns the exit code."""
    st = _read_state()
    if st.get("state") != "running":           # run by hand, not from the panel
        st = {"state": "running", "from": _installed_version(), "to": None, "actor": "command line",
              "started": int(time.time()), "finished": None, "error": None}
        _write_state(st)
    code, error = 0, None
    with open(LOG_FILE, "a", encoding="utf-8") as log:
        def step(args):
            cmd = ["apt-get", *APT_OPTS, *args]
            log.write("$ " + " ".join(cmd) + "\n")
            log.flush()
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=APT_ENV)
            last_err = None
            for line in p.stdout:
                log.write(line)
                log.flush()
                if echo:
                    sys.stdout.write(line)
                if line.startswith("E: "):
                    last_err = line[3:].strip()
            return p.wait(), last_err
        for args in (["update"], ["install", "--only-upgrade", "-y", PACKAGE]):
            code, err = step(args)
            if code != 0:
                error = err or f"apt-get {args[0]} exited with code {code}"
                break
    new = _installed_version()
    if code == 0 and st.get("to") and new != st.get("to"):
        code, error = 1, f"apt finished, but {PACKAGE} {new} is installed, not {st['to']}."
    st.update(state="done" if code == 0 else "failed", finished=int(time.time()), error=error, installed=new)
    if not st.get("to"):
        st["to"] = new
    _write_state(st)
    manager.write_log(f"[UPDATE] RedstonePanel {st.get('from')} -> {new}: "
                      + ("done." if code == 0 else f"failed: {error}"))
    return code
