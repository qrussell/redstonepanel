# FAQ

## Is RedstonePanel right for me?

**A good fit if you:**

- run Minecraft servers for yourself, your family, friends, a club or a small community, on
  a machine you control (a home server, a spare PC, a VPS);
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
- need a native Windows or macOS app. It runs on Ubuntu 24.04 and 25.04 (on Windows, in
  [WSL](#can-i-run-it-on-windows-with-wsl));
- need Forge or NeoForge today. They're on the roadmap's "when asked for" list;
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

## Can I run it on Windows with WSL?

Yes. Install **Ubuntu 24.04** from WSL (`wsl --install -d Ubuntu-24.04`), then run the install
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

- **Java Edition:** Vanilla (Mojang), Paper and Fabric, on any release version you choose. The
  panel installs the Java each version needs (Java 8, 17, 21 or 25) and runs each server on
  its own Java, so old and new versions can run side by side.
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

Your game servers keep running: they're systemd services, not child processes of the panel.
Backup and restart schedules keep running too. Removing the panel package doesn't touch your
servers or backups.

## Is it secure?

The panel runs as root, because it creates systemd units, installs packages and opens firewall
ports. So:

- sign-in uses Linux accounts (PAM) or panel accounts, with **admin** and **user** roles;
  admins are effectively root, so treat admin accounts like `sudo`;
- HTTPS can be self-signed, your own certificate (certbot), or a cluster's CA;
- the file manager can't leave a server's folder and never follows symlinks;
- the packages come from a signed apt repository;
- keep it off the open internet unless it's behind a VPN or a reverse proxy, as the
  [README describes](../../README.md#exposing-the-panel-to-the-internet).

Found a problem? Please report it privately through a
[security advisory](https://github.com/qrussell/redstonepanel/security/advisories/new).

## Can friends help run the servers without full access?

Yes: give them a panel account with the **user** role. Users can start, stop and configure
servers, chat, and take backups, but can't manage accounts or settings, use the file manager,
install Java, or uninstall servers. Finer per-server permissions are planned for 2.0.

## Can I manage servers on several machines?

Yes, with a **cluster** (1.5 and newer, fully managed from the master since 1.15). Make one panel
the master, add the others with a join code, and the master's sidebar lists every machine's
servers with all their tabs. See the [User Manual](../guide/README.md#18-clusters).

## Can I import a server I already have?

Yes: **➕ New Server → Import Existing** takes a `.zip`, `.tar.gz` or `.tar` of the server
folder. The panel gives it a port and a systemd service.

## How do I get help or ask for a feature?

Open an [issue](https://github.com/qrussell/redstonepanel/issues) for bugs, or join the
[discussions](https://github.com/qrussell/redstonepanel/discussions) for questions and ideas.
The [Roadmap](Roadmap.md) shows what's planned, and feedback decides the order.
