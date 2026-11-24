"""Installs and removes Java on Windows: what apt-get does for the panel on Linux.

    javapkg.py update                               ask Adoptium which Java versions it has
    javapkg.py install -y openjdk-21-jre-headless   download Eclipse Temurin 21 (JRE), check its
                                                    SHA-256, unpack it into <home>\\java
    javapkg.py purge -y openjdk-21-jre-headless     remove a Java this program installed
    javapkg.py -s purge ... | -s autoremove         say what would be removed (apt's dry run)

It takes apt-get's arguments and prints lines the way apt does ("Setting up ...", "E: ..."),
because that is what the panel's Java page reads. Downloads come only from the Adoptium API's
own links, over HTTPS, and are refused when the SHA-256 doesn't match.
"""
import hashlib
import json
import os
import re
import shutil
import sys
import time
import urllib.request
import zipfile

_HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != _HERE]
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from core.platform import paths                       # noqa: E402

API = "https://api.adoptium.net/v3"
ALLOWED_HOSTS = ("api.adoptium.net", "github.com", "objects.githubusercontent.com",
                 "release-assets.githubusercontent.com")
PACKAGE_RE = re.compile(r"^openjdk-([0-9]+)-jre-headless$")
MARKER = ".redstonepanel-java.json"
USER_AGENT = "RedstonePanel (Java installer)"
MAX_ZIP = 600 * 1024 * 1024


class Problem(Exception):
    pass


def _open(url, timeout=60):
    host = urllib.request.urlparse(url).hostname or ""
    if not url.startswith("https://") or host not in ALLOWED_HOSTS:
        raise Problem(f"Not a download address the panel uses for Java: {url}")
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=timeout)


def _json(url):
    with _open(url) as r:
        return json.load(r)


def available_majors():
    """The Java versions Adoptium publishes, e.g. [8, 11, 17, 21, 25]."""
    data = _json(f"{API}/info/available_releases")
    return sorted(int(n) for n in data.get("available_releases", []) if int(n) >= 8)


def machine():
    arch = (os.environ.get("PROCESSOR_ARCHITEW6432") or os.environ.get("PROCESSOR_ARCHITECTURE") or "AMD64").upper()
    return {"AMD64": "x64", "ARM64": "aarch64", "X86": "x86"}.get(arch, "x64")


def latest(major):
    """{"link", "sha256", "size", "release", "kind"} of the newest Temurin `major` for Windows: a
    JRE where Adoptium makes one, else the JDK."""
    for kind in ("jre", "jdk"):
        try:
            assets = _json(f"{API}/assets/latest/{int(major)}/hotspot?architecture={machine()}&image_type={kind}"
                           f"&os=windows&vendor=eclipse")
        except OSError:
            assets = []
        for a in assets:
            pkg = (a.get("binary") or {}).get("package") or {}
            if pkg.get("link", "").endswith(".zip") and pkg.get("checksum"):
                return {"link": pkg["link"], "sha256": pkg["checksum"].lower(), "size": int(pkg.get("size") or 0),
                        "release": a.get("release_name") or f"temurin-{major}", "kind": kind}
    raise Problem(f"Adoptium has no Java {major} for Windows on this kind of machine ({machine()}).")


def home(major):
    return os.path.join(paths.JVM_ROOT, f"java-{int(major)}-openjdk-amd64")


def install(major, out=print):
    dest = home(major)
    if os.path.isfile(os.path.join(dest, "bin", "java.exe")):
        out(f"openjdk-{major}-jre-headless is already the newest version.")
        return
    info = latest(major)
    os.makedirs(paths.JVM_ROOT, exist_ok=True)
    tmp = os.path.join(paths.JVM_ROOT, f".download-{major}-{os.getpid()}")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    try:
        archive = os.path.join(tmp, "java.zip")
        out(f"Get:1 {info['link']} [{info['size'] // 1048576} MB]")
        digest, got = hashlib.sha256(), 0
        with _open(info["link"], timeout=120) as r, open(archive, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                got += len(chunk)
                if got > MAX_ZIP:
                    raise Problem("The download is larger than any Java; stopped.")
                digest.update(chunk)
                f.write(chunk)
        if digest.hexdigest() != info["sha256"]:
            raise Problem(f"The download doesn't match Adoptium's SHA-256 for {info['release']}; nothing was installed.")
        out(f"Unpacking openjdk-{major}-jre-headless ({info['release']}) ...")
        unpacked = os.path.join(tmp, "x")
        with zipfile.ZipFile(archive) as z:
            for m in z.infolist():
                name = m.filename.replace("\\", "/")
                if name.startswith("/") or ".." in name.split("/") or ":" in name:
                    raise Problem(f"The archive holds a path that isn't allowed: {m.filename}")
            z.extractall(unpacked)
        tops = [e for e in os.listdir(unpacked)]
        root = os.path.join(unpacked, tops[0]) if len(tops) == 1 else unpacked
        if not os.path.isfile(os.path.join(root, "bin", "java.exe")):
            raise Problem("The archive has no bin\\java.exe.")
        with open(os.path.join(root, MARKER), "w", encoding="utf-8") as f:
            json.dump({"release": info["release"], "kind": info["kind"], "sha256": info["sha256"],
                       "installed": int(time.time())}, f)
        shutil.rmtree(dest, ignore_errors=True)
        os.replace(root, dest)
        out(f"Setting up openjdk-{major}-jre-headless ({info['release']}) ...")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def remove(major, out=print):
    dest = home(major)
    if not os.path.isfile(os.path.join(dest, MARKER)):
        raise Problem(f"openjdk-{major}-jre-headless isn't a Java the panel installed.")
    out(f"Removing openjdk-{major}-jre-headless ...")
    shutil.rmtree(dest)
    if os.path.exists(dest):
        raise Problem(f"Couldn't remove {dest}: a server is still using it.")


def main(argv=None, out=print):
    argv = list(sys.argv[1:] if argv is None else argv)
    simulate, words, skip = False, [], False
    for a in argv:
        if skip:
            skip = False
        elif a == "-o":
            skip = True
        elif a in ("-s", "--simulate", "--dry-run"):
            simulate = True
        elif not a.startswith("-"):
            words.append(a)
    verb, packages = (words[0] if words else ""), words[1:]
    try:
        if verb == "update":
            from core.platform.windows import packages as pk
            out("Java versions at Adoptium: " + ", ".join(str(m) for m in pk.catalog(refresh=True)))
            return 0
        if verb == "autoremove":
            return 0
        if verb in ("install", "purge", "remove"):
            majors = []
            for p in packages:
                m = PACKAGE_RE.fullmatch(p)
                if not m:
                    raise Problem(f"Unable to locate package {p}")
                majors.append(int(m.group(1)))
            for major in majors:
                if simulate:
                    out(f"{'Inst' if verb == 'install' else 'Purg'} openjdk-{major}-jre-headless")
                elif verb == "install":
                    install(major, out)
                else:
                    remove(major, out)
            return 0
        raise Problem(f"Invalid operation {verb or '(none)'}")
    except Problem as e:
        out(f"E: {e}")
    except (OSError, ValueError, zipfile.BadZipFile) as e:
        out(f"E: {type(e).__name__}: {e}")
    return 100


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    sys.exit(main())
