# Roadmap

What's being built now and what's planned after **1.28.0**, the latest release. Version numbers are targets, not promises, and your feedback
decides the order: tell us in the
[discussions](https://github.com/qrussell/redstonepanel/discussions) or on
[Discord](https://discord.gg/B5YkQm3zaX) what you'd use first.

The guiding rules stay the same: **protect people's worlds first**, use the system's own service manager rather than
reinventing it (systemd on Linux), and add no dependencies that aren't in Ubuntu's archive. The full working plan,
with the reasoning behind each item, is in [docs/design/roadmap.md](../design/roadmap.md).

## Next up

### 1.21 to 1.24: RedstonePanel on Windows

A native Windows version with every feature of the Linux one: the same panel in your browser,
no Docker and no WSL. Each game server runs as an ordinary Windows service, schedules are Task
Scheduler tasks, and ports are opened in Windows Defender Firewall, so Windows' own tools
show everything the panel does. It arrives in four releases:

- **1.21: groundwork** *(released)*. The Linux panel is reorganised so one codebase can serve both systems.
  Nothing changes on screen.
- **1.22: Windows preview** *(released)*. An installer for Windows 11 and Windows Server 2022 or 2025, for
  one machine with Java servers: create, console, files, mods and plugins, backups, schedules,
  firewall, webhooks and two-factor sign-in. The first installer is unsigned, so Windows
  shows a warning; checksums are published with it.
- **1.23: everything on one machine** *(released)*. Bedrock servers with auto-update and Phantom, Quilt,
  Forge, NeoForge and modpacks, hard memory and CPU limits, crash explanations for Windows
  causes, off-site backups (SSH storage and S3), panel updates from the panel, signing in with
  a Windows account, and Needs attention notes for ports Hyper-V or WSL reserved, a PC set to
  sleep and a pending Windows Update restart. The terminal stays Linux-only by design: on
  Windows the owner is usually at that computer and can open a command prompt there. SFTP
  comes with 1.24.
- **1.23.1: port check** *(released)*. Finds port conflicts the panel can't prevent when it makes a server:
  another program on a server's port (named, with its process, path, command line, service or
  Docker container), servers edited by hand onto one port, mods the panel doesn't know, a server
  on the panel's own port and router forwards that don't match. It moves RCON, Query and mod
  ports by itself, offers **Move to a free port** for game ports, and refuses to start a server
  whose port another program holds. Linux and Windows.
- **1.23.2: removing the panel safely** *(released)*. **Panel Settings → Remove RedstonePanel** and
  `sudo redstonepanel uninstall` back up your servers and ask what happens to each one (stop it
  and keep its files, keep it running, or delete it) before removing the panel, so no server is
  left running with nothing to manage it by accident.
- **1.24: mixed clusters** *(released)*. Windows and Linux machines in one cluster, with either as the
  master, and servers and backup copies moving between them. Bedrock servers get the new
  machine's program; a move to Windows is refused, with the list, when the folder holds file
  names Windows can't (checked before anything stops). SFTP on Windows, and Let's Encrypt
  through win-acme (a renewed certificate is picked up by itself). Windows stops being a preview.
  Sign-in on Windows panels: Windows accounts and panel accounts, optional two-factor, and
  **Windows Hello** (face, fingerprint or PIN) through passkeys; the installer's Finish page
  opens a one-use link to choose the admin password, and `redstonepanel reset-password NAME`
  resets a forgotten one. Linux panels keep Linux accounts, panel accounts and two-factor.

A few things work differently on Windows and are documented: Windows accounts without a password can't sign in to the panel (panel accounts
can), and Windows 10 installs with an end-of-support note. Windows on ARM comes later.

### 1.25: an API for scripts and bots, and Start at boot *(released)*

- **API tokens** for scripts and bots (a Discord bot that starts a server, a script that backs
  one up): made in Panel Settings or with `redstonepanel token create`, limited to the servers
  and permissions you choose, never an admin, shown once, expiring, revocable, and logged under
  the token's name.
- **Start at boot**, per server, on the Maintenance tab: turn it off for a server you only play
  now and then and it stays stopped after a reboot until someone starts it. Works on Linux and
  Windows, and is kept through restores, reinstalls and moves to another node.
- **Open dashboard, signed in:** an admin on the master opens a node's own panel already signed
  in, without an account there. Each node can turn it off.

### 1.26: sleep mode, and Start on the join page *(released)*

- **Start when a player joins.** A server nobody is playing on stops after a time you choose,
  and the panel answers on its port instead ("Sleeping: join to start it"). When someone joins,
  it starts again and they join about 30 seconds later. For Java and Bedrock, per server, off by
  default, on Linux and Windows and on cluster nodes. A sleeping server doesn't start at boot:
  it goes to sleep instead.
- **Start the server from its join page.** An admin can let visitors of a server's public join
  page start it while it's stopped, with limits so nobody can start it over and over.

### 1.27: Discord /start *(released)*

- **Start a server from Discord.** People in your Discord type `/start`, pick a server, and the
  panel starts it (or wakes it from sleep mode), then says when it's online and where to join.
  The panel itself is the bot: paste a bot token in Panel Settings, choose the Discord servers
  and roles that may use it and the servers it may start. Nothing to forward, Linux and Windows.

### 1.28: Sign in with Discord, who can see a join page, and Whitelist by Discord *(released)*

- **Sign in with Discord** on the panel. Roles in your Discord server give people permissions on
  chosen servers, so giving someone the Mod role is all it takes. Free, in the core panel.
- **Join pages by permission.** Each join page can stay open to anyone, ask visitors to sign in
  (with a panel account or Discord), or go by permission: who may see how to join, who may
  **Start** the server and who may **Stop** it. A page can also require joining your Discord
  first and show its invite link. Discord is optional.
- **Whitelist by Discord.** Players link their Minecraft name in your Discord and the panel keeps
  each server's whitelist: per server for anyone in your Discord, by role, or after a moderator
  approves them. Trusted invites (your friends' link) skip approval; losing the role takes the
  name off again. Names you add by hand are never touched.

## Ideas under consideration

Suggested after 1.15 and not scheduled yet. Each one fits the panel's rules; the ones marked ⭐
close the gaps people most often notice when choosing a panel.

| Idea | Why |
| --- | --- |
| **Cloudflare Tunnel for the master panel** (opt-in): paste a tunnel token in Panel Settings and reach the panel at your own hostname, with no port opened or forwarded on your router. Game traffic is unchanged. | Reaching the panel from outside today means opening a port and handling a certificate. A tunnel needs neither, and keeps your home IP address private. |
| **Whitelist kept in step across servers** from the cluster master. | Today each server's whitelist is changed on its own. |
| **Scheduled commands and announcements** (for example a daily message or `/save-all`). | Common on other panels, and a natural use of the same systemd timers as restarts. |
| **World tools:** upload a world, start a fresh world with a seed, pre-generate with Chunky from a button. | World downloads already exist; the other direction is missing. |
| **Discord two-way bridge:** game chat to a channel and back. | Builds on the existing webhooks. |

## Later, when someone asks for it

- A **CurseForge** browser in the panel, using your own CurseForge API key.
- Adopting a node over SSH, and finding panels on the LAN.
- **Velocity** proxy support for networks across nodes.
- Translations, once a contributor offers one.

## Not planned, and why

| Idea | Why not |
| --- | --- |
| Docker mode | Running servers as plain systemd services is the reason the panel exists. |
| Load balancing or auto-migration | A Minecraft server is one stateful process; moving a server by choice (1.16) covers the real need. |
| Converting worlds between Java and Bedrock | Converters lose blocks and redstone behaviour. Geyser and Floodgate let Bedrock players join Java servers instead. |
| Downgrading a world to an older Minecraft version | Minecraft doesn't support it; 1.19 rolls back to the backup from before an update instead. |
| Multi-tenant hosting, billing or paid tiers | It's an MIT project with one developer, not a hosting business. |

## Recently shipped

See [What's New](Whats-New.md). The latest: **1.28** adds Sign in with Discord, join pages by permission (who may see, Start and Stop) and Whitelist by Discord, which keeps whitelists in step with Discord roles; **1.27** adds a Discord `/start` command that starts a server (or wakes it from sleep mode), with the panel itself as the bot; **1.26** adds sleep mode, which stops a server nobody plays on and starts it when someone joins, and a Start button on join pages; **1.25** adds API tokens for scripts and bots, a Start at boot switch per server and Open dashboard signed in on cluster nodes, and makes Update now work on Windows again; **1.24.1** opens the HTTPS port when a Windows PC joins a cluster shows a server created on a node straight away and loads a master's Dashboard much faster; **1.24** mixes Linux and Windows machines in one cluster and adds SFTP, Windows Hello sign-in and an easier first password on Windows; **1.23.2** lets you remove the panel safely, choosing what happens to each server first; **1.23.1** adds a port check that finds port conflicts, names the other program and fixes what it safely can; **1.23** brings Bedrock, more loaders and modpacks, limits, off-site backups, Update now and Windows sign-in to Windows; **1.22** is the Windows preview: an installer for Windows 11 and Server 2022 or 2025 that runs Java servers on one computer; **1.21.10** opens a shell on the machine in your browser, for admins, as a Linux account; **1.21.9** lets HAProxy or Cloudflare serve the join pages on your own domain, and keeps upgrades from restarting game servers; **1.21.8** fixes security problems found by code scanning: the panel works in server folders with the game account's rights, so links there can't reach other files; **1.21.7** opens settings files such as `server.properties` and `eula.txt` on the Files tab in a Visual Editor like Configuration's; **1.21.6** adds player packs: the mods players need, built from the server's own mods folder and offered on its join page; **1.21.5** adds join pages: a public page per server that tells players how to join, on a port of its own; **1.21.4** adds quick commands to Live Chat: suggestions as you type, a form that builds the command and one-click buttons; **1.21.3** finds Minecraft servers set up outside the panel and can take them over in place, with undo; **1.21.2** makes light/dark mode a one-click switch in the top right corner; **1.21.1** lets you choose the disk and folder servers are kept in; **1.21** is groundwork for the Windows version, with nothing changed on screen; **1.20** adds Purpur, Quilt, NeoForge and Forge servers and modpacks from Modrinth or a CurseForge server pack; **1.19** changes a Java server's type in place (Vanilla, Paper, Fabric) with a backup and undo, and rolls back updates; **1.18** adds per-server permissions for helpers and SFTP for FileZilla or WinSCP; **1.17.2** adds Advanced options to New server (builds, Java, memory, limits, startup flags, world and settings); **1.17.1** explains why a server crashed and offers the fix; **1.17** adds performance charts, suggestions and memory and CPU limits; **1.16.14** adds the Port forwarding page, and **1.16.13** moves big worlds with seconds of downtime.
