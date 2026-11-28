"""Whitelist by Discord (1.28).

Players link their Minecraft name in the community's Discord (a "Link your Minecraft name" button
that opens a form, or /minecraft), and the panel keeps each server's whitelist (Bedrock: allowlist)
in step with who may play there. Each server has one choice:

    off        the default: Discord never touches this server's whitelist
    anyone     everyone in the Discord server who linked a name
    roles      linked people with one of the server's roles (and, with trust "approve", approved)
    approval   linked people a moderator approved for this server; some roles may skip that

Trust: with "roles" (the default) a role is enough; with "approve" a person also needs a trusted
invite (see invites) or a moderator's Approve before any role counts. Every request, approval and
change is logged; the panel removes only names it added itself (`entries`).

Discord does the rest through the panel's own bot (core/discordbot.py): it answers the button,
the form, /minecraft and the moderators' Approve and Deny buttons, hears member changes (with the
Server Members Intent) and tells which invite a new member used. Without the intent, linked
people's roles are read every RECHECK seconds instead.

Settings and state in DATA_DIR/discord-whitelist.json (0600).
"""
import json
import os
import re
import threading
import time

from core import grants as grants_mod
from core.settings import DATA_DIR

WL_FILE = os.path.join(DATA_DIR, "discord-whitelist.json")
MODES = ("off", "anyone", "roles", "approval")
MODE_LABELS = {"off": "Off", "anyone": "Anyone in our Discord", "roles": "These roles",
               "approval": "Moderator approval"}
TRUSTS = ("roles", "approve")
ID_RE = re.compile(r"^[0-9]{15,22}$")
INVITE_RE = re.compile(r"^[A-Za-z0-9-]{2,40}$")
JAVA_NAME = re.compile(r"^[A-Za-z0-9_]{3,16}$")
GAMERTAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ]{0,14}[A-Za-z0-9]?$")
DISCORD_EPOCH_MS = 1420070400000
RECHECK = 300                       # seconds between role reads without the members intent
LINKS_PER_HOUR = 5
LINKS_MAX = 5000
ALL = grants_mod.ALL_SERVERS

_lock = threading.RLock()
_link_times = {}                    # discord id -> [times]


class WhitelistError(ValueError):
    """A change that can't be made; the message is safe to show (in the panel and in Discord)."""


# ---------------------------------------------------------------------------
# Settings and state
# ---------------------------------------------------------------------------
def _defaults():
    return {"enabled": False, "guild": "", "trust": "roles", "members_intent": False,
            "servers": {},              # server key or "*" -> {"mode", "roles": [ids], "skip": [ids]}
            "approvers": [],            # roles that may press Approve (panel admins always may)
            "mod_channel": "", "link_channel": "", "link_message": "",
            "approve_role": "",         # a role Approve also gives (and Applicant roles it takes away)
            "remove_roles": [],
            "invites": {},              # invite code -> {"trust": "trusted"|"approval", "role": id}
            "min_age_days": 0,
            "links": {},                # discord id -> {"name", "java": {...}, "bedrock": {...}, "approved", ...}
            "requests": {},             # discord id -> {server key or "*": {"at"}}
            "approvals": {},            # discord id -> {server key: {"by", "at"}}
            "exceptions": {},           # discord id -> {server key: "allow"|"deny"}
            "entries": {},              # server key -> {lower name: discord id}: names the panel added
            "joins": {},                # discord id -> {"via", "trust", "at"}: how they joined the Discord
            "updated": None, "updated_by": None}


def load():
    out = _defaults()
    try:
        with open(WL_FILE) as f:
            data = json.load(f)
        if isinstance(data, dict):
            out.update({k: data[k] for k in out if k in data and isinstance(data[k], type(out[k]) if out[k] is not None else object)})
    except (OSError, ValueError):
        pass
    out["enabled"] = bool(out["enabled"])
    if out["trust"] not in TRUSTS:
        out["trust"] = "roles"
    return out


def _save(cfg):
    os.makedirs(os.path.dirname(WL_FILE), exist_ok=True)
    tmp = WL_FILE + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(cfg, f, indent=1)
    os.replace(tmp, WL_FILE)


def _log(msg):
    try:
        from core import manager
        manager.write_log(msg)
    except Exception:
        pass


def _ids(values, what):
    if not isinstance(values, (list, tuple)):
        raise WhitelistError(f"Send the {what} as a list.")
    out = []
    for v in values:
        v = str(v).strip()
        if not ID_RE.match(v):
            raise WhitelistError(f"Not a Discord {what[:-1]} ID: {v[:40]}")
        if v not in out:
            out.append(v)
    return out[:100]


def _id(value, what):
    value = str(value or "").strip()
    if value and not ID_RE.match(value):
        raise WhitelistError(f"Not a Discord {what} ID: {value[:40]}")
    return value


def guild(cfg=None):
    """The Discord server whose members count: this feature's own, else Sign in with Discord's."""
    cfg = cfg or load()
    if cfg["guild"]:
        return cfg["guild"]
    from core import discordauth
    return discordauth.load()["guild"]


def save_settings(new, actor="?"):
    """Admin's change from Panel Settings. Only the keys sent change. Returns public()."""
    with _lock:
        cfg = load()
        before = (cfg["enabled"], cfg["members_intent"])
        if "enabled" in new:
            cfg["enabled"] = bool(new["enabled"])
        if "guild" in new:
            cfg["guild"] = _id(new["guild"], "server")
        if "trust" in new:
            if new["trust"] not in TRUSTS:
                raise WhitelistError("Choose whether a role is enough or people need approving first.")
            cfg["trust"] = new["trust"]
        if "members_intent" in new:
            cfg["members_intent"] = bool(new["members_intent"])
        for k in ("mod_channel", "link_channel", "approve_role"):
            if k in new:
                cfg[k] = _id(new[k], "channel" if k.endswith("channel") else "role")
        for k in ("approvers", "remove_roles"):
            if k in new:
                cfg[k] = _ids(new[k], "roles")
        if "min_age_days" in new:
            try:
                cfg["min_age_days"] = max(0, min(365, int(new["min_age_days"] or 0)))
            except (TypeError, ValueError):
                raise WhitelistError("The account age is a number of days.")
        if "servers" in new:
            cfg["servers"] = _clean_servers(new["servers"])
        if "invites" in new:
            cfg["invites"] = _clean_invites(new["invites"])
        if cfg["enabled"] and not guild(cfg):
            raise WhitelistError("Choose your Discord server first.")
        cfg.update(updated=int(time.time()), updated_by=actor)
        _save(cfg)
    _log(f"[DISCORD] {actor} " + ("turned Whitelist by Discord on" if cfg["enabled"] else "turned Whitelist by Discord off")
         + f": {describe(cfg)}.")
    if (cfg["enabled"], cfg["members_intent"]) != before:
        from core import discordbot
        discordbot.apply()          # reconnect with the intents, and (un)register /minecraft
    _after_change()
    return public()


def _clean_servers(rows):
    if not isinstance(rows, dict):
        raise WhitelistError("Send the servers as a table.")
    out = {}
    for key, row in list(rows.items())[:500]:
        if not isinstance(key, str) or not grants_mod.KEY_RE.match(key) or not isinstance(row, dict):
            raise WhitelistError("A server in the table isn't known.")
        mode = row.get("mode") or "off"
        if mode not in MODES:
            raise WhitelistError(f"Unknown choice for {key}.")
        roles = _ids(row.get("roles") or [], "roles")
        skip = _ids(row.get("skip") or [], "roles")
        if mode == "roles" and not roles:
            raise WhitelistError(f"Tick at least one role for {key.split('/')[-1]}, or pick another choice.")
        if mode != "off" or key != ALL:     # a server's own Off keeps it out of an "every server" row
            out[key] = {"mode": mode, "roles": roles if mode == "roles" else [],
                        "skip": skip if mode == "approval" else []}
    return out


def _clean_invites(rows):
    if not isinstance(rows, dict):
        raise WhitelistError("Send the invites as a table.")
    out = {}
    for code, row in list(rows.items())[:200]:
        if not isinstance(code, str) or not INVITE_RE.match(code) or not isinstance(row, dict):
            raise WhitelistError("An invite in the table isn't a Discord invite code.")
        trust = row.get("trust") or "approval"
        if trust not in ("trusted", "approval"):
            raise WhitelistError("Each invite is trusted or needs approval.")
        out[code] = {"trust": trust, "role": _id(row.get("role"), "role"), "label": str(row.get("label") or "")[:60]}
    return out


def describe(cfg):
    rows = [f"{k.split('/')[-1] if k != ALL else 'every server'} {MODE_LABELS[v['mode']].lower()}"
            for k, v in sorted(cfg["servers"].items()) if v["mode"] != "off" or ALL in cfg["servers"]]
    return ", ".join(rows) or "no servers"


def public(cfg=None):
    """What the settings page shows."""
    cfg = cfg or load()
    accounts = []
    for did, link in sorted(cfg["links"].items(), key=lambda x: (x[1].get("name") or "").lower()):
        accounts.append({"id": did, "name": link.get("name") or did, "java": (link.get("java") or {}).get("name"),
                         "bedrock": (link.get("bedrock") or {}).get("name"), "approved": bool(link.get("approved")),
                         "approved_by": link.get("approved_by"), "via": link.get("via"), "member": link.get("member", True),
                         "roles": link.get("roles") or [], "linked": link.get("at"),
                         "servers": sorted(k for k, names in cfg["entries"].items() if did in names.values()),
                         "requests": sorted((cfg["requests"].get(did) or {}).keys()),
                         "exceptions": cfg["exceptions"].get(did) or {}})
    out = {k: v for k, v in cfg.items() if k not in ("links", "requests", "approvals", "entries", "exceptions", "joins")}
    out.update(guild=cfg["guild"], guild_used=guild(cfg), accounts=accounts,
               waiting=sum(1 for did, r in cfg["requests"].items() if r and did in cfg["links"]),
               mode_labels=MODE_LABELS)
    return out


def _after_change():
    """Settings or links changed: bring the whitelists in step, without holding up the caller."""
    threading.Thread(target=sync, name="discord-whitelist-sync", daemon=True).start()


# ---------------------------------------------------------------------------
# Who may play where
# ---------------------------------------------------------------------------
def account_age_days(did, now=None):
    try:
        created = ((int(did) >> 22) + DISCORD_EPOCH_MS) / 1000.0
    except (TypeError, ValueError):
        return 0
    return ((time.time() if now is None else now) - created) / 86400


def _row(cfg, key):
    return cfg["servers"].get(key) or cfg["servers"].get(ALL) or {"mode": "off", "roles": [], "skip": []}


def approved(cfg, did):
    """Trusted at all: always with trust "roles"; with "approve", a trusted invite or Approve."""
    return cfg["trust"] == "roles" or bool((cfg["links"].get(did) or {}).get("approved"))


def may_play(cfg, did, key):
    """(yes/no, why) for this Discord account on the server with this key."""
    from core import discordauth
    link = cfg["links"].get(did)
    if not cfg["enabled"] or not link:
        return False, "not linked"
    exc = (cfg["exceptions"].get(did) or {}).get(key)
    if discordauth.blocked(did):
        return False, "blocked"
    if exc == "deny":
        return False, "kept off by a moderator"
    if exc == "allow":
        return True, "allowed by a moderator"
    if not link.get("member", True):
        return False, "not in the Discord server"
    row = _row(cfg, key)
    roles = set(link.get("roles") or ())
    if row["mode"] == "off":
        return False, "off"
    if row["mode"] == "anyone":
        return True, "in the Discord server"
    if row["mode"] == "roles":
        if not roles & set(row["roles"]):
            return False, "no role for this server"
        if not approved(cfg, did):
            return False, "waiting for approval"
        return True, "has a role for this server"
    # approval
    if key in (cfg["approvals"].get(did) or {}):
        return True, "approved for this server"
    if roles & set(row["skip"]) and approved(cfg, did):
        return True, "has a role that skips approval"
    return False, "waiting for approval"


def wanted(cfg, servers):
    """{server key: {lower name: (name, discord id, why)}} that should be on each whitelist."""
    out = {}
    for s in servers:
        key = s["key"]
        java = s.get("edition") != "Bedrock"
        for did, link in cfg["links"].items():
            ok, why = may_play(cfg, did, key)
            entry = link.get("java" if java else "bedrock") or {}
            if ok and entry.get("name"):
                out.setdefault(key, {})[entry["name"].lower()] = (entry["name"], did, why)
    return out


def _servers():
    from core import discordbot
    return discordbot._all_servers(fresh=True)


# ---------------------------------------------------------------------------
# Keeping the whitelists in step
# ---------------------------------------------------------------------------
_sync_lock = threading.Lock()


def _change(s, action, name):
    """Adds or removes one name on a server here or on a node. Raises WhitelistError."""
    from core import whitelist
    if s["member"] == "local":
        try:
            return (whitelist.add if action == "add" else whitelist.remove)(s["name"], name)
        except whitelist.WhitelistError as e:
            raise WhitelistError(str(e))
    from core import cluster, cluster_client
    node = cluster.find_node(s["member"])
    if not node:
        raise WhitelistError("its cluster member isn't known")
    try:
        return cluster_client.call_node(node, "POST", f"/api/servers/{s['name']}/whitelist/{action}",
                                        {"player": name}, actor="discord-whitelist")
    except cluster_client.NodeError as e:
        raise WhitelistError(str(e))


def sync(servers=None):
    """Adds what's wanted and missing, removes what the panel added and isn't wanted any more.
    Names added by hand are never touched. Returns [(action, server key, name, ok, note)]."""
    if not _sync_lock.acquire(blocking=False):
        return []                   # one already running picks this up: it reads the newest state
    try:
        cfg = load()
        try:
            servers = servers if servers is not None else _servers()
        except Exception as e:      # noqa: BLE001
            _log(f"[DISCORD] Whitelist by Discord couldn't list the servers: {e}")
            return []
        want = wanted(cfg, servers) if cfg["enabled"] else {}
        done = []
        for s in servers:
            key = s["key"]
            have = dict(cfg["entries"].get(key) or {})
            for low, (name, did, why) in (want.get(key) or {}).items():
                if have.get(low) == did:
                    continue
                try:
                    got = _change(s, "add", name)
                    ok, note = True, why
                except WhitelistError as e:
                    ok, note = False, str(e)
                if ok and "already on" in str((got or {}).get("message") or ""):
                    # Someone put this name there by hand: it stays theirs, so the panel never removes it
                    done.append(("add", key, name, True, "already on by hand"))
                    continue
                if ok:
                    have[low] = did
                    who = (cfg["links"].get(did) or {}).get("name") or did
                    _log(f"[DISCORD] Added {name} (discord:{who}) to the whitelist of {s['name']}: {why}.")
                else:
                    _log(f"[DISCORD] Couldn't add {name} to the whitelist of {s['name']}: {note}")
                done.append(("add", key, name, ok, note))
            for low, did in list(have.items()):
                if low in (want.get(key) or {}):
                    continue
                link = cfg["links"].get(did) or {}
                name = ((link.get("java") or {}).get("name") if s.get("edition") != "Bedrock"
                        else (link.get("bedrock") or {}).get("name")) or low
                if name.lower() != low:
                    name = low              # the link moved to another name: remove the old one
                _, why = may_play(cfg, did, key)
                try:
                    _change(s, "remove", name)
                    ok, note = True, why
                except WhitelistError as e:
                    ok, note = "isn't on" in str(e), str(e)
                if ok:
                    have.pop(low, None)
                    _log(f"[DISCORD] Removed {name} (discord:{link.get('name') or did}) from the whitelist of "
                         f"{s['name']}: {why}.")
                else:
                    _log(f"[DISCORD] Couldn't remove {name} from the whitelist of {s['name']}: {note}")
                done.append(("remove", key, name, ok, note))
            with _lock:
                fresh = load()
                if have:
                    fresh["entries"][key] = have
                else:
                    fresh["entries"].pop(key, None)
                _save(fresh)
        return done
    finally:
        _sync_lock.release()


def forget_server(key):
    with _lock:
        cfg = load()
        changed = cfg["servers"].pop(key, None) is not None
        changed = cfg["entries"].pop(key, None) is not None or changed
        for d in (cfg["approvals"], cfg["requests"], cfg["exceptions"]):
            for did in list(d):
                if key in d[did]:
                    d[did].pop(key)
                    changed = True
        if changed:
            _save(cfg)


def rename_server(old_key, new_key):
    if old_key == new_key or not grants_mod.KEY_RE.match(new_key):
        return
    with _lock:
        cfg = load()
        if old_key in cfg["servers"]:
            cfg["servers"][new_key] = cfg["servers"].pop(old_key)
        if old_key in cfg["entries"]:
            cfg["entries"][new_key] = cfg["entries"].pop(old_key)
        for d in (cfg["approvals"], cfg["requests"], cfg["exceptions"]):
            for did in d:
                if old_key in d[did]:
                    d[did][new_key] = d[did].pop(old_key)
        _save(cfg)


# ---------------------------------------------------------------------------
# Linking a name, member changes, requests and approvals
# ---------------------------------------------------------------------------
def check_java(name):
    """(name as Mojang spells it, uuid) or raises WhitelistError."""
    name = str(name or "").strip()
    if not JAVA_NAME.match(name):
        raise WhitelistError(f"'{name[:40]}' isn't a Java player name (3 to 16 letters, numbers or _).")
    from core import manager
    uuid = manager.get_java_uuid(name)
    if not uuid:
        raise WhitelistError(f"Minecraft doesn't know a Java player called {name}. Check the spelling.")
    return name, uuid


def check_bedrock(name):
    name = " ".join(str(name or "").split())
    if not GAMERTAG.match(name):
        raise WhitelistError(f"'{name[:40]}' isn't an Xbox gamertag (up to 16 letters, numbers and spaces).")
    return name


def link(did, who, java=None, bedrock=None, roles=None, now=None):
    """A Discord account links its names (from the button's form or /minecraft). `who`: its
    Discord name. Returns (link, servers it may play on now, servers it may ask for)."""
    from core import discordauth
    now = time.time() if now is None else now
    if not ID_RE.match(str(did)):
        raise WhitelistError("That Discord account can't be read.")
    if discordauth.blocked(did):
        raise WhitelistError("This Discord account is blocked on this panel.")
    java = check_java(java) if java else None
    bedrock = check_bedrock(bedrock) if bedrock else None
    if not java and not bedrock:
        raise WhitelistError("Type your Minecraft name.")
    with _lock:
        cfg = load()
        if not cfg["enabled"]:
            raise WhitelistError("Whitelist by Discord isn't turned on for this panel.")
        times = [t for t in _link_times.get(did, []) if now - t < 3600]
        if len(times) >= LINKS_PER_HOUR:
            raise WhitelistError("You've changed your name a few times in the last hour. Try again later.")
        _link_times[did] = times + [now]
        for other, l in cfg["links"].items():
            if other == did:
                continue
            if java and (l.get("java") or {}).get("uuid") == java[1]:
                raise WhitelistError(f"{java[0]} is already linked to another Discord account. Ask a moderator.")
            if bedrock and (l.get("bedrock") or {}).get("name", "").lower() == bedrock.lower():
                raise WhitelistError(f"{bedrock} is already linked to another Discord account. Ask a moderator.")
        if len(cfg["links"]) >= LINKS_MAX and did not in cfg["links"]:
            raise WhitelistError("This panel has too many linked accounts.")
        l = cfg["links"].setdefault(did, {"approved": False, "at": int(now)})
        join = cfg["joins"].get(did) or {}
        if join and not l.get("via"):
            l["via"] = join.get("via")
        if join.get("trust") == "trusted" and not l.get("approved"):
            l.update(approved=True, approved_by=f"invite {join.get('via')}")
        old = (l.get("java") or {}).get("name"), (l.get("bedrock") or {}).get("name")
        l["name"] = str(who or did)[:40]
        if java:
            l["java"] = {"name": java[0], "uuid": java[1]}
        if bedrock:
            l["bedrock"] = {"name": bedrock}
        if roles is not None:
            l.update(roles=[str(r) for r in roles][:250], member=True, checked=int(now))
        l["changed"] = int(now)
        _save(cfg)
    names = " and ".join(x for x in ((java or [None])[0], bedrock) if x)
    _log(f"[DISCORD] discord:{l['name']} ({did}) linked {names}"
         + (f" (was {' and '.join(x for x in old if x)})" if any(old) else "") + ".")
    sync()
    return status_for(did)


def status_for(did):
    """(link, [servers they may play on], [servers they may ask to join]) as (key, label) lists."""
    cfg = load()
    try:
        servers = _servers()
    except Exception:               # noqa: BLE001
        servers = []
    play, ask = [], []
    for s in servers:
        ok, why = may_play(cfg, did, s["key"])
        label = s["name"] + (f" on {s['where']}" if s.get("where") else "")
        if ok:
            play.append((s["key"], label))
        elif why == "waiting for approval" or (_row(cfg, s["key"])["mode"] == "approval"):
            ask.append((s["key"], label))
    return cfg["links"].get(did), play, ask


def member_update(did, roles=None, member=True, now=None):
    """The bot heard (or read) a member's roles, or that they left. Re-syncs when it matters."""
    with _lock:
        cfg = load()
        l = cfg["links"].get(did)
        if not l:
            return False
        new_roles = [str(r) for r in (roles or [])][:250] if member else []
        if l.get("member", True) == bool(member) and (not member or sorted(l.get("roles") or []) == sorted(new_roles)):
            l["checked"] = int(time.time() if now is None else now)
            _save(cfg)
            return False
        l.update(roles=new_roles, member=bool(member), checked=int(time.time() if now is None else now))
        _save(cfg)
    if not member:
        _log(f"[DISCORD] discord:{l.get('name')} ({did}) left the Discord server.")
    sync()
    return True


def ask(did, key, now=None):
    """A request: key "*" = approve this person at all (trust "approve"), else one server.
    Returns True when it's new (the bot then tells the moderators)."""
    with _lock:
        cfg = load()
        if did not in cfg["links"]:
            raise WhitelistError("Link your Minecraft name first.")
        r = cfg["requests"].setdefault(did, {})
        if key in r:
            return False
        r[key] = {"at": int(time.time() if now is None else now)}
        _save(cfg)
    name = cfg["links"][did].get("name") or did
    _log(f"[DISCORD] discord:{name} ({did}) asked to join " + ("the servers" if key == "*" else key.split("/")[-1]) + ".")
    return True


def may_approve(cfg, roles, is_admin=False):
    return bool(is_admin) or bool(set(roles or ()) & set(cfg["approvers"]))


def decide(did, key, yes, by, now=None):
    """A moderator's Approve or Deny. key "*" approves the person; a server key approves them there.
    Returns a short text for the moderators' message."""
    now = time.time() if now is None else now
    with _lock:
        cfg = load()
        link = cfg["links"].get(did)
        if not link:
            raise WhitelistError("That Discord account hasn't linked a name (or was forgotten).")
        (cfg["requests"].get(did) or {}).pop(key, None)
        if not cfg["requests"].get(did):
            cfg["requests"].pop(did, None)
        if yes:
            if key == "*":
                link.update(approved=True, approved_by=by, approved_at=int(now))
            else:
                cfg["approvals"].setdefault(did, {})[key] = {"by": by, "at": int(now)}
        _save(cfg)
    what = "the servers" if key == "*" else key.split("/")[-1]
    _log(f"[DISCORD] {by} {'approved' if yes else 'turned down'} discord:{link.get('name')} ({did}) for {what}.")
    if yes:
        sync()
    return f"{'Approved' if yes else 'Turned down'} by {by}."


def set_account(did, changes, actor):
    """Admin, from Panel Settings: approve or un-approve, exceptions, or forget the link."""
    with _lock:
        cfg = load()
        link = cfg["links"].get(did)
        if not link:
            raise WhitelistError("No such linked Discord account.")
        if changes.get("forget"):
            cfg["links"].pop(did, None)
            for d in (cfg["requests"], cfg["approvals"], cfg["exceptions"]):
                d.pop(did, None)
            _save(cfg)
            _log(f"[DISCORD] {actor} forgot the linked names of discord:{link.get('name')} ({did}).")
            sync()
            return public()
        if "approved" in changes:
            link["approved"] = bool(changes["approved"])
            link["approved_by"] = actor if link["approved"] else None
            if link["approved"]:
                (cfg["requests"].get(did) or {}).pop("*", None)
        if "approvals" in changes:
            keys = changes["approvals"]
            if not isinstance(keys, list) or not all(isinstance(k, str) and grants_mod.KEY_RE.match(k) for k in keys):
                raise WhitelistError("Choose servers to approve.")
            cfg["approvals"][did] = {k: (cfg["approvals"].get(did) or {}).get(k) or {"by": actor, "at": int(time.time())}
                                     for k in keys}
            for k in keys:
                (cfg["requests"].get(did) or {}).pop(k, None)
        if "exceptions" in changes:
            exc = changes["exceptions"]
            if not isinstance(exc, dict) or not all(isinstance(k, str) and grants_mod.KEY_RE.match(k)
                                                    and v in ("allow", "deny") for k, v in exc.items()):
                raise WhitelistError("Each exception is allow or keep off, for a server.")
            cfg["exceptions"][did] = dict(exc)
        for d in (cfg["requests"], cfg["approvals"], cfg["exceptions"]):
            if did in d and not d[did]:
                d.pop(did)
        _save(cfg)
    _log(f"[DISCORD] {actor} changed what discord:{link.get('name')} ({did}) may join.")
    sync()
    return public()


def invite_trust(cfg, code, did, now=None):
    """What joining through this invite gives: ("trusted"|"approval", role id or "")."""
    row = cfg["invites"].get(code or "") or {}
    trust = row.get("trust") or "approval"
    if trust == "trusted" and cfg["min_age_days"] and account_age_days(did, now) < cfg["min_age_days"]:
        trust = "approval"
    return trust, row.get("role") or ""


def joined(did, name, code, now=None):
    """A new member joined the Discord server through invite `code` (None: couldn't tell).
    Returns (trust, role to give or "")."""
    cfg = load()
    trust, role = invite_trust(cfg, code, did, now) if code else ("approval", "")
    with _lock:
        cfg = load()
        l = cfg["links"].get(did)
        if l is not None:
            l["member"] = True
            if trust == "trusted" and not l.get("approved"):
                l.update(approved=True, approved_by=f"invite {code}")
            _save(cfg)
        pending = cfg["joins"]
        pending[did] = {"via": code, "trust": trust, "at": int(time.time() if now is None else now)}
        if len(pending) > LINKS_MAX:
            for k in sorted(pending, key=lambda k: pending[k]["at"])[:len(pending) - LINKS_MAX]:
                pending.pop(k)
        _save(cfg)
    _log(f"[DISCORD] discord:{name} ({did}) joined the Discord server"
         + (f" through invite {code} ({'trusted' if trust == 'trusted' else 'needs approval'})" if code
            else " (the invite couldn't be told)") + ".")
    return trust, role


