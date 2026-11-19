"""Modrinth (api.modrinth.com, no key): Fabric mods and Paper/Spigot/Bukkit plugins.

A "pick" is the build to install for a server, normalised for both sources:

    {"source": "modrinth", "project": "P7dR8mSH", "slug": "fabric-api", "title": "Fabric API",
     "version_id": "p96k10UR", "version": "0.119.4+1.21.4", "channel": "release",
     "file": {"url": ..., "filename": ..., "size": ..., "hash": ..., "algorithm": "sha512"},
     "requires": [{"source": "modrinth", "project": "..."}], "notes": []}
"""
import json
import urllib.parse

from core.sources import SourceError, get_json

API = "https://api.modrinth.com/v2"
SITE = "https://modrinth.com"
HOSTS = {"cdn.modrinth.com"}
LOADERS = {"fabric": ["fabric"], "paper": ["paper", "spigot", "bukkit"]}
PROJECT_TYPE = {"fabric": "mod", "paper": "plugin"}
CHANNELS = ("release", "beta")          # never alpha


def _q(v):
    return urllib.parse.quote(json.dumps(v, separators=(",", ":")))


def search(kind, minecraft, query="", offset=0, limit=20):
    """Projects for this kind of server and Minecraft version that run on a server."""
    facets = [[f"categories:{l}" for l in LOADERS[kind]], [f"project_type:{PROJECT_TYPE[kind]}"],
              ["server_side:required", "server_side:optional"]]
    if minecraft:
        facets.append([f"versions:{minecraft}"])
    params = {"query": query or "", "limit": max(1, min(int(limit), 50)), "offset": max(0, int(offset)),
              "index": "relevance" if query else "downloads"}
    url = f"{API}/search?{urllib.parse.urlencode(params)}&facets={_q(facets)}"
    d = get_json(url) or {}
    hits = []
    for h in d.get("hits") or []:
        kind_path = "plugin" if kind == "paper" else "mod"
        hits.append({"source": "modrinth", "project": h.get("project_id"), "slug": h.get("slug"),
                     "title": h.get("title"), "author": h.get("author"), "description": h.get("description"),
                     "downloads": h.get("downloads"), "updated": h.get("date_modified"),
                     "icon": h.get("icon_url"), "url": f"{SITE}/{kind_path}/{h.get('slug')}"})
    return {"total": d.get("total_hits", len(hits)), "hits": hits}


def _project(project):
    p = get_json(f"{API}/project/{urllib.parse.quote(project, safe='')}")
    if not p:
        raise SourceError(f"Modrinth has no project '{project}'.", 404)
    return p


def projects(ids):
    """id -> {"slug", "title"} for several projects at once."""
    ids = sorted(set(i for i in ids if i))
    if not ids:
        return {}
    out = {}
    for p in get_json(f"{API}/projects?ids={_q(ids)}") or []:
        out[p["id"]] = {"slug": p.get("slug"), "title": p.get("title")}
    return out


def _primary(version):
    fs = version.get("files") or []
    f = next((f for f in fs if f.get("primary")), fs[0] if fs else None)
    if not f:
        return None
    return {"url": f.get("url"), "filename": f.get("filename"), "size": f.get("size"),
            "hash": (f.get("hashes") or {}).get("sha512"), "algorithm": "sha512"}


def normalise(version, project=None):
    """A Modrinth version object as a pick."""
    requires = []
    for dep in version.get("dependencies") or []:
        if dep.get("dependency_type") != "required":
            continue
        if dep.get("project_id"):
            requires.append({"source": "modrinth", "project": dep["project_id"]})
        elif dep.get("version_id"):
            v = get_json(f"{API}/version/{urllib.parse.quote(dep['version_id'], safe='')}")
            if v and v.get("project_id"):
                requires.append({"source": "modrinth", "project": v["project_id"]})
    project = project or {}
    return {"source": "modrinth", "project": version.get("project_id"), "slug": project.get("slug"),
            "title": project.get("title"), "version_id": version.get("id"),
            "version": version.get("version_number"), "channel": version.get("version_type"),
            "file": _primary(version), "requires": requires, "notes": []}


def _choose(versions):
    for channel in CHANNELS:
        for v in versions:                 # newest first
            if v.get("version_type") == channel and _primary(v):
                return v
    return None


def resolve(project, kind, minecraft):
    """The build of a project to install on this server. Raises SourceError when there's none."""
    p = _project(project)
    params = {"loaders": json.dumps(LOADERS[kind])}
    if minecraft:
        params["game_versions"] = json.dumps([minecraft])
    versions = get_json(f"{API}/project/{urllib.parse.quote(p['id'], safe='')}/version?"
                        f"{urllib.parse.urlencode(params)}") or []
    v = _choose(versions)
    if not v:
        what = {"fabric": "Fabric", "paper": "Paper"}[kind]
        raise SourceError(f"{p.get('title') or project} has no release or beta build for {what}"
                          f"{' ' + minecraft if minecraft else ''}.", 404)
    return normalise(v, {"slug": p.get("slug"), "title": p.get("title")})


def by_hashes(hashes):
    """sha512 -> the Modrinth version that file is (for jars added by hand)."""
    hashes = sorted(set(hashes))
    if not hashes:
        return {}
    return get_json(f"{API}/version_files", body={"hashes": hashes, "algorithm": "sha512"}) or {}


def latest_for(hashes, kind, minecraft):
    """sha512 -> the newest version of the same project for this loader and Minecraft version
    (a hash missing from the answer: no build for it)."""
    hashes = sorted(set(hashes))
    if not hashes:
        return {}
    body = {"hashes": hashes, "algorithm": "sha512", "loaders": LOADERS[kind]}
    if minecraft:
        body["game_versions"] = [minecraft]
    return get_json(f"{API}/version_files/update", body=body) or {}
