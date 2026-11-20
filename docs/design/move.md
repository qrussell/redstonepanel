# Design: move or copy a server between cluster members (1.16)

Roadmap item 12, fleet.md phase 3. Builds on 1.15 (the master manages every node) and 1.6
(removed servers you can restore).

## 1. What the admin sees

On the cluster page, each server gets **Move…** and **Copy…** (master admins only). The dialog
asks where to (any member that answers and runs 1.16 or later, the master included) and, for a
copy, whether to start it. A progress line follows the steps; the result says the new name and
port, and whether either changed.

After a move, the source's files stay as a removed server marked "moved to <member>". The
dialog, and that removed server's row, offer **Delete the old copy**. Until then it can be
restored like any removed server.

Templates get **Copy to…** in the template manager: from the master to a node (as in 1.15),
or from a node to the master or another node.

## 2. Steps of a move (the master runs them, in a background job)

| # | Where | What | If it fails |
| --- | --- | --- | --- |
| 1 | master | Both members answer and run cluster contract 3 | stop, nothing changed |
| 2 | A | **Export:** stop the server and mask its unit for this boot (no timer or restart can start it), then write the whole folder to a `.tar.gz` and hash it | A unmasks and starts it again if it was running |
| 3 | B | **Receive:** check free disk, then take the archive streamed from A through the master, checking its length and SHA-256 | B deletes what it received; A as above |
| 4 | B | **Install:** unpack into `/opt`, keep the name and ports if they're free (else pick others and say so), install the Java it needs, write the unit, carry over memory, backup and restart schedules and Phantom, start it and check it stays running | B removes what it installed; A as above |
| 5 | A | **Finish:** unmask and take it off the panel, keeping the files (a removed server, `moved_to` noted) | the job reports it: B already runs the server, so B is kept and A's stopped server is left for the admin |
| 6 | both | Delete the temporary archives | logged only |

Nothing is deleted on A before B is up. The old copy is deleted only when the admin asks.

A **copy** is the same without stopping A: the export uses a live snapshot (`save-off` and
`save-all flush` over RCON, or Bedrock's `save hold`, as backups do). Step 5 is skipped, and
schedules and Phantom aren't carried over (a copy is usually a test world). Copying to the
same member always gets a new name.

## 3. Node routes (cluster contract 3)

All under `/api/cluster/transfers`: cluster plumbing, reachable only with the master's node
token (`NODE_ONLY_API`), never through the master's proxy. A master that is itself A or B
calls `core/transfer.py` directly instead.

| Method | Path | Does |
| --- | --- | --- |
| POST | `/api/cluster/transfers/export` | `{server, stop}` → `{id, size, sha256, was_running, meta}` |
| GET | `/api/cluster/transfers/{id}/archive` | the export's archive |
| POST | `/api/cluster/transfers/{id}/finish` | export: `{outcome: moved\|copied\|failed, moved_to}` |
| POST | `/api/cluster/transfers/import` | `{size, sha256, meta}` → `{id}`; refuses if the disk is too full |
| PUT | `/api/cluster/transfers/{id}/archive` | the archive (streamed, checked against size and SHA-256) |
| POST | `/api/cluster/transfers/{id}/install` | `{keep_name, start, carry}` → `{name, port, old_port, renamed}` |
| POST | `/api/cluster/transfers/{id}/confirm` | import: keep what was installed |
| DELETE | `/api/cluster/transfers/{id}` | either: undo it (an unconfirmed import removes its server and folder) |
| DELETE | `/api/cluster/transfers/moved/{name}` | delete a removed server, only if a move left it |

The last route is the only way the master can delete files on a node, and only folders that a
move left behind (`.redstonepanel-removed.json` with `moved_to`). Every other removed server
still needs an admin on that node.

Records live in `DATA_DIR/transfers/<id>.json` (0600) with the archive next to them. Records
older than a day are cleaned up when a new transfer starts (an export's server is unmasked).

`meta`, sent from A to B by the master: `edition`, `xms`/`xmx`, `backup` and `restart`
schedules and `phantom` (installed or not). B validates every field; the folder itself is
unpacked with the same safe extraction as imports.

## 4. Streaming

The master opens A's archive and sends it on to B as it reads, with `Content-Length` set, in
64 KB chunks, counting bytes for the progress line. There is no 1 MB cap and no copy on the
master's disk. The SHA-256 from A's export is checked by B after the last byte. Timeouts are
per read (15 minutes), not per transfer.

## 5. What doesn't move

- Backups stay in `/var/backups/redstonepanel/<server>` on A (B starts its own).
- The player history (1.14.2) stays on A.
- Phantom units made by hand.

## 6. Later (1.16.x)

- **Pre-copy for big worlds:** copy while the server runs, then only what changed after it
  stops. Needs a file list with sizes and times from A and a second, partial archive.
- Moving several servers at once.
