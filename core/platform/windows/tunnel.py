"""cloudflared on Windows (1.29): its Windows service, restarting it, checking its file.

`cloudflared.exe service install <token>` makes a service named Cloudflared running as the system
account; its file, when it has one, is in the system account's profile.
"""
import os
import re

from core.platform.windows import _win

PROGRAM = "cloudflared"
NAME_RE = re.compile(r"^cloudflared[\w.-]*$", re.I)


def _split(command):
    """A Windows service command line -> argv (quoted program, then plain words)."""
    command = (command or "").strip()
    if command.startswith('"'):
        end = command.find('"', 1)
        program, rest = (command[1:end], command[end + 1:]) if end > 0 else (command[1:], "")
    else:
        program, _, rest = command.partition(" ")
    return [program] + [w.strip('"') for w in rest.split()]


def find(timeout=10):
    """[{"service", "running", "argv"}] for each cloudflared service on this machine."""
    out = []
    for svc in _win.services_like("(?i)cloudflared"):
        name = str(svc.get("name") or "")
        if not NAME_RE.match(name):
            continue
        out.append({"service": name, "running": str(svc.get("state") or "").lower() == "running",
                    "argv": _split(svc.get("path"))})
    return out


def restart(service, timeout=60):
    """Restarts cloudflared's service. Returns True when it is running again."""
    if not NAME_RE.match(service or ""):
        raise ValueError(service)
    from core import manager
    manager.write_log(f"[TUNNEL] restarting {service}")
    _win.stop_service(service, wait=timeout)
    _win.start_service(service, wait=timeout)
    return _win.service_status(service)[0] == "running"


def validate(program, config, timeout=30):
    """(ok, what cloudflared said) for `cloudflared --config FILE tunnel ingress validate`."""
    program = program or PROGRAM
    if os.path.basename(program).lower() not in (PROGRAM, PROGRAM + ".exe"):
        raise ValueError(program)
    r = _win.quiet([program, "--config", config, "tunnel", "ingress", "validate"], timeout=timeout)
    return r.returncode == 0, ((r.stdout or "") + (r.stderr or "")).strip()[-2000:]


def default_configs():
    """The files cloudflared reads when its service names none (the system account's profile)."""
    root = os.environ.get("SystemRoot", "C:\\Windows")
    prof = os.path.join(root, "System32", "config", "systemprofile", ".cloudflared")
    return [os.path.join(prof, "config.yml"), os.path.join(prof, "config.yaml")]


def install_hint():
    """What to run to install cloudflared on this system (shown, never run)."""
    return ("Install cloudflared for Windows (winget install --id Cloudflare.cloudflared), then run the install "
            "command the Cloudflare dashboard shows for your tunnel, in an administrator terminal: "
            "cloudflared.exe service install <token>")
