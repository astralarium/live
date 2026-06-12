"""Rotation and retention via a small segmentKb/maxKb config."""

from __future__ import annotations

import json
from pathlib import Path


def _configure(project: Path, *, segment_kb: int, max_kb: int) -> None:
    (project / ".live").mkdir(mode=0o700, exist_ok=True)
    (project / ".live" / "config.json").write_text(
        json.dumps({"segmentKb": segment_kb, "maxKb": max_kb})
    )


def _segments(project: Path) -> tuple[Path, list[str], list[str]]:
    [sess] = list((project / ".live" / "sessions").iterdir())
    files = sorted(p.name for p in sess.iterdir())
    streams = [n for n in files if n.startswith("stream.")]
    idxs = [n for n in files if n.startswith("lines.")]
    return sess, streams, idxs


def _retained_stream_bytes(project: Path) -> int:
    sess, streams, _ = _segments(project)
    return sum((sess / s).stat().st_size for s in streams)


def _session_info(project: Path, run_live) -> dict:
    ls = run_live(project, "ls", "-a", "--json")
    return json.loads(ls.stdout.splitlines()[0])


# maxKb is a hard cap: retained <= max_bytes + one active segment.
def _cap_bytes(*, segment_kb: int, max_kb: int) -> int:
    return (max_kb + segment_kb) * 1024


def test_rotation_creates_multiple_segments(project: Path, run_live) -> None:
    # 1 KB segments. Each printf writes ~16 B + CRLF.
    _configure(project, segment_kb=1, max_kb=64)
    # 200 lines of 20 chars -> ~4 KB total -> multiple rotations.
    run_live(
        project,
        "run",
        "--",
        "sh",
        "-c",
        "i=0; while [ $i -lt 200 ]; do printf 'line-number-%03d\\n' $i; i=$((i+1)); done",
    )
    sess, streams, idxs = _segments(project)
    assert len(streams) >= 2, f"expected rotation, got {streams}"
    assert len(streams) == len(idxs)
    # Closed segments are exactly segmentKb: rotation splits mid-line.
    for s in streams[:-1]:
        assert (sess / s).stat().st_size == 1024


def test_retention_drops_oldest_segments(project: Path, run_live) -> None:
    # Tiny budget: 1 KB segments, 2 KB total cap. Produce ~5 KB.
    _configure(project, segment_kb=1, max_kb=2)
    run_live(
        project,
        "run",
        "--",
        "sh",
        "-c",
        "i=0; while [ $i -lt 250 ]; do printf 'line-number-%03d\\n' $i; i=$((i+1)); done",
    )
    assert _retained_stream_bytes(project) <= _cap_bytes(segment_kb=1, max_kb=2)

    # Watermarks should reflect that firstLine > 1 (retention dropped some).
    info = _session_info(project, run_live)
    assert info["firstLine"] > 1
    assert info["lastLine"] == 250


def test_since_after_retention_reports_gap(project: Path, run_live) -> None:
    _configure(project, segment_kb=1, max_kb=2)
    # Lines wide enough that 250 of them blow well past the 2 KB retention cap.
    run_live(
        project,
        "run",
        "-n",
        "spam",
        "--",
        "sh",
        "-c",
        "i=0; while [ $i -lt 250 ]; do "
        "printf 'line-number-%04d-with-padding\\n' $i; i=$((i+1)); done",
    )
    poll = run_live(project, "tail", "-vn", "+0", "spam")
    assert "dropped" in poll.stderr
    assert "first-line=" in poll.stderr
    # The floor lands mid-line, so the whole-lines gap and the head of the
    # first emitted line are reported as ONE notice, not two.
    assert poll.stderr.count("dropped") == 1
    assert " lines + " in poll.stderr


def test_fast_burst_fixed_width_lines_retains_tail(project: Path, run_live) -> None:
    """A fast burst arrives in PTY chunks that rarely end on a line boundary.
    Rotation splits at the segment budget regardless, so retention keeps
    pace and the tail of the log survives."""
    _configure(project, segment_kb=1, max_kb=2)
    # ~26 KB burst: 2000 lines, 13 B each under the PTY (CRLF).
    run_live(
        project,
        "run",
        "-n",
        "burst",
        "--",
        "awk",
        'BEGIN { for (i = 1; i <= 2000; i++) printf "line-%06d\\n", i }',
    )
    info = _session_info(project, run_live)
    assert info["lastLine"] == 2000
    assert info["firstLine"] > 1

    out = run_live(project, "cat", "burst")
    lines = out.stdout.replace("\r", "").splitlines()
    assert lines, "retention deleted all output"
    assert lines[-1] == "line-002000"

    _, streams, _ = _segments(project)
    assert len(streams) >= 2, f"expected rotation, got {streams}"
    assert _retained_stream_bytes(project) <= _cap_bytes(segment_kb=1, max_kb=2)


def test_single_oversized_line_is_capped(project: Path, run_live) -> None:
    """A line larger than maxKb cannot be retained whole: the cap always
    wins. Its retained tail is still readable, and watermarks report no
    fully-retained line (firstLine=0) while lastLine counts it."""
    _configure(project, segment_kb=1, max_kb=2)
    run_live(
        project,
        "run",
        "-n",
        "wide",
        "--",
        "awk",
        'BEGIN { for (i = 0; i < 5000; i++) printf "x"; print "" }',
    )
    assert _retained_stream_bytes(project) <= _cap_bytes(segment_kb=1, max_kb=2)

    info = _session_info(project, run_live)
    assert info["lastLine"] == 1
    assert info["firstLine"] == 0

    out = run_live(project, "cat", "-v", "wide")
    body = out.stdout.replace("\r", "").rstrip("\n")
    assert body, "retention deleted all output"
    assert set(body) == {"x"}, f"unexpected content: {body[:80]!r}"
    assert len(body) < 5000  # head was capped away
    assert len(body) >= 1024  # but the retained tail is intact

    # The line started at lifetime 0, so the truncated head is exactly the
    # retention floor: total written (5000 x's + CRLF) minus retained bytes.
    dropped = 5002 - _retained_stream_bytes(project)
    assert f"dropped {dropped} bytes (from-byte=0" in out.stderr


def test_no_newline_output_is_capped(project: Path, run_live) -> None:
    """Output that never emits a newline (progress bars, binary dumps) must
    still rotate and stay under the cap — rotation is not gated on line
    ends. The retained bytes read back as a partial tail."""
    _configure(project, segment_kb=1, max_kb=2)
    run_live(
        project,
        "run",
        "-n",
        "noline",
        "--",
        "awk",
        'BEGIN { for (i = 0; i < 5000; i++) printf "y" }',
    )
    _, streams, _ = _segments(project)
    assert len(streams) >= 2, f"expected rotation, got {streams}"
    assert _retained_stream_bytes(project) <= _cap_bytes(segment_kb=1, max_kb=2)

    info = _session_info(project, run_live)
    assert info["firstLine"] == 0
    assert info["lastLine"] == 0

    out = run_live(project, "cat", "-v", "noline")
    assert out.stdout
    assert set(out.stdout) == {"y"}

    # No record exists yet, but the idx header's open-line offset still lets
    # readers report exactly how much of the unfinished line is gone.
    dropped = 5000 - _retained_stream_bytes(project)
    assert f"dropped {dropped} bytes (from-byte=0" in out.stderr
    assert "partial-line" in out.stderr


def test_line_spanning_segments_reads_whole(project: Path, run_live) -> None:
    """Rotation mid-line makes lines span segments. Reads must reassemble
    them byte-exactly, and line cursors must keep working across spans."""
    _configure(project, segment_kb=1, max_kb=64)
    # Three 1500-char lines: each is wider than a segment.
    run_live(
        project,
        "run",
        "-n",
        "span",
        "--",
        "awk",
        "BEGIN { for (c = 1; c <= 3; c++) {"
        ' for (i = 0; i < 1500; i++) printf "%c", 96 + c; print "" } }',
    )
    info = _session_info(project, run_live)
    assert info["firstLine"] == 1
    assert info["lastLine"] == 3

    out = run_live(project, "cat", "span")
    lines = out.stdout.replace("\r", "").splitlines()
    assert lines == ["a" * 1500, "b" * 1500, "c" * 1500]

    # Line cursor lands on a spanning line's true start.
    tail = run_live(project, "tail", "-n", "+2", "span")
    tail_lines = tail.stdout.replace("\r", "").splitlines()
    assert tail_lines == ["b" * 1500, "c" * 1500]


def test_cat_without_retention_emits_no_gap_warning(project: Path, run_live) -> None:
    """`cat -v` on a fresh session must not emit a spurious 'dropped' warning.
    Regression: cat_all called lines_since(since=0), and 0 < first_line=1 used
    to wrongly trigger the gap path."""
    run_live(project, "run", "-n", "fresh", "--", "echo", "foo")
    out = run_live(project, "cat", "-v", "fresh")
    assert out.stdout.replace("\r", "") == "foo\n"
    assert "dropped" not in out.stderr
