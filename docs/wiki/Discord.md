# Discord for your Minecraft community

*This page helps you set up a Discord for your own players. To talk to other RedstonePanel
owners, ask a question or hear about new releases, join the
[RedstonePanel Discord](https://discord.gg/MpWzy84Eh).*

A step-by-step guide for a new server admin. You run RedstonePanel with several Minecraft servers
(say a Paper survival server, a Fabric modded server, a Vanilla creative server and a Bedrock
server for console and phone players), and you want one Discord where:

- everyone gets announcements and a general chat;
- each Minecraft server has its own channels and voice, which only its players see;
- you and your moderators get the panel's alerts in a private channel.

You don't need any programming. Steps 1–5 take about half an hour and need nothing installed.
Steps 6 and 7 (chat bridges and voice chat) are optional extras.

**Contents**

1. [How communities use Discord](#1-how-communities-use-discord)
2. [Plan your layout](#2-plan-your-layout)
3. [Create the Discord server and roles](#3-create-the-discord-server-and-roles)
4. [Make a private category for each Minecraft server](#4-make-a-private-category-for-each-minecraft-server)
5. [Send RedstonePanel's alerts to Discord](#5-send-redstonepanels-alerts-to-discord)
6. [Let players pick their servers](#6-let-players-pick-their-servers)
7. [Optional: bridge game chat to Discord](#7-optional-bridge-game-chat-to-discord)
8. [Optional: voice that follows the game](#8-optional-voice-that-follows-the-game)
9. [Keep it safe](#9-keep-it-safe)
10. [Checklist](#10-checklist)

---

## 1. How communities use Discord

A Minecraft community's Discord usually does four jobs:

| Job | What it looks like |
| --- | --- |
| **Announcements** | "Survival restarts at 4:00", "New creative world", "Server updated to 1.21.4". |
| **Chat outside the game** | People talk, share builds and plan events when they aren't playing. Some servers *bridge* game chat into a channel, so both sides see each other's messages. |
| **Voice** | Friends play together in a voice channel, or use proximity voice inside the game. |
| **Staff work** | Alerts from the panel, reports from players, moderation notes. Only staff see these. |

Discord itself doesn't know which Minecraft server someone plays on. You separate people with
**roles**: each Minecraft server gets a role, and only people with that role see its channels.
That's the whole trick, and the rest of this page builds on it.

## 2. Plan your layout

Write down your Minecraft servers and pick a short name and an emoji for each. A layout that
works well:

```
📢 WELCOME              #rules  #announcements  #server-status      everyone (read-only)
💬 COMMUNITY            #general  #screenshots  🔊 Lounge            everyone
🌲 SURVIVAL (Paper)     #survival-chat  #survival-help  🔊 Survival  role: Survival
⚙️ MODDED (Fabric)      #modded-chat  #modpack-info  🔊 Modded       role: Modded
🎨 CREATIVE (Vanilla)   #creative-chat  🔊 Creative                  role: Creative
📱 BEDROCK              #bedrock-chat  🔊 Bedrock                    role: Bedrock
🔧 STAFF                #panel-alerts  #mod-log  #staff-chat         role: Staff
```

Tips:

- **One Discord for everything** is usually best: one invite link, one set of rules, and players
  can join more than one server's channels. Separate Discords only make sense for groups that
  should never meet, such as a public server and a private family one.
- Put **how to connect** (address and port, shown on each server's **Overview** tab in the
  panel) in that server's own channel, not in a public one, if the server is whitelisted.
- `#server-status` can be a read-only channel where you post maintenance and updates.

## 3. Create the Discord server and roles

1. In Discord, click **+** (Add a Server) → **Create My Own** → **For a club or community**.
   Name it after your community.
2. **Server Settings → Enable Community.** This turns on the rules screen, announcement channels
   and **Onboarding**, which step 6 uses to let players pick their servers. Follow its
   prompts (it asks for a rules channel and a moderator-updates channel).
3. **Server Settings → Roles → Create Role**, once per role:
   - **Staff** (you and your moderators). Give it *Manage Messages*, *Kick Members*,
     *Timeout Members* and *Manage Roles*. Don't give *Administrator* to anyone but yourself.
   - One role per Minecraft server: **Survival**, **Modded**, **Creative**, **Bedrock**…
     These need no special permissions; they only unlock channels. Give each a colour so players
     can see who plays where.
4. Drag **Staff** above the server roles in the list. Discord only lets a role manage roles
   below it.

## 4. Make a private category for each Minecraft server

For each Minecraft server:

1. Right-click the channel list → **Create Category**. Name it (for example `🌲 SURVIVAL`) and turn
   on **Private Category**. Pick the server's role (and **Staff**) as who can see it.
2. Right-click the category → **Create Channel** for each text and voice channel. New channels
   **inherit the category's permissions** ("synced"), so they're private too.

If you made the category without the switch, set it by hand: **Edit Category → Permissions**:

| Role | View Channel |
| --- | --- |
| `@everyone` | ✖ deny |
| Survival (this server's role) | ✔ allow |
| Staff | ✔ allow |

Now someone with only the **Survival** role sees the welcome and community channels plus the
Survival category, and nothing of the other servers, text or voice. Someone with **Survival**
and **Modded** sees both.

Make the **🔧 STAFF** category the same way, with only **Staff** allowed.

For **#announcements** and **#rules**, allow `@everyone` to *View Channel* but deny *Send
Messages*, so only staff post there.

## 5. Send RedstonePanel's alerts to Discord

RedstonePanel sends alerts to Discord through a **webhook**: a private address that lets the
panel post into one channel. No bot is needed.

1. In Discord, open `#panel-alerts` (in the staff category) → **Edit Channel → Integrations →
   Webhooks → New Webhook**. Name it "RedstonePanel", then **Copy Webhook URL**.
2. In RedstonePanel, sign in as an admin → **🛡️ Settings → Notifications**. Paste the URL.
3. Tick the events you want. A good start:

   | Event | Why |
   | --- | --- |
   | **Server crashed** | The most important one. The server restarts by itself, and you learn about it, with the cause in plain words when the panel recognises it. |
   | **Backup failed** | So a broken backup never goes unnoticed. |
   | **Scheduled restart** | Confirms nightly restarts happen (or tells you when one fails). |
   | **Player turned away** | Someone tried to join and the whitelist refused them, so you can let them in from **Configuration → Whitelist & Access**. |
   | Server started / stopped, Backup finished, Player joined | Useful on a quiet server, noisy on a busy one. |

4. **Save notifications**, then **Send a test**. A message should appear in `#panel-alerts`.

Each message names the panel and the Minecraft server it's about. Crash messages come straight
from systemd, so they arrive even while the panel itself is stopped.

**Things to know:**

- **One webhook per panel.** All servers on one RedstonePanel post to the same channel. If you
  run a [cluster](../guide/README.md#18-clusters), each machine's panel has its own webhook, so each
  machine can post to its own channel.
- **The webhook URL is a password.** Anyone who has it can post in that channel. The panel keeps
  it readable only by root and never shows it again. If it leaks, delete the webhook in Discord
  and paste a new one.
- Want players to see when a server is down for maintenance? Post it yourself in
  `#server-status`. Keep the panel's alerts in the staff channel: they include details players
  don't need.

## 6. Let players pick their servers

Players need the right roles to see a server's channels. Three ways to give them out:

- **Onboarding (recommended).** **Server Settings → Onboarding → Questions → Add a Question**:
  "Which servers do you play on?", with one answer per Minecraft server, each giving its role,
  and **Allow multiple answers** on. New members answer it when they join; anyone can change their
  answers later under **Channels & Roles** at the top of the channel list.
- **By hand.** Right-click a member → **Roles**. Fine for a small group of friends, and the right
  choice for a whitelisted server: give the role when you add them to the whitelist in the panel.
- **Automatically, from the game.** A chat bridge plugin can link Discord accounts to Minecraft
  accounts and hand out roles to linked players (see step 7). Most work for Paper.

## 7. Optional: bridge game chat to Discord

A bridge copies chat between the game and a Discord channel: players in the game see Discord
messages, and people in Discord see game chat, joins and deaths. Give each Minecraft server its
own bridged channel (for example `#survival-chat`), and they stay separate.

Bridges run **on the Minecraft server**, so what you can use depends on the server's type:

| Server type in RedstonePanel | Bridge |
| --- | --- |
| **Paper** (plugins) | **DiscordSRV** is the most widely used: chat, joins and deaths, a console channel for staff, account linking and role sync. Download it from its official page and drop the `.jar` on the server's **Plugins** tab, or look for it under **Find plugins**. |
| **Fabric** (mods) | Search **Find mods** for "Discord" and pick a *server-side* bridge mod that has a build for your version. Read its page: setup differs per mod. |
| **Vanilla** | No plugins or mods, so no bridge. Use the server's Discord channels for chat, or switch it to Paper or Fabric in place with **Maintenance → Server type** (RedstonePanel 1.19 and later), which keeps the world. |
| **Bedrock** | No plugin system, so no bridge on the server itself. The panel's alerts still work. |

**Setting up a bridge bot** (the same idea for any bridge):

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) →
   **New Application** → name it after your community.
2. **Bot** → **Reset Token** → copy the token. **It's a password for your bot:** never post it,
   and don't put it in a screenshot.
3. On the same page, turn on **Message Content Intent** and **Server Members Intent**. Bridges
   need them to read chat and to manage roles.
4. **OAuth2 → URL Generator:** tick `bot`, and the permissions the bridge's documentation asks
   for (usually *View Channels*, *Send Messages*, *Read Message History*, *Manage Roles* for role
   sync). Open the generated link and add the bot to your Discord.
5. Get the channel's ID: **User Settings → Advanced → Developer Mode** on, then right-click the
   channel → **Copy Channel ID**.
6. In RedstonePanel, open the server's **Files** tab, find the bridge's config file (for
   DiscordSRV, `plugins/DiscordSRV/config.yml`, created on the first start), paste the token and
   channel ID where its documentation says, **Save**, and **Restart** the server.

Use **one bot per Minecraft server**, or one bot shared by all, each server pointed at its own
channel. One bot is simpler; separate bots keep a problem on one server from affecting others.

**Roles from the game:** with account linking, players type a command in game (DiscordSRV:
`/discord link`) and send the code to the bot. The bridge can then give linked players that
server's role, so they see its channels without picking it themselves. Combined with a
permissions plugin such as LuckPerms (in the **Server essentials** bundle), you can tie Discord
roles to in-game ranks.

## 8. Optional: voice that follows the game

You have two choices, and they work well together.

**Discord voice per server.** Each private category from step 4 has its own voice channel, so
players only hear people from their own server. Nothing to install.

**Proximity voice inside the game.** With **Simple Voice Chat**, players hear people who are near
them *in the game*, and only on the same Minecraft server. It doesn't use Discord at all.

- It's available for **Fabric** (a mod) and **Paper** (a plugin): search **Find mods** or **Find
  plugins** for "Simple Voice Chat" and install it. Players install the client mod too, so this
  suits modded communities best.
- **Bedrock** and **Vanilla** players can't use it.
- It needs its **own UDP port**, 24454 by default, and every server on the same machine needs a
  different one. RedstonePanel looks after that: when a second server is set to a voice port
  another server already uses, the panel moves it to the next free port in its
  `voicechat-server.properties` (Fabric: `config/voicechat/`, Paper: `plugins/voicechat/`) and
  **Needs attention** asks you to restart it. See
  [Mods and plugins that open their own port](../guide/README.md#mods-and-plugins-that-open-their-own-port).
- The panel opens each server's voice port in the firewall (ufw) too.
  If players connect from the internet, forward the same UDP ports on your router.

## 9. Keep it safe

- **Secrets:** the panel's webhook URL and any bot token are passwords. Panel admins can read a
  server's files, including a bridge's config with its token, so only make trusted people
  admins (see [Users](../guide/README.md#16-users)). If a token or webhook leaks, reset it in Discord.
- **Server Settings → Safety Setup:** set a verification level (at least *Low*: a verified
  email), and turn on **AutoMod** for spam and harmful links.
- **Whitelists:** for a community server, turn on the whitelist and add players from the panel's
  **Configuration → Whitelist & Access**. "Player turned away" alerts (step 5) tell you when
  someone new is waiting.
- **Kids on your server?** Discord's minimum age is 13 in most countries. Keep voice and DMs in
  mind, and consider in-game chat only for younger players.
- **Backups:** your worlds matter more than your Discord. Set up scheduled backups on each server's
  **Backups** tab, with the **Backup failed** alert on.

## 10. Checklist

- [ ] Discord server created, **Community** enabled.
- [ ] Roles: **Staff**, plus one per Minecraft server.
- [ ] Welcome and community categories, open to everyone; announcements read-only.
- [ ] One **private category** per Minecraft server, text and voice, synced permissions.
- [ ] Private **🔧 STAFF** category with `#panel-alerts`.
- [ ] Webhook pasted into **RedstonePanel → Settings → Notifications**, test message received.
- [ ] **Onboarding** question "Which servers do you play on?" giving the server roles.
- [ ] Each server's address posted in its own channel.
- [ ] Optional: a chat bridge on Paper/Fabric servers, each to its own channel.
- [ ] Optional: Simple Voice Chat, a different UDP port per server, opened in ufw.
- [ ] Verification level and AutoMod on; backups scheduled.

---

**Related pages:** [User Manual → Notifications](../guide/README.md#notifications) ·
[Mods, plugins and add-ons](../guide/README.md#12-mods-plugins-and-add-ons) ·
[Files](../guide/README.md#11-files) · [FAQ](FAQ.md) · [Roadmap](Roadmap.md)
