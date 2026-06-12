"""`live tail -t T` time-range filtering + trailer `last-time`."""

from __future__ import annotations

import itertools
import re
import time
from pathlib import Path

import pytest

from live.format import idx_name, read_idx_records, stream_name
from live.timeutil import duration_secs, fmt_duration


_TRAILER_RE = re.compile(
    r"live: id=([0-9a-f-]+) next-line=(\d+) next-byte=\d+ last-time=([0-9.]+)"
)


def _trailer(stderr: str) -> tuple[str, int, float]:
    m = _TRAILER_RE.search(stderr)
    assert m, f"no trailer found in stderr: {stderr!r}"
    return m.group(1), int(m.group(2)), float(m.group(3))


def _line_times(session_dir: Path) -> dict[str, float]:
    """Map line text -> idx timestamp via the on-disk records (segment 0)."""
    stream = (session_dir / stream_name(0)).read_bytes()
    out: dict[str, float] = {}
    for _, t, off in read_idx_records(session_dir / idx_name(0)):
        text = stream[off:].split(b"\n", 1)[0].rstrip(b"\r").decode()
        out[text] = t
    return out


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

    # Derive the cut from the recorded idx timestamps rather than wall-clock
    # arithmetic, so scheduler load cannot move it across either batch.
    [sess_dir] = (project / ".live" / "sessions").iterdir()
    times = _line_times(sess_dir)
    cut = (times["early-2"] + times["late-1"]) / 2

    out = run_live(project, "tail", "-v", "-t", f"{cut:.6f}", "timed")
    body = out.stdout.replace("\r", "")
    assert "late-1" in body and "late-2" in body
    # The early timestamps sit strictly below the midpoint cut.
    assert "early-1" not in body
    assert "early-2" not in body
    # -v requested -> trailer present.
    assert "last-time=" in out.stderr


def test_time_duration_form(project: Path, run_live) -> None:
    # `-t 1h` = lines from the last hour: everything just recorded qualifies.
    run_live(project, "run", "-n", "dur", "--", "sh", "-c", "echo recent")
    out = run_live(project, "tail", "-v", "-t", "1h", "dur")
    assert "recent" in out.stdout.replace("\r", "")

    # Compound form, as shown by `live ps`.
    out = run_live(project, "tail", "-v", "-t", "1h30m", "dur")
    assert "recent" in out.stdout.replace("\r", "")

    # A zero-length window excludes lines written before now.
    out = run_live(project, "tail", "-v", "-t", "0s", "dur")
    assert "recent" not in out.stdout.replace("\r", "")


@pytest.mark.parametrize(
    "value,seconds",
    [
        ("90s", 90),
        ("2h30m", 2 * 3600 + 30 * 60),
        ("3d4h", 3 * 86400 + 4 * 3600),
        ("1d2h3m4s", 86400 + 2 * 3600 + 3 * 60 + 4),
        ("1.5d12h", 1.5 * 86400 + 12 * 3600),
    ],
)
def test_duration_compound_forms(value: str, seconds: float) -> None:
    assert duration_secs(value) == seconds


@pytest.mark.parametrize(
    "value",
    ["", "7", "30m2h", "1h2h", "5x", "1d2", "d", "1dh"],
)
def test_duration_rejects_malformed(value: str) -> None:
    assert duration_secs(value) is None


def test_fmt_duration_roundtrips_through_parser() -> None:
    """Every string `fmt_duration` emits parses back to the value it displays
    (the input truncated to the smallest unit shown)."""
    grid = itertools.product((0, 1, 3), (0, 1, 23), (0, 1, 59), (0, 1, 59))
    for d, h, m, s in grid:
        total = ((d * 24 + h) * 60 + m) * 60 + s
        text = fmt_duration(total)
        parsed = duration_secs(text)
        assert parsed is not None, f"{text!r} did not parse"
        if total < 3600:
            expect = total  # seconds always shown
        elif total < 86400:
            expect = total - total % 60  # h?m form drops seconds
        else:
            expect = total - total % 3600  # d?h form drops minutes
        assert parsed == expect, f"{total} -> {text!r} -> {parsed}, want {expect}"


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
        project,
        "run",
        "-n",
        "fut",
        "--",
        "sh",
        "-c",
        "echo hello",
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
