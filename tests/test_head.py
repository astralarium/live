"""`live head` — first N lines / K bytes, symmetric to `live tail`."""

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
