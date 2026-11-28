"""cloudflared on Linux (1.29): finding its systemd service, restarting it, checking its file.

The panel never installs cloudflared (it isn't in Ubuntu's or Debian's archive); it looks for the
service that `cloudflared service install` made, reads its command line, and restarts it after
changing a tunnel that is managed in a file.
"""
import os
import re

from core.platform.linux._run import quiet

# Where cloudflared looks for its file when the service names none, in its own order
CONFIG_FILES = ("/etc/cloudflared/config.yml", "/etc/cloudflared/config.yaml",
                "/usr/local/etc/cloudflared/config.yml", "/usr/local/etc/cloudflared/config.yaml")
PROGRAM = "cloudflared"
UNIT_RE = re.compile(r"^cloudflared[@.\w-]*\.service$")


def _argv(exec_start):
    """`systemctl show -p ExecStart --value` -> the first command's argv."""
    m = re.search(r"argv\[\]=(.*?) ;", exec_start or "")
    return m.group(1).split() if m else []


def find(timeout=10):
    """[{"service", "running", "argv"}] for each cloudflared service on this machine."""
    try:
        r = quiet(["systemctl", "list-units", "--all", "--type=service", "--plain", "--no-legend", "--no-pager",
                   "cloudflared*"], timeout=timeout)
        names = [line.split()[0] for line in r.stdout.splitlines() if line.split()]
        r2 = quiet(["systemctl", "list-unit-files", "--type=service", "--plain", "--no-legend", "--no-pager",
                    "cloudflared*"], timeout=timeout)
        names += [line.split()[0] for line in r2.stdout.splitlines() if line.split()]
    except (OSError, ValueError):
        return []
    out = []
    for name in dict.fromkeys(n for n in names if UNIT_RE.match(n) and "@." not in n):
        try:
            r = quiet(["systemctl", "show", name, "-p", "ActiveState", "-p", "ExecStart"], timeout=timeout)
        except OSError:
            continue
        props = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
        out.append({"service": name, "running": props.get("ActiveState") == "active",
                    "argv": _argv(props.get("ExecStart"))})
    return out


def restart(service, timeout=60):
    """Restarts cloudflared's service. Returns True when it is running again."""
    if not UNIT_RE.match(service or ""):
        raise ValueError(service)
    from core import manager
    manager.write_log(f"[TUNNEL] restarting {service}")
    quiet(["systemctl", "restart", service], timeout=timeout)
    return quiet(["systemctl", "is-active", service], timeout=10).stdout.strip() == "active"


def validate(program, config, timeout=30):
    """(ok, what cloudflared said) for `cloudflared --config FILE tunnel ingress validate`."""
    program = program or PROGRAM
    if os.path.basename(program) not in (PROGRAM, PROGRAM + ".exe"):
        raise ValueError(program)
    r = quiet([program, "--config", config, "tunnel", "ingress", "validate"], timeout=timeout)
    return r.returncode == 0, ((r.stdout or "") + (r.stderr or "")).strip()[-2000:]


def default_configs():
    """The files cloudflared reads when its service names none."""
    return list(CONFIG_FILES)


def install_hint():
    """What to run to install cloudflared on this system (shown, never run)."""
    return ("Install cloudflared from Cloudflare's own package repository (pkg.cloudflare.com), then run the "
            "install command the Cloudflare dashboard shows for your tunnel: sudo cloudflared service install <token>")
