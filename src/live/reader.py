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
    is_since_line: bool,
    stdout_is_tty: bool,
) -> bool:
    """Resolve --strip-ansi/--raw/default-by-TTY rules."""
    if explicit_raw:
        return False
    if explicit_strip:
        return True
    if is_since_line:
        return True
    return not stdout_is_tty


@dataclass(frozen=True)
class SegmentRef:
    seg: int
    stream_path: Path
    idx_path: Path


def segment_refs(session_dir: Path) -> list[SegmentRef]:
    segs = list_segments(session_dir).nums
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
    """Output of a cat/tail invocation, before optional ANSI stripping."""

    stdout: bytes
    # Stderr lines (without trailing newlines), in canonical order.
    stderr_lines: list[str]
    first_line: int  # first n actually emitted (0 if none)
    last_line: int  # cursor for the trailer (lastLine at read completion)
    dropped: int  # k lines dropped (gap)
    first_retained: int  # firstLine of session at read time
    partial_bytes: int  # k bytes in partial-line tail
    partial_age: float  # age of partial line in seconds (0.0 if none)
    partial_seg: int | None  # segment number carrying the partial (None if no partial)


def lines_since(
    session_dir: Path,
    *,
    since: int,
) -> ReadResult:
    """Read lines with n > since. Includes any partial-line tail in stdout."""
    refs = segment_refs(session_dir)
    if not refs:
        return ReadResult(b"", [], 0, 0, 0, 0, 0, 0.0, None)

    # Compute firstLine/lastLine, decide gap.
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

    stderr_lines: list[str] = []
    dropped = 0
    if first_line and since + 1 < first_line:
        dropped = first_line - since - 1
        stderr_lines.append(
            f"dropped {dropped} lines (since={since}, first retained={first_line})"
        )
        # Start emission from firstLine.
        emit_from = first_line
    else:
        emit_from = max(since + 1, first_line) if first_line else 0

    out = bytearray()
    if first_line and emit_from <= last_line:
        for ref in refs:
            records = read_idx_records(ref.idx_path)
            if not records:
                continue
            seg_first = records[0][0]
            seg_last = records[-1][0]
            if seg_last < emit_from:
                continue
            stream = stream_segment_bytes(ref.stream_path)
            lines = lines_in_segment(stream, records)
            for rec_idx, (n, _t) in enumerate(records):
                if n < emit_from:
                    continue
                if rec_idx >= len(lines):
                    break
                out.extend(lines[rec_idx])

    # Partial-line tail in the highest segment.
    partial_bytes = 0
    partial_age = 0.0
    partial_seg: int | None = None
    if refs:
        last_ref = refs[-1]
        records = read_idx_records(last_ref.idx_path)
        stream = stream_segment_bytes(last_ref.stream_path)
        tail = partial_tail_bytes(stream, records)
        if tail:
            partial_bytes = len(tail)
            partial_seg = last_ref.seg
            # Estimate age from active idx mtime (heartbeat keeps it fresh,
            # but on partial-only activity the mtime equals last-completed-line time).
            try:
                partial_age = max(
                    0.0, time.time() - os.path.getmtime(last_ref.idx_path)
                )
            except FileNotFoundError:
                partial_age = 0.0
            out.extend(tail)

    return ReadResult(
        stdout=bytes(out),
        stderr_lines=stderr_lines,
        first_line=emit_from,
        last_line=last_line,
        dropped=dropped,
        first_retained=first_line,
        partial_bytes=partial_bytes,
        partial_age=partial_age,
        partial_seg=partial_seg,
    )


def cat_all(session_dir: Path) -> ReadResult:
    """Concatenate every stream segment + partial tail."""
    return lines_since(session_dir, since=0)


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
                # Find previous \n before i-1; if i-1 is a \n itself, skip past it.
                j = body.rfind(b"\n", 0, i - 1) if i >= 1 else -1
                if body[i - 1 : i] != b"\n":
                    # tail must include trailing line even without newline
                    pass
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
        partial_bytes=result.partial_bytes,
        partial_age=result.partial_age,
        partial_seg=result.partial_seg,
    )
