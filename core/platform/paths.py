"""Where things live on this system. The one module that spells out system paths.

The Linux layout is the one the panel has always used. Windows (from 1.22) gives the same
names other values, all under one folder (C:\\ProgramData\\RedstonePanel unless the installer
was given another one); see _windows() below. Feature modules keep their own constants (`backups.BACKUP_ROOT`,
`manager.SERVERS_ROOT`, ...) because tests point those at temp folders, but they take their
values from here.

The environment variables RP_DATA_DIR, RP_TLS_DIR, RP_LOG_FILE, RP_BACKUP_DIR,
RP_PHANTOM_DIR and RP_FAILS_FILE still override the matching path (test instances).
"""
import json
import os

APP_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WINDOWS = os.name == "nt"

# The panel's code: an installed package lives under PACKAGE_PREFIX; a copy anywhere else keeps
# its data, certificates and log next to the code.
PACKAGE_PREFIX = "/usr/"
PACKAGED = APP_DIR.startswith(PACKAGE_PREFIX)

# The panel's own files
DATA_DIR = os.environ.get("RP_DATA_DIR") or ("/var/lib/redstonepanel" if PACKAGED else os.path.join(APP_DIR, "data"))
TLS_DIR = os.environ.get("RP_TLS_DIR") or (os.path.join(DATA_DIR, "tls") if PACKAGED else os.path.join(APP_DIR, "tls"))
LOG_FILE = os.environ.get("RP_LOG_FILE") or ("/var/log/redstonepanel/panel.log" if PACKAGED
                                             else os.path.join(APP_DIR, "panel.log"))
RUN_DIR = "/run/redstonepanel"                       # cleared at boot
PANEL_CMD = "/usr/sbin/redstonepanel"                # the `redstonepanel` command
PYTHON = "/usr/bin/python3"                         # runs the panel's own services
PHANTOM_DIR = os.environ.get("RP_PHANTOM_DIR") or "/usr/local/lib/redstonepanel/phantom"

# Game servers and their backups
SERVERS_ROOT = "/opt"                                # each server is SERVERS_ROOT/minecraft-<name>
BACKUP_ROOT = os.environ.get("RP_BACKUP_DIR") or "/var/backups/redstonepanel"

# The service manager (systemd)
UNIT_DIR = "/etc/systemd/system"                     # unit files the panel writes
SERVICE_MANAGER_DIR = "/run/systemd/system"          # exists when systemd runs this machine
RUNTIME_CONTROL = "/run/systemd/system.control"      # where `systemctl set-property --runtime` writes
CGROUP_ROOT = "/sys/fs/cgroup/system.slice"          # a unit's memory and CPU numbers
SYSTEMCTL = "/usr/bin/systemctl"

# Java
JVM_ROOT = "/usr/lib/jvm"                            # one folder per installed Java
DEFAULT_JAVA = "/usr/bin/java"                       # the system's default Java
JAVA_PROGRAM = "java"                                # the program's file name in a Java's bin folder
SCREEN = "/usr/bin/screen"                           # holds the Bedrock console
BEDROCK_PROGRAM = "bedrock_server"                   # Bedrock Dedicated Server's program in its folder
BEDROCK_DOWNLOAD = "serverBedrockLinux"              # its downloadType in Mojang's download-links API

# The machine
PROC = "/proc"
OS_RELEASE = "/etc/os-release"
LOCALTIME = "/etc/localtime"
BLOCK_DEVICES = "/sys/dev/block"
LOGIN_SHELL = "/bin/bash"                            # the minecraft account's shell
LOGIN = "/bin/login"                                 # 1.21.10: starts a terminal's login session
SHELLS_FILE = "/etc/shells"                          # the login shells this machine allows


# ---------------------------------------------------------------------------
# Windows (preview from 1.22)
# ---------------------------------------------------------------------------
def windows_home(app_dir=APP_DIR, environ=os.environ):
    """The folder that holds everything the panel keeps on Windows: RP_HOME, else the folder the
    installer wrote to install.json next to the code, else %ProgramData%\\RedstonePanel."""
    home = environ.get("RP_HOME")
    if not home:
        try:
            with open(os.path.join(os.path.dirname(app_dir), "install.json"), encoding="utf-8") as f:
                home = json.load(f).get("home")
        except (OSError, ValueError, AttributeError):
            home = None
    return home or os.path.join(environ.get("ProgramData") or r"C:\ProgramData", "RedstonePanel")


def windows_layout(home, app_dir=APP_DIR, environ=os.environ, python=None):
    """Every path above for Windows, as a dict of the same names. Nothing here has a space in it
    unless `home` does, and the installer refuses a home with spaces: a unit's ExecStart is one
    line of words, as on Linux."""
    j = os.path.join
    data = environ.get("RP_DATA_DIR") or j(home, "data")
    windir = environ.get("SystemRoot") or r"C:\Windows"
    return {
        "HOME": home,
        "PACKAGE_PREFIX": os.path.dirname(app_dir) + os.sep,
        "PACKAGED": os.path.isfile(j(os.path.dirname(app_dir), "install.json")),
        "DATA_DIR": data,
        "TLS_DIR": environ.get("RP_TLS_DIR") or j(data, "tls"),
        "LOG_FILE": environ.get("RP_LOG_FILE") or j(home, "logs", "panel.log"),
        "RUN_DIR": j(home, "run"),
        "PANEL_CMD": "redstonepanel",                 # the runner and the layer know what this word starts
        "PYTHON": "python",                           # likewise: the Python the panel itself runs on
        "PHANTOM_DIR": environ.get("RP_PHANTOM_DIR") or j(home, "phantom"),
        "SERVERS_ROOT": j(home, "servers"),
        "BACKUP_ROOT": environ.get("RP_BACKUP_DIR") or j(home, "backups"),
        "UNIT_DIR": j(home, "services"),              # the same unit files; the runner reads them
        "JOURNAL_DIR": j(home, "journal"),            # what each unit printed, and its state
        "SERVICE_MANAGER_DIR": j(windir, "System32"),
        "RUNTIME_CONTROL": j(home, "run", "control"),
        "CGROUP_ROOT": j(home, "journal"),
        "SYSTEMCTL": "systemctl",
        "JVM_ROOT": j(home, "java"),                  # one folder per Java, named as on Ubuntu
        "DEFAULT_JAVA": j(home, "java", "default", "bin", "java.exe"),
        "JAVA_PROGRAM": "java.exe",
        "SCREEN": "console",
        "BEDROCK_PROGRAM": "bedrock_server.exe",
        "BEDROCK_DOWNLOAD": "serverBedrockWindows",
        "PROC": j(home, "run", "proc"),
        "OS_RELEASE": j(home, "run", "os-release"),
        "LOCALTIME": j(home, "run", "localtime"),
        "BLOCK_DEVICES": j(home, "run", "block"),
        "LOGIN_SHELL": j(windir, "System32", "cmd.exe"),
    }


JOURNAL_DIR = "/var/log/journal"                     # Linux: the journal is systemd's, not ours
HOME = "/"

if WINDOWS:
    globals().update(windows_layout(windows_home()))


def server_dir(name):
    """Where a server's files go when nothing says otherwise."""
    return os.path.join(SERVERS_ROOT, name) if WINDOWS else f"{SERVERS_ROOT}/{name}"


def unit_file(unit):
    """The file of a unit the panel wrote, e.g. unit_file("minecraft-java.service")."""
    return os.path.join(UNIT_DIR, unit) if WINDOWS else f"{UNIT_DIR}/{unit}"


def journal_dir(unit):
    """Windows: the folder holding what a unit printed (output.log) and its state (status.json)."""
    return os.path.join(JOURNAL_DIR, unit)


def service_manager_running():
    return os.path.isdir(SERVICE_MANAGER_DIR)
