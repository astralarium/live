"""`live tail -f` follow loop: stream new bytes until the recorder exits or SIGINT."""

from __future__ import annotations

import signal
import sys
import time
from pathlib import Path

from .ansi import strip_ansi
from .config import Config
from .format import (
    LOCK_NAME,
    compute_watermarks,
    idx_name,
    list_segments,
)
from .lock import probe_held
from .reader import last_time_of, load_stream_view
from .session import SessionInfo, session_info
from .verbose import emit_exit, emit_hung, emit_trailer
from .watcher import new_watcher


_INTERRUPTED = False


def _on_sigint(_sig, _frm) -> None:
    global _INTERRUPTED
    _INTERRUPTED = True


def follow_session(
    *,
    cfg: Config,
    info: SessionInfo,
    initial_byte: int,
    strip: bool,
) -> int:
    """Emit new bytes as they arrive; return 0 on clean exit.

    The cursor is a lifetime byte offset — lines and partial-line bytes are
    streamed uniformly, so a line spanning a rotation needs no special case.
    """
    global _INTERRUPTED
    _INTERRUPTED = False
    prev_handler = signal.signal(signal.SIGINT, _on_sigint)

    session_dir = info.path
    cursor = initial_byte
    hung_emitted = False

    watcher = new_watcher()
    try:
        watcher.add_path(session_dir)
        active_seg = list_segments(session_dir)
        active_idx_path: Path | None = None
        if active_seg:
            active_idx_path = session_dir / idx_name(active_seg[-1])
            try:
                watcher.add_path(active_idx_path)
            except OSError:
                active_idx_path = None

        while not _INTERRUPTED:
            # Short timeout so SIGINT and hung-detection re-checks aren't blocked
            # by PEP 475 syscall auto-retry. File events still wake instantly.
            try:
                watcher.wait(1.0)
            except OSError:
                pass

            if _INTERRUPTED:
                break

            # Detect rotation: new highest segment appearing in the directory.
            segs = list_segments(session_dir)
            if not segs:
                # Session dir gone or empty -> recorder probably tore down.
                break
            new_active = session_dir / idx_name(segs[-1])
            if active_idx_path != new_active:
                if active_idx_path is not None:
                    watcher.remove_path(active_idx_path)
                active_idx_path = new_active
                try:
                    watcher.add_path(active_idx_path)
                except OSError:
                    pass

            new_cursor = _emit_new_bytes(session_dir, cursor, strip)
            if new_cursor > cursor:
                cursor = new_cursor
                hung_emitted = False  # fresh activity clears the hung state

            # Hung detection: stale idx mtime past 3 * heartbeatSec, lock still held.
            held = probe_held(session_dir / LOCK_NAME)
            if held is True:
                try:
                    mtime = (session_dir / idx_name(segs[-1])).stat().st_mtime
                except FileNotFoundError:
                    mtime = time.time()
                if time.time() - mtime > 3 * cfg.heartbeat_sec and not hung_emitted:
                    emit_hung(mtime)
                    hung_emitted = True
                continue

            # Lock released -> recorder exited. Drain anything left, then emit trailer.
            cursor = max(cursor, _emit_new_bytes(session_dir, cursor, strip))
            _emit_exit_trailer(session_dir, info.id, cursor, cfg)
            return 0

        return 0
    finally:
        watcher.close()
        signal.signal(signal.SIGINT, prev_handler)


def _emit_new_bytes(session_dir: Path, cursor: int, strip: bool) -> int:
    """Emit stream bytes past lifetime offset `cursor`; return the new cursor.

    If retention outran the cursor, note the dropped span on stderr and
    resume from the floor.
    """
    view = load_stream_view(session_dir, from_byte=cursor)
    if view.base > cursor:
        print(
            f"live: dropped {view.base - cursor} bytes"
            f" (from-byte={cursor}, first-byte={view.base})",
            file=sys.stderr,
        )
    out = view.slice(max(cursor, view.base), view.tip)
    if out:
        data = strip_ansi(out) if strip else out
        try:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
        except BrokenPipeError:
            pass
    return max(cursor, view.tip)


def _emit_exit_trailer(
    session_dir: Path, session_id: str, cursor: int, cfg: Config
) -> None:
    emit_exit(session_info(session_dir, cfg))
    next_line = compute_watermarks(session_dir).last_line + 1
    emit_trailer(session_id, next_line, cursor, last_time_of(session_dir))
