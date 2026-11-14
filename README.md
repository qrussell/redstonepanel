# RedstonePanel

A web panel for running Minecraft servers on your own Ubuntu machine. It installs Java
Edition and Bedrock Dedicated Server instances as systemd services and lets you manage
them from a browser:

- create, start, stop and update servers
- live chat and console commands (over RCON)
- a `server.properties` editor
- player roles (operator, member and so on)
- reusable server templates
- export a server as a zip backup, and import it again
- a panel activity log

The panel is a FastAPI app with a plain HTML/JavaScript front end. It ships as an
`Architecture: all` Debian package for **Ubuntu 24.04 and 25.04**.

## Install

Download `redstonepanel_X_all.deb` from the
[latest release](https://github.com/qrussell/redstonepanel/releases/latest), then:

```bash
sudo apt install ./redstonepanel_X_all.deb
```

Replace `X` with the version you downloaded. `apt` pulls in the dependencies from Ubuntu's
archive (`python3-fastapi`, `python3-uvicorn`, `python3-multipart`, PAM, `screen` and so on),
then starts the `redstonepanel` service. Open **http://&lt;server&gt;:8080/** in a browser.

Java is a *Recommends* (`openjdk-21-jre-headless`), so a normal install includes it. On a
host that will only run Bedrock servers you can leave it out:

```bash
sudo apt install --no-install-recommends ./redstonepanel_X_all.deb
```

`ufw` is also recommended. When it is installed, the panel opens each game server's port
in it.

| What | Where |
| --- | --- |
| Panel code | `/usr/share/redstonepanel` |
| Panel state (accounts, settings, TLS, templates) | `/var/lib/redstonepanel` |
| Activity log | `/var/log/redstonepanel/panel.log` |
| Game servers | `/opt/minecraft-*` |
| Service | `systemctl status redstonepanel` |

`apt remove` stops the panel and keeps its state. `apt purge` also deletes
`/var/lib/redstonepanel`. Neither one touches your game servers in `/opt/minecraft-*`.

## Signing in

There are two kinds of account:

- **Linux accounts.** Members of the `sudo` or `redstonepanel` group sign in with their
  Linux username and password, checked through PAM. They are panel admins. To let someone
  in without giving them `sudo`:

  ```bash
  sudo usermod -aG redstonepanel alice
  ```

- **Panel accounts.** These exist only in the panel, with role `admin` or `user`. An admin
  creates them in the panel, or root creates them from the command line:

  ```bash
  sudo redstonepanel add-user alice --role user
  ```

  The password is asked for interactively, or read from standard input when it is piped.
  The default role is `admin`.

`sudo redstonepanel status` shows the service state, the address, the HTTPS and sign-in
mode, and the panel accounts. See `man redstonepanel` for details.

## Panel Settings

Admins can change how the panel is reached under **Panel Settings**:

- **HTTP (default).** Plain HTTP on port 8080. Fine on a trusted home network; anyone who
  can watch the traffic can see passwords and session cookies.
- **HTTPS, self-signed.** The panel makes its own certificate authority and serves HTTPS
  (port 8443 by default). Install the CA certificate the panel offers for download into your
  browser or OS to get rid of the warning.
- **HTTPS, custom certificate.** Point the panel at a certificate and key you already
  have, for example from certbot.
- **Trusted-LAN sign-in.** Visitors from the private networks you list (by default
  `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `fc00::/7`) can use the panel without
  signing in. Its limits:
  - LAN visitors get the **user** role, never admin. Changing panel settings and
    accounts still needs a sign-in.
  - It never applies to requests through a reverse proxy (any `X-Forwarded-For`,
    `Forwarded` or `X-Real-IP` header) or from loopback, so a proxy on the same machine
    can't make internet visitors look local.
  - It only applies when the browser used one of this machine's own names or addresses
    (add extra names such as `minecraft.lan` in the settings), which blocks DNS rebinding.
  - Anyone on those networks gets in, including guests on your Wi-Fi.

### Locked out?

If a settings change leaves the panel unreachable (a bad certificate, a port clash, a
forgotten trusted range), run this on the server:

```bash
sudo redstonepanel reset-security
```

It switches the panel back to plain HTTP on port 8080 with sign-in required, and restarts it.

## Exposing the panel to the internet

Don't forward port 8080 to the internet. If you need remote access, prefer a VPN (for
example WireGuard or Tailscale). If you do need a public address, put a reverse proxy with a
real certificate in front of the panel, on the same machine, and keep the panel in its
default HTTP mode. With Caddy, the whole config is:

```
panel.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

With nginx:

```nginx
server {
    listen 443 ssl;
    server_name panel.example.com;
    # ssl_certificate / ssl_certificate_key from certbot

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

The panel trusts `X-Forwarded-Proto` only from `127.0.0.1`, which is why the proxy should
run on the same machine; it then marks session cookies `Secure`. The panel still listens on
all interfaces, so block port 8080 from outside with your firewall (for example
`sudo ufw deny 8080/tcp`). Keep sign-in required, and use strong passwords.

## Security note

The panel runs as **root**. It has to: it writes systemd units, installs packages, opens
firewall ports and manages files in `/opt`. Anyone with a panel **admin** account
effectively has root on the machine, so treat admin accounts like `sudo` access. Keep the
panel off the open internet unless it is behind a proxy as described above, and keep
Ubuntu updated.

To report a security problem, please open a private
[security advisory](https://github.com/qrussell/redstonepanel/security/advisories/new)
rather than a public issue.

## Building from source

You need Docker. From a checkout:

```bash
docker run --rm -v "$PWD:/src:ro" -v "$PWD/dist:/out" ubuntu:24.04 bash /src/packaging/build-deb.sh
```

This builds `dist/redstonepanel_X_all.deb` in a clean Ubuntu container and runs lintian on
it. One build serves both 24.04 and 25.04.

To test it like a real server, install it in a systemd container:

```bash
docker build --build-arg REL=24.04 -t rp-systemd:24.04 packaging/test
docker run -d --privileged --name rp-2404 -v "$PWD/dist:/dist:ro" -v "$PWD/packaging/test:/t:ro" rp-systemd:24.04
docker exec rp-2404 bash /t/test-install.sh
docker rm -f rp-2404
```

`test-install.sh` installs, signs in, switches HTTPS on and off, upgrades, removes and
purges the package, printing PASS/FAIL for each step. Pass `--no-install-recommends` to
test a Bedrock-only install.

### The stylesheet

The UI uses Tailwind CSS, compiled into `static/css/tailwind.css` and committed, so the
panel works on networks without internet access. After adding or changing Tailwind
classes in `static/*.html` or `static/js/app.js`, regenerate it (needs Node.js):

```bash
packaging/build-css.sh
```

### Releases

Pushing a tag such as `v1.0.1` runs `.github/workflows/release.yml`. It builds the .deb,
runs the install tests on Ubuntu 24.04 and 25.04 (with and without recommends), and
attaches the .deb to a GitHub Release. The tag must match the version in
`debian/changelog`, so add a changelog entry first (`dch -v 1.0.1`).

## License

MIT. See [LICENSE](LICENSE).
