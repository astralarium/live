"""`live tail -t T` time-range filtering + trailer `last-time`."""

from __future__ import annotations

import re
import time
from pathlib import Path


_TRAILER_RE = re.compile(
    r"live: id=([0-9a-f-]+) next-line=(\d+) next-byte=\d+ last-time=([0-9.]+)"
)


def _trailer(stderr: str) -> tuple[str, int, float]:
    m = _TRAILER_RE.search(stderr)
    assert m, f"no trailer found in stderr: {stderr!r}"
    return m.group(1), int(m.group(2)), float(m.group(3))


def test_time_filters_by_idx_timestamp(project: Path, run_live) -> None:
    # Record two lines, pause, record two more.
    run_live(
        project,
        "run",
        "-n",
        "timed",
        "--",
        "sh",
        "-c",
        "echo early-1; echo early-2; sleep 0.6; echo late-1; echo late-2",
    )

    # Probe the trailer to learn the time, then filter by a midpoint.
    full = run_live(project, "tail", "-vn", "+0", "timed")
    _, _, end_time = _trailer(full.stderr)
    cut = end_time - 0.3  # somewhere between "early" and "late" writes

    out = run_live(project, "tail", "-v", "-t", f"{cut:.6f}", "timed")
    body = out.stdout.replace("\r", "")
    assert "late-1" in body and "late-2" in body
    # The early lines were recorded well before cut; they must not appear.
    assert "early-1" not in body
    assert "early-2" not in body
    # -v requested -> trailer present.
    assert "last-time=" in out.stderr


def test_time_duration_form(project: Path, run_live) -> None:
    # `-t 1h` = lines from the last hour: everything just recorded qualifies.
    run_live(project, "run", "-n", "dur", "--", "sh", "-c", "echo recent")
    out = run_live(project, "tail", "-v", "-t", "1h", "dur")
    assert "recent" in out.stdout.replace("\r", "")

    # A zero-length window excludes lines written before now.
    out = run_live(project, "tail", "-v", "-t", "0s", "dur")
    assert "recent" not in out.stdout.replace("\r", "")


def test_time_iso_form(project: Path, run_live) -> None:
    run_live(project, "run", "-n", "iso", "--", "sh", "-c", "echo hello")
    out = run_live(project, "tail", "-t", "2000-01-01", "iso")
    assert "hello" in out.stdout.replace("\r", "")


def test_time_rejects_garbage(project: Path, run_live) -> None:
    out = run_live(project, "tail", "-t", "5x", "whatever", check=False)
    assert out.returncode == 2
    assert "epoch seconds, duration" in out.stderr


def test_time_quiet_without_verbose(project: Path, run_live) -> None:
    # -t alone (no -v) prints lines but no stderr metadata.
    run_live(project, "run", "-n", "quiet", "--", "sh", "-c", "echo hello")
    out = run_live(project, "tail", "-t", "0", "quiet")
    assert "hello" in out.stdout.replace("\r", "")
    assert out.stderr == "", f"expected silent stderr, got: {out.stderr!r}"


def test_time_in_the_future_emits_cursor_ahead(project: Path, run_live) -> None:
    run_live(
        project, "run", "-n", "fut", "--",
        "sh", "-c", "echo hello",
    )
    future = time.time() + 3600
    out = run_live(project, "tail", "-v", "-t", f"{future:.3f}", "fut")
    assert out.stdout.replace("\r", "") == ""
    assert "from-time=" in out.stderr
    assert "> last-time=" in out.stderr
    assert "check id" in out.stderr


def test_trailer_at_time_advances_after_more_writes(project: Path, run_live) -> None:
    run_live(project, "run", "-n", "adv", "--", "sh", "-c", "echo a")
    first = run_live(project, "tail", "-vn", "+0", "adv")
    _, _, t1 = _trailer(first.stderr)

    # Run again under the same NAME — new session, different uuid; the
    # newest-match-wins selector picks it. Its time should be later.
    time.sleep(0.1)
    run_live(project, "run", "-n", "adv", "--", "sh", "-c", "echo b")
    second = run_live(project, "tail", "-vn", "+0", "adv")
    _, _, t2 = _trailer(second.stderr)
    assert t2 > t1, f"expected time to advance: {t1} -> {t2}"
