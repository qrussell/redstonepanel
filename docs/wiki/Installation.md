# Installing RedstonePanel

RedstonePanel installs in about a minute on a fresh Ubuntu or Debian server or a Raspberry Pi,
and updates with the rest of the system through `apt`. On a Windows PC, see
[On Windows (preview)](#on-windows-preview).

## What you need

- **One of these systems**, on a machine you have `sudo` on: a home server, a spare PC, a
  Raspberry Pi, a VPS or a virtual machine.

  | System | x86-64 (Intel, AMD) | ARM64 |
  | --- | --- | --- |
  | Ubuntu 26.04 LTS (best for a new machine), 24.04 LTS or 26.10 | ✅ | ✅ |
  | Linux Mint 22 or 23 (built on Ubuntu 24.04 and 26.04) | ✅ | |
  | Debian 13 (trixie) | ✅ | ✅ |
  | Raspberry Pi OS 64-bit, on a Raspberry Pi 4 or 5 | | ✅ |

  Ubuntu 25.04 and 25.10 still install but no longer get updates from Ubuntu. Debian 12 (and
  the Raspberry Pi OS built on it), Linux Mint 21 and older, LMDE, 32-bit systems and other
  distributions aren't supported: the installer says so and stops. On Linux Mint 21, upgrade to
  Mint 22 first; its Ubuntu 22.04 base has too old a Python.
- **Bedrock servers need x86-64.** Mojang only makes Bedrock Dedicated Server for 64-bit Intel
  and AMD machines, so on ARM64 (a Raspberry Pi, an ARM VPS) the panel runs Java servers only.
  Bedrock players can join a Java server through Geyser, from the add-on catalog.
- **Memory:** the panel itself uses well under 100 MB. Plan for your game servers: each Java
  server is set up with a 2 GB heap (1 GB on a machine with 3 GB or less), and a Bedrock server
  usually needs 1–2 GB.
- **Ports:** 8080 for the panel (8443 once HTTPS is on), 25565+ (TCP) for Java servers and
  19132+ (TCP and UDP) plus UDP 7551 for Bedrock servers.
- Nothing else. Java is installed later, only when a Java server needs it, and only the version
  it needs.

## Option 1: one command (easiest)

```bash
curl -fsSL https://raw.githubusercontent.com/qrussell/redstonepanel/main/install.sh | sudo sh
```

[`install.sh`](../../install.sh) checks the system and the processor, adds the signed **qrussell-repo**
repository (refusing its key unless the fingerprint is
`9994A9EB39EB3C101C949FB2012BA14E0FE3A355`), and runs `apt install redstonepanel`. Read it
first if you like: it's about 70 lines.

## On a Raspberry Pi

A **Raspberry Pi 4 or 5** with 4 GB or more runs a Java server for a family or a few friends.

1. With **Raspberry Pi Imager**, write **Raspberry Pi OS Lite (64-bit)** to an SSD (USB 3, or
   NVMe on a Pi 5) or a microSD card, and turn on SSH in its settings. An SSD is faster and
   doesn't wear out from world saves and backups the way a card does.
2. Sign in over SSH and run the one-line installer above.
3. Create a **Paper** server: it uses noticeably less CPU than Vanilla.

| Pi | What fits (leave about 1 GB for the system) |
| --- | --- |
| 2 GB | Not recommended: one small server, which gets 1 GB. |
| 4 GB | One server at the default 2 GB, for about 5 to 10 players. |
| 8 GB | Two or three servers at 2 GB, or one at 4 to 5 GB. |
| 16 GB | Several servers; the CPU is the limit. |

To give a server more memory, use **Maintenance → Memory and limits** (1.17); a
**Hard memory limit** of **Automatic** keeps one server from taking the whole Pi. A Pi 5 needs its active cooler to run all day at full speed.
Raspberry Pi OS packages Java 21 and 25, which run Minecraft 1.17 and newer; for older versions
the panel offers Java 21 and says that it may not start them.

## On Windows (preview)

From 1.22 there is a Windows installer, and from 1.23 it does everything one computer needs:
Java servers of every type (Vanilla, Paper, Purpur, Fabric, Quilt, Forge, NeoForge, modpacks)
and Bedrock servers with automatic updates and Phantom, backups and off-site copies (SSH
storage and S3), schedules, the console, the file manager, add-ons, the firewall, memory and
processor limits, crash explanations, webhooks, two-factor sign-in and **Update now**. It stays
a **preview** until clusters and SFTP arrive in 1.24. The terminal is Linux-only by design;
on Windows, open a command prompt on the PC.

- **Windows 11, or Windows Server 2022 or 2025**, 64-bit Intel or AMD. Windows 10 installs too
  but isn't tested, and no longer gets updates from Microsoft.
- An administrator account for the install. Nothing else: Python comes inside the installer,
  and Java (Eclipse Temurin) is downloaded when a server needs it.

1. Download `RedstonePanel-x.y.z-x64.msi` from the
   [latest release](https://github.com/qrussell/redstonepanel/releases/latest) and open it.
   The preview isn't signed yet, so SmartScreen says **Windows protected your PC**: choose
   **More info**, then **Run anyway**. The release lists the file's SHA-256 if you want to
   check it first.
2. The installer asks one question: **where servers, backups and settings are kept**. The
   default is `C:\ProgramData\RedstonePanel`; pick another drive if that is where the room is.
   The folder's name can't have a space in it.
3. Open **http://localhost:8080** on that computer. The user name is **admin** and the password
   is in `first-sign-in.txt` in the folder from step 2. Change the password under **Users**,
   then delete the file. A Windows account in **Administrators** can also sign in, as an admin;
   to let other Windows accounts in, add them to a local group named **RedstonePanel**.

What you get is ordinary Windows: the panel is the service **RedstonePanel**, each game server
is its own service (`minecraft-<name>`) running as its own account that can reach only its own
folder, schedules are tasks under **RedstonePanel** in Task Scheduler, and ports are rules in
the **RedstonePanel** group in Windows Defender Firewall. Game servers and schedules keep
running while the panel is stopped, and start with Windows.

- **Update:** **Update now** on the dashboard (from 1.23), or run the newer installer. Game
  servers stop for about a minute while the files are replaced and start again afterwards.
  Worlds, backups and settings are kept. Coming from 1.22, run the 1.23 installer once by hand.
- **Off-site backups to SSH storage** need Windows' OpenSSH Client (Settings → System →
  Optional features), which most Windows 11 PCs already have.
- **Remove:** start with **Panel Settings → Remove RedstonePanel** (or `redstonepanel uninstall`
  as administrator) to back up and choose what happens to each server, then **Settings → Apps →
  Installed apps → RedstonePanel → Uninstall**. The services, tasks and the panel's firewall rule
  go; the folder from step 2 stays, with your worlds and backups in it. Delete it yourself if you
  don't want them.
- **Without the questions:** `msiexec /i RedstonePanel-x.y.z-x64.msi /qn DATAFOLDER=D:\RedstonePanel`
- **From a prompt** (as administrator): `redstonepanel status`, `redstonepanel add-user NAME`,
  `redstonepanel reset-2fa NAME`, as on Linux.

## Option 2: the apt repository, by hand

```bash
sudo apt install -y curl ca-certificates
curl -fsSL https://qrussell.github.io/qrussell-repo/deb/qrussell-repo.gpg | sudo tee /usr/share/keyrings/qrussell-repo.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/qrussell-repo.gpg] https://qrussell.github.io/qrussell-repo/deb ./" | sudo tee /etc/apt/sources.list.d/qrussell.list
sudo apt update
sudo apt install redstonepanel
```

To check the key before trusting it, `gpg --show-keys /usr/share/keyrings/qrussell-repo.gpg`
should show the fingerprint `9994A9EB39EB3C101C949FB2012BA14E0FE3A355`.

## Option 3: a .deb file

Download `redstonepanel_X_all.deb` from the
[latest release](https://github.com/qrussell/redstonepanel/releases/latest), then:

```bash
sudo apt install ./redstonepanel_X_all.deb
```

You then install each new version the same way.

## First steps

1. Open **http://&lt;server&gt;:8080/** in a browser.
2. Sign in with your Linux username and password. Members of the `sudo` group are panel
   admins. To add someone without giving them `sudo`, either
   `sudo usermod -aG redstonepanel alice` (a Linux account) or
   `sudo redstonepanel add-user alice --role user` (a panel-only account).
3. Click **➕ New Server**, pick Java or Bedrock, and **Deploy Fresh Server**. The first Java
   server asks to install the Java it needs.
4. Set up backups on the server's **Backups** tab.
5. Optional but recommended:
   - install `ufw` (`sudo apt install ufw`) so the panel opens game ports for you;
   - turn on HTTPS under **🛡️ Settings → Connection**;
   - add a Discord webhook under **🛡️ Settings → Notifications**.

Already running a Minecraft server on this machine? **➕ New Server** lists it under **Found on
this computer**. **Manage with RedstonePanel** hands it to the panel where it is, with no files
moved, and **Undo** puts it back.

The [User Manual](../guide/README.md) covers each of these in detail.

## Updating

When a new version is out, the dashboard's **Needs attention** says so and the **Host** card
offers **Update now** (admins). Or from a terminal:

```bash
sudo apt update && sudo apt upgrade
```

New versions reach the repository within a few hours of their release. Game servers keep
running through an update. With a `.deb`, install the newer file the same way as the first.

## Removing

Don't just remove the package while game servers are running or start at boot: they would stay
on this machine with nothing to manage them, holding their ports, with no backups, restarts or
updates. Remove the panel safely with either:

```bash
sudo redstonepanel uninstall
```

or **Panel Settings → Remove RedstonePanel** in the browser (also linked from every server's
**Maintenance** tab). Both back up your servers, ask what happens to each one (**stop it and keep
its files**, the recommended choice, **keep it running without the panel**, or **delete it** after
a backup), then remove the package. `--purge` (or the tick box) also deletes the panel's accounts
and settings in `/var/lib/redstonepanel`. Worlds and backups in `/var/backups/redstonepanel` are
never deleted unless you choose to.

A plain `sudo apt remove redstonepanel` never stops or deletes a game server either: it turns off
the panel's own timers and services and lists every server it leaves running. Servers you stopped
come back from **New server → Removed servers** after installing the panel again.

## Where things are

| What | Where |
| --- | --- |
| Game servers | `/opt/minecraft-*`, or the folder you chose (below) |
| Backups | `/var/backups/redstonepanel/<server>/` |
| Panel settings, accounts, templates | `/var/lib/redstonepanel` |
| Activity log | `/var/log/redstonepanel/panel.log` |
| Panel service | `systemctl status redstonepanel` |

### Keeping servers on another disk

If `/opt` is on a small system disk and your room is somewhere else (a second drive, a bigger
partition), open **Panel Settings → Servers folder**, type the full path of a folder there,
for example `/srv/minecraft` or `/mnt/disk2/minecraft`, and choose **Save folder**. The panel
makes the folder if it isn't there and shows the free space on that disk.

- **New servers go there**: ones you create, import, restore from off-site storage, install
  from a modpack or move in from another cluster member.
- **Servers you already have stay where they are** and keep working. The panel remembers every
  folder it has used, so their names and ports still count and removed servers are still found.
- Leave the box empty to go back to `/opt`.

The panel refuses folders it can't safely use, and says why: system folders (`/etc`, `/usr`,
`/var/lib` and the like), a whole disk's top folder such as `/mnt` or `/home` (give a folder of
its own inside it), names with spaces, a disk mounted read-only or `noexec`, and a folder the
`minecraft` account can't get into because a folder above it is closed (`sudo chmod o+x` on
that folder fixes it). The path also can't contain the words "java" or "bedrock".

In a cluster, each machine has its own servers folder, set in that machine's own panel.

**On Windows (preview)** the same setting takes a folder with its drive letter, for example
`D:\Minecraft\servers`, so servers can live on a second disk while the panel stays on `C:`.
A list next to the box shows this computer's drives with their free space; choosing one fills in
`\Minecraft\servers` on that drive, so the path is the same whatever the drive letter (you can
still change it). Leave it empty for the usual place, `C:\ProgramData\RedstonePanel\servers`. The panel refuses
a whole drive (`D:`), paths with spaces, network shares, removable drives, drives not formatted
as NTFS, and folders inside `C:\Windows`, `C:\Program Files` or `C:\Users`. A folder the panel
makes is open to administrators only; each server's own account is given just its own folder
inside it.

## Reaching the panel from outside your network

Don't forward port 8080 to the internet. Use a VPN (WireGuard, Tailscale), or put a reverse
proxy with a real certificate in front of the panel, as the
[README explains](../../README.md#exposing-the-panel-to-the-internet).

## Locked out?

```bash
sudo redstonepanel reset-security
```

switches the panel back to plain HTTP on port 8080 with sign-in required.
