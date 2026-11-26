"""1.25: open a node's own panel from the master, signed in.

An admin on the master clicks **Open dashboard** on a node. The master asks the node, with its
node token, for a sign-in ticket for that admin; the node signs one (its own secret, valid for a
minute, usable once) and the master hands the browser a link to the node with it. The node turns
the ticket into an ordinary session cookie for "<admin>@<master name>" with the admin role.

The session lasts as a normal sign-in does, but only while this panel is still a node of the same
cluster under the same master (the cluster's epoch), and while the node's own setting
"Let the master's admins sign in here" (Panel Settings > Cluster, on by default) stays on. It
has no account on the node: it can't change a password, two-factor or passkeys here. Everything
it changes is logged with its name, as for a panel account.
"""
import secrets
import threading
import time

from core import auth, cluster
from core import settings as panel_settings

PURPOSE = "master sign-in"
TICKET_SECONDS = 60
SOURCE = "master"
# What a session from the master may reach under /api/auth/ (it has no account here)
AUTH_ALLOWED = ("/api/auth/me", "/api/auth/logout")

_lock = threading.Lock()
_used = {}                      # ticket nonce -> when it expires


class SignInError(ValueError):
    """A ticket that can't be used (message is safe to show)."""


def allowed():
    return panel_settings.load().get("master_sign_in") is not False


def _epoch(st):
    return int(st.get("epoch", 1))


def issue(master_user):
    """A ticket for the master's admin who asked (cluster.master_identity()), or SignInError."""
    st = cluster.state()
    if st.get("role") != "node":
        raise SignInError("This panel isn't a cluster node.")
    if master_user.get("source") != "cluster" or master_user.get("role") != "admin":
        raise SignInError("Only the master's admins can open this panel signed in.")
    if not allowed():
        raise SignInError("This node doesn't let the master's admins sign in. Its own admin can allow it under "
                          "Panel Settings > Cluster.")
    payload = {"a": master_user.get("actor") or "?", "m": master_user.get("master") or "?", "e": _epoch(st),
               "j": secrets.token_hex(16), "exp": int(time.time()) + TICKET_SECONDS}
    return auth._sign(payload, PURPOSE)


def redeem(ticket):
    """The session identity a ticket gives, once. SignInError when it's wrong, old or used."""
    payload = auth._unsign(ticket, PURPOSE)
    if not payload or not payload.get("j"):
        raise SignInError("This sign-in link is wrong or more than a minute old. Click Open dashboard on the "
                          "master again.")
    now = time.time()
    with _lock:
        for nonce in [n for n, exp in _used.items() if exp < now]:
            del _used[nonce]
        if payload["j"] in _used:
            raise SignInError("This sign-in link was used already. Click Open dashboard on the master again.")
        _used[payload["j"]] = payload["exp"]
    st = cluster.state()
    if st.get("role") != "node" or int(payload.get("e") or 0) != _epoch(st) or not allowed():
        raise SignInError("This node no longer lets the master's admins sign in.")
    return {"u": f"{payload['a']}@{payload['m']}", "src": SOURCE, "m": 1, "e": payload["e"]}


def session_user(payload):
    """The user a session cookie from the master gives (auth.read_session_token), or None once
    the node left the cluster, has a new master, or turned master sign-in off."""
    st = cluster.state()
    if st.get("role") != "node" or int(payload.get("e") or 0) != _epoch(st) or not allowed():
        return None
    username = str(payload.get("u") or "")
    if "@" not in username:
        return None
    return {"username": username, "source": SOURCE, "role": "admin", "master": st.get("master_name"),
            "two_factor": True, "passkey": False, "needs_2fa_setup": False}
