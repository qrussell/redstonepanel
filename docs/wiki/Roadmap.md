# Roadmap

What's planned after **1.16.8**. Version numbers are targets, not promises, and your feedback
decides the order: tell us in the
[discussions](https://github.com/qrussell/redstonepanel/discussions) what you'd use first.

The guiding rules stay the same: **protect people's worlds first**, use systemd rather than
reinventing it, and add no dependencies that aren't in Ubuntu's archive. The full working plan,
with the reasoning behind each item, is in [docs/design/roadmap.md](../design/roadmap.md).

## Next up

### 1.16.9: newer Ubuntu, Debian and ARM64

- **Ubuntu 25.10 and 26.04 LTS**, as well as 24.04 and 25.04.
- **Debian 12 and 13**.
- **ARM64** machines such as a Raspberry Pi, for Java servers. Bedrock is only made for
  x86-64, so it's hidden there.

### 1.16.10: a panel that works on a phone

- **Every page fits a phone:** the sidebar becomes a ☰ menu, tables become cards, and Start,
  Restart and Stop are big buttons at the top.
- **Live Chat and the console** work with the on-screen keyboard.
- **Install it as an app** from your phone's browser (over HTTPS), so it opens like any other
  app.

### 1.16.11: backups off the machine, without a cluster

- **Copy each backup to an SSH server or S3-compatible storage** (Backblaze B2, Wasabi,
  Cloudflare R2, MinIO, AWS S3), checked as it's copied.
- **Keep backups by age** ("14 days") as well as by count, with separate settings for copies.
- **Restore from a copy**, or bring a server back on a new machine straight from backup storage.

### 1.16.12: Bedrock that keeps up, and update alerts

- **Bedrock servers update themselves** if you turn it on: players are warned, a backup is
  made, and a failed update is rolled back.
- **Update available** on the dashboard when a new Minecraft release, Paper build, Fabric
  loader or Bedrock version is out, with an optional webhook message.

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
- **Hard memory and CPU limits** per server, so one runaway server can't starve the rest.
- **Alerts** for low TPS or a nearly full disk (off by default).

### 1.17.1: why did it crash?

- **A Last crash card** with the error lines highlighted, and the cause in plain words: a
  missing dependency, a mod for the wrong version, the wrong Java, a port in use, out of memory.
- **The likely mod or plugin named**, with **Turn off and restart** or **Undo the last mod
  update**.

### 1.18: helpers with their own access, and SFTP

- **Per-server permissions** for user accounts: view, console, power, players, files, mods,
  settings and backups, with Moderator and Co-owner presets.
- **SFTP** for FileZilla, WinSCP or any SFTP client, signed in with your panel account (or an
  SSH key) and limited to the servers you may use.

### 1.19: change a server's type or version

- **Switch types in place:** Vanilla → Paper or Fabric, and every other way round. The world,
  settings, players, port and schedules stay; Paper's separate Nether and End folders are
  moved back when leaving Paper.
- **Mods and plugins follow where they can:** the same Modrinth project's build for the new type
  is offered, and the old folder is kept, switched off, for going back.
- **A clear warning** before dropping mods that add blocks or dimensions, which would vanish
  from the world.
- **Always a backup first**, a preview of everything that will happen, and an automatic
  rollback if the server doesn't start.
- **Roll back the last update**, together with the server software it ran before. Backups
  record the type and version that made them, so an older server is never handed a newer world.

### 1.20: Forge, NeoForge, Quilt, Purpur and modpacks

- **New server types:** NeoForge, Forge and Quilt for mods, Purpur for plugins, and the catalog
  filtered for each.
- **Modrinth modpacks:** pick one from Modrinth or upload a `.mrpack`; only server-side files
  are installed, and pack updates keep the world.
- **CurseForge server packs:** download the pack's server files from CurseForge and upload
  them. A CurseForge browser in the panel isn't planned yet, because CurseForge's API needs a
  key and some authors block third-party downloads.

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

See [What's New](Whats-New.md). The latest: **1.16.8** keeps backup copies on another cluster
member; **1.16.7** adds two-factor sign-in, and **1.16.6** stops the console covering the page.
