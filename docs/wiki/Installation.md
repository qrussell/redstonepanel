# Installing RedstonePanel

RedstonePanel installs in about a minute on a fresh Ubuntu or Debian server or a Raspberry Pi,
and updates with the rest of the system through `apt`.

## What you need

- **One of these systems**, on a machine you have `sudo` on: a home server, a spare PC, a
  Raspberry Pi, a VPS or a virtual machine.

  | System | x86-64 (Intel, AMD) | ARM64 |
  | --- | --- | --- |
  | Ubuntu 26.04 LTS (best for a new machine), 24.04 LTS or 26.10 | ✅ | ✅ |
  | Debian 13 (trixie) | ✅ | ✅ |
  | Raspberry Pi OS 64-bit, on a Raspberry Pi 4 or 5 | | ✅ |

  Ubuntu 25.04 and 25.10 still install but no longer get updates from Ubuntu. Debian 12 (and
  the Raspberry Pi OS built on it), 32-bit systems and other distributions aren't supported:
  the installer says so and stops.
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

```bash
sudo apt remove redstonepanel     # stops the panel, keeps its settings
sudo apt purge redstonepanel      # also deletes /var/lib/redstonepanel
```

Neither touches your game servers in `/opt/minecraft-*` or your backups in
`/var/backups/redstonepanel`. Game servers stay installed as systemd services and keep running.

## Where things are

| What | Where |
| --- | --- |
| Game servers | `/opt/minecraft-*` |
| Backups | `/var/backups/redstonepanel/<server>/` |
| Panel settings, accounts, templates | `/var/lib/redstonepanel` |
| Activity log | `/var/log/redstonepanel/panel.log` |
| Panel service | `systemctl status redstonepanel` |

## Reaching the panel from outside your network

Don't forward port 8080 to the internet. Use a VPN (WireGuard, Tailscale), or put a reverse
proxy with a real certificate in front of the panel, as the
[README explains](../../README.md#exposing-the-panel-to-the-internet).

## Locked out?

```bash
sudo redstonepanel reset-security
```

switches the panel back to plain HTTP on port 8080 with sign-in required.
