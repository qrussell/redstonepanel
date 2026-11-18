# RedstonePanel roadmap

Status: planning · Written after 1.5.0 (17 Nov 2026), updated for 1.9.0 · Author: Quentin Russell

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

## What already exists (1.9.0)

Java (vanilla, Paper, Fabric, any release, the right Java per server) and Bedrock servers
as systemd units with `Restart=on-failure`, templates,
`server.properties` and permissions editors, player roles, live chat over RCON or screen,
zip export and .zip/.tar.gz/.tar import, restoring removed servers, scheduled backups with restore, webhook notifications, scheduled restarts, dark/light themes, an activity log, HTTPS modes,
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

### 1.7.0: scheduled backups *(Nov 2026)*

A `redstonepanel-backup@<server>.timer` per server, so backups run while the panel is down.
Running servers are copied consistently (Java `save-off` / `save-all flush` / `save-on` over
RCON, Bedrock `save hold` / `save query` / `save resume`). Archives go to
`/var/backups/redstonepanel/<server>/`, outside the server folder, and scheduled ones are pruned
to the last N. The Backups tab schedules, lists, downloads and restores; a restore keeps the
replaced world as one more backup.

Testing the release candidate on a real host found that Bedrock 1.26 keeps its world in
`worlds/level` and panel-made servers have no `level-name`; worth remembering for anything
else that looks for a Bedrock world.

### 1.8.0: know what's happening *(Nov 2026)*

3. **Discord (and generic) webhook notifications.** Started, stopped, crashed, scheduled
   restart, backup done or failed. Done with a systemd drop-in shipped in the package
   (`/usr/lib/systemd/system/minecraft-.service.d/redstonepanel.conf`) rather than
   `OnFailure=`. Its `ExecStopPost` runs on every stop and sees `$SERVICE_RESULT`, so it tells
   a crash from a normal stop, covers servers made before 1.8 without rewriting their units,
   and fires without the panel. It also sets `SuccessExitStatus=143`: Java exits 143
   when stopped, which systemd counted as a failure. Testing found that a crashed Bedrock
   server was never restarted: `screen` exits 0 when `bedrock_server` dies, so
   `Restart=on-failure` didn't fire. The hook now treats an unrequested stop without Bedrock's
   "Quit correctly" as a crash and starts the server again after 20 s (`systemd-run`). A stop is
   "requested" when systemd has a job queued on the unit, which also covers Bedrock's `screen`
   exiting 1 on every `systemctl stop`. The webhook URL is stored root-only and
   never shown back in full; crash loops are limited to one message per 5 minutes.
4. **Scheduled restarts.** Same timer mechanism as backups
   (`redstonepanel-restart@<server>.timer`), firing the warning time early so the restart
   happens at the time shown. Warns at N minutes, 1 minute and 10 seconds; skips stopped
   servers; waits for a running backup.
5. **Console colouring.** Errors, warnings, backups/restarts, sign-ins/settings and the
   panel's own commands each get a colour. Front-end only.

### 1.8.1: only admins delete *(Nov 2026)*

Uninstall and purge needed no more than the `user` role, which trusted-LAN visitors get
without signing in. Both are admin-only now (API and UI), and the log names who did it.

### 1.9.0: more than vanilla *(Nov 2026)*

6. **Paper and Fabric.** New Java servers can be vanilla, Paper or Fabric, on a chosen
   Minecraft release (newest by default); Update can move a server to a newer release, never
   an older one.
   - Paper: the Fill API v3 (`fill.papermc.io`), newest STABLE build, SHA-256 checked. It
     asks clients for a User-Agent naming the software.
   - Fabric: the meta server-launcher jar for the newest stable loader and installer.
   - Every jar is `server.jar`, so units, backups, export and import didn't change. What it
     is goes in `.redstonepanel-server.json`; older servers are recognised from the jar.
   - Java: Mojang's per-version data (`javaVersion.majorVersion`) is the requirement for all
     three loaders (26.x: 25, 1.20.5+: 21, 1.18+: 17, 1.17: 16, older: 8; Ubuntu has no Java 16, so 1.17 gets 17). The unit runs the matching JRE
     from `/usr/lib/jvm`, or the closest newer one; deploy and update stop before downloading
     and name the `openjdk-*` package when none is new enough.
   - Not done: switching an existing server between loaders, and installing plugins or mods
     (see Later).

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
- **Security review** of sign-in, import (archive unpacking) and anything that builds paths.
- **Mobile pass** on the dashboard, server list and console (there are very few responsive
  classes today).
- **README screenshots and a short GIF.**
- **`install.sh`.** About 20 lines: add the qrussell-repo key and source, `apt install
  redstonepanel`. Hosted on GitHub Pages. It uses the repository that already exists, so it
  doesn't need a panel release.

## Later (only when asked for)

- Backup retention by age ("keep 30 days") as well as by count, and copying backups off the
  machine (rsync/S3).
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
