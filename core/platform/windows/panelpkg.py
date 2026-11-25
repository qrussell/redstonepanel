"""The panel's own updates on Windows (1.23): what apt does for the redstonepanel package on Linux.

A Windows release is the MSI that the release workflow attaches to the GitHub release, with a
`.sha256` file next to it. This module

  * asks GitHub which release is the newest (once a day, or when an admin presses
    **Check for updates**) and keeps the answer in <data>\\upgrade\\release.json, so
    `apt-cache policy redstonepanel` has a Candidate: line;
  * downloads that MSI from GitHub over HTTPS and refuses it unless its SHA-256 matches the
    release's .sha256 file (and the digest GitHub keeps for the file, when it gives one);
  * hands it to the Windows installer in a PowerShell process of its own.

The installer stops the panel and the game servers (they run from the panel's own Python) and
puts in the new files. The PowerShell process waits for it, then starts the panel again, and
the game servers that were running before, whether the installer succeeded or not, and writes
the installer's exit code to <data>\\upgrade\\result.json for the panel to read back.
Nothing here runs from the panel's folder while the installer replaces it.
"""
import hashlib
import json
import os
import re
import subprocess
import time
import urllib.request

from core.platform import paths

REPO = "qrussell/redstonepanel"
LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
ALLOWED_HOSTS = ("api.github.com", "github.com", "objects.githubusercontent.com",
                 "release-assets.githubusercontent.com")
USER_AGENT = "RedstonePanel (update check)"
CHECK_EVERY = 24 * 3600
MAX_MSI = 500 * 1024 * 1024
MSI_RE = re.compile(r"^RedstonePanel-([0-9]+(?:\.[0-9]+){1,3})-x64\.msi$")
OK_CODES = (0, 1641, 3010)          # done; done and Windows restarts; done, restart Windows later


class Problem(Exception):
    pass


def folder():
    return os.path.join(paths.DATA_DIR, "upgrade")


def _file(name):
    return os.path.join(folder(), name)


def _open(url, timeout=60):
    host = urllib.request.urlparse(url).hostname or ""
    if not url.startswith("https://") or host not in ALLOWED_HOSTS:
        raise Problem(f"Not an address the panel downloads its updates from: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(req, timeout=timeout)


def parse_release(data):
    """The Windows installer of a GitHub release (the API's JSON), or None when it has none:
    {"version", "msi", "url", "size", "sha256_url", "digest", "page"}."""
    if not isinstance(data, dict) or data.get("draft") or data.get("prerelease"):
        return None
    assets = {a.get("name"): a for a in data.get("assets") or [] if isinstance(a, dict)}
    for name, a in assets.items():
        m = MSI_RE.fullmatch(name or "")
        check = assets.get(f"{name}.sha256")
        if not m or not check or not a.get("browser_download_url") or not check.get("browser_download_url"):
            continue
        digest = str(a.get("digest") or "")
        return {"version": m.group(1), "msi": name, "url": a["browser_download_url"], "size": int(a.get("size") or 0),
                "sha256_url": check["browser_download_url"],
                "digest": digest[7:].lower() if digest.startswith("sha256:") else None,
                "page": data.get("html_url") or ""}
    return None


def latest(refresh=False, fetch=None):
    """The newest release with a Windows installer, as parse_release gives it, or None. GitHub
    is asked once a day (or when refresh is True); the saved answer is used in between and
    when GitHub can't be reached."""
    try:
        with open(_file("release.json"), encoding="utf-8") as f:
            saved = json.load(f)
    except (OSError, ValueError):
        saved = {}
    if not isinstance(saved, dict):
        saved = {}
    if not refresh and saved.get("at") and time.time() - saved["at"] < CHECK_EVERY:
        return saved.get("release")
    try:
        if fetch:
            data = fetch()
        else:
            with _open(LATEST, timeout=15) as r:
                data = json.load(r)
    except (OSError, ValueError, Problem):
        if refresh:
            raise
        return saved.get("release")
    release = parse_release(data)
    try:
        os.makedirs(folder(), exist_ok=True)
        with open(_file("release.json"), "w", encoding="utf-8") as f:
            json.dump({"at": time.time(), "release": release}, f)
    except OSError:
        pass
    return release


def candidate():
    """The version the newest Windows release has, or None."""
    try:
        info = latest()
    except Exception:
        return None
    return (info or {}).get("version")


def expected_sha256(text, msi):
    """The SHA-256 a `sha256sum` line gives for msi ("<hex>  RedstonePanel-x-x64.msi")."""
    for line in (text or "").splitlines():
        parts = line.strip().split()
        if len(parts) >= 1 and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            if len(parts) == 1 or parts[-1].lstrip("*") == msi:
                return parts[0].lower()
    return None


def download(info, out=print):
    """Downloads the release's MSI into the upgrade folder and checks its SHA-256. Returns its path."""
    with _open(info["sha256_url"], timeout=30) as r:
        want = expected_sha256(r.read(4096).decode("ascii", "replace"), info["msi"])
    if not want:
        raise Problem(f"The release has no SHA-256 for {info['msi']}; nothing was installed.")
    if info.get("digest") and info["digest"] != want:
        raise Problem(f"GitHub's digest and the release's .sha256 file disagree for {info['msi']}; nothing was installed.")
    os.makedirs(folder(), exist_ok=True)
    for old in os.listdir(folder()):                    # the installers of earlier updates
        if MSI_RE.fullmatch(old) or old.endswith(".msi.part"):
            try:
                os.remove(_file(old))
            except OSError:
                pass
    dest = _file(info["msi"])
    tmp = dest + ".part"
    out(f"Get:1 {info['url']} [{(info.get('size') or 0) // 1048576} MB]")
    digest, got = hashlib.sha256(), 0
    try:
        with _open(info["url"], timeout=120) as r, open(tmp, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                got += len(chunk)
                if got > MAX_MSI:
                    raise Problem("The download is larger than any RedstonePanel installer; stopped.")
                digest.update(chunk)
                f.write(chunk)
        if digest.hexdigest() != want:
            raise Problem(f"The download doesn't match the release's SHA-256 for {info['msi']}; nothing was installed.")
        os.replace(tmp, dest)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return dest


def _ps(value):
    return "'" + str(value).replace("'", "''") + "'"


def installer_script(msi, log, result, home, servers, panel_service="redstonepanel"):
    """The PowerShell that runs the installer, waits for it and starts everything again."""
    names = ", ".join(_ps(s) for s in servers) or ""
    return "\r\n".join([
        "$ErrorActionPreference = 'Continue'",
        f"$msiArgs = @('/i', {_ps(msi)}, '/qn', '/norestart', 'DATAFOLDER=\"' + {_ps(home)} + '\"', '/l*v', {_ps(log)})",
        "$p = Start-Process -FilePath \"$env:SystemRoot\\System32\\msiexec.exe\" -ArgumentList $msiArgs -Wait -PassThru",
        "$code = $p.ExitCode",
        f"Start-Service -Name {_ps(panel_service)} -ErrorAction SilentlyContinue",
        f"foreach ($s in @({names})) {{",
        "  $svc = Get-Service -Name $s -ErrorAction SilentlyContinue",
        "  if ($svc -and $svc.Status -eq 'Stopped') { Start-Service -Name $s -ErrorAction SilentlyContinue }",
        "}",
        "$out = @{ code = $code; finished = [int][double]::Parse((Get-Date -UFormat %s)) } | ConvertTo-Json -Compress",
        f"[IO.File]::WriteAllText({_ps(result)}, $out)",
        "",
    ])


def running_servers():
    """The game servers' services (and their Phantoms) running now: started again afterwards."""
    from core.platform.windows import _win
    running = []
    for name in _win.list_services(("minecraft", "phantom-")):
        try:
            if _win.is_ours(_win.service_config(name)) and _win.service_status(name)[0] not in (None, "stopped"):
                running.append(name)
        except OSError:
            pass
    return running


def install(out=print, spawn=None, servers=None):
    """`apt-get install --only-upgrade redstonepanel`: downloads and checks the newest release's
    MSI, then starts the Windows installer in a process of its own and returns. The installer
    restarts the panel; the panel reads the outcome from result.json."""
    from core.platform.windows import _win, packages
    info = latest(refresh=True)
    if not info:
        raise Problem("The newest RedstonePanel release has no Windows installer.")
    installed = packages.installed_version()
    if installed and not packages.is_older(installed, info["version"]):
        out(f"redstonepanel is already the newest version ({installed}).")
        return False
    msi = download(info, out)
    servers = running_servers() if servers is None else servers
    log, result = _file("installer.log"), _file("result.json")
    for stale in (result, log):
        try:
            os.remove(stale)
        except OSError:
            pass
    script = _file("install.ps1")
    with open(script, "w", encoding="utf-8-sig") as f:
        f.write(installer_script(msi, log, result, paths.HOME, servers, _win.PANEL_SERVICE))
    out(f"Unpacking redstonepanel ({info['version']}) ...")
    windir = os.environ.get("SystemRoot") or r"C:\Windows"
    argv = [os.path.join(windir, "System32", "WindowsPowerShell", "v1.0", "powershell.exe"), "-NoProfile",
            "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", script]
    (spawn or _win.spawn_detached)(argv, cwd=folder())
    out(f"Setting up redstonepanel ({info['version']}) ... The Windows installer runs now: the panel and its "
        f"game servers stop for a minute and start again.")
    return True


def result(since=0):
    """The installer's outcome after `since` (a time): {"code", "finished"}, or None while it runs."""
    try:
        with open(_file("result.json"), encoding="utf-8-sig") as f:
            r = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(r, dict) or not isinstance(r.get("code"), int):
        return None
    if since and (r.get("finished") or 0) < since:
        return None
    return r
