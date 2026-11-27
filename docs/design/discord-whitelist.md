# Whitelist by Discord role

Status: **plan, waiting for Quentin's go to build** · Target: **1.28.1** (Quentin, 27 Nov 2026), right
after 1.28 · Written 27 Nov 2026

Asked for by Quentin on 27 Nov 2026 during the 1.28 test: a player adds their Minecraft name in
Discord and is whitelisted. Admins choose **per server** how strict that is, so one community can
have open servers and servers that need a moderator's yes (Quentin, 19:50Z: "flexibility for admins
to implement the best fit for their community"). A Discord community usually is one Minecraft
community, so one linked name counts on every server connected to that Discord, cluster nodes
included.

## Who gets in: one choice per server

| Choice | Who is whitelisted | Fits |
| --- | --- | --- |
| **Off** (default) | Nobody through Discord; the whitelist is managed by hand as today. | Anything private. |
| **Anyone in our Discord** | Everyone in the Discord server who linked a name. | Open communities. |
| **These roles** | Linked people with one of the ticked roles (for example Member, or a Player role that Discord's Onboarding hands out). | Most communities. |
| **Moderator approval** | Linked people, after a moderator presses **Approve**. Roles can be ticked to skip approval. | A special server (hardcore, staff, events). |

**Role per server is the default for a community** (Quentin, 19:59Z: "some play the peaceful
server, and others play anarchy"): one Discord role per Minecraft server, as in the wiki's Discord
guide, so the role opens the server's channels *and* its whitelist. Players pick open servers'
roles themselves through an Onboarding question ("Which servers do you want to play?"); a moderated
server's role isn't offered there, and the bot's **Ask to join Anarchy** button sends a request whose
**Approve** gives that server's role. Removing the role (by the player or a moderator) removes the
whitelist entry. Moderators manage everything with Discord roles and never need the panel.

A **Moderators** role list says who may approve (panel admins always can). Approval happens in the
panel (Whitelist & Access tab, **Requests**) or in Discord: the bot posts "quickfire314 asks to join
hardcore as Steve [Approve] [Deny]" in the moderators' channel, and checks the clicker's roles.
An approval is remembered per server; a new name needs a new approval.

**About Discord's Onboarding:** its questions are multiple choice (they hand out roles and
channels) and can't ask for a Minecraft name. So Onboarding is the way to give everyone a role such
as Player automatically ("Do you play on our Minecraft servers?" → Player), and the name always comes
from `/minecraft` (or the join page, below). The bot's welcome can say "Type /minecraft and your
name to get on the servers".

## How it works

**The player**
1. Joins your Discord and presses **Link your Minecraft name** on a message the bot posts (and
   admins pin) in #welcome or the onboarding channel. Discord opens a small form with one text box
   (a modal), which works on phones and needs no typing of commands. `/minecraft Steve` does the
   same, and is how a name is changed later (see Decisions for the command's name).
2. The bot checks the name (Java: it must exist at Mojang, which also gives the UUID; Bedrock: a
   valid gamertag) and answers privately: "Linked **Steve**. You'll be let in once a moderator gives
   you the Member role." Running it again replaces the name; one Minecraft name per Discord
   account (Java and Bedrock names can be one each).
3. Optional: the bot posts "quickfire314 linked Steve" in a private moderators' channel you pick,
   so someone knows a person is waiting.

**The moderator** (only where a server needs it)
4. Gives the person a role (for example **Member**), or presses **Approve** on a request.

**The panel**
5. Within seconds (or within 5 minutes without the members intent, below) the linked name is added
   to the whitelist (Bedrock: allowlist) of every server whose choice lets them in; servers that
   need approval get a request instead, and the bot tells the player which servers are waiting.
6. Losing the role, leaving the Discord, an admin blocking the Discord account, or a moderator
   pressing **Remove** takes the name off again (not on "Anyone in our Discord" servers while they
   stay in the Discord).
7. Every change is in the panel log: `[DISCORD] Steve (discord:quickfire314) added to the
   whitelist of survival: has Member`.

## Settings (Panel Settings → Discord → **Whitelist by role**)

- One row per server (and **Every server**, for servers added later): the choice above, its roles,
  and which roles skip approval. Cluster: the master's table covers its nodes' servers.
- **Moderators' channel** (optional) for the "linked" notes.
- The server's **Whitelist & Access** tab shows, next to each name the panel added, the Discord
  account it came from, and **Remove and block** (removes the name and blocks the Discord account).
- **Names added by hand are never touched.** The panel removes only names it added itself (it keeps
  a record: server, name, Discord id, why).
- The join page can say "Link your Minecraft name in our Discord with /minecraft" next to the invite.

## Who was invited how: invite → role

Quentin, 19:55Z: a public invite (for example on Reddit) needs a moderator to approve each new
player; an invite from friends should go straight through (join, link, play). Discord has no
built-in "role per invite", and an Onboarding answer like "a friend invited me" can be picked by
anyone, so the panel's bot does it from the invite link itself:

- **Settings → Invites:** a table of the Discord server's invites (read with the bot) → the role a
  new member gets, for example `abc` (Reddit) → **Applicant**, `xyz` (friends) → **Member**.
- On `GUILD_MEMBER_ADD` the bot compares invite use counts (kept in memory, refreshed on
  `INVITE_CREATE`/`INVITE_DELETE`) to find the invite used and gives its role. If two invites changed
  at once or the count can't be read, it gives nothing and notes it in the moderators' channel.
- Needs the bot's **Manage Server** permission (to list invites), **Manage Roles** (its role above the
  roles it gives) and the **Server Members Intent**.

- **Unknown or unclear joins need approval:** an invite not in the table, the vanity URL, a
  Discovery join, or two invites used in the same moment all count as "needs approval" (never as
  trusted). Optional extra check for automatic entry: the Discord account is at least N days old.
- **Trusted links get passed on:** the guide tells admins to make friends' invites with a use limit
  and an expiry (or one per friend).

**Approve gives a role:** a moderator's **Approve** can also swap roles (for example Applicant →
Member), so one approval opens every server where Member counts. **Deny** removes the request and,
if ticked, kicks the person.

**The two workflows:**

| | Friends' invite | Public invite (Reddit) |
| --- | --- | --- |
| Joins Discord | Gets **Member** from the invite | Gets **Applicant** (sees #welcome, #apply) |
| Links name | Button or `/minecraft` | Same |
| Whitelist | At once on every "These roles: Member" server | Request in the moderators' channel |
| Moderator | Nothing to do | **Approve** → Member → whitelisted everywhere Member counts |

A server on **Moderator approval** still asks even for Members (hardcore, staff). Discord's own
"Apply to join" (Server Settings → Access) is an alternative for a fully public server, but it
applies to every invite, friends included.

## How it's built

- **Bot (core/discordbot.py):** a **Post the link button** action in the settings (picks a
  channel; message with a button, custom id, answered with a modal), and a second slash command, `/minecraft name:<text>`, registered next to
  `/start`. Answers are ephemeral (flag 64), so names aren't shown in the channel.
- **Links (core/discordauth.py, `discord-signin.json`):** `mc: {discord id: {"java": {"name",
  "uuid"}, "bedrock": {"name"}}}`, plus the record of panel-added entries
  `wl: {server key: {lower name: discord id}}`.
- **Role watch:** with the **Server Members Intent** (a switch in the Developer Portal, free) the
  gateway sends `GUILD_MEMBER_UPDATE` and `GUILD_MEMBER_REMOVE`, and the panel reacts at once. Without
  it the panel checks each linked account every 5 minutes with `GET /guilds/{g}/members/{id}` (the
  same call 1.28 uses), so only linked people are asked about, never the whole member list.
- **Sync (core/whitelist.py):** add and remove through the existing `add()` and `remove()`, so a
  running server changes at once (RCON or console) and a stopped one gets the file. Cluster: the
  master adds on nodes through the node's whitelist route, like the Whitelist tab does.
- Whitelist must be on for the server to matter; the settings page warns when it's off.
- Everything goes through `core.platform`; works the same on Linux and Windows.

## Safety

- Off by default; each server's choice decides, and approval-only servers never add on their own.
- One name per Discord account per edition, so nobody can fill the list.
- Names are validated; Java names that don't exist are refused.
- Only names the panel added are ever removed.
- Blocked Discord accounts can't link and lose their entries.
- Limits: 5 `/minecraft` changes per account per hour.

**On the join page too:** a visitor signed in with Discord can type their Minecraft name there
instead of using `/minecraft`; it writes the same link, so the same per-server choices apply.

## Not in this plan (later, if wanted)

- Discord's Linked Roles (the panel as a Linked Roles app, "has linked a Minecraft name").

## Building it in parts (all in 1.28.1 unless Quentin splits it)

1. Link button and `/minecraft`, per-server choices, the moderators' approval queue (Discord buttons
   and the panel list), removal on leave/ban/block, the log. This alone covers "moderators approve
   everyone" (the Reddit case).
2. Invite → role (and unclear joins needing approval), the account-age check. This adds the
   friends' case: join, link, play.
3. Approve that swaps roles, a reminder when requests wait longer than a few hours, a nightly
   re-check for changes made while the panel was off.

## Tests

- Unit: the command (valid, invalid, unknown Java name, replace), the role watch (intent events and
  the 5-minute check), sync only touching panel-added names, block, leaving the Discord, cluster.
- Real test on the Superman test panel with the bot and the Members intent: link, give Member, join
  the game, take Member away, can't join.

## Size

About two weeks (approval requests and Discord buttons add half a week, invite → role half a week).

## Decisions for Quentin

1. **Command name:** `/minecraft` (recommended: says what it is) or `/link`.
2. **Release:** decided 27 Nov 2026: 1.28.1, right after 1.28.
