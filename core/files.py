"""File manager: browse, edit, upload, download, rename and delete files in a server's folder.

The panel runs as root and the game server (and any plugin or mod in it) runs as
`minecraft`, which owns the folder. So a path from the browser is never handed to open()
as a string: it is opened one component at a time from the server folder with
O_NOFOLLOW (openat), and anything that is a symlink is refused. A symlink planted in the
folder, or swapped in while a request runs, can't lead outside it. Files the panel creates
are given to the folder's owner.

Paths are relative to the server folder, '/'-separated, without '.', '..' or empty parts.
Text editing is for UTF-8 files up to MAX_TEXT bytes; everything else is download/upload.
"""
import errno
import os
import secrets
import shutil
import stat

from core import manager
from core.platform import system

os = system.fs()          # Python's os; on Windows, the same calls made safe without dir_fd (core/platform)

MAX_TEXT = 1024 * 1024               # editable in the browser
MAX_UPLOAD = 512 * 1024 * 1024       # plugin and mod jars, worlds in pieces
MAX_NAME = 255
MAX_ENTRIES = 5000                   # listed per folder

TEXT_EXTENSIONS = {".txt", ".properties", ".json", ".json5", ".yml", ".yaml", ".toml", ".cfg", ".conf",
                   ".ini", ".log", ".md", ".csv", ".mcfunction", ".mcmeta", ".js", ".sh", ".xml", ".lang",
                   ".secret", ".env", ".snbt"}


def rmtree_at(name, dfd):
    """Deletes folder `name` in the open folder dfd and everything in it, never through a link."""
    if hasattr(os, "rmtree_at"):
        return os.rmtree_at(name, dfd)
    return shutil.rmtree(name, dir_fd=dfd)


class FileError(ValueError):
    """A request that can't be done (message is safe to show). status: HTTP status to use."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _parts(path):
    """'plugins/x.yml' -> ['plugins', 'x.yml']; '' or '/' -> []."""
    path = (path or "").replace("\\", "/").strip("/")
    if not path:
        return []
    parts = path.split("/")
    for p in parts:
        if p in ("", ".", "..") or "\x00" in p or len(p.encode()) > MAX_NAME:
            raise FileError("That isn't a valid path inside the server folder.")
    return parts


def _check_name(name):
    if not name or name in (".", "..") or "/" in name or "\\" in name or "\x00" in name \
            or len(name.encode()) > MAX_NAME:
        raise FileError("Choose a name without slashes.")
    return name


def server_root(server):
    manager._check_server(server)            # raises manager.ServerError
    d = manager.get_server_dir(server)
    if not os.path.isdir(d):
        raise FileError(f"{server}'s folder {d} doesn't exist.", 404)
    return d


def _open_root(server):
    try:
        return os.open(server_root(server), os.O_RDONLY | os.O_DIRECTORY)
    except OSError as e:
        raise FileError(f"Can't open the server folder: {e.strerror}", 500)


def _open_dir(server, parts):
    """An fd for the folder at parts, walked without following symlinks. Caller closes it."""
    fd = _open_root(server)
    try:
        for p in parts:
            try:
                nfd = os.open(p, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except OSError as e:
                raise _walk_error(e, p)
            os.close(fd)
            fd = nfd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _walk_error(e, name):
    if e.errno == errno.ENOENT:
        return FileError(f"'{name}' doesn't exist.", 404)
    if e.errno in (errno.ELOOP, errno.ENOTDIR):
        return FileError(f"'{name}' isn't a folder (symlinks aren't followed).")
    return FileError(f"Can't open '{name}': {e.strerror}")


def _parent(server, path):
    """(fd of the parent folder, last name). Caller closes the fd."""
    parts = _parts(path)
    if not parts:
        raise FileError("Choose a file or folder, not the server folder itself.")
    return _open_dir(server, parts[:-1]), parts[-1]


def _lstat(dfd, name):
    try:
        return os.stat(name, dir_fd=dfd, follow_symlinks=False)
    except FileNotFoundError:
        raise FileError(f"'{name}' doesn't exist.", 404)


def _owner(dfd):
    st = os.fstat(dfd)
    return st.st_uid, st.st_gid


def _open_file(server, path, flags=os.O_RDONLY):
    """(fd, stat) of a regular file, refusing symlinks and hard links."""
    dfd, name = _parent(server, path)
    try:
        try:
            fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
        except OSError as e:
            if e.errno == errno.ENOENT:
                raise FileError(f"'{name}' doesn't exist.", 404)
            if e.errno == errno.ELOOP:
                raise FileError(f"'{name}' is a symlink; the file manager doesn't follow symlinks.")
            raise FileError(f"Can't open '{name}': {e.strerror}")
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            os.close(fd)
            raise FileError(f"'{name}' isn't a regular file.")
        if st.st_nlink > 1:
            os.close(fd)
            raise FileError(f"'{name}' has other hard links; the file manager won't open it.")
        return fd, st
    finally:
        os.close(dfd)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
def _kind(st):
    if stat.S_ISDIR(st.st_mode):
        return "dir"
    if stat.S_ISREG(st.st_mode):
        return "file"
    if stat.S_ISLNK(st.st_mode):
        return "link"
    return "other"


def is_text_name(name):
    lower = name.lower()
    return os.path.splitext(lower)[1] in TEXT_EXTENSIONS or lower in ("eula.txt", "screenrc", ".gitignore")


def list_dir(server, path=""):
    parts = _parts(path)
    fd = _open_dir(server, parts)
    try:
        entries = []
        with os.scandir(fd) as it:
            for e in it:
                if len(entries) >= MAX_ENTRIES:
                    break
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                kind = _kind(st)
                entry = {"name": e.name, "type": kind, "size": st.st_size if kind == "file" else None,
                         "modified": int(st.st_mtime)}
                if kind == "file":
                    entry["editable"] = st.st_size <= MAX_TEXT and is_text_name(e.name)
                if kind == "link":
                    try:
                        entry["target"] = os.readlink(e.name, dir_fd=fd)
                    except OSError:
                        pass
                entries.append(entry)
    finally:
        os.close(fd)
    entries.sort(key=lambda x: (x["type"] != "dir", x["name"].lower()))
    return {"path": "/".join(parts), "entries": entries, "truncated": len(entries) >= MAX_ENTRIES,
            "max_text": MAX_TEXT, "max_upload": MAX_UPLOAD}


def read_text(server, path):
    fd, st = _open_file(server, path)
    try:
        if st.st_size > MAX_TEXT:
            raise FileError(f"The file is {st.st_size // 1024} KB; files over {MAX_TEXT // 1024} KB can only be "
                            f"downloaded.")
        data = b""
        while len(data) <= MAX_TEXT:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            data += chunk
    finally:
        os.close(fd)
    if len(data) > MAX_TEXT:
        raise FileError(f"Files over {MAX_TEXT // 1024} KB can only be downloaded.")
    if b"\x00" in data:
        raise FileError("This looks like a binary file; download it instead.")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise FileError("This file isn't UTF-8 text; download it instead.")
    return {"path": "/".join(_parts(path)), "content": text, "modified": int(st.st_mtime), "size": st.st_size}


def open_for_download(server, path):
    """(fd, size, file name) for streaming a file. Caller closes the fd."""
    fd, st = _open_file(server, path)
    return fd, st.st_size, _parts(path)[-1]


# ---------------------------------------------------------------------------
# Writing (atomically, as the folder's owner)
# ---------------------------------------------------------------------------
def _write_into(dfd, name, chunks, overwrite, mode=None):
    """Writes chunks to a temp file in dfd, then renames it over `name`. Returns bytes written."""
    try:
        st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
        exists = True
    except FileNotFoundError:
        st, exists = None, False
    if exists:
        if not overwrite:
            raise FileError(f"'{name}' already exists.", 409)
        if not stat.S_ISREG(st.st_mode):
            raise FileError(f"'{name}' isn't a regular file, so it can't be replaced.")
        if st.st_nlink > 1:
            raise FileError(f"'{name}' has other hard links; the file manager won't replace it.")
    uid, gid = _owner(dfd)
    tmp = f".redstonepanel-{secrets.token_hex(6)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=dfd)
    written = 0
    try:
        try:
            for chunk in chunks:
                written += len(chunk)
                if written > MAX_UPLOAD:
                    raise FileError(f"Files over {MAX_UPLOAD // (1024 * 1024)} MB can't be uploaded.", 413)
                os.write(fd, chunk)
            os.fchown(fd, uid, gid)
            os.fchmod(fd, stat.S_IMODE(st.st_mode) if exists else (mode or 0o644))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=dfd)
        except OSError:
            pass
        raise
    return written


def write_text(server, path, content, expected_modified=None, actor="?"):
    """Saves a text file (new or existing). expected_modified: the mtime the editor loaded, so a
    file changed meanwhile (by the game server, say) isn't silently overwritten."""
    data = (content or "").encode("utf-8")
    if len(data) > MAX_TEXT:
        raise FileError(f"Text files over {MAX_TEXT // 1024} KB can't be saved from the browser.", 413)
    dfd, name = _parent(server, path)
    try:
        if expected_modified is not None:
            try:
                st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
                if int(st.st_mtime) != int(expected_modified):
                    raise FileError(f"'{name}' changed since you opened it. Reload it, then make your changes "
                                    f"again.", 409)
            except FileNotFoundError:
                pass
        _write_into(dfd, name, [data], overwrite=True)
        st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
    finally:
        os.close(dfd)
    manager.write_log(f"[FILES] {actor} saved {server}/{'/'.join(_parts(path))} ({len(data)} bytes).")
    return {"modified": int(st.st_mtime), "size": st.st_size}


def upload(server, folder, name, chunks, overwrite=False, actor="?"):
    """Saves an uploaded file into folder. chunks: an iterable of bytes."""
    _check_name(name)
    dfd = _open_dir(server, _parts(folder))
    try:
        size = _write_into(dfd, name, chunks, overwrite)
    finally:
        os.close(dfd)
    rel = "/".join(_parts(folder) + [name])
    manager.write_log(f"[FILES] {actor} uploaded {server}/{rel} ({size} bytes).")
    return {"path": rel, "size": size}


def make_dir(server, path, actor="?", via="FILES"):
    dfd, name = _parent(server, path)
    try:
        _check_name(name)
        try:
            os.mkdir(name, 0o755, dir_fd=dfd)
        except FileExistsError:
            raise FileError(f"'{name}' already exists.", 409)
        uid, gid = _owner(dfd)
        os.chown(name, uid, gid, dir_fd=dfd, follow_symlinks=False)
    finally:
        os.close(dfd)
    manager.write_log(f"[{via}] {actor} created folder {server}/{'/'.join(_parts(path))}.")


def rename(server, path, new_name, actor="?"):
    """Renames within the same folder (moving between folders isn't offered)."""
    _check_name(new_name)
    dfd, name = _parent(server, path)
    try:
        _lstat(dfd, name)
        try:
            os.stat(new_name, dir_fd=dfd, follow_symlinks=False)
            raise FileError(f"'{new_name}' already exists.", 409)
        except FileNotFoundError:
            pass
        os.rename(name, new_name, src_dir_fd=dfd, dst_dir_fd=dfd)
    finally:
        os.close(dfd)
    manager.write_log(f"[FILES] {actor} renamed {server}/{'/'.join(_parts(path))} to {new_name}.")


def delete(server, path, actor="?"):
    """Deletes a file, a symlink (not what it points to) or a folder with everything in it."""
    dfd, name = _parent(server, path)
    try:
        st = _lstat(dfd, name)
        if stat.S_ISDIR(st.st_mode):
            # rmtree with dir_fd works on file descriptors and doesn't follow symlinks
            rmtree_at(name, dfd)
        else:
            os.unlink(name, dir_fd=dfd)
    finally:
        os.close(dfd)
    manager.write_log(f"[FILES] {actor} deleted {server}/{'/'.join(_parts(path))}.")


# ---------------------------------------------------------------------------
# For SFTP (core/sftp.py, 1.18): the same confined walk, as file descriptors. An SFTP program
# reads and writes a file in pieces at any offset, so there's no temp file and rename here;
# everything else holds: one component at a time with O_NOFOLLOW, regular files only, no
# hard-linked files, new files and folders given to the folder's owner.
# ---------------------------------------------------------------------------
def rel(path):
    """'a/b' with the same checks as every other path here ('' = the server folder)."""
    return "/".join(_parts(path))


def stat_path(server, path):
    """lstat of a path inside the server folder (the folder itself for '')."""
    parts = _parts(path)
    if not parts:
        fd = _open_root(server)
        try:
            return os.fstat(fd)
        finally:
            os.close(fd)
    dfd = _open_dir(server, parts[:-1])
    try:
        return _lstat(dfd, parts[-1])
    finally:
        os.close(dfd)


def scan(server, path=""):
    """[(name, lstat)] of a folder, nothing followed."""
    fd = _open_dir(server, _parts(path))
    try:
        out = []
        with os.scandir(fd) as it:
            for e in it:
                try:
                    out.append((e.name, e.stat(follow_symlinks=False)))
                except OSError:
                    continue
        return out
    finally:
        os.close(fd)


def open_fd(server, path, flags, mode=0o644):
    """Opens a regular file for reading and/or writing at any offset; (fd, created). flags: the
    os.O_* access mode plus O_CREAT, O_EXCL, O_TRUNC, O_APPEND as asked. Caller closes the fd."""
    dfd, name = _parent(server, path)
    try:
        _check_name(name)
        access = flags & (os.O_RDONLY | os.O_WRONLY | os.O_RDWR | os.O_APPEND)
        created = False
        fd = None
        if flags & os.O_CREAT:
            try:
                fd = os.open(name, access | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode & 0o666 or 0o644, dir_fd=dfd)
                created = True
            except FileExistsError:
                if flags & os.O_EXCL:
                    raise FileError(f"'{name}' already exists.", 409)
        if fd is None:
            try:
                fd = os.open(name, access | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
            except OSError as e:
                if e.errno == errno.ENOENT:
                    raise FileError(f"'{name}' doesn't exist.", 404)
                if e.errno == errno.ELOOP:
                    raise FileError(f"'{name}' is a symlink; symlinks aren't followed.")
                if e.errno == errno.EISDIR:
                    raise FileError(f"'{name}' is a folder.")
                raise FileError(f"Can't open '{name}': {e.strerror}")
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise FileError(f"'{name}' isn't a regular file.")
            if st.st_nlink > 1:
                raise FileError(f"'{name}' has other hard links, so it isn't opened.")
            if created:
                uid, gid = _owner(dfd)
                os.fchown(fd, uid, gid)
            elif flags & os.O_TRUNC and access & (os.O_WRONLY | os.O_RDWR):
                os.ftruncate(fd, 0)          # only after the checks: never through a link
        except BaseException:
            os.close(fd)
            if created:
                try:
                    os.unlink(name, dir_fd=dfd)
                except OSError:
                    pass
            raise
        return fd, created
    finally:
        os.close(dfd)


def set_mtime(server, path, mtime, atime=None):
    dfd, name = _parent(server, path)
    try:
        _lstat(dfd, name)
        os.utime(name, (atime if atime is not None else mtime, mtime), dir_fd=dfd, follow_symlinks=False)
    finally:
        os.close(dfd)


def remove_file(server, path, actor="?", via="FILES"):
    """Deletes one file or symlink (not what it points to); never a folder."""
    dfd, name = _parent(server, path)
    try:
        st = _lstat(dfd, name)
        if stat.S_ISDIR(st.st_mode):
            raise FileError(f"'{name}' is a folder.")
        os.unlink(name, dir_fd=dfd)
    finally:
        os.close(dfd)
    manager.write_log(f"[{via}] {actor} deleted {server}/{rel(path)}.")


def remove_empty_dir(server, path, actor="?", via="FILES"):
    dfd, name = _parent(server, path)
    try:
        st = _lstat(dfd, name)
        if not stat.S_ISDIR(st.st_mode):
            raise FileError(f"'{name}' isn't a folder.")
        try:
            os.rmdir(name, dir_fd=dfd)
        except OSError as e:
            if e.errno in (errno.ENOTEMPTY, errno.EEXIST):
                raise FileError(f"'{name}' isn't empty.")
            raise
    finally:
        os.close(dfd)
    manager.write_log(f"[{via}] {actor} deleted folder {server}/{rel(path)}.")


def move(server, src, dst, actor="?", via="FILES", replace=False):
    """Renames or moves within the server folder. Nothing is replaced unless replace is set, and
    then only a regular file by a regular file."""
    sfd, sname = _parent(server, src)
    try:
        dfd, dname = _parent(server, dst)
        try:
            _check_name(dname)
            sst = _lstat(sfd, sname)
            try:
                dst_st = os.stat(dname, dir_fd=dfd, follow_symlinks=False)
            except FileNotFoundError:
                dst_st = None
            if dst_st is not None:
                if not (replace and stat.S_ISREG(dst_st.st_mode) and stat.S_ISREG(sst.st_mode)):
                    raise FileError(f"'{dname}' already exists.", 409)
            os.rename(sname, dname, src_dir_fd=sfd, dst_dir_fd=dfd)
        finally:
            os.close(dfd)
    finally:
        os.close(sfd)
    manager.write_log(f"[{via}] {actor} renamed {server}/{rel(src)} to {rel(dst)}.")

