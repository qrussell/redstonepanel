# Roadmap

What's planned after **1.16.0**. Version numbers are targets, not promises, and your feedback
decides the order: tell us in the
[discussions](https://github.com/qrussell/redstonepanel/discussions) what you'd use first.

The guiding rules stay the same: **protect people's worlds first**, use systemd rather than
reinventing it, and add no dependencies that aren't in Ubuntu's archive. The full working plan,
with the reasoning behind each item, is in [docs/design/roadmap.md](../design/roadmap.md).

## Next up

### 1.16.x: less downtime for big worlds

- An optional **pre-copy** for moves: copy the world while the server keeps running, then only
  what changed after it stops.
- **Find panels on your network** and offer to add them to the cluster.

### 1.17: server performance and tuning

- **Tick health** (TPS and MSPT) for Paper, Vanilla and Fabric 1.20.3+.
- **Memory:** the heap limit next to what the server really uses, and a **Memory** setting to
  change it safely.
- **History charts:** 24 hours at 1-minute detail and 7 days at 15-minute detail, for each
  server and the host.
- **Suggestions based on what was measured, never applied automatically:** lower simulation
  distance when TPS drops, Lithium for Fabric, FastAsyncWorldEdit, pre-generating with Chunky,
  or moving a busy server to a quieter node.
- **Alerts** for low TPS or a nearly full disk (off by default).

### 2.0: run it for other people

- **Per-server permissions** for user accounts: view, console, power, files, mods, settings.
- **API tokens** with a limited set of allowed routes, shown once and revocable.

## Ideas under consideration

Suggested after 1.15 and not scheduled yet. Each one fits the panel's rules; the ones marked ⭐
close the gaps people most often notice when choosing a panel.

| Idea | Why |
| --- | --- |
| ⭐ **Off-machine backups:** copy backups to another cluster node, an SFTP/rsync target or S3-compatible storage, on the same schedule. | A backup on the same disk doesn't survive losing the disk. Clusters make "copy to another node" nearly free. |
| ⭐ **Automatic Bedrock updates** (opt-in), with a backup first and a player warning. | Bedrock players' clients update themselves, and then can't join an older server. |
| ⭐ **Two-factor sign-in** (TOTP) for admin accounts. | Admins are effectively root; a password alone is a thin wall for a public-facing panel. |
| ⭐ **A first-run setup guide:** create your first server, turn on backups, HTTPS and notifications. | Gets new owners to a safe setup in minutes, without reading the manual. |
| **Player actions:** kick, ban, unban and a ban list on each server; whitelist kept in step across servers. | Today these need `/` commands in Live Chat. |
| **Scheduled commands and announcements** (for example a daily message or `/save-all`). | Common on other panels, and a natural use of the same systemd timers as restarts. |
| **Version update alerts:** "a new Paper build / Minecraft release is available" per server. | Updating is one click already; knowing when to is the missing half. |
| **Modrinth modpacks** (`.mrpack`) and **Forge / NeoForge**. | The most-requested server types the panel doesn't have. |
| **World tools:** upload a world, start a fresh world with a seed, pre-generate with Chunky from a button. | World downloads already exist; the other direction is missing. |
| **Per-server resource limits** (systemd `MemoryMax`, `CPUQuota`) alongside 1.17's Memory setting. | Stops one runaway server from starving the others, using what systemd already offers. |
| **A read-only status page** for players: online or not, address, version, players online. | Players stop asking "is the server up?". Opt-in, and shows nothing private. |
| **Mobile-friendly layout** and an installable app (PWA). | Restarting a server from a phone is a common need. |
| **Debian 12/13 and ARM64** (Raspberry Pi) for Java servers. | Many home servers are Debian or a Pi. Bedrock itself stays x86-64 only. |
| **Discord two-way bridge:** game chat to a channel and back. | Builds on the existing webhooks. |

## Later, when someone asks for it

- Backup retention by age ("keep 30 days") as well as by count.
- Adopting a node over SSH, and finding panels on the LAN.
- **Velocity** proxy support for networks across nodes.
- Translations, once a contributor offers one.

## Not planned, and why

| Idea | Why not |
| --- | --- |
| Docker mode | Running servers as plain systemd services is the reason the panel exists. |
| Load balancing or auto-migration | A Minecraft server is one stateful process; moving a server by choice (1.16) covers the real need. |
| Multi-tenant hosting, billing or paid tiers | It's an MIT project with one developer, not a hosting business. |

## Recently shipped

See [What's New](Whats-New.md). The latest: **1.16.0** moves and copies servers between the
machines of a cluster, and **1.15.0** manages every node's servers from the master.
