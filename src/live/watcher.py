"""File-change watcher for `live tail -f`.

Backends, in order of preference:
  1. macOS: `select.kqueue` on each watched fd.
  2. Linux/WSL: `inotify` syscalls via a small ctypes shim.
  3. Fallback: a tight `os.stat()` poll loop (~50 ms).

The API is event-edge-only: `wait()` returns the set of paths that were touched
since the last call (or empty on timeout). Callers re-read the file to decide
what's new.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
import select
import struct
import sys
import time
from pathlib import Path


class FsWatcher:
    """Abstract base. Subclasses implement wait / close / add_path."""

    def add_path(self, path: Path) -> None:
        raise NotImplementedError

    def remove_path(self, path: Path) -> None:
        raise NotImplementedError

    def wait(self, timeout: float) -> set[Path]:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


# ----- macOS: kqueue -----


class _KqueueWatcher(FsWatcher):
    def __init__(self) -> None:
        self._kq = select.kqueue()
        self._fds: dict[Path, int] = {}
        self._paths: dict[int, Path] = {}

    def add_path(self, path: Path) -> None:
        if path in self._fds:
            return
        fd = os.open(str(path), os.O_RDONLY)
        self._fds[path] = fd
        self._paths[fd] = path
        ev = select.kevent(
            fd,
            filter=select.KQ_FILTER_VNODE,
            flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
            fflags=(
                select.KQ_NOTE_WRITE
                | select.KQ_NOTE_EXTEND
                | select.KQ_NOTE_DELETE
                | select.KQ_NOTE_RENAME
            ),
        )
        self._kq.control([ev], 0, 0)

    def remove_path(self, path: Path) -> None:
        fd = self._fds.pop(path, None)
        if fd is None:
            return
        self._paths.pop(fd, None)
        try:
            os.close(fd)
        except OSError:
            pass

    def wait(self, timeout: float) -> set[Path]:
        try:
            events = self._kq.control(None, 16, timeout)
        except InterruptedError:
            return set()
        touched: set[Path] = set()
        for ev in events:
            p = self._paths.get(ev.ident)
            if p is not None:
                touched.add(p)
        return touched

    def close(self) -> None:
        for fd in list(self._fds.values()):
            try:
                os.close(fd)
            except OSError:
                pass
        self._fds.clear()
        self._paths.clear()
        try:
            self._kq.close()
        except OSError:
            pass


# ----- Linux/WSL: inotify via ctypes -----


_IN_MODIFY = 0x0002
_IN_CREATE = 0x0100
_IN_DELETE = 0x0200
_IN_MOVED_FROM = 0x0040
_IN_MOVED_TO = 0x0080
_IN_CLOEXEC = 0o2000000
_IN_NONBLOCK = 0o4000

# struct inotify_event { int wd; uint32_t mask; uint32_t cookie; uint32_t len; char name[]; }
_INOTIFY_HEADER = struct.Struct("=iIII")


def _load_libc() -> ctypes.CDLL | None:
    name = ctypes.util.find_library("c")
    if not name:
        return None
    try:
        return ctypes.CDLL(name, use_errno=True)
    except OSError:
        return None


class _InotifyWatcher(FsWatcher):
    def __init__(self) -> None:
        libc = _load_libc()
        if libc is None:
            raise OSError("libc not available")
        self._libc = libc
        self._libc.inotify_init1.argtypes = [ctypes.c_int]
        self._libc.inotify_init1.restype = ctypes.c_int
        self._libc.inotify_add_watch.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint32,
        ]
        self._libc.inotify_add_watch.restype = ctypes.c_int
        self._libc.inotify_rm_watch.argtypes = [ctypes.c_int, ctypes.c_int]
        self._libc.inotify_rm_watch.restype = ctypes.c_int

        fd = self._libc.inotify_init1(_IN_CLOEXEC | _IN_NONBLOCK)
        if fd < 0:
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err))
        self._fd = fd
        self._poll = select.poll()
        self._poll.register(self._fd, select.POLLIN)
        self._wd: dict[Path, int] = {}
        self._paths: dict[int, Path] = {}

    def add_path(self, path: Path) -> None:
        if path in self._wd:
            return
        mask = _IN_MODIFY | _IN_CREATE | _IN_DELETE | _IN_MOVED_FROM | _IN_MOVED_TO
        wd = self._libc.inotify_add_watch(self._fd, os.fsencode(str(path)), mask)
        if wd < 0:
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err))
        self._wd[path] = wd
        self._paths[wd] = path

    def remove_path(self, path: Path) -> None:
        wd = self._wd.pop(path, None)
        if wd is None:
            return
        self._paths.pop(wd, None)
        self._libc.inotify_rm_watch(self._fd, wd)

    def wait(self, timeout: float) -> set[Path]:
        ready = self._poll.poll(int(timeout * 1000))
        if not ready:
            return set()
        touched: set[Path] = set()
        try:
            buf = os.read(self._fd, 4096)
        except BlockingIOError:
            return set()
        except OSError as e:
            if e.errno == errno.EAGAIN:
                return set()
            raise
        i = 0
        while i + _INOTIFY_HEADER.size <= len(buf):
            wd, _mask, _cookie, name_len = _INOTIFY_HEADER.unpack_from(buf, i)
            i += _INOTIFY_HEADER.size + name_len
            p = self._paths.get(wd)
            if p is not None:
                touched.add(p)
        return touched

    def close(self) -> None:
        for wd in list(self._wd.values()):
            self._libc.inotify_rm_watch(self._fd, wd)
        self._wd.clear()
        self._paths.clear()
        try:
            os.close(self._fd)
        except OSError:
            pass


# ----- Fallback polling -----


class _PollWatcher(FsWatcher):
    def __init__(self, interval: float = 0.05) -> None:
        self._interval = interval
        self._state: dict[Path, tuple[float, int]] = {}

    def _stat(self, path: Path) -> tuple[float, int] | None:
        try:
            st = path.stat()
            return (st.st_mtime, st.st_size)
        except FileNotFoundError:
            return None

    def add_path(self, path: Path) -> None:
        if path in self._state:
            return
        s = self._stat(path)
        self._state[path] = s if s is not None else (0.0, 0)

    def remove_path(self, path: Path) -> None:
        self._state.pop(path, None)

    def wait(self, timeout: float) -> set[Path]:
        deadline = time.time() + timeout
        while True:
            touched: set[Path] = set()
            for path, prev in list(self._state.items()):
                cur = self._stat(path)
                if cur is None:
                    if prev != (0.0, 0):
                        self._state[path] = (0.0, 0)
                        touched.add(path)
                    continue
                if cur != prev:
                    self._state[path] = cur
                    touched.add(path)
            if touched:
                return touched
            remaining = deadline - time.time()
            if remaining <= 0:
                return set()
            time.sleep(min(self._interval, remaining))

    def close(self) -> None:
        self._state.clear()


def new_watcher() -> FsWatcher:
    """Pick the best available backend for this platform."""
    if sys.platform == "darwin":
        try:
            return _KqueueWatcher()
        except OSError:
            pass
    elif sys.platform.startswith("linux"):
        try:
            return _InotifyWatcher()
        except OSError:
            pass
    return _PollWatcher()
