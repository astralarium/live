"""`live head` — first N lines / K bytes / `-t T`, symmetric to `live tail`."""

from __future__ import annotations

import re
from pathlib import Path


def _setup_session(project: Path, run_live) -> str:
    cmd = "; ".join(f"echo line{i}" for i in range(1, 11))  # line1..line10
    run_live(project, "run", "-n", "h", "--", "sh", "-c", cmd)
    return "h"


def test_head_default_emits_first_ten(project: Path, run_live) -> None:
    sel = _setup_session(project, run_live)
    out = run_live(project, "head", sel).stdout.replace("\r", "")
    lines = [ln for ln in out.split("\n") if ln]
    assert lines == [f"line{i}" for i in range(1, 11)]


def test_head_n_limits_to_first_n(project: Path, run_live) -> None:
    sel = _setup_session(project, run_live)
    out = run_live(project, "head", "-n", "3", sel).stdout.replace("\r", "")
    lines = [ln for ln in out.split("\n") if ln]
    assert lines == ["line1", "line2", "line3"]


def test_head_c_limits_to_first_k_bytes(project: Path, run_live) -> None:
    sel = _setup_session(project, run_live)
    # On disk each line is "lineN\r\n" = 7 bytes (PTY appends \r). Asking for 7
    # bytes returns exactly the first line.
    out = run_live(project, "head", "-c", "7", sel)
    assert out.stdout.replace("\r", "") == "line1\n"


def test_head_verbose_trailer_carries_cursor(project: Path, run_live) -> None:
    sel = _setup_session(project, run_live)
    out = run_live(project, "head", "-vn", "4", sel)
    # 4 lines emitted -> next-line = 5; 4 * len("lineN\r\n") = 28 bytes on disk.
    assert "next-line=5" in out.stderr
    assert "last-time=" in out.stderr
    m = re.search(r"next-byte=(\d+)", out.stderr)
    assert m, out.stderr
    assert int(m.group(1)) == 28


def test_head_n_minus_drops_last_k(project: Path, run_live) -> None:
    """`head -n -K` matches GNU: all lines except the last K."""
    sel = _setup_session(project, run_live)  # 10 lines: line1..line10
    out = run_live(project, "head", "-n", "-3", sel).stdout.replace("\r", "")
    assert out.splitlines() == [f"line{i}" for i in range(1, 8)]  # drop last 3


def test_head_n_minus_K_ge_total_is_empty(project: Path, run_live) -> None:
    """Drop count >= total lines yields empty output."""
    sel = _setup_session(project, run_live)
    out = run_live(project, "head", "-n", "-99", sel).stdout
    assert out == ""


def test_head_c_minus_drops_last_k_bytes(project: Path, run_live) -> None:
    """`head -c -K` matches GNU: all bytes except the last K (raw on-disk bytes)."""
    sel = _setup_session(project, run_live)
    full = run_live(project, "cat", sel, text=False).stdout
    out = run_live(project, "head", "-c", "-5", sel, text=False).stdout
    assert out == full[:-5]


def test_head_n_plus_treated_as_count(project: Path, run_live) -> None:
    """`head -n +N` is a no-op sign — same as `-n N`."""
    sel = _setup_session(project, run_live)
    out = run_live(project, "head", "-n", "+3", sel).stdout.replace("\r", "")
    assert out.splitlines() == ["line1", "line2", "line3"]


def test_head_t_complements_tail_t(project: Path, run_live) -> None:
    """head -t T and tail -t T should partition the session at cursor T."""
    run_live(
        project, "run", "-n", "split", "--", "sh", "-c",
        "echo early-1; echo early-2; sleep 0.6; echo late-1; echo late-2",
    )
    # Probe the trailer time to learn the timestamp range.
    full = run_live(project, "tail", "-vn", "+0", "split")
    m = re.search(r"last-time=([0-9.]+)", full.stderr)
    assert m, full.stderr
    end_t = float(m.group(1))
    cut = end_t - 0.3  # between early and late writes

    head = run_live(project, "head", "-t", f"{cut:.6f}", "split")
    head_body = head.stdout.replace("\r", "")
    assert "early-1" in head_body and "early-2" in head_body
    assert "late-1" not in head_body and "late-2" not in head_body

    tail = run_live(project, "tail", "-t", f"{cut:.6f}", "split")
    tail_body = tail.stdout.replace("\r", "")
    assert "late-1" in tail_body and "late-2" in tail_body
    assert "early-1" not in tail_body and "early-2" not in tail_body
