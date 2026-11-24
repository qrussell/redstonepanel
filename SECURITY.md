# Security policy

Redstone Panel runs as **root** on the machine it manages, so security problems in it are
taken seriously. Thank you for taking the time to report one.

## Supported versions

Fixes go into the **latest release** only. Redstone Panel has one developer and ships small
releases often, so please update before reporting (`sudo redstonepanel upgrade`, or
`sudo apt update && sudo apt install redstonepanel`).

| Version | Supported |
| --- | --- |
| Latest release (see [Releases](https://github.com/qrussell/redstonepanel/releases)) | Yes |
| Anything older | No, please update |

## Reporting a vulnerability

**Please do not open a public issue, discussion or merge request for a security problem.**

Report it privately on GitHub:
[**Report a vulnerability**](https://github.com/qrussell/redstonepanel/security/advisories/new)
(the **Security** tab of the repository, then **Report a vulnerability**). Only the maintainer
can read the report.

It helps to include:

- the Redstone Panel version (shown next to the name in the panel, or `apt show redstonepanel`) and the
  system it runs on (for example Ubuntu 24.04, Debian 13, Raspberry Pi OS);
- what kind of account is needed (none, a **user**, an **admin**, a cluster node);
- the steps to reproduce it, and what happens;
- what an attacker could gain.

Please leave out real passwords, tokens, join codes and world data.

## What to expect

- A first answer within **7 days**.
- If the problem is confirmed, a fix in a new release as soon as it is ready, normally within
  **30 days**, and a published security advisory that credits you unless you would rather stay
  unnamed.
- If it is not a vulnerability, an explanation of why.

Please give the fix a chance to ship before you write about the problem in public.

## What counts

In scope: the panel itself (this repository), its `.deb` package and `install.sh`, the
cluster protocol between a master and its nodes, and the public join pages.

Things that are by design, and so are not vulnerabilities on their own:

- A panel **admin** effectively has root on the machine. That is stated in the
  [README](README.md#security-note). Getting admin rights *without* being given them is a
  vulnerability.
- The terminal (1.21.10), when the owner turns it on, gives an admin a shell as a Linux account
  whose password they type (or the Linux account they signed in with). Getting a shell as an
  account without its password, as root without root being allowed, or as a helper, is a
  vulnerability.
- Trusted-LAN access, when the owner turns it on, lets devices on the local network in without
  signing in (never as an admin).
- Problems in Minecraft itself, in server software (Paper, Fabric, Forge and so on), or in
  mods, plugins and modpacks. Please report those to their own projects.

## Where this project lives

Development happens on the maintainer's own GitLab server, and
[github.com/qrussell/redstonepanel](https://github.com/qrussell/redstonepanel) is a mirror of
it. GitHub is still the right place for security reports. The official website is
<https://redstonepanel.cielocloud.org>.
