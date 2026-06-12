"""Partial-line tail surfacing via tail -n +0."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_partial_line_surfaces_in_tail(
    project: Path, live_env, run_live, wait_for, wait_for_session
) -> None:
    # One complete line, a partial prompt without a newline, then sleep so the
    # partial sits there long enough for the reader to observe it.
    script = (
        "printf 'first complete line\\n'; "
        "printf 'Continue? [Y/n] '; "
        "sleep 10"
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "live.cli", "run", "-n", "prompt", "--",
         "sh", "-c", script],
        cwd=str(project),
        env=live_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        sess_dir = wait_for_session()
        stream = sess_dir / "stream.0000.log"
        idx = sess_dir / "lines.0000.idx"

        def has_partial() -> bool:
            try:
                s = stream.read_bytes()
                i = idx.read_bytes()
            except FileNotFoundError:
                return False
            return len(i) == 40 and b"Continue?" in s and not s.endswith(b"\n")

        assert wait_for(has_partial, timeout=8.0), "partial state never appeared"

        poll = run_live(project, "tail", "-vn", "+0", "prompt")
        out = poll.stdout.replace("\r", "")
        assert "first complete line" in out
        assert "Continue? [Y/n] " in out
        assert "live: partial-line bytes=" in poll.stderr
        assert "age=" in poll.stderr
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
