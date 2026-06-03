"""`live tail -f` follow loop: stream new lines until the recorder exits or SIGINT."""

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
    at_time_of,
    lines_in_segment,
    partial_tail_bytes,
    stream_segment_bytes,
    strip_ansi,
)
from .sweep import SessionInfo, session_info
from .watcher import new_watcher


_INTERRUPTED = False


def _on_sigint(_sig, _frm) -> None:
    global _INTERRUPTED
    _INTERRUPTED = True


def follow_session(
    *,
    cfg: Config,
    info: SessionInfo,
    initial_cursor: int,
    initial_partial_bytes: int = 0,
    initial_partial_seg: int | None = None,
    strip: bool,
) -> int:
    """Emit new lines as they arrive; return 0 on clean exit."""
    global _INTERRUPTED
    _INTERRUPTED = False
    prev_handler = signal.signal(signal.SIGINT, _on_sigint)

    session_dir = info.path
    cursor = initial_cursor
    partial_emitted = initial_partial_bytes
    partial_seg: int | None = initial_partial_seg
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

            new_cursor, partial_emitted, partial_seg = _emit_new_lines(
                session_dir, cursor, strip,
                partial_emitted=partial_emitted, partial_seg=partial_seg,
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
            final_cursor, _, _ = _emit_new_lines(
                session_dir, cursor, strip,
                partial_emitted=partial_emitted, partial_seg=partial_seg,
            )
            cursor = max(cursor, final_cursor)
            _emit_exit_trailer(session_dir, info.id, cursor, cfg)
            return 0

        return 0
    finally:
        watcher.close()
        signal.signal(signal.SIGINT, prev_handler)


def _emit_new_lines(
    session_dir: Path,
    cursor: int,
    strip: bool,
    *,
    partial_emitted: int,
    partial_seg: int | None,
) -> tuple[int, int, int | None]:
    """Emit content past (cursor, partial_emitted on partial_seg). Returns updated state."""
    segs = list_segments(session_dir)
    new_cursor = cursor
    new_partial_emitted = partial_emitted
    new_partial_seg = partial_seg
    out = bytearray()
    if not segs:
        return new_cursor, new_partial_emitted, new_partial_seg

    pending = partial_emitted
    pending_seg = partial_seg

    for seg in segs:
        records = read_idx_records(session_dir / idx_name(seg))
        stream = stream_segment_bytes(session_dir / stream_name(seg))
        lines = lines_in_segment(stream, records)

        for rec_idx, (n, _t) in enumerate(records):
            if n <= cursor or rec_idx >= len(lines):
                continue
            line = lines[rec_idx]
            # Bytes already shown as partial are now absorbed into this line; trim.
            if pending and pending_seg == seg:
                line = line[pending:]
                pending = 0
                pending_seg = None
            out.extend(line)
            new_cursor = max(new_cursor, n)

        # Partial tail can only live on the active segment (rotation is line-aligned).
        if seg == segs[-1]:
            tail = partial_tail_bytes(stream, records)
            already = pending if pending_seg == seg else 0
            if len(tail) > already:
                out.extend(tail[already:])
                new_partial_emitted = len(tail)
                new_partial_seg = seg
            elif len(tail) == 0:
                new_partial_emitted = 0
                new_partial_seg = None
            else:
                new_partial_emitted = len(tail)
                new_partial_seg = seg

    if out:
        data = strip_ansi(bytes(out)) if strip else bytes(out)
        try:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
        except BrokenPipeError:
            pass
    return new_cursor, new_partial_emitted, new_partial_seg


def _emit_exit_trailer(
    session_dir: Path, session_id: str, cursor: int, cfg: Config
) -> None:
    info = session_info(session_dir, cfg)
    at_time = at_time_of(session_dir)
    if info is not None and info.status == "exited" and info.exit_code is not None:
        print(f"live: exit-code={info.exit_code}", file=sys.stderr)
    elif info is not None and info.status == "inconsistent":
        print("live: exit=inconsistent", file=sys.stderr)
    print(
        f"live: id={session_id} at-line={cursor} at-time={at_time:.3f}",
        file=sys.stderr,
    )
