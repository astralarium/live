"""flock-based liveness primitives.

Recorder holds an exclusive flock on `process.lock` for its lifetime.
Probes try LOCK_EX | LOCK_NB on a fresh fd and close immediately:
  - acquired (success) -> recorder is dead
  - EAGAIN/EWOULDBLOCK -> recorder is alive
"""

from __future__ import annotations

import fcntl
import os
import sys
import time
from pathlib import Path


def acquire_lock(lock_path: Path, pid: int) -> int:
    """Create/open lock file, take exclusive non-blocking flock, write pid.

    Returns the open fd; caller must keep it open until exit. Raises BlockingIOError
    if the lock is already held by another process.
    """
    fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise
    os.ftruncate(fd, 0)
    os.write(fd, f"{pid}\n".encode("ascii"))
    return fd


def probe_held(lock_path: Path) -> bool | None:
    """Probe the lock without holding it.

    Returns:
      True  - lock is held by some live process (recorder running/hung)
      False - lock file exists but no one holds it (recorder dead)
      None  - lock file doesn't exist (session in startup, or no such session)
    """
    try:
        fd = os.open(str(lock_path), os.O_RDONLY)
    except FileNotFoundError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        # We got the lock -> recorder is gone. Release before closing.
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        return False
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def read_lock_pid(lock_path: Path) -> int | None:
    try:
        with lock_path.open("rb") as f:
            raw = f.read(32).strip()
        if not raw:
            return None
        return int(raw)
    except (FileNotFoundError, ValueError):
        return None


class LockTimeout(Exception):
    """Raised when a `HeldLock` can't be acquired within its timeout."""

    pass


class HeldLock:
    """Exclusive flock, held until `release()` (idempotent).

    Acquisition polls with LOCK_NB: a one-line wait notice (naming the
    holder's pid) goes to stderr after `notice_after` seconds, and
    `LockTimeout` is raised at `timeout`. The holder's pid is stamped into
    the file so waiters can identify a wedged holder. Aborting (rather than
    proceeding unlocked) is deliberate: the lock guards a check-then-create
    race.

    O_CLOEXEC drops the fd in exec'd children; a forked child inherits it,
    but flock is per open-file-description, so the parent's LOCK_UN releases
    the lock regardless. Forked children that outlive the parent must call
    `close_inherited()` — otherwise their fd keeps the lock alive should the
    parent die without releasing (e.g. SIGHUP from a closing terminal).
    """

    def __init__(
        self,
        lock_path: Path,
        *,
        timeout: float = 5.0,
        notice_after: float = 1.0,
    ):
        self._fd = os.open(
            str(lock_path), os.O_WRONLY | os.O_CREAT | os.O_CLOEXEC, 0o600
        )
        deadline = time.monotonic() + timeout
        notice_at = time.monotonic() + notice_after
        while True:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                now = time.monotonic()
                if notice_at is not None and now >= notice_at:
                    print(
                        f"live: waiting for name lock{_holder(lock_path)}",
                        file=sys.stderr,
                    )
                    notice_at = None
                if now >= deadline:
                    os.close(self._fd)
                    self._fd = -1
                    raise LockTimeout(
                        f"timed out waiting for name lock{_holder(lock_path)}"
                    )
                time.sleep(0.05)
        os.ftruncate(self._fd, 0)
        os.write(self._fd, f"{os.getpid()}\n".encode("ascii"))

    def release(self) -> None:
        if self._fd < 0:
            return
        fd, self._fd = self._fd, -1
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)

    def close_inherited(self) -> None:
        """Close a forked child's copy of the fd WITHOUT unlocking — LOCK_UN
        on the shared open-file-description would drop the parent's lock
        mid-critical-section. With no child copies left, parent death
        auto-releases via the kernel."""
        if self._fd < 0:
            return
        fd, self._fd = self._fd, -1
        os.close(fd)


def _holder(lock_path: Path) -> str:
    """` (held by pid N)` for wait/timeout diagnostics; empty if unknown."""
    pid = read_lock_pid(lock_path)
    return f" (held by pid {pid})" if pid else ""


def kill_pid(pid: int, sig: int) -> bool:
    """Send signal to pid. A vanished process counts as delivered (it is
    already stopped); returns False only when the signal cannot be sent
    (e.g. EPERM on another user's process)."""
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        return False
    return True
