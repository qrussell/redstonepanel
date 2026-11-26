"""The firewall on Windows: Windows Defender Firewall rules in a "RedstonePanel" group.

The rules are ordinary inbound allow rules ("RedstonePanel 25565/tcp"), visible and editable in
"Windows Defender Firewall with Advanced Security". The panel only ever looks at and removes
rules in its own group. status_text() answers in `ufw status` form, which is what the dashboard
and the port checks read on Linux.
"""
import re
import subprocess

from core.platform.windows import _win

NAME = "Windows Defender Firewall"
GROUP = _win.GROUP
_SPEC = re.compile(r"^[0-9]{1,5}(:[0-9]{1,5})?$")


def installed():
    return True


def _rule_name(port, proto):
    return f"{GROUP} {port}/{proto}"


def _check(port, proto):
    port, proto = str(port), str(proto).lower()
    if not _SPEC.fullmatch(port) or proto not in ("tcp", "udp"):
        raise ValueError(f"Not a port rule: {port}/{proto}")
    return port, proto


def _log(text):
    try:
        from core import manager
        manager.write_log(text)
    except Exception:
        pass


def allow(port, proto):
    """Opens a port ("25565" or a range "21200:21300") for "tcp" or "udp"."""
    port, proto = _check(port, proto)
    name = _win.ps_quote(_rule_name(port, proto))
    r = _win.powershell(
        f"if (-not (Get-NetFirewallRule -DisplayName {name} -ErrorAction SilentlyContinue)) {{ "
        f"New-NetFirewallRule -DisplayName {name} -Group {_win.ps_quote(GROUP)} -Direction Inbound -Action Allow "
        f"-Protocol {proto.upper()} -LocalPort {_win.ps_quote(port.replace(':', '-'))} -Profile Any | Out-Null }}")
    _log(f"[FIREWALL] Allowed {port}/{proto} in {NAME}." if r.returncode == 0
         else f"[WARNING] Couldn't allow {port}/{proto} in {NAME}: {(r.stderr or r.stdout).strip()[:300]}")
    return r.returncode


def remove(port, proto):
    """Takes back a rule made by allow()."""
    port, proto = _check(port, proto)
    r = _win.powershell(f"Get-NetFirewallRule -DisplayName {_win.ps_quote(_rule_name(port, proto))} "
                        f"-ErrorAction SilentlyContinue | Remove-NetFirewallRule")
    _log(f"[FIREWALL] Removed the rule for {port}/{proto} from {NAME}." if r.returncode == 0
         else f"[WARNING] Couldn't remove the rule for {port}/{proto}: {(r.stderr or r.stdout).strip()[:300]}")
    return r.returncode


def status_text(timeout=10):
    """The panel's rules in `ufw status` form: "Status: active" and one "PORT/proto ALLOW Anywhere"
    line per rule. Raises CommandError when PowerShell can't be run in time."""
    r = _win.powershell(
        "$on = @(Get-NetFirewallProfile | Where-Object { $_.Enabled -eq 'True' -or $_.Enabled -eq $true }).Count; "
        "\"profiles=$on\"; "
        f"Get-NetFirewallRule -Group {_win.ps_quote(GROUP)} -ErrorAction SilentlyContinue | "
        "Where-Object { $_.Enabled -eq 'True' -and $_.Direction -eq 'Inbound' -and $_.Action -eq 'Allow' } | "
        "ForEach-Object { $f = $_ | Get-NetFirewallPortFilter; \"rule=$($f.Protocol)|$($f.LocalPort -join ',')\" }; "
        "exit 0",                        # no rules yet is not a failure (PowerShell would say 1)
        timeout=max(timeout, 30))
    if r.returncode != 0:
        raise subprocess.SubprocessError((r.stderr or r.stdout).strip()[:300])
    return to_ufw_text(r.stdout)


def to_ufw_text(output):
    """What status_text()'s PowerShell printed -> `ufw status` text."""
    active, rules = False, []
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("profiles="):
            active = line[9:].strip() not in ("", "0")
        elif line.startswith("rule="):
            proto, _, ports = line[5:].partition("|")
            proto = proto.strip().lower()
            for port in ports.split(","):
                port = port.strip().replace("-", ":")
                if proto in ("tcp", "udp") and _SPEC.fullmatch(port):
                    rules.append(f"{port}/{proto}")
    text = f"Status: {'active' if active else 'inactive'}\n"
    if rules:
        text += "\nTo                         Action      From\n--                         ------      ----\n"
        text += "".join(f"{r:<26} ALLOW       Anywhere\n" for r in rules)
    return text


def is_active(text):
    return "Status: active" in (text or "")


PANEL_GROUP = "RedstonePanel panel"


def panel_rule_script(ports, only_if_present=True):
    """The PowerShell that makes the panel's own rule (group "RedstonePanel panel") open exactly
    these TCP ports. With only_if_present, nothing happens when there is no such rule (the panel
    was installed with --no-firewall) or when it already opens exactly these ports."""
    ports = sorted({int(p) for p in ports if 0 < int(p) < 65536})
    name = _win.ps_quote(f"RedstonePanel web panel ({', '.join(str(p) for p in ports)}/tcp)")
    group = _win.ps_quote(PANEL_GROUP)
    make = (f"Get-NetFirewallRule -Group {group} -ErrorAction SilentlyContinue | Remove-NetFirewallRule; "
            f"New-NetFirewallRule -DisplayName {name} -Group {group} -Direction Inbound -Action Allow "
            f"-Protocol TCP -LocalPort {','.join(str(p) for p in ports)} -Profile Any | Out-Null")
    if only_if_present:                 # and left alone when it already opens exactly these ports
        want = _win.ps_quote(",".join(str(p) for p in ports))
        have = (f"(@($r | Get-NetFirewallPortFilter | ForEach-Object {{ $_.LocalPort }}) | "
                "Sort-Object { [int]$_ } -Unique) -join ','")
        return (f"if (Get-NetFirewallRule -Group {group} -ErrorAction SilentlyContinue) {{ "
                f"$r = @(Get-NetFirewallRule -Group {group}); "
                f"if ($r.Count -ne 1 -or ({have}) -ne {want}) {{ {make} }} }}")
    return make


def panel_ports(ports, only_if_present=True):
    """Opens the panel's own web ports (HTTP, and HTTPS when it's on) in its own rule. Returns
    the PowerShell's exit code."""
    r = _win.powershell(panel_rule_script(ports, only_if_present), timeout=120)
    _log(f"[FIREWALL] The panel's rule now opens {', '.join(str(p) for p in sorted(set(ports)))}/tcp." if r.returncode == 0
         else f"[WARNING] Couldn't change the panel's firewall rule: {(r.stderr or r.stdout).strip()[:300]}")
    return r.returncode
