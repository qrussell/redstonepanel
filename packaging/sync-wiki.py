#!/usr/bin/env python3
"""Copies the wiki pages (docs/wiki/*.md) and the user manual (docs/guide/README.md, published as
User-Manual) into a checkout of the project's GitHub wiki.

    python3 packaging/sync-wiki.py WIKI_DIR

Pages are written in the repository so they read correctly there too, so links are rewritten
for the wiki on the way:

- `Installation.md#x` (another wiki page)   -> `Installation#x`
- `../guide/images/a.png`, `images/a.png`  -> the image on raw.githubusercontent.com
- `../../README.md#x`, `../design/x.md`    -> the file on github.com

GITHUB_REPOSITORY (owner/name) and BRANCH pick where those links point; they default to
qrussell/redstonepanel and main. .github/workflows/wiki.yml runs this on every change to main.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WIKI_SRC = os.path.join(ROOT, "docs", "wiki")
MANUAL = os.path.join(ROOT, "docs", "guide", "README.md")
MANUAL_PAGE = "User-Manual"

LINK = re.compile(r"(\]\()([^)\s]+)(\))")


def pages():
    """{page name: source file}: every docs/wiki/*.md, plus the manual."""
    found = {f[:-3]: os.path.join(WIKI_SRC, f) for f in sorted(os.listdir(WIKI_SRC)) if f.endswith(".md")}
    found[MANUAL_PAGE] = MANUAL
    return found


def rewrite(text, src_dir, names, raw, blob):
    def fix(m):
        target = m.group(2)
        if re.match(r"^[a-z]+:|^#", target):          # absolute URL, mailto, or an anchor
            return m.group(0)
        path, _, anchor = target.partition("#")
        anchor = "#" + anchor if anchor else ""
        if path.endswith(".md") and "/" not in path and path[:-3] in names:
            return f"{m.group(1)}{path[:-3]}{anchor}{m.group(3)}"
        full = os.path.normpath(os.path.join(src_dir, path))
        rel = os.path.relpath(full, ROOT).replace(os.sep, "/")
        if rel.startswith(".."):
            return m.group(0)
        if os.path.normpath(full) == os.path.normpath(MANUAL):
            return f"{m.group(1)}{MANUAL_PAGE}{anchor}{m.group(3)}"
        if os.path.normpath(os.path.dirname(full)) == os.path.normpath(WIKI_SRC) and rel.endswith(".md"):
            return f"{m.group(1)}{os.path.basename(rel)[:-3]}{anchor}{m.group(3)}"
        base = raw if re.search(r"\.(png|jpe?g|gif|svg|webp)$", rel, re.I) else blob
        return f"{m.group(1)}{base}/{rel}{anchor}{m.group(3)}"
    return LINK.sub(fix, text)


def main(argv):
    if len(argv) != 2 or not os.path.isdir(argv[1]):
        sys.exit(__doc__.split("\n\n")[1])
    out = argv[1]
    repo = os.environ.get("GITHUB_REPOSITORY", "qrussell/redstonepanel")
    branch = os.environ.get("BRANCH", "main")
    raw = f"https://raw.githubusercontent.com/{repo}/{branch}"
    blob = f"https://github.com/{repo}/blob/{branch}"
    found = pages()
    for name, src in found.items():
        with open(src, encoding="utf-8") as f:
            text = rewrite(f.read(), os.path.dirname(src), found, raw, blob)
        if name not in ("_Sidebar", "_Footer"):
            text = text.rstrip("\n") + (f"\n\n---\n*Generated from [`{os.path.relpath(src, ROOT).replace(os.sep, '/')}`]"
                                        f"({blob}/{os.path.relpath(src, ROOT).replace(os.sep, '/')}); edit it there.*\n")
        with open(os.path.join(out, name + ".md"), "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        print(f"{name}.md  <-  {os.path.relpath(src, ROOT)}")


if __name__ == "__main__":
    main(sys.argv)
