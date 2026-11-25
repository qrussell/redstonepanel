# Roadmap

What's planned after **1.23**. Version numbers are targets, not promises, and your feedback
decides the order: tell us in the
[discussions](https://github.com/qrussell/redstonepanel/discussions) what you'd use first.

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
- **1.24: mixed clusters.** Windows and Linux machines in one cluster, with either as the
  master, and servers and backup copies moving between them. Windows stops being a preview.

A few things work differently on Windows and are documented: Let's Encrypt comes with 1.24, Windows accounts without a password can't sign in to the panel (panel accounts
can), and Windows 10 installs with an end-of-support note. Windows on ARM comes later.

### 1.24.1: sign in with Windows Hello (Windows panels)

- **Passkeys** on Windows panels: sign in with your Windows Hello PIN, face or fingerprint (or a
  phone or security key) instead of typing a password. Works on the panel's own PC at
  `localhost`, or from elsewhere when the panel is on a host name with a trusted certificate.
  Passwords and optional two-factor stay. Linux panels keep Linux accounts and panel accounts
  with optional two-factor.

### 2.0: an API for scripts and bots

- **API tokens** with a limited set of allowed routes and servers, shown once and revocable.

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

See [What's New](Whats-New.md). The latest: **1.23.2** lets you remove the panel safely, choosing what happens to each server first; **1.23.1** adds a port check that finds port conflicts, names the other program and fixes what it safely can; **1.23** brings Bedrock, more loaders and modpacks, limits, off-site backups, Update now and Windows sign-in to Windows; **1.22** is the Windows preview: an installer for Windows 11 and Server 2022 or 2025 that runs Java servers on one computer; **1.21.10** opens a shell on the machine in your browser, for admins, as a Linux account; **1.21.9** lets HAProxy or Cloudflare serve the join pages on your own domain, and keeps upgrades from restarting game servers; **1.21.8** fixes security problems found by code scanning: the panel works in server folders with the game account's rights, so links there can't reach other files; **1.21.7** opens settings files such as `server.properties` and `eula.txt` on the Files tab in a Visual Editor like Configuration's; **1.21.6** adds player packs: the mods players need, built from the server's own mods folder and offered on its join page; **1.21.5** adds join pages: a public page per server that tells players how to join, on a port of its own; **1.21.4** adds quick commands to Live Chat: suggestions as you type, a form that builds the command and one-click buttons; **1.21.3** finds Minecraft servers set up outside the panel and can take them over in place, with undo; **1.21.2** makes light/dark mode a one-click switch in the top right corner; **1.21.1** lets you choose the disk and folder servers are kept in; **1.21** is groundwork for the Windows version, with nothing changed on screen; **1.20** adds Purpur, Quilt, NeoForge and Forge servers and modpacks from Modrinth or a CurseForge server pack; **1.19** changes a Java server's type in place (Vanilla, Paper, Fabric) with a backup and undo, and rolls back updates; **1.18** adds per-server permissions for helpers and SFTP for FileZilla or WinSCP; **1.17.2** adds Advanced options to New server (builds, Java, memory, limits, startup flags, world and settings); **1.17.1** explains why a server crashed and offers the fix; **1.17** adds performance charts, suggestions and memory and CPU limits; **1.16.14** adds the Port forwarding page, and **1.16.13** moves big worlds with seconds of downtime.
