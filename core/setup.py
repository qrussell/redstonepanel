"""The first-run setup guide (1.16.1): four steps to a safe setup, shown on the dashboard to admins.

Every step is ticked from the panel's real state, never from clicks in the guide, so a step done
from the command line or by another admin counts, and a panel upgraded from an older release only
shows what's still missing. The guide stores just two answers of its own in data/setup.json:
  * dismissed: an admin closed the guide (for every admin; Panel Settings reopens it)
  * http_ok:   an admin chose to keep plain HTTP on a network they trust
"""
import json
import os
import threading

from core import backups, cluster, host, notify
from core import settings as panel_settings

STATE_FILE = os.path.join(panel_settings.DATA_DIR, "setup.json")
DEFAULT_STATE = {"dismissed": False, "http_ok": False}
_lock = threading.Lock()


def load_state():
    state = dict(DEFAULT_STATE)
    try:
        with open(STATE_FILE) as f:
            stored = json.load(f)
        if isinstance(stored, dict):
            state.update({k: stored[k] for k in DEFAULT_STATE if isinstance(stored.get(k), bool)})
    except (OSError, ValueError):
        pass
    return state


def save_state(**changes):
    """Changes only the given answers (dismissed, http_ok); returns the new state."""
    with _lock:
        state = load_state()
        state.update({k: bool(v) for k, v in changes.items() if k in DEFAULT_STATE and v is not None})
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE_FILE)
        return state


def status():
    """Each step and whether it's done. Uses the dashboard's cached report, so it's cheap to poll."""
    state = load_state()
    rep = host.report()
    names = [s["name"] for s in rep.get("servers", [])]
    role = cluster.role()
    fw = host.firewall()
    https_mode = panel_settings.load()["https_mode"]

    unscheduled = [n for n in names
                   if backups.NAME_RE.fullmatch(n) and not backups.get_schedule(n)["enabled"]]

    steps = [
        {"id": "server", "done": bool(names), "servers": len(names),
         # The panel opens game ports only through ufw, so say when it can't
         "firewall": "missing" if not fw["installed"] else "off" if not fw["active"] else "on"},
        {"id": "backups", "done": bool(names) and not unscheduled, "unscheduled": unscheduled},
        # A cluster node's HTTPS comes with joining the cluster
        {"id": "connection", "done": https_mode != "off" or state["http_ok"] or role == "node",
         "https_mode": https_mode, "http_ok": state["http_ok"], "cluster_role": role},
        {"id": "notifications", "done": notify.public()["configured"]},
    ]
    done = sum(1 for s in steps if s["done"])
    return {"steps": steps, "done": done, "total": len(steps), "complete": done == len(steps),
            "dismissed": state["dismissed"]}
