"""Hard memory and processor limits on Windows (1.23): Job Objects.

On Linux a server's limits are systemd's MemoryMax= and CPUQuota= in a drop-in next to its unit
(core/perf.py writes it), and the kernel's cgroup enforces them. Windows reads the same drop-in:
the runner starts the game inside a Job Object and gives the job

  * a memory limit (JobMemoryLimit): Windows refuses the game more committed memory than this,
    so Java reports that it ran out of memory and stops, where Linux would kill it;
  * a processor hard cap (CPU rate control): at most this share of the whole machine, the way
    CPUQuota=150% is one and a half cores.

The job has a name (`Global\\RedstonePanel.job.<server>`) that only the system and
administrators may open, so the panel can change the limits of a running server, as
`systemctl set-property --runtime` does, and the game itself can't lift them.

The pure parts (reading the drop-in values, the processor rate) are tested on any system.
"""
import ctypes
import os
import re
from ctypes import wintypes

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x200
JOB_OBJECT_CPU_RATE_CONTROL_ENABLE = 0x1
JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP = 0x4
JobObjectBasicAccountingInformation = 1
JobObjectBasicProcessIdList = 3
JobObjectExtendedLimitInformation = 9
JobObjectCpuRateControlInformation = 15
JOB_OBJECT_QUERY = 0x0004
JOB_OBJECT_SET_ATTRIBUTES = 0x0002
NAME_PREFIX = "Global\\RedstonePanel.job."
# Only the system and administrators may open a job by name: not the game's own account
JOB_SDDL = "D:P(A;;GA;;;SY)(A;;GA;;;BA)"
NEAR_LIMIT = 32 * 1024 ** 2         # a peak this close to the limit means Windows refused memory


def job_name(unit):
    """The Job Object's name for a unit: "minecraft-java.service" -> Global\\RedstonePanel.job.minecraft-java."""
    return NAME_PREFIX + os.path.splitext(unit)[0]


_SIZE_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([KMGT]?)\s*$", re.I)


def memory_bytes(value):
    """MemoryMax= ("3221225472", "3G", "infinity", "") -> bytes, or None for no limit."""
    m = _SIZE_RE.match(value or "")
    if not m:
        return None
    n = float(m.group(1))
    if m.group(2):
        n *= 1024 ** ("KMGT".index(m.group(2).upper()) + 1)
    return int(n) or None


def cpu_cores(value):
    """CPUQuota= ("150%", "") -> cores (1.5), or None for no limit."""
    m = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*%\s*", value or "")
    if not m:
        return None
    cores = float(m.group(1)) / 100
    return cores if cores > 0 else None


def cpu_rate(cores, ncpu=None):
    """Windows' CpuRate for `cores` cores: hundredths of a percent of the whole machine
    (1 to 10000), or None when the cap would be the whole machine anyway."""
    if not cores:
        return None
    ncpu = ncpu or os.cpu_count() or 1
    rate = int(round(10000 * float(cores) / ncpu))
    if rate >= 10000:
        return None
    return max(1, rate)


def limits_from_spec(spec, get):
    """(memory bytes or None, cores or None) from a loaded unit; get is units.get."""
    return (memory_bytes(get(spec, "Service", "MemoryMax", "")),
            cpu_cores(get(spec, "Service", "CPUQuota", "")))


def systemd_quota(cores):
    """CPUQuotaPerSecUSec as `systemctl show` prints it: "1.500000s" for 1.5 cores, or "infinity"."""
    return f"{float(cores):.6f}s" if cores else "infinity"


# ---------------------------------------------------------------------------
# Windows calls
# ---------------------------------------------------------------------------
class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                                  "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION), ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


class JOBOBJECT_CPU_RATE_CONTROL_INFORMATION(ctypes.Structure):
    _fields_ = [("ControlFlags", wintypes.DWORD), ("CpuRate", wintypes.DWORD)]


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p), ("bInheritHandle", wintypes.BOOL)]


def _k32():
    from core.platform.windows import _win
    k = _win.dll("kernel32")
    if not getattr(k, "_rp_jobs", False):
        H = wintypes.HANDLE
        k.CreateJobObjectW.restype = H
        k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k.OpenJobObjectW.restype = H
        k.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        k.SetInformationJobObject.restype = wintypes.BOOL
        k.SetInformationJobObject.argtypes = [H, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k.QueryInformationJobObject.restype = wintypes.BOOL
        k.QueryInformationJobObject.argtypes = [H, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
        k.CloseHandle.restype = wintypes.BOOL
        k.CloseHandle.argtypes = [H]
        k.LocalFree.restype = ctypes.c_void_p
        k.LocalFree.argtypes = [ctypes.c_void_p]
        k._rp_jobs = True
    return k


def create(name=None):
    """A new Job Object (a handle with every right), named when name is given. Only the system
    and administrators can open it by its name afterwards."""
    from core.platform.windows import _win
    k = _k32()
    sa = None
    sd = ctypes.c_void_p()
    if name:
        adv = _win.dll("advapi32")
        adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
        adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
        if adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(JOB_SDDL, 1, ctypes.byref(sd), None):
            sa = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), sd, False)
    h = k.CreateJobObjectW(ctypes.byref(sa) if sa else None, name)
    if not h and name:
        h = k.CreateJobObjectW(None, None)            # a name in use (an old run's job): an unnamed one still works
    if sd:
        k.LocalFree(sd)
    if not h:
        raise _win.winerror("Can't create a Job Object")
    return h


def open_named(unit, access=JOB_OBJECT_QUERY | JOB_OBJECT_SET_ATTRIBUTES):
    """A handle to a running unit's job, or None when it isn't running (or isn't limited by name)."""
    return _k32().OpenJobObjectW(access, False, job_name(unit)) or None


def close(h):
    if h:
        _k32().CloseHandle(h)


def extended(h):
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    if not _k32().QueryInformationJobObject(h, JobObjectExtendedLimitInformation, ctypes.byref(info),
                                            ctypes.sizeof(info), None):
        return None
    return info


def set_limits(h, memory=None, cores=None, kill_on_close=True, cpu=True):
    """Sets (or lifts, with None) the job's memory limit and processor cap (cpu=False leaves the
    cap alone). Returns True; raises OSError when Windows didn't take them."""
    from core.platform.windows import _win
    k = _k32()
    info = extended(h) or JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    flags = info.BasicLimitInformation.LimitFlags & ~JOB_OBJECT_LIMIT_JOB_MEMORY
    if kill_on_close:
        flags |= JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if memory:
        flags |= JOB_OBJECT_LIMIT_JOB_MEMORY
        info.JobMemoryLimit = int(memory)
    else:
        info.JobMemoryLimit = 0
    info.BasicLimitInformation.LimitFlags = flags
    ok = bool(k.SetInformationJobObject(h, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)))
    cpu_ok = True
    if cpu:
        rate = cpu_rate(cores)
        info_cpu = JOBOBJECT_CPU_RATE_CONTROL_INFORMATION()
        if rate:
            info_cpu.ControlFlags = JOB_OBJECT_CPU_RATE_CONTROL_ENABLE | JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP
            info_cpu.CpuRate = rate
        cpu_ok = bool(k.SetInformationJobObject(h, JobObjectCpuRateControlInformation, ctypes.byref(info_cpu),
                                                ctypes.sizeof(info_cpu)))
    if not (ok and cpu_ok):
        raise _win.winerror("Windows didn't take the limits")
    return True


def memory_state(h):
    """(limit in bytes or None, peak committed bytes) of a job."""
    info = extended(h)
    if info is None:
        return None, None
    limit = info.JobMemoryLimit if info.BasicLimitInformation.LimitFlags & JOB_OBJECT_LIMIT_JOB_MEMORY else None
    return limit, info.PeakJobMemoryUsed


def at_limit(limit, peak):
    """True when the job's peak reached its memory limit (Windows refused it more)."""
    return bool(limit and peak and peak >= limit - NEAR_LIMIT)
