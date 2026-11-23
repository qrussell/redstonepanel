# RedstonePanel on Windows

Status: preview in 1.22 (Java servers on one computer) · Author: Quentin Russell

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
- People sign in with panel accounts. Windows accounts come in 1.23.

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
Each server's account still gets only its own folder, when its service is made. The install
test sets another folder, makes a server there, checks its rights and removes it.

## Not in the preview

Refused with one sentence by the API (`WINDOWS_NOT_YET` in `main.py`) and hidden in the page
(`not_yet` in `/api/auth/me`): Bedrock servers, Quilt, Forge and NeoForge (their installers
would have to run as the server's account), modpacks, memory and processor limits, off-site
backup storage, SFTP, clusters, updating from the panel, and signing in with a Windows
account. They are roadmap 1.23 and 1.24.

Installing a new version stops the game servers while the files are replaced and starts them
again afterwards.

## The installer

`packaging/windows/build.py` makes it on Windows: `stage` downloads the embeddable Python,
installs the libraries next to it and copies the panel; `msi` wraps that with WiX 3
(`redstonepanel.wxs`). The installer asks one question, where servers and data go, and runs
`setup.py install`, which makes the folders and their rights, the service, the firewall rule
for the panel's port and the first account. `setup.py uninstall` removes services, tasks and
rules and keeps the home folder. Unattended:
`msiexec /i RedstonePanel-x.y.z-x64.msi /qn DATAFOLDER=D:\RedstonePanel`.

The installer isn't signed yet, so SmartScreen warns; the release carries SHA-256 sums.

## Testing

- `tests/test_windows.py` runs anywhere: the Windows side has every function the Linux side
  has with the same arguments, and the plain logic (units, schedules, firewall text, refused
  names, service account ids, what the preview refuses).
- `packaging/test/test-windows.py` is the install test, the Windows `test-install.sh`: a real
  install, Java from Adoptium, a real Vanilla server, and then what a person would do.
- `.github/workflows/windows.yml` runs both on GitHub's `windows-2022` and `windows-2025`
  machines for `main`, tags and `feature/windows-*` branches. GitLab has no Windows runner;
  the mirror carries branches to GitHub.
