"""The runner: what stands in for systemd on Windows, one unit at a time.

Windows can't run java.exe as a service, hold its console, restart it after a crash and keep
what it printed. So each game server's Windows service (`minecraft-<name>`, shown by
services.msc and `sc query`) is this small program. It reads the server's unit file, the same
text file systemd reads on Linux, and does what the file says:

    runner.py service UNIT         a Windows service: start ExecStart in WorkingDirectory, keep its
                                   output, restart it after a crash (Restart=on-failure,
                                   RestartSec=), type the stop command and wait when the service
                                   is stopped or Windows shuts down
    runner.py run UNIT             a one-off unit (a backup, a scheduled restart): what Task
                                   Scheduler starts for a .timer
    runner.py exec NAME -- CMD...  CMD with its output kept as NAME (work that outlives the panel)
    runner.py later SECONDS UNIT   start a service in a while
    runner.py event                run when a service writes to the event log; sends the
                                   panel's started/stopped/crashed notifications as the system

The game runs inside a Job Object, so everything it started ends with it. Output goes to
<home>\\journal\\<unit>\\output.log (a time and the run's id in front of every line) and the unit's
state to status.json next to it: that is where the panel reads "active", the process id, the
start time and the memory and CPU numbers. Commands for the console arrive on a named pipe that
only administrators and the system may open.

Standard library and ctypes only.
"""
import ctypes
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from ctypes import wintypes

_HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != _HERE]     # run as a script: not this folder
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from core.platform import paths                      # noqa: E402
from core.platform.windows import _win, jobs, units   # noqa: E402

LOG_MAX = 8 * 1024 * 1024          # output.log is rotated at this size
LOG_KEEP = 3                       # ... keeping this many older files
EVENT_SOURCE = "RedstonePanel"
EVENT_STARTED, EVENT_STOPPED = 10, 11
UNIT_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,120}\.service$")
PIPE_PREFIX = "\\\\.\\pipe\\RedstonePanel.console."

SERVICE_ACCEPT_STOP, SERVICE_ACCEPT_PRESHUTDOWN = 0x1, 0x100
SERVICE_CONTROL_STOP, SERVICE_CONTROL_INTERROGATE, SERVICE_CONTROL_PRESHUTDOWN = 1, 4, 15
BELOW_NORMAL_PRIORITY_CLASS = 0x4000


# ---------------------------------------------------------------------------
# What a unit printed, and its state
# ---------------------------------------------------------------------------
class Journal:
    """<journal>\\<unit>\\output.log: one line per line printed, "<unix time> <run id> <text>"."""

    def __init__(self, unit, folder=None):
        self.unit = unit
        self.dir = folder or paths.journal_dir(unit)
        os.makedirs(self.dir, exist_ok=True)
        self.path = os.path.join(self.dir, "output.log")
        self.lock = threading.Lock()
        self.invocation = uuid.uuid4().hex

    def new_run(self):
        self.invocation = uuid.uuid4().hex
        return self.invocation

    def write(self, text):
        line = f"{time.time():.3f} {self.invocation} {text.rstrip()}\n"
        with self.lock:
            try:
                if os.path.getsize(self.path) > LOG_MAX:
                    self._rotate()
            except OSError:
                pass
            try:
                with open(self.path, "a", encoding="utf-8", errors="replace") as f:
                    f.write(line)
            except OSError:
                pass

    def _rotate(self):
        for i in range(LOG_KEEP - 1, 0, -1):
            try:
                os.replace(f"{self.path}.{i}", f"{self.path}.{i + 1}")
            except OSError:
                pass
        try:
            os.replace(self.path, self.path + ".1")
        except OSError:
            pass

    def status(self, **fields):
        """Merges fields into status.json (written whole, then renamed into place)."""
        path = os.path.join(self.dir, "status.json")
        with self.lock:
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                data = {}
            data.update(fields, unit=self.unit)
            _write_json(path, data)
        return data

    def usage(self, **fields):
        _write_json(os.path.join(self.dir, "usage.json"), fields)


def _write_json(path, data):
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def decode(raw):
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("mbcs" if os.name == "nt" else "latin-1", "replace")


# ---------------------------------------------------------------------------
# Job Objects: everything the game starts ends with it, and its numbers
# ---------------------------------------------------------------------------
class JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
    _fields_ = [("TotalUserTime", ctypes.c_longlong), ("TotalKernelTime", ctypes.c_longlong),
                ("ThisPeriodTotalUserTime", ctypes.c_longlong), ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD)]


class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + \
               [(n, ctypes.c_size_t) for n in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                                               "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                                               "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage",
                                               "PrivateUsage")]


def _k32():
    k = _win.dll("kernel32")
    if not getattr(k, "_rp_runner", False):
        H = wintypes.HANDLE
        k.CreateJobObjectW.restype = H
        k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k.SetInformationJobObject.restype = wintypes.BOOL
        k.SetInformationJobObject.argtypes = [H, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k.QueryInformationJobObject.restype = wintypes.BOOL
        k.QueryInformationJobObject.argtypes = [H, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
        k.AssignProcessToJobObject.restype = wintypes.BOOL
        k.AssignProcessToJobObject.argtypes = [H, H]
        k.TerminateJobObject.restype = wintypes.BOOL
        k.TerminateJobObject.argtypes = [H, wintypes.UINT]
        k.OpenProcess.restype = H
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.CloseHandle.restype = wintypes.BOOL
        k.CloseHandle.argtypes = [H]
        k.K32GetProcessMemoryInfo.restype = wintypes.BOOL
        k.K32GetProcessMemoryInfo.argtypes = [H, ctypes.c_void_p, wintypes.DWORD]
        k.CreateNamedPipeW.restype = H
        k.CreateNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                       wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
        k.ConnectNamedPipe.restype = wintypes.BOOL
        k.ConnectNamedPipe.argtypes = [H, ctypes.c_void_p]
        k.DisconnectNamedPipe.restype = wintypes.BOOL
        k.DisconnectNamedPipe.argtypes = [H]
        k.ReadFile.restype = wintypes.BOOL
        k.ReadFile.argtypes = [H, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        k.LocalFree.restype = ctypes.c_void_p
        k.LocalFree.argtypes = [ctypes.c_void_p]
        k._rp_runner = True
    return k


class Job:
    """The Job Object a unit's program runs in. With a unit, it is named (jobs.job_name) so the
    panel can change its limits while it runs; memory and cores are the unit's MemoryMax= and
    CPUQuota= (None: no limit)."""

    def __init__(self, memory_max=None, cores=None, unit=None):
        self.h = jobs.create(jobs.job_name(unit) if unit else None)
        try:
            jobs.set_limits(self.h, memory_max, cores, cpu=bool(cores))
        except OSError:
            jobs.set_limits(self.h, memory_max, None, cpu=False)     # no processor cap taken: memory still applies
        self.memory_max, self.cores = memory_max, cores

    def at_memory_limit(self):
        """True when Windows refused the program more memory than its limit (peak reached it)."""
        limit, peak = jobs.memory_state(self.h)
        return jobs.at_limit(limit, peak)

    def add(self, process):
        if not _k32().AssignProcessToJobObject(self.h, wintypes.HANDLE(int(process._handle))):
            raise _win.winerror("Can't put the server in its Job Object")

    def kill(self, code=1):
        _k32().TerminateJobObject(self.h, code)

    def close(self):
        if self.h:
            _k32().CloseHandle(self.h)
            self.h = None

    def numbers(self):
        """(CPU time used in nanoseconds, memory in bytes, processes) of everything in the job."""
        k = _k32()
        acct = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
        if not k.QueryInformationJobObject(self.h, 1, ctypes.byref(acct), ctypes.sizeof(acct), None):
            return None, None, 0
        cpu = (acct.TotalUserTime + acct.TotalKernelTime) * 100

        class PIDS(ctypes.Structure):
            _fields_ = [("NumberOfAssignedProcesses", wintypes.DWORD), ("NumberOfProcessIdsInList", wintypes.DWORD),
                        ("ProcessIdList", ctypes.c_size_t * 256)]
        pids, memory = PIDS(), 0
        if k.QueryInformationJobObject(self.h, 3, ctypes.byref(pids), ctypes.sizeof(pids), None):
            for i in range(min(pids.NumberOfProcessIdsInList, 256)):
                h = k.OpenProcess(0x1000, False, int(pids.ProcessIdList[i]))      # QUERY_LIMITED_INFORMATION
                if not h:
                    continue
                pmc = PROCESS_MEMORY_COUNTERS_EX()
                pmc.cb = ctypes.sizeof(pmc)
                if k.K32GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
                    memory += pmc.WorkingSetSize
                k.CloseHandle(h)
        return cpu, memory, acct.ActiveProcesses


# ---------------------------------------------------------------------------
# The event log
# ---------------------------------------------------------------------------
def report_event(event_id, strings, error=False):
    """Writes an entry to the Application log as RedstonePanel. The entry is what starts the
    notification task, and what an administrator finds in Event Viewer."""
    try:
        adv = _win.dll("advapi32")
        adv.RegisterEventSourceW.restype = wintypes.HANDLE
        adv.RegisterEventSourceW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        adv.ReportEventW.restype = wintypes.BOOL
        adv.ReportEventW.argtypes = [wintypes.HANDLE, wintypes.WORD, wintypes.WORD, wintypes.DWORD, ctypes.c_void_p,
                                     wintypes.WORD, wintypes.DWORD, ctypes.POINTER(wintypes.LPCWSTR), ctypes.c_void_p]
        adv.DeregisterEventSource.argtypes = [wintypes.HANDLE]
        h = adv.RegisterEventSourceW(None, EVENT_SOURCE)
        if not h:
            return False
        arr = (wintypes.LPCWSTR * len(strings))(*[str(s) for s in strings])
        ok = adv.ReportEventW(h, 2 if error else 4, 0, event_id, None, len(strings), 0, arr, None)
        adv.DeregisterEventSource(h)
        return bool(ok)
    except (OSError, AttributeError):
        return False


# ---------------------------------------------------------------------------
# The console pipe
# ---------------------------------------------------------------------------
def pipe_name(unit):
    return PIPE_PREFIX + os.path.splitext(unit)[0]


def console_server(unit, send):
    """Serves the unit's console pipe for ever: every line a client writes is passed to send().
    Only administrators and the system can open the pipe."""
    k = _k32()
    adv = _win.dll("advapi32")
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]

    class SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                    ("bInheritHandle", wintypes.BOOL)]
    sd = ctypes.c_void_p()
    if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW("D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;OW)", 1,
                                                                    ctypes.byref(sd), None):
        return
    sa = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), sd, False)
    invalid = wintypes.HANDLE(-1).value
    while True:
        # inbound, byte stream, blocking, local clients only
        h = k.CreateNamedPipeW(pipe_name(unit), 0x1, 0x0 | 0x8, 255, 4096, 4096, 0, ctypes.byref(sa))
        if not h or h == invalid:
            time.sleep(5)
            continue
        try:
            if not k.ConnectNamedPipe(h, None) and ctypes.get_last_error() != 535:      # 535: already connected
                continue
            data, buf, n = b"", ctypes.create_string_buffer(4096), wintypes.DWORD()
            while len(data) < 65536 and k.ReadFile(h, buf, 4096, ctypes.byref(n), None) and n.value:
                data += buf.raw[:n.value]
            for line in data.decode("utf-8", "replace").splitlines():
                if line.strip():
                    send(line)
        finally:
            k.DisconnectNamedPipe(h)
            k.CloseHandle(h)


# ---------------------------------------------------------------------------
# One program under supervision
# ---------------------------------------------------------------------------
class Program:
    """ExecStart of a unit, running: its process, Job Object, output and console."""

    def __init__(self, unit, spec, journal):
        self.unit, self.spec, self.journal = unit, spec, journal
        self.process = self.job = None
        self.stdin_lock = threading.Lock()
        self.memory_limit_hit = False

    def start(self):
        argv = _win.resolve_program(units.split_command(units.get(self.spec, "Service", "ExecStart", "")))
        if not argv:
            raise OSError("The unit has no ExecStart.")
        cwd = units.get(self.spec, "Service", "WorkingDirectory") or None
        env = dict(os.environ)
        env.update(units.environment(self.spec))
        flags = _win.CREATE_NO_WINDOW
        if units.get(self.spec, "Service", "Nice"):
            flags |= BELOW_NORMAL_PRIORITY_CLASS
        memory, cores = jobs.limits_from_spec(self.spec, units.get)
        self.job = Job(memory, cores, self.unit)
        self.process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, bufsize=0, creationflags=flags)
        try:
            self.job.add(self.process)
        except OSError as e:
            self.journal.write(f"[runner] {e}")
        threading.Thread(target=self._pump, daemon=True).start()
        threading.Thread(target=self._measure, daemon=True).start()
        return self.process.pid

    def _console_log(self):
        """Bedrock writes nothing to a log file of its own: on Linux screen keeps its console in
        <folder>/server.log, which the panel reads (chat, backups, start-up). The runner does the
        same here. None for other programs."""
        argv = units.split_command(units.get(self.spec, "Service", "ExecStart", ""))
        cwd = units.get(self.spec, "Service", "WorkingDirectory")
        if argv and cwd and os.path.basename(argv[0]).lower() == "bedrock_server.exe":
            try:
                return open(os.path.join(cwd, "server.log"), "a", encoding="utf-8", errors="replace")
            except OSError as e:
                self.journal.write(f"[runner] Can't write server.log: {e}")
        return None

    def _pump(self):
        out = self.process.stdout
        log = self._console_log()
        try:
            for raw in iter(out.readline, b""):
                text = decode(raw)
                self.journal.write(text)
                if log:
                    try:
                        log.write(text.rstrip("\r\n") + "\n")
                        log.flush()
                    except OSError:
                        pass
        finally:
            if log:
                log.close()

    def _measure(self):
        while self.process.poll() is None:
            cpu, memory, count = self.job.numbers()
            limit, peak = jobs.memory_state(self.job.h) if self.job.h else (None, None)
            if jobs.at_limit(limit, peak) and not self.memory_limit_hit:
                self.memory_limit_hit = True
                self.journal.write(f"[runner] {self.unit} reached its memory limit ({limit // 1024 ** 2} MB): "
                                   f"Windows refused it more.")
            if cpu is not None:
                self.journal.usage(at=time.time(), cpu_nsec=cpu, memory=memory, processes=count,
                                   memory_max=limit or 0, memory_peak=peak or 0,
                                   memory_limit_hits=int(self.memory_limit_hit))
            time.sleep(2)

    def send(self, text):
        """Types a line into the program's console."""
        with self.stdin_lock:
            try:
                self.process.stdin.write((text.rstrip("\r\n") + "\n").encode("utf-8", "replace"))
                self.process.stdin.flush()
                return True
            except (OSError, ValueError, AttributeError):
                return False

    def stop(self, tick=None):
        """Stops the program: the unit's stop command typed into its console and a wait for it to
        save and leave, then whatever is left of its Job Object is ended. tick() is called once
        a second while waiting. Returns "clean" or "killed"."""
        command = units.get(self.spec, "Service", "X-RedstonePanel-StopCommand")
        wait = units.seconds(units.get(self.spec, "Service", "TimeoutStopSec"), 90)
        how = "killed"
        if command and self.process.poll() is None and self.send(command):
            deadline = time.time() + wait
            while time.time() < deadline and self.process.poll() is None:
                if tick:
                    tick()
                time.sleep(0.5)
            if self.process.poll() is None:
                self.journal.write(f"[runner] {self.unit} didn't stop within {wait} s of '{command}'; ending it.")
            else:
                how = "clean"
        self.job.kill()
        try:
            self.process.wait(15)
        except subprocess.TimeoutExpired:
            self.process.kill()
        return how

    def close(self):
        if self.job:
            self.job.close()


# ---------------------------------------------------------------------------
# runner.py service UNIT
# ---------------------------------------------------------------------------
class SERVICE_STATUS(ctypes.Structure):
    _fields_ = [("dwServiceType", wintypes.DWORD), ("dwCurrentState", wintypes.DWORD),
                ("dwControlsAccepted", wintypes.DWORD), ("dwWin32ExitCode", wintypes.DWORD),
                ("dwServiceSpecificExitCode", wintypes.DWORD), ("dwCheckPoint", wintypes.DWORD),
                ("dwWaitHint", wintypes.DWORD)]


class Service:
    def __init__(self, unit):
        self.unit = unit
        self.name = os.path.splitext(unit)[0]
        self.journal = Journal(unit)
        self.stop_asked = threading.Event()
        self.handle = None
        self.checkpoint = 0
        self.program = None

    # -- the service manager -------------------------------------------------
    def report(self, state, wait_hint=0, exit_code=0):
        if not self.handle:
            return
        st = SERVICE_STATUS()
        st.dwServiceType = _win.SERVICE_WIN32_OWN_PROCESS
        st.dwCurrentState = state
        st.dwControlsAccepted = (SERVICE_ACCEPT_STOP | SERVICE_ACCEPT_PRESHUTDOWN) if state == _win.SERVICE_RUNNING else 0
        st.dwWin32ExitCode = exit_code
        if state in (_win.SERVICE_START_PENDING, _win.SERVICE_STOP_PENDING):
            self.checkpoint += 1
            st.dwCheckPoint = self.checkpoint
        st.dwWaitHint = wait_hint
        _win.dll("advapi32").SetServiceStatus(self.handle, ctypes.byref(st))

    def _control(self, control, event_type, event_data, context):
        if control in (SERVICE_CONTROL_STOP, SERVICE_CONTROL_PRESHUTDOWN):
            self.stop_asked.set()
        return 0

    def dispatch(self):
        """Hands this process to the service manager; returns when the service has stopped."""
        adv = _win.dll("advapi32")
        HANDLER = ctypes.WINFUNCTYPE(wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p)
        MAIN = ctypes.WINFUNCTYPE(None, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR))

        class SERVICE_TABLE_ENTRYW(ctypes.Structure):
            _fields_ = [("lpServiceName", wintypes.LPWSTR), ("lpServiceProc", MAIN)]
        adv.RegisterServiceCtrlHandlerExW.restype = wintypes.HANDLE
        adv.RegisterServiceCtrlHandlerExW.argtypes = [wintypes.LPCWSTR, HANDLER, ctypes.c_void_p]
        adv.SetServiceStatus.restype = wintypes.BOOL
        adv.SetServiceStatus.argtypes = [wintypes.HANDLE, ctypes.POINTER(SERVICE_STATUS)]
        adv.StartServiceCtrlDispatcherW.restype = wintypes.BOOL
        adv.StartServiceCtrlDispatcherW.argtypes = [ctypes.POINTER(SERVICE_TABLE_ENTRYW)]
        self._handler = HANDLER(self._control)

        def service_main(argc, argv):
            code = 0
            try:
                self.handle = adv.RegisterServiceCtrlHandlerExW(self.name, self._handler, None)
                self.report(_win.SERVICE_START_PENDING, 30000)
                code = self.supervise()
            except BaseException as e:                       # never let the service hang half-started
                self.journal.write(f"[runner] stopped on an error: {type(e).__name__}: {e}")
                code = 1
            self.report(_win.SERVICE_STOPPED, exit_code=0 if code == 0 else 1066)   # 1066: service-specific error
        self._main = MAIN(service_main)
        table = (SERVICE_TABLE_ENTRYW * 2)(SERVICE_TABLE_ENTRYW(self.name, self._main), SERVICE_TABLE_ENTRYW(None, MAIN()))
        if not adv.StartServiceCtrlDispatcherW(table):
            raise _win.winerror("Not started by the Windows service manager")

    # -- the unit ------------------------------------------------------------
    def event(self, event_id, requested=False, result="success", code="", status=""):
        if units.get(self.spec, "Service", "X-RedstonePanel-Events", "no") != "yes":
            return
        kind = "start" if event_id == EVENT_STARTED else "stop"
        note_event(self.unit, {"kind": kind, "requested": "1" if requested else "0", "result": result, "code": code,
                               "status": str(status), "invocation": self.journal.invocation})
        report_event(event_id, [self.unit, kind, "1" if requested else "0", result, code, str(status),
                                self.journal.invocation], error=result != "success")

    def supervise(self):
        """Runs the unit until it is stopped or ends by itself. Returns 0, or 1 when it failed and
        isn't to be started again."""
        restarts = 0
        console_started = False
        while True:
            self.spec = spec = units.load(self.unit)
            if spec is None:
                self.journal.write(f"[runner] {self.unit} has no unit file in {paths.UNIT_DIR}.")
                self.journal.status(state="failed", sub="failed", result="resources", pid=0)
                return 1
            policy = units.get(spec, "Service", "Restart", "no")
            pause = units.seconds(units.get(spec, "Service", "RestartSec"), 20)
            invocation = self.journal.new_run()
            self.program = program = Program(self.unit, spec, self.journal)
            try:
                pid = program.start()
            except OSError as e:
                self.journal.write(f"[runner] Couldn't start {self.unit}: {e}")
                result, code, exit_status, requested = "exit-code", "exited", 203, False
                program.close()
                self.report(_win.SERVICE_RUNNING)
            else:
                if not console_started:
                    console_started = True
                    threading.Thread(target=console_server, args=(self.unit, lambda t: self.program.send(t)),
                                     daemon=True).start()
                self.journal.status(state="active", sub="running", pid=pid, result="success", invocation=invocation,
                                    started=time.time(), started_monotonic=time.monotonic(), restarts=restarts,
                                    exit_code="", exit_status="", job="")
                self.report(_win.SERVICE_RUNNING)
                self.event(EVENT_STARTED)
                while program.process.poll() is None and not self.stop_asked.is_set():
                    self.stop_asked.wait(0.5)
                requested = self.stop_asked.is_set()
                if requested:
                    self.journal.status(state="deactivating", sub="stop", job="stop")
                    self.report(_win.SERVICE_STOP_PENDING, 10000)
                    program.stop(tick=lambda: self.report(_win.SERVICE_STOP_PENDING, 10000))
                exit_status = program.process.wait()
                ok = requested or exit_status == 0 or str(exit_status) in " ".join(
                    spec.get("Service", {}).get("SuccessExitStatus", [])).split()
                result, code = ("success" if ok else "exit-code"), "exited"
                if not ok and (program.memory_limit_hit or program.job.at_memory_limit()):
                    result = "oom-kill"            # what systemd calls a stop for going over MemoryMax=
                    self.journal.write(f"[runner] {self.unit} stopped after reaching its memory limit.")
                program.close()
            failed = result != "success"
            if failed:
                self.journal.write(f"[runner] {self.unit} ended with exit status {exit_status}.")
            if requested or not (policy == "always" or (failed and policy == "on-failure")):
                self.journal.status(state="failed" if failed else "inactive", sub="failed" if failed else "dead",
                                    pid=0, result=result, exit_code=code, exit_status=exit_status,
                                    stopped=time.time(), job="stop" if requested else "")
                self.event(EVENT_STOPPED, requested, result, code, exit_status)
                return 1 if failed else 0
            restarts += 1
            self.journal.status(state="activating", sub="auto-restart", pid=0, result=result, exit_code=code,
                                exit_status=exit_status, stopped=time.time(), restarts=restarts, job="")
            self.event(EVENT_STOPPED, False, result, code, exit_status)
            self.journal.write(f"[runner] Starting {self.unit} again in {pause} seconds.")
            if self.stop_asked.wait(pause):
                self.journal.status(state="inactive", sub="dead", job="")
                return 0


# ---------------------------------------------------------------------------
# runner.py run UNIT / exec NAME -- CMD
# ---------------------------------------------------------------------------
def run_once(unit, argv=None, cwd=None, env=None, timeout=None, low_priority=False):
    """Runs a one-off unit (or argv under the unit's name) to the end, output in the journal.
    Returns the exit status."""
    journal = Journal(unit)
    if argv is None:
        spec = units.load(unit)
        if spec is None:
            journal.write(f"[runner] {unit} has no unit file in {paths.UNIT_DIR}.")
            return 1
        argv = units.split_command(units.get(spec, "Service", "ExecStart", ""))
        cwd = units.get(spec, "Service", "WorkingDirectory") or None
        env = units.environment(spec)
        timeout = units.seconds(units.get(spec, "Service", "TimeoutStartSec"), None)
        low_priority = bool(units.get(spec, "Service", "Nice"))
    argv = _win.resolve_program(argv)
    full_env = dict(os.environ)
    full_env.update(env or {})
    flags = _win.CREATE_NO_WINDOW | (BELOW_NORMAL_PRIORITY_CLASS if low_priority else 0)
    journal.status(state="activating", sub="start", pid=os.getpid(), last_trigger=time.time(),
                   invocation=journal.invocation, result="success")
    try:
        p = subprocess.Popen(argv, cwd=cwd, env=full_env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, bufsize=0, creationflags=flags)
    except OSError as e:
        journal.write(f"[runner] Couldn't start {' '.join(argv)}: {e}")
        journal.status(state="failed", sub="failed", pid=0, result="exit-code", exit_status=203, stopped=time.time())
        return 203
    killer = None
    if timeout:
        killer = threading.Timer(timeout, p.kill)
        killer.daemon = True
        killer.start()
    for raw in iter(p.stdout.readline, b""):
        journal.write(decode(raw))
    status = p.wait()
    if killer:
        killer.cancel()
    journal.status(state="inactive" if status == 0 else "failed", sub="dead" if status == 0 else "failed", pid=0,
                   result="success" if status == 0 else "exit-code", exit_code="exited", exit_status=status,
                   stopped=time.time())
    return status


# ---------------------------------------------------------------------------
# runner.py event
# ---------------------------------------------------------------------------
EVENT_NAME_RE = re.compile(r"[0-9]{20}\.json")
EVENTS_KEPT = 20
EVENT_MAX_AGE = 900            # an entry older than this is history, not news


def events_dir(unit):
    return os.path.join(paths.journal_dir(unit), "events")


def note_event(unit, record):
    """Leaves what happened to a unit where the notification task finds it: one small file in the
    unit's own journal folder, which only that server's account (and the system) can write."""
    try:
        folder = events_dir(unit)
        os.makedirs(folder, exist_ok=True)
        name = f"{time.time_ns():020d}.json"
        tmp = os.path.join(folder, name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(record, f)
        os.replace(tmp, os.path.join(folder, name))
        for old in sorted(n for n in os.listdir(folder) if EVENT_NAME_RE.fullmatch(n))[:-EVENTS_KEPT]:
            os.remove(os.path.join(folder, old))
    except OSError:
        pass


def _valid_event(record):
    return (isinstance(record, dict) and record.get("kind") in ("start", "stop")
            and record.get("requested") in ("0", "1") and record.get("result") in ("success", "exit-code", "oom-kill")
            and record.get("code") in ("", "exited")
            and isinstance(record.get("status"), str) and re.fullmatch(r"-?[0-9]{0,12}", record["status"])
            and isinstance(record.get("invocation"), str) and re.fullmatch(r"[0-9a-f]{32}", record["invocation"]))


def new_events(seen, now=None):
    """[(unit, file name, record or None)] not handled yet, oldest first for each unit. A record is
    None when it is too old or isn't what a runner writes. Nothing here is trusted beyond that:
    a server's account can write its own folder, so the most it can do is announce itself."""
    now = time.time() if now is None else now
    junction = getattr(os.path, "isjunction", lambda p: False)
    try:
        unit_names = sorted(os.listdir(paths.UNIT_DIR))
    except OSError:
        unit_names = []
    found = []
    for unit in unit_names:
        if not unit.startswith("minecraft") or not UNIT_RE.fullmatch(unit):
            continue
        folder = events_dir(unit)
        if os.path.islink(folder) or junction(folder):
            continue
        try:
            names = sorted(n for n in os.listdir(folder) if EVENT_NAME_RE.fullmatch(n) and n > seen.get(unit, ""))
        except OSError:
            continue
        for name in names:
            record = None
            path = os.path.join(folder, name)
            try:
                if now - int(name[:20]) / 1e9 <= EVENT_MAX_AGE and not os.path.islink(path) \
                        and os.path.getsize(path) <= 4096:
                    with open(path, encoding="utf-8") as f:
                        record = json.load(f)
            except (OSError, ValueError):
                record = None
            found.append((unit, name, record if _valid_event(record) else None))
    return found


def handle_event(args=()):
    """Started by Task Scheduler (as the system) when a game server's service writes its
    "started" or "stopped" entry to the event log. The entry only rings the bell: what happened
    is read from the files the services left (note_event), and the panel's own `notify-unit`
    does the rest. Returns what it did, one line per file."""
    os.makedirs(paths.RUN_DIR, exist_ok=True)
    state = os.path.join(paths.RUN_DIR, "events-seen.json")
    lines = []
    with open(os.path.join(paths.RUN_DIR, "events.lock"), "a+b") as lock:
        _lock_file(lock)
        try:
            with open(state, encoding="utf-8") as f:
                seen = json.load(f)
            if not isinstance(seen, dict):
                seen = {}
        except (OSError, ValueError):
            seen = {}
        for unit, name, record in new_events(seen):
            seen[unit] = name
            if record is None:
                lines.append(f"{unit} {name} skipped")
                continue
            env = dict(os.environ, SERVICE_RESULT=record["result"], EXIT_CODE=record["code"],
                       EXIT_STATUS=record["status"], INVOCATION_ID=record["invocation"],
                       RP_STOP_REQUESTED=record["requested"])
            argv = _win.resolve_program(["redstonepanel", "notify-unit", record["kind"], os.path.splitext(unit)[0]])
            try:
                code = subprocess.run(argv, env=env, creationflags=_win.CREATE_NO_WINDOW, timeout=300).returncode
            except (OSError, subprocess.SubprocessError) as e:
                code = f"{type(e).__name__}: {e}"
            lines.append(f"{unit} {record['kind']} requested={record['requested']} result={record['result']} "
                         f"status={record['status']} -> {code}")
            _save_seen(state, seen)
        _save_seen(state, seen)
    return lines


def _save_seen(path, seen):
    try:
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(seen, f)
        os.replace(path + ".tmp", path)
    except OSError:
        pass


def _lock_file(f):
    """One notification task at a time (the lock goes when the file is closed)."""
    try:
        import msvcrt
    except ImportError:
        return
    for _ in range(12):                      # LK_LOCK itself waits ten seconds
        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
            return
        except OSError:
            continue


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    cmd = argv[0] if argv else ""
    if cmd == "service" and len(argv) == 2 and UNIT_RE.fullmatch(argv[1]):
        Service(argv[1]).dispatch()
        return 0
    if cmd == "run" and len(argv) == 2 and UNIT_RE.fullmatch(argv[1]):
        return run_once(argv[1])
    if cmd == "exec" and len(argv) > 3 and argv[2] == "--" and UNIT_RE.fullmatch(argv[1]):
        return run_once(argv[1], argv=argv[3:])
    if cmd == "later" and len(argv) == 3 and argv[1].isdigit() and UNIT_RE.fullmatch(argv[2]):
        time.sleep(int(argv[1]))
        _win.start_service(os.path.splitext(argv[2])[0])
        return 0
    if cmd == "event":
        lines = handle_event()
        try:                                   # one line per entry handled: why a notification did or didn't go out
            with open(os.path.join(os.path.dirname(paths.LOG_FILE), "events.log"), "a", encoding="utf-8") as f:
                for line in lines or ["nothing new"]:
                    f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
        except OSError:
            pass
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
