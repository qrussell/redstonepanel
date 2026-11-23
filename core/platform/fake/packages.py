"""A pretend package manager: fake.state.packages says what is installed and what is on offer."""
import io
import os

from core.platform import fake
from core.platform.fake._result import done

ENV = dict(os.environ)
PANEL_PACKAGE = "redstonepanel"


def available():
    return True


def has_catalog():
    return True


def owner_output(path, timeout=20, env=None):
    pkg = fake.state.file_owners.get(path)
    return done(f"{pkg}: {path}\n") if pkg else done(returncode=1)


def policy(package, timeout=30, env=None):
    p = fake.state.packages.get(package)
    if not p:
        return ""
    return (f"{package}:\n  Installed: {p.get('installed') or '(none)'}\n"
            f"  Candidate: {p.get('candidate') or '(none)'}\n")


def names(prefix, timeout=30):
    return sorted(n for n in fake.state.packages if n.startswith(prefix))


def installed_version(package=PANEL_PACKAGE, timeout=10):
    return fake.state.packages.get(package, {}).get("installed")


def _key(version):
    import re
    return [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", version or "")]


def is_older(installed, candidate, timeout=10):
    return _key(installed) < _key(candidate)


def manager_command(options, args):
    return ["packages", *args]


def _apply(cmd):
    args = [a for a in cmd[1:] if not a.startswith("-")]
    if args[:1] == ["install"]:
        for name in args[1:]:
            p = fake.state.packages.setdefault(name, {"installed": None, "candidate": "1"})
            p["installed"] = p.get("candidate") or "1"
    elif args[:1] in (["remove"], ["purge"]):
        for name in args[1:]:
            fake.state.packages.setdefault(name, {})["installed"] = None


class _Job:
    """A finished command with its output on .stdout, like a Popen."""

    def __init__(self, cmd):
        self.stdout = io.StringIO("")
        self.returncode = 0
        self.args = cmd

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode

    def kill(self):
        pass


def start(cmd, env=None):
    fake.record("packages.run", *cmd[1:])
    _apply(cmd)
    return _Job(cmd)


def run(cmd, timeout=None, env=None):
    fake.record("packages.run", *cmd[1:])
    if "-s" not in cmd:
        _apply(cmd)
    return done()
