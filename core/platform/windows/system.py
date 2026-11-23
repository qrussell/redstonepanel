"""This machine on Windows: its addresses, who owns a server's files, and the numbers the
dashboard shows (memory, processor, network, disks).

There is no `minecraft` account on Windows. Each game server's service runs as its own virtual
service account (NT SERVICE\\minecraft-<name>), which Windows makes with the service and which
can change only that server's folder. "Giving a folder to the game account" therefore means
granting that server's account rights on it.
"""
import ctypes
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import time
from ctypes import wintypes

from core.platform import paths
from core.platform.windows import _win, units

GAME_ACCOUNT = "minecraft"          # the name the panel uses; on Windows a prefix of the real accounts


def _done(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def _log(text):
    try:
        from core import manager
        manager.write_log(text)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Addresses
# ---------------------------------------------------------------------------
def addresses(timeout=None):
    """This machine's IPv4 addresses (not loopback, not self-assigned 169.254)."""
    found = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in found and not ip.startswith(("127.", "169.254.")):
                found.append(ip)
    except OSError:
        pass
    try:                                        # the address used to reach other machines, first
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            ip = s.getsockname()[0]
        if ip and not ip.startswith(("127.", "0.")):
            found = [ip] + [a for a in found if a != ip]
    except OSError:
        pass
    return found


def interfaces_json(timeout=10):
    """Each interface's IPv4 addresses and prefix lengths, as `ip -j -4 addr show` prints them."""
    r = _win.powershell("Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike '169.254.*' } | "
                        "ForEach-Object { \"$($_.InterfaceAlias)|$($_.IPAddress)|$($_.PrefixLength)\" }", timeout=max(timeout, 30))
    out = {}
    for line in r.stdout.splitlines():
        parts = line.strip().split("|")
        if len(parts) == 3 and parts[2].isdigit():
            name = "lo" if parts[1].startswith("127.") else parts[0]
            out.setdefault(name, []).append({"family": "inet", "local": parts[1], "prefixlen": int(parts[2])})
    return json.dumps([{"ifname": k, "addr_info": v} for k, v in out.items()])


def whoami():
    """Who the panel runs as, for the chat troubleshooting view."""
    try:
        return _win.quiet(["whoami"], timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return os.environ.get("USERNAME", "")


# ---------------------------------------------------------------------------
# The game account
# ---------------------------------------------------------------------------
def account_exists(name=GAME_ACCOUNT):
    """True: every game server's account comes with its service."""
    return True


def ensure_game_account(home):
    """Nothing to do: Windows makes each server's account with its service."""
    return None


def _server_for(path):
    """The game server (service name) whose folder holds path, or None."""
    real = os.path.normcase(os.path.abspath(path))
    best = None
    try:
        names = [n for n in os.listdir(paths.UNIT_DIR) if n.startswith(GAME_ACCOUNT) and n.endswith(".service")]
    except OSError:
        names = []
    for n in names:
        folder = units.get(units.load(n), "Service", "WorkingDirectory", "")
        if not folder:
            continue
        folder = os.path.normcase(os.path.abspath(folder))
        if real == folder or real.startswith(folder + os.sep):
            if best is None or len(folder) > len(best[1]):
                best = (n[:-len(".service")], folder)
    if best:
        return best[0]
    # A folder being prepared for a server that has no unit yet: the panel names both alike
    for servers in _servers_folders():
        root = os.path.normcase(os.path.abspath(servers)) + os.sep
        if real.startswith(root):
            first = os.path.abspath(path)[len(root):].split(os.sep)[0]
            if first.startswith(GAME_ACCOUNT):
                return first
    return None


def _servers_folders():
    """The usual servers folder and every one chosen in Panel Settings."""
    found = [paths.SERVERS_ROOT]
    try:
        from core import settings
        s = settings.load()
        found += [f for f in [s.get("servers_folder")] + list(s.get("servers_folders_used") or []) if f]
    except Exception:
        pass
    return found


def _grant(path):
    server = _server_for(path)
    if not server or not os.path.exists(path):
        return _done(0)
    if _win.service_status(server)[0] is None:
        return _done(0)       # no service yet, so no account: services gives it its folder when it makes the service
    r = _win.grant(path, _win.service_sid(server), inherit=os.path.isdir(path))
    if os.path.isdir(path) and not os.path.islink(path):
        try:
            _win.inherit_rights_below(path)      # a restored world, an unpacked archive: see _win
        except (OSError, AttributeError):
            pass
    return r


def give_to_game_account(path):
    """Lets the server the folder belongs to change it and everything in it."""
    r = _grant(path)
    if r.returncode != 0:
        _log(f"[WARNING] Couldn't give {path} to its server's account: {(r.stderr or r.stdout).strip()[:300]}")
    return r.returncode


def take_for_game_account(path):
    """give_to_game_account() without the activity log."""
    return _grant(path)


def as_game_account(cmd):
    """cmd, unchanged: Windows can't start a program as a service's account from outside the
    service. The panel refuses the few jobs that need it (mod loader installers) in the preview."""
    return list(cmd)


def can_run_as_game_account():
    return False


def is_admin():
    """True when the panel runs with full rights (the system account, or an elevated prompt)."""
    return _win.is_admin()


# ---------------------------------------------------------------------------
# Files and programs
# ---------------------------------------------------------------------------
def java_version_text(java="java"):
    """What `java -version` prints (on stderr)."""
    try:
        return _win.quiet([java, "-version"], timeout=30).stderr
    except (OSError, subprocess.SubprocessError):
        return ""


def _force(func, path, exc):
    try:
        os.chmod(path, stat.S_IWRITE)            # read-only files can't be deleted on Windows
        func(path)
    except OSError:
        pass


def delete_tree(path):
    """Deletes a folder and everything in it (written to the activity log)."""
    _log(f"[SYSTEM] Deleting {path}")
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, onerror=_force)
    elif os.path.lexists(path):
        _force(os.remove, path, None)
    return 1 if os.path.lexists(path) else 0


def make_executable(path):
    """Nothing to do: Windows runs a program by its name, not by a permission bit."""
    return 0


def tail(path, lines=3000):
    """The last lines of a file. Returns (text, error)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 4 * 1024 * 1024))
            data = f.read().decode("utf-8", "replace")
        return "\n".join(data.splitlines()[-int(lines):]) + ("\n" if data else ""), ""
    except OSError as e:
        return "", str(e)


def run_program(cmd, cwd=None, env=None, timeout=None):
    """Runs a program the panel was handed and returns the CompletedProcess, output as text."""
    return _win.quiet(cmd, cwd=cwd, env=env, timeout=timeout)


def copy_into(source, dest, wrap=None):
    """Copies everything in folder `source` over folder `dest`, replacing files that are there
    and never following links."""
    try:
        for root, dirs, files in os.walk(source):
            rel = os.path.relpath(root, source)
            target = dest if rel == "." else os.path.join(dest, rel)
            os.makedirs(target, exist_ok=True)
            dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
            for name in files:
                src, dst = os.path.join(root, name), os.path.join(target, name)
                if os.path.islink(src):
                    continue
                if os.path.lexists(dst):
                    _force(os.remove, dst, None)
                shutil.copy2(src, dst, follow_symlinks=False)
    except OSError as e:
        return _done(1, stderr=str(e))
    return _done()


def remove_files(folder, names, wrap=None):
    """Deletes files (paths relative to folder); ones that aren't there are skipped."""
    for name in names:
        try:
            os.remove(os.path.join(folder, name))
        except FileNotFoundError:
            pass
        except OSError:
            _force(os.remove, os.path.join(folder, name), None)
    return _done()


# ---------------------------------------------------------------------------
# Locks
# ---------------------------------------------------------------------------
def lock(fd, wait=False):
    """Takes the lock on an open file (one holder at a time, released when the file is closed
    or its process ends). wait=False raises BlockingIOError when someone else holds it."""
    import msvcrt
    while True:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return
        except OSError:
            if not wait:
                raise BlockingIOError("The lock is held by another process.")
            time.sleep(0.2)


def unlock(fd):
    import msvcrt
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Accounts of the system itself (sign-in)
# ---------------------------------------------------------------------------
SYSTEM_ACCOUNTS = False             # the preview signs in with panel accounts only (Windows accounts: 1.23)
ACCOUNT_KIND = "Windows"


def system_account_exists(username):
    return False


def system_account_allowed(username, groups=("sudo",)):
    return False


def check_system_password(username, password):
    return False


# ---------------------------------------------------------------------------
# The machine's numbers, in the form the dashboard reads them on Linux (/proc)
# ---------------------------------------------------------------------------
class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD)] + \
               [(n, ctypes.c_ulonglong) for n in ("ullTotalPhys", "ullAvailPhys", "ullTotalPageFile", "ullAvailPageFile",
                                                  "ullTotalVirtual", "ullAvailVirtual", "ullAvailExtendedVirtual")]


class MIB_IF_ROW2(ctypes.Structure):
    _fields_ = [("InterfaceLuid", ctypes.c_uint64), ("InterfaceIndex", ctypes.c_uint32),
                ("InterfaceGuid", ctypes.c_ubyte * 16), ("Alias", ctypes.c_uint16 * 257),
                ("Description", ctypes.c_uint16 * 257), ("PhysicalAddressLength", ctypes.c_uint32),
                ("PhysicalAddress", ctypes.c_ubyte * 32), ("PermanentPhysicalAddress", ctypes.c_ubyte * 32),
                ("Mtu", ctypes.c_uint32), ("Type", ctypes.c_uint32), ("TunnelType", ctypes.c_uint32),
                ("MediaType", ctypes.c_uint32), ("PhysicalMediumType", ctypes.c_uint32),
                ("AccessType", ctypes.c_uint32), ("DirectionType", ctypes.c_uint32),
                ("InterfaceAndOperStatusFlags", ctypes.c_ubyte), ("OperStatus", ctypes.c_uint32),
                ("AdminStatus", ctypes.c_uint32), ("MediaConnectState", ctypes.c_uint32),
                ("NetworkGuid", ctypes.c_ubyte * 16), ("ConnectionType", ctypes.c_uint32),
                ("TransmitLinkSpeed", ctypes.c_uint64), ("ReceiveLinkSpeed", ctypes.c_uint64)] + \
               [(n, ctypes.c_uint64) for n in ("InOctets", "InUcastPkts", "InNUcastPkts", "InDiscards", "InErrors",
                                               "InUnknownProtos", "InUcastOctets", "InMulticastOctets",
                                               "InBroadcastOctets", "OutOctets", "OutUcastPkts", "OutNUcastPkts",
                                               "OutDiscards", "OutErrors", "OutUcastOctets", "OutMulticastOctets",
                                               "OutBroadcastOctets", "OutQLen")]


def _memory():
    st = MEMORYSTATUSEX()
    st.dwLength = ctypes.sizeof(st)
    if not _win.dll("kernel32").GlobalMemoryStatusEx(ctypes.byref(st)):
        return ""
    swap_total = max(0, st.ullTotalPageFile - st.ullTotalPhys)
    swap_free = min(swap_total, max(0, st.ullAvailPageFile - st.ullAvailPhys))
    return (f"MemTotal: {st.ullTotalPhys // 1024} kB\nMemAvailable: {st.ullAvailPhys // 1024} kB\n"
            f"SwapTotal: {swap_total // 1024} kB\nSwapFree: {swap_free // 1024} kB\n")


def _cpu():
    idle, kernel, user = ctypes.c_uint64(), ctypes.c_uint64(), ctypes.c_uint64()
    if not _win.dll("kernel32").GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
        return ""
    # Windows counts idle time inside kernel time
    return f"cpu {user.value} 0 {kernel.value - idle.value} {idle.value} 0 0 0 0\n"


def _cpu_model():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
            return f"model name : {winreg.QueryValueEx(k, 'ProcessorNameString')[0].strip()}\n"
    except OSError:
        return ""


def _network():
    """Bytes in and out per connected, real network adapter, in /proc/net/dev's form."""
    ip = _win.dll("iphlpapi")
    table = ctypes.c_void_p()
    ip.GetIfTable2.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
    ip.FreeMibTable.argtypes = [ctypes.c_void_p]
    lines = ["Inter-|   Receive |  Transmit", " face |bytes packets errs drop fifo frame compressed multicast|bytes"]
    if ip.GetIfTable2(ctypes.byref(table)) != 0:
        return "\n".join(lines) + "\n"
    try:
        count = ctypes.cast(table, ctypes.POINTER(ctypes.c_uint32)).contents.value
        offset = 8                                       # the row array starts on an 8-byte boundary
        rows = ctypes.cast(table.value + offset, ctypes.POINTER(MIB_IF_ROW2 * count)).contents
        seen = set()
        for r in rows:
            flags = r.InterfaceAndOperStatusFlags
            # ethernet (6) and Wi-Fi (71) hardware adapters that are up (1), not filter or virtual copies
            if r.Type not in (6, 71) or r.OperStatus != 1 or not flags & 0x1 or flags & 0x2:
                continue
            name = bytes(r.Alias).decode("utf-16-le", "replace").split("\x00", 1)[0].strip().replace(" ", "_") \
                or f"if{r.InterfaceIndex}"
            if name in seen or name.lower().startswith(("vethernet", "loopback")):
                continue
            seen.add(name)
            lines.append(f"{name}: {r.InOctets} 0 0 0 0 0 0 0 {r.OutOctets} 0 0 0 0 0 0 0")
    finally:
        ip.FreeMibTable(table)
    return "\n".join(lines) + "\n"


def os_name():
    """"Windows 11 Pro 24H2 (build 26100)", from the registry."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as k:
            def val(n):
                try:
                    return str(winreg.QueryValueEx(k, n)[0])
                except OSError:
                    return ""
            product, build, display = val("ProductName"), val("CurrentBuildNumber"), val("DisplayVersion")
        if build.isdigit() and int(build) >= 22000 and product.startswith("Windows 10"):
            product = "Windows 11" + product[len("Windows 10"):]      # the registry still says 10
        return " ".join(x for x in (product, display, f"(build {build})" if build else "") if x)
    except (OSError, ImportError):
        return "Windows"


def machine_file(name, root=None):
    """The text of one of the files the dashboard reads on Linux (under /proc, and
    /etc/os-release), made from what Windows says: "meminfo", "stat", "net/dev", "cpuinfo",
    "uptime", "os-release". "" for anything else."""
    try:
        if name == "meminfo":
            return _memory()
        if name == "stat":
            return _cpu()
        if name == "net/dev":
            return _network()
        if name == "cpuinfo":
            return _cpu_model()
        if name == "uptime":
            k = _win.dll("kernel32")
            k.GetTickCount64.restype = ctypes.c_uint64
            return f"{k.GetTickCount64() / 1000:.2f} 0\n"
        if name == "os-release":
            return f'PRETTY_NAME="{os_name()}"\n'
    except (OSError, AttributeError, ValueError):
        pass
    return ""


def kernel():
    """The system's own version, where Linux shows its kernel: "10.0.26100"."""
    import platform
    return platform.version()


def load_average():
    return None                     # Windows has no load average


def filesystem(path):
    """{"device", "mount", "fstype", "total", "used", "free"} of the drive holding path."""
    drive = os.path.splitdrive(os.path.abspath(path))[0] or "C:"
    mount = drive + "\\"
    usage = shutil.disk_usage(mount)
    fstype = ctypes.create_unicode_buffer(64)
    try:
        _win.dll("kernel32").GetVolumeInformationW(mount, None, 0, None, None, None, fstype, 64)
    except OSError:
        pass
    return {"device": drive.upper(), "mount": mount, "fstype": fstype.value or None, "total": usage.total,
            "used": usage.total - usage.free, "free": usage.free}


def root_path():
    """The system drive ("/" on Linux)."""
    return (os.environ.get("SystemDrive") or "C:") + "\\"


def process_alive(pid):
    k = _win.dll("kernel32")
    k.OpenProcess.restype = wintypes.HANDLE
    h = k.OpenProcess(0x1000, False, int(pid))
    if not h:
        return False
    code = wintypes.DWORD()
    k.GetExitCodeProcess(wintypes.HANDLE(h), ctypes.byref(code))
    k.CloseHandle(wintypes.HANDLE(h))
    return code.value == 259                     # STILL_ACTIVE


def secure_folder(path):
    """Leaves a folder to administrators and the system (what mode 0700 owned by root is)."""
    return _win.lock_down(path).returncode


_FOLDER_RE = re.compile(r"[A-Za-z]:(\\[A-Za-z0-9._-]+)+")
_USERS_SID = "S-1-5-32-545"


def _not_for_servers():
    """Folders that are Windows' own or the panel's own."""
    env = os.environ
    found = [env.get("SystemRoot") or "C:\\Windows", env.get("ProgramFiles") or "C:\\Program Files",
             env.get("ProgramFiles(x86)") or "C:\\Program Files (x86)", "C:\\Users",
             paths.DATA_DIR, paths.BACKUP_ROOT, os.path.dirname(paths.LOG_FILE), paths.UNIT_DIR,
             paths.JOURNAL_DIR, paths.JVM_ROOT, paths.RUN_DIR]
    return [os.path.normcase(os.path.abspath(f)) for f in found]


def _drive_type(mount):
    try:
        return _win.dll("kernel32").GetDriveTypeW(mount)
    except (OSError, AttributeError):
        return None


def check_servers_folder(folder):
    """(the folder as it will be stored, "") when game servers can be kept there, else
    (None, why not). Nothing is created: prepare_servers_folder() does that."""
    folder = str(folder).replace("/", "\\").rstrip("\\")
    example = "for example D:\\Minecraft\\servers"
    if folder.startswith("\\\\"):
        return None, f"Servers can't be kept on a network share. Give a folder on a disk in this computer, {example}."
    if " " in folder:
        return None, f"The folder's path can't have a space in it. Give one such as D:\\Minecraft\\servers."
    if re.fullmatch(r"[A-Za-z]:", folder):
        return None, f"Give a folder, not a whole drive, {example}."
    if not _FOLDER_RE.fullmatch(folder) or ".." in folder.split("\\"):
        return None, (f"Give the full path of a folder with its drive letter, {example}. Its name can only have "
                      f"letters, digits, dots, dashes and underscores.")
    if len(folder) > 100:
        return None, "Choose a shorter folder (100 characters at most)."
    folder = folder[0].upper() + folder[1:]
    if os.path.exists(folder):
        real = os.path.realpath(folder)
        if _FOLDER_RE.fullmatch(real):                 # as Windows spells it; a link's target if it is one
            folder = real[0].upper() + real[1:]
    low = os.path.normcase(folder)
    if low == os.path.normcase(os.path.abspath(paths.SERVERS_ROOT)):
        return paths.SERVERS_ROOT, ""
    for bad in _not_for_servers():
        if low == bad or low.startswith(bad + os.sep):
            return None, f"{folder} belongs to Windows or to the panel itself; choose a folder somewhere else, {example}."
    if os.path.lexists(folder) and not os.path.isdir(folder):
        return None, f"{folder} is a file, not a folder."
    mount = folder[:2] + "\\"
    kind = _drive_type(mount)
    if kind in (0, 1) or not os.path.isdir(mount):
        return None, f"There is no drive {folder[:2]} in this computer."
    if kind is not None and kind != 3:                 # 3: a fixed disk
        return None, (f"Drive {folder[:2]} is a removable, network or optical drive. Servers start with Windows, "
                      f"so they need a disk that is always there.")
    try:
        fstype = filesystem(mount).get("fstype")
    except OSError as e:
        return None, f"Can't look at drive {folder[:2]}: {e.strerror or e}."
    if fstype and fstype.upper() not in ("NTFS", "REFS"):
        return None, (f"Drive {folder[:2]} is formatted as {fstype}, which can't keep each server's files to its own "
                      f"account. Choose a drive formatted as NTFS.")
    return folder, ""


SERVERS_ON_A_DRIVE = "Minecraft\\servers"        # what the panel suggests on a drive other than its own


def _volume(mount):
    """(label, file system) of a drive."""
    label, fstype = ctypes.create_unicode_buffer(261), ctypes.create_unicode_buffer(64)
    try:
        _win.dll("kernel32").GetVolumeInformationW(mount, label, 261, None, None, None, fstype, 64)
    except (OSError, AttributeError):
        pass
    return label.value, fstype.value


def _drive_letters():
    try:
        bits = _win.dll("kernel32").GetLogicalDrives()
    except (OSError, AttributeError):
        return []
    return [chr(65 + i) for i in range(26) if bits & (1 << i)]


def servers_drives():
    """The drives a servers folder can be on (fixed disks formatted as NTFS or ReFS), for a list
    to choose from: [{"drive": "D:", "label", "free", "total", "suggested"}]. "suggested" is the
    folder the panel proposes there: "" (the usual place) on the panel's own drive, and
    <drive>\\Minecraft\\servers on any other, so the path is the same whatever the letter."""
    own = os.path.splitdrive(os.path.abspath(paths.SERVERS_ROOT))[0].upper()
    found = []
    for letter in _drive_letters():
        drive, mount = letter + ":", letter + ":\\"
        if _drive_type(mount) != 3:
            continue
        label, fstype = _volume(mount)
        if fstype.upper() not in ("NTFS", "REFS"):
            continue
        try:
            usage = shutil.disk_usage(mount)
        except OSError:
            continue
        found.append({"drive": drive, "label": label, "free": usage.free, "total": usage.total,
                      "suggested": "" if drive == own else f"{drive}\\{SERVERS_ON_A_DRIVE}"})
    return found


def prepare_servers_folder(folder):
    """Makes the folder (and those above it). A folder the panel makes, or finds empty, is left
    to administrators and the system, as the usual one is; each server's account is given its
    own folder inside when the server is made. Returns "" or why it couldn't."""
    try:
        new = not os.path.isdir(folder) or not os.listdir(folder)
        os.makedirs(folder, exist_ok=True)
    except OSError as e:
        return f"Couldn't make {folder}: {e.strerror or e}."
    if new:
        r = _win.lock_down(folder)
        if r.returncode != 0:
            return f"Couldn't set who may open {folder}: {(r.stderr or r.stdout).strip()[:200]}"
    # This folder only: each server's account passes through to its own folder
    r = _win.quiet(["icacls", folder, "/grant", f"*{_USERS_SID}:(RX)", "/C", "/Q"], timeout=60)
    if r.returncode != 0:
        return f"Couldn't set who may open {folder}: {(r.stderr or r.stdout).strip()[:200]}"
    return ""


def fs():
    """The module the file manager uses in place of `os`: the same calls, with the "open
    relative to a folder, never through a link" behaviour Windows doesn't have built in."""
    from core.platform.windows import dirfd
    return dirfd.module()


def hint(what, *args):
    """A sentence telling an administrator what to type or do, in this system's terms."""
    if what == "restart-panel":
        return "Restart it with: Restart-Service redstonepanel (in an administrator PowerShell)"
    if what == "install-tools":
        return f"Missing {', '.join(args)}. Reinstall RedstonePanel; its installer carries everything it needs."
    if what == "allow-port":
        return (f"New-NetFirewallRule -DisplayName 'RedstonePanel {args[0]}/{args[1]}' -Group RedstonePanel "
                f"-Direction Inbound -Action Allow -Protocol {str(args[1]).upper()} -LocalPort {args[0]}")
    if what == "service-manager":
        return "Windows restarts it"
    if what == "firewall-name":
        return "Windows Defender Firewall"
    if what == "allow-ports":                       # args: "25565/tcp", "19132:19140/udp", ...
        return "; ".join(hint("allow-port", *a.split("/", 1)) for a in args).replace(":", "-")
    if what == "close-ports":
        return "; ".join(f"Remove-NetFirewallRule -DisplayName 'RedstonePanel {a}'" for a in args)
    if what == "install-package":                   # Java comes from the panel's Java page; nothing to type
        return ""
    return ""
