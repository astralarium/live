"""Reader helpers: stream views, line ranges, partial-line tail.

Lines are located by idx byte offsets, never by re-scanning segments for
newlines: closed segments are exactly `segmentKb` and a line may span any
number of segments. `StreamView` is the single load point; every read verb
is a slice of it.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

from .format import (
    idx_name,
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


def first_byte_of(session_dir: Path) -> int:
    """Lifetime byte offset of the first currently-retained byte. Read from the
    idx header of the first retained segment; 0 if no segments or header."""
    segs = list_segments(session_dir)
    if not segs:
        return 0
    start = read_segment_start(session_dir / idx_name(segs[0]))
    return start if start is not None else 0


def _partial_fields(
    view: StreamView, last_time: float, start: int, end: int
) -> tuple[int, float]:
    """(partial_bytes, partial_age) for the unterminated bytes inside the
    emitted [start, end) window; zeros when none were emitted."""
    k = min(end, view.tip) - max(start, view.last_end)
    if k > 0 and last_time > 0.0:
        return k, max(0.0, time.time() - last_time)
    return 0, 0.0


def _floor_check(
    view: StreamView, requested_from: int, start: int, out: bytes
) -> tuple[int, int, list[str]]:
    """One gap notice for what retention dropped out of the REQUESTED range:
    whole lines below the floor, plus the head of the first emitted line when
    stdout starts at the floor. A request fully satisfied from retained data
    gets no notice. Returns (dropped_lines, dropped_first_bytes, stderr_lines).
    Byte positions in the notice are 1-based, matching `tail -c +K`."""
    effective_from = max(requested_from, 1)
    first_line = view.first_line
    j = first_line - effective_from if first_line and effective_from < first_line else 0
    k = view.truncated_head if out and start == view.base else 0
    # Keys pair up per clause: dropped lines span [from-line, first-line),
    # dropped bytes span [from-byte, first-byte) — the head of the first
    # emitted line, matching the byte-cursor notice's shape.
    if j and k:
        msg = (
            f"dropped {j} lines + {k} bytes"
            f" (from-line={effective_from}, first-line={first_line},"
            f" from-byte={view.line_start + 1}, first-byte={view.base + 1})"
        )
    elif j:
        msg = f"dropped {j} lines (from-line={effective_from}, first-line={first_line})"
    elif k:
        msg = (
            f"dropped {k} bytes"
            f" (from-byte={view.line_start + 1}, first-byte={view.base + 1})"
        )
    else:
        return 0, 0, []
    return j, k, [msg]


def lines_since(
    session_dir: Path,
    *,
    from_line: int,
) -> ReadResult:
    """Read lines with n >= from_line (Unix `tail -n +N` semantics). The
    unterminated tail counts as line `last_line + 1`: it is emitted only when
    the cursor covers it, so a cursor past the stream emits nothing. Emitting
    from the floor includes the retained suffix of a head-truncated line; the
    `dropped` notice covers it."""
    view = load_stream_view(session_dir)
    last_time = last_time_of(session_dir)

    # Line numbers are 1-indexed; treat from_line<1 as "from the start" with no gap.
    effective_from = max(from_line, 1)

    include_partial = view.partial_len > 0 and effective_from <= view.last_line + 1
    if view.records and effective_from <= view.last_line:
        start = view.start_of(max(effective_from, view.records[0][0]))
        end = view.tip if include_partial else view.last_end
    elif include_partial:
        start, end = view.last_end, view.tip  # caught up at the open line
    else:
        start = end = view.tip  # cursor ahead of the stream
    out = view.slice(start, end)
    dropped, head_dropped, stderr_lines = _floor_check(view, effective_from, start, out)

    first_line = view.first_line
    emit_from = max(effective_from, first_line) if first_line else 0
    partial_bytes, partial_age = _partial_fields(view, last_time, start, end)

    return ReadResult(
        stdout=out,
        stderr_lines=stderr_lines,
        first_emitted=emit_from,
        last_line=view.last_line,
        last_time=last_time,
        next_byte=view.tip + 1,
        dropped=dropped,
        first_line=first_line,
        partial_bytes=partial_bytes,
        partial_age=partial_age,
        emitted_byte=end,
        dropped_first_bytes=head_dropped,
    )


def cat_all(session_dir: Path) -> ReadResult:
    """Concatenate every retained byte + partial tail."""
    return lines_since(session_dir, from_line=0)


def lines_since_time(session_dir: Path, *, from_time: float) -> ReadResult:
    """Read lines whose idx timestamp `t > from_time`. Includes the partial
    tail only when it is newer than `from_time`."""
    view = load_stream_view(session_dir)
    last_time = last_time_of(session_dir)

    start_idx = next(
        (i for i, (_n, t, _b) in enumerate(view.records) if t > from_time), None
    )
    include_partial = view.partial_len > 0 and last_time > from_time
    end = view.tip if include_partial else view.last_end
    if start_idx is not None:
        start = max(view.records[start_idx][2], view.base)
    else:
        start = view.last_end
    out = view.slice(start, end)
    # Time cursors carry no line gap; only the head-drop clause can apply.
    _, head_dropped, trunc_lines = _floor_check(view, view.first_line, start, out)

    partial_bytes, partial_age = _partial_fields(view, last_time, start, end)
    return ReadResult(
        stdout=out,
        stderr_lines=trunc_lines,
        first_emitted=0,
        last_line=view.last_line,
        last_time=last_time,
        next_byte=view.tip + 1,
        dropped=0,
        first_line=view.first_line,
        partial_bytes=partial_bytes,
        partial_age=partial_age,
        emitted_byte=end,
        dropped_first_bytes=head_dropped,
    )


def lines_until_time(session_dir: Path, *, until_t: float) -> ReadResult:
    """Read full lines whose idx timestamp `t <= until_t`. The unterminated
    tail is included only when the whole stream is older than `until_t`
    (its newest write, `last_time`, is the open line's effective timestamp).
    Head-truncated fragments are excluded (head semantics)."""
    view = load_stream_view(session_dir)
    last_time = last_time_of(session_dir)
    first_line = view.first_line

    last_n = 0
    if first_line:
        i = view.index_of(first_line)
        while i < len(view.records) and view.records[i][1] <= until_t:
            last_n = view.records[i][0]
            i += 1

    include_partial = (
        view.partial_len > 0 and 0.0 < last_time <= until_t and last_n == view.last_line
    )
    if last_n:
        start = view.start_of(first_line)
        end = view.tip if include_partial else view.end_of(last_n)
    elif include_partial:  # no complete lines retained; just the open line
        start, end = view.last_end, view.tip
    else:
        start = end = view.base
    out = view.slice(start, end)

    partial_bytes, partial_age = _partial_fields(view, last_time, start, end)
    return ReadResult(
        stdout=out,
        stderr_lines=[],
        first_emitted=first_line if out else 0,
        last_line=last_n,
        last_time=last_time,
        next_byte=end + 1,
        dropped=0,
        first_line=first_line,
        partial_bytes=partial_bytes,
        partial_age=partial_age,
        emitted_byte=end,
    )


def _full_line_span(view: StreamView, first: int, count: int) -> tuple[int, int]:
    """Lifetime [start, end) covering `count` full lines from line `first`."""
    if count <= 0:
        start = view.start_of(first)
        return start, start
    return view.start_of(first), view.end_of(first + count - 1)


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
    of the range from "first K" to "all but the last K"."""
    view = load_stream_view(session_dir)
    last_time = last_time_of(session_dir)
    first_line = view.first_line
    available = view.last_line - first_line + 1 if first_line else 0
    pad = 1 if view.partial_len > 0 else 0  # the open line occupies a slot

    if c_bytes is not None:
        k = max(c_bytes, 0)
        if drop_last:
            end = max(view.tip - k, view.base)
        else:
            end = min(view.base + k, view.tip)
        start = view.base
        body = view.slice(start, end)
        # Last full line wholly inside the body.
        last_line = 0
        if first_line:
            n = first_line
            while n <= view.last_line and view.end_of(n) <= end:
                last_line = n
                n += 1
    else:
        if drop_last:
            emitted = max(available + pad - (n_lines or 0), 0)
        else:
            keep = 10 if n_lines is None else n_lines
            emitted = min(max(keep, 0), available + pad)
        include_partial = emitted > available  # range covers the open line
        complete = min(emitted, available)
        start, end = (
            _full_line_span(view, first_line, complete)
            if first_line
            else (view.base, view.base)
        )
        if include_partial:
            end = view.tip
        body = view.slice(start, end)
        last_line = first_line + complete - 1 if (first_line and complete) else 0
    dropped, head_dropped, stderr_lines = _floor_check(view, 1, start, body)

    partial_bytes, partial_age = _partial_fields(view, last_time, start, end)
    return ReadResult(
        stdout=body,
        stderr_lines=stderr_lines,
        first_emitted=first_line if last_line else 0,
        last_line=last_line,
        last_time=last_time,
        next_byte=end + 1,
        dropped=dropped,
        first_line=first_line,
        partial_bytes=partial_bytes,
        partial_age=partial_age,
        emitted_byte=end,
        dropped_first_bytes=head_dropped,
    )


def tail_last(
    session_dir: Path, *, n_lines: int | None = None, c_bytes: int | None = None
) -> ReadResult:
    """Tail the last N lines or last K bytes (GNU semantics).

    `-c K` is exactly the last K bytes of the stream, counted from the true
    end (unterminated tail included). `-n N` counts the unterminated tail as
    the newest line; N exceeding the retained lines emits everything,
    head-truncated fragment included."""
    view = load_stream_view(session_dir)
    last_time = last_time_of(session_dir)
    first_line = view.first_line
    available = view.last_line - first_line + 1 if first_line else 0

    dropped = 0
    head_dropped = 0
    stderr_lines: list[str] = []
    req_first = 0
    if c_bytes is not None:
        k = max(c_bytes, 0)
        start = max(view.tip - k, view.base)
        if view.tip - k < view.base and k <= view.tip:
            # The requested window dips below the retention floor.
            head_dropped = view.base - (view.tip - k)
            stderr_lines.append(
                f"dropped {head_dropped} bytes"
                f" (from-byte={view.tip - k + 1}, first-byte={view.base + 1})"
            )
    else:
        keep = 10 if n_lines is None else n_lines
        # The unterminated tail occupies the newest slot.
        complete = max(keep - 1, 0) if view.partial_len > 0 else keep
        req_first = view.last_line - complete + 1
        if keep <= 0:
            start = view.tip
        elif complete > available:
            start = view.base
        elif complete <= 0:
            start = view.last_end
        else:
            start = view.start_of(req_first)
    out = view.slice(start, view.tip)
    if c_bytes is None:
        dropped, head_dropped, stderr_lines = _floor_check(view, req_first, start, out)

    emit_from = max(first_line, 1) if first_line else 0
    partial_bytes, partial_age = _partial_fields(view, last_time, start, view.tip)
    return ReadResult(
        stdout=out,
        stderr_lines=stderr_lines,
        first_emitted=emit_from,
        last_line=view.last_line,
        last_time=last_time,
        next_byte=view.tip + 1,
        dropped=dropped,
        first_line=first_line,
        partial_bytes=partial_bytes,
        partial_age=partial_age,
        emitted_byte=view.tip,
        dropped_first_bytes=head_dropped,
    )


def bytes_since(session_dir: Path, *, from_byte: int) -> ReadResult:
    """Read bytes from 1-based lifetime position `from_byte` (GNU `tail -c
    +K`; `+1` and `+0` both mean everything). May start mid-line;
    partial-line bytes are included.

    If the position lies below the retention floor (retention dropped that
    range), emits a `dropped <K> bytes (from-byte=<B>, first-byte=<F>)` extra
    and starts at the floor.
    """
    offset = max(from_byte - 1, 0)  # 1-based position -> lifetime offset
    # Full load (no from_byte): the trailer's line metadata must reflect the
    # whole session, not just records past the cursor.
    view = load_stream_view(session_dir)
    last_time = last_time_of(session_dir)

    stderr_lines: list[str] = []
    dropped = 0
    if offset < view.base:
        dropped = view.base - offset
        stderr_lines.append(
            f"dropped {dropped} bytes"
            f" (from-byte={offset + 1}, first-byte={view.base + 1})"
        )
    start = max(offset, view.base)
    out = view.slice(start, view.tip)

    partial_bytes, partial_age = _partial_fields(view, last_time, start, view.tip)
    return ReadResult(
        stdout=out,
        stderr_lines=stderr_lines,
        first_emitted=0,
        last_line=view.last_line,
        last_time=last_time,
        next_byte=view.tip + 1,
        dropped=dropped,
        first_line=view.first_line,
        partial_bytes=partial_bytes,
        partial_age=partial_age,
        emitted_byte=view.tip,
    )
