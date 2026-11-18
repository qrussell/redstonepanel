#!/usr/bin/env python3
"""Publishes a RedstonePanel .deb as a GitLab release. Run by GitLab CI (.gitlab-ci.yml).

    python3 packaging/gitlab-release.py vX.Y.Z [path/to/redstonepanel_X.Y.Z_all.deb]

1. Uploads the .deb to the project's generic package registry
   (packages/generic/redstonepanel/X.Y.Z/), unless that version already has it.
2. Creates the GitLab release for the tag, or updates it if it exists, with
   release-notes/vX.Y.Z.md as its description and the .deb attached as a package link.

Without a .deb path it downloads the one attached to the GitHub release of the same tag
(GITHUB_REPO, default qrussell/redstonepanel); that is how older releases are backfilled.
When release-notes/vX.Y.Z.md is missing, the GitHub release's description is used.

Authenticates with the CI job token (CI_JOB_TOKEN), so it only runs inside a pipeline.
Standard library only.
"""
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

PACKAGE = "redstonepanel"
TAG_RE = re.compile(r"v(\d+\.\d+\.\d+)")


def env(name):
    value = os.environ.get(name)
    if not value:
        sys.exit(f"{name} is not set; run this in a GitLab CI job.")
    return value


API = env("CI_API_V4_URL")
PROJECT = env("CI_PROJECT_ID")
TOKEN = env("CI_JOB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "qrussell/redstonepanel")


def gitlab(method, path, body=None, data=None, ok=(200, 201)):
    """Calls the GitLab API; returns (status, parsed JSON or None)."""
    headers = {"JOB-TOKEN": TOKEN}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{API}/projects/{PROJECT}/{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    if status not in ok:
        sys.exit(f"{method} {path}: HTTP {status}: {raw[:500].decode(errors='replace')}")
    try:
        return status, json.loads(raw) if raw else None
    except ValueError:
        return status, None


def github(path):
    req = urllib.request.Request(f"https://api.github.com/repos/{GITHUB_REPO}/{path}",
                                 headers={"Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def download(url, dest):
    with urllib.request.urlopen(url, timeout=300) as r, open(dest, "wb") as f:
        while chunk := r.read(1 << 16):
            f.write(chunk)


def main():
    if len(sys.argv) not in (2, 3):
        sys.exit(__doc__)
    tag = sys.argv[1]
    m = TAG_RE.fullmatch(tag)
    if not m:
        sys.exit(f"{tag} is not a vX.Y.Z tag.")
    version = m.group(1)
    filename = f"{PACKAGE}_{version}_all.deb"

    gh_release = None
    if len(sys.argv) == 3:
        deb = sys.argv[2]
    else:
        gh_release = github(f"releases/tags/{tag}")
        asset = next((a for a in gh_release.get("assets", []) if a["name"] == filename), None)
        if not asset:
            sys.exit(f"The GitHub release {tag} has no {filename}.")
        deb = os.path.join(tempfile.mkdtemp(), filename)
        print(f"Downloading {asset['browser_download_url']}")
        download(asset["browser_download_url"], deb)
    if os.path.basename(deb) != filename:
        sys.exit(f"{deb} is not {filename}; the tag and the package version differ.")

    # 1. The package registry keeps the .deb for good (job artifacts expire)
    pkg_path = f"packages/generic/{PACKAGE}/{version}/{filename}"
    status, _ = gitlab("GET", pkg_path, ok=(200, 404))
    if status == 200:
        print(f"Package registry already has {PACKAGE} {version}/{filename}")
    else:
        with open(deb, "rb") as f:
            gitlab("PUT", pkg_path, data=f.read())
        print(f"Uploaded {filename} to the package registry ({PACKAGE} {version})")
    package_url = f"{API}/projects/{PROJECT}/{pkg_path}"

    # 2. The release
    notes_file = os.path.join("release-notes", f"{tag}.md")
    if os.path.isfile(notes_file):
        with open(notes_file, encoding="utf-8") as f:
            notes = f.read()
    else:
        if gh_release is None:
            try:
                gh_release = github(f"releases/tags/{tag}")
            except urllib.error.URLError:
                gh_release = {}
        notes = (gh_release.get("body") or "").strip() or f"RedstonePanel {version}"
        print(f"No {notes_file}; using the GitHub release's description.")

    link = {"name": filename, "url": package_url, "link_type": "package",
            "direct_asset_path": f"/{filename}"}
    q_tag = urllib.parse.quote(tag, safe="")
    status, release = gitlab("GET", f"releases/{q_tag}", ok=(200, 403, 404))
    if status != 200:
        gitlab("POST", "releases", {"tag_name": tag, "name": f"RedstonePanel {version}", "description": notes,
                                    "assets": {"links": [link]}})
        print(f"Created the GitLab release {tag}")
    else:
        gitlab("PUT", f"releases/{q_tag}", {"name": f"RedstonePanel {version}", "description": notes})
        if any(l.get("name") == filename for l in release.get("assets", {}).get("links", [])):
            print(f"Updated the GitLab release {tag} (it already links {filename})")
        else:
            gitlab("POST", f"releases/{q_tag}/assets/links", link)
            print(f"Updated the GitLab release {tag} and attached {filename}")
    print(f"{os.environ.get('CI_PROJECT_URL', '')}/-/releases/{tag}")


if __name__ == "__main__":
    main()
