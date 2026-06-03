"""`live tail -f` follow loop.

Watches the active idx for new records, the session dir for rotation, and the
lock for liveness changes. Stops on graceful/torn exit, on SIGINT, or on
session dir disappearance.
"""

from __future__ import annotations

import signal
import sys
import time
from pathlib import Path

from .config import Config
from .format import (
    LOCK_NAME,
    idx_name,
    list_segments,
    read_idx_records,
    stream_name,
)
from .lock import probe_held
from .reader import (
    lines_in_segment,
    partial_tail_bytes,
    stream_segment_bytes,
    strip_ansi,
)
from .sweep import SessionInfo, session_info
from .watcher import new_watcher
from .paths import Scope


_INTERRUPTED = False


def _on_sigint(_sig, _frm) -> None:
    global _INTERRUPTED
    _INTERRUPTED = True


def follow_session(
    *,
    scope: Scope,
    cfg: Config,
    info: SessionInfo,
    initial_cursor: int,
    strip: bool,
) -> int:
    """Emit new lines as they arrive; return 0 on clean exit."""
    global _INTERRUPTED
    _INTERRUPTED = False
    prev_handler = signal.signal(signal.SIGINT, _on_sigint)

    session_dir = info.path
    cursor = initial_cursor
    last_partial_bytes = 0
    hung_emitted = False

    watcher = new_watcher()
    try:
        watcher.add_path(session_dir)
        active_seg = list_segments(session_dir).nums
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
            segs = list_segments(session_dir).nums
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

            # Emit any newly indexed lines, across all segments after the cursor.
            new_cursor, new_partial_bytes = _emit_new_lines(
                session_dir, cursor, strip
            )
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
                    print(
                        f"live: status=hung last-activity={mtime:.3f}",
                        file=sys.stderr,
                    )
                    hung_emitted = True
                continue

            # Lock released -> recorder exited. Drain anything left, then emit trailer.
            final_cursor, _ = _emit_new_lines(session_dir, cursor, strip)
            cursor = max(cursor, final_cursor)
            _emit_exit_trailer(session_dir, info.id, cursor, cfg)
            return 0

        return 0
    finally:
        watcher.close()
        signal.signal(signal.SIGINT, prev_handler)


def _emit_new_lines(session_dir: Path, cursor: int, strip: bool) -> tuple[int, int]:
    """Emit lines with n > cursor across all segments. Return (new_cursor, partial_bytes)."""
    segs = list_segments(session_dir).nums
    new_cursor = cursor
    partial_bytes = 0
    out = bytearray()
    for seg in segs:
        records = read_idx_records(session_dir / idx_name(seg))
        if not records:
            continue
        seg_last = records[-1][0]
        if seg_last <= cursor:
            continue
        stream = stream_segment_bytes(session_dir / stream_name(seg))
        lines = lines_in_segment(stream, records)
        for rec_idx, (n, _t) in enumerate(records):
            if n <= cursor or rec_idx >= len(lines):
                continue
            out.extend(lines[rec_idx])
            new_cursor = max(new_cursor, n)
        # Track partial-line bytes only on the active (highest) segment.
        if seg == segs[-1]:
            tail = partial_tail_bytes(stream, records)
            partial_bytes = len(tail)
            if tail:
                out.extend(tail)
    if out:
        data = strip_ansi(bytes(out)) if strip else bytes(out)
        try:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
        except BrokenPipeError:
            pass
    return new_cursor, partial_bytes


def _emit_exit_trailer(
    session_dir: Path, session_id: str, cursor: int, cfg: Config
) -> None:
    info = session_info(session_dir, cfg)
    if info is not None and info.status == "exited" and info.exit_code is not None:
        print(f"live: exit-code={info.exit_code}", file=sys.stderr)
    elif info is not None and info.status == "inconsistent":
        print("live: exit=inconsistent", file=sys.stderr)
    print(f"live: id={session_id} at-line={cursor}", file=sys.stderr)
