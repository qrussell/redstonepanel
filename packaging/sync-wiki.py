#!/usr/bin/env python3
"""Copies the wiki pages (docs/wiki/*.md) and the user manual (docs/guide/README.md, published as
User-Manual) into a folder laid out as the project's GitHub or GitLab wiki.

    python3 packaging/sync-wiki.py WIKI_DIR              for the GitHub wiki (a checkout of it)
    python3 packaging/sync-wiki.py --gitlab WIKI_DIR     for the GitLab wiki

Pages are written in the repository so they read correctly there too, so links are rewritten
for the wiki on the way:

- `Installation.md#x` (another wiki page)   -> `Installation#x`
- `../guide/images/a.png`, `images/a.png`  -> the image on raw.githubusercontent.com (GitLab:
                                               the project's /-/raw/ URL), in Markdown links and
                                               in `src="…"`/`srcset="…"` of <img>/<source> tags
- `../../README.md#x`, `../design/x.md`    -> the file on github.com (GitLab: /-/blob/)

On GitLab the home page is `home` and the sidebar `_sidebar`. GITHUB_REPOSITORY (owner/name),
GITLAB_PROJECT_URL and BRANCH pick where links point; they default to qrussell/redstonepanel,
https://gitlab.cielocloud.dev/lab/redstonepanel and main.
.github/workflows/wiki.yml publishes to GitHub, and the `wiki` job in .gitlab-ci.yml (through
packaging/publish-gitlab-wiki.py) to GitLab, on every change to main.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WIKI_SRC = os.path.join(ROOT, "docs", "wiki")
MANUAL = os.path.join(ROOT, "docs", "guide", "README.md")
MANUAL_PAGE = "User-Manual"
GITLAB_NAMES = {"Home": "home", "_Sidebar": "_sidebar"}
SPECIAL = ("_Sidebar", "_Footer")

LINK = re.compile(r"(\]\()([^)\s]+)(\))")
# <img src="…"> and <source srcset="…"> in the <picture> tags that pick the theme's screenshot
ATTR = re.compile(r"""(\b(?:src|srcset)=")([^"\s]+)(")""")


def pages():
    """{page name: source file}: every docs/wiki/*.md, plus the manual."""
    found = {f[:-3]: os.path.join(WIKI_SRC, f) for f in sorted(os.listdir(WIKI_SRC)) if f.endswith(".md")}
    found[MANUAL_PAGE] = MANUAL
    return found


def rewrite(text, src_dir, names, raw, blob, rename=None, gitlab=False):
    rename = rename or {}
    # GitLab turns "World & Maps" into #world-maps where GitHub makes #world--maps
    if gitlab:
        text = re.sub(r"(\]\([^)\s#]*#)([^)\s]+)(\))",
                      lambda m: m.group(1) + re.sub(r"-{2,}", "-", m.group(2)) + m.group(3), text)

    def page(name):
        return rename.get(name, name)

    def fix(m):
        target = m.group(2)
        if re.match(r"^[a-z]+:|^#", target):          # absolute URL, mailto, or an anchor
            return m.group(0)
        path, _, anchor = target.partition("#")
        anchor = "#" + anchor if anchor else ""
        if path.endswith(".md") and "/" not in path and path[:-3] in names:
            return f"{m.group(1)}{page(path[:-3])}{anchor}{m.group(3)}"
        full = os.path.normpath(os.path.join(src_dir, path))
        rel = os.path.relpath(full, ROOT).replace(os.sep, "/")
        if rel.startswith(".."):
            return m.group(0)
        if os.path.normpath(full) == os.path.normpath(MANUAL):
            return f"{m.group(1)}{MANUAL_PAGE}{anchor}{m.group(3)}"
        if os.path.normpath(os.path.dirname(full)) == os.path.normpath(WIKI_SRC) and rel.endswith(".md"):
            return f"{m.group(1)}{page(os.path.basename(rel)[:-3])}{anchor}{m.group(3)}"
        base = raw if re.search(r"\.(png|jpe?g|gif|svg|webp)$", rel, re.I) else blob
        return f"{m.group(1)}{base}/{rel}{anchor}{m.group(3)}"
    return ATTR.sub(fix, LINK.sub(fix, text))


def build(out, gitlab=False):
    """Writes every page into out. Returns [(page name, file name)]."""
    branch = os.environ.get("BRANCH", "main")
    if gitlab:
        project = os.environ.get("GITLAB_PROJECT_URL", "https://gitlab.cielocloud.dev/lab/redstonepanel").rstrip("/")
        raw, blob, rename = f"{project}/-/raw/{branch}", f"{project}/-/blob/{branch}", GITLAB_NAMES
    else:
        repo = os.environ.get("GITHUB_REPOSITORY", "qrussell/redstonepanel")
        raw = f"https://raw.githubusercontent.com/{repo}/{branch}"
        blob, rename = f"https://github.com/{repo}/blob/{branch}", {}
    found = pages()
    written = []
    for name, src in found.items():
        with open(src, encoding="utf-8") as f:
            text = rewrite(f.read(), os.path.dirname(src), found, raw, blob, rename, gitlab)
        rel = os.path.relpath(src, ROOT).replace(os.sep, "/")
        if name not in SPECIAL:
            text = text.rstrip("\n") + f"\n\n---\n*Generated from [`{rel}`]({blob}/{rel}); edit it there.*\n"
        target = rename.get(name, name)
        with open(os.path.join(out, target + ".md"), "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        written.append((target, rel))
        print(f"{target}.md  <-  {rel}")
    return written


def main(argv):
    args = [a for a in argv[1:] if a != "--gitlab"]
    if len(args) != 1 or not os.path.isdir(args[0]):
        sys.exit(__doc__.split("\n\n")[1])
    build(args[0], gitlab="--gitlab" in argv)


if __name__ == "__main__":
    main(sys.argv)
