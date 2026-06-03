"""Rotation and retention via a small segmentKb/maxKb config."""

from __future__ import annotations

import json
from pathlib import Path


def _configure(project: Path, *, segment_kb: int, max_kb: int) -> None:
    (project / ".live").mkdir(mode=0o700, exist_ok=True)
    (project / ".live" / "config.json").write_text(
        json.dumps({"segmentKb": segment_kb, "maxKb": max_kb})
    )


def _segments(project: Path) -> tuple[list[str], list[str]]:
    [sess] = list((project / ".live" / "sessions").iterdir())
    files = sorted(p.name for p in sess.iterdir())
    streams = [n for n in files if n.startswith("stream.")]
    idxs = [n for n in files if n.startswith("lines.")]
    return streams, idxs


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
    streams, idxs = _segments(project)
    assert len(streams) >= 2, f"expected rotation, got {streams}"
    assert len(streams) == len(idxs)


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
    streams, _ = _segments(project)
    # Total retained bytes should be <= maxKb*1024 + at-most-one-segment-overhang.
    [sess] = list((project / ".live" / "sessions").iterdir())
    total = sum((sess / s).stat().st_size for s in streams)
    assert total <= 4 * 1024  # 2 KB cap with one fat-segment leeway

    # Watermarks should reflect that firstLine > 1 (retention dropped some).
    ls = run_live(project, "ls", "-a", "--json")
    info = json.loads(ls.stdout.splitlines()[0])
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
    assert "first retained=" in poll.stderr
