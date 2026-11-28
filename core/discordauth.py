"""Sign in with Discord (1.28), for the panel and for join pages.

The panel is a confidential OAuth2 client of the owner's own Discord application (the same one
that holds the 1.27 /start bot, or a new one). The owner pastes its client secret under Panel
Settings -> Discord -> Sign in with Discord, and copies the redirect addresses shown there into
the Developer Portal (OAuth2 -> Redirects).

The flow (authorization code, RFC 6749 section 4.1):

    begin()   a one-use `state` (10 minutes), kept here and in a short cookie of the browser, and
              the address of Discord's consent page, scopes `identify guilds.members.read`
    finish()  the browser comes back with ?code&state: the state must match the cookie and be
              one we gave out; the code is swapped for a token (with the client secret), the
              Discord user (id, name, two-factor) and their membership of the community's
              Discord server (roles) are read, and the token is revoked at once. Nothing from
              Discord is kept but the user's id, name and roles.

What it gives:

- Panel sign-in. A Discord account LINKED to a panel or Windows/Linux account (My account ->
  Link Discord) signs in as that account, with its own role, two-factor and passkeys. Any other
  Discord account signs in only if a row of the role table gives it permissions (1.18 grants) on
  some server; such an account is never an admin (Quentin, 27 Nov 2026: the panel runs as
  root/SYSTEM, so admin comes only from linking an admin's own account). Its session is
  re-checked: through the 1.27 bot every RECHECK seconds when the bot is in the community's
  server, else at the end of the 12-hour session.
- Join pages (core/joinsite.py): visitors sign in with Discord there too, with a cookie that is
  never a panel session; pages can ask for membership of the Discord server and give See, Start
  and Stop by role (core/joinpage.py).

Settings and what it knows about Discord accounts live in DATA_DIR/discord-signin.json (0600).
The client secret is never sent back to the browser (its last four characters are shown).
Everything is the standard library (urllib), so Linux and Windows are the same code.
"""
import base64
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from core import grants as grants_mod
from core.settings import DATA_DIR

SIGNIN_FILE = os.path.join(DATA_DIR, "discord-signin.json")
API = "https://discord.com/api/v10"
AUTHORIZE = "https://discord.com/oauth2/authorize"
SCOPES = "identify guilds.members.read"
STATE_SECONDS = 600
STATES_MAX = 500
RECHECK = 600                       # seconds between re-reading a signed-in member's roles (through the bot)
PANEL_CALLBACK = "/api/auth/discord/callback"
JOIN_CALLBACK = "/join/_discord/callback"
ANY_MEMBER = "*"                    # a role-table row for everyone in the Discord server
SECRET_MIN, SECRET_MAX = 16, 128
ACCOUNTS_MAX = 2000

_lock = threading.RLock()
_states = {}                        # state -> {"purpose", "base", "exp", "account", "next", "verifier"}
_member_cache = {}                  # discord id -> (time, roles or None when not a member)


class DiscordAuthError(ValueError):
    """Something to show the person, as it is."""


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def _defaults():
    return {"enabled": False, "client_id": "", "secret": "", "guild": "", "panel_url": "", "join_url": "",
            "require_mfa": False, "roles": [], "blocked": [], "links": {}, "accounts": {},
            "updated": None, "updated_by": None}


def load():
    out = _defaults()
    try:
        with open(SIGNIN_FILE) as f:
            data = json.load(f)
        if isinstance(data, dict):
            out.update({k: data[k] for k in out if k in data})
    except (OSError, ValueError):
        pass
    out["enabled"], out["require_mfa"] = bool(out["enabled"]), bool(out["require_mfa"])
    for k in ("client_id", "secret", "guild", "panel_url", "join_url"):
        out[k] = out[k] if isinstance(out[k], str) else ""
    out["roles"] = [r for r in out["roles"] if isinstance(r, dict)] if isinstance(out["roles"], list) else []
    out["blocked"] = [str(x) for x in out["blocked"]] if isinstance(out["blocked"], list) else []
    for k in ("links", "accounts"):
        out[k] = out[k] if isinstance(out[k], dict) else {}
    return out


def _save(cfg):
    os.makedirs(os.path.dirname(SIGNIN_FILE), exist_ok=True)
    tmp = SIGNIN_FILE + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(cfg, f, indent=1)
    os.replace(tmp, SIGNIN_FILE)


def _id(value, what):
    from core.discordbot import ID_RE
    v = str(value or "").strip()
    if v and not ID_RE.match(v):
        raise DiscordAuthError(f"That isn't a Discord {what} ID (a long number: turn on Developer Mode in Discord, then right-click and Copy ID).")
    return v


def clean_base(value, what):
    """A web address with only scheme, host and port, like https://panel.example.com:8443, or ''."""
    url = str(value or "").strip().rstrip("/")
    if not url:
        return ""
    if len(url) > 200 or any(c.isspace() or ord(c) < 32 or c in "\"'<>`\\" for c in url):
        raise DiscordAuthError(f"The {what} isn't usable.")
    if "://" not in url:
        url = "https://" + url
    u = urllib.parse.urlsplit(url)
    try:
        u.port
    except ValueError:
        raise DiscordAuthError(f"The port in the {what} must be a number between 1 and 65535.")
    if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password or u.query or u.fragment \
            or u.path not in ("", "/"):
        raise DiscordAuthError(f"Enter the {what} like https://panel.example.com:8443, without a path.")
    return f"{u.scheme}://{u.netloc.lower()}"


def _clean_rows(rows):
    if not isinstance(rows, (list, tuple)):
        raise DiscordAuthError("Send the role table as a list.")
    out = []
    for r in rows[:50]:
        if not isinstance(r, dict):
            raise DiscordAuthError("Each role row needs a role, servers and permissions.")
        role = str(r.get("role") or "").strip()
        if role != ANY_MEMBER:
            role = _id(role, "role")
        if not role:
            raise DiscordAuthError("Choose the Discord role for each row.")
        servers = r.get("servers") or []
        if not isinstance(servers, (list, tuple)) or not servers or \
                not all(isinstance(k, str) and grants_mod.KEY_RE.match(k) for k in servers):
            raise DiscordAuthError("Choose the servers each role row covers.")
        perms = r.get("perms") or []
        if not isinstance(perms, (list, tuple)) or any(p not in grants_mod.PERMS for p in perms):
            raise DiscordAuthError("Unknown permission in the role table.")
        perms = grants_mod._clean(perms)
        if not perms:
            raise DiscordAuthError("Tick at least one permission for each role row.")
        servers = [grants_mod.ALL_SERVERS] if grants_mod.ALL_SERVERS in servers else list(dict.fromkeys(servers))
        out.append({"role": role, "servers": servers, "perms": perms, "name": str(r.get("name") or "")[:100]})
    return out


def save_settings(new, actor="?"):
    """Admin's change from Panel Settings -> Discord. Returns public()."""
    with _lock:
        cfg = load()
        if new.get("forget_secret"):
            cfg["secret"], cfg["enabled"] = "", False
        secret = str(new.get("secret") or "").strip()
        if secret and not new.get("forget_secret"):
            if len(secret) == 64 and all(c in "0123456789abcdefABCDEF" for c in secret):
                raise DiscordAuthError("That's the application's Public Key (General Information). The client secret is "
                                       "under OAuth2, Client Secret (Reset Secret), about 32 characters.")
            if not SECRET_MIN <= len(secret) <= SECRET_MAX or not all(c.isalnum() or c in "-_" for c in secret):
                raise DiscordAuthError("That doesn't look like a client secret. Copy it from the Developer Portal: "
                                       "your application, OAuth2, Client Secret (Reset Secret).")
            cfg["secret"] = secret
        if "client_id" in new:
            cfg["client_id"] = _id(new["client_id"], "application")
        if "guild" in new:
            cfg["guild"] = _id(new["guild"], "server")
        if "panel_url" in new:
            cfg["panel_url"] = clean_base(new["panel_url"], "panel address")
        if "join_url" in new:
            cfg["join_url"] = clean_base(new["join_url"], "join pages address")
        if "require_mfa" in new:
            cfg["require_mfa"] = bool(new["require_mfa"])
        if "roles" in new:
            cfg["roles"] = _clean_rows(new["roles"])
        if "enabled" in new and not new.get("forget_secret"):
            cfg["enabled"] = bool(new["enabled"])
        if cfg["enabled"]:
            if not client_id(cfg) or not cfg["secret"]:
                raise DiscordAuthError("Enter the application's client ID and client secret first.")
            if not cfg["panel_url"]:
                raise DiscordAuthError("Enter the address you open the panel at, so Discord can send people back.")
        if cfg["roles"] and not cfg["guild"]:
            raise DiscordAuthError("Choose the Discord server whose roles count.")
        cfg.update(updated=int(time.time()), updated_by=actor)
        _save(cfg)
    _member_cache.clear()
    _log(f"[DISCORD] {actor} " + ("turned Sign in with Discord on" if cfg["enabled"] else "turned Sign in with Discord off")
         + (f" ({len(cfg['roles'])} role row{'' if len(cfg['roles']) == 1 else 's'})" if cfg["enabled"] else "")
         + (" and removed the client secret" if new.get("forget_secret") else "") + ".")
    return public()


def adopt_panel_url(new_url, old_url, actor="the panel"):
    """1.29: the panel's public name from Cloudflare Tunnel becomes Discord's panel address when
    none was typed, or when the one there was the tunnel's own earlier name. Returns True if changed."""
    with _lock:
        cfg = load()
        if cfg["panel_url"] and cfg["panel_url"] != old_url:
            return False
        new_url = clean_base(new_url, "panel address") if new_url else ""
        if cfg["panel_url"] == new_url:
            return False
        if not new_url and cfg["enabled"]:
            return False                       # keep the old name rather than turn Discord sign-in off
        cfg["panel_url"] = new_url
        _save(cfg)
    _member_cache.clear()
    _log(f"[DISCORD] The panel address for Sign in with Discord is now {new_url or 'not set'} (Cloudflare Tunnel, "
         f"{actor}). Add {new_url + PANEL_CALLBACK if new_url else 'the new address'} to the redirects in Discord's "
         "Developer Portal.")
    return True


def client_id(cfg=None):
    """The application's client ID: typed in, or the 1.27 bot's own application."""
    cfg = cfg or load()
    if cfg["client_id"]:
        return cfg["client_id"]
    try:
        from core import discordbot
        return discordbot.public()["status"]["app_id"] or ""
    except Exception:
        return ""


def configured(cfg=None):
    """Discord sign-in can be offered somewhere (the app's id and secret are set)."""
    cfg = cfg or load()
    return bool(client_id(cfg) and cfg["secret"])


def panel_available(cfg=None):
    cfg = cfg or load()
    return cfg["enabled"] and configured(cfg) and bool(cfg["panel_url"])


def join_base(cfg=None):
    """Where visitors open the join pages, for Discord's redirect: set here, else the join pages
    address of Panel Settings (1.21.9). '' when neither is set."""
    cfg = cfg or load()
    if cfg["join_url"]:
        return cfg["join_url"]
    from core import settings as panel_settings
    return panel_settings.load().get("join_url") or ""


def public(cfg=None):
    """What the settings page shows. Never the secret."""
    cfg = cfg or load()
    accounts = []
    for did, a in cfg["accounts"].items():
        accounts.append({"id": did, "name": a.get("name") or did, "last": a.get("last"),
                         "roles": a.get("roles") or [], "blocked": did in cfg["blocked"],
                         "linked": cfg["links"].get(did)})
    accounts.sort(key=lambda a: -(a["last"] or 0))
    return {"enabled": cfg["enabled"], "client_id": cfg["client_id"], "client_id_effective": client_id(cfg),
            "has_secret": bool(cfg["secret"]), "secret_hint": cfg["secret"][-4:] if cfg["secret"] else "",
            "guild": cfg["guild"], "panel_url": cfg["panel_url"], "join_url": cfg["join_url"],
            "join_base": join_base(cfg), "require_mfa": cfg["require_mfa"], "roles": cfg["roles"],
            "redirects": [u for u in (cfg["panel_url"] and cfg["panel_url"] + PANEL_CALLBACK,
                                      join_base(cfg) and join_base(cfg) + JOIN_CALLBACK) if u],
            "bot_checks": bool(_bot_for(cfg)), "accounts": accounts,
            "updated": cfg["updated"], "updated_by": cfg["updated_by"]}


def _log(msg):
    try:
        from core import manager
        manager.write_log(msg)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Discord accounts this panel knows: who signed in, blocked ones, links to panel accounts
# ---------------------------------------------------------------------------
def set_blocked(did, blocked, actor="?"):
    did = _id(did, "user")
    with _lock:
        cfg = load()
        if blocked and did not in cfg["blocked"]:
            cfg["blocked"].append(did)
        elif not blocked:
            cfg["blocked"] = [x for x in cfg["blocked"] if x != did]
        acc = cfg["accounts"].get(did)
        if acc:
            acc["v"] = int(acc.get("v") or 1) + 1          # signs out its sessions and join page visits
        _save(cfg)
        name = (acc or {}).get("name") or did
    _log(f"[AUTH] {actor} {'blocked' if blocked else 'unblocked'} the Discord account {name} ({did}).")
    from core import discordwl                      # 1.28: a blocked account's whitelist entries go
    if discordwl.load()["enabled"]:
        discordwl._after_change()


def forget_account(did, actor="?"):
    did = _id(did, "user")
    with _lock:
        cfg = load()
        acc = cfg["accounts"].pop(did, None)
        _save(cfg)
    _log(f"[AUTH] {actor} removed the Discord account {(acc or {}).get('name') or did} ({did}) from the list.")


def linked_account(did, cfg=None):
    cfg = cfg or load()
    return cfg["links"].get(str(did))


def link_of(account_key, cfg=None):
    """(discord id, name) linked to a panel or Windows/Linux account, or None."""
    cfg = cfg or load()
    for did, key in cfg["links"].items():
        if key == account_key:
            return did, (cfg["accounts"].get(did) or {}).get("name") or did
    return None


def link(account_key, who, actor):
    did = who["id"]
    with _lock:
        cfg = load()
        other = cfg["links"].get(did)
        if other and other != account_key:
            raise DiscordAuthError("That Discord account is already linked to another account on this panel.")
        cfg["links"] = {d: k for d, k in cfg["links"].items() if k != account_key}
        cfg["links"][did] = account_key
        _remember(cfg, who)
        _save(cfg)
    _log(f"[AUTH] {actor} linked the Discord account {who['name']} ({did}).")


def unlink(account_key, actor):
    with _lock:
        cfg = load()
        gone = [d for d, k in cfg["links"].items() if k == account_key]
        cfg["links"] = {d: k for d, k in cfg["links"].items() if k != account_key}
        _save(cfg)
    if gone:
        _log(f"[AUTH] {actor} unlinked their Discord account.")
    return bool(gone)


def forget_panel_account(account_key):
    """A deleted panel account takes its Discord link with it."""
    with _lock:
        cfg = load()
        if account_key in cfg["links"].values():
            cfg["links"] = {d: k for d, k in cfg["links"].items() if k != account_key}
            _save(cfg)


def _remember(cfg, who):
    acc = cfg["accounts"].setdefault(who["id"], {"v": 1})
    acc.update(name=who["name"], last=int(time.time()), roles=list(who.get("roles") or []))
    if len(cfg["accounts"]) > ACCOUNTS_MAX:
        oldest = sorted(cfg["accounts"], key=lambda d: cfg["accounts"][d].get("last") or 0)
        for d in oldest[:len(cfg["accounts"]) - ACCOUNTS_MAX]:
            if d not in cfg["links"] and d not in cfg["blocked"]:
                cfg["accounts"].pop(d, None)
    return acc


def remember(who):
    """Notes a Discord account that signed in. Returns its session version."""
    with _lock:
        cfg = load()
        acc = _remember(cfg, who)
        _save(cfg)
        return int(acc.get("v") or 1)


def version(did, cfg=None):
    cfg = cfg or load()
    return int((cfg["accounts"].get(str(did)) or {}).get("v") or 1)


def blocked(did, cfg=None):
    cfg = cfg or load()
    return str(did) in cfg["blocked"]


# ---------------------------------------------------------------------------
# The role table: Discord roles -> permissions on servers (1.18 grants)
# ---------------------------------------------------------------------------
def grants_for(roles, member=True, cfg=None):
    """{server key: [perms]} that these roles give on this panel (rows add up). Nothing for
    someone who isn't in the Discord server."""
    cfg = cfg or load()
    if not member:
        return {}
    have = set(roles or ())
    out = {}
    for row in cfg["roles"]:
        if row["role"] == ANY_MEMBER or row["role"] in have:
            for key in row["servers"]:
                out[key] = grants_mod._clean(set(out.get(key, ())) | set(row["perms"]))
    return out


def forget_server(key):
    with _lock:
        cfg = load()
        changed = False
        for row in cfg["roles"]:
            if key in row["servers"]:
                row["servers"] = [k for k in row["servers"] if k != key]
                changed = True
        if changed:
            cfg["roles"] = [r for r in cfg["roles"] if r["servers"]]
            _save(cfg)


def rename_server(old_key, new_key):
    if old_key == new_key or not grants_mod.KEY_RE.match(new_key):
        return
    with _lock:
        cfg = load()
        changed = False
        for row in cfg["roles"]:
            if old_key in row["servers"]:
                row["servers"] = list(dict.fromkeys(new_key if k == old_key else k for k in row["servers"]))
                changed = True
        if changed:
            _save(cfg)


# ---------------------------------------------------------------------------
# HTTP to Discord
# ---------------------------------------------------------------------------
def _user_agent():
    from core import discordbot
    return discordbot._user_agent()


def _http(method, url, data=None, headers=None, timeout=10):
    """(status, parsed JSON or None). Network failures raise DiscordAuthError."""
    h = {"User-Agent": _user_agent(), "Accept": "application/json"}
    h.update(headers or {})
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
        h["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(1024 * 1024)
            status = r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(65536), e.code
    except (OSError, ValueError) as e:
        raise DiscordAuthError(f"Discord can't be reached right now ({e}). Try again in a minute.")
    try:
        return status, (json.loads(raw) if raw else None)
    except ValueError:
        return status, None


def _basic(cfg):
    raw = f"{client_id(cfg)}:{cfg['secret']}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


# ---------------------------------------------------------------------------
# The sign-in flow
# ---------------------------------------------------------------------------
def redirect_uri(purpose, base):
    return base + (JOIN_CALLBACK if purpose == "join" else PANEL_CALLBACK)


def begin(purpose, base, account=None, next_path=None, cfg=None):
    """Starts a sign-in. purpose: "panel", "link" (account = the signed-in account's key) or
    "join" (next_path = the join page to return to). Returns (state, address of Discord's page)."""
    cfg = cfg or load()
    if not configured(cfg):
        raise DiscordAuthError("Sign in with Discord isn't set up on this panel.")
    if purpose in ("panel", "link") and not panel_available(cfg):
        raise DiscordAuthError("Sign in with Discord is off on this panel.")
    now = time.time()
    state = secrets.token_urlsafe(24)
    with _lock:
        for k in [k for k, v in _states.items() if v["exp"] < now]:
            _states.pop(k, None)
        if len(_states) >= STATES_MAX:
            _states.pop(min(_states, key=lambda k: _states[k]["exp"]))
        _states[state] = {"purpose": purpose, "base": base, "exp": now + STATE_SECONDS, "account": account,
                          "next": next_path}
    q = {"response_type": "code", "client_id": client_id(cfg), "scope": SCOPES, "state": state,
         "redirect_uri": redirect_uri(purpose, base), "prompt": "none"}
    return state, AUTHORIZE + "?" + urllib.parse.urlencode(q, quote_via=urllib.parse.quote)


def finish(state, cookie_state, code, error=None, cfg=None):
    """The browser came back from Discord. Returns (state record, who) where who is
    {"id", "name", "mfa", "member", "roles"}. Raises DiscordAuthError."""
    cfg = cfg or load()
    with _lock:
        rec = _states.pop(str(state or ""), None)
    if not rec or rec["exp"] < time.time() or not cookie_state or \
            not secrets.compare_digest(str(cookie_state).encode(), str(state).encode()):
        raise DiscordAuthError("That Discord sign-in expired or didn't start here. Try again.")
    if error:
        raise DiscordAuthError("Discord sign-in was cancelled." if error == "access_denied"
                               else "Discord didn't sign you in. Try again.")
    if not code or len(str(code)) > 200:
        raise DiscordAuthError("Discord didn't send a sign-in code. Try again.")
    if not configured(cfg):
        raise DiscordAuthError("Sign in with Discord isn't set up on this panel.")
    status, tok = _http("POST", API + "/oauth2/token",
                        {"grant_type": "authorization_code", "code": str(code),
                         "redirect_uri": redirect_uri(rec["purpose"], rec["base"])}, _basic(cfg))
    if status != 200 or not isinstance(tok, dict) or not tok.get("access_token"):
        why = (tok or {}).get("error") if isinstance(tok, dict) else None
        if why == "invalid_client":
            raise DiscordAuthError("Discord refused this panel's client ID or secret. An admin needs to check them "
                                   "under Panel Settings -> Discord.")
        raise DiscordAuthError("Discord didn't accept the sign-in. Try again."
                               + (" (The redirect address in the Developer Portal must match exactly.)"
                                  if why == "invalid_grant" else ""))
    access = tok["access_token"]
    bearer = {"Authorization": f"Bearer {access}"}
    try:
        status, me = _http("GET", API + "/users/@me", headers=bearer)
        if status != 200 or not isinstance(me, dict) or not str(me.get("id") or "").isdigit():
            raise DiscordAuthError("Discord didn't say who you are. Try again.")
        who = {"id": str(me["id"]), "name": str(me.get("username") or me["id"])[:40],
               "display": str(me.get("global_name") or me.get("username") or "")[:40],
               "mfa": bool(me.get("mfa_enabled")), "member": False, "roles": []}
        if cfg["guild"]:
            status, m = _http("GET", API + f"/users/@me/guilds/{cfg['guild']}/member", headers=bearer)
            if status == 200 and isinstance(m, dict):
                who["member"] = True
                who["roles"] = [str(r) for r in m.get("roles") or [] if str(r).isdigit()][:250]
            elif status not in (403, 404):
                raise DiscordAuthError("Discord didn't say whether you're in the server. Try again in a minute.")
    finally:
        try:                                    # the panel keeps nothing of the token
            _http("POST", API + "/oauth2/token/revoke", {"token": access, "token_type_hint": "access_token"},
                  _basic(cfg), timeout=5)
        except DiscordAuthError:
            pass
    with _lock:
        _member_cache[who["id"]] = (time.time(), who["roles"] if who["member"] else None)
    return rec, who


# ---------------------------------------------------------------------------
# Re-checking a member through the 1.27 bot
# ---------------------------------------------------------------------------
def _bot_for(cfg):
    """The bot token when the 1.27 bot is connected to the community's Discord server, else None."""
    if not cfg["guild"]:
        return None
    try:
        from core import discordbot
        bot = discordbot.load()
        st = discordbot.public(bot)["status"]
        if bot["token"] and st["state"] == "online" and any(g["id"] == cfg["guild"] for g in st["guilds"]):
            return bot["token"]
    except Exception:
        pass
    return None


def check_member(did, cfg=None, fresh=False):
    """Roles of a Discord user in the community's server, read through the bot: a list, None when
    they're not a member (any more), or False when the bot can't tell (not set up, Discord down)."""
    cfg = cfg or load()
    now = time.time()
    with _lock:
        hit = _member_cache.get(str(did))
    if hit and not fresh and now - hit[0] < RECHECK:
        return hit[1]
    token = _bot_for(cfg)
    if not token:
        return False
    try:
        status, m = _http("GET", API + f"/guilds/{cfg['guild']}/members/{did}",
                          headers={"Authorization": f"Bot {token}"}, timeout=5)
    except DiscordAuthError:
        return hit[1] if hit else False
    if status == 200 and isinstance(m, dict):
        roles = [str(r) for r in m.get("roles") or [] if str(r).isdigit()]
    elif status == 404:
        roles = None
    else:
        return hit[1] if hit else False
    with _lock:
        if len(_member_cache) > 5000:
            _member_cache.clear()
        _member_cache[str(did)] = (now, roles)
    return roles


# ---------------------------------------------------------------------------
# Panel sessions of Discord accounts that aren't linked to an account here
# ---------------------------------------------------------------------------
def panel_identity(who, cfg=None):
    """The session identity for a Discord account signing in to the panel on its roles alone, or
    raises DiscordAuthError with why not."""
    cfg = cfg or load()
    if blocked(who["id"], cfg):
        raise DiscordAuthError("This Discord account can't sign in to this panel.")
    if not cfg["guild"] or not cfg["roles"]:
        raise DiscordAuthError("Your Discord account isn't linked to an account on this panel. Sign in with your "
                               "password and link it under 🔑 Password.")
    if not who["member"]:
        raise DiscordAuthError("You're not in this panel's Discord server.")
    if not grants_for(who["roles"], True, cfg):
        raise DiscordAuthError("Your Discord account doesn't have a role that can use this panel.")
    if cfg["require_mfa"] and not who["mfa"]:
        raise DiscordAuthError("Turn on two-factor authentication in Discord first (User Settings, My Account).")
    v = remember(who)
    return {"u": who["id"], "src": "discord", "v": v, "dn": who["name"], "r": who["roles"], "t": int(time.time())}


def session_user(payload):
    """read_session_token() for src "discord": the user dict, or None when the session no longer
    counts (sign-in turned off, blocked, left the server, lost every role that gave something)."""
    cfg = load()
    did = str(payload.get("u") or "")
    if not panel_available(cfg) or not did or blocked(did, cfg) or payload.get("v") != version(did, cfg):
        return None
    if did in cfg["links"]:
        return None                    # linked since: it signs in as that account now
    roles = check_member(did, cfg)
    if roles is False:
        roles = payload.get("r") or []             # no bot: the roles from sign-in, for this session
    if roles is None:
        return None
    g = grants_for(roles, True, cfg)
    if not g:
        return None
    return {"username": f"discord:{payload.get('dn') or did}", "source": "discord", "role": "user",
            "discord_id": did, "grants": g, "two_factor": False, "passkey": False, "needs_2fa_setup": False}
