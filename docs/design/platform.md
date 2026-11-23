# The platform layer (`core/platform`)

Status: built in 1.21 · Linux side only · Author: Quentin Russell

RedstonePanel was written for Ubuntu, and until 1.21 it showed: `systemctl`, `ufw`, `screen`,
`apt` and `openssl` were run from more than two dozen files, and `/opt`, `/etc/systemd/system` and friends were
typed out wherever they were needed. A Windows version (roadmap 1.21 to 1.24) can't be built on
that, so 1.21 puts everything the panel asks of the operating system behind one package.
Nothing a person using the panel can see changes in 1.21.

## What it is

```
core/platform/
    __init__.py     the names the rest of the panel imports, and use("linux" | "fake")
    paths.py        where things live: the one module that spells out system paths
    linux/          systemd, ufw, screen, apt and dpkg, openssl
    fake/           a pretend system for tests: runs nothing, remembers everything
```

```python
from core.platform import services, firewall, console, packages, system, certs, paths

services.stop("minecraft-java")              # systemctl stop minecraft-java.service
firewall.allow(25565, "tcp")                 # ufw allow 25565/tcp
console.send("minecraft-bedrock", "save hold")
os.path.join(paths.SERVERS_ROOT, name)       # /opt/<name>
```

| Area | What it is for | Linux |
| --- | --- | --- |
| `services` | Game server and panel services, schedules, their logs, limits on a running server, work that must outlive the panel | `systemctl`, `systemd-run`, `journalctl` |
| `firewall` | Opening and closing ports | `ufw` (optional) |
| `console` | Typing into a server that has no RCON (Bedrock) | `screen` as the `minecraft` account |
| `packages` | Java versions on offer and installed, the panel's own package | `apt-get`, `apt-cache`, `dpkg`, `dpkg-query` |
| `system` | This machine: addresses, the `minecraft` account, file ownership | `hostname`, `ip`, `useradd`, `chown`, `chmod`, `rm`, `tail` |
| `certs` | Making and checking certificates | `openssl` |
| `paths` | Folders and files | `/opt`, `/var/lib/redstonepanel`, `/etc/systemd/system`, `/usr/lib/jvm`, `/proc`, ... |

Each name imported from `core.platform` is a small stand-in that passes the call to the system
in use, so a test can switch systems after the panel's modules are imported.

## The rules

1. **Code outside `core/platform` never starts a system program.** Add a function to the area
   it belongs to, on every side, and call that.
2. **Code outside `core/platform` never spells out a system path.** Take it from `paths`.
   Feature modules keep their own constants (`backups.BACKUP_ROOT`, `manager.SERVERS_ROOT`,
   `UNIT_DIR`) because tests point those at temp folders; they get their values from `paths`.
3. **Every side has the same functions with the same arguments.**
4. **Catch `CommandError`** (`from core.platform import CommandError`) where a missing or slow
   program must not break the page.

`tests/test_platform.py` enforces 1 to 3 by reading the source, so a new `systemctl` call or a
new `/opt` in a feature fails CI with the file and line.

Two files outside the layer still start programs, on purpose, and the test names them:
`core/manager.py` (`run_and_log`, the one place that copies a command's output to the activity
log, and Bedrock's own program started to look up an XUID) and `core/backup_targets.py`
(`ssh`, `ssh-keygen` and `rsync` for off-site backups; OpenSSH ships with Windows too, and
`rsync` gives way to `sftp` there in 1.23).

## Adding a call

Say a feature needs to know whether a service starts at boot:

1. `core/platform/linux/services.py`: add `starts_at_boot(server)`, running `systemctl
   is-enabled`. Name it for what the panel wants, not for the program.
2. `core/platform/fake/services.py`: the same function, answering from `fake.state`.
3. Call `services.starts_at_boot(name)` from the feature.

Changes that belong in the activity log (start, stop, restart, enable, disable, firewall
rules, ownership) go through `manager.run_and_log`, as before; the layer looks it up when
called, so tests that replace `run_and_log` still see every command.

## Testing against the fake

```python
from core import platform
from core.platform import fake

platform.use("fake"); fake.reset()
fake.state.servers["minecraft-java"] = {"state": "active", "exec_start": "/usr/bin/java -jar server.jar"}
manager.control_server("minecraft-java", "stop")
fake.calls            # [("services.stop", "minecraft-java")]
platform.use()        # back to the real system
```

The tests written before 1.21 are unchanged: they replace `subprocess.run` or
`manager.run_and_log` and check the command lines, and the Linux side runs the same command
lines it always did. New tests should use the fake.

## What 1.21 did not move

These are still Linux-shaped in the feature modules. Each is small next to what moved, and
each needs its Windows shape before it can be given a good function name, so they belong to
1.22:

- **The text of unit files.** `manager._write_unit`, the backup, restart and update-check
  timers, Phantom's and SFTP's units and the limits drop-in are still written as systemd text
  by their features (into `paths.UNIT_DIR`). On Windows these become "register a service" and
  "register a scheduled task".
- **Reading the machine without a program:** `/proc` and cgroup files (`host.py`, `perf.py`),
  `pwd`, `grp` and `os.chown` (`files.py`), `fcntl` locks, PAM (`auth.py`).
- **Wording.** Messages that quote a command to type (`sudo apt install ...`,
  `sudo ufw allow ...`) still quote the Linux one.
- **Which Java to offer.** The versions come from apt through `packages`; the choosing logic
  in `javaservers.py` and `runtimes.py` still thinks in `openjdk-N-jre-headless`.

## As built (1.21)

- 29 files changed outside the layer; about 180 program calls moved, including the loader
  installer and modpack calls 1.20 added (the guard test found those on the rebase). `grep`, `find` and the
  `systemctl ... | cut` fallback became Python. The other programs are run with the same
  arguments as before, including the `sudo` in front of the calls that have always had it.
- Tested: the 784 unit tests from before pass unchanged, plus 20 new ones in
  `tests/test_platform.py`. See the merge request for the install and feature test results.
