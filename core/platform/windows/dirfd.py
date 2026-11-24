"""`os` for the file manager on Windows: the dir_fd calls Windows doesn't have.

On Linux the file manager never opens a path as a string. It opens the server's folder, then
each part of the path relative to the folder before it, refusing anything that is a link
(`os.open(name, O_NOFOLLOW, dir_fd=...)`), so a link planted in a server's folder by a plugin
can't lead the panel (which runs as the system) outside it. Python on Windows has none of those
calls. This module gives the same calls the same meaning:

  * A folder "descriptor" is a real handle to the folder, opened without following a link and
    held so the folder can't be renamed, deleted or replaced by a junction while it is in use.
    Windows also refuses to rename any folder above one that is held open. So once a folder is
    open, the path to it can't be swapped for something else.
  * Symbolic links, junctions and every other reparse point are refused or reported as links,
    never followed. A file is checked again after it is opened: it must still be the file that
    was looked at, and not a link.
  * Names Windows treats specially are refused: CON, NUL, COM1 and friends, names with a
    colon (`name:stream` is an alternate data stream of `name`), and names ending in a dot or a
    space (Windows drops them, so `x.` would be `x`).

Everything else (`read`, `write`, `path`, ...) is Python's own `os`.
"""
import ctypes
import errno
import itertools
import os
import shutil
import stat
import threading
from ctypes import wintypes

from core.platform.windows import _win

FILE_ATTRIBUTE_DIRECTORY, FILE_ATTRIBUTE_REPARSE_POINT = 0x10, 0x400
RESERVED = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {f"COM{i}" for i in "123456789¹²³"} \
    | {f"LPT{i}" for i in "123456789¹²³"}
BAD_CHARS = set('<>:"|?*/\\') | {chr(i) for i in range(32)}

_dirs = {}                              # pretend descriptor -> (path, handle)
_numbers = itertools.count(1 << 26)     # far above any real descriptor
_lock = threading.Lock()


def name_problem(name):
    """Why Windows can't hold a file with this name, or None."""
    if not name or name in (".", ".."):
        return "that isn't a file name"
    if any(c in BAD_CHARS for c in name):
        return 'Windows file names can\'t contain < > : " / \\ | ? * or control characters'
    if name[-1] in ". ":
        return "Windows file names can't end with a dot or a space"
    if name.split(".")[0].strip().upper() in RESERVED:
        return f"{name.split('.')[0]} is a device name on Windows, not a file"
    return None


def _check(name):
    why = name_problem(name)
    if why:
        raise OSError(errno.EINVAL, f"'{name}': {why}")
    return name


def is_link(st):
    """True for anything that leads somewhere else: a symbolic link, a junction, a mount point."""
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)


def _as_link(st):
    """st with its type changed to "link" when it is any kind of reparse point, so callers that
    ask S_ISLNK / S_ISDIR / S_ISREG see a junction as a link, not as a folder."""
    if is_link(st) and not stat.S_ISLNK(st.st_mode):
        return os.stat_result((stat.S_IFLNK | 0o777, st.st_ino, st.st_dev, st.st_nlink, 0, 0, st.st_size,
                               st.st_atime, st.st_mtime, st.st_ctime))
    return st


class BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
    _fields_ = [("dwFileAttributes", wintypes.DWORD), ("ftCreationTime", wintypes.FILETIME),
                ("ftLastAccessTime", wintypes.FILETIME), ("ftLastWriteTime", wintypes.FILETIME),
                ("dwVolumeSerialNumber", wintypes.DWORD), ("nFileSizeHigh", wintypes.DWORD),
                ("nFileSizeLow", wintypes.DWORD), ("nNumberOfLinks", wintypes.DWORD),
                ("nFileIndexHigh", wintypes.DWORD), ("nFileIndexLow", wintypes.DWORD)]


def _hold(path):
    """A handle that pins the folder at path: opened without following a link, shared for reading
    and writing but not for deleting or renaming. Raises OSError with ENOENT, ELOOP or ENOTDIR."""
    k = _win.dll("kernel32")
    k.CreateFileW.restype = wintypes.HANDLE
    k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                              wintypes.DWORD, wintypes.HANDLE]
    k.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    # read attributes + list; share read|write; open existing; backup semantics (folders) + don't follow
    h = k.CreateFileW(path, 0x80 | 0x1, 0x1 | 0x2, None, 3, 0x02000000 | 0x00200000, None)
    if not h or h == wintypes.HANDLE(-1).value:
        code = ctypes.get_last_error()
        if code in (2, 3):
            raise OSError(errno.ENOENT, "No such file or folder", path)
        if code == 5:
            raise OSError(errno.EACCES, "Access is denied", path)
        raise OSError(errno.EIO, ctypes.FormatError(code).strip(), path)
    info = BY_HANDLE_FILE_INFORMATION()
    if not k.GetFileInformationByHandle(h, ctypes.byref(info)):
        k.CloseHandle(h)
        raise OSError(errno.EIO, "Can't read the folder's attributes", path)
    if info.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT:
        k.CloseHandle(h)
        raise OSError(errno.ELOOP, "It is a link (symbolic link or junction)", path)
    if not info.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY:
        k.CloseHandle(h)
        raise OSError(errno.ENOTDIR, "Not a folder", path)
    return h


def _force(func, path, exc):
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


class _Os:
    """Python's `os`, with the dir_fd calls added. One instance: module()."""
    O_NOFOLLOW = 0
    O_NONBLOCK = 0
    O_DIRECTORY = 1 << 30               # not a Windows flag: marks "open this as a folder"
    supports_dir_fd = True

    def __getattr__(self, name):
        return getattr(os, name)

    # -- folders -------------------------------------------------------------
    def _path(self, name, dir_fd):
        if dir_fd is None:
            return name
        with _lock:
            entry = _dirs.get(dir_fd)
        if entry is None:
            raise OSError(errno.EBADF, "Not an open folder")
        return os.path.join(entry[0], _check(name))

    def open(self, path, flags, mode=0o777, *, dir_fd=None):
        full = self._path(path, dir_fd)
        if flags & self.O_DIRECTORY:
            handle = _hold(full)
            with _lock:
                fd = next(_numbers)
                _dirs[fd] = (full, handle)
            return fd
        flags &= ~self.O_DIRECTORY
        try:
            before = os.lstat(full)
        except FileNotFoundError:
            before = None
        if before is not None:
            if is_link(before):
                raise OSError(errno.ELOOP, "It is a link (symbolic link or junction)", full)
            if stat.S_ISDIR(before.st_mode):
                raise OSError(errno.EISDIR, "It is a folder", full)
        fd = os.open(full, flags | os.O_BINARY | os.O_NOINHERIT, mode)
        try:
            # Still the file that was looked at, and still not a link
            now, opened = os.lstat(full), os.fstat(fd)
            if is_link(now) or (now.st_ino, now.st_dev) != (opened.st_ino, opened.st_dev):
                raise OSError(errno.ELOOP, "It changed into a link while it was being opened", full)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def close(self, fd):
        with _lock:
            entry = _dirs.pop(fd, None)
        if entry is None:
            return os.close(fd)
        _win.dll("kernel32").CloseHandle(wintypes.HANDLE(entry[1]))

    def fstat(self, fd):
        with _lock:
            entry = _dirs.get(fd)
        return os.fstat(fd) if entry is None else os.stat(entry[0])

    def stat(self, path, *, dir_fd=None, follow_symlinks=True):
        full = self._path(path, dir_fd)
        if follow_symlinks:
            return os.stat(full)
        return _as_link(os.lstat(full))

    def lstat(self, path, *, dir_fd=None):
        return self.stat(path, dir_fd=dir_fd, follow_symlinks=False)

    def scandir(self, where="."):
        with _lock:
            entry = _dirs.get(where) if isinstance(where, int) else None
        return _Scan(os.scandir(entry[0] if entry else where))

    def listdir(self, where="."):
        with _lock:
            entry = _dirs.get(where) if isinstance(where, int) else None
        return os.listdir(entry[0] if entry else where)

    def fwalk(self, top=".", topdown=True, onerror=None, *, follow_symlinks=False, dir_fd=None):
        """(folder, folder names, file names, folder descriptor) for a folder and everything under
        it, never entering a link. Folders are yielded top first."""
        with _lock:
            entry = _dirs.get(dir_fd)
        base = entry[0] if entry else top
        if entry and top not in (".", ""):
            base = os.path.join(base, top)
        stack = [(base, top)]
        while stack:
            full, shown = stack.pop()
            try:
                fd = self.open(full, os.O_RDONLY | self.O_DIRECTORY)
            except OSError as e:
                if onerror:
                    onerror(e)
                continue
            try:
                dirs, files = [], []
                with self.scandir(fd) as it:
                    for e in it:
                        st = e.stat(follow_symlinks=False)
                        (dirs if stat.S_ISDIR(st.st_mode) else files).append(e.name)
                yield shown, dirs, files, fd
                for d in reversed(dirs):
                    stack.append((os.path.join(full, d), os.path.join(shown, d)))
            finally:
                self.close(fd)

    # -- names in a folder ---------------------------------------------------
    def readlink(self, path, *, dir_fd=None):
        return os.readlink(self._path(path, dir_fd))

    def mkdir(self, path, mode=0o777, *, dir_fd=None):
        return os.mkdir(self._path(path, dir_fd))

    def rmdir(self, path, *, dir_fd=None):
        return os.rmdir(self._path(path, dir_fd))

    def unlink(self, path, *, dir_fd=None):
        full = self._path(path, dir_fd)
        st = os.lstat(full)
        if stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode):
            if is_link(st):
                return os.rmdir(full)           # a junction: remove the link, not what it points at
            raise IsADirectoryError(errno.EISDIR, "It is a folder", full)
        try:
            return os.unlink(full)
        except PermissionError:
            os.chmod(full, stat.S_IWRITE)       # read-only files can't be deleted on Windows
            return os.unlink(full)

    remove = unlink

    def rename(self, src, dst, *, src_dir_fd=None, dst_dir_fd=None):
        """Moves src over dst, replacing a file that is there, as rename does on Linux."""
        return os.replace(self._path(src, src_dir_fd), self._path(dst, dst_dir_fd))

    replace = rename

    def utime(self, path, times=None, *, dir_fd=None, follow_symlinks=True):
        return os.utime(self._path(path, dir_fd), times)

    def rmtree_at(self, name, dir_fd):
        """Deletes the folder `name` in an open folder and everything in it, never through a link."""
        full = self._path(name, dir_fd)
        if is_link(os.lstat(full)):
            raise OSError(errno.ELOOP, "It is a link", full)
        shutil.rmtree(full, onerror=_force)

    # -- owners: files in a server's folder inherit the folder's permissions on Windows ------
    def chown(self, path, uid, gid, *, dir_fd=None, follow_symlinks=True):
        return None

    def fchown(self, fd, uid, gid):
        return None

    def fchmod(self, fd, mode):
        return None


class _Scan:
    """os.scandir() whose entries report junctions as links."""

    def __init__(self, it):
        self._it = it

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._it.close()

    def close(self):
        self._it.close()

    def __iter__(self):
        return (_Entry(e) for e in self._it)


class _Entry:
    def __init__(self, entry):
        self._e = entry
        self.name, self.path = entry.name, entry.path

    def stat(self, *, follow_symlinks=True):
        return self._e.stat() if follow_symlinks else _as_link(self._e.stat(follow_symlinks=False))

    def is_symlink(self):
        return is_link(self._e.stat(follow_symlinks=False))

    def is_dir(self, *, follow_symlinks=True):
        if not follow_symlinks and self.is_symlink():
            return False
        return self._e.is_dir(follow_symlinks=follow_symlinks)

    def is_file(self, *, follow_symlinks=True):
        if not follow_symlinks and self.is_symlink():
            return False
        return self._e.is_file(follow_symlinks=follow_symlinks)

    def inode(self):
        return self._e.inode()


_module = _Os()


def module():
    return _module
