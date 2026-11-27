# Web addresses through Cloudflare Tunnel, set up by the panel

Status: **plan, waiting for Quentin's go** · Target: 1.29 (after 1.28 Sign in with Discord) ·
Written 27 Nov 2026

Asked for by Quentin on 27 Nov 2026: once `cloudflared` runs on the machine, the panel should set
up the Cloudflare Tunnel's hostnames and ports itself, for the master panel and the join pages,
"to allow for simplifying the many webpages hosted by the panel".

## The admin problem

A panel serves several web pages: the panel itself (8443), the join pages (8444, 1.21.5), and from
1.28 two Discord sign-in callbacks, one on each. Reaching them from outside the home means a
router forward per port, `address:port` links, a self-signed certificate warning, and for 1.28 a
redirect address that Discord accepts. 1.21.9 already lets a Cloudflare Tunnel carry the join
pages, but the admin copies the ingress lines into Cloudflare by hand, adds the DNS name by hand,
and does it again for the panel. With this, the admin picks a domain and two names once
(`panel.example.org`, `play.example.org`) and the panel keeps the tunnel and DNS in step.

What it can't do: a tunnel carries web traffic only. Minecraft's game ports (Java TCP, Bedrock
UDP) still need port forwarding (or another tool), and the Port forwarding page stays as it is
for them. SFTP (1.18) also stays a forwarded port.

## In a cluster: the master serves every join page

Quentin's question: should the master serve the join pages, or each node its own?

**Recommendation: the master serves all of them, and only the master needs a tunnel.** That is
already how join pages work: since 1.21.5 the master makes pages for its nodes' servers and reads
what to show through `GET /api/servers/{name}/join-info` on the node, since 1.21.6 it builds a
node server's player pack, and since 1.26 a page's **Start** goes from the master to the node
(1.28 adds **Stop** the same way). So:

- One tunnel, one Cloudflare token, one join hostname (`play.example.org/join/...`) for every
  server in the cluster, and one place for the 1.28 join page sign-in and Discord redirect.
- Nodes stay private. Nothing on a node is published, and the cluster rule "nodes never call the
  master or anyone else" holds. Windows and Linux nodes are treated the same.
- What it costs: when the master is down, every page is down (a node's server already shows
  "isn't available right now" when its node can't be reached). The traffic is small: HTML, a
  status check every 30 seconds, and player packs passing through.
- Players still connect their game to the node's own address and game port, which the page
  already shows (the address the admin typed).

A node that isn't in a cluster, or a node whose admin wants its own pages, can still turn on its
own join pages and its own tunnel; nothing here stops that. It's just not needed.

**Node dashboards** are the one thing the master doesn't serve today: 1.25's **Open dashboard ↗**
opens the node's own address, which only works on the home network. Phase 2 below adds an
optional hostname per node through the master's tunnel.

## What's already there to build on

- **1.21.9 `core/joinsite.py`**: `join_url` (the address players open), `join_proxies` (whose
  `X-Forwarded-For` counts), `join_proxy_only`, and `cloudflared_config()`, the ingress lines to
  copy. `core/cloudflare.py` keeps Cloudflare's network list (not needed for a tunnel).
- **Trusted-LAN access** (`settings.trusted_network_user`) already refuses any request with
  `X-Forwarded-For`, which cloudflared always sends, so tunnel visitors never count as the LAN.
- **Services through `core.platform`** (1.21): start, stop, restart and status of a service on
  systemd and on Windows services.
- **Secrets** like the 1.27 bot token: a file under `DATA_DIR`, 0600 (Windows: the data folder's
  ACL), never sent back, last four characters shown.

## Two ways a tunnel is managed, both supported

Cloudflare has two kinds of tunnel, and the panel can tell which one is running:

1. **Managed in Cloudflare** (the dashboard's default, and what `cloudflared service install
   <token>` sets up): the hostnames live in Cloudflare, and cloudflared fetches them. The panel
   changes them through Cloudflare's API with a token the admin creates. No restart needed.
2. **Managed in a file** (`config.yml`, set up with `cloudflared tunnel create`): the hostnames
   live in the file on this machine. The panel edits its own block in the file and restarts
   cloudflared.

### Finding cloudflared (`core.platform.cloudflared()`)

- **Linux:** `systemctl show cloudflared` (and any `cloudflared@*`/`cloudflared-*` unit): running
  or not, and its command line.
- **Windows:** the `Cloudflared` service and its command line from the service's registry entry.
- A command line with `--token <value>` (or `--token-file`) is a tunnel managed in Cloudflare. The
  token is base64 JSON whose `a` is the account and `t` the tunnel ID; the panel reads those two
  and **never stores or shows the token's secret**. A command line with `--config <file>` (or the
  default `/etc/cloudflared/config.yml`, `%SystemRoot%\System32\config\systemprofile\.cloudflared\`
  on Windows) is a tunnel managed in a file.
- cloudflared on **another machine** (a router, a Docker container, a Pi) is fine for a tunnel
  managed in Cloudflare: the admin picks the tunnel from the account's list and says it runs
  elsewhere; the services then point at this panel's LAN address instead of `localhost`, and that
  machine's address goes into the trusted proxies.
- Confirm at build time: the exact unit and service names `cloudflared service install` uses on
  current releases, and whether `--token-file` exists in the version Ubuntu and Windows users get.

## Setup (Panel Settings → **Cloudflare Tunnel**, admins only)

1. The card shows what it found: "cloudflared is running on this machine, tunnel managed in
   Cloudflare (`3f2a…`)" or "cloudflared isn't installed" with the install link and commands for
   this system (the panel does not install cloudflared: it isn't in Ubuntu's archive).
2. **API token** (for a tunnel managed in Cloudflare, and for DNS either way). The card links to
   Cloudflare's token page with the permissions named:
   - Account → **Cloudflare Tunnel: Edit**
   - Zone → **DNS: Edit**, for the one domain
   - optional: Account → **Access: Apps and Policies: Edit**, to put Access in front of the panel
   On save the panel checks the token (`/user/tokens/verify`, then one read per permission) and
   says which one is missing.
3. **Domain** (a list of the token's zones) and the **names**:
   - **Panel** (`panel.example.org`), off by default.
   - **Join pages** (`play.example.org`), on by default when join pages are on. Pages stay under
     `/join`, so the same name can't reach the panel.
   Each name must be inside the chosen domain and not already used by a DNS record the panel
   didn't make.
4. **Apply**. The panel shows what it will change (rules and DNS records) before doing it.

### What Apply does, for a tunnel managed in Cloudflare

- Reads the tunnel's configuration (`GET /accounts/{a}/cfd_tunnel/{t}/configurations`), keeps
  every rule it didn't make, puts its own rules first and the catch-all last, and writes it back
  (`PUT`). Its rules:
  - `panel.example.org` → `https://localhost:<https port>` (or `http://` when HTTPS is off),
    with `noTLSVerify` only for `localhost` and the panel's self-signed certificate.
  - `play.example.org`, path `^/join(/.*)?$` → the join port, the same rule as 1.21.9's copy box.
- Creates a proxied `CNAME` for each name to `<tunnel id>.cfargotunnel.com`, with the comment
  "Made by RedstonePanel". A record with that name that the panel didn't make is never changed.
- Saves which rules and records it made in `data/cloudflare-tunnel.json`. **Remove** takes away
  only those.

### What Apply does, for a tunnel managed in a file

- Edits only a block between `# BEGIN RedstonePanel` and `# END RedstonePanel` at the top of the
  `ingress:` list. Python's standard library has no YAML parser, so the panel edits text lines:
  a file it can't recognise (no `ingress:` list, tabs, flow style) is left alone and the card
  shows the lines to paste, as today.
- Keeps the old file as `config.yml.redstonepanel-old`, runs `cloudflared tunnel ingress validate`
  on the new one, restarts cloudflared through `core.platform`, and puts the old file back if the
  check fails or cloudflared doesn't come back ready within 30 seconds (its metrics `/ready` page).
- DNS: through the API token when there is one; without one, the card shows
  `cloudflared tunnel route dns <tunnel> <name>` to run.

### After Apply

- `join_url` becomes `https://play.example.org`, and `join_proxies` gets `127.0.0.1` and `::1`
  (or the other machine's address), so 1.21.9's per-player rate limit works. **Only through the
  proxy** is offered, with a note that LAN visitors then use the public name too.
- A new `panel_url` setting (`https://panel.example.org`) is used wherever the panel builds a
  link to itself: the 1.28 Discord redirect addresses, the phone QR code, webhook links, and the
  first-password and reset-password links (1.24).
- The panel's own sign-in limit counts the real visitor: a request from the local cloudflared (or
  the named machine) is read from `CF-Connecting-IP`; from anyone else that header is ignored.
  Today everyone through the tunnel would share one limit as `127.0.0.1`.
- The card checks each name from outside: a request to `https://play.example.org/join/assets/join.css`
  and to the panel's sign-in page, and shows the tunnel's health from the API (connections, healthy
  or down). A failure says which step to look at.
- The Port forwarding page and the port check say the panel's ports no longer need forwarding,
  and leave the game ports as they are.

## Keeping the admin panel safe

Putting the panel on the internet is the risky part, so the card says so and defaults to safe:

- **Panel** is off until the admin turns it on; join pages alone need none of this.
- Turning on **Panel** recommends **Require two-factor for admins** (1.16.7), with one click.
- **Cloudflare Access in front of the panel** (recommended, on by default when the token allows
  it): the panel makes an Access application for `panel.example.org` with a policy of the email
  addresses the admin types, using Cloudflare's emailed one-time code. A visitor must pass Access
  before they even see the sign-in page. Join pages never get Access (they're for players; 1.28
  decides who sees what). Without the Access permission the card gives the steps instead.
- The 1.28 Discord callback on the panel name works behind Access: the visitor already has the
  Access cookie from opening the panel.
- Trusted-LAN access is never given through the tunnel (already true, kept by a test).
- **Passkeys** (Windows, 1.24) belong to the address they were made at, so a passkey made at
  `10.0.0.5` doesn't work at `panel.example.org`. The card says so; the admin adds one there.
- The token: admin-only, kept like the bot token, never returned; calls go only to
  `api.cloudflare.com`; names are what the admin typed, never taken from a request; every change
  is logged (`[TUNNEL] alex added play.example.org`). The new routes are not on the node token
  allowlist.

## Windows, Linux and clusters

- Everything is HTTP to Cloudflare's API with `urllib` (proxy-aware, like `discordbot.api()`), the
  same on both systems. The only system-specific parts are finding cloudflared, its config path and
  restarting it, all in `core.platform`.
- Per panel: a master and a node with their own tunnels each have their own card. The master's
  card is the one that matters for join pages, per the recommendation above.

## Phase 2 (a later point release, only if wanted): node dashboards through the master

An optional name per node (`node-pc.example.org`) on the master's tunnel, pointing at the node's
HTTPS address, with the cluster CA as cloudflared's `caPool` so the certificate is checked. 1.25's
**Open dashboard ↗** then uses that name. The node must count the master's address as a proxy for
its sign-in limit and see the public name as its own; a tunnel managed in a file needs the CA file
on the master. Size: about three days. Not in 1.29 unless Quentin asks.

## Not in this plan

- Installing cloudflared or creating the tunnel itself (the admin follows Cloudflare's own steps
  once; the panel takes over from there). Could come later.
- Game traffic through Cloudflare (Spectrum is a paid Cloudflare product).
- Checking the Access token (`Cf-Access-Jwt-Assertion`) in the panel. Possible with the standard
  library, but Access already stops visitors before the panel; later if asked.
- Other tunnel providers (Tailscale Funnel, ngrok). The file-free design would fit them, if asked.

## Tests

- **Unit** (`tests/test_cloudflaretunnel.py`), against a stand-in Cloudflare API: rules merged
  with the admin's own kept and the catch-all last; Remove takes only the panel's rules and
  records; a name outside the domain or held by someone else's record refused; missing permissions
  named; token never in any reply or log; reading account and tunnel from a token without keeping
  its secret; config.yml blocks added, changed and removed, unrecognised files left alone, the old
  file restored when the check fails; `CF-Connecting-IP` used only from the local cloudflared;
  trusted-LAN refused through the tunnel; `panel_url` used for the Discord redirects and QR code.
- **End to end** (`packaging/test/test-cloudflare-tunnel.sh`, systemd containers): a real
  cloudflared from Cloudflare's package with a file-managed configuration, `ingress validate`,
  restart and roll back; the API half against the stand-in. cloudflared's download needs the
  internet, so on GitHub `release.yml` or Superman if the cloud can't reach it.
- **For real, by Quentin**: a tunnel on his Cloudflare account and a throwaway name on his domain,
  on a test machine (not 10.0.0.193 without his OK), on Linux and on Superman with the Windows
  service: Apply, both names load from a phone on mobile data, Access asks for the code, Remove
  leaves his own rules and records untouched, and a 1.28 Discord sign-in through the panel name.

## Size

About two weeks: finding cloudflared on both systems (2 days), the API client, merge and DNS
(3 days), the file mode with validate and roll back (2 days), Access (1 day), `panel_url` and the
sign-in limit through the tunnel (1 day), the card and docs (2 days), tests (2 days).

## Decisions for Quentin

1. Master serves every join page (recommended, already how it works) or each node its own.
2. 1.29 with both tunnel kinds (recommended), or only tunnels managed in Cloudflare first.
3. Access in front of the panel on by default when the token allows it (recommended).
