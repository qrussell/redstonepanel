# Roadmap

What's planned after **1.20**. Version numbers are targets, not promises, and your feedback
decides the order: tell us in the
[discussions](https://github.com/qrussell/redstonepanel/discussions) what you'd use first.

The guiding rules stay the same: **protect people's worlds first**, use systemd rather than
reinventing it, and add no dependencies that aren't in Ubuntu's archive. The full working plan,
with the reasoning behind each item, is in [docs/design/roadmap.md](../design/roadmap.md).

## Next up

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

See [What's New](Whats-New.md). The latest: **1.20** adds Purpur, Quilt, NeoForge and Forge servers and modpacks from Modrinth or a CurseForge server pack; **1.19** changes a Java server's type in place (Vanilla, Paper, Fabric) with a backup and undo, and rolls back updates; **1.18** adds per-server permissions for helpers and SFTP for FileZilla or WinSCP; **1.17.2** adds Advanced options to New server (builds, Java, memory, limits, startup flags, world and settings); **1.17.1** explains why a server crashed and offers the fix; **1.17** adds performance charts, suggestions and memory and CPU limits; **1.16.14** adds the Port forwarding page, and **1.16.13** moves big worlds with seconds of downtime.
