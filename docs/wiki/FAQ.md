# FAQ

## Is RedstonePanel right for me?

**A good fit if you:**

- run Minecraft servers for yourself, your family, friends, a club or a small community, on
  a machine you control (a home server, a spare PC, a Raspberry Pi, a VPS);
- want **Java and Bedrock** in one place, including Paper plugins and Fabric mods;
- want backups, updates and restarts to happen by themselves, and a clear signal when
  something goes wrong;
- like being able to see what's going on underneath: every server is an ordinary systemd
  service, its files are in `/opt/minecraft-*`, and you can still use `systemctl` and
  `journalctl`;
- run more than one machine and want to manage them all from one page (clusters).

**Probably not the right tool if you:**

- want to sell game servers to customers. RedstonePanel has no billing, tenant isolation or
  resale features, and none are planned;
- need a macOS app. It runs on Ubuntu 24.04, 26.04 and 26.10, Linux Mint 22 and 23, Debian
  13 and 64-bit Raspberry Pi OS, and from 1.22 on Windows as a [preview](Installation.md#on-windows-preview) (one
  computer, Java and Bedrock, from 1.23; clusters come in 1.24);
- want every server isolated in its own container. RedstonePanel deliberately uses systemd
  instead of Docker.

## How much does it cost?

Nothing. RedstonePanel is MIT-licensed open source with no paid tier, no account to create and
no telemetry.

## How do I install it?

```bash
curl -fsSL https://raw.githubusercontent.com/qrussell/redstonepanel/main/install.sh | sudo sh
```

See [Installation](Installation.md) for the other ways and what to do next.

## Which systems does it run on?

Ubuntu 24.04 LTS, 26.04 LTS and 26.10, Linux Mint 22 and 23, Debian 13, and
Raspberry Pi OS (64-bit), on x86-64 (Intel, AMD) and ARM64 machines. Ubuntu 25.04 and 25.10
still install, but Ubuntu no longer updates them. Debian 12, Linux Mint 21 and older, LMDE,
32-bit systems and other distributions aren't supported. See
[Installation](Installation.md#what-you-need).

## Can I run it on a Raspberry Pi?

Yes, a **Raspberry Pi 4 or 5** with the **64-bit** Raspberry Pi OS (Lite is enough), using the
same one-line installer. It runs **Java servers only**: Mojang makes Bedrock Dedicated Server
for x86-64 machines only, so the panel hides Bedrock on a Pi. Bedrock players can still join a
Java server through Geyser (in the add-on catalog), and in a cluster a Pi can sit next to an
x86-64 machine that runs the Bedrock servers.

A 4 GB Pi runs one server well (about 5 to 10 players, Paper recommended); an 8 GB Pi two or
three. Use an SSD rather than a microSD card if you can, and the active cooler on a Pi 5.
32-bit Raspberry Pi OS and the older Pi OS based on Debian 12 aren't supported. More in
[Installation](Installation.md#on-a-raspberry-pi).

## Can I run it on Windows with WSL?

Yes, and until the [Windows version](Installation.md#on-windows-preview) is out of preview,
WSL is how to get Bedrock servers, modpacks and clusters on a Windows PC. Install **Ubuntu 26.04** or **24.04** from WSL (`wsl --install -d Ubuntu-24.04`), then run the install
command above inside it. Ubuntu under WSL starts systemd by default, which the panel and its
game servers need; if yours doesn't, add `systemd=true` under `[boot]` in `/etc/wsl.conf` and
run `wsl --shutdown`.

The panel then opens in your Windows browser at **http://localhost:8080/**. Players on other
PCs need a few more steps, because by default WSL keeps Ubuntu behind its own private network:

1. **Turn on mirrored networking** (Windows 11 22H2 or newer). Put this in
   `%UserProfile%\.wslconfig`, then run `wsl --shutdown`:

   ```ini
   [wsl2]
   networkingMode=mirrored
   ```

   Ubuntu then shares your PC's network address, and players connect to your **Windows PC's
   IP address** (from `ipconfig`).

2. **Allow the game ports through the Hyper-V firewall** that sits in front of WSL. In an
   admin PowerShell, for example for the first Bedrock server:

   ```powershell
   New-NetFirewallHyperVRule -Name "MinecraftTCP" -DisplayName "Minecraft TCP" -Direction Inbound -VMCreatorId '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -Protocol TCP -LocalPorts 19132
   New-NetFirewallHyperVRule -Name "MinecraftUDP" -DisplayName "Minecraft UDP" -Direction Inbound -VMCreatorId '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -Protocol UDP -LocalPorts 19140,7551
   ```

3. **Allow the same ports in Windows Defender Firewall**:

   ```powershell
   New-NetFirewallRule -DisplayName "Minecraft TCP" -Direction Inbound -Protocol TCP -LocalPort 19132 -Action Allow
   New-NetFirewallRule -DisplayName "Minecraft UDP" -Direction Inbound -Protocol UDP -LocalPort 19140,7551 -Action Allow
   ```

   Also make sure Windows treats your network as **Private**.

Which ports? A **Java** server needs TCP on its port (25565 for the first one). Current
**Bedrock** servers use the NetherNet transport: TCP on their `server-port` (19132 for the
first), UDP on their `server-udp-ports` (shown in **Configuration**) and UDP 7551 for LAN
discovery. Opening only UDP 19132, as older Bedrock guides say, isn't enough. Each new server
gets its own ports, so add rules for those too. To open the panel from other PCs as well, allow
TCP 8080 the same way.

Two more things to know:

- **WSL can stop Ubuntu when nothing on Windows is using it**, for example a while after you
  close the last Ubuntu window, and it only starts after you sign in to Windows. Your game
  servers stop with it. For a server that's always up, keep an Ubuntu window open, or use a
  real Ubuntu machine or VM.
- For players outside your home, forward the same ports on your router to your **Windows PC's**
  IP address.

## Which Minecraft versions and server types does it support?

- **Java Edition:** Vanilla (Mojang), Paper, Purpur, Fabric, Quilt, NeoForge and Forge, on any
  release version that type supports (Forge from 1.12.2, Quilt from 1.14.4, NeoForge from
  1.20.2), and modpacks from Modrinth or a CurseForge server pack you upload. The
  panel installs the Java each version needs (Java 8, 17, 21 or 25) and runs each server on
  its own Java, so old and new versions can run side by side.
- A Java server can **change type** later (Vanilla to Paper or Fabric, and every other way
  round) and keep its world: **Maintenance → Server type** shows what will happen first,
  always backs up, and puts the old type back if the new one doesn't start.
- **Bedrock Edition:** the official Bedrock Dedicated Server, always the newest release, with
  `.mcpack` and `.mcaddon` add-ons and Phantom LAN proxies.
- Bedrock players can join Java servers through **Geyser** and **Floodgate**, one click away in
  the mod and plugin catalog's **Bedrock players** bundle.

## How many servers can I run?

As many as the machine has memory and CPU for. Each server gets its own port automatically
(Java from 25565, Bedrock from 19132). With a cluster, you can spread servers over several
machines and still manage them from one page.

## Are my worlds safe?

- Scheduled backups run as systemd timers, even while the panel is stopped, and copy running
  servers consistently (saving is paused while files are copied).
- Restoring a backup first saves the current world as a **Before restore** backup, so a restore
  can be undone.
- Mod and plugin updates can back the world up first, and the last update can be undone.
- Uninstalling a server with **Keep World** keeps its files, so it can be restored later.
- Backups are stored in `/var/backups/redstonepanel/` on the same machine. In a cluster, the
  master can keep a copy of each server's backups on another member (**Backups** tab → **Also
  keep a copy on**). Without a cluster, copy them elsewhere for protection against losing the
  disk; off-machine backups for standalone panels are on the roadmap.

## What happens if the panel stops or I uninstall it?

If the panel stops, your game servers keep running: they're systemd services, not child
processes of the panel, and backup and restart schedules keep running too.

To uninstall it, use `sudo redstonepanel uninstall` or **Panel Settings → Remove RedstonePanel**,
not a plain `apt remove`. They ask what happens to each server first: stop it and keep its files
(recommended), keep it running without the panel, or delete it after a backup. A server left
running after the panel is gone keeps starting at boot and holding its ports with nothing to
back it up or update it, which is easy to forget. Worlds and backups are never deleted unless
you choose to. See [Installation → Removing](Installation#removing).

## Is it secure?

The panel runs as root, because it creates systemd units, installs packages and opens firewall
ports. So:

- sign-in uses Linux accounts (PAM) or panel accounts, with **admin** and **user** roles;
  admins are effectively root, so treat admin accounts like `sudo`;
- HTTPS can be self-signed, your own certificate (certbot), or a cluster's CA;
- the file manager and SFTP can't leave a server's folder and never follow symlinks; SFTP is
  off until you turn it on, and gives no shell;
- user accounts can do only what an admin allows, on the servers the admin picks;
- the packages come from a signed apt repository;
- keep it off the open internet unless it's behind a VPN or a reverse proxy, as the
  [README describes](../../README.md#exposing-the-panel-to-the-internet).

Found a problem? Please report it privately through a
[security advisory](https://github.com/qrussell/redstonepanel/security/advisories/new).

## Can friends help run the servers without full access?

Yes: give them a panel account with the **user** role, then choose under **👥 Users → Servers**
what they may do on each server: view, console and chat, start/stop/restart, players, files,
mods and plugins, settings, and backups. **Moderator** and **Co-owner** fill the boxes in for
you. They see only their servers, and can never manage accounts or Panel Settings, install
Java, or create, uninstall or restore a server.

## Can I use FileZilla or WinSCP?

Yes. An admin turns on SFTP under **Panel Settings → SFTP** (port 2022). Each server's
**Files** tab then shows the host, port and username (`account.server`); the password is your
panel password, or use an SSH key added under **🔑 Password → SSH keys** (required for accounts
with two-factor sign-in). A sign-in reaches one server's folder only.

## Can I manage servers on several machines?

Yes, with a **cluster** (1.5 and newer, fully managed from the master since 1.15). Make one panel
the master, add the others with a join code, and the master's sidebar lists every machine's
servers with all their tabs. See the [User Manual](../guide/README.md#18-clusters).

## How do friends outside my home join?

Forward the game ports on your internet router to the machine the server runs on. **🔀 Port
forwarding** (1.16.14, admins) lists each one, with the machine's LAN address, steps for
pfSense and other routers, and a warning if your provider uses carrier-grade NAT, which makes
forwarding impossible. See the [User Manual](../guide/README.md#port-forwarding).

## Can I import a server I already have?

Yes: **➕ New Server → Import Existing** takes a `.zip`, `.tar.gz` or `.tar` of the server
folder. The panel gives it a port and a systemd service.

## How do I get help or ask for a feature?

Open an [issue](https://github.com/qrussell/redstonepanel/issues) for bugs, or join the
[discussions](https://github.com/qrussell/redstonepanel/discussions) for questions and ideas.
The [Roadmap](Roadmap.md) shows what's planned, and feedback decides the order.
