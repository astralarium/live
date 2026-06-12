"""Reader helpers: stream views, line ranges, partial-line tail.

Lines are located by idx byte offsets, never by re-scanning segments for
newlines: closed segments are exactly `segmentKb` and a line may span any
number of segments. `StreamView` is the single load point; every read verb
is a slice of it.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from .ansi import incomplete_escape_len, strip_ansi
from .format import (
    Watermarks,
    compute_watermarks,
    idx_name,
    idx_record_after,
    idx_record_at,
    first_idx_record,
    last_idx_record,
    list_segments,
    read_idx_records,
    read_segment_line_start,
    read_segment_start,
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


@dataclass(frozen=True)
class StreamView:
    """Snapshot of retained stream bytes plus the matching record list.

    `base` is the lifetime offset of `data[0]`. `records` are `(n, t, b)`
    ascending; line numbers are consecutive (retention only drops whole
    oldest segments). Only `records[0]` can reference bytes below `base` —
    a line whose head was retained away. `last_end` is the lifetime offset
    just past the last indexed line's `\\n`; bytes past it are the partial
    tail. A view loaded with `from_byte` covers only segments overlapping
    `[from_byte, tip)`, so `base`/`records`/`last_end` are relative to that
    window, not the session floor.
    """

    base: int
    data: bytes
    records: list[tuple[int, float, int]]
    last_end: int
    line_start: (
        int  # where the line containing data[0] began (== base at a line boundary)
    )

    @property
    def tip(self) -> int:
        return self.base + len(self.data)

    @property
    def truncated_head(self) -> int:
        """Bytes of the line at the floor that retention dropped."""
        return self.base - self.line_start

    @property
    def partial_len(self) -> int:
        return self.tip - self.last_end

    @property
    def first_line(self) -> int:
        """First fully-retained line number (0 if none)."""
        if not self.records:
            return 0
        if self.records[0][2] >= self.base:
            return self.records[0][0]
        return self.records[1][0] if len(self.records) > 1 else 0

    @property
    def last_line(self) -> int:
        return self.records[-1][0] if self.records else 0

    def index_of(self, n: int) -> int:
        return n - self.records[0][0]

    def start_of(self, n: int) -> int:
        """Lifetime offset of line n's first retained byte."""
        return max(self.records[self.index_of(n)][2], self.base)

    def end_of(self, n: int) -> int:
        """Lifetime offset just past line n's `\\n`."""
        i = self.index_of(n)
        if i + 1 < len(self.records):
            return self.records[i + 1][2]
        return self.last_end

    def slice(self, start: int, end: int) -> bytes:
        return self.data[max(start - self.base, 0) : max(end - self.base, 0)]


def load_stream_view(session_dir: Path, *, from_byte: int | None = None) -> StreamView:
    """Load retained stream bytes + records. With `from_byte`, segments lying
    entirely below it are skipped (their lines are already consumed); `base`
    then exceeds `from_byte` only when retention dropped bytes past it.

    Retention can unlink a listed segment mid-load; anything gathered before
    the hole is discarded so the view stays contiguous."""
    refs = segment_refs(session_dir)
    base: int | None = None
    line_start: int | None = None
    chunks: list[bytes] = []
    length = 0
    records: list[tuple[int, float, int]] = []
    cursor = 0
    seeked = False
    for ref in refs:
        start = read_segment_start(ref.idx_path)
        if start is None:
            start = cursor  # torn idx: assume contiguity with the previous segment
        if from_byte is not None:
            try:
                size = os.path.getsize(ref.stream_path)
            except FileNotFoundError:
                size = 0
            if start + size <= from_byte:
                cursor = start + size
                continue
        try:
            if from_byte is not None and base is None and start < from_byte:
                # First overlapping segment: read only bytes past the cursor,
                # so a follow/pager poll is O(new bytes), not O(segment).
                with ref.stream_path.open("rb") as f:
                    f.seek(from_byte - start)
                    data = f.read()
                start = from_byte
                seeked = True
            else:
                data = ref.stream_path.read_bytes()
        except FileNotFoundError:
            # Vanished under us: earlier accumulation is no longer contiguous.
            base, line_start, length = None, None, 0
            chunks.clear()
            records.clear()
            continue
        if base is not None and start != base + length:
            base, line_start, length = None, None, 0
            chunks.clear()
            records.clear()
        if base is None:
            base = start
            line_start = read_segment_line_start(ref.idx_path)
            if line_start is None or line_start > base:
                line_start = base
        chunks.append(data)
        length += len(data)
        records.extend(read_idx_records(ref.idx_path))
        cursor = start + len(data)

    if base is None:
        base = from_byte or 0
    if seeked:
        # Cursor reads start mid-segment: records for lines wholly below the
        # cursor are already consumed, and head-truncation doesn't apply.
        line_start = base
        records = [r for r in records if r[2] >= base]
    if line_start is None:
        line_start = base
    data = b"".join(chunks)
    tip = base + len(data)
    # A torn mid-load idx read can leave a hole in the record run; keep the
    # newest contiguous run (line numbers are consecutive by construction).
    for i in range(len(records) - 1, 0, -1):
        if records[i][0] != records[i - 1][0] + 1:
            del records[:i]
            break
    # The idx can run ahead of the stream snapshot (record written after the
    # stream read) — drop records whose line isn't fully in the snapshot.
    while records and records[-1][2] >= tip:
        records.pop()
    last_end = base
    while records:
        nl = data.find(b"\n", max(records[-1][2] - base, 0))
        if nl >= 0:
            last_end = base + nl + 1
            break
        records.pop()
    return StreamView(
        base=base, data=data, records=records, last_end=last_end, line_start=line_start
    )


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
    # 1-based lifetime position of the next unread byte; agents resume with
    # `tail -c +<next_byte>` (GNU-style inclusive start).
    next_byte: int
    dropped: int  # k lines dropped (gap)
    first_line: int  # retention floor: first n still fully on disk (0 if no full lines)
    partial_bytes: int  # k bytes in partial-line tail
    partial_age: float  # age of partial line in seconds (0.0 if none)
    emitted_byte: int  # lifetime offset just past the last stream byte in stdout
    # Bytes missing from the head of the first emitted line (0 unless stdout
    # starts at the retention floor mid-line).
    dropped_first_bytes: int = 0
    # When set, stdout arrives by draining this iterator instead of `stdout`
    # (all read verbs stream: memory stays O(chunk), not O(range)). The
    # generator may append to `stderr_lines` and update the byte cursors as
    # it runs — drain fully before emitting stderr or reading cursors.
    stdout_iter: Iterator[bytes] | None = None


def write_stdout(result: ReadResult, strip: bool) -> bool:
    """Write a read result's stdout (buffer or streaming iterator) with ANSI
    rules. Streaming strips chunk-wise, holding back a torn trailing escape
    until the next chunk (the `tail -f` holdback); the final chunk flushes
    it. Returns False when downstream closed the pipe (stream abandoned)."""
    try:
        if result.stdout_iter is not None:
            held = b""
            for chunk in result.stdout_iter:
                chunk = held + chunk
                held = b""
                if strip:
                    h = incomplete_escape_len(chunk)
                    if h:
                        held, chunk = chunk[-h:], chunk[:-h]
                    chunk = strip_ansi(chunk)
                if chunk:
                    sys.stdout.buffer.write(chunk)
            tail = strip_ansi(held) if strip else held
            if tail:
                sys.stdout.buffer.write(tail)
        else:
            out = strip_ansi(result.stdout) if strip else result.stdout
            sys.stdout.buffer.write(out)
        sys.stdout.buffer.flush()
    except BrokenPipeError:
        return False
    return True


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


@dataclass(frozen=True)
class SessionStats:
    """Whole-session line/byte metadata from idx headers and seek-read
    records — no stream-segment loads. Read verbs decide their window from
    these, then load only the stream bytes the window needs; a full load is
    the degenerate case, not the default.

    Computed before the window load, so against a live writer the stats may
    trail the window by a few lines — the same advisory-cursor race the
    full-load path always had."""

    first_line: int  # retention floor (0 if no full lines)
    last_line: int  # highest indexed line (0 if none)
    base: int  # lifetime offset of the first retained byte
    tip: int  # lifetime offset just past the newest byte
    last_end: int  # offset just past the last indexed line's \n (base if none)
    last_time: float  # active stream mtime (0.0 if no segment)

    @property
    def partial_len(self) -> int:
        return self.tip - self.last_end

    @property
    def available(self) -> int:
        return self.last_line - self.first_line + 1 if self.first_line else 0


def load_stats(session_dir: Path) -> SessionStats:
    wm = compute_watermarks(session_dir)
    return SessionStats(
        first_line=wm.first_line,
        last_line=wm.last_line,
        base=wm.first_byte,
        tip=wm.last_byte,
        last_end=_scan_last_end(session_dir, wm),
        last_time=last_time_of(session_dir),
    )


def _scan_last_end(session_dir: Path, wm: Watermarks) -> int:
    """Offset just past the last indexed line's `\\n` (== base if no records).

    Seek-based: reads stream bytes only from that line's start byte to its
    newline — a line's record is written after its `\\n` (prefix invariant),
    so the scan terminates unless the snapshot tore; then fall back to `tip`
    (no partial reported), which a torn snapshot can't distinguish anyway.
    """
    last_b: int | None = None
    segs = list_segments(session_dir)
    for seg in reversed(segs):
        rec = last_idx_record(session_dir / idx_name(seg))
        if rec is not None:
            last_b = rec[2]
            break
    if last_b is None:
        return wm.first_byte
    # The first \n at/past the line's start byte is its terminator; the line
    # may span segments (rotation lands mid-line).
    for seg in segs:
        idx_path = session_dir / idx_name(seg)
        start = read_segment_start(idx_path)
        if start is None:
            continue
        try:
            size = os.path.getsize(session_dir / stream_name(seg))
        except FileNotFoundError:
            continue
        if start + size <= last_b:
            continue
        try:
            with (session_dir / stream_name(seg)).open("rb") as f:
                f.seek(max(last_b - start, 0))
                data = f.read()
        except FileNotFoundError:
            continue
        nl = data.find(b"\n")
        if nl >= 0:
            return max(start, last_b) + nl + 1
    return wm.last_byte


def _record_for_line(session_dir: Path, n: int) -> tuple[int, float, int] | None:
    """Seek-read the (n, t, b) record for line `n`: two record reads per
    segment to locate the covering idx (records within one idx are
    consecutive), then one positioned read. None if not found (below the
    floor, past the tip, or torn) — callers fall back to a wider window."""
    for seg in list_segments(session_dir):
        idx_path = session_dir / idx_name(seg)
        first = first_idx_record(idx_path)
        if first is None:
            continue
        if first[0] > n:
            return None  # line n predates this idx run: not retained
        last = last_idx_record(idx_path)
        if last is None or last[0] < n:
            continue
        rec = idx_record_at(idx_path, n - first[0])
        return rec if rec is not None and rec[0] == n else None
    return None


def _first_record_after_time(
    session_dir: Path, t: float
) -> tuple[int, float, int] | None:
    """First record with timestamp > t (records are time-ordered). One open
    + a few interpolated probes per segment; non-covering segments cost two
    probes (their first/last records bound the answer out)."""
    for seg in list_segments(session_dir):
        rec = idx_record_after(session_dir / idx_name(seg), 1, t)
        if rec is not None:
            return rec
    return None


def _first_record_after_byte(
    session_dir: Path, b: int
) -> tuple[int, float, int] | None:
    """First record whose line starts past offset `b` (records are
    byte-ordered). Same shape as the time finder."""
    for seg in list_segments(session_dir):
        rec = idx_record_after(session_dir / idx_name(seg), 2, b)
        if rec is not None:
            return rec
    return None


def _line_offset(session_dir: Path, n: int) -> int | None:
    """Byte offset where line `n` starts. Seek-read fast path; a torn idx
    race falls back to one full snapshot load (rare) so cursor-exact verbs
    never emit from the wrong position. None when `n` isn't retained."""
    rec = _record_for_line(session_dir, n)
    if rec is not None:
        return rec[2]
    view = load_stream_view(session_dir)
    if view.records and view.records[0][0] <= n <= view.last_line:
        return view.start_of(n)
    return None


def _last_line_within(session_dir: Path, stats: SessionStats, end: int) -> int:
    """Highest line wholly inside [floor, end) — its `\\n` at or before `end`.

    Line L's terminator sits at the next record's start byte, so the first
    record past `end` bounds the answer: lines `..m-2` end at or before it.
    """
    if not stats.first_line:
        return 0
    if end >= stats.last_end:
        return stats.last_line
    m = _first_record_after_byte(session_dir, end)
    if m is None:
        last = stats.last_line if stats.last_end <= end else stats.last_line - 1
    else:
        last = m[0] - 2
    return last if last >= stats.first_line else 0


def _partial_fields(
    last_end: int, tip: int, last_time: float, start: int, end: int
) -> tuple[int, float]:
    """(partial_bytes, partial_age) for the unterminated bytes inside the
    emitted [start, end) window; zeros when none were emitted."""
    k = min(end, tip) - max(start, last_end)
    if k > 0 and last_time > 0.0:
        return k, max(0.0, time.time() - last_time)
    return 0, 0.0


def _gap_notice(
    j: int, effective_from: int, first_line: int, k: int, line_start: int, base: int
) -> list[str]:
    """`dropped` notice lines for a request missing `j` whole lines below the
    floor and/or `k` head bytes of its first emitted line. Keys pair up per
    clause: lines span [from-line, first-line), bytes span [from-byte,
    first-byte). Byte positions are 1-based, matching `tail -c +K`."""
    if j and k:
        return [
            f"dropped {j} lines + {k} bytes"
            f" (from-line={effective_from}, first-line={first_line},"
            f" from-byte={line_start + 1}, first-byte={base + 1})"
        ]
    if j:
        return [
            f"dropped {j} lines (from-line={effective_from}, first-line={first_line})"
        ]
    if k:
        return [
            f"dropped {k} bytes (from-byte={line_start + 1}, first-byte={base + 1})"
        ]
    return []


def _floor_notice(
    session_dir: Path, stats: SessionStats, requested_from: int, start: int
) -> tuple[int, int, list[str]]:
    """One gap notice for what retention dropped out of the REQUESTED range:
    whole lines below the floor, plus the head of the first emitted line when
    stdout starts at the floor (its original start comes from the first idx
    header). A request fully satisfied from retained data gets no notice.
    Returns (dropped_lines, dropped_first_bytes, stderr_lines)."""
    effective_from = max(requested_from, 1)
    first_line = stats.first_line
    j = first_line - effective_from if first_line and effective_from < first_line else 0
    k = 0
    line_start = stats.base
    if start <= stats.base and stats.tip > stats.base:
        segs = list_segments(session_dir)
        if segs:
            ls = read_segment_line_start(session_dir / idx_name(segs[0]))
            if ls is not None and ls < stats.base:
                line_start = ls
                k = stats.base - ls
    return j, k, _gap_notice(j, effective_from, first_line, k, line_start, stats.base)


def lines_since(
    session_dir: Path,
    *,
    from_line: int,
) -> ReadResult:
    """Read lines with n >= from_line (Unix `tail -n +N` semantics). The
    unterminated tail counts as line `last_line + 1`: it is emitted only when
    the cursor covers it, so a cursor past the stream emits nothing. Emitting
    from the floor includes the retained suffix of a head-truncated line; the
    `dropped` notice covers it.

    Streams: no stream bytes are buffered, and a caught-up or cursor-ahead
    poll reads no closed segments at all.
    """
    stats = load_stats(session_dir)

    # Line numbers are 1-indexed; treat from_line<1 as "from the start" with no gap.
    effective_from = max(from_line, 1)

    include_partial = stats.partial_len > 0 and effective_from <= stats.last_line + 1
    if stats.last_line and effective_from <= stats.last_line:
        if effective_from <= stats.first_line:
            start = stats.base  # from the floor (head-truncated suffix included)
        else:
            offset = _line_offset(session_dir, effective_from)
            start = offset if offset is not None else stats.base
    elif include_partial:
        start = stats.last_end  # caught up at the open line
    else:
        # Cursor ahead of the stream (or nothing at all): no bytes to read.
        return ReadResult(
            stdout=b"",
            stderr_lines=[],
            first_emitted=0,
            last_line=stats.last_line,
            last_time=stats.last_time,
            next_byte=stats.tip + 1,
            dropped=0,
            first_line=stats.first_line,
            partial_bytes=0,
            partial_age=0.0,
            emitted_byte=stats.tip,
        )

    end = None if include_partial else stats.last_end
    dropped, head_dropped, stderr_lines = _floor_notice(
        session_dir, stats, effective_from, start
    )

    emit_from = max(effective_from, stats.first_line) if stats.first_line else 0
    upto = stats.tip if end is None else end
    partial_bytes, partial_age = _partial_fields(
        stats.last_end, stats.tip, stats.last_time, start, upto
    )

    result = ReadResult(
        stdout=b"",
        stderr_lines=stderr_lines,
        first_emitted=emit_from,
        last_line=stats.last_line,
        last_time=stats.last_time,
        next_byte=upto + 1,
        dropped=dropped,
        first_line=stats.first_line,
        partial_bytes=partial_bytes,
        partial_age=partial_age,
        emitted_byte=upto,
        dropped_first_bytes=head_dropped,
    )
    result.stdout_iter = _stream_range(session_dir, result, start=start, end=end)
    return result


_STREAM_CHUNK = 256 * 1024


def cat_all(session_dir: Path) -> ReadResult:
    """Concatenate every retained byte + partial tail (streamed)."""
    return lines_since(session_dir, from_line=0)


def _stream_range(
    session_dir: Path, result: ReadResult, *, start: int = 0, end: int | None = None
) -> Iterator[bytes]:
    """Yield stream bytes for the lifetime range [start, end) in chunks,
    segment by segment; `end=None` streams to EOF (the tip at read time).

    Lock-free against the writer, but yielded bytes cannot be revoked: a
    range that races retention becomes a `dropped` notice appended to
    `result.stderr_lines` instead of the snapshot loader's contiguity reset.
    Once anything is emitted, the result's byte cursors are updated to what
    was actually emitted."""
    pos = start
    emitted = False
    for seg in list_segments(session_dir):
        if end is not None and pos >= end:
            break
        stream_path = session_dir / stream_name(seg)
        seg_start = read_segment_start(session_dir / idx_name(seg))
        if seg_start is None:
            seg_start = pos  # torn idx: assume contiguity
        if end is not None and seg_start >= end:
            break
        try:
            size = os.path.getsize(stream_path)
        except FileNotFoundError:
            continue
        if seg_start + size <= pos:
            continue
        if seg_start > pos:
            result.stderr_lines.append(
                f"dropped {seg_start - pos} bytes"
                f" (from-byte={pos + 1}, first-byte={seg_start + 1})"
            )
            pos = seg_start
        try:
            with stream_path.open("rb") as f:
                if pos > seg_start:
                    f.seek(pos - seg_start)
                while True:
                    budget = _STREAM_CHUNK
                    if end is not None:
                        budget = min(budget, end - pos)
                        if budget <= 0:
                            break
                    chunk = f.read(budget)
                    if not chunk:
                        break
                    pos += len(chunk)
                    emitted = True
                    yield chunk
        except FileNotFoundError:
            continue  # vanished mid-walk; the next segment gap-notices it
    if emitted:
        result.emitted_byte = pos
        result.next_byte = pos + 1


def lines_since_time(session_dir: Path, *, from_time: float) -> ReadResult:
    """Read lines whose idx timestamp `t > from_time`. Includes the partial
    tail only when it is newer than `from_time`. Loads stream bytes only
    from the first qualifying line."""
    stats = load_stats(session_dir)

    include_partial = stats.partial_len > 0 and stats.last_time > from_time
    rec = _first_record_after_time(session_dir, from_time)
    if rec is None and not include_partial:
        return ReadResult(
            stdout=b"",
            stderr_lines=[],
            first_emitted=0,
            last_line=stats.last_line,
            last_time=stats.last_time,
            next_byte=stats.tip + 1,
            dropped=0,
            first_line=stats.first_line,
            partial_bytes=0,
            partial_age=0.0,
            emitted_byte=stats.tip,
        )

    start = rec[2] if rec is not None else stats.last_end
    # Time cursors carry no line gap; only the head-drop clause can apply
    # (the first qualifying line's head was retained away).
    head_dropped = 0
    trunc_lines: list[str] = []
    if start < stats.base:
        head_dropped = stats.base - start
        trunc_lines = _gap_notice(
            0, 1, stats.first_line, head_dropped, start, stats.base
        )
        start = stats.base
    end = None if include_partial else stats.last_end

    upto = stats.tip if end is None else end
    partial_bytes, partial_age = _partial_fields(
        stats.last_end, stats.tip, stats.last_time, start, upto
    )
    result = ReadResult(
        stdout=b"",
        stderr_lines=trunc_lines,
        first_emitted=0,
        last_line=stats.last_line,
        last_time=stats.last_time,
        next_byte=upto + 1,
        dropped=0,
        first_line=stats.first_line,
        partial_bytes=partial_bytes,
        partial_age=partial_age,
        emitted_byte=upto,
        dropped_first_bytes=head_dropped,
    )
    result.stdout_iter = _stream_range(session_dir, result, start=start, end=end)
    return result


def lines_until_time(session_dir: Path, *, until_t: float) -> ReadResult:
    """Read full lines whose idx timestamp `t <= until_t`. The unterminated
    tail is included only when the whole stream is older than `until_t`
    (its newest write, `last_time`, is the open line's effective timestamp).
    Head-truncated fragments are excluded (head semantics). Loads stream
    bytes only up to the last qualifying line."""
    stats = load_stats(session_dir)

    # The first record past the cut bounds the range: lines through n-1
    # qualify, and its start byte is exactly the end of the emitted span.
    rec_after = _first_record_after_time(session_dir, until_t)
    last_n = rec_after[0] - 1 if rec_after is not None else stats.last_line
    if not stats.first_line or last_n < stats.first_line:
        last_n = 0

    include_partial = (
        stats.partial_len > 0
        and 0.0 < stats.last_time <= until_t
        and last_n == stats.last_line
    )
    if last_n:
        offset = _line_offset(session_dir, stats.first_line)
        start = offset if offset is not None else stats.base
        if include_partial:
            end = stats.tip
        elif rec_after is not None:
            end = rec_after[2]
        else:
            end = stats.last_end
    elif include_partial:  # no complete lines retained; just the open line
        start, end = stats.last_end, stats.tip
    else:
        return ReadResult(
            stdout=b"",
            stderr_lines=[],
            first_emitted=0,
            last_line=0,
            last_time=stats.last_time,
            next_byte=stats.base + 1,
            dropped=0,
            first_line=stats.first_line,
            partial_bytes=0,
            partial_age=0.0,
            emitted_byte=stats.base,
        )

    partial_bytes, partial_age = _partial_fields(
        stats.last_end, stats.tip, stats.last_time, start, end
    )
    result = ReadResult(
        stdout=b"",
        stderr_lines=[],
        first_emitted=stats.first_line if end > start else 0,
        last_line=last_n,
        last_time=stats.last_time,
        next_byte=end + 1,
        dropped=0,
        first_line=stats.first_line,
        partial_bytes=partial_bytes,
        partial_age=partial_age,
        emitted_byte=end,
    )
    result.stdout_iter = _stream_range(session_dir, result, start=start, end=end)
    return result


def head_first(
    session_dir: Path, *, n_lines: int | None = None, c_bytes: int | None = None
) -> ReadResult:
    """Head the first N lines or first K bytes (GNU semantics).

    `-n` counts the unterminated tail as a line: it is emitted only when N
    exceeds the retained full lines. `-c` counts bytes from the retention
    floor to the true stream end. Default N=10 to match Unix `head`.
    `last_line` is the last fully-emitted line; `tail -vn +<L+1>` resumes
    from there.
    """
    return _head(session_dir, n_lines=n_lines, c_bytes=c_bytes, drop_last=False)


def head_drop_last(
    session_dir: Path, *, n_lines: int | None = None, c_bytes: int | None = None
) -> ReadResult:
    """GNU `head -n -K` / `-c -K`: emit everything except the last K lines
    (the unterminated tail counts as the newest line) or the last K bytes
    (counted from the true stream end)."""
    return _head(session_dir, n_lines=n_lines, c_bytes=c_bytes, drop_last=True)


def _head(
    session_dir: Path,
    *,
    n_lines: int | None,
    c_bytes: int | None,
    drop_last: bool,
) -> ReadResult:
    """Emit lines (or bytes) from the floor; `drop_last` flips the end
    of the range from "first K" to "all but the last K". Streams, reading
    only up to the end of the requested range."""
    stats = load_stats(session_dir)
    first_line = stats.first_line
    available = stats.available
    pad = 1 if stats.partial_len > 0 else 0  # the open line occupies a slot

    if c_bytes is not None:
        k = max(c_bytes, 0)
        start = stats.base
        if drop_last:
            end = max(stats.tip - k, stats.base)
        else:
            end = min(stats.base + k, stats.tip)
        last_line = _last_line_within(session_dir, stats, end)
    else:
        if drop_last:
            emitted = max(available + pad - (n_lines or 0), 0)
        else:
            keep = 10 if n_lines is None else n_lines
            emitted = min(max(keep, 0), available + pad)
        include_partial = emitted > available  # range covers the open line
        complete = min(emitted, available)
        if complete:
            offset = _line_offset(session_dir, first_line)
            start = offset if offset is not None else stats.base
        elif include_partial:  # no full lines retained; just the open line
            start = stats.last_end
        else:
            start = _line_offset(session_dir, first_line) if first_line else None
            start = start if start is not None else stats.base
        if include_partial:
            end = stats.tip
        elif complete and complete < available:
            offset = _line_offset(session_dir, first_line + complete)
            end = offset if offset is not None else stats.last_end
        elif complete:  # complete == available: every full line
            end = stats.last_end
        else:
            end = start  # nothing requested
        last_line = first_line + complete - 1 if (first_line and complete) else 0
    dropped, head_dropped, stderr_lines = _floor_notice(session_dir, stats, 1, start)

    partial_bytes, partial_age = _partial_fields(
        stats.last_end, stats.tip, stats.last_time, start, end
    )
    result = ReadResult(
        stdout=b"",
        stderr_lines=stderr_lines,
        first_emitted=first_line if last_line else 0,
        last_line=last_line,
        last_time=stats.last_time,
        next_byte=end + 1,
        dropped=dropped,
        first_line=first_line,
        partial_bytes=partial_bytes,
        partial_age=partial_age,
        emitted_byte=end,
        dropped_first_bytes=head_dropped,
    )
    result.stdout_iter = _stream_range(session_dir, result, start=start, end=end)
    return result


def tail_last(
    session_dir: Path, *, n_lines: int | None = None, c_bytes: int | None = None
) -> ReadResult:
    """Tail the last N lines or last K bytes (GNU semantics).

    `-c K` is exactly the last K bytes of the stream, counted from the true
    end (unterminated tail included). `-n N` counts the unterminated tail as
    the newest line; N exceeding the retained lines emits everything,
    head-truncated fragment included. Streams, reading only from the start
    of the requested range."""
    stats = load_stats(session_dir)
    first_line = stats.first_line
    available = stats.available

    dropped = 0
    head_dropped = 0
    stderr_lines: list[str] = []
    if c_bytes is not None:
        k = max(c_bytes, 0)
        start = max(stats.tip - k, stats.base)
        if stats.tip - k < stats.base and k <= stats.tip:
            # The requested window dips below the retention floor.
            head_dropped = stats.base - (stats.tip - k)
            stderr_lines.append(
                f"dropped {head_dropped} bytes"
                f" (from-byte={stats.tip - k + 1}, first-byte={stats.base + 1})"
            )
    else:
        keep = 10 if n_lines is None else n_lines
        # The unterminated tail occupies the newest slot.
        complete = max(keep - 1, 0) if stats.partial_len > 0 else keep
        req_first = stats.last_line - complete + 1
        if keep <= 0:
            start = stats.tip
        elif complete > available:
            start = stats.base
        elif complete <= 0:
            start = stats.last_end
        else:
            offset = _line_offset(session_dir, req_first)
            start = offset if offset is not None else stats.base
        dropped, head_dropped, stderr_lines = _floor_notice(
            session_dir, stats, req_first, start
        )

    emit_from = max(first_line, 1) if first_line else 0
    partial_bytes, partial_age = _partial_fields(
        stats.last_end, stats.tip, stats.last_time, start, stats.tip
    )
    result = ReadResult(
        stdout=b"",
        stderr_lines=stderr_lines,
        first_emitted=emit_from,
        last_line=stats.last_line,
        last_time=stats.last_time,
        next_byte=stats.tip + 1,
        dropped=dropped,
        first_line=first_line,
        partial_bytes=partial_bytes,
        partial_age=partial_age,
        emitted_byte=stats.tip,
        dropped_first_bytes=head_dropped,
    )
    result.stdout_iter = _stream_range(session_dir, result, start=start)
    return result


def bytes_since(session_dir: Path, *, from_byte: int) -> ReadResult:
    """Read bytes from 1-based lifetime position `from_byte` (GNU `tail -c
    +K`; `+1` and `+0` both mean everything). May start mid-line;
    partial-line bytes are included.

    If the position lies below the retention floor (retention dropped that
    range), emits a `dropped <K> bytes (from-byte=<B>, first-byte=<F>)` extra
    and starts at the floor.
    """
    offset = max(from_byte - 1, 0)  # 1-based position -> lifetime offset
    # Whole-session line metadata for the trailer comes from stats; only the
    # bytes past the cursor are read — a caught-up poll reads no segments.
    stats = load_stats(session_dir)

    stderr_lines: list[str] = []
    dropped = 0
    start = offset
    if offset < stats.base:
        dropped = stats.base - offset
        stderr_lines.append(
            f"dropped {dropped} bytes"
            f" (from-byte={offset + 1}, first-byte={stats.base + 1})"
        )
        start = stats.base

    partial_bytes, partial_age = _partial_fields(
        stats.last_end, stats.tip, stats.last_time, start, stats.tip
    )
    result = ReadResult(
        stdout=b"",
        stderr_lines=stderr_lines,
        first_emitted=0,
        last_line=stats.last_line,
        last_time=stats.last_time,
        next_byte=stats.tip + 1,
        dropped=dropped,
        first_line=stats.first_line,
        partial_bytes=partial_bytes,
        partial_age=partial_age,
        emitted_byte=stats.tip,
    )
    result.stdout_iter = _stream_range(session_dir, result, start=start)
    return result
