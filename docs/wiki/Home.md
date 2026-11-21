# RedstonePanel wiki

**RedstonePanel** is a free, self-hosted web panel for Minecraft **Java** and **Bedrock**
servers on your own Ubuntu or Debian machine or Raspberry Pi. Every game server is a plain systemd service; the panel
installs and updates with `apt`. Current version: **1.16.8**.

<picture><source media="(prefers-color-scheme: light)" srcset="../guide/images/light/hero.png"><img src="../guide/images/hero.png" alt="The RedstonePanel dashboard"></picture>

## Start here

| | |
| --- | --- |
| ⬇️ **[Installation](Installation.md)** | One command, or apt by hand, or a `.deb`. First steps after installing. |
| 📖 **[User Manual](../guide/README.md)** | Every feature, step by step, with screenshots. |
| ✨ **[What's New](Whats-New.md)** | The highlights of every release, newest first. |
| 💬 **[Discord](Discord.md)** | Set up a Discord for your community: a private space per Minecraft server, the panel's alerts, chat bridges and voice. |
| ❓ **[FAQ](FAQ.md)** | Is RedstonePanel right for you? Common questions and answers. |
| 🛣️ **[Roadmap](Roadmap.md)** | What's coming next, and ideas being considered. |

## Install in one line

```bash
curl -fsSL https://raw.githubusercontent.com/qrussell/redstonepanel/main/install.sh | sudo sh
```

Then open `http://<your-server>:8080/` and sign in with a Linux account in the `sudo` group.

## What it does

- **Servers:** Java (Vanilla, Paper, Fabric, any version) and Bedrock side by side, each with
  the Java it needs. Create, import, export, update, uninstall and restore.
- **Mods and plugins:** a Modrinth and Hangar catalog with bundles, dependencies and updates
  with undo; drag-and-drop `.jar`, `.mcpack` and `.mcaddon` files.
- **Worlds kept safe:** scheduled backups of running servers, one-click restore with undo.
- **Players:** live chat and commands, player roles, whitelist editing and an access log.
- **Files:** a file manager and text editor for each server's folder.
- **Awareness:** a host dashboard, Needs attention, Discord and webhook notifications, an
  activity log, scheduled restarts that warn players.
- **Clusters:** one master panel manages the servers on all your machines, with every tab.
- **Security:** Linux or panel accounts with roles, HTTPS, optional trusted-LAN access.

## Help and feedback

- Questions and ideas: the
  [features and roadmap discussion](https://github.com/qrussell/redstonepanel/discussions).
- Bugs: [open an issue](https://github.com/qrussell/redstonepanel/issues).
- Security problems: a private
  [security advisory](https://github.com/qrussell/redstonepanel/security/advisories/new).

*These pages are kept in the repository under `docs/wiki/` and published here whenever they
change. To suggest an edit, open a merge request or issue against that folder.*
