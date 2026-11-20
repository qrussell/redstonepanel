# RedstonePanel roadmap

Status: planning · Written after 1.5.0 (17 Nov 2026), updated for 1.14.0, with clusters (1.15), moving servers (1.16) and performance (1.17) planned · Author: Quentin Russell

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

## What already exists (1.14.0)

Java (vanilla, Paper, Fabric, any release, the right Java per server) and Bedrock servers
as systemd units with `Restart=on-failure`, templates,
`server.properties` and permissions editors, player roles, live chat over RCON or screen,
zip export and .zip/.tar.gz/.tar import, a file manager, drag-and-drop mods, plugins and Bedrock add-ons, a Modrinth and Hangar catalog with bundles and updates, Java installed per server on demand, a host dashboard, Phantom LAN proxies for Bedrock servers, restoring removed servers, scheduled backups with restore, webhook notifications, scheduled restarts, dark/light themes, an activity log, HTTPS modes,
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

### 1.10.0: edit files *(Nov 2026)*

7. **File manager.** A **Files** tab (admins only): browse, edit UTF-8 text up to 1 MB,
   upload (512 MB), download, rename, new folder, delete.
   - Rather than `realpath` and a prefix check, which a symlink swapped in between check and
     use defeats, every path is opened one component at a time from the server folder with
     `openat(O_NOFOLLOW)`. Every symlink is refused, not only those pointing outside; the
     list shows them and their targets. Files with other hard links, FIFOs and devices aren't
     opened. Folder deletes use `shutil.rmtree(dir_fd=)`.
   - The panel writes as root but through those directory fds, then `fchown`s new files to
     the folder's owner (`minecraft`); replacing a file keeps its mode. Writes are atomic
     (temp file + rename).
   - Saving checks the file's mtime against when it was opened, so a change the game made
     meanwhile isn't overwritten silently.
   - Each change is logged with the admin's name.

### 1.11.0: host dashboard *(Nov 2026)*

8. **A dashboard where the empty screen is.** With no server selected, the right side of
   the panel is blank. Fill it with the state of the machine and everything the panel relies
   on, so "is anything wrong?" has an answer without opening each server or SSHing in.

   Why here: it's read-only (low risk), every admin sees it on every visit, and it builds the
   sampler that the resource graphs need, so it takes most of the work out of item 13 (1.17)
   rather than adding to the plan. It comes before 2.0 because per-server permissions and API
   tokens change who can see what; a dashboard that already exists only needs a filter then.

   **What it shows** (refreshed every 5–10 s while the page is visible):
   - **Needs attention**, first and only when something is wrong: disk over 90 % (or under
     5 GB free), memory or swap nearly full, a failed `minecraft-*` or `redstonepanel-*`
     unit, a server crash-looping, the last backup of a server failed, a Java server whose
     Java isn't installed, a certificate expiring within 14 days, a cluster node that hasn't
     answered, a newer RedstonePanel in apt. Each links to where it's fixed.
   - **Host:** name, Ubuntu release, kernel, uptime, time zone, IP addresses, the panel's
     version and whether an update is waiting (`apt-cache policy redstonepanel`, read
     without running `apt update`).
   - **CPU:** model, cores, load average, use in % (from two `/proc/stat` samples).
   - **Memory:** used, available and swap (`/proc/meminfo`).
   - **Disk:** the file systems holding `/opt` (servers), `/var/backups/redstonepanel`
     and `/var/lib/redstonepanel`, with free space (`statvfs`). Per server: folder size and
     backups size. Folder sizes are walked in the background and cached (every 10 min), never
     during a request.
   - **Network:** receive and send rate per interface (`/proc/net/dev`).
   - **Game servers:** one row each: state, uptime, CPU %, memory (from the unit's cgroup:
     `CPUUsageNSec`, `MemoryCurrent`, `ActiveEnterTimestamp`), players online where RCON
     allows, next backup and next restart. Clicking a row opens the server.
   - **Supporting services:** `redstonepanel.service` (state, uptime, memory), the backup
     and restart timers (next run, last result), Phantom proxies, `ufw` (on, and whether each
     game port is open), installed Java runtimes and which servers use each, `screen`,
     HTTPS mode and certificate expiry, notifications (configured or not, last send error),
     cluster role and certificate expiry.
   - **Cluster (on a master):** a card per node with the same summary: reachable or last
     seen, version, CPU, memory, disk, servers online. Needs one more read-only route on the
     node allowlist (`GET /api/host`); it fits the cluster rule that a master can see and
     run game servers but never change a node's settings.

   **How:** `core/host.py` reads `/proc`, `statvfs` and `systemctl show`; no `psutil`, no
   database, no new dependency. One `GET /api/host` for signed-in users. Trusted-LAN visitors
   get the same view minus IP addresses and versions. The CPU and network samples are kept
   in memory between requests; that sampler becomes 1.17's history. Lay it out for phones
   too (see the mobile pass in Groundwork).

   **Size:** about a week. The background folder-size walk and the per-node cards are the
   fiddly parts.

   **As built:** `core/host.py` and `GET /api/host` (plus `/api/cluster/hosts` on a master);
   the dashboard is also behind a **📊 Dashboard** sidebar button, and the Cluster page shows
   each panel's CPU, memory and free disk. Timer times come from `systemctl show` run with
   `TZ=UTC` (systemd 255 ignores `--timestamp=unix` for timer properties). A server counts as
   crash-looping while it's activating after 3 or more automatic restarts, not only in the
   short `auto-restart` pause. Not done: the last notification error (the panel doesn't keep
   one yet), and the sidebar itself still isn't laid out for phones.

### 1.12.0: drop in mods and plugins *(Nov 2026)*

9. **Drag and drop, and the installed list.** Getting a mod onto a server today means the
   Files tab, the right folder, Upload, Restart. Make it one drop, and show what's installed.

   Why here: asked for after 1.11, it's what people running Paper or Fabric do every week,
   and it's small because 1.10 already does the dangerous part (writing into a server folder
   without following symlinks, owned by `minecraft`). It comes before the Modrinth browser
   (item 10) because the browser installs into the same list.

   - **Drag and drop** onto the Files tab uploads into the folder you're in: several files at
     once, a progress bar per file, the existing "replace?" question, the 512 MB limit.
   - **Jars go to the right place.** A dropped `.jar` is opened as a zip (stdlib `zipfile`,
     metadata only, never run) and sorted: `fabric.mod.json` → `mods/` on Fabric;
     `plugin.yml` / `paper-plugin.yml` → `plugins/` on Paper. A jar that can't work there is
     refused with the reason: a Forge/NeoForge mod (`META-INF/mods.toml`) anywhere, a mod on
     Paper, a plugin on Fabric, anything on vanilla. Dropping onto a server's **Mods** or
     **Plugins** list does the same.
   - **Installed list** (a **Mods** or **Plugins** section, admins): name, version, author and
     the Minecraft versions each jar declares, read from the same metadata and cached by
     mtime. A jar declaring a different Minecraft version than the server's is marked. Fabric
     mods whose required dependencies (`depends`, e.g. `fabric-api`) aren't installed are
     marked too. **Disable** renames to `.jar.disabled` (the loaders skip it); **Enable**
     renames back; **Remove** deletes. Each says to restart.
   - **Bedrock add-ons.** A dropped `.mcpack` or `.mcaddon` is unpacked (same checks as
     import: no absolute paths, no `..`, size and file-count limits) into
     `behavior_packs/` or `resource_packs/` by its `manifest.json` module type, and added to
     the active world's `world_behavior_packs.json` / `world_resource_packs.json`.

   **How:** `core/mods.py` for reading jar and pack metadata and placing files; all writes go
   through `core/files.py`. Admin-only, logged as `[MODS] alex installed …`. The browser
   upload uses `XMLHttpRequest` for progress; the endpoint is the existing streaming upload.

   **Size:** about 3 days. The Bedrock world-pack JSON is the fiddly part.

   **As built:** `core/mods.py` and `/api/servers/{name}/addons` (list, install, toggle,
   remove), admin-only. The tab is called **Mods**, **Plugins** or **Add-ons** by server type
   and is hidden on vanilla. Fabric jar-in-jar modules (`META-INF/jars/`) count as installed,
   so a mod needing `fabric-networking-api-v1` is satisfied by Fabric API. Pack folders are
   written into a hidden staging folder first and renamed into place, and are matched by
   their manifest uuid, so dropping a newer version replaces the old one. A browser drag and
   drop test installs and lists jars; a real Fabric 1.21.4 server loaded Fabric API installed
   this way and skipped a `.jar.disabled` mod.

### 1.13.0: Modrinth and Hangar browser *(Nov 2026)*

10. **Find and install mods and plugins from the panel.** Search, install, update, with the
    right build picked for the server.

    Why here: it replaces "download on your PC, then upload" for most people, and builds on
    1.12's installed list. Modrinth (Fabric mods, Paper plugins) and Hangar (Paper plugins)
    have open APIs without keys. Not CurseForge: its API needs a key, and some authors block
    third-party downloads, so a CurseForge button would often fail.

    - **Search** filtered to the server: Modrinth `/v2/search` with facets for the loader
      (`fabric`, or `paper`/`spigot`/`bukkit`), the server's Minecraft version and the project
      type; Hangar `/api/v1/projects` with platform `PAPER` and the version. Results show
      downloads, last update and a link to the project page.
    - **Install** picks the newest release build that matches loader and Minecraft version,
      downloads it from the API's file URL, checks the hash the API gives (Modrinth `sha512`,
      Hangar `sha256`), and installs required dependencies the same way (e.g. Fabric API).
    - **Updates.** The source of each installed jar (project and version id) is kept in
      `.redstonepanel-mods.json` in the server folder, so the list can show **Update
      available** and offer **Update all**. Jars added by hand are matched by hash through
      Modrinth's `version_files` lookup where possible.
    - **Before a Minecraft update** (Maintenance → Update, from 1.9), list the installed mods
      and plugins that have no build for the target version yet, so nobody updates into a
      server that won't start.
    - **Safety.** Admins only. Downloads only from `cdn.modrinth.com` and Hangar's CDN over
      HTTPS, with the upload size limit, written through `core/files.py`. Installing or
      updating several at once takes a pre-change backup first (a new backup kind), so it can
      be undone from the Backups tab. Everything is logged.

    **How:** `core/modrinth.py` and `core/hangar.py`, stdlib `urllib` with the panel's
    User-Agent and the 10-minute JSON cache from `javaservers`; no new dependency.

    **Size:** one to two weeks. Dependency resolution and matching hand-added jars are the
    fiddly parts.

    **As built:** `core/sources.py` (cached JSON, and downloads only over HTTPS from
    `cdn.modrinth.com` and `hangarcdn.papermc.io`, redirects included, hash-checked, size
    limited), `core/modrinth.py`, `core/hangar.py` and `core/catalog.py`; routes under
    `/api/servers/{name}/addons/` (`search`, `install`, `updates`, `update`, `undo`,
    `compatibility`), admin-only. Search asks Modrinth for `server_side` required or optional,
    so client-only mods aren't offered. Install takes the newest release, else beta, never
    alpha. Updates use Modrinth's `version_files/update` (one request for every jar), and
    jars added by hand are matched with `version_files`. Two changes from the plan:
    - **Undo is per update, not only from Backups.** A world backup can't undo a mod change,
      because restore puts back the world, not `mods/`. So updating renames the old jar to
      `….jar.previous` (neither loader loads it) and **Undo** puts the last batch back.
      **Update all** can still take a world backup first, as a new **pre-change** kind, for
      mods that change world data.
    - Single installs don't take a backup: they only add files, and **Remove** undoes them.

    Tested in the systemd container against the real APIs: AppleSkin installed Fabric API
    with it, a hand-added older Fabric API was recognised by hash and updated (with backup)
    and undone, a real Fabric 1.21.4 server started with the installed mods, and WorldEdit
    (Hangar) and LuckPerms (Modrinth) went into a Paper server's `plugins/`.

    Next after this (in Later): Modrinth modpacks (`.mrpack`), which reuse this downloader.

### 1.13.1: Java when it's needed *(Nov 2026)*

Asked for after 1.13: a host shouldn't carry Java versions no server uses, and the first
Java server shouldn't stop with "install this package first".

**As built:** the package suggests Java instead of recommending it, so a fresh install
has none. When creating, updating or restoring a Java server finds its Java missing, the
API answers 409 with the version and package, and an admin's browser offers to install it
and retries. `core/runtimes.py` runs apt in one background job at a time (package names
only from `openjdk-{8,11,17,21,25}-jre-headless`, `DPkg::Lock::Timeout`, an `apt update`
only when the package isn't in the lists), then rebinds every server waiting for that
Java and starts the enabled ones. The dashboard's Java row lists each Java with its
servers, what servers need, and Install / Remove; Maintenance → Java switches a server
to another installed Java that's new enough. Removing purges only the JRE package, refuses
while a server uses it or when apt would take anything else, and then purges the
dependencies that only it needed (the autoremove list before and after, nothing that was
already waiting). Binding stays the full path in each root-owned unit: no symlink in the
server folder, which the game user and the file manager can change and which a backup or
import would carry to a host where it points nowhere.

Also in 1.13.1, asked for alongside it: **Update now** on the dashboard. The upgrade can't run
inside the panel's process, because the package's postinst restarts `redstonepanel.service` and
systemd would kill apt mid-dpkg, so the panel starts `redstonepanel upgrade` as a transient
unit (`systemd-run --unit=redstonepanel-upgrade`) that writes its outcome to
`/var/lib/redstonepanel/upgrade.json`; the page polls through the restart and reloads. And
removing a server now closes its ufw port and resets its failed state; failed units with no
unit file are cleared instead of shown.

Tested in the systemd container: a fresh install had no Java; creating a 1.21.4 server got
409, installed Java 21 and created it; an imported server needing Java 25 was moved onto
it when 25 was installed; switching Java and the too-old refusal; removing 25, then the
last Java with `java-common` and `ca-certificates-java`, leaving the panel installed.

### 1.13.2: Phantom from the panel *(Nov 2026)*

Asked for after 1.13.1: install, uninstall, start, stop and check Phantom LAN proxies for
Bedrock servers, managed like add-ons. 1.11 already found Phantom units and their mistakes
(`core/proxies.py`); this makes them.

**As built:** `core/phantom.py`. The binary is jhead/phantom v0.5.4 (the last release, 2021),
downloaded through `core/sources.download` from github.com and its asset hosts only, and
checked against SHA-256 values pinned per architecture (x86_64, aarch64, armv7l), since the
project publishes none. It goes in `/usr/local/lib/redstonepanel/phantom/` because the panel's
data folder is root-only and the unit runs as `minecraft`. One unit per server,
`phantom-<server>.service`, marked `X-RedstonePanel-Server=` so only the panel's own units are
rewritten; `-server 127.0.0.1:<port> -bind_port <19300..19399>` so ufw can allow a fixed
port. Phantom binds 19132 with SO_REUSEPORT, so several share it, but a Bedrock server on 19132
doesn't: install is refused while one exists. Units made by hand that point at the server
or hold its port can be started, stopped and removed (unit file only). Removing a server
removes its Phantom; the last Phantom closes 19132 unless a server uses it.

Tested in the systemd container with the real download: two Phantoms on 19132 with proxy
ports 19300 and 19301 under the minecraft user, ufw rules opened and closed, a server on
19132 blocking install, a hand-made loop unit removed, purge removing a server's Phantom,
and non-admins refused.

### 1.14.0: a catalog of mods and plugins *(Nov 2026)*

Asked for after 1.13.2: make Find mods / Find plugins feel like a catalog of popular mods and
plugins, not just a search box.

**As built:** the browse view opens on **bundles** and a **starter set** grouped as
Performance, Admin tools, For players and Libraries. Both are hand-picked Modrinth slugs in
`core/featured.py` (20 for Fabric, 14 for Paper), looked up in one `/projects` request and
cached for an hour, and checked against the server's loader and Minecraft version from each
project's own lists: no build for the loader hides it, not updated for the version yet marks
it. Cards carry Installed / Update available / Not for this version badges. Search gains
category and sort filters on both sites (Modrinth facets and `index`, Hangar `category` and
`sort`), validated against fixed lists. Clicking a project shows its details and exactly what
Install would do (the build, its file and size, the dependencies it adds), resolved but not
downloaded. A bundle installs what's missing with dependencies, after an optional world
backup (taken only if something will be installed), and one that fails doesn't stop the
rest. Paper has spark built in, so it isn't offered there.

Tested against the live APIs in the systemd container: Fabric "Server essentials" installed
six jars (Fabric API and Fabric Language Kotlin as dependencies) after a backup, and running it
again installed nothing and took no backup; on Paper, "Build protection" brought WorldEdit for
WorldGuard.

### 1.15: manage every node from the master

11. **Clusters phase 2 ([fleet.md](fleet.md) §13).** Asked for after 1.13.1: with only the
    master reachable, you can't create a server on a node or use most of a node server's
    tabs, and a panel on the internet has only self-signed or cluster certificates.

    **Every server, every tab, from the master.** The master's server list shows each node's
    servers under the node's name, and opening one shows the same tabs as a local server:
    Overview, Live Chat, Configuration, Maintenance, Backups, World & Maps, Files, Mods,
    Java, Phantom.
    - A proxy route, `/api/cluster/nodes/{id}/proxy/{path}`, forwards the request with the
      node's token. The node checks it against a phase-2 allowlist: the server routes,
      deploy and remove, templates, Java runtimes and Phantom, but still never users,
      panel settings or cluster membership.
    - `app.js` gets one `api(path, opts)` helper that knows which panel a server lives on,
      replacing its direct `fetch` calls.
    - Both panels log the action with the master user's name (`[CLUSTER] alex@master: …`).
    - Master admins can do everything; master users only what their role allows (per-server
      grants come in 2.0).

    **Create a server on a node.** New Server gets a **Where** choice. A master template is
    pushed to the node first, then deployed there. Ports are checked on the node, and a
    missing Java is installed there (1.13.1).

    **Certificates for a panel on the internet.**
    - The master can use a real certificate for browsers: a certbot certificate or one you
      upload (`https_mode: "custom"`, refused in a cluster today). The browser only ever
      talks to the master, so nodes can stay on private addresses with cluster-CA
      certificates and never be exposed.
    - A node that does have a public name and certificate is checked with the system's
      trust store instead of the cluster CA (`verify: "system"`, fleet.md §6).
    - Node certificates renew automatically 30 days before expiry, and the cluster backup
      runs nightly.

    **Size:** two to three weeks. The proxy, the allowlist and making every tab panel-aware
    are most of it.

### 1.16: move a server to another node

12. **Move or copy a server between cluster members** (fleet.md phase 3). Asked for after
    1.13.1 together with 1.15, which it needs.

    - **Move:** stop the server and export it the way a backup does (save-off over RCON,
      `save hold` on Bedrock). The archive is streamed from node A through the master into
      an import on node B, in chunks with a SHA-256 checked at the end and no 1 MB limit.
    - **On B:** keep the server's name and port if they're free there (else pick another and
      say so), install its Java if needed, bring along its backup schedule, restart schedule,
      mods tracking and Phantom, start it, and wait until it's running.
    - **On A:** the folder stays as a removed server (1.6), restorable, until you confirm the
      move. Nothing is deleted on A before B is up.
    - **Copy:** the same without removing it from A, under a new name, for a test copy of a
      world.
    - **Shorter downtime for big worlds:** an optional pre-copy while the server keeps
      running, then a final copy of only the files that changed after it stops.
    - **Templates** can be copied between members too.
    - Not automatic: there's still nothing to balance (see Not planned). An admin chooses
      when and where.

    **Size:** one to two weeks; the streaming import and the rollback on failure are the
    careful parts.

### 1.17: server performance and tuning

13. **How well each server runs, its history, and what to change.** 1.11 answers "is the
    machine all right?". This answers "is the game lagging, since when, and why?", and
    suggests fixes based on what was measured, not a fixed checklist.

    Why here: asked for after 1.12 (from a "performance dashboard" proposal, reviewed below).
    It's mostly read-only and builds on 1.11's sampler. It comes after 1.14 because several
    suggestions are "install this mod or plugin" (Lithium, FastAsyncWorldEdit, spark), which
    1.14 makes one click, and after 1.16 because one fix is moving a
    server to a less busy node. It takes the resource graphs out of 2.0, because TPS without
    history only says how things are right now.

    **What 1.11 already shows** from the proposal: CPU, memory and disk use, network rates,
    players online, uptime, per-server memory, folder size, warning colours, and webhook
    messages when a server stops or crashes (1.8). Those stay as they are.

    **New on each server's Overview (a Performance card) and the dashboard's server rows:**
    - **Tick health.** Paper: RCON `tps` (1, 5, 15 min) and `mspt`. Vanilla and Fabric
      1.20.3+: RCON `tick query` (average and percentile ms per tick); TPS is
      `min(20, 1000 / ms)`. Older Java releases and Bedrock have no command for it, so the card
      says "not available" rather than guessing. Polled every 30 s per running server with
      RCON, alongside the player list.
    - **Memory.** The heap limit (`-Xmx` from the unit) next to what the process uses (the
      unit's cgroup). Heap actually in use needs the JDK's `jcmd`, which the headless JRE
      doesn't have, so it isn't shown.
    - **World size** (the world folder, in 1.11's background size walk), **mods/plugins
      loaded** (1.12's list), and **connections** to the game port (`/proc/net/tcp` and
      `tcp6`, counted per port, no new tool).
    - **Address and "updated N s ago"** in the server header, so a stale view is obvious.

    **History (moved from 2.0).** 1.11's sampler writes every 60 s to a small fixed-size ring
    file per server and for the host under `/var/lib/redstonepanel/metrics/`: CPU, memory,
    players, TPS/MSPT; host CPU, memory, disk, network. 24 hours at 1-minute resolution,
    7 days at 15 minutes. Charts are drawn as inline SVG by `app.js` (no chart library) on the
    Performance card and the dashboard. Live numbers keep refreshing every 10 s only while the
    page is visible; history is written whether anyone is watching or not. No `psutil`, no
    database.

    **Memory setting.** Every Java server is made with `-Xms1G -Xmx2G` and nothing in the
    panel changes it. Add **Memory** to Maintenance (admins): rewrite the unit's `-Xms`/`-Xmx`,
    refuse a total across running servers that leaves under 1 GB for the system, and restart
    on request.

    **Suggestions.** Each one is shown only when its condition is measured, says what was
    seen, and links to where it's changed (the `server.properties` editor, Memory, the Mods
    tab, Backups). Nothing is applied automatically, because every change here trades world
    behaviour for speed and only the owner can make that call.
    - TPS under 18 for 10 minutes: lower `simulation-distance` first, then `view-distance`
      (Bedrock: `tick-distance`). Profile with **spark** (a Paper plugin and Fabric mod).
    - Fabric with no performance mods: **Lithium** (and **FerriteCore** when memory is
      tight). Vanilla with low TPS: a new Paper or Fabric server; switching an existing one
      stays out of scope (1.9).
    - Paper with WorldEdit: **FastAsyncWorldEdit** does the same edits off the main thread.
    - Process memory at its `-Xmx` with frequent restarts: raise Memory, if the host has
      room. All `-Xmx` together above the host's RAM minus 1 GB, or swap in use: lower one or
      move a server to another node.
    - A big world with no world border (`worldborder get`): set one, and pre-generate with
      **Chunky** so exploring doesn't generate chunks during play.
    - No backup schedule; `logs/` over 1 GB; worlds on a rotating disk
      (`/sys/block/*/queue/rotational`).

    **Alerts.** The 1.8 webhook gains "TPS under 15 for 5 minutes" and "disk over 95 %",
    each at most once an hour per server. Off by default.

    **Left out of the proposal, and why.**
    - A separate Node.js or Python monitor with its own installer: the panel already runs
      as that process, so this is a feature of it.
    - Fixed advice such as "set `-Xmx6G`", "upgrade to 8–16 GB" or "dual-core": the right
      heap depends on the host and the other servers, so suggestions come from measurements.
      Minecraft's main loop is one thread; core speed matters, core count much less.
    - Raising `view-distance` to 10–12 adds load rather than removing it.
      `sync-chunk-writes=true` and `max-tick-time=60000` are already the defaults;
      `entity-broadcast-radius-per-player` isn't a real setting (the nearest is
      `entity-broadcast-range-percentage`); `use-native-transport` is on by default.
    - Citizens → CustomNPCs and Vault → "SimpleVault": CustomNPCs is a Forge mod that
      can't run on Paper, and Vault is already tiny. Only replacements that exist for the
      server's loader are suggested.
    - Setting `server-ip`, CDNs for resource packs, "clustering for large player bases"
      (see Not planned: load balancing), and version control for config files (Backups
      already keep them).

    **How:** `core/perf.py` (RCON parsing, ring files, suggestions) next to `core/host.py`;
    `GET /api/servers/{name}/perf` and `GET /api/host/history`. The RCON command runs as a
    read-only query, never changing state.

    **Size:** one to two weeks. The ring file format and the SVG charts are the fiddly parts.

### 2.0: run it for other people

14. **Per-server permissions.** Today a `user` can do everything to every server. Add
    per-server grants (view, console/chat, power, files, mods & plugins, settings) for `user`
    accounts. Admins
    stay admins (they're effectively root anyway). The dashboard then lists only the servers
    a user may view.
15. **API tokens.** The API already exists (FastAPI, with `/docs`) and the cluster code already
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
- Modrinth modpacks (`.mrpack`), on top of 1.13's downloader. Not CurseForge: it needs an API
  key and some packs can't be downloaded automatically.
- Adopting a node over SSH (fleet.md phase 4) and finding panels on the LAN.
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
