"""Where things live on this system. The one module that spells out system paths.

Everything here is the Linux layout the panel has always used; a second system gives the same
names other values. Feature modules keep their own constants (`backups.BACKUP_ROOT`,
`manager.SERVERS_ROOT`, ...) because tests point those at temp folders, but they take their
values from here.

The environment variables RP_DATA_DIR, RP_TLS_DIR, RP_LOG_FILE, RP_BACKUP_DIR,
RP_PHANTOM_DIR and RP_FAILS_FILE still override the matching path (test instances).
"""
import os

APP_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
SCREEN = "/usr/bin/screen"                           # holds the Bedrock console

# The machine
PROC = "/proc"
OS_RELEASE = "/etc/os-release"
LOCALTIME = "/etc/localtime"
BLOCK_DEVICES = "/sys/dev/block"
LOGIN_SHELL = "/bin/bash"                            # the minecraft account's shell


def server_dir(name):
    """Where a server's files go when nothing says otherwise."""
    return f"{SERVERS_ROOT}/{name}"


def unit_file(unit):
    """The file of a unit the panel wrote, e.g. unit_file("minecraft-java.service")."""
    return f"{UNIT_DIR}/{unit}"


def service_manager_running():
    return os.path.isdir(SERVICE_MANAGER_DIR)
