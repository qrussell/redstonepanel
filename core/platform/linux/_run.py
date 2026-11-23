"""How this folder runs programs."""
import subprocess


def logged(cmd):
    """Runs cmd with its output copied to the panel log (the dashboard's activity log).

    Goes through manager.run_and_log, looked up at call time: it is the one place that writes
    the log, and tests replace it to record what would have run."""
    from core import manager
    return manager.run_and_log(cmd)


def quiet(cmd, **kw):
    """Runs cmd and returns the CompletedProcess with its output captured as text."""
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    return subprocess.run(cmd, **kw)


def _opt(**kw):
    """Keyword arguments without the ones left at None (so calls look as they always did)."""
    return {k: v for k, v in kw.items() if v is not None}
