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
    # last_line = first_retained + emitted - 1 = 1 + 4 - 1 = 4
    assert "at-line=4" in out.stderr
    assert "at-time=" in out.stderr
    m = re.search(r"at-byte=(\d+)", out.stderr)
    assert m, out.stderr
    # at-byte = emitted bytes on disk: 4 lines * len("lineN\r\n") = 28.
    assert int(m.group(1)) == 28


def test_head_t_complements_tail_t(project: Path, run_live) -> None:
    """head -t T and tail -t T should partition the session at cursor T."""
    run_live(
        project, "run", "-n", "split", "--", "sh", "-c",
        "echo early-1; echo early-2; sleep 0.6; echo late-1; echo late-2",
    )
    # Probe at-time at the end to learn the timestamp range.
    full = run_live(project, "tail", "-vn", "+0", "split")
    m = re.search(r"at-time=([0-9.]+)", full.stderr)
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
