# RedstonePanel roadmap

Status: planning · Written after 1.5.0 (17 Nov 2026), updated for 1.24.0 (26 Nov 2026), with the release order revised the same day: moving servers between cluster members, a first-run setup guide, kick and ban with the redstone branding, a folding server list, two-factor sign-in (1.16.7), backups on another node (1.16.8), newer Ubuntu, Debian, ARM64 and Raspberry Pi (1.16.9), a phone-friendly panel (1.16.10), backups off the machine without a cluster (1.16.11) Bedrock auto-updates and update alerts (1.16.12) pre-copy and LAN discovery (1.16.13) a port forwarding guide (1.16.14), performance (1.17), crash explanations (1.17.1), Advanced options on New server (1.17.2), per-server permissions and SFTP (1.18), changing a server's type or version (1.19) Forge, NeoForge, Quilt, Purpur and modpacks (1.20) the groundwork for Windows (1.21) a servers folder you choose (1.21.1), a one-click light/dark switch (1.21.2), finding servers set up outside the panel (1.21.3), quick commands in Live Chat (1.21.4), join pages (1.21.5), player packs (1.21.6) a Visual Editor for settings files (1.21.7) security fixes from code scanning (1.21.8) join pages through a proxy (1.21.9) a terminal in the panel (1.21.10) the Windows preview (1.22) the whole single machine on Windows (1.23) a port check (1.23.1) removing the panel safely (1.23.2) and mixed Linux and Windows clusters with Windows Hello sign-in (1.24) shipped; API tokens (1.25) next, with proposed additions below · Author: Quentin Russell

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
  On Windows (from 1.22) the same rule reads "use the system's own service manager and
  scheduler": Windows services, Task Scheduler and Job Objects.
- **The panel runs as root.** Every feature that touches files or runs commands is a security
  feature. Confine it to the server's own folder and do the work as the `minecraft` user.
- **No new hard dependencies** unless they're in Ubuntu's archive. On Windows, nothing the
  installer doesn't carry.
- **One developer.** One major feature per minor release. Build a feature once someone asks
  for it, not because a competitor has it.

## What already exists (1.17)

Java (vanilla, Paper, Fabric, any release, the right Java per server) and Bedrock servers
as systemd units with `Restart=on-failure`, templates,
`server.properties` and permissions editors, player roles, live chat over RCON or screen,
zip export and .zip/.tar.gz/.tar import, a file manager, drag-and-drop mods, plugins and Bedrock add-ons, a Modrinth and Hangar catalog with bundles and updates, Java installed per server on demand, a host dashboard, Phantom LAN proxies for Bedrock servers, restoring removed servers, scheduled backups with restore (and, in a cluster, a copy on another member), webhook notifications, scheduled restarts, dark/light themes, a layout for phones and an installable app (PWA), an activity log, HTTPS modes,
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

**Fixed in 1.16.11:** a server.properties with no `transport` line was treated as RakNet, so
only UDP server-port was opened. Found in the 1.16.11 off-site backup test, where an older
backup brought back with a fresh Bedrock 1.26 took players on TCP 19140 and UDP 7551. Checked
against a real BDS 1.26.66.1 in Docker: with no `transport` line it runs NetherNet (TCP
server-port as one dual-stack socket, so server-portv6 is unused, plus UDP 7551, which stays
open even with `enable-lan-visibility=false`); without `server-udp-ports` it takes gameplay
ports from the kernel's random range; it never writes keys back to server.properties. Now only
`transport=raknet` means RakNet (UDP server-port and server-portv6). Import, restore and moves
give a server without `server-udp-ports` its own server-port, as new servers get. Once after
upgrading, the panel does the same for existing Bedrock servers and opens the rules they need,
closing nothing (`/var/lib/redstonepanel/bedrock-firewall-1.16.11` records that it ran).
The panel also keeps ufw in step when server.properties changes without it: it notes the rules
it opened for each server (`firewall-rules.json`), and every two minutes and whenever the
dashboard loads it opens what a hand-edited server now needs, closes what it no longer needs,
and closes the rules of a server whose unit was deleted by hand (never a rule another server or
a Phantom still uses). A rule on a game port (7551, 19132-19999, 25565-25999) that no server
needs gets a dashboard note with the `ufw delete` command; the panel doesn't close rules it
didn't open. A rule the panel opened and an admin later deleted isn't put back; the dashboard
names it as missing.

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
    **Still to do (1.16.13):** the pre-copy for big worlds below, and fleet.md's LAN discovery.

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

### 1.16.8: backups on another cluster node *(released 21 Nov 2026)*

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

    **As built:** `core/backup_copies.py`. The plan's copy step at the end of `run_backup`
    couldn't work for nodes: a node has no credential to call the master or another node
    (node tokens only go master to node), and `run_backup` runs from a systemd timer. So, as
    with moves, **the master does the copying**: every 5 minutes, and straight after **Back up
    now** or turning copies on, it lists each configured source's backups and streams any
    missing ones source to master to holder, with Content-Length and SHA-256 checks. A
    scheduled backup's copy can therefore be up to about 5 minutes late, and nothing is copied
    while the master is down (it catches up after). Holders store copies in
    `/var/backups/redstonepanel/remote/<member id>/<server>/` (0600) with a `.sha256` sidecar
    and prune scheduled copies to the source's **Keep**; manual copies stay until deleted.
    Settings and status live only on the master (`backup-copies.json`,
    `backup-copies-status.json`), and removing a node drops its settings. Copies can be
    downloaded, deleted or **Put back** into the server's backups and restored; **Use a copy
    of another server's backup…** lists every copy in the cluster for a server whose machine
    is gone. A failure is a **Needs attention** item and a `backup_copy_failed` webhook event
    (on by default, sent once per failure streak). Cluster contract 4 adds node-token-only
    routes under `/api/cluster/held-backups` that the proxy never passes on; nodes older than
    1.16.8 get an "update it" message. Tested: 16 new unit tests and
    `packaging/test/test-backup-copies.sh`, a master and two nodes in systemd containers
    (33/33), including restoring a stopped node's world from the other node's copy.

### 1.16.9: newer Ubuntu, Debian, ARM64 and Raspberry Pi *(released 21 Nov 2026)*

17. **Ubuntu 26.04 LTS and 26.10, Debian 13, and ARM64 machines such as a Raspberry Pi, for
    Java servers.** Ubuntu 26.04 is the LTS most new machines will run, and 25.04 and 25.10
    have reached the end of their support, yet `install.sh` refuses everything but 24.04 and
    25.04. Many home servers are also Debian or a Pi, which it refuses too. The package is
    already `Architecture: all` and its dependencies are in Ubuntu's and Debian's archives, so
    most of the work is testing.

    - `install.sh` accepts Ubuntu 24.04, 26.04 and 26.10 and Debian 13 (which 64-bit Raspberry
      Pi OS reports itself as), on amd64 and arm64; Ubuntu 25.04 and 25.10 still install, with a
      note that they no longer get updates. It refuses 32-bit systems (armhf, 32-bit Pi OS) and
      Debian 12 with a message saying what to install instead.
    - **Debian 12 is left for later** (decided 21 Nov 2026): it ships Python 3.11, FastAPI 0.92
      and Starlette 0.26, below the package's minimums, and its Python has no safe tar
      extraction (`filter="data"`), which backups, imports and moves rely on. Raspberry Pi OS
      moved to Debian 13 in late 2025.
    - **Newer Ubuntu releases:** check that their Python, FastAPI and Starlette still meet the
      package's minimum versions and that the panel runs on them, and that their OpenJDK
      packages cover the Java versions Minecraft needs.
    - **Ubuntu 25.04 and 25.10** stay accepted for existing installs but leave the test matrix.
    - **Java:** the panel offers the OpenJDK versions each release packages (asked from apt:
      Debian 13 has only 21 and 25), and says so when a Minecraft version needs a Java the
      release doesn't have.
    - **Bedrock is hidden on ARM64**, because Mojang only ships it for x86-64; Phantom too.
      Creating, importing, restoring, updating or moving a Bedrock server onto an ARM64 machine
      is refused; cluster members report their machine type so the master hides Bedrock for
      ARM64 nodes.
    - **Raspberry Pi:** Pi 4 or 5 with 64-bit Pi OS; a smaller default heap (1 GB) on machines
      with 3 GB or less; the docs cover memory per Pi size, SSD over microSD, and cooling.
    - The install tests run on Ubuntu 24.04, 26.04 and 26.10 and Debian 13, and on ARM64
      for Ubuntu 26.04 and Debian 13; the unit tests also run against each system's own
      Python packages.
    - The README, installation page, user manual, FAQ and man page list the supported systems.

    **Size:** about a week, mostly the test matrix.

    **As built:** as planned. `install.sh` accepts Ubuntu 24.04, 26.04 and 26.10 and Debian 13
    on amd64 and arm64, warns on 25.04 and 25.10, and refuses Debian 12, 32-bit systems and
    anything else (`FORCE=1` still overrides). Java comes from `apt-cache pkgnames` for
    `openjdk-N-jre-headless` (cached 10 minutes, falling back to Ubuntu's list): Debian 13
    offers 21 and 25, Ubuntu 26.04 adds 26 and 26.10 adds 27 and 28. A Minecraft version
    needing a Java the system lacks gets the closest newer one with a note in the New Server
    dialog, or a clear message when nothing is new enough; the 409 for a missing Java now
    carries `major`, which also fixed installing Java 16 for Minecraft 1.17. Bedrock on ARM64
    is refused for create, import, restore, update and move/copy, with a pointer to Geyser;
    `/api/auth/me` reports `machine` and `bedrock_supported`, and cluster `hello`/join report
    each node's `machine` so the master greys Bedrock out for ARM64 nodes. New Java servers
    get `-Xms512M -Xmx1G` on machines with 3 GB or less. The cluster proxy ignores a query
    string in `raw_path` (older Starlette test clients put one there). Tested: 12 new unit
    tests, the unit suite against each system's own Python packages (new `python-system`
    GitLab job on Ubuntu 24.04, 26.04, 26.10 and Debian 13), and install tests on all four
    systems on amd64 plus Ubuntu 26.04 and Debian 13 on GitHub's ARM64 runners, including
    Paper booting on ARM Java and Bedrock refused with nothing left behind.

### 1.16.10: a panel that works on a phone *(released 21 Nov 2026)*

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

    **As built:** below `md` the sidebar is a slide-in menu under a top bar
    (`ServerApp.openNav/closeNav`, `.rp-sidebar` in `input.css`), and closes when something in
    it is picked. The dashboard's server table, backups (and backup copies from 1.16.8), the
    file manager and the access log turn into cards through one CSS rule (`table.rp-cards`,
    labels from `data-label`). The PWA keeps the existing `manifest.json` (adding `id`, `scope`
    and `description`) rather than a new `.webmanifest`, and `static/sw.js` caches nothing,
    only replacing the browser's error page when the panel can't be reached; it registers only
    in a secure context. Added on request: **📱 Open on your phone**, a QR code of the panel's
    address made by `core/qr.py` (`GET /api/phone-qr`, http/https addresses only, signed-in
    users only), offering the machine's addresses when the panel was opened as localhost.
    The screenshot demo takes phone pictures at 390x844. Tested in Chromium with phone
    emulation (390x844), the installability check and against a QR reader. Not yet tried on
    a real iPhone or Android phone.

### 1.16.11: backups off the machine, without a cluster *(released 21 Nov 2026)*

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

    **As built:** `core/backup_targets.py`, standard library only (`hmac`/`hashlib` SigV4,
    `http.client` with a CONNECT tunnel when `https_proxy` is set, `xml.etree` for listings;
    `ssh`, `rsync` and `ssh-keygen` as subprocesses, never through a shell on this side). What
    changed from the plan:
    - **Each panel copies its own servers' backups**, standalone, master or node, rather than
      the 1.16.8 copy step. Storage is a Panel Settings item, so a master can't add it on a node
      (it can tick an existing one on a node server's Backups tab through the proxy, as for any
      server setting). The Backups tab has its own **Off-site copies** section with a checkbox
      per storage (several at once) instead of extra entries in **Also keep a copy on**.
    - **When:** `redstonepanel backup` (the timer) starts the copy in its own transient unit
      (`systemd-run --collect`, `redstonepanel-offsite-<server>`), so it runs after the backup
      unit has finished and also while the panel is stopped; **Back up now** starts it in the
      panel. A sweep every 5 minutes retries failures and copies anything missing, but only
      contacts the storage when a backup is new, a copy is due for deletion, or 6 hours have
      passed since it last listed it (S3 list requests cost money on some providers). A per
      target and server lock file keeps the panel and the unit from copying the same thing.
    - **Layout:** `<folder or prefix>/<panel id>/<server>/<file>` with a `.sha256` file next to
      each copy. The panel id is the host name plus four random hex digits, made once, so
      several panels can share a bucket and a new machine finds the old one's backups.
    - **Checks:** S3 requests carry their body's real SHA-256 (`x-amz-content-sha256`, per part
      for multipart), which the storage verifies; SSH copies are checked on the storage with
      `sha256sum` (or `shasum -a 256`) before they take their final name. Restores and imports
      check the downloaded file against the `.sha256` again.
    - **Retention:** the storage keeps its own count (or the server's Keep) and an optional age;
      local schedules got an optional age too ("Also delete them after N days"). The newest
      scheduled backup or copy is never deleted by age. Manual copies stay until deleted;
      before-restore and before-update backups aren't copied.
    - **SSH host keys** are checked with `StrictHostKeyChecking=accept-new` and the panel's own
      `known_hosts`; a changed key stops copies until an admin clicks **Forget host key**.
    - **Import → From backup storage** unpacks the copy as a removed server and runs the 1.16
      restore path, which downloads the server binary again; for Java at the recorded Minecraft
      version and loader, because backups now include `.redstonepanel-server.json`.
    - `rsync` and `openssh-client` became Recommends. New commands: `redstonepanel
      offsite-copy NAME` and `redstonepanel offsite-key`. The webhook event stays
      `backup_copy_failed` ("Backup copy to another member or storage failed").

    Tested: `tests/test_backup_targets.py` (29 tests: the AWS documentation's two SigV4
    examples, an in-process S3 server that checks each request's signature and body SHA-256
    including multipart and paged listings, a stand-in `ssh` with real rsync, retention, the
    once-per-failure webhook, fetch and import); by hand against a real OpenSSH server and the
    moto S3 server (12 MB multipart upload, download, delete); and
    `packaging/test/test-offsite.sh` (panel, SSH storage, SeaweedFS's S3 server, which checks
    signatures like real S3, and a second panel as the new machine) in Docker: 39 checks passed
    in a cloud thread (copy after Back up now and after scheduled backups with the panel
    stopped, retention on the storage, a storage that's down, restore, a damaged copy refused, a
    changed host key, a 110 MB multipart S3 upload and download, the new machine's import), and
    on Quentin's PC with the internet the import downloaded the real Bedrock server and started
    it. MinIO no longer publishes images that can be pulled, so the test uses SeaweedFS 3.80.

### 1.16.12: Bedrock that keeps up, and update alerts *(released 21 Nov 2026)*

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

    **As built:** `core/updates.py`. Changes from the plan:
    - **One timer for the machine, every 6 hours** (`redstonepanel-updates.timer`, 00:15, 06:15,
      12:15, 18:15 plus up to 15 minutes' random delay, `Persistent=true`) instead of a daily
      `redstonepanel-bedrock-update@` per server: one look at Mojang's API covers every Bedrock
      server, and a day's wait would lock Bedrock players out for most of it. It runs
      `redstonepanel check-updates`, so it works while the panel is stopped. The panel writes and
      enables the units when it starts, so upgrading the package sets it up.
    - **The download comes first.** The zip is downloaded (minecraft.net and
      minecraft-services.net hosts only, also after redirects) and checked (a real zip, no
      `..`, absolute paths or links, has `bedrock_server`) before anyone is warned, so a failed
      download changes nothing. The panel now unpacks Bedrock in Python instead of `curl | jq`,
      `wget` and `unzip`, for new servers and the Update button too.
    - **Rollback** keeps every file the zip replaces in a staging folder, so the old program goes
      back exactly. The world comes back from the "Before update" backup (`pre-update`, the
      newest three kept); the world the new version may have converted is kept as a "Before
      restore" backup. "Started" means BDS logged `Server started.` within 3 minutes; screen
      exiting means it stopped. The stop before the backup sends Bedrock's own `stop` first, so
      it saves the world.
    - **The Update button uses the same path** (no warning, since someone pressed it).
    - **Java servers are never updated automatically**; they get the alert only, because a new
      Minecraft version can break mods and plugins. The alert covers a newer release (vanilla,
      Paper, Fabric), a newer stable Paper build of the same release, and a newer stable Fabric
      loader.
    - **Clusters:** alerts travel in the `update` field of each server in `/api/servers`, which
      the master already reads from its nodes, so no new cluster routes; the settings routes are
      under `/api/servers/{name}/updates`, which nodes already accept from the master.
    - Webhook events `update_available` (on by default, once per version per server), `updated`
      and `update_failed` (on by default). Failures show under **Needs attention** until the next
      successful update.

    **Tested:** `tests/test_updates.py` (30 tests: the links API and a retried non-zip download, and host allowlist, Java alert
    rules, unsafe zips, symlinks in the server folder, keep-files, put-back, alert once per
    version, stale alerts, warning steps and order, failed download/backup/start, rollback,
    stopped servers stay stopped, the timer only updates opted-in servers). End to end,
    `packaging/test/test-bedrock-update.sh` (43 checks, in CI with the cluster scripts): a
    stand-in BDS and a stand-in download API in a systemd container; the timer's run warns,
    stops with `stop`, backs up, keeps settings and world, starts 1.0.0.2; a release that dies
    on start is rolled back to 1.0.0.2 with its world; the Update button; refused hosts and
    damaged zips leave the server running. With a real Bedrock Dedicated Server on Quentin's PC
    (Docker, 21 Nov 2026): a new server got 1.26.66.1 from minecraft.net and stayed up; the
    timer updated a real 1.26.65.1 server to 1.26.66.1 (warnings in chat, `stop` and "Quit
    correctly", Before update backup, settings kept, "Server started."); the Update button
    reinstalled it the same way. Once minecraft.net answered a third download in five minutes
    with something that wasn't a zip, so a non-zip answer is now logged (size, type, first
    bytes) and the download is tried once more after 30 seconds. The screenshot demo takes
    `update-alert.png` and `bedrock-auto-update.png` in both themes.

    **Fixed in the same release: Bedrock always saves before it stops.** Found while testing:
    the ordinary **Stop** and **Restart** of a Bedrock server only ran `systemctl stop`, whose
    SIGTERM goes to `screen`, which hangs up on `bedrock_server`, so it quit without saving and
    the last minutes of play could be lost. The game servers' drop-in now has
    `ExecStop=redstonepanel stop-unit %N $MAINPID`, which types Bedrock's own `stop` into its
    console and waits up to 75 seconds for it to exit (it logs "Quit correctly") before systemd
    ends what is left. Because it is in the unit, every stop is covered: Stop, Restart, backups,
    restores, moves, updates, scheduled restarts, uninstalling and shutting the machine down.
    Java servers are untouched (they save on SIGTERM). Tested with `tests/test_safe_stop.py`
    and `packaging/test/test-bedrock-stop.sh` (a stand-in BDS that only writes its world when it
    gets `stop`, through the Stop and Restart buttons, the restart timer and `systemctl stop`),
    and a real Bedrock Dedicated Server on Quentin's PC.

### 1.16.13: less downtime for big worlds, and finding panels on the network *(released 21 Nov 2026)*

20a. **The rest of 1.16's moves: a pre-copy, and Find panels.** Left over from 1.16.0 (item 12)
    and fleet.md phase 3. Moving a big world stops the server for as long as the whole folder
    takes to pack, copy and unpack, which is minutes for a world of several gigabytes. And adding
    a node means knowing its address and visiting it to paste a join code.

    - **Pre-copy while it runs** (an option in the **Move** window): the folder is packed and
      sent while the server keeps running, and the target unpacks it into a hidden folder next
      to its servers. Then the server stops and only what changed since (and a list of what was
      deleted) is packed and applied over that copy. The server is down for that second, small
      step only. The move window says how long the server was down.
    - **Find panels on your network** (Panel Settings › Cluster › Add a node): the master asks
      every address of its own private networks (each up to a /24, at most four) for
      `/api/panel-info` on ports 8443 and 8080, and lists the RedstonePanels that answer with
      their name, address and whether they're standalone, already a node here, or in another
      cluster. **Add to cluster** makes a join code and says where to paste it. Nothing is
      added without an admin pasting the code into the other panel, as before.

    **How:** `core/transfer.py` gets `precopy`/`base` on export, `stage()` and a `base` on
    import (cluster contract 5, new node route `POST /api/cluster/transfers/{id}/stage`);
    `core/moves.py` runs the two rounds. `core/discovery.py` does the search with the standard
    library (`ip -j addr`, http.client, a thread pool). `/api/panel-info` adds
    `"app": "RedstonePanel"` and the cluster role.

    **Size:** a few days. The careful parts: never missing a changed file (a file's size,
    times and inode are noted *before* it is read, and anything changed within two seconds of
    that counts as changed), and applying the changes without writing through links.

    **As built:** as above. Unit tests in `tests/test_precopy.py`: the second
    archive holds only changed, new and deleted files; a world edited in place (same size),
    new and deleted files, a deleted folder, a file that became a folder and a new link all
    arrive so B's folder equals A's; a file that shrinks while read keeps the archive valid;
    deletions and changes can't reach outside the staged folder through a link; cancelling
    removes the staged copy; the master's job runs in order and cleans up both members after a
    failure in either round; older nodes are refused a pre-copy with a clear message. Discovery:
    only private networks, capped at /24 and four networks, never this machine, HTTPS preferred,
    nodes recognised, answers from other software ignored, a real HTTP answer read. The
    multi-panel test `packaging/test/test-move.sh` (Ubuntu 24.04 containers in a cloud thread,
    48 checks, all passing) moves a stand-in Bedrock server that writes to its world ten times a
    second, with a 20 MB world file, using a pre-copy: down for 1 to 2 seconds, every world file
    identical on the node and the file it kept writing complete up to the stop, nothing staged
    or archived left behind. Find panels, run on the master, found the node (marked as a node
    here) and a third, standalone panel on the same /24 in about 12 seconds; a master user
    (not admin) is refused. Not tested: a multi-gigabyte world, and real Java/Bedrock servers
    (the pre-copy doesn't depend on the edition: it reads files, never the game).

### 1.16.14: port forwarding, for players outside your home *(released 21 Nov 2026)*

21. **A page that says what to forward on the internet router.** Asked for by Quentin
    (21 Nov 2026). The panel already keeps `ufw` on each machine in step, but players outside
    the house also need port forwards on the router in front (pfSense here), and which ports
    a server needs isn't obvious: Bedrock 1.26.50+ uses TCP plus fixed UDP ports, Java servers
    may also run Geyser or a voice-chat plugin, and in a cluster each server lives on a
    different machine behind one public address.

    - **🔀 Port forwarding** (admins): per server and per cluster member, the outside port,
      protocol, the machine's LAN address and inside port, and what each is for. Rows say
      **forward**, **only if you need them** (Query) or **leave closed** (RCON, NetherNet LAN
      discovery on UDP 7551, Phantom, the panel itself).
    - The game ports come from `manager.firewall_rules()`, the same logic ufw uses (1.16.11),
      so the router list and ufw never disagree. Extras read from the server folder: Query,
      RCON, Geyser (`bedrock: port`, `clone-remote-port`), Simple Voice Chat.
    - Two servers that want the same outside port on the one public address are called out.
    - The public address is typed in, or asked of the router itself (NAT-PMP, then UPnP on the
      default gateway); never an outside "what's my IP" service. Carrier-grade NAT (100.64/10)
      and double NAT are explained.
    - **Check from here** connects to the public address (Java status ping and RakNet ping
      confirm it's the right server; NetherNet TCP connect), with a plain note that it needs
      NAT reflection and that a phone on mobile data is the real test.
    - Step-by-step for pfSense (Firewall > NAT > Port Forward, the exact values per rule,
      TCP/UDP in one rule, static DHCP mappings, NAT reflection, IPv6, dynamic DNS) and for
      other routers.

    **How:** `core/portforward.py`; `GET /api/port-forwarding` (the master asks each node's
    `GET /api/port-forwarding/local`, added to the node allowlist; nodes older than 1.16.14
    fall back to their server list). `CLUSTER_API` unchanged. No new dependencies.

    **Size:** a few days.

    **As built (MR, 21 Nov 2026):** as planned. Tested with `tests/test_portforward.py` (34
    tests: port parsing, Geyser and voice-chat configs, Bedrock NetherNet/RakNet rows, clashes,
    old-node fallback, NAT-PMP against a stand-in, Java status and RakNet pings against stand-in
    servers, the API and admin-only access) and the new `packaging/test/test-port-forwarding.sh`
    (master + node in Docker with ufw, stand-in Bedrock servers, 26 checks: each machine's LAN
    address, rows per server, the clash and its fix after a port change, double NAT and CGNAT
    warnings, no NAT-PMP/UPnP on Docker's gateway, and Check from here before and after a
    listener), run in a cloud thread. Not tested: a real pfSense with NAT-PMP or UPnP turned on,
    and a Bedrock player joining through a real port forward from outside.

    **Fixed in the same release: the phone menu's last servers.** Found in 1.16.10's screenshots:
    on a short phone the ☰ menu pinned the account footer (name, Sign out, Users, Settings,
    Password) over the bottom of the menu, leaving the server list a strip about two servers
    high, so the last servers looked hidden behind the footer. Below 768 px the whole menu now
    scrolls as one page and the footer comes after the last server. Desktop is unchanged.
    Tested with `tests/test_phone.py` (the phone CSS and the footer's place in the page) and
    Playwright at 360 x 640, 375 x 667 and 390 x 844 in both themes (canned data, four servers).

### 1.17: server performance and tuning *(released 22 Nov 2026)*

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

    **As built:** `core/perf.py`, built as one release in stages (asked 21 Nov 2026). Tick health
    sends only `tps`/`mspt` (Paper, Purpur, Folia) or `tick query` (everything else, and Paper as
    a fallback), cached 25 s per server; no RCON, Bedrock, a stopped server or an older version
    each say why it's not available. History is one ring file per server plus `host.ring` under
    `/var/lib/redstonepanel/metrics/` (0700, files 0600): an 8-byte header, then 1,440 one-minute
    and 672 fifteen-minute slots of a uint32 time and five float32 values (NaN = not measured),
    about 50 KB each. A slot is found from its time, so there's no head pointer to lose in a
    crash; each write recomputes its 15-minute average from the minute slots. A daemon thread
    (`perf.sampler()`, started in `run.py` next to the backup-copy sweep, nice 10) samples every
    60 s with its own previous sample, so it doesn't disturb the dashboard's rates. Server
    fields: CPU % of the machine, process memory, players, TPS, MSPT; host: CPU %, memory %, the
    servers' disk %, network in and out (without `lo`). Routes: `GET /api/servers/{name}/perf`,
    `GET|PUT /api/servers/{name}/resources` (PUT admins only, logged as `[PERF]`) and
    `GET /api/host/history` (also on the node allowlist, read-only); the dashboard's server rows
    gain `tps` and `mspt`. **Memory** and the limits ended up as one **Memory and limits** card
    on Maintenance rather than two settings. The heap check counts only *running* Java servers
    and keeps 1 GB for the system, or 768 MB on machines with 3 GB or less, because a 2 GB
    Raspberry Pi reports about 1.8 GB and its default 1 GB heap must still fit. Limits are a
    drop-in (`minecraft-NAME.service.d/redstonepanel-limits.conf`), not part of the unit, so
    every path that rewrites the unit (update, Java change, restore, move) keeps them; they're
    also set live with `systemctl set-property --runtime`. "Automatic" is the heap plus half
    again (at least 512 MB): a smaller margin made healthy servers look like crashes. Limits stay
    on the machine (a move doesn't take them; a restore puts them back from `limits.json`).
    "Held back" reads `cpu.stat` `throttled_usec` between samples, `MemoryCurrent` against the
    limit, and `Result=oom-kill`. Suggestions dropped "move a server to another node" as its own
    rule (it's part of the memory ones) and added swap; "frequent restarts" means `NRestarts` ≥ 2.
    The webhook gets `tps_low` and `disk_full`, off by default, once an hour per server or disk.
    Charts are inline SVG drawn at their real width (text stays readable on phones), one
    measure per chart, a crosshair and tooltip on hover or touch, two colours checked for colour
    blindness and contrast on both themes. Tested: 37 unit tests (`tests/test_perf.py`: tick
    parsers with Paper's colour codes, ring wrap-around and averaging, refusals, the 2 GB Pi,
    drop-in contents, suggestions, alerts); `test-install.sh` on Ubuntu 24.04 in a systemd
    container with a stand-in Java server: Memory rewrites the unit with the same Java and the
    restart picks it up, `MemoryMax`/`CPUQuota` applied live, limits off, history written,
    uninstall removes the drop-in (cgroup `memory.max`/`cpu.max` are checked where the host has
    cgroup v2 controllers); the screens with canned data at 1440 and 390 px in both themes.

### 1.17.1: why did it crash? *(released 22 Nov 2026)*

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

    **As built (1.17.1):** `core/crashes.py`, called from `notify.unit_event()` (the drop-in's
    `ExecStopPost`) whenever a stop wasn't asked for, so a failed start counts too: a port in
    use or an unaccepted EULA makes Minecraft exit 0, and those are reported as "stopped" with
    the cause. It reads a crash report from `crash-reports/` only if written in the last 10
    minutes, the end (256 KB) of `logs/latest.log` (Bedrock `server.log`), and this run's
    journal by `_SYSTEMD_INVOCATION_ID`, because the wrong Java fails before the server opens
    its log; a log older than 10 minutes is looked at last. Files are opened one component at a
    time with `O_NOFOLLOW` (the hook runs as root). Causes are a table of line patterns, first
    match per cause: Fabric's "Mod 'X' (x) … requires … which is missing / wrong version"
    (every missing dependency of that mod in one line, with friendly names such as Fabric API),
    Paper's "Unknown/missing dependency plugins" and "Unsupported API version",
    `UnsupportedClassVersionError` (class file version minus 44 gives the Java, both needed and
    running), `FAILED TO BIND TO PORT` and Bedrock's "Network port occupied" (with the port from
    `server.properties`), `OutOfMemoryError` or systemd's `oom-kill` (the 1.17 hard limit), the
    EULA, `level.dat`, and chunk/region errors. A real crash report often only says "Failed to
    initialize server", so the log's causes are added to the report's. Suspects come from
    "Suspected Mods", mixin "from mod", Paper's "Error occurred while enabling" and "Could not
    load 'plugins/x.jar'", and the mod named in missing-dependency lines, matched against the
    installed jars' ids, names and file names. The record is `/var/lib/redstonepanel/crashes/
    NAME.json` (0600): causes, up to 400 log lines with error lines marked, suspects, and the
    last mod update if it was in the last 3 days. Routes: `GET|DELETE /api/servers/{name}/crash`,
    `POST …/crash/turn-off` (only a file the record names) and `POST …/crash/undo` (both admins
    only, logged as `[CRASH]`, then restart and close the card). The webhook message gets the
    first cause, or "Likely cause: the mod X"; **Needs attention** shows an undismissed crash for
    a week (and adds the cause to "keeps crashing"). The world-touching fixes in the plan
    (restoring a backup for a damaged world) stay as advice pointing at the Backups tab, which
    already backs up first, so nothing here touches the world. Found while testing:
    `stop_requested()` counted any queued job, and a server that dies within a second of starting
    exits while its start job waits for `ExecStartPost`, so the wrong Java was reported as
    "stopped"; now only stop and restart jobs count. Tested: `tests/test_crashes.py` (23 tests)
    with logs and crash reports from real servers made to fail on purpose with Java 21: vanilla
    26.3 (needs Java 25), vanilla 1.21.4 without the EULA, with its port taken, with a
    corrupted `level.dat` and with a 40 MB heap, and Fabric 0.20.1 with Ledger from Modrinth but
    no Fabric API or Fabric Language Kotlin, all named correctly (`tests/crash_samples/`);
    `packaging/test/test-crashes.sh` (21 checks, also in the GitHub `cluster` job): the installed
    .deb's real hooks write the record as root, the webhook line, Needs attention, the API,
    Turn off and restart, the wrong Java from the journal, a failed start that exits 0, Dismiss;
    the card with canned data from the real Fabric failure at 1440 and 390 px in both themes.
    The release also makes the RedstonePanel logo and name link to the project website
    (redstonepanel.cielocloud.org).

### 1.17.2: Advanced options on New server *(released 22 Nov 2026)*

- **Choose what New server otherwise decides: name, port, build, Java, heap, startup flags,
    world and the common settings.** Asked for on 21 Nov 2026 after trying
    [minecraft-server-manager](https://github.com/anefzaoui/minecraft-server-manager), a
    Docker-based Java panel whose wizard exposes every setting of its server image. The ideas
    that fit a native systemd panel are rebuilt here (no code taken; it is MIT licensed).
    Plan: the Claude Doc "Advanced options for new Java servers".

    Why here: it reuses 1.17's memory checks, needs nothing from 1.18 or 1.19, and gives 1.20's
    new types and modpacks a place for their builds.

    - **A collapsed Advanced options section** under the existing form. Closed, New server works
      as before. Every field is optional; empty means "as without Advanced options".
    - **Server:** the name (`minecraft-NAME`, lower-case letters, digits and dashes; a Bedrock
      name can't contain "java") and the game port, both checked for clashes before anything is
      downloaded.
    - **Software (Java):** pin a Paper build or Fabric loader version (listed from Fill and Fabric
      meta, unstable ones labelled), show Vanilla snapshots and pre-releases of the current
      cycle, and choose an installed Java (never older than the version needs).
    - **Memory (Java):** the heap, refused when it leaves under 1 GB for the system (768 MB on
      machines with 3 GB or less). Java starts with half of it.
    - **Hard limits (Java and Bedrock):** 1.17's hard memory limit (Automatic for Java, or a size)
      and CPU limit in cores, set through `perf.set_resources()` before the first start, so the
      drop-in is there from the start. A size too close to the heap is refused before any download.
    - **Startup flags (Java):** none, or Aikar's flags (with the heap fully allocated up front,
      as Aikar recommends), plus extra flags for admins: only `-XX:`, `-D` and `-Xss`, never
      `-XX:OnError`, `OnOutOfMemoryError`, `Flags` or `VMOptionsFile`, and none of the characters
      systemd interprets. Kept in `/var/lib/redstonepanel/jvm-flags.json` and written into the
      unit on every rewrite, so Update, Memory, a Java switch, restore and moves keep them.
      **Maintenance → Java → Startup flags** changes them later.
    - **World:** seed, world type (Java: Default, Flat, Large biomes, Amplified; Bedrock:
      Default, Flat) with the right `level-type` spelling for the version, and the world
      folder name.
    - **Settings:** MOTD (Bedrock `server-name`), game mode, difficulty (numbers before Java 1.14),
      max players, online mode, whitelist/allowlist players and Java operators. A chosen template
      is applied first and these win; its values show as hints.
    - **Nodes:** the master sends the same object; nodes need contract 6, otherwise the section
      says to update RedstonePanel there.

    **Not included, and why:** Spigot/CraftBukkit (BuildTools compiles on the machine; Paper runs
    their plugins), Folia, Pufferfish and Leaf (Folia breaks most plugins; the forks can join the
    Paper family if asked), hybrids such as Mohist and Arclight (mods plus plugins break
    unpredictably), Limbo and Sponge, CurseForge auto-download (see 1.20), Docker settings, JMX,
    disk quotas, auto-pause (a scheduled-commands idea, not a deploy option). Add-ons at creation
    join 1.20.

    **How:** `core/deployopts.py` (checks, Aikar's flags, the flags store),
    `ServerDeployRequest.advanced`, `GET /api/java/builds`, `GET /api/java/snapshots`,
    `POST /api/servers/{name}/jvm-flags`; `CLUSTER_API` 6.

    **Size:** about a week.

    **As built (21 Nov 2026, MR on `feature/advanced-deploy-options`):** as planned, except that
    templates don't store the Software, Memory and Startup flags groups yet. Rebased on 1.17.1
    on 22 Nov 2026 and the hard limits added. Tested:
    `tests/test_deployopts.py` (29 tests, including the hard limits: every check, flags in the unit across rewrites, pinned
    Paper and Fabric builds, snapshots and the downgrade check, deploy with every option,
    template order, Maintenance flags); `test-install.sh` (refusals with nothing left behind,
    flags on Maintenance, `jvm-flags.json` 0600); `packaging/test/test-advanced-deploy.sh`
    with real downloads on Docker Desktop, 33/33: Paper 26.3 with every option (build 256 pinned,
    Java 25, Aikar's flags plus an extra flag, flat world, Notch whitelisted and op), Fabric with
    loader 0.20.0 pinned, and Vanilla 26.4-snapshot-10, each reaching "Done"; updating the
    snapshot back to 26.3 refused. Rerun after the rebase with the hard limits (build 257 pinned;
    Automatic memory 1.5G and 1.5 cores in the drop-in and applied by systemd): all passed, as did
    `test-install.sh` with the limit refusals.

### 1.18: helpers with their own access, and SFTP *(released 22 Nov 2026)*

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

    **As built (22 Nov 2026, MR !103):** as planned, with these
    differences.
    - **One rule per route, not a dependency.** `core/grants.py` holds a table with a rule for
      every API route (`public`, `user`, `signed`, `admin`, `node`, `proxy`, or a permission,
      optionally with the path parameter that names the server). The middleware in `main.py`
      finds the route's template and applies the rule; a route missing from the table is
      admin-only, and `tests/test_grants.py` fails when a route has no rule. Server lists, the
      dashboard, Needs attention, the cluster overview and the activity log are cut down to
      what the account may view, and each server carries the account's `perms` so the page
      hides the tabs and buttons that would be refused.
    - **Clusters:** the master keeps node grants under `<node id>/<server>` and checks the
      inner route's rule before it proxies, then tells the node what the user may do in an
      `X-RedstonePanel-Perms` header that only a node token can send. A master older than 1.18
      sends no header and the node gives its users what they could always do. `CLUSTER_API`
      stays 6. A moved server takes its grants along.
    - **Updating:** the first start of 1.18 gives the user accounts that already exist the old
      user rights (everything except Files and Mods & plugins) on every server, so nothing
      changes for them; accounts made afterwards start with nothing. Trusted-LAN visitors
      keep the same old rights.
    - **Tightened for users:** importing a server, the removed-servers list and restore,
      editing and deleting templates, the cluster's list of moves and pushing a template to a
      node are admin-only now, since none of them belongs to one server a grant could name.
    - **SFTP is off by default** and turned on under **Panel Settings → SFTP**: opening a new
      port on a machine that may face the internet should be the owner's choice. The panel
      writes `redstonepanel-sftp.service` (`redstonepanel sftp-serve`) itself and opens or
      closes the port in ufw.
    - **Node servers aren't reachable through the master's SFTP,** and nodes don't check
      sign-ins with the master: nodes never call the master (a design rule of the cluster).
      Each machine serves SFTP for its own servers and its own panel's accounts; a node
      server's Files tab says so on the master.
    - **Sign-in:** `account.server` is split at the last `.minecraft`, so account names with
      dots work. Password (panel or Linux account) or an SSH key (Ed25519, ECDSA, RSA of 2048
      bits or more; ten per account, in `data/ssh_keys.json`). Accounts with two-factor must
      use a key. Failed sign-ins share the panel's limit through
      `/run/redstonepanel/signin-fails.json`. A session re-checks the account and its
      **Files** grant every 30 seconds, so disabling an account or taking the grant away ends
      it. Only the `sftp` subsystem is offered: no shell, exec or forwarding. The host key is
      ECDSA P-256 and its fingerprint is shown on the Files tab.
    - **Files:** new descriptor-based helpers in `core/files.py` (`open_fd`, `scan`,
      `stat_path`, `move`, `remove_file`, `remove_empty_dir`, `set_mtime`) walk each path
      component with `O_NOFOLLOW`, refuse files with other hard links and give new files to
      the folder's owner. Symlinks are listed but can't be followed, made or read through.
    - **Tested:** `tests/test_grants.py` (31 tests: the store, every route has a rule and each
      per-server route needs its permission, filtered lists, the proxy and the node side) and
      `tests/test_sftp.py` (19 tests against a live paramiko server: sign-in by password and
      key, two-factor, the grant, path and symlink escapes, hard links, revocation, no shell).
      `packaging/test/test-permissions-sftp.sh` (also in the GitHub `cluster` job) installs the
      .deb on Ubuntu 24.04 (paramiko 2.12) with two stand-in servers and checks, with the real
      OpenSSH `sftp` client: what each permission opens and refuses, the filtered dashboard,
      SFTP off until turned on, password and key sign-in for panel and Linux accounts, upload
      and download checksums, escapes through `..` and symlinks, a refused shell, access ending
      when a grant is removed, the shared sign-in limit, changing the port, turning it off,
      a start with no grants file (as after updating from 1.17.2) and running without
      `python3-paramiko`. `test-install.sh` still passes. Not tested on a real
      cluster end to end; the node side is covered by unit tests.

### 1.19: change a server's type or version *(released 23 Nov 2026)*

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

    **As built (22 Nov 2026, MR !105):** as planned, with these
    differences.

    - **Minecraft 26.1 changed the world layout** after this was written: every type, Paper
      included, now keeps all dimensions under `world/dimensions/`. So the folder moves apply
      only to worlds still in the older layout (up to 1.21.11); the panel tells the layouts apart
      from the folders on disk, and refuses a 26.1+ world that still has Paper's old folders
      beside the new ones (an unfinished upgrade). Paper 26.1+ still differs in one way, found by
      the end-to-end test: it keeps `game_rules.dat`, `scheduled_events.dat`, `weather.dat` and
      `world_gen_settings.dat` in each dimension's `data/minecraft` instead of
      `world/data/minecraft`, and Vanilla and Fabric won't start without them ("Overworld
      settings missing"). Leaving Paper, the panel moves the Overworld's copies back.
    - **Going to Paper,** the panel moves the dimensions itself when Paper's own folder
      (`world_nether`) is left from an earlier stay, because Paper only moves a dimension into a
      folder it can create.
    - **Content mods** are recognised from the jars themselves (block states, item models,
      dimensions, world generation; one level into bundled jars) and from Modrinth's categories
      and client side, not from `level.dat` alone, whose data-pack list turned out to name few
      mods. Disabled mods don't count.
    - **Changing type and version together** is one step with one backup, not two.
    - **Fabric API** is installed along with any Fabric builds chosen in the preview, since
      nearly all of them need it.
    - **Same type, newer version** stays with **Update Server**, which now takes a "Before
      update" backup first (ticked by default, newest three kept) and keeps the old
      `server.jar`, so **Roll back the last update** is the same undo.
    - **Undo restores the world before the software,** so its "Before restore" backup is
      labelled with the newer software that wrote it and can't be restored onto the older one
      by mistake.
    - The backup manifest is `.redstonepanel-backup.json`, the first member of every archive;
      Java backups since 1.16.11 already carried the server info file and show **Made by** too.
    - The change runs in the background (`GET /api/servers/{name}/convert` is its progress and,
      with `?to=`, the preview), so it also works through the master's proxy for a node's server.

    Files: `core/convert.py`; routes `GET`/`POST /api/servers/{name}/convert`,
    `POST …/convert/undo`, `POST …/update/rollback` (admins; reachable by the master like every
    route of a server, `CLUSTER_API` unchanged); new notification **Server type changed**.
    Tested: `tests/test_convert.py` (33 tests: layout moves and refusals, symlinks, content
    detection, the plan, every step of a change, a failed start, undo, update roll back, the
    manifest and the newer-world refusal); `test-install.sh` (status and refusals on an
    unrecognised jar); `packaging/test/test-convert.sh` with real downloads in the systemd
    container (a cloud Docker host, 22 Nov 2026): one world with a block placed in the Overworld,
    the Nether and the End goes Vanilla → Paper → Vanilla → Fabric → Paper, with LuckPerms'
    Fabric build swapped for its Paper build on the way; then Undo type change, a Fabric that
    can't start because a mod's dependency is missing (Vanilla comes back by itself, with the
    log line that says why), the typed-name check for a mod that adds blocks, and a Vanilla
    update rolled back. The blocks, seed and player data are checked after every step. 84/84
    on Minecraft 1.21.11 (Paper build 132, Paper's separate folders) and 81/81 with
    `MC=26.1.2 OLD=26.1.1` (the shared layout, Java 25). The first 1.21.11 run found two bugs in
    undo (the add-on tracking file, and the "Before restore" backup's label), fixed before
    this MR. The default (1.21.11) run is in the GitHub `cluster` job.

### 1.20: Forge, NeoForge, Quilt, Purpur and modpacks *(released 23 Nov 2026)*

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

    **As built (22 Nov 2026, MR !104):** as planned, with these
    differences. Quilt is not a `server.jar`: like Forge and NeoForge it has an installer, which
    the panel runs as `minecraft` in the new server's folder (or, for an update, in a staging
    folder that is swapped in only when it worked). What the unit starts is recorded as `launch`
    in `.redstonepanel-server.json` and checked against a strict pattern before it goes into
    `ExecStart`, because that file is writable by `minecraft` and the unit belongs to root. Forge
    covers 1.12.2 and newer, Quilt 1.14.4 and newer, NeoForge 1.20.2 and newer. Forge before 1.17
    doesn't start on a newer Java than it was made for, so those servers get Java 8 (below 1.13)
    or at most Java 11. Ubuntu's Java 8 package has no `release` file and was never found by the
    panel before; it is now. Backups leave the loader's libraries out, and a restore runs the
    installer again at the recorded build. NeoForge servers take Forge mods only up to Minecraft
    1.20.4. A modpack server is given 4 GB of heap when the machine has room, gets no server
    software update alert (the pack decides), and a pack update removes only the mods the old
    pack version installed. An uploaded server pack's scripts, installers, server jars and
    libraries are never copied or run. Modpack installs and searches work on nodes
    (`CLUSTER_API` 7; older nodes show the new types greyed out). New routes:
    `GET /api/modpacks/search`, `GET /api/modpacks/versions`, `POST /api/servers/modpack`,
    `GET /api/servers/{name}/modpack`, `POST …/modpack/check`, `POST …/modpack/update`.
    Tested: `tests/test_loaders.py` and `tests/test_modpacks.py` (45 tests: version lists and
    build choice for each type, the installer run and what it records, the unit, old Forge's
    Java, mods accepted and refused per type, update alerts, reading and refusing packs, hashes
    and hosts, path safety, create and update). `packaging/test/test-loaders-modpacks.sh` with
    real downloads in a systemd container, 92/92: Purpur 1.21.4, Quilt 1.21.4, NeoForge 1.21.1
    (a pinned build, then updated to the newest), Forge 1.20.1 on Java 17 and Forge 1.12.2 on
    Java 8 each reaching "Done"; a NeoForge mod accepted and a Fabric one refused; the Modrinth
    pack Adrenaserver installed at an older version, then updated across Minecraft versions with
    a backup first, the world, settings and the owner's own mod kept; an uploaded `.mrpack` and
    an uploaded Forge server pack started; a wrong hash, a download from another host, a
    CurseForge client zip and a non-admin all refused with nothing left behind. The new dialogs
    were checked at 390 x 844. Rebased on 1.18 and 1.19 on 22 Nov 2026: 1.19's type changes take the new types (Purpur
    counts as Paper for the world layout; Paper ↔ Purpur and Fabric ↔ Quilt leave plugins and
    mods in place; other mod loaders set each other's `mods` aside), Quilt, Forge and NeoForge
    keep no `server.jar.previous` and are put back by running their installer again for the
    recorded build, a modpack server keeps its type, and the modpack routes have their rules in
    `core/grants.py` (admins; the Modpack card is visible with view access). The end-to-end
    script gained a NeoForge update roll back and Paper → Purpur → undo, Fabric → Quilt → undo,
    Quilt → Fabric → undo, and passes 119/119 on the rebased build. Not tested: ARM64 (the installers are plain Java).

### 1.21 to 1.24: RedstonePanel on Windows

Decided 22 Nov 2026: a native Windows version that keeps every feature, built after 1.20 so
the releases before it aren't refactored underneath. One codebase and the same web UI; no
Docker and no WSL. The four releases below ship in order, and each has a gate the next one
waits for. From 1.21 on, every new feature is built against the platform layer and lands on
both systems in the same release.

**Why:** many people's spare machine is a Windows PC, and "install Ubuntu first" is where they
stop. The panel's promise carries over: each game server is an ordinary Windows service that
`services.msc`, `sc query` and Task Scheduler show, the way `systemctl` does on Linux.

**What maps to what.** Every Linux-specific part has an equivalent that ships with Windows:

| Linux today | Windows |
| --- | --- |
| systemd unit `minecraft-<name>.service`, `Restart=on-failure` | Windows service `minecraft-<name>` hosted by the panel's runner, restarted by service recovery actions |
| `screen` for the Bedrock console | The runner's console pipe (administrators and the system only) and a rotated log file |
| systemd timers (backups, restarts, update check, firewall sync) | Task Scheduler tasks run as SYSTEM, so they work while the panel is stopped |
| `ExecStopPost` crash hook, journal | The runner writes to the Windows Event Log; a task triggered by that entry sends the webhook |
| `ufw` | Windows Defender Firewall rules in a `RedstonePanel` group |
| cgroup `MemoryMax` and `CPUQuota`, cgroup numbers | Job Object limits and accounting |
| User `minecraft` | A virtual service account per server, with rights to its own folder only |
| PAM and the `sudo` group | Windows sign-in check and the Administrators group; panel accounts are the default |
| `/opt/minecraft-<name>`, `/var/lib`, `/var/backups`, `/var/log` | `C:\ProgramData\RedstonePanel\servers\<name>`, `\data`, `\backups`, `\logs`; the installer offers another drive |
| `.deb`, signed apt repository, `redstonepanel upgrade` | MSI installer carrying Python and the web framework, published on GitHub Releases with SHA-256 sums, updated by a one-off scheduled task |
| `openjdk-N-jre-headless` from apt | Eclipse Temurin JREs from the Adoptium API, hash-checked, one folder per version |
| Bedrock's Linux build | Mojang's Windows build from the same download-links API |
| `ssh` and `rsync --partial` for off-site backups | `ssh` and `sftp` from the OpenSSH client that ships with Windows |
| `openssl` command, certbot | The `cryptography` library; a Windows ACME client for Let's Encrypt |

**The runner.** Windows can't run `java.exe` as a service, hold its console and cap its memory
on its own. Each server's service is a short Python program (standard library and `ctypes`)
that talks to the service manager, starts the game inside a Job Object, holds its console for
commands and a clean `stop`, and logs starts, stops and crashes. Bundling WinSW was considered
and stays as the fallback for the service part only.

**What won't match exactly** (each gets a line in the docs):

- A memory limit refuses memory, so Java reports out of memory and exits, where Linux kills
  the process. Crash explanations gain a "reached its memory limit" result.
- Let's Encrypt needs a different client; self-signed, cluster CA and your own certificate
  come first.
- Windows accounts that sign in with a PIN, Hello or a passwordless Microsoft account have no
  password to check, so they can't be used to sign in to the panel.
- An unsigned installer shows a SmartScreen warning. The preview ships unsigned with SHA-256
  sums while the project applies to SignPath Foundation for free open-source signing.
- apt no longer updates Java, so the update check also looks for newer Temurin builds.
- Windows locks a running server's jars, so replacing a loaded jar needs a stop first.

**Systems:** Windows 11 and Windows Server 2022 or 2025, 64-bit Intel or AMD. Windows 10
installs with an end-of-support note and isn't tested. Windows on ARM is left for later.

26. **1.21: groundwork, no visible change** *(released 23 Nov 2026)*. Move every Linux-specific call behind a new
    `core/platform/` package with a Linux side only (about 105 `systemctl` calls in 13 files,
    48 `ufw` calls in 5, about 59 `screen` references in 11 and 48 apt or dpkg calls in 7, at
    1.17.2). Paths come from one module; `curl`, `wget`, `jq`, `unzip` and `tar` give way to
    Python where they're still used; unit tests gain a fake platform so they run on any
    system. **Gate:** every existing unit test and install script passes unchanged on all
    supported Linux systems. **Size:** large; it touches most of `core/`.

    **As built (23 Nov 2026, MR !108):** as planned for programs and paths, with a line drawn
    under them. `core/platform/` has six areas (`services`, `firewall`, `console`, `packages`,
    `system`, `certs`) and `paths.py`; about 180 program calls left 29 files, including the
    loader installer and modpack calls 1.20 added, which the new guard test found on the
    rebase. Every program runs with the same arguments as before (the old tests check the
    command lines), `grep`, `find` and a `systemctl | cut` fallback became Python, and `curl`,
    `wget`, `jq`, `unzip` and `tar` were already gone. `core/platform/fake/` is a second side
    that runs nothing and records every call. `tests/test_platform.py` reads the source and
    fails when a file outside the layer starts a system program or spells out a system path,
    and when the two sides' functions differ. Two files still start programs on purpose:
    `core/manager.py` (`run_and_log`, and Bedrock's own program for an XUID lookup) and
    `core/backup_targets.py` (`ssh`, `ssh-keygen`, `rsync`). **Not moved, and now part of
    1.22:** the text of systemd unit files (game servers, timers, Phantom, SFTP, the limits
    drop-in), the `/proc` and cgroup readers, `pwd`, `fcntl` and PAM, and messages that quote a
    Linux command to type; each needs its Windows shape before it gets a good function name.
    The old unit tests still replace `subprocess.run`, so they need Linux; only new tests use
    the fake. Design notes: [platform.md](platform.md). Tested: the 784 unit tests from before
    pass unchanged, plus 20 new; no test script was changed; `test-install.sh` 131/131 on
    Ubuntu 24.04 in Docker; GitLab CI on Ubuntu 24.04, 26.04, 26.10 and Debian 13; and the
    GitHub matrix on the branch (install tests on all four systems and ARM64, the cluster and
    feature scripts). Not tested: nothing runs on Windows yet.

27. **1.22: Windows preview** *(released 24 Nov 2026)*. One machine, Java servers. The MSI installer with bundled
    Python, the panel service, the runner, Java from Adoptium, Defender Firewall, Task
    Scheduler schedules, backups and restore, console, file manager (refusing symlinks,
    junctions and Windows trap names such as `CON` and `name:stream`), catalog, webhooks,
    panel accounts with two-factor, self-signed HTTPS, the host dashboard and the first-run
    guide. Labelled a preview in the panel and the release notes. **Gate:** a Windows install
    test passes on GitHub's `windows-2022` and `windows-2025` runners. **Size:** large.

    **As built (released 24 Nov 2026).** As planned, with these differences. The runner is the
    panel's own Python program (`core/platform/windows/runner.py`, standard library and
    `ctypes`), not WinSW. Windows keeps the same systemd-format unit files, which the runner
    reads, so feature code is shared; timers become Task Scheduler tasks. Each server runs as
    its own virtual account (`NT SERVICE\minecraft-<name>`) with rights to its own folder only.
    Event-log triggers in Task Scheduler didn't fire reliably, so the crash task reads the
    runner's own event files. Python 3.13 makes folders with inheritance turned off, so the panel
    turns it on below each server folder; a jar moved in from elsewhere keeps its old rights, so
    a found server's files are granted by name. Releases made while it was built came along:
    the servers folder on any drive (1.21.1, with a drive list on Windows), Found servers (1.21.3,
    with the Windows process tree and services), quick commands, join pages and player packs,
    the Visual Editor. Not in the preview: the terminal (1.21.10), which needs ConPTY and Windows
    account sign-in; it is left for a later Windows release. Found while testing: Linux-only calls outside
    `core/platform` (`os.major`) broke the Performance card on Windows; fixed, and the Windows
    install test now opens it. Tested: `.github/workflows/windows.yml` on `windows-2022` and
    `windows-2025` (staged install and MSI, upgrade over itself, removal), and Quentin's own
    Windows PC. The release attaches the MSI and its SHA-256; the MSI isn't signed yet
    (SignPath application pending).

28. **1.23: the whole single machine** *(released 25 Nov 2026)*. Bedrock and its auto-update, Phantom, hard limits and
    performance history, crash explanations with Windows causes, off-site backups over `sftp`
    and S3, self-update, Windows account sign-in, the port forwarding guide and diagnostics
    (including port ranges reserved by Hyper-V or WSL), and Needs attention notes when the PC
    is set to sleep or a Windows Update restart is pending. **Gate:** every 1.17.2 feature
    checked on a real Windows machine. **Size:** large.

    **As built (released 25 Nov 2026, MR !150).** As planned, plus Quilt, Forge, NeoForge and
    modpacks (Quentin's choice), and with these differences. **No terminal on Windows, ever**
    (Quentin, 24 Nov): the owner usually sits at that PC and can open a command prompt, and it
    keeps a way to run any command as any account out of the panel. **SFTP moves to 1.24.**
    Limits are Job Objects (`core/platform/windows/jobs.py`): Windows refuses committed memory
    past the limit, so Java stops with out of memory; the runner records it as `oom-kill` so the
    crash card says it hit its limit. Loader installers run as the new server's virtual account
    by pointing its service at `runner.py service UNIT --task` for one run
    (`services.run_as_server`), since Windows starts a virtual account only through its
    service. SSH storage uses OpenSSH's `sftp` (resuming with `reput`) instead of `rsync`.
    Self-update downloads the release's MSI from GitHub, checks it against the `.sha256` file and
    GitHub's digest, and a detached PowerShell runs `msiexec /qn`, restarts the panel and the
    servers that ran, and writes the exit code the new panel reads back
    (`core/platform/windows/panelpkg.py`); game servers stop for about a minute, unlike Linux.
    1.22 panels can't update themselves, so the first update is by hand. Windows sign-in uses
    `LogonUser` (network logon); Administrators (by SID) or a local group **RedstonePanel** may
    sign in, and every spelling of an account (`.\name`, `PC\name`, a Microsoft account's
    e-mail) becomes one panel identity. Needs attention reads `netsh` excluded port ranges,
    `powercfg` and the Windows Update restart keys. Found on a real PC (Quentin's): the router
    address was always empty on Windows (now `system.default_gateway`, from
    `GetIpForwardTable`), and with HTTPS on only the HTTP port had a firewall rule (the
    installer and Panel Settings now keep the panel's rule in step). Also in this release, on
    both systems: a new Bedrock server's automatic port skips UDP ports another program holds,
    and Geyser's port is read on NeoForge and BungeeCord. Tested: `windows.yml` on
    `windows-2022` and `windows-2025` (staged install and MSI, the full install test including
    limits, S3 copies, a Bedrock server with auto-update and Phantom, a Forge server, the servers
    folder, Found servers and Windows accounts, upgrade over itself, removal); Quentin's PC as
    administrator with HTTPS on and servers on another drive, 96 of 104 checks (the 8 failures
    were the test's own assumptions, fixed; the run stopped at the Bedrock step, so Forge, the
    servers folder and Found servers were covered there by CI only); 1,065 unit tests; the
    Linux release workflow on every supported system and ARM64.

29. **1.24: mixed Linux and Windows clusters** *(released Nov 2026)*. Windows nodes and masters; members report
    their system (`CLUSTER_API` goes up by one). Java servers move both ways unchanged;
    Bedrock servers move with their program downloaded again for the target system. Before a
    move or restore onto Windows the master checks for file names Windows can't hold (names
    differing only by case, `:` or trailing dots, reserved names) and refuses with the list.
    Backup copies cross systems, and paths are never sent between members. Let's Encrypt
    through a Windows client, and 1.18's SFTP and permissions confirmed on Windows. The
    preview label comes off. **Size:** medium.

    **As built (1.24):** as planned, with `CLUSTER_API` 9. Members report `platform`; the
    Cluster page and Panel Settings say *Linux* or *Windows*, and join codes come with a command
    for each system. The Windows certificate code (the `cryptography` library standing in for
    `openssl`) gained `req -verify`, `x509 -ext subjectAltName`, `verify -attime` and `enc` in
    OpenSSL's own format, so cluster CAs, node certificates and encrypted cluster backups work
    across systems. The name check is `core/winnames.py`: the source refuses before it stops
    the server (the master asks with `for_windows`), and a Windows panel checks every archive,
    pre-copy and restore too. Bedrock gets this system's program of the version it ran
    (`updates.same_version`) or the newest, and the other system's program goes. SFTP runs on
    Windows as `redstonepanel-sftp` (paramiko in the MSI). "HTTPS with my own certificate"
    restarts the panel when certbot or win-acme renews the files. Added after testing: a server
    moved away and back keeps its name, numbered (`minecraft-survival-2`); start, stop and
    restart on a node wait up to 3 minutes for its answer; and, asked by Quentin, the installer's
    Finish page opens the panel and the Start menu gets **RedstonePanel** (both open
    `RedstonePanel.url`, which the panel rewrites on every start). Not possible: a Docker Desktop
    container can't join a Windows master on the same PC (the master sees the PC's own address),
    so that direction was tested with the roles swapped and in CI. Tests: 1,149 unit tests; the
    Windows install and MSI tests on Server 2022 and 2025 (SFTP, a Windows master with an
    encrypted backup read back, archives with bad names refused); the Linux release workflow;
    and a real mixed cluster on Quentin's PC (a Linux master in Docker Desktop, the Windows panel
    as its node): Java and Bedrock moves both ways with pre-copy, the name refusal, backup copies
    both ways with a restore across, the cluster backup read by `openssl` on both systems, and
    SFTP on the node, all passing.

    **Sign-in on Windows, added to 1.24 (Quentin, 26 Nov 2026; first planned as 1.24.1):**
    Windows panels sign in with Windows accounts and panel accounts, optional two-factor, and
    **Windows Hello passkeys** (WebAuthn, `core/passkeys.py`): any account adds one under
    **🔑 Password** after typing its password, and **Sign in with Windows Hello** needs no user
    name. The passkey can be on the PC (Windows Hello), a phone (Windows shows a QR code) or a
    security key: no authenticator attachment is asked for, after Quentin tried his phone in the
    first test. Only the public key is kept (`data/passkeys.json`); ES256 and RS256; user verification
    required, so a passkey sign-in counts as both steps (and meets "require two-factor for
    admins"); one-use 2-minute challenges; the origin and host name are checked, and a counter
    that goes backwards is refused. Browsers allow passkeys at `localhost` or a host name over
    HTTPS, never a bare IP, which the panel explains where it can't offer them. `reset-2fa` and
    the admin's reset remove passkeys too. Linux panels are unchanged (Linux accounts, panel
    accounts, optional two-factor). **Remember this device** came with it too (Quentin's ask while testing, `core/devices.py`): a box on
    the sign-in page makes a 30-day session tied to a device record (`data/devices.json`), checked
    on every request, listed under **🔑 Password** with Forget and Forget All Other Devices; signing
    out, a new password, or a disabled or deleted account forgets it. Two small fixes came with it: the installer's Finish page now
    opens a **one-use link to choose the admin password** (`core/firstsignin.py`, 7 days, the
    token after `#`, the file readable only by the person who ran the installer; using it deletes
    `first-sign-in.txt`), and `redstonepanel reset-password NAME` sets a forgotten panel
    password from an administrator prompt. Google, GitHub and Apple sign-in stay out of the free
    panel (the paid Teams bundle's roadmap). Tests: unit tests with a synthetic authenticator
    (ES256 and RS256, replay, wrong origin or host, no user verification, bad signature, counter,
    disabled account, rate limit), the first sign-in link and `reset-password`; the Windows test
    walks the link on Server 2022 and 2025.

**Alongside 1.18 to 1.20:** new code that runs `systemctl`, `ufw`, `apt` or a shell tool goes
in a small helper function, with no new hard-coded `/opt` or `/var` paths, so the port doesn't
grow. A runner prototype on a Windows PC (start a Paper server as a service, send it `stop`,
cap its memory, survive a reboot) can start at any time; it's new files only.

**Testing:** unit tests on both systems (the Windows run in GitHub Actions on the mirror,
since GitLab CI has no Windows runner); a Windows install test on GitHub's Windows runners;
several panels side by side on one runner for cluster tests; mixed clusters on a Windows PC
with Linux nodes in Docker Desktop. A tag publishes the `.deb` and the MSI together, and only
when both systems' tests pass.

**Risks to settle with the prototype:** the runner itself, backing up a live world whose
files the server holds open (fallback: a Volume Shadow Copy snapshot), antivirus scanning
slowing or locking world saves, clean stops inside the time Windows allows at shutdown, and
third-party firewalls that ignore Defender's rules.

### 1.21.1: choose where servers are kept *(released 23 Nov 2026)*

**Why.** Servers have always gone in `/opt`, which is on the system disk. On a machine with a
small system disk and a big second drive, or separate partitions, that is the wrong place, and
there was no supported way to change it. Quentin asked for it on 23 Nov 2026 while trying the
Windows preview, where the same question is "which drive".

**How.** One setting, **Panel Settings → Servers folder** (`servers_folder` in the settings
file, empty for the usual place), with its own API (`GET`/`PUT /api/settings/servers-folder`,
admins only, never reachable by a cluster master). It decides where *new* servers are made.
Servers already made stay put: each unit file has always carried its own folder, so nothing
about a running server depends on the setting. Every folder that was ever chosen is remembered
(`servers_folders_used`), and everything that used to look in `/opt` now looks in all of them:
free names, used ports, removed servers, what may be deleted. What is a fit folder is the
platform layer's business (`system.check_servers_folder`, `system.prepare_servers_folder`).

**Size.** Small: one setting, about thirty places that named the folder.

**As built.** As planned. Refused, with the reason: system folders, the top folder of a disk
(`/mnt`, `/home`), names with spaces or other odd characters, read-only and `noexec` disks, a
folder the `minecraft` account can't reach, and paths containing "java" or "bedrock" (the panel
tells the two editions apart by those words in a unit's start command). An unpacked backup
becomes a server by a rename on the disk it was unpacked on. Not in this release: moving an
existing server to the new folder, and choosing where backups go. Tested: 15 unit tests
(`tests/test_servers_folder.py`); `test-install.sh` on Ubuntu 24.04 (158 checks, 27 of them
new: refusals, a folder on `/srv`, import there under a name `/opt` doesn't use, ownership,
unit file, file manager, backup, uninstall keeping the world, Removed servers, going back to
`/opt`, deleting for good).

### 1.21.2: a simple light/dark switch *(released 23 Nov 2026)*

**Why.** Light/dark mode and the accent color sat behind a ⚙️ button in the sidebar that opened
a dialog titled "Panel Settings". It looked like a second Settings button, next to the real
**🛡️ Settings** at the bottom of the sidebar, and changing the mode took four clicks. Quentin
asked on 23 Nov 2026 for a simple switch in the top right corner.

**How.** A sun/moon button fixed in the top right corner of the page (in the top bar on a
phone) toggles the mode with one click; a colored dot beside it opens four swatches for the
accent color. The ⚙️ button and its dialog are removed. The sign-in page gets the switch too.
Still browser-only preferences (`rp_themeMode`, `rp_themeColor` in localStorage), so saved
choices carry over. No API or package change.

**As built.** As planned. Page headers keep clear of the corner with extra right padding from
`md` up. Tested: Chromium at 1280 x 800 and 390 x 844 (toggle and accent saved, swatches close
on a pick, Esc and a click elsewhere, no sideways scroll, 44 px buttons on the phone); 819 unit
tests. Every manual screenshot was retaken, since they all show the sidebar or a page header.

### 1.21.3: servers you set up yourself *(released 23 Nov 2026)*

**Why.** People who install RedstonePanel often already run a Minecraft server on the machine,
from their own service or a `screen` session. The panel ignored it, apart from avoiding its
port and name, so that server could only be brought in by importing a copy. Quentin asked on
23 Nov 2026 for the panel to find such servers, treat them as custom installations and allow
managing them.

**How.** Look at the running Java and Bedrock programs, keep the ones whose working folder has
a `server.properties` and isn't a panel server, and work out what started each (a systemd
service, or a terminal). List them read-only under **Found on this computer** in New server.
**Manage with RedstonePanel** stops and disables the old service, never deletes it, and writes
the panel's own unit pointing at the same folder; **Undo** reverses it from a record kept in
the panel's data folder. Never automatic. Admins only, and not through a node token.

**As built.** As planned, in `core/found.py` with the machine-specific parts behind
`core.platform` (`system.game_programs`, `services.other_control`). The first run on a real
system found three bugs, fixed before release: a foreign unit file in `/etc/systemd/system`
was taken for one of the panel's, the panel's default starting memory could be larger than the
server's own maximum, and a server that failed to start was reported as handed over. It now
keeps the server's own memory settings, waits to see the server running, and puts everything
back with the server's last output if it isn't. A server whose version the panel can't tell
runs with the Java it already used. A server started from a terminal has to be stopped by
hand and stays listed for an hour. Also in this release: a new server no longer shows the Last
crash of a removed server with the same name. Tested: `packaging/test/test-found.sh`, 43
checks on Ubuntu 24.04 in a systemd container (in the GitHub `cluster` job), and 838 unit
tests. The Windows side ships with the 1.22 preview. The manual's picture uses sample data.

### 1.21.4: quick commands in Live Chat *(released 23 Nov 2026)*

**Why.** Live Chat ran any command, but only if you knew how to write it. Quentin asked on
23 Nov 2026 for quick commands: a command builder that asks for the player, place and other
values a command needs, quick buttons for the common ones, and suggestions as in the game,
with the players who match what is typed. He liked autocomplete with a list to pick a command
from. It serves the short visit: "make Steve creative", "set it to day", "who is on".

**How.** Three ways to the same command line, all in the Live Chat tab and all under the
existing **Console & chat** permission. (1) Suggestions while typing: commands after `/`, then
a usage line with the current part highlighted and the players, selectors or fixed choices
that fit; Tab or a click fills one in. (2) A **Commands…** list that opens a form asking only
for what the picked command needs, with the exact command shown before **Run**. (3) A row of
one-click buttons for commands that take nothing. The commands are a curated list per edition
in `static/commands.json`; the logic is plain functions in `static/js/chat-commands.js`. The
only new route, `GET /api/servers/{name}/chat/assist`, says who is online and who was seen
before and, for a running Java server that answers `help` over RCON with a usage line per
command (Vanilla, Fabric, Quilt, Forge, NeoForge), which commands and game rules it really
has. It sends `list`, `help` and `help gamerule` and nothing else.

**As built.** As planned, in `core/chatassist.py` (no machine-specific code; it uses
`moderation`, `playerlog` and `backups`, which go through `core.platform`). No change to the
cluster contract: the route is under the server's own routes, and a node on an older version
simply gives no player names. Commands that are hard to take back (`stop`, `kill`, `clear`)
ask first and are never one-click. Bedrock gets no **List players** or **Save world** button
because it sends no reply to the panel. Paper and Purpur answer `help` with paged text, so
plugin commands are not suggested; game rules then follow the Minecraft version. Tested:
every Java command in the list, with and without its optional values, each item, effect and
game rule, against real vanilla 1.21.10 and 1.21.11 servers over RCON (131 command lines each,
none refused as wrong), and the same for Bedrock against a real Bedrock Dedicated Server
1.26.66.1 through its console (113 lines, none refused); `tests/test_chatassist.py` and
`tests/js/chat-commands.test.js` (13 Node tests, run by the unit tests when Node is
installed); `packaging/test/test-chat-commands.sh`, 25 checks in a systemd container with
stand-in servers that answer RCON as a real one does (in the GitHub `cluster` job); and 29
browser checks at 1280 and 390 pixels wide with sample data. Not tested with a real player
online: no game client was available, so player names came from the stand-in. The manual's
two Quick commands pictures use sample data; the Live Chat pictures were retaken.

### 1.21.5: join pages for players *(released 23 Nov 2026)*

**Why.** Getting a friend onto a server means sending the address, the version and, for a
modded server, which mods to install, and sending it again when any of it changes. Quentin
asked on 23 Nov 2026 for a web page hosted by the panel for joining its servers, with links
to the modpack that follow what the server runs. It also delivers the "read-only status page
for players" idea: players stop asking whether the server is up.

**How.** A public page per server, off by default, that shows the name, whether it's online,
the player count, the address to type, the Minecraft version and loader, and how to join. A
server made from a Modrinth modpack links to the exact pack version it runs; other mod
servers show a link the admin pastes. The pages get their own port (8444) so the panel never
has to face the internet, and that port answers nothing else. Player packs the panel builds
itself from a hand-assembled mods folder (`.mrpack`, packwiz) came in 1.21.6.

**As built.** `core/joinpage.py` holds each page's settings (`data/join-pages.json`), what
this machine knows about a server, and `public_view()`, the fixed list of fields that may
leave it. `core/joinsite.py` is the public side: a second, tiny web server on a thread of the
panel (so there is no new service to install per operating system; the port is opened
through `core.platform`). It answers only GET and HEAD under `/join`, sets no cookies, reads
no request bodies, sends a strict Content-Security-Policy and builds every page on the server
with every value escaped; its stylesheet and script are its own, with nothing loaded from the
panel. It uses the panel's certificate when the panel serves HTTPS. Who is online comes from
the game server's own status ping on this machine, reused for 30 seconds, and requests are
limited per address. The address is only ever what an admin typed (per page, or the public
address on the Port forwarding page). Pages can be unlisted (a random link), and a list at
`/join` is a separate switch. Changed from the plan: no change to the cluster contract. The
master reads a node's server through `GET /api/servers/{name}/join-info`, which is under the
server's own routes; a node older than 1.21.5 gives the type, version and whether it runs,
without player counts. A node's own join pages can't be set from the master. Everything on
the admin side is admin-only. Tested: `tests/test_joinpage.py` (24 tests: the fields that
leave, escaping, links that aren't web links, the listener starting, moving and stopping on
real ports) and `packaging/test/test-join-page.sh`, 87 checks in systemd containers with
stand-in servers that answer the status ping as a real one does (in the GitHub `cluster`
job): every route the panel has, 209 of them with its files, is tried on the public port and
gets Not found; a plugin, a modpack and a Bedrock server's page; unlisted pages; a stopped
server; a panel restart; and a master serving a node's server over HTTPS, staying up when the
node is unreachable. Not tested: a real player following a page from outside the home, the
Modrinth App button on a real desktop, and the Add server button on a real phone. The
manual's pictures are of the test container's stand-in servers.


### 1.21.6: player packs *(released 24 Nov 2026)*

**Why.** The second half of Quentin's request of 23 Nov 2026: the join page should link a
modpack that follows the mods the server uses. A server made from a Modrinth modpack already
links to that pack (1.21.5); a server whose mods were picked by hand had nothing to link.

**How.** Hash every jar in the server's mods folder, ask Modrinth which file each is, and sort
by the project's client side: required goes in, optional goes in marked optional, unsupported
(server-only) stays out, and a file Modrinth doesn't know stays out and is named on the page.
Build a `.mrpack` that points at Modrinth's own files by checksum, serve it from the join page,
and rebuild it whenever the folder changes. Offer the same list as a packwiz folder.

**As built.** `core/playerpack.py`. The admin's choices are in `data/player-packs.json`: mode
(automatic, on, off), a name, a choice per file, and client-only extras by Modrinth project.
`manifest()` is the whole pack as plain JSON, cached on the folder's names, sizes and times;
its eight-character version is a hash of what players get. Added to the plan: what a required
mod depends on (from the jars' own metadata) is required too, because Modrinth calls Fabric
API optional on the client; and **Include** on a file Modrinth doesn't know copies the jar
into the pack's `overrides` (64 MB together). Automatic means on unless the server came from
a Modrinth modpack. If Modrinth can't be reached the last manifest stands. The public side is
`/join/<link>/pack.mrpack` and `/join/<link>/packwiz/...` on the join port, under the same
rate limit. A cluster master builds a node server's pack itself from the public part of the
node's manifest, which rides in `join-info` and is checked field by field (file names,
checksums, Modrinth-only download addresses); a jar the admin chose to hand out is fetched
from the node through `GET /api/servers/{name}/player-pack/file` and checked against its
checksum. No change to the cluster contract. The Mods tab has a third view, **Player pack**
(Mods & plugins permission). `RP_MODRINTH_API` points the panel at a stand-in for tests.
Tested: `tests/test_playerpack.py` (17 tests), `packaging/test/test-player-pack.sh` (55
checks in systemd containers, in the GitHub `cluster` matrix), and the sorting against
Modrinth's real answers for six mods. Not tested: importing the pack into Modrinth App or
Prism Launcher on a real desktop, and packwiz-installer.

### 1.21.7: a Visual Editor for settings files on the Files tab *(released 24 Nov 2026)*

**Why.** Configuration has a Visual Editor for `server.properties`, but every other settings
file (a plugin's `.properties` or `.cfg`, `eula.txt`, a second properties file) opened on the
Files tab as plain text. Quentin asked on 23 Nov 2026 for a visual editor for any file that
resembles a property file, like the one used for server properties, with a way to switch
between it and the current editor.

**How.** Nothing new on the server: the Files tab's editor already reads and saves the text
under the **Files** permission, and the form is another view of that text. When every line of
a file is blank, a comment, a `[section]` heading or `name=value`, a **Visual Editor** /
**Raw Text** switch appears above it. The form uses the same cards as Configuration (now one
shared function): True/False lists, the choices for `difficulty` and `gamemode`, number boxes,
text boxes and a filter. A change in the form rewrites that one value in the text, so comments,
blank lines, order, spacing and Windows line endings are kept and the two views cannot differ.

**As built.** `static/js/propfile.js` (plain functions) and the editor in `app.js`; no route,
no Python and no change to the cluster contract, so it works on nodes and on Windows as it is.
`.properties` files are read and written with Java's escapes, as `core/properties.py` does;
other files are plain `name=value`. A file with one line that does not fit (a value continued
with a backslash, a name with spaces) is not offered the form, and one that stops fitting while
you type says which line. `.yml`, `.json`, `.toml` and scripts always open as text. The form
does not add or remove settings; Raw Text does. Tested: `tests/js/propfile.test.js` (9 Node
tests) and `tests/test_propfile.py`, which also checks that values written by the form are read
back the same by `core/properties.py`; 24 browser checks at 1440, 1280 and 390 pixels wide in
both themes with sample data (form to text, text to form, save, filter, sections, a file that
stops fitting, Configuration's own editor); and the .deb installed in a systemd container with
a stand-in Java server, edited in a browser: `server.properties` saved with Java's escapes and
read back by Configuration, a `.cfg` with sections, `eula.txt`, each save logged as `[FILES]`.
That run found that a text box turns Windows line endings (CRLF) into LF, which the old text
editor did too; the editor now remembers a file's line endings and saves them back. The
manual's picture uses sample data.

### 1.21.8: security fixes from code scanning *(released 24 Nov 2026)*

**Why.** GitHub code scanning (CodeQL), set up on 24 Nov 2026, and the review of its 212 alerts
that Quentin asked for found real problems along with the false alarms. Quentin chose to ship
the fixes on their own as 1.21.8 ("the best, not the quickest"), with join pages through a
proxy moving to 1.21.9.

**How.**
- **Player names reach the console checked** (!129). Configuration → Players took any text as a
  Bedrock player's name and, when the Xbox lookups failed, typed it into the server's console
  with a line break in it (so it could run any command), with the server started as root.
  Names and IDs are now checked like the whitelist's, and that lookup runs the server as
  `minecraft`. !133 fixes a name clash in that change that cost Java players their UUID.
- **Server names and world names** (!130). Every route with a server name in it answers 404
  before anything else unless the name is a `minecraft*` unit name, a backup's world name must
  be a plain path inside the server folder, and the website's demo console escapes its text.
- **The panel works in server folders as the game account.** The panel runs as root, the game
  account owns its servers' folders, and the server or a plugin can put a link there
  (`server.properties` → `/etc/shadow`, `level-name=../../etc`). Plain `open()`, `rename()` and
  `shutil` calls as root followed such links, so settings, operators, the whitelist, a server or
  world download, a world rename, the Live Chat log and installs could read or change files
  anywhere. `core/files.py` (Files tab, SFTP) already opened each path step with `O_NOFOLLOW`;
  everything else now runs inside `system.game_files()`, which switches only the calling
  thread's file-access user and group to `minecraft` (Linux `setfsuid`/`setfsgid`), so a link
  reaches only what the game account could reach anyway and new files belong to it. The
  panel's own log and data are written with `system.panel_files()` from inside such a block.
  Downloads leave links out; a world outside the folder is refused; `chown -R -h` and
  `chmod +x` no longer follow a link; staging folders for installs and updates are made by
  the game account. On the first start of 1.21.8, files the panel wrote in server folders as
  root before are handed to `minecraft`, or it couldn't change them any more. A file the game
  account can't open is answered with 403 and logged as `[SECURITY]`.

**As built.** `core/platform/linux/system.py` (`game_files`, `panel_files`; the fake platform
has both, doing nothing), used in `manager`, `javaservers`, `updates`, `backups`, `convert`,
`modpacks`, `whitelist`, `playerlog`, `transfer` and `archive.zip_tree`. New code that touches
a server folder outside `core/files.py` must do it inside `system.game_files()`, must not start
threads there (they would inherit the switch), and needs the folder to belong to `minecraft`
first. Tested: `tests/test_game_files.py` (root only: links to root files are refused, new
files belong to `minecraft`, other threads keep root), `tests/test_player_roles.py`,
`packaging/test/test-game-files.sh` (27 checks; 16 of them fail on 1.21.7: it plants links
as `minecraft` and checks that settings, operators, whitelist, downloads, world rename and
Live Chat never read or change a root-only file), the install test and `test-bedrock-update.sh`.

### 1.21.9: join pages through a proxy (HAProxy or Cloudflare) *(released 24 Nov 2026)*

**Why.** Join pages (1.21.5) have a port of their own, 8444, to forward on the router. Quentin
asked on 24 Nov 2026 to also let HAProxy (his runs on pfSense) route to them, and then
Cloudflare. A proxy already reaches the pages, since they use relative links, but three things
went wrong behind one: every player looked like the proxy and shared one rate limit, the panel
showed `address:8444` links, and the port couldn't be closed to everyone but the proxy.
He chose 1.21.8, after the visual editor and before the Windows preview; it moved to 1.21.9 when
1.21.8 became a security-only release (24 Nov 2026).

**As built.** A **Through a proxy (HAProxy or Cloudflare)** card in Join pages: the proxy's
address (requests from it may name the player in `X-Forwarded-For`, read from the right and
skipping proxies; anyone else's header is ignored), the address players open (used for the
panel's links), **Cloudflare's proxy (orange cloud)** (requests from Cloudflare's published
networks may name the player in `CF-Connecting-IP`; `core/cloudflare.py` ships the list and
refreshes it from cloudflare.com daily, checking it before use) and **Only through the proxy**.
It shows HAProxy and Cloudflare Tunnel lines to copy, filled in. Nothing changes on the public
pages and the cluster contract is unchanged. Tested: unit tests for the address rules, the
settings and the Cloudflare list; `test-join-page.sh` runs a real HAProxy on exactly the lines
the panel offers (pages and health check through it, a separate limit per player, a forged
header ignored, the port closed to others). Not tested: a real Cloudflare Tunnel or proxied
domain.

**Also: upgrades leave game servers running.** Upgrading the 1.21.6 test build on Quentin's
Ubuntu 25.04 panel restarted one of his game servers: Ubuntu's `needrestart` restarts services
that still use an old copy of a library after any apt install. The package now installs
`/etc/needrestart/conf.d/redstonepanel.conf`, which tells needrestart to leave `minecraft*` and
`phantom-minecraft*` services alone; they pick up new libraries at their next restart.
`test-needrestart.sh` runs needrestart in automatic mode against a stand-in game server, a
stand-in Phantom proxy and an ordinary service that all use a deleted library: only the
ordinary one is restarted, and without the rule the game server would have been.

### 1.21.10: a terminal in the panel *(released 24 Nov 2026)*

**Why.** Quentin asked on 24 Nov 2026 for a terminal inside the panel, so an admin can fix
something on the machine without a separate SSH program, from any browser or phone. It has to
work without giving anyone the panel's own root. Placed after 1.21.9 (join pages through a
proxy) and before 1.22 (Windows preview), at Quentin's choice: it is Linux-only, so it doesn't
wait for Windows.

**How.**
- **Who.** A **⌨️ Terminal** button on the Dashboard (and on each member of the Cluster page)
  for panel **admins**, only on a Linux machine and only when **Panel Settings → Terminal** is
  on (off by default). `user` accounts and trusted-LAN visitors never get one, and the API
  refuses them (`core/grants.py`: admin rules).
- **As which account.** The shell runs as a Linux account, never as the panel. An admin who
  signed in **with their Linux account** gets a shell as that account, with no prompt while the
  sign-in is recent (**Skip the password for**, 15 minutes by default) and that account's
  password after that. A **panel-only admin** gets a `login:` prompt and types a Linux account
  and its password. Accounts are never linked by matching names: any admin can create a panel
  account, so a name match would let them become any Linux user. `root` only when **Allow
  logging in as root** is on; `nologin` accounts (such as `minecraft`) never.
- **Checks.** The password is checked through PAM (as the panel's sign-in does) and never
  reaches the shell or a log. With panel two-factor on, the code is asked after the password.
  Wrong passwords count per account and per address in the sign-in failure store: 5 in 10
  minutes blocks for 10 minutes, with 2 seconds after each.
- **The shell.** `login -f -h <address> <account>` on a new pseudo-terminal (the
  `core/platform` area `terminal`), so the account gets its own groups, home, limits, PAM
  session (in the system log) and logind session, like a console login. The panel checks that the foreground process
  runs as that account and closes it otherwise.
- **The browser.** xterm.js 5.5 (MIT, vendored in `static/vendor/xterm`, loaded only when a
  terminal opens) over a websocket. `POST /api/terminal/ticket` gives a one-use ticket for 30
  seconds (cross-site checks apply); the websocket checks the ticket, the session cookie and its
  Origin. HTTPS only, except from the machine itself. A phone gets a key row (Esc, Tab, Ctrl,
  arrows).
- **Limits.** Closes after N minutes with nothing typed (15 by default, warning a minute
  before), after 12 hours, when the admin signs out (sign-out now also stops that cookie
  opening terminals) or stops being an admin, and when the terminal is turned off. 3 per
  admin, 10 per machine. **Open now** in Panel Settings lists them with **End**.
- **Logging.** `[TERMINAL]` lines for every open, close (with why) and failed login; what is
  typed is never recorded.
- **Never on the join port.** The join pages' server has no `/api` and accepts no websocket.
- **Clusters.** A node allows a terminal from the master only when its own admin turns on
  **Allow a terminal from the cluster master** (the master can't change a node's settings). The
  master copies bytes between the browser and the node's `/api/terminal/node-ws` (its token);
  the node shows its own prompt and checks its own passwords. `CLUSTER_API` 8.
- **Command line.** `redstonepanel terminal on|off`.

**Size.** About 1.18's: one feature, one new dependency (`python3-websockets`, in Ubuntu's and
Debian's archives; uvicorn needs it for websockets), one vendored library.

**As built.** `core/terminal.py` (tickets, sessions, the login prompt, the relay),
`core/platform/linux/terminal.py` (PAM check, `login -f` on a pseudo-terminal, foreground uid
check; a fallback without `login` sets groups and ids itself) and a fake side for tests,
`static/js/terminal.js`. Session tokens gained a random part so two sign-ins in the same second
differ, and sign-out marks that cookie signed out. Ubuntu 24.04's `login` writes no wtmp entry,
so the logins are checked in the journal (`session opened for user`). Tested by
`tests/test_terminal.py` (33 tests) and `packaging/test/test-terminal.sh` (46 checks in two
systemd containers: the button, the prompt, a linked sign-in, wrong passwords and the block,
root refused, `sudo`, idle close, sign-out, limits, the node opt-in through the master, and no
websocket on the join port).

### 1.23.1: a port check *(released Nov 2026)*

Asked for by Quentin on 25 Nov 2026 ("a port audit to identify and fix port conflicts outside of
the expected use cases"), after 1.23 so Bedrock and Phantom on Windows are covered from the start.

**Why:** the panel picks free ports when it makes a server and moves known mods off shared
ports (1.21.9), but a home server is shared with other things: an old server started by hand, a
Docker container, another panel, a mod nobody told the panel about, a server.properties edited
by hand. Then a server crash-loops on a taken port and the admin has to find out with `ss` who
holds it.

**How:** `core/portaudit.py` puts the ports every server needs (game, IPv6, Query, RCON,
gameplay, known mods) and the panel's own (web, join pages, SFTP, Phantom) next to every
listening socket from `system.listeners()` (Linux: `/proc/net/*` and each process's open files;
Windows: `Get-NetTCPConnection`/`Get-NetUDPEndpoint` and the process and service lists) and
`docker ps`. It fixes by itself only what nobody outside the machine relies on (RCON, Query,
known mod ports, an unknown mod's port when exactly one config line sets it); a game port moves
on the admin's click; another program is named and never touched. A start from the panel is
refused while another program holds the game port. The report sits at the top of Port
forwarding (each member's comes with its rows, so no new node route), in Needs attention, behind
`redstonepanel ports`, and in the `port_conflict` webhook. **Check the router** reads the
router's forwards over UPnP. Windows reserved ranges stay 1.23's (`system.reserved_ports()`) and
are avoided by Move.

**Size:** medium. One core module, three platform functions, a card on an existing page.

**As built (1.23.1):** `core/portaudit.py` with `system.listeners()`, `docker_ps()`,
`ephemeral_ports()` and `can_bind()` on Linux, Windows and the fake platform. The bind probe
was added after the 1.23 work found that WSL mirrored networking holds ports no socket list
shows. `modports.resolve(held=)` moves known mod ports off outside holders. The check runs in
`manager.control_server` (refuses the start) and in the firewall sweep (moves, `port_conflict`
webhook, state in `DATA_DIR/port-check.json`). The report rides on the existing
`/api/port-forwarding/local` node route; new master routes `GET /api/port-check`,
`POST /api/port-check/router` (UPnP `GetGenericPortMappingEntry`) and
`POST /api/servers/{name}/ports/move`, all admin. The sidebar keeps the name "Port forwarding".
Tested by `tests/test_portaudit.py` (24 tests), `packaging/test/test-port-check.sh` (31 checks
in a systemd container, in release.yml), a Port check section in `test-windows.py` (Windows
Server 2022 and 2025) and a test build on a live panel with four servers.

### 1.23.2: removing the panel without leaving servers behind *(released Nov 2026)*

**Why.** Uninstalling the package (`apt remove`) left every game server running and enabled with
nothing to manage it, and the panel's backup, restart and update timers firing a command that no
longer existed. Quentin found one such server, left by an old Linux panel inside WSL, still
holding TCP 19132 days later and silently stopping the Windows panel's Bedrock server from
accepting players. Nothing on screen said how to remove the panel properly.

**How.** `core/uninstall.py`, **Panel Settings → Remove RedstonePanel** (linked from every
server's **Maintenance** tab) and `sudo redstonepanel uninstall` follow the same steps: back up
every server, choose for each one (**stop it, keep its files**, recommended, which is Uninstall
with Keep World; **keep it running without the panel**, Linux only; **delete it**, after a backup
from the last 24 hours and typing DELETE, backups kept unless asked), then remove the package
(**Uninstall now** runs `redstonepanel remove-package` in a transient unit). A new prerm runs
`redstonepanel uninstall-hook` on remove: it never stops a server nobody chose for, but turns off
the panel's own timers and services (schedules kept) and prints every server left running or
starting at boot with the command to stop it. The panel turns those servers' schedules back on
when it is installed again. The postinst message and the Windows installer's last page say how
to remove the panel safely. Size: S.

**As built (1.23.2):** as above. The plan of servers to keep is `DATA_DIR/uninstall.json`;
`run.py` calls `uninstall.after_reinstall()` on start. Keep running is refused on Windows (its
services run through the panel's own program, which the MSI removes). Tests: 17 unit tests
(`tests/test_uninstall.py`) and `packaging/test/test-uninstall.sh`, 30 of 30 checks in a systemd
container (in the GitHub release workflow's cluster job), also run on Superman; the overview
and the refusals were checked read-only against the real servers of the Windows panel.

### 2.0: an API for scripts and bots

30. **API tokens.** The API already exists (FastAPI, with `/docs`) and the cluster code already
    has tokens limited by a route allowlist. Reuse that: admin-created tokens with a named set
    of allowed routes and, after 1.18, the servers they may touch. Shown once, revocable, and
    logged under the token's name. Per-server permissions moved to 1.18; if nothing else joins
    this release, it can ship as a point release after 1.24 instead.

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
- ~~**Ubuntu 26.04 and 26.10, Debian 13 and ARM64 (Raspberry Pi)** for Java servers.~~ *Scheduled as
  1.16.9 (item 17 above).*

**For players**

- ~~**A read-only status page**~~ *(built as the join pages, 1.21.5)*.
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
