"""Java runtimes for Java servers: which are installed, which servers run on each, and
installing or removing Ubuntu's openjdk-N-jre-headless packages when they're needed (admins).

The package no longer pulls in Java, so a host only gets the Java versions its servers use.
When a server needs a Java that isn't installed, the panel offers to install it with apt, then
points every server that was waiting for it at the new Java ("rebinding" its systemd unit).
A Java no server uses can be removed again, but only the plain JRE package: if apt would take
anything else with it, the panel refuses and says what, and the admin decides.

Each server's unit names its java by full path (/usr/lib/jvm/java-21-openjdk-amd64/bin/java),
so servers needing different Java versions run side by side and the system default
(/usr/bin/java) doesn't matter.

apt runs in one background job at a time; its output is kept on the job and the main steps go
to the activity log. Package names are only ever built from the fixed list of versions below,
never from request text.
"""
import os
import re
import subprocess
import threading
import time

from core import javaservers, manager

PACKAGE_RE = re.compile(r"^openjdk-([0-9]+)-jre-headless$")
APT_ENV = dict(os.environ, DEBIAN_FRONTEND="noninteractive", LC_ALL="C", LANG="C")
APT_OPTS = ["-q", "-o", "DPkg::Lock::Timeout=300", "-o", "Dpkg::Use-Pty=0"]
APT_TIMEOUT = 1800
MAX_LINES = 300
ING = {"install": "installing", "remove": "removing"}


class RuntimeJobError(ValueError):
    """A Java install or removal that can't be done (message is safe to show)."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def package_for(major):
    """The JRE package for a Java version the panel can install."""
    try:
        major = int(major)
    except (TypeError, ValueError):
        raise RuntimeJobError("Choose a Java version.")
    if major not in javaservers.UBUNTU_JAVA:
        raise RuntimeJobError(f"Java {major} isn't one the panel installs "
                              f"({', '.join(map(str, javaservers.UBUNTU_JAVA))}).")
    return f"openjdk-{major}-jre-headless"


# ---------------------------------------------------------------------------
# What's installed, and who uses it
# ---------------------------------------------------------------------------
def owner(path):
    """The package that installed a file (None if dpkg doesn't know it)."""
    try:
        r = subprocess.run(["dpkg-query", "-S", os.path.realpath(path)], capture_output=True, text=True,
                           timeout=20, env=APT_ENV)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    first = r.stdout.splitlines()[0] if r.stdout else ""
    pkg = first.split(":", 1)[0].split(",")[0].strip()
    return pkg or None


def candidate(package):
    """The version apt would install (None when the package sources don't have it)."""
    try:
        r = subprocess.run(["apt-cache", "policy", package], capture_output=True, text=True,
                           timeout=30, env=APT_ENV)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"^\s*Candidate:\s*(\S+)", r.stdout, re.M)
    return m.group(1) if m and m.group(1) != "(none)" else None


def _default_major():
    """The Java /usr/bin/java points at (update-alternatives), if any."""
    if not os.path.exists("/usr/bin/java"):
        return None
    return javaservers.java_major_of("/usr/bin/java")


def server_usage(servers=None):
    """[{name, required, using, path}] for the Java servers (from manager.list_servers())."""
    servers = manager.list_servers() if servers is None else servers
    return [{"name": s["name"], "required": s.get("java_required"), "using": s.get("java_installed"),
             "path": s.get("java_path")} for s in servers if s.get("edition") == "Java"]


def status(servers=None, packages=True):
    """Installed Java versions with the servers on each, the ones servers need but don't have,
    and the versions that could be installed."""
    usage = server_usage(servers)
    javas = javaservers.installed_javas()
    default = _default_major()
    installed = []
    for major, path in sorted(javas.items()):
        users = sorted(u["name"] for u in usage if u["using"] == major)
        pkg = owner(path) if packages else None
        m = PACKAGE_RE.match(pkg or "")
        removable, why = True, None
        if users:
            removable, why = False, "used by " + ", ".join(users)
        elif not m or int(m.group(1)) != major:
            removable, why = False, (f"installed by {pkg}" if pkg else "not installed from a package")
        installed.append({"major": major, "path": path, "package": pkg, "servers": users,
                          "default": major == default, "removable": removable, "why_not": why})
    needed = {}
    for u in usage:
        if u["required"] and (u["using"] or 0) < u["required"]:
            n = needed.setdefault(u["required"], {"major": u["required"], "servers": [],
                                                   "package": javaservers.java_package(u["required"])})
            n["servers"].append(u["name"])
    installable = [{"major": m, "package": f"openjdk-{m}-jre-headless"}
                   for m in javaservers.UBUNTU_JAVA if m not in javas]
    switch = []
    for u in usage:
        best = _best(u["required"], javas)
        if best and u["using"] != best and (u["using"] or 0) >= (u["required"] or 0):
            switch.append({"name": u["name"], "using": u["using"], "best": best})
    return {"installed": installed, "needed": sorted(needed.values(), key=lambda n: n["major"]),
            "installable": installable, "switch": switch, "job": job()}


def _best(required, javas):
    """The Java a server needing `required` should run on: that one, else the closest newer."""
    if not required:
        return None
    newer = sorted(m for m in javas if m >= int(required))
    return newer[0] if newer else None


# ---------------------------------------------------------------------------
# Background apt jobs (one at a time)
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_job = {}


def job():
    with _lock:
        if not _job:
            return None
        return dict(_job, lines=list(_job.get("lines", []))[-40:])


def _start(action, major, package, actor, work):
    with _lock:
        if _job.get("state") == "running":
            raise RuntimeJobError(f"Already {ING[_job['action']]} Java {_job['major']}; wait for it to finish.", 409)
        _job.clear()
        _job.update(id=int(time.time() * 1000), action=action, major=major, package=package, actor=actor,
                    state="running", started=int(time.time()), finished=None, error=None, lines=[], rebound=[])
        snapshot = dict(_job)
    threading.Thread(target=_run_job, args=(work,), daemon=True, name=f"java-{action}-{major}").start()
    return snapshot


def _run_job(work):
    try:
        work()
        state, error = "done", None
    except RuntimeJobError as e:
        state, error = "failed", str(e)
    except Exception as e:                       # never leave a job "running" forever
        state, error = "failed", f"{type(e).__name__}: {e}"
    with _lock:
        _job.update(state=state, error=error, finished=int(time.time()))
        action, major = _job.get("action"), _job.get("major")
    if error:
        manager.write_log(f"[JAVA] {ING[action].capitalize()} Java {major} failed: {error}")


def _line(text):
    with _lock:
        lines = _job.setdefault("lines", [])
        lines.append(text)
        del lines[:-MAX_LINES]


def _apt(args):
    """Runs apt-get, keeping its output on the job. Returns the exit code."""
    cmd = ["apt-get", *APT_OPTS, *args]
    _line("$ " + " ".join(cmd))
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=APT_ENV)
    except OSError as e:
        raise RuntimeJobError(f"Couldn't run apt-get: {e.strerror or e}")
    started = time.time()
    for raw in p.stdout:
        text = raw.rstrip()
        if text:
            _line(text)
            if text.startswith(("Setting up openjdk-", "Removing openjdk-", "Purging configuration files for openjdk-", "E: ")):
                manager.write_log(f"[JAVA] {text}")
        if time.time() - started > APT_TIMEOUT:
            p.kill()
            raise RuntimeJobError("apt-get took more than 30 minutes and was stopped.")
    return p.wait()


def _apt_error(code):
    with _lock:
        errs = [l[3:] for l in _job.get("lines", []) if l.startswith("E: ")]
    return errs[-1] if errs else f"apt-get exited with code {code}"


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------
def install(major, actor="admin"):
    """Starts installing Java `major`. Returns the job (poll job() for progress)."""
    package = package_for(major)
    major = int(major)
    if major in javaservers.installed_javas():
        raise RuntimeJobError(f"Java {major} is already installed.")
    job_ = _start("install", major, package, actor, lambda: _install(major, package))
    manager.write_log(f"[JAVA] {actor} is installing Java {major} ({package}).")
    return job_


def _install(major, package):
    if not candidate(package):
        _line(f"{package} isn't in the package lists yet; updating them.")
        code = _apt(["update"])
        if code != 0:
            raise RuntimeJobError(f"Couldn't update the package lists: {_apt_error(code)}")
        if not candidate(package):
            raise RuntimeJobError(f"This system's package sources don't have {package}. Install Java {major} "
                                  f"another way into /usr/lib/jvm, or choose a Minecraft version that needs a "
                                  f"Java you have.")
    code = _apt(["install", "-y", "--no-install-recommends", package])
    if code != 0:
        raise RuntimeJobError(f"Installing {package} failed: {_apt_error(code)}")
    javas = javaservers.installed_javas()
    if major not in javas:
        raise RuntimeJobError(f"{package} installed, but no Java {major} appeared in {javaservers.JVM_ROOT}.")
    manager.write_log(f"[JAVA] Installed Java {major} ({javas[major]}).")
    rebound = rebind_waiting()
    with _lock:
        _job["rebound"] = rebound


def rebind_waiting():
    """Points every Java server whose Java is missing or too old at the best installed one, and
    starts the ones that were enabled but couldn't run. Returns the servers changed."""
    javas = javaservers.installed_javas()
    changed = []
    for u in server_usage():
        if not u["required"] or (u["using"] or 0) >= u["required"]:
            continue
        best = _best(u["required"], javas)
        if not best:
            continue
        _rebind(u["name"], javas[best])
        changed.append({"name": u["name"], "java": best})
        if _systemctl("is-enabled", u["name"]) == "enabled" and _systemctl("is-active", u["name"]) != "active":
            _systemctl("reset-failed", u["name"])
            manager.run_and_log(["systemctl", "restart", f"{u['name']}.service"])
    return changed


def _systemctl(verb, name):
    try:
        return subprocess.run(["systemctl", verb, f"{name}.service"], capture_output=True, text=True,
                              timeout=20).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _rebind(name, java):
    d = manager.get_server_dir(name)
    exec_start = manager._unit_exec_start(name)
    manager._write_unit(name, d, "java", java=java, **manager.unit_heap(exec_start))
    manager.write_log(f"[JAVA] {name} now runs on {java}.")


# ---------------------------------------------------------------------------
# Choose the Java one server runs on
# ---------------------------------------------------------------------------
def use(server_name, major, actor="admin"):
    """Points a Java server at an installed Java (that version or newer than it needs), and
    restarts it if it's running."""
    manager._check_server(server_name)
    u = next((u for u in server_usage() if u["name"] == server_name), None)
    if not u:
        raise RuntimeJobError(f"{server_name} isn't a Java server.")
    javas = javaservers.installed_javas()
    try:
        major = int(major)
    except (TypeError, ValueError):
        raise RuntimeJobError("Choose a Java version.")
    if major not in javas:
        raise RuntimeJobError(f"Java {major} isn't installed.")
    if u["required"] and major < u["required"]:
        raise RuntimeJobError(f"{server_name} needs Java {u['required']} or newer.")
    running = _systemctl("is-active", server_name) == "active"
    _rebind(server_name, javas[major])
    manager.write_log(f"[JAVA] {actor} moved {server_name} to Java {major}.")
    if running:
        manager.run_and_log(["systemctl", "restart", f"{server_name}.service"])
    return {"name": server_name, "java": major, "path": javas[major], "restarted": running}


# ---------------------------------------------------------------------------
# Remove
# ---------------------------------------------------------------------------
def removal_check(major):
    """Why Java `major` can't be removed (RuntimeJobError), else its package."""
    try:
        major = int(major)
    except (TypeError, ValueError):
        raise RuntimeJobError("Choose a Java version.")
    javas = javaservers.installed_javas()
    if major not in javas:
        raise RuntimeJobError(f"Java {major} isn't installed.")
    users = sorted(u["name"] for u in server_usage() if u["using"] == major)
    if users:
        raise RuntimeJobError(f"Java {major} is used by {', '.join(users)}. Move "
                              f"{'them' if len(users) > 1 else 'it'} to another Java first (Maintenance → Java).")
    pkg = owner(javas[major])
    m = PACKAGE_RE.match(pkg or "")
    if not m or int(m.group(1)) != major:
        raise RuntimeJobError(f"Java {major} was installed by {pkg or 'something other than a package'}, "
                              f"not openjdk-{major}-jre-headless, so the panel leaves it alone.")
    others = _would_remove(pkg)
    if others:
        raise RuntimeJobError(f"Removing {pkg} would also remove {', '.join(others)}, which the panel "
                              f"didn't install. Remove it yourself if that's what you want: "
                              f"sudo apt purge {pkg}")
    return major, pkg


def _simulate(args, what):
    """Packages an apt-get dry run would remove or purge."""
    try:
        r = subprocess.run(["apt-get", "-s", *args], capture_output=True, text=True, timeout=60, env=APT_ENV)
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeJobError(f"Couldn't check what {what} would do: {e}")
    if r.returncode != 0:
        err = next((l[3:] for l in r.stdout.splitlines() + r.stderr.splitlines() if l.startswith("E: ")), None)
        raise RuntimeJobError(f"Couldn't check what {what} would do: {err or 'apt-get failed'}")
    return sorted(set(l.split()[1].split(":")[0] for l in r.stdout.splitlines() if l.startswith(("Remv ", "Purg "))))


def _would_remove(*packages):
    """Other packages purging `packages` would take with it."""
    return sorted(set(_simulate(["purge", *packages], f"removing {', '.join(packages)}")) - set(packages))


def _autoremovable():
    """Packages apt installed as dependencies that nothing needs any more."""
    return _simulate(["autoremove"], "autoremove")


def remove(major, actor="admin"):
    """Starts removing an unused Java (purging its package, and the dependencies it alone
    needed). Returns the job."""
    major, package = removal_check(major)
    job_ = _start("remove", major, package, actor, lambda: _remove(major, package))
    manager.write_log(f"[JAVA] {actor} is removing Java {major} ({package}), which no server uses.")
    return job_


def _remove(major, package):
    if any(u["using"] == major for u in server_usage()):     # a server started using it meanwhile
        raise RuntimeJobError(f"A server started using Java {major}; it wasn't removed.")
    before = set(_autoremovable())
    code = _apt(["purge", "-y", package])
    if code != 0:
        raise RuntimeJobError(f"Removing {package} failed: {_apt_error(code)}")
    manager.write_log(f"[JAVA] Removed Java {major} ({package}).")
    # What only this Java needed (java-common, ca-certificates-java, libraries), and nothing else
    # that was already waiting for an autoremove
    extra = sorted(set(_autoremovable()) - before)
    if extra and not _would_remove(*extra):
        if _apt(["purge", "-y", *extra]) == 0:
            manager.write_log(f"[JAVA] Also removed what only Java {major} needed: {', '.join(extra)}.")
