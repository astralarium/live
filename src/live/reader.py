"""Reader helpers: segment scanning, line ranges, partial-line tail."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

from .format import (
    first_idx_record,
    idx_name,
    last_idx_record,
    list_segments,
    read_idx_records,
    read_segment_start,
    segment_tip_byte,
    stream_name,
)

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


def lines_in_segment(
    stream: bytes, idx_records: list[tuple[int, float, int]]
) -> list[bytes]:
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


def partial_tail_bytes(
    stream: bytes, idx_records: list[tuple[int, float, int]]
) -> bytes:
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
    first_emitted: int  # first n actually emitted (0 if none)
    last_line: (
        int  # highest emitted line number (0 if none); next-line cursor = last_line + 1
    )
    last_time: (
        float  # wall-clock time of last write (active stream mtime); 0.0 if no segment
    )
    next_byte: int  # lifetime byte cursor — agents resume with `tail -c +<next_byte>`
    dropped: int  # k lines dropped (gap)
    first_line: int  # retention floor: first n still on disk (0 if no records)
    partial_bytes: int  # k bytes in partial-line tail
    partial_age: float  # age of partial line in seconds (0.0 if none)
    partial_seg: int | None  # segment number carrying the partial (None if no partial)


def last_time_of(session_dir: Path) -> float:
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


def first_byte_of(session_dir: Path) -> int:
    """Lifetime byte offset of the first currently-retained byte. Read from the
    idx header of the first retained segment; 0 if no segments or header."""
    segs = list_segments(session_dir)
    if not segs:
        return 0
    start = read_segment_start(session_dir / idx_name(segs[0]))
    return start if start is not None else 0


def next_byte_of(session_dir: Path) -> int:
    """Lifetime byte cursor at the session tip (partial-line bytes included).
    `tail -c +K` resumes from lifetime offset K."""
    segs = list_segments(session_dir)
    if not segs:
        return 0
    return segment_tip_byte(
        session_dir / idx_name(segs[-1]),
        session_dir / stream_name(segs[-1]),
    )


def lines_since(
    session_dir: Path,
    *,
    from_line: int,
) -> ReadResult:
    """Read lines with n >= from_line (Unix `tail -n +N` semantics). Includes
    any partial-line tail in stdout."""
    refs = segment_refs(session_dir)
    if not refs:
        return ReadResult(b"", [], 0, 0, 0.0, 0, 0, 0, 0, 0.0, None)

    first_line, last_line = _first_and_last_line(refs)

    stderr_lines: list[str] = []
    dropped = 0
    # Line numbers are 1-indexed; treat from_line<1 as "from the start" with no gap.
    effective_from = max(from_line, 1)
    if first_line and effective_from < first_line:
        dropped = first_line - effective_from
        stderr_lines.append(
            f"dropped {dropped} lines (from-line={from_line}, first-line={first_line})"
        )
        emit_from = first_line
    else:
        emit_from = max(effective_from, first_line) if first_line else 0

    out = bytearray()
    if first_line and emit_from <= last_line:
        for ref in refs:
            records = read_idx_records(ref.idx_path)
            if not records or records[-1][0] < emit_from:
                continue
            stream = stream_segment_bytes(ref.stream_path)
            lines = lines_in_segment(stream, records)
            for rec_idx, (n, _t, _b) in enumerate(records):
                if n < emit_from:
                    continue
                if rec_idx >= len(lines):
                    break
                out.extend(lines[rec_idx])

    partial_bytes = 0
    partial_age = 0.0
    partial_seg: int | None = None
    last_time = 0.0
    if refs:
        last_ref = refs[-1]
        try:
            last_time = os.path.getmtime(last_ref.stream_path)
        except FileNotFoundError:
            last_time = 0.0
        records = read_idx_records(last_ref.idx_path)
        stream = stream_segment_bytes(last_ref.stream_path)
        tail = partial_tail_bytes(stream, records)
        if tail:
            out.extend(tail)
            if last_time > 0.0:
                partial_bytes = len(tail)
                partial_seg = last_ref.seg
                partial_age = max(0.0, time.time() - last_time)

    return ReadResult(
        stdout=bytes(out),
        stderr_lines=stderr_lines,
        first_emitted=emit_from,
        last_line=last_line,
        last_time=last_time,
        next_byte=next_byte_of(session_dir),
        dropped=dropped,
        first_line=first_line,
        partial_bytes=partial_bytes,
        partial_age=partial_age,
        partial_seg=partial_seg,
    )


def cat_all(session_dir: Path) -> ReadResult:
    """Concatenate every stream segment + partial tail."""
    return lines_since(session_dir, from_line=0)


def lines_since_time(session_dir: Path, *, from_time: float) -> ReadResult:
    """Read lines whose idx timestamp `t > from_time`. Includes partial-line tail."""
    refs = segment_refs(session_dir)
    if not refs:
        return ReadResult(b"", [], 0, 0, 0.0, 0, 0, 0, 0, 0.0, None)

    first_line, last_line = _first_and_last_line(refs)

    out = bytearray()
    for ref in refs:
        records = read_idx_records(ref.idx_path)
        if not records or records[-1][1] <= from_time:
            continue
        stream = stream_segment_bytes(ref.stream_path)
        lines = lines_in_segment(stream, records)
        for rec_idx, (_n, t, _b) in enumerate(records):
            if t <= from_time or rec_idx >= len(lines):
                continue
            out.extend(lines[rec_idx])

    partial_bytes = 0
    partial_age = 0.0
    partial_seg: int | None = None
    last_time = 0.0
    last_ref = refs[-1]
    try:
        last_time = os.path.getmtime(last_ref.stream_path)
    except FileNotFoundError:
        last_time = 0.0
    records = read_idx_records(last_ref.idx_path)
    stream = stream_segment_bytes(last_ref.stream_path)
    tail = partial_tail_bytes(stream, records)
    if tail and last_time > from_time:
        partial_bytes = len(tail)
        partial_seg = last_ref.seg
        partial_age = max(0.0, time.time() - last_time)
        out.extend(tail)

    return ReadResult(
        stdout=bytes(out),
        stderr_lines=[],
        first_emitted=0,
        last_line=last_line,
        last_time=last_time,
        next_byte=next_byte_of(session_dir),
        dropped=0,
        first_line=first_line,
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

    first_n = result.first_line or 0

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
    # cat_all spans [first_byte, first_byte + len(result.stdout)); take len(body)
    # from the head, so next_byte = first_byte + len(body).
    first_byte = result.next_byte - len(result.stdout)
    return ReadResult(
        stdout=body,
        stderr_lines=result.stderr_lines,
        first_emitted=first_n if emitted else 0,
        last_line=last_line,
        last_time=result.last_time,
        next_byte=first_byte + len(body),
        dropped=result.dropped,
        first_line=first_n,
        partial_bytes=0,
        partial_age=0.0,
        partial_seg=None,
    )


def bytes_since(session_dir: Path, *, from_byte: int) -> ReadResult:
    """Read bytes after lifetime offset `from_byte` (tail -c +K). May start
    mid-line; partial-line bytes are included.

    If `from_byte` points below the retention floor (retention dropped that
    range), emits a `dropped <K> bytes (from-byte=<B>, first-byte=<F>)` extra
    and starts at the floor.
    """
    refs = segment_refs(session_dir)
    first_byte = first_byte_of(session_dir)
    if not refs:
        return ReadResult(b"", [], 0, 0, 0.0, first_byte, 0, 0, 0, 0.0, None)

    first_line, last_line = _first_and_last_line(refs)

    stderr_lines: list[str] = []
    dropped = 0
    if from_byte < first_byte:
        dropped = first_byte - from_byte
        stderr_lines.append(
            f"dropped {dropped} bytes (from-byte={from_byte}, first-byte={first_byte})"
        )
        effective_from = first_byte
    else:
        effective_from = from_byte

    out = bytearray()
    cumulative = first_byte  # lifetime offset of next segment's start
    for ref in refs:
        try:
            size = os.path.getsize(ref.stream_path)
        except FileNotFoundError:
            size = 0
        if cumulative + size > effective_from:
            offset = max(0, effective_from - cumulative)
            out.extend(stream_segment_bytes(ref.stream_path)[offset:])
        cumulative += size

    partial_bytes = 0
    partial_age = 0.0
    partial_seg: int | None = None
    last_time = 0.0
    last_ref = refs[-1]
    try:
        last_time = os.path.getmtime(last_ref.stream_path)
    except FileNotFoundError:
        last_time = 0.0
    records = read_idx_records(last_ref.idx_path)
    stream = stream_segment_bytes(last_ref.stream_path)
    tail = partial_tail_bytes(stream, records)
    if tail and last_time > 0.0:
        partial_bytes = len(tail)
        partial_seg = last_ref.seg
        partial_age = max(0.0, time.time() - last_time)

    return ReadResult(
        stdout=bytes(out),
        stderr_lines=stderr_lines,
        first_emitted=0,
        last_line=last_line,
        last_time=last_time,
        next_byte=cumulative,
        dropped=dropped,
        first_line=first_line,
        partial_bytes=partial_bytes,
        partial_age=partial_age,
        partial_seg=partial_seg,
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

    first_n = result.first_line or 0

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
    first_byte = result.next_byte - len(result.stdout)
    return ReadResult(
        stdout=body,
        stderr_lines=result.stderr_lines,
        first_emitted=first_n if emitted else 0,
        last_line=last_line,
        last_time=result.last_time,
        next_byte=first_byte + len(body),
        dropped=result.dropped,
        first_line=first_n,
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
    last_byte_end: int | None = None
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
        for rec_idx, (n, t, b) in enumerate(records):
            if rec_idx >= len(lines) or t > until_t:
                done = True
                break
            line = lines[rec_idx]
            out.extend(line)
            last_n = n
            last_byte_end = b + len(line)

    next_byte = (
        last_byte_end if last_byte_end is not None else first_byte_of(session_dir)
    )
    return ReadResult(
        stdout=bytes(out),
        stderr_lines=[],
        first_emitted=first_line if (first_line and out) else 0,
        last_line=last_n,
        last_time=last_time_of(session_dir),
        next_byte=next_byte,
        dropped=0,
        first_line=first_line,
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
        first_emitted=result.first_emitted,
        last_line=result.last_line,
        dropped=result.dropped,
        first_line=result.first_line,
        last_time=result.last_time,
        next_byte=result.next_byte,
        partial_bytes=result.partial_bytes,
        partial_age=result.partial_age,
        partial_seg=result.partial_seg,
    )
