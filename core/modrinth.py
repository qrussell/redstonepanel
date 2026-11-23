"""Modrinth (api.modrinth.com, no key): Fabric, Quilt, Forge and NeoForge mods and
Paper/Purpur/Spigot/Bukkit plugins.

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
# What each kind of server loads (Quilt also loads Fabric mods; Purpur is Paper with extras)
LOADERS = {"fabric": ["fabric"], "quilt": ["quilt", "fabric"], "forge": ["forge"], "neoforge": ["neoforge"],
           "paper": ["paper", "spigot", "bukkit"], "purpur": ["purpur", "paper", "spigot", "bukkit"]}
PROJECT_TYPE = {"fabric": "mod", "quilt": "mod", "forge": "mod", "neoforge": "mod",
                "paper": "plugin", "purpur": "plugin"}
NAMES = {"fabric": "Fabric", "quilt": "Quilt", "forge": "Forge", "neoforge": "NeoForge", "paper": "Paper",
         "purpur": "Purpur"}
CHANNELS = ("release", "beta")          # never alpha
SORTS = {"downloads": "downloads", "follows": "follows", "updated": "updated", "newest": "newest"}
CATALOG_TTL = 3600                      # project details for the catalog change slowly
# Modrinth lists loaders among a project's categories; the catalog shows only real categories
LOADER_TAGS = {"fabric", "quilt", "forge", "neoforge", "paper", "spigot", "bukkit", "folia", "purpur",
               "sponge", "velocity", "bungeecord", "waterfall", "datapack", "liteloader", "modloader", "rift",
               "babric", "bta-babric", "java-agent", "legacy-fabric", "nilloader", "ornithe", "minecraft"}


def _q(v):
    return urllib.parse.quote(json.dumps(v, separators=(",", ":")))


def search(kind, minecraft, query="", offset=0, limit=20, category=None, sort=None):
    """Projects for this kind of server and Minecraft version that run on a server. category: a
    Modrinth category (core/featured.py); sort: a key of SORTS (default: relevance when there's
    a query, else downloads)."""
    facets = [[f"categories:{l}" for l in LOADERS[kind]], [f"project_type:{PROJECT_TYPE[kind]}"],
              ["server_side:required", "server_side:optional"]]
    if minecraft:
        facets.append([f"versions:{minecraft}"])
    if category:
        facets.append([f"categories:{category}"])
    index = SORTS.get(sort) or ("relevance" if query else "downloads")
    params = {"query": query or "", "limit": max(1, min(int(limit), 50)), "offset": max(0, int(offset)),
              "index": index}
    url = f"{API}/search?{urllib.parse.urlencode(params)}&facets={_q(facets)}"
    d = get_json(url) or {}
    hits = []
    for h in d.get("hits") or []:
        hits.append({"source": "modrinth", "project": h.get("project_id"), "slug": h.get("slug"),
                     "title": h.get("title"), "author": h.get("author"), "description": h.get("description"),
                     "downloads": h.get("downloads"), "follows": h.get("follows"),
                     "updated": h.get("date_modified"), "icon": h.get("icon_url"),
                     "categories": [c for c in h.get("display_categories") or h.get("categories") or []
                                    if c not in LOADER_TAGS],
                     "url": project_url(kind, h.get("slug"))})
    return {"total": d.get("total_hits", len(hits)), "hits": hits}


def project_url(kind, slug):
    return f"{SITE}/{PROJECT_TYPE.get(kind, 'mod')}/{slug}"


def catalog_projects(slugs):
    """Full project objects for several slugs or ids at once (cached for an hour), in the
    order given; unknown ones are left out."""
    slugs = [s for s in slugs if s]
    if not slugs:
        return []
    found = get_json(f"{API}/projects?ids={_q(sorted(set(slugs)))}", ttl=CATALOG_TTL) or []
    by = {}
    for p in found:
        by[p.get("slug")] = p
        by[p.get("id")] = p
    return [by[s] for s in slugs if s in by]


def card(p, kind, minecraft):
    """A full project object as a catalog card: fits says whether it has builds for this loader
    and Minecraft version (from the project's own lists)."""
    loaders = set(p.get("loaders") or [])
    has_loader = bool(loaders & set(LOADERS[kind]))
    has_version = not minecraft or minecraft in (p.get("game_versions") or [])
    reason = None
    if not has_loader:
        reason = f"No {NAMES[kind]} build"
    elif not has_version:
        reason = f"Not for {minecraft} yet"
    return {"source": "modrinth", "project": p.get("id"), "slug": p.get("slug"), "title": p.get("title"),
            "description": p.get("description"), "downloads": p.get("downloads"),
            "follows": p.get("followers"), "updated": p.get("updated"), "icon": p.get("icon_url"),
            "categories": [c for c in p.get("categories") or [] if c not in LOADER_TAGS],
            "url": project_url(kind, p.get("slug")), "fits": reason is None, "not_fit": reason,
            "server_side": p.get("server_side"), "client_side": p.get("client_side")}


def details(project, kind):
    """What a project page says, for the catalog's details view."""
    p = _project(project)
    links = {k: p.get(k) for k in ("source_url", "issues_url", "wiki_url", "discord_url") if p.get(k)}
    lic = p.get("license") or {}
    return {**card(p, kind, None), "license": lic.get("name") or lic.get("id"), "links": links,
            "game_versions": (p.get("game_versions") or [])[-6:], "published": p.get("published"),
            "gallery": [g.get("url") for g in (p.get("gallery") or [])[:3] if g.get("url")]}


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


def project_sides(ids):
    """id -> {"slug", "title", "client_side", "server_side"}: which side of the game each
    project runs on (for player packs)."""
    ids = sorted(set(i for i in ids if i))
    out = {}
    for at in range(0, len(ids), 100):
        for p in get_json(f"{API}/projects?ids={_q(ids[at:at + 100])}") or []:
            out[p["id"]] = {"slug": p.get("slug"), "title": p.get("title"),
                            "client_side": p.get("client_side"), "server_side": p.get("server_side")}
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
        what = NAMES[kind]
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


# ---------------------------------------------------------------------------
# Modpacks (1.20)
# ---------------------------------------------------------------------------
PACK_LOADERS = ("fabric", "quilt", "forge", "neoforge")


def search_modpacks(query="", offset=0, limit=20, loader=None, minecraft=None, sort=None):
    """Modpacks that can run on a server, for New server > From a modpack."""
    facets = [["project_type:modpack"], ["server_side:required", "server_side:optional"],
              [f"categories:{l}" for l in ([loader] if loader in PACK_LOADERS else PACK_LOADERS)]]
    if minecraft:
        facets.append([f"versions:{minecraft}"])
    index = SORTS.get(sort) or ("relevance" if query else "downloads")
    params = {"query": query or "", "limit": max(1, min(int(limit), 50)), "offset": max(0, int(offset)),
              "index": index}
    d = get_json(f"{API}/search?{urllib.parse.urlencode(params)}&facets={_q(facets)}") or {}
    hits = []
    for h in d.get("hits") or []:
        cats = h.get("categories") or []
        hits.append({"project": h.get("project_id"), "slug": h.get("slug"), "title": h.get("title"),
                     "author": h.get("author"), "description": h.get("description"),
                     "downloads": h.get("downloads"), "follows": h.get("follows"),
                     "updated": h.get("date_modified"), "icon": h.get("icon_url"),
                     "loaders": [l for l in PACK_LOADERS if l in cats],
                     "latest_minecraft": (h.get("versions") or [None])[-1],
                     "url": f"{SITE}/modpack/{h.get('slug')}"})
    return {"total": d.get("total_hits", len(hits)), "hits": hits}


def modpack_versions(project, limit=40):
    """A modpack's versions, newest first: [{id, name, version, channel, minecraft, loaders,
    published, file}] where file is its .mrpack (None when it has none)."""
    p = _project(project)
    if p.get("project_type") != "modpack":
        raise SourceError(f"{p.get('title') or project} isn't a modpack.", 400)
    versions = get_json(f"{API}/project/{urllib.parse.quote(p['id'], safe='')}/version") or []
    out = []
    for v in versions:
        fs = [f for f in v.get("files") or [] if str(f.get("filename", "")).lower().endswith(".mrpack")]
        f = next((f for f in fs if f.get("primary")), fs[0] if fs else None)
        if not f:
            continue
        out.append({"id": v.get("id"), "name": v.get("name"), "version": v.get("version_number"),
                    "channel": v.get("version_type"), "minecraft": v.get("game_versions") or [],
                    "loaders": [l for l in v.get("loaders") or [] if l in PACK_LOADERS],
                    "published": v.get("date_published"),
                    "file": {"url": f.get("url"), "filename": f.get("filename"), "size": f.get("size"),
                             "hash": (f.get("hashes") or {}).get("sha512"), "algorithm": "sha512"}})
        if len(out) >= limit:
            break
    return {"project": p["id"], "slug": p.get("slug"), "title": p.get("title"), "icon": p.get("icon_url"),
            "description": p.get("description"), "server_side": p.get("server_side"),
            "url": f"{SITE}/modpack/{p.get('slug')}", "versions": out}
