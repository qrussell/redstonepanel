"""What a finished command looks like to callers (the same fields as subprocess.CompletedProcess)."""
from types import SimpleNamespace


def done(stdout="", returncode=0, stderr=""):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode, args=[])
