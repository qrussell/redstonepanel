r"""server.properties parsing that agrees with the game server reading the same file.

Java Edition reads and rewrites server.properties with java.util.Properties, so its values are
escaped: on every start the server saves `rcon.password=a:b!` as `rcon.password=a\:b\!`.
A plain "split at =" reader then sees a different password from the one the server uses, and
also disagrees with Java on trailing spaces and on lines continued with a trailing backslash
(which can silently turn `enable-rcon` off). parse()/format_line() follow Properties.load()
and Properties.store() exactly.

Bedrock Dedicated Server reads the file as plain `key=value` lines, so `java=False` keeps that.
"""
import re

_NEWLINE = re.compile(r"\r\n|\r|\n")      # Java's line terminators (str.splitlines() knows more)
_WS = " \t\f"
_UNESCAPE = {"t": "\t", "n": "\n", "r": "\r", "f": "\f"}
_ESCAPE = {"\\": "\\\\", "\t": "\\t", "\n": "\\n", "\r": "\\r", "\f": "\\f",
           "=": "\\=", ":": "\\:", "#": "\\#", "!": "\\!"}


def _odd_trailing_backslashes(s):
    return (len(s) - len(s.rstrip("\\"))) % 2 == 1


def _logical_lines(lines):
    """Yields (first, last, text): each key/value entry and the natural lines it spans."""
    i, n = 0, len(lines)
    while i < n:
        first = i
        text = lines[i].lstrip(_WS)
        if not text or text[0] in "#!":
            i += 1
            continue
        while _odd_trailing_backslashes(text):
            text = text[:-1]
            if i + 1 >= n:
                break
            i += 1
            text += lines[i].lstrip(_WS)
        yield first, i, text
        i += 1


def _unescape(s):
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c != "\\" or i + 1 >= len(s):
            out.append(c)
            i += 1
            continue
        c = s[i + 1]
        if c == "u" and re.fullmatch(r"[0-9a-fA-F]{4}", s[i + 2:i + 6]):
            out.append(chr(int(s[i + 2:i + 6], 16)))
            i += 6
            continue
        out.append(_UNESCAPE.get(c, c))
        i += 2
    return "".join(out)


def _split_entry(text):
    """'key = value' / 'key:value' / 'key value' -> (key, value), both unescaped."""
    j = 0
    while j < len(text):
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c in "=:" or c in _WS:
            break
        j += 1
    key_end = j
    has_sep = j < len(text) and text[j] in "=:"
    j = min(j + 1, len(text)) if j < len(text) else j
    while j < len(text):
        c = text[j]
        if c not in _WS:
            if not has_sep and c in "=:":
                has_sep = True
            else:
                break
        j += 1
    return _unescape(text[:key_end]), _unescape(text[j:])


def _entries(lines, java):
    """Yields (first, last, key, value) for every entry, in file order."""
    if java:
        for first, last, text in _logical_lines(lines):
            yield (first, last) + _split_entry(text)
        return
    for i, line in enumerate(lines):
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            yield i, i, k.strip(), v.strip()


def split_lines(content):
    lines = _NEWLINE.split(content)
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def parse(content, java=True):
    """server.properties text -> {key: value}. A repeated key keeps its last value, as Java does."""
    return {k: v for _, _, k, v in _entries(split_lines(content), java)}


def _escape(s, is_key):
    out = []
    for i, c in enumerate(s):
        if c == " " and (is_key or i == 0):
            out.append("\\ ")
        else:
            out.append(_ESCAPE.get(c, c))
    return "".join(out)


def format_line(key, value, java=True):
    if not java:
        return f"{key}={value}"
    return f"{_escape(key, True)}={_escape(str(value), False)}"


def update(content, new_props, java=True):
    """Returns content with new_props applied. Entries whose value doesn't change keep their
    original text; a changed key is rewritten in place (all its lines, if it was continued or
    repeated); keys not in the file are appended."""
    lines = split_lines(content)
    new_props = {k: str(v) for k, v in new_props.items()}
    current = parse(content, java)
    replace = {}                       # first line index -> new text, or None to drop the lines
    for first, last, key, _ in _entries(lines, java):
        if key in new_props and new_props[key] != current.get(key):
            for i in range(first, last + 1):
                replace[i] = None
            replace[first] = (key, format_line(key, new_props[key], java))
    out, written = [], set()
    for i, line in enumerate(lines):
        if i not in replace:
            out.append(line)
        elif replace[i] is not None:
            key, text = replace[i]
            if key not in written:     # a repeated key is written once, where it first appeared
                out.append(text)
                written.add(key)
    for key, value in new_props.items():
        if key not in current:
            out.append(format_line(key, value, java))
    return "\n".join(out) + "\n"
