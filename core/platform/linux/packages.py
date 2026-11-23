"""The package manager on Linux: apt and dpkg (Ubuntu and Debian).

Used for two things: the Java versions a server can run (openjdk-N-jre-headless) and the
panel's own package (redstonepanel), which updates itself with apt.
"""
import os
import shutil
import subprocess

from core.platform.linux._run import quiet, _opt

ENV = dict(os.environ, DEBIAN_FRONTEND="noninteractive", LC_ALL="C", LANG="C")
PANEL_PACKAGE = "redstonepanel"


def available():
    return bool(shutil.which("apt-get"))


def has_catalog():
    return bool(shutil.which("apt-cache"))


def owner_output(path, timeout=20, env=None):
    """`dpkg-query -S PATH` -> CompletedProcess: which package installed a file."""
    return quiet(["dpkg-query", "-S", path], timeout=timeout, env=ENV if env is None else env)


def policy(package, timeout=30, env=None):
    """`apt-cache policy PACKAGE` as text (Installed: and Candidate: lines)."""
    return quiet(["apt-cache", "policy", package], **_opt(timeout=timeout, env=env)).stdout


def names(prefix, timeout=30):
    """Package names starting with prefix that the package sources know."""
    return quiet(["apt-cache", "pkgnames", prefix], timeout=timeout, env=dict(os.environ, LC_ALL="C")).stdout.split()


def installed_version(package=PANEL_PACKAGE, timeout=10):
    """The installed version of a package, or None."""
    r = quiet(["dpkg-query", "-W", "-f=${Version}", package], timeout=timeout)
    return (r.stdout.strip() or None) if r.returncode == 0 else None


def is_older(installed, candidate, timeout=10):
    """True when version `installed` sorts before `candidate` (dpkg's rules)."""
    return subprocess.run(["dpkg", "--compare-versions", installed, "lt", candidate], timeout=timeout).returncode == 0


def manager_command(options, args):
    """The apt-get command line for args, e.g. ["install", "-y", "openjdk-21-jre-headless"]."""
    return ["apt-get", *options, *args]


def start(cmd, env=None):
    """Starts a package manager command with its output on a pipe (text). Returns the Popen."""
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            env=ENV if env is None else env)


def run(cmd, timeout=None, env=None):
    """Runs a package manager command to the end. Returns the CompletedProcess."""
    return quiet(cmd, timeout=timeout, env=ENV if env is None else env)

