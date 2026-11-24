"""The panel's terminal (1.21.10): a shell on this machine, in the browser, for admins.

Who gets what:
  * Only panel admins, and only on a machine whose platform has a terminal (Linux; hidden on
    Windows for now). `user` accounts and trusted-LAN visitors never get one.
  * The shell always runs as a Linux account, never as the panel's root:
      - an admin who signed in to the panel WITH their Linux account gets a shell as that same
        account; with no password while the sign-in is recent (Panel Settings > Terminal), else
        after typing that account's password once;
      - a panel-only admin gets a login prompt and types a Linux account and its password.
    Accounts are never linked by matching names: a panel account called "quentin" is not the
    Linux account "quentin".
  * root may only be typed at the prompt when Panel Settings allows it; accounts whose shell is
    nologin/false (the `minecraft` account, system accounts) never.

Safety:
  * Off until an admin turns it on. Over plain HTTP only from this machine itself.
  * The browser first gets a one-use ticket (POST, so the usual cross-site checks apply), then
    opens the websocket with it; the websocket also checks the session cookie and its Origin.
  * Wrong passwords count per account (term:NAME) and per address (term-ip:ADDR) in the same
    store as sign-in failures: 5 in 10 minutes blocks for 10 minutes. 2 seconds after each.
  * Closed after N minutes with nothing typed, after 12 hours, when the admin signs out or
    stops being an admin, or when the terminal is turned off. Every open, close and failed
    login is in the panel log; what is typed is never recorded.
  * The password never reaches the shell or a log.

The cluster: a node that allows it ("Allow from the cluster master") accepts a terminal from
the master's token on its own websocket; it shows its own login prompt and checks its own
passwords. The master only copies bytes between the browser and the node.
"""
import asyncio
import hashlib
import ipaddress
import json
import os
import secrets
import threading
import time

from core import auth, manager, twofactor
from core import settings as panel_settings
from core.platform import terminal as platform_terminal

TICKET_SECONDS = 30
MAX_SECONDS = 12 * 3600                 # same as a panel sign-in
MAX_PER_ADMIN = 3
MAX_PER_HOST = 10
PROMPT_SECONDS = 60                     # to type a name or password
WRONG_DELAY = 2.0
TRIES = 3                               # wrong passwords before the window closes
WARN_SECONDS = 60                       # "closing in a minute" before the idle close
TICK = 5.0                              # how often the watchdog looks
RECHECK_SECONDS = 60                    # how often the sign-in is checked again
UID_CHECK_SECONDS = 15                  # the shell must show its account within this long
SEND_TIMEOUT = 60                       # a browser that stops reading is disconnected
MAX_INPUT = 64 * 1024
LINE_MAX = 256

_lock = threading.Lock()
_tickets = {}                           # ticket -> {"user", "session", "node", "exp", "ip"}
_recent = {}                            # sha256(session cookie) -> when its password was last typed here
_signed_out = {}                        # sha256(session cookie) -> when it signed out
SESSIONS = {}                           # id -> Session


class Refused(Exception):
    """Why a terminal can't be opened (safe to show)."""


# ---------------------------------------------------------------------------
# Who may, and what the button shows
# ---------------------------------------------------------------------------
def is_local(ip):
    try:
        return ipaddress.ip_address((ip or "").split("%")[0]).is_loopback
    except ValueError:
        return False


def linked_account(user):
    """The Linux account a panel user is, or None. Only a sign-in with a Linux account links."""
    return user.get("username") if user.get("source") == "linux" else None


def status(user, secure, ip):
    """What the Terminal button needs to know, for this signed-in user on this machine."""
    s = panel_settings.load()
    supported = platform_terminal.supported()
    admin = user.get("role") == "admin" and user.get("source") in ("panel", "linux")
    secure_ok = bool(secure) or is_local(ip)
    why = None
    if not supported:
        why = "This machine's system doesn't have the terminal yet."
    elif not admin:
        why = "Only admins can open a terminal."
    elif not s["terminal_enabled"]:
        why = "The terminal is off. Turn it on under Panel Settings > Terminal."
    elif not secure_ok:
        why = "The terminal needs HTTPS. Turn it on under Panel Settings > Connection."
    return {"supported": supported, "enabled": s["terminal_enabled"], "admin": admin, "secure": secure_ok,
            "available": why is None, "why": why, "linked": linked_account(user) if admin else None,
            "idle_minutes": s["terminal_idle_minutes"], "from_master": s["terminal_from_master"]}


def node_fields():
    """What a node tells the master about its terminal (cluster hello)."""
    s = panel_settings.load()
    return {"supported": platform_terminal.supported(), "enabled": s["terminal_enabled"],
            "from_master": s["terminal_from_master"]}


def valid_ip(text):
    try:
        ipaddress.ip_address(text)
        return True
    except ValueError:
        return False


def _session_hash(cookie):
    return hashlib.sha256((cookie or "").encode()).hexdigest()


def issue_ticket(user, cookie, secure, ip, node=None):
    """A one-use ticket for opening the websocket (POST /api/terminal/ticket). node: a cluster
    node's id, when the terminal is on that node (it decides itself whether it allows one)."""
    st = status(user, secure, ip)
    if node is None and not st["available"]:
        raise Refused(st["why"])
    if node is not None and not (st["admin"] and st["secure"]):
        raise Refused(st["why"] if not st["admin"] or not st["secure"] else "Not allowed.")
    if is_signed_out(cookie):
        raise Refused("Sign in again.")
    _check_room(user["username"])
    ticket = secrets.token_urlsafe(32)
    now = time.time()
    with _lock:
        for t in [t for t, e in _tickets.items() if e["exp"] < now]:
            del _tickets[t]
        _tickets[ticket] = {"user": user["username"], "source": user["source"], "session": _session_hash(cookie),
                            "node": node, "exp": now + TICKET_SECONDS, "ip": ip}
    return ticket


def take_ticket(ticket, cookie, node=None):
    """The ticket's entry, once, when it was issued to this same session (and for this node)."""
    with _lock:
        entry = _tickets.pop(ticket or "", None)
    if not entry or entry["exp"] < time.time() or entry["session"] != _session_hash(cookie) \
            or entry["node"] != node:
        return None
    return entry


def _check_room(panel_user):
    with _lock:
        mine = sum(1 for s in SESSIONS.values() if s.panel_user == panel_user)
        if mine >= MAX_PER_ADMIN:
            raise Refused(f"You already have {MAX_PER_ADMIN} terminals open. Close one first.")
        if len(SESSIONS) >= MAX_PER_HOST:
            raise Refused(f"This machine already has {MAX_PER_HOST} terminals open.")


def recent(cookie):
    """True when this panel session typed its Linux password (at sign-in or in a terminal) within
    the "skip the password" window."""
    window = recent_seconds()
    if window <= 0:
        return False
    last = max(auth.session_started(cookie) or 0, _recent.get(_session_hash(cookie), 0))
    return time.time() - last <= window


def _mark_recent(cookie):
    if cookie:
        with _lock:
            _recent[_session_hash(cookie)] = time.time()
            for k in [k for k, t in _recent.items() if time.time() - t > MAX_SECONDS]:
                del _recent[k]


def signed_out(cookie):
    """Sign-out: that session's terminals close, and its cookie opens no more (a session cookie
    is only signed, so a copy kept elsewhere would otherwise still work until it expires)."""
    if not cookie:
        return
    h = _session_hash(cookie)
    with _lock:
        _signed_out[h] = time.time()
        for k in [k for k, t in _signed_out.items() if time.time() - t > MAX_SECONDS]:
            del _signed_out[k]
        _recent.pop(h, None)
        sessions = [s for s in SESSIONS.values() if s.session_hash == h]
    for s in sessions:
        _call_soon(s, "you signed out")


def is_signed_out(cookie):
    return _session_hash(cookie) in _signed_out


def idle_seconds():
    """Panel Settings, or RP_TERMINAL_IDLE_SECONDS (test instances only)."""
    test = os.environ.get("RP_TERMINAL_IDLE_SECONDS")
    return int(test) if test and test.isdigit() else panel_settings.load()["terminal_idle_minutes"] * 60


def recent_seconds():
    """Panel Settings, or RP_TERMINAL_RECENT_SECONDS (test instances only)."""
    test = os.environ.get("RP_TERMINAL_RECENT_SECONDS")
    return int(test) if test and test.isdigit() else panel_settings.load()["terminal_recent_minutes"] * 60


def fail_keys(account, ip):
    return (f"term:{(account or '').lower()}", f"term-ip:{ip}")


# ---------------------------------------------------------------------------
# Open sessions (Panel Settings > Terminal lists them, with End)
# ---------------------------------------------------------------------------
class Session:
    def __init__(self, panel_user, ip, via=None, cookie=None):
        self.id = secrets.token_hex(8)
        self.session_hash = _session_hash(cookie) if cookie else None
        self.panel_user, self.ip, self.via = panel_user, ip, via
        self.account = None
        self.started = self.last_input = time.time()
        self.end_reason = None
        self.ended = asyncio.Event()

    def info(self):
        return {"id": self.id, "panel_user": self.panel_user, "account": self.account, "ip": self.ip,
                "via": self.via, "started": int(self.started), "idle": int(time.time() - self.last_input)}

    def end(self, reason):
        if not self.end_reason:
            self.end_reason = reason
        self.ended.set()


def list_sessions():
    with _lock:
        return [s.info() for s in SESSIONS.values()]


def end_session(sid, actor):
    with _lock:
        s = SESSIONS.get(sid)
    if not s:
        return False
    manager.write_log(f"[TERMINAL] {actor} ended {s.panel_user}'s terminal (as {s.account or 'not signed in'}).")
    _call_soon(s, f"ended by {actor}")
    return True


def end_all(reason):
    """The terminal was turned off: every open one closes."""
    with _lock:
        sessions = list(SESSIONS.values())
    for s in sessions:
        _call_soon(s, reason)


def _call_soon(session, reason):
    loop = getattr(session, "loop", None)
    if loop and loop.is_running():
        loop.call_soon_threadsafe(session.end, reason)
    else:
        session.end(reason)


def turn(enabled, actor, **changes):
    """Panel Settings > Terminal. Returns the saved settings."""
    old = panel_settings.load()
    new = panel_settings.validate(dict(old, terminal_enabled=bool(enabled), **changes))
    panel_settings.save(new)
    if old["terminal_enabled"] != new["terminal_enabled"]:
        manager.write_log(f"[TERMINAL] {actor} turned the terminal {'on' if new['terminal_enabled'] else 'off'}.")
        if not new["terminal_enabled"]:
            end_all(f"the terminal was turned off by {actor}")
    elif any(old[k] != new[k] for k in panel_settings.TERMINAL_KEYS):
        manager.write_log(f"[TERMINAL] {actor} changed the terminal settings.")
    return new


# ---------------------------------------------------------------------------
# One terminal: the login prompt, then bytes both ways
# ---------------------------------------------------------------------------
class Closed(Exception):
    pass


class _Input:
    """What the browser sends: binary frames are keystrokes, text frames are JSON controls."""

    def __init__(self, conn, session):
        self.conn, self.session = conn, session
        self.size = (24, 80)
        self.buffer = bytearray()

    async def next(self, timeout=None):
        """The next keystrokes (bytes). Resizes are applied on the way."""
        if self.buffer:                          # typed ahead of the last prompt
            data, self.buffer = bytes(self.buffer), bytearray()
            return data
        while True:
            kind, value = await (asyncio.wait_for(self.conn.recv(), timeout) if timeout else self.conn.recv())
            if kind == "closed":
                raise Closed()
            if kind == "text":
                try:
                    msg = json.loads(value)
                except ValueError:
                    continue
                if isinstance(msg, dict) and msg.get("type") == "resize":
                    try:
                        self.size = (max(2, min(int(msg["rows"]), 500)), max(10, min(int(msg["cols"]), 1000)))
                    except (KeyError, TypeError, ValueError):
                        continue
                    if getattr(self.session, "shell", None):
                        self.session.shell.resize(*self.size)
                continue
            if len(value) > MAX_INPUT:
                raise Closed()
            self.session.last_input = time.time()
            return value

    async def line(self, conn, echo):
        """A line typed at a prompt, edited here (the shell isn't running yet)."""
        out = bytearray()
        deadline = time.monotonic() + PROMPT_SECONDS
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                await conn.send_bytes(b"\r\nTimed out.\r\n")
                raise Closed()
            try:
                data = await self.next(timeout=left)
            except asyncio.TimeoutError:
                continue
            for i, b in enumerate(data):
                if b in (13, 10):
                    await conn.send_bytes(b"\r\n")
                    rest = data[i + 1:]
                    if rest[:1] == b"\n" and b == 13:
                        rest = rest[1:]
                    self.buffer[:0] = rest
                    return out.decode("utf-8", "replace")
                if b in (3, 4):                                   # Ctrl-C, Ctrl-D
                    await conn.send_bytes(b"^C\r\n")
                    raise Closed()
                if b in (8, 127):
                    if out:
                        out.pop()
                        if echo:
                            await conn.send_bytes(b"\b \b")
                    continue
                if b < 32 or len(out) >= LINE_MAX:
                    continue
                out.append(b)
                if echo:
                    await conn.send_bytes(bytes([b]))


async def _blocking(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


ROOT_REFUSED = "root sign-in is off"


def refusal(account, s=None):
    """Why this account may not have a terminal here (None: it may, with its password). Any
    account with uid 0 counts as root."""
    s = s or panel_settings.load()
    if not account or account.startswith("-") or any(c.isspace() or ord(c) < 32 for c in account):
        return "no such account, or it can't log in"
    if not platform_terminal.login_shell(account):
        return "no such account, or it can't log in"
    if platform_terminal.account_uid(account) == 0 and not s["terminal_allow_root"]:
        return ROOT_REFUSED
    return None


async def _sign_in(conn, inp, who, session):
    """Shows the prompts and checks them. Returns the Linux account to start, or raises Closed."""
    s = panel_settings.load()
    linked = who.get("linked")
    ip = who["ip"]
    if linked:
        why = await _blocking(refusal, linked, s)
        if why:
            manager.write_log(f"[TERMINAL] Refused {session.panel_user}'s own account {linked!r}: {why}.")
            await conn.send_bytes((b"root can't sign in here (Panel Settings > Terminal). Use sudo.\r\n"
                                   if why == ROOT_REFUSED else b"Your Linux account can't log in here.\r\n"))
            raise Closed()
    if linked and who.get("recent"):
        await conn.send_bytes(f"Signed in as {linked} (your Linux account).\r\n".encode())
        return linked
    tries = 0
    while tries < TRIES:
        tries += 1
        if linked:
            account = linked
            await conn.send_bytes(f"Password for {linked}: ".encode())
        else:
            await conn.send_bytes(b"login: ")
            account = (await inp.line(conn, echo=True)).strip()
            if not account:
                tries -= 1
                continue
            await conn.send_bytes(b"Password: ")
        password = await inp.line(conn, echo=False)
        keys = fail_keys(account, ip)
        if await _blocking(auth.throttled, keys):
            manager.write_log(f"[TERMINAL] Refused {account} for {session.panel_user} from {ip}: too many "
                              "wrong passwords.")
            await conn.send_bytes(b"Too many wrong passwords. Try again in a few minutes.\r\n")
            raise Closed()
        refused = await _blocking(refusal, account, s)
        ok = not refused and await _blocking(platform_terminal.check_password, account, password)
        password = None
        if ok and who.get("two_factor_key"):
            await conn.send_bytes(b"Authenticator code: ")
            code = (await inp.line(conn, echo=False)).strip()
            if not await _blocking(twofactor.verify, who["two_factor_key"], code):
                await _blocking(auth.record_fail, keys)
                manager.write_log(f"[TERMINAL] Wrong two-factor code from {session.panel_user} at {ip}.")
                await conn.send_bytes(b"That code is wrong or was already used.\r\n")
                raise Closed()
        if ok:
            await _blocking(auth.clear_fails, keys)
            return account
        await _blocking(auth.record_fail, keys)
        manager.write_log(f"[TERMINAL] Login failed for {account!r} ({refused or 'wrong password'}), panel user "
                          f"{session.panel_user}, from {ip}.")
        await asyncio.sleep(WRONG_DELAY)
        if refused == ROOT_REFUSED:
            await conn.send_bytes(b"root can't sign in here (Panel Settings > Terminal). Use sudo.\r\n")
        else:
            await conn.send_bytes(b"Login incorrect\r\n")
    raise Closed()


async def serve(conn, who):
    """Runs one terminal on this machine until it closes.
    conn: send_bytes(b), send_text(str), recv() -> ("bytes"|"text"|"closed", value), close(code).
    who: {"panel_user", "ip", "linked", "recent", "two_factor_key", "cookie", "via",
          "still_allowed": callable -> reason or None (checked every minute)}"""
    loop = asyncio.get_running_loop()
    session = Session(who["panel_user"], who["ip"], who.get("via"), who.get("cookie"))
    session.loop = loop
    session.shell = None
    try:
        _check_room(session.panel_user)
    except Refused as e:
        await conn.send_bytes(f"{e}\r\n".encode())
        await conn.close(4003)
        return
    with _lock:
        SESSIONS[session.id] = session
    inp = _Input(conn, session)
    reason = None
    try:
        host = panel_settings.display_name()
        await conn.send_bytes(f"RedstonePanel terminal on {host}\r\n".encode())
        try:
            account = await _sign_in(conn, inp, who, session)
        except asyncio.TimeoutError:
            raise Closed()
        if who.get("cookie") and not (who.get("linked") and who.get("recent")):
            _mark_recent(who["cookie"])
        session.account = account
        rows, cols = inp.size
        try:
            shell = await _blocking(platform_terminal.spawn, account, rows, cols, who["ip"])
        except (OSError, PermissionError) as e:
            manager.write_log(f"[TERMINAL] Couldn't start a shell as {account} for {session.panel_user}: {e}")
            await conn.send_bytes(b"The shell couldn't be started.\r\n")
            raise Closed()
        session.shell = shell
        await conn.send_text(json.dumps({"type": "started", "account": account, "id": session.id}))
        manager.write_log(f"[TERMINAL] {session.panel_user} opened a shell as {account} from {session.ip}"
                          + (f" ({session.via})" if session.via else "") + ".")
        reason = await _run(conn, inp, session, shell, who, await _blocking(platform_terminal.account_uid, account))
    except Closed:
        reason = reason or session.end_reason or "closed"
    except Exception as e:                       # noqa: BLE001  (never leave a shell behind)
        reason = f"error: {e}"
    finally:
        with _lock:
            SESSIONS.pop(session.id, None)
        if session.shell:
            await _blocking(session.shell.close)
            minutes = max(0, int((time.time() - session.started) // 60))
            manager.write_log(f"[TERMINAL] {session.panel_user}'s shell as {session.account} closed after "
                              f"{minutes} min ({reason or session.end_reason or 'closed'}).")
        try:
            await conn.close(1000)
        except Exception:                        # noqa: BLE001
            pass


async def _run(conn, inp, session, shell, who, expected_uid):
    loop = asyncio.get_running_loop()
    ready = asyncio.Event()
    if shell.fd >= 0:
        loop.add_reader(shell.fd, ready.set)

    async def output():
        while True:
            data = shell.read()
            if data is None:
                if shell.fd >= 0:
                    ready.clear()
                    data = shell.read()          # nothing lost between read and clear
                    if data is None:
                        await ready.wait()
                        continue
                else:
                    await asyncio.sleep(0.02)
                    continue
            if data == b"":
                return "the shell ended"
            await asyncio.wait_for(conn.send_bytes(data), SEND_TIMEOUT)

    async def keys():
        while True:
            data = await inp.next()
            await _blocking(shell.write, data)

    async def watchdog():
        idle = idle_seconds()
        warned = False
        checked_uid = False
        last_check = time.monotonic()
        while True:
            await asyncio.sleep(TICK)
            now = time.time()
            if not checked_uid:
                uid = await _blocking(shell.foreground_uid)
                if uid is not None:
                    if uid != expected_uid:
                        manager.write_log(f"[TERMINAL] Closed {session.panel_user}'s shell: it ran as uid {uid}, "
                                          f"not {session.account} ({expected_uid}).")
                        return "it didn't run as the right account"
                    checked_uid = True
                elif now - session.started > UID_CHECK_SECONDS + 60:
                    return "the shell didn't start"
            quiet = now - session.last_input
            if quiet >= idle:
                await conn.send_bytes(b"\r\n[Closed after no typing for a while.]\r\n")
                return "idle"
            if quiet >= idle - WARN_SECONDS and not warned:
                warned = True
                await conn.send_text(json.dumps({"type": "warning", "text": "Closing in a minute: nothing typed."}))
            elif quiet < idle - WARN_SECONDS:
                warned = False
            if now - session.started >= MAX_SECONDS:
                await conn.send_bytes(b"\r\n[Closed: open for 12 hours.]\r\n")
                return "12 hours"
            if time.monotonic() - last_check >= RECHECK_SECONDS:
                last_check = time.monotonic()
                why = await _blocking(who["still_allowed"]) if who.get("still_allowed") else None
                if why:
                    await conn.send_bytes(f"\r\n[Closed: {why}]\r\n".encode())
                    return why

    tasks = [asyncio.ensure_future(t) for t in (output(), keys(), watchdog(), session.ended.wait())]
    try:
        done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        task = done.pop()
        if task is tasks[3]:
            reason = session.end_reason or "ended"
            try:
                await conn.send_bytes(f"\r\n[Closed: {reason}]\r\n".encode())
            except Exception:                    # noqa: BLE001
                pass
            return reason
        exc = task.exception()
        if isinstance(exc, (Closed, asyncio.TimeoutError)):
            return "the browser went away" if isinstance(exc, asyncio.TimeoutError) else "the window was closed"
        if exc:
            raise exc
        return task.result()
    finally:
        for t in tasks:
            t.cancel()
        if shell.fd >= 0:
            try:
                loop.remove_reader(shell.fd)
            except (ValueError, OSError):
                pass


# ---------------------------------------------------------------------------
# The cluster: the master copies bytes between the browser and a node's terminal
# ---------------------------------------------------------------------------
async def relay(conn, node_conn, still_allowed=None):
    """Copies frames both ways until either side closes, or the admin signs out (still_allowed()
    returns a reason, checked every minute). node_conn has the same shape as conn."""
    async def pipe(a, b):
        while True:
            kind, value = await a.recv()
            if kind == "closed":
                return
            if kind == "bytes":
                await b.send_bytes(value)
            else:
                await b.send_text(value)

    async def watch():
        while True:
            await asyncio.sleep(RECHECK_SECONDS)
            why = await _blocking(still_allowed) if still_allowed else None
            if why:
                await conn.send_bytes(f"\r\n[Closed: {why}]\r\n".encode())
                return

    tasks = [asyncio.ensure_future(pipe(conn, node_conn)), asyncio.ensure_future(pipe(node_conn, conn)),
             asyncio.ensure_future(watch())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        for c in (conn, node_conn):
            try:
                await c.close(1000)
            except Exception:                    # noqa: BLE001
                pass
