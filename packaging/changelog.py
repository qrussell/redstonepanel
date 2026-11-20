#!/usr/bin/env python3
"""Writes CHANGELOG (Markdown) from debian/changelog, which has every version's changes.

    python3 packaging/changelog.py           rewrite CHANGELOG
    python3 packaging/changelog.py --check   exit 1 if CHANGELOG isn't up to date (CI)

Run it after adding a debian/changelog entry, in the same merge request.
"""
import email.utils
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(ROOT, "debian", "changelog")
TARGET = os.path.join(ROOT, "CHANGELOG")
REPO = "https://github.com/qrussell/redstonepanel"

HEADER = f"""# Changelog

Every RedstonePanel release, newest first. This file is generated from `debian/changelog` by
`packaging/changelog.py`; edit that instead. The highlights of each release, with how to install
it, are in `release-notes/` and on the releases page: {REPO}/releases
"""


def entries(text):
    """[(version, date, [change, ...])] in file order (newest first)."""
    out = []
    for block in re.split(r"\n(?=\S)", text.strip()):
        head = re.match(r"^\S+ \(([^)]+)\)", block)
        if not head:
            continue
        sign = re.search(r"^ -- .*?  (.+)$", block, re.M)
        date = ""
        if sign:
            parsed = email.utils.parsedate_to_datetime(sign.group(1).strip())
            date = parsed.strftime("%Y-%m-%d")
        changes, current = [], None
        for line in block.splitlines()[1:]:
            if line.startswith("  * "):
                current = [line[4:].strip()]
                changes.append(current)
            elif line.startswith("    ") and current is not None:
                current.append(line.strip())
            elif line.startswith(" -- "):
                break
        out.append((head.group(1), date, [_join(c) for c in changes]))
    return out


def _join(lines):
    """One change's wrapped lines as one line; "last-" + "seen" stays "last-seen"."""
    text = lines[0]
    for line in lines[1:]:
        text += line if re.search(r"[A-Za-z]-$", text) else " " + line
    return text


def render(text):
    parts = [HEADER]
    for version, date, changes in entries(text):
        notes = f"release-notes/v{version}.md"
        title = f"## {version}" + (f" ({date})" if date else "")
        parts.append("\n" + title + "\n")
        if os.path.exists(os.path.join(ROOT, notes)):
            parts.append(f"\nRelease notes: {notes}\n")
        parts.append("\n" + "\n".join(f"- {c}" for c in changes) + "\n")
    return "".join(parts)


def main(argv):
    with open(SOURCE, encoding="utf-8") as f:
        wanted = render(f.read())
    if "--check" in argv:
        try:
            with open(TARGET, encoding="utf-8") as f:
                have = f.read()
        except FileNotFoundError:
            have = ""
        if have != wanted:
            sys.exit("CHANGELOG is out of date: run python3 packaging/changelog.py and commit it.")
        print("CHANGELOG is up to date.")
        return
    with open(TARGET, "w", encoding="utf-8", newline="\n") as f:
        f.write(wanted)
    print(f"Wrote CHANGELOG ({len(entries(open(SOURCE, encoding='utf-8').read()))} versions).")


if __name__ == "__main__":
    main(sys.argv[1:])
