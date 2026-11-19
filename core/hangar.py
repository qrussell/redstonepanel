"""Hangar (hangar.papermc.io, no key): Paper plugins. Picks are shaped as in core/modrinth.py;
"project" is the Hangar slug and the hash is SHA-256."""
import urllib.parse

from core.sources import SourceError, get_json

API = "https://hangar.papermc.io/api/v1"
SITE = "https://hangar.papermc.io"
HOSTS = {"hangarcdn.papermc.io"}


def _slug(s):
    return urllib.parse.quote(str(s), safe="")


def search(minecraft, query="", offset=0, limit=20):
    params = {"q": query or "", "platform": "PAPER", "limit": max(1, min(int(limit), 25)),
              "offset": max(0, int(offset))}
    if minecraft:
        params["version"] = minecraft
    if not query:
        params["sort"] = "-downloads"
    d = get_json(f"{API}/projects?{urllib.parse.urlencode(params)}") or {}
    hits = []
    for h in d.get("result") or []:
        ns = h.get("namespace") or {}
        hits.append({"source": "hangar", "project": ns.get("slug") or h.get("name"), "slug": ns.get("slug"),
                     "title": h.get("name"), "author": ns.get("owner"), "description": h.get("description"),
                     "downloads": (h.get("stats") or {}).get("downloads"), "updated": h.get("lastUpdated"),
                     "icon": h.get("avatarUrl"), "url": f"{SITE}/{ns.get('owner')}/{ns.get('slug')}"})
    return {"total": (d.get("pagination") or {}).get("count", len(hits)), "hits": hits}


def _versions(slug, minecraft, channel=None):
    params = {"platform": "PAPER", "limit": 10}
    if minecraft:
        params["platformVersion"] = minecraft
    if channel:
        params["channel"] = channel
    d = get_json(f"{API}/projects/{_slug(slug)}/versions?{urllib.parse.urlencode(params)}")
    if d is None:
        raise SourceError(f"Hangar has no project '{slug}'.", 404)
    return d.get("result") or []


def normalise(slug, v):
    dl = (v.get("downloads") or {}).get("PAPER") or {}
    info = dl.get("fileInfo") or {}
    requires, notes = [], []
    for dep in (v.get("pluginDependencies") or {}).get("PAPER") or []:
        if not dep.get("required"):
            continue
        if dep.get("projectId") is not None:
            requires.append({"source": "hangar", "project": str(dep["projectId"]), "name": dep.get("name")})
        else:
            notes.append(f"Also needs {dep.get('name')}" + (f" ({dep['externalUrl']})" if dep.get("externalUrl") else "")
                         + ", which isn't on Hangar; install it yourself.")
    return {"source": "hangar", "project": slug, "slug": slug, "title": slug, "version_id": str(v.get("id")),
            "version": v.get("name"), "channel": ((v.get("channel") or {}).get("name") or "").lower(),
            "external": dl.get("externalUrl"),
            "file": {"url": dl.get("downloadUrl"), "filename": info.get("name"), "size": info.get("sizeBytes"),
                     "hash": info.get("sha256Hash"), "algorithm": "sha256"} if dl.get("downloadUrl") else None,
            "requires": requires, "notes": notes}


def slug_of(project):
    """A numeric Hangar project id (as dependencies give them) -> its slug."""
    if not str(project).isdigit():
        return project
    p = get_json(f"{API}/projects/{_slug(project)}")
    if not p:
        raise SourceError(f"Hangar has no project {project}.", 404)
    return (p.get("namespace") or {}).get("slug") or p.get("name")


def resolve(project, minecraft):
    slug = slug_of(project)
    versions = _versions(slug, minecraft, "Release") or _versions(slug, minecraft)
    if not versions:
        raise SourceError(f"{slug} has no build for Paper{' ' + minecraft if minecraft else ''} on Hangar.", 404)
    pick = normalise(slug, versions[0])
    if not pick["file"]:
        where = urllib.parse.urlsplit(pick.get("external") or "").hostname or "another site"
        raise SourceError(f"{slug} is downloaded from {where}, not Hangar. Get it there and drop the .jar "
                          f"on this tab.", 400)
    return pick
