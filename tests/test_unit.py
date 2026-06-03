"""Pure-function unit tests for reader helpers and format primitives."""

from __future__ import annotations

import struct

import pytest

from live.format import IDX_RECORD, Meta, count_complete_lines, idx_record_count
from live.reader import (
    lines_in_segment,
    partial_tail_bytes,
    should_strip_ansi,
    strip_ansi,
)


# ----- ANSI strip -----


def test_strip_ansi_csi_color_codes() -> None:
    raw = b"\x1b[31mred\x1b[0m\n\x1b[1;32mbold-green\x1b[0m\n"
    assert strip_ansi(raw) == b"red\nbold-green\n"


def test_strip_ansi_osc_window_title() -> None:
    raw = b"\x1b]0;my title\x07after\n"
    assert strip_ansi(raw) == b"after\n"


def test_strip_ansi_two_byte_escape() -> None:
    # ESC D (Index, 0x1B 0x44) is a 2-byte Fe escape in the @-_ range.
    raw = b"before\x1bDafter\n"
    assert strip_ansi(raw) == b"beforeafter\n"


def test_strip_ansi_passthrough_for_clean_text() -> None:
    assert strip_ansi(b"plain text\n") == b"plain text\n"


# ----- should_strip_ansi resolution -----


@pytest.mark.parametrize(
    "explicit_strip,explicit_raw,is_since,is_tty,expected",
    [
        (False, False, False, False, True),   # default: not tty -> strip
        (False, False, False, True, False),   # default: tty -> raw
        (False, False, True, True, True),     # --since wins
        (True, False, False, True, True),     # --strip-ansi wins
        (False, True, False, False, False),   # --raw wins
        (False, True, True, False, False),    # --raw still wins over --since
    ],
)
def test_should_strip_ansi_matrix(
    explicit_strip: bool,
    explicit_raw: bool,
    is_since: bool,
    is_tty: bool,
    expected: bool,
) -> None:
    assert (
        should_strip_ansi(
            explicit_strip=explicit_strip,
            explicit_raw=explicit_raw,
            is_since=is_since,
            stdout_is_tty=is_tty,
        )
        is expected
    )


# ----- lines_in_segment / partial_tail_bytes -----


def _records(n: int) -> list[tuple[int, float]]:
    """Stand-in idx records: only line numbers matter for these helpers."""
    return [(i + 1, 0.0) for i in range(n)]


def test_lines_in_segment_splits_on_newlines() -> None:
    stream = b"alpha\nbravo\ncharlie\n"
    assert lines_in_segment(stream, _records(3)) == [b"alpha\n", b"bravo\n", b"charlie\n"]


def test_lines_in_segment_returns_empty_when_no_records() -> None:
    assert lines_in_segment(b"some text\n", []) == []


def test_partial_tail_bytes_returns_unindexed_trailing() -> None:
    # 2 complete lines, then a partial "downloading 50%" (no \n)
    stream = b"alpha\nbravo\ndownloading 50%"
    assert partial_tail_bytes(stream, _records(2)) == b"downloading 50%"


def test_partial_tail_bytes_empty_when_all_indexed() -> None:
    stream = b"alpha\nbravo\n"
    assert partial_tail_bytes(stream, _records(2)) == b""


# ----- format helpers -----


def test_idx_record_pack_unpack_roundtrip() -> None:
    n, t = 42, 1717200000.123456
    buf = IDX_RECORD.pack(n, t)
    assert len(buf) == 16
    assert IDX_RECORD.unpack(buf) == (n, pytest.approx(t, abs=1e-9))


def test_meta_roundtrips_through_dict() -> None:
    m = Meta(
        id="0190131a-8c00-7000-8000-000000000000",
        command=["sh", "-c", "echo hi"],
        cwd="/tmp",
        started_at=1717200000.5,
        exited_at=1717200001.25,
        exit_code=0,
        name="dev",
    )
    d = m.to_dict()
    m2 = Meta.from_dict(d)
    assert m == m2


def test_meta_without_name_omits_field() -> None:
    m = Meta(
        id="x",
        command=["true"],
        cwd="/",
        started_at=1.0,
    )
    assert "name" not in m.to_dict()


def test_count_complete_lines(tmp_path) -> None:
    p = tmp_path / "stream.0000.log"
    p.write_bytes(b"one\ntwo\nthree\n")
    assert count_complete_lines(p) == 3


def test_count_complete_lines_ignores_partial_tail(tmp_path) -> None:
    p = tmp_path / "stream.0000.log"
    p.write_bytes(b"one\ntwo\nthree-without-newline")
    assert count_complete_lines(p) == 2


def test_idx_record_count(tmp_path) -> None:
    p = tmp_path / "lines.0000.idx"
    p.write_bytes(b"\x00" * 48)  # 3 records
    assert idx_record_count(p) == 3
