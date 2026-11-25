"""Removing RedstonePanel from a machine without leaving game servers behind by accident (1.23.2).

Uninstalling the panel never deletes a world on its own. What happens to each game server is the
owner's choice, made before the panel goes, on Panel Settings > Remove RedstonePanel or with
`sudo redstonepanel uninstall`:

  stop    Stop and disable it, keep its files (the same as Uninstall with Keep World). It shows up
          under New server > Removed servers once the panel is installed again. Recommended.
  keep    Leave it running as an ordinary service with nothing to manage it: it keeps starting at
          boot and holding its ports, with no backups, restarts or updates. Linux only (on Windows
          the service runs through the panel's own program, which uninstalling removes).
  delete  Remove it and delete its folder (the same as Uninstall without Keep World). Only after a
          backup from the last 24 hours, and only when the owner types DELETE. Its backups stay
          unless they ask for those to go too.

The package's prerm (Linux) runs `redstonepanel uninstall-hook` when the package is removed. A
server nobody chose for is left as it is (the package must never stop someone's server without
being asked), but the hook prints a warning naming every server left running or starting at boot,
and how to stop it. In every case it turns off the panel's own timers and services (backups,
restarts, update checks, SFTP), whose program is about to disappear, so nothing is left failing.
Their settings stay, so a panel installed again (after `apt remove`, not purge) turns the timers
of the servers that were left alone back on (after_reinstall()).
"""
import json
import os
import time

from core import manager
from core.platform import paths, services
from core import platform as _platform

CHOICES = ("stop", "keep", "delete")
RECENT_BACKUP = 24 * 3600
CONFIRM_WORD = "DELETE"
UNIT = "redstonepanel-uninstall"
PLAN_FILE = os.path.join(paths.DATA_DIR, "uninstall.json")
# The panel's own units in its unit folder that run the `redstonepanel` command
OWN_UNIT_PREFIX = "redstonepanel-"


class UninstallError(ValueError):
    """A choice that can't be carried out; the message is safe to show."""


def on_windows():
    return _platform.name() == "windows"


def keep_possible():
    """Whether a server can keep running once the panel is gone (Linux: plain systemd units)."""
    return not on_windows()


def remove_command(purge=False):
    if on_windows():
        return "Settings > Apps > Installed apps > RedstonePanel > Uninstall"
    return f"sudo apt {'purge' if purge else 'remove'} redstonepanel"


# ---------------------------------------------------------------------------
# What is on this machine
# ---------------------------------------------------------------------------
def _enabled(name):
    try:
        return services.unit_state(services.unit(name), "is-enabled", timeout=10) == "enabled"
    except Exception:
        return False


def _last_backup(name):
    from core import backups
    try:
        found = backups.list_backups(name)
    except Exception:
        return None
    return found[0]["created"] if found else None


def servers():
    """Every game server of this machine, with what the page shows about it."""
    out = []
    for s in manager.list_servers():
        name = s["name"]
        out.append({"name": name, "edition": s.get("edition"), "running": s.get("status") == "online",
                    "enabled": _enabled(name), "port": s.get("port"), "directory": s.get("directory"),
                    "last_backup": _last_backup(name)})
    return out


def overview():
    """Everything the Remove RedstonePanel page shows."""
    listed = servers()
    return {
        "servers": listed,
        "windows": on_windows(),
        "keep_possible": keep_possible(),
        "can_run": can_run(),
        "command": remove_command(),
        "purge_command": None if on_windows() else remove_command(purge=True),
        "backup_root": paths.BACKUP_ROOT,
        "recent_backup_hours": RECENT_BACKUP // 3600,
        "plan": load_plan(),
        "left_behind": [s["name"] for s in listed if s["running"] or s["enabled"]],
    }


# ---------------------------------------------------------------------------
# The owner's choices
# ---------------------------------------------------------------------------
def load_plan():
    try:
        with open(PLAN_FILE, encoding="utf-8") as f:
            plan = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(plan, dict) or not isinstance(plan.get("keep"), list):
        return None
    plan["keep"] = [n for n in plan["keep"] if isinstance(n, str)]
    return plan


def _save_plan(plan):
    os.makedirs(os.path.dirname(PLAN_FILE), exist_ok=True)
    tmp = PLAN_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(plan, f)
    os.replace(tmp, PLAN_FILE)


def clear_plan():
    try:
        os.remove(PLAN_FILE)
    except OSError:
        pass


def check(choices, confirm=None, now=None):
    """Raises UninstallError when the choices can't be carried out as they are."""
    now = now or time.time()
    known = {s["name"]: s for s in servers()}
    if not isinstance(choices, dict) or not choices:
        raise UninstallError("Choose what happens to each server.")
    for name, choice in choices.items():
        if name not in known:
            raise UninstallError(f"There is no server {name} on this machine.")
        if choice not in CHOICES:
            raise UninstallError(f"Choose Stop, Keep running or Delete for {name}.")
        if choice == "keep" and not keep_possible():
            raise UninstallError(f"{name} can't keep running on Windows once the panel is gone: its service "
                                 f"runs through the panel's own program. Choose Stop to keep its files.")
        if choice == "delete":
            last = known[name]["last_backup"]
            if not last or now - last > RECENT_BACKUP:
                raise UninstallError(f"Back up {name} first: deleting it needs a backup from the last "
                                     f"{RECENT_BACKUP // 3600} hours.")
    missing = [n for n in known if n not in choices]
    if missing:
        raise UninstallError("Choose what happens to " + ", ".join(missing) + " too.")
    if "delete" in choices.values() and str(confirm or "").strip() != CONFIRM_WORD:
        raise UninstallError(f"Type {CONFIRM_WORD} to delete servers.")


def apply(choices, actor="admin", confirm=None, delete_backups=False):
    """Carries out the choices now (stop and delete happen straight away; keep is remembered for
    the package). Returns {stopped, kept, deleted, command}."""
    check(choices, confirm)
    from core import backups
    done = {"stopped": [], "kept": [], "deleted": []}
    for name, choice in sorted(choices.items()):
        if choice == "stop":
            manager.remove_server(name, keep_world=True)
            done["stopped"].append(name)
        elif choice == "delete":
            manager.remove_server(name, keep_world=False)
            if delete_backups:
                from core.platform import system
                d = backups.server_backup_dir(name)
                if os.path.isdir(d):
                    system.delete_tree(d)
            done["deleted"].append(name)
        else:
            done["kept"].append(name)
    if done["kept"]:
        _save_plan({"keep": done["kept"], "by": actor, "at": int(time.time())})
    else:
        clear_plan()
    manager.write_log(f"[UNINSTALL] {actor} got this machine ready to remove RedstonePanel: "
                      + "; ".join(f"{what} {', '.join(names)}" for what, names in done.items() if names)
                      + (" (their backups deleted too)" if delete_backups and done["deleted"] else "") + ".")
    done["command"] = remove_command()
    return done


# ---------------------------------------------------------------------------
# Removing the package (Linux)
# ---------------------------------------------------------------------------
def can_run():
    """The page's Uninstall now button: Linux with systemd-run, the panel installed as a package."""
    from core.settings import PACKAGED
    return (not on_windows()) and PACKAGED and services.can_detach()


def run(purge=False, actor="admin"):
    """Removes the redstonepanel package in a unit of its own (the panel stops on the way)."""
    if not can_run():
        raise UninstallError("Remove the panel with: " + remove_command(purge))
    manager.write_log(f"[UNINSTALL] {actor} removed RedstonePanel from this machine"
                      + (" with its accounts and settings (purge)." if purge else "."))
    r = services.run_detached(UNIT, [paths.PANEL_CMD, "remove-package"] + (["--purge"] if purge else []),
                              description="Remove RedstonePanel", no_block=True)
    if r.returncode != 0:
        raise UninstallError(f"Couldn't start removing the panel: {(r.stderr or r.stdout).strip()[:300]}")
    return {"started": True, "command": remove_command(purge)}


def remove_package(purge=False, echo=print):
    """Removes (or purges) the redstonepanel package with the package manager, passing its
    output to echo. Returns its exit code."""
    from core.platform import packages
    verb = "purge" if purge else "remove"
    p = packages.start(packages.manager_command(["-y", "-q"], [verb, packages.PANEL_PACKAGE]))
    for line in p.stdout:
        echo(line.rstrip("\n"))
    return p.wait()


def _own_units():
    """The panel's timers and long-running services in its unit folder."""
    names = []
    for suffix in (".timer", ".service"):
        for f in services.unit_files(prefix=OWN_UNIT_PREFIX, suffix=suffix):
            if "@." in f:                     # templates aren't running on their own
                continue
            names.append(f)
    return sorted(names)


def turn_off_own_units(echo=print):
    """Stops and disables the panel's own timers and services (their program is going). Linux;
    the Windows uninstaller removes its scheduled tasks itself."""
    turned = []
    if on_windows():
        return turned
    for unit in _own_units():
        try:
            r = services.systemctl("disable", "--now", unit, timeout=60)
            if r.returncode == 0:
                turned.append(unit)
        except Exception as e:
            echo(f"  couldn't turn off {unit}: {e}")
    try:
        from core import sftp
        manager.close_firewall_rules(sftp.firewall_rules())
    except Exception:
        pass
    return turned


def package_hook(echo=print):
    """Run by the package's prerm on remove (and purge). Never stops a game server nobody chose for:
    it warns about it instead. Returns the names left running or starting at boot."""
    plan = load_plan() or {"keep": []}
    left = []
    for s in servers():
        name = s["name"]
        # Their timers run the `redstonepanel` command, which is going; their settings stay
        for mod in ("backups", "restarts", "updates"):
            try:
                __import__(f"core.{mod}", fromlist=[mod]).on_server_removed(name, purged=False)
            except Exception:
                pass
        if s["running"] or s["enabled"]:
            left.append(s)
    turn_off_own_units(echo)
    _save_plan({"keep": [s["name"] for s in servers()], "by": plan.get("by", "the package"),
                "at": int(time.time()), "removed": True})
    chosen = set(plan.get("keep") or [])
    if left:
        echo("")
        echo("RedstonePanel is being removed. These game servers stay on this machine with nothing to")
        echo("manage them: they keep starting at boot and holding their ports, with no backups,")
        echo("restarts or updates.")
        for s in left:
            state = ", ".join(w for w, on in (("running", s["running"]), ("starts at boot", s["enabled"])) if on)
            echo(f"  {s['name']:<24} port {s['port'] or '?':<6} {state}"
                 + ("  (you chose to keep it)" if s["name"] in chosen else ""))
        echo("Stop one for good with:   sudo systemctl disable --now minecraft-NAME")
        echo(f"Their folders stay where they are; backups stay in {paths.BACKUP_ROOT}.")
        echo("Next time, choose for each server first: Panel Settings > Remove RedstonePanel, or")
        echo("sudo redstonepanel uninstall")
        echo("")
    try:
        removed = manager.list_removed_servers()
    except Exception:
        removed = []
    if removed:
        echo(f"Stopped servers kept with their worlds: {', '.join(r['name'] for r in removed)}. Install the panel")
        echo("again and restore them from New server > Removed servers.")
    return [s["name"] for s in left]


def after_reinstall():
    """On panel start: servers that were left alone while the panel was away get their backup,
    restart and update timers back (their settings stayed)."""
    plan = load_plan()
    if not plan or not plan.get("removed"):
        return []
    present = {s["name"] for s in manager.list_servers()}
    back = []
    for name in plan["keep"]:
        if name not in present:
            continue
        for mod in ("backups", "restarts", "updates"):
            try:
                restore = getattr(__import__(f"core.{mod}", fromlist=[mod]), "on_server_restored", None)
                if restore:
                    restore(name)
            except Exception:
                pass
        back.append(name)
    clear_plan()
    if back:
        manager.write_log(f"[UNINSTALL] The panel is back: schedules of {', '.join(back)} are on again.")
    return back
