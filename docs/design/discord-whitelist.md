# Whitelist by Discord role

Status: **plan, waiting for Quentin's go to build** · Target: **1.28.1** (Quentin, 27 Nov 2026), right
after 1.28 · Written 27 Nov 2026

Asked for by Quentin on 27 Nov 2026 during the 1.28 test: a player adds their Minecraft name in
Discord, a moderator gives them the **Member** role, and from then on they can join the server.
"Anyone can join the Discord" is not enough on its own, so **the role is the only switch**: a name
on its own never gets anyone in.

## How it works

**The player**
1. Joins your Discord and types `/minecraft Steve` (the command's name is the bot's; see Decisions).
2. The bot checks the name (Java: it must exist at Mojang, which also gives the UUID; Bedrock: a
   valid gamertag) and answers privately: "Linked **Steve**. You'll be let in once a moderator gives
   you the Member role." Running it again replaces the name; one Minecraft name per Discord
   account (Java and Bedrock names can be one each).
3. Optional: the bot posts "quickfire314 linked Steve" in a private moderators' channel you pick,
   so someone knows a person is waiting.

**The moderator**
4. Gives the person **Member** in Discord, as they would anyway.

**The panel**
5. Within seconds (or within 5 minutes without the members intent, below) the linked name is added
   to the whitelist (Bedrock: allowlist) of every server that role opens.
6. Taking the role away, the person leaving the Discord, or an admin blocking the Discord account
   in the panel removes the name from those servers again.
7. Every change is in the panel log: `[DISCORD] Steve (discord:quickfire314) added to the
   whitelist of survival: has Member`.

## Settings (Panel Settings → Discord → **Whitelist by role**)

- A table like the role table of Sign in with Discord: **role → servers**. For example Member →
  survival, creative. Rows add up. "Anyone in the Discord server" is not offered here.
- **Moderators' channel** (optional) for the "linked" notes.
- The server's **Whitelist & Access** tab shows, next to each name the panel added, the Discord
  account it came from, and **Remove and block** (removes the name and blocks the Discord account).
- **Names added by hand are never touched.** The panel removes only names it added itself (it keeps
  a record: server, name, Discord id, why).
- The join page can say "Link your Minecraft name in our Discord with /minecraft" next to the invite.

## How it's built

- **Bot (core/discordbot.py):** a second slash command, `/minecraft name:<text>`, registered next to
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

- The role is the switch: a linked name without the role is never added.
- One name per Discord account per edition, so nobody can fill the list.
- Names are validated; Java names that don't exist are refused.
- Only names the panel added are ever removed.
- Blocked Discord accounts can't link and lose their entries.
- Limits: 5 `/minecraft` changes per account per hour.

## Not in this plan (later, if wanted)

- A request/approve list in the panel with Approve and Deny buttons in Discord.
- Linking from the join page (sign in with Discord, type the name). Easy to add on top, since it
  writes the same link.

## Tests

- Unit: the command (valid, invalid, unknown Java name, replace), the role watch (intent events and
  the 5-minute check), sync only touching panel-added names, block, leaving the Discord, cluster.
- Real test on the Superman test panel with the bot and the Members intent: link, give Member, join
  the game, take Member away, can't join.

## Size

About a week.

## Decisions for Quentin

1. **Command name:** `/minecraft` (recommended: says what it is) or `/link`.
2. **Release:** decided 27 Nov 2026: 1.28.1, right after 1.28.
