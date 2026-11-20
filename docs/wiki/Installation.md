# Installing RedstonePanel

RedstonePanel installs in about a minute on a fresh Ubuntu server, and updates with the rest of
the system through `apt`.

## What you need

- **Ubuntu 24.04 LTS or 25.04**, on a machine you have `sudo` on: a home server, a spare PC, a
  VPS or a virtual machine. The package is `Architecture: all`; Bedrock Dedicated Server
  itself only runs on x86-64.
- **Memory:** the panel itself uses well under 100 MB. Plan for your game servers: each Java
  server is set up with a 2 GB heap, and a Bedrock server usually needs 1–2 GB.
- **Ports:** 8080 for the panel (8443 once HTTPS is on), 25565+ (TCP) for Java servers and
  19132+ (UDP) for Bedrock servers.
- Nothing else. Java is installed later, only when a Java server needs it, and only the version
  it needs.

## Option 1: one command (easiest)

```bash
curl -fsSL https://raw.githubusercontent.com/qrussell/redstonepanel/main/install.sh | sudo sh
```

[`install.sh`](../../install.sh) checks the Ubuntu release, adds the signed **qrussell-repo**
repository (refusing its key unless the fingerprint is
`9994A9EB39EB3C101C949FB2012BA14E0FE3A355`), and runs `apt install redstonepanel`. Read it
first if you like: it's about 40 lines.

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
