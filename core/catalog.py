"""Installing and updating mods and plugins from Modrinth and Hangar.

  * Fabric servers search Modrinth; Paper servers Modrinth and Hangar.
  * Install picks the newest release (else beta) build for the server's loader and Minecraft
    version, downloads it from the source's CDN, checks its hash, and installs it through
    core/mods.py exactly like a dropped jar (so a wrong kind of jar is still refused). Required
    dependencies that aren't installed yet are installed the same way first.
  * Where each jar came from is kept in <server>/.redstonepanel-mods.json, so the list can say
    which have updates. Jars added by hand are recognised by their SHA-512 on Modrinth.
  * The catalog's home page is a hand-picked starter set and bundles (core/featured.py), checked
    against the server's loader and Minecraft version from Modrinth's project data. Search can
    filter by category and sort by downloads, follows or date.
  * Updating renames the old jar to <file>.previous (which neither loader loads) before
    installing the new one; "Undo" puts the last batch back. Optionally takes a world backup
    first (kind "pre-change").
"""
import hashlib
import json
import os
import re
import threading
import time

from core import backups, featured, files, hangar, manager, modrinth, mods
from core.sources import SourceError, download

TRACK_FILE = ".redstonepanel-mods.json"
PREVIOUS = ".previous"
MAX_PLAN = 12                  # jars installed by one click, dependencies included
MAX_DEPTH = 4
SOURCES = {"fabric": ["modrinth"], "paper": ["modrinth", "hangar"]}
SOURCE_NAMES = {"modrinth": "Modrinth", "hangar": "Hangar"}
JAR_NAME_RE = re.compile(r"^[^/\\\x00]+\.jar$", re.I)

_lock = threading.Lock()
_hash_cache = {}               # (server, folder, file, size, mtime) -> sha512


# ---------------------------------------------------------------------------
# The tracking file
# ---------------------------------------------------------------------------
def read_tracking(server):
    try:
        fd, _ = files._open_file(server, TRACK_FILE)
    except files.FileError:
        return {"files": {}, "previous": None}
    try:
        with os.fdopen(fd, "rb") as f:
            data = json.loads(f.read(1 << 20).decode("utf-8", "replace"))
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data["files"] = data.get("files") if isinstance(data.get("files"), dict) else {}
    data.setdefault("previous", None)
    return data


def _save_tracking(server, data):
    dfd = files._open_root(server)
    try:
        files._write_into(dfd, TRACK_FILE, [json.dumps(data, indent=1, sort_keys=True).encode()], overwrite=True)
    finally:
        os.close(dfd)


def _record(pick):
    f = pick.get("file") or {}
    return {k: v for k, v in {"source": pick["source"], "project": pick["project"], "slug": pick.get("slug"),
                              "title": pick.get("title"), "version_id": pick.get("version_id"),
                              "version": pick.get("version"), "hash": f.get("hash"),
                              "algorithm": f.get("algorithm")}.items() if v is not None}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _java_kind(server):
    kind, minecraft = mods.server_kind(server)
    if kind not in SOURCES:
        what = "Bedrock servers" if kind == "bedrock" else "Vanilla servers"
        raise mods.ModError(f"{what} can't load mods or plugins from Modrinth or Hangar.")
    return kind, minecraft


def _check_source(kind, source):
    if source not in SOURCES[kind]:
        raise mods.ModError(f"{SOURCE_NAMES.get(source, source)} isn't a source for "
                            f"{'Fabric' if kind == 'fabric' else 'Paper'} servers.")


def _sha512(server, folder, item):
    key = (server, folder, item["file"], item["size"], item["modified"])
    if key in _hash_cache:
        return _hash_cache[key]
    fd, _ = files._open_file(server, f"{folder}/{item['file']}")
    h = hashlib.sha512()
    with os.fdopen(fd, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    if len(_hash_cache) > 5000:
        _hash_cache.clear()
    _hash_cache[key] = h.hexdigest()
    return _hash_cache[key]


def _resolve(source, project, kind, minecraft):
    if source == "modrinth":
        return modrinth.resolve(project, kind, minecraft)
    return hangar.resolve(project, minecraft)


def _hosts(source):
    return modrinth.HOSTS if source == "modrinth" else hangar.HOSTS


def _installed(server):
    """(list from core/mods, tracking data, {(source, project)}, {lower-case mod/plugin ids})."""
    listing = mods.list_installed(server)
    tracking = read_tracking(server)
    present = {i["file"] for i in listing["items"]}
    projects = {(t.get("source"), t.get("project")) for f, t in tracking["files"].items() if f in present}
    projects |= {(t.get("source"), t.get("slug")) for f, t in tracking["files"].items() if f in present}
    ids = set()
    for i in listing["items"]:
        for x in [i.get("id")] + list(i.get("provides") or []):
            if x:
                ids.add(str(x).lower())
    return listing, tracking, projects, ids


def _is_installed(pick_or_ref, projects, ids):
    src, proj = pick_or_ref["source"], pick_or_ref.get("project")
    if (src, proj) in projects or (src, pick_or_ref.get("slug")) in projects:
        return True
    for name in (pick_or_ref.get("slug"), pick_or_ref.get("name"), pick_or_ref.get("title")):
        if name and str(name).lower() in ids:
            return True
    return False


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------
def _filters(source):
    return {"categories": [{"id": i, "label": l} for i, l in featured.CATEGORIES[source]],
            "sorts": [{"id": i, "label": l} for i, l in featured.SORTS]}


def search(server, source="modrinth", query="", offset=0, category="", sort=""):
    kind, minecraft = _java_kind(server)
    _check_source(kind, source)
    query = (query or "").strip()[:100]
    category = category or None
    if category and category not in {c for c, _ in featured.CATEGORIES[source]}:
        raise mods.ModError(f"Unknown category '{category}'.")
    sort = sort or None
    if sort and sort not in {s for s, _ in featured.SORTS}:
        raise mods.ModError(f"Unknown sort order '{sort}'.")
    out = modrinth.search(kind, minecraft, query, offset, category=category, sort=sort) if source == "modrinth" \
        else hangar.search(minecraft, query, offset, category=category, sort=sort)
    _, _, projects, ids = _installed(server)
    for h in out["hits"]:
        h["installed"] = _is_installed(h, projects, ids)
    return {"source": source, "sources": SOURCES[kind], "kind": kind, "minecraft": minecraft,
            "category": category, "sort": sort, **_filters(source), **out}


# ---------------------------------------------------------------------------
# The catalog: starter set, bundles, details
# ---------------------------------------------------------------------------
def home(server):
    """The hand-picked groups and bundles for this server, each project as a card that says
    whether it's installed and whether it has a build for this server."""
    kind, minecraft = _java_kind(server)
    cards = {}
    for p in modrinth.catalog_projects(featured.slugs(kind)):
        cards[p.get("slug")] = modrinth.card(p, kind, minecraft)
    _, _, projects, ids = _installed(server)
    for c in cards.values():
        c["installed"] = _is_installed(c, projects, ids)
    groups = []
    for gid, title, slugs in featured.GROUPS.get(kind, []):
        items = [cards[s] for s in slugs if s in cards]
        # a project with no build for this loader at all isn't shown; one not updated for this
        # Minecraft version yet is, marked
        items = [c for c in items if c["fits"] or c["not_fit"] and not c["not_fit"].startswith("No ")]
        if items:
            groups.append({"id": gid, "title": title, "items": items})
    bundles = []
    for bid, title, desc, slugs in featured.BUNDLES.get(kind, []):
        items = [cards[s] for s in slugs if s in cards]
        bundles.append({"id": bid, "title": title, "description": desc, "items": items,
                        "missing": [c["title"] for c in items if not c["installed"] and c["fits"]],
                        "unavailable": [c["title"] for c in items if not c["fits"]]})
    return {"kind": kind, "minecraft": minecraft, "sources": SOURCES[kind], "groups": groups,
            "bundles": bundles, **_filters("modrinth")}


def details(server, source, project):
    """A project's page in short, and exactly what Install would put on this server: the build,
    and the dependencies it would add."""
    kind, minecraft = _java_kind(server)
    _check_source(kind, source)
    if not project or len(str(project)) > 100:
        raise mods.ModError("Choose a project.")
    info = modrinth.details(str(project), kind) if source == "modrinth" else hangar.details(str(project))
    _, _, projects, ids = _installed(server)
    info["installed"] = _is_installed(info, projects, ids)
    try:
        pick = _resolve(source, str(project), kind, minecraft)
        plan, notes = _plan(kind, minecraft, pick, projects, ids)
    except SourceError as e:
        info["install"] = {"error": str(e)}
        return info
    f = pick.get("file") or {}
    info["install"] = {"version": pick.get("version"), "channel": pick.get("channel"),
                       "file": f.get("filename"), "size": f.get("size"),
                       "also": [{"title": p.get("title") or p.get("slug") or p["project"], "version": p.get("version")}
                                for p in plan if p is not pick],
                       "notes": notes}
    return info


def install_bundle(server, bundle, backup=False, actor="?"):
    """Installs every project of a bundle that isn't installed yet (with what each needs), after
    an optional world backup. One that can't be installed doesn't stop the others."""
    kind, minecraft = _java_kind(server)
    found = next((b for b in featured.BUNDLES.get(kind, []) if b[0] == bundle), None)
    if not found:
        raise mods.ModError(f"No bundle '{bundle}' for {'Fabric' if kind == 'fabric' else 'Paper'} servers.")
    _, title, _, slugs = found
    with _lock:
        _, _, projects, ids = _installed(server)
        todo, skipped, failed, notes = [], [], [], []
        for slug in slugs:
            try:
                pick = _resolve("modrinth", slug, kind, minecraft)
            except SourceError as e:
                failed.append({"project": slug, "error": str(e)})
                continue
            if _is_installed(pick, projects, ids):
                skipped.append(pick.get("title") or slug)
                continue
            todo.append(pick)
        if not todo:
            return {"bundle": title, "installed": [], "skipped": skipped, "failed": failed, "notes": notes,
                    "backup": None, "restart": False}
        pre = backups.run_backup(server, "pre-change")["file"] if backup else None
        done = []
        for pick in todo:
            _, _, projects, ids = _installed(server)
            if _is_installed(pick, projects, ids):      # came in as another one's dependency
                continue
            try:
                plan, more = _plan(kind, minecraft, pick, projects, ids)
                notes.extend(more)
                for p in plan:
                    done.append(_install_pick(server, p, actor))
            except (SourceError, files.FileError, OSError) as e:
                failed.append({"project": pick.get("title") or pick["project"], "error": str(e)})
    manager.write_log(f"[MODS] {actor} installed the {title} bundle on {server}: "
                      + (", ".join(d.get("name") or d["file"] for d in done) or "nothing new")
                      + (f"; {len(failed)} failed" if failed else "") + (f"; world backup {pre}" if pre else "") + ".")
    return {"bundle": title, "installed": done, "skipped": skipped, "failed": failed, "notes": notes,
            "backup": pre, "restart": bool(done)}


# ---------------------------------------------------------------------------
# Install (with dependencies)
# ---------------------------------------------------------------------------
def _plan(kind, minecraft, first, projects, ids):
    """The picks to install, dependencies first, skipping what's installed."""
    order, seen, notes = [], set(), list(first.get("notes") or [])

    def visit(pick, depth):
        key = (pick["source"], pick["project"])
        if key in seen:
            return
        seen.add(key)
        for ref in pick.get("requires") or []:
            if (ref["source"], ref["project"]) in seen or _is_installed(ref, projects, ids):
                continue
            if depth >= MAX_DEPTH or len(order) >= MAX_PLAN:
                raise mods.ModError(f"{pick.get('title') or pick['project']} needs too many other "
                                    f"{'mods' if kind == 'fabric' else 'plugins'} to install in one go.")
            try:
                dep = _resolve(ref["source"], ref["project"], kind, minecraft)
            except SourceError as e:
                notes.append(f"Couldn't install {ref.get('name') or ref['project']}, which "
                             f"{pick.get('title') or pick['project']} needs: {e}")
                continue
            if _is_installed(dep, projects, ids):
                continue
            notes.extend(dep.get("notes") or [])
            visit(dep, depth + 1)
        order.append(pick)

    visit(first, 0)
    return order, notes


def _install_pick(server, pick, actor, overwrite=True):
    f = pick.get("file") or {}
    name = os.path.basename(str(f.get("filename") or ""))
    if not JAR_NAME_RE.match(name):
        raise mods.ModError(f"{pick.get('title') or pick['project']}'s file ({name or 'no name'}) isn't a .jar.")
    fobj = download(f.get("url") or "", _hosts(pick["source"]), f["algorithm"], f.get("hash"), f.get("size"))
    with fobj:
        res = mods.install(server, name, fobj, overwrite=overwrite, actor=actor)
    tracking = read_tracking(server)
    for old in res.get("replaced") or []:
        tracking["files"].pop(old, None)
    tracking["files"][name] = _record(pick)
    _save_tracking(server, tracking)
    manager.write_log(f"[MODS] {name} on {server} came from {SOURCE_NAMES[pick['source']]} "
                      f"({pick.get('slug') or pick['project']} {pick.get('version') or ''}).".replace(" )", ")"))
    return {**res["installed"][0], "fits": res.get("fits"), "replaced": res.get("replaced") or [],
            "source": pick["source"], "project": pick["project"], "channel": pick.get("channel")}


def install(server, source, project, actor="?"):
    kind, minecraft = _java_kind(server)
    _check_source(kind, source)
    if not project or len(str(project)) > 100:
        raise mods.ModError("Choose a project to install.")
    with _lock:
        pick = _resolve(source, str(project), kind, minecraft)
        _, _, projects, ids = _installed(server)
        plan, notes = _plan(kind, minecraft, pick, projects, ids)
        done = []
        for p in plan:
            done.append(_install_pick(server, p, actor))
    return {"installed": done, "notes": notes, "restart": True}


# ---------------------------------------------------------------------------
# Updates
# ---------------------------------------------------------------------------
def _identify(server, kind, listing, tracking):
    """Fills tracking for jars added by hand that Modrinth recognises. Returns
    {file: (record, sha512)} for every jar with a known source."""
    folder = listing["folder"]
    known, unknown = {}, {}
    for i in listing["items"]:
        if i.get("problem") or not i["file"].endswith(".jar"):
            continue
        h = _sha512(server, folder, i)
        rec = tracking["files"].get(i["file"])
        if rec and rec.get("source") == "hangar":
            known[i["file"]] = (rec, h)
        elif rec and rec.get("source") == "modrinth" and rec.get("hash") == h:
            known[i["file"]] = (rec, h)
        else:
            unknown[h] = i["file"]
    if unknown:
        found = modrinth.by_hashes(list(unknown))
        changed = False
        for h, v in found.items():
            if h not in unknown or not isinstance(v, dict):
                continue
            pick = modrinth.normalise(v)
            pick["file"] = {**(pick["file"] or {}), "hash": h}
            rec = {**_record(pick), "matched": True}
            tracking["files"][unknown[h]] = rec
            known[unknown[h]] = (rec, h)
            changed = True
        if changed:
            names = modrinth.projects([r["project"] for r, _ in known.values() if r.get("source") == "modrinth"])
            for r, _ in known.values():
                if r.get("source") == "modrinth" and r["project"] in names:
                    r.setdefault("slug", names[r["project"]]["slug"])
                    r.setdefault("title", names[r["project"]]["title"])
            _save_tracking(server, tracking)
    return known


def _latest(kind, minecraft, known):
    """file -> the newest pick for minecraft, or None when there's no build for it."""
    out = {}
    m_hashes = {h: f for f, (r, h) in known.items() if r.get("source") == "modrinth"}
    latest = modrinth.latest_for(list(m_hashes), kind, minecraft) if m_hashes else {}
    for h, f in m_hashes.items():
        v = latest.get(h)
        out[f] = modrinth.normalise(v) if isinstance(v, dict) and v.get("version_type") != "alpha" else None
    for f, (r, _) in known.items():
        if r.get("source") != "hangar":
            continue
        try:
            out[f] = hangar.resolve(r["project"], minecraft)
        except SourceError as e:
            if e.status != 404:
                raise
            out[f] = None
    return out


def updates(server):
    kind, minecraft = _java_kind(server)
    listing, tracking, _, _ = _installed(server)
    known = _identify(server, kind, listing, tracking)
    latest = _latest(kind, minecraft, known)
    items = {}
    for f, (rec, _) in known.items():
        new = latest.get(f)
        items[f] = {"source": rec["source"], "project": rec["project"], "slug": rec.get("slug"),
                    "title": rec.get("title"), "version": rec.get("version"), "matched": bool(rec.get("matched")),
                    "update": None}
        if new and new.get("version_id") and new["version_id"] != rec.get("version_id"):
            items[f]["update"] = {"version": new.get("version"), "channel": new.get("channel")}
    prev = tracking.get("previous") or None
    return {"items": items, "checked": int(time.time()),
            "unknown": [i["file"] for i in listing["items"] if i["file"] not in known and i["file"].endswith(".jar")],
            "undo": {"at": prev["at"], "files": [p["new"] for p in prev["items"]]} if prev and prev.get("items") else None}


def _clear_previous(server, folder, tracking):
    prev = tracking.get("previous") or {}
    for p in prev.get("items") or []:
        try:
            files.delete(server, f"{folder}/{p['old']}{PREVIOUS}", actor="panel")
        except files.FileError:
            pass
    tracking["previous"] = None


def update(server, chosen, backup=False, actor="?"):
    """Updates the chosen jars (file names; all with updates when empty)."""
    kind, minecraft = _java_kind(server)
    with _lock:
        listing, tracking, _, _ = _installed(server)
        folder = listing["folder"]
        known = _identify(server, kind, listing, tracking)
        latest = _latest(kind, minecraft, known)
        todo = []
        for f, (rec, _) in known.items():
            new = latest.get(f)
            if new and new.get("version_id") and new["version_id"] != rec.get("version_id") \
                    and (not chosen or f in chosen):
                todo.append((f, rec, new))
        if not todo:
            return {"updated": [], "failed": [], "backup": None}
        pre = backups.run_backup(server, "pre-change")["file"] if backup else None
        tracking = read_tracking(server)
        _clear_previous(server, folder, tracking)
        _save_tracking(server, tracking)
        done, failed, previous = [], [], []
        for f, rec, new in todo:
            for k in ("slug", "title"):          # the update lookup doesn't name the project
                if not new.get(k):
                    new[k] = rec.get(k)
            files.rename(server, f"{folder}/{f}", f + PREVIOUS, actor=actor)
            try:
                res = _install_pick(server, new, actor, overwrite=True)
            except (files.FileError, OSError) as e:
                files.rename(server, f"{folder}/{f}{PREVIOUS}", f, actor=actor)
                failed.append({"file": f, "error": str(e)})
                continue
            previous.append({"old": f, "new": res["file"], "record": rec})
            done.append({"file": f, "new": res["file"], "title": rec.get("title"), "from": rec.get("version"),
                         "to": new.get("version")})
        tracking = read_tracking(server)
        tracking["previous"] = {"at": int(time.time()), "items": previous} if previous else None
        _save_tracking(server, tracking)
    manager.write_log(f"[MODS] {actor} updated {len(done)} on {server}: "
                      + ", ".join(f"{d['title'] or d['file']} {d['from']} → {d['to']}" for d in done)
                      + (f"; {len(failed)} failed" if failed else "") + (f"; world backup {pre}" if pre else "") + ".")
    return {"updated": done, "failed": failed, "backup": pre, "restart": bool(done)}


def undo(server, actor="?"):
    """Puts back the jars the last update replaced."""
    kind, _ = _java_kind(server)
    folder = mods.FOLDERS[kind]
    with _lock:
        tracking = read_tracking(server)
        prev = tracking.get("previous") or {}
        if not prev.get("items"):
            raise mods.ModError("There's no update to undo.")
        restored = []
        for p in prev["items"]:
            if p["new"] != p["old"]:
                try:
                    files.delete(server, f"{folder}/{p['new']}", actor=actor)
                except files.FileError as e:
                    if e.status != 404:
                        raise
            elif mods._exists(server, f"{folder}/{p['new']}"):
                files.delete(server, f"{folder}/{p['new']}", actor=actor)
            tracking["files"].pop(p["new"], None)
            files.rename(server, f"{folder}/{p['old']}{PREVIOUS}", p["old"], actor=actor)
            tracking["files"][p["old"]] = p["record"]
            restored.append(p["old"])
        tracking["previous"] = None
        _save_tracking(server, tracking)
    manager.write_log(f"[MODS] {actor} undid the last update on {server}: put back {', '.join(restored)}.")
    return {"restored": restored, "restart": True}


# ---------------------------------------------------------------------------
# Before a Minecraft update
# ---------------------------------------------------------------------------
def compatibility(server, target):
    """Which enabled jars have a build for Minecraft `target` (by their source)."""
    kind, _ = _java_kind(server)
    listing, tracking, _, _ = _installed(server)
    known = _identify(server, kind, listing, tracking)
    latest = _latest(kind, target, known)
    missing, ok = [], []
    for f, (rec, _) in known.items():
        (ok if latest.get(f) else missing).append({"file": f, "title": rec.get("title") or f})
    unknown = [{"file": i["file"], "title": i.get("name") or i["file"]} for i in listing["items"]
               if i["enabled"] and i["file"] not in known]
    return {"target": target, "kind": kind, "missing": missing, "ok": ok, "unknown": unknown}
