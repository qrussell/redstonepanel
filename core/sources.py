"""HTTP for the mod and plugin sources (Modrinth, Hangar): JSON with a short cache, and
downloads that only ever come from the sources' own CDNs.

Downloads are refused unless the URL (and every redirect) is HTTPS on an allowed host, are
limited to the upload size limit, and are checked against the hash the API gave before
anything is installed. Nothing here writes into a server folder; the caller hands the
downloaded file to core/mods.py, which checks it like a dropped file.
"""
import hashlib
import json
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from core import files
from core.javaservers import _user_agent

TIMEOUT = 30
CACHE_SECONDS = 600
MAX_DOWNLOAD = files.MAX_UPLOAD

_cache = {}
_cache_lock = threading.Lock()


class SourceError(files.FileError):
    """Couldn't search or download (message is safe to show)."""


def _host(url):
    return urllib.parse.urlsplit(url).hostname or "?"


def get_json(url, body=None, cache=True):
    """GET (or POST a JSON body) and parse the answer. None on 404."""
    key = (url, json.dumps(body, sort_keys=True) if body is not None else None)
    now = time.time()
    if cache:
        with _cache_lock:
            hit = _cache.get(key)
            if hit and now - hit[0] < CACHE_SECONDS:
                return hit[1]
    headers = {"User-Agent": _user_agent(), "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            out = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise SourceError(f"{_host(url)} answered HTTP {e.code}.", 502)
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise SourceError(f"Couldn't reach {_host(url)}: {getattr(e, 'reason', e)}", 502)
    if cache:
        with _cache_lock:
            if len(_cache) > 500:
                _cache.clear()
            _cache[key] = (now, out)
    return out


def clear_cache():
    with _cache_lock:
        _cache.clear()


class _Redirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed):
        self.allowed = allowed

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl, self.allowed)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def check_url(url, allowed):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or (parts.hostname or "").lower() not in allowed:
        raise SourceError(f"Refusing to download from {parts.hostname or url}: only "
                          f"{', '.join(sorted(allowed))} over HTTPS.")


def download(url, allowed, algorithm, expected, size=None):
    """Downloads url into a temporary file (deleted when closed) and checks its hash.
    Returns the open file, positioned at the start."""
    check_url(url, allowed)
    if not expected:
        raise SourceError(f"{_host(url)} gave no checksum for this file, so it wasn't downloaded.")
    if size and size > MAX_DOWNLOAD:
        raise SourceError(f"The file is over {MAX_DOWNLOAD // 1048576} MB.", 413)
    opener = urllib.request.build_opener(_Redirects(allowed))
    req = urllib.request.Request(url, headers={"User-Agent": _user_agent()})
    h = hashlib.new(algorithm)
    out = tempfile.TemporaryFile()
    total = 0
    try:
        with opener.open(req, timeout=TIMEOUT) as r:
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_DOWNLOAD:
                    raise SourceError(f"The file is over {MAX_DOWNLOAD // 1048576} MB.", 413)
                h.update(chunk)
                out.write(chunk)
    except SourceError:
        out.close()
        raise
    except (urllib.error.URLError, OSError) as e:
        out.close()
        raise SourceError(f"Downloading from {_host(url)} failed: {getattr(e, 'reason', e)}", 502)
    if h.hexdigest() != str(expected).lower():
        out.close()
        raise SourceError(f"The file from {_host(url)} didn't match its checksum; nothing was installed.", 502)
    out.seek(0)
    return out
