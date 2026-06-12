"""Windowed reads: verbs load only the segments their range needs.

Builds multi-segment sessions directly on disk and counts which stream files
get opened — the regression guard for the read-scaling work. `load_stats`
legitimately opens the LAST segment (the last-line newline scan), so "reads
nothing" assertions allow the final segment and nothing else.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from live.format import IDX_RECORD, idx_name, pack_idx_header, stream_name
from live.reader import (
    bytes_since,
    cat_all,
    head_first,
    lines_since,
    lines_until_time,
    load_stats,
    tail_last,
)

SEGS = 8
LINES_PER = 50
LINE_LEN = 100


def _build_session(d: Path, *, partial: bytes = b"") -> dict:
    """SEGS segments x LINES_PER lines of LINE_LEN bytes; line n has idx
    timestamp 1000+n. `partial` appends unindexed bytes to the last segment."""
    d.mkdir(parents=True, exist_ok=True)
    n = 0
    offset = 0
    data = b""
    for s in range(SEGS):
        body = b""
        recs = b""
        seg_start = offset
        for _ in range(LINES_PER):
            n += 1
            line = f"line-{n:06d}-".encode().ljust(LINE_LEN - 1, b"x") + b"\n"
            recs += IDX_RECORD.pack(n, 1000.0 + n, offset)
            body += line
            offset += len(line)
        if s == SEGS - 1 and partial:
            body += partial
            offset += len(partial)
        (d / stream_name(s)).write_bytes(body)
        (d / idx_name(s)).write_bytes(pack_idx_header(seg_start, seg_start) + recs)
        data += body
    return {"lines": n, "tip": offset, "data": data}


LAST_SEG = stream_name(SEGS - 1)


@pytest.fixture
def opened(monkeypatch) -> list[str]:
    """Record the file names passed through Path.open (covers read_bytes too)."""
    names: list[str] = []
    orig = Path.open

    def counting(self, *args, **kwargs):
        names.append(self.name)
        return orig(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counting)
    return names


def _streams(names: list[str]) -> set[str]:
    return {x for x in names if x.startswith("stream.")}


def _drain(res) -> bytes:
    """Materialize a (possibly streaming) result's stdout. Cursor fields are
    only final after the drain."""
    if res.stdout_iter is not None:
        return res.stdout + b"".join(res.stdout_iter)
    return res.stdout


def test_stats_match_session(tmp_path: Path) -> None:
    info = _build_session(tmp_path, partial=b"OPEN")
    stats = load_stats(tmp_path)
    assert stats.first_line == 1
    assert stats.last_line == info["lines"]
    assert stats.base == 0
    assert stats.tip == info["tip"]
    assert stats.last_end == info["tip"] - 4
    assert stats.partial_len == 4


def test_tail_n_reads_only_last_segment(tmp_path: Path, opened) -> None:
    info = _build_session(tmp_path)
    opened.clear()
    res = tail_last(tmp_path, n_lines=1)
    assert _drain(res) == info["data"][-LINE_LEN:]
    assert res.next_byte == info["tip"] + 1
    assert _streams(opened) <= {LAST_SEG}


def test_tail_n_partial_occupies_slot_windowed(tmp_path: Path, opened) -> None:
    _build_session(tmp_path, partial=b"PROMPT> ")
    opened.clear()
    res = tail_last(tmp_path, n_lines=1)
    assert _drain(res) == b"PROMPT> "
    assert _streams(opened) <= {LAST_SEG}


def test_tail_c_reads_only_last_segment(tmp_path: Path, opened) -> None:
    info = _build_session(tmp_path)
    opened.clear()
    res = tail_last(tmp_path, c_bytes=250)
    assert _drain(res) == info["data"][-250:]
    assert _streams(opened) <= {LAST_SEG}


def test_bytes_since_caught_up_reads_only_scan(tmp_path: Path, opened) -> None:
    info = _build_session(tmp_path)
    opened.clear()
    res = bytes_since(tmp_path, from_byte=info["tip"] + 1)
    assert _drain(res) == b""
    assert res.next_byte == info["tip"] + 1
    assert res.last_line == info["lines"]
    assert _streams(opened) <= {LAST_SEG}


def test_bytes_since_suffix_reads_suffix_segments(tmp_path: Path, opened) -> None:
    info = _build_session(tmp_path)
    opened.clear()
    # Last 150 positions: starts inside the second-to-last segment.
    res = bytes_since(tmp_path, from_byte=info["tip"] - 149)
    assert _drain(res) == info["data"][-150:]
    assert _streams(opened) <= {stream_name(SEGS - 2), LAST_SEG}


def test_lines_since_mid_cursor_reads_suffix(tmp_path: Path, opened) -> None:
    info = _build_session(tmp_path)
    opened.clear()
    from_line = 5 * LINES_PER + 1  # first line of segment 5
    res = lines_since(tmp_path, from_line=from_line)
    assert _drain(res) == info["data"][(from_line - 1) * LINE_LEN :]
    assert _streams(opened) <= {stream_name(s) for s in range(5, SEGS)}


def test_lines_since_cursor_ahead_reads_only_scan(tmp_path: Path, opened) -> None:
    info = _build_session(tmp_path, partial=b"OPEN")
    opened.clear()
    res = lines_since(tmp_path, from_line=info["lines"] + 5)
    assert _drain(res) == b""
    assert res.last_line == info["lines"]
    assert _streams(opened) <= {LAST_SEG}


def test_head_n_reads_only_head_segments(tmp_path: Path, opened) -> None:
    info = _build_session(tmp_path)
    opened.clear()
    res = head_first(tmp_path, n_lines=3)
    assert _drain(res) == info["data"][: 3 * LINE_LEN]
    assert res.last_line == 3
    assert res.next_byte == 3 * LINE_LEN + 1
    assert _streams(opened) <= {stream_name(0), LAST_SEG}


def test_head_c_reads_only_head_segments(tmp_path: Path, opened) -> None:
    info = _build_session(tmp_path)
    opened.clear()
    res = head_first(tmp_path, c_bytes=120)
    assert _drain(res) == info["data"][:120]
    assert _streams(opened) <= {stream_name(0), LAST_SEG}


def test_head_t_bounds_load_to_cut(tmp_path: Path, opened) -> None:
    info = _build_session(tmp_path)
    opened.clear()
    res = lines_until_time(tmp_path, until_t=1000.0 + 150.5)  # lines 1..150
    assert _drain(res) == info["data"][: 150 * LINE_LEN]
    assert res.last_line == 150
    assert _streams(opened) <= {stream_name(s) for s in range(0, 4)} | {LAST_SEG}


def test_cat_streams_everything(tmp_path: Path, opened) -> None:
    info = _build_session(tmp_path, partial=b"OPEN")
    opened.clear()
    res = cat_all(tmp_path)
    assert res.stdout == b""
    assert res.stdout_iter is not None
    assert _drain(res) == info["data"]
    assert res.next_byte == info["tip"] + 1
    assert res.emitted_byte == info["tip"]
    assert res.stderr_lines == []
    assert _streams(opened) == {stream_name(s) for s in range(SEGS)}
