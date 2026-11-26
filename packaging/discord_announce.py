#!/usr/bin/env python3
"""Post a release announcement to the RedstonePanel Discord (#announcements).

Usage: python3 packaging/discord_announce.py vX.Y.Z

Run by the GitLab `discord-announce` job after `release` creates the GitLab release. The
webhook URL comes from the masked, protected CI variable DISCORD_RELEASE_WEBHOOK (Settings >
CI/CD > Variables); it is never kept in the repository. Without it the job says so and passes.

The message is built from release-notes/vX.Y.Z.md: its first paragraph, then the bold title of
each item under "## New" and "## Fixed", and links to the release and its notes. A failed post
is a warning, never a failed pipeline: the release itself has already happened.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request

REPO = "https://github.com/qrussell/redstonepanel"
COLOR = 0xE0322F  # redstone red
# Discord limits: embed description 4096 characters, title 256.
MAX_DESCRIPTION = 4000
SECTIONS = ("New", "Fixed", "Changed", "Security")


def parse_notes(text):
    """Return (summary, {section: [item titles]}) from a release-notes file."""
    summary_lines = []
    sections = {}
    current = None
    for line in text.splitlines():
        heading = re.match(r"^##\s+(.+?)\s*$", line)
        if heading:
            current = heading.group(1)
            continue
        if current is None:
            if line.strip():
                summary_lines.append(line.strip())
            elif summary_lines:
                current = ""  # the summary is the first paragraph only
            continue
        if current in SECTIONS:
            item = re.match(r"^- \*\*(.+?)\*\*", line)
            if item:
                sections.setdefault(current, []).append(item.group(1).rstrip(".:"))
    return " ".join(summary_lines), sections


def build_message(tag, notes_text):
    version = tag[1:] if tag.startswith("v") else tag
    summary, sections = parse_notes(notes_text or "")
    parts = []
    if summary:
        parts.append(summary)
    for name in SECTIONS:
        if sections.get(name):
            parts.append(f"**{name}**\n" + "\n".join(f"• {t}" for t in sections[name]))
    parts.append(
        f"[Release notes and downloads]({REPO}/releases/tag/{tag}) · "
        f"[What's new]({REPO}/blob/main/docs/wiki/Whats-New.md)\n"
        "Update with `sudo apt update && sudo apt upgrade`, or **Update now** on the dashboard."
    )
    description = "\n\n".join(parts)
    if len(description) > MAX_DESCRIPTION:
        description = description[: MAX_DESCRIPTION - 1].rstrip() + "…"
    return {
        "username": "RedstonePanel",
        "content": f"📦 **RedstonePanel {version}** is out!",
        "embeds": [{
            "title": f"RedstonePanel {version}",
            "url": f"{REPO}/releases/tag/{tag}",
            "description": description,
            "color": COLOR,
        }],
        # Never ping @everyone or roles from text in the release notes.
        "allowed_mentions": {"parse": []},
    }


def main(argv):
    if len(argv) != 2 or not re.match(r"^v\d+\.\d+\.\d+$", argv[1]):
        print("usage: discord_announce.py vX.Y.Z", file=sys.stderr)
        return 2
    tag = argv[1]
    webhook = os.environ.get("DISCORD_RELEASE_WEBHOOK", "").strip()
    if not webhook:
        print("No DISCORD_RELEASE_WEBHOOK variable: not posting to Discord.")
        return 0
    if not webhook.startswith(("https://discord.com/api/webhooks/",
                               "https://discordapp.com/api/webhooks/")):
        print("WARNING - DISCORD_RELEASE_WEBHOOK is not a Discord webhook URL; not posting.")
        return 0
    notes_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                              "release-notes", f"{tag}.md")
    try:
        with open(notes_path, encoding="utf-8") as f:
            notes = f.read()
    except OSError:
        print(f"WARNING - no release-notes/{tag}.md; posting a short announcement.")
        notes = ""
    body = json.dumps(build_message(tag, notes)).encode()
    req = urllib.request.Request(
        webhook + ("&" if "?" in webhook else "?") + "wait=true", data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "User-Agent": "RedstonePanel-release (https://github.com/qrussell/redstonepanel, 1)"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            print(f"Posted the {tag} announcement to Discord (HTTP {resp.status}).")
    except urllib.error.HTTPError as e:
        # Never print the webhook URL; the error body has no secrets.
        print(f"WARNING - Discord refused the announcement: HTTP {e.code} {e.read()[:300]!r}")
    except (urllib.error.URLError, OSError) as e:
        print(f"WARNING - could not reach Discord: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
