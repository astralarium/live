"""Reader helpers: segment scanning, line ranges, partial-line tail, ANSI strip."""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .format import (
    first_idx_record,
    idx_name,
    last_idx_record,
    list_segments,
    read_idx_records,
    stream_name,
)

# ECMA-48 / VT100 escape sequences.
_ANSI_RE = re.compile(
    rb"""
    \x1B
    (?:
        \[ [0-?]* [ -/]* [@-~]    # CSI
      | \] [^\x07]*? (?:\x07|\x1B\\)  # OSC ... BEL or ESC \\
      | [@-_]                     # 2-byte (Fp, Fe, Fs)
    )
    """,
    re.VERBOSE,
)


def strip_ansi(data: bytes) -> bytes:
    """Remove ANSI/VT escape sequences from byte stream."""
    return _ANSI_RE.sub(b"", data)


def should_strip_ansi(
    *,
    explicit_strip: bool,
    explicit_raw: bool,
    stdout_is_tty: bool,
) -> bool:
    """Resolve --strip-ansi/--raw/default-by-TTY rules.

    Default: strip when stdout isn't a TTY (agent pipes get plain text; humans
    in a terminal get colors). Explicit flags override.
    """
    if explicit_raw:
        return False
    if explicit_strip:
        return True
    return not stdout_is_tty


@dataclass(frozen=True)
class SegmentRef:
    seg: int
    stream_path: Path
    idx_path: Path


def segment_refs(session_dir: Path) -> list[SegmentRef]:
    segs = list_segments(session_dir)
    return [
        SegmentRef(
            seg=s,
            stream_path=session_dir / stream_name(s),
            idx_path=session_dir / idx_name(s),
        )
        for s in segs
    ]


def stream_segment_bytes(stream_path: Path) -> bytes:
    try:
        return stream_path.read_bytes()
    except FileNotFoundError:
        return b""


def lines_in_segment(stream: bytes, idx_records: list[tuple[int, int]]) -> list[bytes]:
    """Split the stream bytes into N complete lines matching idx_records.

    Each line includes its trailing `\\n`. Any partial tail (no `\\n`) is dropped.
    """
    line_count = len(idx_records)
    if line_count == 0:
        return []
    lines: list[bytes] = []
    start = 0
    for _ in range(line_count):
        nl = stream.find(b"\n", start)
        if nl < 0:
            break
        lines.append(stream[start : nl + 1])
        start = nl + 1
    return lines


def partial_tail_bytes(stream: bytes, idx_records: list[tuple[int, int]]) -> bytes:
    """Return any bytes after the last `\\n` that idx_records covers."""
    line_count = len(idx_records)
    pos = 0
    for _ in range(line_count):
        nl = stream.find(b"\n", pos)
        if nl < 0:
            return b""  # malformed; bail
        pos = nl + 1
    return stream[pos:]


@dataclass
class ReadResult:
    """Output of a cat/head/tail invocation, before optional ANSI stripping."""

    stdout: bytes
    # Stderr lines (without trailing newlines), in canonical order.
    stderr_lines: list[str]
    first_line: int  # first n actually emitted (0 if none)
    last_line: int  # trailer cursor — agents resume with `tail -n +<L+1>`
    at_time: float  # wall-clock time of last write (active stream mtime); 0.0 if no segment
    at_byte: int  # cumulative byte cursor (where -c +B would resume)
    dropped: int  # k lines dropped (gap)
    first_retained: int  # firstLine of session at read time
    partial_bytes: int  # k bytes in partial-line tail
    partial_age: float  # age of partial line in seconds (0.0 if none)
    partial_seg: int | None  # segment number carrying the partial (None if no partial)


def at_time_of(session_dir: Path) -> float:
    """Wall-clock time of the most recent byte written to the active stream.
    Returns 0.0 if no segment exists. Heartbeats only touch the idx, so this
    reflects real byte writes — partial-line bytes included."""
    segs = list_segments(session_dir)
    if not segs:
        return 0.0
    try:
        return os.path.getmtime(session_dir / stream_name(segs[-1]))
    except FileNotFoundError:
        return 0.0


def _first_and_last_line(refs: list[SegmentRef]) -> tuple[int, int]:
    """Scan refs from each end for the first/last indexed line. (0, 0) if empty."""
    first_line = 0
    for ref in refs:
        rec = first_idx_record(ref.idx_path)
        if rec is not None:
            first_line = rec[0]
            break
    last_line = 0
    for ref in reversed(refs):
        rec = last_idx_record(ref.idx_path)
        if rec is not None:
            last_line = rec[0]
            break
    return first_line, last_line


def at_byte_of(session_dir: Path) -> int:
    """Cumulative byte count across all stream segments (partial bytes included).
    Returns 0 if no segments exist. This is the cursor where `tail -c +K` would
    resume."""
    refs = segment_refs(session_dir)
    total = 0
    for ref in refs:
        try:
            total += os.path.getsize(ref.stream_path)
        except FileNotFoundError:
            pass
    return total


def lines_since(
    session_dir: Path,
    *,
    since: int,
) -> ReadResult:
    """Read lines with n >= since (Unix `tail -n +N` semantics). Includes any
    partial-line tail in stdout."""
    refs = segment_refs(session_dir)
    if not refs:
        return ReadResult(b"", [], 0, 0, 0.0, 0, 0, 0, 0, 0.0, None)

    first_line, last_line = _first_and_last_line(refs)

    stderr_lines: list[str] = []
    dropped = 0
    # Line numbers are 1-indexed; treat since<1 as "from the start" with no gap.
    effective_since = max(since, 1)
    if first_line and effective_since < first_line:
        dropped = first_line - effective_since
        stderr_lines.append(
            f"dropped {dropped} lines (since={since}, first retained={first_line})"
        )
        emit_from = first_line
    else:
        emit_from = max(effective_since, first_line) if first_line else 0

    out = bytearray()
    if first_line and emit_from <= last_line:
        for ref in refs:
            records = read_idx_records(ref.idx_path)
            if not records or records[-1][0] < emit_from:
                continue
            stream = stream_segment_bytes(ref.stream_path)
            lines = lines_in_segment(stream, records)
            for rec_idx, (n, _t) in enumerate(records):
                if n < emit_from:
                    continue
                if rec_idx >= len(lines):
                    break
                out.extend(lines[rec_idx])

    partial_bytes = 0
    partial_age = 0.0
    partial_seg: int | None = None
    at_time = 0.0
    if refs:
        last_ref = refs[-1]
        try:
            at_time = os.path.getmtime(last_ref.stream_path)
        except FileNotFoundError:
            at_time = 0.0
        records = read_idx_records(last_ref.idx_path)
        stream = stream_segment_bytes(last_ref.stream_path)
        tail = partial_tail_bytes(stream, records)
        if tail:
            partial_bytes = len(tail)
            partial_seg = last_ref.seg
            partial_age = max(0.0, time.time() - at_time)
            out.extend(tail)

    return ReadResult(
        stdout=bytes(out),
        stderr_lines=stderr_lines,
        first_line=emit_from,
        last_line=last_line,
        at_time=at_time,
        at_byte=at_byte_of(session_dir),
        dropped=dropped,
        first_retained=first_line,
        partial_bytes=partial_bytes,
        partial_age=partial_age,
        partial_seg=partial_seg,
    )


def cat_all(session_dir: Path) -> ReadResult:
    """Concatenate every stream segment + partial tail."""
    return lines_since(session_dir, since=0)


def lines_since_time(session_dir: Path, *, since_t: float) -> ReadResult:
    """Read lines whose idx timestamp `t > since_t`. Includes partial-line tail."""
    refs = segment_refs(session_dir)
    if not refs:
        return ReadResult(b"", [], 0, 0, 0.0, 0, 0, 0, 0, 0.0, None)

    first_line, last_line = _first_and_last_line(refs)

    out = bytearray()
    for ref in refs:
        records = read_idx_records(ref.idx_path)
        if not records or records[-1][1] <= since_t:
            continue
        stream = stream_segment_bytes(ref.stream_path)
        lines = lines_in_segment(stream, records)
        for rec_idx, (_n, t) in enumerate(records):
            if t <= since_t or rec_idx >= len(lines):
                continue
            out.extend(lines[rec_idx])

    partial_bytes = 0
    partial_age = 0.0
    partial_seg: int | None = None
    at_time = 0.0
    last_ref = refs[-1]
    try:
        at_time = os.path.getmtime(last_ref.stream_path)
    except FileNotFoundError:
        at_time = 0.0
    records = read_idx_records(last_ref.idx_path)
    stream = stream_segment_bytes(last_ref.stream_path)
    tail = partial_tail_bytes(stream, records)
    if tail and at_time > since_t:
        partial_bytes = len(tail)
        partial_seg = last_ref.seg
        partial_age = max(0.0, time.time() - at_time)
        out.extend(tail)

    return ReadResult(
        stdout=bytes(out),
        stderr_lines=[],
        first_line=0,
        last_line=last_line,
        at_time=at_time,
        at_byte=at_byte_of(session_dir),
        dropped=0,
        first_retained=first_line,
        partial_bytes=partial_bytes,
        partial_age=partial_age,
        partial_seg=partial_seg,
    )


def head_first(
    session_dir: Path, *, n_lines: int | None = None, c_bytes: int | None = None
) -> ReadResult:
    """Head the first N lines or first K bytes (excluding partial tail).

    Mirror of `tail_last`. Default N=10 to match Unix `head`. `last_line` is the
    last fully-emitted line; `tail -vn +<L+1>` resumes from there.
    """
    result = cat_all(session_dir)
    body = result.stdout
    if result.partial_bytes:
        body = body[: -result.partial_bytes]

    first_n = result.first_retained or 0

    if c_bytes is not None:
        body = body[:c_bytes] if c_bytes < len(body) else body
    else:
        keep = 10 if n_lines is None else n_lines
        if keep <= 0:
            body = b""
        else:
            count = 0
            i = 0
            while i < len(body) and count < keep:
                j = body.find(b"\n", i)
                if j < 0:
                    break
                count += 1
                i = j + 1
            body = body[:i]

    emitted = body.count(b"\n")
    last_line = (first_n + emitted - 1) if (first_n and emitted) else 0
    return ReadResult(
        stdout=body,
        stderr_lines=result.stderr_lines,
        first_line=first_n if emitted else 0,
        last_line=last_line,
        at_time=result.at_time,
        at_byte=len(body),
        dropped=result.dropped,
        first_retained=first_n,
        partial_bytes=0,
        partial_age=0.0,
        partial_seg=None,
    )


def bytes_since(session_dir: Path, *, since: int) -> ReadResult:
    """Read bytes after virtual offset `since` (tail -c +K). May start mid-line.
    Partial-line bytes are included since they live in the active stream file."""
    refs = segment_refs(session_dir)
    if not refs:
        return ReadResult(b"", [], 0, 0, 0.0, 0, 0, 0, 0, 0.0, None)

    first_line, last_line = _first_and_last_line(refs)

    out = bytearray()
    cumulative = 0
    for ref in refs:
        try:
            size = os.path.getsize(ref.stream_path)
        except FileNotFoundError:
            size = 0
        if cumulative + size > since:
            offset = max(0, since - cumulative)
            out.extend(stream_segment_bytes(ref.stream_path)[offset:])
        cumulative += size

    at_time = at_time_of(session_dir)

    return ReadResult(
        stdout=bytes(out),
        stderr_lines=[],
        first_line=0,
        last_line=last_line,
        at_time=at_time,
        at_byte=cumulative,
        dropped=0,
        first_retained=first_line,
        partial_bytes=0,
        partial_age=0.0,
        partial_seg=None,
    )


def head_drop_last(
    session_dir: Path, *, n_lines: int | None = None, c_bytes: int | None = None
) -> ReadResult:
    """GNU `head -n -K` / `-c -K`: emit everything except the last K lines (or
    K bytes). Partial-line tail excluded."""
    result = cat_all(session_dir)
    body = result.stdout
    if result.partial_bytes:
        body = body[: -result.partial_bytes]

    first_n = result.first_retained or 0

    if c_bytes is not None:
        if c_bytes >= len(body):
            body = b""
        elif c_bytes > 0:
            body = body[:-c_bytes]
    else:
        drop = n_lines or 0
        if drop > 0:
            i = len(body)
            count = 0
            while i > 0 and count < drop:
                j = body.rfind(b"\n", 0, i - 1) if i >= 1 else -1
                count += 1
                if j < 0:
                    i = 0
                    break
                i = j + 1
            body = body[:i]

    emitted = body.count(b"\n")
    last_line = (first_n + emitted - 1) if (first_n and emitted) else 0
    return ReadResult(
        stdout=body,
        stderr_lines=result.stderr_lines,
        first_line=first_n if emitted else 0,
        last_line=last_line,
        at_time=result.at_time,
        at_byte=len(body),
        dropped=result.dropped,
        first_retained=first_n,
        partial_bytes=0,
        partial_age=0.0,
        partial_seg=None,
    )


def lines_until_time(session_dir: Path, *, until_t: float) -> ReadResult:
    """Read lines whose idx timestamp `t <= until_t`. Partial tail excluded
    (head semantics: this is the start of the session, not the active end)."""
    refs = segment_refs(session_dir)
    if not refs:
        return ReadResult(b"", [], 0, 0, 0.0, 0, 0, 0, 0, 0.0, None)

    first_line, _ = _first_and_last_line(refs)

    out = bytearray()
    last_n = 0
    done = False
    for ref in refs:
        if done:
            break
        records = read_idx_records(ref.idx_path)
        if not records:
            continue
        if records[0][1] > until_t:
            break
        stream = stream_segment_bytes(ref.stream_path)
        lines = lines_in_segment(stream, records)
        for rec_idx, (n, t) in enumerate(records):
            if rec_idx >= len(lines) or t > until_t:
                done = True
                break
            out.extend(lines[rec_idx])
            last_n = n

    return ReadResult(
        stdout=bytes(out),
        stderr_lines=[],
        first_line=first_line if (first_line and out) else 0,
        last_line=last_n,
        at_time=at_time_of(session_dir),
        at_byte=len(out),
        dropped=0,
        first_retained=first_line,
        partial_bytes=0,
        partial_age=0.0,
        partial_seg=None,
    )


def tail_last(
    session_dir: Path, *, n_lines: int | None = None, c_bytes: int | None = None
) -> ReadResult:
    """Tail the last N lines or last K bytes of stream content (excluding partial tail)."""
    result = cat_all(session_dir)
    # Drop the partial tail for line/byte trimming; re-append below.
    body = result.stdout
    if result.partial_bytes:
        body = body[: -result.partial_bytes]
    partial = result.stdout[len(body) :] if result.partial_bytes else b""

    if c_bytes is not None:
        body = body[-c_bytes:] if c_bytes < len(body) else body
    else:
        keep = 10 if n_lines is None else n_lines
        if keep <= 0:
            body = b""
        else:
            # Walk backward to find the start of the Nth-from-last line.
            count = 0
            i = len(body)
            while i > 0 and count < keep:
                j = body.rfind(b"\n", 0, i - 1) if i >= 1 else -1
                count += 1
                if j < 0:
                    i = 0
                    break
                i = j + 1
            body = body[i:]

    new_stdout = body + partial
    return ReadResult(
        stdout=new_stdout,
        stderr_lines=result.stderr_lines,
        first_line=result.first_line,
        last_line=result.last_line,
        dropped=result.dropped,
        first_retained=result.first_retained,
        at_time=result.at_time,
        at_byte=result.at_byte,
        partial_bytes=result.partial_bytes,
        partial_age=result.partial_age,
        partial_seg=result.partial_seg,
    )
