# RedstonePanel roadmap

Status: planning · Written after 1.5.0 (17 Nov 2026), updated for 1.16.7 (21 Nov 2026), with the release order revised the same day: moving servers between cluster members, a first-run setup guide, kick and ban with the redstone branding, a folding server list and two-factor sign-in (1.16.7) shipped; backups on another node (1.16.8), newer Ubuntu, Debian and ARM64 (1.16.9), a phone-friendly panel (1.16.10), backups off the machine without a cluster (1.16.11), Bedrock auto-updates and update alerts (1.16.12), pre-copy and LAN discovery (1.16.x), performance (1.17), crash explanations (1.17.1), per-server permissions and SFTP (1.18), changing a server's type or version (1.19), Forge, NeoForge, Quilt, Purpur and modpacks (1.20) and API tokens (2.0) next, with proposed additions below · Author: Quentin Russell

The public summary of this plan is [docs/wiki/Roadmap.md](../wiki/Roadmap.md); keep the two in
step when either changes.

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

## What already exists (1.16.7)

Java (vanilla, Paper, Fabric, any release, the right Java per server) and Bedrock servers
as systemd units with `Restart=on-failure`, templates,
`server.properties` and permissions editors, player roles, live chat over RCON or screen,
zip export and .zip/.tar.gz/.tar import, a file manager, drag-and-drop mods, plugins and Bedrock add-ons, a Modrinth and Hangar catalog with bundles and updates, Java installed per server on demand, a host dashboard, Phantom LAN proxies for Bedrock servers, restoring removed servers, scheduled backups with restore, webhook notifications, scheduled restarts, dark/light themes, an activity log, HTTPS modes,
trusted-LAN sign-in, two-factor sign-in with recovery codes, clusters (managing every node's servers from the master, with real
certificates for a master on the internet, and moving or copying servers between members), a first-run setup guide, kick, ban and unban, the redstone logo and theme, a server list that folds by node and edition, the signed apt repository, and Bedrock connection
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
   sampler that the resource graphs need, so it takes most of the work out of item 21 (1.17)
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
   too (see 1.16.10).

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

### 1.14.1: NetherNet-aware firewall and Phantom *(Nov 2026)*

Found on a live host: a Bedrock server switched to `transport=nethernet` listens on TCP
server-port (signaling), UDP `server-udp-ports` (gameplay) and UDP 7551 (LAN discovery), but
the panel only ever opened UDP server-port, and the Phantom card offered a RakNet proxy that
can't carry NetherNet players.

**As built:** `manager.firewall_rules(edition, props)` derives the rules from
server.properties; `list_servers` reports them. Deploy, import and restore open them; saving
properties (form, raw editor, template) opens what's new and closes what's gone; removal closes
only rules no remaining server needs (7551 is shared). The dashboard checks each rule (port
ranges included) and names the missing ones with the ufw command. Every ufw change clears the
host cache so the dashboard is current at once. Phantom refuses NetherNet servers with the
reason, and its port bookkeeping treats them as holding their UDP ports and 7551, not 19132.
Existing servers aren't changed on upgrade; the dashboard lists what's missing.

### 1.14.2: who joined, who was turned away *(Nov 2026)*

Asked for after the whitelist was turned on on every server: see who has been joining and who
the whitelist keeps out, without SSH. The panel had no whitelist editing at all (Player Roles
only sets ops/permissions).

**As built:** `core/playerlog.py` parses join, leave and refused-login lines (vanilla, Paper and
Fabric prefixes; Bedrock "Player connected/disconnected") from the journal and the server's log
files, keeps names and UUIDs/XUIDs (never the address in the line) as JSON lines per server,
90 days, and scans every 30 s from a thread run.py starts. The first scan reads rotated logs too.
`core/whitelist.py` edits whitelist.json / allowlist.json, through RCON or screen when the server
runs. Configuration gains **Whitelist & Access**; Needs attention lists players turned away in the
last 24 h; webhooks gain Player joined / Player turned away (off by default). Bedrock doesn't log
refused joins, so those are Java only.

### 1.15.0: manage every node from the master *(Nov 2026)*

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
      grants come in 1.18).

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

    **As built:** the proxy streams requests and answers both ways (uploads and downloads too),
    never passes on the cluster's own routes, and turns a node's 401 into a 502 so the browser
    isn't signed out. Nodes refuse purging from the master; uninstalling keeps the world. The
    master sends the user's role; master admins are admins on nodes, everyone else a user. A
    master-side route copies a template to a node, and Save as Template on a node's server makes
    a master template. Housekeeping runs hourly on the master: renewal 30 days ahead (a failure is
    shown on the node's row) and the nightly backup after 3:00, keeping 7, with the passphrase
    stored on the master. 1.14 nodes keep phase 1 and are asked to update.

### 1.16.0: move a server to another node *(Nov 2026)*

12. **Move or copy a server between cluster members** (fleet.md phase 3). Asked for after
    1.13.1 together with 1.15, which it needs.

    **Shipped in 1.16.0** as designed in [move.md](move.md): move and copy (to another member or
    the same one), the streamed archive with its SHA-256, names and ports kept when free, Java
    installed on the target, schedules and Phantom carried over, rollback on failure, the old
    copy kept as a removed server until an admin deletes it, and templates copied both ways.
    **Still to do (1.16.x):** the pre-copy for big worlds below, and fleet.md's LAN discovery.

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

### 1.16.1: a first-run setup guide *(Nov 2026)*

13. **Get a new owner to a safe setup without reading the manual.** Added to the plan after
    1.16.0 from a comparison with other panels: installing is already one command, but the
    first visit lands on an empty dashboard, and backups, HTTPS and notifications stay off
    unless the owner finds Panel Settings and each server's Backups tab. Beginners on other
    panels get a guided start; here that gap is the cheapest one to close.

    Why here: it's small, it touches no server or cluster code, and it's what every new
    owner sees first, so it ships as a point release ahead of the rest of 1.16.x and
    performance (1.17).

    - **A Setup card on the dashboard** (admins only), shown while any step is unfinished:
      1. **Create your first server**: opens New Server.
      2. **Back up your worlds**: a schedule on each server, with a suggested daily one;
         opens that server's Backups tab.
      3. **Choose how the panel is reached**: HTTP on a trusted LAN, self-signed HTTPS or
         your own certificate; opens Panel Settings → Connection, and says plainly what HTTP
         means for passwords.
      4. **Get told when something breaks**: a Discord or webhook address and a test
         message; opens Panel Settings → Notifications.
    - **Every step is an existing dialog.** The guide only links and explains; it adds no
      second way to do anything.
    - **Steps are ticked from the panel's real state**, not from clicks: a server exists,
      every server has a backup schedule, HTTPS is on (or HTTP was chosen on purpose), a
      webhook is saved. So a step done elsewhere (the CLI, another admin) ticks itself, and
      a panel upgraded from 1.16.0 shows only what's still missing.
    - **Dismissable**, and reopened from Panel Settings. The dismissal is a panel setting, so
      it holds for every admin.
    - **On a cluster** a node's HTTPS step is done by joining, and the master's card counts
      servers on every node.
    - **Firewall:** when `ufw` isn't installed or is inactive, say so on step 1, since the
      panel opens game ports only through it.

    **How:** `GET /api/settings/setup` returns each step's state from existing settings and
    server data, and `PUT` stores the two answers the guide keeps (dismissed, keep HTTP) in
    `data/setup.json`; the card and its links are in `app.js`.

    **Size:** a few days. Wording, and the screenshots for the manual, are most of it.

### 1.16.2: kick and ban from the panel *(Nov 2026)*

14. **Kick, ban and unban without typing commands.** Added after 1.16.0 from the comparison
    with other panels: every other panel has buttons for these, and here they need `/kick` and
    `/ban` typed in Live Chat. 1.14.2's Whitelist & Access already lists who joined and who was
    turned away, so the buttons go there.

    - **Kick** (with an optional reason) from the online players list and the access log.
      Java over RCON, Bedrock through its `screen` session.
    - **Ban and unban** on Java: over RCON while the server runs (`ban`, `pardon`), and by
      editing `banned-players.json` while it's stopped, so the list is the same either way.
      A **Banned** list beside the whitelist, with the reason, who banned and when.
    - **Bedrock has no ban list**, so it gets kick only, and the panel says to use the
      allowlist to keep someone out.
    - Bans by name and UUID only, never by IP address, in line with 1.14.2's access log.
    - Each action goes to the activity log with who did it. Users and admins both may kick and
      ban (it's server management, like the console); trusted-LAN visitors too.

    **How:** `core/whitelist.py` grows `banned-players.json` handling next to the whitelist;
    `POST /api/servers/{name}/players/kick|ban|unban`, allowed for the cluster master.

    **Size:** a few days.

    **Shipped in 1.16.2**, in `core/moderation.py`. A running Java server without RCON is
    refused rather than having its ban file edited, since it would overwrite the change. The
    release also brought the RedstonePanel logo and a stone-and-redstone theme, with Redstone
    Red as the default accent.

### 1.16.3: a server list that folds *(Nov 2026)*

    Not planned ahead: added after 1.16.2 because the sidebar grows long on a cluster. Every
    heading in the server list (this machine's Java and Bedrock Edition, each node) folds with
    a click and, folded, shows how many of its servers are online. A node's servers get their
    own Java and Bedrock headings, like this machine's. What's folded is kept in the browser
    (`rp_collapsedGroups` in localStorage), so nothing is stored on the server. The planned
    point releases below each moved up one number.

### 1.16.4: Bedrock settings fix *(Nov 2026)*

    Not planned ahead: a fix. New Bedrock servers had lost every `server.properties` setting
    except the ports, because the deploy replaced the file from the Bedrock download instead
    of setting the ports in it. The planned point releases below each moved up one number.

### 1.16.5: Bedrock gameplay ports *(Nov 2026)*

    Not planned ahead: a fix for 1.16.4. Keeping Mojang's `server.properties` also kept its
    `server-udp-ports=19132`, so a second Bedrock server's gameplay port clashed with the
    first's. Each Bedrock server now gets its own, on the same number as its TCP port, and
    `server-udp-ports` counts when the panel picks ports. The planned point releases below each
    moved up one number again.

### 1.16.6: a console that makes room *(Nov 2026)*

    Not planned ahead: the console slid up over the bottom of the window, sidebar included, and
    new users didn't know to close it. While it's open, the page beside the sidebar now ends
    above it, it starts at the sidebar's edge, it's at most 35% of the window's height, and the
    button that opened it is scrolled back into view. The planned point releases below each
    moved up one number again.

### 1.16.7: two-factor sign-in *(released 21 Nov 2026)*

15. **A second step at sign-in for admin accounts.** An admin is effectively root, and a
    password alone is a thin wall once the panel is reachable from outside the home network.
    Other panels offer it; this closes the most-noticed security gap.

    - **TOTP** (the six-digit codes from any authenticator app), set up under **🔑 Password**
      for panel accounts and Linux (PAM) accounts alike: a QR code and the key to type, then
      a code to confirm. Python's standard library does TOTP with `hmac`; the QR code is drawn
      in the page by a small script shipped with the panel, so nothing new is installed.
    - **Ten recovery codes** shown once at setup, each usable once.
    - **Panel Settings → Sign-in → Require two-factor for admins** (off by default): an admin
      without it is sent to set it up after signing in.
    - **Recovery from the server:** `sudo redstonepanel reset-2fa NAME`, as with
      `reset-security`.
    - Trusted-LAN sign-in is unchanged (it never gives admin), and cluster node tokens don't
      use it (they're machine credentials, not people).
    - Failed codes count towards the existing sign-in rate limit.

    **How:** secrets in `data/totp.json` (0600, root only), checked in `core/auth.py` between
    the password and issuing the session.

    **Size:** about a week, most of it the sign-in flow and recovery.

    **As built:** `core/twofactor.py` (RFC 6238 TOTP with `hmac`, SHA-1, 30-second steps, one
    step of drift either way, a used code can't be replayed) and `core/qr.py`, a small QR
    encoder (byte mode, level M, versions 1-10). The QR code is drawn on the server as an SVG
    rather than by a script in the page: same result, nothing new installed, and the encoder
    is unit-tested in Python. Accounts are keyed `panel:NAME` / `linux:NAME` in
    `data/totp.json` (0600); recovery codes are stored as SHA-256 only. Sign-in: the right
    password on an account with 2FA returns `code_needed` and a five-minute ticket in its own
    cookie (`rp_2fa`, path `/api/auth/login`, signed with a key derived for that purpose so it
    can never pass as a session); `POST /api/auth/login/code` exchanges it for the session.
    The password step no longer resets the failure count when a code is still needed, so
    re-entering the password can't be used to keep guessing codes. Sessions record that they
    passed the second step, so turning 2FA on signs out every other session (this browser
    gets a fresh cookie). **Require two-factor for admins** is enforced in the API, not just
    the page: until they set it up, such an admin reaches only `/api/auth/me`,
    `/api/auth/2fa*` and sign-out. The panel refuses to turn the setting on for an admin who
    hasn't set up 2FA themselves, and while it's on admins can't turn their own off.
    Additions to the plan: **Reset two-factor** for panel accounts on the Users screen, new
    recovery codes from **🔑 Password**, a **2FA** badge on Users, `redstonepanel status`
    lists who has it, and the 🔑 Password button now shows for Linux accounts too. A damaged
    `totp.json` asks every account for a code (fails closed); `reset-2fa` moves it aside.
    Tested: 27 new unit tests (RFC 6238 vectors, replay, recovery codes, the rate limit,
    enforcement, admin reset), every QR version and mask decoded by an independent decoder
    (zxing-cpp), and the whole flow in Chromium against a running panel (setup, sign-in with
    a code, a wrong code, a recovery code, a second admin forced through setup), including
    decoding the QR code from the screenshot.

### 1.16.8: backups on another cluster node

16. **A copy of every backup off the machine it protects.** A backup on the same disk doesn't
    survive losing the disk, and the guiding rule is to protect worlds first. Clusters make the
    cheap version nearly free: the members already trust each other over HTTPS, so copying to
    another member needs no new credentials or services.

    - On a server's **Backups** tab: **Also keep a copy on** another cluster member (on a
      master, any node; on a node, the master or another node through it).
    - After each scheduled or manual backup, the archive is streamed to that member with a
      SHA-256 check, the same way 1.16.0 moves servers, and stored under
      `/var/backups/redstonepanel/remote/<panel>/<server>/` there.
    - **Same retention** as the server's own backups. The copies are listed on the Backups tab
      and can be downloaded or restored from, even after the original machine is gone.
    - A failed copy is a **Needs attention** item and a webhook event (`backup_copy_failed`).
    - Standalone panels don't get it; SFTP/rsync and S3-compatible storage stay in the
      proposals below for them.

    **How:** a copy step at the end of `run_backup`, sent over the cluster client. Nodes
    accept copies only from cluster members.

    **Size:** about a week.

### 1.16.9: newer Ubuntu, Debian and ARM64

17. **Ubuntu 25.10 and 26.04 LTS, Debian 12/13, and ARM64 machines such as a Raspberry Pi, for
    Java servers.** Ubuntu 26.04 is the LTS most new machines will run, and 25.04 reached the
    end of its support in January 2026, yet `install.sh` refuses everything but 24.04 and 25.04.
    Many home servers are also Debian or a Pi, which it refuses too. The package is already
    `Architecture: all` and its dependencies are in Ubuntu's and Debian's archives, so most of
    the work is testing.

    - `install.sh` accepts Ubuntu 24.04, 25.04, 25.10 and 26.04, and Debian 12 and 13.
    - **Newer Ubuntu releases:** check that their Python, FastAPI and Starlette still meet the
      package's minimum versions and that the panel runs on them, and that their OpenJDK
      packages cover the Java versions Minecraft needs.
    - **Ubuntu 25.04** stays accepted for existing installs but leaves the test matrix, since it
      no longer gets updates.
    - **Java:** the panel offers the OpenJDK versions each release packages, and says so when
      a Minecraft version needs a Java the release doesn't have.
    - **Bedrock is hidden on ARM64**, because Mojang only ships it for x86-64; Phantom too.
    - The install tests run on Ubuntu 24.04, 25.10 and 26.04 and Debian 12 and 13, and on ARM64
      for one Ubuntu and one Debian release.
    - The README, installation page and FAQ list the supported systems.

    **Size:** about a week, mostly the test matrix.

### 1.16.10: a panel that works on a phone

18. **Every page usable on a phone, and the panel installable as an app.** Restarting a
    crashed server or answering "is it up?" from a phone is one of the commonest reasons to
    open a panel, and today the fixed sidebar takes most of a phone's width: the dashboard,
    server pages and the setup guide are squeezed into a strip beside it. Other panels work on
    phones; this closes that gap. It takes over the "mobile pass" from Groundwork.

    Why here: after the 1.16.2–1.16.9 point releases it touches every page, so it goes after
    them, and before 1.17 so the performance charts are designed for small screens from the
    start.

    - **Sidebar becomes a menu** below the `md` breakpoint: a top bar with the panel name, the
      current server and a ☰ button that slides the sidebar in, closing when a server is picked.
    - **Server pages:** Start, Restart and Stop stay at the top as large buttons; the tabs
      scroll sideways instead of wrapping; dialogs fill the screen.
    - **Tables become cards** on narrow screens: the dashboard's server table, backups, the
      file manager, mods and players.
    - **Live Chat and the console** keep the input above the on-screen keyboard and stop the
      page from zooming on focus (16 px inputs).
    - **Touch targets** of at least 44 px for every button, and no actions that only appear on
      hover.
    - **An installable app (PWA):** a web app manifest and icons, so phones offer "Install" or
      "Add to Home Screen" and the panel opens without browser bars. Browsers only allow
      installing over HTTPS, so on plain HTTP it stays a home-screen bookmark, and the setup
      guide's HTTPS step says so. No offline mode: a panel that can't reach its server has
      nothing true to show.
    - The screenshot demo also takes phone-sized pictures (390 x 844) for the manual.

    **How:** Tailwind's responsive classes in `index.html` and the markup `app.js` builds, a
    `static/manifest.webmanifest` and icons, and a minimal service worker (required for
    installing; it caches nothing). No framework, no new dependency.

    **Size:** one to two weeks; the tables and every dialog are most of it.

### 1.16.11: backups off the machine, without a cluster

19. **Copy every backup to an SSH server or S3-compatible storage.** 1.16.8 only helps people
    with a second machine in a cluster, and most panels are a single machine. A backup on the
    same disk doesn't survive the disk, and protecting worlds is the first guiding rule, so
    this goes ahead of everything that only adds convenience.

    Why here: right after 1.16.8, which adds the copy step and the remote listing this reuses,
    and before 1.17, which is mostly read-only. It moves "copying backups off the machine" and
    "retention by age" up from Later.

    - **Panel Settings → Backup storage** (admins): add one or more targets.
      - **SSH/rsync:** host, port, user and folder. The panel makes an SSH key for itself and
        shows the public key to add to the target's `authorized_keys`, and checks the host key
        on first use, as `ssh` does. Copies run `rsync -e ssh`, which can resume.
      - **S3-compatible** (Backblaze B2, Wasabi, Cloudflare R2, MinIO, AWS S3): endpoint,
        bucket, prefix, access key and secret. Uploads are signed with AWS Signature v4 using
        Python's `hmac` and `hashlib`, in parts for archives over 100 MB, so nothing new is
        installed.
      - **Test** writes, lists and deletes a small file, and says which step failed.
    - On a server's **Backups** tab, **Also keep a copy on** lists cluster members (1.16.8) and
      these targets. Each copy is checked against the archive's SHA-256.
    - **Retention by age** as well as by count ("keep 14 days"), for local backups and copies
      alike, with separate settings for the copy (for example 7 here, 30 off the machine).
    - **Restore from a copy:** the copies are listed on the Backups tab; restoring downloads
      the archive, checks it and restores as usual. A copy can also be imported as a new server
      on a fresh machine (Import → **From backup storage**), which is the "the machine died"
      case.
    - A failed copy is a **Needs attention** item and a `backup_copy_failed` webhook event, as
      in 1.16.8. Copies run after the backup unit finishes, so a slow upload never holds the
      server's save off.
    - Secrets are stored in `/var/lib/redstonepanel/backup-targets.json` (0600, root only) and
      never sent to the browser after they're saved.

    **How:** `core/backup_targets.py` (rsync and S3 clients, retention), called from the copy
    step that 1.16.8 adds to `run_backup`. `rsync` and `openssh-client` are in Ubuntu's and
    Debian's archives; they become Recommends. Tested against a real SSH host, MinIO in the
    test container, and one hosted S3 provider.

    **Size:** about a week; S3 multipart upload and the restore-on-a-new-machine path are most
    of it.

### 1.16.12: Bedrock that keeps up, and update alerts

20. **Bedrock servers that update themselves (opt-in), and a notice when any server has an
    update.** Bedrock clients update automatically and then can't join an older server, so
    owners update by hand within hours of every release. Java owners don't hear about new
    Paper or Fabric builds or Minecraft releases unless they go looking.

    - **Bedrock, per server:** **Update automatically** (off by default). A daily systemd timer
      (`redstonepanel-bedrock-update@`) checks the download-links API. When there's a new
      version it warns players in game, waits five minutes, backs up, updates and restarts, and
      posts a webhook event. A failed update puts the old server back from the "Before update"
      backup and shows under **Needs attention**.
    - **Update alerts for every server:** the same daily check looks for a newer Bedrock
      release, Minecraft release, Paper build or Fabric loader for each server, and shows
      **Update available** on the dashboard row and the server header, with the existing
      **Update** one click away. It also sends one webhook message per new version, never
      repeated. Snapshots and pre-releases are ignored.
    - Works on cluster nodes: the master shows each node's alerts, and a node runs its own timer.

    **How:** `core/updates.py`, reusing the version lookups in `core/javaservers.py` and the
    Bedrock downloader. Results are cached in `/var/lib/redstonepanel/updates.json` so the
    dashboard never waits on the network.

    **Size:** a few days.

### 1.17: server performance and tuning

21. **How well each server runs, its history, and what to change.** 1.11 answers "is the
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

    **Resource limits (moved up from the proposals).** Beside Memory, optional **Hard memory
    limit** and **CPU share** per server, written as systemd `MemoryMax=` and `CPUQuota=` in the
    unit, so one runaway server can't starve the others. The memory limit defaults to the heap
    plus a margin for Java's own use, and the card shows when a server is being held back.

    **Suggestions.** Each one is shown only when its condition is measured, says what was
    seen, and links to where it's changed (the `server.properties` editor, Memory, the Mods
    tab, Backups). Nothing is applied automatically, because every change here trades world
    behaviour for speed and only the owner can make that call.
    - TPS under 18 for 10 minutes: lower `simulation-distance` first, then `view-distance`
      (Bedrock: `tick-distance`). Profile with **spark** (a Paper plugin and Fabric mod).
    - Fabric with no performance mods: **Lithium** (and **FerriteCore** when memory is
      tight). Vanilla with low TPS: switch it to Paper or Fabric (1.19; until then, a new
      server).
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

### 1.17.1: why did it crash?

22. **When a server crashes or won't start, say why in plain words, and offer the fix.** Today
    the owner gets a "crashed" webhook and then reads `crash-reports/` and `logs/latest.log`
    by hand. Mod and plugin conflicts, missing dependencies and the wrong Java are the
    commonest reasons a server won't come up, and the panel already knows the mods, their
    dependencies and the Java each version needs.

    - **A "Last crash" card** on the server's Overview after a crash or a failed start: the
      newest crash report or the end of the log, with the error lines highlighted and the
      rest folded away.
    - **Known causes, named:** a missing dependency ("Sodium requires Fabric API"), a mod for
      the wrong loader or Minecraft version, the wrong Java (`UnsupportedClassVersionError`),
      the port already in use (`FAILED TO BIND TO PORT`), out of memory, an unaccepted EULA,
      and a corrupt chunk or `level.dat`.
    - **The likely mod or plugin:** frames and "Suspected Mods" lines in the crash report are
      matched against the installed list, and the card names it, with **Turn off and restart**
      (renamed to `.disabled`, as 1.12 already does) and **Undo the last mod update** when that
      is the most recent change.
    - The crash webhook gains one line with the cause, and **Needs attention** links to the card.
    - Nothing is changed without a click, and a backup runs first when the fix touches the world.

    **How:** `core/crashes.py` (pattern table and matching), run from the existing
    `ExecStopPost` notification. Tested against real crash reports for each cause, collected
    on purpose in the systemd container.

    **Size:** about a week; the pattern table is most of it and grows with what people report.

### 1.18: helpers with their own access, and SFTP

23. **Give a helper access to only their servers, and let people use FileZilla or WinSCP.**
    Today any `user` account can do everything to every server, which stops owners from
    adding a helper. SFTP is the most-noticed table-stakes gap: Pterodactyl, AMP and
    PufferPanel all have it, and it's how people move big worlds and edit many configs with the
    tools they know. The two go together, because an SFTP login must only reach the servers
    its account may use.

    Why here: moved up from 2.0, ahead of type switching, because it changes who can safely use
    the panel at all, while the rest are conveniences for the owner. It comes after 1.16.7
    (two-factor) because helpers' accounts make the sign-in matter more.

    **Per-server permissions.**
    - **Users → (account) → Servers:** for each server, tick **View**, **Console & chat**,
      **Start/stop/restart**, **Players** (whitelist, kick, ban), **Files**, **Mods & plugins**,
      **Settings** and **Backups** (make and download; restore stays admin-only). Presets:
      **Moderator** (view, console, players) and **Co-owner** (everything but deleting).
    - Admins stay admins: they're effectively root anyway. A user with no grants sees an
      empty dashboard with a note to ask an admin.
    - The dashboard, server list, Needs attention and webhooks list only what the user may
      view, and every API route checks the grant, with a test per route as the cluster's
      allowlist already has.
    - On a cluster, grants for node servers are kept on the master, as the master's users are.

    **SFTP.**
    - A built-in SFTP server on port 2022 (configurable), run as its own systemd unit
      `redstonepanel-sftp.service`. Sign in as `account.server` with the panel password or an
      SSH key added under **🔑 Password → SSH keys**. Accounts with two-factor on must use a key,
      since SFTP clients can't ask for a code.
    - Each session sees only that server's folder, and files are read and written through the
      same confined code as the file manager (`openat(O_NOFOLLOW)` per component, written as
      `minecraft`). Needs the **Files** grant; admins can reach every server.
    - The server page shows the address, port and username, with a **Copy** button and a
      `sftp://` link that FileZilla and WinSCP open.
    - Uploads and deletes are logged with who did them (`[SFTP] alex uploaded …`), and failed
      sign-ins count towards the existing rate limit.
    - Cluster nodes run their own SFTP server for their servers and check sign-ins with the
      master over the cluster link.

    **How:** grants in `/var/lib/redstonepanel/grants.json`, checked by one dependency in
    `core/auth.py`. SFTP uses `python3-paramiko`, which is in Ubuntu's and Debian's archives, as
    a Recommends; without it the SFTP setting explains how to install it. `core/sftp.py` maps
    SFTP operations onto `core/files.py`.

    **Size:** two to three weeks: one for the permissions and their per-route tests, the rest
    for SFTP and its security tests (path escapes, symlinks, the sign-in limit).

### 1.19: change a server's type or version

24. **Turn a Vanilla server into Paper or Fabric (and any other way round), and roll back an
    update, without making a new server.** Today a server keeps the type it was created with
    (1.9 left switching out), and **Update Server** only goes up. The usual dead end: someone
    starts on Vanilla, then wants plugins or mods, and has to make a new server and copy the
    world by hand. The second one: an update breaks a mod, and there's no clean way back.

    Why here: it's self-contained (Maintenance tab, `core/convert.py`) and needs nothing from
    1.16, 1.17 or 1.18, so it can be pulled forward if it's asked for first. It comes before
    1.20, which adds more types to the two families it defines. 1.17's "Vanilla with low TPS"
    suggestion then becomes "switch to Paper" instead of "make a new server".

    **What changes between types.** Every type shares `server.properties`, `ops.json`, the
    whitelist and ban lists, player data, advancements, statistics, data packs
    (`world/datapacks`) and the region files. Two things differ:

    - **World layout.** Vanilla and Fabric keep the Nether and End inside the world folder
      (`world/DIM-1`, `world/DIM1`). Paper keeps them in their own folders
      (`world_nether/DIM-1`, `world_the_end/DIM1`); it moves a vanilla world into that layout by
      itself on its first start, but never back.
    - **Add-ons.** Vanilla has none, Fabric loads mods from `mods/`, Paper loads plugins from
      `plugins/`. A mod can't run as a plugin or the other way round, though many Modrinth
      projects publish both (LuckPerms, Chunky, spark, Geyser, Floodgate).

    Later loaders (1.20) slot into the same two families: Quilt, Forge and NeoForge use the
    vanilla layout and mods; Purpur uses Paper's layout and plugins.

    **Every combination, at the same Minecraft version:**

    | From → to | World | Mods and plugins | Risk |
    | --- | --- | --- | --- |
    | Vanilla → Paper | Paper splits off the Nether and End on its first start; the panel doesn't touch the world. | None to carry. Offer the Paper bundles afterwards. | Low. |
    | Vanilla → Fabric | Nothing moves. | None to carry. Offer Fabric API and the performance bundle. | Low. |
    | Paper → Vanilla | The panel moves `world_nether/DIM-1` to `world/DIM-1` and `world_the_end/DIM1` to `world/DIM1`, and refuses if either target already exists. | `plugins/` is renamed `plugins.off`, kept for switching back. | Low to medium: what plugins kept (regions, homes, economy, logs) is unused; blocks they placed stay. |
    | Paper → Fabric | Moved back as above. | `plugins.off`; for each plugin whose Modrinth project has a Fabric build, offer that build. Its settings and data aren't carried over. | Medium, as above. |
    | Fabric → Vanilla | Nothing moves. | `mods/` is renamed `mods.off`. | **High if any mod adds content:** its blocks, items and entities disappear and its dimensions (`world/dimensions/<mod>/`) can't be reached. Server-side mods (Lithium, FerriteCore, Krypton) are safe to drop. |
    | Fabric → Paper | Paper splits the dimensions on first start. | `mods.off`; offer the Paper/Bukkit build of the same Modrinth projects. | High with content mods, as above. |

    Content mods are recognised from what the panel already knows: each mod's Modrinth
    categories and its client/server sides (from 1.13's lookup), the namespaces in the
    world's `level.dat` data-pack list, and folders under `world/dimensions/`. When any are
    found, the preview names them and the change needs the server's name typed to confirm.

    **Versions.**

    - **Up:** **Update Server** as today. Changing type and version together is allowed upwards
      only, and happens in that order (type at the current version, then the update) with one
      backup. If the new type has no build for the current version (Paper is often days behind a
      new release; Fabric starts at 1.14), the preview lists the versions both support.
    - **Down:** a world opened by a newer version can't safely go back, so there is no general
      downgrade. Instead, **Roll back the last update** restores the "Before update" backup
      *together with* the server software it was taken with. That needs the groundwork below.
      To play an older version with a fresh world, make a new server (already possible).
    - **Bedrock** has one type and always updates to the newest release, so none of this
      applies; rolling back a Bedrock update would need older downloads, which Mojang doesn't
      keep listed.
    - **Java ↔ Bedrock** isn't a type change: the world formats are different, and converters
      are lossy (see Not planned). For mixed players, the **Bedrock players** bundle (Geyser,
      Floodgate) already lets Bedrock clients join a Java server.

    **How a change runs** (Maintenance → **Change type or version**, admins; also from the
    master for a node's server, since it's under `/api/servers/minecraft*`):

    1. **Preview, changing nothing:** the target build, the Java it needs (offered for install as
       usual), the world moves, what happens to each mod or plugin (turned off, equivalent
       available, needs a library such as Fabric API), content-mod warnings, and whether there's
       room on disk for the backup.
    2. **Download first:** the new `server.jar` goes into a staging folder, as Update already
       does, so a failed download changes nothing.
    3. **Back up, always:** a "Before type change" backup (not optional, unlike mod updates).
    4. **Stop, swap and move:** replace `server.jar` and `.redstonepanel-server.json`, keep the
       old ones as `server.jar.previous` and `.redstonepanel-server.previous.json`, move world
       folders when leaving Paper, rename the add-on folder, install the chosen equivalents,
       rewrite the unit if the Java changes. Port, properties, players, backup and restart
       schedules and templates stay as they are.
    5. **Start and check:** wait for the server's "Done (" line (5 minutes). If it doesn't come up,
       put everything back automatically (old jar and info, world from the backup, add-on folder
       renamed back), start the old server, and show the log lines that explain why.
    6. **Undo:** **Undo type change** stays available until the next change or update, and does
       the same as step 5's automatic rollback.

    Every step goes in the activity log (`[UPDATE] alex changed minecraft-java from Vanilla 1.21.4
    to Paper 1.21.4 (build 232)`), and a "Server type changed" notification can be sent.

    **Groundwork it needs (useful on its own):**

    - **Backups record what made them.** Today backups leave out `server.jar` and every
      `.redstonepanel-*` file, so a backup doesn't say which type and version wrote its world.
      Add a small manifest to each archive (type, Minecraft version, build, panel version) and
      show it on the Backups tab. Restoring a world made by a *newer* version than the server
      runs is then refused with an explanation, instead of corrupting it.
    - **Update backs up first**, as mod updates already do (ticked by default), as a "Before
      update" backup that Roll back uses.

    **How:** `core/convert.py` (plan, layout moves, rollback) next to `core/javaservers.py`;
    `GET /api/servers/{name}/convert?to=paper&minecraft=1.21.4` (the preview),
    `POST /api/servers/{name}/convert`, `POST /api/servers/{name}/convert/undo`,
    `POST /api/servers/{name}/update/rollback`. Tests: layout moves and conflict refusals on a
    temp folder, content-mod detection, the manifest; an end-to-end run in the systemd container
    taking one real world Vanilla → Paper → Vanilla → Fabric → Paper and checking that the seed,
    region files and player data come through, plus a failed start that must roll back.

    **Size:** one to two weeks. The world moves are simple; the rollback on a failed start and
    the content-mod checks are the careful parts.

### 1.20: Forge, NeoForge, Quilt, Purpur and modpacks

25. **Run the server types and modpacks people actually ask for.** Forge, NeoForge and
    modpacks are the commonest reason a Java owner can't use the panel at all. 1.19 sorted
    every type into two families (vanilla layout with mods, Paper layout with plugins), so new
    types now slot into creating, updating, switching and the catalog.

    - **New types when creating a server:** **NeoForge**, **Forge** and **Quilt** (mods, vanilla
      layout) and **Purpur** (plugins, Paper layout). Purpur and Quilt are a `server.jar` like
      Paper and Fabric. Forge and NeoForge run their installer as `minecraft` in a staging
      folder, and the unit starts them with the `@libraries/…/unix_args.txt` file the installer
      writes, with `-Xms`/`-Xmx` from 1.17's Memory setting rather than `user_jvm_args.txt`.
    - **The catalog** filters Modrinth by the new loaders, and Hangar and Modrinth plugins work
      on Purpur as on Paper. 1.12's drop-in check accepts Forge and NeoForge mods on those
      types instead of refusing them.
    - **Modrinth modpacks:** **New server → From a modpack** searches Modrinth's modpacks, or
      takes an uploaded `.mrpack`. The panel reads `modrinth.index.json`, installs the loader
      and Minecraft version it names, downloads only server-side files (checked against the
      pack's SHA-512 hashes, from the hosts the format allows), and copies `overrides/` and
      `server-overrides/`. Updating to a newer pack version backs up first and keeps the world.
    - **CurseForge server packs, by upload:** most CurseForge modpacks publish a "server pack"
      zip. **New server → From a modpack → Upload server pack** unpacks it like an import, works
      out the loader and version from the pack, installs that loader itself (it never runs the
      pack's own start scripts) and keeps the pack's `mods/` and `config/`. The owner downloads
      the pack from CurseForge, so no API key is needed and authors' download choices are
      respected.
    - **Java per type:** Forge and NeoForge need the same Java as their Minecraft version, which
      the panel already installs.

    **Not included, and why.** A CurseForge browser in the panel: CurseForge's API needs a key
    issued to the project, its terms limit what third-party apps may do, and authors can
    block third-party downloads of their files, so many installs would fail half-way. Add it
    only if people ask, with each admin pasting their own key and blocked files linking to
    CurseForge's download page. SpigotMC and similar sites have no file hashes and many paid or
    external-link resources, so they stay drag-and-drop.

    **How:** new type handlers in `core/javaservers.py` (Forge/NeoForge installer runs, Purpur's
    and Quilt's download APIs), `core/modpacks.py` for `.mrpack` and server packs, built on
    1.13's downloader. Tested end to end with a real pack of each kind, and type switching
    (1.19) tested for Fabric ↔ Quilt and Paper ↔ Purpur.

    **Size:** two to three weeks; Forge's installer and the modpack update path are the slow
    parts.

### 2.0: an API for scripts and bots

26. **API tokens.** The API already exists (FastAPI, with `/docs`) and the cluster code already
    has tokens limited by a route allowlist. Reuse that: admin-created tokens with a named set
    of allowed routes and, after 1.18, the servers they may touch. Shown once, revocable, and
    logged under the token's name. Per-server permissions moved to 1.18; if nothing else joins
    this release, it can ship as a 1.20.x point release instead.

## Groundwork (do alongside, before promoting)

- **Tests for `core/manager.py`.** Deploy, import, remove, restore and port allocation run as
  root against `/opt`; cover them with a temp `SERVERS_ROOT` and a stubbed `systemctl`, and in
  the systemd test container.
- **Security review** of sign-in, import (archive unpacking) and anything that builds paths.
  Do it before 1.16.11 and 1.18, which add stored credentials for backup targets and an SFTP
  server; other panels had path-traversal and XSS advisories in 2026.
- ~~**Mobile pass**~~ on the dashboard, server list and console: *scheduled as 1.16.10
  (item 18 above).*
- ~~**README screenshots**~~ *(done with 1.15.0: a screenshot tour in the README, retaken by
  `packaging/screenshots/demo.sh`)*. A short GIF is still to do.
- ~~**`install.sh`**~~ *(done with 1.15.0: `install.sh` in the repository root, served from
  raw.githubusercontent.com; it checks the key's fingerprint)*.
- ~~**A wiki**~~ *(done with 1.15.0: `docs/wiki/`, published to the GitHub wiki by
  `.github/workflows/wiki.yml`)*. Each release adds its highlights to `Whats-New.md`.

## Proposed additions (suggested after 1.15, not scheduled)

A review of what people choosing a panel look for, against what RedstonePanel has. Each
fits the guiding rules. Suggested slots are only suggestions; nothing here moves the plan above
until it's agreed.

**Protect worlds (first, per the guiding rules)**

- ~~**Off-machine backups.**~~ *Scheduled: to another cluster node as 1.16.8 (item 16), to
  SSH/rsync or S3-compatible storage as 1.16.11 (item 19).*
- ~~**Automatic Bedrock updates.**~~ *Scheduled as 1.16.12 (item 20 above).*

**Security**

- ~~**Two-factor sign-in (TOTP).**~~ *Scheduled as 1.16.7 (item 15 above).*
- **Reach the master panel through a Cloudflare Tunnel** (opt-in). Today a panel on the
  internet needs a port opened or forwarded to 8443 and a certificate (1.15.0). With
  `cloudflared`, the machine makes an outbound connection to Cloudflare, and the panel is
  served at a hostname like `panel.example.com` with Cloudflare's certificate. No inbound port
  is opened and the home IP address stays hidden. Suggested after 1.16.7, so admins have a
  second sign-in step before the panel is reachable from anywhere.
  - **Panel Settings → Connection → Cloudflare Tunnel:** paste the tunnel token from the
    Cloudflare dashboard. The panel writes a token-based `cloudflared` service pointing at
    `http://127.0.0.1:8080`, starts it, and shows the tunnel's state and public hostname on
    the dashboard. Turn off stops and removes the service. CLI: `redstonepanel tunnel on|off|status`.
  - **cloudflared isn't in Ubuntu's archive**, so it stays optional (a Suggests, not a
    Depends). The panel tells the admin how to install it from Cloudflare's apt repository
    and detects it; it doesn't download binaries itself.
  - **Security:** tunnel visitors must never count as the trusted LAN. The current check
    already refuses them, because `cloudflared` adds `X-Forwarded-For` and the Host is the
    public name; keep a test for it. Everything through the tunnel arrives from 127.0.0.1, so
    the sign-in rate limit and the activity log must use `Cf-Connecting-IP`, but only for
    requests from the local `cloudflared`. Suggest Cloudflare Access (an email or SSO check
    in front of the panel) in the setting's help text.
  - **The master only.** Nodes keep talking to the master over the cluster's own HTTPS.
    Players' game traffic doesn't go through the tunnel: a free Cloudflare Tunnel carries
    HTTP to the panel, not Minecraft's TCP/UDP, so game ports are unchanged.
  - *Size: about a week, mostly the service handling, the IP handling and the manual.*

**Getting started**

- ~~**First-run setup guide.**~~ *Scheduled as 1.16.1 (item 13 above).*

**Day-to-day management**

- ~~**Player actions:** kick, ban and unban with a ban list per server~~ *(shipped in
  1.16.2, item 14 above)*. Still proposed: on a cluster master, apply a whitelist change to
  several servers at once.
- **Scheduled commands and announcements:** a list of commands per server on a schedule
  (`redstonepanel-command@.timer`), for messages, `save-all` or event toggles.
- ~~**Version update alerts.**~~ *Scheduled with automatic Bedrock updates as 1.16.12 (item 20).*
- **World tools:** upload a world zip into a stopped server (with a backup first), start a new
  world with a seed, and pre-generate with Chunky from a button (it's already in the catalog).

**More servers and machines**

- ~~**Modrinth modpacks (`.mrpack`)** and **Forge/NeoForge**~~ *Scheduled as 1.20 (item 25),
  with Quilt, Purpur and CurseForge server packs by upload.*
- ~~**Per-server resource limits.**~~ *Scheduled with 1.17's Memory setting (item 21).*
- ~~**SFTP access.**~~ *Not in the first review; scheduled with per-server permissions as 1.18
  (item 23).*
- ~~**Ubuntu 25.10 and 26.04, Debian 12/13 and ARM64** for Java servers.~~ *Scheduled as
  1.16.9 (item 17 above).*

**For players**

- **A read-only status page** (opt-in, no sign-in): each chosen server's name, address, state,
  version and players online. Nothing else from the panel is exposed.
- **Discord two-way chat bridge** on top of the 1.8 webhooks, using a bot token stored like the
  webhook URL.

**Docs and reach**

- A short GIF for the README, and a GitHub Pages site built from `docs/wiki/`
  if the wiki proves hard to find.

## Later (only when asked for)

- A CurseForge browser in the panel, with each admin's own API key (see 1.20 for why it isn't
  planned yet; CurseForge server packs can be uploaded from 1.20).
- Adopting a node over SSH (fleet.md phase 4) and finding panels on the LAN.
- Velocity proxy support, for people running a network across nodes.
- Translations, once a contributor offers one.

## Not planned

| Idea | Why not |
| --- | --- |
| Docker mode | Contradicts the reason the panel exists; two backends to maintain. |
| SPA framework rewrite | The UI is already one page with JavaScript; weeks of work for no visible gain. |
| Load balancing / auto-migration | A Minecraft server is one stateful process; there's nothing to balance. "Move server to node" covers the real need. |
| Converting worlds between Java and Bedrock | Different world formats; converters lose blocks, entities and redstone behaviour, and a bad conversion looks fine until players find the damage. Geyser and Floodgate (one click in the catalog) let Bedrock players join Java servers instead. |
| Downgrading a world to an older Minecraft version | Minecraft doesn't support it and worlds break in subtle ways. 1.19's "Roll back the last update" restores the backup from before the update, with its server software. |
| White-label / multi-tenant hosting | A business, not a feature: tenant isolation, billing and support for other people's customers. |
| Founders badges, premium tiers, bug bounties | An MIT project with no paid tier and no budget. |
