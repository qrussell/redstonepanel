"""File names Windows can't hold (1.24), for moves, copies and restores onto a Windows member.

A Linux server folder can hold names that Windows refuses or reads as something else: device
names (CON, NUL, COM1, also as con.txt), a colon (name:stream is an alternate data stream),
other characters Windows forbids, a name ending in a dot or a space (Windows drops them), and
two names that differ only in upper and lower case (Windows names aren't case-sensitive, so the
second would overwrite the first). Moving such a folder to Windows would lose files or fail
half-way, so the master checks first and refuses with the list (core/moves.py), and a Windows
panel checks every archive before it unpacks one (core/archive.py, core/backups.py,
core/transfer.py).

Plain logic only: it runs on both systems, on the source as well as the target.
"""
import os

RESERVED = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {f"COM{i}" for i in "123456789¹²³"} \
    | {f"LPT{i}" for i in "123456789¹²³"}
BAD_CHARS = set('<>:"|?*/\\') | {chr(i) for i in range(32)}
SHOWN = 20                      # problems listed in a message; the rest are counted


def name_problem(name):
    """Why Windows can't hold a file or folder with this name, or None."""
    if not name or name in (".", ".."):
        return "that isn't a file name"
    if any(c in BAD_CHARS for c in name):
        return 'Windows file names can\'t contain < > : " / \\ | ? * or control characters'
    if name[-1] in ". ":
        return "Windows file names can't end with a dot or a space"
    if name.split(".")[0].strip().upper() in RESERVED:
        return f"{name.split('.')[0]} is a device name on Windows, not a file"
    return None


def problems(paths):
    """[(path, why)] for '/'-separated relative paths (files and folders, in any order) that a
    Windows folder can't hold. Each bad name is reported once, at its shortest path."""
    out, seen, folded = [], set(), {}
    for path in paths:
        parts = [p for p in str(path).replace("\\", "/").split("/") if p not in ("", ".")]
        for i in range(len(parts)):
            rel = "/".join(parts[:i + 1])
            if rel in seen:
                continue
            seen.add(rel)
            why = name_problem(parts[i])
            if why:
                out.append((rel, why))
                break                       # nothing below a bad name needs a second line
            key = rel.casefold()
            other = folded.setdefault(key, rel)
            if other != rel:
                out.append((rel, f"Windows sees it as the same name as {other} (names there aren't "
                                 f"case-sensitive)"))
                break
    return out


def folder_problems(root):
    """problems() for everything in a folder on this machine, never following links."""
    def walk():
        for here, dirs, files in os.walk(root):
            rel_here = os.path.relpath(here, root).replace(os.sep, "/")
            rel_here = "" if rel_here == "." else rel_here + "/"
            dirs.sort()
            for name in dirs + sorted(files):
                yield rel_here + name
    return problems(walk())


def describe(found, where="Windows"):
    """One message listing what was found (the first SHOWN of them)."""
    lines = [f"{p} ({why})" for p, why in found[:SHOWN]]
    more = len(found) - SHOWN
    return (f"{len(found)} file name{'s' if len(found) != 1 else ''} in it can't be kept on {where}: "
            + "; ".join(lines) + (f"; and {more} more" if more > 0 else "")
            + ". Rename or remove them first, then try again.")
