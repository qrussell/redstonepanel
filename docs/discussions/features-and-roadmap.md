# RedstonePanel: what it does today, and what's coming next

Hi everyone 👋

RedstonePanel is a web panel for running Minecraft servers on your own Ubuntu machine. Each
Java or Bedrock server is a plain systemd service, there's no Docker and no database, and the
panel installs from a signed apt repository. This post is a tour of what the panel already
does (as of **1.16.1**) and what's planned next. Your feedback decides the order, so please
reply with what you'd use, what's missing, or what gets in your way.

---

## ✅ What's in the panel today

### Servers
- **Java and Bedrock**, side by side. Java servers come as **Vanilla**, **Paper** (plugins)
  or **Fabric** (mods), on whichever Minecraft version you pick.
- **The right Java for each server**, installed when it's needed. A Bedrock-only host never
  gets Java. The dashboard shows which servers use which Java.
- **Create, start, stop, restart and update** from the browser. Every server is a systemd
  unit with `Restart=on-failure`.
- **Templates** for servers you make often.
- **Import and export:** take a server out as a zip, or bring one in from `.zip`,
  `.tar.gz` or `.tar`.
- **Restore removed servers:** uninstall with **Keep World** and bring the server back
  later under the same name.

### Playing and managing
- **Live chat and console:** RCON for Java, screen for Bedrock.
- **A `server.properties` editor**, a permissions editor, and **player roles**
  (operator, member and so on).
- **Whitelist & Access** *(new in 1.14.2)*: see who's on the whitelist or allowlist,
  allow or remove players, and see who was **turned away** and why. Every server gets an
  access log of joins, leaves and refused logins. It keeps names and IDs only, never IP
  addresses.
- **A file manager** for each server's folder: browse, edit configs, upload by drag and
  drop, download, rename and delete.

### Mods, plugins and add-ons
- **Drop in** `.jar`, `.mcpack` or `.mcaddon` files. Each one is checked and put in the
  right place. The panel warns about the wrong Minecraft version or missing dependencies.
- **A catalog from Modrinth and Hangar**, with bundles, a hand-picked starter set,
  categories and details. Dependencies are installed for you.
- **Updates for mods and plugins:** see which have updates and update them all at once.
  The panel backs up the world first, and the last update can be undone.

### Keeping worlds safe
- **Scheduled and on-demand backups** with retention. Copies are consistent while the
  server runs (save-off/save hold). Schedules are systemd timers, so they run even when
  the panel is stopped.
- **Restore a world** from any backup.
- **Scheduled restarts** that warn players in game first.

### Knowing what's going on
- **A host dashboard:** CPU, memory, disks and network, plus each server's players, CPU,
  memory and disk use.
- **Needs attention:** one list of anything that needs a look, such as a crashing server, a
  failed backup, a closed firewall port, an expiring certificate or players being turned
  away.
- **Notifications to Discord or any webhook:** crashes, starts and stops, backups, and
  (opt-in) players joining or being turned away.
- **A panel activity log** of who did what.

### Networking
- **Firewall rules kept in step:** with `ufw`, each server's ports are opened for you,
  including NetherNet ports for Bedrock.
- **Phantom LAN proxies**, so a Bedrock server on another port shows up under LAN Games.
- **Bedrock connection diagnostics.**

### The panel itself
- **A setup guide** on a new panel's dashboard *(new in 1.16.1)*: first server, backups,
  HTTPS and alerts, each step ticked from what the panel can see.
- **HTTPS:** self-signed (with its own CA), your own certificate, or plain HTTP on a
  trusted LAN.
- **Sign-in** with Linux accounts (through PAM) or panel-only accounts with admin/user
  roles. **Trusted-LAN sign-in** is optional.
- **Clusters:** one panel is the master and manages every node's servers with all their tabs,
  creates servers on any node, and can use a real (certbot) certificate on the internet. There
  are join codes, a cluster CA with automatic renewal, and encrypted cluster backups (also
  nightly) that let you promote another panel if the master is lost. **Move or copy** a
  server to another machine of the cluster *(new in 1.16)*.
- **Dark and light themes.** A CLI (`sudo redstonepanel …`) covers status, backups,
  restores and cluster tasks.
- **Installs with `apt install redstonepanel`** from the signed qrussell-repo repository.
  `apt upgrade` keeps it current.

---

## 🛣️ On the roadmap

Version numbers are targets, not promises. The guiding rule stays the same: **protect
people's worlds first**, and use systemd rather than reinventing it.

### Just shipped: 1.16.1 adds a setup guide
- **A Setup card on a new panel's dashboard:** your first server, a backup schedule for every
  server, HTTPS or HTTP on purpose, and a Discord or webhook alert. Each step opens the place
  where it's done and ticks itself when it is.

### Also new: 1.16 moves servers between machines
- **Move** a server to another cluster member. The world is streamed and checksummed, and
  the server keeps its name, ports, Java, backup and restart schedules, mods and Phantom.
  **Nothing is deleted on the old machine until you say so.**
- **Copy** a server, to another machine or the same one, without stopping it.
- **Templates** copied between the master and its nodes.

### 1.16.2: kick and ban from the panel
- **Kick** from the players list or the access log, with an optional reason.
- **Ban and unban** on Java servers, running or stopped, with a **Banned** list. Bedrock
  gets kick only, as it has no ban list.

### 1.16.3: two-factor sign-in
- **Authenticator app codes** at sign-in, with recovery codes, and an option to require them
  for every admin.

### 1.16.4: backups on another cluster node
- **A copy of each backup on another cluster member**, checked as it's copied, that you can
  restore from even if the original machine is gone.

### 1.16.5: Debian and ARM64
- **Debian 12 and 13**, and **ARM64** machines such as a Raspberry Pi for Java servers.

### 1.16.x: less downtime for big worlds
- An optional **pre-copy** while the server keeps running.
- **Find panels on your network** and add them to the cluster.

### 1.17: server performance and tuning
- **Tick health** (TPS/MSPT) for Paper, Vanilla and Fabric 1.20.3+.
- **Memory:** the heap limit next to what the server really uses, and a **Memory** setting
  to change `-Xms`/`-Xmx` safely.
- **History charts:** 24 hours at 1-minute detail and 7 days at 15-minute detail, for each
  server and the host. No database or chart library needed.
- **Suggestions based on what was measured, never applied automatically.** Examples: lower
  simulation distance when TPS drops, Lithium for Fabric, FastAsyncWorldEdit for
  WorldEdit, pre-generating with Chunky, or moving a busy server to a quieter node.
- **Alerts** for low TPS or a nearly full disk (off by default).

### 2.0: run it for other people
- **Per-server permissions** for user accounts: view, console, power, files, mods and
  settings.
- **API tokens** with a limited set of allowed routes. They're shown once and can be
  revoked.

### Groundwork along the way
- More tests and a security review of sign-in, imports and path handling.
- A **mobile pass** on the dashboard, server list and console.
- ✅ Done: a one-line installer (`curl … install.sh | sudo sh`), screenshots in the README and
  a wiki. Still to do: a short GIF.

### Ideas we're considering (tell us which matter to you)
- **Off-machine backups** to rsync/SFTP or S3-compatible storage, for panels without a
  cluster.
- **Automatic Bedrock updates**, so players on new clients can always join.
- **Whitelist changes across servers** from the cluster master.
- **Scheduled commands and announcements.**
- **Update alerts** when a new Minecraft release or Paper/Fabric build is out.
- **World tools:** upload a world, new world with a seed, pre-generate with Chunky.
- **Per-server CPU and memory limits.**
- **A read-only status page** for players, and a **Discord chat bridge**.

### Later, when someone asks for it
- Backup retention by age, and off-machine copies (rsync/S3).
- **Forge and NeoForge.**
- **Modrinth modpacks** (`.mrpack`).
- Adopting a node over SSH, and finding panels on the LAN.
- **Velocity** proxy support for networks across nodes.
- Translations, once a contributor offers one.

### Not planned (and why)
- **Docker mode:** running servers as plain systemd services is the reason the panel
  exists.
- **Load balancing or auto-migration:** a Minecraft server is one stateful process.
  Moving a server by choice (1.16) covers the real need.
- **Multi-tenant hosting or paid tiers:** it's an MIT project with one developer.

---

## 💬 Tell us
- Which roadmap item would you use first?
- What's missing from your setup? For example: a loader, a backup target, or a way you
  manage players.
- Running a cluster? What do you still have to SSH in for?

Bugs are best reported as issues. Security problems should go through a
[security advisory](https://github.com/qrussell/redstonepanel/security/advisories/new).
Thanks for running RedstonePanel! ⛏️
