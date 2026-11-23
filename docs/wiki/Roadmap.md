# Roadmap

What's planned after **1.21**. Version numbers are targets, not promises, and your feedback
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
- **1.22: Windows preview.** An installer for Windows 11 and Windows Server 2022 or 2025, for
  one machine with Java servers: create, console, files, mods and plugins, backups, schedules,
  firewall, webhooks and two-factor sign-in. The first installer is unsigned, so Windows
  shows a warning; checksums are published with it.
- **1.23: everything on one machine.** Bedrock servers with auto-update, memory and CPU
  limits, performance history, crash explanations, off-site backups, panel updates from the
  panel, and the port forwarding guide.
- **1.24: mixed clusters.** Windows and Linux machines in one cluster, with either as the
  master, and servers and backup copies moving between them. Windows stops being a preview.

A few things work differently on Windows and will be documented: Let's Encrypt comes after the
first release, Windows accounts without a password can't sign in to the panel (panel accounts
can), and Windows 10 installs with an end-of-support note. Windows on ARM comes later.

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
| **A read-only status page** for players: online or not, address, version, players online. | Players stop asking "is the server up?". Opt-in, and shows nothing private. |
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

See [What's New](Whats-New.md). The latest: **1.21.2** makes light/dark mode a one-click switch in the top right corner; **1.21.1** lets you choose the disk and folder servers are kept in; **1.21** is groundwork for the Windows version, with nothing changed on screen; **1.20** adds Purpur, Quilt, NeoForge and Forge servers and modpacks from Modrinth or a CurseForge server pack; **1.19** changes a Java server's type in place (Vanilla, Paper, Fabric) with a backup and undo, and rolls back updates; **1.18** adds per-server permissions for helpers and SFTP for FileZilla or WinSCP; **1.17.2** adds Advanced options to New server (builds, Java, memory, limits, startup flags, world and settings); **1.17.1** explains why a server crashed and offers the fix; **1.17** adds performance charts, suggestions and memory and CPU limits; **1.16.14** adds the Port forwarding page, and **1.16.13** moves big worlds with seconds of downtime.
