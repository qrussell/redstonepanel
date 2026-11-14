"""Safe unpacking of uploaded server backups: .zip, .tar.gz / .tgz, .tar (also .tar.xz/.bz2).

The panel runs as root, so an archive must never be able to write outside the folder it's
unpacked into: absolute paths, '..' components and links pointing elsewhere are refused
(tar uses Python's 'data' extraction filter; zip entries are checked here; zip symlinks are skipped).

Backups often wrap everything in one folder (e.g. 'bedrock-server_backup_20261110/'); that
folder is stripped so the server files end up at the top of the server directory.
"""
import os
import shutil
import stat
import tarfile
import tempfile
import zipfile

JUNK = {"__MACOSX", ".DS_Store", "Thumbs.db"}   # added by macOS/Windows zip tools


class ArchiveError(ValueError):
    """The upload can't be used (message is safe to show)."""


def _kind(path):
    if zipfile.is_zipfile(path):
        return "zip"
    try:
        if tarfile.is_tarfile(path):
            return "tar"
    except (OSError, tarfile.TarError):
        pass
    raise ArchiveError("Unsupported file: upload a .zip, .tar.gz/.tgz or .tar server backup.")


def _clean_name(name):
    """Normalised relative path, or raise if it tries to leave the target folder."""
    name = name.replace("\\", "/")        # some Windows tools store backslashes
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        raise ArchiveError(f"Archive entry has an absolute path: {name}")
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise ArchiveError(f"Archive entry points outside the archive: {name}")
    return "/".join(parts)


def _uncompressed_size(path, kind):
    if kind == "zip":
        with zipfile.ZipFile(path) as z:
            return sum(i.file_size for i in z.infolist())
    with tarfile.open(path) as t:
        return sum(m.size for m in t.getmembers() if m.isfile())


def _extract_zip(path, dest):
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            rel = _clean_name(info.filename)
            if not rel:
                continue
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                continue                   # never create links from an upload
            target = os.path.join(dest, rel)
            if not os.path.realpath(target).startswith(os.path.realpath(dest) + os.sep):
                raise ArchiveError(f"Archive entry points outside the archive: {info.filename}")
            if info.is_dir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            # keep executables executable (bedrock_server); zip only stores this for Unix-made archives
            os.chmod(target, 0o755 if mode & 0o111 else 0o644)


def _extract_tar(path, dest):
    with tarfile.open(path) as t:
        for m in t.getmembers():
            _clean_name(m.name)            # clear message for the common bad cases
        try:
            # 'data' filter: refuses absolute paths, '..', links leaving dest, devices; drops setuid
            t.extractall(dest, filter="data")
        except tarfile.FilterError as e:
            raise ArchiveError(f"Unsafe archive entry refused: {e}")


def unpack(path, parent):
    """Unpacks into a new temp folder inside `parent` (same filesystem, so it can be renamed into
    place). Returns (staging_dir, root): root is the folder holding the server files, i.e. the
    staging dir itself or its single wrapping folder. Caller removes staging_dir when done."""
    kind = _kind(path)
    needed = _uncompressed_size(path, kind)
    free = shutil.disk_usage(parent).free
    if needed > free * 0.9:
        raise ArchiveError(f"Not enough disk space: the backup unpacks to {needed / 1e9:.1f} GB, "
                           f"{free / 1e9:.1f} GB free.")
    staging = tempfile.mkdtemp(prefix=".redstonepanel-import-", dir=parent)
    try:
        (_extract_zip if kind == "zip" else _extract_tar)(path, staging)
        for junk in JUNK:
            p = os.path.join(staging, junk)
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p)
            elif os.path.lexists(p):
                os.remove(p)
        entries = os.listdir(staging)
        root = staging
        if len(entries) == 1:
            only = os.path.join(staging, entries[0])
            if os.path.isdir(only) and not os.path.islink(only):
                root = only                # one wrapping folder: use its contents
        return staging, root
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
