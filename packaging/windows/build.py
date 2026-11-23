"""Builds RedstonePanel for Windows. Run on Windows with any Python 3.10+ (CI: GitHub's runners).

    python packaging\\windows\\build.py stage OUT     OUT\\python (Python and the libraries the panel
                                                     needs) and OUT\\app (the panel)
    python packaging\\windows\\build.py msi OUT DIST   DIST\\RedstonePanel-<version>-x64.msi and
                                                     SHA256SUMS.txt, from a staged OUT (needs WiX 3)

The installer carries its own Python: the embeddable package from python.org (checked against
the SHA-256 below) with FastAPI, uvicorn, python-multipart and cryptography installed next to
it. Nothing is downloaded on the machine the panel is installed on.
"""
import hashlib
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
PYTHON_VERSION = "3.13.5"
PYTHON_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip"
PYTHON_SHA256 = ""        # of the zip above; an empty value prints the hash and goes on (set it before a release)
APP_FILES = ("main.py", "run.py", "https_redirect.py", "LICENSE", "README.md")
APP_DIRS = ("core", "static")
UPGRADE_CODE = "6B0C1D8E-7C1B-4E0B-9E42-52D5A1B7F1A2"       # never changes: how Windows knows an upgrade


def version():
    with open(os.path.join(ROOT, "debian", "changelog"), encoding="utf-8") as f:
        return re.search(r"\(([^)]+)\)", f.readline()).group(1)


def sh(cmd, **kw):
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, **kw)


def stage(out):
    out = os.path.abspath(out)
    shutil.rmtree(out, ignore_errors=True)
    py, app = os.path.join(out, "python"), os.path.join(out, "app")
    os.makedirs(py)
    archive = os.path.join(out, "python-embed.zip")
    print(f"Downloading {PYTHON_URL}", flush=True)
    urllib.request.urlretrieve(PYTHON_URL, archive)
    with open(archive, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    print(f"python embed sha256: {digest}")
    if PYTHON_SHA256 and digest != PYTHON_SHA256:
        raise SystemExit("The Python download doesn't match the SHA-256 in build.py.")
    with zipfile.ZipFile(archive) as z:
        z.extractall(py)
    os.remove(archive)
    # The embeddable Python only looks where its ._pth file says: its own zip, the libraries and the panel
    pth = [n for n in os.listdir(py) if n.endswith("._pth")][0]
    with open(os.path.join(py, pth), "w", encoding="ascii", newline="\n") as f:
        f.write(f"{pth[:-5]}.zip\n.\nLib\\site-packages\n..\\app\nimport site\n")
    site = os.path.join(py, "Lib", "site-packages")
    os.makedirs(site)
    short = "".join(PYTHON_VERSION.split(".")[:2])
    sh([sys.executable, "-m", "pip", "install", "--no-compile", "--target", site, "--platform", "win_amd64",
        "--python-version", ".".join(PYTHON_VERSION.split(".")[:2]), "--implementation", "cp", "--abi", f"cp{short}",
        "--only-binary=:all:", "-r", os.path.join(HERE, "requirements.txt")])
    shutil.rmtree(os.path.join(site, "bin"), ignore_errors=True)
    os.makedirs(app)
    for name in APP_FILES:
        shutil.copy2(os.path.join(ROOT, name), os.path.join(app, name))
    for name in APP_DIRS:
        shutil.copytree(os.path.join(ROOT, name), os.path.join(app, name),
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    os.makedirs(os.path.join(app, "packaging", "windows"))
    shutil.copy2(os.path.join(HERE, "setup.py"), os.path.join(app, "packaging", "windows", "setup.py"))
    with open(os.path.join(app, "VERSION"), "w", encoding="ascii") as f:
        f.write(version() + "\n")
    # `redstonepanel status`, `redstonepanel add-user NAME`, ... from an administrator prompt
    with open(os.path.join(out, "redstonepanel.cmd"), "w", encoding="ascii", newline="\r\n") as f:
        f.write('@echo off\n"%~dp0python\\python.exe" "%~dp0app\\run.py" %*\n')
    sh([os.path.join(py, "python.exe"), "-c",
        "import fastapi, uvicorn, multipart, cryptography, sys; print('bundled', sys.version.split()[0], "
        "'fastapi', fastapi.__version__, 'uvicorn', uvicorn.__version__, 'cryptography', cryptography.__version__)"])
    sh([sys.executable, "-m", "pip", "list", "--path", site, "--format", "freeze"])
    print(f"Staged in {out}")


def msi(out, dist):
    out, dist = os.path.abspath(out), os.path.abspath(dist)
    os.makedirs(dist, exist_ok=True)
    wix = os.environ.get("WIX")
    if not wix:
        raise SystemExit("WiX Toolset 3 isn't installed (no WIX environment variable).")
    bin_ = os.path.join(wix, "bin")
    work = os.path.join(dist, "wix")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    v = version()
    msi_version = ".".join((v.split("-")[0].split(".") + ["0", "0"])[:3])
    sh([os.path.join(bin_, "heat.exe"), "dir", out, "-nologo", "-cg", "PanelFiles", "-dr", "INSTALLDIR", "-gg", "-g1",
        "-sfrag", "-srd", "-sreg", "-var", "var.Stage", "-out", os.path.join(work, "files.wxs")])
    common = ["-nologo", f"-dStage={out}", f"-dVersion={msi_version}", f"-dUpgradeCode={UPGRADE_CODE}",
              f"-dSource={HERE}", "-arch", "x64", "-ext", "WixUtilExtension", "-ext", "WixUIExtension"]
    sh([os.path.join(bin_, "candle.exe"), *common, "-out", work + os.sep, os.path.join(HERE, "redstonepanel.wxs"),
        os.path.join(work, "files.wxs")])
    target = os.path.join(dist, f"RedstonePanel-{v}-x64.msi")
    sh([os.path.join(bin_, "light.exe"), "-nologo", "-ext", "WixUtilExtension", "-ext", "WixUIExtension",
        "-cultures:en-us", "-sice:ICE61", "-sice:ICE91", "-out", target,
        os.path.join(work, "redstonepanel.wixobj"), os.path.join(work, "files.wixobj")])
    with open(target, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    with open(os.path.join(dist, "SHA256SUMS.txt"), "w", encoding="ascii", newline="\n") as f:
        f.write(f"{digest}  {os.path.basename(target)}\n")
    shutil.rmtree(work, ignore_errors=True)
    print(f"{target}\nsha256 {digest}")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "stage":
        stage(sys.argv[2])
    elif len(sys.argv) == 4 and sys.argv[1] == "msi":
        msi(sys.argv[2], sys.argv[3])
    else:
        sys.exit(__doc__)
