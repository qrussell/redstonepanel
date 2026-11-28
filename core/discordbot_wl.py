"""The bot's side of Whitelist by Discord (1.28): the link button and its form, /minecraft, the
"Ask to join" buttons, the moderators' Approve and Deny, member events, invites and the re-check.

Rules and state live in core/discordwl.py; this module only talks to Discord. Interactions that
need a slow step (Mojang's name check, adding to whitelists) are deferred: Discord gets an answer
at once and the message is edited when the work is done.
"""
import threading
import time

from core import discordbot as bot
from core import discordwl as wl

DEFERRED = 5                        # interaction response: "thinking", edited later
UPDATE_MESSAGE = 7
MODAL = 9
ADMINISTRATOR = 8                   # Discord permission bit
LINK_ID, FORM_ID = "rpwl:link", "rpwl:form"
LINK_TEXT = ("**Play on our Minecraft servers**\nPress the button and type your Minecraft name. "
             "You can also type `/minecraft` anywhere in this Discord.")

_invites = {}                       # guild id -> {code: uses}
_invite_lock = threading.Lock()


def _reply(text, ephemeral=True, components=None):
    r = bot._reply(text, ephemeral)
    if components:
        r["data"]["components"] = components
    return r


def _buttons(items):
    """[(custom_id, label, style)] -> action rows of up to 5 buttons (25 at most)."""
    rows = []
    for i in range(0, min(len(items), 25), 5):
        rows.append({"type": 1, "components": [{"type": 2, "style": st, "label": label[:80], "custom_id": cid[:100]}
                                               for cid, label, st in items[i:i + 5]]})
    return rows


def _edit(itoken, text, components=None):
    app_id = bot._status.get("app_id")
    if not itoken or not app_id:
        return
    body = {"content": text[:1900], "allowed_mentions": {"parse": []}, "components": components or []}
    try:
        bot.api("PATCH", f"/webhooks/{app_id}/{itoken}/messages/@original", body)
    except bot.ApiError as e:
        bot._log(f"[DISCORD] Couldn't update an answer: {e}")


def _member_roles(d):
    return [str(r) for r in (d.get("member") or {}).get("roles") or []]


def _is_admin(d):
    try:
        return bool(int((d.get("member") or {}).get("permissions") or 0) & ADMINISTRATOR)
    except (TypeError, ValueError):
        return False


def _token():
    return bot.load()["token"]


# ---------------------------------------------------------------------------
# Interactions
# ---------------------------------------------------------------------------
def answer(d, now=None):
    """INTERACTION_CREATE for Whitelist by Discord -> (response, function to run afterwards)."""
    cfg = wl.load()
    if not cfg["enabled"]:
        return _reply("Whitelist by Discord isn't turned on for this panel."), None
    if str(d.get("guild_id") or "") != wl.guild(cfg):
        return _reply("Use this in the community's Discord server."), None
    data = d.get("data") or {}
    cid = str(data.get("custom_id") or "")
    if d.get("type") == 3 and cid == LINK_ID:
        return {"type": MODAL, "data": {"custom_id": FORM_ID, "title": "Link your Minecraft name", "components": [
            {"type": 1, "components": [{"type": 4, "custom_id": "java", "label": "Java Edition name", "style": 1,
                                        "min_length": 3, "max_length": 16, "required": False,
                                        "placeholder": "Steve"}]},
            {"type": 1, "components": [{"type": 4, "custom_id": "bedrock", "label": "Bedrock (Xbox) gamertag",
                                        "style": 1, "max_length": 16, "required": False,
                                        "placeholder": "Only if you play Bedrock"}]}]}}, None
    if d.get("type") == 5 and cid == FORM_ID:
        values = {}
        for row in data.get("components") or []:
            for c in row.get("components") or []:
                values[c.get("custom_id")] = str(c.get("value") or "").strip()
        return _link(d, values.get("java"), values.get("bedrock"))
    if d.get("type") == 2:
        opts = {o.get("name"): str(o.get("value") or "") for o in data.get("options") or [] if isinstance(o, dict)}
        if not opts.get("java") and not opts.get("bedrock"):
            return _reply("Type your name: `/minecraft java:Steve` (or `bedrock:` for an Xbox gamertag)."), None
        return _link(d, opts.get("java"), opts.get("bedrock"))
    if d.get("type") == 3 and cid.startswith("rpwl:ask:"):
        return _ask(d, cid[len("rpwl:ask:"):])
    if d.get("type") == 3 and cid.startswith(("rpwl:ok:", "rpwl:no:")):
        _, verb, rest = cid.split(":", 2)
        did, _, key = rest.partition(":")
        return _decide(d, cfg, did, key, verb == "ok")
    return _reply("That button is from an older version of this panel."), None


def _link(d, java, bedrock):
    did, name = bot._who(d)
    roles = _member_roles(d)
    itoken = d.get("token")

    def after():
        try:
            link, play, ask = wl.link(did, name, java or None, bedrock or None, roles=roles)
        except wl.WhitelistError as e:
            _edit(itoken, str(e))
            return
        cfg = wl.load()
        names = " and ".join(x for x in ((link.get("java") or {}).get("name"), (link.get("bedrock") or {}).get("name")) if x)
        text = f"Linked **{names}**."
        if play:
            text += " You can play on " + ", ".join(f"**{label}**" for _, label in play) + "."
        if cfg["trust"] == "approve" and not link.get("approved"):
            if wl.ask(did, "*"):
                notify_moderators(did, "*")
            text += " A moderator will check your request soon; you'll be let in when they approve it."
        elif not play and not ask:
            text += " There's no server for your roles yet; a moderator can give you one."
        buttons = [(f"rpwl:ask:{key}", f"Ask to join {label}", 2) for key, label in ask]
        _edit(itoken, text, _buttons(buttons))

    return {"type": DEFERRED, "data": {"flags": bot.EPHEMERAL}}, after


def _ask(d, key):
    did, name = bot._who(d)
    cfg = wl.load()
    if did not in cfg["links"]:
        return _reply("Link your Minecraft name first (the button, or `/minecraft`)."), None
    ok, why = wl.may_play(cfg, did, key)
    if ok:
        return _reply("You can already play there."), None
    try:
        new = wl.ask(did, key)
    except wl.WhitelistError as e:
        return _reply(str(e)), None
    if new:
        threading.Thread(target=notify_moderators, args=(did, key), daemon=True).start()
    return _reply("Asked. A moderator will check it soon." if new else "You've already asked; a moderator will "
                  "check it soon."), None


def _decide(d, cfg, did, key, yes):
    if not wl.may_approve(cfg, _member_roles(d), _is_admin(d)):
        return _reply("Only moderators can do that."), None
    _, by = bot._who(d)
    try:
        text = wl.decide(did, key, yes, f"discord:{by}")
    except wl.WhitelistError as e:
        return _reply(str(e)), None
    old = str((d.get("message") or {}).get("content") or "")
    if yes and key == "*":
        threading.Thread(target=_approve_roles, args=(did, cfg), daemon=True).start()
    return {"type": UPDATE_MESSAGE, "data": {"content": f"{old}\n**{text}**"[:1900], "components": [],
                                             "allowed_mentions": {"parse": []}}}, None


def _approve_roles(did, cfg):
    """Approve also gives a role (for example Member) and takes the Applicant roles, if set."""
    g, token = wl.guild(cfg), _token()
    try:
        if cfg["approve_role"]:
            bot.api("PUT", f"/guilds/{g}/members/{did}/roles/{cfg['approve_role']}", token=token)
        for r in cfg["remove_roles"]:
            bot.api("DELETE", f"/guilds/{g}/members/{did}/roles/{r}", token=token)
    except bot.ApiError as e:
        bot._log(f"[DISCORD] Couldn't change the roles of {did} after approval: {e}")
        return
    if cfg["approve_role"] or cfg["remove_roles"]:
        read_member(did, token, g)


def notify_moderators(did, key):
    cfg = wl.load()
    if not cfg["mod_channel"]:
        return
    link = cfg["links"].get(did) or {}
    names = ", ".join(f"{ed} `{(link.get(ed.lower()) or {}).get('name')}`" for ed in ("Java", "Bedrock")
                      if (link.get(ed.lower()) or {}).get("name"))
    days = int(wl.account_age_days(did))
    what = "to join the servers" if key == "*" else f"to join **{key.split('/')[-1]}**"
    via = link.get("via") or (cfg["joins"].get(did) or {}).get("via")
    text = (f"<@{did}> ({link.get('name')}) asks {what}: {names}. Discord account {days} days old"
            + (f", joined through invite `{via}`" if via else "") + ".")
    body = {"content": text[:1900], "allowed_mentions": {"parse": []},
            "components": _buttons([(f"rpwl:ok:{did}:{key}", "Approve", 3), (f"rpwl:no:{did}:{key}", "Deny", 4)])}
    try:
        bot.api("POST", f"/channels/{cfg['mod_channel']}/messages", body, token=_token())
    except bot.ApiError as e:
        bot._log(f"[DISCORD] Couldn't post the request in the moderators' channel: {e}")


def post_link_button(channel):
    """Posts the "Link your Minecraft name" message in `channel`. Returns its message id."""
    if not wl.ID_RE.match(str(channel or "")):
        raise wl.WhitelistError("Choose the channel for the button.")
    token = _token()
    if not token or bot._status.get("state") != "online":
        raise wl.WhitelistError("The Discord bot isn't connected. Set it up under Discord first.")
    body = {"content": LINK_TEXT, "allowed_mentions": {"parse": []},
            "components": _buttons([(LINK_ID, "Link your Minecraft name", 1)])}
    try:
        got = bot.api("POST", f"/channels/{channel}/messages", body, token=token)
    except bot.ApiError as e:
        raise wl.WhitelistError(f"Discord didn't take the message: {e}. The bot needs to see and send messages in "
                                "that channel.")
    return str((got or {}).get("id") or "")


def channels(guild=None):
    """[{id, name}] of the guild's text channels, for the settings page."""
    g = guild or wl.guild()
    token = _token()
    if not g or not token:
        return []
    try:
        got = bot.api("GET", f"/guilds/{g}/channels", token=token) or []
    except bot.ApiError:
        return []
    return [{"id": str(c["id"]), "name": str(c.get("name") or "")[:100]} for c in got
            if isinstance(c, dict) and c.get("type") in (0, 5)]


def invites(guild=None):
    """[{code, uses, inviter, max_uses, expires}] of the guild's invites, for the settings page."""
    g = guild or wl.guild()
    token = _token()
    if not g or not token:
        return []
    try:
        got = bot.api("GET", f"/guilds/{g}/invites", token=token) or []
    except bot.ApiError:
        return []
    return [{"code": str(i.get("code")), "uses": int(i.get("uses") or 0), "max_uses": int(i.get("max_uses") or 0),
             "inviter": str((i.get("inviter") or {}).get("username") or ""), "expires": i.get("expires_at")}
            for i in got if isinstance(i, dict) and i.get("code")]


# ---------------------------------------------------------------------------
# Member events, invites, the re-check
# ---------------------------------------------------------------------------
def _uses(g, token):
    got = bot.api("GET", f"/guilds/{g}/invites", token=token) or []
    return {str(i["code"]): int(i.get("uses") or 0) for i in got if isinstance(i, dict) and i.get("code")}


def ready(token, guild_id=None):
    """READY or GUILD_CREATE: remember each invite's use count, to tell later joins apart."""
    cfg = wl.load()
    g = wl.guild(cfg)
    if not cfg["enabled"] or not g or (guild_id and guild_id != g):
        return
    try:
        uses = _uses(g, token)
    except bot.ApiError as e:
        bot._log(f"[DISCORD] Can't read the Discord server's invites (the bot needs Manage Server): {e}")
        return
    with _invite_lock:
        _invites[g] = uses


def which_invite(g, token):
    """The invite a member who just joined used: the one whose count went up by one, alone, or None."""
    with _invite_lock:
        before = dict(_invites.get(g) or {})
        try:
            after = _uses(g, token)
        except bot.ApiError:
            return None
        _invites[g] = after
    up = [c for c, n in after.items() if n > before.get(c, 0)]
    gone = [c for c in before if c not in after]       # a last use of a limited invite deletes it
    if len(up) == 1 and not gone:
        return up[0]
    if not up and len(gone) == 1:
        return gone[0]
    return None


def event(t, d, token):
    cfg = wl.load()
    g = wl.guild(cfg)
    if not cfg["enabled"] or str(d.get("guild_id") or "") != g:
        return
    user = d.get("user") or {}
    did = str(user.get("id") or "")
    if t in ("INVITE_CREATE", "INVITE_DELETE"):
        with _invite_lock:
            inv = _invites.setdefault(g, {})
            if t == "INVITE_CREATE":
                inv[str(d.get("code"))] = int(d.get("uses") or 0)
            else:
                inv.pop(str(d.get("code")), None)
        return
    if not did or user.get("bot"):
        return
    if t == "GUILD_MEMBER_REMOVE":
        wl.member_update(did, member=False)
        return
    roles = [str(r) for r in d.get("roles") or []]
    if t == "GUILD_MEMBER_ADD":
        code = which_invite(g, token)
        name = str(user.get("global_name") or user.get("username") or did)[:40]
        trust, role = wl.joined(did, name, code)
        if role:
            try:
                bot.api("PUT", f"/guilds/{g}/members/{did}/roles/{role}", token=token)
                roles.append(role)
            except bot.ApiError as e:
                bot._log(f"[DISCORD] Couldn't give the invite's role to {name}: {e}")
    wl.member_update(did, roles=roles, member=True)


def read_member(did, token, g):
    try:
        m = bot.api("GET", f"/guilds/{g}/members/{did}", token=token)
    except bot.ApiError as e:
        if e.status == 404:
            wl.member_update(did, member=False)
        return
    wl.member_update(did, roles=(m or {}).get("roles") or [], member=True)


def recheck_loop(token, stop, every=None):
    """Reads linked people's roles: every wl.RECHECK seconds without the members intent, once a day
    with it (to catch what changed while the panel was off)."""
    last_full = 0
    while not stop.wait(30):
        cfg = wl.load()
        g = wl.guild(cfg)
        if not cfg["enabled"] or not g:
            continue
        now = time.time()
        period = every or (86400 if cfg["members_intent"] else wl.RECHECK)
        if now - last_full < period:
            continue
        last_full = now
        for did in list(cfg["links"]):
            if stop.is_set():
                return
            read_member(did, token, g)
            stop.wait(0.5)
        wl.sync()
