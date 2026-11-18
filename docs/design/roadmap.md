# RedstonePanel roadmap

Status: planning · Written after 1.5.0 (17 Nov 2026), updated for 1.6.0 · Author: Quentin Russell

This is the working plan for what to build next and in what order. It replaces an earlier
generic draft. Each item says why it matters, how it fits the panel's design (Ubuntu package,
systemd units, no Docker, no database), and roughly how big it is. Version numbers are
targets, not promises.

## Guiding rules

- **Protect people's worlds first.** Anything that prevents data loss comes before anything
  that adds convenience.
- **Use systemd, don't reinvent it.** Schedules are timers, crash handling is `Restart=` and
  `OnFailure=`, resource numbers come from the unit's cgroup. Work keeps happening when the
  panel itself is stopped.
- **The panel runs as root.** Every feature that touches files or runs commands is a security
  feature. Confine it to the server's own folder and do the work as the `minecraft` user.
- **No new hard dependencies** unless they're in Ubuntu's archive.
- **One developer.** One major feature per minor release. Build a feature once someone asks
  for it, not because a competitor has it.

## What already exists (1.6.0)

Java and Bedrock servers as systemd units with `Restart=on-failure`, templates,
`server.properties` and permissions editors, player roles, live chat over RCON or screen,
zip export and .zip/.tar.gz/.tar import, restoring removed servers, dark/light themes, an activity log, HTTPS modes,
trusted-LAN sign-in, clusters (phase 1), the signed apt repository, and Bedrock connection
diagnostics. The Bedrock download already uses the official download-links API, with the
minecraft.net page only as a fallback.

## Release plan

### 1.6.0: restore removed servers *(released 18 Nov 2026)*

"Uninstall (Keep World)" deleted the systemd unit but left `/opt/minecraft-*`, and nothing in
the panel could bring it back. The panel now lists server folders that have no unit, and can
restore one: a new unit, a free port if the old one was taken, and the server binary downloaded
again only if it's missing. Uninstall now keeps the binary, so a restore runs the same game
version as before.

### 1.7: don't lose worlds

1. **Scheduled backups with retention.** *(in progress)* The single most valuable missing feature.
   - A `redstonepanel-backup@<server>.timer` / `.service` pair per server, written by the panel.
     Backups run even when the panel is down.
   - Consistent copies: Java `save-off` → `save-all flush` (RCON) → archive → `save-on`;
     Bedrock `save hold` → poll `save query` → copy the listed files → `save resume` through
     the screen session.
   - Stored in `/var/backups/redstonepanel/<server>/` (outside the server folder, so purging a
     server doesn't delete its backups); keep the last N and/or N days.
   - A Backups tab: schedule, list, download, restore over the current world (stop, swap,
     start, keeping the replaced world as one more backup).
2. **`install.sh`.** About 20 lines: add the qrussell-repo key and source, `apt install
   redstonepanel`. Hosted on GitHub Pages. Uses the repo you already publish, so it doesn't
   need a panel release.

### 1.8: know what's happening

3. **Discord (and generic) webhook notifications.** Started, stopped, crashed, backup done or
   failed. "Crashed" comes from an `OnFailure=` unit, so it fires without the panel. The
   webhook URL is a secret: stored in panel state, never shown back in full.
4. **Scheduled restarts.** Same timer mechanism as backups, with an in-game warning
   (`say Restarting in 5 minutes`) before the restart.
5. **Console colouring.** Highlight `ERROR` / `WARN` / `INFO` in the console the panel already
   polls. Front-end only.

### 1.9: more than vanilla

6. **Paper and Fabric.** The most-asked-for gap: the panel only installs Mojang's vanilla jar.
   - Paper: the PaperMC downloads API (latest build for a Minecraft version).
   - Fabric: the Fabric meta server-launcher jar (one file, same `java -jar` unit).
   - A version picker (latest by default, or a chosen release) for vanilla too.
   - Java versions: Minecraft 1.20.5+ needs Java 21, older versions need 17 or 8. Detect the
     installed JREs (`/usr/lib/jvm`) and write the right `java` path into each unit. Say
     clearly which `openjdk-*` package to install when the one needed is missing.

### 1.10: edit files

7. **File manager.** Browse, edit text files, upload, download, delete.
   - Every path goes through `realpath` and must stay inside the server's folder; symlinks
     pointing outside are refused.
   - Writes happen as the `minecraft` user, with size limits.
   - Admin-only to begin with (see per-server permissions below).
   - Text editing only; binary files are download/upload.

### 2.0: run it for other people

8. **Resource graphs.** CPU and memory per server from the unit's cgroup
   (`systemctl show -p CPUUsageNSec,MemoryCurrent`), disk use of the server folder, player
   count over RCON/query. Sampled every 30–60 s into a small on-disk ring buffer per server;
   24-hour charts. No `psutil`, no database.
9. **Per-server permissions.** Today a `user` can do everything to every server. Add
    per-server grants (view, console/chat, power, files, settings) for `user` accounts. Admins
    stay admins (they're effectively root anyway).
10. **API tokens.** The API already exists (FastAPI, with `/docs`) and the cluster code already
    has tokens limited by a route allowlist. Reuse that: admin-created tokens with a named set
    of allowed routes, shown once, revocable.

## Groundwork (do alongside, before promoting)

- **Tests for `core/manager.py`.** Deploy, import, remove, restore and port allocation run as
  root against `/opt`; cover them with a temp `SERVERS_ROOT` and a stubbed `systemctl`, and in
  the systemd test container.
- **Tighten who can delete.** Any signed-in `user`, and anonymous trusted-LAN visitors, can
  currently uninstall or purge a server. Purge should need an admin; uninstall probably too.
- **Security review** of sign-in, import (archive unpacking) and anything that builds paths.
- **Mobile pass** on the dashboard, server list and console (there are very few responsive
  classes today).
- **README screenshots and a short GIF.**

## Later (only when asked for)

- Forge and NeoForge (installer jar, `run.sh`, `user_jvm_args.txt`).
- Modrinth modpacks (`.mrpack`). Not CurseForge: it needs an API key and some packs can't be
  downloaded automatically.
- Plugin/mod install from Modrinth (and Hangar for Paper plugins).
- **Move a server to another node** (export + import through the cluster), and clusters
  phase 2 (see [fleet.md](fleet.md)).
- Velocity proxy support, for people running a network across nodes.
- Translations, once a contributor offers one.

## Not planned

| Idea | Why not |
| --- | --- |
| Docker mode | Contradicts the reason the panel exists; two backends to maintain. |
| SPA framework rewrite | The UI is already one page with JavaScript; weeks of work for no visible gain. |
| Load balancing / auto-migration | A Minecraft server is one stateful process; there's nothing to balance. "Move server to node" covers the real need. |
| White-label / multi-tenant hosting | A business, not a feature: tenant isolation, billing and support for other people's customers. |
| Founders badges, premium tiers, bug bounties | An MIT project with no paid tier and no budget. |
