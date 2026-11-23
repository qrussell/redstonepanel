"""Windows plumbing for this folder: the service manager, service accounts and running programs.

Standard library and ctypes only. Nothing here touches Windows when the module is imported, so
the pure parts (service SIDs, command lines) can be tested on any system.
"""
import ctypes
import hashlib
import os
import struct
import subprocess
import sys
import time
from ctypes import wintypes

from core.platform import paths

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000

# Service manager
SC_MANAGER_ALL_ACCESS = 0xF003F
SC_MANAGER_CONNECT = 0x0001
SERVICE_ALL_ACCESS = 0xF01FF
SERVICE_QUERY_STATUS = 0x0004
SERVICE_QUERY_CONFIG = 0x0001
SERVICE_WIN32_OWN_PROCESS = 0x10
SERVICE_AUTO_START, SERVICE_DEMAND_START, SERVICE_DISABLED = 2, 3, 4
SERVICE_ERROR_NORMAL = 1
SERVICE_NO_CHANGE = 0xFFFFFFFF
SERVICE_CONTROL_STOP = 1
SERVICE_STOPPED, SERVICE_START_PENDING, SERVICE_STOP_PENDING, SERVICE_RUNNING = 1, 2, 3, 4
SERVICE_CONFIG_DESCRIPTION, SERVICE_CONFIG_FAILURE_ACTIONS = 1, 2
SERVICE_CONFIG_DELAYED_AUTO_START_INFO, SERVICE_CONFIG_SERVICE_SID_INFO = 3, 5
SC_STATUS_PROCESS_INFO = 0
SC_ACTION_RESTART = 1
ERROR_SERVICE_DOES_NOT_EXIST = 1060
ERROR_SERVICE_ALREADY_RUNNING = 1056
ERROR_SERVICE_NOT_ACTIVE = 1062
ERROR_SERVICE_MARKED_FOR_DELETE = 1072
ERROR_SERVICE_EXISTS = 1073
ERROR_INSUFFICIENT_BUFFER = 122

PANEL_SERVICE = "redstonepanel"
GROUP = "RedstonePanel"                      # firewall rule group, Task Scheduler folder, event source


class SERVICE_STATUS_PROCESS(ctypes.Structure):
    _fields_ = [("dwServiceType", wintypes.DWORD), ("dwCurrentState", wintypes.DWORD),
                ("dwControlsAccepted", wintypes.DWORD), ("dwWin32ExitCode", wintypes.DWORD),
                ("dwServiceSpecificExitCode", wintypes.DWORD), ("dwCheckPoint", wintypes.DWORD),
                ("dwWaitHint", wintypes.DWORD), ("dwProcessId", wintypes.DWORD),
                ("dwServiceFlags", wintypes.DWORD)]


class SERVICE_STATUS(ctypes.Structure):
    _fields_ = [("dwServiceType", wintypes.DWORD), ("dwCurrentState", wintypes.DWORD),
                ("dwControlsAccepted", wintypes.DWORD), ("dwWin32ExitCode", wintypes.DWORD),
                ("dwServiceSpecificExitCode", wintypes.DWORD), ("dwCheckPoint", wintypes.DWORD),
                ("dwWaitHint", wintypes.DWORD)]


class QUERY_SERVICE_CONFIGW(ctypes.Structure):
    _fields_ = [("dwServiceType", wintypes.DWORD), ("dwStartType", wintypes.DWORD),
                ("dwErrorControl", wintypes.DWORD), ("lpBinaryPathName", wintypes.LPWSTR),
                ("lpLoadOrderGroup", wintypes.LPWSTR), ("dwTagId", wintypes.DWORD),
                ("lpDependencies", wintypes.LPWSTR), ("lpServiceStartName", wintypes.LPWSTR),
                ("lpDisplayName", wintypes.LPWSTR)]


class SERVICE_DESCRIPTIONW(ctypes.Structure):
    _fields_ = [("lpDescription", wintypes.LPWSTR)]


class SC_ACTION(ctypes.Structure):
    _fields_ = [("Type", ctypes.c_int), ("Delay", wintypes.DWORD)]


class SERVICE_FAILURE_ACTIONSW(ctypes.Structure):
    _fields_ = [("dwResetPeriod", wintypes.DWORD), ("lpRebootMsg", wintypes.LPWSTR),
                ("lpCommand", wintypes.LPWSTR), ("cActions", wintypes.DWORD),
                ("lpsaActions", ctypes.POINTER(SC_ACTION))]


class SERVICE_SID_INFO(ctypes.Structure):
    _fields_ = [("dwServiceSidType", wintypes.DWORD)]


_dlls = {}


def dll(name):
    """kernel32, advapi32, ... loaded on first use, with errors kept for ctypes.get_last_error()."""
    if name not in _dlls:
        _dlls[name] = ctypes.WinDLL(name, use_last_error=True)
    return _dlls[name]


def _adv():
    a = dll("advapi32")
    if not getattr(a, "_rp_ready", False):
        H = wintypes.HANDLE
        a.OpenSCManagerW.restype = H
        a.OpenSCManagerW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        a.OpenServiceW.restype = H
        a.OpenServiceW.argtypes = [H, wintypes.LPCWSTR, wintypes.DWORD]
        a.CreateServiceW.restype = H
        a.CreateServiceW.argtypes = [H, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                     wintypes.DWORD, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                     ctypes.c_void_p, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR]
        a.ChangeServiceConfigW.restype = wintypes.BOOL
        a.ChangeServiceConfigW.argtypes = [H, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPCWSTR,
                                           wintypes.LPCWSTR, ctypes.c_void_p, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                           wintypes.LPCWSTR, wintypes.LPCWSTR]
        a.ChangeServiceConfig2W.restype = wintypes.BOOL
        a.ChangeServiceConfig2W.argtypes = [H, wintypes.DWORD, ctypes.c_void_p]
        a.QueryServiceConfigW.restype = wintypes.BOOL
        a.QueryServiceConfigW.argtypes = [H, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        a.QueryServiceStatusEx.restype = wintypes.BOOL
        a.QueryServiceStatusEx.argtypes = [H, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                           ctypes.POINTER(wintypes.DWORD)]
        a.StartServiceW.restype = wintypes.BOOL
        a.StartServiceW.argtypes = [H, wintypes.DWORD, ctypes.c_void_p]
        a.ControlService.restype = wintypes.BOOL
        a.ControlService.argtypes = [H, wintypes.DWORD, ctypes.POINTER(SERVICE_STATUS)]
        a.DeleteService.restype = wintypes.BOOL
        a.DeleteService.argtypes = [H]
        a.CloseServiceHandle.restype = wintypes.BOOL
        a.CloseServiceHandle.argtypes = [H]
        a._rp_ready = True
    return a


def winerror(what):
    code = ctypes.get_last_error()
    return OSError(code, f"{what}: {ctypes.FormatError(code).strip()}", None, code)


# ---------------------------------------------------------------------------
# Programs
# ---------------------------------------------------------------------------
# The bundled Python ignores PYTHON* environment variables, and without this it writes text in
# the machine's legacy code page, which has no arrows, check marks or most alphabets
UTF8 = ("-X", "utf8")


def python():
    """The Python the panel runs on (the one the installer carries)."""
    return sys.executable


def runner_command(*args):
    """The command line that runs the service runner with args."""
    return [python(), *UTF8, os.path.join(paths.APP_DIR, "core", "platform", "windows", "runner.py"), *args]


def resolve_program(argv):
    """argv with the two words a unit may start with made real: "redstonepanel" (the panel's
    command) and "python"."""
    argv = list(argv)
    if argv and argv[0] in ("redstonepanel", paths.PANEL_CMD):
        return [python(), *UTF8, "-u", os.path.join(paths.APP_DIR, "run.py")] + argv[1:]      # -u: output as it happens
    if argv and argv[0] == "python":
        return [python(), *UTF8] + argv[1:]
    return argv


def quiet(cmd, **kw):
    """Runs cmd without a console window and returns the CompletedProcess, output as text."""
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    kw.setdefault("errors", "replace")
    if os.name == "nt":
        kw.setdefault("creationflags", CREATE_NO_WINDOW)
    return subprocess.run(cmd, **kw)


def powershell(script, timeout=60):
    """Runs a PowerShell script (the Windows one, always there) and returns the CompletedProcess.
    The script's own text never comes from a request: values are passed with ps_quote()."""
    exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0",
                       "powershell.exe")
    return quiet([exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
                  "$ProgressPreference='SilentlyContinue'; $ErrorActionPreference='Stop'; " + script],
                 timeout=timeout)


def ps_quote(value):
    """A PowerShell single-quoted string holding value."""
    return "'" + str(value).replace("'", "''") + "'"


def spawn_detached(argv, cwd=None, env=None):
    """Starts a program that outlives the caller (and the service the caller runs in)."""
    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    kw = dict(cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
              close_fds=True)
    try:
        return subprocess.Popen(argv, creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, **kw)
    except OSError:
        return subprocess.Popen(argv, creationflags=flags, **kw)      # the job doesn't allow breaking away


def is_admin():
    try:
        return bool(dll("shell32").IsUserAnAdmin())
    except (OSError, AttributeError):
        return False


# ---------------------------------------------------------------------------
# Service accounts
# ---------------------------------------------------------------------------
def service_sid(service):
    """The SID of a service's own account (NT SERVICE\\<service>): S-1-5-80 and the SHA-1 of the
    upper-case name. Known before the service exists, so folders can be handed over first."""
    digest = hashlib.sha1(service.upper().encode("utf-16-le")).digest()
    return "S-1-5-80-" + "-".join(str(n) for n in struct.unpack("<5I", digest))


def service_account(service):
    return f"NT SERVICE\\{service}"


def grant(path, sid, rights="M", inherit=True):
    """Gives an account (by SID) rights on a folder and what is in it: "M" modify, "RX" read."""
    spec = f"*{sid}:{'(OI)(CI)' if inherit else ''}{rights}"
    return quiet(["icacls", path, "/grant", spec, "/C", "/Q"], timeout=300)


def _security():
    a = dll("advapi32")
    if not getattr(a, "_rp_security", False):
        a.GetNamedSecurityInfoW.restype = wintypes.DWORD
        a.GetNamedSecurityInfoW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p,
                                            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                            ctypes.POINTER(ctypes.c_void_p)]
        a.GetSecurityDescriptorControl.restype = wintypes.BOOL
        a.GetSecurityDescriptorControl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.WORD),
                                                   ctypes.POINTER(wintypes.DWORD)]
        a.InitializeAcl.restype = wintypes.BOOL
        a.InitializeAcl.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD]
        a.SetNamedSecurityInfoW.restype = wintypes.DWORD
        a.SetNamedSecurityInfoW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p,
                                            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        k = dll("kernel32")
        k.LocalFree.restype = ctypes.c_void_p
        k.LocalFree.argtypes = [ctypes.c_void_p]
        a._rp_security = True
    return a


def keeps_own_rights(path):
    """True when a file or folder takes no rights from the folder it is in ("inheritance off")."""
    a = _security()
    sd = ctypes.c_void_p()
    if a.GetNamedSecurityInfoW(path, 1, 4, None, None, None, None, ctypes.byref(sd)) != 0:   # a file, its DACL
        return False
    try:
        control, revision = wintypes.WORD(), wintypes.DWORD()
        if not a.GetSecurityDescriptorControl(sd, ctypes.byref(control), ctypes.byref(revision)):
            return False
        return bool(control.value & 0x1000)                                    # SE_DACL_PROTECTED
    finally:
        dll("kernel32").LocalFree(sd)


def inherit_rights(path):
    """Gives a file or folder the rights of the folder it is in and nothing else (what
    `icacls /reset` does), and Windows passes them on to what is inside."""
    a = _security()
    acl = ctypes.create_string_buffer(8)
    if not a.InitializeAcl(acl, 8, 2):                                         # an empty list, ACL_REVISION
        return False
    return a.SetNamedSecurityInfoW(path, 1, 4 | 0x20000000, None, None, acl, None) == 0     # ... and inheritance on


def inherit_rights_below(folder):
    """Turns inheritance back on for every folder under this one that has it off. Python makes
    such folders when it unpacks an archive or makes a temporary folder (mode 0700), and moving
    them into a server's folder would leave the server unable to open its own world. Links are
    left alone and never entered. Returns how many folders were changed."""
    changed = 0
    junction = getattr(os.path, "isjunction", lambda p: False)
    stack = [folder]
    while stack:
        try:
            entries = list(os.scandir(stack.pop()))
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_symlink() or junction(e.path) or not e.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            if keeps_own_rights(e.path) and inherit_rights(e.path):
                changed += 1
            stack.append(e.path)
    return changed


def lock_down(path):
    """Leaves a folder to administrators and the system only (what `chmod 700` as root does)."""
    return quiet(["icacls", path, "/inheritance:r", "/grant:r", "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F",
                  "/C", "/Q"], timeout=300)


# ---------------------------------------------------------------------------
# The service manager
# ---------------------------------------------------------------------------
class _Handle:
    def __init__(self, h):
        self.h = h

    def __enter__(self):
        return self.h

    def __exit__(self, *exc):
        if self.h:
            _adv().CloseServiceHandle(self.h)


def _scm(access=SC_MANAGER_ALL_ACCESS):
    h = _adv().OpenSCManagerW(None, None, access)
    if not h:
        raise winerror("Can't reach the Windows service manager")
    return _Handle(h)


def _open(scm, name, access=SERVICE_ALL_ACCESS):
    h = _adv().OpenServiceW(scm, name, access)
    if not h:
        if ctypes.get_last_error() == ERROR_SERVICE_DOES_NOT_EXIST:
            return None
        raise winerror(f"Can't open the service {name}")
    return _Handle(h)


def service_status(name):
    """(state, pid) of a Windows service: state is "running", "stopped", "starting", "stopping"
    or None when there is no such service."""
    try:
        with _scm(SC_MANAGER_CONNECT) as scm:
            svc = _open(scm, name, SERVICE_QUERY_STATUS)
            if svc is None:
                return None, 0
            with svc as h:
                st, need = SERVICE_STATUS_PROCESS(), wintypes.DWORD()
                if not _adv().QueryServiceStatusEx(h, SC_STATUS_PROCESS_INFO, ctypes.byref(st), ctypes.sizeof(st),
                                                   ctypes.byref(need)):
                    raise winerror(f"Can't ask about the service {name}")
                state = {SERVICE_RUNNING: "running", SERVICE_STOPPED: "stopped", SERVICE_START_PENDING: "starting",
                         SERVICE_STOP_PENDING: "stopping"}.get(st.dwCurrentState, "running")
                return state, st.dwProcessId
    except OSError:
        return None, 0


def service_config(name):
    """{"start": "auto"|"manual"|"disabled", "command": ..., "account": ...} or None."""
    with _scm(SC_MANAGER_CONNECT) as scm:
        svc = _open(scm, name, SERVICE_QUERY_CONFIG)
        if svc is None:
            return None
        with svc as h:
            need = wintypes.DWORD()
            _adv().QueryServiceConfigW(h, None, 0, ctypes.byref(need))
            buf = ctypes.create_string_buffer(need.value or 8192)
            if not _adv().QueryServiceConfigW(h, buf, len(buf), ctypes.byref(need)):
                raise winerror(f"Can't read the service {name}")
            cfg = ctypes.cast(buf, ctypes.POINTER(QUERY_SERVICE_CONFIGW)).contents
            return {"start": {SERVICE_AUTO_START: "auto", SERVICE_DEMAND_START: "manual",
                              SERVICE_DISABLED: "disabled"}.get(cfg.dwStartType, "manual"),
                    "command": cfg.lpBinaryPathName, "account": cfg.lpServiceStartName}


def ensure_service(name, command, display, description, account=None, restart_after=20):
    """Creates the Windows service `name` running `command` (one string), or brings an existing
    one in line. account None is the system account. Returns True when it was created."""
    adv = _adv()
    created = False
    with _scm() as scm:
        svc = _open(scm, name)
        if svc is None:
            h = adv.CreateServiceW(scm, name, display, SERVICE_ALL_ACCESS, SERVICE_WIN32_OWN_PROCESS,
                                   SERVICE_DEMAND_START, SERVICE_ERROR_NORMAL, command, None, None, None,
                                   account, None)
            if not h:
                raise winerror(f"Can't create the service {name}")
            svc, created = _Handle(h), True
        with svc as h:
            if not created:
                if not adv.ChangeServiceConfigW(h, SERVICE_NO_CHANGE, SERVICE_NO_CHANGE, SERVICE_NO_CHANGE, command,
                                                None, None, None, account or "LocalSystem", None, display):
                    raise winerror(f"Can't update the service {name}")
            desc = SERVICE_DESCRIPTIONW(description)
            adv.ChangeServiceConfig2W(h, SERVICE_CONFIG_DESCRIPTION, ctypes.byref(desc))
            # The runner restarts a crashed game itself; this is for the runner dying
            actions = (SC_ACTION * 3)(*[SC_ACTION(SC_ACTION_RESTART, restart_after * 1000)] * 3)
            fa = SERVICE_FAILURE_ACTIONSW(86400, None, None, 3, actions)
            adv.ChangeServiceConfig2W(h, SERVICE_CONFIG_FAILURE_ACTIONS, ctypes.byref(fa))
    return created


def set_start(name, how):
    """how: "auto" (starts with Windows) or "manual"."""
    with _scm() as scm:
        svc = _open(scm, name)
        if svc is None:
            raise OSError(ERROR_SERVICE_DOES_NOT_EXIST, f"There is no service {name}.")
        with svc as h:
            start = SERVICE_AUTO_START if how == "auto" else SERVICE_DEMAND_START
            if not _adv().ChangeServiceConfigW(h, SERVICE_NO_CHANGE, start, SERVICE_NO_CHANGE, None, None, None,
                                               None, None, None, None):
                raise winerror(f"Can't change how the service {name} starts")


def start_service(name, wait=30):
    with _scm(SC_MANAGER_CONNECT) as scm:
        svc = _open(scm, name, 0x0010 | SERVICE_QUERY_STATUS)          # SERVICE_START
        if svc is None:
            raise OSError(ERROR_SERVICE_DOES_NOT_EXIST, f"There is no service {name}.")
        with svc as h:
            if not _adv().StartServiceW(h, 0, None) and ctypes.get_last_error() != ERROR_SERVICE_ALREADY_RUNNING:
                raise winerror(f"Can't start the service {name}")
    deadline = time.time() + wait
    while time.time() < deadline:
        state, _ = service_status(name)
        if state != "starting":
            return state
        time.sleep(0.2)
    return "starting"


def stop_service(name, wait=120):
    """Asks the service to stop and waits for it. Returns its state afterwards."""
    with _scm(SC_MANAGER_CONNECT) as scm:
        svc = _open(scm, name, 0x0020 | SERVICE_QUERY_STATUS)          # SERVICE_STOP
        if svc is None:
            return None
        with svc as h:
            st = SERVICE_STATUS()
            if not _adv().ControlService(h, SERVICE_CONTROL_STOP, ctypes.byref(st)):
                code = ctypes.get_last_error()
                if code not in (ERROR_SERVICE_NOT_ACTIVE, 1061):       # 1061: busy stopping already
                    raise winerror(f"Can't stop the service {name}")
    deadline = time.time() + wait
    while time.time() < deadline:
        state, _ = service_status(name)
        if state in ("stopped", None):
            return state
        time.sleep(0.25)
    return service_status(name)[0]


def delete_service(name):
    with _scm() as scm:
        svc = _open(scm, name)
        if svc is None:
            return False
        with svc as h:
            if not _adv().DeleteService(h) and ctypes.get_last_error() != ERROR_SERVICE_MARKED_FOR_DELETE:
                raise winerror(f"Can't remove the service {name}")
    return True


def list_services(prefixes):
    """Names of the Windows services whose name starts with one of the prefixes (from the
    registry, where the service manager keeps them)."""
    import winreg
    out = []
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Services") as key:
        i = 0
        while True:
            try:
                name = winreg.EnumKey(key, i)
            except OSError:
                break
            i += 1
            if name.lower().startswith(tuple(p.lower() for p in prefixes)):
                out.append(name)
    return out
