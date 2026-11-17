"""The panel's own version (the release, e.g. 1.4.0).

Installed from the .deb: what dpkg says is installed. Run from a source checkout: the first
entry of debian/changelog, which is where each release sets its version.
"""
import os
import re
import subprocess

from core.settings import APP_DIR, PACKAGED

_CHANGELOG_RE = re.compile(r"^\S+ \(([^)]+)\)")
_cached = None


def from_changelog(text):
    """'redstonepanel (1.4.0) noble; urgency=medium' -> '1.4.0'; None if the first line isn't an entry."""
    m = _CHANGELOG_RE.match(text.lstrip("﻿").split("\n", 1)[0]) if text else None
    return m.group(1) if m else None


def _installed():
    try:
        r = subprocess.run(["dpkg-query", "-W", "-f=${Version}", "redstonepanel"],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() or None if r.returncode == 0 else None


def _source():
    try:
        with open(os.path.join(APP_DIR, "debian", "changelog"), encoding="utf-8") as f:
            return from_changelog(f.read(200))
    except OSError:
        return None


def panel_version():
    """Looked up once: the panel restarts when it is upgraded."""
    global _cached
    if _cached is None:
        _cached = (_installed() if PACKAGED else _source()) or _source() or "unknown"
    return _cached
