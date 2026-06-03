"""Lifecycle sweep and per-session status reporting."""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .format import (
    DEAD_NAME,
    INCONSISTENT_MARKER,
    LOCK_NAME,
    Meta,
    Watermarks,
    compute_watermarks,
    count_complete_lines,
    idx_name,
    idx_record_count,
    list_segments,
    read_meta,
    stream_name,
)
from .lock import probe_held
from .paths import Scope


@dataclass
class SessionInfo:
    """Aggregated session view. All times are float seconds."""

    id: str
    path: Path
    meta: Meta
    status: str  # running | hung | exited | inconsistent
    watermarks: Watermarks
    last_activity: float  # seconds since epoch
    exited_at: float | None
    exit_code: int | None


def _read_deadat(dead_path: Path) -> bytes | None:
    try:
        return dead_path.read_bytes()
    except FileNotFoundError:
        return None


def _verdict_inconsistent(session_dir: Path) -> bool:
    """Compute the consistency verdict for sweep stamping.

    Equal counts -> consistent (False).
    Any drift -> inconsistent (True). Missing idx counts as 0 records.
    """
    segs = list_segments(session_dir).nums
    if not segs:
        return False
    last_seg = segs[-1]
    stream_lines = count_complete_lines(session_dir / stream_name(last_seg))
    idx_lines = idx_record_count(session_dir / idx_name(last_seg))
    return stream_lines != idx_lines


def _stamp_dead(dead_path: Path, *, inconsistent: bool) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(str(dead_path), flags, 0o600)
        try:
            if inconsistent:
                os.write(fd, INCONSISTENT_MARKER)
        finally:
            os.close(fd)
    except FileExistsError:
        pass


def sweep_one(session_dir: Path, cfg: Config) -> None:
    """Stamp deadAt for dead-but-unmarked sessions; delete TTL-expired ones."""
    lock_path = session_dir / LOCK_NAME
    held = probe_held(lock_path)
    if held is None:
        return  # session in startup; skip this round
    if held:
        return  # recorder alive

    dead = session_dir / DEAD_NAME
    if not dead.exists():
        try:
            _stamp_dead(dead, inconsistent=_verdict_inconsistent(session_dir))
        except FileNotFoundError:
            return

    # TTL: delete if older than ttlDays.
    try:
        mtime = os.path.getmtime(dead)
    except FileNotFoundError:
        return
    if time.time() - mtime > cfg.ttl_days * 86400:
        try:
            shutil.rmtree(session_dir, ignore_errors=True)
        except FileNotFoundError:
            pass


def sweep_all(scope: Scope, cfg: Config) -> None:
    sessions_dir = scope.sessions_dir
    if not sessions_dir.exists():
        return
    try:
        entries = list(os.scandir(sessions_dir))
    except FileNotFoundError:
        return
    for entry in entries:
        if not entry.is_dir(follow_symlinks=False):
            continue
        sweep_one(Path(entry.path), cfg)


def status_of(session_dir: Path, cfg: Config) -> str:
    """Resolve the four-way status for a session directory."""
    lock_path = session_dir / LOCK_NAME
    dead = session_dir / DEAD_NAME
    held = probe_held(lock_path)
    if held:
        # Live; check staleness for "hung".
        segs = list_segments(session_dir).nums
        last_act = 0.0
        if segs:
            try:
                last_act = os.path.getmtime(session_dir / idx_name(segs[-1]))
            except FileNotFoundError:
                last_act = 0.0
        if not last_act:
            try:
                last_act = os.path.getmtime(session_dir)
            except FileNotFoundError:
                last_act = time.time()
        if time.time() - last_act > 3 * cfg.heartbeat_sec:
            return "hung"
        return "running"

    # Dead. Look at deadAt verdict.
    payload = _read_deadat(dead)
    if payload is None:
        # No deadAt yet; sweep will stamp soon. Treat as inconsistent observer.
        return "exited"
    if payload.strip() == INCONSISTENT_MARKER.strip():
        return "inconsistent"
    return "exited"


def session_info(session_dir: Path, cfg: Config) -> SessionInfo | None:
    meta = read_meta(session_dir)
    if meta is None:
        return None  # starting / malformed; skip
    wm = compute_watermarks(session_dir)
    segs = list_segments(session_dir).nums
    last_activity = 0.0
    if segs:
        try:
            last_activity = os.path.getmtime(session_dir / idx_name(segs[-1]))
        except FileNotFoundError:
            last_activity = 0.0
    if not last_activity:
        try:
            last_activity = os.path.getmtime(session_dir)
        except FileNotFoundError:
            last_activity = meta.started_at

    status = status_of(session_dir, cfg)

    # Resolve exitedAt precedence.
    exited_at: float | None = None
    exit_code: int | None = None
    if meta.exited_at is not None:
        exited_at = meta.exited_at
        exit_code = meta.exit_code
    elif status in ("exited", "inconsistent"):
        dead = session_dir / DEAD_NAME
        try:
            exited_at = os.path.getmtime(dead)
        except FileNotFoundError:
            exited_at = last_activity

    return SessionInfo(
        id=meta.id,
        path=session_dir.resolve(),
        meta=meta,
        status=status,
        watermarks=wm,
        last_activity=last_activity,
        exited_at=exited_at,
        exit_code=exit_code,
    )


def list_sessions(scope: Scope, cfg: Config) -> list[SessionInfo]:
    sessions_dir = scope.sessions_dir
    out: list[SessionInfo] = []
    if not sessions_dir.exists():
        return out
    try:
        entries = list(os.scandir(sessions_dir))
    except FileNotFoundError:
        return out
    for entry in entries:
        if not entry.is_dir(follow_symlinks=False):
            continue
        try:
            info = session_info(Path(entry.path), cfg)
        except FileNotFoundError:
            continue
        if info is not None:
            out.append(info)
    # Newest-first by UUIDv7 lex.
    out.sort(key=lambda s: s.id, reverse=True)
    return out
