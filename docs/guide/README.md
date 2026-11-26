# RedstonePanel user manual

How to set up RedstonePanel and use each of its features, step by step. It's written for
version **1.17.0**. The screenshots come from a demo cluster: a panel called "Survival Box"
(the master) running a Paper server with plugins and a Bedrock server, and a node called
"Creative Lab" running a Fabric server with mods.

**About the screenshots:** each one was taken in the panel's dark and light theme, and on GitHub
you see the one that matches your browser or system theme (GitLab shows the dark one). To
compare, open the [screenshot gallery](#screenshot-gallery) at the end, in either theme. The
close-up of mod updates is from an earlier version.

New to RedstonePanel? Start with [Install](#1-install), then
[Create or import a server](#4-create-or-import-a-server) and [Backups](#9-backups). For file
locations and the security model, see the main [README](../../README.md); for every
command-line option, run `man redstonepanel`. This manual is also published as the
[User Manual](https://github.com/qrussell/redstonepanel/wiki/User-Manual) page of the wiki.

**How do I…**

| …? | Where |
| --- | --- |
| Install or update the panel | [Install](#1-install) |
| Run it on a Windows PC | [On Windows](#on-windows), or [with WSL](#on-windows-with-wsl) |
| Create a Java or Bedrock server | [Create or import a server](#4-create-or-import-a-server) |
| Bring in a server I already have | [Import](#4-create-or-import-a-server) |
| Talk to players or run a command | [Live Chat](#6-live-chat) |
| Change difficulty, game mode, ports… | [Configuration](#7-configuration-player-roles-and-the-whitelist) |
| Make someone an operator, or whitelist them | [Player roles and the whitelist](#7-configuration-player-roles-and-the-whitelist) |
| Back up a world, or put one back | [Backups](#9-backups) |
| Keep backups off this machine (NAS, Backblaze B2, S3) | [Off-site copies](#off-site-copies-ssh-or-s3-storage) |
| Restart a server every night | [Scheduled restarts](#scheduled-restarts) |
| Find out why a server lags | [Performance](#performance) |
| Give a server more memory, or limit it | [Memory and limits](#memory-and-limits) |
| Update a server to a newer Minecraft version | [Maintenance](#8-maintenance-templates-export-update-uninstall) |
| Turn a Vanilla server into Paper or Fabric (or back) | [Changing a server's type](#changing-a-servers-type) |
| Go back after an update broke something | [Rolling back an update](#rolling-back-an-update) |
| Keep a Bedrock server on the newest version | [Automatic Bedrock updates](#update-alerts-and-automatic-bedrock-updates) |
| Add mods, plugins or Bedrock add-ons | [Mods, plugins and add-ons](#12-mods-plugins-and-add-ons) |
| Let Bedrock players join a Java server | [Finding mods and plugins](#finding-mods-and-plugins) (the **Bedrock players** bundle) |
| Edit a plugin's config file | [Files](#11-files) |
| Get a Discord message when a server crashes | [Notifications](#notifications) |
| Give a friend access to only some servers | [What a helper may do](#what-a-helper-may-do) |
| Move files with FileZilla or WinSCP | [SFTP](#sftp-filezilla-winscp) |
| Open a shell on the machine in the browser | [Terminal](#terminal) |
| Turn on HTTPS | [Panel Settings](#17-panel-settings-name-https-sign-in) |
| Manage servers on several machines | [Clusters](#18-clusters) |
| Fix "players can't connect" or a lock-out | [Troubleshooting](#20-troubleshooting) |

**New in 1.16**

| Version | What's new | Where |
| --- | --- | --- |
| 1.23.0 | **🪟 Windows, the whole machine**: Bedrock, more loaders and modpacks, limits, off-site backups, **Update now** and Windows account sign-in. | [On Windows](#on-windows) |
| 1.21.10 | **⌨️ Terminal**: a shell on the machine in your browser, for admins, as a Linux account. | [Terminal](#terminal) |
| 1.21.9 | **🔗 Join pages** can sit behind HAProxy or Cloudflare, on your own domain. | [Join pages through a proxy](#join-pages-through-a-proxy-haproxy-or-cloudflare-1219) |
| 1.21.6 | **Player packs**: the mods players need, as one download on the join page that follows the server. | [Player packs](#player-packs) |
| 1.21.5 | **🔗 Join pages**: a public page for each server that tells players how to join, on a port of its own. | [Join pages](#join-pages) |
| 1.21.2 | Light and dark mode is a **sun/moon switch in the top right corner**, with a colored dot beside it for the accent color. The ⚙️ button in the sidebar is gone. | [The dashboard](#3-the-dashboard) |
| 1.21.0 | Groundwork for the Windows version. Nothing new on screen. | [Roadmap](../wiki/Roadmap.md) |
| 1.20.0 | **Purpur, Quilt, NeoForge and Forge** servers; **From a modpack** (Modrinth, `.mrpack`, CurseForge server packs) and pack updates. | [Create or import a server](#4-create-or-import-a-server), [Modpack](#modpack) |
| 1.19.0 | **Server type**: change a Java server between Vanilla, Paper and Fabric in place, with a preview, a backup and undo; **Roll back the last update**; backups show what made them. | [Changing a server's type](#changing-a-servers-type), [Rolling back an update](#rolling-back-an-update) |
| 1.18.0 | **Per-server permissions** for user accounts, with Moderator and Co-owner presets, and **SFTP** for FileZilla or WinSCP with passwords or SSH keys. | [What a helper may do](#what-a-helper-may-do), [SFTP](#sftp-filezilla-winscp) |
| 1.17.0 | **Performance**: tick health, memory, history charts and suggestions for every server and the machine; **Memory and limits** for the heap and hard memory and CPU limits; low TPS and full disk alerts. | [Performance](#performance), [Memory and limits](#memory-and-limits) |
| 1.16.14 | **🔀 Port forwarding**: what to forward on your internet router (pfSense and others) for every server and node; the phone menu shows every server. | [Port forwarding](#port-forwarding), [On a phone](#on-a-phone) |
| 1.16.13 | **Pre-copy while it runs** for moves of big worlds, and **Find panels on your network** when adding a node. | [Clusters](#18-clusters) |
| 1.16.12 | **Update available** alerts for every server, **automatic updates for Bedrock** (opt-in) with a backup and rollback, and Bedrock servers that always save before stopping. | [Update alerts and automatic Bedrock updates](#update-alerts-and-automatic-bedrock-updates) |
| 1.16.11 | **Off-site backup copies** to an SSH server or S3-compatible storage, retention by age, and bringing a server back on a new machine; Bedrock servers get the firewall rules they really need. | [Off-site copies](#off-site-copies-ssh-or-s3-storage), [Backup storage](#backup-storage) |
| 1.16.10 | **Works on a phone**: a ☰ menu, big Start/Restart/Stop buttons and tables as cards; **installable as an app** over HTTPS; **📱 Open on your phone** shows a QR code on the desktop. | [On a phone](#on-a-phone) |
| 1.16.9 | Runs on **Ubuntu 26.04 and 26.10, Debian 13 and a Raspberry Pi 4 or 5** (64-bit Pi OS), on x86-64 and ARM64 (Java only on ARM64), with the Java versions each system packages. | [Install](#1-install), [On a Raspberry Pi](#on-a-raspberry-pi), [Java versions](#java-versions) |
| 1.16.8 | **Backup copies on another cluster member**, made by the master, checked with SHA-256 and restorable after losing the original machine. | [Backups](#9-backups) |
| 1.16.7 | **Two-factor sign-in** with an authenticator app and recovery codes, and an option to require it for admins. | [Two-factor sign-in](#two-factor-sign-in) |
| 1.16.6 | The console makes room instead of covering the page, and keeps the sidebar visible. | [Console](#13-console) |
| 1.16.5 | Each Bedrock server gets its own UDP gameplay port (`server-udp-ports`), so several can run on one machine. | [Create a server](#4-create-or-import-a-server) |
| 1.16.4 | New Bedrock servers keep every setting from the Bedrock download, so **Configuration** lists them all. | [Configuration](#7-configuration-player-roles-and-the-whitelist) |
| 1.16.3 | Fold the server list by edition and by cluster node; a folded heading shows how many servers are online. | [The dashboard](#3-the-dashboard) |
| 1.16.2 | **Kick**, **Ban** and **Unban** players, and a **Banned** list for Java servers. | [Kick, ban and unban](#7-configuration-player-roles-and-the-whitelist) |
| 1.16.2 | The RedstonePanel logo, a stone-and-redstone theme with **Redstone Red** accents, and an icon for a phone's home screen. | [The dashboard](#3-the-dashboard), [Sign in](#2-sign-in) |
| 1.16.1 | A setup guide on a new panel's dashboard: first server, backups, HTTPS and alerts. | [The dashboard](#3-the-dashboard) |
| 1.16.0 | Move or copy a server to another machine of a cluster. | [Clusters](#18-clusters) |

Every release's highlights are on the [What's New](../wiki/Whats-New.md) page.

**Contents**

1. [Install](#1-install)
   - [On Windows](#on-windows)
   - [On Windows with WSL](#on-windows-with-wsl)
2. [Sign in](#2-sign-in)
3. [The dashboard](#3-the-dashboard)
4. [Create or import a server](#4-create-or-import-a-server)
5. [Overview](#5-overview)
6. [Live Chat](#6-live-chat)
7. [Configuration, player roles and the whitelist](#7-configuration-player-roles-and-the-whitelist)
8. [Maintenance: templates, export, update, uninstall](#8-maintenance-templates-export-update-uninstall)
   - [Update alerts and automatic Bedrock updates](#update-alerts-and-automatic-bedrock-updates)
9. [Backups](#9-backups)
   - [Off-site copies (SSH or S3 storage)](#off-site-copies-ssh-or-s3-storage)
   - [Scheduled restarts](#scheduled-restarts)
10. [World & Maps](#10-world--maps)
11. [Files](#11-files)
12. [Mods, plugins and add-ons](#12-mods-plugins-and-add-ons)
13. [Console](#13-console)
14. [Templates](#14-templates)
15. [Restore a removed server](#15-restore-a-removed-server)
16. [Users](#16-users)
17. [Panel Settings: name, HTTPS, sign-in](#17-panel-settings-name-https-sign-in)
    - [Notifications](#notifications)
    - [Backup storage](#backup-storage)
18. [Clusters](#18-clusters)
19. [Command line](#19-command-line)
20. [Troubleshooting](#20-troubleshooting)

At the end: the [screenshot gallery](#screenshot-gallery), every screenshot in the light and the dark theme.

---

## 1. Install

RedstonePanel runs on **Ubuntu 24.04, 26.04 or 26.10**, **Linux Mint 22 or 23**, **Debian 13** and
**64-bit Raspberry Pi OS** (on a Raspberry Pi 4 or 5), on x86-64 and ARM64 machines. On ARM64, including every
Raspberry Pi, it runs **Java servers only**, because Mojang makes Bedrock Dedicated Server for
x86-64 only (see [On a Raspberry Pi](#on-a-raspberry-pi)). Ubuntu 25.04 and 25.10 still work
but no longer get updates; Debian 12, Linux Mint 21 and 32-bit systems aren't supported. From 1.22 there is also
a Windows installer (see [On Windows](#on-windows)). On Linux, the quickest way is
the installer, which adds
the signed qrussell-repo apt repository (checking its key) and installs the panel:

```bash
curl -fsSL https://raw.githubusercontent.com/qrussell/redstonepanel/main/install.sh | sudo sh
```

Or do the same by hand, so that `apt upgrade` brings each new version:

```bash
sudo apt install -y curl ca-certificates
curl -fsSL https://qrussell.github.io/qrussell-repo/deb/qrussell-repo.gpg | sudo tee /usr/share/keyrings/qrussell-repo.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/qrussell-repo.gpg] https://qrussell.github.io/qrussell-repo/deb ./" | sudo tee /etc/apt/sources.list.d/qrussell.list
sudo apt update
sudo apt install redstonepanel
```

The package doesn't install Java. The first time you create a Java Edition server, the panel
offers to install the Java that Minecraft version needs (see [Java versions](#java-versions)),
so a machine that only runs Bedrock never gets Java.

Install `ufw` too if you want the panel to open each game server's ports in the firewall for
you: TCP for Java, and for Bedrock (NetherNet, the default since Bedrock 1.26.50, also when
server.properties has no `transport` line) TCP on its port, UDP on its `server-udp-ports` and
UDP 7551 for LAN discovery. A Bedrock server set to `transport=raknet` gets UDP on its port and
its IPv6 port instead. It follows port and transport changes you make in Configuration, and
closes the ports again when the server is removed (7551 stays open while another NetherNet
server needs it). If you edit server.properties by hand, the panel moves the rules within two
minutes or when you open the dashboard. The dashboard also names any game-port rule that no
server uses, with the command to close it.

**Updating.** When a new version is out, the dashboard's **Needs attention** says so and the
**Host** card shows **Update now** (admins). Click it: the panel updates its package with apt,
restarts, and the page reloads by itself, usually within a minute. Game servers keep running.
**Check for updates** on the Host card asks apt straight away instead of waiting for the daily
check. From a terminal, `sudo redstonepanel upgrade` does the same, or use
`sudo apt update && sudo apt upgrade`.

The panel then runs as the `redstonepanel` service on port **8080**. Open
`http://<server>:8080/` in a browser.

### On a Raspberry Pi

Use a **Raspberry Pi 4 or 5**, ideally with 4 GB of memory or more.

1. In **Raspberry Pi Imager**, choose **Raspberry Pi OS Lite (64-bit)**, write it to an SSD or
   a microSD card, and turn on SSH in the settings it offers before writing. An SSD (USB 3, or
   NVMe on a Pi 5) is faster and lasts longer than a card under world saves and backups.
2. Start the Pi, sign in over SSH, and run the installer above.
3. Open `http://<pi>:8080/` and create a **Paper** server: it uses noticeably less CPU than
   Vanilla, which matters on a Pi.

**Bedrock** isn't offered: the **New Server** dialog shows it greyed out with the reason, and a
Bedrock backup can't be imported. To let Bedrock players join, add the **Bedrock players**
bundle (Geyser) to a Java server (see [Finding mods and plugins](#finding-mods-and-plugins)).
In a cluster, a Pi can be the master or a node next to an x86-64 machine that runs the Bedrock
servers; a Bedrock server can't be moved to a Pi.

**Memory.** A new Java server may use up to 2 GB (1 GB on a machine with 3 GB or less);
**Overview → Max RAM** shows it. Leave about 1 GB for the system:

| Pi | What fits |
| --- | --- |
| 2 GB | Not recommended: one small server, which gets 1 GB. |
| 4 GB | One server at 2 GB, for about 5 to 10 players. |
| 8 GB | Two or three servers at 2 GB, or one at 4 to 5 GB. |
| 16 GB | Several servers; the CPU is the limit. |

To change a server's memory, use **Maintenance → [Memory and limits](#memory-and-limits)**.
On a machine with 3 GB or less (a 2 GB Pi reports about 1.8 GB) the panel keeps 768 MB for the
system instead of 1 GB, so the default 1 GB heap still fits. Updates and moves keep the setting.

**Java.** Raspberry Pi OS (like Debian 13) packages Java 21 and 25, which run Minecraft 1.17 and
newer. For older versions, which ask for Java 8, the panel says the Pi doesn't have it and
offers Java 21, which some old versions and modpacks don't start on.

**Cooling.** A Pi 5 needs its active cooler (or a case with a fan) to run a server all day
without slowing itself down.

### On Windows

From 1.22 RedstonePanel also installs on **Windows 11** and **Windows Server 2022 or 2025**
(64-bit Intel or AMD) with its own installer, no WSL needed. From 1.23 it runs everything one
computer needs: Java servers of every type and Bedrock servers, with backups and off-site
copies, schedules, the console, Live Chat, the file manager, add-ons, the firewall, memory and
processor limits, crash explanations, webhooks, two-factor sign-in and **Update now**. From 1.24
it is no longer a preview: a Windows PC can be a cluster's master or one of its nodes, next to
Linux machines (see [Clusters](#18-clusters)), and [SFTP](#sftp-filezilla-winscp) works there
too. The [terminal](#terminal) is Linux-only by design: on Windows, open a command prompt on
the PC.

1. Download `RedstonePanel-x.y.z-x64.msi` from the
   [latest release](https://github.com/qrussell/redstonepanel/releases/latest) and open it.
   The installer isn't signed yet, so SmartScreen says **Windows protected your PC**: choose
   **More info**, then **Run anyway**. The release lists the file's SHA-256 next to it.
2. The installer asks where servers, backups and settings are kept. The default is
   `C:\ProgramData\RedstonePanel`; the folder's name can't have a space in it.
3. Leave **Open RedstonePanel in my browser** ticked and click **Finish**. On a first install
   the browser opens a page to **choose a password** for the panel account **admin** (from 1.24;
   the link works once, for 7 days, and only for you). Type it twice and click
   **Set Password and Sign In**. If you skip it, the random password is in `first-sign-in.txt`
   in that folder (open it as an administrator); choosing your own deletes that file. Later,
   open **RedstonePanel** from the Start menu; it follows the panel if you change its port or
   turn on HTTPS. You can also sign in with your own Windows account if it is in
   **Administrators**, or in a local group named **RedstonePanel** that you make, and add
   **Windows Hello** (face, fingerprint or PIN) to skip the password; see
   [Windows Hello](#windows-hello-passkeys). Windows accounts always sign in as admins; for
   people who should only run some servers, make panel accounts under **Users**.

Java (Eclipse Temurin) is downloaded the first time a server needs it. Each game server is a
Windows service (`minecraft-<name>`) running as its own account that can reach only its own
folder; schedules are tasks under **RedstonePanel** in Task Scheduler, and game ports are rules
in the **RedstonePanel** group in Windows Defender Firewall. To update, click **Update now** on
the dashboard (from 1.23; game servers stop for about a minute), or run the newer installer.
Needs attention also warns when the PC is set to sleep, when Windows Update waits to restart it,
and when a server's port is in a range Hyper-V, WSL or Docker Desktop reserved. To remove it, use **Settings → Apps → Installed apps**. Your worlds and backups stay
in the folder from step 2. More detail is in the wiki's
[Installation](https://github.com/qrussell/redstonepanel/wiki/Installation#on-windows)
page.

If the machine already runs Minecraft servers that something else set up, **New server** lists
them under **Found on this computer** (see [section 4](#4-create-or-import-a-server)).

### On Windows with WSL

For Bedrock servers, modpacks and clusters on a Windows PC before the Windows version has them,
RedstonePanel also runs in Ubuntu under WSL. Install **Ubuntu 24.04**
(`wsl --install -d Ubuntu-24.04` in PowerShell), open it, and run the installer above inside
it. Ubuntu under WSL starts systemd by default, which the panel and its game servers need. If
`ps -p 1 -o comm=` doesn't print `systemd`, add `systemd=true` under `[boot]` in
`/etc/wsl.conf` and run `wsl --shutdown` in PowerShell.

The panel then opens in your Windows browser at `http://localhost:8080/`, and creating servers
works as usual. Players on other PCs, though, can't reach them yet: by default WSL keeps Ubuntu
behind its own private network. To let them in:

1. **Turn on mirrored networking** (Windows 11 22H2 or newer). Put this in
   `%UserProfile%\.wslconfig`, then run `wsl --shutdown`:

   ```ini
   [wsl2]
   networkingMode=mirrored
   ```

   Ubuntu then shares your PC's network address. Players connect to your **Windows PC's IP
   address** (from `ipconfig`) and the port shown on the server's **Overview**.

2. **Allow the game ports through the Hyper-V firewall** that sits in front of WSL. In an
   admin PowerShell, for example for the first Bedrock server:

   ```powershell
   New-NetFirewallHyperVRule -Name "MinecraftTCP" -DisplayName "Minecraft TCP" -Direction Inbound -VMCreatorId '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -Protocol TCP -LocalPorts 19132
   New-NetFirewallHyperVRule -Name "MinecraftUDP" -DisplayName "Minecraft UDP" -Direction Inbound -VMCreatorId '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -Protocol UDP -LocalPorts 19140,7551
   ```

3. **Allow the same ports in Windows Defender Firewall**, and make sure Windows treats your
   network as **Private**:

   ```powershell
   New-NetFirewallRule -DisplayName "Minecraft TCP" -Direction Inbound -Protocol TCP -LocalPort 19132 -Action Allow
   New-NetFirewallRule -DisplayName "Minecraft UDP" -Direction Inbound -Protocol UDP -LocalPort 19140,7551 -Action Allow
   ```

**Which ports?** A **Java** server needs TCP on its port (25565 for the first one). Current
**Bedrock** servers use the NetherNet transport: TCP on their `server-port` (19132 for the
first), UDP on their `server-udp-ports` (both in **Configuration**) and UDP 7551 for LAN
discovery. Opening only UDP 19132, as older Bedrock guides say, isn't enough. Each new server
gets its own ports, so add rules for those too. To open the panel from other PCs, allow TCP
8080 (or 8443 with HTTPS) the same way. If `ufw` is active inside Ubuntu, the panel opens the
game ports there as usual; the Windows firewalls still need their own rules.

**Keep in mind:**

- **WSL can stop Ubuntu when nothing on Windows is using it**, for example a while after you
  close the last Ubuntu window, and it only starts after you sign in to Windows. Your game
  servers stop with it. For servers that are always up, keep an Ubuntu window open, or use a
  real Ubuntu machine or VM.
- For players outside your home, forward the same ports on your router to your **Windows
  PC's** IP address.

## 2. Sign in

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/sign-in.png"><img src="images/sign-in.png" alt="Sign-in page"></picture>

There are two kinds of account:

- **Linux accounts** in the `sudo` or `redstonepanel` group sign in with their normal Linux
  username and password. They are panel admins. To let someone in without giving them
  `sudo`, run `sudo usermod -aG redstonepanel alice`.
- **Panel accounts** exist only in the panel. Admins create them under **👥 Users** (see
  [Users](#16-users)), or root creates one with `sudo redstonepanel add-user alice --role user`.
  Forgot the password and no other admin can reset it under **Users**? Run
  `sudo redstonepanel reset-password NAME` on the server (in an administrator prompt on
  Windows, without `sudo`); it signs that account out everywhere.

On **Windows** the first kind is **Windows accounts** in **Administrators** or a local group named
**RedstonePanel**, with their Windows password, and both kinds can add
[Windows Hello](#windows-hello-passkeys).

Admins can turn sign-in off for devices on your home network; see
[Panel Settings](#17-panel-settings-name-https-sign-in).

### Two-factor sign-in

Any account, panel or Linux, can ask for a second step after the password: a six-digit code
from an authenticator app on your phone (Google Authenticator, Microsoft Authenticator, Aegis,
1Password, Bitwarden and so on).

1. Click **🔑 Password** in the sidebar, then **Set Up Two-Factor**.
2. Scan the QR code with the app, or type the key under it.
3. Type the code the app shows and click **Turn On**.
4. **Save the ten recovery codes** it shows (Copy, Download, or print them). Each one signs you
   in once if you lose your phone. They aren't shown again.

From then on the sign-in page asks for a code after your password. Turning it on signs out
your other browsers, which sign in again with a code. Under **🔑 Password** you can also see
how many recovery codes are left, make new ones, or turn it off (both need a current code).

Lost your phone and your recovery codes? Another admin can click **Reset two-factor** for a
panel account under **👥 Users**, or root runs `sudo redstonepanel reset-2fa NAME` (this works
for Linux accounts too). Wrong codes count towards the same limit as wrong passwords: five
in ten minutes and that account and address wait a few minutes.

Admins can make it compulsory for every admin; see
[Panel Settings](#17-panel-settings-name-https-sign-in). A [Windows Hello](#windows-hello-passkeys)
sign-in counts as both steps.

### Windows Hello (passkeys)

From 1.24, on a panel installed on **Windows**, any account (Windows or panel) can sign in with
**Windows Hello**: your face, fingerprint or Windows Hello PIN, instead of a password. The panel
keeps only a public key (a *passkey*); the PIN or fingerprint never leaves Windows. Linux panels
keep Linux accounts, panel accounts and two-factor codes.

1. Sign in as usual, click **🔑 Password**, and under **Passkeys (Windows Hello)** type your
   password, give the passkey a name if you like (for example *Desk PC*) and click
   **Add Passkey**. Windows asks for your face, fingerprint or PIN.
2. Next time, click **Sign in with Windows Hello** on the sign-in page. No user name needed:
   the passkey says whose it is.

It counts as two-factor sign-in on its own (the PC's key plus your face, finger or PIN), so it
also meets **Require two-factor sign-in for admins**. Browsers allow passkeys only at a name,
not an IP address, and only over HTTPS or at `http://localhost`, so it works at
`http://localhost:8080` on the PC itself, or from other machines at a host name with a trusted
certificate (see [HTTPS](#17-panel-settings-name-https-sign-in)). A passkey belongs to the
address it was added at: one added at `localhost` works there only. Each account can have up to
ten. **Remove** takes one away; an admin's **Reset two-factor** (or **Remove passkeys**) under
**👥 Users**, or `redstonepanel reset-2fa NAME` in an administrator prompt, removes them all.

### On a phone

The panel works on a phone or a small tablet. Below 768 px wide:

- The sidebar becomes a menu: tap **☰** in the top bar, which also names the open server. The
  menu closes when you pick something, tap outside it or press Escape.
- On a server's page, **Start**, **Restart** and **Stop** are large buttons across the top, and
  the tabs scroll sideways.
- Tables (the dashboard's servers, backups, files, the access log) become cards, and dialogs
  fill the screen.
- Live Chat keeps its box above the on-screen keyboard, and fields don't make the page zoom in.

<p><img src="images/phone-dashboard.png" alt="The dashboard on a phone" width="260"> <img src="images/phone-menu.png" alt="The menu open on a phone" width="260"> <img src="images/phone-server.png" alt="A server's page on a phone" width="260"></p>

**Open on your phone.** On a computer, click **📱 Open on your phone** in the sidebar and scan the
QR code with the phone's camera. The code is made by the panel itself, so the address never
goes to an outside service. If you opened the panel as `localhost`, it offers this machine's
network addresses instead.

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/phone-qr.png"><img src="images/phone-qr.png" alt="The Open on your phone dialog with a QR code"></picture>

**Install it as an app.** Over HTTPS, Android's Chrome offers **Install app** (or ⋮ → **Install
app**), and on an iPhone use Safari's **Share → Add to Home Screen**. The panel then opens in
its own window with the RedstonePanel icon, without the browser's address bar. Browsers only
install apps from HTTPS sites, so on plain HTTP the phone can only add a bookmark to its home
screen; turn on HTTPS in [Panel Settings](#17-panel-settings-name-https-sign-in). If the phone
can't reach the panel, the app shows **Can't reach the panel** with **Try again**.

## 3. The dashboard

When no server is selected, the right side shows the state of this machine and everything the
panel relies on. It's what you see after signing in, and **📊 Dashboard** in the sidebar brings
it back. It refreshes every 10 seconds while the page is open.

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/dashboard.png"><img src="images/dashboard.png" alt="The dashboard on a cluster master"></picture>

- **Set up RedstonePanel** (admins) comes first on a new panel: four steps to a safe setup.
  1. **Create your first server**: opens **➕ New Server**. If the firewall `ufw` is missing or
     off, it says so here, because the panel opens game ports only through `ufw`.
  2. **Back up your worlds**: a button for each server without a backup schedule, which opens
     its **Backups** tab ([Backups](#9-backups)).
  3. **Choose how the panel is reached**: **Turn on HTTPS** opens Panel Settings at
     **Connection**. On a home network you trust, **Keep HTTP, this is my home network** ticks
     the step instead.
  4. **Get told when something breaks**: opens Panel Settings at
     [Notifications](#notifications).

  <picture><source media="(prefers-color-scheme: light)" srcset="images/light/setup-guide.png"><img src="images/setup-guide.png" alt="The setup guide on the dashboard, three of four steps done"></picture>

  Steps tick themselves from what the panel can see, so a step done from the command line or by
  another admin counts too. On a cluster node, joining the cluster ticks the HTTPS step; on a
  master, servers on the nodes count for the first step. The card goes away when every step is
  done. **Hide guide** hides it for every admin; Panel Settings → **Setup guide** shows it again,
  and is also where you can take back **Keep HTTP**.
- **Needs attention** comes first, and only lists what's wrong: a disk over 90% full, memory
  or swap nearly full, a failed or crash-looping server, a failed backup, a Java server whose
  Java isn't installed, a game port the firewall (ufw) blocks, a stopped Phantom proxy, a
  certificate expiring within 14 days, an unreachable cluster node, or a newer RedstonePanel
  in apt. Items about a server have an **Open** link.
- **CPU, Memory, Disk, Game servers**: use right now, with the load average, swap, the free
  space where the servers live, players online and network traffic.
- **History**: charts of the machine's CPU, memory, the disk holding the servers, and network
  traffic in and out, for the **Last 24 hours** (one point a minute) or the **Last 7 days**
  (15-minute averages). The panel measures every minute whether anyone is watching. Point at
  a chart, or touch it on a phone, to read a value.
- **Game servers**: one row per server with its state and uptime, CPU (a share of the whole
  machine), memory, players online and **TPS** (Java servers with RCON set up), folder size, backups and
  when the next backup and restart are due. Click a row to open the server. Folder sizes are
  measured in the background every 10 minutes, so a new server shows *measuring…* at first.
- **Host**: host name, Ubuntu release, kernel, uptime, time zone, addresses, the panel's
  version and whether an update is waiting.
- **Disks**: each file system holding the servers, backups and panel data, with free space.
- **Supporting services**: the panel itself, failed units, installed Java versions and which
  servers use each (with **Install** and **Remove**, see [Java versions](#java-versions)), `screen`, the firewall, Phantom proxies, backup and restart timers, HTTPS
  and its certificate, notifications, and the cluster.
- On a cluster master, **Cluster** adds a card per panel with its CPU, memory, disk, servers
  online and anything that needs attention. Nodes need RedstonePanel 1.11 or newer for this.

Visitors from a trusted local network who haven't signed in see the dashboard without
addresses, versions, certificates or settings.

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/overview.png"><img src="images/overview.png" alt="Dashboard with a Java server selected"></picture>

The sidebar on the left holds everything that isn't about one server:

| Item | What it does |
| --- | --- |
| **RedstonePanel** logo, version and name | The redstone "R" logo, the panel's version (for example `v1.16.4`) and its name, such as **Survival Box**, so you can tell several panels apart. |
| **➕ New Server** | Create a server, import one, or restore a removed one. |
| **📊 Dashboard** | This machine's resources, services and what needs attention (above). |
| **🌐 Cluster** | On a cluster master: every panel's servers (see [Clusters](#18-clusters)). |
| **🔀 Port forwarding** | Admins: what to set up on your internet router so players outside your home can join (see [Port forwarding](#port-forwarding)). |
| **🔗 Join pages** | Admins: a public page per server that tells players how to join (see [Join pages](#join-pages)). |
| **🖥️ Console** | The panel's live activity log. |
| **📋 Templates** | Saved server settings to reuse. |
| **Sort by** | Name A–Z, Z–A, or online servers first. |
| Server list | Every server, grouped by edition: green dot = running, grey = stopped, with its version and port. Click one to open it. On a cluster master, each node's servers follow under the node's 🌐 name, grouped by edition the same way. Click a heading to fold it; a folded heading shows how many of its servers are online, and the panel remembers what you folded in this browser. |
| **♻️ N removed servers: restore** | Shown when servers were uninstalled with **Keep World**. |
| **👥 Users / 🛡️ Settings / 🔑 Password** | Accounts and panel settings (admins), and changing your own password (panel accounts) and two-factor sign-in (all accounts). |

In the **top right corner** of every page (at the right end of the top bar on a phone) are two
small buttons, also saved in your browser:

- The **sun** or **moon** switches between light and dark mode with one click. The sign-in page
  has it too.
- The **colored dot** picks the accent color: **Redstone Red** (the default), Diamond Blue,
  Emerald Green or Amethyst Purple.

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/accent-color.png"><img src="images/accent-color.png" alt="The accent color choices under the colored dot, beside the sun/moon switch"></picture>

### Port forwarding

Players on your home network join with the machine's LAN address. Players anywhere else reach
your internet address, so your internet router (pfSense, or the box from your provider) has to
pass each game port on to the right machine. **🔀 Port forwarding** (admins) lists exactly what
to forward, for every server on this panel and, on a cluster master, on every node, with each
machine's LAN address. This is about the router in front of your machines; the panel already
looks after the firewall (`ufw`) on each machine.

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/port-forwarding.png"><img src="images/port-forwarding.png" alt="The Port forwarding page on a cluster master"></picture>

- **Your internet address**: type your router's WAN address, or a dynamic DNS name for it.
  **Ask the router** asks your own router with NAT-PMP or UPnP (on pfSense: Services > UPnP &
  NAT-PMP, off by default). The panel never asks an outside "what's my IP" website. If the
  address starts with 100.64 to 100.127, your provider shares it between customers (carrier-grade
  NAT) and port forwarding can't work; a private address (192.168.x.x, 10.x.x.x) means a second
  router in front of yours.
- **Forward these**: one line per forward: outside port, protocol, the machine's LAN address and
  inside port, and what it's for. **Copy as text** copies the list.
  - Java: TCP on the server port. If they're set up, also Geyser (UDP, Bedrock players on a
    Java server) and Simple Voice Chat (UDP).
  - Bedrock 1.26.50 and later (NetherNet, the default): TCP on the server port and UDP on its
    `server-udp-ports` (the panel sets both to the same port). Keep the outside and inside port
    the same. Bedrock with `transport=raknet`: UDP on the server port.
- **Two servers want the same outside port**: with one internet address, each outside port can
  go to only one server. Two servers on different machines that both use 25565 can't both be
  forwarded; give one another port in its **Configuration** tab.
- **Check from here** connects to your internet address from this machine and, for Java and
  RakNet servers, asks the server for its name so it can tell when the wrong server answered.
  It only works when the router loops connections back inside (pfSense: **NAT Reflection**);
  "No answer" doesn't prove a forward is wrong. The real test is a phone on mobile data.
- **How to set it up**: step-by-step for **pfSense** (Firewall > NAT > Port Forward, with the
  exact values for each rule, and TCP/UDP in one rule where a port needs both) and for **other
  routers**. Give each machine a fixed address first (a DHCP static mapping or reservation), or a
  forward stops working when its address changes.
- **Only if you need them**: Query (UDP), for server-list websites.
- **Leave these closed**: RCON, NetherNet LAN discovery (UDP 7551) and Phantom are for the home
  network only. The panel's own port is listed as optional and not recommended: a VPN into
  your home (pfSense has WireGuard and OpenVPN) is safer than putting the panel on the internet.
  If you do forward it, use HTTPS and two-factor sign-in for every admin.

Nodes older than 1.16.14 show only their game ports, with the address the master knows them by.

#### Port check

The top of the page (since 1.23.1) compares every port the servers and the panel need with what
is really using each port on the machine (on a cluster master, on every member). It catches
what can happen outside the panel: another program on a server's port (a Docker container, a
server started by hand, another panel), two servers edited by hand onto one port, a mod or plugin
the panel doesn't know opening a port, and a server set to the panel's own port.

- **Who holds a port** is named, so you can change or stop that program yourself: its name,
  process id, path and command line (which often shows the port setting itself), the systemd
  unit or Windows service it runs as, or the Docker container and its published port. A Java or
  Bedrock server the panel didn't start is recognised as one. The panel never stops or changes
  another program. When nothing visible holds a port (Docker publishing it through firewall rules
  only, or the Windows HTTP service), it says so and gives the command that shows more.
- **Fixed by itself**, because nothing outside the machine relies on them: RCON and Query ports,
  the known mods' ports above (now also when another program holds them), and a port a mod the
  panel doesn't know opened, when exactly one line in that server's `config` or `plugins` files
  sets it. **Needs attention** says what moved until the server restarts.
- **Move to a free port** moves a server's game port (with its IPv6, Query and gameplay ports)
  to the next free one, stops and starts the server if it was running, opens the new port in the
  firewall and tells you what to change on your router. The join page follows by itself.
- **Starting** a server from the panel while another program holds its game port is refused with
  the reason, instead of the server crashing and restarting over and over.
- Ports a server opened through a mod the panel doesn't know are listed and remembered, so no
  new server or mod is given them.
- **Check the router** reads the router's forwards over UPnP and says which point to the wrong
  machine or port, are turned off, or lead to a port no server uses now. pfSense lists only the
  forwards UPnP made, not the ones under Firewall > NAT > Port Forward.
- Problems also show under **Needs attention** with a **Port check** link, the **Last crash** card
  links here when a port was taken, and the `port_conflict` webhook (Panel Settings >
  Notifications, on by default) sends each new problem once.
- `sudo redstonepanel ports` prints the same list on the command line, also while the panel is
  stopped (for example when another program took the panel's own port).

### Join pages

Telling a new player how to join usually means a message with the address, the version and,
for a modded server, which mods to get. **🔗 Join pages** (admins) gives each server a public
page that says all of it and keeps itself up to date: whether the server is online, how many
are playing, the address to type, the Minecraft version, and for a modpack server the exact
pack to install.

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/join-page.png"><img src="images/join-page.png" alt="A server's join page, as a player sees it"></picture>

Nothing is public until you turn it on, twice: the port, and then each server's page.

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/join-pages.png"><img src="images/join-pages.png" alt="The Join pages page in the panel"></picture>

- **The public port** (8444 unless you change it). Join pages have a port of their own, so you
  can forward that one port on your router and keep the panel off the internet. The port
  answers nothing but the join pages: there is no sign-in page on it and none of the panel.
  It uses HTTPS with the panel's certificate when the panel does. The panel opens the port in
  this machine's firewall; for players outside your home, forward TCP 8444 on your router too
  (see [Port forwarding](#port-forwarding)).
- **Also list the public pages at /join** adds a page that lists them. Off, a page is found
  only by its own link.
- **Servers**: press **Set up** next to a server, tick **Make this server's join page
  public** and **Save page**. The link appears under the server, with **Copy link** to send
  to your players.
  - **Link name** is the end of the link, for example `/join/survival`.
  - **Address players type**. Leave it empty to use your internet address from **🔀 Port
    forwarding** with the server's own port. Type one here for a server with its own name, and
    add `:port` when your router shows a different port outside. The panel never publishes an
    address it worked out by itself: with no address anywhere, the page asks players to get it
    from you.
  - **Unlisted** gives the page a long random link instead of the link name, for a
    friends-only server. It can't be guessed and never appears in the list. **Make a new
    link** replaces it; the old link stops working.
  - **Show who is playing** adds the players' names. Off, only the number is shown.
  - **A note for players** and **a link of your own** (a Discord invite, a website) appear at
    the top of the page. Only `https://` links are accepted.
  - **Where players get the mods** (mod servers). A server made **From a modpack** on Modrinth
    links to that exact pack version by itself, with an **Open in Modrinth App** button and
    steps for Modrinth App and Prism Launcher. Any other mod server offers its
    [player pack](#player-packs) *(1.21.6)*: a download made from the server's own mods. A
    link pasted here is shown only when the player pack is off.

What a page shows depends on the server:

| Server | The page says |
| --- | --- |
| Vanilla, Paper, Purpur | The address, the Minecraft version, "No mods are needed", and how to add the server in the game. |
| Paper or Purpur with Geyser | The same, and the address and port for Bedrock players. |
| Fabric, Quilt, Forge, NeoForge | The loader and version, the modpack to install (see above), and how to join with a launcher that handles mods. |
| Bedrock | The address and port, an **Add this server to Minecraft** button for phones, tablets and Windows, and the steps by hand. |

<img src="images/phone-join-page.png" width="280" alt="A join page on a phone">

A page never shows a LAN address, the machine's or a node's name, a folder, the panel's
version, account names or the list of plugins. It sets no cookies and has no forms. The
server is asked for its player count at most once every 30 seconds, however many people look.

On a cluster master the list includes the servers on every node, and the master serves their
pages; nodes open nothing. A node's own join pages belong to that node's admin and can't be
turned on from the master. If a node can't be reached, its servers' pages stay up and say
offline.

#### Join pages through a proxy (HAProxy or Cloudflare) *(1.21.9)*

Already run HAProxy (for example the HAProxy package on pfSense) or Cloudflare for your
website? Then players can open the join pages on your own domain and certificate, like
`https://play.example.com/join/survival`. The proxy sends every path that starts with `/join` to
the panel's join port. The port itself keeps working as before.

In **🔗 Join pages**, under **Through a proxy (HAProxy or Cloudflare, optional)**:

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/join-pages-proxy.png"><img src="images/join-pages-proxy.png" alt="The Through a proxy section of Join pages, with the HAProxy settings to copy"></picture>

- **The proxy's address**: the address HAProxy or a Cloudflare Tunnel connects to the panel
  from. On pfSense that's its LAN address, for example `10.0.0.1`; for `cloudflared` on the
  panel's own machine it's `127.0.0.1`. Only requests from this address may say which player
  they are for (the `X-Forwarded-For` header). That keeps a separate request limit for each
  player instead of one shared limit for everyone behind the proxy. Anyone else who sends the
  header is ignored, so nobody can use it to get around the limit.
- **Address players open**, for example `https://play.example.com`. The panel uses it for the
  links it shows on this page and on the Mods tab. Leave it empty to keep using your Port
  forwarding address with port 8444.
- **Cloudflare's proxy (orange cloud) sends players here**: for a domain proxied by Cloudflare
  with a port forwarded on your router. Requests from Cloudflare's own addresses may then name
  the player (the `CF-Connecting-IP` header). The panel ships Cloudflare's list of addresses and
  refreshes it from cloudflare.com once a day. Not needed for a Cloudflare Tunnel.
- **Only through the proxy** makes the join port refuse everyone but the proxy (and Cloudflare,
  when that's on). With a tunnel or HAProxy, don't forward 8444 on your router then.
- **HAProxy settings to copy** and **Cloudflare Tunnel settings to copy** show the lines to
  paste, already filled in with this panel's address and port.

**HAProxy on pfSense** (**Services > HAProxy**):

1. **Backend** tab, **Add**: name it `redstonepanel_join`. Under **Server list**, add a server
   with this panel's LAN address and port `8444`. Tick **Encrypt (SSL)** when the panel uses
   HTTPS, and leave **SSL checks** off, because the panel's own certificate is self-signed.
   Under **Health checking**, choose **HTTP** and set **Http check URI** to
   `/join/assets/join.css`.
2. **Frontend** tab: open the HTTPS frontend that already serves your domain. Under **Default
   backend, access control lists and actions**, add an ACL named `join` with **Path starts
   with** `/join`, and an action **Use Backend** `redstonepanel_join` for the ACL `join`.
   Make sure **Use "forwardfor" option** is ticked.
3. **Apply Changes**, then enter pfSense's LAN address as **The proxy's address** in the panel.

**Cloudflare Tunnel** (no port forwarding at all):

1. In the Cloudflare dashboard, open your tunnel (**Networks > Tunnels**) and add a **public
   hostname**: your domain, path `^/join`, service type **HTTPS** (or **HTTP** when the panel
   uses HTTP) and URL `localhost:8444` when `cloudflared` runs on the panel's machine, or the
   panel's LAN address with port 8444 when it runs elsewhere. Under **Additional application
   settings > TLS**, turn on **No TLS Verify**, because the panel's certificate is
   self-signed. A tunnel run from a `config.yml` file takes the lines from **Cloudflare Tunnel
   settings to copy** instead.
2. In the panel, enter the address `cloudflared` connects from as **The proxy's address**
   (`127.0.0.1` on the same machine), and tick **Only through the proxy**.

**Cloudflare's proxy (orange cloud) with port forwarding**: Cloudflare carries only some
ports, and 8444 isn't one of them. Forward a port it does carry, such as 8443 or 2053, on your
router to the panel's port 8444, set the **Address players open** to
`https://play.example.com:8443` (or leave out the port when you forward 443), and tick
**Cloudflare's proxy (orange cloud) sends players here**. Set Cloudflare's SSL/TLS mode to
**Full**, because the panel's certificate is self-signed.

On a cluster, point the proxy at the master only. It serves the pages of every node's servers.

On the right is the selected server. **Start**, **Restart** and **Stop** are always at the top
(**Stop** is the outlined red one), with six tabs underneath. Admins also get **Files** and, on Fabric, Paper and Bedrock servers,
**Mods**, **Plugins** or **Add-ons**.

### Terminal

Admins can open a shell on the machine from the browser: **⌨️ Terminal** at the top of the
Dashboard, or on a machine's card on the Cluster page. It is a full terminal (colours, `htop`,
`nano`, `sudo`), on a phone too, with a row of keys a phone keyboard doesn't have (Esc, Tab,
Ctrl, arrows).

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/terminal.png"><img src="images/terminal.png" alt="A terminal open on the Dashboard, logged in as a Linux account"></picture>

<p><img src="images/phone-terminal.png" alt="The terminal on a phone, with its row of keys" width="260"></p>

It is **off** until an admin turns it on under **🛡️ Settings → Terminal**, and it only works
over HTTPS, so what you type can't be read on your network.

**Which account the shell runs as.** Never the panel's own (root). It is always a Linux account
on that machine:

- If you signed in to the panel **with your Linux account**, the terminal opens as that account.
  For 15 minutes after signing in it opens straight away; after that it asks for that account's
  password once (**Skip the password for** changes the 15 minutes; 0 always asks).
- If you signed in with a **panel account**, the terminal shows a `login:` prompt. Type a Linux
  account on that machine and its password, as you would over SSH. A panel account named like a
  Linux account is not that account.
- With [two-factor sign-in](#two-factor-sign-in) on, the terminal asks for a code after the
  password.
- `root` can't log in unless you tick **Allow logging in as root**: log in as yourself and use
  `sudo`. Accounts that can't log in at all, like `minecraft`, can't log in here either.

**Who can't.** User accounts (helpers) never get a terminal, whatever they're allowed to do on
their servers, and neither do visitors let in by **trusted networks**. The join pages' port
has no terminal.

Good to know:

- A terminal closes after 15 minutes with nothing typed (a minute's warning first; **Close
  after** changes it), after 12 hours, when you sign out, and when the terminal is turned off.
  Restarting or updating the panel closes open terminals too.
- Each admin can have 3 open, and a machine 10. **Settings → Terminal → Open now** lists them
  with an **End** button.
- Five wrong passwords in 10 minutes block that account and your address for 10 minutes.
- Every terminal that opens or closes, and every wrong password, is in the activity log
  (`[TERMINAL] alex opened a shell as alex from 192.168.1.20`). What you type is never recorded.
  The logins are also in the system's own log (`journalctl`, or `/var/log/auth.log`), as
  `session opened for user alex`.
- **On a cluster**, the master can open a terminal on a node only when that node's own admin
  ticks **Allow a terminal from the cluster master** in the node's settings. You log in with an
  account of the node; your password passes through the master, encrypted on both legs.
<picture><source media="(prefers-color-scheme: light)" srcset="images/light/terminal-settings.png"><img src="images/terminal-settings.png" alt="Settings, Terminal: on or off, Close after, Skip the password for, root, and the terminals open now"></picture>

- In an emergency, `sudo redstonepanel terminal off` turns it off from SSH.
- Not on Windows yet: the button doesn't appear on a Windows machine.

## 4. Create or import a server

Click **➕ New Server**.

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/new-server.png"><img src="images/new-server.png" alt="New Server dialog"></picture>

**Deploy a fresh server**

1. Choose **Java Edition** or **Bedrock Edition**. On an ARM64 machine such as a Raspberry Pi,
   Bedrock is greyed out: Mojang only makes the Bedrock server for x86-64.
2. For Java, choose the **Server Type** and **Minecraft Version**:
   - **Vanilla (Mojang)**: the official server.
   - **Paper (plugins)**: a faster server that runs Bukkit/Paper plugins. Put plugin `.jar`
     files in the server's `plugins` folder.
   - **Purpur (plugins)**: Paper with more settings to tune. It runs the same plugins as
     Paper, in the same `plugins` folder.
   - **Fabric (mods)**: runs Fabric mods. Put mod `.jar` files in the server's `mods`
     folder; players need the same mods.
   - **Quilt (mods)**: runs Quilt mods and most Fabric mods. Minecraft 1.14.4 and newer.
   - **NeoForge (mods)**: runs NeoForge mods. Most new modpacks use it. Minecraft 1.20.2 and
     newer.
   - **Forge (mods)**: runs Forge mods, including the many older ones. Minecraft 1.12.2 and
     newer.
   - **Newest** is the default. Pick an older version to match your players' clients or your
     plugins and mods.
   - The line under the lists says which Java that version needs and which installed Java
     will run it. If none is new enough, an admin is asked to install it when they click
     **Deploy Fresh Server**; other accounts are told which package an admin needs to install
     (`openjdk-21-jre-headless`, for example).
3. Optionally pick a **Base Template** (see [Templates](#14-templates)) to start from saved
   settings.
4. On a cluster master, choose **Where**: this panel or one of its nodes. The Java line and
   ports then come from that machine. See [Clusters](#18-clusters).
5. Optionally open **Advanced options** (see below).
6. Click **Deploy Fresh Server**.

**Advanced options**

Everything in this section is optional; a field left empty is decided as usual. All choices
are checked before anything is downloaded, so a taken name or port fails straight away.

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/new-server-advanced.png"><img src="images/new-server-advanced.png" alt="New Server's Advanced options filled in for a Paper server"></picture>

| Group | What you can choose |
| --- | --- |
| **Server** | The name (it becomes `minecraft-NAME`) and the game port. |
| **Software** (Java) | **Build**: a specific Paper or Purpur build, or Fabric, Quilt, Forge or NeoForge version, instead of the newest stable one, for when a plugin or mod needs a known one. **Show snapshots and pre-releases** (Vanilla) lists the current test versions in **Minecraft Version**. **Java**: an installed Java to run on, never older than the version needs. |
| **Memory** (Java) | **Heap**: Java's memory. Refused if it would leave less than 1 GB for the system. |
| **Hard limits** | A hard memory limit (Automatic for Java: the heap plus room for Java) and a CPU limit in cores, the same as **Maintenance → Memory and limits**. |
| **Startup flags** (Java) | **Aikar's flags**, the usual tuning for Paper, and for admins extra flags: `-XX:` options, `-D` properties and `-Xss` only. |
| **World** | Seed, world type (Flat, Large biomes, Amplified; Bedrock: Flat) and the world folder name. |
| **Settings** | MOTD, game mode, difficulty, max players, online mode, players on the whitelist (Bedrock: allowlist) and, for Java, operators. |

A **Base Template** is applied first, and anything chosen here wins over it. A snapshot server
can't be "updated" back to an older release, because worlds can't go back. On a node, the node
needs RedstonePanel 1.17.2 or later. Startup flags can be changed later under
**Maintenance → Java → Startup flags** (a running server restarts); they stay through updates,
Java changes, restores and moves.

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/startup-flags.png"><img src="images/startup-flags.png" alt="Maintenance, Java card with Startup flags"></picture>

The panel then:

- names the server (`minecraft-java`, then `minecraft-java-2`, and so on) and picks a free
  port (Java from 25565, Bedrock from 19132), unless you chose them in Advanced options;
- downloads the server: Mojang's `server.jar`, the newest stable Paper or Purpur build, or
  Fabric's server launcher for the chosen version (always saved as `server.jar`), or the latest
  Bedrock Dedicated Server. Vanilla, Paper and Purpur downloads are checked against their
  published checksums. For Quilt, Forge and NeoForge it downloads that project's installer
  (checked the same way) and runs it in the new server's folder as the `minecraft` user; the
  installer fetches Minecraft and the loader's libraries, which takes a few minutes. Forge
  picks its recommended build for the Minecraft version, NeoForge its newest stable one;
- sets the server's ports in its `server.properties`. A Bedrock server keeps the full file
  that comes with the Bedrock download, so every setting is there to change in
  **Configuration**, and gets UDP `server-udp-ports` (its gameplay port) on the same number as
  its TCP `server-port`, so each Bedrock server has its own;
- accepts the Minecraft EULA for you (`eula=true`), so by deploying a server you agree to it;
- creates a systemd service that runs as the `minecraft` user and restarts the server if it
  crashes;
- opens its ports in `ufw`, if `ufw` is installed;
- starts the server.

Watch the progress in **🖥️ Console**. A Java server is ready in about a minute; most of that
time is the first world being generated.

**From a modpack** (admins)

A modpack is a set of mods and settings that were made to work together. Click **Choose a
modpack…** under **From a modpack**.

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/modpack-search.png"><img src="images/modpack-search.png" alt="Choosing a modpack from Modrinth"></picture>

- **Modrinth**: type a name, pick a pack and then a version of it. Only packs for
  Fabric, Quilt, Forge and NeoForge are listed, and each version says which of those and which
  Minecraft version it uses.
- **Upload a pack**: a Modrinth `.mrpack` file, or a **server pack** `.zip`, which is how
  CurseForge packs are installed. On the pack's CurseForge page, open **Files**, pick the
  version and download its **Server Pack** (under *Additional files*), then upload that zip
  here. The panel has no CurseForge browser: you download the pack, so the authors' download
  rules are kept and no API key is needed.

Then click **Create server** (or **Upload & create server**). The panel:

1. reads the pack and works out the server type and Minecraft version it needs. For an
   `.mrpack` they are written in the pack. For a server pack the panel reads them from the
   pack's files; when it can't tell, choose them under **If the pack doesn't say what it needs**;
2. installs that server type itself, at the version the pack names, with the Java it needs;
3. downloads the pack's server-side mods (an `.mrpack` lists them with checksums, and each
   download is checked) and copies the pack's own `mods`, `config` and other folders in.
   Mods marked as not for servers are left out;
4. gives the server 4 GB of memory when the machine has room (change it under
   **Maintenance → Memory and limits**), and starts it.

The panel never runs anything from a pack. Start scripts, installers and server jars inside an
uploaded pack are not copied: the panel downloads the server software from its own project
instead. Players need the same pack in their launcher to join. A big pack can take ten
minutes or more to install; watch **🖥️ Console**.

On a Raspberry Pi or another small machine, pick light packs: many modpacks want 6 GB of
memory or more. On a cluster, a modpack server can be created on a node that runs
RedstonePanel 1.20 or later.

**Import a server you already have**

Under **Import Existing**, choose a `.zip`, `.tar.gz`/`.tgz` or `.tar` of a server folder and
click **Upload & Import**. A backup wrapped in a single folder is fine; its contents are used.
The panel gives it a name, a free port and a service, like a fresh deploy.

**Bring a server back from backup storage** (admins)

If a server's backups were copied to an SSH server or S3 storage (see
[Off-site copies](#off-site-copies-ssh-or-s3-storage)), **From backup storage → Choose a
backup…** lists every backup there, from every panel that copies to it. Pick one and click
**Import from backup storage**: the copy is downloaded and checked, the server gets its old name
if it's free here, its server software is downloaded again, and it starts. Add the storage under
[Backup storage](#backup-storage) first.

**A server you set up yourself, outside the panel** (admins)

If the machine already runs a Minecraft server that RedstonePanel didn't make, started by your
own systemd service, a script or a `screen` session, the dialog lists it under **Found on this
computer** with its folder, port, version and what started it. Looking changes nothing.

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/found-servers.png"><img src="images/found-servers.png" alt="Found on this computer: two servers set up outside the panel, one already handed over with an Undo button, and a leftover service it can't manage"></picture>

Click **Manage with RedstonePanel** to hand one over:

- The service that ran it is stopped and turned off. It is not deleted.
- The panel runs the server from the **same folder** under a service of its own, with the memory
  it had. No file is copied or moved; the folder is given to the `minecraft` account, and a jar
  with another name is renamed `server.jar`.
- Players are disconnected for the moment it takes to restart.
- If the server doesn't start under the panel, everything is put back by itself.

A server started from a terminal or `screen` has no service the panel could stop safely: stop it
yourself (type `stop` in its console), and it stays listed, as stopped, for an hour so you can
hand it over.

**Undo**, next to a server you handed over, puts it back: the panel's service is removed, the
folder goes back to its owner, the jar gets its name back and the old service is turned on and
started again. The world is never touched.

The panel says why when it can't take a server over: a folder inside a home folder the
`minecraft` account can't get into, a path with a space, a port one of the panel's servers
already uses, or a Java server that isn't started with `-jar`.

Below them, dashed, the list names what the panel saw but can't manage, with the reason: a server
running from a folder that no longer has its `server.properties` (its settings and world were
moved or deleted), one whose folder the panel couldn't read, or a service named for Minecraft
that is stopped, whose folder is gone, or that runs something other than a game server (a
proxy, for example). These often come from another tool that was uninstalled. When there is
nothing at all, the section says so.

**Removed servers** are listed at the bottom of the same dialog. See
[Restore a removed server](#15-restore-a-removed-server).

## 5. Overview

The **Overview** tab (shown in [The dashboard](#3-the-dashboard)) shows quick stats:

- status;
- engine;
- Minecraft version;
- the port players connect to;
- the Java heap limit.

For Java servers it also shows which Java version the server needs and which one is installed.

### Last crash

When a server crashes, or stops without anyone asking it to (a start that fails, say), a
**Last crash** card appears under the quick stats. It says what went wrong in plain words and
what to do about it.

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/overview-crash.png"><img src="images/overview-crash.png" alt="The Last crash card of a Fabric server that is missing Fabric API"></picture>

- **The cause, named**, when it's one the panel knows: a missing dependency ("Ledger requires
  Fabric API and Fabric Language Kotlin, which aren't installed"), a mod or plugin made for
  another Minecraft version, the wrong Java (`UnsupportedClassVersionError`), the port already
  in use, out of memory (including being stopped for going over its
  [hard memory limit](#memory-and-limits)), an EULA that hasn't been accepted, and a damaged
  `level.dat` or world chunk.
- **The likely mod or plugin**: names in the crash report ("Suspected Mods"), in the log and in
  the missing-dependency lines are matched against what's installed. Admins get
  **Turn off and restart**, which renames the jar to `.disabled` (as the Mods tab does) and
  starts the server again.
- **Undo the last mod update**, when the mods or plugins were updated in the last three days.
  It puts back the files the update replaced, then restarts the server.
- **The log**: the newest crash report if one was just written, otherwise the end of
  `logs/latest.log` (Bedrock: `server.log`) or of what the server printed before it had a log.
  The error lines are highlighted; open it with the link under the card.

Nothing is changed until you click. **Dismiss** hides the card; the next crash brings it back.
The [crash notification](#notifications) gets the same one-line cause, and the dashboard's
**Needs attention** lists the crash for a week with a link to the card.

### Performance

Under the quick stats, **Performance** answers "is the game lagging, since when, and why?". It
refreshes every 10 seconds while the tab is open, and says when it was last updated, so a stale
view is obvious.

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/performance.png"><img src="images/performance.png" alt="The Performance card of a Java server"></picture>

- **Tick health (TPS)**: Minecraft aims for 20 ticks a second. 19 or more is **Smooth**, 15 to
  19 is **Some lag**, under 15 is **Lagging**. Paper servers report the 1, 5 and 15 minute
  averages and milliseconds per tick (`tps`, `mspt`); Vanilla and Fabric 1.20.3 and newer report
  milliseconds per tick (`tick query`). The panel only asks; it never changes the tick rate.
  It needs RCON (turn it on in **Live Chat**). Older Java versions and Bedrock have no such
  command, so the card says *Not available* rather than guessing.
- **Memory**: what the whole server process uses, next to its heap (**Max heap**) and hard
  limit, if it has one. Java uses some memory outside the heap, so the process is normally a
  little bigger than the heap.
- **Players** and the number of connections to the game port; **World** size and how many
  plugins or mods are installed.
- **Held back by its limits**: shown when the server's [CPU limit](#memory-and-limits) slowed it
  down in the last minute, when it's at its hard memory limit, or when systemd stopped it for
  going over.
- **History**: CPU, memory, players and TPS for the last 24 hours or 7 days, written every
  minute whether anyone is watching. A gap means the server (or the panel) wasn't running.
- **Suggestions**, each shown only when what it's about was measured, with a link to where it's
  changed. Nothing is applied for you, because each change trades how the world plays for speed:
  - TPS under 18 for 10 minutes: lower `simulation-distance`, then `view-distance`; profile
    with **spark**. On Vanilla, Paper or Fabric run the same world much faster.
  - A Fabric server without **Lithium** (and **FerriteCore** when memory is tight); a Paper
    server with WorldEdit but not **FastAsyncWorldEdit**.
  - A server that uses all of its memory and keeps restarting, or that hit its hard limit;
    all Java servers together promised more memory than the machine has; swap in use.
  - A world over 4 GB with no world border: set one and pre-generate it with **Chunky**.
  - No backup schedule, a `logs` folder over 1 GB, or worlds on a spinning hard disk.

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/overview-bedrock.png"><img src="images/overview-bedrock.png" alt="Bedrock server overview"></picture>

Players connect to `<server address>:<port>`. On Bedrock, use the port shown here; the
default 19132 belongs to the first Bedrock server.

## 6. Live Chat

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/live-chat.png"><img src="images/live-chat.png" alt="Live Chat"></picture>

The chat players see in game. Type in the box and click **Send**:

- plain text is broadcast to everyone as `[Server] …`;
- text starting with `/` runs as a server command, for example `/time set day`,
  `/whitelist add Steve` or `/list`. The reply shows under the chat.

**Clear** empties the panel's view; it doesn't touch the server.

### Quick commands

You don't have to remember how a command is written. There are three ways to run one:

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/quick-commands.png"><img src="images/quick-commands.png" alt="Live Chat suggesting players for a command"></picture>

- **Type it, with suggestions, as in the game.** Type `/` and a list opens above the box with
  the commands that match and what each does. After the command, a line shows what it takes,
  for example `/tp <player> <player or x y z>`, with the part you are on highlighted, and the
  list changes to what fits there: the players online now (🟢), players seen before, `@a`
  for everyone, or the fixed choices such as `survival`, `creative`, `clear`, `rain`.
  **Tab** or a click fills in a suggestion, the arrow keys move through the list, **Esc** closes
  it, and **Enter** sends as before. In a plain message, type the first two letters of an online
  player's name and press **Tab** to finish it.
- **Pick it from Commands.** The **Commands…** list above the box groups the everyday
  commands under Players, World, Messages and Server. Pick one and a form asks only for what
  that command needs: a player from a list, a choice, three numbers for a place, a reason.
  The exact command is shown before you press **Run**. **Edit** puts it in the message box
  instead, to change by hand. Commands that are hard to take back (stop the server, kill,
  empty an inventory) ask first.

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/quick-commands-form.png"><img src="images/quick-commands-form.png" alt="The Commands form for teleporting a player"></picture>

- **One click.** The buttons next to Commands run a command that needs nothing filled in:
  **List players**, **Save world**, **Day**, **Night** and **Clear weather**.

Things to know:

- The list holds about 35 everyday commands for each edition; Java and Bedrock servers each
  get their own (a Java server has `/whitelist` and `/ban`, a Bedrock server `/allowlist`).
  Anything else can still be typed in full.
- A running Vanilla, Fabric, Quilt, Forge or NeoForge server also tells the panel which
  commands and game rules it has, so commands added by mods are suggested too, and game rules
  are offered as that Minecraft version names them (they were renamed in 1.21.11). Paper and
  Purpur don't answer that question, so plugin commands aren't suggested.
- A Bedrock gamertag with a space is put in quotes for you: `/kick "Sam Carter"`.
- Bedrock servers don't send a reply back to the panel, so **List players** and **Save
  world** are offered on Java servers only. Who is online on a Bedrock server comes from its
  log and can be up to 30 seconds behind.
- Suggestions and the form are for anyone with **Console & chat** on the server, the same
  permission as the chat itself.

Java servers are reached over **RCON**. The first time you open this tab on a server without
RCON, the panel turns it on with a random password and restarts the server once; send your
message again when it's back. The RCON port is shown top right. Bedrock servers have no RCON;
the panel types into the server's own console instead.

## 7. Configuration, player roles and the whitelist

**Visual Editor**: every setting in `server.properties`, as a form.

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/configuration.png"><img src="images/configuration.png" alt="Configuration, visual editor"></picture>

Type in the filter box to find a setting quickly, for example `difficulty`, `pvp`, `rcon` or
`port`.

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/configuration-filter.png"><img src="images/configuration-filter.png" alt="Filtering properties"></picture>

Click **Save Changes**, then **Restart** the server so it reads the new settings.

**Raw Text** is the same file as plain text, for pasting a whole configuration or editing
settings the form doesn't know.

**A Bedrock server that shows only its ports?** Bedrock servers created before 1.16.4 got a
`server.properties` with only `server-port`, `server-portv6` and `query.port`. They run on
Bedrock's built-in defaults, but the form has nothing else to show. Updating the server's
Bedrock version doesn't replace that file. Either add the settings you want as lines in **Raw
Text** (for example `difficulty=normal`), or stop the server, copy `server.properties` from a
fresh Bedrock download into its folder with **Files**, set `server-port` and `server-portv6`
back to the values it had, and start it again.

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/configuration-raw.png"><img src="images/configuration-raw.png" alt="Raw server.properties"></picture>

**Player Roles** lists players seen in the server's logs plus everyone already in the
permissions file, with a role for each:

- **Java:** *Operator* (in `ops.json`) or *Member*;
- **Bedrock:** *Operator*, *Member* or *Visitor* (in `permissions.json`).

Changing a role takes effect at the next restart.

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/player-roles.png"><img src="images/player-roles.png" alt="Player roles"></picture>

**Whitelist & Access** shows who is on, who may join and who has tried:

- **Online now**: the players on the server, each with **Kick** and, on Java, **Ban**. Java
  servers with RCON answer straight away; otherwise the list comes from the server's log and
  can be up to 30 seconds behind.
- **Whitelist** (Java `whitelist.json`, Bedrock `allowlist.json`): whether it's on
  (`white-list` or `allow-list` in `server.properties`; turn it on in the Visual Editor), each
  player on it with their last join, **Allow** to add a player and **Remove**. While the server
  runs, changes go through its console (RCON for Java, screen for Bedrock) and apply at once;
  otherwise the panel edits the file and the server reads it when it starts. A Java player
  needs a UUID, which the panel takes from its access log, `usercache.json`, the offline-mode
  formula or Mojang, in that order.
- **Banned** (Java, `banned-players.json`): each banned player with the reason, when, and who
  banned them, and **Unban**. Type a name and an optional reason to ban someone who isn't
  online. Bedrock has no ban list: keep someone out with the allowlist, or remove them now
  with **Kick**.
- **Turned away in the last 7 days**: players the server refused who still aren't allowed,
  how often, and why. **Allow** puts one on the whitelist, and **Unban** lifts a ban.
- **Access log**: who joined, left or was turned away (not on the whitelist, banned, wrong game
  version, server full). Filter by player name or ID, event and period (24 hours to 90 days).
  Each row has **Kick** while that player is online and, on Java, **Ban**.

**Kick** removes a player, with an optional reason they see; they can join again straight away.
**Ban** also removes them if they're online, and keeps them out until you **Unban** them. A kick
needs the server running. On a running Java server, kicking, banning and unbanning go through
RCON: open **Live Chat** once to set it up. A stopped Java server's ban list is edited directly,
and the server reads it when it starts. Bans are by name and UUID, never by address. Users and admins may kick and ban,
as with the console, and each one goes in the activity log with who did it.

The access log comes from the server's own console and log files, which the panel reads every
30 seconds, so it covers players who joined before the panel was installed too, as far back as
the logs go. Only names and IDs are kept, never addresses, in
`/var/lib/redstonepanel/players/`, for 90 days. Purging a server deletes its log. Bedrock
doesn't log refused joins, so on Bedrock the log shows joins and leaves only.

The dashboard's **Needs attention** lists players turned away in the last 24 hours who still
aren't allowed, and **Notifications** can send a message when a player joins or is turned away.

**Permissions (Raw)** edits `ops.json` (Java) or `permissions.json` (Bedrock) directly.

## 8. Maintenance: templates, export, update, uninstall

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/maintenance.png"><img src="images/maintenance.png" alt="Maintenance tab"></picture>

- **Save as Template**: saves this server's properties and permissions as a reusable
  template.
- **Apply Template**: overwrites this server's settings with a template, keeping its ports.
  Restart afterwards.
- **Export Server → Download .zip**: the whole server folder as a zip, to move it to another
  machine. Import it there with **➕ New Server → Import**.
- **Update Server → Check & Update**: for Java, choose **Newest** or a Minecraft version (the
  server keeps its type: a Paper server gets the newest Paper build of that version). Older
  versions than the server's own aren't offered, because a world can't go back. The new jar is
  downloaded before the server is stopped, so a failed download changes nothing. Then it stops
  the server, installs the download and starts it again. Your world, settings, ports,
  operators and allowlist are kept. With **Back up first** ticked (it is, unless you untick
  it), a Java server gets a "Before update" backup while it is stopped, and **Roll back the
  last update** appears under the button afterwards: see
  [Rolling back an update](#rolling-back-an-update). Bedrock servers always update to the newest release,
  the same safe way as [automatic updates](#update-alerts-and-automatic-bedrock-updates): a
  "Before update" backup first, and the old version put back if the new one doesn't start.
  A Forge or NeoForge server runs its installer again for the new version, in a separate
  folder, and only swaps the result in when it worked. A server made from a modpack is updated
  with its pack instead: see [Modpack](#modpack) below.
  On Fabric, Quilt, Paper and Purpur servers, moving to a newer Minecraft version first lists the mods or
  plugins that have no build for it yet on Modrinth or Hangar (and any it couldn't check),
  so you can wait rather than update into a server that won't start. If the new version
  needs a Java that isn't installed, you're asked to install it first.
- **Server type** (Java servers): change Vanilla, Paper, Purpur, Fabric, Quilt, NeoForge or
  Forge to another type and keep the world. See [Changing a server's type](#changing-a-servers-type) below.
- **Java** (admins, Java servers): which Java the server runs on and which it needs. Choose
  another installed Java that's new enough and click **Use**; a running server restarts.
- **Memory and limits**: see below.
- **Modpack** (servers made from a modpack): see below.

### Modpack

A server made from a modpack has a **Modpack** card on **Maintenance** with the pack's name,
version, server type and Minecraft version.

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/modpack-card.png"><img src="images/modpack-card.png" alt="Modpack card on the Maintenance tab"></picture>

- **Check for a newer version** (Modrinth packs) asks Modrinth for a newer version of the pack
  for this server's type and Minecraft version or newer. Pick one and click **Update the
  pack**.
- **Upload a newer version** takes a newer `.mrpack` or server pack `.zip`.

An update downloads and checks everything first, so a failed download changes nothing. Then
the server is backed up (**Before mod changes** on the **Backups** tab; untick **Back up the server first** to
skip it), stopped, moved to the pack's new server type version if that changed, and started
again. The mods the old pack version installed and the new one doesn't have are removed. The
world, `server.properties`, the whitelist, operators and bans, and any mods you added yourself
are kept. Config files the pack ships are replaced with the pack's new ones.

The panel doesn't show **Update available** for the server software of a modpack server,
because the pack decides which version it runs on. Which pack a server came from is kept in
`.redstonepanel-modpack.json` in its folder.

### Changing a server's type

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/server-type.png"><img src="images/server-type.png" alt="Server type on the Maintenance tab"></picture>

A server that started on Vanilla can become Paper or Purpur (for plugins) or Fabric, Quilt,
NeoForge or Forge (for mods), and any type can become any other, without making a new server. The world, port, settings, operators,
whitelist, bans, backups and restart schedules stay as they are. Only admins can do it, and
only for Java servers: Bedrock has one type. A server made from a modpack keeps the type its
pack needs.

1. On **Maintenance → Server type**, click **Change to Paper…** (or another type).
2. Read the preview. Nothing has changed yet.
3. Click **Change to Paper**. The card shows each step; you can leave the page.

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/change-type-preview.png"><img src="images/change-type-preview.png" alt="Preview of a type change"></picture>

**What the preview tells you**

- **Server software**: the build that will be downloaded and the Java it runs on. You can
  choose a newer Minecraft version at the same time, never an older one. If the new type has
  no build for your version yet (Paper is often a few days behind a new release), it lists
  the versions it has.
- **World**: before Minecraft 26.1, Paper kept the Nether and the End in their own folders
  (`world_nether`, `world_the_end`) and Vanilla and Fabric keep them inside `world`. Paper
  moves them out by itself; when a server leaves Paper, the panel moves them back in. From
  26.1 every type uses the same folders, but Paper keeps the world's game rules, weather and
  world generation settings with each dimension; when a server leaves Paper, the panel moves
  those files back to where Vanilla and Fabric look for them. The preview lists every move.
- **Mods and plugins**: a mod can't run as a plugin, or the other way round. The folder the
  new type can't load is renamed (`mods` to `mods.off`, `plugins` to `plugins.off`) and comes
  back if you return to that type. The same happens between mod loaders that can't load each
  other's mods (Fabric and NeoForge, for example). Two pairs keep everything in place: Purpur
  runs Paper's plugins and keeps the world the way Paper does, and Quilt runs Fabric's mods.
  Where the same Modrinth project also has a build for the
  new type (LuckPerms, Chunky, Geyser and many others), it's ticked for install. Their
  settings and saved data aren't carried over.
- **Safety**: how much room the backup needs.

**Mods that add things to the world.** If a Fabric, Quilt, NeoForge or Forge server has mods that add blocks, items,
mobs, dimensions or world generation, those things disappear from the world when a type
without them opens it. The preview names each such mod, and you have to type the server's
name to go ahead. Mods that only run on the server (Lithium, FerriteCore, spark) are safe to
drop.

**What happens when you confirm**

1. The new server software is downloaded first. If that fails, nothing has changed.
2. The server stops and a **Before type change** backup is taken, always.
3. The software is swapped (the old `server.jar` is kept as `server.jar.previous`), the world
   and add-on folders move, and the chosen mods or plugins are installed. Quilt, NeoForge and
   Forge are more than one file, so nothing of theirs is kept: putting one of them back runs
   its installer again for the same build, which needs the internet.
4. The server starts, and the panel waits up to 5 minutes for it to say it's ready. A server
   that was stopped before is stopped again afterwards.
5. If it doesn't start, the old type, its add-ons and the world from the backup are put back
   by themselves, the old server is started, and the card shows why (the log lines, and what
   the panel recognises from [crash explanations](#last-crash)).

**Undo type change** stays on the card until the next change or update. It puts the old
type and the world *from before the change* back. What was built since then is saved as a
"Before restore" backup, but isn't in the world any more, because the old type may not be
able to open it. Undo isn't offered once its backup has been deleted.

Each change is in the activity log with who did it, and the **Server type changed**
notification can be sent to your webhook.

### Rolling back an update

Updating a Java server with **Back up first** ticked keeps the old `server.jar` and a "Before
update" backup (the newest three are kept). Until the next update or type change, **Roll back
the last update** under **Update Server** puts both back: the old version, and the world as
it was before the update. A world that a newer Minecraft version has opened can't be opened
by an older one, so what was built since the update is saved as a "Before restore" backup
and isn't in the world afterwards.

There is still no way to take a world to an older Minecraft version than it has been played
on; rolling back works only because the backup is from before the update.

### Memory and limits

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/memory-and-limits.png"><img src="images/memory-and-limits.png" alt="Memory and limits on the Maintenance tab"></picture>

Everyone can see these; only admins can change them.

- **Memory (Java heap)**: how much memory the server starts with (**To start**, `-Xms`) and may
  use at most (**At most**, `-Xmx`). Write sizes like `2G` or `1536M`. A new server gets 1 to 2 GB
  (512 MB to 1 GB on a machine with 3 GB or less). The panel refuses a size that, with the other
  *running* Java servers, would leave under 1 GB for the system (768 MB on a machine with 3 GB or
  less). A running server uses the new size after a restart: tick **Restart now if the memory
  changes**, or restart it later. The server keeps its Java, and updates, moves and restores keep
  the setting.
- **Hard memory limit** (systemd `MemoryMax=`): if the whole server process goes over it, systemd
  stops the server (and, as after any crash, starts it again 20 seconds later). It stops one
  runaway server from starving the others. **Automatic** is the heap plus half again (at least
  512 MB more), for what Java uses outside the heap; a custom size must be at least 256 MB more
  than the heap. Bedrock servers take a custom size.
- **CPU limit** (systemd `CPUQuota=`): the most CPU the server may use, in cores; `0` is no
  limit. Minecraft's main loop runs on one core, so a limit under 1 makes it lag.

Limits apply at once, without a restart, and are kept in a drop-in file
(`/etc/systemd/system/minecraft-NAME.service.d/redstonepanel-limits.conf`), so rewriting the
server's unit keeps them. Uninstalling a server removes the drop-in; restoring it puts it back.
Limits belong to the machine, so a server moved to another cluster member doesn't take them along.

### Update alerts and automatic Bedrock updates

Every 6 hours the panel looks for a newer Bedrock Dedicated Server, Minecraft release, Paper
or Purpur build, Fabric or Quilt loader, or Forge or NeoForge build for each server (never snapshots or pre-releases). When there is one,
the server shows **⬆** and the new version in the sidebar, on the dashboard, on its
**Overview** and under **Maintenance → Update Server**. **Check now** looks right away. With
[notifications](#notifications) on, you get one **Update available** message per new version.

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/update-alert.png"><img src="images/update-alert.png" alt="Update available on a Paper server"></picture>

Bedrock players' apps update themselves, and then can't join a server on the older version.
So a Bedrock server can update itself: tick **Update automatically** under **Maintenance →
Update Server** (it's off until you do).

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/bedrock-auto-update.png"><img src="images/bedrock-auto-update.png" alt="Update automatically on a Bedrock server"></picture>

When a new release is out:

1. The new version is downloaded from minecraft.net and checked. If that fails, nothing else
   happens and the server keeps running.
2. Players see "Server updates to the new Minecraft version in 5 minutes" in chat, then again
   1 minute and 10 seconds before.
3. The server stops and is backed up (**Before update** on the **Backups** tab; the newest
   three are kept).
4. The new files go in. The server's `server.properties`, `permissions.json` and
   `allowlist.json` stay as they are, and so do its worlds and add-ons.
5. The server starts. When it says "Server started.", the update is done (**Bedrock server
   updated** notification).

If the new version doesn't finish starting within 3 minutes, the old files and the world from
the "Before update" backup go back, the server starts on the old version, and the failure
shows under **Needs attention** and as a **Server update failed** notification. A stopped
server is updated but stays stopped.

Java servers are never updated automatically, because a new version can break mods and
plugins: you get the alert and update with **Check & Update** when you're ready. Bedrock
can't run on ARM64 machines such as a Raspberry Pi, so there is nothing to update there.

On a cluster, each member checks its own servers (also while its panel is stopped), and the
master shows the nodes' alerts. From the command line, `sudo redstonepanel check-updates` does
the same check and updates.

### Java versions

Each Minecraft version needs a particular Java: 26.1 and newer need Java 25, 1.20.5 to
1.21.11 Java 21, 1.18 to 1.20.4 Java 17, and older versions Java 8. Each server's systemd unit
names its own Java (`/usr/lib/jvm/java-21-openjdk-amd64/bin/java`), so servers needing
different versions run side by side, and the system's default `java` doesn't matter.

The panel offers the Java versions your system's package sources have. Ubuntu packages 8, 11,
17, 21 and 25 (newer releases also newer ones); Debian 13 and Raspberry Pi OS only 21 and 25.
When a Minecraft version needs a Java the system doesn't package, the line under **Minecraft
Version** says so and the panel offers the closest newer one instead: Java 17 for 1.17's
Java 16 on Ubuntu, or Java 21 for an old version's Java 8 on Debian, which some old versions
and modpacks don't start on. If the system has nothing new enough, it says so and installs
nothing.

- **Installing.** When you create, update or restore a Java server and the Java it needs isn't
  installed, the panel asks to install it (`openjdk-21-jre-headless`, for example) with apt,
  shows the progress in **🖥️ Console**, and then carries on. Servers that were waiting for
  that Java, such as an imported one that couldn't start, are moved onto it and started.
  You can also install one ahead of time from the dashboard: **Supporting services → Java →
  Install**. Only admins can install or remove Java.
- **Removing.** A Java that no server uses shows **Remove** on the dashboard, and the
  dashboard's **Needs attention** mentions it. The panel removes only the
  `openjdk-N-jre-headless` package, and refuses if apt would take anything else with it
  (a JDK you installed, for example); remove those yourself. Anything else on the machine that
  uses that Java stops working, so check before removing. A removed server that's restored
  later installs its Java again.
- **When the exact Java isn't installed**, a server runs on the closest newer one, which
  usually works. **Maintenance → Java** offers to install the exact one and switch to it.

Uninstalling and purging need an **admin**; other accounts, and visitors on a trusted local
network, don't see these two sections.

- **Uninstall (Keep World)**: removes the server's systemd service, so it leaves the panel.
  Its files stay in `/opt/minecraft-…` and its backups stay too, so it can be
  [restored](#15-restore-a-removed-server).
- **Danger Zone → Purge Data**: removes the service and deletes the server's folder,
  world included. This can't be undone. Backups in `/var/backups/redstonepanel` are kept.

Both also remove the server's backup and restart timers, close its game ports in `ufw`, and
clear it from systemd's list of failed units. Failed units left behind by servers removed
with older versions are cleared the next time the dashboard loads.

### Removing RedstonePanel

Don't just uninstall the package (`apt remove`) or the Windows app. Game servers that are
running or start on their own would stay on the machine with nothing to manage them: on Linux
they keep starting at boot and holding their ports, with no backups, restarts or updates.

Use **Panel Settings → Remove RedstonePanel** (also linked from every server's **Maintenance**
tab), or `sudo redstonepanel uninstall` in a terminal. Both go through the same steps:

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/remove-panel.png"><img src="images/remove-panel.png" alt="Panel Settings, Remove RedstonePanel: back up, then choose for each server"></picture>

1. **Back up every server** (one button; backups stay in `/var/backups/redstonepanel` after the
   panel is gone).
2. **Choose what happens to each server**, or one choice for all of them:
   - **Stop it, keep its files** (recommended): its service is stopped, turned off and removed;
     the world stays in its folder. Install the panel again and bring it back from **New server
     → Removed servers**.
   - **Keep it running without the panel** (Linux only): it stays an ordinary service. Stop it
     later with `sudo systemctl disable --now minecraft-NAME`.
   - **Delete it completely**: needs a backup from the last 24 hours and typing **DELETE**. Its
     backups stay unless you tick **Delete their backups too**.
3. **Remove the panel**: **Uninstall now** (Linux), or `sudo apt remove redstonepanel`
   (`purge` also deletes the panel's accounts and settings). On Windows: **Settings → Apps →
   RedstonePanel → Uninstall**.

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/remove-panel-steps.png"><img src="images/remove-panel-steps.png" alt="Typing DELETE, carrying out the choices and Uninstall now"></picture>

If someone runs `apt remove` without these steps, the panel still never stops a game server or
deletes a world: it turns off its own timers and services and prints every server left running
or starting at boot, with the command to stop it. Installing the panel again turns the left
servers' backup and restart schedules back on.

## 9. Backups

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/backups.png"><img src="images/backups.png" alt="Backups tab, Java server"></picture>

**Set up automatic backups**

1. Tick **Automatic backups**.
2. Choose how often: every hour, every 6 or 12 hours, every day, or every Sunday.
3. Choose the time and how many scheduled backups to **Keep**. Optionally, **Also delete them
   after** a number of days ("keep 7, and none older than 14 days"); the newest backup always
   stays, so a server that has been stopped for a month still has one.
4. Click **Save schedule**. **Next backup** shows when the next one will run.

The time is the **server's clock**, and the form shows its time zone (UTC on most servers). A
"04:00" backup on a UTC server runs at 04:00 UTC, whatever time your browser shows.

**How it works**

- Each schedule is a systemd timer (`redstonepanel-backup@<server>.timer`), so backups happen
  even while the panel itself is stopped. If the machine was off at backup time, the backup
  runs when it boots.
- Running servers are copied consistently:
  - **Java:** the panel pauses saving over RCON while the files are copied. A Java server
    without RCON is copied while it runs, and the panel warns you; open its **Live Chat** tab
    once to set RCON up.
  - **Bedrock:** the server's own `save hold` / `save resume` commands are used.
- A backup is a `.tar.gz` of the server folder: the world, `server.properties`, operators,
  allowlist and so on. It leaves out what can be downloaded again (`server.jar`,
  `bedrock_server`, Bedrock's built-in packs) and logs.
- Backups are stored in `/var/backups/redstonepanel/<server>/`, readable only by root. To have
  backups that survive losing this machine, copy them to an SSH server or S3-compatible storage
  ([off-site copies](#off-site-copies-ssh-or-s3-storage), below) or, in a cluster, to another
  member.
- Only **Scheduled** backups are pruned to the number you keep. **Manual** and **Before
  restore** backups stay until you delete them.
- **Made by** shows the server software and Minecraft version that last wrote the world in
  each backup (Paper 1.21.4, Bedrock 1.26.66.1). It's recorded inside every backup taken since
  1.19; older Java backups show it too, and older Bedrock ones say "not recorded". A Java
  backup made by a newer Minecraft version than the server runs now isn't restored: the older
  version would open the world and damage it. Update the server first, then restore.

**Other buttons**

- **Back up now**: takes a manual backup immediately.
- **⬇️**: downloads a backup.
- **Restore** (admins): stops the server, replaces its world with the one in the backup, and
  starts it again. The world being replaced is saved first as a **Before restore** backup, so
  you can undo a restore by restoring that one. **Before mod changes** backups are taken by
  **Update all** on the Mods / Plugins tab.
- **🗑**: deletes a backup (admins).

**Copies on another cluster member** (cluster masters)

A backup on the same disk doesn't survive losing the disk. In a cluster, open a server's
**Backups** tab on the master and choose **Also keep a copy on**: the master, or another node.

- After each scheduled or manual backup, the master copies it to that member within a few
  minutes (straight away after **Back up now**) and checks it arrived whole with SHA-256. A
  node's backup going to another node passes through the master; nodes never talk to each
  other.
- Copies are kept in `/var/backups/redstonepanel/remote/<member>/<server>/` on the member that
  holds them. **Scheduled** copies are kept to the same number as the server's own; **Manual**
  ones stay until you delete them.
- If a copy fails (the member is off, or its disk is full), it shows under **Needs attention**
  on the master's dashboard, and as **Backup copy to another member or storage failed** in
  webhook notifications. The master tries again every few minutes and catches up on what it missed.
- **⬇️** downloads a copy. **Put back** (admins) copies it into the server's own backups, where
  **Restore** brings its world back.
- If a machine is lost, its servers' copies are still on the other member: make a new server
  of the same edition and version, open its **Backups** tab, click **Use a copy of another
  server's backup…**, **Put back** the copy you want, and **Restore** it.
- Every member needs RedstonePanel 1.16.8 or later. Standalone panels don't have this; they
  can use off-site copies instead.

### Off-site copies (SSH or S3 storage)

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/offsite-copies.png"><img src="images/offsite-copies.png" alt="Off-site copies on the Backups tab"></picture>

Any panel, with or without a cluster, can copy its servers' backups off the machine:

- **An SSH server**: a NAS, another PC or a VPS with `rsync` installed. Copies resume where they
  stopped if the connection drops.
- **S3-compatible storage**: Backblaze B2, Wasabi, Cloudflare R2, MinIO, AWS S3 and others.
  Archives over 100 MB go up in parts.

An admin adds the storage once under [Panel Settings → Backup storage](#backup-storage). After
that, each server's **Backups** tab has an **Off-site copies** section:

- Tick **Copy to** a storage to copy this server's backups there (a storage can be set to copy
  every server's backups, which is the default).
- After each scheduled or manual backup, the copy starts straight away in the background, so a
  slow upload never holds the server up, and it happens even while the panel itself is stopped.
  The panel also checks every few minutes for anything missing and copies it.
- Every copy is checked with SHA-256 when it arrives, and again before it's restored. A
  `.sha256` file is kept next to each copy.
- What's copied: every **Manual** backup, and the newest **Scheduled** ones, up to the storage's
  own **Keep** (or the server's, if the storage doesn't set one) and, if set, no older than its
  **Delete copies after** days. Older scheduled copies are deleted from the storage; the newest
  copy always stays. **Before restore** and **Before mod changes** backups stay on this machine.
  So you can keep, for example, 7 here and 30 days off the machine.
- **Restore** (admins) downloads the copy into the server's backups, checks it, and restores
  its world as usual (the current world is saved first as a **Before restore** backup).
- **🗑** deletes a copy from the storage (admins). **Copy now** copies anything missing straight
  away.
- If copying fails (the storage is off, the key was removed, the bucket is full), it shows under
  **Needs attention** on the dashboard and as **Backup copy to another member or storage
  failed** in webhook notifications, once until it works again. The panel keeps trying every
  few minutes.

On the storage, copies are kept as `<folder>/<panel id>/<server>/<backup>.tar.gz`. The panel id
is this machine's host name and four random characters (shown in Panel Settings), so several
panels can share one bucket or folder.

**If the machine dies:** install RedstonePanel on the new machine, add the same storage under
**Panel Settings → Backup storage** (for SSH, add the new panel's key on the storage too), then
**➕ New Server → From backup storage**: pick the backup, and the server is set up again with its
old name if it's free, its server software downloaded again (a Java server at the same
Minecraft version and type), and started.

From the command line, `sudo redstonepanel offsite-copy minecraft-java` copies a server's
backups now, and `sudo redstonepanel offsite-key` prints the panel's SSH key.

Bedrock servers work the same way:

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/backups-bedrock.png"><img src="images/backups-bedrock.png" alt="Backups tab, Bedrock server"></picture>

From the command line, `sudo redstonepanel backup minecraft-java` takes a backup (and starts
its off-site copy).

### Scheduled restarts

**Maintenance → Scheduled Restarts** restarts the server every day, every 6 or 12 hours, or
every Sunday, at the time you pick (the server's clock).

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/scheduled-restarts.png"><img src="images/scheduled-restarts.png" alt="Scheduled restarts"></picture>

1. Tick **On**.
2. Choose how often and the time.
3. Choose a warning: none, or 1, 5, 10 or 15 minutes.
4. Click **Save schedule**. **Next restart** shows when it will happen.

Players see "Server restarts in 5 minutes" in chat, then again 1 minute and 10 seconds before,
and the restart happens at the time you chose. Things to know:

- Java servers are warned over RCON. Open the **Live Chat** tab once so the panel sets it up;
  without it the restart still happens on time, without a warning.
- A server you stopped stays stopped.
- If a backup of the same server is running, the restart waits for it.
- The schedule is a systemd timer (`redstonepanel-restart@<server>.timer`), so it works while
  the panel is stopped.

## 10. World & Maps

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/world.png"><img src="images/world.png" alt="World &amp; Maps"></picture>

- **Download World**: only the world folder, as a zip. Use it to open the world in
  single-player or a map viewer.
- **Rename World**: renames the world folder, updates `level-name` in `server.properties`, and
  restarts the server.

## 11. Files

**Files** (admins, and users given **Files** on the server) shows the server's folder in `/opt/minecraft-…`, so you can change
things the other tabs don't cover: plugin and mod settings, `ops.json`, `bukkit.yml`, and so on.

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/files.png"><img src="images/files.png" alt="Files tab"></picture>

- Click a folder to open it; the path at the top goes back up.
- **Upload**, or drag files from your computer onto the tab, adds them to the folder you're
  in, with a progress bar for each. Uploading a file that already exists asks before
  replacing it. Up to 512 MB per file.
- Mods, plugins and Bedrock add-ons dropped at the top of the folder (or into `mods` or
  `plugins`) are checked and put in the right place, as on the
  [Mods / Plugins / Add-ons](#12-mods-plugins-and-add-ons) tab.
- **Download** saves a file. To get a whole folder, use **Export Server** (Maintenance) or a
  [backup](#9-backups).
- **Rename**, **Delete** (a folder is deleted with everything in it), **New folder**,
  **New file**.
- Text files up to 1 MB (`.yml`, `.json`, `.properties`, `.toml`, `.txt`, logs and the like)
  open in the editor. **Save** (or Ctrl+S) writes the file. If the server changed the file
  after you opened it, saving stops and asks you to reload it, so neither change is lost
  silently.

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/file-editor.png"><img src="images/file-editor.png" alt="Editing a plugin&#x27;s config.yml"></picture>

- A file that is a list of `name=value` settings (`server.properties`, `eula.txt`, most `.cfg`,
  `.conf` and `.ini` files, any `.properties` file) opens in a **Visual Editor** like the one
  on [Configuration](#7-configuration-player-roles-and-the-whitelist): one card per setting, with **True**/**False** lists,
  number boxes and a filter. **Visual Editor** and **Raw Text** above it switch between the
  form and the plain text. They show the same file, so a change made in one is there in the
  other, and the panel opens the next file in the view you used last.
- The form changes only the values you change. Comments, blank lines, spacing and the order of
  the file stay as they were. To add or remove a setting, or edit a comment, use **Raw Text**.
- The switch appears only when every line of the file fits. `.yml`, `.json` and `.toml` files
  have their own layout and always open as text.

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/file-visual.png"><img src="images/file-visual.png" alt="server.properties in the Files tab&#x27;s Visual Editor"></picture>

Most servers read their files when they start, so **Restart** after changing settings.
Some plugins have a reload command; check the plugin's documentation.

Safety:

- Nothing outside the server's folder can be reached. Symlinks are listed (with where they
  point) but never followed, and a file with other hard links isn't opened.
- Files and folders the panel creates belong to the `minecraft` user, like the rest of the
  server.
- Every change is in the activity log with who made it (`[FILES] alex saved …`).

### SFTP (FileZilla, WinSCP)

For moving a whole world or many files at once, use an SFTP program such as FileZilla or WinSCP
instead of the browser. SFTP is **off** until an admin turns it on under
**🛡️ Settings → SFTP** (port 2022 unless you change it). After that, the top of each server's
**Files** tab shows what to type:

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/sftp.png"><img src="images/sftp.png" alt="SFTP host, port and username on the Files tab"></picture>

- **Host** and **Port**: the panel's address and the SFTP port. **Open** starts your SFTP
  program with them filled in, and **Copy** copies the `sftp://` address.
- **Username**: your account, a dot, and the server, such as `alex.minecraft-java`. Each
  sign-in reaches one server's folder and nothing else.
- **Password**: your panel password (or Linux or Windows password, for such an account).
- Admins reach every server. A user account needs **Files** on the server
  (see [What a helper may do](#what-a-helper-may-do)); taking it away ends their open session
  within 30 seconds.
- The first time, your SFTP program shows the server's **host key**. Compare it with the one
  on the Files tab before accepting.

**SSH keys.** Under **🔑 Password → SSH keys**, paste a public key (the `.pub` file) to sign in
without a password. Accounts with [two-factor sign-in](#two-factor-sign-in) **must** use a key,
because an SFTP program can't ask for the six-digit code.

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/ssh-keys.png"><img src="images/ssh-keys.png" alt="SSH keys in the Password dialog" width="420"></picture>

Good to know:

- It is SFTP only: no shell, no commands, no port forwarding. Files go through the same
  safety checks as the Files tab (nothing outside the folder, symlinks never followed) and
  belong to the `minecraft` user. On Windows they get the server's own account's rights, and
  names Windows can't hold (`CON`, `a:b`, a name ending in a dot) are refused.
- Uploads, deletes and renames are in the activity log (`[SFTP] alex uploaded …`), and wrong
  passwords count towards the same limit as the sign-in page (5 in 10 minutes).
- Turning SFTP on opens its port in this machine's firewall (ufw, or Windows Defender
  Firewall); turning it off closes it. For people outside your home, forward the port on your
  router too.
- On Linux it needs the `python3-paramiko` package, which is installed with the panel unless
  you installed without recommended packages; the setting tells you if it is missing. On
  Windows (from 1.24) the installer carries it, and SFTP runs as the Windows service
  **redstonepanel-sftp**.
- **On a cluster**, each machine serves SFTP for its own servers. For a server on a node, turn
  SFTP on in that node's own panel and sign in with an account of that panel; the master's
  accounts don't reach node servers over SFTP.

## 12. Mods, plugins and add-ons

Admins see a **Mods** tab on Fabric, Quilt, Forge and NeoForge servers, **Plugins** on Paper
and Purpur servers and **Add-ons** on Bedrock servers. Vanilla servers can't load mods or plugins, so they don't have one.

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/mods.png"><img src="images/mods.png" alt="Mods tab on a Fabric server"></picture>

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/plugins.png"><img src="images/plugins.png" alt="Plugins tab on a Paper server"></picture>

**Adding:** drag `.jar` files (Java servers) or `.mcpack` / `.mcaddon` files (Bedrock) onto
the tab, or click the box to choose them. Each one is checked before it's installed:

- A mod goes into `mods/`, a Paper or Bukkit/Spigot plugin into `plugins/`.
- A file that can't work on this server is refused with the reason: a mod for another loader
  (a Forge mod on a Fabric server, a Fabric mod on a NeoForge server), a plugin on a mod
  server, a mod on a Paper or Purpur server, a proxy (Velocity, BungeeCord) plugin, or a jar
  that isn't a mod or plugin at all. Quilt servers take Fabric mods too, and NeoForge servers
  take Forge mods up to Minecraft 1.20.4, where the two were still the same.
- A newer version of something already installed (same mod id, different file name) asks
  first and then replaces the old file, so the server never has two copies.
- The panel only reads each jar's description file; nothing in it is run.

**The list** shows each mod or plugin's name, version, authors and file, and warns when:

- it was made for a different Minecraft version than the server runs (from the mod's
  `fabric.mod.json`, `quilt.mod.json` or `mods.toml`, or the plugin's `api-version`);
- it needs another mod or plugin that isn't installed, for example *Needs fabric-api* (most
  Fabric mods need [Fabric API](https://modrinth.com/mod/fabric-api)).

**Disable** renames the file to `….jar.disabled`, which the server skips; **Enable** renames
it back. **Remove** deletes it. Changes apply when the server restarts; the tab offers
**Restart now**.

**Bedrock add-ons.** A `.mcpack` holds one pack, a `.mcaddon` usually a behavior pack and a
resource pack. Each pack is unpacked into `behavior_packs/` or `resource_packs/` and added to
the active world (`world_behavior_packs.json`, `world_resource_packs.json`), so it's on after
a restart. **Disable** takes a pack out of the world and keeps its files; **Remove** deletes
it. Players download resource packs only if they accept server packs. Skin packs and world
templates are refused.

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/addons-bedrock.png"><img src="images/addons-bedrock.png" alt="Add-ons tab on a Bedrock server"></picture>

### Server tools: Phantom (Bedrock)

Consoles and phones list a Bedrock server under **Friends → LAN Games** only when it runs on
port 19132. [Phantom](https://github.com/jhead/phantom) answers those LAN pings for a server
on any other port and forwards players to it. The Add-ons tab of a Bedrock server has a
**Server tools** card for it (admins):

- **Install** downloads Phantom 0.5.4 from GitHub once, checks it against a SHA-256 built into
  the panel (Phantom publishes none), and keeps it in `/usr/local/lib/redstonepanel/phantom/`.
  It then creates `phantom-<server>.service`, which runs as the `minecraft` user, forwards to
  the server on this machine and starts at boot, and opens UDP 19132 and the Phantom's own
  port (19300 and up, one per server) in ufw.
- The card shows whether it's running, where it forwards, the UDP ports it listens on and its
  recent log, with **Stop** / **Start**, **Restart** and **Uninstall**. Uninstall stops and
  deletes the service and closes the ports no other Phantom needs.
- If you change the server's port, the card says the Phantom points at the old one; **Repair**
  points it at the new port.
- Several servers can each have a Phantom: they share UDP 19132. A Bedrock server on 19132
  can't share it, so Install is refused while one is on this machine (move it to another port
  in Configuration). A server on 19132 needs no Phantom anyway.
- A server using the NetherNet transport (`transport=nethernet`) gets no Phantom: Phantom only
  relays the older RakNet protocol. It doesn't need one either: it announces itself to
  consoles on the same network on UDP 7551, which the panel opens in ufw, and it doesn't hold
  UDP 19132 (its `server-port` is TCP), so other servers' Phantoms can run next to it.
- Phantom services you made yourself that point at the server or use its port are listed too,
  with what's wrong with them, Start / Stop and **Remove unit**, which deletes only the unit
  file, never the program. If yours is called `phantom-<server>.service`, remove it before
  installing the panel's.

Removing a server removes its Phantom as well.

### Player packs

*(1.21.6)* On a Fabric, Quilt, Forge or NeoForge server, players need the same mods as the
server to join. The **Player pack** view of the Mods tab works out that list from the
server's own `mods` folder and hands it to players on the server's [join page](#join-pages)
as a pack their launcher installs in one go. It follows the folder: add, update or remove a
mod and the pack players download is the new one, with a new pack number.

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/player-pack.png"><img src="images/player-pack.png" alt="The Player pack view of the Mods tab"></picture>

For each file the panel asks Modrinth which mod it is and which side of the game it runs on:

| Modrinth says | In the pack |
| --- | --- |
| Players need it | **In the pack** |
| Works without it on the player's side | **Optional**: the launcher lets the player leave it out |
| Works without it, but a mod in the pack needs it (Fabric API, for one) | **In the pack** |
| Runs only on the server | **Left out** |
| Modrinth doesn't know the file | **Left out**, and named on the join page so players know to ask |

- The menu on each line overrules that: **Include**, **Optional** or **Leave out**.
  **Include** on a file Modrinth doesn't know copies the file itself into the pack (up to
  64 MB of such files together). Only do that with mods you may hand out.
- **Extras for players** adds mods the server doesn't run, such as shaders or a minimap, by
  their Modrinth name or page address. They go in as optional.
- **Player pack: Automatic** is on, unless the server was made **From a modpack** on
  Modrinth: its join page then links to that pack. Choose **On** if you have added or removed
  mods since, or **Off** to hand out nothing.
- **Download .mrpack** gives you the same file players get.

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/join-page-pack.png"><img src="images/join-page-pack.png" alt="A join page offering the server's player pack"></picture>

The pack is a Modrinth pack (`.mrpack`), which Modrinth App, Prism Launcher and most other
launchers import. It holds no mods, only where each one is on Modrinth and its checksum, so
the launcher downloads them from Modrinth. The join page also offers the pack as a
[packwiz](https://packwiz.infra.link) folder for players who set their launcher up to check
the server's mods every time the game starts.

The panel needs to reach Modrinth to work the list out. If it can't, the last pack it built
stays in place.

### Mods and plugins that open their own port

Some mods and plugins open a network port of their own next to the game port, with the same
default on every server. Two servers on one machine that both run one of them would fight over
it: the second can't open it, and Simple Voice Chat on Fabric even stops the whole server. The
panel knows where these keep their port:

| Mod or plugin | Config file | Default |
|---|---|---|
| Simple Voice Chat | `config/voicechat/` or `plugins/voicechat/voicechat-server.properties` (`port`) | UDP 24454 |
| Geyser | `config.yml` in its folder (`bedrock: port`) | UDP 19132 |
| BlueMap | `webserver.conf` (`port`) | TCP 8100 |
| Dynmap | `configuration.txt` (`webserver-port`) | TCP 8123 |
| Plan | `plugins/Plan/config.yml` (`Webserver: Port`) | TCP 8804 |
| NuVotifier | `config.yml` in its folder (`port`) | TCP 8192 |

- When two servers are set to the same port, or one is set to a port a server uses for the game,
  RCON or query, the panel moves one of them to the **next free port** in its config file (a port
  no other server uses and nothing on the machine is listening on). The server that has the port
  now keeps it. The panel checks before it starts a server, after a server stops unexpectedly (so
  the restart 20 seconds later works), and every two minutes. Most of these mods write their
  config file the first time they start, so a new one is moved after that first start.
- A server whose port moved uses the new one from its next start. **Needs attention** says which
  port moved and where to until you restart it.
- The panel opens these ports in the firewall (`ufw`) with the game port, and **Port
  forwarding** lists them. Voice chat and Geyser are for players outside; the web maps, Plan and
  NuVotifier are listed under "Only if you need them".
- Geyser with `clone-remote-port: true` and voice chat with `port=-1` share the game port's
  number and are left alone. If a mod the panel doesn't know can't open its port, the **Last
  crash** card says a mod's port was taken, and names the port when the log does; the **Port
  check** (under Port forwarding) names what holds it and, when one config line sets it, moves it.

### Finding mods and plugins

On Java servers other than Vanilla, **Find mods** (or **Find plugins**) is a catalog of
[Modrinth](https://modrinth.com) and, for Paper and Purpur, [Hangar](https://hangar.papermc.io),
PaperMC's plugin site. It lists what Modrinth has for the server's own type: Forge mods on a
Forge server, NeoForge mods on a NeoForge server, Quilt and Fabric mods on a Quilt server. It opens on:

- **Bundles**: a few mods or plugins that belong together, installed with one click.
  - Fabric: **Performance pack** (Lithium, FerriteCore, Krypton, C2ME, ScalableLux), **Server
    essentials** (spark, Chunky, LuckPerms, Ledger) and **Bedrock players** (Geyser,
    Floodgate).
  - Paper: **Server essentials** (Chunky, LuckPerms, EssentialsX, CoreProtect), **Build
    protection** (WorldEdit, WorldGuard, CoreProtect) and **Bedrock players** (Geyser).
  - Quilt has Fabric's bundles and Purpur has Paper's.
  - NeoForge: **Performance pack** (Lithium, FerriteCore, ModernFix, ScalableLux), **Server
    essentials** (spark, Chunky, LuckPerms) and **Bedrock players** (Geyser, Floodgate).
  - Forge: **Performance pack** (FerriteCore, ModernFix, ServerCore) and **Server essentials**
    (spark, Chunky, LuckPerms).

  A bundle installs only what's missing, with what each one needs, after a world backup
  (**Back up the world before installing a bundle**, on by default; skipped when there's
  nothing to install). One that can't be installed is named and doesn't stop the others.
- **A starter set** of popular server mods or plugins, picked for RedstonePanel and grouped
  as Performance, Admin tools, For players and Libraries. One that hasn't been updated for the
  server's Minecraft version yet is shown greyed out as **Not for 1.21.x yet**.
- **Most downloaded**, below that, and category buttons to browse by (Optimization,
  Management, Economy, …).

Each card shows downloads, when it was last updated, its categories, and **Installed** or
**Update available** when it's on the server. Search by name, pick a **category**, and sort by
**most downloaded**, **most followed**, **recently updated** or **newest**; **Back to the
catalog** clears them. Only projects with a build for the server's loader and Minecraft
version, and that run on a server, are listed, so client-only mods such as Sodium don't
appear. Paper has spark built in, so it's only offered for Fabric.

**Click a mod or plugin** for its details: description, downloads, followers, licence, links
to its page and source code, and exactly what Install would do: the version and file it
picks, and the mods or plugins it needs that would be installed with it (for example
WorldGuard brings WorldEdit).

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/mods-details.png"><img src="images/mods-details.png" alt="Details of a mod before installing it"></picture>

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/mods-catalog.png"><img src="images/mods-catalog.png" alt="The mod catalog on a Fabric server"></picture>

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/mods-browse.png"><img src="images/mods-browse.png" alt="Finding mods on Modrinth"></picture>

**Install** picks the newest release build (a beta only when there's no release, never an
alpha), downloads it from the site's own download server, checks it against the checksum the
site gives, and then installs it like a dropped file. Mods and plugins it needs are installed
first, for example Fabric API. A dependency that isn't on the same site, or a Hangar plugin
that's downloaded from somewhere else, is named so you can get it yourself.

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/plugins-hangar.png"><img src="images/plugins-hangar.png" alt="Plugins from Hangar on a Paper server"></picture>

### Updates

When you open the tab, the panel asks Modrinth and Hangar whether anything installed has a
newer build for the server's Minecraft version. Jars installed from the tab are known by
their project; jars you added by hand are looked up on Modrinth by their checksum, so most of
them are found too. Each one shows where it's from, with **Update to …** when there's
something newer.

![Mods with an update available](images/mods-updates.png)

- **Update all** updates every one, and can back up the world first (ticked by default): the
  backup shows on the Backups tab as **Before mod changes**.
- An update keeps the old file as `….jar.previous`, which the server doesn't load, and
  **Undo** puts the last update's old files back. They're kept until the next update.
- Restart the server to load the new versions.

Where each jar came from is kept in `.redstonepanel-mods.json` in the server folder.

Every install, change and removal goes in the activity log (`[MODS] alex installed Sodium …`).

## 13. Console

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/console.png"><img src="images/console.png" alt="Console"></picture>

**🖥️ Console** opens the panel's activity log at the bottom of the screen, beside the sidebar:
deploys, updates, service starts and stops, firewall changes, backups, and who signed in. Lines
are coloured by kind: errors and crashes red, warnings yellow, backups and restarts blue,
sign-ins and settings purple, commands the panel runs grey. The same log is in
`/var/log/redstonepanel/panel.log`.

It also opens by itself when you start something that takes a while, such as creating, starting
or stopping a server, a backup or a restore, so you can watch it happen. While it's open, the
page above it ends where the console starts, so you can still scroll to everything on it, and
the button you clicked stays in view. It takes up to a third of the window's height. Close it
with **❌ Close** at its top right; **🖥️ Console** in the sidebar opens it again.

## 14. Templates

**📋 Templates** opens the template manager. A template is a set of `server.properties`
values plus an operators/permissions list, for one edition.

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/templates.png"><img src="images/templates.png" alt="Template manager"></picture>

- Create one from a server you've set up: **Maintenance → Save as Template**.
- Edit its properties and permissions here, then click **Save All Updates**.
- Use it when deploying (**➕ New Server → Base Template**), or apply it to an existing
  server (**Maintenance → Apply Template**). Ports are never copied from a template, so
  servers made from the same template don't clash.

## 15. Restore a removed server

A server uninstalled with **Keep World** still has its files in `/opt`. The sidebar shows
**♻️ N removed servers: restore**, and they are listed under **➕ New Server → Removed servers**
(see the [New Server dialog](#4-create-or-import-a-server)).

- **Restore** gives the server a new systemd service under the same name and starts it:
  - it keeps its old port, unless another server took that port in the meantime; then it moves
    to the next free one and tells you;
  - the server binary is downloaded only if it's missing;
  - its backup schedule comes back.
- **🗑** (admins) permanently deletes the removed server's files. This can't be undone.

The same from a terminal:

```bash
sudo redstonepanel removed
sudo redstonepanel restore minecraft-java-2
```

## 16. Users

**👥 Users** (admins) manages panel accounts.

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/users.png"><img src="images/users.png" alt="Panel users"></picture>

- **Add user**: a username, a password of at least 8 characters, and a role:
  - **User** can do what you allow on the servers you pick, under **Servers**
    (see [What a helper may do](#what-a-helper-may-do)). A new user account sees no servers
    until you give it some;
  - **Admin** can do everything on every server, and also manage panel users and Panel
    Settings, create, import, uninstall and purge servers, restore backups, and delete backups
    and removed servers.
- For each account you can change the role, **Reset password**, **Disable** (it can't sign
  in until enabled again) or **Delete**.
- Accounts with two-factor sign-in on show **2FA**. **Reset two-factor** turns it off for
  someone who lost their phone; they sign in with their password and set it up again.
- Linux accounts aren't listed here; they're managed with the usual Linux tools.

An admin account controls a service that runs as root, so treat it like `sudo` access.

### What a helper may do

**Servers** next to a user account opens what that account may do, server by server:

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/user-servers.png"><img src="images/user-servers.png" alt="What a user account may do on each server"></picture>

| Tick | Lets them |
| --- | --- |
| **View** | See the server on the dashboard and in the list, its Overview, performance and logs. |
| **Console & chat** | Read the console, send commands and chat. |
| **Start/stop/restart** | Start, stop and restart it, and dismiss a crash card. |
| **Players** | Whitelist, operators, kick, ban, and the access log. |
| **Files** | The Files tab, **Export Server**, and SFTP for this server. |
| **Mods & plugins** | Install, update, turn off and remove mods, plugins and add-ons. |
| **Settings** | `server.properties`, scheduled restarts, updating the server and its update settings, saving and applying templates. |
| **Backups** | Make and download backups and world downloads, and set the schedule. Restoring stays with admins. |

- Pick **Moderator** (view, console and players) or **Co-owner** (everything in the table)
  from the list, or tick boxes yourself. **No access** hides the server from them.
- **Every server** at the top applies to all servers, including ones added later.
- Creating, importing, uninstalling and restoring servers, editing templates, Java, memory
  and limits, moving servers between machines and Panel Settings stay with admins whatever
  is ticked.
- A user sees only their servers: the dashboard, the list, Needs attention and the activity
  log leave the rest out. With nothing ticked they see a note to ask an admin.
- On a cluster, the master keeps what its users may do on node servers too.
- **Updating from before 1.18:** user accounts that already existed keep what users could
  always do (everything in the table except **Files** and **Mods & plugins**) on every
  server, so nothing changes for them until you narrow it. Visitors let in by trusted-LAN
  sign-in have the same rights.

## 17. Panel Settings: name, HTTPS, sign-in

**🛡️ Settings** (admins).

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/panel-settings.png"><img src="images/panel-settings.png" alt="Panel Settings: name, notifications and cluster"></picture>

**Panel name**: shown in the sidebar, the browser tab and on the sign-in page. It defaults to
the machine's host name and helps when you run several panels.

**Setup guide**: how many of the dashboard's setup steps are done, with **Show it on the
dashboard** or **Hide it** (see [The dashboard](#3-the-dashboard)).

**Connection**:

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/panel-settings-1.png"><img src="images/panel-settings-1.png" alt="Panel Settings on a cluster master: connection and sign-in"></picture>

- **HTTP (simple)**: port 8080, fine on a trusted home network.
- **HTTPS with a self-signed certificate**: the panel makes its own certificate authority.
  Download its CA certificate from here and install it on your devices once to get rid of
  the browser warning.
- **HTTPS with my own certificate**: point it at a certificate and key you already have, for
  example from certbot on Linux or [win-acme](https://www.win-acme.com) on Windows (choose to
  store the certificate as **PEM files**, then give the `-chain.pem` and `-key.pem` files here).
  When the certificate is renewed, the panel notices within ten minutes and restarts itself to
  serve the new one *(1.24)*; no hook script is needed.

In a cluster, the choices are different (as in the screenshot): a node always uses the
cluster's HTTPS, and a master chooses between **HTTPS from the cluster** and **HTTPS with my
own certificate**. See [Clusters](#18-clusters).

**SFTP**: tick **Let people move files with FileZilla or WinSCP (SFTP)**, choose the port
(2022 by default) and **Save SFTP**. It's off until you turn it on. See
[SFTP](#sftp-filezilla-winscp).

### Notifications

Get a message in Discord, or any service that takes a webhook, when something happens.

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/notifications.png"><img src="images/notifications.png" alt="Notification settings"></picture>

1. In Discord, open the channel's settings → **Integrations** → **Webhooks** → **New
   Webhook**, then **Copy Webhook URL**.
2. Paste it into **Notifications** and tick the events you want:
   - **Server crashed**: the server stopped without being asked to. It starts again by itself
     after 20 seconds (Java and Bedrock alike).
   - **Server started** / **Server stopped**: every start and stop, including your own.
   - **Scheduled restart**: a [scheduled restart](#scheduled-restarts) happened, or failed.
   - **Backup finished** / **Backup failed**.
   - **Update available**: a newer version is out for a server, once per version (see
     [Update alerts](#update-alerts-and-automatic-bedrock-updates)).
   - **Bedrock server updated** / **Server update failed**: a Bedrock update finished, or
     didn't and the old version was put back.
   - **Backup copy to another member or storage failed**: an
     [off-site copy](#off-site-copies-ssh-or-s3-storage) or a copy on another cluster member
     failed (once, until it works again).
   - **Player joined** / **Player turned away**: from the access log (see
     [Whitelist & Access](#7-configuration-player-roles-and-the-whitelist)), within about 30 seconds.
     Turned-away messages are sent at most once a minute per server.
   - **TPS under 15 for 5 minutes** (Java servers whose tick health the panel can read) and
     **Disk over 95 % full** (any disk holding servers, backups or panel data): at most once an
     hour per server or disk. Both are off until you tick them.
3. Click **Save notifications**, then **Send a test**.

Setting up a whole Discord for your community, with a private space per Minecraft server, is
explained step by step on the [Discord setup](../wiki/Discord.md) page.

By default only crashes, failed backups and scheduled restarts are sent. A server that keeps
crashing sends one message every 5 minutes, not one per crash.

Crash, start and stop messages come straight from systemd, so they arrive even while the panel
is stopped. The webhook URL lets anyone post in your channel, so the panel stores it readable
only by root and never shows it again. Paste a new one to replace it, or **Remove webhook**.

Other services receive a JSON POST with the fields `text`, `event`, `server`, `panel`,
`message` and `time`.

### Backup storage

Where the [off-site copies](#off-site-copies-ssh-or-s3-storage) of this machine's backups go.

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/backup-storage.png"><img src="images/backup-storage.png" alt="Backup storage settings"></picture>

Click **Add storage** and choose:

- **SSH server (rsync)**: the **Host**, **Port**, **User** and **Folder** (relative to that
  user's home unless it starts with `/`). Open **This panel's SSH key** and add the line shown to
  `~/.ssh/authorized_keys` of that user on the SSH server. That machine needs `rsync` (`sudo apt
  install rsync`). The panel records the server's host key on the first connection, as `ssh`
  does, and refuses to send anything if it changes later; if that machine was reinstalled, click
  **Forget host key** and **Test** again.
- **S3-compatible**: the **Endpoint** (for example `https://s3.eu-central-003.backblazeb2.com`,
  `https://<account>.r2.cloudflarestorage.com` or `http://192.168.1.30:9000` for MinIO), the
  **Bucket**, an optional **Folder in the bucket**, the **Region** (`auto` for Cloudflare R2) and
  an **Access key** and **Secret key** that can read, write, list and delete in that bucket. Leave
  **Path-style addresses** ticked unless your provider needs bucket names in the host name.
  The secret is stored on this machine readable only by root and never shown again; leave it
  empty when editing to keep it.

For both:

- **Scheduled copies to keep**: empty keeps as many as each server keeps here. **Delete copies
  after (days)**: empty never deletes by age. Manual copies stay until you delete them.
- **Copy every server's backups** (the default), or leave it off and tick the storage on each
  server's **Backups** tab.
- **Test** writes a small file, lists it and deletes it, and says which step failed.
- **Remove** stops copying there. The copies already there stay; delete them on the storage if
  you want them gone.

Each machine in a cluster copies its own servers' backups: add the storage on each node's own
panel (the master can't change a node's settings).

**Sign-in**:

- **Always required (recommended)**.
- **Not required on the local network**: devices on the private networks you list can manage
  servers without signing in. Things to know:
  - they get the *User* role, so users, settings and uninstalling servers still need an
    admin sign-in;
  - it never applies to requests coming through a reverse proxy;
  - everyone on those networks gets in, including guests on your Wi-Fi.

**Require two-factor for admins** (off by default): an admin without
[two-factor sign-in](#two-factor-sign-in) is sent to set it up right after signing in, and
can't do anything else until they have. Set it up for your own account first; the panel won't
turn this on before you do. While it's on, admins can't turn their own two-factor off (another
admin or `reset-2fa` can reset it). Users (not admins) can still choose.

Trusted-network access is unchanged by two-factor: it never gives admin rights. Cluster nodes
don't use it either: the master reaches them with its node token, not a person's sign-in.

If a change locks you out, run `sudo redstonepanel reset-security` on the machine (see
[Troubleshooting](#20-troubleshooting)).

**Don't forward port 8080 to the internet.** For remote access use a VPN (WireGuard,
Tailscale), or a reverse proxy with a real certificate as described in the
[README](../../README.md#exposing-the-panel-to-the-internet).

### Servers folder

Where new servers are made. Out of the box that is `/opt`, on the system disk. If your room is
on a second drive or another partition, type the full path of a folder there, for example
`/srv/minecraft` or `/mnt/disk2/minecraft`, and click **Save folder**. The panel makes the
folder if it isn't there and shows how much space is free on that disk. Leave the box empty to
go back to `/opt`.

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/servers-folder.png"><img src="images/servers-folder.png" alt="The Servers folder section of Panel Settings"></picture>

- **New servers go there**: ones you create, import, restore from backup storage, install from
  a modpack or move in from another cluster member.
- **Servers you already have stay where they are** and keep working. The section lists any
  other folder that still has servers in it.
- The panel says no, with the reason, to a folder it can't use: a system folder, the top
  folder of a disk such as `/mnt` (give a folder of its own inside it), a name with spaces, a
  disk mounted read-only or `noexec`, or a folder the `minecraft` account can't get into.
- In a cluster, each machine has its own servers folder, set in that machine's own panel.

## 18. Clusters

Running RedstonePanel on several machines? One panel can be the **master** of a cluster and
manage every **node's** game servers. In the master's sidebar, each node's servers are listed
under the node's name (🌐), and opening one gives you every tab, just like a server on the
master. The **🌐 Cluster** page shows every panel at once, with start, stop, restart and **Open**
for each server, and each panel's CPU, memory and free disk. The master's dashboard has a card
per panel.

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/cluster.png"><img src="images/cluster.png" alt="The Cluster page"></picture>

**A node's server, opened on the master.** The header says which machine it's on
(🌐 Creative Lab), and every tab works as it does on the master: changes go through the master
to the node.

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/node-server.png"><img src="images/node-server.png" alt="A node&#x27;s Fabric server opened from the master"></picture>

**Setting up a cluster**

1. On the master: **Settings → Cluster → Make this panel a master**, give the cluster a name,
   and click **Create cluster**. The panel restarts on HTTPS (port 8443).
2. On the master: make a **join code**. Codes work once and expire after an hour. Or click
   **Find panels on your network** *(1.16.13)*: the master lists the other RedstonePanels on its
   local networks (it asks each address on ports 8443 and 8080), says which are already in a
   cluster, and **Add to cluster** makes the code and links to the panel to paste it into.

   <picture><source media="(prefers-color-scheme: light)" srcset="images/light/cluster-find.png"><img src="images/cluster-find.png" alt="Find panels on your network"></picture>
3. On each node: paste the code under **Settings → Cluster → Join a cluster**, and click
   **Join**. Or run the command shown with the code: `sudo redstonepanel join '…'` on Linux, or
   `redstonepanel join …` in a command prompt opened as administrator on Windows.
4. Download the cluster CA from **Settings → Cluster** and install it on your devices once.

**Linux and Windows together** *(1.24)*: a cluster can mix Ubuntu, Debian, Raspberry Pi and
Windows machines, and any of them can be the master. Each member says which system it runs
(**Settings → Cluster** and the **🌐 Cluster** page show *Linux* or *Windows*). Everything above
works between them, including moves, copies and backup copies:

- **Java servers** move both ways as they are. A Forge or NeoForge server starts with the
  arguments file for the system it lands on.
- **Bedrock servers** get the new machine's own Bedrock program, of the version they ran when
  minecraft.net still offers it (else the newest), downloaded during the move; worlds, packs and
  settings come along, and the other system's program is removed.
- **File names Windows can't hold.** Linux allows names Windows refuses or reads differently:
  device names (`CON`, `NUL`, `COM1`, also as `con.txt`), a colon or `< > " | ? *`, a name ending
  in a dot or space, and two names that differ only in upper and lower case. Before a server
  moves or is copied to a Windows machine, the old machine checks its folder and the move is
  refused with the list, before anything is stopped. Rename or remove them (in **Files**) and
  try again. A Windows panel checks the same when it restores a backup or imports an archive.
- Both machines need RedstonePanel 1.24 or later for a Windows member; Linux members on 1.16 or
  later still move servers between themselves.

**Creating a server on a node:** in **New Server**, pick the node under **Where**. A template
you choose is copied to the node first, ports are picked on the node, and if the node doesn't have
the Java that Minecraft version needs, you're asked to install it there. **Save as Template** on a
node's server adds the template to the master's list.

**Moving a server to another machine** *(1.16)*: on the **🌐 Cluster** page, admins have
**Move…** and **Copy…** next to every server. Choose where it goes:

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/cluster-move.png"><img src="images/cluster-move.png" alt="Moving a server to another cluster member"></picture>

A move stops the server, packs its whole folder (world, server software, mods and settings),
streams it through the master to the other machine, where every byte is checked, and starts it
there. It keeps its name and ports when they're free on the new machine; if not, it gets others
and the window tells you which. The Java it needs is installed there, and its memory, backup and
restart schedules and Phantom come along. A progress bar follows the copy, and both consoles log
each step (`[MOVE] …`).

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/cluster-moved.png"><img src="images/cluster-moved.png" alt="A finished move, with the old copy still kept"></picture>

The old machine keeps the files as a removed server marked **moved to …**. Check the server on
its new machine, then click **Delete the old copy** (or 🗑 next to it in **New Server → Removed
servers** with that machine chosen under **Where**). Until then you can restore it there like any
removed server. If anything fails along the way, the new copy is removed and the server starts
again where it was. While a move runs, the server can't be started on the old machine.

**Copy…** works the same way without stopping the server (its world is saved first, as backups
do) and without removing anything. Copying to the same machine always gives the copy a new name;
you choose whether to start it. Schedules and Phantom stay with the original.

Both machines need RedstonePanel 1.16 or later. Older backups and the player history stay on the
old machine.

**Pre-copy while it runs** *(1.16.13)*: for a big world, tick this in the **Move** window. The
folder is copied to the other machine while players stay on, then the server stops and only the
files that changed since are copied and checked. The server is down for seconds instead of
minutes, and the window says how long it was down. Both machines need RedstonePanel 1.16.13 or
later for this.

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/cluster-precopy.png"><img src="images/cluster-precopy.png" alt="Moving a server with a pre-copy"></picture>

**Templates between machines:** in the template manager, **Copy to node** sends one of the
master's templates to a node. Templates a node made on its own panel are listed under its name,
with **Copy to this panel**.

### Accounts in a cluster

Each panel keeps **its own accounts**. Joining a cluster doesn't copy, share or sync them, on
Linux or Windows:

- **You manage the cluster from the master, with the master's accounts.** Sign in there with a
  panel account, or with a system account the master accepts (on Linux, in **sudo** or
  **redstonepanel**; on Windows, in **Administrators** or **RedstonePanel**). A Windows account
  can't sign in to a Linux master, and a Linux account can't sign in to a Windows master: only
  the machine an account belongs to can check its password.
- **Admins on the master manage the whole cluster:** every server on every node, moves and
  copies, backup copies, templates and the cluster's settings. They need no account on the
  nodes; a node trusts its master through the cluster certificate.
- **User accounts on the master** get only what you tick for them under **Users → Servers**,
  and that includes node servers (the master keeps those ticks too; see
  [What a helper may do](#what-a-helper-may-do)). Moving servers and choosing backup copies stay
  with admins.
- **Every change is logged with the master user's name**, on the master and on the node, e.g.
  `[CLUSTER] alex@Main: start minecraft-java on Garage`.
- **A node's own accounts keep working on the node.** Signing in at a node's own address shows
  only that machine, as a standalone panel would. The master can never change a node's users,
  settings or HTTPS, or erase a server's files.
- **So:** give each person one account, on the master. Keep a working admin on every node too
  (the one made at install is enough), for when the master is down.

**Certificates:** the master renews the nodes' certificates, and its own, 30 days before they
expire. A master on the internet can serve its own certificate (certbot, or win-acme on Windows)
to browsers: **Settings →
Connection → HTTPS with my own certificate**. A node with its own public certificate can join too;
under **Settings → Cluster** it shows as checked with "the system's trust store".

**Settings → Cluster on the master** lists every node with its address, version and certificate,
how the master checks it (the cluster CA or the system's trust store), **Renew** and **Remove**,
plus join codes, the cluster backup and **Nightly backups**.

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/panel-settings.png"><img src="images/panel-settings.png" alt="Cluster settings on the master: the node list"></picture>

**Nightly backups** write an encrypted cluster backup after 3:00 every night to
`/var/lib/redstonepanel/cluster/backups/` (on Windows, `data\cluster\backups` in the panel's
folder) and keep the last 7. Turning them on asks for a
passphrase, which is stored on the master (readable only by root) so the backup can run
unattended; turning them off forgets it. Download one now and then and keep it off the master:
it's what lets `sudo redstonepanel cluster promote FILE` make another panel the master (Linux or
Windows: a backup made on one opens on the other).
Below them, **Connection** lets a master choose between the cluster's HTTPS and its own
certificate.

<picture><source media="(prefers-color-scheme: light)" srcset="images/light/panel-settings-1.png"><img src="images/panel-settings-1.png" alt="Nightly backups and the master&#x27;s HTTPS choices"></picture>

Each node keeps its own dashboard, and its game servers keep running if the master is down.
Download an encrypted **cluster backup** after adding or removing nodes, or turn on **Nightly
backups**, so another panel can take over if the master is lost. Details are in the
[README](../../README.md#clusters-several-servers-from-one-panel).

## 19. Command line

Everything below runs as root (`sudo`). `man redstonepanel` has the details.

| Command | What it does |
| --- | --- |
| `redstonepanel status` | Address, HTTPS and sign-in mode, panel users. |
| `redstonepanel add-user NAME [--role admin\|user]` | Create a panel account (asks for the password). |
| `redstonepanel set-name "NAME"` | Set the panel name (`""` = host name). |
| `redstonepanel reset-security` | Back to plain HTTP on 8080 with sign-in required. |
| `redstonepanel reset-2fa NAME` | Turn off two-factor sign-in for an account that lost its phone and recovery codes. |
| `redstonepanel terminal on\|off` | Turn the [terminal](#terminal) on or off; off closes open terminals within a minute. |
| `redstonepanel removed` | List servers uninstalled with Keep World. |
| `redstonepanel restore NAME` | Restore a removed server. |
| `redstonepanel backup NAME` | Back up a server now. |
| `redstonepanel restart-server NAME` | Warn players, then restart a server (what its restart timer runs). |
| `redstonepanel check-updates` | Look for newer server versions and update the Bedrock servers set to update automatically (what the update timer runs every 6 hours). |
| `redstonepanel notify-test` | Send a test notification. |
| `redstonepanel uninstall` | [Remove the panel safely](#removing-redstonepanel): choose what happens to each server first. |
| `redstonepanel cluster create "NAME"` | Make this panel a cluster master. |
| `redstonepanel cluster join-code` | Make a join code (master). |
| `redstonepanel join 'CODE'` | Join a cluster. |
| `redstonepanel leave` | Leave the cluster. |
| `redstonepanel cluster backup FILE` / `cluster promote FILE` | Back up the cluster / make this panel the master from a backup. |
| `redstonepanel cluster nightly on\|off` | Nightly encrypted cluster backups on the master (keeps 7). |

Game servers are ordinary systemd services, so the usual tools work too:

```bash
systemctl status minecraft-java
journalctl -u minecraft-java -f
systemctl list-timers 'redstonepanel-backup@*'
```

## 20. Troubleshooting

**I can't reach the panel after changing a setting.** Run this on the machine:

```bash
sudo redstonepanel reset-security
```

It switches the panel back to plain HTTP on port 8080 with sign-in required. If HTTPS is
turned on but its certificate can't be used, the panel starts on HTTP by itself and warns
admins in the sidebar.

**I lost the phone with my authenticator app.** Sign in with one of your recovery codes
instead of the app's code, then set two-factor up again under **🔑 Password**. No codes left?
Another admin can **Reset two-factor** under **👥 Users**, or run this on the machine:

```bash
sudo redstonepanel reset-2fa NAME
```

**Players can't connect.** Check:

- the server is green in the sidebar;
- players use the port shown on **Overview**;
- the port is open in the firewall (`sudo ufw status`): TCP for Java, UDP for Bedrock, and for a
  NetherNet Bedrock server TCP on its port plus UDP on `server-udp-ports` and 7551. The
  dashboard's **Needs attention** lists any that are missing, with the command to open them;
- if players connect from the internet, your router forwards that port;
- on Windows with WSL, mirrored networking is on and the ports are allowed in the Hyper-V and
  Windows firewalls (see [On Windows with WSL](#on-windows-with-wsl)).

**A server crashed or won't start.** Open it: the **Last crash** card on its **Overview** says
why, when the panel recognises the cause, and offers the fix (see [Last crash](#last-crash)).
If it found no known cause, the highlighted lines in its log are the place to look.

**Live Chat says RCON isn't answering.** The server is probably still starting; wait a moment.
Otherwise **Restart** it so it picks up its RCON settings.

**A backup says it was copied live.** That Java server has no RCON yet. Open its **Live Chat**
tab once; the panel sets RCON up and later backups are consistent.

**Where is everything?**

| What | Where |
| --- | --- |
| Game servers | `/opt/minecraft-*` |
| Backups | `/var/backups/redstonepanel/<server>/` |
| Notification webhook (root only) | `/var/lib/redstonepanel/notifications.json` |
| Panel settings, accounts, templates | `/var/lib/redstonepanel` |
| Activity log | `/var/log/redstonepanel/panel.log` |
| Panel service | `systemctl status redstonepanel` |

## Screenshot gallery

Every screenshot in this manual, in the theme you pick:

<details>
<summary>🌙 <b>Show every screenshot in the dark theme</b></summary>

<table>
<tr><td><img src="images/sign-in.png" width="280" alt="Sign-in page (dark theme)"><br>Sign-in page</td><td><img src="images/dashboard.png" width="280" alt="The dashboard on a cluster master (dark theme)"><br>The dashboard on a cluster master</td><td><img src="images/overview.png" width="280" alt="Dashboard with a Java server selected (dark theme)"><br>Dashboard with a Java server selected</td></tr>
<tr><td><img src="images/new-server.png" width="280" alt="New Server dialog (dark theme)"><br>New Server dialog</td><td><img src="images/overview-bedrock.png" width="280" alt="Bedrock server overview (dark theme)"><br>Bedrock server overview</td><td><img src="images/live-chat.png" width="280" alt="Live Chat (dark theme)"><br>Live Chat</td></tr>
<tr><td><img src="images/configuration.png" width="280" alt="Configuration, visual editor (dark theme)"><br>Configuration, visual editor</td><td><img src="images/configuration-filter.png" width="280" alt="Filtering properties (dark theme)"><br>Filtering properties</td><td><img src="images/configuration-raw.png" width="280" alt="Raw server.properties (dark theme)"><br>Raw server.properties</td></tr>
<tr><td><img src="images/player-roles.png" width="280" alt="Player roles (dark theme)"><br>Player roles</td><td><img src="images/maintenance.png" width="280" alt="Maintenance tab (dark theme)"><br>Maintenance tab</td><td><img src="images/backups.png" width="280" alt="Backups tab, Java server (dark theme)"><br>Backups tab, Java server</td></tr>
<tr><td><img src="images/backups-bedrock.png" width="280" alt="Backups tab, Bedrock server (dark theme)"><br>Backups tab, Bedrock server</td><td><img src="images/scheduled-restarts.png" width="280" alt="Scheduled restarts (dark theme)"><br>Scheduled restarts</td><td><img src="images/world.png" width="280" alt="World & Maps (dark theme)"><br>World & Maps</td></tr>
<tr><td><img src="images/files.png" width="280" alt="Files tab (dark theme)"><br>Files tab</td><td><img src="images/file-editor.png" width="280" alt="Editing a plugin's config.yml (dark theme)"><br>Editing a plugin's config.yml</td><td><img src="images/mods.png" width="280" alt="Mods tab on a Fabric server (dark theme)"><br>Mods tab on a Fabric server</td></tr>
<tr><td><img src="images/plugins.png" width="280" alt="Plugins tab on a Paper server (dark theme)"><br>Plugins tab on a Paper server</td><td><img src="images/addons-bedrock.png" width="280" alt="Add-ons tab on a Bedrock server (dark theme)"><br>Add-ons tab on a Bedrock server</td><td><img src="images/mods-details.png" width="280" alt="Details of a mod before installing it (dark theme)"><br>Details of a mod before installing it</td></tr>
<tr><td><img src="images/mods-catalog.png" width="280" alt="The mod catalog on a Fabric server (dark theme)"><br>The mod catalog on a Fabric server</td><td><img src="images/mods-browse.png" width="280" alt="Finding mods on Modrinth (dark theme)"><br>Finding mods on Modrinth</td><td><img src="images/plugins-hangar.png" width="280" alt="Plugins from Hangar on a Paper server (dark theme)"><br>Plugins from Hangar on a Paper server</td></tr>
<tr><td><img src="images/console.png" width="280" alt="Console (dark theme)"><br>Console</td><td><img src="images/templates.png" width="280" alt="Template manager (dark theme)"><br>Template manager</td><td><img src="images/users.png" width="280" alt="Panel users (dark theme)"><br>Panel users</td></tr>
<tr><td><img src="images/panel-settings.png" width="280" alt="Panel Settings: name, notifications and cluster (dark theme)"><br>Panel Settings: name, notifications and cluster</td><td><img src="images/panel-settings-1.png" width="280" alt="Panel Settings on a cluster master: connection and sign-in (dark theme)"><br>Panel Settings on a cluster master: connection and sign-in</td><td><img src="images/notifications.png" width="280" alt="Notification settings (dark theme)"><br>Notification settings</td></tr>
<tr><td><img src="images/cluster.png" width="280" alt="The Cluster page (dark theme)"><br>The Cluster page</td><td><img src="images/node-server.png" width="280" alt="A node's Fabric server opened from the master (dark theme)"><br>A node's Fabric server opened from the master</td><td><img src="images/cluster-move.png" width="280" alt="Moving a server to another cluster member (dark theme)"><br>Moving a server to another cluster member</td></tr>
<tr><td><img src="images/cluster-moved.png" width="280" alt="A finished move, with the old copy still kept (dark theme)"><br>A finished move, with the old copy still kept</td><td><img src="images/port-forwarding.png" width="280" alt="The Port forwarding page (dark theme)"><br>The Port forwarding page</td><td><img src="images/overview-crash.png" width="280" alt="The Last crash card (dark theme)"><br>The Last crash card</td></tr>
</table>

</details>
<details>
<summary>☀️ <b>Show every screenshot in the light theme</b></summary>

<table>
<tr><td><img src="images/light/sign-in.png" width="280" alt="Sign-in page (light theme)"><br>Sign-in page</td><td><img src="images/light/dashboard.png" width="280" alt="The dashboard on a cluster master (light theme)"><br>The dashboard on a cluster master</td><td><img src="images/light/overview.png" width="280" alt="Dashboard with a Java server selected (light theme)"><br>Dashboard with a Java server selected</td></tr>
<tr><td><img src="images/light/new-server.png" width="280" alt="New Server dialog (light theme)"><br>New Server dialog</td><td><img src="images/light/overview-bedrock.png" width="280" alt="Bedrock server overview (light theme)"><br>Bedrock server overview</td><td><img src="images/light/live-chat.png" width="280" alt="Live Chat (light theme)"><br>Live Chat</td></tr>
<tr><td><img src="images/light/configuration.png" width="280" alt="Configuration, visual editor (light theme)"><br>Configuration, visual editor</td><td><img src="images/light/configuration-filter.png" width="280" alt="Filtering properties (light theme)"><br>Filtering properties</td><td><img src="images/light/configuration-raw.png" width="280" alt="Raw server.properties (light theme)"><br>Raw server.properties</td></tr>
<tr><td><img src="images/light/player-roles.png" width="280" alt="Player roles (light theme)"><br>Player roles</td><td><img src="images/light/maintenance.png" width="280" alt="Maintenance tab (light theme)"><br>Maintenance tab</td><td><img src="images/light/backups.png" width="280" alt="Backups tab, Java server (light theme)"><br>Backups tab, Java server</td></tr>
<tr><td><img src="images/light/backups-bedrock.png" width="280" alt="Backups tab, Bedrock server (light theme)"><br>Backups tab, Bedrock server</td><td><img src="images/light/scheduled-restarts.png" width="280" alt="Scheduled restarts (light theme)"><br>Scheduled restarts</td><td><img src="images/light/world.png" width="280" alt="World & Maps (light theme)"><br>World & Maps</td></tr>
<tr><td><img src="images/light/files.png" width="280" alt="Files tab (light theme)"><br>Files tab</td><td><img src="images/light/file-editor.png" width="280" alt="Editing a plugin's config.yml (light theme)"><br>Editing a plugin's config.yml</td><td><img src="images/light/mods.png" width="280" alt="Mods tab on a Fabric server (light theme)"><br>Mods tab on a Fabric server</td></tr>
<tr><td><img src="images/light/plugins.png" width="280" alt="Plugins tab on a Paper server (light theme)"><br>Plugins tab on a Paper server</td><td><img src="images/light/addons-bedrock.png" width="280" alt="Add-ons tab on a Bedrock server (light theme)"><br>Add-ons tab on a Bedrock server</td><td><img src="images/light/mods-details.png" width="280" alt="Details of a mod before installing it (light theme)"><br>Details of a mod before installing it</td></tr>
<tr><td><img src="images/light/mods-catalog.png" width="280" alt="The mod catalog on a Fabric server (light theme)"><br>The mod catalog on a Fabric server</td><td><img src="images/light/mods-browse.png" width="280" alt="Finding mods on Modrinth (light theme)"><br>Finding mods on Modrinth</td><td><img src="images/light/plugins-hangar.png" width="280" alt="Plugins from Hangar on a Paper server (light theme)"><br>Plugins from Hangar on a Paper server</td></tr>
<tr><td><img src="images/light/console.png" width="280" alt="Console (light theme)"><br>Console</td><td><img src="images/light/templates.png" width="280" alt="Template manager (light theme)"><br>Template manager</td><td><img src="images/light/users.png" width="280" alt="Panel users (light theme)"><br>Panel users</td></tr>
<tr><td><img src="images/light/panel-settings.png" width="280" alt="Panel Settings: name, notifications and cluster (light theme)"><br>Panel Settings: name, notifications and cluster</td><td><img src="images/light/panel-settings-1.png" width="280" alt="Panel Settings on a cluster master: connection and sign-in (light theme)"><br>Panel Settings on a cluster master: connection and sign-in</td><td><img src="images/light/notifications.png" width="280" alt="Notification settings (light theme)"><br>Notification settings</td></tr>
<tr><td><img src="images/light/cluster.png" width="280" alt="The Cluster page (light theme)"><br>The Cluster page</td><td><img src="images/light/node-server.png" width="280" alt="A node's Fabric server opened from the master (light theme)"><br>A node's Fabric server opened from the master</td><td><img src="images/light/cluster-move.png" width="280" alt="Moving a server to another cluster member (light theme)"><br>Moving a server to another cluster member</td></tr>
<tr><td><img src="images/light/cluster-moved.png" width="280" alt="A finished move, with the old copy still kept (light theme)"><br>A finished move, with the old copy still kept</td><td><img src="images/light/port-forwarding.png" width="280" alt="The Port forwarding page (light theme)"><br>The Port forwarding page</td><td><img src="images/light/overview-crash.png" width="280" alt="The Last crash card (light theme)"><br>The Last crash card</td></tr>
</table>

</details>
