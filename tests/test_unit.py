"""Pure-function unit tests for reader helpers, format primitives, selector
resolution, and the polling watcher backend."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from live.format import IDX_RECORD, Meta, Watermarks, count_complete_lines, idx_record_count
from live.reader import (
    lines_in_segment,
    partial_tail_bytes,
    should_strip_ansi,
)
from live.session import (
    NoSuchSelectorError,
    SelectorError,
    SessionInfo,
    resolve_many,
    resolve_one,
)
from live.watcher import _PollWatcher


# ----- should_strip_ansi resolution -----


@pytest.mark.parametrize(
    "explicit_strip,explicit_raw,is_tty,expected",
    [
        (False, False, False, True),   # default: not tty -> strip
        (False, False, True, False),   # default: tty -> raw
        (True, False, True, True),     # --strip-ansi wins
        (False, True, False, False),   # --raw wins
    ],
)
def test_should_strip_ansi_matrix(
    explicit_strip: bool,
    explicit_raw: bool,
    is_tty: bool,
    expected: bool,
) -> None:
    assert (
        should_strip_ansi(
            explicit_strip=explicit_strip,
            explicit_raw=explicit_raw,
            stdout_is_tty=is_tty,
        )
        is expected
    )


# ----- lines_in_segment / partial_tail_bytes -----


def _records(n: int) -> list[tuple[int, float, int]]:
    """Stand-in idx records: only line numbers matter for these helpers."""
    return [(i + 1, 0.0, 0) for i in range(n)]


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
    n, t, b = 42, 1717200000.123456, 12345678
    buf = IDX_RECORD.pack(n, t, b)
    assert len(buf) == 24
    unpacked = IDX_RECORD.unpack(buf)
    assert unpacked[0] == n
    assert unpacked[1] == pytest.approx(t, abs=1e-9)
    assert unpacked[2] == b


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
    p.write_bytes(b"\x00" * (8 + 3 * 24))  # 8-byte header + 3 records
    assert idx_record_count(p) == 3


# ----- selector resolution -----


def _stub(id_: str, name: str | None = None) -> SessionInfo:
    return SessionInfo(
        id=id_,
        path=Path(f"/sessions/{id_}"),
        meta=Meta(id=id_, command=["sh"], cwd="/", started_at=0.0, name=name),
        status="exited",
        watermarks=Watermarks(0, 0, 0, 0, 0, 0, 0),
        last_activity=0.0,
        exited_at=None,
        exit_code=None,
    )


def test_resolve_one_no_match_raises_no_such() -> None:
    with pytest.raises(NoSuchSelectorError, match="no such session"):
        resolve_one([_stub("abc")], "zzz")


def test_resolve_one_ambiguous_uuid_prefix_raises_selector_error() -> None:
    a = _stub("abc12345-0000-0000-0000-000000000001")
    b = _stub("abc12345-0000-0000-0000-000000000002")
    with pytest.raises(SelectorError, match="ambiguous") as ei:
        resolve_one([a, b], "abc")
    # Ambiguity is NOT a NoSuchSelectorError — `rm -f` should still surface it.
    assert not isinstance(ei.value, NoSuchSelectorError)


def test_resolve_one_unique_prefix_returns_match() -> None:
    a = _stub("abc12345-0000-0000-0000-000000000001")
    b = _stub("def00000-0000-0000-0000-000000000002")
    assert resolve_one([a, b], "abc").id == a.id


def test_resolve_one_name_takes_priority_over_uuid_prefix() -> None:
    a = _stub("zzz00000-0000-0000-0000-000000000000", name="abc")
    b = _stub("abc12345-0000-0000-0000-000000000001")
    # `abc` matches a's NAME and would also match b's UUID prefix; name wins.
    assert resolve_one([a, b], "abc").id == a.id


def test_resolve_many_returns_every_name_match() -> None:
    a = _stub("id-1", name="dup")
    b = _stub("id-2", name="dup")
    c = _stub("id-3", name="other")
    result = resolve_many([a, b, c], "dup")
    assert {s.id for s in result} == {a.id, b.id}


def test_resolve_many_no_match_raises_no_such() -> None:
    with pytest.raises(NoSuchSelectorError):
        resolve_many([_stub("abc")], "zzz")


def test_resolve_many_ambiguous_uuid_prefix_raises_selector_error() -> None:
    a = _stub("abc12345-0000-0000-0000-000000000001")
    b = _stub("abc12345-0000-0000-0000-000000000002")
    with pytest.raises(SelectorError, match="ambiguous") as ei:
        resolve_many([a, b], "abc")
    assert not isinstance(ei.value, NoSuchSelectorError)


# ----- _PollWatcher fallback backend -----


def test_poll_watcher_detects_modification(tmp_path: Path) -> None:
    p = tmp_path / "f"
    p.write_text("hello")
    w = _PollWatcher(interval=0.01)
    try:
        w.add_path(p)
        assert w.wait(0.1) == set()  # no change yet
        # Bump mtime AND size so a coarse-clock filesystem still registers it.
        p.write_text("hello world")
        assert w.wait(1.0) == {p}
    finally:
        w.close()


def test_poll_watcher_detects_deletion(tmp_path: Path) -> None:
    p = tmp_path / "f"
    p.write_text("x")
    w = _PollWatcher(interval=0.01)
    try:
        w.add_path(p)
        p.unlink()
        assert w.wait(1.0) == {p}
    finally:
        w.close()


def test_poll_watcher_timeout_returns_empty(tmp_path: Path) -> None:
    p = tmp_path / "stable"
    p.write_text("x")
    w = _PollWatcher(interval=0.01)
    try:
        w.add_path(p)
        t0 = time.time()
        assert w.wait(0.15) == set()
        assert time.time() - t0 >= 0.10
    finally:
        w.close()
