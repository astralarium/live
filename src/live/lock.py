"""flock-based liveness primitives.

Recorder holds an exclusive flock on `process.lock` for its lifetime.
Probes try LOCK_EX | LOCK_NB on a fresh fd and close immediately:
  - acquired (success) -> recorder is dead
  - EAGAIN/EWOULDBLOCK -> recorder is alive
"""

from __future__ import annotations

import fcntl
import os
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


def kill_pid(pid: int, sig: int) -> bool:
    """Send signal to pid. Returns False if the process doesn't exist."""
    try:
        os.kill(pid, sig)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
