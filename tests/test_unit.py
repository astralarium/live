"""Pure-function unit tests for reader helpers, format primitives, selector
resolution, name-lock acquisition, and the polling watcher backend."""

from __future__ import annotations

import fcntl
import os
import signal
import time
from pathlib import Path

import pytest

from live.format import (
    IDX_HEADER,
    IDX_MAGIC,
    IDX_RECORD,
    IDX_VERSION,
    Meta,
    Watermarks,
    count_complete_lines,
    idx_record_after,
    idx_record_count,
    last_idx_record,
    pack_idx_header,
    read_segment_start,
)
from live.reader import (
    StreamView,
    should_strip_ansi,
)
from live.session import (
    NoSuchSelectorError,
    SelectorError,
    SessionInfo,
    resolve_many,
    resolve_one,
)
from live.lock import HeldLock, LockTimeout, acquire_lock, probe_held
from live.watcher import _PollWatcher


# ----- should_strip_ansi resolution -----


@pytest.mark.parametrize(
    "explicit_strip,explicit_raw,is_tty,expected",
    [
        (False, False, False, True),  # default: not tty -> strip
        (False, False, True, False),  # default: tty -> raw
        (True, False, True, True),  # --strip-ansi wins
        (False, True, False, False),  # --raw wins
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


# ----- StreamView extent math -----


def _view(
    data: bytes,
    records: list[tuple[int, float, int]],
    base: int = 0,
    line_start: int | None = None,
) -> StreamView:
    """Build a view the way load_stream_view does: last_end = just past the
    last indexed line's newline."""
    if records:
        nl = data.find(b"\n", max(records[-1][2] - base, 0))
        last_end = base + nl + 1
    else:
        last_end = base
    return StreamView(
        base=base,
        data=data,
        records=records,
        last_end=last_end,
        line_start=base if line_start is None else line_start,
    )


def test_stream_view_extents_split_on_record_offsets() -> None:
    data = b"alpha\nbravo\ncharlie\n"
    recs = [(1, 0.0, 0), (2, 0.0, 6), (3, 0.0, 12)]
    v = _view(data, recs)
    assert [v.slice(v.start_of(n), v.end_of(n)) for n in (1, 2, 3)] == [
        b"alpha\n",
        b"bravo\n",
        b"charlie\n",
    ]
    assert v.partial_len == 0


def test_stream_view_partial_tail_after_last_record() -> None:
    # 2 complete lines, then a partial "downloading 50%" (no \n)
    data = b"alpha\nbravo\ndownloading 50%"
    v = _view(data, [(1, 0.0, 0), (2, 0.0, 6)])
    assert v.slice(v.last_end, v.tip) == b"downloading 50%"
    assert v.partial_len == 15


def test_stream_view_no_records_is_all_partial() -> None:
    v = _view(b"some text\n", [])
    assert v.first_line == 0
    assert v.last_line == 0
    assert v.slice(v.last_end, v.tip) == b"some text\n"


def test_stream_view_head_truncated_first_record() -> None:
    # Line 5's head was retained away (b=90 < base=100); line 6 is full.
    data = b"tail-of-5\nline-6\n"
    v = _view(data, [(5, 0.0, 90), (6, 0.0, 110)], base=100, line_start=90)
    assert v.first_line == 6
    assert v.last_line == 6
    assert v.truncated_head == 10
    # The truncated record still slices to its retained suffix.
    assert v.slice(v.start_of(5), v.end_of(5)) == b"tail-of-5\n"
    assert v.slice(v.start_of(6), v.end_of(6)) == b"line-6\n"


def test_stream_view_line_spanning_offsets() -> None:
    # One line larger than any segment: extents are pure offset math, so a
    # spanning line is whole as long as its bytes are retained.
    body = b"x" * 100 + b"\n" + b"y\n"
    v = _view(body, [(1, 0.0, 0), (2, 0.0, 101)])
    assert v.slice(v.start_of(1), v.end_of(1)) == b"x" * 100 + b"\n"
    assert v.slice(v.start_of(2), v.end_of(2)) == b"y\n"


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


def test_meta_tty_closed_and_detached_roundtrip() -> None:
    m = Meta(
        id="x",
        command=["true"],
        cwd="/",
        started_at=1.0,
        tty_closed_at=2.5,
        detached=True,
    )
    assert Meta.from_dict(m.to_dict()) == m


def test_meta_omits_tty_fields_by_default() -> None:
    d = Meta(id="x", command=["true"], cwd="/", started_at=1.0).to_dict()
    assert "ttyClosedAt" not in d
    assert "detached" not in d


def test_count_complete_lines(tmp_path) -> None:
    p = tmp_path / "stream.0000.log"
    p.write_bytes(b"one\ntwo\nthree\n")
    assert count_complete_lines(p) == 3


def test_count_complete_lines_ignores_partial_tail(tmp_path) -> None:
    p = tmp_path / "stream.0000.log"
    p.write_bytes(b"one\ntwo\nthree-without-newline")
    assert count_complete_lines(p) == 2


def test_idx_header_roundtrip(tmp_path) -> None:
    p = tmp_path / "lines.0000.idx"
    p.write_bytes(pack_idx_header(4096, 4000))
    assert read_segment_start(p) == 4096


def test_idx_header_rejects_bad_magic(tmp_path) -> None:
    p = tmp_path / "lines.0000.idx"
    p.write_bytes(IDX_HEADER.pack(b"NOPE", IDX_VERSION, 4096, 4000))
    assert read_segment_start(p) is None


def test_idx_header_rejects_unknown_version(tmp_path) -> None:
    p = tmp_path / "lines.0000.idx"
    p.write_bytes(IDX_HEADER.pack(IDX_MAGIC, IDX_VERSION + 1, 4096, 4000))
    assert read_segment_start(p) is None


def test_idx_record_count(tmp_path) -> None:
    p = tmp_path / "lines.0000.idx"
    p.write_bytes(pack_idx_header(0, 0) + b"\x00" * (3 * 24))  # header + 3 records
    assert idx_record_count(p) == 3


def test_last_idx_record_ignores_torn_append(tmp_path) -> None:
    # Readers are lock-free against a live writer: a record observed
    # mid-append must not be read straddled across two records.
    p = tmp_path / "lines.0000.idx"
    rec1 = IDX_RECORD.pack(1, 1000.0, 0)
    rec2 = IDX_RECORD.pack(2, 1001.0, 10)
    p.write_bytes(pack_idx_header(0, 0) + rec1 + rec2 + rec1[:7])  # torn third record
    assert last_idx_record(p) == (2, 1001.0, 10)


def test_last_idx_record_header_only(tmp_path) -> None:
    p = tmp_path / "lines.0000.idx"
    p.write_bytes(pack_idx_header(0, 0))
    assert last_idx_record(p) is None


def _write_idx(p, offsets: list[int]) -> None:
    """Idx with line n at byte offsets[n-1], timestamp 1000+n."""
    recs = b"".join(IDX_RECORD.pack(n, 1000.0 + n, b) for n, b in enumerate(offsets, 1))
    p.write_bytes(pack_idx_header(0, 0) + recs)


def test_idx_record_after_uniform_offsets(tmp_path) -> None:
    p = tmp_path / "lines.0000.idx"
    _write_idx(p, [n * 100 for n in range(100)])  # lines 1..100 every 100 bytes
    assert idx_record_after(p, 2, 0) == (2, 1002.0, 100)
    assert idx_record_after(p, 2, 4950) == (51, 1051.0, 5000)
    assert idx_record_after(p, 1, 1050.5) == (51, 1051.0, 5000)


def test_idx_record_after_boundaries(tmp_path) -> None:
    p = tmp_path / "lines.0000.idx"
    _write_idx(p, [n * 100 for n in range(100)])
    assert idx_record_after(p, 2, -1) == (1, 1001.0, 0)  # below first: first wins
    assert idx_record_after(p, 2, 9900) is None  # cut at last offset: strict >
    assert idx_record_after(p, 1, 1100.0) is None
    assert idx_record_after(p, 2, 5000) == (52, 1052.0, 5100)  # exact hit excluded


def test_idx_record_after_skewed_offsets(tmp_path) -> None:
    # Interpolation's guess is badly misled (one huge line among tiny ones);
    # the halving fallback must still converge to the exact record.
    p = tmp_path / "lines.0000.idx"
    offsets = list(range(50)) + [60_000 + n for n in range(50)]
    _write_idx(p, offsets)
    assert idx_record_after(p, 2, 48) == (50, 1050.0, 49)
    assert idx_record_after(p, 2, 49) == (51, 1051.0, 60_000)
    assert idx_record_after(p, 2, 59_999) == (51, 1051.0, 60_000)
    assert idx_record_after(p, 2, 60_010) == (62, 1062.0, 60_011)


def test_idx_record_after_ignores_torn_append(tmp_path) -> None:
    p = tmp_path / "lines.0000.idx"
    rec1 = IDX_RECORD.pack(1, 1000.0, 0)
    rec2 = IDX_RECORD.pack(2, 1001.0, 10)
    p.write_bytes(pack_idx_header(0, 0) + rec1 + rec2 + rec1[:7])
    assert idx_record_after(p, 2, 0) == (2, 1001.0, 10)
    assert idx_record_after(p, 2, 10) is None  # torn record never qualifies


def test_idx_record_after_header_only_and_missing(tmp_path) -> None:
    p = tmp_path / "lines.0000.idx"
    p.write_bytes(pack_idx_header(0, 0))
    assert idx_record_after(p, 2, 0) is None
    assert idx_record_after(tmp_path / "nope.idx", 2, 0) is None


# ----- lock probing -----


def test_probe_held_missing_file_is_none(tmp_path: Path) -> None:
    assert probe_held(tmp_path / "process.lock") is None


def test_probe_held_unheld_file_is_false(tmp_path: Path) -> None:
    lock = tmp_path / "process.lock"
    lock.write_text("12345\n")
    assert probe_held(lock) is False


def test_probe_held_exclusive_holder_is_true(tmp_path: Path) -> None:
    # Simulate a live recorder: hold LOCK_EX while probing.
    lock = tmp_path / "process.lock"
    fd = acquire_lock(lock, 12345)
    try:
        assert probe_held(lock) is True
    finally:
        os.close(fd)
    assert probe_held(lock) is False


def test_probe_held_ignores_concurrent_probe(tmp_path: Path) -> None:
    """A racing probe (transient LOCK_SH holder) must not make a dead
    session read as alive — probes contend only with the recorder's EX."""
    lock = tmp_path / "process.lock"
    lock.write_text("12345\n")
    fd = os.open(str(lock), os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        assert probe_held(lock) is False
    finally:
        os.close(fd)


# ----- selector resolution -----


def _stub(id_: str, name: str | None = None) -> SessionInfo:
    return SessionInfo(
        id=id_,
        path=Path(f"/sessions/{id_}"),
        meta=Meta(id=id_, command=["sh"], cwd="/", started_at=0.0, name=name),
        status="exited",
        watermarks=Watermarks(0, 0, 0, 0, 0),
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


# ----- HeldLock (name lock) acquisition -----


def test_held_lock_times_out_and_names_holder(tmp_path: Path, capfd) -> None:
    lock_path = tmp_path / "name.lock"
    guard = HeldLock(lock_path)
    with pytest.raises(LockTimeout) as ei:
        HeldLock(lock_path, timeout=0.4, notice_after=0.1)
    assert f"held by pid {os.getpid()}" in str(ei.value)
    # The one-line wait notice precedes the timeout.
    assert "waiting for name lock" in capfd.readouterr().err
    guard.release()
    HeldLock(lock_path, timeout=0.5).release()  # acquirable again


def test_held_lock_inherited_copy_keeps_lock_alive(tmp_path: Path) -> None:
    """The bug class close_inherited() exists for: a forked child that keeps
    its fd copy holds the lock even after the parent's fd is gone."""
    lock_path = tmp_path / "name.lock"
    guard = HeldLock(lock_path)
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:  # child: keep the inherited fd open
        try:
            os.write(w, b"x")
            time.sleep(10)
        finally:
            os._exit(0)
    os.close(w)
    assert os.read(r, 1) == b"x"
    os.close(r)
    os.close(guard._fd)  # simulate the parent dying without release()
    guard._fd = -1
    try:
        with pytest.raises(LockTimeout):
            HeldLock(lock_path, timeout=0.3, notice_after=10)
    finally:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)


def test_held_lock_close_inherited_drops_orphan_hold(tmp_path: Path) -> None:
    """A child that calls close_inherited() leaves the parent's fd as the
    lock's only reference, so parent death auto-releases it."""
    lock_path = tmp_path / "name.lock"
    guard = HeldLock(lock_path)
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            guard.close_inherited()
            os.write(w, b"x")
            time.sleep(10)
        finally:
            os._exit(0)
    os.close(w)
    assert os.read(r, 1) == b"x"  # child's copy is closed
    os.close(r)
    os.close(guard._fd)  # simulate the parent dying without release()
    guard._fd = -1
    try:
        HeldLock(lock_path, timeout=0.5, notice_after=10).release()
    finally:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)


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
