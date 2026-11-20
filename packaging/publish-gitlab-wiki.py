#!/usr/bin/env python3
"""Publishes the wiki pages to the project's GitLab wiki through the API.

    python3 packaging/publish-gitlab-wiki.py

Runs in the `wiki` job of .gitlab-ci.yml on main. It builds the pages with
`sync-wiki.py --gitlab`, then creates each page that's missing and updates each one that
changed. Pages made by hand in the GitLab wiki are left alone.

Needs WIKI_TOKEN: a project access token with the `api` scope and at least the Developer role
(Settings → Access tokens), stored as a masked CI/CD variable. CI's own job token can't write
to the wiki. Uses CI_API_V4_URL and CI_PROJECT_ID, which GitLab CI sets.
"""
import importlib.util
import json
import os
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def api(method, path, token, data=None):
    url = os.environ["CI_API_V4_URL"].rstrip("/") + f"/projects/{os.environ['CI_PROJECT_ID']}{path}"
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method, headers={"PRIVATE-TOKEN": token})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"null")


def main():
    token = os.environ.get("WIKI_TOKEN")
    if not token:
        print("WIKI_TOKEN isn't set, so the GitLab wiki wasn't updated. See this script's header.")
        return
    spec = importlib.util.spec_from_file_location("sync_wiki", os.path.join(HERE, "sync-wiki.py"))
    sync = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sync)
    out = tempfile.mkdtemp(prefix="wiki-")
    written = sync.build(out, gitlab=True)
    existing = {p["slug"]: p for p in api("GET", "/wikis?with_content=1", token)}
    for slug, _ in written:
        with open(os.path.join(out, slug + ".md"), encoding="utf-8") as f:
            content = f.read()
        page = existing.get(slug)
        if page and page.get("content", "").rstrip("\n") == content.rstrip("\n"):
            print(f"unchanged  {slug}")
        elif page:
            api("PUT", "/wikis/" + urllib.parse.quote(slug, safe=""), token,
                {"title": page["title"], "content": content, "format": "markdown"})
            print(f"updated    {slug}")
        else:
            api("POST", "/wikis", token, {"title": slug, "content": content, "format": "markdown"})
            print(f"created    {slug}")


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as e:
        sys.exit(f"GitLab API: {e.code} {e.reason}: {e.read()[:300]!r}")
