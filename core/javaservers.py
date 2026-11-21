"""Java server software: vanilla, Paper and Fabric, a chosen Minecraft version, and the right Java.

Every Java server's jar is <server>/server.jar, whatever the loader, so the systemd unit,
backups, export and import treat them all alike. Which loader and Minecraft version it is
lives in <server>/.redstonepanel-server.json (written at every download):

    {"loader": "paper", "minecraft": "1.21.4", "build": "232", "java_required": 21}

Sources:
- vanilla: Mojang's version manifest (piston-meta.mojang.com), server jar checked by SHA-1.
- Paper: the PaperMC Fill API v3 (fill.papermc.io), latest STABLE build, checked by SHA-256.
- Fabric: Fabric meta (meta.fabricmc.net), the server launcher jar for the latest stable
  loader and installer. Its first start downloads the game and libraries into the folder.

Mojang's data for each version says which Java it needs (javaVersion.majorVersion); the same
number is used for Paper and Fabric. The unit runs that Java from /usr/lib/jvm: the installed
major that matches, else the closest newer one.
"""
import glob
import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request

from core.version import panel_version

LOADERS = {"vanilla": "Vanilla", "paper": "Paper", "fabric": "Fabric"}
INFO_FILE = ".redstonepanel-server.json"
JVM_ROOT = "/usr/lib/jvm"

MOJANG_MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
PAPER_API = "https://fill.papermc.io/v3/projects/paper"
FABRIC_META = "https://meta.fabricmc.net/v2"

RELEASE_RE = re.compile(r"^[0-9]+(\.[0-9]+)+$")     # 1.21.4, 26.3 (no snapshots, -rc, -pre)
TIMEOUT = 30
CACHE_SECONDS = 600


class JavaServerError(ValueError):
    """A version that can't be found or downloaded (message is safe to show)."""


class MissingJavaError(RuntimeError):
    """The Java a Minecraft version needs isn't installed (message names the package, or says
    this system's package sources have no Java new enough)."""

    def __init__(self, required, minecraft=None):
        self.required = required
        self.major = java_major_for(required)          # what to install; None when there's nothing
        self.package = java_package(required)
        what = f"Minecraft {minecraft + ' ' if minecraft else ''}needs Java {required}, which isn't installed. "
        if self.major is None:
            have = ", ".join(map(str, packaged_javas())) or "none"
            super().__init__(what + f"This system's package sources have no Java {required} or newer "
                                    f"(they have: {have}).")
        else:
            super().__init__(what + f"Install it with: sudo apt install {self.package}")


# The OpenJDK versions Ubuntu 24.04 packages (there is no openjdk-16, which 1.17 asks for). Other
# releases package other sets (Debian 13: 21 and 25; Ubuntu 26.10: 8 to 28 without 26), so the
# panel asks apt which ones this machine's package sources have, and falls back to this list
# when apt can't say.
UBUNTU_JAVA = (8, 11, 17, 21, 25)
_JRE_RE = re.compile(r"^openjdk-([0-9]+)-jre-headless$")
_packaged = {"at": 0.0, "majors": None}
_packaged_lock = threading.Lock()


def packaged_javas(refresh=False):
    """The Java versions this machine's apt sources have a JRE package for, e.g. (21, 25) on
    Debian 13. Cached for CACHE_SECONDS; UBUNTU_JAVA when apt-cache isn't there or knows none."""
    with _packaged_lock:
        if not refresh and _packaged["majors"] and time.time() - _packaged["at"] < CACHE_SECONDS:
            return _packaged["majors"]
        majors = ()
        try:
            r = subprocess.run(["apt-cache", "pkgnames", "openjdk-"], capture_output=True, text=True,
                               timeout=30, env=dict(os.environ, LC_ALL="C"))
            majors = tuple(sorted({int(m.group(1)) for m in map(_JRE_RE.match, r.stdout.split())
                                   if m and int(m.group(1)) >= 8}))
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
        _packaged.update(at=time.time(), majors=majors or UBUNTU_JAVA)
        return _packaged["majors"]


def java_major_for(major):
    """The Java version to install for a server needing Java `major`: that one if this system
    packages it, else the closest newer one it packages (None if it has none new enough)."""
    major = int(major)
    return next((m for m in packaged_javas() if m >= major), None)


def java_package(major):
    """The package that provides Java `major`, or the closest newer one this system packages."""
    pick = java_major_for(major)
    return f"openjdk-{pick if pick is not None else int(major)}-jre-headless"


def version_key(v):
    """'1.21.4' < '1.21.10' < '26.3'."""
    return tuple(int(p) for p in str(v).split(".") if p.isdigit())


# ---------------------------------------------------------------------------
# HTTP with a small cache
# ---------------------------------------------------------------------------
_cache = {}
_cache_lock = threading.Lock()


def _get_json(url, cache=True):
    now = time.time()
    if cache:
        with _cache_lock:
            hit = _cache.get(url)
            if hit and now - hit[0] < CACHE_SECONDS:
                return hit[1]
    req = urllib.request.Request(url, headers={"User-Agent": _user_agent(), "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise JavaServerError(f"{_host(url)} answered HTTP {e.code}.")
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise JavaServerError(f"Couldn't reach {_host(url)}: {getattr(e, 'reason', e)}")
    if cache:
        with _cache_lock:
            _cache[url] = (now, data)
    return data


def _user_agent():
    # PaperMC asks API clients to say who they are
    return f"RedstonePanel/{panel_version()} (https://github.com/qrussell/redstonepanel)"


def _host(url):
    return url.split("/")[2]


def _download(url, dest, sha1=None, sha256=None):
    """Downloads url to dest (atomically), checking the checksum when there is one."""
    tmp = dest + ".part"
    h1, h256 = hashlib.sha1(), hashlib.sha256()
    req = urllib.request.Request(url, headers={"User-Agent": _user_agent()})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r, open(tmp, "wb") as f:
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                h1.update(chunk)
                h256.update(chunk)
                f.write(chunk)
    except (urllib.error.URLError, OSError) as e:
        _unlink(tmp)
        raise JavaServerError(f"Downloading from {_host(url)} failed: {getattr(e, 'reason', e)}")
    if (sha1 and h1.hexdigest() != sha1.lower()) or (sha256 and h256.hexdigest() != sha256.lower()):
        _unlink(tmp)
        raise JavaServerError(f"The file from {_host(url)} didn't match its checksum; nothing was changed.")
    os.replace(tmp, dest)


def _unlink(path):
    try:
        os.remove(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------
def _mojang_releases():
    data = _get_json(MOJANG_MANIFEST) or {}
    return [v for v in data.get("versions", []) if v.get("type") == "release"]


def mojang_version(minecraft):
    """Mojang's data for a release: server jar URL and SHA-1, and the Java it needs."""
    entry = next((v for v in _mojang_releases() if v["id"] == minecraft), None)
    if not entry:
        raise JavaServerError(f"Minecraft {minecraft} isn't a Minecraft release.")
    data = _get_json(entry["url"]) or {}
    server = (data.get("downloads") or {}).get("server")
    return {"java_required": int((data.get("javaVersion") or {}).get("majorVersion") or 8),
            "server_url": server and server.get("url"), "sha1": server and server.get("sha1")}


def list_versions(loader):
    """Minecraft releases this loader offers, newest first."""
    if loader == "vanilla":
        return [v["id"] for v in _mojang_releases()]
    if loader == "paper":
        data = _get_json(PAPER_API) or {}
        found = [v for group in (data.get("versions") or {}).values() for v in group if RELEASE_RE.match(v)]
        return sorted(set(found), key=version_key, reverse=True)
    if loader == "fabric":
        data = _get_json(f"{FABRIC_META}/versions/game") or []
        return [v["version"] for v in data if v.get("stable") and RELEASE_RE.match(v.get("version", ""))]
    raise JavaServerError(f"Unknown server type '{loader}'.")


def _paper_build(minecraft):
    """The newest STABLE (or RECOMMENDED) Paper build for a version, or None."""
    builds = _get_json(f"{PAPER_API}/versions/{minecraft}/builds") or []
    for b in sorted(builds, key=lambda b: b.get("id", 0), reverse=True):
        if b.get("channel") in ("STABLE", "RECOMMENDED"):
            dl = (b.get("downloads") or {}).get("server:default")
            if dl and dl.get("url"):
                return {"build": str(b["id"]), "url": dl["url"], "sha256": (dl.get("checksums") or {}).get("sha256")}
    return None


def _latest_stable(items):
    return next((i["version"] for i in items or [] if i.get("stable")), None)


def resolve(loader, minecraft=None):
    """What to download: {loader, minecraft, build, url, sha1|sha256, java_required}.
    Without a version: the newest release this loader has a stable build of."""
    if loader not in LOADERS:
        raise JavaServerError(f"Unknown server type '{loader}'.")
    if minecraft is not None:
        minecraft = str(minecraft).strip()
        if not RELEASE_RE.match(minecraft):
            raise JavaServerError("Choose a Minecraft release such as 1.21.4.")
    versions = list_versions(loader)
    if minecraft and minecraft not in versions:
        raise JavaServerError(f"{LOADERS[loader]} doesn't offer Minecraft {minecraft}.")
    candidates = [minecraft] if minecraft else versions[:5]
    for mc in candidates:
        if loader == "vanilla":
            info = mojang_version(mc)
            if not info["server_url"]:
                if minecraft:
                    raise JavaServerError(f"Mojang has no server download for Minecraft {mc}.")
                continue
            return {"loader": loader, "minecraft": mc, "build": None, "url": info["server_url"],
                    "sha1": info["sha1"], "java_required": info["java_required"]}
        if loader == "paper":
            build = _paper_build(mc)
            if not build:
                if minecraft:
                    raise JavaServerError(f"Paper has no stable build for Minecraft {mc} yet; "
                                          f"choose an older version.")
                continue
            return {"loader": loader, "minecraft": mc, **build, "java_required": mojang_version(mc)["java_required"]}
        # fabric
        fabric_loader = _latest_stable(_get_json(f"{FABRIC_META}/versions/loader"))
        installer = _latest_stable(_get_json(f"{FABRIC_META}/versions/installer"))
        if not fabric_loader or not installer:
            raise JavaServerError("Fabric meta didn't list a stable loader and installer.")
        return {"loader": loader, "minecraft": mc, "build": f"loader {fabric_loader}",
                "url": f"{FABRIC_META}/versions/loader/{mc}/{fabric_loader}/{installer}/server/jar",
                "java_required": mojang_version(mc)["java_required"]}
    raise JavaServerError(f"No {LOADERS[loader]} version is available to download right now.")


# ---------------------------------------------------------------------------
# A server folder
# ---------------------------------------------------------------------------
def read_info(d):
    """The folder's loader and version: the info file, else what the jar says, else {}."""
    try:
        with open(os.path.join(d, INFO_FILE)) as f:
            info = json.load(f)
        if isinstance(info, dict) and info.get("loader") in LOADERS:
            return info
    except (OSError, ValueError):
        pass
    return sniff_jar(os.path.join(d, "server.jar"))


def sniff_jar(jar):
    """Tells vanilla, Paper and Fabric jars apart (for servers made before 1.9, or imported)."""
    import zipfile
    try:
        with zipfile.ZipFile(jar) as z:
            names = set(z.namelist())
            if "install.properties" in names:          # Fabric server launcher
                props = dict(line.split("=", 1) for line in z.read("install.properties").decode().splitlines()
                             if "=" in line)
                mc = props.get("game-version")
                return {"loader": "fabric", "minecraft": mc, "build": props.get("fabric-loader-version")}
            if any(n.startswith("io/papermc/paperclip/") for n in names) or "META-INF/download-context" in names:
                mc = None
                if "version.json" in names:
                    mc = json.loads(z.read("version.json")).get("id")
                return {"loader": "paper", "minecraft": mc}
            if "version.json" in names:
                v = json.loads(z.read("version.json"))
                return {"loader": "vanilla", "minecraft": v.get("id") or v.get("name"),
                        "java_required": v.get("java_version")}
    except Exception:
        pass
    return {}


def install(d, loader, minecraft=None, current=None):
    """Downloads the server jar into d (as server.jar) and records what it is.
    current: the folder's existing info, to refuse a Minecraft downgrade.
    Checks for the Java it needs before downloading. Returns (info, path to java)."""
    target = resolve(loader, minecraft)
    if current and current.get("minecraft") and RELEASE_RE.match(str(current["minecraft"])):
        if version_key(target["minecraft"]) < version_key(current["minecraft"]):
            raise JavaServerError(f"This server runs Minecraft {current['minecraft']}; worlds can't be moved "
                                  f"back to {target['minecraft']}. Choose {current['minecraft']} or newer.")
    java = choose_java(target["java_required"], target["minecraft"])
    os.makedirs(d, exist_ok=True)
    _download(target["url"], os.path.join(d, "server.jar"), sha1=target.get("sha1"), sha256=target.get("sha256"))
    info = {k: target[k] for k in ("loader", "minecraft", "build", "java_required")}
    with open(os.path.join(d, INFO_FILE), "w") as f:
        json.dump(info, f)
    return info, java


def describe(info):
    """'Paper 1.21.4', 'Fabric 1.21.4', '1.21.4' (vanilla), or None."""
    if not info or not info.get("minecraft"):
        return None
    return info["minecraft"] if info.get("loader", "vanilla") == "vanilla" else \
        f"{LOADERS[info['loader']]} {info['minecraft']}"


# ---------------------------------------------------------------------------
# Java
# ---------------------------------------------------------------------------
def _release_major(java_home):
    """Major version from <java_home>/release (JAVA_VERSION="21.0.5" or "1.8.0_432")."""
    try:
        with open(os.path.join(java_home, "release")) as f:
            m = re.search(r'^JAVA_VERSION="([0-9]+)(?:\.([0-9]+))?', f.read(), re.M)
    except OSError:
        return None
    if not m:
        return None
    return int(m.group(2)) if m.group(1) == "1" and m.group(2) else int(m.group(1))


def installed_javas():
    """{major: path to bin/java} for the JREs in /usr/lib/jvm (symlinked duplicates dropped)."""
    found = {}
    for home in sorted(glob.glob(os.path.join(JVM_ROOT, "*"))):
        if os.path.islink(home):
            continue
        java = os.path.join(home, "bin", "java")
        major = _release_major(home)
        if major and os.access(java, os.X_OK):
            found.setdefault(major, java)
    return found


def choose_java(required, minecraft=None):
    """The java for a server needing Java `required`: that major if installed, else the closest
    newer one. Raises MissingJavaError when none is new enough."""
    javas = installed_javas()
    required = int(required or 8)
    newer = sorted(m for m in javas if m >= required)
    if not newer:
        raise MissingJavaError(required, minecraft)
    return javas[newer[0]]


def java_major_of(path):
    """Major version of a java binary in a unit's ExecStart (None if unknown)."""
    real = os.path.realpath(path)
    if real.endswith("/bin/java"):
        return _release_major(os.path.dirname(os.path.dirname(real)))
    return None


def requirement(minecraft):
    """What the UI shows for a version: the Java it needs and whether it's installed."""
    need = mojang_version(minecraft)["java_required"]
    javas = installed_javas()
    newer = sorted(m for m in javas if m >= need)
    major = java_major_for(need)
    return {"minecraft": minecraft, "java_required": need, "java_used": newer[0] if newer else None,
            "installed": sorted(javas), "package": java_package(need), "package_major": major,
            "packaged": list(packaged_javas())}
