# Sign in with Discord, and who can see a join page

Status: **built for 1.28** (go from Quentin 27 Nov 2026), not yet released · Written 27 Nov 2026

Asked for by Quentin on 27 Nov 2026: "communities that selfhost would find this a useful feature".
Discord sign-in moves into the free core. Microsoft, Google, GitHub, Apple and Steam sign-in stay in
the paid Teams bundle (private add-ons catalog, entry 10).

Two parts, sharing the Discord sign-in code:

1. **Sign in with Discord on the panel's sign-in page**, with Discord roles giving panel access.
2. **Join page access**: a page can stay open to anyone, ask visitors to sign in (a panel account
   or Discord), or go by permission. With Discord, a page can require membership of
   the community's Discord server. What each visitor sees and may do (the address, **Start**,
   **Stop**) depends on their panel permissions or Discord roles. Works with no Discord set up.

## The admin problem

Community servers already organise people in Discord: who's a member, who's a mod, who's staff.
Today the owner repeats that by hand in the panel (panel accounts, 1.18 permissions) and on the
join page (public to anyone with the link). With this, the Discord server is the one list:
give someone the Mod role and they can stop the server from the join page; take it away and they
can't. Kick someone from Discord and they lose the join page too.

## What's already there to build on

- **1.27 `core/discordbot.py`**: Settings → Discord holds a bot token (0600, never sent back),
  the Discord servers (guilds) it answers in, roles, and a small Discord REST client (`api()`,
  proxy-aware, standard library). The same Discord application gets the sign-in: its
  **Application ID** is the OAuth2 client ID, and the owner adds a **client secret** and a
  **redirect** in the Developer Portal's OAuth2 page.
- **1.26 join page Start** (`joinpage.start_from_page`, `start_server`): cooldown per server,
  limit per address, logged. Discord visitors reuse it with a per-Discord-user limit instead.
- **1.18 grants** (`core/grants.py`): per-server permissions (`view`, `console`, `power`, ...) and
  presets. Discord roles map onto these, so nothing new is invented for what a helper may do.
- **Sign-in** (`core/auth.py`): identities `{u, src, v, m}`, `read_session_token` re-checks the
  account on every request, the 5-per-10-minutes sign-in limit, 2FA, passkeys, Remember this
  device.

## Part 1: Sign in with Discord on the panel

### Setup (Panel Settings → Discord → **Sign in with Discord**)

- **Sign in with Discord** switch, off by default.
- **Client secret** (kept with the bot token in `discord.json`, 0600, never returned; last four
  shown). The client ID is read from the bot's application when a bot token is set; otherwise
  the admin types the Application ID. Sign-in works **without a bot** too: it needs only the
  OAuth2 app.
- **Redirect address to copy** into the Developer Portal, built from the address the admin opened
  the panel by (`https://<host>:8443/api/auth/discord/callback`), with a note when it's an IP
  address or self-signed. Behind HAProxy or Cloudflare (1.21.9) it's the public name.
- **Discord server** (one guild) whose membership and roles count.
- **Role table**: each row is a Discord role → what it gives on this panel:
  - a permission preset or chosen permissions (1.18) on chosen servers or every server, or
  Never **Admin** (decided below): an admin uses **Link Discord** on their own admin account.
  Nothing ticked means nobody can sign in with Discord. Several rows add up.
- **Require Discord two-factor** (off by default): Discord's `identify`
  scope says whether the account has 2FA on (`mfa_enabled`; confirm at build time), and an
  account without it is refused with "Turn on two-factor in Discord first".

### How it works

Standard OAuth2 authorization-code flow, the panel being a confidential client (it holds the
secret, the browser never sees it):

1. **Sign in with Discord** on the sign-in page → `GET /api/auth/discord/start` sets a one-use
   `state` (random, 10 minutes, in a short HttpOnly cookie and server-side) and sends the browser to
   `discord.com/oauth2/authorize` with scopes `identify guilds.members.read`. PKCE as well if Discord
   accepts it for confidential clients (check at build time; `state` is required either way).
2. Discord sends the browser back to `/api/auth/discord/callback?code&state`. The panel checks the
   `state`, swaps the code for a token (`POST /oauth2/token`), reads `GET /users/@me` (fixed user
   ID, name, `mfa_enabled`) and `GET /users/@me/guilds/{guild}/member` (membership, roles, nick),
   then **throws the token away**. Nothing from Discord is stored but the user ID, name and roles.
3. The roles are matched against the role table. No match → "Your Discord account doesn't have a
   role that can use this panel." Counts towards the sign-in limit per address.
4. A session cookie as today, identity `{u: "<discord id>", src: "discord", r: [role ids], t: <checked>}`.

### Accounts

- **A new account source, `discord`**, keyed by the Discord user ID (names change, IDs don't):
  `discord:123456789012345678`. Shown on the Users page as "Discord: QuickFire314" with the roles
  that gave access. Not created by hand; it appears at first sign-in and can be **Blocked** by an
  admin (blocked beats any role).
- **Link Discord** in My account: an existing panel or Windows/Linux account links its Discord ID,
  so **Sign in with Discord** signs in as that account (its own role and grants, its own 2FA and
  passkeys). One Discord ID links to one account. Unlinking is in the same place.
- **Grants** come from the role table at each check, not stored, so changing a role in Discord or
  a row in the table changes access at the next check.
- **Re-checks**: `read_session_token` re-reads the member every 10 minutes (cached), through the
  1.27 bot when it's set up and in that guild (`GET /guilds/{g}/members/{id}`, no stored user token;
  confirm no privileged intent is needed) and otherwise at the session's end (12 hours). Left the
  Discord server or lost the role → signed out at that check.
- **Two-factor**: a Discord-only account has no panel password, so no panel TOTP or recovery codes;
  its second step is Discord's own 2FA (above). A **linked** account keeps its panel 2FA: after
  Discord, the panel still asks for its code (or a passkey), so linking Discord never weakens an
  account. "Require two-factor for admins" is met only by panel 2FA or a passkey, since a Discord
  account is never an admin by itself.
- **What a Discord-only account can't do**: SFTP (no password; it gets the same "use SSH keys"
  note as passkey-only accounts, if SFTP keys are added later), the terminal (admins only, as now),
  and API tokens are made by admins as today.
- **Logged** as `[AUTH] discord:QuickFire314 (id) signed in with Discord (roles: Mod)` and every
  action as `discord:QuickFire314`, like `[DISCORD]` in 1.27.

### Windows, Linux and clusters

- Pure HTTP to Discord through the existing `discordbot.api()` (urllib, proxy-aware): the same code
  on both systems, files under `DATA_DIR`, nothing in `core.platform` needed beyond what auth uses.
  Windows account sign-in and Linux PAM are unchanged and stay the way back in.
- **Clusters**: per panel, like every account (Quentin's rule). Each panel that wants Discord
  sign-in sets its own redirect (the redirect must be that panel's address). A master's **Open
  dashboard signed in** keeps working for master admins; a Discord-signed-in master admin uses it
  the same way. Nodes never call Discord for the master. New routes: `/api/auth/discord/*` are not
  on the node token allowlist.
- No new dependency: standard library only.

## Part 2: Who can see a join page, and Start and Stop by permission

Discord is **optional** here (Quentin, 27 Nov): everything below works on a panel with no Discord
set up, using panel accounts, and Discord sign-in is one more way in when it is set up.

### Setup (Join pages → a page → **Who can see this page**)

Three choices per page:

1. **Anyone with the link**: today's page, no sign-in (the default, and what every existing page
   keeps). The 1.26 **Let visitors start the server** switch stays as it is.
2. **Anyone who signs in**: the page asks for a sign-in first, and every signed-in visitor sees
   the whole page and may press Start if the page allows it.
3. **Signed in, by permission**: what each signed-in visitor sees and may do comes from a table.

Ways to sign in on the page (ticks, at least one for choices 2 and 3):

- **Panel account**: a username and password form on the page, with the account's 2FA code if it
  has 2FA on. Any panel account works, including a plain "player" account with no server
  permissions in the panel, made on the Users page just for the join page. Windows and Linux
  accounts can't sign in here: they are the machine's admins, and their passwords shouldn't be typed
  into a public page.
- **Discord** (only when Sign in with Discord is set up): with an optional **Must be a member of
  our Discord server** and an **Invite link** (validated `https://discord.gg/...` or
  `discord.com/invite/...`) shown as **Join our Discord**.

The permission table (choice 3), one row per "who", each with three ticks, **See how to join**
(address, how-to steps, player pack, players), **Start the server**, **Stop the server**:

| Who | Example |
| --- | --- |
| Any signed-in visitor | See |
| A panel account (picked from the list) | See, Start |
| Panel accounts by their server permissions | `view` on this server = See; `power` = See, Start, Stop (on by default, so 1.18 helpers need no new setup) |
| Any member of the Discord server | See, Start |
| A Discord role (picked from the server's roles) | Mod: See, Start, Stop |

Rows add up; panel admins always have all three. Quentin's example: *Any Discord member*: See,
Start. *Mod* role: See, Start, Stop.

### What the visitor sees

| Visitor | Sees |
| --- | --- |
| Not signed in | Server name, icon, Online/Asleep/Offline, edition and version; **Sign in** (panel account form and/or **Sign in with Discord**) and **Join our Discord** if set. No address, players or pack. |
| Signed in, not in the Discord server | "You're signed in as QuickFire314 but not in our Discord yet." **Join our Discord**, then **Check again**. |
| Signed in, no row gives anything | "Ask a moderator for access." |
| With **See how to join** | Today's page: address, steps, pack, players (if the page shows them). |
| ... with **Start** | **Start the server** while it's stopped or asleep (1.26 button). |
| ... with **Stop** | **Stop the server** while it's online; with players online it asks "3 players are on. Stop anyway?" first. |

### How it works

- **Panel account sign-in on the join site**: `POST /join/signin` (and `/join/signin/code` for
  2FA), the same 5-per-10-minutes limit as the panel's sign-in (shared counts, so the join site
  can't be used to guess passwords faster), the same account checks (disabled, deleted). It gives a
  join visitor cookie, never a panel session.
- For Discord, the join site gets its own callback, `/join/discord/callback` on the join port (and through the
  1.21.9 proxy), a second redirect to paste into the Developer Portal. It shares the OAuth code
  with Part 1 but **never** gives a panel session: a separate signing purpose (`join visitor`),
  cookie `rp_join` scoped to `/join`, HttpOnly, Secure when HTTPS, `SameSite=Lax` (the callback is
  a top-level redirect from discord.com). The CSP stays strict; the only new outside link is the
  redirect to discord.com.
- Scopes `identify guilds.members.read`; the token is thrown away after reading the member. The
  cookie holds the Discord ID, name, guild, role IDs and when they were checked, for 12 hours.
- **Re-checks**: panel accounts are re-read on every request (as panel sessions are). Discord:
  Start and Stop always re-read the member first (through the bot when it's there,
  otherwise the roles may be up to 30 minutes old and the page sends the visitor through Discord
  again, which is one click for someone who already allowed it). Viewing re-checks every 30 minutes.
- **Start** reuses `start_from_page`: the 2-minute cooldown per server, and a limit of 5 starts per
  signed-in visitor an hour (like `/start`) in place of the per-address limit. On choice 1 pages
  the 1.26 per-address limit stays.
- **Stop** is new on the join site: `POST /join/<link>/stop`, the same `X-Join-Start`-style header
  so another site's form can't send it, and only for a role with **Stop**. It does what the panel's
  Stop does (`manager.control_server(server, "stop")`: a clean stop that saves the world), so with
  sleep mode on it counts as **Stop means stopped** (no waking until the next Start). Never during a
  backup, move or type change (it waits for the backup lock or says "busy, try again"). Limit: one
  stop per server every 2 minutes. A node's server stops through the node's action route, as Start
  does.
- **Logged**: `[JOIN] discord:QuickFire314 (id) stopped minecraft-survival from its join page (role Mod)`, or
  `[JOIN] alex (panel account) started ...`.
- **Webhooks**: the existing stopped/started notifications fire as for any stop and start.
- **Clusters**: the join site runs on the panel that serves the page (master or standalone), so
  only that panel's Discord settings are used; node pages work as today.

### An honest note for the guide

Hiding the address on the join page keeps casual visitors and link-sharers out, but a server's
address is not a secret (it can be guessed or shared). The guide says to keep the whitelist on
for a members-only server. A follow-up idea, not in this plan: **"Add me to the whitelist"** on
the join page for members, asking their Minecraft name once.

## Security review checklist (before release)

- `state` one use, 10 minutes, tied to the browser's cookie; the callback refuses anything else.
- The client secret never leaves the panel; the token is never stored or logged.
- Redirect addresses fixed by settings, never taken from the request (no open redirect).
- Discord sign-ins count in the 5-per-10-minutes limit per address; a refused role counts.
- Join visitor cookies can't be used on the panel and panel cookies don't work on the join site
  (different signing purposes and ports).
- The join site's password form shares the panel's sign-in limits and refuses Windows and Linux
  accounts; it is off unless a page uses it.
- A blocked Discord account stays blocked whatever its roles.
- The role table can't give more than its rows say; no role ever gives Admin.

## Tests

- **Unit** (`tests/test_discordauth.py`, stand-in Discord API like `test_discordbot.py`): the full
  OAuth flow, bad or reused `state`, token exchange failure, no member, wrong roles, several roles
  adding up, no role giving Admin, Discord 2FA required, blocked accounts, linking and unlinking, re-check
  after a role is removed (signed out), sign-in limit, the node allowlist decision, settings that
  never return the secret. Join site: the three access choices with and without Discord set up, panel account sign-in and its
  2FA step and shared limit, each permission row (account, server permissions, Discord member,
  Discord role), each visitor state of the table above, Start and Stop with
  and without the role, the stop confirmation, cooldowns and per-user limit, the header check,
  cookies not crossing between the panel and the join site.
- **Install test** (`test-join-page.sh`, `test-install.sh`): routes present, Discord off by default,
  a gated page shows no address without a cookie.
- **Real Discord**: the cloud can't reach discord.com, so on Superman with the "Redstonepanel Test"
  app in Cielo Cloud, a test role "Member" and "Mod": sign in on the panel as each role, lose the
  role and get signed out, gated join page Start as Member, Stop as Mod, refused Stop as Member.
- **Screens** at 1280 and 390 x 844, dark and light: the sign-in page button, Settings → Discord,
  the Users page, the join page settings and each visitor state.

## Size and release

About two and a half weeks: the shared OAuth code and panel sign-in about a week, the join page access
choices, panel account sign-in on the page, the permission table and Stop about a week and a half. One feature ("Sign in with Discord") for **1.28**, released after 1.27.

## Decisions

1. **Can a Discord role make someone a panel admin?** **Decided 27 Nov 2026 (Quentin): no.**
   The panel runs as root (SYSTEM on Windows). Discord roles give helper permissions only; an admin
   signs in with Discord by **linking** their own admin account, which keeps its panel 2FA.
2. **One release or two?** Both parts in 1.28 (Quentin's go, 27 Nov 2026, "Go ahead to 1.28").

## As built (27 Nov 2026)

- **Code:** `core/discordauth.py` (settings in `discord-signin.json`, 0600; the OAuth2 code flow;
  role grants; linked accounts; member re-checks through the 1.27 bot every 10 minutes, cached).
  `core/auth.py` reads Discord sessions (`src: "discord"`, the name in `dn` because `n` is the
  session nonce). `core/grants.py` treats Discord sign-ins like API tokens: their own grants, never
  admin. `core/joinpage.py` has the access choices, `gate()`, `abilities()` and Stop;
  `core/joinsite.py` the sign-in forms, the Discord callback on the join port and the `rp_join`
  cookie. Everything goes through `core.platform` and runs the same on Linux and Windows.
- **Routes:** `GET /api/auth/discord/{available,start,callback}` (public),
  `GET/POST/DELETE /api/auth/discord/link`, `GET/PUT /api/settings/discord-signin`,
  `PUT/DELETE /api/settings/discord-signin/accounts/{id}`; on the join port
  `POST /join/_signin`, `/join/_signin/code`, `/join/_signout`, `GET /join/_discord/start` and
  `/join/_discord/callback`, and `POST /join/<link>/stop`.
- **Differences from the plan:** Discord visitors on a join page without the bot sign in again
  after 30 minutes (roles can't be re-checked without it); the panel keeps roles for the sign-in
  (12 hours) in that case. Role sign-ins can't change a password. The callback answers with a
  short page that moves on, because the panel's Strict cookie isn't sent on Discord's redirect.
  The sign-in form on the join port checks that the Origin matches the Host.
- **Tests:** `tests/test_discordauth.py` (a Discord stand-in: settings, the flow, routes, join
  pages); `packaging/test/test-join-page.sh` section "1.28" (panel account sign-in on a page,
  See/Start/Stop by account, Stop and Start of a real server, cookie flags, forms from other sites).
  Discord itself can't be reached from the cloud test containers; it was tested on a real Discord
  server separately (see the merge request).
