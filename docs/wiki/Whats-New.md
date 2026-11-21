# What's new

The highlights of each RedstonePanel release, newest first. Each version links to its full
release notes. To update, run `sudo apt update && sudo apt upgrade`, or click **Update now** on
the dashboard.

*Maintainers: add each release at the top of this page in the same merge request as its
`release-notes/vX.Y.Z.md`.*

---

## 1.16.11: backups off the machine, without a cluster *(November 2026)*

- **Copy backups to an SSH server or S3-compatible storage**, set up under **🛡️ Settings →
  Backup storage** and chosen per server on its **Backups** tab. Each copy is checked with
  SHA-256.
- **Keep backups by age** ("14 days") as well as by count, for local backups and for each
  storage.
- **Restore from a copy**, or bring a server back on a new machine with **New Server → From
  backup storage**.
- **Bedrock firewall fix:** servers using Bedrock's default NetherNet transport get TCP and
  UDP 7551 opened, and the panel follows **server.properties** edited by hand.

[Release notes](../../release-notes/v1.16.11.md)

## 1.16.10: a panel that works on a phone *(November 2026)*

- **Every page fits a phone:** a ☰ menu instead of the sidebar, big **Start**, **Restart** and
  **Stop** buttons, tables as cards and full-screen dialogs.
- **Install it as an app** from your phone's browser over HTTPS, so it opens like any other
  app.
- **📱 Open on your phone** on the desktop shows a QR code of the panel's address.

[Release notes](../../release-notes/v1.16.10.md)

## 1.16.9: newer Ubuntu, Debian, ARM64 and Raspberry Pi *(November 2026)*

- **Ubuntu 26.04 LTS and 26.10, Debian 13**, as well as Ubuntu 24.04, on x86-64 and ARM64.
- **Raspberry Pi 4 or 5** with the 64-bit Raspberry Pi OS, for Java servers. See
  [On a Raspberry Pi](Installation.md#on-a-raspberry-pi) for memory, storage and cooling.
- **Bedrock is x86-64 only**, so it's greyed out on ARM64 machines and nodes; Bedrock players
  can join a Java server through Geyser.
- **The Java versions your system packages** (Debian has 21 and 25), with a note when a
  Minecraft version needs one it doesn't have.

[Release notes](../../release-notes/v1.16.9.md)

## 1.16.8: backups on another cluster node *(November 2026)*

- **Also keep a copy on** another cluster member, from a server's **Backups** tab on the
  master. Each copy is checked with SHA-256 and kept to the same number as the server's own.
- **Restore from a copy** even after the original machine is gone: **Use a copy of another
  server's backup…** on a new server, then **Put back** and **Restore**.
- A failed copy shows under **Needs attention** and can send a webhook alert.

[Release notes](../../release-notes/v1.16.8.md)

## 1.16.7: two-factor sign-in *(November 2026)*

- **A code from your phone after your password**, for panel and Linux accounts. Set it up
  under **🔑 Password** by scanning a QR code with any authenticator app.
- **Ten recovery codes** for a lost phone, and **Reset two-factor** for admins
  (`sudo redstonepanel reset-2fa NAME` on the server).
- **Require two-factor for admins**, an optional setting under **🛡️ Settings → Sign-in**.

[Release notes](../../release-notes/v1.16.7.md)

## 1.16.6: a console that makes room *(November 2026)*

- **The console no longer hides the page.** While it's open, the page beside the sidebar ends
  above it, so buttons near the bottom of a tab can still be reached, and the sidebar's buttons
  stay visible.
- It's at most a third of the window's height, and when a click opens it, the button you
  clicked is scrolled back into view.

[Release notes](../../release-notes/v1.16.6.md)

## 1.16.5: a gameplay port for each Bedrock server *(November 2026)*

- **Several Bedrock servers on one machine work again.** Since 1.16.4 every new Bedrock server
  used UDP 19132 for gameplay, so a second one clashed with the first. Each now gets its own,
  on the same number as its port (19132, 19134, …), which is also easy to forward on a router.

[Release notes](../../release-notes/v1.16.5.md)

## 1.16.4: Bedrock settings fix *(November 2026)*

- **New Bedrock servers keep all their settings.** A newly created Bedrock server's
  `server.properties` had only its ports, so Configuration showed almost nothing to edit. It
  now has every setting from the official Bedrock download.

[Release notes](../../release-notes/v1.16.4.md)

## 1.16.3: a server list that folds *(November 2026)*

A long server list, especially on a cluster, is now easy to tidy.

- **Click any heading** in the sidebar (Java Edition, Bedrock Edition or a cluster node) to fold
  it. A folded heading shows how many of its servers are online, and the browser remembers it.
- **Each cluster node's servers** get their own Java and Bedrock headings, like this machine's.

[Release notes](../../release-notes/v1.16.3.md)

## 1.16.2: kick and ban, and a new look *(November 2026)*

Remove or ban a player with a click, and RedstonePanel now looks like itself.

- **Kick, ban and unban** in **Configuration → Whitelist & Access**: kick anyone online, ban
  and unban on Java (running or stopped), a **Banned** list, and Kick/Ban on each access log row.
  Bedrock has no ban list, so it gets kick only. By name, never by IP address.
- **A new look:** an original RedstonePanel logo and favicon, and warm stone greys with
  redstone-red accents in both the dark and light themes. **Redstone Red** is the new default
  accent; a palette you picked before is kept.
- **Stop** is now an outlined button, so it no longer looks like Restart.

[Release notes](../../release-notes/v1.16.2.md)

## 1.16.1: a setup guide for new panels *(November 2026)*

A new panel now shows you how to get to a safe setup, right on the dashboard.

- **Set up RedstonePanel** (admins): four steps to create your first server, put every server on
  a backup schedule, choose HTTPS (or keep HTTP on a network you trust) and get crash alerts in
  Discord or any webhook. Each step opens the place where it's done.
- **Steps tick themselves** from what the panel can see, so work done elsewhere counts, and an
  upgraded panel only shows what's missing.
- **Hide guide** when you're done; **Panel Settings → Setup guide** brings it back.

[Release notes](../../release-notes/v1.16.1.md)

## 1.16.0: move a server to another machine *(November 2026)*

Move a server to another machine in your cluster, or copy it, from the master's cluster page.
Nothing is deleted until you say so.

- **Move…** on every server of the 🌐 Cluster page (admins). The server stops, its whole folder
  is streamed to the other machine and checked byte for byte, and it starts there with the same
  name and ports when they're free, the Java it needs, its memory, backup and restart schedules
  and Phantom.
- **Nothing is lost.** The old machine keeps its files as a removed server marked "moved to …"
  until you click **Delete the old copy**. A move that fails undoes itself and starts the server
  where it was.
- **Copy…** a server, to another machine or the same one, without stopping it.
- **Templates** can be copied from the master to a node and from a node to the master.

[Release notes](../../release-notes/v1.16.0.md)

## 1.15.0: manage every node from the master *(November 2026)*

Manage every node's servers from the cluster master: every tab, new servers on any node, and
real certificates for a master on the internet.

- **Every server, every tab, from the master.** Node servers are listed in the master's sidebar
  under the node's name and open with Configuration, Backups, Files, Mods, Java, Phantom and the
  rest. Uploads and downloads work through the master too.
- **Rights follow the master's accounts.** Master admins are admins on the nodes and master
  users are users. Both panels log each change as `[CLUSTER] user@master`.
- **New Server → Where.** Create a server on any node. Its template is copied over first, and
  the Java it needs can be installed there.
- **Real certificates** (certbot) for a master on the internet, and nodes with public
  certificates can join.
- **Automatic certificate renewal** 30 days before expiry, and **nightly encrypted cluster
  backups** (keeps 7).

[Release notes](../../release-notes/v1.15.0.md)

## 1.14.2: who joined, who was turned away *(20 November 2026)*

See who joins your servers, who the whitelist keeps out, and edit the whitelist from the panel
(**Configuration → Whitelist & Access**). Names and IDs only, never addresses.
[Release notes](../../release-notes/v1.14.2.md)

## 1.14.1: NetherNet-aware firewall and Phantom *(20 November 2026)*

The panel knows about Bedrock's NetherNet transport: it opens the right firewall ports, checks
them on the dashboard, and doesn't offer Phantom where it can't help.
[Release notes](../../release-notes/v1.14.1.md)

## 1.14.0: a catalog of mods and plugins *(20 November 2026)*

**Find mods** and **Find plugins** became a catalog: bundles, a hand-picked starter set,
categories and sorting, and a details view that says exactly what Install will do.
[Release notes](../../release-notes/v1.14.0.md)

## 1.13.2: Phantom from the panel *(20 November 2026)*

Bedrock servers on any port can show up under **Friends → LAN Games**: the panel installs and
runs Phantom LAN proxies for them. [Release notes](../../release-notes/v1.13.2.md)

## 1.13.1: Java when it's needed *(20 November 2026)*

Java is installed when a server needs it, and only the versions your servers use stay
installed. The panel can update itself from the dashboard.
[Release notes](../../release-notes/v1.13.1.md)

## 1.13.0: Modrinth and Hangar *(19 November 2026)*

Find, install and update mods and plugins from Modrinth and Hangar, inside the panel.
[Release notes](../../release-notes/v1.13.0.md)

## 1.12.0: drop in mods and plugins *(19 November 2026)*

Add mods, plugins and Bedrock add-ons by dropping them on the server. Each is checked and put
in the right place. [Release notes](../../release-notes/v1.12.0.md)

## 1.11.0: host dashboard *(18 November 2026)*

See the whole machine at a glance: the panel's empty screen became a dashboard with a
**Needs attention** list. [Release notes](../../release-notes/v1.11.0.md)

## 1.10.0: edit files *(18 November 2026)*

A file manager and text editor for each server's folder: plugin configs, `ops.json`, mods and
more. [Release notes](../../release-notes/v1.10.0.md)

## 1.9.0: more than vanilla *(18 November 2026)*

Paper and Fabric servers, on the Minecraft version you choose, each with the Java it needs.
[Release notes](../../release-notes/v1.9.0.md)

## 1.8.1: only admins delete *(18 November 2026)*

Only admins can uninstall or purge a server. [Release notes](../../release-notes/v1.8.1.md)

## 1.8.0: know what's happening *(18 November 2026)*

Discord and webhook notifications when a server crashes, scheduled restarts that warn players,
and crashed Bedrock servers come back by themselves.
[Release notes](../../release-notes/v1.8.0.md)

## 1.7.0: scheduled backups *(18 November 2026)*

Back up worlds on a schedule, and put one back with a click.
[Release notes](../../release-notes/v1.7.0.md)

## 1.6.0: restore removed servers *(18 November 2026)*

Bring back a server you uninstalled with **Keep World**.
[Release notes](../../release-notes/v1.6.0.md)

## 1.5.0: clusters *(17 November 2026)*

Run several RedstonePanels from one: a master panel sees and controls the game servers on the
others. [Release notes](../../release-notes/v1.5.0.md)

## 1.4.0: Bedrock connection diagnostics *(17 November 2026)*

Shows why Bedrock players can't connect, including Phantom proxies the panel didn't install.
[Release notes](../../release-notes/v1.4.0.md)

## Earlier

1.0.0 to 1.3.1 (14–16 November 2026): the first releases, with Java and Bedrock servers as
systemd services, live chat over RCON, the `server.properties` editor, templates, export,
importing `.zip`, `.tar.gz` and `.tar` backups, and panel names. See
[all releases](https://github.com/qrussell/redstonepanel/releases).
