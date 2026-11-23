"""The "package manager" on Windows: Java from Adoptium, and the panel's own version.

Windows has no apt. The Java versions a server can run are Eclipse Temurin JREs, downloaded
from the Adoptium API, checked against the SHA-256 the API gives and unpacked one folder per
version into <home>\\java. To the rest of the panel they keep the names it already knows
(`openjdk-21-jre-headless`, in a folder `java-21-openjdk-amd64`), and this module answers in
apt's words (`apt-cache policy`, `dpkg-query -S`), so choosing, installing and removing Java
is the same code on both systems. The work itself is done by javapkg.py.
"""
import json
import os
import re
import subprocess
import time

from core.platform import paths
from core.platform.windows import _win

ENV = dict(os.environ)
PANEL_PACKAGE = "redstonepanel"
JAVA_PACKAGE_RE = re.compile(r"^openjdk-([0-9]+)-jre-headless$")
MARKER = ".redstonepanel-java.json"                 # in a Java folder the panel installed
CATALOG_TTL = 24 * 3600
FALLBACK_JAVA = (8, 11, 17, 21, 25)


def java_home(major):
    return os.path.join(paths.JVM_ROOT, f"java-{int(major)}-openjdk-amd64")


def _done(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def available():
    return True


def has_catalog():
    return True


def _catalog_file():
    return os.path.join(paths.JVM_ROOT, "catalog.json")


def catalog(refresh=False, fetch=None):
    """The Java versions Adoptium has: [8, 11, 17, ...]. Asked once a day; the last answer (or
    a fixed list) when Adoptium can't be reached."""
    path = _catalog_file()
    try:
        with open(path, encoding="utf-8") as f:
            saved = json.load(f)
    except (OSError, ValueError):
        saved = {}
    if not refresh and saved.get("majors") and time.time() - saved.get("at", 0) < CATALOG_TTL:
        return saved["majors"]
    try:
        from core.platform.windows import javapkg
        majors = (fetch or javapkg.available_majors)()
    except Exception:
        return saved.get("majors") or list(FALLBACK_JAVA)
    try:
        os.makedirs(paths.JVM_ROOT, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"at": time.time(), "majors": majors}, f)
    except OSError:
        pass
    return majors


def _installed_release(major):
    try:
        with open(os.path.join(java_home(major), MARKER), encoding="utf-8") as f:
            return json.load(f).get("release") or "installed"
    except (OSError, ValueError, AttributeError):
        return None


def owner_output(path, timeout=20, env=None):
    """Which package installed a file, in `dpkg-query -S` form: "openjdk-21-jre-headless: PATH"
    for a file in a Java folder the panel installed."""
    real = os.path.normcase(os.path.realpath(path))
    root = os.path.normcase(os.path.realpath(paths.JVM_ROOT)) + os.sep
    if real.startswith(root):
        folder = real[len(root):].split(os.sep)[0]
        m = re.fullmatch(r"java-([0-9]+)-openjdk-amd64", folder)
        if m and _installed_release(m.group(1)):
            return _done(0, f"openjdk-{m.group(1)}-jre-headless: {path}\n")
    return _done(1, "", f"no path found matching pattern {path}\n")


def policy(package, timeout=30, env=None):
    """`apt-cache policy PACKAGE` in apt's words: Installed: and Candidate: lines."""
    if package == PANEL_PACKAGE:
        version = installed_version() or "(none)"
        return f"{package}:\n  Installed: {version}\n  Candidate: {version}\n"
    m = JAVA_PACKAGE_RE.fullmatch(package or "")
    if not m:
        return ""
    major = int(m.group(1))
    installed = _installed_release(major)
    offered = major in catalog()
    return (f"{package}:\n  Installed: {installed or '(none)'}\n"
            f"  Candidate: {('temurin-' + str(major)) if offered else '(none)'}\n")


def names(prefix, timeout=30):
    """Package names starting with prefix: one openjdk-N-jre-headless per Java Adoptium has."""
    return [n for n in (f"openjdk-{m}-jre-headless" for m in catalog()) if n.startswith(prefix)]


def installed_version(package=PANEL_PACKAGE, timeout=10):
    """The installed version of the panel, or of a Java the panel installed. None if not installed."""
    if package == PANEL_PACKAGE:
        try:                                    # the installer writes the release next to the code
            with open(os.path.join(paths.APP_DIR, "VERSION"), encoding="ascii") as f:
                return f.read().strip() or None
        except OSError:
            return None
    m = JAVA_PACKAGE_RE.fullmatch(package or "")
    return _installed_release(m.group(1)) if m else None


def _key(v):
    return [int(x) if x.isdigit() else x for x in re.findall(r"[0-9]+|[A-Za-z]+", str(v))]


def is_older(installed, candidate, timeout=10):
    """True when version `installed` sorts before `candidate`."""
    try:
        return _key(installed) < _key(candidate)
    except TypeError:
        return str(installed) < str(candidate)


def manager_command(options, args):
    """The command line that does what `apt-get OPTIONS ARGS` does for Java on Linux."""
    return [_win.python(), os.path.join(os.path.dirname(os.path.abspath(__file__)), "javapkg.py"), *options, *args]


def start(cmd, env=None):
    """Starts a package manager command with its output on a pipe (text). Returns the Popen."""
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
                            env=ENV if env is None else env, creationflags=_win.CREATE_NO_WINDOW)


def run(cmd, timeout=None, env=None):
    """Runs a package manager command to the end. Returns the CompletedProcess."""
    return _win.quiet(cmd, timeout=timeout, env=ENV if env is None else env)
