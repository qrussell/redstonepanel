# RedstonePanel on Windows

Status: built; 1.22 Java servers on one computer (preview), 1.23 the whole single machine, 1.24 mixed clusters and SFTP · Author: Quentin Russell

The plan and the list of what maps to what are in the [roadmap](roadmap.md) under "1.21 to
1.24". This page says how the Windows side is built, for whoever changes it next.

## The one decision everything follows from

**Windows keeps the same unit files.** A game server on Windows is still described by
`minecraft-<name>.service`, a schedule by a `.timer`, and the crash hook by the drop-in
`minecraft-.service.d/redstonepanel.conf`. The files are the ones the Linux panel writes, with
Windows paths in them, kept in `C:\ProgramData\RedstonePanel\services`.

What reads them is ours: `core/platform/windows/runner.py`, a short Python program (standard
library and `ctypes`) that is "systemd for one unit". The Windows side of the platform layer
then answers the panel in the words it already understands: `services.show_many` returns
`systemctl show` blocks, the firewall status reads like `ufw status`, Java versions carry
their apt names, and `system.machine_file("meminfo")` returns `/proc/meminfo` text.

That is why almost no feature code changed in 1.22. Deploy, backups, restore, schedules,
crash handling, the dashboard and the file manager run the code they run on Linux.

## What is where

```
C:\Program Files\RedstonePanel\        the program (replaced by every install)
    python\                            Python 3.13 (embeddable) with FastAPI, uvicorn, cryptography
    app\                               the panel: main.py, run.py, core\, static\
    redstonepanel.cmd                  `redstonepanel status`, `redstonepanel add-user NAME`, ...
    install.json                       {"home": "..."}: where the data is
C:\ProgramData\RedstonePanel\          the home folder (kept by an uninstall), or the folder chosen
    data\  logs\  backups\             as on Linux: /var/lib, /var/log, /var/backups
    servers\minecraft-<name>\          /opt/minecraft-<name>
    services\                          unit files, timers, drop-ins (/etc/systemd/system)
    journal\<unit>\                    output.log, status.json, usage.json, events\ (the journal)
    java\java-21-openjdk-amd64\        Java (/usr/lib/jvm)
    first-sign-in.txt                  the first account's password, until it is deleted
```

The home folder can't have a space in its name: unit files split command lines on spaces.
`core/platform/paths.py` works all of this out (`windows_layout`); `RP_HOME` overrides it.

## The runner

`runner.py service UNIT` is what each Windows service runs. It:

- reads the unit and its drop-ins (`units.py`: sections, `%i`, `Environment=`, `ExecStart=`,
  `Restart=`, `RestartSec=`, `TimeoutStopSec=`);
- starts the program inside a Job Object that kills everything in it when the runner goes, so
  nothing is left behind;
- keeps the program's input open, and listens on a named pipe
  (`\\.\pipe\RedstonePanel.console.<server>`, administrators and the system only) so the panel
  can type a command;
- writes each output line to `journal\<unit>\output.log` with its time and run id (rotated at
  8 MB), and the state to `status.json`;
- stops a server by typing its stop command (`X-RedstonePanel-StopCommand=stop`) and waiting
  `TimeoutStopSec` before ending the job; Windows shutting down does the same;
- starts it again after a crash as `Restart=` and `RestartSec=` say;
- leaves a small file in `journal\<unit>\events\` saying what happened, and writes a "started"
  or "stopped" entry to the Application event log (source RedstonePanel). The entry starts
  the task `\RedstonePanel\notifications`, which reads the new files and runs
  `redstonepanel notify-unit`, which is what `ExecStopPost` does on Linux. Webhooks and crash
  explanations work while the panel is stopped. The entry carries nothing the task trusts:
  anyone signed in can write one, but only a server's own account can write its folder.

The panel itself is the service `redstonepanel`, run by the same runner from
`redstonepanel.service` (`Restart=always`).

## Accounts and rights

- The panel runs as the system account, as it runs as root on Linux.
- Each game server runs as its own virtual account, `NT SERVICE\minecraft-<name>`, which
  Windows makes with the service. It can change its own server folder and write its own
  journal, and read `services\` and `java\`. It can't read the panel's data, other servers or
  backups. This is stricter than Linux, where all servers share the `minecraft` user.
- The home folder is for administrators and the system.
- People sign in with panel accounts or (1.23) Windows accounts. A Windows account may sign in
  when it is in the computer's **Administrators** group (found by its SID, so it works in any
  language) or in a local group named **RedstonePanel**: the two stand for Linux's `sudo` and
  `redstonepanel` groups, and such accounts are panel admins, as Linux ones are. Windows checks
  the password itself (`LogonUser`, a network sign-in, so disabled, locked, expired and
  empty-password accounts are refused; a Windows Hello PIN isn't a password). Microsoft accounts
  sign in with their email or the computer's name for them. Windows names aren't
  case-sensitive and one account has several spellings (`quent`, `.\Quent`, `PC\quent`, the
  email), so the panel keys sessions and two-factor on the one name Windows gives the account's
  SID back (`system.canonical_account`). Group membership is asked again on every request,
  kept for 30 seconds, so leaving the group ends the session within half a minute.

## The other areas

| Area | Windows |
| --- | --- |
| `services` | The service manager through `ctypes` (`_win.py`); schedules are Task Scheduler tasks in `\RedstonePanel\`, written from the timer's `OnCalendar=` and run as the system |
| `firewall` | Defender Firewall rules named `RedstonePanel <port>/<proto>` in the group `RedstonePanel`, through PowerShell's NetSecurity commands |
| `console` | The runner's pipe |
| `packages` | Eclipse Temurin from `api.adoptium.net`, SHA-256 checked, unpacked into `java\` (`javapkg.py` plays `apt-get`) |
| `system` | `GlobalMemoryStatusEx`, `GetSystemTimes`, `GetIfTable2`, `icacls`; `msvcrt` locks |
| `certs` | The `cryptography` library, given the same `openssl` arguments the panel uses on Linux |

**The file manager** keeps its rule of never following a link. On Linux that is
`openat(O_NOFOLLOW)`; on Windows `dirfd.py` gives `core/files.py` the same calls by holding
each folder open (so it can't be swapped for a link underneath), opening links as links, and
checking what was opened is what was asked for. Symbolic links and junctions show as links
and are never entered. Names Windows would read as something else are refused: devices
(`CON`, `NUL`, `COM1`, also as `con.txt`), `name:stream`, and names ending in a dot or space.

### Servers folder

The **Servers folder** setting (1.21.1) works here too: `system.check_servers_folder` takes a
path with a drive letter on a fixed NTFS or ReFS disk (no spaces, no whole drive, no share, not
Windows' or the panel's own folders) and `system.prepare_servers_folder` makes it and leaves a
new or empty folder to administrators and the system, as the installer does for the usual one.
Each server's account still gets only its own folder, when its service is made. `system.servers_drives()` lists the
fixed NTFS and ReFS drives for the dropdown beside the box; picking one fills in
`<drive>\Minecraft\servers` (the usual place on the panel's own drive). Linux returns no list
and keeps the plain box. The install
test sets another folder, makes a server there, checks its rights and removes it.

## 1.23: the whole single machine

- **Hard limits** (`core/platform/windows/jobs.py`). The runner starts the game inside a Job
  Object named `Global\RedstonePanel.job.<server>`, readable and changeable only by the system
  and administrators, and gives it the limits drop-in's `MemoryMax=` as a job memory limit and
  `CPUQuota=` as a processor hard cap (`CpuRate`, hundredths of a percent of the whole
  machine). The panel changes a running server's limits by opening the job by name, as
  `systemctl set-property --runtime` does. Windows limits *committed* memory: it refuses the
  game more, so Java reports it ran out of memory and stops, where Linux kills it. The runner
  notices the job's peak at the limit, logs it, counts it for the Performance card and records
  the stop as `oom-kill`, so the crash explanation says it reached its hard memory limit.
- **Crash causes on Windows** (`core/crashes.py`): a port Windows reserved (Hyper-V, WSL,
  Docker Desktop), a file locked by another program (antivirus, OneDrive), a file the server's
  account may not change, and the exit statuses for a missing Visual C++ runtime and a crashed
  program.
- **Bedrock** runs `bedrock_server.exe` from the download-links API's `serverBedrockWindows`,
  with the same auto-update. Its console is the runner's pipe, and the runner copies what it
  prints to `server.log` in its folder, as `screen` does on Linux. **Phantom** is the pinned
  `phantom-windows.exe` (SHA-256 checked) as its own service and virtual account.
- **Quilt, Forge and NeoForge installers and modpacks.** Windows only starts a program as a
  virtual account through that account's service, so `services.run_as_server` points the
  stopped server's service at `runner.py service UNIT --task <job>` for one run (made for the
  occasion when the server is new), starts it, waits, and puts the service back. The installer
  then runs as `NT SERVICE\minecraft-<name>`, confined to the server's folder, as it runs as
  `minecraft` on Linux. Copies the panel makes into a server folder (as the system) refuse
  links and junctions.
- **Off-site backups.** S3 as on Linux. SSH storage uses Windows' own OpenSSH client
  (Settings → System → Optional features → OpenSSH Client) and `sftp` in batch mode instead of
  `rsync`, resuming a cut-off upload with `reput`.
- **Updating from the panel** (`core/platform/windows/panelpkg.py`). **Check for updates**
  asks GitHub for the newest release (and the panel asks once a day by itself). **Update now**
  downloads its `RedstonePanel-x.y.z-x64.msi` from GitHub over HTTPS, refuses it unless its
  SHA-256 matches the release's `.sha256` (and GitHub's own digest of the file, when it gives
  one), and starts a PowerShell process of its own that runs `msiexec /qn`, waits, starts the
  panel and the game servers that were running before (also when the installer failed), and
  writes the installer's exit code. Nothing runs from the panel's folder while the installer
  replaces it. The new panel reads the code back; a failure names `installer.log` in
  `<data>\upgrade\`. Unlike Linux, game servers stop for about a minute, because they run from
  the panel's own Python.
- **Needs attention about the PC itself** (`system.machine_notes`, every 2 minutes): the PC is
  set to sleep when plugged in (`powercfg`), or Windows Update is waiting to restart it.
  **Reserved ports** (`system.reserved_ports`, `netsh int ipv4 show excludedportrange`, every
  5 minutes): a server whose port falls in a range Hyper-V, WSL or Docker Desktop took is an
  error in Needs attention and under **Fix these first** on the Port forwarding page, which
  also lists the ranges.

## 1.24: clusters and SFTP

Nothing is held back any more: `WINDOWS_NOT_YET` and the page's `not_yet` list are gone, and so
is the word "preview". Installing a new version still stops the game servers while the files
are replaced and starts them again afterwards.

- **Clusters.** A Windows panel is a master or a node like a Linux one; members say which
  system they run (`platform` in the hello and the join request, `CLUSTER_API` 9). The cluster
  code is unchanged: it spells its certificate work as openssl command lines, and `certs.py`
  does those with the `cryptography` library. 1.24 added the ones clusters use: `req -verify`
  (a node's request is signed by its key), `x509 -ext subjectAltName`, `verify -attime` (with
  not-yet-valid), and `enc -aes-256-cbc -pbkdf2` for the encrypted cluster backup, written in
  OpenSSL's own format (`Salted__`, the salt, AES-256-CBC with key and IV from PBKDF2-HMAC-SHA256),
  so a backup made on one system opens on the other. Unit tests run these against the real
  `openssl`: a CA with name constraints made the Windows way is accepted by openssl and
  Python's `ssl` (which Linux members use), and the other way round.
- **File names Windows can't hold** (`core/winnames.py`, shared with the file manager's
  `dirfd.py`): devices (`CON`, `NUL`, `COM1`, also as `con.txt`), `< > : " / \ | ? *` and control
  characters, a trailing dot or space, and names that differ only by case. A move or copy to a
  Windows member asks the source to check its folder before it stops the server
  (`transfer.export(for_windows=True)`) and fails with the list; a node before 1.24 ignores the
  field, and the Windows target's own check of the archive then refuses it. A Windows panel
  checks every archive it unpacks (import, move, copy), a pre-copy's changes, and the world in a
  backup it restores, before changing anything.
- **Bedrock across systems.** A Bedrock folder that arrives without this system's program gets
  it downloaded, of the version it ran when minecraft.net still has that one
  (`updates.fetch_bedrock(prefer=...)`, the same URL with the version swapped), else the newest;
  the other system's `bedrock_server`/`.exe` and its symbols file are removed. Java servers need
  nothing: the unit is written again on arrival, and Forge/NeoForge pick `win_args.txt` or
  `unix_args.txt` there.
- **SFTP.** `redstonepanel-sftp.service` is a unit like the panel's (`ExecStart=redstonepanel
  sftp-serve`), so the runner hosts it as the Windows service `redstonepanel-sftp` as the system
  account; it checks Windows passwords as the panel does and writes through the file manager's
  code, so new files take the server account's rights by inheritance. Python on Windows has no
  `os.pread`/`os.pwrite`: each SFTP handle seeks and reads under its own lock instead. The
  installer carries paramiko (with bcrypt, PyNaCl and invoke).
- **Let's Encrypt.** certbot doesn't run on Windows; win-acme does, and writes PEM files the
  panel's "HTTPS with my own certificate" takes. On both systems the panel now looks at those
  files every ten minutes and restarts itself when a renewed pair loads (`tls.watch_own_certificate`),
  so no client needs a hook script.

## No terminal on Windows

The panel's terminal (1.21.10) stays Linux-only, by design (Quentin, 24 Nov 2026). A Windows
panel usually runs on the computer its owner sits at, and a command prompt is one click away
there without a remote connection. Leaving it out also keeps a way to run any command as any
Windows account out of the panel. On Windows `terminal.supported()` is False, so the page hides the Terminal
button and its Settings switch and the API refuses a session.

## The installer

`packaging/windows/build.py` makes it on Windows: `stage` downloads the embeddable Python,
installs the libraries next to it and copies the panel; `msi` wraps that with WiX 3
(`redstonepanel.wxs`). The installer asks one question, where servers and data go, and runs
`setup.py install`, which makes the folders and their rights, the service, the firewall rule
for the panel's ports (HTTP, and HTTPS when it is on; Panel Settings keeps it in step) and the
first account. `setup.py uninstall` removes services, tasks and
rules and keeps the home folder. Unattended:
`msiexec /i RedstonePanel-x.y.z-x64.msi /qn DATAFOLDER=D:\RedstonePanel`.

The installer isn't signed yet, so SmartScreen warns; the release carries SHA-256 sums.

## Testing

- `tests/test_windows.py` runs anywhere: the Windows side has every function the Linux side
  has with the same arguments, and the plain logic (units, schedules, firewall text, refused
  names, service account ids). `tests/test_mixed_cluster.py` (1.24) runs the certificate code
  against the real `openssl`, and the cluster and name checks.
- `packaging/test/test-windows.py` is the install test, the Windows `test-install.sh`: a real
  install, Java from Adoptium, a real Vanilla server, and then what a person would do.
- `.github/workflows/windows.yml` runs both on GitHub's `windows-2022` and `windows-2025`
  machines for `main`, tags and `feature/windows-*` branches. GitLab has no Windows runner;
  the mirror carries branches to GitHub.
