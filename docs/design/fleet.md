# Design: RedstonePanel clusters (one master, many nodes)

Status: v2 · Phase 1 shipped in 1.5.0, phase 2 in 1.15.0; phases 3+ are proposals · Author: Quentin Russell

v2 replaces the hub/member draft. The two ideas there that survive: a panel that controls other
panels through tokens, and an allowlist that limits what a token can do. New in v2: one
**master** per cluster, nodes join with a **join code**, a **cluster certificate authority**
(CA), and **promoting a node to master** from a backup.

## 1. Problem

RedstonePanel manages the Minecraft servers on the one Ubuntu host it is installed on. Someone
with several hosts has to open each panel separately, sign in to each, and remember which server
lives where. Omni Server Manager tried to fix this with a Windows desktop app that drove each host
over SSH. That needs SSH keys and sudoers entries on every host, does nothing while the desktop
is off, and duplicates logic RedstonePanel already has.

## 2. Goals and non-goals

**Goals**

- One **cluster dashboard** on the master: every server on every node, with start, stop, restart
  and (phase 2) full management.
- Every node keeps working on its own. Servers keep running and the node's **local dashboard**
  still manages its own servers when the master is down.
- Adding a host is one command (or one paste) on that host. No SSH needed.
- A compromised master can control game servers but can never change a node's users, settings,
  HTTPS or sign-in mode, or delete a server.
- Cluster traffic is always HTTPS, checked against the cluster CA. No certificate pinning that
  breaks on renewal.
- The same package on every host. Roles are interchangeable: any node can be promoted to master.
- No new hard dependencies. Python stdlib plus the `openssl` binary the package already depends on.

**Non-goals**

- **Several masters, or every node serving the cluster dashboard.** See §11.
- **A shared IP or load balancer for the dashboard.** Point a DNS name (e.g. `panel.lan`) at
  the master and move it when you promote another node. Players are unaffected either way:
  Minecraft clients connect straight to `host:port`.
- Signing in once for all panels (SSO). A node's local dashboard uses that node's own accounts.
- A node belonging to two clusters, or clusters of clusters.

## 3. Roles

Each panel is in exactly one role, stored in `data/cluster/cluster.json`:

| Role | What it does |
| --- | --- |
| **standalone** | Today's behaviour (the default). |
| **master** | Holds the cluster CA, the node list and each node's token. Serves the cluster dashboard. Also runs its own local servers, shown as the first node. |
| **node** | Runs its own servers. Accepts commands from whoever holds its node token, within the allowlist. Never calls the master after joining. |

```
 Browser ──sign-in cookie──▶ Master ──HTTPS + node token──▶ Node A ─▶ its servers
                              │  (cluster CA)          └──▶ Node B ─▶ its servers
                              └─▶ its own servers
 Browser ──sign-in cookie──▶ Node A  (local dashboard, Node A's servers only)
```

- The browser only talks to the panel it opened. The master calls the nodes server-side. This
  avoids CORS, the `samesite=strict` cookie, mixed content and certificate warnings, and node
  tokens never reach a browser.
- **The master is the only caller.** Nodes don't poll the master or send it heartbeats. A node
  only needs to know two things: the hash of its token, and the cluster epoch (§8).
- **Who can use the cluster dashboard:** the master's own accounts (its panel accounts and
  its Linux sudo/redstonepanel users), exactly as they sign in to the master today. Nodes keep
  their own accounts for their local dashboards. Accounts are never synchronised between hosts.

## 4. Creating a cluster

Master: **Panel Settings → Cluster → Create cluster** (admin), or `sudo redstonepanel cluster create "Home"`.

1. Creates the **cluster CA** in `data/cluster/ca.key` / `ca.crt` (EC P-256, 10 years,
   `openssl` as `core/tls.py` already does for the self-signed CA).
2. Issues the master its own certificate from the cluster CA, with this host's names and
   addresses (`settings.own_hosts()`) as SANs.
3. Switches the master to the new `https_mode: "cluster"` and restarts (the existing
   `_restart_soon()` path). A panel that uses a **custom** certificate (e.g. certbot) can't
   create or join a cluster in phase 1 (§6).
4. Records `{"role": "master", "cluster_id": "<uuid>", "name": "Home", "epoch": 1}`.

Browsers trust the whole cluster once the cluster CA is installed (Panel Settings offers
`cluster-ca.crt` for download, like `ca.crt` today). That replaces trusting each panel's own CA.

## 5. Joining a node (adoption)

### 5.1 Join code

On the master: **Cluster → Add node** creates a **join code**: single-use and valid for 1 hour.
The master stores only its scrypt hash:

```
rpjoin1:<base64url JSON {"u": "https://192.168.1.10:8443", "s": "<join secret>", "ca": "sha256:<cluster CA fingerprint>"}>
```

Alongside it, the UI shows a command to copy for a fresh Ubuntu 24.04/25.04 host:

```bash
curl -fsSL https://qrussell.github.io/qrussell-repo/setup.sh | sudo bash && sudo apt install -y redstonepanel && sudo redstonepanel join 'rpjoin1:...'
```

The `setup.sh` URL is a placeholder: use whatever qrussell-repo documents for adding its apt
source. If it has no one-line setup script yet, adding one belongs to qrussell-repo, not this
package.

On a host that already runs RedstonePanel, an admin can instead paste the code into
**Panel Settings → Cluster → Join a cluster**.

### 5.2 What `join` does

1. **Refuses** unless this panel is standalone. A panel that is already a node or a master
   must `leave` first.
2. Connects to `u`, accepts the master only if its certificate chains to a CA whose SHA-256
   matches `ca`, then saves that CA as `data/cluster/ca.crt`. This is trust on first use, but
   the fingerprint comes from the admin's own copy-paste, not from the network.
3. Generates locally:
   - a **node token**: `rpn_` + 32 random bytes. Only its scrypt hash is kept on the node.
   - a TLS key and a CSR for its names and addresses (`own_hosts()` plus `extra_hosts`).
4. `POST /api/cluster/join` to the master with `{secret, name, token, csr, version, cluster_api}`.
5. The master checks the secret (single-use, not expired, throttled like sign-in) and signs the
   CSR. It **refuses SANs that belong to the master or another node**, so a joining host can't
   get a certificate for someone else's name. It saves the node record and returns
   `{cluster_id, name, epoch, cert}`.
6. The node saves its certificate, sets `https_mode: "cluster"`, records
   `{"role": "node", "cluster_id", "master_name", "epoch"}` and restarts.
7. The master records the node's address as the source IP of the join request (an admin can
   edit it later). Phase 1 doesn't call back right away: the Cluster page shows the node as
   **ok** once it answers there.

The master's address in a join code is the one the admin's browser used if the master's
certificate covers it, otherwise the master's first private IP. Single-label host names like
`basement` are never in cluster certificates (see §6), so the node can always check the master's
certificate including its host name.

The join secret only works during step 4. After that, everything goes master → node.

### 5.3 Leaving

- **From the master:** **Remove node** calls `DELETE /api/cluster/membership` on the node, which
  makes the node delete its token hash and node certificate, return to `standalone` with
  self-signed HTTPS, and restart. The master then forgets the node and its token.
- **If the master can't reach the node:** the master forgets it anyway. The node's admin runs
  `sudo redstonepanel leave`.
- `redstonepanel reset-security` on a node switches to plain HTTP as it does today. That makes the
  node unreachable to the master (shown as **unreachable** with that reason) until
  `sudo redstonepanel cluster-https` turns cluster HTTPS back on.

## 6. Certificates and HTTPS

- New `https_mode: "cluster"`: the panel serves `data/cluster/node.crt` and `node.key`. It is
  validated in `settings.validate()` and started by `run.py` like `self_signed`. If the cert is
  missing or invalid, the existing fallback to HTTP applies, with a warning shown to admins.
- **Master → node checks:** an `ssl` context with `cafile=data/cluster/ca.crt` and
  `check_hostname=True` against the node address on record. That address is in the SANs because
  the master signed them. No pinning, so renewing a certificate breaks nothing.
- **Renewal:** node certificates last 825 days. 30 days before expiry, the master calls
  `POST /api/cluster/cert/csr` on the node (which returns a fresh CSR), signs it, and calls
  `PUT /api/cluster/cert`. The node swaps the files and restarts. Phase 1 does this from a
  **Renew** button on the master. Phase 2 does it automatically.
- **Name constraints:** the cluster CA can only vouch for private IPv4 addresses (10/8,
  172.16/12, 192.168/16, 100.64/10 for Tailscale-style addresses) and names under `.lan`,
  `.local`, `.home.arpa` and `.internal`. Importing it into a browser therefore can't be used to
  impersonate a public website, even if the master's CA key is stolen. The same lists decide which
  of a host's names go into its certificate, and a node must reach the master from a private address.
  A node on a public address needs the custom-certificate support below.
- **Clock differences:** certificates start at the moment they are signed (OpenSSL 3.0 on
  Ubuntu 24.04 can't backdate them), so the panel's own checks verify 10 minutes ahead to
  tolerate a signer whose clock runs a little fast.
- **Custom certificates (certbot), phase 2:** in phase 1 a panel with `https_mode: "custom"`
  can't create or join a cluster. The plan: such a panel keeps its certificate for browsers *and*
  for cluster traffic, its node record gets `verify: "system"`, and the master checks it with
  `ssl.create_default_context()` instead of the cluster CA. A second TLS port only for the cluster
  isn't worth it.
- **Cluster traffic never uses plain HTTP.** Joining always turns HTTPS on.

## 7. Node tokens and the allowlist (node side)

### Storage: `data/cluster/cluster.json` (mode 0600)

```json
{"role": "node", "cluster_id": "5f0c...", "cluster_name": "Home", "master_name": "Main panel",
 "epoch": 1, "token_hash": "scrypt$...", "joined": 1763300000,
 "last_used": 1763301234, "last_ip": "192.168.1.10"}
```

A node holds one token: the master's. Verifying it takes one scrypt call (about 50 ms). After a
successful check, the result is cached in memory for 60 s, keyed by the token's SHA-256, so the
overview stays quick.

### Middleware change (`main.py access_control`)

Before the cookie check:

```python
auth_header = request.headers.get("authorization", "")
if auth_header.startswith("Bearer rpn_"):
    user = cluster.read_node_token(auth_header[7:], request.headers.get("x-redstonepanel-epoch"), ip)
    if not user:                                  # wrong token, not a node, or stale epoch
        return JSONResponse({"detail": "Not this cluster's master."}, status_code=401)
    if not cluster.path_allowed(request.method, path):
        return JSONResponse({"detail": "Not allowed for the cluster master."}, status_code=403)
```

- Identity: `{"username": "master:<master name>", "source": "cluster", "role": "user"}`.
- Bearer requests carry no `Origin`, so `same_origin()` returns `None` and the CSRF check
  passes, as it does for curl today. The token is never a cookie, so a malicious website can't
  use it.
- `trusted_network_user()` is never consulted for a Bearer request.
- Failed token checks count toward the existing per-IP sign-in throttle in `core/auth.py`.

### What the master's token can reach (allowlist)

| Method | Path | Phase |
| --- | --- | --- |
| GET | `/api/cluster/hello` | 1 |
| DELETE | `/api/cluster/membership` | 1 |
| POST, PUT | `/api/cluster/cert/csr`, `/api/cluster/cert` | 1 |
| GET | `/api/servers`, `/api/logs`, `/api/templates` | 1 |
| POST | `/api/servers/{name}/action` | 1 |
| GET, POST | `/api/servers/{name}/properties`, `.../properties/raw`, `.../permissions/raw` | 2 |
| GET, POST | `/api/servers/{name}/players`, `.../players/role`, `.../chat` | 2 |
| POST | `/api/servers/{name}/update`, `/api/servers/deploy`, `/api/cluster/templates` | 2 |
| GET | `/api/servers/{name}/export`, `.../worlds/export` | 3 |
| POST | `/api/servers/import` | 3 |

**Never** reachable with a node token: `/api/users*`, `/api/settings*`, `/api/auth/*`,
`DELETE /api/servers/{name}` and every master-side `/api/cluster/*` route. A test checks every
route in `app.routes` against this list, so a new route stays off it until someone deliberately
adds it.

### `GET /api/cluster/hello`

```json
{"name": "Basement Survival", "version": "1.5.0", "cluster_api": 1, "platform": "linux",
 "cert_expires": 1834000000}
```

`cluster_api` is the contract version. It is bumped only when the allowlist or a response the
master relies on changes. The master shows **update RedstonePanel on this node** for nodes
below the version it needs. `platform` exists for the Windows track (§12).

### Activity log

State-changing requests from the master are logged on the node, for example:
`[CLUSTER] Main panel (192.168.1.10) as quentin: POST /api/servers/minecraft-java-1/action stop`.
The master sends `X-RedstonePanel-Actor: <username>` so the node can name the person who
clicked. The header is informational only.

## 8. Master side

### Storage: `data/cluster/` (directory 0700, files 0600)

| File | Contents |
| --- | --- |
| `cluster.json` | `{role: "master", cluster_id, name, epoch}` |
| `ca.key`, `ca.crt` | Cluster CA |
| `node.key`, `node.crt` | The master's own cluster certificate |
| `nodes.json` | `[{id, name, url, token, verify: "cluster"\|"system", cert_expires, platform, added, added_by, last_ok}]` |
| `joins.json` | Pending join codes: `[{id, secret_hash, expires, created_by}]` |

`nodes.json` holds live tokens and `ca.key` can issue certificates. Together they are the cluster's
keys, so no API ever returns them.

### Cluster epoch (no split-brain after a promotion)

`epoch` starts at 1. The master sends `X-RedstonePanel-Epoch: <n>` on every request. A node
remembers the highest epoch it has seen and **refuses lower ones**. Promoting a node (§9) bumps
the epoch. The first time the new master contacts each node, the node moves to the new epoch.
From then on, an old master that comes back online gets a 401 ("Not this cluster's master") and
shows **This panel is no longer the master** instead of controlling anything.

### Master API

| Method | Path | Who |
| --- | --- | --- |
| POST | `/api/cluster/join` | Anyone with a valid join secret (public route, throttled) |
| GET | `/api/cluster/overview` | Signed-in users, not trusted-LAN visitors |
| POST | `/api/cluster/nodes/{id}/servers/{name}/action` | Signed-in users, not trusted-LAN visitors |
| GET | `/api/settings/cluster` | Admin: role, nodes, pending join codes, CA info |
| POST | `/api/settings/cluster` | Admin: create the cluster |
| POST | `/api/settings/cluster/joins` → `{join_code, command}` | Admin |
| DELETE | `/api/settings/cluster/joins/{id}` | Admin |
| PATCH, DELETE | `/api/settings/cluster/nodes/{id}` | Admin: edit address or name, renew certificate, remove |
| GET | `/api/settings/cluster/backup` | Admin (§9) |

Admin routes sit under `/api/settings/`, so the existing `ADMIN_API` prefix check covers them.

**Trusted-LAN visitors get no cluster access.** Otherwise, turning on trusted-LAN sign-in on the
master would let anyone on its Wi-Fi control nodes that require sign-in.

### HTTP client (`core/cluster_client.py`, stdlib only)

`http.client.HTTPSConnection` with the context from §6. Timeout 4 s, 1 MB response cap (phases
1–2). Calls go out in parallel through a `ThreadPoolExecutor` (8 workers).

### `GET /api/cluster/overview`

Calls `/api/servers` on every node and caches each node's result for 10 s, because
`manager.list_servers()` runs several `systemctl` calls per server. The master's own servers come
straight from `manager.list_servers()`:

```json
{"cluster": "Home", "nodes": [
  {"id": "local", "name": "Main panel", "state": "ok", "servers": [...]},
  {"id": "n-7f3a", "name": "Basement Survival", "url": "https://192.168.1.20:8443",
   "platform": "linux", "state": "ok", "cert_expires": 1834000000, "servers": [...]},
  {"id": "n-91c0", "name": "VPS", "state": "unreachable", "error": "timed out", "servers": []}
]}
```

`state` is one of `ok`, `unreachable`, `rejected` (401: token revoked, node left, or a newer
epoch), `cert_error`, `too_old`, `error`.

## 9. Backup and promotion

The master's `data/cluster/` directory, its `users.json` and its `templates.json` make up the
whole cluster state.

- **Backup:** **Cluster → Download backup**, or `sudo redstonepanel cluster backup FILE`,
  produces a tar archive encrypted with a passphrase (`openssl enc -aes-256-cbc -pbkdf2`). The
  master also writes an encrypted copy to `data/cluster/backups/` every night, keeping 7. That
  only helps if the disk survives, so the UI reminds the admin to download a copy somewhere
  else after they add or remove a node.
- **Promote:** on the new master (a node of this cluster, or a fresh standalone host):
  `sudo redstonepanel cluster promote FILE`. This:
  1. Decrypts the backup and restores `data/cluster/`, the users and the templates.
  2. Sets `role: "master"` and `epoch += 1`.
  3. If this host was a node, adds itself to the node list as `local`, and drops its own node
     token.
  4. Issues itself a certificate with this host's names and restarts.

  Nodes need no changes. The new master holds the same tokens and the same CA, and its
  higher epoch locks out the old master.
- Promotion is **manual** on purpose. Automatic failover needs the nodes to agree on who is
  master, which is exactly the multi-master problem §11 avoids.

## 10. UI

- **Sidebar:**
  - On a master: **Cluster**.
  - On a node: a badge, e.g. "Node of Home · managed by Main panel".
  - On a standalone panel: nothing new.
- **Cluster page (master):** one card per node, with the master first. Each card has server rows
  (name, edition, version, status, port, start/stop/restart) and **Open dashboard ↗**, which
  links to the node's local UI. Unhealthy nodes show their state and the fix. The page refreshes
  every 15 s while visible.
- **Panel Settings → Cluster:**
  - Standalone: **Create cluster**, **Join a cluster**.
  - Master: nodes (address, platform, version, certificate expiry, Renew, Remove), **Add node**
    (join code and install command, shown once), pending codes, **Download backup**, and
    **Download cluster CA**.
  - Node: cluster name, master name, last contact time and IP, and **Leave cluster**.
- `static/js/app.js` calls `fetch('/api/...')` directly in about 35 places. Phase 1 leaves those
  alone. Phase 2 starts by replacing them with one `api(path, opts)` helper that takes a base
  prefix, so the existing server screens work against
  `/api/cluster/nodes/{id}/proxy/...` unchanged.

## 11. Decision record: why one master

Every node serving the cluster dashboard behind one shared IP was considered and rejected
for now:

- **Accounts would have to be replicated.** `users.json` would need copying between nodes and a
  rule for conflicting edits made while nodes are cut off from each other (split-brain).
  Linux/PAM accounts can't be shared at all, because `read_session_token()` checks group
  membership on the local host.
- **Sessions would need one `secret.key` on every node,** and every node would need a
  certificate for the shared name.
- **A shared IP needs keepalived/VRRP (Linux only, which rules out Windows nodes) or an external
  reverse proxy** (a new single point of failure, and the panel deliberately switches off
  trusted-LAN sign-in for proxied requests).
- **The gain is small.** Games keep running when the master is down, every node keeps its local
  dashboard, and promotion (§9) takes one command.

If this changes, the cluster CA, the tokens and the epoch are the building blocks a
multi-master design would need anyway.

## 12. Windows track (separate from the phases)

The cluster protocol is HTTPS + JSON + bearer tokens with nothing Linux-specific, so a Windows
RedstonePanel can be a node or a master if it passes the same contract tests. The work is in
`core/manager.py`, which is built on systemd, `screen`, ufw, `/opt` and `apt`:

1. **Platform layer:** move service control, firewall, paths, user accounts and the Bedrock
   console behind a `platform` interface, with `linux` as the first backend. This is worth
   doing even without Windows, because it makes `manager.py` testable.
2. **Contract tests** for `cluster_api` that any node must pass.
3. **Windows node:**
   - Windows services through WinSW
   - Bedrock console through stdin owned by the panel
   - `New-NetFirewallRule` for the firewall
   - `ProgramData` paths with ACLs
   - panel accounts only (no PAM)
   - an installer built with PyInstaller in the existing Windows release pipeline
4. **Windows master:** mostly the same code once step 3 works.

The Docker image covers Windows users in the meantime.

## 13. Phases

**Phase 1 (1.5.0): a cluster you can see and switch on and off** (implemented: `core/cluster.py`,
`core/cluster_client.py`, tests in `tests/test_cluster.py` and `packaging/test/test-cluster.sh`)
- Roles, `https_mode: "cluster"`, cluster CA with name constraints, join codes,
  `redstonepanel join` and `leave`
- Node tokens, middleware allowlist, epoch, `/api/cluster/hello`
- Cluster page with start/stop/restart, Panel Settings → Cluster
- Manual certificate renewal, encrypted backup download, dissolving an empty cluster
- CLI: `cluster create | status | join-code | backup | promote`, `join`, `leave`, `cluster-https`
- Not in phase 1: custom (certbot) certificates in a cluster, nodes on public addresses,
  nightly backups, the master's "Add node" install one-liner (waits on a qrussell-repo setup script)
- Also fixed on the way: `POST /api/servers/{name}/action` only accepts start/stop/restart for the
  panel's own `minecraft*` units. Before, any signed-in user could pass any systemctl verb and unit.

**Host numbers on the cluster page (roadmap 1.11)**
- Each node card shows the node's CPU, memory, disk, version and servers online, from one more
  read-only route on the allowlist, `GET /api/host`. It reveals nothing a node's own signed-in
  users can't see and changes nothing.

**Phase 2: manage everything from the master** (1.15.0, implemented: the proxy in `main.py`
and `core/cluster_client.py`, tests in `tests/test_cluster_proxy.py` and
`packaging/test/test-cluster-phase2.sh`)
- As built: the node allowlist gives the master every route of one server
  (`/api/servers/minecraft*/...`), deploy, import, the removed-server list and restore,
  `POST /api/cluster/templates` and the Java runtime routes. `DELETE /api/servers/{name}` is
  allowed, but a node refuses `keep_world=false` from the master, so nothing is erased. The master
  sends `X-RedstonePanel-Role` with the user's role on the master; the node gives a master admin
  the admin role and anyone else the user role. The proxy (`proxy_allowed`) never passes on
  `/api/cluster/*`. Cluster contract (`cluster_api`) 2; masters still let contract-1 nodes join.
- Custom certificates: a master keeps `https_mode: "custom"` in a cluster; a custom-certificate
  node joins with `verify: "system"` and its public name, needs no cluster certificate and may
  join from a public address. Nightly backups use a passphrase stored in
  `data/cluster/nightly.json` (0600).
- `api()` helper in `app.js`, plus a proxy `/api/cluster/nodes/{id}/proxy/{path}` limited to
  the phase-2 allowlist
- Properties, permissions, players, chat and server updates on any node
- Deploy a new server on any node with a master template (the master pushes the template to
  the node, then deploys)
- Automatic certificate renewal and nightly backups

**Phase 3: move things between nodes** (roadmap 1.16; moving and copying servers and templates
implemented in 1.16.0: `core/transfer.py`, `core/moves.py`, [move.md](move.md), tests in
`tests/test_move.py` and `packaging/test/test-move.sh`; LAN discovery not yet)
- Copy templates between nodes, and move or copy a server (export on A streamed into import on
  B through the master, no 1 MB cap)
- LAN discovery: find standalone panels via `GET /api/panel-info` on 8080/8443 and offer
  **Add node**. Optionally list other Minecraft servers read-only by pinging 25565/TCP and
  19132/UDP (port the pinger from Omni Server Manager's `network_scanner.py`).

**Phase 4: adoption over SSH**
- **Add node → Install over SSH:** the admin gives a host, user and password or key once. The
  master shows the host-key fingerprint to confirm, checks the host runs Ubuntu 24.04/25.04,
  runs the install command from §5.1, then **discards the credentials**. Uses `python3-paramiko`
  as a *Recommends* dependency, so the feature is hidden when paramiko isn't installed.

## 14. Security summary

| Threat | Mitigation |
| --- | --- |
| Master compromised | Allowlist: no users, settings, HTTPS, sign-in or delete on nodes. Each node can `leave`. Promote a clean master from a backup (the epoch locks out the old one). |
| Join code leaked | Single-use, expires in 1 hour, can be cancelled. It only lets a host join, which gives that host no control over other nodes. |
| Joining host claims another host's name | The master refuses SANs that belong to the master or an existing node. |
| Backup stolen | Encrypted with a passphrase. Contains tokens and the CA key, so treat it like a password vault. |
| Old master comes back after promotion | Epoch check on every request. |
| Man in the middle | All cluster traffic is HTTPS, checked against the cluster CA (or system CAs for custom certificates). The first join is anchored by the CA fingerprint in the pasted code. |
| Trusted-LAN visitor on the master reaches nodes | Cluster routes refuse `source == "trusted"`. |
| Malicious website uses the token | The token is a header the master adds server-side. Browsers never hold it. |
| Token brute force | 256-bit tokens. Failures count toward the existing per-IP throttle. |

## 15. Testing

- **Unit (`tests/test_cluster.py`):**
  - join-code encode/decode and rejecting bad codes
  - token verify and cache
  - epoch: accept equal or higher, refuse lower
  - `path_allowed()` checked against every route in `app.routes`
  - trusted-LAN visitors refused on cluster routes
  - SAN-collision refusal
  - backup encrypt → promote round trip on temporary data directories (`RP_DATA_DIR`)
- **Integration (`packaging/test`):** three containers from the existing Dockerfile.
  1. Create the cluster on A and join B and C.
  2. Check the overview lists all three, and stop a server on C from A.
  3. Remove B and check it is standalone again.
  4. Promote C from A's backup, then check A gets `rejected` from B.
  5. Renew B's certificate.

## 16. Open questions

1. Should the master be able to give some cluster-dashboard users **read-only** access (see
   everything, start/stop nothing)? That needs a third role on the master, not on the nodes.
2. Should the overview show **player counts**? `list_servers()` doesn't return them today, and
   querying each server makes every refresh slower.
3. Should **Open dashboard ↗** eventually sign the user in on the node automatically (a short,
   one-time sign-in link the node issues to the master)? That edges towards SSO.
4. When the master is unreachable, should a node's local dashboard show a banner (for example
   "Master last seen 3 days ago")? Nodes don't call the master today, so this would need the
   node to track when the master last contacted it, which is cheap since it's already logged.
