# What's new

The highlights of each RedstonePanel release, newest first. Each version links to its full
release notes. To update, run `sudo apt update && sudo apt upgrade`, or click **Update now** on
the dashboard.

*Maintainers: add each release at the top of this page in the same merge request as its
`release-notes/vX.Y.Z.md`.*

---

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
