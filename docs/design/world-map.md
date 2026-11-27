# A world map in the panel

Status: **plan, all three decisions made, waiting for Quentin's go to build** · Target: 1.30 (after
1.29 Cloudflare Tunnel) · Written 27 Nov 2026

Asked for by Quentin on 27 Nov 2026: "How can you put a world map view in the panel?" He said yes
to planning it as a feature for after 1.29.

## The admin problem

People want to see their world from above: where the base is, how far the world goes, where
everyone is right now. Today an admin who wants that installs a map plugin by hand, finds its
config file, changes its port, forwards that port on the router, and sends players
`http://address:8100`. Two servers with the same map plugin fight over the port (1.21.9's
`core/modports.py` already moves the second one). Vanilla and Bedrock servers can't run plugins at
all, so their owners get nothing.

With this, a server's dashboard gets a **Map** tab. For a server that can run plugins the panel
installs the map with one click; for vanilla and Bedrock it draws the map itself on a schedule.
The map opens through the panel's own address, so there is nothing to forward, and it can be shown
on the server's join page.

## What admins will see

- **Server → Map tab** (anyone who can **View** the server, 1.18). Before a map exists it says what
  the panel can do for this server type and offers one button:
  - Paper, Purpur, Fabric, Forge, NeoForge, Quilt: **Add a map** (installs BlueMap from Modrinth, or
    squaremap, the light choice), then **Restart now** or "from the next start".
  - Vanilla Java: **Draw a map** (BlueMap's standalone renderer, run by the panel, see below).
  - Bedrock: **Draw a map** (converted and drawn nightly, below).
- Once there is a map, the tab shows it full width (full screen on a phone, with a **Open in a new
  tab ↗** link), with a line under it: "3D map by BlueMap · live" or "Drawn tonight at 3:00 ·
  next 3:00 tomorrow · **Draw now**".
- **Map settings** (admins and people with **Settings** on the server): when to draw (nightly at
  3:00 by default, or after each backup, or only by hand), which worlds (Overworld on by default,
  Nether and End off), show players on the map (on in the panel), and **Show on the join page**
  (off by default).
- A map plugin the admin installed by hand (BlueMap, Dynmap, squaremap, Pl3xMap) is recognised
  from its config file (the same `KNOWN` table as 1.21.9) and shown in the tab without anything
  to install.
- The Mods tab's "For players" group already lists BlueMap; installing it from there is the same
  as **Add a map**.

## How a map reaches the browser

### Never on the panel's own address

A map is someone else's web page (HTML and JavaScript from the map tool, with text from the game
in it: player names, sign markers, Dynmap chat). If it ran at the panel's own address, a mistake in
it could act as the signed-in admin. So the panel never serves a map at its own origin. Maps are
served by the **join site** listener (1.21.5, port 8444 by default, its own origin), under
`/map/<id>/...`:

- In the panel the Map tab is an `<iframe>` of the join site's `/map/<id>/` address. The panel
  hands the iframe a one-minute ticket (signed, for one server's map only); the join site turns it
  into its own `rp_map` cookie (HttpOnly, path `/map/<id>/`, 8 hours). It is never a panel session.
- The join site listener starts when join pages **or** maps are on. If join pages are off, it
  listens but answers only `/map/` addresses, and the firewall opens its port only for the home
  network (or not at all on a machine with no players outside). Behind 1.29's tunnel the map is at
  `play.example.org/map/<id>/`.
- The panel's state-changing routes already refuse requests from another origin
  (`settings.same_origin`), so a map page can't change anything in the panel.
- The join site's strict Content-Security-Policy stays on join pages; map pages get their own
  (scripts and WebGL from the map's own address only, no other sites, `frame-ancestors` the
  panel's and join site's addresses).
- Only `GET` and `HEAD` pass through. That also turns off Dynmap's web chat, which would let a web
  visitor speak in the game; it stays off in 1.30.

### The panel proxies; map web servers listen only on the machine

- When the panel installs (or recognises) a map, it sets the map's own web server to listen on
  `127.0.0.1` only (`webserver.conf` `ip`, squaremap `internal-webserver.bind`, Dynmap
  `webserver-bindaddress`) and gives it a free port through `core/modports.py`. The map's port is
  then not opened in the firewall. An admin who already forwards a map's port gets asked first
  (**Keep it reachable directly** keeps the old bind).
- The join site passes `/map/<id>/...` to `127.0.0.1:<port>` as a stream (no buffering of big
  tiles), keeping `Content-Encoding` (BlueMap's tiles are pre-compressed).
- Maps poll for live data over plain HTTP (BlueMap `live/players.json`, Dynmap `up/world/...`,
  squaremap `tiles/players.json`); no websockets to carry.

### When the server is stopped or asleep

BlueMap (plugin and standalone) writes its whole map to files in the server folder. When the
server is stopped, asleep (1.26), or the map was drawn by the panel, the join site serves those
files itself, read-only, through `core/files.py` (never following a symlink), with the right
`Content-Encoding` for BlueMap's `.gz` files. So **opening the map never wakes or starts a server**.
The map shows "Server is asleep · players aren't shown". Other map tools need their own web server,
so their tab says "The map shows while the server is running".

### In a cluster

Same rule as 1.29: the master serves join pages and only the master faces the internet.

- A node's map web server listens on the node's `127.0.0.1` too. The master's join site passes
  `/map/<id>/...` for a node's server to a new read-only node route
  `GET /api/servers/{name}/map/raw/{path}` through `cluster_client.open_proxy` (streams, like
  player packs), and the node passes it to its local map or serves the files. The route goes on
  the node token allowlist as GET-only; `tests/test_cluster.py` covers it.
- A node's own panel shows its own maps the same way, for people signed in to the node.
- Nodes never call the master.

## Drawing maps for vanilla Java

- The panel downloads **BlueMap's standalone renderer** (`BlueMap-<ver>-cli.jar`) from BlueMap's
  GitHub releases, checks it against the SHA-256 that GitHub lists for the file (as 1.20 checks
  loader downloads), and keeps one copy for all servers under the panel's data folder.
- It runs with the server's own Java (BlueMap needs Java 21 or newer; an older server Java gets the
  newest one apt or the Windows Java package offers, as 1.16.9 does), as the `minecraft` account,
  in the server's folder (`bluemap/` next to the world), `-r` to draw what changed since last time.
- Before drawing, a running server gets `save-all flush` over RCON so the files on disk are
  current. The drawing reads the world and never writes to it.
- **Once, for BlueMap:** BlueMap needs Minecraft's block textures, which it downloads from Mojang
  (`accept-download` in `core.conf`). The panel asks the admin to tick "BlueMap may download
  Minecraft's textures from Mojang" the first time, and writes it only then.
- Scheduled as `redstonepanel-map@<server>` through `core.platform` schedules: a systemd timer on
  Linux, a Task Scheduler task on Windows (the same code as backups and restarts). It runs while
  the panel is stopped.
- Low priority so it never slows the game: `nice 19` and idle I/O on Linux, Below normal on
  Windows; `render-thread-count` 1 on machines with 4 cores or fewer.
- Skipped when the world hasn't changed since the last drawing (region file times), so a sleeping
  server costs nothing. Never at the same time as a backup, restore, move or type switch of the
  same server (it waits for them).

## Drawing maps for Bedrock

No map tool reads Bedrock worlds well and stays maintained, except by converting first:

1. Copy the world the same way a backup does (`save hold` / `save query` / `save resume`, as Bedrock backups already do)
   into a temporary folder. The real world is never opened by the converter.
2. Convert the copy to a Java world with **Chunker** (Hive Games' open-source converter, Java CLI
   jar from its GitHub releases, SHA-256 checked).
3. Draw it with the same BlueMap standalone renderer, then delete the copy.

Before starting, the panel checks there is room for about twice the world's size and refuses with
a Needs attention item if not (protect worlds first). Chunker needs updating when Bedrock adds new
blocks; until then new blocks show as their nearest match or missing. Players don't appear on a
Bedrock map (it's drawn, not live). Both tools are Java, so this needs Java on the machine even for
a Bedrock-only panel (installed the first time, with the admin's OK).

Not used: uNmINeD (has Bedrock support and a command line, but isn't open source and its licence
doesn't allow the panel to download it for people), bedrock-viz (behind on new versions).

## Join pages

- **Show on the join page** (off by default). The join page then gets a **Map** button opening
  `/join/<link>/map/`, served the same way.
- Who can see it follows the page's 1.28 setting (anyone with the link / signed in / by
  permission). With "by permission", there's a new tick **See the map** beside See how to join,
  Start and Stop.
- **Players on the public map**: hidden by default, with a switch. When hidden, the join site doesn't pass the map's live
  player data (each tool's live path is in the `KNOWN` table) to join page visitors; the panel's
  own Map tab still shows them.
- If the master's 1.29 tunnel is set up, the map is at `play.example.org/join/<link>/map/`.

## Raspberry Pi and small machines

- 3D drawing is heavy: BlueMap's first full drawing of a large world can take hours on a Pi 4 and a
  few GB of disk; later drawings only redo what changed. The Map tab says so before the first
  drawing on an ARM64 machine with 8 GB or less.
- squaremap is the light choice on a Pi: flat 2D, small files, little CPU.
  On a Pi, **Add a map** suggests it first.
- Phones viewing a 3D map: BlueMap has a flat view toggle; the tab opens in flat view on phones.
- Documented in README, install guide, wiki and man page, as every Pi note is.

## Backups and disk

- Map files can be rebuilt, so backups leave them out by default (`bluemap/web/maps`,
  `plugins/BlueMap/web/maps`, `dynmap/web/tiles`, `squaremap/web/tiles`, Pl3xMap `web/tiles`). A
  tick in Backups puts them back in.
- The Map tab shows how much space the map takes, with **Delete the map** (keeps the tool, its
  settings and markers; the next drawing starts from scratch).
- Moving a server and changing its type (1.19) keep or rebuild the map; a server switched
  from vanilla to Paper keeps its drawn map and later gets the plugin's live one.

## Windows, Linux and clusters

- Everything new goes through `core.platform`: the schedule, low priority, Java choice, and file
  serving. Downloads use `urllib` (proxy-aware) the same on both systems.
- The map web servers listen on `127.0.0.1` on Windows too; no Windows Firewall rule is needed for
  them, and 1.24.1's cluster firewall rules don't change.
- Every machine draws its own servers' maps; the master never draws a node's map.

## Security

- Map content on its own origin (above), GET only, `rp_map` cookie per map, one-minute tickets.
- Downloads only from GitHub releases (BlueMap, Chunker) and the existing Modrinth/Hangar allowlist,
  each checked by SHA-256; kept root-owned, run as `minecraft`.
- Static map files served through the safe file code, confined to the map folders, never through a
  symlink, with `X-Content-Type-Options: nosniff`.
- Every change is logged (`[MAP] alex added BlueMap to survival`, `[MAP] survival drawn in 41 min`).
- New routes: `GET/PUT /api/servers/{name}/map` (settings, View/Settings permission),
  `POST /api/servers/{name}/map/draw` (Settings), `POST /api/servers/{name}/map/ticket` (View),
  and the node's GET-only raw route. API tokens (1.25) get the map with the `view` grant.

## Not in this plan

- Markers, claims and region overlays set up from the panel (BlueMap and others have add-ons for
  WorldGuard, GriefPrevention and so on; admins can add them from Mods).
- A map drawn by the panel itself (our own renderer). Big, and the tools above do it well.
- Dynmap web chat, and anything else that writes from the web.
- Maps for Bedrock add-on servers (Endstone, LeviLamina); the panel doesn't run those.

## Tests

- **Unit** (`tests/test_worldmap.py`): bind address and port set in each tool's config without
  touching other lines; recognised tools from config files; ticket signing, expiry and one-server
  scope; `rp_map` cookie never accepted by the panel; only GET/HEAD passed; live player paths
  dropped for join visitors when players are hidden; static serving confined to the map folder and
  refusing symlinks; `Content-Encoding` on `.gz` files; schedule skipped when the world hasn't
  changed; refused when the disk is short (Bedrock); backup excludes; node allowlist GET-only.
- **End to end** (`packaging/test/test-world-map.sh`, systemd containers): Paper with BlueMap from
  Modrinth, map opens through the join site, the plugin's port not reachable from outside, server
  stopped then map still served from files; vanilla world drawn by the standalone renderer; a
  cluster where the master's join site shows a node's map. Needs Modrinth, GitHub and Mojang
  downloads, so on GitHub `release.yml` or Superman if the cloud can't reach them.
- **For real, by Quentin**: a map on a real world (10.0.0.193 only with his OK) opened from the
  panel, the join page and a phone; on Windows on Superman; a Pi if one is at hand for timings.

## Size

About two and a half weeks: plugin install, config and recognising (2 days), join site map
proxy, tickets, cookie and static serving (3 days), standalone drawing, schedules on both systems
(3 days), Bedrock through Chunker (2 days), join page and 1.28 permission tick (1 day), cluster
route (1 day), the tab, settings, docs and Pi notes (2 days), tests (2 days). Without Bedrock,
about two weeks.

## Decisions for Quentin

All three decided by Quentin on 27 Nov 2026 ("go with the recommendations"):

1. ~~Which map tools the panel installs with one click.~~ Decided: **BlueMap and squaremap**.
   BlueMap covers every Java server type, plugin or not; squaremap is the light flat map for a Pi.
   Dynmap and Pl3xMap are recognised when installed by hand but not installed by the panel.
2. ~~Bedrock maps in 1.30, or in a later point release.~~ Decided: **in 1.30**, drawn nightly
   through Chunker, not live.
3. ~~Players on a map shown on a join page.~~ Decided: **hidden by default**, with a switch.
