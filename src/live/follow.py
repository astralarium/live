"""`live tail -f` follow loop: stream new bytes until the recorder exits or SIGINT."""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

from .ansi import incomplete_escape_len, strip_ansi
from .config import Config
from .format import (
    LOCK_NAME,
    compute_watermarks,
    idx_name,
    list_segments,
    read_meta,
    stream_name,
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
    verbose: bool,
) -> int:
    """Emit new bytes as they arrive; return 0 on clean exit.

    The cursor is a lifetime byte offset — lines and partial-line bytes are
    streamed uniformly, so a line spanning a rotation needs no special case.

    `verbose` gates all stderr metadata (dropped/hung/exit/trailer), matching
    the non-follow verbose contract. EPIPE from downstream (e.g. `| head`)
    ends the follow immediately.
    """
    global _INTERRUPTED
    _INTERRUPTED = False
    prev_handler = signal.signal(signal.SIGINT, _on_sigint)

    session_dir = info.path
    cursor = initial_byte
    hung_emitted = False

    watcher = new_watcher()
    watched_seg: int | None = None
    watched_paths: list[Path] = []

    def _watch_segment(seg: int) -> None:
        """Re-arm per-segment watches: the idx (completed lines) and the
        stream (partial-line bytes — GNU `tail -f` latency for progress bars
        and prompts; the idx only changes on a newline)."""
        nonlocal watched_seg
        for old in watched_paths:
            watcher.remove_path(old)
        watched_paths.clear()
        for p in (session_dir / idx_name(seg), session_dir / stream_name(seg)):
            try:
                watcher.add_path(p)
                watched_paths.append(p)
            except OSError:
                pass
        watched_seg = seg

    try:
        watcher.add_path(session_dir)
        active_seg = list_segments(session_dir)
        if active_seg:
            _watch_segment(active_seg[-1])

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
                # Segments never vanish under a live recorder: the session
                # was removed (`live rm`) mid-follow.
                return _report_removed()
            if segs[-1] != watched_seg:
                _watch_segment(segs[-1])

            try:
                new_cursor = _emit_new_bytes(session_dir, cursor, strip, verbose)
            except BrokenPipeError:
                return _drop_stdout()
            if new_cursor > cursor:
                cursor = new_cursor
                hung_emitted = False  # fresh activity clears the hung state

            # Hung detection: stale idx mtime past 3 * heartbeatSec, lock still held.
            held = probe_held(session_dir / LOCK_NAME)
            if held is None:
                # Lock file gone (a graceful exit releases but never unlinks
                # it): the session was removed mid-follow.
                return _report_removed()
            if held is True:
                meta = read_meta(session_dir)
                if meta is not None and meta.tty_closed_at is not None:
                    # The child closed its terminal: no output can ever
                    # arrive again. Drain what's left and exit instead of
                    # waiting on a permanently dry stream.
                    try:
                        cursor = max(
                            cursor,
                            _emit_new_bytes(
                                session_dir, cursor, strip, verbose, flush=True
                            ),
                        )
                    except BrokenPipeError:
                        return _drop_stdout()
                    if verbose:
                        print("live: tty closed; no further output", file=sys.stderr)
                        _emit_exit_trailer(session_dir, info.id, cursor, cfg)
                    return 0
                try:
                    mtime = (session_dir / idx_name(segs[-1])).stat().st_mtime
                except FileNotFoundError:
                    mtime = time.time()
                if time.time() - mtime > 3 * cfg.heartbeat_sec and not hung_emitted:
                    if verbose:
                        emit_hung(mtime)
                    hung_emitted = True
                continue

            # Lock released -> recorder exited. Drain anything left (flushing
            # any held-back escape bytes), then emit trailer.
            try:
                cursor = max(
                    cursor,
                    _emit_new_bytes(session_dir, cursor, strip, verbose, flush=True),
                )
            except BrokenPipeError:
                return _drop_stdout()
            if verbose:
                _emit_exit_trailer(session_dir, info.id, cursor, cfg)
            return 0

        return 0
    finally:
        watcher.close()
        signal.signal(signal.SIGINT, prev_handler)


def _emit_new_bytes(
    session_dir: Path,
    cursor: int,
    strip: bool,
    verbose: bool,
    *,
    flush: bool = False,
) -> int:
    """Emit stream bytes past lifetime offset `cursor`; return the new cursor.

    If retention outran the cursor, note the dropped span on stderr (verbose
    only; 1-based positions) and resume from the floor. In strip mode an
    unterminated trailing escape is held back — the cursor stops short of it
    — so a CSI/OSC torn across drains is never emitted as junk; `flush`
    (final drain) emits it regardless. EPIPE propagates to the caller.
    """
    view = load_stream_view(session_dir, from_byte=cursor)
    if view.base > cursor and verbose:
        print(
            f"live: dropped {view.base - cursor} bytes"
            f" (from-byte={cursor + 1}, first-byte={view.base + 1})",
            file=sys.stderr,
        )
    out = view.slice(max(cursor, view.base), view.tip)
    new_cursor = max(cursor, view.tip)
    if strip and not flush:
        hold = incomplete_escape_len(out)
        if hold:
            out = out[:-hold]
            new_cursor -= hold
    if out:
        data = strip_ansi(out) if strip else out
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    return new_cursor


def _report_removed() -> int:
    """Session deleted out from under the follower; errors always print."""
    print("live: session removed", file=sys.stderr)
    return 1


def _drop_stdout() -> int:
    """After downstream EPIPE: point stdout at /dev/null so interpreter-exit
    flushes can't raise again, and report a clean exit."""
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.close(devnull)
    return 0


def _emit_exit_trailer(
    session_dir: Path, session_id: str, cursor: int, cfg: Config
) -> None:
    emit_exit(session_info(session_dir, cfg))
    next_line = compute_watermarks(session_dir).last_line + 1
    # `cursor` is a 0-based offset; the trailer speaks 1-based positions.
    emit_trailer(session_id, next_line, cursor + 1, last_time_of(session_dir))
