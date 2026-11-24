"""Java server software: vanilla, Paper, Purpur, Fabric, Quilt, Forge and NeoForge, a chosen
Minecraft version, and the right Java.

Vanilla, Paper, Purpur and Fabric are one jar, <server>/server.jar, so the systemd unit,
backups, export and import treat them all alike. Quilt, Forge and NeoForge (1.20) come as an
installer, which is run as the minecraft user and leaves a libraries/ folder and something
else to start: an arguments file (@libraries/.../unix_args.txt), or a launch jar. Which loader
and Minecraft version a folder is, and what starts it, lives in
<server>/.redstonepanel-server.json (written at every download):

    {"loader": "paper", "minecraft": "1.21.4", "build": "232", "java_required": 21}
    {"loader": "neoforge", "minecraft": "1.21.1", "build": "21.1.200", "java_required": 21,
     "launch": "@libraries/net/neoforged/neoforge/21.1.200/unix_args.txt"}

Sources:
- vanilla: Mojang's version manifest (piston-meta.mojang.com), server jar checked by SHA-1.
- Paper: the PaperMC Fill API v3 (fill.papermc.io), latest STABLE build, checked by SHA-256.
- Fabric: Fabric meta (meta.fabricmc.net), the server launcher jar for the latest stable
  loader and installer. Its first start downloads the game and libraries into the folder.
- Purpur: api.purpurmc.org, the latest build of a version, checked by MD5 (all it publishes).
- Quilt: meta.quiltmc.org for the installer (SHA-256) and loader versions.
- Forge: files.minecraftforge.net for versions and the recommended build, the installer from
  maven.minecraftforge.net (SHA-1). Minecraft 1.12.2 and newer.
- NeoForge: maven.neoforged.net (SHA-256). Minecraft 1.20.2 and newer.

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
from core.platform import packages, paths, system, TimeoutExpired

LOADERS = {"vanilla": "Vanilla", "paper": "Paper", "purpur": "Purpur", "fabric": "Fabric", "quilt": "Quilt",
           "forge": "Forge", "neoforge": "NeoForge"}
# The two families (1.19): plugins in plugins/ with Paper's world layout, or mods in mods/ with
# the vanilla layout
PLUGIN_LOADERS = ("paper", "purpur")
MOD_LOADERS = ("fabric", "quilt", "forge", "neoforge")
# These come as an installer that's run in the folder, not as a server.jar
INSTALLER_LOADERS = ("quilt", "forge", "neoforge")
INFO_FILE = ".redstonepanel-server.json"
JVM_ROOT = paths.JVM_ROOT

MOJANG_MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
PAPER_API = "https://fill.papermc.io/v3/projects/paper"
FABRIC_META = "https://meta.fabricmc.net/v2"
PURPUR_API = "https://api.purpurmc.org/v2/purpur"
QUILT_META = "https://meta.quiltmc.org/v3"
FORGE_FILES = "https://files.minecraftforge.net/net/minecraftforge/forge"
FORGE_MAVEN = "https://maven.minecraftforge.net/net/minecraftforge/forge"
NEOFORGE_VERSIONS = "https://maven.neoforged.net/api/maven/versions/releases/net/neoforged/neoforge"
NEOFORGE_MAVEN = "https://maven.neoforged.net/releases/net/neoforged/neoforge"
FORGE_OLDEST = (1, 12, 2)        # older installers and file names differ; not offered
QUILT_OLDEST = (1, 14, 4)
INSTALLER_TIMEOUT = 1800         # Forge's installer downloads and patches the game
INSTALLER_JAR = ".redstonepanel-installer.jar"
# What a unit may start instead of "-jar server.jar": only these two shapes (the info file is
# writable by the minecraft user, and this goes into a root-owned unit)
LAUNCH_RE = re.compile(r"^(?:@libraries/[A-Za-z0-9._+/-]+/unix_args\.txt|-jar [A-Za-z0-9._+-]+\.jar)$")

RELEASE_RE = re.compile(r"^[0-9]+(\.[0-9]+)+$")     # 1.21.4, 26.3 (no snapshots, -rc, -pre)
TIMEOUT = 30
CACHE_SECONDS = 600


class JavaServerError(ValueError):
    """A version that can't be found or downloaded (message is safe to show)."""


class MissingJavaError(RuntimeError):
    """The Java a Minecraft version needs isn't installed (message names the package, or says
    this system's package sources have no Java new enough)."""

    def __init__(self, required, minecraft=None, maximum=None):
        self.required = required
        self.major = java_major_for(required)          # what to install; None when there's nothing
        self.package = java_package(required)
        what = f"Minecraft {minecraft + ' ' if minecraft else ''}needs Java {required}, which isn't installed. "
        if maximum and (self.major is None or self.major > int(maximum)):
            # Old Forge (before Minecraft 1.17) doesn't start on a newer Java, so none is substituted
            self.major = None
            have = ", ".join(map(str, packaged_javas())) or "none"
            span = f"Java {required}" if int(maximum) == int(required) else f"Java {required} to {maximum}"
            super().__init__(f"Forge for Minecraft {minecraft or 'this version'} only runs on {span}, which isn't "
                             f"installed, and this system's package sources don't have it (they have: {have}).")
        elif self.major is None:
            have = ", ".join(map(str, packaged_javas())) or "none"
            super().__init__(what + f"This system's package sources have no Java {required} or newer "
                                    f"(they have: {have}).")
        else:
            cmd = system.hint("install-package", self.package)
            super().__init__(what + (f"Install it with: {cmd}" if cmd else "Install it under Supporting services → Java."))


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
            majors = tuple(sorted({int(m.group(1)) for m in map(_JRE_RE.match, packages.names("openjdk-", timeout=30))
                                   if m and int(m.group(1)) >= 8}))
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
        # A fallback is only kept for a minute, so a slow or busy apt is asked again soon
        _packaged.update(at=time.time() - (0 if majors else CACHE_SECONDS - 60), majors=majors or UBUNTU_JAVA)
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


def _get_text(url):
    """A small text file (a checksum next to a download), or None on 404."""
    req = urllib.request.Request(url, headers={"User-Agent": _user_agent()})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.read(4096).decode("ascii", "replace").strip()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise JavaServerError(f"{_host(url)} answered HTTP {e.code}.")
    except (urllib.error.URLError, OSError) as e:
        raise JavaServerError(f"Couldn't reach {_host(url)}: {getattr(e, 'reason', e)}")


def _user_agent():
    # PaperMC asks API clients to say who they are
    return f"RedstonePanel/{panel_version()} (https://github.com/qrussell/redstonepanel)"


def _host(url):
    return url.split("/")[2]


def _download(url, dest, sha1=None, sha256=None, md5=None):
    """Downloads url to dest (atomically), checking the checksum when there is one."""
    tmp = dest + ".part"
    h1, h256, h5 = hashlib.sha1(), hashlib.sha256(), hashlib.md5(usedforsecurity=False)
    req = urllib.request.Request(url, headers={"User-Agent": _user_agent()})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r, open(tmp, "wb") as f:
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                h1.update(chunk)
                h256.update(chunk)
                h5.update(chunk)
                f.write(chunk)
    except (urllib.error.URLError, OSError) as e:
        _unlink(tmp)
        raise JavaServerError(f"Downloading from {_host(url)} failed: {getattr(e, 'reason', e)}")
    if (sha1 and h1.hexdigest() != sha1.lower()) or (sha256 and h256.hexdigest() != sha256.lower()) \
            or (md5 and h5.hexdigest() != md5.lower()):
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


SNAPSHOT_RE = re.compile(r"^[0-9][0-9A-Za-z.+_-]{1,39}$")   # 25w14a, 1.21.5-pre1, 1.21.5-rc1, 26.1-snapshot-1


def list_snapshots():
    """Vanilla snapshots, pre-releases and release candidates newer than the newest release,
    newest first (the current development cycle; older ones were superseded by that release)."""
    versions = (_get_json(MOJANG_MANIFEST) or {}).get("versions", [])
    out = []
    for v in versions:                      # the manifest lists newest first
        if v.get("type") == "release":
            break
        if v.get("type") == "snapshot" and SNAPSHOT_RE.match(v.get("id", "")):
            out.append(v["id"])
    return out


def _mojang_entry(minecraft):
    data = _get_json(MOJANG_MANIFEST) or {}
    return next((v for v in data.get("versions", []) if v.get("id") == minecraft
                 and v.get("type") in ("release", "snapshot")), None)


def is_release(minecraft):
    return bool(RELEASE_RE.match(str(minecraft or "")))


def mojang_version(minecraft):
    """Mojang's data for a release or snapshot: server jar URL and SHA-1, the Java it needs,
    and when it came out."""
    entry = _mojang_entry(minecraft)
    if not entry:
        raise JavaServerError(f"Minecraft {minecraft} isn't a Minecraft release.")
    data = _get_json(entry["url"]) or {}
    server = (data.get("downloads") or {}).get("server")
    return {"java_required": int((data.get("javaVersion") or {}).get("majorVersion") or 8),
            "server_url": server and server.get("url"), "sha1": server and server.get("sha1"),
            "released": entry.get("releaseTime") or ""}


def require_installer_support(loader):
    """Quilt, Forge and NeoForge are set up by their own installer, which is other people's code
    and so runs as the game account. Where that can't be done (the Windows preview), they are
    refused before anything is made."""
    if loader in INSTALLER_LOADERS and not system.can_run_as_game_account():
        raise JavaServerError(f"{LOADERS[loader]} servers aren't in the Windows preview yet. Vanilla, Paper, "
                              f"Purpur and Fabric work here.")


def list_versions(loader):
    """Minecraft releases this loader offers, newest first."""
    require_installer_support(loader)
    if loader == "vanilla":
        return [v["id"] for v in _mojang_releases()]
    if loader == "paper":
        data = _get_json(PAPER_API) or {}
        found = [v for group in (data.get("versions") or {}).values() for v in group if RELEASE_RE.match(v)]
        return sorted(set(found), key=version_key, reverse=True)
    if loader == "fabric":
        data = _get_json(f"{FABRIC_META}/versions/game") or []
        return [v["version"] for v in data if v.get("stable") and RELEASE_RE.match(v.get("version", ""))]
    if loader == "purpur":
        found = [v for v in (_get_json(PURPUR_API) or {}).get("versions") or [] if RELEASE_RE.match(str(v))]
        return sorted(set(found), key=version_key, reverse=True)
    if loader == "quilt":
        data = _get_json(f"{QUILT_META}/versions/game") or []
        return [v["version"] for v in data if v.get("stable") and RELEASE_RE.match(v.get("version", ""))
                and version_key(v["version"]) >= QUILT_OLDEST]
    if loader == "forge":
        found = [v for v in _forge_builds() if RELEASE_RE.match(v) and version_key(v) >= FORGE_OLDEST]
        return sorted(found, key=version_key, reverse=True)
    if loader == "neoforge":
        builds = _neoforge_builds()
        # A version with only beta builds is still offered (NeoForge calls early builds beta)
        return sorted(builds, key=version_key, reverse=True)
    raise JavaServerError(f"Unknown server type '{loader}'.")


# --- Purpur ---
def _purpur_build(minecraft, pinned=None):
    data = _get_json(f"{PURPUR_API}/{minecraft}") or {}
    builds = data.get("builds") or {}
    build = str(pinned or builds.get("latest") or "")
    if not build or (pinned and build not in [str(b) for b in builds.get("all") or []]):
        if pinned:
            raise JavaServerError(f"Purpur has no build {pinned} for Minecraft {minecraft}.")
        return None
    one = _get_json(f"{PURPUR_API}/{minecraft}/{build}") or {}
    if one.get("result") not in (None, "SUCCESS") or not one.get("md5"):
        if pinned:
            raise JavaServerError(f"Purpur build {build} for Minecraft {minecraft} can't be downloaded.")
        return None
    return {"build": build, "url": f"{PURPUR_API}/{minecraft}/{build}/download", "md5": one["md5"]}


# --- Forge ---
def _forge_builds():
    """{Minecraft version: [Forge versions, oldest first]} (files.minecraftforge.net)."""
    data = _get_json(f"{FORGE_FILES}/maven-metadata.json") or {}
    out = {}
    for mc, names in data.items():
        builds = [n[len(mc) + 1:] for n in names or [] if isinstance(n, str) and n.startswith(mc + "-")]
        builds = [b for b in builds if re.fullmatch(r"[0-9]+(\.[0-9]+)+", b)]
        if builds:
            out[mc] = sorted(builds, key=version_key)
    return out


def _forge_pick(minecraft, pinned=None):
    builds = _forge_builds().get(minecraft) or []
    if pinned:
        if pinned not in builds:
            raise JavaServerError(f"Forge has no version {pinned} for Minecraft {minecraft}.")
        return pinned
    promos = (_get_json(f"{FORGE_FILES}/promotions_slim.json") or {}).get("promos") or {}
    for key in (f"{minecraft}-recommended", f"{minecraft}-latest"):
        if promos.get(key) in builds:
            return promos[key]
    return builds[-1] if builds else None


def forge_java_max(minecraft):
    """Forge before Minecraft 1.17 doesn't start on a new Java: 8 only up to 1.12.2, 8 to 11
    for 1.13 to 1.16.5. None from 1.17 on."""
    key = version_key(minecraft)
    if key < (1, 13):
        return 8
    return 11 if key < (1, 17) else None


# --- NeoForge ---
def neoforge_minecraft(version):
    """The Minecraft version a NeoForge version is for: 21.1.200 -> 1.21.1, 20.4.237 -> 1.20.4,
    21.0.167 -> 1.21, 26.1.2.129 -> 26.1.2, 26.3.0.115 -> 26.3. None for anything else."""
    m = re.fullmatch(r"([0-9]+)\.([0-9]+)\.([0-9]+)(?:\.([0-9]+))?(-beta)?", str(version or ""))
    if not m:
        return None
    a, b, c, d = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4)
    if a >= 26:                                   # year versions: 26.<drop>.<patch>.<build>
        if d is None:
            return None
        return f"{a}.{b}" + (f".{c}" if c else "")
    if a < 20 or d is not None:
        return None
    return f"1.{a}" + (f".{b}" if b else "")


def _neoforge_builds():
    """{Minecraft version: [NeoForge versions, oldest first]}."""
    out = {}
    for v in (_get_json(NEOFORGE_VERSIONS) or {}).get("versions") or []:
        mc = neoforge_minecraft(v)
        if mc:
            out.setdefault(mc, []).append(v)
    for mc in out:
        out[mc].sort(key=lambda v: (version_key(v.replace("-beta", ""))))
    return out


def _neoforge_pick(minecraft, pinned=None):
    builds = _neoforge_builds().get(minecraft) or []
    if pinned:
        if pinned not in builds:
            raise JavaServerError(f"NeoForge has no version {pinned} for Minecraft {minecraft}.")
        return pinned
    stable = [b for b in builds if not b.endswith("-beta")]
    return (stable or builds or [None])[-1]


# --- Quilt ---
def _quilt_stable(version):
    return bool(re.fullmatch(r"[0-9]+(\.[0-9]+)+", str(version or "")))


def _quilt_loaders():
    return [i["version"] for i in _get_json(f"{QUILT_META}/versions/loader") or [] if i.get("version")]


def _quilt_installer():
    for i in _get_json(f"{QUILT_META}/versions/installer") or []:
        url, sha = i.get("url"), (i.get("hashes") or {}).get("sha256")
        if url and sha and str(url).startswith("https://maven.quiltmc.org/"):
            return {"installer_url": url, "sha256": sha}
    raise JavaServerError("Quilt meta didn't list an installer.")


def _paper_build(minecraft):
    """The newest STABLE (or RECOMMENDED) Paper build for a version, or None."""
    builds = _get_json(f"{PAPER_API}/versions/{minecraft}/builds") or []
    for b in sorted(builds, key=lambda b: b.get("id", 0), reverse=True):
        if b.get("channel") in ("STABLE", "RECOMMENDED"):
            dl = (b.get("downloads") or {}).get("server:default")
            if dl and dl.get("url"):
                return {"build": str(b["id"]), "url": dl["url"], "sha256": (dl.get("checksums") or {}).get("sha256")}
    return None


def list_builds(loader, minecraft):
    """Builds that can be pinned, newest first: [{"build", "label"}]. Paper: every build of that
    version (experimental ones labelled); Fabric: loader versions (unstable ones labelled)."""
    if loader == "paper":
        if not RELEASE_RE.match(str(minecraft or "")):
            raise JavaServerError("Choose a Minecraft release such as 1.21.4.")
        builds = _get_json(f"{PAPER_API}/versions/{minecraft}/builds") or []
        out = []
        for b in sorted(builds, key=lambda b: b.get("id", 0), reverse=True):
            if (b.get("downloads") or {}).get("server:default", {}).get("url"):
                ch = b.get("channel")
                out.append({"build": str(b["id"]), "label": str(b["id"]) + ("" if ch in ("STABLE", "RECOMMENDED")
                                                                            else f" ({str(ch).lower()})")})
        return out
    if loader == "fabric":
        return [{"build": i["version"], "label": i["version"] + ("" if i.get("stable") else " (unstable)")}
                for i in _get_json(f"{FABRIC_META}/versions/loader") or [] if i.get("version")][:40]
    if loader == "quilt":
        return [{"build": v, "label": v + ("" if _quilt_stable(v) else " (beta)")} for v in _quilt_loaders()][:40]
    if loader in ("purpur", "forge", "neoforge"):
        if not RELEASE_RE.match(str(minecraft or "")):
            raise JavaServerError("Choose a Minecraft release such as 1.21.4.")
        if loader == "purpur":
            all_builds = ((_get_json(f"{PURPUR_API}/{minecraft}") or {}).get("builds") or {}).get("all") or []
            found = sorted({str(b) for b in all_builds if str(b).isdigit()}, key=int, reverse=True)
            return [{"build": b, "label": b} for b in found]
        if loader == "forge":
            pick = _forge_pick(minecraft)
            return [{"build": b, "label": b + (" (recommended)" if b == pick else "")}
                    for b in reversed(_forge_builds().get(minecraft) or [])]
        return [{"build": b, "label": b.replace("-beta", " (beta)")}
                for b in reversed(_neoforge_builds().get(minecraft) or [])]
    raise JavaServerError("Vanilla servers have no builds to choose.")


def _paper_pinned(minecraft, build):
    for b in _get_json(f"{PAPER_API}/versions/{minecraft}/builds") or []:
        if str(b.get("id")) == str(build):
            dl = (b.get("downloads") or {}).get("server:default")
            if dl and dl.get("url"):
                return {"build": str(b["id"]), "url": dl["url"], "sha256": (dl.get("checksums") or {}).get("sha256")}
    raise JavaServerError(f"Paper has no build {build} for Minecraft {minecraft}.")


def _latest_stable(items):
    return next((i["version"] for i in items or [] if i.get("stable")), None)


def resolve(loader, minecraft=None, build=None, snapshot=False):
    """What to download: {loader, minecraft, build, url, sha1|sha256, java_required}.
    Without a version: the newest release this loader has a stable build of.
    build: a Paper build or Fabric loader version to use instead of the newest stable one.
    snapshot: a Vanilla snapshot or pre-release may be named (or, with no version, the newest one)."""
    if loader not in LOADERS:
        raise JavaServerError(f"Unknown server type '{loader}'.")
    if snapshot:
        if loader != "vanilla":
            raise JavaServerError("Snapshots and pre-releases are offered for Vanilla only.")
        if minecraft is None or not RELEASE_RE.match(str(minecraft).strip()):
            snaps = list_snapshots()
            mc = str(minecraft).strip() if minecraft else (snaps[0] if snaps else None)
            if mc is None:
                minecraft = None                  # no snapshot running now: the newest release
            else:
                if mc not in snaps:
                    raise JavaServerError(f"Minecraft {mc} isn't a current snapshot or pre-release.")
                info = mojang_version(mc)
                if not info["server_url"]:
                    raise JavaServerError(f"Mojang has no server download for Minecraft {mc}.")
                return {"loader": loader, "minecraft": mc, "build": None, "url": info["server_url"],
                        "sha1": info["sha1"], "java_required": info["java_required"]}
    if minecraft is not None:
        minecraft = str(minecraft).strip()
        if not RELEASE_RE.match(minecraft):
            raise JavaServerError("Choose a Minecraft release such as 1.21.4.")
    versions = list_versions(loader)
    if minecraft and minecraft not in versions:
        raise JavaServerError(f"{LOADERS[loader]} doesn't offer Minecraft {minecraft}.")
    candidates = [minecraft] if minecraft else versions[:5]
    pinned = build
    if pinned and not minecraft:
        candidates = versions[:1]              # a pin belongs to one version: the newest
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
            build = _paper_pinned(mc, pinned) if pinned else _paper_build(mc)
            if not build:
                if minecraft:
                    raise JavaServerError(f"Paper has no stable build for Minecraft {mc} yet; "
                                          f"choose an older version.")
                continue
            return {"loader": loader, "minecraft": mc, **build, "java_required": mojang_version(mc)["java_required"]}
        if loader == "purpur":
            build = _purpur_build(mc, pinned)
            if not build:
                if minecraft:
                    raise JavaServerError(f"Purpur has no build for Minecraft {mc} that can be downloaded.")
                continue
            return {"loader": loader, "minecraft": mc, **build, "java_required": mojang_version(mc)["java_required"]}
        if loader == "forge":
            build = _forge_pick(mc, pinned)
            if not build:
                if minecraft:
                    raise JavaServerError(f"Forge has no version for Minecraft {mc}.")
                continue
            name = f"{mc}-{build}"
            url = f"{FORGE_MAVEN}/{name}/forge-{name}-installer.jar"
            sha1 = _get_text(url + ".sha1")
            if not sha1 or not re.fullmatch(r"[0-9a-fA-F]{40}", sha1.split()[0]):
                if minecraft:
                    raise JavaServerError(f"Forge {build} for Minecraft {mc} has no installer to download.")
                continue
            return {"loader": loader, "minecraft": mc, "build": build, "url": None, "installer_url": url,
                    "sha1": sha1.split()[0], "java_required": mojang_version(mc)["java_required"],
                    "java_max": forge_java_max(mc)}
        if loader == "neoforge":
            build = _neoforge_pick(mc, pinned)
            if not build:
                if minecraft:
                    raise JavaServerError(f"NeoForge has no version for Minecraft {mc}.")
                continue
            url = f"{NEOFORGE_MAVEN}/{build}/neoforge-{build}-installer.jar"
            sha256 = _get_text(url + ".sha256")
            if not sha256 or not re.fullmatch(r"[0-9a-fA-F]{64}", sha256.split()[0]):
                raise JavaServerError(f"NeoForge {build} has no installer to download.")
            return {"loader": loader, "minecraft": mc, "build": build, "url": None, "installer_url": url,
                    "sha256": sha256.split()[0], "java_required": mojang_version(mc)["java_required"]}
        if loader == "quilt":
            loaders = _quilt_loaders()
            if pinned and pinned not in loaders:
                raise JavaServerError(f"Quilt has no loader version {pinned}.")
            quilt_loader = pinned or next((v for v in loaders if _quilt_stable(v)), None)
            if not quilt_loader:
                raise JavaServerError("Quilt meta didn't list a stable loader.")
            return {"loader": loader, "minecraft": mc, "build": f"loader {quilt_loader}", "url": None,
                    **_quilt_installer(), "java_required": mojang_version(mc)["java_required"]}
        # fabric
        loaders = _get_json(f"{FABRIC_META}/versions/loader") or []
        if pinned and not any(i.get("version") == pinned for i in loaders):
            raise JavaServerError(f"Fabric has no loader version {pinned}.")
        fabric_loader = pinned or _latest_stable(loaders)
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
    with system.game_files():          # the game's folder: read as the game account
        try:
            with open(os.path.join(d, INFO_FILE)) as f:
                info = json.load(f)
            if isinstance(info, dict) and info.get("loader") in LOADERS:
                return info
        except (OSError, ValueError):
            pass
        return sniff_jar(os.path.join(d, "server.jar")) or sniff_folder(d)


def launch_args(d, info=None):
    """What the unit runs after the JVM options: "-jar server.jar", or what a Quilt, Forge or
    NeoForge install recorded ("@libraries/.../unix_args.txt", "-jar quilt-server-launch.jar").
    Anything that isn't one of those two shapes is ignored."""
    info = read_info(d) if info is None else info
    launch = info.get("launch") if isinstance(info, dict) else None
    if info.get("loader") in INSTALLER_LOADERS and isinstance(launch, str) and LAUNCH_RE.match(launch) \
            and ".." not in launch:
        return launch
    return "-jar server.jar"


def launch_file(d, info=None):
    """The file launch_args() points at, inside d."""
    launch = launch_args(d, info)
    return os.path.join(d, launch[1:] if launch.startswith("@") else launch[len("-jar "):])


def runnable(d, info=None):
    """True when the folder has what its unit starts. A backup leaves out server.jar and
    libraries/ (they're downloaded again), so a restored Forge folder isn't runnable until
    its installer has run again."""
    info = read_info(d) if info is None else info
    if not os.path.isfile(launch_file(d, info)):
        return False
    if info.get("loader") in INSTALLER_LOADERS:
        if not os.path.isdir(os.path.join(d, "libraries")):
            return False
        if info.get("loader") == "quilt" and not os.path.isfile(os.path.join(d, "server.jar")):
            return False
    return True


def is_java_folder(d):
    """A Java server folder: it has a server.jar, or the panel recorded its loader."""
    if os.path.isfile(os.path.join(d, "server.jar")):
        return True
    try:
        with system.game_files(), open(os.path.join(d, INFO_FILE)) as f:
            return json.load(f).get("loader") in LOADERS
    except (OSError, ValueError, AttributeError):
        return False


def _find_launch(d, loader, minecraft, build):
    """What a finished Quilt, Forge or NeoForge install starts with, or None."""
    if loader == "quilt":
        return "-jar quilt-server-launch.jar" if os.path.isfile(os.path.join(d, "quilt-server-launch.jar")) else None
    group = "net/neoforged/neoforge" if loader == "neoforge" else "net/minecraftforge/forge"
    name = build if loader == "neoforge" else f"{minecraft}-{build}"
    rel = f"libraries/{group}/{name}/unix_args.txt"
    if os.path.isfile(os.path.join(d, rel)):
        return "@" + rel
    if loader == "forge":                          # before 1.17: a jar next to the vanilla one
        for jar in (f"forge-{name}.jar", f"forge-{name}-universal.jar", f"forge-{name}-shim.jar"):
            if os.path.isfile(os.path.join(d, jar)):
                return "-jar " + jar
    return None


def sniff_folder(d):
    """Tells an installed Forge, NeoForge or Quilt folder without an info file (an import, or a
    server pack): the loader, its version and the Minecraft version, from libraries/."""
    for loader, group in (("neoforge", "net/neoforged/neoforge"), ("forge", "net/minecraftforge/forge")):
        base = os.path.join(d, "libraries", *group.split("/"))
        try:
            names = sorted(n for n in os.listdir(base) if os.path.isfile(os.path.join(base, n, "unix_args.txt")))
        except OSError:
            continue
        for name in reversed(names):
            if loader == "neoforge" and neoforge_minecraft(name):
                info = {"loader": loader, "minecraft": neoforge_minecraft(name), "build": name}
            elif loader == "forge" and re.fullmatch(r"[0-9]+(\.[0-9]+)+-[0-9]+(\.[0-9]+)+", name):
                mc, build = name.split("-", 1)
                info = {"loader": loader, "minecraft": mc, "build": build}
            else:
                continue
            launch = f"@libraries/{group}/{name}/unix_args.txt"
            if LAUNCH_RE.match(launch):
                return {**info, "launch": launch}
    return {}


def _as_minecraft(cmd):
    """Installers are other people's code: they run as the minecraft user, never as root."""
    return system.as_game_account(cmd)


def _run_installer(d, target, java):
    """Downloads the loader's installer into d, checks it, runs it there as the minecraft user
    and clears up after it. Returns what the unit starts (see launch_args)."""
    loader, mc = target["loader"], target["minecraft"]
    require_installer_support(loader)
    build = str(target["build"]).replace("loader ", "")
    jar = os.path.join(d, INSTALLER_JAR)
    _download(target["installer_url"], jar, sha1=target.get("sha1"), sha256=target.get("sha256"))
    if loader == "quilt":
        args = ["install", "server", mc, build, f"--install-dir={d}", "--download-server"]
    elif loader == "neoforge":
        args = ["--install-server", d]
    else:
        args = ["--installServer", d]
    if system.is_admin():
        system.take_for_game_account(d)
    env = dict(os.environ, HOME=d)
    try:
        r = system.run_program(_as_minecraft([java, "-jar", jar] + args), cwd=d, env=env, timeout=INSTALLER_TIMEOUT)
    except TimeoutExpired:
        raise JavaServerError(f"The {LOADERS[loader]} installer didn't finish in "
                              f"{INSTALLER_TIMEOUT // 60} minutes; nothing was changed.")
    except OSError as e:
        raise JavaServerError(f"Couldn't run the {LOADERS[loader]} installer: {e}")
    finally:
        _unlink(jar)
    # Start scripts and their own JVM options aren't used: the unit starts the server, with
    # the memory and flags set in the panel
    for junk in ("run.sh", "run.bat", "user_jvm_args.txt", INSTALLER_JAR + ".log", "installer.log",
                 "README.txt"):
        _unlink(os.path.join(d, junk))
    for name in os.listdir(d):
        if name.endswith("-installer.jar.log"):
            _unlink(os.path.join(d, name))
    launch = _find_launch(d, loader, mc, build) if r.returncode == 0 else None
    if not launch or not LAUNCH_RE.match(launch):
        tail = " ".join((r.stdout + "\n" + r.stderr).strip().splitlines()[-3:])[-400:]
        raise JavaServerError(f"The {LOADERS[loader]} installer failed for Minecraft {mc}"
                              + (f": {tail}" if tail else "."))
    return launch


def adopt(staging, d):
    """Moves a finished download or install from staging into the server folder d: files
    replace the old ones, libraries/ is merged. The old loader's launch jar goes."""
    with system.game_files():          # all of it happens in the game's folder
        old = read_info(d)
        old_launch = launch_file(d, old) if old.get("loader") in INSTALLER_LOADERS else None
        for name in sorted(os.listdir(staging)):
            src, dst = os.path.join(staging, name), os.path.join(d, name)
            if os.path.isdir(src) and not os.path.islink(src):
                if os.path.isdir(dst) and not os.path.islink(dst):
                    import shutil
                    shutil.copytree(src, dst, dirs_exist_ok=True, symlinks=True)
                    shutil.rmtree(src, ignore_errors=True)
                    continue
            os.replace(src, dst)
        new = read_info(d)
        if old_launch and old_launch != launch_file(d, new) and os.path.dirname(old_launch) == d \
                and os.path.basename(old_launch) != "server.jar":
            _unlink(old_launch)


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
                purpur = any(n in names and b"purpur" in z.read(n)[:4096].lower()
                             for n in ("META-INF/versions.list", "META-INF/patches.list"))
                return {"loader": "purpur" if purpur else "paper", "minecraft": mc}
            if "version.json" in names:
                v = json.loads(z.read("version.json"))
                return {"loader": "vanilla", "minecraft": v.get("id") or v.get("name"),
                        "java_required": v.get("java_version")}
    except Exception:
        pass
    return {}


def _older(target, current):
    """True if Minecraft `target` came out before `current` (a downgrade). Releases compare by
    number; a snapshot by Mojang's release time, since its name says nothing about order."""
    if RELEASE_RE.match(str(target)) and RELEASE_RE.match(str(current)):
        return version_key(target) < version_key(current)
    try:
        return mojang_version(target)["released"] < mojang_version(current)["released"]
    except JavaServerError:
        return False


def install(d, loader, minecraft=None, current=None, build=None, snapshot=False, java_major=None):
    """Downloads the server jar into d (as server.jar) and records what it is.
    current: the folder's existing info, to refuse a Minecraft downgrade.
    build/snapshot: see resolve(). java_major: run on this installed Java instead of the
    closest one (refused if it's older than the version needs).
    Checks for the Java it needs before downloading. Returns (info, path to java)."""
    target = resolve(loader, minecraft, build=build, snapshot=snapshot)
    if current and current.get("minecraft") and _older(target["minecraft"], current["minecraft"]):
        raise JavaServerError(f"This server runs Minecraft {current['minecraft']}; worlds can't be moved "
                              f"back to {target['minecraft']}. Choose {current['minecraft']} or newer.")
    java_max = target.get("java_max")
    if java_major:
        java = choose_exact_java(java_major, target["java_required"], target["minecraft"], java_max)
    else:
        java = choose_java(target["java_required"], target["minecraft"], java_max)
    # Everything lands in the game's folder as the game account: a link left there by the
    # game (server.jar.part -> /etc/...) can't send a root write elsewhere. d belongs to it.
    with system.game_files():
        os.makedirs(d, exist_ok=True)
        info = {k: target[k] for k in ("loader", "minecraft", "build", "java_required")}
        if loader in INSTALLER_LOADERS:
            info["launch"] = _run_installer(d, target, java)
            if java_max:
                info["java_max"] = java_max
        else:
            _download(target["url"], os.path.join(d, "server.jar"), sha1=target.get("sha1"),
                      sha256=target.get("sha256"), **({"md5": target["md5"]} if target.get("md5") else {}))
        with open(os.path.join(d, INFO_FILE), "w") as f:
            json.dump(info, f)
    return info, java


def describe(info):
    """'Paper 1.21.4', 'NeoForge 1.21.1', '1.21.4' (vanilla), or None."""
    if not info or not info.get("minecraft"):
        return None
    return info["minecraft"] if info.get("loader", "vanilla") == "vanilla" else \
        f"{LOADERS[info['loader']]} {info['minecraft']}"


# ---------------------------------------------------------------------------
# Java
# ---------------------------------------------------------------------------
def _release_major(java_home):
    """Major version from <java_home>/release (JAVA_VERSION="21.0.5" or "1.8.0_432"). Ubuntu's
    and Debian's Java 8 has no release file (and its java is jre/bin/java), so the folder's
    packaged name (java-8-openjdk-amd64) is the fallback."""
    if os.path.basename(java_home) == "jre":
        java_home = os.path.dirname(java_home)
    try:
        with open(os.path.join(java_home, "release")) as f:
            m = re.search(r'^JAVA_VERSION="([0-9]+)(?:\.([0-9]+))?', f.read(), re.M)
    except OSError:
        m = re.match(r"java-([0-9]+)-openjdk-", os.path.basename(java_home))
        return int(m.group(1)) if m else None
    if not m:
        return None
    return int(m.group(2)) if m.group(1) == "1" and m.group(2) else int(m.group(1))


def installed_javas():
    """{major: path to bin/java} for the JREs in /usr/lib/jvm (symlinked duplicates dropped)."""
    found = {}
    for home in sorted(glob.glob(os.path.join(JVM_ROOT, "*"))):
        if os.path.islink(home):
            continue
        java = os.path.join(home, "bin", paths.JAVA_PROGRAM)
        major = _release_major(home)
        if major and os.access(java, os.X_OK):
            found.setdefault(major, java)
    return found


def choose_java(required, minecraft=None, maximum=None):
    """The java for a server needing Java `required`: that major if installed, else the closest
    newer one (no newer than `maximum`, which old Forge sets). Raises MissingJavaError when
    none fits."""
    javas = installed_javas()
    required = int(required or 8)
    newer = sorted(m for m in javas if m >= required and (not maximum or m <= int(maximum)))
    if not newer:
        raise MissingJavaError(required, minecraft, maximum)
    return javas[newer[0]]


def choose_exact_java(major, required, minecraft=None, maximum=None):
    """The installed Java `major`, which must be at least `required` (and at most `maximum`)."""
    major, required = int(major), int(required or 8)
    if major < required:
        raise JavaServerError(f"Minecraft {minecraft or ''} needs Java {required} or newer; Java {major} can't run it.")
    if maximum and major > int(maximum):
        raise JavaServerError(f"Forge for Minecraft {minecraft or 'this version'} doesn't start on Java {major}; "
                              f"use Java {maximum} or older.")
    javas = installed_javas()
    if major not in javas:
        raise MissingJavaError(major, minecraft)
    return javas[major]


def java_major_of(path):
    """Major version of a java binary in a unit's ExecStart (None if unknown)."""
    real = os.path.realpath(path)
    if real.endswith(os.sep + os.path.join("bin", paths.JAVA_PROGRAM)):
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
