"""`live tail -c +B` resumable byte cursor (consumes `at-byte` from trailer)."""

from __future__ import annotations

import re
from pathlib import Path


def test_tail_c_plus_emits_bytes_after_cursor(project: Path, run_live) -> None:
    """Probe at-byte, then `tail -c +B` should emit only bytes after position B."""
    run_live(
        project, "run", "-n", "bc", "--", "sh", "-c",
        "echo aaa; echo bbb; echo ccc",
    )
    # Get full read + trailer at-byte.
    full = run_live(project, "tail", "-vn", "+0", "bc")
    m = re.search(r"at-byte=(\d+)", full.stderr)
    assert m, full.stderr
    total = int(m.group(1))

    # Pick a midpoint cursor: end of first line ("aaa\r\n" = 5 bytes on disk).
    cursor = 5
    out = run_live(project, "tail", "-c", f"+{cursor}", "bc")
    text = out.stdout.replace("\r", "")
    assert "bbb" in text and "ccc" in text
    assert "aaa" not in text
    assert total == 15  # sanity: 3 lines * 5 bytes each on disk


def test_tail_c_plus_cursor_at_end_is_empty(project: Path, run_live) -> None:
    run_live(project, "run", "-n", "bc2", "--", "sh", "-c", "echo a")
    full = run_live(project, "tail", "-vn", "+0", "bc2")
    m = re.search(r"at-byte=(\d+)", full.stderr)
    total = int(m.group(1))

    out = run_live(project, "tail", "-c", f"+{total}", "bc2")
    assert out.stdout == ""


def test_tail_c_plus_cursor_ahead_warns(project: Path, run_live) -> None:
    run_live(project, "run", "-n", "bc3", "--", "sh", "-c", "echo a")
    full = run_live(project, "tail", "-vn", "+0", "bc3")
    m = re.search(r"at-byte=(\d+)", full.stderr)
    total = int(m.group(1))

    out = run_live(project, "tail", "-v", "-c", f"+{total + 1000}", "bc3")
    assert out.stdout == ""
    assert f"bytes={total + 1000}" in out.stderr
    assert f"> at-byte={total}" in out.stderr
    assert "check id" in out.stderr


def test_tail_c_minus_treated_as_count(project: Path, run_live) -> None:
    """`tail -c -K` is a no-op sign — same as `-c K` (last K bytes)."""
    run_live(project, "run", "-n", "bc4", "--", "sh", "-c", "echo aaa; echo bbb")
    a = run_live(project, "tail", "-c", "5", "bc4").stdout
    b = run_live(project, "tail", "-c", "-5", "bc4").stdout
    assert a == b
